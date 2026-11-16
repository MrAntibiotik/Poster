"""Публікація на всі платформи: незалежно, з повторними спробами, архівування, запис у json."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from app.core import media
from app.core.config import PLATFORMS, local_dt, now_ts
from app.core.meta import apply_always_hashtags, highlight_description, load_meta, move_pair, read_json, write_json
from app.platforms import RateLimited, Stopped, TokenExpired

log = logging.getLogger("publisher")


class Publisher:
    def __init__(self, app):
        self.app = app
        self.active: set[int] = set()      # у роботі: публікується зараз або чекає в черзі
        self.waiting: list[int] = []        # черга публікацій — строго по одному шортсу
        self.running: int | None = None
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()
        self.sync = False  # для тестів: публікувати в поточному потоці
        self.sleep = time.sleep

    def is_active(self, item_id: int) -> bool:
        with self._lock:
            return item_id in self.active

    def hold(self, item_id: int) -> str:
        """«⏸ Пауза» для одного шортса: зупинити його (якщо йде) або прибрати з черги — далі йде наступний."""
        self.app.db.set(f"held:{item_id}", 1)
        with self._lock:
            if item_id in self.waiting:
                self.waiting.remove(item_id)
                self.active.discard(item_id)
                return "queued"
            return "running" if self.running == item_id else "idle"

    def release(self, item_id: int) -> None:
        """«▶️ Продовжити»: шортс знову стає в чергу публікацій (в кінець)."""
        self.app.db.set(f"held:{item_id}", None)
        self.start(item_id)

    def position(self, item_id: int) -> int | None:
        """0 — публікується зараз; 1, 2… — місце в черзі публікацій; None — не в роботі."""
        with self._lock:
            if self.running == item_id:
                return 0
            return self.waiting.index(item_id) + 1 if item_id in self.waiting else None

    def start(self, item_id: int) -> None:
        if self.app.db.get("stopped") == "1":  # «⛔ Стоп публікацій» — не починаємо нічого
            log.info("#%s: публікації зупинено — не публікую", item_id)
            if getattr(self.app.bot, "send", None) and self.app.db.get(f"stop_told:{item_id}") != "1":
                self.app.db.set(f"stop_told:{item_id}", "1")
                self.app.bot.send(f"⛔ Публікації зупинено — #{item_id} не публікую. «✅ Відновити публікації», "
                                  "щоб продовжити.")
            return
        self.app.db.set(f"stop_told:{item_id}", None)
        if self.app.db.get(f"held:{item_id}"):
            log.info("#%s на паузі — не публікую, доки не натиснуть «▶️ Продовжити»", item_id)
            return
        with self._lock:
            if item_id in self.active:
                return
            self.active.add(item_id)
            self.waiting.append(item_id)
            ahead = len(self.waiting) - 1 + (1 if self.running is not None else 0)
        self.app.db.update_item(item_id, status="publishing")
        if ahead:
            log.info("#%s: у черзі публікацій (%s-й) — шортси публікуються по одному", item_id, ahead + 1)
            if hasattr(self.app.bot, "refresh"):
                self.app.bot.refresh(item_id)
        if self.sync:
            self._drain()
            return
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._drain, daemon=True, name="publisher")
            self._worker.start()

    def _drain(self) -> None:
        """Єдиний потік публікацій: бере шортси з черги по одному."""
        while True:
            with self._lock:
                if not self.waiting:
                    self._worker = None if not self.sync else self._worker
                    return
                if self.app.db.get("stopped") == "1":   # «⛔ Стоп»: решта черги чекає «✅ Відновити»
                    for i in self.waiting:
                        self.active.discard(i)
                    left, self.waiting = list(self.waiting), []
                else:
                    left = None
                    item_id = self.waiting.pop(0)
                    self.running = item_id
            if left is not None:
                for i in left:
                    if hasattr(self.app.bot, "refresh"):
                        self.app.bot.refresh(i)
                return
            for i in list(self.waiting):     # у решти змінилось місце в черзі
                if hasattr(self.app.bot, "refresh"):
                    try:
                        self.app.bot.refresh(i)
                    except Exception:  # noqa: BLE001
                        pass
            self._run_safe(item_id)

    def _run_safe(self, item_id: int) -> None:
        try:
            self.run(item_id)
        except Exception as e:  # noqa: BLE001
            log.exception("Публікація #%s аварійно завершилась", item_id)
            self.app.db.update_item(item_id, status="failed", note=str(e)[:300])
            self.app.bot.send(f"❌ Публікація #{item_id} аварійно завершилась: {e}")
        finally:
            with self._lock:
                self.active.discard(item_id)
                if self.running == item_id:
                    self.running = None

    # ------------------------------------------------------------------
    def targets(self, item: dict) -> list[str]:
        return [p for p in PLATFORMS if p in self.app.platforms and p not in item["disabled"]]

    def run(self, item_id: int) -> None:
        app, db = self.app, self.app.db
        item = db.get_item(item_id)
        meta = apply_always_hashtags(load_meta(item["json"], item["title_override"]), app.cfg)
        if item.get("kind") == "highlight":
            meta.kind = "highlight"
            raw = load_meta(item["json"], item["title_override"]).yt_description
            meta.yt_description = highlight_description(meta, app.cfg, raw)   # опис каналу + усі хештеги
            thumb = db.get(f"thumb:{item_id}")
            meta.thumbnail = thumb if thumb and Path(thumb).exists() else ""
        results = dict(item["results"])
        log.info("Публікую #%s «%s» (%s)", item_id, meta.yt_title, item["mp4"])
        app.bot.refresh(item_id)

        targets = self.targets(item)
        for key in targets:
            if results.get(key, {}).get("status") == "ok":
                continue  # уже опубліковано (відновлення після збою/перезапуску)
            if db.get("stopped") == "1":  # «⛔ Стоп» під час публікації: решту платформ — після відновлення
                log.info("#%s: публікації зупинено — %s та далі після «Відновити»", item_id, key)
                app.bot.refresh(item_id)
                return
            db.set(f"pubnow:{item_id}", key)  # яка платформа зараз — для підпису відео
            res = self._publish_one(key, item, meta)
            db.set(f"pubnow:{item_id}", None)
            if res is None:   # «⛔ Стоп» посеред публікації — ця й наступні платформи після «Відновити»
                log.info("#%s: публікацію зупинено на %s — продовжиться після «Відновити»", item_id, key)
                with self._lock:
                    self.active.discard(item_id)
                app.bot.refresh(item_id)
                return
            results[key] = res
            db.update_item(item_id, results=results)
            app.bot.refresh(item_id)  # у підписі відео видно, куди вже вийшло

        ok = [k for k in targets if results.get(k, {}).get("status") == "ok"]
        failed = [k for k in targets if k not in ok]
        now = now_ts()
        item = db.get_item(item_id)
        if ok:
            posted_root = Path(app.cfg["paths"]["posted"])
            if posted_root in Path(item["mp4"]).parents:  # повтор (/retry) уже заархівованого
                new_mp4, new_json = item["mp4"], item["json"]
            else:
                day = local_dt(app.cfg, now).strftime("%Y-%m-%d")
                try:
                    new_mp4, new_json = move_pair(item["mp4"], item["json"], posted_root / day)
                except OSError as e:  # файл відкритий у плеєрі тощо — публікація все одно вдалася
                    log.error("#%s: не вдалося перенести файли в posted/: %s", item_id, e)
                    app.bot.send(f"⚠️ #{item_id} опубліковано, але файли не вдалося перенести в posted/ "
                                 f"({e}). Перенесіть вручну.")
                    new_mp4, new_json = item["mp4"], item["json"]
            media.drop_previews(item["mp4"], app.cfg)
            db.update_item(item_id, mp4=new_mp4, json=new_json, status="posted",
                           posted_at=item["posted_at"] or now)
            self._write_json(new_json, item, results, "posted", now)
            db.event(item_id, "published", {"title": meta.yt_title, "ok": ok, "failed": failed,
                                            "urls": {k: results[k].get("url") for k in ok}})
        else:
            db.update_item(item_id, status="failed")
            self._write_json(item["json"], item, results, "post_failed", now)
            db.event(item_id, "publish_failed", {"title": meta.yt_title, "failed": failed})
        db.set(f"ignored:{item_id}", None)
        app.bot.refresh(item_id)
        db.set("last_activity", now_ts())  # після публікації ПК ще кілька хвилин не засне
        if ok and hasattr(app.bot, "drop_all_texts"):
            app.bot.drop_all_texts(item_id)  # назва й хештеги вже в підписі відео — окреме повідомлення не потрібне
        if hasattr(app.bot, "finish_item_msgs"):
            app.bot.finish_item_msgs(item_id)  # «🔁 Повторюю…» → прибрати / стати результатом
        if not failed and hasattr(app.bot, "drop_failed_card"):
            app.bot.drop_failed_card(item_id)  # усе вийшло — окреме повідомлення про невдачу більше не потрібне
        single_ig = (app.cfg["platforms"].get("instagram") or {}).get("single_attempt", True)
        limited = [results[k]["retry_at"] for k in failed if results.get(k, {}).get("retry_at")
                   and not (k == "instagram" and single_ig)]   # діагностичний режим: Instagram без автоповторів
        if limited:
            self._retry_after_limit(item_id, max(limited) + 120)
        if failed and hasattr(app.bot, "notify_failed"):
            app.bot.notify_failed(item_id)
        log.info("#%s: успішно %s, помилки %s", item_id, ok, failed)

    def _publish_one(self, key: str, item: dict, meta) -> dict:
        app = self.app
        plat = app.platforms[key]
        attempts = max(1, int(app.cfg["retry"]["attempts"]))
        if key == "instagram" and (app.cfg["platforms"].get("instagram") or {}).get("single_attempt", True):
            attempts = 1   # діагностичний режим Instagram: без повторних завантажень
        pause = int(app.cfg["retry"]["pause_seconds"])
        last = None
        try:
            plat.should_stop = lambda _i=item["id"]: app.db.get("stopped") == "1" or bool(app.db.get(f"held:{_i}"))
            plat.report = lambda phase, _i=item["id"]: self._report(_i, phase)
        except AttributeError:
            pass
        self._report(item["id"], "завантажую")
        for i in range(1, attempts + 1):
            if app.db.get("stopped") == "1" or app.db.get(f"held:{item['id']}"):
                return None
            try:
                if app.cfg.get("dry_run"):
                    r = {"id": f"dry-{key}-{item['id']}", "url": f"https://example.com/{key}/{item['id']}",
                         "note": "dry_run: реальної публікації не було"}
                else:
                    if not Path(item["mp4"]).is_file():   # файл перенесли? шукаємо за назвою, перш ніж здаватися
                        new = None
                        try:
                            from app.channels import cutter_link
                            new = cutter_link.locate(item["mp4"], cutter_link.build_index(app))
                        except Exception:  # noqa: BLE001
                            pass
                        if not new:
                            raise RuntimeError(f"файлу немає на місці: {item['mp4']}")
                        log.info("#%s: файл перенесено — беру з нового місця %s", item["id"], new)
                        app.db.update_item(item["id"], mp4=str(new), json=str(new.with_suffix(".json")))
                        item = {**item, "mp4": str(new), "json": str(new.with_suffix(".json"))}
                    previews = Path(app.cfg["paths"]["data"]) / "previews"
                    if previews in Path(item["mp4"]).parents:
                        raise RuntimeError("спроба опублікувати стиснуту прев'ю-копію замість оригіналу — зупинено")
                    if i == 1:
                        log.info("#%s %s: завантажую оригінал без змін: %s (%.1f МБ)", item["id"], key,
                                 item["mp4"], Path(item["mp4"]).stat().st_size / 1024 / 1024)
                    r = plat.publish(item["mp4"], meta)
                log.info("#%s %s: опубліковано %s", item["id"], key, r.get("url"))
                if hasattr(app.bot, "resolve"):
                    app.bot.resolve(f"reauth_{key}")
                return {"status": "ok", "attempts": i, "published_at": now_ts(), **r}
            except Stopped:
                app.db.set(f"pubtry:{item['id']}:{key}", None)
                return None
            except RateLimited as e:
                # повтори одразу лише подовжують блокування — публікація піде сама після паузи
                log.warning("#%s %s: %s", item["id"], key, e)
                app.db.set(f"pubtry:{item['id']}:{key}", None)
                app.db.event(item["id"], "platform_error", {"platform": key, "error": str(e)})
                return {"status": "error", "attempts": i, "error": str(e)[:500], "retry_at": e.retry_at}
            except TokenExpired as e:
                log.error("#%s %s: потрібна авторизація: %s", item["id"], key, e)
                app.bot.notify_reauth(key, str(e))
                app.db.event(item["id"], "platform_error", {"platform": key, "error": f"авторизація: {e}"})
                return {"status": "error", "reauth": True, "attempts": i,
                        "error": f"потрібна повторна авторизація: {e}"[:500]}
            except Exception as e:  # noqa: BLE001
                last = e
                log.warning("#%s %s: спроба %d/%d невдала: %s", item["id"], key, i, attempts, e,
                            exc_info=not isinstance(e, (OSError,)) and log.isEnabledFor(logging.DEBUG))
                if "не прийняв відео" in str(e):
                    break  # Instagram уже перепробував усі варіанти файлу — повторювати все ще раз марно
                if i < attempts:
                    app.db.set(f"pubtry:{item['id']}:{key}", f"{i + 1}/{attempts}")
                    self.sleep(pause)
        app.db.set(f"pubtry:{item['id']}:{key}", None)
        app.db.event(item["id"], "platform_error", {"platform": key, "error": str(last)[:2000]})
        return {"status": "error", "attempts": attempts, "error": str(last)[:2000]}

    def _report(self, item_id: int, phase: str) -> None:
        """Етап публікації для картки: що робимо, з якого часу, і коли востаннє було «живе» підтвердження."""
        import json as _json
        now = now_ts()
        try:
            old = _json.loads(self.app.db.get(f"pubphase:{item_id}") or "{}")
        except ValueError:
            old = {}
        since = old.get("since") if old.get("phase") == phase else now
        self.app.db.set(f"pubphase:{item_id}", _json.dumps({"phase": phase, "since": since, "beat": now},
                                                           ensure_ascii=False))
        if old.get("phase") != phase and hasattr(self.app.bot, "refresh"):
            try:
                self.app.bot.refresh(item_id)
            except Exception:  # noqa: BLE001
                pass

    def _retry_after_limit(self, item_id: int, ts: int) -> None:
        """Платформа обмежила запити — повтор невдалих платформ ставимо на час після паузи (сам, без кнопок)."""
        sched = getattr(self.app, "scheduler", None)
        if not sched or not hasattr(sched, "reserve_retry"):
            return
        ts = max(ts, now_ts() + 300)
        # кілька повторів після ліміту — з інтервалом 15 хв, щоб разом знову не вичерпати квоту
        last = self.app.db.one("SELECT MAX(slot_at) AS t FROM slots WHERE state='retry' AND item_id<>? AND slot_at>?",
                               (item_id, now_ts()))
        if last and last["t"] and last["t"] + 900 > ts:
            ts = last["t"] + 900
        ts = ts // 60 * 60 + 60
        for _ in range(120):
            try:
                sched.reserve_retry(item_id, ts)
                log.info("#%s: повтор після ліміту запитів — %s", item_id, local_dt(self.app.cfg, ts).strftime("%H:%M"))
                return
            except ValueError:
                ts += 60
        log.warning("#%s: не вдалося запланувати повтор після ліміту", item_id)

    def _write_json(self, path: str, item: dict, results: dict, status: str, now: int) -> None:
        try:
            data = read_json(path)
        except Exception as e:  # noqa: BLE001
            log.warning("Не вдалося прочитати %s: %s", path, e)
            return
        data["status"] = status
        data["posting"] = {
            "posted_at": local_dt(self.app.cfg, now).isoformat(timespec="seconds"),
            "slot": local_dt(self.app.cfg, item["slot_at"]).isoformat(timespec="minutes") if item["slot_at"] else None,
            "title_used": item["title_override"] or None,
            "platforms": {
                k: {**v, "published_at": local_dt(self.app.cfg, v["published_at"]).isoformat(timespec="seconds")}
                if v.get("published_at") else v
                for k, v in results.items()
            },
        }
        for k in item["disabled"]:
            data["posting"]["platforms"].setdefault(k, {"status": "disabled"})
        write_json(path, data)
