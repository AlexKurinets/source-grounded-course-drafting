"""Route each model call by the sensitivity of what it sends, and log it before it is made."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TypeVar

from psycopg_pool import ConnectionPool

from .. import store
from ..clock import Clock
from ..config import Settings
from .hosted import AnthropicDrafter, OpenAICompatibleDrafter
from .offline import OfflineDrafter

T = TypeVar("T")
SENSITIVITY_ORDER = {"public": 0, "internal": 1, "restricted": 2}
LOCAL_ADAPTERS = {"offline", "ollama"}


class ConfigError(Exception):
    pass


def make_drafter(kind: str, model: str, settings: Settings):
    if kind == "offline":
        return OfflineDrafter(fault_every=settings.fault_every)
    if kind == "anthropic":
        return AnthropicDrafter(model)
    if kind == "openai":
        return OpenAICompatibleDrafter(
            model, settings.openai_base_url, os.environ.get("OPENAI_API_KEY", "")
        )
    if kind == "ollama":
        return OpenAICompatibleDrafter(model, settings.ollama_url, adapter="ollama")
    raise ConfigError(f"unknown adapter {kind!r}")


class Router:
    """Public and internal material may go to the hosted model, which runs under zero-retention
    API terms. Anything restricted (instructors' notes from client engagements) only goes to the
    local model. The audit row is written first, so a failed call is still on record."""

    def __init__(self, hosted, local, pool: ConnectionPool, clock: Clock) -> None:
        self.hosted = hosted
        self.local = local
        self.pool = pool
        self.clock = clock

    @classmethod
    def from_settings(cls, settings: Settings, pool: ConnectionPool, clock: Clock) -> Router:
        if settings.local_adapter not in LOCAL_ADAPTERS:
            raise ConfigError(
                "the local route must stay on this machine; "
                f"{settings.local_adapter!r} is a hosted service "
                f"(use one of {sorted(LOCAL_ADAPTERS)})"
            )
        hosted = make_drafter(settings.hosted_adapter, settings.hosted_model, settings)
        local = make_drafter(settings.local_adapter, settings.local_model, settings)
        return cls(hosted, local, pool, clock)

    def call(
        self,
        run_id: str,
        step: str,
        chunks: list[dict],
        fn: Callable[[object], T],
        sensitivity: str | None = None,
        extra_ids: list[str] | None = None,
    ) -> T:
        levels = [c["sensitivity"] for c in chunks] + ([sensitivity] if sensitivity else [])
        level = max(levels, key=SENSITIVITY_ORDER.__getitem__, default="public")
        route = "local" if level == "restricted" else "hosted"
        drafter = self.local if route == "local" else self.hosted
        store.log_model_call(
            self.pool,
            run_id=run_id,
            step=step,
            route=route,
            adapter=drafter.adapter,
            model=drafter.model,
            sensitivity=level,
            chunk_ids=[c["id"] for c in chunks] + (extra_ids or []),
            chars_sent=sum(len(c["title"]) + len(c["body"]) for c in chunks),
            created_at=self.clock.now(),
        )
        return fn(drafter)
