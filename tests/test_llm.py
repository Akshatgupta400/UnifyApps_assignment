import unittest

from app.agent.llm import LLMError, fill_missing
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
