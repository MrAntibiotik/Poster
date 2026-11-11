"""Вибір будь-якого відео з черги й публікація тоді, коли хочеться."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


class PickTest(FlowBase):
    def setUp(self):
        super().setUp()
        self.cfg["ask_texts_before_publish"] = True
        for n in "abcde":
            self.make_short(n, age=1000 - ord(n))
        config.FAKE_NOW = self.ts(11, "08:00")
        self.app.scheduler.scan()

    def say(self, text):
        self.app.bot.handle({"update_id": 1, "message": {"text": text, "chat": {"id": 42, "type": "private"}}})

    def button(self, data):
        self.app.bot.handle({"update_id": 1, "callback_query": {
            "id": "c", "data": data, "message": {"message_id": 999, "chat": {"id": 42}, "text": ""}}})

    def item(self, name):
        return [i for i in self.app.db.items("1") if Path(i["mp4"]).stem == name][0]

    def test_queue_buttons_and_publish_now(self):
        self.say("📋 Черга")
        kb = self.app.bot.tg.sent[-1][2]["inline_keyboard"]
        labels = [r[0]["text"] for r in kb]
        self.assertTrue(labels[3].startswith("#4 d · сьогодні 19:00"))
        d = self.item("d")
        self.button(f"qi:{d['id']}")  # картка з відео
        card = self.app.bot.tg.sent[-1]
        self.assertEqual(card[0], "video")
        self.assertIn("qt:", json.dumps(card[2]))
        self.button(f"qt:{d['id']}")
        self.assertIn("Коли опублікувати", self.app.bot.tg.sent[-1][1])
        self.button(f"tn:{d['id']}")  # ⚡ Зараз
        self.assertEqual(self.app.db.get_item(d["id"])["status"], "notified")
        self.assertIn("Крок 1/2", self.app.bot.tg.sent[-1][1])
        self.say("Камбек")
        self.say("wot")  # без кроку 3 — час уже обрано
        self.assertEqual(self.app.db.get_item(d["id"])["status"], "posted")
        # решта черги зсунулась
        names = [Path(it["mp4"]).stem for it, _ in self.app.scheduler.projection(self.ts(11, "08:00"))]
        self.assertEqual(names, ["a", "b", "c", "e"])

    def test_pick_custom_time(self):
        e = self.item("e")
        self.button(f"qt:{e['id']}")
        self.button(f"tc:{e['id']}")
        self.say("сьогодні 11:45")
        it = self.app.db.get_item(e["id"])
        self.assertEqual((it["status"], it["slot_at"]), ("notified", self.ts(11, "11:45")))
        self.say("Назва")
        self.say("-")
        self.assertEqual(self.app.db.get_item(e["id"])["status"], "approved")
        self.assertIn("Публікація сьогодні 11:45", self.app.bot.tg.sent[-1][1])
        self.app.scheduler.tick(self.ts(11, "11:45"))
        self.assertEqual(self.app.db.get_item(e["id"])["status"], "posted")

    def test_move_first_and_remove(self):
        c = self.item("c")
        self.button(f"qf:{c['id']}")
        self.assertIn("першим у черзі", self.app.bot.tg.sent[-1][1])
        names = [Path(it["mp4"]).stem for it, _ in self.app.scheduler.projection(self.ts(11, "08:00"))]
        self.assertEqual(names[0], "c")
        b = self.item("b")
        self.button(f"qd:{b['id']}")
        it = self.app.db.get_item(b["id"])
        self.assertEqual(it["status"], "skipped")
        self.assertIn(str(self.cfg["paths"]["skipped"]), it["mp4"])
        self.app.scheduler.scan()  # не повертається в чергу
        self.assertNotIn("b", [Path(x["mp4"]).stem for x in self.app.db.queue()])

    def test_viewer_sees_no_buttons(self):
        self.app.db.set_user(777, "viewer", "Друг")
        self.app.bot.handle({"update_id": 2, "message": {"text": "📋 Черга", "chat": {"id": 777, "type": "private"}}})
        self.assertIsNone(self.app.bot.tg.sent[-1][2])


if __name__ == "__main__":
    unittest.main()
