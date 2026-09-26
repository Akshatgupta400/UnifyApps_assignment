"""Shared fixtures: a fresh sample database in a temp directory per test run."""
from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path

from app.config import Settings
from app.db.introspect import Schema, load_schema
from app.db.seed import build_database


@lru_cache(maxsize=1)
def sample_db() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="sql-agent-test-"))
    return build_database(directory / "sample.db")


@lru_cache(maxsize=1)
def sample_schema() -> Schema:
    return load_schema(sample_db())


def settings(**overrides) -> Settings:
    return Settings(database_path=sample_db(), **overrides)
