"""Розклад і черга: сканування ready/, слоти, сповіщення за годину, пропуски, звіти."""
from __future__ import annotations

from .common import (  # константи й допоміжні функції модуля
    AUTO_FOLDER,
    SKIP_DIRS,
    find_videos,
    game_of,
    is_auto_folder,
    log,
)
from .base import BaseMixin
from .scan import ScanMixin
from .slots import SlotsMixin
from .publishing import PublishingMixin
from .periodic import PeriodicMixin
from .updates import UpdatesMixin
from .power import PowerMixin


class Scheduler(BaseMixin, ScanMixin, SlotsMixin, PublishingMixin, PeriodicMixin, UpdatesMixin, PowerMixin):
    pass


__all__ = ['AUTO_FOLDER', 'SKIP_DIRS', 'Scheduler', 'find_videos', 'game_of', 'is_auto_folder', 'log']
