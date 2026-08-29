import asyncio
import logging
import os
import tempfile
from datetime import datetime, timedelta

from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from config import (
    ALERT_HOUR,
    ALERT_MINUTE,
    ALERT_NOTIFY_USER_IDS,
    ALLOWED_TELEGRAM_USER_IDS,
    DASHBOARD_DATABASE_URL,
    SUMMARY_HOUR,
    SUMMARY_MINUTE,
    SYNC_API_SECRET,
    SYNC_HOUR,
    SYNC_MINUTE,
    SYNC_NOTIFY_USER_IDS,
    TELEGRAM_BOT_TOKEN,
    UMAG_PASSWORD,
    UMAG_PHONE,
)
from alerts import check_daily_norm, monthly_summary, weekly_summary
from daily_sync import sync_day
from employees import load_employees
from report import build_report
from umag_client import UmagClient, UmagError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sales_report_bot")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

umag = UmagClient(UMAG_PHONE, UMAG_PASSWORD)


class ReportFlow(StatesGroup):
    custom_date = State()


def _allowed_user_id(user_id: int) -> bool:
    if not ALLOWED_TELEGRAM_USER_IDS:
        return True
    return user_id in ALLOWED_TELEGRAM_USER_IDS


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user and not _allowed_user_id(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("У вас нет доступа к этому боту.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("У вас нет доступа к этому боту.")
            return
        return await handler(event, data)


dp.message.outer_middleware(AccessMiddleware())
dp.callback_query.outer_middleware(AccessMiddleware())


def _ensure_login():
    if umag.store_id is None:
        umag.login()
        umag.select_store("Iposuda")


def _period_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data="period:today"),
                InlineKeyboardButton(text="Вчера", callback_data="period:yesterday"),
            ],
            [InlineKeyboardButton(text="📅 Указать дату", callback_data="period:customdate")],
        ]
    )


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Привет! Собираю отчёт по продажам сотрудников из UMAG в Excel.\n\nЗа какой день?",
        reply_markup=_period_menu_markup(),
    )


@dp.message(F.text == "/report")
async def report_command(message: Message):
    await message.answer("За какой день?", reply_markup=_period_menu_markup())


@dp.callback_query(F.data.startswith("period:"))
async def period_selected(callback: CallbackQuery, state: FSMContext):
    choice = callback.data.split(":", 1)[1]

    if choice == "customdate":
        await state.set_state(ReportFlow.custom_date)
        await callback.message.edit_text("Напиши дату, например: 08.08.2026 или 2026-08-08")
        await callback.answer()
        return

    target = datetime.now() if choice == "today" else datetime.now() - timedelta(days=1)
    await callback.answer()
    await _send_report(callback.message, target.date())


@dp.message(ReportFlow.custom_date, F.text)
async def custom_date_entered(message: Message, state: FSMContext):
    text = message.text.strip()
    parsed_date = None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            parsed_date = datetime.strptime(text, fmt).date()
            break
        except ValueError:
            continue
    if parsed_date is None:
        await message.answer("Не понял дату. Напиши в формате 08.08.2026 или 2026-08-08.")
        return
    await state.clear()
    await _send_report(message, parsed_date)


async def _send_report(message: Message, report_date):
    try:
        _ensure_login()
    except UmagError as e:
        await message.answer(f"Не удалось подключиться к UMAG: {e}")
        return

    status = await message.answer(f"Собираю отчёт за {report_date.strftime('%d.%m.%Y')}...")

    try:
        employees = await load_employees()
    except RuntimeError as e:
        await status.edit_text(f"⚠️ {e}")
        return

    date_from = datetime(report_date.year, report_date.month, report_date.day)
    date_to = date_from.replace(hour=23, minute=59, second=59, microsecond=999000)

    rows = []
    not_found = []
    for store_name in sorted({emp.get("store", "Iposuda") for emp in employees}):
        umag.select_store(store_name)
        for emp in employees:
            if emp.get("store", "Iposuda") != store_name:
                continue
            seller = umag.find_seller(emp["name"])
            if not seller:
                not_found.append(emp["name"])
                stats = {"count": 0, "saleAmount": 0}
            else:
                stats = umag.sale_stats(seller["id"], date_from, date_to, all_pos=True)
            rows.append(
                {
                    "name": emp["name"],
                    "position": emp["position"],
                    "plan": emp["plan"],
                    "count": stats["count"],
                    "amount": stats["saleAmount"],
                }
            )

    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = os.path.join(tmp_dir, f"Отчет_продажи_{report_date.strftime('%d.%m.%Y')}.xlsx")
        build_report(report_date, rows, out_path)
        await status.delete()
        caption = None
        if not_found:
            caption = f"⚠️ Не нашёл в UMAG: {', '.join(not_found)}"
        await message.answer_document(FSInputFile(out_path), caption=caption)


def _format_sync_summary(summary: dict) -> str:
    lines = [f"✅ Синхронизация продаж за {summary['date']}: {len(summary['synced'])} сотрудников"]
    if summary["not_in_umag"]:
        lines.append(f"⚠️ Не нашёл в UMAG: {', '.join(summary['not_in_umag'])}")
    if summary["not_in_dashboard"]:
        lines.append(f"⚠️ Нет в KPI-дашборде: {', '.join(summary['not_in_dashboard'])}")
    return "\n".join(lines)


async def _run_sync(report_date) -> dict:
    _ensure_login()
    return await sync_day(umag, report_date)


@dp.message(F.text == "/sync")
async def sync_command(message: Message):
    if not DASHBOARD_DATABASE_URL:
        await message.answer("DASHBOARD_DATABASE_URL не настроен — синхронизация с дашбордом отключена.")
        return
    status = await message.answer("Синхронизирую сегодняшние продажи с KPI-дашбордом...")
    try:
        summary = await _run_sync(datetime.now().date())
    except Exception as e:
        log.exception("Manual sync failed")
        await status.edit_text(f"⚠️ Синхронизация не удалась: {e}")
        return
    await status.edit_text(_format_sync_summary(summary))


async def _scheduled_sync():
    if not DASHBOARD_DATABASE_URL:
        return
    log.info("Running scheduled daily sync")
    try:
        summary = await _run_sync(datetime.now().date())
        text = _format_sync_summary(summary)
    except Exception as e:
        log.exception("Scheduled sync failed")
        text = f"⚠️ Ежедневная синхронизация не удалась: {e}"
    for user_id in SYNC_NOTIFY_USER_IDS:
        try:
            await bot.send_message(user_id, text)
        except Exception:
            log.exception("Failed to notify user %s about sync result", user_id)


async def _scheduled_daily_norm_alert():
    if not DASHBOARD_DATABASE_URL:
        return
    yesterday = (datetime.now() - timedelta(days=1)).date()
    log.info("Checking daily norm for %s", yesterday)
    try:
        text = await check_daily_norm(yesterday)
    except Exception:
        log.exception("Daily norm check failed")
        return
    if not text:
        return
    for user_id in ALERT_NOTIFY_USER_IDS:
        try:
            await bot.send_message(user_id, text)
        except Exception:
            log.exception("Failed to notify user %s about daily norm", user_id)


async def _scheduled_weekly_summary():
    if not DASHBOARD_DATABASE_URL:
        return
    # Запускается в воскресенье вечером, после того как сегодняшняя (=
    # последнего дня недели) синхронизация уже прошла -- значит неделя
    # заканчивается СЕГОДНЯ, а не вчера.
    week_end = datetime.now().date()
    log.info("Building weekly summary ending %s", week_end)
    try:
        text = await weekly_summary(week_end)
    except Exception:
        log.exception("Weekly summary failed")
        return
    if not text:
        return
    for user_id in ALERT_NOTIFY_USER_IDS:
        try:
            await bot.send_message(user_id, text)
        except Exception:
            log.exception("Failed to notify user %s about weekly summary", user_id)


async def _scheduled_monthly_summary():
    if not DASHBOARD_DATABASE_URL:
        return
    # Запускается в последний день месяца вечером, после сегодняшней
    # синхронизации -- значит месяц заканчивается СЕГОДНЯ, а не вчера.
    target_month = datetime.now().date()
    log.info("Building monthly summary for %s", target_month)
    try:
        text = await monthly_summary(target_month)
    except Exception:
        log.exception("Monthly summary failed")
        return
    if not text:
        return
    for user_id in ALERT_NOTIFY_USER_IDS:
        try:
            await bot.send_message(user_id, text)
        except Exception:
            log.exception("Failed to notify user %s about monthly summary", user_id)


@dp.errors()
async def error_handler(event: ErrorEvent):
    log.exception("Unhandled error while processing update", exc_info=event.exception)
    update = event.update
    chat_id = None
    if update.message:
        chat_id = update.message.chat.id
    elif update.callback_query and update.callback_query.message:
        chat_id = update.callback_query.message.chat.id
    if chat_id is not None:
        try:
            await bot.send_message(chat_id, f"⚠️ Что-то пошло не так: {event.exception}")
        except Exception:
            log.exception("Failed to notify user about the error")
    return True


async def _http_sync(request: web.Request) -> web.Response:
    """POST /sync — ручной запуск синхронизации по HTTP (кнопка
    «Синхронизировать» на дашборде), альтернатива команде /sync в
    Telegram. Требует заголовок X-Sync-Secret, совпадающий с
    SYNC_API_SECRET; без настроенного секрета эндпоинт всегда 503."""
    if not SYNC_API_SECRET:
        return web.json_response({"error": "sync endpoint not configured"}, status=503)
    if request.headers.get("X-Sync-Secret") != SYNC_API_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    if not DASHBOARD_DATABASE_URL:
        return web.json_response({"error": "DASHBOARD_DATABASE_URL not set"}, status=503)
    try:
        summary = await _run_sync(datetime.now().date())
    except Exception as e:
        log.exception("HTTP-triggered sync failed")
        return web.json_response({"error": str(e)}, status=500)
    text = "🔄 Синхронизация запущена вручную с дашборда\n" + _format_sync_summary(summary)
    for user_id in SYNC_NOTIFY_USER_IDS:
        try:
            await bot.send_message(user_id, text)
        except Exception:
            log.exception("Failed to notify user %s about manual HTTP sync", user_id)
    return web.json_response(summary)


async def _run_health_server():
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", lambda _req: web.Response(text="ok"))
    app.router.add_post("/sync", _http_sync)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Health check server listening on port %s", port)


async def main():
    await _run_health_server()

    if DASHBOARD_DATABASE_URL:
        scheduler = AsyncIOScheduler()
        scheduler.add_job(_scheduled_sync, "cron", hour=SYNC_HOUR, minute=SYNC_MINUTE)
        scheduler.add_job(_scheduled_daily_norm_alert, "cron", hour=ALERT_HOUR, minute=ALERT_MINUTE)
        # Недельная -- в воскресенье вечером (итог только что закончившейся
        # недели), месячная -- в последний день месяца вечером, оба сразу
        # после дневной синхронизации (SUMMARY_HOUR/MINUTE), а не наутро.
        scheduler.add_job(_scheduled_weekly_summary, "cron", day_of_week="sun", hour=SUMMARY_HOUR, minute=SUMMARY_MINUTE)
        scheduler.add_job(_scheduled_monthly_summary, "cron", day="last", hour=SUMMARY_HOUR, minute=SUMMARY_MINUTE)
        scheduler.start()
        log.info("Daily UMAG->dashboard sync scheduled at %02d:%02d", SYNC_HOUR, SYNC_MINUTE)
        log.info("Daily norm alert scheduled at %02d:%02d", ALERT_HOUR, ALERT_MINUTE)
        log.info("Weekly (Sun)/monthly (last day) summaries scheduled at %02d:%02d", SUMMARY_HOUR, SUMMARY_MINUTE)
    else:
        log.info("DASHBOARD_DATABASE_URL not set — daily dashboard sync disabled")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
