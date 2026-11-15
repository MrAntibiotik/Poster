"""Діалог назви/хештегів/привʼю перед публікацією, перегенерація AI."""
from __future__ import annotations

import json
import re
from app.core import media
from app.core.meta import apply_manual
from app.core.meta import load_meta
from app.core.meta import parse_hashtags
from pathlib import Path
from .common import (
    esc,
    log,
)


class DialogMixin:
    def dialog_get(self, chat: int) -> dict | None:
        raw = self.db.get(f"dialog:{chat}")
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def dialog_set(self, chat: int, state: dict | None) -> None:
        self.db.set(f"dialog:{chat}", json.dumps(state, ensure_ascii=False) if state else None)

    def dialog_start(self, chat: int, item_id: int, preset=None, bulk: list | None = None) -> None:
        item = self.db.get_item(item_id)
        meta = self._raw_meta(item)
        self.dialog_set(chat, {"item": item_id, "step": "title", "preset": preset, "bulk": bulk or []})
        kb = {"inline_keyboard": [
            [{"text": f"Залишити: {meta.yt_title[:40]}", "callback_data": f"keep:{item_id}:title"}],
            [{"text": "❌ Скасувати", "callback_data": f"dlgx:{item_id}"}]]}
        always = self._always_line()
        note = f"\nℹ️ У кінець назви автоматично додаються: {esc(always)}" if always else ""
        left = f" · ще {len(bulk)} після нього" if bulk else ""
        self.tmp(chat, f"✍️ <b>Крок 1/2.</b> Надішліть <b>назву</b> для шортса #{item_id}{left}.\n"
                       f"Зараз: <i>{esc(meta.yt_title)}</i>{note}", reply_markup=kb)

    def _after_title(self, chat: int, item: dict, st: dict, title: str) -> None:
        """Після назви: шортс — хештеги; хайлайт — прев'ю (картинка для YouTube)."""
        if item.get("kind") == "highlight":
            self.dialog_set(chat, {**st, "step": "thumb", "title": title})
            return self._ask_thumb(chat, item["id"])
        self.dialog_set(chat, {**st, "step": "tags", "title": title})
        return self._ask_tags(chat, item["id"])

    def _ask_thumb(self, chat: int, item_id: int) -> None:
        rows = []
        if self.db.get(f"thumb:{item_id}") and Path(self.db.get(f"thumb:{item_id}")).exists():
            rows.append([{"text": "✅ Залишити поточне прев'ю", "callback_data": f"keep:{item_id}:thumb"}])
        rows.append([{"text": "Без прев'ю (YouTube вибере кадр сам)", "callback_data": f"keep:{item_id}:thumb:none"}])
        rows.append([{"text": "❌ Скасувати", "callback_data": f"dlgx:{item_id}"}])
        self.tmp(chat, "🖼 <b>Крок 2/2.</b> Надішліть <b>прев'ю</b> для хайлайта — картинку 16:9 "
                       "(фото або файлом JPG/PNG, краще 1280×720).", reply_markup={"inline_keyboard": rows})

    def save_thumb(self, chat: int, m: dict) -> None:
        """Картинка прев'ю з чату → data/thumbs/<id>.jpg (≤2 МБ, як вимагає YouTube) → далі вибір часу."""
        st = self.dialog_get(chat) or {}
        item = self.db.get_item(st.get("item") or 0)
        if not item:
            return
        photo = (m.get("photo") or [])[-1:] or []
        doc = m.get("document") or {}
        file_id = photo[0]["file_id"] if photo else (doc.get("file_id") if str(doc.get("mime_type", "")).startswith("image/") else None)
        if not file_id:
            return self.tmp(chat, "Це не картинка. Надішліть фото або файл JPG/PNG.", html_mode=False)
        self._track(chat, m.get("message_id"))
        folder = Path(self.cfg["paths"]["data"]) / "thumbs"
        raw = folder / f"{item['id']}_src"
        out = folder / f"{item['id']}.jpg"
        try:
            self.tg.download(file_id, raw)
            r = media.run_hidden([self.cfg["ffmpeg"], "-y", "-hide_banner", "-loglevel", "error", "-i", str(raw),
                                  "-vf", "scale='min(1280,iw)':-2", "-q:v", "3", str(out)], timeout=60)
            if r.returncode != 0 or not out.exists():
                raise RuntimeError(r.stderr.decode(errors="ignore")[-200:])
            if out.stat().st_size > 2 * 1024 * 1024:  # YouTube приймає прев'ю до 2 МБ
                media.run_hidden([self.cfg["ffmpeg"], "-y", "-loglevel", "error", "-i", str(raw), "-vf",
                                  "scale='min(1280,iw)':-2", "-q:v", "8", str(out)], timeout=60)
        except Exception as e:  # noqa: BLE001
            log.warning("Прев'ю #%s: %s", item["id"], e)
            return self.tmp(chat, f"⚠️ Не вдалося обробити картинку: {e}. Спробуйте ще раз.", html_mode=False)
        finally:
            try:
                raw.unlink()
            except OSError:
                pass
        self.db.set(f"thumb:{item['id']}", str(out))
        self.tmp(chat, "🖼 Прев'ю збережено — його буде встановлено на YouTube.", html_mode=False)
        self._dialog_finish(chat, item, st.get("title"), self._raw_meta(item).hashtags)

    def _ask_tags(self, chat: int, item_id: int) -> None:
        meta = self._raw_meta(self.db.get_item(item_id))
        always = {t.lower() for t in (self._always_line().split())}
        current = " ".join(t for t in meta.hashtags if t.lower() not in always) or "—"
        kb = {"inline_keyboard": [
            [{"text": f"Залишити: {current[:40]}", "callback_data": f"keep:{item_id}:tags"}],
            [{"text": "❌ Скасувати", "callback_data": f"dlgx:{item_id}"}]]}
        self.tmp(chat, "#️⃣ <b>Крок 2/2.</b> Надішліть <b>хештеги</b> через пробіл — можна з # або без "
                       "(напр.: <code>wot танки shorts</code>). «-» — без хештегів.", reply_markup=kb)

    def _dialog_item(self, chat: int, item_id: int, allow_approved: bool = False,
                     allow_queued: bool = False) -> dict | None:
        item = self.db.get_item(item_id)
        ok = ("queued",) if allow_queued else ("notified", "approved") if allow_approved else ("notified",)
        if not item or item["status"] not in ok:
            self.dialog_set(chat, None)
            self.tmp(chat, "Цей шортс уже неактуальний (слот минув або його опубліковано).", html_mode=False)
            return None
        return item

    def dialog_answer(self, chat: int, text: str) -> None:
        st = self.dialog_get(chat)
        if st.get("mode") == "retry":
            item = self.db.get_item(st["item"])
            if not item or item["status"] not in ("failed", "posted"):
                self.dialog_set(chat, None)
                return self.cleanup(chat)
        else:
            item = self._dialog_item(chat, st["item"], allow_approved=st.get("mode") == "change",
                                     allow_queued=st.get("mode") == "queue")
        if not item:
            return
        if st["step"] in ("time", "time_custom"):
            return self._time_typed(chat, item, st, text)
        if st["step"] == "title":
            title = re.sub(r"\s+", " ", text).strip()[:100]
            if not title:
                return self.tmp(chat, "Назва порожня — надішліть текст.", html_mode=False)
            return self._after_title(chat, item, st, title)
        if st["step"] == "thumb":
            if text.strip() in ("-", "—"):
                return self._dialog_finish(chat, item, st.get("title"), self._raw_meta(item).hashtags)
            return self.tmp(chat, "Надішліть картинку прев'ю (фото або файл JPG/PNG) або «-» — без прев'ю.",
                            html_mode=False)
        tags = parse_hashtags(text)
        if text.strip() not in ("-", "—") and not tags:
            return self.tmp(chat, "Не бачу хештегів — надішліть слова через пробіл або «-».", html_mode=False)
        if st["step"] == "tags":
            return self._dialog_finish(chat, item, st.get("title"), tags)

    def dialog_button(self, chat: int, item_id: int, action: str, rest: list, answer) -> None:
        st = self.dialog_get(chat)
        if action == "dlgx":
            self.dialog_set(chat, None)
            answer("Скасовано. Шортс чекає на «🚀 Опублікувати» до свого слоту.", True)
            return self.cleanup(chat)
        if not st or st.get("item") != item_id:
            return answer("Цей запит уже неактуальний", True)
        item = self._dialog_item(chat, item_id)
        if not item:
            return answer()
        meta = self._raw_meta(item)
        if rest and rest[0] == "title" and st["step"] == "title":
            answer("Назву залишено")
            return self._after_title(chat, item, st, meta.yt_title)
        if rest and rest[0] == "thumb" and st["step"] == "thumb":
            if len(rest) > 1 and rest[1] == "none":
                self.db.set(f"thumb:{item_id}", None)
            answer("Без нового прев'ю" if len(rest) > 1 and rest[1] == "none" else "Прев'ю залишено")
            return self._dialog_finish(chat, item, st.get("title"), meta.hashtags)
        if rest and rest[0] == "tags" and st["step"] == "tags":
            answer("Хештеги залишено")
            return self._dialog_finish(chat, item, st.get("title"), meta.hashtags)
        answer()

    def _dialog_finish(self, chat: int, item: dict, title: str | None, tags: list[str]) -> None:
        item_id = item["id"]
        title = title or self._raw_meta(item).yt_title
        try:
            apply_manual(item["json"], title, tags)
        except Exception as e:  # noqa: BLE001
            log.error("Не вдалося записати тексти #%s: %s", item_id, e)
            return self.tmp(chat, f"❌ Не вдалося зберегти тексти: {e}", html_mode=False)
        edited = [x for x in item["edited"] if x not in ("назву", "тексти")] + ["назву", "хештеги"]
        self.db.update_item(item_id, title_override=None, edited=edited)
        if len(tags) > 15:
            self.tmp(chat, "⚠️ YouTube ігнорує всі хештеги, якщо їх більше 15.", html_mode=False)
        prev = self.dialog_get(chat) or {}
        preset = prev.get("preset")
        if preset == "bulk":  # «опублікувати всі»: цей — у чергу публікації, далі — наступний шортс
            rest = [i for i in prev.get("bulk") or []
                    if (self.db.get_item(i) or {}).get("status") == "notified"]
            self.dialog_set(chat, None)
            self.refresh(item_id, texts=True)
            self._queue_bulk([item_id], by=chat)
            if rest:
                return self.dialog_start(chat, rest[0], preset="bulk", bulk=rest[1:])
            return self.cleanup(chat)
        st = {"item": item_id, "step": "time", "mode": "dialog", "title": title, "tags": tags}
        self.dialog_set(chat, st)
        self.refresh(item_id, texts=True)
        if preset:  # час уже обрали з черги
            return self._apply_time(chat, self.db.get_item(item_id), st, "now" if preset == "now" else None)
        self._ask_time(chat, item_id, dialog=True)

    def _regen_ai(self, item_id: int) -> None:
        from app.core import ai
        item = self.db.get_item(item_id)
        try:
            result = ai.generate(self.cfg, item["mp4"], item["game"])
            ai.apply_to_json(item["json"], result)
        except Exception as e:  # noqa: BLE001
            log.error("ШІ-перегенерація #%s: %s", item_id, e)
            self.send(f"❌ ШІ не змогло перегенерувати тексти #{item_id}: {e}")
            return
        item = self.db.get_item(item_id)
        edited = [x for x in item["edited"] if x not in ("назву", "тексти")] + ["тексти"]
        self.db.update_item(item_id, title_override=None, edited=edited)
        self.db.event(item_id, "edited", "regen-ai")
        self.refresh(item_id, texts=True)

    def _regen(self, item_id: int) -> None:
        item = self.db.get_item(item_id)
        args = [str(a).format(mp4=item["mp4"], json=item["json"], game=item["game"])
                for a in self.cfg["regenerate_command"]]
        log.info("Перегенерація текстів #%s: %s", item_id, args)
        try:
            r = media.run_hidden(args, timeout=600, cwd=self.cfg.get("regenerate_cwd") or None)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout or b"").decode(errors="ignore")[-400:] or f"код {r.returncode}")
            load_meta(item["json"])
        except Exception as e:  # noqa: BLE001
            log.error("Перегенерація #%s не вдалася: %s", item_id, e)
            self.send(f"❌ Перегенерація текстів #{item_id} не вдалася: {e}")
            return
        item = self.db.get_item(item_id)
        edited = [x for x in item["edited"] if x not in ("назву", "тексти")] + ["тексти"]
        self.db.update_item(item_id, title_override=None, edited=edited)
        self.db.event(item_id, "edited", "regen")
        self.refresh(item_id, texts=True)
