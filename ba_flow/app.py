"""Wiring: builds the shared objects every command needs."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ba_flow.clickup.client import ClickUpClient
from ba_flow.clickup.resolver import Resolver, Workspace
from ba_flow.clickup.writer import ClickUpWriter
from ba_flow.core.config import ConfigError, Settings, load_settings
from ba_flow.core.observability import setup_logging
from ba_flow.store.state import StateStore


@dataclass
class App:
    settings: Settings
    store: StateStore
    client: ClickUpClient
    resolver: Resolver
    writer: ClickUpWriter

    def startup_check(self) -> Workspace:
        """Automatic check-config: uses the cache when settings are unchanged, re-inspects otherwise."""
        return self.resolver.load()

    def close(self) -> None:
        self.client.close()
        self.store.close()


def build_app(config_path: Path | str | None = None, dry_run: bool = False, log_level: str | None = None) -> App:
    settings = load_settings(config_path)
    log_file = settings.path(settings.logging.file) if settings.logging.file else None
    setup_logging(log_level or settings.logging.level, log_file)
    if not settings.secrets.clickup_token:
        raise ConfigError("CLICKUP_API_TOKEN is not set (.env)")
    store = StateStore(settings.path(settings.storage.db_path))
    client = ClickUpClient(settings.secrets.clickup_token)
    return App(
        settings=settings,
        store=store,
        client=client,
        resolver=Resolver(client, settings, store),
        writer=ClickUpWriter(client, store, dry_run=dry_run),
    )
