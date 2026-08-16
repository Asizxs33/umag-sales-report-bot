"""Разовое дозаполнение продаж за прошедшие дни (например, за начало текущего
месяца, до того как заработала ежедневная авто-синхронизация, или если бот
не работал несколько дней).

Использование:
    DASHBOARD_DATABASE_URL=... python backfill.py 2026-08-01 2026-08-16

Тянет продажи каждого сотрудника из UMAG за каждый день диапазона (включительно)
и пишет их в базу дашборда тем же способом, что и ежедневная синхронизация
(source="umag-daily") -- безопасно перезапускать повторно, старые записи
за те же дни просто обновляются.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta

from config import UMAG_PASSWORD, UMAG_PHONE
from daily_sync import sync_day
from umag_client import UmagClient


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


async def main():
    if len(sys.argv) != 3:
        print("Использование: python backfill.py YYYY-MM-DD YYYY-MM-DD")
        sys.exit(1)

    start = _parse_date(sys.argv[1])
    end = _parse_date(sys.argv[2])
    if start > end:
        print("Начальная дата позже конечной")
        sys.exit(1)

    umag = UmagClient(UMAG_PHONE, UMAG_PASSWORD)
    umag.login()
    umag.ensure_store()

    day = start
    while day <= end:
        print(f"--- {day.isoformat()} ---")
        summary = await sync_day(umag, day)
        print(f"  синхронизировано: {len(summary['synced'])}")
        if summary["not_in_umag"]:
            print(f"  не найдено в UMAG: {', '.join(summary['not_in_umag'])}")
        if summary["not_in_dashboard"]:
            print(f"  нет в дашборде: {', '.join(summary['not_in_dashboard'])}")
        day += timedelta(days=1)

    print("Готово.")


if __name__ == "__main__":
    asyncio.run(main())
