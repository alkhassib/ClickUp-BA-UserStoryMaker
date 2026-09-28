"""Resolve configured ClickUp names to IDs, validate them, and cache the result.

The same inspection powers `ba-flow check-config` and the automatic check at startup:
if anything configured is missing in ClickUp, the app refuses to start.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

from ba_flow.clickup.client import ClickUpClient, ClickUpError
from ba_flow.core.config import Settings
from ba_flow.core.observability import get_logger, log
from ba_flow.store.state import StateStore

CACHE_KEY = "clickup_workspace"
LIST_KEYS = ("intake", "drafts", "stories")

logger = get_logger("resolver")

Level = Literal["ok", "warn", "fail"]


@dataclass
class Check:
    level: Level
    section: str
    message: str


@dataclass
class FieldInfo:
    id: str
    name: str
    type: str
    options: dict[str, str] = field(default_factory=dict)  # option name -> option id (dropdowns)


@dataclass
class ListInfo:
    key: str
    id: str
    name: str
    statuses: dict[str, str]  # status name (lower) -> status type
    fields: dict[str, FieldInfo]  # field name -> info


@dataclass
class Workspace:
    team_id: str
    space_id: str
    space_name: str
    bot_user_id: int
    bot_username: str
    lists: dict[str, ListInfo]

    def list_id(self, key: str) -> str:
        return self.lists[key].id

    def field(self, list_key: str, name: str) -> FieldInfo:
        return self.lists[list_key].fields[name]

    def option_id(self, list_key: str, field_name: str, option: str) -> str:
        return self.field(list_key, field_name).options[option]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Workspace":
        lists = {
            k: ListInfo(
                key=v["key"], id=v["id"], name=v["name"], statuses=v["statuses"],
                fields={fn: FieldInfo(**fi) for fn, fi in v["fields"].items()},
            )
            for k, v in d["lists"].items()
        }
        return cls(**{**d, "lists": lists})


class WorkspaceConfigError(Exception):
    def __init__(self, checks: list[Check]):
        self.checks = checks
        failures = "\n".join(f"  - [{c.section}] {c.message}" for c in checks if c.level == "fail")
        super().__init__(f"ClickUp workspace does not match settings.yaml:\n{failures}\n"
                         "Run `ba-flow check-config` for the full report.")


def _norm(name: str) -> str:
    return name.strip().lower()


def _find_by_name(items: list[dict], name: str) -> dict | None:
    return next((i for i in items if _norm(i["name"]) == _norm(name)), None)


def inspect_workspace(client: ClickUpClient, settings: Settings) -> tuple[Workspace | None, list[Check]]:
    """Fetch everything the app depends on from ClickUp and validate it against settings."""
    cfg = settings.clickup
    checks: list[Check] = []

    def ok(section: str, msg: str) -> None:
        checks.append(Check("ok", section, msg))

    def warn(section: str, msg: str) -> None:
        checks.append(Check("warn", section, msg))

    def fail(section: str, msg: str) -> None:
        checks.append(Check("fail", section, msg))

    # Token / bot identity
    try:
        user = client.get_user()
    except ClickUpError as e:
        fail("token", f"CLICKUP_API_TOKEN rejected by ClickUp (HTTP {e.status})")
        return None, checks
    bot_id, bot_name = int(user["id"]), user.get("username") or str(user["id"])
    if cfg.bot_user_id is not None and cfg.bot_user_id != bot_id:
        fail("token", f"token belongs to '{bot_name}' ({bot_id}), but clickup.bot_user_id is {cfg.bot_user_id}")
    else:
        ok("token", f"authenticated as '{bot_name}' ({bot_id}) — changes made by this user are treated as bot actions")

    # Team
    teams = client.get_teams()
    team = next((t for t in teams if str(t["id"]) == cfg.team_id), None)
    if not team:
        fail("team", f"team {cfg.team_id} not accessible with this token")
        return None, checks
    ok("team", f"team '{team['name']}' ({cfg.team_id})")

    # Space
    space = _find_by_name(client.get_spaces(cfg.team_id), cfg.space)
    if not space:
        fail("space", f"space '{cfg.space}' not found in team")
        return None, checks
    ok("space", f"space '{space['name']}' ({space['id']})")

    # Lists (folderless and inside folders)
    all_lists = list(client.get_folderless_lists(space["id"]))
    for folder in client.get_folders(space["id"]):
        all_lists.extend(folder.get("lists", []))

    lists: dict[str, ListInfo] = {}
    for key in LIST_KEYS:
        name = getattr(cfg.lists, key)
        found = _find_by_name(all_lists, name)
        if not found:
            fail(f"list:{key}", f"list '{name}' not found in space '{cfg.space}'")
            continue
        detail = client.get_list(found["id"])
        statuses = {_norm(s["status"]): s["type"] for s in detail.get("statuses", [])}
        fields = {
            f["name"]: FieldInfo(
                id=f["id"], name=f["name"], type=f["type"],
                options={o["name"]: o["id"] for o in (f.get("type_config") or {}).get("options", [])
                         if o.get("name")},
            )
            for f in client.get_list_fields(found["id"])
        }
        lists[key] = ListInfo(key=key, id=found["id"], name=found["name"], statuses=statuses, fields=fields)
        ok(f"list:{key}", f"'{found['name']}' ({found['id']})")

        missing = [f"{k} = '{v}'" for k, v in cfg.statuses.for_list(key).items() if _norm(v) not in statuses]
        if missing:
            fail(f"list:{key}", "missing statuses: " + ", ".join(missing))
        else:
            ok(f"list:{key}", f"{len(cfg.statuses.for_list(key))} configured statuses present")

    _check_fields(settings, lists, ok, warn, fail)

    if any(c.level == "fail" for c in checks):
        return None, checks
    return Workspace(
        team_id=cfg.team_id, space_id=space["id"], space_name=space["name"],
        bot_user_id=bot_id, bot_username=bot_name, lists=lists,
    ), checks


def _check_fields(settings: Settings, lists: dict[str, ListInfo], ok, warn, fail) -> None:
    f = settings.clickup.fields

    if drafts := lists.get("drafts"):
        so = f.drafts.selected_option
        field_ = drafts.fields.get(so.name)
        if not field_:
            fail("fields:drafts", f"dropdown field '{so.name}' not found")
        elif field_.type != "drop_down":
            fail("fields:drafts", f"'{so.name}' must be a dropdown, found '{field_.type}'")
        else:
            missing = [o for o in so.all_options if o not in field_.options]
            if missing:
                fail("fields:drafts", f"'{so.name}' is missing options: {missing}")
            else:
                ok("fields:drafts", f"'{so.name}' has all {len(so.all_options)} options")
        for name in (f.drafts.custom_answer, f.drafts.justification, f.drafts.source_key):
            if name in drafts.fields:
                ok("fields:drafts", f"'{name}' ({drafts.fields[name].type})")
            else:
                fail("fields:drafts", f"field '{name}' not found")

    if stories := lists.get("stories"):
        if f.stories.source_key in stories.fields:
            ok("fields:stories", f"'{f.stories.source_key}' ({stories.fields[f.stories.source_key].type})")
        else:
            fail("fields:stories", f"field '{f.stories.source_key}' not found")
        for key, name in f.stories.metadata.items():
            if name in stories.fields:
                ok("fields:stories", f"{key} -> '{name}' ({stories.fields[name].type})")
            else:
                # Metadata is optional: the story is still created, the field is just left empty.
                warn("fields:stories", f"optional field '{name}' ({key}) not found — it will be skipped")


def local_checks(settings: Settings) -> list[Check]:
    """Checks that do not need the network."""
    checks: list[Check] = []
    if settings.secrets.clickup_token:
        checks.append(Check("ok", "env", "CLICKUP_API_TOKEN is set"))
    else:
        checks.append(Check("fail", "env", "CLICKUP_API_TOKEN is not set (.env)"))

    provider = settings.llm.provider
    if settings.secrets.llm_key(provider):
        checks.append(Check("ok", "env", f"API key for llm.provider '{provider}' is set"))
    else:
        checks.append(Check("warn", "env", f"API key for llm.provider '{provider}' is not set "
                                           "(needed from phase 2: analysis)"))

    if settings.trigger.mode in ("webhook", "hybrid"):
        if settings.secrets.webhook_secret:
            checks.append(Check("ok", "env", f"CLICKUP_WEBHOOK_SECRET is set (trigger.mode '{settings.trigger.mode}')"))
        else:
            checks.append(Check("fail", "env", f"trigger.mode is '{settings.trigger.mode}' but CLICKUP_WEBHOOK_SECRET "
                                               "is not set (`ba-flow webhook register --url ... --save`)"))

    lang = settings.output.user_story_language
    for code, rel in settings.output.templates.items():
        exists = settings.path(rel).exists()
        level: Level = "ok" if exists else ("fail" if code == lang else "warn")
        mark = " (active)" if code == lang else ""
        checks.append(Check(level, "templates", f"{code}{mark}: {rel}" + ("" if exists else " — file not found")))
    return checks


class Resolver:
    def __init__(self, client: ClickUpClient, settings: Settings, store: StateStore):
        self._client = client
        self._settings = settings
        self._store = store
        self._workspace: Workspace | None = None
        self.last_checks: list[Check] = []

    def load(self, force: bool = False) -> Workspace:
        if self._workspace and not force:
            return self._workspace
        if not force:
            cached = self._store.cache_get(CACHE_KEY, self._settings.config_hash)
            if cached:
                self._workspace = Workspace.from_dict(cached)
                return self._workspace
        return self.refresh()

    def refresh(self) -> Workspace:
        """Re-inspect ClickUp. Raises WorkspaceConfigError if anything configured is missing."""
        workspace, checks = inspect_workspace(self._client, self._settings)
        self.last_checks = checks
        for c in checks:
            if c.level != "ok":
                log(logger, 30 if c.level == "warn" else 40, c.message, section=c.section)
        if workspace is None:
            self._workspace = None
            self._store.cache_delete(CACHE_KEY)
            raise WorkspaceConfigError(checks)
        self._store.cache_set(CACHE_KEY, workspace.to_dict(), self._settings.config_hash,
                              self._settings.storage.cache_ttl_seconds)
        self._workspace = workspace
        log(logger, 20, "clickup workspace resolved", space=workspace.space_name, bot=workspace.bot_username)
        return workspace

    def invalidate(self) -> None:
        """Call when ClickUp rejects an ID/name we resolved (e.g. HTTP 400/404)."""
        self._workspace = None
        self._store.cache_delete(CACHE_KEY)
