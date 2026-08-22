"""Sales team roster -- sourced live from the KPI dashboard's own Postgres
database (single source of truth), not hardcoded here. Add/edit employees
on the dashboard's "Сотрудники" page and the bot picks them up on the next
sync/report automatically -- no code change or redeploy needed.
"""

from __future__ import annotations

from datetime import date

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


def to_daily_plan(monthly_plan: int) -> int:
    """Дашборд хранит план помесячно (см. MonthlyPlan в iposuda-kpi-web);
    дневной отчёт /report сравнивает с дневным планом -- тот же пересчёт
    (/26 рабочих дней), который раньше делался вручную для каждого
    сотрудника в этом файле."""
    return round(monthly_plan / 26) if monthly_plan else 0


async def load_employees() -> list[dict]:
    """Загружает активных сотрудников напрямую из БД дашборда: [{name,
    position, plan, store}]. Открывает собственное соединение -- подходит
    для мест без уже открытого (например, /report); daily_sync.py делает
    аналогичный запрос через уже открытое соединение, чтобы не плодить
    лишние подключения.

    План продаж теперь помесячный (таблица monthly_plans, привязан к
    employee_id + текущему месяцу) -- раньше был статичной колонкой
    employees.sales_target, которую убрали при переходе на помесячные
    планы."""
    if not DASHBOARD_DATABASE_URL:
        raise RuntimeError("DASHBOARD_DATABASE_URL не задан -- список сотрудников недоступен")
    month_key = date.today().strftime("%Y-%m")
    conn = await asyncpg.connect(DASHBOARD_DATABASE_URL)
    try:
        rows = await conn.fetch(
            """
            SELECT e.full_name, e.position, e.department, COALESCE(mp.amount, 0) AS monthly_plan
            FROM employees e
            LEFT JOIN monthly_plans mp ON mp.employee_id = e.id AND mp.month_key = $1
            WHERE e.active = true
            ORDER BY e.created_at
            """,
            month_key,
        )
    finally:
        await conn.close()
    return [
        {
            "name": r["full_name"],
            "position": r["position"] or "Продавец",
            "plan": to_daily_plan(r["monthly_plan"]),
            "store": DEPARTMENT_TO_STORE.get(r["department"], "Iposuda"),
        }
        for r in rows
    ]
