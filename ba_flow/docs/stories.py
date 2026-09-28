"""Split a parsed document that contains several user stories into one section per story.

A story starts at a heading such as "### قصة المستخدم: …" or "### User Story: …". The epic heading right
before it belongs to the story. Everything before the first story (versions, actors table…) is shared
context given to every story's analysis.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

STORY_HEADING = re.compile(r"^#{1,6}\s*(?:قصة المستخدم|user story)\b\s*[:：\-–]?\s*(.*)$", re.IGNORECASE)
EPIC_HEADING = re.compile(r"^#{1,6}\s.*\bepic\b", re.IGNORECASE)
STORY_ID = re.compile(r"\bUS-[A-Z0-9]+(?:-[A-Z0-9]+)*-\d+\b")
FILTER_LINE = re.compile(r"^\s*stories\s*[:：]\s*(.+)$", re.IGNORECASE | re.MULTILINE)


CANCELLED = re.compile(r"(?:إلغاء|الغاء|ملغا[ةه]|ملغي[ةه]?|\bcancell?ed\b|\bdeprecated\b)", re.IGNORECASE)


@dataclass(frozen=True)
class StorySection:
    id: str       # US-IND-01, or S01 when the document states no id
    title: str
    text: str

    @property
    def cancelled(self) -> bool:
        """A story whose whole body is just a note that it was cancelled (e.g. "تم الغاء قصة المستخدم")."""
        body = "\n".join(line for line in self.text.split("\n") if not line.startswith("#"))
        return len(body) < 300 and bool(CANCELLED.search(body))


@dataclass(frozen=True)
class SplitDocument:
    shared: str
    stories: list[StorySection]

    @property
    def is_multi(self) -> bool:
        return len(self.stories) > 1


def split_stories(text: str) -> SplitDocument:
    lines = text.split("\n")
    starts = [i for i, line in enumerate(lines) if STORY_HEADING.match(line)]
    if len(starts) < 2:
        m = STORY_ID.search(text)
        return SplitDocument(shared="", stories=[StorySection(id=m.group(0) if m else "", title="", text=text)])

    # Pull a directly preceding epic heading into the story it introduces.
    begins = [s - 1 if s > 0 and EPIC_HEADING.match(lines[s - 1]) else s for s in starts]
    shared = "\n".join(lines[: begins[0]]).strip()

    stories: list[StorySection] = []
    used: set[str] = set()
    for n, (begin, start) in enumerate(zip(begins, starts), start=1):
        end = begins[n] if n < len(begins) else len(lines)
        body = "\n".join(lines[begin:end]).strip()
        m = STORY_ID.search(body)
        sid = m.group(0) if m else f"S{n:02d}"
        if sid in used:  # same id twice in a document: keep both, distinguishable
            sid = f"{sid}~{n}"
        used.add(sid)
        title = STORY_HEADING.match(lines[start]).group(1).strip()
        stories.append(StorySection(id=sid, title=title, text=body))
    return SplitDocument(shared=shared, stories=stories)


def parse_story_filter(description: str | None) -> list[str]:
    """`stories: US-IND-01, US-COMP-01` anywhere in the intake task description. Empty list = all stories."""
    if not description or not (m := FILTER_LINE.search(description)):
        return []
    return [s.strip().upper() for s in re.split(r"[,\s;،]+", m.group(1)) if s.strip()]
