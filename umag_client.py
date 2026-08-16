"""Thin client around the (undocumented) UMAG REST API -- sales-stats subset.

Auth confirmed live 2026-07-12 against a different UMAG account, and the
"Продавцы" (sellers) custom-field + per-seller sale stats confirmed live
2026-08-14 against this account (storeId=42219, field id=20244).
"""

from __future__ import annotations

import base64
import difflib
from datetime import datetime, timedelta
from typing import Any

import httpx

BASE_URL = "https://api.umag.kz/rest/cabinet"
CLIENT_VER = "angular_cabinet_20.0.10"
API_VER = "1.4"

SELLER_FIELD_NAME = "Продавцы"


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
        self._http = httpx.Client(base_url=BASE_URL, timeout=20.0)

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
        field_id = self._find_seller_field_id()
        data = self._get(f"/org/custom-field/get/{field_id}", params={"storeId": self.store_id})
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

    def sale_stats(self, seller_id: int, date_from: datetime, date_to: datetime) -> dict:
        """{'count': int, 'saleAmount': float} for one seller over a period,
        matching exactly what the "Продажи" (накладные) list shows:
        - scoped to the virtual manual-entry POS (see _find_manual_pos_id),
          same as that page's default filter -- confirmed live 2026-08-14
          this excludes ordinary POS-terminal retail receipts;
        - excludes any sale that currently has an outstanding balance
          ("Осталось" > 0 / shown red) -- `list-without-products` returns a
          `debts` array of {saleId, amount} for exactly those sales, which
          `list-all`'s aggregate stats don't expose per-sale.
        """
        pos_id = self._find_manual_pos_id()
        page_size = 500
        first = 0
        all_sales: list[dict] = []
        unpaid_ids: set[int] = set()
        while True:
            params = {
                "customFieldItemId": seller_id,
                "posId": pos_id,
                "first": first,
                "pageSize": page_size,
                "fromTime": int(date_from.timestamp() * 1000),
                "toTime": int(date_to.timestamp() * 1000),
                "storeId": self.store_id,
            }
            data = self._get("/opr/sale/list-without-products", params=params)
            all_sales.extend(data.get("sales", []))
            unpaid_ids.update(d["saleId"] for d in data.get("debts", []))
            if len(all_sales) >= data.get("count", 0) or not data.get("sales"):
                break
            first += page_size

        paid_sales = [s for s in all_sales if s["id"] not in unpaid_ids]
        return {"count": len(paid_sales), "saleAmount": sum(s["amount"] for s in paid_sales)}
