"""Панель керування poster у браузері (http://127.0.0.1:8770). Код — app/web/.

    python panel.py [--config шлях] [--port 8771] [--no-browser]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.web.panel import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
