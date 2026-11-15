"""Підключення платформ: YouTube, Meta (Instagram/Facebook), TikTok."""
from __future__ import annotations

import json
import os
from pathlib import Path


class AuthMixin:
    def youtube_secret(self, content: str) -> str:
        try:
            data = json.loads(content)
        except ValueError as e:
            raise ValueError("Це не JSON-файл") from e
        if not isinstance(data, dict) or not ({"installed", "web"} & set(data)):
            raise ValueError("Це не client_secret від Google (немає розділу installed/web)")
        if "web" in data and "installed" not in data:
            raise ValueError("Потрібен OAuth-клієнт типу «Desktop app», а не «Web application»")
        path = Path(self.cfg["platforms"]["youtube"]["client_secrets_file"])
        if self.cfg.get("channel"):
            # додатковий канал (EU): свій ключ — у своїй папці. НІКОЛИ не перезаписувати ключ основного каналу.
            path = Path(self.cfg["root"]) / "client_secret.json"
            raw = self.raw_config()
            raw.setdefault("platforms", {}).setdefault("youtube", {})["client_secrets_file"] = "client_secret.json"
            Path(self.cfg["config_path"]).write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        old_id = ""
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            old_id = (old.get("installed") or old.get("web") or {}).get("client_id", "")
        except (OSError, ValueError):
            pass
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        new_id = (data.get("installed") or {}).get("client_id", "")
        token = Path(self.cfg["paths"]["tokens"]) / "youtube_token.json"
        if old_id != new_id and token.exists():
            token.unlink()                    # токен від іншого ключа не працюватиме — авторизуйтесь заново
        self.reload()
        return f"Збережено {path}" + (" — тепер натисніть «Авторизувати YouTube»" if not token.exists() else "")

    def youtube_auth(self) -> str:
        from app.platforms.youtube import YouTube
        if not Path(self.cfg["platforms"]["youtube"]["client_secrets_file"]).exists():
            raise ValueError("Спершу завантажте client_secret.json")
        yt = YouTube(self.reload())

        def show(url):                         # браузер міг не відкритися (панель у фоні) — посилання в панелі
            if self.jobs.get("youtube_auth", {}).get("state") == "running":
                self.jobs["youtube_auth"]["message"] = f"Увійдіть у Google за посиланням (5 хв): {url}"
        self.job("youtube_auth", lambda: (yt.authorize(on_url=show, timeout_s=300),
                                          f"YouTube підключено: {yt.check()}")[1])
        return "Відкриваю вхід у Google… Якщо браузер не відкрився — посилання з'явиться під кнопкою."

    def meta_exchange(self, token: str) -> list[dict]:
        from app.platforms.meta_graph import meta_pages
        self.meta_pages = meta_pages(self.reload(), token)
        return [{"index": i, "name": p["name"], "id": p["id"],
                 "instagram": (p.get("instagram_business_account") or {}).get("username")}
                for i, p in enumerate(self.meta_pages)]

    def meta_select(self, index: int) -> str:
        from app.platforms.meta_graph import meta_save
        if not 0 <= index < len(self.meta_pages):
            raise ValueError("Спершу отримайте список сторінок")
        msg = meta_save(self.cfg, self.meta_pages[index])
        self.meta_pages = []
        for k in ("META_PAGE_TOKEN", "FB_PAGE_ID", "IG_USER_ID", "META_USER_TOKEN"):
            os.environ.pop(k, None)
        self.reload()
        return msg

    def tiktok_start(self) -> dict:
        from app.platforms.tiktok import TikTok
        tt = TikTok(self.reload())
        flow = tt.auth_begin()
        self.tiktok_flow = flow
        if flow["local"]:
            def run():
                back = tt.auth_wait_local(flow)
                tok = tt.auth_finish(flow, back)
                return f"TikTok підключено (дозволи: {tok.get('scope')})"
            self.job("tiktok_auth", run)
        return {"url": flow["url"], "local": flow["local"]}

    def tiktok_finish(self, url: str) -> str:
        from app.platforms.tiktok import TikTok
        if not self.tiktok_flow:
            raise ValueError("Спершу натисніть «Авторизувати»")
        tok = TikTok(self.cfg).auth_finish(self.tiktok_flow, url)
        self.tiktok_flow = None
        return f"TikTok підключено (дозволи: {tok.get('scope')})"
