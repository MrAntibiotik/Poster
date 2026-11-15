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

    def test_publish_all_asks_title_and_tags_for_each(self):
        self.cfg["on_missed_approval"] = "wait"
        self.cfg["ask_texts_before_publish"] = True
        self.sch.tick(self.ts(11, "09:00"))
        self.sch.tick(self.ts(11, "10:00"))
        self.sch.tick(self.ts(11, "12:00"))
        self.sch.tick(self.ts(11, "13:00"))
        a, b = self.db.items("status='notified'", order="slot_at")
        say = lambda t: self.app.bot.handle({"update_id": 1, "message": {  # noqa: E731
            "message_id": 50, "text": t, "chat": {"id": 42, "type": "private"}}})
        self.button("pall:0", a)
        self.assertIn(f"назву</b> для шортса #{a['id']} · ще 1", self.app.bot.tg.sent[-1][1])
        self.assertEqual(self.db.get_item(a["id"])["status"], "notified")  # без назви — не публікуємо
        say("Перший")
        say("wot танки")
        self.assertEqual(self.db.get_item(a["id"])["status"], "posted")
        self.assertIn(f"назву</b> для шортса #{b['id']}", self.app.bot.tg.sent[-1][1])
        say("Другий")
        say("-")
        self.assertEqual(self.db.get_item(b["id"])["status"], "posted")
        import json
        self.assertIn("Перший", json.dumps(json.loads(Path(self.db.get_item(a["id"])["json"]).read_text(encoding="utf-8")),
                                            ensure_ascii=False))


class QueueCardTest(FlowBase):
    def test_new_short_shows_in_chat_then_card_is_replaced(self):
        self.cfg["telegram"]["queue_cards"] = True
        tg, db, sch = self.app.bot.tg, self.app.db, self.app.scheduler
        self.make_short("a")
        import config
        config.FAKE_NOW = self.ts(11, "08:00")
        sch.scan()
        item = db.items("status='queued'")[0]
        card = db.tg_msg(item["id"], 42)
        self.assertIsNotNone(card)  # одразу в чаті
        sent = [m for m in tg.sent if "Новий у черзі" in str(m[1])]
        self.assertTrue(sent)
        self.assertIn("📦 У черзі → сьогодні 10:00", sent[-1][1])
        self.assertIn("qt:", str(sent[-1][2]))
        sch.tick(self.ts(11, "09:00"))  # за годину до слоту — картка на підтвердження замість картки черги
        self.assertIn(card["msg_id"], tg.deleted)
        new = db.tg_msg(item["id"], 42)
        self.assertNotEqual(new["msg_id"], card["msg_id"])
        self.assertEqual(db.get_item(item["id"])["status"], "notified")


    def test_confirm_from_queue_card(self):
        self.cfg["telegram"]["queue_cards"] = True
        self.cfg["ask_texts_before_publish"] = True
        tg, db, sch = self.app.bot.tg, self.app.db, self.app.scheduler
        self.make_short("c")
        import config
        config.FAKE_NOW = self.ts(11, "08:00")
        sch.scan()
        item = db.items("status='queued'")[0]
        kb = str(self.app.bot.keyboard(item))
        self.assertIn(f"qa:{item['id']}", kb)
        self.assertIn("10:00", kb)
        say = lambda t: self.app.bot.handle({"update_id": 1, "message": {  # noqa: E731
            "message_id": 60, "text": t, "chat": {"id": 42, "type": "private"}}})
        self.app.bot.handle({"update_id": 1, "callback_query": {"id": "c", "data": f"qa:{item['id']}",
                             "message": {"message_id": db.tg_msg(item["id"], 42)["msg_id"], "chat": {"id": 42}, "text": ""}}})
        self.assertIn("Крок 1/2", tg.sent[-1][1])
        say("Моя назва")
        say("wot")
        it = db.get_item(item["id"])
        self.assertEqual((it["status"], it["slot_at"]), ("approved", self.ts(11, "10:00")))

    def test_cards_only_for_today_rest_as_list(self):
        self.cfg["telegram"]["queue_cards"] = True
        tg, db, sch = self.app.bot.tg, self.app.db, self.app.scheduler
        for n in "abcdefg":
            self.make_short(n, age=1000 - ord(n))
        import config
        config.FAKE_NOW = self.ts(11, "08:00")
        sch.scan()  # 5 слотів сьогодні (10…21), 2 — завтра
        with_card = [i for i in db.items("status='queued'") if db.tg_msg(i["id"], 42)]
        self.assertEqual(len(with_card), 5)
        self.assertTrue(any("Нові шортси в черзі: 2" in str(m[1]) for m in tg.sent))  # решта — списком
        config.FAKE_NOW = self.ts(12, "08:00")  # наступного дня завтрашні стають «сьогоднішніми»
        for i in with_card:  # (вчорашні вже опубліковано б — прибираємо з черги для тесту)
            db.update_item(i["id"], status="posted")
        self.app.bot.heal()
        later = [i for i in db.items("status='queued'") if db.tg_msg(i["id"], 42)]
        self.assertEqual(len(later), 2)

    def test_queued_without_card_gets_one(self):
        db, sch = self.app.db, self.app.scheduler
        self.make_short("b")
        import config
        config.FAKE_NOW = self.ts(11, "08:00")
        sch.scan()  # чергу поставлено ще старою версією — картки не було
        self.cfg["telegram"]["queue_cards"] = True
        item = db.items("status='queued'")[0]
        self.assertIsNone(db.tg_msg(item["id"], 42))
        self.app.bot.heal()
        self.assertIsNotNone(db.tg_msg(item["id"], 42))


if __name__ == "__main__":
    unittest.main()
