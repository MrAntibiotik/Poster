"""App — спільний стан запущеної служби: конфіг, БД, платформи, бот, планувальник; тексти черги/статусу."""
from __future__ import annotations

from __future__ import annotations
import html
import logging
import os
from pathlib import Path
from app.core.config import NAMES, SHORT, ConfigError, enabled_platforms, ensure_dirs, fmt_slot, fmt_when, load_config, now_ts
from app.core.db import DB
from app.platforms import build_platforms

log = logging.getLogger("poster")
esc = html.escape


class App:
    def __init__(self, cfg: dict, bot=None):
        self.cfg = cfg
        ensure_dirs(cfg)
        self.db = DB(Path(cfg["paths"]["data"]) / "poster.sqlite3")
        self.platforms = build_platforms(cfg)
        from app.engine.publisher import Publisher
        from app.engine.scheduler import Scheduler

        self.publisher = Publisher(self)
        self.scheduler = Scheduler(self)
        if bot is None:
            from app.telegram.bot import Bot
            bot = Bot(self)
        self.bot = bot
        self._cfg_mtime = self._config_mtime()

    # ---------- налаштування «на льоту» ----------
    def _config_mtime(self) -> float:
        total = 0.0
        for p in (self.cfg.get("config_path"), self.cfg.get("env_path")):
            try:
                total += os.path.getmtime(p) if p else 0
            except OSError:
                pass
        return total

    def reload_config(self, force: bool = False) -> bool:
        """Підхоплює зміни config.json / .env без перезапуску (напр. з панелі). True — якщо перечитано."""
        m = self._config_mtime()
        if not force and m == self._cfg_mtime:
            return False
        try:
            new = load_config(self.cfg["config_path"])
        except ConfigError as e:
            log.error("Нові налаштування з помилкою — лишаю попередні: %s", e)
            self._cfg_mtime = m
            return False
        self.cfg.clear()
        self.cfg.update(new)  # той самий об'єкт — його бачать бот, планувальник, публікатор
        self.platforms = build_platforms(self.cfg)
        self._cfg_mtime = m
        log.info("Налаштування перечитано; платформи: %s", list(self.platforms))
        return True

    def set_platform_enabled(self, key: str, enabled: bool) -> None:
        import json
        path = Path(self.cfg["config_path"])
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.setdefault("platforms", {}).setdefault(key, {})["enabled"] = bool(enabled)
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        self.reload_config(force=True)

    # ---------- тексти для команд ----------
    def queue_text(self) -> str:
        now = now_ts()
        active = self.db.items("status IN ('notified','approved','publishing')", order="slot_at")
        queue = self.db.queue()
        lines = [f"📦 <b>Черга: {len(queue)}</b>"]
        st_names = {"notified": "⏳ чекає підтвердження", "approved": "👍 підтверджено", "publishing": "⏫ публікується"}
        for it in active:
            slot = fmt_slot(self.cfg, it["slot_at"], now) if it["slot_at"] else ""
            hl = "🎞 " if it.get("kind") == "highlight" else ""
            lines.append(f"▶️ #{it['id']} {hl}{esc(Path(it['mp4']).stem)} — {st_names[it['status']]} {slot}")
        plan = self.scheduler.projection(now)
        for n, (it, ts) in enumerate(plan[:25], 1):
            game = f"[{esc(it['game'])}] " if it["game"] else ""
            if it.get("kind") == "highlight":
                game = "🎞 хайлайт · "
            sk = f" (пропусків: {it['skips']})" if it["skips"] else ""
            when = f" → <b>{fmt_when(self.cfg, ts, now)}</b>" if ts else ""
            lines.append(f"{n}. #{it['id']} {game}{esc(Path(it['mp4']).stem)}{when}{sk}")
        if plan and plan[0][1] is None and self.db.get("paused") == "1":
            lines.append("⏸ Розклад на паузі — час не призначається")
        if len(queue) > 25:
            lines.append(f"… і ще {len(queue) - 25}")
        failed = self.db.items("status='failed'")
        if failed:
            lines.append("\n❌ Не опубліковано: " + ", ".join(f"#{i['id']}" for i in failed) + " (/retry N)")
        return "\n".join(lines)

    def now_lines(self, now: int) -> list[str]:
        """Що відбувається просто зараз: публікації, черга Instagram, повтори, ліміт Meta."""
        out = ["⏫ <b>Зараз</b>"]
        ig_now = None
        try:
            from app.platforms import meta_graph
            ig_now = meta_graph.Instagram.current
            until = meta_graph.blocked_until()
            usage = meta_graph.usage_pct()
        except Exception:  # noqa: BLE001
            until, usage = 0, 0
        stopped = self.db.get("stopped") == "1"
        busy = False
        items = self.db.items("status='publishing'", order="id")
        items.sort(key=lambda i: (self.publisher.position(i["id"]) is None, self.publisher.position(i["id"]) or 0))
        for it in items:
            busy = True
            name = f"#{it['id']} {esc(Path(it['mp4']).stem)}"
            done = [SHORT[p] for p, r in it["results"].items() if r.get("status") == "ok"]
            ok = f" (вже: {', '.join(done)} ✅)" if done else ""
            if not self.publisher.is_active(it["id"]):
                out.append(f"• {name} — {'⛔ зупинено' if stopped else '⚠️ перервано (🔁 Повторити під відео)'}{ok}")
                continue
            pos = self.publisher.position(it["id"])
            if pos:
                out.append(f"• {name} — ⏳ у черзі публікацій: {pos}-й{ok}")
                continue
            cur = self.db.get(f"pubnow:{it['id']}")
            tr = self.db.get(f"pubtry:{it['id']}:{cur}") if cur else None
            if cur == "instagram" and ig_now and Path(ig_now).stem != Path(it["mp4"]).stem \
                    and not Path(ig_now).stem.startswith(Path(it["mp4"]).stem):
                state = "📸 чекає черги в Instagram (по одному відео)"
            elif cur:
                phase = self.bot._phase_text(it["id"]) if hasattr(self.bot, "_phase_text") else "вантажиться"
                state = f"{NAMES.get(cur, cur)} — {phase}" + (f", спроба {tr}" if tr else "")
            else:
                state = "готується"
            out.append(f"• {name} — {state}{ok}")
        for r in self.db.q("SELECT slot_at, item_id FROM slots WHERE state='retry' AND slot_at>? ORDER BY slot_at",
                           (now,))[:5]:
            busy = True
            out.append(f"• 🔁 повтор #{r['item_id']} — {fmt_when(self.cfg, r['slot_at'], now)}")
        if not busy:
            out.append("✅ Нічого не публікується")
        if until:
            out.append(f"⏳ Meta (Instagram/Facebook) обмежила запити до {fmt_slot(self.cfg, until, now)}")
        elif usage >= 50:
            out.append(f"📊 Квота запитів Meta використана на {usage}%")
        return out

    def status_text(self) -> str:
        now = now_ts()
        lines = self.now_lines(now) + ["", "🕐 <b>Найближчі слоти</b>"]
        kinds = {"notified": "⏳ чекає підтвердження", "approved": "👍 підтверджено", "publishing": "⏫ публікується",
                 "planned": "📦 з черги"}
        for s in self.scheduler.slot_plan(now, 6):
            src = " (тестовий)" if s["manual"] else " 🎞 хайлайт" if s.get("lane") == "highlight" else ""
            it = s["item"]
            who = (f"#{it['id']} {esc(Path(it['mp4']).stem)} — {kinds.get(s['kind'], s['kind'])}" if it else
                   "⏸ пауза" if s["kind"] == "paused" else "порожньо (черга порожня)")
            lines.append(f"• {fmt_when(self.cfg, s['slot_at'], now)}{src}: {who}")
        if self.db.get("stopped") == "1":
            lines.append("⛔ <b>Публікації зупинено</b> — нічого не публікується («✅ Відновити публікації»)")
        if self.db.get("paused") == "1":
            lines.append("⏸ <b>Розклад на паузі</b> — нові шортси не надсилаються")
        lines.append(f"\n📦 У черзі: {len(self.db.queue())}")
        lines.append("Платформи: " + ", ".join(NAMES[p] for p in enabled_platforms(self.cfg)))
        tt = self.cfg["platforms"]["tiktok"]
        if tt.get("enabled"):
            lines.append("TikTok: " + ("публічно" if tt.get("public") else "приватно (SELF_ONLY)"))
        if self.db.get("health_ts"):
            lines.append(f"\n🔐 <b>Доступи</b> (перевірено {fmt_slot(self.cfg, int(self.db.get('health_ts')), now)}):")
            for p in self.platforms:
                lines.append(f"{NAMES[p]}: {esc(self.db.get(f'health_{p}') or '—')}")
        if self.cfg.get("dry_run"):
            lines.append("🧪 dry_run увімкнено")
        return "\n".join(lines)
