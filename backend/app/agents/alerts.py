"""Proactive delay alert agent (P3) with deterministic message checks and an optional LLM judge (P5).

HISTORICAL REPLAY. The alerts are replayed on August-September 2026 deliveries using only what Halcyon knew at
despatch: the risk model and the lane rule are learned on April-July (analytics.holdout), the trigger is the
predicted risk (not the known outcome), and the eventual delivery date is never shown to the model. The actual
outcome is kept beside the draft so the trigger's precision can be measured.

The remedial action comes from the playbook and is PROPOSED, not executed: no execution record exists in the
data, so every draft needs an operations approver to confirm the action before it can be sent.
"""
from __future__ import annotations

import re
from datetime import datetime

import pandas as pd

from ..analytics import holdout
from ..data import load_dataset
from ..llm.prompts import load_prompt
from ..llm.providers import get_provider
from .schemas import Alert, JudgeScore

CAUSE_TEXT = {  # short on purpose: three causes plus the action must fit in 80 words
    "drv_address": "the delivery address was incomplete",
    "drv_stock": "the item was out of stock at your nearest warehouse",
    "drv_cutoff": "the order arrived after the 8am pick list",
    "drv_lane": "the carrier on your route is running late",
}
BANNED = re.compile(r"penalt|compensat|refund|liabil|our fault|\bSLA\b|\bOTD\b|drv_", re.I)
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
# claims of something Halcyon did or will do; each must be backed by the action or causes in the facts
ACTION_CLAIM = re.compile(  # specific remedies only; everyday words ("feel free", "dispatch") are not claims
    r"\b(rerout\w*|redirect\w*|upgrad\w*|expedit\w*|prioriti[sz]\w*|priority (?:shipping|delivery|service)|courier|"
    r"drone\w*|air freight|overnight|credit\w*|discount\w*|waiv\w*|second pick wave|ship\w* (?:it )?from|"
    r"replac\w*|resend\w*|re-?deliver\w*|free (?:shipping|delivery|of charge)|switch\w* (?:carrier|to)|"
    r"mov(?:e|ed|ing) (?:it|your order|the order) to)\b", re.I)
# named things the message may only mention when the facts do
CARRIER_NAME = re.compile(r"\bcarriers?\s+([a-f])\b|\b(fedex|ups|dhl|usps|amazon|ontrac|xpo|purolator)\b", re.I)
WAREHOUSE_NAME = re.compile(r"\b(W\d|WH-?\d+)\b|\b([A-Z][a-z]+(?: \d)?) warehouse\b")
# causes the message may only give when the facts do
CAUSE_CLAIM = re.compile(  # outside events the facts never contain; a paraphrase like "stock shortage" is fine
    r"\b(weather|snow\w*|storm\w*|hurricane|flood\w*|wildfire|fire|strike\w*|accident\w*|traffic|road closure\w*|"
    r"highway|customs|holiday\w*|breakdown|mechanical|outage\w*)\b", re.I)
MONTHS = "jan|feb|mar|apr|(?-i:May)|jun|jul|aug|sep|sept|oct|nov|dec"  # "may" as a verb is not a date
DATE_ANY = re.compile(
    rf"(?<![\w-])\d{{4}}-\d{{2}}-\d{{2}}\b|(?<![\w-])\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?\b"
    rf"|(?<![\w-])\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})[a-z]*\.?(?:,?\s+\d{{4}})?\b"
    rf"|\b(?:{MONTHS})[a-z]*\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?\b"
    r"|\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)
ALERT_RISK = 0.5  # predicted late probability at despatch that triggers an alert


def _action(row: pd.Series, best_carrier: str) -> str:
    if row.drv_address:
        return "we have asked your team to confirm the full address and will dispatch as soon as it is confirmed"
    if row.drv_lane:
        return f"we are moving it to Carrier {best_carrier}, which serves your region reliably"
    if row.drv_stock:
        return f"we are shipping it from our {row.fulfilment_warehouse_id} warehouse instead"
    if row.drv_cutoff:
        return "it is on the second pick wave and leaves this afternoon"
    return "our team is tracking it with the carrier"


def _row(delivery_id: str) -> pd.Series:
    t = holdout()["test"]
    row = t.loc[t.delivery_id == delivery_id]
    if row.empty:
        raise KeyError(f"{delivery_id} is not in the replay window (orders from {holdout()['summary']['split']})")
    return row.iloc[0]


def facts_for(delivery_id: str) -> dict:
    """What the agent may see: known at despatch. The eventual outcome is returned separately, never in facts."""
    ds = load_dataset()
    row = _row(delivery_id)
    client = ds.clients.set_index("client_id").loc[row.client_id]
    causes = [CAUSE_TEXT[k] for k in CAUSE_TEXT if row[k] == 1] or ["the carrier reported a delay"]
    best = holdout()["best_carrier_by_region"].get(row.destination_region, "A")  # learned on Apr-Jul only
    replay_at = pd.Timestamp(row.shipped_ts)
    next_update = (replay_at.normalize() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    tier1 = client.account_tier == "Tier 1"
    return {
        "delivery_id": delivery_id, "order_ref": row.order_id, "client_name": client.client_name,
        "tier": client.account_tier, "promised_date": row.promised_date,
        "replay_at": replay_at.strftime("%Y-%m-%d %H:%M"),
        "new_estimate": "none",  # unknown at despatch; the real delivery date is the outcome, not a fact
        "causes": "; ".join(causes), "action": _action(row, best),
        "action_status": "proposed: operations must confirm it before this alert is sent",
        "next_update": f"by 12:00 on {next_update}",
        "account_owner": "your Halcyon account director" if tier1 else "n/a",
        "preferred_channel": "email",
        "risk": round(float(row.risk), 3),
    }


def outcome_for(delivery_id: str) -> dict:
    """The real result, hidden from the agent and shown only to score the trigger."""
    row = _row(delivery_id)
    return {"late": bool(row.late), "days_late": int(row.days_late), "delivered_date": row.delivered_date}


def _dates_in(text: str, year: int) -> list[tuple[str, object]]:
    """Every date-like phrase in the text, parsed when possible (a weekday stays a weekday name)."""
    out = []
    for m in DATE_ANY.finditer(text):
        raw = m.group(0)
        t = re.sub(r"(st|nd|rd|th)\b", "", raw, flags=re.I).replace(",", "").replace(".", "").strip()
        val = None
        if re.fullmatch(r"[A-Za-z]+day", t):
            val = t.lower()
        else:
            if not re.search(r"\b\d{4}\b|/\d{2,4}$", t):
                t = f"{t}/{year}" if "/" in t else f"{t} {year}"  # no year written: the promised date's year
            for f in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y"):
                try:
                    val = datetime.strptime(t, f).date()
                    break
                except ValueError:
                    continue
        out.append((raw, val))
    return out


# Claims about where the order physically is. The replay facts never state a shipment status, so none may be asserted.
STATUS_CLAIM = re.compile(
    # where the order is, or that the risk is gone: the replay facts never say either
    r"\b(?:has|have|had|was|were|is|are)\s+(?:now\s+|already\s+|just\s+)?(?:been\s+)?"
    r"(dispatched|despatched|shipped|sent out|delivered|collected|picked up|loaded)\b"
    r"|\b(?:on its way|on the way|in transit|out for delivery|left our warehouse|on time|on schedule|on track|"
    r"no longer|resolved|fixed|sorted out|back on|ensur\w*|guarante\w*|promis\w* (?:to|that|you)|commit\w* to|make sure)\b", re.I)
# promises of delivery or despatch; allowed only when the facts' action uses the same verb ("will dispatch as soon as")
PROMISE = re.compile(r"\b(?:will|shall|'ll|going to|expect to|should)\s+(?:now\s+|still\s+|then\s+)?(?:be\s+)?"
                     r"(deliver\w*|dispatch\w*|despatch\w*|ship\w*|send\w* (?:it|your order|the order|the item|out)|arriv\w*|reach\w* you|"
                     r"leav\w*|get (?:it|your order) to)", re.I)
# time-of-day and relative-day promises; allowed only in a sentence about the next update, at the facts' time
TIME_WORD = re.compile(r"\b(?:today|tomorrow|tonight|this (?:morning|afternoon|evening)|end of (?:the )?day|eod|"
                       r"within \w+ (?:hours?|days?|minutes?)|\d{1,2}(?::\d{2})?\s*(?:am|pm)|\d{1,2}:\d{2}|noon|midday)\b", re.I)
UPDATE_SENTENCE = re.compile(r"\b(?:updat\w*|let you know|write|be in touch|contact you|hear from)\b", re.I)
NEGATION = re.compile(r"\b(?:not|no|never|none|nothing|without|won't|isn't|aren't|wasn't|weren't|don't|doesn't|didn't|"
                      r"can't|cannot|couldn't|hasn't|haven't)\b", re.I)
COURTESY_NEGATION = re.compile(r"\b(?:do not|don't|please do not) hesitate\b", re.I)
SYNONYM_ACTIONS = [{"moving", "move", "moved", "switching", "switch", "transferring", "transfer", "reassigning", "reassign"}]
# ALLOWLIST: every content word must come from the facts or from this neutral set. A blocklist of "bad" causes can
# never be complete ("staffing shortages", "a labour dispute", ...); an allowlist fails closed. Words that would
# carry a claim on their own (today, tomorrow, resolved, fixed, on time) are deliberately NOT here.
NEUTRAL = set("""
hello hi dear good morning afternoon team client customer order orders shipment shipments delivery deliveries due promised
date dates expected expect late later delay delays delayed risk miss missed missing schedule time update updates updated
further next send write writing let know inform informing reach reaching out ahead advance want wanted would will can may
might could should please contact questions question help apologise apologize apology apologies sorry inconvenience
regret thank thanks regards kind best sincerely warm patience understanding appreciate appreciated hesitate monitoring
monitor situation closely follow up halcyon success account director your our we us you this that these those it its the
a an and or but however because so as to of for with by at in on from about before after until while also currently
soon possible reason reasons affected following unfortunately note notice notify notifying heads aware share sharing
keep posted hear any if all both which what when where who how there here than then very more most such only
just able working work progress status latest direct line regarding concerning subject re arrives
arriving originally planned plan new current feel free detail details information receive received take taking step
steps action actions fix today tomorrow arrive cause don't be been being is are was were has have had do does did i i'm i'll we're we'll we've it's you're
you'll there's that's let's monday tuesday wednesday thursday friday saturday sunday january february march april
may june july august september october november december jan feb mar apr jun jul aug sep sept oct nov dec am pm
""".split())


def _stem(w: str) -> str:
    w = re.sub(r"'s$", "", w.lower()).strip("'")
    for suf in ("ies", "ing", "ed", "es", "s", "ly"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z']*[A-Za-z]|[A-Za-z]", text)  # hyphens split ("follow-up" -> follow, up)


def _sentences(text: str) -> list[str]:
    return [x for x in re.split(r"(?<=[.!?])\s+|\n+", text) if x.strip()]


def check_alert(alert: Alert, facts: dict) -> dict:
    """Deterministic checks on subject AND body. Anything named (date, action, carrier, warehouse, cause) must be in the facts."""
    text = f"{alert.subject}\n{alert.body}"
    backing = f"{facts['action']} {facts['causes']}".lower()
    promised = pd.Timestamp(facts["promised_date"]).date()
    allowed = {promised} | {pd.Timestamp(x).date() for x in DATE.findall(facts.get("next_update", "") + " " + str(facts.get("new_estimate")))}
    allowed_days = {d.strftime("%A").lower() for d in allowed}
    bad_dates = sorted({raw for raw, val in _dates_in(text, promised.year)
                        if val is None or (isinstance(val, str) and val not in allowed_days) or (not isinstance(val, str) and val not in allowed)})
    def _backed(claim: str) -> bool:
        first = claim.split()[0]
        if claim in backing or first in backing.split():
            return True
        return any(first in grp and grp & set(backing.split()) for grp in SYNONYM_ACTIONS)
    unbacked = sorted({m.group(0).lower() for m in ACTION_CLAIM.finditer(text) if not _backed(m.group(0).lower())})
    wh = load_dataset().warehouses.set_index("warehouse_id").warehouse_name.to_dict()
    ok_names = backing + " " + " ".join(f"{w} {n}".lower() for w, n in wh.items() if w.lower() in backing)
    names = sorted({(f"Carrier {m.group(1).upper()}" if m.group(1) else m.group(2)) for m in CARRIER_NAME.finditer(text)
                    if (f"carrier {m.group(1).lower()}" if m.group(1) else m.group(2).lower()) not in backing}
                   | {m.group(0) for m in WAREHOUSE_NAME.finditer(text) if (m.group(1) or m.group(2)).lower() not in ok_names})
    causes = sorted({m.group(0).lower() for m in CAUSE_CLAIM.finditer(text) if m.group(0).lower() not in backing})
    status = sorted({m.group(0).lower() for m in STATUS_CLAIM.finditer(text)})
    fact_text = " ".join(str(facts.get(k, "")) for k in ("causes", "action", "next_update", "account_owner", "client_name",
                                                         "tier", "order_ref"))
    fact_text += " " + ok_names  # a warehouse the action names may also be called by its name ("W4" = "Northeast")
    fact_low = fact_text.lower()
    allowed_stems = {_stem(w) for w in _words(fact_text)} | {_stem(w) for w in NEUTRAL}
    for grp in SYNONYM_ACTIONS:  # "switching to Carrier C" when the action says "moving it to Carrier C"
        if grp & set(fact_low.split()):
            allowed_stems |= {_stem(w) for w in grp}
    unsupported = sorted({w.lower() for w in _words(text) if len(w) > 1 and _stem(w) not in allowed_stems
                          and w != facts["order_ref"] and not re.fullmatch(r"ORD|W\d|AM|PM", w, re.I)})
    # promises to deliver/dispatch/arrive: only the verb the action itself uses
    # the action is a FIXED sentence: it must appear word for word, so its conditions survive ("will dispatch as
    # soon as it is confirmed" cannot shrink to "will dispatch your order"). Promises anywhere else are not allowed.
    norm = lambda t: re.sub(r"\s+", " ", t).strip().lower()  # noqa: E731
    action_n = norm(facts["action"])
    action_verbatim = action_n in norm(text)
    outside_action = re.sub(re.escape(action_n), " ", norm(text)) if action_verbatim else norm(text)
    promises = sorted({m.group(0).lower() for m in PROMISE.finditer(outside_action)})
    # times: only in a next-update sentence, and only the facts' own time; or a time the action states
    update_times = {t.lower() for t in TIME_WORD.findall(facts.get("next_update", ""))} | {"12:00", "noon", "midday", "12 pm", "12pm"}
    times = []
    for sent in _sentences(text):
        for t in TIME_WORD.findall(sent):
            tl = t.lower()
            in_action = tl in fact_low  # a time the facts state ("after the 8am pick list", "leaves this afternoon")
            is_update = UPDATE_SENTENCE.search(sent) and (tl in update_times or tl == "tomorrow")
            if not (in_action or is_update):
                times.append(t)
    # numbers: every number must be one the facts contain (order ref, dates, the next-update time, 8am, W4 ...)
    fact_nums = set(re.findall(r"\d+", fact_text + " " + facts["promised_date"] + " " + facts.get("next_update", "")))
    numbers = sorted({n for n in re.findall(r"\d+", re.sub(r"\b\d{4}-\d{2}-\d{2}\b|" + re.escape(facts["order_ref"]), " ", text))
                      if n not in fact_nums})
    # negations can flip a fact ("it will not be late", "not caused by the address"); only the facts' own may appear
    negations = sorted({m.group(0).lower() for m in NEGATION.finditer(COURTESY_NEGATION.sub(" ", text))
                        if m.group(0).lower() not in fact_low.split()})
    words = len(alert.body.split())
    passed = (not BANNED.search(text) and not bad_dates and not unbacked and not names and not causes and not status
              and action_verbatim and not unsupported and not promises and not times and not numbers and not negations
              and words <= 80 and facts["order_ref"] in alert.subject)
    return {
        "banned_terms": sorted({m.group(0).lower() for m in BANNED.finditer(text)}),
        "dates_not_in_facts": bad_dates,
        "actions_not_in_facts": unbacked,
        "names_not_in_facts": names,
        "causes_not_in_facts": causes,
        "status_claims_not_in_facts": status,
        "words_not_in_facts": unsupported,  # allowlist: content words neither in the facts nor in the neutral set
        "action_verbatim": action_verbatim,
        "promises_not_in_facts": promises,
        "times_not_in_facts": times,
        "numbers_not_in_facts": numbers,
        "negations_not_in_facts": negations,
        "word_count": words, "within_80_words": words <= 80,
        "has_order_ref": facts["order_ref"] in alert.subject,
        "passed": passed,
    }


def draft(delivery_id: str, provider_name: str | None = None, judge: bool = False) -> dict:
    provider = get_provider(provider_name)
    facts = facts_for(delivery_id)
    p3 = load_prompt("p3_delay_alert")
    values = {k: facts[k] for k in ["order_ref", "client_name", "tier", "promised_date", "new_estimate", "causes",
                                    "action", "next_update", "account_owner", "preferred_channel"]}
    alert = provider.structured(p3, p3.render_user(**values), Alert, context=facts)
    checks = check_alert(alert, facts)
    out = {"facts": facts, "alert": alert.model_dump(), "checks": checks, "outcome": outcome_for(delivery_id),
           "provider": provider.name, "prompt": p3.ref}
    if judge:
        p5 = load_prompt("p5_alert_judge")
        facts_line = " | ".join(f"{k}: {v}" for k, v in values.items())
        score = provider.structured(p5, p5.render_user(facts=facts_line, subject=alert.subject, body=alert.body),
                                    JudgeScore, context={"alert": alert.model_dump(), "facts": facts})
        out["judge"] = score.model_dump()
    judged_ok = not judge or out["judge"]["factual"] == 5
    # never "safe to send": a person confirms the proposed action first
    out["status"] = "ready_for_approval" if checks["passed"] and judged_ok else "needs_edit"
    return out


def _threat_test() -> pd.DataFrame:
    ds = load_dataset()
    threat = set(ds.clients.loc[ds.clients.account_status == "threatening_to_leave", "client_id"])
    t = holdout()["test"]
    return t[t.client_id.isin(threat)]


def candidates(limit: int = 25) -> list[dict]:
    """Deliveries of threatening accounts whose risk at despatch crossed the trigger, highest risk first."""
    names = load_dataset().clients.set_index("client_id").client_name
    t = _threat_test()
    pick = t[t.risk >= ALERT_RISK].sort_values(["risk", "order_received_ts"], ascending=[False, False]).head(limit)
    return [{"delivery_id": r.delivery_id, "client_id": r.client_id, "client_name": names[r.client_id],
             "promised": r.promised_date, "risk": round(float(r.risk), 3), "carrier": r.carrier_id,
             "region": r.destination_region, "causes": [k for k in CAUSE_TEXT if getattr(r, k) == 1],
             "outcome_late": bool(r.late), "outcome_days_late": int(r.days_late)} for r in pick.itertuples()]


def trigger_stats() -> dict:
    """How good is the trigger on unseen months? Precision = alerts that were really late; recall = late ones caught."""
    t = _threat_test()
    fired = t.risk >= ALERT_RISK
    late = t.late == 1
    n_fired = int(fired.sum())
    return {"window": f"orders from {holdout()['summary']['split']}", "threshold": ALERT_RISK,
            "deliveries": int(len(t)), "alerts_fired": n_fired, "late": int(late.sum()),
            "precision": round(float((fired & late).sum() / max(n_fired, 1)), 3),
            "recall": round(float((fired & late).sum() / max(int(late.sum()), 1)), 3)}
