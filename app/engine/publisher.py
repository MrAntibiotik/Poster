"""Публікація на всі платформи: незалежно, з повторними спробами, архівування, запис у json."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from app.core import media
from app.core.config import PLATFORMS, local_dt, now_ts
from app.core.meta import apply_always_hashtags, highlight_description, load_meta, move_pair, read_json, write_json
from app.platforms import RateLimited, StillProcessing, Stopped, TokenExpired

log = logging.getLogger("publisher")

# Instagram ще не опубліковано, але це не помилка: надіслано вам / пропущено / Instagram обробляє у фоні /
# чекає, поки Instagram закінчить попереднє відео. Такі платформи не повторюються і не рахуються невдалими.
PENDING = ("manual", "skipped", "processing", "ig_wait", "ig_next")
DONE = tuple(x for x in ("ok",) + PENDING if x != "ig_next")   # ig_next — Instagram ще треба завантажити (інший спосіб)
IG_PENDING_SQL = ("status IN ('posted','publishing','failed') AND "
                  "(results LIKE '%\"processing\"%' OR results LIKE '%\"ig_wait\"%' OR results LIKE '%\"ig_next\"%')")
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
        app = self.app
        igc = app.cfg["platforms"].get("instagram") or {}
        too_long = key == "instagram" and not igc.get("manual") and self._ig_too_long(item, igc)
        if key == "instagram" and (igc.get("manual") or too_long) \
                and hasattr(app.bot, "instagram_manual") and not app.cfg.get("dry_run"):
            try:   # напівавтомат: відео й підпис — вам у Telegram, публікуєте з телефона
                app.bot.instagram_manual(item["id"], meta)
                if too_long:
                    log.info("#%s instagram: відео %s с — довше за %s с, Instagram через API таке не приймає; "
                             "надіслано вам у Telegram повністю (без запитів до Instagram)",
                             item["id"], too_long, igc.get("auto_max_seconds"))
                return {"status": "manual", "sent_at": now_ts(),
                        "note": (f"{too_long} с — довше за {igc.get('auto_max_seconds')} с, тому вам у Telegram"
                                 if too_long else "надіслано в Telegram для публікації з телефона")}
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
                            if new and cutter_link.path_owner(app.db, new, item["id"]):
                                new = None   # файл уже належить іншому запису — не беремо (буде дубль)
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
                    if key == "instagram" and hasattr(plat, "upload_only") and \
                            (app.cfg["platforms"].get("instagram") or {}).get("silent_wait", False):
                        # завантажили — і мовчки чекаємо wait_min; один запит «готово?» робить finish_instagram
                        prev = item["results"].get("instagram") or {}
                        m = int(prev.get("method") or 0) if prev.get("status") == "ig_next" else 0
                        up = plat.upload_only(item["mp4"], meta, m)
                        now = now_ts()
                        wait = 60 * int((app.cfg["platforms"].get("instagram") or {}).get("wait_min", 20))
                        log.info("#%s instagram: завантажено (%s) — перевірю один раз через %s хв, до того Instagram "
                                 "не турбую", item["id"], up.get("method_name"), wait // 60)
                        app.db.set(f"pubtry:{item['id']}:{key}", None)
                        return {"status": "processing", **up, "since": now, "next_at": now + wait,
                                "tried": list(prev.get("tried") or []) + [up.get("method_name")],
                                "note": f"Instagram обробляє ({up.get('method_name')}) — перевірю через {wait // 60} хв"}
                    r = plat.publish(item["mp4"], meta)
                log.info("#%s %s: опубліковано %s", item["id"], key, r.get("url"))
                if hasattr(app.bot, "resolve"):
                    app.bot.resolve(f"reauth_{key}")
                return {"status": "ok", "attempts": i, "published_at": now_ts(), **r}
            except Stopped:
                app.db.set(f"pubtry:{item['id']}:{key}", None)
                return None
            except StillProcessing as e:
                # файл дійшов, Instagram довго обробляє — черга йде далі, публікація завершиться у фоні
                app.db.set(f"pubtry:{item['id']}:{key}", None)
                app.db.event(item["id"], "ig_processing", {"container": e.container_id})
                now = now_ts()
                return {"status": "processing", "flow": "poll", "container": e.container_id, "since": now,
                        "next_at": now + 120, "checks": 0,
                        "note": "Instagram ще обробляє — опублікую, щойно буде готово"}
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

    def _ig_too_long(self, item: dict, igc: dict) -> int:
        """Тривалість (с), якщо відео довше за platforms.instagram.auto_max_seconds (інакше 0)."""
        limit = float(igc.get("auto_max_seconds", 55) or 0)
        if not limit:
            return 0
        try:
            dur = float(media.ig_probe(item["mp4"], self.app.cfg.get("ffprobe", "ffprobe")).get("duration") or 0)
        except Exception:  # noqa: BLE001 — не вдалося виміряти: пробуємо як звичайно
            return 0
        return int(round(dur)) if dur > limit else 0

    def ig_processing(self, exclude: int | None = None) -> int | None:
        """Шортс, чиє відео Instagram зараз обробляє у фоні (або None)."""
        for it in self.app.db.items(IG_PENDING_SQL, order="id"):
            if it["id"] != exclude and ((it["results"] or {}).get("instagram") or {}).get("status") in IG_BUSY:
                return it["id"]
        return None

    def finish_instagram(self, now: int | None = None) -> None:
        """Фон (раз на хвилину з планувальника): довга обробка Instagram — перевірити контейнер і опублікувати;
        коли Instagram звільнився — запустити Instagram для шортса, що чекав своєї черги."""
        app, db = self.app, self.app.db
        now = now or now_ts()
        plat = app.platforms.get("instagram")
        if not plat or not hasattr(plat, "finish_container") or db.get("stopped") == "1" or app.cfg.get("dry_run"):
            return
        items = db.items(IG_PENDING_SQL, order="id")
        for it in items:
            r = (it["results"] or {}).get("instagram") or {}
            if r.get("status") != "processing" or now < int(r.get("next_at") or 0) or self.is_active(it["id"]):
                continue
            if not r.get("method_name"):   # звичайний спосіб (як у версіях до 157): рідкі перевірки до 60 хв
                if self._finish_poll(it, r, plat, now) == "limit":
                    return
                continue
            why = ""
            try:   # ОДИН запит: контейнер готовий → публікуємо; ні → наступний спосіб
                got = plat.finish_container(r["container"])
            except RateLimited as e:
                self._save_ig(it, {**r, "next_at": max(e.retry_at, now + 300)})
                return
            except Exception as e:  # noqa: BLE001
                got, why = None, str(e)[:300]
            if got:
                log.info("#%s instagram: опубліковано (%s) %s", it["id"], r.get("method_name"), got.get("url"))
                self._save_ig(it, {"status": "ok", "attempts": len(r.get("tried") or [1]), "published_at": now_ts(),
                                   **got, "note": r.get("method_name") or ""})
                db.event(it["id"], "published", {"ok": ["instagram"], "urls": {"instagram": got.get("url")},
                                                  "background": True})
                if hasattr(app.bot, "drop_failed_card"):
                    app.bot.drop_failed_card(it["id"])
                continue
            why = why or f"за {(now - int(r.get('since') or now)) // 60} хв не оброблено"
            nxt = int(r.get("method") or 0) + 1
            methods = getattr(plat, "METHODS", ())
            if nxt < len(methods):
                log.warning("#%s instagram: %s не пройшла (%s) — пробую наступний спосіб: %s",
                            it["id"], r.get("method_name"), why, methods[nxt][0])
                self._save_ig(it, {"status": "ig_next", "method": nxt, "tried": r.get("tried") or [],
                                   "note": f"{r.get('method_name')}: {why}; далі — {methods[nxt][0]}"})
                db.event(it["id"], "ig_next_method", {"failed": r.get("method_name"), "why": why})
                self.start(it["id"])     # той самий шортс, наступний спосіб; інші в Instagram — після нього
                return
            err = (f"Instagram не прийняв відео жодним способом ({', '.join(r.get('tried') or [])}). "
                   f"Останнє: {why}. Опублікуйте з телефона («📱 Надіслати мені для Instagram») або повторіть пізніше.")
            log.warning("#%s instagram: %s", it["id"], err)
            self._save_ig(it, {"status": "error", "attempts": len(r.get("tried") or []), "error": err})
            db.event(it["id"], "platform_error", {"platform": "instagram", "error": err})
            if hasattr(app.bot, "notify_failed"):
                app.bot.notify_failed(it["id"])
        if db.get("stopped") == "1":
            return
        for it in db.items(IG_PENDING_SQL, order="id"):   # «наступний спосіб», який не стартував (перезапуск / Стоп)
            if ((it["results"] or {}).get("instagram") or {}).get("status") == "ig_next" and not self.is_active(it["id"]):
                self.start(it["id"])
                return
        if self.ig_processing() or db.get("stopped") == "1":
            return
        for it in db.items(IG_PENDING_SQL, order="id"):
            if ((it["results"] or {}).get("instagram") or {}).get("status") == "ig_wait" and not self.is_active(it["id"]):
                results = dict(it["results"])
                results.pop("instagram", None)          # Instagram вільний — публікуємо (інші платформи вже готові)
                db.update_item(it["id"], results=results)
                log.info("#%s instagram: Instagram звільнився — публікую", it["id"])
                self.start(it["id"])
                return   # по одному: наступний — коли й цей закінчиться

    def _finish_poll(self, it: dict, r: dict, plat, now: int) -> str:
        """Звичайний спосіб (версії 144–156, що публікував): контейнер обробляється довше 10 хв — перевіряємо
        у фоні рідко (2, 3, 5, 10, далі раз на 15 хв), до 60 хв; готово — публікуємо, завис — ❌."""
        app, db = self.app, self.app.db
        checks = int(r.get("checks") or 0) + 1
        try:
            got = plat.finish_container(r["container"])
        except RateLimited as e:
            self._save_ig(it, {**r, "next_at": max(e.retry_at, now + 300)})
            return "limit"
        except Exception as e:  # noqa: BLE001
            log.warning("#%s instagram: фонова обробка не вдалася: %s", it["id"], e)
            self._save_ig(it, {"status": "error", "attempts": 1, "error": str(e)[:500]})
            db.event(it["id"], "platform_error", {"platform": "instagram", "error": str(e)[:2000]})
            if hasattr(app.bot, "notify_failed"):
                app.bot.notify_failed(it["id"])
            return "error"
        mins = (now - int(r.get("since") or now)) // 60
        if got:
            log.info("#%s instagram: опубліковано після фонової обробки %s", it["id"], got.get("url"))
            self._save_ig(it, {"status": "ok", "attempts": 1, "published_at": now_ts(), **got,
                               "note": f"Instagram обробляв {max(1, mins)} хв"})
            db.event(it["id"], "published", {"ok": ["instagram"], "urls": {"instagram": got.get("url")},
                                              "background": True})
            if hasattr(app.bot, "drop_failed_card"):
                app.bot.drop_failed_card(it["id"])
            return "ok"
        if mins > int((app.cfg["platforms"].get("instagram") or {}).get("background_max_min", 60)):
            err = (f"Instagram так і не обробив відео ({mins} хв «In Progress») — контейнер завис. "
                   "Повторіть або опублікуйте з телефона («📱 Надіслати мені для Instagram»).")
            log.warning("#%s instagram: контейнер %s завис (%s хв) — більше не чекаю", it["id"], r.get("container"), mins)
            self._save_ig(it, {"status": "error", "attempts": 1, "error": err})
            db.event(it["id"], "platform_error", {"platform": "instagram", "error": err})
            if hasattr(app.bot, "notify_failed"):
                app.bot.notify_failed(it["id"])
            return "error"
        log.info("#%s instagram: контейнер %s ще обробляється (%s хв) — перевірю пізніше", it["id"], r.get("container"), mins)
        step = (120, 180, 300, 600)[checks - 1] if checks <= 4 else 900
        self._save_ig(it, {**r, "checks": checks, "next_at": now + step})
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
