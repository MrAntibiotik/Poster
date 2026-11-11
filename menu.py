"""Меню керування poster (запускається з menu.bat)."""
from __future__ import annotations

import glob
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
WIN = sys.platform == "win32"

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass


def poster(*args) -> int:
    return subprocess.call([PY, str(ROOT / "poster.py"), *args], cwd=ROOT)


def cfg_raw() -> dict:
    try:
        return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def is_running() -> bool:
    port = int(cfg_raw().get("lock_port", 47613))
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def pause(msg: str = "Натисніть Enter, щоб продовжити…") -> None:
    try:
        input(msg)
    except EOFError:
        pass


def start_bot() -> bool:
    if is_running():
        print("✅ Бот уже працює.")
        return True
    print("Запускаю бота в окремому вікні (не закривайте його)…")
    kw = {"creationflags": subprocess.CREATE_NEW_CONSOLE} if WIN else {}
    subprocess.Popen([PY, str(ROOT / "poster.py"), "run"], cwd=ROOT, **kw)
    for _ in range(30):
        time.sleep(1)
        if is_running():
            print("✅ Бот запущено — у Telegram має прийти «🟢 poster запущено».")
            return True
    print("❌ Бот не запустився. Подивіться log.txt (пункт 9).")
    return False


# ---------------------------------------------------------------- дії
def first_run() -> None:
    print("\n=== Крок 1/3. Зв'язок із Telegram ===")
    while poster("tg-test") != 0:
        print("\nЯкщо відкрився Telegram — натисніть у боті «Start», потім поверніться сюди.")
        ans = input("Enter — перевірити ще раз, q — вийти в меню: ").strip().lower()
        if ans == "q":
            return
    print("\n=== Крок 2/3. Запуск бота ===")
    if not start_bot():
        return
    print("\n=== Крок 3/3. Тестовий шортс ===")
    ready = Path(cfg_raw().get("paths", {}).get("ready", "ready"))
    ready = ready if ready.is_absolute() else ROOT / ready
    if not list(ready.glob("*.mp4")) + list(ready.glob("*/*.mp4")):
        print(f"⚠️ У {ready} немає шортсів. Покладіть mp4 + json у {ready}\\<Гра>\\ і повторіть пункт 3.")
        return
    time.sleep(3)
    poster("test-slot", "10")
    mode = "тестовий (нічого реально не публікується)" if cfg_raw().get("dry_run") else "БОЙОВИЙ — публікація справжня!"
    print(f"\n👉 Протягом 1–2 хв у Telegram прийде шортс із кнопками. Натисніть «🚀 Опублікувати».\n"
          f"   Через 10 хв підпис зміниться на «✅ Опубліковано». Режим: {mode}")


def auth_menu() -> None:
    print("\n  1. YouTube   (відкриється браузер — увійдіть в акаунт каналу)\n"
          "  2. Facebook + Instagram   (потрібен токен із Graph API Explorer, README 4.3)\n"
          "  3. TikTok    (відкриється браузер)\n  0. Назад")
    choice = input("Ваш вибір: ").strip()
    target = {"1": "youtube", "2": "meta", "3": "tiktok"}.get(choice)
    if target:
        if target == "youtube" and not (ROOT / "client_secret.json").exists():
            print("❌ Немає client_secret.json поруч із poster.py — див. README, розділ 4.2.")
            return
        poster("auth", target)
        print("Перезапустіть бота, щоб він підхопив новий токен (або дочекайтесь щоденної перевірки).")


def mode_menu() -> None:
    cur = "тестовий" if cfg_raw().get("dry_run") else "бойовий"
    print(f"\nЗараз: {cur} режим.\n  1. Тестовий (імітація публікацій)\n  2. Бойовий (справжні публікації)\n  0. Назад")
    choice = input("Ваш вибір: ").strip()
    if choice in ("1", "2"):
        if choice == "2":
            print("Переконайтесь, що пункт 6 «Перевірити підключення» показує ✅ для потрібних платформ.")
            if input("Увімкнути бойовий режим? (так/ні): ").strip().lower() not in ("так", "т", "y", "yes"):
                return
        poster("mode", "test" if choice == "1" else "live")
        if is_running():
            print("⚠️ Бот уже працює — закрийте його вікно і запустіть знову (пункт 2), щоб режим змінився.")


def autostart_menu() -> None:
    print("\n  1. Увімкнути автозапуск з Windows (у фоні, без вікна)\n  2. Вимкнути автозапуск\n  0. Назад")
    choice = input("Ваш вибір: ").strip()
    script = {"1": "install_task.ps1", "2": "uninstall_task.ps1"}.get(choice)
    if not script:
        return
    if choice == "1" and is_running():
        print("⚠️ Спершу закрийте вікно бота — інакше фонова копія не запуститься (працює лише одна).")
        pause()
    subprocess.call(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / script)], cwd=ROOT)


def find_ffmpeg() -> tuple[str, str] | None:
    local = os.environ.get("LOCALAPPDATA", "")
    for base in (os.path.join(local, "Microsoft", "WinGet", "Links"),
                 *glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "*", "bin")),
                 r"C:\ffmpeg\bin"):
        ff, fp = os.path.join(base, "ffmpeg.exe"), os.path.join(base, "ffprobe.exe")
        if os.path.exists(ff) and os.path.exists(fp):
            return ff, fp
    return None


def install_ffmpeg() -> None:
    found = find_ffmpeg()
    if not found:
        print("Встановлюю ffmpeg через winget…")
        subprocess.call(["winget", "install", "-e", "--id", "Gyan.FFmpeg",
                         "--accept-package-agreements", "--accept-source-agreements"])
        found = find_ffmpeg()
    if not found:
        print("❌ ffmpeg не знайдено. Встановіть вручну (README, розділ 2) і пропишіть шлях у config.json.")
        return
    ff, fp = found
    path = ROOT / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["ffmpeg"], data["ffprobe"] = ff.replace("\\", "/"), fp.replace("\\", "/")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ ffmpeg: {ff}\n   Шлях записано в config.json. Перезапустіть бота.")


def open_file(name: str) -> None:
    p = ROOT / name
    if not p.exists():
        print(f"Файлу {name} ще немає.")
        return
    if WIN:
        subprocess.Popen(["notepad.exe", str(p)])
    else:
        print(p.read_text(encoding="utf-8", errors="replace")[-3000:])


def files_menu() -> None:
    print("\n  1. log.txt (журнал)\n  2. config.json (налаштування)\n  3. .env (ключі)\n  0. Назад")
    name = {"1": "log.txt", "2": "config.json", "3": ".env"}.get(input("Ваш вибір: ").strip())
    if name:
        open_file(name)


MENU = [
    ("1", "🚀 Перший запуск (перевірка Telegram → запуск бота → тестовий шортс)", first_run),
    ("2", "▶️  Запустити бота", start_bot),
    ("3", "🧪 Тестовий слот через 10 хв", lambda: poster("test-slot", "10")),
    ("4", "📋 Черга і найближчі слоти", lambda: poster("queue")),
    ("5", "🔑 Авторизація платформ (YouTube / Meta / TikTok)", auth_menu),
    ("6", "🔍 Перевірити всі підключення", lambda: poster("check")),
    ("7", "⚙️  Режим: тестовий / бойовий", mode_menu),
    ("8", "🔁 Автозапуск з Windows", autostart_menu),
    ("9", "📄 Відкрити журнал / налаштування / ключі", files_menu),
    ("10", "🎞  Встановити ffmpeg (для відео > 50 МБ)", install_ffmpeg),
]


def main() -> None:
    os.chdir(ROOT)
    while True:
        state = "працює ✅" if is_running() else "не запущено"
        raw = cfg_raw()
        mode = "немає config.json" if not raw else ("тестовий 🧪" if raw.get("dry_run") else "бойовий 🚀")
        print(f"\n========== poster | бот: {state} | режим: {mode} ==========")
        for key, title, _ in MENU:
            print(f" {key:>2}. {title}")
        print("  0. Вихід")
        choice = input("Ваш вибір: ").strip()
        if choice in ("0", "q", ""):
            if choice == "":
                continue
            return
        action = next((fn for key, _, fn in MENU if key == choice), None)
        if not action:
            continue
        try:
            action()
        except KeyboardInterrupt:
            print("\nПерервано.")
        except Exception as e:  # noqa: BLE001
            print(f"❌ Помилка: {e}")
        pause()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        pass
