"""Полная синхронизация одного дня из UMAG: одним проходом по каждой точке.

В базу KPI-дашборда (схема public):
  * log_entries (UPSELL, source="umag-daily") -- личные продажи активных
    сотрудников, долговые накладные входят полной суммой;
  * store_daily_revenue -- выручка точки как в отчёте UMAG «Прибыль/убытки»
    (продажи минус возвраты) и сумма долгов;
  * store_daily_debt -- строки долгов: у продавцов из AGGREGATED_DEBT_SELLERS
    одной суммой («Сардор К»), остальные по контрагентам.

В базу кассового сайта (схема cash, если with_cash):
  * payments -- платежи за день (отметки проверяющего не трогаются);
  * cash_days -- снимок итогов дня (остатки, текст WhatsApp не трогаются).

Повторный прогон за тот же день безопасен: записи обновляются, не дублируются.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone

import asyncpg

from employees import DEPARTMENT_TO_STORE
from umag_client import UmagClient

log = logging.getLogger("full_sync")

DASHBOARD_DATABASE_URL = os.environ.get("DASHBOARD_DATABASE_URL", "")

# Казахстан -- UTC+5 без летнего времени. Границы дня задаём явно, чтобы
# результат не зависел от часового пояса сервера (на Render он UTC).
ALMATY = timezone(timedelta(hours=5))

# Точка UMAG -> отдел на дашборде (для store_daily_revenue / store_daily_debt)
DEPARTMENT_OF_STORE = {"Iposuda": "Iposuda", "Kids": "Ikids"}
# Точка UMAG -> ключ магазина на кассовом сайте
CASH_STORE_KEY = {"Iposuda": "iposuda", "Kids": "ikids"}


def day_bounds(report_date: date) -> tuple[datetime, datetime]:
    start = datetime(report_date.year, report_date.month, report_date.day, tzinfo=ALMATY)
    return start, start.replace(hour=23, minute=59, second=59, microsecond=999000)


def _utc_naive(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).replace(tzinfo=None)


async def _write_cash(conn: asyncpg.Connection, store_name: str, report_date: date, snapshot: dict, payments: list[dict]) -> None:
    store = CASH_STORE_KEY[store_name]
    day = report_date.isoformat()
    for p in payments:
        await conn.execute(
            """
            INSERT INTO cash.payments
                (id, store, umag_id, date, "time", type, amount, account, purpose, note, user_name, removed, verdict)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, false, 'UNCHECKED'::cash."Verdict")
            ON CONFLICT (store, umag_id) DO UPDATE SET
                date = EXCLUDED.date, "time" = EXCLUDED."time", type = EXCLUDED.type, amount = EXCLUDED.amount,
                account = EXCLUDED.account, purpose = EXCLUDED.purpose, note = EXCLUDED.note,
                user_name = EXCLUDED.user_name, removed = false
            """,
            str(uuid.uuid4()), store, p["id"], day, _utc_naive(p["time_ms"]), p["type"], p["amount"],
            p["account"], p["purpose"], p["note"], p["user"],
        )
    await conn.execute(
        "UPDATE cash.payments SET removed = true WHERE store = $1 AND date = $2 AND umag_id <> ALL($3::int[])",
        store, day, [p["id"] for p in payments],
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    await conn.execute(
        """
        INSERT INTO cash.cash_days (id, store, date, "umagSnapshot", "fetchedAt", updated_at)
        VALUES ($1, $2, $3, $4::jsonb, $5, $5)
        ON CONFLICT (date, store) DO UPDATE SET
            "umagSnapshot" = EXCLUDED."umagSnapshot", "fetchedAt" = EXCLUDED."fetchedAt", updated_at = EXCLUDED.updated_at
        """,
        str(uuid.uuid4()), store, day, json.dumps(snapshot, ensure_ascii=False), now,
    )


async def full_sync_day(umag: UmagClient, report_date: date, *, with_cash: bool = True) -> dict:
    """Синхронизирует один день по всем точкам. Возвращает сводку для
    уведомления/логов: {date, synced, not_in_umag, not_in_dashboard, revenue, cash}."""
    if not DASHBOARD_DATABASE_URL:
        raise RuntimeError("DASHBOARD_DATABASE_URL не задан -- нечего синхронизировать")
    if umag.store_id is None:
        umag.login()

    date_from, date_to = day_bounds(report_date)
    day = report_date.isoformat()

    conn = await asyncpg.connect(DASHBOARD_DATABASE_URL)
    try:
        rows = await conn.fetch("SELECT id, full_name, department FROM employees WHERE active = true")
        employees = [
            {"id": r["id"], "name": r["full_name"], "store": DEPARTMENT_TO_STORE.get(r["department"], "Iposuda")}
            for r in rows
        ]

        synced: list[str] = []
        not_in_umag: list[str] = []
        revenue: dict[str, int] = {}
        cash_error: str | None = None

        # Точки: все, где есть сотрудники, плюс обе точки с общей выручкой
        stores = sorted({e["store"] for e in employees} | set(DEPARTMENT_OF_STORE))
        for store_name in stores:
            umag.select_store(store_name)
            full = umag.day_full(date_from, date_to)
            snapshot = full["snapshot"]

            for emp in employees:
                if emp["store"] != store_name:
                    continue
                seller = umag.find_seller(emp["name"])
                if not seller:
                    not_in_umag.append(emp["name"])
                    continue
                stats = full["by_seller"].get(seller["id"], {"count": 0, "amount": 0.0})
                # Явный каст "LogType": Prisma создаёт нативный enum-тип.
                await conn.execute(
                    """
                    INSERT INTO log_entries
                        (id, type, date, employee_id, source, sales_amount, customers_served, created_at)
                    VALUES ($1, 'UPSELL'::"LogType", $2, $3, 'umag-daily', $4, $5, now())
                    ON CONFLICT (employee_id, date, source, type)
                    DO UPDATE SET sales_amount = EXCLUDED.sales_amount, customers_served = EXCLUDED.customers_served
                    """,
                    str(uuid.uuid4()), day, emp["id"], int(stats["amount"]), stats["count"],
                )
                synced.append(emp["name"])

            department = DEPARTMENT_OF_STORE.get(store_name)
            if department:
                sales = snapshot["sales"]
                revenue[department] = sales["revenue"]
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO store_daily_revenue (id, department, date, amount, debt_amount, updated_at)
                        VALUES ($1, $2, $3, $4, $5, now())
                        ON CONFLICT (department, date)
                        DO UPDATE SET amount = EXCLUDED.amount, debt_amount = EXCLUDED.debt_amount, updated_at = now()
                        """,
                        str(uuid.uuid4()), department, day, sales["revenue"], sales["debt"],
                    )
                    await conn.execute("DELETE FROM store_daily_debt WHERE department = $1 AND date = $2", department, day)
                    for d in snapshot["debtors"]:
                        await conn.execute(
                            """
                            INSERT INTO store_daily_debt (id, department, date, name, amount, updated_at)
                            VALUES ($1, $2, $3, $4, $5, now())
                            ON CONFLICT (department, date, name) DO UPDATE SET amount = EXCLUDED.amount, updated_at = now()
                            """,
                            str(uuid.uuid4()), department, day, d["name"], d["amount"],
                        )

            if with_cash and store_name in CASH_STORE_KEY:
                try:
                    payments = umag.payments(date_from, date_to)
                    async with conn.transaction():
                        await _write_cash(conn, store_name, report_date, snapshot, payments)
                except Exception as exc:  # кассовый сайт не должен ломать синхронизацию KPI
                    log.exception("Cash report sync failed for %s %s", store_name, day)
                    cash_error = f"{type(exc).__name__}: {exc}"

        return {
            "date": day,
            "synced": synced,
            "not_in_umag": not_in_umag,
            "not_in_dashboard": [],
            "revenue": revenue,
            "cash_error": cash_error,
        }
    finally:
        await conn.close()
