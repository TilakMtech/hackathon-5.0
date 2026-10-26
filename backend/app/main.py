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
