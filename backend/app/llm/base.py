"""Provider-neutral LLM interface.

Agents never import a vendor SDK. They ask for one of two things:

* structured(): one call that MUST return an object matching a Pydantic schema
  (implemented as a forced tool call on Claude and Azure OpenAI, validated with Pydantic).
* tool_loop(): an agent loop where the model calls our tools (e.g. read-only SQL) and must
  finish by calling `final_answer`, whose arguments are validated against a schema.

The provider is chosen by OTD_LLM_PROVIDER = mock | claude_foundry | azure_openai.
"""
from __future__ import annotations

import copy
import json
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from ..config import settings
from .prompts import Prompt

T = TypeVar("T", bound=BaseModel)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[[dict], Any]


@dataclass
class CallRecord:
    prompt_ref: str
    provider: str
    model: str
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    steps: int = 1
    ok: bool = True
    retries: int = 0
    repair_reasons: list[str] = field(default_factory=list)
    error: str | None = None
    ts: float = field(default_factory=time.time)


_CALLS: deque[CallRecord] = deque(maxlen=5000)


def log_call(rec: CallRecord) -> None:
    _CALLS.append(rec)
    if settings.llm_log_file:
        p = Path(settings.llm_log_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(rec.__dict__) + "\n")


def recent_calls(n: int = 50) -> list[dict]:
    return [r.__dict__ for r in list(_CALLS)[-n:]][::-1]


class LLMError(RuntimeError):
    pass


class Provider(Protocol):
    name: str

    def model_for(self, tier: str) -> str: ...

    def structured(self, prompt: Prompt, user: str, schema: type[T], context: dict | None = None) -> T: ...

    def tool_loop(self, prompt: Prompt, user: str, tools: list[Tool], final_schema: type[T],
                  context: dict | None = None, max_steps: int = 6) -> tuple[T, list[dict]]: ...


def inline_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema with $defs inlined: both vendors accept it, and it is easier for models to follow."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            return {k: resolve(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    return resolve(schema)


def is_temperature_error(exc: Exception) -> bool:
    return "temperature" in str(exc).lower()
