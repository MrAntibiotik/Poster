"""Невдалі публікації: причина, повтор зараз / у час / у вільний слот, ігнорування."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


class Flaky:
    def __init__(self, fail_times):
        self.left = fail_times

    def publish(self, path, meta):
        if self.left > 0:
            self.left -= 1
            raise RuntimeError("Graph API HTTP 400: ProcessingFailedError")
        return {"id": "m1", "url": "https://ig/m1"}


class Ok:
    def publish(self, path, meta):
        return {"id": "y1", "url": "https://yt/y1"}


class RetryTest(FlowBase):
    def setUp(self):
        super().setUp()
        self.cfg["dry_run"] = False
        self.ig = Flaky(3)
        self.app.platforms = {"youtube": Ok(), "instagram": self.ig}
        self.make_short("a")
        self.make_short("b", age=110)
        self.app.scheduler.tick(self.ts(11, "12:00"))
        self.item = self.app.db.items("status='notified'")[0]
        self.press(f"pub:{self.item['id']}", self.item)
        self.app.scheduler.tick(self.ts(11, "13:00"))  # YouTube ок, Instagram — 3 невдалі спроби

    def button(self, data):
        self.app.bot.handle({"update_id": 1, "callback_query": {
            "id": "c", "data": data, "message": {"message_id": 999, "chat": {"id": 42}, "text": ""}}})

    def say(self, text):
        self.app.bot.handle({"update_id": 1, "message": {"text": text, "chat": {"id": 42, "type": "private"}}})

    def test_summary_and_retry_now(self):
        it = self.app.db.get_item(self.item["id"])
        self.assertEqual(it["status"], "posted")
        summary = [s for s in self.app.bot.tg.sent if s[0] == "text" and "Не опубліковано" in s[1]][-1]
        self.assertIn("ProcessingFailedError", summary[1])
        self.assertIn("rf:", json.dumps(summary[2]))
        self.assertIn("rf:", json.dumps(self.app.bot.tg.edits[-1][2]))  # кнопки й під самим відео
        self.say("⚠️ Невдалі")
        self.assertTrue(any("Невдалі публікації: 1" in s[1] for s in self.app.bot.tg.sent if s[0] == "text"))
        self.button(f"rf:{self.item['id']}")  # тепер Instagram проходить
        it = self.app.db.get_item(self.item["id"])
        self.assertEqual(it["results"]["instagram"]["status"], "ok")
        self.assertEqual(it["results"]["youtube"]["id"], "y1")  # YouTube не дублюється
        self.assertEqual(self.app.scheduler.failed_items(), [])

    def test_retry_in_free_slot(self):
        self.button(f"rq:{self.item['id']}")
        when = self.app.scheduler.retry_at(self.item["id"])
        config.FAKE_NOW = self.ts(11, "13:01")
        self.assertEqual(config.fmt_when(self.cfg, when, self.ts(11, "13:01")), "сьогодні 16:00")
        # слот зайнято повтором — черга (b) іде далі
        plan = self.app.scheduler.projection(self.ts(11, "13:01"))
        self.assertEqual(config.fmt_when(self.cfg, plan[0][1], self.ts(11, "13:01")), "сьогодні 19:00")
        self.app.scheduler.tick(self.ts(11, "16:00"))
        self.assertEqual(self.app.db.get_item(self.item["id"])["results"]["instagram"]["status"], "ok")

    def test_retry_custom_time_and_ignore(self):
        self.button(f"rt:{self.item['id']}")
        self.assertIn("Коли повторити", self.app.bot.tg.sent[-1][1])
        self.button(f"tc:{self.item['id']}")
        self.say("сьогодні 18:20")
        self.assertEqual(self.app.scheduler.retry_at(self.item["id"]), self.ts(11, "18:20"))
        self.button(f"ri:{self.item['id']}")
        self.assertIsNone(self.app.scheduler.retry_at(self.item["id"]))
        self.assertEqual(self.app.scheduler.failed_items(), [])
        self.say("/failed")
        self.assertIn("Невдалих публікацій немає", self.app.bot.tg.sent[-1][1])


if __name__ == "__main__":
    unittest.main()
