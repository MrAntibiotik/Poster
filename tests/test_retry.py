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
        self.assertFalse([s for s in self.app.bot.tg.sent if s[0] == "text" and "Не опубліковано" in s[1]])
        self.assertIn("📸 Instagram — ❌ не зміг обробити відео", self.app.bot.tg.edits[-1][1])  # причина — у підписі відео
        self.assertIn("rf:", json.dumps(self.app.bot.tg.edits[-1][2]))  # кнопки під самим відео
        self.say("⚠️ Невдалі")
        self.assertTrue(any("Невдалі публікації: 1" in s[1] for s in self.app.bot.tg.sent if s[0] == "text"))
        self.assertEqual(self.app.bot.tg.sent[-1][0], "video")
        self.button(f"rf:{self.item['id']}")  # тепер Instagram проходить
        it = self.app.db.get_item(self.item["id"])
        self.assertEqual(it["results"]["instagram"]["status"], "ok")
        self.assertEqual(it["results"]["youtube"]["id"], "y1")  # YouTube не дублюється
        self.assertEqual(self.app.scheduler.failed_items(), [])

    def test_retry_message_disappears_when_published(self):
        self.button(f"rf:{self.item['id']}")  # «🔁 Повторити зараз» з окремого повідомлення (id 999)
        self.assertEqual(self.app.db.get_item(self.item["id"])["results"]["instagram"]["status"], "ok")
        self.assertIn(999, self.app.bot.tg.deleted)  # статус — у підписі відео, окреме повідомлення прибрано
        self.assertIsNone(self.app.db.get(f"itemmsg:{self.item['id']}"))

    def test_caption_shows_current_platform_while_retrying(self):
        db, bot = self.app.db, self.app.bot
        db.update_item(self.item["id"], status="publishing")
        db.set(f"pubnow:{self.item['id']}", "instagram")
        db.set(f"pubtry:{self.item['id']}:instagram", "2/3")
        cap = bot.caption(db.get_item(self.item["id"]))
        self.assertIn("📸 Instagram — ⏳ публікується… (спроба 2/3)", cap)
        self.assertNotIn("ProcessingFailed", cap)  # стара помилка не плутає

    def test_instagram_all_variants_failed_no_outer_retry(self):
        calls = []

        class Rejects:
            def publish(self, path, meta):
                calls.append(1)
                raise RuntimeError("Instagram не прийняв відео (3 спроб): …")
        self.app.platforms["instagram"] = Rejects()
        self.button(f"rf:{self.item['id']}")
        self.assertEqual(len(calls), 1)  # без ще двох повних кіл по 3 варіанти файлу

    def test_details_button(self):
        kb = str(self.app.bot.keyboard(self.app.db.get_item(self.item["id"])))
        self.assertIn(f"err:{self.item['id']}", kb)
        self.button(f"err:{self.item['id']}")
        text = self.app.bot.tg.sent[-1][1]
        self.assertIn("🧾 <b>Деталі публікації", text)
        self.assertIn("❌ <b>Instagram</b>", text)
        self.assertIn("ProcessingFailedError", text)  # повний текст причини

    def test_retry_in_free_slot(self):
        self.button(f"rq:{self.item['id']}")
        when = self.app.scheduler.retry_at(self.item["id"])
        config.FAKE_NOW = self.ts(11, "13:01")
        self.assertEqual(config.fmt_when(self.cfg, when, self.ts(11, "13:01")), "сьогодні 16:00 (11.11)")
        # слот зайнято повтором — черга (b) іде далі
        plan = self.app.scheduler.projection(self.ts(11, "13:01"))
        self.assertEqual(config.fmt_when(self.cfg, plan[0][1], self.ts(11, "13:01")), "сьогодні 19:00 (11.11)")
        self.app.scheduler.tick(self.ts(11, "16:00"))
        self.assertEqual(self.app.db.get_item(self.item["id"])["results"]["instagram"]["status"], "ok")

    def test_retry_custom_time_and_ignore(self):
        self.button(f"rt:{self.item['id']}")
        self.assertIn("Коли повторити", self.app.bot.tg.sent[-1][1])
        self.button(f"tc:{self.item['id']}")
        self.say("сьогодні 18:20 (11.11)")
        self.assertEqual(self.app.scheduler.retry_at(self.item["id"]), self.ts(11, "18:20"))
        self.button(f"ri:{self.item['id']}")
        self.assertIsNone(self.app.scheduler.retry_at(self.item["id"]))
        self.assertEqual(self.app.scheduler.failed_items(), [])
        self.say("/failed")
        last = self.app.bot.tg.sent[-1]
        self.assertIn("Невдалих публікацій немає", last[1])
        self.assertIn("Проігноровані (1)", json.dumps(last[2], ensure_ascii=False))
        self.button("ig:0")
        card = self.app.bot.tg.sent[-1]
        self.assertEqual(card[0], "video")  # з самим відео, як перед публікацією
        self.assertIn("🙈 ⚠️ <b>Не опубліковано</b>", card[1])
        self.assertIn("ru:", json.dumps(card[2]))
        self.button(f"ru:{self.item['id']}")
        self.assertEqual(len(self.app.scheduler.failed_items()), 1)  # повернулось до невдалих
        self.assertEqual(self.app.scheduler.failed_items(ignored=True), [])


class CardFallbackTest(RetryTest):
    def test_card_falls_back_to_text(self):
        def boom(*a, **k):
            raise ValueError("щось зламалось")
        self.app.bot.failed_text = boom
        self.app.bot.tg.sent.clear()
        self.app.bot.handle({"update_id": 9, "message": {"text": "/failed", "chat": {"id": 42, "type": "private"}}})
        last = self.app.bot.tg.sent[-1]
        self.assertIn("картку з відео не вдалося показати", last[1])
        self.assertIn("rf:", json.dumps(last[2]))  # кнопки на місці


if __name__ == "__main__":
    unittest.main()
