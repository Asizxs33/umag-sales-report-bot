"""Thin client around the (undocumented) UMAG REST API -- sales-stats subset.

Auth confirmed live 2026-07-12 against a different UMAG account, and the
"Продавцы" (sellers) custom-field + per-seller sale stats confirmed live
2026-08-14 against this account (storeId=42219, field id=20244).
"""

from __future__ import annotations

import base64
import difflib
import math
import time
from datetime import datetime, timedelta
from typing import Any

import httpx

BASE_URL = "https://api.umag.kz/rest/cabinet"
CLIENT_VER = "angular_cabinet_20.0.10"
API_VER = "1.4"

SELLER_FIELD_NAME = "Продавцы"

# Продавцы, чьи долги в отчёте сворачиваются в одну строку («Сардор К: сумма»)
# вместо списка контрагентов. Тот же список, что AGGREGATED_DEBT_SELLERS в
# сайте кассового отчёта (iposuda-cash-report/src/lib/stores.ts).
AGGREGATED_DEBT_SELLERS = [(("камилов", "сардор"), "Сардор К")]


def round_half_up(x: float) -> int:
    """Как Math.round в JS (218528.5 -> 218529), а не банковское округление."""
    return int(math.floor(x + 0.5))


class UmagError(RuntimeError):
    pass


class UmagClient:
    def __init__(self, phone: str, password: str):
        self._phone = phone
        self._password = password
        self._session_token: str | None = None
        self.store_id: int | None = None
        self._seller_field_id: int | None = None
        self._manual_pos_id: int | None = None
        self._http = httpx.Client(base_url=BASE_URL, timeout=40.0)
        self._sellers_cache: dict[int, tuple[float, list[dict]]] = {}

    # ---------------------------------------------------------------- auth
    def login(self) -> None:
        basic = base64.b64encode(f"{self._phone}:{self._password}".encode()).decode()
        resp = self._http.get("/org/login/signin", headers=self._headers(auth=f"Basic {basic}"))
        if resp.status_code != 200:
            raise UmagError(f"Login failed: {resp.status_code} {resp.text}")
        self._session_token = resp.json()["sessionToken"]

    def list_stores(self) -> list[dict]:
        stores = self._get("/org/store/list")
        return stores if isinstance(stores, list) else stores.get("data", stores)

    def ensure_store(self) -> None:
        stores = self.list_stores()
        if not stores:
            raise UmagError("No stores available on this account")
        self.store_id = stores[0]["id"]

    def select_store(self, name: str) -> None:
        """Switches the active store by (partial, case-insensitive) name --
        this account has multiple retail points (e.g. "Iposuda", "Kids",
        "Dubai Gold"), each with its own custom-field / manual-POS ids, so
        those per-store caches are reset on switch."""
        stores = self.list_stores()
        match = next((s for s in stores if name.lower() in s["name"].lower()), None)
        if not match:
            names = ", ".join(s["name"] for s in stores)
            raise UmagError(f'Store "{name}" not found among: {names}')
        self.store_id = match["id"]
        self._seller_field_id = None
        self._manual_pos_id = None

    def _headers(self, auth: str | None = None) -> dict[str, str]:
        h = {"client-ver": CLIENT_VER, "api-ver": API_VER, "Content-Type": "application/json"}
        if auth:
            h["Authorization"] = auth
        elif self._session_token:
            h["Authorization"] = self._session_token
        return h

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._http.get(path, params=params, headers=self._headers())
        if resp.status_code == 401 and self._session_token is not None:
            self.login()
            resp = self._http.get(path, params=params, headers=self._headers())
        if resp.status_code >= 400:
            raise UmagError(f"GET {path} -> {resp.status_code}: {resp.text}")
        return resp.json() if resp.content else None

    # ------------------------------------------------------------- sellers
    def _find_seller_field_id(self) -> int:
        if self._seller_field_id is not None:
            return self._seller_field_id
        data = self._get("/org/custom-field/list", params={"className": "Sale", "storeId": self.store_id})
        for field in data.get("customFields", []):
            if field["name"] == SELLER_FIELD_NAME:
                self._seller_field_id = field["id"]
                return field["id"]
        raise UmagError(f'Custom field "{SELLER_FIELD_NAME}" not found for Sale')

    def list_sellers(self) -> list[dict]:
        """[{id, name, deleted}] -- the "Продавцы" dropdown options."""
        cached = self._sellers_cache.get(self.store_id)
        if cached and time.time() - cached[0] < 600:
            return cached[1]
        field_id = self._find_seller_field_id()
        data = self._get(f"/org/custom-field/get/{field_id}", params={"storeId": self.store_id})
        self._sellers_cache[self.store_id] = (time.time(), data["customFieldItems"])
        return data["customFieldItems"]

    @staticmethod
    def _normalize_name(name: str) -> str:
        return " ".join(sorted(name.lower().split()))

    def find_seller(self, query: str) -> dict | None:
        """Fuzzy-matches a roster name to a live UMAG seller item, tolerating
        reversed word order (Иванов Пётр / Пётр Иванов) and small typos.
        Prefers non-deleted items."""
        sellers = self.list_sellers()
        target = self._normalize_name(query)

        def score(s: dict) -> float:
            return difflib.SequenceMatcher(None, target, self._normalize_name(s["name"])).ratio()

        active = [s for s in sellers if not s.get("deleted")]
        for pool in (active, sellers):
            if not pool:
                continue
            best = max(pool, key=score)
            if score(best) >= 0.6:
                return best
        return None

    def _find_manual_pos_id(self) -> int:
        """UMAG's "Продажи" (накладные) list is scoped to one specific POS:
        a virtual, non-terminal POS used for manually-entered invoice sales
        (named "Кабинет касса" in this account) -- as opposed to the
        physical retail registers (Касса-1, Касса-2...). Confirmed live
        2026-08-14: /org/pos/list only lists physical terminals, so the
        virtual one has to be found via a sale listing's embedded `poses`.
        """
        if self._manual_pos_id is not None:
            return self._manual_pos_id
        now = datetime.now()
        data = self._get(
            "/opr/sale/list-without-products",
            params={
                "first": 0,
                "pageSize": 500,
                "fromTime": int((now - timedelta(days=60)).timestamp() * 1000),
                "toTime": int(now.timestamp() * 1000),
                "storeId": self.store_id,
            },
        )
        for pos in data.get("poses", []):
            if pos.get("virtual"):
                self._manual_pos_id = pos["id"]
                return pos["id"]
        raise UmagError("No virtual (manual-entry) POS found for this store")

    def _list_sales(
        self,
        date_from: datetime,
        date_to: datetime,
        seller_id: int | None = None,
        all_pos: bool = False,
        exclude_unpaid: bool = True,
    ) -> dict:
        """Shared paging/debt-filtering logic behind sale_stats() and
        store_total_sales() -- see those for the all_pos/exclude_unpaid
        semantics."""
        pos_id = None if all_pos else self._find_manual_pos_id()
        page_size = 500
        first = 0
        all_sales: list[dict] = []
        # saleId -> оставшаяся неоплаченная сумма ("Осталось", может быть
        # меньше полной суммы продажи при частичной оплате) -- берём именно
        # amount из самой записи долга, а не сумму продажи целиком.
        debts: dict[int, float] = {}
        while True:
            params: dict[str, Any] = {
                "first": first,
                "pageSize": page_size,
                "fromTime": int(date_from.timestamp() * 1000),
                "toTime": int(date_to.timestamp() * 1000),
                "storeId": self.store_id,
            }
            if seller_id is not None:
                params["customFieldItemId"] = seller_id
            if pos_id is not None:
                params["posId"] = pos_id
            data = self._get("/opr/sale/list-without-products", params=params)
            all_sales.extend(data.get("sales", []))
            for d in data.get("debts", []):
                debts[d["saleId"]] = d["amount"]
            if len(all_sales) >= data.get("count", 0) or not data.get("sales"):
                break
            first += page_size

        unpaid_ids = set(debts.keys())
        # Сумма долга считается по ВСЕМ продажам за период (не только
        # оставшимся после exclude_unpaid) -- иначе при exclude_unpaid=False
        # (store_total_sales) долг всегда был бы 0, т.к. неоплаченные уже
        # не отфильтрованы из sales.
        debt_amount = sum(debts.values())

        sales = [s for s in all_sales if s["id"] not in unpaid_ids] if exclude_unpaid else all_sales
        return {"count": len(sales), "saleAmount": sum(s["amount"] for s in sales), "debtAmount": debt_amount}

    def sale_stats(
        self, seller_id: int, date_from: datetime, date_to: datetime,
        all_pos: bool = True, *, exclude_unpaid: bool = False,
    ) -> dict:
        """{'count': int, 'saleAmount': float} for one seller over a period.

        all_pos=True (default): no posId filter -- counts sales across
          every POS at the store (manual invoices/накладные AND physical
          terminal receipts/чеки). Confirmed live 2026-08-16 that sellers
          across stores (Iposuda included) ring up both channels, so this
          is the default everywhere now.
        all_pos=False: scoped to just the virtual manual-entry POS (see
          _find_manual_pos_id) -- matches exactly what the "Продажи"
          (накладные) list shows, i.e. manual invoices only.

        Includes unpaid and partially paid invoices at their FULL sale
        amount, and counts each as a served customer, just like paid sales.
        Debt is reported separately in debtAmount; it must not be added to
        saleAmount again. Paying an invoice does not create another sale.
        Set exclude_unpaid=True only for an explicitly paid-only report.
        """
        return self._list_sales(date_from, date_to, seller_id=seller_id, all_pos=all_pos, exclude_unpaid=exclude_unpaid)

    def debts_by_seller(self, date_from: datetime, date_to: datetime) -> dict[int, float]:
        """{customFieldItemId: outstanding debt amount} for the WHOLE store
        over a period -- joins the same `debts` ({saleId, amount}) array
        used by store_total_sales() with `saleCustomFields` (saleId ->
        "Продавцы" customFieldItemId), both returned by the same
        /opr/sale/list-without-products endpoint, to attribute each
        unpaid sale to the seller tagged on it.

        Debt on sales with no seller tag (e.g. an "Online"/aggregate
        channel) is simply omitted here -- callers wanting that remainder
        should subtract sum(this dict.values()) from the store-wide
        debtAmount (store_total_sales) for the same period.

        No posId filter (matches store_total_sales(all_pos=True)) so the
        per-seller total is comparable to the store-wide debt figure
        already synced into store_daily_revenue.debtAmount.
        """
        seller_field_id = self._find_seller_field_id()
        page_size = 500
        first = 0
        seen_sales = 0
        debts: dict[int, float] = {}
        sale_seller: dict[int, int] = {}
        while True:
            params: dict[str, Any] = {
                "first": first,
                "pageSize": page_size,
                "fromTime": int(date_from.timestamp() * 1000),
                "toTime": int(date_to.timestamp() * 1000),
                "storeId": self.store_id,
            }
            data = self._get("/opr/sale/list-without-products", params=params)
            page_sales = data.get("sales", [])
            seen_sales += len(page_sales)
            for scf in data.get("saleCustomFields", []):
                if scf.get("customFieldId") == seller_field_id:
                    sale_seller[scf["saleId"]] = scf["customFieldItemId"]
            for d in data.get("debts", []):
                debts[d["saleId"]] = d["amount"]
            if seen_sales >= data.get("count", 0) or not page_sales:
                break
            first += page_size

        by_seller: dict[int, float] = {}
        for sale_id, amount in debts.items():
            seller_id = sale_seller.get(sale_id)
            if seller_id is not None:
                by_seller[seller_id] = by_seller.get(seller_id, 0.0) + amount
        return by_seller

    def period_breakdown(self, date_from: datetime, date_to: datetime) -> dict:
        """Одним проходом по /opr/sale/list-without-products (все кассы, без
        фильтра по продавцу): итог по точке целиком (как store_total_sales:
        amount включает неоплаченные, debt -- их сумма) и личные продажи по
        каждому продавцу (как sale_stats: включая долговые накладные
        целиком, независимо от суммы оставшегося долга).

        {'amount', 'debt', 'by_seller': {customFieldItemId: {'count', 'amount'}}}
        Для исторической загрузки (backfill_history.py): вместо отдельного
        запроса на каждого продавца -- один на день.
        """
        seller_field_id = self._find_seller_field_id()
        page_size = 500
        first = 0
        all_sales: list[dict] = []
        debts: dict[int, float] = {}
        sale_seller: dict[int, int] = {}
        while True:
            data = self._get(
                "/opr/sale/list-without-products",
                params={
                    "first": first,
                    "pageSize": page_size,
                    "fromTime": int(date_from.timestamp() * 1000),
                    "toTime": int(date_to.timestamp() * 1000),
                    "storeId": self.store_id,
                },
            )
            page_sales = data.get("sales", [])
            all_sales.extend(page_sales)
            for scf in data.get("saleCustomFields", []):
                if scf.get("customFieldId") == seller_field_id:
                    sale_seller[scf["saleId"]] = scf["customFieldItemId"]
            for d in data.get("debts", []):
                debts[d["saleId"]] = d["amount"]
            if len(all_sales) >= data.get("count", 0) or not page_sales:
                break
            first += page_size

        by_seller: dict[int, dict] = {}
        for s in all_sales:
            seller_id = sale_seller.get(s["id"])
            if seller_id is None:
                continue
            entry = by_seller.setdefault(seller_id, {"count": 0, "amount": 0.0})
            entry["count"] += 1
            entry["amount"] += s["amount"]
        return {
            "amount": sum(s["amount"] for s in all_sales),
            "debt": sum(debts.values()),
            "by_seller": by_seller,
        }

    def day_full(self, date_from: datetime, date_to: datetime) -> dict:
        """Всё про один день выбранной точки за один проход (все кассы).

        snapshot -- итоги в том же виде, что снимок кассового отчёта:
          выручка как в UMAG «Прибыль/убытки» (продажи минус возвраты),
          долги, накладные оплачено/нет, строки долгов (у продавцов из
          AGGREGATED_DEBT_SELLERS одной суммой, остальные по контрагентам).
        by_seller -- личные продажи по продавцам, включая долговые накладные
          полной суммой (как sale_stats по умолчанию).
        """
        seller_field_id = self._find_seller_field_id()
        seller_names = {s["id"]: s["name"] for s in self.list_sellers()}
        page_size = 500
        first = 0
        sales: list[dict] = []
        debts: dict[int, float] = {}
        sale_seller: dict[int, int] = {}
        customers: dict[int, str] = {}
        virtual_pos: set[int] = set()
        while True:
            data = self._get(
                "/opr/sale/list-without-products",
                params={
                    "first": first,
                    "pageSize": page_size,
                    "fromTime": int(date_from.timestamp() * 1000),
                    "toTime": int(date_to.timestamp() * 1000),
                    "storeId": self.store_id,
                },
            )
            page = data.get("sales", [])
            sales.extend(page)
            for d in data.get("debts", []):
                debts[d["saleId"]] = d["amount"]
            for f in data.get("saleCustomFields", []):
                if f.get("customFieldId") == seller_field_id:
                    sale_seller[f["saleId"]] = f["customFieldItemId"]
            for c in data.get("customers", []):
                customers[c["id"]] = str(c.get("name") or "").strip()
            for pos in data.get("poses", []):
                if pos.get("virtual"):
                    virtual_pos.add(pos["id"])
            if len(sales) >= data.get("count", 0) or not page:
                break
            first += page_size

        pnl = self._get(
            "/report/profit-and-loss",
            params={
                "fromTime": int(date_from.timestamp() * 1000),
                "toTime": int(date_to.timestamp() * 1000),
                "storeId": self.store_id,
            },
        ).get("profitReport", {})
        gross = float(pnl.get("saleAmount", sum(s["amount"] for s in sales)))
        refunds = float(pnl.get("refundAmount", 0))
        revenue = float(pnl.get("revenueAmount", gross - refunds))

        by_seller: dict[int, dict] = {}
        debt_lines: dict[str, float] = {}
        for s in sales:
            # Личные продажи: долговые и частично оплаченные накладные входят
            # полной суммой, как в sale_stats(exclude_unpaid=False); долг
            # считается отдельно и к продажам повторно не прибавляется.
            seller_id = sale_seller.get(s["id"])
            if seller_id is not None:
                entry = by_seller.setdefault(seller_id, {"count": 0, "amount": 0.0})
                entry["count"] += 1
                entry["amount"] += s["amount"]
            owed = debts.get(s["id"])
            if owed is None:
                continue
            seller =seller_names.get(sale_seller.get(s["id"], -1), "").lower()
            aggregated = next((label for words, label in AGGREGATED_DEBT_SELLERS if all(w in seller for w in words)), None)
            name = aggregated or customers.get(s.get("customerId") or -1) or "Без контрагента"
            debt_lines[name] = debt_lines.get(name, 0.0) + owed

        invoices = [s for s in sales if s["posId"] in virtual_pos]
        snapshot = {
            "sales": {
                "salesCount": len(sales),
                "revenue": round_half_up(revenue),
                "grossSales": round_half_up(gross),
                "refunds": round_half_up(refunds),
                "debt": round_half_up(sum(debts.values())),
                "unpaidCount": len(debts),
                "invoices": {
                    "count": len(invoices),
                    "paidCount": sum(1 for s in invoices if s["id"] not in debts),
                    "unpaidCount": sum(1 for s in invoices if s["id"] in debts),
                    "amount": round_half_up(sum(s["amount"] for s in invoices)),
                    "debt": round_half_up(sum(debts.get(s["id"], 0.0) for s in invoices)),
                },
            },
            "debtors": sorted(
                ({"name": n, "amount": round_half_up(a)} for n, a in debt_lines.items()),
                key=lambda d: -d["amount"],
            ),
        }
        return {"snapshot": snapshot, "by_seller": by_seller}

    def payments(self, date_from: datetime, date_to: datetime) -> list[dict]:
        """Платежи точки за период (раздел «Платежи» кабинета UMAG):
        [{id, time_ms, type, amount, account, purpose, note, user}]."""
        out: dict[int, dict] = {}
        first = 0
        while True:
            data = self._get(
                "/fin/payment/list-store-payments",
                params={
                    "first": first,
                    "pageSize": 100,
                    "fromTime": int(date_from.timestamp() * 1000),
                    "toTime": int(date_to.timestamp() * 1000),
                    "storeId": self.store_id,
                },
            )
            item_name = {i["id"]: i["name"] for i in data.get("customFieldItems", [])}
            purpose: dict[int, str] = {}
            for f in data.get("paymentCustomFields", []):
                if f["paymentId"] not in purpose and f["customFieldItemId"] in item_name:
                    purpose[f["paymentId"]] = item_name[f["customFieldItemId"]]
            account = {a["paymentId"]: a["accountName"] for a in data.get("accounts", [])}
            user = {u["paymentId"]: u["firstName"] for u in data.get("users", [])}
            for p in data.get("payments", []):
                out[p["id"]] = {
                    "id": p["id"],
                    "time_ms": p["docTime"],
                    "type": p["type"],
                    "amount": round_half_up(p["amount"]),
                    "account": account.get(p["id"]),
                    "purpose": purpose.get(p["id"]),
                    "note": p.get("note"),
                    "user": user.get(p["id"]),
                }
            if len(out) >= data.get("count", 0) or not data.get("payments"):
                break
            first += 100
        return list(out.values())

    def store_total_sales(self, date_from: datetime, date_to: datetime, all_pos: bool = True) -> dict:
        """{'count': int, 'saleAmount': float} for the WHOLE store over a
        period -- no seller filter, so this includes sales not tagged to
        any specific "Продавцы" item (e.g. an "Online"/aggregate bucket) as
        well as every named seller's sales.

        exclude_unpaid=False (same as sale_stats): UMAG's own "Отчёт
        прибыль/убытки" "Выручка" figure counts every sale regardless of
        outstanding balance -- confirmed live 2026-08-16 (Iposuda, matched
        to within 0.3%). The store total can exceed the sum of tracked
        sellers because it also includes sales without a seller tag."""
        return self._list_sales(date_from, date_to, seller_id=None, all_pos=all_pos, exclude_unpaid=False)
