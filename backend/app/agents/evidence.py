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


def grounding(answer: EvidenceAnswer, trace: list[dict]) -> dict:
    """Every number in the answer should be traceable to a query result (allowing % and rounding)."""
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
    # labels like "Tier 2", "C049", "W3", "2026-08" are names, not figures
    text = re.sub(r"\b(Tier|Carrier|W)\s?\d+|\bC\d{3}\b|\d{4}-\d{2}(-\d{2})?", " ", answer.answer)
    nums = [n for n in _numbers(text) if not (2026 <= n <= 2027)]

    def found(n: float) -> bool:
        return any(abs(n - p) <= max(0.051, abs(p) * 0.005) or round(p) == n for p in pool)

    untraced = [n for n in nums if not found(n)]
    return {"numbers_in_answer": len(nums), "untraced_numbers": untraced, "grounded": not untraced}


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
