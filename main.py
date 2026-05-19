import asyncio
import logging
import os
import time
from typing import Callable, Dict, Any, Awaitable
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, ChatMemberUpdated, ChatPermissions,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramForbiddenError
from dotenv import load_dotenv

from database import Database

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found in .env file")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

db = Database()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

PENDING_CONFIRMATIONS = {}


def get_user_mention(user) -> str:
    if user.username:
        return f"@{user.username}"
    return f"<a href='tg://user?id={user.id}'>{user.first_name}</a>"


def get_name(user) -> str:
    return user.first_name or user.username or f"ID: {user.id}"


ADMIN_CACHE = {}  # key: (chat_id, user_id) -> value: (is_admin, expiry_timestamp)

async def is_chat_admin(bot: Bot, user_id: int, chat_id: int) -> bool:
    now = time.time()
    cache_key = (chat_id, user_id)
    if cache_key in ADMIN_CACHE:
        is_adm, expiry = ADMIN_CACHE[cache_key]
        if now < expiry:
            return is_adm

    try:
        member = await bot.get_chat_member(chat_id, user_id)
        is_adm = member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
        # Cache for 60 seconds
        ADMIN_CACHE[cache_key] = (is_adm, now + 60)
        return is_adm
    except Exception:
        return False


async def is_bot_admin_in_chat(bot: Bot, chat_id: int) -> bool:
    try:
        me = await bot.me()
        member = await bot.get_chat_member(chat_id, me.id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except Exception:
        return False


async def resolve_target_user(message: Message) -> types.User | None:
    if message.reply_to_message:
        return message.reply_to_message.from_user
    args = message.text.split()[1:]
    if args:
        try:
            user_id = int(args[0])
            db.upsert_user(user_id)
            return types.User(id=user_id, is_bot=False, first_name=f"ID: {user_id}")
        except ValueError:
            return None
    return None


async def mute_user(chat_id: int, user_id: int) -> str | None:
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=ChatPermissions(can_send_messages=False),
        )
        return None
    except TelegramForbiddenError:
        return "❌ Botda admin huquqi yetarli emas. Iltimos, botni guruhga ADMIN qilib tayinlang."
    except Exception as e:
        return f"❌ Xatolik: {e}"


async def unmute_user(chat_id: int, user_id: int) -> str | None:
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_invite_users=True,
            ),
        )
        return None
    except TelegramForbiddenError:
        return "❌ Botda admin huquqi yetarli emas. Iltimos, botni guruhga ADMIN qilib tayinlang."
    except Exception as e:
        return f"❌ Xatolik: {e}"


async def check_and_restrict(user_id: int, group_id: int):
    status = db.get_force_add_status(group_id)
    if not status["force_add_enabled"]:
        return
    if db.is_user_priv(user_id):
        return
    required = status["force_add_count"]
    count = db.get_user_invite_count(user_id, group_id)
    if count < required:
        err = await mute_user(group_id, user_id)
        if err:
            logger.warning(f"Mute failed for {user_id} in {group_id}: {err}")


async def check_and_unmute(user_id: int, group_id: int):
    status = db.get_force_add_status(group_id)
    if not status["force_add_enabled"]:
        return
    required = status["force_add_count"]
    count = db.get_user_invite_count(user_id, group_id)
    if count >= required:
        err = await unmute_user(group_id, user_id)
        if err:
            logger.warning(f"Unmute failed for {user_id} in {group_id}: {err}")


async def scan_and_mute_members(group_id: int, required_count: int) -> tuple:
    admin_ids = set()
    try:
        admins = await bot.get_chat_administrators(group_id)
        admin_ids = {a.user.id for a in admins}
    except Exception as e:
        logger.warning(f"Could not get admins for {group_id}: {e}")

    member_ids = db.get_all_group_members(group_id)
    muted = 0
    already_ok = 0
    errors = 0

    for uid in member_ids:
        if uid in admin_ids:
            continue
        if db.is_user_priv(uid):
            continue
        count = db.get_user_invite_count(uid, group_id)
        if count < required_count:
            err = await mute_user(group_id, uid)
            if err:
                errors += 1
                logger.warning(err)
            else:
                muted += 1
            # Add a small delay to avoid Telegram rate limiting (Flood Control)
            await asyncio.sleep(0.05)
        else:
            already_ok += 1

    return muted, already_ok, errors, len(admin_ids)


async def check_channel_subscription_for_user(bot: Bot, user_id: int, channels: list) -> list:
    not_subscribed = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch["channel_id"], user_id)
            if member.status == ChatMemberStatus.LEFT:
                not_subscribed.append(ch)
        except Exception:
            not_subscribed.append(ch)
    return not_subscribed


class CheckRequirementsMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any]
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)

        message = event
        if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            return await handler(event, data)

        if not message.from_user or message.from_user.is_bot:
            return await handler(event, data)

        user_id = message.from_user.id
        group_id = message.chat.id

        # Allow certain commands so users can interact with the bot to check requirements
        if message.text and message.text.startswith(("/", "!")):
            command = message.text.split()[0].split("@")[0].lower()
            if command in ("/checkme", "/mymembers", "/help", "/start"):
                return await handler(event, data)

        # Check if the user is an admin
        is_admin = await is_chat_admin(message.bot, user_id, group_id)
        if is_admin:
            return await handler(event, data)

        # Check if user has privilege (is_priv = 1)
        if db.is_user_priv(user_id):
            return await handler(event, data)

        # 1. Check Channel Subscription (Force Subscribe)
        channels = db.get_group_channels(group_id)
        if channels:
            not_subscribed = await check_channel_subscription_for_user(message.bot, user_id, channels)
            if not_subscribed:
                channel_links = []
                for ch in not_subscribed:
                    if ch["channel_title"]:
                        channel_links.append(f"<b>{ch['channel_title']}</b>")
                    else:
                        channel_links.append(f"<b>ID: {ch['channel_id']}</b>")
                
                channel_names = ", ".join(channel_links)
                warning = (
                    f"⚠️ {get_user_mention(message.from_user)}, guruhda yozish uchun "
                    f"quyidagi kanallarga a'zo bo'lishingiz kerak:\n{channel_names}"
                )
                try:
                    await message.delete()
                except Exception:
                    pass

                text_time = db.get_force_add_text_time(group_id)
                try:
                    sent = await message.answer(warning, parse_mode="HTML")
                    if text_time > 0:
                        await asyncio.sleep(text_time)
                        await sent.delete()
                except Exception:
                    pass
                return

        # 2. Check Force Add Count
        status = db.get_force_add_status(group_id)
        if status["force_add_enabled"]:
            required = status["force_add_count"]
            count = db.get_user_invite_count(user_id, group_id)
            if count < required:
                # Automatically mute the user since they haven't met the count
                await mute_user(group_id, user_id)

                custom_text = db.get_force_add_text(group_id)
                warning = (
                    f"⚠️ {get_user_mention(message.from_user)}, guruhda yozish uchun kamida "
                    f"<b>{required}</b> ta odam qo'shishingiz kerak!\n"
                    f"Siz qo'shgan odamlar: <b>{count}/{required}</b> ta.\n"
                    f"Yana <b>{required - count}</b> ta odam qo'shing."
                )
                if custom_text:
                    warning += f"\n\n{custom_text}"

                try:
                    await message.delete()
                except Exception:
                    pass

                text_time = db.get_force_add_text_time(group_id)
                try:
                    sent = await message.answer(warning, parse_mode="HTML")
                    if text_time > 0:
                        await asyncio.sleep(text_time)
                        await sent.delete()
                except Exception:
                    pass
                return

        return await handler(event, data)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    first_name = user.first_name or "Foydalanuvchi"
    db.upsert_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name
    )
    text = (
        f"🤖 Botga xush kelibsiz, {first_name}!\n\n"
        f"📊 Men Guruhga kim qancha odam qo'shganligini aytib beruvchi botman.\n\n"
        f"Bot orqali Guruhingizga istagancha odam yigʻib olasiz. "
        f"/help - buyrug'i orqali bot buyruqlari haqida ma'lumot olishingiz mumkin☑️\n\n"
        f"⚠️ Bot to'g'ri ishlashi uchun ADMIN huquqini berishingiz kerak"
    )
    await message.answer(text)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "🤖 Botimizning buyruqlari!\n\n"
        "/mymembers - 📊 Siz qo'shgan odamlar soni!\n"
        "______\n"
        "/yourmembers - 📊 Reply qilingan odamning, guruhga qo'shgan odamlar soni!\n"
        "______\n"
        "/top - 🏆 Eng ko'p odam qo'shgan 10 talik!\n"
        "______\n"
        "/delson - 🗑 Guruhga odam qo'shganlarni barchasini tozalash!\n"
        "______\n"
        "/clean - 🧹 Reply qilingan xabar egasini ma'lumotlarini 0 ga tenglash!\n\n"
        "👥 Guruhga odam yigʻish buyruqlari\n\n"
        "/add - majburiy odam qo'shish holatini ko'rish\n"
        "/add 10 - majburiy odam qo'shishni yoqish (10 ta odam shart)\n"
        "/add off - o'chirish\n"
        "_____\n"
        "/textforce - majburiy odam qo'shish matnini tagiga matn qo'shish\n"
        "Namuna: /textforce *Salom*\n\n"
        "/textforce 0 - majburiy odam qo'shish matnini o'chirib qo'yish!\n\n"
        "/text_time - majburiy odam qo'shish matni avtomatik o'chish vaqti!\n\n"
        "/checkme - Majburiy odam qo'shish shartini tekshirish!\n"
        "_____\n"
        "/deforce (id yoki reply) - majburiy odam qo'shish ma'lumotini tozalash!\n\n"
        "/plus (id yoki reply) - balni boshqa foydalanuvchiga o'tkazish.\n"
        "/plus <manba> (reply) - manba foydalanuvchi balini reply ga o'tkazish\n\n"
        "/priv (id yoki reply) - imtiyoz berish (majburiy tizimdan xoli)\n"
        "/unpriv (id yoki reply) - imtiyozni qaytarib olish\n"
        "_____\n"
        "Kanal bilan bog'lash:\n"
        "/link @kanalusername - kanalni guruhga ulash\n"
        "/unlink - barcha ulangan kanallarni o'chirish"
    )
    await message.answer(text)


@dp.message(Command("mymembers"))
async def cmd_mymembers(message: Message):
    user_id = message.from_user.id

    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        group_id = message.chat.id
        count = db.get_user_invite_count(user_id, group_id)
        await message.answer(f"📊 Siz qo'shgan odamlar soni: {count} ta")
    else:
        stats = db.get_user_stats(user_id)
        if not stats:
            await message.answer("📊 Siz hali hech kimni guruhga qo'shmagansiz.")
            return
        total = sum(s["invite_count"] for s in stats)
        text = f"📊 Sizning umumiy statistikangiz:\nJami: {total} ta\n\n"
        for s in stats:
            title = s["title"] or "Noma'lum guruh"
            text += f"  {title}: {s['invite_count']} ta\n"
        await message.answer(text)


@dp.message(Command("yourmembers"))
async def cmd_yourmembers(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("Bu buyruq faqat guruhlarda ishlaydi.")
        return
    if not message.reply_to_message:
        await message.answer("Iltimos, biror foydalanuvchiga reply qiling.")
        return
    target_user = message.reply_to_message.from_user
    group_id = message.chat.id
    count = db.get_user_invite_count(target_user.id, group_id)
    name = get_user_mention(target_user)
    await message.answer(f"📊 {name} qo'shgan odamlar soni: {count} ta", parse_mode="HTML")


@dp.message(Command("top"))
async def cmd_top(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("Bu buyruq faqat guruhlarda ishlaydi.")
        return
    group_id = message.chat.id
    top_inviters = db.get_group_top_inviters(group_id, limit=10)
    if not top_inviters:
        await message.answer("🏆 Hali hech kim taklif qilinmagan.")
        return
    group_title = message.chat.title or "Guruh"
    text = f"🏆 TOP 10 Taklif qiluvchilar - {group_title}\n\n"
    for i, inviter in enumerate(top_inviters, 1):
        name = inviter["username"] or inviter["first_name"] or f"ID: {inviter['user_id']}"
        text += f"{i}. {name} - {inviter['invite_count']} ta\n"
    await message.answer(text)


@dp.message(Command("checkme"))
async def cmd_checkme(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("Bu buyruq faqat guruhlarda ishlaydi.")
        return
    user_id = message.from_user.id
    group_id = message.chat.id
    status = db.get_force_add_status(group_id)
    if not status["force_add_enabled"]:
        await message.answer("✅ Bu guruhda majburiy odam qo'shish talabi yo'q.")
        return
    required = status["force_add_count"]
    count = db.get_user_invite_count(user_id, group_id)
    if count >= required:
        await check_and_unmute(user_id, group_id)
        await message.answer(f"✅ Siz {count} ta odam qo'shgansiz, talab: {required} ta. Mutingiz olib tashlandi!")
    else:
        await message.answer(f"⚠️ Siz {count}/{required} ta odam qo'shgansiz. Yana {required - count} ta qo'shishingiz kerak.")


@dp.message(Command("add"))
async def cmd_add(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("Bu buyruq faqat guruhlarda ishlaydi.")
        return
    if not await is_chat_admin(bot, message.from_user.id, message.chat.id):
        await message.answer("Bu buyruqdan faqat adminlar foydalanishi mumkin.")
        return

    args = message.text.split()[1:]
    group_id = message.chat.id

    if not args:
        status = db.get_force_add_status(group_id)
        if status["force_add_enabled"]:
            total_members = 0
            try:
                chat = await bot.get_chat(group_id)
                total_members = chat.member_count or 0
            except Exception:
                pass
            text = (
                f"✅ Majburiy odam qo'shish yoqilgan\n"
                f"Talab: {status['force_add_count']} ta odam\n"
                f"Guruh a'zolari: ~{total_members} ta"
            )
        else:
            text = "❌ Majburiy odam qo'shish o'chirilgan."
        await message.answer(text)
        return

    if args[0].lower() == "off":
        db.set_force_add(group_id, False)
        await message.answer(
            "✅ Majburiy odam qo'shish o'chirildi!\n\n"
            "Cheklovda qolgan a'zolar /checkme buyrug'i orqali ovozsiz rejimdan chiqishi mumkin."
        )
        return

    try:
        count = int(args[0])
        if count <= 0:
            await message.answer("❗️ Iltimos, musbat son kiriting.")
            return
    except ValueError:
        await message.answer("❗️ Iltimos, to'g'ri son kiriting.")
        return

    db.set_force_add(group_id, True, count)
    await message.answer(
        f"✅ Majburiy odam qo'shish yoqildi! Talab: {count} ta odam.\n\n"
        f"🔄 Eskidan bor a'zolar tekshirilmoqda..."
    )

    muted, ok, errs, admins = await scan_and_mute_members(group_id, count)
    await message.answer(
        f"📊 Skaner natijasi:\n"
        f"  • Adminlar: {admins} ta (cheklanmadi)\n"
        f"  • Talabni bajargan: {ok} ta\n"
        f"  • Mute qilindi: {muted} ta\n"
        f"{'  • Xatolik: ' + str(errs) + ' ta (bot admin huquqiga ega emas)' if errs else ''}\n\n"
        f"Shartni bajarmaganlar ovozsiz rejimga o'tkazildi.\n"
        f"Yangi odam qo'shilganda yoki /checkme buyrug'i bilan cheklov olib tashlanadi."
    )


@dp.message(Command("textforce"))
async def cmd_textforce(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("Bu buyruq faqat guruhlarda ishlaydi.")
        return
    if not await is_chat_admin(bot, message.from_user.id, message.chat.id):
        await message.answer("Bu buyruqdan faqat adminlar foydalanishi mumkin.")
        return
    args = message.text.split(maxsplit=1)
    group_id = message.chat.id
    if len(args) < 2:
        current_text = db.get_force_add_text(group_id)
        if current_text:
            await message.answer(f"📝 Hozirgi matn:\n{current_text}")
        else:
            await message.answer("📝 Hozircha matn o'rnatilmagan.")
        return
    text = args[1]
    if text == "0":
        db.set_force_add_text(group_id, "")
        await message.answer("✅ Majburiy odam qo'shish matni o'chirildi!")
        return
    db.set_force_add_text(group_id, text)
    await message.answer(f"✅ Matn qo'shildi.")


@dp.message(Command("text_time"))
async def cmd_text_time(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("Bu buyruq faqat guruhlarda ishlaydi.")
        return
    if not await is_chat_admin(bot, message.from_user.id, message.chat.id):
        await message.answer("Bu buyruqdan faqat adminlar foydalanishi mumkin.")
        return
    args = message.text.split()[1:]
    group_id = message.chat.id
    if not args:
        current = db.get_force_add_text_time(group_id)
        if current:
            await message.answer(f"⏱ Hozirgi vaqt: {current} soniya")
        else:
            await message.answer("⏱ Hozircha vaqt o'rnatilmagan.")
        return
    try:
        seconds = int(args[0])
        if seconds < 0:
            await message.answer("❗️ Iltimos, musbat son kiriting.")
            return
    except ValueError:
        await message.answer("❗️ Iltimos, to'g'ri son kiriting.")
        return
    db.set_force_add_text_time(group_id, seconds)
    await message.answer(f"✅ Matn {seconds} soniyadan keyin avtomatik o'chadi.")


@dp.message(Command("deforce"))
async def cmd_deforce(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("Bu buyruq faqat guruhlarda ishlaydi.")
        return
    if not await is_chat_admin(bot, message.from_user.id, message.chat.id):
        await message.answer("Bu buyruqdan faqat adminlar foydalanishi mumkin.")
        return

    target_user = await resolve_target_user(message)
    if not target_user:
        await message.answer("❗️ Iltimos, biror foydalanuvchiga reply qiling yoki ID kiriting.")
        return

    name = get_user_mention(target_user)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Ha", callback_data=f"confirm_deforce:{target_user.id}"),
            InlineKeyboardButton(text="❌ Yo'q", callback_data="cancel_action")
        ]
    ])
    await message.answer(
        f"{name} ma'lumotlarini tozalash va himoyaga olishni tasdiqlaysizmi?",
        parse_mode="HTML", reply_markup=keyboard
    )


@dp.message(Command("plus"))
async def cmd_plus(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("Bu buyruq faqat guruhlarda ishlaydi.")
        return
    if not await is_chat_admin(bot, message.from_user.id, message.chat.id):
        await message.answer("Bu buyruqdan faqat adminlar foydalanishi mumkin.")
        return

    if not message.reply_to_message:
        await message.answer("❗️ Bal o'tkazish uchun biror foydalanuvchiga reply qiling.\n"
                             "Namuna: /plus (reply) - o'z balingizni o'tkazadi\n"
                             "Namuna: /plus 123456 (reply) - 123456 ID li user balini o'tkazadi")
        return

    target_user = message.reply_to_message.from_user
    group_id = message.chat.id
    args = message.text.split()[1:]
    source_user_id = message.from_user.id

    if args:
        try:
            source_user_id = int(args[0])
        except ValueError:
            await message.answer("❗️ Noto'g'ri ID formati.")
            return

    if source_user_id == target_user.id:
        await message.answer("❗️ O'z-o'ziga bal o'tkazib bo'lmaydi.")
        return

    from_count = db.get_user_invite_count(source_user_id, group_id)
    if from_count == 0:
        await message.answer("❗️ Bu foydalanuvchining balansi 0.")
        return

    source_mention = get_user_mention(types.User(id=source_user_id, is_bot=False, first_name=f"ID: {source_user_id}"))
    target_mention = get_user_mention(target_user)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Ha", callback_data=f"confirm_plus:{source_user_id}:{target_user.id}"),
            InlineKeyboardButton(text="❌ Yo'q", callback_data="cancel_action")
        ]
    ])

    await message.answer(
        f"{source_mention} ning {from_count} ta balini {target_mention} ga o'tkazishni tasdiqlaysizmi?\n\n"
        f"⚠️ Bu amalni qaytarib bo'lmaydi!",
        parse_mode="HTML", reply_markup=keyboard
    )


@dp.message(Command("priv"))
async def cmd_priv(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("Bu buyruq faqat guruhlarda ishlaydi.")
        return
    if not await is_chat_admin(bot, message.from_user.id, message.chat.id):
        await message.answer("Bu buyruqdan faqat adminlar foydalanishi mumkin.")
        return
    target_user = await resolve_target_user(message)
    if not target_user:
        await message.answer("❗️ Iltimos, biror foydalanuvchiga reply qiling yoki ID kiriting.")
        return
    db.set_user_priv(target_user.id, True)
    name = get_user_mention(target_user)
    await message.answer(f"✅ {name} ga imtiyoz berildi! Endi majburiy tizimdan xoli.", parse_mode="HTML")


@dp.message(Command("unpriv"))
async def cmd_unpriv(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("Bu buyruq faqat guruhlarda ishlaydi.")
        return
    if not await is_chat_admin(bot, message.from_user.id, message.chat.id):
        await message.answer("Bu buyruqdan faqat adminlar foydalanishi mumkin.")
        return
    target_user = await resolve_target_user(message)
    if not target_user:
        await message.answer("❗️ Iltimos, biror foydalanuvchiga reply qiling yoki ID kiriting.")
        return
    db.set_user_priv(target_user.id, False)
    name = get_user_mention(target_user)
    await message.answer(f"✅ {name} ning imtiyozi olib tashlandi!", parse_mode="HTML")


@dp.message(Command("link"))
async def cmd_link(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("Bu buyruq faqat guruhlarda ishlaydi.")
        return
    if not await is_chat_admin(bot, message.from_user.id, message.chat.id):
        await message.answer("Bu buyruqdan faqat adminlar foydalanishi mumkin.")
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer("❗️ Kanal username kiriting.\nNamuna: /link @kanal_username")
        return

    channel_username = args[1].strip().lstrip("@")
    group_id = message.chat.id

    try:
        chat = await bot.get_chat(f"@{channel_username}")
    except TelegramForbiddenError:
        await message.answer(
            "❌ Bot kanalga kira olmadi.\n\n"
            "1. Botni kanalga admin qilib qo'shing\n"
            "2. Keyin qaytadan /link @kanalusername"
        )
        return
    except Exception as e:
        await message.answer(f"❌ Kanal topilmadi: {e}\n\n"
                             f"Kanal username to'g'riligini va bot kanalda admin ekanligini tekshiring.")
        return

    if chat.type not in (ChatType.CHANNEL,):
        await message.answer("❗️ Bu kanal emas. Iltimos, kanal username kiriting.")
        return

    if not await is_bot_admin_in_chat(bot, chat.id):
        await message.answer(
            "❌ Bot bu kanalda admin emas!\n\n"
            "Iltimos, avval botni kanalga admin qilib qo'shing, keyin qaytadan /link @kanalusername"
        )
        return

    channels = db.get_group_channels(group_id)
    if any(ch["channel_id"] == chat.id for ch in channels):
        await message.answer(f"ℹ️ Bu kanal ({chat.title}) allaqachon ulangan.")
        return

    db.add_group_channel(group_id, chat.id, chat.title)
    await message.answer(f"✅ {chat.title} kanali guruhga ulandi!\n"
                         f"Endi yangi a'zolar kanalga obuna bo'lishi shart.")


@dp.message(Command("unlink"))
async def cmd_unlink(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("Bu buyruq faqat guruhlarda ishlaydi.")
        return
    if not await is_chat_admin(bot, message.from_user.id, message.chat.id):
        await message.answer("Bu buyruqdan faqat adminlar foydalanishi mumkin.")
        return
    group_id = message.chat.id
    channels = db.get_group_channels(group_id)
    if not channels:
        await message.answer("❗️ Guruhda hech qanday kanal sozlanmagan.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Ha", callback_data="confirm_unlink"),
            InlineKeyboardButton(text="❌ Yo'q", callback_data="cancel_action")
        ]
    ])

    text = "Quyidagi kanallarni o'chirishni tasdiqlaysizmi?\n\n"
    for ch in channels:
        text += f"- {ch['channel_title'] or ch['channel_id']}\n"

    await message.answer(text, reply_markup=keyboard)


@dp.message(Command("delson"))
async def cmd_delson(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("Bu buyruq faqat guruhlarda ishlaydi.")
        return
    if not await is_chat_admin(bot, message.from_user.id, message.chat.id):
        await message.answer("Bu buyruqdan faqat adminlar foydalanishi mumkin.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Ha, hammasini tozala", callback_data="confirm_delson"),
            InlineKeyboardButton(text="❌ Yo'q", callback_data="cancel_action")
        ]
    ])
    await message.answer(
        "🗑 Guruhdagi barcha odam qo'shish ma'lumotlarini tozalashni tasdiqlaysizmi?\n\n⚠️ Bu amalni qaytarib bo'lmaydi!",
        reply_markup=keyboard
    )


@dp.message(Command("clean"))
async def cmd_clean(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("Bu buyruq faqat guruhlarda ishlaydi.")
        return
    if not await is_chat_admin(bot, message.from_user.id, message.chat.id):
        await message.answer("Bu buyruqdan faqat adminlar foydalanishi mumkin.")
        return

    target_user = await resolve_target_user(message)
    if not target_user:
        await message.answer("❗️ Iltimos, biror foydalanuvchiga reply qiling.")
        return

    name = get_user_mention(target_user)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Ha", callback_data=f"confirm_clean:{target_user.id}"),
            InlineKeyboardButton(text="❌ Yo'q", callback_data="cancel_action")
        ]
    ])
    await message.answer(
        f"🧹 {name} ma'lumotlarini 0 ga tenglashni tasdiqlaysizmi?\n⚠️ Bu amalni qaytarib bo'lmaydi!",
        parse_mode="HTML", reply_markup=keyboard
    )


@dp.callback_query()
async def handle_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = callback.data

    if data == "cancel_action":
        await callback.message.edit_text("❌ Amal bekor qilindi.")
        await callback.answer()
        return

    if data == "confirm_delson":
        if not await is_chat_admin(bot, user_id, callback.message.chat.id):
            await callback.answer("Siz admin emassiz!", show_alert=True)
            return
        group_id = callback.message.chat.id
        db.clear_group_invites(group_id)
        await callback.message.edit_text("🗑 Guruhdagi barcha odam qo'shish ma'lumotlari tozalandi!")
        await callback.answer()

    elif data.startswith("confirm_clean:"):
        if not await is_chat_admin(bot, user_id, callback.message.chat.id):
            await callback.answer("Siz admin emassiz!", show_alert=True)
            return
        target_id = int(data.split(":")[1])
        group_id = callback.message.chat.id
        db.clear_user_invites(target_id, group_id)
        await callback.message.edit_text(f"🧹 Foydalanuvchi ma'lumotlari 0 ga tenglandi!")
        await callback.answer()

    elif data.startswith("confirm_deforce:"):
        if not await is_chat_admin(bot, user_id, callback.message.chat.id):
            await callback.answer("Siz admin emassiz!", show_alert=True)
            return
        target_id = int(data.split(":")[1])
        group_id = callback.message.chat.id
        db.clear_user_invites(target_id, group_id)
        db.set_user_deforced(target_id, True)
        await callback.message.edit_text(f"✅ Foydalanuvchi ma'lumotlari tozalandi va himoyaga olindi!")
        await callback.answer()

    elif data.startswith("confirm_plus:"):
        if not await is_chat_admin(bot, user_id, callback.message.chat.id):
            await callback.answer("Siz admin emassiz!", show_alert=True)
            return
        parts = data.split(":")
        source_id = int(parts[1])
        target_id = int(parts[2])
        group_id = callback.message.chat.id

        from_count = db.get_user_invite_count(source_id, group_id)
        if from_count > 0:
            db.transfer_invites(source_id, target_id, group_id)
            await callback.message.edit_text(
                f"✅ {from_count} ta bal muvaffaqiyatli o'tkazildi!"
            )
        else:
            await callback.message.edit_text("❗️ Balans 0, o'tkazib bo'lmaydi.")
        await callback.answer()

    elif data == "confirm_unlink":
        if not await is_chat_admin(bot, user_id, callback.message.chat.id):
            await callback.answer("Siz admin emassiz!", show_alert=True)
            return
        group_id = callback.message.chat.id
        db.remove_group_channels(group_id)
        await callback.message.edit_text("✅ Barcha kanallar o'chirildi!")
        await callback.answer()


@dp.my_chat_member()
async def on_bot_chat_member_update(event: ChatMemberUpdated):
    group_id = event.chat.id
    new_status = event.new_chat_member.status

    if new_status == ChatMemberStatus.ADMINISTRATOR:
        db.set_group_admin_status(group_id, True)
        db.upsert_group(group_id, event.chat.title)
        logger.info(f"Bot admin bo'ldi: {group_id}")
    elif new_status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        db.set_group_admin_status(group_id, False)
        logger.info(f"Bot chiqarib yuborildi: {group_id}")


@dp.message(F.new_chat_members)
async def on_new_member(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    group_id = message.chat.id
    db.upsert_group(group_id, message.chat.title)

    for new_member in message.new_chat_members:
        if new_member.id == message.bot.id:
            continue

        db.upsert_user(
            user_id=new_member.id,
            username=new_member.username,
            first_name=new_member.first_name,
            last_name=new_member.last_name
        )

        inviter_id = message.from_user.id
        is_invited = False
        if inviter_id != new_member.id:
            db.record_invite(inviter_id, new_member.id, group_id)
            is_invited = True
            logger.info(f"{inviter_id} taklif qildi {new_member.id} ni {group_id} guruhiga")

            await check_and_unmute(inviter_id, group_id)

        welcome_text = (
            f"👋 Xush kelibsiz, {get_user_mention(new_member)}!\n"
            f"Guruhga qo'shilganingizdan xursandmiz."
        )
        if is_invited and inviter_id != new_member.id:
            inviter_name = get_user_mention(message.from_user)
            welcome_text += f"\n\nSizni {inviter_name} taklif qildi."

        try:
            await message.answer(welcome_text, parse_mode="HTML")
        except Exception:
            pass

        await check_and_restrict(new_member.id, group_id)

        channels = db.get_group_channels(group_id)
        if channels and not db.is_user_priv(new_member.id):
            not_subscribed = await check_channel_subscription_for_user(bot, new_member.id, channels)
            if not_subscribed:
                channel_names = ", ".join([
                    ch["channel_title"] or str(ch["channel_id"]) for ch in not_subscribed
                ])
                warning = (
                    f"⚠️ {get_user_mention(new_member)}, guruhda qolish uchun "
                    f"quyidagi kanallarga obuna bo'ling:\n{channel_names}"
                )
                text_time = db.get_force_add_text_time(group_id)
                try:
                    if text_time > 0:
                        sent = await message.answer(warning, parse_mode="HTML")
                        await asyncio.sleep(text_time)
                        try:
                            await sent.delete()
                        except Exception:
                            pass
                    else:
                        await message.answer(warning, parse_mode="HTML")
                except Exception:
                    pass


@dp.message(F.left_chat_member)
async def on_member_left(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    group_id = message.chat.id
    left_member = message.left_chat_member
    if left_member.id == message.bot.id:
        db.set_group_admin_status(group_id, False)
        return
    db.remove_member(left_member.id, group_id)
    logger.info(f"{left_member.id} guruhdan chiqdi: {group_id}")


async def main():
    logger.info("Bot ishga tushmoqda...")
    # Register outer middleware to check channel subscriptions and force add count for group messages
    dp.message.outer_middleware(CheckRequirementsMiddleware())
    try:
        me = await bot.me()
        logger.info(f"Bot muvaffaqiyatli ishga tushdi: @{me.username}")
    except Exception as e:
        logger.error(f"Botni ishga tushirishda xatolik: {e}")
        return
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
