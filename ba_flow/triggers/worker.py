"""Single-threaded event worker for `ba-flow serve`.

Webhook deliveries, the startup catch-up poll and the hybrid safety poll all go through one queue and one
thread, so two runs never touch the same Draft Ticket (or the SQLite connection) at the same time.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

from ba_flow.app import App
from ba_flow.clickup.client import ClickUpError
from ba_flow.core.observability import get_logger, log
from ba_flow.flow.dispatcher import DT_REVERTED, DT_SUBMITTED, INTAKE_TO_ANALYZE, Dispatcher, Event
from ba_flow.flow.state_machine import StatusMap, decide_on_human_change
from ba_flow.triggers.poller import Poller

logger = get_logger("worker")


@dataclass(frozen=True)
class StatusChange:
    """A `taskStatusUpdated` webhook delivery, reduced to what the worker needs."""
    task_id: str
    before: str | None
    after: str | None
    actor_id: int | None
    item_id: str          # ClickUp history item id: unique per change, used for dedupe
    received_at: float


@dataclass(frozen=True)
class PollTick:
    resume: bool = False


class EventWorker:
    def __init__(self, app: App, dispatcher: Dispatcher, *, debounce_seconds: float = 0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time):
        self.app = app
        self.dispatcher = dispatcher
        self.poller = Poller(app, dispatcher)
        self.debounce = debounce_seconds
        self._sleep = sleep
        self._clock = clock
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.dt_status = StatusMap(app.settings.clickup.statuses.dt)
        self.processed = 0
        self.last_error: str | None = None

    # --- producers (any thread) ---------------------------------------------------------------
    def submit(self, item: StatusChange | PollTick) -> None:
        self._queue.put(item)

    def qsize(self) -> int:
        return self._queue.qsize()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # --- consumer -------------------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="ba-flow-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30) -> None:
        self._stop.set()
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            item = self._queue.get()
            if item is None:
                break
            self.process(item)

    def drain(self) -> None:
        """Process everything queued, in this thread (tests / one-shot use)."""
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if item is not None:
                self.process(item)

    def process(self, item: StatusChange | PollTick) -> str:
        try:
            outcome = self.poll(item) if isinstance(item, PollTick) else self.handle_change(item)
            self.last_error = None
        except Exception as e:  # the worker must survive anything
            logger.exception("worker item failed")
            self.last_error = f"{type(e).__name__}: {e}"
            outcome = "error"
        self.processed += 1
        return outcome

    def poll(self, tick: PollTick) -> str:
        n = self.poller.poll_once(resume=tick.resume)
        if n:
            log(logger, 20, "poll dispatched events", count=n, resume=tick.resume)
        return f"polled:{n}"

    def handle_change(self, c: StatusChange) -> str:
        app, cfg = self.app, self.app.settings.clickup
        ws = app.resolver.load()
        if c.actor_id is not None and c.actor_id == ws.bot_user_id:
            return "ignored:bot"

        wait = c.received_at + self.debounce - self._clock()
        if wait > 0:
            self._sleep(wait)
        try:
            task = app.client.get_task(c.task_id)
        except ClickUpError as e:
            if e.status == 404:
                return "ignored:gone"
            raise
        current = task["status"]["status"].lower()
        if current != (c.after or "").lower():
            log(logger, 20, "status changed again before processing; skipped", task_id=c.task_id,
                expected=c.after, current=current)
            return "superseded"

        list_id = str((task.get("list") or {}).get("id", ""))
        if list_id == ws.list_id("intake"):
            if current == cfg.statuses.intake.to_analyze.lower():
                self.dispatcher.dispatch(Event(INTAKE_TO_ANALYZE, c.task_id, "webhook", current, c.item_id))
                return "dispatched:intake"
            return "ignored:intake-status"

        if list_id == ws.list_id("drafts") and not task.get("parent"):
            frm, to = self.dt_status.state_for(c.before), self.dt_status.state_for(c.after)
            decision = decide_on_human_change(frm, to)
            if decision.action == "process":
                self.dispatcher.dispatch(Event(DT_SUBMITTED, c.task_id, "webhook", current, c.item_id))
                return "dispatched:dt"
            if decision.action == "revert" and decision.revert_to is not None:
                self._revert(task, c, decision.reason, self.dt_status.status_for(decision.revert_to))
                return "reverted"
            return f"ignored:{decision.reason}"
        return "ignored:other-list"

    def _revert(self, task: dict, c: StatusChange, reason: str, back_to: str) -> None:
        """Undo a status change a BA is not allowed to make (only detectable with webhooks: they carry the actor)."""
        def undo() -> dict:
            self.app.writer.set_status(task["id"], back_to)
            self.app.writer.comment(
                task["id"],
                f"↩️ Moving this ticket from '{c.before}' to '{c.after}' is not allowed ({reason}). "
                f"It was moved back to '{back_to}'. To finish a Draft Ticket, answer every gap and move it to "
                f"'{self.app.settings.clickup.statuses.dt.submitted}'.",
                notify_all=True,
            )
            return {"final_result": "reverted"}

        self.dispatcher.run(Event(DT_REVERTED, task["id"], "webhook", c.after, c.item_id), undo)
