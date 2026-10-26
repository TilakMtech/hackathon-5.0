"""Versioned prompt files.

Each prompt lives in app/prompts/<id>.md:

    ---
    id: p1_email_extraction
    version: 1.0.0
    tier: fast            # fast -> Haiku / gpt-4.1-mini, smart -> Sonnet / gpt-4.1
    temperature: 0
    max_tokens: 1500
    ---
    <system prompt>
    === USER ===
    <user template with {{placeholders}}>

Prompts are code: reviewed in pull requests, versioned, and every LLM call logs id + version + hash.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
_PLACEHOLDER = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")


@dataclass(frozen=True)
class Prompt:
    id: str
    version: str
    tier: str
    temperature: float
    max_tokens: int
    system: str
    user_template: str
    sha: str

    def render_user(self, **values: object) -> str:
        missing = set(_PLACEHOLDER.findall(self.user_template)) - set(values)
        if missing:
            raise KeyError(f"prompt {self.id} needs values for {sorted(missing)}")
        return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), self.user_template)

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}#{self.sha}"


def _parse(text: str) -> Prompt:
    if not text.startswith("---"):
        raise ValueError("prompt file must start with front matter")
    _, front, body = text.split("---", 2)
    meta = {}
    for line in front.strip().splitlines():
        key, _, val = line.partition(":")
        meta[key.strip()] = val.split("#", 1)[0].strip()
    system, sep, user = body.partition("=== USER ===")
    if not sep:
        raise ValueError(f"prompt {meta.get('id')} has no '=== USER ===' section")
    return Prompt(
        id=meta["id"], version=meta["version"], tier=meta.get("tier", "fast"),
        temperature=float(meta.get("temperature", 0)), max_tokens=int(meta.get("max_tokens", 1024)),
        system=system.strip(), user_template=user.strip(),
        sha=hashlib.sha256(text.encode()).hexdigest()[:8],
    )


@lru_cache(maxsize=32)
def load_prompt(prompt_id: str) -> Prompt:
    return _parse((PROMPT_DIR / f"{prompt_id}.md").read_text())


def all_prompts() -> list[Prompt]:
    return [load_prompt(p.stem) for p in sorted(PROMPT_DIR.glob("*.md"))]
