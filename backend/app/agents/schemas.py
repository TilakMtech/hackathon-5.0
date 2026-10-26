"""Output contracts for every LLM call. The model must fill these; Pydantic enforces them."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

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
    order_lines: list[OrderLine]
    commitment_type: Literal["same_day", "next_day", "standard", "unknown"]
    requested_date: str | None = Field(None, description="YYYY-MM-DD or null")
    delivery_address: Address
    missing_fields: list[AddressField] = Field(default_factory=list)
    evidence: dict[str, str] = Field(default_factory=dict, description="field name -> exact quote from the email")
    confidence: float = Field(ge=0, le=1)
    suspicious_instructions: bool = False
    notes: str | None = None


class EmailDraft(BaseModel):
    subject: str
    body: str


class Alert(BaseModel):
    subject: str
    body: str
    channel: Literal["email", "sms"] = "email"


class Figure(BaseModel):
    label: str
    value: str


class EvidenceAnswer(BaseModel):
    answer: str
    sql: str | None = None
    figures: list[Figure] = Field(default_factory=list)
    answerable: bool = True


class JudgeScore(BaseModel):
    factual: int = Field(ge=1, le=5)
    clarity: int = Field(ge=1, le=5)
    action: int = Field(ge=1, le=5)
    tone: int = Field(ge=1, le=5)
    issues: list[str] = Field(default_factory=list)
