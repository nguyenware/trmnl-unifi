#!/usr/bin/env python3
"""UniFi dashboard: show the health of a UniFi network on a TRMNL.

Reads two official UniFi APIs:

  * the local UniFi Network Integration API on the console (UDM Pro, UDM SE,
    Cloud Gateway, ...): devices, clients, and live gateway statistics, and
  * the optional Site Manager cloud API (api.ui.com): ISP name, WAN uptime,
    internet issues and 24 hours of latency / packet loss history.

The combined summary is then either:

  * pushed to a TRMNL private plugin webhook (``push``, the default), or
  * served as JSON for a TRMNL polling plugin / BYOS server (``serve``), or
  * printed once (``once``) for testing. ``check`` tests both API keys.

Configuration comes from environment variables (optionally loaded from a
``.env`` file next to this script). See ``.env.example``.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
import urllib3

HERE = os.path.dirname(os.path.abspath(__file__))

CLOUD_URL = "https://api.ui.com"
PAGE_LIMIT = 200  # the Network API's maximum page size
LATENCY_POINTS = 48  # 24h of 5-minute samples averaged into 30-minute buckets
MAX_LISTED = 4  # offline devices named on screen
MAX_APS = 6
NAME_LEN = 18
POE_HOT_PCT = 80  # name a device on screen once its PoE draw reaches this share of its budget


def load_dotenv(path):
    """Minimal .env loader so the script only depends on requests."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def env_flag(name, default):
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value not in ("0", "false", "no", "off")


class Config:  # pylint: disable=too-many-instance-attributes,too-few-public-methods
    """All settings, read from the environment."""

    def __init__(self):
        host = os.environ.get("UNIFI_HOST", "").strip().rstrip("/")
        if host and not host.startswith(("http://", "https://")):
            host = f"https://{host}"
        self.host = host
        self.api_key = os.environ.get("UNIFI_API_KEY", "").strip()
        self.site = os.environ.get("UNIFI_SITE", "").strip()
        ca_bundle = os.environ.get("UNIFI_CA_BUNDLE", "").strip()
        # UniFi consoles ship a self-signed certificate, so verification is off
        # unless a CA bundle is given or UNIFI_VERIFY_SSL=1.
        self.verify = ca_bundle or env_flag("UNIFI_VERIFY_SSL", False)
        self.cloud_key = os.environ.get("UNIFI_CLOUD_API_KEY", "").strip()
        self.cloud_site_id = os.environ.get("UNIFI_CLOUD_SITE_ID", "").strip()
        self.webhook_url = os.environ.get("TRMNL_WEBHOOK_URL", "").strip()
        self.interval = int(os.environ.get("PUSH_INTERVAL", "") or 300)
        self.payload_limit = int(os.environ.get("PAYLOAD_LIMIT", "") or 2048)
        self.show_poe = env_flag("SHOW_POE", True)
        self.time_format = os.environ.get("TIME_FORMAT", "%I:%M %p")
        self.listen_host = os.environ.get("HOST", "0.0.0.0")
        self.port = int(os.environ.get("PORT", "") or 5000)
        if not self.verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class LocalApi:
    """The UniFi Network Integration API on the console."""

    def __init__(self, config, session=None):
        if not config.host or not config.api_key:
            raise SystemExit("UNIFI_HOST and UNIFI_API_KEY are required")
        self.base = f"{config.host}/proxy/network/integration/v1"
        self.legacy_base = f"{config.host}/proxy/network/api"
        self.session = session or requests.Session()
        self.session.headers.update({"X-API-Key": config.api_key, "Accept": "application/json"})
        self.session.verify = config.verify

    def get(self, path, **params):
        response = self.session.get(f"{self.base}{path}", params=params or None, timeout=15)
        response.raise_for_status()
        return response.json()

    def get_all(self, path):
        """Follows offset/limit paging and returns every item."""
        items, offset = [], 0
        while True:
            page = self.get(path, offset=offset, limit=PAGE_LIMIT)
            data = page.get("data") or []
            items.extend(data)
            offset += len(data)
            if not data or offset >= page.get("totalCount", 0):
                return items

    def legacy_devices(self, site_ref):
        """Raw devices from the undocumented classic API, the only source of PoE watts.

        UniFi OS accepts the same X-API-Key here, but Ubiquiti doesn't document
        or guarantee it.
        """
        response = self.session.get(f"{self.legacy_base}/s/{site_ref}/stat/device", timeout=20)
        response.raise_for_status()
        return response.json().get("data") or []

    def site(self, wanted=""):
        """The site matching UNIFI_SITE (name, internal reference or id), else the first one."""
        sites = self.get_all("/sites")
        if not sites:
            raise RuntimeError("The console reported no sites")
        if not wanted:
            return sites[0]
        for site in sites:
            if wanted.lower() in (site.get("id", "").lower(), site.get("name", "").lower(),
                                  site.get("internalReference", "").lower()):
                return site
        names = ", ".join(s.get("name", "") for s in sites)
        raise RuntimeError(f"No site named {wanted!r} (found: {names})")


class CloudApi:
    """The Site Manager API at api.ui.com (read-only key from unifi.ui.com)."""

    def __init__(self, config, session=None):
        self.session = session or requests.Session()
        self.session.headers.update({"X-API-Key": config.cloud_key, "Accept": "application/json"})

    def get(self, path, **params):
        response = self.session.get(f"{CLOUD_URL}{path}", params=params or None, timeout=20)
        response.raise_for_status()
        return response.json()

    def sites(self):
        sites, token = [], None
        while True:
            page = self.get("/v1/sites", **({"nextToken": token} if token else {}))
            sites.extend(page.get("data") or [])
            token = page.get("nextToken")
            if not token:
                return sites

    def isp_metrics(self):
        return self.get("/v1/isp-metrics/5m", duration="24h").get("data") or []


def pick_cloud_site(sites, local_site, wanted_id=""):
    """Matches the cloud site to the local one by id, internal name ("default") or display name."""
    if wanted_id:
        return next((s for s in sites if s.get("siteId") == wanted_id), None)
    if len(sites) == 1:
        return sites[0]
    # Every console has a site called "default", so the display name must match too.
    ref = (local_site.get("internalReference") or "").lower()
    name = (local_site.get("name") or "").lower()
    matches = [s for s in sites
               if (s.get("meta") or {}).get("name", "").lower() == ref
               and (s.get("meta") or {}).get("desc", "").lower() == name]
    return matches[0] if len(matches) == 1 else None


def short(name, length=NAME_LEN):
    name = (name or "").strip()
    return name if len(name) <= length else name[: length - 1].rstrip() + "…"


def duration(seconds):
    """Compact uptime such as 12d 4h, 5h 20m or 7m."""
    if seconds is None:
        return ""
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def mbps(bps):
    if bps is None:
        return None
    value = bps / 1_000_000
    return round(value, 1) if value < 100 else round(value)


def pct(value):
    return None if value is None else round(value)


def count_clients(clients):
    counts = {"wifi": 0, "wired": 0, "vpn": 0, "guest": 0}
    for client in clients:
        kind = client.get("type")
        if kind == "WIRELESS":
            counts["wifi"] += 1
        elif kind == "WIRED":
            counts["wired"] += 1
        elif kind in ("VPN", "TELEPORT"):
            counts["vpn"] += 1
        if (client.get("access") or {}).get("type") == "GUEST":
            counts["guest"] += 1
    counts["total"] = len(clients)
    return counts


def latency_series(periods, points=LATENCY_POINTS):
    """Averages 5-minute avgLatency samples into `points` evenly sized buckets (oldest first)."""
    samples = []
    for period in sorted(periods, key=lambda p: p.get("metricTime", "")):
        wan = (period.get("data") or {}).get("wan") or {}
        samples.append(wan.get("avgLatency"))
    if not samples:
        return []
    size = max(1, -(-len(samples) // points))  # ceiling division
    series = []
    for start in range(0, len(samples), size):
        bucket = [s for s in samples[start:start + size] if s is not None]
        series.append(round(sum(bucket) / len(bucket)) if bucket else None)
    return series


def summarize_isp(periods):
    """Latest and 24h figures from 5-minute ISP metrics."""
    ordered = sorted(periods, key=lambda p: p.get("metricTime", ""))
    wans = [(p.get("data") or {}).get("wan") or {} for p in ordered]
    if not wans:
        return {}
    latencies = [w["avgLatency"] for w in wans if w.get("avgLatency") is not None]
    losses = [w["packetLoss"] for w in wans if w.get("packetLoss") is not None]
    latest = wans[-1]
    return {
        "isp": latest.get("ispName") or next((w.get("ispName") for w in reversed(wans) if w.get("ispName")), ""),
        "latency": latest.get("avgLatency"),
        "latency_avg": round(sum(latencies) / len(latencies)) if latencies else None,
        "latency_max": max((w.get("maxLatency") or 0) for w in wans) or None,
        "loss": latest.get("packetLoss"),
        "loss_max": max(losses) if losses else None,
        "lossy": sum(1 for x in losses if x),  # 5-minute intervals with any packet loss
        "lat": latency_series(ordered),
    }


def build_cloud(config, local_site, cloud=None):
    """ISP / internet fields from the Site Manager API, or {} if it isn't configured or fails.

    A cloud outage shouldn't blank the dashboard, so errors are logged and the
    local data is still pushed.
    """
    if not config.cloud_key:
        return {}
    try:
        return fetch_cloud(config, local_site, cloud or CloudApi(config))
    except (requests.RequestException, ValueError) as error:
        print(f"Cloud API failed, showing local data only: {error}", file=sys.stderr, flush=True)
        return {}


def fetch_cloud(config, local_site, cloud):
    site = pick_cloud_site(cloud.sites(), local_site, config.cloud_site_id)
    if site is None:
        print("Could not match a cloud site; set UNIFI_CLOUD_SITE_ID", file=sys.stderr)
        return {}
    stats = site.get("statistics") or {}
    counts = stats.get("counts") or {}
    result = {
        "isp": (stats.get("ispInfo") or {}).get("name", ""),
        "wan_uptime": (stats.get("percentages") or {}).get("wanUptime"),
        "issues": len(stats.get("internetIssues") or []),
        "alerts": counts.get("criticalNotification", 0),
        "gateway_offline": counts.get("offlineGatewayDevice", 0),
    }
    for entry in cloud.isp_metrics():
        if entry.get("siteId") == site.get("siteId"):
            isp = summarize_isp(entry.get("periods") or [])
            isp["isp"] = result["isp"] or isp.get("isp", "")
            result.update(isp)
            break
    return result


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def summarize_poe(legacy_devices):
    """PoE draw against budget, from classic /stat/device records.

    Devices that report a budget (total_max_power) make up the bar. Draw on
    devices without one, such as some gateways, is reported separately.
    Returns None when nothing supplies PoE.
    """
    used = budget = other = 0.0
    hot, found = [], False
    for dev in legacy_devices:
        ports = [p for p in dev.get("port_table") or [] if p.get("port_poe")]
        max_power = to_float(dev.get("total_max_power")) or 0
        if not ports and not max_power:
            continue
        found = True
        draw = to_float(dev.get("total_used_power"))
        if draw is None:
            draw = sum(to_float(p.get("poe_power")) or 0 for p in ports)
        if max_power:
            used += draw
            budget += max_power
            share = 100 * draw / max_power
            if share >= POE_HOT_PCT:
                hot.append((share, f"{short(dev.get('name') or dev.get('model'))} {round(share)}%"))
        else:
            other += draw
    if not found:
        return None
    return {
        "w": round(used),
        "max": round(budget) or None,
        "pct": round(100 * used / budget) if budget else None,
        "other": round(other),
        "hot": max(hot)[1] if hot else "",
    }


_POE_WARNED = []


def build_poe(config, local, site):
    """PoE summary, or None if disabled or the classic API isn't reachable with the key."""
    if not config.show_poe:
        return None
    try:
        poe = summarize_poe(local.legacy_devices(site.get("internalReference") or "default"))
        _POE_WARNED.clear()
        return poe
    except (requests.RequestException, ValueError) as error:
        if not _POE_WARNED:  # once per outage, not every push
            print(f"PoE data unavailable (classic API): {error}; set SHOW_POE=0 to stop trying",
                  file=sys.stderr, flush=True)
            _POE_WARNED.append(True)
        return None


def internet_status(cloud, gateway):
    """up / degraded / down / unknown for the headline."""
    if gateway is None or gateway.get("state") != "ONLINE" or cloud.get("gateway_offline"):
        return "down"
    if not cloud:
        return "unknown"
    if cloud.get("issues") or cloud.get("loss"):
        return "degraded"
    return "up"


def build_dashboard(config, local=None, cloud_api=None):  # pylint: disable=too-many-locals
    """Builds the display payload."""
    local = local or LocalApi(config)
    site = local.site(config.site)
    site_id = site["id"]
    devices = local.get_all(f"/sites/{site_id}/devices")
    clients = local.get_all(f"/sites/{site_id}/clients")

    gateway = next((d for d in devices if "gateway" in (d.get("features") or [])), None)
    gw_stats = {}
    if gateway and gateway.get("state") == "ONLINE":
        try:
            gw_stats = local.get(f"/sites/{site_id}/devices/{gateway['id']}/statistics/latest")
        except (requests.RequestException, ValueError) as error:
            print(f"Gateway statistics failed: {error}", file=sys.stderr, flush=True)

    per_uplink = {}
    for client in clients:
        if client.get("type") == "WIRELESS" and client.get("uplinkDeviceId"):
            per_uplink[client["uplinkDeviceId"]] = per_uplink.get(client["uplinkDeviceId"], 0) + 1
    aps = []
    for device in devices:
        if "accessPoint" not in (device.get("features") or []):
            continue
        ap = {"n": short(device.get("name") or device.get("model")),
              "c": per_uplink.get(device["id"], 0),
              "on": device.get("state") == "ONLINE"}
        if ap["on"]:
            try:
                stats = local.get(f"/sites/{site_id}/devices/{device['id']}/statistics/latest")
                retries = [r.get("txRetriesPct") for r in (stats.get("interfaces") or {}).get("radios", [])
                           if r.get("txRetriesPct") is not None]
                ap["r"] = pct(max(retries)) if retries else None
            except (requests.RequestException, ValueError):
                ap["r"] = None
        aps.append(ap)
    aps.sort(key=lambda a: (-a["c"], a["n"]))

    offline = [d for d in devices if d.get("state") != "ONLINE"]
    cloud = build_cloud(config, site, cloud_api)
    uplink = gw_stats.get("uplink") or {}
    dashboard = {
        "status": "ok",
        "updated": int(time.time()),
        "time": datetime.now().strftime(config.time_format).lstrip("0"),
        "site": site.get("name", ""),
        "internet": internet_status(cloud, gateway),
        "isp": cloud.get("isp", ""),
        "wan_uptime": cloud.get("wan_uptime"),
        "latency": cloud.get("latency"),
        "latency_avg": cloud.get("latency_avg"),
        "latency_max": cloud.get("latency_max"),
        "loss": cloud.get("loss"),
        "loss_max": cloud.get("loss_max"),
        "lossy": cloud.get("lossy"),
        "alerts": cloud.get("alerts", 0),
        "lat": cloud.get("lat", []),
        # The gateway's uplink is the WAN: rx is download, tx is upload.
        "down": mbps(uplink.get("rxRateBps")),
        "up": mbps(uplink.get("txRateBps")),
        "gateway": short(gateway.get("name") or gateway.get("model")) if gateway else "",
        "cpu": pct(gw_stats.get("cpuUtilizationPct")),
        "mem": pct(gw_stats.get("memoryUtilizationPct")),
        "uptime": duration(gw_stats.get("uptimeSec")),
        "clients": count_clients(clients),
        "devices": len(devices),
        "online": len(devices) - len(offline),
        "offline": [f"{short(d.get('name') or d.get('model'))} ({d.get('state', '').lower().replace('_', ' ')})"
                    for d in offline[:MAX_LISTED]],
        "updates": sum(1 for d in devices if d.get("firmwareUpdatable")),
        "aps": aps[:MAX_APS],
        "poe": build_poe(config, local, site),
    }
    return dashboard


def fit(dashboard, limit):
    """Trims the payload until the webhook body fits TRMNL's size limit."""
    def size():
        return len(json.dumps({"merge_variables": dashboard}, separators=(",", ":"), ensure_ascii=False).encode())

    while size() > limit and len(dashboard.get("lat", [])) > 12:
        dashboard["lat"] = dashboard["lat"][::2]
    while size() > limit and dashboard.get("aps"):
        dashboard["aps"].pop()
    while size() > limit and dashboard.get("offline"):
        dashboard["offline"].pop()
    if size() > limit and dashboard.get("poe"):
        dashboard["poe"]["hot"] = ""
    return dashboard


def push(config, dashboard):
    """Sends the payload to the TRMNL private plugin webhook."""
    body = {"merge_variables": fit(dashboard, config.payload_limit)}
    response = requests.post(config.webhook_url, json=body, timeout=15)
    if response.status_code == 429:
        print("TRMNL rate limit hit (429); consider raising PUSH_INTERVAL", file=sys.stderr)
    response.raise_for_status()


def error_payload(error):
    return {"status": "error", "error": str(error)[:200], "updated": int(time.time())}


def run_push(config):
    if not config.webhook_url:
        raise SystemExit("TRMNL_WEBHOOK_URL is required for push mode")
    failures = 0
    while True:
        try:
            dashboard = build_dashboard(config)
            push(config, dashboard)
            failures = 0
            print(f"Pushed internet={dashboard['internet']} latency={dashboard['latency']}ms "
                  f"clients={dashboard['clients']['total']} devices={dashboard['online']}/{dashboard['devices']}",
                  flush=True)
        except (requests.RequestException, OSError, ValueError, RuntimeError, KeyError) as error:
            failures += 1
            print(f"Update failed: {error}", file=sys.stderr, flush=True)
            if failures == 3:  # show the problem on screen instead of stale numbers
                try:
                    push(config, error_payload(error))
                except requests.RequestException as push_error:
                    print(f"Error push failed: {push_error}", file=sys.stderr, flush=True)
        time.sleep(config.interval)


def run_server(config):
    class Handler(BaseHTTPRequestHandler):
        """Serves the dashboard as JSON."""

        def do_GET(self):  # pylint: disable=invalid-name
            if self.path.split("?")[0] not in ("/", "/unifi"):
                self.send_error(404)
                return
            try:
                status, body = 200, build_dashboard(config)
            except (requests.RequestException, OSError, ValueError, RuntimeError, KeyError) as error:
                status, body = 502, error_payload(error)
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    print(f"Serving on http://{config.listen_host}:{config.port}/unifi", flush=True)
    ThreadingHTTPServer((config.listen_host, config.port), Handler).serve_forever()


def run_check(config):
    """Tests both APIs and prints what was found."""
    ok = True
    try:
        local = LocalApi(config)
        print(f"Network application {local.get('/info').get('applicationVersion', '?')} at {config.host}")
        site = local.site(config.site)
        devices = local.get_all(f"/sites/{site['id']}/devices")
        print(f"  site: {site.get('name')} ({site.get('internalReference')}), {len(devices)} devices")
        if config.show_poe:
            try:
                poe = summarize_poe(local.legacy_devices(site.get("internalReference") or "default"))
                print(f"  PoE: {poe}" if poe else "  PoE: no PoE devices found")
            except (requests.RequestException, ValueError) as error:
                print(f"  PoE: classic API not available with this key ({error}); set SHOW_POE=0")
    except (requests.RequestException, ValueError, RuntimeError) as error:
        print(f"  local API FAILED: {error}")
        return False
    if not config.cloud_key:
        print("Cloud API: not configured (no ISP name, latency or packet loss)")
        return ok
    try:
        sites = CloudApi(config).sites()
        match = pick_cloud_site(sites, site, config.cloud_site_id)
        print(f"Cloud API: {len(sites)} site(s)")
        for cloud_site in sites:
            meta = cloud_site.get("meta") or {}
            mark = "*" if cloud_site is match else " "
            print(f"  {mark} {cloud_site.get('siteId')}  {meta.get('desc', '')} ({meta.get('name', '')})")
        if match is None:
            print("  No match; set UNIFI_CLOUD_SITE_ID to one of the ids above")
            ok = False
    except (requests.RequestException, ValueError) as error:
        print(f"  cloud API FAILED: {error}")
        ok = False
    return ok


def main():
    load_dotenv(os.path.join(HERE, ".env"))
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("mode", nargs="?", default="push", choices=("push", "serve", "once", "check"),
                        help="push to a TRMNL webhook (default), serve JSON, print once, "
                             "or check the API keys")
    args = parser.parse_args()
    config = Config()
    if args.mode == "check":
        sys.exit(0 if run_check(config) else 1)
    elif args.mode == "once":
        dashboard = fit(build_dashboard(config), config.payload_limit)
        print(json.dumps(dashboard, indent=2, ensure_ascii=False))
        size = len(json.dumps({"merge_variables": dashboard}, separators=(",", ":"), ensure_ascii=False).encode())
        print(f"# webhook body: {size} bytes (limit {config.payload_limit})", file=sys.stderr)
    elif args.mode == "serve":
        run_server(config)
    else:
        run_push(config)


if __name__ == "__main__":
    main()
