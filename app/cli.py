"""poster — автопублікація шортсів на YouTube / TikTok / Instagram / Facebook
з підтвердженням через Telegram.

    python poster.py                 запустити службу (бот + розклад)
    python poster.py check           перевірити налаштування і доступи
    python poster.py auth youtube    авторизація YouTube (OAuth у браузері)
    python poster.py auth tiktok     авторизація TikTok
    python poster.py auth meta       токен сторінки Facebook + Instagram
    python poster.py queue           показати чергу
    python poster.py test-slot 10    позаплановий слот через 10 хв (для тесту)"""
from __future__ import annotations

from __future__ import annotations
import argparse
import html
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path
from app.core.config import NAMES, ConfigError, enabled_platforms, fmt_slot, load_config, now_ts, setup_logging
from app.core.db import DB
from app.platforms import get_class
from app.engine.runtime import App

log = logging.getLogger("poster")
esc = html.escape


# ======================================================================
def restart_after_update(cfg: dict, lock) -> None:
    """Новий процес бота (з новим кодом) + перезапуск панелі, якщо вона відкрита."""
    from app.system import control
    from app.system import updater
    from app import ROOT as root
    log.info("Перезапуск після оновлення")
    try:
        from app.channels import manager as channels
        channels.restart_running(cfg)  # додаткові канали (EN) — теж на новий код
    except Exception as e:  # noqa: BLE001
        log.warning("Перезапуск каналів: %s", e)
    try:
        lock.close()
    except OSError:
        pass
    updater.spawn([str(root / "poster.py"), "--config", cfg["config_path"], "run", "--wait-lock", "60"])
    port = int(cfg.get("panel_port") or 8770)
    if control.is_running(port):
        updater.spawn([str(root / "panel.py"), "--config", cfg["config_path"], "--no-browser"])


def wait_new_boot(db, old_version: str, timeout: int = 120) -> bool:
    """Чекаємо, поки новий процес запишеться в базу «я запустився» (db — база або кілька баз)."""
    dbs = db if isinstance(db, (list, tuple)) else [db]
    deadline = time.time() + timeout
    while time.time() < deadline:
        for d in dbs:
            boot = d.get("boot_ok") or ""
            if boot and not boot.startswith(old_version + "|"):
                return True
        time.sleep(2)
    return False


def single_instance(port: int) -> socket.socket | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s
    except OSError:
        return None


def run_service(cfg: dict, wait_lock: int = 0) -> int:
    lock = single_instance(int(cfg["lock_port"]))
    deadline = time.time() + wait_lock
    while lock is None and time.time() < deadline:  # після оновлення старий процес ще завершується
        time.sleep(1)
        lock = single_instance(int(cfg["lock_port"]))
    if lock is None:
        log.error("poster уже запущено (порт %s зайнятий) — виходжу", cfg["lock_port"])
        return 1
    try:
        app = App(cfg)
    except ConfigError as e:
        log.error("Бот не запущено: %s", e)
        return 2
    app.db.set("pid", os.getpid())
    app.bot.setup_commands()
    app.bot.start_panel_worker()
    from app.channels import manager as channels
    from app.system import control
    threading.Thread(target=control.ensure_shortcut_once, args=(app.db,), daemon=True).start()
    threading.Thread(target=channels.autostart_all, args=(cfg,), daemon=True).start()
    channels.publish_paths(cfg)  # щоб «Різак» сам знав, куди класти шортси для кожного каналу
    threading.Thread(target=channels.refresh_panels, args=(cfg,), daemon=True).start()  # панелі каналів — на новий код
    if app.db.get("captions_v") != "2":  # один раз: підписи вже надісланих відео — у новому вигляді й з актуальним станом
        app.db.set("captions_v", "2")
        for it in app.db.items("posted_at > ? OR status IN ('notified','approved','failed')", (now_ts() - 7 * 86400,),
                               order="id"):
            try:
                app.bot.refresh(it["id"])
            except Exception as e:  # noqa: BLE001
                log.debug("refresh #%s: %s", it["id"], e)
    if app.db.get("chat_cleaned") != "5":  # один раз після оновлення — прибрати старе «сміття» з чату
        app.db.set("chat_cleaned", "5")
        for cid in app.bot.recipients():
            try:
                app.bot.clean_chat(cid)
            except Exception as e:  # noqa: BLE001
                log.warning("Прибирання чату %s: %s", cid, e)
    try:
        app.bot.ensure_keyboard()  # кнопки внизу (у т.ч. «⬆️ Оновити») — завжди на місці
        if app.db.get("stopped") == "1":
            app.bot.refresh_publishing()   # після перезапуску під «Стопом» — картки показують «зупинено», не «публікується»
    except Exception as e:  # noqa: BLE001
        log.warning("Кнопки: %s", e)
    from app.core.config import VERSION
    log.info("Версія %s", VERSION)
    app.db.set("boot_ok", f"{VERSION}|{time.time()}")  # для процесу, що перезапускає нас після оновлення
    try:
        from app.system import updater
        updater.cleanup_legacy(cfg)          # старі модулі з кореня → data/versions/legacy-root (одноразово)
        from app.core import layout
        if layout.MIGRATED_FROM and layout.MIGRATED_FROM.exists():   # процес, що нас запустив, чекає на старій базі
            DB(layout.MIGRATED_FROM).set("boot_ok", f"{VERSION}|{time.time()}")
        pinned = (cfg.get("updates") or {}).get("pinned")
        if pinned and pinned != VERSION:     # після ручного відкату вручну оновились — автооновлення знову вмикаємо
            updater.set_auto_paused(cfg, None)
    except Exception as e:  # noqa: BLE001
        log.warning("Прибирання після оновлення: %s", e)
    log.info("poster запущено. Платформи: %s. dry_run=%s", list(app.platforms), cfg.get("dry_run"))
    app.scheduler.scan()
    app.scheduler.ensure_slots(now_ts())
    app.db.set("control", None)
    nxt = app.scheduler.upcoming(now_ts(), 1)
    paused = " ⏸ Розклад на паузі." if app.db.get("paused") == "1" else ""
    log.info("У черзі: %s. Найближчий слот: %s.%s", len(app.db.queue()),  # у Telegram не пишемо — там лише відео
             fmt_slot(cfg, nxt[0]["slot_at"]) if nxt else "—", paused)
    last_tick = 0.0
    errors = 0

    def stop_requested() -> bool:
        ctl = app.db.get("control")
        if ctl == "restart":
            nonlocal lock
            app.db.set("control", None)
            from app.core.config import VERSION as old_version
            app.db.set("boot_ok", f"{old_version}|{time.time()}")
            restart_after_update(cfg, lock)
            from app.core import layout
            dbs = [app.db] + ([DB(layout.UNMIGRATED_TO)] if layout.UNMIGRATED_TO else [])  # стара версія — база в корені
            if wait_new_boot(dbs, old_version):
                log.info("Нова версія запустилась — цей процес завершується")
                return True
            # Нова версія не стартувала за 2 хв: повертаємо попередні файли і працюємо далі самі (старий код у пам'яті)
            relock = single_instance(int(cfg["lock_port"]))
            if relock is None:  # новий процес тримає замок — він живий, просто довго стартує; виходимо
                return True
            lock = relock
            log.error("Нова версія не запустилась за 2 хв — відкат до %s", old_version)
            from app.system import updater
            try:
                updater.rollback(cfg, old_version)
                note = f"файли повернуто до {old_version}"
            except Exception as e:  # noqa: BLE001
                note = f"відкат не вдався: {e}"
            app.db.set("update_error", f"нова версія не запустилась; {note}")
            app.bot.send(f"⚠️ Оновлення не запустилось — {note}. Бот працює далі. Надішліть «📄 Журнал».")
            return False
        if ctl != "stop":
            return False
        app.db.set("control", None)
        log.info("Зупинка за командою з панелі керування")
        return True

    while True:
        try:
            if stop_requested():
                return 0
            if time.time() - last_tick >= 15:
                last_tick = time.time()
                app.scheduler.tick()
            app.bot.poll_once(timeout=10)
            errors = 0
        except KeyboardInterrupt:
            log.info("Зупинено користувачем")
            return 0
        except Exception as e:  # noqa: BLE001
            errors += 1
            if errors <= 3 or errors % 20 == 0:
                log.exception("Помилка в головному циклі: %s", e)
            else:
                log.warning("Помилка в головному циклі (%s-й раз поспіль): %s", errors, e)
            for _ in range(min(60, 5 * errors)):  # пауза, але команду зупинки помічаємо щосекунди
                time.sleep(1)
                if app.db.get("control") == "stop":
                    break


def cmd_check(cfg: dict) -> int:
    ok = True

    def line(mark, text):
        print(f" {mark} {text}")

    print("Налаштування:", cfg["config_path"])
    for key in ("ready", "posted", "skipped"):
        line("•", f"{key}: {cfg['paths'][key]}")
    line("•", f"Слоти: {', '.join(cfg['slots'])} ({cfg['timezone']})")
    from app.core import media
    try:
        r = media.run_hidden([cfg["ffmpeg"], "-version"], timeout=30)
        line("✅", "ffmpeg: " + r.stdout.decode(errors="ignore").splitlines()[0][:60])
    except media.MediaError as e:
        line("⚠️", f"ffmpeg: {e} (потрібен лише для файлів > 50 МБ)")
    try:
        from app.telegram.api import TelegramAPI
        me = TelegramAPI(cfg["secrets"]["TELEGRAM_BOT_TOKEN"]).call("getMe")
        line("✅", f"Telegram: @{me['username']}, chat_id={cfg['secrets']['TELEGRAM_CHAT_ID'] or '— НЕ ЗАДАНО'}")
        if not cfg["secrets"]["TELEGRAM_CHAT_ID"]:
            ok = False
    except Exception as e:  # noqa: BLE001
        ok = False
        line("❌", f"Telegram: {e}")
    for key in enabled_platforms(cfg):
        try:
            info = get_class(key)(cfg).check()
            line("✅", f"{NAMES[key]}: {info}")
        except Exception as e:  # noqa: BLE001
            ok = False
            line("❌", f"{NAMES[key]}: {type(e).__name__}: {e}")
    if cfg.get("dry_run"):
        line("🧪", "dry_run увімкнено — публікації імітуються")
    return 0 if ok else 2


def cmd_tg_test(cfg: dict) -> int:
    """Перевіряє токен і chat_id; якщо бот ще не може писати — відкриває його в Telegram."""
    import webbrowser

    from app.telegram.api import TelegramAPI, TelegramError

    token, chat = cfg["secrets"]["TELEGRAM_BOT_TOKEN"], cfg["secrets"]["TELEGRAM_CHAT_ID"]
    if not token or not chat:
        print("❌ У .env не заповнено TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID")
        return 1
    if token.split(":")[0] == chat:
        print("❌ TELEGRAM_CHAT_ID — це id самого бота. Потрібен ВАШ id (напишіть @userinfobot)")
        return 1
    try:
        tg = TelegramAPI(token)
        me = tg.call("getMe")
    except TelegramError as e:
        print(f"❌ Токен не підходить або немає інтернету: {e}")
        return 1
    link = f"https://t.me/{me['username']}"
    print(f"✅ Бот: @{me['username']} ({link})")
    try:
        tg.send_message(int(chat), "✅ Зв'язок із poster налаштовано. Бот працює!", html=False)
    except (TelegramError, ValueError) as e:
        print(f"❌ Бот не може написати вам: {e}")
        print("   Відкриваю бота в Telegram — натисніть там «Start» / «Почати».")
        webbrowser.open(link)
        return 1
    print("✅ Тестове повідомлення надіслано — перевірте Telegram.")
    return 0


def cmd_mode(cfg: dict, mode: str) -> int:
    import re

    path = Path(cfg["config_path"])
    text = path.read_text(encoding="utf-8")
    value = "true" if mode == "test" else "false"
    new, n = re.subn(r'("dry_run"\s*:\s*)(true|false)', rf"\g<1>{value}", text)
    if not n:
        import json
        data = json.loads(text)
        data["dry_run"] = mode == "test"
        new = json.dumps(data, ensure_ascii=False, indent=2)
    path.write_text(new, encoding="utf-8")
    print("🧪 Тестовий режим: публікації імітуються." if mode == "test"
          else "🚀 Бойовий режим: шортси публікуються по-справжньому.")
    print("   Перезапустіть бота, щоб зміна набула чинності.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Автопублікація шортсів")
    ap.add_argument("--config", help="шлях до config.json")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--wait-lock", type=int, default=0, help=argparse.SUPPRESS)
    sub.add_parser("check")
    sub.add_parser("queue")
    a = sub.add_parser("auth")
    a.add_argument("platform", choices=["youtube", "tiktok", "meta", "instagram", "facebook"])
    t = sub.add_parser("test-slot")
    t.add_argument("minutes", type=int, nargs="?", default=10)
    sub.add_parser("panel-reset", help="скинути пароль панелі керування")
    sub.add_parser("panel", help="відкрити панель керування в браузері")
    sub.add_parser("tg-test", help="перевірити зв'язок із ботом і надіслати тестове повідомлення")
    m = sub.add_parser("mode", help="test — імітація публікацій (dry_run), live — справжні публікації")
    m.add_argument("mode", choices=["test", "live"])
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # українські тексти в консолі Windows
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print("Помилка налаштувань:", e, file=sys.stderr)
        return 2
    setup_logging(cfg, args.verbose)
    os.chdir(cfg["root"])

    cmd = args.cmd or "run"
    if cmd == "tg-test":
        return cmd_tg_test(cfg)
    if cmd == "panel":
        from app.web import panel
        return panel.serve(args.config)
    if cmd == "panel-reset":
        DB(Path(cfg["paths"]["data"]) / "poster.sqlite3").set("panel_pw", None)
        print("Пароль панелі скинуто — при наступному відкритті створіть новий.")
        return 0
    if cmd == "mode":
        return cmd_mode(cfg, args.mode)
    if cmd == "run":
        return run_service(cfg, getattr(args, "wait_lock", 0) or 0)
    if cmd == "check":
        return cmd_check(cfg)
    if cmd == "auth":
        if args.platform in ("meta", "instagram", "facebook"):
            from app.platforms.meta_graph import authorize_meta
            authorize_meta(cfg)
        else:
            get_class(args.platform)(cfg).authorize()
        return 0

    class _NoBot:  # для CLI-команд без Telegram
        def __getattr__(self, name):
            return lambda *a, **k: None

    try:
        app = App(cfg, bot=_NoBot())
    except ConfigError as e:
        print("Помилка налаштувань:", e, file=sys.stderr)
        return 2
    if cmd == "queue":
        app.scheduler.scan()
        app.scheduler.ensure_slots(now_ts())
        import re
        print(re.sub(r"<[^>]+>", "", app.queue_text()))
        print(re.sub(r"<[^>]+>", "", app.status_text()))
    elif cmd == "test-slot":
        ts = app.scheduler.add_manual_slot(args.minutes)
        print(f"Додано позаплановий слот о {fmt_slot(cfg, ts)}. Запущена служба надішле сповіщення протягом хвилини.")
    return 0
