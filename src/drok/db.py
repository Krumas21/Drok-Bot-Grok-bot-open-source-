"""SQLite store. One connection, one lock: every query is tiny, so this never becomes the bottleneck."""

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    fallbacks TEXT NOT NULL DEFAULT '[]',
    tools TEXT NOT NULL DEFAULT '["*"]',
    screen INTEGER NOT NULL DEFAULT 1,
    is_orchestrator INTEGER NOT NULL DEFAULT 0,
    color TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'idle',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,              -- dm | group
    title TEXT NOT NULL DEFAULT '',
    members TEXT NOT NULL,           -- json list of agent ids
    summary TEXT NOT NULL DEFAULT '',
    summarized_upto INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL,
    author TEXT NOT NULL,            -- 'user' | agent id | 'system'
    kind TEXT NOT NULL DEFAULT 'text', -- text | handoff | approval | status | request | tool
    content TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conv ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY,
    agent_id TEXT NOT NULL,
    conversation_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued', -- queued | running | waiting | done | failed | cancelled
    source TEXT NOT NULL DEFAULT 'user',   -- user | telegram | handoff | routine | system
    trigger TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    llm_messages TEXT NOT NULL DEFAULT '[]',
    steps INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS turns_agent ON turns(agent_id, status, id);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY,
    turn_id INTEGER NOT NULL,
    agent_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending', -- pending | approved | denied
    created_at REAL NOT NULL,
    decided_at REAL
);
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    tool TEXT NOT NULL,       -- fnmatch pattern on tool name
    subject TEXT NOT NULL,    -- fnmatch pattern on the call's subject (command, url, ...)
    action TEXT NOT NULL,     -- allow | ask | deny
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    agent_id TEXT,            -- NULL = shared with every agent
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(content, content='memories', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TABLE IF NOT EXISTS routines (
    id INTEGER PRIMARY KEY,
    agent_id TEXT NOT NULL,
    name TEXT NOT NULL,
    instructions TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'schedule', -- schedule | webhook
    cron TEXT NOT NULL DEFAULT '',
    tz TEXT NOT NULL DEFAULT 'UTC',
    secret TEXT NOT NULL DEFAULT '',       -- webhook shared secret
    match TEXT NOT NULL DEFAULT '',        -- webhook: substring the payload must contain
    enabled INTEGER NOT NULL DEFAULT 1,
    paused_reason TEXT NOT NULL DEFAULT '',
    next_run_at REAL,
    last_run_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS routine_runs (
    id INTEGER PRIMARY KEY,
    routine_id INTEGER NOT NULL,
    turn_id INTEGER,
    status TEXT NOT NULL DEFAULT 'running',
    result TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS secrets (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL,      -- Fernet-encrypted
    description TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mcp_servers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    target TEXT NOT NULL,     -- URL (streamable HTTP) or command line (stdio)
    env TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    agent_id TEXT NOT NULL,
    turn_id INTEGER,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY,
    agent_id TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

JSON_COLUMNS = {"fallbacks", "tools", "members", "meta", "llm_messages", "args", "env"}


def now() -> float:
    return time.time()


class DB:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        d = dict(row)
        for k in JSON_COLUMNS & d.keys():
            if isinstance(d[k], str):
                d[k] = json.loads(d[k])
        return d

    @staticmethod
    def _arg(v):
        return json.dumps(v) if isinstance(v, (dict, list)) else v

    def all(self, sql: str, *args) -> list[dict]:
        with self.lock:
            return [self._row(r) for r in self.conn.execute(sql, [self._arg(a) for a in args])]

    def one(self, sql: str, *args) -> dict | None:
        rows = self.all(sql, *args)
        return rows[0] if rows else None

    def run(self, sql: str, *args) -> int:
        """Execute a write; returns lastrowid."""
        with self.lock:
            return self.conn.execute(sql, [self._arg(a) for a in args]).lastrowid

    def insert(self, table: str, **values) -> int:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        return self.run(f"INSERT INTO {table} ({cols}) VALUES ({marks})", *values.values())

    def update(self, table: str, key, key_col: str = "id", **values) -> None:
        sets = ", ".join(f"{k} = ?" for k in values)
        self.run(f"UPDATE {table} SET {sets} WHERE {key_col} = ?", *values.values(), key)

    def get_kv(self, key: str, default=None):
        row = self.one("SELECT value FROM kv WHERE key = ?", key)
        return json.loads(row["value"]) if row else default

    def set_kv(self, key: str, value) -> None:
        self.run(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            key,
            json.dumps(value),
        )
