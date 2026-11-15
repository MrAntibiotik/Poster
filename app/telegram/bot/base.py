"""Основа бота: стан, ролі, доступ, клавіатура, опитування Telegram."""
from __future__ import annotations

import threading
from app.core.config import now_ts
from app.telegram.api import TelegramAPI
from app.telegram.api import TelegramError
from .common import (
    COMMANDS,
    HELP_ADMIN,
    HELP_VIEWER,
    KBD_VERSION,
    esc,
    log,
    parse_ids,
    user_name,
)


class BaseMixin:
    def __init__(self, app):
        self._last_incoming: dict[int, int] = {}
        self._bulk_running = False
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

    def stopped(self) -> bool:
        return self.db.get("stopped") == "1"

    def reply_keyboard(self, chat_id: int) -> dict:
        if self.role(chat_id) == "admin":
            pause = "▶️ Продовжити" if self.paused() else "⏸ Пауза"
            stop = "✅ Відновити публікації" if self.stopped() else "⛔ Стоп публікацій"
            rows = [["📋 Черга", "🕐 Статус", "📊 Звіт"], ["📈 Аналітика", "🏆 Топ тижня", "🧪 Тест-слот"],
                    ["🔌 Платформи", pause, "👥 Доступ"], ["⚠️ Невдалі", "🎮 Стрім", "⬆️ Оновити"],
                    [stop, "📄 Журнал", "🧹 Очистити чат"], ["❓ Довідка"]]
            from app.channels import eu_tab
            if eu_tab.available(self.cfg):
                rows.append(["🇬🇧 EU-канал"])
        else:
            rows = [["📋 Черга", "🕐 Статус", "📊 Звіт"], ["📈 Аналітика", "🏆 Топ тижня"]]
        return {"keyboard": [[{"text": t} for t in r] for r in rows], "resize_keyboard": True,
                "is_persistent": True}

    def ensure_keyboard(self) -> None:
        """Кнопки внизу тримаються на повідомленні, яке їх надіслало. Якщо його видалено (прибирання чату) —
        Telegram ховає кнопки. Тож завжди лишаємо одне коротке повідомлення з актуальними кнопками."""
        for cid in self.recipients():
            if self.db.get(f"keep:{cid}:kbd") and self.db.get(f"kbd_v:{cid}") == KBD_VERSION:
                continue
            self.say(cid, "⌨️ Кнопки керування — внизу", html_mode=False, reply_markup=self.reply_keyboard(cid))
            self.db.set(f"kbd_v:{cid}", KBD_VERSION)

    def setup_commands(self) -> None:
        """Кнопка «Меню» в Telegram зі списком команд (best-effort)."""
        try:
            self.tg.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
            self.tg.call("setChatMenuButton", menu_button={"type": "commands"})
        except TelegramError as e:
            log.warning("Не вдалося налаштувати меню команд: %s", e)

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
        self.db.set("last_activity", now_ts())  # ви щось робите в боті — ПК не засне
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
            self.say(cid, keep=f"acc{chat}", text=f"👤 <b>Запит на доступ</b>\n{esc(name)}\nchat_id: <code>{chat}</code>\n\n"
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

    def _bg(self, fn, *args) -> None:
        """Довгі дії (стискання відео) — у фоні, щоб бот одразу відповідав на інші кнопки."""
        if getattr(self.app.publisher, "sync", False):
            return fn(*args)
        threading.Thread(target=fn, args=args, daemon=True, name=fn.__name__).start()

    def typing(self, chat: int, action: str = "upload_video") -> None:
        """Індикатор «надсилає відео…» у чаті, поки готується копія для Telegram."""
        try:
            self.tg.call("sendChatAction", chat_id=chat, action=action)
        except Exception as e:  # noqa: BLE001
            log.debug("sendChatAction: %s", e)
