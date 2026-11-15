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
from pathlib import Path

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


def _pretty(item: dict) -> str:
    """Зрозуміла назва шортса: назва з json, без хештегів; інакше — ім'я файлу без технічних частин."""
    from reports import _clean_title
    t = _clean_title({}, item)
    stem = Path(item["mp4"]).stem
    if t and t != stem:
        return t
    m = re.search(r"replay_wr_\d+_(.+?)_\d{4}_\d\d_\d\d_\d\d_\d\d_\d\d_battle_\d+_dmg_(\d+)", stem)
    if m:
        name = m.group(1).replace("_s_", "'s ").replace("_", " ")
        return f"{name} · {int(m.group(2)):,} шкоди".replace(",", " ")
    return stem.replace("_", " ")


def _when(ts: int, ch_tz, home_tz) -> str:
    """«сьогодні 18:00 (11:00 NY)» — київський час, а в дужках — час каналу."""
    from datetime import datetime
    here = datetime.fromtimestamp(ts, home_tz)
    there = datetime.fromtimestamp(ts, ch_tz)
    today = datetime.now(home_tz).date()
    day = {0: "сьогодні", 1: "завтра"}.get((here.date() - today).days, here.strftime("%d.%m"))
    short = {"America/New_York": "NY"}.get(str(ch_tz), str(ch_tz).split("/")[-1])
    return f"{day} {here:%H:%M}" + ("" if here.utcoffset() == there.utcoffset() else f" ({there:%H:%M} {short})")


def _plan(limit: int = 10):
    """Черга EN-каналу з часом публікації — прямо з його бази (без запуску окремого процесу)."""
    import types
    from config import load_config
    from scheduler import Scheduler
    cfg = load_config(channels.config_path(KEY))
    db = _db()
    sch = Scheduler(types.SimpleNamespace(cfg=cfg, db=db, app=None, platforms={}))
    work = db.items("status IN ('notified','approved','publishing')", order="slot_at")
    return cfg, work, sch.projection()[:limit], len(db.queue())


def view(queue: bool = True, home_tz=None) -> tuple[str, dict]:
    from zoneinfo import ZoneInfo
    st = channels.status(KEY)
    db = _db()
    home_tz = home_tz or ZoneInfo("Europe/Kyiv")
    paused, stopped = db.get("paused") == "1", db.get("stopped") == "1"
    head = f"🇬🇧 <b>{html.escape(st.get('name') or 'EU-канал')}</b> · " + ("🟢 працює" if st.get("running") else "🔴 бот зупинено")
    lines = [head]
    if paused:
        lines.append("⏸ Розклад на паузі")
    if stopped:
        lines.append("⛔ Публікації зупинено")
    if not st.get("telegram_ready"):
        lines.append("⚠️ Немає свого Telegram-бота — панель EN-каналу → «✈️ Telegram»")
    if "youtube" in (st.get("platforms") or []) and not st.get("youtube_authorized"):
        lines.append("🔑 YouTube не підключено — панель EN-каналу → YouTube → «Авторизувати»")
    if queue:
        try:
            cfg, work, plan, total = _plan()
            ch_tz = cfg["tz"]
            if work:
                lines.append("\n🎬 <b>У роботі</b>")
                st_ua = {"notified": "⏳ чекає підтвердження", "approved": "👍 підтверджено", "publishing": "⏫ публікується"}
                for it in work:
                    when = _when(it["slot_at"], ch_tz, home_tz) if it["slot_at"] else "—"
                    lines.append(f"• {html.escape(_pretty(it), quote=False)}\n   {st_ua.get(it['status'], it['status'])} · {when}")
            lines.append(f"\n📦 <b>Черга: {total}</b>" + ("" if total else " — порожня, Різак ще нічого не передав"))
            for n, (it, ts) in enumerate(plan, 1):
                lines.append(f"{n}. {html.escape(_pretty(it), quote=False)}\n   → {_when(ts, ch_tz, home_tz) if ts else 'час ще не визначено'}")
            if total > len(plan):
                lines.append(f"… і ще {total - len(plan)}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"\nНе вдалося прочитати чергу: {html.escape(str(e)[:200])}")
    lines.append("\n<i>На підтвердження шортси приходять у бот EU-каналу.</i>")
    text = "\n".join(lines)
    if len(text) > LIMIT:
        text = text[:LIMIT] + "\n…"
    b = lambda text, op: {"text": text, "callback_data": f"eu:0:{op}"}  # noqa: E731
    kb = {"inline_keyboard": [
        [b("🔄 Оновити", "refresh"), b("▶️ Продовжити розклад" if paused else "⏸ Пауза розкладу",
                                       "resume" if paused else "pause")],
        [b("✅ Відновити публікації" if stopped else "⛔ Стоп публікацій", "go" if stopped else "stop")],
        [b("⏹ Зупинити бота EU" if st.get("running") else "▶️ Запустити бота EU",
           "botstop" if st.get("running") else "botstart")],
    ]}
    return text, kb


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
