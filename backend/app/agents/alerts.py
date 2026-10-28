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

CAUSE_TEXT = {
    "drv_address": "the delivery address on the order was incomplete",
    "drv_stock": "the item was not in stock at the warehouse nearest to you",
    "drv_cutoff": "the order arrived after the 8am pick list",
    "drv_lane": "the assigned carrier is running behind on deliveries to your region",
}
BANNED = re.compile(r"penalt|compensat|refund|liabil|our fault|\bSLA\b|\bOTD\b|drv_", re.I)
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
# claims of something Halcyon did or will do; each must be backed by the action or causes in the facts
ACTION_CLAIM = re.compile(
    r"\b(rerout\w*|redirect\w*|upgrad\w*|expedit\w*|priority|courier|drone\w*|air freight|overnight|credit\w*|"
    r"discount\w*|waiv\w*|mov(?:e|ed|ing)|switch\w*|transferr\w*|second pick wave|ship\w* (?:it )?from|"
    r"dispatch\w*|replac\w*|resend\w*|re-?deliver\w*|free)\b", re.I)
# named things the message may only mention when the facts do
CARRIER_NAME = re.compile(r"\bcarriers?\s+([a-f])\b|\b(fedex|ups|dhl|usps|amazon|ontrac|xpo|purolator)\b", re.I)
WAREHOUSE_NAME = re.compile(r"\b(W\d|WH-?\d+)\b|\b([A-Z][a-z]+(?: \d)?) warehouse\b")
# causes the message may only give when the facts do
CAUSE_CLAIM = re.compile(r"\b(weather|snow\w*|storm\w*|hurricane|flood\w*|fire|strike\w*|accident\w*|traffic|road\w*|"
                         r"highway|closure\w*|customs|holiday\w*|breakdown|mechanical|shortage\w*|staff\w*|outage\w*|"
                         r"technical|peak|demand|volume)\b", re.I)
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
        "account_owner": "your Halcyon account director (direct line on your contract)" if tier1 else "n/a",
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


def check_alert(alert: Alert, facts: dict) -> dict:
    """Deterministic checks on subject AND body. Anything named (date, action, carrier, warehouse, cause) must be in the facts."""
    text = f"{alert.subject}\n{alert.body}"
    backing = f"{facts['action']} {facts['causes']}".lower()
    promised = pd.Timestamp(facts["promised_date"]).date()
    allowed = {promised} | {pd.Timestamp(x).date() for x in DATE.findall(facts.get("next_update", "") + " " + str(facts.get("new_estimate")))}
    allowed_days = {d.strftime("%A").lower() for d in allowed}
    bad_dates = sorted({raw for raw, val in _dates_in(text, promised.year)
                        if val is None or (isinstance(val, str) and val not in allowed_days) or (not isinstance(val, str) and val not in allowed)})
    unbacked = sorted({m.group(0).lower() for m in ACTION_CLAIM.finditer(text) if m.group(0).lower() not in backing})
    wh = load_dataset().warehouses.set_index("warehouse_id").warehouse_name.to_dict()
    ok_names = backing + " " + " ".join(f"{w} {n}".lower() for w, n in wh.items() if w.lower() in backing)
    names = sorted({(f"Carrier {m.group(1).upper()}" if m.group(1) else m.group(2)) for m in CARRIER_NAME.finditer(text)
                    if (f"carrier {m.group(1).lower()}" if m.group(1) else m.group(2).lower()) not in backing}
                   | {m.group(0) for m in WAREHOUSE_NAME.finditer(text) if (m.group(1) or m.group(2)).lower() not in ok_names})
    causes = sorted({m.group(0).lower() for m in CAUSE_CLAIM.finditer(text) if m.group(0).lower() not in backing})
    words = len(alert.body.split())
    passed = (not BANNED.search(text) and not bad_dates and not unbacked and not names and not causes and words <= 80
              and facts["order_ref"] in alert.subject)
    return {
        "banned_terms": sorted({m.group(0).lower() for m in BANNED.finditer(text)}),
        "dates_not_in_facts": bad_dates,
        "actions_not_in_facts": unbacked,
        "names_not_in_facts": names,
        "causes_not_in_facts": causes,
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
