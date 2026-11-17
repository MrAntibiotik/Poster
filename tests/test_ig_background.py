"""Довга обробка Instagram: черга не стоїть, Instagram — по одному відео, без дублів."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config  # noqa: E402
from app.platforms import PublishError, StillProcessing  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


class Ok:
    def __init__(self):
        self.n = 0

    def publish(self, path, meta):
        self.n += 1
        return {"id": f"y{self.n}", "url": f"https://yt/{self.n}"}


class SlowIG:
    """publish: файл дійшов, але Instagram обробляє довго → StillProcessing. finish_container — за сценарієм."""

    def __init__(self):
        self.uploads, self.finished, self.ready, self.fail = [], [], set(), set()

    def publish(self, path, meta):
        cid = f"C{len(self.uploads) + 1}"
        self.uploads.append(cid)
        raise StillProcessing("Instagram ще обробляє", cid)

    def finish_container(self, cid):
        if cid in self.fail:
            raise PublishError("Instagram відхилив відео під час обробки: ERROR")
        if cid in self.ready:
            self.finished.append(cid)
            return {"id": "M" + cid, "url": f"https://ig/{cid}"}
        return None


class IgBackgroundTest(FlowBase):
    def setUp(self):
        super().setUp()
        self.cfg["dry_run"] = False
        self.cfg["platforms"]["instagram"]["single_attempt"] = True
        self.yt, self.ig = Ok(), SlowIG()
        self.app.platforms = {"youtube": self.yt, "instagram": self.ig}
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
        self.now += minutes * 60
        config.FAKE_NOW = self.now
        self.app.publisher.finish_instagram(self.now)

    def test_queue_goes_on_and_instagram_one_at_a_time(self):
        a, b = self.publish("a", "b")
        # YouTube обох — одразу; Instagram: #a обробляється, #b чекає, другий файл в Instagram не вантажили
        self.assertEqual(self.yt.n, 2)
        self.assertEqual(self.ig.uploads, ["C1"])
        self.assertEqual(self.res(a)["status"], "processing")
        self.assertEqual(self.res(b)["status"], "ig_wait")
        self.assertEqual(self.app.db.get_item(a)["status"], "posted")
        self.assertEqual(self.app.db.get_item(b)["status"], "posted")
        self.later(3)                          # ще обробляється
        self.assertEqual(self.res(a)["status"], "processing")
        self.assertEqual(self.ig.uploads, ["C1"])
        self.ig.ready.add("C1")
        self.later(5)                          # готово → публікуємо той самий контейнер, без нового завантаження
        self.assertEqual(self.res(a)["status"], "ok")
        self.assertEqual(self.res(a)["url"], "https://ig/C1")
        self.assertEqual(self.ig.finished, ["C1"])
        # Instagram звільнився → #b вантажиться в Instagram (YouTube #b вдруге НЕ публікується)
        self.assertEqual(self.ig.uploads, ["C1", "C2"])
        self.assertEqual(self.yt.n, 2)
        self.assertEqual(self.res(b)["status"], "processing")
        self.ig.ready.add("C2")
        self.later(5)
        self.assertEqual(self.res(b)["status"], "ok")
        self.assertEqual(self.ig.finished, ["C1", "C2"])

    def test_retry_while_processing_does_not_reupload(self):
        a, = self.publish("a")
        self.app.publisher.start(a)            # «🔁 Повторити» / автоповтор, поки Instagram обробляє
        self.assertEqual(self.ig.uploads, ["C1"])
        self.assertEqual(self.yt.n, 1)

    def test_processing_error_becomes_failure(self):
        a, = self.publish("a")
        self.ig.fail.add("C1")
        self.later(3)
        r = self.res(a)
        self.assertEqual(r["status"], "error")
        self.assertIn("ERROR", r["error"])

    def test_stuck_container_gives_up_after_an_hour_and_frees_queue(self):
        a, b = self.publish("a", "b")
        for _ in range(4):
            self.later(15)
        self.assertEqual(self.res(a)["status"], "processing")
        self.later(15)
        r = self.res(a)
        self.assertEqual(r["status"], "error")
        self.assertIn("контейнер завис", r["error"])
        self.assertEqual(self.ig.uploads, ["C1", "C2"])        # черга Instagram пішла далі — #b вантажиться
        self.assertEqual(self.res(b)["status"], "processing")

    def test_survives_restart(self):
        a, = self.publish("a")
        self.app = self._app()                 # «перезапуск»: новий процес, та сама база
        self.app.platforms = {"youtube": self.yt, "instagram": self.ig}
        self.ig.ready.add("C1")
        self.later(3)
        self.assertEqual(self.res(a)["status"], "ok")
        self.assertEqual(self.ig.uploads, ["C1"])

    def test_card_text(self):
        a, b = self.publish("a", "b")
        cap = self.app.bot.caption(self.app.db.get_item(a))
        self.assertIn("Instagram ще обробляє", cap)
        cap = self.app.bot.caption(self.app.db.get_item(b))
        self.assertIn(f"чекає, поки Instagram обробить #{a}", cap)


if __name__ == "__main__":
    unittest.main()
