"""HTTP-сервер панелі: маршрути API, віддача сторінки, запуск і самоперезапуск при новій версії коду."""
from __future__ import annotations
import json
import os
import re
import sys
import threading
import time
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from app import ROOT  # noqa: E402  папка програми
from app.system import control  # noqa: E402
from app.core.config import ConfigError
from .common import log, SESSION_HOURS
from . import Panel


def make_handler(panel: Panel):
    static = Path(__file__).resolve().parent.parent / "static"   # сторінка панелі: розмітка, стилі, скрипт
    files = {"/": ("index.html", "text/html; charset=utf-8"),
             "/index.html": ("index.html", "text/html; charset=utf-8"),
             "/static/panel.css": ("panel.css", "text/css; charset=utf-8"),
             "/static/panel.js": ("panel.js", "application/javascript; charset=utf-8")}
    assets = {path: ((static / name).read_bytes(), ctype) for path, (name, ctype) in files.items()}
    allowed_hosts = {f"127.0.0.1:{panel.port}", f"localhost:{panel.port}"}

    class H(BaseHTTPRequestHandler):
        server_version = "poster-panel"

        def log_message(self, fmt, *args):
            log.debug("panel: " + fmt, *args)

        # ---- helpers ----
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                log.debug("Браузер закрив з'єднання до відповіді")

        def json(self, data, code: int = 200, extra: dict | None = None):
            self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

        def session(self) -> str | None:
            c = SimpleCookie(self.headers.get("Cookie") or "")
            return c["poster_session"].value if "poster_session" in c else None

        def authed(self) -> bool:
            return panel.valid_session(self.session())

        def host_ok(self) -> bool:
            return (self.headers.get("Host") or "") in allowed_hosts

        def body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 2_000_000:
                raise ValueError("Завеликий запит")
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw or b"{}")

        def cookie(self, token: str, max_age: int) -> dict:
            return {"Set-Cookie": f"poster_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}"}

        # ---- GET ----
        def do_GET(self):  # noqa: N802
            if not self.host_ok():
                return self._send(403, b"Forbidden", "text/plain")
            if self.path in assets:
                body, ctype = assets[self.path]
                return self._send(200, body, ctype,
                                  {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                              "script-src 'self' 'unsafe-inline'; img-src 'self' data:"})
            if self.path == "/api/session":
                from app.core.config import VERSION
                return self.json({"authed": self.authed(), "has_password": panel.has_password(), "version": VERSION})
            if self.path == "/api/state":
                if not self.authed():
                    return self.json({"error": "auth"}, 401)
                try:
                    return self.json(panel.state())
                except Exception as e:  # noqa: BLE001
                    log.exception("state")
                    return self.json({"error": str(e)}, 500)
            if self.path == "/api/versions":
                if not self.authed():
                    return self.json({"error": "auth"}, 401)
                from app.system import updater
                return self.json({"versions": updater.version_list(panel.cfg),
                                  "pinned": (panel.cfg.get("updates") or {}).get("pinned") or ""})
            if self.path.startswith("/api/video?"):
                if not self.authed():
                    return self._send(401, b"auth", "text/plain")
                return self.video()
            return self._send(404, b"Not found", "text/plain")

        def video(self):
            from urllib.parse import parse_qs, urlparse
            try:
                item = panel.db.get_item(int(parse_qs(urlparse(self.path).query)["id"][0]))
                path = Path(item["mp4"])
                size = path.stat().st_size
            except (KeyError, ValueError, TypeError, OSError):
                return self._send(404, b"Not found", "text/plain")
            start, end, code = 0, size - 1, 200
            m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                code = 206
            self.send_response(code)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "private, max-age=600")
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            try:
                with open(path, "rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = f.read(min(256 * 1024, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except (ConnectionError, OSError):
                pass  # браузер перервав завантаження (перемотування) — це нормально

        # ---- POST ----
        def do_POST(self):  # noqa: N802
            if not self.host_ok() or self.headers.get("X-Poster") != "1":
                return self._send(403, b"Forbidden", "text/plain")
            try:
                data = self.body()
            except ValueError as e:
                return self.json({"error": str(e)}, 400)
            path = self.path
            try:
                if path == "/api/setup":
                    if panel.has_password():
                        return self.json({"error": "Пароль уже встановлено"}, 400)
                    panel.set_password(str(data.get("password") or ""))
                    token = panel.new_session()
                    return self.json({"ok": True}, extra=self.cookie(token, SESSION_HOURS * 3600))
                if path == "/api/login":
                    if panel.check_password(str(data.get("password") or "")):
                        token = panel.new_session()
                        return self.json({"ok": True}, extra=self.cookie(token, SESSION_HOURS * 3600))
                    time.sleep(1.5)
                    return self.json({"error": "Неправильний пароль"}, 403)
                if not self.authed():
                    return self.json({"error": "auth"}, 401)
                if path == "/api/logout":
                    panel.sessions.pop(self.session() or "", None)
                    return self.json({"ok": True}, extra=self.cookie("", 0))
                if path == "/api/password":
                    if not panel.check_password(str(data.get("old") or "")):
                        return self.json({"error": "Поточний пароль неправильний"}, 403)
                    panel.set_password(str(data.get("new") or ""))
                    return self.json({"message": "Пароль змінено"})
                if path == "/api/action":
                    msg = panel.action(str(data.get("name")), data)
                    log.info("Дія «%s»: %s", data.get("name"), str(msg)[:300])
                    return self.json({"message": msg})
                if path == "/api/settings":
                    panel.save_settings(data)
                    return self.json({"message": "Налаштування збережено"})
                if path == "/api/secrets":
                    panel.save_secrets(data)
                    return self.json({"message": "Збережено"})
                if path == "/api/users":
                    return self.json({"message": panel.set_user(int(data["chat_id"]), str(data["role"]))})
                if path == "/api/youtube/secret":
                    return self.json({"message": panel.youtube_secret(str(data.get("content") or ""))})
                if path == "/api/auth/youtube":
                    return self.json({"message": panel.youtube_auth()})
                if path == "/api/auth/meta":
                    return self.json({"pages": panel.meta_exchange(str(data.get("token") or ""))})
                if path == "/api/auth/meta/select":
                    return self.json({"message": panel.meta_select(int(data.get("index", -1)))})
                if path == "/api/auth/tiktok":
                    return self.json(panel.tiktok_start())
                if path == "/api/auth/tiktok/finish":
                    return self.json({"message": panel.tiktok_finish(str(data.get("url") or ""))})
                if path == "/api/fs":
                    return self.json(panel.list_dir(data.get("path")))
                if path == "/api/fs/mkdir":
                    return self.json(panel.make_dir(str(data.get("path") or ""), str(data.get("name") or "")))
                if path == "/api/pick_folder":
                    picked = panel.pick_folder_native(str(data.get("initial") or ""))
                    return self.json({"path": picked, "native": picked is not None})
                if path == "/api/shutdown":
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return self.json({"message": "Панель закрито"})
                return self.json({"error": "Not found"}, 404)
            except (ValueError, KeyError, TypeError) as e:
                log.warning("Панель: %s %s — помилка: %s", path, data.get("name") or "", e)
                return self.json({"error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001
                log.exception("panel %s", path)
                return self.json({"error": f"{type(e).__name__}: {e}"}, 500)

    return H


def disk_version() -> str:
    """VERSION у файлі config.py на диску (після оновлення він новіший, ніж код у пам'яті цієї панелі)."""
    from app import read_version
    v = read_version()
    return "" if v == "0" else v


def _restart_on_new_code(httpd, every: int = 30) -> None:
    """Код оновився (панель відкрита давно, напр. панель EN-каналу) — запускаємо нову панель на тому ж порту,
    вона сама закриє цю. Так панель ніколи не лишається на старій версії."""
    from app.core.config import VERSION
    import subprocess
    while True:
        time.sleep(every)
        new = disk_version()
        if new and new != VERSION:
            port = next((a for a in sys.argv[sys.argv.index("--port") + 1:][:1]), "8770") if "--port" in sys.argv else "8770"
            if not control.spawn_allowed(f"panel-{port}", 180):
                continue   # нову панель на цьому порту вже запускає хтось інший
            log.info("Код оновлено (%s → %s) — перезапускаю панель", VERSION, new)
            args = [control.python_exe(windowless=True), str(ROOT / "panel.py"), *sys.argv[1:]]
            if "--no-browser" not in args:
                args.append("--no-browser")
            try:
                flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW) if control.WIN else 0
                subprocess.Popen(args, cwd=str(ROOT), creationflags=flags, close_fds=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=not control.WIN)
            except OSError as e:
                log.warning("Перезапуск панелі: %s", e)
            return


def serve(config_path: str | None = None, open_browser: bool = True, port: int | None = None) -> int:
    from app.core.config import setup_logging

    try:
        panel = Panel(config_path)
    except ConfigError as e:
        show_fatal(f"Помилка налаштувань: {e}")
        return 2
    setup_logging(panel.cfg)
    if port:
        panel.port = port
    url = f"http://127.0.0.1:{panel.port}/"
    from app.core.config import VERSION
    pid_file = Path(panel.cfg["paths"]["data"]) / "panel.pid"
    httpd = None
    for attempt in range(3):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", panel.port), make_handler(panel))
            break
        except OSError:
            # Порт зайнятий старою копією панелі (напр. після оновлення) — закриваємо її і пробуємо ще раз
            # хто справді тримає порт (стара версія панелі могла не записати PID або файл застарів)
            owner = control.port_owner(panel.port) or {}
            if "panel.py" in str(owner.get("cmd") or "") and owner.get("pid"):
                old = int(owner["pid"])
            else:
                old = int(pid_file.read_text().strip() or 0) if pid_file.exists() else 0
            if attempt < 2 and old and old != os.getpid():
                log.info("Закриваю попередню копію панелі (PID %s)", old)
                control.kill(old)
                time.sleep(1.5)
                continue
            if attempt < 2:  # не знайшли, хто тримає порт, — закриваємо всі старі копії панелі на цьому порту
                n = control.kill_other_panels(panel.port)
                log.info("Закрито старих копій панелі: %s", n)
                if n:
                    time.sleep(1.5)
                    continue
            log.info("Панель уже відкрита — відкриваю браузер")
            if open_browser:
                webbrowser.open(url)
            return 0
    try:
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    log.info("Панель керування %s: %s", VERSION, url)
    threading.Thread(target=control.ensure_shortcut_once, args=(panel.db,), daemon=True).start()
    threading.Thread(target=_restart_on_new_code, args=(httpd,), daemon=True, name="panel-code-watch").start()

    def _channels_watch():  # основна панель стежить, щоб панелі каналів (EN) не лишались на старому коді
        from app.channels import manager as channels
        while True:
            try:
                channels.refresh_panels(panel.cfg)
            except Exception as e:  # noqa: BLE001
                log.debug("refresh_panels: %s", e)
            time.sleep(300)
    if not panel.cfg.get("channel"):
        threading.Thread(target=_channels_watch, daemon=True, name="channel-panels-watch").start()
    if sys.stdout:
        print(f"Панель керування: {url}  (Ctrl+C — закрити)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()
    return 0


def show_fatal(text: str) -> None:
    """Без консолі (pythonw) помилку інакше ніхто не побачить: пишемо у файл і показуємо вікно Windows."""
    try:
        (ROOT / "panel_error.txt").write_text(text, encoding="utf-8")
    except OSError:
        pass
    if sys.stderr is not None:
        print(text, file=sys.stderr)
    if control.WIN:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, text[-1500:] + "\n\nТекст помилки також збережено у panel_error.txt — надішліть його.",
                "poster — панель не запустилась", 0x10)
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    import argparse
    import traceback

    ap = argparse.ArgumentParser(description="Панель керування poster")
    ap.add_argument("--config")
    ap.add_argument("--port", type=int)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    try:
        code = serve(a.config, not a.no_browser, a.port)
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        show_fatal(traceback.format_exc())
        code = 1
    return code
