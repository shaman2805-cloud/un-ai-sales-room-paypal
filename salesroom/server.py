"""Loopback owner panel. Room agents use the CLI, never this session credential."""
from __future__ import annotations

import hmac
import json
import os
import secrets
import time
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .ai_intake import draft_scope_change
from .core import SalesRoom, _hash
from .paypal import PayPalSandbox, SandboxError, _require_quote, capture_payment, create_payment, settlement_state
from .refunds import refund_credit

ROOT = Path(__file__).resolve().parent.parent
MAX_BODY = 65536
OWNER_COOKIE = "salesroom_paypal_owner"


def owner_key(data_dir: Path) -> str:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "owner.key"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink():
            raise ValueError("Use a regular file for the owner key.")
        key = path.read_text(encoding="ascii").strip()
        if len(key) < 32:
            raise ValueError("The local owner key is invalid.")
        return key
    key = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(key + "\n")
    return key


class OwnerServer(HTTPServer):
    def __init__(self, data_dir: Path, catalog: Path, port: int = 8787):
        self.data_dir = data_dir.resolve()
        self.catalog = catalog.resolve()
        self.owner_token = owner_key(self.data_dir)
        # Persist the owner binding before accepting any client requests.
        SalesRoom(self.data_dir / "sales.sqlite3", self.catalog, self.owner_token)
        self.bootstrap_token = secrets.token_urlsafe(32)
        self.session_token = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(32)
        self.paypal = PayPalSandbox.from_environment()
        super().__init__(("127.0.0.1", port), OwnerHandler)

    def room(self) -> SalesRoom:
        return SalesRoom(self.data_dir / "sales.sqlite3", self.catalog, self.owner_token)


class OwnerHandler(BaseHTTPRequestHandler):
    server: OwnerServer

    def log_message(self, *_args):
        # Never put session capabilities, client briefs, or private URLs in logs.
        pass

    def send(self, code: int, body, kind="application/json; charset=utf-8", cookie=None):
        raw = json.dumps(body, ensure_ascii=False).encode() if isinstance(body, (dict, list)) else body
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def valid_host(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def authenticated(self) -> bool:
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
            token = jar[OWNER_COOKIE].value
        except (KeyError, ValueError):
            return False
        return hmac.compare_digest(token, self.server.session_token)

    def body(self) -> dict:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Send the request as JSON.")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= MAX_BODY:
            raise ValueError("The request size is invalid.")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("Send a JSON object.")
        return value

    def scope_change(self, room, lead_id, action, data):
        request = data.get("request")
        if not isinstance(request, str) or not 0 < len(request.strip()) <= 2000:
            raise ValueError("Enter a fictional change request of up to 2,000 characters.")
        request = request.strip()
        revision, quote_hash = data["revision"], data["quote_hash"]
        binding = (lead_id, revision, quote_hash, _hash(request))
        if action == "apply-change":
            cached = getattr(self.server, "scope_draft", None)
            if not cached or cached[:2] != (data.get("draft_id"), binding) or time.monotonic() >= cached[3]:
                raise ValueError("This draft is no longer current. Generate a new scope draft.")
            return room.update_lead(lead_id, f"scope-draft-{cached[0]}", cached[2],
                                    expected_revision=revision, expected_quote_hash=quote_hash)

        before = room.get_lead(lead_id)
        _require_quote(before, revision, quote_hash)
        draft = draft_scope_change(request, before["brief"]["services"])
        before, after = room.preview_scope_change(lead_id, revision, quote_hash, draft["services"])
        old = {line["service_id"]: line for line in before["quote"]["lines"]}
        new = {line["service_id"]: line for line in after["quote"]["lines"]}
        changes = []
        for service_id, line in {**old, **new}.items():
            original = old.get(service_id, {}).get("quantity", 0)
            revised = new.get(service_id, {}).get("quantity", 0)
            if original != revised:
                changes.append({"name": line["name"], "from": original, "to": revised})

        draft_id = None
        self.server.scope_draft = None
        if changes and not draft["questions"]:
            draft_id = secrets.token_urlsafe(24)
            updated_request = f"{before['brief']['request']}\n\nChange request: {request}"
            self.server.scope_draft = (draft_id, binding, {"services": draft["services"], "request": updated_request},
                                       time.monotonic() + 900)
        return {**draft, "draft_id": draft_id, "changes": changes,
                "before_total": before["quote"]["total_usd"], "after_total": after["quote"]["total_usd"]}

    def handle_request(self, post=False):
        if not self.valid_host():
            self.send(403, {"error": "The owner panel address is invalid."})
            return
        path = urlsplit(self.path).path
        if not post and path == "/":
            self.send(200, (ROOT / "web/index.html").read_bytes(), "text/html; charset=utf-8")
            return
        try:
            data = self.body() if post else {}
            if post:
                origin = self.headers.get("Origin")
                if origin != f"http://{self.headers['Host']}":
                    raise PermissionError("Open the owner panel on this computer.")
            if path == "/api/session":
                if post:
                    token = data.get("token", "")
                    if not isinstance(token, str) or not hmac.compare_digest(token, self.server.bootstrap_token):
                        raise PermissionError("Open the private link from the launcher window.")
                    # The top-level return from PayPal can recover the owner session.
                    # Every write still requires a same-origin request and CSRF token.
                    cookie = f"{OWNER_COOKIE}={self.server.session_token}; HttpOnly; SameSite=Lax; Path=/"
                    self.send(200, {"csrf": self.server.csrf_token}, cookie=cookie)
                elif self.authenticated():
                    self.send(200, {"csrf": self.server.csrf_token})
                else:
                    raise PermissionError("Open the private link from the launcher window.")
                return
            if not self.authenticated():
                raise PermissionError("An owner session is required.")
            if post and not hmac.compare_digest(self.headers.get("X-Salesroom-CSRF", ""), self.server.csrf_token):
                raise PermissionError("Refresh the owner panel.")
            if post and path == "/api/paypal/connect":
                if self.server.paypal is not None:
                    raise ValueError("PayPal Sandbox is already connected. Restart the panel to change the app.")
                keys = (data.get("client_id"), data.get("client_secret"))
                if not all(isinstance(key, str) for key in keys):
                    raise ValueError("Copy the Client ID and Secret from the same Sandbox app.")
                client_id, secret = (key.strip() for key in keys)
                if (not all(12 <= len(key) <= 512 and all(33 <= ord(char) <= 126 for char in key)
                            for key in (client_id, secret)) or ":" in client_id):
                    raise ValueError("Copy the Client ID and Secret using the copy buttons in the Sandbox app.")
                candidate = PayPalSandbox(client_id, secret)
                candidate.check_credentials()
                self.server.paypal = candidate
                self.send(200, {"paypal_enabled": True})
                return
            room = self.server.room()
            if not post and path == "/api/state":
                self.send(200, {"leads": room.list_leads(),
                                "catalog": json.loads(self.server.catalog.read_text(encoding="utf-8")),
                                "paypal_enabled": self.server.paypal is not None,
                                "switch_verified": False})
            elif post and path == "/api/leads":
                self.send(200, room.create_lead(data["event_id"], data["brief"]))
            else:
                parts = path.strip("/").split("/")
                if len(parts) not in (3, 4) or parts[:2] != ["api", "leads"]:
                    self.send(404, {"error": "Page unavailable."})
                    return
                lead_id = parts[2]
                action = parts[3] if len(parts) == 4 else "get"
                if not post and action == "get":
                    self.send(200, room.get_lead(lead_id))
                elif not post and action == "audit":
                    self.send(200, {"events": room.audit(lead_id)})
                elif not post and action == "proposal":
                    self.send(200, room.render_proposal(lead_id), "text/markdown; charset=utf-8")
                elif not post and action == "payment":
                    self.send(200, settlement_state(room, lead_id))
                elif post and action in {"draft-change", "apply-change"}:
                    self.send(200, self.scope_change(room, lead_id, action, data))
                elif post and action == "paypal-refund":
                    sandbox = self.server.paypal
                    if sandbox is None:
                        raise ValueError("Connect PayPal Sandbox before issuing a test refund.")
                    self.send(200, {"settlement": refund_credit(room, sandbox, lead_id, data["revision"], data["quote_hash"])})
                elif post and action in {"paypal-create", "paypal-capture"}:
                    sandbox = self.server.paypal
                    if sandbox is None:
                        raise ValueError("Set the PayPal Sandbox credentials before starting checkout.")
                    args = (room, sandbox, lead_id, data["revision"], data["quote_hash"])
                    if action == "paypal-create":
                        payment = create_payment(*args, f"http://{self.headers['Host']}")
                    else:
                        payment = capture_payment(*args, data["order_id"])
                    self.send(200, {"payment": payment})
                elif post and action == "update":
                    self.send(200, room.update_lead(lead_id, data["event_id"], data["changes"]))
                elif post and action == "review":
                    self.send(200, room.review(lead_id, data["event_id"]))
                elif post and action == "approve":
                    self.send(200, room.approve(lead_id, data["event_id"], data["revision"], data["quote_hash"], self.server.owner_token))
                elif post and action == "export":
                    from .__main__ import export_proposal
                    self.send(200, export_proposal(room, lead_id, self.server.data_dir))
                else:
                    self.send(404, {"error": "Action unavailable."})
        except PermissionError as exc:
            self.send(403, {"error": str(exc)})
        except (ValueError, KeyError, TypeError) as exc:
            self.send(409, {"error": str(exc)})
        except SandboxError as exc:
            self.send(502, {"error": str(exc)})
        except Exception:
            self.send(500, {"error": "The request failed. Check the local project data."})

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request(post=True)


def serve(data_dir: Path, catalog: Path, port: int, open_browser=False):
    if not 1 <= port <= 65535:
        raise ValueError("Choose a port from 1 to 65535.")
    server = OwnerServer(data_dir, catalog, port)
    url = f"http://127.0.0.1:{server.server_address[1]}/#token={server.bootstrap_token}"
    print("UN AI Sales Room · PayPal Sandbox. Prices are fictional.", flush=True)
    if server.paypal is not None:
        try:
            server.paypal.check_credentials()
        except SandboxError as exc:
            print(f"Sandbox connection check: {exc}", flush=True)
            server.paypal = None
        else:
            print("PayPal Sandbox credentials verified.", flush=True)
    if not open_browser or not webbrowser.open(url):
        print("Private owner link. Keep it out of recordings and agent chats.", flush=True)
        print(url, flush=True)
    print("Press Ctrl+C to stop the owner panel.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
