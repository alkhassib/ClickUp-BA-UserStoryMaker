"""Resumable multi-step operations.

Each completed step is recorded in SQLite with the ClickUp id it produced, so a rerun continues from
the last successful step. Creation steps also look the task up by its `Source Key` custom field first,
which covers a crash between "created in ClickUp" and "recorded in SQLite".
"""
from __future__ import annotations

from typing import Callable

from ba_flow.clickup.client import ClickUpClient
from ba_flow.core.observability import current_run_id, get_logger, log
from ba_flow.store.state import StateStore

logger = get_logger("steps")


class Steps:
    def __init__(self, store: StateStore, run_key: str, dry_run: bool = False):
        self._store = store
        self.run_key = run_key
        self._dry_run = dry_run
        self._done = {k: v for k, v in store.get_steps(run_key).items() if v["status"] == "done"}

    def done(self, step: str) -> dict | None:
        return self._done.get(step)

    def mark(self, step: str, clickup_id: str | None = None) -> None:
        row = {"step": step, "status": "done", "clickup_id": clickup_id}
        self._done[step] = row
        if not self._dry_run:  # a dry run must never make a later real run skip work
            self._store.set_step(self.run_key, step, "done", clickup_id=clickup_id, run_id=current_run_id())

    def run(self, step: str, fn: Callable[[], str | None]) -> str | None:
        if (d := self.done(step)) is not None:
            log(logger, 10, "step already done", step=step, run_key=self.run_key)
            return d["clickup_id"]
        result = fn()
        self.mark(step, result)
        return result

    def find_or_create(self, step: str, client: ClickUpClient, *, list_id: str, source_field_id: str,
                       source_key: str, create: Callable[[], str]) -> str:
        def _do() -> str:
            existing = client.find_tasks_by_field(list_id, source_field_id, source_key)
            if existing:
                log(logger, 20, "reusing existing task", step=step, source_key=source_key, task_id=existing[0]["id"])
                return existing[0]["id"]
            return create()

        result = self.run(step, _do)
        assert result is not None
        return result
