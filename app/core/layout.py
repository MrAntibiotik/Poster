"""Де лежать дані каналів.

    <папка програми>/
        app/ … poster.py, panel.py   — код (спільний для всіх каналів, оновлюється з GitHub)
        channels/main/               — основний канал: config.json, .env, client_secret.json, tokens/, data/, log.txt
        channels/en/                 — EU/EN-канал: те саме, окремо

До версії 2026.11.16-87 дані основного каналу лежали прямо в корені програми. migrate_main() один раз
переносить їх у channels/main: копіює → перевіряє → перемикається. Старі файли в корені лишаються
резервною копією (root config.json перейменовується, щоб ніхто його не підхопив).
Відео-папки (ready/posted/skipped/cancelled) нікуди не переносяться — у конфігу лише стають абсолютними шляхами.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path

from app import ROOT

log = logging.getLogger("layout")

CHANNELS_DIR = ROOT / "channels"
MAIN_KEY = "main"
CHANNEL_FILES = ("data", "tokens", "log")          # переносяться разом із каналом
MOVED_SUFFIX = ".перенесено"
NOTE = "ПЕРЕНЕСЕНО в channels\\main.txt"
MIGRATED_FROM: Path | None = None                   # стара база, якщо перенесення було в цьому процесі


def main_dir(root: Path = ROOT) -> Path:
    return root / "channels" / MAIN_KEY


def default_config_path(root: Path = ROOT) -> Path:
    return resolve_config(None, root)


def resolve_config(path, root: Path = ROOT) -> Path:
    """Шлях до config.json з урахуванням нового розташування: старий <корінь>/config.json → channels/main."""
    p = Path(path or os.environ.get("POSTER_CONFIG") or root / "config.json").resolve()
    legacy = (root / "config.json").resolve()
    if p != legacy:
        return p
    new = main_dir(root) / "config.json"
    if new.exists():
        return new.resolve()
    if legacy.exists():
        try:
            got = migrate_main(root)
        except Exception as e:  # noqa: BLE001 — не вдалося: працюємо по-старому, з кореня
            log.error("Перенесення основного каналу в channels/main не вдалося: %s", e)
            got = None
        if got:
            return got.resolve()
    return p


def _rel(value, default: str) -> Path:
    return Path(os.path.expandvars(str(value or default)))


def _absolute(p: Path) -> bool:
    """Абсолютний шлях — і для Windows-вигляду (D:/…, \\\\server), де б не перевіряли."""
    s = str(p).replace("\\", "/")
    return p.is_absolute() or bool(re.match(r"^[A-Za-z]:/", s)) or s.startswith("//")


def _copy_db(src: Path, dst: Path) -> None:
    """Узгоджена копія SQLite (разом із WAL), навіть якщо базу зараз хтось тримає відкритою."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def _tables(db: Path) -> dict:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {n: c.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        c.close()


def _copy_tree(src: Path, dst: Path) -> list[Path]:
    """Копіює папку; бази SQLite — через backup. Повертає список баз."""
    dbs = []
    for f in src.rglob("*"):
        rel = f.relative_to(src)
        if f.is_dir() or "__pycache__" in rel.parts:
            continue
        if f.name.endswith((".sqlite3-wal", ".sqlite3-shm", "-journal")):
            continue
        if f.suffix == ".sqlite3":
            _copy_db(f, dst / rel)
            dbs.append(rel)
            continue
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(f, dst / rel)
        except OSError as e:     # зайнятий файл (напр. тимчасовий) — не критично
            log.warning("Не скопійовано %s: %s", f, e)
    return dbs


def _acquire(lock: Path) -> bool:
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            os.close(os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 300:   # завислий замок
                    lock.unlink()
                    continue
            except OSError:
                pass
            return False
    return False


def migrate_main(root: Path = ROOT) -> Path | None:
    """Переносить дані основного каналу з кореня в channels/main. Повертає новий шлях config.json."""
    global MIGRATED_FROM
    from app.core.config import DEFAULTS
    src_cfg = root / "config.json"
    dst = main_dir(root)
    if (dst / "config.json").exists():
        return dst / "config.json"
    if not src_cfg.exists():
        return None
    lock = root / "channels" / ".migrate.lock"
    if not _acquire(lock):              # переносить інший процес (бот/панель) — чекаємо
        for _ in range(120):
            if (dst / "config.json").exists():
                return dst / "config.json"
            time.sleep(0.5)
        return None
    tmp = dst.with_name(MAIN_KEY + ".tmp")
    try:
        raw = json.loads(src_cfg.read_text(encoding="utf-8"))
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        paths = raw.setdefault("paths", {})
        old_db = None
        checks = []
        for key, default in DEFAULTS["paths"].items():
            p = _rel(paths.get(key), default)
            if _absolute(p):
                continue
            if key not in CHANNEL_FILES:
                paths[key] = str((root / p).resolve())   # відео-папки лишаються на місці
                continue
            src = root / p
            if src.is_dir():
                for rel in _copy_tree(src, tmp / p):
                    checks.append((src / rel, tmp / p / rel))
                    if rel.name == "poster.sqlite3":
                        old_db = src / rel
            elif src.is_file():
                (tmp / p).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, tmp / p)
        yt = (raw.get("platforms") or {}).get("youtube") or {}
        sec = _rel(yt.get("client_secrets_file"), "client_secret.json")
        if not _absolute(sec) and (root / sec).is_file():
            (tmp / sec).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / sec, tmp / sec)
        if (root / ".env").is_file():
            shutil.copy2(root / ".env", tmp / ".env")
        (tmp / "config.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        # перевірка: конфіг читається, у копіях баз ті самі таблиці й не менше записів
        json.loads((tmp / "config.json").read_text(encoding="utf-8"))
        for a, b in checks:
            ta, tb = _tables(a), _tables(b)
            if set(ta) != set(tb) or any(tb[k] < ta[k] for k in ta):
                raise RuntimeError(f"копія бази {a.name} не збігається з оригіналом")
        tmp.rename(dst)
        os.replace(src_cfg, src_cfg.with_name("config.json" + MOVED_SUFFIX))
        (root / NOTE).write_text(
            "Дані основного каналу тепер у папці channels\\main (config.json, .env, client_secret.json, tokens, data, log.txt).\n"
            "Тут, у корені, лишились їхні старі копії (data, tokens, .env, client_secret.json, log.txt, "
            "config.json" + MOVED_SUFFIX + ") — це резервна копія на випадок проблем.\n"
            "Коли переконаєтесь, що все працює (кілька днів), ці старі копії можна видалити.\n",
            encoding="utf-8")
        MIGRATED_FROM = old_db
        log.warning("Основний канал перенесено в %s", dst)
        return dst / "config.json"
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        try:
            lock.unlink()
        except OSError:
            pass
