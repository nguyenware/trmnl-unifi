import contextlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
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
LEGACY_HEALTH = "/proxy/network/api/s/default/stat/health"


def www_health(last_run, down=999.0, up=1017.0, status="Success"):
    # Shaped like a real UniFi OS stat/health "www" entry (unpoller endpoints_data/stat-health.json).
    return {"subsystem": "www", "status": "ok", "latency": 9, "uptime": 492506, "drops": 1,
            "xput_up": up, "xput_down": down, "speedtest_status": status,
            "speedtest_lastrun": last_run, "speedtest_ping": 10}


def health_route(_params):
    return {"meta": {"rc": "ok"}, "data": [{"subsystem": "wlan", "status": "ok"},
                                           www_health(time.time() - 3 * 3600 - 60)]}


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
        LEGACY_HEALTH: health_route,
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
    assert dash["down"] == 246 and dash["up"] == 12
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
    assert dash["speedtest"] == {"down": 999, "up": 1017, "ping": 10, "ago": "3h", "ok": True}


def test_dashboard_without_cloud_key(config):
    dash = build(config, cloud_key="")
    assert dash["internet"] == "up"  # from classic health's www status
    assert dash["latency"] == 9 and dash["lat"] == []  # www latency stands in for the cloud's
    assert dash["down"] == 246


def test_cloud_outage_keeps_local_data(config):
    dash = build(config, cloud={})  # every cloud request returns 404
    assert dash["status"] == "ok"
    assert dash["internet"] == "up"
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
    ud._WARNED.clear()
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


def test_speedtest_summary():
    now = 1_769_700_000
    health = [{"subsystem": "wan"}, www_health(now - 25 * 60, down=236.4, up=11.6)]
    assert ud.summarize_speedtest(health, now) == {"down": 236, "up": 12, "ping": 10, "ago": "25m", "ok": True}
    failed = [www_health(now - 2 * 86400 - 5, status="Failed")]
    assert ud.summarize_speedtest(failed, now)["ok"] is False
    assert ud.summarize_speedtest(failed, now)["ago"] == "2d"


def test_speedtest_never_run():
    assert ud.summarize_speedtest([{"subsystem": "www", "status": "ok", "latency": 9}]) is None
    assert ud.summarize_speedtest([www_health(0)]) is None
    assert ud.summarize_speedtest([www_health(1_769_686_425, down=0, up=0)]) is None
    assert ud.summarize_speedtest([]) is None


def test_ago():
    assert [ud.ago(s) for s in (5, 90, 3 * 3600, 3 * 86400)] == ["now", "1m", "3h", "3d"]


def test_classic_api_rejected_skips_speedtest(config, capsys):
    routes = local_routes()
    del routes[LEGACY_HEALTH]
    ud._WARNED.clear()
    dash = build(config, local=routes)
    assert dash["speedtest"] is None and dash["poe"] is not None
    assert "Health data unavailable" in capsys.readouterr().err


GW_MAC = "28:70:4E:85:A9:E9"


def wan_health(**extra):
    # Shaped like a real UniFi OS stat/health "wan" entry (unpoller endpoints_data/stat-health.json).
    entry = {"subsystem": "wan", "status": "ok", "wan_ip": "203.0.113.7", "gw_mac": GW_MAC.lower(),
             "gw_name": "Dream Machine Pro SE", "isp_name": "Ziply Fiber",
             "gw_system-stats": {"cpu": "5.6", "mem": "67.2", "uptime": "492633"},
             "tx_bytes-r": 1_537_500, "rx_bytes-r": 118_750_000}
    entry.update(extra)
    return entry


def udm_se_routes(gateway_listed=True):
    """A UDM Pro SE whose official device entry lacks the "gateway" feature."""
    devices = [device(AP1, "Garage", ["accessPoint"]), device(SW, "Office Switch", ["switching"])]
    if gateway_listed:
        gw = device(GW, "Dream Machine Pro SE", ["switching"])
        gw["macAddress"] = GW_MAC
        devices.insert(0, gw)
    routes = local_routes(devices=devices)
    routes[LEGACY_HEALTH] = lambda _p: {"data": [wan_health(), www_health(time.time() - 60)]}
    return routes


def test_udm_se_gateway_found_by_mac(config):
    dash = build(config, local=udm_se_routes(), cloud_key="")
    assert dash["gateway"] == "Dream Machine Pro…"  # names are shortened to fit
    assert dash["internet"] == "up"
    assert dash["cpu"] == 12 and dash["uptime"] == "3d 5h"  # official statistics still preferred
    assert dash["down"] == 950 and dash["up"] == 12  # WAN rates from classic health, bytes/s -> Mbps
    assert dash["isp"] == "Ziply Fiber"


def test_gateway_missing_from_official_list_uses_health(config):
    dash = build(config, local=udm_se_routes(gateway_listed=False), cloud_key="")
    assert dash["gateway"] == "Dream Machine Pro…"  # names are shortened to fit
    assert dash["cpu"] == 6 and dash["mem"] == 67 and dash["uptime"] == "5d 16h"
    assert dash["internet"] == "up"


def test_no_gateway_no_health_no_cloud_is_unknown(config):
    routes = local_routes(devices=[device(AP1, "AP", ["accessPoint"])])
    del routes[LEGACY_HEALTH]
    ud._WARNED.clear()
    dash = build(config, local=routes, cloud_key="")
    assert dash["internet"] == "unknown"
    assert dash["gateway"] == "" and dash["cpu"] is None


def test_www_error_means_down(config):
    routes = local_routes()
    routes[LEGACY_HEALTH] = lambda _p: {"data": [dict(www_health(time.time()), status="error")]}
    assert build(config, local=routes, cloud_key="")["internet"] == "down"


def test_cloud_site_matched_by_gateway_mac(config):
    # Two consoles on one account, both with a site called "Default (default)".
    old = {"siteId": "old", "meta": {"name": "default", "desc": "Default", "gatewayMac": "70:a7:41:00:00:01"}}
    cloud = cloud_routes()
    cloud["/v1/sites"]["data"][0]["meta"]["gatewayMac"] = GW_MAC.lower()
    cloud["/v1/sites"]["data"].insert(0, old)
    dash = build(config, local=udm_se_routes(), cloud=cloud)
    assert dash["isp"] == "Ziply Fiber" and dash["wan_uptime"] == 99.9 and len(dash["lat"]) == 48


def test_pick_cloud_site_by_mac():
    local = {"internalReference": "default", "name": "Default"}
    a = {"siteId": "a", "meta": {"name": "default", "desc": "Default", "gatewayMac": "aa:aa:aa:aa:aa:aa"}}
    b = {"siteId": "b", "meta": {"name": "default", "desc": "Default", "gatewayMac": "bb:bb:bb:bb:bb:bb"}}
    assert ud.pick_cloud_site([a, b], local, gateway_mac="BB:BB:BB:BB:BB:BB") is b
    assert ud.pick_cloud_site([a, b], local) is None
    assert ud.pick_cloud_site([a, b], local, gateway_mac="cc:cc:cc:cc:cc:cc") is None


def test_status_reasons(config):
    dash = build(config, cloud=cloud_routes(latencies=[10, 12], loss=[0, 3]))
    assert (dash["internet"], dash["why"]) == ("degraded", "packet loss 3%")
    dash = build(config, cloud=cloud_routes(issues=[{"type": "high_latency", "wanId": "WAN"}]))
    assert (dash["internet"], dash["why"]) == ("degraded", "high latency")
    dash = build(config, cloud=cloud_routes(issues=[{"wanId": "WAN"}, {}]))
    assert dash["why"] == "UniFi reports 2 internet issues"
    assert build(config)["why"] == ""
    devices = [device(GW, "UDM", ["gateway"], state="CONNECTION_INTERRUPTED")]
    assert build(config, local=local_routes(devices=devices, clients=[]))["why"] == "gateway connection interrupted"


def test_mbps_rounding():
    assert ud.mbps(9_440_000) == 9.4
    assert ud.mbps(20_040_000) == 20


# Seen on a live UDM Pro SE: a WAN outage ~19.7h before this run, and the 5-minute period after it.
REAL_NOW = 1_790_751_181
REAL_ISSUES = [{"index": 5968934, "wanDowntime": True}, {"index": 5968935}]


def test_old_issues_are_history_not_status():
    split = ud.split_issues(REAL_ISSUES, REAL_NOW)
    assert split == {"issues": [], "last_issue": "WAN down 19h ago"}


def test_recent_issue_is_current():
    now_index = REAL_NOW // 300
    split = ud.split_issues([{"index": now_index - 1, "wanDowntime": True}], REAL_NOW)
    assert len(split["issues"]) == 1 and split["last_issue"].startswith("WAN down")
    assert ud.describe_issues(split["issues"]) == "WAN down"


def test_unrecognised_issue_format_counts_as_current():
    weird = [{"index": 42}, {"wanId": "WAN"}, "text", {"index": True}]
    assert ud.split_issues(weird, REAL_NOW) == {"issues": weird, "last_issue": ""}


def test_dashboard_up_with_last_issue(config, monkeypatch):
    monkeypatch.setattr(ud.time, "time", lambda: REAL_NOW)
    dash = build(config, cloud=cloud_routes(issues=REAL_ISSUES))
    assert dash["internet"] == "up"
    assert dash["why"] == "WAN down 19h ago"


def test_current_issue_degrades(config, monkeypatch):
    monkeypatch.setattr(ud.time, "time", lambda: REAL_NOW)
    issues = [{"index": REAL_NOW // 300, "wanDowntime": True}]
    dash = build(config, cloud=cloud_routes(issues=issues))
    assert (dash["internet"], dash["why"]) == ("degraded", "WAN down")



@contextlib.contextmanager
def console_answering(statuses):
    """A real local HTTP server that answers each GET with the next status (the last one repeats)."""
    hits = []
    body = json.dumps(page([{"id": SITE_ID, "internalReference": "default", "name": "Default"}])).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # pylint: disable=invalid-name
            status = statuses[min(len(hits), len(statuses) - 1)]
            hits.append(status)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body if status == 200 else b"{}")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", hits
    finally:
        server.shutdown()


def test_local_api_retries_temporary_503(config, monkeypatch):
    monkeypatch.setattr(ud, "RETRY_BACKOFF", 0)
    with console_answering([503, 503, 200]) as (url, hits):  # Network app restarting, then back
        config.host = url
        assert ud.LocalApi(config).site()["name"] == "Default"
        assert hits == [503, 503, 200]


def test_local_api_gives_up_after_retries(config, monkeypatch):
    monkeypatch.setattr(ud, "RETRY_BACKOFF", 0)
    with console_answering([503]) as (url, hits):
        config.host = url
        with pytest.raises(ud.requests.HTTPError, match="503"):
            ud.LocalApi(config).site()
        assert len(hits) == 4  # the first try plus 3 retries
