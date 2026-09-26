"""HTTP API tests (Flask test client, scripted LLM)."""
import json
import unittest

from app.server import create_app
from tests.fakes import FakeLLM, draft, intent
from tests.helpers import settings


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.llm = FakeLLM()
        self.app = create_app(settings(), llm=self.llm)
        self.client = self.app.test_client()

    def chat(self, message, session_id=None, **extra):
        res = self.client.post("/api/chat", json={"message": message, "session_id": session_id, **extra})
        self.assertEqual(res.status_code, 200, res.data)
        return res.get_json()

    def test_index_and_static_assets(self):
        self.assertIn(b"SQL Assistant", self.client.get("/").data)
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)
        self.assertEqual(self.client.get("/static/app.css").status_code, 200)

    def test_health(self):
        data = self.client.get("/api/health").get_json()
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["llm_ready"])

    def test_schema_endpoint(self):
        tables = {t["name"] for t in self.client.get("/api/schema").get_json()["tables"]}
        self.assertIn("Employees", tables)

    def test_graph_endpoint_returns_mermaid(self):
        self.assertIn("generate_sql", self.client.get("/api/graph").get_json()["mermaid"])

    def test_chat_and_follow_up_share_a_session(self):
        self.llm.queue("IntentDecision", intent("generate"), intent("refine"))
        self.llm.queue("SQLDraft", draft("SELECT * FROM Customers"),
                       draft("SELECT * FROM Customers WHERE State = 'CA'"))
        first = self.chat("Show all customers.")
        sid = first["session_id"]
        second = self.chat("Only those from California.", session_id=sid)
        self.assertEqual(second["session_id"], sid)
        self.assertEqual(second["response"]["results"]["row_count"], 55)
        sessions = self.client.get("/api/sessions").get_json()["sessions"]
        self.assertEqual(sessions[0]["title"], "Show all customers.")
        self.assertEqual(sessions[0]["turns"], 2)
        history = self.client.get(f"/api/sessions/{sid}").get_json()
        self.assertEqual(len(history["messages"]), 4)

    def test_streaming_emits_steps_then_final(self):
        self.llm.queue("IntentDecision", intent("generate"))
        self.llm.queue("SQLDraft", draft("SELECT Name FROM Departments"))
        res = self.client.post("/api/chat/stream", json={"message": "List departments"})
        self.assertEqual(res.mimetype, "text/event-stream")
        events = [block for block in res.get_data(as_text=True).split("\n\n") if block.strip()]
        names = [e.splitlines()[0].removeprefix("event: ") for e in events]
        self.assertEqual(names[-1], "final")
        self.assertIn("step", names)
        final = json.loads(events[-1].splitlines()[1].removeprefix("data: "))
        self.assertEqual(final["response"]["type"], "sql")
        steps = [json.loads(e.splitlines()[1].removeprefix("data: "))["node"] for e in events[:-1]]
        self.assertEqual(steps[0], "guard_input")
        self.assertEqual(steps[-1], "respond")

    def test_out_of_scope_via_api(self):
        self.llm.queue("IntentDecision", intent("out_of_scope"))
        r = self.chat("Who won the FIFA World Cup?")["response"]
        self.assertEqual(r["type"], "refusal")

    def test_empty_message_rejected(self):
        res = self.client.post("/api/chat", json={"message": "  "})
        self.assertEqual(res.status_code, 400)

    def test_csv_export(self):
        res = self.client.post("/api/export/csv", json={"sql": "SELECT DepartmentId, Name FROM Departments"})
        self.assertEqual(res.status_code, 200)
        lines = res.get_data(as_text=True).strip().splitlines()
        self.assertEqual(lines[0], "DepartmentId,Name")
        self.assertEqual(len(lines), 7)

    def test_csv_export_enforces_guardrails(self):
        res = self.client.post("/api/export/csv", json={"sql": "DELETE FROM Orders"})
        self.assertEqual(res.status_code, 400)

    def test_delete_session(self):
        self.llm.queue("IntentDecision", intent("greeting"))
        sid = self.chat("hello")["session_id"]
        self.assertEqual(self.client.delete(f"/api/sessions/{sid}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/sessions/{sid}").status_code, 404)

    def test_app_starts_without_llm_credentials(self):
        app = create_app(settings(llm_model="not-a-provider:nothing"))
        client = app.test_client()
        health = client.get("/api/health").get_json()
        self.assertFalse(health["llm_ready"])
        r = client.post("/api/chat", json={"message": "Show all customers"}).get_json()["response"]
        self.assertEqual(r["type"], "error")


if __name__ == "__main__":
    unittest.main()
