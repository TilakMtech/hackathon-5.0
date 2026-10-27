"""Output contracts for every LLM call. The model must fill these; Pydantic enforces them."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Models sometimes leak tool-call markup ("</answer>", "<parameter name=...>") into string fields.
# Rejecting it makes the provider send an automatic repair request instead of showing garbage to users.
_MARKUP = re.compile(r"</?\s*(answer|parameter|sql|output|invoke|function_calls|tool_use)\b", re.I)


def _no_markup(v: str | None) -> str | None:
    if v and _MARKUP.search(v):
        raise ValueError("text fields must be plain text with no XML or parameter tags; put each value in its own field")
    return v

AddressField = Literal["line1", "city", "state", "postcode"]


class OrderLine(BaseModel):
    sku: str = Field(description="SKU exactly as written, e.g. SKU-12345")
    qty: int = Field(ge=1, description="Quantity ordered")


class Address(BaseModel):
    line1: str | None = Field(None, description="Street number and street name, or null if absent")
    city: str | None = None
    state: str | None = Field(None, description="2-letter state code")
    postcode: str | None = None


class Extraction(BaseModel):
    # field order is generation order: the model quotes the email first, then extracts from its own quotes
    evidence: dict[str, str] = Field(default_factory=dict, description="field name -> exact quote from the email")
    order_lines: list[OrderLine]
    commitment_type: Literal["same_day", "next_day", "standard", "unknown"]
    requested_date: str | None = Field(None, description="YYYY-MM-DD or null")
    delivery_address: Address
    missing_fields: list[AddressField] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    suspicious_instructions: bool = False
    notes: str | None = None


class EmailDraft(BaseModel):
    subject: str
    body: str

    _clean = field_validator("subject", "body")(classmethod(lambda cls, v: _no_markup(v)))


class Alert(BaseModel):
    subject: str
    body: str
    channel: Literal["email", "sms"] = "email"

    _clean = field_validator("subject", "body")(classmethod(lambda cls, v: _no_markup(v)))


class Figure(BaseModel):
    label: str
    value: str


class EvidenceAnswer(BaseModel):
    answer: str = Field(description="Plain-text answer, at most 4 sentences. No SQL, no tags, no column names.")
    sql: str | None = Field(None, description="The final SELECT query you ran")
    figures: list[Figure] = Field(default_factory=list)
    answerable: bool = True

    _clean = field_validator("answer")(classmethod(lambda cls, v: _no_markup(v)))

    @field_validator("sql")
    @classmethod
    def _sql_clean(cls, v: str | None) -> str | None:
        return _no_markup(v)


class JudgeScore(BaseModel):
    factual: int = Field(ge=1, le=5)
    clarity: int = Field(ge=1, le=5)
    action: int = Field(ge=1, le=5)
    tone: int = Field(ge=1, le=5)
    issues: list[str] = Field(default_factory=list)
