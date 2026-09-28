"""Render a UserStoryDraft into the ClickUp ticket markdown, and check it before publishing."""
from __future__ import annotations

import re
from dataclasses import dataclass

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ba_flow.core.config import Settings
from ba_flow.flow.answers import GapAnswer
from ba_flow.llm.schemas import UserStoryDraft

# Section -> mandatory? (per the template's definition; "mandatory for screens" counts as optional here)
SECTIONS: dict[str, bool] = {
    "preconditions": True, "trigger": True, "dependencies": False, "scenarios": True, "business_rules": True,
    "fields": False, "buttons": False, "messages": False, "permissions": True, "timed_events": False,
    "acceptance_criteria": True, "examples": True, "outputs": True, "nfr": False, "design": False,
    "out_of_scope": True,
}

LABELS = {
    "en": {
        "missing": "Not specified in the source document.",
        "yes": "Yes", "no": "No",
        "scenario": {"main": "🟢 Main", "alternative": "🔵 Alternative", "exception": "🔴 Exception"},
        "ac": {"positive": "🟢 Positive", "negative": "🔴 Negative", "boundary": "🟡 Boundary",
               "alternative": "🔵 Alternative"},
        "outputs": {"saved": "Saved", "new_status": "New status", "numbers": "Generated numbers",
                    "notifications": "Notifications", "audit": "Audit", "where_visible": "Where the effect appears"},
        "source": {"option": "Option", "custom": "BA answer"},
    },
    "ar": {
        "missing": "لم يُذكر في المستند.",
        "yes": "نعم", "no": "لا",
        "scenario": {"main": "🟢 رئيسي", "alternative": "🔵 بديل", "exception": "🔴 استثنائي"},
        "ac": {"positive": "🟢 إيجابي", "negative": "🔴 سلبي", "boundary": "🟡 حدّي", "alternative": "🔵 بديل"},
        "outputs": {"saved": "ما يُحفظ", "new_status": "الحالة الجديدة", "numbers": "الأرقام المتولدة",
                    "notifications": "الإشعارات", "audit": "التدقيق", "where_visible": "أين يظهر الأثر"},
        "source": {"option": "خيار", "custom": "إجابة المحلل"},
    },
}


def _cell(value: object) -> str:
    if value is None or value == "":
        return "—"
    return str(value).replace("|", "/").replace("\r", " ").replace("\n", "<br>")


def _steps(items: list[str] | None) -> str:
    if not items:
        return "—"
    return "<br>".join(f"{i}. {_cell(s)}" for i, s in enumerate(items, start=1))


def _environment(settings: Settings) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(settings.root)), trim_blocks=True, lstrip_blocks=True,
        undefined=StrictUndefined, keep_trailing_newline=True, autoescape=False,
    )
    env.filters["cell"] = _cell
    env.filters["steps"] = _steps
    env.filters["check"] = lambda v: "✅" if v else "❌"
    env.filters["yesno"] = lambda v, L: "—" if v is None else (L["yes"] if v else L["no"])
    return env


@dataclass
class OpenQuestion:
    id: str
    gap: str
    text: str


def open_questions(answers: list[GapAnswer]) -> list[OpenQuestion]:
    tbd = [a for a in answers if a.decision_source == "tbd"]
    return [OpenQuestion(id=f"Q-{i:02d}", gap=a.gap_id, text=f"{a.question} — {a.decision_text}")
            for i, a in enumerate(tbd, start=1)]


def render_story(settings: Settings, story: UserStoryDraft, answers: list[GapAnswer]) -> str:
    lang = settings.output.user_story_language
    L = LABELS[lang]
    show: dict[str, str | None] = {}
    for key, mandatory in SECTIONS.items():
        policy = settings.output.empty_mandatory if mandatory else settings.output.empty_optional
        show[key] = "full" if getattr(story, key) else ("missing" if policy == "mark" else None)

    decisions = []
    if settings.output.include_decisions:
        decisions = [
            {"gap": a.gap_id, "title": a.title, "text": a.decision_text,
             "source": f"{L['source']['option']} {a.selection}" if a.decision_source == "option"
             else L["source"]["custom"]}
            for a in answers if a.decision_source in ("option", "custom")
        ]

    template = _environment(settings).get_template(settings.output.templates[lang])
    md = template.render(s=story, L=L, show=show, open_questions=open_questions(answers), decisions=decisions)
    return re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"


# --- checks before publishing ---------------------------------------------------------------

# Template placeholders only; a plain ellipsis can be legitimate text (e.g. "Loading…").
PLACEHOLDERS = re.compile(
    r"US-XXX|\b[A-Z]{2,4}-nnn?\b|DD/MM/YYYY|\[(?:role|action|value|Field Name|Button|condition|N/A \+)[^\]]*\]",
    re.IGNORECASE,
)
ARABIC = re.compile(r"[؀-ۿ]")
LATIN = re.compile(r"[A-Za-z]")


def _texts(obj: object) -> list[str]:
    if obj is None:
        return []
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, (list, tuple)):
        return [t for x in obj for t in _texts(x)]
    if hasattr(obj, "model_dump"):
        return _texts(list(obj.model_dump().values()))
    if isinstance(obj, dict):
        return _texts(list(obj.values()))
    return []


# Repository identifiers the story may only reuse, never create (BR/AC/EX/SC are numbered by the story itself).
REPO_ID = re.compile(r"\b(?:MSG|CFG|LKP|UI|ST|NUM|NTF|AUD|INT|CMP|GR)-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")


def check_story(story: UserStoryDraft, language: str, source_text: str | None = None) -> list[str]:
    """Deterministic problems in a generated story. Empty list = OK.

    `source_text` is everything the model was given; repository identifiers must come from it.
    """
    problems: list[str] = []
    texts = _texts(story)
    for t in texts:
        if m := PLACEHOLDERS.search(t):
            problems.append(f"template placeholder left in text: '{m.group(0)}' in \"{t[:60]}\"")
            break

    if source_text is not None:
        invented = sorted({m.group(0) for t in texts for m in REPO_ID.finditer(t)} - set(REPO_ID.findall(source_text)))
        if invented:
            problems.append("identifiers not present in the document or decisions (do not invent ids; "
                            f"use '—' instead): {', '.join(invented)}")

    br_ids = {r.id for r in story.business_rules or []}
    ac_ids = {a.id for a in story.acceptance_criteria or []}
    # A criterion may verify several rules ("BR-06, BR-10") and an example may show several criteria.
    for ac in story.acceptance_criteria or []:
        unknown = [r for r in re.findall(r"BR-\d+", ac.rule or "") if r not in br_ids]
        if unknown:
            problems.append(f"{ac.id} references unknown rule(s) {', '.join(unknown)}")
    for ex in story.examples or []:
        refs = re.findall(r"AC-\d+", ex.criterion)
        unknown = [c for c in refs if c not in ac_ids] if refs else [ex.criterion]
        if unknown:
            problems.append(f"{ex.id} references unknown criterion {', '.join(unknown)}")

    sections = {
        "title": [story.title],
        "statement": [story.as_a, story.i_want, story.so_that],
        "business_rules": [r.rule for r in story.business_rules or []],
        "acceptance_criteria": [f"{a.given} {a.when} {a.then}" for a in story.acceptance_criteria or []],
        "scenarios": [s for sc in story.scenarios or [] for s in sc.steps],
    }
    wrong = []
    for name, parts in sections.items():
        text = " ".join(parts)
        ar, lat = len(ARABIC.findall(text)), len(LATIN.findall(text))
        if ar + lat == 0:
            continue
        # Latin identifiers/field names inside Arabic prose are fine; mostly-Latin prose is not.
        if (language == "ar" and ar < lat) or (language == "en" and ar > lat):
            wrong.append(name)
    if wrong:
        target = "Arabic" if language == "ar" else "English"
        problems.append(f"not written in {target}: {', '.join(wrong)} — translate every text field to {target}")
    return problems
