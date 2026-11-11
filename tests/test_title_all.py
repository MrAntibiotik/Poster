"""Назва, введена вручну, потрапляє в підписи всіх платформ."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meta import load_meta  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


class TitleAllTest(FlowBase):
    def test_manual_title_button_updates_all_captions(self):
        mp4 = self.make_short("a")
        self.app.scheduler.tick(self.ts(11, "12:00"))
        item = self.app.db.items("status='notified'")[0]
        self.press(f"title:{item['id']}", item)
        self.app.bot.handle({"update_id": 2, "message": {"text": "Перші враження від 11лвл",
                                                          "chat": {"id": 42, "type": "private"}}})
        m = load_meta(mp4.with_suffix(".json"))
        self.assertEqual(m.yt_title, "Перші враження від 11лвл")
        self.assertTrue(m.instagram_text.startswith("Перші враження від 11лвл"))
        self.assertTrue(m.tiktok_text.startswith("Перші враження від 11лвл"))
        self.assertIn("#wot", m.instagram_text)  # хештеги лишились
        self.assertNotIn("Титул a", m.instagram_text + m.tiktok_text + m.facebook_text)

    def test_old_override_also_retitles_captions(self):
        mp4 = self.make_short("a")
        m = load_meta(mp4.with_suffix(".json"), "Нова назва")
        self.assertEqual(m.yt_title, "Нова назва")
        self.assertIn("Нова назва", m.instagram_text)
        self.assertIn("Нова назва", m.tiktok_text)


if __name__ == "__main__":
    unittest.main()
