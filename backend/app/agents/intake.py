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
# where an email stops being the order: forwarded or quoted mail, a signature, a legal footer
BLOCK_END = re.compile(r"^\s*(?:-{2,}.*|>.*|From:.*|On .+ wrote:.*|.*\bconfidential\b.*)$", re.I | re.M)
# a quantity written right after its SKU: "x 120", ": 24 pcs", " - 50", "*QTY*200", " (10)", ",10", "| 10 |", "qty50"
QTY_AFTER = re.compile(r"^(?:\s|[:|*,(=\-–×]|x(?=\s*\d)|qty|quantity)*?(\d[\d,]*(?:\.\d+)?)(?!\d)", re.I)
# ... or right before it: "50 x SKU", "40x SKU", "250 units of SKU", "120 of SKU"
QTY_BEFORE = re.compile(r"(?<![\d.])(\d[\d,]*(?:\.\d+)?)\s*(?:x|×|units? of|units?|pcs of|pcs|of)?\s*$", re.I)
# the client changing their mind inside the email: a person decides which quantity stands
CORRECTION = re.compile(r"\b(correct(?:ed|ion|ing)|amend\w*|revis\w*|instead of|change[ds]? (?:the |that |it |qty|quantity|to)|"
                        r"make (?:it|that) \d|scratch that|i meant|meant to|should (?:be|have been|read)|typo|oops|no wait|"
                        r"double it|halve it|by mistake|was wrong|\d+ more\b|more of the same|additional|extra|on top of|"
                        r"update[ds]? (?:the )?(?:qty|quantity|quantities))\b", re.I)
# numbers that are labels, not quantities: "PO 4471", "Line 1", "#12", "Ref 99", "Client 128", "1." list numbering
LABEL_NUMBER = re.compile(r"\b(?:po|p\.o\.|ref|order|line|item|no\.?|client|invoice|account)\s*[#:.]?\s*\d+|#\s*\d+|^\s*\d+[.)](?=\s)",
                          re.I | re.M)
DATE_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}|\b\d{8}\b|\d{1,2}/\d{1,2}/\d{2,4}|\b\d{1,2}(?:st|nd|rd|th)? [A-Za-z]{3,9}\.? \d{4}|"
                       r"[A-Za-z]{3,9}\.? \d{1,2}(?:st|nd|rd|th)?,? \d{4}|\b\d{1,2}:\d{2}\b|\b\d{1,2}\s?(?:am|pm)\b", re.I)
SEPARATOR = re.compile(r"[;|,\n]|\band\b", re.I)
SERVICE_WORDS = {
    "same_day": re.compile(r"same[\s_-]?day|today", re.I),
    "next_day": re.compile(r"next[\s_-]?day|tomorrow|overnight", re.I),
    "standard": re.compile(r"standard|no rush", re.I),
}
DATE_FORMATS = ["%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%Y%m%d"]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def order_part(email_body: str) -> str:
    """The part of the email that is the order: everything before a forwarded block, signature or legal footer."""
    m = BLOCK_END.search(email_body)
    return email_body[:m.start()] if m else email_body


def written_qty(email_body: str, sku: str) -> list[int]:
    """Quantities written beside each mention of this SKU, read with the layout's own patterns."""
    out = []
    for m in SKU_RE.finditer(email_body):
        if m.group(0).upper() != sku.upper():
            continue
        line_end = email_body.find("\n", m.end()) % (len(email_body) + 1)
        nxt = SKU_RE.search(email_body, m.end(), line_end)
        after = email_body[m.end():nxt.start() if nxt else line_end]
        a = QTY_AFTER.match(after)
        line_start = email_body.rfind("\n", 0, m.start()) + 1
        before = email_body[line_start:m.start()]
        cut = [x.end() for x in SEPARATOR.finditer(before)] + [x.end() for x in SKU_RE.finditer(before)]
        seg = before[max(cut, default=0):]
        b = QTY_BEFORE.search(seg)
        if b and re.search(r"(?:line|item|po|p\.o\.|no\.?|#|ref)\s*[:#]?\s*$", seg[:b.start()], re.I):
            b = None  # "Line 1 SKU-..." / "PO 4471 SKU-..." : a label, not a quantity
        out += [float(g.group(1).replace(",", "")) for g in (a, b) if g]
    return out


def qty_written_next_to_sku(email_body: str, sku: str, qty: int) -> bool:
    """True only if this exact whole number is written beside the SKU ("10.5" is not 10)."""
    return float(qty) in written_qty(email_body, sku)


def _parse_date(text: str):
    for m in re.finditer(r"\d{4}-\d{2}-\d{2}|\d{8}|\d{1,2}/\d{1,2}/\d{4}|\d{1,2} [A-Za-z]+ \d{4}|[A-Za-z]+ \d{1,2}, \d{4}", text):
        for f in DATE_FORMATS:
            try:
                return datetime.strptime(m.group(0), f).date()
            except ValueError:
                continue
    return None


def guardrails(email_body: str, default_address: str, x: Extraction, received_at: str) -> dict:
    """Checks the model cannot talk its way around."""
    order = order_part(email_body)  # forwarded mail, signatures and legal footers are not the order
    order_n = _norm(order)
    flags: list[str] = []

    # 1. every evidence quote must exist verbatim in the order part of the email
    ungrounded = [k for k, q in x.evidence.items() if q and _norm(q) not in order_n]
    if ungrounded:
        flags.append(f"evidence not found in the order part of the email for: {', '.join(ungrounded)}")

    # 2. address values must appear in the order part (blocks the address on file and office addresses in footers)
    addr = x.delivery_address.model_dump()
    invented = [k for k, v in addr.items() if v and _norm(v) not in order_n]
    if invented:
        flags.append(f"address value not in the order part of the email: {', '.join(invented)}")
    missing = sorted(set(x.missing_fields) | {k for k in REQUIRED if not addr.get(k)} | set(invented))

    # 3. ZIP code vs state sanity
    st, pc = (addr.get("state") or "").upper(), addr.get("postcode") or ""
    if st in ZIP_PREFIX_BY_STATE and pc and not pc.startswith(ZIP_PREFIX_BY_STATE[st]):
        flags.append(f"ZIP {pc} does not match state {st}")

    # 3b. a postcode is written right after the state but was not extracted
    if st and not pc:
        m = re.search(rf"\b{re.escape(st)}\s+(\d{{5}})\b", order, re.I)
        if m:
            flags.append(f"postcode {m.group(1)} appears after {st} in the email but was not extracted")

    # 4. order lines: exactly the SKUs of the order part, each once, each quantity written beside its own SKU
    written = {m.upper() for m in SKU_RE.findall(order)}
    got = [li.sku.upper() for li in x.order_lines]
    if len(got) != len(set(got)):
        flags.append("a SKU appears on more than one order line")
    if set(got) - written:
        flags.append(f"not in the order part of the email: {', '.join(sorted(set(got) - written))}")
    if written - set(got):
        flags.append(f"SKUs in the email but not extracted: {', '.join(sorted(written - set(got)))}")
    # every number in the order must be accounted for: a quantity beside its SKU, the street number, the ZIP, a date,
    # or a label (PO, line, client). Anything else ("I meant 20", "cases of 12", "for our 3 stores") goes to a person.
    rest = LABEL_NUMBER.sub(" ", DATE_TEXT.sub(" ", SKU_RE.sub(" ", order)))
    for v in (addr.get("line1"), addr.get("postcode")):
        if v:
            rest = re.sub(re.escape(v), " ", rest, flags=re.I)
    qtys = {float(li.qty) for li in x.order_lines}
    stray = sorted({n for n in re.findall(r"(?<![\w.])\d(?:[\d,]*\d)?(?:\.\d+)?", rest) if float(n.replace(",", "")) not in qtys})
    if stray:
        flags.append(f"numbers in the email that are not a quantity, address or date: {', '.join(stray)}")
    if re.search(r"\b\d{1,3}\.\d{3}\b", order):
        flags.append("a number like 1.000 could be a decimal or a thousands separator")
    services = {k for k, rx in SERVICE_WORDS.items() if rx.search(order)}
    if len(services) > 1:
        flags.append(f"the email mentions more than one service level: {', '.join(sorted(services))}")
    m = CORRECTION.search(order)
    if m:
        flags.append(f"the email changes or corrects itself ({m.group(0)!r}): a person checks the quantities")
    for sku in written:
        seen = set(written_qty(order, sku))
        if len(seen) > 1:
            flags.append(f"{sku} has more than one quantity in the email: {sorted(seen)}")
        if any(q != int(q) for q in seen):
            flags.append(f"{sku} has a fractional quantity in the email: {sorted(seen)}")
    for line in x.order_lines:
        if line.qty > 5000:
            flags.append(f"quantity {line.qty} for {line.sku} is implausible")
        if line.sku.upper() in written and not qty_written_next_to_sku(order, line.sku, line.qty):
            flags.append(f"quantity {line.qty} is not written next to {line.sku} in the email")
    if not x.order_lines:
        flags.append("no order lines found")

    # 4b. every consequential field needs its quote, and the quote must say what the field says
    need = ["order_lines"] + (["commitment_type"] if x.commitment_type != "unknown" else []) \
        + (["requested_date"] if x.requested_date else [])
    no_quote = [k for k in need if not (x.evidence.get(k) or "").strip()]
    if no_quote:
        flags.append(f"no evidence quote for: {', '.join(no_quote)}")
    received = datetime.fromisoformat(received_at)
    q_service = x.evidence.get("commitment_type") or ""
    if x.commitment_type == "unknown":
        flags.append("service level not stated")
    elif q_service:
        says = {k for k, rx in SERVICE_WORDS.items() if rx.search(q_service)}
        if x.commitment_type == "standard" and not says:
            d = _parse_date(q_service)
            says = {"standard"} if d and (d - received.date()).days >= 3 else set()
        if says != {x.commitment_type}:
            flags.append(f"service quote {q_service!r} does not say {x.commitment_type}")

    # 4c. the requested date must be a real date, read from its quote, and not before the order arrived
    if x.requested_date:
        try:
            req = datetime.strptime(x.requested_date, "%Y-%m-%d").date()
        except ValueError:
            req = None
            flags.append(f"requested date {x.requested_date!r} is not a valid date")
        q_date = x.evidence.get("requested_date") or ""
        if req and q_date and _parse_date(q_date) != req:
            flags.append(f"requested date {x.requested_date} is not what its quote {q_date!r} says")
        if req and req < received.date():
            flags.append(f"requested date {x.requested_date} is before the order arrived")

    # 5. prompt-injection text present (independent of whether the model noticed)
    injection = bool(INJECTION_PATTERNS.search(email_body))
    if injection and not x.suspicious_instructions:
        flags.append("email contains instructions aimed at the AI; model did not flag them")
    if x.suspicious_instructions:  # either detector is enough to stop auto-accept
        flags.append("model flagged suspicious instructions in the email")

    # 6. operational risk from the root-cause analysis
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


RECONCILE = {"postcode": re.compile(r"\d{5}"), "state": re.compile(r"[A-Za-z]{2}")}


def reconcile(x: Extraction, email_body: str) -> tuple[Extraction, list[str]]:
    """Quote-then-extract, enforced: when the model quoted a postcode or state but left the field blank, take the value
    from its own quote, only if the quote is exactly that shape and appears in the order part of the email.
    Seen in real runs: Haiku's notes say "postcode 12121 is copied" while the field stays null."""
    order_n = _norm(order_part(email_body))
    addr = x.delivery_address.model_dump()
    filled = []
    for k, shape in RECONCILE.items():
        q = (x.evidence.get(k) or "").strip()
        if not addr.get(k) and shape.fullmatch(q) and _norm(q) in order_n:
            addr[k] = q.upper() if k == "state" else q
            filled.append(k)
    if not filled:
        return x, []
    return x.model_copy(update={"delivery_address": x.delivery_address.model_copy(update={k: addr[k] for k in filled}),
                                "missing_fields": [f for f in x.missing_fields if f not in filled]}), filled


def extract(email: dict, provider_name: str | None = None) -> dict:
    """email: {client_id, email_format_id, received_ts, body, default_address, [email_id, truth]}"""
    provider = get_provider(provider_name)
    p1 = load_prompt("p1_email_extraction")
    user = p1.render_user(client_id=email["client_id"], email_format_id=email.get("email_format_id", "unknown"),
                          received_at=email["received_ts"], default_address=email.get("default_address", "none"),
                          email_body=email["body"])
    x = provider.structured(p1, user, Extraction, context={"email": email})
    x, reconciled = reconcile(x, email["body"])
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

    return {"extraction": x.model_dump(), "guardrails": {**checks, "reconciled_from_quote": reconciled}, "address_request": request,
            "provider": provider.name, "prompt": p1.ref}


def request_checks(draft: EmailDraft, email_body: str, missing: list[str]) -> dict:
    """P2 must never put an address value in the email that the client did not send."""
    numbers = set(re.findall(r"\b\d{5}\b", draft.body)) - set(re.findall(r"\b\d{5}\b", email_body))
    words = len(draft.body.split())
    mentions = all(FIELD_WORDS[f].split()[0].lower() in draft.body.lower() for f in missing if f in FIELD_WORDS)
    return {"no_invented_zip": not numbers, "word_count": words, "within_60_words": words <= 60,
            "mentions_every_missing_field": mentions}
