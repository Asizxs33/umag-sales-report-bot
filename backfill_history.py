"""Разовая загрузка исторических данных (например, за прошлый год) для
сравнения «год к году» на дашборде.

Отличие от backfill.py: быстрее (один проход по продавцам за день, см.
UmagClient.period_breakdown) и не трогает settings -- пишет только
store_daily_revenue (выручка и долг точки целиком) и log_entries UPSELL
для СЕЙЧАС активных сотрудников дашборда, найденных среди продавцов UMAG
по имени. Бывшие продавцы, которых нет в списке сотрудников, попадают
только в выручку точки.

Использование:
    DASHBOARD_DATABASE_URL=... python -u backfill_history.py 2025-01-01 2025-12-31
Безопасно перезапускать: записи за те же дни обновляются.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import date, datetime, timedelta

import asyncpg
import httpx

from config import DASHBOARD_DATABASE_URL, UMAG_PASSWORD, UMAG_PHONE
from daily_sync import STORE_TOTAL_SETTINGS_COLUMN
from employees import DEPARTMENT_TO_STORE
from umag_client import UmagClient, UmagError


def _breakdown_with_retry(umag: UmagClient, date_from: datetime, date_to: datetime, attempts: int = 4) -> dict:
    for attempt in range(1, attempts + 1):
        try:
            return umag.period_breakdown(date_from, date_to)
        except (httpx.TimeoutException, httpx.TransportError, UmagError):
            if attempt == attempts:
                raise
    raise RuntimeError("unreachable")


async def main():
    if len(sys.argv) != 3:
        print("Использование: python backfill_history.py YYYY-MM-DD YYYY-MM-DD")
        sys.exit(1)
    start = date.fromisoformat(sys.argv[1])
    end = date.fromisoformat(sys.argv[2])
    if start > end:
        print("Начальная дата позже конечной")
        sys.exit(1)
    if not DASHBOARD_DATABASE_URL:
        print("DASHBOARD_DATABASE_URL не задан")
        sys.exit(1)

    umag = UmagClient(UMAG_PHONE, UMAG_PASSWORD)
    umag.login()

    conn = await asyncpg.connect(DASHBOARD_DATABASE_URL)
    try:
        rows = await conn.fetch("SELECT id, full_name, department FROM employees WHERE active = true")
        employees = [
            {"id": r["id"], "name": r["full_name"], "store": DEPARTMENT_TO_STORE.get(r["department"], "Iposuda")}
            for r in rows
        ]
        stores = sorted({e["store"] for e in employees} | set(STORE_TOTAL_SETTINGS_COLUMN))

        # {store: {customFieldItemId: employee_id}} -- сопоставление один раз
        # на точку, а не на каждый день.
        seller_to_employee: dict[str, dict[int, str]] = {}
        for store in stores:
            umag.select_store(store)
            mapping: dict[int, str] = {}
            for emp in employees:
                if emp["store"] != store:
                    continue
                seller = umag.find_seller(emp["name"])
                if seller:
                    mapping[seller["id"]] = emp["id"]
                else:
                    print(f"  [{store}] нет в UMAG: {emp['name']}", flush=True)
            seller_to_employee[store] = mapping

        failed: list[str] = []
        day = start
        while day <= end:
            date_from = datetime(day.year, day.month, day.day)
            date_to = date_from.replace(hour=23, minute=59, second=59, microsecond=999000)
            parts = []
            for store in stores:
                umag.select_store(store)
                try:
                    result = _breakdown_with_retry(umag, date_from, date_to)
                except Exception as exc:  # noqa: BLE001 -- не валим весь прогон из-за одного дня
                    failed.append(f"{day.isoformat()} {store}: {exc!r}")
                    parts.append(f"{store}=ОШИБКА")
                    continue

                department = STORE_TOTAL_SETTINGS_COLUMN.get(store, (None, None, None))[2]
                if department:
                    await conn.execute(
                        """
                        INSERT INTO store_daily_revenue (id, department, date, amount, debt_amount, updated_at)
                        VALUES ($1, $2, $3, $4, $5, now())
                        ON CONFLICT (department, date)
                        DO UPDATE SET amount = EXCLUDED.amount, debt_amount = EXCLUDED.debt_amount, updated_at = now()
                        """,
                        str(uuid.uuid4()), department, day.isoformat(), int(result["amount"]), int(result["debt"]),
                    )

                for seller_id, employee_id in seller_to_employee[store].items():
                    stats = result["by_seller"].get(seller_id)
                    if not stats:
                        continue
                    await conn.execute(
                        """
                        INSERT INTO log_entries
                            (id, type, date, employee_id, source, sales_amount, customers_served, created_at)
                        VALUES
                            ($1, 'UPSELL'::"LogType", $2, $3, 'umag-daily', $4, $5, now())
                        ON CONFLICT (employee_id, date, source, type)
                        DO UPDATE SET sales_amount = EXCLUDED.sales_amount, customers_served = EXCLUDED.customers_served
                        """,
                        str(uuid.uuid4()), day.isoformat(), employee_id, int(stats["amount"]), stats["count"],
                    )
                parts.append(f"{store}={int(result['amount'])}")
            print(f"{day.isoformat()}  " + "  ".join(parts), flush=True)
            day += timedelta(days=1)

        print("Готово.", flush=True)
        if failed:
            print(f"Не удалось ({len(failed)}):", flush=True)
            for line in failed:
                print("  " + line, flush=True)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
