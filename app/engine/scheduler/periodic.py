"""Періодичні задачі: денний звіт, статистика, AI-підписи, перевірка ключів."""
from __future__ import annotations

import threading
from app.core.config import local_dt
from app.core.config import now_ts
from app.core.meta import read_json
from .common import (
    log,
)


class PeriodicMixin:
    def maybe_daily_report(self, now: int) -> None:
        dt = local_dt(self.cfg, now)
        if dt.strftime("%H:%M") < self.cfg["reports"]["daily_time"]:
            return
        day = dt.strftime("%Y-%m-%d")
        if self.db.get("daily_report_day") == day:
            return
        self.db.set("daily_report_day", day)
        weekly = dt.weekday() == int(self.cfg["reports"].get("weekly_day", 6))

        def work():
            from app.insights.reports import collect, daily_report, weekly_top
            stats = None
            try:
                stats = collect(self.app)
            except Exception:  # noqa: BLE001
                log.exception("Збір статистики для звіту")
            self.app.bot.send(daily_report(self.app, now, stats), html_mode=True, to="all", kind="daily")
            if weekly and stats:
                self.app.bot.send(weekly_top(self.app, stats), html_mode=True, to="all", kind="weekly")
        if getattr(self.app.publisher, "sync", False):
            work()
        else:
            threading.Thread(target=work, daemon=True, name="daily-report").start()

    def maybe_stats(self, now: int) -> None:
        if self._stats_thread and self._stats_thread.is_alive():
            return
        hours = float(self.cfg["reports"]["stats_after_hours"])
        due = self.db.items("status='posted' AND stats_sent=0 AND posted_at<=?", (now - int(hours * 3600),),
                            order="posted_at")
        if not due:
            return
        from app.insights.reports import send_stats

        def work():
            for item in due[:5]:
                send_stats(self.app, item)
        if getattr(self.app.publisher, "sync", False):
            work()
        else:
            self._stats_thread = threading.Thread(target=work, daemon=True, name="stats")
            self._stats_thread.start()

    def maybe_ai(self) -> None:
        """Шортсам із текстами «з імені файлу» замовляє тексти в ШІ (у фоні, до сповіщення)."""
        from app.core import ai
        if not ai.enabled(self.cfg) or (self._ai_thread and self._ai_thread.is_alive()):
            return
        todo = []
        for item in self.db.items("status IN ('queued','notified') AND kind!='highlight'"):
            if int(self.db.get(f"ai_tries:{item['id']}") or 0) >= 3:
                continue
            try:
                data = read_json(item["json"])
            except (OSError, ValueError):
                continue
            if data.get("auto_generated") and not data.get("ai_generated"):
                todo.append(item)
        if not todo:
            return

        def work():
            for item in todo[:5]:
                key = f"ai_tries:{item['id']}"
                self.db.set(key, int(self.db.get(key) or 0) + 1)
                try:
                    if ai.enrich(self.cfg, item):
                        self.db.event(item["id"], "ai_texts")
                        fresh = self.db.get_item(item["id"])
                        if fresh and fresh["status"] in ("notified", "approved"):
                            self.app.bot.refresh(item["id"], texts=True)
                except ai.AIError as e:
                    log.warning("ШІ для #%s: %s", item["id"], e)
                    if "ліміт" in str(e):
                        break
                except Exception:  # noqa: BLE001
                    log.exception("ШІ для #%s", item["id"])
        if getattr(self.app.publisher, "sync", False):
            work()
        else:
            self._ai_thread = threading.Thread(target=work, daemon=True, name="ai")
            self._ai_thread.start()

    def maybe_health_check(self, now: int) -> None:
        """Раз на день (і при запуску) перевіряє доступ до платформ, щоб про протерміновані
        токени дізнатися до слоту, а не під час публікації."""
        if self.cfg.get("dry_run") or (self._health_thread and self._health_thread.is_alive()):
            return
        dt = local_dt(self.cfg, now)
        day = dt.strftime("%Y-%m-%d")
        if self._health_started:
            if dt.strftime("%H:%M") < self.cfg["reports"]["health_check_time"] or self.db.get("health_day") == day:
                return
            self.db.set("health_day", day)
        self._health_started = True

        if getattr(self.app.publisher, "sync", False):
            self.health_check()
        else:
            self._health_thread = threading.Thread(target=self.health_check, daemon=True, name="health")
            self._health_thread.start()

    def health_check(self) -> dict[str, str]:
        from app.platforms import TokenExpired

        out = {}
        for key, plat in self.app.platforms.items():
            try:
                info = plat.check()
                out[key] = f"✅ {info}"
                self.app.bot.resolve(f"reauth_{key}")  # доступ є — повідомлення «потрібна авторизація» прибираємо
                warn = plat.token_warning()
                if warn:
                    out[key] = f"⚠️ {warn}"
                    if self.db.get(f"warn_{key}") != local_dt(self.cfg, now_ts()).strftime("%Y-%m-%d"):
                        self.db.set(f"warn_{key}", local_dt(self.cfg, now_ts()).strftime("%Y-%m-%d"))
                        self.app.bot.send(warn)
            except TokenExpired as e:
                out[key] = f"🔑 потрібна авторизація: {e}"
                self.app.bot.notify_reauth(key, str(e))
            except Exception as e:  # noqa: BLE001 — мережа тощо: не тривожимо, лише журнал
                out[key] = f"❓ не вдалося перевірити: {str(e)[:150]}"
                log.warning("Перевірка %s: %s", key, e)
            self.db.set(f"health_{key}", out[key])
        self.db.set("health_ts", now_ts())
        log.info("Перевірка доступів: %s", out)
        return out
