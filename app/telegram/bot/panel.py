"""Виконання дій, що надходять із вебпанелі."""
from __future__ import annotations

import json
import re
import threading
import time
from app.core.config import NAMES
from app.core.config import PLATFORMS
from app.core.config import fmt_when
from app.core.meta import apply_manual
from app.core.meta import parse_hashtags
from .common import (
    log,
)


class PanelMixin:
    def start_panel_worker(self) -> None:
        def loop():
            while True:
                try:
                    for row in self.db.q("SELECT key, value FROM kv WHERE key LIKE 'pcmd:%'"):
                        self.db.x("DELETE FROM kv WHERE key=?", (row["key"],))
                        threading.Thread(target=self._panel_exec, args=(row["key"][5:], row["value"]),
                                         daemon=True, name="panel-cmd").start()
                except Exception as e:  # noqa: BLE001
                    log.warning("Команди з панелі: %s", e)
                time.sleep(1)
        threading.Thread(target=loop, daemon=True, name="panel-worker").start()

    def _panel_exec(self, cid: str, raw: str) -> None:
        try:
            cmd = json.loads(raw or "{}")
            res = {"ok": True, "message": self.panel_do(cmd)}
        except Exception as e:  # noqa: BLE001
            if not isinstance(e, ValueError):
                log.exception("Команда з панелі %s", raw)
            res = {"ok": False, "message": str(e)}
        self.db.set(f"pres:{cid}", json.dumps(res, ensure_ascii=False))

    def _panel_texts(self, item: dict, cmd: dict) -> bool:
        title, tags = cmd.get("title"), cmd.get("tags")
        if title is None and tags is None:
            return False
        raw = self._raw_meta(item)
        title = re.sub(r"\s+", " ", str(title or "")).strip()[:100] or raw.yt_title
        if tags is None:
            tag_list = raw.hashtags
        else:
            always = {t.lower() for t in self._always_line().split()}
            tag_list = [t for t in parse_hashtags(str(tags)) if t.lower() not in always]
        if title == raw.yt_title and tag_list == [t for t in raw.hashtags]:
            return False
        apply_manual(item["json"], title, tag_list)
        edited = [x for x in item["edited"] if x not in ("назву", "тексти")] + ["назву", "хештеги"]
        self.db.update_item(item["id"], title_override=None, edited=sorted(set(edited), key=edited.index))
        self.db.event(item["id"], "edited", {"by": "panel", "title": title, "hashtags": tag_list})
        return True

    def panel_do(self, cmd: dict) -> str:
        """Дія з панелі. Повертає текст для користувача; ValueError — зрозуміла помилка."""
        op = cmd.get("op")
        app = self.app
        if op == "update":
            text, kb = self.update_now(force=bool(cmd.get("force")))
            if kb and "upd:" in str(kb):  # йде публікація — панель покаже питання
                raise ValueError(text + " Натисніть «⬆️ Оновити» ще раз, щоб оновитись усе одно.")
            return text
        if op == "switch":
            return self.switch_version(str(cmd.get("version") or ""))
        if op == "publish_all":
            return self.publish_all(by="panel")
        if op == "details":
            return self.details_text(int(cmd.get("id") or 0))
        if op in ("report", "stats", "top"):
            from app.insights.reports import collect, daily_report, weekly_top
            if op == "report":
                text = daily_report(app)
            else:
                stats = collect(app)
                if op == "stats":
                    from app.insights import analytics
                    text = analytics.period_report(self.cfg, self.db, 1, list(app.platforms))
                else:
                    text = weekly_top(app, stats)
            if cmd.get("send"):
                self.send(text, html_mode=True)
            return text
        item_id = int(cmd.get("id") or 0)
        item = self.db.get_item(item_id)
        if not item:
            raise ValueError("Шортс не знайдено")
        st = item["status"]
        if op == "cancel":
            app.scheduler.cancel(item_id, by="panel")
            return f"🚫 #{item_id} скасовано — файл у папці «скасовані», наступний шортс стає на цей слот"
        if op == "restore":
            app.scheduler.restore(item_id)
            return f"↩️ #{item_id} повернуто — перший у черзі"
        if op in ("first", "remove") and st != "queued":
            raise ValueError("Цього відео вже немає в черзі")
        if op == "first":
            self.db.update_item(item_id, qpos=self.db.first_qpos())
            return f"⬆️ #{item_id} першим у черзі"
        if op == "remove":
            app.scheduler.cancel(item_id, by="panel")
            return f"🚫 #{item_id} скасовано — файл у папці «скасовані»"
        if st not in ("queued", "notified", "approved"):
            raise ValueError("Шортс уже публікується або опубліковано — оновіть сторінку")
        if app.publisher.is_active(item_id):
            raise ValueError("Шортс уже публікується")
        if op == "toggle":
            p = cmd.get("platform")
            if p not in PLATFORMS:
                raise ValueError("Невідома платформа")
            disabled = [x for x in item["disabled"] if x != p] if p in item["disabled"] else item["disabled"] + [p]
            self.db.update_item(item_id, disabled=disabled)
            self.refresh(item_id)
            return f"{NAMES[p]}: {'вимкнено' if p in disabled else 'увімкнено'} для #{item_id}"
        if op == "unapprove":
            if st == "approved":
                self.db.update_item(item_id, status="notified")
                self.refresh(item_id)
            return "Підтвердження скасовано — шортс чекає на підтвердження"
        if op == "regen":
            from app.core import ai
            if self.cfg.get("regenerate_command"):
                self._regen(item_id)
            elif ai.enabled(self.cfg):
                self._regen_ai(item_id)
            else:
                raise ValueError("Перегенерація недоступна: підключіть ШІ (вкладка «🤖 ШІ») або команду перегенерації")
            return "Тексти перегенеровано"
        if op not in ("publish", "save"):
            raise ValueError(f"Невідома дія {op}")

        when = cmd.get("when", "keep")
        if when not in ("keep", "now"):
            try:
                when = int(when)
            except (TypeError, ValueError):
                raise ValueError("Неправильний час") from None
        if st == "queued" and when == "keep" and op == "publish":
            raise ValueError("Оберіть час публікації або «Зараз»")
        if op == "save" and when == "now":
            when = "keep"
        if not app.publisher.targets(item) and op == "publish":
            raise ValueError("Усі платформи вимкнено — нічого публікувати")
        changed = self._panel_texts(item, cmd)
        if isinstance(when, int):
            app.scheduler.move_slot(item_id, when)
        msg = []
        if st == "queued" and when != "keep":
            self.db.update_item(item_id, status="notified", note=None)
            self.db.event(item_id, "picked", {"by": "panel", "when": when})
            try:
                self.notify_item(item_id)  # копія в Telegram — як звичайне сповіщення
            except Exception as e:  # noqa: BLE001
                log.warning("Сповіщення #%s: %s", item_id, e)
        item = self.db.get_item(item_id)
        if op == "publish":
            if item["status"] == "notified":
                self.db.update_item(item_id, status="approved")
                self.db.event(item_id, "approved", {"by": "panel"})
            if when == "now":  # у панелі час обрано явно — «залишити» означає «в цей слот», а не «зараз»
                app.scheduler.release_slot(item_id)
                app.publisher.start(item_id)
                return f"🚀 #{item_id} публікується зараз"
            msg.append(f"✅ #{item_id} підтверджено — публікація {fmt_when(self.cfg, self.db.get_item(item_id)['slot_at'])}")
        else:
            if changed:
                msg.append("Тексти збережено")
            if isinstance(when, int):
                msg.append(f"час: {fmt_when(self.cfg, when)}")
            msg = [", ".join(msg) or "Без змін"]
        self.refresh(item_id, texts=True)
        return "; ".join(msg)
