"""Markdown for the Draft Ticket and gap subtasks (built by code, not by the LLM)."""
from __future__ import annotations

from dataclasses import dataclass

from ba_flow.flow.context_pack import ContextPack, PackGap

SEVERITY_ICON = {"high": "🔴", "medium": "🟡", "low": "🟢"}

LABELS = {
    "en": {
        "summary": "Summary", "actors": "Actors", "source": "Source", "gaps": "Gaps", "gap": "Gap",
        "section": "Section", "severity": "Severity", "question": "Question", "options": "Options",
        "how": "How to answer",
        "sev": {"high": "High", "medium": "Medium", "low": "Low"},
        "steps": [
            "Open each gap subtask and choose a value in **{option_field}**.",
            "**{other}** → write your answer in **{custom_field}**.",
            "**{tbd}** → explain in **{justification}**. It becomes an open question in the user story.",
            "**{na}** → the gap is not real. Explain why in **{justification}**.",
            "When every gap is answered, move this ticket to **{submitted}**.",
        ],
        "gap_hint": "Choose one value in **{option_field}**. Also available: **{tbd}** (fill **{justification}**), "
                    "**{other}** (fill **{custom_field}**), **{na}** — not a real gap (fill **{justification}**).",
        "no_quote": "Not mentioned in the document.",
        "no_gaps": "No gaps were found: the story looks complete. Review it and move this ticket to "
                   "**{submitted}** to generate the user story.",
    },
    "ar": {
        "summary": "الملخص", "actors": "الأدوار", "source": "المصدر", "gaps": "الفجوات", "gap": "الفجوة",
        "section": "القسم", "severity": "الأهمية", "question": "السؤال", "options": "الخيارات",
        "how": "طريقة الإجابة",
        "sev": {"high": "عالية", "medium": "متوسطة", "low": "منخفضة"},
        "steps": [
            "افتح كل فجوة (subtask) واختر قيمة من حقل **{option_field}**.",
            "**{other}** ← اكتب إجابتك في حقل **{custom_field}**.",
            "**{tbd}** ← اكتب السبب في حقل **{justification}**، وستظهر كسؤال مفتوح في الـ User Story.",
            "**{na}** ← الفجوة غير حقيقية، اكتب السبب في حقل **{justification}**.",
            "بعد الإجابة على كل الفجوات انقل هذه التذكرة إلى **{submitted}**.",
        ],
        "gap_hint": "اختر قيمة واحدة من **{option_field}**. متاح أيضاً: **{tbd}** (املأ **{justification}**)، "
                    "**{other}** (املأ **{custom_field}**)، **{na}** — ليست فجوة حقيقية (املأ **{justification}**).",
        "no_quote": "غير مذكور في المستند.",
        "no_gaps": "لم تُكتشف فجوات، والقصة تبدو مكتملة. راجعها ثم انقل هذه التذكرة إلى **{submitted}** "
                   "لتوليد الـ User Story.",
    },
}


@dataclass(frozen=True)
class FieldNames:
    option_field: str
    custom_field: str
    justification: str
    tbd: str
    other: str
    na: str
    submitted: str


def task_url(task_id: str) -> str:
    return f"https://app.clickup.com/t/{task_id}"


def dt_name(pack: ContextPack) -> str:
    prefix = f"{pack.story_id} " if pack.story_id else ""
    return f"DT · {prefix}{pack.story_title}"


def gap_name(gap: PackGap) -> str:
    return f"{gap.id} · {gap.title}"


def dt_markdown(pack: ContextPack, names: FieldNames, intake_task: dict) -> str:
    L = LABELS[pack.lang]
    fmt = names.__dict__
    lines = [f"## 🧩 {L['summary']}", pack.summary, ""]
    if pack.actors:
        lines.append(f"**{L['actors']}:** " + " · ".join(pack.actors))
    lines += [f"**{L['source']}:** [{intake_task.get('name', intake_task['id'])}]({task_url(intake_task['id'])})", "",
              "---", "", f"## 🕳️ {L['gaps']} ({len(pack.gaps)})"]
    if not pack.gaps:
        return "\n".join([*lines, L["no_gaps"].format(**fmt)])
    lines += [f"| # | {L['gap']} | {L['section']} | {L['severity']} |", "|---|---|---|---|"]
    for g in pack.gaps:
        lines.append(f"| **{g.id}** | {_cell(g.title)} | {_cell(g.section)} | "
                     f"{SEVERITY_ICON.get(g.sev, '')} {L['sev'].get(g.sev, g.sev)} |")
    lines += ["", "---", "", f"## ✍️ {L['how']}"]
    lines += [f"{i}. {step.format(**fmt)}" for i, step in enumerate(L["steps"], start=1)]
    return "\n".join(lines)


def gap_markdown(gap: PackGap, lang: str, names: FieldNames) -> str:
    L = LABELS[lang]
    lines = [f"**{L['question']}:** {gap.q}", ""]
    lines.append(f"> {gap.src}" if gap.src else f"> _{L['no_quote']}_")
    lines += ["", f"**{L['section']}:** {gap.section} · **{L['severity']}:** "
                  f"{SEVERITY_ICON.get(gap.sev, '')} {L['sev'].get(gap.sev, gap.sev)}",
              "", f"### {L['options']}"]
    lines += [f"**{letter})** {text}" for letter, text in gap.opts.items()]
    lines += ["", "---", L["gap_hint"].format(**names.__dict__)]
    return "\n".join(lines)


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")
