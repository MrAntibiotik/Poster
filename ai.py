"""Назви, хештеги й тексти від безкоштовного ШІ (Google Gemini API, безкоштовний тариф).

Програма витягує кілька кадрів із шортса (ffmpeg) і надсилає їх разом із назвою гри та файлу.
Ключ: https://aistudio.google.com/apikey  ->  .env: GEMINI_API_KEY=...
Якщо ШІ недоступне — лишаються тексти з імені файлу (meta.auto_meta_json).
"""
from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path

import requests

import media
from meta import _hashtags, game_hashtags, read_json, write_json

log = logging.getLogger("ai")

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "description": {"type": "STRING"},
        "hashtags": {"type": "ARRAY", "items": {"type": "STRING"}},
        "tiktok": {"type": "STRING"},
        "instagram": {"type": "STRING"},
    },
    "required": ["title", "description", "hashtags", "tiktok", "instagram"],
}


class AIError(Exception):
    pass


def settings(cfg: dict) -> dict:
    return cfg.get("ai") or {}


def enabled(cfg: dict) -> bool:
    return bool(settings(cfg).get("enabled") and cfg["secrets"].get("GEMINI_API_KEY"))


def extract_frames(path: str | Path, cfg: dict, count: int = 4) -> list[bytes]:
    """Кілька кадрів JPEG (480 px) рівномірно по відео. Без ffmpeg — порожній список."""
    dur = media.probe(str(path), cfg["ffprobe"]).get("duration") or 0
    if not dur:
        return []
    frames = []
    for i in range(count):
        t = dur * (i + 1) / (count + 1)
        try:
            r = media.run_hidden([cfg["ffmpeg"], "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}",
                                  "-i", str(path), "-frames:v", "1", "-vf", "scale=-2:480",
                                  "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "-"], timeout=60)
        except media.MediaError as e:
            log.warning("Кадри для ШІ: %s", e)
            return frames
        if r.returncode == 0 and r.stdout:
            frames.append(r.stdout)
    return frames


def language(cfg: dict) -> str:
    """Мова текстів ШІ: «uk» (за замовчуванням) або «en» (англомовний канал)."""
    return "en" if str(settings(cfg).get("language") or "uk").lower().startswith("en") else "uk"


def build_prompt_en(cfg: dict, game: str, filename: str, n_tags: int, has_frames: bool) -> str:
    s = settings(cfg)
    channel = s.get("channel_description") or "English-language gaming YouTube channel"
    lines = [
        f"You are the social media editor of this channel: {channel}.",
        f"Write texts for a vertical short from the game \"{game or 'unknown game'}\".",
        f"File name (may be a hint or meaningless): \"{filename}\".",
        "Frames from the video are attached." if has_frames else "No frames — rely on the game and the file name.",
        "",
        "Requirements (everything in natural US English):",
        "• title — a catchy YouTube Shorts title up to 70 characters, no quotes, at most 1 emoji, "
        "no false clickbait; do not invent details that are not visible in the frames;",
        "• description — 1–2 sentences;",
        f"• hashtags — exactly {n_tags} hashtags with #: the game, the moment/genre, #shorts;",
        "• tiktok — a TikTok caption: 1 short sentence + these hashtags;",
        "• instagram — an Instagram Reels caption: 1–2 sentences + these hashtags.",
    ]
    extra = (s.get("extra_prompt") or "").strip()
    if extra:
        lines += ["", f"Extra wishes from the author: {extra}"]
    return "\n".join(lines)


def build_prompt(cfg: dict, game: str, filename: str, n_tags: int, has_frames: bool) -> str:
    if language(cfg) == "en":
        return build_prompt_en(cfg, game, filename, n_tags, has_frames)
    s = settings(cfg)
    channel = s.get("channel_description") or "український ігровий YouTube-канал"
    lines = [
        f"Ти — SMM-редактор каналу: {channel}.",
        f"Придумай тексти для вертикального шортса з гри «{game or 'невідома гра'}».",
        f"Назва файлу (може бути підказкою або безлика): «{filename}».",
        "Кадри з відео додано." if has_frames else "Кадрів немає — орієнтуйся на гру й назву файлу.",
        "",
        "Вимоги (усе українською):",
        "• title — чіпка назва для YouTube Shorts до 70 символів, без лапок, максимум 1 емодзі, "
        "без неправдивого клікбейту; не вигадуй деталей, яких не видно на кадрах;",
        "• description — 1–2 речення опису;",
        f"• hashtags — рівно {n_tags} хештегів з #: гра, жанр/момент, українська аудиторія, #shorts;",
        "• tiktok — підпис для TikTok: 1 коротке речення + ці хештеги;",
        "• instagram — підпис для Instagram Reels: 1–2 речення + ці хештеги.",
    ]
    extra = (s.get("extra_prompt") or "").strip()
    if extra:
        lines += ["", f"Додаткові побажання автора: {extra}"]
    return "\n".join(lines)


def normalize(result: dict, cfg: dict, game: str, n_tags: int) -> dict:
    title = re.sub(r"\s+", " ", str(result.get("title") or "")).strip().strip('"«»“”')[:100]
    if not title:
        raise AIError("ШІ не повернуло назву")
    tags = _hashtags([t for t in re.split(r"[\s,]+", " ".join(map(str, result.get("hashtags") or []))) if t])
    table = (cfg.get("auto_meta") or {}).get("hashtags") or {}
    extra_pool = (["#shorts", "#gaming", "#gameplay", "#worldoftanks", "#wot"] if language(cfg) == "en" else
                  ["#shorts", "#gaming", "#ігри", "#gameplay", "#українською", "#геймінг"])
    pool = game_hashtags(cfg, game) + _hashtags(table.get("*") or []) + extra_pool
    for t in pool:  # добираємо до потрібної кількості
        if len(tags) >= n_tags:
            break
        if t.lower() not in {x.lower() for x in tags}:
            tags.append(t)
    tags = tags[:n_tags]
    line = " ".join(tags)

    def with_tags(text: str, fallback: str) -> str:
        text = re.sub(r"#[\w\d_]+", "", str(text or "")).strip() or fallback
        return f"{text}\n\n{line}".strip()

    desc = str(result.get("description") or "").strip()
    return {
        "title": title,
        "description": desc,
        "hashtags": tags,
        "tiktok": with_tags(result.get("tiktok"), title).replace("\n\n", " "),
        "instagram": with_tags(result.get("instagram"), title),
    }


def generate(cfg: dict, mp4: str | Path, game: str = "") -> dict:
    s = settings(cfg)
    key = cfg["secrets"].get("GEMINI_API_KEY")
    if not key:
        raise AIError("Не задано ключ Gemini (панель → 🤖 ШІ)")
    n_tags = int(s.get("hashtags_count") or 5)
    frames = extract_frames(mp4, cfg, int(s.get("frames") or 4))
    parts = [{"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(f).decode()}} for f in frames]
    parts.append({"text": build_prompt(cfg, game, Path(mp4).stem, n_tags, bool(frames))})
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": SCHEMA,
                             "temperature": float(s.get("temperature", 0.9))},
    }
    model = s.get("model") or "gemini-3.5-flash"
    try:
        r = requests.post(API.format(model=model), json=body, timeout=120,
                          headers={"x-goog-api-key": key, "Content-Type": "application/json"})
    except requests.RequestException as e:
        raise AIError(f"Немає зв'язку з Gemini: {e}") from e
    if r.status_code == 429:
        raise AIError("Вичерпано безкоштовний ліміт Gemini — спробую пізніше")
    if r.status_code in (400, 401, 403):
        msg = ""
        try:
            msg = r.json().get("error", {}).get("message", "")
        except ValueError:
            pass
        if "API key" in msg or r.status_code in (401, 403):
            raise AIError(f"Ключ Gemini недійсний або без доступу: {msg[:200]}")
        raise AIError(f"Gemini відхилив запит: {msg[:300]}")
    if r.status_code == 404:
        raise AIError(f"Модель «{model}» не знайдено — змініть назву моделі в панелі (🤖 ШІ)")
    if r.status_code >= 400:
        raise AIError(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
    try:
        cand = (r.json().get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
        result = json.loads(text)
    except (ValueError, KeyError, IndexError, TypeError) as e:
        reason = (r.json().get("promptFeedback") or {}).get("blockReason") if r.headers.get(
            "content-type", "").startswith("application/json") else None
        raise AIError(f"Не вдалося розібрати відповідь Gemini{' (' + reason + ')' if reason else ''}") from e
    out = normalize(result, cfg, game, n_tags)
    log.info("ШІ: «%s» %s", out["title"], " ".join(out["hashtags"]))
    return out


def apply_to_json(js: str | Path, ai: dict) -> None:
    data = read_json(js)
    yt = data.get("youtube") if isinstance(data.get("youtube"), dict) else {}
    yt.update({"title": ai["title"], "description": ai["description"],
               "hashtags": ai["hashtags"], "tags": [t.lstrip("#") for t in ai["hashtags"]]})
    data["youtube"] = yt
    data["tiktok"] = {"caption": ai["tiktok"]}
    data["instagram"] = {"caption": ai["instagram"]}
    data.pop("facebook", None)
    data["ai_generated"] = True
    write_json(js, data)


def enrich(cfg: dict, item: dict) -> bool:
    """Замінює тексти «з імені файлу» на тексти від ШІ. True — якщо замінено."""
    data = read_json(item["json"])
    if not data.get("auto_generated") or data.get("ai_generated"):
        return False
    ai = generate(cfg, item["mp4"], item["game"])
    apply_to_json(item["json"], ai)
    return True
