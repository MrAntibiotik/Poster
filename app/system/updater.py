"""Оновлення й історія версій.

* Оновлення: порівнює VERSION у репозиторії з локальною, завантажує архів і встановлює файли програми.
* Історія: перед кожною зміною версії повна копія коду зберігається в data/versions/<версія>
  (код займає ~1 МБ, тримаємо останні KEEP_VERSIONS). Кожен реліз також має тег v<версія> на GitHub.
* Відкат (rollback/switch_to): на будь-яку збережену версію або на будь-який тег із GitHub.
  Файли, яких у цільовій версії немає, прибираються (за списком встановлених файлів data/installed.json).

Ваші файли (.env, config.json, client_secret.json, tokens/, data/, channels/, log.txt, .venv/) ніколи не змінюються.
Приватний репозиторій — потрібен токен GitHub (GITHUB_TOKEN у .env, лише читання вмісту).
"""
from __future__ import annotations

import io
import json
import logging
import time
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
from app import ROOT  # noqa: E402  папка програми
API = "https://api.github.com"
PROTECT = {".env", "config.json", "client_secret.json", "log.txt", ".git"}
PROTECT_DIRS = {".venv", "tokens", "data", ".git", "__pycache__"}
PROTECT_TOP = {"channels", "cancelled"}           # верхні папки з даними користувача (канали, скасовані шортси)
KEEP_VERSIONS = 15
# модулі зі старого (до 2026.11.16-87) розташування в корені — тепер вони в app/
LEGACY = ["ai.py", "analytics.py", "bot.py", "channels.py", "control.py", "db.py", "dupes.py", "eu_tab.py",
          "media.py", "meta.py", "panel.html", "power.py", "publisher.py", "reports.py", "scheduler.py",
          "telegram_api.py", "updater.py", "platforms"]


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
    from app.core.config import VERSION
    remote = remote_version(cfg)
    return {"local": VERSION, "remote": remote, "newer": version_key(remote) > version_key(VERSION)}


def _protected(rel: Path) -> bool:
    return (rel.name in PROTECT and len(rel.parts) == 1 or any(p in PROTECT_DIRS for p in rel.parts)
            or (len(rel.parts) > 1 and rel.parts[0] in PROTECT_TOP))


# ---------------------------------------------------------------- історія версій
def versions_dir(cfg: dict) -> Path:
    return Path(cfg["paths"]["data"]) / "versions"


def _manifest_path(cfg: dict) -> Path:
    return Path(cfg["paths"]["data"]) / "installed.json"


def installed_files(cfg: dict, root: Path = ROOT) -> list[str] | None:
    """Файли програми, встановлені останнім оновленням (None — список ще не вівся)."""
    try:
        files = json.loads(_manifest_path(cfg).read_text(encoding="utf-8")).get("files")
        return sorted(files) if isinstance(files, list) else None
    except (OSError, ValueError):
        return None


def _save_manifest(cfg: dict, version: str, files) -> None:
    p = _manifest_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"version": version, "files": sorted(set(files))}, ensure_ascii=False), encoding="utf-8")


PROGRAM_EXT = {".py", ".ico", ".html", ".css", ".js", ".bat", ".cmd", ".vbs", ".ps1", ".md", ".txt", ".example", ".json"}


def program_files(cfg: dict, root: Path = ROOT) -> list[str]:
    """Поточні файли програми: зі списку встановлених або (вперше) — пошуком у папці програми."""
    files = installed_files(cfg, root)
    if files is not None:
        return [f for f in files if (root / f).is_file()]
    out = []
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(root)
        if _protected(rel) or rel.suffix.lower() not in PROGRAM_EXT or rel.parts[0].startswith("."):
            continue
        if rel.suffix == ".json" and not rel.name.endswith(".example.json"):
            continue   # .json — це налаштування користувача, крім прикладів
        if rel.suffix == ".txt" and rel.name != "requirements.txt":
            continue
        out.append(rel.as_posix())
    return sorted(out)


def snapshot(cfg: dict, version: str, root: Path = ROOT) -> Path:
    """Повна копія коду поточної версії в data/versions/<версія> (для відкату)."""
    dst = versions_dir(cfg) / version
    files = program_files(cfg, root)
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    for rel in files:
        (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, tmp / rel)
    (tmp / "version.json").write_text(json.dumps({"version": version, "saved": int(time.time()), "files": files},
                                                 ensure_ascii=False), encoding="utf-8")
    shutil.rmtree(dst, ignore_errors=True)
    tmp.rename(dst)
    _prune_versions(cfg, keep=version)
    return dst


def _prune_versions(cfg: dict, keep: str) -> None:
    vs = sorted(local_versions(cfg), key=version_key, reverse=True)
    for v in vs[KEEP_VERSIONS:]:
        if v != keep:
            shutil.rmtree(versions_dir(cfg) / v, ignore_errors=True)


def local_versions(cfg: dict) -> list[str]:
    d = versions_dir(cfg)
    if not d.is_dir():
        return []
    return sorted((p.name for p in d.iterdir() if (p / "version.json").is_file()), key=version_key, reverse=True)


def _install(cfg: dict, new_files: dict[str, bytes], new_version: str, root: Path = ROOT) -> int:
    """Записує файли нової версії; прибирає файли попередньої, яких у новій немає. Повертає к-сть файлів."""
    from app.core.config import VERSION
    old_set = installed_files(cfg, root)
    old_req = (root / "requirements.txt").read_bytes() if (root / "requirements.txt").exists() else b""
    snapshot(cfg, VERSION, root)
    with tempfile.TemporaryDirectory(dir=str(Path(cfg["paths"]["data"]))) as tmp:
        for rel, data in new_files.items():
            dst = Path(tmp) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
        for rel in new_files:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            os.replace(Path(tmp) / rel, root / rel)
    removed = 0
    for rel in sorted(set(old_set or []) - set(new_files)):   # копія є у знімку попередньої версії
        f = root / rel
        if f.is_file() and not _protected(Path(rel)):
            f.unlink()
            removed += 1
            _drop_empty_dirs(f.parent, root)
    _save_manifest(cfg, new_version, new_files)
    log.info("Встановлено %s → %s (%s файлів, прибрано %s; копія попередньої: data/versions/%s)",
             VERSION, new_version, len(new_files), removed, VERSION)
    new_req = (root / "requirements.txt").read_bytes() if (root / "requirements.txt").exists() else b""
    if new_req != old_req:
        install_requirements(root)
    return len(new_files)


def _drop_empty_dirs(d: Path, root: Path) -> None:
    while d != root and d.is_dir():
        if any(p for p in d.iterdir() if p.name != "__pycache__"):
            return
        shutil.rmtree(d, ignore_errors=True)
        d = d.parent


def cleanup_legacy(cfg: dict, root: Path = ROOT) -> int:
    """Одноразово після переходу на нову структуру: старі модулі з кореня → data/versions/legacy-root.
    Викликається лише після успішного запуску нової версії (щоб відкат на стару ще мав ці файли)."""
    if not (root / "app" / "__init__.py").is_file():
        return 0
    dst = versions_dir(cfg) / "legacy-root"
    n = 0
    for name in LEGACY:
        src = root / name
        if not src.exists():
            continue
        (dst / name).parent.mkdir(parents=True, exist_ok=True)
        if (dst / name).exists():
            shutil.rmtree(dst / name, ignore_errors=True) if (dst / name).is_dir() else (dst / name).unlink()
        shutil.move(str(src), str(dst / name))
        n += 1
    if n:
        log.info("Старі модулі з кореня (%s) перенесено в %s", n, dst)
    if installed_files(cfg, root) is None:
        from app.core.config import VERSION
        _save_manifest(cfg, VERSION, program_files(cfg, root))
    return n


def _download(cfg: dict, ref: str | None = None) -> tuple[str, dict[str, bytes]]:
    """Архів гілки (або тегу ref) з GitHub → (версія, {шлях: вміст}) без захищених файлів."""
    repo, branch, headers = _conf(cfg)
    try:
        r = requests.get(f"{API}/repos/{repo}/zipball/{ref or branch}", headers=headers, timeout=120)
    except requests.RequestException as e:
        raise UpdateError(f"не вдалося завантажити оновлення: {e}") from e
    if r.status_code == 404 and ref:
        raise UpdateError(f"версії {ref} на GitHub немає")
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
    out = {}
    for rel_s, name in files.items():
        rel = Path(rel_s)
        if rel.is_absolute() or ".." in rel.parts or _protected(rel):
            continue
        out[rel.as_posix()] = zf.read(name)
    return (m.group(1).decode() if m else "?"), out


def apply(cfg: dict, root: Path = ROOT, ref: str | None = None) -> str:
    """Завантажує й встановлює останню версію (або тег ref). Повертає нову версію."""
    new_version, files = _download(cfg, ref)
    _install(cfg, files, new_version, root)
    return new_version


def rollback(cfg: dict, version: str, root: Path = ROOT) -> int:
    """Повертає файли програми версії version: з повної копії data/versions/<версія>
    (або, для старих версій, з часткової резервної копії data/backup/<версія>)."""
    snap = versions_dir(cfg) / version
    if (snap / "version.json").is_file():
        files = json.loads((snap / "version.json").read_text(encoding="utf-8"))["files"]
        n = _install(cfg, {rel: (snap / rel).read_bytes() for rel in files if (snap / rel).is_file()}, version, root)
        log.warning("Відкат до %s: відновлено %s файлів", version, n)
        return n
    backup = Path(cfg["paths"]["data"]) / "backup" / version
    if not backup.is_dir():
        raise UpdateError(f"копії версії {version} немає")
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


def remote_versions(cfg: dict) -> list[str]:
    """Версії, що мають тег на GitHub (v<версія>), найновіші першими."""
    repo, _, headers = _conf(cfg)
    try:
        r = requests.get(f"{API}/repos/{repo}/tags", params={"per_page": 100}, headers=headers, timeout=20)
    except requests.RequestException as e:
        raise UpdateError(f"немає зв'язку з GitHub: {e}") from e
    if r.status_code != 200:
        raise UpdateError(_explain(r, "Authorization" in headers))
    vs = [t["name"][1:] for t in r.json() if str(t.get("name", "")).startswith("v")]
    return sorted(vs, key=version_key, reverse=True)


def changelog(root: Path = ROOT) -> dict[str, list[str]]:
    """CHANGELOG.md → {версія: [пункти]}."""
    out: dict[str, list[str]] = {}
    cur = None
    try:
        text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        m = re.match(r"^##\s+(\S+)", line)
        if m:
            cur = m.group(1)
            out[cur] = []
        elif cur and line.lstrip().startswith(("- ", "* ")):
            out[cur].append(line.lstrip()[2:].strip())
    return out


def version_list(cfg: dict, root: Path = ROOT, remote: bool = True) -> list[dict]:
    """Версії, на які можна перейти: збережені локально + теги на GitHub, з описом змін."""
    from app.core.config import VERSION
    notes = changelog(root)
    local = set(local_versions(cfg))
    tags: set[str] = set()
    error = ""
    if remote:
        try:
            tags = set(remote_versions(cfg))
        except UpdateError as e:
            error = str(e)
    out = []
    for v in sorted(local | tags | {VERSION}, key=version_key, reverse=True):
        out.append({"version": v, "local": v in local, "remote": v in tags, "current": v == VERSION,
                    "notes": notes.get(v, [])})
    if error:
        out.append({"error": error})
    return out


def switch_to(cfg: dict, version: str, root: Path = ROOT) -> str:
    """Перейти на конкретну версію (відкат або вперед). Автооновлення ставиться на паузу,
    інакше програма за годину сама повернулась би на останню версію. «⬆️ Оновити» знімає паузу."""
    from app.core.config import VERSION
    if version == VERSION:
        raise UpdateError(f"версія {version} уже встановлена")
    if (versions_dir(cfg) / version / "version.json").is_file():
        rollback(cfg, version, root)
    else:
        got = apply(cfg, root, ref=f"v{version}")
        if got != version:
            raise UpdateError(f"у тегу v{version} знайдено версію {got}")
    set_auto_paused(cfg, version)
    return version


def set_auto_paused(cfg: dict, version: str | None) -> None:
    """Пауза автооновлення після ручного відкату (у config.json — її розуміють і старі версії програми)."""
    path = Path(cfg["config_path"])
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    up = raw.setdefault("updates", {})
    if version:
        if "pinned" not in up:
            up["auto_before_pin"] = bool(up.get("auto", (cfg.get("updates") or {}).get("auto", True)))
        up["auto"] = False
        up["pinned"] = version
    else:
        if "pinned" not in up:
            return
        up["auto"] = bool(up.pop("auto_before_pin", True))
        up.pop("pinned", None)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def install_requirements(root: Path = ROOT) -> None:
    from app.system.control import python_exe
    log.info("Змінились залежності — встановлюю")
    r = subprocess.run([python_exe(windowless=False), "-m", "pip", "install", "-q", "-r", str(root / "requirements.txt")],
                       cwd=root, capture_output=True, text=True, timeout=900,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode:
        log.error("pip: %s", (r.stderr or r.stdout)[-500:])


def spawn(args: list[str], root: Path = ROOT) -> None:
    """Запускає окремий процес, що переживе поточний (для перезапуску після оновлення)."""
    from app.system.control import python_exe
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen([python_exe(windowless=True), *args], cwd=root, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=sys.platform != "win32")
