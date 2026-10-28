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
    r"\b(Tier|Carrier|W|Client)\s?\d+|\bC\d{3}\b|\d{4}-\d{2}(-\d{2})?"   # labels: "Tier 2", "C049", "Client 033", "W3", "2026-08"
    r"|\b\d{1,2}-(?:day|week|month|quarter|year)\b"                     # the data's window: "6-month window"
    r"|\b\d{1,2} months? of (?:data|history|deliveries|records)\b"        # ... "6 months of data"; "9 days late" IS a claim
    rf"|\b{MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?\b(?:,?\s+\d{{4}})?"         # dates: "April 1", "Sep 30, 2026"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTH}(?=\W|$)", re.I)             # dates: "1 April"
SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}
# ranges like "24.7-24.8%" give both ends; "SKU-123" is a code
FIGURE = re.compile(r"(?<![\w.,])(?<![A-Za-z]-)(\d(?:[\d,]*\d)?(?:\.\d+)?)\s*(thousand|million|billion|mn|bn|k|m|b)?\b", re.I)
REGIONS = ("Midwest", "Southeast", "Northeast", "West")
ENTITY = re.compile(r"\b(?i:carriers?),?\s+[A-F](?:(?:\s*,\s*|\s+and\s+|\s*&\s*)[A-F]\b)*"
                    r"|\bC\d{3}\b|\bClient \d{3}\b|\bTier\s?[123]\b|\bW\d\b|\b(?:" + "|".join(REGIONS) + r")\b")
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


def _ent_set(e: str) -> set[str]:
    if e.startswith("Client "):
        return {"C" + e.split()[1], e, e.replace(" ", "")}
    if e.lower().startswith("carrier"):
        return set(re.findall(r"\b[A-F]\b", e.split(None, 1)[1].lstrip(", ")))
    return {e.replace(" ", "") if e.startswith("Tier") else e}


CLAUSE_END = re.compile(r"[.;)](?=\s|$)|\(|\n")
SENTENCE_END = re.compile(r"[.!?](?=\s|$)|\n")
PRONOUN_START = re.compile(r"^\s*(?:its|it|their|they|this|that|these|those|the carrier|the client|the region)\b", re.I)


def _entity_for(text: str, pos: int) -> set[str] | None:
    """The entity a figure is about: the one named nearest it in its clause (before, else after); in a sentence that
    opens with a pronoun ("Its late rate is..."), the last entity named in the sentence before."""
    # clause and sentence boundaries; a decimal point ("39.5") is not one
    bounds = [m.end() for m in CLAUSE_END.finditer(text)]
    clause_start = max([b for b in bounds if b <= pos], default=0)
    clause_end = min([b for b in bounds if b > pos], default=len(text))
    fig_end = pos + len(FIGURE.match(_blank(text), pos).group(0)) if FIGURE.match(_blank(text), pos) else pos
    near = [(pos - m.end(), m) for m in ENTITY.finditer(text, clause_start, pos) if pos - m.end() <= ENTITY_REACH]
    # an entity AFTER the figure binds only when nothing separates them ("24.7% ... with Carrier D"), not across a
    # colon or comma ("total $872,460: Tier 2 $310,680")
    near += [(m.start() - fig_end, m) for m in ENTITY.finditer(text, fig_end, clause_end)
             if m.start() - fig_end <= ENTITY_REACH and not re.search(r"[:,;]", text[fig_end:m.start()])]
    if near:  # the entity named closest to the figure, on either side
        return _ent_set(min(near, key=lambda t: t[0])[1].group(0))
    sent_start = max([m.end() for m in SENTENCE_END.finditer(text) if m.end() <= pos], default=0)
    if PRONOUN_START.match(text[sent_start:pos]):
        prev = list(ENTITY.finditer(text, 0, sent_start))
        if prev:
            return _ent_set(prev[-1].group(0))
    return None


def _row_entities(row: list) -> set[str]:
    out = set()
    for v in row:
        if isinstance(v, str):
            out.add(v.strip())
            out.add(v.replace("Carrier ", "").replace(" ", "").strip())
    return out


LATE_WORDS = re.compile(r"\blate\b|\blateness\b|\bdelay|\bfail\w*|\bmiss\w*", re.I)
ONTIME_WORDS = re.compile(r"on[- ]time|\bOTD\b|punctual|\bmet\b|\bhit\b", re.I)
RATE_COL = re.compile(r"rate|pct|percent|share|ratio|otd|proportion|frac", re.I)
METRIC_REACH = 40


def _col_kind(name: str) -> str | None:
    n = (name or "").lower()
    if "on_time" in n or "ontime" in n or "otd" in n:
        return "ontime"
    if "late" in n or "delay" in n:
        return "late"
    if re.search(r"deliveries|orders|shipments|total|count|volume|\bn\b", n):
        return "total"  # an all-deliveries count, as opposed to a late count
    return None


def _fig_unit(text: str, pos: int) -> str:
    m = FIGURE.match(_blank(text), pos)
    tail = text[m.end() if m else pos:][:24].lower()
    if tail.lstrip().startswith(("%", "percent", "per cent", "percentage point")):
        return "pct"
    # the first end of a range takes the unit written after the second: "24.7-24.8%", "24.7 to 24.8%"
    return "pct" if re.match(r"\s*(?:[-\u2013]|to)\s*\d[\d.,]*\s*(?:%|percent)", tail) else "raw"


def _fig_kinds(text: str) -> dict[int, str]:
    """Is each figure a late figure or an on-time figure? Each metric word belongs to the figure nearest it in its
    clause ("39.5% late, so 60.5% were on time": "late" is 39.5's, "on time" is 60.5's)."""
    figs = [pos for _, _, pos in _figures_at(text)]
    bounds = [m.end() for m in CLAUSE_END.finditer(text)]
    clause = lambda i: max([b for b in bounds if b <= i], default=0)  # noqa: E731
    best: dict[int, tuple[int, str]] = {}
    for kind, rx in (("late", LATE_WORDS), ("ontime", ONTIME_WORDS)):
        for m in rx.finditer(text):
            same = [f for f in figs if clause(f) == clause(m.start())]
            if not same:
                continue
            f = min(same, key=lambda f: abs(f - m.start()))
            d = abs(f - m.start())
            if d <= METRIC_REACH and (f not in best or d < best[f][0]):
                best[f] = (d, kind)
    kinds = {f: k for f, (_, k) in best.items()}
    # a COUNT with no late/on-time word is a count of all deliveries ("3,525 total deliveries", "8,928 deliveries"),
    # except the part in "3,525 of 8,928 deliveries", which is a subset of unknown metric
    for _, _, f in _figures_at(text):
        if f in kinds or _fig_unit(text, f) != "raw":
            continue
        m = FIGURE.match(_blank(text), f)
        after = text[m.end():] if m else ""
        if re.match(r"\s*of\s+\d", after):
            continue
        if re.match(r"\s*(?:total\s+)?(?:deliveries|orders|shipments)\b|\s*in total\b", after, re.I) or \
                re.search(r"\btotal(?: of)?\s*$|\ball\s*$", text[max(0, f - 12):f], re.I):
            kinds[f] = "total"
    return kinds


ALL_ROWS = "__all_rows__"  # a column total belongs to no single entity


def _respectively(text: str) -> dict[int, set[str]]:
    """"Carrier B was best and Carrier D worst, at 24.7% and 39.5% respectively": pair entities and figures in order."""
    out: dict[int, set[str]] = {}
    starts = [0] + [m.end() for m in SENTENCE_END.finditer(text)]
    for a, b in zip(starts, [*starts[1:], len(text)], strict=False):
        sent = text[a:b]
        if "respectively" not in sent.lower():
            continue
        ents: list[set[str]] = []
        for m in ENTITY.finditer(sent):
            es = _ent_set(m.group(0))
            ents += [{e} for e in sorted(es)] if m.group(0).lower().startswith("carrier") and len(es) > 1 else [es]
        figs = [pos for _, _, pos in _figures_at(text) if a <= pos < b]
        if ents and len(ents) == len(figs):
            out.update(dict(zip(figs, ents, strict=True)))
    return out


def grounding(answer: EvidenceAnswer, trace: list[dict]) -> dict:
    """Claim-level check that binds ENTITY + METRIC + UNIT + VALUE. Each figure must match a value in a query result
    (allowing rounding and K/M/B units) such that:
      - the unit agrees: "39.5%" matches a rate (0.395 x 100), never a count; "8,928" matches a raw value;
      - the metric agrees: a figure called "late" cannot match an on-time column or the complement of a late rate;
      - the entity agrees: a figure stated for Carrier A must come from Carrier A's row; a column total only
        backs a figure that names no entity.
    An answerable answer with no successful query is never grounded. This verifies traceability, not correctness:
    answer accuracy is measured separately against gold SQL."""
    cands: list[tuple[float, object, str | None, str]] = []  # value, entities (set or ALL_ROWS), metric kind, unit
    any_rows = False
    for step in trace:
        out = step.get("output", {}) or {}
        if out.get("error"):
            continue
        rows = out.get("rows", []) or []
        cols = out.get("columns") or []
        any_rows = any_rows or bool(rows)
        for row in rows:
            ents = _row_entities(row)
            for ci, v in enumerate(row):
                if not isinstance(v, int | float) or isinstance(v, bool):
                    continue
                kind = _col_kind(cols[ci]) if ci < len(cols) else None
                v = float(v)
                cands.append((v, ents, kind, "raw"))
                if -1 <= v <= 1:  # a rate stored as a fraction is stated as a percentage
                    cands.append((v * 100, ents, kind, "pct"))
                    if 0 <= v and kind:  # its complement, with the metric flipped: late 39.5% <-> on time 60.5%
                        cands.append((100 - v * 100, ents, "ontime" if kind == "late" else "late", "pct"))
                elif 1 < v <= 100 and ci < len(cols) and RATE_COL.search(cols[ci]):  # a rate already in percent ("share_pct")
                    cands.append((v, ents, kind, "pct"))
        for ci, col in enumerate(zip(*rows, strict=False)):
            vals = [float(v) for v in col if isinstance(v, int | float) and not isinstance(v, bool)]
            if vals:
                # a total backs a figure naming no entity, or naming every entity the rows cover ("B and D together")
                cands.append((sum(vals), (ALL_ROWS, [_row_entities(r) for r in rows]),
                              _col_kind(cols[ci]) if ci < len(cols) else None, "raw"))

    def check(text: str) -> tuple[list[float], list[float]]:
        untraced, wrong = [], []
        kinds = _fig_kinds(text)
        resp = _respectively(text)
        for n, tol, pos in _figures_at(text):
            unit, kind = _fig_unit(text, pos), kinds.get(pos)
            want = resp.get(pos) or _entity_for(text, pos)
            near = [c for c in cands if c[3] == unit and (abs(n - c[0]) <= max(0.051, abs(c[0]) * 0.005, tol) or round(c[0]) == n)]
            if not near:
                untraced.append(n)
                continue
            def entity_ok(c, want=want) -> bool:
                if isinstance(c[1], tuple):  # a column total
                    return want is None or all(want & ents for ents in c[1][1] if ents)
                return want is None or not c[1] or bool(want & c[1])
            ok = [c for c in near if (kind is None or c[2] is None or c[2] == kind) and entity_ok(c)]
            if not ok:
                wrong.append(n)
        return untraced, wrong

    untraced, wrong = check(answer.answer)
    fig_untraced: list[float] = []
    for f in answer.figures:
        u, w = check(f"{f.label}: {f.value}")
        fig_untraced += u + w
    n = len(_figures_at(answer.answer))
    no_query = answer.answerable and not any_rows
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
