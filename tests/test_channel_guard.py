"""Різний контент не змішується: шортси з реплеїв (EN-канал) не потрапляють у чергу основного каналу."""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")
    old = time.time() - 120  # «старий» файл — Poster не чекає, поки його допишуть
    os.utime(path, (old, old))


class ChannelGuardTest(FlowBase):
    def scan(self):
        config.FAKE_NOW = self.ts(11, "08:00")
        self.app.scheduler.scan()
        return sorted(Path(i["mp4"]).stem for i in self.app.db.queue())

    def test_replay_json_is_not_for_main_channel(self):
        self.make_short("mine")
        rp = self.make_short("replay_5000001_battle_01")
        data = json.loads(rp.with_suffix(".json").read_text(encoding="utf-8"))
        data.update(source="replay", channel="en")
        write_json(rp.with_suffix(".json"), data)
        self.assertEqual(self.scan(), ["mine"])

    def test_marked_folder_without_json_is_skipped(self):
        # напр. папка готових шортсів основного каналу = уся папка програми нарізки, де є output_replays_en
        self.make_short("mine")
        eu = self.make_short("battle_02", game="output_replays_en")
        eu.with_suffix(".json").unlink()
        (eu.parent / ".poster_channel").write_text("en", encoding="utf-8")
        self.assertEqual(self.scan(), ["mine"])
        self.assertFalse(eu.with_suffix(".json").exists())  # json для чужого відео навіть не створюється

    def test_already_queued_foreign_item_is_removed(self):
        self.make_short("mine")
        rp = self.make_short("replay_x")
        self.assertEqual(self.scan(), ["mine", "replay_x"])  # потрапив раніше, до захисту
        data = json.loads(rp.with_suffix(".json").read_text(encoding="utf-8"))
        data["source"] = "replay"
        write_json(rp.with_suffix(".json"), data)
        self.assertEqual(self.scan(), ["mine"])
        item = next(i for i in self.app.db.items("1") if Path(i["mp4"]).stem == "replay_x")
        self.assertEqual(item["status"], "missing")
        self.assertIn("іншого каналу", item["note"])
        self.assertTrue(rp.exists())  # файл не чіпаємо — він для EN-каналу

    def test_en_channel_takes_its_own(self):
        cfg = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        cfg["channel"] = {"key": "en", "name": "EN"}
        (self.tmp / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.cfg = config.load_config(self.tmp / "config.json")
        self.app = self._app()
        rp = self.make_short("replay_5000001_battle_01")
        data = json.loads(rp.with_suffix(".json").read_text(encoding="utf-8"))
        data.update(source="replay", channel="en")
        write_json(rp.with_suffix(".json"), data)
        self.assertEqual(self.scan(), ["replay_5000001_battle_01"])


if __name__ == "__main__":
    unittest.main()
