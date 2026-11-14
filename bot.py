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
import time
from pathlib import Path

import media
from config import NAMES, PLATFORMS, SHORT, enabled_platforms, fmt_slot, fmt_when, local_dt, now_ts, parse_when
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
    "/platforms — увімкнути / вимкнути платформи для всіх шортсів\n"
    "/failed — невдалі публікації: причина, повтор зараз / пізніше / у вільний слот\n"
    "/update — оновити програму з GitHub зараз\n"
    "/log — журнал (помилки); /log instagram — лише рядки з цим словом\n"
    "/clean — прибрати з чату все, крім відео шортсів, звітів і аналітики\n"
    "/stream — режим стріму: ПК не засне (і за бажанням публікації чекають кінця стріму)\n"
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
    "👥 Доступ": "/users", "❓ Довідка": "/help", "📈 Аналітика": "/stats", "🏆 Топ тижня": "/top", "🔌 Платформи": "/platforms", "⚠️ Невдалі": "/failed", "🎮 Стрім": "/stream", "⬆️ Оновити": "/update", "📄 Журнал": "/log",
}
VIEWER_CMDS = {"/start", "/help", "/menu", "/queue", "/status", "/report", "/stats", "/top"}

# Кнопка «Меню» біля поля введення (setMyCommands)
KBD_VERSION = "3"  # збільшується, коли змінюється набір кнопок — бот надішле нові

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
    ("platforms", "Увімкнути / вимкнути платформи"),
    ("failed", "Невдалі публікації: причина й повтор"),
    ("users", "Хто має доступ"),
    ("update", "Оновити програму зараз"),
    ("log", "Журнал: помилки й попередження"),
    ("stream", "Режим стріму: ПК не засне"),
    ("clean", "Прибрати чат — лишити тільки відео"),
    ("help", "Довідка"),
]


def visible_len(text: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def parse_ids(value: str) -> list[int]:
    return [int(x) for x in re.findall(r"-?\d+", value or "")]


def user_name(u: dict) -> str:
    name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or "без імені"
    return f"{name} (@{u['username']})" if u.get("username") else name


REPLY_TTL = 24 * 3600    # відповіді зникають при наступній дії (або за добу — Telegram дає видаляти лише 48 год)
AUTO_TTL = 24 * 3600     # службові повідомлення й звіти — так само: до наступної дії або за добу

class Bot:
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

    def reply_keyboard(self, chat_id: int) -> dict:
        if self.role(chat_id) == "admin":
            pause = "▶️ Продовжити" if self.paused() else "⏸ Пауза"
            rows = [["📋 Черга", "🕐 Статус", "📊 Звіт"], ["📈 Аналітика", "🏆 Топ тижня", "🧪 Тест-слот"],
                    ["🔌 Платформи", pause, "👥 Доступ"], ["⚠️ Невдалі", "🎮 Стрім", "⬆️ Оновити"], ["📄 Журнал", "❓ Довідка"]]
        else:
            rows = [["📋 Черга", "🕐 Статус", "📊 Звіт"], ["📈 Аналітика", "🏆 Топ тижня"]]
        return {"keyboard": [[{"text": t} for t in r] for r in rows], "resize_keyboard": True,
                "is_persistent": True}

    def log_text(self, flt: str = "", only_errors: bool = True, mode: str = "err") -> tuple[str, dict]:
        """Журнал у Telegram: останні рядки (за замовчуванням — лише помилки й попередження), без секретів."""
        path = Path(self.cfg["paths"]["log"])
        lines = []
        try:
            with open(path, "rb") as f:
                f.seek(max(0, path.stat().st_size - 300_000))
                lines = f.read().decode("utf-8", errors="replace").splitlines()[1:]
        except OSError:
            pass
        flt = (flt or "").strip().lower()
        out = []
        for line in lines:
            m = re.match(r"\d{4}-(\d\d)-(\d\d) (\d\d:\d\d):\d\d,\d+ \[(\w+)\] [\w.]+: (.*)", line)
            if not m:
                continue
            level, msg = m.group(4), m.group(5)
            if only_errors and level not in ("WARNING", "ERROR", "CRITICAL"):
                continue
            if flt and flt not in msg.lower():
                continue
            for v in self.cfg["secrets"].values():
                if v and len(v) > 12:
                    msg = msg.replace(v, v[:4] + "…")
            icon = {"WARNING": "⚠️", "ERROR": "❌", "CRITICAL": "🛑"}.get(level, "•")
            out.append(f"{icon} <b>{m.group(2)}.{m.group(1)} {m.group(3)}</b> {esc(msg[:350])}")
        title = {"err": "помилки й попередження", "all": "усі події", "ig": "Instagram", "yt": "YouTube",
                 "fb": "Facebook", "tt": "TikTok"}.get(mode, f"«{flt}»")
        head = f"📄 <b>Журнал: {title}</b>"
        body, size = [], len(head)
        for line in reversed(out):  # найновіші, скільки вміщається в одне повідомлення
            if size + len(line) + 2 > 3800:
                break
            body.insert(0, line)
            size += len(line) + 2
        text = head + ("\n\n" + "\n\n".join(body) if body else "\n\nНічого не знайдено — і це добре 🙂")
        kb = {"inline_keyboard": [
            [{"text": "⚠️ Помилки", "callback_data": "log:0:err"}, {"text": "📜 Усе", "callback_data": "log:0:all"}],
            [{"text": "📸 IG", "callback_data": "log:0:ig"}, {"text": "▶️ YT", "callback_data": "log:0:yt"},
             {"text": "📘 FB", "callback_data": "log:0:fb"}, {"text": "🎵 TT", "callback_data": "log:0:tt"}]]}
        return text, kb

    def details_text(self, item_id: int) -> str:
        """Повні причини помилок і що пробувала програма — для «🧾 Деталі» (Telegram і панель)."""
        from platforms.meta_graph import short_error
        item = self.db.get_item(item_id)
        if not item:
            return "Шортс не знайдено"
        lines = [f"🧾 <b>Деталі публікації #{item_id}</b>", esc(Path(item["mp4"]).name)]
        for p in PLATFORMS:
            r = item["results"].get(p)
            if not r:
                continue
            if r.get("status") == "ok":
                lines.append(f"\n✅ <b>{NAMES[p]}</b>" + (f" — {esc(str(r.get('note')))}" if r.get("note") else ""))
                for t in r.get("tried") or []:
                    lines.append(f"   ✖️ не пройшло: {esc(str(t)[:400])}")
            else:
                lines.append(f"\n❌ <b>{NAMES[p]}</b> (спроб: {r.get('attempts', '?')})")
                err = str(r.get("error", ""))
                for part in err.split("; ") if "спроб)" in err else [err]:
                    lines.append(f"   • {esc(short_error(part) if len(part) > 400 else part)}")
        evs = self.db.q("SELECT ts, details FROM events WHERE item_id=? AND kind='platform_error' ORDER BY ts DESC LIMIT 6",
                        (item_id,))
        if evs:
            lines.append("\n<b>Останні помилки (журнал)</b>")
            for e in evs:
                try:
                    d = json.loads(e["details"] or "{}")
                except ValueError:
                    d = {}
                lines.append(f"• {local_dt(self.cfg, e['ts']):%d.%m %H:%M} {NAMES.get(d.get('platform'), d.get('platform', ''))}: "
                             f"{esc(short_error(d.get('error', '')))}")
        text = "\n".join(lines)
        return text if len(text) < 4000 else text[:3990] + "…"

    def has_details(self, item: dict) -> bool:
        return any(r.get("status") != "ok" or r.get("tried") for r in item["results"].values())

    def publish_all(self, by="") -> str:
        """Опублікувати всі шортси, що чекають на підтвердження (пропущені слоти) — по черзі, з поточними назвами."""
        items = [i for i in self.db.items("status='notified'", order="slot_at")
                 if self.app.publisher.targets(i) and not self.app.publisher.is_active(i["id"])]
        if not items:
            raise ValueError("Немає шортсів, що чекають на підтвердження")
        ids = [i["id"] for i in items]
        for i in ids:
            self.db.update_item(i, status="approved")
            self.db.event(i, "approved", {"by": by, "bulk": True})
            self.app.scheduler.release_slot(i)
            self.refresh(i)

        try:
            pending = json.loads(self.db.get("bulk_ids") or "[]")
        except ValueError:
            pending = []
        self.db.set("bulk_ids", json.dumps(pending + [i for i in ids if i not in pending]))
        self.resume_bulk()
        self.db.set("last_activity", now_ts())
        return f"🚀 Публікую по черзі: {', '.join('#' + str(i) for i in ids)}"

    def resume_bulk(self) -> None:
        """Черга «опублікувати всі»: по одному шортсу; переживає перезапуск бота (список — у базі)."""
        if self._bulk_running or not self.db.get("bulk_ids"):
            return
        self._bulk_running = True

        def work():
            try:
                while True:
                    try:
                        ids = json.loads(self.db.get("bulk_ids") or "[]")
                    except ValueError:
                        ids = []
                    if not ids:
                        break
                    i = ids[0]
                    item = self.db.get_item(i)
                    if item and item["status"] in ("approved", "publishing"):
                        self.app.publisher.start(i)  # по одному — щоб не вантажити інтернет і платформи
                        while self.app.publisher.is_active(i):
                            time.sleep(2)
                    self.db.set("bulk_ids", json.dumps(ids[1:]) if ids[1:] else None)
            finally:
                self._bulk_running = False
        self._bg(work)

    def update_now(self, force: bool = False) -> tuple[str, dict | None]:
        """Оновлення одразу, без очікування. Якщо саме йде публікація — питаємо (якщо не force)."""
        import updater
        info = updater.check(self.cfg)
        self.db.set("update_remote", info["remote"])
        self.db.set("update_error", None)
        if not info["newer"]:
            return f"✅ У вас остання версія ({info['local']}).", None
        busy = sorted(self.app.publisher.active) or [i["id"] for i in self.db.items("status='publishing'")]
        if busy and not force:
            return (f"⬆️ Є версія {info['remote']} (у вас {info['local']}), але зараз публікується "
                    f"#{', #'.join(map(str, busy))}.\nЯкщо оновитись зараз, ця публікація продовжиться після перезапуску "
                    "(туди, куди вже вийшло, повторно не піде).",
                    {"inline_keyboard": [[{"text": "⚡ Оновити зараз", "callback_data": "upd:1"},
                                          {"text": "⏳ Після публікації", "callback_data": "upd:0"}]]})
        new = updater.apply(self.cfg)
        self.db.event(None, "updated", {"version": new, "manual": True})
        self.db.set("control", "restart")  # головний цикл перезапустить бота за кілька секунд
        return f"⬆️ Оновлено до {new} — перезапускаюсь (≈10 с).", None

    def stream_text(self) -> tuple[str, dict]:
        now = now_ts()
        until = int(self.db.get("stream_until") or 0)
        sch = self.app.scheduler
        auto = sch.stream_reason(now) if until <= now else None
        power = self.cfg.get("power") or {}
        lines = ["🎮 <b>Режим стріму</b>"]
        if until > now:
            lines.append(f"Увімкнено до <b>{fmt_slot(self.cfg, until)}</b> — ПК не засне.")
        elif auto:
            lines.append(f"Зараз {esc(auto[2:])} — ПК не засне, поки програма працює.")
        else:
            lines.append("Вимкнено. ПК не засинає й тоді, коли запущено OBS / Streamlabs / гру.")
        lines.append("Публікації під час стріму: " + ("⏸ чекають кінця стріму" if power.get("hold_during_stream")
                                                     else "▶️ йдуть за розкладом") + " (змінити — у панелі).")
        rows = [[{"text": f"{h} год", "callback_data": f"stm:{h}"} for h in (2, 4, 6, 10)]]
        if until > now:
            rows.append([{"text": "⏹ Вимкнути режим стріму", "callback_data": "stm:0"}])
        return "\n".join(lines), {"inline_keyboard": rows}

    def platforms_text(self) -> tuple[str, dict]:
        lines = ["🔌 <b>Платформи</b> — куди публікувати всі шортси",
                 "Натисніть, щоб вимкнути або увімкнути. Діє одразу, без перезапуску."]
        rows = []
        for p in PLATFORMS:
            on = bool(self.cfg["platforms"].get(p, {}).get("enabled"))
            lines.append(f"{'✅' if on else '🚫'} {NAMES[p]}")
            rows.append([{"text": f"{'✅' if on else '🚫'} {NAMES[p]} — {'вимкнути' if on else 'увімкнути'}",
                          "callback_data": f"plat:0:{p}"}])
        lines.append("\nДля окремого шортса — кнопки «Вимкнути платформу» під ним.")
        return "\n".join(lines), {"inline_keyboard": rows}

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

    # ================= службові повідомлення =================
    def say(self, chat_id: int, text: str, html_mode: bool = True, reply_markup=None,
            keep: bool | str = False, ttl: int = REPLY_TTL, kind: str | None = None):
        """keep=False — тимчасове (зникне при наступній дії або через ttl с);
        keep=True — лишається назавжди; keep='kind' — лишається лише останнє такого типу."""
        try:
            msg = self.tg.send_message(chat_id, text if html_mode else esc(text), reply_markup=reply_markup)
        except TelegramError as e:
            log.error("Не вдалося надіслати повідомлення в Telegram (%s): %s", chat_id, e)
            return None
        if keep is False and isinstance(reply_markup, dict) and "keyboard" in reply_markup:
            keep = "kbd"  # повідомлення з кнопками внизу — лишаємо останнє, щоб кнопки не зникли
        if isinstance(keep, str):
            self.keep_latest(chat_id, keep, msg)
        elif not keep:
            self.ephemeral(chat_id, msg, ttl)
            if kind:  # автоматичне повідомлення того ж типу (звіт дня, нові в черзі…) — лишається лише останнє
                old = self.db.get(f"ekind:{chat_id}:{kind}")
                self.db.set(f"ekind:{chat_id}:{kind}", msg.get("message_id") if isinstance(msg, dict) else None)
                if old and str(old).isdigit():
                    self._delete(chat_id, [int(old)])
        else:
            self._remember_kept(chat_id, msg)
        return msg

    def resolve(self, kind: str) -> None:
        """Службове повідомлення більше не актуальне (напр. авторизацію відновлено) — прибираємо."""
        for cid in self.recipients():
            key = f"keep:{cid}:{kind}"
            mid = self.db.get(key)
            if mid and str(mid).isdigit():
                self._delete(cid, [int(mid)])
            self.db.set(key, None)

    # ---------- завдання, що виконуються: сповіщення висить, доки не завершиться ----------
    def task_start(self, chat_id: int, text: str):
        """«⏳ …» не зникає при натисканні інших кнопок, поки завдання не завершиться."""
        try:
            msg = self.tg.send_message(chat_id, esc(text))
        except TelegramError as e:
            log.error("task_start: %s", e)
            return None
        self._remember_kept(chat_id, msg, key=f"tasks:{chat_id}")
        return msg

    def task_done(self, chat_id: int, msg, text: str, kb=None) -> None:
        """Завершилось — те саме повідомлення стає результатом (і зникне при наступній дії)."""
        mid = (msg or {}).get("message_id")
        if mid:
            try:
                ids = [i for i in json.loads(self.db.get(f"tasks:{chat_id}") or "[]") if i != mid]
                self.db.set(f"tasks:{chat_id}", json.dumps(ids))
            except ValueError:
                pass
            try:
                self.tg.edit_text(chat_id, mid, text, kb)
                self.ephemeral(chat_id, mid)
                return
            except TelegramError as e:  # задовге для редагування тощо — надсилаємо окремо
                log.debug("task_done edit: %s", e)
                self._delete(chat_id, [mid])
        self.say(chat_id, text, reply_markup=kb)

    def _remember_kept(self, chat_id: int, msg, key: str | None = None) -> None:
        mid = msg.get("message_id") if isinstance(msg, dict) else msg
        if not mid:
            return
        key = key or f"kept:{chat_id}"
        try:
            ids = json.loads(self.db.get(key) or "[]")
        except ValueError:
            ids = []
        self.db.set(key, json.dumps((ids + [int(mid)])[-500:]))

    def clean_chat(self, chat_id: int, latest: int | None = None, depth: int = 1500) -> int:
        """Прибирає з чату все, крім відео шортсів, звітів і аналітики (Telegram дозволяє лише за 48 год)."""
        keep = {int(r["msg_id"]) for r in self.db.q("SELECT msg_id FROM tg_msgs WHERE chat_id=?", (int(chat_id),))}
        service = ("kbd", "reauth_", "failed", "acc")  # лише невирішені службові; аналітика/звіти — ні
        for r in self.db.q("SELECT key, value FROM kv WHERE key LIKE ?", (f"keep:{chat_id}:%",)):
            kind = r["key"].split(":", 2)[2]
            if r["value"] and str(r["value"]).isdigit() and kind.startswith(service):
                keep.add(int(r["value"]))
            elif not kind.startswith(service):
                self.db.set(r["key"], None)  # старі «лишати останню аналітику/звіт» — більше не діють
        try:
            keep.update(json.loads(self.db.get(f"tasks:{chat_id}") or "[]"))
        except ValueError:
            pass
        self.db.set(f"kept:{chat_id}", None)
        known = set(keep)
        for k in (f"eph:{chat_id}", f"trash:{chat_id}"):
            try:
                rows = json.loads(self.db.get(k) or "[]")
                known.update(r[0] if isinstance(r, list) else r for r in rows)
            except ValueError:
                pass
        if not latest:  # точний номер останнього повідомлення — через тимчасове «🧹»
            try:
                latest = int(self.tg.send_message(chat_id, "🧹 Прибираю чат…", html=False)["message_id"])
            except (TelegramError, KeyError, TypeError):
                latest = max(known) + 30 if known else 0
        if not latest:
            return 0
        ids = [i for i in range(max(1, latest - depth), latest + 1) if i not in keep]
        self._delete(chat_id, ids)
        self.db.set(f"eph:{chat_id}", None)
        self.db.set(f"trash:{chat_id}", None)
        return len(ids)

    # ---------- охайний чат: лишаються відео з прогресом, звіти й аналітика ----------
    def ephemeral(self, chat_id: int, msg, ttl: int = REPLY_TTL) -> None:
        mid = msg.get("message_id") if isinstance(msg, dict) else msg
        if not mid:
            return
        key = f"eph:{chat_id}"
        try:
            rows = json.loads(self.db.get(key) or "[]")
        except ValueError:
            rows = []
        rows.append([int(mid), now_ts() + ttl])
        self.db.set(key, json.dumps(rows[-300:]))

    def keep_latest(self, chat_id: int, kind: str, msg) -> None:
        mid = msg.get("message_id") if isinstance(msg, dict) else msg
        if not mid:
            return
        key = f"keep:{chat_id}:{kind}"
        old = self.db.get(key)
        self.db.set(key, int(mid))
        if old and int(old) != int(mid):
            self._delete(chat_id, [int(old)])

    def _delete(self, chat_id: int, ids: list[int]) -> None:
        for i in range(0, len(ids), 100):
            try:
                self.tg.call("deleteMessages", chat_id=chat_id, message_ids=ids[i:i + 100])
            except Exception as e:  # noqa: BLE001 — старі повідомлення (>48 год) Telegram не видаляє
                log.debug("deleteMessages: %s", e)

    def purge(self, chat_id: int, only_expired: bool = False) -> None:
        key = f"eph:{chat_id}"
        try:
            rows = json.loads(self.db.get(key) or "[]")
        except ValueError:
            rows = []
        now = now_ts()
        drop = [m for m, exp in rows if not only_expired or exp <= now]
        if not drop:
            return
        self.db.set(key, json.dumps([[m, exp] for m, exp in rows if m not in set(drop)]) if only_expired else None)
        self._delete(chat_id, sorted(drop))

    def purge_expired(self) -> None:
        for row in self.db.q("SELECT key FROM kv WHERE key LIKE 'eph:%' AND value IS NOT NULL"):
            try:
                self.purge(int(row["key"][4:]), only_expired=True)
            except ValueError:
                pass

    # ---------- тимчасові повідомлення (діалоги) — прибираються після завершення ----------
    def tmp(self, chat_id: int, text: str, html_mode: bool = True, reply_markup=None):
        msg = self.say(chat_id, text, html_mode, reply_markup)
        self._track(chat_id, msg)
        return msg

    def _track(self, chat_id: int, msg) -> None:
        mid = msg.get("message_id") if isinstance(msg, dict) else msg
        if not mid:
            return
        key = f"trash:{chat_id}"
        try:
            ids = json.loads(self.db.get(key) or "[]")
        except ValueError:
            ids = []
        ids.append(int(mid))
        self.db.set(key, json.dumps(ids[-200:]))

    def cleanup(self, chat_id: int) -> None:
        """Видаляє кроки діалогу, відповіді на них і службові повідомлення — лишається лише відео шортса."""
        key = f"trash:{chat_id}"
        try:
            ids = sorted(set(json.loads(self.db.get(key) or "[]")))
        except ValueError:
            ids = []
        self.db.set(key, None)
        for i in range(0, len(ids), 100):
            try:
                self.tg.call("deleteMessages", chat_id=chat_id, message_ids=ids[i:i + 100])
            except Exception as e:  # noqa: BLE001 — старі повідомлення (>48 год) Telegram не видаляє
                log.debug("deleteMessages: %s", e)

    def _drop_texts(self, chat_id: int, item_id: int) -> None:
        """Після підтвердження окреме повідомлення з текстами не потрібне — назва й хештеги в підписі відео."""
        row = self.db.tg_msg(item_id, chat_id)
        if row and row.get("text_msg_id"):
            self._track(chat_id, row["text_msg_id"])
            self.db.x("UPDATE tg_msgs SET text_msg_id=NULL WHERE item_id=? AND chat_id=?", (item_id, chat_id))

    def drop_all_texts(self, item_id: int) -> None:
        for r in self.db.tg_msgs(item_id):
            if r.get("text_msg_id"):
                self._delete(r["chat_id"], [r["text_msg_id"]])
                self.db.x("UPDATE tg_msgs SET text_msg_id=NULL WHERE item_id=? AND chat_id=?", (item_id, r["chat_id"]))

    def send(self, text: str, html_mode: bool = False, to: str = "admins", keyboard: bool = False,
             keep: bool | str = False, kind: str | None = None) -> None:
        """to: 'admins' — службові повідомлення; 'all' — також глядачам (звіти, перегляди).
        Службові повідомлення (keep=False) самі зникають за кілька годин або при наступній дії."""
        for cid in (self.recipients() if to == "all" else self.admins()):
            self.say(cid, text, html_mode, self.reply_keyboard(cid) if keyboard else None, keep=keep, ttl=AUTO_TTL,
                     kind=kind)

    def notify_reauth(self, key: str, reason: str) -> None:
        k = f"reauth_{key}"
        last = int(self.db.get(k) or 0)
        if now_ts() - last < 6 * 3600:
            return
        self.db.set(k, now_ts())
        hint = self.app.platforms[key].reauth_hint if key in self.app.platforms else f"python poster.py auth {key}"
        self.send(keep=f"reauth_{key}", text=f"🔑 <b>Потрібна повторна авторизація: {NAMES[key]}</b>\n"
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
            if item["slot_at"] and item["slot_at"] <= now_ts():
                return (f"⏳ Слот {slot} минув — чекаю на «🚀 Опублікувати», після нього публікую одразу "
                        f"(або «🕐 Змінити час»){edited}")
            return f"⏳ Очікує підтвердження до {slot}{edited}"
        if st == "approved":
            return f"👍 Підтверджено — публікація о {slot}{edited}"
        if st == "publishing":
            return "⏫ <b>Публікується…</b>" + self._results(item, publishing=True)
        if st in ("posted", "failed"):
            targets = [p for p in PLATFORMS if p in self.app.platforms and p not in item["disabled"]] or \
                list(item["results"])
            ok = [p for p in targets if (item["results"].get(p) or {}).get("status") == "ok"]
            if not ok:
                head = "❌ <b>Не опубліковано</b>"
            elif len(ok) < len([p for p in targets if p in item["results"]]):
                head = f"⚠️ <b>Опубліковано частково</b> ({len(ok)} з {len([p for p in targets if p in item['results']])})"
            else:
                head = "✅ <b>Опубліковано</b>"
            when = self.app.scheduler.retry_at(item["id"])
            ignored = " · 🙈 проігноровано" if self.db.get(f"ignored:{item['id']}") else ""
            return head + ignored + self._results(item) + (f"\n🔁 Повтор: {fmt_when(self.cfg, when)}" if when else "")
        if st == "queued":
            if item["note"]:
                return esc(item["note"])
            return f"⏭ Пропущено ({item['skips']}/{self.cfg['max_skips']}) — шортс переміщено в кінець черги"
        if st == "skipped":
            return f"⏭ Пропущено {item['skips']} рази — файли перенесено в skipped/"
        if st == "cancelled":
            return "🚫 <b>Скасовано</b> — файл у папці «скасовані». Черга зсунулась: наступний шортс іде в цей слот."
        if st == "missing":
            return "⚠️ Файл не знайдено — шортс прибрано з черги"
        return st

    @staticmethod
    def _short_note(note: str) -> str:
        n = str(note or "").lower()
        if "крайній варіант" in n or "30 к/с" in n:
            return "копія 30 к/с"
        if "сувора копія" in n or "тією ж частотою" in n:
            return "полегшена копія"
        if "перекодовано" in n or "копію під вимоги" in n or "копія під вимоги" in n or "не прийняв оригінал" in n:
            return "копія під вимоги Reels"
        if "приват" in n:
            return "приватне"
        if "токеном користувача" in n:
            return "резервний токен"
        return ""

    def _results(self, item: dict, publishing: bool = False) -> str:
        from reports import ICONS, explain_error
        lines = []
        targets = [p for p in PLATFORMS if p in self.app.platforms and p not in item["disabled"]]
        current = self.db.get(f"pubnow:{item['id']}") if publishing else None
        for p in PLATFORMS:
            r = item["results"].get(p)
            icon = ICONS.get(p, "•")
            if p in item["disabled"]:
                continue
            if p == current:  # саме зараз публікується (стару помилку не показуємо — вона може вже не бути актуальною)
                tr = self.db.get(f"pubtry:{item['id']}:{p}")
                lines.append(f"{icon} {NAMES[p]} — ⏳ публікується…" + (f" (спроба {tr})" if tr else ""))
                continue
            if not r:
                if publishing and p in targets:
                    lines.append(f"{icon} {NAMES[p]} — ⏳")
                continue
            if r.get("status") == "ok":
                lock = {"SELF_ONLY": " 🔒", "private": " 🔒", "unlisted": " 🔗"}.get(r.get("privacy"), "")
                name = f'<a href="{esc(r["url"])}">{NAMES[p]}</a>' if r.get("url") else NAMES[p]
                extra = []
                if isinstance(r.get("views24"), int):
                    extra.append(f"👁 {r['views24']:,}".replace(",", " ") +
                                 (f" ❤️ {r['likes24']}" if r.get("likes24") is not None else ""))
                note = self._short_note(r.get("note"))
                if note:
                    extra.append(f"<i>{note}</i>")
                lines.append(f"{icon} {name} ✅{lock}" + (" · " + " · ".join(extra) if extra else ""))
            else:
                _, text, _ = explain_error(p, r.get("error", ""))
                text = text.replace(f"{NAMES[p]}: ", "").replace(f"{NAMES[p]} ", "")
                lines.append(f"{icon} {NAMES[p]} — ❌ {esc(text[:80])}")
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
        q = self.db.get(f"qline:{item['id']}") if item["status"] in ("notified", "approved") else None
        st = item["status"]
        if st in ("notified", "approved"):
            info = f"🕐 Слот: {slot} | Платформи: {self._platform_line(item)}\n" + (f"📐 {esc(q)}\n" if q else "")
        elif st in ("posted", "failed") and item["posted_at"]:
            info = f"📅 {local_dt(self.cfg, item['posted_at']):%d.%m %H:%M}\n"
        elif st == "queued":
            plan = {it["id"]: ts for it, ts in self.app.scheduler.projection()}
            info = f"📦 У черзі → {fmt_when(self.cfg, plan[item['id']])}\n" if plan.get(item["id"]) else ""
        else:
            info = ""
        tail = info + f"\n{self._status_block(item)}{hint}"
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

    def failed_errors(self, item: dict) -> dict:
        return {p: r for p, r in item["results"].items()
                if r.get("status") != "ok" and p in self.app.platforms and p not in item["disabled"]}

    def retry_keyboard(self, item_id: int) -> dict:
        return {"inline_keyboard": [
            [{"text": "🔁 Повторити зараз", "callback_data": f"rf:{item_id}"}],
            [{"text": "🕐 Обрати час", "callback_data": f"rt:{item_id}"},
             {"text": "📦 У вільний слот", "callback_data": f"rq:{item_id}"}],
            [{"text": "✖️ Ігнорувати", "callback_data": f"ri:{item_id}"}]]}

    def keyboard(self, item: dict) -> dict | None:
        if item["status"] in ("failed", "posted"):
            det = [{"text": "🧾 Деталі", "callback_data": f"err:{item['id']}"}] if self.has_details(item) else []
            if self.failed_errors(item) or item["status"] == "failed":
                if not self.db.get(f"ignored:{item['id']}"):
                    kb = self.retry_keyboard(item["id"])
                    if det:
                        kb["inline_keyboard"].append(det)
                    return kb
            return {"inline_keyboard": [det]} if det else None
        if item["status"] == "cancelled":
            return {"inline_keyboard": [[{"text": "↩️ Повернути в чергу", "callback_data": f"cr:{item['id']}"}]]} \
                if Path(item["mp4"]).exists() else None
        if item["status"] not in ("notified", "approved"):
            return None
        i = item["id"]
        first = ({"text": "🚀 Опублікувати", "callback_data": f"pub:{i}"} if item["status"] == "notified"
                 else {"text": "↩️ Скасувати підтвердження", "callback_data": f"unpub:{i}"})
        toggles = [{"text": f"{SHORT[p]} {'🚫' if p in item['disabled'] else '✅'}",
                    "callback_data": f"tog:{i}:{p}"} for p in enabled_platforms(self.cfg)]
        import ai
        edit = [{"text": "✏️ Назва", "callback_data": f"title:{i}"}, {"text": "📝 Тексти", "callback_data": f"txt:{i}"}]
        if self.cfg.get("regenerate_command") or ai.enabled(self.cfg):
            edit.append({"text": "🔄 Нові тексти", "callback_data": f"regen:{i}"})
        rows = [[first], [{"text": "🕐 Змінити час / дату", "callback_data": f"time:{i}"}], edit]
        if toggles:
            rows.append([{"text": "Вимкнути платформу:", "callback_data": f"noop:{i}"}])
            rows.append(toggles)
        waiting = len(self.db.items("status='notified'"))
        if item["status"] == "notified" and waiting >= 2:
            rows.append([{"text": f"🚀 Опублікувати всі, що чекають ({waiting})", "callback_data": "pall:0"}])
        rows.append([{"text": "🚫 Скасувати цей шортс", "callback_data": f"cx:{i}"}])
        return {"inline_keyboard": rows}

    def notify_item(self, item_id: int) -> None:
        """Надсилає сам шортс (відеоповідомлення) + тексти всім отримувачам; кнопки — лише адміністраторам."""
        item = self.db.get_item(item_id)
        meta = self._meta(item)
        try:
            q = media.quality(item["mp4"], self.cfg["ffprobe"])
            qline = f"📐 <b>Оригінал:</b> {esc(media.quality_line(q))}\nНа платформи завантажується саме він — без перекодування."
            ph = int((self.cfg.get("telegram") or {}).get("preview_height", 720) or 0)
            if ph and min(q.get("width") or 0, q.get("height") or 0) > ph:
                qline += f"\nℹ️ У цьому чаті — швидка копія {ph}p; на платформи іде оригінал."
            elif q["size_mb"] * 1024 * 1024 > media.TG_LIMIT:
                qline += ("\nℹ️ У цьому чаті — копія, стиснута до 50 МБ (ліміт Telegram для ботів); "
                          "на YouTube/TikTok/Instagram/Facebook іде оригінал.")
        except OSError:
            qline = ""
        try:
            if q.get("width"):
                self.db.set(f"qline:{item_id}", f"{q['width']}×{q['height']} · {q.get('fps') or '?'} к/с · "
                                                f"{q.get('size_mb')} МБ → на платформи оригінал")
        except NameError:  # файл недоступний — без рядка якості
            pass
        separate = bool((self.cfg.get("telegram") or {}).get("texts_message", False))
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
                text_msg = self.tg.send_message(cid, texts, reply_to=msg["message_id"]) if separate else None
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
        if not (self.cfg.get("telegram") or {}).get("texts_message", False) and any(r.get("text_msg_id") for r in rows):
            self.drop_all_texts(item_id)  # старі окремі «📝 Тексти» прибираємо — лишається тільки відео
            rows = self.db.tg_msgs(item_id)
            texts = False
        meta = self._meta(item)
        cap, kb = self.caption(item, meta), self.keyboard(item)
        txt = self.texts(item, meta) if texts else None
        admins = set(self.admins())
        for r in rows:
            markup = kb if r["chat_id"] in admins else None
            digest = self._cap_digest(cap, markup)
            try:
                if r["is_text"]:
                    self.tg.edit_text(r["chat_id"], r["msg_id"], cap, markup)
                else:
                    self.tg.edit_caption(r["chat_id"], r["msg_id"], cap, markup)
                if txt and r["text_msg_id"]:
                    self.tg.edit_text(r["chat_id"], r["text_msg_id"], txt)
            except Exception as e:  # noqa: BLE001 — немає інтернету тощо: доробимо пізніше (heal)
                if "not modified" not in str(e).lower():
                    log.warning("Не вдалося оновити повідомлення #%s у %s: %s — повторю, щойно буде зв'язок",
                                item_id, r["chat_id"], e)
                    continue
            self.db.set(f"cap:{item_id}:{r['chat_id']}", digest)  # що саме зараз показано в чаті

    @staticmethod
    def _cap_digest(cap: str, kb) -> str:
        import hashlib
        return hashlib.md5((cap + json.dumps(kb, ensure_ascii=False, sort_keys=True)).encode()).hexdigest()[:16]

    def heal(self, limit: int = 10) -> int:
        """Самовідновлення: підписи відео, які не вдалося оновити (світло/інтернет), доводимо до актуального стану."""
        fixed = 0
        since = now_ts() - 3 * 86400
        admins = set(self.admins())
        for item in self.db.items("id IN (SELECT item_id FROM tg_msgs) AND (status IN "
                                  "('notified','approved','publishing','failed','queued') OR posted_at > ?)",
                                  (since,), order="id DESC"):
            meta = self._meta(item)
            cap, kb = self.caption(item, meta), self.keyboard(item)
            stale = any(self.db.get(f"cap:{item['id']}:{r['chat_id']}") !=
                        self._cap_digest(cap, kb if r["chat_id"] in admins else None)
                        for r in self.db.tg_msgs(item["id"]))
            if stale:
                self.refresh(item["id"])
                fixed += 1
                if fixed >= limit:
                    break
        fixed += self.upgrade_text_cards()
        return fixed

    def upgrade_text_cards(self, limit: int = 2) -> int:
        """Картка без відео (відео тоді не вдалося надіслати — світло, інтернет, збій стискання):
        щойно можна — надсилаємо картку з відео, а текстову прибираємо. Спроби — не частіше ніж раз на 30 хв."""
        done = 0
        admins = set(self.admins())
        for r in self.db.q("SELECT * FROM tg_msgs WHERE is_text=1 ORDER BY item_id DESC LIMIT 20"):
            item = self.db.get_item(r["item_id"])
            key = f"upgrade_try:{r['item_id']}:{r['chat_id']}"
            if not item or not Path(item["mp4"]).exists() or now_ts() - int(self.db.get(key) or 0) < 1800:
                continue
            self.db.set(key, now_ts())
            try:
                video = media.telegram_copy(item["mp4"], self.cfg)
                info = media.probe(video, self.cfg["ffprobe"])
                cap, kb = self.caption(item), self.keyboard(item)
                markup = kb if r["chat_id"] in admins else None
                msg = self.tg.send_video(r["chat_id"], video, cap, markup, width=info.get("width"),
                                         height=info.get("height"), duration=info.get("duration"))
            except Exception as e:  # noqa: BLE001
                log.info("Картка #%s досі без відео: %s — спробую пізніше", r["item_id"], e)
                continue
            self._delete(r["chat_id"], [r["msg_id"]])
            self.db.set_tg_msg(r["item_id"], r["chat_id"], msg["message_id"], r.get("text_msg_id"), 0)
            self.db.set(f"cap:{r['item_id']}:{r['chat_id']}", self._cap_digest(cap, markup))
            self.db.set(key, None)
            log.info("Картку #%s замінено на картку з відео", r["item_id"])
            done += 1
            if done >= limit:
                break
        return done

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
            answer(result[:200], True)
            try:
                text, kb = self.users_text()
                if "Запит на доступ" in ((cq.get("message") or {}).get("text") or ""):
                    self._delete(chat, [cq["message"]["message_id"]])  # рішення — у спливному вікні, чат чистий
                else:
                    self.tg.edit_text(chat, cq["message"]["message_id"], text, kb)
            except TelegramError as e:
                log.debug("edit users: %s", e)
            return
        if action == "noop":
            return answer("Натисніть на платформу нижче, щоб вимкнути/увімкнути її")
        if action in ("keep", "dlgx"):
            return self.dialog_button(chat, target, action, rest, answer)
        if action == "plat" and rest:
            key = rest[0]
            if key not in PLATFORMS:
                return answer()
            on = not self.cfg["platforms"].get(key, {}).get("enabled")
            self.app.set_platform_enabled(key, on)
            answer(f"{NAMES[key]}: {'увімкнено' if on else 'вимкнено'}")
            text, kb = self.platforms_text()
            try:
                self.tg.edit_text(chat, cq["message"]["message_id"], text, kb)
            except TelegramError as e:
                log.debug("edit platforms: %s", e)
            return
        if action == "log":
            answer()
            mode = (rest[0] if rest else "err")
            flt = {"err": "", "all": "", "ig": "instagram", "yt": "youtube", "fb": "facebook", "tt": "tiktok"}.get(mode, "")
            text, kb = self.log_text(flt, only_errors=mode in ("err", "ig", "yt", "fb", "tt"), mode=mode)
            try:
                self.tg.edit_text(chat, cq["message"]["message_id"], text, kb)
            except TelegramError as e:
                log.debug("log edit: %s", e)
            return
        if action == "err":
            answer()
            return self.say(chat, self.details_text(target))  # зникне при наступній дії
        if action in ("cx", "cr", "pall"):
            try:
                if action == "cx":
                    self.app.scheduler.cancel(target, by=chat)
                    answer("🚫 Скасовано — файл у папці «скасовані». Наступний шортс стає на цей слот.", True)
                elif action == "cr":
                    self.app.scheduler.restore(target)
                    answer("↩️ Повернуто — перший у черзі", True)
                else:
                    answer(self.publish_all(by=chat)[:190], True)
            except ValueError as e:
                answer(str(e)[:190], True)
            return
        if action == "an":
            import analytics
            answer(analytics.PERIODS.get(target, ""))
            try:
                self.tg.edit_text(chat, cq["message"]["message_id"],
                                  analytics.period_report(self.cfg, self.db, target, list(self.app.platforms)),
                                  analytics.keyboard(target))
            except TelegramError as e:
                log.debug("analytics edit: %s", e)
            return
        if action == "upd":
            answer("Оновлюю…" if target else "Оновлю після публікації")
            if target:
                def work():
                    try:
                        text, _ = self.update_now(force=True)
                    except Exception as e:  # noqa: BLE001
                        text = f"⚠️ Оновлення не вдалося: {e}"
                    self.say(chat, text, html_mode=False)
                self._bg(work)
            else:
                self.db.set("update_available", "manual")  # планувальник оновить, щойно публікація завершиться
            return self._delete(chat, [cq["message"]["message_id"]])
        if action == "stm":
            hours = target
            if hours:
                self.db.set("stream_until", now_ts() + hours * 3600)
                answer(f"🎮 Режим стріму на {hours} год")
            else:
                self.db.set("stream_until", None)
                answer("Режим стріму вимкнено")
            text, kb = self.stream_text()
            try:
                self.tg.edit_text(chat, cq["message"]["message_id"], text, kb)
            except TelegramError as e:
                log.debug("edit stream: %s", e)
            return
        if action == "ig":
            answer("⏳ Готую відео…")
            return self._bg(self.ignored_list, chat)
        if action in ("rf", "rt", "rq", "ri", "ru"):
            return self.retry_button(chat, target, action, cq, answer)
        if action in ("qp", "qi", "qt", "qf", "qd"):
            return self.queue_button(chat, target, action, cq, answer)
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
        elif action == "txt":
            answer()
            q = self.db.get(f"qline:{item_id}") or ""
            self.say(chat, self.texts(item, self._meta(item), f"📐 {esc(q)}" if q else ""))  # зникне при наступній дії
        elif action == "title":
            self.db.set(f"await_title:{chat}", item_id)
            self._track(chat, self.tg.send_message(
                chat, f"✏️ Надішліть нову назву для YouTube (шортс #{item_id}, до 100 символів). /cancel — скасувати",
                reply_markup={"force_reply": True, "input_field_placeholder": "Нова назва"}))
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

    def dialog_start(self, chat: int, item_id: int, preset=None) -> None:
        item = self.db.get_item(item_id)
        meta = self._raw_meta(item)
        self.dialog_set(chat, {"item": item_id, "step": "title", "preset": preset})
        kb = {"inline_keyboard": [
            [{"text": f"Залишити: {meta.yt_title[:40]}", "callback_data": f"keep:{item_id}:title"}],
            [{"text": "❌ Скасувати", "callback_data": f"dlgx:{item_id}"}]]}
        always = self._always_line()
        note = f"\nℹ️ У кінець назви автоматично додаються: {esc(always)}" if always else ""
        self.tmp(chat, f"✍️ <b>Крок 1/2.</b> Надішліть <b>назву</b> для шортса #{item_id}.\n"
                       f"Зараз: <i>{esc(meta.yt_title)}</i>{note}", reply_markup=kb)

    def _ask_tags(self, chat: int, item_id: int) -> None:
        meta = self._raw_meta(self.db.get_item(item_id))
        always = {t.lower() for t in (self._always_line().split())}
        current = " ".join(t for t in meta.hashtags if t.lower() not in always) or "—"
        kb = {"inline_keyboard": [
            [{"text": f"Залишити: {current[:40]}", "callback_data": f"keep:{item_id}:tags"}],
            [{"text": "❌ Скасувати", "callback_data": f"dlgx:{item_id}"}]]}
        self.tmp(chat, "#️⃣ <b>Крок 2/2.</b> Надішліть <b>хештеги</b> через пробіл — можна з # або без "
                       "(напр.: <code>wot танки shorts</code>). «-» — без хештегів.", reply_markup=kb)

    def _dialog_item(self, chat: int, item_id: int, allow_approved: bool = False,
                     allow_queued: bool = False) -> dict | None:
        item = self.db.get_item(item_id)
        ok = ("queued",) if allow_queued else ("notified", "approved") if allow_approved else ("notified",)
        if not item or item["status"] not in ok:
            self.dialog_set(chat, None)
            self.tmp(chat, "Цей шортс уже неактуальний (слот минув або його опубліковано).", html_mode=False)
            return None
        return item

    def dialog_answer(self, chat: int, text: str) -> None:
        st = self.dialog_get(chat)
        if st.get("mode") == "retry":
            item = self.db.get_item(st["item"])
            if not item or item["status"] not in ("failed", "posted"):
                self.dialog_set(chat, None)
                return self.cleanup(chat)
        else:
            item = self._dialog_item(chat, st["item"], allow_approved=st.get("mode") == "change",
                                     allow_queued=st.get("mode") == "queue")
        if not item:
            return
        if st["step"] in ("time", "time_custom"):
            return self._time_typed(chat, item, st, text)
        if st["step"] == "title":
            title = re.sub(r"\s+", " ", text).strip()[:100]
            if not title:
                return self.tmp(chat, "Назва порожня — надішліть текст.", html_mode=False)
            self.dialog_set(chat, {**st, "step": "tags", "title": title})
            return self._ask_tags(chat, item["id"])
        tags = parse_hashtags(text)
        if text.strip() not in ("-", "—") and not tags:
            return self.tmp(chat, "Не бачу хештегів — надішліть слова через пробіл або «-».", html_mode=False)
        if st["step"] == "tags":
            return self._dialog_finish(chat, item, st.get("title"), tags)

    def dialog_button(self, chat: int, item_id: int, action: str, rest: list, answer) -> None:
        st = self.dialog_get(chat)
        if action == "dlgx":
            self.dialog_set(chat, None)
            answer("Скасовано. Шортс чекає на «🚀 Опублікувати» до свого слоту.", True)
            return self.cleanup(chat)
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
            return self.tmp(chat, f"❌ Не вдалося зберегти тексти: {e}", html_mode=False)
        edited = [x for x in item["edited"] if x not in ("назву", "тексти")] + ["назву", "хештеги"]
        self.db.update_item(item_id, title_override=None, edited=edited)
        if len(tags) > 15:
            self.tmp(chat, "⚠️ YouTube ігнорує всі хештеги, якщо їх більше 15.", html_mode=False)
        preset = (self.dialog_get(chat) or {}).get("preset")
        st = {"item": item_id, "step": "time", "mode": "dialog", "title": title, "tags": tags}
        self.dialog_set(chat, st)
        self.refresh(item_id, texts=True)
        if preset:  # час уже обрали з черги
            return self._apply_time(chat, self.db.get_item(item_id), st, "now" if preset == "now" else None)
        self._ask_time(chat, item_id, dialog=True)

    # ---------- невдалі публікації ----------
    def failed_text(self, item: dict, err_len: int = 700) -> str:
        meta = self._meta(item)
        errs = self.failed_errors(item)
        ok = [p for p, r in item["results"].items() if r.get("status") == "ok"]
        lines = [f"⚠️ <b>Не опубліковано</b> #{item['id']}: {esc(meta.yt_title)}"]
        for p, r in errs.items():
            lines.append(f"❌ <b>{NAMES[p]}</b>: {esc(str(r.get('error', 'невідома помилка'))[:err_len])}")
        if ok:
            lines.append("✅ Вийшло: " + ", ".join(NAMES[p] for p in ok))
        when = self.app.scheduler.retry_at(item["id"])
        if when:
            lines.append(f"🔁 Повтор заплановано: <b>{fmt_when(self.cfg, when)}</b>")
        lines.append("Повтор публікує лише туди, де не вийшло.")
        return "\n".join(lines)

    def notify_failed(self, item_id: int) -> None:
        """Невдача видна в підписі самого відео (з кнопками повтору). Окреме повідомлення — лише якщо відео в чаті немає."""
        item = self.db.get_item(item_id)
        if not item:
            return
        for cid in self.admins():
            if self.db.tg_msg(item_id, cid):
                continue
            self.say(cid, self.failed_text(item), reply_markup=self.retry_keyboard(item_id), keep=f"failed{item_id}")

    def drop_failed_card(self, item_id: int) -> None:
        for cid in self.admins():
            key = f"keep:{cid}:failed{item_id}"
            mid = self.db.get(key)
            if mid:
                self._delete(cid, [int(mid)])
                self.db.set(key, None)

    def retry_button(self, chat: int, item_id: int, action: str, cq: dict, answer) -> None:
        item = self.db.get_item(item_id)
        if not item or item["status"] not in ("failed", "posted"):
            return answer("Цей шортс уже неактуальний", True)
        if self.app.publisher.is_active(item_id):
            return answer("Уже публікується…", True)
        msg_id = (cq.get("message") or {}).get("message_id")
        if action == "ru":
            self.db.set(f"ignored:{item_id}", None)
            answer("↩️ Повернуто до невдалих")
            self._close_msg(chat, msg_id, "↩️ Повернуто до «⚠️ Невдалі»", item_id)
            return self.refresh(item_id)
        if action == "rf":
            self.db.set(f"ignored:{item_id}", None)
            answer("🔁 Повторюю публікацію…")
            self._close_msg(chat, msg_id, "🔁 Повторюю публікацію…", item_id, running=True)
            return self.app.publisher.start(item_id)
        if action == "rq":
            try:
                ts = self.app.scheduler.reserve_retry(item_id)
            except ValueError as e:
                return answer(str(e), True)
            answer(f"🔁 Повтор: {fmt_when(self.cfg, ts)}", True)
            self._close_msg(chat, msg_id, f"🔁 Повтор заплановано: {fmt_when(self.cfg, ts)}", item_id)
            return self.refresh(item_id)
        if action == "rt":
            self.dialog_set(chat, {"item": item_id, "step": "time", "mode": "retry", "msg": msg_id})
            answer()
            return self._ask_time(chat, item_id, dialog=False)
        if action == "ri":
            self.db.set(f"ignored:{item_id}", "1")
            self.db.x("DELETE FROM slots WHERE state='retry' AND item_id=?", (item_id,))
            answer("Гаразд, більше не нагадуватиму")
            self._close_msg(chat, msg_id, "✖️ Проігноровано", item_id)
            return self.refresh(item_id)

    def _close_msg(self, chat: int, msg_id: int | None, note: str, item_id: int | None = None,
                   running: bool = False) -> None:
        """Окреме повідомлення про збій після дії з ним.
        Якщо в чаті є відео цього шортса (статус видно в його підписі) — повідомлення прибираємо.
        Якщо дія триває (повтор зараз) — воно висить як «🔁 Повторюю…» і зникне, щойно публікація завершиться."""
        if not msg_id:
            return
        own = self.db.q("SELECT 1 FROM tg_msgs WHERE chat_id=? AND msg_id=?", (chat, msg_id))
        if own:
            return  # це повідомлення з відео — його оновить refresh()
        has_card = bool(item_id and self.db.tg_msg(item_id, chat))
        if running and item_id:
            self._remember_kept(chat, msg_id, key=f"tasks:{chat}")  # не зникне від інших кнопок, поки триває
            key = f"itemmsg:{item_id}"
            try:
                rows = json.loads(self.db.get(key) or "[]")
            except ValueError:
                rows = []
            self.db.set(key, json.dumps(rows + [[int(chat), int(msg_id)]]))
        elif has_card:
            return self._delete(chat, [msg_id])
        else:
            self.ephemeral(chat, msg_id)
        try:
            self.tg.call("editMessageReplyMarkup", chat_id=chat, message_id=msg_id,
                         reply_markup={"inline_keyboard": [[{"text": note, "callback_data": "noop:0"}]]})
        except Exception as e:  # noqa: BLE001
            log.debug("close msg: %s", e)

    def finish_item_msgs(self, item_id: int) -> None:
        """Публікацію завершено: «🔁 Повторюю…» прибираємо — результат видно в підписі відео
        (або, якщо відео в чаті немає, це повідомлення стає результатом)."""
        key = f"itemmsg:{item_id}"
        try:
            rows = json.loads(self.db.get(key) or "[]")
        except ValueError:
            rows = []
        self.db.set(key, None)
        item = self.db.get_item(item_id)
        for chat, mid in rows:
            try:
                ids = [i for i in json.loads(self.db.get(f"tasks:{chat}") or "[]") if i != mid]
                self.db.set(f"tasks:{chat}", json.dumps(ids))
            except ValueError:
                pass
            if self.db.tg_msg(item_id, chat) or not item:
                self._delete(chat, [mid])
                continue
            if self.failed_errors(item) or item["status"] == "failed":
                text, kb = self.failed_text(item), self.retry_keyboard(item_id)
            else:
                text, kb = f"✅ Опубліковано #{item_id}" + self._results(item), None
            try:
                self.tg.edit_text(chat, mid, text, kb)
                self.ephemeral(chat, mid)
            except TelegramError:
                self._delete(chat, [mid])

    def _retry_choice(self, chat: int, item: dict, choice, answer=None) -> None:
        item_id = item["id"]
        st = self.dialog_get(chat) or {}
        self.dialog_set(chat, None)
        if choice == "now":
            self.cleanup(chat)
            self._close_msg(chat, st.get("msg"), "🔁 Повторюю публікацію…")
            return self.app.publisher.start(item_id)
        if isinstance(choice, int):
            try:
                ts = self.app.scheduler.reserve_retry(item_id, choice)
            except ValueError as e:
                if answer:
                    answer(str(e), True)
                self.dialog_set(chat, st)
                return self.tmp(chat, f"⚠️ {e}. Оберіть інший час.", html_mode=False)
            if answer:
                answer(f"🔁 Повтор: {fmt_when(self.cfg, ts)}")
            self._close_msg(chat, st.get("msg"), f"🔁 Повтор заплановано: {fmt_when(self.cfg, ts)}")
        self.cleanup(chat)
        self.refresh(item_id)

    def _item_card(self, chat: int, item: dict, kb: dict, prefix: str = "") -> None:
        """Картка шортса з самим відео. Що б не сталося — користувач отримає хоча б текст із кнопками."""
        try:
            self._item_card_inner(chat, item, kb, prefix)
        except Exception as e:  # noqa: BLE001
            log.exception("Картка #%s", item.get("id"))
            errs = "; ".join(f"{NAMES.get(p, p)}: {str(r.get('error', ''))[:150]}"
                             for p, r in (item.get("results") or {}).items() if r.get("status") != "ok")
            text = (f"{prefix}⚠️ Шортс #{item.get('id')} ({Path(item.get('mp4') or '').stem})\n"
                    f"{errs or 'причина невідома'}\n(картку з відео не вдалося показати: {str(e)[:150]})")
            try:
                self.tg.send_message(chat, text, reply_markup=kb, html=False)
            except Exception as e2:  # noqa: BLE001
                log.error("Навіть текстову картку #%s не надіслано: %s", item.get("id"), e2)

    def _item_card_inner(self, chat: int, item: dict, kb: dict, prefix: str = "") -> None:
        cap = ""
        for n in (300, 150, 80, 40):
            cap = prefix + self.failed_text(item, err_len=n)
            if visible_len(cap) <= CAPTION_LIMIT:
                break
        if visible_len(cap) > CAPTION_LIMIT:
            cap = cap[:CAPTION_LIMIT - 1] + "…"
            cap = re.sub(r"<[^>]*$", "", cap)
        path = item["mp4"]
        if not Path(path).exists():
            return self.say(chat, cap + "\n<i>(файл відео не знайдено)</i>", reply_markup=kb)
        self.typing(chat)
        try:
            video = media.telegram_copy(path, self.cfg)
            info = media.probe(video, self.cfg["ffprobe"])
            self.tg.send_video(chat, video, cap, kb, width=info.get("width"), height=info.get("height"),
                               duration=info.get("duration"))
        except (media.MediaError, TelegramError, OSError) as e:
            log.warning("Картка #%s без відео: %s", item["id"], e)
            self.say(chat, cap, reply_markup=kb)

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

    def failed_list(self, chat: int) -> None:
        items = self.app.scheduler.failed_items()
        ignored = self.app.scheduler.failed_items(ignored=True)
        kb = ({"inline_keyboard": [[{"text": f"🙈 Проігноровані ({len(ignored)})", "callback_data": "ig:0"}]]}
              if ignored else None)
        if not items:
            return self.say(chat, "✅ Невдалих публікацій немає.", reply_markup=kb)
        self.say(chat, f"⚠️ <b>Невдалі публікації: {len(items)}</b>" + (" (показано 10)" if len(items) > 10 else "")
                       + "\n⏳ Відео надсилаються — великі файли готуються до хвилини.")
        for item in items[:10]:
            self._item_card(chat, item, self.retry_keyboard(item["id"]))
        if kb:
            self.say(chat, "Проігноровані відео — тут 👇", reply_markup=kb)

    def ignored_list(self, chat: int) -> None:
        items = self.app.scheduler.failed_items(ignored=True)
        if not items:
            return self.say(chat, "Проігнорованих відео немає.", html_mode=False)
        self.say(chat, f"🙈 <b>Проігноровані: {len(items)}</b> — бот про них не нагадує"
                       + (" (показано 10)" if len(items) > 10 else "")
                       + "\n⏳ Відео надсилаються — великі файли готуються до хвилини.")
        for item in items[:10]:
            kb = {"inline_keyboard": [[{"text": "↩️ Повернути до невдалих", "callback_data": f"ru:{item['id']}"}],
                                      [{"text": "🔁 Повторити зараз", "callback_data": f"rf:{item['id']}"}]]}
            self._item_card(chat, item, kb, prefix="🙈 ")

    # ---------- вибір відео з черги ----------
    def queue_keyboard(self, page: int = 0, per: int = 8) -> dict | None:
        plan = self.app.scheduler.projection()
        if not plan:
            return None
        now = now_ts()
        chunk = plan[page * per:(page + 1) * per]
        rows = [[{"text": f"#{it['id']} {Path(it['mp4']).stem[:24]} · {fmt_when(self.cfg, ts, now) if ts else '—'}",
                  "callback_data": f"qi:{it['id']}"}] for it, ts in chunk]
        nav = []
        if page > 0:
            nav.append({"text": "⬅️ Назад", "callback_data": f"qp:{page - 1}"})
        if (page + 1) * per < len(plan):
            nav.append({"text": "Далі ➡️", "callback_data": f"qp:{page + 1}"})
        if nav:
            rows.append(nav)
        return {"inline_keyboard": rows}

    def queue_button(self, chat: int, target: int, action: str, cq: dict, answer) -> None:
        if action == "qp":
            answer()
            kb = self.queue_keyboard(target)
            try:
                self.tg.edit_text(chat, cq["message"]["message_id"], self.app.queue_text(), kb)
            except TelegramError as e:
                log.debug("queue page: %s", e)
            return
        item = self.db.get_item(target)
        if not item or item["status"] != "queued":
            return answer("Цього відео вже немає в черзі", True)
        if action == "qi":
            answer()
            return self._queue_card(chat, item)
        if action == "qt":
            self.dialog_set(chat, {"item": target, "step": "time", "mode": "queue"})
            answer()
            return self._ask_time(chat, target, dialog=False)
        if action == "qf":
            self.db.update_item(target, qpos=self.db.first_qpos())
            plan = {it["id"]: ts for it, ts in self.app.scheduler.projection()}
            when = fmt_when(self.cfg, plan[target]) if plan.get(target) else "—"
            return answer(f"⬆️ Першим у черзі → {when}", True)
        if action == "qd":
            try:
                self.app.scheduler.cancel(target, by=chat)
            except ValueError as e:
                return answer(str(e)[:190], True)
            answer("🚫 Скасовано — файл у папці «скасовані»", True)
            return self.cleanup(chat)
        answer()

    def _queue_card(self, chat: int, item: dict) -> None:
        plan = {it["id"]: ts for it, ts in self.app.scheduler.projection()}
        ts = plan.get(item["id"])
        meta = self._meta(item)
        cap = (f"🎬 <b>{esc(meta.yt_title)}</b>\n📦 У черзі #{item['id']} → "
               f"<b>{fmt_when(self.cfg, ts) if ts else '—'}</b>\nОпублікувати раніше чи пізніше — «🕐 Обрати час».")
        kb = {"inline_keyboard": [
            [{"text": "🕐 Обрати час / ⚡ зараз", "callback_data": f"qt:{item['id']}"}],
            [{"text": "⬆️ Першим у черзі", "callback_data": f"qf:{item['id']}"},
             {"text": "🗑 Прибрати", "callback_data": f"qd:{item['id']}"}]]}
        try:
            video = media.telegram_copy(item["mp4"], self.cfg)
            info = media.probe(video, self.cfg["ffprobe"])
            self._track(chat, self.tg.send_video(chat, video, cap, kb, width=info.get("width"),
                                                 height=info.get("height"), duration=info.get("duration")))
        except (media.MediaError, TelegramError, OSError) as e:
            log.warning("Картка #%s без відео: %s", item["id"], e)
            self.tmp(chat, cap, reply_markup=kb)

    def _pick_from_queue(self, chat: int, item: dict, choice, answer=None) -> None:
        """Відео з черги на обраний час (або зараз): сповіщення з кнопками, далі — назва й хештеги."""
        item_id = item["id"]
        if choice is None:
            return self.dialog_set(chat, None)
        if isinstance(choice, int):
            try:
                self.app.scheduler.move_slot(item_id, choice)
            except ValueError as e:
                if answer:
                    answer(str(e), True)
                return self.tmp(chat, f"⚠️ {e}. Оберіть інший час.", html_mode=False)
            if answer:
                answer("Час обрано")
        self.db.update_item(item_id, status="notified", note=None)
        self.db.event(item_id, "picked", {"by": chat, "when": choice})
        self.dialog_set(chat, None)
        try:
            self.notify_item(item_id)
        except TelegramError as e:
            log.error("Сповіщення #%s: %s", item_id, e)
        if self.cfg.get("ask_texts_before_publish"):
            return self.dialog_start(chat, item_id, preset=choice)
        st = {"item": item_id, "mode": "dialog"}
        self._apply_time(chat, self.db.get_item(item_id), st, "now" if choice == "now" else None)

    # ---------- вибір часу публікації ----------
    def _ask_time(self, chat: int, item_id: int, dialog: bool) -> None:
        item = self.db.get_item(item_id)
        now = now_ts()
        rows = []
        passed = bool(item["slot_at"] and item["slot_at"] <= now + 60)
        if item["slot_at"] and not passed:  # минулий слот «залишити» не можна — це було б «зараз»
            rows.append([{"text": f"✅ Залишити: {fmt_when(self.cfg, item['slot_at'], now)}",
                          "callback_data": f"tk:{item_id}"}])
        free = self.app.scheduler.future_slots(now, 8)
        btns = [{"text": fmt_when(self.cfg, ts, now), "callback_data": f"ts:{item_id}:{ts}"} for ts in free]
        rows += [btns[i:i + 2] for i in range(0, len(btns), 2)]
        extra = [{"text": "⌨️ Свій час", "callback_data": f"tc:{item_id}"}]
        if dialog or item["status"] in ("approved", "queued", "failed", "posted"):
            extra.append({"text": "⚡ Зараз", "callback_data": f"tn:{item_id}"})
        rows.append(extra)
        rows.append([{"text": "❌ Скасувати", "callback_data": f"dlgx:{item_id}" if dialog else f"tx:{item_id}"}])
        head = (f"🔁 <b>Коли повторити публікацію #{item_id}?</b>" if item["status"] in ("failed", "posted") else
                "🕐 <b>Крок 3/3. Коли публікуємо?</b>" if dialog else
                f"🕐 <b>Коли опублікувати шортс #{item_id}?</b>" if item["status"] == "queued" else
                f"🕐 <b>Новий час для шортса #{item_id}</b>")
        note = (f"\n⚠️ Слот {fmt_slot(self.cfg, item['slot_at'], now)} уже минув — оберіть новий час." if passed else "")
        self.tmp(chat, f"{head}{note}\nОберіть вільний слот, «Свій час» (напр. <code>завтра 18:30</code>) або «⚡ Зараз».",
                 reply_markup={"inline_keyboard": rows})

    def time_button(self, chat: int, item_id: int, action: str, rest: list, answer) -> None:
        item = self.db.get_item(item_id)
        st = self.dialog_get(chat)
        from_queue = bool(st and st.get("item") == item_id and st.get("mode") == "queue")
        retry = bool(st and st.get("item") == item_id and st.get("mode") == "retry")
        ok = ("queued",) if from_queue else ("failed", "posted") if retry else ("notified", "approved")
        if not item or item["status"] not in ok:
            return answer("Цей шортс уже неактуальний", True)
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
            answer("Скасовано")
            return self.cleanup(chat)
        if action == "tc":
            self.dialog_set(chat, {**st, "step": "time_custom"})
            answer()
            return self.tmp(chat, "⌨️ Напишіть дату й час: <code>18:30</code>, <code>завтра 18:30</code> "
                                  "або <code>13.11 18:30</code>")
        if action == "tn":
            answer("Публікую зараз…")
            return self._apply_time(chat, item, st, "now")
        if action == "tk":
            if item["slot_at"] and item["slot_at"] <= now_ts() + 60:  # стара кнопка, а слот уже минув
                answer("Цей слот уже минув — оберіть новий час", True)
                return self._ask_time(chat, item_id, dialog=dialog)
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
            return self.tmp(chat, f"⚠️ {e}", html_mode=False)
        self._apply_time(chat, item, st, ts)

    def _apply_time(self, chat: int, item: dict, st: dict, choice, answer=None) -> None:
        """choice: timestamp — новий час; None — залишити; 'now' — опублікувати зараз."""
        item_id = item["id"]
        if st.get("mode") == "queue":
            return self._pick_from_queue(chat, item, choice, answer)
        if st.get("mode") == "retry":
            return self._retry_choice(chat, item, choice, answer)
        if isinstance(choice, int):
            try:
                self.app.scheduler.move_slot(item_id, choice)
            except ValueError as e:
                if answer:
                    answer(str(e), True)
                return self.tmp(chat, f"⚠️ {e}. Оберіть інший час.", html_mode=False)
            if answer:
                answer("Час змінено")
        dialog = st.get("mode") == "dialog"
        self.dialog_set(chat, None)
        item = self.db.get_item(item_id)
        # Час обрано явно (крок 3/3 або «Залишити») — публікуємо саме тоді, навіть якщо ввімкнено «одразу після підтвердження».
        now_pub = choice == "now"
        if dialog or now_pub:
            if item["status"] == "notified":
                self.db.update_item(item_id, status="approved")
                self.db.event(item_id, "approved", {"by": chat, "title": st.get("title"), "hashtags": st.get("tags")})
        if dialog:
            self._drop_texts(chat, item_id)
        self.cleanup(chat)
        if now_pub:
            self.app.scheduler.release_slot(item_id)
            return self.app.publisher.start(item_id)  # підпис відео покаже «⏫ Публікується…» → «✅ Опубліковано»
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

    # ---------- команди з панелі керування (виконуються тут, у процесі бота) ----------
    def start_panel_worker(self) -> None:
        def loop():
            while True:
                try:
                    for row in self.db.q("SELECT key, value FROM kv WHERE key LIKE 'pcmd:%'"):
                        self.db.x("DELETE FROM kv WHERE key=?", (row["key"],))
                        threading.Thread(target=self._panel_exec, args=(row["key"][5:], row["value"]),
                                         daemon=True, name="panel-cmd").start()
                except Exception as e:  # noqa: BLE001
                    log.warning("Команди з панелі: %s", e)
                time.sleep(1)
        threading.Thread(target=loop, daemon=True, name="panel-worker").start()

    def _panel_exec(self, cid: str, raw: str) -> None:
        try:
            cmd = json.loads(raw or "{}")
            res = {"ok": True, "message": self.panel_do(cmd)}
        except Exception as e:  # noqa: BLE001
            if not isinstance(e, ValueError):
                log.exception("Команда з панелі %s", raw)
            res = {"ok": False, "message": str(e)}
        self.db.set(f"pres:{cid}", json.dumps(res, ensure_ascii=False))

    def _panel_texts(self, item: dict, cmd: dict) -> bool:
        title, tags = cmd.get("title"), cmd.get("tags")
        if title is None and tags is None:
            return False
        raw = self._raw_meta(item)
        title = re.sub(r"\s+", " ", str(title or "")).strip()[:100] or raw.yt_title
        if tags is None:
            tag_list = raw.hashtags
        else:
            always = {t.lower() for t in self._always_line().split()}
            tag_list = [t for t in parse_hashtags(str(tags)) if t.lower() not in always]
        if title == raw.yt_title and tag_list == [t for t in raw.hashtags]:
            return False
        apply_manual(item["json"], title, tag_list)
        edited = [x for x in item["edited"] if x not in ("назву", "тексти")] + ["назву", "хештеги"]
        self.db.update_item(item["id"], title_override=None, edited=sorted(set(edited), key=edited.index))
        self.db.event(item["id"], "edited", {"by": "panel", "title": title, "hashtags": tag_list})
        return True

    def panel_do(self, cmd: dict) -> str:
        """Дія з панелі. Повертає текст для користувача; ValueError — зрозуміла помилка."""
        op = cmd.get("op")
        app = self.app
        if op == "update":
            text, kb = self.update_now(force=bool(cmd.get("force")))
            if kb:  # йде публікація — панель покаже питання
                raise ValueError(text + " Натисніть «⬆️ Оновити» ще раз, щоб оновитись усе одно.")
            return text
        if op == "publish_all":
            return self.publish_all(by="panel")
        if op == "details":
            return self.details_text(int(cmd.get("id") or 0))
        if op in ("report", "stats", "top"):
            from reports import collect, daily_report, weekly_top
            if op == "report":
                text = daily_report(app)
            else:
                stats = collect(app)
                if op == "stats":
                    import analytics
                    text = analytics.period_report(self.cfg, self.db, 1, list(app.platforms))
                else:
                    text = weekly_top(app, stats)
            if cmd.get("send"):
                self.send(text, html_mode=True)
            return text
        item_id = int(cmd.get("id") or 0)
        item = self.db.get_item(item_id)
        if not item:
            raise ValueError("Шортс не знайдено")
        st = item["status"]
        if op == "cancel":
            app.scheduler.cancel(item_id, by="panel")
            return f"🚫 #{item_id} скасовано — файл у папці «скасовані», наступний шортс стає на цей слот"
        if op == "restore":
            app.scheduler.restore(item_id)
            return f"↩️ #{item_id} повернуто — перший у черзі"
        if op in ("first", "remove") and st != "queued":
            raise ValueError("Цього відео вже немає в черзі")
        if op == "first":
            self.db.update_item(item_id, qpos=self.db.first_qpos())
            return f"⬆️ #{item_id} першим у черзі"
        if op == "remove":
            app.scheduler.cancel(item_id, by="panel")
            return f"🚫 #{item_id} скасовано — файл у папці «скасовані»"
        if st not in ("queued", "notified", "approved"):
            raise ValueError("Шортс уже публікується або опубліковано — оновіть сторінку")
        if app.publisher.is_active(item_id):
            raise ValueError("Шортс уже публікується")
        if op == "toggle":
            p = cmd.get("platform")
            if p not in PLATFORMS:
                raise ValueError("Невідома платформа")
            disabled = [x for x in item["disabled"] if x != p] if p in item["disabled"] else item["disabled"] + [p]
            self.db.update_item(item_id, disabled=disabled)
            self.refresh(item_id)
            return f"{NAMES[p]}: {'вимкнено' if p in disabled else 'увімкнено'} для #{item_id}"
        if op == "unapprove":
            if st == "approved":
                self.db.update_item(item_id, status="notified")
                self.refresh(item_id)
            return "Підтвердження скасовано — шортс чекає на підтвердження"
        if op == "regen":
            import ai
            if self.cfg.get("regenerate_command"):
                self._regen(item_id)
            elif ai.enabled(self.cfg):
                self._regen_ai(item_id)
            else:
                raise ValueError("Перегенерація недоступна: підключіть ШІ (вкладка «🤖 ШІ») або команду перегенерації")
            return "Тексти перегенеровано"
        if op not in ("publish", "save"):
            raise ValueError(f"Невідома дія {op}")

        when = cmd.get("when", "keep")
        if when not in ("keep", "now"):
            try:
                when = int(when)
            except (TypeError, ValueError):
                raise ValueError("Неправильний час") from None
        if st == "queued" and when == "keep" and op == "publish":
            raise ValueError("Оберіть час публікації або «Зараз»")
        if op == "save" and when == "now":
            when = "keep"
        if not app.publisher.targets(item) and op == "publish":
            raise ValueError("Усі платформи вимкнено — нічого публікувати")
        changed = self._panel_texts(item, cmd)
        if isinstance(when, int):
            app.scheduler.move_slot(item_id, when)
        msg = []
        if st == "queued" and when != "keep":
            self.db.update_item(item_id, status="notified", note=None)
            self.db.event(item_id, "picked", {"by": "panel", "when": when})
            try:
                self.notify_item(item_id)  # копія в Telegram — як звичайне сповіщення
            except Exception as e:  # noqa: BLE001
                log.warning("Сповіщення #%s: %s", item_id, e)
        item = self.db.get_item(item_id)
        if op == "publish":
            if item["status"] == "notified":
                self.db.update_item(item_id, status="approved")
                self.db.event(item_id, "approved", {"by": "panel"})
            if when == "now":  # у панелі час обрано явно — «залишити» означає «в цей слот», а не «зараз»
                app.scheduler.release_slot(item_id)
                app.publisher.start(item_id)
                return f"🚀 #{item_id} публікується зараз"
            msg.append(f"✅ #{item_id} підтверджено — публікація {fmt_when(self.cfg, self.db.get_item(item_id)['slot_at'])}")
        else:
            if changed:
                msg.append("Тексти збережено")
            if isinstance(when, int):
                msg.append(f"час: {fmt_when(self.cfg, when)}")
            msg = [", ".join(msg) or "Без змін"]
        self.refresh(item_id, texts=True)
        return "; ".join(msg)

    # ---------- повідомлення й команди ----------
    def on_message(self, m: dict, chat: int, role: str) -> None:
        text = (m.get("text") or "").strip()
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
                from reports import collect, weekly_top
                import analytics
                kb = None
                try:
                    stats = collect(app)
                    if which == "/stats":  # доба + кнопки періодів (тиждень … рік — з бази, миттєво)
                        text = analytics.period_report(self.cfg, self.db, 1, list(app.platforms))
                        kb = analytics.keyboard(1)
                    else:
                        text = weekly_top(app, stats)
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
            from config import VERSION
            wait = self.task_start(chat, f"🔍 Перевіряю оновлення… (у вас {VERSION})")

            def work():
                try:
                    text, kb = self.update_now(force=False)
                except Exception as e:  # noqa: BLE001
                    text, kb = f"⚠️ Оновлення не вдалося: {e}", None
                self.task_done(chat, wait, esc(text), kb)
            self._bg(work)
        elif cmd == "/stream":
            if role != "admin":
                return reply("⛔ Лише для адміністратора.", False)
            text, kb = self.stream_text()
            reply(text, kb=kb)
        elif cmd == "/failed":
            self._bg(self.failed_list, chat)
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
