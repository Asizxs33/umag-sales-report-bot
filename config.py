import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
UMAG_PHONE = os.environ["UMAG_PHONE"]
UMAG_PASSWORD = os.environ["UMAG_PASSWORD"]

ALLOWED_TELEGRAM_USER_IDS = [
    int(uid) for uid in os.environ.get("ALLOWED_TELEGRAM_USER_IDS", "").split(",") if uid.strip()
]

# Postgres-строка подключения к базе KPI-дашборда (iposuda-kpi-web) --
# обязательна: список сотрудников (employees.load_employees) теперь
# читается напрямую оттуда, поэтому без неё не работают ни /report,
# ни ежедневная синхронизация /sync.
DASHBOARD_DATABASE_URL = os.environ.get("DASHBOARD_DATABASE_URL", "")

# Время ежедневной авто-синхронизации (локальное время сервера, где крутится бот).
SYNC_HOUR = int(os.environ.get("SYNC_HOUR", "21"))
SYNC_MINUTE = int(os.environ.get("SYNC_MINUTE", "30"))

# Кому слать сводку по итогам ежедневной синхронизации; по умолчанию -- всем
# из ALLOWED_TELEGRAM_USER_IDS.
SYNC_NOTIFY_USER_IDS = [
    int(uid) for uid in os.environ.get("SYNC_NOTIFY_USER_IDS", "").split(",") if uid.strip()
] or ALLOWED_TELEGRAM_USER_IDS

# Время проверки дневной нормы (2 отзыва + 2 на 2 этаж за вчера) и кому
# слать -- по умолчанию всем из ALLOWED_TELEGRAM_USER_IDS, как и остальные
# уведомления. Запускается утром, чтобы вечерний ввод данных за вчера уже
# точно попал в базу.
ALERT_HOUR = int(os.environ.get("ALERT_HOUR", "9"))
ALERT_MINUTE = int(os.environ.get("ALERT_MINUTE", "0"))
ALERT_NOTIFY_USER_IDS = [
    int(uid) for uid in os.environ.get("ALERT_NOTIFY_USER_IDS", "").split(",") if uid.strip()
] or ALLOWED_TELEGRAM_USER_IDS
