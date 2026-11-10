"""Settle a reduced, owner-approved quote against verified Sandbox captures."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlsplit
from uuid import uuid4

from .core import _now
from .paypal import SandboxError, _matches_amount, _provider_id, _record, _require_quote, settlement_summary


def _reserve(room, db, view):
    lead_id, revision = view["id"], view["revision"]
    existing = db.execute("SELECT 1 FROM paypal_refunds WHERE lead_id=? AND revision=?", (lead_id, revision)).fetchone()
    if existing:
        return
    unresolved = db.execute("""
        SELECT EXISTS (SELECT 1 FROM paypal_payments WHERE lead_id=? AND state IN ('capturing','pending'))
            OR EXISTS (SELECT 1 FROM paypal_refunds WHERE lead_id=? AND state IN ('reserved','pending'))
    """, (lead_id, lead_id)).fetchone()[0]
    if unresolved:
        raise ValueError("Check the unresolved Sandbox payment or refund before refunding this proposal.")

    remaining = Decimal(settlement_summary(db, view)["credit"])
    if remaining <= 0:
        raise ValueError("The approved quote has no credit to refund.")
    returned = {}
    for row in db.execute("SELECT capture_id,amount FROM paypal_refunds WHERE lead_id=? AND state='refunded'", (lead_id,)):
        capture_id = row["capture_id"]
        returned[capture_id] = returned.get(capture_id, Decimal(0)) + Decimal(row["amount"])

    allocations = []
    captures = db.execute("SELECT capture_id,amount FROM paypal_payments WHERE lead_id=? AND state='paid' ORDER BY revision DESC", (lead_id,))
    for row in captures:
        capture_id = _provider_id(row["capture_id"])
        available = Decimal(row["amount"]) - returned.get(capture_id, Decimal(0))
        if available < 0:
            raise ValueError("A recorded refund exceeds its source capture. Check the Sandbox ledger.")
        amount = min(available, remaining)
        if amount:
            allocations.append((lead_id, revision, view["quote"]["hash"], capture_id, f"{amount:.2f}", str(uuid4()), _now()))
            remaining -= amount
        if remaining == 0:
            break
    if remaining != 0:
        raise ValueError("The recorded Sandbox captures cannot cover this credit.")

    db.executemany("""INSERT INTO paypal_refunds
        (lead_id,revision,quote_hash,capture_id,amount,request_id,created_at,state)
        VALUES(?,?,?,?,?,?,?,'reserved')""", allocations)
    _record(room, db, view, "paypal_refund_reserved", amount=f"{sum(Decimal(item[4]) for item in allocations):.2f}",
            currency="USD", captures=len(allocations))


def _verified_state(row, refund):
    refund_id = _provider_id(refund.get("id"))
    if row["refund_id"] and row["refund_id"] != refund_id:
        raise SandboxError("PayPal returned a different Sandbox refund ID. No credit was recorded.")
    if not _matches_amount(refund.get("amount"), row["amount"]):
        raise SandboxError("PayPal did not confirm the exact Sandbox refund amount and currency.")
    invoice_id = refund.get("invoice_id")
    links = refund.get("links")
    if not isinstance(links, list):
        links = []
    source_paths = [urlsplit(link["href"]).path for link in links
                    if isinstance(link, dict) and link.get("rel") == "up" and isinstance(link.get("href"), str)]
    expected_path = f"/v2/payments/captures/{row['capture_id']}"
    if (invoice_id is not None and invoice_id != row["request_id"]) or (source_paths and expected_path not in source_paths):
        raise SandboxError("PayPal did not match the refund to the original Sandbox capture.")
    status = {"COMPLETED": "refunded", "PENDING": "pending", "CANCELLED": "failed", "FAILED": "failed"}.get(refund.get("status"))
    if status is None:
        raise SandboxError("PayPal did not provide a definitive Sandbox refund status.")
    return refund_id, status


def refund_credit(room, sandbox, lead_id, revision, quote_hash):
    """Reserve all portions, then commit each provider result before the next call."""
    with room.payment_transaction(lead_id) as (db, view):
        _require_quote(view, revision, quote_hash)
        _reserve(room, db, view)

    while True:
        with room.payment_transaction(lead_id) as (db, view):
            _require_quote(view, revision, quote_hash)
            row = db.execute("""SELECT * FROM paypal_refunds
                WHERE lead_id=? AND revision=? AND state IN ('reserved','pending') ORDER BY rowid LIMIT 1""",
                             (lead_id, revision)).fetchone()
            if row is None:
                return settlement_summary(db, view)
            if row["quote_hash"] != quote_hash:
                raise ValueError("The Sandbox refund belongs to a different quote.")
            if row["refund_id"]:
                result = sandbox.get_refund(row["refund_id"])
            else:
                if datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"]) >= timedelta(days=44):
                    raise ValueError("The Sandbox refund retry window has expired. Reconcile it in PayPal before taking another action.")
                result = sandbox.refund_capture(row)
            refund_id, state = _verified_state(row, result)
            if state != row["state"] or refund_id != row["refund_id"]:
                db.execute("UPDATE paypal_refunds SET refund_id=?,state=? WHERE request_id=?", (refund_id, state, row["request_id"]))
                _record(room, db, view, f"paypal_refund_{state}", amount=row["amount"], currency="USD",
                        capture_id=row["capture_id"], refund_id=refund_id)
            if state == "failed":
                db.execute("UPDATE paypal_refunds SET state='failed' WHERE lead_id=? AND revision=? AND state='reserved'", (lead_id, revision))
            settlement = settlement_summary(db, view)
            if state != "refunded" or not settlement["refund_pending"]:
                return settlement
