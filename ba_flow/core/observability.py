"""Logging with a run_id carried through every layer via contextvars."""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_id", default=None)


def new_run_id() -> str:
    return uuid.uuid4().hex


def current_run_id() -> str | None:
    return _run_id.get()


def short_run_id(run_id: str | None = None) -> str:
    rid = run_id or current_run_id()
    return rid[:8] if rid else "-"


@contextmanager
def run_context(run_id: str) -> Iterator[str]:
    token = _run_id.set(run_id)
    try:
        yield run_id
    finally:
        _run_id.reset(token)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if rid := current_run_id():
            payload["run_id"] = rid
        payload.update(getattr(record, "data", None) or {})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        data = getattr(record, "data", None) or {}
        extras = " ".join(f"{k}={v}" for k, v in data.items())
        line = f"{ts} {record.levelname:<7} [{short_run_id()}] {record.getMessage()}"
        if extras:
            line += f"  {extras}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup_logging(level: str = "INFO", file: Path | None = None) -> None:
    root = logging.getLogger("ba_flow")
    root.handlers.clear()
    root.setLevel(level.upper())
    root.propagate = False

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(_ConsoleFormatter())
    root.addHandler(console)

    if file:
        file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(file, encoding="utf-8")
        fh.setFormatter(_JsonFormatter())
        root.addHandler(fh)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ba_flow.{name}")


def log(logger: logging.Logger, level: int, msg: str, **data: object) -> None:
    """Log with structured fields (end up as JSON keys in the file log)."""
    logger.log(level, msg, extra={"data": data})
