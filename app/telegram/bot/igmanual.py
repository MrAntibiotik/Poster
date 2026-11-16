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
        caption = (f"📸 <b>Для Instagram</b> · #{item_id}\n"
                   "1) Утримайте відео → «Поділитися» → Instagram → Reels\n"
                   "2) Скопіюйте підпис з наступного повідомлення (дотик по тексту) і вставте\n"
                   "3) Після публікації — «✅ Опубліковано»")
        text = meta.instagram_text or meta.yt_title
        sent = []
        for cid in self.admins():
            try:
                v = self.tg.send_video(cid, path, caption, kb)
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
        results["instagram"] = ({"status": "ok", "url": "", "note": "опубліковано вручну з телефона",
                                 "published_at": now_ts()} if done else
                                {"status": "skipped", "note": "Instagram пропущено вручну"})
        self.db.update_item(item_id, results=results)
        self.db.event(item_id, "instagram_manual", {"done": done, "by": chat})
        try:
            for cid, v, t in json.loads(self.db.get(f"igman:{item_id}") or "[]"):
                self._delete(cid, [v, t])
        except (ValueError, TypeError):
            pass
        self.db.set(f"igman:{item_id}", None)
        answer("✅ Позначено: опубліковано в Instagram" if done else "⏭ Instagram для цього шортса пропущено")
        self.refresh(item_id)
