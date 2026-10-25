"""Run the whole suite against either storage backend.

    python -m pytest -q                                   # files (default)
    MNEMOSYNE_STORAGE=postgres \\
    MNEMOSYNE_TEST_DATABASE_URL=postgresql://user:pw@localhost/db \\
    python -m pytest -q                                   # Postgres + pgvector

In Postgres mode every test store gets its own workspace (derived from its
temp directory) inside a schema created for this session and dropped after.
Tests that inspect the file layout itself are marked `files_only`.
"""

import os
import uuid

import pytest

PG = os.environ.get("MNEMOSYNE_STORAGE") == "postgres"


def pytest_configure(config):
    config.addinivalue_line("markers", "files_only: inspects the file backend's on-disk layout")
    if PG:
        url = os.environ.get("MNEMOSYNE_TEST_DATABASE_URL")
        if not url:
            raise pytest.UsageError("MNEMOSYNE_STORAGE=postgres needs MNEMOSYNE_TEST_DATABASE_URL")
        os.environ["MNEMOSYNE_DATABASE_URL"] = url
        os.environ["MNEMOSYNE_PG_SCHEMA"] = f"mnemo_test_{uuid.uuid4().hex[:10]}"
        os.environ["MNEMOSYNE_WORKSPACE"] = "@root"


def pytest_collection_modifyitems(config, items):
    if PG:
        skip = pytest.mark.skip(reason="file-backend layout test")
        for item in items:
            if "files_only" in item.keywords:
                item.add_marker(skip)


def pytest_unconfigure(config):
    if PG:
        import psycopg
        with psycopg.connect(os.environ["MNEMOSYNE_DATABASE_URL"], autocommit=True) as c:
            c.execute(f'DROP SCHEMA IF EXISTS {os.environ["MNEMOSYNE_PG_SCHEMA"]} CASCADE')
