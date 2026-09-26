"""The state that flows through the LangGraph workflow.

Two kinds of fields:
- Conversation memory (``history``, ``last_sql``) persists across turns through
  the LangGraph checkpointer, keyed by the session's thread_id. This is what
  lets "Only those from California" modify the previous query.
- Per-turn fields are reset at the start of every turn by ``new_turn``.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class AgentState(TypedDict, total=False):
    # ---- input ----------------------------------------------------------------
    user_input: str
    execute: bool                  # run the validated query against the database?

    # ---- per-turn working fields ---------------------------------------------
    block_reason: str              # set by the input guard: injection, destructive, ...
    intent: str
    intent_reason: str
    clarifying_question: str
    user_sql: str                  # SQL the user supplied (debug / optimize / explain)
    engine_report: str             # what SQLite said about the user's SQL
    schema_context: str
    sql: str                       # current candidate query
    notes: dict[str, Any]          # assumptions / issues / changes / index suggestions
    cannot_answer: str
    validation: dict[str, Any]
    attempts: int
    optimization: dict[str, Any]
    results: dict[str, Any]
    explanation: str
    answer: str                    # text answers: greeting, schema info, SQL concepts
    error: str
    response: dict[str, Any]       # final payload returned to the API

    # ---- conversation memory (checkpointed) -----------------------------------
    history: Annotated[list[dict[str, Any]], operator.add]
    last_sql: str


PER_TURN_DEFAULTS: dict[str, Any] = {
    "block_reason": "", "intent": "", "intent_reason": "", "clarifying_question": "",
    "user_sql": "", "engine_report": "", "schema_context": "", "sql": "", "notes": {},
    "cannot_answer": "", "validation": {}, "attempts": 0, "optimization": {}, "results": {},
    "explanation": "", "answer": "", "error": "", "response": {},
}


def new_turn(user_input: str, execute: bool = True) -> dict[str, Any]:
    """Graph input for a new user turn: the message plus reset per-turn fields."""
    return {"user_input": user_input, "execute": execute, **PER_TURN_DEFAULTS}
