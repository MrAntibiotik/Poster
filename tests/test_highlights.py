"""Хайлайти: окрема черга й свій час, лише YouTube як звичайне відео (не Shorts)."""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from config import load_config  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


class HighlightTest(FlowBase):
    def setUp(self):
        super().setUp()
        self.hl_dir = self.tmp / "Хайлайти"
        self.hl_dir.mkdir()
        cfg = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        cfg["highlights"] = {"enabled": True, "folder": str(self.hl_dir), "slots": ["18:00"]}
        (self.tmp / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.cfg = load_config(self.tmp / "config.json")
        self.app = self._app()

    def make_highlight(self, name, with_json=True, age=120):
        mp4 = self.hl_dir / f"{name}.mp4"
        mp4.write_bytes(b"\x00" * 1024)
        if with_json:
            mp4.with_suffix(".json").write_text(json.dumps({
                "status": "ready_to_post", "kind": "highlight",
                "youtube": {"title": f"Хайлайт {name}", "description": "Опис", "tags": ["wot"],
                            "hashtags": ["#wot"]},
            }, ensure_ascii=False), encoding="utf-8")
        old = time.time() - age
        for f in (mp4, mp4.with_suffix(".json")):
            if f.exists():
                os.utime(f, (old, old))
        return mp4

    def plan(self, now):
        config.FAKE_NOW = now
        return [(Path(it["mp4"]).stem, it["kind"], config.fmt_when(self.cfg, ts, now) if ts else None)
                for it, ts in self.app.scheduler.projection(now)]

    def test_own_lane_does_not_shift_shorts(self):
        self.make_short("a", age=300)
        self.make_highlight("h1", age=200)
        self.make_highlight("h2", age=100)
        self.make_short("b", age=50)
        config.FAKE_NOW = self.ts(11, "08:00")
        self.app.scheduler.scan()
        self.assertEqual(self.plan(self.ts(11, "08:00")), [
            ("a", "short", "сьогодні 10:00 (11.11)"),
            ("h1", "highlight", "сьогодні 18:00 (11.11)"),
            ("h2", "highlight", "завтра 18:00 (12.11)"),
            ("b", "short", "сьогодні 13:00 (11.11)")])
        h1 = next(i for i in self.app.db.queue() if Path(i["mp4"]).stem == "h1")
        self.assertEqual(sorted(h1["disabled"]), ["facebook", "instagram", "tiktok"])
        self.assertIn("🎞 хайлайт", self.app.queue_text())

    def test_notify_and_publish_youtube_only(self):
        self.make_short("a")
        self.make_highlight("h1")
        sch = self.app.scheduler
        sch.tick(self.ts(11, "09:00"))  # шортс на 10:00
        sch.tick(self.ts(11, "17:00"))  # хайлайт на 18:00
        hl = [i for i in self.app.db.items("status='notified'") if i["kind"] == "highlight"]
        self.assertEqual(len(hl), 1)
        self.assertEqual(hl[0]["slot_at"], self.ts(11, "18:00"))
        caps = [s[1] for s in self.app.bot.tg.sent if s[0] == "video" and "ХАЙЛАЙТ" in s[1]]
        self.assertEqual(len(caps), 1)
        item = hl[0]
        self.press(f"pub:{item['id']}", self.app.db.get_item(item["id"]))
        sch.tick(self.ts(11, "18:00"))
        item = self.app.db.get_item(item["id"])
        self.assertEqual(item["status"], "posted")
        self.assertEqual(list(item["results"]), ["youtube"])

    def test_auto_json_without_shorts_tag(self):
        self.make_highlight("2026-11-13 23-38 · rec1 · 05_бій5 · хайлайт", with_json=False)
        config.FAKE_NOW = self.ts(11, "08:00")
        self.app.scheduler.scan()
        it = self.app.db.queue("highlight")[0]
        data = json.loads(Path(it["json"]).read_text(encoding="utf-8"))
        self.assertEqual(data["kind"], "highlight")
        self.assertNotIn("#shorts", json.dumps(data, ensure_ascii=False).lower())
        self.assertFalse(data.get("auto_generated"))  # ШІ для Shorts хайлайти не переписує

    def test_same_time_as_short_slot_moves_one_minute(self):
        cfg = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        cfg["highlights"]["slots"] = ["13:00"]
        (self.tmp / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.cfg = load_config(self.tmp / "config.json")
        self.app = self._app()
        self.make_highlight("h1")
        config.FAKE_NOW = self.ts(11, "08:00")
        self.app.scheduler.scan()
        self.assertEqual(self.plan(self.ts(11, "08:00")), [("h1", "highlight", "сьогодні 13:01 (11.11)")])


class YouTubeHighlightBody(unittest.TestCase):
    def test_regular_video_no_shorts(self):
        from meta import PostMeta
        from platforms.youtube import YouTube
        cfg = {"platforms": {"youtube": {"privacy": "private", "add_shorts_hashtag": True, "category_id": "20"}},
               "paths": {"tokens": "/tmp"}, "secrets": {},
               "highlights": {"privacy": "unlisted", "category_id": "20"}}
        yt = YouTube(cfg)
        sent = {}

        class Req:
            def next_chunk(self, num_retries=0):
                return None, {"id": "VID", "status": {"privacyStatus": "unlisted"}}

        class Videos:
            def insert(self, part, body, media_body, notifySubscribers):
                sent.update(body)
                return Req()

        yt._api = lambda: SimpleNamespace(videos=lambda: Videos())
        import googleapiclient.http as gh
        orig = gh.MediaFileUpload
        gh.MediaFileUpload = lambda *a, **k: SimpleNamespace(_fd=None)
        try:
            out = yt.publish("x.mp4", PostMeta(yt_title="Бій #shorts", yt_description="Опис",
                                               hashtags=["#wot", "#shorts"], kind="highlight"))
        finally:
            gh.MediaFileUpload = orig
        self.assertEqual(out["url"], "https://youtu.be/VID")
        self.assertNotIn("#shorts", (sent["snippet"]["title"] + sent["snippet"]["description"]).lower())
        self.assertEqual(sent["status"]["privacyStatus"], "unlisted")


if __name__ == "__main__":
    unittest.main()
