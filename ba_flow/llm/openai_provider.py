from __future__ import annotations

from openai import OpenAI

from ba_flow.llm.base import LLMError, LLMResult, LLMUsage, T


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, reasoning_effort: str = "low", client: OpenAI | None = None):
        self._client = client or OpenAI(api_key=api_key, max_retries=3, timeout=300)
        self._effort = reasoning_effort

    def parse(self, *, model: str, system: str, user: str, schema: type[T], max_output_tokens: int,
              effort: str | None = None) -> LLMResult[T]:
        kwargs = {}
        if model.startswith(("gpt-5", "o")):
            kwargs["reasoning"] = {"effort": effort or self._effort}
        resp = self._client.responses.parse(
            model=model,
            instructions=system,
            input=user,
            text_format=schema,
            max_output_tokens=max_output_tokens,
            **kwargs,
        )
        if resp.status == "incomplete":
            reason = getattr(resp.incomplete_details, "reason", "unknown")
            raise LLMError(f"OpenAI response incomplete ({reason}); raise llm.max_output_tokens or shorten input")
        if resp.output_parsed is None:
            raise LLMError("OpenAI returned no parsable output (refusal or empty response)")
        u = resp.usage
        cached = getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0
        return LLMResult(
            parsed=resp.output_parsed,
            usage=LLMUsage(input_tokens=u.input_tokens, output_tokens=u.output_tokens, cache_read_tokens=cached),
            model=model,
        )
