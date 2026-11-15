"""Панель керування poster у браузері (лише цей ПК: http://127.0.0.1:8770).

Вхід за паролем (створюється при першому відкритті). Тут можна:
  • запускати / зупиняти / ставити на паузу бота, тестовий слот, режим тест/бойовий, автозапуск;
  • вписати токени й ключі (Telegram, YouTube, Meta, TikTok) і авторизувати платформи;
  • керувати тим, хто отримує сповіщення бота;
  • змінювати розклад і папки, дивитися чергу й журнал.

Запуск: panel.bat  (або python panel.py)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shlex
import subprocess
import sys
import threading
import types
import time
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import control  # noqa: E402
from config import (DEFAULTS, NAMES, PLATFORMS, SECRETS, ConfigError, _hm, _merge,  # noqa: E402
                    fmt_slot, load_config, local_dt, now_ts)
from db import DB  # noqa: E402

log = logging.getLogger("panel")


def VERSION_NOW() -> str:  # noqa: N802
    from config import VERSION
    return VERSION

DEFAULT_PORT = 8770
SESSION_HOURS = 12
PUBLIC_IDS = {"TELEGRAM_CHAT_ID", "FB_PAGE_ID", "IG_USER_ID", "META_APP_ID", "TIKTOK_CLIENT_KEY"}
SECRET_LABELS = {
    "TELEGRAM_BOT_TOKEN": "Токен бота", "TELEGRAM_CHAT_ID": "Головні адміністратори (chat_id)",
    "META_APP_ID": "App ID", "META_APP_SECRET": "App Secret", "META_PAGE_TOKEN": "Токен сторінки",
    "META_IG_TOKEN": "Окремий токен Instagram", "META_USER_TOKEN": "Токен користувача Meta", "GEMINI_API_KEY": "Ключ Gemini API", "FB_PAGE_ID": "ID сторінки Facebook",
    "IG_USER_ID": "ID акаунта Instagram", "GITHUB_TOKEN": "Токен GitHub (оновлення)", "TIKTOK_CLIENT_KEY": "Client key", "TIKTOK_CLIENT_SECRET": "Client secret",
}

# Поля config.json, які можна змінювати з панелі: шлях -> тип
FIELDS = {
    "slots": "slots", "timezone": "str", "notify_before_minutes": "int", "min_notify_lead_minutes": "int",
    "publish_on_approve": "bool", "telegram.queue_cards": "bool", "ask_texts_before_publish": "bool", "catchup_minutes": "int", "max_skips": "int",
    "paths.ready": "str", "paths.posted": "str", "paths.skipped": "str", "paths.cancelled": "str", "ready_status": "str",
    "regenerate_command": "cmd", "regenerate_cwd": "str", "ffmpeg": "str", "ffprobe": "str",
    "reports.daily_time": "time", "reports.health_check_time": "time", "reports.stats_after_hours": "int", "reports.weekly_day": "int",
    "retry.attempts": "int", "retry.pause_seconds": "int", "graph_api_version": "str", "dry_run": "bool",
    "platforms.youtube.enabled": "bool", "platforms.youtube.privacy": "privacy",
    "platforms.youtube.add_shorts_hashtag": "bool", "platforms.youtube.notify_subscribers": "bool",
    "platforms.instagram.enabled": "bool", "platforms.instagram.share_to_feed": "bool", "platforms.instagram.max_upload_mb": "int",
    "platforms.facebook.enabled": "bool",
    "platforms.tiktok.enabled": "bool", "platforms.tiktok.public": "bool", "platforms.tiktok.redirect_uri": "str",
    "auto_meta.enabled": "bool", "auto_meta.hashtags": "tagmap",
    "always_hashtags.tags": "taglist", "always_hashtags.in_title": "bool", "always_hashtags.in_captions": "bool",
    "ai.enabled": "bool", "ai.model": "str", "ai.hashtags_count": "int", "ai.channel_description": "str",
    "ai.extra_prompt": "str", "ai.language": "lang", "platforms.youtube.language": "lang",
    "footer.text": "str", "footer.in_description": "bool", "footer.in_captions": "bool", "auto_meta.default_title": "str",
    "on_missed_approval": "missed", "updates.auto": "bool", "updates.check_minutes": "int", "power.sleep_between_slots": "bool",
    "power.wake_before_minutes": "int", "power.quiet_minutes": "int", "power.hold_during_stream": "bool",
    "power.no_sleep_apps": "applist",
    "highlights.enabled": "bool", "highlights.folder": "str", "highlights.slots": "hlslots",
    "highlights.privacy": "privacy_opt",
}


def mask(value: str) -> str:
    if not value:
        return ""
    return value[:4] + "…" + value[-3:] if len(value) > 12 else "•" * len(value)


def dig(d: dict, path: str):
    for p in path.split("."):
        d = d.get(p) if isinstance(d, dict) else None
    return d


def put(d: dict, path: str, value) -> None:
    parts = path.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


def coerce(kind: str, value):
    if kind == "bool":
        return bool(value)
    if kind == "int":
        v = int(value)
        if v < 0:
            raise ValueError("має бути ≥ 0")
        return v
    if kind == "time":
        return _hm(value)
    if kind == "applist":
        items = value if isinstance(value, list) else re.split(r"[,;\n]+", str(value))
        return [x.strip() for x in items if x.strip()]
    if kind == "missed":
        if value not in ("wait", "skip", "auto"):
            raise ValueError("wait / skip / auto")
        return value
    if kind == "lang":
        if value not in ("uk", "en"):
            raise ValueError("uk / en")
        return value
    if kind == "privacy":
        if value not in ("public", "unlisted", "private"):
            raise ValueError("public / unlisted / private")
        return value
    if kind == "hlslots":
        items = value if isinstance(value, list) else re.split(r"[,\s;]+", str(value))
        slots = sorted({_hm(x) for x in items if str(x).strip()})
        if not 1 <= len(slots) <= 3:
            raise ValueError("хайлайти: від 1 до 3 разів на день, напр. 18:00")
        return slots
    if kind == "privacy_opt":
        if value not in ("", "public", "unlisted", "private"):
            raise ValueError("порожньо / public / unlisted / private")
        return value
    if kind == "slots":
        items = value if isinstance(value, list) else re.split(r"[,\s;]+", str(value))
        slots = sorted({_hm(x) for x in items if str(x).strip()})
        if not 1 <= len(slots) <= 6:
            raise ValueError("потрібно від 1 до 6 слотів")
        return slots
    if kind == "taglist":
        if isinstance(value, list):
            value = " ".join(map(str, value))
        out = []
        for t in re.split(r"[\s,;]+", str(value)):
            t = "#" + re.sub(r"[^\w]", "", t.lstrip("#"), flags=re.UNICODE)
            if len(t) > 1 and t.lower() not in {x.lower() for x in out}:
                out.append(t)
        return out
    if kind == "tagmap":
        if isinstance(value, dict):
            return value
        out = {}
        for line in str(value).splitlines():
            if ":" not in line:
                continue
            game, tags = line.split(":", 1)
            game = game.strip() or "*"
            if game in ("усі", "всі", "інші", "все"):
                game = "*"
            tags = ["#" + t.lstrip("#") for t in re.split(r"[\s,]+", tags) if t.strip("#").strip()]
            if tags:
                out[game] = tags
        if "*" not in out:
            out["*"] = ["#shorts"]
        return out
    if kind == "cmd":
        if isinstance(value, list):
            return [str(x) for x in value]
        return [t.strip('"') for t in shlex.split(str(value), posix=False)] if str(value).strip() else []
    return str(value).strip()


def tagmap_to_text(table: dict) -> str:
    table = table or {}
    lines = [f"{k}: {' '.join(v)}" for k, v in table.items() if k != "*"]
    lines.append(f"*: {' '.join(table.get('*', []))}")
    return "\n".join(lines)


def cmd_to_text(cmd) -> str:
    return " ".join(f'"{a}"' if " " in a else a for a in (cmd or []))


class Panel:
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

    # ---------------- налаштування ----------------
    def reload(self) -> dict:
        self.cfg = load_config(self.config_path)
        return self.cfg

    def raw_config(self) -> dict:
        return json.loads(Path(self.cfg["config_path"]).read_text(encoding="utf-8"))

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
            from config import ensure_dirs
            ensure_dirs(load_config(tmp))
        except ConfigError as e:
            tmp.unlink(missing_ok=True)
            raise ValueError(str(e)) from e
        os.replace(tmp, path)
        if patch.get("power.sleep_between_slots") is False:
            import power
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

    def mark_restart(self) -> None:
        if control.is_running(self.cfg["lock_port"]):
            self.restart_needed = True

    # ---------------- фонові задачі ----------------
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

    # ---------------- дії ----------------
    def action(self, name: str, data: dict) -> str:
        self.reload()  # .env / config.json могли змінити вручну
        port = self.cfg["lock_port"]
        if name in ("start", "restart"):
            sec = self.cfg["secrets"]
            if not sec["TELEGRAM_BOT_TOKEN"] or not sec["TELEGRAM_CHAT_ID"]:
                raise ValueError("Спершу впишіть токен бота і свій chat_id: вкладка «✈️ Telegram і доступ» → Зберегти")
        self._owner_cache = (0, None)
        if name == "start":
            if not control.start_bot(port, config=self.cfg["config_path"]):
                raise ValueError("Бот не запустився. " + self.last_error())
            self.restart_needed = False
            return "Бот запущено"
        if name == "stop":
            if not control.stop_bot(self.db, port):
                raise ValueError("Бот не зупинився. Закрийте вікно бота або в Диспетчері завдань завершіть "
                                 "процеси pythonw.exe / python.exe, потім натисніть «Запустити».")
            return "Бот зупинено"
        if name == "restart":
            if not control.stop_bot(self.db, port):
                raise ValueError("Бот не зупинився. Закрийте вікно бота або в Диспетчері завдань завершіть "
                                 "процеси pythonw.exe / python.exe, потім натисніть «Запустити».")
            time.sleep(1)
            if not control.start_bot(port, config=self.cfg["config_path"]):
                raise ValueError("Бот не запустився. " + self.last_error())
            self.restart_needed = False
            return "Бот перезапущено"
        if name == "channel":
            return self.channel_action(str(data.get("key") or "en"), str(data.get("op") or ""), data)
        if name == "stop_pub":  # «⛔ Стоп публікацій»: нічого не публікується, навіть підтверджене
            self.db.set("stopped", "1")
            self.db.event(None, "stopped", {"by": "panel"})
            return "Публікації зупинено"
        if name == "go_pub":
            self.db.set("stopped", None)
            self.db.event(None, "resumed_publishing", {"by": "panel"})
            return "Публікації відновлено"
        if name == "pause":
            self.db.set("paused", "1")
            return "Розклад призупинено"
        if name == "resume":
            self.db.set("paused", None)
            return "Розклад відновлено"
        if name == "testslot":
            minutes = int(data.get("minutes") or 10)
            code, out = self.run_poster("test-slot", str(minutes))
            if code:
                raise ValueError(out)
            return out
        if name == "mode":
            mode = data.get("mode")
            if mode not in ("test", "live"):
                raise ValueError("mode")
            self.save_settings({"dry_run": mode == "test"})
            return "Тестовий режим" if mode == "test" else "Бойовий режим — публікації справжні"
        if name in ("check", "tg_test"):
            def run(cmd=("check" if name == "check" else "tg-test")):
                code, out = self.run_poster(cmd)
                if code:
                    raise RuntimeError(out)
                return out
            self.job(name, run)
            return "Перевіряю…"
        if name in ("autostart_on", "autostart_off"):
            if not control.WIN:
                raise ValueError("Автозапуск налаштовується лише у Windows")

            def run(script=("install_task.ps1" if name == "autostart_on" else "uninstall_task.ps1")):
                r = control.run_powershell(script)
                if r.returncode:
                    raise RuntimeError((r.stderr or r.stdout)[-500:])
                return (r.stdout or "Готово").strip()[-500:]
            self.job("autostart", run)
            return "Виконую…"
        if name == "open":
            target = {"ready": self.cfg["paths"]["ready"], "posted": self.cfg["paths"]["posted"],
                      "highlights": self.cfg["highlights"]["folder_path"],
                      "skipped": self.cfg["paths"]["skipped"], "cancelled": self.cfg["paths"]["cancelled"],
                      "log": self.cfg["paths"]["log"],
                      "folder": self.root}.get(data.get("what"))
            if not target:
                raise ValueError("Невідомо що відкрити")
            control.open_path(target)
            return "Відкрито"
        if name == "details":
            return self.bot_call({"op": "details", "id": int(data.get("id"))}, 30)
        if name == "publish_all":
            return self.bot_call({"op": "publish_all"}, 60)
        if name == "item":
            cmd = {k: data.get(k) for k in ("op", "id", "title", "tags", "when", "platform") if k in data}
            return self.bot_call(cmd, 180 if cmd.get("op") in ("publish", "save", "regen") else 40)
        if name == "analytics":
            import analytics
            days = int(data.get("days") or 1)
            plats = [p for p in PLATFORMS if self.cfg["platforms"].get(p, {}).get("enabled")]
            self.jobs["report"] = {"state": "ok", "message": analytics.period_report(self.cfg, self.db, days, plats),
                                   "finished": now_ts()}
            return analytics.PERIODS.get(days, "")
        if name == "report":
            kind = data.get("kind")
            if kind not in ("report", "stats", "top"):
                raise ValueError("kind")
            self.job("report", lambda: self.bot_call({"op": kind, "send": bool(data.get("send"))}, 300))
            return "Збираю дані…"
        if name == "platform":
            key = data.get("key")
            if key not in PLATFORMS:
                raise ValueError("Невідома платформа")
            on = bool(data.get("on"))
            self.save_settings({f"platforms.{key}.enabled": on})
            return f"{NAMES[key]}: {'увімкнено' if on else 'вимкнено'}"
        import updater
        if name == "update_check":

            def run():
                info = updater.check(self.cfg)
                self.db.set("update_remote", info["remote"])
                self.db.set("update_error", None)
                return (f"Доступна нова версія {info['remote']} (у вас {info['local']})" if info["newer"]
                        else f"У вас остання версія ({info['local']})")
            self.job("update", run)
            return "Перевіряю…"
        if name == "update_now":
            if control.is_running(self.cfg["lock_port"]):
                force = time.time() - getattr(self, "_upd_asked", 0) < 120  # друге натискання — оновити попри публікацію
                self._upd_asked = time.time()
                msg = self.bot_call({"op": "update", "force": force}, 180)
                return msg + (" Панель перезапуститься — оновіть сторінку за ~15 с." if "Оновлено" in msg else "")

            def run():
                new = updater.apply(self.cfg)
                updater.spawn([str(ROOT / "panel.py"), "--config", self.cfg["config_path"], "--no-browser"])
                return f"Оновлено до {new}. Панель перезапускається — оновіть сторінку за ~10 с"
            self.job("update", run)
            return "Завантажую оновлення…"
        if name == "stream":
            hours = int(data.get("hours") or 0)
            self.db.set("stream_until", now_ts() + hours * 3600 if hours > 0 else None)
            return f"🎮 Режим стріму на {hours} год — ПК не засне" if hours > 0 else "Режим стріму вимкнено"
        if name == "shortcut":
            desk = control.create_shortcut()
            return f"Ярлик «{control.SHORTCUT_NAME}» створено на робочому столі{(' (' + desk + ')') if desk else ''} і в меню «Пуск»"
        if name == "power_allow":
            import power
            return power.allow_wake_timers()
        if name == "failed":
            item_id, op = int(data.get("id")), str(data.get("op"))
            sched = self._sched()
            item = self.db.get_item(item_id)
            if not item or item["status"] not in ("failed", "posted"):
                raise ValueError("Цього шортса вже немає серед невдалих")
            if op == "ignore":
                self.db.set(f"ignored:{item_id}", "1")
                self.db.x("DELETE FROM slots WHERE state='retry' AND item_id=?", (item_id,))
                return "Проігноровано"
            if op == "unignore":
                self.db.set(f"ignored:{item_id}", None)
                return "Повернуто до невдалих"
            if op in ("now", "slot"):
                self.db.set(f"ignored:{item_id}", None)
                ts = sched.retry_soon(item_id) if op == "now" else sched.reserve_retry(item_id)
                from config import fmt_when
                tail = "" if control.is_running(self.cfg["lock_port"]) else " (бот зараз зупинений — запустіть його)"
                return f"Повтор: {fmt_when(self.cfg, ts)}{tail}"
            raise ValueError("Невідома дія")
        if name == "ai_test":
            import ai
            from scheduler import find_videos
            cfg = self.cfg
            videos = find_videos(Path(cfg["paths"]["ready"])) if Path(cfg["paths"]["ready"]).is_dir() else []
            if not videos:
                raise ValueError("У папці з шортсами немає відео для проби")
            mp4 = videos[0]
            from scheduler import game_of
            game = game_of(mp4, Path(cfg["paths"]["ready"]))

            def run():
                r = ai.generate(cfg, mp4, game)
                return (f"Відео: {mp4.name}\n\nНазва: {r['title']}\nХештеги: {' '.join(r['hashtags'])}\n\n"
                        f"Опис: {r['description']}\n\nTikTok: {r['tiktok']}\n\nInstagram:\n{r['instagram']}")
            self.job("ai_test", run)
            return "ШІ пише тексти для першого відео…"
        if name == "retry":
            raise ValueError("Повтор — командою /retry N у Telegram")
        raise ValueError(f"Невідома дія {name}")

    def channel_action(self, key: str, op: str, data: dict) -> str:
        """Вкладка «🇬🇧 EN-канал»: створити, запустити/зупинити бота каналу, відкрити його панель."""
        import channels
        if self.cfg.get("channel"):
            raise ValueError("Керування каналами — у панелі основного каналу")
        if op == "create":
            ready = str(data.get("ready") or "").strip() or None
            d = channels.create(self.cfg, key, ready)
            return f"Канал створено: {d}. Далі — «Відкрити панель каналу»: Telegram-бот, YouTube, папка шортсів."
        if op == "start":
            if not channels.start(key):
                raise ValueError("Бот каналу не запустився — подивіться журнал у панелі каналу")
            return "Бот каналу запущено"
        if op == "stop":
            if not channels.stop(key):
                raise ValueError("Бот каналу не зупинився")
            return "Бот каналу зупинено"
        if op == "panel":
            url = channels.open_panel(key)
            return f"Панель каналу: {url}"
        if op == "autostart":
            channels.set_autostart(key, bool(data.get("on")))
            return "Автозапуск каналу " + ("увімкнено" if data.get("on") else "вимкнено")
        raise ValueError(f"Невідома дія «{op}»")

    def channels_state(self) -> dict:
        import channels
        if self.cfg.get("channel"):
            return {"is_channel": True, "name": self.cfg["channel"].get("name") or "", "list": []}
        out = []
        for key in channels.TEMPLATES:
            try:
                out.append(channels.status(key))
            except Exception as e:  # noqa: BLE001
                out.append({"key": key, "exists": True, "error": str(e)})
        return {"is_channel": False, "name": "", "list": out}

    def last_error(self) -> str:
        """Остання помилка служби з журналу — щоб показати причину просто в панелі."""
        try:
            lines = Path(self.cfg["paths"]["log"]).read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
        except OSError:
            return "Подивіться вкладку «Журнал»."
        for line in reversed(lines):
            if "[ERROR]" in line or "[CRITICAL]" in line or "Error:" in line:
                text = line.split("]: ", 1)[-1] if "]: " in line else line
                for v in self.cfg["secrets"].values():
                    if v and len(v) > 12:
                        text = text.replace(v, mask(v))
                return "Причина: " + text[:300]
        return "Подивіться вкладку «Журнал»."

    # ---------------- користувачі ----------------
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

    def tg_say(self, chat_id: int, text: str) -> None:
        token = self.cfg["secrets"]["TELEGRAM_BOT_TOKEN"]
        if not token:
            return

        def run():
            try:
                from telegram_api import TelegramAPI
                TelegramAPI(token).send_message(chat_id, text, html=False)
            except Exception as e:  # noqa: BLE001
                log.warning("tg_say %s: %s", chat_id, e)
        threading.Thread(target=run, daemon=True).start()

    # ---------------- авторизація платформ ----------------
    def youtube_secret(self, content: str) -> str:
        try:
            data = json.loads(content)
        except ValueError as e:
            raise ValueError("Це не JSON-файл") from e
        if not isinstance(data, dict) or not ({"installed", "web"} & set(data)):
            raise ValueError("Це не client_secret від Google (немає розділу installed/web)")
        if "web" in data and "installed" not in data:
            raise ValueError("Потрібен OAuth-клієнт типу «Desktop app», а не «Web application»")
        path = Path(self.cfg["platforms"]["youtube"]["client_secrets_file"])
        if self.cfg.get("channel"):
            # додатковий канал (EU): свій ключ — у своїй папці. НІКОЛИ не перезаписувати ключ основного каналу.
            path = Path(self.cfg["root"]) / "client_secret.json"
            raw = self.raw_config()
            raw.setdefault("platforms", {}).setdefault("youtube", {})["client_secrets_file"] = "client_secret.json"
            Path(self.cfg["config_path"]).write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        old_id = ""
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            old_id = (old.get("installed") or old.get("web") or {}).get("client_id", "")
        except (OSError, ValueError):
            pass
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        new_id = (data.get("installed") or {}).get("client_id", "")
        token = Path(self.cfg["paths"]["tokens"]) / "youtube_token.json"
        if old_id != new_id and token.exists():
            token.unlink()                    # токен від іншого ключа не працюватиме — авторизуйтесь заново
        self.reload()
        return f"Збережено {path}" + (" — тепер натисніть «Авторизувати YouTube»" if not token.exists() else "")

    def youtube_auth(self) -> str:
        from platforms.youtube import YouTube
        if not Path(self.cfg["platforms"]["youtube"]["client_secrets_file"]).exists():
            raise ValueError("Спершу завантажте client_secret.json")
        yt = YouTube(self.reload())

        def show(url):                         # браузер міг не відкритися (панель у фоні) — посилання в панелі
            if self.jobs.get("youtube_auth", {}).get("state") == "running":
                self.jobs["youtube_auth"]["message"] = f"Увійдіть у Google за посиланням (5 хв): {url}"
        self.job("youtube_auth", lambda: (yt.authorize(on_url=show, timeout_s=300),
                                          f"YouTube підключено: {yt.check()}")[1])
        return "Відкриваю вхід у Google… Якщо браузер не відкрився — посилання з'явиться під кнопкою."

    def meta_exchange(self, token: str) -> list[dict]:
        from platforms.meta_graph import meta_pages
        self.meta_pages = meta_pages(self.reload(), token)
        return [{"index": i, "name": p["name"], "id": p["id"],
                 "instagram": (p.get("instagram_business_account") or {}).get("username")}
                for i, p in enumerate(self.meta_pages)]

    def meta_select(self, index: int) -> str:
        from platforms.meta_graph import meta_save
        if not 0 <= index < len(self.meta_pages):
            raise ValueError("Спершу отримайте список сторінок")
        msg = meta_save(self.cfg, self.meta_pages[index])
        self.meta_pages = []
        for k in ("META_PAGE_TOKEN", "FB_PAGE_ID", "IG_USER_ID", "META_USER_TOKEN"):
            os.environ.pop(k, None)
        self.reload()
        return msg

    def tiktok_start(self) -> dict:
        from platforms.tiktok import TikTok
        tt = TikTok(self.reload())
        flow = tt.auth_begin()
        self.tiktok_flow = flow
        if flow["local"]:
            def run():
                back = tt.auth_wait_local(flow)
                tok = tt.auth_finish(flow, back)
                return f"TikTok підключено (дозволи: {tok.get('scope')})"
            self.job("tiktok_auth", run)
        return {"url": flow["url"], "local": flow["local"]}

    def tiktok_finish(self, url: str) -> str:
        from platforms.tiktok import TikTok
        if not self.tiktok_flow:
            raise ValueError("Спершу натисніть «Авторизувати»")
        tok = TikTok(self.cfg).auth_finish(self.tiktok_flow, url)
        self.tiktok_flow = None
        return f"TikTok підключено (дозволи: {tok.get('scope')})"

    # ---------------- папки ----------------
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
            from scheduler import find_videos
            files = find_videos(folder, exclude=(self.cfg["paths"]["posted"], self.cfg["paths"]["skipped"]))[:5000]
        except OSError:
            return info
        for mp4 in files:
            info["total"] += 1
            from scheduler import game_of
            if game_of(mp4, folder):
                games.add(game_of(mp4, folder))
            js = mp4.with_suffix(".json")
            if not js.exists():
                info["no_json"] += 1
                if auto:
                    info["ready"] += 1
                continue
            try:
                from meta import json_status, read_json
                if json_status(read_json(js)) == ready_status:
                    info["ready"] += 1
            except (OSError, ValueError):
                pass
        info["games"] = sorted(games)
        return info

    @staticmethod
    def path_problems(cfg: dict) -> list[str]:
        from config import PATH_NAMES
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

    def bot_call(self, cmd: dict, timeout: int = 40) -> str:
        """Передає дію запущеному боту (через базу) і чекає на результат."""
        if not control.is_running(self.cfg["lock_port"]):
            raise ValueError("Бот зупинений — спершу натисніть «▶️ Запустити»")
        cid = secrets.token_hex(8)
        self.db.set(f"pcmd:{cid}", json.dumps(cmd, ensure_ascii=False))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self.db.get(f"pres:{cid}")
            if raw:
                self.db.x("DELETE FROM kv WHERE key=?", (f"pres:{cid}",))
                res = json.loads(raw)
                if not res.get("ok"):
                    raise ValueError(res.get("message") or "Помилка")
                return res.get("message") or "Готово"
            time.sleep(0.3)
        if self.db.get(f"pcmd:{cid}") is not None:
            self.db.x("DELETE FROM kv WHERE key=?", (f"pcmd:{cid}",))
            raise ValueError("Бот не взяв команду — перезапустіть його (стара версія не вміє виконувати дії з панелі)")
        raise ValueError("Бот ще виконує дію — оновіть сторінку за хвилину")

    def item_view(self, cfg: dict, item: dict, now: int, ts: int | None = None) -> dict:
        from meta import PostMeta, load_meta
        try:
            meta = load_meta(item["json"], item["title_override"])
        except Exception:  # noqa: BLE001
            meta = PostMeta(yt_title=item["title_override"] or Path(item["mp4"]).stem)
        conf = cfg.get("always_hashtags") or {}
        always = {t.lower() for t in conf.get("tags") or []}
        from config import fmt_when
        ts = ts if ts is not None else item["slot_at"]
        return {"id": item["id"], "name": Path(item["mp4"]).stem, "game": item["game"], "status": item["status"],
                "title": meta.yt_title, "tags": " ".join(t for t in meta.hashtags if t.lower() not in always),
                "always": " ".join(conf.get("tags") or []), "when": fmt_when(cfg, ts, now) if ts else "",
                "ts": ts or 0, "disabled": item["disabled"], "skips": item["skips"], "edited": item["edited"],
                "exists": Path(item["mp4"]).exists()}

    def _sched(self):
        from platforms import build_platforms
        from scheduler import Scheduler
        app = types.SimpleNamespace(cfg=self.cfg, db=self.db, platforms={k: None for k in build_platforms(self.cfg)})
        sched = Scheduler(app)
        app.scheduler = sched
        return sched

    def failed_state(self, cfg: dict, now: int) -> list[dict]:
        from config import fmt_when
        try:
            sched = self._sched()
            rows = [(i, False) for i in sched.failed_items()] + [(i, True) for i in sched.failed_items(ignored=True)]
        except Exception as e:  # noqa: BLE001
            log.warning("failed_state: %s", e)
            return []
        out = []
        for item, ignored in rows[:30]:
            when = sched.retry_at(item["id"])
            out.append({
                "id": item["id"], "name": Path(item["mp4"]).stem, "ignored": ignored,
                "exists": Path(item["mp4"]).exists(),
                "errors": {NAMES.get(p, p): str(r.get("error", ""))[:900] for p, r in item["errors"].items()}
                or ({"—": "не опубліковано ніде"} if item["status"] == "failed" else {}),
                "ok": [NAMES.get(p, p) for p, r in item["results"].items() if r.get("status") == "ok"],
                "retry": fmt_when(cfg, when, now) if when else "",
            })
        return out

    # ---------------- стан для сторінки ----------------
    def state(self) -> dict:
        cfg = self.reload()
        now = now_ts()
        merged = _merge(DEFAULTS, self.raw_config())
        settings = {k: dig(merged, k) for k in FIELDS}
        settings["regenerate_command"] = cmd_to_text(settings["regenerate_command"])
        settings["auto_meta.hashtags"] = tagmap_to_text(settings["auto_meta.hashtags"])
        settings["always_hashtags.tags"] = " ".join(settings["always_hashtags.tags"] or [])
        settings["power.no_sleep_apps"] = ", ".join(settings["power.no_sleep_apps"] or [])
        settings["highlights.slots"] = ", ".join(settings["highlights.slots"] or [])
        secrets_ = {n: {"set": bool(v), "value": v if n in PUBLIC_IDS else mask(v), "label": SECRET_LABELS.get(n, n)}
                    for n, v in cfg["secrets"].items()}

        tokens = {}
        yt_secret = Path(cfg["platforms"]["youtube"]["client_secrets_file"])
        tokens["youtube"] = {"client_secret": yt_secret.exists(),
                             "connected": (Path(cfg["paths"]["tokens"]) / "youtube_token.json").exists()}
        tt_file = Path(cfg["paths"]["tokens"]) / "tiktok_token.json"
        tt = {"connected": tt_file.exists()}
        if tt_file.exists():
            try:
                t = json.loads(tt_file.read_text(encoding="utf-8"))
                tt["refresh_expires"] = local_dt(cfg, int(t.get("refresh_expires_at", 0))).strftime("%d.%m.%Y")
                tt["scope"] = t.get("scope")
            except (ValueError, OSError):
                pass
        tokens["tiktok"] = tt
        tokens["meta"] = {"connected": bool(cfg["secrets"]["META_PAGE_TOKEN"])}

        db = self.db
        from config import fmt_when
        from scheduler import Scheduler
        sched = Scheduler(types.SimpleNamespace(cfg=cfg, db=db, app=None))
        queue = [{"id": i["id"], "name": Path(i["mp4"]).stem, "game": i["game"], "skips": i["skips"],
                  "when": fmt_when(cfg, ts, now) if ts else ""}
                 for i, ts in sched.projection(now)[:30]]
        current = [{"id": i["id"], "name": Path(i["mp4"]).stem, "status": i["status"],
                    "slot": fmt_slot(cfg, i["slot_at"], now) if i["slot_at"] else ""}
                   for i in db.items("status IN ('notified','approved','publishing','failed')", order="slot_at")]
        upcoming = [{"slot": fmt_when(cfg, s["slot_at"], now), "item": s["item"]["id"] if s["item"] else None,
                     "name": Path(s["item"]["mp4"]).stem if s["item"] else "", "kind": s["kind"], "manual": s["manual"]}
                    for s in sched.slot_plan(now, 6)]
        posted = [{"id": i["id"], "name": Path(i["mp4"]).stem,
                   "when": local_dt(cfg, i["posted_at"]).strftime("%d.%m %H:%M") if i["posted_at"] else "",
                   "links": {p: r.get("url") for p, r in i["results"].items() if r.get("status") == "ok"},
                   "errors": {p: r.get("error") for p, r in i["results"].items() if r.get("status") != "ok"},
                   "details": any(r.get("status") != "ok" or r.get("tried") for r in i["results"].values())}
                  for i in db.items("status='posted'", order="posted_at DESC")[:10]]
        owners = [int(x) for x in re.findall(r"-?\d+", cfg["secrets"]["TELEGRAM_CHAT_ID"] or "")]
        users = [{**u, "owner": u["chat_id"] in owners} for u in db.users()]
        health = {p: db.get(f"health_{p}") for p in PLATFORMS}

        log_tail = ""
        log_path = Path(cfg["paths"]["log"])
        if log_path.exists():
            with open(log_path, "rb") as f:
                f.seek(max(0, log_path.stat().st_size - 80000))
                log_tail = f.read().decode("utf-8", errors="replace").split("\n", 1)[-1]
            for v in cfg["secrets"].values():
                if v and len(v) > 12:
                    log_tail = log_tail.replace(v, mask(v))
            log_tail = "\n".join(log_tail.splitlines()[-400:])

        running = control.is_running(cfg["lock_port"])
        owner = None
        if running:  # пошук процесу на Windows запускає PowerShell — не частіше ніж раз на 20 с
            cached = getattr(self, "_owner_cache", (0, None))
            if time.time() - cached[0] > 20:
                cached = (time.time(), control.port_owner(cfg["lock_port"]))
                self._owner_cache = cached
            owner = cached[1]
        return {
            "running": running,
            "foreign_bot": owner["cmd"] if control.is_foreign(owner) else None,
            "paused": db.get("paused") == "1",
            "stopped": db.get("stopped") == "1",
            "dry_run": bool(cfg.get("dry_run")),
            "restart_needed": self.restart_needed,
            "autostart": control.autostart_installed() if control.WIN else None,
            "windows": control.WIN,
            "settings": settings, "secrets": secrets_, "tokens": tokens,
            "platforms": {p: {"name": NAMES[p], "enabled": bool(cfg["platforms"][p].get("enabled")),
                              "health": health[p]} for p in PLATFORMS},
            "queue": queue, "current": current, "upcoming": upcoming, "posted": posted,
            "users": users, "jobs": self.jobs, "log": log_tail,
            "health_time": fmt_slot(cfg, int(db.get("health_ts")), now) if db.get("health_ts") else None,
            "ready_info": self.count_shorts(cfg["paths"]["ready"]),
            "failed": self.failed_state(cfg, now),
            "cancelled": [{"id": i["id"], "name": Path(i["mp4"]).stem, "exists": Path(i["mp4"]).exists()}
                          for i in db.items("status='cancelled'", order="id DESC")[:15]],
            "update": {"remote": db.get("update_remote") or "", "error": db.get("update_error") or "",
                       "newer": __import__("updater").version_key(db.get("update_remote") or "")
                       > __import__("updater").version_key(VERSION_NOW()),
                       "token": bool(cfg["secrets"].get("GITHUB_TOKEN"))},
            "stream_until": fmt_slot(cfg, int(db.get("stream_until")), now)
            if int(db.get("stream_until") or 0) > now else "",
            "power": {"on": bool((cfg.get("power") or {}).get("sleep_between_slots")),
                      "state": db.get("power_state") or "",
                      "next_wake": fmt_when(cfg, int(db.get("power_next_wake")), now)
                      if db.get("power_next_wake") and int(db.get("power_next_wake")) > now else ""},
            "work": [self.item_view(cfg, i, now) for i in
                     db.items("status IN ('notified','approved','publishing')", order="slot_at")],
            "queue_items": [self.item_view(cfg, i, now, ts or 0) for i, ts in sched.projection(now)[:40]],
            "free_slots": [{"ts": t, "label": fmt_when(cfg, t, now)} for t in sched.future_slots(now, 10)],
            "pending_users": [u for u in users if u["role"] == "pending"],
            "can_regen": bool(cfg.get("regenerate_command")) or bool(cfg.get("ai", {}).get("enabled")
                                                                     and cfg["secrets"].get("GEMINI_API_KEY")),
            "path_problems": self.path_problems(cfg),
            "root": str(self.root), "now": local_dt(cfg, now).strftime("%d.%m.%Y %H:%M"),
            "channels": self.channels_state(),
        }

    # ---------------- пароль ----------------
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


# ======================================================================
def make_handler(panel: Panel):
    page = (ROOT / "panel.html").read_bytes()
    allowed_hosts = {f"127.0.0.1:{panel.port}", f"localhost:{panel.port}"}

    class H(BaseHTTPRequestHandler):
        server_version = "poster-panel"

        def log_message(self, fmt, *args):
            log.debug("panel: " + fmt, *args)

        # ---- helpers ----
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                log.debug("Браузер закрив з'єднання до відповіді")

        def json(self, data, code: int = 200, extra: dict | None = None):
            self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

        def session(self) -> str | None:
            c = SimpleCookie(self.headers.get("Cookie") or "")
            return c["poster_session"].value if "poster_session" in c else None

        def authed(self) -> bool:
            return panel.valid_session(self.session())

        def host_ok(self) -> bool:
            return (self.headers.get("Host") or "") in allowed_hosts

        def body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 2_000_000:
                raise ValueError("Завеликий запит")
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw or b"{}")

        def cookie(self, token: str, max_age: int) -> dict:
            return {"Set-Cookie": f"poster_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}"}

        # ---- GET ----
        def do_GET(self):  # noqa: N802
            if not self.host_ok():
                return self._send(403, b"Forbidden", "text/plain")
            if self.path in ("/", "/index.html"):
                return self._send(200, page, "text/html; charset=utf-8",
                                  {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                              "script-src 'self' 'unsafe-inline'; img-src 'self' data:"})
            if self.path == "/api/session":
                from config import VERSION
                return self.json({"authed": self.authed(), "has_password": panel.has_password(), "version": VERSION})
            if self.path == "/api/state":
                if not self.authed():
                    return self.json({"error": "auth"}, 401)
                try:
                    return self.json(panel.state())
                except Exception as e:  # noqa: BLE001
                    log.exception("state")
                    return self.json({"error": str(e)}, 500)
            if self.path.startswith("/api/video?"):
                if not self.authed():
                    return self._send(401, b"auth", "text/plain")
                return self.video()
            return self._send(404, b"Not found", "text/plain")

        def video(self):
            from urllib.parse import parse_qs, urlparse
            try:
                item = panel.db.get_item(int(parse_qs(urlparse(self.path).query)["id"][0]))
                path = Path(item["mp4"])
                size = path.stat().st_size
            except (KeyError, ValueError, TypeError, OSError):
                return self._send(404, b"Not found", "text/plain")
            start, end, code = 0, size - 1, 200
            m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                code = 206
            self.send_response(code)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "private, max-age=600")
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            try:
                with open(path, "rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = f.read(min(256 * 1024, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except (ConnectionError, OSError):
                pass  # браузер перервав завантаження (перемотування) — це нормально

        # ---- POST ----
        def do_POST(self):  # noqa: N802
            if not self.host_ok() or self.headers.get("X-Poster") != "1":
                return self._send(403, b"Forbidden", "text/plain")
            try:
                data = self.body()
            except ValueError as e:
                return self.json({"error": str(e)}, 400)
            path = self.path
            try:
                if path == "/api/setup":
                    if panel.has_password():
                        return self.json({"error": "Пароль уже встановлено"}, 400)
                    panel.set_password(str(data.get("password") or ""))
                    token = panel.new_session()
                    return self.json({"ok": True}, extra=self.cookie(token, SESSION_HOURS * 3600))
                if path == "/api/login":
                    if panel.check_password(str(data.get("password") or "")):
                        token = panel.new_session()
                        return self.json({"ok": True}, extra=self.cookie(token, SESSION_HOURS * 3600))
                    time.sleep(1.5)
                    return self.json({"error": "Неправильний пароль"}, 403)
                if not self.authed():
                    return self.json({"error": "auth"}, 401)
                if path == "/api/logout":
                    panel.sessions.pop(self.session() or "", None)
                    return self.json({"ok": True}, extra=self.cookie("", 0))
                if path == "/api/password":
                    if not panel.check_password(str(data.get("old") or "")):
                        return self.json({"error": "Поточний пароль неправильний"}, 403)
                    panel.set_password(str(data.get("new") or ""))
                    return self.json({"message": "Пароль змінено"})
                if path == "/api/action":
                    msg = panel.action(str(data.get("name")), data)
                    log.info("Дія «%s»: %s", data.get("name"), str(msg)[:300])
                    return self.json({"message": msg})
                if path == "/api/settings":
                    panel.save_settings(data)
                    return self.json({"message": "Налаштування збережено"})
                if path == "/api/secrets":
                    panel.save_secrets(data)
                    return self.json({"message": "Збережено"})
                if path == "/api/users":
                    return self.json({"message": panel.set_user(int(data["chat_id"]), str(data["role"]))})
                if path == "/api/youtube/secret":
                    return self.json({"message": panel.youtube_secret(str(data.get("content") or ""))})
                if path == "/api/auth/youtube":
                    return self.json({"message": panel.youtube_auth()})
                if path == "/api/auth/meta":
                    return self.json({"pages": panel.meta_exchange(str(data.get("token") or ""))})
                if path == "/api/auth/meta/select":
                    return self.json({"message": panel.meta_select(int(data.get("index", -1)))})
                if path == "/api/auth/tiktok":
                    return self.json(panel.tiktok_start())
                if path == "/api/auth/tiktok/finish":
                    return self.json({"message": panel.tiktok_finish(str(data.get("url") or ""))})
                if path == "/api/fs":
                    return self.json(panel.list_dir(data.get("path")))
                if path == "/api/fs/mkdir":
                    return self.json(panel.make_dir(str(data.get("path") or ""), str(data.get("name") or "")))
                if path == "/api/pick_folder":
                    picked = panel.pick_folder_native(str(data.get("initial") or ""))
                    return self.json({"path": picked, "native": picked is not None})
                if path == "/api/shutdown":
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return self.json({"message": "Панель закрито"})
                return self.json({"error": "Not found"}, 404)
            except (ValueError, KeyError, TypeError) as e:
                log.warning("Панель: %s %s — помилка: %s", path, data.get("name") or "", e)
                return self.json({"error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001
                log.exception("panel %s", path)
                return self.json({"error": f"{type(e).__name__}: {e}"}, 500)

    return H


def serve(config_path: str | None = None, open_browser: bool = True, port: int | None = None) -> int:
    from config import setup_logging

    try:
        panel = Panel(config_path)
    except ConfigError as e:
        show_fatal(f"Помилка налаштувань: {e}")
        return 2
    setup_logging(panel.cfg)
    if port:
        panel.port = port
    url = f"http://127.0.0.1:{panel.port}/"
    from config import VERSION
    pid_file = Path(panel.cfg["paths"]["data"]) / "panel.pid"
    httpd = None
    for attempt in range(2):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", panel.port), make_handler(panel))
            break
        except OSError:
            # Порт зайнятий старою копією панелі (напр. після оновлення) — закриваємо її і пробуємо ще раз
            old = int(pid_file.read_text().strip() or 0) if pid_file.exists() else 0
            if attempt == 0 and old and old != os.getpid():
                log.info("Закриваю попередню копію панелі (PID %s)", old)
                control.kill(old)
                time.sleep(1.5)
                continue
            log.info("Панель уже відкрита — відкриваю браузер")
            if open_browser:
                webbrowser.open(url)
            return 0
    try:
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    log.info("Панель керування %s: %s", VERSION, url)
    threading.Thread(target=control.ensure_shortcut_once, args=(panel.db,), daemon=True).start()
    if sys.stdout:
        print(f"Панель керування: {url}  (Ctrl+C — закрити)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()
    return 0


def show_fatal(text: str) -> None:
    """Без консолі (pythonw) помилку інакше ніхто не побачить: пишемо у файл і показуємо вікно Windows."""
    try:
        (ROOT / "panel_error.txt").write_text(text, encoding="utf-8")
    except OSError:
        pass
    if sys.stderr is not None:
        print(text, file=sys.stderr)
    if control.WIN:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, text[-1500:] + "\n\nТекст помилки також збережено у panel_error.txt — надішліть його.",
                "poster — панель не запустилась", 0x10)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    import argparse
    import traceback

    ap = argparse.ArgumentParser(description="Панель керування poster")
    ap.add_argument("--config")
    ap.add_argument("--port", type=int)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    try:
        code = serve(a.config, not a.no_browser, a.port)
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        show_fatal(traceback.format_exc())
        code = 1
    sys.exit(code)
