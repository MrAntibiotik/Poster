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
        f"Last status_code: {v(rec.get('status_code'))}",
        f"Last status: {v(rec.get('status'))}",
        f"Last error_message: {v(rec.get('error'))}",
        f"Processing time: {v(rec.get('processing_s'), ' с')}",
        f"Result: {v(rec.get('outcome'))}",
    ])


def report(cfg: dict, rec: dict) -> str:
    """Записати підсумок по контейнеру (журнал + файл). Ніколи не ламає публікацію."""
    from datetime import datetime
    rec = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **{k: v for k, v in rec.items() if v is not None}}
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
