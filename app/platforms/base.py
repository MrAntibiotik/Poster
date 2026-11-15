"""Спільний інтерфейс для модулів платформ."""
from __future__ import annotations

from pathlib import Path


class TokenExpired(Exception):
    """Токен протерміновано/відкликано — потрібна повторна авторизація (без повторних спроб)."""


class PublishError(Exception):
    """Будь-яка інша помилка публікації (буде повторена згідно з retry)."""


class Stopped(Exception):
    """Натиснуто «⛔ Стоп публікацій» посеред публікації — платформу не публікуємо, продовжимо після «Відновити»."""


class RateLimited(PublishError):
    """Платформа тимчасово обмежила кількість запитів. retry_at — коли можна знову (unix-час)."""

    def __init__(self, msg: str, retry_at: int):
        super().__init__(msg)
        self.retry_at = int(retry_at)


class Platform:
    key = ""
    name = ""

    should_stop = staticmethod(lambda: False)   # Publisher підставляє перевірку «⛔ Стоп публікацій»
    report = staticmethod(lambda phase: None)    # Publisher підставляє: етап публікації для картки («обробка…»)

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.pcfg = cfg["platforms"].get(self.key, {})
        self.tokens_dir = Path(cfg["paths"]["tokens"])
        self.secrets = cfg["secrets"]

    def publish(self, video_path: str, meta) -> dict:
        """Публікує ОРИГІНАЛЬНИЙ файл. Повертає {'id', 'url', 'note'?, ...}."""
        raise NotImplementedError

    def get_views(self, result: dict) -> int | None:
        raise NotImplementedError

    def get_stats(self, result: dict) -> dict:
        """Статистика поста: {'views', 'likes', 'comments'} (None — недоступно)."""
        return {"views": self.get_views(result)}

    def account_stats(self) -> dict:
        """Статистика акаунта/каналу: {'followers', ...} або {} якщо недоступно."""
        return {}

    def check(self) -> str:
        """Перевірка доступу; повертає короткий опис акаунта або кидає виняток."""
        raise NotImplementedError

    def token_warning(self) -> str | None:
        """Попередження, якщо токен скоро протермінується (None — усе гаразд)."""
        return None

    @property
    def reauth_hint(self) -> str:
        return f"python poster.py auth {self.key}"
