"""Скасування шортса (папка «скасовані», черга зсувається, можна повернути) і «опублікувати всі, що чекають»."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_flow import FlowBase  # noqa: E402


class CancelTest(FlowBase):
    def setUp(self):
        super().setUp()
        for n in "abc":
            self.make_short(n, age=1000 - ord(n))
        self.sch, self.db = self.app.scheduler, self.app.db

    def name(self, item):
        return Path(item["mp4"]).stem

    def button(self, data, item):
        own = self.db.tg_msg(item["id"], 42)
        self.app.bot.handle({"update_id": 1, "callback_query": {
            "id": "c", "data": data, "message": {"message_id": own["msg_id"], "chat": {"id": 42}, "text": ""}}})

    def test_cancel_moves_file_and_next_takes_slot(self):
        self.sch.tick(self.ts(11, "09:00"))  # a → 10:00
        a = self.db.items("status='notified'")[0]
        self.assertIn("cx:", str(self.app.bot.keyboard(a)))
        self.button(f"cx:{a['id']}", a)
        a = self.db.get_item(a["id"])
        self.assertEqual(a["status"], "cancelled")
        self.assertIn(str(self.cfg["paths"]["cancelled"]), a["mp4"])
        self.assertTrue(Path(a["mp4"]).exists())
        self.assertIn("Скасовано", self.app.bot.tg.edits[-1][1])
        nxt = self.db.items("status='notified'")[0]  # черга зсунулась: b на той самий слот 10:00
        self.assertEqual((self.name(nxt), nxt["slot_at"]), ("b", self.ts(11, "10:00")))
        self.sch.scan()
        self.assertEqual(len(self.db.items("1")), 3)  # файл у «скасованих» не потрапляє в чергу знову

    def test_restore(self):
        self.sch.tick(self.ts(11, "09:00"))
        a = self.db.items("status='notified'")[0]
        self.sch.cancel(a["id"])
        self.button(f"cr:{a['id']}", a)
        a = self.db.get_item(a["id"])
        self.assertEqual(a["status"], "queued")
        self.assertIn(str(self.cfg["paths"]["ready"]), a["mp4"])
        self.assertEqual(self.name(self.sch.projection()[0][0]), "a")  # перший у черзі

    def test_cancel_from_queue_and_guards(self):
        self.sch.scan()
        c = [i for i in self.db.items("1") if self.name(i) == "c"][0]
        self.sch.cancel(c["id"])
        self.assertEqual(self.db.get_item(c["id"])["status"], "cancelled")
        with self.assertRaises(ValueError):
            self.sch.cancel(c["id"])
        with self.assertRaises(ValueError):
            self.sch.restore(999)

    def test_publish_all_waiting(self):
        self.cfg["on_missed_approval"] = "wait"
        self.sch.tick(self.ts(11, "09:00"))   # a → 10:00
        self.sch.tick(self.ts(11, "10:00"))   # не підтверджено — чекає
        self.sch.tick(self.ts(11, "12:00"))   # b → 13:00
        self.sch.tick(self.ts(11, "13:00"))   # теж чекає
        waiting = self.db.items("status='notified'", order="slot_at")
        self.assertEqual([self.name(i) for i in waiting], ["a", "b"])
        self.assertIn("pall:0", str(self.app.bot.keyboard(waiting[0])))
        self.button("pall:0", waiting[0])
        self.assertEqual([self.db.get_item(i["id"])["status"] for i in waiting], ["posted", "posted"])
        self.assertIsNone(self.db.get("bulk_ids"))
        with self.assertRaises(ValueError):
            self.app.bot.publish_all()


if __name__ == "__main__":
    unittest.main()
