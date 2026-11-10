"""A reduction can refund multiple captures exactly once."""
import json
from pathlib import Path
import tempfile
import unittest

from salesroom.core import SalesRoom, _now
from salesroom.paypal import SandboxError, settlement_state
from salesroom.refunds import refund_credit


ROOT = Path(__file__).resolve().parent.parent


class RefundSandboxStub:
    def __init__(self):
        self.calls = []
        self.results = {}
        self.lost_response = False
        self.pending = False
        self.wrong_amount = False

    def refund_capture(self, row):
        key = row["request_id"]
        self.calls.append((row["capture_id"], row["amount"], key))
        if key not in self.results:
            self.results[key] = {
                "id": f"TESTREFUND{len(self.results) + 1}",
                "status": "PENDING" if self.pending else "COMPLETED",
                "amount": {"currency_code": "USD", "value": "0.01" if self.wrong_amount else row["amount"]},
                "invoice_id": key,
                "links": [{"rel": "up", "href": "https://api-m.sandbox.paypal.com/v2/payments/captures/" + row["capture_id"]}],
            }
            if self.lost_response:
                self.lost_response = False
                raise SandboxError("Simulated lost refund response.")
        return self.results[key]

    def get_refund(self, refund_id):
        result = next(item for item in self.results.values() if item["id"] == refund_id)
        if not self.pending:
            result["status"] = "COMPLETED"
        return result


class RefundTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.room = SalesRoom(Path(directory.name) / "sales.sqlite3", owner_token="test-owner")
        brief = json.loads((ROOT / "examples/lead.complete.json").read_text())
        self.lead_id = self.room.create_lead("start", brief)["id"]
        self.sandbox = RefundSandboxStub()

    def approve(self):
        lead = self.room.get_lead(self.lead_id)
        revision = lead["revision"]
        self.room.review(self.lead_id, f"review-{revision}")
        return self.room.approve(self.lead_id, f"approve-{revision}", revision, lead["quote"]["hash"], "test-owner")

    def scope(self, forms):
        services = [{"service_id": "landing_page", "quantity": 1}]
        if forms:
            services.append({"service_id": "lead_form", "quantity": forms})
        self.room.update_lead(self.lead_id, f"scope-{forms}", {"services": services})
        return self.approve()

    def paid(self, quote, amount):
        revision = quote["revision"]
        with self.room.payment_transaction(self.lead_id) as (db, _):
            db.execute("""INSERT INTO paypal_payments
                (lead_id,revision,quote_hash,amount,state,create_key,capture_key,order_id,capture_id,created_at)
                VALUES(?,?,?,?,'paid',?,?,?,?,?)""", (self.lead_id, revision, quote["quote"]["hash"], amount,
                f"create-{revision}", f"capture-{revision}", f"TESTORDER{revision}", f"TESTCAPTURE{revision}", _now()))

    def overpayment(self):
        self.paid(self.approve(), "150.00")
        self.paid(self.scope(2), "30.00")
        return self.scope(0)

    def refund(self, quote):
        return refund_credit(self.room, self.sandbox, self.lead_id, quote["revision"], quote["quote"]["hash"])

    def test_refunds_exact_credit_across_two_captures_and_replays(self):
        quote = self.overpayment()
        before = settlement_state(self.room, self.lead_id)["settlement"]
        self.assertEqual((before["credit"], before["net_paid"], before["due"]), ("60.00", "180.00", "0.00"))
        after = self.refund(quote)
        self.assertEqual([(capture, amount) for capture, amount, _ in self.sandbox.calls],
                         [("TESTCAPTURE2", "30.00"), ("TESTCAPTURE1", "30.00")])
        self.assertEqual((after["refunded"], after["net_paid"], after["credit"]), ("60.00", "120.00", "0.00"))
        self.assertEqual(self.refund(quote), after)
        self.assertEqual(len(self.sandbox.calls), 2)

    def test_lost_response_reuses_reservation_and_blocks_edits(self):
        quote = self.overpayment()
        self.sandbox.lost_response = True
        with self.assertRaises(SandboxError):
            self.refund(quote)
        with self.assertRaises(ValueError):
            self.room.update_lead(self.lead_id, "while-refunding", {"budget_usd": 410})
        after = self.refund(quote)
        self.assertEqual(self.sandbox.calls[0], self.sandbox.calls[1])
        self.assertEqual(len(self.sandbox.results), 2)
        self.assertEqual(after["refunded"], "60.00")

    def test_pending_refund_is_reconciled_without_resending(self):
        quote = self.overpayment()
        self.sandbox.pending = True
        self.assertTrue(self.refund(quote)["refund_pending"])
        self.assertEqual(len(self.sandbox.calls), 1)
        self.sandbox.pending = False
        self.assertEqual(self.refund(quote)["refunded"], "60.00")
        self.assertEqual(len(self.sandbox.calls), 2)

    def test_stale_revision_and_wrong_provider_amount_cannot_settle(self):
        quote = self.overpayment()
        with self.assertRaises(ValueError):
            refund_credit(self.room, self.sandbox, self.lead_id, quote["revision"] - 1, quote["quote"]["hash"])
        self.assertEqual(len(self.sandbox.calls), 0)
        self.sandbox.wrong_amount = True
        with self.assertRaises(SandboxError):
            self.refund(quote)
        after = settlement_state(self.room, self.lead_id)["settlement"]
        self.assertEqual((after["refunded"], after["credit"]), ("0.00", "60.00"))
