"""Every ClickUp write goes through here: dry-run, logging, run tags, self-write log."""
from __future__ import annotations

import uuid
from typing import Any

from ba_flow.clickup.client import ClickUpClient
from ba_flow.core.observability import current_run_id, get_logger, log, short_run_id
from ba_flow.store.state import StateStore

logger = get_logger("writer")


class ClickUpWriter:
    def __init__(self, client: ClickUpClient, store: StateStore, dry_run: bool = False):
        self._client = client
        self._store = store
        self.dry_run = dry_run

    def _log(self, action: str, **data: Any) -> None:
        log(logger, 20, f"{'[dry-run] ' if self.dry_run else ''}{action}", action=action,
            dry_run=self.dry_run, **data)

    def create_task(self, list_id: str, name: str, *, parent: str | None = None, status: str | None = None,
                    markdown: str | None = None, custom_fields: list[dict] | None = None,
                    assignees: list[int] | None = None) -> str:
        body: dict[str, Any] = {"name": name}
        if parent:
            body["parent"] = parent
        if status:
            body["status"] = status
        if markdown is not None:
            body["markdown_content"] = markdown
        if custom_fields:
            body["custom_fields"] = custom_fields
        if assignees:
            body["assignees"] = assignees
        self._log("create_task", list_id=list_id, name=name, parent=parent, status=status)
        if self.dry_run:
            return f"dry-{uuid.uuid4().hex[:8]}"
        task_id = self._client.create_task(list_id, body)["id"]
        self._store.record_self_write(task_id, "create", status, current_run_id())
        return task_id

    def set_status(self, task_id: str, status: str) -> None:
        self._log("set_status", task_id=task_id, status=status)
        if self.dry_run:
            return
        # Recorded first: the change event can arrive before the API call returns.
        self._store.record_self_write(task_id, "status", status, current_run_id())
        self._client.update_task(task_id, {"status": status})

    def set_field(self, task_id: str, field_id: str, value: Any) -> None:
        self._log("set_field", task_id=task_id, field_id=field_id)
        if self.dry_run:
            return
        self._client.set_custom_field(task_id, field_id, value)

    def comment(self, task_id: str, text: str, notify_all: bool = False) -> None:
        text = f"{text}\n\n— BA Flow · run: {short_run_id()}"
        self._log("comment", task_id=task_id, chars=len(text))
        if self.dry_run:
            return
        self._client.add_comment(task_id, text, notify_all=notify_all)

    def add_dependency(self, task_id: str, depends_on: str) -> None:
        self._log("add_dependency", task_id=task_id, depends_on=depends_on)
        if self.dry_run:
            return
        self._client.add_dependency(task_id, depends_on)
