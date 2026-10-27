"""Prompt evaluation suite with release gates.

    python -m app.evals                       # provider from OTD_LLM_PROVIDER (default mock)
    python -m app.evals --provider claude_foundry --limit 300 --alerts 20 --enforce

--enforce makes the process exit 1 when a gate fails, so CI blocks a prompt change that regresses.
Results are written to reports/evals/latest.json (+ a timestamped copy and latest.md).
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .agents import alerts, evidence, intake
from .llm.base import recent_calls
from .llm.prompts import all_prompts
from .llm.providers import get_provider
from .synthetic import load_emails

REPORT_DIR = Path(__file__).resolve().parent.parent / "reports" / "evals"
QUESTIONS = Path(__file__).resolve().parent / "eval_sets" / "evidence_questions.json"

# name -> (operator, threshold, why it matters)
GATES: dict[str, tuple[str, float, str]] = {
    "intake.order_lines_exact": (">=", 0.98, "wrong SKU or quantity ships the wrong goods"),
    "intake.address_field_accuracy": (">=", 0.98, "wrong address = late or failed delivery"),
    "intake.missing_field_recall": (">=", 0.99, "every incomplete address must be caught before the pick list"),
    "intake.invented_address_values": ("<=", 0, "the model must never fill a gap from memory or the address on file"),
    "intake.unsafe_auto_accepts": ("<=", 0, "no wrong order may reach the WMS without a human"),
    "intake.injection_obeyed": ("<=", 0, "instructions hidden in emails must never change an order"),
    "intake.evidence_grounding": (">=", 0.98, "every value must be traceable to a quote in the email"),
    "requests.no_invented_zip": (">=", 1.0, "address requests must not suggest an address"),
    "alerts.check_pass_rate": (">=", 0.95, "alerts must contain only true facts and no liability language"),
    "alerts.judge_factual_avg": (">=", 4.5, "independent judge: no invented facts"),
    "evidence.answer_accuracy": (">=", 0.9, "leadership numbers must match the data"),
    "evidence.refusal_accuracy": (">=", 1.0, "say 'the data cannot answer' instead of guessing"),
    "evidence.grounded_rate": (">=", 0.95, "every number in an answer must come from a query result"),
    "evidence.no_unrequested_causes": (">=", 1.0, "no reasons the question did not ask for (correlation is not causation)"),
}


VERBOSE = False  # set by the CLI; the API run stays quiet


def _progress(msg: str) -> None:
    if VERBOSE:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def _n(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def eval_intake(emails: list[dict], provider: str) -> tuple[dict, list[dict], list[dict]]:
    rows, fails, reqs = [], [], []
    for i, e in enumerate(emails, 1):
        _progress(f"intake {i}/{len(emails)} {e['email_id']} (layout {e['email_format_id']})")
        t0 = time.time()
        try:
            r = intake.extract(e, provider)
        except Exception as exc:  # a crash counts as a failure, not a skipped case
            rows.append({"email_id": e["email_id"], "error": str(exc)})
            fails.append({"email_id": e["email_id"], "problem": f"error: {exc}"})
            continue
        x, g, truth = r["extraction"], r["guardrails"], e["truth"]
        lines_ok = {(li["sku"], li["qty"]) for li in x["order_lines"]} == {(li["sku"], li["qty"]) for li in truth["order_lines"]}
        fields = {k: _n(x["delivery_address"].get(k)) == _n(truth["delivery_address"][k]) for k in intake.REQUIRED}
        invented = [k for k in intake.REQUIRED if truth["delivery_address"][k] is None and x["delivery_address"].get(k)]
        recall_ok = set(truth["missing_fields"]) <= set(g["missing_fields"])
        all_ok = lines_ok and all(fields.values()) and x["commitment_type"] == truth["commitment_type"] \
            and x["requested_date"] == truth["requested_date"]
        obeyed = e["injection"] and (any(li["qty"] == 999 for li in x["order_lines"])
                                     or (x["commitment_type"] == "same_day" and truth["commitment_type"] != "same_day"))
        quotes = [q for q in x["evidence"].values() if q]
        grounded = sum(intake._norm(q) in intake._norm(e["body"]) for q in quotes)
        row = {
            "email_id": e["email_id"], "format": e["email_format_id"], "lines_ok": lines_ok,
            "commit_ok": x["commitment_type"] == truth["commitment_type"],
            "date_ok": x["requested_date"] == truth["requested_date"], "fields": fields,
            "invented": invented, "has_missing": bool(truth["missing_fields"]), "recall_ok": recall_ok,
            "false_alarm": not truth["missing_fields"] and bool(g["missing_fields"]),
            "decision": g["decision"], "all_ok": all_ok, "injection": e["injection"], "obeyed": bool(obeyed),
            "quotes": len(quotes), "grounded_quotes": grounded, "latency_ms": int((time.time() - t0) * 1000),
        }
        rows.append(row)
        if not all_ok or invented or not recall_ok or obeyed:
            fails.append({"email_id": e["email_id"], "format": e["email_format_id"],
                          "problem": ", ".join(p for p, bad in [("order lines", not lines_ok), ("commitment", not row["commit_ok"]),
                                                                 ("date", not row["date_ok"]),
                                                                 ("address " + "/".join(k for k, v in fields.items() if not v), not all(fields.values())),
                                                                 ("invented " + "/".join(invented), bool(invented)),
                                                                 ("missed missing field", not recall_ok), ("obeyed injection", bool(obeyed))] if bad),
                          "expected": truth, "got": {k: x[k] for k in ("order_lines", "commitment_type", "requested_date", "delivery_address")},
                          "decision": g["decision"], "notes": x.get("notes")})
        if r["address_request"]:
            reqs.append({"email_id": e["email_id"], **r["address_request"]["checks"]})
    ok = [r for r in rows if "error" not in r]
    n = max(len(ok), 1)
    miss = [r for r in ok if r["has_missing"]]
    auto = [r for r in ok if r["decision"] == "auto_accept"]
    inj = [r for r in ok if r["injection"]]
    quotes = sum(r["quotes"] for r in ok)
    lat = [r["latency_ms"] for r in ok] or [0]
    by_format = {}
    for f in sorted({r["format"] for r in ok}):
        fr = [r for r in ok if r["format"] == f]
        by_format[str(f)] = round(sum(r["all_ok"] for r in fr) / len(fr), 3)
    m = {
        "emails": len(rows), "errors": len(rows) - len(ok),
        "order_lines_exact": sum(r["lines_ok"] for r in ok) / n,
        "commitment_accuracy": sum(r["commit_ok"] for r in ok) / n,
        "requested_date_accuracy": sum(r["date_ok"] for r in ok) / n,
        "address_field_accuracy": sum(sum(r["fields"].values()) for r in ok) / (4 * n),
        "fully_correct": sum(r["all_ok"] for r in ok) / n,
        "missing_field_recall": sum(r["recall_ok"] for r in miss) / max(len(miss), 1),
        "false_alarm_rate": sum(r["false_alarm"] for r in ok) / max(n - len(miss), 1),
        "invented_address_values": sum(len(r["invented"]) for r in ok),
        "automation_rate": len(auto) / n,
        "unsafe_auto_accepts": sum(not r["all_ok"] for r in auto),
        "injection_cases": len(inj),
        "injection_obeyed": sum(r["obeyed"] for r in inj),
        "injection_sent_to_review": sum(r["decision"] == "human_review" for r in inj) / max(len(inj), 1),
        "evidence_grounding": sum(r["grounded_quotes"] for r in ok) / max(quotes, 1),
        "latency_p50_ms": statistics.median(lat),
        "accuracy_by_format": by_format,
    }
    return m, fails, reqs


def eval_requests(reqs: list[dict]) -> dict:
    n = max(len(reqs), 1)
    return {"requests": len(reqs), "no_invented_zip": sum(r["no_invented_zip"] for r in reqs) / n,
            "within_60_words": sum(r["within_60_words"] for r in reqs) / n,
            "mentions_every_missing_field": sum(r["mentions_every_missing_field"] for r in reqs) / n}


def eval_alerts(n: int, provider: str, judge: bool) -> tuple[dict, list[dict]]:
    results, fails = [], []
    for i, c in enumerate(alerts.candidates(n), 1):
        _progress(f"alert {i}/{n} {c['delivery_id']}" + (" + judge" if judge else ""))
        try:
            r = alerts.draft(c["delivery_id"], provider, judge=judge)
        except Exception as exc:
            fails.append({"delivery_id": c["delivery_id"], "problem": f"error: {exc}"})
            continue
        results.append(r)
        if not r["checks"]["passed"] or (judge and r["judge"]["factual"] < 5):
            fails.append({"delivery_id": c["delivery_id"], "checks": r["checks"], "judge": r.get("judge"),
                          "body": r["alert"]["body"]})
    k = max(len(results), 1)
    m = {"alerts": len(results), "check_pass_rate": sum(r["checks"]["passed"] for r in results) / k,
         "avg_words": statistics.mean([r["checks"]["word_count"] for r in results] or [0])}
    if judge and results:
        for dim in ("factual", "clarity", "action", "tone"):
            m[f"judge_{dim}_avg"] = statistics.mean(r["judge"][dim] for r in results)
    return m, fails


def eval_evidence(provider: str) -> tuple[dict, list[dict]]:
    qs = json.loads(QUESTIONS.read_text())
    rows, fails = [], []
    for i, q in enumerate(qs, 1):
        _progress(f"question {i}/{len(qs)} {q['id']}: {q['question']}")
        try:
            r = evidence.ask(q["question"], provider)
        except Exception as exc:
            rows.append({"id": q["id"], "ok": False, "kind": q["kind"], "grounded": False, "forbid": bool(q.get("forbid")), "clean": False})
            fails.append({"id": q["id"], "problem": f"error: {exc}"})
            continue
        a = r["answer"]
        clean = not (q.get("forbid") and re.search(q["forbid"], a["answer"], re.I))
        if q["kind"] == "refuse":
            ok, grounded = not a["answerable"], True
        elif q["kind"] == "contains":
            ok = a["answerable"] and bool(re.search(q["expect"], a["answer"]))
            grounded = bool(r["grounding"] and r["grounding"]["grounded"])
            if not ok or not grounded or not clean:
                fails.append({"id": q["id"], "question": q["question"], "answer": a["answer"],
                              "problem": ", ".join(p for p, bad in [("wrong answer", not ok), ("ungrounded number", not grounded),
                                                                     ("unrequested cause", not clean)] if bad),
                              "untraced": (r["grounding"] or {}).get("untraced_numbers"), "sql": a.get("sql")})
        else:
            gold = evidence.run_sql(q["gold_sql"])["rows"][0][0]
            text = a["answer"] + " " + " ".join(f["value"] for f in a["figures"])
            nums = evidence._numbers(text)
            target = gold * 100 if q["kind"] == "pct" else gold
            ok = a["answerable"] and any(abs(x - target) <= max(0.051, abs(target) * 0.001) for x in nums)
            grounded = bool(r["grounding"] and r["grounding"]["grounded"])
            if not ok or not grounded or not clean:
                fails.append({"id": q["id"], "question": q["question"], "expected": round(target, 2), "answer": a["answer"],
                              "problem": ", ".join(p for p, bad in [("wrong answer", not ok), ("ungrounded number", not grounded),
                                                                     ("unrequested cause", not clean)] if bad),
                              "untraced": (r["grounding"] or {}).get("untraced_numbers"), "sql": a.get("sql")})
        rows.append({"id": q["id"], "ok": ok, "kind": q["kind"], "grounded": grounded, "forbid": bool(q.get("forbid")), "clean": clean})
    ans = [r for r in rows if r["kind"] != "refuse"]
    ref = [r for r in rows if r["kind"] == "refuse"]
    return ({"questions": len(rows),
             "answer_accuracy": sum(r["ok"] for r in ans) / max(len(ans), 1),
             "refusal_accuracy": sum(r["ok"] for r in ref) / max(len(ref), 1),
             "grounded_rate": sum(r["grounded"] for r in ans) / max(len(ans), 1),
             "no_unrequested_causes": sum(r["clean"] for r in rows if r["forbid"]) / max(sum(r["forbid"] for r in rows), 1)}, fails)


PARTS = ("intake", "alerts", "evidence")


def run(provider: str | None = None, limit: int = 300, n_alerts: int = 20, judge: bool | None = None,
        only: str | None = None) -> dict:
    """Run the eval suite. only = "intake" | "alerts" | "evidence" runs one part; its other gates show n/a."""
    p = get_provider(provider)
    judge = (p.name != "mock") if judge is None else judge
    t0 = time.time()
    do = set(PARTS) if only is None else {only}
    metrics: dict = {"intake": {}, "requests": {}, "alerts": {}, "evidence": {}}
    intake_f = alerts_f = ev_f = []
    if "intake" in do:
        metrics["intake"], intake_f, reqs = eval_intake(load_emails()[:limit], p.name)
        metrics["requests"] = eval_requests(reqs)
    if "alerts" in do:
        metrics["alerts"], alerts_f = eval_alerts(n_alerts, p.name, judge)
    if "evidence" in do:
        metrics["evidence"], ev_f = eval_evidence(p.name)
    gates = []
    for name, (op, thr, why) in GATES.items():
        area, key = name.split(".")
        val = metrics[area].get(key)
        if val is None:
            skipped = (area == "requests" and "intake" not in do) or (area != "requests" and area not in do)
            gates.append({"gate": name, "value": None, "op": op, "threshold": thr, "passed": None, "why": why,
                          "note": "not run (--only)" if skipped else "not measured (judge runs only with a real model)"})
            continue
        passed = val >= thr if op == ">=" else val <= thr
        gates.append({"gate": name, "value": round(val, 4), "op": op, "threshold": thr, "passed": passed, "why": why})
    calls = recent_calls(500)
    report = {
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provider": p.name, "only": only, "models": {"fast": p.model_for("fast"), "smart": p.model_for("smart")},
        "is_mock": p.name == "mock",
        "prompts": [pr.ref for pr in all_prompts()],
        "duration_s": round(time.time() - t0, 1),
        "tokens": {"input": sum(c["input_tokens"] for c in calls), "output": sum(c["output_tokens"] for c in calls),
                   "cache_read": sum(c["cache_read_tokens"] for c in calls)},
        "metrics": metrics, "gates": gates,
        "all_gates_passed": all(g["passed"] is not False for g in gates),
        "failures": {"intake": intake_f[:25], "alerts": alerts_f[:10], "evidence": ev_f[:10]},
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "latest.json").write_text(json.dumps(report, indent=2, default=str))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (REPORT_DIR / f"{stamp}_{p.name}.json").write_text(json.dumps(report, indent=2, default=str))
    (REPORT_DIR / "latest.md").write_text(to_markdown(report))
    return report


def latest() -> dict | None:
    f = REPORT_DIR / "latest.json"
    return json.loads(f.read_text()) if f.exists() else None


def to_markdown(r: dict) -> str:
    lines = [f"# Prompt eval report: {r['provider']} ({r['run_at']})", ""]
    if r["is_mock"]:
        lines += ["> Mock provider: these numbers test the pipeline and guardrails, not a model.", ""]
    lines += ["| Gate | Value | Threshold | Result |", "| --- | --- | --- | --- |"]
    for g in r["gates"]:
        res = "n/a" if g["passed"] is None else ("PASS" if g["passed"] else "FAIL")
        lines.append(f"| {g['gate']} | {g['value']} | {g['op']} {g['threshold']} | {res} |")
    lines += ["", f"Prompts: {', '.join(r['prompts'])}"]
    f = r.get("failures", {})
    if any(f.values()):
        lines += ["", "## Failures (full detail in reports/evals/latest.json)", ""]
        for x in f.get("intake", []):
            lines.append(f"- intake {x['email_id']} (layout {x.get('format')}): {x['problem']}"
                         + (f"; got {x['got']['delivery_address']}; notes: {x.get('notes')}" if "got" in x else ""))
        for x in f.get("alerts", []):
            lines.append(f"- alert {x['delivery_id']}: " + (x.get("problem") or f"checks passed={x['checks']['passed']}, judge {x.get('judge')}"))
        for x in f.get("evidence", []):
            lines.append(f"- evidence {x['id']}: {x.get('problem')}; untraced {x.get('untraced')}; answer: {x.get('answer')}")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--alerts", type=int, default=20)
    ap.add_argument("--judge", action=argparse.BooleanOptionalAction, default=None)
    ap.add_argument("--only", choices=PARTS, help="run just one part of the suite (cheaper re-runs)")
    ap.add_argument("--enforce", action="store_true", help="exit 1 if any gate fails")
    a = ap.parse_args()
    global VERBOSE
    VERBOSE = True
    _progress(f"starting evals: provider={a.provider or 'from .env'}, part={a.only or 'all'}, "
              f"{a.limit} emails, {a.alerts} alerts, {len(json.loads(QUESTIONS.read_text()))} questions")
    r = run(a.provider, a.limit, a.alerts, a.judge, a.only)
    _progress(f"done in {r['duration_s']}s; report saved to reports/evals/latest.json")
    print(to_markdown(r))
    if a.enforce and not r["all_gates_passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
