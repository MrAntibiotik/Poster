"""Панель керування poster у браузері (лише цей ПК: http://127.0.0.1:8770).

Вхід за паролем (створюється при першому відкритті). Тут можна:
  • запускати / зупиняти / ставити на паузу бота, тестовий слот, режим тест/бойовий, автозапуск;
  • вписати токени й ключі (Telegram, YouTube, Meta, TikTok) і авторизувати платформи;
  • керувати тим, хто отримує сповіщення бота;
  • змінювати розклад і папки, дивитися чергу й журнал.

Запуск: panel.bat  (або python panel.py)"""
from __future__ import annotations

from .common import (  # константи й допоміжні функції модуля
    DEFAULT_PORT,
    FIELDS,
    PUBLIC_IDS,
    SECRET_LABELS,
    SESSION_HOURS,
    VERSION_NOW,
    cmd_to_text,
    coerce,
    dig,
    log,
    mask,
    put,
    tagmap_to_text,
)
from .base import BaseMixin
from .settings import SettingsMixin
from .auth import AuthMixin
from .actions import ActionsMixin
from .state import StateMixin


class Panel(BaseMixin, SettingsMixin, AuthMixin, ActionsMixin, StateMixin):
    """Вебпанель керування: складається з міксинів за темами."""


from .server import (  # noqa: E402 — серверу потрібен уже визначений Panel
    _restart_on_new_code,
    disk_version,
    main,
    make_handler,
    serve,
    show_fatal,
)


__all__ = ['DEFAULT_PORT', 'FIELDS', 'PUBLIC_IDS', 'Panel', 'SECRET_LABELS', 'SESSION_HOURS', 'VERSION_NOW', '_restart_on_new_code', 'cmd_to_text', 'coerce', 'dig', 'disk_version', 'log', 'main', 'make_handler', 'mask', 'put', 'serve', 'show_fatal', 'tagmap_to_text']
