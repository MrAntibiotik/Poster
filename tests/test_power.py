"""Пропущене підтвердження (wait/auto) і сон між слотами."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import power  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


class MissedApprovalTest(FlowBase):
    def setUp(self):
        super().setUp()
        self.make_short("a")
        self.sch = self.app.scheduler
        self.sch.tick(self.ts(11, "09:00"))  # a → слот 10:00, сповіщено
        self.item = self.app.db.items("status='notified'")[0]

    def button(self, data):
        own = self.app.db.tg_msg(self.item["id"], 42)
        self.app.bot.handle({"update_id": 1, "callback_query": {
            "id": "c", "data": data, "message": {"message_id": own["msg_id"], "chat": {"id": 42}, "text": ""}}})

    def test_wait_then_publish_right_after_approve(self):
        self.cfg["on_missed_approval"] = "wait"
        self.sch.tick(self.ts(11, "10:00"))
        it = self.app.db.get_item(self.item["id"])
        self.assertEqual(it["status"], "notified")  # не пропущено
        self.assertIn("минув — чекаю", self.app.bot.tg.edits[-1][1])
        self.assertEqual(self.sch.busy_reason(self.ts(11, "11:30")), "шортс чекає на підтвердження або публікується")
        self.sch.tick(self.ts(11, "11:30"))
        self.button(f"pub:{self.item['id']}")
        self.sch.tick(self.ts(11, "11:31"))
        self.assertEqual(self.app.db.get_item(self.item["id"])["status"], "posted")

    def test_auto_publishes_unapproved(self):
        self.cfg["on_missed_approval"] = "auto"
        self.sch.tick(self.ts(11, "10:00"))
        self.assertEqual(self.app.db.get_item(self.item["id"])["status"], "posted")


class SleepPlanTest(FlowBase):
    def test_wake_times_and_busy(self):
        self.cfg["power"] = {"sleep_between_slots": True, "wake_before_minutes": 5, "idle_minutes": 10}
        sch = self.app.scheduler
        times = sch.wake_times(self.ts(11, "08:00"))
        self.assertEqual(times[0], self.ts(11, "09:55"))
        self.assertIn(self.ts(11, "20:55"), times)
        self.assertIn(self.ts(11, "22:29"), times)  # щоденний звіт
        sch.ensure_slots(self.ts(11, "08:00"))
        sch._awake_since = 0
        self.assertIsNone(sch.busy_reason(self.ts(11, "11:00")))  # не Windows: «користувач неактивний»
        self.assertEqual(sch.busy_reason(self.ts(11, "12:50")), "скоро слот")

    def test_notification_still_sent_when_woken_5_min_before(self):
        self.cfg["power"] = {"sleep_between_slots": True, "wake_before_minutes": 5, "idle_minutes": 10}
        self.make_short("a")
        self.app.scheduler.tick(self.ts(11, "09:55"))
        self.assertEqual(len(self.app.db.items("status='notified'")), 1)

    def test_wake_script(self):
        s = power.wake_script([self.ts(11, "09:55")], r"C:\py\pythonw.exe")
        self.assertIn("-WakeToRun", s)
        self.assertIn("'2026-11-11T07:55:00Z'", s)
        self.assertIn(power.TASK, s)


if __name__ == "__main__":
    unittest.main()
