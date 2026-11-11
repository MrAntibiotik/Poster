"""Охайний чат: відповіді на кнопки й натискання зникають, лишаються відео з прогресом і аналітика."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


class CleanChatTest(FlowBase):
    def say(self, text, mid):
        self.app.bot.handle({"update_id": 1, "message": {"message_id": mid, "text": text,
                                                          "chat": {"id": 42, "type": "private"}}})

    def last_id(self):
        return self.app.bot.tg.msg_id

    def test_replies_disappear_videos_and_analytics_stay(self):
        tg = self.app.bot.tg
        self.make_short("a")
        self.app.scheduler.tick(self.ts(11, "09:00"))  # відео-картка шортса
        item = self.app.db.items("status='notified'")[0]
        video_ids = [r["msg_id"] for r in self.app.db.tg_msgs(item["id"])]
        self.assertTrue(video_ids)
        self.say("📋 Черга", 5001)
        queue_reply = self.last_id()
        self.say("🕐 Статус", 5002)
        self.assertIn(5001, tg.deleted)          # натискання кнопки прибрано
        self.assertIn(queue_reply, tg.deleted)   # попередня відповідь прибрана
        self.say("📈 Аналітика", 5003)
        first_stats = self.last_id()
        self.say("📈 Аналітика", 5004)
        self.assertIn(first_stats - 1, tg.deleted)  # заглушка «Збираю…» видаляється одразу
        self.assertIn(first_stats, tg.deleted)    # стара аналітика замінена новою
        second_stats = self.last_id()
        self.say("🕐 Статус", 5005)
        self.assertNotIn(second_stats, tg.deleted)  # остання аналітика лишається
        for v in video_ids:
            self.assertNotIn(v, tg.deleted)          # відео шортса з прогресом лишається

    def test_service_messages_expire(self):
        tg = self.app.bot.tg
        self.app.bot.send("🟢 poster запущено")
        mid = self.last_id()
        self.app.scheduler.tick(self.ts(11, "09:00"))
        config.FAKE_NOW = None
        self.assertNotIn(mid, tg.deleted)
        import time
        config.FAKE_NOW = int(time.time()) + 7 * 3600
        self.app.bot.purge_expired()
        self.assertIn(mid, tg.deleted)


if __name__ == "__main__":
    unittest.main()
