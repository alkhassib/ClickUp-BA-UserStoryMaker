"""Structured outputs requested from the LLM.

Kept compatible with strict JSON-schema modes (OpenAI + Anthropic): every field is required,
no free-form dicts. Limits (gap count, option count) are enforced in code after parsing.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class GapOption(BaseModel):
    text: str = Field(description="One concrete, self-contained resolution; one sentence.")


class Gap(BaseModel):
    title: str = Field(description="Short label, max ~10 words.")
    question: str = Field(description="The precise question the BA must answer.")
    source_quote: str = Field(
        description="Verbatim excerpt (max ~30 words) from the document that the gap is about; "
                    "empty string if the gap is about something the document does not mention at all."
    )
    section: str = Field(description="User story template section affected, e.g. 'Business Rules', "
                                     "'Permissions', 'Acceptance Criteria'.")
    severity: Literal["high", "medium", "low"]
    options: list[GapOption] = Field(description="2 to 5 mutually exclusive options. Do NOT add TBD/Other/N/A.")


class Term(BaseModel):
    term: str
    meaning: str


class AnalysisResult(BaseModel):
    story_title: str = Field(description="Short title of the user story, in the document language.")
    story_id: str = Field(description="Story identifier if the document states one (e.g. US-ABC-01), else ''.")
    doc_language: Literal["ar", "en", "mixed"]
    summary: str = Field(description="What the story is about in 3-5 sentences.")
    actors: list[str]
    rules: list[str] = Field(description="Business rules explicitly stated in the document, one per item, "
                                         "condensed. Do not invent rules.")
    glossary: list[Term] = Field(description="Domain terms needed to understand the story; may be empty.")
    gaps: list[Gap]


# --- W2: validation + user story -------------------------------------------------------------

Str = str | None


class Contradiction(BaseModel):
    gaps: list[str] = Field(description="Ids of the conflicting decisions, e.g. ['G2', 'G5'].")
    reason: str = Field(description="Why they conflict, max ~25 words.")


class Dependency(BaseModel):
    type: str = Field(description="Story / Integration / Component / Global rule")
    id: str
    note: Str


class Scenario(BaseModel):
    id: str = Field(description="SC-01, SC-02, …")
    type: Literal["main", "alternative", "exception"]
    steps: list[str]


class BusinessRule(BaseModel):
    id: str = Field(description="BR-01, BR-02, …")
    rule: str = Field(description="If … then …")
    effect: Str
    message: Str = Field(description="Message id or exact text shown, if stated.")


class FieldRow(BaseModel):
    name: str = Field(description="Field name; 'English / عربي' when both are known.")
    type: Str
    required: bool | None
    editable: bool | None
    default: Str
    constraints: Str
    source: Str
    notes: Str = Field(description="Visibility, rules (BR-nn) and messages.")


class ButtonRow(BaseModel):
    button: str
    visible_when: Str
    enabled_when: Str
    on_click: list[str]
    confirmation: Str
    navigates_to: Str


class MessageRow(BaseModel):
    id: str
    text: str = Field(description="Exact text as stated; never invent message texts.")
    shown_when: Str


class PermissionRow(BaseModel):
    role: str
    sees: bool
    executes: bool
    unauthorized_behavior: Str


class TimedEvent(BaseModel):
    event: str
    formula: Str
    setting: Str
    result: Str


class AcceptanceCriterion(BaseModel):
    id: str = Field(description="AC-01, AC-02, …")
    given: str
    when: str
    then: str
    rule: Str = Field(description="BR id this criterion verifies.")
    type: Literal["positive", "negative", "boundary", "alternative"]


class Example(BaseModel):
    id: str = Field(description="EX-01, EX-02, …")
    input: str = Field(description="Only what differs from the baseline values.")
    expected: str
    criterion: str = Field(description="AC id this example demonstrates.")


class Outputs(BaseModel):
    saved: Str
    new_status: Str
    numbers: Str
    notifications: Str
    audit: Str
    where_visible: Str


class StoryMetadata(BaseModel):
    epic: Str
    actor: Str
    channel: Literal["Web", "App", "Backend", "System"] | None
    story_type: Literal["Input", "View", "Decision", "Management", "Scheduled", "Integration"] | None
    priority: Literal["Must", "Should", "Could", "Won't"] | None
    version: Str


class UserStoryDraft(BaseModel):
    """Every section is nullable: null means 'not stated in the document or decisions'."""
    title: str
    as_a: str
    i_want: str
    so_that: str
    preconditions: list[str] | None
    trigger: Str
    dependencies: list[Dependency] | None
    scenarios: list[Scenario] | None
    business_rules: list[BusinessRule] | None
    fields: list[FieldRow] | None
    buttons: list[ButtonRow] | None
    messages: list[MessageRow] | None
    permissions: list[PermissionRow] | None
    timed_events: list[TimedEvent] | None
    acceptance_criteria: list[AcceptanceCriterion] | None
    assumed_today: Str = Field(description="DD/MM/YYYY used by the examples, if examples are given.")
    baseline_values: Str
    examples: list[Example] | None
    outputs: Outputs | None
    nfr: list[str] | None
    design: Str
    out_of_scope: list[str] | None
    metadata: StoryMetadata


class ValidationOutput(BaseModel):
    contradictions: list[Contradiction]
    user_stories: list[UserStoryDraft] | None = Field(
        description="null when there are contradictions; otherwise usually exactly one story."
    )
