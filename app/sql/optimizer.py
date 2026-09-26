"""Performance analysis grounded in SQLite's real query plan.

Everything here is deterministic: suggestions come from ``EXPLAIN QUERY PLAN``,
the schema's actual indexes and row counts, and the query's structure. The LLM
may add its own advice for "optimize this" requests; this module is what makes
sure every returned query gets a factual check.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.db.introspect import Schema
from .structure import COMPARISON_OPS, Structure, analyze
from .validator import open_readonly

SMALL_TABLE_ROWS = 100            # below this, an index is not worth recommending
NON_SARGABLE_FUNCS = {"strftime", "date", "datetime", "julianday", "lower", "upper", "substr",
                      "substring", "trim", "cast", "ifnull", "coalesce", "abs", "round", "length"}


@dataclass
class Optimization:
    suggestions: list[str] = field(default_factory=list)
    index_recommendations: list[str] = field(default_factory=list)
    plan: list[str] = field(default_factory=list)
    cost: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"suggestions": self.suggestions, "index_recommendations": self.index_recommendations,
                "plan": self.plan, "cost": self.cost}


def query_plan(sql: str, db_path: str | Path) -> list[str]:
    conn = open_readonly(db_path)
    try:
        return [row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()]
    finally:
        conn.close()


def _filtered_columns(structure: Structure, schema: Schema) -> list[tuple[str, str]]:
    """(table, column) pairs compared against a value in the WHERE clause."""
    aliases = structure.alias_map()
    real_tables = [schema.table(r.table) for r in structure.refs if schema.table(r.table)]
    toks = structure.clause_tokens("WHERE")
    found: list[tuple[str, str]] = []
    for i, t in enumerate(toks):
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if not t.ident or nxt is None:
            continue
        is_filter = nxt.value in COMPARISON_OPS or nxt.upper in ("IN", "LIKE", "BETWEEN", "IS", "GLOB")
        if not is_filter or (i > 0 and toks[i - 1].value == "(" and i > 1 and toks[i - 2].ident
                             and toks[i - 2].ident.lower() in NON_SARGABLE_FUNCS):
            continue
        qualifier = toks[i - 2].ident if i >= 2 and toks[i - 1].value == "." else None
        if qualifier:
            table = schema.table(aliases.get(qualifier.lower()))
            if table and table.column(t.ident):
                found.append((table.name, table.column(t.ident).name))
        else:
            owners = [tb for tb in real_tables if tb.column(t.ident)]
            if len(owners) == 1:
                found.append((owners[0].name, owners[0].column(t.ident).name))
    return list(dict.fromkeys(found))


def _is_indexed(schema: Schema, table: str, column: str) -> bool:
    t = schema.table(table)
    if not t:
        return True
    col = t.column(column)
    if col and col.primary_key and "INT" in col.type:
        return True
    return any(cols and cols[0].lower() == column.lower() for _, cols in t.indexes)


def estimate_cost(plan: list[str], structure: Structure, schema: Schema) -> dict:
    """Heuristic cost: rows SQLite will touch, based on the plan's loop order."""
    aliases = structure.alias_map()
    examined, outer = 0.0, 1.0
    sort_rows = 0.0
    for step in plan:
        m = re.match(r"(SCAN|SEARCH) (\S+)(?: AS \S+)?(.*)", step)
        if m:
            kind, name, rest = m.groups()
            table = schema.table(aliases.get(name.lower(), name))
            n = float(table.row_count if table else 1000)
            if kind == "SCAN":
                per_loop = out_rows = n
            elif "PRIMARY KEY" in rest or "rowid=" in rest:
                per_loop = out_rows = 1.0
            else:   # indexed search: assume ~10% selectivity, at least a few rows
                per_loop = out_rows = max(n * 0.1, 1.0) if "=" in rest else max(n * 0.3, 1.0)
            examined += outer * per_loop
            outer = min(outer * out_rows, 1e12)
        elif "TEMP B-TREE" in step:
            sort_rows = max(sort_rows, outer)
    examined += sort_rows * math.log2(sort_rows) if sort_rows > 1 else 0
    level = "low" if examined < 5_000 else "medium" if examined < 200_000 else "high"
    return {"estimated_rows_examined": int(examined), "level": level,
            "basis": "Heuristic from SQLite's query plan and table row counts."}


def analyze_performance(sql: str, db_path: str | Path, schema: Schema) -> Optimization:
    opt = Optimization()
    structure = analyze(sql)
    try:
        opt.plan = query_plan(sql, db_path)
    except Exception as exc:  # validation already passed, so this is unexpected
        opt.suggestions.append(f"Could not read the query plan: {exc}")
        return opt
    aliases = structure.alias_map()
    toks = structure.tokens
    upper_words = [t.upper for t in toks if t.kind == "word"]

    # --- index recommendations from full scans ---------------------------------
    scanned = {}
    for step in opt.plan:
        m = re.match(r"SCAN (\S+)(?: AS \S+)?(.*)", step)
        if m and "COVERING INDEX" not in m.group(2):
            table = schema.table(aliases.get(m.group(1).lower(), m.group(1)))
            if table:
                scanned[table.name] = table
        auto = re.match(r"SEARCH (\S+)(?: AS \S+)? USING AUTOMATIC (?:COVERING )?INDEX \((\w+)", step)
        if auto:
            table = schema.table(aliases.get(auto.group(1).lower(), auto.group(1)))
            if table and table.column(auto.group(2)):
                col = table.column(auto.group(2)).name
                opt.index_recommendations.append(
                    f"CREATE INDEX idx_{table.name.lower()}_{col.lower()} ON {table.name}({col});")
                opt.suggestions.append(f"SQLite builds a temporary index on {table.name}.{col} every time "
                                       f"this join runs; a permanent index avoids that work.")
    for table_name, column in _filtered_columns(structure, schema):
        table = schema.table(table_name)
        if table_name in scanned and not _is_indexed(schema, table_name, column):
            if table.row_count >= SMALL_TABLE_ROWS:
                stmt = f"CREATE INDEX idx_{table.name.lower()}_{column.lower()} ON {table.name}({column});"
                if stmt not in opt.index_recommendations:
                    opt.index_recommendations.append(stmt)
    # A big table scanned while the WHERE filter sits on the table it joins to:
    # an index on the scanned side's join (foreign-key) column lets SQLite
    # start from the filtered rows and look matches up instead.
    filtered_tables = {t for t, _ in _filtered_columns(structure, schema)}
    for _ref, pred in structure.join_predicates():
        sides = [(aliases.get(pred.left_alias.lower()), pred.left_column),
                 (aliases.get(pred.right_alias.lower()), pred.right_column)]
        for (tbl, col), (other, _) in (sides, sides[::-1]):
            table, other_t = schema.table(tbl), schema.table(other)
            if (table and other_t and table.name in scanned and other_t.name in filtered_tables
                    and other_t.name != table.name and table.row_count >= SMALL_TABLE_ROWS
                    and table.column(col) and not _is_indexed(schema, table.name, col)):
                c = table.column(col).name
                stmt = f"CREATE INDEX idx_{table.name.lower()}_{c.lower()} ON {table.name}({c});"
                if stmt not in opt.index_recommendations:
                    opt.index_recommendations.append(stmt)
    if opt.index_recommendations:
        opt.suggestions.append("Some filters or joins read every row of a table. The index(es) below "
                               "would let SQLite jump straight to the matching rows.")

    # --- query-shape suggestions ------------------------------------------------
    select_star = any(toks[i].upper == "SELECT" and i + 1 < len(toks) and toks[i + 1].value == "*"
                      for i in range(len(toks)))
    if select_star or re.search(r"\b\w+\s*\.\s*\*", sql):
        opt.suggestions.append("SELECT * returns every column. Listing only the columns you need "
                               "makes results easier to read and cheaper to transfer.")

    where = structure.clause_tokens("WHERE")
    for i, t in enumerate(where[:-1]):
        if t.ident and t.ident.lower() in NON_SARGABLE_FUNCS and where[i + 1].value == "(":
            inner = next((w.ident for w in where[i + 2:i + 8] if w.ident and any(
                schema.table(r.table) and schema.table(r.table).column(w.ident) for r in structure.refs)), "the column")
            opt.suggestions.append(f"The WHERE clause wraps {inner} in {t.ident.upper()}(), which stops SQLite "
                                   f"from using an index on it. Compare the raw column instead, e.g. "
                                   f"{inner} >= '2024-01-01' AND {inner} < '2025-01-01' for a year filter.")
            break
    if re.search(r"\bLIKE\s+'%", sql, re.IGNORECASE):
        opt.suggestions.append("LIKE with a leading % cannot use an index; use a prefix match "
                               "(LIKE 'abc%') if that fits the requirement.")

    # --- unnecessary joins --------------------------------------------------------
    if not select_star:
        for ref in structure.refs:
            if not ref.join_type.endswith("JOIN") or ref.table == "(subquery)":
                continue
            start, end = ref.token_span
            outside = toks[:start] + toks[end:]
            used = any(outside[k].ident and outside[k].ident.lower() == ref.alias.lower()
                       and k + 1 < len(outside) and outside[k + 1].value == "."
                       for k in range(len(outside)))
            table = schema.table(ref.table)
            if not used and table:
                others = [schema.table(r.table) for r in structure.refs if r is not ref and schema.table(r.table)]
                bare_cols = {t.ident.lower() for t in outside if t.ident}
                owned_only_here = any(c.name.lower() in bare_cols and not any(o.column(c.name) for o in others)
                                      for c in table.columns)
                if not owned_only_here:
                    how = ("It can be removed." if ref.join_type.startswith("LEFT")
                           else "If it is not meant to filter rows, it can be removed.")
                    opt.suggestions.append(f"The join to {ref.table} is not used in the result, "
                                           f"filters or sorting. {how}")

    has_limit = "LIMIT" in upper_words
    is_aggregate = "GROUP" in upper_words or re.search(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", sql, re.I)
    biggest = max((schema.table(r.table).row_count for r in structure.refs if schema.table(r.table)), default=0)
    if not has_limit and not is_aggregate and biggest >= 1000:
        opt.suggestions.append("This can return a large number of rows; add a LIMIT if you only need a sample.")
    if any("TEMP B-TREE FOR ORDER BY" in s for s in opt.plan) and biggest >= 1000 \
            and "GROUP" not in upper_words:
        opt.suggestions.append("Sorting needs a temporary structure; an index on the ORDER BY column(s) "
                               "can remove that step for large tables.")

    opt.cost = estimate_cost(opt.plan, structure, schema)
    return opt
