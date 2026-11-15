"""Автооновлення з GitHub: порівнює VERSION у репозиторії з локальною, завантажує архів і оновлює файли.

Ваші файли (.env, config.json, client_secret.json, tokens/, data/, log.txt, .venv/) ніколи не змінюються —
у репозиторії їх немає, а архів лише додає/замінює файли програми. Перед оновленням робиться резервна копія.
Приватний репозиторій — потрібен токен GitHub (GITHUB_TOKEN у .env, лише читання вмісту).
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import requests

log = logging.getLogger("updater")
ROOT = Path(__file__).resolve().parent
API = "https://api.github.com"
PROTECT = {".env", "config.json", "client_secret.json", "log.txt", ".git"}
PROTECT_DIRS = {".venv", "tokens", "data", ".git", "__pycache__"}


class UpdateError(Exception):
    pass


def version_key(v: str) -> tuple:
    """«2026.11.12-40» → ((2026, 11, 12), 40) для порівняння."""
    m = re.match(r"(\d+)\.(\d+)\.(\d+)(?:-(\d+))?", str(v or ""))
    if not m:
        return ((0, 0, 0), 0)
    return ((int(m.group(1)), int(m.group(2)), int(m.group(3))), int(m.group(4) or 0))


def _conf(cfg: dict) -> tuple[str, str, dict]:
    up = cfg.get("updates") or {}
    repo = up.get("repo") or "MrAntibiotik/poster"
    branch = up.get("branch") or "main"
    token = (cfg.get("secrets") or {}).get("GITHUB_TOKEN") or ""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "poster-updater",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return repo, branch, headers


def _explain(r: requests.Response, token: bool) -> str:
    if r.status_code in (401, 403) and token:
        return "GitHub відхилив токен (прострочений або без доступу до репозиторію)"
    if r.status_code in (404, 401, 403):
        return ("репозиторій не знайдено — він приватний: додайте токен GitHub у панелі "
                "(вкладка «🔄 Оновлення»)" if not token else "репозиторій не знайдено або токен не має до нього доступу")
    return f"GitHub HTTP {r.status_code}: {r.text[:150]}"


def remote_version(cfg: dict) -> str:
    repo, branch, headers = _conf(cfg)
    try:
        r = requests.get(f"{API}/repos/{repo}/contents/config.py", params={"ref": branch},
                         headers={**headers, "Accept": "application/vnd.github.raw"}, timeout=20)
    except requests.RequestException as e:
        raise UpdateError(f"немає зв'язку з GitHub: {e}") from e
    if r.status_code != 200:
        raise UpdateError(_explain(r, "Authorization" in headers))
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', r.text, re.M)
    if not m:
        raise UpdateError("у репозиторії не знайдено VERSION")
    return m.group(1)


def check(cfg: dict) -> dict:
    from config import VERSION
    remote = remote_version(cfg)
    return {"local": VERSION, "remote": remote, "newer": version_key(remote) > version_key(VERSION)}


def _protected(rel: Path) -> bool:
    return rel.name in PROTECT and len(rel.parts) == 1 or any(p in PROTECT_DIRS for p in rel.parts)


def apply(cfg: dict, root: Path = ROOT) -> str:
    """Завантажує й розгортає останню версію. Повертає нову версію."""
    repo, branch, headers = _conf(cfg)
    try:
        r = requests.get(f"{API}/repos/{repo}/zipball/{branch}", headers=headers, timeout=120)
    except requests.RequestException as e:
        raise UpdateError(f"не вдалося завантажити оновлення: {e}") from e
    if r.status_code != 200:
        raise UpdateError(_explain(r, "Authorization" in headers))
    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile as e:
        raise UpdateError("архів оновлення пошкоджено") from e
    names = [n for n in zf.namelist() if not n.endswith("/")]
    prefix = os.path.commonpath(names).split("/")[0] + "/" if names else ""
    files = {n[len(prefix):]: n for n in names if n.startswith(prefix)}
    if "poster.py" not in files or "config.py" not in files:
        raise UpdateError("в архіві немає файлів програми")
    m = re.search(rb'^VERSION\s*=\s*"([^"]+)"', zf.read(files["config.py"]), re.M)
    new_version = m.group(1).decode() if m else "?"

    from config import VERSION
    backup = Path(cfg["paths"]["data"]) / "backup" / VERSION
    old_req = (root / "requirements.txt").read_bytes() if (root / "requirements.txt").exists() else b""
    with tempfile.TemporaryDirectory(dir=str(Path(cfg["paths"]["data"]))) as tmp:
        staged = []
        for rel_s, name in files.items():
            rel = Path(rel_s)
            if rel.is_absolute() or ".." in rel.parts or _protected(rel):
                continue
            dst = Path(tmp) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(zf.read(name))
            staged.append(rel)
        for rel in staged:  # резервна копія того, що буде замінено
            cur = root / rel
            if cur.is_file():
                (backup / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cur, backup / rel)
        for rel in staged:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            os.replace(Path(tmp) / rel, root / rel)
    log.info("Оновлено %s → %s (%s файлів; резервна копія: %s)", VERSION, new_version, len(staged), backup)
    new_req = (root / "requirements.txt").read_bytes() if (root / "requirements.txt").exists() else b""
    if new_req != old_req:
        install_requirements(root)
    return new_version


def rollback(cfg: dict, version: str, root: Path = ROOT) -> int:
    """Повертає файли програми з резервної копії data/backup/<версія> (якщо нова версія не запустилась)."""
    backup = Path(cfg["paths"]["data"]) / "backup" / version
    if not backup.is_dir():
        raise UpdateError(f"резервної копії {version} немає")
    n = 0
    for f in backup.rglob("*"):
        if f.is_file():
            rel = f.relative_to(backup)
            if _protected(rel):
                continue
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, root / rel)
            n += 1
    log.warning("Відкат до %s: відновлено %s файлів", version, n)
    return n


def install_requirements(root: Path = ROOT) -> None:
    from control import python_exe
    log.info("Змінились залежності — встановлюю")
    r = subprocess.run([python_exe(windowless=False), "-m", "pip", "install", "-q", "-r", str(root / "requirements.txt")],
                       cwd=root, capture_output=True, text=True, timeout=900,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode:
        log.error("pip: %s", (r.stderr or r.stdout)[-500:])


def spawn(args: list[str], root: Path = ROOT) -> None:
    """Запускає окремий процес, що переживе поточний (для перезапуску після оновлення)."""
    from control import python_exe
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen([python_exe(windowless=True), *args], cwd=root, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=sys.platform != "win32")
