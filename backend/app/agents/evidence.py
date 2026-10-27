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
FIGURE = re.compile(r"(?<![\w.,])(?<![A-Za-z]-)(\d[\d,]*(?:\.\d+)?)\s*(thousand|million|billion|mn|bn|k|m|b)?\b", re.I)


def _figures(text: str) -> list[tuple[float, float]]:
    """Numbers in prose as (value, rounding tolerance); "$14.2 million" -> (14_200_000, 50_000)."""
    out = []
    for num, unit in FIGURE.findall(NON_FIGURES.sub(" ", text)):
        v = float(num.replace(",", ""))
        if not unit and 2026 <= v <= 2027:
            continue  # a year
        if unit:
            decimals = len(num.split(".")[1]) if "." in num else 0
            s = SCALE[unit.lower()]
            out.append((v * s, 0.5 * 10 ** -decimals * s))
        else:
            out.append((v, 0.0))
    return out


def grounding(answer: EvidenceAnswer, trace: list[dict]) -> dict:
    """Every figure in the answer should be traceable to a query result (allowing %, rounding and K/M/B units)."""
    pool: list[float] = []
    for step in trace:
        rows = step.get("output", {}).get("rows", []) or []
        for row in rows:
            for v in row:
                if isinstance(v, int | float):
                    pool += [float(v), float(v) * 100]
        for col in zip(*rows, strict=False):  # column totals are derived figures the agent may report
            vals = [float(v) for v in col if isinstance(v, int | float)]
            if vals:
                pool.append(sum(vals))

    def found(n: float, tol: float) -> bool:
        return any(abs(n - p) <= max(0.051, abs(p) * 0.005, tol) or round(p) == n for p in pool)

    figs = _figures(answer.answer)
    untraced = [n for n, tol in figs if not found(n, tol)]
    return {"numbers_in_answer": len(figs), "untraced_numbers": untraced, "grounded": not untraced}


def ask(question: str, provider_name: str | None = None) -> dict:
    provider = get_provider(provider_name)
    p4 = load_prompt("p4_evidence_agent")
    tool = Tool("run_sql", "Run one read-only SELECT query on the Halcyon DuckDB and get rows back.",
                SQL_TOOL_SCHEMA, lambda args: run_sql(args["query"]))
    answer, trace = provider.tool_loop(p4, p4.render_user(question=question), [tool], EvidenceAnswer,
                                       context={"question": question, "run_sql": run_sql})
    return {"question": question, "answer": answer.model_dump(), "trace": trace,
            "grounding": grounding(answer, trace) if answer.answerable else None,
            "provider": provider.name, "prompt": p4.ref}
