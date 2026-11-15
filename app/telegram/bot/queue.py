"""Черга: картки на сьогодні, підтвердження часу, «опублікувати всі»."""
from __future__ import annotations

import json
import time
from app.core import media
from app.core.config import fmt_slot
from app.core.config import fmt_when
from app.core.config import local_dt
from app.core.config import now_ts
from app.core.config import parse_when
from app.telegram.api import TelegramError
from pathlib import Path
from .common import (
    esc,
    log,
)


class QueueMixin:
    def publish_all(self, by="", chat: int | None = None) -> str:
        """Опублікувати всі шортси, що чекають на підтвердження (пропущені слоти) — по черзі.
        Якщо ввімкнено «питати назву й хештеги» і натиснуто в Telegram — спершу назва й хештеги для кожного."""
        items = [i for i in self.db.items("status='notified'", order="slot_at")
                 if self.app.publisher.targets(i) and not self.app.publisher.is_active(i["id"])]
        if not items:
            raise ValueError("Немає шортсів, що чекають на підтвердження")
        ids = [i["id"] for i in items]
        if chat and self.cfg.get("ask_texts_before_publish"):
            self.dialog_start(chat, ids[0], preset="bulk", bulk=ids[1:])
            return f"✍️ Назва й хештеги для кожного ({len(ids)} шт.), далі — публікація по черзі"
        self._queue_bulk(ids, by)
        return f"🚀 Публікую по черзі: {', '.join('#' + str(i) for i in ids)}"

    def _queue_bulk(self, ids: list[int], by="") -> None:
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

    def queue_cards(self, item_ids: list[int], limit: int = 10) -> int:
        """Щойно шортс став у чергу — картка з відео в чаті: час публікації й кнопки (час / першим / скасувати).
        За годину до слоту вона замінюється карткою на підтвердження."""
        sent = 0
        admins = set(self.admins())
        for item_id in item_ids[:limit]:
            item = self.db.get_item(item_id)
            if not item or item["status"] != "queued" or self.db.tg_msgs(item_id):
                continue
            try:
                video = media.telegram_copy(item["mp4"], self.cfg)
                info = media.probe(video, self.cfg["ffprobe"])
            except (media.MediaError, OSError) as e:
                log.warning("Картка черги #%s без відео: %s", item_id, e)
                video, info = None, {}
            cap, kb = self.caption(item), self.keyboard(item)
            file_id = None
            for cid in self.recipients():
                markup = kb if cid in admins else None
                try:
                    if video:
                        msg = self.tg.send_video(cid, file_id or video, cap, markup, width=info.get("width"),
                                                 height=info.get("height"), duration=info.get("duration"))
                        file_id = file_id or ((msg or {}).get("video") or {}).get("file_id")
                        is_text = 0
                    else:
                        msg, is_text = self.tg.send_message(cid, cap, reply_markup=markup), 1
                    self.db.set_tg_msg(item_id, cid, msg["message_id"], None, is_text)
                    self.db.set(f"cap:{item_id}:{cid}", self._cap_digest(cap, markup))
                except Exception as e:  # noqa: BLE001
                    log.warning("Картка черги #%s для %s: %s", item_id, cid, e)
            sent += 1
        return sent

    def today_queue_ids(self) -> list[int]:
        """Шортси з черги, що за планом виходять сьогодні (у порядку черги)."""
        now = now_ts()
        day = local_dt(self.cfg, now).date()
        return [it["id"] for it, ts in self.app.scheduler.projection(now) if ts and local_dt(self.cfg, ts).date() == day]

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
        if action == "qa":  # підтвердити зараз на запланований час: назва, хештеги — і готово
            plan = {it["id"]: ts for it, ts in self.app.scheduler.projection()}
            ts = plan.get(target)
            if not ts:
                return answer("Для цього шортса ще немає часу (розклад на паузі?) — оберіть час вручну", True)
            answer("Назва й хештеги — і шортс вийде о " + fmt_slot(self.cfg, ts))
            return self._pick_from_queue(chat, item, ts)
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
