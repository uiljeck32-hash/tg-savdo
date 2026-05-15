import os
from dotenv import load_dotenv

load_dotenv()

# Telegram Bot Token
BOT_TOKEN = os.getenv("BOT_TOKEN", "8812768181:AAFu5ZRbZ7ev_lrEq1ALTUefgKmhxRdOuAw")

# Admin Telegram ID
ADMIN_ID = os.getenv("ADMIN_ID", "6020543252")

# Groq API Key
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "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")