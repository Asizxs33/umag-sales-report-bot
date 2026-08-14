import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
UMAG_PHONE = os.environ["UMAG_PHONE"]
UMAG_PASSWORD = os.environ["UMAG_PASSWORD"]

ALLOWED_TELEGRAM_USER_IDS = [
    int(uid) for uid in os.environ.get("ALLOWED_TELEGRAM_USER_IDS", "").split(",") if uid.strip()
]
