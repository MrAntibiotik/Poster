"""Керування службою poster: чи працює, запуск у фоні, зупинка, автозапуск Windows."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WIN = sys.platform == "win32"
TASK_NAME = "ShortsPoster"


def is_running(port: int) -> bool:
    """Служба тримає локальний порт-замок; якщо порт зайнятий — вона працює."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", int(port)))
        return False
    except OSError:
        return True
    finally:
        s.close()


def python_exe(windowless: bool = True) -> str:
    venv = ROOT / ".venv" / "Scripts" / ("pythonw.exe" if windowless else "python.exe")
    if WIN and venv.exists():
        return str(venv)
    exe = Path(sys.executable)
    if WIN and windowless and exe.name.lower() == "python.exe" and (exe.parent / "pythonw.exe").exists():
        return str(exe.parent / "pythonw.exe")
    return sys.executable


def start_bot(port: int, window: bool = False, wait: int = 30) -> bool:
    if is_running(port):
        return True
    args = [python_exe(windowless=not window), str(ROOT / "poster.py"), "run"]
    if WIN:
        flags = subprocess.CREATE_NEW_CONSOLE if window else (subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)
        subprocess.Popen(args, cwd=ROOT, creationflags=flags, close_fds=True)
    else:
        subprocess.Popen(args, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(wait * 2):
        time.sleep(0.5)
        if is_running(port):
            return True
    return False


def stop_bot(db, port: int, wait: int = 20) -> bool:
    """Спершу м'яко (прапорець у БД, служба помічає його за кілька секунд), потім примусово за PID."""
    if not is_running(port):
        return True
    db.set("control", "stop")
    for _ in range(wait * 2):
        time.sleep(0.5)
        if not is_running(port):
            return True
    pid = int(db.get("pid") or 0)
    if pid:
        kill(pid)
        for _ in range(20):
            time.sleep(0.5)
            if not is_running(port):
                db.set("control", None)
                return True
    return False


def kill(pid: int) -> None:
    try:
        if WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass


def autostart_installed() -> bool | None:
    if not WIN:
        return None
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode == 0


def run_powershell(script: str) -> subprocess.CompletedProcess:
    cmd = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; $OutputEncoding=[Text.Encoding]::UTF8; "
           f"& '{str(ROOT / script).replace(chr(39), chr(39) * 2)}'")
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def open_path(path: str | os.PathLike) -> None:
    path = str(path)
    if WIN:
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
