"""LLM clients with JSON-schema output and token/cost accounting.

Both clients expose the same interface, so the evaluator does not care which is used:

    client.generate_json(system, prompt, schema) -> dict   # schema: standard JSON Schema, object root
    client.usage                                         # Usage (calls, tokens, cost)
    client.model

ClaudeClient uses the official Anthropic SDK. GeminiClient calls the Gemini REST API directly.
The Jev backend (justnews/jev.py) is not a text generator; the evaluator calls it differently.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

import anthropic
import requests

log = logging.getLogger("justnews.llm")

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
# Request rejected for a reason a retry cannot fix (bad key, model, billing or request).
FATAL_STATUS = (400, 401, 402, 403, 404)
# Claude list prices (USD per 1M input, output tokens), for the usage report.
PROVIDER_PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00), "claude-opus-5-5": (4.00, 20.00)}


class LLMError(Exception):
    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal  # retrying (or trying the next batch) cannot help


@dataclass
class Usage:
    model: str
    price_input_per_m: float
    price_output_per_m: float
    calls: int = 0
    failed_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0  # includes thinking tokens, which both providers bill as output

    @property
    def cost_usd(self) -> float:
        return (self.input_tokens * self.price_input_per_m + self.output_tokens * self.price_output_per_m) / 1e6

    def as_dict(self) -> dict:
        return {"model": self.model, "calls": self.calls, "failed_calls": self.failed_calls,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cost_usd": round(self.cost_usd, 6)}


def _closed(schema):
    """The schema with additionalProperties false on every object, which Claude's structured
    outputs require (the shared schemas leave it out, as Gemini doesn't need it)."""
    if isinstance(schema, list):
        return [_closed(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: _closed(v) for k, v in schema.items()}
    if out.get("type") == "object":
        out.setdefault("additionalProperties", False)
    return out


class ClaudeClient:
    def __init__(self, api_key: str, model: str, price_input_per_m: float, price_output_per_m: float,
                 effort: str = "low", base_url: str = "https://api.anthropic.com",
                 max_tokens: int = 16000, timeout: float = 120.0):
        if not api_key:
            raise LLMError("CLAUDE_API_KEY is not set (add it to .env or the environment)", fatal=True)
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        # The SDK retries connection errors, 408, 409, 429 and 5xx with backoff.
        self.client = anthropic.Anthropic(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=3)
        self.usage = Usage(model, price_input_per_m, price_output_per_m)

    def generate_json(self, system: str, prompt: str, schema: dict) -> dict:
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": _closed(schema)}},
            )
        except anthropic.APIStatusError as exc:
            self.usage.failed_calls += 1
            raise LLMError(f"HTTP {exc.status_code}: {exc.message}", fatal=exc.status_code in FATAL_STATUS) from exc
        except anthropic.APIConnectionError as exc:
            self.usage.failed_calls += 1
            raise LLMError(f"connection error: {exc}") from exc

        self.usage.calls += 1
        self.usage.input_tokens += response.usage.input_tokens
        self.usage.output_tokens += response.usage.output_tokens
        if response.stop_reason in ("refusal", "max_tokens"):
            raise LLMError(f"incomplete response (stop_reason={response.stop_reason})")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"invalid JSON from model: {exc}") from exc


def _gemini_quota_info(resp: requests.Response) -> tuple[float | None, bool]:
    """From a Gemini 429, return (seconds Google asks us to wait, whether a per-day quota is exhausted)."""
    try:
        details = resp.json()["error"].get("details", [])
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, False
    delay, daily = None, False
    for d in details:
        kind = d.get("@type", "")
        if kind.endswith("RetryInfo") and str(d.get("retryDelay", "")).endswith("s"):
            try:
                delay = float(d["retryDelay"][:-1])
            except ValueError:
                pass
        if kind.endswith("QuotaFailure"):
            daily = daily or any("PerDay" in v.get("quotaId", "") for v in d.get("violations", []))
    return delay, daily


class GeminiClient:
    # Free-tier limits are per project and per model; space out requests across all clients.
    _last_call: dict[str, float] = {}

    def __init__(self, api_key: str, model: str, price_input_per_m: float, price_output_per_m: float,
                 thinking_level: str = "", timeout: int = 90, retries: int = 3, min_interval: float = 0.0,
                 temperature: float = 0.2):
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set (add it to .env or the environment)", fatal=True)
        self.model = model
        self.thinking_level = thinking_level
        self.timeout = timeout
        self.retries = retries
        self.min_interval = min_interval
        self.temperature = temperature
        self.session = requests.Session()
        # Key goes in a header, never in the URL, so it cannot leak into logs.
        self.session.headers.update({"x-goog-api-key": api_key, "Content-Type": "application/json"})
        self.usage = Usage(model, price_input_per_m, price_output_per_m)

    def generate_json(self, system: str, prompt: str, schema: dict) -> dict:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": self.temperature,
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
            },
        }
        if self.thinking_level and self.model.startswith("gemini-3"):
            body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": self.thinking_level}
        url = f"{GEMINI_API_BASE}/models/{self.model}:generateContent"
        last_error = ""
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                resp = self.session.post(url, json=body, timeout=self.timeout)
            except requests.RequestException as exc:
                resp, last_error = None, f"{type(exc).__name__}: {exc}"
            if resp is not None and resp.status_code == 200:
                try:
                    data = resp.json()
                    self._track(data.get("usageMetadata", {}))
                    return self._parse(data)
                except (LLMError, json.JSONDecodeError) as exc:
                    last_error = f"bad response: {exc}"
            elif resp is not None:
                last_error = self._error_message(resp)
                if resp.status_code not in (429, 500, 502, 503, 504):
                    self.usage.failed_calls += 1
                    raise LLMError(last_error, fatal=resp.status_code in FATAL_STATUS)
                if resp.status_code == 429:
                    delay, daily = _gemini_quota_info(resp)
                    if daily:  # free daily quota for this model is gone until midnight Pacific
                        self.usage.failed_calls += 1
                        raise LLMError(f"daily free-tier quota exhausted for {self.model}: {last_error}", fatal=True)
                    if delay is not None and attempt < self.retries:
                        wait = min(delay + 1, 90)
                        log.warning("Gemini %s per-minute limit hit; waiting %.0fs as requested", self.model, wait)
                        time.sleep(wait)
                        continue
            if attempt < self.retries:
                wait = 2 ** (attempt + 1)
                log.warning("Gemini call failed (%s); retrying in %ss", last_error[:120], wait)
                time.sleep(wait)
        self.usage.failed_calls += 1
        raise LLMError(last_error)

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        wait = GeminiClient._last_call.get(self.model, 0.0) + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        GeminiClient._last_call[self.model] = time.monotonic()

    @staticmethod
    def _error_message(resp: requests.Response) -> str:
        try:
            return f"HTTP {resp.status_code}: {resp.json()['error']['message']}"
        except (ValueError, KeyError, TypeError):
            return f"HTTP {resp.status_code}: {resp.text[:300]}"

    def _track(self, meta: dict) -> None:
        self.usage.calls += 1
        self.usage.input_tokens += meta.get("promptTokenCount", 0)
        self.usage.output_tokens += meta.get("candidatesTokenCount", 0) + meta.get("thoughtsTokenCount", 0)

    @staticmethod
    def _parse(data: dict) -> dict:
        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMError(f"no candidates (promptFeedback={data.get('promptFeedback')})")
        parts = candidates[0].get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text:
            raise LLMError(f"empty response (finishReason={candidates[0].get('finishReason')})")
        return json.loads(text)


def claude_fallback(settings, timeout: float = 60.0) -> list[ClaudeClient]:
    """The paid Claude model tried after the free Gemini ones, or none (no key, or turned off)."""
    model = settings.claude_fallback_model
    if not settings.claude_api_key or model in ("", "off"):
        return []
    prices = PROVIDER_PRICES.get(model, (0.0, 0.0))
    return [ClaudeClient(settings.claude_api_key, model, *prices, effort="low",
                         base_url=settings.claude_base_url, max_tokens=16000, timeout=timeout)]


def make_llm_client(settings) -> ClaudeClient | GeminiClient:
    """Build the scorer for the configured provider (LLM_PROVIDER=claude|gemini|jev)."""
    if settings.llm_provider == "claude":
        return ClaudeClient(settings.claude_api_key, settings.llm_model, settings.price_input_per_m,
                            settings.price_output_per_m, effort=settings.claude_effort,
                            base_url=settings.claude_base_url)
    if settings.llm_provider == "gemini":
        return GeminiClient(settings.gemini_api_key, settings.llm_model, settings.price_input_per_m,
                            settings.price_output_per_m, thinking_level=settings.gemini_thinking_level)
    if settings.llm_provider == "jev":
        from .jev import JevScorer  # imported lazily: jev.py builds on this module
        return JevScorer(settings.typesafe_api_key, settings.llm_model, settings.price_input_per_m)
    raise LLMError(f"Unknown LLM_PROVIDER {settings.llm_provider!r} (use claude, gemini or jev)", fatal=True)
