"""Дії з кнопок панелі (як у боті) та керування каналами."""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from app import ROOT
from app.core.config import NAMES
from app.core.config import PLATFORMS
from app.core.config import now_ts
from app.system import control
from pathlib import Path
from .common import (
    log,
)


class ActionsMixin:
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
            from app.insights import analytics
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
        from app.system import updater
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
                updater.set_auto_paused(self.cfg, None)
                updater.spawn([str(ROOT / "panel.py"), "--config", self.cfg["config_path"], "--no-browser"])
                return f"Оновлено до {new}. Панель перезапускається — оновіть сторінку за ~10 с"
            self.job("update", run)
            return "Завантажую оновлення…"
        if name == "switch_version":
            version = str(data.get("version") or "")
            if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}-\d+", version):
                raise ValueError("Невідома версія")
            if control.is_running(self.cfg["lock_port"]):
                msg = self.bot_call({"op": "switch", "version": version}, 180)
                return msg + " Панель перезапуститься — оновіть сторінку за ~15 с."

            def run_switch():
                updater.switch_to(self.cfg, version)
                updater.spawn([str(ROOT / "panel.py"), "--config", self.cfg["config_path"], "--no-browser"])
                return f"Встановлено {version}. Панель перезапускається — оновіть сторінку за ~10 с"
            self.job("update", run_switch)
            return f"Переходжу на {version}…"
        if name == "stream":
            hours = int(data.get("hours") or 0)
            self.db.set("stream_until", now_ts() + hours * 3600 if hours > 0 else None)
            return f"🎮 Режим стріму на {hours} год — ПК не засне" if hours > 0 else "Режим стріму вимкнено"
        if name == "shortcut":
            desk = control.create_shortcut()
            return f"Ярлик «{control.SHORTCUT_NAME}» створено на робочому столі{(' (' + desk + ')') if desk else ''} і в меню «Пуск»"
        if name == "power_allow":
            from app.system import power
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
                from app.core.config import fmt_when
                tail = "" if control.is_running(self.cfg["lock_port"]) else " (бот зараз зупинений — запустіть його)"
                return f"Повтор: {fmt_when(self.cfg, ts)}{tail}"
            raise ValueError("Невідома дія")
        if name == "ai_test":
            from app.core import ai
            from app.engine.scheduler import find_videos
            cfg = self.cfg
            videos = find_videos(Path(cfg["paths"]["ready"])) if Path(cfg["paths"]["ready"]).is_dir() else []
            if not videos:
                raise ValueError("У папці з шортсами немає відео для проби")
            mp4 = videos[0]
            from app.engine.scheduler import game_of
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
        from app.channels import manager as channels
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
        from app.channels import manager as channels
        if self.cfg.get("channel"):
            return {"is_channel": True, "name": self.cfg["channel"].get("name") or "", "list": []}
        out = []
        for key in channels.TEMPLATES:
            try:
                out.append(channels.status(key))
            except Exception as e:  # noqa: BLE001
                out.append({"key": key, "exists": True, "error": str(e)})
        return {"is_channel": False, "name": "", "list": out}

    def tg_say(self, chat_id: int, text: str) -> None:
        token = self.cfg["secrets"]["TELEGRAM_BOT_TOKEN"]
        if not token:
            return

        def run():
            try:
                from app.telegram.api import TelegramAPI
                TelegramAPI(token).send_message(chat_id, text, html=False)
            except Exception as e:  # noqa: BLE001
                log.warning("tg_say %s: %s", chat_id, e)
        threading.Thread(target=run, daemon=True).start()

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
        from app.core.meta import PostMeta, load_meta
        try:
            meta = load_meta(item["json"], item["title_override"])
        except Exception:  # noqa: BLE001
            meta = PostMeta(yt_title=item["title_override"] or Path(item["mp4"]).stem)
        conf = cfg.get("always_hashtags") or {}
        always = {t.lower() for t in conf.get("tags") or []}
        from app.core.config import fmt_when
        ts = ts if ts is not None else item["slot_at"]
        return {"id": item["id"], "name": Path(item["mp4"]).stem, "game": item["game"], "status": item["status"],
                "title": meta.yt_title, "tags": " ".join(t for t in meta.hashtags if t.lower() not in always),
                "always": " ".join(conf.get("tags") or []), "when": fmt_when(cfg, ts, now) if ts else "",
                "ts": ts or 0, "disabled": item["disabled"], "skips": item["skips"], "edited": item["edited"],
                "exists": Path(item["mp4"]).exists()}
