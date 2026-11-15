"""Розклад і черга: сканування ready/, слоти, сповіщення за годину, пропуски, звіти."""
from __future__ import annotations

import logging
import re
from pathlib import Path


log = logging.getLogger("scheduler")


SKIP_DIRS = {"posted", "skipped", "cancelled", "data", "tokens", "previews", ".venv", "__pycache__"}


def find_videos(folder: Path, depth: int = 4, exclude: tuple = ()) -> list[Path]:
    """mp4 у папці та підпапках до `depth` рівнів (напр. output/job_…/battle_…/відео.mp4).

    Розширення без урахування регістру; папки posted/skipped/службові і `exclude` пропускаються.
    """
    out: list[Path] = []
    excl = {Path(e).resolve() for e in exclude}

    def walk(d: Path, level: int) -> None:
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return
        for e in entries:
            try:
                if e.is_file() and e.suffix.lower() == ".mp4":
                    out.append(e)
                elif (e.is_dir() and level < depth and not e.name.startswith(".")
                      and e.name.lower() not in SKIP_DIRS and e.resolve() not in excl):
                    walk(e, level + 1)
            except OSError:
                continue
    walk(folder, 0)
    return out


AUTO_FOLDER = re.compile(r"(?i)^(battle|job|clip|short|video|output|replay|render|export)s?[_\-\s]?\d"
                         r"|_(dmg|assist|kills?|frags?)_\d|^\d{4}[_\-.]?\d\d[_\-.]?\d\d|^\d+$")


def is_auto_folder(name: str) -> bool:
    """Папки, які створює програма нарізки (battle_02_dmg_5004, job_20261111_…), — це не назва гри."""
    return bool(AUTO_FOLDER.search(name or ""))


def game_of(mp4: Path, ready: Path) -> str:
    """Назва «гри» = перша «людська» підпапка в папці з шортсами (ready/WoT/… → WoT); інакше — ''."""
    try:
        parts = mp4.resolve().relative_to(ready.resolve()).parts[:-1]
    except ValueError:
        parts = (mp4.parent.name,)
    return next((p for p in parts if not is_auto_folder(p)), "")
