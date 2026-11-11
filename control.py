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


def port_owner(port: int) -> dict | None:
    """Який процес тримає порт-замок бота: {'pid', 'cmd'} (щоб знайти бота, навіть запущеного з іншої папки)."""
    try:
        if WIN:
            ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                  f"$c = Get-NetTCPConnection -LocalPort {int(port)} -State Listen -ErrorAction SilentlyContinue | "
                  "Select-Object -First 1; if ($c) { $p = Get-CimInstance Win32_Process -Filter "
                  "\"ProcessId=$($c.OwningProcess)\"; \"$($c.OwningProcess)|$($p.CommandLine)\" }")
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=30,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            line = (r.stdout or "").strip().splitlines()
            if not line or "|" not in line[-1]:
                return None
            pid, cmd = line[-1].split("|", 1)
            return {"pid": int(pid), "cmd": cmd.strip()}
        pid = _linux_port_pid(int(port))
        if not pid:
            return None
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            cmd = ""
        return {"pid": pid, "cmd": cmd}
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _linux_port_pid(port: int) -> int | None:
    """Linux/macOS-налагодження: процес, що слухає порт (через /proc, без зовнішніх утиліт)."""
    inode = None
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            for line in Path(table).read_text().splitlines()[1:]:
                f = line.split()
                if f[3] == "0A" and int(f[1].split(":")[1], 16) == port:  # 0A = LISTEN
                    inode = f[9]
                    break
        except OSError:
            continue
        if inode:
            break
    if not inode:
        return None
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            for fd in (proc / "fd").iterdir():
                if os.readlink(fd) == f"socket:[{inode}]":
                    return int(proc.name)
        except OSError:
            continue
    return None


def is_foreign(owner: dict | None) -> bool:
    """Бот запущено з іншої папки (інша копія програми)."""
    if not owner or not owner.get("cmd"):
        return False
    return str(ROOT).lower() not in owner["cmd"].lower().replace("/", "\\" if WIN else "/")


def stop_bot(db, port: int, wait: int = 15) -> bool:
    """Спершу м'яко (прапорець у БД), потім примусово: за PID із БД, а якщо не допомогло —
    процес, що тримає порт (так зупиняється і бот, запущений з іншої папки)."""
    if not is_running(port):
        return True
    owner = port_owner(port)
    if not is_foreign(owner):
        db.set("control", "stop")
        for _ in range(wait * 2):
            time.sleep(0.5)
            if not is_running(port):
                return True
    for pid in (int(db.get("pid") or 0), (owner or {}).get("pid") or 0, (port_owner(port) or {}).get("pid") or 0):
        if not pid or pid == os.getpid():
            continue
        kill(pid)
        for _ in range(16):
            time.sleep(0.5)
            if not is_running(port):
                db.set("control", None)
                return True
    return not is_running(port)


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
