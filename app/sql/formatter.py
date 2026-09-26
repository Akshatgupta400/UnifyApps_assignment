"""Readable SQL layout: upper-case keywords, one clause per line.

Deliberately conservative: it only re-lays out whitespace and keyword case, so
the formatted query is always equivalent to the input.
"""
from __future__ import annotations

from .tokens import tokenize

KEYWORDS = {
    "SELECT", "FROM", "WHERE", "GROUP", "BY", "ORDER", "HAVING", "LIMIT", "OFFSET", "JOIN",
    "LEFT", "RIGHT", "INNER", "OUTER", "FULL", "CROSS", "NATURAL", "ON", "USING", "AS", "AND",
    "OR", "NOT", "IN", "IS", "NULL", "LIKE", "BETWEEN", "EXISTS", "DISTINCT", "ALL", "UNION",
    "INTERSECT", "EXCEPT", "WITH", "RECURSIVE", "CASE", "WHEN", "THEN", "ELSE", "END", "ASC",
    "DESC", "OVER", "PARTITION", "WINDOW", "COLLATE", "GLOB", "ESCAPE", "CAST", "FILTER",
}
NEWLINE_BEFORE = {"SELECT", "FROM", "WHERE", "GROUP", "ORDER", "HAVING", "LIMIT", "UNION",
                  "INTERSECT", "EXCEPT", "WINDOW"}
JOIN_STARTERS = {"JOIN", "LEFT", "RIGHT", "INNER", "FULL", "CROSS", "NATURAL"}


def format_sql(sql: str) -> str:
    tokens = tokenize(sql)
    if not tokens:
        return sql.strip()
    lines: list[str] = []
    line: list[str] = []
    depth = 0
    clause = ""
    prev = None

    def flush():
        if line:
            lines.append("".join(line).rstrip())
            line.clear()

    for i, tok in enumerate(tokens):
        word = tok.upper if tok.kind == "word" else None
        text = word if word in KEYWORDS else tok.value
        prev_word = prev.upper if prev is not None and prev.kind == "word" else None
        new_line = False
        indent = "    " * depth
        if word in NEWLINE_BEFORE and not (word == "SELECT" and prev is not None and prev.value == "("):
            new_line, clause = True, word
        elif word in JOIN_STARTERS and prev_word not in JOIN_STARTERS | {"OUTER"}:
            new_line, clause = True, "JOIN"
        elif word in ("AND", "OR") and clause in ("WHERE", "HAVING", "JOIN") \
                and not _inside_between(tokens, i):
            new_line, indent = True, indent + "  "
        if new_line and (line or lines):
            flush()
            line.append(indent)
        elif line and not _no_space(prev, tok):
            line.append(" ")
        line.append(text)
        if tok.value == "(":
            depth += 1
        elif tok.value == ")":
            depth = max(depth - 1, 0)
        prev = tok
    flush()
    return "\n".join(lines)


def _inside_between(tokens, i) -> bool:
    """True if the AND at position i belongs to a BETWEEN ... AND ..."""
    for j in range(i - 1, max(i - 6, -1), -1):
        if tokens[j].upper == "BETWEEN":
            return True
        if tokens[j].upper in ("AND", "OR", "WHERE", "ON", "HAVING"):
            return False
    return False


def _no_space(prev, tok) -> bool:
    if prev is None:
        return True
    if tok.value in (",", ")", ".") or prev.value in ("(", "."):
        return True
    # function call: name( ... but keep space for "IN (", "AS (", "EXISTS ("
    if tok.value == "(" and prev.kind in ("word", "qident") and prev.upper not in KEYWORDS:
        return True
    return False
