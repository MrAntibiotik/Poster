"""poster — автопублікація шортсів на YouTube / TikTok / Instagram / Facebook
з підтвердженням через Telegram.

    python poster.py                 запустити службу (бот + розклад)
    python poster.py check           перевірити налаштування і доступи
    python poster.py auth youtube    авторизація YouTube (OAuth у браузері)
    python poster.py auth tiktok     авторизація TikTok
    python poster.py auth meta       токен сторінки Facebook + Instagram
    python poster.py queue           показати чергу
    python poster.py test-slot 10    позаплановий слот через 10 хв (для тесту)
"""
from __future__ import annotations

import argparse
import html
import logging
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import NAMES, ConfigError, enabled_platforms, ensure_dirs, fmt_slot, fmt_when, load_config, now_ts, setup_logging  # noqa: E402
from db import DB  # noqa: E402
from platforms import build_platforms, get_class  # noqa: E402

log = logging.getLogger("poster")
esc = html.escape


class App:
    def __init__(self, cfg: dict, bot=None):
        self.cfg = cfg
        ensure_dirs(cfg)
        self.db = DB(Path(cfg["paths"]["data"]) / "poster.sqlite3")
        self.platforms = build_platforms(cfg)
        from publisher import Publisher
        from scheduler import Scheduler

        self.publisher = Publisher(self)
        self.scheduler = Scheduler(self)
        if bot is None:
            from bot import Bot
            bot = Bot(self)
        self.bot = bot
        self._cfg_mtime = self._config_mtime()

    # ---------- налаштування «на льоту» ----------
    def _config_mtime(self) -> float:
        total = 0.0
        for p in (self.cfg.get("config_path"), self.cfg.get("env_path")):
            try:
                total += os.path.getmtime(p) if p else 0
            except OSError:
                pass
        return total

    def reload_config(self, force: bool = False) -> bool:
        """Підхоплює зміни config.json / .env без перезапуску (напр. з панелі). True — якщо перечитано."""
        m = self._config_mtime()
        if not force and m == self._cfg_mtime:
            return False
        try:
            new = load_config(self.cfg["config_path"])
        except ConfigError as e:
            log.error("Нові налаштування з помилкою — лишаю попередні: %s", e)
            self._cfg_mtime = m
            return False
        self.cfg.clear()
        self.cfg.update(new)  # той самий об'єкт — його бачать бот, планувальник, публікатор
        self.platforms = build_platforms(self.cfg)
        self._cfg_mtime = m
        log.info("Налаштування перечитано; платформи: %s", list(self.platforms))
        return True

    def set_platform_enabled(self, key: str, enabled: bool) -> None:
        import json
        path = Path(self.cfg["config_path"])
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.setdefault("platforms", {}).setdefault(key, {})["enabled"] = bool(enabled)
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        self.reload_config(force=True)

    # ---------- тексти для команд ----------
    def queue_text(self) -> str:
        now = now_ts()
        active = self.db.items("status IN ('notified','approved','publishing')", order="slot_at")
        queue = self.db.queue()
        lines = [f"📦 <b>Черга: {len(queue)}</b>"]
        st_names = {"notified": "⏳ чекає підтвердження", "approved": "👍 підтверджено", "publishing": "⏫ публікується"}
        for it in active:
            slot = fmt_slot(self.cfg, it["slot_at"], now) if it["slot_at"] else ""
            lines.append(f"▶️ #{it['id']} {esc(Path(it['mp4']).stem)} — {st_names[it['status']]} {slot}")
        plan = self.scheduler.projection(now)
        for n, (it, ts) in enumerate(plan[:25], 1):
            game = f"[{esc(it['game'])}] " if it["game"] else ""
            sk = f" (пропусків: {it['skips']})" if it["skips"] else ""
            when = f" → <b>{fmt_when(self.cfg, ts, now)}</b>" if ts else ""
            lines.append(f"{n}. #{it['id']} {game}{esc(Path(it['mp4']).stem)}{when}{sk}")
        if plan and plan[0][1] is None and self.db.get("paused") == "1":
            lines.append("⏸ Розклад на паузі — час не призначається")
        if len(queue) > 25:
            lines.append(f"… і ще {len(queue) - 25}")
        failed = self.db.items("status='failed'")
        if failed:
            lines.append("\n❌ Не опубліковано: " + ", ".join(f"#{i['id']}" for i in failed) + " (/retry N)")
        return "\n".join(lines)

    def status_text(self) -> str:
        now = now_ts()
        lines = ["🕐 <b>Найближчі слоти</b>"]
        kinds = {"notified": "⏳ чекає підтвердження", "approved": "👍 підтверджено", "publishing": "⏫ публікується",
                 "planned": "📦 з черги"}
        for s in self.scheduler.slot_plan(now, 6):
            src = " (тестовий)" if s["manual"] else ""
            it = s["item"]
            who = (f"#{it['id']} {esc(Path(it['mp4']).stem)} — {kinds.get(s['kind'], s['kind'])}" if it else
                   "⏸ пауза" if s["kind"] == "paused" else "порожньо (черга порожня)")
            lines.append(f"• {fmt_when(self.cfg, s['slot_at'], now)}{src}: {who}")
        if self.db.get("paused") == "1":
            lines.append("⏸ <b>Розклад на паузі</b> — нові шортси не надсилаються")
        lines.append(f"\n📦 У черзі: {len(self.db.queue())}")
        lines.append("Платформи: " + ", ".join(NAMES[p] for p in enabled_platforms(self.cfg)))
        tt = self.cfg["platforms"]["tiktok"]
        if tt.get("enabled"):
            lines.append("TikTok: " + ("публічно" if tt.get("public") else "приватно (SELF_ONLY)"))
        if self.db.get("health_ts"):
            lines.append(f"\n🔐 <b>Доступи</b> (перевірено {fmt_slot(self.cfg, int(self.db.get('health_ts')), now)}):")
            for p in self.platforms:
                lines.append(f"{NAMES[p]}: {esc(self.db.get(f'health_{p}') or '—')}")
        if self.cfg.get("dry_run"):
            lines.append("🧪 dry_run увімкнено")
        return "\n".join(lines)


# ======================================================================
def restart_after_update(cfg: dict, lock) -> None:
    """Новий процес бота (з новим кодом) + перезапуск панелі, якщо вона відкрита."""
    import control
    import updater
    root = Path(__file__).resolve().parent
    log.info("Перезапуск після оновлення")
    try:
        lock.close()
    except OSError:
        pass
    updater.spawn([str(root / "poster.py"), "--config", cfg["config_path"], "run", "--wait-lock", "60"])
    port = int(cfg.get("panel_port") or 8770)
    if control.is_running(port):
        updater.spawn([str(root / "panel.py"), "--config", cfg["config_path"], "--no-browser"])


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
    from config import VERSION
    log.info("Версія %s", VERSION)
    log.info("poster запущено. Платформи: %s. dry_run=%s", list(app.platforms), cfg.get("dry_run"))
    app.scheduler.scan()
    app.scheduler.ensure_slots(now_ts())
    app.db.set("control", None)
    nxt = app.scheduler.upcoming(now_ts(), 1)
    paused = " ⏸ Розклад на паузі." if app.db.get("paused") == "1" else ""
    app.bot.send(f"🟢 poster запущено. У черзі: {len(app.db.queue())}. Найближчий слот: "
                 f"{fmt_slot(cfg, nxt[0]['slot_at']) if nxt else '—'}.{paused}", keyboard=True)
    last_tick = 0.0
    errors = 0

    def stop_requested() -> bool:
        ctl = app.db.get("control")
        if ctl == "restart":
            app.db.set("control", None)
            restart_after_update(cfg, lock)
            return True
        if ctl != "stop":
            return False
        app.db.set("control", None)
        log.info("Зупинка за командою з панелі керування")
        app.bot.send("🔴 poster зупинено з панелі керування.")
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
    import media
    try:
        r = media.run_hidden([cfg["ffmpeg"], "-version"], timeout=30)
        line("✅", "ffmpeg: " + r.stdout.decode(errors="ignore").splitlines()[0][:60])
    except media.MediaError as e:
        line("⚠️", f"ffmpeg: {e} (потрібен лише для файлів > 50 МБ)")
    try:
        from telegram_api import TelegramAPI
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

    from telegram_api import TelegramAPI, TelegramError

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
        import panel
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
            from platforms.meta_graph import authorize_meta
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


if __name__ == "__main__":
    sys.exit(main())
