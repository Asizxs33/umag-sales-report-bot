"""Ежедневная синхронизация продаж из UMAG в базу данных KPI-дашборда
(iposuda-kpi-web, Next.js/Prisma). Пишет напрямую в Postgres (Neon) через
asyncpg -- без HTTP, дашборду не обязательно быть публично доступным.

Каждая запись помечается source="umag-daily" и уникальна по
(employee_id, date, source, type) -- повторный прогон за тот же день
обновляет существующую запись, а не плодит дубликаты.

Сопоставление сотрудников: имя из employees.py ищем СТРОГИМ (без учёта
регистра/пробелов) совпадением по employees.full_name в БД дашборда --
роспись имён в employees.py считается каноничной и в дашборде должна быть
заведена один в один. Если сотрудника в БД дашборда нет -- запись
пропускается с предупреждением в лог, а не создаётся "на угад".
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import date, datetime

import asyncpg

from employees import EMPLOYEES
from umag_client import UmagClient, UmagError

log = logging.getLogger("daily_sync")

DASHBOARD_DATABASE_URL = os.environ.get("DASHBOARD_DATABASE_URL", "")

# После синхронизации сотрудников этой точки -- также затянуть суммарную
# выручку по точке целиком (без привязки к продавцу, месяц-к-дате) в
# указанную колонку settings. Нужно там, где на дашборде есть отдельная
# карточка "Общая выручка <отдел>", которая должна показывать факт по
# точке UMAG целиком, а не только сумму по отслеживаемым сотрудникам
# (см. Settings.ikidsOnlineActual / totalRevenueActual в схеме дашборда).
# Значение (settings_column, all_pos) -- all_pos определяет, учитывать ли
# обычные чеки с кассы-терминала в дополнение к ручным накладным. И для
# Iposuda, и для Kids используется all_pos=True -- store_total_sales()
# считает выручку по точке целиком, как в отчёте UMAG "Прибыль/убытки"
# (подтверждено live 2026-08-16 для Iposuda, совпадение с точностью
# до 0.3%), в отличие от sale_stats() по отслеживаемым сотрудникам, где
# для Iposuda намеренно остаётся только канал накладных.
STORE_TOTAL_SETTINGS_COLUMN = {
    "Iposuda": ("total_revenue_actual", True),
    "Kids": ("ikids_online_actual", True),
}


def _normalize(name: str) -> str:
    return " ".join(name.strip().lower().split())


async def sync_day(umag: UmagClient, report_date: date) -> dict:
    """Тянет продажи каждого сотрудника из UMAG за report_date и пишет их
    в log_entries базы дашборда. Возвращает сводку для уведомления/логов."""

    if not DASHBOARD_DATABASE_URL:
        raise RuntimeError("DASHBOARD_DATABASE_URL не задан -- нечего синхронизировать")

    if umag.store_id is None:
        umag.login()

    date_from = datetime(report_date.year, report_date.month, report_date.day)
    date_to = date_from.replace(hour=23, minute=59, second=59, microsecond=999000)

    conn = await asyncpg.connect(DASHBOARD_DATABASE_URL)
    try:
        rows = await conn.fetch("SELECT id, full_name FROM employees")
        by_name = {_normalize(r["full_name"]): r["id"] for r in rows}

        synced: list[str] = []
        not_in_umag: list[str] = []
        not_in_dashboard: list[str] = []

        stores_needed = sorted({emp.get("store", "Iposuda") for emp in EMPLOYEES})
        for store_name in stores_needed:
            umag.select_store(store_name)

            for emp in EMPLOYEES:
                if emp.get("store", "Iposuda") != store_name:
                    continue

                employee_id = by_name.get(_normalize(emp["name"]))
                if employee_id is None:
                    not_in_dashboard.append(emp["name"])
                    continue

                seller = umag.find_seller(emp["name"])
                if not seller:
                    not_in_umag.append(emp["name"])
                    continue

                stats = umag.sale_stats(seller["id"], date_from, date_to, all_pos=True)

                # Postgres-миграция Prisma по умолчанию создаёт нативный enum-тип
                # "LogType" -- отсюда явный каст. Если после первого прогона
                # `prisma migrate dev` на Neon окажется, что поле хранится как
                # обычный TEXT, убери `::"LogType"` из запроса ниже.
                await conn.execute(
                    """
                    INSERT INTO log_entries
                        (id, type, date, employee_id, source, sales_amount, customers_served, created_at)
                    VALUES
                        ($1, 'UPSELL'::"LogType", $2, $3, 'umag-daily', $4, $5, now())
                    ON CONFLICT (employee_id, date, source, type)
                    DO UPDATE SET
                        sales_amount = EXCLUDED.sales_amount,
                        customers_served = EXCLUDED.customers_served
                    """,
                    str(uuid.uuid4()),
                    report_date.isoformat(),
                    employee_id,
                    int(stats["saleAmount"]),
                    stats["count"],
                )
                synced.append(emp["name"])

            store_total_entry = STORE_TOTAL_SETTINGS_COLUMN.get(store_name)
            if store_total_entry:
                settings_column, total_all_pos = store_total_entry
                month_start = datetime(report_date.year, report_date.month, 1)
                total = umag.store_total_sales(month_start, date_to, all_pos=total_all_pos)
                await conn.execute(
                    f'UPDATE settings SET {settings_column} = $1 WHERE id = $2',
                    int(total["saleAmount"]),
                    "singleton",
                )

        return {
            "date": report_date.isoformat(),
            "synced": synced,
            "not_in_umag": not_in_umag,
            "not_in_dashboard": not_in_dashboard,
        }
    finally:
        await conn.close()
