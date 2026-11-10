"""PayPal Sandbox checkout for a specific owner-approved revision."""
from __future__ import annotations

from base64 import b64encode
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from .core import _now


SANDBOX_API = "https://api-m.sandbox.paypal.com"
TOKEN_PATH = "/v1/oauth2/token"
ORDER_PATH = "/v2/checkout/orders"
PAYMENTS_PATH = "/v2/payments"
SANDBOX_HOSTS = {"www.sandbox.paypal.com", "sandbox.paypal.com"}
PAYPAL_ID = re.compile(r"[A-Z0-9]{1,36}")


class SandboxError(Exception):
    """A provider failure safe to show without provider payloads or secrets."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PayPalSandbox:
    def __init__(self, client_id: str, client_secret: str):
        self._basic = b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
        self._opener = build_opener(NoRedirect())
        self._token = ""
        self._expires = 0.0

    @classmethod
    def from_environment(cls):
        client_id = os.environ.get("PAYPAL_SANDBOX_CLIENT_ID", "").strip()
        client_secret = os.environ.get("PAYPAL_SANDBOX_CLIENT_SECRET", "").strip()
        return cls(client_id, client_secret) if client_id and client_secret else None

    def _request(self, path, headers, method, body=None):
        request = Request(SANDBOX_API + path, data=body, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=4) as response:
                result = json.loads(response.read(512_000))
        except HTTPError as exc:
            exc.close()
            if path == TOKEN_PATH and exc.code == 401:
                raise SandboxError("PayPal Sandbox rejected the credentials (HTTP 401). Copy the Client ID and Secret from the same Sandbox app and try again.") from None
            raise SandboxError(f"PayPal Sandbox returned HTTP {exc.code}. Check the Sandbox app or try again.") from None
        except (URLError, OSError, ValueError) as exc:
            raise SandboxError("PayPal Sandbox could not be reached or returned an unreadable response. Try again.") from exc
        if not isinstance(result, dict):
            raise SandboxError("PayPal Sandbox returned an unexpected response.")
        return result

    def _access_token(self):
        if time.monotonic() >= self._expires:
            result = self._request(TOKEN_PATH, {
                "Authorization": f"Basic {self._basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            }, "POST", b"grant_type=client_credentials")
            token = result.get("access_token")
            expires = result.get("expires_in")
            if not isinstance(token, str) or not token or not isinstance(expires, int):
                raise SandboxError("PayPal Sandbox did not provide an access token.")
            self._token = token
            self._expires = time.monotonic() + max(0, expires - 60)
        return self._token

    def check_credentials(self):
        self._access_token()

    def _order_request(self, path, method, payload=None, request_id=None):
        headers = {"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json"}
        body = None
        if request_id is not None:
            headers.update({"Content-Type": "application/json", "Prefer": "return=representation",
                            "PayPal-Request-Id": request_id})
            body = json.dumps(payload).encode()
        return self._request(path, headers, method, body)

    def create_order(self, payment, origin):
        lead = urlencode({"lead": payment["lead_id"]})
        return self._order_request(ORDER_PATH, "POST", {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": payment["create_key"],
                "description": f"UN AI demo proposal, revision {payment['revision']}",
                "amount": {"currency_code": "USD", "value": payment["amount"]},
            }],
            "payment_source": {"paypal": {"experience_context": {
                "shipping_preference": "NO_SHIPPING", "user_action": "PAY_NOW",
                "return_url": f"{origin}/?paypal_return=1&{lead}",
                "cancel_url": f"{origin}/?paypal_cancel=1&{lead}",
            }}},
        }, payment["create_key"])

    def get_order(self, order_id):
        return self._order_request(f"{ORDER_PATH}/{order_id}", "GET")

    def capture_order(self, payment):
        return self._order_request(f"{ORDER_PATH}/{payment['order_id']}/capture", "POST", {}, payment["capture_key"])

    def refund_capture(self, payment):
        capture_id = _provider_id(payment["capture_id"])
        request_id = payment["request_id"]
        return self._order_request(f"{PAYMENTS_PATH}/captures/{capture_id}/refund", "POST", {
            "amount": {"currency_code": "USD", "value": _dollars(_cents(payment["amount"]))},
            "invoice_id": request_id,
        }, request_id)

    def get_refund(self, refund_id):
        return self._order_request(f"{PAYMENTS_PATH}/refunds/{_provider_id(refund_id)}", "GET")


def _payment(db, view):
    return db.execute("SELECT * FROM paypal_payments WHERE lead_id=? AND revision=?",
                      (view["id"], view["revision"])).fetchone()


def _public(row):
    if row is None:
        return None
    fields = ("state", "revision", "quote_hash", "amount", "approval_url", "order_id", "capture_id")
    return {**{field: row[field] for field in fields}, "currency": "USD"}


def payment_status(room, lead_id):
    with room.payment_transaction(lead_id) as (db, view):
        return _public(_payment(db, view))


def _cents(amount):
    if not isinstance(amount, str) or not re.fullmatch(r"[0-9]{1,15}(?:\.[0-9]{1,2})?", amount):
        raise ValueError("A saved settlement amount needs reconciliation.")
    return int(Decimal(amount) * 100)


def _dollars(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def settlement_summary(db, view):
    """Summarize confirmed USD captures and refunds for this lead across revisions."""
    lead_id = view["id"]
    captured = {}
    for row in db.execute("SELECT amount,capture_id FROM paypal_payments WHERE lead_id=? AND state='paid'", (lead_id,)):
        amount = _cents(row["amount"])
        capture_id = row["capture_id"]
        if not amount or not isinstance(capture_id, str) or not PAYPAL_ID.fullmatch(capture_id):
            raise ValueError("Saved capture history needs reconciliation.")
        captured[capture_id] = amount

    refunded = {}
    for row in db.execute("SELECT amount,capture_id,refund_id FROM paypal_refunds WHERE lead_id=? AND state='refunded'", (lead_id,)):
        amount = _cents(row["amount"])
        capture_id, refund_id = row["capture_id"], row["refund_id"]
        if (not amount or capture_id not in captured or
                not isinstance(refund_id, str) or not PAYPAL_ID.fullmatch(refund_id)):
            raise ValueError("Saved refund history needs reconciliation.")
        refunded[capture_id] = refunded.get(capture_id, 0) + amount
        if refunded[capture_id] > captured[capture_id]:
            raise ValueError("Saved refunds exceed the captured amount. Reconcile checkout before continuing.")

    captured_total = sum(captured.values())
    refunded_total = sum(refunded.values())
    net_paid = captured_total - refunded_total
    quote_total = view["quote"]["total_usd"] * 100
    refunds = [dict(row) for row in db.execute("""
        SELECT state,amount,refund_id,revision FROM paypal_refunds
        WHERE lead_id=? AND revision=? ORDER BY created_at,capture_id
    """, (lead_id, view["revision"]))]
    return {"currency": "USD", "quote_total": _dollars(quote_total), "captured": _dollars(captured_total),
            "refunded": _dollars(refunded_total), "net_paid": _dollars(net_paid),
            "due": _dollars(max(0, quote_total - net_paid)), "credit": _dollars(max(0, net_paid - quote_total)),
            "refunds": refunds, "refund_pending": any(row["state"] in {"reserved", "pending"} for row in refunds)}


def settlement_state(room, lead_id):
    with room.payment_transaction(lead_id) as (db, view):
        return {"payment": _public(_payment(db, view)), "settlement": settlement_summary(db, view)}


def _checkout_due(db, view):
    if db.execute("""
        SELECT EXISTS (SELECT 1 FROM paypal_payments WHERE lead_id=? AND revision<>?
                       AND state IN ('capturing','pending'))
            OR EXISTS (SELECT 1 FROM paypal_refunds WHERE lead_id=? AND state IN ('reserved','pending'))
    """, (view["id"], view["revision"], view["id"])).fetchone()[0]:
        raise ValueError("Resolve the pending capture or refund before starting another checkout.")
    due = settlement_summary(db, view)["due"]
    if due == "0.00":
        raise ValueError("This proposal has no outstanding payment.")
    return due


def _require_checkout_balance(db, view, row):
    if _cents(row["amount"]) != _cents(_checkout_due(db, view)):
        raise ValueError("This checkout amount changed. Review the balance and approve a new revision.")


def _require_quote(view, revision, quote_hash):
    if type(revision) is not int or revision != view["revision"] or quote_hash != view["quote"]["hash"]:
        raise ValueError("The proposal changed. Review and approve the current revision first.")
    if view["status"] != "approved" or view["quote"]["currency"] != "USD" or not view["quote"]["demo"]:
        raise ValueError("Checkout requires a current, owner-approved USD demo proposal.")


def _provider_id(value):
    if not isinstance(value, str) or not PAYPAL_ID.fullmatch(value):
        raise SandboxError("PayPal Sandbox returned an invalid order, capture, or refund ID.")
    return value


def _checkout_link(order):
    links = order.get("links")
    if isinstance(links, list):
        for link in links:
            if isinstance(link, dict) and link.get("rel") in {"payer-action", "approve"}:
                href = link.get("href")
                if isinstance(href, str):
                    url = urlsplit(href)
                    if url.scheme == "https" and url.netloc in SANDBOX_HOSTS:
                        return href
    raise SandboxError("PayPal did not return a Sandbox checkout link.")


def _record(room, db, view, kind, **details):
    room._audit(db, view["id"], kind, {"revision": view["revision"], **details})


def create_payment(room, sandbox, lead_id, revision, quote_hash, origin):
    with room.payment_transaction(lead_id) as (db, view):
        _require_quote(view, revision, quote_hash)
        row = _payment(db, view)
        if row is None:
            amount = _checkout_due(db, view)
            db.execute("""INSERT INTO paypal_payments
                (lead_id,revision,quote_hash,amount,state,create_key,capture_key,created_at)
                VALUES(?,?,?,?,'creating',?,?,?)""",
                       (lead_id, revision, quote_hash, amount, str(uuid4()), str(uuid4()), _now()))
    with room.payment_transaction(lead_id) as (db, view):
        _require_quote(view, revision, quote_hash)
        row = _payment(db, view)
        if row["quote_hash"] != quote_hash:
            raise ValueError("This checkout belongs to different approved terms.")
        if row["state"] in {"creating", "ready"}:
            _require_checkout_balance(db, view, row)
        if row["state"] == "creating":
            if datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"]) >= timedelta(hours=5):
                raise ValueError("The checkout attempt expired. Edit and approve a new proposal revision to start again.")
            order = sandbox.create_order(row, origin)
            order_id = _provider_id(order.get("id"))
            approval_url = _checkout_link(order)
            db.execute("UPDATE paypal_payments SET state='ready',order_id=?,approval_url=? WHERE create_key=?",
                       (order_id, approval_url, row["create_key"]))
            _record(room, db, view, "paypal_order_created", order_id=order_id, amount=row["amount"], currency="USD")
        return _public(_payment(db, view))


def _matches_amount(amount, expected):
    if not isinstance(amount, dict) or amount.get("currency_code") != "USD":
        return False
    value = amount.get("value")
    return isinstance(value, str) and re.fullmatch(r"\d{1,15}(?:\.\d{1,2})?", value) and Decimal(value) == Decimal(expected)


def _order_unit(order, payment):
    units = order.get("purchase_units")
    if (order.get("id") != payment["order_id"] or not isinstance(units, list) or len(units) != 1
            or not isinstance(units[0], dict) or units[0].get("reference_id") != payment["create_key"]):
        raise SandboxError("The PayPal order did not match the saved checkout. Payment remains unconfirmed.")
    return units[0]


def _set_state(room, db, view, row, state, capture_id=None):
    if row["state"] != state or row["capture_id"] != capture_id:
        db.execute("UPDATE paypal_payments SET state=?,capture_id=? WHERE create_key=?",
                   (state, capture_id, row["create_key"]))
        _record(room, db, view, f"paypal_payment_{state}", order_id=row["order_id"], capture_id=capture_id,
                amount=row["amount"], currency="USD")
    return _public(_payment(db, view))


def _capture_result(room, db, view, row, order, unit):
    payments = unit.get("payments", {})
    if not isinstance(payments, dict):
        raise SandboxError("PayPal returned an unreadable payment record.")
    captures = payments.get("captures", [])
    if not captures:
        return None
    if (not isinstance(captures, list) or len(captures) != 1 or not isinstance(captures[0], dict)
            or not _matches_amount(captures[0].get("amount"), row["amount"])):
        raise SandboxError("PayPal did not confirm the exact checkout amount and currency.")
    capture = captures[0]
    capture_id = _provider_id(capture.get("id"))
    if capture.get("status") == "COMPLETED" and order.get("status") == "COMPLETED":
        return _set_state(room, db, view, row, "paid", capture_id)
    if capture.get("status") in {"DECLINED", "FAILED"}:
        return _set_state(room, db, view, row, "failed", capture_id)
    return _set_state(room, db, view, row, "pending", capture_id)


def capture_payment(room, sandbox, lead_id, revision, quote_hash, order_id):
    with room.payment_transaction(lead_id) as (db, view):
        _require_quote(view, revision, quote_hash)
        row = _payment(db, view)
        if row is None or row["quote_hash"] != quote_hash or not row["order_id"] or row["order_id"] != order_id:
            raise ValueError("This order is not associated with the current approved proposal.")
        if row["state"] in {"paid", "failed"}:
            return _public(row)
        if row["state"] == "ready":
            _require_checkout_balance(db, view, row)
            order = sandbox.get_order(order_id)
            unit = _order_unit(order, row)
            result = _capture_result(room, db, view, row, order, unit)
            if result is not None:
                return result
            if order.get("status") == "VOIDED":
                return _set_state(room, db, view, row, "failed")
            if order.get("status") in {"CREATED", "SAVED", "PAYER_ACTION_REQUIRED"}:
                return _public(row)
            if order.get("status") != "APPROVED" or not _matches_amount(unit.get("amount"), row["amount"]):
                raise SandboxError("PayPal has not confirmed an approved order with the expected amount.")
            _set_state(room, db, view, row, "capturing")

    # Commit the reservation before contacting PayPal. An interrupted request can then
    # be reconciled against the same order, while lead edits remain locked.
    with room.payment_transaction(lead_id) as (db, view):
        _require_quote(view, revision, quote_hash)
        row = _payment(db, view)
        if row["state"] in {"paid", "failed", "ready"}:
            return _public(row)
        order = sandbox.get_order(order_id)
        unit = _order_unit(order, row)
        result = _capture_result(room, db, view, row, order, unit)
        if result is not None:
            return result
        if order.get("status") == "VOIDED":
            return _set_state(room, db, view, row, "failed")
        if row["state"] == "pending" or order.get("status") == "COMPLETED":
            return _set_state(room, db, view, row, "pending", row["capture_id"])
        if order.get("status") in {"CREATED", "SAVED", "PAYER_ACTION_REQUIRED"}:
            return _set_state(room, db, view, row, "ready")
        if order.get("status") != "APPROVED" or not _matches_amount(unit.get("amount"), row["amount"]):
            raise SandboxError("PayPal has not confirmed an approved order with the expected amount.")
        _require_checkout_balance(db, view, row)
        order = sandbox.capture_order(row)
        unit = _order_unit(order, row)
        result = _capture_result(room, db, view, row, order, unit)
        if result is None:
            raise SandboxError("PayPal has not confirmed a capture. Check the same order again.")
        return result
