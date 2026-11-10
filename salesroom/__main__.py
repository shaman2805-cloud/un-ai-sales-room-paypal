"""Agent-facing CLI and a separate owner-only local approval surface."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import SalesRoom

ROOT = Path(__file__).resolve().parent.parent


def export_proposal(room: SalesRoom, lead_id: str, data_dir: Path) -> dict:
    # One engine transaction covers approval, snapshot rendering and file write.
    result = room.export_proposal(lead_id, data_dir / "proposals")
    return {**result, "download_url": f"/api/leads/{lead_id}/proposal"}


def read_input(filename: str) -> dict:
    raw = sys.stdin.read() if filename == "-" else Path(filename).read_text(encoding="utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Input must be a JSON object.")
    return value


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="UN AI Sales Room local demo")
    p.add_argument("--data-dir", type=Path, default=ROOT / "runtime")
    p.add_argument("--catalog", type=Path, default=ROOT / "salesroom/catalog.demo.json")
    sub = p.add_subparsers(dest="command", required=True)
    for command in ("create", "update"):
        s = sub.add_parser(command)
        if command == "update":
            s.add_argument("lead_id")
        s.add_argument("--event-id", required=True)
        s.add_argument("--input", default="-", help="JSON file or - for stdin")
    for command in ("get", "review", "audit", "export"):
        s = sub.add_parser(command)
        s.add_argument("lead_id")
        if command == "review":
            s.add_argument("--event-id", required=True)
    sub.add_parser("list")
    s = sub.add_parser("serve", help="Start the private owner panel separately from the agents")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--open", action="store_true", help="Open the private owner panel in your browser")
    return p


def main() -> int:
    args = parser().parse_args()
    try:
        args.data_dir.mkdir(parents=True, exist_ok=True)
        if args.command == "serve":
            from .server import serve
            serve(args.data_dir, args.catalog, args.port, args.open)
            return 0
        # Agent commands deliberately do not read owner.key or offer an approve command.
        room = SalesRoom(args.data_dir / "sales.sqlite3", args.catalog)
        if args.command == "create":
            result = room.create_lead(args.event_id, read_input(args.input))
        elif args.command == "update":
            result = room.update_lead(args.lead_id, args.event_id, read_input(args.input))
        elif args.command == "get":
            result = room.get_lead(args.lead_id)
        elif args.command == "review":
            result = room.review(args.lead_id, args.event_id)
        elif args.command == "list":
            result = room.list_leads()
        elif args.command == "audit":
            result = room.audit(args.lead_id)
        else:
            result = export_proposal(room, args.lead_id, args.data_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, PermissionError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
