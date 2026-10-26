"""Late-cause analytics for Halcyon.

All numbers shown to leadership come from here: plain pandas + a logistic regression.
The LLM agents (next slice) only read these results; they never compute figures.
"""
from __future__ import annotations

from functools import lru_cache
from itertools import product

import numpy as np
import pandas as pd
import statsmodels.formula.api as smf

from .config import settings
from .data import Dataset, load_dataset

DRIVERS: list[dict] = [
    {
        "key": "drv_address", "label": "Incomplete address at intake",
        "step": "3. Email order keying", "fix": "Email intake agent + address check",
    },
    {
        "key": "drv_cutoff", "label": "Same-day order after the 8am cutoff",
        "step": "5. Pick list", "fix": "Second pick wave for same-day orders",
    },
    {
        "key": "drv_stock", "label": "Nearest warehouse had no stock",
        "step": "7. Stock placement", "fix": "Monthly stock re-placement",
    },
    {
        "key": "drv_lane", "label": "Carrier sent to a lane it fails on",
        "step": "8. Carrier choice", "fix": "Lane-based carrier rules",
    },
]
DRIVER_KEYS = [x["key"] for x in DRIVERS]
FORMULA = (
    "late ~ drv_address + drv_stock + drv_cutoff + drv_lane"
    " + C(commitment_type) + C(order_channel) + stops_on_route + planned_route_km"
)


def _r(x: float, n: int = 4) -> float:
    return float(round(float(x), n))


@lru_cache(maxsize=1)
def fit_model():
    d = load_dataset().deliveries
    return smf.logit(FORMULA, d).fit(disp=0)


def _predict_mean(d: pd.DataFrame, zero: tuple[str, ...] = (), route_cut: float = 0.0) -> float:
    """Mean predicted late probability on the full dataset. Cached: the data is static per deploy."""
    return _predict_mean_cached(tuple(sorted(zero)), round(float(route_cut), 4))


@lru_cache(maxsize=256)
def _predict_mean_cached(zero: tuple[str, ...], route_cut: float) -> float:
    d = load_dataset().deliveries
    x = d
    if zero or route_cut:
        x = d.copy()
        for k in zero:
            x[k] = 0
        if route_cut:
            x["stops_on_route"] = x.stops_on_route * (1 - route_cut)
            x["planned_route_km"] = x.planned_route_km * (1 - route_cut)
    return float(fit_model().predict(x).mean())


# ---------- overview ----------

def overview(ds: Dataset | None = None) -> dict:
    ds = ds or load_dataset()
    d = ds.deliveries
    monthly = (
        d.groupby("month").agg(otd=("late", lambda s: 1 - s.mean()), deliveries=("late", "size")).reset_index()
    )
    cl = ds.clients
    threat = cl[cl.account_status == "threatening_to_leave"]
    cs = ds.cs_time[["hours_total", "hours_on_delivery_complaints", "hours_keying_email_orders",
                     "hours_on_proactive_client_work"]].sum()
    late = d[d.late == 1]
    return {
        "deliveries": len(d),
        "period": {"from": str(d.order_received_ts.min().date()), "to": str(d.order_received_ts.max().date())},
        "otd": _r(1 - d.late.mean()),
        "otd_target": settings.otd_target,
        "otd_benchmark": settings.otd_benchmark,
        "otd_clean_orders": _r(1 - d.loc[d.any_driver == 0, "late"].mean()),
        "clean_orders": int((d.any_driver == 0).sum()),
        "late_with_driver_share": _r(late.any_driver.mean()),
        "monthly": [
            {"month": r.month, "otd": _r(r.otd), "deliveries": int(r.deliveries)} for r in monthly.itertuples()
        ],
        "cost": {
            "sla_penalties_usd": int(ds.penalties.penalty_usd.sum()),
            "redelivery_usd": int(d.redelivery_cost_usd.sum()),
        },
        "complaints": {
            "total": len(ds.complaints),
            "by_category": ds.complaints.category.value_counts().to_dict(),
            "from_threatening_accounts": int(ds.complaints.client_id.isin(threat.client_id).sum()),
        },
        "revenue_at_risk_usd": int(threat.annual_revenue_usd.sum()),
        "threatening_accounts": len(threat),
        "cs_hours_week": {k: _r(v, 1) for k, v in cs.items()},
        "data_quality": ds.quality,
    }


# ---------- drivers ----------

def drivers(ds: Dataset | None = None) -> dict:
    ds = ds or load_dataset()
    d = ds.deliveries
    m = fit_model()
    base = 1 - _predict_mean(d)
    pen = ds.penalties.merge(d[["delivery_id", *DRIVER_KEYS]], on="delivery_id")
    rows = []
    for drv in DRIVERS:
        k = drv["key"]
        with_, without = d[d[k] == 1], d[d[k] == 0]
        if k == "drv_cutoff":  # compare like with like: same-day orders before the cutoff
            without = d[(d.commitment_type == "same_day") & (d[k] == 0)]
        rows.append({
            **drv,
            "share": _r(d[k].mean()),
            "deliveries": int(d[k].sum()),
            "late_rate_with": _r(with_.late.mean()),
            "late_rate_without": _r(without.late.mean()),
            "odds_ratio": _r(np.exp(m.params[k]), 2),
            "p_value": _r(m.pvalues[k], 6),
            "otd_if_fixed": _r(1 - _predict_mean(d, zero=(k,))),
            "penalties_usd": int(pen.loc[pen[k] == 1, "penalty_usd"].sum()),
        })
    routing = {
        "key": "routing", "label": "Route length and stops (what the vendor optimises)",
        "step": "9. Delivery", "fix": "Routing optimisation engine",
        "odds_ratio_per_stop": _r(np.exp(m.params["stops_on_route"]), 3),
        "p_value_stops": _r(m.pvalues["stops_on_route"], 4),
        "odds_ratio_per_km": _r(np.exp(m.params["planned_route_km"]), 4),
        "p_value_km": _r(m.pvalues["planned_route_km"], 4),
        "otd_if_routes_20pct_shorter": _r(1 - _predict_mean(d, route_cut=0.2)),
        "late_rate_by_stops": [
            {"band": str(b), "late_rate": _r(v)}
            for b, v in d.groupby(pd.cut(d.stops_on_route, [0, 10, 20, 30, 60]), observed=True).late.mean().items()
        ],
    }
    return {"otd_today_model": _r(base), "drivers": rows, "routing": routing,
            "pseudo_r2": _r(m.prsquared), "n": int(m.nobs)}


def simulate(fixes: dict[str, float], route_cut: float = 0.0) -> dict:
    """Expected OTD if each driver is removed for a share `fixes[k]` of affected orders.

    Exact expectation: the fix pattern M (which drivers are removed) has probability
    prod f_i (i in M) * prod (1 - f_i) (i not in M), independent of the row, so
    E[OTD] = 1 - sum_M P(M) * mean(predict with M zeroed).
    """
    d = load_dataset().deliveries
    f = {k: min(max(float(fixes.get(k, 0.0)), 0.0), 1.0) for k in DRIVER_KEYS}
    expected_late = 0.0
    for mask in product([0, 1], repeat=len(DRIVER_KEYS)):
        p = 1.0
        for k, on in zip(DRIVER_KEYS, mask, strict=True):
            p *= f[k] if on else 1 - f[k]
        if p == 0:
            continue
        zero = tuple(k for k, on in zip(DRIVER_KEYS, mask, strict=True) if on)
        expected_late += p * _predict_mean(d, zero=zero, route_cut=route_cut)
    otd = 1 - expected_late
    today = 1 - _predict_mean(d)
    months = d.month.nunique()
    return {
        "fixes": f, "route_cut": route_cut,
        "otd_today": _r(today),
        "otd_expected": _r(otd),
        "meets_target": bool(otd >= settings.otd_target),
        "late_deliveries_avoided_per_month": int(round((otd - today) * len(d) / months)),
    }


def bridge() -> dict:
    d = load_dataset().deliveries
    today = 1 - _predict_mean(d)
    steps = [{"step": "routing", "label": "Routing 20% shorter",
              "delta": _r((1 - _predict_mean(d, route_cut=0.2)) - today)}]
    for drv in sorted(DRIVERS, key=lambda x: _predict_mean(d, zero=(x["key"],))):
        steps.append({"step": drv["key"], "label": drv["fix"],
                      "delta": _r((1 - _predict_mean(d, zero=(drv["key"],))) - today)})
    all_fixed = 1 - _predict_mean(d, zero=tuple(DRIVER_KEYS))
    overlap = all_fixed - today - sum(s["delta"] for s in steps)
    steps.append({"step": "overlap", "label": "Overlap between fixes", "delta": _r(overlap)})
    return {"today": _r(today), "all_fixed": _r(all_fixed), "steps": steps,
            "target": settings.otd_target, "benchmark": settings.otd_benchmark}


# ---------- carrier lanes ----------

def lanes(ds: Dataset | None = None) -> dict:
    ds = ds or load_dataset()
    d = ds.deliveries
    clean = d[d.clean_upstream == 1]
    g_all = d.groupby(["carrier_id", "destination_region"]).agg(
        deliveries=("late", "size"), late_rate=("late", "mean"), avg_rate_usd=("carrier_rate_usd", "mean"))
    g_clean = clean.groupby(["carrier_id", "destination_region"]).agg(late_rate_clean=("late", "mean"))
    bad = d.groupby(["carrier_id", "destination_region"]).drv_lane.max().rename("bad")
    t = g_all.join(g_clean).join(bad).reset_index()
    carriers = ds.carriers.set_index("carrier_id")

    recs = []
    for region, grp in t.groupby("destination_region"):
        good = grp[grp.bad == 0]
        best_rate = good.late_rate_clean.min()
        # cheapest carrier within 2 points of the best clean late rate
        cand = good[good.late_rate_clean <= best_rate + 0.02].sort_values("avg_rate_usd")
        pick = cand.iloc[0]
        bad_rows = grp[grp.bad == 1]
        vol = int(bad_rows.deliveries.sum())
        cost_delta = float(((pick.avg_rate_usd - bad_rows.avg_rate_usd) * bad_rows.deliveries).sum())
        recs.append({
            "region": region,
            "avoid": sorted(bad_rows.carrier_id.tolist()),
            "use": pick.carrier_id,
            "use_late_rate_clean": _r(pick.late_rate_clean),
            "deliveries_to_move_6m": vol,
            "extra_cost_usd_per_month": int(round(cost_delta / 6)),
        })
    return {
        "lanes": [
            {
                "carrier": r.carrier_id, "region": r.destination_region, "deliveries": int(r.deliveries),
                "late_rate": _r(r.late_rate), "late_rate_clean": _r(r.late_rate_clean),
                "avg_rate_usd": _r(r.avg_rate_usd, 2), "bad": bool(r.bad),
            }
            for r in t.itertuples()
        ],
        "carriers": [
            {
                "carrier": cid, "has_api": c.has_api == "Y", "event_feed": c.delivery_event_feed,
                "preferred_supplier": c.preferred_supplier_agreement == "Y", "base_rate_usd": c.base_rate_usd,
                "late_rate": _r(d.loc[d.carrier_id == cid, "late"].mean()),
                "late_rate_clean": _r(clean.loc[clean.carrier_id == cid, "late"].mean()),
            }
            for cid, c in carriers.iterrows()
        ],
        "recommendations": recs,
        "rule": {
            "multiple_of_network": settings.bad_lane_multiple, "floor": settings.bad_lane_floor,
            "network_clean_late_rate": _r(clean.late.mean()),
        },
    }


# ---------- stock placement ----------

def stock(ds: Dataset | None = None, top: int = 15) -> dict:
    ds = ds or load_dataset()
    d = ds.deliveries
    miss = d[d.drv_stock == 1]
    by_client = (
        d.groupby("client_id").agg(deliveries=("late", "size"), misses=("drv_stock", "sum"))
        .assign(miss_rate=lambda x: x.misses / x.deliveries).sort_values("misses", ascending=False)
    )
    top_region = miss.groupby("client_id").nearest_warehouse_id.agg(lambda s: s.value_counts().index[0])
    out = by_client.head(top).join(top_region.rename("add_stock_at")).reset_index()
    matrix = pd.crosstab(miss.nearest_warehouse_id, miss.fulfilment_warehouse_id)
    return {
        "miss_rate": _r(d.drv_stock.mean()),
        "clients_affected": int(miss.client_id.nunique()),
        "median_miles_when_stocked": float(d.loc[d.drv_stock == 0, "distance_miles"].median()),
        "median_miles_when_missed": float(miss.distance_miles.median()),
        "late_rate_when_missed": _r(miss.late.mean()),
        "matrix": {
            "nearest": matrix.index.tolist(), "fulfilled_from": matrix.columns.tolist(),
            "counts": matrix.values.tolist(),
        },
        "top_clients": [
            {"client_id": r.client_id, "deliveries": int(r.deliveries), "misses": int(r.misses),
             "miss_rate": _r(r.miss_rate), "add_stock_at": r.add_stock_at}
            for r in out.itertuples()
        ],
    }


# ---------- accounts ----------

def accounts(ds: Dataset | None = None) -> dict:
    ds = ds or load_dataset()
    d = ds.deliveries
    per = d.groupby("client_id").agg(
        deliveries=("late", "size"), otd=("late", lambda s: 1 - s.mean()),
        **{k: (k, "mean") for k in DRIVER_KEYS},
    )
    pen = ds.penalties.groupby("client_id").penalty_usd.sum().rename("penalties_usd")
    comp = ds.complaints.groupby("client_id").size().rename("complaints")
    t = ds.clients.set_index("client_id").join(per).join(pen).join(comp).fillna(
        {"penalties_usd": 0, "complaints": 0})
    t["main_driver"] = t[DRIVER_KEYS].idxmax(axis=1).map({x["key"]: x["label"] for x in DRIVERS})
    threat = t[t.account_status == "threatening_to_leave"].sort_values("annual_revenue_usd", ascending=False)

    def row(cid, r) -> dict:
        return {
            "client_id": cid, "name": r.client_name, "tier": r.account_tier, "channel": r.primary_order_channel,
            "revenue_usd": int(r.annual_revenue_usd), "status": r.account_status,
            "deliveries": int(r.deliveries), "otd": _r(r.otd), "penalties_usd": int(r.penalties_usd),
            "complaints": int(r.complaints), "main_driver": r.main_driver,
            "driver_rates": {k: _r(getattr(r, k)) for k in DRIVER_KEYS},
        }

    return {
        "threatening": [row(cid, r) for cid, r in threat.iterrows()],
        "lowest_otd_tier1": [row(cid, r) for cid, r in
                             t[(t.account_tier == "Tier 1") & (t.account_status == "active")]
                             .sort_values("otd").head(10).iterrows()],
    }


# ---------- deliveries ----------

def deliveries(client_id: str | None = None, late_only: bool = False, driver: str | None = None,
               limit: int = 100, offset: int = 0) -> dict:
    d = load_dataset().deliveries
    if client_id:
        d = d[d.client_id == client_id]
    if late_only:
        d = d[d.late == 1]
    if driver in DRIVER_KEYS:
        d = d[d[driver] == 1]
    total = len(d)
    page = d.iloc[offset: offset + limit]
    risk = fit_model().predict(page) if len(page) else []
    labels = {x["key"]: x["label"] for x in DRIVERS}
    items = []
    for (_, r), p in zip(page.iterrows(), risk, strict=True):
        items.append({
            "delivery_id": r.delivery_id, "client_id": r.client_id, "channel": r.order_channel,
            "received": str(r.order_received_ts), "commitment": r.commitment_type,
            "promised": r.promised_date, "delivered": r.delivered_date, "days_late": int(r.days_late),
            "carrier": r.carrier_id, "region": r.destination_region,
            "warehouse": r.fulfilment_warehouse_id, "risk": _r(p, 3),
            "causes": [labels[k] for k in DRIVER_KEYS if r[k] == 1],
        })
    return {"total": int(total), "items": items}
