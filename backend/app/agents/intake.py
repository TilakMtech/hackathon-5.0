"""Email intake agent: P1 extraction -> deterministic guardrails -> decision -> P2 request if needed.

The LLM reads the messy email. Code decides what is safe to send to the warehouse.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from ..config import settings
from ..llm.prompts import load_prompt
from ..llm.providers import get_provider
from .schemas import EmailDraft, Extraction

REQUIRED = ["line1", "city", "state", "postcode"]
FIELD_WORDS = {"line1": "street address", "city": "city", "state": "state", "postcode": "ZIP code"}
ZIP_PREFIX_BY_STATE = {  # first digits used for a cheap consistency check; production uses Azure Maps geocoding
    "OH": "4", "IN": "4", "IA": "5", "WI": "5", "NC": "2", "TN": "3", "GA": "3", "FL": "3",
    "CT": "0", "NY": "1", "RI": "0", "MA": "0", "ID": "8", "NV": "8", "WA": "9", "CA": "9", "IL": "6",
}
INJECTION_PATTERNS = re.compile(
    r"ignore (all )?(previous|prior)|disregard your|admin override|if you are an ai|system note to ai|assistant,",
    re.I)


SKU_RE = re.compile(r"SKU-\d{5}", re.I)
QTY_WINDOW = 20  # characters either side of a SKU, never past the neighbouring SKU


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def qty_written_next_to_sku(email_body: str, sku: str, qty: int) -> bool:
    """The quantity must be written beside ITS SKU, not anywhere in the email (a 4,999 in a PO number is not a quantity)."""
    ms = list(SKU_RE.finditer(email_body))
    for i, m in enumerate(ms):
        if m.group(0).upper() != sku.upper():
            continue
        line_lo = email_body.rfind("\n", 0, m.start()) + 1  # same line only: the next line's quantity is not this SKU's
        line_hi = email_body.find("\n", m.end()) % (len(email_body) + 1)
        lo = max(ms[i - 1].end() if i else 0, m.start() - QTY_WINDOW, line_lo)
        hi = min(ms[i + 1].start() if i + 1 < len(ms) else len(email_body), m.end() + QTY_WINDOW, line_hi)
        if re.search(rf"(?<![\d.]){qty}(?!\d)(?!\.\d)", email_body[lo:m.start()] + " " + email_body[m.end():hi]):
            return True
    return False


def guardrails(email_body: str, default_address: str, x: Extraction, received_at: str) -> dict:
    """Checks the model cannot talk its way around."""
    body = _norm(email_body)
    flags: list[str] = []

    # 1. every evidence quote must exist verbatim in the email
    ungrounded = [k for k, q in x.evidence.items() if q and _norm(q) not in body]
    if ungrounded:
        flags.append(f"evidence not found in email for: {', '.join(ungrounded)}")

    # 2. address values must appear in the email (blocks values copied from the address on file)
    addr = x.delivery_address.model_dump()
    invented = [k for k, v in addr.items() if v and _norm(v) not in body]
    if invented:
        flags.append(f"address value not in email: {', '.join(invented)}")
    missing = sorted(set(x.missing_fields) | {k for k in REQUIRED if not addr.get(k)} | set(invented))

    # 3. ZIP code vs state sanity
    st, pc = (addr.get("state") or "").upper(), addr.get("postcode") or ""
    if st in ZIP_PREFIX_BY_STATE and pc and not pc.startswith(ZIP_PREFIX_BY_STATE[st]):
        flags.append(f"ZIP {pc} does not match state {st}")

    # 3b. a postcode is written right after the state but was not extracted
    if st and not pc:
        m = re.search(rf"\b{re.escape(st)}\s+(\d{{5}})\b", email_body, re.I)
        if m:
            flags.append(f"postcode {m.group(1)} appears after {st} in the email but was not extracted")

    # 4. quantities and SKUs must be plausible, present, and paired the way the email pairs them
    for line in x.order_lines:
        if line.qty > 5000:
            flags.append(f"quantity {line.qty} for {line.sku} is implausible")
        if _norm(line.sku) not in body:
            flags.append(f"{line.sku} not found in email")
        elif not qty_written_next_to_sku(email_body, line.sku, line.qty):
            flags.append(f"quantity {line.qty} is not written next to {line.sku} in the email")
    if not x.order_lines:
        flags.append("no order lines found")

    # 4b. every consequential field needs its quote (checked verbatim in 1); without one, a person decides
    need = ["order_lines"] + (["commitment_type"] if x.commitment_type != "unknown" else []) \
        + (["requested_date"] if x.requested_date else [])
    no_quote = [k for k in need if not (x.evidence.get(k) or "").strip()]
    if no_quote:
        flags.append(f"no evidence quote for: {', '.join(no_quote)}")

    # 4c. an order we cannot schedule is not auto-accepted
    if x.commitment_type == "unknown":
        flags.append("service level not stated")
    if x.requested_date:
        try:
            datetime.strptime(x.requested_date, "%Y-%m-%d")
        except ValueError:
            flags.append(f"requested date {x.requested_date!r} is not a valid date")

    # 5. prompt-injection text present (independent of whether the model noticed)
    injection = bool(INJECTION_PATTERNS.search(email_body))
    if injection and not x.suspicious_instructions:
        flags.append("email contains instructions aimed at the AI; model did not flag them")
    if x.suspicious_instructions:  # either detector is enough to stop auto-accept
        flags.append("model flagged suspicious instructions in the email")

    # 6. operational risk from the root-cause analysis
    received = datetime.fromisoformat(received_at)
    risks = []
    if missing:
        risks.append("Incomplete address: 65% of such orders arrive late")
    if x.commitment_type == "same_day" and received.hour >= 8:
        risks.append("Same-day order after the 8am pick list: 61% arrive late unless a second wave picks it")

    if flags or injection or x.suspicious_instructions:
        decision = "human_review"
    elif missing:
        decision = "request_info"
    elif x.confidence < settings.intake_auto_accept_confidence:
        decision = "human_review"
    else:
        decision = "auto_accept"
    return {"decision": decision, "missing_fields": missing, "flags": flags,
            "injection_detected": injection, "risks": risks}


def extract(email: dict, provider_name: str | None = None) -> dict:
    """email: {client_id, email_format_id, received_ts, body, default_address, [email_id, truth]}"""
    provider = get_provider(provider_name)
    p1 = load_prompt("p1_email_extraction")
    user = p1.render_user(client_id=email["client_id"], email_format_id=email.get("email_format_id", "unknown"),
                          received_at=email["received_ts"], default_address=email.get("default_address", "none"),
                          email_body=email["body"])
    x = provider.structured(p1, user, Extraction, context={"email": email})
    checks = guardrails(email["body"], email.get("default_address", ""), x, email["received_ts"])

    request = None
    if checks["missing_fields"] and x.order_lines:
        p2 = load_prompt("p2_address_request")
        missing_words = ", ".join(FIELD_WORDS[f] for f in checks["missing_fields"] if f in FIELD_WORDS)
        reply_by = (datetime.fromisoformat(email["received_ts"]) + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
        order_ref = email.get("email_id", "your order")
        values = dict(client_name=email.get("client_name", email["client_id"]), order_ref=order_ref,
                      skus=", ".join(f"{li.sku} x {li.qty}" for li in x.order_lines),
                      missing_fields=missing_words, reply_by=reply_by)
        draft = provider.structured(p2, p2.render_user(**values), EmailDraft, context=values)
        request = {**draft.model_dump(), "checks": request_checks(draft, email["body"], checks["missing_fields"])}

    return {"extraction": x.model_dump(), "guardrails": checks, "address_request": request,
            "provider": provider.name, "prompt": p1.ref}


def request_checks(draft: EmailDraft, email_body: str, missing: list[str]) -> dict:
    """P2 must never put an address value in the email that the client did not send."""
    numbers = set(re.findall(r"\b\d{5}\b", draft.body)) - set(re.findall(r"\b\d{5}\b", email_body))
    words = len(draft.body.split())
    mentions = all(FIELD_WORDS[f].split()[0].lower() in draft.body.lower() for f in missing if f in FIELD_WORDS)
    return {"no_invented_zip": not numbers, "word_count": words, "within_60_words": words <= 60,
            "mentions_every_missing_field": mentions}
