"""poster — автопублікація шортсів на YouTube / TikTok / Instagram / Facebook
з підтвердженням через Telegram.

    python poster.py                 запустити службу (бот + розклад)
    python poster.py check           перевірити налаштування і доступи
    python poster.py auth youtube    авторизація YouTube (OAuth у браузері)
    python poster.py auth tiktok     авторизація TikTok
    python poster.py auth meta       токен сторінки Facebook + Instagram
    python poster.py queue           показати чергу
    python poster.py test-slot 10    позаплановий слот через 10 хв (для тесту)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
