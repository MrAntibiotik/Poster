"""Instagram: однозначна обробка статусів контейнера — без дублів і без нового контейнера через тайм-аут.

Імітація Graph API (без мережі). Перевіряється саме кількість контейнерів / завантажень / публікацій."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.platforms import ContainerFailed, PublishError, StillProcessing, meta_graph  # noqa: E402
from tests.test_platforms import META, Base, Resp  # noqa: E402

BODY400 = '{"debug_info":{"retriable":false,"type":"ProcessingFailedError","message":"Request processing failed"}}'


class IgBase(Base):
    def setUp(self):
        super().setUp()
        from app.core import media
        self.media = media
        orig = (media.ig_probe, media.instagram_copy)
        self.addCleanup(lambda: (setattr(media, "ig_probe", orig[0]), setattr(media, "instagram_copy", orig[1])))
        self.cfg.update(ffprobe="ffprobe", ffmpeg="ffmpeg")
        self.cfg["paths"]["data"] = self.tmp / "data"
        self.probe = {}          # «ffprobe не прочитав» → вантажимо оригінал як є
        media.ig_probe = lambda path, ffprobe="ffprobe": dict(self.probe)
        self.clock = [1000.0]
        real_time = self._orig[1]     # справжній модуль time (Base відновлює його в tearDown; cleanup — після)
        meta_graph.time = type("T", (), {"time": staticmethod(lambda: self.clock[0]),
                                         "sleep": staticmethod(lambda s: self.clock.__setitem__(0, self.clock[0] + s))})
        self.addCleanup(lambda: setattr(meta_graph, "time", real_time))

    def routes(self, statuses, upload=None, publish=None):
        return [("POST", "/IG1/media_publish", publish or Resp({"id": "M1"})),
                ("GET", "/IG1/media", Resp({"data": [{"id": "MOLD", "permalink": "https://ig/old",
                                                      "timestamp": "2026-11-19T10:00:00+0000"}]})),
                ("POST", "/IG1/media", Resp({"id": "C1", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/C1"})),
                ("POST", "rupload", upload or Resp({"success": True})),
                ("GET", "/C1", [Resp(s) for s in statuses]),
                ("GET", "/M1", Resp({"permalink": "https://ig/M1"}))]

    def count(self, f):
        return {"containers": len([c for c in f.calls if c[0] == "POST" and c[1].endswith("/IG1/media")]),
                "uploads": len([c for c in f.calls if c[0] == "POST" and "rupload" in c[1]]),
                "publishes": len([c for c in f.calls if "media_publish" in c[1]])}


class StatusTest(IgBase):
    def test_finished_publishes(self):                                   # 3
        f = self.http(meta_graph, self.routes([{"status_code": "IN_PROGRESS"}, {"status_code": "FINISHED"}]))
        r = meta_graph.Instagram(self.cfg).publish(str(self.video), META)
        self.assertEqual((r["id"], r["url"]), ("M1", "https://ig/M1"))
        self.assertEqual(self.count(f), {"containers": 1, "uploads": 1, "publishes": 1})

    def test_in_progress_timeout_keeps_same_container(self):            # 1, 8
        f = self.http(meta_graph, self.routes([{"status_code": "IN_PROGRESS", "status": "In Progress"}] * 60))
        with self.assertRaises(StillProcessing) as cm:
            meta_graph.Instagram(self.cfg).publish(str(self.video), META)
        self.assertEqual(cm.exception.container_id, "C1")
        self.assertEqual(self.count(f), {"containers": 1, "uploads": 1, "publishes": 0})

    def test_pending_keeps_same_container(self):                        # 2
        f = self.http(meta_graph, self.routes([{"status_code": "PENDING"}] * 60))
        with self.assertRaises(StillProcessing):
            meta_graph.Instagram(self.cfg).publish(str(self.video), META)
        self.assertEqual(self.count(f)["containers"], 1)

    def test_error_is_confirmed_failure(self):                          # 4
        f = self.http(meta_graph, self.routes([{"status_code": "ERROR", "status": "Error: code 2207026"}]))
        with self.assertRaises(ContainerFailed) as cm:
            meta_graph.Instagram(self.cfg).publish(str(self.video), META)
        self.assertEqual((cm.exception.state, cm.exception.status_code), ("error", "ERROR"))
        self.assertEqual(self.count(f), {"containers": 1, "uploads": 1, "publishes": 0})

    def test_expired_is_confirmed_failure(self):                        # 5
        self.http(meta_graph, self.routes([{"status_code": "EXPIRED"}]))
        with self.assertRaises(ContainerFailed) as cm:
            meta_graph.Instagram(self.cfg).publish(str(self.video), META)
        self.assertEqual(cm.exception.state, "expired")

    def test_published_is_not_published_again(self):                    # 6
        f = self.http(meta_graph, self.routes([{"status_code": "PUBLISHED"}]))
        self.clock[0] = 1763546400.0   # 2026-11-19T10:00:00Z — щоб знайти щойно опублікований Reels
        r = meta_graph.Instagram(self.cfg).publish(str(self.video), META)
        self.assertEqual(self.count(f)["publishes"], 0)
        self.assertEqual(r["url"], "https://ig/old")

    def test_unknown_status_waits_same_container(self):                 # 7
        f = self.http(meta_graph, self.routes([{"status_code": "SOMETHING_NEW"}] * 60))
        with self.assertRaises(StillProcessing):
            meta_graph.Instagram(self.cfg).publish(str(self.video), META)
        self.assertEqual(self.count(f)["containers"], 1)

    def test_upload_400_is_not_a_verdict(self):
        # rupload 400, але статус FINISHED — публікуємо; статус вирішує, не відповідь завантаження
        f = self.http(meta_graph, self.routes([{"status_code": "FINISHED"}], upload=Resp(None, 400, BODY400)))
        r = meta_graph.Instagram(self.cfg).publish(str(self.video), META)
        self.assertEqual(r["id"], "M1")
        self.assertEqual(self.count(f), {"containers": 1, "uploads": 1, "publishes": 1})

    def test_container_saved_before_upload(self):
        seen = []
        f = self.http(meta_graph, self.routes([{"status_code": "FINISHED"}]))
        meta_graph.Instagram(self.cfg).publish(str(self.video), META,
                                               on_container=lambda rec: seen.append((rec["container"], len(f.calls))))
        self.assertEqual(seen, [("C1", 1)])            # одразу після створення, до завантаження файлу

    def test_finish_container_states(self):
        for code, state in (("IN_PROGRESS", "waiting"), ("PENDING", "waiting"), ("ERROR", "error"),
                            ("EXPIRED", "expired"), ("WHATEVER", "unknown")):
            f = self.http(meta_graph, self.routes([{"status_code": code}]))
            res = meta_graph.Instagram(self.cfg).finish_container("C1")
            self.assertEqual(res["state"], state, code)
            self.assertEqual(self.count(f), {"containers": 0, "uploads": 0, "publishes": 0})

    def test_media_publish_error_is_marked(self):
        self.http(meta_graph, self.routes([{"status_code": "FINISHED"}],
                                          publish=Resp({"error": {"code": 9007, "message": "not ready"}}, 400)))
        with self.assertRaisesRegex(PublishError, "^media_publish:"):
            meta_graph.Instagram(self.cfg).finish_container("C1")


class DurationTest(IgBase):
    def probe_of(self, dur):
        return {"vcodec": "h264", "width": 1080, "height": 1920, "fps": 60, "vfr": False, "pix_fmt": "yuv420p",
                "vkbps": 6000, "acodec": "aac", "akbps": 112, "arate": 48000, "channels": 2, "duration": dur,
                "trc": "", "primaries": "", "colorspace": "", "format": "mov,mp4,m4a,3gp,3g2,mj2", "size_mb": 0.002}

    def test_long_video_is_not_blocked(self):                           # 9
        self.probe = self.probe_of(75.0)
        f = self.http(meta_graph, self.routes([{"status_code": "FINISHED"}]))
        r = meta_graph.Instagram(self.cfg).publish(str(self.video), META)
        self.assertEqual(r["method_name"], "оригінал")   # сумісний оригінал іде без перекодування й без обрізання
        self.assertEqual(self.count(f)["containers"], 1)

    def test_outside_official_limits_no_container(self):
        for dur in (2.0, 16 * 60.0):
            self.probe = self.probe_of(dur)
            f = self.http(meta_graph, self.routes([{"status_code": "FINISHED"}]))
            with self.assertRaisesRegex(PublishError, "тривалість"):
                meta_graph.Instagram(self.cfg).publish(str(self.video), META)
            self.assertEqual(self.count(f)["containers"], 0)

    def test_incompatible_goes_as_copy(self):
        self.probe = {**self.probe_of(59.0), "akbps": 192}
        copy = self.tmp / "copy.mp4"
        copy.write_bytes(b"c" * 100)
        self.media.instagram_copy = lambda path, cfg, **kw: str(copy)
        self.http(meta_graph, self.routes([{"status_code": "FINISHED"}]))
        r = meta_graph.Instagram(self.cfg).publish(str(self.video), META)
        self.assertEqual(r["method_name"], "копія під вимоги Reels")


class CopyTest(unittest.TestCase):
    """Справжній ffmpeg: копія не обрізається, оригінал не змінюється, старі обрізані копії не беруться."""

    def setUp(self):
        import shutil
        import tempfile
        if not shutil.which("ffmpeg"):
            self.skipTest("немає ffmpeg")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = self.tmp / "long.mp4"
        from app.core import media
        self.media = media
        r = media.run_hidden(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=360x640:rate=30",
                              "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "62",
                              "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-b:a", "192k", str(self.src)],
                             timeout=300)
        self.assertEqual(r.returncode, 0)
        self.cfg = {"paths": {"data": str(self.tmp / "data")}, "ffmpeg": "ffmpeg", "ffprobe": "ffprobe",
                    "platforms": {"instagram": {}}}

    def test_copy_not_trimmed_original_untouched(self):                 # 10, 12
        import hashlib
        before = hashlib.md5(self.src.read_bytes()).hexdigest()
        out = self.media.instagram_copy(str(self.src), self.cfg, mode="safe30")
        p = self.media.ig_probe(out, "ffprobe")
        self.assertGreater(p["duration"], 61.5)                         # не 59.5
        self.assertLessEqual(p["akbps"], 128)
        self.assertEqual(p["arate"], 48000)
        self.assertEqual(hashlib.md5(self.src.read_bytes()).hexdigest(), before)

    def test_old_trimmed_cache_not_used(self):                          # 11
        import hashlib
        import os
        cache = self.tmp / "data" / "previews"
        cache.mkdir(parents=True)
        old_key = hashlib.md5(f"{self.src}|{os.path.getmtime(self.src)}|45.0|sdr3|t59.5".encode()).hexdigest()[:10]
        old = cache / f"{self.src.stem}_{old_key}_igl.mp4"
        old.write_bytes(b"old trimmed copy")
        out = self.media.instagram_copy(str(self.src), self.cfg, mode="safe30")
        self.assertNotEqual(Path(out), old)
        self.assertGreater(self.media.ig_probe(out, "ffprobe")["duration"], 61.5)


class TestVideoTest(IgBase):
    def test_test_video_never_publishes(self):
        f = self.http(meta_graph, self.routes([{"status_code": "IN_PROGRESS"}, {"status_code": "FINISHED"}]))
        clip_dir = self.tmp / "previews"
        clip_dir.mkdir()
        (clip_dir / "ig_api_test.mp4").write_bytes(b"v" * 4096)
        with mock.patch.object(meta_graph.time, "sleep", lambda s: None):
            out = meta_graph.Instagram(self.cfg).test_video(clip_dir)
        self.assertIn("не публікуємо", "\n".join(out))
        self.assertEqual(self.count(f)["publishes"], 0)


if __name__ == "__main__":
    unittest.main()
