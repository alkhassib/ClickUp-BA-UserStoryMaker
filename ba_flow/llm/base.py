"""Provider-neutral LLM interface + a service adding result caching and usage accounting."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

from ba_flow.core.config import Settings
from ba_flow.core.observability import current_run_id, get_logger, log
from ba_flow.store.state import StateStore

T = TypeVar("T", bound=BaseModel)
logger = get_logger("llm")


class LLMError(Exception):
    pass


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass
class LLMResult(Generic[T]):
    parsed: T
    usage: LLMUsage
    model: str
    cached: bool = False


class LLMProvider(Protocol):
    name: str

    def parse(self, *, model: str, system: str, user: str, schema: type[T], max_output_tokens: int,
              effort: str | None = None) -> LLMResult[T]:
        ...


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    text: str


_VERSION_RE = re.compile(r"<!--\s*version:\s*(\S+)\s*-->")


def load_prompt(settings: Settings, name: str) -> Prompt:
    path: Path = settings.path(f"config/prompts/{name}.md")
    text = path.read_text(encoding="utf-8")
    m = _VERSION_RE.search(text)
    if not m:
        raise LLMError(f"prompt {path} has no '<!-- version: ... -->' header")
    return Prompt(name=name, version=m.group(1), text=_VERSION_RE.sub("", text, count=1).strip())


def build_provider(settings: Settings) -> LLMProvider:
    provider = settings.llm.provider
    key = settings.secrets.llm_key(provider)
    if not key:
        raise LLMError(f"API key for llm.provider '{provider}' is not set")
    if provider == "openai":
        from ba_flow.llm.openai_provider import OpenAIProvider
        return OpenAIProvider(key, reasoning_effort=settings.llm.reasoning_effort)
    from ba_flow.llm.anthropic_provider import AnthropicProvider
    return AnthropicProvider(key)


class LLMService:
    def __init__(self, provider: LLMProvider, settings: Settings, store: StateStore):
        self._provider = provider
        self._settings = settings
        self._store = store

    def run(self, step: str, prompt: Prompt, user: str, schema: type[T]) -> LLMResult[T]:
        model = self._settings.llm.model_for(step)
        effort = self._settings.llm.effort_for(step)
        key = hashlib.sha256(
            "\x1f".join([self._provider.name, model, effort, prompt.name, prompt.version, schema.__name__, user]).encode()
        ).hexdigest()
        run_id = current_run_id()

        if (hit := self._store.llm_cache_get(key)) is not None:
            log(logger, 20, "llm cache hit", step=step, model=model, prompt=prompt.version)
            if run_id:
                self._store.update_run(run_id, cache_hit=1)
            return LLMResult(schema.model_validate(hit), LLMUsage(), model, cached=True)

        log(logger, 20, "llm call", step=step, model=model, effort=effort, prompt=prompt.version,
            input_chars=len(user))
        result = self._provider.parse(model=model, system=prompt.text, user=user, schema=schema,
                                      max_output_tokens=self._settings.llm.max_output_tokens, effort=effort)
        u = result.usage
        log(logger, 20, "llm done", step=step, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
            cache_read_tokens=u.cache_read_tokens)
        if run_id:
            self._store.add_run_usage(run_id, u.input_tokens, u.output_tokens, u.cache_read_tokens)
            self._store.update_run(run_id, model=model, prompt_version=prompt.version)
        self._store.record_llm_call(
            run_id=run_id, step=step, provider=self._provider.name, model=model, prompt_version=prompt.version,
            input_tokens=u.input_tokens, output_tokens=u.output_tokens, cache_read_tokens=u.cache_read_tokens,
            payload=json.dumps({"user": user, "output": result.parsed.model_dump()}, ensure_ascii=False)
            if self._settings.logging.store_llm_payloads else None,
        )
        self._store.llm_cache_set(key, result.parsed.model_dump())
        return result
