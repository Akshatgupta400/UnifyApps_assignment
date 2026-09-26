"""Deterministic input guardrails that run before any LLM call.

These are cheap, predictable checks. They are not the only defence: the
intent classifier refuses anything off-topic, and the SQL validator enforces
read-only access at the database-engine level whatever the LLM produces.
"""
from __future__ import annotations

import re
import unicodedata

MAX_INPUT_CHARS = 4000

# Phrases that try to override the agent's instructions or extract its prompt.
_INJECTION_PATTERNS = [
    r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(previous|prior|above|earlier|all|your|system)\b.{0,30}\b(instructions?|rules?|prompts?|guidelines?|constraints?|guardrails?)",
    r"\b(reveal|show|print|repeat|output|display|leak|tell me)\b.{0,40}\b(system|hidden|initial|original|developer)\s+(prompt|instructions?|message)",
    r"\byou are (now|no longer)\b",
    r"\b(pretend|act|behave|roleplay)\b.{0,20}\b(as|like)\b.{0,30}\b(unrestricted|jailbroken|dan|different (ai|assistant)|general(-| )purpose)",
    r"\b(developer|god|admin|jailbreak|dan)\s+mode\b",
    r"\bnew (system )?instructions?\s*:",
    r"<\s*/?\s*(system|assistant|instructions?)\s*>",
    r"^\s*(system|assistant)\s*:",
]
_INJECTION_RE = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in _INJECTION_PATTERNS]

# A destructive SQL statement written out by the user (not natural language).
_DESTRUCTIVE_SQL_RE = re.compile(
    r"\b(DELETE\s+FROM|DROP\s+(TABLE|VIEW|INDEX|DATABASE|SCHEMA)|INSERT\s+(OR\s+\w+\s+)?INTO|"
    r"UPDATE\s+[\w\"`\[\]]+\s+SET|ALTER\s+TABLE|TRUNCATE(\s+TABLE)?\s+\w+|REPLACE\s+INTO|"
    r"CREATE\s+(TABLE|VIEW|INDEX|TRIGGER))\b",
    re.IGNORECASE,
)
_CTE = r"WITH\s+(?:RECURSIVE\s+)?\w+\s*(?:\([^)]*\)\s*)?AS\s*\("
_SQL_START_RE = re.compile(rf"^\s*(SELECT\b.+?\bFROM\b|{_CTE})", re.IGNORECASE | re.DOTALL)
_FENCE_RE = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_INLINE_SQL_RE = re.compile(rf"(?is)\b((?:SELECT\b.+?\bFROM\b|{_CTE}).*)$")


def sanitize(text: str) -> str:
    """Normalise unicode, drop control/zero-width characters, trim."""
    text = unicodedata.normalize("NFKC", text or "")
    text = "".join(ch for ch in text
                   if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    return text.strip()


def detect_injection(text: str) -> bool:
    return any(p.search(text) for p in _INJECTION_RE)


def contains_destructive_sql(text: str) -> bool:
    return bool(_DESTRUCTIVE_SQL_RE.search(text))


def extract_sql(text: str) -> str:
    """Pull a SQL query out of a message: a ```sql fence, or a trailing SELECT/WITH."""
    fence = _FENCE_RE.search(text)
    if fence:
        return fence.group(1).strip().rstrip(";").strip()
    if _SQL_START_RE.match(text):
        return text.strip().rstrip(";").strip()
    inline = _INLINE_SQL_RE.search(text)
    if inline:
        return inline.group(1).strip().rstrip(";").strip()
    return ""


def check_input(text: str) -> tuple[str, str | None]:
    """Return (cleaned text, block reason or None).

    Block reasons: 'empty', 'too_long', 'injection', 'destructive'.
    """
    cleaned = sanitize(text)
    if not cleaned:
        return cleaned, "empty"
    if len(cleaned) > MAX_INPUT_CHARS:
        return cleaned, "too_long"
    if detect_injection(cleaned):
        return cleaned, "injection"
    # Only statement-shaped SQL is caught here ("DELETE FROM x", "DROP TABLE y"), so
    # plain English like "drop duplicates" is not misread; the LLM classifier and
    # the validator handle everything else.
    if contains_destructive_sql(cleaned):
        return cleaned, "destructive"
    return cleaned, None
