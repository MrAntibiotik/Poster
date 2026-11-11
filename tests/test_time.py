"""Вибір і зміна часу публікації в Telegram."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from config import fmt_when, parse_when  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


class ParseWhenTest(FlowBase):
    def test_formats(self):
        now = self.ts(11, "10:00")
        self.assertEqual(parse_when("18:30", self.cfg, now), self.ts(11, "18:30"))
        self.assertEqual(parse_when("09:15", self.cfg, now), self.ts(12, "09:15"))  # уже минуло -> завтра
        self.assertEqual(parse_when("завтра 18:30 (12.11)", self.cfg, now), self.ts(12, "18:30"))
        self.assertEqual(parse_when("13.11 7:05", self.cfg, now), self.ts(13, "07:05"))
        self.assertEqual(parse_when("о 21:00 сьогодні", self.cfg, now), self.ts(11, "21:00"))
        for bad, why in [("завтра", "ГГ:ХХ"), ("25:00", "не буває"), ("сьогодні 09:00 (11.11)", "минув"),
                         ("31.02 10:00", "не існує")]:
            with self.assertRaisesRegex(ValueError, why):
                parse_when(bad, self.cfg, now)


class TimeTest(FlowBase):
    def setUp(self):
        super().setUp()
        self.cfg["ask_texts_before_publish"] = True

    def say(self, text):
        self.app.bot.handle({"update_id": 1, "message": {"text": text, "chat": {"id": 42, "type": "private"}}})

    def button(self, data):
        self.app.bot.handle({"update_id": 1, "callback_query": {
            "id": "c", "data": data, "message": {"message_id": 999, "chat": {"id": 42}, "text": ""}}})

    def kb(self):
        return [b for row in self.app.bot.tg.sent[-1][2]["inline_keyboard"] for b in row]

    def start(self, names="ab"):
        for n in names:
            self.make_short(n, age=1000 - ord(n))
        self.app.scheduler.tick(self.ts(11, "12:00"))  # a -> 13:00 (сповіщено), b -> 16:00
        item = self.app.db.items("status='notified'")[0]
        self.press(f"pub:{item['id']}", item)
        self.say("Назва")
        self.say("shorts")
        self.assertIn("Крок 3/3", self.app.bot.tg.sent[-1][1])
        return item

    def test_pick_free_slot_and_queue_moves_up(self):
        item = self.start()
        labels = [b["text"] for b in self.kb()]
        self.assertIn("✅ Залишити: сьогодні 13:00 (11.11)", labels)
        self.assertIn("завтра 10:00 (12.11)", labels)
        self.assertIn("⚡ Зараз", labels)
        ts = self.ts(12, "10:00")
        self.button(f"ts:{item['id']}:{ts}")
        it = self.app.db.get_item(item["id"])
        self.assertEqual((it["status"], it["slot_at"]), ("approved", ts))
        self.assertIn("публікація о 12.11 10:00", self.app.bot.tg.edits[-1][1])
        # звільнений слот 13:00 одразу дістається наступному відео (b)
        config.FAKE_NOW = self.ts(11, "12:01")
        plan = self.app.scheduler.projection(self.ts(11, "12:01"))
        self.assertEqual(fmt_when(self.cfg, plan[0][1], self.ts(11, "12:01")), "сьогодні 13:00 (11.11)")
        self.app.scheduler.tick(self.ts(11, "12:02"))
        self.assertEqual(Path(self.app.db.items("status='notified'")[0]["mp4"]).stem, "b")
        # у новий час публікується саме a
        self.app.scheduler.tick(self.ts(11, "13:00"))  # b не підтверджено -> пропуск
        self.app.scheduler.tick(self.ts(12, "10:00"))
        self.assertEqual(self.app.db.get_item(item["id"])["status"], "posted")

    def test_custom_time_and_change_later(self):
        item = self.start("a")
        self.button(f"tc:{item['id']}")
        self.say("сьогодні 09:00 (11.11)")  # минуло
        self.assertIn("минув", self.app.bot.tg.sent[-1][1])
        self.say("сьогодні 18:45 (11.11)")
        it = self.app.db.get_item(item["id"])
        self.assertEqual((it["status"], it["slot_at"]), ("approved", self.ts(11, "18:45")))
        self.assertEqual(self.app.db.slot(self.ts(11, "18:45"))["source"], "manual")
        # пізніше: кнопка «🕐 Змінити час» під шортсом
        self.press(f"time:{item['id']}", it)
        self.assertIn("Новий час для шортса", self.app.bot.tg.sent[-1][1])
        self.say("завтра 08:10 (12.11)")
        it = self.app.db.get_item(item["id"])
        self.assertEqual((it["status"], it["slot_at"]), ("approved", self.ts(12, "08:10")))
        self.assertIsNone(self.app.db.slot(self.ts(11, "18:45")))  # ручний слот прибрано
        self.app.scheduler.tick(self.ts(12, "08:10"))
        self.assertEqual(self.app.db.get_item(item["id"])["status"], "posted")

    def test_publish_now_and_taken_slot(self):
        item = self.start("ab")
        b_plan = self.app.scheduler.projection(self.ts(11, "12:00"))[0][1]  # 16:00 для b — вільний слот
        self.button(f"ts:{item['id']}:{b_plan}")  # можна: b ще в черзі, просто зсунеться
        self.assertEqual(self.app.db.get_item(item["id"])["slot_at"], b_plan)
        item2 = self.app.db.get_item(item["id"])
        self.press(f"time:{item['id']}", item2)
        self.button(f"tn:{item['id']}")  # ⚡ Зараз
        self.assertEqual(self.app.db.get_item(item["id"])["status"], "posted")


if __name__ == "__main__":
    unittest.main()
