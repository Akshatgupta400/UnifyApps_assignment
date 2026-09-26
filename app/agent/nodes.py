"""Node functions for the LangGraph workflow (wired together in graph.py).

Each node takes the current state and returns only the fields it changes.
LLM nodes never have the last word: everything they produce passes through the
deterministic validator before it reaches the user.
"""
from __future__ import annotations

import re
from typing import Any

from app.config import Settings
from app.db.introspect import Schema
from app.sql.executor import execute_query
from app.sql.formatter import format_sql
from app.sql.optimizer import analyze_performance
from app.sql.validator import clean_sql, contains_write_statement, validate_sql
from . import prompts as P
from .guards import check_input, extract_sql
from .llm import LLMClient, LLMError
from .state import AgentState

INTENTS = {"generate", "refine", "explain", "debug", "optimize", "schema_info", "sql_concept",
           "greeting", "destructive", "ambiguous", "out_of_scope"}
SQL_INTENTS = {"generate", "refine"}
REVIEW_INTENTS = {"explain", "debug", "optimize"}
INFO_INTENTS = {"schema_info", "sql_concept", "greeting"}


_INDEX_RE = re.compile(r"\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?[\w\"]+\s+"
                       r"ON\s+[\"]?(\w+)[\"]?\s*\(([^)]*)\)", re.IGNORECASE)


def _index_key(stmt: str) -> tuple[str, tuple[str, ...]] | None:
    """(table, columns) for a CREATE INDEX statement, lower-cased; None if not one."""
    m = _INDEX_RE.match(stmt or "")
    if not m:
        return None
    cols = tuple(c.strip().split()[0].strip('"').lower() for c in m.group(2).split(",") if c.strip())
    return (m.group(1).lower(), cols) if cols else None


def _history_text(history: list[dict[str, Any]], turns: int) -> str:
    recent = history[-2 * turns:] if history else []
    if not recent:
        return "(none)"
    lines = []
    for h in recent:
        if h.get("role") == "user":
            lines.append(f"User: {h.get('content', '')}")
        else:
            sql = f"\n  SQL: {h['sql']}" if h.get("sql") else ""
            lines.append(f"Assistant: {h.get('content', '')[:300]}{sql}")
    return "\n".join(lines)


class AgentNodes:
    def __init__(self, llm: LLMClient | None, schema: Schema, settings: Settings,
                 llm_error: str = "") -> None:
        self.llm = llm
        self.schema = schema
        self.settings = settings
        self.db_path = settings.database_path
        self.llm_error = llm_error

    def _require_llm(self) -> LLMClient:
        if self.llm is None:
            raise LLMError(self.llm_error or "No language model is configured.")
        return self.llm

    # ------------------------------------------------------------------ guard
    def guard_input(self, state: AgentState) -> dict:
        cleaned, reason = check_input(state.get("user_input", ""))
        update: dict[str, Any] = {"user_input": cleaned, "block_reason": reason or ""}
        if reason == "destructive":
            update["intent"] = "destructive"
        return update

    # ---------------------------------------------------- intent + scope check
    def classify_intent(self, state: AgentState) -> dict:
        text = state["user_input"]
        last_sql = state.get("last_sql", "")
        detected_sql = extract_sql(text)
        try:
            decision = self._require_llm().structured(
                P.INTENT_SYSTEM.substitute(tables=", ".join(self.schema.table_names()),
                                           last_sql=last_sql or "(none)"),
                P.INTENT_USER.substitute(
                    history=_history_text(state.get("history", []), self.settings.history_turns),
                    message=text),
                P.INTENT_SCHEMA,
            )
        except LLMError as exc:
            return {"intent": "error", "error": str(exc)}

        intent = str(decision.get("intent", "")).strip().lower()
        if intent not in INTENTS:
            intent = "ambiguous"
        user_sql = detected_sql or clean_sql(decision.get("user_sql", ""))
        question = (decision.get("clarifying_question") or "").strip()

        if intent == "refine" and not last_sql:
            intent = "generate"
        if intent in REVIEW_INTENTS:
            if not user_sql and last_sql:
                user_sql = last_sql          # "explain that query" -> previous query
            elif not user_sql:
                question = f"Please paste the SQL query you'd like me to {intent}."
                intent = "ambiguous"
            if user_sql and contains_write_statement(user_sql):
                intent = "destructive"
        if intent == "ambiguous" and not question:
            question = ("Could you say a bit more about what you'd like to see? For example, "
                        "which records, which time period, or how the results should be sorted.")
        return {"intent": intent, "intent_reason": decision.get("reason", ""),
                "user_sql": user_sql, "clarifying_question": question}

    # -------------------------------------------------------- terminal replies
    def refuse(self, state: AgentState) -> dict:
        reason = state.get("block_reason") or state.get("intent")
        message = {
            "injection": P.INJECTION_MESSAGE,
            "too_long": P.TOO_LONG_MESSAGE,
            "empty": P.EMPTY_MESSAGE,
            "destructive": P.DESTRUCTIVE_MESSAGE,
        }.get(reason, P.OUT_OF_SCOPE_MESSAGE)
        return {"response": {"type": "refusal", "message": message, "reason": reason}}

    def clarify(self, state: AgentState) -> dict:
        return {"response": {"type": "clarification", "message": state["clarifying_question"]}}

    def answer_info(self, state: AgentState) -> dict:
        intent = state["intent"]
        if intent == "greeting":
            message = P.GREETING_MESSAGE
        elif intent == "schema_info":
            message = self._describe_schema()
        else:  # sql_concept
            try:
                message = self._require_llm().text(
                    P.CONCEPT_SYSTEM.substitute(schema=self.schema.to_prompt()), state["user_input"])
            except LLMError as exc:
                return {"error": str(exc)}
        return {"response": {"type": "info", "message": message}}

    def _describe_schema(self) -> str:
        lines = ["The database has these tables:"]
        for t in self.schema.tables.values():
            lines.append(f"- {t.name} ({t.row_count} rows): {', '.join(t.column_names)}")
        rels = [f"{t.name}.{fk.column} → {fk.ref_table}.{fk.ref_column}"
                for t in self.schema.tables.values() for fk in t.foreign_keys]
        lines.append("Relationships: " + "; ".join(rels) + ".")
        return "\n".join(lines)

    # ------------------------------------------------------- schema retrieval
    def retrieve_schema(self, state: AgentState) -> dict:
        query_text = " ".join([state["user_input"], state.get("last_sql", ""), state.get("user_sql", "")])
        tables = self.schema.relevant_tables(query_text)
        return {"schema_context": self.schema.to_prompt(tables)}

    # ---------------------------------------------------------- generation
    def generate_sql(self, state: AgentState) -> dict:
        validation = state.get("validation") or {}
        feedback = ""
        if validation.get("errors"):
            feedback = P.GENERATE_FEEDBACK.substitute(
                sql=state.get("sql", ""), errors="\n".join(f"- {e}" for e in validation["errors"]))
        try:
            draft = self._require_llm().structured(
                P.GENERATE_SYSTEM.substitute(schema=state["schema_context"]),
                P.GENERATE_USER.substitute(
                    history=_history_text(state.get("history", []), self.settings.history_turns),
                    last_sql=state.get("last_sql") or "(none)",
                    intent=state["intent"], message=state["user_input"], feedback=feedback),
                P.GENERATE_SCHEMA,
            )
        except LLMError as exc:
            return {"error": str(exc)}
        sql = clean_sql(draft.get("sql", ""))
        if not sql:
            reason = (draft.get("cannot_answer_reason") or "").strip() or \
                "The provided schema doesn't contain the data needed for that request."
            return {"cannot_answer": reason, "sql": ""}
        return {"sql": sql, "notes": {"assumptions": list(draft.get("assumptions") or [])}}

    # ------------------------------------------------ review of user's SQL
    def review_sql(self, state: AgentState) -> dict:
        mode = state["intent"]
        user_sql = state["user_sql"]
        report = state.get("engine_report", "")
        update: dict[str, Any] = {}
        if not report:
            check = validate_sql(user_sql, self.db_path, self.schema, strict_relationships=False)
            report = "No errors: the query compiles against the schema." if check.ok \
                else " ".join(check.errors)
            if check.warnings:
                report += " Warnings: " + " ".join(check.warnings)
            update["engine_report"] = report
            if mode == "explain" and check.ok:
                # Nothing to fix: explain the user's query exactly as written.
                return {**update, "sql": check.sql, "notes": {}}

        validation = state.get("validation") or {}
        feedback = ""
        if validation.get("errors") and state.get("attempts", 0) > 0:
            feedback = P.GENERATE_FEEDBACK.substitute(
                sql=state.get("sql", ""), errors="\n".join(f"- {e}" for e in validation["errors"]))
        try:
            review = self._require_llm().structured(
                P.REVIEW_SYSTEM.substitute(mode_description=P.REVIEW_MODES[mode],
                                           schema=state["schema_context"]),
                P.REVIEW_USER.substitute(message=state["user_input"], sql=user_sql,
                                         engine_report=report, feedback=feedback),
                P.REVIEW_SCHEMA,
            )
        except LLMError as exc:
            return {**update, "error": str(exc)}
        corrected = clean_sql(review.get("corrected_sql", "")) or user_sql
        notes = {"issues": list(review.get("issues") or []),
                 "changes": list(review.get("changes") or []),
                 "index_suggestions": list(review.get("index_suggestions") or [])}
        return {**update, "sql": corrected, "notes": notes}

    # ------------------------------------------------------------ validation
    def validate(self, state: AgentState) -> dict:
        strict = state["intent"] in SQL_INTENTS
        result = validate_sql(state.get("sql", ""), self.db_path, self.schema,
                              strict_relationships=strict)
        update: dict[str, Any] = {"validation": result.to_dict(),
                                  "attempts": state.get("attempts", 0) + 1}
        if result.ok:
            update["sql"] = result.sql if "\n" in result.sql else format_sql(result.sql)
        if result.destructive:
            update["block_reason"] = "destructive"
        return update

    # ----------------------------------------------------------- optimization
    def optimize(self, state: AgentState) -> dict:
        opt = analyze_performance(state["sql"], self.db_path, self.schema).to_dict()
        # Merge index ideas from the LLM review, keeping only ones on real columns
        # and not already recommended by the plan analysis.
        seen = {_index_key(s) for s in opt["index_recommendations"]}
        for stmt in (state.get("notes") or {}).get("index_suggestions", []):
            key = _index_key(stmt)
            if key and key not in seen and all(self.schema.has_column(key[0], c) for c in key[1]):
                opt["index_recommendations"].append(stmt.strip().rstrip(";") + ";")
                seen.add(key)
        return {"optimization": opt}

    # -------------------------------------------------------------- execution
    def execute(self, state: AgentState) -> dict:
        result = execute_query(state["sql"], self.db_path, self.settings.max_result_rows,
                               self.settings.query_timeout_seconds)
        return {"results": result.to_dict()}

    # ------------------------------------------------------------ explanation
    def explain(self, state: AgentState) -> dict:
        try:
            text = self._require_llm().text(
                P.EXPLAIN_SYSTEM, P.EXPLAIN_USER.substitute(message=state["user_input"], sql=state["sql"]))
        except LLMError:
            tables = ", ".join((state.get("validation") or {}).get("tables", [])) or "the database"
            text = f"This query reads data from {tables}."
        return {"explanation": text}

    # --------------------------------------------------------------- response
    def respond(self, state: AgentState) -> dict:
        intent = state.get("intent", "")
        response = dict(state.get("response") or {})
        validation = state.get("validation") or {}
        notes = state.get("notes") or {}
        final_sql = ""

        if state.get("error", "").startswith("RATE_LIMIT"):
            response = {"type": "error",
                        "message": "The AI model has reached its free-tier usage limit for now, so I "
                                   "can't answer this one. Please try again in a few minutes."}
        elif state.get("error"):
            response = {"type": "error",
                        "message": "I couldn't reach the language model, so I can't answer right now. "
                                   f"Details: {state['error']}"}
        elif state.get("cannot_answer"):
            response = {"type": "cannot_answer",
                        "message": f"{state['cannot_answer']} The available tables are: "
                                   f"{', '.join(self.schema.table_names())}."}
        elif not response and validation and not validation.get("ok"):
            errors = " ".join(validation.get("errors", []))
            response = {"type": "error",
                        "message": f"I couldn't produce a valid query for that request after "
                                   f"{state.get('attempts', 0)} attempts. Last problem: {errors} "
                                   f"Try rephrasing, or name the columns you're interested in.",
                        "sql": state.get("sql", ""), "validation": validation}
        elif not response:
            final_sql = state["sql"]
            response = {
                "type": "sql",
                "message": self._summary(intent, notes, state),
                "sql": final_sql,
                "original_sql": state.get("user_sql") if intent in REVIEW_INTENTS else "",
                "explanation": state.get("explanation", ""),
                "assumptions": notes.get("assumptions", []),
                "issues": notes.get("issues", []),
                "changes": notes.get("changes", []),
                "validation": validation,
                "optimization": state.get("optimization") or {},
                "results": state.get("results") or None,
                "attempts": state.get("attempts", 0),
            }
        response["intent"] = intent

        update: dict[str, Any] = {
            "response": response,
            "history": [
                {"role": "user", "content": state.get("user_input", "")},
                {"role": "assistant", "content": response.get("message", ""), "sql": final_sql},
            ],
        }
        if final_sql:
            update["last_sql"] = final_sql
        return update

    @staticmethod
    def _summary(intent: str, notes: dict, state: AgentState) -> str:
        if intent == "refine":
            text = "I updated the previous query."
        elif intent == "debug":
            issues = notes.get("issues") or []
            text = (f"I found {len(issues)} issue{'s' if len(issues) != 1 else ''} and corrected the query."
                    if issues else "I couldn't find any errors; the query is valid against the schema.")
        elif intent == "optimize":
            text = "Here's an optimized version of your query."
        elif intent == "explain":
            text = state.get("explanation") or "Here's what the query does."
        else:
            text = "Here's the SQL for your request."
        if state.get("attempts", 0) > 1:
            text += " (The first draft failed validation, so it was corrected automatically.)"
        return text
