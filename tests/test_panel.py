"""Тести панелі керування: пароль, захист запитів, налаштування, секрети, користувачі."""
from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.web import panel as panel_mod  # noqa: E402

BOT_TOKEN = "123456789:AA" + "x" * 33


class PanelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        cfg = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        cfg["paths"].update({"ready": "ready", "posted": "posted", "skipped": "skipped"})
        cfg["lock_port"] = 47999
        (self.tmp / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        (self.tmp / ".env").write_text(f"TELEGRAM_BOT_TOKEN={BOT_TOKEN}\nTELEGRAM_CHAT_ID=42\n", encoding="utf-8")
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "META_APP_ID"):
            os.environ.pop(k, None)
        self.panel = panel_mod.Panel(str(self.tmp / "config.json"))
        self.panel.tg_say = lambda *a: None
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), panel_mod.make_handler(self.panel))
        self.port = self.srv.server_address[1]
        self.panel.port = self.port
        self.srv.RequestHandlerClass = panel_mod.make_handler(self.panel)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.cookie = ""

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "META_APP_ID"):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def req(self, method, path, body=None, host=None, header=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if header:
            h["X-Poster"] = "1"
        if self.cookie:
            h["Cookie"] = self.cookie
        data = json.dumps(body).encode() if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        sc = r.getheader("Set-Cookie")
        if sc:
            self.cookie = sc.split(";")[0]
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    def login(self):
        st, _ = self.req("POST", "/api/setup", {"password": "secret1"})
        self.assertEqual(st, 200)

    def test_page_assets(self):
        """Сторінка панелі розділена на розмітку, стилі й скрипт — усе віддається."""
        st, html = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b'/static/panel.css', html)
        self.assertIn(b'/static/panel.js', html)
        st, css = self.req("GET", "/static/panel.css")
        self.assertEqual(st, 200)
        self.assertIn(b"{", css)
        st, js = self.req("GET", "/static/panel.js")
        self.assertEqual(st, 200)
        self.assertIn(b"/api/state", js)
        self.assertEqual(self.req("GET", "/static/../config.json")[0], 404)

    def test_auth_and_protection(self):
        sess = self.req("GET", "/api/session")[1]
        self.assertEqual((sess["authed"], sess["has_password"]), (False, False))
        self.assertTrue(sess["version"])
        self.assertEqual(self.req("GET", "/api/state")[0], 401)
        self.assertEqual(self.req("GET", "/", host="evil.example:80")[0], 403)          # DNS rebinding
        self.assertEqual(self.req("POST", "/api/setup", {"password": "x1234"}, header=False)[0], 403)  # CSRF
        self.assertEqual(self.req("POST", "/api/setup", {"password": "12"})[0], 400)    # закороткий
        self.login()
        self.assertEqual(self.req("POST", "/api/setup", {"password": "other"})[0], 400)  # другий раз не можна
        self.assertEqual(self.req("GET", "/api/state")[0], 200)
        self.req("POST", "/api/logout", {})
        self.cookie = ""
        self.assertEqual(self.req("POST", "/api/login", {"password": "nope"})[0], 403)
        self.assertEqual(self.req("POST", "/api/login", {"password": "secret1"})[0], 200)
        st, s = self.req("GET", "/api/state")
        self.assertEqual(st, 200)
        # секрети не віддаються повністю
        self.assertNotIn(BOT_TOKEN, json.dumps(s))
        self.assertEqual(s["secrets"]["TELEGRAM_BOT_TOKEN"]["value"], "1234…xxx")
        self.assertEqual(s["secrets"]["TELEGRAM_CHAT_ID"]["value"], "42")

    def test_settings(self):
        self.login()
        st, r = self.req("POST", "/api/settings", {"slots": ["9:05", "21:00", ""], "max_skips": 4,
                                                   "platforms.tiktok.public": True, "unknown.key": 1})
        self.assertEqual(st, 200, r)
        cfg = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["slots"], ["09:05", "21:00"])
        self.assertEqual(cfg["max_skips"], 4)
        self.assertTrue(cfg["platforms"]["tiktok"]["public"])
        self.assertNotIn("unknown", cfg)
        self.assertEqual(self.req("POST", "/api/settings", {"slots": ["25:00"]})[0], 400)
        self.assertEqual(self.req("POST", "/api/settings", {"slots": [f"1{i}:00" for i in range(7)]})[0], 400)
        self.assertEqual(json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))["slots"], ["09:05", "21:00"])
        # хайлайти: папка, час (1–3), видимість
        st, r = self.req("POST", "/api/settings", {"highlights.enabled": True, "highlights.folder": "D:/Хайлайти",
                                                   "highlights.slots": "18:00, 9:30", "highlights.privacy": ""})
        self.assertEqual(st, 200, r)
        hl = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))["highlights"]
        self.assertEqual(hl, {"enabled": True, "folder": "D:/Хайлайти", "slots": ["09:30", "18:00"], "privacy": ""})
        self.assertEqual(self.req("POST", "/api/settings", {"highlights.slots": "10:00,11:00,12:00,13:00"})[0], 400)
        self.assertEqual(self.req("GET", "/api/state")[1]["settings"]["highlights.slots"], "09:30, 18:00")
        st, r = self.req("POST", "/api/settings", {"regenerate_command": 'python "D:/My Shorts/gen.py" {mp4}'})
        self.assertEqual(json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))["regenerate_command"],
                         ["python", "D:/My Shorts/gen.py", "{mp4}"])
        self.assertEqual(self.req("GET", "/api/state")[1]["settings"]["regenerate_command"],
                         'python "D:/My Shorts/gen.py" {mp4}')
        self.req("POST", "/api/action", {"name": "mode", "mode": "live"})
        self.assertFalse(self.req("GET", "/api/state")[1]["dry_run"])

    def test_secrets(self):
        self.login()
        self.assertEqual(self.req("POST", "/api/secrets", {"TELEGRAM_CHAT_ID": "123456789"})[0], 400)  # id бота
        self.assertEqual(self.req("POST", "/api/secrets", {"TELEGRAM_BOT_TOKEN": "abc"})[0], 400)
        st, _ = self.req("POST", "/api/secrets", {"TELEGRAM_CHAT_ID": "42, 77", "META_APP_ID": "999",
                                                  "TELEGRAM_BOT_TOKEN": "", "EVIL": "x"})
        self.assertEqual(st, 200)
        env = (self.tmp / ".env").read_text(encoding="utf-8")
        self.assertIn("TELEGRAM_CHAT_ID=42,77", env)
        self.assertIn("META_APP_ID=999", env)
        self.assertIn(BOT_TOKEN, env)  # порожнє поле не стирає токен
        self.assertNotIn("EVIL", env)

    def test_users(self):
        self.login()
        self.panel.db.set_user(42, "admin", "власник")
        self.panel.db.set_user(5, "pending", "Друг")
        self.assertEqual(self.req("POST", "/api/users", {"chat_id": 5, "role": "viewer"})[0], 200)
        self.assertEqual(self.panel.db.user(5)["role"], "viewer")
        self.assertEqual(self.req("POST", "/api/users", {"chat_id": 42, "role": "delete"})[0], 400)
        self.assertEqual(self.req("POST", "/api/users", {"chat_id": 5, "role": "hacker"})[0], 400)
        self.req("POST", "/api/users", {"chat_id": 5, "role": "delete"})
        self.assertIsNone(self.panel.db.user(5))
        users = self.req("GET", "/api/state")[1]["users"]
        self.assertTrue(users[0]["owner"])

    def test_start_without_telegram(self):
        (self.tmp / ".env").write_text("", encoding="utf-8")
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            os.environ.pop(k, None)  # як у користувача, що ще не вписав токен
        self.login()
        st, r = self.req("POST", "/api/action", {"name": "start"})
        self.assertEqual(st, 400)
        self.assertIn("токен бота", r["error"])
        self.assertFalse(self.req("GET", "/api/state")[1]["secrets"]["TELEGRAM_BOT_TOKEN"]["set"])

    def test_bad_folder_rejected(self):
        self.login()
        bad = "/proc/немає/posted"  # як «D:\\Shorts» без диска D: — створити неможливо
        st, r = self.req("POST", "/api/settings", {"paths.posted": bad})
        self.assertEqual(st, 400)
        self.assertIn("неможливо створити", r["error"])
        self.assertNotEqual(json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))["paths"]["posted"], bad)
        # CLI-команда (тестовий слот) дає зрозуміле повідомлення, а не traceback
        cfg = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        cfg["paths"]["posted"] = bad
        (self.tmp / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        import subprocess
        r = subprocess.run([sys.executable, str(ROOT / "poster.py"), "--config", str(self.tmp / "config.json"),
                            "test-slot", "10"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("Розклад і папки", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_folders(self):
        self.login()
        lib = self.tmp / "Мої шортси"
        (lib / "WoT").mkdir(parents=True)
        (lib / "WoT" / "a.mp4").write_bytes(b"0a")
        (lib / "WoT" / "a.json").write_text('{"status": "ready_to_post"}', encoding="utf-8")
        (lib / "WoT" / "b.mp4").write_bytes(b"0b")  # без json — тексти створяться автоматично
        st, j = self.req("POST", "/api/fs", {"path": "Мої шортси"})  # відносний шлях -> повний
        self.assertEqual(st, 200)
        self.assertEqual(Path(j["path"]), lib.resolve())
        self.assertEqual(j["dirs"], ["WoT"])
        sh = j["shorts"]
        self.assertEqual((sh["total"], sh["ready"], sh["no_json"], sh["games"]), (2, 2, 1, ["WoT"]))
        st, j = self.req("POST", "/api/fs/mkdir", {"path": str(lib), "name": "Dota"})
        self.assertTrue((lib / "Dota").is_dir())
        self.assertEqual(self.req("POST", "/api/fs/mkdir", {"path": str(lib), "name": "../evil"})[0], 400)
        self.req("POST", "/api/settings", {"paths.ready": str(lib)})
        info = self.req("GET", "/api/state")[1]["ready_info"]
        self.assertEqual((info["exists"], info["ready"]), (True, 2))
        # хештеги за грою редагуються текстом
        self.req("POST", "/api/settings", {"auto_meta.hashtags": "WoT: #wot танки\nDota: #dota2\n*: #shorts", "auto_meta.enabled": False})
        cfg = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["auto_meta"]["hashtags"], {"WoT": ["#wot", "#танки"], "Dota": ["#dota2"], "*": ["#shorts"]})
        st = self.req("GET", "/api/state")[1]
        self.assertEqual(st["settings"]["auto_meta.hashtags"], "WoT: #wot #танки\nDota: #dota2\n*: #shorts")
        self.assertEqual(st["ready_info"]["ready"], 1)  # без json не рахуються, коли вимкнено
        # на не-Windows рідного вікна немає — панель показує власний перегляд папок
        if not __import__("app.system.control", fromlist=["x"]).WIN:
            self.assertEqual(self.req("POST", "/api/pick_folder", {})[1], {"path": None, "native": False})

    def test_failed_block_and_actions(self):
        self.login()
        db = self.panel.db
        mp4 = self.tmp / "ready" / "a.mp4"
        mp4.parent.mkdir(parents=True, exist_ok=True)
        mp4.write_bytes(b"0")
        js = mp4.with_suffix(".json")
        js.write_text('{"status": "ready_to_post"}', encoding="utf-8")
        item_id = db.add_item(str(mp4), str(js), "WoT")
        import time
        db.update_item(item_id, status="posted", posted_at=int(time.time()),
                       results={"youtube": {"status": "ok", "url": "u"},
                                "instagram": {"status": "error", "error": "ProcessingFailedError"}})
        db.set(f"ignored:{item_id}", "1")
        f = self.req("GET", "/api/state")[1]["failed"]
        self.assertEqual((f[0]["id"], f[0]["ignored"]), (item_id, True))
        self.assertIn("ProcessingFailedError", f[0]["errors"]["Instagram"])
        self.assertEqual(self.req("POST", "/api/action", {"name": "failed", "id": item_id, "op": "unignore"})[0], 200)
        self.assertFalse(self.req("GET", "/api/state")[1]["failed"][0]["ignored"])
        st, r = self.req("POST", "/api/action", {"name": "failed", "id": item_id, "op": "now"})
        self.assertEqual(st, 200, r)
        self.assertIn("Повтор:", r["message"])
        self.assertEqual(len(db.q("SELECT * FROM slots WHERE state='retry' AND item_id=?", (item_id,))), 1)
        self.assertTrue(self.req("GET", "/api/state")[1]["failed"][0]["retry"])

    def test_youtube_secret_upload(self):
        self.login()
        self.assertEqual(self.req("POST", "/api/youtube/secret", {"content": "{}"})[0], 400)
        self.assertEqual(self.req("POST", "/api/youtube/secret", {"content": '{"web": {}}'})[0], 400)
        st, _ = self.req("POST", "/api/youtube/secret", {"content": '{"installed": {"client_id": "x"}}'})
        self.assertEqual(st, 200)
        self.assertTrue((self.tmp / "client_secret.json").exists())
        self.assertTrue(self.req("GET", "/api/state")[1]["tokens"]["youtube"]["client_secret"])


if __name__ == "__main__":
    unittest.main()
