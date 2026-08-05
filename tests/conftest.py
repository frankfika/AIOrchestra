"""pytest configuration for the P0 test suite.

The smoke tests (default ``pytest``) must not require PostgreSQL; the
e2e tests (marked ``@pytest.mark.e2e``) are skipped when the DB is
unreachable so a developer without Postgres still sees a green smoke
run.
"""
from __future__ import annotations

import os
import uuid

import pytest


def _db_reachable(dsn: str) -> bool:
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=2) as c:
            with c.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception:  # noqa: BLE001
        return False


DEFAULT_DSN = "postgresql://orchestra:orchestra@127.0.0.1:5432/orchestra"


@pytest.fixture(scope="session")
def dsn() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DSN)


@pytest.fixture(scope="session")
def db_available(dsn: str) -> bool:
    return _db_reachable(dsn)


@pytest.fixture
def fresh_task_run_id() -> str:
    return f"trun-{uuid.uuid4().hex[:10]}"


@pytest.fixture
def fresh_node_run_id() -> str:
    return f"nrun-{uuid.uuid4().hex[:10]}"


def pytest_collection_modifyitems(config, items):
    skip_e2e = pytest.mark.skip(reason="PostgreSQL not reachable; e2e skipped")
    for item in items:
        if "e2e" in item.keywords:
            dsn = os.environ.get("DATABASE_URL", DEFAULT_DSN)
            if not _db_reachable(dsn):
                item.add_marker(skip_e2e)
