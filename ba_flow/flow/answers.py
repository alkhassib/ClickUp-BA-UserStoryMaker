"""W2 step 1: read the BA's decisions from the gap subtasks and validate them in code (0 tokens)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ba_flow.core.config import DraftFields
from ba_flow.flow.context_pack import ContextPack

DecisionSource = Literal["option", "custom", "tbd", "not_applicable"]


@dataclass
class GapAnswer:
    gap_id: str                 # G1… from the pack, or BA1… for gaps the BA added by hand
    task_id: str
    title: str
    selection: str | None       # dropdown value name (A, TBD, Other, N/A…)
    custom_answer: str | None
    justification: str | None
    ba_added: bool = False
    question: str = ""
    issues: list[str] = field(default_factory=list)
    decision_source: DecisionSource | None = None
    decision_text: str | None = None


def _field_value(task: dict, name: str) -> object | None:
    for f in task.get("custom_fields") or []:
        if f.get("name") == name:
            value = f.get("value")
            if f.get("type") == "drop_down" and value is not None:
                # Dropdown values come back as the option's orderindex.
                for opt in (f.get("type_config") or {}).get("options", []):
                    if str(opt.get("orderindex")) == str(value) or opt.get("id") == value:
                        return opt.get("name")
                return None
            return value
    return None


def _text(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def collect_answers(subtasks: list[dict], pack: ContextPack, dt_key: str, fields: DraftFields) -> list[GapAnswer]:
    """Map every subtask of the DT to a gap answer. Subtasks without a known Source Key are BA-added gaps."""
    answers: list[GapAnswer] = []
    manual = 0
    for t in subtasks:
        key = _text(_field_value(t, fields.source_key)) or ""
        gap_id = key[len(dt_key) + 1:] if key.startswith(dt_key + ":") else None
        pack_gap = pack.gap(gap_id) if gap_id else None
        if pack_gap is None:
            manual += 1
            gap_id = f"BA{manual}"
        answers.append(GapAnswer(
            gap_id=gap_id,
            task_id=t["id"],
            title=pack_gap.title if pack_gap else t.get("name", ""),
            selection=_text(_field_value(t, fields.selected_option.name)),
            custom_answer=_text(_field_value(t, fields.custom_answer)),
            justification=_text(_field_value(t, fields.justification)),
            ba_added=pack_gap is None,
            question=pack_gap.q if pack_gap else (_text(t.get("text_content")) or t.get("name", "")),
        ))
    # Stable, readable order: G1, G2 … then BA1 …
    answers.sort(key=lambda a: (a.ba_added, int("".join(c for c in a.gap_id if c.isdigit()) or 0)))
    return answers


def validate_answers(answers: list[GapAnswer], pack: ContextPack, fields: DraftFields) -> list[GapAnswer]:
    """Fill `issues` and the resolved decision on each answer. Returns the answers that have issues."""
    so = fields.selected_option
    present = {a.gap_id for a in answers}
    for a in answers:
        a.issues.clear()
        sel = a.selection
        if sel is None:
            a.issues.append(f"no value selected in '{so.name}'")
        elif sel == so.other:
            if a.custom_answer:
                a.decision_source, a.decision_text = "custom", a.custom_answer
            else:
                a.issues.append(f"'{so.other}' selected but '{fields.custom_answer}' is empty")
        elif sel in (so.tbd, so.not_applicable):
            if a.justification:
                a.decision_source = "tbd" if sel == so.tbd else "not_applicable"
                a.decision_text = a.justification
            else:
                a.issues.append(f"'{sel}' selected but '{fields.justification}' is empty")
        elif sel in so.letters:
            opts = {} if a.ba_added else pack.gap(a.gap_id).opts
            if sel in opts:
                a.decision_source, a.decision_text = "option", opts[sel]
            elif a.ba_added:
                a.issues.append(f"gap added manually: use '{so.other}' (with '{fields.custom_answer}'), "
                                f"'{so.tbd}' or '{so.not_applicable}' — it has no lettered options")
            else:
                a.issues.append(f"option {sel} does not exist for this gap (available: {', '.join(opts)})")
        else:
            a.issues.append(f"unknown value '{sel}'")

    for g in pack.gaps:  # a gap subtask deleted by the BA
        if g.id not in present:
            answers.append(GapAnswer(gap_id=g.id, task_id="", title=g.title, selection=None, custom_answer=None,
                                     justification=None, question=g.q,
                                     issues=["gap subtask is missing (deleted?) — restore it or re-run analysis"]))
    return [a for a in answers if a.issues]


def compact_answers(answers: list[GapAnswer]) -> str:
    """What the LLM sees: one line per decision, only the chosen text."""
    lines = []
    for a in answers:
        # N/A gaps produce no rules, but their justification can move a topic out of scope, so it is sent.
        tag = {"option": a.selection, "custom": "CUSTOM", "tbd": "TBD",
               "not_applicable": "NOT_A_GAP"}[a.decision_source]
        prefix = f"{a.gap_id}[BA-added: {a.question}]" if a.ba_added else a.gap_id
        lines.append(f"{prefix}={tag}: {a.decision_text}")
    return "\n".join(lines)
