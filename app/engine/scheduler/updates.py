"""Автооновлення з GitHub у вільний момент."""
from __future__ import annotations

import threading
from .common import (
    log,
)


class UpdatesMixin:
    def maybe_update(self, now: int) -> None:
        up = self.cfg.get("updates") or {}
        if getattr(self.app, "no_update", False):
            return
        if self.db.get("control") == "update":  # «Оновити зараз» з панелі
            self.db.set("control", None)
            self.db.set("update_available", "manual")
        last = int(self.db.get("update_check_ts") or 0)
        every = max(60.0, float(up.get("check_minutes") or 60)) * 60   # не частіше ніж раз на годину
        if up.get("auto") and now - last >= every and not (self._upd_thread and self._upd_thread.is_alive()):
            self.db.set("update_check_ts", now)

            def work():
                from app.system import updater
                try:
                    info = updater.check(self.cfg)
                    self.db.set("update_remote", info["remote"])
                    self.db.set("update_error", None)
                    if info["newer"]:
                        log.info("Є оновлення: %s → %s", info["local"], info["remote"])
                        self.db.set("update_available", info["remote"])
                except Exception as e:  # noqa: BLE001
                    if self.db.get("update_error") != str(e)[:300]:  # раз на 5 хв — не засмічуємо журнал
                        log.warning("Перевірка оновлень: %s", e)
                    self.db.set("update_error", str(e)[:300])
            self._upd_thread = threading.Thread(target=work, daemon=True, name="update-check")
            self._upd_thread.start()
        if self.db.get("update_available") and self.update_idle(now):
            self.install_update()

    def update_idle(self, now: int) -> bool:
        """Оновлюємось лише коли нічого не публікується, не відкрито діалог і ви 5 хв не клацали в боті."""
        if self.app.publisher.active or self.db.items("status='publishing'"):
            return False
        if self.db.q("SELECT key FROM kv WHERE key LIKE 'dialog:%' AND value IS NOT NULL"):
            return False
        if now - int(self.db.get("last_activity") or 0) < 300 and self.db.get("update_available") != "manual":
            return False
        soon = self.db.one("SELECT slot_at FROM slots WHERE slot_at>? AND slot_at<=? AND state IN ('notified','retry')",
                           (now, now + 180))
        return not soon

    def install_update(self) -> None:
        from app.system import updater
        manual = self.db.get("update_available") == "manual"
        self.db.set("update_available", None)
        try:
            info = updater.check(self.cfg)
            self.db.set("update_remote", info["remote"])
            if not info["newer"]:
                if manual:
                    self.app.bot.send(f"✅ У вас остання версія ({info['local']}).")
                return
            new = updater.apply(self.cfg)
            if manual:
                updater.set_auto_paused(self.cfg, None)
        except Exception as e:  # noqa: BLE001
            log.error("Оновлення не вдалося: %s", e)
            self.db.set("update_error", str(e)[:300])
            self.app.bot.send(f"⚠️ Автооновлення не вдалося: {e}")
            return
        self.db.set("update_error", None)
        self.db.event(None, "updated", {"version": new})
        self.app.bot.send(f"⬆️ poster оновлено до версії {new} — перезапускаюсь (≈10 с).")
        self.db.set("control", "restart")
