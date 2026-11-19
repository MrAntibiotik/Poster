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
    if sys.platform == "win32":  # не показувати консольні вікна під pythonw; низький пріоритет — гра/стрім не гальмують
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
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


# Вимоги Instagram Reels (Content Publishing API, довідник IG User Media → Reel Specifications).
# Офіційні: MP4/MOV, H.264/HEVC, AAC до 48 кГц моно/стерео, 23–60 к/с, тривалість 3 с – 15 хв.
# Ширина ≤1920, відео ≤25 Мбіт/с, звук ≤128 кбіт/с — з тієї ж таблиці; 300 МБ — за сторонніми джерелами
# (на сторінці Meta з цієї сесії не перевірено). Внутрішні налаштування Poster — окремо (IG_TARGET_MB нижче).
IG_MAX_VIDEO_KBPS = 25_000
IG_MAX_AUDIO_KBPS = 128
IG_MAX_WIDTH = 1920
IG_MAX_MB = 300
IG_MIN_SECONDS = 3
IG_MAX_DURATION = 15 * 60


def ig_probe(path: str, ffprobe: str = "ffprobe") -> dict:
    """Параметри, які перевіряє Instagram: відео й аудіо окремо."""
    try:
        r = run_hidden([ffprobe, "-v", "error", "-show_entries",
                        "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,bit_rate,sample_rate,channels,"
                        "pix_fmt,color_transfer,color_primaries,color_space:format=duration,bit_rate,format_name", "-of", "json", str(path)],
                       timeout=60)
        info = json.loads(r.stdout or b"{}")
    except (MediaError, ValueError):
        return {}
    v = next((s for s in info.get("streams") or [] if s.get("codec_type") == "video"), {})
    a = next((s for s in info.get("streams") or [] if s.get("codec_type") == "audio"), {})
    if not v:
        return {}  # не вдалося прочитати — перевірку пропускаємо, завантажуємо як є
    fmt = info.get("format") or {}
    fps = 0.0
    try:
        n, d = (v.get("avg_frame_rate") or "0/1").split("/")
        fps = float(n) / float(d) if float(d) else 0.0
    except ValueError:
        pass
    total_k = int(fmt.get("bit_rate") or 0) // 1000
    a_k = int(a.get("bit_rate") or 0) // 1000
    try:
        rn, rd = (v.get("r_frame_rate") or "0/1").split("/")
        rfps = float(rn) / float(rd) if float(rd) else 0.0
    except ValueError:
        rfps = 0.0
    return {"vcodec": v.get("codec_name"), "width": int(v.get("width") or 0), "height": int(v.get("height") or 0),
            "fps": fps, "vfr": bool(rfps and fps and abs(rfps - fps) > 0.5), "pix_fmt": v.get("pix_fmt"),
            "vkbps": int(v.get("bit_rate") or 0) // 1000 or max(0, total_k - a_k),
            "acodec": a.get("codec_name"), "akbps": a_k, "arate": int(a.get("sample_rate") or 0),
            "channels": int(a.get("channels") or 0), "duration": float(fmt.get("duration") or 0),
            "trc": v.get("color_transfer") or "", "primaries": v.get("color_primaries") or "",
            "colorspace": v.get("color_space") or "", "format": fmt.get("format_name") or "",
            "size_mb": os.path.getsize(path) / 1024 / 1024}


def is_hdr(p: dict) -> bool:
    """HDR / широка гама (BT.2020, PQ, HLG) — Instagram такі Reels не приймає."""
    return (p.get("trc") in ("smpte2084", "arib-std-b67") or p.get("primaries") == "bt2020"
            or str(p.get("colorspace") or "").startswith("bt2020"))


def ig_summary(p: dict) -> str:
    """Коротко про файл — щоб порівняти відео, яке Instagram не прийняв, з тими, що пройшли."""
    if not p:
        return ""
    m, s = divmod(int(p.get("duration") or 0), 60)
    parts = [f"{p['width']}×{p['height']}", f"{p['fps']:.0f} к/с" + (" (змінна частота!)" if p.get("vfr") else ""),
             f"{p['vkbps'] / 1000:.1f} Мбіт/с", f"{m}:{s:02d}", f"{p['size_mb']:.0f} МБ", str(p.get("pix_fmt") or "")]
    if is_hdr(p):
        parts.append(f"HDR ({p.get('trc')}/{p.get('primaries')})!")
    elif p.get("trc") or p.get("primaries"):
        parts.append(f"колір {p.get('primaries') or '?'}/{p.get('trc') or '?'}")
    if p.get("acodec"):
        parts.append(f"звук {p['acodec']} {p['akbps']} кбіт/с {p['arate'] // 1000} кГц")
    else:
        parts.append("без звуку")
    return ", ".join(x for x in parts if x)


def ig_duration_problem(p: dict) -> str:
    """Тривалість поза офіційними межами Reels API (3 с – 15 хв). Це не виправити перекодуванням (не обрізаємо)."""
    d = float((p or {}).get("duration") or 0)
    if not d:
        return ""
    if d < IG_MIN_SECONDS:
        return f"тривалість {d:.1f} с — менше за {IG_MIN_SECONDS} с (мінімум Reels API)"
    if d > IG_MAX_DURATION:
        return f"тривалість {d / 60:.1f} хв — більше за 15 хв (максимум Reels API)"
    return ""


def ig_problems(p: dict) -> list[str]:
    """Чому файл не підходить Instagram Reels (порожньо — підходить)."""
    if not p:
        return []
    out = []
    fmt = str(p.get("format") or "")
    if fmt and not any(x in fmt for x in ("mp4", "mov")):
        out.append(f"контейнер {fmt}")
    if p["vcodec"] not in ("h264", "hevc"):
        out.append(f"кодек відео {p['vcodec']}")
    if p["width"] > IG_MAX_WIDTH:
        out.append(f"ширина {p['width']} px > {IG_MAX_WIDTH}")
    if p["fps"] and not 23 <= p["fps"] <= 60.5:
        out.append(f"{p['fps']:.0f} к/с")
    if p.get("vfr"):
        out.append("змінна частота кадрів (VFR)")
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
    if is_hdr(p):
        out.append(f"HDR / BT.2020 ({p.get('trc')})")
    return out


# Внутрішнє налаштування Poster (НЕ вимога Meta): цільовий розмір копії. 16.11 rupload відкидав копії 85–89 МБ,
# ~45 МБ проходили. Змінюється в config: platforms.instagram.copy_target_mb.
IG_TARGET_MB = 45
# Рішення користувача (НЕ вимога Meta — Reels API приймає до 15 хв): шортси до 60 с, тому копія для Instagram
# обрізається до 59,5 с. Налаштування platforms.instagram.max_seconds (0 — не обрізати). Оригінал не змінюється.
IG_MAX_SECONDS = 59.5
IG_COPY_VERSION = "v5"   # змінюється, коли змінюється спосіб копіювання: старі копії не беруться


def ig_max_seconds(cfg: dict) -> float:
    """Межа тривалості копії для Instagram (с); 0 — без обрізання."""
    v = ((cfg.get("platforms") or {}).get("instagram") or {}).get("max_seconds", IG_MAX_SECONDS)
    try:
        return max(0.0, float(v or 0))
    except (TypeError, ValueError):
        return IG_MAX_SECONDS


def instagram_copy(path: str, cfg: dict, lite: bool = False, mode: str = "") -> str:
    """mode: '' — копія під вимоги; 'safe60' — сувора копія з тією ж частотою кадрів (≤10 Мбіт/с);
    'safe30' (= lite) — сувора копія 30 к/с, 6 Мбіт/с. Завжди 1080 по ширині (720p — надто погана якість)."""
    mode = mode or ("safe30" if lite else "")
    lite = mode == "safe30"
    strict = mode in ("safe60", "safe30")
    """Копія під вимоги Instagram Reels: H.264 High, yuv420p, ≤1080 по ширині (для 9:16 — 1080×1920),
    та сама частота кадрів, до 20 Мбіт/с, AAC 128 кбіт/с 48 кГц, faststart. Оригінал не змінюється."""
    cache = Path(cfg["paths"]["data"]) / "previews"
    cache.mkdir(parents=True, exist_ok=True)
    target = float((cfg.get("platforms") or {}).get("instagram", {}).get("copy_target_mb") or IG_TARGET_MB)
    limit = ig_max_seconds(cfg)
    key = hashlib.md5(f"{path}|{os.path.getmtime(path)}|{target}|{IG_COPY_VERSION}|t{limit:g}".encode()).hexdigest()[:10]
    out = cache / f"{Path(path).stem}_{key}_{ {'safe60': 'ig60', 'safe30': 'igl'}.get(mode, 'ig') }.mp4"
    if out.exists() and out.stat().st_size > 0:
        return str(out)
    p = ig_probe(path, cfg["ffprobe"])
    fps = p.get("fps") or 30
    if lite:  # «безпечна» копія: 30 к/с рівно, рівень H.264 4.1, без метаданих — Instagram перекодовує її без збоїв
        fps = 30
    # сталий темп кадрів (CFR); частота — як в оригіналі в межах 23–60: вище 60 → 60, нижче 23 → 30
    if lite or fps < 23:
        fps_args = ["-r", "30", "-vsync", "cfr"]
    elif fps > 60.5:
        fps_args = ["-r", "60", "-vsync", "cfr"]
    else:
        fps_args = ["-vsync", "cfr"]
    g = int(round(min(max(fps, 23), 60) * 2))
    vf = "scale='if(gt(iw,ih),min(iw,1920),min(iw,1080))':-2:flags=lanczos,setsar=1,format=yuv420p"
    if is_hdr(p) and cfg.get("_ig_tonemap", True):   # HDR → SDR (Instagram HDR-Reels через API не приймає)
        vf = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
              "zscale=t=bt709:m=bt709:r=tv," + vf)
    level = ["-level:v", "4.1"] if lite else ["-level:v", "4.2"]
    cap = 6000 if lite else 10000 if strict else 20000            # кбіт/с
    dur = float(p.get("duration") or 0)
    trim = ["-t", f"{limit:g}"] if limit and dur > limit else []   # шортс — до 59,5 с (налаштування max_seconds)
    if trim:
        dur = limit
    if dur > 0:   # файл має вийти ≤ target МБ: великі файли Instagram відкидає (rupload 400)
        cap = min(cap, max(2500, int(target * 8 * 1024 / dur) - 160))
    crf = "21" if lite else "19" if strict else "17"
    rate = ["-crf", crf, "-maxrate", f"{cap}k", "-bufsize", f"{cap * 2}k"]
    args = [cfg["ffmpeg"], "-y", "-hide_banner", "-loglevel", "error", "-i", str(path), *trim,
            "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1", "-vf", vf, *fps_args, *level,
            "-c:v", "libx264", "-preset", "medium", "-profile:v", "high", *rate, "-g", str(g), "-keyint_min", str(g), "-sc_threshold", "0",
            # завжди позначки SDR BT.709: без них (або з BT.2020/PQ від джерела) Instagram відкидає відео
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
            # звук ≤128 кбіт/с (вимога Instagram): з «128k» кодер AAC давав 130–131 — тому 112k
            "-c:a", "aac", "-b:a", "112k", "-ar", "48000", "-ac", "2", "-tag:v", "avc1",
            "-movflags", "+faststart", str(out)]
    log.info("Готую копію для Instagram (%s): %s%s", mode or "під вимоги", path,
             " — HDR → SDR" if is_hdr(p) else "")
    r = run_hidden(args, timeout=3600)
    if r.returncode != 0 and "zscale" in vf and b"zscale" in (r.stderr or b""):   # ffmpeg без zimg — без тонмапінгу
        log.warning("ffmpeg без zscale — HDR лише перепозначається як SDR (кольори можуть бути блідими)")
        return instagram_copy(path, {**cfg, "_ig_tonemap": False}, lite=lite, mode=mode)
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
    """Що показати в Telegram: швидка копія 720p (налаштування telegram.preview_height, 0 — оригінал).

    Це лише попередній перегляд для бота. На платформи завжди йде оригінал, не ця копія.
    """
    limit_h = int((cfg.get("telegram") or {}).get("preview_height", 720) or 0)
    size = os.path.getsize(path)
    q = quality(path, cfg["ffprobe"])
    short_side = min(q.get("width") or 1080, q.get("height") or 1920) or 1080
    target = min(short_side, limit_h) if limit_h else short_side
    if size <= TG_LIMIT and target >= short_side:
        return path  # і так маленьке — надсилаємо як є
    cache = Path(cfg["paths"]["data"]) / "previews"
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(f"{path}|{os.path.getmtime(path)}|{target}|sdr2|t59.5".encode()).hexdigest()[:10]
    out = cache / f"{Path(path).stem}_{key}_tg.mp4"
    if out.exists() and 0 < out.stat().st_size <= TG_LIMIT:
        return str(out)

    dur = max(1, q.get("duration") or probe(path, cfg["ffprobe"]).get("duration") or 60)
    audio_k = 128
    budget = int(TG_LIMIT * 8 * 0.94 / dur / 1000) - audio_k  # кбіт/с на відео, щоб уміститися в 50 МБ
    src_k = int((q.get("mbps") or 0) * 1000) or 50_000
    cap = 4_000 if target <= 720 else 16_000
    kbps = max(400, min(budget, src_k, cap))
    if kbps < 2500 and target > 720:
        target = 720  # дуже довге відео — 720p, щоб не «сипалось»
    vf = ["-vf", f"scale='if(gt(iw,ih),-2,{target})':'if(gt(iw,ih),{target},-2)':flags=lanczos"] \
        if target < short_side else []
    log.info("Готую копію для Telegram: %s → %sp, %s кбіт/с (на платформи йде оригінал)", path, target, kbps)
    for _ in range(4):
        args = [cfg["ffmpeg"], "-y", "-hide_banner", "-loglevel", "error", "-i", str(path), *vf,
                "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-pix_fmt", "yuv420p",
                "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.1)}k", "-bufsize", f"{kbps * 2}k",
                "-c:a", "aac", "-b:a", f"{audio_k}k", "-movflags", "+faststart", str(out)]
        r = run_hidden(args, timeout=1800)
        if r.returncode != 0:
            if size <= TG_LIMIT:
                log.warning("Копія для Telegram не вдалася, надсилаю оригінал: %s", r.stderr.decode(errors="ignore")[-200:])
                return path
            raise MediaError(f"ffmpeg: {r.stderr.decode(errors='ignore')[-300:]}")
        if out.stat().st_size <= TG_LIMIT:
            return str(out)
        kbps = int(kbps * 0.85)  # трохи не вмістилось — зменшуємо бітрейт і пробуємо ще
    raise MediaError("Не вдалося вмістити копію для Telegram у 50 МБ")


def drop_previews(path: str, cfg: dict) -> None:
    cache = Path(cfg["paths"]["data"]) / "previews"
    for p in [*cache.glob(f"{Path(path).stem}_*_720p.mp4"), *cache.glob(f"{Path(path).stem}_*_tg.mp4"),
              *cache.glob(f"{Path(path).stem}_*_ig.mp4"),
              *cache.glob(f"{Path(path).stem}_*_igl.mp4"), *cache.glob(f"{Path(path).stem}_*_ig60.mp4")]:
        try:
            p.unlink()
        except OSError:
            pass


FILE_SETTLE = 2.0   # скільки секунд розмір файлу має не змінюватися перед завантаженням


def file_check(path: str, settle: float | None = None) -> dict:
    """Перевірка файлу перед завантаженням: існує, не порожній, повністю читається, розмір не змінюється
    (ніхто не дописує / не синхронізує — напр. OneDrive). Повертає {"size", "mtime", "md5"}; MediaError — якщо ні."""
    import time as _t
    p = Path(path)
    if not p.is_file():
        raise MediaError(f"файлу немає: {path}")
    st1 = p.stat()
    if st1.st_size == 0:
        raise MediaError(f"файл порожній: {path}")
    _t.sleep(FILE_SETTLE if settle is None else settle)
    st2 = p.stat()
    if (st2.st_size, st2.st_mtime) != (st1.st_size, st1.st_mtime):
        raise MediaError(f"файл ще змінюється (дописується або синхронізується): {path}")
    h, n = hashlib.md5(), 0
    try:
        with open(p, "rb") as f:
            for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
                h.update(block)
                n += len(block)
    except OSError as e:
        raise MediaError(f"файл не читається: {e}") from e
    if n != st2.st_size:
        raise MediaError(f"прочитано {n} з {st2.st_size} байт — файл недоступний повністю: {path}")
    return {"size": st2.st_size, "mtime": st2.st_mtime, "md5": h.hexdigest()}


def file_unchanged(path: str, before: dict) -> bool:
    try:
        st = Path(path).stat()
    except OSError:
        return False
    return (st.st_size, st.st_mtime) == (before["size"], before["mtime"])
