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

import panel as panel_mod  # noqa: E402

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

    def test_auth_and_protection(self):
        self.assertEqual(self.req("GET", "/api/session")[1], {"authed": False, "has_password": False})
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
        self.login()
        st, r = self.req("POST", "/api/action", {"name": "start"})
        self.assertEqual(st, 400)
        self.assertIn("токен бота", r["error"])
        self.assertFalse(self.req("GET", "/api/state")[1]["secrets"]["TELEGRAM_BOT_TOKEN"]["set"])

    def test_folders(self):
        self.login()
        lib = self.tmp / "Мої шортси"
        (lib / "WoT").mkdir(parents=True)
        (lib / "WoT" / "a.mp4").write_bytes(b"0")
        (lib / "WoT" / "a.json").write_text('{"status": "ready_to_post"}', encoding="utf-8")
        (lib / "WoT" / "b.mp4").write_bytes(b"0")  # без json — не рахується
        st, j = self.req("POST", "/api/fs", {"path": "Мої шортси"})  # відносний шлях -> повний
        self.assertEqual(st, 200)
        self.assertEqual(Path(j["path"]), lib.resolve())
        self.assertEqual(j["dirs"], ["WoT"])
        self.assertEqual((j["shorts"]["total"], j["shorts"]["ready"], j["shorts"]["games"]), (1, 1, ["WoT"]))
        st, j = self.req("POST", "/api/fs/mkdir", {"path": str(lib), "name": "Dota"})
        self.assertTrue((lib / "Dota").is_dir())
        self.assertEqual(self.req("POST", "/api/fs/mkdir", {"path": str(lib), "name": "../evil"})[0], 400)
        self.req("POST", "/api/settings", {"paths.ready": str(lib)})
        info = self.req("GET", "/api/state")[1]["ready_info"]
        self.assertEqual((info["exists"], info["ready"]), (True, 1))
        # на не-Windows рідного вікна немає — панель показує власний перегляд папок
        if not panel_mod.control.WIN:
            self.assertEqual(self.req("POST", "/api/pick_folder", {})[1], {"path": None, "native": False})

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
