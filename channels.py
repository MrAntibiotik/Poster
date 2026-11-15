"""Додаткові канали (напр. англомовний «EN-канал») — окремі екземпляри poster зі спільним кодом.

Кожен канал живе в папці channels/<ключ>/ і має свої:
  config.json   — розклад (свій часовий пояс), папка готових шортсів, мова, хештеги, підпис каналу;
  .env          — свій Telegram-бот і свої ключі Meta / TikTok (YouTube — свій токен у tokens/);
  tokens/, data/ (черга, база), posted/, skipped/, log.txt.
Тобто черга, слоти й акаунти платформ повністю окремі від основного (українського) каналу,
а код один: оновлення програми діє на всі канали.

Основний бот сам запускає увімкнені канали і перезапускає їх після оновлення.
"""
from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path

import control

log = logging.getLogger("channels")

ROOT = Path(__file__).resolve().parent
CHANNELS_DIR = ROOT / "channels"

# Англомовний канал: шаблон налаштувань (усе можна змінити в панелі цього каналу)
EN_TEMPLATE: dict = {
    "channel": {"key": "en", "name": "EN-канал", "autostart": True},
    # Найбільше переглядів в англомовних шортсах — вдень і ввечері за часом США (Нью-Йорк)
    "timezone": "America/New_York",
    "slots": ["11:00", "14:00", "17:00", "19:30", "21:30"],
    "lock_port": 47614,
    "panel_port": 8771,
    "paths": {"ready": "ready", "posted": "posted", "skipped": "skipped", "cancelled": "cancelled",
              "data": "data", "tokens": "tokens", "log": "log.txt"},
    # код оновлює основний бот — тут лише перезапуск після його оновлення
    "updates": {"auto": False},
    "always_hashtags": {"tags": ["#worldoftanks", "#wot", "#shorts"], "in_title": True, "in_captions": True},
    "footer": {
        "text": "🇺🇦 Ukrainian veteran on the road to 1000 subscribers — subscribe to support 🛡️♥️",
        "in_description": True,
        "in_captions": True,
    },
    "auto_meta": {
        "enabled": True,
        "default_title": "{game} — insane moment 🔥",
        "hashtags": {"WoT": ["#worldoftanks", "#wot", "#tanks", "#shorts"],
                     "*": ["#shorts", "#gaming", "#gameplay"]},
    },
    "ai": {
        "enabled": True,
        "language": "en",
        "channel_description": "English-language World of Tanks Shorts channel run by a Ukrainian veteran: "
                               "the best plays, clutches and one-shots",
        "extra_prompt": "",
    },
    "platforms": {
        "youtube": {"enabled": True, "language": "en"},
        "tiktok": {"enabled": False},
        "instagram": {"enabled": False},
        "facebook": {"enabled": False},
    },
}
TEMPLATES = {"en": EN_TEMPLATE}

# Що з .env основного каналу можна взяти як стартові значення (ключі програми, не акаунтів)
SHARED_SECRETS = ("TELEGRAM_CHAT_ID", "GEMINI_API_KEY", "META_APP_ID", "META_APP_SECRET",
                  "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "GITHUB_TOKEN")


def channel_dir(key: str) -> Path:
    if not key.isalnum():
        raise ValueError(f"Неправильний ключ каналу «{key}»")
    return CHANNELS_DIR / key


def config_path(key: str) -> Path:
    return channel_dir(key) / "config.json"


def db_path(key: str) -> Path:
    """База (черга, слоти) каналу."""
    raw = read_raw(key)
    data = Path(os.path.expandvars(str((raw.get("paths") or {}).get("data") or "data")))
    return (data if data.is_absolute() else channel_dir(key) / data) / "poster.sqlite3"


def exists(key: str) -> bool:
    return config_path(key).exists()


def keys() -> list[str]:
    if not CHANNELS_DIR.is_dir():
        return []
    return sorted(d.name for d in CHANNELS_DIR.iterdir() if d.is_dir() and (d / "config.json").exists())


def read_raw(key: str) -> dict:
    return json.loads(config_path(key).read_text(encoding="utf-8"))


def _read_env(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def create(main_cfg: dict, key: str = "en", ready: str | None = None) -> Path:
    """Створює папку каналу з шаблону. Існуючий канал не перезаписується."""
    if key not in TEMPLATES:
        raise ValueError(f"Невідомий канал «{key}»")
    d = channel_dir(key)
    if (d / "config.json").exists():
        raise ValueError("Канал уже створено")
    d.mkdir(parents=True, exist_ok=True)
    raw = copy.deepcopy(TEMPLATES[key])
    if ready:
        raw["paths"]["ready"] = ready
    # той самий Google-проєкт (client_secret.json) підходить і для другого YouTube-каналу
    secrets_file = (main_cfg.get("platforms") or {}).get("youtube", {}).get("client_secrets_file")
    if secrets_file:
        raw["platforms"]["youtube"]["client_secrets_file"] = str(secrets_file)
    for k in ("ffmpeg", "ffprobe"):
        if main_cfg.get(k):
            raw[k] = main_cfg[k]
    (d / "config.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    main_env = _read_env(Path(main_cfg.get("env_path") or ROOT / ".env"))
    lines = ["# Секрети каналу «%s». Telegram-бот — ОКРЕМИЙ (створіть у @BotFather)." % raw["channel"]["name"],
             "TELEGRAM_BOT_TOKEN="]
    lines += [f"{k}={main_env[k]}" for k in SHARED_SECRETS if main_env.get(k)]
    (d / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("Створено канал %s: %s", key, d)
    return d


def _port(raw: dict, name: str, default: int) -> int:
    try:
        return int(raw.get(name) or default)
    except (TypeError, ValueError):
        return default


def status(key: str) -> dict:
    """Стан каналу для панелі."""
    if not exists(key):
        return {"key": key, "exists": False}
    raw = read_raw(key)
    lock = _port(raw, "lock_port", EN_TEMPLATE["lock_port"])
    pport = _port(raw, "panel_port", EN_TEMPLATE["panel_port"])
    env = _read_env(channel_dir(key) / ".env")
    ready = Path(os.path.expandvars(str((raw.get("paths") or {}).get("ready") or "ready")))
    if not ready.is_absolute():
        ready = channel_dir(key) / ready
    yt_token = Path(os.path.expandvars(str((raw.get("paths") or {}).get("tokens") or "tokens")))
    if not yt_token.is_absolute():
        yt_token = channel_dir(key) / yt_token
    plats = raw.get("platforms") or {}
    return {
        "key": key,
        "exists": True,
        "name": (raw.get("channel") or {}).get("name") or key,
        "autostart": bool((raw.get("channel") or {}).get("autostart", True)),
        "dir": str(channel_dir(key)),
        "ready": str(ready),
        "timezone": raw.get("timezone"),
        "slots": raw.get("slots") or [],
        "lock_port": lock,
        "panel_port": pport,
        "panel_url": f"http://127.0.0.1:{pport}/",
        "running": control.is_running(lock),
        "panel_running": control.is_running(pport),
        "telegram_ready": bool(env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID")),
        "youtube_authorized": (yt_token / "youtube_token.json").exists(),
        "platforms": [p for p in ("youtube", "tiktok", "instagram", "facebook") if (plats.get(p) or {}).get("enabled")],
        "footer": (raw.get("footer") or {}).get("text") or "",
    }


def set_autostart(key: str, on: bool) -> None:
    raw = read_raw(key)
    raw.setdefault("channel", {})["autostart"] = bool(on)
    config_path(key).write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


def start(key: str, wait: int = 30) -> bool:
    st = status(key)
    if not st["exists"]:
        raise ValueError("Канал ще не створено")
    if not st["telegram_ready"]:
        raise ValueError("У каналу немає свого Telegram-бота: відкрийте панель каналу → «✈️ Telegram і доступ»")
    return control.start_bot(st["lock_port"], wait=wait, config=str(config_path(key)))


def stop(key: str) -> bool:
    st = status(key)
    if not st["exists"] or not st["running"]:
        return True
    from db import DB
    raw = read_raw(key)
    data = Path(os.path.expandvars(str((raw.get("paths") or {}).get("data") or "data")))
    if not data.is_absolute():
        data = channel_dir(key) / data
    return control.stop_bot(DB(data / "poster.sqlite3"), st["lock_port"])


def open_panel(key: str) -> str:
    """Запускає панель каналу (якщо ще не працює) і повертає її адресу."""
    st = status(key)
    if not st["exists"]:
        raise ValueError("Канал ще не створено")
    if not st["panel_running"]:
        control.spawn_panel(str(config_path(key)), st["panel_port"])
    return st["panel_url"]


def autostart_all(main_cfg: dict) -> None:
    """Основний бот при запуску піднімає увімкнені канали (якщо в них налаштовано Telegram)."""
    if main_cfg.get("channel"):  # сам є додатковим каналом
        return
    for key in keys():
        try:
            st = status(key)
            if st["autostart"] and st["telegram_ready"] and not st["running"]:
                ok = start(key, wait=20)
                log.info("Канал %s: %s", key, "запущено" if ok else "не запустився (див. його log.txt)")
        except Exception as e:  # noqa: BLE001
            log.warning("Канал %s не запущено: %s", key, e)


def restart_running(main_cfg: dict) -> None:
    """Після оновлення коду: перезапустити працюючі канали, щоб і вони працювали на новій версії."""
    if main_cfg.get("channel"):
        return
    for key in keys():
        try:
            if status(key)["running"]:
                stop(key)
                start(key, wait=20)
                log.info("Канал %s перезапущено після оновлення", key)
        except Exception as e:  # noqa: BLE001
            log.warning("Канал %s після оновлення: %s", key, e)


def discovery_file() -> Path:
    """Де інші програми (напр. «Різак» — нарізка шортсів) знаходять папки готових шортсів poster без налаштувань."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".config")
    return Path(base) / "MrAntibiotik" / "poster.json"


def publish_paths(main_cfg: dict) -> Path | None:
    """Записує шляхи: основний канал і всі додаткові (EN …). Файл перезаписується при кожному запуску бота,
    тож шляхи завжди актуальні, навіть якщо папку змінили в панелі."""
    if main_cfg.get("channel"):
        return None
    data = {"version": 1, "root": str(ROOT), "ready": str(main_cfg["paths"]["ready"]), "channels": {}}
    for key in keys():
        try:
            st = status(key)
            Path(st["ready"]).mkdir(parents=True, exist_ok=True)
            data["channels"][key] = {"name": st["name"], "ready": st["ready"], "timezone": st["timezone"]}
        except Exception as e:  # noqa: BLE001
            log.warning("Канал %s: %s", key, e)
    path = discovery_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path
    except OSError as e:
        log.warning("Не вдалося записати %s: %s", path, e)
        return None
