"""Context Pack: the compact, versioned summary the document is reduced to.

W1 reads the full document once and produces the pack; W2 only ever sees the pack plus the BA's answers.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel

from ba_flow.llm.schemas import AnalysisResult
from ba_flow.store.state import StateStore

SCHEMA_VERSION = 1


class PackGap(BaseModel):
    id: str             # G1, G2, …
    title: str
    q: str              # question
    src: str            # verbatim quote ('' if about missing info)
    section: str
    sev: str            # high | medium | low
    opts: dict[str, str]  # letter -> option text


class ContextPack(BaseModel):
    schema_version: int = SCHEMA_VERSION
    pack_id: str
    doc_sha: str
    prompt_version: str
    analyzer_model: str
    created_at: str
    lang: str
    story_title: str
    story_id: str
    summary: str
    actors: list[str]
    rules: list[str]
    glossary: dict[str, str]
    gaps: list[PackGap]

    def gap(self, gap_id: str) -> PackGap | None:
        return next((g for g in self.gaps if g.id == gap_id), None)

    def compact_json(self) -> str:
        """What W2 sends to the LLM: content only, no metadata, no whitespace."""
        return self.model_dump_json(
            exclude={"schema_version", "pack_id", "doc_sha", "prompt_version", "analyzer_model", "created_at"},
        )


_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def build_pack(analysis: AnalysisResult, *, doc_sha: str, prompt_version: str, model: str,
               letters: list[str], max_gaps: int) -> ContextPack:
    """Normalize LLM output: stable order, sequential ids, option letters, enforced limits."""
    gaps = sorted(analysis.gaps, key=lambda g: _SEVERITY_ORDER.get(g.severity, 3))[:max_gaps]
    pack_gaps = []
    for i, g in enumerate(gaps, start=1):
        options = [o.text.strip() for o in g.options if o.text.strip()][: len(letters)]
        if not options:
            continue
        pack_gaps.append(PackGap(
            id=f"G{i}", title=g.title.strip(), q=g.question.strip(), src=g.source_quote.strip(),
            section=g.section.strip(), sev=g.severity, opts=dict(zip(letters, options)),
        ))
    # Re-number after dropping option-less gaps so ids stay contiguous.
    for i, g in enumerate(pack_gaps, start=1):
        g.id = f"G{i}"
    lang = analysis.doc_language if analysis.doc_language in ("ar", "en") else "en"
    return ContextPack(
        pack_id=uuid.uuid4().hex, doc_sha=doc_sha, prompt_version=prompt_version, analyzer_model=model,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), lang=lang,
        story_title=analysis.story_title.strip(), story_id=analysis.story_id.strip(),
        summary=analysis.summary.strip(), actors=analysis.actors, rules=analysis.rules,
        glossary={t.term: t.meaning for t in analysis.glossary}, gaps=pack_gaps,
    )


def save_pack(store: StateStore, pack: ContextPack, *, run_key: str, intake_task_id: str) -> None:
    store.save_pack(
        pack_id=pack.pack_id, run_key=run_key, intake_task_id=intake_task_id, doc_sha=pack.doc_sha,
        schema_version=pack.schema_version, prompt_version=pack.prompt_version,
        analyzer_model=pack.analyzer_model, lang=pack.lang, body=pack.model_dump_json(),
        created_at=pack.created_at,
    )


def load_pack(store: StateStore, *, run_key: str | None = None, dt_id: str | None = None) -> ContextPack | None:
    row = store.get_pack(run_key=run_key, dt_id=dt_id)
    if not row:
        return None
    if row["schema_version"] != SCHEMA_VERSION:
        # Future migrations go here; until then an outdated pack is rebuilt from the document.
        return None
    return ContextPack.model_validate_json(row["body"])
