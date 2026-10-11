"""Ежедневная синхронизация из UMAG в базу данных KPI-дашборда
(iposuda-kpi-web, Next.js/Prisma). Пишет напрямую в Postgres (Neon) через
asyncpg -- без HTTP, дашборду не обязательно быть публично доступным.

Вся работа делается в full_sync.full_sync_day: личные продажи сотрудников
(долговые накладные входят полной суммой), выручка точки как в UMAG
«Прибыль/убытки» (продажи минус возвраты), строки долгов и -- для кассового
сайта -- платежи и снимок дня. Здесь остаётся тонкая обёртка, чтобы
bot.py, backfill.py и команда /sync продолжали вызывать sync_day().

Список сотрудников читается напрямую из таблицы employees БД дашборда
(активные, active=true) -- добавление/редактирование сотрудника на
странице «Сотрудники» дашборда сразу подхватывается следующим прогоном
синхронизации. Сопоставление с продавцом в UMAG идёт по имени через
нечёткий поиск (см. UmagClient.find_seller).
"""

from __future__ import annotations

from datetime import date

from full_sync import full_sync_day
from umag_client import UmagClient

# Оставлено для обратной совместимости (раньше сюда писалась сумма месяца
# в settings, дашборд её больше не использует): точка UMAG -> (колонка, all_pos, отдел).
STORE_TOTAL_SETTINGS_COLUMN = {
    "Iposuda": ("total_revenue_actual", True, "Iposuda"),
    "Kids": ("ikids_online_actual", True, "Ikids"),
}


async def sync_day(umag: UmagClient, report_date: date) -> dict:
    """Синхронизирует report_date целиком. Возвращает сводку для
    уведомления/логов."""
    return await full_sync_day(umag, report_date)
