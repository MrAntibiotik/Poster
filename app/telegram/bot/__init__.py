"""Telegram-бот: сповіщення-відео з кнопками підтвердження, команди, доступ для кількох людей.

Ролі:
  admin  — отримує все й керує (кнопки під шортсами, команди, запити на доступ);
           головні адміністратори задаються в .env (TELEGRAM_CHAT_ID, можна кілька через кому);
  viewer — отримує шортси (без кнопок), опубліковані посилання, звіти;
  pending/blocked — запит на доступ / відхилено. Усіх інших бот ігнорує."""
from __future__ import annotations

from .common import (  # константи й допоміжні функції модуля
    AUTO_TTL,
    BTN,
    CAPTION_LIMIT,
    COMMANDS,
    HELP_ADMIN,
    HELP_VIEWER,
    KBD_VERSION,
    REPLY_TTL,
    VIEWER_CMDS,
    esc,
    log,
    parse_ids,
    user_name,
    visible_len,
)
from .base import BaseMixin
from .chat import ChatMixin
from .cards import CardsMixin
from .queue import QueueMixin
from .dialog import DialogMixin
from .failed import FailedMixin
from .info import InfoMixin
from .callbacks import CallbacksMixin
from .commands import CommandsMixin
from .panel import PanelMixin
from .versions import VersionsMixin
from .igmanual import IgManualMixin


class Bot(BaseMixin, ChatMixin, CardsMixin, QueueMixin, DialogMixin, FailedMixin, InfoMixin, CallbacksMixin, CommandsMixin, PanelMixin, VersionsMixin, IgManualMixin):
    pass


__all__ = ['AUTO_TTL', 'BTN', 'Bot', 'CAPTION_LIMIT', 'COMMANDS', 'HELP_ADMIN', 'HELP_VIEWER', 'KBD_VERSION', 'REPLY_TTL', 'VIEWER_CMDS', 'esc', 'log', 'parse_ids', 'user_name', 'visible_len']
