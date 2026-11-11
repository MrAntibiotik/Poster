"""Доступ для кількох людей, кнопки керування, пауза, перенесення на тестовий слот."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_flow import FlowBase  # noqa: E402

OWNER, FRIEND, STRANGER = 42, 777, 999


class AccessTest(FlowBase):
    def msg(self, chat, text, **frm):
        self.app.bot.handle({"update_id": 1, "message": {
            "text": text, "chat": {"id": chat, "type": "private"}, "from": {"id": chat, **frm}}})

    def cb(self, chat, data, message_id):
        self.app.bot.handle({"update_id": 1, "callback_query": {
            "id": "c", "data": data, "message": {"message_id": message_id, "chat": {"id": chat}, "text": ""}}})

    def sent_to(self, chat):
        return [s for s in self.app.bot.tg.sent if s[-1] == chat]

    def test_access_request_and_viewer(self):
        tg = self.app.bot.tg
        self.msg(FRIEND, "/start", first_name="Друг", username="friend")
        self.assertEqual(self.app.db.user(FRIEND)["role"], "pending")
        req = [s for s in self.sent_to(OWNER) if "Запит на доступ" in s[1]][0]
        self.assertIn("@friend", req[1])
        self.msg(FRIEND, "/start")  # повторний запит не спамить власника
        self.assertEqual(len([s for s in self.sent_to(OWNER) if "Запит на доступ" in s[1]]), 1)

        self.cb(OWNER, f"acc:{FRIEND}:viewer", 500)
        self.assertEqual(self.app.db.user(FRIEND)["role"], "viewer")
        self.assertTrue(any("Доступ надано" in s[1] for s in self.sent_to(FRIEND)))

        # шортс приходить обом, кнопки — лише власнику
        self.make_short("a")
        self.app.scheduler.tick(self.ts(11, "12:00"))
        vids = {s[-1]: s for s in tg.sent if s[0] == "video"}
        self.assertIsNotNone(vids[OWNER][2])
        self.assertIsNone(vids[FRIEND][2])
        self.assertTrue(vids[FRIEND][3].startswith("FILE"))  # відео не завантажується вдруге

        # глядач не може керувати
        item = self.app.db.items("status='notified'")[0]
        own = self.app.db.tg_msg(item["id"], FRIEND)
        self.cb(FRIEND, f"pub:{item['id']}", own["msg_id"])
        self.assertEqual(self.app.db.get_item(item["id"])["status"], "notified")
        self.msg(FRIEND, "/pause")
        self.assertIsNone(self.app.db.get("paused"))

        # підтвердження власником оновлює підпис в обох чатах
        self.press(f"pub:{item['id']}", item)
        edited_chats = {e[0] for e in tg.edits}
        self.assertTrue({self.app.db.tg_msg(item["id"], OWNER)["msg_id"], own["msg_id"]} <= edited_chats)

        # звіт іде всім
        self.app.scheduler.tick(self.ts(11, "13:00"))
        self.app.scheduler.tick(self.ts(11, "22:31"))
        self.assertTrue(any("Підсумок" in s[1] for s in self.sent_to(FRIEND)))

    def test_reject_and_remove(self):
        self.msg(STRANGER, "привіт")
        self.assertIsNone(self.app.db.user(STRANGER))  # без /start нічого не відбувається
        self.msg(STRANGER, "/start")
        self.cb(OWNER, f"acc:{STRANGER}:blocked", 501)
        n = len(self.app.bot.tg.sent)
        self.msg(STRANGER, "/start")
        self.assertEqual(len(self.app.bot.tg.sent), n)  # відхилених ігнорує мовчки
        self.cb(OWNER, f"acc:{STRANGER}:admin", 502)
        self.msg(STRANGER, "/pause")
        self.assertEqual(self.app.db.get("paused"), "1")
        self.cb(OWNER, f"acc:{STRANGER}:delete", 503)
        self.assertIsNone(self.app.db.user(STRANGER))
        self.cb(OWNER, f"acc:{OWNER}:delete", 504)
        self.assertEqual(self.app.db.user(OWNER)["role"], "admin")  # головного не прибрати

    def test_reply_buttons_and_pause(self):
        self.msg(OWNER, "/start")
        kb = self.sent_to(OWNER)[-1][2]
        self.assertIn("⏸ Пауза", [b["text"] for row in kb["keyboard"] for b in row])
        self.msg(OWNER, "⏸ Пауза")
        self.assertEqual(self.app.db.get("paused"), "1")
        self.make_short("a")
        self.app.scheduler.tick(self.ts(11, "12:00"))
        self.app.scheduler.tick(self.ts(11, "13:00"))
        self.assertFalse(any(s[0] == "video" for s in self.app.bot.tg.sent))
        self.assertEqual(self.app.db.slot(self.ts(11, "13:00"))["state"], "empty")
        self.msg(OWNER, "▶️ Продовжити")
        self.assertIsNone(self.app.db.get("paused"))
        self.msg(OWNER, "📋 Черга")
        self.assertIn("Черга: 1", self.sent_to(OWNER)[-1][1])

    def test_manual_slot_takes_waiting_short(self):
        import config
        self.make_short("a")
        self.app.scheduler.tick(self.ts(11, "18:03"))  # шортс узяв слот 19:00
        item = self.app.db.items("status='notified'")[0]
        self.assertEqual(item["slot_at"], self.ts(11, "19:00"))
        config.FAKE_NOW = self.ts(11, "18:03")
        ts = self.app.scheduler.add_manual_slot(10)
        item = self.app.db.get_item(item["id"])
        self.assertEqual(item["slot_at"], ts)
        self.assertEqual(self.app.db.slot(self.ts(11, "19:00"))["state"], "pending")
        self.assertIn("Слот: 18:13", self.app.bot.tg.edits[-1][1])
        self.press(f"pub:{item['id']}", item)
        self.app.scheduler.tick(ts)
        self.assertEqual(self.app.db.get_item(item["id"])["status"], "posted")



if __name__ == "__main__":
    unittest.main()
