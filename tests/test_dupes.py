"""Захист від повторних публікацій і «⛔ Стоп публікацій»."""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dupes  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


class DupesTest(FlowBase):
    def post(self, name):
        """Шортс пройшов увесь шлях: черга → опубліковано (файли в posted/)."""
        self.make_short(name)
        now = self.ts(10, "08:00")
        self.app.scheduler.tick(now)
        item = [i for i in self.app.db.items("1", order="id") if Path(i["mp4"]).stem == name][-1]
        self.app.db.update_item(item["id"], status="approved")
        self.app.publisher.start(item["id"])
        return self.app.db.get_item(item["id"])

    def copy_back(self, src: Path, name: str, game="WoT") -> Path:
        """Той самий файл знову в папці готових — з новим json (як від автонарізки)."""
        dst = self.cfg["paths"]["ready"] / game / f"{name}.mp4"
        shutil.copy2(src, dst)
        dst.with_suffix(".json").write_text('{"status": "ready_to_post", "youtube": {"title": "Знову"}}',
                                            encoding="utf-8")
        old = time.time() - 120
        os.utime(dst, (old, old))
        os.utime(dst.with_suffix(".json"), (old, old))
        return dst

    def dup_messages(self):
        return [s for s in self.app.bot.tg.sent if s[0] == "text" and "Схоже на повтор" in s[1]]

    def test_norm_name(self):
        self.assertEqual(dupes.norm_name("Rec_battle_03_dmg_3837_2"), "rec_battle_03_dmg_3837")
        self.assertEqual(dupes.norm_name("rec_battle_03_dmg_3837 (1)"), "rec_battle_03_dmg_3837")
        self.assertEqual(dupes.norm_name("battle_03"), "battle_03")   # номер бою не обрізається до «battle»
        self.assertEqual(dupes.norm_name("rec_battle_03"), "rec_battle_03")
        self.assertEqual(dupes.norm_name("Камбек - копія"), "камбек")

    def test_same_file_again_is_not_queued_and_asks(self):
        item = self.post("rec_battle_01_dmg_5004")
        self.assertEqual(item["status"], "posted")
        again = self.copy_back(Path(item["mp4"]), "rec_battle_01_dmg_5004")
        self.app.scheduler.tick(self.ts(10, "08:05"))
        dup = self.app.db.one("SELECT * FROM items WHERE status='duplicate'")
        self.assertIsNotNone(dup)
        self.assertEqual(dup["mp4"], str(again.resolve()))
        self.assertEqual(self.app.db.queue(), [])                     # у черзі нічого
        msgs = self.dup_messages()
        self.assertEqual(len(msgs), 1)
        self.assertIn("опубліковано", msgs[0][1])
        self.assertEqual(msgs[0][2]["inline_keyboard"][0][0]["callback_data"], f"dup:{dup['id']}:yes")
        self.app.scheduler.tick(self.ts(10, "08:10"))
        self.assertEqual(len(self.dup_messages()), 1)                # питає лише раз

        # «🗑 Ні, прибрати» — файл у «Скасовані», у черзі нічого
        cq = {"id": "1", "data": f"dup:{dup['id']}:no", "message": {"message_id": 5}}
        self.app.bot.on_callback(cq, 42)
        it = self.app.db.get_item(dup["id"])
        self.assertEqual(it["status"], "cancelled")
        self.assertFalse(again.exists())
        self.assertIn(str(self.cfg["paths"]["cancelled"]), it["mp4"])
        self.assertEqual(self.app.db.queue(), [])

    def test_same_name_rerender_yes_requeues(self):
        self.post("rec_battle_02_dmg_4100")
        self.make_short("rec_battle_02_dmg_4100 (1)")   # перенарізаний (інший вміст, та сама назва)
        self.app.scheduler.tick(self.ts(10, "08:05"))
        dup = self.app.db.one("SELECT * FROM items WHERE status='duplicate'")
        self.assertIsNotNone(dup)
        self.assertIn("та сама назва", self.dup_messages()[0][1])
        self.app.bot.on_callback({"id": "1", "data": f"dup:{dup['id']}:yes", "message": {"message_id": 5}}, 42)
        self.assertEqual(self.app.db.get_item(dup["id"])["status"], "queued")
        self.assertEqual([i["id"] for i in self.app.db.queue()], [dup["id"]])
        # повторне натискання — нічого не ламає
        self.app.bot.on_callback({"id": "2", "data": f"dup:{dup['id']}:no", "message": {"message_id": 5}}, 42)
        self.assertEqual(self.app.db.get_item(dup["id"])["status"], "queued")

    def test_twice_in_queue_before_posting(self):
        first = self.make_short("rec_battle_05_dmg_6000")
        self.app.scheduler.tick(self.ts(10, "08:00"))
        self.copy_back(first, "rec_battle_05_dmg_6000_2")
        self.app.scheduler.tick(self.ts(10, "08:05"))
        self.assertEqual(len(self.app.db.queue()), 1)
        self.assertIn("в роботі", self.dup_messages()[0][1])

    def test_different_shorts_are_not_duplicates(self):
        self.make_short("rec_battle_01_dmg_1000")
        self.make_short("rec_battle_02_dmg_2000")
        self.app.scheduler.tick(self.ts(10, "08:00"))
        self.assertEqual(len(self.app.db.queue()), 2)
        self.assertEqual(self.dup_messages(), [])


class StopTest(FlowBase):
    def test_stop_blocks_everything_until_go(self):
        self.make_short("stop_test_short")
        self.app.scheduler.tick(self.ts(10, "08:00"))
        self.app.scheduler.tick(self.ts(10, "09:01"))           # за годину до слоту 10:00 — сповіщення
        item = self.app.db.items("status='notified'")[0]
        self.app.db.update_item(item["id"], status="approved")  # «🚀 Опублікувати» — чекає свого слоту
        self.app.db.set("stopped", "1")
        self.app.publisher.start(item["id"])                    # «⚡ зараз» під час стопу
        self.assertEqual(self.app.db.get_item(item["id"])["status"], "approved")
        self.assertTrue(any("Публікації зупинено" in s[1] for s in self.app.bot.tg.sent if s[0] == "text"))
        self.app.scheduler.tick(self.ts(10, "10:01"))           # слот настав — під час стопу нічого
        self.assertEqual(self.app.db.get_item(item["id"])["status"], "approved")
        self.assertIn("Публікації зупинено", self.app.status_text())
        kb = self.app.bot.reply_keyboard(42)
        self.assertIn("✅ Відновити публікації", [b["text"] for row in kb["keyboard"] for b in row])
        self.app.db.set("stopped", None)
        self.app.scheduler.tick(self.ts(10, "10:02"))           # відновили — підтверджений виходить
        self.assertEqual(self.app.db.get_item(item["id"])["status"], "posted")

    def test_stop_during_publishing_resumes_remaining_platforms(self):
        self.make_short("stop_mid_short")
        self.app.scheduler.tick(self.ts(10, "08:00"))
        item = self.app.db.queue()[0]
        pub = self.app.publisher
        orig = pub._publish_one

        def one(key, it, meta):
            res = orig(key, it, meta)
            self.app.db.set("stopped", "1")                      # натиснули «Стоп» після першої платформи
            return res
        pub._publish_one = one
        self.app.db.update_item(item["id"], status="approved")
        pub.start(item["id"])
        it = self.app.db.get_item(item["id"])
        self.assertEqual(it["status"], "publishing")
        self.assertEqual(len([k for k, r in it["results"].items() if r.get("status") == "ok"]), 1)
        pub._publish_one = orig
        self.app.scheduler.tick(self.ts(10, "08:05"))           # стоп — не продовжує
        self.assertEqual(self.app.db.get_item(item["id"])["status"], "publishing")
        self.app.db.set("stopped", None)
        self.app.scheduler.tick(self.ts(10, "08:06"))
        self.assertEqual(self.app.db.get_item(item["id"])["status"], "posted")
