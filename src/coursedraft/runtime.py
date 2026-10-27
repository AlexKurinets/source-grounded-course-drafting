from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langgraph.types import Command
from psycopg_pool import ConnectionPool

from . import store
from .clock import Clock
from .config import Settings
from .db import create_schema, make_pool
from .embed import Embedder, HashingEmbedder
from .llm import Router


class Conflict(Exception):
    pass


@dataclass
class Runtime:
    settings: Settings
    pool: ConnectionPool
    clock: Clock
    embedder: Embedder
    router: Router
    graph: Any = None

    @classmethod
    def build(cls, settings: Settings, clock: Clock | None = None, drafter=None) -> Runtime:
        """Open the pool, make sure the schema exists and compile the graph.

        ``drafter`` replaces both routes with one drafter, which is how the tests inject a
        drafter with a fixed failure pattern.
        """
        from .workflow import build_graph

        pool = make_pool(settings.database_url)
        try:
            create_schema(pool, settings.embed_dim)
            clock = clock or Clock()
            router = (
                Router(drafter, drafter, pool, clock)
                if drafter is not None
                else Router.from_settings(settings, pool, clock)
            )
        except Exception:
            pool.close()
            raise
        rt = cls(settings, pool, clock, HashingEmbedder(settings.embed_dim), router)
        rt.graph = build_graph(rt)
        return rt

    def close(self) -> None:
        self.pool.close()

    # -- the operations the API, the CLI and the seed script share -------------------------
    def start_run(self, course_id: str, kind: str = "course") -> dict:
        """Draft a course up to the approval gate and return the run."""
        store.get_course(self.pool, course_id)
        run_id = store.next_run_id(self.pool, course_id, kind)
        store.create_run(self.pool, run_id, course_id, kind, self.clock.now())
        try:
            self.graph.invoke({"run_id": run_id, "course_id": course_id}, _config(run_id))
        except Exception:
            store.set_status(self.pool, run_id, "failed", self.clock.now())
            raise
        return store.get_run(self.pool, run_id)

    def decide(self, run_id: str, decision: str, reviewer: str, notes: str = "") -> dict:
        """Resume a run paused at the approval gate. Approval exports; a return ends the run."""
        run = store.get_run(self.pool, run_id)
        if run["status"] != "awaiting_approval":
            raise Conflict(f"run {run_id} is {run['status']}, not awaiting approval")
        if decision not in ("approved", "returned"):
            raise ValueError("decision must be 'approved' or 'returned'")
        if not reviewer.strip():
            raise ValueError("a reviewer name is required")
        resume = {"decision": decision, "reviewer": reviewer.strip(), "notes": notes}
        self.graph.invoke(Command(resume=resume), _config(run_id))
        return store.get_run(self.pool, run_id)


def _config(run_id: str) -> dict:
    return {"configurable": {"thread_id": run_id}}
