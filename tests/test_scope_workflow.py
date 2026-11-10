import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from salesroom.core import SalesRoom
from salesroom.server import OwnerHandler, ROOT


class ScopeWorkflowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.room = SalesRoom(Path(directory.name) / "sales.sqlite3", owner_token="test-owner")
        brief = json.loads((ROOT / "examples/lead.complete.json").read_text())
        lead = self.room.create_lead("create", brief)
        self.lead_id = lead["id"]
        self.room.review(self.lead_id, "review")
        self.room.approve(self.lead_id, "approve", 1, lead["quote"]["hash"], "test-owner")
        self.request = "Добавьте ещё одну форму заявки, please. Лендинг сол күйінде қалсын."
        self.payload = {"revision": 1, "quote_hash": lead["quote"]["hash"], "request": self.request}
        self.handler = object.__new__(OwnerHandler)
        self.handler.server = SimpleNamespace()

    def draft(self, questions=None):
        if questions:
            services = self.room.get_lead(self.lead_id)["brief"]["services"]
        else:
            services = [{"service_id": "landing_page", "quantity": 1}, {"service_id": "lead_form", "quantity": 2}]
        with patch("salesroom.server.draft_scope_change", return_value={
            "services": services, "evidence": ["Добавьте ещё одну форму заявки"], "questions": questions or [],
        }):
            return self.handler.scope_change(self.room, self.lead_id, "draft-change", self.payload)

    def apply(self, draft, **changes):
        return self.handler.scope_change(self.room, self.lead_id, "apply-change",
                                         {**self.payload, "draft_id": draft["draft_id"], **changes})

    def test_preview_does_not_mutate_and_apply_requires_new_approval(self):
        draft = self.draft()
        self.assertEqual((draft["before_total"], draft["after_total"]), (150, 180))
        self.assertEqual((draft["changes"][0]["from"], draft["changes"][0]["to"]), (1, 2))
        self.assertEqual(self.room.get_lead(self.lead_id)["revision"], 1)
        after = self.apply(draft)
        self.assertEqual((after["revision"], after["quote"]["total_usd"], after["status"]), (2, 180, "needs_review"))
        self.assertIsNone(after["approval"])
        self.assertIn(self.request, after["brief"]["request"])
        self.assertEqual(self.apply(draft)["revision"], 2)

    def test_changed_input_and_stale_preview_are_rejected(self):
        draft = self.draft()
        with self.assertRaises(ValueError):
            self.apply(draft, request="Add three forms instead.")
        self.room.update_lead(self.lead_id, "another-edit", {"budget_usd": 410})
        with self.assertRaises(ValueError):
            self.apply(draft)

    def test_unresolved_question_cannot_create_a_revision(self):
        draft = self.draft(questions=["How many forms are needed?"])
        self.assertIsNone(draft["draft_id"])
        with self.assertRaises(ValueError):
            self.apply(draft)
        self.assertEqual(self.room.get_lead(self.lead_id)["revision"], 1)
