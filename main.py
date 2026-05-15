import logging
import asyncio
import os
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from config import BOT_TOKEN, ADMIN_ID, GROQ_API_KEY
from products import PRODUCTS
from groq import AsyncGroq
from aiohttp import web

# Logging sozlamalari
logging.basicConfig(level=logging.INFO)

# Bot va Groq obyektlari
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
client = AsyncGroq(api_key=GROQ_API_KEY)

SYSTEM_PROMPT = f"""
Sening isming Jamshid. 20 yoshlar atrofidasan. Vodiy (Andijon, Farg'ona, Namangan) tomondanisan. 
Texnikani, noutbuk va kompyuterlarni juda zo'r tushunadigan mutaxassisan. 
Xushmuomala, xushchaqchaq va ajoyib sotuvchisan. 
Gaplaringda vodiycha shira bo'lsin (masalan, 'aka', 'uka', 'baraka toping', 'moshshina'), lekin me'yorni bil. 
Insondek gapir, o'zingni AI ekanligingni hech qachon aytma. Javoblaring qisqa va mazmunli bo'lsin.

Do'konimizdagi tovarlar: {PRODUCTS}

Mijoz narsa olmoqchi bo'lsa, 'olaman' desa yoki shunga shama qilsa, albatta telefon raqamini so'ra. 
Haqiqiy insondek muloqot qil.
"""

async def get_ai_response(user_input):
    try:
        completion = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_input}
            ],
            temperature=0.7,
            max_tokens=500,
        )
        return completion.choices[0].message.content
    except Exception as e:
        logging.error(f"AI Error: {e}")
        return "Aka, bir minut kutasizmi? Aloqa biroz yomon bo'lyapti, hozir to'g'irlab yuboraman."

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Assalomu alaykum aka! Charchamayapsizmi? \n"
        "Men Jamshidman, nima xizmat? Noutbukmi yoki kompyuter? O'zim boplab beraman!"
    )

@dp.message(F.contact)
async def handle_contact(message: types.Message):
    user = message.from_user
    contact = message.contact
    
    admin_msg = (
        "🔥 *Yangi Mijoz! (AI orqali)*\n\n"
        f"Ismi: {user.first_name}\n"
        f"Username: @{user.username if user.username else 'yoq'}\n"
        f"Nomeri: {contact.phone_number}\n"
        f"ID: {user.id}\n"
    )
    
    try:
        target_id = int(ADMIN_ID) if str(ADMIN_ID).isdigit() else ADMIN_ID
        await bot.send_message(target_id, admin_msg, parse_mode="Markdown")
        await message.answer("Rahmat aka! Akalarimiz hozir telefon qilishadi, gaplashib olasizlar. Baraka toping!")
    except Exception as e:
        logging.error(f"Admin error: {e}")
        await message.answer("Tushunarli aka, yozib oldim!")

@dp.message()
async def handle_text(message: types.Message):
    if not message.text:
        return

    response_text = await get_ai_response(message.text)
    
    check_text = message.text.lower() + " " + response_text.lower()
    purchase_words = ["olaman", "maqul", "ma'qul", "bo'ladi", "nomer", "raqam", "telefon"]
    
    if any(word in check_text for word in purchase_words):
        kb = [[KeyboardButton(text="📱 Nomerimni yuborish", request_contact=True)]]
        keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=True)
        await message.answer(response_text, reply_markup=keyboard)
    else:
        await message.answer(response_text)

# Render uchun oddiy web server
async def handle_health(request):
    return web.Response(text="Bot is running!")

async def main():
    # Web serverni sozlash
    app = web.Application()
    app.router.add_get("/", handle_health)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Render PORT muhit o'zgaruvchisidan foydalanadi
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    
    # Ham botni, ham serverni parallel ishga tushiramiz
    await asyncio.gather(
        site.start(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot to'xtatildi")
