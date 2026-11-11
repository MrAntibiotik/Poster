"""Обов'язкові хештеги в назві й підписах (#wot #wotua #mrantibiotik #танки)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meta import PostMeta, apply_always_hashtags  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402

TAGS = ["#wot", "#wotua", "#mrantibiotik", "#танки"]
CFG = {"always_hashtags": {"tags": TAGS, "in_title": True, "in_captions": True}}
LINE = "#wot #wotua #mrantibiotik #танки"


class UnitTest(unittest.TestCase):
    def meta(self, title, tt="", ig="", tags=()):
        return PostMeta(yt_title=title, tiktok_text=tt, instagram_text=ig, facebook_text=ig, hashtags=list(tags))

    def test_title_and_captions(self):
        m = apply_always_hashtags(self.meta("Камбек", "Камбек #shorts", "Камбек\n\n#wot", ["#wot", "#shorts"]), CFG)
        self.assertEqual(m.yt_title, f"Камбек {LINE}")
        self.assertEqual(m.tiktok_text, f"Камбек #shorts {LINE}")
        self.assertEqual(m.instagram_text, "Камбек\n\n#wot #wotua #mrantibiotik #танки")  # #wot не дублюється
        self.assertEqual(m.hashtags, ["#shorts"])  # те, що вже в назві, з опису прибрано

    def test_no_duplicates_in_title(self):
        m = apply_always_hashtags(self.meta("Камбек #WoT #танки на Прохорівці"), CFG)
        self.assertEqual(m.yt_title, f"Камбек на Прохорівці {LINE}")
        twice = apply_always_hashtags(self.meta(m.yt_title), CFG)  # повторне застосування нічого не ламає
        self.assertEqual(twice.yt_title, m.yt_title)

    def test_youtube_100_chars(self):
        m = apply_always_hashtags(self.meta("Дуже " * 40), CFG)
        self.assertLessEqual(len(m.yt_title), 100)
        self.assertTrue(m.yt_title.endswith(LINE))
        self.assertIn("…", m.yt_title)

    def test_disabled(self):
        m = apply_always_hashtags(self.meta("Камбек", "tt"), {"always_hashtags": {"tags": []}})
        self.assertEqual((m.yt_title, m.tiktok_text), ("Камбек", "tt"))
        m = apply_always_hashtags(self.meta("Камбек", "tt"), {"always_hashtags": {"tags": TAGS, "in_title": False}})
        self.assertEqual(m.yt_title, "Камбек")
        self.assertTrue(m.tiktok_text.endswith(LINE))


class FlowTest(FlowBase):
    def setUp(self):
        super().setUp()
        self.cfg["always_hashtags"] = CFG["always_hashtags"]
        self.cfg["ask_texts_before_publish"] = True
        self.published = {}

        class Capture:
            def __init__(s, key):
                s.key = key

            def publish(s, path, meta):
                self.published[s.key] = meta
                return {"id": "1", "url": f"https://{s.key}"}
        self.cfg["dry_run"] = False
        self.app.platforms = {k: Capture(k) for k in ("youtube", "tiktok", "instagram", "facebook")}

    def say(self, text):
        self.app.bot.handle({"update_id": 1, "message": {"text": text, "chat": {"id": 42, "type": "private"}}})

    def test_dialog_then_publish(self):
        self.make_short("a")
        self.app.scheduler.tick(self.ts(11, "12:00"))
        item = self.app.db.items("status='notified'")[0]
        cap = [s for s in self.app.bot.tg.sent if s[0] == "video"][0][1]
        self.assertIn(f"Титул a {LINE}", cap)  # у Telegram видно фінальну назву
        self.press(f"pub:{item['id']}", item)
        self.assertIn("автоматично додаються: " + LINE, self.app.bot.tg.sent[-1][1])
        self.say("Камбек на Прохорівці")
        self.say("shorts танки")
        self.app.scheduler.tick(self.ts(11, "13:00"))
        yt = self.published["youtube"]
        self.assertEqual(yt.yt_title, f"Камбек на Прохорівці {LINE}")
        self.assertEqual(yt.hashtags, ["#shorts"])
        self.assertEqual(self.published["tiktok"].tiktok_text, "Камбек на Прохорівці #shorts #танки #wot #wotua #mrantibiotik")
        self.assertTrue(self.published["instagram"].instagram_text.endswith("#wot #wotua #mrantibiotik"))
        self.assertEqual(self.app.db.get_item(item["id"])["status"], "posted")


class PrivacyTest(FlowBase):
    def test_private_marks_and_note(self):
        self.cfg["platforms"]["youtube"]["privacy"] = "private"
        self.cfg["dry_run"] = False

        class YT:
            def publish(s, path, meta):
                return {"id": "v1", "url": "https://youtube.com/shorts/v1", "privacy": "private",
                        "note": "YouTube зробив відео «приватне»…"}
        self.app.platforms = {"youtube": YT()}
        self.make_short("a")
        self.app.scheduler.tick(self.ts(11, "12:00"))
        cap = [s for s in self.app.bot.tg.sent if s[0] == "video"][0][1]
        self.assertIn("YT ✅🔒", cap)
        item = self.app.db.items("status='notified'")[0]
        self.press(f"pub:{item['id']}", item)
        self.app.scheduler.tick(self.ts(11, "13:00"))
        self.assertIn("🔒 приватне", self.app.bot.tg.edits[-1][1])
        self.assertTrue(any("YouTube зробив відео" in s[1] for s in self.app.bot.tg.sent if s[0] == "text"))


if __name__ == "__main__":
    unittest.main()
