"""Розклад і черга: сканування ready/, слоти, сповіщення за годину, пропуски, звіти."""
from __future__ import annotations

import html
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import media
from config import fmt_slot, fmt_when, local_dt, now_ts
from meta import auto_meta_json, json_status, load_meta, move_pair, read_json, write_json

log = logging.getLogger("scheduler")


SKIP_DIRS = {"posted", "skipped", "cancelled", "data", "tokens", "previews", ".venv", "__pycache__"}


def find_videos(folder: Path, depth: int = 4, exclude: tuple = ()) -> list[Path]:
    """mp4 у папці та підпапках до `depth` рівнів (напр. output/job_…/battle_…/відео.mp4).

    Розширення без урахування регістру; папки posted/skipped/службові і `exclude` пропускаються.
    """
    out: list[Path] = []
    excl = {Path(e).resolve() for e in exclude}

    def walk(d: Path, level: int) -> None:
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return
        for e in entries:
            try:
                if e.is_file() and e.suffix.lower() == ".mp4":
                    out.append(e)
                elif (e.is_dir() and level < depth and not e.name.startswith(".")
                      and e.name.lower() not in SKIP_DIRS and e.resolve() not in excl):
                    walk(e, level + 1)
            except OSError:
                continue
    walk(folder, 0)
    return out


AUTO_FOLDER = re.compile(r"(?i)^(battle|job|clip|short|video|output|replay|render|export)s?[_\-\s]?\d"
                         r"|_(dmg|assist|kills?|frags?)_\d|^\d{4}[_\-.]?\d\d[_\-.]?\d\d|^\d+$")


def is_auto_folder(name: str) -> bool:
    """Папки, які створює програма нарізки (battle_02_dmg_5004, job_20261111_…), — це не назва гри."""
    return bool(AUTO_FOLDER.search(name or ""))


def game_of(mp4: Path, ready: Path) -> str:
    """Назва «гри» = перша «людська» підпапка в папці з шортсами (ready/WoT/… → WoT); інакше — ''."""
    try:
        parts = mp4.resolve().relative_to(ready.resolve()).parts[:-1]
    except ValueError:
        parts = (mp4.parent.name,)
    return next((p for p in parts if not is_auto_folder(p)), "")


class Scheduler:
    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.db = app.db
        self._last_scan = 0
        self._stats_thread: threading.Thread | None = None
        self._health_thread: threading.Thread | None = None
        self._ai_thread: threading.Thread | None = None
        self._health_started = False
        self._awake_since = time.time()
        self._wake_key = ""
        self._last_heal = 0.0
        self._upd_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    def tick(self, now: int | None = None) -> None:
        now = now or now_ts()
        if hasattr(self.app, "reload_config"):
            self.app.reload_config()  # зміни з панелі — без перезапуску
        if now - self._last_scan >= int(self.cfg["scan_interval_seconds"]):
            self._last_scan = now
            self.scan(now)
        self.ensure_slots(now)
        stopped = self.db.get("stopped") == "1"  # «⛔ Стоп публікацій»: ні публікацій, ні нових сповіщень
        if not stopped:
            self.resume_publishing()
            if hasattr(self.app.bot, "resume_bulk"):
                self.app.bot.resume_bulk()
        pending_refresh = self.db.get("refresh_item")
        if pending_refresh:
            self.db.set("refresh_item", None)
            self.app.bot.refresh(int(pending_refresh))
        if not stopped:
            self.process_due_slots(now)
            self.process_notifications(now)
        self.maybe_daily_report(now)
        self.maybe_stats(now)
        self.maybe_health_check(now)
        self.maybe_ai()
        self.maybe_update(now)
        if time.time() - self._last_heal >= 60:  # раз на хвилину: підписи відео, що «застрягли» без зв'язку
            self._last_heal = time.time()
            try:
                self.app.bot.heal()
            except Exception as e:  # noqa: BLE001
                log.debug("heal: %s", e)
        self.maybe_sleep(now)
        try:
            self.app.bot.purge_expired()
        except Exception as e:  # noqa: BLE001
            log.debug("purge: %s", e)

    # ---------- черга ----------
    def scan(self, now: int | None = None) -> list[int]:
        """Додає нові ready/<Гра>/*.mp4 у кінець черги (FIFO).

        Відео з json — якщо статус ready_to_post; відео без json — json створюється автоматично (auto_meta).
        """
        now = now or now_ts()
        ready = Path(self.cfg["paths"]["ready"])
        if not ready.exists():
            ready.mkdir(parents=True, exist_ok=True)
            return []
        self.prune_queue(ready)
        for it in self.db.items("status='queued'"):  # старі записи з «грою» на кшталт battle_02_dmg_5004
            if it["game"] and is_auto_folder(it["game"]):
                self.db.update_item(it["id"], game=game_of(Path(it["mp4"]), ready))
        missing = {r["mp4"]: r["id"] for r in self.db.q("SELECT id, mp4 FROM items WHERE status='missing'")}
        known = self.db.known_paths() - set(missing)
        found = []
        auto = bool((self.cfg.get("auto_meta") or {}).get("enabled", True))
        for mp4 in find_videos(ready, exclude=(self.cfg["paths"]["posted"], self.cfg["paths"]["skipped"],
                                               self.cfg["paths"]["cancelled"])):
            path = str(mp4.resolve())
            if path in known:
                continue
            js = mp4.with_suffix(".json")
            game = game_of(mp4, ready)
            try:
                if time.time() - mp4.stat().st_mtime < 30:
                    continue  # файл ще записується / копіюється
                if not js.exists():
                    if not auto:
                        continue
                    write_json(js, auto_meta_json(mp4, game, self.cfg))
                    log.info("Створено json для %s (назва з імені файлу)", mp4.name)
                    order = mp4.stat().st_mtime
                else:
                    if time.time() - js.stat().st_mtime < 30 and not read_json(js).get("auto_generated"):
                        continue  # json ще записується
                    order = js.stat().st_mtime
                data = read_json(js)
            except (OSError, ValueError) as e:
                log.debug("Пропускаю %s: %s", mp4, e)
                continue
            if json_status(data) != self.cfg["ready_status"]:
                continue
            found.append((order, path, str(js.resolve()), game))
        added = []
        import dupes
        try:
            dupes.backfill(self.db)
        except Exception as e:  # noqa: BLE001
            log.debug("Відбитки старих шортсів: %s", e)
        for _, path, js, game in sorted(found):
            if path in missing:  # файл повернувся (напр. знову вибрали ту саму папку) — назад у чергу
                item_id = missing[path]
                self.db.update_item(item_id, status="queued", qpos=self.db.next_qpos(), note=None,
                                    slot_at=None, skips=0, json=js, game=game)
                added.append(item_id)
                self.db.event(item_id, "queued", {"file": Path(path).name, "again": True})
                continue
            item_id = self.db.add_item(path, js, game)
            if item_id:
                fp = dupes.fingerprint(path)
                self.db.x("UPDATE items SET fp=? WHERE id=?", (fp, item_id))
                dup = dupes.find_duplicate(self.db, path, fp, exclude_id=item_id)
                if dup:  # уже публікувався / уже в черзі — не ставимо, питаємо
                    kind, other = dup
                    self.db.update_item(item_id, status="duplicate", note=f"{kind}:{other['id']}")
                    self.db.event(item_id, "duplicate", {"file": Path(path).name, "kind": kind, "of": other["id"]})
                    log.info("#%s схожий на #%s (%s) — питаю, чи публікувати ще раз: %s", item_id, other["id"], kind, path)
                    if hasattr(self.app.bot, "ask_duplicate"):
                        self.app.bot.ask_duplicate(item_id, other, kind)
                    continue
                added.append(item_id)
                self.db.event(item_id, "queued", {"file": Path(path).name})
                log.info("У чергу додано #%s: %s", item_id, path)
        if added:
            self.announce_queued(added)
        return added

    def prune_queue(self, ready: Path) -> list[dict]:
        """Прибирає з черги відео, файлів яких більше немає або які не з поточної папки з шортсами."""
        ready = ready.resolve()
        removed = []
        for item in self.db.items("status='duplicate'"):  # файл-повтор прибрали вручну — питання вже не актуальне
            if not Path(item["mp4"]).exists():
                self.db.update_item(item["id"], status="cancelled", note="файл видалено")
        for item in self.db.items("status IN ('queued','notified')"):
            mp4 = Path(item["mp4"])
            gone = not mp4.exists()
            moved = not gone and item["status"] == "queued" and ready not in mp4.resolve().parents
            if not (gone or moved):
                continue
            reason = "файл видалено" if gone else "не з поточної папки"
            self.db.update_item(item["id"], status="missing", note=reason)
            self.db.event(item["id"], "missing", {"file": mp4.name, "reason": reason})
            if item["slot_at"]:  # звільняємо слот для наступного відео
                self.db.x("UPDATE slots SET state='pending', item_id=NULL WHERE slot_at=? AND item_id=?",
                          (item["slot_at"], item["id"]))
                self.app.bot.refresh(item["id"])
            removed.append({**item, "reason": reason})
            log.info("#%s прибрано з черги (%s): %s", item["id"], reason, mp4)
        if removed:
            lines = [f"🗑 <b>Прибрано з черги: {len(removed)}</b> — наступні відео зсунулись уперед"]
            for it in removed[:15]:
                lines.append(f"• #{it['id']} {html.escape(Path(it['mp4']).stem)} ({it['reason']})")
            if len(removed) > 15:
                lines.append(f"… і ще {len(removed) - 15}")
            self.app.bot.send("\n".join(lines), html_mode=True, kind="removed")
        return removed

    def announce_queued(self, item_ids: list[int]) -> None:
        plan = {it["id"]: ts for it, ts in self.projection()}
        paused = self.db.get("paused") == "1"
        lines = [f"📥 <b>Нові шортси в черзі: {len(item_ids)}</b>"]
        new = set(item_ids)
        order = [it["id"] for it, _ in self.projection()]
        first_new = min((order.index(i) for i in item_ids if i in order), default=0)
        ahead = [(it, ts) for it, ts in self.projection()[:first_new] if it["id"] not in new]
        if ahead and not paused:
            busy = ", ".join(fmt_when(self.cfg, ts).split(" (")[0] for _, ts in ahead[:6] if ts)
            lines.append(f"Перед ними в черзі ще {len(ahead)} — займають слоти: {busy}"
                         f"{' …' if len(ahead) > 6 else ''}. Змінити порядок — «📋 Черга».")
        for item_id in item_ids[:15]:
            item = self.db.get_item(item_id)
            ts = plan.get(item_id)
            when = fmt_when(self.cfg, ts) if ts else "—"
            lines.append(f"• #{item_id} {html.escape(Path(item['mp4']).stem)} → <b>{when}</b>")
        if len(item_ids) > 15:
            lines.append(f"… і ще {len(item_ids) - 15}")
        lines.append("⏸ Розклад на паузі — час зсунеться після «Продовжити»." if paused else
                     f"За {self.cfg['notify_before_minutes']} хв до часу прийде шортс із кнопкою «🚀 Опублікувати».")
        self.app.bot.send("\n".join(lines), html_mode=True, kind="announce")

    # ---------- розклад наперед ----------
    def future_slots(self, now: int, count: int) -> list[int]:
        """Найближчі вільні слоти (у т.ч. наступних днів і позапланові) — для розкладу черги."""
        if count <= 0:
            return []
        min_lead = int(self.cfg["min_notify_lead_minutes"]) * 60
        rows = self.db.q("SELECT slot_at, state, item_id FROM slots WHERE slot_at>?", (now + min_lead,))
        taken = {r["slot_at"] for r in rows if r["state"] != "pending" or r["item_id"]}
        free = {r["slot_at"] for r in rows if r["state"] == "pending" and not r["item_id"]}
        today = local_dt(self.cfg, now).date()
        for d in range(400):  # до ~1 року наперед
            day = today + timedelta(days=d)
            for ts in self._day_slots(day):
                if ts > now + min_lead and ts not in taken:
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
        slots = self.future_slots(now, len(queue))
        return [(it, slots[i] if i < len(slots) else None) for i, it in enumerate(queue)]

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
                    if r.get("status") != "ok" and p in self.app.platforms and p not in item["disabled"]}
            if item["status"] == "failed" or errs:
                out.append({**item, "errors": errs})
        return out

    def release_slot(self, item_id: int) -> None:
        """Звільняє слот шортса (коли його публікують «зараз»)."""
        item = self.db.get_item(item_id)
        if item and item["slot_at"]:
            self.db.x("UPDATE slots SET state='done' WHERE slot_at=? AND item_id=?", (item["slot_at"], item_id))

    # ---------- слоти ----------
    def _day_slots(self, day) -> list[int]:
        out = []
        for hm in self.cfg["slots"]:
            h, m = map(int, str(hm).split(":"))
            out.append(int(datetime(day.year, day.month, day.day, h, m, tzinfo=self.cfg["tz"]).timestamp()))
        return out

    def ensure_slots(self, now: int) -> None:
        today = local_dt(self.cfg, now).date()
        wanted = set()
        for d in (today, today + timedelta(days=1)):
            wanted.update(self._day_slots(d))
        for ts in wanted:
            self.db.x("INSERT OR IGNORE INTO slots(slot_at, source, state) VALUES (?, 'config', 'pending')", (ts,))
        # Слоти, прибрані з config.json, ще не використані — видаляємо
        for s in self.db.q("SELECT slot_at FROM slots WHERE source='config' AND state='pending' AND slot_at>?", (now,)):
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
            out.append({"slot_at": s["slot_at"], "manual": s["source"] == "manual", "item": item, "kind": kind})
        return out

    def upcoming(self, now: int, limit: int = 6) -> list[dict]:
        return self.db.q("SELECT * FROM slots WHERE slot_at>? AND state IN ('pending','notified') "
                         "ORDER BY slot_at LIMIT ?", (now, limit))

    # ---------- відновлення ----------
    def resume_publishing(self) -> None:
        for item in self.db.items("status='publishing'"):
            if not self.app.publisher.is_active(item["id"]):
                log.info("Відновлюю перервану публікацію #%s", item["id"])
                self.app.publisher.start(item["id"])

    # ---------- настання слоту ----------
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
            self.app.bot.refresh(item["id"])
            self.app.bot.send(f"🗑 Шортс #{item['id']} «{title}» пропущено {skips} рази — "
                              f"файли перенесено в skipped/.")
            log.info("#%s → skipped/", item["id"])
        else:
            self.db.update_item(item["id"], status="queued", skips=skips, qpos=self.db.next_qpos())
            self.app.bot.refresh(item["id"])
            self.db.update_item(item["id"], slot_at=None)
            log.info("#%s пропущено (%s/%s) → у кінець черги", item["id"], skips, self.cfg["max_skips"])

    # ---------- скасування вручну ----------
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

    # ---------- сповіщення за годину ----------
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
            item = self._next_item()
            if not item:
                return
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

    def _next_item(self) -> dict | None:
        for item in self.db.queue():
            if Path(item["mp4"]).exists() and Path(item["json"]).exists():
                return item
            log.warning("#%s: файл зник (%s) — прибираю з черги", item["id"], item["mp4"])
            self.db.update_item(item["id"], status="missing")
            self.db.event(item["id"], "missing", {"file": Path(item["mp4"]).name})
        return None

    # ---------- звіти ----------
    def maybe_daily_report(self, now: int) -> None:
        dt = local_dt(self.cfg, now)
        if dt.strftime("%H:%M") < self.cfg["reports"]["daily_time"]:
            return
        day = dt.strftime("%Y-%m-%d")
        if self.db.get("daily_report_day") == day:
            return
        self.db.set("daily_report_day", day)
        weekly = dt.weekday() == int(self.cfg["reports"].get("weekly_day", 6))

        def work():
            from reports import collect, daily_report, weekly_top
            stats = None
            try:
                stats = collect(self.app)
            except Exception:  # noqa: BLE001
                log.exception("Збір статистики для звіту")
            self.app.bot.send(daily_report(self.app, now, stats), html_mode=True, to="all", kind="daily")
            if weekly and stats:
                self.app.bot.send(weekly_top(self.app, stats), html_mode=True, to="all", kind="weekly")
        if getattr(self.app.publisher, "sync", False):
            work()
        else:
            threading.Thread(target=work, daemon=True, name="daily-report").start()

    def maybe_stats(self, now: int) -> None:
        if self._stats_thread and self._stats_thread.is_alive():
            return
        hours = float(self.cfg["reports"]["stats_after_hours"])
        due = self.db.items("status='posted' AND stats_sent=0 AND posted_at<=?", (now - int(hours * 3600),),
                            order="posted_at")
        if not due:
            return
        from reports import send_stats

        def work():
            for item in due[:5]:
                send_stats(self.app, item)
        if getattr(self.app.publisher, "sync", False):
            work()
        else:
            self._stats_thread = threading.Thread(target=work, daemon=True, name="stats")
            self._stats_thread.start()

    # ---------- тексти від ШІ ----------
    def maybe_ai(self) -> None:
        """Шортсам із текстами «з імені файлу» замовляє тексти в ШІ (у фоні, до сповіщення)."""
        import ai
        if not ai.enabled(self.cfg) or (self._ai_thread and self._ai_thread.is_alive()):
            return
        todo = []
        for item in self.db.items("status IN ('queued','notified')"):
            if int(self.db.get(f"ai_tries:{item['id']}") or 0) >= 3:
                continue
            try:
                data = read_json(item["json"])
            except (OSError, ValueError):
                continue
            if data.get("auto_generated") and not data.get("ai_generated"):
                todo.append(item)
        if not todo:
            return

        def work():
            for item in todo[:5]:
                key = f"ai_tries:{item['id']}"
                self.db.set(key, int(self.db.get(key) or 0) + 1)
                try:
                    if ai.enrich(self.cfg, item):
                        self.db.event(item["id"], "ai_texts")
                        fresh = self.db.get_item(item["id"])
                        if fresh and fresh["status"] in ("notified", "approved"):
                            self.app.bot.refresh(item["id"], texts=True)
                except ai.AIError as e:
                    log.warning("ШІ для #%s: %s", item["id"], e)
                    if "ліміт" in str(e):
                        break
                except Exception:  # noqa: BLE001
                    log.exception("ШІ для #%s", item["id"])
        if getattr(self.app.publisher, "sync", False):
            work()
        else:
            self._ai_thread = threading.Thread(target=work, daemon=True, name="ai")
            self._ai_thread.start()

    # ---------- перевірка токенів ----------
    # ---------- автооновлення ----------
    def maybe_update(self, now: int) -> None:
        up = self.cfg.get("updates") or {}
        if getattr(self.app, "no_update", False):
            return
        if self.db.get("control") == "update":  # «Оновити зараз» з панелі
            self.db.set("control", None)
            self.db.set("update_available", "manual")
        last = int(self.db.get("update_check_ts") or 0)
        every = max(1.0, float(up.get("check_minutes") or 5)) * 60
        if up.get("auto") and now - last >= every and not (self._upd_thread and self._upd_thread.is_alive()):
            self.db.set("update_check_ts", now)

            def work():
                import updater
                try:
                    info = updater.check(self.cfg)
                    self.db.set("update_remote", info["remote"])
                    self.db.set("update_error", None)
                    if info["newer"]:
                        log.info("Є оновлення: %s → %s", info["local"], info["remote"])
                        self.db.set("update_available", info["remote"])
                except Exception as e:  # noqa: BLE001
                    if self.db.get("update_error") != str(e)[:300]:  # раз на 5 хв — не засмічуємо журнал
                        log.warning("Перевірка оновлень: %s", e)
                    self.db.set("update_error", str(e)[:300])
            self._upd_thread = threading.Thread(target=work, daemon=True, name="update-check")
            self._upd_thread.start()
        if self.db.get("update_available") and self.update_idle(now):
            self.install_update()

    def update_idle(self, now: int) -> bool:
        """Оновлюємось лише коли нічого не публікується, не відкрито діалог і ви 5 хв не клацали в боті."""
        if self.app.publisher.active or self.db.items("status='publishing'"):
            return False
        if self.db.q("SELECT key FROM kv WHERE key LIKE 'dialog:%' AND value IS NOT NULL"):
            return False
        if now - int(self.db.get("last_activity") or 0) < 300 and self.db.get("update_available") != "manual":
            return False
        soon = self.db.one("SELECT slot_at FROM slots WHERE slot_at>? AND slot_at<=? AND state IN ('notified','retry')",
                           (now, now + 180))
        return not soon

    def install_update(self) -> None:
        import updater
        manual = self.db.get("update_available") == "manual"
        self.db.set("update_available", None)
        try:
            info = updater.check(self.cfg)
            self.db.set("update_remote", info["remote"])
            if not info["newer"]:
                if manual:
                    self.app.bot.send(f"✅ У вас остання версія ({info['local']}).")
                return
            new = updater.apply(self.cfg)
        except Exception as e:  # noqa: BLE001
            log.error("Оновлення не вдалося: %s", e)
            self.db.set("update_error", str(e)[:300])
            self.app.bot.send(f"⚠️ Автооновлення не вдалося: {e}")
            return
        self.db.set("update_error", None)
        self.db.event(None, "updated", {"version": new})
        self.app.bot.send(f"⬆️ poster оновлено до версії {new} — перезапускаюсь (≈10 с).")
        self.db.set("control", "restart")

    # ---------- сон між слотами ----------
    def wake_times(self, now: int, days: int = 3) -> list[int]:
        """Коли прокидатися: за N хв до кожного слоту (наступні дні), повторів і щоденного звіту."""
        before = int((self.cfg.get("power") or {}).get("wake_before_minutes", 5)) * 60
        out = set()
        today = local_dt(self.cfg, now).date()
        for d in range(days):
            for ts in self._day_slots(today + timedelta(days=d)):
                out.add(ts - before)
            h, m = map(int, self.cfg["reports"]["daily_time"].split(":"))
            day = today + timedelta(days=d)
            out.add(int(datetime(day.year, day.month, day.day, h, m, tzinfo=self.cfg["tz"]).timestamp()) - 60)
        for r in self.db.q("SELECT slot_at FROM slots WHERE slot_at>? AND state IN ('pending','notified','retry')", (now,)):
            out.add(r["slot_at"] - before)
        return sorted(t for t in out if t > now + 60)

    def stream_reason(self, now: int) -> str | None:
        """Режим стріму (кнопка «🎮 Стрім») або запущена програма для стріму / гра."""
        until = int(self.db.get("stream_until") or 0)
        if until > now:
            return f"🎮 режим стріму до {fmt_slot(self.cfg, until)}"
        import power as pw
        apps = {a.lower() for a in (self.cfg.get("power") or {}).get("no_sleep_apps") or []}
        found = sorted(apps & pw.running_processes())
        return f"🎮 запущено {', '.join(found)}" if found else None

    def busy_reason(self, now: int) -> str | None:
        """Чому ПК не можна приспати (None — можна)."""
        power = self.cfg.get("power") or {}
        stream = self.stream_reason(now)
        if stream:
            return stream
        before = int(power.get("wake_before_minutes", 5)) * 60
        if self.db.items("status IN ('notified','publishing')"):
            return "шортс чекає на підтвердження або публікується"
        if self.app.publisher.active:
            return "триває публікація"
        soon = self.db.one("SELECT slot_at FROM slots WHERE slot_at>? AND slot_at<=? AND state IN ('pending','notified','retry')",
                           (now - 60, now + before + 600))
        if soon:
            return "скоро слот"
        h, m = map(int, self.cfg["reports"]["daily_time"].split(":"))
        dt = local_dt(self.cfg, now)
        rep = int(dt.replace(hour=h, minute=m, second=0, microsecond=0).timestamp())
        if rep - 600 <= now and self.db.get("daily_report_day") != dt.strftime("%Y-%m-%d"):
            return "скоро щоденний звіт"
        for t in (self._stats_thread, self._health_thread, self._ai_thread):
            if t and t.is_alive():
                return "збирається статистика / перевірка"
        if self.db.q("SELECT key FROM kv WHERE key LIKE 'dialog:%' AND value IS NOT NULL"):
            return "відкрито діалог у Telegram"
        if self.db.q("SELECT key FROM kv WHERE key LIKE 'pcmd:%'"):
            return "виконується дія з панелі"
        last = int(self.db.get("last_activity") or 0)
        quiet = int(power.get("quiet_minutes", 5)) * 60
        if now - last < quiet:
            return f"чекаю {int(power.get('quiet_minutes', 5))} хв тиші після вашої дії / публікації"
        return None

    def maybe_sleep(self, now: int) -> None:
        power = self.cfg.get("power") or {}
        import power as pw
        if not power.get("sleep_between_slots") or not pw.WIN:
            return
        times = self.wake_times(now)
        key = ",".join(map(str, times[:20]))
        if key != self._wake_key:  # розклад пробуджень змінився — оновлюємо завдання Windows
            try:
                pw.set_wake_times(times)
                self._wake_key = key
                self.db.set("power_next_wake", times[0] if times else None)
            except Exception as e:  # noqa: BLE001
                log.error("Не вдалося записати пробудження: %s", e)
                self.db.set("power_state", f"помилка пробудження: {str(e)[:200]}")
                return
        reason = self.busy_reason(now)
        pw.keep_awake(bool(reason))  # зайнятий — Windows не засне сама
        if reason:
            self.db.set("power_state", reason)
            return
        # Приспати самі — лише якщо ПК розбудили ми і відтоді ним ніхто не користувався.
        # Якщо ви за комп'ютером (чи дивитесь відео) — засинає за звичайними налаштуваннями Windows.
        awake = time.time() - self._awake_since
        untouched = pw.idle_seconds() >= awake - 5
        if not untouched or awake < 180:
            self.db.set("power_state", "засне за налаштуваннями Windows" if not untouched else "щойно прокинувся")
            return
        self.db.set("power_state", "засинаю")
        nxt = fmt_slot(self.cfg, times[0]) if times else "—"
        log.info("💤 Засинаю. Наступне пробудження: %s", nxt)
        self.db.set("power_next_wake", times[0] if times else None)
        self.db.event(None, "sleep", {"wake": times[0] if times else None})
        pw.sleep_now()
        self._awake_since = time.time()  # сюди повертаємось після пробудження
        log.info("☀️ Прокинувся")
        self.db.set("power_state", "щойно прокинувся")

    def maybe_health_check(self, now: int) -> None:
        """Раз на день (і при запуску) перевіряє доступ до платформ, щоб про протерміновані
        токени дізнатися до слоту, а не під час публікації."""
        if self.cfg.get("dry_run") or (self._health_thread and self._health_thread.is_alive()):
            return
        dt = local_dt(self.cfg, now)
        day = dt.strftime("%Y-%m-%d")
        if self._health_started:
            if dt.strftime("%H:%M") < self.cfg["reports"]["health_check_time"] or self.db.get("health_day") == day:
                return
            self.db.set("health_day", day)
        self._health_started = True

        if getattr(self.app.publisher, "sync", False):
            self.health_check()
        else:
            self._health_thread = threading.Thread(target=self.health_check, daemon=True, name="health")
            self._health_thread.start()

    def health_check(self) -> dict[str, str]:
        from platforms import TokenExpired

        out = {}
        for key, plat in self.app.platforms.items():
            try:
                info = plat.check()
                out[key] = f"✅ {info}"
                self.app.bot.resolve(f"reauth_{key}")  # доступ є — повідомлення «потрібна авторизація» прибираємо
                warn = plat.token_warning()
                if warn:
                    out[key] = f"⚠️ {warn}"
                    if self.db.get(f"warn_{key}") != local_dt(self.cfg, now_ts()).strftime("%Y-%m-%d"):
                        self.db.set(f"warn_{key}", local_dt(self.cfg, now_ts()).strftime("%Y-%m-%d"))
                        self.app.bot.send(warn)
            except TokenExpired as e:
                out[key] = f"🔑 потрібна авторизація: {e}"
                self.app.bot.notify_reauth(key, str(e))
            except Exception as e:  # noqa: BLE001 — мережа тощо: не тривожимо, лише журнал
                out[key] = f"❓ не вдалося перевірити: {str(e)[:150]}"
                log.warning("Перевірка %s: %s", key, e)
            self.db.set(f"health_{key}", out[key])
        self.db.set("health_ts", now_ts())
        log.info("Перевірка доступів: %s", out)
        return out

    def _title(self, item: dict) -> str:
        try:
            return load_meta(item["json"], item["title_override"]).yt_title
        except Exception:  # noqa: BLE001
            return Path(item["mp4"]).stem
