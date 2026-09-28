"""Polling trigger: turns task statuses into events for the dispatcher.

Only statuses the bot never sets itself are polled (`to analyze`, `submitted`),
so the bot cannot re-trigger itself.
"""
from __future__ import annotations

import time

from ba_flow.app import App
from ba_flow.core.observability import get_logger, log
from ba_flow.flow.dispatcher import DT_SUBMITTED, INTAKE_TO_ANALYZE, Dispatcher, Event

logger = get_logger("poller")


class Poller:
    def __init__(self, app: App, dispatcher: Dispatcher):
        self.app = app
        self.dispatcher = dispatcher

    def poll_once(self, resume: bool = False) -> int:
        """Returns the number of events dispatched. `resume` also picks tasks left in `analyzing` by a crash."""
        ws = self.app.resolver.load()
        intake = self.app.settings.clickup.statuses.intake
        statuses = [intake.to_analyze] + ([intake.analyzing] if resume else [])
        count = 0
        for t in self.app.client.get_list_tasks(ws.list_id("intake"), statuses=statuses):
            event = Event(INTAKE_TO_ANALYZE, t["id"], "poll", t["status"]["status"], t.get("date_updated"))
            if self.dispatcher.dispatch(event, force=resume) is not None:
                count += 1

        # Top-level tasks only: gap subtasks never use the DT's `submitted` status.
        submitted = self.app.settings.clickup.statuses.dt.submitted
        for t in self.app.client.get_list_tasks(ws.list_id("drafts"), statuses=[submitted]):
            event = Event(DT_SUBMITTED, t["id"], "poll", t["status"]["status"], t.get("date_updated"))
            if self.dispatcher.dispatch(event) is not None:
                count += 1
        return count

    def run_forever(self, interval: int) -> None:
        log(logger, 20, "poller started", interval=interval, dry_run=self.app.writer.dry_run)
        first = True
        while True:
            try:
                n = self.poll_once(resume=first)
                if n:
                    log(logger, 20, "poll cycle", dispatched=n)
                first = False
            except Exception:
                logger.exception("poll cycle failed")
            time.sleep(interval)
