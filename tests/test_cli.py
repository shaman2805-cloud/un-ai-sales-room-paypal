"""Actual subprocesses verify persistence and the agent-facing command boundary."""
import json
import secrets
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from salesroom.core import SalesRoom

ROOT = Path(__file__).resolve().parent.parent


class CLIIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.token = secrets.token_urlsafe(32)
        self.owner = SalesRoom(self.data / "sales.sqlite3", ROOT / "salesroom/catalog.demo.json", self.token)
        self.brief = json.loads((ROOT / "examples/lead.complete.json").read_text(encoding="utf-8"))

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args, value=None):
        return subprocess.run([sys.executable, "-m", "salesroom", "--data-dir", str(self.data), *args],
                              input=json.dumps(value) if value is not None else None,
                              text=True, capture_output=True, cwd=ROOT, timeout=15)

    def test_agent_commands_cannot_approve_but_export_owner_snapshot(self):
        created = self.cli("create", "--event-id", "cli-intake", value=self.brief)
        self.assertEqual(created.returncode, 0, created.stderr)
        lead = json.loads(created.stdout)
        reviewed = self.cli("review", lead["id"], "--event-id", "cli-review")
        self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
        lead = json.loads(reviewed.stdout)
        self.assertEqual(lead["status"], "awaiting_approval")
        self.assertNotEqual(self.cli("export", lead["id"]).returncode, 0)
        self.assertEqual(self.cli("approve", lead["id"]).returncode, 2)
        self.owner.approve(lead["id"], "test-owner", lead["revision"], lead["quote"]["hash"], self.token)
        exported = self.cli("export", lead["id"])
        self.assertEqual(exported.returncode, 0, exported.stderr)
        result = json.loads(exported.stdout)
        self.assertEqual(result["quote_hash"], lead["quote"]["hash"])
        self.assertTrue(result["snapshot"])
        self.assertTrue(Path(result["path"]).is_file())
        self.assertNotIn(self.token, exported.stdout + exported.stderr)

    def test_process_replay_and_authority_fields_rejected(self):
        first = self.cli("create", "--event-id", "same-message", value=self.brief)
        second = self.cli("create", "--event-id", "same-message", value=self.brief)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(first.stdout)["id"], json.loads(second.stdout)["id"])
        self.assertEqual(len(json.loads(self.cli("list").stdout)), 1)
        forged = {**self.brief, "actor": "owner", "approved": True}
        denied = self.cli("create", "--event-id", "forged-owner", value=forged)
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual(len(self.owner.list_leads()), 1)


if __name__ == "__main__":
    unittest.main()
