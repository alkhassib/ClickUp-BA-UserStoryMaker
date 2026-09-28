"""Official Draft Ticket (DT) lifecycle.

Internal states are fixed in code; their ClickUp status names come from settings.yaml.
RETURNED is internal only: the reason is recorded and the DT goes straight back to BA_REVIEW.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from ba_flow.core.config import DtStatuses


class DTState(StrEnum):
    DRAFT = "DRAFT"
    BA_REVIEW = "BA_REVIEW"
    SUBMITTED = "SUBMITTED"
    RETURNED = "RETURNED"
    COMPILED = "COMPILED"
    FAILED = "FAILED"


class Actor(StrEnum):
    BOT = "bot"
    HUMAN = "human"


S = DTState

# (from, to) -> who may perform it
ALLOWED: dict[tuple[DTState, DTState], Actor] = {
    (S.DRAFT, S.BA_REVIEW): Actor.BOT,          # all gaps created
    (S.BA_REVIEW, S.SUBMITTED): Actor.HUMAN,    # BA submits answers
    (S.SUBMITTED, S.BA_REVIEW): Actor.HUMAN,    # BA withdraws before processing
    (S.SUBMITTED, S.RETURNED): Actor.BOT,       # validation failed
    (S.RETURNED, S.BA_REVIEW): Actor.BOT,       # back to the BA with a comment
    (S.SUBMITTED, S.COMPILED): Actor.BOT,       # user story created
    (S.FAILED, S.BA_REVIEW): Actor.HUMAN,       # manual recovery
    (S.FAILED, S.SUBMITTED): Actor.HUMAN,       # manual retry
}


class IllegalTransition(Exception):
    pass


@dataclass(frozen=True)
class Decision:
    action: Literal["process", "ignore", "revert"]
    reason: str
    revert_to: DTState | None = None


class StatusMap:
    """Maps between internal states and configured ClickUp status names."""

    def __init__(self, statuses: DtStatuses):
        self._to_status = {
            S.DRAFT: statuses.draft,
            S.BA_REVIEW: statuses.ba_review,
            S.SUBMITTED: statuses.submitted,
            S.RETURNED: statuses.ba_review,  # internal-only state, surfaces as BA review
            S.COMPILED: statuses.compiled,
            S.FAILED: statuses.failed,
        }
        self._to_state = {v.strip().lower(): k for k, v in self._to_status.items() if k != S.RETURNED}

    def status_for(self, state: DTState) -> str:
        return self._to_status[state]

    def state_for(self, status: str | None) -> DTState | None:
        return self._to_state.get((status or "").strip().lower())


def is_allowed(frm: DTState, to: DTState, actor: Actor) -> bool:
    if actor is Actor.BOT and to is S.FAILED and frm is not S.FAILED:
        return True
    return ALLOWED.get((frm, to)) is actor


def assert_bot_transition(frm: DTState, to: DTState) -> None:
    """Guard for transitions the bot is about to perform."""
    if not is_allowed(frm, to, Actor.BOT):
        raise IllegalTransition(f"bot may not move DT from {frm} to {to}")


def decide_on_human_change(frm: DTState | None, to: DTState | None) -> Decision:
    """What to do when a human (BA) changed a DT status."""
    if to is None:
        return Decision("revert", "unknown status for a Draft Ticket", revert_to=frm) if frm else \
            Decision("ignore", "unknown status and no previous state")
    if frm is None:
        # First time we see this DT (e.g. state lost): only a submit is actionable.
        return Decision("process", "submitted") if to is S.SUBMITTED else Decision("ignore", f"observed {to}")
    if frm == to:
        return Decision("ignore", "no change")
    if not is_allowed(frm, to, Actor.HUMAN):
        return Decision("revert", f"{frm} → {to} is not allowed for a BA", revert_to=frm)
    if to is S.SUBMITTED:
        return Decision("process", "submitted")
    return Decision("ignore", f"{frm} → {to} needs no processing")
