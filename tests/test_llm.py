import unittest

from app.agent.llm import FallbackLLM, LangChainLLM, LLMError, fill_missing
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


class _ScriptedClient:
    def __init__(self, name, error=None):
        self.model_name, self.error, self.calls = name, error, 0

    def structured(self, system, user, schema):
        self.calls += 1
        if self.error:
            raise LLMError(self.error)
        return {"intent": "generate", "model": self.model_name}

    def text(self, system, user):
        self.calls += 1
        if self.error:
            raise LLMError(self.error)
        return self.model_name


RATE_LIMITED = "Error code: 429 - Rate limit reached for model on tokens per day (TPD)"


class FallbackLLMTests(unittest.TestCase):
    def test_uses_the_next_model_when_the_first_is_rate_limited(self):
        first, second = _ScriptedClient("big", RATE_LIMITED), _ScriptedClient("small")
        llm = FallbackLLM([first, second])
        self.assertEqual(llm.structured("s", "u", INTENT_SCHEMA)["model"], "small")
        self.assertEqual(llm.text("s", "u"), "small")
        self.assertEqual(llm.model_name, "big")

    def test_first_model_is_used_when_it_works(self):
        first, second = _ScriptedClient("big"), _ScriptedClient("small")
        self.assertEqual(FallbackLLM([first, second]).text("s", "u"), "big")
        self.assertEqual(second.calls, 0)

    def test_all_rate_limited_is_reported_as_rate_limit(self):
        llm = FallbackLLM([_ScriptedClient("a", RATE_LIMITED), _ScriptedClient("b", RATE_LIMITED)])
        with self.assertRaises(LLMError) as ctx:
            llm.text("s", "u")
        self.assertTrue(str(ctx.exception).startswith("RATE_LIMIT"))

    def test_other_errors_are_passed_through(self):
        llm = FallbackLLM([_ScriptedClient("a", RATE_LIMITED), _ScriptedClient("b", "Invalid API Key")])
        with self.assertRaises(LLMError) as ctx:
            llm.text("s", "u")
        self.assertIn("Invalid API Key", str(ctx.exception))

    def test_rate_limited_model_rests_during_cooldown(self):
        first, second = _ScriptedClient("big", RATE_LIMITED), _ScriptedClient("small")
        llm = FallbackLLM([first, second], cooldown_seconds=60)
        llm.text("s", "u")
        llm.text("s", "u")
        self.assertEqual(first.calls, 1)    # not retried while resting
        self.assertEqual(second.calls, 2)

    def test_resting_models_are_still_tried_when_nothing_else_works(self):
        first, second = _ScriptedClient("big", RATE_LIMITED), _ScriptedClient("small", RATE_LIMITED)
        llm = FallbackLLM([first, second], cooldown_seconds=60)
        for _ in range(2):
            with self.assertRaises(LLMError):
                llm.text("s", "u")
        self.assertEqual((first.calls, second.calls), (2, 2))

    def test_rate_limit_skips_the_json_schema_retry(self):
        llm = LangChainLLM.__new__(LangChainLLM)
        llm._model = _FakeChatModel()
        llm._model.with_structured_output = lambda schema, method: (
            llm._model.methods.append(method) or _FakeRunnable(RuntimeError(RATE_LIMITED)))
        llm._structured_cache = {}
        with self.assertRaises(LLMError):
            llm.structured("s", "u", INTENT_SCHEMA)
        self.assertEqual(llm._model.methods, ["function_calling"])
