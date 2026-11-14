#!/usr/bin/env python3
"""AIP infra probe. Checks the infra-owned layers of AIP and POSTs the results to the receiver on the
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
import concurrent.futures
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


INGRESS, PUBLIC, PATH = "ingress (MetalLB/traefik)", "public edge (public IP/firewall/NAT)", "network path to AIP"


class NoPath(OSError):
    """TCP connect or TLS handshake failed. Traefik ends TLS, so this is the network path or traefik, never an app."""


def err(e):
    if isinstance(e, NoPath):
        return str(e)  # already the text of the real error
    return "%s: %s" % (type(e).__name__, e) if str(e) else type(e).__name__


def no_answer(e):
    """Which part gave no HTTP answer: "path" (connect/TLS), "read" (request sent, no reply), None (a bad certificate)."""
    return "path" if isinstance(e, NoPath) else None if isinstance(e, ssl.SSLCertVerificationError) else "read"


def pmap(fn, items):
    """fn over items at the same time. One by one, a dead path costs a full timeout per check: the round then takes
    minutes and the receiver calls the probe silent."""
    with concurrent.futures.ThreadPoolExecutor(32) as ex:
        return list(ex.map(fn, items))


def tls_connect(host, ip=None, port=443):
    """TLS socket to ip (or host) with host as SNI, like curl --resolve. Raises NoPath when connect or handshake fails."""
    try:
        s = socket.create_connection((ip or host, port), timeout=TIMEOUT)
        try:
            return ssl.create_default_context().wrap_socket(s, server_hostname=host)
        except BaseException:
            s.close()
            raise
    except ssl.SSLCertVerificationError:
        raise  # a bad certificate is a finding of its own
    except OSError as e:
        raise NoPath(err(e)) from None


def get(url, insecure=False, headers=None, ctx=None):
    """(status, body). Raises on network/TLS failure; any HTTP status means the server answered."""
    ctx = ctx or (ssl._create_unverified_context() if insecure else ssl.create_default_context())
    req = urllib.request.Request(url, headers=dict({"User-Agent": "sakoo-infra-probe"}, **(headers or {})))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
    try:  # no proxy: checks must take the direct path into AIP, not an HTTP(S)_PROXY from the environment
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
    host as SNI and Host header (like curl --resolve). Returns (tls_ok, detail, http_status or None, no_answer(error))."""
    try:
        with tls_connect(host, ip) as t:
            days = (ssl.cert_time_to_seconds(t.getpeercert()["notAfter"]) - now()) / 86400
            t.sendall(b"GET / HTTP/1.1\r\nHost: %s\r\nUser-Agent: sakoo-infra-probe\r\nConnection: close\r\n\r\n" % host.encode())
            data = b""
            while len(data) < 1024 and b"\r\n\r\n" not in data:
                chunk = t.recv(1024)
                if not chunk:
                    break
                data += chunk
            if b"\r\n\r\n" in data and len(data.partition(b"\r\n\r\n")[2]) < 20:
                data += t.recv(256)  # body may arrive in its own packet
        head, _, body = data.partition(b"\r\n\r\n")
        line = head.split(b"\r\n")[0].split()
        code = int(line[1]) if len(line) > 1 and line[1].isdigit() else None
        if days < CERT_WARN_DAYS:
            return False, "certificate expires in %.0f days" % days, code, None
        if code == 404 and body.startswith(b"404 page not found"):  # traefik's own page: no route for this host
            return False, "traefik has no route for this host (404 page not found)", code, None
        return True, "HTTP %s, cert %.0f days left" % (code, days), code, None
    except (OSError, ssl.SSLError) as e:
        return False, err(e), None, no_answer(e)


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


def check_vpn(iface, log=None):
    """Up = the interface exists AND OpenVPN's log shows it connected after its last restart. The interface alone is
    not enough: OpenVPN keeps tun devices (persist-tun) while it reconnects, and a dead tunnel must not look like
    every AIP check failing."""
    try:
        state = Path("/sys/class/net/%s/operstate" % iface).read_text().strip()
    except OSError:
        return False, "interface %s does not exist (VPN not running?)" % iface
    if log and Path(log).exists():
        lines = Path(log).read_text(errors="replace").splitlines()[-400:]
        last_up = max((i for i, l in enumerate(lines) if "Initialization Sequence Completed" in l), default=-1)
        last_down = max((i for i, l in enumerate(lines) if "restarting" in l or "connection failed" in l), default=-1)
        if last_down > last_up:
            recent = lines[last_up + 1:last_down + 1]  # most useful reason first: connect error, auth, inactivity
            why = next((l for key in ("connect to", "AUTH", "Inactivity", "TLS Error") for l in reversed(recent) if key in l), lines[last_down])
            return False, "%s reconnecting: %s" % (iface, why[20:160])
    return state in ("up", "unknown"), "%s connected" % iface  # tun devices report "unknown" when up


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
    spaces = CFG.get("event_namespaces", [])
    if errors and len(errors) == len(spaces):  # read nothing: rule results would be false "OK"s
        return {"kubernetes events": (False, "; ".join(errors), "monitor")}
    # capacity reached = the cluster is full, nothing is down: its own owner, not "infra down"
    res = {"k8s " + name: (not h, ("%d events, e.g. %s" % (len(h), h[0])) if h else "no events",
                           "capacity" if name.endswith("capacity") else "infra") for name, h in hits.items()}
    # cannot read events: the API is checked separately, so this is a probe/RBAC problem. Reported when fine too:
    # a result that only exists while it fails never recovers, and the receiver then never alerts it again
    res["kubernetes events"] = (not errors, "; ".join(errors) or "read %d namespaces" % len(spaces), "monitor")
    return res


def fetch(url, ip=None):
    """(status, first 300 bytes of the body) over TLS to ip (or the URL's host), SNI = URL host. Raises on network errors."""
    u = urllib.parse.urlsplit(url)
    with tls_connect(u.hostname, ip, u.port or 443) as t:
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


def check_assets(net=None):
    """MLOps assets (the asset list): each must have a ready pod behind its Kubernetes Service, and, if it has an
    external URL, its app must answer through traefik (status < 500, and not traefik's fallback page).
    An asset whose URL gave no HTTP answer at all is noted in net (name -> (hostname, no_answer)) for path_check."""
    ns, assets, ip = CFG.get("assets_namespace", "aip-mlops-service"), CFG.get("assets", []), CFG.get("ingress_ip")
    server, ctx = kube()
    code, body = get("%s/api/v1/namespaces/%s/endpoints" % (server, ns), ctx=ctx)
    if code != 200:
        return {"asset list": (False, "cannot read endpoints in %s: HTTP %d" % (ns, code), "monitor")}
    ready = {e["metadata"]["name"]: sum(len(x.get("addresses", [])) for x in e.get("subsets", []))
             for e in json.loads(body)["items"]}

    def no_such_path(host):  # body of a path no app serves, to recognise a route that has disappeared
        try:
            return fetch("https://%s/sakoo-probe-no-such-path" % host, ip)[1]
        except (OSError, ssl.SSLError) as e:
            return e
    hosts = sorted({urllib.parse.urlsplit(a["url"]).hostname for a in assets if a.get("url")})
    fallback = dict(zip(hosts, pmap(no_such_path, hosts)))

    def one(a):
        problems = ["%s: no ready pod" % svc if svc in ready else "%s: no such Service" % svc
                    for svc in a.get("services", [a["name"]]) if not ready.get(svc)]
        url, tested, gone = a.get("url"), "pods", None
        if url and not problems:
            u = urllib.parse.urlsplit(url)
            try:
                if isinstance(fallback[u.hostname], Exception):
                    raise fallback[u.hostname]
                status, sample = fetch(url, ip)
                want = a.get("expect")  # health endpoint: must be 200 and contain this text
                # short form: path on mlopsapipilot, else host/path
                short = (u.path or "/") if u.hostname.startswith("mlopsapipilot.") else u.hostname.split(".")[0] + (u.path or "/")
                tested = "%s %d" % (short, status)
                if want and (status != 200 or want.encode() not in sample):
                    problems.append("%s answered HTTP %d without %r: %r" % (url, status, want, sample[:80]))
                elif status >= 500 or status == 0:
                    problems.append("%s answered HTTP %d" % (url, status))
                elif u.path not in ("", "/") and sample == fallback[u.hostname]:
                    problems.append("%s has no route (traefik serves the fallback page)" % url)
            except (OSError, ssl.SSLError) as e:
                problems.append("%s: %s" % (url, err(e)))
                gone = no_answer(e) and (u.hostname, no_answer(e), url)
        n = sum(ready.get(svc, 0) for svc in a.get("services", [a["name"]]))
        meta = {"person": a.get("person", "?"), "value": a.get("value", "?"), "test": tested, "pods": n}
        return (not problems, "; ".join(problems) or "%d ready pod(s), %s" % (n, tested), a.get("owner", "asset"), meta), gone

    res = {"asset list": (True, "%d assets" % len(assets), "monitor")}  # reported when fine too, so its alert recovers
    for a, (r, gone) in zip(assets, pmap(one, assets)):
        res[a["name"]] = r
        if gone and net is not None:
            net[a["name"]] = gone
    return res


def host_checks(res, ip, label, owner_if_all_fail):
    """Check every host over one path; one alert if all fail, else per-host results. Returns True if any answered."""
    names = CFG.get("hosts", [])
    hosts = dict(zip(names, pmap(lambda h: check_host(h, ip), names)))
    if not hosts:
        return True
    if not any(ok or code for ok, _, code, _ in hosts.values()):
        res[label] = (False, "all %d hostnames fail: %s" % (len(hosts), next(iter(hosts.values()))[1]), owner_if_all_fail)
        return False
    res[label] = (True, "%d/%d hostnames answer" % (sum(bool(c) for _, _, c, _ in hosts.values()), len(hosts)), owner_if_all_fail)
    return hosts


def answers(url, ip):
    """True if url answers over ip with a status below 500: the app is fine on that path."""
    try:
        return 0 < fetch(url, ip)[0] < 500
    except (OSError, ssl.SSLError):
        return False


def path_check(res, net, public_ip=None):
    """net: checks that got no HTTP answer through the cluster VPN, name -> (hostname, "path" | "read", url).
    With public_ip (the second VPN works), each one is asked again over the public address users take: it answers
    there = the probe's cluster path dropped it, not the app (one result for the path, none for the app); no answer
    there either = the app (or traefik) is really broken, and its failure stays. Without a second path: a failed
    connect or TLS handshake, or no answer from two or more hostnames in one round, is taken as the path."""
    if not res.get(INGRESS, (True,))[0]:  # ingress down: its own alert says it all
        for name in net:
            res.pop(name, None)
        return
    if public_ip:
        ok = dict(zip(net, pmap(lambda n: answers(net[n][2], public_ip), list(net))))
        hidden = sorted(n for n in net if ok[n])
        for n in net:
            if ok[n]:
                res.pop(n, None)
            elif n in res:
                r = res[n]
                res[n] = r[:1] + (r[1] + " (no answer over the public IP either)",) + r[2:]
    else:
        flaky = any(part == "path" for _, part, _ in net.values()) or len({h for h, _, _ in net.values()}) > 1
        hidden = sorted(net) if flaky else []
        for n in hidden:
            res.pop(n, None)
    res[PATH] = (not hidden, "%d checks get no answer through the cluster VPN (%s)%s: unstable network between the probe and "
                 "AIP, not the apps" % (len(hidden), ", ".join(hidden[:4]), " but answer over the public IP" if public_ip else "")
                 if hidden else "stable", "monitor")


def run_checks():
    if CFG.get("receiver_url"):
        sync_clock()
    res, vpn = {}, CFG.get("vpn", {})  # {"cluster": "tun0", "endpoints": "tun1"}
    for role, iface in vpn.items():
        res["probe VPN " + role] = check_vpn(iface, "/tmp/vpn-%s.log" % role) + ("monitor",)
    cluster_ok = res.get("probe VPN cluster", (True,))[0]
    public_ok = res.get("probe VPN endpoints", (True,))[0]

    tcp, http = CFG.get("tcp_checks", {}), CFG.get("http_checks", {})
    if cluster_ok:
        net = {}  # checks that got no HTTP answer, for path_check
        for name in CFG.get("dns_names", []):
            res["dns " + name] = check_dns(name) + ("infra",)
        hosts = host_checks(res, CFG.get("ingress_ip"), INGRESS, "infra")
        if isinstance(hosts, dict):
            for h, (ok, detail, code, gone) in hosts.items():
                if code and code >= 500:  # traefik answered, the app behind it did not: ours
                    ok, detail = False, "HTTP %d from the app behind traefik" % code
                res["host " + h] = (ok, detail, "infra" if "certificate" in detail else "us")
                if gone:
                    net["host " + h] = (h, gone, "https://%s/" % h)
        for name, r in zip(tcp, pmap(lambda a: check_tcp(*a), tcp.values())):
            res[name] = r + ("infra",)
        for name, c in http.items():
            res[name] = check_http(c["url"], c.get("healthy_text"), c.get("insecure", False)) + (c.get("owner", "infra"),)
        if CFG.get("kubeconfig") and res.get("kubernetes API", (True,))[0]:  # API down is already its own alert
            res.update(check_events())
            if CFG.get("assets"):
                try:
                    res.update(check_assets(net))
                except Exception as e:
                    res["asset list"] = (False, err(e), "monitor")
        if CFG.get("nodes"):
            res.update(check_nodes())
        path_check(res, net, CFG.get("public_ip") if public_ok else None)

    # the public path users take: public IP -> edge firewall/NAT -> traefik. Only meaningful if traefik itself is up,
    # or as the second opinion when nothing at all answers through the cluster VPN.
    inner = [res[n][0] for n in (INGRESS, *tcp, *http) if n in res]
    dead = len(inner) > 1 and not any(inner)
    if public_ok and CFG.get("public_ip") and (dead or res.get(INGRESS, (True,))[0]):
        host_checks(res, CFG["public_ip"], PUBLIC, "infra")
    if dead and res.get(PUBLIC, (False,))[0]:  # users reach AIP, so it is up: the probe's own path is broken, not infra
        res = {k: v for k, v in res.items() if k.startswith("probe VPN") or k == PUBLIC}
        res["probe VPN cluster"] = (False, "%s is connected but nothing answers through it (ingress, Kubernetes API, nodes), while "
                                    "the public address answers: AIP is up, the probe's VPN path is broken" % vpn.get("cluster", "VPN"), "monitor")
    return res


def report(res):
    body = json.dumps({"probe": socket.gethostname(), "ts": int(now()), "results": res}).encode()
    sig = hmac.new(os.environ["PROBE_KEY"].encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(CFG["receiver_url"], data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Signature": sig})
    urllib.request.urlopen(req, timeout=20).read()


def main():
    if "--print" in sys.argv:
        for name, r in run_checks().items():
            ok, detail, owner = r[:3]
            print("%-4s %-45s %-7s %s" % ("OK" if ok else "FAIL", name, owner, detail))
        return
    while True:
        t0 = time.time()
        try:
            res = run_checks()
            if os.environ.get("PROBE_RESULTS"):  # the ops dashboard on the same station reads this file
                tmp = Path(os.environ["PROBE_RESULTS"] + ".tmp")
                tmp.write_text(json.dumps({"ts": now(), "results": res}))
                tmp.replace(os.environ["PROBE_RESULTS"])
            report(res)
        except Exception as e:  # receiver unreachable: it raises "probe silent" on its side
            print(time.strftime("%H:%M:%S"), "report failed:", err(e), file=sys.stderr, flush=True)
        time.sleep(max(1, CFG.get("interval_s", 60) - (time.time() - t0)))


if __name__ == "__main__":
    main()
