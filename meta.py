"""Читання метаданих шортса (json від generate_meta.py) і робота з файлами."""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

HASHTAG_RE = re.compile(r"#[\w\d_]+", re.UNICODE)


@dataclass
class PostMeta:
    yt_title: str
    yt_description: str = ""
    yt_tags: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    tiktok_text: str = ""
    instagram_text: str = ""
    facebook_text: str = ""
    game: str = ""
    auto: bool = False  # тексти створено автоматично з імені файлу
    ai: bool = False    # тексти написало ШІ
    kind: str = "short"  # short — шортс; highlight — довге горизонтальне відео (YouTube, не Shorts)


def read_json(path: str | Path) -> dict:
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def write_json(path: str | Path, data: dict) -> None:
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _pick(d, *keys) -> str:
    if isinstance(d, str):
        return d.strip()
    if not isinstance(d, dict):
        return ""
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            return " ".join(v).strip()
    return ""


def _as_list(v) -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[,\n]", v) if "," in v or "\n" in v else v.split()
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()]


def _hashtags(v) -> list[str]:
    out = []
    for t in _as_list(v):
        t = t.strip()
        if not t:
            continue
        t = "#" + t.lstrip("#").replace(" ", "")
        if t not in out:
            out.append(t)
    return out


def _with_hashtags(text: str, tags: list[str]) -> str:
    """Додає хештеги в кінець тексту, якщо в тексті їх ще немає."""
    if not tags or HASHTAG_RE.search(text or ""):
        return text
    return (text + "\n\n" + " ".join(tags)).strip()


def json_status(data: dict) -> str:
    st = data.get("status")
    if isinstance(st, dict):
        st = st.get("state") or st.get("status")
    return str(st or "")


def load_meta(json_path: str | Path, title_override: str | None = None) -> PostMeta:
    """Гнучко читає формат generate_meta.py.

    Очікувано: {"youtube": {"title", "description", "tags", "hashtags"},
                "tiktok": {"caption"} | "текст", "instagram": {"caption"} | "текст",
                "facebook": (необов'язково, інакше береться instagram)}
    """
    data = read_json(json_path)
    yt = data.get("youtube") or data.get("yt") or {}
    tt = data.get("tiktok") or data.get("tt") or {}
    ig = data.get("instagram") or data.get("ig") or data.get("reels") or {}
    fb = data.get("facebook") or data.get("fb") or {}

    title = _pick(yt, "title", "name") or _pick(data, "title") or Path(json_path).stem
    desc = _pick(yt, "description", "desc", "text")
    tags = _as_list(yt.get("tags") if isinstance(yt, dict) else None) or _as_list(data.get("tags"))
    hashtags = _hashtags((yt.get("hashtags") if isinstance(yt, dict) else None) or data.get("hashtags"))
    if not hashtags:
        hashtags = _hashtags(HASHTAG_RE.findall(f"{title} {desc}"))

    tt_text = _pick(tt, "caption", "text", "description", "title")
    tt_tags = _hashtags(tt.get("hashtags")) if isinstance(tt, dict) else []
    ig_text = _pick(ig, "caption", "text", "description")
    ig_tags = _hashtags(ig.get("hashtags")) if isinstance(ig, dict) else []
    fb_text = _pick(fb, "caption", "text", "description")
    fb_tags = _hashtags(fb.get("hashtags")) if isinstance(fb, dict) else []

    tt_text = _with_hashtags(tt_text or title, tt_tags or hashtags)
    ig_text = _with_hashtags(ig_text or title, ig_tags or hashtags)
    fb_text = _with_hashtags(fb_text, fb_tags or ig_tags or hashtags) if fb_text else ig_text

    if title_override and title_override.strip() != title:
        # назва, змінена вручну, — у підписах усіх платформ, а не лише на YouTube
        new = title_override.strip()

        def retitle(text: str) -> str:
            if title and title in text:
                return text.replace(title, new, 1)
            tags = " ".join(HASHTAG_RE.findall(text or ""))
            return f"{new}\n\n{tags}".strip()
        tt_text, ig_text, fb_text = retitle(tt_text), retitle(ig_text), retitle(fb_text)

    return PostMeta(
        yt_title=(title_override or title).strip(),
        yt_description=desc,
        yt_tags=tags,
        hashtags=hashtags,
        tiktok_text=tt_text,
        instagram_text=ig_text,
        facebook_text=fb_text,
        game=str(data.get("game") or Path(json_path).parent.name),
        auto=bool(data.get("auto_generated")) and not data.get("ai_generated"),
        ai=bool(data.get("ai_generated")),
        kind="highlight" if str(data.get("kind") or "") == "highlight" else "short",
    )


GENERIC_NAME = re.compile(r"(?i)^(vid|video|clip|short|shorts|replay|obs|img|mov|record|recording|запис|відео)?[\s\d]*$")


# імена від автонарізки шортсів: «rec_battle_01_dmg_3837», «battle_02_assist_2650_dmg_900»
AUTOCUT_NAME = re.compile(r"(?i)^(?:.*\s)?battle \d+(?: (?:dmg|assist) \d+)*$")


def title_from_filename(stem: str, game: str = "", default_title: str = "") -> str:
    """«бій_на_прохорівці» -> «Бій на прохорівці»; безлике «2026-11-10 21-33-05» -> «WoT — епічний момент 🔥».
    default_title — своя назва для безликих файлів ({game} — назва гри), напр. «{game} — insane moment 🔥»."""
    name = re.sub(r"[_\-.]+", " ", stem)
    name = re.sub(r"\s+", " ", name).strip()
    letters = sum(ch.isalpha() for ch in name)
    if letters < 3 or GENERIC_NAME.match(name) or AUTOCUT_NAME.match(name):
        if default_title.strip():
            return default_title.replace("{game}", game or "").strip(" —-")
        return f"{game} — епічний момент 🔥" if game else "Епічний ігровий момент 🔥"
    return (name[:1].upper() + name[1:])[:100]


def game_hashtags(cfg: dict, game: str) -> list[str]:
    table = (cfg.get("auto_meta") or {}).get("hashtags") or {}
    for key, tags in table.items():
        if key != "*" and game and key.casefold() == game.casefold():
            return _hashtags(tags)
    return _hashtags(table.get("*") or [])


def auto_meta_json(mp4: Path, game: str, cfg: dict) -> dict:
    """json для відео, у якого його немає (замість generate_meta.py)."""
    title = title_from_filename(mp4.stem, game, str((cfg.get("auto_meta") or {}).get("default_title") or ""))
    tags = game_hashtags(cfg, game)
    line = " ".join(tags)
    return {
        "status": cfg.get("ready_status", "ready_to_post"),
        "auto_generated": True,
        "game": game,
        "youtube": {"title": title, "description": title, "tags": [t.lstrip("#") for t in tags], "hashtags": tags},
        "tiktok": {"caption": f"{title} {line}".strip()},
        "instagram": {"caption": f"{title}\n\n{line}".strip()},
    }


def highlight_meta_json(mp4: Path, cfg: dict) -> dict:
    """json для хайлайту без json (зазвичай його кладе shorts-autocut). Без #shorts і без ШІ."""
    name = re.sub(r"\s*·\s*хайлайт$", "", mp4.stem)
    title = title_from_filename(name, "WoT", "World of Tanks — бій повністю 🔥")
    tags = ["#wot", "#worldoftanks", "#танки"]
    return {
        "status": cfg.get("ready_status", "ready_to_post"),
        "kind": "highlight",
        "game": "WoT",
        "youtube": {"title": title, "description": title, "tags": [t.lstrip("#") for t in tags], "hashtags": tags},
    }


def _strip_tags(text: str, tags: list[str]) -> str:
    low = {t.lower() for t in tags}
    words = [w for w in re.split(r"(\s+)", text or "") if w.strip().lower() not in low]
    return re.sub(r"\s+", " ", "".join(words)).strip()


TRAILING_TAGS = re.compile(r"(#[\w\d_]+(?:\s+#[\w\d_]+)*)\s*$", re.UNICODE)


def _add_footer(text: str, footer: str) -> str:
    """Підпис каналу перед хештегами в кінці тексту (або просто в кінець). Без дублів."""
    text = (text or "").strip()
    if not footer or footer in text:
        return text
    m = TRAILING_TAGS.search(text)
    if m and m.start() > 0:
        return f"{text[:m.start()].rstrip()}\n\n{footer}\n\n{m.group(1)}".strip()
    if m:  # текст — самі хештеги
        return f"{footer}\n\n{m.group(1)}"
    return f"{text}\n\n{footer}".strip()


def apply_footer(meta: PostMeta, cfg: dict) -> PostMeta:
    """Постійний підпис каналу (config «footer»): в опис YouTube і в підписи TikTok / Instagram / Facebook."""
    conf = cfg.get("footer") or {}
    footer = str(conf.get("text") or "").strip()
    if not footer:
        return meta
    if conf.get("in_description", True):
        meta.yt_description = _add_footer(meta.yt_description, footer)
    if conf.get("in_captions", True):
        meta.tiktok_text = _add_footer(meta.tiktok_text, footer)
        meta.instagram_text = _add_footer(meta.instagram_text, footer)
        meta.facebook_text = _add_footer(meta.facebook_text, footer)
    return meta


def apply_always_hashtags(meta: PostMeta, cfg: dict) -> PostMeta:
    """Додає обов'язкові хештеги в кінець назви й підписів (без дублів; назва YouTube ≤ 100 символів).
    Спершу — постійний підпис каналу (footer), якщо він заданий."""
    meta = apply_footer(meta, cfg)
    conf = cfg.get("always_hashtags") or {}
    tags = _hashtags(conf.get("tags") or [])
    if not tags:
        return meta
    line = " ".join(tags)
    if conf.get("in_title", True):
        base = _strip_tags(meta.yt_title, tags)
        room = 100 - len(line) - 1
        if len(base) > room:
            base = base[:max(0, room - 1)].rstrip() + "…"
        meta.yt_title = f"{base} {line}".strip()
    if conf.get("in_captions", True):
        def add(text: str) -> str:
            present = {t.lower() for t in HASHTAG_RE.findall(text or "")}
            missing = [t for t in tags if t.lower() not in present]
            return f"{text} {' '.join(missing)}".strip() if missing else text
        meta.tiktok_text = add(meta.tiktok_text)
        meta.instagram_text = add(meta.instagram_text)
        meta.facebook_text = add(meta.facebook_text)
    # окремі хештеги (опис YouTube) — без тих, що вже стоять у назві
    if conf.get("in_title", True):
        low = {t.lower() for t in tags}
        meta.hashtags = [t for t in meta.hashtags if t.lower() not in low]
    return meta


def parse_hashtags(text: str) -> list[str]:
    """«wot, #танки shorts» -> ['#wot', '#танки', '#shorts']; «-» -> []."""
    text = (text or "").strip()
    if text in ("-", "—", "0", "без", "нема", "ні"):
        return []
    words = [w.strip().lstrip("#") for w in re.split(r"[\s,;]+", text)]
    return _hashtags([re.sub(r"[^\w]", "", w, flags=re.UNICODE) for w in words if w])


def apply_manual(json_path: str | Path, title: str, hashtags: list[str]) -> None:
    """Назва й хештеги, введені в Telegram, — для всіх платформ (записуються в json)."""
    data = read_json(json_path)
    meta = load_meta(json_path)
    line = " ".join(hashtags)

    def strip_tags(text: str) -> str:
        return re.sub(r"\s*#[\w\d_]+", "", text or "").strip()

    desc = strip_tags(meta.yt_description)
    yt = data.get("youtube") if isinstance(data.get("youtube"), dict) else {}
    yt.update({"title": title, "description": desc, "hashtags": hashtags,
               "tags": [t.lstrip("#") for t in hashtags]})
    data["youtube"] = yt
    data["tiktok"] = {"caption": f"{title} {line}".strip()}
    data["instagram"] = {"caption": f"{title}\n\n{line}".strip()}
    data.pop("facebook", None)
    data["manual_texts"] = True
    write_json(json_path, data)


def move_pair(mp4: str, json_path: str, dest_dir: Path) -> tuple[str, str]:
    """Переносить mp4 + json у dest_dir, уникаючи конфліктів імен."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    src_mp4, src_json = Path(mp4), Path(json_path)
    stem = src_mp4.stem
    n = 1
    while (dest_dir / f"{stem}.mp4").exists() or (dest_dir / f"{stem}.json").exists():
        n += 1
        stem = f"{src_mp4.stem}_{n}"
    new_mp4, new_json = dest_dir / f"{stem}.mp4", dest_dir / f"{stem}.json"
    if src_mp4.exists():
        shutil.move(str(src_mp4), str(new_mp4))
    if src_json.exists():
        shutil.move(str(src_json), str(new_json))
    return str(new_mp4), str(new_json)
