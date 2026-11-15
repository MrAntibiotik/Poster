"""Налаштування: збереження конфігу й ключів, користувачі, папки."""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from app.core.config import ConfigError
from app.core.config import SECRETS
from app.core.config import load_config
from app.system import control
from pathlib import Path
from .common import (
    FIELDS,
    coerce,
    log,
    put,
)


class SettingsMixin:
    def save_settings(self, patch: dict) -> None:
        raw = self.raw_config()
        errors = []
        for key, value in patch.items():
            if key not in FIELDS:
                continue
            try:
                put(raw, key, coerce(FIELDS[key], value))
            except (ValueError, TypeError) as e:
                errors.append(f"{key}: {e}")
        if errors:
            raise ValueError("; ".join(errors))
        path = Path(self.cfg["config_path"])
        tmp = path.with_name("config.check.json")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            load_config(tmp)
        except ConfigError as e:
            tmp.unlink(missing_ok=True)
            raise ValueError(str(e)) from e
        try:
            from app.core.config import ensure_dirs
            ensure_dirs(load_config(tmp))
        except ConfigError as e:
            tmp.unlink(missing_ok=True)
            raise ValueError(str(e)) from e
        os.replace(tmp, path)
        if patch.get("power.sleep_between_slots") is False:
            from app.system import power
            threading.Thread(target=power.remove_wake_task, daemon=True).start()
        self.reload()  # бот підхопить зміни сам протягом ~15 с, перезапуск не потрібен

    def save_secrets(self, values: dict) -> None:
        from dotenv import set_key

        env = self.cfg["env_path"]
        if not os.path.exists(env):
            Path(env).write_text("", encoding="utf-8")
        for name, value in values.items():
            if name not in SECRETS or value is None:
                continue
            value = str(value).strip()
            if not value:
                continue
            if name == "TELEGRAM_CHAT_ID":
                ids = re.findall(r"-?\d+", value)
                if not ids:
                    raise ValueError("chat_id — це число (можна кілька через кому)")
                bot = (self.cfg["secrets"].get("TELEGRAM_BOT_TOKEN") or values.get("TELEGRAM_BOT_TOKEN") or "").split(":")[0]
                if bot and bot in ids:
                    raise ValueError("Це id самого бота. Потрібен ВАШ id — напишіть @userinfobot")
                value = ",".join(ids)
            if name == "TELEGRAM_BOT_TOKEN" and not re.fullmatch(r"\d+:[\w-]{30,}", value):
                raise ValueError("Токен бота має вигляд 123456789:AA… — скопіюйте його з @BotFather повністю")
            set_key(env, name, value, quote_mode="never")
            os.environ[name] = value
        self.reload()
        if any(k.startswith("TELEGRAM_") and v for k, v in values.items()):
            self.mark_restart()  # токен / адміністратори Telegram — лише після перезапуску

    def set_user(self, chat_id: int, role: str) -> str:
        owners = [int(x) for x in re.findall(r"-?\d+", self.cfg["secrets"]["TELEGRAM_CHAT_ID"] or "")]
        if chat_id in owners:
            raise ValueError("Головних адміністраторів змінюють у полі «Головні адміністратори»")
        if role == "delete":
            self.db.del_user(chat_id)
            self.tg_say(chat_id, "🔒 Ваш доступ до бота скасовано.")
            return "Прибрано"
        self.db.set_user(chat_id, role)
        if role in ("admin", "viewer"):
            what = "адміністратор (повне керування)" if role == "admin" else "сповіщення та звіти"
            self.tg_say(chat_id, f"✅ Доступ надано: {what}. Надішліть /start, щоб побачити кнопки.")
        return "Збережено"

    def count_shorts(self, folder: str | Path) -> dict:
        """Скільки mp4 з парним json лежить у папці та її підпапках-іграх (і скільки з них готові)."""
        folder = Path(folder)
        ready_status = self.cfg["ready_status"]
        info = {"path": str(folder), "exists": folder.is_dir(), "total": 0, "ready": 0, "games": [], "no_json": 0}
        auto = bool((self.cfg.get("auto_meta") or {}).get("enabled", True))
        if not info["exists"]:
            return info
        games = set()
        try:
            from app.engine.scheduler import find_videos
            files = find_videos(folder, exclude=(self.cfg["paths"]["posted"], self.cfg["paths"]["skipped"]))[:5000]
        except OSError:
            return info
        for mp4 in files:
            info["total"] += 1
            from app.engine.scheduler import game_of
            if game_of(mp4, folder):
                games.add(game_of(mp4, folder))
            js = mp4.with_suffix(".json")
            if not js.exists():
                info["no_json"] += 1
                if auto:
                    info["ready"] += 1
                continue
            try:
                from app.core.meta import json_status, read_json
                if json_status(read_json(js)) == ready_status:
                    info["ready"] += 1
            except (OSError, ValueError):
                pass
        info["games"] = sorted(games)
        return info

    @staticmethod
    def path_problems(cfg: dict) -> list[str]:
        from app.core.config import PATH_NAMES
        out = []
        for key in ("ready", "posted", "skipped", "cancelled"):
            p = Path(cfg["paths"][key])
            if p.anchor and not Path(p.anchor).exists():
                out.append(f"«{PATH_NAMES[key]}»: {p} — диска {p.anchor} немає")
        return out

    def list_dir(self, path: str | None) -> dict:
        p = Path(path).expanduser() if path else Path(self.cfg["paths"]["ready"])
        p = (p if p.is_absolute() else self.root / p).resolve()
        while not p.is_dir() and p != p.parent:
            p = p.parent
        if not p.is_dir():
            p = Path.home()
        try:
            dirs = sorted((d.name for d in p.iterdir() if d.is_dir() and not d.name.startswith((".", "$"))),
                          key=str.lower)[:500]
        except OSError as e:
            raise ValueError(f"Немає доступу до {p}: {e}") from e
        drives = [f"{c}:\\" for c in "CDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{c}:\\")] if control.WIN else []
        return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "dirs": dirs,
                "drives": drives, "home": str(Path.home()), "shorts": self.count_shorts(p)}

    def make_dir(self, path: str, name: str) -> dict:
        name = name.strip()
        if not name or re.search(r'[<>:"/\\|?*]', name):
            raise ValueError("Недопустима назва папки")
        new = Path(path) / name
        new.mkdir(parents=True, exist_ok=True)
        return self.list_dir(str(new))

    def pick_folder_native(self, initial: str = "") -> str | None:
        """Звичайне вікно вибору папки Windows. None — якщо недоступне (тоді панель покаже свій перегляд)."""
        if not control.WIN:
            return None
        if initial and not Path(initial).is_absolute():
            initial = str((self.root / initial).resolve())
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        tk_script = (
            "import sys\n"
            "sys.stdout.reconfigure(encoding='utf-8')\n"
            "import tkinter as tk\nfrom tkinter import filedialog\n"
            "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True); r.update()\n"
            "p = filedialog.askdirectory(parent=r, initialdir=sys.argv[1] or None, mustexist=False,\n"
            "                            title='Оберіть папку з готовими шортсами')\n"
            "print('PICKED:' + (p or ''))\n")
        try:
            r = subprocess.run([control.python_exe(windowless=False), "-c", tk_script, initial or ""],
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=900, creationflags=flags)
            for line in r.stdout.splitlines():
                if line.startswith("PICKED:"):
                    return line[7:].replace("/", "\\") or ""
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("tkinter-діалог: %s", e)
        ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; Add-Type -AssemblyName System.Windows.Forms; "
              "$d=New-Object System.Windows.Forms.FolderBrowserDialog; $d.ShowNewFolderButton=$true; "
              "$d.Description='Оберіть папку з готовими шортсами'; "
              f"$d.SelectedPath='{(initial or '').replace(chr(39), chr(39) * 2)}'; "
              "$f=New-Object System.Windows.Forms.Form -Property @{TopMost=$true}; "
              "if($d.ShowDialog($f) -eq 'OK'){'PICKED:'+$d.SelectedPath}else{'PICKED:'}")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=900, creationflags=flags)
            for line in r.stdout.splitlines():
                if line.startswith("PICKED:"):
                    return line[7:]
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("PowerShell-діалог: %s", e)
        return None
