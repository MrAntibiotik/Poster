"""Довідкові тексти: журнал, деталі помилок, стрім, платформи, оновлення."""
from __future__ import annotations

import json
import re
from app.core.config import NAMES
from app.core.config import PLATFORMS
from app.core.config import fmt_slot
from app.core.config import local_dt
from app.core.config import now_ts
from pathlib import Path
from .common import (
    esc,
)


class InfoMixin:
    def log_text(self, flt: str = "", only_errors: bool = True, mode: str = "err") -> tuple[str, dict]:
        """Журнал у Telegram: останні рядки (за замовчуванням — лише помилки й попередження), без секретів."""
        path = Path(self.cfg["paths"]["log"])
        lines = []
        try:
            with open(path, "rb") as f:
                f.seek(max(0, path.stat().st_size - 300_000))
                lines = f.read().decode("utf-8", errors="replace").splitlines()[1:]
        except OSError:
            pass
        flt = (flt or "").strip().lower()
        out = []
        for line in lines:
            m = re.match(r"\d{4}-(\d\d)-(\d\d) (\d\d:\d\d):\d\d,\d+ \[(\w+)\] [\w.]+: (.*)", line)
            if not m:
                continue
            level, msg = m.group(4), m.group(5)
            if only_errors and level not in ("WARNING", "ERROR", "CRITICAL"):
                continue
            if flt and flt not in msg.lower():
                continue
            for v in self.cfg["secrets"].values():
                if v and len(v) > 12:
                    msg = msg.replace(v, v[:4] + "…")
            icon = {"WARNING": "⚠️", "ERROR": "❌", "CRITICAL": "🛑"}.get(level, "•")
            out.append(f"{icon} <b>{m.group(2)}.{m.group(1)} {m.group(3)}</b> {esc(msg[:350])}")
        title = {"err": "помилки й попередження", "all": "усі події", "ig": "Instagram", "yt": "YouTube",
                 "fb": "Facebook", "tt": "TikTok"}.get(mode, f"«{flt}»")
        head = f"📄 <b>Журнал: {title}</b>"
        body, size = [], len(head)
        for line in reversed(out):  # найновіші, скільки вміщається в одне повідомлення
            if size + len(line) + 2 > 3800:
                break
            body.insert(0, line)
            size += len(line) + 2
        text = head + ("\n\n" + "\n\n".join(body) if body else "\n\nНічого не знайдено — і це добре 🙂")
        kb = {"inline_keyboard": [
            [{"text": "⚠️ Помилки", "callback_data": "log:0:err"}, {"text": "📜 Усе", "callback_data": "log:0:all"}],
            [{"text": "📸 IG", "callback_data": "log:0:ig"}, {"text": "▶️ YT", "callback_data": "log:0:yt"},
             {"text": "📘 FB", "callback_data": "log:0:fb"}, {"text": "🎵 TT", "callback_data": "log:0:tt"}]]}
        return text, kb

    def details_text(self, item_id: int) -> str:
        """Повні причини помилок і що пробувала програма — для «🧾 Деталі» (Telegram і панель)."""
        from app.platforms.meta_graph import short_error
        item = self.db.get_item(item_id)
        if not item:
            return "Шортс не знайдено"
        lines = [f"🧾 <b>Деталі публікації #{item_id}</b>", esc(Path(item["mp4"]).name)]
        for p in PLATFORMS:
            r = item["results"].get(p)
            if not r:
                continue
            if r.get("status") == "ok":
                lines.append(f"\n✅ <b>{NAMES[p]}</b>" + (f" — {esc(str(r.get('note')))}" if r.get("note") else ""))
                for t in r.get("tried") or []:
                    lines.append(f"   ✖️ не пройшло: {esc(str(t)[:400])}")
            else:
                lines.append(f"\n❌ <b>{NAMES[p]}</b> (спроб: {r.get('attempts', '?')})")
                err = str(r.get("error", ""))
                for part in err.split("; ") if "спроб)" in err else [err]:
                    lines.append(f"   • {esc(short_error(part) if len(part) > 700 else part)}")
                if p == "instagram" and "Етапи:" not in err:
                    diag = self._last_ig_diag(item)
                    if diag:
                        lines.append(f"   🔬 Етапи ({esc(diag.get('started', ''))}): {esc(diag.get('summary', ''))}")
        if self.app.publisher.is_active(item_id):   # публікація йде зараз — етапи наживо
            cur = self.db.get(f"pubnow:{item_id}")
            lines.append(f"\n⏳ <b>Зараз: {NAMES.get(cur, cur or '—')}</b> — {self._phase_text(item_id)}")
            plat = self.app.platforms.get(cur) if cur else None
            d = getattr(plat, "diag", None) or {}
            if d.get("file") and cur == "instagram":
                lines.append(f"   файл: {esc(d['file'])} ({d.get('size_mb')} МБ)")
                for s in d.get("stages") or []:
                    lines.append(f"   {'✅' if s['ok'] else '❌'} {esc(s['stage'])} · {s['seconds']:g} с"
                                 + (f" — {esc(str(s['info'])[:200])}" if s.get("info") else ""))
                polls = d.get("polls") or []
                if polls:
                    last = polls[-1]
                    lines.append(f"   🔄 перевірок обробки: {len(polls)}, остання (через {last[0]} с): "
                                 f"{esc(str(last[1]))} {esc(str(last[2])[:100])}")
        evs = self.db.q("SELECT ts, details FROM events WHERE item_id=? AND kind='platform_error' ORDER BY ts DESC LIMIT 6",
                        (item_id,))
        if evs:
            lines.append("\n<b>Останні помилки (журнал)</b>")
            for e in evs:
                try:
                    d = json.loads(e["details"] or "{}")
                except ValueError:
                    d = {}
                lines.append(f"• {local_dt(self.cfg, e['ts']):%d.%m %H:%M} {NAMES.get(d.get('platform'), d.get('platform', ''))}: "
                             f"{esc(short_error(d.get('error', '')))}")
        text = "\n".join(lines)
        return text if len(text) < 4000 else text[:3990] + "…"

    def _last_ig_diag(self, item: dict) -> dict | None:
        """Останній звіт діагностики Instagram (data/ig_diag) для цього шортса."""
        folder = Path(self.cfg["paths"]["data"]) / "ig_diag"
        stem = Path(item["mp4"]).stem.lower()
        try:
            files = sorted(folder.glob("*.json"), reverse=True)
        except OSError:
            return None
        for f in files[:50]:
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if str(d.get("file", "")).lower().startswith(stem) and d.get("summary"):
                return d
        return None

    def has_details(self, item: dict) -> bool:
        return any(r.get("status") not in ("ok", "manual", "skipped") or r.get("tried") for r in item["results"].values())

    def update_now(self, force: bool = False) -> tuple[str, dict | None]:
        """Оновлення одразу, без очікування. Якщо саме йде публікація — питаємо (якщо не force)."""
        from app.system import updater
        info = updater.check(self.cfg)
        self.db.set("update_remote", info["remote"])
        self.db.set("update_error", None)
        hist = {"inline_keyboard": [[{"text": "🕘 Версії / відкат", "callback_data": "ver:-1"}]]}
        if not info["newer"]:
            return f"✅ У вас остання версія ({info['local']}).", hist
        busy = sorted(self.app.publisher.active) or [i["id"] for i in self.db.items("status='publishing'")]
        if busy and not force:
            return (f"⬆️ Є версія {info['remote']} (у вас {info['local']}), але зараз публікується "
                    f"#{', #'.join(map(str, busy))}.\nЯкщо оновитись зараз, ця публікація продовжиться після перезапуску "
                    "(туди, куди вже вийшло, повторно не піде).",
                    {"inline_keyboard": [[{"text": "⚡ Оновити зараз", "callback_data": "upd:1"},
                                          {"text": "⏳ Після публікації", "callback_data": "upd:0"}]]})
        new = updater.apply(self.cfg)
        updater.set_auto_paused(self.cfg, None)   # ручне оновлення знімає паузу після відкату
        self.db.event(None, "updated", {"version": new, "manual": True})
        self.db.set("control", "restart")  # головний цикл перезапустить бота за кілька секунд
        return f"⬆️ Оновлено до {new} — перезапускаюсь (≈10 с).", None

    def stream_text(self) -> tuple[str, dict]:
        now = now_ts()
        until = int(self.db.get("stream_until") or 0)
        sch = self.app.scheduler
        auto = sch.stream_reason(now) if until <= now else None
        power = self.cfg.get("power") or {}
        lines = ["🎮 <b>Режим стріму</b>"]
        if until > now:
            lines.append(f"Увімкнено до <b>{fmt_slot(self.cfg, until)}</b> — ПК не засне.")
        elif auto:
            lines.append(f"Зараз {esc(auto[2:])} — ПК не засне, поки програма працює.")
        else:
            lines.append("Вимкнено. ПК не засинає й тоді, коли запущено OBS / Streamlabs / гру.")
        lines.append("Публікації під час стріму: " + ("⏸ чекають кінця стріму" if power.get("hold_during_stream")
                                                     else "▶️ йдуть за розкладом") + " (змінити — у панелі).")
        rows = [[{"text": f"{h} год", "callback_data": f"stm:{h}"} for h in (2, 4, 6, 10)]]
        if until > now:
            rows.append([{"text": "⏹ Вимкнути режим стріму", "callback_data": "stm:0"}])
        return "\n".join(lines), {"inline_keyboard": rows}

    def platforms_text(self) -> tuple[str, dict]:
        lines = ["🔌 <b>Платформи</b> — куди публікувати всі шортси",
                 "Натисніть, щоб вимкнути або увімкнути. Діє одразу, без перезапуску."]
        rows = []
        for p in PLATFORMS:
            on = bool(self.cfg["platforms"].get(p, {}).get("enabled"))
            lines.append(f"{'✅' if on else '🚫'} {NAMES[p]}")
            rows.append([{"text": f"{'✅' if on else '🚫'} {NAMES[p]} — {'вимкнути' if on else 'увімкнути'}",
                          "callback_data": f"plat:0:{p}"}])
        lines.append("\nДля окремого шортса — кнопки «Вимкнути платформу» під ним.")
        return "\n".join(lines), {"inline_keyboard": rows}
