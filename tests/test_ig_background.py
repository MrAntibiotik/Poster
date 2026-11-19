"""Публікатор Instagram: один контейнер — одне очікування. Новий контейнер — лише після ERROR / EXPIRED від Meta.
Тайм-аут не створює нового контейнера; стан переживає перезапуск; черга інших платформ не стоїть."""
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
    """Імітація Instagram: сценарій статусів на кожен контейнер."""
    METHODS = (("копія під вимоги Reels", ""), ("легка копія 30 к/с", "safe30"))

    def __init__(self):
        self.uploads, self.checks, self.publishes = [], [], []
        self.inline = {}       # cid → що повернути одразу: "ok" | "wait" | "error" | "expired"
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

    def publish(self, path, meta, method=0, on_container=None):
        cid, rec = self._new(method, on_container)
        how = self.inline.get(cid, self.default_inline)
        if how == "ok":
            self.publishes.append(cid)
            return {"id": "M" + cid, "url": f"https://ig/{cid}", **rec}
        if how in ("error", "expired"):
            raise ContainerFailed(f"контейнер {how.upper()}", cid, how, how.upper(), "x")
        raise StillProcessing("ще обробляється", cid)

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
        self.sent, self.msgs = [], []
        self.app.bot.instagram_manual = lambda item_id, meta: self.sent.append(item_id)
        orig_send = self.app.bot.send
        self.app.bot.send = lambda text, *a, **k: (self.msgs.append(text), orig_send(text, *a, **k))
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


class PollFlowTest(IgFlowBase):
    def test_instant_publish(self):
        self.ig.default_inline = "ok"
        a, = self.publish("a")
        self.assertEqual(self.res(a)["status"], "ok")
        self.assertEqual(self.ig.checks, [])

    def test_in_progress_never_creates_new_container(self):             # 1, 8
        a, = self.publish("a")
        self.assertEqual(self.res(a)["status"], "processing")
        self.later(6 * 60)                                                 # 6 годин IN_PROGRESS
        self.assertEqual(self.ig.uploads, [("C1", 0)])                    # той самий контейнер
        self.assertEqual(self.res(a)["status"], "processing")
        self.assertLessEqual(len(self.ig.checks), 7)                      # рідкі перевірки: 2,5,10,20,40 хв, далі раз на 3 год
        self.ig.status["C1"] = "FINISHED"
        self.later(180)
        self.assertEqual(self.res(a)["status"], "ok")                     # 3
        self.assertEqual(self.ig.publishes, ["C1"])

    def test_whole_day_in_progress_few_requests(self):
        a, = self.publish("a")
        self.later(24 * 60 + 15)                                          # Meta добу не змінює статус
        self.assertLessEqual(self.ig.checks.count("C1"), 13)              # ≈13 запитів за добу, не сотні
        self.assertEqual(self.ig.uploads, [("C1", 0), ("C2", 1)])          # після доби — як EXPIRED: інший спосіб

    def test_pending_never_creates_new_container(self):                 # 2
        a, = self.publish("a")
        self.ig.status["C1"] = "PENDING"
        self.later(120)
        self.assertEqual(self.ig.uploads, [("C1", 0)])
        self.assertEqual(self.res(a)["status"], "processing")

    def test_error_allows_one_controlled_next_method(self):             # 4
        a, = self.publish("a")
        self.ig.status["C1"] = "ERROR"
        self.later(3)
        self.assertEqual(self.ig.uploads, [("C1", 0), ("C2", 1)])          # наступний спосіб — лише після ERROR
        self.ig.status["C2"] = "ERROR"
        self.later(10)
        self.assertEqual(len(self.ig.uploads), 2)                          # способів більше немає — без нових контейнерів
        self.assertEqual(self.res(a)["status"], "manual")                  # повне відео — вам у Telegram
        self.assertEqual(self.sent, [a])

    def test_expired_allows_new_attempt(self):                           # 5
        a, = self.publish("a")
        self.ig.status["C1"] = "EXPIRED"
        self.later(3)
        self.assertEqual(self.ig.uploads, [("C1", 0), ("C2", 1)])

    def test_inline_error_goes_to_next_method(self):
        self.ig.inline["C1"] = "error"
        self.ig.inline["C2"] = "ok"
        a, = self.publish("a")
        self.later(2)
        self.assertEqual(self.ig.uploads, [("C1", 0), ("C2", 1)])
        self.assertEqual(self.res(a)["status"], "ok")

    def test_published_status_no_duplicate(self):                       # 6
        a, = self.publish("a")
        self.ig.status["C1"] = "PUBLISHED"
        self.later(3)
        self.assertEqual(self.res(a)["status"], "ok")
        self.assertEqual(self.ig.publishes, [])
        self.assertEqual(len(self.ig.uploads), 1)

    def test_unknown_status_keeps_waiting(self):                        # 7
        a, = self.publish("a")
        self.ig.status["C1"] = "SOMETHING_NEW"
        self.later(120)
        self.assertEqual(len(self.ig.uploads), 1)
        self.assertEqual(self.res(a)["status"], "processing")

    def test_media_publish_error_retries_same_container(self):
        a, = self.publish("a")
        self.ig.status["C1"] = "MEDIA_PUBLISH_FAIL"
        self.later(30)
        self.assertEqual(len(self.ig.uploads), 1)                          # жодного нового контейнера
        self.assertEqual(self.res(a)["status"], "error")
        self.assertIn("media_publish", self.res(a)["error"])

    def test_one_at_a_time_and_delay_releases_queue(self):
        a, b = self.publish("a", "b")
        self.assertEqual(self.yt.n, 2)                                     # YouTube обох — одразу
        self.assertEqual(self.res(b)["status"], "ig_wait")
        self.later(30)
        self.assertEqual(len(self.ig.uploads), 1)                          # поки #a обробляється — #b чекає
        self.later(40)                                                     # понад годину — #a чергу більше не тримає
        self.assertEqual(self.ig.uploads, [("C1", 0), ("C2", 0)])
        self.assertEqual(self.res(a)["status"], "processing")              # #a чекає далі той самий контейнер
        self.assertEqual(len([m for m in self.msgs if "обробляє #" in m]), 1)   # повідомлення про затримку — раз

    def test_restart_during_wait_does_not_reupload(self):
        a, = self.publish("a")
        self.app = self._app()                                             # «перезапуск»
        self.app.platforms = {"youtube": self.yt, "instagram": self.ig}
        self.app.scheduler.resume_publishing()
        self.ig.status["C1"] = "FINISHED"
        self.later(5)
        self.assertEqual(self.res(a)["status"], "ok")
        self.assertEqual(len(self.ig.uploads), 1)

    def test_restart_mid_inline_wait_keeps_container(self):
        # процес упав посеред першого очікування: ID контейнера вже в базі — новий не створюється
        self.ig.default_inline = "crash"

        def crash(path, meta, method=0, on_container=None):
            self.ig._new(method, on_container)
            raise KeyboardInterrupt
        self.ig.publish = crash
        self.make_short("a")
        self.app.scheduler.scan(self.now)
        a = self.app.db.queue()[0]["id"]
        with self.assertRaises(KeyboardInterrupt):
            self.app.publisher.start(a)
        self.assertEqual(self.res(a)["container"], "C1")
        self.app = self._app()
        self.app.platforms = {"youtube": self.yt, "instagram": FakeIG()}
        self.app.platforms["instagram"].status["C1"] = "FINISHED"
        self.app.scheduler.resume_publishing()
        self.later(15)
        self.assertEqual(self.res(a)["status"], "ok")
        self.assertEqual(self.app.platforms["instagram"].uploads, [])       # жодного нового завантаження

    def test_retry_while_processing_does_not_reupload(self):
        a, = self.publish("a")
        self.app.publisher.start(a)
        self.assertEqual(len(self.ig.uploads), 1)
        self.assertEqual(self.yt.n, 1)

    def test_long_video_not_routed_to_telegram(self):                    # 9
        from unittest import mock
        from app.core import media
        self.ig.default_inline = "ok"
        with mock.patch.object(media, "ig_probe", return_value={"duration": 59.2}):
            a, = self.publish("a")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.res(a)["status"], "ok")


class SilentFlowTest(IgFlowBase):
    silent = True

    def test_silent_wait_then_check_and_no_new_container_on_timeout(self):
        a, = self.publish("a")
        self.later(19)
        self.assertEqual(self.ig.checks, [])                               # 19 хв — жодного запиту
        self.later(1)
        self.assertEqual(self.ig.checks, ["C1"])
        self.later(60)
        self.assertEqual(self.ig.uploads, [("C1", 0)])                     # не готово ≠ помилка: без нового контейнера
        self.ig.status["C1"] = "FINISHED"
        self.later(30)
        self.assertEqual(self.res(a)["status"], "ok")


class DiagnosticsFlowTest(IgFlowBase):
    def test_history_keeps_every_status_check_and_status_shows_container(self):
        a, = self.publish("a")
        self.later(20)
        text = self.app.status_text()
        self.assertIn("контейнер C1", text)                                 # «🕐 Статус»: що й скільки чекає
        self.ig.status["C1"] = "FINISHED"
        self.later(30)
        import json
        row = self.app.db.one("SELECT * FROM history WHERE item_id=? AND result='ok_background'", (a,))
        polls = json.loads(row["polls"])
        self.assertGreaterEqual(len(polls), 4)                             # усі перевірки, а не лише остання
        self.assertEqual(polls[-1][1], "FINISHED")
        self.assertEqual(row["category"], None)                               # опубліковано — проблеми немає


if __name__ == "__main__":
    unittest.main()
