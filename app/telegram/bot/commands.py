"""Текстові повідомлення та команди."""
from __future__ import annotations

import re
import threading
from app.core.config import NAMES
from app.core.config import fmt_slot
from app.core.meta import apply_manual
from .common import (
    BTN,
    HELP_ADMIN,
    HELP_VIEWER,
    VIEWER_CMDS,
    esc,
    log,
)


class CommandsMixin:
    def on_message(self, m: dict, chat: int, role: str) -> None:
        text = (m.get("text") or "").strip()
        if (m.get("photo") or m.get("document")) and role == "admin" \
                and (self.dialog_get(chat) or {}).get("step") == "thumb":
            return self.save_thumb(chat, m)
        if not text:
            return
        if text in BTN:
            text = BTN[text]
        self._last_incoming = {**(getattr(self, "_last_incoming", None) or {}), chat: int(m.get("message_id") or 0)}
        in_dialog = bool(self.dialog_get(chat) or self.db.get(f"await_title:{chat}"))
        if not in_dialog:  # нова дія — прибираємо попередні відповіді й натискання кнопок
            self.purge(chat)
            self.cleanup(chat)  # картки з черги, залишки діалогів
            self.ephemeral(chat, m.get("message_id"))
        if text.startswith("/"):
            cmd, *args = text.split()
            cmd = cmd.split("@")[0].lower()
            if role != "admin" and cmd not in VIEWER_CMDS:
                return self.say(chat, "⛔ Ця дія доступна лише адміністратору.", html_mode=False)
            return self.command(cmd, args, chat, role)
        if role != "admin":
            return self.say(chat, HELP_VIEWER, reply_markup=self.reply_keyboard(chat))
        if self.dialog_get(chat) or self.db.get(f"await_title:{chat}"):
            self._track(chat, m.get("message_id"))
        if self.dialog_get(chat):
            return self.dialog_answer(chat, text)
        await_id = self.db.get(f"await_title:{chat}")
        if await_id:
            item_id = int(await_id)
            self.db.set(f"await_title:{chat}", None)
            item = self.db.get_item(item_id)
            if not item or item["status"] not in ("notified", "approved"):
                self.cleanup(chat)
                return self.say(chat, "Цей шортс уже неактуальний — назву не змінено.", html_mode=False)
            title = re.sub(r"\s+", " ", text)[:100]
            edited = [x for x in item["edited"] if x != "назву"] + ["назву"]
            try:  # назва — для всіх платформ (YouTube, TikTok, Instagram, Facebook), хештеги лишаються
                apply_manual(item["json"], title, self._raw_meta(item).hashtags)
                self.db.update_item(item_id, title_override=None, edited=edited)
            except Exception as e:  # noqa: BLE001
                log.warning("Назва #%s лише для YouTube: %s", item_id, e)
                self.db.update_item(item_id, title_override=title, edited=edited)
            self.db.event(item_id, "edited", {"title": title})
            self.refresh(item_id)  # нова назва — у підписі відео
            return self.cleanup(chat)
        self.say(chat, HELP_ADMIN, reply_markup=self.reply_keyboard(chat))

    def command(self, cmd: str, args: list[str], chat: int | None = None, role: str = "admin") -> None:
        app = self.app
        chat = chat or self.chat_id
        reply = lambda text, html_mode=True, kb=None, keep=False: self.say(chat, text, html_mode, kb, keep)  # noqa: E731
        if cmd in ("/stats", "/top"):
            wait = self.task_start(chat, "⏳ Збираю статистику з платформ…")

            def work(which=cmd):
                from app.insights.reports import collect, weekly_top
                from app.insights import analytics
                kb = None
                try:
                    stats = collect(app)
                    if which == "/stats":  # доба + кнопки періодів (тиждень … рік — з бази, миттєво)
                        text = analytics.period_report(self.cfg, self.db, 1, list(app.platforms))
                        kb = analytics.keyboard(1)
                    else:
                        text = weekly_top(app, stats)
                        kb = analytics.keyboard(analytics.TOP)
                except Exception as e:  # noqa: BLE001
                    log.exception("Аналітика")
                    text = f"❌ Не вдалося зібрати статистику: {esc(str(e))}"
                self.task_done(chat, wait, text, kb)  # сповіщення «⏳…» перетворюється на результат
            if getattr(app.publisher, "sync", False):
                work()
            else:
                threading.Thread(target=work, daemon=True, name="stats-cmd").start()
            return
        if cmd == "/menu":
            return reply("Кнопки керування — під полем введення ⬇️", False, self.reply_keyboard(chat))
        if cmd in ("/start", "/help"):
            reply(HELP_ADMIN if role == "admin" else HELP_VIEWER, kb=self.reply_keyboard(chat))
        elif cmd == "/cancel":
            self.db.set(f"await_title:{chat}", None)
            self.db.set(f"dialog:{chat}", None)
            self.cleanup(chat)
            reply("Скасовано.", False)
        elif cmd == "/queue":
            kb = self.queue_keyboard(0) if role == "admin" else None
            reply(app.queue_text() + ("\n\n👇 Оберіть відео, щоб опублікувати його коли захочете" if kb else ""), kb=kb)
        elif cmd == "/status":
            reply(app.status_text())
        elif cmd == "/eu":
            from app.channels import eu_tab
            if not eu_tab.available(self.cfg):
                return reply("🇬🇧 EU-канал ще не створено — панель керування → вкладка «🇬🇧 EN-канал» → «Створити».", False)
            wait = self.task_start(chat, "⏳ Читаю чергу EU-каналу…")

            def work():
                try:
                    text, kb = eu_tab.view(home_tz=self.cfg["tz"])
                except Exception as e:  # noqa: BLE001
                    log.exception("EU-канал")
                    text, kb = f"❌ Не вдалося прочитати EU-канал: {esc(str(e))}", None
                self.task_done(chat, wait, text, kb)
            if getattr(app.publisher, "sync", False):
                work()
            else:
                threading.Thread(target=work, daemon=True, name="eu-cmd").start()
        elif cmd == "/report":
            from app.insights.reports import daily_report
            reply(daily_report(app))
        elif cmd == "/stop":
            self.db.set("stopped", "1")
            self.db.event(None, "stopped", {"by": chat})
            self.refresh_publishing()
            self.send("⛔ <b>Публікації зупинено.</b> Нічого не публікується — навіть підтверджене чи «⚡ зараз». "
                      "Instagram, що зараз обробляється, зупиниться за кілька секунд (YouTube/Facebook — щойно дозавантажиться файл).\n"
                      "Відновити — «✅ Відновити публікації».", html_mode=True, keyboard=True)
        elif cmd == "/go":
            self.db.set("stopped", None)
            self.db.event(None, "resumed_publishing", {"by": chat})
            self.refresh_publishing()
            self.send("✅ Публікації відновлено. Пропущені слоти: підтверджені шортси (до 2 год тому) вийдуть одразу, "
                      "решта — повернуться на початок черги.", keyboard=True)
        elif cmd == "/pause":
            self.db.set("paused", "1")
            self.db.event(None, "paused", {"by": chat})
            self.send("⏸ Розклад призупинено: нові шортси не надсилатимуться, слоти лишатимуться порожніми.\n"
                      "Уже підтверджені шортси буде опубліковано. Відновити — «▶️ Продовжити».", keyboard=True)
        elif cmd == "/resume":
            self.db.set("paused", None)
            self.db.event(None, "resumed", {"by": chat})
            self.send("▶️ Розклад відновлено.", keyboard=True)
        elif cmd == "/clean":
            self.clean_chat(chat, latest=(self._last_incoming or {}).get(chat))
        elif cmd == "/log":
            if role != "admin":
                return reply("⛔ Лише для адміністратора.", False)
            text, kb = self.log_text(" ".join(args), only_errors=not args)
            reply(text, kb=kb)
        elif cmd == "/update":
            if role != "admin":
                return reply("⛔ Лише для адміністратора.", False)
            from app.core.config import VERSION
            wait = self.task_start(chat, f"🔍 Перевіряю оновлення… (у вас {VERSION})")

            def work():
                try:
                    text, kb = self.update_now(force=False)
                except Exception as e:  # noqa: BLE001
                    text, kb = f"⚠️ Оновлення не вдалося: {e}", None
                self.task_done(chat, wait, esc(text), kb)
            self._bg(work)
        elif cmd == "/versions":
            if role != "admin":
                return reply("⛔ Лише для адміністратора.", False)
            wait = self.task_start(chat, "🕘 Завантажую список версій…")

            def show_versions():
                try:
                    text, kb = self.versions_text()
                except Exception as e:  # noqa: BLE001
                    text, kb = f"⚠️ Не вдалося отримати версії: {esc(str(e))}", None
                self.task_done(chat, wait, text, kb)
            self._bg(show_versions)
        elif cmd == "/stream":
            if role != "admin":
                return reply("⛔ Лише для адміністратора.", False)
            text, kb = self.stream_text()
            reply(text, kb=kb)
        elif cmd == "/failed":
            self._bg(self.failed_list, chat)
        elif cmd == "/igcheck":
            if role != "admin":
                return reply("⛔ Лише для адміністратора.", False)
            wait = self.task_start(chat, "🔬 Питаю в Meta про доступ Instagram…")

            def ig_check():
                try:
                    from app.platforms.meta_graph import Instagram
                    plat = self.app.platforms.get("instagram") or Instagram(self.cfg)
                    lines = plat.api_report()
                    text = "🔬 <b>Instagram API — перевірка доступу</b>\n" + "\n".join(f"• {esc(x)}" for x in lines)
                    if args and args[0].lower() in ("test", "тест"):
                        try:
                            self.tg.edit_text(chat, (wait or {})["message_id"], text + "\n\n🧪 Тестове відео (без публікації)…")
                        except Exception:  # noqa: BLE001
                            pass
                        from pathlib import Path as _P
                        test = plat.test_video(_P(self.cfg["paths"]["data"]) / "previews")
                        text += "\n\n🧪 <b>Тестове відео (без публікації)</b>\n" + "\n".join(f"• {esc(x)}" for x in test)
                    else:
                        text += "\n\nТест відео без публікації: <code>/igcheck test</code>"
                except Exception as e:  # noqa: BLE001
                    text = f"⚠️ Перевірка не вдалась: {esc(str(e))}"
                self.task_done(chat, wait, text)
            self._bg(ig_check)
        elif cmd == "/platforms":
            if role != "admin":
                return reply("⛔ Лише для адміністратора.", False)
            text, kb = self.platforms_text()
            reply(text, kb=kb)
        elif cmd == "/users":
            text, kb = self.users_text()
            reply(text, kb=kb if kb["inline_keyboard"] else None)
        elif cmd == "/retry":
            if not args or not args[0].lstrip("#").isdigit():
                return reply("Використання: /retry N", False)
            item_id = int(args[0].lstrip("#"))
            item = self.db.get_item(item_id)
            if not item or item["status"] not in ("failed", "posted"):
                return reply("Повторити можна лише шортс зі статусом «не опубліковано» або частково опублікований.", False)
            pending = [p for p in app.publisher.targets(item) if item["results"].get(p, {}).get("status") != "ok"]
            if not pending:
                return reply("Усі платформи для цього шортса вже успішні.", False)
            reply(f"🔁 Повторюю #{item_id}: {', '.join(NAMES[p] for p in pending)}", False)
            app.publisher.start(item_id)
        elif cmd == "/testslot":
            minutes = int(args[0]) if args and args[0].isdigit() else 10
            ts = app.scheduler.add_manual_slot(minutes)
            reply(f"🧪 Додано позаплановий слот о {fmt_slot(self.cfg, ts)}. "
                  "Сповіщення прийде протягом хвилини (якщо черга не порожня).", False)
        else:
            reply(HELP_ADMIN if role == "admin" else HELP_VIEWER, kb=self.reply_keyboard(chat))
