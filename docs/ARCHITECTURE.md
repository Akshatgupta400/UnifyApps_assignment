# Architecture

![Architecture diagram](architecture.png)

(Source: [`architecture.dot`](architecture.dot), also rendered as [`architecture.svg`](architecture.svg). Regenerate with `dot -Tpng architecture.dot -o architecture.png`.)

## Components

**Browser UI** (`app/static/`). Plain HTML, CSS and JavaScript, served by Flask, no build step. Three columns: conversation history and a
schema browser; the chat; and a query sheet with the agent's progress track, the SQL (syntax-highlighted, Copy SQL, Download .sql),
the plain-English explanation with assumptions / issues / changes, and tabs for Results (with CSV download), Checks (validation
report) and Performance (query plan, cost estimate, index recommendations). Dark mode, Enter-to-send, and drawers on narrow
screens. Responses stream in over server-sent events so each workflow step is visible while it runs.

**Flask backend** (`app/server.py`). A thin HTTP layer: it owns the conversation list and passes each message into the compiled
LangGraph with the conversation's `thread_id`. Endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Status, model name, whether the LLM is configured |
| GET | `/api/schema` | Tables, columns, keys, row counts (schema browser) |
| GET | `/api/graph` | The LangGraph as Mermaid |
| GET / POST | `/api/sessions` | List / create conversations |
| GET / DELETE | `/api/sessions/<id>` | Messages of a conversation / delete it |
| POST | `/api/chat` | One turn, JSON response |
| POST | `/api/chat/stream` | One turn, SSE: `step` events then `final` |
| POST | `/api/export/csv` | Re-validates the SQL and returns up to `MAX_EXPORT_ROWS` rows as CSV |

**LangGraph agent** (`app/agent/`). `graph.py` wires the nodes, `nodes.py` implements them, `prompts.py` holds every prompt,
`guards.py` has the deterministic input checks, `llm.py` wraps a LangChain chat model. See [WORKFLOW.md](WORKFLOW.md).

**SQL toolkit** (`app/sql/`). Pure Python on top of `sqlite3`, no LLM involved, so it is fully unit-tested:
`tokens.py` (SQL tokenizer), `structure.py` (tables, aliases, join predicates), `validator.py`, `optimizer.py`, `formatter.py`, `executor.py`.

**Database** (`app/db/`). `schema.sql` is the sample schema, `seed.py` builds a deterministic sample dataset,
`introspect.py` reads the live schema (tables, columns, types, foreign keys, notes, sample values) for prompts and validation.

## Design decisions

- **The database is the source of truth for validation.** Rather than a hand-written SQL parser, queries are compiled by SQLite
  (`EXPLAIN`) on a read-only connection with an authorizer. If SQLite accepts it, the tables, columns and syntax are real; the
  error messages are real too, and they feed the repair loop.
- **Defence in depth for read-only.** Input guard (regex, before any LLM call) → intent classifier (`destructive`) → keyword scan of
  the generated SQL → SQLite authorizer at compile time → read-only connection (`mode=ro`) at execution time. Any one of these
  would stop a write; together they cover model mistakes and adversarial input.
- **Deterministic where possible, LLM where necessary.** Refusals, schema questions, validation, optimisation analysis, cost
  estimation and execution use no model calls. The model does classification, SQL writing, review and explanation.
- **Structured outputs.** Every decision the graph branches on comes from a JSON-schema function call, not from parsing prose.
- **Provider-agnostic.** `init_chat_model("provider:model")` means OpenAI, Groq, Anthropic or Gemini work by changing `LLM_MODEL`
  and installing the matching `langchain-*` package. Graph nodes depend only on a two-method `LLMClient` protocol, which is also
  what lets the tests run offline with a scripted fake model.
- **The app degrades gracefully.** Without an API key the server still starts; the UI shows a banner and the schema browser
  works. Model or network failures become a readable error message rather than a stack trace.
