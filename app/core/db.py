"""Локальний стан (SQLite): черга, слоти, журнал подій. Переживає перезапуск ПК."""
from __future__ import annotations

import json
import sqlite3
import threading
from app.core.config import now_ts
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mp4           TEXT NOT NULL UNIQUE,
    json          TEXT NOT NULL,
    game          TEXT DEFAULT '',
    added_at      INTEGER NOT NULL,
    qpos          INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',
    skips         INTEGER NOT NULL DEFAULT 0,
    slot_at       INTEGER,
    tg_msg        INTEGER,
    tg_msg_is_text INTEGER NOT NULL DEFAULT 0,
    tg_text_msg   INTEGER,
    disabled      TEXT NOT NULL DEFAULT '[]',
    title_override TEXT,
    edited        TEXT NOT NULL DEFAULT '[]',
    results       TEXT NOT NULL DEFAULT '{}',
    posted_at     INTEGER,
    stats_sent    INTEGER NOT NULL DEFAULT 0,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS items_status ON items(status, qpos);

CREATE TABLE IF NOT EXISTS slots (
    slot_at  INTEGER PRIMARY KEY,
    source   TEXT NOT NULL DEFAULT 'config',
    item_id  INTEGER,
    state    TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    item_id  INTEGER,
    kind     TEXT NOT NULL,
    details  TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS users (
    chat_id   INTEGER PRIMARY KEY,
    name      TEXT,
    role      TEXT NOT NULL DEFAULT 'pending',
    added_at  INTEGER
);

CREATE TABLE IF NOT EXISTS tg_msgs (
    item_id     INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    msg_id      INTEGER NOT NULL,
    text_msg_id INTEGER,
    is_text     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (item_id, chat_id)
);

CREATE TABLE IF NOT EXISTS post_stats (
    item_id  INTEGER NOT NULL,
    platform TEXT NOT NULL,
    ts       INTEGER NOT NULL,
    views    INTEGER,
    likes    INTEGER,
    comments INTEGER
);
CREATE INDEX IF NOT EXISTS post_stats_idx ON post_stats(item_id, platform, ts);

CREATE TABLE IF NOT EXISTS account_stats (
    platform  TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    followers INTEGER,
    views     INTEGER,
    likes     INTEGER
);
CREATE INDEX IF NOT EXISTS account_stats_idx ON account_stats(platform, ts);

-- історія публікацій: кожна спроба кожного відео на кожній платформі (для аналізу закономірностей)
CREATE TABLE IF NOT EXISTS history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL,
    item_id      INTEGER,
    platform     TEXT NOT NULL,
    result       TEXT NOT NULL,
    file         TEXT,
    title        TEXT,
    duration     REAL,
    width        INTEGER,
    height       INTEGER,
    fps          REAL,
    vkbps        INTEGER,
    size_mb      REAL,
    sent_file    TEXT,
    sent_mb      REAL,
    method       TEXT,
    upload_http  INTEGER,
    upload_s     REAL,
    processing_s REAL,
    total_s      REAL,
    url          TEXT,
    error        TEXT,
    version      TEXT,
    stage        TEXT,
    upload_body  TEXT,
    container_id TEXT,
    container_status TEXT,
    container_status_text TEXT
);
CREATE INDEX IF NOT EXISTS history_idx ON history(item_id, platform, ts);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

JSON_COLS = {"disabled": list, "edited": list, "results": dict}
# Статуси, у яких шортс ще «живий» у робочому процесі
ACTIVE = ("queued", "notified", "approved", "publishing", "failed")


class DB:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(items)")}
            if "fp" not in cols:  # відбиток вмісту — захист від повторних публікацій (dupes.py)
                self.conn.execute("ALTER TABLE items ADD COLUMN fp TEXT")
            self.conn.execute("CREATE INDEX IF NOT EXISTS items_fp ON items(fp)")
            # нові колонки в старих базах: тип відео (short / highlight) і «доріжка» слоту
            for table, col, ddl in (("items", "kind", "TEXT NOT NULL DEFAULT 'short'"),
                                    ("slots", "lane", "TEXT NOT NULL DEFAULT 'short'"),
                                    # історія публікацій: де саме зламалось і що відповів Instagram
                                    ("history", "stage", "TEXT"), ("history", "upload_body", "TEXT"),
                                    ("history", "container_id", "TEXT"), ("history", "container_status", "TEXT"),
                                    ("history", "container_status_text", "TEXT")):
                cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if col not in cols:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

    # ---- базові операції ----
    def q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args=()) -> int:
        with self.lock:
            return self.conn.execute(sql, args).lastrowid

    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    # ---- items ----
    @staticmethod
    def _item(row: dict | None) -> dict | None:
        if row is None:
            return None
        for col, typ in JSON_COLS.items():
            try:
                row[col] = json.loads(row[col] or "null") or typ()
            except (TypeError, ValueError):
                row[col] = typ()
        return row

    def get_item(self, item_id: int) -> dict | None:
        return self._item(self.one("SELECT * FROM items WHERE id=?", (item_id,)))

    def items(self, where: str = "1", args=(), order: str = "qpos") -> list[dict]:
        return [self._item(r) for r in self.q(f"SELECT * FROM items WHERE {where} ORDER BY {order}", args)]

    def update_item(self, item_id: int, **fields) -> None:
        if not fields:
            return
        sets, vals = [], []
        for k, v in fields.items():
            if k in JSON_COLS:
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k}=?")
            vals.append(v)
        vals.append(item_id)
        self.x(f"UPDATE items SET {', '.join(sets)} WHERE id=?", vals)

    def add_item(self, mp4: str, json_path: str, game: str, kind: str = "short",
                 disabled: list[str] | None = None) -> int | None:
        with self.tx():
            if self.conn.execute("SELECT 1 FROM items WHERE mp4=?", (mp4,)).fetchone():
                return None
            qpos = self.next_qpos()
            cur = self.conn.execute(
                "INSERT INTO items(mp4, json, game, added_at, qpos, kind, disabled) VALUES (?,?,?,?,?,?,?)",
                (mp4, json_path, game, now_ts(), qpos, kind, json.dumps(disabled or [])),
            )
            return cur.lastrowid

    def next_qpos(self) -> int:
        r = self.one("SELECT COALESCE(MAX(qpos), 0) AS m FROM items")
        return int(r["m"]) + 1

    def first_qpos(self) -> int:
        r = self.one("SELECT COALESCE(MIN(qpos), 1) AS m FROM items WHERE status='queued'")
        return int(r["m"]) - 1

    def queue(self, kind: str | None = None) -> list[dict]:
        """Черга; kind — лише шортси ("short") або лише хайлайти ("highlight")."""
        if kind:
            return self.items("status='queued' AND kind=?", (kind,))
        return self.items("status='queued'")

    def known_paths(self) -> set[str]:
        return {r["mp4"] for r in self.q("SELECT mp4 FROM items")}

    # ---- slots ----
    def slot(self, slot_at: int) -> dict | None:
        return self.one("SELECT * FROM slots WHERE slot_at=?", (slot_at,))

    def set_slot(self, slot_at: int, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE slots SET {sets} WHERE slot_at=?", (*fields.values(), slot_at))

    # ---- користувачі Telegram ----
    ROLES = ("admin", "viewer", "pending", "blocked")

    def users(self, *roles: str) -> list[dict]:
        if roles:
            marks = ",".join("?" * len(roles))
            return self.q(f"SELECT * FROM users WHERE role IN ({marks}) ORDER BY added_at", roles)
        return self.q("SELECT * FROM users ORDER BY role, added_at")

    def user(self, chat_id: int) -> dict | None:
        return self.one("SELECT * FROM users WHERE chat_id=?", (int(chat_id),))

    def set_user(self, chat_id: int, role: str, name: str | None = None) -> None:
        if role not in self.ROLES:
            raise ValueError(role)
        self.x("INSERT INTO users(chat_id, name, role, added_at) VALUES (?,?,?,?) "
               "ON CONFLICT(chat_id) DO UPDATE SET role=excluded.role, name=COALESCE(excluded.name, users.name)",
               (int(chat_id), name, role, now_ts()))

    def del_user(self, chat_id: int) -> None:
        self.x("DELETE FROM users WHERE chat_id=?", (int(chat_id),))

    # ---- повідомлення Telegram по шортсах ----
    def tg_msgs(self, item_id: int) -> list[dict]:
        return self.q("SELECT * FROM tg_msgs WHERE item_id=?", (item_id,))

    def tg_msg(self, item_id: int, chat_id: int) -> dict | None:
        return self.one("SELECT * FROM tg_msgs WHERE item_id=? AND chat_id=?", (item_id, int(chat_id)))

    def set_tg_msg(self, item_id: int, chat_id: int, msg_id: int, text_msg_id: int | None, is_text: int) -> None:
        self.x("INSERT OR REPLACE INTO tg_msgs(item_id, chat_id, msg_id, text_msg_id, is_text) VALUES (?,?,?,?,?)",
               (item_id, int(chat_id), msg_id, text_msg_id, is_text))

    def clear_tg_msgs(self, item_id: int) -> None:
        self.x("DELETE FROM tg_msgs WHERE item_id=?", (item_id,))

    # ---- events / kv ----
    def event(self, item_id: int | None, kind: str, details=None) -> None:
        if details is not None and not isinstance(details, str):
            details = json.dumps(details, ensure_ascii=False)
        self.x("INSERT INTO events(ts, item_id, kind, details) VALUES (?,?,?,?)",
               (now_ts(), item_id, kind, details))

    def events(self, start: int, end: int) -> list[dict]:
        rows = self.q("SELECT * FROM events WHERE ts>=? AND ts<? ORDER BY ts", (start, end))
        for r in rows:
            try:
                r["details"] = json.loads(r["details"]) if r["details"] else None
            except ValueError:
                pass
        return rows

    def get(self, key: str, default=None):
        r = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return r["value"] if r else default

    def set(self, key: str, value) -> None:
        self.x("INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, None if value is None else str(value)))
