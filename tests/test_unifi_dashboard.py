import json
from urllib.parse import urlparse

import pytest

import unifi_dashboard as ud

SITE_ID = "88f7af54-98f8-306a-a1c7-c9349722b1f6"
GW, AP1, AP2, SW = "gw-1", "ap-1", "ap-2", "sw-1"


class FakeResponse:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ud.requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self.data


class FakeSession:
    """Answers GETs from a {path: payload} map and records the requests."""

    def __init__(self, routes):
        self.routes, self.headers, self.verify, self.calls = routes, {}, True, []

    def get(self, url, params=None, timeout=None):  # pylint: disable=unused-argument
        path = urlparse(url).path
        self.calls.append((path, params))
        if path not in self.routes:
            return FakeResponse({}, 404)
        payload = self.routes[path]
        return FakeResponse(payload(params) if callable(payload) else payload)


def page(items):
    return {"offset": 0, "limit": 200, "count": len(items), "totalCount": len(items), "data": items}


def device(dev_id, name, features, state="ONLINE", updatable=False):
    return {"id": dev_id, "name": name, "model": "X", "state": state, "features": features,
            "firmwareUpdatable": updatable, "interfaces": [], "macAddress": "", "ipAddress": "",
            "supported": True, "firmwareVersion": "1"}


def client(kind, uplink=None, access="DEFAULT"):
    data = {"id": "c", "name": "c", "type": kind, "access": {"type": access}}
    if uplink:
        data["uplinkDeviceId"] = uplink
    return data


LOCAL_BASE = "/proxy/network/integration/v1"
LEGACY_DEVICES = "/proxy/network/api/s/default/stat/device"


def poe_port(idx, watts, poe=True):
    # The classic API reports watts as strings, and "0.00" even on ports without PoE.
    return {"port_idx": idx, "port_poe": poe, "poe_enable": poe, "poe_power": f"{watts:.2f}"}


LEGACY = {"meta": {"rc": "ok"}, "data": [
    {"name": "Dream Machine Pro SE", "model": "UDMPROSE",  # PoE ports but no reported budget
     "port_table": [poe_port(1, 6.5), poe_port(2, 5.2), poe_port(9, 0, poe=False)]},
    {"name": "USW Pro Max 24 PoE", "model": "USPM24P", "total_max_power": 400, "total_used_power": 87.4,
     "port_table": [poe_port(1, 40), poe_port(2, 47.4)]},
    {"name": "Flex Mini", "model": "USWFLEXMINI", "port_table": [poe_port(1, 0, poe=False)]},
]}


def local_routes(devices=None, clients=None):
    devices = devices if devices is not None else [
        device(GW, "Dream Machine Pro SE", ["switching", "gateway"]),
        device(AP1, "Living Room", ["accessPoint"]),
        device(AP2, "Garage AP", ["accessPoint"], state="OFFLINE"),
        device(SW, "Office Switch", ["switching"], updatable=True),
    ]
    clients = clients if clients is not None else (
        [client("WIRELESS", AP1)] * 3 + [client("WIRED", SW)] * 2
        + [client("WIRELESS", AP1, access="GUEST"), client("VPN")])
    return {
        f"{LOCAL_BASE}/sites": page([{"id": SITE_ID, "internalReference": "default", "name": "Default"}]),
        f"{LOCAL_BASE}/sites/{SITE_ID}/devices": page(devices),
        f"{LOCAL_BASE}/sites/{SITE_ID}/clients": page(clients),
        LEGACY_DEVICES: LEGACY,
        f"{LOCAL_BASE}/sites/{SITE_ID}/devices/{GW}/statistics/latest": {
            "uptimeSec": 3 * 86400 + 5 * 3600, "cpuUtilizationPct": 12.4, "memoryUtilizationPct": 61.6,
            "uplink": {"rxRateBps": 245_600_000, "txRateBps": 12_300_000}},
        f"{LOCAL_BASE}/sites/{SITE_ID}/devices/{AP1}/statistics/latest": {
            "uptimeSec": 100, "interfaces": {"radios": [{"frequencyGHz": 2.4, "txRetriesPct": 7.6},
                                                        {"frequencyGHz": 5, "txRetriesPct": 2.1}]}},
    }


def isp_periods(latencies, loss=None):
    loss = loss or [0] * len(latencies)
    return [{"metricTime": f"2026-09-30T{i // 12:02d}:{(i % 12) * 5:02d}:00Z", "version": "1",
             "data": {"wan": {"avgLatency": lat, "maxLatency": lat + 5, "packetLoss": pl, "ispName": "Ziply",
                              "download_kbps": 0, "upload_kbps": 0, "downtime": 0, "uptime": 100}}}
            for i, (lat, pl) in enumerate(zip(latencies, loss))]


def cloud_routes(latencies=(10,) * 288, loss=None, issues=()):
    return {
        "/v1/sites": {"data": [{
            "siteId": "cloud-site", "hostId": "host",
            "meta": {"name": "default", "desc": "Default", "timezone": "America/Los_Angeles"},
            "statistics": {"counts": {"criticalNotification": 0, "offlineGatewayDevice": 0},
                           "internetIssues": list(issues), "ispInfo": {"name": "Ziply Fiber"},
                           "percentages": {"wanUptime": 99.9}}}]},
        "/v1/isp-metrics/5m": {"data": [
            {"metricType": "5m", "siteId": "other", "hostId": "h", "periods": isp_periods([999])},
            {"metricType": "5m", "siteId": "cloud-site", "hostId": "host",
             "periods": isp_periods(list(latencies), loss)}]},
    }


@pytest.fixture
def config(monkeypatch):
    for key in list(ud.os.environ):
        if key.startswith(("UNIFI_", "TRMNL_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("UNIFI_HOST", "192.168.1.1")
    monkeypatch.setenv("UNIFI_API_KEY", "local-key")
    return ud.Config()


def build(config, local=None, cloud=None, cloud_key="cloud-key"):
    config.cloud_key = cloud_key
    local_api = ud.LocalApi(config, FakeSession(local or local_routes()))
    cloud_api = ud.CloudApi(config, FakeSession(cloud if cloud is not None else cloud_routes())) if cloud_key else None
    return ud.build_dashboard(config, local_api, cloud_api)


def test_config_defaults_to_https_without_verification(config):
    assert config.host == "https://192.168.1.1"
    assert config.verify is False


def test_local_api_sends_key_header(config):
    session = FakeSession(local_routes())
    ud.LocalApi(config, session)
    assert session.headers["X-API-Key"] == "local-key"
    assert session.verify is False


def test_dashboard_end_to_end(config):
    dash = build(config)
    assert dash["status"] == "ok"
    assert dash["site"] == "Default"
    assert dash["internet"] == "up"
    assert dash["isp"] == "Ziply Fiber"
    assert dash["wan_uptime"] == 99.9
    assert dash["latency"] == 10
    assert dash["down"] == 246 and dash["up"] == 12.3
    assert dash["cpu"] == 12 and dash["mem"] == 62
    assert dash["uptime"] == "3d 5h"
    assert dash["clients"] == {"wifi": 4, "wired": 2, "vpn": 1, "guest": 1, "total": 7}
    assert dash["devices"] == 4 and dash["online"] == 3
    assert dash["offline"] == ["Garage AP (offline)"]
    assert dash["updates"] == 1
    assert dash["aps"] == [{"n": "Living Room", "c": 4, "on": True, "r": 8},
                           {"n": "Garage AP", "c": 0, "on": False}]
    assert len(dash["lat"]) == 48
    assert dash["poe"] == {"w": 87, "max": 400, "pct": 22, "other": 12, "hot": ""}


def test_dashboard_without_cloud_key(config):
    dash = build(config, cloud_key="")
    assert dash["internet"] == "unknown"
    assert dash["latency"] is None and dash["lat"] == []
    assert dash["down"] == 246


def test_cloud_outage_keeps_local_data(config):
    dash = build(config, cloud={})  # every cloud request returns 404
    assert dash["status"] == "ok"
    assert dash["internet"] == "unknown"
    assert dash["clients"]["total"] == 7 and dash["down"] == 246


def test_missing_gateway_stats_keeps_dashboard(config):
    routes = local_routes()
    del routes[f"{LOCAL_BASE}/sites/{SITE_ID}/devices/{GW}/statistics/latest"]
    dash = build(config, local=routes)
    assert dash["cpu"] is None and dash["down"] is None
    assert dash["internet"] == "up"


def test_gateway_offline_means_internet_down(config):
    devices = [device(GW, "UDM", ["gateway"], state="OFFLINE")]
    dash = build(config, local=local_routes(devices=devices, clients=[]))
    assert dash["internet"] == "down"
    assert dash["cpu"] is None and dash["uptime"] == ""


def test_packet_loss_or_issues_mean_degraded(config):
    assert build(config, cloud=cloud_routes(latencies=[10, 12], loss=[0, 3]))["internet"] == "degraded"
    assert build(config, cloud=cloud_routes(issues=[{"type": "highLatency"}]))["internet"] == "degraded"


def test_isp_summary():
    summary = ud.summarize_isp(list(reversed(isp_periods([10, 20, 30], loss=[0, 2, 0]))))
    assert summary["latency"] == 30  # newest period, even when the API returns them out of order
    assert summary["latency_avg"] == 20
    assert summary["latency_max"] == 35
    assert summary["loss"] == 0 and summary["loss_max"] == 2 and summary["lossy"] == 1
    assert summary["isp"] == "Ziply"
    assert ud.summarize_isp([]) == {}


def test_latency_series_buckets_and_gaps():
    periods = isp_periods(list(range(288)))
    series = ud.latency_series(periods)
    assert len(series) == 48
    assert series[0] == round(sum(range(6)) / 6)
    periods[0]["data"]["wan"]["avgLatency"] = None
    assert ud.latency_series(periods[:6], points=6)[0] is None
    assert ud.latency_series([]) == []


def test_paging_follows_total_count(config):
    items = [client("WIRED")] * 450

    def clients_page(params):
        start = params["offset"]
        chunk = items[start:start + params["limit"]]
        return {"offset": start, "limit": params["limit"], "count": len(chunk),
                "totalCount": len(items), "data": chunk}

    routes = local_routes()
    routes[f"{LOCAL_BASE}/sites/{SITE_ID}/clients"] = clients_page
    session = FakeSession(routes)
    api = ud.LocalApi(config, session)
    assert len(api.get_all(f"/sites/{SITE_ID}/clients")) == 450
    assert [p["offset"] for path, p in session.calls if path.endswith("/clients")] == [0, 200, 400]


def test_site_selection(config):
    routes = local_routes()
    routes[f"{LOCAL_BASE}/sites"] = page([
        {"id": "a", "internalReference": "default", "name": "Default"},
        {"id": "b", "internalReference": "x7k2", "name": "Cabin"}])
    api = ud.LocalApi(config, FakeSession(routes))
    assert api.site()["id"] == "a"
    assert api.site("cabin")["id"] == "b"
    assert api.site("x7k2")["id"] == "b"
    with pytest.raises(RuntimeError):
        api.site("nope")


def test_pick_cloud_site():
    local = {"internalReference": "default", "name": "Home"}
    home = {"siteId": "1", "meta": {"name": "default", "desc": "Home"}}
    cabin = {"siteId": "2", "meta": {"name": "default", "desc": "Cabin"}}
    assert ud.pick_cloud_site([cabin], local) is cabin  # a single site always matches
    assert ud.pick_cloud_site([cabin, home], local) is home
    assert ud.pick_cloud_site([cabin, home], {"internalReference": "default", "name": "Other"}) is None
    assert ud.pick_cloud_site([cabin, home], local, wanted_id="2") is cabin


def test_duration_and_mbps():
    assert ud.duration(None) == ""
    assert ud.duration(420) == "7m"
    assert ud.duration(5 * 3600 + 20 * 60) == "5h 20m"
    assert ud.duration(12 * 86400 + 4 * 3600) == "12d 4h"
    assert ud.mbps(None) is None
    assert ud.mbps(950_000) == 0.9
    assert ud.mbps(940_400_000) == 940


def test_fit_trims_to_limit(config):
    dash = build(config)
    dash["offline"] = [f"Device number {i} (offline)" for i in range(4)]
    dash["aps"] = [{"n": f"Access point {i}", "c": i, "on": True, "r": 3} for i in range(6)]
    full = len(json.dumps({"merge_variables": dash}, separators=(",", ":")).encode())
    fitted = ud.fit(json.loads(json.dumps(dash)), 750)
    size = len(json.dumps({"merge_variables": fitted}, separators=(",", ":"), ensure_ascii=False).encode())
    assert full > 750 >= size
    assert 12 <= len(fitted["lat"]) < 48


def test_typical_payload_fits_2kb(config):
    dash = build(config)
    dash["offline"] = [f"{'Long device name'} (connection interrupted)" for _ in range(4)]
    dash["aps"] = [{"n": "Access point name", "c": 30, "on": True, "r": 12} for _ in range(6)]
    body = json.dumps({"merge_variables": dash}, separators=(",", ":"), ensure_ascii=False).encode()
    assert len(body) <= 2048


def test_poe_uses_port_sum_without_total_and_flags_hot_switch():
    poe = ud.summarize_poe([
        {"name": "Garage Switch", "total_max_power": 52, "port_table": [poe_port(1, 23.1), poe_port(2, 21.5)]},
        {"name": "Office Switch", "total_max_power": 400, "total_used_power": 60},
    ])
    assert poe == {"w": 105, "max": 452, "pct": 23, "other": 0, "hot": "Garage Switch 86%"}


def test_poe_without_any_budget():
    poe = ud.summarize_poe(LEGACY["data"][:1])
    assert poe == {"w": 0, "max": None, "pct": None, "other": 12, "hot": ""}


def test_no_poe_devices():
    assert ud.summarize_poe([{"name": "AP", "port_table": [poe_port(1, 0, poe=False)]}]) is None
    assert ud.summarize_poe([]) is None


def test_classic_api_rejected_skips_poe(config, capsys):
    routes = local_routes()
    del routes[LEGACY_DEVICES]  # 404, like a console that refuses the key there
    ud._POE_WARNED.clear()
    first = build(config, local=routes)
    second = build(config, local=routes)
    assert first["poe"] is None and second["poe"] is None
    assert first["status"] == "ok" and first["clients"]["total"] == 7
    assert capsys.readouterr().err.count("PoE data unavailable") == 1


def test_show_poe_off_skips_classic_api(config):
    config.show_poe = False
    session = FakeSession(local_routes())
    config.cloud_key = ""
    dash = ud.build_dashboard(config, ud.LocalApi(config, session))
    assert dash["poe"] is None
    assert LEGACY_DEVICES not in [path for path, _ in session.calls]
