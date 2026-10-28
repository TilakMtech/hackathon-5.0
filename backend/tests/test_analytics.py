from app import analytics as a


def test_overview_matches_problem_statement():
    o = a.overview()
    assert o["deliveries"] == 48340
    assert abs(o["otd"] - 0.71) < 0.005  # 71% per sponsor
    assert o["revenue_at_risk_usd"] == 14_200_000
    assert o["threatening_accounts"] == 9
    assert o["complaints"]["total"] == 34
    # every check passes except one known supplied-data issue, which is flagged and kept, not repaired
    bad = [c for c in o["data_quality"] if not c["ok"]]
    assert [c["check"] for c in bad] == ["Delivered before carrier collection (kept, flagged)"] and bad[0]["value"] == 450


def test_each_driver_raises_lateness_and_routing_does_not():
    dr = a.drivers()
    for row in dr["drivers"]:
        assert row["late_rate_with"] > row["late_rate_without"]
        assert row["odds_ratio"] > 2 and row["p_value"] < 0.001
        assert row["otd_if_fixed"] > dr["otd_today_model"]
    r = dr["routing"]
    assert r["p_value_stops"] > 0.05 and r["p_value_km"] > 0.05
    assert abs(r["otd_if_routes_20pct_shorter"] - dr["otd_today_model"]) < 0.002


def test_simulate_bounds():
    none = a.simulate({})
    full = a.simulate({k: 1.0 for k in a.DRIVER_KEYS})
    b = a.bridge()
    assert abs(none["otd_expected"] - none["otd_today"]) < 1e-6
    assert abs(full["otd_expected"] - b["all_fixed"]) < 1e-4
    half = a.simulate({k: 0.5 for k in a.DRIVER_KEYS})
    assert none["otd_expected"] < half["otd_expected"] < full["otd_expected"]


def test_bridge_adds_up():
    b = a.bridge()
    total = b["today"] + sum(s["delta"] for s in b["steps"])
    assert abs(total - b["all_fixed"]) < 1e-3


def test_bad_lanes_are_data_driven():
    lanes = {(x["carrier"], x["region"]) for x in a.lanes()["lanes"] if x["bad"]}
    assert lanes == {("D", "Midwest"), ("D", "Southeast"), ("E", "West"), ("F", "Northeast"), ("F", "Southeast")}
    for rec in a.lanes()["recommendations"]:
        assert rec["use"] not in rec["avoid"]


def test_fixes_only_reach_eligible_orders():
    e = a.eligibility()
    assert e["drv_address"] == {"affected": 5994, "reachable_by_email_agent": 4250, "needs_feed_validation": 1744}
    assert e["drv_cutoff"]["received_at_or_after_12"] == 1565
    email_only = a.simulate({"drv_address": 1})["otd_expected"]
    every_address = a.simulate({"drv_address": 1, "drv_address_feed": 1})["otd_expected"]
    flag_gone = 1 - a._predict_mean(a.load_dataset().deliveries, zero=("drv_address",))
    assert email_only < every_address and abs(every_address - flag_gone) < 1e-3  # the audit's 75.2% vs 77.1%


def test_out_of_time_holdout_and_sensitivity():
    h = a.holdout()["summary"]
    assert h["auc"] > 0.75 and abs(h["otd_predicted"] - h["otd_observed"]) < 0.01 and h["same_lanes_as_full_data"]
    s = a.sensitivity()
    assert s["without_chronology_exceptions"]["rows"] == 48340 - 450
    for run in ("all_rows", "without_chronology_exceptions"):  # the routing finding survives the exclusion
        lo, hi = s[run]["odds_ratios"]["stops_on_route"]["ci95"]
        assert lo <= 1.0 + 1e-9 or hi < 1.02
