"""Історія публікацій: кожна спроба записується, CSV і підсумок для аналізу."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config, media  # noqa: E402
from app.insights import history  # noqa: E402
from app.platforms import PublishError  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


BODY = ('{"debug_info":{"retriable":false,"type":"ProcessingFailedError",'
        '"message":"Request processing failed"}}')


class Ok:
    def publish(self, path, meta):
        return {"id": "y1", "url": "https://yt/1"}


class IGOk:
    diag = None

    def publish(self, path, meta):
        self.diag = {"file": "x_ig.mp4", "size_mb": 23.4, "variant": "копія під вимоги Reels",
                     "rupload": {"http": 200},
                     "stages": [{"stage": "rupload", "ok": True, "seconds": 21.4},
                                {"stage": "обробка", "ok": True, "seconds": 2}]}
        return {"id": "M1", "url": "https://ig/1"}


class IGBad:
    diag = None

    def publish(self, path, meta):
        self.diag = {"file": "x_ig.mp4", "size_mb": 44, "container": "C9",
                     "rupload": {"http": 400, "body": BODY},
                     "polls": [[10, "IN_PROGRESS", "In Progress"], [30, "IN_PROGRESS", "Media is still being processed."]],
                     "stages": [{"stage": "контейнер", "ok": True, "seconds": 1},
                                {"stage": "rupload", "ok": False, "seconds": 35},
                                {"stage": "обробка", "ok": False, "seconds": 180}]}
        raise PublishError("Graph API HTTP 400: ProcessingFailedError")


class HistoryTest(FlowBase):
    def setUp(self):
        super().setUp()
        self.cfg["dry_run"] = False
        self.cfg["platforms"]["instagram"]["single_attempt"] = True
        config.FAKE_NOW = self.ts(11, "12:00")
        self.sent = []
        self.app.bot.instagram_manual = lambda item_id, meta: self.sent.append(item_id)

    def publish(self, name, dur):
        self.make_short(name)
        self.app.scheduler.scan()
        it = [i for i in self.app.db.queue() if name in i["mp4"]][0]
        probe = {"duration": dur, "width": 1080, "height": 1920, "fps": 60, "vkbps": 19000, "size_mb": 140}
        with mock.patch.object(media, "ig_probe", return_value=probe):
            self.app.publisher.start(it["id"])
        return it["id"]

    def rows(self, item_id):
        return {r["platform"]: r for r in self.app.db.q("SELECT * FROM history WHERE item_id=?", (item_id,))}

    def test_ok_and_rejected_and_long(self):
        self.app.platforms = {"youtube": Ok(), "instagram": IGOk()}
        a = self.publish("a", 52.0)
        r = self.rows(a)
        self.assertEqual(r["youtube"]["result"], "ok")
        self.assertEqual(r["instagram"]["result"], "ok")
        self.assertEqual((r["instagram"]["duration"], r["instagram"]["upload_http"]), (52.0, 200))
        self.assertEqual(r["instagram"]["method"], "копія під вимоги Reels")

        self.app.platforms = {"youtube": Ok(), "instagram": IGBad()}
        b = self.publish("b", 50.0)
        r = self.rows(b)
        self.assertEqual(r["instagram"]["result"], "manual_rejected")
        self.assertEqual(r["instagram"]["upload_http"], 400)
        self.assertIn("ProcessingFailed", r["instagram"]["error"])
        ig = r["instagram"]
        self.assertEqual(ig["stage"], "rupload (завантаження)")            # де зламалось
        self.assertEqual(ig["upload_body"], BODY)                           # повний debug_info
        self.assertEqual((ig["container_id"], ig["container_status"]), ("C9", "IN_PROGRESS"))
        self.assertEqual(ig["container_status_text"], "Media is still being processed.")
        self.assertEqual((ig["duration"], ig["size_mb"]), (50.0, 140))

        c = self.publish("c", 59.0)   # стару межу «понад 55 с — лише вручну» прибрано: пробуємо через API
        self.assertEqual(self.rows(c)["instagram"]["result"], "manual_rejected")

        csv_text = history.to_csv(self.app).decode("utf-8-sig")
        self.assertIn("manual_rejected", csv_text)
        self.assertIn("52.0", csv_text)
        self.assertIn("Request processing failed", csv_text)
        s = history.summary(self.app)
        self.assertIn("46–55 с: 1 з 2 пройшло", s)
        self.assertIn("HTTP 200: 1 з 1", s)
        self.assertIn("по днях", s)
        self.assertIn("🧩 Як вантажилось", s)
        self.assertIn("11.11 1/3", s)                                       # a — ok, b і c — відмова
        self.assertIn("HTTP 400: 0 з 2", s)   # b і c (обидва — IGBad)
        self.assertIn("rupload (завантаження) — 2", s)
        self.assertIn("IN_PROGRESS — 2", s)


class ContainerDetailsTest(unittest.TestCase):
    def test_media_publish_stage(self):
        plat = type("P", (), {"last_container": {"container_id": "C1", "container_status": "FINISHED",
                                                 "container_status_text": "Finished", "stage": "media_publish"}})()
        d = history.container_details(plat)
        self.assertEqual(d["stage"], "media_publish")
        self.assertEqual(d["container_status"], "FINISHED")


if __name__ == "__main__":
    unittest.main()


class ContainersLogTest(unittest.TestCase):
    def test_format_and_file(self):
        import tempfile
        from app.insights import containers
        d = Path(tempfile.mkdtemp())
        cfg = {"paths": {"data": str(d)}}
        rec = {"container": "18454129246135562", "item_id": 63, "duration": 52.3, "size_mb": 44.0, "codec": "h264",
               "resolution": "1080×1920", "fps": 60, "bitrate_kbps": 5800,
               "upload": "❌ ні, HTTP 400 за 35 с", "status_code": "IN_PROGRESS",
               "status": "Media is still being processed.",
               "error": containers.error_message(BODY, "IN_PROGRESS", "x", "fallback"),
               "processing_s": 4080, "outcome": "контейнер завис"}
        text = containers.report(cfg, rec)
        for line in ("Instagram container:", "ID: 18454129246135562", "Video duration: 52.3 с", "File size: 44.0 МБ",
                     "Codec: h264", "Resolution: 1080×1920", "FPS: 60", "Bitrate: 5800 кбіт/с",
                     "Upload completed: ❌ ні, HTTP 400 за 35 с", "Last status_code: IN_PROGRESS",
                     "Last status: Media is still being processed.",
                     "Last error_message: ProcessingFailedError: Request processing failed",
                     "Processing time: 4080 с"):
            self.assertIn(line, text)
        self.assertEqual(containers.last(cfg, 5)[0]["container"], "18454129246135562")
        self.assertEqual(containers.error_message(None, "ERROR", "Error: code 2207026", ""), "Error: code 2207026")
