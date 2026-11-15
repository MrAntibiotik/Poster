"""Невдалі та проігноровані: картки помилок, повтор."""
from __future__ import annotations

from app.core.config import NAMES
from app.core.config import fmt_when
from .common import (
    esc,
)


class FailedMixin:
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
        if not item or item["status"] not in ("failed", "posted", "publishing"):
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
            if self.db.get("stopped") == "1":
                return answer("⛔ Публікації зупинено — спершу «✅ Відновити публікації»", True)
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
