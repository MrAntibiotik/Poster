"""Ярлик панелі на робочому столі (Windows)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import control  # noqa: E402


class ShortcutTest(unittest.TestCase):
    def test_script(self):
        s = control.shortcut_script()
        self.assertIn("GetFolderPath('Desktop')", s)  # і для OneDrive-робочого столу
        self.assertIn("panel.py", s)
        self.assertIn("poster.ico,0", s)
        self.assertIn(control.SHORTCUT_NAME, s)
        self.assertTrue((control.ROOT / "poster.ico").exists())

    def test_once_noop_off_windows(self):
        class DB(dict):
            def get(self, k, d=None):
                return dict.get(self, k, d)

            def set(self, k, v):
                self[k] = v
        db = DB()
        control.ensure_shortcut_once(db)
        self.assertEqual(db.get("shortcut_done"), "1" if control.WIN else None)


if __name__ == "__main__":
    unittest.main()
