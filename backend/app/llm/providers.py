"""Vendor implementations of the Provider interface.

ClaudeFoundryProvider  - Anthropic Claude deployed in Microsoft Foundry (AnthropicFoundry client).
AzureOpenAIProvider    - Azure OpenAI / Foundry OpenAI v1 endpoint (OpenAI client with base_url).
MockProvider           - deterministic, offline; see mock.py.

Both real providers:
* force a tool call for structured output, then validate with Pydantic (one repair retry on failure);
* send the system prompt with prompt caching (Claude) so long few-shot prompts are cheap;
* retry once without `temperature` if a model rejects it;
* log prompt version, model, tokens and latency for every call.
"""
from __future__ import annotations

import json
import time
from functools import lru_cache

from pydantic import BaseModel, ValidationError

from ..config import settings
from .base import (
    CallRecord,
    LLMError,
    Provider,
    T,
    Tool,
    inline_schema,
    is_temperature_error,
    log_call,
)
from .prompts import Prompt

FINAL_TOOL = "final_answer"
OUTPUT_TOOL = "record_output"


def _reasons(err: ValidationError) -> list[str]:
    """Short labels like "extra_forbidden delivery_address.zip" for the eval report."""
    return [f"{e['type']} {'.'.join(str(x) for x in e['loc'])}" for e in err.errors()][:5]


def _repair_msg(err: ValidationError) -> str:
    return ("Your last tool call did not match the schema: "
            f"{err.errors(include_url=False)[:5]}. Call the tool again with corrected arguments only.")


# ---------------- Claude on Microsoft Foundry ----------------

class ClaudeFoundryProvider:
    name = "claude_foundry"

    def __init__(self, client=None):
        if client is None:
            from anthropic import AnthropicFoundry  # reads ANTHROPIC_FOUNDRY_API_KEY + _RESOURCE or _BASE_URL
            client = AnthropicFoundry(timeout=settings.llm_timeout_s, max_retries=2)
        self.client = client
        self._no_temperature: set[str] = set()
        self._no_forced_tools: set[str] = set()

    def model_for(self, tier: str) -> str:
        return settings.claude_smart_model if tier == "smart" else settings.claude_fast_model

    def _create(self, prompt: Prompt, **kw):
        """messages.create with compatibility fallbacks, learned once per model:
        - a model that rejects `temperature` (e.g. with extended thinking on) is retried without it;
        - a model that rejects a forced tool_choice while thinking is retried with tool_choice auto
          (the caller then nudges the model to use the tool if it answers in plain text)."""
        model = kw.setdefault("model", self.model_for(prompt.tier))
        kw.setdefault("max_tokens", prompt.max_tokens)
        kw["system"] = [{"type": "text", "text": prompt.system, "cache_control": {"type": "ephemeral"}}]
        if settings.llm_send_temperature and model not in self._no_temperature:
            kw["temperature"] = prompt.temperature
        if model in self._no_forced_tools and kw.get("tool_choice", {}).get("type") in ("tool", "any"):
            kw["tool_choice"] = {"type": "auto"}
        for _ in range(3):
            try:
                return self.client.messages.create(**kw)
            except Exception as exc:
                msg = str(exc).lower()
                if "temperature" in kw and is_temperature_error(exc):
                    kw.pop("temperature")
                    self._no_temperature.add(model)
                    continue
                if "thinking" in msg and "tool" in msg and kw.get("tool_choice", {}).get("type") in ("tool", "any"):
                    kw["tool_choice"] = {"type": "auto"}
                    self._no_forced_tools.add(model)
                    continue
                raise
        return self.client.messages.create(**kw)

    @staticmethod
    def _usage(resp) -> tuple[int, int, int]:
        u = getattr(resp, "usage", None)
        return (getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0,
                getattr(u, "cache_read_input_tokens", 0) or 0)

    @staticmethod
    def _blocks(resp) -> list[dict]:
        return [b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else dict(b) for b in resp.content]

    def structured(self, prompt: Prompt, user: str, schema: type[T], context: dict | None = None) -> T:
        tool = {"name": OUTPUT_TOOL, "description": f"Return the {schema.__name__} result.",
                "input_schema": inline_schema(schema)}
        messages: list[dict] = [{"role": "user", "content": user}]
        t0, tin, tout, tcache = time.time(), 0, 0, 0
        reasons: list[str] = []
        for attempt in range(3):
            resp = self._create(prompt, messages=messages, tools=[tool],
                                tool_choice={"type": "tool", "name": OUTPUT_TOOL})
            i, o, c = self._usage(resp)
            tin, tout, tcache = tin + i, tout + o, tcache + c
            call = next((b for b in self._blocks(resp) if b.get("type") == "tool_use"), None)
            if call is None:  # answered in text (tool not forced): ask once more for the tool call
                messages += [{"role": "assistant", "content": self._blocks(resp)},
                             {"role": "user", "content": f"Now give your result by calling the {OUTPUT_TOOL} tool."}]
                continue
            try:
                out = schema.model_validate(call["input"])
                log_call(CallRecord(prompt.ref, self.name, self.model_for(prompt.tier),
                                    int((time.time() - t0) * 1000), tin, tout, tcache, attempt + 1, retries=attempt,
                                    repair_reasons=reasons))
                return out
            except ValidationError as err:
                reasons += _reasons(err)
                messages += [{"role": "assistant", "content": self._blocks(resp)},
                             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"],
                                                           "content": _repair_msg(err), "is_error": True}]}]
        log_call(CallRecord(prompt.ref, self.name, self.model_for(prompt.tier),
                            int((time.time() - t0) * 1000), tin, tout, tcache, 2, ok=False, error="schema"))
        raise LLMError("no valid structured output after 3 attempts")

    def tool_loop(self, prompt: Prompt, user: str, tools: list[Tool], final_schema: type[T],
                  context: dict | None = None, max_steps: int = 6) -> tuple[T, list[dict]]:
        specs = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
        specs.append({"name": FINAL_TOOL, "description": "Give the final answer. Always finish with this tool.",
                      "input_schema": inline_schema(final_schema)})
        by_name = {t.name: t for t in tools}
        messages: list[dict] = [{"role": "user", "content": user}]
        trace: list[dict] = []
        t0, tin, tout, tcache = time.time(), 0, 0, 0
        for step in range(1, max_steps + 1):
            resp = self._create(prompt, messages=messages, tools=specs, tool_choice={"type": "any"})
            i, o, c = self._usage(resp)
            tin, tout, tcache = tin + i, tout + o, tcache + c
            blocks = self._blocks(resp)
            messages.append({"role": "assistant", "content": blocks})
            results = []
            for b in (b for b in blocks if b.get("type") == "tool_use"):
                if b["name"] == FINAL_TOOL:
                    try:
                        out = final_schema.model_validate(b["input"])
                    except ValidationError as err:
                        results.append({"type": "tool_result", "tool_use_id": b["id"],
                                        "content": _repair_msg(err), "is_error": True})
                        continue
                    log_call(CallRecord(prompt.ref, self.name, self.model_for(prompt.tier),
                                        int((time.time() - t0) * 1000), tin, tout, tcache, step))
                    return out, trace
                tool = by_name.get(b["name"])
                try:
                    result = tool.fn(b["input"]) if tool else {"error": f"unknown tool {b['name']}"}
                except Exception as exc:  # tool errors go back to the model so it can fix its query
                    result = {"error": str(exc)}
                trace.append({"tool": b["name"], "input": b["input"], "output": result})
                results.append({"type": "tool_result", "tool_use_id": b["id"],
                                "content": json.dumps(result, default=str)[:20000]})
            # no tool call (tool_choice auto with a thinking model): nudge it to continue with a tool
            messages.append({"role": "user", "content": results or
                             f"Continue: call run_sql to query the data, or {FINAL_TOOL} to answer."})
        log_call(CallRecord(prompt.ref, self.name, self.model_for(prompt.tier),
                            int((time.time() - t0) * 1000), tin, tout, tcache, max_steps, ok=False, error="max_steps"))
        raise LLMError(f"no final answer after {max_steps} steps")


# ---------------- Azure OpenAI (Foundry v1 endpoint) ----------------

class AzureOpenAIProvider:
    name = "azure_openai"

    def __init__(self, client=None):
        if client is None:
            from openai import OpenAI
            if not settings.aoai_base_url:
                raise LLMError("set OTD_AOAI_BASE_URL, e.g. https://<resource>.openai.azure.com/openai/v1")
            client = OpenAI(base_url=settings.aoai_base_url, api_key=settings.aoai_api_key,
                            timeout=settings.llm_timeout_s, max_retries=2)
        self.client = client

    def model_for(self, tier: str) -> str:
        return settings.aoai_smart_deployment if tier == "smart" else settings.aoai_fast_deployment

    def _create(self, prompt: Prompt, messages: list[dict], **kw):
        kw.update(model=self.model_for(prompt.tier), max_completion_tokens=prompt.max_tokens,
                  messages=[{"role": "system", "content": prompt.system}, *messages])
        if settings.llm_send_temperature:
            try:
                return self.client.chat.completions.create(temperature=prompt.temperature, **kw)
            except Exception as exc:
                if not is_temperature_error(exc):
                    raise
        return self.client.chat.completions.create(**kw)

    @staticmethod
    def _usage(resp) -> tuple[int, int, int]:
        u = getattr(resp, "usage", None)
        details = getattr(u, "prompt_tokens_details", None)
        return (getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0,
                getattr(details, "cached_tokens", 0) or 0)

    @staticmethod
    def _assistant(msg) -> dict:
        return {"role": "assistant", "content": msg.content or "",
                "tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.function.name, "arguments": c.function.arguments}}
                               for c in (msg.tool_calls or [])]}

    def structured(self, prompt: Prompt, user: str, schema: type[T], context: dict | None = None) -> T:
        tool = {"type": "function", "function": {"name": OUTPUT_TOOL, "description": f"Return the {schema.__name__}.",
                                                 "parameters": inline_schema(schema)}}
        messages: list[dict] = [{"role": "user", "content": user}]
        t0, tin, tout, tcache = time.time(), 0, 0, 0
        reasons: list[str] = []
        for attempt in range(2):
            resp = self._create(prompt, messages, tools=[tool],
                                tool_choice={"type": "function", "function": {"name": OUTPUT_TOOL}})
            i, o, c = self._usage(resp)
            tin, tout, tcache = tin + i, tout + o, tcache + c
            msg = resp.choices[0].message
            if not msg.tool_calls:
                raise LLMError("model returned no tool call")
            call = msg.tool_calls[0]
            try:
                out = schema.model_validate_json(call.function.arguments)
                log_call(CallRecord(prompt.ref, self.name, self.model_for(prompt.tier),
                                    int((time.time() - t0) * 1000), tin, tout, tcache, attempt + 1, retries=attempt,
                                    repair_reasons=reasons))
                return out
            except ValidationError as err:
                reasons += _reasons(err)
                messages += [self._assistant(msg), {"role": "tool", "tool_call_id": call.id, "content": _repair_msg(err)}]
        log_call(CallRecord(prompt.ref, self.name, self.model_for(prompt.tier),
                            int((time.time() - t0) * 1000), tin, tout, tcache, 2, ok=False, error="schema"))
        raise LLMError("output failed schema validation twice")

    def tool_loop(self, prompt: Prompt, user: str, tools: list[Tool], final_schema: type[T],
                  context: dict | None = None, max_steps: int = 6) -> tuple[T, list[dict]]:
        specs = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                   "parameters": t.parameters}} for t in tools]
        specs.append({"type": "function", "function": {"name": FINAL_TOOL, "description": "Give the final answer.",
                                                       "parameters": inline_schema(final_schema)}})
        by_name = {t.name: t for t in tools}
        messages: list[dict] = [{"role": "user", "content": user}]
        trace: list[dict] = []
        t0, tin, tout, tcache = time.time(), 0, 0, 0
        for step in range(1, max_steps + 1):
            resp = self._create(prompt, messages, tools=specs, tool_choice="required")
            i, o, c = self._usage(resp)
            tin, tout, tcache = tin + i, tout + o, tcache + c
            msg = resp.choices[0].message
            messages.append(self._assistant(msg))
            for call in msg.tool_calls or []:
                args = json.loads(call.function.arguments or "{}")
                if call.function.name == FINAL_TOOL:
                    try:
                        out = final_schema.model_validate(args)
                    except ValidationError as err:
                        messages.append({"role": "tool", "tool_call_id": call.id, "content": _repair_msg(err)})
                        continue
                    log_call(CallRecord(prompt.ref, self.name, self.model_for(prompt.tier),
                                        int((time.time() - t0) * 1000), tin, tout, tcache, step))
                    return out, trace
                tool = by_name.get(call.function.name)
                try:
                    result = tool.fn(args) if tool else {"error": f"unknown tool {call.function.name}"}
                except Exception as exc:
                    result = {"error": str(exc)}
                trace.append({"tool": call.function.name, "input": args, "output": result})
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": json.dumps(result, default=str)[:20000]})
        log_call(CallRecord(prompt.ref, self.name, self.model_for(prompt.tier),
                            int((time.time() - t0) * 1000), tin, tout, tcache, max_steps, ok=False, error="max_steps"))
        raise LLMError(f"no final answer after {max_steps} steps")


@lru_cache(maxsize=4)
def get_provider(name: str | None = None) -> Provider:
    name = name or settings.llm_provider
    if name == "claude_foundry":
        return ClaudeFoundryProvider()
    if name == "azure_openai":
        return AzureOpenAIProvider()
    if name == "mock":
        from .mock import MockProvider
        return MockProvider()
    raise LLMError(f"unknown OTD_LLM_PROVIDER '{name}' (use mock, claude_foundry or azure_openai)")


__all__ = ["get_provider", "ClaudeFoundryProvider", "AzureOpenAIProvider", "FINAL_TOOL", "OUTPUT_TOOL", "BaseModel"]
