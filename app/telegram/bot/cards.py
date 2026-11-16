"""Картки шортсів: підпис, статуси платформ, кнопки, оновлення та самовідновлення."""
from __future__ import annotations

import json
import re
from app.core import media
from app.core.config import NAMES
from app.core.config import PLATFORMS
from app.core.config import SHORT
from app.core.config import enabled_platforms
from app.core.config import fmt_slot
from app.core.config import fmt_when
from app.core.config import local_dt
from app.core.config import now_ts
from app.core.meta import PostMeta
from app.core.meta import apply_always_hashtags
from app.core.meta import load_meta
from app.telegram.api import TelegramError
from pathlib import Path
from .common import (
    CAPTION_LIMIT,
    esc,
    log,
    visible_len,
)


class CardsMixin:
    def ask_duplicate(self, item_id: int, other: dict, kind: str) -> None:
        """Шортс уже публікувався / уже в черзі — питаємо, чи справді публікувати ще раз."""
        item = self.db.get_item(item_id)
        name = esc(Path(item["mp4"]).stem)
        how = "той самий файл" if kind == "same" else "та сама назва"
        if other["status"] == "posted":
            when = fmt_when(self.cfg, other["posted_at"]) if other.get("posted_at") else "раніше"
            links = [f'<a href="{esc(r["url"])}">{NAMES.get(k, k)}</a>' for k, r in (other.get("results") or {}).items()
                     if isinstance(r, dict) and r.get("status") == "ok" and r.get("url")]
            state = f"уже <b>опубліковано</b> {when}" + (f": {', '.join(links)}" if links else "")
        else:
            state = f"уже <b>в роботі</b> (#{other['id']}, {other['status']})"
        text = (f"⚠️ <b>Схоже на повтор</b> ({how})\n{name}\n\nЦей шортс {state}.\n"
                "Я <b>не ставлю його в чергу</b>. Опублікувати ще раз?")
        kb = {"inline_keyboard": [[{"text": "🔁 Так, ще раз", "callback_data": f"dup:{item_id}:yes"},
                                   {"text": "🗑 Ні, прибрати", "callback_data": f"dup:{item_id}:no"}]]}
        for cid in self.admins():
            self.say(cid, text, True, kb, keep=True)

    def resolve_duplicate(self, item_id: int, yes: bool, by=None) -> str:
        item = self.db.get_item(item_id)
        if not item or item["status"] != "duplicate":
            return "Уже вирішено"
        if yes:
            self.db.update_item(item_id, status="queued", qpos=self.db.next_qpos(), note=None)
            self.db.event(item_id, "queued", {"file": Path(item["mp4"]).name, "duplicate_ok": True, "by": by})
            self.app.scheduler.announce_queued([item_id])
            return f"🔁 #{item_id} у черзі — буде опубліковано ще раз"
        from app.core.meta import move_pair
        try:
            mp4, js = move_pair(item["mp4"], item["json"], Path(self.cfg["paths"]["cancelled"]))
        except OSError as e:
            mp4, js = item["mp4"], item["json"]
            log.warning("Повтор #%s: не вдалося перенести файл: %s", item_id, e)
        self.db.update_item(item_id, status="cancelled", mp4=mp4, json=js, note="повтор")
        self.db.event(item_id, "cancelled", {"by": by, "duplicate": True})
        return f"🗑 #{item_id} прибрано (файл — у папці «Скасовані»)"

    def notify_reauth(self, key: str, reason: str) -> None:
        k = f"reauth_{key}"
        last = int(self.db.get(k) or 0)
        if now_ts() - last < 6 * 3600:
            return
        self.db.set(k, now_ts())
        hint = self.app.platforms[key].reauth_hint if key in self.app.platforms else f"python poster.py auth {key}"
        self.send(keep=f"reauth_{key}", text=f"🔑 <b>Потрібна повторна авторизація: {NAMES[key]}</b>\n"
                  f"Причина: {esc(reason[:300])}\n\nНа ПК: панель керування → {NAMES[key]} → «Авторизувати»\n"
                  f"або команда <code>{esc(hint)}</code>", html_mode=True)

    # ================= сповіщення про шортс =================
    def _meta(self, item: dict) -> PostMeta:
        try:
            return apply_always_hashtags(load_meta(item["json"], item["title_override"]), self.cfg)
        except Exception as e:  # noqa: BLE001
            if not self.db.get(f"metawarn:{item['id']}"):   # один раз, а не щохвилини
                self.db.set(f"metawarn:{item['id']}", 1)
                log.warning("Метадані #%s недоступні: %s", item["id"], e)
            return PostMeta(yt_title=item["title_override"] or Path(item["mp4"]).stem)

    def _raw_meta(self, item: dict) -> PostMeta:
        """Метадані без обов'язкових хештегів — те, що вводить/залишає користувач."""
        try:
            return load_meta(item["json"], item["title_override"])
        except Exception:  # noqa: BLE001
            return PostMeta(yt_title=item["title_override"] or Path(item["mp4"]).stem)

    def _always_line(self) -> str:
        conf = self.cfg.get("always_hashtags") or {}
        return " ".join(conf.get("tags") or []) if conf.get("in_title", True) else ""

    def _platform_line(self, item: dict) -> str:
        def mark(p: str) -> str:
            if p in item["disabled"]:
                return "🚫"
            if p == "youtube":
                priv = self.cfg["platforms"]["youtube"].get("privacy", "public")
                return {"private": "✅🔒", "unlisted": "✅🔗"}.get(priv, "✅")
            if p == "tiktok" and not self.cfg["platforms"]["tiktok"].get("public"):
                return "✅🔒"
            return "✅"
        parts = [f"{SHORT[p]} {mark(p)}" for p in enabled_platforms(self.cfg)]
        return " ".join(parts) or "—"

    def _status_block(self, item: dict) -> str:
        st = item["status"]
        slot = fmt_slot(self.cfg, item["slot_at"]) if item["slot_at"] else "—"
        edited = f"\n✏️ Змінено: {', '.join(item['edited'])}" if item["edited"] else ""
        if st == "notified":
            if item["slot_at"] and item["slot_at"] <= now_ts():
                return (f"⏳ Слот {slot} минув — чекаю на «🚀 Опублікувати», після нього публікую одразу "
                        f"(або «🕐 Змінити час»){edited}")
            return f"⏳ Очікує підтвердження до {slot}{edited}"
        if st == "approved":
            return f"👍 Підтверджено — публікація о {slot}{edited}"
        if st == "publishing":
            pos = self.app.publisher.position(item["id"]) if hasattr(self.app.publisher, "position") else None
            if pos:
                return (f"⏳ <b>У черзі на публікацію</b> ({pos}-й) — шортси публікуються по одному"
                        + self._results(item, publishing=True))
            if not self.app.publisher.is_active(item["id"]):
                if self.db.get(f"held:{item['id']}"):
                    return ("⏸ <b>На паузі</b> — інші шортси публікуються далі; «▶️ Продовжити», щоб поставити в чергу"
                            + self._results(item, publishing=True))
                if self.db.get("stopped") == "1":
                    return ("⛔ <b>Публікацію зупинено</b> — решта платформ після «✅ Відновити публікації»"
                            + self._results(item, publishing=True))
                return ("⚠️ <b>Публікацію перервано</b> — «🔁 Повторити зараз» або дочекайтесь повтору"
                        + self._results(item, publishing=True))
            return "⏫ <b>Публікується…</b>" + self._results(item, publishing=True)
        if st in ("posted", "failed"):
            targets = [p for p in PLATFORMS if p in self.app.platforms and p not in item["disabled"]] or \
                list(item["results"])
            ok = [p for p in targets if (item["results"].get(p) or {}).get("status") in ("ok", "manual", "skipped")]
            if not ok:
                head = "❌ <b>Не опубліковано</b>"
            elif len(ok) < len([p for p in targets if p in item["results"]]):
                head = f"⚠️ <b>Опубліковано частково</b> ({len(ok)} з {len([p for p in targets if p in item['results']])})"
            else:
                head = "✅ <b>Опубліковано</b>"
            when = self.app.scheduler.retry_at(item["id"])
            ignored = " · 🙈 проігноровано" if self.db.get(f"ignored:{item['id']}") else ""
            return head + ignored + self._results(item) + (f"\n🔁 Повтор: {fmt_when(self.cfg, when)}" if when else "")
        if st == "queued":
            if item["note"]:
                return esc(item["note"])
            if item["skips"]:
                return f"⏭ Пропущено ({item['skips']}/{self.cfg['max_skips']}) — шортс переміщено в кінець черги"
            return ("📥 <b>Новий у черзі</b> — можна підтвердити вже зараз («✅ Підтвердити»), "
                    f"інакше прийде на підтвердження за {self.cfg['notify_before_minutes']} хв до часу.")
        if st == "skipped":
            return f"⏭ Пропущено {item['skips']} рази — файли перенесено в skipped/"
        if st == "cancelled":
            return "🚫 <b>Скасовано</b> — файл у папці «скасовані». Черга зсунулась: наступний шортс іде в цей слот."
        if st == "missing":
            return "⚠️ Файл не знайдено — шортс прибрано з черги"
        return st

    @staticmethod
    def _short_note(note: str) -> str:
        n = str(note or "").lower()
        if "крайній варіант" in n or "30 к/с" in n:
            return "копія 30 к/с"
        if "сувора копія" in n or "тією ж частотою" in n:
            return "полегшена копія"
        if "перекодовано" in n or "копію під вимоги" in n or "копія під вимоги" in n or "не прийняв оригінал" in n:
            return "копія під вимоги Reels"
        if "приват" in n:
            return "приватне"
        if "токеном користувача" in n:
            return "резервний токен"
        return ""

    def _phase_text(self, item_id: int) -> str:
        """«⏳ Instagram обробляє відео · 7 хв (перевірено 23:58)» або попередження, якщо давно немає відповіді."""
        import json as _json
        try:
            ph = _json.loads(self.db.get(f"pubphase:{item_id}") or "{}")
        except ValueError:
            ph = {}
        if not ph.get("phase"):
            return "⏳ публікується…"
        now = now_ts()
        mins = max(0, (now - int(ph.get("since") or now)) // 60)
        quiet = (now - int(ph.get("beat") or now)) // 60
        dur = f" · {mins} хв" if mins else ""
        beat = local_dt(self.cfg, int(ph.get("beat") or now)).strftime("%H:%M")
        phase = str(ph["phase"])
        limit = 25 if "копію" in phase else 15 if phase.startswith("завантаж") else 6   # копія/завантаження — довгі кроки
        if quiet >= limit:
            return (f"⚠️ {esc(ph['phase'])}{dur}, але вже {quiet} хв без відповіді (остання {beat}) — "
                    "можливо, зависло: «⛔ Стоп» і «🔁 Повторити»")
        return f"⏳ {esc(ph['phase'])}{dur} (перевірено {beat})"

    def _results(self, item: dict, publishing: bool = False) -> str:
        from app.insights.reports import ICONS, explain_error
        lines = []
        targets = [p for p in PLATFORMS if p in self.app.platforms and p not in item["disabled"]]
        # «зараз публікується» — лише якщо публікація справді йде (після перезапуску / «Стоп» позначка застаріла)
        pos = self.app.publisher.position(item["id"]) if publishing and hasattr(self.app.publisher, "position") \
            else None
        current = self.db.get(f"pubnow:{item['id']}") if pos == 0 else None   # лише той, що справді йде
        in_work = publishing and self.app.publisher.is_active(item["id"])
        for p in PLATFORMS:
            r = item["results"].get(p)
            icon = ICONS.get(p, "•")
            if p in item["disabled"]:
                continue
            if p == current:  # саме зараз публікується (стару помилку не показуємо — вона може вже не бути актуальною)
                tr = self.db.get(f"pubtry:{item['id']}:{p}")
                lines.append(f"{icon} {NAMES[p]} — {self._phase_text(item['id'])}" + (f" (спроба {tr})" if tr else ""))
                continue
            if in_work and p in targets and (not r or r.get("status") != "ok"):
                lines.append(f"{icon} {NAMES[p]} — ⏳ далі")   # стара помилка неактуальна: платформа ще попереду
                continue
            if not r:
                if publishing and p in targets:
                    lines.append(f"{icon} {NAMES[p]} — " + ("⏸" if self.db.get("stopped") == "1" else "⏳"))
                continue
            if r.get("status") == "manual":
                lines.append(f"{icon} {NAMES[p]} — 📱 надіслано вам у чат: опублікуйте з телефона й натисніть «✅»")
                continue
            if r.get("status") == "skipped":
                lines.append(f"{icon} {NAMES[p]} — ⏭ пропущено")
                continue
            if r.get("status") == "ok":
                lock = {"SELF_ONLY": " 🔒", "private": " 🔒", "unlisted": " 🔗"}.get(r.get("privacy"), "")
                name = f'<a href="{esc(r["url"])}">{NAMES[p]}</a>' if r.get("url") else NAMES[p]
                extra = []
                if isinstance(r.get("views24"), int):
                    extra.append(f"👁 {r['views24']:,}".replace(",", " ") +
                                 (f" ❤️ {r['likes24']}" if r.get("likes24") is not None else ""))
                note = self._short_note(r.get("note"))
                if note:
                    extra.append(f"<i>{note}</i>")
                lines.append(f"{icon} {name} ✅{lock}" + (" · " + " · ".join(extra) if extra else ""))
            else:
                _, text, _ = explain_error(p, r.get("error", ""))
                text = text.replace(f"{NAMES[p]}: ", "").replace(f"{NAMES[p]} ", "")
                lines.append(f"{icon} {NAMES[p]} — ❌ {esc(text[:80])}")
        return ("\n" + "\n".join(lines)) if lines else ""

    def caption(self, item: dict, meta: PostMeta | None = None) -> str:
        meta = meta or self._meta(item)
        slot = fmt_slot(self.cfg, item["slot_at"]) if item["slot_at"] else "—"
        head = f"🎬 <b>{esc(meta.yt_title)}</b>"
        if item.get("kind") == "highlight":
            head = "🎞 <b>ХАЙЛАЙТ</b> · YouTube, звичайне відео\n" + head
        tags = esc(" ".join(meta.hashtags))
        hint = ""
        if item["status"] == "notified" and self.cfg.get("ask_texts_before_publish"):
            hint = "\n✍️ Натисніть «🚀 Опублікувати» — бот попросить назву й хештеги"
        elif item["status"] in ("notified", "approved") and "назву" not in item["edited"]:
            if meta.ai:
                hint = "\n🤖 Тексти від ШІ — перевірте; інший варіант: «🔄 Перегенерувати тексти»"
            elif meta.auto:
                hint = "\nℹ️ Назва з імені файлу — змінити: «✏️ Ввести назву вручну»"
        q = self.db.get(f"qline:{item['id']}") if item["status"] in ("notified", "approved") else None
        st = item["status"]
        if st in ("notified", "approved"):
            info = f"🕐 Слот: {slot} | Платформи: {self._platform_line(item)}\n" + (f"📐 {esc(q)}\n" if q else "")
        elif st in ("posted", "failed") and item["posted_at"]:
            info = f"📅 {local_dt(self.cfg, item['posted_at']):%d.%m %H:%M}\n"
        elif st == "queued":
            plan = {it["id"]: ts for it, ts in self.app.scheduler.projection()}
            info = f"📦 У черзі → {fmt_when(self.cfg, plan[item['id']])}\n" if plan.get(item["id"]) else ""
        else:
            info = ""
        tail = info + f"\n{self._status_block(item)}{hint}"
        text = "\n".join(x for x in (head, tags, tail) if x)
        if visible_len(text) > CAPTION_LIMIT and tags:
            room = CAPTION_LIMIT - visible_len(head + "\n\n" + tail) - 2
            short = " ".join(meta.hashtags)[:max(0, room)].rsplit(" ", 1)[0] if room > 0 else ""
            text = "\n".join(x for x in (head, esc(short), tail) if x)
        if visible_len(text) > CAPTION_LIMIT:
            text = text[:CAPTION_LIMIT - 1] + "…"
            text = re.sub(r"<[^>]*$", "", text)
        return text

    def texts(self, item: dict, meta: PostMeta | None = None, quality: str = "") -> str:
        meta = meta or self._meta(item)
        private = "" if self.cfg["platforms"]["tiktok"].get("public") else " (приватно)"
        parts = [f"📝 Тексти для шортса #{item['id']}"]
        if quality:
            parts.append(quality)
        if "tiktok" in self.app.platforms:
            parts.append(f"🎵 <b>TikTok</b>{private}:\n{esc(meta.tiktok_text[:1300])}")
        same = meta.facebook_text == meta.instagram_text
        if "instagram" in self.app.platforms or "facebook" in self.app.platforms:
            parts.append(f"📸 <b>Instagram{' + Facebook' if same else ''}</b>:\n{esc(meta.instagram_text[:1300])}")
        if not same and "facebook" in self.app.platforms:
            parts.append(f"📘 <b>Facebook</b>:\n{esc(meta.facebook_text[:1000])}")
        return "\n\n".join(parts)[:4096]

    def failed_errors(self, item: dict) -> dict:
        return {p: r for p, r in item["results"].items()
                if r.get("status") not in ("ok", "manual", "skipped") and p in self.app.platforms and p not in item["disabled"]}

    def retry_keyboard(self, item_id: int) -> dict:
        item = self.db.get_item(item_id) or {"results": {}}
        ig = (item.get("results") or {}).get("instagram") or {}
        manual_row = ([[{"text": "📱 Надіслати мені для Instagram", "callback_data": f"igsend:{item_id}"}]]
                      if ig and ig.get("status") not in ("ok", "manual", "skipped") else [])
        return {"inline_keyboard": manual_row + [
            [{"text": "🔁 Повторити зараз", "callback_data": f"rf:{item_id}"}],
            [{"text": "🕐 Обрати час", "callback_data": f"rt:{item_id}"},
             {"text": "📦 У вільний слот", "callback_data": f"rq:{item_id}"}],
            [{"text": "✖️ Ігнорувати", "callback_data": f"ri:{item_id}"}]]}

    def keyboard(self, item: dict) -> dict | None:
        if item["status"] == "publishing" and self.app.publisher.is_active(item["id"]):
            return {"inline_keyboard": [[{"text": "⏸ Пауза — далі наступне", "callback_data": f"hold:{item['id']}"}],
                                        [{"text": "🧾 Деталі (наживо)", "callback_data": f"err:{item['id']}"}]]}
        if item["status"] == "publishing" and self.db.get(f"held:{item['id']}"):
            return {"inline_keyboard": [[{"text": "▶️ Продовжити", "callback_data": f"unhold:{item['id']}"}],
                                        [{"text": "✖️ Ігнорувати", "callback_data": f"ri:{item['id']}"}]]}
        if item["status"] == "publishing" and not self.app.publisher.is_active(item["id"]):
            # зупинена / перервана публікація: кнопки повтору мають бути завжди
            kb = self.retry_keyboard(item["id"])
            if self.has_details(item):
                kb["inline_keyboard"].append([{"text": "🧾 Деталі", "callback_data": f"err:{item['id']}"}])
            return kb
        if item["status"] in ("failed", "posted"):
            det = [{"text": "🧾 Деталі", "callback_data": f"err:{item['id']}"}] if self.has_details(item) else []
            if self.failed_errors(item) or item["status"] == "failed":
                if not self.db.get(f"ignored:{item['id']}"):
                    kb = self.retry_keyboard(item["id"])
                    if det:
                        kb["inline_keyboard"].append(det)
                    return kb
            return {"inline_keyboard": [det]} if det else None
        if item["status"] == "cancelled":
            return {"inline_keyboard": [[{"text": "↩️ Повернути в чергу", "callback_data": f"cr:{item['id']}"}]]} \
                if Path(item["mp4"]).exists() else None
        if item["status"] == "queued":
            i = item["id"]
            plan = {it["id"]: ts for it, ts in self.app.scheduler.projection()}
            rows = []
            if plan.get(i):
                rows.append([{"text": f"✅ Підтвердити на {fmt_slot(self.cfg, plan[i])} (назва, хештеги)",
                              "callback_data": f"qa:{i}"}])
            rows += [[{"text": "🕐 Інший час / ⚡ зараз", "callback_data": f"qt:{i}"}],
                     [{"text": "⬆️ Першим у черзі", "callback_data": f"qf:{i}"},
                      {"text": "🚫 Скасувати", "callback_data": f"qd:{i}"}]]
            return {"inline_keyboard": rows}
        if item["status"] not in ("notified", "approved"):
            return None
        i = item["id"]
        first = ({"text": "🚀 Опублікувати", "callback_data": f"pub:{i}"} if item["status"] == "notified"
                 else {"text": "↩️ Скасувати підтвердження", "callback_data": f"unpub:{i}"})
        toggles = [{"text": f"{SHORT[p]} {'🚫' if p in item['disabled'] else '✅'}",
                    "callback_data": f"tog:{i}:{p}"} for p in enabled_platforms(self.cfg)]
        from app.core import ai
        edit = [{"text": "✏️ Назва", "callback_data": f"title:{i}"}, {"text": "📝 Тексти", "callback_data": f"txt:{i}"}]
        if self.cfg.get("regenerate_command") or ai.enabled(self.cfg):
            edit.append({"text": "🔄 Нові тексти", "callback_data": f"regen:{i}"})
        rows = [[first], [{"text": "🕐 Змінити час / дату", "callback_data": f"time:{i}"}], edit]
        if toggles:
            rows.append([{"text": "Вимкнути платформу:", "callback_data": f"noop:{i}"}])
            rows.append(toggles)
        waiting = len(self.db.items("status='notified'"))
        if item["status"] == "notified" and waiting >= 2:
            rows.append([{"text": f"🚀 Опублікувати всі, що чекають ({waiting})", "callback_data": "pall:0"}])
        rows.append([{"text": "🚫 Скасувати цей шортс", "callback_data": f"cx:{i}"}])
        return {"inline_keyboard": rows}

    def notify_item(self, item_id: int) -> None:
        """Надсилає сам шортс (відеоповідомлення) + тексти всім отримувачам; кнопки — лише адміністраторам."""
        item = self.db.get_item(item_id)
        meta = self._meta(item)
        try:
            q = media.quality(item["mp4"], self.cfg["ffprobe"])
            qline = f"📐 <b>Оригінал:</b> {esc(media.quality_line(q))}\nНа платформи завантажується саме він — без перекодування."
            ph = int((self.cfg.get("telegram") or {}).get("preview_height", 720) or 0)
            if ph and min(q.get("width") or 0, q.get("height") or 0) > ph:
                qline += f"\nℹ️ У цьому чаті — швидка копія {ph}p; на платформи іде оригінал."
            elif q["size_mb"] * 1024 * 1024 > media.TG_LIMIT:
                qline += ("\nℹ️ У цьому чаті — копія, стиснута до 50 МБ (ліміт Telegram для ботів); "
                          "на YouTube/TikTok/Instagram/Facebook іде оригінал.")
        except OSError:
            qline = ""
        try:
            if q.get("width"):
                self.db.set(f"qline:{item_id}", f"{q['width']}×{q['height']} · {q.get('fps') or '?'} к/с · "
                                                f"{q.get('size_mb')} МБ → на платформи оригінал")
        except NameError:  # файл недоступний — без рядка якості
            pass
        separate = bool((self.cfg.get("telegram") or {}).get("texts_message", False))
        caption, kb, texts = self.caption(item, meta), self.keyboard(item), self.texts(item, meta, qline)
        admins = self.admins()
        old = self.db.tg_msgs(item_id)  # картка «нове в черзі» (або попередня) — замінюється новою, щоб не дублювались
        for r in old:
            self._delete(r["chat_id"], [r["msg_id"]] + ([r["text_msg_id"]] if r.get("text_msg_id") else []))
        self.db.clear_tg_msgs(item_id)
        video, info, file_id, video_error = None, {}, None, None
        try:
            video = media.telegram_copy(item["mp4"], self.cfg)
            info = media.probe(video, self.cfg["ffprobe"])
        except (media.MediaError, OSError) as e:
            video_error = e
        delivered_admin = False
        for cid in self.recipients():
            is_admin = cid in admins
            try:
                is_text, msg = 0, None
                if video_error is None:
                    try:
                        msg = self.tg.send_video(cid, file_id or video, caption, kb if is_admin else None,
                                                 width=info.get("width"), height=info.get("height"),
                                                 duration=info.get("duration"))
                        file_id = file_id or ((msg or {}).get("video") or {}).get("file_id")
                    except TelegramError as e:
                        video_error = e
                if msg is None:
                    log.error("Не вдалося надіслати відео #%s: %s", item_id, video_error)
                    msg = self.tg.send_message(cid, caption, reply_markup=kb if is_admin else None)
                    if is_admin:
                        self.say(cid, f"⚠️ Відео #{item_id} не вдалося надіслати в чат: {esc(str(video_error))}")
                    is_text = 1
                text_msg = self.tg.send_message(cid, texts, reply_to=msg["message_id"]) if separate else None
                self.db.set_tg_msg(item_id, cid, msg["message_id"], (text_msg or {}).get("message_id"), is_text)
                if is_admin:
                    delivered_admin = True
                    if cid == self.chat_id:
                        self.db.update_item(item_id, tg_msg=msg["message_id"], tg_msg_is_text=is_text,
                                            tg_text_msg=(text_msg or {}).get("message_id"))
            except TelegramError as e:
                log.error("Сповіщення #%s для %s не доставлено: %s", item_id, cid, e)
        if not delivered_admin:
            raise TelegramError("сповіщення не доставлено жодному адміністратору")

    def refresh(self, item_id: int, texts: bool = False) -> None:
        item = self.db.get_item(item_id)
        if not item:
            return
        rows = self.db.tg_msgs(item_id)
        if not rows:
            return
        if not (self.cfg.get("telegram") or {}).get("texts_message", False) and any(r.get("text_msg_id") for r in rows):
            self.drop_all_texts(item_id)  # старі окремі «📝 Тексти» прибираємо — лишається тільки відео
            rows = self.db.tg_msgs(item_id)
            texts = False
        meta = self._meta(item)
        cap, kb = self.caption(item, meta), self.keyboard(item)
        txt = self.texts(item, meta) if texts else None
        admins = set(self.admins())
        for r in rows:
            markup = kb if r["chat_id"] in admins else None
            digest = self._cap_digest(cap, markup)
            try:
                if r["is_text"]:
                    self.tg.edit_text(r["chat_id"], r["msg_id"], cap, markup)
                else:
                    self.tg.edit_caption(r["chat_id"], r["msg_id"], cap, markup)
                if txt and r["text_msg_id"]:
                    self.tg.edit_text(r["chat_id"], r["text_msg_id"], txt)
            except Exception as e:  # noqa: BLE001 — немає інтернету тощо: доробимо пізніше (heal)
                if "not modified" not in str(e).lower():
                    log.warning("Не вдалося оновити повідомлення #%s у %s: %s — повторю, щойно буде зв'язок",
                                item_id, r["chat_id"], e)
                    continue
            self.db.set(f"cap:{item_id}:{r['chat_id']}", digest)  # що саме зараз показано в чаті

    @staticmethod
    def _cap_digest(cap: str, kb) -> str:
        import hashlib
        return hashlib.md5((cap + json.dumps(kb, ensure_ascii=False, sort_keys=True)).encode()).hexdigest()[:16]

    def refresh_publishing(self) -> None:
        """Картки шортсів, що публікуються, — одразу оновити (після «Стоп» / «Відновити» / перезапуску)."""
        for it in self.db.items("status='publishing'"):
            try:
                self.refresh(it["id"])
            except Exception as e:  # noqa: BLE001
                log.debug("refresh #%s: %s", it["id"], e)

    def heal(self, limit: int = 10) -> int:
        """Самовідновлення: підписи відео, які не вдалося оновити (світло/інтернет), доводимо до актуального стану."""
        fixed = 0
        since = now_ts() - 3 * 86400
        admins = set(self.admins())
        for item in self.db.items("id IN (SELECT item_id FROM tg_msgs) AND (status IN "
                                  "('notified','approved','publishing','failed','queued') OR posted_at > ?)",
                                  (since,), order="id DESC"):
            meta = self._meta(item)
            cap, kb = self.caption(item, meta), self.keyboard(item)
            stale = any(self.db.get(f"cap:{item['id']}:{r['chat_id']}") !=
                        self._cap_digest(cap, kb if r["chat_id"] in admins else None)
                        for r in self.db.tg_msgs(item["id"]))
            if stale:
                self.refresh(item["id"])
                fixed += 1
                if fixed >= limit:
                    break
        fixed += self.upgrade_text_cards()
        if (self.cfg.get("telegram") or {}).get("queue_cards", True):
            today = self.today_queue_ids()
            # картки з відео — лише для тих, що вийдуть сьогодні; решта — списком у «📋 Черга»
            for it in self.db.items("status='queued' AND id IN (SELECT item_id FROM tg_msgs)"):
                if it["id"] not in today:
                    for r in self.db.tg_msgs(it["id"]):
                        self._delete(r["chat_id"], [r["msg_id"]])
                    self.db.clear_tg_msgs(it["id"])
            missing = [i for i in today if not self.db.tg_msgs(i) and not self.db.get(f"noqcard:{i}")]
            if missing:
                fixed += self.queue_cards(missing, limit=3)
        return fixed

    def upgrade_text_cards(self, limit: int = 2) -> int:
        """Картка без відео (відео тоді не вдалося надіслати — світло, інтернет, збій стискання):
        щойно можна — надсилаємо картку з відео, а текстову прибираємо. Спроби — не частіше ніж раз на 30 хв."""
        done = 0
        admins = set(self.admins())
        for r in self.db.q("SELECT * FROM tg_msgs WHERE is_text=1 ORDER BY item_id DESC LIMIT 20"):
            item = self.db.get_item(r["item_id"])
            key = f"upgrade_try:{r['item_id']}:{r['chat_id']}"
            if not item or not Path(item["mp4"]).exists() or now_ts() - int(self.db.get(key) or 0) < 1800:
                continue
            self.db.set(key, now_ts())
            try:
                video = media.telegram_copy(item["mp4"], self.cfg)
                info = media.probe(video, self.cfg["ffprobe"])
                cap, kb = self.caption(item), self.keyboard(item)
                markup = kb if r["chat_id"] in admins else None
                msg = self.tg.send_video(r["chat_id"], video, cap, markup, width=info.get("width"),
                                         height=info.get("height"), duration=info.get("duration"))
            except Exception as e:  # noqa: BLE001
                log.info("Картка #%s досі без відео: %s — спробую пізніше", r["item_id"], e)
                continue
            self._delete(r["chat_id"], [r["msg_id"]])
            self.db.set_tg_msg(r["item_id"], r["chat_id"], msg["message_id"], r.get("text_msg_id"), 0)
            self.db.set(f"cap:{r['item_id']}:{r['chat_id']}", self._cap_digest(cap, markup))
            self.db.set(key, None)
            log.info("Картку #%s замінено на картку з відео", r["item_id"])
            done += 1
            if done >= limit:
                break
        return done

    def _close_msg(self, chat: int, msg_id: int | None, note: str, item_id: int | None = None,
                   running: bool = False) -> None:
        """Окреме повідомлення про збій після дії з ним.
        Якщо в чаті є відео цього шортса (статус видно в його підписі) — повідомлення прибираємо.
        Якщо дія триває (повтор зараз) — воно висить як «🔁 Повторюю…» і зникне, щойно публікація завершиться."""
        if not msg_id:
            return
        own = self.db.q("SELECT 1 FROM tg_msgs WHERE chat_id=? AND msg_id=?", (chat, msg_id))
        if own:
            return  # це повідомлення з відео — його оновить refresh()
        has_card = bool(item_id and self.db.tg_msg(item_id, chat))
        if running and item_id:
            self._remember_kept(chat, msg_id, key=f"tasks:{chat}")  # не зникне від інших кнопок, поки триває
            key = f"itemmsg:{item_id}"
            try:
                rows = json.loads(self.db.get(key) or "[]")
            except ValueError:
                rows = []
            self.db.set(key, json.dumps(rows + [[int(chat), int(msg_id)]]))
        elif has_card:
            return self._delete(chat, [msg_id])
        else:
            self.ephemeral(chat, msg_id)
        try:
            self.tg.call("editMessageReplyMarkup", chat_id=chat, message_id=msg_id,
                         reply_markup={"inline_keyboard": [[{"text": note, "callback_data": "noop:0"}]]})
        except Exception as e:  # noqa: BLE001
            log.debug("close msg: %s", e)

    def finish_item_msgs(self, item_id: int) -> None:
        """Публікацію завершено: «🔁 Повторюю…» прибираємо — результат видно в підписі відео
        (або, якщо відео в чаті немає, це повідомлення стає результатом)."""
        key = f"itemmsg:{item_id}"
        try:
            rows = json.loads(self.db.get(key) or "[]")
        except ValueError:
            rows = []
        self.db.set(key, None)
        item = self.db.get_item(item_id)
        for chat, mid in rows:
            try:
                ids = [i for i in json.loads(self.db.get(f"tasks:{chat}") or "[]") if i != mid]
                self.db.set(f"tasks:{chat}", json.dumps(ids))
            except ValueError:
                pass
            if self.db.tg_msg(item_id, chat) or not item:
                self._delete(chat, [mid])
                continue
            if self.failed_errors(item) or item["status"] == "failed":
                text, kb = self.failed_text(item), self.retry_keyboard(item_id)
            else:
                text, kb = f"✅ Опубліковано #{item_id}" + self._results(item), None
            try:
                self.tg.edit_text(chat, mid, text, kb)
                self.ephemeral(chat, mid)
            except TelegramError:
                self._delete(chat, [mid])

    def _item_card(self, chat: int, item: dict, kb: dict, prefix: str = "") -> None:
        """Картка шортса з самим відео. Що б не сталося — користувач отримає хоча б текст із кнопками."""
        try:
            self._item_card_inner(chat, item, kb, prefix)
        except Exception as e:  # noqa: BLE001
            log.exception("Картка #%s", item.get("id"))
            errs = "; ".join(f"{NAMES.get(p, p)}: {str(r.get('error', ''))[:150]}"
                             for p, r in (item.get("results") or {}).items() if r.get("status") != "ok")
            text = (f"{prefix}⚠️ Шортс #{item.get('id')} ({Path(item.get('mp4') or '').stem})\n"
                    f"{errs or 'причина невідома'}\n(картку з відео не вдалося показати: {str(e)[:150]})")
            try:
                self.tg.send_message(chat, text, reply_markup=kb, html=False)
            except Exception as e2:  # noqa: BLE001
                log.error("Навіть текстову картку #%s не надіслано: %s", item.get("id"), e2)

    def _item_card_inner(self, chat: int, item: dict, kb: dict, prefix: str = "") -> None:
        cap = ""
        for n in (300, 150, 80, 40):
            cap = prefix + self.failed_text(item, err_len=n)
            if visible_len(cap) <= CAPTION_LIMIT:
                break
        if visible_len(cap) > CAPTION_LIMIT:
            cap = cap[:CAPTION_LIMIT - 1] + "…"
            cap = re.sub(r"<[^>]*$", "", cap)
        path = item["mp4"]
        if not Path(path).exists():
            return self.say(chat, cap + "\n<i>(файл відео не знайдено)</i>", reply_markup=kb)
        self.typing(chat)
        try:
            video = media.telegram_copy(path, self.cfg)
            info = media.probe(video, self.cfg["ffprobe"])
            self.tg.send_video(chat, video, cap, kb, width=info.get("width"), height=info.get("height"),
                               duration=info.get("duration"))
        except (media.MediaError, TelegramError, OSError) as e:
            log.warning("Картка #%s без відео: %s", item["id"], e)
            self.say(chat, cap, reply_markup=kb)
