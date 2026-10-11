"""Load one employee's UMAG history, without changing other employees or store totals.

python backfill_employee.py EMPLOYEE_ID YYYY-MM-DD YYYY-MM-DD [--apply]
Without --apply, only reads and reports UMAG data. Uses business dates (UTC+5).
"""
import argparse
import asyncio
import calendar
import json
import uuid
from datetime import date, datetime, time, timedelta, timezone

import asyncpg
import httpx

from config import DASHBOARD_DATABASE_URL, UMAG_PHONE, UMAG_PASSWORD
from employees import DEPARTMENT_TO_STORE
from umag_client import UmagClient, UmagError

BUSINESS_TZ = timezone(timedelta(hours=5))


def read_month(client, seller_id, start, end):
    params = {
        "first": 0, "pageSize": 500, "storeId": client.store_id,
        "customFieldItemId": seller_id,
        "fromTime": int(datetime.combine(start, time.min, BUSINESS_TZ).timestamp() * 1000),
        "toTime": int(datetime.combine(end, time(23, 59, 59, 999000), BUSINESS_TZ).timestamp() * 1000),
    }
    sales, debts = {}, {}
    while True:
        for attempt in range(3):
            try:
                data = client._get("/opr/sale/list-without-products", params=params)
                break
            except (httpx.TransportError, UmagError):
                if attempt == 2:
                    raise
        page = data.get("sales", [])
        for sale in page:
            sales[sale["id"]] = sale
        for debt in data.get("debts", []):
            debts[debt["saleId"]] = debt["amount"]
        if len(sales) >= data.get("count", 0):
            break
        if not page:
            raise RuntimeError("Incomplete UMAG response; refusing to overwrite history")
        params["first"] += len(page)
    return group_days(sales.values(), debts, start, end)


def group_days(sales, debts, start, end):
    days = {(start + timedelta(days=n)).isoformat(): {"amount": 0, "count": 0, "debt": 0}
            for n in range((end - start).days + 1)}
    for sale in sales:
        day = datetime.fromtimestamp(sale["time"] / 1000, BUSINESS_TZ).date().isoformat()
        if day not in days:
            raise RuntimeError("UMAG returned a sale outside the requested business dates")
        days[day]["amount"] += sale["amount"]
        days[day]["count"] += 1
        days[day]["debt"] += debts.get(sale["id"], 0)
    return days


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("employee_id")
    parser.add_argument("start", type=date.fromisoformat)
    parser.add_argument("end", type=date.fromisoformat)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.start > args.end or args.end > datetime.now(BUSINESS_TZ).date():
        parser.error("Invalid date range")
    if not DASHBOARD_DATABASE_URL:
        parser.error("DASHBOARD_DATABASE_URL is required")
    conn = await asyncpg.connect(DASHBOARD_DATABASE_URL)
    client = UmagClient(UMAG_PHONE, UMAG_PASSWORD)
    try:
        employee = await conn.fetchrow("SELECT id, full_name, department FROM employees WHERE id=$1", args.employee_id)
        if not employee:
            raise RuntimeError("Employee not found")
        store = DEPARTMENT_TO_STORE[employee["department"]]
        client.login()
        client.select_store(store)
        seller = client.find_seller(employee["full_name"])
        if not seller or client._normalize_name(seller["name"]) != client._normalize_name(employee["full_name"]):
            raise RuntimeError("Exact seller name match required for historical import")
        debt_department = "Ikids" if store == "Kids" else "Iposuda"
        total_amount, total_count = 0, 0
        start = args.start
        while start <= args.end:
            end = min(args.end, date(start.year, start.month, calendar.monthrange(start.year, start.month)[1]))
            days = read_month(client, seller["id"], start, end)
            amount = sum(row["amount"] for row in days.values())
            count = sum(row["count"] for row in days.values())
            if args.apply:
                async with conn.transaction():
                    await conn.executemany('''
                        INSERT INTO log_entries (id,type,date,employee_id,source,sales_amount,customers_served,created_at)
                        VALUES ($1,'UPSELL'::"LogType",$2,$3,'umag-daily',$4,$5,now())
                        ON CONFLICT (employee_id,date,source,type)
                        DO UPDATE SET sales_amount=EXCLUDED.sales_amount,customers_served=EXCLUDED.customers_served
                    ''', [(str(uuid.uuid4()),day,args.employee_id,int(row["amount"]),row["count"]) for day,row in days.items()])
                    await conn.executemany('''
                        INSERT INTO employee_daily_debt (id,employee_id,department,date,amount,updated_at)
                        VALUES ($1,$2,$3,$4,$5,now())
                        ON CONFLICT (employee_id,date)
                        DO UPDATE SET amount=EXCLUDED.amount,department=EXCLUDED.department,updated_at=now()
                    ''', [(str(uuid.uuid4()),args.employee_id,debt_department,day,int(row["debt"])) for day,row in days.items()])
            total_amount += amount
            total_count += count
            print(json.dumps({"from":str(start),"to":str(end),"days":len(days),"sales":amount,"invoices":count,"applied":args.apply}),flush=True)
            start = end + timedelta(days=1)
        print(json.dumps({"complete":True,"sales":total_amount,"invoices":total_count}),flush=True)
    finally:
        client._http.close()
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
