"""Історія версій у боті: список версій з описом змін і перехід (відкат) на будь-яку з них."""
from __future__ import annotations

from app.telegram.api import TelegramError
from .common import esc, log

VERSIONS_SHOWN = 8


class VersionsMixin:
    def versions_text(self) -> tuple[str, dict]:
        from app.core.config import VERSION
        from app.system import updater
        vl = updater.version_list(self.cfg)
        error = next((v["error"] for v in vl if "error" in v), "")
        vl = [v for v in vl if "version" in v][:VERSIONS_SHOWN]
        self._versions = [v["version"] for v in vl]
        pinned = (self.cfg.get("updates") or {}).get("pinned")
        lines = [f"🕘 <b>Версії програми</b> · зараз <b>{esc(VERSION)}</b>"]
        if pinned:
            lines.append("⏸ Автооновлення на паузі після відкату — «⬆️ Оновити» поверне останню версію.")
        lines.append("")
        rows, row = [], []
        for i, v in enumerate(vl):
            mark = "▶️" if v["current"] else ("💾" if v["local"] else "☁️")
            note = "; ".join(v["notes"][:2]) or "—"
            lines.append(f"{mark} <b>{esc(v['version'])}</b> — {esc(note[:160])}")
            if not v["current"]:
                newer = updater.version_key(v["version"]) > updater.version_key(VERSION)
                row.append({"text": ("⬆️ " if newer else "↩️ ") + v["version"].split(".", 1)[1],
                            "callback_data": f"ver:{i}"})
                if len(row) == 2:
                    rows.append(row)
                    row = []
        if row:
            rows.append(row)
        lines.append("\n▶️ встановлена · 💾 збережена на ПК · ☁️ є на GitHub")
        if error:
            lines.append(f"⚠️ GitHub: {esc(error)} — показано лише збережені на ПК.")
        rows.append([{"text": "✖️ Закрити", "callback_data": "ver:-2"}])
        return "\n".join(lines), {"inline_keyboard": rows}

    def version_callback(self, chat: int, msg_id: int, idx: int, rest: list, answer) -> None:
        """ver:-1 — показати список; ver:-2 — закрити; ver:i — підтвердження; ver:i:go — перейти."""
        if self.role(chat) != "admin":
            return answer("⛔ Лише для адміністратора", True)
        if idx == -2:
            answer()
            return self._delete(chat, [msg_id])
        if idx == -1:
            answer("Завантажую список…")

            def show():
                try:
                    text, kb = self.versions_text()
                except Exception as e:  # noqa: BLE001
                    text, kb = f"⚠️ Не вдалося отримати версії: {esc(str(e))}", None
                try:
                    self.tg.edit_text(chat, msg_id, text, kb)
                except TelegramError as e:
                    log.debug("versions edit: %s", e)
            return self._bg(show)
        versions = getattr(self, "_versions", [])
        if not 0 <= idx < len(versions):
            return answer("Список застарів — відкрийте «🕘 Версії» ще раз", True)
        version = versions[idx]
        if not rest:
            answer()
            kb = {"inline_keyboard": [[{"text": f"✅ Так, перейти на {version}", "callback_data": f"ver:{idx}:go"},
                                       {"text": "↩️ Назад", "callback_data": "ver:-1"}]]}
            try:
                self.tg.edit_text(chat, msg_id, f"Перейти на версію <b>{esc(version)}</b>?\n\n"
                                  "Поточна версія збережеться — повернутись можна будь-коли. "
                                  "Автооновлення стане на паузу, доки не натиснете «⬆️ Оновити».", kb)
            except TelegramError as e:
                log.debug("versions confirm: %s", e)
            return
        answer(f"Переходжу на {version}…")

        def go():
            try:
                text = self.switch_version(version)
            except Exception as e:  # noqa: BLE001
                text = f"⚠️ Не вдалося перейти на {version}: {e}"
            try:
                self.tg.edit_text(chat, msg_id, esc(text), None)
            except TelegramError:
                self.say(chat, text, html_mode=False)
        self._bg(go)

    def switch_version(self, version: str) -> str:
        from app.system import updater
        busy = sorted(self.app.publisher.active) or [i["id"] for i in self.db.items("status='publishing'")]
        if busy:
            raise ValueError(f"зараз публікується #{', #'.join(map(str, busy))} — спробуйте після публікації")
        updater.switch_to(self.cfg, version)
        self.db.event(None, "updated", {"version": version, "switch": True})
        self.db.set("control", "restart")   # головний цикл перезапустить бота на вибраній версії
        return f"↩️ Встановлено версію {version} — перезапускаюсь (≈10 с). Автооновлення на паузі."
