"""Історія версій у боті: список, підтвердження, перехід (відкат) і перезапуск."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.system import updater  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


class VersionsBotTest(FlowBase):
    def cb(self, data, mid=500):
        self.app.bot._bg = lambda fn, *a: fn(*a)
        self.app.bot.handle({"update_id": 1, "callback_query": {
            "id": "cb", "data": data, "message": {"message_id": mid, "chat": {"id": 42}}}})

    def test_list_confirm_switch(self):
        from app.core.config import VERSION
        vl = [{"version": "2099.01.01-2", "local": False, "remote": True, "current": False, "notes": ["Нове"]},
              {"version": VERSION, "local": False, "remote": True, "current": True, "notes": []},
              {"version": "2026.01.01-5", "local": True, "remote": False, "current": False, "notes": ["Старе <b>"]}]
        tg = self.app.bot.tg
        with mock.patch.object(updater, "version_list", return_value=vl):
            self.cb("ver:-1")
        text, kb = tg.edits[-1][1], tg.edits[-1][2]
        self.assertIn("Нове", text)
        self.assertIn("Старе &lt;b&gt;", text)                 # опис екранується
        labels = [b["text"] for row in kb["inline_keyboard"] for b in row]
        self.assertIn("⬆️ 01.01-2", labels)
        self.assertIn("↩️ 01.01-5", labels)
        self.cb("ver:2")                                          # підтвердження
        self.assertIn("Перейти на версію <b>2026.01.01-5</b>", tg.edits[-1][1])
        with mock.patch.object(updater, "switch_to") as sw:
            self.cb("ver:2:go")
        sw.assert_called_once_with(self.app.cfg, "2026.01.01-5")
        self.assertIn("перезапускаюсь", tg.edits[-1][1])
        self.assertEqual(self.app.db.get("control"), "restart")

    def test_switch_refused_while_publishing(self):
        self.app.publisher.active.add(7)
        with mock.patch.object(updater, "switch_to") as sw, self.assertRaisesRegex(ValueError, "публікується"):
            self.app.bot.switch_version("2026.01.01-5")
        sw.assert_not_called()

    def test_stale_index(self):
        self.cb("ver:5")
        self.assertIn("Список застарів", self.app.bot.tg.answers[-1])


if __name__ == "__main__":
    unittest.main()
