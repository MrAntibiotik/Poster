"""Публікація у слот, пропуск/скасування/відновлення, сповіщення."""
from __future__ import annotations

from app.core import media
from app.core.config import fmt_slot
from app.core.config import now_ts
from app.core.meta import move_pair
from app.core.meta import read_json
from app.core.meta import write_json
from pathlib import Path
from .common import (
    log,
)


class PublishingMixin:
    def resume_publishing(self) -> None:
        for item in self.db.items("status='publishing'"):
            if not self.app.publisher.is_active(item["id"]):
                log.info("Відновлюю перервану публікацію #%s", item["id"])
                self.app.publisher.start(item["id"])

    def process_due_slots(self, now: int) -> None:
        for s in self.db.q("SELECT * FROM slots WHERE state='retry' AND slot_at<=? ORDER BY slot_at", (now,)):
            self.db.set_slot(s["slot_at"], state="done")
            item = self.db.get_item(s["item_id"]) if s["item_id"] else None
            if item and item["status"] in ("failed", "posted"):
                log.info("Повторна публікація #%s за розкладом", item["id"])
                self.app.publisher.start(item["id"])
        for s in self.db.q("SELECT * FROM slots WHERE state='waiting' ORDER BY slot_at"):
            item = self.db.get_item(s["item_id"]) if s["item_id"] else None
            if not item or item["slot_at"] != s["slot_at"] or item["status"] not in ("notified", "approved"):
                self.db.set_slot(s["slot_at"], state="done")
            elif item["status"] == "approved" and (self.cfg.get("power") or {}).get("hold_during_stream") \
                    and self.stream_reason(now):
                continue  # чекаємо кінця стріму
            elif item["status"] == "approved":  # підтвердили після слоту — публікуємо одразу
                self.db.set_slot(s["slot_at"], state="done")
                log.info("#%s підтверджено після слоту — публікую зараз", item["id"])
                self.app.publisher.start(item["id"])
        for s in self.db.q("SELECT * FROM slots WHERE state IN ('pending','notified') AND slot_at<=? "
                           "ORDER BY slot_at", (now,)):
            if s["state"] == "pending":
                self.db.set_slot(s["slot_at"], state="empty")
                self.db.event(None, "empty_slot", {"slot": s["slot_at"]})
                continue
            item = self.db.get_item(s["item_id"]) if s["item_id"] else None
            if not item or item["slot_at"] != s["slot_at"]:
                self.db.set_slot(s["slot_at"], state="done")
                continue
            if item["status"] == "approved" and (self.cfg.get("power") or {}).get("hold_during_stream") \
                    and self.stream_reason(now):
                self.db.set_slot(s["slot_at"], state="waiting")
                log.info("Стрім — публікацію #%s відкладено до його завершення", item["id"])
                self.app.bot.send(f"🎮 Йде стрім — публікацію #{item['id']} відкладено, опублікую одразу після нього.")
                continue
            if item["status"] == "approved":
                late = now - s["slot_at"]
                if late <= int(self.cfg["catchup_minutes"]) * 60:
                    self.db.set_slot(s["slot_at"], state="done")
                    log.info("Слот %s: публікую #%s", fmt_slot(self.cfg, s["slot_at"]), item["id"])
                    self.app.publisher.start(item["id"])
                else:  # ПК був вимкнений під час слоту — повертаємо на початок черги без штрафу
                    self.db.set_slot(s["slot_at"], state="missed")
                    self.db.update_item(item["id"], status="queued", qpos=self.db.first_qpos(), slot_at=None,
                                        note="↩️ Слот минув, поки ПК був вимкнений — шортс повернуто на початок черги")
                    self.app.bot.refresh(item["id"])
                    self.db.update_item(item["id"], note=None)
            elif item["status"] == "notified":
                mode = self.cfg.get("on_missed_approval", "wait")
                if mode == "auto":
                    self.db.set_slot(s["slot_at"], state="done")
                    self.db.update_item(item["id"], status="approved")
                    self.db.event(item["id"], "approved", {"by": "auto"})
                    log.info("Слот %s: #%s не підтверджено — публікую автоматично", fmt_slot(self.cfg, s["slot_at"]), item["id"])
                    self.app.publisher.start(item["id"])
                elif mode == "wait":
                    self.db.set_slot(s["slot_at"], state="waiting")
                    self.db.event(item["id"], "waiting", {"slot": s["slot_at"]})
                    log.info("Слот %s: #%s чекає на підтвердження", fmt_slot(self.cfg, s["slot_at"]), item["id"])
                    self.app.bot.refresh(item["id"])
                else:
                    self.db.set_slot(s["slot_at"], state="skipped")
                    self.skip(item)
            else:
                self.db.set_slot(s["slot_at"], state="done")

    def skip(self, item: dict) -> None:
        skips = item["skips"] + 1
        title = self._title(item)
        self.db.event(item["id"], "skipped", {"title": title, "skips": skips})
        if skips >= int(self.cfg["max_skips"]):
            try:
                mp4, js = move_pair(item["mp4"], item["json"], Path(self.cfg["paths"]["skipped"]))
            except OSError as e:
                log.error("#%s: не вдалося перенести в skipped/: %s", item["id"], e)
                mp4, js = item["mp4"], item["json"]
            media.drop_previews(item["mp4"], self.cfg)
            try:
                data = read_json(js)
                data["status"] = "skipped"
                write_json(js, data)
            except Exception:  # noqa: BLE001
                pass
            self.db.update_item(item["id"], status="skipped", skips=skips, mp4=mp4, json=js)
            self.db.event(item["id"], "moved_to_skipped", {"title": title})
            if hasattr(self.app.bot, "remove_item_msgs"):
                self.app.bot.remove_item_msgs(item["id"])   # більше не актуальний — з чату прибираємо
            else:
                self.app.bot.refresh(item["id"])
            self.app.bot.send(f"🗑 Шортс #{item['id']} «{title}» пропущено {skips} рази — "
                              f"файли перенесено в skipped/.")
            log.info("#%s → skipped/", item["id"])
        else:
            self.db.update_item(item["id"], status="queued", skips=skips, qpos=self.db.next_qpos())
            self.app.bot.refresh(item["id"])
            self.db.update_item(item["id"], slot_at=None)
            log.info("#%s пропущено (%s/%s) → у кінець черги", item["id"], skips, self.cfg["max_skips"])

    def cancel(self, item_id: int, by="") -> dict:
        """Скасувати шортс: файл — у папку «скасовані», слот звільняється, черга зсувається (наступний — у цей слот)."""
        item = self.db.get_item(item_id)
        if not item or item["status"] not in ("queued", "notified", "approved"):
            raise ValueError("Цей шортс уже не можна скасувати (публікується, опублікований або прибраний)")
        if self.app.publisher.is_active(item_id):
            raise ValueError("Шортс уже публікується")
        now = now_ts()
        origin = str(Path(item["mp4"]).parent)
        try:
            mp4, js = move_pair(item["mp4"], item["json"], Path(self.cfg["paths"]["cancelled"]))
        except OSError as e:
            raise ValueError(f"Не вдалося перенести файл у папку скасованих: {e}") from e
        media.drop_previews(item["mp4"], self.cfg)
        if item["slot_at"]:
            if item["slot_at"] > now:  # слот ще попереду — віддаємо його наступному в черзі
                self.db.x("UPDATE slots SET state='pending', item_id=NULL WHERE slot_at=? AND item_id=?",
                          (item["slot_at"], item_id))
            else:
                self.db.x("UPDATE slots SET state='done' WHERE slot_at=? AND item_id=?", (item["slot_at"], item_id))
        self.db.update_item(item_id, status="cancelled", mp4=mp4, json=js, note=None)
        self.db.set(f"cancel_from:{item_id}", origin)
        self.db.event(item_id, "cancelled", {"by": by, "title": self._title(self.db.get_item(item_id))})
        log.info("#%s скасовано → %s", item_id, mp4)
        if hasattr(self.app.bot, "remove_item_msgs"):
            self.app.bot.remove_item_msgs(item_id)   # чат — лише актуальне; повернути можна в панелі («Скасовані»)
        else:
            self.app.bot.refresh(item_id)
        self.process_notifications(now, min_lead=60)  # наступний із черги — одразу в цей слот
        return self.db.get_item(item_id)

    def restore(self, item_id: int) -> dict:
        """Повернути скасований шортс: файл назад у папку з шортсами, першим у черзі."""
        item = self.db.get_item(item_id)
        if not item or item["status"] != "cancelled":
            raise ValueError("Цей шортс не скасований")
        if not Path(item["mp4"]).exists():
            raise ValueError("Файл у папці скасованих не знайдено")
        origin = Path(self.db.get(f"cancel_from:{item_id}") or self.cfg["paths"]["ready"])
        ready = Path(self.cfg["paths"]["ready"]).resolve()
        if ready != origin.resolve() and ready not in origin.resolve().parents:
            origin = ready  # папку з шортсами змінили — повертаємо в поточну
        mp4, js = move_pair(item["mp4"], item["json"], origin)
        self.db.update_item(item_id, status="queued", mp4=mp4, json=js, slot_at=None, qpos=self.db.first_qpos(),
                            note="↩️ Повернуто зі скасованих — перший у черзі")
        self.db.set(f"cancel_from:{item_id}", None)
        self.db.event(item_id, "restored", {})
        self.app.bot.refresh(item_id)
        self.db.update_item(item_id, note=None)
        return self.db.get_item(item_id)

    def process_notifications(self, now: int, min_lead: int | None = None) -> None:
        if self.db.get("paused") == "1":
            return  # пауза: нові шортси не надсилаються, слоти минають порожніми
        lead = int(self.cfg["notify_before_minutes"]) * 60
        forced = min_lead
        min_lead = int(self.cfg["min_notify_lead_minutes"]) * 60
        if (self.cfg.get("power") or {}).get("sleep_between_slots"):
            min_lead = 60  # ПК прокидається за 5 хв — сповіщення має встигнути
        if forced is not None:
            min_lead = forced
        for s in self.db.q("SELECT * FROM slots WHERE state='pending' AND slot_at>? AND slot_at<=? "
                           "ORDER BY slot_at", (now + min_lead, now + lead)):
            item = self._next_item(s.get("lane") or "short")
            if not item:
                continue  # для цієї доріжки (шортси / хайлайти) черга порожня
            with self.db.tx():
                self.db.conn.execute("UPDATE slots SET item_id=?, state='notified' WHERE slot_at=?",
                                     (item["id"], s["slot_at"]))
                self.db.conn.execute("UPDATE items SET status='notified', slot_at=? WHERE id=?",
                                     (s["slot_at"], item["id"]))
            try:
                self.app.bot.notify_item(item["id"])
            except Exception as e:  # noqa: BLE001
                log.error("Не вдалося надіслати сповіщення #%s: %s — спробую пізніше", item["id"], e)
                with self.db.tx():
                    self.db.conn.execute("UPDATE slots SET item_id=NULL, state='pending' WHERE slot_at=?",
                                         (s["slot_at"],))
                    self.db.conn.execute("UPDATE items SET status='queued', slot_at=NULL WHERE id=?", (item["id"],))
                return
            self.db.event(item["id"], "notified", {"slot": s["slot_at"]})
            log.info("Сповіщення: #%s на слот %s", item["id"], fmt_slot(self.cfg, s["slot_at"]))
