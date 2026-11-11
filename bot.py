"""Telegram-бот: сповіщення-відео з кнопками підтвердження, команди, доступ для кількох людей.

Ролі:
  admin  — отримує все й керує (кнопки під шортсами, команди, запити на доступ);
           головні адміністратори задаються в .env (TELEGRAM_CHAT_ID, можна кілька через кому);
  viewer — отримує шортси (без кнопок), опубліковані посилання, звіти;
  pending/blocked — запит на доступ / відхилено. Усіх інших бот ігнорує.
"""
from __future__ import annotations

import html
import json
import logging
import re
import threading
from pathlib import Path

import media
from config import NAMES, PLATFORMS, SHORT, enabled_platforms, fmt_slot, fmt_when, now_ts, parse_when
from meta import PostMeta, apply_always_hashtags, apply_manual, load_meta, parse_hashtags
from telegram_api import TelegramAPI, TelegramError

log = logging.getLogger("bot")

esc = html.escape
CAPTION_LIMIT = 1024

HELP_ADMIN = (
    "🤖 <b>poster</b> — автопублікація шортсів\n\n"
    "Кнопки внизу екрана — основне керування. Команди:\n"
    "/queue — черга\n"
    "/status — найближчі слоти й доступи\n"
    "/report — підсумок за сьогодні\n"
    "/testslot [хв] — позаплановий слот через N хв (типово 10)\n"
    "/pause, /resume — призупинити / відновити розклад\n"
    "/retry N — повторити невдалі платформи для шортса #N\n"
    "/users — хто має доступ до бота\n"
    "/stats — аналітика по платформах, /top — лідери тижня\n"
    "/menu — показати кнопки\n"
    "/cancel — скасувати введення назви"
)
HELP_VIEWER = (
    "🤖 <b>poster</b>\n\nВи отримуватимете шортси перед публікацією, посилання на пости та звіти.\n"
    "/queue — черга\n/status — найближчі слоти\n/report — підсумок дня"
)

BTN = {
    "📋 Черга": "/queue", "🕐 Статус": "/status", "📊 Звіт": "/report",
    "🧪 Тест-слот": "/testslot", "⏸ Пауза": "/pause", "▶️ Продовжити": "/resume",
    "👥 Доступ": "/users", "❓ Довідка": "/help", "📈 Аналітика": "/stats", "🏆 Топ тижня": "/top",
}
VIEWER_CMDS = {"/start", "/help", "/menu", "/queue", "/status", "/report", "/stats", "/top"}

# Кнопка «Меню» біля поля введення (setMyCommands)
COMMANDS = [
    ("menu", "Показати кнопки керування"),
    ("queue", "Черга шортсів"),
    ("status", "Найближчі слоти й доступи"),
    ("report", "Підсумок за сьогодні"),
    ("stats", "Аналітика: підписники, перегляди, лайки"),
    ("top", "Лідери тижня"),
    ("testslot", "Тестовий слот через 10 хв"),
    ("pause", "Призупинити розклад"),
    ("resume", "Відновити розклад"),
    ("users", "Хто має доступ"),
    ("help", "Довідка"),
]


def visible_len(text: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def parse_ids(value: str) -> list[int]:
    return [int(x) for x in re.findall(r"-?\d+", value or "")]


def user_name(u: dict) -> str:
    name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or "без імені"
    return f"{name} (@{u['username']})" if u.get("username") else name


class Bot:
    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.db = app.db
        self.tg = TelegramAPI(self.cfg["secrets"]["TELEGRAM_BOT_TOKEN"])
        self.owners = parse_ids(self.cfg["secrets"]["TELEGRAM_CHAT_ID"])
        if not self.owners:
            raise TelegramError("Не задано TELEGRAM_CHAT_ID")
        bot_id = self.cfg["secrets"]["TELEGRAM_BOT_TOKEN"].split(":")[0]
        for cid in self.owners:
            if str(cid) == bot_id:
                raise TelegramError("TELEGRAM_CHAT_ID — це id самого бота; потрібен ваш id (@userinfobot)")
            u = self.db.user(cid)
            if not u or u["role"] != "admin":
                self.db.set_user(cid, "admin", (u or {}).get("name") or "власник")

    # ================= ролі =================
    @property
    def chat_id(self) -> int:  # головний адміністратор
        return self.owners[0]

    def admins(self) -> list[int]:
        ids = list(self.owners)
        ids += [u["chat_id"] for u in self.db.users("admin") if u["chat_id"] not in ids]
        return ids

    def recipients(self) -> list[int]:
        ids = self.admins()
        ids += [u["chat_id"] for u in self.db.users("viewer") if u["chat_id"] not in ids]
        return ids

    def role(self, chat_id: int) -> str | None:
        if chat_id in self.owners:
            return "admin"
        u = self.db.user(chat_id)
        return u["role"] if u else None

    def paused(self) -> bool:
        return self.db.get("paused") == "1"

    def reply_keyboard(self, chat_id: int) -> dict:
        if self.role(chat_id) == "admin":
            pause = "▶️ Продовжити" if self.paused() else "⏸ Пауза"
            rows = [["📋 Черга", "🕐 Статус", "📊 Звіт"], ["📈 Аналітика", "🏆 Топ тижня", "🧪 Тест-слот"],
                    [pause, "👥 Доступ", "❓ Довідка"]]
        else:
            rows = [["📋 Черга", "🕐 Статус", "📊 Звіт"], ["📈 Аналітика", "🏆 Топ тижня"]]
        return {"keyboard": [[{"text": t} for t in r] for r in rows], "resize_keyboard": True,
                "is_persistent": True}

    def setup_commands(self) -> None:
        """Кнопка «Меню» в Telegram зі списком команд (best-effort)."""
        try:
            self.tg.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
            self.tg.call("setChatMenuButton", menu_button={"type": "commands"})
        except TelegramError as e:
            log.warning("Не вдалося налаштувати меню команд: %s", e)

    # ================= службові повідомлення =================
    def say(self, chat_id: int, text: str, html_mode: bool = True, reply_markup=None):
        try:
            return self.tg.send_message(chat_id, text if html_mode else esc(text), reply_markup=reply_markup)
        except TelegramError as e:
            log.error("Не вдалося надіслати повідомлення в Telegram (%s): %s", chat_id, e)
            return None

    def send(self, text: str, html_mode: bool = False, to: str = "admins", keyboard: bool = False) -> None:
        """to: 'admins' — службові повідомлення; 'all' — також глядачам (звіти, перегляди)."""
        for cid in (self.recipients() if to == "all" else self.admins()):
            self.say(cid, text, html_mode, self.reply_keyboard(cid) if keyboard else None)

    def notify_reauth(self, key: str, reason: str) -> None:
        k = f"reauth_{key}"
        last = int(self.db.get(k) or 0)
        if now_ts() - last < 6 * 3600:
            return
        self.db.set(k, now_ts())
        hint = self.app.platforms[key].reauth_hint if key in self.app.platforms else f"python poster.py auth {key}"
        self.send(f"🔑 <b>Потрібна повторна авторизація: {NAMES[key]}</b>\n"
                  f"Причина: {esc(reason[:300])}\n\nНа ПК: панель керування → {NAMES[key]} → «Авторизувати»\n"
                  f"або команда <code>{esc(hint)}</code>", html_mode=True)

    # ================= сповіщення про шортс =================
    def _meta(self, item: dict) -> PostMeta:
        try:
            return apply_always_hashtags(load_meta(item["json"], item["title_override"]), self.cfg)
        except Exception as e:  # noqa: BLE001
            log.warning("Метадані #%s недоступні: %s", item["id"], e)
            return PostMeta(yt_title=item["title_override"] or Path(item["mp4"]).stem)

    def _raw_meta(self, item: dict) -> PostMeta:
        """Метадані без обов'язкових хештегів — те, що вводить/залишає користувач."""
        try:
            return load_meta(item["json"], item["title_override"])
        except Exception:  # noqa: BLE001
            return PostMeta(yt_title=item["title_override"] or Path(item["mp4"]).stem)

    def _always_line(self) -> str:
        conf = self.cfg.get("always_hashtags") or {}
        return " ".join(conf.get("tags") or []) if conf.get("in_title", True) else ""

    def _platform_line(self, item: dict) -> str:
        def mark(p: str) -> str:
            if p in item["disabled"]:
                return "🚫"
            if p == "youtube":
                priv = self.cfg["platforms"]["youtube"].get("privacy", "public")
                return {"private": "✅🔒", "unlisted": "✅🔗"}.get(priv, "✅")
            if p == "tiktok" and not self.cfg["platforms"]["tiktok"].get("public"):
                return "✅🔒"
            return "✅"
        parts = [f"{SHORT[p]} {mark(p)}" for p in enabled_platforms(self.cfg)]
        return " ".join(parts) or "—"

    def _status_block(self, item: dict) -> str:
        st = item["status"]
        slot = fmt_slot(self.cfg, item["slot_at"]) if item["slot_at"] else "—"
        edited = f"\n✏️ Змінено: {', '.join(item['edited'])}" if item["edited"] else ""
        if st == "notified":
            return f"⏳ Очікує підтвердження до {slot}{edited}"
        if st == "approved":
            return f"👍 Підтверджено — публікація о {slot}{edited}"
        if st == "publishing":
            return "⏫ Публікується…" + self._results(item)
        if st == "posted":
            return f"✅ Опубліковано{edited}" + self._results(item)
        if st == "failed":
            return "❌ Не опубліковано" + self._results(item) + f"\nПовторити: /retry {item['id']}"
        if st == "queued":
            if item["note"]:
                return esc(item["note"])
            return f"⏭ Пропущено ({item['skips']}/{self.cfg['max_skips']}) — шортс переміщено в кінець черги"
        if st == "skipped":
            return f"⏭ Пропущено {item['skips']} рази — файли перенесено в skipped/"
        if st == "missing":
            return "⚠️ Файл не знайдено — шортс прибрано з черги"
        return st

    def _results(self, item: dict) -> str:
        lines = []
        for p in PLATFORMS:
            r = item["results"].get(p)
            if p in item["disabled"]:
                if p in self.app.platforms:
                    lines.append(f"{SHORT[p]}: 🚫 вимкнено")
                continue
            if not r:
                continue
            if r.get("status") == "ok":
                lock = {"SELF_ONLY": " 🔒 приватно", "private": " 🔒 приватне",
                        "unlisted": " 🔗 за посиланням"}.get(r.get("privacy"), "")
                link = f'<a href="{esc(r["url"])}">{NAMES[p]}</a>' if r.get("url") else f"{NAMES[p]} ✅"
                lines.append(f"{SHORT[p]}: {link}{lock}")
            else:
                lines.append(f"{SHORT[p]}: ❌ {esc(str(r.get('error', ''))[:120])}")
        return ("\n" + "\n".join(lines)) if lines else ""

    def caption(self, item: dict, meta: PostMeta | None = None) -> str:
        meta = meta or self._meta(item)
        slot = fmt_slot(self.cfg, item["slot_at"]) if item["slot_at"] else "—"
        head = f"🎬 <b>{esc(meta.yt_title)}</b>"
        tags = esc(" ".join(meta.hashtags))
        hint = ""
        if item["status"] == "notified" and self.cfg.get("ask_texts_before_publish"):
            hint = "\n✍️ Натисніть «🚀 Опублікувати» — бот попросить назву й хештеги"
        elif item["status"] in ("notified", "approved") and "назву" not in item["edited"]:
            if meta.ai:
                hint = "\n🤖 Тексти від ШІ — перевірте; інший варіант: «🔄 Перегенерувати тексти»"
            elif meta.auto:
                hint = "\nℹ️ Назва з імені файлу — змінити: «✏️ Ввести назву вручну»"
        tail = (f"🕐 Слот: {slot} | Платформи: {self._platform_line(item)}\n\n"
                f"{self._status_block(item)}{hint}")
        text = "\n".join(x for x in (head, tags, tail) if x)
        if visible_len(text) > CAPTION_LIMIT and tags:
            room = CAPTION_LIMIT - visible_len(head + "\n\n" + tail) - 2
            short = " ".join(meta.hashtags)[:max(0, room)].rsplit(" ", 1)[0] if room > 0 else ""
            text = "\n".join(x for x in (head, esc(short), tail) if x)
        if visible_len(text) > CAPTION_LIMIT:
            text = text[:CAPTION_LIMIT - 1] + "…"
            text = re.sub(r"<[^>]*$", "", text)
        return text

    def texts(self, item: dict, meta: PostMeta | None = None, quality: str = "") -> str:
        meta = meta or self._meta(item)
        private = "" if self.cfg["platforms"]["tiktok"].get("public") else " (приватно)"
        parts = [f"📝 Тексти для шортса #{item['id']}"]
        if quality:
            parts.append(quality)
        if "tiktok" in self.app.platforms:
            parts.append(f"🎵 <b>TikTok</b>{private}:\n{esc(meta.tiktok_text[:1300])}")
        same = meta.facebook_text == meta.instagram_text
        if "instagram" in self.app.platforms or "facebook" in self.app.platforms:
            parts.append(f"📸 <b>Instagram{' + Facebook' if same else ''}</b>:\n{esc(meta.instagram_text[:1300])}")
        if not same and "facebook" in self.app.platforms:
            parts.append(f"📘 <b>Facebook</b>:\n{esc(meta.facebook_text[:1000])}")
        return "\n\n".join(parts)[:4096]

    def keyboard(self, item: dict) -> dict | None:
        if item["status"] not in ("notified", "approved"):
            return None
        i = item["id"]
        first = ({"text": "🚀 Опублікувати", "callback_data": f"pub:{i}"} if item["status"] == "notified"
                 else {"text": "↩️ Скасувати підтвердження", "callback_data": f"unpub:{i}"})
        toggles = [{"text": f"{SHORT[p]} {'🚫' if p in item['disabled'] else '✅'}",
                    "callback_data": f"tog:{i}:{p}"} for p in enabled_platforms(self.cfg)]
        rows = [[first],
                [{"text": "🕐 Змінити час / дату", "callback_data": f"time:{i}"}],
                [{"text": "🔄 Перегенерувати тексти", "callback_data": f"regen:{i}"}],
                [{"text": "✏️ Ввести назву вручну", "callback_data": f"title:{i}"}]]
        if toggles:
            rows.append([{"text": "Вимкнути платформу:", "callback_data": f"noop:{i}"}])
            rows.append(toggles)
        return {"inline_keyboard": rows}

    def notify_item(self, item_id: int) -> None:
        """Надсилає сам шортс (відеоповідомлення) + тексти всім отримувачам; кнопки — лише адміністраторам."""
        item = self.db.get_item(item_id)
        meta = self._meta(item)
        try:
            q = media.quality(item["mp4"], self.cfg["ffprobe"])
            qline = f"📐 <b>Оригінал:</b> {esc(media.quality_line(q))}\nНа платформи завантажується саме він — без перекодування."
            if q["size_mb"] * 1024 * 1024 > media.TG_LIMIT:
                qline += ("\nℹ️ У цьому чаті — копія, стиснута до 50 МБ (ліміт Telegram для ботів); "
                          "на YouTube/TikTok/Instagram/Facebook іде оригінал.")
        except OSError:
            qline = ""
        caption, kb, texts = self.caption(item, meta), self.keyboard(item), self.texts(item, meta, qline)
        admins = self.admins()
        self.db.clear_tg_msgs(item_id)
        video, info, file_id, video_error = None, {}, None, None
        try:
            video = media.telegram_copy(item["mp4"], self.cfg)
            info = media.probe(video, self.cfg["ffprobe"])
        except (media.MediaError, OSError) as e:
            video_error = e
        delivered_admin = False
        for cid in self.recipients():
            is_admin = cid in admins
            try:
                is_text, msg = 0, None
                if video_error is None:
                    try:
                        msg = self.tg.send_video(cid, file_id or video, caption, kb if is_admin else None,
                                                 width=info.get("width"), height=info.get("height"),
                                                 duration=info.get("duration"))
                        file_id = file_id or ((msg or {}).get("video") or {}).get("file_id")
                    except TelegramError as e:
                        video_error = e
                if msg is None:
                    log.error("Не вдалося надіслати відео #%s: %s", item_id, video_error)
                    msg = self.tg.send_message(cid, caption, reply_markup=kb if is_admin else None)
                    if is_admin:
                        self.say(cid, f"⚠️ Відео #{item_id} не вдалося надіслати в чат: {esc(str(video_error))}")
                    is_text = 1
                text_msg = self.tg.send_message(cid, texts, reply_to=msg["message_id"])
                self.db.set_tg_msg(item_id, cid, msg["message_id"], (text_msg or {}).get("message_id"), is_text)
                if is_admin:
                    delivered_admin = True
                    if cid == self.chat_id:
                        self.db.update_item(item_id, tg_msg=msg["message_id"], tg_msg_is_text=is_text,
                                            tg_text_msg=(text_msg or {}).get("message_id"))
            except TelegramError as e:
                log.error("Сповіщення #%s для %s не доставлено: %s", item_id, cid, e)
        if not delivered_admin:
            raise TelegramError("сповіщення не доставлено жодному адміністратору")

    def refresh(self, item_id: int, texts: bool = False) -> None:
        item = self.db.get_item(item_id)
        if not item:
            return
        rows = self.db.tg_msgs(item_id)
        if not rows:
            return
        meta = self._meta(item)
        cap, kb = self.caption(item, meta), self.keyboard(item)
        txt = self.texts(item, meta) if texts else None
        admins = set(self.admins())
        for r in rows:
            markup = kb if r["chat_id"] in admins else None
            try:
                if r["is_text"]:
                    self.tg.edit_text(r["chat_id"], r["msg_id"], cap, markup)
                else:
                    self.tg.edit_caption(r["chat_id"], r["msg_id"], cap, markup)
                if txt and r["text_msg_id"]:
                    self.tg.edit_text(r["chat_id"], r["text_msg_id"], txt)
            except TelegramError as e:
                log.warning("Не вдалося оновити повідомлення #%s у %s: %s", item_id, r["chat_id"], e)

    # ================= вхідні оновлення =================
    def poll_once(self, timeout: int = 15) -> None:
        offset = self.db.get("tg_offset")
        updates = self.tg.get_updates(int(offset) if offset else None, timeout)
        for u in updates:
            self.db.set("tg_offset", u["update_id"] + 1)
            try:
                self.handle(u)
            except Exception:  # noqa: BLE001
                log.exception("Помилка обробки оновлення %s", u.get("update_id"))

    def handle(self, u: dict) -> None:
        if "callback_query" in u:
            cq = u["callback_query"]
            chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
            if self.role(chat) != "admin":
                log.warning("Callback від chat_id=%s без прав адміністратора — ігнорую", chat)
                self.tg.answer_callback(cq["id"], "⛔ Лише для адміністратора")
                return
            self.on_callback(cq, chat)
        elif "message" in u:
            m = u["message"]
            chat = (m.get("chat") or {}).get("id")
            if (m.get("chat") or {}).get("type", "private") != "private" and chat not in self.recipients():
                return
            role = self.role(chat)
            if role in ("admin", "viewer"):
                self.on_message(m, chat, role)
            elif role != "blocked":
                self.access_request(m, chat, role)

    # ---------- доступ ----------
    def access_request(self, m: dict, chat: int, role: str | None) -> None:
        if not (m.get("text") or "").startswith("/start"):
            if role is None:
                self.say(chat, "Щоб отримати доступ до цього бота, надішліть /start", html_mode=False)
            return
        name = user_name(m.get("from") or {})
        if role == "pending":
            self.say(chat, "⏳ Запит на доступ уже надіслано власнику. Зачекайте.", html_mode=False)
            return
        self.db.set_user(chat, "pending", name)
        log.info("Запит на доступ від %s (%s)", chat, name)
        self.say(chat, "📨 Запит на доступ надіслано власнику бота. Ви отримаєте повідомлення, коли його схвалять.",
                 html_mode=False)
        kb = {"inline_keyboard": [[
            {"text": "👁 Сповіщення", "callback_data": f"acc:{chat}:viewer"},
            {"text": "🛠 Адмін", "callback_data": f"acc:{chat}:admin"},
            {"text": "❌ Відхилити", "callback_data": f"acc:{chat}:blocked"}]]}
        for cid in self.admins():
            self.say(cid, f"👤 <b>Запит на доступ</b>\n{esc(name)}\nchat_id: <code>{chat}</code>\n\n"
                          "👁 Сповіщення — отримуватиме шортси, посилання і звіти (без керування)\n"
                          "🛠 Адмін — повне керування", reply_markup=kb)

    def set_access(self, chat: int, role: str, by: int | None = None) -> str:
        if chat in self.owners:
            return "Головного адміністратора змінити не можна (він задається в .env)"
        u = self.db.user(chat)
        name = (u or {}).get("name") or str(chat)
        if role == "delete":
            self.db.del_user(chat)
            self.say(chat, "🔒 Ваш доступ до бота скасовано.", html_mode=False,
                     reply_markup={"remove_keyboard": True})
            return f"🗑 {name}: доступ скасовано"
        self.db.set_user(chat, role)
        if role in ("admin", "viewer"):
            what = "адміністратор (повне керування)" if role == "admin" else "сповіщення та звіти"
            self.say(chat, f"✅ Доступ надано: {what}.\n\n" + (HELP_ADMIN if role == "admin" else HELP_VIEWER),
                     reply_markup=self.reply_keyboard(chat))
            return f"✅ {name}: {'адміністратор' if role == 'admin' else 'глядач'}"
        return f"❌ {name}: відхилено"

    def users_text(self) -> tuple[str, dict]:
        lines, rows = ["👥 <b>Доступ до бота</b>"], []
        titles = {"admin": "🛠 адмін", "viewer": "👁 глядач", "pending": "⏳ запит", "blocked": "🚫 відхилено"}
        for u in self.db.users():
            main = " (головний)" if u["chat_id"] in self.owners else ""
            lines.append(f"{titles.get(u['role'], u['role'])}: {esc(u['name'] or '')} <code>{u['chat_id']}</code>{main}")
            if u["chat_id"] in self.owners:
                continue
            cid, label = u["chat_id"], (u["name"] or str(u["chat_id"]))[:20]
            if u["role"] in ("pending", "blocked"):
                rows.append([{"text": f"👁 {label}", "callback_data": f"acc:{cid}:viewer"},
                             {"text": "🛠 Адмін", "callback_data": f"acc:{cid}:admin"},
                             {"text": "🗑", "callback_data": f"acc:{cid}:delete"}])
            else:
                other = "viewer" if u["role"] == "admin" else "admin"
                rows.append([{"text": f"{'👁' if other == 'viewer' else '🛠'} {label}", "callback_data": f"acc:{cid}:{other}"},
                             {"text": "🗑 Прибрати", "callback_data": f"acc:{cid}:delete"}])
        lines.append("\nЩоб додати людину — хай знайде бота і надішле /start. Запит прийде сюди.")
        return "\n".join(lines), {"inline_keyboard": rows}

    # ---------- кнопки під повідомленнями ----------
    def on_callback(self, cq: dict, chat: int) -> None:
        answer = lambda text="", alert=False: self.tg.answer_callback(cq["id"], text, alert)  # noqa: E731
        try:
            action, sid, *rest = (cq.get("data") or "").split(":")
            target = int(sid)
        except ValueError:
            return answer()
        if action == "acc" and rest:
            result = self.set_access(target, rest[0], by=chat)
            answer(result[:200])
            try:
                text, kb = self.users_text()
                if "Запит на доступ" in ((cq.get("message") or {}).get("text") or ""):
                    self.tg.edit_text(chat, cq["message"]["message_id"], esc(result))
                else:
                    self.tg.edit_text(chat, cq["message"]["message_id"], text, kb)
            except TelegramError as e:
                log.debug("edit users: %s", e)
            return
        if action == "noop":
            return answer("Натисніть на платформу нижче, щоб вимкнути/увімкнути її")
        if action in ("keep", "dlgx"):
            return self.dialog_button(chat, target, action, rest, answer)
        if action in ("time", "tk", "ts", "tc", "tn", "tx"):
            return self.time_button(chat, target, action, rest, answer)
        item_id = target
        item = self.db.get_item(item_id)
        own = self.db.tg_msg(item_id, chat)
        if (not item or item["status"] not in ("notified", "approved")
                or not own or cq["message"]["message_id"] != own["msg_id"]):
            return answer("Це сповіщення вже неактуальне", True)

        if action == "pub":
            if not self.app.publisher.targets(item):
                return answer("Усі платформи вимкнено — нічого публікувати", True)
            if item["status"] != "notified":
                return answer()
            if self.cfg.get("ask_texts_before_publish"):
                answer("Введіть назву й хештеги")
                return self.dialog_start(chat, item_id)
            self.db.update_item(item_id, status="approved")
            self.db.event(item_id, "approved", {"by": chat})
            if self.cfg.get("publish_on_approve"):
                answer("Публікую зараз…")
                self.app.publisher.start(item_id)
            else:
                answer(f"Підтверджено. Публікація о {fmt_slot(self.cfg, item['slot_at'])}")
                self.refresh(item_id)
        elif action == "unpub":
            if item["status"] == "approved" and not self.app.publisher.is_active(item_id):
                self.db.update_item(item_id, status="notified")
                self.refresh(item_id)
            answer("Підтвердження скасовано")
        elif action == "tog" and rest and rest[0] in PLATFORMS:
            p = rest[0]
            disabled = [x for x in item["disabled"] if x != p] if p in item["disabled"] else item["disabled"] + [p]
            self.db.update_item(item_id, disabled=disabled)
            self.refresh(item_id)
            answer(f"{NAMES[p]}: {'вимкнено' if p in disabled else 'увімкнено'}")
        elif action == "title":
            self.db.set(f"await_title:{chat}", item_id)
            self.tg.send_message(chat, f"✏️ Надішліть нову назву для YouTube (шортс #{item_id}, "
                                       "до 100 символів). /cancel — скасувати",
                                 reply_markup={"force_reply": True, "input_field_placeholder": "Нова назва"})
            answer()
        elif action == "regen":
            import ai
            if self.cfg.get("regenerate_command"):
                answer("Перегенеровую тексти…")
                threading.Thread(target=self._regen, args=(item_id,), daemon=True).start()
            elif ai.enabled(self.cfg):
                answer("🤖 ШІ пише новий варіант…")
                threading.Thread(target=self._regen_ai, args=(item_id,), daemon=True).start()
            else:
                return answer("Підключіть безкоштовне ШІ: панель керування → 🤖 ШІ", True)
        else:
            answer()

    # ---------- діалог «назва + хештеги» перед публікацією ----------
    def dialog_get(self, chat: int) -> dict | None:
        raw = self.db.get(f"dialog:{chat}")
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def dialog_set(self, chat: int, state: dict | None) -> None:
        self.db.set(f"dialog:{chat}", json.dumps(state, ensure_ascii=False) if state else None)

    def dialog_start(self, chat: int, item_id: int) -> None:
        item = self.db.get_item(item_id)
        meta = self._raw_meta(item)
        self.dialog_set(chat, {"item": item_id, "step": "title"})
        kb = {"inline_keyboard": [
            [{"text": f"Залишити: {meta.yt_title[:40]}", "callback_data": f"keep:{item_id}:title"}],
            [{"text": "❌ Скасувати", "callback_data": f"dlgx:{item_id}"}]]}
        always = self._always_line()
        note = f"\nℹ️ У кінець назви автоматично додаються: {esc(always)}" if always else ""
        self.say(chat, f"✍️ <b>Крок 1/2.</b> Надішліть <b>назву</b> для шортса #{item_id}.\n"
                       f"Зараз: <i>{esc(meta.yt_title)}</i>{note}", reply_markup=kb)

    def _ask_tags(self, chat: int, item_id: int) -> None:
        meta = self._raw_meta(self.db.get_item(item_id))
        always = {t.lower() for t in (self._always_line().split())}
        current = " ".join(t for t in meta.hashtags if t.lower() not in always) or "—"
        kb = {"inline_keyboard": [
            [{"text": f"Залишити: {current[:40]}", "callback_data": f"keep:{item_id}:tags"}],
            [{"text": "❌ Скасувати", "callback_data": f"dlgx:{item_id}"}]]}
        self.say(chat, "#️⃣ <b>Крок 2/2.</b> Надішліть <b>хештеги</b> через пробіл — можна з # або без "
                       "(напр.: <code>wot танки shorts</code>). «-» — без хештегів.", reply_markup=kb)

    def _dialog_item(self, chat: int, item_id: int, allow_approved: bool = False) -> dict | None:
        item = self.db.get_item(item_id)
        ok = ("notified", "approved") if allow_approved else ("notified",)
        if not item or item["status"] not in ok:
            self.dialog_set(chat, None)
            self.say(chat, "Цей шортс уже неактуальний (слот минув або його опубліковано).", html_mode=False)
            return None
        return item

    def dialog_answer(self, chat: int, text: str) -> None:
        st = self.dialog_get(chat)
        item = self._dialog_item(chat, st["item"], allow_approved=st.get("mode") == "change")
        if not item:
            return
        if st["step"] in ("time", "time_custom"):
            return self._time_typed(chat, item, st, text)
        if st["step"] == "title":
            title = re.sub(r"\s+", " ", text).strip()[:100]
            if not title:
                return self.say(chat, "Назва порожня — надішліть текст.", html_mode=False)
            self.dialog_set(chat, {**st, "step": "tags", "title": title})
            return self._ask_tags(chat, item["id"])
        tags = parse_hashtags(text)
        if text.strip() not in ("-", "—") and not tags:
            return self.say(chat, "Не бачу хештегів — надішліть слова через пробіл або «-».", html_mode=False)
        if st["step"] == "tags":
            return self._dialog_finish(chat, item, st.get("title"), tags)

    def dialog_button(self, chat: int, item_id: int, action: str, rest: list, answer) -> None:
        st = self.dialog_get(chat)
        if action == "dlgx":
            self.dialog_set(chat, None)
            answer("Скасовано")
            return self.say(chat, "Скасовано. Шортс чекає на «🚀 Опублікувати» до свого слоту.", html_mode=False)
        if not st or st.get("item") != item_id:
            return answer("Цей запит уже неактуальний", True)
        item = self._dialog_item(chat, item_id)
        if not item:
            return answer()
        meta = self._raw_meta(item)
        if rest and rest[0] == "title" and st["step"] == "title":
            self.dialog_set(chat, {**st, "step": "tags", "title": meta.yt_title})
            answer("Назву залишено")
            return self._ask_tags(chat, item_id)
        if rest and rest[0] == "tags" and st["step"] == "tags":
            answer("Хештеги залишено")
            return self._dialog_finish(chat, item, st.get("title"), meta.hashtags)
        answer()

    def _dialog_finish(self, chat: int, item: dict, title: str | None, tags: list[str]) -> None:
        item_id = item["id"]
        title = title or self._raw_meta(item).yt_title
        try:
            apply_manual(item["json"], title, tags)
        except Exception as e:  # noqa: BLE001
            log.error("Не вдалося записати тексти #%s: %s", item_id, e)
            return self.say(chat, f"❌ Не вдалося зберегти тексти: {e}", html_mode=False)
        edited = [x for x in item["edited"] if x not in ("назву", "тексти")] + ["назву", "хештеги"]
        self.db.update_item(item_id, title_override=None, edited=edited)
        if len(tags) > 15:
            self.say(chat, "⚠️ YouTube ігнорує всі хештеги, якщо їх більше 15.", html_mode=False)
        self.dialog_set(chat, {"item": item_id, "step": "time", "mode": "dialog", "title": title, "tags": tags})
        self.refresh(item_id, texts=True)
        self._ask_time(chat, item_id, dialog=True)

    # ---------- вибір часу публікації ----------
    def _ask_time(self, chat: int, item_id: int, dialog: bool) -> None:
        item = self.db.get_item(item_id)
        now = now_ts()
        rows = []
        if item["slot_at"]:
            rows.append([{"text": f"✅ Залишити: {fmt_when(self.cfg, item['slot_at'], now)}",
                          "callback_data": f"tk:{item_id}"}])
        free = self.app.scheduler.future_slots(now, 8)
        btns = [{"text": fmt_when(self.cfg, ts, now), "callback_data": f"ts:{item_id}:{ts}"} for ts in free]
        rows += [btns[i:i + 2] for i in range(0, len(btns), 2)]
        extra = [{"text": "⌨️ Свій час", "callback_data": f"tc:{item_id}"}]
        if dialog or item["status"] == "approved":
            extra.append({"text": "⚡ Зараз", "callback_data": f"tn:{item_id}"})
        rows.append(extra)
        rows.append([{"text": "❌ Скасувати", "callback_data": f"dlgx:{item_id}" if dialog else f"tx:{item_id}"}])
        head = "🕐 <b>Крок 3/3. Коли публікуємо?</b>" if dialog else f"🕐 <b>Новий час для шортса #{item_id}</b>"
        self.say(chat, f"{head}\nОберіть вільний слот, «Свій час» (напр. <code>завтра 18:30</code>) або «Зараз».",
                 reply_markup={"inline_keyboard": rows})

    def time_button(self, chat: int, item_id: int, action: str, rest: list, answer) -> None:
        item = self.db.get_item(item_id)
        if not item or item["status"] not in ("notified", "approved"):
            return answer("Цей шортс уже неактуальний", True)
        st = self.dialog_get(chat)
        dialog = bool(st and st.get("item") == item_id and st.get("mode") == "dialog")
        if action == "time":
            if st and st.get("item") != item_id:
                self.dialog_set(chat, None)
            if not dialog:
                self.dialog_set(chat, {"item": item_id, "step": "time", "mode": "change"})
            answer()
            return self._ask_time(chat, item_id, dialog=dialog)
        if not st or st.get("item") != item_id or st.get("step") not in ("time", "time_custom"):
            return answer("Цей вибір уже неактуальний", True)
        if action == "tx":
            self.dialog_set(chat, None)
            return answer("Скасовано")
        if action == "tc":
            self.dialog_set(chat, {**st, "step": "time_custom"})
            answer()
            return self.say(chat, "⌨️ Напишіть дату й час: <code>18:30</code>, <code>завтра 18:30</code> "
                                  "або <code>13.11 18:30</code>")
        if action == "tn":
            answer("Публікую зараз…")
            return self._apply_time(chat, item, st, "now")
        if action == "tk":
            answer("Час залишено")
            return self._apply_time(chat, item, st, None)
        if action == "ts" and rest:
            try:
                return self._apply_time(chat, item, st, int(rest[0]), answer)
            except ValueError:
                return answer()
        answer()

    def _time_typed(self, chat: int, item: dict, st: dict, text: str) -> None:
        try:
            ts = parse_when(text, self.cfg)
        except ValueError as e:
            return self.say(chat, f"⚠️ {e}", html_mode=False)
        self._apply_time(chat, item, st, ts)

    def _apply_time(self, chat: int, item: dict, st: dict, choice, answer=None) -> None:
        """choice: timestamp — новий час; None — залишити; 'now' — опублікувати зараз."""
        item_id = item["id"]
        if isinstance(choice, int):
            try:
                self.app.scheduler.move_slot(item_id, choice)
            except ValueError as e:
                if answer:
                    answer(str(e), True)
                return self.say(chat, f"⚠️ {e}. Оберіть інший час.", html_mode=False)
            if answer:
                answer("Час змінено")
        dialog = st.get("mode") == "dialog"
        self.dialog_set(chat, None)
        item = self.db.get_item(item_id)
        now_pub = choice == "now" or (dialog and choice is None and self.cfg.get("publish_on_approve"))
        if dialog or now_pub:
            if item["status"] == "notified":
                self.db.update_item(item_id, status="approved")
                self.db.event(item_id, "approved", {"by": chat, "title": st.get("title"), "hashtags": st.get("tags")})
        if now_pub:
            self.app.scheduler.release_slot(item_id)
            self.say(chat, f"✅ <b>Готово!</b> Публікую шортс #{item_id} зараз.")
            return self.app.publisher.start(item_id)
        when = fmt_when(self.cfg, item["slot_at"]) if item["slot_at"] else "—"
        if dialog:
            meta = self._meta(item)
            self.say(chat, f"✅ <b>Готово!</b> Публікація {when}.\n🎬 {esc(meta.yt_title)}\n"
                           f"{esc(' '.join(meta.hashtags))}")
        else:
            self.say(chat, f"🕐 Час шортса #{item_id}: <b>{when}</b>")
        self.refresh(item_id, texts=dialog)

    def _regen_ai(self, item_id: int) -> None:
        import ai
        item = self.db.get_item(item_id)
        try:
            result = ai.generate(self.cfg, item["mp4"], item["game"])
            ai.apply_to_json(item["json"], result)
        except Exception as e:  # noqa: BLE001
            log.error("ШІ-перегенерація #%s: %s", item_id, e)
            self.send(f"❌ ШІ не змогло перегенерувати тексти #{item_id}: {e}")
            return
        item = self.db.get_item(item_id)
        edited = [x for x in item["edited"] if x not in ("назву", "тексти")] + ["тексти"]
        self.db.update_item(item_id, title_override=None, edited=edited)
        self.db.event(item_id, "edited", "regen-ai")
        self.refresh(item_id, texts=True)

    def _regen(self, item_id: int) -> None:
        item = self.db.get_item(item_id)
        args = [str(a).format(mp4=item["mp4"], json=item["json"], game=item["game"])
                for a in self.cfg["regenerate_command"]]
        log.info("Перегенерація текстів #%s: %s", item_id, args)
        try:
            r = media.run_hidden(args, timeout=600, cwd=self.cfg.get("regenerate_cwd") or None)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout or b"").decode(errors="ignore")[-400:] or f"код {r.returncode}")
            load_meta(item["json"])
        except Exception as e:  # noqa: BLE001
            log.error("Перегенерація #%s не вдалася: %s", item_id, e)
            self.send(f"❌ Перегенерація текстів #{item_id} не вдалася: {e}")
            return
        item = self.db.get_item(item_id)
        edited = [x for x in item["edited"] if x not in ("назву", "тексти")] + ["тексти"]
        self.db.update_item(item_id, title_override=None, edited=edited)
        self.db.event(item_id, "edited", "regen")
        self.refresh(item_id, texts=True)

    # ---------- повідомлення й команди ----------
    def on_message(self, m: dict, chat: int, role: str) -> None:
        text = (m.get("text") or "").strip()
        if not text:
            return
        if text in BTN:
            text = BTN[text]
        if text.startswith("/"):
            cmd, *args = text.split()
            cmd = cmd.split("@")[0].lower()
            if role != "admin" and cmd not in VIEWER_CMDS:
                return self.say(chat, "⛔ Ця дія доступна лише адміністратору.", html_mode=False)
            return self.command(cmd, args, chat, role)
        if role != "admin":
            return self.say(chat, HELP_VIEWER, reply_markup=self.reply_keyboard(chat))
        if self.dialog_get(chat):
            return self.dialog_answer(chat, text)
        await_id = self.db.get(f"await_title:{chat}")
        if await_id:
            item_id = int(await_id)
            self.db.set(f"await_title:{chat}", None)
            item = self.db.get_item(item_id)
            if not item or item["status"] not in ("notified", "approved"):
                return self.say(chat, "Цей шортс уже неактуальний — назву не змінено.", html_mode=False)
            title = re.sub(r"\s+", " ", text)[:100]
            edited = [x for x in item["edited"] if x != "назву"] + ["назву"]
            self.db.update_item(item_id, title_override=title, edited=edited)
            self.db.event(item_id, "edited", {"title": title})
            self.refresh(item_id)
            note = " (обрізано до 100 символів)" if len(text) > 100 else ""
            return self.say(chat, f"✅ Назву для #{item_id} змінено{note}: {title}", html_mode=False)
        self.say(chat, HELP_ADMIN, reply_markup=self.reply_keyboard(chat))

    def command(self, cmd: str, args: list[str], chat: int | None = None, role: str = "admin") -> None:
        app = self.app
        chat = chat or self.chat_id
        reply = lambda text, html_mode=True, kb=None: self.say(chat, text, html_mode, kb)  # noqa: E731
        if cmd in ("/stats", "/top"):
            reply("⏳ Збираю статистику з платформ…", False)

            def work(which=cmd):
                from reports import analytics_text, collect, weekly_top
                try:
                    stats = collect(app)
                    reply(analytics_text(app, stats) if which == "/stats" else weekly_top(app, stats))
                except Exception as e:  # noqa: BLE001
                    log.exception("Аналітика")
                    reply(f"❌ Не вдалося зібрати статистику: {esc(str(e))}")
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
            reply("Скасовано.", False)
        elif cmd == "/queue":
            reply(app.queue_text())
        elif cmd == "/status":
            reply(app.status_text())
        elif cmd == "/report":
            from reports import daily_report
            reply(daily_report(app))
        elif cmd == "/pause":
            self.db.set("paused", "1")
            self.db.event(None, "paused", {"by": chat})
            self.send("⏸ Розклад призупинено: нові шортси не надсилатимуться, слоти лишатимуться порожніми.\n"
                      "Уже підтверджені шортси буде опубліковано. Відновити — «▶️ Продовжити».", keyboard=True)
        elif cmd == "/resume":
            self.db.set("paused", None)
            self.db.event(None, "resumed", {"by": chat})
            self.send("▶️ Розклад відновлено.", keyboard=True)
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
