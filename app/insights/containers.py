"""Журнал контейнерів Instagram: окремий запис на кожен контейнер — параметри файлу, завантаження, останній стан,
помилка, час обробки. У log.txt (блоком), у data/ig_containers.jsonl і в боті (/containers)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("ig-container")

KEEP = 1000   # скільки останніх записів зберігати у файлі


def _file(cfg: dict) -> Path:
    return Path(cfg["paths"]["data"]) / "ig_containers.jsonl"


def error_message(body: str | None, status_code: str | None, status: str | None, error: str | None) -> str:
    """Найточніший текст помилки: debug_info від rupload → status контейнера (ERROR) → текст винятку."""
    if body:
        try:
            dbg = (json.loads(body) or {}).get("debug_info") or {}
            if dbg:
                return f"{dbg.get('type', '')}: {dbg.get('message', '')}".strip(": ")
        except (ValueError, AttributeError):
            pass
    if status_code in ("ERROR", "EXPIRED") and status:
        return status
    return (error or "").strip()


def category(rec: dict) -> str:
    """Де проблема: 1 — сам файл, 2 — завантаження, 3 — обробка на стороні Meta, 4 — публікація (media_publish).
    (5 — зависання самого Poster — видно в «🕐 Статус»: «остання ознака життя» публікації давно не оновлювалась.)"""
    out = str(rec.get("outcome") or "")
    err = str(rec.get("error") or "")
    code = rec.get("status_code")
    if out.startswith("опубліковано") or out.startswith("контейнер уже"):
        return ""
    if "media_publish" in err or "media_publish" in out:
        return "4. публікація (media_publish)"
    if code == "ERROR" and any(c in err for c in ("2207026", "2207050", "2207004", "2207003")):
        return "1. файл (Meta відхилила формат/параметри)"
    if any(w in err for w in ("тривалість", "файл не готовий", "ffmpeg", "перевірка файлу")):
        return "1. файл"
    if "контейнер" in out and not rec.get("container"):
        return "створення контейнера (API)"
    http = rec.get("upload_http")
    if http and int(http) >= 400:
        return f"2. завантаження (rupload HTTP {http})" + (" → контейнер не обробився" if code in ("IN_PROGRESS", "PENDING") else "")
    if code in ("ERROR", "EXPIRED", "IN_PROGRESS", "PENDING") or "завис" in out or "обробля" in out:
        return "3. обробка на стороні Meta"
    return ""


def fmt(rec: dict) -> str:
    def v(x, unit=""):
        return "—" if x in (None, "") else f"{x}{unit}"
    up = rec.get("upload")
    return "\n".join([
        "Instagram container:",
        f"ID: {v(rec.get('container'))}" + (f"  (шортс #{rec['item_id']})" if rec.get("item_id") else ""),
        f"Video duration: {v(rec.get('duration'), ' с')}",
        f"File size: {v(rec.get('size_mb'), ' МБ')}" + (f"  ({rec['file']})" if rec.get("file") else ""),
        f"Codec: {v(rec.get('codec'))}",
        f"Resolution: {v(rec.get('resolution'))}",
        f"FPS: {v(rec.get('fps'))}",
        f"Bitrate: {v(rec.get('bitrate_kbps'), ' кбіт/с')}",
        f"Upload completed: {v(up)}",
        f"Upload mode: {v(rec.get('upload_mode'))}",
        f"Last status_code: {v(rec.get('status_code'))}",
        f"Last status: {v(rec.get('status'))}",
        f"Last error_message: {v(rec.get('error'))}",
        f"Processing time: {v(rec.get('processing_s'), ' с')}",
        f"Result: {v(rec.get('outcome'))}",
        f"Problem: {v(category(rec))}",
        f"Audio: {v(rec.get('audio'))}",
        f"Container created: {v(rec.get('container_created'))}",
        f"Trace: {v(', '.join(f'{k}={x}' for k, x in (rec.get('trace') or {}).items()))}",
        "Status checks: " + ("—" if not rec.get("polls") else
                             "; ".join(f"{p[0]} {p[1]}" + (f" ({p[2]})" if len(p) > 2 and p[2] else "")
                                       for p in rec["polls"][-30:])),
    ])


def report(cfg: dict, rec: dict) -> str:
    """Записати підсумок по контейнеру (журнал + файл). Ніколи не ламає публікацію."""
    from datetime import datetime
    rec = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **{k: v for k, v in rec.items() if v is not None}}
    rec.setdefault("category", category(rec))
    text = fmt(rec)
    log.info("%s", text)
    try:
        f = _file(cfg)
        f.parent.mkdir(parents=True, exist_ok=True)
        lines = f.read_text(encoding="utf-8").splitlines() if f.exists() else []
        lines.append(json.dumps(rec, ensure_ascii=False))
        f.write_text("\n".join(lines[-KEEP:]) + "\n", encoding="utf-8")
    except (OSError, KeyError, TypeError) as e:
        log.debug("ig_containers: %s", e)
    return text


def last(cfg: dict, n: int = 5) -> list[dict]:
    try:
        lines = _file(cfg).read_text(encoding="utf-8").splitlines()
    except (OSError, KeyError, TypeError):
        return []
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def wait_text(r: dict, now: int) -> str:
    """Стан очікування контейнера для картки й «🕐 Статус»: ID, скільки чекає, останній статус, наступна перевірка."""
    since = r.get("since")
    mins = max(0, (now - int(now if since is None else since)) // 60)
    left = max(0, (int(r.get("next_at") or 0) - now + 59) // 60)
    how = f"{r['method_name']}, " if r.get("method_name") else ""
    last = r.get("last_status") or "—"
    txt = (f"⏳ Instagram обробляє ({how}контейнер {r.get('container') or '—'}): {mins} хв, статус {last}; "
           + (f"наступна перевірка через {left} хв" if left else "перевіряю зараз"))
    if r.get("delay_told"):
        txt += " · довго — інші шортси в Instagram ідуть далі"
    return txt
