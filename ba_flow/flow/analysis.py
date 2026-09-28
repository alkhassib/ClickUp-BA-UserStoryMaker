"""W1: a User Story task in US Intake moved to `to analyze` -> one Draft Ticket (+ gap subtasks) per story.

A document may contain one story or many. Each story is analysed on its own (plus the document's shared
context, e.g. the actors table), gets its own Context Pack and its own Draft Ticket.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ba_flow.app import App
from ba_flow.clickup.resolver import Workspace
from ba_flow.core.observability import current_run_id, get_logger, log
from ba_flow.docs.parser import (
    SUPPORTED_EXTENSIONS, DocumentError, estimate_tokens, extension_of, parse_document, sha256,
)
from ba_flow.docs.stories import STORY_ID, parse_story_filter, split_stories
from ba_flow.flow import render
from ba_flow.flow.context_pack import ContextPack, build_pack, load_pack, save_pack
from ba_flow.flow.state_machine import DTState, StatusMap, assert_bot_transition
from ba_flow.flow.steps import Steps
from ba_flow.llm.base import LLMService, load_prompt
from ba_flow.llm.schemas import AnalysisResult

logger = get_logger("analysis")


class AnalysisError(Exception):
    """An expected failure that should be reported to the BA on the intake task."""


@dataclass
class Document:
    filename: str
    data: bytes
    text: str
    sha: str


@dataclass(frozen=True)
class StoryUnit:
    """One story to analyse. Single-story documents keep the original (suffix-free) keys."""
    story_id: str        # '' when unknown (single-story document without an id)
    title: str
    text: str            # the story's own text (whole document for single-story documents)
    llm_input: str
    run_key: str         # W1:{intake}:{sha12}[:{story}]
    dt_key: str          # DT:{intake}:{sha8}[:{story}]
    multi: bool


_ARABIC = re.compile(r"[؀-ۿ]")
_LATIN = re.compile(r"[A-Za-z]")


def detect_language(text: str) -> str:
    """'ar' when Arabic letters dominate the text, else 'en'."""
    return "ar" if len(_ARABIC.findall(text)) >= len(_LATIN.findall(text)) else "en"


def dt_key_parts(dt_key: str) -> tuple[str, str, str | None]:
    """DT:{intake}:{sha8}[:{story}] -> (intake, sha8, story or None)."""
    parts = dt_key.split(":")
    return parts[1], parts[2], (parts[3] if len(parts) > 3 else None)


class AnalysisHandler:
    def __init__(self, app: App, llm: LLMService):
        self.app = app
        self.llm = llm
        self.cfg = app.settings.clickup
        self.dt_status = StatusMap(self.cfg.statuses.dt)

    def handle(self, intake_task_id: str) -> dict:
        app, cfg = self.app, self.cfg
        ws = app.resolver.load()
        task = app.client.get_task(intake_task_id)
        run_id = current_run_id()

        app.writer.set_status(intake_task_id, cfg.statuses.intake.analyzing)
        try:
            doc = self.load_document(task)
            if run_id:
                app.store.update_run(run_id, doc_sha=doc.sha)
            units, skipped = self.story_units(doc, task)
        except AnalysisError as e:
            self._report_failure(intake_task_id, str(e))
            return {"final_result": "failed", "error": str(e)}
        except Exception as e:
            self._report_failure(intake_task_id, f"Unexpected error: {type(e).__name__}: {e}")
            raise

        done: list[tuple[StoryUnit, str, ContextPack]] = []
        failures: list[tuple[str, str]] = []
        for unit in units:
            label = unit.story_id or "story"
            try:
                steps = Steps(app.store, unit.run_key, dry_run=app.writer.dry_run)
                pack = self.get_or_build_pack(doc, unit, intake_task_id, steps)
                done.append((unit, self._create_tickets(ws, task, pack, unit, steps), pack))
            except AnalysisError as e:
                failures.append((label, str(e)))
            except Exception as e:  # keep going: one broken story must not block the others
                logger.exception("story analysis failed", extra={"data": {"story": label}})
                failures.append((label, f"{type(e).__name__}: {e}"))

        if failures:
            lines = [f"- {sid}: {err}" for sid, err in failures]
            if done:
                lines.append(f"\n{len(done)} other stor{'y was' if len(done) == 1 else 'ies were'} analysed; "
                             "move the task to 'to analyze' again to retry the failed ones.")
            self._report_failure(intake_task_id, "\n" + "\n".join(lines))
            return {"final_result": "failed" if not done else "partial", "dts": len(done), "failed": len(failures),
                    "error": "; ".join(f"{s}: {e}" for s, e in failures)}

        self._intake_ready(task, doc, done, skipped)
        if run_id and len(done) == 1:
            app.store.update_run(run_id, dt_id=done[0][1], pack_id=done[0][2].pack_id)
        return {"final_result": "analyzed", "dts": len(done), "gaps": sum(len(p.gaps) for _, _, p in done),
                "skipped": len(skipped)}

    # --- document -----------------------------------------------------------
    def load_document(self, task: dict) -> Document:
        all_attachments = task.get("attachments") or []
        attachments = [a for a in all_attachments
                       if extension_of(a.get("title") or a.get("url", "")) in SUPPORTED_EXTENSIONS]
        supported = ", ".join(f".{e}" for e in sorted(SUPPORTED_EXTENSIONS))
        if not attachments:
            if all_attachments:
                names = ", ".join(a.get("title") or a.get("url", "?").rsplit("/", 1)[-1] for a in all_attachments)
                raise AnalysisError(f"Attachment type not supported: {names}. Supported: {supported}")
            raise AnalysisError(f"No User Story document attached. Supported: {supported}")
        att = max(attachments, key=lambda a: int(a.get("date") or 0))  # newest wins
        filename = att.get("title") or att["url"].rsplit("/", 1)[-1]
        data = self.app.client.download(att["url"])
        try:
            text = parse_document(data, filename)
        except DocumentError as e:
            raise AnalysisError(str(e)) from e
        log(logger, 20, "document loaded", filename=filename, chars=len(text), est_tokens=estimate_tokens(text))
        return Document(filename=filename, data=data, text=text, sha=sha256(data))

    def story_units(self, doc: Document, task: dict) -> tuple[list[StoryUnit], list[str]]:
        """Stories to analyse (after the optional `stories:` filter) and the ids skipped as cancelled."""
        split = split_stories(doc.text)
        base_run, base_dt = f"W1:{task['id']}:{doc.sha[:12]}", f"DT:{task['id']}:{doc.sha[:8]}"
        limit = self.app.settings.llm.max_doc_tokens

        if not split.is_multi:
            s = split.stories[0]
            self._check_size(s.id or "document", doc.text, limit)
            return [StoryUnit(s.id, s.title, doc.text, f"Document file name: {doc.filename}\n\n<document>\n{doc.text}\n</document>",
                              base_run, base_dt, multi=False)], []

        wanted = parse_story_filter(task.get("text_content") or task.get("description"))
        by_id = {s.id.upper(): s for s in split.stories}
        if unknown := [w for w in wanted if w not in by_id]:
            raise AnalysisError(f"Unknown story id(s) in 'stories:' filter: {', '.join(unknown)}. "
                                f"Available: {', '.join(s.id for s in split.stories)}")
        selected = [by_id[w] for w in wanted] if wanted else split.stories
        skipped = [s.id for s in selected if s.cancelled]

        units = []
        for s in selected:
            if s.cancelled:
                continue
            llm_input = (
                f"Document file name: {doc.filename}\n"
                "This document contains several user stories. Analyse ONLY the story inside <story>. "
                "<shared_context> is background shared by all stories (actors, glossary, conventions).\n\n"
                f"<shared_context>\n{split.shared}\n</shared_context>\n\n<story id=\"{s.id}\">\n{s.text}\n</story>"
            )
            self._check_size(s.id, llm_input, limit)
            units.append(StoryUnit(s.id, s.title, s.text, llm_input, f"{base_run}:{s.id}", f"{base_dt}:{s.id}",
                                   multi=True))
        log(logger, 20, "stories selected", total=len(split.stories), selected=len(units), skipped=len(skipped),
            filtered=bool(wanted))
        if not units:
            raise AnalysisError("No story left to analyse (all selected stories are cancelled).")
        return units, skipped

    @staticmethod
    def _check_size(label: str, text: str, limit: int) -> None:
        if (tokens := estimate_tokens(text)) > limit:
            raise AnalysisError(f"{label} is too long (~{tokens} tokens, limit {limit}; llm.max_doc_tokens).")

    # --- context pack ---------------------------------------------------------
    def get_or_build_pack(self, doc: Document, unit: StoryUnit, intake_task_id: str, steps: Steps) -> ContextPack:
        # A saved pack always wins, even if the step record is missing: gaps may already exist in ClickUp,
        # and re-analysing (e.g. after a prompt change) would produce different gaps than those tasks.
        if pack := load_pack(self.app.store, run_key=unit.run_key):
            log(logger, 20, "reusing context pack", pack_id=pack.pack_id, story=unit.story_id)
            steps.mark("PACK_SAVED", pack.pack_id)
            return pack

        s = self.app.settings
        prompt = load_prompt(s, "analyze")
        prompt = type(prompt)(prompt.name, prompt.version, prompt.text.replace("{max_gaps}", str(s.llm.max_gaps)))
        result = self.llm.run("analyze", prompt, unit.llm_input, AnalysisResult)
        if not result.parsed.gaps:
            # Small models occasionally return an empty list for rich stories; ask once more explicitly, with the
            # `analyze_retry` model (in testing gpt-5-nano/low returned 0 gaps where gpt-5-mini/low found 13).
            log(logger, 30, "no gaps returned, retrying once", story=unit.story_id,
                model=s.llm.model_for("analyze_retry"))
            result = self.llm.run("analyze_retry", prompt, unit.llm_input + (
                "\n\nYour previous answer contained no gaps. Re-read the story carefully against every template "
                "section. Return an empty list only if the story is truly complete and unambiguous."
            ), AnalysisResult)

        pack = build_pack(
            result.parsed, doc_sha=doc.sha, prompt_version=prompt.version, model=result.model,
            letters=self.cfg.fields.drafts.selected_option.letters, max_gaps=s.llm.max_gaps,
        )
        if unit.multi:
            pack.story_id = unit.story_id  # the split already knows it exactly
            # The heading's title is exact; the model sometimes returns the epic name instead.
            pack.story_title = unit.title or pack.story_title
        elif not pack.story_id and (m := STORY_ID.search(doc.text)):
            pack.story_id = m.group(0)  # small models sometimes miss an id that is plainly in the text
        # The model's own language label proved unreliable (Arabic stories with English field names were
        # labelled 'en'), so the DT/gap interface language is decided from the text itself.
        pack.lang = detect_language(unit.text)
        if not pack.gaps:
            # A complete story is a valid outcome: the DT is created without gaps and the BA just submits it.
            log(logger, 30, "no gaps found; DT will be created without gaps", story=unit.story_id)
        if not self.app.writer.dry_run:
            save_pack(self.app.store, pack, run_key=unit.run_key, intake_task_id=intake_task_id)
        steps.mark("PACK_SAVED", pack.pack_id)
        log(logger, 20, "context pack built", pack_id=pack.pack_id, story=unit.story_id, gaps=len(pack.gaps),
            lang=pack.lang)
        return pack

    # --- ClickUp ----------------------------------------------------------------
    def _field_names(self) -> render.FieldNames:
        f = self.cfg.fields.drafts
        return render.FieldNames(
            option_field=f.selected_option.name, custom_field=f.custom_answer, justification=f.justification,
            tbd=f.selected_option.tbd, other=f.selected_option.other, na=f.selected_option.not_applicable,
            submitted=self.cfg.statuses.dt.submitted,
        )

    def _create_tickets(self, ws: Workspace, task: dict, pack: ContextPack, unit: StoryUnit, steps: Steps) -> str:
        app, cfg = self.app, self.cfg
        drafts_id = ws.list_id("drafts")
        source_field = ws.field("drafts", cfg.fields.drafts.source_key).id
        names = self._field_names()
        dt_key = unit.dt_key

        creator = (task.get("creator") or {}).get("id")
        assignees = [int(creator)] if creator and int(creator) != ws.bot_user_id else None

        dt_id = steps.find_or_create(
            "DT_CREATED", app.client, list_id=drafts_id, source_field_id=source_field, source_key=dt_key,
            create=lambda: app.writer.create_task(
                drafts_id, render.dt_name(pack), status=self.dt_status.status_for(DTState.DRAFT),
                markdown=render.dt_markdown(pack, names, task), assignees=assignees,
                custom_fields=[{"id": source_field, "value": dt_key}],
            ),
        )
        if not app.writer.dry_run:
            app.store.set_pack_dt(pack.pack_id, dt_id)

        for gap in pack.gaps:
            gap_key = f"{dt_key}:{gap.id}"
            steps.find_or_create(
                f"GAP_CREATED:{gap.id}", app.client, list_id=drafts_id, source_field_id=source_field,
                source_key=gap_key,
                create=lambda gap=gap, gap_key=gap_key: app.writer.create_task(
                    drafts_id, render.gap_name(gap), parent=dt_id, status=cfg.statuses.gap.open,
                    markdown=render.gap_markdown(gap, pack.lang, names), assignees=assignees,
                    custom_fields=[{"id": source_field, "value": gap_key}],
                ),
            )

        def _ready() -> None:
            assert_bot_transition(DTState.DRAFT, DTState.BA_REVIEW)
            app.writer.set_status(dt_id, self.dt_status.status_for(DTState.BA_REVIEW))

        steps.run("DT_READY", _ready)
        return dt_id

    def _intake_ready(self, task: dict, doc: Document, done: list[tuple[StoryUnit, str, ContextPack]],
                      skipped: list[str]) -> None:
        steps = Steps(self.app.store, f"W1:{task['id']}:{doc.sha[:12]}", dry_run=self.app.writer.dry_run)

        def _do() -> None:
            gaps = sum(len(p.gaps) for _, _, p in done)
            lines = [f"✅ Analysis done: {len(done)} Draft Ticket(s), {gaps} gap(s)."]
            lines += [f"- {render.dt_name(p)} ({len(p.gaps)} gaps): {render.task_url(dt)}" for _, dt, p in done]
            if skipped:
                lines.append(f"\nSkipped (cancelled in the document): {', '.join(skipped)}")
            self.app.writer.set_status(task["id"], self.cfg.statuses.intake.analyzed)
            self.app.writer.comment(task["id"], "\n".join(lines))

        steps.run("INTAKE_READY", _do)

    def _report_failure(self, intake_task_id: str, message: str) -> None:
        log(logger, 40, "analysis failed", task_id=intake_task_id, error=message)
        try:
            self.app.writer.set_status(intake_task_id, self.cfg.statuses.intake.failed)
            self.app.writer.comment(intake_task_id, f"⚠️ Analysis failed: {message}")
        except Exception as e:  # never hide the original failure
            log(logger, 40, "could not report failure to ClickUp", error=str(e))
