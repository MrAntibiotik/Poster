"""Пошук нових шортсів у папках, хайлайти, чужі канали, прибирання черги."""
from __future__ import annotations

import html
import time
from app.core.config import fmt_when
from app.core.config import highlights_on
from app.core.config import now_ts
from app.core.meta import auto_meta_json
from app.core.meta import highlight_meta_json
from app.core.meta import json_status
from app.core.meta import read_json
from app.core.meta import write_json
from pathlib import Path
from .common import (
    find_videos,
    game_of,
    is_auto_folder,
    log,
)


class ScanMixin:
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
            if self.foreign_channel(mp4, ready):
                self._why(mp4, f"належить іншому каналу ({self.foreign_channel(mp4, ready)})")
                continue  # шортс іншого каналу (напр. реплеї для EN) — не наш контент
            try:
                if time.time() - mp4.stat().st_mtime < 30:
                    continue  # файл ще записується / копіюється
                if not js.exists():
                    if not auto:
                        self._why(mp4, "немає json, а автоназви вимкнено")
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
                self._why(mp4, f"статус у json «{json_status(data)}», а потрібен «{self.cfg['ready_status']}»")
                continue
            if self.foreign_channel(mp4, ready, data):
                self._why(mp4, f"у json вказано інший канал ({self.foreign_channel(mp4, ready, data)})")
                continue
            found.append((order, path, str(js.resolve()), game, "short"))
        found += self._scan_highlights(known)
        added = []
        from app.core import dupes
        try:
            dupes.backfill(self.db)
        except Exception as e:  # noqa: BLE001
            log.debug("Відбитки старих шортсів: %s", e)
        for _, path, js, game, kind in sorted(found):
            if path in missing:  # файл повернувся (напр. знову вибрали ту саму папку) — назад у чергу
                item_id = missing[path]
                self.db.update_item(item_id, status="queued", qpos=self.db.next_qpos(), note=None,
                                    slot_at=None, skips=0, json=js, game=game)
                added.append(item_id)
                self.db.event(item_id, "queued", {"file": Path(path).name, "again": True})
                continue
            disabled = [p for p in ("tiktok", "instagram", "facebook")] if kind == "highlight" else None
            item_id = self.db.add_item(path, js, game, kind=kind, disabled=disabled)
            if item_id:
                fp = dupes.fingerprint(path)
                self.db.x("UPDATE items SET fp=? WHERE id=?", (fp, item_id))
                dup = dupes.find_duplicate(self.db, path, fp, exclude_id=item_id)
                if dup:  # уже публікувався / уже в черзі — не ставимо, питаємо
                    dup_kind, other = dup
                    self.db.update_item(item_id, status="duplicate", note=f"{dup_kind}:{other['id']}")
                    self.db.event(item_id, "duplicate", {"file": Path(path).name, "kind": dup_kind, "of": other["id"]})
                    log.info("#%s схожий на #%s (%s) — питаю, чи публікувати ще раз: %s", item_id, other["id"],
                             dup_kind, path)
                    if hasattr(self.app.bot, "ask_duplicate"):
                        self.app.bot.ask_duplicate(item_id, other, dup_kind)
                    continue
                added.append(item_id)
                self.db.event(item_id, "queued", {"file": Path(path).name})
                log.info("У чергу додано #%s: %s", item_id, path)
        if added:
            if (self.cfg.get("telegram") or {}).get("queue_cards", True) and hasattr(self.app.bot, "queue_cards"):
                today = set(self.app.bot.today_queue_ids())
                cards = [i for i in added if i in today]  # картки з відео — лише на сьогодні
                self.app.bot.queue_cards(cards)
                rest = [i for i in added if i not in today]
                if rest:
                    self.announce_queued(rest)  # решта — одним списком із часом публікації
            else:
                self.announce_queued(added)
        return added

    def _scan_highlights(self, known: set) -> list[tuple]:
        """Хайлайти з окремої папки (Робочий стіл\\Хайлайти): json від shorts-autocut або створений тут."""
        if not highlights_on(self.cfg):
            for it in self.db.items("status IN ('queued','notified','approved') AND kind='highlight'"):
                # хайлайти тут не публікуються (напр. EN-канал) — прибираємо з черги; файл не чіпаємо
                self.db.update_item(it["id"], status="missing", note="хайлайти публікує лише основний канал")
                if it["slot_at"]:
                    self.db.x("UPDATE slots SET state='pending', item_id=NULL WHERE slot_at=? AND item_id=?",
                              (it["slot_at"], it["id"]))
                if hasattr(self.app.bot, "_delete"):
                    for r in self.db.tg_msgs(it["id"]):
                        self.app.bot._delete(r["chat_id"], [r["msg_id"]])
                log.info("#%s: хайлайт прибрано з черги цього каналу", it["id"])
            return []
        folder = Path(self.cfg["highlights"]["folder_path"])
        if not folder.is_dir():
            return []
        out = []
        for mp4 in find_videos(folder, depth=2):
            path = str(mp4.resolve())
            if path in known:
                continue
            js = mp4.with_suffix(".json")
            try:
                if time.time() - mp4.stat().st_mtime < 30:
                    continue  # ще рендериться / копіюється
                if not js.exists():
                    write_json(js, highlight_meta_json(mp4, self.cfg))
                    log.info("Створено json для хайлайту %s", mp4.name)
                elif time.time() - js.stat().st_mtime < 30:
                    continue
                data = read_json(js)
            except (OSError, ValueError) as e:
                log.debug("Пропускаю хайлайт %s: %s", mp4, e)
                continue
            if json_status(data) != self.cfg["ready_status"]:
                continue
            out.append((mp4.stat().st_mtime, path, str(js.resolve()), "WoT", "highlight"))
        return out

    def foreign_channel(self, mp4: Path, ready: Path, data: dict | None = None) -> str:
        """Ключ іншого каналу, якому належить відео, або "" (наше). Різний контент не змішується:
        • json із "channel" (або "source": "replay" — шортси з реплеїв для EN-каналу);
        • файл-мітка .poster_channel у папці відео чи вище (до папки готових шортсів) —
          напр. output_replays_en програми нарізки, де шортси з реплеїв лежать ще без json."""
        mine = self.channel_key()
        if data is not None:
            ch = str(data.get("channel") or ("en" if data.get("source") == "replay" else ""))
            if ch and ch != mine:
                return ch
        try:
            stop = ready.resolve()
            for d in Path(mp4).resolve().parents:
                mark = d / ".poster_channel"
                if mark.is_file():
                    ch = mark.read_text(encoding="utf-8").strip()
                    if ch and ch != mine:
                        return ch
                if d == stop:
                    break
        except OSError:
            pass
        return ""

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
            home = self._home(item, ready)
            moved = not gone and item["status"] == "queued" and home not in mp4.resolve().parents
            try:
                data = read_json(item["json"]) if not gone else None
            except (OSError, ValueError):
                data = None
            foreign = "" if gone else self.foreign_channel(mp4, home, data)
            if not (gone or moved or foreign):
                continue
            reason = ("файл видалено" if gone else
                      f"контент іншого каналу ({foreign.upper()}) — не для цього" if foreign else "не з поточної папки")
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
