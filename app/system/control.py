"""Керування службою poster: чи працює, запуск у фоні, зупинка, автозапуск Windows."""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from app import ROOT  # noqa: E402  папка програми
WIN = sys.platform == "win32"
TASK_NAME = "ShortsPoster"


_PS_LOCK = threading.Lock()          # одночасно — не більше одного PowerShell з цього процесу
_OWNER_CACHE: dict[int, tuple[float, dict | None]] = {}


def spawn_allowed(name: str, every: int) -> bool:
    """Запобіжник від «лавини» процесів: той самий запуск (панель на порту, бот) — не частіше ніж раз на every с,
    навіть якщо його просять кілька процесів одночасно. Позначка — файл у TEMP (спільна для всіх копій)."""
    stamp = Path(tempfile.gettempdir()) / f"poster-spawn-{re.sub(r'[^A-Za-z0-9_-]', '_', name)}"
    try:
        if time.time() - stamp.stat().st_mtime < every:
            return False
    except OSError:
        pass
    try:
        stamp.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    return True


def powershell(ps: str, timeout: int = 30, bypass: bool = False) -> subprocess.CompletedProcess:
    """PowerShell без вікна, по одному за раз (кожен запуск — сотні МБ пам'яті й секунди процесора)."""
    args = ["powershell", "-NoProfile"] + (["-ExecutionPolicy", "Bypass"] if bypass else []) + ["-Command", ps]
    with _PS_LOCK:
        return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


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


def start_bot(port: int, window: bool = False, wait: int = 30, config: str | None = None) -> bool:
    """config — шлях до config.json (для додаткового каналу); без нього — основний config.json."""
    if is_running(port):
        return True
    if not window and not spawn_allowed(f"bot-{port}", 90):   # хтось уже запускає цього бота
        for _ in range(wait * 2):
            time.sleep(0.5)
            if is_running(port):
                return True
        return False
    args = [python_exe(windowless=not window), str(ROOT / "poster.py")]
    if config:
        args += ["--config", str(config)]
    args.append("run")
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


def spawn_panel(config: str, port: int) -> None:
    """Панель керування для іншого config.json (додатковий канал) — у фоні, без відкриття браузера."""
    if not spawn_allowed(f"panel-{int(port)}", 180):
        return   # цю панель уже запускали щойно — не плодимо копії
    args = [python_exe(windowless=True), str(ROOT / "panel.py"), "--config", str(config), "--port", str(int(port)),
            "--no-browser"]
    if WIN:
        subprocess.Popen(args, cwd=ROOT, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
                         close_fds=True)
    else:
        subprocess.Popen(args, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(40):
        time.sleep(0.25)
        if is_running(port):
            return


def port_owner(port: int) -> dict | None:
    """Який процес тримає порт-замок бота: {'pid', 'cmd'} (щоб знайти бота, навіть запущеного з іншої папки).
    Результат кешується на 30 с — на Windows це запуск PowerShell."""
    hit = _OWNER_CACHE.get(int(port))
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    res = _port_owner(port)
    _OWNER_CACHE[int(port)] = (time.time(), res)
    return res


def _port_owner(port: int) -> dict | None:
    try:
        if WIN:
            ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                  f"$c = Get-NetTCPConnection -LocalPort {int(port)} -State Listen -ErrorAction SilentlyContinue | "
                  "Select-Object -First 1; if ($c) { $p = Get-CimInstance Win32_Process -Filter "
                  "\"ProcessId=$($c.OwningProcess)\"; \"$($c.OwningProcess)|$($p.CommandLine)\" }")
            r = powershell(ps)
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


SHORTCUT_NAME = "Poster — панель керування"


def shortcut_script() -> str:
    """PowerShell: ярлик панелі на робочому столі (і в меню «Пуск») — без вікна консолі, з іконкою."""
    q = lambda x: "'" + str(x).replace("'", "''") + "'"  # noqa: E731
    target, icon = python_exe(windowless=True), ROOT / "poster.ico"
    return ("$ws = New-Object -ComObject WScript.Shell; "
            "foreach ($dir in @([Environment]::GetFolderPath('Desktop'), "
            "(Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'))) { "
            f"$s = $ws.CreateShortcut((Join-Path $dir {q(SHORTCUT_NAME + '.lnk')})); "
            f"$s.TargetPath = {q(target)}; $s.Arguments = {q(chr(34) + str(ROOT / 'panel.py') + chr(34))}; "
            f"$s.WorkingDirectory = {q(ROOT)}; $s.IconLocation = {q(str(icon) + ',0')}; "
            "$s.Description = 'Автопублікація шортсів: панель керування'; $s.Save() }; "
            "[Environment]::GetFolderPath('Desktop')")


def create_shortcut() -> str:
    """Створює ярлик; повертає шлях до робочого столу. Лише Windows."""
    if not WIN:
        raise RuntimeError("Ярлик створюється лише у Windows")
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + shortcut_script()],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[-300:])
    return (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else ""


def shortcut_exists() -> bool | None:
    if not WIN:
        return None
    import os as _os
    desk = Path(_os.path.expandvars(r"%USERPROFILE%")) / "Desktop"
    cands = [desk, Path(_os.path.expandvars(r"%USERPROFILE%")) / "OneDrive" / "Desktop",
             Path(_os.environ.get("OneDrive", "")) / "Desktop"]
    return any((c / f"{SHORTCUT_NAME}.lnk").exists() for c in cands if str(c))


def ensure_shortcut_once(db) -> None:
    """Один раз (після встановлення/оновлення) — створити ярлик. Якщо ви його видалите, сам не повернеться."""
    if not WIN or db.get("shortcut_done"):
        return
    db.set("shortcut_done", "1")
    try:
        if not shortcut_exists():
            create_shortcut()
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger("control").warning("Ярлик панелі: %s", e)


def kill_other_panels(port: int | None = None) -> int:
    """Закриває інші копії панелі (panel.py), як panel.bat. port — лише ту, що на цьому порту
    (основна панель — без --port або з --port 8770). Повертає, скільки процесів зупинено."""
    me = os.getpid()
    pids = []
    if not spawn_allowed(f"killpanels-{port}", 60):   # пошук через WMI важкий — не частіше ніж раз на хвилину
        return 0
    if WIN:
        ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
              "Where-Object { $_.CommandLine -like '*panel.py*' } | ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }")
        try:
            r = powershell(ps)
            rows = [ln.split("|", 1) for ln in r.stdout.splitlines() if "|" in ln]
        except (OSError, subprocess.SubprocessError):
            rows = []
    else:
        rows = []
        for d in Path("/proc").glob("[0-9]*"):
            try:
                cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            except OSError:
                continue
            if "panel.py" in cmd:
                rows.append((d.name, cmd))
    for pid_s, cmd in rows:
        try:
            pid = int(pid_s.strip())
        except ValueError:
            continue
        if pid == me:
            continue
        if port is not None:
            m = re.search(r"--port\s+(\d+)", cmd)
            their = int(m.group(1)) if m else 8770
            if their != int(port):
                continue
        pids.append(pid)
    for pid in pids:
        kill(pid)
    return len(pids)
