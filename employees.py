"""Sales team roster -- sourced live from the KPI dashboard's own Postgres
database (single source of truth), not hardcoded here. Add/edit employees
on the dashboard's "Сотрудники" page and the bot picks them up on the next
sync/report automatically -- no code change or redeploy needed.
"""

from __future__ import annotations

import asyncpg

from config import DASHBOARD_DATABASE_URL

# Отдел (department) на дашборде -> торговая точка UMAG, откуда тянутся
# его продажи. Колл-центр продаёт через тот же UMAG-магазин, что и
# Iposuda (это не отдельная физическая точка).
DEPARTMENT_TO_STORE = {
    "Iposuda": "Iposuda",
    "Колл-центр": "Iposuda",
    "Ikids": "Kids",
}


def to_daily_plan(sales_target: int) -> int:
    """Дашборд хранит план как месячный; дневной отчёт /report сравнивает
    с дневным планом -- тот же пересчёт (/26 рабочих дней), который раньше
    делался вручную для каждого сотрудника в этом файле."""
    return round(sales_target / 26) if sales_target else 0


async def load_employees() -> list[dict]:
    """Загружает активных сотрудников напрямую из БД дашборда: [{name,
    position, plan, store}]. Открывает собственное соединение -- подходит
    для мест без уже открытого (например, /report); daily_sync.py делает
    аналогичный запрос через уже открытое соединение, чтобы не плодить
    лишние подключения."""
    if not DASHBOARD_DATABASE_URL:
        raise RuntimeError("DASHBOARD_DATABASE_URL не задан -- список сотрудников недоступен")
    conn = await asyncpg.connect(DASHBOARD_DATABASE_URL)
    try:
        rows = await conn.fetch(
            "SELECT full_name, position, department, sales_target FROM employees WHERE active = true ORDER BY created_at"
        )
    finally:
        await conn.close()
    return [
        {
            "name": r["full_name"],
            "position": r["position"] or "Продавец",
            "plan": to_daily_plan(r["sales_target"]),
            "store": DEPARTMENT_TO_STORE.get(r["department"], "Iposuda"),
        }
        for r in rows
    ]
