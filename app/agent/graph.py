"""The LangGraph workflow.

    START
      │
    guard_input ──(blocked)──────────────────────────────► refuse ──┐
      │                                                             │
    classify_intent ──(out_of_scope / destructive)───────► refuse   │
      │   ├──(ambiguous)─────────────────────────────────► clarify ─┤
      │   ├──(greeting / schema_info / sql_concept)──────► answer_info
      │   └──(LLM error)─────────────────────────────────► respond  │
      ▼                                                             │
    retrieve_schema                                                 │
      ├──(generate / refine)──► generate_sql ─┐                     │
      └──(explain/debug/optimize)► review_sql ┤                     │
                                              ▼                     │
                     ┌─────────────────── validate_sql              │
      (invalid, retry│left) ◄──────────────┤ ├─(write detected)► refuse
      back to generate_sql / review_sql    │ └─(retries used up)► respond
                                           ▼                        │
                                        optimize                    │
                                           │                        │
                              (execute on) ├──► execute_sql         │
                                           ▼         │              │
                                        explain ◄────┘              │
                                           │                        │
                                        respond ◄───────────────────┘
                                           │
                                          END

Conversation memory lives in the checkpointer: every session is a LangGraph
thread, so ``history`` and ``last_sql`` survive between turns.
"""
from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

try:
    from langgraph.checkpoint.memory import InMemorySaver
except ImportError:  # older langgraph releases
    from langgraph.checkpoint.memory import MemorySaver as InMemorySaver

from app.config import Settings
from app.db.introspect import Schema
from .llm import LLMClient
from .nodes import INFO_INTENTS, REVIEW_INTENTS, SQL_INTENTS, AgentNodes
from .state import AgentState

# Human-readable labels, streamed to the UI as each node finishes.
NODE_LABELS = {
    "guard_input": "Checking the request",
    "classify_intent": "Detecting intent and scope",
    "refuse": "Declining out-of-scope request",
    "clarify": "Asking for clarification",
    "answer_info": "Answering",
    "retrieve_schema": "Retrieving the schema",
    "generate_sql": "Generating SQL",
    "review_sql": "Reviewing your SQL",
    "validate_sql": "Validating SQL",
    "optimize": "Checking performance",
    "execute_sql": "Running the query",
    "explain": "Explaining the query",
    "respond": "Preparing the answer",
}


def build_graph(llm: LLMClient | None, schema: Schema, settings: Settings,
                checkpointer: Any | None = None, llm_error: str = ""):
    nodes = AgentNodes(llm, schema, settings, llm_error=llm_error)
    g = StateGraph(AgentState)

    g.add_node("guard_input", nodes.guard_input)
    g.add_node("classify_intent", nodes.classify_intent)
    g.add_node("refuse", nodes.refuse)
    g.add_node("clarify", nodes.clarify)
    g.add_node("answer_info", nodes.answer_info)
    g.add_node("retrieve_schema", nodes.retrieve_schema)
    g.add_node("generate_sql", nodes.generate_sql)
    g.add_node("review_sql", nodes.review_sql)
    g.add_node("validate_sql", nodes.validate)
    g.add_node("optimize", nodes.optimize)
    g.add_node("execute_sql", nodes.execute)
    g.add_node("explain", nodes.explain)
    g.add_node("respond", nodes.respond)

    g.add_edge(START, "guard_input")

    def after_guard(state: AgentState) -> str:
        return "refuse" if state.get("block_reason") else "classify_intent"

    g.add_conditional_edges("guard_input", after_guard, ["refuse", "classify_intent"])

    def after_intent(state: AgentState) -> str:
        intent = state.get("intent")
        if intent == "error":
            return "respond"
        if intent in ("out_of_scope", "destructive"):
            return "refuse"
        if intent == "ambiguous":
            return "clarify"
        if intent in INFO_INTENTS:
            return "answer_info"
        return "retrieve_schema"

    g.add_conditional_edges("classify_intent", after_intent,
                            ["respond", "refuse", "clarify", "answer_info", "retrieve_schema"])

    def after_schema(state: AgentState) -> str:
        return "generate_sql" if state.get("intent") in SQL_INTENTS else "review_sql"

    g.add_conditional_edges("retrieve_schema", after_schema, ["generate_sql", "review_sql"])

    def after_draft(state: AgentState) -> str:
        if state.get("error") or state.get("cannot_answer"):
            return "respond"
        return "validate_sql"

    g.add_conditional_edges("generate_sql", after_draft, ["respond", "validate_sql"])
    g.add_conditional_edges("review_sql", after_draft, ["respond", "validate_sql"])

    def after_validation(state: AgentState) -> str:
        validation = state.get("validation") or {}
        if validation.get("ok"):
            return "optimize"
        if validation.get("destructive"):
            return "refuse"
        if state.get("attempts", 0) >= settings.max_sql_attempts:
            return "respond"
        return "review_sql" if state.get("intent") in REVIEW_INTENTS else "generate_sql"

    g.add_conditional_edges("validate_sql", after_validation,
                            ["optimize", "refuse", "respond", "review_sql", "generate_sql"])

    def after_optimize(state: AgentState) -> str:
        return "execute_sql" if state.get("execute") else "explain"

    g.add_conditional_edges("optimize", after_optimize, ["execute_sql", "explain"])
    g.add_edge("execute_sql", "explain")
    g.add_edge("explain", "respond")
    for terminal in ("refuse", "clarify", "answer_info"):
        g.add_edge(terminal, "respond")
    g.add_edge("respond", END)

    return g.compile(checkpointer=checkpointer if checkpointer is not None else InMemorySaver())
