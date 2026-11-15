"""Instagram Reels і Facebook Reels через офіційний Graph API (завантаження файлу з ПК)."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

import requests

from .base import Platform, PublishError, RateLimited, Stopped, TokenExpired

log = logging.getLogger("meta")

TOKEN_ERROR_CODES = {102, 190, 463, 467}
# Ліміти запитів Meta: 4 — додатка (≈200 запитів/год на користувача), 17 — користувача, 32 — сторінки,
# 613 — частота, 80001–80014 — ліміти Business Use Case. Вікно — ковзна година.
RATE_LIMIT_CODES = {4, 17, 32, 613} | set(range(80001, 80015))
LIMIT_FILE = Path(tempfile.gettempdir()) / "poster-meta-limit.json"   # спільний для всіх каналів (один додаток Meta)
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

    def upload_binary(self, url: str, path: str) -> dict:
        size = os.path.getsize(path)
        headers = {"Authorization": f"OAuth {self.token}", "offset": "0", "file_size": str(size)}
        try:
            with open(path, "rb") as f:
                r = requests.post(url, headers=headers, data=f, timeout=1800)
        except requests.RequestException as e:
            raise PublishError(f"завантаження файлу: {e}") from e
        return self._check(r)


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
        big = size_mb > float(self.pcfg.get("max_upload_mb", 100) or 10 ** 6)
        # Порядок спроб: оригінал (якщо легкий і за вимогами) → копія під вимоги Reels (вона вже не раз
        # проходила і з важкими файлами) → сувора копія 10 Мбіт/с → крайній варіант 30 к/с.
        files = ([] if problems or big else [("оригінал", lambda: video_path)]) + \
            [("копія під вимоги Reels", lambda: media.instagram_copy(video_path, self.cfg))] + \
            [("сувора копія (та ж частота кадрів, 10 Мбіт/с)",
              lambda: media.instagram_copy(video_path, self.cfg, mode="safe60")),
             ("крайній варіант (30 к/с, 6 Мбіт/с)", lambda: media.instagram_copy(video_path, self.cfg, mode="safe30"))]
        errors = []
        via_fb = bool(self.pcfg.get("facebook_fallback", True) and self.secrets.get("FB_PAGE_ID"))
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
                return out
        try:
            about = media.ig_summary(media.ig_probe(video_path, self.cfg.get("ffprobe", "ffprobe")))
        except Exception:  # noqa: BLE001
            about = ""
        raise PublishError(f"Instagram не прийняв відео ({len(errors)} спроб): " + "; ".join(errors)
                           + (f". Файл: {about}" if about else "")
                           + ("" if len(tokens) > 1 else
                              " — Порада: у панелі (Instagram + Facebook) ще раз «Отримати сторінки» → «Підключити»,"
                              " щоб зберегти токен користувача для резервної спроби."))

    @staticmethod
    def _upload_rejected(e: Exception) -> bool:
        t = str(e).lower()
        return "processingfailed" in t or "processing failed" in t or "не прийняв завантаження" in t

    def _publish_file(self, video_path: str, meta, token: str | None = None) -> dict:
        g = self.graph(token) if token else self._g()
        c = g.post(f"{self.ig_id}/media", media_type="REELS", upload_type="resumable",
                   caption=meta.instagram_text[:2200],
                   share_to_feed="true" if self.pcfg.get("share_to_feed", True) else "false")
        cid = c["id"]
        uri = c.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{g.version}/{cid}"
        log.info("Instagram: контейнер %s, завантажую %s (%.1f МБ)", cid, video_path,
                 os.path.getsize(video_path) / 1024 / 1024)
        self.report(f"завантажую файл ({os.path.getsize(video_path) / 1024 / 1024:.0f} МБ)")
        upload_error = None
        try:
            g.upload_binary(uri, video_path)
        except PublishError as e:
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
        while True:
            st = g.get(cid, fields="status_code,status")
            code = st.get("status_code")
            self.report("Instagram обробляє відео")   # «живий»: остання перевірка щойно
            if code == "IN_PROGRESS" and upload_error and deadline < start + AFTER_400_WAIT:
                log.info("Instagram: попри помилку завантаження відео обробляється — чекаю до %s хв",
                         AFTER_400_WAIT // 60)
                deadline = start + AFTER_400_WAIT
            if code == "FINISHED":
                if upload_error:
                    log.info("Instagram: попри помилку завантаження контейнер готовий — публікую")
                break
            if code in ("ERROR", "EXPIRED"):
                reason = f"{st.get('status')}{ig_hint(st.get('status'))}"
                raise PublishError(f"{upload_error} | стан контейнера: {reason}" if upload_error
                                   else f"Instagram відхилив відео: {reason}")
            if time.time() > deadline:
                if upload_error:
                    waited = int((time.time() - start) / 60)
                    raise PublishError(f"{upload_error}{self._container_reason(g, cid)} (чекали {waited} хв)")
                raise PublishError("Instagram: обробка відео триває понад 20 хв")
            # перевіряємо рідше, що довше обробка: кожен запит з'їдає годинну квоту Meta; «Стоп» — щосекунди
            # квота Meta ≈200 запитів/год на весь додаток: опитування раз на 20 с, потім раз на хвилину,
            # а при квоті ≥80% — раз на 2 хв
            pause = 20 if time.time() - start < 120 else 60
            if usage_pct() >= 80:
                pause = 120
            for _ in range(pause):
                self._check_stop()
                time.sleep(1)

        self._check_stop()   # відео готове, але натиснули «Стоп» — не публікуємо
        self.report("публікую готове відео")
        mid = g.post(f"{self.ig_id}/media_publish", creation_id=cid)["id"]
        out = {"id": mid, "url": ""}
        try:  # після публікації — помилки не критичні (щоб не опублікувати двічі)
            out["url"] = g.get(mid, fields="permalink").get("permalink", "")
        except Exception as e:  # noqa: BLE001
            log.warning("Instagram permalink: %s", e)
        return out

    FB_LINK_WAIT = 10 * 60

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
