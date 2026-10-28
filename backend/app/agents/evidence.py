"""Leadership evidence agent (P4): question -> read-only SQL tool loop -> grounded answer."""
from __future__ import annotations

import re
import threading
from functools import lru_cache

import duckdb

from ..config import settings
from ..data import load_dataset
from ..llm.base import Tool
from ..llm.prompts import load_prompt
from ..llm.providers import get_provider
from .schemas import EvidenceAnswer

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|detach|copy|export|import|install|load|pragma|call|set|"
    r"read_csv\w*|read_parquet|read_json\w*|glob|httpfs)\b", re.I)
_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def _conn() -> duckdb.DuckDBPyConnection:
    ds = load_dataset()
    con = duckdb.connect(":memory:")
    d = ds.deliveries.drop(columns=["month", "lane", "route_ratio", "after_cutoff", "clean_upstream"])
    for name, df in {"deliveries": d, "clients": ds.clients, "carriers": ds.carriers, "warehouses": ds.warehouses,
                     "sla_penalties": ds.penalties, "complaints": ds.complaints}.items():
        con.register(f"_{name}", df)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM _{name}")
        con.unregister(f"_{name}")
    for col in ("promised_date", "pick_list_date", "delivered_date"):
        con.execute(f"ALTER TABLE deliveries ALTER {col} TYPE DATE")
    con.execute("SET enable_external_access = false")  # no file or network access from SQL
    return con


def run_sql(query: str) -> dict:
    """Read-only guard: a single SELECT/WITH statement, no file/network functions, row-limited."""
    q = query.strip().rstrip(";").strip()
    if ";" in q:
        raise ValueError("only one statement is allowed")
    if not re.match(r"^(select|with)\b", q, re.I):
        raise ValueError("only SELECT or WITH queries are allowed")
    if FORBIDDEN.search(q):
        raise ValueError("query uses a forbidden keyword or function")
    with _LOCK:
        cur = _conn().execute(f"SELECT * FROM ({q}) AS _q LIMIT {settings.sql_row_limit}")
        cols = [c[0] for c in cur.description]
        rows = cur.fetchall()
    return {"columns": cols, "rows": [[_jsonable(v) for v in r] for r in rows], "row_count": len(rows)}


def _jsonable(v):
    if isinstance(v, float):
        return round(v, 6)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


SQL_TOOL_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string", "description": "One read-only DuckDB SELECT statement"}},
    "required": ["query"],
}


def _numbers(text: str) -> list[float]:
    return [float(x.replace(",", "")) for x in re.findall(r"(?<![\w-])\d[\d,]*\.?\d*", text)]


MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
NON_FIGURES = re.compile(
    r"\b(Tier|Carrier|W)\s?\d+|\bC\d{3}\b|\d{4}-\d{2}(-\d{2})?"          # labels: "Tier 2", "C049", "W3", "2026-08"
    r"|\b\d{1,2}[- ](?:day|week|month|quarter|year)s?\b"                 # time spans: "6-month window", "3 days"
    rf"|\b{MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?\b(?:,?\s+\d{{4}})?"         # dates: "April 1", "Sep 30, 2026"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTH}(?=\W|$)", re.I)             # dates: "1 April"
SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}
FIGURE = re.compile(r"(?<![\w.,])(?<![A-Za-z]-)(\d[\d,]*(?:\.\d+)?)\s*(thousand|million|billion|mn|bn|k|m|b)?\b", re.I)  # ranges like "24.7-24.8%" give both ends; "SKU-123" is a code
REGIONS = ("Midwest", "Southeast", "Northeast", "West")
ENTITY = re.compile(r"\bCarriers?\s+[A-F](?:(?:\s*,\s*|\s+and\s+|\s*&\s*)[A-F]\b)*|\bC\d{3}\b|\bTier\s?[123]\b|\bW\d\b|\b(?:"
                    + "|".join(REGIONS) + r")\b")
ENTITY_REACH = 60  # a figure belongs to the entity named at most this many characters before it, in the same clause


def _blank(text: str) -> str:
    """Labels, dates and time spans become spaces of the same length, so positions still line up with the answer."""
    return NON_FIGURES.sub(lambda m: " " * len(m.group(0)), text)


def _figures_at(text: str) -> list[tuple[float, float, int]]:
    """Numbers in prose as (value, rounding tolerance, position); "$14.2 million" -> (14_200_000, 50_000, i)."""
    out = []
    for m in FIGURE.finditer(_blank(text)):
        num, unit = m.group(1), m.group(2)
        v = float(num.replace(",", ""))
        if not unit and 2026 <= v <= 2027:
            continue  # a year
        if unit:
            decimals = len(num.split(".")[1]) if "." in num else 0
            sc = SCALE[unit.lower()]
            out.append((v * sc, 0.5 * 10 ** -decimals * sc, m.start()))
        else:
            out.append((v, 0.0, m.start()))
    return out


def _figures(text: str) -> list[tuple[float, float]]:
    return [(v, tol) for v, tol, _ in _figures_at(text)]


def _entity_for(text: str, pos: int) -> set[str] | None:
    """The entity a figure is about: the last one named shortly before it in the same clause ({"D"}, {"C049"}, ...)."""
    clause_start = max(text.rfind(c, 0, pos) for c in ".;(\n") + 1
    hits = [m for m in ENTITY.finditer(text, clause_start, pos) if pos - m.end() <= ENTITY_REACH]
    if not hits:
        return None
    e = hits[-1].group(0)
    if e.lower().startswith("carrier"):
        return set(re.findall(r"\b[A-F]\b", e.split(None, 1)[1]))
    return {e.replace(" ", "") if e.startswith("Tier") else e}


def _row_entities(row: list) -> set[str]:
    out = set()
    for v in row:
        if isinstance(v, str):
            out.add(v.strip())
            out.add(v.replace("Carrier ", "").replace(" ", "").strip())
    return out


def grounding(answer: EvidenceAnswer, trace: list[dict]) -> dict:
    """Claim-level check. Each figure must match a value in a query result (allowing %, rounding, K/M/B units and
    column totals), AND, when the sentence ties it to a carrier, client, region, tier or warehouse, the matching
    row must be that entity's row. An answerable answer with no successful query is never grounded.
    This verifies traceability, not correctness: answer accuracy is measured separately against gold SQL."""
    rows_all: list[list] = []
    for step in trace:
        out = step.get("output", {}) or {}
        if not out.get("error"):
            rows_all += out.get("rows", []) or []
    cands: list[tuple[float, set[str]]] = []
    for row in rows_all:
        ents = _row_entities(row)
        for v in row:
            if isinstance(v, int | float) and not isinstance(v, bool):
                cands += [(float(v), ents), (float(v) * 100, ents)]
    for step in trace:
        rows = (step.get("output", {}) or {}).get("rows", []) or []
        for col in zip(*rows, strict=False):  # column totals are derived figures the agent may report
            vals = [float(v) for v in col if isinstance(v, int | float) and not isinstance(v, bool)]
            if vals:
                cands.append((sum(vals), set()))

    def check(text: str) -> tuple[list[float], list[float]]:
        untraced, wrong_entity = [], []
        for n, tol, pos in _figures_at(text):
            match = [ents for p, ents in cands if abs(n - p) <= max(0.051, abs(p) * 0.005, tol) or round(p) == n]
            if not match:
                untraced.append(n)
                continue
            want = _entity_for(text, pos)
            if want and not any(not ents or (want & ents) for ents in match):
                wrong_entity.append(n)
        return untraced, wrong_entity

    untraced, wrong = check(answer.answer)
    fig_untraced: list[float] = []
    for f in answer.figures:
        u, w = check(f"{f.label}: {f.value}")
        fig_untraced += u + w
    n = len(_figures_at(answer.answer))
    no_query = answer.answerable and not rows_all
    grounded = not untraced and not wrong and not fig_untraced and not no_query
    return {"numbers_in_answer": n, "untraced_numbers": untraced, "wrong_entity_numbers": wrong,
            "untraced_figures": fig_untraced, "no_query_result": no_query, "grounded": grounded}


def ask(question: str, provider_name: str | None = None) -> dict:
    provider = get_provider(provider_name)
    p4 = load_prompt("p4_evidence_agent")
    tool = Tool("run_sql", "Run one read-only SELECT query on the Halcyon DuckDB and get rows back.",
                SQL_TOOL_SCHEMA, lambda args: run_sql(args["query"]))
    ctx = {"question": question, "run_sql": run_sql}
    answer, trace = provider.tool_loop(p4, p4.render_user(question=question), [tool], EvidenceAnswer, context=ctx)
    g = grounding(answer, trace) if answer.answerable else None
    repaired = False
    if g and not g["grounded"]:  # one repair round: name the problem, ask for a re-query; never pass it through silently
        problems = []
        if g["no_query_result"]:
            problems.append("you answered without a successful query result")
        if g["untraced_numbers"] or g["untraced_figures"]:
            problems.append(f"these numbers are in no query result: {g['untraced_numbers'] + g['untraced_figures']}")
        if g["wrong_entity_numbers"]:
            problems.append(f"these numbers belong to a different carrier, client, region or tier row: {g['wrong_entity_numbers']}")
        retry = (p4.render_user(question=question) + "\n\nA check on your previous answer found that "
                 + "; ".join(problems) + ". Run the queries again and state only numbers that appear in a result row for "
                 "the entity you name. If you cannot, set answerable to false.")
        answer2, trace2 = provider.tool_loop(p4, retry, [tool], EvidenceAnswer, context=ctx)
        g2 = grounding(answer2, trace2) if answer2.answerable else None
        answer, trace, g, repaired = answer2, trace + trace2, g2, True
    status = "declined" if not answer.answerable else ("verified" if g and g["grounded"] else "unverified")
    return {"question": question, "answer": answer.model_dump(), "trace": trace, "grounding": g, "status": status,
            "repaired": repaired, "provider": provider.name, "prompt": p4.ref}
