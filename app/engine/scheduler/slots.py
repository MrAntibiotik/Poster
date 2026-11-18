"""Слоти публікацій: розклад, перенесення, повтори."""
from __future__ import annotations

from app.core.config import fmt_slot
from app.core.config import highlights_on
from app.core.config import local_dt
from app.core.config import now_ts
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from .common import (
    log,
)


class SlotsMixin:
    def future_slots(self, now: int, count: int, lane: str = "short") -> list[int]:
        """Найближчі вільні слоти (у т.ч. наступних днів і позапланові) — для розкладу черги.
        lane: "short" — слоти шортсів, "highlight" — слоти хайлайтів."""
        if count <= 0:
            return []
        min_lead = int(self.cfg["min_notify_lead_minutes"]) * 60
        rows = self.db.q("SELECT slot_at, state, item_id FROM slots WHERE slot_at>? AND lane=?",
                         (now + min_lead, lane))
        taken_any = {r["slot_at"] for r in self.db.q("SELECT slot_at FROM slots WHERE slot_at>?", (now + min_lead,))}
        taken = {r["slot_at"] for r in rows if r["state"] != "pending" or r["item_id"]}
        free = {r["slot_at"] for r in rows if r["state"] == "pending" and not r["item_id"]}
        today = local_dt(self.cfg, now).date()
        for d in range(400):  # до ~1 року наперед
            day = today + timedelta(days=d)
            for ts in self._day_slots(day, lane):
                if ts > now + min_lead and ts not in taken and (ts in free or ts not in taken_any):
                    free.add(ts)
            # слоти з БД (сьогодні/завтра, позапланові) вже враховано; далі дні лише пізніші
            if d >= 1 and len(free) >= count:
                break
        return sorted(free)[:count]

    def projection(self, now: int | None = None) -> list[tuple[dict, int | None]]:
        """Черга з часом публікації: перше відео — у найближчий вільний слот, друге — у наступний…"""
        now = now or now_ts()
        queue = [it for it in self.db.queue() if Path(it["mp4"]).exists()]  # видалені файли не показуємо
        if self.db.get("paused") == "1":
            return [(it, None) for it in queue]
        when: dict[int, int | None] = {}
        for lane in ("short", "highlight"):           # шортси і хайлайти — кожні у своїх слотах
            part = [it for it in queue if (it.get("kind") or "short") == lane]
            slots = self.future_slots(now, len(part), lane) if (lane == "short" or highlights_on(self.cfg)) else []
            for i, it in enumerate(part):
                when[it["id"]] = slots[i] if i < len(slots) else None
        return [(it, when.get(it["id"])) for it in queue]

    def move_slot(self, item_id: int, ts: int) -> None:
        """Переносить сповіщений/підтверджений шортс на інший час; старий слот віддається черзі."""
        now = now_ts()
        if ts <= now + 60:
            raise ValueError("Цей час уже минув або надто близько")
        item = self.db.get_item(item_id)
        existing = self.db.slot(ts)
        if existing and existing["item_id"] and existing["item_id"] != item_id:
            raise ValueError("На цей час уже запланований інший шортс")
        if existing and existing["state"] not in ("pending", "notified"):
            raise ValueError("Цей слот уже використано")
        old = item["slot_at"]
        hm = local_dt(self.cfg, ts).strftime("%H:%M")
        with self.db.tx():
            c = self.db.conn
            if old and old != ts:
                row = c.execute("SELECT source FROM slots WHERE slot_at=?", (old,)).fetchone()
                if row and row["source"] == "manual":
                    c.execute("DELETE FROM slots WHERE slot_at=? AND item_id=?", (old, item_id))
                else:
                    c.execute("UPDATE slots SET state='pending', item_id=NULL WHERE slot_at=? AND item_id=?",
                              (old, item_id))
            if existing:
                c.execute("UPDATE slots SET state='notified', item_id=? WHERE slot_at=?", (item_id, ts))
            else:
                c.execute("INSERT INTO slots(slot_at, source, item_id, state) VALUES (?,?,?, 'notified')",
                          (ts, "config" if hm in self.cfg["slots"] else "manual", item_id))
            c.execute("UPDATE items SET slot_at=? WHERE id=?", (ts, item_id))
        self.db.event(item_id, "rescheduled", {"from": old, "to": ts})
        log.info("#%s перенесено: %s → %s", item_id, fmt_slot(self.cfg, old) if old else "—", fmt_slot(self.cfg, ts))

    def reserve_retry(self, item_id: int, ts: int | None = None) -> int:
        """Повтор невдалих платформ: у вказаний час або в найближчий вільний слот (слот резервується)."""
        now = now_ts()
        if ts is None:
            free = self.future_slots(now, 1)
            if not free:
                raise ValueError("Немає вільних слотів")
            ts = free[0]
        if ts <= now + 60:
            raise ValueError("Цей час уже минув або надто близько")
        ex = self.db.slot(ts)
        if ex and (ex["item_id"] or ex["state"] != "pending"):
            raise ValueError("Цей час уже зайнятий")
        self.db.x("DELETE FROM slots WHERE state='retry' AND item_id=?", (item_id,))  # лише один запланований повтор
        if ex:
            self.db.x("UPDATE slots SET state='retry', item_id=? WHERE slot_at=?", (item_id, ts))
        else:
            self.db.x("INSERT INTO slots(slot_at, source, item_id, state) VALUES (?, 'manual', ?, 'retry')",
                      (ts, item_id))
        self.db.event(item_id, "retry_planned", {"at": ts})
        return ts

    def retry_soon(self, item_id: int) -> int:
        """Повтор якнайшвидше (для панелі: службу запускає планувальник протягом ~1–2 хв)."""
        ts = (now_ts() + 90) // 60 * 60 + 60
        while self.db.slot(ts):
            ts += 60
        return self.reserve_retry(item_id, ts)

    def retry_at(self, item_id: int) -> int | None:
        r = self.db.one("SELECT slot_at FROM slots WHERE state='retry' AND item_id=?", (item_id,))
        return r["slot_at"] if r else None

    def failed_items(self, days: int = 30, ignored: bool = False) -> list[dict]:
        """Шортси, що не вийшли хоча б на одну увімкнену платформу.
        ignored=False — без проігнорованих; ignored=True — лише проігноровані."""
        out = []
        for item in self.db.items("status IN ('failed','posted') AND (posted_at IS NULL OR posted_at>=?)",
                                  (now_ts() - days * 86400,), order="id DESC"):
            if bool(self.db.get(f"ignored:{item['id']}")) != ignored:
                continue
            errs = {p: r for p, r in item["results"].items()
                    if r.get("status") not in ("ok", "manual", "skipped", "processing", "ig_wait", "ig_next") and p in self.app.platforms and p not in item["disabled"]}
            if item["status"] == "failed" or errs:
                out.append({**item, "errors": errs})
        return out

    def release_slot(self, item_id: int) -> None:
        """Звільняє слот шортса (коли його публікують «зараз»)."""
        item = self.db.get_item(item_id)
        if item and item["slot_at"]:
            self.db.x("UPDATE slots SET state='done' WHERE slot_at=? AND item_id=?", (item["slot_at"], item_id))

    def _day_slots(self, day, lane: str = "short") -> list[int]:
        def at(hm: str) -> int:
            h, m = map(int, str(hm).split(":"))
            return int(datetime(day.year, day.month, day.day, h, m, tzinfo=self.cfg["tz"]).timestamp())
        shorts = [at(hm) for hm in self.cfg["slots"]]
        if lane == "short":
            return shorts
        if not highlights_on(self.cfg):
            return []
        out = []
        for hm in self.cfg["highlights"]["slots"]:
            ts = at(hm)
            while ts in shorts:  # той самий час, що й у шортса, — хайлайт на хвилину пізніше
                ts += 60
            out.append(ts)
        return out

    def ensure_slots(self, now: int) -> None:
        today = local_dt(self.cfg, now).date()
        wanted: dict[int, str] = {}
        for lane in ("highlight", "short"):  # шортс перемагає, якщо час збігся
            for d in (today, today + timedelta(days=1)):
                for ts in self._day_slots(d, lane):
                    wanted[ts] = lane
        for ts, lane in wanted.items():
            self.db.x("INSERT OR IGNORE INTO slots(slot_at, source, state, lane) VALUES (?, 'config', 'pending', ?)",
                      (ts, lane))
            self.db.x("UPDATE slots SET lane=? WHERE slot_at=? AND source='config' AND state='pending' "
                      "AND item_id IS NULL", (lane, ts))
        # Слоти, прибрані з config.json, ще не використані — видаляємо
        for s in self.db.q("SELECT slot_at FROM slots WHERE source='config' AND state='pending' AND slot_at>? "
                           "AND item_id IS NULL", (now,)):
            if s["slot_at"] not in wanted:
                self.db.x("DELETE FROM slots WHERE slot_at=?", (s["slot_at"],))

    def add_manual_slot(self, minutes: int) -> int:
        minutes = max(int(minutes), int(self.cfg["min_notify_lead_minutes"]) + 2)
        ts = (now_ts() + minutes * 60 + 59) // 60 * 60
        while self.db.slot(ts):
            ts += 60
        self.db.x("INSERT INTO slots(slot_at, source, state) VALUES (?, 'manual', 'pending')", (ts,))
        log.info("Додано ручний слот %s", fmt_slot(self.cfg, ts))
        # Якщо шортс уже чекає на пізніший слот — переносимо його на цей (тест «зараз», а не о 19:00)
        later = self.db.one("SELECT s.slot_at, s.item_id FROM slots s JOIN items i ON i.id=s.item_id "
                            "WHERE s.state='notified' AND s.slot_at>? AND i.status IN ('notified','approved') "
                            "ORDER BY s.slot_at LIMIT 1", (ts,))
        if later:
            with self.db.tx():
                self.db.conn.execute("UPDATE slots SET state='pending', item_id=NULL WHERE slot_at=?",
                                     (later["slot_at"],))
                self.db.conn.execute("UPDATE slots SET state='notified', item_id=? WHERE slot_at=?",
                                     (later["item_id"], ts))
                self.db.conn.execute("UPDATE items SET slot_at=? WHERE id=?", (ts, later["item_id"]))
            log.info("#%s перенесено зі слоту %s на ручний %s", later["item_id"],
                     fmt_slot(self.cfg, later["slot_at"]), fmt_slot(self.cfg, ts))
            self.app.bot.refresh(later["item_id"])
            self.db.set("refresh_item", later["item_id"])  # якщо слот додано з панелі/меню — оновить служба
        return ts

    def slot_plan(self, now: int | None = None, limit: int = 6) -> list[dict]:
        """Найближчі слоти разом із тим, що в них вийде: уже сповіщений шортс або наступний із черги."""
        now = now or now_ts()
        planned = {ts: it for it, ts in self.projection(now) if ts}
        out = []
        for s in self.upcoming(now, limit):
            item = self.db.get_item(s["item_id"]) if s["item_id"] else planned.get(s["slot_at"])
            kind = (item["status"] if item and s["item_id"] else "planned" if item else
                    "paused" if self.db.get("paused") == "1" else "empty")
            out.append({"slot_at": s["slot_at"], "manual": s["source"] == "manual", "item": item, "kind": kind,
                        "lane": s.get("lane") or "short"})
        return out

    def upcoming(self, now: int, limit: int = 6) -> list[dict]:
        return self.db.q("SELECT * FROM slots WHERE slot_at>? AND state IN ('pending','notified') "
                         "ORDER BY slot_at LIMIT ?", (now, limit))
