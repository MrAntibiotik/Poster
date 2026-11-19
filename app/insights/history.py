"""Історія публікацій: по запису на кожну спробу кожного відео на кожній платформі — тривалість, розмір, частота
кадрів, бітрейт файлу, спосіб, відповідь завантаження, час обробки, результат і помилка. Для пошуку закономірностей
(що проходить, що ні). Вивантаження — CSV (/history у боті, панель → Журнал)."""
from __future__ import annotations

import csv
import io
import json
import logging
from pathlib import Path

from app.core.config import NAMES, VERSION, local_dt, now_ts

log = logging.getLogger("history")

COLUMNS = ("id", "ts", "item_id", "platform", "result", "file", "title", "duration", "width", "height", "fps",
           "vkbps", "size_mb", "sent_file", "sent_mb", "method", "upload_http", "upload_s", "processing_s",
           "total_s", "url", "error", "version", "stage", "upload_body", "container_id", "container_status",
           "container_status_text")

STAGES = {"файл": "файл", "контейнер": "створення контейнера", "rupload": "rupload (завантаження)",
          "обробка": "обробка в Instagram", "публікація": "media_publish", "media_publish": "media_publish"}

RESULTS = {   # що означає result
    "ok": "опубліковано",
    "ok_background": "опубліковано після довгої обробки",
    "processing": "завантажено, Instagram обробляє (далі — у фоні)",
    "manual_long": "довше за межу — надіслано в Telegram без спроби",
    "manual_rejected": "Instagram не прийняв — надіслано в Telegram",
    "manual": "ручний режим — надіслано в Telegram",
    "manual_done": "опубліковано вручну з телефона",
    "skipped": "пропущено вручну",
    "stuck": "контейнер завис",
    "next_method": "спосіб не пройшов — пробую наступний",
    "error": "помилка",
    "limited": "ліміт запитів платформи",
    "auth": "потрібна авторизація",
}


def file_info(app, item: dict) -> dict:
    """Параметри оригіналу (кешуються в kv: файл не змінюється)."""
    key = f"probe:{item['id']}"
    try:
        cached = json.loads(app.db.get(key) or "null")
    except ValueError:
        cached = None
    if cached:
        return cached
    try:
        from app.core import media
        p = media.ig_probe(item["mp4"], app.cfg.get("ffprobe", "ffprobe"))
    except Exception:  # noqa: BLE001
        p = {}
    if not p:
        return {}
    info = {"duration": round(p.get("duration") or 0, 1), "width": p.get("width"), "height": p.get("height"),
            "fps": round(p.get("fps") or 0, 2), "vkbps": p.get("vkbps"), "size_mb": round(p.get("size_mb") or 0, 1)}
    app.db.set(key, json.dumps(info))
    return info


def _title(item: dict) -> str:
    try:
        from app.core.meta import load_meta
        return load_meta(item["json"], item.get("title_override")).yt_title[:200]
    except Exception:  # noqa: BLE001
        return Path(item["mp4"]).stem


def ig_details(plat) -> dict:
    """Що відбувалося в Instagram (з діагностики останньої спроби): файл, спосіб, HTTP і повна відповідь
    завантаження (debug_info), стан контейнера (status_code / status), на якому етапі зламалось."""
    d = getattr(plat, "diag", None) or {}
    out = {}
    if d.get("file"):
        out["sent_file"] = d["file"]
        out["sent_mb"] = d.get("size_mb")
    up = d.get("rupload") or {}
    for st in d.get("stages") or []:
        if st.get("stage") == "rupload":
            out["upload_s"] = st.get("seconds")
            out["upload_http"] = up.get("http") or (200 if st.get("ok") else None)
        if st.get("stage") == "обробка":
            out["processing_s"] = st.get("seconds")
        if not st.get("ok") and "stage" not in out:
            out["stage"] = STAGES.get(st.get("stage"), st.get("stage"))
    if up.get("body"):
        out["upload_body"] = up["body"]
    if d.get("container"):
        out["container_id"] = d["container"]
    polls = d.get("polls") or []
    if polls:
        out["container_status"] = polls[-1][1]
        out["container_status_text"] = polls[-1][2]
    if d.get("variant"):
        out["method"] = d["variant"]
    return out


def container_details(plat) -> dict:
    """Стан контейнера з останньої фонової перевірки (finish_container)."""
    c = dict(getattr(plat, "last_container", None) or {})
    if c.get("stage"):
        c["stage"] = STAGES.get(c["stage"], c["stage"])
    else:
        c.pop("stage", None)
    return c


def record(app, item: dict, platform: str, result: str, started: float | None = None, **fields) -> None:
    """Записати спробу. Ніколи не ламає публікацію."""
    try:
        row = {"ts": now_ts(), "item_id": item["id"], "platform": platform, "result": result,
               "file": Path(item["mp4"]).name, "title": _title(item), "version": VERSION,
               **file_info(app, item)}
        if started:
            row["total_s"] = round(now_ts() - started, 1)
        for k, v in fields.items():
            if v is not None and k in COLUMNS:
                row[k] = str(v)[:4000] if k in ("error", "upload_body", "container_status_text") else v
        cols = [c for c in COLUMNS if c in row and c != "id"]
        app.db.x(f"INSERT INTO history({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 tuple(row[c] for c in cols))
    except Exception as e:  # noqa: BLE001
        log.debug("history: %s", e)


def backfill(app) -> int:
    """Один раз: відео, опубліковані до появи історії — з результатів у базі (без деталей завантаження)."""
    db = app.db
    if db.get("history_backfilled"):
        return 0
    n = 0
    for it in db.items("status IN ('posted','failed','publishing')", order="id"):
        for p, r in (it["results"] or {}).items():
            if db.one("SELECT 1 FROM history WHERE item_id=? AND platform=?", (it["id"], p)):
                continue
            st = r.get("status")
            res = {"ok": "ok", "manual": "manual", "skipped": "skipped", "processing": "processing"}.get(st, "error")
            if st == "ok" and "вручну" in str(r.get("note") or ""):
                res = "manual_done"
            row = {"ts": int(r.get("published_at") or r.get("sent_at") or it.get("posted_at") or it["added_at"]),
                   "item_id": it["id"], "platform": p, "result": res, "file": Path(it["mp4"]).name,
                   "title": _title(it), "url": r.get("url") or None, "error": (r.get("error") or None),
                   "version": "до історії"}
            if Path(it["mp4"]).exists():
                row.update(file_info(app, it))
            cols = [c for c in COLUMNS if c in row and row[c] is not None and c != "id"]
            db.x(f"INSERT INTO history({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 tuple(row[c] for c in cols))
            n += 1
    db.set("history_backfilled", 1)
    if n:
        log.info("Історія публікацій: додано %s записів про попередні відео", n)
    return n


def to_csv(app) -> bytes:
    """Уся історія — CSV (Excel відкриває з українськими літерами: UTF-8 з BOM, роздільник «;»)."""
    rows = app.db.q("SELECT * FROM history ORDER BY ts, id")
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["№", "дата", "шортс", "платформа", "результат", "пояснення", "файл", "назва", "тривалість, с",
                "ширина", "висота", "к/с", "відео, кбіт/с", "розмір, МБ", "надіслано файл", "надіслано, МБ",
                "спосіб", "HTTP завантаження", "завантаження, с", "обробка, с", "усього, с", "посилання", "помилка",
                "версія", "етап, де зламалось", "відповідь завантаження (debug_info)", "контейнер",
                "status_code контейнера", "status контейнера"])
    for r in rows:
        w.writerow([r["id"], local_dt(app.cfg, r["ts"]).strftime("%Y-%m-%d %H:%M"), r["item_id"],
                    NAMES.get(r["platform"], r["platform"]), r["result"], RESULTS.get(r["result"], ""),
                    r["file"], r["title"], r["duration"], r["width"], r["height"], r["fps"], r["vkbps"],
                    r["size_mb"], r["sent_file"], r["sent_mb"], r["method"], r["upload_http"], r["upload_s"],
                    r["processing_s"], r["total_s"], r["url"], r["error"], r["version"], r.get("stage"),
                    r.get("upload_body"), r.get("container_id"), r.get("container_status"),
                    r.get("container_status_text")])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def summary(app) -> str:
    """Короткий підсумок для Telegram: по платформах і (Instagram) за тривалістю та відповіддю завантаження."""
    from html import escape as esc
    db = app.db
    lines = ["🗂 <b>Історія публікацій</b>"]
    total = db.one("SELECT COUNT(*) AS n, COUNT(DISTINCT item_id) AS v FROM history")
    lines.append(f"Записів: {total['n']}, відео: {total['v']}")
    for p in ("youtube", "instagram", "facebook", "tiktok"):
        rows = db.q("SELECT result, COUNT(*) AS n FROM history WHERE platform=? GROUP BY result ORDER BY n DESC", (p,))
        if rows:
            lines.append(f"\n<b>{esc(NAMES.get(p, p))}</b>: " + ", ".join(f"{esc(r['result'])} {r['n']}" for r in rows))
    ok = ("ok", "ok_background")
    buckets = ((0, 30.5, "≤30 с"), (30.5, 45.5, "31–45 с"), (45.5, 55.5, "46–55 с"), (55.5, 999, "понад 55 с"))
    ig = db.q("SELECT duration, result, upload_http FROM history WHERE platform='instagram' AND duration>0 "
              "AND result NOT IN ('processing','next_method')")
    if ig:
        lines.append("\n📸 <b>Instagram за тривалістю</b> (через API):")
        for lo, hi, name in buckets:
            part = [r for r in ig if lo < (r["duration"] or 0) <= hi and r["result"] not in ("manual", "manual_long")]
            if part:
                good = sum(1 for r in part if r["result"] in ok)
                lines.append(f"• {name}: {good} з {len(part)} пройшло")
        codes = {}
        for r in ig:
            if r["upload_http"]:
                c = codes.setdefault(r["upload_http"], [0, 0])
                c[0] += 1
                c[1] += r["result"] in ok
        if codes:
            lines.append("📤 Відповідь завантаження → опубліковано: "
                         + ", ".join(f"HTTP {k}: {v[1]} з {v[0]}" for k, v in sorted(codes.items())))
    stages = db.q("SELECT stage, COUNT(*) AS n FROM history WHERE platform='instagram' AND stage IS NOT NULL "
                  "AND stage<>'' GROUP BY stage ORDER BY n DESC")
    if stages:
        lines.append("🧩 Де зламалось: " + ", ".join(f"{esc(r['stage'])} — {r['n']}" for r in stages))
    st = db.q("SELECT container_status, COUNT(*) AS n FROM history WHERE platform='instagram' "
              "AND container_status IS NOT NULL GROUP BY container_status ORDER BY n DESC")
    if st:
        lines.append("📦 Стан контейнера: " + ", ".join(f"{esc(str(r['container_status']))} — {r['n']}" for r in st))
    lines.append("\nПовна таблиця — файлом нижче (відкривається в Excel).")
    return "\n".join(lines)
