"""Зв'язок з програмою нарізки («Різак»): її папки й перенесення відео.

Різак записує %LOCALAPPDATA%\\MrAntibiotik\\cutter.json:
  {"folders": {"highlights": …, "publish": …, …}, "moved": [[було, стало], …]}
• папка хайлайтів за замовчуванням (highlights.folder порожнє) — звідти;
• коли Різак переніс відео (напр. у Робочий стіл\\Контент\\Мій канал\\…), Poster сам переводить на нові шляхи
  свої налаштування (папка шортсів, папка хайлайтів) і відео в черзі — нічого не губиться й не публікується двічі.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("cutter_link")


def discovery_file() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".config")
    return Path(base) / "MrAntibiotik" / "cutter.json"


def load() -> dict | None:
    try:
        return json.loads(discovery_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def highlights_folder() -> Path | None:
    p = ((load() or {}).get("folders") or {}).get("highlights")
    return Path(p) if p else None


def _same_or_inside(p: str, old: str) -> str | None:
    """Залишок шляху p після old (з роздільником) або None, якщо p не в old."""
    a, b = p.replace("/", "\\"), old.replace("/", "\\").rstrip("\\")
    if os.name == "nt":
        al, bl = a.lower(), b.lower()
    else:
        al, bl = a, b
    if al == bl:
        return ""
    if al.startswith(bl + "\\"):
        return p[len(old.rstrip("/\\")):]
    return None


def remap(path, moved: list) -> str | None:
    """Новий шлях для path за історією перенесень (точніший — першим) або None."""
    if not path:
        return None
    p = str(path)
    for old, new in sorted(moved, key=lambda m: -len(m[0])):
        rest = _same_or_inside(p, old)
        if rest is not None:
            return str(new) + rest
    return None


def sync(app) -> int:
    """Застосувати перенесення Різака: налаштування й черга. Повертає, скільки змін. Дешево: лише коли cutter.json змінився."""
    f = discovery_file()
    try:
        stamp = str(f.stat().st_mtime_ns)
    except OSError:
        return 0
    if app.db.get("cutter_seen") == stamp:
        return 0
    data = load() or {}
    moved = data.get("moved") or []
    n = 0
    if moved:
        n += _sync_config(app, moved)
        n += _sync_items(app, moved)
    app.db.set("cutter_seen", stamp)
    if n:
        log.info("Різак переніс відео — Poster перейшов на нові папки (змін: %d)", n)
    return n


def _sync_config(app, moved: list) -> int:
    cfg = app.cfg
    path = Path(cfg.get("config_path") or "")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    n = 0
    for keys in (("paths", "ready"), ("highlights", "folder")):
        cur = raw
        for k in keys[:-1]:
            cur = cur.setdefault(k, {})
        val = cur.get(keys[-1])
        if not val and keys == ("paths", "ready"):
            val = str(cfg["paths"]["ready"])                # типова папка (не вписана в config.json) — діюча
        if not val:
            continue
        absval = str(Path(val) if Path(val).is_absolute() else (path.parent / val).resolve())
        new = remap(absval, moved)
        if new and new != absval:
            cur[keys[-1]] = new
            n += 1
            log.info("Папку %s змінено: %s → %s", ".".join(keys), val, new)
            if keys == ("paths", "ready"):
                cfg["paths"]["ready"] = Path(new)
            else:
                cfg["highlights"]["folder_path"] = Path(new)
    if n:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    return n


def _sync_items(app, moved: list) -> int:
    """Відео в черзі (і щойно «зниклі» через перенесення) — на нові шляхи; «зниклі» повертаються в чергу."""
    db = app.db
    n = 0
    for it in db.items("status IN ('queued','notified','approved','failed','missing','duplicate')"):
        mp4 = remap(it["mp4"], moved)
        if not mp4 or not Path(mp4).exists():
            continue
        js = remap(it["json"], moved) or str(Path(mp4).with_suffix(".json"))
        fields = {"mp4": mp4, "json": js}
        if it["status"] == "missing":
            fields.update(status="queued", note=None, slot_at=None)
        db.update_item(it["id"], **fields)
        db.event(it["id"], "moved_by_cutter", {"from": it["mp4"], "to": mp4})
        n += 1
    return n


# ---------------------------------------------------------------- пошук «зниклих» файлів за назвою
def search_roots(app) -> list[Path]:
    """Де шукати відео, якщо його немає на старому місці: папки Різака (Контент, До публікації…) і папка шортсів."""
    data = load() or {}
    roots = [data.get("publish"), data.get("content"), *((data.get("folders") or {}).values()),
             str(app.cfg["paths"]["ready"])]
    out, seen = [], set()
    for r in roots:
        if r and Path(r).is_dir() and str(Path(r).resolve()).lower() not in seen:
            seen.add(str(Path(r).resolve()).lower())
            out.append(Path(r))
    return out


def build_index(app, limit: int = 20000) -> dict[str, list[Path]]:
    """{назва файлу (без регістру): [шляхи]} — лише .mp4, з обмеженням, щоб не обходити весь диск."""
    idx: dict[str, list[Path]] = {}
    n = 0
    for root in search_roots(app):
        for f in root.rglob("*.mp4"):
            idx.setdefault(f.name.lower(), []).append(f)
            n += 1
            if n >= limit:
                return idx
    return idx


def locate(path, idx: dict[str, list[Path]]) -> Path | None:
    """Новий шлях файлу з такою самою назвою — лише якщо знайдено рівно один (інакше не вгадуємо)."""
    hits = [p for p in idx.get(Path(str(path)).name.lower(), []) if p.exists()]
    uniq = {str(p.resolve()).lower(): p for p in hits}
    return next(iter(uniq.values())) if len(uniq) == 1 else None


def recover_missing(app, days: int = 7) -> int:
    """Відео, що «зникли» (перенесені Різаком чи вручну), — знайти за назвою й повернути в чергу."""
    import time as _t
    db = app.db
    items = [it for it in db.items("status='missing'")
             if (it.get("added_at") or 0) > _t.time() - days * 86400 and not Path(it["mp4"]).exists()]
    if not items:
        return 0
    idx = build_index(app)
    n = 0
    for it in items:
        new = locate(it["mp4"], idx)
        if not new:
            continue
        js = new.with_suffix(".json")
        db.update_item(it["id"], mp4=str(new), json=str(js), status="queued", note=None, slot_at=None)
        db.event(it["id"], "found_moved", {"from": it["mp4"], "to": str(new)})
        log.info("#%s знайдено на новому місці — повернуто в чергу: %s", it["id"], new)
        n += 1
    return n


def follow_publish_folder(app) -> bool:
    """Основний канал: якщо папки шортсів немає (або в ній жодного відео), а Різак кладе готові шортси
    в іншу («До публікації»), — Poster переходить на неї (і записує в config.json)."""
    cfg = app.cfg
    if cfg.get("channel"):
        return False
    pub = (load() or {}).get("publish")
    if not pub or not Path(pub).is_dir():
        return False
    ready = Path(cfg["paths"]["ready"])
    if str(ready.resolve()).lower() == str(Path(pub).resolve()).lower():
        return False
    empty = not ready.exists() or not any(ready.rglob("*.mp4"))
    if not empty or not any(Path(pub).rglob("*.mp4")):
        return False
    path = Path(cfg.get("config_path") or "")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.setdefault("paths", {})["ready"] = str(pub)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, ValueError) as e:
        log.warning("Не вдалося записати нову папку шортсів: %s", e)
        return False
    cfg["paths"]["ready"] = Path(pub)
    log.info("Папку шортсів змінено на ту, куди кладе Різак: %s → %s", ready, pub)
    return True
