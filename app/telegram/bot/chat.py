"""Чистий чат: тимчасові повідомлення, завдання, що виконуються, прибирання."""
from __future__ import annotations

import json
from app.core.config import now_ts
from app.telegram.api import TelegramError
from .common import (
    AUTO_TTL,
    REPLY_TTL,
    esc,
    log,
)


class ChatMixin:
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
        """Лишає в чаті лише відео опублікованих шортсів і тих, що зараз у роботі (чекають підтвердження,
        підтверджені, публікуються, не вийшли — з кнопкою повтору) + кнопки внизу. Решта (картки черги,
        скасовані, пропущені, відповіді) — геть. Telegram дозволяє видаляти лише повідомлення до 48 год."""
        keep = set()
        for r in self.db.q("SELECT t.msg_id, t.item_id, i.status FROM tg_msgs t JOIN items i ON i.id=t.item_id "
                           "WHERE t.chat_id=?", (int(chat_id),)):
            if r["status"] in ("posted", "publishing", "notified", "approved", "failed"):
                keep.add(int(r["msg_id"]))
            else:  # картка черги / скасованого / пропущеного — прибираємо і забуваємо
                self.db.x("DELETE FROM tg_msgs WHERE chat_id=? AND msg_id=?", (int(chat_id), r["msg_id"]))
                if r["status"] == "queued":
                    self.db.set(f"noqcard:{r['item_id']}", 1)  # не надсилати знову — прийде на підтвердження
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

    def remove_item_msgs(self, item_id: int) -> None:
        """Відхилений (скасований / остаточно пропущений) шортс зникає з чату повністю: відео, тексти, картки."""
        for r in self.db.tg_msgs(item_id):
            self._delete(r["chat_id"], [int(x) for x in (r["msg_id"], r.get("text_msg_id")) if x])
            self.db.set(f"cap:{item_id}:{r['chat_id']}", None)
        self.db.clear_tg_msgs(item_id)
        self.db.update_item(item_id, tg_msg=None)
        if hasattr(self, "drop_failed_card"):
            self.drop_failed_card(item_id)

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
