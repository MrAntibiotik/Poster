"""Панель керування poster у браузері (лише цей ПК: http://127.0.0.1:8770).

Вхід за паролем (створюється при першому відкритті). Тут можна:
  • запускати / зупиняти / ставити на паузу бота, тестовий слот, режим тест/бойовий, автозапуск;
  • вписати токени й ключі (Telegram, YouTube, Meta, TikTok) і авторизувати платформи;
  • керувати тим, хто отримує сповіщення бота;
  • змінювати розклад і папки, дивитися чергу й журнал.

Запуск: panel.bat  (або python panel.py)
"""
from __future__ import annotations

import logging
import re
import shlex
import sys

from app import ROOT  # noqa: E402  папка програми
sys.path.insert(0, str(ROOT))

from app.core.config import _hm

log = logging.getLogger("panel")


def VERSION_NOW() -> str:  # noqa: N802
    from app.core.config import VERSION
    return VERSION

DEFAULT_PORT = 8770
SESSION_HOURS = 12
PUBLIC_IDS = {"TELEGRAM_CHAT_ID", "FB_PAGE_ID", "IG_USER_ID", "META_APP_ID", "TIKTOK_CLIENT_KEY"}
SECRET_LABELS = {
    "TELEGRAM_BOT_TOKEN": "Токен бота", "TELEGRAM_CHAT_ID": "Головні адміністратори (chat_id)",
    "META_APP_ID": "App ID", "META_APP_SECRET": "App Secret", "META_PAGE_TOKEN": "Токен сторінки",
    "META_IG_TOKEN": "Окремий токен Instagram", "META_USER_TOKEN": "Токен користувача Meta", "IG_LOGIN_TOKEN": "Токен Instagram Login", "GEMINI_API_KEY": "Ключ Gemini API", "FB_PAGE_ID": "ID сторінки Facebook",
    "IG_USER_ID": "ID акаунта Instagram", "GITHUB_TOKEN": "Токен GitHub (оновлення)", "TIKTOK_CLIENT_KEY": "Client key", "TIKTOK_CLIENT_SECRET": "Client secret",
}

# Поля config.json, які можна змінювати з панелі: шлях -> тип
FIELDS = {
    "slots": "slots", "timezone": "str", "notify_before_minutes": "int", "min_notify_lead_minutes": "int",
    "publish_on_approve": "bool", "telegram.queue_cards": "bool", "ask_texts_before_publish": "bool", "catchup_minutes": "int", "max_skips": "int",
    "paths.ready": "str", "paths.posted": "str", "paths.skipped": "str", "paths.cancelled": "str", "ready_status": "str",
    "regenerate_command": "cmd", "regenerate_cwd": "str", "ffmpeg": "str", "ffprobe": "str",
    "reports.daily_time": "time", "reports.health_check_time": "time", "reports.stats_after_hours": "int", "reports.weekly_day": "int",
    "retry.attempts": "int", "retry.pause_seconds": "int", "graph_api_version": "str", "dry_run": "bool",
    "platforms.youtube.enabled": "bool", "platforms.youtube.privacy": "privacy",
    "platforms.youtube.add_shorts_hashtag": "bool", "platforms.youtube.notify_subscribers": "bool",
    "platforms.instagram.enabled": "bool", "platforms.instagram.share_to_feed": "bool", "platforms.instagram.max_upload_mb": "int",
    "platforms.instagram.single_attempt": "bool", "platforms.instagram.manual": "bool",
    "platforms.facebook.enabled": "bool",
    "platforms.tiktok.enabled": "bool", "platforms.tiktok.public": "bool", "platforms.tiktok.redirect_uri": "str",
    "auto_meta.enabled": "bool", "auto_meta.hashtags": "tagmap",
    "always_hashtags.tags": "taglist", "always_hashtags.in_title": "bool", "always_hashtags.in_captions": "bool",
    "ai.enabled": "bool", "ai.model": "str", "ai.hashtags_count": "int", "ai.channel_description": "str",
    "ai.extra_prompt": "str", "ai.language": "lang", "platforms.youtube.language": "lang",
    "footer.text": "str", "footer.in_description": "bool", "footer.in_captions": "bool", "auto_meta.default_title": "str",
    "on_missed_approval": "missed", "updates.auto": "bool", "updates.check_minutes": "int", "power.sleep_between_slots": "bool",
    "power.wake_before_minutes": "int", "power.quiet_minutes": "int", "power.hold_during_stream": "bool",
    "power.no_sleep_apps": "applist",
    "highlights.enabled": "bool", "highlights.folder": "str", "highlights.slots": "hlslots",
    "highlights.privacy": "privacy_opt", "highlights.description": "str", "highlights.battle_line": "bool",
}


def mask(value: str) -> str:
    if not value:
        return ""
    return value[:4] + "…" + value[-3:] if len(value) > 12 else "•" * len(value)


def dig(d: dict, path: str):
    for p in path.split("."):
        d = d.get(p) if isinstance(d, dict) else None
    return d


def put(d: dict, path: str, value) -> None:
    parts = path.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


def coerce(kind: str, value):
    if kind == "bool":
        return bool(value)
    if kind == "int":
        v = int(value)
        if v < 0:
            raise ValueError("має бути ≥ 0")
        return v
    if kind == "time":
        return _hm(value)
    if kind == "applist":
        items = value if isinstance(value, list) else re.split(r"[,;\n]+", str(value))
        return [x.strip() for x in items if x.strip()]
    if kind == "missed":
        if value not in ("wait", "skip", "auto"):
            raise ValueError("wait / skip / auto")
        return value
    if kind == "lang":
        if value not in ("uk", "en"):
            raise ValueError("uk / en")
        return value
    if kind == "privacy":
        if value not in ("public", "unlisted", "private"):
            raise ValueError("public / unlisted / private")
        return value
    if kind == "hlslots":
        items = value if isinstance(value, list) else re.split(r"[,\s;]+", str(value))
        slots = sorted({_hm(x) for x in items if str(x).strip()})
        if not 1 <= len(slots) <= 3:
            raise ValueError("хайлайти: від 1 до 3 разів на день, напр. 18:00")
        return slots
    if kind == "privacy_opt":
        if value not in ("", "public", "unlisted", "private"):
            raise ValueError("порожньо / public / unlisted / private")
        return value
    if kind == "slots":
        items = value if isinstance(value, list) else re.split(r"[,\s;]+", str(value))
        slots = sorted({_hm(x) for x in items if str(x).strip()})
        if not 1 <= len(slots) <= 6:
            raise ValueError("потрібно від 1 до 6 слотів")
        return slots
    if kind == "taglist":
        if isinstance(value, list):
            value = " ".join(map(str, value))
        out = []
        for t in re.split(r"[\s,;]+", str(value)):
            t = "#" + re.sub(r"[^\w]", "", t.lstrip("#"), flags=re.UNICODE)
            if len(t) > 1 and t.lower() not in {x.lower() for x in out}:
                out.append(t)
        return out
    if kind == "tagmap":
        if isinstance(value, dict):
            return value
        out = {}
        for line in str(value).splitlines():
            if ":" not in line:
                continue
            game, tags = line.split(":", 1)
            game = game.strip() or "*"
            if game in ("усі", "всі", "інші", "все"):
                game = "*"
            tags = ["#" + t.lstrip("#") for t in re.split(r"[\s,]+", tags) if t.strip("#").strip()]
            if tags:
                out[game] = tags
        if "*" not in out:
            out["*"] = ["#shorts"]
        return out
    if kind == "cmd":
        if isinstance(value, list):
            return [str(x) for x in value]
        return [t.strip('"') for t in shlex.split(str(value), posix=False)] if str(value).strip() else []
    return str(value).strip()


def tagmap_to_text(table: dict) -> str:
    table = table or {}
    lines = [f"{k}: {' '.join(v)}" for k, v in table.items() if k != "*"]
    lines.append(f"*: {' '.join(table.get('*', []))}")
    return "\n".join(lines)


def cmd_to_text(cmd) -> str:
    return " ".join(f'"{a}"' if " " in a else a for a in (cmd or []))




# ======================================================================
