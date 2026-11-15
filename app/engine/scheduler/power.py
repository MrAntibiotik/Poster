"""Сон ПК між слотами, стрім, пробудження."""
from __future__ import annotations

import time
from app.core.config import fmt_slot
from app.core.config import local_dt
from datetime import datetime
from datetime import timedelta
from .common import (
    log,
)


class PowerMixin:
    def wake_times(self, now: int, days: int = 3) -> list[int]:
        """Коли прокидатися: за N хв до кожного слоту (наступні дні), повторів і щоденного звіту."""
        before = int((self.cfg.get("power") or {}).get("wake_before_minutes", 5)) * 60
        out = set()
        today = local_dt(self.cfg, now).date()
        for d in range(days):
            for ts in self._day_slots(today + timedelta(days=d)):
                out.add(ts - before)
            h, m = map(int, self.cfg["reports"]["daily_time"].split(":"))
            day = today + timedelta(days=d)
            out.add(int(datetime(day.year, day.month, day.day, h, m, tzinfo=self.cfg["tz"]).timestamp()) - 60)
        for r in self.db.q("SELECT slot_at FROM slots WHERE slot_at>? AND state IN ('pending','notified','retry')", (now,)):
            out.add(r["slot_at"] - before)
        return sorted(t for t in out if t > now + 60)

    def stream_reason(self, now: int) -> str | None:
        """Режим стріму (кнопка «🎮 Стрім») або запущена програма для стріму / гра."""
        until = int(self.db.get("stream_until") or 0)
        if until > now:
            return f"🎮 режим стріму до {fmt_slot(self.cfg, until)}"
        from app.system import power as pw
        apps = {a.lower() for a in (self.cfg.get("power") or {}).get("no_sleep_apps") or []}
        found = sorted(apps & pw.running_processes())
        return f"🎮 запущено {', '.join(found)}" if found else None

    def busy_reason(self, now: int) -> str | None:
        """Чому ПК не можна приспати (None — можна)."""
        power = self.cfg.get("power") or {}
        stream = self.stream_reason(now)
        if stream:
            return stream
        before = int(power.get("wake_before_minutes", 5)) * 60
        if self.db.items("status IN ('notified','publishing')"):
            return "шортс чекає на підтвердження або публікується"
        if self.app.publisher.active:
            return "триває публікація"
        soon = self.db.one("SELECT slot_at FROM slots WHERE slot_at>? AND slot_at<=? AND state IN ('pending','notified','retry')",
                           (now - 60, now + before + 600))
        if soon:
            return "скоро слот"
        h, m = map(int, self.cfg["reports"]["daily_time"].split(":"))
        dt = local_dt(self.cfg, now)
        rep = int(dt.replace(hour=h, minute=m, second=0, microsecond=0).timestamp())
        if rep - 600 <= now and self.db.get("daily_report_day") != dt.strftime("%Y-%m-%d"):
            return "скоро щоденний звіт"
        for t in (self._stats_thread, self._health_thread, self._ai_thread):
            if t and t.is_alive():
                return "збирається статистика / перевірка"
        if self.db.q("SELECT key FROM kv WHERE key LIKE 'dialog:%' AND value IS NOT NULL"):
            return "відкрито діалог у Telegram"
        if self.db.q("SELECT key FROM kv WHERE key LIKE 'pcmd:%'"):
            return "виконується дія з панелі"
        last = int(self.db.get("last_activity") or 0)
        quiet = int(power.get("quiet_minutes", 5)) * 60
        if now - last < quiet:
            return f"чекаю {int(power.get('quiet_minutes', 5))} хв тиші після вашої дії / публікації"
        return None

    def maybe_sleep(self, now: int) -> None:
        power = self.cfg.get("power") or {}
        from app.system import power as pw
        if not power.get("sleep_between_slots") or not pw.WIN:
            return
        times = self.wake_times(now)
        key = ",".join(map(str, times[:20]))
        if key != self._wake_key and time.time() >= getattr(self, "_wake_retry", 0):
            # розклад пробуджень змінився — оновлюємо завдання Windows (після помилки — не частіше ніж раз на 30 хв)
            try:
                pw.set_wake_times(times)
                self._wake_key = key
                self.db.set("power_next_wake", times[0] if times else None)
            except Exception as e:  # noqa: BLE001
                self._wake_retry = time.time() + 1800
                log.error("Не вдалося записати пробудження: %s", e)
                self.db.set("power_state", f"помилка пробудження: {str(e)[:200]}")
                return
        reason = self.busy_reason(now)
        pw.keep_awake(bool(reason))  # зайнятий — Windows не засне сама
        if reason:
            self.db.set("power_state", reason)
            return
        # Приспати самі — лише якщо ПК розбудили ми і відтоді ним ніхто не користувався.
        # Якщо ви за комп'ютером (чи дивитесь відео) — засинає за звичайними налаштуваннями Windows.
        awake = time.time() - self._awake_since
        untouched = pw.idle_seconds() >= awake - 5
        if not untouched or awake < 180:
            self.db.set("power_state", "засне за налаштуваннями Windows" if not untouched else "щойно прокинувся")
            return
        self.db.set("power_state", "засинаю")
        nxt = fmt_slot(self.cfg, times[0]) if times else "—"
        log.info("💤 Засинаю. Наступне пробудження: %s", nxt)
        self.db.set("power_next_wake", times[0] if times else None)
        self.db.event(None, "sleep", {"wake": times[0] if times else None})
        pw.sleep_now()
        self._awake_since = time.time()  # сюди повертаємось після пробудження
        log.info("☀️ Прокинувся")
        self.db.set("power_state", "щойно прокинувся")
