"""Перенесення основного каналу з кореня програми в channels/main."""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import layout  # noqa: E402
from app.core.db import DB  # noqa: E402


class MigrateMainTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        r = self.root
        (r / "config.json").write_text(json.dumps({
            "paths": {"ready": "ready", "posted": "D:/Shorts/posted"},
            "platforms": {"youtube": {"client_secrets_file": "client_secret.json"}},
            "lock_port": 47613}), encoding="utf-8")
        (r / ".env").write_text("TELEGRAM_BOT_TOKEN=abc\n", encoding="utf-8")
        (r / "client_secret.json").write_text('{"installed": {}}', encoding="utf-8")
        (r / "tokens").mkdir()
        (r / "tokens" / "youtube_token.json").write_text("{}", encoding="utf-8")
        (r / "log.txt").write_text("old log\n", encoding="utf-8")
        db = DB(r / "data" / "poster.sqlite3")
        db.set("hello", "world")
        self.db = db
        (r / "data" / "versions" / "x").mkdir(parents=True)
        (r / "data" / "versions" / "x" / "a.py").write_text("a", encoding="utf-8")
        layout.MIGRATED_FROM = None

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        layout.MIGRATED_FROM = None

    def test_migrate(self):
        r = self.root
        got = layout.resolve_config(None, r)
        main = r / "channels" / "main"
        self.assertEqual(got, (main / "config.json").resolve())
        raw = json.loads((main / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(raw["paths"]["ready"]), (r / "ready").resolve())     # відео лишаються на місці
        self.assertEqual(raw["paths"]["posted"], "D:/Shorts/posted")               # абсолютні — без змін
        self.assertNotIn("data", raw["paths"])                                     # data/tokens/log — відносні, переїхали
        self.assertEqual((main / ".env").read_text(encoding="utf-8"), "TELEGRAM_BOT_TOKEN=abc\n")
        self.assertTrue((main / "client_secret.json").exists())
        self.assertTrue((main / "tokens" / "youtube_token.json").exists())
        self.assertEqual((main / "log.txt").read_text(encoding="utf-8"), "old log\n")
        self.assertTrue((main / "data" / "versions" / "x" / "a.py").exists())
        self.assertEqual(DB(main / "data" / "poster.sqlite3").get("hello"), "world")   # база з даними
        self.assertFalse((r / "config.json").exists())                             # старий конфіг ніхто не підхопить
        self.assertTrue((r / ("config.json" + layout.MOVED_SUFFIX)).exists())
        self.assertTrue((r / layout.NOTE).exists())
        self.assertTrue((r / ".env").exists())                                      # старі копії — резервні
        self.assertEqual(layout.MIGRATED_FROM, r / "data" / "poster.sqlite3")
        self.assertFalse((r / "channels" / "main.tmp").exists())
        # повторно — нічого не переносить; явний старий шлях теж веде в channels/main
        self.assertEqual(layout.resolve_config(r / "config.json", r), got)
        self.assertEqual(layout.default_config_path(r), got)
        # інші конфіги (напр. channels/en) — як є
        self.assertEqual(layout.resolve_config(r / "channels" / "en" / "config.json", r),
                         (r / "channels" / "en" / "config.json").resolve())

    def test_main_config_loads(self):
        from app.core import config
        layout.resolve_config(None, self.root)
        cfg = config.load_config(self.root / "channels" / "main" / "config.json")
        self.assertEqual(Path(cfg["paths"]["data"]), (self.root / "channels" / "main" / "data").resolve())
        self.assertEqual(Path(cfg["paths"]["ready"]), (self.root / "ready").resolve())
        self.assertEqual(Path(cfg["platforms"]["youtube"]["client_secrets_file"]),
                         self.root / "channels" / "main" / "client_secret.json")

    def test_failed_copy_keeps_old_layout(self):
        orig = layout._tables
        calls = []

        def bad(db):
            calls.append(db)
            return {"kv": len(calls)} if len(calls) == 1 else {"kv": 0}   # «копія» має менше записів
        layout._tables = bad
        try:
            got = layout.resolve_config(None, self.root)
        finally:
            layout._tables = orig
        self.assertEqual(got, (self.root / "config.json").resolve())     # лишились на старому місці
        self.assertTrue((self.root / "config.json").exists())
        self.assertFalse((self.root / "channels" / "main").exists())
        self.assertFalse((self.root / "channels" / "main.tmp").exists())
        self.assertFalse((self.root / "channels" / ".migrate.lock").exists())

    def test_channels_list_skips_main(self):
        from app.channels import manager
        with self.assertRaises(ValueError):
            manager.channel_dir("main")
        sqlite3.connect(":memory:").close()


if __name__ == "__main__":
    unittest.main()
