"""Обробка натискань inline-кнопок."""
from __future__ import annotations

import threading
from app.core.config import NAMES
from app.core.config import PLATFORMS
from app.core.config import fmt_slot
from app.core.config import now_ts
from app.telegram.api import TelegramError
from .common import (
    esc,
    log,
)


class CallbacksMixin:
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
        if action == "eu" and rest:
            if self.role(chat) != "admin":
                return answer("⛔ Лише для адміністратора", True)
            from app.channels import eu_tab
            toast = eu_tab.action(rest[0]) if rest[0] != "refresh" else "🔄 Оновлюю…"
            answer(toast[:200])
            text, kb = eu_tab.view(home_tz=self.cfg["tz"])
            try:
                self.tg.edit_text(chat, cq["message"]["message_id"], text, kb)
            except TelegramError as e:
                if "not modified" not in str(e):
                    log.debug("edit eu: %s", e)
            return
        if action == "dup" and rest:
            if self.role(chat) != "admin":
                return answer("⛔ Лише для адміністратора", True)
            result = self.resolve_duplicate(target, rest[0] == "yes", by=chat)
            answer(result[:200])
            try:
                self.tg.edit_text(chat, cq["message"]["message_id"], result, None)
            except TelegramError as e:
                log.debug("edit dup: %s", e)
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
                    answer("🚫 Скасовано й прибрано з чату. Наступний шортс стає на цей слот. Повернути — панель → «Скасовані».", True)
                elif action == "cr":
                    self.app.scheduler.restore(target)
                    answer("↩️ Повернуто — перший у черзі", True)
                else:
                    answer(self.publish_all(by=chat, chat=chat)[:190], True)
            except ValueError as e:
                answer(str(e)[:190], True)
            return
        if action == "an":
            from app.insights import analytics
            if target == analytics.TOP:   # «🏆 Топ тижня» — тепер у аналітиці
                answer("🏆 Збираю топ тижня…")
                mid = cq["message"]["message_id"]

                def top():
                    from app.insights.reports import collect, weekly_top
                    try:
                        text = weekly_top(self.app, collect(self.app))
                    except Exception as e:  # noqa: BLE001
                        text = f"❌ Не вдалося зібрати статистику: {esc(str(e))}"
                    try:
                        self.tg.edit_text(chat, mid, text, analytics.keyboard(analytics.TOP))
                    except TelegramError as e:
                        log.debug("top edit: %s", e)
                return self._bg(top)
            answer(analytics.PERIODS.get(target, ""))
            try:
                self.tg.edit_text(chat, cq["message"]["message_id"],
                                  analytics.period_report(self.cfg, self.db, target, list(self.app.platforms)),
                                  analytics.keyboard(target))
            except TelegramError as e:
                log.debug("analytics edit: %s", e)
            return
        if action in ("igdone", "igskip"):
            if self.role(chat) != "admin":
                return answer("⛔ Лише для адміністратора", True)
            return self.instagram_manual_done(chat, target, action == "igdone", answer)
        if action in ("hold", "unhold"):
            if self.role(chat) != "admin":
                return answer("⛔ Лише для адміністратора", True)
            item = self.db.get_item(target)
            if not item or item["status"] != "publishing":
                return answer("Цей шортс уже не публікується", True)
            if action == "hold":
                where = self.app.publisher.hold(target)
                answer("⏸ На паузі — " + ("зупиняю за кілька секунд, далі наступний" if where == "running"
                                          else "прибрано з черги, далі наступний"), True)
            else:
                if self.db.get("stopped") == "1":
                    return answer("⛔ Публікації зупинено — спершу «✅ Відновити публікації»", True)
                self.app.publisher.release(target)
                answer("▶️ Повернуто в чергу публікацій")
            return self.refresh(target)
        if action == "ver":
            return self.version_callback(chat, cq["message"]["message_id"], target, rest, answer)
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
        if action in ("qp", "qi", "qt", "qf", "qd", "qa"):
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
            if self.cfg.get("ask_texts_before_publish") or item.get("kind") == "highlight":
                answer("Введіть назву" + (" і надішліть прев'ю" if item.get("kind") == "highlight" else " й хештеги"))
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
            from app.core import ai
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
