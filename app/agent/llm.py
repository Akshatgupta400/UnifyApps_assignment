"""LLM access through LangChain.

The graph nodes only depend on the small ``LLMClient`` protocol, so the model
provider is a configuration choice (``LLM_MODEL=openai:gpt-4o-mini``,
``groq:openai/gpt-oss-120b``, ``anthropic:...``, ``google_genai:...``) and tests
can swap in a scripted fake without any network access.
"""
from __future__ import annotations

import time
from typing import Any, Protocol


class LLMError(RuntimeError):
    """The model could not be reached or returned something unusable."""


def is_rate_limit(exc: BaseException) -> bool:
    """True for a provider's "too many requests / quota used up" error (HTTP 429)."""
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "rate_limit" in text


_EMPTY = {"string": "", "array": [], "object": {}, "boolean": False}


def fill_missing(result: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Give omitted ``required`` fields an empty value of their type.

    An enum field has no safe empty value, so leaving one out is still an error.
    """
    result = dict(result)
    for name in schema.get("required", []):
        if name in result:
            continue
        spec = schema.get("properties", {}).get(name, {})
        if "enum" in spec or spec.get("type") not in _EMPTY:
            raise LLMError(f"The language model response is missing the '{name}' field.")
        result[name] = type(_EMPTY[spec["type"]])()
    return result


class LLMClient(Protocol):
    def structured(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Return a dict matching the JSON ``schema``."""

    def text(self, system: str, user: str) -> str:
        """Return a plain-text completion."""


class LangChainLLM:
    """``LLMClient`` backed by any LangChain chat model."""

    def __init__(self, model: str, temperature: float = 0.0, max_retries: int | None = None) -> None:
        try:
            from langchain.chat_models import init_chat_model
        except ImportError as exc:  # pragma: no cover - dependency missing
            raise LLMError("LangChain is not installed. Run: pip install -r requirements.txt") from exc
        try:
            # Only arguments every provider accepts, so any "provider:model" works.
            kwargs: dict[str, Any] = {"temperature": temperature}
            if max_retries is not None:   # SDKs retry 429s themselves, waiting up to a minute
                kwargs["max_retries"] = max_retries
            self._model = init_chat_model(model, **kwargs)
        except Exception as exc:  # missing provider package or API key
            raise LLMError(f"Could not initialise model '{model}': {exc}") from exc
        self.model_name = model
        self._structured_cache: dict[str, Any] = {}

    @staticmethod
    def _messages(system: str, user: str):
        from langchain_core.messages import HumanMessage, SystemMessage
        return [SystemMessage(content=system), HumanMessage(content=user)]

    # Tool/function calling is the most widely supported structured-output mode
    # (OpenAI, Anthropic, Groq, Gemini, Ollama), but a model can still answer in
    # prose instead of calling the tool. The provider's native JSON-schema mode
    # constrains decoding to the schema, so it is the fallback.
    STRUCTURED_METHODS = ("function_calling", "json_schema")

    def _structured_runnable(self, schema: dict[str, Any], method: str):
        key = (schema["title"], method)
        if key not in self._structured_cache:
            # "required" is enforced locally by fill_missing: some providers (e.g.
            # Groq) reject the whole reply when a model omits a field that would be empty.
            relaxed = {k: v for k, v in schema.items() if k != "required"}
            self._structured_cache[key] = self._model.with_structured_output(relaxed, method=method)
        return self._structured_cache[key]

    def structured(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        last_exc: Exception | None = None
        for method in self.STRUCTURED_METHODS:
            try:
                result = self._structured_runnable(schema, method).invoke(self._messages(system, user))
            except Exception as exc:   # includes providers that don't support a method
                last_exc = exc
                if is_rate_limit(exc):
                    break              # another method would hit the same limit
                continue
            if isinstance(result, dict):
                return fill_missing(result, schema)
        if last_exc is not None:
            raise LLMError(f"The language model request failed: {last_exc}") from last_exc
        raise LLMError("The language model returned an unexpected response format.")

    def text(self, system: str, user: str) -> str:
        try:
            reply = self._model.invoke(self._messages(system, user))
        except Exception as exc:
            raise LLMError(f"The language model request failed: {exc}") from exc
        content = reply.content
        if isinstance(content, list):   # some providers return content blocks
            content = "".join(part.get("text", "") if isinstance(part, dict) else str(part)
                              for part in content)
        return str(content).strip()


class FallbackLLM:
    """Tries each ``LLMClient`` in order, moving on when one fails.

    Free tiers have per-model daily quotas, so a second model keeps the agent
    answering when the first one's quota is used up. A rate-limited model is
    skipped for ``cooldown_seconds`` so requests don't keep paying for a call
    that will fail.
    """

    def __init__(self, clients: list[LLMClient], cooldown_seconds: float = 60.0) -> None:
        if not clients:
            raise ValueError("FallbackLLM needs at least one client")
        self.clients = clients
        self.model_name = getattr(clients[0], "model_name", "")
        self.cooldown_seconds = cooldown_seconds
        self._resting_until = [0.0] * len(clients)

    def _call(self, method: str, *args) -> Any:
        errors: list[LLMError] = []
        now = time.monotonic()
        # Resting clients go last rather than being dropped, so a request still
        # gets an answer if every client is resting.
        order = sorted(range(len(self.clients)), key=lambda i: self._resting_until[i] > now)
        for i in order:
            try:
                return getattr(self.clients[i], method)(*args)
            except LLMError as exc:
                errors.append(exc)
                if is_rate_limit(exc):
                    self._resting_until[i] = time.monotonic() + self.cooldown_seconds
        if all(is_rate_limit(e) for e in errors):
            raise LLMError("RATE_LIMIT: every configured model has reached its usage limit. "
                           f"Last error: {errors[-1]}") from errors[-1]
        raise errors[-1]

    def structured(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        return self._call("structured", system, user, schema)

    def text(self, system: str, user: str) -> str:
        return self._call("text", system, user)
