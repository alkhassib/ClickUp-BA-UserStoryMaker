"""W2: a Draft Ticket moved to `submitted` -> validate the BA's decisions -> return it, or write the story.

Cost order: deterministic checks (0 tokens) -> LLM result cache (0 tokens) -> one combined LLM call
(contradictions + user story) that sees only the Context Pack and the decisions, never the document.
"""
from __future__ import annotations

import hashlib

from ba_flow.app import App
from ba_flow.clickup.resolver import Workspace
from ba_flow.core.observability import current_run_id, get_logger, log
from ba_flow.flow import render
from ba_flow.flow.analysis import AnalysisError, AnalysisHandler, dt_key_parts
from ba_flow.flow.answers import GapAnswer, collect_answers, compact_answers, validate_answers
from ba_flow.flow.context_pack import ContextPack, load_pack, save_pack
from ba_flow.flow.state_machine import DTState, StatusMap, assert_bot_transition
from ba_flow.flow.steps import Steps
from ba_flow.flow.story import check_story, render_story
from ba_flow.llm.base import LLMService, load_prompt
from ba_flow.llm.schemas import UserStoryDraft, ValidationOutput

logger = get_logger("validation")

LANGUAGE_NAMES = {"ar": "Arabic", "en": "English"}


def _field(task: dict, name: str) -> object | None:
    return next((f.get("value") for f in task.get("custom_fields") or [] if f.get("name") == name), None)


class ValidationHandler:
    def __init__(self, app: App, llm: LLMService, analysis: AnalysisHandler):
        self.app = app
        self.llm = llm
        self.analysis = analysis
        self.cfg = app.settings.clickup
        self.dt_status = StatusMap(self.cfg.statuses.dt)

    def handle(self, dt_id: str) -> dict:
        app, cfg = self.app, self.cfg
        ws = app.resolver.load()
        run_id = current_run_id()
        if run_id:
            app.store.update_run(run_id, dt_id=dt_id)

        dt = app.client.get_task(dt_id)
        if dt.get("parent"):
            return {"final_result": "skipped", "reason": "not a Draft Ticket (it is a subtask)"}
        if self.dt_status.state_for(dt["status"]["status"]) is not DTState.SUBMITTED:
            return {"final_result": "skipped", "reason": f"status is '{dt['status']['status']}'"}
        dt_key = str(_field(dt, cfg.fields.drafts.source_key) or "")
        if not dt_key.startswith("DT:"):
            return {"final_result": "skipped", "reason": "not created by BA Flow (no DT source key)"}

        try:
            pack = self._pack_for(dt_id, dt_key)
        except AnalysisError as e:
            return self._fail(dt, f"Context pack unavailable: {e}")
        if run_id:
            app.store.update_run(run_id, pack_id=pack.pack_id, doc_sha=pack.doc_sha)

        drafts_id = ws.list_id("drafts")
        subtasks = [t for t in app.client.get_list_tasks(drafts_id, subtasks=True, include_closed=True)
                    if t.get("parent") == dt_id]
        answers = collect_answers(subtasks, pack, dt_key, cfg.fields.drafts)
        problems = validate_answers(answers, pack, cfg.fields.drafts)
        self._sync_gap_statuses(answers, subtasks)

        if problems:
            lines = [f"- **{a.gap_id}** {a.title}: " + "; ".join(a.issues) for a in problems]
            return self._return(dt, "incomplete", "⚠️ Some gaps are not answered correctly:", lines)

        decisions = compact_answers(answers)
        result, warnings = self._validate_and_write(pack, decisions)
        if result.contradictions:
            lines = [f"- **{' ↔ '.join(c.gaps)}**: {c.reason}" for c in result.contradictions]
            return self._return(dt, "contradictions", "⚠️ Contradicting decisions found. Please revise:", lines)
        if not result.user_stories:
            return self._fail(dt, "The model returned neither contradictions nor a user story.")

        story_ids = self._finalize(ws, dt, dt_key, pack, answers, result.user_stories, decisions, warnings)
        if run_id:
            app.store.update_run(run_id, validation_result="ok_with_warnings" if warnings else "ok")
        return {"final_result": "compiled", "stories": story_ids, "warnings": len(warnings)}

    # --- context pack -------------------------------------------------------------------------
    def _pack_for(self, dt_id: str, dt_key: str) -> ContextPack:
        if pack := load_pack(self.app.store, dt_id=dt_id):
            return pack
        # Fallback: rebuild from the intake document (normally an LLM cache hit, so no tokens).
        intake_id, sha8, story = dt_key_parts(dt_key)
        log(logger, 30, "context pack missing, rebuilding from intake document", intake=intake_id, story=story)
        doc = self.analysis.load_document(self.app.client.get_task(intake_id))
        if doc.sha[:8] != sha8:
            raise AnalysisError("the intake document changed since the analysis; analyze it again")
        units, _ = self.analysis.story_units(doc, {"id": intake_id})  # no filter: find this DT's story
        unit = next((u for u in units if u.dt_key == dt_key), None)
        if unit is None:
            raise AnalysisError(f"story {story} is no longer in the intake document")
        steps = Steps(self.app.store, unit.run_key, dry_run=True)  # do not touch W1's recorded steps
        pack = self.analysis.get_or_build_pack(doc, unit, intake_id, steps)
        if not self.app.writer.dry_run:
            save_pack(self.app.store, pack, run_key=unit.run_key, intake_task_id=intake_id)
            self.app.store.set_pack_dt(pack.pack_id, dt_id)
        return pack

    # --- LLM ----------------------------------------------------------------------------------
    def _validate_and_write(self, pack: ContextPack, decisions: str) -> tuple[ValidationOutput, list[str]]:
        """Returns the model output and any check problems that remain after one retry."""
        lang = self.app.settings.output.user_story_language
        prompt = load_prompt(self.app.settings, "validate_and_write")
        prompt = type(prompt)(prompt.name, prompt.version, prompt.text.replace("{language_name}", LANGUAGE_NAMES[lang]))
        user = f"CONTEXT:\n{pack.compact_json()}\n\nDECISIONS:\n{decisions}"

        result = self.llm.run("validate_and_write", prompt, user, ValidationOutput).parsed
        if not result.contradictions and not result.user_stories:
            log(logger, 30, "empty validation result, retrying once")
            result = self.llm.run("validate_and_write", prompt, user + "\n\nYou must return either at least one "
                                  "contradiction or the user story (user_stories).", ValidationOutput).parsed
        if result.contradictions or not result.user_stories:
            return result, []

        def problems_of(r: ValidationOutput) -> list[str]:
            return [p for s in r.user_stories or [] for p in check_story(s, lang, source_text=user)]

        problems = problems_of(result)
        if problems:
            log(logger, 30, "story failed checks, retrying once", problems=problems)
            retry_user = user + "\n\nYour previous answer had these problems; fix them:\n" + \
                "\n".join(f"- {p}" for p in problems)
            retry = self.llm.run("validate_and_write", prompt, retry_user, ValidationOutput).parsed
            if retry.contradictions:
                return retry, []
            if retry.user_stories and len(retry_problems := problems_of(retry)) <= len(problems):
                result, problems = retry, retry_problems
            if problems:
                log(logger, 30, "story still has problems after retry; publishing with a warning", problems=problems)
        return result, problems

    # --- ClickUp ------------------------------------------------------------------------------
    def _sync_gap_statuses(self, answers: list[GapAnswer], subtasks: list[dict]) -> None:
        """Answered gaps -> `gap closed`, unanswered/invalid -> `gap open`, so the BA sees what is left."""
        g = self.cfg.statuses.gap
        current = {t["id"]: t["status"]["status"].lower() for t in subtasks}
        for a in answers:
            if not a.task_id or current.get(a.task_id) == g.compiled.lower():
                continue
            target = g.open if a.issues else g.closed
            if current.get(a.task_id) != target.lower():
                self.app.writer.set_status(a.task_id, target)

    def _return(self, dt: dict, kind: str, headline: str, lines: list[str]) -> dict:
        assert_bot_transition(DTState.SUBMITTED, DTState.RETURNED)
        assert_bot_transition(DTState.RETURNED, DTState.BA_REVIEW)
        text = "\n".join([headline, *lines, "",
                          f"Fix them and move this ticket to **{self.cfg.statuses.dt.submitted}** again."])
        self.app.writer.comment(dt["id"], text, notify_all=True)
        self.app.writer.set_status(dt["id"], self.dt_status.status_for(DTState.RETURNED))
        if run_id := current_run_id():
            self.app.store.update_run(run_id, validation_result=kind)
        log(logger, 20, "DT returned to BA", reason=kind, items=len(lines))
        return {"final_result": "returned", "reason": kind, "items": len(lines)}

    def _fail(self, dt: dict, message: str) -> dict:
        log(logger, 40, "validation failed", dt_id=dt["id"], error=message)
        self.app.writer.comment(dt["id"], f"❌ Processing failed: {message}", notify_all=True)
        self.app.writer.set_status(dt["id"], self.dt_status.status_for(DTState.FAILED))
        return {"final_result": "failed", "error": message}

    def _intake_progress(self, ws: Workspace, intake_id: str, sha8: str, current_dt: str) -> tuple[int, int]:
        """(compiled, total) Draft Tickets created from this intake document. The current DT counts as compiled."""
        prefix = f"DT:{intake_id}:{sha8}"
        field_id = ws.field("drafts", self.cfg.fields.drafts.source_key).id
        dts = [
            t for t in self.app.client.get_list_tasks(ws.list_id("drafts"), subtasks=True, include_closed=True,
                                                      custom_fields=[{"field_id": field_id, "operator": "=",
                                                                      "value": prefix}])
            if not t.get("parent") and str(_field(t, self.cfg.fields.drafts.source_key) or "").startswith(prefix)
        ]
        compiled_status = self.cfg.statuses.dt.compiled.lower()
        compiled = sum(1 for t in dts if t["id"] == current_dt or t["status"]["status"].lower() == compiled_status)
        return compiled, max(len(dts), 1)

    def _story_name(self, pack: ContextPack, story: UserStoryDraft, index: int, total: int) -> str:
        if not pack.story_id:
            return story.title
        sid = pack.story_id if total == 1 else f"{pack.story_id}.{index}"
        return f"[{sid}] {story.title}"

    def _story_fields(self, ws: Workspace, story: UserStoryDraft, source_key: str) -> list[dict]:
        stories = ws.lists["stories"]
        f = self.cfg.fields.stories
        out = [{"id": stories.fields[f.source_key].id, "value": source_key}]
        meta = story.metadata.model_dump()
        for key, field_name in f.metadata.items():
            value, info = meta.get(key), stories.fields.get(field_name)
            if value is None or info is None:
                continue
            if info.type == "drop_down":
                if value in info.options:
                    out.append({"id": info.id, "value": info.options[value]})
            else:
                out.append({"id": info.id, "value": str(value)})
        return out

    def _finalize(self, ws: Workspace, dt: dict, dt_key: str, pack: ContextPack, answers: list[GapAnswer],
                  stories: list[UserStoryDraft], decisions: str, warnings: list[str]) -> list[str]:
        app, cfg = self.app, self.cfg
        run_key = f"W2:{dt['id']}:{hashlib.sha256(decisions.encode()).hexdigest()[:12]}"
        steps = Steps(app.store, run_key, dry_run=app.writer.dry_run)
        stories_list = ws.list_id("stories")
        source_field = ws.field("stories", cfg.fields.stories.source_key).id

        story_ids: list[str] = []
        for i, story in enumerate(stories, start=1):
            key = f"US:{dt_key}:{i}"
            story_ids.append(steps.find_or_create(
                f"USER_STORY_CREATED:{i}", app.client, list_id=stories_list, source_field_id=source_field,
                source_key=key,
                create=lambda story=story, i=i, key=key: app.writer.create_task(
                    stories_list, self._story_name(pack, story, i, len(stories)), status=cfg.statuses.story.new,
                    markdown=render_story(app.settings, story, answers),
                    custom_fields=self._story_fields(ws, story, key),
                    assignees=[a["id"] for a in dt.get("assignees") or []] or None,
                ),
            ))

        if len(story_ids) > 1:
            def _link() -> None:
                for prev, nxt in zip(story_ids, story_ids[1:]):
                    app.writer.add_dependency(nxt, prev)
            steps.run("STORIES_LINKED", _link)

        def _gaps() -> None:
            for a in answers:
                if a.task_id:
                    app.writer.set_status(a.task_id, cfg.statuses.gap.compiled)
        steps.run("GAPS_UPDATED", _gaps)

        def _dt() -> None:
            assert_bot_transition(DTState.SUBMITTED, DTState.COMPILED)
            links = "\n".join(f"- {render.task_url(s)}" for s in story_ids)
            text = f"✅ Validated. User story created:\n{links}"
            if warnings:
                text += "\n\n⚠️ Please review — automatic checks still flag:\n" + "\n".join(f"- {w}" for w in warnings)
            app.writer.comment(dt["id"], text, notify_all=True)
            app.writer.set_status(dt["id"], self.dt_status.status_for(DTState.COMPILED))
        steps.run("DT_UPDATED", _dt)

        def _intake() -> None:
            intake_id, sha8, story = dt_key_parts(dt_key)
            compiled, total = self._intake_progress(ws, intake_id, sha8, dt["id"])
            label = f"{story}: " if story else ""
            links = "\n".join(f"- {render.task_url(s)}" for s in story_ids)
            if compiled >= total:
                app.writer.set_status(intake_id, cfg.statuses.intake.done)
                app.writer.comment(intake_id, f"✅ {label}User story created:\n{links}"
                                   + (f"\n\nAll {total} Draft Tickets are compiled." if total > 1 else ""))
            else:
                app.writer.comment(intake_id, f"✅ {label}User story created ({compiled}/{total} compiled):\n{links}")
        steps.run("INTAKE_UPDATED", _intake)
        steps.mark("FINALIZATION_COMPLETE")
        return story_ids
