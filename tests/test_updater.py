"""Автооновлення з GitHub: порівняння версій, розгортання архіву без зачіпання ваших файлів, перезапуск."""
from __future__ import annotations

import io
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.system import updater  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


class Resp:
    def __init__(self, status=200, text="", content=b""):
        self.status_code, self.text, self.content = status, text, content


def make_zip(version: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("MrAntibiotik-poster-abc123/poster.py", "# new poster\n")
        z.writestr("MrAntibiotik-poster-abc123/config.py", f'VERSION = "{version}"\n')
        z.writestr("MrAntibiotik-poster-abc123/platforms/new.py", "x = 1\n")
        z.writestr("MrAntibiotik-poster-abc123/.env", "EVIL=1\n")          # не повинно потрапити
        z.writestr("MrAntibiotik-poster-abc123/../escape.py", "bad\n")     # шлях за межі папки
    return buf.getvalue()


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "poster.py").write_text("# old\n", encoding="utf-8")
        (self.root / ".env").write_text("TELEGRAM_BOT_TOKEN=mine\n", encoding="utf-8")
        (self.root / "config.json").write_text("{}", encoding="utf-8")
        self.cfg = {"paths": {"data": str(self.root / "data")}, "secrets": {"GITHUB_TOKEN": "T"},
                    "updates": {"repo": "MrAntibiotik/poster"}}
        (self.root / "data").mkdir()
        self._get = updater.requests.get

    def tearDown(self):
        updater.requests.get = self._get
        shutil.rmtree(self.root, ignore_errors=True)

    def test_version_compare(self):
        k = updater.version_key
        self.assertGreater(k("2026.11.12-41"), k("2026.11.12-40"))
        self.assertGreater(k("2026.11.13-1"), k("2026.11.12-99"))
        self.assertGreater(k("2026.11.12-10"), k("2026.11.12-9"))

    def test_check_and_private_repo_message(self):
        calls = []

        def get(url, **kw):
            calls.append(kw["headers"].get("Authorization"))
            return Resp(200, 'VERSION = "2099.01.01-1"  # x\n')
        updater.requests.get = get
        self.assertTrue(updater.check(self.cfg)["newer"])
        self.assertEqual(calls, ["Bearer T"])
        updater.requests.get = lambda url, **kw: Resp(404, "Not Found")
        self.cfg["secrets"]["GITHUB_TOKEN"] = ""
        with self.assertRaisesRegex(updater.UpdateError, "приватний"):
            updater.check(self.cfg)

    def test_apply_keeps_user_files(self):
        updater.requests.get = lambda url, **kw: Resp(200, content=make_zip("2099.01.01-1"))
        new = updater.apply(self.cfg, root=self.root)
        self.assertEqual(new, "2099.01.01-1")
        self.assertEqual((self.root / "poster.py").read_text(), "# new poster\n")
        self.assertTrue((self.root / "platforms" / "new.py").exists())
        self.assertEqual((self.root / ".env").read_text(encoding="utf-8"), "TELEGRAM_BOT_TOKEN=mine\n")
        self.assertFalse((self.root.parent / "escape.py").exists())
        from app.core.config import VERSION
        self.assertEqual((self.root / "data" / "versions" / VERSION / "poster.py").read_text(), "# old\n")


def zip_of(version: str, files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("o-poster-1/poster.py", "# poster\n")
        z.writestr("o-poster-1/config.py", f'VERSION = "{version}"\n')
        for k, v in files.items():
            z.writestr("o-poster-1/" + k, v)
    return buf.getvalue()


class HistoryTest(unittest.TestCase):
    """Історія версій: повні копії, прибирання зайвих файлів, відкат на будь-яку версію, пауза автооновлення."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "poster.py").write_text("# old\n", encoding="utf-8")
        (self.root / "bot.py").write_text("# old root module\n", encoding="utf-8")
        (self.root / ".env").write_text("X=1\n", encoding="utf-8")
        (self.root / "channels" / "en").mkdir(parents=True)
        (self.root / "channels" / "en" / "notes.py").write_text("# user\n", encoding="utf-8")
        (self.root / "data").mkdir()
        (self.root / "config.json").write_text('{"updates": {"auto": true}}', encoding="utf-8")
        self.cfg = {"paths": {"data": str(self.root / "data")}, "secrets": {}, "updates": {"auto": True},
                    "config_path": str(self.root / "config.json")}
        self._get = updater.requests.get
        from app.core import config
        self._ver = config.VERSION

    def tearDown(self):
        updater.requests.get = self._get
        from app.core import config
        config.VERSION = self._ver
        shutil.rmtree(self.root, ignore_errors=True)

    def install(self, version, files):
        from app.core import config
        updater.requests.get = lambda url, **kw: Resp(200, content=zip_of(version, files))
        got = updater.apply(self.cfg, root=self.root)
        config.VERSION = got          # «перезапуск» на нову версію
        return got

    def test_full_history_and_switch(self):
        from app.core import config
        first = config.VERSION
        self.install("2099.01.01-1", {"app/a.py": "a1", "app/b.py": "b1"})
        self.assertTrue((self.root / "data" / "versions" / first / "bot.py").exists())   # повна копія старої
        self.assertFalse((self.root / "data" / "versions" / first / "channels").exists())  # дані каналів — не код
        self.install("2099.01.01-2", {"app/a.py": "a2"})
        self.assertFalse((self.root / "app" / "b.py").exists())      # файл, якого в новій версії немає, прибрано
        self.assertEqual((self.root / "app" / "a.py").read_text(), "a2")
        self.assertEqual(updater.local_versions(self.cfg), ["2099.01.01-1", first])
        # відкат на -1 з локальної копії
        updater.switch_to(self.cfg, "2099.01.01-1", root=self.root)
        config.VERSION = "2099.01.01-1"
        self.assertEqual((self.root / "app" / "b.py").read_text(), "b1")
        self.assertEqual((self.root / "app" / "a.py").read_text(), "a1")
        self.assertTrue((self.root / ".env").exists())
        self.assertTrue((self.root / "channels" / "en" / "notes.py").exists())
        import json
        up = json.loads((self.root / "config.json").read_text(encoding="utf-8"))["updates"]
        self.assertEqual((up["auto"], up["pinned"]), (False, "2099.01.01-1"))   # автооновлення на паузі
        # тепер -2 є в історії — можна повернутись вперед
        self.assertIn("2099.01.01-2", updater.local_versions(self.cfg))
        updater.switch_to(self.cfg, "2099.01.01-2", root=self.root)
        self.assertEqual((self.root / "app" / "a.py").read_text(), "a2")
        self.assertFalse((self.root / "app" / "b.py").exists())
        updater.set_auto_paused(self.cfg, None)                        # «Оновити» — пауза знімається
        up = json.loads((self.root / "config.json").read_text(encoding="utf-8"))["updates"]
        self.assertEqual(up, {"auto": True})

    def test_switch_to_version_from_github(self):
        urls = []
        sha = "a" * 40

        def get(url, **kw):
            urls.append(url)
            if url.endswith("/contents/releases.json"):
                return Resp(200, '{"2098.05.05-3": "%s", "bad": "not-a-sha"}' % sha)
            return Resp(200, content=zip_of("2098.05.05-3", {"app/x.py": "x"}))
        updater.requests.get = get
        updater.switch_to(self.cfg, "2098.05.05-3", root=self.root)
        self.assertTrue(urls[-1].endswith("/zipball/" + sha))
        self.assertEqual((self.root / "app" / "x.py").read_text(), "x")
        with self.assertRaisesRegex(updater.UpdateError, "немає ні на ПК"):
            updater.switch_to(self.cfg, "2098.05.05-9", root=self.root)

        def get2(url, **kw):
            if url.endswith("/contents/releases.json"):
                return Resp(200, '{"2098.05.05-7": "%s"}' % sha)
            return Resp(200, content=zip_of("2098.05.05-4", {}))
        updater.requests.get = get2
        with self.assertRaisesRegex(updater.UpdateError, "а не 2098.05.05-7"):
            updater.switch_to(self.cfg, "2098.05.05-7", root=self.root)

    def test_cleanup_legacy_root_modules(self):
        (self.root / "app").mkdir()
        (self.root / "app" / "__init__.py").write_text("", encoding="utf-8")
        (self.root / "platforms").mkdir()
        (self.root / "platforms" / "youtube.py").write_text("# old\n", encoding="utf-8")
        self.assertEqual(updater.cleanup_legacy(self.cfg, root=self.root), 2)
        self.assertFalse((self.root / "bot.py").exists())
        self.assertFalse((self.root / "platforms").exists())
        legacy = self.root / "data" / "versions" / "legacy-root"
        self.assertTrue((legacy / "platforms" / "youtube.py").exists())
        self.assertTrue((self.root / "poster.py").exists())
        self.assertEqual(updater.cleanup_legacy(self.cfg, root=self.root), 0)    # вдруге — нічого
        self.assertIn("poster.py", updater.installed_files(self.cfg, self.root))

    def test_changelog_and_version_list(self):
        (self.root / "CHANGELOG.md").write_text("# Історія\n\n## 2099.01.01-2\n- Нове\n- Ще\n\n## 2099.01.01-1\n- Перше\n",
                                               encoding="utf-8")
        self.assertEqual(updater.changelog(self.root)["2099.01.01-2"], ["Нове", "Ще"])
        updater.requests.get = lambda url, **kw: Resp(200, '{"2099.01.01-1": "%s"}' % ("b" * 40))
        vl = updater.version_list(self.cfg, root=self.root)
        got = {v["version"]: v for v in vl}
        self.assertTrue(got["2099.01.01-1"]["remote"])
        self.assertEqual(got["2099.01.01-1"]["notes"], ["Перше"])
        self.assertTrue(any(v.get("current") for v in vl))


class RollbackTest(UpdaterTest):
    def test_rollback_restores_backup(self):
        updater.requests.get = lambda url, **kw: Resp(200, content=make_zip("2099.01.01-1"))
        from app.core.config import VERSION
        updater.apply(self.cfg, root=self.root)
        self.assertEqual((self.root / "poster.py").read_text(), "# new poster\n")
        n = updater.rollback(self.cfg, VERSION, root=self.root)
        self.assertGreaterEqual(n, 1)
        self.assertEqual((self.root / "poster.py").read_text(), "# old\n")
        self.assertEqual((self.root / ".env").read_text(encoding="utf-8"), "TELEGRAM_BOT_TOKEN=mine\n")
        with self.assertRaises(updater.UpdateError):
            updater.rollback(self.cfg, "1999.01.01-1", root=self.root)

    def test_wait_new_boot(self):
        from app import cli as poster

        class DB(dict):
            def get(self, k, d=None):
                return dict.get(self, k, d)
        db = DB(boot_ok="2026.11.15-74|1")
        self.assertFalse(poster.wait_new_boot(db, "2026.11.15-74", timeout=1))
        db["boot_ok"] = "2026.11.15-75|2"
        self.assertTrue(poster.wait_new_boot(db, "2026.11.15-74", timeout=1))


class AutoUpdateFlowTest(FlowBase):
    def test_installs_when_idle_and_requests_restart(self):
        self.cfg["updates"] = {"auto": True, "check_minutes": 5}
        applied = []
        orig = (updater.check, updater.apply)
        updater.check = lambda cfg: {"local": "1", "remote": "2099.01.01-1", "newer": True}
        updater.apply = lambda cfg: applied.append(1) or "2099.01.01-1"
        try:
            sch = self.app.scheduler
            self.app.db.set("last_activity", self.ts(11, "08:58"))
            sch.tick(self.ts(11, "09:00"))
            sch._upd_thread.join()
            sch.tick(self.ts(11, "09:01"))
            self.assertEqual(applied, [])  # ви щойно клацали в боті — чекаємо
            sch.tick(self.ts(11, "09:10"))
            self.assertEqual(applied, [1])
            self.assertEqual(self.app.db.get("control"), "restart")
            self.assertTrue(any("оновлено до версії 2099.01.01-1" in s[1] for s in self.app.bot.tg.sent))
        finally:
            updater.check, updater.apply = orig

    def test_checks_at_most_hourly(self):
        self.cfg["updates"] = {"auto": True, "check_minutes": 5}       # навіть якщо в налаштуваннях 5 хв
        checks = []
        orig = updater.check
        updater.check = lambda cfg: checks.append(1) or {"local": "1", "remote": "1", "newer": False}
        try:
            sch = self.app.scheduler
            for hm in ("09:00", "09:05", "09:30", "09:59"):
                sch.tick(self.ts(11, hm))
                if sch._upd_thread:
                    sch._upd_thread.join()
            self.assertEqual(len(checks), 1)
            sch.tick(self.ts(11, "10:00"))
            sch._upd_thread.join()
            self.assertEqual(len(checks), 2)
        finally:
            updater.check = orig

    def test_update_button_works_even_with_auto_off(self):
        calls = []
        orig = (updater.check, updater.apply)
        updater.check = lambda cfg: {"local": "1", "remote": "1", "newer": False}
        updater.apply = lambda cfg: calls.append(1) or "x"
        try:
            self.app.bot.handle({"update_id": 1, "message": {"message_id": 5, "text": "⬆️ Оновити",
                                                              "chat": {"id": 42, "type": "private"}}})
            texts = [m[1] for m in self.app.bot.tg.sent]
            self.assertTrue(any("Перевіряю оновлення" in t for t in texts))
            self.assertIn("остання версія", self.app.bot.tg.edits[-1][1])  # «🔍…» стало результатом, одразу
            self.assertEqual(calls, [])
            # є нова версія, але йде публікація — питаємо; «⚡ Оновити зараз» — оновлюємось одразу
            updater.check = lambda cfg: {"local": "1", "remote": "2", "newer": True}
            self.app.publisher.active.add(7)
            text, kb = self.app.bot.update_now()
            self.assertIn("публікується", text)
            self.assertEqual(kb["inline_keyboard"][0][0]["callback_data"], "upd:1")
            self.app.bot.handle({"update_id": 2, "callback_query": {
                "id": "c", "data": "upd:1", "message": {"message_id": 99, "chat": {"id": 42}, "text": ""}}})
            self.assertEqual(calls, [1])
            self.assertEqual(self.app.db.get("control"), "restart")
        finally:
            updater.check, updater.apply = orig


if __name__ == "__main__":
    unittest.main()


class ReleaseTest(unittest.TestCase):
    def test_changelog_has_current_version(self):
        """Кожен реліз — із записом у CHANGELOG.md (його показує список версій у боті й панелі)."""
        from app.core.config import VERSION
        notes = updater.changelog(Path(__file__).resolve().parents[1])
        self.assertTrue(notes.get(VERSION), f"додайте розділ «## {VERSION}» у CHANGELOG.md")
