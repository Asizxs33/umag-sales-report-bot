"""Offline regression checks: python -m unittest test_credit_sales -v."""

import copy
import unittest
from datetime import date, datetime
from unittest.mock import AsyncMock, Mock, patch

import daily_sync
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


class SyncCreditSalesTests(unittest.IsolatedAsyncioTestCase):
    async def test_resync_replaces_totals_and_preserves_service_marks(self):
        stored = {("employee", "2026-10-11"): {"sales": 10000, "count": 1, "full_service_count": 1}}
        debt = {}
        conn = Mock()
        conn.fetch = AsyncMock(return_value=[{"id": "employee", "full_name": "Test", "department": "Iposuda"}])
        conn.close = AsyncMock()

        async def execute(sql, *args):
            if "INSERT INTO log_entries" in sql:
                self.assertIn("ON CONFLICT (employee_id, date, source, type)", sql)
                self.assertNotIn("full_service_count", sql)
                row = stored.setdefault((args[2], args[1]), {})
                row.update(sales=args[3], count=args[4])
            if "INSERT INTO employee_daily_debt" in sql:
                debt[(args[1], args[3])] = args[4]

        conn.execute = AsyncMock(side_effect=execute)
        umag = Mock(store_id=1)
        umag.find_seller.return_value = {"id": 42}
        umag.sale_stats.return_value = {"count": 3, "saleAmount": 130000, "debtAmount": 50000}
        umag.store_total_sales.return_value = {"count": 3, "saleAmount": 130000, "debtAmount": 50000}
        umag.debts_by_seller.return_value = {42: 50000}
        with patch.object(daily_sync, "DASHBOARD_DATABASE_URL", "postgresql://test-only"), patch.object(daily_sync.asyncpg, "connect", AsyncMock(return_value=conn)):
            await daily_sync.sync_day(umag, date(2026, 10, 11))
            await daily_sync.sync_day(umag, date(2026, 10, 11))
            umag.debts_by_seller.return_value = {}
            await daily_sync.sync_day(umag, date(2026, 10, 11))
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[("employee", "2026-10-11")], {"sales": 130000, "count": 3, "full_service_count": 1})
        self.assertEqual(debt[("employee", "2026-10-11")], 0)
        self.assertFalse(umag.sale_stats.call_args.kwargs["exclude_unpaid"])


if __name__ == "__main__":
    unittest.main()
