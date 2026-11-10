"""Owner-only HTTP boundary, exercised against a real loopback server."""
import http.client
import io
import json
import secrets
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from salesroom.core import SalesRoom
from salesroom.paypal import SandboxError, settlement_state
from salesroom.server import OwnerServer, OwnerHandler, ROOT, owner_key


class SandboxStub:
    def __init__(self):
        self.creates = []
        self.captures = []
        self.order = None
        self.lose_capture_response = False
        self.capture_amount = None

    def create_order(self, row, origin):
        self.creates.append(dict(row))
        order_id = f"TESTORDER{122 + len(self.creates)}"
        self.order = {"id": order_id, "status": "CREATED", "purchase_units": [{
            "reference_id": row["create_key"], "amount": {"currency_code": "USD", "value": row["amount"]},
        }], "links": [{"rel": "payer-action", "href": f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}"}]}
        return self.order

    def get_order(self, order_id):
        assert order_id == self.order["id"]
        return self.order

    def capture_order(self, row):
        self.captures.append(dict(row))
        self.order["status"] = "COMPLETED"
        self.order["purchase_units"][0]["payments"] = {"captures": [{
            "id": f"TESTCAPTURE{122 + len(self.captures)}", "status": "COMPLETED",
            "amount": {"currency_code": "USD", "value": self.capture_amount or row["amount"]},
        }]}
        if self.lose_capture_response:
            raise SandboxError("Simulated provider timeout after capture.")
        return self.order


class OwnerHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        try:
            self.server = OwnerServer(Path(self.tmp.name), ROOT / "salesroom/catalog.demo.json", 0)
        except PermissionError:
            self.tmp.cleanup()
            self.skipTest("This environment prevents opening a loopback HTTP port")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.cookie = None
        self.csrf = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, method, path, value=None, auth=True, origin=True, csrf=True, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if host:
            headers["Host"] = host
        if auth and self.cookie:
            headers["Cookie"] = self.cookie
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{self.port}"
        if csrf and self.csrf:
            headers["X-Salesroom-CSRF"] = self.csrf
        raw = None
        if value is not None:
            raw = json.dumps(value)
            headers["Content-Type"] = "application/json"
        connection.request(method, path, raw, headers)
        response = connection.getresponse()
        data = response.read().decode()
        status = response.status
        cookie = response.getheader("Set-Cookie")
        connection.close()
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            pass
        return status, data, cookie

    def login(self):
        code, result, cookie = self.request("POST", "/api/session", {"token": self.server.bootstrap_token})
        self.assertEqual(code, 200)
        self.cookie = cookie.split(";", 1)[0]
        self.csrf = result["csrf"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def create(self):
        brief = json.loads((ROOT / "examples/lead.complete.json").read_text())
        code, lead, _ = self.request("POST", "/api/leads", {"brief": brief, "event_id": "http-create"})
        self.assertEqual(code, 200, lead)
        return lead

    def test_anonymous_actor_claim_cannot_approve(self):
        code, _, _ = self.request("POST", "/api/leads/fake/approve", {
            "actor": "owner", "approved": True, "revision": 1, "quote_hash": "fake", "event_id": "fake"
        }, auth=False)
        self.assertEqual(code, 403)
        code, _, _ = self.request("GET", "/api/state", auth=False)
        self.assertEqual(code, 403)

    def test_origin_csrf_and_host_checks(self):
        self.login()
        code, _, _ = self.request("POST", "/api/leads", {}, origin=False)
        self.assertEqual(code, 403)
        code, _, _ = self.request("POST", "/api/leads", {}, csrf=False)
        self.assertEqual(code, 403)
        code, _, _ = self.request("GET", "/api/state", host="attacker.invalid")
        self.assertEqual(code, 403)

    def test_browser_connection_requires_owner_and_keeps_only_verified_sandbox(self):
        self.server.paypal = None
        path = "/api/paypal/connect"
        keys = {"client_id": "sandbox-demo-client", "client_secret": "sandbox-demo-secret"}
        self.assertEqual(self.request("POST", path, keys, auth=False)[0], 403)
        self.login()
        self.assertEqual(self.request("POST", path, keys, origin=False)[0], 403)
        self.assertEqual(self.request("POST", path, keys, csrf=False)[0], 403)
        self.assertEqual(self.request("POST", path, {**keys, "client_secret": "••••••••••••••••"})[0], 409)
        with patch("salesroom.server.PayPalSandbox") as sandbox:
            candidate = sandbox.return_value
            candidate.check_credentials.side_effect = SandboxError("Sandbox authentication failed.")
            code, result, _ = self.request("POST", path, keys)
            self.assertEqual(code, 502, result)
            self.assertIsNone(self.server.paypal)
            self.assertNotIn(keys["client_secret"], json.dumps(result))

            candidate.check_credentials.side_effect = None
            code, result, _ = self.request("POST", path, keys)
            self.assertEqual((code, result), (200, {"paypal_enabled": True}))
            self.assertIs(self.server.paypal, candidate)
            self.assertEqual(self.request("POST", path, keys)[0], 409)

    def test_actual_owner_flow_and_stale_download_refused(self):
        self.login()
        lead = self.create()
        url = "/api/leads/" + lead["id"]
        code, _, _ = self.request("GET", url + "/proposal")
        self.assertEqual(code, 409)
        code, lead, _ = self.request("POST", url + "/review", {"event_id": "http-review"})
        self.assertEqual(code, 200, lead)
        self.assertEqual(lead["status"], "awaiting_approval")
        code, lead, _ = self.request("POST", url + "/approve", {
            "event_id": "http-approve", "revision": lead["revision"], "quote_hash": lead["quote"]["hash"]
        })
        self.assertEqual(code, 200, lead)
        self.assertEqual(lead["status"], "approved")
        code, exported, _ = self.request("POST", url + "/export", {})
        self.assertEqual(code, 200, exported)
        self.assertTrue((Path(self.tmp.name) / "proposals" / exported["filename"]).exists())
        code, proposal, _ = self.request("GET", url + "/proposal")
        self.assertEqual(code, 200)
        self.assertIn("Alma Studio", proposal)
        self.assertIn(lead["quote"]["hash"], proposal)
        code, revised, _ = self.request("POST", url + "/update", {
            "event_id": "http-change", "changes": {"budget_usd": 450}
        })
        self.assertEqual(code, 200, revised)
        self.assertIsNone(revised["approval"])
        code, _, _ = self.request("GET", url + "/proposal")
        self.assertIn(code, (403, 409))

    def approved_checkout(self):
        self.login()
        lead = self.create()
        url = "/api/leads/" + lead["id"]
        quote = {"revision": lead["revision"], "quote_hash": lead["quote"]["hash"]}
        self.server.paypal = SandboxStub()
        self.assertEqual(self.request("POST", url + "/paypal-create", quote)[0], 409)
        self.assertEqual(self.request("POST", url + "/review", {"event_id": "checkout-review"})[0], 200)
        code, data, _ = self.request("POST", url + "/approve", {**quote, "event_id": "checkout-approve"})
        self.assertEqual(code, 200, data)
        return url, quote

    def test_checkout_uses_owner_quote_and_requires_buyer_approval(self):
        url, quote = self.approved_checkout()
        self.assertEqual(self.request("POST", url + "/paypal-create", quote, auth=False)[0], 403)
        self.assertEqual(self.request("POST", url + "/paypal-create", quote, csrf=False)[0], 403)
        code, data, _ = self.request("POST", url + "/paypal-create", {**quote, "amount": "0.01"})
        self.assertEqual(code, 200, data)
        payment = data["payment"]
        self.assertEqual((payment["state"], payment["amount"], payment["currency"]), ("ready", "150.00", "USD"))
        self.assertEqual(self.request("POST", url + "/paypal-create", quote)[1], data)
        self.assertEqual(len(self.server.paypal.creates), 1)
        capture = {**quote, "order_id": payment["order_id"]}
        self.assertEqual(self.request("GET", "/?paypal_return=1&token=TESTORDER123", auth=False)[0], 200)
        self.assertEqual(self.request("POST", url + "/paypal-capture", {**capture, "order_id": "WRONG"})[0], 409)
        code, data, _ = self.request("POST", url + "/paypal-capture", capture)
        self.assertEqual(code, 200, data)
        self.assertEqual(data["payment"]["state"], "ready")
        self.assertEqual(len(self.server.paypal.captures), 0)
        self.server.paypal.order["status"] = "APPROVED"
        code, data, _ = self.request("POST", url + "/paypal-capture", capture)
        self.assertEqual(code, 200, data)
        self.assertEqual(data["payment"]["state"], "paid")
        self.assertEqual(data["payment"]["capture_id"], "TESTCAPTURE123")
        self.assertEqual(self.request("POST", url + "/paypal-capture", capture)[1], data)
        self.assertEqual(len(self.server.paypal.captures), 1)

    def test_checkout_refuses_stale_proposal(self):
        url, quote = self.approved_checkout()
        _, data, _ = self.request("POST", url + "/paypal-create", quote)
        self.server.paypal.order["status"] = "APPROVED"
        self.assertEqual(self.request("POST", url + "/update", {
            "event_id": "new-terms", "changes": {"budget_usd": 450},
        })[0], 200)
        self.assertIsNone(self.request("GET", url + "/payment")[1]["payment"])
        self.assertEqual(self.request("POST", url + "/paypal-capture", {
            **quote, "order_id": data["payment"]["order_id"],
        })[0], 409)
        self.assertEqual(len(self.server.paypal.captures), 0)

    def revised_approved_quote(self, url, changes, event):
        code, changed, _ = self.request("POST", url + "/update", {"event_id": event + "-edit", "changes": changes})
        self.assertEqual(code, 200, changed)
        quote = {"revision": changed["revision"], "quote_hash": changed["quote"]["hash"]}
        self.assertEqual(self.request("POST", url + "/review", {"event_id": event + "-review"})[0], 200)
        self.assertEqual(self.request("POST", url + "/approve", {**quote, "event_id": event + "-approve"})[0], 200)
        return quote

    def pay_quote(self, url, quote, amount):
        code, data, _ = self.request("POST", url + "/paypal-create", quote)
        self.assertEqual(code, 200, data)
        self.assertEqual(data["payment"]["amount"], amount)
        self.server.paypal.order["status"] = "APPROVED"
        code, data, _ = self.request("POST", url + "/paypal-capture", {**quote, "order_id": data["payment"]["order_id"]})
        self.assertEqual(code, 200, data)
        self.assertEqual(data["payment"]["state"], "paid")
        return data["payment"]

    def test_revised_checkout_only_collects_the_outstanding_balance(self):
        url, quote = self.approved_checkout()
        self.pay_quote(url, quote, "150.00")

        unchanged_total = self.revised_approved_quote(url, {"budget_usd": 450}, "same-price")
        self.assertEqual(self.request("POST", url + "/paypal-create", unchanged_total)[0], 409)
        self.assertEqual(1, len(self.server.paypal.creates))

        increased = self.revised_approved_quote(url, {"services": [
            {"service_id": "landing_page", "quantity": 1}, {"service_id": "lead_form", "quantity": 2},
        ]}, "increased-price")
        paid = self.pay_quote(url, increased, "30.00")
        self.assertEqual(paid, self.request("POST", url + "/paypal-create", increased)[1]["payment"])
        self.assertEqual(2, len(self.server.paypal.creates))
        self.assertEqual(2, len(self.server.paypal.captures))

        reduced = self.revised_approved_quote(url, {"services": [
            {"service_id": "landing_page", "quantity": 1},
        ]}, "reduced-price")
        self.assertEqual(self.request("POST", url + "/paypal-create", reduced)[0], 409)
        lead_id = url.rsplit("/", 1)[1]
        summary = settlement_state(self.server.room(), lead_id)["settlement"]
        self.assertEqual((summary["quote_total"], summary["captured"], summary["due"], summary["credit"]),
                         ("120.00", "180.00", "0.00", "60.00"))
        self.assertEqual(2, len(self.server.paypal.creates))

    def test_legacy_order_cannot_capture_the_full_quote_again(self):
        url, quote = self.approved_checkout()
        self.pay_quote(url, quote, "150.00")
        increased = self.revised_approved_quote(url, {"services": [
            {"service_id": "landing_page", "quantity": 1}, {"service_id": "lead_form", "quantity": 2},
        ]}, "legacy-order")
        lead_id = url.rsplit("/", 1)[1]
        with self.server.room().payment_transaction(lead_id) as (db, current):
            db.execute("""INSERT INTO paypal_payments
                (lead_id,revision,quote_hash,amount,state,create_key,capture_key,order_id,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                       (lead_id, increased["revision"], increased["quote_hash"], "180.00", "ready",
                        "legacy-create-key", "legacy-capture-key", "TESTLEGACYORDER", current["created_at"]))
        self.assertEqual(self.request("POST", url + "/paypal-create", increased)[0], 409)
        self.assertEqual(self.request("POST", url + "/paypal-capture", {**increased, "order_id": "TESTLEGACYORDER"})[0], 409)
        self.assertEqual(1, len(self.server.paypal.creates))
        self.assertEqual(1, len(self.server.paypal.captures))

    def test_lost_capture_response_is_reconciled_without_second_capture(self):
        url, quote = self.approved_checkout()
        _, data, _ = self.request("POST", url + "/paypal-create", quote)
        capture = {**quote, "order_id": data["payment"]["order_id"]}
        self.server.paypal.order["status"] = "APPROVED"
        self.server.paypal.lose_capture_response = True
        self.assertEqual(self.request("POST", url + "/paypal-capture", capture)[0], 502)
        self.assertEqual(self.request("GET", url + "/payment")[1]["payment"]["state"], "capturing")
        self.assertEqual(self.request("POST", url + "/update", {
            "event_id": "concurrent-change", "changes": {"budget_usd": 450},
        })[0], 409)
        code, data, _ = self.request("POST", url + "/paypal-capture", capture)
        self.assertEqual(code, 200, data)
        self.assertEqual(data["payment"]["state"], "paid")
        self.assertEqual(len(self.server.paypal.captures), 1)

    def test_provider_mismatched_capture_does_not_mark_quote_paid(self):
        url, quote = self.approved_checkout()
        _, data, _ = self.request("POST", url + "/paypal-create", quote)
        self.server.paypal.order["status"] = "APPROVED"
        self.server.paypal.capture_amount = "149.99"
        self.assertEqual(self.request("POST", url + "/paypal-capture", {
            **quote, "order_id": data["payment"]["order_id"],
        })[0], 502)
        self.assertEqual(self.request("GET", url + "/payment")[1]["payment"]["state"], "capturing")


class RequestPipelineTests(OwnerHTTPTests):
    """Exercise real HTTP parsing/authorization in memory; not a live browser claim."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data_dir = Path(self.tmp.name)
        catalog = ROOT / "salesroom/catalog.demo.json"
        token = owner_key(data_dir)
        SalesRoom(data_dir / "sales.sqlite3", catalog, token)
        self.server = SimpleNamespace(
            data_dir=data_dir, catalog=catalog, owner_token=token,
            bootstrap_token=secrets.token_urlsafe(32),
            session_token=secrets.token_urlsafe(32), csrf_token=secrets.token_urlsafe(32),
            paypal=None,
            server_address=("127.0.0.1", 8787),
            room=lambda: SalesRoom(data_dir / "sales.sqlite3", catalog, token),
        )
        self.port, self.cookie, self.csrf = 8787, None, None

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, method, path, value=None, auth=True, origin=True, csrf=True, host=None):
        raw = json.dumps(value).encode() if value is not None else b""
        lines = [f"{method} {path} HTTP/1.1", f"Host: {host or f'127.0.0.1:{self.port}'}", "Connection: close"]
        if auth and self.cookie:
            lines.append("Cookie: " + self.cookie)
        if origin:
            lines.append(f"Origin: http://127.0.0.1:{self.port}")
        if csrf and self.csrf:
            lines.append("X-Salesroom-CSRF: " + self.csrf)
        if value is not None:
            lines.extend(["Content-Type: application/json", f"Content-Length: {len(raw)}"])

        class MemorySocket:
            def __init__(self, request):
                self.input = io.BytesIO(request)
                self.output = io.BytesIO()

            def makefile(self, *_args, **_kwargs):
                return self.input

            def sendall(self, data):
                self.output.write(data)

        connection = MemorySocket(("\r\n".join(lines) + "\r\n\r\n").encode() + raw)
        OwnerHandler(connection, ("127.0.0.1", 1), self.server)
        response = http.client.HTTPResponse(MemorySocket(connection.output.getvalue()))
        response.begin()
        data = response.read().decode()
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            pass
        return response.status, data, response.getheader("Set-Cookie")


if __name__ == "__main__":
    unittest.main()
