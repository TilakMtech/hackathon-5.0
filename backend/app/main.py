"""Halcyon OTD control tower API (slice 1: analytics)."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import analytics
from .config import settings
from .data import load_dataset


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Load data, fit the model and warm the prediction cache so first requests are fast
    load_dataset()
    analytics.fit_model()
    analytics.drivers()
    analytics.bridge()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Halcyon OTD Control Tower API",
    version="0.1.0",
    description="Root-cause analytics for late deliveries. Every figure is computed from the hackathon dataset.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "deliveries": len(load_dataset().deliveries)}


@app.get("/api/overview")
def overview() -> dict:
    return analytics.overview()


@app.get("/api/drivers")
def drivers() -> dict:
    return analytics.drivers()


@app.get("/api/bridge")
def bridge() -> dict:
    return analytics.bridge()


@app.get("/api/lanes")
def lanes() -> dict:
    return analytics.lanes()


@app.get("/api/stock")
def stock(top: int = Query(15, ge=1, le=100)) -> dict:
    return analytics.stock(top=top)


@app.get("/api/accounts")
def accounts() -> dict:
    return analytics.accounts()


@app.get("/api/deliveries")
def deliveries(
    client_id: str | None = None,
    late_only: bool = False,
    driver: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    if driver and driver not in analytics.DRIVER_KEYS:
        raise HTTPException(400, f"driver must be one of {analytics.DRIVER_KEYS}")
    return analytics.deliveries(client_id, late_only, driver, limit, offset)


class SimulateRequest(BaseModel):
    fixes: dict[str, float] = Field(
        default_factory=dict,
        description="Share of affected orders fixed per driver (0-1), keys: " + ", ".join(analytics.DRIVER_KEYS),
    )
    route_cut: float = Field(0.0, ge=0, le=0.5, description="Fractional cut in stops and route km")


@app.post("/api/simulate")
def simulate(req: SimulateRequest) -> dict:
    unknown = set(req.fixes) - set(analytics.DRIVER_KEYS)
    if unknown:
        raise HTTPException(400, f"unknown drivers: {sorted(unknown)}")
    return analytics.simulate(req.fixes, req.route_cut)


# ---------------- Slice 2: LLM agents ----------------

from .agents import alerts as alert_agent  # noqa: E402
from .agents import evidence as evidence_agent  # noqa: E402
from .agents import intake as intake_agent  # noqa: E402
from .llm.base import LLMError, recent_calls  # noqa: E402
from .llm.prompts import all_prompts  # noqa: E402
from .llm.providers import get_provider  # noqa: E402
from .synthetic import load_emails  # noqa: E402


def _llm_error(exc: Exception) -> HTTPException:
    return HTTPException(502, f"LLM call failed: {exc}")


@app.get("/api/llm/status")
def llm_status() -> dict:
    try:
        p = get_provider()
        return {"provider": p.name, "models": {"fast": p.model_for("fast"), "smart": p.model_for("smart")},
                "is_mock": p.name == "mock", "prompts": [pr.ref for pr in all_prompts()]}
    except Exception as exc:  # misconfigured provider: report it instead of crashing the dashboard
        return {"provider": settings.llm_provider, "error": str(exc), "is_mock": False, "prompts": []}


@app.get("/api/llm/calls")
def llm_calls(n: int = Query(50, ge=1, le=500)) -> list[dict]:
    return recent_calls(n)


@app.get("/api/intake/samples")
def intake_samples(limit: int = Query(42, ge=1, le=300)) -> list[dict]:
    return [{"email_id": e["email_id"], "client_id": e["client_id"], "format": e["email_format_id"],
             "received_ts": e["received_ts"], "subject": e["subject"], "body": e["body"],
             "default_address": e["default_address"], "injection": e["injection"],
             "has_missing": bool(e["truth"]["missing_fields"])} for e in load_emails()[:limit]]


class IntakeRequest(BaseModel):
    email_id: str | None = None
    body: str | None = None
    client_id: str = "C000"
    received_ts: str = "2026-10-12T09:30"
    default_address: str = "none"


@app.post("/api/intake/extract")
def intake_extract(req: IntakeRequest) -> dict:
    if req.email_id:
        email = next((e for e in load_emails() if e["email_id"] == req.email_id), None)
        if not email:
            raise HTTPException(404, "unknown email_id")
    elif req.body and req.body.strip():
        email = {"email_id": "PASTED", "client_id": req.client_id, "email_format_id": "unknown",
                 "received_ts": req.received_ts, "body": req.body[:8000], "default_address": req.default_address}
    else:
        raise HTTPException(400, "send email_id or body")
    try:
        out = intake_agent.extract(email)
    except (LLMError, Exception) as exc:
        raise _llm_error(exc) from exc
    if "truth" in email:
        out["truth"] = email["truth"]
    return out


@app.get("/api/alerts/candidates")
def alert_candidates(limit: int = Query(25, ge=1, le=100)) -> list[dict]:
    return alert_agent.candidates(limit)


class AlertRequest(BaseModel):
    delivery_id: str
    judge: bool = False


@app.get("/api/alerts/trigger-stats")
def alert_trigger_stats() -> dict:
    return alert_agent.trigger_stats()


@app.post("/api/alerts/draft")
def alert_draft(req: AlertRequest) -> dict:
    try:
        return alert_agent.draft(req.delivery_id, judge=req.judge)
    except KeyError as exc:
        raise HTTPException(404, "unknown delivery_id") from exc
    except Exception as exc:
        raise _llm_error(exc) from exc


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)


@app.post("/api/ask")
def ask(req: AskRequest) -> dict:
    try:
        return evidence_agent.ask(req.question)
    except Exception as exc:
        raise _llm_error(exc) from exc


@app.get("/api/evals/latest")
def evals_latest() -> dict:
    from . import evals
    r = evals.latest()
    if not r:
        raise HTTPException(404, "no eval report yet: run `python -m app.evals` or POST /api/evals/run")
    return r


@app.post("/api/evals/run")
def evals_run(limit: int = Query(60, ge=5, le=300), alerts: int = Query(10, ge=1, le=50)) -> dict:
    from . import evals
    return evals.run(limit=limit, n_alerts=alerts)
