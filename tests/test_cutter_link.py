"""Різак переніс відео (Робочий стіл\\Контент) — Poster сам переходить на нові шляхи, черга не губиться."""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.channels import cutter_link  # noqa: E402
from app.core import config  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


class CutterLinkTest(FlowBase):
    def setUp(self):
        super().setUp()
        self._env = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = str(self.tmp / "appdata")

    def tearDown(self):
        if self._env is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self._env
        super().tearDown()

    def cutter_moves(self, old: Path, new: Path):
        f = cutter_link.discovery_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"folders": {"highlights": str(self.tmp / "Контент" / "Хайлайти")},
                                 "moved": [[str(old), str(new)]]}), encoding="utf-8")

    def test_ready_folder_and_queue_follow_the_move(self):
        self.make_short("a")
        config.FAKE_NOW = self.ts(11, "08:00")
        self.app.scheduler.scan()
        item = self.app.db.queue()[0]
        old = Path(self.cfg["paths"]["ready"])
        new = self.tmp / "Контент" / "Мій канал" / "До публікації"
        new.parent.mkdir(parents=True)
        shutil.move(str(old), str(new))                         # Різак переніс папку
        self.cutter_moves(old, new)
        self.app.scheduler.scan()
        raw = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["paths"]["ready"], str(new))       # налаштування Poster — на новій папці
        it = self.app.db.get_item(item["id"])
        self.assertEqual(it["status"], "queued")               # не «зник» і не задвоївся
        self.assertTrue(it["mp4"].startswith(str(new)))
        self.assertEqual(len(self.app.db.queue()), 1)
        self.assertFalse(old.exists())                          # стару папку не створили знову

    def test_missing_item_comes_back(self):
        self.make_short("a")
        config.FAKE_NOW = self.ts(11, "08:00")
        self.app.scheduler.scan()
        item = self.app.db.queue()[0]
        old = Path(self.cfg["paths"]["ready"])
        new = self.tmp / "moved_ready"
        shutil.move(str(old), str(new))
        old.mkdir()
        self.app.scheduler.prune_queue(old)                     # Poster встиг вирішити, що файл зник
        self.assertEqual(self.app.db.get_item(item["id"])["status"], "missing")
        self.cutter_moves(old, new)
        self.app.scheduler.scan()
        self.assertEqual(self.app.db.get_item(item["id"])["status"], "queued")

    def test_highlights_folder_from_cutter(self):
        self.cutter_moves(self.tmp / "x", self.tmp / "y")
        cfg = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        cfg["highlights"] = {"enabled": True, "folder": ""}
        (self.tmp / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        loaded = config.load_config(self.tmp / "config.json")
        self.assertEqual(loaded["highlights"]["folder_path"], self.tmp / "Контент" / "Хайлайти")

    def test_remap_rules(self):
        moved = [["C:\\ShortsTool\\output", "D:\\K\\Нарізки"],
                 ["C:\\ShortsTool\\output\\_до_публікації", "D:\\K\\До публікації"]]
        self.assertEqual(cutter_link.remap("C:\\ShortsTool\\output\\_до_публікації\\a.mp4", moved),
                         "D:\\K\\До публікації\\a.mp4")                      # точніший шлях — першим
        self.assertEqual(cutter_link.remap("C:\\ShortsTool\\output\\job_1\\b.mp4", moved), "D:\\K\\Нарізки\\job_1\\b.mp4")
        self.assertIsNone(cutter_link.remap("C:\\ShortsTool\\output2\\b.mp4", moved))   # не плутати схожі назви


if __name__ == "__main__":
    unittest.main()


class FindMovedTest(CutterLinkTest):
    """Перенесення, яких немає в історії Різака (напр. двічі поспіль, у «Відео\\Контент»): шукаємо за назвою."""

    def publish_dir(self, d: Path):
        f = cutter_link.discovery_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"content": str(self.tmp / "Відео" / "Контент"), "publish": str(d), "moved": []}),
                     encoding="utf-8")

    def test_unrecorded_move_is_found_by_name(self):
        self.make_short("a")
        self.make_short("b")
        config.FAKE_NOW = self.ts(11, "08:00")
        self.app.scheduler.scan()
        self.assertEqual(len(self.app.db.queue()), 2)
        old = Path(self.cfg["paths"]["ready"])
        new = self.tmp / "Відео" / "Контент" / "Мій канал" / "До публікації"
        new.parent.mkdir(parents=True)
        shutil.move(str(old), str(new))                         # перенесли, але в «moved» нічого
        self.publish_dir(new)
        self.app.scheduler.scan()
        q = self.app.db.queue()
        self.assertEqual(len(q), 2)                             # нічого не загубилось
        self.assertTrue(all(it["mp4"].startswith(str(new)) for it in q))
        raw = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["paths"]["ready"], str(new))       # Poster перейшов на папку Різака

    def test_already_removed_items_are_recovered(self):
        self.make_short("a")
        config.FAKE_NOW = self.ts(11, "08:00")
        self.app.scheduler.scan()
        it = self.app.db.queue()[0]
        old = Path(self.cfg["paths"]["ready"])
        new = self.tmp / "Відео" / "Контент" / "Мій канал" / "До публікації"
        new.parent.mkdir(parents=True)
        shutil.move(str(old), str(new))
        self.app.db.update_item(it["id"], status="missing", note="файл видалено")   # стара версія вже прибрала
        self.publish_dir(new)
        self.app.db.set("recover_ts", 0)
        self.app.scheduler.scan()
        back = self.app.db.get_item(it["id"])
        self.assertEqual(back["status"], "queued")
        self.assertTrue(back["mp4"].startswith(str(new)))
        self.assertEqual(len(self.app.db.queue()), 1)          # без дубля


class VideosContentTest(CutterLinkTest):
    """Різак переніс усе в Відео\\Контент і не повідомив Poster (немає cutter.json): знаходимо й не прибираємо."""

    def test_found_in_videos_content_and_kept(self):
        from unittest import mock
        self.make_short("a")
        self.make_short("b")
        config.FAKE_NOW = self.ts(11, "08:00")
        self.app.scheduler.scan()
        ids = [i["id"] for i in self.app.db.queue()]
        old = Path(self.cfg["paths"]["ready"])
        content = self.tmp / "Videos" / "Контент"
        cuts = content / "Мій канал" / "Нарізки" / "_до_публікації"
        cuts.parent.mkdir(parents=True)
        (content / "Мій канал" / "До публікації").mkdir(parents=True)
        shutil.move(str(old), str(cuts))
        with mock.patch.object(cutter_link, "content_guesses", return_value=[content]):
            self.app.scheduler.scan()
            self.app.scheduler.scan()                        # і наступні скани — нічого не прибирають
        q = self.app.db.queue()
        self.assertEqual(sorted(i["id"] for i in q), sorted(ids))
        self.assertTrue(all(str(cuts) in i["mp4"] for i in q))
        raw = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["paths"]["ready"], str(content / "Мій канал" / "До публікації"))
