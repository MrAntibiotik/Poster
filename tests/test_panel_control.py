"""Керування з панелі: дії над шортсами виконує бот (через базу), відео в панелі, звіти, платформи."""
from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402
from tests import test_panel as _tp  # noqa: E402  (лише допоміжні методи)


class BotPanelCommandsTest(FlowBase):
    def setUp(self):
        super().setUp()
        for n in "abc":
            self.make_short(n, age=1000 - ord(n))
        config.FAKE_NOW = self.ts(11, "08:00")
        self.app.scheduler.scan()

    def item(self, name):
        return [i for i in self.app.db.items("1") if Path(i["mp4"]).stem == name][0]

    def do(self, **cmd):
        return self.app.bot.panel_do(cmd)

    def test_publish_from_queue_now_with_texts(self):
        b = self.item("b")
        msg = self.do(op="publish", id=b["id"], title="Камбек на 11 рівні", tags="wot танки", when="now")
        self.assertIn("публікується", msg)
        it = self.app.db.get_item(b["id"])
        self.assertEqual(it["status"], "posted")
        data = json.loads(Path(it["json"]).read_text(encoding="utf-8"))
        self.assertIn("Камбек на 11 рівні", json.dumps(data, ensure_ascii=False))
        self.assertIn("#танки", json.dumps(data, ensure_ascii=False))

    def test_queue_needs_time_and_schedule_then_approve(self):
        c = self.item("c")
        with self.assertRaises(ValueError):
            self.do(op="publish", id=c["id"], when="keep")
        ts = self.ts(11, "11:30")
        self.do(op="save", id=c["id"], when=ts)
        it = self.app.db.get_item(c["id"])
        self.assertEqual((it["status"], it["slot_at"]), ("notified", ts))
        self.assertIn("video", [m[0] for m in self.app.bot.tg.sent])  # картка пішла й у Telegram
        msg = self.do(op="publish", id=c["id"], when="keep")
        self.assertIn("підтверджено", msg)
        self.assertEqual(self.app.db.get_item(c["id"])["status"], "approved")
        self.do(op="unapprove", id=c["id"])
        self.assertEqual(self.app.db.get_item(c["id"])["status"], "notified")
        self.do(op="toggle", id=c["id"], platform="youtube")
        self.assertIn("youtube", self.app.db.get_item(c["id"])["disabled"])

    def test_past_time_rejected(self):
        c = self.item("c")
        for past in (self.ts(11, "07:00"), self.ts(10, "19:00"), self.ts(11, "08:00")):
            with self.assertRaises(ValueError):
                self.do(op="publish", id=c["id"], when=past)
        self.assertEqual(self.app.db.get_item(c["id"])["status"], "queued")
        with self.assertRaises(ValueError):
            config.parse_when("сьогодні 07:00", self.cfg, self.ts(11, "08:00"))
        self.assertEqual(config.parse_when("07:00", self.cfg, self.ts(11, "08:00")), self.ts(12, "07:00"))
        for it, ts in self.app.scheduler.projection(self.ts(11, "08:00")):
            self.assertGreater(ts, self.ts(11, "08:00"))

    def test_first_remove_and_errors(self):
        c = self.item("c")
        self.do(op="first", id=c["id"])
        self.assertEqual(Path(self.app.scheduler.projection()[0][0]["mp4"]).stem, "c")
        self.do(op="remove", id=c["id"])
        self.assertEqual(self.app.db.get_item(c["id"])["status"], "cancelled")
        with self.assertRaises(ValueError):
            self.do(op="first", id=c["id"])
        with self.assertRaises(ValueError):
            self.do(op="publish", id=99999, when="now")

    def test_report_text_and_worker_roundtrip(self):
        self.assertIn("<b>", self.do(op="report"))
        db = self.app.db
        self.app.bot.start_panel_worker()
        db.set("pcmd:t1", json.dumps({"op": "first", "id": self.item("c")["id"]}))
        for _ in range(50):
            if db.get("pres:t1"):
                break
            time.sleep(0.1)
        res = json.loads(db.get("pres:t1"))
        self.assertTrue(res["ok"], res)
        self.assertIsNone(db.get("pcmd:t1"))


class PanelControlHttpTest(unittest.TestCase):
    setUp, tearDown, req, login = _tp.PanelTest.setUp, _tp.PanelTest.tearDown, _tp.PanelTest.req, _tp.PanelTest.login

    def _item(self):
        mp4 = self.tmp / "ready" / "clip.mp4"
        mp4.parent.mkdir(parents=True, exist_ok=True)
        mp4.write_bytes(bytes(range(256)) * 40)
        js = mp4.with_suffix(".json")
        js.write_text(json.dumps({"status": "ready_to_post", "youtube": {"title": "Бій", "hashtags": ["#wot"]}}),
                      encoding="utf-8")
        return self.panel.db.add_item(str(mp4), str(js), "WoT"), mp4

    def test_video_endpoint_with_range(self):
        item_id, mp4 = self._item()
        self.assertEqual(self.req("GET", f"/api/video?id={item_id}")[0], 401)
        self.login()
        st, body = self.req("GET", f"/api/video?id={item_id}")
        self.assertEqual((st, body), (200, mp4.read_bytes()))
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", f"/api/video?id={item_id}", headers={"Host": f"127.0.0.1:{self.port}", "Cookie": self.cookie,
                                                              "Range": "bytes=10-19"})
        r = c.getresponse()
        self.assertEqual((r.status, r.read()), (206, mp4.read_bytes()[10:20]))
        self.assertEqual(self.req("GET", "/api/video?id=999")[0], 404)

    def test_state_has_queue_items_and_free_slots(self):
        item_id, _ = self._item()
        self.login()
        s = self.req("GET", "/api/state")[1]
        q = [i for i in s["queue_items"] if i["id"] == item_id][0]
        self.assertEqual(q["title"], "Бій")
        self.assertTrue(s["free_slots"])
        self.assertEqual(s["work"], [])

    def test_item_action_requires_running_bot(self):
        item_id, _ = self._item()
        self.login()
        st, r = self.req("POST", "/api/action", {"name": "item", "op": "first", "id": item_id})
        self.assertEqual(st, 400)
        self.assertIn("Бот зупинений", r["error"])

    def test_bot_call_roundtrip(self):
        self.panel.cfg["lock_port"] = 47999
        from app.system import control
        orig = control.is_running
        control.is_running = lambda port: True
        try:
            def fake_bot():
                for _ in range(50):
                    rows = self.panel.db.q("SELECT key, value FROM kv WHERE key LIKE 'pcmd:%'")
                    if rows:
                        k = rows[0]["key"]
                        self.panel.db.x("DELETE FROM kv WHERE key=?", (k,))
                        self.panel.db.set("pres:" + k[5:], json.dumps({"ok": True, "message": "зроблено"}))
                        return
                    time.sleep(0.05)
            threading.Thread(target=fake_bot, daemon=True).start()
            self.assertEqual(self.panel.bot_call({"op": "first", "id": 1}, 5), "зроблено")
        finally:
            control.is_running = orig

    def test_platform_toggle(self):
        self.login()
        st, r = self.req("POST", "/api/action", {"name": "platform", "key": "tiktok", "on": False})
        self.assertEqual(st, 200, r)
        raw = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        self.assertFalse(raw["platforms"]["tiktok"]["enabled"])


if __name__ == "__main__":
    unittest.main()
