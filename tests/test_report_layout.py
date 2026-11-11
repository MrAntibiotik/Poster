"""Щоденний звіт: по шортсах, помилки згруповано за причиною, виправлені повторами — окремо."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from reports import daily_report, explain_error  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


class ReportLayoutTest(FlowBase):
    def test_grouped_by_short_and_reason(self):
        self.cfg["always_hashtags"] = {"tags": ["#wot", "#танки"], "in_title": True}
        for n in "ab":
            self.make_short(n, age=1000 - ord(n))
        config.FAKE_NOW = self.ts(11, "08:00")
        self.app.scheduler.scan()
        db = self.app.db
        a, b = db.items("1")[:2]
        db.update_item(a["id"], status="posted", posted_at=self.ts(11, "20:00"), results={
            "youtube": {"status": "ok", "url": "https://youtu.be/x"},
            "instagram": {"status": "ok", "url": "https://instagram.com/r/1"},
            "tiktok": {"status": "error", "error": "авторизація: немає токена TikTok"}})
        db.update_item(b["id"], status="failed", results={
            "youtube": {"status": "error", "error": "[WinError 3] The system cannot find the path specified: 'C:\\\\x.mp4'"},
            "tiktok": {"status": "error", "error": "авторизація: немає токена TikTok"}})
        for _ in range(3):
            db.event(a["id"], "platform_error", {"platform": "instagram", "error": "Graph API HTTP 400 ProcessingFailedError"})
        db.event(a["id"], "published", {"title": "A #wot #танки", "ok": ["youtube"], "failed": []})
        db.event(b["id"], "publish_failed", {"title": "B", "failed": ["youtube"]})
        r = daily_report(self.app, self.ts(11, "22:30"))
        print("\n" + r)
        self.assertIn("Вийшло шортсів: <b>1</b>", r)
        self.assertIn("не вийшло: <b>1</b>", r)
        self.assertIn('▶️ <a href="https://youtu.be/x">YouTube</a>', r)
        self.assertNotIn("#танки</b>", r)  # обов'язкові хештеги не захаращують назву
        self.assertEqual(r.count("TikTok не підключено"), 1)  # одна група на дві помилки
        self.assertIn("<b>Файл відео не знайдено</b>: 1 шортс (#2)", r)
        self.assertIn("#1, 20:00", r)
        self.assertIn("Ще 3 помилки виправили повторні спроби", r)
        self.assertNotIn("WinError", r)

    def test_explain(self):
        self.assertEqual(explain_error("instagram", "Graph API HTTP 400: ProcessingFailedError")[0], "ig_processing")
        self.assertEqual(explain_error("instagram", "авторизація: не задано META_PAGE_TOKEN")[0], "meta_token")


if __name__ == "__main__":
    unittest.main()
