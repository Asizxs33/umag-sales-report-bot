"""Разовая загрузка исторических данных (например, за прошлый год) для
сравнения «год к году» на дашборде.

Гоняет ту же полную синхронизацию, что кнопка на дашборде и ночной прогон
(см. full_sync.full_sync_day), по дням диапазона: выручка точки как в UMAG
(продажи минус возвраты), личные продажи активных сотрудников, строки долгов.
Платежи и снимки кассового сайта за прошлые дни не пишутся (with_cash=False),
чтобы не раздувать базу; добавьте --cash, если нужны и они.

Использование:
    DASHBOARD_DATABASE_URL=... python -u backfill_history.py 2025-01-01 2025-12-31 [--cash]
Безопасно перезапускать: записи за те же дни обновляются.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta

import httpx

from config import DASHBOARD_DATABASE_URL, UMAG_PASSWORD, UMAG_PHONE
from full_sync import full_sync_day
from umag_client import UmagClient, UmagError


async def _sync_with_retry(umag: UmagClient, day: date, with_cash: bool, attempts: int = 3) -> dict:
    for attempt in range(1, attempts + 1):
        try:
            return await full_sync_day(umag, day, with_cash=with_cash)
        except (httpx.TimeoutException, httpx.TransportError, UmagError):
            if attempt == attempts:
                raise
    raise RuntimeError("unreachable")


async def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    with_cash = "--cash" in sys.argv
    if len(args) != 2:
        print("Использование: python backfill_history.py YYYY-MM-DD YYYY-MM-DD [--cash]")
        sys.exit(1)
    start, end = date.fromisoformat(args[0]), date.fromisoformat(args[1])
    if start > end:
        print("Начальная дата позже конечной")
        sys.exit(1)
    if not DASHBOARD_DATABASE_URL:
        print("DASHBOARD_DATABASE_URL не задан")
        sys.exit(1)

    umag = UmagClient(UMAG_PHONE, UMAG_PASSWORD)
    umag.login()

    failed: list[str] = []
    day = start
    while day <= end:
        try:
            summary = await _sync_with_retry(umag, day, with_cash)
            print(f"{day.isoformat()}  " + "  ".join(f"{k}={v}" for k, v in summary["revenue"].items()), flush=True)
        except Exception as exc:  # noqa: BLE001 -- не валим весь прогон из-за одного дня
            failed.append(f"{day.isoformat()}: {exc!r}")
            print(f"{day.isoformat()}  ОШИБКА", flush=True)
        day += timedelta(days=1)

    print("Готово.", flush=True)
    if failed:
        print(f"Не удалось ({len(failed)}):", flush=True)
        for line in failed:
            print("  " + line, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
