"""Публікація на всі платформи: незалежно, з повторними спробами, архівування, запис у json."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from app.core import media
from app.core.config import PLATFORMS, local_dt, now_ts
from app.core.meta import apply_always_hashtags, highlight_description, load_meta, move_pair, read_json, write_json
from app.platforms import ContainerFailed, PublishError, RateLimited, StillProcessing, Stopped, TokenExpired

log = logging.getLogger("publisher")

# Instagram ще не опубліковано, але це не помилка: надіслано вам / пропущено / Instagram обробляє у фоні /
# чекає, поки Instagram закінчить попереднє відео. Такі платформи не повторюються і не рахуються невдалими.
PENDING = ("manual", "skipped", "processing", "ig_wait", "ig_next")
DONE = tuple(x for x in ("ok",) + PENDING if x != "ig_next")   # ig_next — Instagram ще треба завантажити (інший спосіб)
IG_PENDING_SQL = ("status IN ('posted','publishing','failed') AND "
                  "(results LIKE '%\"processing\"%' OR results LIKE '%\"ig_wait\"%' OR results LIKE '%\"ig_next\"%')")
# якщо процес перезапустився посеред першого очікування (до 10 хв) — фон підхопить той самий контейнер після цього
INLINE_GRACE = 12 * 60
IG_BUSY = ("processing", "ig_next")   # Instagram зайнятий цим шортсом — інші чекають (строго одне відео за раз)


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
            if results.get(key, {}).get("status") in DONE:
                continue  # уже опубліковано / надіслано вам для ручної публікації (відновлення після збою/перезапуску)
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
        # «manual» — Instagram надіслано вам у Telegram (публікуєте з телефона): не помилка; «skipped» — ви пропустили
        waiting = [k for k in targets if results.get(k, {}).get("status") in PENDING]
        failed = [k for k in targets if k not in ok and k not in waiting]
        now = now_ts()
        item = db.get_item(item_id)
        if ok or waiting:
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
        """Публікація на одну платформу + запис спроби в історію публікацій (для аналізу)."""
        started = now_ts()
        plat = self.app.platforms.get(key)
        if plat is not None:
            try:
                plat.diag = None          # деталі Instagram — лише цієї спроби
            except AttributeError:
                pass
        res = self._publish_one_raw(key, item, meta)
        if res is not None and res.get("status") != "ig_wait":
            st = res.get("status")
            note = str(res.get("note") or "")
            kind = {"ok": "ok", "processing": "processing"}.get(st)
            if st == "manual":
                kind = "manual_long" if "довше" in note else "manual_rejected" if "не прийняв" in note else "manual"
            elif st == "error":
                kind = "auth" if res.get("reauth") else "limited" if res.get("retry_at") else "error"
            from app.insights import history
            extra = history.ig_details(plat) if key == "instagram" else {}
            history.record(self.app, item, key, kind or str(st), started, url=res.get("url") or None,
                           error=res.get("error"), method=res.get("method_name") or extra.pop("method", None),
                           **extra)
        return res

    def _publish_one_raw(self, key: str, item: dict, meta) -> dict:
        app = self.app
        igc = app.cfg["platforms"].get("instagram") or {}
        # (старе внутрішнє обмеження «довші за 55 с — лише вручну» прибрано: Reels API приймає 3 с – 15 хв)
        if key == "instagram" and igc.get("manual") \
                and hasattr(app.bot, "instagram_manual") and not app.cfg.get("dry_run"):
            try:   # напівавтомат (увімкнений вами): відео й підпис — вам у Telegram, публікуєте з телефона
                app.bot.instagram_manual(item["id"], meta)
                return {"status": "manual", "sent_at": now_ts(), "note": "надіслано в Telegram для публікації з телефона"}
            except Exception as e:  # noqa: BLE001
                log.warning("#%s Instagram вручну: не вдалося надіслати в Telegram: %s", item["id"], e)
                return {"status": "error", "attempts": 1, "error": f"не вдалося надіслати відео в Telegram: {e}"[:500]}
        if key == "instagram" and not app.cfg.get("dry_run"):
            busy = self.ig_processing(exclude=item["id"])
            if busy:   # в Instagram — строго одне відео за раз: чекаємо, поки він обробить попереднє
                log.info("#%s instagram: чекає, поки Instagram обробить #%s", item["id"], busy)
                return {"status": "ig_wait", "after": busy, "since": now_ts(),
                        "note": f"чекає, поки Instagram обробить #{busy}"}
        plat = app.platforms[key]
        attempts = max(1, int(app.cfg["retry"]["attempts"]))
        if key == "instagram" and (app.cfg["platforms"].get("instagram") or {}).get("single_attempt", True):
            attempts = 1   # діагностичний режим Instagram: без повторних завантажень
        pause = int(app.cfg["retry"]["pause_seconds"])
        last = None
        try:
            plat.current_item = item["id"]       # для журналу контейнерів Instagram
            plat.should_stop = lambda _i=item["id"]: app.db.get("stopped") == "1" or bool(app.db.get(f"held:{_i}"))
            plat.report = lambda phase, _i=item["id"]: self._report(_i, phase)
        except AttributeError:
            pass
        self._report(item["id"], "завантажую")
        if key == "instagram" and hasattr(plat, "upload_only") and not app.cfg.get("dry_run"):
            if app.db.get("stopped") == "1" or app.db.get(f"held:{item['id']}"):
                return None
            try:
                item = self._ensure_file(item)
            except RuntimeError as e:
                app.db.event(item["id"], "platform_error", {"platform": key, "error": str(e)})
                return {"status": "error", "attempts": 1, "error": str(e)[:500]}
            return self._publish_instagram(item, meta, plat)
        for i in range(1, attempts + 1):
            if app.db.get("stopped") == "1" or app.db.get(f"held:{item['id']}"):
                return None
            try:
                if app.cfg.get("dry_run"):
                    r = {"id": f"dry-{key}-{item['id']}", "url": f"https://example.com/{key}/{item['id']}",
                         "note": "dry_run: реальної публікації не було"}
                else:
                    item = self._ensure_file(item)
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
        if key == "instagram" and self._ig_to_phone(item, meta, str(last)):
            return {"status": "manual", "sent_at": now_ts(), "error": str(last)[:500],
                    "note": "Instagram через API не прийняв — надіслано вам у Telegram"}
        return {"status": "error", "attempts": attempts, "error": str(last)[:2000]}

    def _hist(self, it: dict, plat, result: str, since=None, **kw) -> None:
        """Запис фонової події Instagram в історію — зі станом контейнера (status_code / status) і етапом —
        і підсумок у журнал контейнерів."""
        from app.insights import containers, history
        cd = history.container_details(plat)
        history.record(self.app, it, "instagram", result, since, **{**cd, **kw})
        r = (it.get("results") or {}).get("instagram") or {}
        rec = dict(r.get("ig_report") or {})
        rec.update({"container": cd.get("container_id") or r.get("container") or rec.get("container"),
                    "item_id": it["id"],
                    "status_code": cd.get("container_status") or rec.get("status_code"),
                    "status": cd.get("container_status_text") or rec.get("status"),
                    "processing_s": kw.get("processing_s") or rec.get("processing_s"),
                    "outcome": {"ok_background": "опубліковано (після довгої обробки)", "stuck": "контейнер завис",
                                "next_method": "не оброблено — наступний спосіб", "error": "помилка"}.get(result, result)})
        rec["error"] = containers.error_message(None, rec.get("status_code"), rec.get("status"),
                                                kw.get("error")) or rec.get("error")
        if rec.get("container"):
            try:
                containers.report(self.app.cfg, rec)
            except Exception as e:  # noqa: BLE001
                log.debug("журнал контейнера: %s", e)

    def _ig_to_phone(self, item: dict, meta, why: str) -> bool:
        """Instagram через API не прийняв — одразу повне відео вам у Telegram (platforms.instagram.fallback_manual)."""
        app = self.app
        if not (app.cfg["platforms"].get("instagram") or {}).get("fallback_manual", True) \
                or not hasattr(app.bot, "instagram_manual") or app.cfg.get("dry_run"):
            return False
        try:
            app.bot.instagram_manual(item["id"], meta)
        except Exception as e:  # noqa: BLE001
            log.warning("#%s Instagram вручну: не вдалося надіслати в Telegram: %s", item["id"], e)
            return False
        log.info("#%s instagram: через API не прийняв (%s) — відео надіслано вам у Telegram", item["id"], why[:120])
        return True

    # ---------------- Instagram: єдина логіка (звичайна, «тиха» і фонова) ----------------
    def _ensure_file(self, item: dict) -> dict:
        """Файл перенесли? Шукаємо за назвою, перш ніж здаватися (RuntimeError — файлу немає)."""
        app = self.app
        if Path(item["mp4"]).is_file():
            return item
        new = None
        try:
            from app.channels import cutter_link
            new = cutter_link.locate(item["mp4"], cutter_link.build_index(app))
            if new and cutter_link.path_owner(app.db, new, item["id"]):
                new = None   # файл уже належить іншому запису — не беремо (буде дубль)
        except Exception as e:  # noqa: BLE001
            log.debug("пошук перенесеного: %s", e)
        if not new:
            raise RuntimeError(f"файлу немає на місці: {item['mp4']}")
        log.info("#%s: файл перенесено — беру з нового місця %s", item["id"], new)
        app.db.update_item(item["id"], mp4=str(new), json=str(new.with_suffix(".json")))
        return {**item, "mp4": str(new), "json": str(new.with_suffix(".json"))}

    def _ig_cfg(self) -> dict:
        return self.app.cfg["platforms"].get("instagram") or {}

    def _publish_instagram(self, item: dict, meta, plat) -> dict:
        """Один контейнер — одне очікування. ID контейнера зберігається в базі одразу після створення (перезапуск не
        створить другий). Новий контейнер (наступний спосіб) — лише після підтвердженої відмови Meta (ERROR/EXPIRED)."""
        app, igc = self.app, self._ig_cfg()
        prev = item["results"].get("instagram") or {}
        method = int(prev.get("method") or 0) if prev.get("status") == "ig_next" else 0
        tried = list(prev.get("tried") or []) if prev.get("status") == "ig_next" else []
        silent = bool(igc.get("silent_wait", False))
        wait = 60 * int(igc.get("wait_min", 20))
        started = now_ts()

        def on_container(rec: dict) -> None:   # відразу в базу: після перезапуску — той самий контейнер, не новий
            now = now_ts()
            self._save_ig(item, {"status": "processing", **rec, "since": now, "checks": 0, "tried": tried,
                                 "next_at": now + (wait if silent else INLINE_GRACE), "inline": not silent,
                                 "note": f"Instagram обробляє ({rec.get('method_name')})"})
        try:
            if silent:
                up = plat.upload_only(item["mp4"], meta, method, on_container)
                snap = self._ig_snapshot(plat, "завантажено — тиша до першої перевірки")
                log.info("#%s instagram: завантажено (%s, контейнер %s) — перша перевірка через %s хв",
                         item["id"], up["method_name"], up["container"], wait // 60)
                return {"status": "processing", **up, "since": started, "next_at": now_ts() + wait, "checks": 0,
                        "tried": tried + [up["method_name"]], "ig_report": snap,
                        "note": f"Instagram обробляє ({up['method_name']}) — перевірю через {wait // 60} хв"}
            r = plat.publish(item["mp4"], meta, method, on_container)
            log.info("#%s instagram: опубліковано (%s) %s", item["id"], r.get("method_name"), r.get("url"))
            if hasattr(app.bot, "resolve"):
                app.bot.resolve("reauth_instagram")
            return {"status": "ok", "attempts": len(tried) + 1, "published_at": now_ts(), **r}
        except Stopped:
            stored = (app.db.get_item(item["id"])["results"].get("instagram") or {})
            if stored.get("status") == "processing":   # контейнер уже є — НЕ кидаємо його, чекаємо у фоні
                return stored
            return None
        except StillProcessing as e:
            stored = (app.db.get_item(item["id"])["results"].get("instagram") or {})
            app.db.event(item["id"], "ig_processing", {"container": e.container_id})
            return {**stored, "status": "processing", "container": e.container_id, "inline": False,
                    "since": int(stored.get("since") or started), "next_at": now_ts() + 120, "checks": 0,
                    "tried": tried + [stored.get("method_name") or ""], "ig_report": self._ig_snapshot(plat, "обробляється — далі у фоні"),
                    "note": "Instagram ще обробляє — чекаю той самий контейнер, опублікую, щойно буде готово"}
        except ContainerFailed as e:
            stored = (app.db.get_item(item["id"])["results"].get("instagram") or {})
            return self._ig_after_failure(item, plat, {**stored, "container": e.container_id, "method": method,
                                                       "tried": tried + [stored.get("method_name") or ""]},
                                          e.state, str(e), meta=meta)
        except RateLimited as e:
            log.warning("#%s instagram: %s", item["id"], e)
            app.db.event(item["id"], "platform_error", {"platform": "instagram", "error": str(e)})
            return {"status": "error", "attempts": 1, "error": str(e)[:500], "retry_at": e.retry_at}
        except TokenExpired as e:
            log.error("#%s instagram: потрібна авторизація: %s", item["id"], e)
            app.bot.notify_reauth("instagram", str(e))
            return {"status": "error", "reauth": True, "attempts": 1, "error": f"потрібна повторна авторизація: {e}"[:500]}
        except PublishError as e:
            # контейнер не створено / файл не підходить / мережа: нового контейнера автоматично НЕ робимо
            log.warning("#%s instagram: %s", item["id"], e)
            app.db.event(item["id"], "platform_error", {"platform": "instagram", "error": str(e)[:2000]})
            if "тривалість" not in str(e) and self._ig_to_phone(item, meta, str(e)):
                return {"status": "manual", "sent_at": now_ts(), "error": str(e)[:500],
                        "note": "Instagram через API не прийняв — надіслано вам у Telegram"}
            return {"status": "error", "attempts": 1, "error": str(e)[:2000]}

    def _ig_snapshot(self, plat, outcome: str) -> dict:
        try:
            return plat.container_record(outcome)
        except Exception as e:  # noqa: BLE001 — діагностика ніколи не ламає публікацію
            log.debug("знімок контейнера: %s", e)
            return {}

    def _ig_after_failure(self, it: dict, plat, r: dict, state: str, why: str, meta=None) -> dict:
        """Підтверджена відмова Meta (ERROR / EXPIRED): наступний спосіб (новий контейнер) або — якщо способів
        більше немає — повне відео вам у Telegram / ❌."""
        methods = len(plat.methods_for(it["mp4"])) if hasattr(plat, "methods_for") else 1
        nxt = int(r.get("method") or 0) + 1
        if nxt < methods:
            log.warning("#%s instagram: контейнер %s — %s (%s); пробую наступний спосіб", it["id"], r.get("container"),
                        state.upper(), why[:200])
            self.app.db.event(it["id"], "ig_next_method", {"container": r.get("container"), "state": state, "why": why[:500]})
            return {"status": "ig_next", "method": nxt, "tried": [t for t in r.get("tried") or [] if t],
                    "note": f"{r.get('method_name') or 'спосіб'}: {state.upper()} — далі інший спосіб"}
        err = (f"Instagram не прийняв відео ({', '.join(t for t in r.get('tried') or [] if t) or 'усі способи'}). "
               f"Останнє: {why[:600]}")
        self.app.db.event(it["id"], "platform_error", {"platform": "instagram", "error": err[:2000]})
        try:
            meta = meta or apply_always_hashtags(load_meta(it["json"], it["title_override"]), self.app.cfg)
        except Exception:  # noqa: BLE001
            meta = None
        if meta is not None and self._ig_to_phone(it, meta, err):
            return {"status": "manual", "sent_at": now_ts(), "error": err[:500],
                    "note": "Instagram через API не прийняв — надіслано вам у Telegram"}
        return {"status": "error", "attempts": len(r.get("tried") or []) or 1, "error": err}

    def ig_processing(self, exclude: int | None = None) -> int | None:
        """Шортс, чий контейнер Instagram зараз обробляє (або ще потрібен інший спосіб). Інші чекають — в Instagram
        одне відео за раз. Контейнер, що обробляється довше block_max_min, чергу більше не тримає (сам чекає далі)."""
        limit = 60 * int(self._ig_cfg().get("block_max_min", 60))
        now = now_ts()
        for it in self.app.db.items(IG_PENDING_SQL, order="id"):
            r = ((it["results"] or {}).get("instagram") or {})
            if it["id"] == exclude or r.get("status") not in IG_BUSY:
                continue
            if r.get("status") == "processing" and now - int(r.get("since") or now) > limit:
                continue
            return it["id"]
        return None

    def finish_instagram(self, now: int | None = None) -> None:
        """Фон (раз на хвилину): ОДНА перевірка кожного контейнера, якому настав час. Статуси:
        FINISHED → публікуємо; PUBLISHED → вже опубліковано (без повтору); IN_PROGRESS / PENDING / невідомий →
        чекаємо той самий контейнер (паузи ростуть, повідомлення про затримку — один раз); ERROR / EXPIRED →
        наступний спосіб. Потім — Instagram для шортсів, що чекали черги."""
        app, db = self.app, self.app.db
        now = now or now_ts()
        plat = app.platforms.get("instagram")
        if not plat or not hasattr(plat, "finish_container") or db.get("stopped") == "1" or app.cfg.get("dry_run"):
            return
        for it in db.items(IG_PENDING_SQL, order="id"):
            r = (it["results"] or {}).get("instagram") or {}
            if r.get("status") != "processing" or now < int(r.get("next_at") or 0) or self.is_active(it["id"]) \
                    or not r.get("container"):
                continue
            if self._ig_check(it, r, plat, now) == "limit":
                return
        if db.get("stopped") == "1":
            return
        for it in db.items(IG_PENDING_SQL, order="id"):   # інший спосіб, що ще не стартував (перезапуск / «Стоп»)
            if ((it["results"] or {}).get("instagram") or {}).get("status") == "ig_next" and not self.is_active(it["id"]):
                self.start(it["id"])
                return
        if self.ig_processing():
            return
        for it in db.items(IG_PENDING_SQL, order="id"):
            if ((it["results"] or {}).get("instagram") or {}).get("status") == "ig_wait" and not self.is_active(it["id"]):
                results = dict(it["results"])
                results.pop("instagram", None)          # Instagram вільний — публікуємо (інші платформи вже готові)
                db.update_item(it["id"], results=results)
                log.info("#%s instagram: Instagram звільнився — публікую", it["id"])
                self.start(it["id"])
                return   # по одному: наступний — коли й цей закінчиться

    BG_STEPS = (120, 180, 300, 600, 900)   # фонові перевірки: 2, 3, 5, 10, 15 хв, далі 30 хв, після 6 год — 60 хв

    def _ig_check(self, it: dict, r: dict, plat, now: int) -> str:
        app, db = self.app, self.app.db
        cid, since = r["container"], int(r.get("since") or now)
        age = now - since
        checks = int(r.get("checks") or 0) + 1
        polls = list(r.get("polls") or [])[-200:]
        try:
            res = plat.finish_container(cid, since)
        except RateLimited as e:
            self._save_ig(it, {**r, "next_at": max(e.retry_at, now + 300)})
            return "limit"
        except PublishError as e:
            if str(e).startswith("media_publish:"):   # контейнер готовий, але публікація не пройшла — той самий
                fails = int(r.get("publish_fails") or 0) + 1
                self._hist(it, plat, "error", since, error=str(e))
                if fails >= 3:
                    err = f"media_publish не вдався 3 рази поспіль: {e}"
                    log.warning("#%s instagram: %s", it["id"], err)
                    self._save_ig(it, {"status": "error", "attempts": 1, "error": err[:2000], "container": cid})
                    if hasattr(app.bot, "notify_failed"):
                        app.bot.notify_failed(it["id"])
                    return "error"
                self._save_ig(it, {**r, "publish_fails": fails, "next_at": now + 300})
                return "retry"
            # мережа / тимчасова помилка запиту — стан невідомий: чекаємо той самий контейнер
            log.info("#%s instagram: перевірка контейнера %s не вдалася (%s) — спробую пізніше", it["id"], cid, e)
            self._save_ig(it, {**r, "next_at": now + 600, "last_check_error": str(e)[:300]})
            return "retry"
        polls.append([res.get("time") or now, res.get("status_code"), (res.get("status") or "")[:300]])
        state = res["state"]
        if state in ("published", "already"):
            log.info("#%s instagram: %s %s", it["id"], "опубліковано" if state == "published"
                     else "контейнер уже був опублікований (без повтору)", res.get("url"))
            self._hist(it, plat, "ok_background", since, url=res.get("url") or None, processing_s=age)
            self._save_ig(it, {"status": "ok", "attempts": len(r.get("tried") or []) or 1, "published_at": now_ts(),
                               "id": res.get("id"), "url": res.get("url") or "", "container": cid, "polls": polls,
                               "note": r.get("method_name") or ""})
            db.event(it["id"], "published", {"ok": ["instagram"], "urls": {"instagram": res.get("url")},
                                              "background": True})
            if hasattr(app.bot, "drop_failed_card"):
                app.bot.drop_failed_card(it["id"])
            return "ok"
        if state in ("error", "expired"):
            why = f"{res.get('status_code')}: {res.get('status')}"
            self._hist(it, plat, "next_method" if state == "error" else "error", since, error=why, processing_s=age)
            self._save_ig(it, self._ig_after_failure(it, plat, {**r, "polls": polls}, state, why))
            return "failed"
        # waiting / unknown — той самий контейнер, нового НЕ створюємо
        if state == "unknown":
            log.warning("#%s instagram: невідомий статус контейнера %s: %r — чекаю далі той самий контейнер",
                        it["id"], cid, res.get("status_code"))
        step = self.BG_STEPS[checks - 1] if checks <= len(self.BG_STEPS) else (1800 if age < 6 * 3600 else 3600)
        upd = {**r, "checks": checks, "next_at": now + step, "polls": polls,
               "last_status": res.get("status_code"), "last_status_text": res.get("status")}
        notify_after = 60 * int(self._ig_cfg().get("block_max_min", 60))
        if age >= notify_after and not r.get("delay_told"):
            upd["delay_told"] = True
            log.warning("#%s instagram: контейнер %s обробляється %s хв (%s) — зберігаю й перевіряю далі",
                        it["id"], cid, age // 60, res.get("status_code"))
            if hasattr(app.bot, "send"):
                app.bot.send(f"⏳ Instagram обробляє #{it['id']} уже {age // 60} хв (статус {res.get('status_code')}). "
                             f"Контейнер {cid} збережено — перевіряю далі й опублікую, щойно Meta закінчить; новий не "
                             "створюю. Інші шортси в Instagram ідуть далі. Не хочете чекати — «📱 Надіслати мені для "
                             "Instagram» під шортсом.")
        else:
            log.info("#%s instagram: контейнер %s — %s (%s хв), наступна перевірка через %s хв",
                     it["id"], cid, res.get("status_code"), age // 60, step // 60)
        self._save_ig(it, upd)
        return "wait"

    def _save_ig(self, item: dict, res: dict) -> None:
        results = dict(self.app.db.get_item(item["id"])["results"])
        results["instagram"] = res
        self.app.db.update_item(item["id"], results=results)
        if hasattr(self.app.bot, "refresh"):
            try:
                self.app.bot.refresh(item["id"])
            except Exception:  # noqa: BLE001
                pass

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
