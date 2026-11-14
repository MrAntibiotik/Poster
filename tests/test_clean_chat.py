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
        self.app.bot.send(keep="reauth_youtube", text="🔑 потрібна авторизація")
        report = self.last_id()
        self.app.bot.say(42, "📈 стара аналітика", keep="stats")  # так зберігали старі версії
        old_stats = self.last_id()
        tg.msg_id += 20  # «старе сміття» до оновлення, про яке бот нічого не знає
        self.app.bot.clean_chat(42)
        cleaned = set(tg.deleted)
        self.assertNotIn(video, cleaned)
        self.assertNotIn(report, cleaned)  # невирішене службове лишається
        self.assertIn(old_stats, cleaned)  # аналітика — ні
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

    def test_heal_after_outage(self):
        tg, bot = self.app.bot.tg, self.app.bot
        self.make_short("a")
        self.app.scheduler.tick(self.ts(11, "09:00"))
        item = self.app.db.items("status='notified'")[0]
        self.assertEqual(bot.heal(), 0)  # усе актуально
        orig = tg.edit_caption

        def down(*a, **kw):
            raise ConnectionError("немає інтернету")
        tg.edit_caption = down
        self.app.db.update_item(item["id"], status="posted", posted_at=self.ts(11, "10:00"),
                                results={"youtube": {"status": "ok", "url": "https://yt/x"}})
        bot.refresh(item["id"])  # не вдалося — світла/інтернету немає
        tg.edit_caption = orig
        n = len(tg.edits)
        self.assertEqual(bot.heal(), 1)  # зв'язок повернувся — підпис доведено до актуального
        self.assertIn("✅ <b>Опубліковано</b>", tg.edits[n][1])
        self.assertEqual(bot.heal(), 0)

    def test_text_card_upgraded_to_video(self):
        tg, bot, db = self.app.bot.tg, self.app.bot, self.app.db
        self.make_short("a")
        orig = tg.send_video

        def fail(*a, **kw):
            from telegram_api import TelegramError
            raise TelegramError("немає інтернету")
        tg.send_video = fail
        self.app.scheduler.tick(self.ts(11, "09:00"))  # відео не пішло — картка текстом
        item = db.items("status='notified'")[0]
        old = db.tg_msg(item["id"], 42)
        self.assertEqual(old["is_text"], 1)
        tg.send_video = orig
        db.set(f"upgrade_try:{item['id']}:42", None)  # минуло 30 хв від попередньої спроби
        self.assertEqual(bot.upgrade_text_cards(), 1)
        new = db.tg_msg(item["id"], 42)
        self.assertEqual(new["is_text"], 0)
        self.assertIn(old["msg_id"], tg.deleted)
        self.assertEqual(tg.sent[-1][0], "video")

    def test_keyboard_restored_after_cleanup(self):
        tg, bot = self.app.bot.tg, self.app.bot
        bot.ensure_keyboard()
        kb = tg.sent[-1][2]
        self.assertIn("⬆️ Оновити", str(kb))
        first = self.last_id()
        bot.clean_chat(42)
        self.assertNotIn(first, tg.deleted)  # повідомлення з кнопками не прибирається
        n = len(tg.sent)
        bot.ensure_keyboard()
        self.assertEqual(len(tg.sent), n)  # уже є — не дублюємо

    def test_log_in_bot(self):
        from pathlib import Path as P
        log = P(self.cfg["paths"]["log"])
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            "2026-11-14 20:01:02,123 [INFO] scheduler: усе гаразд\n"
            "2026-11-14 20:05:00,001 [WARNING] platforms.meta_graph: Instagram: копія — відмова: Video Transcoding Error\n"
            "2026-11-14 20:06:00,001 [ERROR] publisher: YouTube впав, токен 123:abcdefghijklmnop\n", encoding="utf-8")
        self.cfg["secrets"]["TELEGRAM_BOT_TOKEN"] = "123:abcdefghijklmnop"
        self.say("📄 Журнал", 7001)
        text = self.app.bot.tg.sent[-1][1]
        self.assertIn("Video Transcoding Error", text)
        self.assertNotIn("усе гаразд", text)  # лише помилки
        self.assertNotIn("abcdefghijklmnop", text)  # секрети приховано
        text, _ = self.app.bot.log_text("instagram", mode="ig")
        self.assertIn("Transcoding", text)
        self.assertNotIn("YouTube впав", text)

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
