"""Розклад наперед: кожне відео в черзі одразу отримує свій час публікації."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


class PlanTest(FlowBase):
    def plan(self, now):
        config.FAKE_NOW = now
        return [(Path(it["mp4"]).stem, config.fmt_when(self.cfg, ts, now) if ts else None)
                for it, ts in self.app.scheduler.projection(now)]

    def test_times_assigned_on_queue_and_spill_to_next_days(self):
        for n in "abcdefg":
            self.make_short(n, age=1000 - ord(n))
        config.FAKE_NOW = self.ts(11, "08:00")
        self.app.scheduler.scan()
        self.assertEqual(self.plan(self.ts(11, "08:00")), [
            ("a", "сьогодні 10:00 (11.11)"), ("b", "сьогодні 13:00 (11.11)"), ("c", "сьогодні 16:00 (11.11)"), ("d", "сьогодні 19:00 (11.11)"),
            ("e", "сьогодні 21:00 (11.11)"), ("f", "завтра 10:00 (12.11)"), ("g", "завтра 13:00 (12.11)")])
        msg = [s[1] for s in self.app.bot.tg.sent if s[0] == "text" and "Нові шортси в черзі: 7" in s[1]]
        self.assertEqual(len(msg), 1)
        self.assertIn("#1 a → <b>сьогодні 10:00 (11.11)</b>", msg[0])
        self.assertIn("#7 g → <b>завтра 13:00 (12.11)</b>", msg[0])
        # черга в Telegram теж показує час
        self.assertIn("a → <b>сьогодні 10:00 (11.11)</b>", self.app.queue_text())

    def test_slot_plan_shows_queue_items_and_status(self):
        for n in "ab":
            self.make_short(n, age=1000 - ord(n))
        self.app.scheduler.tick(self.ts(11, "09:00"))  # a сповіщено на 10:00
        plan = self.app.scheduler.slot_plan(self.ts(11, "09:00"), 4)
        self.assertEqual([(p["kind"], Path(p["item"]["mp4"]).stem if p["item"] else None) for p in plan],
                         [("notified", "a"), ("planned", "b"), ("empty", None), ("empty", None)])
        self.assertIn("#2 b — 📦 з черги", self.app.status_text())

    def test_game_of_ignores_tool_folders(self):
        from scheduler import game_of
        r = Path("/r")
        self.assertEqual(game_of(r / "battle_02_dmg_5004" / "x.mp4", r), "")
        self.assertEqual(game_of(r / "job_20261111_171232" / "battle_03_dmg_5051" / "x.mp4", r), "")
        self.assertEqual(game_of(r / "WoT" / "battle_01_assist_6133" / "x.mp4", r), "WoT")
        self.assertEqual(game_of(r / "Мир танков" / "x.mp4", r), "Мир танков")

    def test_announce_says_what_is_ahead(self):
        for n in "abc":
            self.make_short(n, age=1000 - ord(n))
        config.FAKE_NOW = self.ts(11, "08:00")
        self.app.scheduler.scan()
        for n in "de":
            self.make_short(n, age=500 - ord(n))
        self.app.scheduler.scan()
        msg = [s[1] for s in self.app.bot.tg.sent if s[0] == "text" and "Нові шортси в черзі: 2" in s[1]][0]
        self.assertIn("#4 d → <b>сьогодні 19:00 (11.11)</b>", msg)
        self.assertIn("Перед ними в черзі ще 3 — займають слоти: сьогодні 10:00, сьогодні 13:00, сьогодні 16:00", msg)

    def test_notification_matches_plan_and_skip_shifts(self):
        for n in "abc":
            self.make_short(n, age=1000 - ord(n))
        sch = self.app.scheduler
        sch.tick(self.ts(11, "09:00"))  # a -> 10:00 (уже сповіщено), b -> 13:00, c -> 16:00
        notified = self.app.db.items("status='notified'")[0]
        self.assertEqual((Path(notified["mp4"]).stem, notified["slot_at"]), ("a", self.ts(11, "10:00")))
        self.assertEqual(self.plan(self.ts(11, "09:00")), [("b", "сьогодні 13:00 (11.11)"), ("c", "сьогодні 16:00 (11.11)")])
        sch.tick(self.ts(11, "10:00"))  # a не підтверджено -> у кінець черги
        self.assertEqual(self.plan(self.ts(11, "10:00")),
                         [("b", "сьогодні 13:00 (11.11)"), ("c", "сьогодні 16:00 (11.11)"), ("a", "сьогодні 19:00 (11.11)")])
        sch.tick(self.ts(11, "12:00"))  # сповіщення 13:00 — саме для b
        notified = self.app.db.items("status='notified'")[0]
        self.assertEqual(Path(notified["mp4"]).stem, "b")

    def test_pause_and_manual_slot(self):
        self.make_short("a")
        self.make_short("b", age=110)
        config.FAKE_NOW = self.ts(11, "11:00")
        self.app.scheduler.scan()
        self.assertEqual(self.plan(self.ts(11, "11:00")), [("a", "сьогодні 13:00 (11.11)"), ("b", "сьогодні 16:00 (11.11)")])
        self.app.scheduler.add_manual_slot(30)  # 11:30
        self.assertEqual(self.plan(self.ts(11, "11:00")), [("a", "сьогодні 11:30 (11.11)"), ("b", "сьогодні 13:00 (11.11)")])
        self.app.db.set("paused", "1")
        self.assertEqual(self.plan(self.ts(11, "11:00")), [("a", None), ("b", None)])
        self.assertIn("на паузі", self.app.queue_text())

    def test_when_format(self):
        now = self.ts(11, "08:00")  # середа
        self.assertEqual(config.fmt_when(self.cfg, self.ts(13, "16:00"), now), "пт 13.11 16:00")


if __name__ == "__main__":
    unittest.main()
