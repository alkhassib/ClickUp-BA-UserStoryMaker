from __future__ import annotations

import anthropic

from ba_flow.llm.base import LLMError, LLMResult, LLMUsage, T


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, client: anthropic.Anthropic | None = None):
        self._client = client or anthropic.Anthropic(api_key=api_key, max_retries=3)

    def parse(self, *, model: str, system: str, user: str, schema: type[T], max_output_tokens: int,
              effort: str | None = None) -> LLMResult[T]:
        # `effort` is an OpenAI reasoning setting; claude-haiku-4-5 does not take it.
        resp = self._client.messages.parse(
            model=model,
            max_tokens=max_output_tokens,
            # Stable instructions first and cached; the variable document/answers go in the user turn.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
        if resp.stop_reason == "max_tokens":
            raise LLMError("Anthropic response hit max_tokens; raise llm.max_output_tokens or shorten input")
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            raise LLMError(f"Anthropic returned no parsable output (stop_reason={resp.stop_reason})")
        u = resp.usage
        return LLMResult(
            parsed=resp.parsed_output,
            usage=LLMUsage(
                input_tokens=u.input_tokens + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0),
                output_tokens=u.output_tokens,
                cache_read_tokens=u.cache_read_input_tokens or 0,
            ),
            model=model,
        )
