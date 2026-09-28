"""Settings loading: config/settings.yaml (structure) + .env (secrets)."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # only meaningful for a source checkout / editable install
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "settings.yaml"


def default_config_path() -> Path:
    """Where settings.yaml is looked for when --config is not given.

    1. $BA_FLOW_CONFIG  2. ./config/settings.yaml (working directory — how a server runs it)
    3. the source checkout. A regular `pip install .` puts the code in site-packages, so (3) alone is not enough.
    """
    if env := os.getenv("BA_FLOW_CONFIG"):
        return Path(env)
    cwd = Path.cwd() / "config" / "settings.yaml"
    return cwd if cwd.exists() else DEFAULT_CONFIG_PATH

Language = Literal["ar", "en"]


class ConfigError(Exception):
    """Raised when settings are missing or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListNames(_Strict):
    intake: str
    drafts: str
    stories: str


class IntakeStatuses(_Strict):
    new: str
    to_analyze: str
    analyzing: str
    analyzed: str
    failed: str
    done: str


class DtStatuses(_Strict):
    draft: str
    ba_review: str
    submitted: str
    compiled: str
    failed: str


class GapStatuses(_Strict):
    open: str
    closed: str
    compiled: str


class StoryStatuses(_Strict):
    new: str


class Statuses(_Strict):
    intake: IntakeStatuses
    dt: DtStatuses
    gap: GapStatuses
    story: StoryStatuses

    def for_list(self, list_key: str) -> dict[str, str]:
        """Configured status names that must exist on a given list, keyed by '<group>.<name>'."""
        groups = {
            "intake": ["intake"],
            "drafts": ["dt", "gap"],
            "stories": ["story"],
        }[list_key]
        out: dict[str, str] = {}
        for group in groups:
            for key, value in getattr(self, group).model_dump().items():
                out[f"{group}.{key}"] = value
        return out


class SelectedOptionField(_Strict):
    name: str
    letters: list[str] = Field(min_length=1, max_length=10)
    tbd: str
    other: str
    not_applicable: str

    @property
    def all_options(self) -> list[str]:
        return [*self.letters, self.tbd, self.other, self.not_applicable]

    @model_validator(mode="after")
    def _unique(self) -> "SelectedOptionField":
        opts = self.all_options
        if len(set(opts)) != len(opts):
            raise ValueError(f"selected_option values must be unique, got {opts}")
        return self


class DraftFields(_Strict):
    selected_option: SelectedOptionField
    custom_answer: str
    justification: str
    source_key: str


class StoryFields(_Strict):
    source_key: str
    metadata: dict[str, str] = Field(default_factory=dict)


class Fields(_Strict):
    drafts: DraftFields
    stories: StoryFields


class ClickUpSettings(_Strict):
    team_id: str
    space: str
    bot_user_id: int | None = None
    lists: ListNames
    statuses: Statuses
    fields: Fields


class OutputSettings(_Strict):
    user_story_language: Language = "en"
    templates: dict[Language, str]
    empty_mandatory: Literal["mark", "omit"] = "mark"
    empty_optional: Literal["mark", "omit"] = "omit"
    include_decisions: bool = True

    @model_validator(mode="after")
    def _template_for_language(self) -> "OutputSettings":
        if self.user_story_language not in self.templates:
            raise ValueError(f"no template configured for language '{self.user_story_language}'")
        return self


class LLMSettings(_Strict):
    provider: Literal["anthropic", "openai"]
    models: dict[Literal["anthropic", "openai"], str]
    steps: dict[str, str] = Field(default_factory=dict)
    max_doc_tokens: int = Field(default=100000, gt=0)
    max_output_tokens: int = Field(default=32000, gt=0)
    reasoning_effort: Literal["minimal", "low", "medium", "high"] = "low"
    step_effort: dict[str, Literal["minimal", "low", "medium", "high"]] = Field(default_factory=dict)
    max_gaps: int = Field(default=15, ge=1, le=50)

    def model_for(self, step: str) -> str:
        return self.steps.get(step) or self.models[self.provider]

    def effort_for(self, step: str) -> str:
        return self.step_effort.get(step) or self.reasoning_effort


class WebhookSettings(_Strict):
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    path: str = "/clickup/webhook"
    # Wait this long after a status change before acting, then re-check the status: a BA who clicks
    # `submitted` and immediately changes their mind is not processed.
    debounce_seconds: int = Field(default=10, ge=0)


class TriggerSettings(_Strict):
    mode: Literal["polling", "webhook", "hybrid"] = "polling"
    interval_seconds: int = Field(default=60, ge=10)          # polling mode
    safety_poll_seconds: int = Field(default=900, ge=60)      # hybrid mode: slow poll that catches missed events
    webhook: WebhookSettings = WebhookSettings()


class StorageSettings(_Strict):
    db_path: str = "data/ba_flow.db"
    cache_ttl_seconds: int = Field(default=3600, ge=0)


class LoggingSettings(_Strict):
    level: str = "INFO"
    file: str | None = "logs/ba_flow.jsonl"
    store_llm_payloads: bool = False


class Secrets(BaseModel):
    clickup_token: str | None = None
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    webhook_secret: str | None = None

    def llm_key(self, provider: str) -> str | None:
        return {"anthropic": self.anthropic_api_key, "openai": self.openai_api_key}[provider]


class Settings(_Strict):
    clickup: ClickUpSettings
    output: OutputSettings
    llm: LLMSettings
    trigger: TriggerSettings = TriggerSettings()
    storage: StorageSettings = StorageSettings()
    logging: LoggingSettings = LoggingSettings()

    # Filled by load_settings, not read from YAML.
    secrets: Secrets = Field(default_factory=Secrets, exclude=True)
    config_hash: str = Field(default="", exclude=True)
    root: Path = Field(default=PROJECT_ROOT, exclude=True)

    def path(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else self.root / p


def _format_validation_error(err: ValidationError) -> str:
    lines = []
    for e in err.errors():
        loc = ".".join(str(x) for x in e["loc"])
        lines.append(f"  - {loc}: {e['msg']}")
    return "Invalid settings.yaml:\n" + "\n".join(lines)


def load_settings(config_path: Path | str | None = None, env_file: Path | str | None = None) -> Settings:
    path = Path(config_path) if config_path else default_config_path()
    if not path.exists():
        raise ConfigError(f"Settings file not found: {path}. Run from the project directory (the one containing "
                          "config/), pass --config, or set BA_FLOW_CONFIG.")

    root = path.resolve().parent.parent
    load_dotenv(Path(env_file) if env_file else root / ".env", override=False)

    raw = path.read_bytes()
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"settings.yaml is not valid YAML: {e}") from e

    if team_override := os.getenv("CLICKUP_TEAM_ID"):
        data.setdefault("clickup", {})["team_id"] = team_override

    try:
        settings = Settings.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(e)) from e

    settings.secrets = Secrets(
        clickup_token=os.getenv("CLICKUP_API_TOKEN") or None,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        webhook_secret=os.getenv("CLICKUP_WEBHOOK_SECRET") or None,
    )
    # The hash covers the YAML, the team override and the token (bot identity), so any change
    # invalidates cached IDs.
    token_fp = hashlib.sha256((settings.secrets.clickup_token or "").encode()).hexdigest()[:8]
    settings.config_hash = hashlib.sha256(
        raw + settings.clickup.team_id.encode() + token_fp.encode()
    ).hexdigest()[:16]
    settings.root = root
    return settings
