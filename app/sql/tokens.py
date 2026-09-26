"""A small SQL tokenizer.

It is not a parser. It is just enough to reason about SQL safely: comments are
dropped and string literals / quoted identifiers are kept as single tokens, so a
keyword hidden inside a string (``WHERE note = 'drop table'``) or a comment
(``/* DELETE */``) is never mistaken for a real statement.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<line_comment>--[^\n]*)
  | (?P<block_comment>/\*.*?(?:\*/|\Z))
  | (?P<string>'(?:[^']|'')*'?)
  | (?P<qident>"(?:[^"]|"")*"?|`[^`]*`?|\[[^\]]*\]?)
  | (?P<number>\d+(?:\.\d*)?(?:[eE][-+]?\d+)?|\.\d+)
  | (?P<word>[A-Za-z_][A-Za-z0-9_$]*)
  | (?P<param>[?:@$][A-Za-z0-9_]*)
  | (?P<op><=|>=|<>|!=|==|\|\||<<|>>|[-+*/%<>=~&|])
  | (?P<punct>[(),.;])
  | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)


@dataclass(frozen=True)
class Token:
    kind: str   # word | qident | string | number | op | punct | param | other
    value: str

    @property
    def upper(self) -> str:
        return self.value.upper()

    @property
    def ident(self) -> str | None:
        """The identifier this token names, unquoted (None if not an identifier)."""
        if self.kind == "word":
            return self.value
        if self.kind == "qident":
            return self.value[1:-1] if len(self.value) >= 2 else self.value
        return None


def tokenize(sql: str) -> list[Token]:
    tokens = []
    for m in _TOKEN_RE.finditer(sql):
        kind = m.lastgroup
        if kind in ("ws", "line_comment", "block_comment"):
            continue
        tokens.append(Token(kind, m.group()))
    return tokens


def strip_comments(sql: str) -> str:
    """Return ``sql`` with comments removed (strings untouched)."""
    out, last = [], 0
    for m in _TOKEN_RE.finditer(sql):
        if m.lastgroup in ("line_comment", "block_comment"):
            out.append(sql[last:m.start()])
            out.append(" ")
            last = m.end()
    out.append(sql[last:])
    return "".join(out)
