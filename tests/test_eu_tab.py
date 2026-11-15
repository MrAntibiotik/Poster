"""Розділ «🇬🇧 EU-канал» у головному боті: черга EN-каналу і керування ним."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import channels  # noqa: E402
import eu_tab  # noqa: E402
from db import DB  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


class EuTabTest(FlowBase):
    def setUp(self):
        super().setUp()
        self.p = mock.patch.object(channels, "CHANNELS_DIR", self.tmp / "channels")
        self.p.start()

    def tearDown(self):
        self.p.stop()
        super().tearDown()

    def kb_texts(self, kb):
        return [b["text"] for row in kb["keyboard"] for b in row]

    def test_hidden_until_channel_exists(self):
        self.assertFalse(eu_tab.available(self.cfg))
        self.assertNotIn("🇬🇧 EU-канал", self.kb_texts(self.app.bot.reply_keyboard(42)))
        channels.create(self.cfg, "en")
        self.assertTrue(eu_tab.available(self.cfg))
        self.assertIn("🇬🇧 EU-канал", self.kb_texts(self.app.bot.reply_keyboard(42)))
        self.assertFalse(eu_tab.available({**self.cfg, "channel": {"key": "en"}}))   # у боті самого EN-каналу — ні

    def test_view_and_controls(self):
        import json
        channels.create(self.cfg, "en")
        en = DB(channels.db_path("en"))
        ready = Path(channels.status("en")["ready"]) / "WoT"
        ready.mkdir(parents=True, exist_ok=True)
        stem = "replay_wr_448222_Fossa_VM_68_Fisherman_s_Bay_2026_11_15_12_52_35_battle_01_dmg_11396_assist_466"
        (ready / f"{stem}.mp4").write_bytes(b"0")
        (ready / f"{stem}.json").write_text(json.dumps({"status": "ready_to_post"}), encoding="utf-8")
        en.add_item(str(ready / f"{stem}.mp4"), str(ready / f"{stem}.json"), "WoT")
        with mock.patch.object(channels.control, "is_running", return_value=False):
            self.app.bot.handle({"update_id": 1, "message": {"message_id": 5, "text": "🇬🇧 EU-канал",
                                                              "chat": {"id": 42, "type": "private"}}})
            text = self.app.bot.tg.edits[-1][1] if self.app.bot.tg.edits else self.app.bot.tg.sent[-1][1]
            self.assertIn("EN-канал</b> · 🔴 бот зупинено", text)
            self.assertIn("📦 <b>Черга: 1</b>", text)
            self.assertIn("1. Fossa VM 68 Fisherman's Bay · 11 396 шкоди", text)  # зрозуміла назва, не ім'я файлу
            self.assertRegex(text, r"→ \S+ \d\d:\d\d \(\d\d:\d\d NY\)")  # київський час, у дужках — Нью-Йорк
            self.assertIn("🔑 YouTube не підключено", text)
            # пауза і стоп публікацій — у базі EN-каналу
            for op in ("pause", "stop"):
                self.app.bot.handle({"update_id": 2, "callback_query": {
                    "id": "c", "data": f"eu:0:{op}", "message": {"message_id": 99, "chat": {"id": 42}, "text": ""}}})
        self.assertEqual((en.get("paused"), en.get("stopped")), ("1", "1"))
        self.assertIsNone(self.app.db.get("stopped"))                     # основний канал не чіпали
        self.assertIn("⛔ Публікації EU-каналу зупинено", self.app.bot.tg.answers)
        text, kb = eu_tab.view(queue=False)
        labels = [b["text"] for row in kb["inline_keyboard"] for b in row]
        self.assertIn("▶️ Продовжити розклад", labels)
        self.assertIn("✅ Відновити публікації", labels)
