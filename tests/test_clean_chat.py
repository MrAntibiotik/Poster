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

    def test_only_videos_stay_and_running_task_survives(self):
        tg = self.app.bot.tg
        self.make_short("a")
        self.app.scheduler.tick(self.ts(11, "09:00"))  # відео-картка шортса
        item = self.app.db.items("status='notified'")[0]
        video_ids = [r["msg_id"] for r in self.app.db.tg_msgs(item["id"])]
        self.say("📋 Черга", 5001)
        queue_reply = self.last_id()
        self.say("🕐 Статус", 5002)
        self.assertTrue({5001, queue_reply} <= set(tg.deleted))  # попередня кнопка й відповідь прибрані
        self.say("📈 Аналітика", 5003)
        stats = self.last_id()  # «⏳ Збираю…» → стало результатом (редагування того ж повідомлення)
        self.assertIn("Аналітика", [e for e in tg.edits if e[0] == stats][-1][1])
        self.say("🕐 Статус", 5004)
        self.assertIn(stats, tg.deleted)  # результат прибрано при наступній дії
        # завдання, що ще виконується, не зникає від інших кнопок
        running = self.app.bot.task_start(42, "⏳ Довга дія…")["message_id"]
        self.say("📋 Черга", 5005)
        self.assertNotIn(running, tg.deleted)
        self.app.bot.task_done(42, {"message_id": running}, "✅ Готово")
        self.say("🕐 Статус", 5006)
        self.assertIn(running, tg.deleted)
        for v in video_ids:
            self.assertNotIn(v, tg.deleted)  # відео шортса з прогресом лишається

    def test_clean_chat_keeps_only_videos_and_reports(self):
        tg = self.app.bot.tg
        self.make_short("a")
        self.app.scheduler.tick(self.ts(11, "09:00"))
        item = self.app.db.items("status='notified'")[0]
        video = self.app.db.tg_msg(item["id"], 42)["msg_id"]
        self.app.bot.send("📊 звіт", keep=True)
        report = self.last_id()
        tg.msg_id += 20  # «старе сміття» до оновлення, про яке бот нічого не знає
        self.app.bot.clean_chat(42)
        cleaned = set(tg.deleted)
        self.assertNotIn(video, cleaned)
        self.assertNotIn(report, cleaned)
        self.assertTrue({report + 5, report + 15, self.last_id()} <= cleaned)  # і тимчасове «🧹» теж

    def test_auto_reports_replace_and_service_resolves(self):
        tg, bot = self.app.bot.tg, self.app.bot
        bot.send("📊 звіт 1", kind="daily")
        first = self.last_id()
        bot.send("📊 звіт 2", kind="daily")
        self.assertIn(first, tg.deleted)  # лишається лише останній автозвіт
        bot.send(keep="reauth_youtube", text="🔑 Потрібна повторна авторизація")
        need = self.last_id()
        self.say("🕐 Статус", 6001)
        self.assertNotIn(need, tg.deleted)  # службове — висить, доки не вирішено
        bot.resolve("reauth_youtube")
        self.assertIn(need, tg.deleted)  # вирішено — зникло

    def test_views_24h_in_video_caption(self):
        self.make_short("a")
        self.app.scheduler.tick(self.ts(11, "09:00"))
        item = self.app.db.items("status='notified'")[0]
        self.app.db.update_item(item["id"], status="posted", results={
            "youtube": {"status": "ok", "url": "https://yt/x", "views24": 1500, "likes24": 40}})
        self.app.bot.refresh(item["id"])
        self.assertIn("👁 1 500 ❤️ 40", self.app.bot.tg.edits[-1][1])

    def test_service_messages_expire(self):
        tg = self.app.bot.tg
        self.app.bot.send("🟢 poster запущено")
        mid = self.last_id()
        self.app.scheduler.tick(self.ts(11, "09:00"))
        config.FAKE_NOW = None
        self.assertNotIn(mid, tg.deleted)
        import time
        config.FAKE_NOW = int(time.time()) + 25 * 3600
        self.app.bot.purge_expired()
        self.assertIn(mid, tg.deleted)


if __name__ == "__main__":
    unittest.main()
