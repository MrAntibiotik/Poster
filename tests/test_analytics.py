"""Аналітика: підписники/перегляди/лайки по платформах у щоденному звіті, лідери тижня, кнопки й меню."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from reports import analytics_text, collect, weekly_top  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402


class FakePlatform:
    """Статистика, яку можна змінювати між «днями»."""

    def __init__(self, key, followers):
        self.key, self.followers = key, followers
        self.views: dict[str, int] = {}

    def publish(self, path, meta):
        pid = f"{self.key}-{len(self.views) + 1}"
        self.views[pid] = 0
        return {"id": pid, "url": f"https://{self.key}/{pid}"}

    def get_stats(self, res):
        v = self.views.get(res["id"], 0)
        return {"views": v, "likes": v // 10, "comments": 1}

    def account_stats(self):
        return {"followers": self.followers}


class AnalyticsTest(FlowBase):
    def setUp(self):
        super().setUp()
        self.cfg["dry_run"] = False
        self.yt, self.tt = FakePlatform("youtube", 1000), FakePlatform("tiktok", 500)
        self.app.platforms = {"youtube": self.yt, "tiktok": self.tt}

    def publish(self, name, day, hm):
        self.make_short(name)
        h, m = map(int, hm.split(":"))
        self.app.scheduler.tick(self.ts(day, f"{h - 1:02d}:{m:02d}"))
        it = self.app.db.items("status='notified'")[0]
        self.press(f"pub:{it['id']}", it)
        self.app.scheduler.tick(self.ts(day, hm))
        return self.app.db.get_item(it["id"])

    def reports(self):
        return [s[1] for s in self.app.bot.tg.sent if s[0] == "text" and "Підсумок" in s[1]]

    def test_daily_report_with_deltas_and_weekly_top(self):
        a = self.publish("a", 11, "10:00")
        b = self.publish("b", 11, "13:00")
        self.yt.views.update({a["results"]["youtube"]["id"]: 300, b["results"]["youtube"]["id"]: 900})
        self.tt.views.update({a["results"]["tiktok"]["id"]: 50, b["results"]["tiktok"]["id"]: 20})
        self.app.scheduler.tick(self.ts(11, "22:31"))
        r1 = self.reports()[-1]
        self.assertIn("Вийшло шортсів: <b>2</b>", r1)
        self.assertIn("У черзі: <b>0</b>", r1)
        self.assertIn("<b>YouTube</b>: 👥 1 000", r1)
        self.assertIn("👁 +1 200 · ❤️ +120 (шортси)", r1)  # опубліковані сьогодні — уся статистика за добу
        self.assertIn("<b>TikTok</b>: 👥 500", r1)

        # наступна доба: +25 підписників YouTube, +600 переглядів
        self.yt.followers, self.tt.followers = 1025, 490
        self.yt.views[a["results"]["youtube"]["id"]] = 900
        self.make_short("c")  # лежить у черзі
        self.app.scheduler.tick(self.ts(12, "22:31"))
        r2 = self.reports()[-1]
        self.assertIn("👥 1 025 (+25)", r2)
        self.assertIn("👥 490 (−10)", r2)
        self.assertIn("👁 +600 · ❤️ +60 (шортси)", r2)
        self.assertIn("У черзі: <b>1</b>", r2)

        # лідери тижня (неділя 15.11.2026)
        self.app.scheduler.tick(self.ts(15, "22:31"))
        top = [s[1] for s in self.app.bot.tg.sent if s[0] == "text" and "Лідери тижня" in s[1]][-1]
        self.assertLess(top.index("Титул a"), top.index("Титул b"))  # a: 900+50=950 > b: 900+20=920
        stats = collect(self.app)
        wt = weekly_top(self.app, stats)
        self.assertIn("🥇 <b>Титул a</b>", wt)  # a: 900 + 50 = 950 > b: 900 + 20 = 920
        self.assertIn("Усього за тиждень: 2 шортсів", wt)

    def test_buttons_menu_and_viewer_access(self):
        tg = self.app.bot.tg
        calls = []
        tg.call = lambda method, **kw: calls.append((method, kw))
        self.app.bot.setup_commands()
        self.assertEqual(calls[0][0], "setMyCommands")
        names = [c["command"] for c in calls[0][1]["commands"]]
        self.assertTrue({"menu", "stats", "top", "queue"} <= set(names))
        self.assertEqual(calls[1][1]["menu_button"], {"type": "commands"})

        self.app.bot.handle({"update_id": 1, "message": {"text": "/menu", "chat": {"id": 42, "type": "private"}}})
        kb = tg.sent[-1][2]["keyboard"]
        labels = [b["text"] for row in kb for b in row]
        self.assertTrue({"📈 Аналітика", "🏆 Топ тижня", "📋 Черга", "⏸ Пауза"} <= set(labels))

        # глядач теж бачить аналітику
        self.app.db.set_user(777, "viewer", "Друг")
        self.app.bot.handle({"update_id": 2, "message": {"text": "📈 Аналітика", "chat": {"id": 777, "type": "private"}}})
        out = [s[1] for s in tg.sent if s[-1] == 777] + [e[1] for e in tg.edits]  # «⏳…» стає результатом
        self.assertTrue(any("📈 <b>Аналітика</b>" in t for t in out))
        self.assertTrue(any("YouTube" in t for t in out))

    def test_dry_run_and_errors(self):
        self.cfg["dry_run"] = True
        stats = collect(self.app)
        self.assertIn("Тестовий режим", analytics_text(self.app, stats))
        self.cfg["dry_run"] = False

        class Broken(FakePlatform):
            def account_stats(self):
                raise RuntimeError("немає доступу")
        self.app.platforms = {"youtube": Broken("youtube", 0)}
        config.FAKE_NOW = self.ts(11, "20:00")
        text = analytics_text(self.app, collect(self.app))
        self.assertIn("⚠️ немає доступу", text)


if __name__ == "__main__":
    unittest.main()
