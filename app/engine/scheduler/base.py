"""Основа планувальника: такт роботи (tick) і дрібні допоміжні методи."""
from __future__ import annotations

import threading
import time
from app.core.config import now_ts
from app.core.meta import load_meta
from pathlib import Path
from .common import (
    log,
)


class BaseMixin:
    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.db = app.db
        self._last_scan = 0
        self._stats_thread: threading.Thread | None = None
        self._health_thread: threading.Thread | None = None
        self._ai_thread: threading.Thread | None = None
        self._health_started = False
        self._awake_since = time.time()
        self._wake_key = ""
        self._last_heal = 0.0
        self._upd_thread: threading.Thread | None = None

    def tick(self, now: int | None = None) -> None:
        now = now or now_ts()
        if hasattr(self.app, "reload_config"):
            self.app.reload_config()  # зміни з панелі — без перезапуску
        if now - self._last_scan >= int(self.cfg["scan_interval_seconds"]):
            self._last_scan = now
            self.scan(now)
        self.ensure_slots(now)
        stopped = self.db.get("stopped") == "1"  # «⛔ Стоп публікацій»: ні публікацій, ні нових сповіщень
        if not stopped:
            self.resume_publishing()
            if hasattr(self.app.bot, "resume_bulk"):
                self.app.bot.resume_bulk()
        pending_refresh = self.db.get("refresh_item")
        if pending_refresh:
            self.db.set("refresh_item", None)
            self.app.bot.refresh(int(pending_refresh))
        if not stopped:
            self.process_due_slots(now)
            self.process_notifications(now)
        self.maybe_daily_report(now)
        self.maybe_stats(now)
        self.maybe_health_check(now)
        self.maybe_ig_watch(now)
        self.maybe_ai()
        self.maybe_update(now)
        if time.time() - self._last_heal >= 60:  # раз на хвилину: підписи відео, що «застрягли» без зв'язку
            self._last_heal = time.time()
            try:
                self.app.bot.heal()
            except Exception as e:  # noqa: BLE001
                log.debug("heal: %s", e)
        self.maybe_sleep(now)
        try:
            self.app.bot.purge_expired()
        except Exception as e:  # noqa: BLE001
            log.debug("purge: %s", e)

    def channel_key(self) -> str:
        """Ключ цього каналу: "" — основний, "en" — EN/EU-канал тощо."""
        return str((self.cfg.get("channel") or {}).get("key") or "")

    def _why(self, mp4: Path, reason: str) -> None:
        """Чому відео з папки не стало в чергу — у журнал один раз на файл (видно в «📄 Журнал»)."""
        seen = self.__dict__.setdefault("_why_seen", {})
        if seen.get(str(mp4)) != reason:
            seen[str(mp4)] = reason
            log.warning("Не беру в чергу %s: %s", Path(mp4).name, reason)

    def _home(self, item: dict, ready: Path) -> Path:
        """Папка, з якої відео має бути в черзі: шортси — папка шортсів, хайлайти — папка хайлайтів."""
        if item.get("kind") == "highlight":
            return Path(self.cfg["highlights"]["folder_path"]).resolve()
        return ready

    def _next_item(self, kind: str = "short") -> dict | None:
        for item in self.db.queue(kind):
            if Path(item["mp4"]).exists() and Path(item["json"]).exists():
                return item
            log.warning("#%s: файл зник (%s) — прибираю з черги", item["id"], item["mp4"])
            self.db.update_item(item["id"], status="missing")
            self.db.event(item["id"], "missing", {"file": Path(item["mp4"]).name})
        return None

    def _title(self, item: dict) -> str:
        try:
            return load_meta(item["json"], item["title_override"]).yt_title
        except Exception:  # noqa: BLE001
            return Path(item["mp4"]).stem
