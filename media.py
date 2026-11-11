"""ffprobe / ffmpeg: розміри відео і стиснута прев'ю-копія для Telegram (ліміт Bot API 50 МБ)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("media")

TG_LIMIT = 49 * 1024 * 1024  # з невеликим запасом до 50 МБ


class MediaError(Exception):
    pass


def run_hidden(args: list[str], timeout: int = 900, cwd: str | None = None) -> subprocess.CompletedProcess:
    kw = {"cwd": cwd or None}
    if sys.platform == "win32":  # не показувати консольні вікна під pythonw
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout, **kw)
    except FileNotFoundError as e:
        raise MediaError(f"Не знайдено {args[0]} — встановіть ffmpeg і вкажіть шлях у config.json") from e
    except subprocess.TimeoutExpired as e:
        raise MediaError(f"{Path(args[0]).name}: перевищено час очікування") from e


def probe(path: str, ffprobe: str = "ffprobe") -> dict:
    """Повертає {'width', 'height', 'duration'} або {} якщо ffprobe недоступний."""
    try:
        r = run_hidden([ffprobe, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height:stream_tags=rotate:format=duration",
                        "-of", "json", str(path)], timeout=60)
        info = json.loads(r.stdout or b"{}")
        st = (info.get("streams") or [{}])[0]
        w, h = int(st.get("width") or 0), int(st.get("height") or 0)
        if str((st.get("tags") or {}).get("rotate", "0")) in ("90", "270", "-90"):
            w, h = h, w
        dur = float((info.get("format") or {}).get("duration") or 0)
        return {"width": w, "height": h, "duration": int(round(dur))}
    except (MediaError, ValueError) as e:
        log.warning("ffprobe недоступний або помилка (%s): %s", path, e)
        return {}


def quality(path: str, ffprobe: str = "ffprobe") -> dict:
    """Параметри оригіналу: роздільність, к/с, бітрейт, кодек, розмір — для показу в Telegram."""
    out = {"size_mb": round(os.path.getsize(path) / 1024 / 1024, 1)}
    try:
        r = run_hidden([ffprobe, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height,codec_name,avg_frame_rate,bit_rate:"
                        "stream_tags=rotate:format=duration,bit_rate", "-of", "json", str(path)], timeout=60)
        info = json.loads(r.stdout or b"{}")
    except (MediaError, ValueError):
        return out
    st = (info.get("streams") or [{}])[0]
    fmt = info.get("format") or {}
    w, h = int(st.get("width") or 0), int(st.get("height") or 0)
    if str((st.get("tags") or {}).get("rotate", "0")) in ("90", "270", "-90"):
        w, h = h, w
    fps = 0.0
    try:
        num, den = (st.get("avg_frame_rate") or "0/1").split("/")
        fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    except ValueError:
        pass
    br = int(st.get("bit_rate") or fmt.get("bit_rate") or 0)
    out.update(width=w, height=h, fps=round(fps, 2), codec=st.get("codec_name") or "",
               mbps=round(br / 1_000_000, 1), duration=int(round(float(fmt.get("duration") or 0))))
    return out


# Вимоги Instagram Reels (Content Publishing API)
IG_MAX_VIDEO_KBPS = 25_000
IG_MAX_AUDIO_KBPS = 128
IG_MAX_WIDTH = 1920
IG_MAX_MB = 300


def ig_probe(path: str, ffprobe: str = "ffprobe") -> dict:
    """Параметри, які перевіряє Instagram: відео й аудіо окремо."""
    try:
        r = run_hidden([ffprobe, "-v", "error", "-show_entries",
                        "stream=codec_type,codec_name,width,height,avg_frame_rate,bit_rate,sample_rate,channels,"
                        "pix_fmt:format=duration,bit_rate", "-of", "json", str(path)], timeout=60)
        info = json.loads(r.stdout or b"{}")
    except (MediaError, ValueError):
        return {}
    v = next((s for s in info.get("streams") or [] if s.get("codec_type") == "video"), {})
    a = next((s for s in info.get("streams") or [] if s.get("codec_type") == "audio"), {})
    fmt = info.get("format") or {}
    fps = 0.0
    try:
        n, d = (v.get("avg_frame_rate") or "0/1").split("/")
        fps = float(n) / float(d) if float(d) else 0.0
    except ValueError:
        pass
    total_k = int(fmt.get("bit_rate") or 0) // 1000
    a_k = int(a.get("bit_rate") or 0) // 1000
    return {"vcodec": v.get("codec_name"), "width": int(v.get("width") or 0), "height": int(v.get("height") or 0),
            "fps": fps, "pix_fmt": v.get("pix_fmt"),
            "vkbps": int(v.get("bit_rate") or 0) // 1000 or max(0, total_k - a_k),
            "acodec": a.get("codec_name"), "akbps": a_k, "arate": int(a.get("sample_rate") or 0),
            "channels": int(a.get("channels") or 0), "duration": float(fmt.get("duration") or 0),
            "size_mb": os.path.getsize(path) / 1024 / 1024}


def ig_problems(p: dict) -> list[str]:
    """Чому файл не підходить Instagram Reels (порожньо — підходить)."""
    if not p:
        return []
    out = []
    if p["vcodec"] not in ("h264", "hevc"):
        out.append(f"кодек відео {p['vcodec']}")
    if p["width"] > IG_MAX_WIDTH:
        out.append(f"ширина {p['width']} px > {IG_MAX_WIDTH}")
    if p["fps"] and not 23 <= p["fps"] <= 60.5:
        out.append(f"{p['fps']:.0f} к/с")
    if p["vkbps"] > IG_MAX_VIDEO_KBPS:
        out.append(f"відео {p['vkbps'] / 1000:.0f} Мбіт/с > 25")
    if p["pix_fmt"] and p["pix_fmt"] not in ("yuv420p", "yuvj420p"):
        out.append(f"формат пікселів {p['pix_fmt']}")
    if p["acodec"] and p["acodec"] != "aac":
        out.append(f"звук {p['acodec']}")
    if p["akbps"] > IG_MAX_AUDIO_KBPS:
        out.append(f"звук {p['akbps']} кбіт/с > 128")
    if p["arate"] > 48000 or p["channels"] > 2:
        out.append("звук понад 48 кГц / стерео")
    if p["size_mb"] > IG_MAX_MB:
        out.append(f"розмір {p['size_mb']:.0f} МБ > {IG_MAX_MB}")
    return out


def instagram_copy(path: str, cfg: dict) -> str:
    """Копія під вимоги Instagram Reels: H.264 High, yuv420p, ≤1080 по ширині (для 9:16 — 1080×1920),
    та сама частота кадрів, до 20 Мбіт/с, AAC 128 кбіт/с 48 кГц, faststart. Оригінал не змінюється."""
    cache = Path(cfg["paths"]["data"]) / "previews"
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(f"{path}|{os.path.getmtime(path)}".encode()).hexdigest()[:10]
    out = cache / f"{Path(path).stem}_{key}_ig.mp4"
    if out.exists() and out.stat().st_size > 0:
        return str(out)
    p = ig_probe(path, cfg["ffprobe"])
    fps = p.get("fps") or 30
    fps_args = [] if 23 <= fps <= 60.5 else ["-r", "30"]
    g = int(round(min(max(fps, 23), 60) * 2))
    vf = "scale='if(gt(iw,ih),min(iw,1920),min(iw,1080))':-2:flags=lanczos,format=yuv420p"
    args = [cfg["ffmpeg"], "-y", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vf", vf, *fps_args,
            "-c:v", "libx264", "-preset", "medium", "-profile:v", "high", "-crf", "17",
            "-maxrate", "20M", "-bufsize", "40M", "-g", str(g), "-keyint_min", str(g), "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(out)]
    log.info("Готую копію під вимоги Instagram: %s", path)
    r = run_hidden(args, timeout=3600)
    if r.returncode != 0:
        out.unlink(missing_ok=True)
        raise MediaError(f"ffmpeg (Instagram): {r.stderr.decode(errors='ignore')[-300:]}")
    return str(out)


def quality_line(q: dict) -> str:
    """«1080×1920 · 60 к/с · 24.5 Мбіт/с · h264 · 45.2 МБ» + попередження."""
    parts = []
    if q.get("width"):
        parts.append(f"{q['width']}×{q['height']}")
    if q.get("fps"):
        parts.append(f"{q['fps']:g} к/с")
    if q.get("mbps"):
        parts.append(f"{q['mbps']:g} Мбіт/с")
    if q.get("codec"):
        parts.append(q["codec"])
    parts.append(f"{q['size_mb']:g} МБ")
    warn = []
    w, h = q.get("width") or 0, q.get("height") or 0
    if w and h:
        if w >= h:
            warn.append("відео не вертикальне — Shorts/Reels/TikTok додадуть поля або обріжуть")
        elif abs(w / h - 9 / 16) > 0.02:
            warn.append(f"пропорції {w}:{h} не 9:16 — платформи можуть обрізати краї")
        if min(w, h) < 1080:
            warn.append(f"роздільність нижча за 1080p ({min(w, h)}p)")
    if (q.get("duration") or 0) > 180:
        warn.append(f"тривалість {q['duration']} с — YouTube Shorts до 3 хв, Reels до 90 с")
    return " · ".join(parts) + "".join(f"\n⚠️ {x}" for x in warn)


def telegram_copy(path: str, cfg: dict) -> str:
    """Що показати в Telegram: оригінал, якщо ≤ 50 МБ (ліміт Bot API).

    Інакше — копія найкращої якості, що вміщується в 50 МБ: роздільність як в оригіналі (до 1080p),
    та сама частота кадрів, максимальний бітрейт. На платформи завжди йде оригінал, не ця копія.
    """
    if os.path.getsize(path) <= TG_LIMIT:
        return path
    cache = Path(cfg["paths"]["data"]) / "previews"
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(f"{path}|{os.path.getmtime(path)}".encode()).hexdigest()[:10]
    out = cache / f"{Path(path).stem}_{key}_tg.mp4"
    if out.exists() and 0 < out.stat().st_size <= TG_LIMIT:
        return str(out)

    q = quality(path, cfg["ffprobe"])
    dur = max(1, q.get("duration") or probe(path, cfg["ffprobe"]).get("duration") or 60)
    audio_k = 160
    budget = int(TG_LIMIT * 8 * 0.94 / dur / 1000) - audio_k  # кбіт/с на відео, щоб уміститися в 50 МБ
    src_k = int((q.get("mbps") or 0) * 1000) or 50_000
    kbps = max(400, min(budget, src_k, 16_000))
    short_side = min(q.get("width") or 1080, q.get("height") or 1920) or 1080
    target = min(short_side, 1080 if kbps >= 2500 else 720)  # дуже довге відео — 720p, щоб не «сипалось»
    vf = ["-vf", f"scale='if(gt(iw,ih),-2,{target})':'if(gt(iw,ih),{target},-2)':flags=lanczos"] \
        if target < short_side else []
    log.info("Файл %s (%.0f МБ) > 50 МБ — готую копію для Telegram: %sp, %s кбіт/с (публікується оригінал)",
             path, q.get("size_mb", 0), target, kbps)
    for _ in range(4):
        args = [cfg["ffmpeg"], "-y", "-hide_banner", "-loglevel", "error", "-i", str(path), *vf,
                "-c:v", "libx264", "-preset", "medium", "-profile:v", "high", "-pix_fmt", "yuv420p",
                "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.1)}k", "-bufsize", f"{kbps * 2}k",
                "-c:a", "aac", "-b:a", f"{audio_k}k", "-movflags", "+faststart", str(out)]
        r = run_hidden(args, timeout=1800)
        if r.returncode != 0:
            raise MediaError(f"ffmpeg: {r.stderr.decode(errors='ignore')[-300:]}")
        if out.stat().st_size <= TG_LIMIT:
            return str(out)
        kbps = int(kbps * 0.85)  # трохи не вмістилось — зменшуємо бітрейт і пробуємо ще
    raise MediaError("Не вдалося вмістити копію для Telegram у 50 МБ")


def drop_previews(path: str, cfg: dict) -> None:
    cache = Path(cfg["paths"]["data"]) / "previews"
    for p in [*cache.glob(f"{Path(path).stem}_*_720p.mp4"), *cache.glob(f"{Path(path).stem}_*_tg.mp4"),
              *cache.glob(f"{Path(path).stem}_*_ig.mp4")]:
        try:
            p.unlink()
        except OSError:
            pass
