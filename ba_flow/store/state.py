"""SQLite state: metadata cache, runs, processed events, self-writes, resumable steps."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv_cache (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    config_hash  TEXT NOT NULL,
    expires_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id             TEXT PRIMARY KEY,
    trigger            TEXT,
    event_type         TEXT,
    task_id            TEXT,
    dt_id              TEXT,
    doc_sha            TEXT,
    pack_id            TEXT,
    prompt_version     TEXT,
    model              TEXT,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_hit          INTEGER NOT NULL DEFAULT 0,
    validation_result  TEXT,
    final_result       TEXT,
    error              TEXT,
    dry_run            INTEGER NOT NULL DEFAULT 0,
    started_at         TEXT NOT NULL,
    finished_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
CREATE INDEX IF NOT EXISTS idx_runs_dt ON runs(dt_id);
CREATE TABLE IF NOT EXISTS processed_events (
    event_key     TEXT PRIMARY KEY,
    run_id        TEXT,
    processed_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS self_writes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    value       TEXT,
    run_id      TEXT,
    written_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_self_writes_task ON self_writes(task_id, kind);
CREATE TABLE IF NOT EXISTS steps (
    run_key     TEXT NOT NULL,
    step        TEXT NOT NULL,
    status      TEXT NOT NULL,
    clickup_id  TEXT,
    run_id      TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (run_key, step)
);
CREATE TABLE IF NOT EXISTS llm_cache (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             TEXT,
    step               TEXT NOT NULL,
    provider           TEXT NOT NULL,
    model              TEXT NOT NULL,
    prompt_version     TEXT NOT NULL,
    input_tokens       INTEGER NOT NULL,
    output_tokens      INTEGER NOT NULL,
    cache_read_tokens  INTEGER NOT NULL,
    payload            TEXT,
    created_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS packs (
    pack_id         TEXT PRIMARY KEY,
    run_key         TEXT NOT NULL UNIQUE,
    intake_task_id  TEXT NOT NULL,
    dt_id           TEXT,
    doc_sha         TEXT NOT NULL,
    schema_version  INTEGER NOT NULL,
    prompt_version  TEXT NOT NULL,
    analyzer_model  TEXT NOT NULL,
    lang            TEXT NOT NULL,
    body            TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_packs_dt ON packs(dt_id);
CREATE TABLE IF NOT EXISTS poll_cursors (
    name   TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""

RUN_COLUMNS = {
    "trigger", "event_type", "task_id", "dt_id", "doc_sha", "pack_id", "prompt_version", "model",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_hit", "validation_result",
    "final_result", "error", "dry_run", "finished_at",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, db_path: Path | str):
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._conn:
            yield self._conn

    # --- metadata cache -------------------------------------------------
    def cache_get(self, key: str, config_hash: str) -> Any | None:
        row = self._conn.execute(
            "SELECT value, config_hash, expires_at FROM kv_cache WHERE key = ?", (key,)
        ).fetchone()
        if not row or row["config_hash"] != config_hash or row["expires_at"] < time.time():
            return None
        return json.loads(row["value"])

    def cache_set(self, key: str, value: Any, config_hash: str, ttl_seconds: int) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO kv_cache(key, value, config_hash, expires_at) VALUES (?,?,?,?)",
                (key, json.dumps(value, ensure_ascii=False), config_hash, time.time() + ttl_seconds),
            )

    def cache_delete(self, key: str | None = None) -> None:
        with self._tx() as c:
            if key is None:
                c.execute("DELETE FROM kv_cache")
            else:
                c.execute("DELETE FROM kv_cache WHERE key = ?", (key,))

    # --- runs -----------------------------------------------------------
    def start_run(self, run_id: str, **fields: Any) -> None:
        self._check_run_fields(fields)
        cols = ["run_id", "started_at", *fields]
        with self._tx() as c:
            c.execute(
                f"INSERT INTO runs({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                (run_id, _now_iso(), *fields.values()),
            )

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        self._check_run_fields(fields)
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._tx() as c:
            c.execute(f"UPDATE runs SET {sets} WHERE run_id = ?", (*fields.values(), run_id))

    def add_run_usage(self, run_id: str, input_tokens: int = 0, output_tokens: int = 0,
                      cache_read_tokens: int = 0) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE runs SET input_tokens = input_tokens + ?, output_tokens = output_tokens + ?, "
                "cache_read_tokens = cache_read_tokens + ? WHERE run_id = ?",
                (input_tokens, output_tokens, cache_read_tokens, run_id),
            )

    def finish_run(self, run_id: str, final_result: str, error: str | None = None, **fields: Any) -> None:
        self.update_run(run_id, final_result=final_result, error=error, finished_at=_now_iso(), **fields)

    def get_run(self, run_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ? OR run_id LIKE ? ORDER BY started_at DESC LIMIT 1",
            (run_id, f"{run_id}%"),
        ).fetchone()
        return dict(row) if row else None

    def list_runs(self, task_id: str | None = None, limit: int = 20) -> list[dict]:
        if task_id:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE task_id = ? OR dt_id = ? ORDER BY started_at DESC LIMIT ?",
                (task_id, task_id, limit),
            )
        else:
            rows = self._conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    @staticmethod
    def _check_run_fields(fields: dict) -> None:
        unknown = set(fields) - RUN_COLUMNS
        if unknown:
            raise ValueError(f"unknown run fields: {sorted(unknown)}")

    # --- event dedupe ---------------------------------------------------
    def mark_event_processed(self, event_key: str, run_id: str | None) -> bool:
        """Returns False if the event was already processed."""
        with self._tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO processed_events(event_key, run_id, processed_at) VALUES (?,?,?)",
                (event_key, run_id, _now_iso()),
            )
            return cur.rowcount == 1

    def is_event_processed(self, event_key: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM processed_events WHERE event_key = ?", (event_key,)
        ).fetchone() is not None

    # --- self-write log (loop protection) -------------------------------
    def record_self_write(self, task_id: str, kind: str, value: str | None, run_id: str | None) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO self_writes(task_id, kind, value, run_id, written_at) VALUES (?,?,?,?,?)",
                (task_id, kind, value, run_id, time.time()),
            )

    def last_self_write(self, task_id: str, kind: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM self_writes WHERE task_id = ? AND kind = ? ORDER BY written_at DESC LIMIT 1",
            (task_id, kind),
        ).fetchone()
        return dict(row) if row else None

    # --- resumable steps ------------------------------------------------
    def get_steps(self, run_key: str) -> dict[str, dict]:
        rows = self._conn.execute("SELECT * FROM steps WHERE run_key = ?", (run_key,))
        return {r["step"]: dict(r) for r in rows}

    def set_step(self, run_key: str, step: str, status: str, clickup_id: str | None = None,
                 run_id: str | None = None) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO steps(run_key, step, status, clickup_id, run_id, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (run_key, step, status, clickup_id, run_id, _now_iso()),
            )

    # --- LLM result cache & call log -------------------------------------
    def llm_cache_get(self, key: str) -> Any | None:
        row = self._conn.execute("SELECT value FROM llm_cache WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def llm_cache_set(self, key: str, value: Any) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO llm_cache(key, value, created_at) VALUES (?,?,?)",
                      (key, json.dumps(value, ensure_ascii=False), _now_iso()))

    def record_llm_call(self, *, run_id: str | None, step: str, provider: str, model: str, prompt_version: str,
                        input_tokens: int, output_tokens: int, cache_read_tokens: int,
                        payload: str | None = None) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO llm_calls(run_id, step, provider, model, prompt_version, input_tokens, output_tokens, "
                "cache_read_tokens, payload, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, step, provider, model, prompt_version, input_tokens, output_tokens, cache_read_tokens,
                 payload, _now_iso()),
            )

    # --- context packs --------------------------------------------------
    def save_pack(self, *, pack_id: str, run_key: str, intake_task_id: str, doc_sha: str, schema_version: int,
                  prompt_version: str, analyzer_model: str, lang: str, body: str, created_at: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO packs(pack_id, run_key, intake_task_id, dt_id, doc_sha, schema_version, "
                "prompt_version, analyzer_model, lang, body, created_at) VALUES (?,?,?,NULL,?,?,?,?,?,?,?)",
                (pack_id, run_key, intake_task_id, doc_sha, schema_version, prompt_version, analyzer_model, lang,
                 body, created_at),
            )

    def set_pack_dt(self, pack_id: str, dt_id: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE packs SET dt_id = ? WHERE pack_id = ?", (dt_id, pack_id))

    def get_pack(self, *, run_key: str | None = None, dt_id: str | None = None) -> dict | None:
        if run_key:
            row = self._conn.execute("SELECT * FROM packs WHERE run_key = ?", (run_key,)).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM packs WHERE dt_id = ? ORDER BY created_at DESC LIMIT 1", (dt_id,)
            ).fetchone()
        return dict(row) if row else None

    # --- polling cursors ------------------------------------------------
    def get_cursor(self, name: str) -> str | None:
        row = self._conn.execute("SELECT value FROM poll_cursors WHERE name = ?", (name,)).fetchone()
        return row["value"] if row else None

    def set_cursor(self, name: str, value: str) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO poll_cursors(name, value) VALUES (?,?)", (name, value))
