from app import analytics as a


def test_overview_matches_problem_statement():
    o = a.overview()
    assert o["deliveries"] == 48340
    assert abs(o["otd"] - 0.71) < 0.005  # 71% per sponsor
    assert o["revenue_at_risk_usd"] == 14_200_000
    assert o["threatening_accounts"] == 9
    assert o["complaints"]["total"] == 34
    assert all(c["ok"] for c in o["data_quality"])


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
