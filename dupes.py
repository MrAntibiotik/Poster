"""Захист від повторних публікацій.

Той самий шортс може вдруге потрапити в папку готових: двічі натиснули «Підходить» в автонарізці,
перенарізали той самий запис, скопіювали ще раз. Програма раніше відрізняла відео лише за шляхом —
і публікувала його знову. Тепер новий файл порівнюється з уже відомими:
  • «same» — той самий вміст (відбиток: розмір + перший і останній мегабайт);
  • «name» — та сама назва (без «_2», « (1)» в кінці) серед шортсів за останні DAYS днів.
Такий файл не стає в чергу, а бот питає: «Опублікувати ще раз?».
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from config import now_ts

CHUNK = 1024 * 1024
DAYS = 60
# статуси, у яких шортс уже «зайнятий»: у черзі, чекає, публікується, опублікований, невдалий
LIVE = ("queued", "notified", "approved", "publishing", "posted", "failed")


def fingerprint(path: str | Path) -> str | None:
    p = Path(path)
    try:
        size = p.stat().st_size
        h = hashlib.sha1(str(size).encode())
        with open(p, "rb") as f:
            h.update(f.read(CHUNK))
            if size > 2 * CHUNK:
                f.seek(size - CHUNK)
                h.update(f.read(CHUNK))
        return h.hexdigest()
    except OSError:
        return None


COPY_MARK = re.compile(r"(?:\s*\(\d{1,2}\)|\s*-\s*(?:copy|копія)|(?<=\d)_\d)$")


def norm_name(stem: str) -> str:
    """Назва без позначок копії: «… (1)», «… - копія», «…_3837_2» → «rec_battle_03_dmg_3837».
    Номер бою («battle_03») не чіпається: «_N» прибирається лише після числа (dmg_3837_2)."""
    s = stem.lower().strip()
    while True:
        new = COPY_MARK.sub("", s).strip()
        if new == s or len(new) < 4:
            return s
        s = new


def backfill(db, limit: int = 30) -> int:
    """Відбитки для старих записів (до оновлення) — потроху, щоб не гальмувати."""
    n = 0
    marks = ",".join("?" * len(LIVE))
    for it in db.q(f"SELECT id, mp4 FROM items WHERE fp IS NULL AND status IN ({marks}) ORDER BY id DESC LIMIT ?",
                   (*LIVE, limit)):
        fp = fingerprint(it["mp4"])
        db.x("UPDATE items SET fp=? WHERE id=?", (fp or "-", it["id"]))
        n += 1
    return n


def find_duplicate(db, path: str | Path, fp: str | None, exclude_id: int | None = None) -> tuple[str, dict] | None:
    """('same' | 'name', запис) — якщо такий шортс уже є в роботі чи опублікований."""
    marks = ",".join("?" * len(LIVE))
    if fp:
        row = db.one(f"SELECT * FROM items WHERE fp=? AND status IN ({marks}) AND id IS NOT ? ORDER BY id DESC",
                     (fp, *LIVE, exclude_id))
        if row:
            return "same", db.get_item(row["id"])
    name = norm_name(Path(path).stem)
    if len(name) < 6:
        return None
    since = now_ts() - DAYS * 86400
    for row in db.q(f"SELECT id, mp4 FROM items WHERE status IN ({marks}) AND added_at>=? AND id IS NOT ? "
                    "ORDER BY id DESC", (*LIVE, since, exclude_id)):
        if norm_name(Path(row["mp4"]).stem) == name:
            return "name", db.get_item(row["id"])
    return None


__all__ = ["DAYS", "LIVE", "backfill", "find_duplicate", "fingerprint", "norm_name"]
