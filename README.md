# SQL Query AI Agent

A natural-language-to-SQL assistant built with **LangGraph** and **LangChain**, served by **Flask**, working on a **SQLite** sample
database. Ask a question in plain English and get a validated, read-only SQL query, an explanation, performance advice and the
results. You can also paste SQL to have it explained, debugged or optimised. Anything that is not about SQL or this database
is refused.

![Architecture](docs/architecture.png)

## Quick start

Requires Python 3.10+ and an API key for one LLM provider (OpenAI by default).

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # then put your key in .env: OPENAI_API_KEY=sk-...
python run.py                      # open http://localhost:8000
```

The sample database (`data/sample.db`) is created and seeded automatically on first start. To rebuild it: `python -m app.db.seed`.

### Using a different model

The model is any `provider:model` string understood by LangChain's `init_chat_model`. Install the provider package and set the key:

| Provider | Install | `.env` |
|---|---|---|
| OpenAI (default) | included | `OPENAI_API_KEY=...`, `LLM_MODEL=openai:gpt-4o-mini` |
| Groq (free tier) | `pip install langchain-groq` | `GROQ_API_KEY=...`, `LLM_MODEL=groq:openai/gpt-oss-120b` |
| Anthropic | `pip install langchain-anthropic` | `ANTHROPIC_API_KEY=...`, `LLM_MODEL=anthropic:claude-sonnet-4-5` |
| Google Gemini | `pip install langchain-google-genai` | `GOOGLE_API_KEY=...`, `LLM_MODEL=google_genai:gemini-2.0-flash` |

Without a key the server still starts; the UI shows a banner explaining what to configure.

### Docker

```bash
cp .env.example .env               # add your API key
docker compose up --build          # http://localhost:8000
```

### Tests

```bash
pip install -r requirements-dev.txt
pytest                             # or: python -m unittest discover -s tests -t .
```

89 tests, no network or API key needed: the LLM is replaced by a scripted fake (`tests/fakes.py`), and each test uses a fresh temporary
copy of the seeded database.

| File | Covers |
|---|---|
| `test_validator.py` | read-only enforcement, syntax / table / column errors with suggestions, FK relationship checks, comments and strings that look like SQL keywords |
| `test_sql_tools.py` | optimizer (index advice, anti-patterns, cost estimate), formatter, executor (row cap, timeout, read-only) |
| `test_guards.py` | prompt-injection patterns, destructive SQL detection, SQL extraction from messages |
| `test_graph.py` | the LangGraph workflow end to end: NL→SQL, automatic repair of invalid SQL, follow-up refinement, explain / debug / optimize, all refusal paths, clarifying questions, LLM outage |
| `test_api.py` | HTTP endpoints, SSE streaming, sessions, CSV export re-validation |

## What it does

| Requirement | How |
|---|---|
| Natural language → SQL | `generate_sql` node, prompted with the introspected schema (columns, notes, sample values, relationships) |
| Validation: tables, columns, relationships, syntax | SQLite compiles the query (`EXPLAIN`) on a read-only connection with an authorizer; joins must follow declared foreign keys; invalid SQL is sent back to the model for up to 2 automatic repairs |
| Read-only, refuse DELETE / UPDATE / INSERT / DROP / ALTER / TRUNCATE | Five layers: input guard, intent classifier, keyword scan, SQLite authorizer, read-only connection. Refusals are polite and offer a SELECT instead |
| Plain-English explanation | `explain` node, shown in the explanation panel |
| Optimisation | `EXPLAIN QUERY PLAN` analysis, index recommendations, `SELECT *`, non-sargable filters, leading-wildcard `LIKE`, unused joins; plus LLM review for readability |
| Debugging | Paste broken SQL: SQLite's real error, the issues, why, and a corrected query that is itself validated |
| Conversation context | Each conversation is a LangGraph thread with an `InMemorySaver` checkpointer; "Only those from California" modifies the previous query |
| Out-of-scope refusal | Exactly: *"I'm designed to assist only with SQL and database-related tasks. Please ask a question related to the provided database schema."* |
| Frontend | Chat, SQL panel, explanation panel, conversation history, live progress indicator, error messages, Copy SQL |
| Optional: dark mode, streaming, download SQL / results | All three: theme toggle, SSE step-by-step streaming, `.sql` and CSV downloads |

Bonus items implemented: query execution with results, follow-up refinement, query cost estimation, SQL syntax highlighting,
CSV export, prompt-injection protection, Docker, unit and integration tests. Not implemented: multi-dialect support (SQLite only).

Try these (also available as buttons on the welcome screen):

```
Show all employees hired after January 2024.
Show all customers.          → then: Only those from California.
Top 5 products by revenue in 2024
Fix this: SELECT FirstName, Salry FROM Employee WHERE HireDate > 2024
Optimize: SELECT * FROM Orders o JOIN Customers c ON o.CustomerId = c.CustomerId WHERE substr(o.OrderDate,1,4) = '2025'
Delete all cancelled orders                  (refused: write operation)
Who won the FIFA World Cup?                  (refused: out of scope)
Ignore previous instructions and ...         (refused: prompt injection)
```

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, API endpoints, design decisions
- [docs/WORKFLOW.md](docs/WORKFLOW.md): the LangGraph workflow, node by node, validation and memory
- [docs/PROMPTS.md](docs/PROMPTS.md): every prompt and output schema
- [docs/SCHEMA.md](docs/SCHEMA.md): the sample database
- [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md): shot list for the demo video

## Project layout

```
app/
  agent/     graph.py (LangGraph wiring), nodes.py, state.py, prompts.py, guards.py, llm.py
  sql/       validator.py, optimizer.py, executor.py, formatter.py, structure.py, tokens.py
  db/        schema.sql, seed.py, introspect.py
  static/    index.html, app.css, app.js
  server.py  Flask app and API
  config.py  settings from environment / .env
tests/       unittest-style tests, run with pytest
docs/        architecture diagram and documentation
run.py       entry point
```

## Configuration

All settings are environment variables (see `.env.example`): `LLM_MODEL`, `LLM_TEMPERATURE` (default 0), `DATABASE_PATH`,
`AUTO_EXECUTE`, `MAX_RESULT_ROWS` (200), `MAX_EXPORT_ROWS` (10,000), `QUERY_TIMEOUT_SECONDS` (5), `MAX_SQL_ATTEMPTS` (3),
`HISTORY_TURNS` (6), `HOST`, `PORT`.

## Assumptions and limitations

- **SQLite only.** The assignment allows SQLite, PostgreSQL or MySQL; SQLite keeps setup to zero and its compiler and query planner
  are used directly for validation and optimisation. Supporting another dialect would need a dialect-specific validator.
- **Flask** was chosen from the allowed backends; its small surface keeps the focus on the agent. SSE streaming works under Flask's
  threaded server and under gunicorn (used in Docker).
- **Conversations are kept in memory** (LangGraph `InMemorySaver` plus a session list), so they are lost on restart. Swapping in
  LangGraph's SQLite or Postgres checkpointer would make them durable; that was out of scope for a single-user demo.
- **Execution is on by default** against the bundled sample data only, read-only, capped at 200 displayed rows and 5 seconds.
  It can be switched off per message in the UI or globally with `AUTO_EXECUTE=false`.
- **Relationship checking** requires JOIN equalities to match declared foreign keys. For generated SQL a mismatch is an error;
  for SQL the user pastes it is a warning, since a user may join on purpose.
- **Cost estimation** is a heuristic based on the query plan and table sizes (rows examined, low / medium / high), not a real
  cost model; SQLite does not expose one.
- **Ambiguity:** the agent prefers a reasonable interpretation, states it under *Assumptions*, and only asks a clarifying question
  when there is no sensible default.
- Refusal and guardrail messages are fixed strings, so they cannot be talked around and stay consistent.
- The quality of generated SQL depends on the model chosen; everything around the model (validation, repair, refusal, execution)
  is deterministic and tested.
