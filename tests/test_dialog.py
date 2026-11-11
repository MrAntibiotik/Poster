"""Перед публікацією бот питає назву й хештеги."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meta import load_meta, parse_hashtags  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


class DialogTest(FlowBase):
    def setUp(self):
        super().setUp()
        self.cfg["ask_texts_before_publish"] = True

    def say(self, text):
        self.app.bot.handle({"update_id": 1, "message": {"text": text, "chat": {"id": 42, "type": "private"}}})

    def button(self, data):
        self.app.bot.handle({"update_id": 1, "callback_query": {
            "id": "c", "data": data, "message": {"message_id": 999, "chat": {"id": 42}, "text": ""}}})

    def last(self):
        return self.app.bot.tg.sent[-1][1]

    def start(self):
        mp4 = self.make_short("a")
        self.app.scheduler.tick(self.ts(11, "12:00"))
        item = self.app.db.items("status='notified'")[0]
        self.assertIn("бот попросить назву й хештеги", [s for s in self.app.bot.tg.sent if s[0] == "video"][0][1])
        self.press(f"pub:{item['id']}", item)
        return mp4, item

    def test_title_and_tags_then_publish(self):
        mp4, item = self.start()
        self.assertEqual(self.app.db.get_item(item["id"])["status"], "notified")  # ще не підтверджено
        self.assertIn("Крок 1/2", self.last())
        self.say("Камбек на Прохорівці")
        self.assertIn("Крок 2/2", self.last())
        self.say("wot, #танки shorts")
        self.assertIn("Крок 3/3", self.last())
        self.assertEqual(self.app.db.get_item(item["id"])["status"], "notified")  # ще чекає на час
        self.button(f"tk:{item['id']}")
        it = self.app.db.get_item(item["id"])
        self.assertEqual(it["status"], "approved")
        # допоміжні повідомлення прибрано, статус — у підписі відео
        tg = self.app.bot.tg
        prompts = [i + 101 for i, m in enumerate(tg.sent) if m[0] == "text" and "Крок" in m[1]]
        self.assertTrue(set(prompts) <= set(tg.deleted), (prompts, tg.deleted))
        self.assertIn("Підтверджено — публікація о 13:00", tg.edits[-1][1])
        self.assertFalse(any("Готово" in m[1] for m in tg.sent if m[0] == "text"))
        m = load_meta(mp4.with_suffix(".json"))
        self.assertEqual(m.yt_title, "Камбек на Прохорівці")
        self.assertEqual(m.hashtags, ["#wot", "#танки", "#shorts"])
        self.assertEqual(m.tiktok_text, "Камбек на Прохорівці #wot #танки #shorts")
        self.assertEqual(m.instagram_text, "Камбек на Прохорівці\n\n#wot #танки #shorts")
        self.assertNotIn("#", m.yt_description)
        self.app.scheduler.tick(self.ts(11, "13:00"))
        it = self.app.db.get_item(item["id"])
        self.assertEqual(it["status"], "posted")
        data = json.loads(Path(it["json"]).read_text(encoding="utf-8"))
        self.assertTrue(data["manual_texts"])

    def test_keep_buttons_and_no_tags(self):
        mp4, item = self.start()
        self.button(f"keep:{item['id']}:title")
        self.assertIn("Крок 2/2", self.last())
        self.say("-")
        self.button(f"tk:{item['id']}")
        self.assertEqual(self.app.db.get_item(item["id"])["status"], "approved")
        m = load_meta(mp4.with_suffix(".json"))
        self.assertEqual(m.yt_title, "Титул a")
        self.assertEqual(m.hashtags, [])

    def test_keep_tags(self):
        mp4, item = self.start()
        self.say("Нова назва")
        self.button(f"keep:{item['id']}:tags")
        m = load_meta(mp4.with_suffix(".json"))
        self.assertEqual((m.yt_title, m.hashtags), ("Нова назва", ["#wot", "#танки"]))

    def test_cancel_and_slot_passed(self):
        mp4, item = self.start()
        self.button(f"dlgx:{item['id']}")
        self.say("Назва після скасування")  # вже не діалог — просто довідка
        self.assertEqual(self.app.db.get_item(item["id"])["status"], "notified")
        self.assertNotEqual(load_meta(mp4.with_suffix(".json")).yt_title, "Назва після скасування")
        # діалог почато, але слот минув -> шортс пропущено, відповідь неактуальна
        self.press(f"pub:{item['id']}", item)
        self.app.scheduler.tick(self.ts(11, "13:00"))
        self.say("Запізніла назва")
        self.assertIn("неактуальний", self.last())
        self.assertEqual(self.app.db.get_item(item["id"])["status"], "queued")

    def test_parse_hashtags(self):
        self.assertEqual(parse_hashtags("wot танки, #Shorts;  "), ["#wot", "#танки", "#Shorts"])
        self.assertEqual(parse_hashtags("-"), [])
        self.assertEqual(parse_hashtags("#wot #wot ще-раз"), ["#wot", "#щераз"])


if __name__ == "__main__":
    unittest.main()
