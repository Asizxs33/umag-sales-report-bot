"""Ежедневная проверка нормы (2 отзыва + 2 отправки на 2 этаж в день,
только Iposuda -- см. DAILY_MIN_REVIEWS/REFERRALS в iposuda-kpi-web/src/lib/kpi.ts)
и еженедельная/ежемесячная сводка по продажам -- всё читает напрямую из
БД дашборда (та же DASHBOARD_DATABASE_URL, что и daily_sync.py/employees.py),
без похода в UMAG."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

import asyncpg

from config import DASHBOARD_DATABASE_URL

DAILY_MIN_REVIEWS = 2
DAILY_MIN_REFERRALS = 2


async def check_daily_norm(report_date: date) -> str | None:
    """Проверяет норму за report_date для активных сотрудников Iposuda.
    Возвращает текст сообщения со списком не дотянувших, либо None, если
    все выполнили норму (сообщение слать не нужно)."""
    if not DASHBOARD_DATABASE_URL:
        return None

    conn = await asyncpg.connect(DASHBOARD_DATABASE_URL)
    try:
        rows = await conn.fetch(
            """
            SELECT
                e.full_name,
                COALESCE(SUM(CASE WHEN l.type = 'COMPLAINT' AND l.kind = 'REVIEW' THEN COALESCE(l.count, 1) ELSE 0 END), 0) AS reviews,
                COALESCE(SUM(CASE WHEN l.type = 'REFERRAL' THEN COALESCE(l.count, 0) ELSE 0 END), 0) AS referrals
            FROM employees e
            LEFT JOIN log_entries l
                ON l.employee_id = e.id AND l.date = $1 AND l.source IS NULL
            WHERE e.active = true AND e.department = 'Iposuda'
            GROUP BY e.id, e.full_name
            ORDER BY e.full_name
            """,
            report_date.isoformat(),
        )
    finally:
        await conn.close()

    failed = [r for r in rows if r["reviews"] < DAILY_MIN_REVIEWS or r["referrals"] < DAILY_MIN_REFERRALS]
    if not failed:
        return None

    lines = [f"⚠️ Норма за {report_date.strftime('%d.%m.%Y')} (2 отзыва + 2 на 2 этаж) не выполнена:"]
    for r in failed:
        lines.append(f"• {r['full_name']}: отзывы {r['reviews']}/{DAILY_MIN_REVIEWS}, 2 этаж {r['referrals']}/{DAILY_MIN_REFERRALS}")
    return "\n".join(lines)


def _fmt_money(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " ₸"


RU_MONTHS = {
    1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь",
    7: "июль", 8: "август", 9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь",
}


async def _sales_leaderboard(conn: asyncpg.Connection, date_from: date, date_to: date) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT e.full_name, e.department,
            COALESCE(SUM(l.sales_amount), 0) AS sales
        FROM employees e
        LEFT JOIN log_entries l
            ON l.employee_id = e.id AND l.type = 'UPSELL' AND l.date BETWEEN $1 AND $2
        WHERE e.active = true AND e.department != 'Ikids'
        GROUP BY e.id, e.full_name, e.department
        ORDER BY sales DESC
        """,
        date_from.isoformat(),
        date_to.isoformat(),
    )


async def _department_actual(conn: asyncpg.Connection, department: str, date_from: date, date_to: date) -> int:
    row = await conn.fetchrow(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM store_daily_revenue WHERE department = $1 AND date BETWEEN $2 AND $3",
        department,
        date_from.isoformat(),
        date_to.isoformat(),
    )
    return int(row["total"]) if row else 0


async def _department_plan(conn: asyncpg.Connection, department: str, month_key: str) -> int:
    row = await conn.fetchrow(
        "SELECT COALESCE(amount, 0) AS amount FROM monthly_plans WHERE department = $1 AND month_key = $2",
        department,
        month_key,
    )
    return int(row["amount"]) if row else 0


def _leaderboard_lines(rows: list[asyncpg.Record]) -> list[str]:
    nonzero = [r for r in rows if r["sales"] > 0]
    if not nonzero:
        return ["Нет продаж за период."]
    top = nonzero[:3]
    bottom = [r for r in nonzero[-3:] if r not in top]
    lines = ["🏆 Топ по продажам:"]
    for i, r in enumerate(top, 1):
        lines.append(f"{i}. {r['full_name']} — {_fmt_money(r['sales'])}")
    if bottom:
        lines.append("")
        lines.append("📉 Наименьшие продажи:")
        for r in reversed(bottom):
            lines.append(f"• {r['full_name']} — {_fmt_money(r['sales'])}")
    return lines


async def weekly_summary(week_end: date) -> str | None:
    """Сводка за 7 дней, заканчивая week_end включительно (обычно вчера,
    воскресенье, если джоб идёт по понедельникам)."""
    if not DASHBOARD_DATABASE_URL:
        return None
    date_from = week_end - timedelta(days=6)

    conn = await asyncpg.connect(DASHBOARD_DATABASE_URL)
    try:
        rows = await conn.fetch(
            """
            SELECT e.full_name, e.department, COALESCE(SUM(l.sales_amount), 0) AS sales
            FROM employees e
            LEFT JOIN log_entries l ON l.employee_id = e.id AND l.type = 'UPSELL' AND l.date BETWEEN $1 AND $2
            WHERE e.active = true AND e.department != 'Ikids'
            GROUP BY e.id, e.full_name, e.department
            ORDER BY sales DESC
            """,
            date_from.isoformat(),
            week_end.isoformat(),
        )
        iposuda_actual = await _department_actual(conn, "Iposuda", date_from, week_end)
    finally:
        await conn.close()

    total = sum(r["sales"] for r in rows)
    lines = [
        f"📊 Итог недели {date_from.strftime('%d.%m')} — {week_end.strftime('%d.%m.%Y')}",
        f"Личные продажи (сумма по журналам): {_fmt_money(total)}",
        f"Выручка Iposuda по UMAG за неделю: {_fmt_money(iposuda_actual)}",
        "",
    ]
    lines.extend(_leaderboard_lines(rows))
    return "\n".join(lines)


async def monthly_summary(target_month: date) -> str | None:
    """Сводка за календарный месяц, в котором лежит target_month (обычно
    вчера, последний день предыдущего месяца, если джоб идёт 1-го числа)."""
    if not DASHBOARD_DATABASE_URL:
        return None
    month_key = target_month.strftime("%Y-%m")
    date_from = target_month.replace(day=1)
    last_day = calendar.monthrange(target_month.year, target_month.month)[1]
    date_to = target_month.replace(day=last_day)

    conn = await asyncpg.connect(DASHBOARD_DATABASE_URL)
    try:
        rows = await _sales_leaderboard(conn, date_from, date_to)
        iposuda_actual = await _department_actual(conn, "Iposuda", date_from, date_to)
        iposuda_plan = await _department_plan(conn, "Iposuda", month_key)
        ikids_actual = await _department_actual(conn, "Ikids", date_from, date_to)
        ikids_plan = await _department_plan(conn, "Ikids", month_key)
    finally:
        await conn.close()

    def pct(actual: int, plan: int) -> str:
        return f"{round(actual / plan * 100)}%" if plan > 0 else "план не задан"

    lines = [
        f"📊 Итог месяца — {RU_MONTHS[target_month.month]} {target_month.year}",
        f"Iposuda: {_fmt_money(iposuda_actual)} из {_fmt_money(iposuda_plan)} ({pct(iposuda_actual, iposuda_plan)})",
        f"Ikids: {_fmt_money(ikids_actual)} из {_fmt_money(ikids_plan)} ({pct(ikids_actual, ikids_plan)})",
        "",
    ]
    lines.extend(_leaderboard_lines(rows))
    return "\n".join(lines)
