"""Structural facts about a SELECT statement: tables, aliases, JOIN conditions.

Used by the validator (relationship checks) and the optimizer (index advice,
unused joins). SQLite itself is the authority on syntax, tables and columns;
this module only extracts what SQLite's compiler does not report.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .tokens import Token, tokenize

JOIN_MODIFIERS = {"LEFT", "RIGHT", "FULL", "INNER", "CROSS", "NATURAL", "OUTER"}
CLAUSE_END = {"JOIN", "LEFT", "RIGHT", "FULL", "INNER", "CROSS", "NATURAL", "WHERE", "GROUP",
              "ORDER", "HAVING", "LIMIT", "UNION", "EXCEPT", "INTERSECT", "WINDOW", "ON",
              "USING", "OFFSET", "RETURNING"}
RESERVED = CLAUSE_END | {"SELECT", "FROM", "AS", "AND", "OR", "NOT", "BY", "SET", "VALUES",
                         "WITH", "CASE", "WHEN", "THEN", "ELSE", "END", "IS", "NULL", "IN",
                         "LIKE", "BETWEEN", "EXISTS", "DISTINCT", "ALL", "OUTER", "INDEXED"}
COMPARISON_OPS = {"=", "==", "<", ">", "<=", ">=", "<>", "!="}


@dataclass
class TableRef:
    table: str                 # name as written (may be a CTE or a derived table "(subquery)")
    alias: str
    join_type: str = "FROM"    # FROM, JOIN, LEFT JOIN, CROSS JOIN, ...
    has_condition: bool = False
    on_tokens: list[Token] = field(default_factory=list)
    token_span: tuple[int, int] = (0, 0)   # tokens that belong to the ref + its ON clause


@dataclass
class JoinPredicate:
    left_alias: str
    left_column: str
    right_alias: str
    right_column: str


@dataclass
class Structure:
    tokens: list[Token]
    refs: list[TableRef]
    cte_names: set[str]

    def alias_map(self) -> dict[str, str]:
        """alias (lower) -> table name; the table's own name also maps to itself."""
        m = {}
        for r in self.refs:
            m[r.table.lower()] = r.table
            m[r.alias.lower()] = r.table
        return m

    def join_predicates(self) -> list[tuple[TableRef, JoinPredicate]]:
        """``a.x = b.y`` equality predicates found in each JOIN ... ON clause."""
        out = []
        for ref in self.refs:
            toks = ref.on_tokens
            for i in range(len(toks) - 6):
                a, dot1, c1, op, b, dot2, c2 = toks[i:i + 7]
                if (dot1.value == "." and dot2.value == "." and op.value in ("=", "==")
                        and a.ident and c1.ident and b.ident and c2.ident):
                    out.append((ref, JoinPredicate(a.ident, c1.ident, b.ident, c2.ident)))
        return out

    def clause_tokens(self, keyword: str) -> list[Token]:
        """Tokens of every ``keyword`` clause (e.g. WHERE), at any nesting depth."""
        toks, out = self.tokens, []
        for i, t in enumerate(toks):
            if t.kind == "word" and t.upper == keyword:
                if keyword in ("GROUP", "ORDER") and not (i + 1 < len(toks) and toks[i + 1].upper == "BY"):
                    continue
                depth = 0
                for t2 in toks[i + 1:]:
                    if t2.value == "(":
                        depth += 1
                    elif t2.value == ")":
                        if depth == 0:
                            break
                        depth -= 1
                    elif depth == 0 and t2.kind == "word" and t2.upper in CLAUSE_END - {"ON", "USING"} \
                            and t2.upper != keyword:
                        break
                    elif t2.value == ";":
                        break
                    out.append(t2)
        return out


def _find_matching_paren(tokens: list[Token], start: int) -> int:
    depth = 0
    for i in range(start, len(tokens)):
        if tokens[i].value == "(":
            depth += 1
        elif tokens[i].value == ")":
            depth -= 1
            if depth == 0:
                return i
    return len(tokens) - 1


def analyze(sql: str) -> Structure:
    tokens = tokenize(sql)
    refs: list[TableRef] = []
    cte_names: set[str] = set()

    # CTE names: WITH [RECURSIVE] name [(cols)] AS ( ... ), name AS ( ... )
    for i, t in enumerate(tokens):
        if t.kind in ("word", "qident") and i + 1 < len(tokens):
            nxt = tokens[i + 1]
            prev = tokens[i - 1] if i else None
            if prev is not None and (prev.upper in ("WITH", "RECURSIVE") or prev.value == ",") \
                    and (nxt.upper == "AS" or nxt.value == "(") and t.ident and t.upper not in RESERVED:
                j = i + 1
                if nxt.value == "(":
                    j = _find_matching_paren(tokens, j) + 1
                if j < len(tokens) and tokens[j].upper == "AS":
                    cte_names.add(t.ident.lower())

    def parse_ref(i: int, join_type: str) -> tuple[TableRef | None, int]:
        """Parse a table reference starting at token i; return (ref, next index)."""
        if i >= len(tokens):
            return None, i
        start = i
        if tokens[i].value == "(":
            close = _find_matching_paren(tokens, i)
            table, i = "(subquery)", close + 1
        elif tokens[i].ident and tokens[i].upper not in RESERVED:
            table = tokens[i].ident
            i += 1
            if i + 1 < len(tokens) and tokens[i].value == "." and tokens[i + 1].ident:
                table = tokens[i + 1].ident        # schema.table -> table
                i += 2
        else:
            return None, i
        alias = table
        if i < len(tokens) and tokens[i].upper == "AS" and i + 1 < len(tokens) and tokens[i + 1].ident:
            alias, i = tokens[i + 1].ident, i + 2
        elif i < len(tokens) and tokens[i].ident and tokens[i].upper not in RESERVED:
            alias, i = tokens[i].ident, i + 1
        ref = TableRef(table=table, alias=alias, join_type=join_type)
        if i < len(tokens) and tokens[i].upper in ("ON", "USING"):
            ref.has_condition = True
            is_on = tokens[i].upper == "ON"
            i += 1
            depth = 0
            while i < len(tokens):
                t = tokens[i]
                if t.value == "(":
                    depth += 1
                elif t.value == ")":
                    if depth == 0:
                        break
                    depth -= 1
                elif depth == 0 and (t.value == ";" or (t.kind == "word" and t.upper in CLAUSE_END - {"ON", "USING"})):
                    break
                if is_on:
                    ref.on_tokens.append(t)
                i += 1
        ref.token_span = (start, i)
        return ref, i

    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.kind == "word" and t.upper == "FROM":
            ref, i = parse_ref(i + 1, "FROM")
            if ref:
                refs.append(ref)
            while ref and i < len(tokens) and tokens[i].value == ",":   # FROM a, b
                ref, i = parse_ref(i + 1, "CROSS JOIN (comma)")
                if ref:
                    refs.append(ref)
            continue
        if t.kind == "word" and t.upper == "JOIN":
            mods, k = [], i - 1
            while k >= 0 and tokens[k].upper in JOIN_MODIFIERS:
                mods.insert(0, tokens[k].upper)
                k -= 1
            ref, i = parse_ref(i + 1, " ".join(mods + ["JOIN"]))
            if ref:
                refs.append(ref)
            continue
        i += 1
    return Structure(tokens=tokens, refs=refs, cte_names=cte_names)
