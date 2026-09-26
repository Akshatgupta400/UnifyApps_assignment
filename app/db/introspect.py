"""Read the live database schema.

The schema is always introspected from the database itself (never hard-coded
in a prompt), so the agent's view of tables, columns and relationships can
never drift from what actually exists.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

# Text columns with at most this many distinct values get their values listed in
# the prompt, so the model knows e.g. that State holds 'CA', not 'California'.
MAX_SAMPLE_VALUES = 15


@dataclass
class Column:
    name: str
    type: str
    primary_key: bool = False
    not_null: bool = False
    note: str = ""          # the column's "-- comment" from its CREATE TABLE statement


@dataclass
class ForeignKey:
    column: str
    ref_table: str
    ref_column: str


@dataclass
class Table:
    name: str
    columns: list[Column]
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    indexes: list[tuple[str, list[str]]] = field(default_factory=list)
    row_count: int = 0
    sample_values: dict[str, list[str]] = field(default_factory=dict)

    def column(self, name: str) -> Column | None:
        name = name.lower()
        return next((c for c in self.columns if c.name.lower() == name), None)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


@dataclass
class Schema:
    tables: dict[str, Table]

    # ---- lookups (case-insensitive, like SQLite itself) --------------------
    def table(self, name: str | None) -> Table | None:
        if not name:
            return None
        name = name.strip('"`[]').lower()
        return next((t for key, t in self.tables.items() if key.lower() == name), None)

    def has_column(self, table: str, column: str) -> bool:
        t = self.table(table)
        return bool(t and t.column(column))

    def all_columns(self) -> list[str]:
        return sorted({c.name for t in self.tables.values() for c in t.columns})

    def relationship_pairs(self) -> set[frozenset[tuple[str, str]]]:
        """Every declared FK as an unordered pair of (table, column), lower-cased."""
        pairs = set()
        for t in self.tables.values():
            for fk in t.foreign_keys:
                pairs.add(frozenset({(t.name.lower(), fk.column.lower()),
                                     (fk.ref_table.lower(), fk.ref_column.lower())}))
        return pairs

    # ---- rendering -----------------------------------------------------------
    def table_names(self) -> list[str]:
        return list(self.tables)

    def to_prompt(self, tables: list[str] | None = None) -> str:
        """Compact, LLM-friendly description of (a subset of) the schema."""
        chosen = [self.tables[n] for n in (tables or self.tables) if n in self.tables]
        blocks = []
        for t in chosen:
            lines = [f"TABLE {t.name}  -- {t.row_count} rows"]
            for c in t.columns:
                bits = [f"  {c.name} {c.type or 'TEXT'}"]
                if c.primary_key:
                    bits.append("PRIMARY KEY")
                fk = next((f for f in t.foreign_keys if f.column == c.name), None)
                if fk:
                    bits.append(f"REFERENCES {fk.ref_table}({fk.ref_column})")
                comment = [c.note] if c.note else []
                if c.name in t.sample_values:
                    comment.append("values: " + ", ".join(f"'{v}'" for v in t.sample_values[c.name]))
                if comment:
                    bits.append("-- " + "; ".join(comment))
                lines.append(" ".join(bits))
            blocks.append("\n".join(lines))
        rels = [f"{t.name}.{fk.column} -> {fk.ref_table}.{fk.ref_column}"
                for t in chosen for fk in t.foreign_keys]
        text = "\n\n".join(blocks)
        if rels:
            text += "\n\nRELATIONSHIPS (join only along these):\n" + "\n".join(f"  {r}" for r in rels)
        return text

    def to_dict(self) -> dict:
        return {
            "tables": [
                {
                    "name": t.name,
                    "row_count": t.row_count,
                    "columns": [{"name": c.name, "type": c.type, "primary_key": c.primary_key,
                                 "not_null": c.not_null, "note": c.note} for c in t.columns],
                    "foreign_keys": [{"column": f.column, "ref_table": f.ref_table,
                                      "ref_column": f.ref_column} for f in t.foreign_keys],
                    "indexes": [{"name": n, "columns": cols} for n, cols in t.indexes],
                }
                for t in self.tables.values()
            ]
        }

    # ---- retrieval -----------------------------------------------------------
    def relevant_tables(self, text: str, limit: int = 8) -> list[str]:
        """Pick the tables most relevant to ``text``.

        For the bundled schema every table fits in the prompt, so this returns
        all of them. For larger databases it ranks tables by name/column
        mentions and pulls in their FK neighbours, keeping prompts small.
        """
        if len(self.tables) <= limit:
            return list(self.tables)
        words = set(re.findall(r"[a-z]+", text.lower()))
        scores: dict[str, int] = {}
        for t in self.tables.values():
            name = t.name.lower()
            score = 3 * any(w in name or name.rstrip("s") in w for w in words)
            score += sum(1 for c in t.columns if c.name.lower() in words)
            scores[t.name] = score
        ranked = [n for n, s in sorted(scores.items(), key=lambda kv: -kv[1]) if s > 0][:limit]
        for name in list(ranked):
            for fk in self.tables[name].foreign_keys:
                if fk.ref_table in self.tables and fk.ref_table not in ranked:
                    ranked.append(fk.ref_table)
        return ranked or list(self.tables)[:limit]


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _column_notes(create_sql: str | None) -> dict[str, str]:
    """Map column name -> trailing ``-- comment`` in the stored CREATE TABLE text."""
    notes = {}
    for line in (create_sql or "").splitlines():
        m = re.match(r"\s*[\"`\[]?(\w+)[\"`\]]?\s+\w.*?--\s*(.+?)\s*$", line)
        if m:
            notes[m.group(1)] = m.group(2)
    return notes


def load_schema(db_path: str | Path) -> Schema:
    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY rowid").fetchall()
        tables: dict[str, Table] = {}
        for name, create_sql in rows:
            q = _quote(name)
            notes = _column_notes(create_sql)
            cols = [Column(name=r[1], type=(r[2] or "").upper(), primary_key=bool(r[5]),
                           not_null=bool(r[3]), note=notes.get(r[1], ""))
                    for r in conn.execute(f"PRAGMA table_info({q})")]
            fks = [ForeignKey(column=r[3], ref_table=r[2], ref_column=r[4] or "")
                   for r in conn.execute(f"PRAGMA foreign_key_list({q})")]
            indexes = []
            for r in conn.execute(f"PRAGMA index_list({q})"):
                idx_cols = [c[2] for c in conn.execute(f"PRAGMA index_info({_quote(r[1])})")]
                indexes.append((r[1], idx_cols))
            row_count = conn.execute(f"SELECT COUNT(*) FROM {q}").fetchone()[0]
            samples = {}
            for c in cols:
                if "CHAR" in c.type or "TEXT" in c.type or c.type == "":
                    distinct = conn.execute(
                        f"SELECT DISTINCT {_quote(c.name)} FROM {q} WHERE {_quote(c.name)} IS NOT NULL "
                        f"LIMIT {MAX_SAMPLE_VALUES + 1}").fetchall()
                    if 0 < len(distinct) <= MAX_SAMPLE_VALUES and len(distinct) < max(row_count, 1):
                        samples[c.name] = sorted(str(v[0]) for v in distinct)
            tables[name] = Table(name=name, columns=cols, foreign_keys=fks, indexes=indexes,
                                 row_count=row_count, sample_values=samples)
        return Schema(tables=tables)
    finally:
        conn.close()
