"""Configuration, read from environment variables (and a .env file if present)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv is optional
    pass

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    database_path: Path = ROOT / "data" / "sample.db"
    llm_model: str = "openai:gpt-4o-mini"
    llm_temperature: float = 0.0
    max_result_rows: int = 200          # rows shown in the UI per query
    max_export_rows: int = 10_000       # rows written to a CSV export
    query_timeout_seconds: float = 5.0
    max_sql_attempts: int = 3           # first try + 2 automatic repairs
    history_turns: int = 6              # conversation turns sent to the LLM
    auto_execute: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            database_path=Path(env("DATABASE_PATH", str(cls.database_path))),
            llm_model=env("LLM_MODEL", cls.llm_model),
            llm_temperature=float(env("LLM_TEMPERATURE", cls.llm_temperature)),
            max_result_rows=int(env("MAX_RESULT_ROWS", cls.max_result_rows)),
            max_export_rows=int(env("MAX_EXPORT_ROWS", cls.max_export_rows)),
            query_timeout_seconds=float(env("QUERY_TIMEOUT_SECONDS", cls.query_timeout_seconds)),
            max_sql_attempts=int(env("MAX_SQL_ATTEMPTS", cls.max_sql_attempts)),
            history_turns=int(env("HISTORY_TURNS", cls.history_turns)),
            auto_execute=env("AUTO_EXECUTE", "true").lower() in ("1", "true", "yes"),
        )
