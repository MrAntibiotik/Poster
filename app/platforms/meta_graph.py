"""Instagram Reels і Facebook Reels через офіційний Graph API (завантаження файлу з ПК)."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

from .base import Platform, PublishError, RateLimited, Stopped, TokenExpired

log = logging.getLogger("meta")

TOKEN_ERROR_CODES = {102, 190, 463, 467}
# Ліміти запитів Meta: 4 — додатка (≈200 запитів/год на користувача), 17 — користувача, 32 — сторінки,
# 613 — частота, 80001–80014 — ліміти Business Use Case. Вікно — ковзна година.
RATE_LIMIT_CODES = {4, 17, 32, 613} | set(range(80001, 80015))
LIMIT_FILE = Path(tempfile.gettempdir()) / "poster-meta-limit.json"   # спільний для всіх каналів (один додаток Meta)
POLL_DELAYS = (10, 20, 40, 80)   # перевірки стану контейнера: затримка наростає, далі — раз на 80 с
AFTER_400_WAIT = 8 * 60   # після «rupload 400» довго чекати марно — далі наступний варіант файлу
STATS_MAX_USAGE = 60      # статистику не знімаємо, якщо використано ≥ N% годинної квоти — бережемо для публікацій


def _limit_state() -> dict:
    try:
        return json.loads(LIMIT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_limit(**kw) -> None:
    st = _limit_state()
    st.update(kw)
    try:
        LIMIT_FILE.write_text(json.dumps(st), encoding="utf-8")
    except OSError as e:
        log.debug("meta limit file: %s", e)


def blocked_until() -> int:
    """До якого часу Meta не приймає запитів (0 — обмеження немає)."""
    until = int(_limit_state().get("until") or 0)
    return until if until > time.time() else 0


def usage_pct() -> int:
    """Скільки відсотків годинної квоти використано (за останньою відповіддю Meta, не старшою за годину)."""
    st = _limit_state()
    if time.time() - float(st.get("usage_ts") or 0) > 3600:
        return 0
    return int(st.get("usage") or 0)


def _read_usage(r) -> tuple[int, int]:
    """(відсоток використання, хвилин до відновлення) із заголовків X-App-Usage / X-Business-Use-Case-Usage."""
    pct, regain = 0, 0
    headers = getattr(r, "headers", None) or {}
    for name in ("X-App-Usage", "X-Ad-Account-Usage", "X-Page-Usage"):
        raw = headers.get(name)
        if raw:
            try:
                d = json.loads(raw)
                pct = max([pct] + [int(v) for k, v in d.items() if isinstance(v, (int, float))])
            except (ValueError, TypeError):
                pass
    raw = headers.get("X-Business-Use-Case-Usage")
    if raw:
        try:
            for entries in json.loads(raw).values():
                for e in entries:
                    pct = max(pct, int(e.get("call_count") or 0), int(e.get("total_time") or 0),
                              int(e.get("total_cputime") or 0))
                    regain = max(regain, int(e.get("estimated_time_to_regain_access") or 0))
        except (ValueError, TypeError, AttributeError):
            pass
    return pct, regain


def _hm(ts: int) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


IG_HINTS = {
    "2207026": "формат відео не підтримується (потрібно MP4 H.264/AAC)",
    "2207020": "відео обробляється задовго або сесія завантаження застаріла",
    "2207003": "Instagram не зміг отримати файл",
    "2207042": "перевищено ліміт публікацій через API за добу",
    "2207050": "акаунт Instagram обмежений або неактивний",
    "2207051": "дію заблоковано Instagram (підозріла активність) — спробуйте пізніше",
    "2207004": "файл завеликий",
    "2207009": "непідтримувані пропорції (потрібно 9:16)",
}


def short_error(e) -> str:
    """Суть помилки Instagram в одному рядку (без вкладеного JSON)."""
    import re
    t = str(e)
    for _ in range(3):
        t = t.replace('\\\\', '\\').replace('\\"', '"')
    parts = []
    m = re.search(r"HTTP (\d{3})", t)
    if m:
        parts.append(f"HTTP {m.group(1)}")
    m = re.search(r"(Video Transcoding Error[^\"\\}]{0,70})", t)
    if m:
        parts.append(m.group(1).strip())
    elif "ProcessingFailedError" in t:
        parts.append("ProcessingFailedError")
    m = re.search(r"rupload отримав [\d.]+ з [\d.]+ МБ", t)
    if m:
        parts.append(m.group(0) + " — файл не дійшов до Instagram")
    m = re.search(r"стан контейнера: ([^|(]{0,90})", t)
    if m:
        parts.append("стан: " + m.group(1).strip())
    m = re.search(r"\(чекали \d+ хв\)", t)
    if m:
        parts.append(m.group(0))
    return ", ".join(parts) if parts else t[:150]


def ig_hint(text) -> str:
    text = str(text or "")
    for code, hint in IG_HINTS.items():
        if code in text:
            return f" → {hint}"
    return ""


class Graph:
    def __init__(self, version: str, token: str):
        if not token:
            raise TokenExpired("не задано META_PAGE_TOKEN")
        self.version = version
        self.token = token
        self.base = f"https://graph.facebook.com/{version}"

    def _check(self, r: requests.Response) -> dict:
        pct, regain = _read_usage(r)
        if pct:
            _save_limit(usage=pct, usage_ts=time.time())
            st = _limit_state()
            if pct >= 90 and (pct >= int(st.get("warned_pct") or 0) + 5 or time.time() - float(st.get("warned_ts") or 0) > 1800):
                _save_limit(warned_pct=pct, warned_ts=time.time())   # без спаму: раз на +5% або раз на 30 хв
                log.warning("Meta: використано %s%% годинної квоти запитів — публікації сповільнюються, "
                            "статистика відкладена", pct)
        try:
            j = r.json()
        except ValueError:
            raise PublishError(f"Graph API HTTP {r.status_code}: {r.text[:200]}") from None
        err = j.get("error") if isinstance(j, dict) else None
        if err:
            code = err.get("code")
            msg = err.get("error_user_msg") or err.get("message") or str(err)
            if code in TOKEN_ERROR_CODES:
                raise TokenExpired(f"Meta: {msg}")
            if code in RATE_LIMIT_CODES or err.get("error_subcode") in RATE_LIMIT_CODES:
                until = int(time.time()) + max(15, regain or 60) * 60   # ковзне вікно — година, якщо Meta не сказала
                _save_limit(until=until, usage=100, usage_ts=time.time())
                log.warning("Meta: ліміт запитів (%s) — пауза до %s", code, _hm(until))
                raise RateLimited(f"Meta: вичерпано ліміт запитів ({code}) — повтор після {_hm(until)}", until)
            raise PublishError(f"Meta ({code}/{err.get('error_subcode')}): {msg}")
        if r.status_code >= 400:
            raise PublishError(f"Graph API HTTP {r.status_code}: {r.text[:200]}")
        return j

    def _guard(self, low: bool = False) -> None:
        """Не стукаємо в Meta, поки діє обмеження (інакше вікно ліміту лише подовжується)."""
        until = blocked_until()
        if until:
            raise RateLimited(f"Meta: вичерпано ліміт запитів — повтор після {_hm(until)}", until)
        if low and usage_pct() >= STATS_MAX_USAGE:
            raise RateLimited(f"Meta: використано {usage_pct()}% квоти — статистику відкладено, щоб вистачило "
                              "на публікації", int(time.time()) + 3600)

    def get(self, path: str, low: bool = False, **params) -> dict:
        self._guard(low)
        params["access_token"] = self.token
        try:
            r = requests.get(f"{self.base}/{path.lstrip('/')}", params=params, timeout=60)
        except requests.RequestException as e:
            raise PublishError(f"мережа: {e}") from e
        return self._check(r)

    def post(self, path: str, **data) -> dict:
        self._guard()
        data["access_token"] = self.token
        try:
            r = requests.post(f"{self.base}/{path.lstrip('/')}", data=data, timeout=120)
        except requests.RequestException as e:
            raise PublishError(f"мережа: {e}") from e
        return self._check(r)

    def upload_video_unpublished(self, page_id: str, path: str, description: str = "") -> dict:
        """Відео на сторінку Facebook БЕЗ публікації (його ніхто не бачить) — щоб мати посилання на файл."""
        self._guard()
        try:
            with open(path, "rb") as f:
                r = requests.post(f"https://graph-video.facebook.com/{self.version}/{page_id}/videos",
                                  data={"published": "false", "no_story": "true", "description": description,
                                        "access_token": self.token},
                                  files={"source": (os.path.basename(path), f, "video/mp4")}, timeout=1800)
        except requests.RequestException as e:
            raise PublishError(f"завантаження на Facebook: {e}") from e
        return self._check(r)

    def delete(self, path: str) -> dict:
        try:
            r = requests.delete(f"{self.base}/{path.lstrip('/')}", params={"access_token": self.token}, timeout=60)
        except requests.RequestException as e:
            raise PublishError(f"мережа: {e}") from e
        return self._check(r)

    def upload_binary(self, url: str, path: str, chunk_mb: float = 0) -> dict:
        """rupload. chunk_mb > 0 — частинами зі зсувом (offset): один довгий запит Instagram обриває з 400."""
        size = os.path.getsize(path)
        chunk = int(chunk_mb * 1024 * 1024) if chunk_mb and chunk_mb > 0 else 0
        self.last_upload = {"bytes": size, "url_host": url.split("/")[2] if "//" in url else url,
                            "chunk_mb": chunk_mb or 0, "chunks": []}
        t0 = time.time()
        offset, last = 0, None
        try:
            with open(path, "rb") as f:
                while True:
                    n = (size - offset) if not chunk else min(chunk, size - offset)
                    data = f.read(n) if chunk else f
                    headers = {"Authorization": f"OAuth {self.token}", "offset": str(offset), "file_size": str(size)}
                    tc = time.time()
                    r = requests.post(url, headers=headers, data=data, timeout=1800)
                    code = getattr(r, "status_code", None)
                    self.last_upload["chunks"].append([offset, n, round(time.time() - tc, 1), code])
                    last = r
                    if not chunk or code not in (200, 201, 206):   # 206 = «частину прийнято, давай далі»
                        break
                    offset += n
                    self.last_upload["sent"] = offset
                    if offset >= size:
                        break
        except requests.RequestException as e:
            self.last_upload.update(seconds=round(time.time() - t0, 1), error=str(e)[:300], sent=offset)
            raise PublishError(f"завантаження файлу: {e}") from e
        sec = max(0.1, time.time() - t0)
        hdr = getattr(last, "headers", None) or {}
        self.last_upload.update(
            seconds=round(sec, 1), http=getattr(last, "status_code", None), mbps=round(size * 8 / sec / 1e6, 1),
            body=(getattr(last, "text", "") or "")[:400], sent=offset if chunk else size,
            trace={k: hdr.get(k) for k in ("x-fb-trace-id", "x-fb-rev", "x-fb-debug") if hdr.get(k)})
        return self._check(last)

    def upload_status(self, url: str) -> dict:
        """Скільки байтів rupload реально отримав (GET того ж URI). Для діагностики; помилки — не критичні."""
        try:
            r = requests.get(url, headers={"Authorization": f"OAuth {self.token}"}, timeout=30)
            try:
                return {"http": r.status_code, **(r.json() if isinstance(r.json(), dict) else {"body": r.text[:200]})}
            except ValueError:
                return {"http": r.status_code, "body": (r.text or "")[:200]}
        except Exception as e:  # noqa: BLE001 — лише діагностика
            return {"error": str(e)[:200]}


class _MetaBase(Platform):
    reauth_cmd = "python poster.py auth meta"

    @property
    def reauth_hint(self) -> str:
        return self.reauth_cmd

    def graph(self, token: str | None = None) -> Graph:
        return Graph(self.cfg["graph_api_version"], token or self.secrets["META_PAGE_TOKEN"])


class Instagram(_MetaBase):
    key = "instagram"
    name = "Instagram"

    def _g(self) -> Graph:
        return self.graph(self.secrets.get("META_IG_TOKEN") or None)

    @property
    def ig_id(self) -> str:
        ig = self.secrets["IG_USER_ID"]
        if not ig:
            raise PublishError("не задано IG_USER_ID")
        return ig

    def publish(self, video_path: str, meta) -> dict:
        """Публікація Reels з кількома спробами, бо Instagram буває вибагливим до завантаження:
        файл — оригінал (якщо відповідає вимогам Reels), потім копія під вимоги Instagram;
        токен — токен сторінки, потім токен користувача (у прикладах Meta саме він).
        Помилка 400 від rupload не означає провал: спершу перевіряємо стан контейнера."""
        # по одному відео за раз: паралельні великі завантаження Instagram відхиляє; поки чекаємо — слухаємо «Стоп»
        while not Instagram._upload_lock.acquire(timeout=3):
            self._check_stop()
        Instagram.current = video_path   # для «🕐 Статус»: що саме зараз вантажиться в Instagram
        try:
            self._check_stop()
            return self._publish_locked(video_path, meta)
        finally:
            Instagram.current = None
            Instagram._upload_lock.release()

    current: str | None = None

    def _check_stop(self) -> None:
        if self.should_stop():
            raise Stopped("публікації зупинено")

    _upload_lock = threading.Lock()

    def _publish_locked(self, video_path: str, meta) -> dict:
        from app.core import media
        try:
            problems = media.ig_problems(media.ig_probe(video_path, self.cfg.get("ffprobe", "ffprobe")))
        except OSError:
            problems = []
        tokens = [("сторінки", None)]
        user = self.secrets.get("META_USER_TOKEN")
        if user and user != self._g().token:
            tokens.append(("користувача", user))
        size_mb = os.path.getsize(video_path) / 1024 / 1024
        limit = float(self.pcfg.get("max_upload_mb", 100) or 10 ** 6)
        limit = min(limit, float(self.pcfg.get("copy_target_mb") or media.IG_TARGET_MB) + 5)   # більші — лише копією
        big = size_mb > limit
        # Порядок спроб: оригінал (якщо легкий і за вимогами) → копія під вимоги Reels (вона вже не раз
        # проходила і з важкими файлами) → сувора копія 10 Мбіт/с → крайній варіант 30 к/с.
        files = ([] if problems or big else [("оригінал", lambda: video_path)]) + \
            [("копія під вимоги Reels", lambda: media.instagram_copy(video_path, self.cfg))] + \
            [("сувора копія (та ж частота кадрів, 10 Мбіт/с)",
              lambda: media.instagram_copy(video_path, self.cfg, mode="safe60")),
             ("крайній варіант (30 к/с, 6 Мбіт/с)", lambda: media.instagram_copy(video_path, self.cfg, mode="safe30"))]
        normal_first = files[0][0]
        best = self._best_variant()
        if best and best != normal_first and any(f[0] == best for f in files):
            # останні рази Instagram приймав лише цей варіант — починаємо з нього (60 к/с перевіряємо раз на добу)
            files = [f for f in files if f[0] == best] + [f for f in files if f[0] != best]
            log.info("Instagram: починаю з варіанта, що спрацював минулого разу: %s", best)
        single = bool(self.pcfg.get("single_attempt", True))
        if single:
            # Діагностичний режим (тимчасово): одна спроба — один контейнер, одне завантаження, без перебору
            # варіантів і без запасного шляху через Facebook. Повтор — вручну («🔁»), після аналізу етапів.
            # Яку одну копію пробуємо — експеримент діагностики (platforms.instagram.diag_variant):
            # "" — звичайна (60 к/с), "safe60" — 60 к/с полегшена, "safe30" — 30 к/с рівень 4.1 (єдина, що проходила)
            want = {"safe30": "крайній", "safe60": "сувора"}.get(str(self.pcfg.get("diag_variant", "safe30")))
            pick = [f for f in files if want and f[0].startswith(want)]
            files = pick[:1] or files[:1]
            tokens = tokens[:1]   # і один токен: жодного другого контейнера / завантаження
        if single and str(self.pcfg.get("diag_method", "rupload")) == "fb_link" and self.secrets.get("FB_PAGE_ID"):
            return self._diag_via_facebook(files[0], video_path, meta)
        errors = []
        via_fb = bool(self.pcfg.get("facebook_fallback", False) and self.secrets.get("FB_PAGE_ID")) and not single
        for tname, token in tokens:
            for fname, get_file in files:
                self._check_stop()
                try:
                    if fname != "оригінал":
                        self.report("готую копію для Instagram")
                    out = self._publish_file(get_file(), meta, token)
                except PublishError as e:
                    if not self._upload_rejected(e):
                        raise  # інша помилка (токен, ліміт, формат) — без перебору
                    self._save_diag("відмова")
                    log.warning("Instagram: %s + токен %s — відмова: %s", fname, tname, e)
                    errors.append(f"{fname}" + (f" (токен {tname})" if len(tokens) > 1 else "") + f" — {short_error(e)}")
                    if not via_fb:
                        continue
                    via_fb = False                       # запасний шлях — один раз, одразу після першої відмови
                    try:
                        self._check_stop()
                        self.report("готую копію для Instagram")
                        out = self._publish_via_facebook(media.instagram_copy(video_path, self.cfg), meta)
                    except Stopped:
                        raise
                    except PublishError as e2:
                        log.warning("Instagram через Facebook — відмова: %s", e2)
                        errors.append(f"через тимчасове відео Facebook — {short_error(e2)}")
                        continue
                    out["note"] = ("Instagram: пряме завантаження відхилено — опубліковано через тимчасове "
                                   "відео Facebook (його видалено)")
                    out["tried"] = errors[:6]
                    return out
                notes = []
                if big:
                    notes.append(f"файл {size_mb:.0f} МБ — завантажено {fname} "
                                 "(Instagram однаково стискає Reels до ~3–5 Мбіт/с)")
                elif problems:
                    notes.append("перекодовано під вимоги Reels (" + ", ".join(problems) + ")")
                elif fname != "оригінал":
                    notes.append("Instagram не прийняв оригінал — опубліковано копію під вимоги Reels")
                if token:
                    notes.append("завантажено з токеном користувача")
                if notes:
                    out["note"] = "Instagram: " + "; ".join(notes)
                if errors:  # що не пройшло перед успіхом — для «🧾 Деталі»
                    out["tried"] = errors[:6]
                self._remember_variant(None if fname == normal_first else fname)
                return out
        try:
            about = media.ig_summary(media.ig_probe(video_path, self.cfg.get("ffprobe", "ffprobe")))
        except Exception:  # noqa: BLE001
            about = ""
        stages = (getattr(self, "diag", None) or {}).get("summary")
        if single:
            raise PublishError("Instagram не прийняв відео (діагностичний режим: одна спроба, без повторів). "
                               + (f"Етапи: {stages}. " if stages else "") + "; ".join(errors)
                               + " — Повторити вручну: «🔁 Повторити зараз».")
        raise PublishError(f"Instagram не прийняв відео ({len(errors)} спроб): " + "; ".join(errors)
                           + (f". Файл: {about}" if about else "")
                           + ("" if len(tokens) > 1 else
                              " — Порада: у панелі (Instagram + Facebook) ще раз «Отримати сторінки» → «Підключити»,"
                              " щоб зберегти токен користувача для резервної спроби."))

    def _stage(self, name: str, ok: bool, seconds: float, info: str = "") -> None:
        d = getattr(self, "diag", None)
        if d is not None:
            d["stages"].append({"stage": name, "ok": ok, "seconds": round(seconds, 1), "info": info})

    def _save_diag(self, result: str) -> None:
        """Звіт по етапах (контейнер → rupload → обробка → публікація): журнал + data/ig_diag/*.json."""
        d = getattr(self, "diag", None)
        if not d or d.get("saved"):
            return
        d["saved"] = True
        d["result"] = result
        line = " → ".join(f"{s['stage']} {'✅' if s['ok'] else '❌'} {s['seconds']:g} с"
                          + (f" ({s['info']})" if s["info"] and (not s["ok"] or s["stage"] == "файл") else "")
                          for s in d["stages"])
        d["summary"] = line
        log.info("Instagram діагностика [%s, %s МБ]: %s", d.get("file"), d.get("size_mb"), line)
        try:
            folder = Path(self.cfg["paths"]["data"]) / "ig_diag"
            folder.mkdir(parents=True, exist_ok=True)
            name = datetime.now().strftime("%Y%m%d-%H%M%S") + f"_{d.get('container') or 'x'}.json"
            (folder / name).write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
            for old in sorted(folder.glob("*.json"))[:-50]:
                old.unlink(missing_ok=True)
        except (OSError, KeyError) as e:
            log.debug("ig_diag: %s", e)

    def _variant_file(self) -> Path:
        data = (self.cfg.get("paths") or {}).get("data")
        return Path(data) / "ig_variant.json" if data else None

    def _best_variant(self) -> str | None:
        """Варіант файлу, який Instagram прийняв минулого разу, коли звичайний не пройшов.
        Раз на добу пробуємо знову звичайний порядок (60 к/с) — раптом Instagram знову приймає."""
        try:
            st = json.loads(self._variant_file().read_text(encoding="utf-8"))
        except (OSError, ValueError, AttributeError):
            return None
        if time.time() - float(st.get("ts") or 0) > 24 * 3600:
            return None
        return st.get("best")

    def _remember_variant(self, best: str | None) -> None:
        f = self._variant_file()
        if f is None:
            return
        try:
            if best:
                f.write_text(json.dumps({"best": best, "ts": time.time()}, ensure_ascii=False), encoding="utf-8")
            else:
                f.unlink(missing_ok=True)
        except OSError as e:
            log.debug("ig_variant: %s", e)

    @staticmethod
    def _upload_rejected(e: Exception) -> bool:
        t = str(e).lower()
        return "processingfailed" in t or "processing failed" in t or "не прийняв завантаження" in t

    def _publish_file(self, video_path: str, meta, token: str | None = None) -> dict:
        g = self.graph(token) if token else self._g()
        from app.core import media as _media
        try:   # той самий файл, що підемо вантажити: є, читається повністю, не змінюється
            check = _media.file_check(video_path)
        except _media.MediaError as e:
            raise PublishError(f"Instagram: перевірка файлу перед завантаженням — {e}") from e
        size = check["size"]
        self.diag = diag = {"started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "file": Path(video_path).name,
                            "size_mb": round(size / 1048576, 1), "token": "користувача" if token else "сторінки",
                            "stages": [], "file_check": {"size": check["size"], "md5": check["md5"]}}
        try:
            from app.core import media
            diag["probe"] = media.ig_summary(media.ig_probe(video_path, self.cfg.get("ffprobe", "ffprobe")))
        except Exception:  # noqa: BLE001
            pass
        self._stage("файл", True, 0, f"{size / 1048576:.1f} МБ, читається повністю, не змінюється")
        t0 = time.time()
        try:
            c = g.post(f"{self.ig_id}/media", media_type="REELS", upload_type="resumable",
                       caption=meta.instagram_text[:2200],
                       share_to_feed="true" if self.pcfg.get("share_to_feed", True) else "false")
        except PublishError as e:
            self._stage("контейнер", False, time.time() - t0, str(e)[:300])
            self._save_diag("помилка створення контейнера")
            raise
        cid = c["id"]
        diag["container"] = cid
        self._stage("контейнер", True, time.time() - t0, cid)
        uri = c.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{g.version}/{cid}"
        log.info("Instagram: контейнер %s, завантажую %s (%.1f МБ)", cid, video_path,
                 os.path.getsize(video_path) / 1024 / 1024)
        self.report(f"завантажую файл ({os.path.getsize(video_path) / 1024 / 1024:.0f} МБ)")
        upload_error = None
        try:
            g.upload_binary(uri, video_path, float(self.pcfg.get("upload_chunk_mb", 8) or 0))
            if not _media.file_unchanged(video_path, check):
                diag["file_changed_during_upload"] = True
            up = getattr(g, "last_upload", {})
            if up.get("chunk_mb"):   # частинами: переконатись, що сервер має ВСІ байти, перш ніж чекати обробки
                got = g.upload_status(uri)
                diag["rupload_after"] = got
                if isinstance(got.get("offset"), int) and got["offset"] < size:
                    raise PublishError(f"rupload прийняв {got['offset'] / 1048576:.1f} з {size / 1048576:.1f} МБ "
                                       f"(частин надіслано {len(up.get('chunks') or [])}) — файл дійшов не повністю")
            up = getattr(g, "last_upload", {})
            parts = len(up.get("chunks") or [])
            self._stage("rupload", True, up.get("seconds", 0),
                        f"HTTP {up.get('http')}, {up.get('mbps')} Мбіт/с" + (f", частин {parts}" if parts > 1 else ""))
            log.info("Instagram rupload ✅ %.1f МБ за %s с%s", size / 1048576, up.get("seconds"),
                     f" ({parts} частин по {up.get('chunk_mb'):g} МБ)" if parts > 1 else "")
            diag["uri"] = uri
        except PublishError as e:
            up = getattr(g, "last_upload", {})
            if not _media.file_unchanged(video_path, check):
                diag["file_changed_during_upload"] = True
                log.warning("Instagram: файл змінився під час завантаження (OneDrive / інша програма?): %s", video_path)
            got = g.upload_status(uri)
            diag["rupload"] = {**up, "after": got}
            self._stage("rupload", False, up.get("seconds", 0),
                        f"HTTP {up.get('http')} за {up.get('seconds')} с ({up.get('mbps')} Мбіт/с"
                        + (f", частинами по {up.get('chunk_mb'):g} МБ: прийнято {len(up.get('chunks') or []) - 1}"
                           f" з {-(-up.get('bytes', 0) // int(up.get('chunk_mb') * 1048576))}" if up.get("chunk_mb") else "")
                        + "); "
                        f"сервер каже: {json.dumps(got, ensure_ascii=False)[:160]}")
            if isinstance(got.get("offset"), int) and got["offset"] < size:
                # файл не дійшов (сервер має менше байтів, ніж ми надіслали) — чекати «обробки» марно
                self._save_diag("файл не дійшов до Instagram")
                raise PublishError(f"{e} | rupload отримав {got['offset'] / 1048576:.1f} з {size / 1048576:.1f} МБ — "
                                   "файл не дійшов до Instagram (обробку не чекали)")
            # 400 від rupload буває хибною: файл дійшов і обробляється. Повторно в той самий контейнер НЕ шлемо —
            # друга відправка ламає обробку («Video Transcoding Error: both HD and SD progressive failed»).
            upload_error = e
            # часто хибна відповідь: файл дійшов і обробляється. Справжня помилка буде в підсумку публікації
            log.info("Instagram rupload: %s — перевіряю, чи відео все одно обробляється", short_error(e))

        return self._wait_and_publish(g, cid, upload_error)

    def _wait_and_publish(self, g, cid: str, upload_error=None) -> dict:
        """Чекає, поки Instagram обробить контейнер, і публікує його."""
        start = time.time()
        deadline = start + (3 * 60 if upload_error else 20 * 60)
        self.report("Instagram обробляє відео")
        delays = iter(POLL_DELAYS)
        diag = getattr(self, "diag", None) or {"stages": []}
        polls = diag.setdefault("polls", [])
        while True:
            st = g.get(cid, fields="status_code,status")
            code = st.get("status_code")
            polls.append([int(time.time() - start), code, (st.get("status") or "")[:120]])
            self.report("Instagram обробляє відео")   # «живий»: остання перевірка щойно
            if code == "IN_PROGRESS" and upload_error and deadline < start + AFTER_400_WAIT:
                log.info("Instagram: попри помилку завантаження відео обробляється — чекаю до %s хв",
                         AFTER_400_WAIT // 60)
                deadline = start + AFTER_400_WAIT
            if code == "FINISHED":
                if upload_error:
                    log.info("Instagram: попри помилку завантаження контейнер готовий — публікую")
                self._stage("обробка", True, time.time() - start, f"FINISHED, перевірок {len(polls)}")
                break
            if code in ("ERROR", "EXPIRED"):
                self._stage("обробка", False, time.time() - start, f"{code}: {st.get('status')}")
                self._save_diag(f"контейнер {code}")
                reason = f"{st.get('status')}{ig_hint(st.get('status'))}"
                raise PublishError(f"{upload_error} | стан контейнера: {reason}" if upload_error
                                   else f"Instagram відхилив відео: {reason}")
            if time.time() > deadline:
                srv = g.upload_status(diag["uri"]) if diag.get("uri") else {}
                if srv:
                    diag["rupload_after_wait"] = srv
                self._stage("обробка", False, time.time() - start,
                            f"{code} після {len(polls)} перевірок: {(st.get('status') or '')[:100]}"
                            + (f"; rupload має: {json.dumps(srv, ensure_ascii=False)[:120]}" if srv else ""))
                self._save_diag("обробка не завершилась")
                if upload_error:
                    waited = int((time.time() - start) / 60)
                    raise PublishError(f"{upload_error}{self._container_reason(g, cid)} (чекали {waited} хв)")
                raise PublishError("Instagram: обробка відео триває понад 20 хв"
                                   + (f". Етапи: {diag.get('summary')}" if diag.get("summary") else ""))
            # перевіряємо рідше, що довше обробка: кожен запит з'їдає годинну квоту Meta; «Стоп» — щосекунди
            # квота Meta ≈200 запитів/год на весь додаток: опитування раз на 20 с, потім раз на хвилину,
            # а при квоті ≥80% — раз на 2 хв
            pause = next(delays, POLL_DELAYS[-1])     # 10 → 20 → 40 → 80 → 80… с: один контейнер, рідкі перевірки
            if usage_pct() >= 80:
                pause = max(pause, 120)
            for _ in range(pause):
                self._check_stop()
                time.sleep(1)

        self._check_stop()   # відео готове, але натиснули «Стоп» — не публікуємо
        self.report("публікую готове відео")
        t1 = time.time()
        try:
            mid = g.post(f"{self.ig_id}/media_publish", creation_id=cid)["id"]
        except PublishError as e:
            self._stage("публікація", False, time.time() - t1, str(e)[:300])
            self._save_diag("помилка media_publish")
            raise
        self._stage("публікація", True, time.time() - t1, mid)
        self._save_diag("опубліковано")
        out = {"id": mid, "url": ""}
        try:  # після публікації — помилки не критичні (щоб не опублікувати двічі)
            out["url"] = g.get(mid, fields="permalink").get("permalink", "")
        except Exception as e:  # noqa: BLE001
            log.warning("Instagram permalink: %s", e)
        return out

    FB_LINK_WAIT = 10 * 60

    def _diag_via_facebook(self, variant, original: str, meta) -> dict:
        """Експеримент діагностики: одна спроба БЕЗ rupload — через неопубліковане відео Facebook (video_url)."""
        fname, get_file = variant
        self._check_stop()
        if fname != "оригінал":
            self.report("готую копію для Instagram")
        path = get_file()
        from app.core import media as _media
        try:
            check = _media.file_check(path)
        except _media.MediaError as e:
            raise PublishError(f"Instagram: перевірка файлу перед завантаженням — {e}") from e
        self.diag = {"started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "file": Path(path).name,
                     "size_mb": round(check["size"] / 1048576, 1), "method": "через Facebook (video_url)",
                     "stages": []}
        try:
            orig = _media.ig_summary(_media.ig_probe(original, self.cfg.get("ffprobe", "ffprobe")))
            copy = _media.ig_summary(_media.ig_probe(path, self.cfg.get("ffprobe", "ffprobe")))
        except Exception:  # noqa: BLE001
            orig = copy = ""
        self._stage("файл", True, 0, f"{check['size'] / 1048576:.1f} МБ, {fname}"
                    + (f"; оригінал: {orig}" if orig else "") + (f"; копія: {copy}" if copy and path != original else ""))
        t0 = time.time()
        try:
            out = self._publish_via_facebook(path, meta)
        except Stopped:
            raise
        except PublishError as e:
            self._stage("через Facebook", False, time.time() - t0, str(e)[:300])
            self._save_diag("відмова (шлях через Facebook)")
            raise PublishError("Instagram не прийняв відео (діагностика: одна спроба через Facebook, без rupload). "
                               f"Етапи: {self.diag.get('summary')}") from e
        self._stage("через Facebook", True, time.time() - t0, "опубліковано")
        self._save_diag("опубліковано через Facebook")
        out["note"] = ("Instagram: опубліковано через тимчасове відео Facebook (пряме завантаження rupload "
                       "Instagram зараз відкидає)")
        return out

    def _publish_via_facebook(self, video_path: str, meta) -> dict:
        """Запасний шлях, коли Instagram відхиляє пряме завантаження (rupload ProcessingFailedError):
        відео — на сторінку Facebook НЕОПУБЛІКОВАНИМ (ніхто не бачить) → Instagram бере його за посиланням
        (video_url) → тимчасове відео з Facebook видаляється за будь-якого результату."""
        page = self.secrets.get("FB_PAGE_ID")
        if not page:
            raise PublishError("немає FB_PAGE_ID — запасний шлях через Facebook недоступний")
        fb = self.graph()                                   # токен сторінки: відео — на сторінку
        self.report("запасний шлях: вантажу відео на Facebook (неопубліковане)")
        vid = fb.upload_video_unpublished(page, video_path, "тимчасово — для публікації в Instagram")["id"]
        log.info("Instagram через Facebook: тимчасове відео %s", vid)
        try:
            src, start = "", time.time()
            while time.time() - start < self.FB_LINK_WAIT:
                st = fb.get(vid, fields="status,source")
                vs = (st.get("status") or {}).get("video_status")
                if vs == "error":
                    raise PublishError(f"Facebook не обробив тимчасове відео: {st.get('status')}")
                if st.get("source") and vs in (None, "ready"):
                    src = st["source"]
                    break
                self.report("запасний шлях: Facebook обробляє відео")
                for _ in range(20 if usage_pct() < 80 else 60):
                    self._check_stop()
                    time.sleep(1)
            if not src:
                raise PublishError("Facebook не дав посилання на тимчасове відео за 10 хв")
            g = self._g()
            c = g.post(f"{self.ig_id}/media", media_type="REELS", video_url=src,
                       caption=meta.instagram_text[:2200],
                       share_to_feed="true" if self.pcfg.get("share_to_feed", True) else "false")
            out = self._wait_and_publish(g, c["id"])
            out["via"] = "facebook"
            return out
        finally:
            try:
                fb.delete(vid)
            except Exception as e:  # noqa: BLE001
                log.warning("Тимчасове відео Facebook %s не видалено: %s — видаліть вручну (неопубліковане)", vid, e)

    @staticmethod
    def _container_reason(g, cid: str) -> str:
        """Instagram зберігає точну причину відмови в контейнері — додаємо її до помилки."""
        try:
            st = g.get(cid, fields="status_code,status")
        except Exception as e:  # noqa: BLE001
            log.debug("status контейнера: %s", e)
            return ""
        reason = st.get("status") or st.get("status_code") or ""
        return f" | стан контейнера: {reason}{ig_hint(reason)}" if reason else ""

    _metric: str | None = None   # яка метрика переглядів спрацювала — щоб не витрачати 3 запити щоразу

    def get_views(self, result: dict) -> int | None:
        g = self._g()
        order = ("views", "plays", "reach")
        if Instagram._metric:
            order = (Instagram._metric,) + tuple(m for m in order if m != Instagram._metric)
        for metric in order:
            try:
                data = g.get(f"{result['id']}/insights", low=True, metric=metric).get("data") or []
            except RateLimited:
                raise
            except PublishError:
                continue
            if data:
                Instagram._metric = metric
                d = data[0]
                vals = d.get("values") or []
                if vals:
                    return int(vals[0].get("value") or 0)
                if d.get("total_value"):
                    return int(d["total_value"].get("value") or 0)
        return None

    def get_stats(self, result: dict) -> dict:
        out = {"views": self.get_views(result)}
        try:
            r = self._g().get(result["id"], low=True, fields="like_count,comments_count")
            out.update(likes=r.get("like_count"), comments=r.get("comments_count"))
        except RateLimited:
            raise
        except PublishError as e:
            log.debug("IG likes: %s", e)
        return out

    def account_stats(self) -> dict:
        r = self._g().get(self.ig_id, low=True, fields="followers_count,media_count")
        return {"followers": r.get("followers_count"), "videos": r.get("media_count")}

    def check(self) -> str:
        r = self._g().get(self.ig_id, fields="username")
        return f"@{r.get('username')}"

    def test_upload(self, clip: Path, label: str) -> tuple[bool | None, str]:
        """Один тест без публікації: контейнер → rupload → обробка. (True/False/None — пройшло / ні / невідомо, текст)."""
        g = self._g()
        size = clip.stat().st_size / 1048576
        t0 = time.time()
        c = g.post(f"{self.ig_id}/media", media_type="REELS", upload_type="resumable", caption="test",
                   share_to_feed="false")
        cid = c["id"]
        uri = c.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{g.version}/{cid}"
        try:
            g.upload_binary(uri, str(clip))
        except PublishError as e:
            return False, f"{label} ({size:.1f} МБ): ❌ rupload {time.time() - t0:.0f} с — {short_error(e)}"
        t1 = time.time()
        code = None
        for delay in (5, 10, 20, 30, 40, 60, 60, 60):
            st = g.get(cid, fields="status_code,status")
            code = st.get("status_code")
            if code == "FINISHED":
                return True, f"{label} ({size:.1f} МБ): ✅ прийнято (завантаження {t1 - t0:.0f} с, обробка {time.time() - t1:.0f} с)"
            if code in ("ERROR", "EXPIRED"):
                return False, f"{label} ({size:.1f} МБ): ❌ обробка — {st.get('status')}"
            time.sleep(delay)
        return None, f"{label} ({size:.1f} МБ): ⏳ обробка не завершилась ({code})"

    def test_suite(self, original: str, workdir: Path, report=lambda line: None) -> list[str]:
        """Серія тестів без публікації на справжньому шортсі — кожен міняє одну річ, щоб знайти, що не подобається."""
        from app.core import media as _media
        workdir.mkdir(parents=True, exist_ok=True)
        ff = self.cfg.get("ffmpeg", "ffmpeg")
        p = _media.ig_probe(original, self.cfg.get("ffprobe", "ffprobe"))
        dur = max(5, int(p.get("duration") or 30))
        enc = ["-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-pix_fmt", "yuv420p",
               "-maxrate", "6M", "-bufsize", "12M", "-crf", "21", "-r", "30",
               "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1"]
        aud = ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
        tests = [
            ("1) перші 5 с шортса", ["-i", original, "-t", "5", *enc, *aud]),
            ("2) шортс БЕЗ звуку", ["-i", original, *enc, "-an"]),
            ("3) лише звук шортса (чорний кадр)", ["-f", "lavfi", "-i", "color=black:s=1080x1920:r=30", "-i", original,
                                                   "-map", "0:v", "-map", "1:a", "-shortest", "-t", str(dur),
                                                   "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", *aud]),
            (f"4) згенероване {dur} с 1080×1920", ["-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30", "-f", "lavfi",
                                                   "-i", "sine=frequency=440:sample_rate=48000", "-t", str(dur),
                                                   "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                                                   "-maxrate", "6M", "-bufsize", "12M", *aud]),
        ]
        out = [f"оригінал: {_media.ig_summary(p)}"]
        for i, (label, args) in enumerate(tests, 1):
            clip = workdir / f"ig_suite_{i}.mp4"
            r = _media.run_hidden([ff, "-y", "-hide_banner", "-loglevel", "error", *args,
                                   "-movflags", "+faststart", str(clip)], timeout=900)
            if r.returncode != 0:
                line = f"{label}: не вдалося підготувати ({r.stderr.decode(errors='ignore')[-120:]})"
            else:
                try:
                    _, line = self.test_upload(clip, label)
                except Exception as e:  # noqa: BLE001
                    line = f"{label}: помилка тесту — {e}"
                clip.unlink(missing_ok=True)
            out.append(line)
            report(line)
        return out

    def raw_v1_test(self, workdir: Path, seconds=(5, 59), report=lambda line: None, version: str = "",
                    kind: str = "page") -> list[str]:
        """Дослівно спосіб найпершої версії (11.11): прямі запити requests, без класу Graph, копій і перевірок.
        Згенеровані ролики, БЕЗ публікації. Якщо 59 с і тут не проходить — справа не в коді програми."""
        from app.core import media as _media
        import requests as rq
        workdir.mkdir(parents=True, exist_ok=True)
        ver = version or self.cfg["graph_api_version"]
        host = "graph.facebook.com"
        if kind == "user":
            token = self.secrets.get("META_USER_TOKEN") or ""
            if not token:
                return ["немає токена користувача — перепідключіть Meta в панелі («Отримати сторінки» → «Підключити»)"]
            ig = self.ig_id
            label = "токен користувача"
        elif kind == "ig":
            token = self.secrets.get("IG_LOGIN_TOKEN") or ""
            if not token:
                return ["немає токена Instagram Login — панель → «📸 Instagram + Facebook» → «Токен Instagram Login»"]
            import requests as _rq
            if token.startswith("IG"):   # токен Instagram Login — свій сервер graph.instagram.com
                host = "graph.instagram.com"
                me = _rq.get(f"https://{host}/{ver}/me", timeout=60,
                             params={"fields": "user_id,username", "access_token": token}).json()
                if me.get("error"):
                    return [f"токен Instagram Login не працює: {me['error'].get('message')}"]
                ig = str(me.get("user_id") or me.get("id"))
                label = f"Instagram Login (@{me.get('username')})"
            else:   # системний токен (Unified login, SUAT) — graph.facebook.com
                ig = self.ig_id
                me = _rq.get(f"https://graph.facebook.com/{ver}/{ig}", timeout=60,
                             params={"fields": "username", "access_token": token}).json()
                if me.get("error"):
                    return [f"новий токен не працює: {me['error'].get('message')}"]
                label = f"системний токен Unified login (@{me.get('username')})"
        else:
            token = self.secrets.get("META_IG_TOKEN") or self.secrets["META_PAGE_TOKEN"]
            ig = self.ig_id
            label = "токен сторінки"
        out = [f"{host}/{ver}, {label}, один запит rupload"]
        for sec in seconds:
            clip = workdir / f"ig_raw_{sec}.mp4"
            _media.run_hidden([self.cfg.get("ffmpeg", "ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
                               "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30", "-f", "lavfi",
                               "-i", "sine=frequency=440:sample_rate=48000", "-t", str(sec), "-c:v", "libx264",
                               "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart",
                               str(clip)], timeout=300)
            try:
                c = rq.post(f"https://{host}/{ver}/{ig}/media", timeout=120, data={
                    "media_type": "REELS", "upload_type": "resumable", "caption": "test", "share_to_feed": "false",
                    "access_token": token}).json()
                if "id" not in c:
                    raise RuntimeError(f"контейнер: {(c.get('error') or {}).get('message') or c}")
                cid = c["id"]
                uri = c.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{ver}/{cid}"
                size = os.path.getsize(clip)
                t0 = time.time()
                with open(clip, "rb") as f:
                    r = rq.post(uri, headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)},
                                data=f, timeout=1800)
                if r.status_code >= 400:
                    line = f"{sec} с: ❌ rupload HTTP {r.status_code} за {time.time() - t0:.0f} с — {r.text[:120]}"
                else:
                    line = f"{sec} с: ❌ обробка не завершилась"
                    for _ in range(30):
                        st = rq.get(f"https://{host}/{ver}/{cid}", timeout=60, params={
                            "fields": "status_code,status", "access_token": token}).json()
                        if st.get("status_code") == "FINISHED":
                            line = f"{sec} с: ✅ прийнято (rupload {time.time() - t0:.0f} с)"
                            break
                        if st.get("status_code") in ("ERROR", "EXPIRED"):
                            line = f"{sec} с: ❌ обробка — {st.get('status')}"
                            break
                        time.sleep(10)
            except Exception as e:  # noqa: BLE001
                line = f"{sec} с: помилка — {e}"
            clip.unlink(missing_ok=True)
            out.append(line)
            report(line)
        return out

    def long_video_ok(self, workdir: Path, seconds: int = 59) -> tuple[bool | None, str]:
        """Чи Instagram зараз приймає довге відео (згенероване, без публікації). Для щоденної перевірки."""
        from app.core import media as _media
        workdir.mkdir(parents=True, exist_ok=True)
        clip = workdir / f"ig_watch_{seconds}.mp4"
        r = _media.run_hidden([self.cfg.get("ffmpeg", "ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
                               "-f", "lavfi", "-i", "color=black:s=1080x1920:r=30", "-f", "lavfi",
                               "-i", "sine=frequency=440:sample_rate=48000", "-t", str(seconds), "-c:v", "libx264",
                               "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                               "-movflags", "+faststart", str(clip)], timeout=300)
        if r.returncode != 0:
            return None, "не вдалося підготувати тестовий ролик"
        try:
            return self.test_upload(clip, f"{seconds} с")
        finally:
            clip.unlink(missing_ok=True)

    def test_lengths(self, workdir: Path, lengths=(10, 20, 30, 45), report=lambda line: None) -> list[str]:
        """Межа довжини, яку Instagram зараз приймає: крихітні ролики (чорний кадр + тон) різної довжини, без публікації."""
        from app.core import media as _media
        workdir.mkdir(parents=True, exist_ok=True)
        out = []
        for sec in lengths:
            clip = workdir / f"ig_len_{sec}.mp4"
            r = _media.run_hidden([self.cfg.get("ffmpeg", "ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
                                   "-f", "lavfi", "-i", "color=black:s=1080x1920:r=30", "-f", "lavfi",
                                   "-i", "sine=frequency=440:sample_rate=48000", "-t", str(sec), "-c:v", "libx264",
                                   "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                                   "-movflags", "+faststart", str(clip)], timeout=300)
            if r.returncode != 0:
                line = f"{sec} с: не вдалося підготувати"
            else:
                try:
                    _, line = self.test_upload(clip, f"{sec} с")
                except Exception as e:  # noqa: BLE001
                    line = f"{sec} с: помилка тесту — {e}"
                clip.unlink(missing_ok=True)
            out.append(line)
            report(line)
        return out

    def test_video(self, workdir: Path) -> list[str]:
        """Тест БЕЗ публікації: 5-секундне згенероване відео (смуги + тон) → контейнер → rupload → обробка.
        Показує, чи Instagram API приймає БУДЬ-ЯКЕ відео від нас (контейнер потім просто спливає, на акаунті нічого)."""
        from app.core import media as _media
        workdir.mkdir(parents=True, exist_ok=True)
        clip = workdir / "ig_api_test.mp4"
        if not clip.exists():
            r = _media.run_hidden([self.cfg.get("ffmpeg", "ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
                                   "-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=30", "-f", "lavfi",
                                   "-i", "sine=frequency=440:sample_rate=48000", "-t", "5", "-c:v", "libx264",
                                   "-profile:v", "high", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                                   "-movflags", "+faststart", str(clip)], timeout=120)
            if r.returncode != 0:
                return [f"не вдалося створити тестове відео: {r.stderr.decode(errors='ignore')[-200:]}"]
        g = self._g()
        out = [f"тестове відео: 5 с, 720×1280, {clip.stat().st_size / 1024:.0f} КБ"]
        t0 = time.time()
        c = g.post(f"{self.ig_id}/media", media_type="REELS", upload_type="resumable", caption="test",
                   share_to_feed="false")
        cid = c["id"]
        out.append(f"контейнер ✅ {time.time() - t0:.1f} с")
        uri = c.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{g.version}/{cid}"
        t1 = time.time()
        try:
            g.upload_binary(uri, str(clip))
            up = getattr(g, "last_upload", {})
            out.append(f"rupload ✅ {time.time() - t1:.1f} с (HTTP {up.get('http')})")
        except PublishError as e:
            out.append(f"rupload ❌ {time.time() - t1:.1f} с — {short_error(e)}; сервер має: "
                       f"{json.dumps(g.upload_status(uri), ensure_ascii=False)[:120]}")
            return out
        t2 = time.time()
        for delay in (5, 10, 20, 30, 40, 60, 60, 60):
            st = g.get(cid, fields="status_code,status")
            code = st.get("status_code")
            if code in ("FINISHED", "ERROR", "EXPIRED"):
                ok = code == "FINISHED"
                out.append(f"обробка {'✅' if ok else '❌'} {time.time() - t2:.0f} с — {code}"
                           + ("" if ok else f": {st.get('status')}") + " (не публікуємо)")
                return out
            time.sleep(delay)
        out.append(f"обробка ⏳ понад {time.time() - t2:.0f} с — досі {code}")
        return out

    def api_report(self) -> list[str]:
        """Як Meta бачить наш доступ до публікації: токен (дійсний, дозволи, доступ до даних), акаунт, добовий ліміт."""
        out = []
        s = self.secrets
        g = self._g()
        app_tok = f"{s.get('META_APP_ID')}|{s.get('META_APP_SECRET')}" if s.get("META_APP_ID") and s.get("META_APP_SECRET") else ""
        for name, tok in (("токен сторінки", g.token), ("токен користувача", s.get("META_USER_TOKEN") or "")):
            if not tok:
                continue
            if not app_tok:
                out.append(f"{name}: немає App ID / App Secret — перевірку токена пропущено")
                break
            try:
                r = requests.get(f"https://graph.facebook.com/{g.version}/debug_token",
                                 params={"input_token": tok, "access_token": app_tok}, timeout=30).json()
                d = r.get("data") or {}
                if not d:
                    out.append(f"{name}: {((r.get('error') or {}).get('message') or r)!s:.200}")
                    continue
                exp = d.get("expires_at") or 0
                dax = d.get("data_access_expires_at") or 0
                fmt = lambda ts: "безстроковий" if not ts else datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")  # noqa: E731
                scopes = d.get("scopes") or []
                need = ["instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement"]
                miss = [x for x in need if x not in scopes]
                out.append(f"{name}: {'✅ дійсний' if d.get('is_valid') else '❌ НЕдійсний'}; діє до: {fmt(exp)}; "
                           f"доступ до даних до: {fmt(dax)}"
                           + (f"; ❌ бракує дозволів: {', '.join(miss)}" if miss else "; дозволи ✅")
                           + (f"; помилка: {d['error'].get('message')}" if d.get("error") else ""))
            except Exception as e:  # noqa: BLE001
                out.append(f"{name}: перевірка не вдалась — {e}")
        try:
            r = g.get(self.ig_id, fields="username")
            out.append(f"Instagram: @{r.get('username')}")
        except Exception as e:  # noqa: BLE001
            out.append(f"Instagram акаунт: {e}")
        try:
            r = g.get(f"{self.ig_id}/content_publishing_limit", fields="quota_usage,config")
            d = (r.get("data") or [{}])[0]
            total = (d.get("config") or {}).get("quota_total")
            out.append(f"Добовий ліміт публікацій через API: використано {d.get('quota_usage')} з {total or '?'}")
        except Exception as e:  # noqa: BLE001
            out.append(f"Добовий ліміт: {e}")
        # навантаження застосунку саме зараз — із заголовків щойно отриманих відповідей Meta
        st = _limit_state()
        fresh = time.time() - float(st.get("usage_ts") or 0) < 120
        until = blocked_until()
        if until:
            out.append(f"Квота запитів Meta: ❌ вичерпано — пауза до {_hm(until)}")
        elif fresh:
            pct = int(st.get("usage") or 0)
            mark = "✅" if pct < 50 else "⚠️" if pct < 90 else "❌"
            out.append(f"Квота запитів Meta зараз: {mark} {pct}% від годинної")
        else:
            out.append("Квота запитів Meta зараз: ✅ навантаження мале (Meta навіть не повідомляє відсоток)")
        return out


class Facebook(_MetaBase):
    key = "facebook"
    name = "Facebook"

    @property
    def page_id(self) -> str:
        pid = self.secrets["FB_PAGE_ID"]
        if not pid:
            raise PublishError("не задано FB_PAGE_ID")
        return pid

    def publish(self, video_path: str, meta) -> dict:
        g = self.graph()
        start = g.post(f"{self.page_id}/video_reels", upload_phase="start")
        vid = start["video_id"]
        url = start.get("upload_url") or f"https://rupload.facebook.com/video-upload/{g.version}/{vid}"
        g.upload_binary(url, video_path)
        g.post(f"{self.page_id}/video_reels", upload_phase="finish", video_id=vid,
               video_state="PUBLISHED", description=meta.facebook_text[:2200])
        out = {"id": vid, "url": f"https://www.facebook.com/reel/{vid}"}

        deadline = time.time() + 15 * 60
        while time.time() < deadline:
            try:
                st = g.get(vid, fields="status").get("status") or {}
            except PublishError as e:
                log.warning("Facebook status: %s", e)
                break
            pub = (st.get("publishing_phase") or {}).get("status")
            proc = (st.get("processing_phase") or {}).get("status")
            if st.get("video_status") == "error" or "error" in (pub, proc):
                errs = (st.get("processing_phase") or {}).get("errors") or st
                raise PublishError(f"Facebook відхилив відео: {errs}")
            if pub == "complete" or st.get("video_status") == "ready":
                return out
            time.sleep(30 if usage_pct() < 80 else 120)  # кожен запит — з годинної квоти Meta
        out["note"] = "Facebook ще обробляє відео — з'явиться на сторінці трохи пізніше"
        return out

    def get_views(self, result: dict) -> int | None:
        data = self.graph().get(f"{result['id']}/video_insights", low=True).get("data") or []
        by_name = {d.get("name"): d for d in data}
        for name in ("fb_reels_total_plays", "blue_reels_play_count", "post_video_views", "total_video_views"):
            d = by_name.get(name)
            if d and d.get("values"):
                return int(d["values"][0].get("value") or 0)
        return None

    def get_stats(self, result: dict) -> dict:
        out = {"views": self.get_views(result)}
        try:
            r = self.graph().get(result["id"], low=True,
                                 fields="likes.summary(true).limit(0),comments.summary(true).limit(0)")
            out.update(likes=((r.get("likes") or {}).get("summary") or {}).get("total_count"),
                       comments=((r.get("comments") or {}).get("summary") or {}).get("total_count"))
        except RateLimited:
            raise
        except PublishError as e:
            log.debug("FB likes: %s", e)
        return out

    def account_stats(self) -> dict:
        r = self.graph().get(self.page_id, low=True, fields="followers_count,fan_count")
        return {"followers": r.get("followers_count") or r.get("fan_count")}

    def check(self) -> str:
        r = self.graph().get(self.page_id, fields="name")
        return f"сторінка «{r.get('name')}»"


def meta_pages(cfg: dict, short_token: str) -> list[dict]:
    """Обмінює короткий user-токен на довготривалий і повертає сторінки з токенами сторінок."""
    s = cfg["secrets"]
    ver = cfg["graph_api_version"]
    if not s["META_APP_ID"] or not s["META_APP_SECRET"]:
        raise PublishError("Спершу заповніть App ID і App Secret")
    try:
        r = requests.get(f"https://graph.facebook.com/{ver}/oauth/access_token", timeout=60, params={
            "grant_type": "fb_exchange_token", "client_id": s["META_APP_ID"],
            "client_secret": s["META_APP_SECRET"], "fb_exchange_token": short_token.strip()}).json()
    except (requests.RequestException, ValueError) as e:
        raise PublishError(f"мережа: {e}") from e
    if "access_token" not in r:
        raise PublishError(f"Не вдалося обміняти токен: {(r.get('error') or {}).get('message') or r}")
    token = r["access_token"]
    fields = "id,name,access_token,instagram_business_account{id,username}"

    def get(path: str, **params) -> dict:
        try:
            j = requests.get(f"https://graph.facebook.com/{ver}/{path}", timeout=60,
                             params={"access_token": token, **params}).json()
        except (requests.RequestException, ValueError) as e:
            raise PublishError(f"мережа: {e}") from e
        if j.get("error"):
            log.warning("Graph %s: %s", path, j["error"].get("message"))
        return j

    data = get("me/accounts", fields=fields).get("data") or []
    for page in data:
        page["_user_token"] = token
    if not data:
        # Сторінки, що належать бізнес-портфоліо, у me/accounts можуть не з'являтися
        seen = set()
        for biz in get("me/businesses", fields="id,name").get("data") or []:
            for edge in ("owned_pages", "client_pages"):
                for page in get(f"{biz['id']}/{edge}", fields=fields).get("data") or []:
                    if page.get("access_token") and page["id"] not in seen:
                        seen.add(page["id"])
                        page["_user_token"] = token
                        data.append(page)
    if not data:
        granted = [p["permission"] for p in get("me/permissions").get("data") or [] if p.get("status") == "granted"]
        raise PublishError(
            "Facebook не показав жодної сторінки. Найчастіше причина — у вікні входу не поставлено галочку "
            "на вашій сторінці (повторіть Generate Access Token і виберіть сторінку та Instagram). "
            "Також перевірте, що у вас є саме СТОРІНКА Facebook (не лише профіль) і ви її адміністратор. "
            f"Надані дозволи: {', '.join(granted) or 'немає'}")
    return data


def meta_save(cfg: dict, page: dict) -> str:
    """Записує токен сторінки, FB_PAGE_ID та IG_USER_ID у .env."""
    from dotenv import set_key

    env = cfg["env_path"]
    if not os.path.exists(env):
        open(env, "a", encoding="utf-8").close()
    set_key(env, "META_PAGE_TOKEN", page["access_token"], quote_mode="never")
    set_key(env, "FB_PAGE_ID", page["id"], quote_mode="never")
    if page.get("_user_token"):  # резерв для завантаження в Instagram (див. Instagram.publish)
        set_key(env, "META_USER_TOKEN", page["_user_token"], quote_mode="never")
    ig = page.get("instagram_business_account") or {}
    if ig.get("id"):
        set_key(env, "IG_USER_ID", ig["id"], quote_mode="never")
        return f"Сторінка «{page['name']}» та Instagram @{ig.get('username')} підключені"
    return (f"Сторінку «{page['name']}» підключено, але до неї не прив'язано професійний Instagram — "
            "IG_USER_ID не записано")


def authorize_meta(cfg: dict) -> None:
    """Авторизація з консолі."""
    short = input("Вставте короткий User Access Token з Graph API Explorer: ").strip()
    try:
        data = meta_pages(cfg, short)
    except PublishError as e:
        print("Помилка:", e)
        return
    for i, p in enumerate(data, 1):
        ig = p.get("instagram_business_account") or {}
        print(f"{i}. {p['name']} (id {p['id']}) — Instagram: @{ig.get('username', '—')}")
    idx = int(input("Номер сторінки: ").strip() or "1") - 1 if len(data) > 1 else 0
    print(meta_save(cfg, data[idx]))
