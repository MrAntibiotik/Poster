"""Код програми poster. Точки входу — poster.py (бот/служба), panel.py (панель), menu.py (меню) у корені.

Структура:
  app/core/       налаштування, база, відео, тексти (json), дублікати, ШІ
  app/engine/     розклад (черга, слоти), публікація
  app/platforms/  YouTube, Instagram/Facebook (Meta), TikTok
  app/telegram/   Telegram API і бот (картки, діалоги, черга, чистка чату…)
  app/web/        панель керування в браузері
  app/insights/   звіти й аналітика
  app/channels/   додаткові канали (EU/EN) і розділ EU в основному боті
  app/system/     запуск/зупинка, оновлення, сон ПК
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # папка програми (там poster.py, panel.py, channels/, .venv)


def read_version(root: Path = ROOT) -> str:
    """VERSION із config.py у корені (єдине місце; оновлювач старих версій шукає його саме там)."""
    try:
        m = re.search(r'^VERSION\s*=\s*"([^"]+)"', (root / "config.py").read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else "0"
    except OSError:
        return "0"


VERSION = read_version()
