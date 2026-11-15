"""Стан для головної сторінки: черга, помилки, слоти."""
from __future__ import annotations

import json
import re
import time
import types
from app.core.config import DEFAULTS
from app.core.config import NAMES
from app.core.config import PLATFORMS
from app.core.config import _merge
from app.core.config import fmt_slot
from app.core.config import local_dt
from app.core.config import now_ts
from app.system import control
from pathlib import Path
from .common import (
    FIELDS,
    PUBLIC_IDS,
    SECRET_LABELS,
    VERSION_NOW,
    cmd_to_text,
    dig,
    log,
    mask,
    tagmap_to_text,
)


class StateMixin:
    def last_error(self) -> str:
        """Остання помилка служби з журналу — щоб показати причину просто в панелі."""
        try:
            lines = Path(self.cfg["paths"]["log"]).read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
        except OSError:
            return "Подивіться вкладку «Журнал»."
        for line in reversed(lines):
            if "[ERROR]" in line or "[CRITICAL]" in line or "Error:" in line:
                text = line.split("]: ", 1)[-1] if "]: " in line else line
                for v in self.cfg["secrets"].values():
                    if v and len(v) > 12:
                        text = text.replace(v, mask(v))
                return "Причина: " + text[:300]
        return "Подивіться вкладку «Журнал»."

    def _sched(self):
        from app.platforms import build_platforms
        from app.engine.scheduler import Scheduler
        app = types.SimpleNamespace(cfg=self.cfg, db=self.db, platforms={k: None for k in build_platforms(self.cfg)})
        sched = Scheduler(app)
        app.scheduler = sched
        return sched

    def failed_state(self, cfg: dict, now: int) -> list[dict]:
        from app.core.config import fmt_when
        try:
            sched = self._sched()
            rows = [(i, False) for i in sched.failed_items()] + [(i, True) for i in sched.failed_items(ignored=True)]
        except Exception as e:  # noqa: BLE001
            log.warning("failed_state: %s", e)
            return []
        out = []
        for item, ignored in rows[:30]:
            when = sched.retry_at(item["id"])
            out.append({
                "id": item["id"], "name": Path(item["mp4"]).stem, "ignored": ignored,
                "exists": Path(item["mp4"]).exists(),
                "errors": {NAMES.get(p, p): str(r.get("error", ""))[:900] for p, r in item["errors"].items()}
                or ({"—": "не опубліковано ніде"} if item["status"] == "failed" else {}),
                "ok": [NAMES.get(p, p) for p, r in item["results"].items() if r.get("status") == "ok"],
                "retry": fmt_when(cfg, when, now) if when else "",
            })
        return out

    def state(self) -> dict:
        cfg = self.reload()
        now = now_ts()
        merged = _merge(DEFAULTS, self.raw_config())
        settings = {k: dig(merged, k) for k in FIELDS}
        settings["regenerate_command"] = cmd_to_text(settings["regenerate_command"])
        settings["auto_meta.hashtags"] = tagmap_to_text(settings["auto_meta.hashtags"])
        settings["always_hashtags.tags"] = " ".join(settings["always_hashtags.tags"] or [])
        settings["power.no_sleep_apps"] = ", ".join(settings["power.no_sleep_apps"] or [])
        settings["highlights.slots"] = ", ".join(settings["highlights.slots"] or [])
        secrets_ = {n: {"set": bool(v), "value": v if n in PUBLIC_IDS else mask(v), "label": SECRET_LABELS.get(n, n)}
                    for n, v in cfg["secrets"].items()}

        tokens = {}
        yt_secret = Path(cfg["platforms"]["youtube"]["client_secrets_file"])
        tokens["youtube"] = {"client_secret": yt_secret.exists(),
                             "connected": (Path(cfg["paths"]["tokens"]) / "youtube_token.json").exists()}
        tt_file = Path(cfg["paths"]["tokens"]) / "tiktok_token.json"
        tt = {"connected": tt_file.exists()}
        if tt_file.exists():
            try:
                t = json.loads(tt_file.read_text(encoding="utf-8"))
                tt["refresh_expires"] = local_dt(cfg, int(t.get("refresh_expires_at", 0))).strftime("%d.%m.%Y")
                tt["scope"] = t.get("scope")
            except (ValueError, OSError):
                pass
        tokens["tiktok"] = tt
        tokens["meta"] = {"connected": bool(cfg["secrets"]["META_PAGE_TOKEN"])}

        db = self.db
        from app.core.config import fmt_when
        from app.engine.scheduler import Scheduler
        sched = Scheduler(types.SimpleNamespace(cfg=cfg, db=db, app=None))
        queue = [{"id": i["id"], "name": Path(i["mp4"]).stem, "game": i["game"], "skips": i["skips"],
                  "when": fmt_when(cfg, ts, now) if ts else ""}
                 for i, ts in sched.projection(now)[:30]]
        current = [{"id": i["id"], "name": Path(i["mp4"]).stem, "status": i["status"],
                    "slot": fmt_slot(cfg, i["slot_at"], now) if i["slot_at"] else ""}
                   for i in db.items("status IN ('notified','approved','publishing','failed')", order="slot_at")]
        upcoming = [{"slot": fmt_when(cfg, s["slot_at"], now), "item": s["item"]["id"] if s["item"] else None,
                     "name": Path(s["item"]["mp4"]).stem if s["item"] else "", "kind": s["kind"], "manual": s["manual"]}
                    for s in sched.slot_plan(now, 6)]
        posted = [{"id": i["id"], "name": Path(i["mp4"]).stem,
                   "when": local_dt(cfg, i["posted_at"]).strftime("%d.%m %H:%M") if i["posted_at"] else "",
                   "links": {p: r.get("url") for p, r in i["results"].items() if r.get("status") == "ok"},
                   "errors": {p: r.get("error") for p, r in i["results"].items() if r.get("status") != "ok"},
                   "details": any(r.get("status") != "ok" or r.get("tried") for r in i["results"].values())}
                  for i in db.items("status='posted'", order="posted_at DESC")[:10]]
        owners = [int(x) for x in re.findall(r"-?\d+", cfg["secrets"]["TELEGRAM_CHAT_ID"] or "")]
        users = [{**u, "owner": u["chat_id"] in owners} for u in db.users()]
        health = {p: db.get(f"health_{p}") for p in PLATFORMS}

        log_tail = ""
        log_path = Path(cfg["paths"]["log"])
        if log_path.exists():
            with open(log_path, "rb") as f:
                f.seek(max(0, log_path.stat().st_size - 80000))
                log_tail = f.read().decode("utf-8", errors="replace").split("\n", 1)[-1]
            for v in cfg["secrets"].values():
                if v and len(v) > 12:
                    log_tail = log_tail.replace(v, mask(v))
            log_tail = "\n".join(log_tail.splitlines()[-400:])

        running = control.is_running(cfg["lock_port"])
        owner = None
        if running:  # пошук процесу на Windows запускає PowerShell — не частіше ніж раз на 20 с
            cached = getattr(self, "_owner_cache", (0, None))
            if time.time() - cached[0] > 20:
                cached = (time.time(), control.port_owner(cfg["lock_port"]))
                self._owner_cache = cached
            owner = cached[1]
        return {
            "running": running,
            "foreign_bot": owner["cmd"] if control.is_foreign(owner) else None,
            "paused": db.get("paused") == "1",
            "stopped": db.get("stopped") == "1",
            "dry_run": bool(cfg.get("dry_run")),
            "restart_needed": self.restart_needed,
            "autostart": control.autostart_installed() if control.WIN else None,
            "windows": control.WIN,
            "settings": settings, "secrets": secrets_, "tokens": tokens,
            "platforms": {p: {"name": NAMES[p], "enabled": bool(cfg["platforms"][p].get("enabled")),
                              "health": health[p]} for p in PLATFORMS},
            "queue": queue, "current": current, "upcoming": upcoming, "posted": posted,
            "users": users, "jobs": self.jobs, "log": log_tail,
            "health_time": fmt_slot(cfg, int(db.get("health_ts")), now) if db.get("health_ts") else None,
            "ready_info": self.count_shorts(cfg["paths"]["ready"]),
            "failed": self.failed_state(cfg, now),
            "cancelled": [{"id": i["id"], "name": Path(i["mp4"]).stem, "exists": Path(i["mp4"]).exists()}
                          for i in db.items("status='cancelled'", order="id DESC")[:15]],
            "update": {"remote": db.get("update_remote") or "", "error": db.get("update_error") or "",
                       "newer": __import__("app.system.updater", fromlist=["x"]).version_key(db.get("update_remote") or "")
                       > __import__("app.system.updater", fromlist=["x"]).version_key(VERSION_NOW()),
                       "token": bool(cfg["secrets"].get("GITHUB_TOKEN"))},
            "stream_until": fmt_slot(cfg, int(db.get("stream_until")), now)
            if int(db.get("stream_until") or 0) > now else "",
            "power": {"on": bool((cfg.get("power") or {}).get("sleep_between_slots")),
                      "state": db.get("power_state") or "",
                      "next_wake": fmt_when(cfg, int(db.get("power_next_wake")), now)
                      if db.get("power_next_wake") and int(db.get("power_next_wake")) > now else ""},
            "work": [self.item_view(cfg, i, now) for i in
                     db.items("status IN ('notified','approved','publishing')", order="slot_at")],
            "queue_items": [self.item_view(cfg, i, now, ts or 0) for i, ts in sched.projection(now)[:40]],
            "free_slots": [{"ts": t, "label": fmt_when(cfg, t, now)} for t in sched.future_slots(now, 10)],
            "pending_users": [u for u in users if u["role"] == "pending"],
            "can_regen": bool(cfg.get("regenerate_command")) or bool(cfg.get("ai", {}).get("enabled")
                                                                     and cfg["secrets"].get("GEMINI_API_KEY")),
            "path_problems": self.path_problems(cfg),
            "root": str(self.root), "now": local_dt(cfg, now).strftime("%d.%m.%Y %H:%M"),
            "channels": self.channels_state(),
        }
