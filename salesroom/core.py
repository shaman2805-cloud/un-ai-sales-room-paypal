"""Transactional sales state; client and agent text never grants owner authority.

Brief schema: client_name/request/deadline (ISO date) are strings;
budget_usd is a positive integer; services is [{service_id: str, quantity: int}].
Optional contact is a string and assumptions is a list of strings. Missing fields
are accepted as an incomplete lead and block review. Unknown fields and malformed
values are rejected. Demo catalog values are fictional, not UN AI's real prices.

Owner capability is bound once in the database. Agents use owner_token=None.
This is an application boundary on a trusted local device, not OS isolation from
an actor who can edit the database or read the owner's private capability file.
"""

from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import hmac
import json
import os
import tempfile
from pathlib import Path
import sqlite3
import uuid


DEMO_SERVICE_LABELS = {
    "landing_page": "Demo landing page",
    "lead_form": "Demo lead form",
    "whatsapp_bot": "WhatsApp bot",
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _positive_integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 10**12


class SalesRoom:
    def __init__(self, db_path, catalog_path=None, owner_token=None):
        self.db_path = str(db_path)
        if self.db_path == ":memory:":
            raise ValueError("Use a file database for persistent state")
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.catalog_path = Path(catalog_path) if catalog_path else Path(__file__).with_name("catalog.demo.json")
        self._owner_enabled = owner_token is not None
        if owner_token is not None and (not isinstance(owner_token, str) or not owner_token):
            raise ValueError("Owner capability must be a nonempty string")
        with self._tx() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS leads(
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL, brief TEXT NOT NULL,
                    catalog TEXT NOT NULL, catalog_hash TEXT NOT NULL,
                    review TEXT, approval TEXT, rendered_quote_hash TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions(
                    lead_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    brief TEXT NOT NULL, catalog TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(lead_id,revision));
                CREATE TABLE IF NOT EXISTS events(
                    event_id TEXT PRIMARY KEY, action TEXT NOT NULL, payload_hash TEXT NOT NULL,
                    lead_id TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT NOT NULL,
                    kind TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS paypal_payments(
                    lead_id TEXT NOT NULL, revision INTEGER NOT NULL, quote_hash TEXT NOT NULL,
                    amount TEXT NOT NULL, state TEXT NOT NULL, create_key TEXT NOT NULL UNIQUE,
                    capture_key TEXT NOT NULL UNIQUE, order_id TEXT UNIQUE, approval_url TEXT,
                    capture_id TEXT UNIQUE, created_at TEXT NOT NULL, PRIMARY KEY(lead_id,revision));
                CREATE TABLE IF NOT EXISTS paypal_refunds(
                    lead_id TEXT NOT NULL, revision INTEGER NOT NULL, quote_hash TEXT NOT NULL,
                    capture_id TEXT NOT NULL, amount TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL, refund_id TEXT UNIQUE, created_at TEXT NOT NULL,
                    PRIMARY KEY(lead_id,revision,capture_id));
            """)
            # executescript commits; explicitly reacquire a writer lock before binding.
            db.execute("BEGIN IMMEDIATE")
            if owner_token is not None:
                capability_hash = hashlib.sha256(owner_token.encode()).hexdigest()
                row = db.execute("SELECT value FROM settings WHERE key='owner_capability_sha256'").fetchone()
                if row and not hmac.compare_digest(row["value"], capability_hash):
                    raise PermissionError("Owner capability is already bound; rebinding denied")
                db.execute("INSERT OR IGNORE INTO settings VALUES('owner_capability_sha256',?)", (capability_hash,))

    @contextmanager
    def _tx(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def payment_transaction(self, lead_id):
        """Share the revision lock with the owner checkout ledger."""
        if not self._owner_enabled:
            raise PermissionError("An owner session is required for checkout.")
        with self._tx() as db:
            view = self._view(self._row(db, lead_id, self._catalog()))
            yield db, view

    def _catalog(self):
        try:
            catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("Catalog unavailable; cannot quote or approve") from error
        if not isinstance(catalog, dict) or not isinstance(catalog.get("version"), str) or not catalog["version"]:
            raise ValueError("Catalog needs a version")
        if catalog.get("currency") != "USD" or not isinstance(catalog.get("demo"), bool):
            raise ValueError("Catalog needs USD currency and explicit demo marker")
        if not isinstance(catalog.get("services"), list):
            raise ValueError("Catalog services must be a list")
        identifiers = set()
        for item in catalog["services"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                raise ValueError("Catalog has invalid service")
            if item["id"] in identifiers:
                raise ValueError("Catalog service IDs must be unique")
            identifiers.add(item["id"])
            if not isinstance(item.get("available"), bool) or not isinstance(item.get("name"), str):
                raise ValueError("Catalog service needs name and availability")
            if item.get("unit_price_usd") is not None and not _positive_integer(item["unit_price_usd"]):
                raise ValueError("Catalog prices must be positive integer USD")
        return catalog

    @staticmethod
    def _brief(brief):
        allowed = {"client_name", "request", "budget_usd", "deadline", "services", "contact", "assumptions"}
        if not isinstance(brief, dict) or set(brief) - allowed:
            raise ValueError("Brief contains unknown fields or is not an object")
        brief = json.loads(_json(brief))
        for key in ("client_name", "request", "deadline", "contact"):
            if key in brief and brief[key] is not None:
                if not isinstance(brief[key], str) or len(brief[key]) > 10000:
                    raise ValueError(f"{key} must be a string up to 10000 characters")
                brief[key] = brief[key].strip()
        if brief.get("budget_usd") is not None and not _positive_integer(brief["budget_usd"]):
            raise ValueError("budget_usd must be a positive integer")
        if brief.get("deadline"):
            try:
                date.fromisoformat(brief["deadline"])
            except ValueError as error:
                raise ValueError("deadline must be an ISO date YYYY-MM-DD") from error
        if "services" in brief and brief["services"] is not None:
            if not isinstance(brief["services"], list) or len(brief["services"]) > 50:
                raise ValueError("services must be a list of at most 50 items")
            identifiers = set()
            for item in brief["services"]:
                if not isinstance(item, dict) or set(item) != {"service_id", "quantity"}:
                    raise ValueError("Service needs only service_id and quantity")
                if not isinstance(item["service_id"], str) or not item["service_id"].strip():
                    raise ValueError("service_id must be nonempty")
                if item["service_id"] in identifiers or not _positive_integer(item["quantity"]):
                    raise ValueError("Duplicate service or invalid positive integer quantity")
                identifiers.add(item["service_id"])
        if "assumptions" in brief and (not isinstance(brief["assumptions"], list) or
                len(brief["assumptions"]) > 50 or any(not isinstance(x, str) or len(x) > 2000 for x in brief["assumptions"])):
            raise ValueError("assumptions must be a list of at most 50 short strings")
        return brief

    @staticmethod
    def _event_id(event_id):
        if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 200:
            raise ValueError("event_id must be nonempty and at most 200 characters")

    def _seen(self, db, event_id, action, payload):
        self._event_id(event_id)
        row = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if row and (row["action"] != action or row["payload_hash"] != _hash(payload)):
            raise ValueError("Idempotency collision: event_id already used for different data")
        return row["lead_id"] if row else None

    @staticmethod
    def _event(db, event_id, action, payload, lead_id):
        db.execute("INSERT INTO events VALUES(?,?,?,?,?)", (event_id, action, _hash(payload), lead_id, _now()))

    @staticmethod
    def _audit(db, lead_id, kind, details):
        db.execute("INSERT INTO audit(lead_id,kind,details,created_at) VALUES(?,?,?,?)",
                   (lead_id, kind, _json(details), _now()))

    @staticmethod
    def _snapshot(db, row):
        db.execute("INSERT INTO revisions VALUES(?,?,?,?,?)",
                   (row["id"], row["revision"], row["brief"], row["catalog"], _now()))

    @staticmethod
    def _settlement_unresolved(db, row):
        key = (row["id"], row["revision"])
        return db.execute("""
            SELECT EXISTS (SELECT 1 FROM paypal_payments WHERE lead_id=? AND revision=?
                           AND state IN ('capturing','pending'))
                OR EXISTS (SELECT 1 FROM paypal_refunds WHERE lead_id=? AND revision=?
                           AND state IN ('reserved','pending'))
        """, (*key, *key)).fetchone()[0]

    def _row(self, db, lead_id, catalog):
        row = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not row:
            raise ValueError("Lead not found")
        catalog_hash = _hash(catalog)
        if row["catalog_hash"] != catalog_hash:
            if self._settlement_unresolved(db, row):
                # Reconcile the outstanding operation against the approved snapshot first.
                return row
            old_revision = row["revision"]
            db.execute("UPDATE leads SET revision=revision+1,catalog=?,catalog_hash=?,review=NULL,approval=NULL,"
                       "rendered_quote_hash=NULL,updated_at=? WHERE id=?",
                       (_json(catalog), catalog_hash, _now(), lead_id))
            row = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
            self._snapshot(db, row)
            self._audit(db, lead_id, "catalog_changed", {"from_revision": old_revision,
                        "revision": row["revision"], "catalog_version": catalog["version"], "catalog_hash": catalog_hash})
        return row

    @staticmethod
    def _view(row):
        brief, catalog = json.loads(row["brief"]), json.loads(row["catalog"])
        blockers = []
        for key, question in (("client_name", "What is the client's name?"),
                              ("request", "What does the client need?"),
                              ("budget_usd", "What is the client's budget in USD?"),
                              ("deadline", "What deadline did the client request (YYYY-MM-DD)?")):
            if not brief.get(key):
                blockers.append(question)
        if not brief.get("services"):
            blockers.append("Which services has the owner confirmed?")
        services = {item["id"]: item for item in catalog["services"]}
        lines = []
        for requested in brief.get("services") or []:
            item = services.get(requested["service_id"])
            if not item:
                blockers.append(f"The confirmed catalogue does not include {requested['service_id']}.")
            elif not item["available"] or item.get("unit_price_usd") is None:
                name = DEMO_SERVICE_LABELS.get(item["id"], item["name"]) if catalog["demo"] else item["name"]
                blockers.append(f"The owner needs to confirm availability and pricing for {name}.")
            else:
                lines.append({"service_id": item["id"], "name": item["name"], "quantity": requested["quantity"],
                              "unit_price_usd": item["unit_price_usd"],
                              "total_usd": item["unit_price_usd"] * requested["quantity"]})
        total = sum(item["total_usd"] for item in lines)
        if brief.get("budget_usd") and total > brief["budget_usd"]:
            blockers.append(f"The quote of {total} USD exceeds the {brief['budget_usd']} USD budget.")
        quote = {"lines": lines, "total_usd": total, "currency": "USD", "catalog_version": catalog["version"],
                 "catalog_hash": row["catalog_hash"], "demo": catalog["demo"]}
        quote["hash"] = _hash({"lead_id": row["id"], "revision": row["revision"], "brief": brief, "quote": quote})
        review = json.loads(row["review"]) if row["review"] else None
        approval = json.loads(row["approval"]) if row["approval"] else None
        status = "blocked" if blockers else "needs_review"
        if not blockers and review and review["passed"] and review["quote_hash"] == quote["hash"]:
            status = "awaiting_approval"
            if approval and approval["quote_hash"] == quote["hash"] and approval["revision"] == row["revision"]:
                status = "approved"
        return {"id": row["id"], "revision": row["revision"], "brief": brief, "quote": quote,
                "blockers": blockers, "status": status, "review": review, "approval": approval,
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def create_lead(self, event_id, brief):
        brief = self._brief(brief)
        with self._tx() as db:
            catalog = self._catalog()
            previous = self._seen(db, event_id, "create", brief)
            if previous:
                return self._view(self._row(db, previous, catalog))
            lead_id, now = uuid.uuid4().hex, _now()
            db.execute("INSERT INTO leads VALUES(?,1,?,?,?,NULL,NULL,NULL,?,?)",
                       (lead_id, _json(brief), _json(catalog), _hash(catalog), now, now))
            row = self._row(db, lead_id, catalog)
            self._snapshot(db, row)
            self._event(db, event_id, "create", brief, lead_id)
            self._audit(db, lead_id, "lead_created", {"revision": 1, "catalog_version": catalog["version"], "event_id": event_id})
            return self._view(row)

    @staticmethod
    def _require_approved_revision(view, revision, quote_hash):
        if (type(revision) is not int or view["revision"] != revision or
                view["quote"]["hash"] != quote_hash or view["status"] != "approved"):
            raise ValueError("The approved proposal changed. Refresh and review the current revision.")

    def preview_scope_change(self, lead_id, revision, quote_hash, services):
        changes = self._brief({"services": services})
        with self._tx() as db:
            row = self._row(db, lead_id, self._catalog())
            before = self._view(row)
            self._require_approved_revision(before, revision, quote_hash)
            brief = self._brief({**before["brief"], **changes})
            if _json(brief) == row["brief"]:
                return before, before
            draft = {**dict(row), "brief": _json(brief), "revision": row["revision"] + 1,
                     "review": None, "approval": None}
            return before, self._view(draft)

    def update_lead(self, lead_id, event_id, changes, *, expected_revision=None, expected_quote_hash=None):
        changes = self._brief(changes)
        payload = {"lead_id": lead_id, "changes": changes}
        guarded = expected_revision is not None or expected_quote_hash is not None
        if guarded:
            payload.update(expected_revision=expected_revision, expected_quote_hash=expected_quote_hash)
        with self._tx() as db:
            catalog = self._catalog()
            previous = self._seen(db, event_id, "update", payload)
            row = self._row(db, lead_id, catalog)
            if not previous:
                if guarded:
                    self._require_approved_revision(self._view(row), expected_revision, expected_quote_hash)
                brief = self._brief({**json.loads(row["brief"]), **changes})
                if _json(brief) != row["brief"]:
                    if self._settlement_unresolved(db, row):
                        raise ValueError("Refresh checkout in the owner panel to resolve the pending capture or refund before editing this request.")
                    db.execute("UPDATE leads SET revision=revision+1,brief=?,review=NULL,approval=NULL,"
                               "rendered_quote_hash=NULL,updated_at=? WHERE id=?", (_json(brief), _now(), lead_id))
                    row = self._row(db, lead_id, catalog)
                    self._snapshot(db, row)
                    self._audit(db, lead_id, "lead_updated", {"revision": row["revision"], "changed_fields": sorted(changes), "event_id": event_id})
                self._event(db, event_id, "update", payload, lead_id)
            return self._view(row)

    def get_lead(self, lead_id):
        with self._tx() as db:
            return self._view(self._row(db, lead_id, self._catalog()))

    def list_leads(self):
        with self._tx() as db:
            catalog = self._catalog()
            identifiers = [r["id"] for r in db.execute("SELECT id FROM leads ORDER BY created_at,id")]
            return [self._view(self._row(db, identifier, catalog)) for identifier in identifiers]

    def review(self, lead_id, event_id):
        payload = {"lead_id": lead_id}
        with self._tx() as db:
            previous = self._seen(db, event_id, "review", payload)
            row = self._row(db, lead_id, self._catalog())
            view = self._view(row)
            if not previous:
                result = {"passed": not view["blockers"], "revision": row["revision"],
                          "quote_hash": view["quote"]["hash"], "blockers": view["blockers"], "checked_at": _now()}
                db.execute("UPDATE leads SET review=?,updated_at=? WHERE id=?", (_json(result), _now(), lead_id))
                self._event(db, event_id, "review", payload, lead_id)
                self._audit(db, lead_id, "review_completed", {**result, "event_id": event_id})
                row = self._row(db, lead_id, self._catalog())
            return self._view(row)

    def approve(self, lead_id, event_id, revision, quote_hash, owner_token):
        if not self._owner_enabled or not isinstance(owner_token, str):
            raise PermissionError("Human owner capability required")
        if not isinstance(revision, int) or isinstance(revision, bool) or not isinstance(quote_hash, str):
            raise ValueError("Explicit revision and quote_hash required")
        payload = {"lead_id": lead_id, "revision": revision, "quote_hash": quote_hash}
        with self._tx() as db:
            bound = db.execute("SELECT value FROM settings WHERE key='owner_capability_sha256'").fetchone()
            supplied = hashlib.sha256(owner_token.encode()).hexdigest()
            if not bound or not hmac.compare_digest(bound["value"], supplied):
                raise PermissionError("Human owner capability required")
            previous = self._seen(db, event_id, "approve", payload)
            row = self._row(db, lead_id, self._catalog())
            view = self._view(row)
            if not previous:
                if row["revision"] != revision or view["quote"]["hash"] != quote_hash:
                    raise ValueError("Stale approval: review the current revision and quote")
                if view["status"] not in {"awaiting_approval", "approved"}:
                    raise ValueError("Approval requires a successful current review without blockers")
                approval = {"revision": revision, "quote_hash": quote_hash, "approved_at": _now(), "by": "human_owner"}
                db.execute("UPDATE leads SET approval=?,updated_at=? WHERE id=?", (_json(approval), _now(), lead_id))
                self._event(db, event_id, "approve", payload, lead_id)
                self._audit(db, lead_id, "owner_approved", {**approval, "event_id": event_id})
                row = self._row(db, lead_id, self._catalog())
            return self._view(row)

    def audit(self, lead_id):
        with self._tx() as db:
            self._row(db, lead_id, self._catalog())
            return [{"sequence": row["sequence"], "kind": row["kind"], "details": json.loads(row["details"]),
                     "created_at": row["created_at"]}
                    for row in db.execute("SELECT * FROM audit WHERE lead_id=? ORDER BY sequence", (lead_id,))]

    def render_proposal(self, lead_id):
        with self._tx() as db:
            return self._render(db, self._row(db, lead_id, self._catalog()))

    def export_proposal(self, lead_id, output_dir):
        """Publish a versioned snapshot while concurrent lead mutations wait.

        Export metadata always names the approved revision/hash. A subsequent
        update can make that artifact historical; current downloads revalidate.
        """
        staged = None
        try:
            with self._tx() as db:
                row = self._row(db, lead_id, self._catalog())
                view = self._view(row)
                markdown = self._render(db, row)
                output_dir = Path(output_dir)
                output_dir.mkdir(parents=True, exist_ok=True)
                target = output_dir / f"{row['id']}-v{row['revision']}.md"
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output_dir,
                                                 prefix=".proposal-", suffix=".tmp", delete=False) as stream:
                    staged = Path(stream.name)
                    stream.write(markdown)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Catalog files may be edited outside SQLite: check their content
                # again immediately before publication, not only at initial read.
                if _hash(self._catalog()) != row["catalog_hash"]:
                    raise ValueError("Catalog changed during export; review the current revision")
                os.replace(staged, target)
                staged = None
                prior = db.execute("SELECT details FROM audit WHERE lead_id=? AND kind='proposal_exported'",
                                   (lead_id,)).fetchall()
                if not any(json.loads(item["details"])["quote_hash"] == view["quote"]["hash"] for item in prior):
                    self._audit(db, lead_id, "proposal_exported", {"revision": row["revision"],
                                "quote_hash": view["quote"]["hash"], "filename": target.name})
                return {"filename": target.name, "path": str(target), "revision": row["revision"],
                        "quote_hash": view["quote"]["hash"], "snapshot": True}
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)

    def _render(self, db, row):
        view = self._view(row)
        if view["status"] != "approved":
            raise ValueError("Current reviewed owner approval required before proposal export")
        quote_hash = view["quote"]["hash"]
        if row["rendered_quote_hash"] != quote_hash:
            language = "en"
            self._audit(db, row["id"], "proposal_rendered", {
                "revision": view["revision"], "quote_hash": quote_hash, "language": language,
            })
            db.execute("UPDATE leads SET rendered_quote_hash=? WHERE id=?", (quote_hash, row["id"]))
        else:
            # Old render events have no language. Keep their saved proposals reproducible.
            previous = db.execute("SELECT details FROM audit WHERE lead_id=? AND kind='proposal_rendered' "
                                  "ORDER BY sequence DESC LIMIT 1", (row["id"],)).fetchone()
            language = json.loads(previous["details"]).get("language") if previous else None

        def literal(value):
            # Keep client links/images/instructions as literal quoted data.
            value = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            value = value.replace("\\", "\\\\")
            for character in "`*_{}[]()#+-!|":
                value = value.replace(character, "\\" + character)
            return value.replace("\r", "").replace("\n", "\n> ")

        if language == "en":
            brief, quote = view["brief"], view["quote"]
            marker = "Fictional demo pricing." if quote["demo"] else "Pricing uses the owner's catalogue."
            parts = ["# Proposal", "", marker, "", f"Lead: {row['id']}", f"Revision: {view['revision']}",
                     f"Catalogue: {quote['catalog_version']}", f"Quote hash: {quote_hash}", "", "## Client and request", "",
                     "> " + literal(brief["client_name"]), "", "> " + literal(brief["request"]), "",
                     "## Confirmed scope", ""]
            for item in quote["lines"]:
                name = DEMO_SERVICE_LABELS.get(item["service_id"], item["name"]) if quote["demo"] else item["name"]
                parts.append(f"- {literal(name)}: {item['quantity']} × {item['unit_price_usd']} USD = {item['total_usd']} USD")
            parts += ["", f"Total: **{quote['total_usd']} USD**.", f"Client budget: {brief['budget_usd']} USD.",
                      f"Requested deadline: {brief['deadline']}. Confirm team availability separately.", "", "## Terms", "",
                      "Changes require a fresh review and owner approval.",
                      "This scope covers the listed work. External integrations, client messaging, payments, and sales guarantees are outside it."]
            if brief.get("assumptions"):
                parts += ["", "Client assumptions to verify:"]
                parts += ["> " + literal(item) for item in brief["assumptions"]]
            parts += ["", "Owner approval: " + view["approval"]["approved_at"], ""]
            return "\n".join(parts)

        marker = "ДЕМОНСТРАЦИЯ — вымышленные цены, не действующий прайс UN AI." if view["quote"]["demo"] else "Расчёт по предоставленному владельцем каталогу."
        parts = ["# Коммерческое предложение", "", marker, "", f"Заявка: {row['id']}",
                 f"Версия: {view['revision']}", f"Каталог: {view['quote']['catalog_version']}",
                 f"Контрольная сумма: {view['quote']['hash']}", "", "## Клиент и задача", "",
                 "> " + literal(view["brief"]["client_name"]), "", "> " + literal(view["brief"]["request"]),
                 "", "## Подтверждённый состав работ", ""]
        for item in view["quote"]["lines"]:
            parts.append(f"- {literal(item['name'])}: {item['quantity']} × {item['unit_price_usd']} USD = {item['total_usd']} USD")
        parts += ["", f"Итого: **{view['quote']['total_usd']} USD**.", f"Бюджет клиента: {view['brief']['budget_usd']} USD.",
                  f"Запрошенный срок: {view['brief']['deadline']}; доступность команды требует отдельного подтверждения.",
                  "", "## Условия", "", "Подтверждена указанная версия расчёта. Изменение условий требует новой проверки и утверждения.",
                  "Внешние интеграции, сообщения клиентам и оплата не подключены. Гарантии продаж или экономии не заявляются."]
        if view["brief"].get("assumptions"):
            parts += ["", "Допущения из заявки (данные клиента, не подтверждение владельца):"]
            parts += ["> " + literal(item) for item in view["brief"]["assumptions"]]
        parts += ["", "Утверждено владельцем: " + view["approval"]["approved_at"], ""]
        return "\n".join(parts)
