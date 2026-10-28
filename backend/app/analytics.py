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
        "step": "3. Order intake (email keying; EDI/API feeds)", "fix": "Email intake agent + feed address validation",
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


HOLDOUT_SPLIT = "2026-08-01"  # train on orders received before this date, test on the rest


def _auc(y, p) -> float:
    """Rank-based ROC AUC (Mann-Whitney), no extra dependency."""
    r = pd.Series(p).rank().to_numpy()
    y = pd.Series(y).to_numpy()
    n1, n0 = int(y.sum()), int(len(y) - y.sum())
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


@lru_cache(maxsize=1)
def holdout() -> dict:
    """Out-of-time check: learn the lane rule AND fit the model on Apr-Jul only, then score Aug-Sep.
    This tests prediction on unseen months. It does not prove what an intervention would do: that needs a pilot."""
    from .data import apply_lanes, bad_lane_set
    d = load_dataset().deliveries
    is_train = d.order_received_ts < pd.Timestamp(HOLDOUT_SPLIT)
    bad = bad_lane_set(d[is_train])
    dd = apply_lanes(d, bad)
    train, test = dd[is_train], dd[~is_train].copy()
    m = smf.logit(FORMULA, train).fit(disp=0)
    test["risk"] = m.predict(test)
    test["decile"] = pd.qcut(test.risk.rank(method="first"), 10, labels=False)
    cal = test.groupby("decile").agg(predicted=("risk", "mean"), observed=("late", "mean"), n=("late", "size"))
    full_bad = bad_lane_set(d)
    clean = train[(train.clean_upstream == 1) & (train.drv_lane == 0)]
    best = clean.groupby(["destination_region", "carrier_id"]).late.mean().reset_index().sort_values("late")
    best_by_region = best.groupby("destination_region").carrier_id.first().to_dict()
    return {
        "model": m, "test": test, "bad_lanes_train": sorted(bad), "best_carrier_by_region": best_by_region,
        "summary": {
            "split": HOLDOUT_SPLIT, "train_rows": int(len(train)), "test_rows": int(len(test)),
            "auc": _r(_auc(test.late, test.risk)),
            "otd_predicted": _r(1 - test.risk.mean()), "otd_observed": _r(1 - test.late.mean()),
            "bad_lanes_train": sorted(bad), "same_lanes_as_full_data": bad == full_bad,
            "calibration": [{"decile": int(i) + 1, "predicted_late": _r(r.predicted), "observed_late": _r(r.observed),
                             "n": int(r.n)} for i, r in cal.iterrows()],
        },
    }


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
            "odds_ratio_ci95": [_r(np.exp(m.conf_int().loc[k, 0]), 2), _r(np.exp(m.conf_int().loc[k, 1]), 2)],
            "p_value": _r(m.pvalues[k], 6),
            "otd_if_fixed": _r(1 - _predict_mean(d, zero=(k,))),
            "penalties_usd": int(pen.loc[pen[k] == 1, "penalty_usd"].sum()),
        })
    routing = {
        "key": "routing", "label": "Route length and stops (what the vendor optimises)",
        "step": "9. Delivery", "fix": "Routing optimisation engine",
        "odds_ratio_per_stop": _r(np.exp(m.params["stops_on_route"]), 3),
        "odds_ratio_per_stop_ci95": [_r(np.exp(m.conf_int().loc["stops_on_route", 0]), 3),
                                     _r(np.exp(m.conf_int().loc["stops_on_route", 1]), 3)],
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


@lru_cache(maxsize=1)
def sensitivity() -> dict:
    """Are the findings robust to the 450 deliveries dated before carrier collection? Refit without them."""
    d = load_dataset().deliveries
    out = {}
    for name, x in [("all_rows", d), ("without_chronology_exceptions", d[d.dq_chronology == 0])]:
        m = smf.logit(FORMULA, x).fit(disp=0)
        ci = m.conf_int()
        out[name] = {
            "rows": int(len(x)), "otd": _r(1 - x.late.mean()),
            "odds_ratios": {k: {"or": _r(np.exp(m.params[k]), 2), "ci95": [_r(np.exp(ci.loc[k, 0]), 2), _r(np.exp(ci.loc[k, 1]), 2)]}
                            for k in [*DRIVER_KEYS, "stops_on_route", "planned_route_km"]},
            "otd_all_flags_removed": _r(1 - m.predict(x.assign(**dict.fromkeys(DRIVER_KEYS, 0))).mean()),
        }
    return {**out, "note": "The 450 exception rows are all marked on time. Excluding them lowers OTD and raises the "
                           "cutoff odds ratio; the routing finding (odds ratios near 1) does not change."}


def model_checks() -> dict:
    """What the model can and cannot claim: out-of-time prediction, calibration, sensitivity."""
    return {"holdout": holdout()["summary"], "sensitivity": sensitivity(),
            "limits": [
                "Odds ratios are associations from observational data, not proven causal effects.",
                "The 'if fixed' figures set flags to zero in the model: scenario estimates, not demonstrated performance.",
                "Routing coefficients near 1 mean no measurable association with stops or route km in this data; they do "
                "not prove a routing engine (which may also change carrier choice) would add nothing.",
                "Intervention effects need a controlled pilot: see the decision memo.",
            ]}


# ---------- scenarios: each fix only reaches the orders it can reach ----------
SECOND_WAVE_HOUR = 12  # a second pick wave at noon cannot catch same-day orders received after it
LEVERS = {
    "drv_address": "Email intake agent: incomplete addresses on EMAIL orders",
    "drv_address_feed": "Address validation on EDI/API feeds (not built in this slice)",
    "drv_lane": "Lane rules: bad-lane volume moved to a reliable carrier",
    "drv_cutoff": f"Second pick wave at {SECOND_WAVE_HOUR}:00: same-day orders received before it",
    "drv_stock": "Re-slotting: stock misses at the nearest warehouse",
}


def _eligibility(d: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"email": d.order_channel == "Email",
                         "before_wave": d.order_received_ts.dt.hour < SECOND_WAVE_HOUR}, index=d.index)


@lru_cache(maxsize=1024)
def _group_mean(email: bool, before_wave: bool, zero: tuple[str, ...], route_cut: float) -> tuple[float, int]:
    d = load_dataset().deliveries
    el = _eligibility(d)
    x = d[(el.email == email) & (el.before_wave == before_wave)].copy()
    for k in zero:
        x[k] = 0
    if route_cut:
        x["stops_on_route"] = x.stops_on_route * (1 - route_cut)
        x["planned_route_km"] = x.planned_route_km * (1 - route_cut)
    return (float(fit_model().predict(x).mean()) if len(x) else 0.0), int(len(x))


def simulate(fixes: dict[str, float], route_cut: float = 0.0) -> dict:
    """Expected OTD (model scenario, not a forecast) when each lever fixes a share of the orders it can REACH.

    Rows are split by what each fix can reach (email or not; received before or after the second wave). Within a
    group every driver has one removal probability, so the exact expectation is the sum over the 16 removal
    patterns M of P(M) x mean(prediction with M zeroed), weighted by group size.
    """
    d = load_dataset().deliveries
    f = {k: min(max(float(fixes.get(k, 0.0)), 0.0), 1.0) for k in LEVERS}
    route_cut = round(float(route_cut), 4)
    expected_late, total = 0.0, len(d)
    for email in (True, False):
        for before in (True, False):
            g = {"drv_address": f["drv_address"] if email else f["drv_address_feed"],
                 "drv_lane": f["drv_lane"], "drv_cutoff": f["drv_cutoff"] if before else 0.0, "drv_stock": f["drv_stock"]}
            for mask in product([0, 1], repeat=len(DRIVER_KEYS)):
                pm = 1.0
                for k, on in zip(DRIVER_KEYS, mask, strict=True):
                    pm *= g[k] if on else 1 - g[k]
                if pm == 0:
                    continue
                zero = tuple(sorted(k for k, on in zip(DRIVER_KEYS, mask, strict=True) if on))
                mean, n = _group_mean(email, before, zero, route_cut)
                expected_late += pm * mean * n / total
    otd = 1 - expected_late
    today = 1 - _predict_mean(d)
    months = d.month.nunique()
    return {
        "fixes": f, "route_cut": route_cut,
        "otd_today": _r(today),
        "otd_expected": _r(otd),
        "meets_target": bool(otd >= settings.otd_target),
        "late_deliveries_avoided_per_month": int(round((otd - today) * len(d) / months)),
        "kind": "model scenario: assumes each fix removes the flag's association for the orders it reaches; "
                "not a forecast until a pilot measures the real effect",
    }


def eligibility() -> dict:
    """How much of each driver the proposed fixes can reach at all."""
    d = load_dataset().deliveries
    el = _eligibility(d)
    addr, cut = d.drv_address == 1, d.drv_cutoff == 1
    return {
        "drv_address": {"affected": int(addr.sum()), "reachable_by_email_agent": int((addr & el.email).sum()),
                        "needs_feed_validation": int((addr & ~el.email).sum())},
        "drv_cutoff": {"affected": int(cut.sum()), f"received_before_{SECOND_WAVE_HOUR}": int((cut & el.before_wave).sum()),
                       f"received_at_or_after_{SECOND_WAVE_HOUR}": int((cut & ~el.before_wave).sum())},
        "levers": LEVERS,
    }


def bridge() -> dict:
    """Each proposed fix alone, at full coverage of the orders it can reach."""
    d = load_dataset().deliveries
    today = 1 - _predict_mean(d)
    full = {"drv_address": 1, "drv_address_feed": 0, "drv_lane": 1, "drv_cutoff": 1, "drv_stock": 1}

    def only(k: str) -> float:
        return simulate({j: (full[j] if j == k else 0) for j in LEVERS})["otd_expected"]

    steps = [{"step": "routing", "label": "Routing 20% shorter",
              "delta": _r(simulate({}, route_cut=0.2)["otd_expected"] - today)}]
    for k in sorted([k for k in LEVERS if full[k]], key=lambda k: -only(k)):
        steps.append({"step": k, "label": next(x["fix"] for x in DRIVERS if x["key"] == k), "delta": _r(only(k) - today)})
    all_fixed = simulate(full)["otd_expected"]
    overlap = all_fixed - today - sum(s["delta"] for s in steps)
    steps.append({"step": "overlap", "label": "Overlap between fixes", "delta": _r(overlap)})
    return {"today": _r(today), "all_fixed": _r(all_fixed), "steps": steps,
            "scope": "each fix at 100% of the orders it can reach (email-only address fix; second wave at "
                     f"{SECOND_WAVE_HOUR}:00); model scenario, not a forecast",
            "all_flags_removed": _r(1 - _predict_mean(d, zero=tuple(DRIVER_KEYS))),
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
