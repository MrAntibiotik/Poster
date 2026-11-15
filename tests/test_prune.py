"""Черга відповідає папці: видалені файли й відео зі старої папки прибираються, вкладені папки скануються."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


class PruneTest(FlowBase):
    def video(self, rel: str, root: Path | None = None) -> Path:
        p = (root or self.cfg["paths"]["ready"]) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"0" + str(p).encode())
        os.utime(p, (os.path.getmtime(p) - 300,) * 2)
        return p

    def names(self):
        config.FAKE_NOW = self.ts(11, "08:00")
        return [Path(it["mp4"]).stem for it, _ in self.app.scheduler.projection(self.ts(11, "08:00"))]

    def test_deleted_files_leave_queue(self):
        vids = [self.video(f"WoT/v{i}.mp4") for i in range(5)]
        config.FAKE_NOW = self.ts(11, "08:00")
        self.app.scheduler.scan()
        self.assertEqual(len(self.names()), 5)
        for v in vids[:3]:
            v.unlink()
        self.assertEqual(self.names(), ["v3", "v4"])  # одразу не показуються
        self.app.scheduler.scan()
        msg = [s[1] for s in self.app.bot.tg.sent if s[0] == "text" and "Прибрано з черги: 3" in s[1]]
        self.assertEqual(len(msg), 1)
        self.assertIn("файл видалено", msg[0])
        plan = self.app.scheduler.projection(self.ts(11, "08:00"))
        self.assertEqual(config.fmt_when(self.cfg, plan[0][1], self.ts(11, "08:00")), "сьогодні 10:00 (11.11)")  # зсунулись

    def test_folder_change_and_back(self):
        old_root = self.cfg["paths"]["ready"]
        self.video("WoT/old1.mp4")
        config.FAKE_NOW = self.ts(11, "08:00")
        self.app.scheduler.scan()
        new_root = self.tmp / "ShortsTool" / "output"
        self.video("job_20261111_171232/battle_03_dmg_5051/1_battle_03.mp4", new_root)  # 3 рівні вкладеності
        self.cfg["paths"]["ready"] = new_root
        self.app.scheduler.scan()
        self.assertEqual(self.names(), ["1_battle_03"])
        q = self.app.db.queue()
        self.assertEqual(q[0]["game"], "")  # папки програми нарізки — не назва гри
        self.cfg["paths"]["ready"] = old_root  # повернулись до старої папки
        self.app.scheduler.scan()
        self.assertEqual(self.names(), ["old1"])
        self.assertEqual(len(self.app.db.items("mp4 LIKE '%old1%'")), 1)  # той самий запис, не дубль

    def test_notified_file_deleted_frees_slot(self):
        v = self.video("WoT/a.mp4")
        self.video("WoT/b.mp4")
        self.app.scheduler.tick(self.ts(11, "09:00"))  # перший отримав сповіщення на 10:00
        item = self.app.db.items("status='notified'")[0]
        Path(item["mp4"]).unlink()
        config.FAKE_NOW = self.ts(11, "09:05")
        self.app.scheduler.scan()
        self.assertEqual(self.app.db.get_item(item["id"])["status"], "missing")
        self.assertEqual(self.app.db.slot(self.ts(11, "10:00"))["state"], "pending")
        self.app.scheduler.tick(self.ts(11, "09:06"))  # слот 10:00 отримує наступне відео
        nxt = self.app.db.items("status='notified'")
        self.assertEqual([Path(i["mp4"]).stem for i in nxt], ["b" if Path(v).stem == "a" else "a"])

    def test_posted_inside_ready_is_ignored(self):
        self.cfg["paths"]["posted"] = self.cfg["paths"]["ready"] / "posted"
        self.video("posted/2026-11-10/done.mp4")
        self.video("WoT/new.mp4")
        config.FAKE_NOW = self.ts(11, "08:00")
        self.app.scheduler.scan()
        self.assertEqual(self.names(), ["new"])


if __name__ == "__main__":
    unittest.main()
