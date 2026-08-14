import asyncio
import logging
import os
import tempfile
from datetime import datetime, timedelta

from aiohttp import web

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

from config import ALLOWED_TELEGRAM_USER_IDS, TELEGRAM_BOT_TOKEN, UMAG_PASSWORD, UMAG_PHONE
from employees import EMPLOYEES
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
        umag.ensure_store()


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

    date_from = datetime(report_date.year, report_date.month, report_date.day)
    date_to = date_from.replace(hour=23, minute=59, second=59, microsecond=999000)

    rows = []
    not_found = []
    for emp in EMPLOYEES:
        seller = umag.find_seller(emp["name"])
        if not seller:
            not_found.append(emp["name"])
            stats = {"count": 0, "saleAmount": 0}
        else:
            stats = umag.sale_stats(seller["id"], date_from, date_to)
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


async def _run_health_server():
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", lambda _req: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Health check server listening on port %s", port)


async def main():
    await _run_health_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
