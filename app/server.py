"""HTTP API and static web UI (Flask).

Endpoints
  GET    /                         web UI
  GET    /api/health               status, model name, whether the LLM is ready
  GET    /api/schema               tables, columns, relationships
  GET    /api/graph                the LangGraph workflow as Mermaid text
  GET    /api/sessions             conversation list (newest first)
  POST   /api/sessions             start a new conversation
  GET    /api/sessions/<id>        one conversation's messages
  DELETE /api/sessions/<id>        delete a conversation
  POST   /api/chat                 {session_id, message, execute} -> response JSON
  POST   /api/chat/stream          same, as Server-Sent Events (step..., final)
  POST   /api/export/csv           {sql} -> CSV of the query's results
"""
from __future__ import annotations

import csv
import io
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator

from flask import Flask, Response, jsonify, request, send_from_directory

from app.agent.graph import NODE_LABELS, build_graph
from app.agent.llm import FallbackLLM, LangChainLLM, LLMClient, LLMError
from app.agent.state import new_turn
from app.config import ROOT, Settings
from app.db.introspect import load_schema
from app.db.seed import ensure_database
from app.sql.executor import execute_query
from app.sql.validator import validate_sql

STATIC_DIR = ROOT / "app" / "static"


@dataclass
class Session:
    id: str
    title: str = "New conversation"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    messages: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def summary(self) -> dict:
        return {"id": self.id, "title": self.title, "created_at": self.created_at,
                "updated_at": self.updated_at, "turns": len(self.messages) // 2}


class SessionStore:
    """In-memory conversation list for the UI; LangGraph's checkpointer holds agent memory."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self) -> Session:
        with self._lock:
            session = Session(id=uuid.uuid4().hex[:12])
            self._sessions[session.id] = session
            return session

    def get_or_create(self, session_id: str | None) -> Session:
        with self._lock:
            if session_id and session_id in self._sessions:
                return self._sessions[session_id]
        session = self.create()
        if session_id and len(session_id) <= 64 and session_id.replace("-", "").isalnum():
            with self._lock:   # keep the client's id so it can resume the thread
                del self._sessions[session.id]
                session.id = session_id
                self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def delete(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def list(self) -> list[dict]:
        return sorted((s.summary() for s in self._sessions.values()),
                      key=lambda s: s["updated_at"], reverse=True)


def create_app(settings: Settings | None = None, llm: LLMClient | None = None) -> Flask:
    settings = settings or Settings.from_env()
    ensure_database(settings.database_path)
    schema = load_schema(settings.database_path)

    llm_error = ""
    if llm is None:
        try:
            # With a fallback configured, fail fast instead of letting the SDK sit
            # out a rate limit; the fallback model answers instead.
            llm = LangChainLLM(settings.llm_model, settings.llm_temperature,
                               max_retries=0 if settings.llm_fallback_models else None)
        except LLMError as exc:
            llm_error = str(exc)   # the UI still loads and shows how to fix this
        else:
            fallbacks = []
            last = len(settings.llm_fallback_models) - 1
            for i, name in enumerate(settings.llm_fallback_models):
                try:
                    fallbacks.append(LangChainLLM(name, settings.llm_temperature,
                                                  max_retries=None if i == last else 0))
                except LLMError:   # a broken fallback must not take the main model down
                    logging.getLogger(__name__).warning("Fallback model %s could not be initialised", name)
            if fallbacks:
                llm = FallbackLLM([llm, *fallbacks])
    graph = build_graph(llm, schema, settings, llm_error=llm_error)
    sessions = SessionStore()

    app = Flask(__name__, static_folder=None)
    app.config.update(SETTINGS=settings, GRAPH=graph, SESSIONS=sessions)

    # ---------------------------------------------------------------- helpers
    def bad_request(message: str, status: int = 400):
        return jsonify({"error": message}), status

    def parse_chat_request():
        body = request.get_json(silent=True) or {}
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            return None, bad_request("Please type a message.")
        execute = body.get("execute", settings.auto_execute)
        session = sessions.get_or_create(body.get("session_id"))
        return (session, message, bool(execute)), None

    def record(session: Session, message: str, response: dict) -> None:
        if not session.messages:
            session.title = message.strip().splitlines()[0][:60]
        session.messages.append({"role": "user", "content": message, "at": time.time()})
        session.messages.append({"role": "assistant", "response": response, "at": time.time()})
        session.updated_at = time.time()

    def config_for(session: Session) -> dict:
        return {"configurable": {"thread_id": session.id}}

    # ----------------------------------------------------------------- routes
    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:filename>")
    def static_files(filename: str):
        return send_from_directory(STATIC_DIR, filename)

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok", "model": settings.llm_model, "llm_ready": llm is not None,
                        "llm_error": llm_error, "database": settings.database_path.name,
                        "dialect": "SQLite"})

    @app.get("/api/schema")
    def get_schema():
        return jsonify(schema.to_dict())

    @app.get("/api/graph")
    def get_graph():
        return jsonify({"mermaid": graph.get_graph().draw_mermaid()})

    @app.get("/api/sessions")
    def list_sessions():
        return jsonify({"sessions": sessions.list()})

    @app.post("/api/sessions")
    def new_session():
        return jsonify(sessions.create().summary()), 201

    @app.get("/api/sessions/<session_id>")
    def get_session(session_id: str):
        session = sessions.get(session_id)
        if not session:
            return bad_request("Conversation not found.", 404)
        return jsonify({**session.summary(), "messages": session.messages})

    @app.delete("/api/sessions/<session_id>")
    def delete_session(session_id: str):
        if not sessions.delete(session_id):
            return bad_request("Conversation not found.", 404)
        return jsonify({"deleted": session_id})

    @app.post("/api/chat")
    def chat():
        parsed, error = parse_chat_request()
        if error:
            return error
        session, message, execute = parsed
        with session.lock:   # one turn at a time per conversation
            try:
                state = graph.invoke(new_turn(message, execute), config_for(session))
                response = state["response"]
            except Exception as exc:  # never leak a stack trace to the UI
                app.logger.exception("chat turn failed")
                response = {"type": "error", "message": f"Something went wrong while processing "
                                                        f"that request: {exc}"}
            record(session, message, response)
        return jsonify({"session_id": session.id, "title": session.title, "response": response})

    @app.post("/api/chat/stream")
    def chat_stream():
        parsed, error = parse_chat_request()
        if error:
            return error
        session, message, execute = parsed

        def sse(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"

        def events() -> Iterator[str]:
            with session.lock:
                response: dict | None = None
                try:
                    for update in graph.stream(new_turn(message, execute), config_for(session),
                                               stream_mode="updates"):
                        for node, _ in update.items():
                            yield sse("step", {"node": node, "label": NODE_LABELS.get(node, node)})
                    response = graph.get_state(config_for(session)).values.get("response")
                except Exception as exc:
                    app.logger.exception("chat stream failed")
                    response = {"type": "error", "message": f"Something went wrong while processing "
                                                            f"that request: {exc}"}
                response = response or {"type": "error", "message": "No response was produced."}
                record(session, message, response)
                yield sse("final", {"session_id": session.id, "title": session.title,
                                    "response": response})

        return Response(events(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/export/csv")
    def export_csv():
        body = request.get_json(silent=True) or {}
        sql = body.get("sql", "")
        check = validate_sql(sql, settings.database_path, schema, strict_relationships=False)
        if not check.ok:   # exports go through the same guardrails as chat
            return bad_request(" ".join(check.errors))
        result = execute_query(check.sql, settings.database_path, settings.max_export_rows,
                               settings.query_timeout_seconds)
        if result.error:
            return bad_request(result.error)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(result.columns)
        writer.writerows(result.rows)
        return Response(buffer.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=query_results.csv"})

    return app
