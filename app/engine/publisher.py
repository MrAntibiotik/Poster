"""Публікація на всі платформи: незалежно, з повторними спробами, архівування, запис у json."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from app.core import media
from app.core.config import PLATFORMS, local_dt, now_ts
from app.core.meta import apply_always_hashtags, load_meta, move_pair, read_json, write_json
from app.platforms import RateLimited, TokenExpired

log = logging.getLogger("publisher")


class Publisher:
    def __init__(self, app):
        self.app = app
        self.active: set[int] = set()
        self._lock = threading.Lock()
        self.sync = False  # для тестів: публікувати в поточному потоці
        self.sleep = time.sleep

    def is_active(self, item_id: int) -> bool:
        with self._lock:
            return item_id in self.active

    def start(self, item_id: int) -> None:
        if self.app.db.get("stopped") == "1":  # «⛔ Стоп публікацій» — не починаємо нічого
            log.info("#%s: публікації зупинено — не публікую", item_id)
            if getattr(self.app.bot, "send", None) and self.app.db.get(f"stop_told:{item_id}") != "1":
                self.app.db.set(f"stop_told:{item_id}", "1")
                self.app.bot.send(f"⛔ Публікації зупинено — #{item_id} не публікую. «✅ Відновити публікації», "
                                  "щоб продовжити.")
            return
        self.app.db.set(f"stop_told:{item_id}", None)
        with self._lock:
            if item_id in self.active:
                return
            self.active.add(item_id)
        self.app.db.update_item(item_id, status="publishing")
        if self.sync:
            self._run_safe(item_id)
        else:
            threading.Thread(target=self._run_safe, args=(item_id,), daemon=True,
                             name=f"publish-{item_id}").start()

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

    # ------------------------------------------------------------------
    def targets(self, item: dict) -> list[str]:
        return [p for p in PLATFORMS if p in self.app.platforms and p not in item["disabled"]]

    def run(self, item_id: int) -> None:
        app, db = self.app, self.app.db
        item = db.get_item(item_id)
        meta = apply_always_hashtags(load_meta(item["json"], item["title_override"]), app.cfg)
        if item.get("kind") == "highlight":
            meta.kind = "highlight"
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
            results[key] = self._publish_one(key, item, meta)
            db.set(f"pubnow:{item_id}", None)
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
        limited = [results[k]["retry_at"] for k in failed if results.get(k, {}).get("retry_at")]
        if limited:
            self._retry_after_limit(item_id, max(limited) + 120)
        if failed and hasattr(app.bot, "notify_failed"):
            app.bot.notify_failed(item_id)
        log.info("#%s: успішно %s, помилки %s", item_id, ok, failed)

    def _publish_one(self, key: str, item: dict, meta) -> dict:
        app = self.app
        plat = app.platforms[key]
        attempts = max(1, int(app.cfg["retry"]["attempts"]))
        pause = int(app.cfg["retry"]["pause_seconds"])
        last = None
        for i in range(1, attempts + 1):
            try:
                if app.cfg.get("dry_run"):
                    r = {"id": f"dry-{key}-{item['id']}", "url": f"https://example.com/{key}/{item['id']}",
                         "note": "dry_run: реальної публікації не було"}
                else:
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

    def _retry_after_limit(self, item_id: int, ts: int) -> None:
        """Платформа обмежила запити — повтор невдалих платформ ставимо на час після паузи (сам, без кнопок)."""
        sched = getattr(self.app, "scheduler", None)
        if not sched or not hasattr(sched, "reserve_retry"):
            return
        ts = max(ts, now_ts() + 300) // 60 * 60 + 60
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
