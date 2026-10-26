"""Load, validate and enrich the Halcyon hackathon dataset.

Every derived column here is a deterministic rule on the raw records. No LLM is involved.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pandas as pd

from .config import settings

EXPECTED_COLUMNS: dict[str, list[str]] = {
    "deliveries": [
        "delivery_id", "order_id", "client_id", "order_channel", "email_format_id",
        "order_received_ts", "commitment_type", "promised_date", "address_status",
        "pick_list_date", "shipped_ts", "destination_region", "nearest_warehouse_id",
        "fulfilment_warehouse_id", "nearest_warehouse_had_stock", "distance_miles",
        "carrier_id", "carrier_rate_usd", "stops_on_route", "planned_route_km",
        "actual_route_km", "delivered_date", "days_late", "on_time", "redelivery_cost_usd",
    ],
    "clients": [
        "client_id", "client_name", "region", "primary_order_channel", "account_tier",
        "sla_penalty_clause", "annual_revenue_usd", "account_status",
    ],
    "carriers": [
        "carrier_id", "carrier_name", "has_api", "delivery_event_feed",
        "preferred_supplier_agreement", "base_rate_usd",
    ],
    "warehouses": [
        "warehouse_id", "warehouse_name", "region", "pick_list_cutoff", "carrier_collection_window",
    ],
    "sla_penalties": ["penalty_id", "delivery_id", "client_id", "days_late", "penalty_usd"],
    "complaints": [
        "complaint_id", "complaint_date", "client_id", "delivery_id", "channel", "category",
        "escalated_to_account_owner", "text",
    ],
    "customer_success_time_study": [
        "staff_id", "week_of", "hours_total", "hours_on_delivery_complaints",
        "hours_keying_email_orders", "hours_on_proactive_client_work",
    ],
}


@dataclass
class Dataset:
    deliveries: pd.DataFrame
    clients: pd.DataFrame
    carriers: pd.DataFrame
    warehouses: pd.DataFrame
    distances: pd.DataFrame
    penalties: pd.DataFrame
    complaints: pd.DataFrame
    cs_time: pd.DataFrame
    quality: list[dict] = field(default_factory=list)


def _read(data_dir: Path, name: str) -> pd.DataFrame:
    df = pd.read_csv(data_dir / f"{name}.csv")
    missing = set(EXPECTED_COLUMNS.get(name, [])) - set(df.columns)
    if missing:
        raise ValueError(f"{name}.csv is missing columns: {sorted(missing)}")
    return df


def _quality_checks(d: pd.DataFrame) -> list[dict]:
    """Data-quality report shown in the UI so judges see we checked before trusting."""
    checks: list[dict] = []

    def add(name: str, value: int, expected: str, ok: bool) -> None:
        checks.append({"check": name, "value": int(value), "expected": expected, "ok": bool(ok)})

    add("Delivery rows", len(d), "48,340 per problem statement", len(d) == 48340)
    add("Duplicate delivery_id", d.delivery_id.duplicated().sum(), "0", d.delivery_id.duplicated().sum() == 0)
    add("Blank actual_route_km", d.actual_route_km.isna().sum(), "some blanks allowed", True)
    email_blank = d.loc[d.order_channel == "Email", "email_format_id"].isna().sum()
    add("Email orders with no email_format_id", email_blank, "0", email_blank == 0)
    recomputed = (pd.to_datetime(d.delivered_date) - pd.to_datetime(d.promised_date)).dt.days
    mismatch = (recomputed != d.days_late).sum()
    add("days_late disagrees with dates", mismatch, "0", mismatch == 0)
    flag_mismatch = ((d.days_late <= 0) != (d.on_time == "Y")).sum()
    add("on_time flag disagrees with days_late", flag_mismatch, "0", flag_mismatch == 0)
    shipped_before_order = (d.shipped_ts < d.order_received_ts).sum()
    add("Shipped before order received", shipped_before_order, "0", shipped_before_order == 0)
    return checks


def enrich(d: pd.DataFrame, warehouses: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    d["order_received_ts"] = pd.to_datetime(d.order_received_ts)
    d["shipped_ts"] = pd.to_datetime(d.shipped_ts)
    d["month"] = d.order_received_ts.dt.to_period("M").astype(str)
    d["late"] = (d.on_time == "N").astype(int)

    cutoff = warehouses.set_index("warehouse_id").pick_list_cutoff
    cutoff_hour = d.fulfilment_warehouse_id.map(cutoff).str.slice(0, 2).astype(int)
    d["after_cutoff"] = (d.order_received_ts.dt.hour >= cutoff_hour).astype(int)

    # The four upstream drivers (see playbook: Deconstruct)
    d["drv_address"] = (d.address_status == "incomplete").astype(int)
    d["drv_stock"] = (d.nearest_warehouse_had_stock == "N").astype(int)
    d["drv_cutoff"] = ((d.commitment_type == "same_day") & (d.after_cutoff == 1)).astype(int)
    d["clean_upstream"] = ((d.drv_address + d.drv_stock + d.drv_cutoff) == 0).astype(int)
    d["lane"] = d.carrier_id + "|" + d.destination_region
    d["route_ratio"] = d.actual_route_km / d.planned_route_km
    return d


def flag_bad_lanes(d: pd.DataFrame) -> pd.DataFrame:
    """A lane (carrier x destination region) is 'bad' when, on orders with no upstream problem,
    its late rate is at least double the network rate and above the configured floor.
    Data-driven, so the rule re-learns when new months arrive."""
    clean = d[d.clean_upstream == 1]
    network = clean.late.mean()
    lanes = clean.groupby("lane").agg(late_rate=("late", "mean"), n=("late", "size")).reset_index()
    lanes["bad"] = (
        (lanes.late_rate >= settings.bad_lane_multiple * network)
        & (lanes.late_rate >= settings.bad_lane_floor)
        & (lanes.n >= settings.bad_lane_min_n)
    )
    bad = set(lanes.loc[lanes.bad, "lane"])
    d = d.copy()
    d["drv_lane"] = d.lane.isin(bad).astype(int)
    d["any_driver"] = ((d.drv_address + d.drv_stock + d.drv_cutoff + d.drv_lane) > 0).astype(int)
    return d


@lru_cache(maxsize=1)
def load_dataset(data_dir: str | None = None) -> Dataset:
    path = Path(data_dir or settings.data_dir)
    raw = _read(path, "deliveries")
    warehouses = _read(path, "warehouses")
    d = enrich(raw, warehouses)
    quality = _quality_checks(d)
    d = flag_bad_lanes(d)
    return Dataset(
        deliveries=d,
        clients=_read(path, "clients"),
        carriers=_read(path, "carriers"),
        warehouses=warehouses,
        distances=pd.read_csv(path / "warehouse_region_distance_miles.csv"),
        penalties=_read(path, "sla_penalties"),
        complaints=_read(path, "complaints"),
        cs_time=_read(path, "customer_success_time_study"),
        quality=quality,
    )
