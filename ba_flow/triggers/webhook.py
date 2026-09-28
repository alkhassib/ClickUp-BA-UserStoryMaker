"""HTTP endpoint for ClickUp webhooks (FastAPI).

The request thread only verifies, parses and queues: ClickUp marks a webhook as failing when a delivery takes
longer than 7 seconds, so all real work happens in the EventWorker thread.
Invalid signatures get 403, never 401: a 401 makes ClickUp suspend the webhook immediately.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ba_flow.core.observability import get_logger, log
from ba_flow.triggers.worker import EventWorker, StatusChange

logger = get_logger("webhook")

STATUS_EVENT = "taskStatusUpdated"


def verify_signature(secret: str, raw_body: bytes, signature: str | None) -> bool:
    """ClickUp sends `X-Signature`: hex HMAC-SHA256 of the raw request body with the webhook secret."""
    if not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


def parse_status_change(payload: dict, received_at: float) -> StatusChange | None:
    """Only status changes matter; everything else is acknowledged and ignored."""
    if payload.get("event") != STATUS_EVENT or not payload.get("task_id"):
        return None
    items = [h for h in payload.get("history_items") or [] if h.get("field") in (None, "status")]
    if not items:
        return None
    item = items[-1]

    def status(side: object) -> str | None:
        return side.get("status") if isinstance(side, dict) else (side if isinstance(side, str) else None)

    user = item.get("user") or {}
    actor = user.get("id")
    return StatusChange(
        task_id=str(payload["task_id"]),
        before=status(item.get("before")),
        after=status(item.get("after")),
        actor_id=int(actor) if actor is not None else None,
        item_id=str(item.get("id") or item.get("date") or received_at),
        received_at=received_at,
    )


def create_http_app(worker: EventWorker, *, secret: str, path: str, bot_user_id: int) -> FastAPI:
    api = FastAPI(title="BA Flow webhook", docs_url=None, redoc_url=None)
    stats = {"received": 0, "queued": 0, "rejected": 0, "last_event_at": None}

    @api.post(path)
    async def receive(request: Request) -> JSONResponse:
        raw = await request.body()
        stats["received"] += 1
        if not verify_signature(secret, raw, request.headers.get("X-Signature")):
            stats["rejected"] += 1
            log(logger, 30, "rejected webhook with invalid signature", client=getattr(request.client, "host", "?"))
            return JSONResponse({"status": "invalid signature"}, status_code=403)
        try:
            payload = json.loads(raw)
        except ValueError:
            return JSONResponse({"status": "invalid json"}, status_code=400)

        change = parse_status_change(payload, received_at=time.time())
        if change is None:
            return JSONResponse({"status": "ignored"})
        if change.actor_id == bot_user_id:  # our own status changes: never re-process them
            return JSONResponse({"status": "ignored: bot"})
        worker.submit(change)
        stats["queued"] += 1
        stats["last_event_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        log(logger, 20, "webhook queued", task_id=change.task_id, before=change.before, after=change.after,
            actor=change.actor_id)
        return JSONResponse({"status": "queued"})

    @api.get("/health")
    async def health() -> JSONResponse:
        ok = worker.alive
        return JSONResponse({"status": "ok" if ok else "worker stopped", "queue": worker.qsize(),
                             "processed": worker.processed, "last_error": worker.last_error, **stats},
                            status_code=200 if ok else 503)

    return api
