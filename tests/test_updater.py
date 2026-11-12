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

import updater  # noqa: E402
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
        backups = list((self.root / "data" / "backup").rglob("poster.py"))
        self.assertEqual(backups[0].read_text(), "# old\n")


class AutoUpdateFlowTest(FlowBase):
    def test_installs_when_idle_and_requests_restart(self):
        self.cfg["updates"] = {"auto": True, "check_hours": 3}
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


if __name__ == "__main__":
    unittest.main()
