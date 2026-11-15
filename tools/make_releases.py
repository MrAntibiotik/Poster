"""Оновлює releases.json — список версій програми з комітами, щоб можна було відкотитись на будь-яку
прямо з GitHub (бот /versions, панель «Історія версій»). Запуск перед кожним релізом:

    python tools/make_releases.py

Береться останній коміт кожної версії з історії git (за рядком VERSION у config.py).
Поточна (ще не закомічена) версія потрапить у файл наступним релізом — її й так видно як останню на main.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def main() -> None:
    out: dict[str, str] = {}
    for line in git("log", "--format=%H", "--", "config.py").split():
        try:
            text = git("show", f"{line}:config.py")
        except subprocess.CalledProcessError:
            continue
        m = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.M)
        if m and m.group(1) not in out:          # git log — від нових до старих: перший = останній коміт версії
            out[m.group(1)] = line
    path = ROOT / "releases.json"
    path.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print(f"releases.json: {len(out)} версій")


if __name__ == "__main__":
    main()
