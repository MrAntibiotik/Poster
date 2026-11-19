"""Тести модулів платформ на підставних HTTP-відповідях (без мережі).

Перевіряють послідовність запитів, розбиття на шматки, обробку протермінованих токенів.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from app.core.meta import PostMeta  # noqa: E402
from app.platforms import PublishError, TokenExpired  # noqa: E402
from app.platforms import meta_graph, tiktok  # noqa: E402


class Resp:
    def __init__(self, data=None, status=200, text=""):
        self._data, self.status_code = data, status
        self.text = text or json.dumps(data)

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


class FakeHTTP:
    """Підміна модуля requests: маршрутизує за (метод, підрядок URL)."""

    RequestException = requests.RequestException

    def __init__(self, routes):
        self.routes = routes  # список (method, url_substring, handler|Resp|list[Resp])
        self.calls = []

    def _do(self, method, url, **kw):
        if "data" in kw and hasattr(kw["data"], "read"):
            kw["data"] = kw["data"].read()
        self.calls.append((method, url, kw))
        if method == "GET" and "rupload" in url:   # діагностика «скільки байтів дійшло» — окремо від маршрутів
            return Resp(getattr(self, "rupload_status", {"dc": "test"}))
        for m, sub, h in self.routes:
            if m == method and sub in url:
                if isinstance(h, list):
                    return h.pop(0) if len(h) > 1 else h[0]
                return h(url, kw) if callable(h) else h
        raise AssertionError(f"Неочікуваний запит {method} {url}")

    def get(self, url, **kw):
        return self._do("GET", url, **kw)

    def post(self, url, **kw):
        return self._do("POST", url, **kw)

    def put(self, url, **kw):
        return self._do("PUT", url, **kw)

    def delete(self, url, **kw):
        return self._do("DELETE", url, **kw)


FAST_TIME = types.SimpleNamespace(time=time.time, sleep=lambda s: None)


def make_cfg(tmp: Path) -> dict:
    return {
        "graph_api_version": "v25.0",
        "paths": {"tokens": tmp / "tokens"},
        "platforms": {"instagram": {"enabled": True, "share_to_feed": True, "single_attempt": False}, "facebook": {"enabled": True},
                      "tiktok": {"enabled": True, "public": False, "redirect_uri": "http://localhost:8765/callback/"},
                      "youtube": {"enabled": True}},
        "secrets": {"META_PAGE_TOKEN": "PAGE", "META_IG_TOKEN": "", "IG_USER_ID": "IG1", "FB_PAGE_ID": "P1",
                    "TIKTOK_CLIENT_KEY": "ck", "TIKTOK_CLIENT_SECRET": "cs"},
    }


META = PostMeta(yt_title="Т", tiktok_text="TT #wot", instagram_text="IG #wot", facebook_text="FB #wot")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_cfg(self.tmp)
        self.video = self.tmp / "v.mp4"
        self.video.write_bytes(b"x" * 2048)
        self._orig = (meta_graph.requests, meta_graph.time, tiktok.requests, tiktok.time)
        meta_graph.time = FAST_TIME
        tiktok.time = FAST_TIME
        from app.core import media
        self._settle, media.FILE_SETTLE = media.FILE_SETTLE, 0   # перевірка «файл не змінюється» — без очікування

    def tearDown(self):
        from app.core import media
        media.FILE_SETTLE = self._settle
        meta_graph.requests, meta_graph.time, tiktok.requests, tiktok.time = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def http(self, mod, routes):
        f = FakeHTTP(routes)
        mod.requests = f
        return f


class InstagramTest(Base):
    def test_publish_flow(self):
        uri = "https://rupload.facebook.com/ig-api-upload/v25.0/C1"
        f = self.http(meta_graph, [
            ("POST", "/IG1/media_publish", Resp({"id": "M1"})),
            ("POST", "/IG1/media", Resp({"id": "C1", "uri": uri})),
            ("POST", "rupload.facebook.com", Resp({"success": True})),
            ("GET", "/C1", [Resp({"status_code": "IN_PROGRESS"}), Resp({"status_code": "FINISHED"})]),
            ("GET", "/M1", Resp({"permalink": "https://www.instagram.com/reel/abc/"})),
        ])
        r = meta_graph.Instagram(self.cfg).publish(str(self.video), META)
        self.assertEqual((r["id"], r["url"], r["container"]), ("M1", "https://www.instagram.com/reel/abc/", "C1"))
        create = f.calls[0][2]["data"]
        self.assertEqual((create["media_type"], create["upload_type"], create["caption"]),
                         ("REELS", "resumable", "IG #wot"))
        up = f.calls[1]
        self.assertEqual(up[1], uri)
        self.assertEqual(up[2]["headers"], {"Authorization": "OAuth PAGE", "offset": "0", "file_size": "2048"})
        self.assertEqual(up[2]["data"], b"x" * 2048)

    def test_container_error(self):
        self.http(meta_graph, [
            ("POST", "/IG1/media", Resp({"id": "C1"})),
            ("POST", "rupload", Resp({"success": True})),
            ("GET", "/C1", Resp({"status_code": "ERROR", "status": "Error: 2207026"})),
        ])
        with self.assertRaisesRegex(PublishError, "2207026"):
            meta_graph.Instagram(self.cfg).publish(str(self.video), META)

    def test_expired_token(self):
        self.http(meta_graph, [("POST", "/IG1/media", Resp(
            {"error": {"code": 190, "message": "Error validating access token"}}, 400))])
        with self.assertRaises(TokenExpired):
            meta_graph.Instagram(self.cfg).publish(str(self.video), META)

    def test_no_stats_requests(self):
        f = self.http(meta_graph, [])   # статистику з Meta не запитуємо взагалі
        ig = meta_graph.Instagram(self.cfg)
        self.assertEqual((ig.get_views({"id": "M1"}), ig.get_stats({"id": "M1"}), ig.account_stats()), (None, {}, {}))
        self.assertEqual(f.calls, [])


class StatsTest(Base):
    def test_tiktok_stats(self):
        d = self.tmp / "tokens"
        d.mkdir(exist_ok=True)
        (d / "tiktok_token.json").write_text(json.dumps({"access_token": "AT", "refresh_token": "RT",
            "expires_at": int(time.time()) + 3600, "refresh_expires_at": int(time.time()) + 99999}))
        f = self.http(tiktok, [
            ("POST", "/v2/video/query/", Resp({"data": {"videos": [{"id": "7", "view_count": 5000, "like_count": 400,
                                                                    "comment_count": 12}]}, "error": {"code": "ok"}})),
            ("GET", "/v2/user/info/", Resp({"data": {"user": {"follower_count": 900, "likes_count": 15000,
                                                              "video_count": 40}}, "error": {"code": "ok"}})),
        ])
        tt = tiktok.TikTok(self.cfg)
        self.assertEqual(tt.get_stats({"post_id": "7"}), {"views": 5000, "likes": 400, "comments": 12})
        self.assertEqual(tt.get_stats({"id": "pub"}), {})  # приватний пост — без статистики
        self.assertEqual(tt.account_stats()["followers"], 900)
        self.assertIn("follower_count", f.calls[-1][2]["params"]["fields"])
        self.assertIn("user.info.stats", tiktok.SCOPES)


class FacebookTest(Base):
    def test_publish_flow(self):
        f = self.http(meta_graph, [
            ("POST", "/P1/video_reels", lambda url, kw: Resp(
                {"video_id": "V1", "upload_url": "https://rupload.facebook.com/video-upload/v25.0/V1"}
                if kw["data"]["upload_phase"] == "start" else {"success": True})),
            ("POST", "rupload.facebook.com/video-upload", Resp({"success": True})),
            ("GET", "/V1", [Resp({"status": {"video_status": "upload_complete",
                                             "publishing_phase": {"status": "in_progress"}}}),
                            Resp({"status": {"video_status": "ready",
                                             "publishing_phase": {"status": "complete"}}})]),
        ])
        r = meta_graph.Facebook(self.cfg).publish(str(self.video), META)
        self.assertEqual(r, {"id": "V1", "url": "https://www.facebook.com/reel/V1"})
        finish = f.calls[2][2]["data"]
        self.assertEqual((finish["upload_phase"], finish["video_state"], finish["description"]),
                         ("finish", "PUBLISHED", "FB #wot"))

    def test_no_stats_requests(self):
        f = self.http(meta_graph, [])
        fb = meta_graph.Facebook(self.cfg)
        self.assertEqual((fb.get_views({"id": "V1"}), fb.get_stats({"id": "V1"}), fb.account_stats()), (None, {}, {}))
        self.assertEqual(f.calls, [])


class TikTokTest(Base):
    def token(self, expires_in=3600):
        d = self.tmp / "tokens"
        d.mkdir(exist_ok=True)
        (d / "tiktok_token.json").write_text(json.dumps({
            "access_token": "AT", "refresh_token": "RT", "expires_at": int(time.time()) + expires_in,
            "refresh_expires_at": int(time.time()) + 86400 * 300}), encoding="utf-8")

    def routes(self, puts, privacy_opts=("SELF_ONLY",)):
        def put(url, kw):
            puts.append(kw["headers"]["Content-Range"])
            return Resp(None, 201 if kw["headers"]["Content-Range"].split("/")[0].endswith(
                str(int(kw["headers"]["Content-Range"].split("/")[1]) - 1)) else 206)
        return [
            ("POST", "creator_info/query", Resp({"data": {"creator_username": "mr_antibiotik",
                                                          "privacy_level_options": list(privacy_opts)},
                                                 "error": {"code": "ok"}})),
            ("POST", "/video/init/", lambda url, kw: Resp({"data": {"publish_id": "PUB1", "upload_url": "https://up/x"},
                                                           "error": {"code": "ok"}, "_req": kw["json"]})),
            ("PUT", "https://up/x", put),
            ("POST", "status/fetch", [Resp({"data": {"status": "PROCESSING_UPLOAD"}, "error": {"code": "ok"}}),
                                      Resp({"data": {"status": "PUBLISH_COMPLETE"}, "error": {"code": "ok"}})]),
        ]

    def test_small_file_private(self):
        self.token()
        puts = []
        f = self.http(tiktok, self.routes(puts))
        r = tiktok.TikTok(self.cfg).publish(str(self.video), META)
        self.assertEqual(r["id"], "PUB1")
        self.assertEqual(r["privacy"], "SELF_ONLY")
        self.assertIn("приватно", r["note"])
        self.assertEqual(puts, ["bytes 0-2047/2048"])
        init = [c for c in f.calls if "/video/init/" in c[1]][0][2]["json"]
        self.assertEqual(init["post_info"]["title"], "TT #wot")
        self.assertEqual(init["source_info"], {"source": "FILE_UPLOAD", "video_size": 2048,
                                               "chunk_size": 2048, "total_chunk_count": 1})
        self.assertEqual(f.calls[0][2]["headers"]["Authorization"], "Bearer AT")

    def test_big_file_chunks(self):
        self.token()
        big = self.tmp / "big.mp4"
        size = 105 * 1024 * 1024 + 123
        with open(big, "wb") as fh:
            fh.truncate(size)
        puts = []
        f = self.http(tiktok, self.routes(puts))
        tiktok.TikTok(self.cfg).publish(str(big), META)
        init = [c for c in f.calls if "/video/init/" in c[1]][0][2]["json"]["source_info"]
        mb = 1024 * 1024
        self.assertEqual((init["chunk_size"], init["total_chunk_count"]), (10 * mb, 10))
        self.assertEqual(len(puts), 10)
        self.assertEqual(puts[0], f"bytes 0-{10 * mb - 1}/{size}")
        self.assertEqual(puts[-1], f"bytes {90 * mb}-{size - 1}/{size}")

    def test_public_when_allowed(self):
        self.token()
        self.cfg["platforms"]["tiktok"]["public"] = True
        f = self.http(tiktok, self.routes([], ("PUBLIC_TO_EVERYONE", "SELF_ONLY")))
        f.routes[-1] = ("POST", "status/fetch", Resp({"data": {"status": "PUBLISH_COMPLETE",
                                                              "publicaly_available_post_id": [7311]},
                                                     "error": {"code": "ok"}}))
        r = tiktok.TikTok(self.cfg).publish(str(self.video), META)
        self.assertEqual(r["url"], "https://www.tiktok.com/@mr_antibiotik/video/7311")
        self.assertNotIn("note", r)

    def test_refresh_and_expired(self):
        self.token(expires_in=-10)
        f = self.http(tiktok, [("POST", "/oauth/token/", Resp({"error": "invalid_grant",
                                                              "error_description": "Refresh token is invalid"}))])
        with self.assertRaises(TokenExpired):
            tiktok.TikTok(self.cfg).publish(str(self.video), META)
        self.assertEqual(f.calls[0][2]["data"]["grant_type"], "refresh_token")

        f = self.http(tiktok, [("POST", "/oauth/token/", Resp({"access_token": "NEW", "refresh_token": "RT2",
                                                              "expires_in": 86400, "refresh_expires_in": 86400 * 365})),
                               ("POST", "creator_info", Resp({"data": {"creator_username": "x"}, "error": {"code": "ok"}}))])
        self.assertIn("@x", tiktok.TikTok(self.cfg).check())
        saved = json.loads((self.tmp / "tokens" / "tiktok_token.json").read_text())
        self.assertEqual((saved["access_token"], saved["refresh_token"]), ("NEW", "RT2"))

    def test_api_token_error(self):
        self.token()
        self.http(tiktok, [("POST", "creator_info", Resp({"error": {"code": "access_token_invalid",
                                                                    "message": "expired"}}, 401))])
        with self.assertRaises(TokenExpired):
            tiktok.TikTok(self.cfg).publish(str(self.video), META)

    def test_failed_status(self):
        self.token()
        f = self.http(tiktok, self.routes([]))
        f.routes[-1] = ("POST", "status/fetch", Resp({"data": {"status": "FAILED", "fail_reason": "file_format_check_failed"},
                                                     "error": {"code": "ok"}}))
        with self.assertRaisesRegex(PublishError, "file_format_check_failed"):
            tiktok.TikTok(self.cfg).publish(str(self.video), META)


class YouTubeTest(Base):
    def test_error_mapping(self):
        from app.platforms.youtube import YouTube

        class E(Exception):
            def __init__(self, status, text):
                super().__init__(text)
                self.resp = types.SimpleNamespace(status=status)

        self.assertIsInstance(YouTube._http_error(E(401, "Invalid Credentials")), TokenExpired)
        self.assertIn("квоту", str(YouTube._http_error(E(403, "quotaExceeded"))))
        self.assertIsInstance(YouTube._http_error(E(500, "backend")), PublishError)

    def test_no_token(self):
        from app.platforms.youtube import YouTube
        with self.assertRaises(TokenExpired):
            YouTube(self.cfg)._creds()


if __name__ == "__main__":
    unittest.main()


class FileCheckTest(unittest.TestCase):
    def test_file_check(self):
        from app.core import media
        d = Path(tempfile.mkdtemp())
        f = d / "a.mp4"
        f.write_bytes(b"x" * 1000)
        r = media.file_check(str(f), settle=0)
        self.assertEqual(r["size"], 1000)
        self.assertTrue(media.file_unchanged(str(f), r))
        f.write_bytes(b"y" * 1001)
        self.assertFalse(media.file_unchanged(str(f), r))
        with self.assertRaisesRegex(media.MediaError, "файлу немає"):
            media.file_check(str(d / "none.mp4"), settle=0)
        (d / "e.mp4").write_bytes(b"")
        with self.assertRaisesRegex(media.MediaError, "порожній"):
            media.file_check(str(d / "e.mp4"), settle=0)
        shutil.rmtree(d, ignore_errors=True)


class YouTubeReauthReminderTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        from app.platforms.youtube import YouTube
        self.yt = YouTube.__new__(YouTube)
        self.yt.tokens_dir = self.tmp
        self.yt.pcfg = {}

    def test_reminder_on_day_six(self):
        self.assertIsNone(self.yt.token_warning())                     # не знаємо, коли підключали — мовчимо
        self.yt.authorized_file.write_text(str(int(time.time()) - 2 * 86400), encoding="utf-8")
        self.assertIsNone(self.yt.token_warning())                     # 2-й день — рано
        self.yt.authorized_file.write_text(str(int(time.time()) - 6 * 86400), encoding="utf-8")
        self.assertIn("Авторизувати YouTube", self.yt.token_warning())
        self.yt.pcfg = {"weekly_reauth_reminder": False}
        self.assertIsNone(self.yt.token_warning())
