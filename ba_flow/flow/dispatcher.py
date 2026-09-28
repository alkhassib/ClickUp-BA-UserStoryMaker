"""Event -> run (run_id, dedupe, runs table) -> handler."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ba_flow.app import App
from ba_flow.clickup.client import ClickUpError
from ba_flow.core.observability import get_logger, log, new_run_id, run_context

logger = get_logger("dispatcher")

INTAKE_TO_ANALYZE = "intake_to_analyze"
DT_SUBMITTED = "dt_submitted"
DT_REVERTED = "dt_reverted"      # a BA made a status change that is not allowed; the bot undid it


@dataclass(frozen=True)
class Event:
    type: str
    task_id: str
    trigger: str                 # poll | webhook | manual
    status: str | None = None
    date_updated: str | None = None

    @property
    def key(self) -> str:
        return f"{self.type}:{self.task_id}:{self.status}:{self.date_updated}"


Handler = Callable[[Event], dict]


class Dispatcher:
    def __init__(self, app: App, handlers: dict[str, Handler]):
        self.app = app
        self.handlers = handlers

    def dispatch(self, event: Event, force: bool = False) -> dict | None:
        if not force and self.app.store.is_event_processed(event.key):
            return None
        handler = self.handlers.get(event.type)
        if handler is None:
            log(logger, 30, "no handler for event", event_type=event.type)
            return None
        return self.run(event, lambda: handler(event))

    def run(self, event: Event, fn: Callable[[], dict]) -> dict:
        """Run `fn` as a recorded run (run_id, runs table, event dedupe), whatever produced the event."""
        store, dry_run = self.app.store, self.app.writer.dry_run
        run_id = new_run_id()
        with run_context(run_id):
            store.start_run(run_id, trigger=event.trigger, event_type=event.type, task_id=event.task_id,
                            dry_run=int(dry_run))
            log(logger, 20, "run started", event_type=event.type, task_id=event.task_id, trigger=event.trigger)
            try:
                result = fn()
                store.finish_run(run_id, result.get("final_result", "ok"), error=result.get("error"))
            except Exception as e:
                if isinstance(e, ClickUpError) and e.status in (400, 404):
                    self.app.resolver.invalidate()  # a cached name/ID may be stale
                logger.exception("run failed", extra={"data": {"task_id": event.task_id}})
                store.finish_run(run_id, "failed", error=f"{type(e).__name__}: {e}")
                result = {"final_result": "failed", "error": str(e)}
            log(logger, 20, "run finished", **{k: v for k, v in result.items() if k != "error"})
            if not dry_run:
                store.mark_event_processed(event.key, run_id)
        return {"run_id": run_id, **result}
