"""Deterministic offline stand-in for the LLM.

Lets the team build, demo and test every screen and the eval harness with no Azure quota.
It is a rule-based imitation, NOT a model: eval scores produced with it only prove the plumbing,
and the UI labels them as such. Swap OTD_LLM_PROVIDER to claude_foundry for real results.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

from pydantic import BaseModel

from .base import CallRecord, T, Tool, log_call
from .prompts import Prompt

STREET = re.compile(r"\b\d{2,5} [A-Za-z]+ (?:Street|Avenue|Road|Drive|Way|Boulevard|Lane)\b", re.I)
STATES = {"OH", "IN", "IA", "WI", "NC", "TN", "GA", "FL", "CT", "NY", "RI", "MA", "ID", "NV", "WA", "CA"}
CITY_STATE = re.compile(r"(?:([A-Za-z][A-Za-z ]*?),\s*)?\b([A-Z]{2})\b(?:\s+(\d{5}))?")
INJECTION = re.compile(r"ignore (all )?(previous|prior)|disregard your|admin override|if you are an ai|system note", re.I)
COMMIT = [("same_day", r"same[ -]day|needed today|urgent - today"),
          ("next_day", r"next[ -]day|by tomorrow|overnight"),
          ("standard", r"standard|no rush")]
DATE_PATTERNS = [(r"\b\d{4}-\d{2}-\d{2}\b", "%Y-%m-%d"), (r"\b\d{2} [A-Z][a-z]{2} \d{4}\b", "%d %b %Y"),
                 (r"\b\d{2}/\d{2}/\d{4}\b", "%m/%d/%Y"), (r"\b[A-Z][a-z]+ \d{2}, \d{4}\b", "%B %d, %Y"),
                 (r"DTM\*(\d{8})", "%Y%m%d")]


def _clean(body: str) -> tuple[str, bool]:
    body = body.split("---------- Forwarded message")[0]
    keep, suspicious = [], False
    for line in body.splitlines():
        if INJECTION.search(line):
            suspicious = True
            continue
        if re.search(r"confidential|our office", line, re.I):
            continue
        keep.append(line)
    return "\n".join(keep), suspicious


def mock_extract(email: dict) -> dict:
    text, suspicious = _clean(email["body"])
    ev: dict[str, str] = {}
    lines, seen = [], set()
    for m in re.finditer(r"SKU-\d{5}", text):
        sku = m.group(0)
        if sku in seen:
            continue
        before = re.search(r"(\d{1,4})\s*(?:x|units of|of|pcs of)?\s*$", text[max(0, m.start() - 14):m.start()])
        after = re.match(r"\D{0,12}?(\d{1,4})", text[m.end():])
        qty = int(before.group(1)) if before else int(after.group(1)) if after else None
        if qty:
            seen.add(sku)
            lines.append({"sku": sku, "qty": qty})
    if lines:
        ev["order_lines"] = lines[0]["sku"]
    commit = "unknown"
    for kind, pat in COMMIT:
        m = re.search(pat, text, re.I)
        if m:
            commit, ev["commitment_type"] = kind, m.group(0)
            break
    req = None
    for pat, fmt in DATE_PATTERNS:
        m = re.search(pat, text)
        if m:
            raw = m.group(1) if m.groups() else m.group(0)
            try:
                req = datetime.strptime(raw, fmt).date().isoformat()
                ev["requested_date"] = m.group(0)
                break
            except ValueError:
                continue
    addr = {"line1": None, "city": None, "state": None, "postcode": None}
    s = STREET.search(text)
    start = 0
    if s:
        addr["line1"], ev["line1"], start = s.group(0), s.group(0), s.end()
    for m in CITY_STATE.finditer(text, start):
        if m.group(2) in STATES and (s or m.group(1) or m.group(3)):
            city = (m.group(1) or "").strip()
            city = city.split("\n")[-1].strip() or None
            if city and re.search(r"\d|address|ship|to$", city, re.I):
                city = None
            addr["city"], addr["state"], addr["postcode"] = city, m.group(2), m.group(3)
            for k in ("city", "state", "postcode"):
                if addr[k]:
                    ev[k] = addr[k]
            break
    missing = [k for k, v in addr.items() if not v]
    complete = bool(lines) and not missing and commit != "unknown"
    return {"order_lines": lines, "commitment_type": commit, "requested_date": req, "delivery_address": addr,
            "missing_fields": missing, "evidence": ev, "confidence": 0.95 if complete and not suspicious else 0.6,
            "suspicious_instructions": suspicious,
            "notes": "Email contains instructions aimed at the assistant; ignored." if suspicious else None}


def mock_request(v: dict) -> dict:
    return {"subject": f"Address needed for {v['order_ref']}",
            "body": (f"Hello {v['client_name']},\n\nThanks for your order {v['order_ref']} ({v['skus']}). "
                     f"To ship it on time we need the {v['missing_fields']}. "
                     f"Please reply by {v['reply_by']} so it makes the next pick list.\n\nHalcyon Customer Success")}


def mock_alert(f: dict) -> dict:
    when = (f"will now arrive on {f['new_estimate']}" if f["new_estimate"] != "none"
            else f"is at risk of missing that date; we will update you {f['next_update']}")
    sign = (f"Your contact: {f['account_owner']}" if f["tier"] == "Tier 1" else "Halcyon Customer Success")
    return {"subject": f"Update on order {f['order_ref']}",
            "body": (f"Hello,\n\nYour order {f['order_ref']}, due on {f['promised_date']}, {when}. "
                     f"This is because {f['causes'].split('; ')[0]}. To fix it, {f['action']}.\n\n{sign}"),
            "channel": "email"}


def mock_judge(ctx: dict) -> dict:
    from ..agents.alerts import check_alert
    from ..agents.schemas import Alert
    c = check_alert(Alert(**ctx["alert"]), ctx["facts"])
    return {"factual": 5 if not c["dates_not_in_facts"] else 1, "clarity": 4, "action": 4,
            "tone": 5 if not c["banned_terms"] else 2, "issues": c["banned_terms"] + c["dates_not_in_facts"]}


# ---------- evidence agent: keyword router to template SQL ----------

MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
UNANSWERABLE = re.compile(r"weather|traffic|fuel|driver'?s? name|employee|salary|stock price|2027|forecast|"
                          r"next (month|quarter|year)|competitor", re.I)


def _route(q: str) -> tuple[str, str] | None:
    ql = q.lower()
    client = re.search(r"\bc\d{3}\b", ql)
    month = next((MONTHS[m] for m in MONTHS if m in ql), None)
    mfilter = f" AND month(order_received_ts) = {month}" if month else ""
    if client and re.search(r"why|cause|driver|reason", ql):
        c = client.group(0).upper()
        return ("drivers", f"SELECT COUNT(*) AS late_deliveries, AVG(drv_address) AS address, AVG(drv_cutoff) AS cutoff, "
                f"AVG(drv_stock) AS stock, AVG(drv_lane) AS lane FROM deliveries WHERE client_id = '{c}' AND late = 1{mfilter}")
    if client:
        c = client.group(0).upper()
        return ("client_otd", f"SELECT COUNT(*) AS deliveries, AVG(CASE WHEN on_time = 'Y' THEN 1 ELSE 0 END) AS otd "
                f"FROM deliveries WHERE client_id = '{c}'{mfilter}")
    if "carrier" in ql:
        return ("carrier", "SELECT carrier_id, COUNT(*) AS deliveries, AVG(late) AS late_rate FROM deliveries "
                f"WHERE 1=1{mfilter} GROUP BY carrier_id ORDER BY late_rate DESC")
    if "region" in ql:
        return ("region", "SELECT destination_region, AVG(CASE WHEN on_time = 'Y' THEN 1 ELSE 0 END) AS otd FROM deliveries "
                f"WHERE 1=1{mfilter} GROUP BY destination_region ORDER BY otd")
    if "penalt" in ql:
        return ("penalty", "SELECT c.account_tier, SUM(p.penalty_usd) AS penalties_usd FROM sla_penalties p "
                "JOIN clients c USING (client_id) GROUP BY c.account_tier ORDER BY penalties_usd DESC")
    if "complaint" in ql:
        return ("complaints", "SELECT category, COUNT(*) AS complaints FROM complaints GROUP BY category ORDER BY complaints DESC")
    if "email" in ql and re.search(r"incomplete|address", ql):
        return ("email_addr", "SELECT order_channel, AVG(CASE WHEN address_status = 'incomplete' THEN 1 ELSE 0 END) "
                "AS incomplete_share FROM deliveries GROUP BY order_channel ORDER BY incomplete_share DESC")
    if re.search(r"threat|churn|leav|at risk", ql):
        return ("threat", "SELECT COUNT(*) AS accounts, SUM(annual_revenue_usd) AS revenue_usd FROM clients "
                "WHERE account_status = 'threatening_to_leave'")
    if re.search(r"on[- ]time|otd|late", ql):
        if "month" in ql or "trend" in ql:
            return ("trend", "SELECT strftime(order_received_ts, '%Y-%m') AS month, "
                    "AVG(CASE WHEN on_time = 'Y' THEN 1 ELSE 0 END) AS otd FROM deliveries GROUP BY 1 ORDER BY 1")
        return ("otd", f"SELECT COUNT(*) AS deliveries, AVG(CASE WHEN on_time = 'Y' THEN 1 ELSE 0 END) AS otd "
                f"FROM deliveries WHERE 1=1{mfilter}")
    return None


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _phrase(kind: str, r: dict, q: str) -> tuple[str, list[dict]]:
    cols, rows = r["columns"], r["rows"]
    if not rows:
        return "The query returned no rows for that filter.", []
    first = dict(zip(cols, rows[0], strict=True))
    if kind in ("client_otd", "otd"):
        if not first["deliveries"]:
            return "There are no deliveries matching that filter.", []
        return (f"On-time delivery was {_pct(first['otd'])} across {first['deliveries']:,} deliveries.",
                [{"label": "OTD", "value": _pct(first["otd"])}, {"label": "Deliveries", "value": str(first["deliveries"])}])
    if kind == "drivers":
        parts = {k: first[k] or 0 for k in ("address", "cutoff", "stock", "lane")}
        top = max(parts, key=parts.get)
        return (f"Of {first['late_deliveries']} late deliveries, {_pct(parts['address'])} had an incomplete address, "
                f"{_pct(parts['cutoff'])} missed the same-day cutoff, {_pct(parts['stock'])} shipped from a far warehouse "
                f"and {_pct(parts['lane'])} went on a failing carrier lane. The most common driver is {top}.",
                [{"label": k, "value": _pct(v)} for k, v in parts.items()])
    if kind == "carrier":
        worst, best = dict(zip(cols, rows[0], strict=True)), dict(zip(cols, rows[-1], strict=True))
        return (f"Carrier {worst['carrier_id']} has the highest late rate at {_pct(worst['late_rate'])}; "
                f"Carrier {best['carrier_id']} the lowest at {_pct(best['late_rate'])}.",
                [{"label": f"Carrier {x[0]}", "value": _pct(x[2])} for x in rows])
    if kind == "region":
        return ("On-time delivery by destination: " + ", ".join(f"{x[0]} {_pct(x[1])}" for x in rows) + ".",
                [{"label": x[0], "value": _pct(x[1])} for x in rows])
    if kind == "penalty":
        total = sum(x[1] for x in rows)
        return (f"SLA penalties total ${total:,.0f}: " + ", ".join(f"{x[0]} ${x[1]:,.0f}" for x in rows) + ".",
                [{"label": x[0], "value": f"${x[1]:,.0f}"} for x in rows])
    if kind == "complaints":
        total = sum(x[1] for x in rows)
        return (f"There are {total} formal complaints: " + ", ".join(f"{x[0]} {x[1]}" for x in rows) + ".",
                [{"label": x[0], "value": str(x[1])} for x in rows])
    if kind == "email_addr":
        return ("Share of orders with an incomplete address: " + ", ".join(f"{x[0]} {_pct(x[1])}" for x in rows) + ".",
                [{"label": x[0], "value": _pct(x[1])} for x in rows])
    if kind == "threat":
        return (f"{first['accounts']} accounts worth ${first['revenue_usd']:,.0f} a year are threatening to leave.",
                [{"label": "Accounts", "value": str(first["accounts"])},
                 {"label": "Revenue", "value": f"${first['revenue_usd']:,.0f}"}])
    if kind == "trend":
        return ("On-time delivery by month: " + ", ".join(f"{x[0]} {_pct(x[1])}" for x in rows) + ".",
                [{"label": x[0], "value": _pct(x[1])} for x in rows])
    return "Done.", []


def mock_evidence(ctx: dict) -> tuple[dict, list[dict]]:
    q = ctx["question"]
    if UNANSWERABLE.search(q):
        return ({"answer": "The dataset cannot answer this. It holds deliveries, clients, carriers, warehouses, "
                           "penalties and complaints from April to September 2026, with nothing on that topic.",
                 "sql": None, "figures": [], "answerable": False}, [])
    route = _route(q)
    if not route:
        return ({"answer": "Mock mode answers questions about OTD (overall, by month, client, carrier or region), "
                           "late-delivery drivers for a client, penalties, complaints, email address quality and accounts "
                           "at risk. Switch to a real model for open questions.", "sql": None, "figures": [],
                 "answerable": False}, [])
    kind, sql = route
    result = ctx["run_sql"](sql)
    answer, figures = _phrase(kind, result, q)
    return ({"answer": answer, "sql": sql, "figures": figures, "answerable": True},
            [{"tool": "run_sql", "input": {"query": sql}, "output": result}])


class MockProvider:
    name = "mock"

    def model_for(self, tier: str) -> str:
        return f"mock-{tier}"

    def structured(self, prompt: Prompt, user: str, schema: type[T], context: dict | None = None) -> T:
        t0 = time.time()
        ctx = context or {}
        data = {
            "p1_email_extraction": lambda: mock_extract(ctx["email"]),
            "p2_address_request": lambda: mock_request(ctx),
            "p3_delay_alert": lambda: mock_alert(ctx),
            "p5_alert_judge": lambda: mock_judge(ctx),
        }[prompt.id]()
        out = schema.model_validate(data)
        log_call(CallRecord(prompt.ref, self.name, self.model_for(prompt.tier), int((time.time() - t0) * 1000)))
        return out

    def tool_loop(self, prompt: Prompt, user: str, tools: list[Tool], final_schema: type[T],
                  context: dict | None = None, max_steps: int = 6) -> tuple[T, list[dict]]:
        t0 = time.time()
        data, trace = mock_evidence(context or {})
        log_call(CallRecord(prompt.ref, self.name, self.model_for(prompt.tier), int((time.time() - t0) * 1000),
                            steps=len(trace) + 1))
        return final_schema.model_validate(data), trace


__all__ = ["MockProvider", "BaseModel"]
