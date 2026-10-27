from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coursedraft.clock import FrozenClock
from coursedraft.config import Settings
from coursedraft.db import reset_db
from coursedraft.runtime import Runtime
from coursedraft.synthetic import LEASES

from .helpers import ingest_course

TEST_DB = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5452/coursedraft_test"
)
REPO = Path(__file__).resolve().parents[1]
START = datetime(2026, 10, 19, 8, 30, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=TEST_DB,
        data_dir=tmp_path / "data",
        template_dir=REPO / "templates",
        fault_every=0,
    )


@pytest.fixture
def make_rt(settings: Settings):
    """A fresh database per test. ``make_rt()`` can be called again to get a second runtime on
    the same database, which is how the tests resume a run from another process."""
    reset_db(settings.database_url)
    made: list[Runtime] = []

    def make(drafter=None, clock=None, **overrides) -> Runtime:
        s = settings.with_(**overrides) if overrides else settings
        rt = Runtime.build(s, clock or FrozenClock(START), drafter=drafter)
        made.append(rt)
        return rt

    yield make
    for rt in made:
        rt.close()


@pytest.fixture
def rt(make_rt) -> Runtime:
    return make_rt()


@pytest.fixture
def leases(rt: Runtime) -> Runtime:
    ingest_course(rt, LEASES)
    return rt
