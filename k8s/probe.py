#!/usr/bin/env python3
"""Sakoo infra probe. Checks the infra-owned layers of Sakoo and POSTs the results to the receiver on the
London server, which sends the Telegram alerts. Stdlib only. Runs in two places with different configs:
  - inside the cluster (k8s/probe.json): cluster DNS, ingress, node objects via the pod's service account
  - on a machine with the VPNs (station/probe-station.json): internal and public ingress path, node kubelet
    ports, Kubernetes API, warning events read with a kubeconfig

  probe.py            loop forever: check, report, sleep interval_s
  probe.py --print    one round, print results, do not report
Config: JSON file from env PROBE_CONFIG (default ./probe.json). Secret: env PROBE_KEY (shared with the receiver).
Result format: {check_name: [ok, detail, owner]}, owner = "infra" | "us" | "monitor".
"""
import base64
import calendar
import email.utils
import hashlib
import hmac
import json
import os
import re
import socket
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CFG = json.loads(Path(os.environ.get("PROBE_CONFIG", "probe.json")).read_text())
TIMEOUT = CFG.get("timeout_s", 8)
CERT_WARN_DAYS = CFG.get("cert_warn_days", 14)
SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")

# Warning events that mean the platform is down or out of capacity (infra), as (check name, reason, all-of substrings).
EVENT_RULES = [
    ("GPU capacity", "FailedScheduling", ["Insufficient nvidia.com/gpu"]),
    ("CPU capacity", "FailedScheduling", ["Insufficient cpu"]),
    ("RAM capacity", "FailedScheduling", ["Insufficient memory"]),
    ("nodes ready", "NodeNotReady", []),
    ("volumes (Ceph)", "FailedMount", []),
    ("volumes (Ceph)", "FailedAttachVolume", []),
    ("Harbor reachable", "Failed", ["harbor", "i/o timeout"]),
    ("Harbor reachable", "Failed", ["harbor", "connection refused"]),
    ("Harbor reachable", "Failed", ["harbor", "503"]),
]


CLOCK = {"offset": 0.0}  # seconds to add to the host clock; hosts in Iran often cannot reach NTP


def now():
    return time.time() + CLOCK["offset"]


def sync_clock():
    """Take the time from the receiver's HTTP Date header, so a drifting host clock cannot break the
    receiver's freshness check or the event window. Keeps the last offset if the receiver is unreachable."""
    url = CFG["receiver_url"].rsplit("/", 1)[0] + "/health"
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=TIMEOUT) as r:
            CLOCK["offset"] = email.utils.parsedate_to_datetime(r.headers["Date"]).timestamp() - time.time()
    except Exception:
        pass


def err(e):
    return "%s: %s" % (type(e).__name__, e) if str(e) else type(e).__name__


def get(url, insecure=False, headers=None, ctx=None):
    """(status, body). Raises on network/TLS failure; any HTTP status means the server answered."""
    ctx = ctx or (ssl._create_unverified_context() if insecure else ssl.create_default_context())
    req = urllib.request.Request(url, headers=dict({"User-Agent": "sakoo-infra-probe"}, **(headers or {})))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
    try:  # no proxy: checks must take the direct path into Sakoo, not an HTTP(S)_PROXY from the environment
        with opener.open(req, timeout=TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def check_dns(name):
    try:
        return True, socket.gethostbyname(name)
    except OSError as e:
        return False, err(e)


def check_host(host, ip=None):
    """TCP + TLS to the ingress, certificate expiry, and the HTTP status of /. With ip, connect there and send
    host as SNI and Host header (like curl --resolve). Returns (tls_ok, detail, http_status or None)."""
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((ip or host, 443), timeout=TIMEOUT) as s, ctx.wrap_socket(s, server_hostname=host) as t:
            days = (ssl.cert_time_to_seconds(t.getpeercert()["notAfter"]) - now()) / 86400
            t.sendall(b"GET / HTTP/1.1\r\nHost: %s\r\nUser-Agent: sakoo-infra-probe\r\nConnection: close\r\n\r\n" % host.encode())
            line = t.recv(256).split(b"\r\n")[0].split()
        code = int(line[1]) if len(line) > 1 and line[1].isdigit() else None
        if days < CERT_WARN_DAYS:
            return False, "certificate expires in %.0f days" % days, code
        return True, "HTTP %s, cert %.0f days left" % (code, days), code
    except (OSError, ssl.SSLError) as e:
        return False, err(e), None


def check_tcp(ip, port):
    try:
        socket.create_connection((ip, port), timeout=TIMEOUT).close()
        return True, "%s:%d open" % (ip, port)
    except OSError as e:
        return False, err(e)


def check_http(url, healthy_text=None, insecure=False):
    try:
        code, body = get(url, insecure)
    except (OSError, ssl.SSLError) as e:
        return False, err(e)
    if healthy_text and healthy_text.encode() not in body:
        return False, "HTTP %d, %r not in response" % (code, healthy_text)
    return True, "HTTP %d" % code


def check_vpn(iface):
    try:
        state = Path("/sys/class/net/%s/operstate" % iface).read_text().strip()
    except OSError:
        return False, "interface %s does not exist (VPN not running?)" % iface
    return state in ("up", "unknown"), "%s is %s" % (iface, state)  # tun devices report "unknown" when up


def check_nodes():
    """Node Ready / pressure and GPU allocatable, via the API with the pod's service account (in-cluster mode).
    Returns {} when the account may not read nodes."""
    try:
        code, body = get("https://kubernetes.default.svc/api/v1/nodes", ctx=ssl.create_default_context(cafile=str(SA / "ca.crt")),
                         headers={"Authorization": "Bearer " + (SA / "token").read_text()})
    except (OSError, ssl.SSLError) as e:
        return {"nodes": (False, err(e), "infra")}
    if code != 200:
        return {}
    res = {}
    for n in json.loads(body)["items"]:
        name, st = n["metadata"]["name"], n["status"]
        bad = [c["type"] for c in st.get("conditions", []) if (c["type"] == "Ready") != (c["status"] == "True")]
        gpu_cap = int(st.get("capacity", {}).get("nvidia.com/gpu", 0))
        gpu_ok = int(st.get("allocatable", {}).get("nvidia.com/gpu", 0))
        if gpu_cap and gpu_ok < gpu_cap:
            bad.append("GPU allocatable %d/%d" % (gpu_ok, gpu_cap))
        res["node " + name] = (not bad, ", ".join(bad) or ("Ready" + (", %d GPU" % gpu_cap if gpu_cap else "")), "infra")
    return res


_kube = {}


def kube():
    """(server, ssl context) from the kubeconfig's first cluster and user (client certificate). Cached."""
    if not _kube:
        text = Path(CFG["kubeconfig"]).read_text()
        field = lambda k: re.search(r"^\s*%s:\s*(\S+)" % k, text, re.M).group(1)
        ctx = ssl.create_default_context(cadata=base64.b64decode(field("certificate-authority-data")).decode())
        d = tempfile.mkdtemp()  # load_cert_chain needs files; mkdtemp is private to this user
        for name, k in (("crt", "client-certificate-data"), ("key", "client-key-data")):
            Path(d, name).write_bytes(base64.b64decode(field(k)))
        ctx.load_cert_chain(Path(d, "crt"), Path(d, "key"))
        _kube.update(server=field("server"), ctx=ctx)
    return _kube["server"], _kube["ctx"]


def event_time(e):
    ts = e.get("lastTimestamp") or e.get("eventTime") or e["metadata"]["creationTimestamp"]
    return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))


def check_events():
    """Recent Warning events in the watched namespaces that match EVENT_RULES. One result per rule name."""
    window = CFG.get("events_window_s", 600)
    hits, errors = {name: [] for name, _, _ in EVENT_RULES}, []
    try:
        server, ctx = kube()
    except Exception as e:
        return {"kubernetes events": (False, "kubeconfig: " + err(e), "monitor")}
    for ns in CFG.get("event_namespaces", []):
        url = "%s/api/v1/namespaces/%s/events?fieldSelector=%s" % (server, ns, urllib.parse.quote("type=Warning"))
        try:
            code, body = get(url, ctx=ctx)
        except (OSError, ssl.SSLError) as e:
            errors.append("%s: %s" % (ns, err(e)))
            continue
        if code != 200:
            errors.append("%s: HTTP %d" % (ns, code))
            continue
        for e in json.loads(body)["items"]:
            if now() - event_time(e) > window:
                continue
            msg = e.get("message", "")
            for name, reason, needles in EVENT_RULES:
                if e.get("reason") == reason and all(n.lower() in msg.lower() for n in needles):
                    obj = e.get("involvedObject", {})
                    hits[name].append("%s/%s: %s" % (ns, obj.get("name", "?"), msg[:140]))
    if len(errors) == len(CFG.get("event_namespaces", [])):  # read nothing: rule results would be false "OK"s
        return {"kubernetes events": (False, "; ".join(errors), "monitor")}
    res = {"k8s " + name: (not h, ("%d events, e.g. %s" % (len(h), h[0])) if h else "no events", "infra")
           for name, h in hits.items()}
    if errors:  # cannot read events: the API is checked separately, so this is a probe/RBAC problem
        res["kubernetes events"] = (False, "; ".join(errors), "monitor")
    return res


def fetch(url, ip=None):
    """(status, first 300 bytes of the body) over TLS to ip (or the URL's host), SNI = URL host. Raises on network errors."""
    u = urllib.parse.urlsplit(url)
    with socket.create_connection((ip or u.hostname, u.port or 443), timeout=TIMEOUT) as s:
        with ssl.create_default_context().wrap_socket(s, server_hostname=u.hostname) as t:
            t.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: sakoo-infra-probe\r\nConnection: close\r\n\r\n"
                       % (u.path or "/", u.hostname)).encode())
            data = b""
            while len(data) < 2000:
                chunk = t.recv(2000)
                if not chunk:
                    break
                data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    line = head.split(b"\r\n")[0].split()
    return (int(line[1]) if len(line) > 1 and line[1].isdigit() else 0), body[:300]


def check_assets():
    """MLOps assets (the asset list): each must have a ready pod behind its Kubernetes Service, and, if it has an
    external URL, its app must answer through traefik (status < 500, and not traefik's fallback page)."""
    ns, assets = CFG.get("assets_namespace", "aip-mlops-service"), CFG.get("assets", [])
    server, ctx = kube()
    code, body = get("%s/api/v1/namespaces/%s/endpoints" % (server, ns), ctx=ctx)
    if code != 200:
        return {"asset list": (False, "cannot read endpoints in %s: HTTP %d" % (ns, code), "monitor")}
    ready = {e["metadata"]["name"]: sum(len(x.get("addresses", [])) for x in e.get("subsets", []))
             for e in json.loads(body)["items"]}
    fallback = {}  # host -> body of a path no app serves, to recognise a route that has disappeared
    res = {}
    for a in assets:
        label = "%s (%s, %s)" % (a["name"], a.get("person", "?"), a.get("value", "?"))
        problems = ["%s: no ready pod" % svc if svc in ready else "%s: no such Service" % svc
                    for svc in a.get("services", [a["name"]]) if not ready.get(svc)]
        url = a.get("url")
        if url and not problems:
            host = urllib.parse.urlsplit(url).hostname
            try:
                if host not in fallback:
                    fallback[host] = fetch("https://%s/sakoo-probe-no-such-path" % host, CFG.get("ingress_ip"))[1]
                status, sample = fetch(url, CFG.get("ingress_ip"))
                if status >= 500 or status == 0:
                    problems.append("%s answered HTTP %d" % (url, status))
                elif urllib.parse.urlsplit(url).path not in ("", "/") and sample == fallback[host]:
                    problems.append("%s has no route (traefik serves the fallback page)" % url)
            except (OSError, ssl.SSLError) as e:
                problems.append("%s: %s" % (url, err(e)))
        n = sum(ready.get(svc, 0) for svc in a.get("services", [a["name"]]))
        res[label] = (not problems, "; ".join(problems) or "%d ready pod(s)%s" % (n, ", URL answers" if url else ""),
                      a.get("owner", "asset"))
    return res


def host_checks(res, ip, label, owner_if_all_fail):
    """Check every host over one path; one alert if all fail, else per-host results. Returns True if any answered."""
    hosts = {h: check_host(h, ip) for h in CFG.get("hosts", [])}
    if not hosts:
        return True
    if not any(ok or code for ok, _, code in hosts.values()):
        res[label] = (False, "all %d hostnames fail: %s" % (len(hosts), next(iter(hosts.values()))[1]), owner_if_all_fail)
        return False
    res[label] = (True, "%d/%d hostnames answer" % (sum(bool(c) for _, _, c in hosts.values()), len(hosts)), owner_if_all_fail)
    return hosts


def run_checks():
    if CFG.get("receiver_url"):
        sync_clock()
    res, vpn = {}, CFG.get("vpn", {})  # {"cluster": "tun0", "endpoints": "tun1"}
    for role, iface in vpn.items():
        res["probe VPN " + role] = check_vpn(iface) + ("monitor",)
    cluster_ok = res.get("probe VPN cluster", (True,))[0]
    public_ok = res.get("probe VPN endpoints", (True,))[0]

    if cluster_ok:
        for name in CFG.get("dns_names", []):
            res["dns " + name] = check_dns(name) + ("infra",)
        hosts = host_checks(res, CFG.get("ingress_ip"), "ingress (MetalLB/traefik)", "infra")
        if isinstance(hosts, dict):
            for h, (ok, detail, code) in hosts.items():
                if code and code >= 500:  # traefik answered, the app behind it did not: ours
                    ok, detail = False, "HTTP %d from the app behind traefik" % code
                res["host " + h] = (ok, detail, "infra" if "certificate" in detail else "us")
        for name, (ip, port) in CFG.get("tcp_checks", {}).items():
            res[name] = check_tcp(ip, port) + ("infra",)
        for name, c in CFG.get("http_checks", {}).items():
            res[name] = check_http(c["url"], c.get("healthy_text"), c.get("insecure", False)) + (c.get("owner", "infra"),)
        if CFG.get("kubeconfig") and res.get("kubernetes API", (True,))[0]:  # API down is already its own alert
            res.update(check_events())
            if CFG.get("assets"):
                try:
                    res.update(check_assets())
                except Exception as e:
                    res["asset list"] = (False, err(e), "monitor")
        if CFG.get("nodes"):
            res.update(check_nodes())

    # the public path users take: public IP -> edge firewall/NAT -> traefik. Only meaningful if traefik itself is up.
    if public_ok and CFG.get("public_ip") and res.get("ingress (MetalLB/traefik)", (True,))[0]:
        host_checks(res, CFG["public_ip"], "public edge (public IP/firewall/NAT)", "infra")
    return res


def report(res):
    body = json.dumps({"probe": socket.gethostname(), "ts": int(now()), "results": res}).encode()
    sig = hmac.new(os.environ["PROBE_KEY"].encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(CFG["receiver_url"], data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Signature": sig})
    urllib.request.urlopen(req, timeout=20).read()


def main():
    if "--print" in sys.argv:
        for name, (ok, detail, owner) in run_checks().items():
            print("%-4s %-45s %-7s %s" % ("OK" if ok else "FAIL", name, owner, detail))
        return
    while True:
        t0 = time.time()
        try:
            report(run_checks())
        except Exception as e:  # receiver unreachable: it raises "probe silent" on its side
            print(time.strftime("%H:%M:%S"), "report failed:", err(e), file=sys.stderr, flush=True)
        time.sleep(max(1, CFG.get("interval_s", 60) - (time.time() - t0)))


if __name__ == "__main__":
    main()
