"""Основа панелі: конфіг, фонові завдання, запуск poster.py, пароль і сесії."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import subprocess
import threading
import time
from app import ROOT
from app.core.config import load_config
from app.core.config import now_ts
from app.core.db import DB
from app.system import control
from pathlib import Path
from .common import (
    DEFAULT_PORT,
    SESSION_HOURS,
    log,
)


class BaseMixin:
    def __init__(self, config_path: str | None = None):
        self.config_path = config_path
        self.cfg = load_config(config_path)
        self.root = Path(self.cfg["root"])
        self.db = DB(Path(self.cfg["paths"]["data"]) / "poster.sqlite3")
        self.port = int(self.cfg.get("panel_port") or DEFAULT_PORT)
        self.sessions: dict[str, float] = {}
        self.jobs: dict[str, dict] = {}
        self.restart_needed = False
        self.meta_pages: list[dict] = []
        self.tiktok_flow: dict | None = None
        self.lock = threading.Lock()

    def reload(self) -> dict:
        self.cfg = load_config(self.config_path)
        return self.cfg

    def raw_config(self) -> dict:
        return json.loads(Path(self.cfg["config_path"]).read_text(encoding="utf-8"))

    def mark_restart(self) -> None:
        if control.is_running(self.cfg["lock_port"]):
            self.restart_needed = True

    def job(self, name: str, fn, *args) -> None:
        with self.lock:
            if self.jobs.get(name, {}).get("state") == "running":
                raise ValueError("Уже виконується — зачекайте")
            self.jobs[name] = {"state": "running", "message": "Виконується…", "started": now_ts()}

        def run():
            try:
                msg = fn(*args)
                self.jobs[name] = {"state": "ok", "message": msg or "Готово", "finished": now_ts()}
            except Exception as e:  # noqa: BLE001
                log.exception("Задача %s", name)
                self.jobs[name] = {"state": "error", "message": str(e), "finished": now_ts()}
        threading.Thread(target=run, daemon=True, name=f"job-{name}").start()

    def run_poster(self, *args, timeout: int = 180) -> tuple[int, str]:
        # код програми — поруч із panel.py (ROOT); self.root — папка налаштувань (для EN-каналу — channels/en)
        r = subprocess.run([control.python_exe(windowless=False), str(ROOT / "poster.py"),
                            "--config", self.cfg["config_path"], *args],
                           cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = re.sub(r"^\d{4}-\d\d-\d\d [\d:,]+ \[\w+\] [\w.]+: .*\n?", "", r.stdout + r.stderr, flags=re.M)
        return r.returncode, out.strip()

    def has_password(self) -> bool:
        return bool(self.db.get("panel_pw"))

    def set_password(self, pw: str) -> None:
        if len(pw) < 4:
            raise ValueError("Пароль — щонайменше 4 символи")
        salt = secrets.token_bytes(16)
        h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
        self.db.set("panel_pw", f"{salt.hex()}:{h.hex()}")

    def check_password(self, pw: str) -> bool:
        stored = self.db.get("panel_pw") or ""
        if ":" not in stored:
            return False
        salt, h = stored.split(":", 1)
        got = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
        return hmac.compare_digest(got, h)

    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self.sessions[token] = time.time() + SESSION_HOURS * 3600
        return token

    def valid_session(self, token: str | None) -> bool:
        exp = self.sessions.get(token or "")
        return bool(exp and exp > time.time())
