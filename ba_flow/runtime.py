"""Builds the event dispatcher with all handlers (kept apart from app.py to avoid import cycles)."""
from __future__ import annotations

from ba_flow.app import App
from ba_flow.flow.analysis import AnalysisHandler
from ba_flow.flow.validation import ValidationHandler
from ba_flow.flow.dispatcher import DT_SUBMITTED, INTAKE_TO_ANALYZE, Dispatcher
from ba_flow.llm.base import LLMProvider, LLMService, build_provider


def build_dispatcher(app: App, provider: LLMProvider | None = None) -> Dispatcher:
    llm = LLMService(provider or build_provider(app.settings), app.settings, app.store)
    analysis = AnalysisHandler(app, llm)
    validation = ValidationHandler(app, llm, analysis)
    return Dispatcher(app, {
        INTAKE_TO_ANALYZE: lambda event: analysis.handle(event.task_id),
        DT_SUBMITTED: lambda event: validation.handle(event.task_id),
    })
