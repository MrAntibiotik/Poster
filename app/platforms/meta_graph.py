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

from .base import ContainerFailed, Platform, PublishError, RateLimited, Stopped, StillProcessing, TokenExpired

log = logging.getLogger("meta")

TOKEN_ERROR_CODES = {102, 190, 463, 467}
# Ліміти запитів Meta: 4 — додатка (≈200 запитів/год на користувача), 17 — користувача, 32 — сторінки,
# 613 — частота, 80001–80014 — ліміти Business Use Case. Вікно — ковзна година.
RATE_LIMIT_CODES = {4, 17, 32, 613} | set(range(80001, 80015))
LIMIT_FILE = Path(tempfile.gettempdir()) / "poster-meta-limit.json"   # спільний для всіх каналів (один додаток Meta)
POLL_DELAYS = (10, 20, 40, 80)   # перевірки стану контейнера: затримка наростає, далі — раз на 80 с

# Статуси контейнера Instagram (IG Container status_code) → що робить Poster
CONTAINER_STATES = {
    "FINISHED": "ready",       # можна публікувати (media_publish)
    "IN_PROGRESS": "waiting",  # Meta обробляє — чекати той самий контейнер
    "PENDING": "waiting",      # у черзі обробки — чекати той самий контейнер
    "ERROR": "error",          # підтверджена відмова — причину зберегти, можна наступний спосіб
    "EXPIRED": "expired",      # не опублікований за 24 год — можна нову спробу
    "PUBLISHED": "already",    # уже опубліковано — НЕ публікувати вдруге
}


def container_state(code) -> str:
    """Невідомий статус — 'unknown': зберігаємо в діагностиці й чекаємо, новий контейнер НЕ створюємо."""
    return CONTAINER_STATES.get(str(code or "").upper(), "unknown")
INLINE_WAIT = 10 * 60     # стільки чекаємо обробки, тримаючи чергу; далі — той самий контейнер у фоні (StillProcessing)
# Тайм-аути Poster — лише про те, як довго тримати чергу й як часто питати. Вони НЕ означають помилку Meta:
# новий контейнер створюється тільки після status_code ERROR або EXPIRED (Meta: невикористаний контейнер — 24 год).
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
            body=(getattr(last, "text", "") or "")[:4000], sent=offset if chunk else size,   # повний debug_info
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
    # Статистику (перегляди, лайки, підписники) в Instagram/Facebook НЕ запитуємо взагалі: зайві запити до Meta
    # шкодять публікаціям. Аналітика — лише з YouTube.
    def get_views(self, result: dict) -> int | None:
        return None

    def get_stats(self, result: dict) -> dict:
        return {}

    def account_stats(self) -> dict:
        return {}

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

    def publish(self, video_path: str, meta, method: int = 0, on_container=None, inline_wait: int | None = None) -> dict:
        """Публікація Reels за єдиною логікою: один контейнер, одне завантаження, очікування ТОГО САМОГО контейнера.
        FINISHED → media_publish; PUBLISHED → знайти вже опублікований (без повтору); ERROR/EXPIRED → ContainerFailed
        (наступний спосіб вирішує Publisher); IN_PROGRESS/PENDING/невідомий після inline_wait → StillProcessing
        (той самий контейнер чекає у фоні — новий НЕ створюється)."""
        up = self.upload_only(video_path, meta, method, on_container)
        cid = up["container"]
        since = time.time()
        p = self.wait_inline(cid, INLINE_WAIT if inline_wait is None else inline_wait)
        info = {"method": up["method"], "method_name": up["method_name"], "container": cid}
        if p["state"] == "ready":
            self._stage("обробка", True, p.get("waited_s", 0), "FINISHED")
            try:
                out = self.publish_container(cid)
            except PublishError:
                self._save_diag("помилка media_publish")
                raise
            self._save_diag("опубліковано")
            return {**out, **info}
        if p["state"] == "already":
            self._stage("обробка", True, p.get("waited_s", 0), "PUBLISHED")
            self._save_diag("контейнер уже опубліковано")
            return {**self.find_published(since - 3600), **info, "note": "контейнер уже був опублікований — без повтору"}
        if p["state"] in ("error", "expired"):
            self._stage("обробка", False, p.get("waited_s", 0), f"{p['status_code']}: {p['status']}")
            self._save_diag(f"контейнер {p['status_code']}")
            raise ContainerFailed(f"Instagram: контейнер {p['status_code']}: {p['status']}{ig_hint(p['status'])}"
                                  + (f" (завантаження: {up['upload_note']})" if up.get("upload_note") else ""),
                                  cid, p["state"], p["status_code"], p["status"])
        # waiting / unknown: тайм-аут — це не помилка; той самий контейнер чекає у фоні
        self._stage("обробка", True, p.get("waited_s", 0), f"{p['status_code']} — далі у фоні")
        log.info("Instagram: контейнер %s ще %s після %s с — чекаю у фоні той самий контейнер%s",
                 cid, p["status_code"], p.get("waited_s"),
                 " (завантаження відповіло HTTP %s)" % up.get("upload_http") if up.get("upload_note") else "")
        e = StillProcessing(f"Instagram ще обробляє відео (контейнер {cid}, {p['status_code']})", cid)
        e.upload_lost = bool(up.get("upload_lost"))
        raise e

    current: str | None = None

    def _check_stop(self) -> None:
        if self.should_stop():
            raise Stopped("публікації зупинено")

    _upload_lock = threading.Lock()

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
        if d.get("container"):
            try:   # журнал контейнерів ніколи не ламає публікацію
                from app.insights import containers
                containers.report(self.cfg, self.container_record(result))
            except Exception as e:  # noqa: BLE001
                log.debug("журнал контейнера: %s", e)

    def container_record(self, outcome: str, error: str = "") -> dict:
        """Підсумок по контейнеру з діагностики цієї спроби (для журналу контейнерів)."""
        from app.insights import containers
        d = getattr(self, "diag", None) or {}
        p = d.get("probe_raw") or {}
        up = d.get("rupload") or {}
        st = {s["stage"]: s for s in d.get("stages") or []}
        polls = d.get("polls") or []
        code, status = (polls[-1][1], polls[-1][2]) if polls else (None, None)
        u = st.get("rupload")
        upload = None
        if u:
            upload = (f"{'✅ так' if u['ok'] else '❌ ні'}, HTTP {up.get('http') or '—'} за {u['seconds']:g} с")
        fail = next((s for s in d.get("stages") or [] if not s["ok"]), None)
        return {"container": d.get("container"), "item_id": getattr(self, "current_item", None),
                "file": d.get("file"), "duration": round(p["duration"], 1) if p.get("duration") else None,
                "size_mb": d.get("size_mb"), "codec": p.get("vcodec"),
                "resolution": f"{p['width']}×{p['height']}" if p.get("width") else None,
                "fps": round(p["fps"], 2) if p.get("fps") else None, "bitrate_kbps": p.get("vkbps"),
                "upload": upload, "upload_http": up.get("http"), "status_code": code, "status": status,
                "error": containers.error_message(up.get("body"), code, status,
                                                  error or (fail or {}).get("info") if fail else error),
                "processing_s": (st.get("обробка") or {}).get("seconds"), "method": d.get("variant"),
                "outcome": outcome,
                # повна діагностика: звук, коли створено контейнер, КОЖНА перевірка статусу, трасування Meta
                "audio": (f"{p.get('acodec')} {p.get('akbps')} кбіт/с {p.get('arate')} Гц {p.get('channels')} кан."
                          if p.get("acodec") else None),
                "vfr": p.get("vfr"), "pix_fmt": p.get("pix_fmt"),
                "container_created": d.get("container_created"), "upload_s": up.get("seconds"),
                "upload_body": up.get("body"), "trace": up.get("trace") or None,
                "upload_mode": self.upload_mode(up) if up else None,
                "polls": [list(x) for x in polls] or None,
                "stages": [[s["stage"], s["ok"], s["seconds"], (s.get("info") or "")[:300]]
                           for s in d.get("stages") or []] or None}

    # ---------------- Єдиний шлях публікації Reels (звичайний, «тихий» і фоновий) ----------------
    # Один активний контейнер — одне очікування. Новий контейнер (наступний спосіб) — ЛИШЕ після підтвердженої
    # відповіді Meta ERROR або EXPIRED. Тайм-аут очікування НЕ є помилкою: контейнер зберігається й перевіряється далі.
    # Способи: оригінал (якщо вже відповідає вимогам Reels) → копія під вимоги Reels → легка копія 30 к/с.
    METHODS = (("копія під вимоги Reels", ""), ("легка копія 30 к/с", "safe30"))   # лише 1080p — 720p надто погано

    CHUNK_MB = 8   # «частинами»: кожна частина — окремий короткий запит (~8 с), далеко від 40-секундного обриву

    def chunk_mb(self) -> float:
        """Розмір частини для rupload (0 — одним запитом). Перемикач у панелі: platforms.instagram.chunked_upload."""
        if self.pcfg.get("chunked_upload"):
            return float(self.pcfg.get("upload_chunk_mb") or self.CHUNK_MB)
        return 0.0

    @staticmethod
    def upload_mode(up: dict) -> str:
        """Як вантажилось: «одним запитом» або «частинами по 8 МБ ×6 (206×5, 200)» — для історії й журналу контейнерів."""
        chunks = up.get("chunks") or []
        if not up.get("chunk_mb"):
            return "одним запитом"
        codes = [str(c[3]) for c in chunks]
        summary = ", ".join(f"{k}×{codes.count(k)}" if codes.count(k) > 1 else k for k in dict.fromkeys(codes))
        return f"частинами по {up['chunk_mb']:g} МБ ×{len(chunks)}" + (f" ({summary})" if summary else "")

    def methods_for(self, video_path: str) -> tuple:
        """Способи для цього файлу: оригінал без перекодування, якщо він уже сумісний (не знижуємо якість без потреби)
        або якщо ffprobe не зміг його прочитати (тоді вантажимо як є — так було й раніше)."""
        from app.core import media as _media
        try:
            p = _media.ig_probe(video_path, self.cfg.get("ffprobe", "ffprobe"))
        except Exception:  # noqa: BLE001
            p = {}
        max_mb = float(self.pcfg.get("max_upload_mb", 100) or 1e6)
        limit = _media.ig_max_seconds(self.cfg)
        too_long = bool(limit and float(p.get("duration") or 0) > limit)   # довше 59,5 с — лише обрізана копія
        if not p or (not _media.ig_problems(p) and not too_long and p.get("size_mb", 0) <= max_mb):
            return (("оригінал", None),) + self.METHODS
        return self.METHODS

    def upload_only(self, video_path: str, meta, method: int = 0, on_container=None) -> dict:
        """Перевірка тривалості → (копія) → контейнер → ОДНЕ завантаження файлу. БЕЗ опитування статусу.
        on_container(record) — викликається одразу після створення контейнера (Poster зберігає ID у базі:
        після перезапуску контейнер не губиться і не створюється вдруге). «rupload 400» — ще не відмова:
        результат покаже статус контейнера."""
        from app.core import media as _media
        while not Instagram._upload_lock.acquire(timeout=3):   # одне завантаження в Instagram за раз
            self._check_stop()
        Instagram.current = video_path
        try:
            self._check_stop()
            orig = _media.ig_probe(video_path, self.cfg.get("ffprobe", "ffprobe")) or {}
            bad = _media.ig_duration_problem(orig)
            if bad:   # поза офіційними межами Reels API — контейнер навіть не створюємо (і не обрізаємо)
                raise PublishError(f"Instagram: {bad}")
            methods = self.methods_for(video_path)
            name, mode = methods[min(method, len(methods) - 1)]
            self.report(f"готую файл для Instagram: {name}")
            path = video_path if mode is None else _media.instagram_copy(video_path, self.cfg, mode=mode)
            try:
                check = _media.file_check(path)
            except Exception as e:  # noqa: BLE001
                raise PublishError(f"Instagram: файл не готовий — {e}") from e
            self._check_stop()
            size = check["size"] / 1048576
            self.diag = d = {"started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "file": Path(path).name,
                             "size_mb": round(size, 1), "variant": name, "method": method, "stages": [], "polls": []}
            try:
                d["probe_raw"] = orig if mode is None else _media.ig_probe(path, self.cfg.get("ffprobe", "ffprobe"))
            except Exception:  # noqa: BLE001
                pass
            g = self._g()
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
            d["container"] = cid
            d["container_created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._stage("контейнер", True, time.time() - t0, cid)
            rec = {"container": cid, "method": method, "method_name": name, "file_mb": round(size, 1),
                   "methods_total": len(methods)}
            if on_container:
                on_container(rec)
            uri = c.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{g.version}/{cid}"
            self.report(f"завантажую в Instagram ({name}, {size:.0f} МБ)")
            log.info("Instagram: контейнер %s, %s — завантажую %s (%.1f МБ)", cid, name, path, size)
            note = ""
            t0 = time.time()
            try:
                g.upload_binary(uri, path, self.chunk_mb())
            except RateLimited:
                raise
            except PublishError as e:
                note = short_error(e)[:300]
                log.info("Instagram rupload: %s — ще не відмова, результат покаже статус контейнера", note)
            up = getattr(g, "last_upload", {}) or {}
            d["rupload"] = {"http": up.get("http"), "body": up.get("body"), "seconds": up.get("seconds"),
                            "mbps": up.get("mbps"), "trace": up.get("trace") or {},
                            "chunk_mb": up.get("chunk_mb") or 0, "chunks": up.get("chunks") or []}
            # Запит «скільки байтів дійшло» більше НЕ робимо: 21.11 (#82) Meta відповіла «400, 0 байтів», а відео за
            # 0,7 с оброблено й опубліковано — ця відповідь нічого не означає. Результат показує лише статус контейнера.
            if not _media.file_unchanged(path, check):
                d["file_changed_during_upload"] = True
            self._stage("rupload", not note, time.time() - t0, f"HTTP {up.get('http')}" + (f": {note}" if note else ""))
            return {**rec, "upload_note": note, "upload_http": up.get("http"), "upload_lost": False}
        finally:
            Instagram.current = None
            Instagram._upload_lock.release()

    def upload_offset(self, cid: str) -> int | None:
        """ОДИН запит: скільки байтів відео Meta отримала для контейнера (None — невідомо)."""
        g = self._g()
        srv = g.upload_status(f"https://rupload.facebook.com/ig-api-upload/{g.version}/{cid}")
        return srv["offset"] if isinstance(srv.get("offset"), int) else None

    def poll_container(self, cid: str) -> dict:
        """ОДИН запит стану контейнера. {"state", "status_code", "status", "time"}; кожна перевірка — у діагностиці."""
        st = self._g().get(cid, fields="status_code,status")
        code = st.get("status_code")
        res = {"state": container_state(code), "status_code": code, "status": (st.get("status") or "")[:1000],
               "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        d = getattr(self, "diag", None)
        if d is not None and d.get("container") == cid:
            d.setdefault("polls", []).append([res["time"], code, res["status"][:300]])
        self.last_container = {"container_id": cid, "container_status": code,
                               "container_status_text": res["status"], "stage": "обробка"}
        return res

    def publish_container(self, cid: str) -> dict:
        """media_publish готового контейнера + посилання. Помилка — PublishError з префіксом «media_publish:»."""
        g = self._g()
        self.last_container = {**(getattr(self, "last_container", None) or {}), "stage": "media_publish"}
        t1 = time.time()
        try:
            mid = g.post(f"{self.ig_id}/media_publish", creation_id=cid)["id"]
        except PublishError as e:
            self._stage("публікація", False, time.time() - t1, str(e)[:300])
            raise PublishError(f"media_publish: {e}") from e
        self._stage("публікація", True, time.time() - t1, mid)
        self.last_container["stage"] = ""
        out = {"id": mid, "url": ""}
        try:   # після публікації — помилки не критичні (щоб не опублікувати двічі)
            out["url"] = g.get(mid, fields="permalink").get("permalink", "")
        except Exception as e:  # noqa: BLE001
            log.warning("Instagram permalink: %s", e)
        return out

    def find_published(self, since: float) -> dict:
        """Контейнер уже PUBLISHED (наприклад, media_publish спрацював, але відповідь загубилась) — знайти цей Reels
        серед останніх публікацій акаунта (один запит), щоб НЕ публікувати вдруге."""
        try:
            r = self._g().get(f"{self.ig_id}/media", limit=10, fields="id,permalink,timestamp")
        except PublishError as e:
            log.info("Instagram: не вдалося знайти вже опублікований Reels: %s", e)
            return {"id": "", "url": ""}
        best = None
        for m in r.get("data") or []:
            try:
                ts = datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z").timestamp()
            except (KeyError, ValueError):
                continue
            if ts >= since - 120 and (best is None or ts < best[0]):
                best = (ts, m)
        return {"id": best[1]["id"], "url": best[1].get("permalink") or ""} if best else {"id": "", "url": ""}

    def finish_container(self, cid: str, since: float = 0) -> dict:
        """ОДНА перевірка контейнера й, якщо готовий, публікація. Повертає {"state", "status_code", "status", …}:
        published — опубліковано (id, url); waiting — IN_PROGRESS/PENDING (чекати далі той самий контейнер);
        unknown — невідомий статус (чекати, НЕ створювати новий); error / expired — підтверджена відмова Meta
        (можна наступний спосіб); already — контейнер уже PUBLISHED (не публікувати вдруге). Файл НЕ вантажимо."""
        p = self.poll_container(cid)
        if p["state"] == "ready":
            return {**p, **self.publish_container(cid), "state": "published"}
        if p["state"] == "already":
            return {**p, **self.find_published(since), "state": "already"}
        return p

    def wait_inline(self, cid: str, seconds: int) -> dict:
        """Чекати обробки до `seconds` (наростаючі паузи 10/20/40/80 с, «Стоп» — щосекунди). Повертає останню
        перевірку. Тайм-аут — це НЕ помилка: повертається стан waiting/unknown, контейнер чекає далі у фоні."""
        start = time.time()
        delays = iter(POLL_DELAYS)
        self.report("Instagram обробляє відео")
        while True:
            p = self.poll_container(cid)
            self.report("Instagram обробляє відео")
            if p["state"] not in ("waiting", "unknown"):
                p["waited_s"] = round(time.time() - start, 1)
                return p
            if p["state"] == "unknown":
                log.warning("Instagram: невідомий статус контейнера %s: %r — чекаю той самий контейнер",
                            cid, p["status_code"])
            pause = next(delays, POLL_DELAYS[-1])
            if usage_pct() >= 80:
                pause = max(pause, 120)
            if time.time() + pause - start > seconds:
                p["waited_s"] = round(time.time() - start, 1)
                return p
            for _ in range(pause):
                self._check_stop()
                time.sleep(1)

    def check(self) -> str:
        r = self._g().get(self.ig_id, fields="username")
        return f"@{r.get('username')}"

    def test_upload(self, clip: Path, label: str, params: dict | None = None,
                    wait_min: int = 20) -> tuple[bool | None, str]:
        """Один тест без публікації: контейнер → rupload → обробка. (True/False/None — пройшло / ні / невідомо, текст)."""
        g = self._g()
        size = clip.stat().st_size / 1048576
        t0 = time.time()
        try:
            c = g.post(f"{self.ig_id}/media", upload_type="resumable",
                       **(params or {"media_type": "REELS", "caption": "test", "share_to_feed": "false"}))
        except RateLimited:
            raise
        except PublishError as e:
            return False, f"{label}: ❌ контейнер не створено — {short_error(e)[:120]}"
        cid = c["id"]
        uri = c.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{g.version}/{cid}"
        err = None
        try:
            g.upload_binary(uri, str(clip))
        except PublishError as e:   # як у робочих версіях: після 400 не здаємось, а дивимось, чи контейнер обробляється
            err = f"rupload {short_error(e)[:60]} через {time.time() - t0:.0f} с"
        t1 = time.time()
        note = f"; попри {err}" if err else ""
        code = None
        for delay in (5, 10, 20, 30, 40, 60, 60, 60) + ((60,) * max(0, wait_min - 6) if err else ()):   # після 400 — до wait_min хв
            st = g.get(cid, fields="status_code,status")
            code = st.get("status_code")
            if code == "FINISHED":
                return True, (f"{label} ({size:.1f} МБ): ✅ прийнято (завантаження {t1 - t0:.0f} с, "
                              f"обробка {time.time() - t1:.0f} с{note})")
            if code in ("ERROR", "EXPIRED"):
                return False, f"{label} ({size:.1f} МБ): ❌ обробка — {st.get('status')}{note}"
            if err and code is None:
                break
            time.sleep(delay)
        if err:
            return False, f"{label} ({size:.1f} МБ): ❌ {err}; контейнер так і не обробився ({code}, чекали {(time.time() - t1) / 60:.0f} хв)"
        return None, f"{label} ({size:.1f} МБ): ⏳ обробка не завершилась ({code})"

    KINDS = (
        ("Reels у стрічці й на вкладці Reels (як публікує програма)",
         {"media_type": "REELS", "caption": "test", "share_to_feed": "true"}),
        ("Reels лише на вкладці Reels", {"media_type": "REELS", "caption": "test", "share_to_feed": "false"}),
        ("Історія (Stories)", {"media_type": "STORIES"}),
        ("Пробний Reels (trial, лише не-підписникам)",
         {"media_type": "REELS", "caption": "test", "trial_params": json.dumps({"graduation_strategy": "MANUAL"})}),
        ("Старий тип «відео-допис» (VIDEO)", {"media_type": "VIDEO", "caption": "test"}),
    )

    def test_kinds(self, workdir: Path, seconds: int = 59, report=lambda line: None) -> list[str]:
        """Те саме довге відео — різними типами публікації (без публікації): чи обмеження лише для Reels."""
        from app.core import media as _media
        workdir.mkdir(parents=True, exist_ok=True)
        clip = workdir / f"ig_kind_{seconds}.mp4"
        r = _media.run_hidden([self.cfg.get("ffmpeg", "ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
                               "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30", "-f", "lavfi",
                               "-i", "sine=frequency=440:sample_rate=48000", "-t", str(seconds), "-c:v", "libx264",
                               "-preset", "veryfast", "-pix_fmt", "yuv420p", "-maxrate", "6M", "-bufsize", "12M",
                               "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(clip)], timeout=300)
        if r.returncode != 0:
            return ["не вдалося підготувати тестовий ролик"]
        out = []
        try:
            for label, params in self.KINDS:
                try:
                    _, line = self.test_upload(clip, f"{label}, {seconds} с", params, wait_min=10)
                except RateLimited:
                    raise
                except Exception as e:  # noqa: BLE001
                    line = f"{label}: помилка тесту — {short_error(e)[:120]}"
                out.append(line)
                report(line)
        finally:
            clip.unlink(missing_ok=True)
        return out

    def test_sizes(self, workdir: Path, mbits=(1, 2, 3, 4, 6, 8), seconds: int = 59,
                   report=lambda line: None, stop_on_fail: bool = True) -> list[str]:
        """Те саме 59-секундне відео різного розміру (бітрейту), без публікації: чи межа — у розмірі файлу."""
        from app.core import media as _media
        workdir.mkdir(parents=True, exist_ok=True)
        out = []
        for mb in mbits:
            clip = workdir / f"ig_size_{mb}m.mp4"
            r = _media.run_hidden([self.cfg.get("ffmpeg", "ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
                                   "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30",
                                   "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                                   "-vf", "noise=alls=40:allf=t", "-t", str(seconds),
                                   "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                                   "-b:v", f"{mb}M", "-minrate", f"{mb}M", "-maxrate", f"{mb}M", "-bufsize", f"{mb * 2}M",
                                   "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(clip)], timeout=600)
            if r.returncode != 0:
                line = f"{mb} Мбіт/с: не вдалося підготувати"
            else:
                ok = None
                try:
                    ok, line = self.test_upload(clip, f"{seconds} с, {mb} Мбіт/с", wait_min=8)
                except RateLimited:
                    raise
                except Exception as e:  # noqa: BLE001
                    line = f"{mb} Мбіт/с: помилка тесту — {short_error(e)[:120]}"
                clip.unlink(missing_ok=True)
                if ok is False and stop_on_fail:   # межу знайдено — більші файли не вантажимо даремно
                    out.append(line)
                    report(line)
                    out.append("Більші розміри не перевіряю: межа між попереднім ✅ і цим ❌")
                    report(out[-1])
                    return out
            out.append(line)
            report(line)
        return out

    RES_VARIANTS = (("1080×1920", 1080, 1920), ("720×1280", 720, 1280), ("540×960", 540, 960))

    def test_resolutions(self, workdir: Path, original: str | None = None, seconds: int = 59,
                         report=lambda line: None) -> list[str]:
        """Те саме відео 59 с у 1080p / 720p / 540p (30 к/с), без публікації: чи Instagram встигає обробити
        менше пікселів. original — справжній шортс (інакше згенерована «складна» картинка)."""
        from app.core import media as _media
        workdir.mkdir(parents=True, exist_ok=True)
        out = []
        for label, w, h in self.RES_VARIANTS:
            clip = workdir / f"ig_res_{h}.mp4"
            src = (["-i", original, "-t", str(seconds)] if original else
                   ["-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30", "-f", "lavfi",
                    "-i", "sine=frequency=440:sample_rate=48000", "-t", str(seconds)])
            vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
            if not original:
                vf = "noise=alls=40:allf=t," + vf
            mb = max(1.0, 3.0 * w * h / (1080 * 1920))   # той самий «бітрейт на піксель»
            r = _media.run_hidden([self.cfg.get("ffmpeg", "ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", *src,
                                   "-vf", vf, "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
                                   "-pix_fmt", "yuv420p", "-b:v", f"{mb:.1f}M", "-maxrate", f"{mb:.1f}M",
                                   "-bufsize", f"{mb * 2:.1f}M", "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
                                   "-movflags", "+faststart", str(clip)], timeout=900)
            if r.returncode != 0:
                line = f"{label}: не вдалося підготувати"
            else:
                try:
                    _, line = self.test_upload(clip, f"{label}, {seconds} с", wait_min=6)
                except RateLimited:
                    raise
                except Exception as e:  # noqa: BLE001
                    line = f"{label}: помилка тесту — {short_error(e)[:120]}"
                clip.unlink(missing_ok=True)
            out.append(line)
            report(line)
        return out

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
                bad = r.status_code >= 400
                line = (f"{sec} с: ❌ rupload HTTP {r.status_code} за {time.time() - t0:.0f} с, "
                        f"контейнер не обробився за 20 хв — {r.text[:100]}" if bad else f"{sec} с: ❌ обробка не завершилась")
                # як у версіях 11–14.11: навіть після 400 чекаємо, чи контейнер обробиться
                for _ in range(120 if bad else 30):
                    st = rq.get(f"https://{host}/{ver}/{cid}", timeout=60, params={
                        "fields": "status_code,status", "access_token": token}).json()
                    if st.get("status_code") == "FINISHED":
                        line = f"{sec} с: ✅ прийнято (rupload {time.time() - t0:.0f} с" + (
                            f", попри HTTP {r.status_code})" if bad else ")")
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
        """Чи Instagram зараз приймає довге відео (згенероване, без публікації). Для щоденної перевірки.
        Складна картинка (рух + шум, 1080×1920, ~3 Мбіт/с), як справжні шортси: чорний кадр проходить завжди,
        тож по ньому не можна судити."""
        from app.core import media as _media
        workdir.mkdir(parents=True, exist_ok=True)
        clip = workdir / f"ig_watch_{seconds}.mp4"
        r = _media.run_hidden([self.cfg.get("ffmpeg", "ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
                               "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30", "-f", "lavfi",
                               "-i", "sine=frequency=440:sample_rate=48000", "-vf", "noise=alls=40:allf=t",
                               "-t", str(seconds), "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
                               "-pix_fmt", "yuv420p", "-b:v", "3M", "-maxrate", "3M", "-bufsize", "6M",
                               "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(clip)], timeout=600)
        if r.returncode != 0:
            return None, "не вдалося підготувати тестовий ролик"
        try:
            return self.test_upload(clip, f"{seconds} с, складне відео ~22 МБ", wait_min=8)
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
