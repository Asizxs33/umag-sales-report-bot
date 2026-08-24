"""Ежедневная синхронизация продаж из UMAG в базу данных KPI-дашборда
(iposuda-kpi-web, Next.js/Prisma). Пишет напрямую в Postgres (Neon) через
asyncpg -- без HTTP, дашборду не обязательно быть публично доступным.

Каждая запись помечается source="umag-daily" и уникальна по
(employee_id, date, source, type) -- повторный прогон за тот же день
обновляет существующую запись, а не плодит дубликаты.

Список сотрудников читается напрямую из таблицы employees БД дашборда
(активные, active=true) -- добавление/редактирование сотрудника на
странице «Сотрудники» дашборда сразу подхватывается следующим прогоном
синхронизации, без изменений кода бота. Сопоставление с продавцом в
UMAG идёт по имени через нечёткий поиск (см. UmagClient.find_seller).
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import date, datetime

import asyncpg

from employees import DEPARTMENT_TO_STORE
from umag_client import UmagClient, UmagError

log = logging.getLogger("daily_sync")

DASHBOARD_DATABASE_URL = os.environ.get("DASHBOARD_DATABASE_URL", "")

# После синхронизации сотрудников этой точки -- также затянуть суммарную
# выручку по точке целиком (без привязки к продавцу, месяц-к-дате) в
# указанную колонку settings. Нужно там, где на дашборде есть отдельная
# карточка "Общая выручка <отдел>", которая должна показывать факт по
# точке UMAG целиком, а не только сумму по отслеживаемым сотрудникам
# (см. Settings.ikidsOnlineActual / totalRevenueActual в схеме дашборда).
# Значение (settings_column, all_pos, department) -- all_pos определяет,
# учитывать ли обычные чеки с кассы-терминала в дополнение к ручным
# накладным. И для Iposuda, и для Kids используется all_pos=True --
# store_total_sales() считает выручку по точке целиком, как в отчёте UMAG
# "Прибыль/убытки" (подтверждено live 2026-08-16 для Iposuda, совпадение
# с точностью до 0.3%). department -- значение Employee.department на
# дашборде (для записи в store_daily_revenue; НЕ совпадает с названием
# точки UMAG для Kids/Ikids).
STORE_TOTAL_SETTINGS_COLUMN = {
    "Iposuda": ("total_revenue_actual", True, "Iposuda"),
    "Kids": ("ikids_online_actual", True, "Ikids"),
}


async def sync_day(umag: UmagClient, report_date: date) -> dict:
    """Тянет продажи каждого активного сотрудника дашборда из UMAG за
    report_date и пишет их в log_entries. Возвращает сводку для
    уведомления/логов."""

    if not DASHBOARD_DATABASE_URL:
        raise RuntimeError("DASHBOARD_DATABASE_URL не задан -- нечего синхронизировать")

    if umag.store_id is None:
        umag.login()

    date_from = datetime(report_date.year, report_date.month, report_date.day)
    date_to = date_from.replace(hour=23, minute=59, second=59, microsecond=999000)

    conn = await asyncpg.connect(DASHBOARD_DATABASE_URL)
    try:
        rows = await conn.fetch("SELECT id, full_name, department FROM employees WHERE active = true")
        employees = [
            {
                "id": r["id"],
                "name": r["full_name"],
                "store": DEPARTMENT_TO_STORE.get(r["department"], "Iposuda"),
            }
            for r in rows
        ]

        synced: list[str] = []
        not_in_umag: list[str] = []

        stores_needed = sorted({emp["store"] for emp in employees})
        for store_name in stores_needed:
            umag.select_store(store_name)

            for emp in employees:
                if emp["store"] != store_name:
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
                    emp["id"],
                    int(stats["saleAmount"]),
                    stats["count"],
                )
                synced.append(emp["name"])

            store_total_entry = STORE_TOTAL_SETTINGS_COLUMN.get(store_name)
            if store_total_entry:
                settings_column, total_all_pos, department = store_total_entry

                month_start = datetime(report_date.year, report_date.month, 1)
                month_total = umag.store_total_sales(month_start, date_to, all_pos=total_all_pos)
                await conn.execute(
                    f'UPDATE settings SET {settings_column} = $1 WHERE id = $2',
                    int(month_total["saleAmount"]),
                    "singleton",
                )

                # Тот же расчёт, но только за report_date -- пишется в
                # store_daily_revenue, чтобы "Общая выручка" на дашборде
                # могла показывать точный факт за произвольный диапазон
                # дат, а не только "месяц-к-дате" (см. Settings выше).
                # debtAmount -- сумма неоплаченных продаж за этот день,
                # позволяет дашборду показать "чистую" выручку (без долга).
                day_total = umag.store_total_sales(date_from, date_to, all_pos=total_all_pos)
                await conn.execute(
                    """
                    INSERT INTO store_daily_revenue (id, department, date, amount, debt_amount, updated_at)
                    VALUES ($1, $2, $3, $4, $5, now())
                    ON CONFLICT (department, date)
                    DO UPDATE SET amount = EXCLUDED.amount, debt_amount = EXCLUDED.debt_amount, updated_at = now()
                    """,
                    str(uuid.uuid4()),
                    department,
                    report_date.isoformat(),
                    int(day_total["saleAmount"]),
                    int(day_total["debtAmount"]),
                )

        return {
            "date": report_date.isoformat(),
            "synced": synced,
            "not_in_umag": not_in_umag,
            "not_in_dashboard": [],
        }
    finally:
        await conn.close()
