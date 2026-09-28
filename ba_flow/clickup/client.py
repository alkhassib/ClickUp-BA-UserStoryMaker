"""Thin ClickUp API v2 client with retry, backoff and rate-limit handling."""
from __future__ import annotations

import json as _json
import time
from typing import Any, Callable

import httpx

from ba_flow.core.observability import get_logger, log

BASE_URL = "https://api.clickup.com/api/v2"
RETRY_STATUSES = {429, 500, 502, 503, 504}

logger = get_logger("clickup")


class ClickUpError(Exception):
    def __init__(self, status: int, method: str, path: str, body: str):
        self.status = status
        self.method = method
        self.path = path
        self.body = body
        super().__init__(f"ClickUp {method} {path} -> HTTP {status}: {body[:300]}")


class ClickUpClient:
    def __init__(
        self,
        token: str,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 4,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"Authorization": token, "Content-Type": "application/json"},
            transport=transport,
        )
        self._max_retries = max_retries
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    def request(self, method: str, path: str, *, params: Any = None, json: Any = None) -> dict:
        attempt = 0
        while True:
            try:
                resp = self._http.request(method, path, params=params, json=json)
            except httpx.TransportError as e:
                if attempt >= self._max_retries:
                    raise
                delay = self._backoff(attempt)
                log(logger, 30, "clickup transport error, retrying", error=str(e), delay=delay)
            else:
                if resp.status_code < 400:
                    return resp.json() if resp.content else {}
                if resp.status_code not in RETRY_STATUSES or attempt >= self._max_retries:
                    raise ClickUpError(resp.status_code, method, path, resp.text)
                delay = self._retry_delay(resp, attempt)
                log(logger, 30, "clickup retryable response", status=resp.status_code, path=path, delay=delay)
            self._sleep(delay)
            attempt += 1

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(2.0 ** attempt, 30.0)

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float:
        if resp.status_code == 429 and (reset := resp.headers.get("X-RateLimit-Reset")):
            try:
                return max(0.5, min(float(reset) - time.time() + 0.5, 60.0))
            except ValueError:
                pass
        return self._backoff(attempt)

    # --- read -------------------------------------------------------------
    def get_user(self) -> dict:
        return self.request("GET", "/user")["user"]

    def get_teams(self) -> list[dict]:
        return self.request("GET", "/team")["teams"]

    def get_spaces(self, team_id: str) -> list[dict]:
        return self.request("GET", f"/team/{team_id}/space", params={"archived": "false"})["spaces"]

    def get_folders(self, space_id: str) -> list[dict]:
        return self.request("GET", f"/space/{space_id}/folder", params={"archived": "false"})["folders"]

    def get_folderless_lists(self, space_id: str) -> list[dict]:
        return self.request("GET", f"/space/{space_id}/list", params={"archived": "false"})["lists"]

    def get_list(self, list_id: str) -> dict:
        return self.request("GET", f"/list/{list_id}")

    def get_list_fields(self, list_id: str) -> list[dict]:
        return self.request("GET", f"/list/{list_id}/field")["fields"]

    def get_task(self, task_id: str, include_subtasks: bool = False) -> dict:
        params = {"include_subtasks": "true"} if include_subtasks else None
        return self.request("GET", f"/task/{task_id}", params=params)

    def get_list_tasks(self, list_id: str, *, statuses: list[str] | None = None, subtasks: bool = False,
                       include_closed: bool = False, custom_fields: list[dict] | None = None) -> list[dict]:
        """All matching tasks of a list (follows pagination)."""
        params: list[tuple[str, str]] = [
            ("subtasks", str(subtasks).lower()), ("include_closed", str(include_closed).lower()),
        ]
        params += [("statuses[]", s) for s in statuses or []]
        if custom_fields:
            params.append(("custom_fields", _json.dumps(custom_fields)))
        tasks, page = [], 0
        while True:
            data = self.request("GET", f"/list/{list_id}/task", params=[*params, ("page", str(page))])
            tasks.extend(data.get("tasks", []))
            if data.get("last_page", True) or not data.get("tasks"):
                return tasks
            page += 1

    def find_tasks_by_field(self, list_id: str, field_id: str, value: str) -> list[dict]:
        """Tasks whose custom field equals `value` exactly.

        ClickUp's "=" filter on text fields is a substring match ("DT:1" also matches "DT:1:G10"),
        so results are re-checked here.
        """
        candidates = self.get_list_tasks(
            list_id, subtasks=True, include_closed=True,
            custom_fields=[{"field_id": field_id, "operator": "=", "value": value}],
        )
        return [
            t for t in candidates
            if any(f.get("id") == field_id and f.get("value") == value for f in t.get("custom_fields") or [])
        ]

    def download(self, url: str) -> bytes:
        resp = self._http.get(url, follow_redirects=True)
        if resp.status_code >= 400:
            raise ClickUpError(resp.status_code, "GET", url, resp.text[:200])
        return resp.content

    # --- webhooks ----------------------------------------------------------
    def create_webhook(self, team_id: str, endpoint: str, events: list[str], space_id: str) -> dict:
        return self.request("POST", f"/team/{team_id}/webhook",
                            json={"endpoint": endpoint, "events": events, "space_id": int(space_id)})

    def list_webhooks(self, team_id: str) -> list[dict]:
        return self.request("GET", f"/team/{team_id}/webhook").get("webhooks", [])

    def update_webhook(self, webhook_id: str, body: dict) -> dict:
        return self.request("PUT", f"/webhook/{webhook_id}", json=body)

    def delete_webhook(self, webhook_id: str) -> dict:
        return self.request("DELETE", f"/webhook/{webhook_id}")

    # --- write (use ClickUpWriter, not these directly) --------------------
    def create_task(self, list_id: str, body: dict) -> dict:
        return self.request("POST", f"/list/{list_id}/task", json=body)

    def update_task(self, task_id: str, body: dict) -> dict:
        return self.request("PUT", f"/task/{task_id}", json=body)

    def set_custom_field(self, task_id: str, field_id: str, value: Any) -> dict:
        return self.request("POST", f"/task/{task_id}/field/{field_id}", json={"value": value})

    def add_dependency(self, task_id: str, depends_on: str) -> dict:
        return self.request("POST", f"/task/{task_id}/dependency", json={"depends_on": depends_on})

    def add_comment(self, task_id: str, text: str, notify_all: bool = False) -> dict:
        return self.request(
            "POST", f"/task/{task_id}/comment", json={"comment_text": text, "notify_all": notify_all}
        )
