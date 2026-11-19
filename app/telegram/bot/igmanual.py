"""Instagram вручну (напівавтомат): поки Meta не приймає довгі відео через API, бот надсилає вам
готове відео й підпис — ви публікуєте з телефона («Поділитися → Instagram») і тиснете «✅ Опубліковано»."""
from __future__ import annotations

import json

from app.core import media
from app.core.config import now_ts
from app.telegram.api import TelegramError
from .common import esc, log

TG_BOT_LIMIT_MB = 47   # бот Telegram надсилає файли до 50 МБ


class IgManualMixin:
    def instagram_manual(self, item_id: int, meta) -> None:
        item = self.db.get_item(item_id)
        cfg = {**self.cfg, "platforms": {**self.cfg["platforms"], "instagram": {
            **(self.cfg["platforms"].get("instagram") or {}), "copy_target_mb": TG_BOT_LIMIT_MB}}}
        path = media.instagram_copy(item["mp4"], cfg)        # 1080×1920, ≤ 59,5 с, ≤ 47 МБ — бот може надіслати
        kb = {"inline_keyboard": [[{"text": "✅ Опубліковано в Instagram", "callback_data": f"igdone:{item_id}"}],
                                  [{"text": "⏭ Пропустити Instagram", "callback_data": f"igskip:{item_id}"}]]}
        caption = (f"📸 <b>Для Instagram</b> · #{item_id} · вертикальне 1080×1920, оригінальна якість файлу\n"
                   "1) Утримайте відео → «Поділитися» → Instagram → Reels\n"
                   "2) Скопіюйте підпис з наступного повідомлення (дотик по тексту) і вставте\n"
                   "3) Після публікації — «✅ Опубліковано»")
        text = meta.instagram_text or meta.yt_title
        info = media.probe(path, self.cfg["ffprobe"])   # розміри кадру — інакше Telegram показує квадрат/обрізає
        sent, file_id = [], None
        for cid in self.admins():
            try:
                v = self.tg.send_video(cid, file_id or path, caption, kb, width=info.get("width"),
                                       height=info.get("height"), duration=info.get("duration"))
                file_id = file_id or ((v.get("video") or {}).get("file_id"))   # інші адміни — той самий файл
                t = self.tg.send_message(cid, f"<code>{esc(text)}</code>")
                sent.append([cid, v["message_id"], t["message_id"]])
            except TelegramError as e:
                log.warning("Instagram вручну #%s → %s: %s", item_id, cid, e)
        if not sent:
            raise RuntimeError("Telegram не прийняв відео")
        self.db.set(f"igman:{item_id}", json.dumps(sent))
        log.info("#%s: відео й підпис для Instagram надіслано в Telegram (публікація вручну)", item_id)

    def instagram_manual_done(self, chat: int, item_id: int, done: bool, answer) -> None:
        item = self.db.get_item(item_id)
        if not item:
            return answer("Шортс не знайдено", True)
        results = dict(item["results"])
        prev = results.get("instagram") or {}
        results["instagram"] = ({"status": "ok", "url": "", "note": "опубліковано вручну з телефона",
                                 "published_at": now_ts(),
                                 **({"sent_at": prev["sent_at"]} if prev.get("sent_at") else {})} if done else
                                {"status": "skipped", "note": "Instagram пропущено вручну"})
        self.db.update_item(item_id, results=results)
        self.db.event(item_id, "instagram_manual", {"done": done, "by": chat})
        from app.insights import history
        history.record(self.app, item, "instagram", "manual_done" if done else "skipped")
        try:
            for cid, v, t in json.loads(self.db.get(f"igman:{item_id}") or "[]"):
                self._delete(cid, [v, t])
        except (ValueError, TypeError):
            pass
        self.db.set(f"igman:{item_id}", None)
        answer("✅ Позначено: опубліковано в Instagram" if done else "⏭ Instagram для цього шортса пропущено")
        self.refresh(item_id)

    def instagram_send_now(self, chat: int, item_id: int, answer) -> None:
        """Кнопка «📱 Надіслати мені для Instagram» під невдалим шортсом — незалежно від налаштування режиму."""
        item = self.db.get_item(item_id)
        if not item:
            return answer("Шортс не знайдено", True)
        from pathlib import Path
        if not Path(item["mp4"]).exists():
            return answer("Файлу шортса вже немає на диску", True)
        answer("📱 Готую відео для Instagram…")

        def work():
            from app.core.meta import apply_always_hashtags, load_meta
            try:
                meta = apply_always_hashtags(load_meta(item["json"], item["title_override"]), self.cfg)
                self.instagram_manual(item_id, meta)
                results = dict(self.db.get_item(item_id)["results"])
                results["instagram"] = {"status": "manual", "sent_at": now_ts(),
                                        "note": "надіслано в Telegram для публікації з телефона"}
                self.db.update_item(item_id, results=results)
                self.drop_failed_card(item_id)
                self.refresh(item_id)
            except Exception as e:  # noqa: BLE001
                log.warning("Instagram вручну #%s: %s", item_id, e)
                self.say(chat, f"⚠️ Не вдалося надіслати відео для Instagram: {e}", html_mode=False)
        self._bg(work)
