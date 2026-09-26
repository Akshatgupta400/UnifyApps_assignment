"""Validate SQL before it is ever shown to the user or executed.

Checks run in layers, each catching what the one before can't:

1. Lexical: exactly one statement, starting with SELECT/WITH, and no write or
   admin keyword anywhere outside strings and comments.
2. Engine: the statement is compiled (never run) by SQLite with an *authorizer*
   that only permits reading. SQLite reports unknown tables, unknown columns and
   syntax errors, and the authorizer rejects any write the lexical pass missed.
   Double-quoted strings are disabled, so a misspelled "Column" can't silently
   become a string literal.
3. Relationships: every ``a.x = b.y`` JOIN condition must follow a declared
   foreign key.
"""
from __future__ import annotations

import difflib
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.db.introspect import Schema
from .structure import analyze
from .tokens import _TOKEN_RE, tokenize

# Statements the agent must never produce (the brief's list plus other writes/admin).
DESTRUCTIVE_KEYWORDS = ("DELETE", "UPDATE", "INSERT", "DROP", "ALTER", "TRUNCATE")
BLOCKED_KEYWORDS = set(DESTRUCTIVE_KEYWORDS) | {
    "CREATE", "REPLACE", "UPSERT", "MERGE", "ATTACH", "DETACH", "PRAGMA", "VACUUM",
    "REINDEX", "ANALYZE", "GRANT", "REVOKE", "EXEC", "EXECUTE", "CALL", "COPY",
    "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE",
}
BLOCKED_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"}

_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
}


@dataclass
class ValidationResult:
    ok: bool
    sql: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    destructive: bool = False
    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings,
                "destructive": self.destructive, "tables": self.tables, "columns": self.columns}


def clean_sql(sql: str) -> str:
    """Strip markdown fences, surrounding whitespace and trailing semicolons."""
    sql = (sql or "").strip()
    fence = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", sql, re.DOTALL)
    if fence:
        sql = fence.group(1)
    return sql.strip().rstrip(";").strip()


def contains_write_statement(sql: str) -> bool:
    """True if ``sql`` contains any write/admin keyword outside strings/comments."""
    tokens = tokenize(sql)
    for i, t in enumerate(tokens):
        if t.kind != "word":
            continue
        word = t.upper
        if word == "REPLACE":   # replace() is a harmless string function...
            nxt = tokens[i + 1].upper if i + 1 < len(tokens) else ""
            prev = tokens[i - 1].upper if i else ""
            if nxt == "INTO" or prev in ("", "OR", ";"):   # ...REPLACE INTO / INSERT OR REPLACE is not
                return True
            continue
        if word in BLOCKED_KEYWORDS:
            # A column literally called e.g. "Update" would be quoted, so a bare
            # keyword is always treated as the statement keyword.
            return True
    return False


def strict_identifiers(sql: str) -> str:
    """Rewrite "double-quoted" identifiers as `backtick` ones for compilation.

    SQLite silently turns an unresolvable "Name" into the string 'Name'. It never
    does that for backticks, so after this rewrite a misspelled quoted column is
    reported as missing on every Python/SQLite version. String literals are
    untouched because the tokenizer keeps them as separate tokens.
    """
    def repl(m: re.Match) -> str:
        text = m.group()
        if m.lastgroup == "qident" and len(text) >= 2 and text[0] == text[-1] == '"':
            return "`" + text[1:-1].replace('""', '"').replace("`", "``") + "`"
        return text
    return _TOKEN_RE.sub(repl, sql)


def open_readonly(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True, check_same_thread=False)
    try:  # Python 3.12+: make "Nope" an error instead of the string 'Nope'.
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DDL, False)
    except (AttributeError, sqlite3.Error):
        pass
    return conn


class _ReadOnlyAuthorizer:
    """SQLite authorizer: records what a statement reads, denies everything else."""

    def __init__(self) -> None:
        self.tables: set[str] = set()
        self.columns: set[str] = set()
        self.denied: list[str] = []

    def __call__(self, action, arg1, arg2, _db, _trigger):
        if action == sqlite3.SQLITE_READ:
            if arg1 and arg1.lower().startswith("sqlite_"):
                self.denied.append(f"reading internal table {arg1}")
                return sqlite3.SQLITE_DENY
            if arg1:
                self.tables.add(arg1)
                if arg2:
                    self.columns.add(f"{arg1}.{arg2}")
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            if (arg2 or "").lower() in BLOCKED_FUNCTIONS:
                self.denied.append(f"function {arg2}()")
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        if action in _ALLOWED_ACTIONS:
            return sqlite3.SQLITE_OK
        self.denied.append(f"action code {action}")
        return sqlite3.SQLITE_DENY


def _friendly_error(message: str, schema: Schema) -> str:
    m = re.match(r"no such table: (?:main\.)?(\S+)", message)
    if m:
        name = m.group(1)
        close = difflib.get_close_matches(name, schema.table_names(), n=2, cutoff=0.6)
        hint = f" Did you mean {' or '.join(close)}?" if close else ""
        return f"Table '{name}' does not exist in the schema.{hint} Available tables: {', '.join(schema.table_names())}."
    m = re.match(r"no such column: (\S+)", message)
    if m:
        name = m.group(1)
        bare = name.split(".")[-1]
        close = difflib.get_close_matches(bare, schema.all_columns(), n=3, cutoff=0.6)
        hint = f" Did you mean {' or '.join(close)}?" if close else ""
        return f"Column '{name}' does not exist.{hint}"
    m = re.match(r'near "(.+?)": syntax error', message)
    if m:
        return f"SQL syntax error near '{m.group(1)}'."
    if message.startswith("ambiguous column name"):
        col = message.split(":", 1)[-1].strip()
        return f"Column '{col}' is ambiguous: it exists in more than one joined table, so qualify it with a table alias."
    if "incomplete input" in message:
        return "SQL syntax error: the query ends unexpectedly (unclosed parenthesis or quote?)."
    return f"SQL error: {message}."


def validate_sql(sql: str, db_path: str | Path, schema: Schema,
                 strict_relationships: bool = True) -> ValidationResult:
    """Validate ``sql``. ``strict_relationships`` turns non-FK joins into errors."""
    sql = clean_sql(sql)
    result = ValidationResult(ok=False, sql=sql)
    if not sql:
        result.errors.append("The query is empty.")
        return result

    tokens = tokenize(sql)
    # ---- 1. lexical ---------------------------------------------------------
    if contains_write_statement(sql):
        result.destructive = True
        result.errors.append("Only read-only SELECT queries are allowed; this SQL modifies data or schema.")
        return result
    if any(t.value == ";" for t in tokens):
        result.errors.append("Only a single SQL statement is allowed.")
        return result
    first = tokens[0].upper if tokens else ""
    if first not in ("SELECT", "WITH"):
        result.errors.append(f"Queries must start with SELECT or WITH (found '{tokens[0].value}').")
        return result

    # ---- 2. engine ------------------------------------------------------------
    auth = _ReadOnlyAuthorizer()
    conn = open_readonly(db_path)
    try:
        conn.set_authorizer(auth)
        conn.execute(f"EXPLAIN QUERY PLAN {strict_identifiers(sql)}").fetchall()
    except sqlite3.ProgrammingError as exc:   # e.g. several statements
        result.errors.append("Only a single SQL statement is allowed."
                             if "one statement" in str(exc) else f"SQL error: {exc}.")
        return result
    except sqlite3.Error as exc:
        if "not authorized" in str(exc) or auth.denied:
            result.destructive = not any(d.startswith(("function", "reading")) for d in auth.denied)
            result.errors.append("Only read-only queries against the provided tables are allowed"
                                 + (f" (blocked: {', '.join(auth.denied)})." if auth.denied else "."))
        else:
            result.errors.append(_friendly_error(str(exc), schema))
        return result
    finally:
        conn.close()
    result.tables = sorted(auth.tables)
    result.columns = sorted(auth.columns)

    # ---- 3. relationships -----------------------------------------------------
    structure = analyze(sql)
    aliases = structure.alias_map()
    pairs = schema.relationship_pairs()
    for ref, pred in structure.join_predicates():
        left_t = schema.table(aliases.get(pred.left_alias.lower()))
        right_t = schema.table(aliases.get(pred.right_alias.lower()))
        if not left_t or not right_t:
            continue   # CTE or subquery: its columns are already checked by SQLite
        pair = frozenset({(left_t.name.lower(), pred.left_column.lower()),
                          (right_t.name.lower(), pred.right_column.lower())})
        if pair not in pairs:
            msg = (f"Join condition {pred.left_alias}.{pred.left_column} = "
                   f"{pred.right_alias}.{pred.right_column} does not follow a declared relationship "
                   f"between {left_t.name} and {right_t.name}.")
            (result.errors if strict_relationships else result.warnings).append(msg)
    for ref in structure.refs:
        if ref.join_type.endswith("JOIN") and ref.join_type not in ("CROSS JOIN", "NATURAL JOIN") \
                and not ref.has_condition:
            result.warnings.append(f"The join to {ref.table} has no ON condition, so it produces "
                                   f"every combination of rows (a cartesian product).")
        if ref.join_type == "CROSS JOIN (comma)":
            result.warnings.append(f"{ref.table} is joined with a comma; an explicit JOIN ... ON is clearer.")

    result.ok = not result.errors
    return result
