import unittest

from app.agent.llm import LangChainLLM, LLMError, fill_missing
from app.agent.prompts import GENERATE_SCHEMA, INTENT_SCHEMA


class FillMissingTests(unittest.TestCase):
    def test_omitted_optional_style_fields_get_empty_values(self):
        # Models often drop fields that would be empty (seen with gpt-oss on Groq).
        out = fill_missing({"intent": "generate", "reason": "asks for data"}, INTENT_SCHEMA)
        self.assertEqual(out["user_sql"], "")
        self.assertEqual(out["clarifying_question"], "")

    def test_omitted_array_gets_a_fresh_list(self):
        a = fill_missing({"sql": "SELECT 1"}, GENERATE_SCHEMA)
        b = fill_missing({"sql": "SELECT 1"}, GENERATE_SCHEMA)
        self.assertEqual(a["assumptions"], [])
        self.assertIsNot(a["assumptions"], b["assumptions"])

    def test_missing_enum_field_is_an_error(self):
        with self.assertRaises(LLMError):
            fill_missing({"reason": "?"}, INTENT_SCHEMA)


class _FakeRunnable:
    def __init__(self, reply):
        self.reply = reply

    def invoke(self, _messages):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class _FakeChatModel:
    """Answers in prose instead of calling the tool, like gpt-oss on Groq sometimes does."""

    def __init__(self):
        self.methods = []

    def with_structured_output(self, schema, method):
        self.methods.append(method)
        self.schema_sent = schema
        if method == "function_calling":
            return _FakeRunnable(RuntimeError("Tool choice is required, but model did not call a tool"))
        return _FakeRunnable({"intent": "debug", "reason": "fix request"})


class StructuredFallbackTests(unittest.TestCase):
    def make_llm(self):
        llm = LangChainLLM.__new__(LangChainLLM)   # skip provider initialisation
        llm._model = _FakeChatModel()
        llm._structured_cache = {}
        return llm

    def test_falls_back_to_json_schema_when_the_tool_is_not_called(self):
        llm = self.make_llm()
        out = llm.structured("system", "user", INTENT_SCHEMA)
        self.assertEqual(out["intent"], "debug")
        self.assertEqual(out["user_sql"], "")
        self.assertEqual(llm._model.methods, ["function_calling", "json_schema"])
        self.assertNotIn("required", llm._model.schema_sent)

    def test_error_when_every_method_fails(self):
        llm = self.make_llm()
        llm.STRUCTURED_METHODS = ("function_calling",)
        with self.assertRaises(LLMError):
            llm.structured("system", "user", INTENT_SCHEMA)
