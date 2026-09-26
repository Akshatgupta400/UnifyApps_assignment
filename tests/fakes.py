"""A scripted stand-in for the LLM, so tests run offline and deterministically.

Responses are queued per structured-output schema title (IntentDecision,
SQLDraft, SQLReview) and for plain-text calls. Every call is recorded, which
lets tests assert on what the agent actually sent to the model.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Callable

from app.agent.llm import LLMError


def intent(name: str, user_sql: str = "", question: str = "") -> dict:
    return {"intent": name, "user_sql": user_sql, "clarifying_question": question, "reason": "test"}


def draft(sql: str, assumptions: list[str] | None = None, cannot: str = "") -> dict:
    return {"sql": sql, "cannot_answer_reason": cannot, "assumptions": assumptions or []}


def review(sql: str, issues=None, changes=None, indexes=None) -> dict:
    return {"corrected_sql": sql, "issues": issues or [], "changes": changes or [],
            "index_suggestions": indexes or []}


class FakeLLM:
    def __init__(self, text: str | Callable[[str, str], str] = "This query lists the requested rows.",
                 fail: bool = False, fail_message: str = "simulated outage") -> None:
        self.queues: dict[str, deque] = defaultdict(deque)
        self.text_reply = text
        self.fail = fail
        self.fail_message = fail_message
        self.calls: list[dict[str, Any]] = []

    def queue(self, title: str, *responses: dict) -> "FakeLLM":
        self.queues[title].extend(responses)
        return self

    def structured(self, system: str, user: str, schema: dict) -> dict:
        self.calls.append({"kind": schema["title"], "system": system, "user": user})
        if self.fail:
            raise LLMError(self.fail_message)
        queue = self.queues[schema["title"]]
        if not queue:
            raise AssertionError(f"FakeLLM: no scripted response left for {schema['title']}")
        return queue.popleft()

    def text(self, system: str, user: str) -> str:
        self.calls.append({"kind": "text", "system": system, "user": user})
        if self.fail:
            raise LLMError(self.fail_message)
        return self.text_reply(system, user) if callable(self.text_reply) else self.text_reply

    def calls_of(self, kind: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["kind"] == kind]
