"""Публікатор Instagram: контейнер чекаємо не довше 10 хв (рішення користувача). Не готово — цей контейнер
покидаємо (його ніхто не опублікує, дубля немає), нове завантаження через 30 хв, ще одне через 60 хв, далі —
у Telegram. ERROR / EXPIRED — інший спосіб. Між спробами інші шортси публікуються як завжди."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config  # noqa: E402
from app.platforms import ContainerFailed, PublishError, StillProcessing  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


class Ok:
    def __init__(self):
        self.n = 0

    def publish(self, path, meta):
        self.n += 1
        return {"id": f"y{self.n}", "url": f"https://yt/{self.n}"}


class FakeIG:
    """Імітація Instagram: що станеться з кожним контейнером."""
    METHODS = (("копія під вимоги Reels", ""), ("легка копія 30 к/с", "safe30"))

    def __init__(self):
        self.uploads, self.checks, self.publishes, self.waits = [], [], [], []
        self.inline = {}       # cid → "ok" | "wait" | "lost" | "error" | "expired"
        self.status = {}       # cid → status_code для фонових перевірок
        self.default_inline = "wait"

    def methods_for(self, path):
        return self.METHODS

    def container_record(self, outcome):
        return {"outcome": outcome}

    def _new(self, method, on_container):
        cid = f"C{len(self.uploads) + 1}"
        self.uploads.append((cid, method))
        rec = {"container": cid, "method": method, "method_name": self.METHODS[method][0], "file_mb": 44}
        if on_container:
            on_container(rec)
        return cid, rec

    def upload_only(self, path, meta, method=0, on_container=None):
        cid, rec = self._new(method, on_container)
        return {**rec, "upload_note": "", "upload_http": 200}

    def publish(self, path, meta, method=0, on_container=None, inline_wait=None):
        self.waits.append(inline_wait)
        cid, rec = self._new(method, on_container)
        how = self.inline.get(cid, self.default_inline)
        if how == "ok":
            self.publishes.append(cid)
            return {"id": "M" + cid, "url": f"https://ig/{cid}", **rec}
        if how in ("error", "expired"):
            raise ContainerFailed(f"контейнер {how.upper()}", cid, how, how.upper(), "x")
        e = StillProcessing("ще обробляється", cid)
        e.upload_lost = how == "lost"
        raise e

    def finish_container(self, cid, since=0):
        self.checks.append(cid)
        code = self.status.get(cid, "IN_PROGRESS")
        if code == "MEDIA_PUBLISH_FAIL":
            raise PublishError("media_publish: (#9007) not ready")
        state = {"FINISHED": "ready", "IN_PROGRESS": "waiting", "PENDING": "waiting", "ERROR": "error",
                 "EXPIRED": "expired", "PUBLISHED": "already"}.get(code, "unknown")
        res = {"state": state, "status_code": code, "status": code.title(), "time": "t"}
        if state == "ready":
            self.publishes.append(cid)
            return {**res, "state": "published", "id": "M" + cid, "url": f"https://ig/{cid}"}
        if state == "already":
            return {**res, "id": "MX", "url": "https://ig/already"}
        return res


class IgFlowBase(FlowBase):
    silent = False

    def setUp(self):
        super().setUp()
        self.cfg["dry_run"] = False
        self.cfg["platforms"]["instagram"]["silent_wait"] = self.silent
        self.yt, self.ig = Ok(), FakeIG()
        self.app.platforms = {"youtube": self.yt, "instagram": self.ig}
        self.sent = []
        self.app.bot.instagram_manual = lambda item_id, meta: self.sent.append(item_id)
        self.now = self.ts(11, "12:00")
        config.FAKE_NOW = self.now

    def publish(self, *names):
        for n in names:
            self.make_short(n)
        self.app.scheduler.scan(self.now)
        ids = [i["id"] for i in self.app.db.queue()]
        for i in ids:
            self.app.publisher.start(i)
        return ids

    def res(self, item_id):
        return self.app.db.get_item(item_id)["results"].get("instagram") or {}

    def later(self, minutes):
        for _ in range(minutes):
            self.now += 60
            config.FAKE_NOW = self.now
            self.app.publisher.finish_instagram(self.now)


class TenMinutesTest(IgFlowBase):
    def test_instant_publish(self):
        self.ig.default_inline = "ok"
        a, = self.publish("a")
        self.assertEqual(self.res(a)["status"], "ok")
        self.assertEqual(self.ig.waits, [600])                             # чекаємо не більше 10 хв

    def test_not_ready_in_10_min_reupload_later_then_publish(self):
        a, = self.publish("a")                                             # C1: 10 хв — не готово
        self.assertEqual(self.res(a)["status"], "ig_next")
        self.assertEqual(self.ig.checks, [])                               # фонового очікування немає
        self.later(29)
        self.assertEqual(len(self.ig.uploads), 1)                          # до 30 хв — нічого
        self.ig.inline["C2"] = "ok"
        self.later(2)
        self.assertEqual(self.ig.uploads, [("C1", 0), ("C2", 0)])          # нове завантаження того самого відео
        self.assertEqual(self.res(a)["status"], "ok")
        self.assertEqual(self.ig.publishes, ["C2"])                        # опубліковано один раз — без дубля

    def test_three_tries_then_telegram(self):
        a, = self.publish("a")
        self.later(4 * 60)
        self.assertEqual(len(self.ig.uploads), 3)                          # перше + 2 повторні, не більше
        self.assertEqual(self.res(a)["status"], "manual")
        self.assertEqual(self.sent, [a])
        rows = self.app.db.q("SELECT result FROM history WHERE item_id=? AND result='stuck_retry'", (a,))
        self.assertEqual(len(rows), 3)

    def test_details_show_waiting_not_error(self):
        a, = self.publish("a")
        text = self.app.bot.details_text(a)
        self.assertNotIn("❌ <b>Instagram</b>", text)                      # чекає повтору — це не помилка
        self.assertIn("⏳ <b>Instagram</b>", text)
        self.assertIn("повторю завантаження", text)

    def test_others_publish_while_waiting_for_retry(self):
        a, b = self.publish("a", "b")
        self.assertEqual(self.res(a)["status"], "ig_next")
        self.assertEqual(self.ig.uploads[1], ("C2", 0))                     # #b одразу, не чекає #a
        self.assertEqual(self.yt.n, 2)

    def test_retry_button_does_not_upload_early(self):
        a, = self.publish("a")
        self.app.publisher.start(a)                                        # «🔁» / перезапуск — до призначеного часу
        self.assertEqual(len(self.ig.uploads), 1)
        self.assertEqual(self.yt.n, 1)

    def test_error_goes_to_next_method(self):
        self.ig.inline["C1"] = "error"
        self.ig.inline["C2"] = "ok"
        a, = self.publish("a")
        self.later(2)
        self.assertEqual(self.ig.uploads, [("C1", 0), ("C2", 1)])
        self.assertEqual(self.res(a)["status"], "ok")

    def test_expired_goes_to_next_method(self):
        self.ig.inline["C1"] = "expired"
        a, = self.publish("a")
        self.later(2)
        self.assertEqual(self.ig.uploads[1], ("C2", 1))

    def test_long_video_not_routed_to_telegram(self):
        from unittest import mock
        from app.core import media
        self.ig.default_inline = "ok"
        with mock.patch.object(media, "ig_probe", return_value={"duration": 59.2}):
            a, = self.publish("a")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.res(a)["status"], "ok")


class RestartTest(IgFlowBase):
    def crash_after_container(self):
        def crash(path, meta, method=0, on_container=None, inline_wait=None):
            self.ig._new(method, on_container)
            raise KeyboardInterrupt
        self.ig.publish = crash
        self.make_short("a")
        self.app.scheduler.scan(self.now)
        a = self.app.db.queue()[0]["id"]
        with self.assertRaises(KeyboardInterrupt):
            self.app.publisher.start(a)
        self.app = self._app()                                             # «перезапуск»
        self.ig = FakeIG()
        self.app.platforms = {"youtube": self.yt, "instagram": self.ig}
        self.app.scheduler.resume_publishing()
        return a

    def test_restart_finished_container_published_once(self):
        a = self.crash_after_container()
        self.ig.status["C1"] = "FINISHED"
        self.later(13)
        self.assertEqual(self.res(a)["status"], "ok")
        self.assertEqual(self.ig.uploads, [])                              # не вантажили вдруге

    def test_restart_published_status_no_duplicate(self):
        a = self.crash_after_container()
        self.ig.status["C1"] = "PUBLISHED"
        self.later(13)
        self.assertEqual(self.res(a)["status"], "ok")
        self.assertEqual(self.ig.publishes, [])

    def test_restart_stuck_container_retries_after_10_min(self):
        a = self.crash_after_container()
        self.later(12)
        self.assertEqual(self.res(a)["status"], "ig_next")
        self.assertEqual(self.ig.uploads, [])                              # нове — лише через паузу


class SilentFlowTest(IgFlowBase):
    silent = True

    def test_silent_one_check_then_retry_later(self):
        a, = self.publish("a")
        self.later(19)
        self.assertEqual(self.ig.checks, [])                               # 19 хв — жодного запиту
        self.later(1)
        self.assertEqual(self.ig.checks, ["C1"])                           # одна перевірка: не готово
        self.assertEqual(self.res(a)["status"], "ig_next")

    def test_silent_media_publish_error_retries_same_container(self):
        a, = self.publish("a")
        self.ig.status["C1"] = "MEDIA_PUBLISH_FAIL"
        self.later(40)
        self.assertEqual(len(self.ig.uploads), 1)
        self.assertEqual(self.res(a)["status"], "error")
        self.assertIn("media_publish", self.res(a)["error"])

    def test_silent_finished_publishes(self):
        a, = self.publish("a")
        self.ig.status["C1"] = "FINISHED"
        self.later(20)
        self.assertEqual(self.res(a)["status"], "ok")


class LegacyTest(IgFlowBase):
    def test_container_from_old_version_waiting_hours_is_retried_now(self):
        a, = self.publish("a")
        results = dict(self.app.db.get_item(a)["results"])                 # як #73: «processing» 6 годин
        results["instagram"] = {"status": "processing", "container": "C9", "since": self.now - 391 * 60,
                                "next_at": self.now + 20 * 60, "method": 0, "method_name": "копія під вимоги Reels"}
        self.app.db.update_item(a, results=results)
        self.later(1)
        self.assertEqual(self.ig.checks, ["C9"])                           # одна перевірка — одразу
        self.assertEqual(self.res(a)["status"], "ig_next")


class RealInstagramFlowTest(IgFlowBase):
    """Справжній модуль Instagram + публікатор (імітація лише HTTP): тут ловляться помилки стику (як аварія #73)."""

    def test_full_cycle_400_then_reupload_publishes(self):
        from app.core import media
        from app.platforms import meta_graph
        from tests.test_platforms import FakeHTTP, Resp
        body = '{"debug_info":{"retriable":false,"type":"ProcessingFailedError","message":"Request processing failed"}}'
        self.cfg["secrets"].update(META_PAGE_TOKEN="PAGE", IG_USER_ID="IG1")
        orig = (meta_graph.time, meta_graph.requests, media.FILE_SETTLE)
        self.addCleanup(lambda: (setattr(meta_graph, "time", orig[0]), setattr(meta_graph, "requests", orig[1]),
                                 setattr(media, "FILE_SETTLE", orig[2])))
        clock = [1_763_000_000.0]
        meta_graph.time = type("T", (), {"time": staticmethod(lambda: clock[0]),
                                         "sleep": staticmethod(lambda s: clock.__setitem__(0, clock[0] + s))})
        media.FILE_SETTLE = 0
        n = {"c": 0}

        def create(url, kw):
            n["c"] += 1
            return Resp({"id": f"C{n['c']}", "uri": f"https://rupload.facebook.com/ig-api-upload/v25.0/C{n['c']}"})
        f = FakeHTTP([("POST", "/IG1/media_publish", Resp({"id": "M2"})),
                      ("POST", "/IG1/media", create),
                      ("POST", "rupload", [Resp(None, 400, body), Resp({"success": True})]),
                      ("GET", "/C1", [Resp({"status_code": "IN_PROGRESS", "status": "In Progress"})] * 999),
                      ("GET", "/C2", Resp({"status_code": "FINISHED"})),
                      ("GET", "/M2", Resp({"permalink": "https://ig/M2"}))])
        f.rupload_status = {"offset": 0}
        meta_graph.requests = f
        self.app.platforms = {"instagram": meta_graph.Instagram(self.cfg)}
        a, = self.publish("a")
        self.assertNotEqual(self.app.db.get_item(a)["status"], "failed")    # без «аварійно завершилась»
        r = self.res(a)
        self.assertEqual(r["status"], "ig_next")
        self.assertIn("не обробив за 10 хв", r["note"])
        polls_c1 = [c for c in f.calls if c[0] == "GET" and c[1].endswith("/C1")]
        self.assertLessEqual(len(polls_c1), 12)                            # ~10 хв, рідко
        self.later(31)
        self.assertEqual(self.res(a)["status"], "ok")
        self.assertEqual(n["c"], 2)                                        # рівно два контейнери, один Reels
        rows = [x["result"] for x in self.app.db.q("SELECT result FROM history WHERE item_id=? AND platform='instagram'",
                                                   (a,))]
        self.assertIn("stuck_retry", rows)
        self.assertIn("ok", rows)


if __name__ == "__main__":
    unittest.main()
