"""Вмикання/вимикання платформ із Telegram і зміни налаштувань без перезапуску."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_flow import FlowBase  # noqa: E402


class HotReloadTest(FlowBase):
    def test_platform_toggle_from_telegram(self):
        self.app.bot.handle({"update_id": 1, "message": {"text": "🔌 Платформи", "chat": {"id": 42, "type": "private"}}})
        msg = self.app.bot.tg.sent[-1]
        self.assertIn("✅ TikTok", msg[1])
        self.assertIn("plat:0:tiktok", json.dumps(msg[2]))
        self.app.bot.handle({"update_id": 2, "callback_query": {
            "id": "c", "data": "plat:0:tiktok", "message": {"message_id": msg[-1] and 5, "chat": {"id": 42}, "text": ""}}})
        self.assertNotIn("tiktok", self.app.platforms)
        raw = json.loads(Path(self.cfg["config_path"]).read_text(encoding="utf-8"))
        self.assertFalse(raw["platforms"]["tiktok"]["enabled"])
        self.assertIn("🚫 TikTok", self.app.bot.tg.edits[-1][1])
        # новий шортс уже без TikTok
        self.make_short("a")
        self.app.scheduler.tick(self.ts(11, "12:00"))
        cap = [s for s in self.app.bot.tg.sent if s[0] == "video"][0][1]
        self.assertNotIn("TT", cap.split("Платформи:")[1].split("\n")[0])
        item = self.app.db.items("status='notified'")[0]
        self.press(f"pub:{item['id']}", item)
        self.app.scheduler.tick(self.ts(11, "13:00"))
        self.assertNotIn("tiktok", self.app.db.get_item(item["id"])["results"])
        # глядачам — не можна
        self.app.db.set_user(777, "viewer", "Друг")
        self.app.bot.handle({"update_id": 3, "message": {"text": "/platforms", "chat": {"id": 777, "type": "private"}}})
        self.assertIn("лише адміністратору", self.app.bot.tg.sent[-1][1])

    def test_config_change_applies_without_restart(self):
        path = Path(self.cfg["config_path"])
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["max_skips"] = 5
        raw.setdefault("platforms", {}).setdefault("instagram", {})["enabled"] = False
        path.write_text(json.dumps(raw), encoding="utf-8")
        import os
        os.utime(path, (os.path.getmtime(path) + 5,) * 2)
        self.app.scheduler.tick(self.ts(11, "08:00"))
        self.assertEqual(self.app.cfg["max_skips"], 5)
        self.assertNotIn("instagram", self.app.platforms)
        self.assertIs(self.app.scheduler.cfg, self.app.cfg)  # той самий об'єкт у всіх частинах


if __name__ == "__main__":
    unittest.main()
