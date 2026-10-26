from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_endpoints_respond():
    for path in ["/api/health", "/api/overview", "/api/drivers", "/api/bridge", "/api/lanes",
                 "/api/stock", "/api/accounts", "/api/deliveries?late_only=true&limit=5"]:
        r = client.get(path)
        assert r.status_code == 200, path


def test_simulate_validates_input():
    assert client.post("/api/simulate", json={"fixes": {"bogus": 1}}).status_code == 400
    r = client.post("/api/simulate", json={"fixes": {"drv_lane": 1.0}})
    assert r.status_code == 200 and r.json()["otd_expected"] > r.json()["otd_today"]


def test_deliveries_filter():
    r = client.get("/api/deliveries", params={"driver": "drv_address", "limit": 3}).json()
    assert r["total"] > 0 and all("Incomplete address at intake" in i["causes"] for i in r["items"])
