"""Пропущене підтвердження (wait/auto) і сон між слотами."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.system import power  # noqa: E402

_orig_procs = power.running_processes
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
    def tearDown(self):
        power.running_processes = _orig_procs
        super().tearDown()

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

    def test_quiet_after_activity_and_stream(self):
        self.cfg["power"] = {"sleep_between_slots": True, "wake_before_minutes": 5, "quiet_minutes": 5,
                             "no_sleep_apps": []}
        sch, db = self.app.scheduler, self.app.db
        sch.ensure_slots(self.ts(11, "08:00"))
        db.set("last_activity", self.ts(11, "10:58"))
        self.assertIn("тиші", sch.busy_reason(self.ts(11, "11:01")))
        self.assertIsNone(sch.busy_reason(self.ts(11, "11:04")))
        db.set("stream_until", self.ts(11, "15:00"))
        self.assertIn("режим стріму", sch.busy_reason(self.ts(11, "11:04")))
        power.running_processes = lambda: {"obs64.exe"}
        self.cfg["power"]["no_sleep_apps"] = ["OBS64.exe"]
        db.set("stream_until", None)
        self.assertIn("obs64.exe", sch.busy_reason(self.ts(11, "11:04")))

    def test_hold_publishing_during_stream(self):
        self.cfg["power"] = {"hold_during_stream": True, "no_sleep_apps": []}
        self.make_short("a")
        sch, db = self.app.scheduler, self.app.db
        sch.tick(self.ts(11, "09:00"))
        item = db.items("status='notified'")[0]
        db.update_item(item["id"], status="approved")
        db.set("stream_until", self.ts(11, "12:00"))
        sch.tick(self.ts(11, "10:00"))
        self.assertEqual(db.get_item(item["id"])["status"], "approved")  # чекає кінця стріму
        sch.tick(self.ts(11, "12:01"))
        self.assertEqual(db.get_item(item["id"])["status"], "posted")

    def test_stream_button(self):
        self.app.bot.handle({"update_id": 1, "message": {"message_id": 7, "text": "🎮 Стрім",
                                                          "chat": {"id": 42, "type": "private"}}})
        self.assertIn("Режим стріму", self.app.bot.tg.sent[-1][1])
        self.app.bot.handle({"update_id": 2, "callback_query": {
            "id": "c", "data": "stm:4", "message": {"message_id": 999, "chat": {"id": 42}, "text": ""}}})
        self.assertGreater(int(self.app.db.get("stream_until")), 0)

    def test_wake_script(self):
        s = power.wake_script([self.ts(11, "09:55")], r"C:\py\pythonw.exe")
        self.assertIn("-WakeToRun", s)
        self.assertIn("'2026-11-11T07:55:00Z'", s)
        self.assertIn(power.TASK, s)


if __name__ == "__main__":
    unittest.main()
