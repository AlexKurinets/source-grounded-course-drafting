"""Write the synthetic courses to disk, ingest them, and leave the system in a state worth looking
at: one deck approved and exported, the same course redrafted after its standards update changed,
two more drafts waiting for review, and one returned with notes."""

from __future__ import annotations

from datetime import UTC, datetime

from . import store
from .clock import FrozenClock
from .ingest import ingest_file, put_fact_record, upsert_course
from .runtime import Runtime
from .synthetic import COURSES, LEASES, _course_meta, write_course_files, write_standards

SEED_START = datetime(2026, 10, 19, 8, 30, tzinfo=UTC)


def ingest_courses(rt: Runtime) -> int:
    root = rt.settings.sources_dir
    count = 0
    for course in COURSES:
        upsert_course(rt.pool, _course_meta(course))
        put_fact_record(rt.pool, course["id"], course["fact_record"])
        for path, source_type in write_course_files(course, root):
            ingest_file(rt.pool, rt.embedder, rt.clock, course["id"], path, source_type)
            count += 1
    return count


def seed(rt: Runtime, demo_runs: bool = True) -> dict:
    clock = rt.clock
    files = ingest_courses(rt)
    if not demo_runs:
        return {"files": files, "runs": 0}

    def tick(**kw):
        if isinstance(clock, FrozenClock):
            clock.advance(**kw)

    tick(hours=1)
    first = rt.start_run(LEASES["id"])
    tick(hours=20)
    rt.decide(
        first["id"],
        "approved",
        "Dana Whitfield",
        "Checked the citations against the standards update.",
    )

    # The standards update is reissued with a new paragraph. Re-ingesting it creates version 2;
    # the next run drafts against it and the review page shows it beside the approved deck.
    tick(days=2)
    path = write_standards(LEASES, rt.settings.sources_dir, version=2)
    ingest_file(rt.pool, rt.embedder, rt.clock, LEASES["id"], path, "standard")
    tick(hours=1)
    rt.start_run(LEASES["id"])

    tick(hours=2)
    rt.start_run("revenue-recognition-refresher")
    tick(hours=1)
    returned = rt.start_run("impairment-essentials")
    tick(hours=5)
    rt.decide(
        returned["id"],
        "returned",
        "Marcus Lindqvist",
        "Swap the bottling example on slide 5 for a manufacturing client before this goes out.",
    )
    return {"files": files, "runs": len(store.list_runs(rt.pool))}


def is_seeded(rt: Runtime) -> bool:
    return bool(store.list_courses(rt.pool))


def default_seed_clock() -> FrozenClock:
    return FrozenClock(SEED_START)
