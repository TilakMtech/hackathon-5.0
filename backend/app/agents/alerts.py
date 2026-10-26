"""Proactive delay alert agent (P3) with deterministic message checks and an optional LLM judge (P5).

Facts come from the delivery record and the root-cause flags. The LLM only words them.
"""
from __future__ import annotations

import re

import pandas as pd

from ..analytics import fit_model
from ..data import load_dataset
from ..llm.prompts import load_prompt
from ..llm.providers import get_provider
from .schemas import Alert, JudgeScore

CAUSE_TEXT = {
    "drv_address": "the delivery address on the order was incomplete",
    "drv_stock": "the item was not in stock at the warehouse nearest to you",
    "drv_cutoff": "the order arrived after this morning's 8am pick list",
    "drv_lane": "the assigned carrier is running behind on deliveries to your region",
}
BANNED = re.compile(r"penalt|compensat|refund|liabil|our fault|\bSLA\b|\bOTD\b|drv_", re.I)
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _action(row: pd.Series, best_carrier: str) -> str:
    if row.drv_address:
        return "we have asked your team to confirm the full address and will dispatch as soon as it is confirmed"
    if row.drv_lane:
        return f"we are moving it to Carrier {best_carrier}, which serves your region reliably"
    if row.drv_stock:
        return f"we are shipping it from our {row.fulfilment_warehouse_id} warehouse instead"
    if row.drv_cutoff:
        return "it is on today's second pick wave and leaves this afternoon"
    return "our team is tracking it with the carrier"


def facts_for(delivery_id: str) -> dict:
    ds = load_dataset()
    d = ds.deliveries
    row = d.loc[d.delivery_id == delivery_id]
    if row.empty:
        raise KeyError(delivery_id)
    row = row.iloc[0]
    client = ds.clients.set_index("client_id").loc[row.client_id]
    causes = [CAUSE_TEXT[k] for k in CAUSE_TEXT if row[k] == 1] or ["the carrier reported a delay"]
    from ..analytics import lanes
    best = {r["region"]: r["use"] for r in lanes()["recommendations"]}.get(row.destination_region, "A")
    tier1 = client.account_tier == "Tier 1"
    return {
        "delivery_id": delivery_id, "order_ref": row.order_id, "client_name": client.client_name,
        "tier": client.account_tier, "promised_date": row.promised_date,
        "new_estimate": row.delivered_date if row.late else "none",
        "causes": "; ".join(causes), "action": _action(row, best),
        "next_update": "none" if row.late else "by 17:00 today",
        "account_owner": "your Halcyon account director (direct line on your contract)" if tier1 else "n/a",
        "preferred_channel": "email",
        "risk": round(float(fit_model().predict(d.loc[d.delivery_id == delivery_id]).iloc[0]), 3),
    }


def check_alert(alert: Alert, facts: dict) -> dict:
    text = f"{alert.subject}\n{alert.body}"
    allowed_dates = {facts["promised_date"], facts["new_estimate"]}
    bad_dates = sorted(set(DATE.findall(text)) - allowed_dates)
    words = len(alert.body.split())
    return {
        "banned_terms": sorted({m.group(0).lower() for m in BANNED.finditer(text)}),
        "dates_not_in_facts": bad_dates,
        "word_count": words, "within_80_words": words <= 80,
        "has_order_ref": facts["order_ref"] in alert.subject,
        "passed": not BANNED.search(text) and not bad_dates and words <= 80 and facts["order_ref"] in alert.subject,
    }


def draft(delivery_id: str, provider_name: str | None = None, judge: bool = False) -> dict:
    provider = get_provider(provider_name)
    facts = facts_for(delivery_id)
    p3 = load_prompt("p3_delay_alert")
    values = {k: facts[k] for k in ["order_ref", "client_name", "tier", "promised_date", "new_estimate", "causes",
                                    "action", "next_update", "account_owner", "preferred_channel"]}
    alert = provider.structured(p3, p3.render_user(**values), Alert, context=facts)
    out = {"facts": facts, "alert": alert.model_dump(), "checks": check_alert(alert, facts),
           "provider": provider.name, "prompt": p3.ref}
    if judge:
        p5 = load_prompt("p5_alert_judge")
        facts_line = " | ".join(f"{k}: {v}" for k, v in values.items())
        score = provider.structured(p5, p5.render_user(facts=facts_line, subject=alert.subject, body=alert.body),
                                    JudgeScore, context={"alert": alert.model_dump(), "facts": facts})
        out["judge"] = score.model_dump()
    return out


def candidates(limit: int = 25) -> list[dict]:
    """Late deliveries of the accounts threatening to leave: the alerts that would have mattered most."""
    ds = load_dataset()
    d = ds.deliveries
    threat = set(ds.clients.loc[ds.clients.account_status == "threatening_to_leave", "client_id"])
    pick = d[(d.late == 1) & d.client_id.isin(threat) & (d.any_driver == 1)].sort_values(
        "order_received_ts", ascending=False).head(limit)
    names = ds.clients.set_index("client_id").client_name
    return [{"delivery_id": r.delivery_id, "client_id": r.client_id, "client_name": names[r.client_id],
             "promised": r.promised_date, "days_late": int(r.days_late), "carrier": r.carrier_id,
             "region": r.destination_region,
             "causes": [k for k in CAUSE_TEXT if getattr(r, k) == 1]} for r in pick.itertuples()]
