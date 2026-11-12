"""Instagram Reels і Facebook Reels через офіційний Graph API (завантаження файлу з ПК)."""
from __future__ import annotations

import logging
import os
import time

import requests

from .base import Platform, PublishError, TokenExpired

log = logging.getLogger("meta")

TOKEN_ERROR_CODES = {102, 190, 463, 467}


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
            raise PublishError(f"Meta ({code}/{err.get('error_subcode')}): {msg}")
        if r.status_code >= 400:
            raise PublishError(f"Graph API HTTP {r.status_code}: {r.text[:200]}")
        return j

    def get(self, path: str, **params) -> dict:
        params["access_token"] = self.token
        try:
            r = requests.get(f"{self.base}/{path.lstrip('/')}", params=params, timeout=60)
        except requests.RequestException as e:
            raise PublishError(f"мережа: {e}") from e
        return self._check(r)

    def post(self, path: str, **data) -> dict:
        data["access_token"] = self.token
        try:
            r = requests.post(f"{self.base}/{path.lstrip('/')}", data=data, timeout=120)
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
        import media
        try:
            problems = media.ig_problems(media.ig_probe(video_path, self.cfg.get("ffprobe", "ffprobe")))
        except OSError:
            problems = []
        tokens = [("сторінки", None)]
        user = self.secrets.get("META_USER_TOKEN")
        if user and user != self._g().token:
            tokens.append(("користувача", user))
        files = ([] if problems else [("оригінал", lambda: video_path)]) + \
            [("копія під вимоги Reels", lambda: media.instagram_copy(video_path, self.cfg)),
             ("легка копія (6 Мбіт/с)", lambda: media.instagram_copy(video_path, self.cfg, lite=True))]
        errors = []
        for tname, token in tokens:
            for fname, get_file in files:
                try:
                    out = self._publish_file(get_file(), meta, token)
                except PublishError as e:
                    if not self._upload_rejected(e):
                        raise  # інша помилка (токен, ліміт, формат) — без перебору
                    log.warning("Instagram: %s + токен %s — відмова: %s", fname, tname, e)
                    errors.append(f"{fname}/токен {tname}: {e}")
                    continue
                notes = []
                if problems:
                    notes.append("перекодовано під вимоги Reels (" + ", ".join(problems) + ")")
                elif fname != "оригінал":
                    notes.append("Instagram не прийняв оригінал — опубліковано копію під вимоги Reels")
                if token:
                    notes.append("завантажено з токеном користувача")
                if notes:
                    out["note"] = "Instagram: " + "; ".join(notes)
                return out
        raise PublishError("Instagram не прийняв завантаження жодним способом. " + " || ".join(errors)
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
        upload_error = None
        for attempt in range(2):  # 400 від rupload буває випадковою — одна повторна відправка того ж файлу
            try:
                g.upload_binary(uri, video_path)
                upload_error = None
                break
            except PublishError as e:
                upload_error = e  # буває хибна 400 — відео все одно обробляється; перевіримо контейнер
                log.warning("Instagram rupload (спроба %s): %s", attempt + 1, e)
                if attempt == 0:
                    st0 = g.get(cid, fields="status_code").get("status_code")
                    if st0 == "FINISHED":
                        break
                    time.sleep(10)

        start = time.time()
        deadline = start + (3 * 60 if upload_error else 20 * 60)
        while True:
            st = g.get(cid, fields="status_code,status")
            code = st.get("status_code")
            if code == "IN_PROGRESS" and upload_error and deadline < start + 10 * 60:
                # 400 від rupload могла бути хибною: відео дійшло й обробляється — чекаємо довше
                log.info("Instagram: попри помилку завантаження відео обробляється — чекаю до 10 хв")
                deadline = start + 10 * 60
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
            time.sleep(10)

        mid = g.post(f"{self.ig_id}/media_publish", creation_id=cid)["id"]
        out = {"id": mid, "url": ""}
        try:  # після публікації — помилки не критичні (щоб не опублікувати двічі)
            out["url"] = g.get(mid, fields="permalink").get("permalink", "")
        except Exception as e:  # noqa: BLE001
            log.warning("Instagram permalink: %s", e)
        return out

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

    def get_views(self, result: dict) -> int | None:
        g = self._g()
        for metric in ("views", "plays", "reach"):
            try:
                data = g.get(f"{result['id']}/insights", metric=metric).get("data") or []
            except PublishError:
                continue
            if data:
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
            r = self._g().get(result["id"], fields="like_count,comments_count")
            out.update(likes=r.get("like_count"), comments=r.get("comments_count"))
        except PublishError as e:
            log.debug("IG likes: %s", e)
        return out

    def account_stats(self) -> dict:
        r = self._g().get(self.ig_id, fields="followers_count,media_count")
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
            time.sleep(10)
        out["note"] = "Facebook ще обробляє відео — з'явиться на сторінці трохи пізніше"
        return out

    def get_views(self, result: dict) -> int | None:
        data = self.graph().get(f"{result['id']}/video_insights").get("data") or []
        by_name = {d.get("name"): d for d in data}
        for name in ("fb_reels_total_plays", "blue_reels_play_count", "post_video_views", "total_video_views"):
            d = by_name.get(name)
            if d and d.get("values"):
                return int(d["values"][0].get("value") or 0)
        return None

    def get_stats(self, result: dict) -> dict:
        out = {"views": self.get_views(result)}
        try:
            r = self.graph().get(result["id"], fields="likes.summary(true).limit(0),comments.summary(true).limit(0)")
            out.update(likes=((r.get("likes") or {}).get("summary") or {}).get("total_count"),
                       comments=((r.get("comments") or {}).get("summary") or {}).get("total_count"))
        except PublishError as e:
            log.debug("FB likes: %s", e)
        return out

    def account_stats(self) -> dict:
        r = self.graph().get(self.page_id, fields="followers_count,fan_count")
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
