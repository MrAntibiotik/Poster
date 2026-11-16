"""Ліміт запитів Meta (код 4): пауза без зайвих запитів, статистика не з'їдає квоту, публікація повторюється сама."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.platforms import RateLimited  # noqa: E402
from app.platforms import meta_graph  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


class Resp:
    def __init__(self, body, status=200, headers=None):
        self.body, self.status_code, self.headers, self.text = body, status, headers or {}, json.dumps(body)

    def json(self):
        return self.body


class LimitFileMixin:
    def setUp(self):
        super().setUp()
        self._lf = meta_graph.LIMIT_FILE
        meta_graph.LIMIT_FILE = Path(tempfile.mkdtemp()) / "limit.json"
        self._get, self._post = meta_graph.requests.get, meta_graph.requests.post

    def tearDown(self):
        meta_graph.LIMIT_FILE = self._lf
        meta_graph.requests.get, meta_graph.requests.post = self._get, self._post
        super().tearDown()


class GraphLimitTest(LimitFileMixin, unittest.TestCase):
    def test_code4_blocks_without_more_requests(self):
        calls = []

        def get(url, **kw):
            calls.append(url)
            return Resp({"error": {"code": 4, "message": "(#4) Application request limit reached"}}, 400)
        meta_graph.requests.get = get
        g = meta_graph.Graph("v25.0", "T")
        with self.assertRaises(RateLimited) as cm:
            g.get("123", fields="id")
        self.assertIn("вичерпано ліміт запитів", str(cm.exception))
        self.assertGreater(cm.exception.retry_at, time.time() + 50 * 60)     # ≈ година
        with self.assertRaises(RateLimited):
            g.post("123/media", caption="x")                                 # під час паузи — навіть не стукаємо
        self.assertEqual(len(calls), 1)
        self.assertTrue(meta_graph.blocked_until())

    def test_regain_time_from_header(self):
        hdr = {"X-Business-Use-Case-Usage": json.dumps({"1": [{"call_count": 100, "estimated_time_to_regain_access": 20}]})}
        meta_graph.requests.get = lambda url, **kw: Resp({"error": {"code": 80002, "message": "limit"}}, 400, hdr)
        with self.assertRaises(RateLimited) as cm:
            meta_graph.Graph("v25.0", "T").get("1")
        self.assertLess(cm.exception.retry_at, time.time() + 21 * 60)

    def test_high_usage_spares_quota_for_publishing(self):
        calls = []

        def get(url, **kw):
            calls.append(url)
            return Resp({"id": "1"}, 200, {"X-App-Usage": json.dumps({"call_count": 72, "total_time": 5})})
        meta_graph.requests.get = get
        g = meta_graph.Graph("v25.0", "T")
        g.get("1")
        self.assertEqual(meta_graph.usage_pct(), 72)
        with self.assertRaisesRegex(RateLimited, "статистику відкладено"):
            g.get("1/insights", low=True, metric="views")                   # статистика — ні
        g.get("1", fields="status_code")                                     # публікація — так
        self.assertEqual(len(calls), 2)


class Limited:
    def __init__(self):
        self.calls = 0

    def publish(self, path, meta):
        self.calls += 1
        raise RateLimited("Meta: вичерпано ліміт запитів (4) — повтор після 14:00", int(time.time()) + 3600)


class Ok:
    def publish(self, path, meta):
        return {"id": "y1", "url": "https://yt/y1"}


class PublishLimitTest(FlowBase):
    def test_no_hammering_and_auto_retry(self):
        self.cfg["dry_run"] = False
        ig = Limited()
        self.app.platforms = {"youtube": Ok(), "instagram": ig}
        self.make_short("a")
        self.app.scheduler.tick(self.ts(11, "12:00"))
        it = self.app.db.items("status='notified'")[0]
        self.press(f"pub:{it['id']}", it)
        from app.core import config
        config.FAKE_NOW = int(time.time())
        self.app.publisher.run(it["id"])
        self.assertEqual(ig.calls, 1)                                        # без 3 спроб підряд
        res = self.app.db.get_item(it["id"])["results"]
        self.assertEqual(res["youtube"]["status"], "ok")
        self.assertIn("ліміт запитів", res["instagram"]["error"])
        self.assertIsNotNone(self.app.scheduler.retry_at(it["id"]))          # повтор заплановано сам
        self.assertGreater(self.app.scheduler.retry_at(it["id"]), res["instagram"]["retry_at"])


class StatsLimitTest(FlowBase):
    def test_collect_reuses_fresh_and_stops_on_limit(self):
        from app.insights.reports import collect
        self.cfg["dry_run"] = False

        class P:
            calls = 0

            def publish(self, path, meta):
                return {"id": "p", "url": "u"}

            def get_stats(self, res):
                P.calls += 1
                raise RateLimited("ліміт", int(time.time()) + 3600)

            def account_stats(self):
                return {"followers": 10}
        self.app.platforms = {"instagram": P()}
        now = self.ts(11, "12:00")
        from app.core import config
        config.FAKE_NOW = now
        for n in range(3):
            iid = self.app.db.x("INSERT INTO items(mp4, json, status, posted_at, results, added_at, qpos) VALUES (?,?,?,?,?,?,?)",
                                (f"/x/{n}.mp4", f"/x/{n}.json", "posted", now - 3600,
                                 json.dumps({"instagram": {"status": "ok", "id": str(n)}}), now - 7200, n))
        out = collect(self.app)
        self.assertEqual(P.calls, 1)                    # після першого «ліміт» решту відео не чіпаємо
        self.assertIn("instagram", out["errors"])
        collect(self.app)
        self.assertEqual(self.app.db.one("SELECT COUNT(*) AS n FROM account_stats")["n"], 1)   # підписники — з бази
        self.assertTrue(iid)


if __name__ == "__main__":
    unittest.main()


class StaggerAndMissingTest(FlowBase):
    def test_retries_after_limit_are_spread(self):
        from app.core import config
        now = int(time.time())
        config.FAKE_NOW = now
        ids = []
        for n in ("a", "b"):
            self.make_short(n)
        self.app.scheduler.scan()
        ids = [i["id"] for i in self.app.db.items("status='queued'")][:2]
        for i in ids:
            self.app.publisher._retry_after_limit(i, now + 3600)
        a, b = (self.app.scheduler.retry_at(i) for i in ids)
        self.assertGreaterEqual(b - a, 900)

    def test_failed_item_with_deleted_file_leaves_chat(self):
        self.make_short("a")
        self.app.scheduler.scan()
        it = self.app.db.items("status='queued'")[0]
        self.app.db.update_item(it["id"], status="failed")
        self.app.db.set_tg_msg(it["id"], 42, 555, None, 0)
        Path(it["mp4"]).unlink()
        self.app.scheduler.scan()
        self.assertEqual(self.app.db.get_item(it["id"])["status"], "missing")
        self.assertIn(555, self.app.bot.tg.deleted)


class GoneStatsTest(FlowBase):
    def test_deleted_video_not_asked_again(self):
        from app.insights.reports import collect
        from app.core import config
        self.cfg["dry_run"] = False

        class P:
            calls = 0

            def get_stats(self, res):
                P.calls += 1
                raise RuntimeError("Meta (100/33): Unsupported get request. Object with ID '9' does not exist")

            def account_stats(self):
                return {}
        self.app.platforms = {"facebook": P()}
        now = self.ts(11, "12:00")
        config.FAKE_NOW = now
        self.app.db.x("INSERT INTO items(mp4, json, status, posted_at, results, added_at, qpos) VALUES (?,?,?,?,?,?,?)",
                      ("/x/a.mp4", "/x/a.json", "posted", now - 3600,
                       json.dumps({"facebook": {"status": "ok", "id": "9"}}), now - 7200, 1))
        collect(self.app)
        config.FAKE_NOW = now + 8 * 3600
        collect(self.app)
        self.assertEqual(P.calls, 1)


class StopMidPublishTest(FlowBase):
    def test_stop_interrupts_instagram(self):
        from app.platforms import Stopped
        self.cfg["dry_run"] = False
        db = self.app.db

        class SlowIG:
            should_stop = staticmethod(lambda: False)

            def publish(self, path, meta):
                db.set("stopped", "1")            # користувач натиснув «⛔ Стоп» під час обробки
                if self.should_stop():
                    raise Stopped("стоп")
                return {"id": "x", "url": "u"}
        ig = SlowIG()
        self.app.platforms = {"youtube": Ok(), "instagram": ig}
        self.make_short("a")
        self.app.scheduler.tick(self.ts(11, "12:00"))
        it = self.app.db.items("status='notified'")[0]
        self.press(f"pub:{it['id']}", it)
        db.update_item(it["id"], status="publishing")
        self.app.publisher.run(it["id"])
        item = db.get_item(it["id"])
        self.assertEqual(item["status"], "publishing")          # продовжиться після «Відновити»
        self.assertNotIn("instagram", item["results"])          # не записано як помилку
        self.assertEqual(item["results"]["youtube"]["status"], "ok")
        cap = self.app.bot.caption(item)
        self.assertIn("Публікацію зупинено", cap)
        self.assertIn("Instagram — ⏸", cap)


class StaleProgressTest(FlowBase):
    def test_after_restart_card_not_publishing(self):
        self.make_short("a")
        self.app.scheduler.scan()
        it = self.app.db.items("status='queued'")[0]
        db = self.app.db
        db.update_item(it["id"], status="publishing", results={"youtube": {"status": "ok", "url": "u"}})
        db.set(f"pubnow:{it['id']}", "instagram")          # позначка лишилась з процесу до перезапуску
        db.set("stopped", "1")
        cap = self.app.bot.caption(db.get_item(it["id"]))
        self.assertIn("Публікацію зупинено", cap)
        self.assertNotIn("публікується…", cap)


class StuckPublishingButtonsTest(FlowBase):
    def test_retry_buttons_on_interrupted(self):
        self.make_short("a")
        self.app.scheduler.scan()
        it = self.app.db.items("status='queued'")[0]
        self.app.db.update_item(it["id"], status="publishing",
                                results={"instagram": {"status": "error", "error": "Meta: ліміт"}})
        item = self.app.db.get_item(it["id"])
        self.assertIn(f"rf:{it['id']}", json.dumps(self.app.bot.keyboard(item)))
        self.assertIn("Публікацію перервано", self.app.bot.caption(item))
        self.app.publisher.active.add(it["id"])
        kb = json.dumps(self.app.bot.keyboard(item))             # справді публікується — лише «⏸ Пауза»
        self.assertIn(f"hold:{it['id']}", kb)
        self.assertNotIn("rf:", kb)
        self.app.publisher.active.discard(it["id"])


class StatusNowTest(FlowBase):
    def test_status_shows_what_is_publishing(self):
        app = self.app
        self.assertIn("✅ Нічого не публікується", app.status_text())
        self.make_short("a")
        self.make_short("b")
        app.scheduler.scan()
        a, b = app.db.items("status='queued'")[:2]
        app.db.update_item(a["id"], status="publishing", results={"youtube": {"status": "ok"}})
        app.db.update_item(b["id"], status="publishing")
        app.publisher.active.add(a["id"])
        app.publisher.running = a["id"]
        app.db.set(f"pubnow:{a['id']}", "instagram")
        app.publisher._report(a["id"], "Instagram обробляє відео")
        text = app.status_text()
        self.assertIn(f"#{a['id']} a — Instagram — ⏳ Instagram обробляє відео (перевірено", text)
        self.assertIn("(вже: YT ✅)", text)
        cap = app.bot.caption(app.db.get_item(a["id"]))
        self.assertIn("Instagram — ⏳ Instagram обробляє відео", cap)
        from app.core import config
        import json as J
        ph = J.loads(app.db.get(f"pubphase:{a['id']}"))
        ph["beat"] -= 10 * 60                                   # 10 хв без відповіді
        app.db.set(f"pubphase:{a['id']}", J.dumps(ph))
        config.FAKE_NOW = None
        self.assertIn("можливо, зависло", app.bot.caption(app.db.get_item(a["id"])))
        app.publisher.running = None
        self.assertIn(f"#{b['id']} b — ⚠️ перервано", text)
        app.publisher.active.discard(a["id"])


class PublishQueueTest(FlowBase):
    def test_many_at_once_go_one_by_one(self):
        import threading
        self.cfg["dry_run"] = False
        state = {"now": 0, "max": 0, "order": []}
        lock = threading.Lock()
        gate = threading.Event()

        class Slow:
            def publish(self, path, meta):
                with lock:
                    state["now"] += 1
                    state["max"] = max(state["max"], state["now"])
                    state["order"].append(Path(path).stem)
                gate.wait(5)
                with lock:
                    state["now"] -= 1
                return {"id": "x", "url": "u"}
        app = self.app
        app.platforms = {"youtube": Slow()}
        app.publisher.sync = False
        for n in ("a", "b", "c"):
            self.make_short(n)
        app.scheduler.scan()
        ids = [i["id"] for i in app.db.items("status='queued'", order="id")][:3]
        for i in ids:
            app.publisher.start(i)
        time.sleep(0.3)
        self.assertEqual([app.publisher.position(i) for i in ids], [0, 1, 2])
        text = app.status_text()
        self.assertIn("у черзі публікацій: 1-й", text)
        self.assertIn("У черзі на публікацію</b> (2-й)", app.bot.caption(app.db.get_item(ids[2])))
        gate.set()
        for _ in range(100):
            if not app.publisher.active:
                break
            time.sleep(0.05)
        self.assertEqual(state["max"], 1)                       # ніколи двоє одночасно
        self.assertEqual(state["order"], ["a", "b", "c"])        # у порядку натискання
        self.assertTrue(all(app.db.get_item(i)["status"] == "posted" for i in ids))


class HoldTest(FlowBase):
    def test_pause_one_short_next_goes(self):
        import threading
        self.cfg["dry_run"] = False
        app = self.app
        gate = threading.Event()
        order = []

        class Slow:
            should_stop = staticmethod(lambda: False)

            def publish(self, path, meta):
                order.append(Path(path).stem)
                for _ in range(100):                   # «обробка», слухаємо «⏸»
                    if self.should_stop():
                        from app.platforms import Stopped
                        raise Stopped("пауза")
                    if gate.wait(0.02):
                        break
                return {"id": "x", "url": "u"}
        app.platforms = {"instagram": Slow()}
        app.publisher.sync = False
        for n in ("a", "b"):
            self.make_short(n)
        app.scheduler.scan()
        a, b = [i["id"] for i in app.db.items("status='queued'", order="id")][:2]
        app.publisher.start(a)
        app.publisher.start(b)
        time.sleep(0.2)
        self.assertEqual(app.publisher.position(a), 0)
        app.bot._bg = lambda fn, *x: fn(*x)
        app.bot.handle({"update_id": 1, "callback_query": {"id": "c", "data": f"hold:{a}", "message": {
            "message_id": 1, "chat": {"id": 42}}}})
        for _ in range(100):
            if app.publisher.position(b) == 0:
                break
            time.sleep(0.05)
        self.assertEqual(app.publisher.position(b), 0)          # пішов наступний
        self.assertIn("На паузі", app.bot.caption(app.db.get_item(a)))
        app.scheduler.resume_publishing()
        self.assertFalse(app.publisher.is_active(a))            # сам не продовжується
        gate.set()
        for _ in range(100):
            if not app.publisher.active:
                break
            time.sleep(0.05)
        self.assertEqual(app.db.get_item(b)["status"], "posted")
        app.bot.handle({"update_id": 2, "callback_query": {"id": "c", "data": f"unhold:{a}", "message": {
            "message_id": 1, "chat": {"id": 42}}}})
        for _ in range(100):
            if app.db.get_item(a)["status"] == "posted":
                break
            time.sleep(0.05)
        self.assertEqual(app.db.get_item(a)["status"], "posted")
        self.assertEqual(order[:2], ["a", "b"])
