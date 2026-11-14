"""Розділ «🇬🇧 EU-канал» у головному Telegram-боті: черга, слоти й стан англомовного каналу
(channels/en) і керування ним — без перемикання на бота EN-каналу.

Черга й слоти беруться самим EN-каналом (`poster.py --config channels/en/config.json queue`), тож
показується рівно те, що бачить він. Кнопки: оновити, пауза розкладу, стоп публікацій, запуск/зупинка
бота EN-каналу. Шортси перед публікацією, як і раніше, надсилає бот EN-каналу (кнопка «🚀 Опублікувати»).
"""
from __future__ import annotations

import html
import logging
import re
import subprocess

import channels
import control
from db import DB

log = logging.getLogger("eu")
KEY = "en"
LIMIT = 3500          # Telegram: до 4096 символів у повідомленні


def available(cfg: dict) -> bool:
    """Розділ є в головному боті, якщо EN-канал створено (і це не бот самого EN-каналу)."""
    return not cfg.get("channel") and channels.exists(KEY)


def _db() -> DB:
    return DB(channels.db_path(KEY))


def _queue_text(timeout: int = 90) -> str:
    """Черга й слоти EN-каналу — так, як їх бачить сам EN-канал."""
    try:
        r = subprocess.run([control.python_exe(windowless=False), str(channels.ROOT / "poster.py"),
                            "--config", str(channels.config_path(KEY)), "queue"],
                           cwd=str(channels.ROOT), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as e:
        return f"Не вдалося прочитати чергу: {e}"
    out = re.sub(r"^\d{4}-\d\d-\d\d [\d:,]+ \[\w+\] [\w.]+: .*\n?", "", (r.stdout or "") + (r.stderr or ""),
                 flags=re.M).strip()
    return out or "Черга порожня."


def view(queue: bool = True) -> tuple[str, dict]:
    st = channels.status(KEY)
    db = _db()
    paused, stopped = db.get("paused") == "1", db.get("stopped") == "1"
    lines = [f"🇬🇧 <b>{html.escape(st.get('name') or 'EU-канал')}</b>",
             f"Бот каналу: {'🟢 працює' if st.get('running') else '🔴 зупинено'}"
             + ("" if st.get("telegram_ready") else " · ⚠️ немає свого Telegram-бота"),
             f"Розклад: {'⏸ на паузі' if paused else '▶️ активний'} · "
             f"Публікації: {'⛔ зупинено' if stopped else '✅ дозволено'}",
             f"Слоти: {', '.join(st.get('slots') or [])} ({html.escape(str(st.get('timezone') or ''))})",
             f"Платформи: {', '.join(st.get('platforms') or []) or '—'}"]
    if queue:
        q = _queue_text()
        if len(q) > LIMIT:
            q = q[:LIMIT] + "\n…"
        lines += ["", f"<pre>{html.escape(q)}</pre>"]
    lines.append("\nШортси перед публікацією надсилає бот EU-каналу (кнопка «🚀 Опублікувати»).")
    b = lambda text, op: {"text": text, "callback_data": f"eu:0:{op}"}  # noqa: E731
    kb = {"inline_keyboard": [
        [b("🔄 Оновити", "refresh"), b("▶️ Продовжити розклад" if paused else "⏸ Пауза розкладу",
                                       "resume" if paused else "pause")],
        [b("✅ Відновити публікації" if stopped else "⛔ Стоп публікацій", "go" if stopped else "stop")],
        [b("⏹ Зупинити бота EU" if st.get("running") else "▶️ Запустити бота EU",
           "botstop" if st.get("running") else "botstart")],
    ]}
    return "\n".join(lines), kb


def action(op: str) -> str:
    db = _db()
    if op == "pause":
        db.set("paused", "1")
        return "⏸ Розклад EU-каналу на паузі"
    if op == "resume":
        db.set("paused", None)
        return "▶️ Розклад EU-каналу відновлено"
    if op == "stop":
        db.set("stopped", "1")
        db.event(None, "stopped", {"by": "main-bot"})
        return "⛔ Публікації EU-каналу зупинено"
    if op == "go":
        db.set("stopped", None)
        db.event(None, "resumed_publishing", {"by": "main-bot"})
        return "✅ Публікації EU-каналу відновлено"
    if op == "botstart":
        try:
            ok = channels.start(KEY)
        except ValueError as e:
            return str(e)[:190]
        return "▶️ Бот EU-каналу запущено" if ok else "Бот EU-каналу не запустився — подивіться його журнал"
    if op == "botstop":
        return "⏹ Бот EU-каналу зупинено" if channels.stop(KEY) else "Бот EU-каналу не зупинився"
    return ""


__all__ = ["action", "available", "view"]
