"""Сон між слотами (Windows): ПК прокидається за кілька хвилин до публікації й засинає, коли роботи немає.

Пробудження — завдання Планувальника Windows «ShortsPoster-Wake» з прапорцем «Будити комп'ютер».
Засинання — лише коли ви не користуєтесь ПК (немає натискань клавіш/миші) і бот нічого не чекає.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from datetime import datetime, timezone

log = logging.getLogger("power")
WIN = sys.platform == "win32"
from app import ROOT  # noqa: E402  папка програми
TASK = "ShortsPoster-Wake"


def idle_seconds() -> float:
    """Скільки секунд немає натискань клавіатури/миші (не Windows — «дуже довго»)."""
    if not WIN:
        return 10 ** 9
    import ctypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0


_proc_cache: tuple[float, set[str]] = (0.0, set())


def running_processes() -> set[str]:
    """Імена запущених програм (у нижньому регістрі), кеш 60 с."""
    import time
    global _proc_cache
    if not WIN:
        return set()
    if time.time() - _proc_cache[0] < 60:
        return _proc_cache[1]
    try:
        r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, errors="replace",
                           timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        names = {line.split('","')[0].strip('"').lower() for line in r.stdout.splitlines() if line.startswith('"')}
    except Exception:  # noqa: BLE001
        names = set()
    _proc_cache = (time.time(), names)
    return names


def keep_awake(on: bool) -> None:
    """Поки бот чимось зайнятий — Windows не засне сама (екран може згаснути)."""
    if not WIN:
        return
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


def sleep_now() -> bool:
    """Звичайний сон (не гібернація і не вимкнення)."""
    if not WIN:
        log.info("Сон підтримується лише у Windows")
        return False
    import ctypes
    return bool(ctypes.windll.powrprof.SetSuspendState(False, False, False))


def _ps(command: str, timeout: int = 60) -> subprocess.CompletedProcess:
    cmd = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ErrorActionPreference='Stop'; " + command
    from app.system.control import powershell
    return powershell(cmd, timeout=timeout, bypass=True)


def wake_script(times: list[int], pythonw: str) -> str:
    stamps = ",".join(f"'{datetime.fromtimestamp(t, timezone.utc):%Y-%m-%dT%H:%M:%SZ}'" for t in times)
    q = lambda s: "'" + str(s).replace("'", "''") + "'"  # noqa: E731
    return (f"$tr = @(); foreach ($s in @({stamps})) {{ $tr += New-ScheduledTaskTrigger -Once "
            f"-At ([DateTime]::Parse($s).ToUniversalTime().ToLocalTime()) }}; "
            f"$a = New-ScheduledTaskAction -Execute {q(pythonw)} -Argument {q(chr(34) + str(ROOT / 'poster.py') + chr(34) + ' run')} "
            f"-WorkingDirectory {q(ROOT)}; "
            "$st = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
            "-StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew; "
            "$p = New-ScheduledTaskPrincipal -UserId \"$env:USERDOMAIN\\$env:USERNAME\" -LogonType Interactive -RunLevel Limited; "
            f"Register-ScheduledTask -TaskName '{TASK}' -Action $a -Trigger $tr -Settings $st -Principal $p "
            "-Description 'poster: пробудження ПК перед публікацією' -Force | Out-Null")


def set_wake_times(times: list[int]) -> None:
    """Записує найближчі моменти пробудження (до 20) у Планувальник Windows."""
    if not WIN or not times:
        return
    from app.system.control import python_exe
    r = _ps(wake_script(sorted(set(times))[:20], python_exe(windowless=True)))
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[-400:])


def remove_wake_task() -> None:
    if WIN:
        _ps(f"Unregister-ScheduledTask -TaskName '{TASK}' -Confirm:$false -ErrorAction SilentlyContinue")


def allow_wake_timers() -> str:
    """Дозволяє таймери пробудження в поточній схемі живлення (від мережі й від батареї)."""
    if not WIN:
        return "Лише у Windows"
    r = _ps("powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1; "
            "powercfg /SETDCVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1; powercfg /SETACTIVE SCHEME_CURRENT")
    if r.returncode:
        raise RuntimeError("Не вдалося змінити схему живлення: " + (r.stderr or r.stdout).strip()[-300:] +
                           "\nЗробіть вручну: Панель керування → Електроживлення → Змінити параметри схеми → "
                           "Додаткові параметри → Сон → Дозволити таймери пробудження → Увімкнути.")
    return "Таймери пробудження дозволено"
