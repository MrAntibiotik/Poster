"""Instagram: завантажили → мовчки чекаємо 20 хв → ОДИН запит «готово?» → публікуємо або наступний спосіб.
Черга не стоїть, в Instagram — одне відео за раз, без дублів і паралельних запитів."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config  # noqa: E402
from app.platforms import PublishError  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


class Ok:
    def __init__(self):
        self.n = 0

    def publish(self, path, meta):
        self.n += 1
        return {"id": f"y{self.n}", "url": f"https://yt/{self.n}"}


class IG:
    METHODS = (("копія під вимоги Reels", ""), ("легка копія 30 к/с", "safe30"))

    def __init__(self):
        self.uploads, self.checks, self.ready, self.fail = [], [], set(), set()

    def upload_only(self, path, meta, method=0):
        cid = f"C{len(self.uploads) + 1}"
        self.uploads.append((cid, method))
        return {"container": cid, "method": method, "method_name": self.METHODS[method][0], "upload_note": ""}

    def finish_container(self, cid):
        self.checks.append(cid)
        if cid in self.fail:
            raise PublishError("Instagram відхилив відео під час обробки: ERROR")
        if cid in self.ready:
            return {"id": "M" + cid, "url": f"https://ig/{cid}"}
        return None


class IgWaitTest(FlowBase):
    def setUp(self):
        super().setUp()
        self.cfg["dry_run"] = False
        self.cfg["platforms"]["instagram"]["single_attempt"] = True
        self.yt, self.ig = Ok(), IG()
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
        for _ in range(minutes):          # планувальник «тікає» щохвилини
            self.now += 60
            config.FAKE_NOW = self.now
            self.app.publisher.finish_instagram(self.now)

    def test_silent_wait_then_one_check(self):
        a, = self.publish("a")
        self.assertEqual(self.res(a)["status"], "processing")
        self.later(19)
        self.assertEqual(self.ig.checks, [])                  # 19 хв — жодного запиту
        self.ig.ready.add("C1")
        self.later(1)
        self.assertEqual(self.ig.checks, ["C1"])              # рівно один
        self.assertEqual(self.res(a)["status"], "ok")
        self.assertEqual(self.res(a)["url"], "https://ig/C1")

    def test_next_method_after_20_min(self):
        a, = self.publish("a")
        self.later(20)                                        # не готово → наступний спосіб (новий контейнер)
        self.assertEqual(self.ig.uploads, [("C1", 0), ("C2", 1)])
        self.assertEqual(self.res(a)["status"], "processing")
        self.assertEqual(self.yt.n, 1)                        # YouTube вдруге не публікується
        self.ig.fail.add("C2")
        self.later(20)                                        # ERROR, а способів більше немає → ❌
        r = self.res(a)
        self.assertEqual(r["status"], "error")
        self.assertIn("жодним способом", r["error"])
        self.assertEqual(len(self.ig.uploads), 2)
        self.assertEqual(self.ig.checks, ["C1", "C2"])        # по одному запиту на спосіб

    def test_one_at_a_time_queue_goes_on(self):
        a, b = self.publish("a", "b")
        self.assertEqual(self.yt.n, 2)                        # YouTube обох — одразу
        self.assertEqual(self.ig.uploads, [("C1", 0)])        # Instagram — лише перший
        self.assertEqual(self.res(b)["status"], "ig_wait")
        self.ig.ready.add("C1")
        self.later(20)
        self.assertEqual(self.res(a)["status"], "ok")
        self.assertEqual(self.ig.uploads, [("C1", 0), ("C2", 0)])   # тепер #b, з першого способу
        self.assertEqual(self.res(b)["status"], "processing")

    def test_next_method_blocks_others(self):
        a, b = self.publish("a", "b")
        self.later(20)                                        # #a → наступний спосіб; #b досі чекає
        self.assertEqual(self.ig.uploads, [("C1", 0), ("C2", 1)])
        self.assertEqual(self.res(b)["status"], "ig_wait")

    def test_retry_while_processing_does_not_reupload(self):
        a, = self.publish("a")
        self.app.publisher.start(a)
        self.assertEqual(len(self.ig.uploads), 1)
        self.assertEqual(self.yt.n, 1)

    def test_survives_restart(self):
        a, = self.publish("a")
        self.app = self._app()
        self.app.platforms = {"youtube": self.yt, "instagram": self.ig}
        self.ig.ready.add("C1")
        self.later(20)
        self.assertEqual(self.res(a)["status"], "ok")
        self.assertEqual(len(self.ig.uploads), 1)

    def test_card_text(self):
        a, b = self.publish("a", "b")
        cap = self.app.bot.caption(self.app.db.get_item(a))
        self.assertIn("перевірю через 20 хв", cap)
        self.assertIn("до того не турбую", cap)
        cap = self.app.bot.caption(self.app.db.get_item(b))
        self.assertIn(f"чекає, поки Instagram обробить #{a}", cap)


if __name__ == "__main__":
    unittest.main()
