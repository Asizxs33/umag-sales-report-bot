"""Offline regression checks: python -m unittest test_credit_sales -v."""

import copy
import unittest
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import daily_sync
import full_sync
from umag_client import UmagClient


START = datetime(2026, 10, 11)
END = datetime(2026, 10, 11, 23, 59, 59)
SALES = {
    "count": 3,
    "sales": [
        {"id": 1, "amount": 10000},  # paid
        {"id": 2, "amount": 20000},  # completely unpaid
        {"id": 3, "amount": 100000},  # partially paid; only 30000 outstanding
    ],
    "debts": [{"saleId": 2, "amount": 20000}, {"saleId": 3, "amount": 30000}],
    "saleCustomFields": [
        {"saleId": sale_id, "customFieldId": 7, "customFieldItemId": 42}
        for sale_id in [1, 2, 3]
    ],
}


def client(payload):
    # No HTTP client or real credentials: every API response is a fixture.
    result = UmagClient.__new__(UmagClient)
    result.store_id = 1
    result._get = Mock(return_value=payload)
    result._find_seller_field_id = Mock(return_value=7)
    result._find_manual_pos_id = Mock(return_value=99)
    return result


class CreditSalesTests(unittest.TestCase):
    def test_all_invoices_count_at_full_value(self):
        umag = client(SALES)
        result = umag.sale_stats(42, START, END)
        self.assertEqual(result, {"count": 3, "saleAmount": 130000, "debtAmount": 50000})
        params = umag._get.call_args.kwargs["params"]
        self.assertEqual(params["customFieldItemId"], 42)
        self.assertNotIn("posId", params)

    def test_settlement_does_not_add_another_sale(self):
        paid = copy.deepcopy(SALES)
        paid["debts"] = []
        before = client(SALES).sale_stats(42, START, END)
        after = client(paid).sale_stats(42, START, END)
        self.assertEqual(before["saleAmount"], after["saleAmount"])
        self.assertEqual(before["count"], after["count"])
        self.assertEqual(after["debtAmount"], 0)

    def test_explicit_paid_only_filter_remains_available(self):
        result = client(SALES).sale_stats(42, START, END, exclude_unpaid=True)
        self.assertEqual(result["saleAmount"], 10000)
        self.assertEqual(result["count"], 1)

    def test_historical_breakdown_matches_daily_totals(self):
        umag = client(SALES)
        history = umag.period_breakdown(START, END)
        daily = umag.sale_stats(42, START, END)
        self.assertEqual(history["by_seller"][42], {"count": daily["count"], "amount": daily["saleAmount"]})
        self.assertEqual(history["debt"], 50000)
        self.assertEqual(umag.debts_by_seller(START, END), {42: 50000})

    def test_unassigned_sale_is_not_credited_to_a_consultant(self):
        payload = copy.deepcopy(SALES)
        payload["sales"].append({"id": 4, "amount": 9000})
        payload["count"] = 4
        payload["debts"].append({"saleId": 4, "amount": 9000})
        result = client(payload).period_breakdown(START, END)
        self.assertEqual(result["amount"], 139000)
        self.assertEqual(result["by_seller"][42]["amount"], 130000)

    def test_debt_on_later_page_does_not_remove_sale(self):
        umag = client(SALES)
        umag._get.side_effect = [
            {"count": 2, "sales": [{"id": 1, "amount": 10000}], "debts": []},
            {"count": 2, "sales": [{"id": 2, "amount": 100000}], "debts": [{"saleId": 2, "amount": 30000}]},
        ]
        self.assertEqual(umag.sale_stats(42, START, END), {"count": 2, "saleAmount": 110000, "debtAmount": 30000})


class DayFullTests(unittest.TestCase):
    def test_debts_by_counterparty_with_aggregated_seller_and_net_revenue(self):
        sales_payload = {
            "count": 4,
            "sales": [
                {"id": 1, "amount": 10000, "posId": 5, "customerId": None},
                {"id": 2, "amount": 20000, "posId": 99, "customerId": 11},  # Камилов Сардор: сворачивается
                {"id": 3, "amount": 30000, "posId": 99, "customerId": 12},  # Камилов Сардор: сворачивается
                {"id": 4, "amount": 100000, "posId": 99, "customerId": 13},  # другой продавец: по контрагенту
            ],
            "debts": [{"saleId": 2, "amount": 20000}, {"saleId": 3, "amount": 25000.5}, {"saleId": 4, "amount": 40000}],
            "customers": [{"id": 11, "name": "Клиент А"}, {"id": 12, "name": "Клиент Б"}, {"id": 13, "name": "Клиент В"}],
            "saleCustomFields": [
                {"saleId": 1, "customFieldId": 7, "customFieldItemId": 1},
                {"saleId": 2, "customFieldId": 7, "customFieldItemId": 2},
                {"saleId": 3, "customFieldId": 7, "customFieldItemId": 2},
                {"saleId": 4, "customFieldId": 7, "customFieldItemId": 3},
            ],
            "poses": [{"id": 99, "virtual": True}],
        }
        pnl = {"profitReport": {"saleAmount": 160000, "refundAmount": 5000, "revenueAmount": 155000}}

        umag = UmagClient.__new__(UmagClient)
        umag.store_id = 1
        umag._find_seller_field_id = Mock(return_value=7)
        umag.list_sellers = Mock(return_value=[{"id": 1, "name": "Иванов Пётр"}, {"id": 2, "name": "Камилов Сардор"}, {"id": 3, "name": "Дилнора Т"}])
        umag._get = Mock(side_effect=lambda path, params=None: pnl if "profit-and-loss" in path else sales_payload)

        result = umag.day_full(START, END)
        sales = result["snapshot"]["sales"]
        self.assertEqual(sales["revenue"], 155000)  # продажи минус возвраты, как в UMAG
        self.assertEqual(sales["refunds"], 5000)
        self.assertEqual(sales["debt"], 85001)  # 20000 + 25000.5 + 40000, округление как в JS
        self.assertEqual(sales["invoices"], {"count": 3, "paidCount": 0, "unpaidCount": 3, "amount": 150000, "debt": 85001})
        self.assertEqual(
            result["snapshot"]["debtors"],
            [{"name": "Сардор К", "amount": 45001}, {"name": "Клиент В", "amount": 40000}],
        )
        # долговые накладные входят в личные продажи полной суммой
        self.assertEqual(result["by_seller"][2], {"count": 2, "amount": 50000})


class SyncCreditSalesTests(unittest.IsolatedAsyncioTestCase):
    async def test_resync_replaces_totals_and_preserves_service_marks(self):
        stored = {("employee", "2026-10-11"): {"sales": 10000, "count": 1, "full_service_count": 1}}
        debt_lines = {}
        conn = Mock()
        conn.fetch = AsyncMock(return_value=[{"id": "employee", "full_name": "Test", "department": "Iposuda"}])
        conn.close = AsyncMock()
        transaction = MagicMock()
        transaction.__aenter__ = AsyncMock(return_value=None)
        transaction.__aexit__ = AsyncMock(return_value=False)
        conn.transaction = Mock(return_value=transaction)

        async def execute(sql, *args):
            if "INSERT INTO log_entries" in sql:
                self.assertIn("ON CONFLICT (employee_id, date, source, type)", sql)
                self.assertNotIn("full_service_count", sql)
                row = stored.setdefault((args[2], args[1]), {})
                row.update(sales=args[3], count=args[4])
            if "DELETE FROM store_daily_debt" in sql:
                for key in [k for k in debt_lines if k[:2] == (args[0], args[1])]:
                    del debt_lines[key]
            if "INSERT INTO store_daily_debt" in sql:
                debt_lines[(args[1], args[2], args[3])] = args[4]

        conn.execute = AsyncMock(side_effect=execute)

        def day(debtors):
            return {
                "snapshot": {
                    "sales": {"revenue": 130000, "debt": sum(d["amount"] for d in debtors)},
                    "debtors": debtors,
                },
                "by_seller": {42: {"count": 3, "amount": 130000}},
            }

        umag = Mock(store_id=1)
        umag.find_seller.return_value = {"id": 42}
        umag.payments.return_value = []
        umag.day_full.return_value = day([{"name": "Customer", "amount": 50000}])
        with patch.object(full_sync, "DASHBOARD_DATABASE_URL", "postgresql://test-only"), patch.object(full_sync.asyncpg, "connect", AsyncMock(return_value=conn)):
            await daily_sync.sync_day(umag, date(2026, 10, 11))
            await daily_sync.sync_day(umag, date(2026, 10, 11))
            self.assertEqual(debt_lines[("Iposuda", "2026-10-11", "Customer")], 50000)
            umag.day_full.return_value = day([])
            await daily_sync.sync_day(umag, date(2026, 10, 11))
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[("employee", "2026-10-11")], {"sales": 130000, "count": 3, "full_service_count": 1})
        # погашенный долг исчезает из строк долгов при повторной синхронизации
        self.assertFalse([k for k in debt_lines if k[0] == "Iposuda"])


if __name__ == "__main__":
    unittest.main()
