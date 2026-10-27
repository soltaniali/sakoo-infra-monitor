#!/usr/bin/env python3
"""AIP ops collector (diagnose only). Every round it reads Kubernetes through the station's VPN (GET requests only),
keeps an inventory, records changes (feature/release, new service, new model), detects symptoms (bug, down), and turns
them into incidents with cause, owner, evidence and a suggested fix. Humans do the fix. Stdlib only.

Rounds: every ROUND_S our namespace; every SLOW_EVERY rounds all readable namespaces and our pods' logs.
Data: SQLite $OPS_DATA/ops.db (default /data). Probe results: $PROBE_RESULTS (written by probe.py on this station).
Optional integrations, each on when its env is set (secrets/ops.env):
  LITELLM_URL (…/v1), LITELLM_KEY, LITELLM_MODEL     log and incident summaries, "registered in litellm?" for models
  GITLAB_URL, GITLAB_TOKEN, GITLAB_PROJECTS            author and MR of a change (comma-separated project paths)
  GRAFANA_URL, GRAFANA_TOKEN [, GRAFANA_DATASOURCE_UID] GPU % per model pod (DCGM_FI_DEV_GPU_UTIL)
Alerts: one Telegram message per round, through the London receiver's POST /alert (HMAC with PROBE_KEY).
"""
import calendar
import concurrent.futures
import contextlib
import hashlib
import hmac
import json
import os
import queue
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import probe

OUR_NS = os.environ.get("OPS_OUR_NS", "aip-mlops-service")
DATA = Path(os.environ.get("OPS_DATA", "/data"))
OPS_URL = os.environ.get("OPS_URL", "")  # dashboard link in alerts
ROUND_S, SLOW_EVERY, WORKERS = 60, 5, 8
CHANGE_WINDOW = 1800   # a symptom within 30 min after a change in the same object is linked to it
RELEASE_WATCH = 900    # a release is judged healthy/bad 15 min after it
DOWN_GRACE, PENDING_GRACE, MODEL_GRACE = 180, 300, 1800
FLAP_RESTARTS, FLAP_WINDOW = 3, 900
CLEAN_TO_CLOSE, ERROR_QUIET_CLOSE = 2, 1800
LOG_BASELINE_S = 3600  # first hour: learn the error signatures that already exist, open no incidents for them
RETENTION = 30 * 86400
BUG = ("crashloop", "oomkilled", "flapping", "new-error", "probe-failing")
DOWN = ("down", "model-down", "model-test", "pending", "image-pull", "endpoint", "infra")
# a log LEVEL, not a word in the message: payloads (prompts, papers) contain "error" and "critical" as plain words
ERROR_RE = re.compile(r'\b(ERROR|CRITICAL|FATAL|PANIC)\b|(?i:level=(error|fatal|critical))|(?i:"level"\s*:\s*"(error|fatal|critical)")'
                      r'|^Traceback \(most recent call last\)|^\s*[A-Za-z_.]+(Error|Exception): ')
LEVEL_ZONE = 160  # only the start of a line holds its level; the rest is the message
probe.TIMEOUT = 25  # the VPN path is slow: 8 s timed out on big lists

# symptom: (cause, suggested fix, playbook section)
RULES = {
    "crashloop": ("the container exits right after it starts", "read the previous container's log below and fix that error",
                  "Pipeline A: service is down"),
    "oomkilled": ("the container hit its memory limit (OOMKilled)", "raise the memory limit (now {limit}) or fix the memory leak",
                  "Pipeline B: service is up but broken"),
    "image-pull": ("the image cannot be pulled", "check the image tag and the pull secret", "Pipeline A: service is down"),
    "pending": ("the pod cannot be scheduled or started", "read the event below", "Pipeline A: service is down"),
    "flapping": ("the container restarts again and again", "read the previous container's log; look for OOM or a failing dependency",
                 "Pipeline B: service is up but broken"),
    "probe-failing": ("the readiness/liveness probe fails", "check the app log, then the probe path and timeout",
                      "Pipeline B: service is up but broken"),
    "down": ("fewer ready replicas than wanted, no pod-level reason found", "look at the pods and events below",
             "Pipeline A: service is down"),
    "model-down": ("the model has fewer ready replicas than minReplicas", "look at the model pod and events below",
                   "Pipeline A: service is down"),
    "new-error": ("a new error message appeared in the log", "read the sample; if it started after a release, roll back",
                  "Pipeline B: service is up but broken"),
    "endpoint": ("a probe check of our endpoint or asset fails", "check the service behind it (pods, logs)",
                 "Pipeline B: service is up but broken"),
    "model-test": ("a test call to the model through litellm fails twice in a row",
                   "check the model pod and its log, then the litellm route for this model", "Pipeline B: service is up but broken"),
    "infra": ("an infra check fails", "send the message below to the infra team", "Infra or us? Decide the owner first"),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS inventory (key TEXT PRIMARY KEY, ns TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS changes (id INTEGER PRIMARY KEY, ts REAL, ns TEXT, kind TEXT, name TEXT, type TEXT,
  before TEXT, after TEXT, info TEXT, verdict TEXT);
CREATE TABLE IF NOT EXISTS incidents (id INTEGER PRIMARY KEY, key TEXT, scope TEXT, state TEXT, ns TEXT, obj TEXT,
  symptom TEXT, owner TEXT, cause TEXT, fix TEXT, playbook TEXT, detail TEXT, evidence TEXT, change_id INTEGER,
  opened REAL, last_seen REAL, closed REAL, clean INTEGER, alerted INTEGER, summary TEXT);
CREATE TABLE IF NOT EXISTS signatures (sig TEXT PRIMARY KEY, ns TEXT, workload TEXT, first REAL, last REAL,
  count INTEGER, sample TEXT);
CREATE TABLE IF NOT EXISTS restarts (day TEXT, ns TEXT, wl TEXT, first INTEGER, last INTEGER, PRIMARY KEY (day, ns, wl));
CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, ts REAL, text TEXT, sent REAL);
CREATE INDEX IF NOT EXISTS changes_ts ON changes (ts);
CREATE INDEX IF NOT EXISTS incidents_state ON incidents (state, scope);
"""


class KubeError(Exception):
    @property
    def code(self):
        return self.args[1]


def kget(path, raw=False):
    """GET one Kubernetes API path: the only verb this dashboard uses. JSON, or text with raw=True. Retries timeouts."""
    server, ctx = probe.kube()
    last = "?"
    for _ in range(3):
        try:
            code, body = probe.get(server + path, ctx=ctx)
        except OSError as e:  # timeouts, TLS and URL errors are all OSError
            last = probe.err(e)
            time.sleep(2)
            continue
        if code == 204:  # e.g. the log of a container that never started
            return "" if raw else {"items": []}
        if code != 200:
            raise KubeError("HTTP %s %s" % (code, path.split("?")[0]), code)
        return body.decode(errors="replace") if raw else json.loads(body)
    raise KubeError("%s %s" % (last, path.split("?")[0]), None)


def http_json(method, url, headers=None, body=None, timeout=30):
    """JSON request to an integration (GitLab, Grafana, litellm), never through a proxy."""
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), method=method,
                                 headers=dict({"Content-Type": "application/json", "User-Agent": "sakoo-ops"}, **(headers or {})))
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as r:
        return json.loads(r.read())


def ts(s):
    return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")) if s else None


def digest(o):
    return hashlib.sha1(json.dumps(o, sort_keys=True).encode()).hexdigest()[:12]


def short_image(image):
    return image.rsplit("/", 1)[-1]


def owner_of(ns, obj):
    """us = MLOps; infra = infra team; otherwise the team or user that owns the namespace."""
    if ns == OUR_NS or (ns.startswith("user-") and obj.endswith("clearml-agent")):  # per-user ClearML agents are ours
        return "us"
    return ns


def alertable(ns, owner):
    return not ns.startswith("user-") or owner == "us"  # users' own workloads: dashboard only


def signature(line):
    """Same error with other ids, numbers or times = same signature."""
    s = re.sub(r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>", line)
    s = re.sub(r"(?i)\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", "<hex>", s)
    s = re.sub(r"\d+(\.\d+)*", "<n>", s)
    s = re.sub(r"\s+", " ", s).strip()[:200]
    return hashlib.sha1(s.encode()).hexdigest()[:16]


# --- object summaries (what we keep from each Kubernetes object) --------------------------------------------------
def workload_of(p):
    md = p["metadata"]
    model = md.get("labels", {}).get("model")
    if model and md["name"].startswith("model-"):  # KubeAI model pod
        return model
    name = (md.get("ownerReferences") or [{}])[0].get("name") or md["name"]
    h = md.get("labels", {}).get("pod-template-hash")
    return name[:-len(h) - 1] if h and name.endswith("-" + h) else name


def qty(v):
    """Kubernetes quantity -> number (CPU in cores, memory in bytes). '500m' -> 0.5, '1Gi' -> 1073741824."""
    if v in (None, ""):
        return 0.0
    v = str(v)
    units = {"m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4,
             "n": 1e-9, "u": 1e-6}
    for u in sorted(units, key=len, reverse=True):
        if v.endswith(u):
            return float(v[:-len(u)]) * units[u]
    return float(v)


def resources(containers):
    """Summed requests/limits of a pod's containers; GPUs as {resource name: count} (nvidia.com/gpu, MIG slices)."""
    r = {"cpu_req": 0.0, "mem_req": 0.0, "mem_lim": 0.0, "gpu": {}}
    for c in containers:
        res = c.get("resources", {})
        req, lim = res.get("requests", {}), res.get("limits", {})
        r["cpu_req"] += qty(req.get("cpu"))
        r["mem_req"] += qty(req.get("memory"))
        r["mem_lim"] += qty(lim.get("memory"))
        for k, v in {**req, **lim}.items():
            if k.startswith("nvidia.com/"):
                r["gpu"][k] = max(r["gpu"].get(k, 0), int(qty(v)))
    return r


def sum_pod(p):
    st, spec = p.get("status", {}), p.get("spec", {})
    css = st.get("initContainerStatuses", []) + st.get("containerStatuses", [])
    waiting = [c["state"]["waiting"].get("reason", "") for c in css if "waiting" in c.get("state", {})]
    last = [[c["lastState"]["terminated"].get("reason", ""), ts(c["lastState"]["terminated"].get("finishedAt")), c["name"]]
            for c in css if "terminated" in c.get("lastState", {})]
    init_running = [c["name"] for c in st.get("initContainerStatuses", []) if "running" in c.get("state", {})]
    bad = (init_running or [None])[0] or next((c["name"] for c in css if "waiting" in c.get("state", {}) or c.get("restartCount")), None)
    containers = [c["name"] for c in spec.get("initContainers", []) + spec.get("containers", [])]
    sched = next((c.get("message", "") for c in st.get("conditions", []) if c["type"] == "PodScheduled" and c["status"] == "False"), "")
    return {"name": p["metadata"]["name"], "workload": workload_of(p), "phase": st.get("phase"),
            "ready": any(c["type"] == "Ready" and c["status"] == "True" for c in st.get("conditions", [])),
            "restarts": sum(c.get("restartCount", 0) for c in st.get("containerStatuses", [])),
            "node": spec.get("nodeName"), "created": ts(p["metadata"].get("creationTimestamp")), "started": ts(st.get("startTime")),
            "waiting": waiting, "last": last, "sched": sched, "init_running": init_running, "containers": containers, "bad": bad if bad in containers else (containers or [None])[0],
            "mem": {c["name"]: c.get("resources", {}).get("limits", {}).get("memory") for c in spec.get("containers", [])},
            "res": resources(spec.get("containers", []))}


def gaps(containers):
    g = []
    if any(not c.get("readinessProbe") for c in containers):
        g.append("no readiness probe")
    if any(not c.get("resources", {}).get("limits", {}).get("memory") for c in containers):
        g.append("no memory limit")
    if any("@" not in short_image(c["image"]) and short_image(c["image"]).partition(":")[2] in ("", "latest") for c in containers):
        g.append("image tag latest or missing")
    return g


def sum_workload(o, kind):
    st = o.get("status", {})
    tpl = o["spec"]["jobTemplate"]["spec"]["template"] if kind == "CronJob" else o["spec"]["template"]
    cs = tpl.get("spec", {}).get("containers", [])
    desired, ready = {"DaemonSet": (st.get("desiredNumberScheduled", 0), st.get("numberReady", 0)),
                      "CronJob": (0, 0)}.get(kind, (o["spec"].get("replicas", 1), st.get("readyReplicas", 0)))  # a CronJob runs now and then
    return {"kind": kind, "name": o["metadata"]["name"], "desired": desired, "ready": ready,
            "images": [short_image(c["image"]) for c in cs], "tpl": digest(tpl), "created": ts(o["metadata"].get("creationTimestamp")),
            "gaps": gaps(cs)}


def sum_model(m):
    s, st = m.get("spec", {}), m.get("status", {})
    return {"kind": "Model", "name": m["metadata"]["name"], "engine": s.get("engine"), "url": s.get("url"),
            "profile": s.get("resourceProfile"), "min": s.get("minReplicas", 0), "max": s.get("maxReplicas"),
            "ready": st.get("replicas", {}).get("ready", 0), "all": st.get("replicas", {}).get("all", 0),
            "created": ts(m["metadata"].get("creationTimestamp")), "spec": digest({k: v for k, v in s.items() if k != "replicas"})}


def sum_ingress(i):
    return {"kind": "Ingress", "name": i["metadata"]["name"], "hosts": sorted({r["host"] for r in i["spec"].get("rules", []) if r.get("host")}),
            "tls": bool(i["spec"].get("tls"))}


def sum_event(e):
    o = e.get("involvedObject", {})
    return {"kind": o.get("kind"), "name": o.get("name"), "reason": e.get("reason"), "msg": (e.get("message") or "")[:400],
            "ts": ts(e.get("lastTimestamp") or e.get("eventTime") or e["metadata"].get("creationTimestamp")), "count": e.get("count", 1)}


class Collector:
    def __init__(self, db_path=None):
        DATA.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path or DATA / "ops.db"), check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.n = 0
        self.pods, self.events, self.inv, self.read_at = {}, {}, {}, {}  # ns -> latest read
        self.namespaces, self.forbidden, self.hosts = [], {}, {}
        self.since, self.restart_hist = {}, {}
        self.alerts, self.probe, self.probe_seen = [], {}, None
        self.status = {"last": None, "took": None, "errors": [], "slow_last": None}
        self.integ_err, self.cache = {}, {}
        self.model_tests, self.test_fails = {}, {}  # model -> {ok, ms, detail, ts, route}
        self.bg = queue.Queue()
        threading.Thread(target=self._bg_loop, daemon=True).start()

    # --- database helpers -----------------------------------------------------------------------------------------
    def rows(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args)]

    def one(self, sql, args=()):
        r = self.rows(sql, args)
        return r[0] if r else None

    def exec(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    def meta(self, k):
        r = self.one("SELECT v FROM meta WHERE k=?", (k,))
        return r and r["v"]

    def set_meta(self, k, v):
        self.exec("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, v))

    # --- integrations ---------------------------------------------------------------------------------------------
    @staticmethod
    def integrations():
        e = os.environ.get
        return {"litellm": bool(e("LITELLM_URL") and e("LITELLM_KEY") and e("LITELLM_MODEL")),
                "gitlab": bool(e("GITLAB_URL") and e("GITLAB_TOKEN") and e("GITLAB_PROJECTS")),
                "grafana": bool(e("GRAFANA_URL") and e("GRAFANA_TOKEN"))}

    def cached(self, key, ttl, fn):
        hit = self.cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            val = fn()
            self.integ_err.pop(key, None)
        except Exception as e:
            self.integ_err[key] = probe.err(e)
            val = None
        self.cache[key] = (time.time(), val)
        return val

    def llm(self, text):
        url = os.environ["LITELLM_URL"].rstrip("/") + "/chat/completions"
        r = http_json("POST", url, {"Authorization": "Bearer " + os.environ["LITELLM_KEY"]}, {
            "model": os.environ["LITELLM_MODEL"], "temperature": 0.2, "max_tokens": 350, "messages": [
                {"role": "system", "content": "You help the on-call MLOps engineer of AIP, a Kubernetes AI platform. Reply in at most "
                                              "5 short sentences, plain text: what failed, the most likely cause, the next step."},
                {"role": "user", "content": text[-6000:]}]}, timeout=120)
        return r["choices"][0]["message"]["content"].strip()

    def litellm_models(self):
        url = os.environ["LITELLM_URL"].rstrip("/") + "/models"
        return self.cached("litellm", 300, lambda: [m["id"] for m in http_json(
            "GET", url, {"Authorization": "Bearer " + os.environ["LITELLM_KEY"]})["data"]])

    def gpu_util(self):
        """{pod name: GPU utilization %} from Grafana's Prometheus (DCGM exporter)."""
        def fetch():
            g, auth = os.environ["GRAFANA_URL"].rstrip("/"), {"Authorization": "Bearer " + os.environ["GRAFANA_TOKEN"]}
            uid = os.environ.get("GRAFANA_DATASOURCE_UID")
            if not uid:
                try:
                    ds = http_json("GET", g + "/api/datasources", auth)
                except urllib.error.HTTPError as e:
                    if e.code != 403:
                        raise
                    ds = list(http_json("GET", g + "/api/frontend/settings", auth)["datasources"].values())  # Viewer tokens
                uid = next(d["uid"] for d in ds if d.get("type") == "prometheus")
            q = urllib.parse.quote("DCGM_FI_DEV_GPU_UTIL")
            res = http_json("GET", "%s/api/datasources/proxy/uid/%s/api/v1/query?query=%s" % (g, uid, q), auth)["data"]["result"]
            out = {}
            for s in res:
                pod = s["metric"].get("exported_pod") or s["metric"].get("pod")
                out.setdefault(pod, []).append(float(s["value"][1]))
            return {p: sum(v) / len(v) for p, v in out.items() if p}
        return self.cached("grafana", 60, fetch) or {}

    def gitlab_enrich(self, change_id):
        """Author and MR of a change: commits in the 30 min before it whose diff mentions the new image tag or the name."""
        c = self.one("SELECT * FROM changes WHERE id=?", (change_id,))
        g, auth = os.environ["GITLAB_URL"].rstrip("/") + "/api/v4", {"PRIVATE-TOKEN": os.environ["GITLAB_TOKEN"]}
        iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
        needles = [x for x in (c["after"] or "").split(",") if x] + [c["name"]]
        info, window = json.loads(c["info"] or "{}"), []
        for proj in [p.strip() for p in os.environ["GITLAB_PROJECTS"].split(",") if p.strip()]:
            pid = urllib.parse.quote(proj, safe="")
            for cm in http_json("GET", "%s/projects/%s/repository/commits?since=%s&until=%s&per_page=20"
                                % (g, pid, iso(c["ts"] - CHANGE_WINDOW), iso(c["ts"] + 60)), auth):
                window.append("%s: %s" % (cm["author_name"], cm["title"]))
                diff = http_json("GET", "%s/projects/%s/repository/commits/%s/diff?per_page=100" % (g, pid, cm["id"]), auth)
                if any(n in d.get("diff", "") + d.get("new_path", "") for d in diff for n in needles):
                    info.update(author=cm["author_name"], commit=cm["title"], commit_url=cm.get("web_url"))
                    mrs = http_json("GET", "%s/projects/%s/repository/commits/%s/merge_requests" % (g, pid, cm["id"]), auth)
                    if mrs:
                        info.update(mr_url=mrs[0]["web_url"], mr_title=mrs[0]["title"], mr_author=mrs[0]["author"]["name"])
                    break
            if "author" in info:
                break
        if "author" not in info:
            info["commits_in_window"] = window[:5]
        self.exec("UPDATE changes SET info=? WHERE id=?", (json.dumps(info), change_id))

    def _bg_loop(self):
        """Slow integration calls (GitLab, LLM) off the collector loop, one at a time."""
        while True:
            kind, arg = self.bg.get()
            try:
                if kind == "gitlab":
                    self.gitlab_enrich(arg)
                elif kind == "litellm":
                    self.exec("UPDATE incidents SET summary=? WHERE id=?", (self.llm(self.incident_text(arg)), arg))
                self.integ_err.pop(kind, None)
            except Exception as e:
                self.integ_err[kind] = probe.err(e)

    # --- reading Kubernetes ---------------------------------------------------------------------------------------
    def read_ns(self, ns):
        """Snapshot of one namespace; None if we may not read it; an error string if reading failed."""
        def opt(path):
            try:
                return kget(path)["items"]
            except KubeError as e:
                if e.code == 403:
                    return []
                raise
        try:
            snap = {"pods": kget("/api/v1/namespaces/%s/pods" % ns)["items"],
                    "deploy": opt("/apis/apps/v1/namespaces/%s/deployments" % ns),
                    "sts": opt("/apis/apps/v1/namespaces/%s/statefulsets" % ns),
                    "ds": opt("/apis/apps/v1/namespaces/%s/daemonsets" % ns),
                    "cj": opt("/apis/batch/v1/namespaces/%s/cronjobs" % ns),
                    "cm": opt("/api/v1/namespaces/%s/configmaps" % ns),
                    "events": opt("/api/v1/namespaces/%s/events?fieldSelector=type%%3DWarning" % ns),
                    "ing": opt("/apis/networking.k8s.io/v1/namespaces/%s/ingresses" % ns),
                    "models": opt("/apis/kubeai.org/v1/namespaces/%s/models" % ns) if ns == OUR_NS else [],
                    "usage": opt("/apis/metrics.k8s.io/v1beta1/namespaces/%s/pods" % ns)}
            return snap
        except KubeError as e:
            if e.code == 403:
                self.forbidden[ns] = time.time()
                return None
            return "%s: %s" % (ns, e.args[0])
        except Exception as e:
            return "%s: %s" % (ns, probe.err(e))

    def refresh_namespaces(self, t):
        names = sorted(i["metadata"]["name"] for i in kget("/api/v1/namespaces")["items"])
        known = json.loads(self.meta("namespaces") or "null")
        if known is not None:
            for ns in sorted(set(names) - set(known)):
                self.add_change(t, ns, "Namespace", ns, "new-namespace", "", ns, {})
            for ns in sorted(set(known) - set(names)):
                self.add_change(t, ns, "Namespace", ns, "removed-namespace", ns, "", {})
                self.exec("DELETE FROM inventory WHERE ns=?", (ns,))
                for d in (self.pods, self.events, self.inv):
                    d.pop(ns, None)
        self.set_meta("namespaces", json.dumps(names))
        self.namespaces = names

    # --- one round ------------------------------------------------------------------------------------------------
    def run_round(self):
        t = time.time()
        self.n += 1
        slow, errors = (self.n - 1) % SLOW_EVERY == 0, []
        if slow or not self.namespaces:
            try:
                self.refresh_namespaces(t)
            except Exception as e:
                errors.append("namespaces: " + probe.err(e))
        targets = [OUR_NS] + ([n for n in self.namespaces if n != OUR_NS and t - self.forbidden.get(n, 0) > 3600] if slow else [])
        with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
            snaps = list(ex.map(self.read_ns, targets))
        for ns, snap in zip(targets, snaps):
            if isinstance(snap, str):
                errors.append(snap)
            elif snap is not None:
                try:
                    self.apply_ns(ns, snap, t)
                except Exception as e:
                    errors.append("%s: %s" % (ns, probe.err(e)))
        self.hosts = {h: ns for ns, inv in self.inv.items() for k, v in inv.items() if v["kind"] == "Ingress" for h in v["hosts"]}
        for step in ([self.check_probe] + ([self.scan_logs, self.test_models] if slow else []) + [self.judge_changes]):
            try:
                step(t)
            except Exception as e:
                errors.append("%s: %s" % (step.__name__, probe.err(e)))
        self.flush_alerts()
        if t - float(self.meta("pruned") or 0) > 86400:
            self.prune(t)
        self.status.update(last=t, took=round(time.time() - t, 1), errors=errors[:20], namespaces=len(self.inv),
                           slow_last=t if slow else self.status["slow_last"])

    def run_forever(self):
        while True:
            t0 = time.time()
            try:
                self.run_round()
            except Exception as e:
                self.status["errors"] = ["round: " + probe.err(e)]
                print("[ops] round failed:", probe.err(e), flush=True)
            time.sleep(max(5, ROUND_S - (time.time() - t0)))

    def apply_ns(self, ns, snap, t):
        pods = [sum_pod(p) for p in snap["pods"]]
        events = sorted((e for e in map(sum_event, snap["events"]) if e["ts"] and t - e["ts"] < 3600), key=lambda e: e["ts"])
        inv = {}
        for kind, items in (("Deployment", snap["deploy"]), ("StatefulSet", snap["sts"]), ("DaemonSet", snap.get("ds", [])),
                            ("CronJob", snap.get("cj", []))):
            for o in items:
                inv["%s/%s/%s" % (kind, ns, o["metadata"]["name"])] = sum_workload(o, kind)
        for c in snap.get("cm", []):  # config changes that do not touch a pod template
            name = c["metadata"]["name"]
            if name == "kube-root-ca.crt" or name.endswith(("-lock", "-leader", "leader-election", "-state", "-status", "-heartbeat")):
                continue
            inv["ConfigMap/%s/%s" % (ns, name)] = {"kind": "ConfigMap", "name": name, "data": digest([c.get("data"), c.get("binaryData")])}
        for i in snap["ing"]:
            inv["Ingress/%s/%s" % (ns, i["metadata"]["name"])] = sum_ingress(i)
        for m in snap["models"]:
            inv["Model/%s/%s" % (ns, m["metadata"]["name"])] = sum_model(m)
        self.diff(ns, inv, t)
        use = {u["metadata"]["name"]: (sum(qty(c.get("usage", {}).get("cpu")) for c in u.get("containers", [])),
                                       sum(qty(c.get("usage", {}).get("memory")) for c in u.get("containers", [])))
               for u in snap.get("usage", [])}
        for p in pods:
            p["use_cpu"], p["use_mem"] = use.get(p["name"], (None, None))
        self.record_restarts(ns, pods, t)
        self.pods[ns], self.events[ns], self.inv[ns], self.read_at[ns] = pods, events, inv, t
        self.detect(ns, pods, events, inv, t)

    # --- changes (events 3, 4, 5) ---------------------------------------------------------------------------------
    def add_change(self, t, ns, kind, name, typ, before, after, info):
        watch = typ in ("release", "config", "new-service", "new-model")
        cid = self.exec("INSERT INTO changes (ts, ns, kind, name, type, before, after, info, verdict) VALUES (?,?,?,?,?,?,?,?,?)",
                        (t, ns, kind, name, typ, before, after, json.dumps(info), "watching" if watch else ""))
        if alertable(ns, owner_of(ns, name)) and typ != "new-namespace" or (typ == "new-namespace" and not ns.startswith("user-")):
            label = {"release": "RELEASE", "config": "CONFIG CHANGE", "new-service": "NEW SERVICE", "new-host": "NEW HOSTNAME",
                     "new-model": "NEW MODEL", "model-changed": "MODEL CHANGED", "removed": "REMOVED", "model-removed": "MODEL REMOVED",
                     "new-namespace": "NEW NAMESPACE", "removed-namespace": "NAMESPACE REMOVED", "configmap": "CONFIGMAP CHANGED"}.get(typ, typ.upper())
            what = "%s → %s" % (before, after) if before and after else (after or before)
            extra = (" · gaps: " + ", ".join(info["gaps"])) if info.get("gaps") else ""
            self.alerts.append("🆕 %s %s/%s: %s%s" % (label, ns, name, what, extra))
        if self.integrations()["gitlab"] and typ not in ("removed-namespace", "removed", "model-removed"):
            self.bg.put(("gitlab", cid))
        return cid

    def diff(self, ns, new, t):
        """Compare with the stored inventory of this namespace. The first read of a namespace is the baseline."""
        old = {r["key"]: json.loads(r["data"]) for r in self.rows("SELECT key, data FROM inventory WHERE ns=?", (ns,))}
        if self.meta("seen:" + ns):
            fresh_kinds = () if self.meta("seen2:" + ns) else ("DaemonSet", "CronJob")  # kinds added later: baseline once
            for key in sorted(new.keys() - old.keys()):
                o = new[key]
                if o["kind"] in fresh_kinds or o["kind"] == "ConfigMap":  # a new ConfigMap comes with its service
                    continue
                if o["kind"] == "Model":
                    self.add_change(t, ns, "Model", o["name"], "new-model", "", "%s %s" % (o["engine"], o["profile"]), {"url": o["url"]})
                elif o["kind"] == "Ingress":
                    self.add_change(t, ns, "Ingress", o["name"], "new-host", "", ", ".join(o["hosts"]), {"tls": o["tls"]})
                else:
                    self.add_change(t, ns, o["kind"], o["name"], "new-service", "", ",".join(o["images"]), {"gaps": o["gaps"]})
            for key in sorted(old.keys() - new.keys()):
                o = old[key]
                if o["kind"] == "ConfigMap":
                    continue
                self.add_change(t, ns, o["kind"], o["name"], "model-removed" if o["kind"] == "Model" else "removed", o["name"], "", {})
            for key in sorted(new.keys() & old.keys()):
                a, b = old[key], new[key]
                if b["kind"] == "Model" and a["spec"] != b["spec"]:
                    fields = [f for f in ("engine", "url", "profile", "min", "max") if a.get(f) != b.get(f)]
                    self.add_change(t, ns, "Model", b["name"], "model-changed", "; ".join("%s %s" % (f, a.get(f)) for f in fields),
                                    "; ".join("%s %s" % (f, b.get(f)) for f in fields), {})
                elif b["kind"] == "Ingress" and set(b["hosts"]) - set(a["hosts"]):
                    self.add_change(t, ns, "Ingress", b["name"], "new-host", "", ", ".join(sorted(set(b["hosts"]) - set(a["hosts"]))), {})
                elif b["kind"] == "ConfigMap":
                    busy = self.one("SELECT count(*) n FROM changes WHERE ns=? AND name=? AND type='configmap' AND ts>?",
                                    (ns, b["name"], t - 3600))["n"] >= 3  # rewritten all the time by a program: state, not config
                    if a["data"] != b["data"] and not busy:
                        self.add_change(t, ns, "ConfigMap", b["name"], "configmap", "data " + a["data"], "data " + b["data"], {})
                elif b["kind"] in ("Deployment", "StatefulSet", "DaemonSet", "CronJob"):
                    if a["images"] != b["images"]:
                        self.add_change(t, ns, b["kind"], b["name"], "release", ",".join(a["images"]), ",".join(b["images"]), {})
                    elif a["tpl"] != b["tpl"]:
                        self.add_change(t, ns, b["kind"], b["name"], "config", "template " + a["tpl"], "template " + b["tpl"], {})
        with self.lock:
            self.conn.execute("DELETE FROM inventory WHERE ns=?", (ns,))
            self.conn.executemany("INSERT INTO inventory VALUES (?, ?, ?)", [(k, ns, json.dumps(v)) for k, v in new.items()])
            self.conn.commit()
        self.set_meta("seen:" + ns, "1")
        self.set_meta("seen2:" + ns, "1")

    def judge_changes(self, t):
        for c in self.rows("SELECT * FROM changes WHERE verdict='watching'"):
            if c["type"] == "new-model":
                m = self.inv.get(c["ns"], {}).get("Model/%s/%s" % (c["ns"], c["name"]))
                verdict = "ready" if m and m["ready"] else ("not ready" if t - c["ts"] > MODEL_GRACE else None)
            elif t - c["ts"] >= RELEASE_WATCH:
                bad = self.one("SELECT id FROM incidents WHERE ns=? AND obj=? AND opened>=? ORDER BY id LIMIT 1", (c["ns"], c["name"], c["ts"]))
                verdict = "bad" if bad else "healthy"
                if bad and alertable(c["ns"], owner_of(c["ns"], c["name"])):
                    what = "%s → %s" % (c["before"], c["after"]) if c["before"] else c["after"]
                    self.alerts.append("⚠️ BAD %s %s/%s: %s (incident #%d)" % (c["type"].upper(), c["ns"], c["name"], what, bad["id"]))
            else:
                verdict = None
            if verdict:
                self.exec("UPDATE changes SET verdict=? WHERE id=?", (verdict, c["id"]))

    # --- symptoms (events 1, 2) -----------------------------------------------------------------------------------
    def detect(self, ns, pods, events, inv, t):
        sym = {}

        def add(symptom, obj, detail, **ctx):
            sym.setdefault("%s|%s|%s|%s" % (ns, symptom, ns, obj), (symptom, ns, obj, detail, ctx))

        by_pod = {}
        for e in events:
            by_pod.setdefault(e["name"], []).append(e)
        live = set()
        for p in pods:
            live.add(p["name"])
            if p["phase"] in ("Succeeded", "Failed"):
                continue
            wl, w, pev = p["workload"], set(p["waiting"]), by_pod.get(p["name"], [])
            hist = self.restart_hist.setdefault((ns, p["name"]), [])
            hist.append((t, p["restarts"]))
            hist[:] = [x for x in hist if t - x[0] <= FLAP_WINDOW]
            crashed_lately = p["restarts"] >= 5 and any(f and t - f < 600 for _, f, _ in p["last"])  # between two crashes
            if "CrashLoopBackOff" in w or (crashed_lately and not p["ready"]):
                add("crashloop", wl, "pod %s in CrashLoopBackOff, %d restarts" % (p["name"], p["restarts"]), pod=p["name"])
            elif w & {"ImagePullBackOff", "ErrImagePull"}:
                msg = next((e["msg"] for e in reversed(pev) if e["reason"] in ("Failed", "BackOff")), "")
                add("image-pull", wl, "pod %s: %s" % (p["name"], msg or "/".join(w)), pod=p["name"], event=msg)
            elif any(r == "OOMKilled" and f and t - f < CHANGE_WINDOW for r, f, _ in p["last"]):
                c = next(c for r, f, c in p["last"] if r == "OOMKilled")
                add("oomkilled", wl, "pod %s container %s was OOMKilled, %d restarts" % (p["name"], c, p["restarts"]),
                    pod=p["name"], limit=p["mem"].get(c))
            elif p["phase"] == "Pending" and t - (p["created"] or t) > PENDING_GRACE:
                e = next((e for e in reversed(pev) if e["reason"] in ("FailedScheduling", "FailedMount", "FailedAttachVolume")), None)
                e = e or ({"reason": "FailedScheduling", "msg": p["sched"]} if p["sched"] else None)  # events expire after 1 h
                e = e or ({"reason": "Init", "msg": "init container %s has not finished" % p["init_running"][0]} if p["init_running"] else None)
                add("pending", wl, "pod %s Pending for %d min%s" % (p["name"], (t - p["created"]) // 60, ": " + e["msg"] if e else ""),
                    pod=p["name"], event=e["msg"] if e else "", reason=e["reason"] if e else "")
            elif hist[-1][1] - hist[0][1] >= FLAP_RESTARTS:
                add("flapping", wl, "pod %s restarted %d times in %d min" % (p["name"], hist[-1][1] - hist[0][1], FLAP_WINDOW // 60), pod=p["name"])
            elif (p["phase"] == "Running" and not p["ready"] and t - (p["started"] or t) > DOWN_GRACE
                  and hist[-1][1] == hist[0][1] and any(e["reason"] == "Unhealthy" for e in pev)):
                msg = next(e["msg"] for e in reversed(pev) if e["reason"] == "Unhealthy")
                add("probe-failing", wl, "pod %s not ready: %s" % (p["name"], msg), pod=p["name"], event=msg)
        for k in [k for k in self.restart_hist if k[0] == ns and k[1] not in live]:
            del self.restart_hist[k]
        has = {s[2] for s in sym.values()}
        for key, o in inv.items():
            short = None
            if o["kind"] in ("Deployment", "StatefulSet", "DaemonSet", "CronJob") and o["desired"] and o["ready"] < o["desired"]:
                short, grace = "down", DOWN_GRACE
                detail = "%d/%d replicas ready" % (o["ready"], o["desired"])
            elif o["kind"] == "Model" and o["min"] and o["ready"] < o["min"]:
                short = "model-down"
                grace = MODEL_GRACE if t - (o["created"] or 0) < MODEL_GRACE else DOWN_GRACE
                detail = "%d/%d replicas ready (minReplicas %d)" % (o["ready"], o["min"], o["min"])
            if not short:
                self.since.pop(key, None)
                continue
            since = self.since.setdefault(key, t)
            if t - since >= grace and o["name"] not in has:  # a pod-level symptom already explains it
                add(short, o["name"], "%s for %d min" % (detail, (t - since) // 60))
        self.observe(ns, sym, t)

    def check_probe(self, t):
        """Failing checks of the probe on this station. London alerts them already, so no Telegram here."""
        f = Path(os.environ.get("PROBE_RESULTS", str(DATA / "probe-results.json")))
        if not f.exists():
            return
        data = json.loads(f.read_text())
        if t - data["ts"] > 300 or data["ts"] == self.probe_seen:  # stale, or the same report as last round
            return
        self.probe, self.probe_seen = data, data["ts"]
        sym = {}
        for name, r in data["results"].items():
            ok, detail, owner = r[:3]
            if ok or owner == "monitor":
                continue
            symptom = "infra" if owner == "infra" else "endpoint"
            ns, obj = "-", name
            if name in {o["name"] for o in self.inv.get(OUR_NS, {}).values()}:
                ns = OUR_NS
            elif name.startswith("host ") and name[5:] in self.hosts:
                ns = self.hosts[name[5:]]
            sym["probe|%s|%s|%s" % (symptom, ns, obj)] = (symptom, ns, obj, detail, {"meta": r[3] if len(r) > 3 else {}})
        self.observe("probe", sym, t, alert=False)

    def scan_logs(self, t):
        """New error signatures in the logs of every readable namespace (last SLOW_EVERY rounds of log)."""
        if self.meta("logs_since") and not self.meta("logs_since:" + OUR_NS):  # learned before per-namespace learning
            self.set_meta("logs_since:" + OUR_NS, self.meta("logs_since"))
        learning = {}
        for ns in list(self.pods):
            since = float(self.meta("logs_since:" + ns) or 0) or t
            self.set_meta("logs_since:" + ns, str(since))
            learning[ns] = t - since < LOG_BASELINE_S
        open_keys = {r["key"] for r in self.rows("SELECT key FROM incidents WHERE state='open' AND scope='logs'")}
        jobs = [(ns, p, c) for ns, pods in list(self.pods.items()) for p in pods if p["phase"] == "Running" for c in p["containers"]]

        def read(job):
            ns, p, c = job
            try:
                return job, kget("/api/v1/namespaces/%s/pods/%s/log?container=%s&sinceSeconds=%d&tailLines=500"
                                 % (ns, p["name"], c, ROUND_S * SLOW_EVERY + 30), raw=True)
            except Exception:
                return job, ""
        seen = {}  # (ns, sig) -> [workload, pod, sample line, count]
        with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
            for (ns, p, c), text in ex.map(read, jobs):
                for line in text.splitlines():
                    if ERROR_RE.search(line[:LEVEL_ZONE]):
                        sig = signature(line) if ns == OUR_NS else signature(ns + " " + line)  # our old signatures stay valid
                        seen.setdefault((ns, sig), [p["workload"], p["name"], line[:500], 0])[3] += 1
        known = {r["sig"] for r in self.rows("SELECT sig FROM signatures")}
        with self.lock:
            self.conn.executemany("INSERT INTO signatures VALUES (?,?,?,?,?,?,?) ON CONFLICT(sig) DO UPDATE SET last=?, count=count+?",
                                  [(s, ns, wl, t, t, n, line, t, n) for (ns, s), (wl, _, line, n) in seen.items()])
            self.conn.commit()
        sym = {}
        for (ns, sig), (wl, pod, line, n) in seen.items():
            key = "logs|new-error|%s|%s|%s" % (ns, wl, sig)
            if (sig not in known and not learning.get(ns, True)) or key in open_keys:
                sym[key] = ("new-error", ns, wl, "%s (%d× in the last %d min)" % (line[:300], n, ROUND_S * SLOW_EVERY // 60),
                            {"pod": pod, "sample": line})
        self.observe("logs", sym, t)

    def test_models(self, t):
        """Every 5 min: a 1-token chat call (or a tiny embedding) to each KubeAI model litellm knows. Needs the litellm key."""
        if not self.integrations()["litellm"]:
            return
        names = self.litellm_models() or []
        base, auth = os.environ["LITELLM_URL"].rstrip("/"), {"Authorization": "Bearer " + os.environ["LITELLM_KEY"]}
        sym = {}
        for o in [o for o in self.inv.get(OUR_NS, {}).values() if o["kind"] == "Model" and o["min"]]:
            if o["engine"] == "FasterWhisper":
                continue  # speech models take audio, not text
            route = next((n for n in names if n == o["name"] or o["name"] in n), None)
            if not route:
                continue
            embed = o["engine"] == "Infinity" or any(k in o["name"] for k in ("embed", "bge", "jina", "dinov2"))
            t0 = time.time()
            try:
                if embed:
                    http_json("POST", base + "/embeddings", auth, {"model": route, "input": "ping"}, timeout=60)
                else:
                    http_json("POST", base + "/chat/completions", auth,
                              {"model": route, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]}, timeout=60)
                ok, detail = True, "%d ms" % ((time.time() - t0) * 1000)
            except Exception as e:
                ok, detail = False, probe.err(e)[:200]
            self.model_tests[o["name"]] = {"ok": ok, "ms": int((time.time() - t0) * 1000), "detail": detail, "ts": t, "route": route}
            self.test_fails[o["name"]] = 0 if ok else self.test_fails.get(o["name"], 0) + 1
            if self.test_fails[o["name"]] >= 2:
                sym["models|model-test|%s|%s" % (OUR_NS, o["name"])] = ("model-test", OUR_NS, o["name"],
                                                                         "test call via litellm (%s) failed: %s" % (route, detail), {})
        self.observe("models", sym, t)

    # --- incidents ------------------------------------------------------------------------------------------------
    def observe(self, scope, sym, t, alert=True):
        """Open new incidents, refresh the ones still seen, close the ones gone for CLEAN_TO_CLOSE rounds."""
        open_rows = {r["key"]: r for r in self.rows("SELECT * FROM incidents WHERE state='open' AND scope=?", (scope,))}
        for key, (symptom, ns, obj, detail, ctx) in sym.items():
            r = open_rows.get(key)
            if r:
                d = self.diagnose(symptom, ns, obj, detail, ctx, r["opened"])
                self.exec("UPDATE incidents SET last_seen=?, detail=?, clean=0, cause=?, fix=?, owner=?, playbook=?, "
                          "change_id=coalesce(change_id, ?) WHERE id=?",
                          (t, detail, d["cause"], d["fix"], d["owner"], d["playbook"], d["change_id"], r["id"]))
            else:
                self.open_incident(key, scope, symptom, ns, obj, detail, ctx, t, alert)
        for key, r in open_rows.items():
            if key in sym:
                continue
            if r["symptom"] == "new-error":
                if t - r["last_seen"] >= ERROR_QUIET_CLOSE:
                    self.close(r, t)
            elif r["clean"] + 1 >= CLEAN_TO_CLOSE:
                self.close(r, t)
            else:
                self.exec("UPDATE incidents SET clean=clean+1 WHERE id=?", (r["id"],))

    def diagnose(self, symptom, ns, obj, detail, ctx, t):
        cause, fix, book = RULES[symptom]
        owner = "infra" if symptom == "infra" else "us" if symptom == "endpoint" and ns in ("-", OUR_NS) else owner_of(ns, obj)
        low = (ctx.get("event") or detail).lower()
        if symptom == "pending":
            if "insufficient" in low:
                cause = "no free %s in the cluster" % "/".join(sorted(set(re.findall(r"insufficient ([\w./-]+)", low))))
                owner, book = "infra", "Infra or us? Decide the owner first"
                fix = "ask the infra team for capacity, or lower the request (smaller resourceProfile or fewer replicas)"
            elif "persistentvolumeclaim" in low:
                cause, fix = "the volume claim is missing or not bound", "create the PVC or fix its StorageClass"
            elif ctx.get("reason") == "Init":
                cause = ctx["event"]
                fix = "read that init container's log (Logs page); it usually waits for a dependency (database, S3, another service)"
            elif ctx.get("reason") in ("FailedMount", "FailedAttachVolume"):
                cause, fix, owner = "the volume cannot be mounted (Ceph)", "send the event below to the infra team", "infra"
        elif symptom == "image-pull":
            if any(k in low for k in ("i/o timeout", "connection refused", "503", "no route to host")):
                cause, fix, owner = "Harbor (the registry) is unreachable", "send the event below to the infra team", "infra"
            elif any(k in low for k in ("not found", "manifest unknown", "unauthorized", "denied")):
                cause, fix = "the image tag does not exist, or the pull secret is wrong", "push the image to Harbor or fix the tag / pull secret"
        elif symptom == "oomkilled":
            fix = fix.format(limit=ctx.get("limit") or "not set")
        elif symptom == "endpoint":
            behind = self.rows("SELECT id, symptom, obj FROM incidents WHERE state='open' AND scope=? ORDER BY id", (ns,))
            if behind:
                cause = "the app behind it is broken: " + ", ".join("#%d %s %s" % (b["id"], b["symptom"], b["obj"]) for b in behind[:3])
                fix = "fix incident #%d first" % behind[0]["id"]
            if ctx.get("meta", {}).get("person"):
                fix += " · asset owner: %s" % ctx["meta"]["person"]
        change = None
        if owner != "infra":
            change = self.one("SELECT * FROM changes WHERE ns=? AND name=? AND ts>=? AND type NOT IN ('removed','model-removed') "
                              "ORDER BY ts DESC LIMIT 1", (ns, obj, t - CHANGE_WINDOW))
            if change:
                mins = (t - change["ts"]) // 60
                if change["type"] == "release":
                    cause = "bad release %s → %s, %d min before. Symptom: %s" % (change["before"], change["after"], mins, cause)
                    fix = "roll back %s to %s: revert its MR in prod-cluster, or set the image back. Otherwise: %s" % (obj, change["before"], fix)
                else:
                    cause = "%s %d min before. Symptom: %s" % (change["type"], mins, cause)
                    fix = "check or revert that %s. Otherwise: %s" % (change["type"], fix)
            else:
                other = self.one("SELECT * FROM changes WHERE ns=? AND ts>=? ORDER BY ts DESC LIMIT 1", (ns, t - CHANGE_WINDOW))
                if other:
                    cause += " (possibly related: %s of %s, %d min before)" % (other["type"], other["name"], (t - other["ts"]) // 60)
        return {"cause": cause, "fix": fix, "owner": owner, "playbook": book, "change_id": change and change["id"]}

    def evidence(self, ns, obj, symptom, ctx, t):
        pods = [p for p in self.pods.get(ns, []) if p["workload"] == obj]
        ev = {"pods": pods[:10]}
        names = {p["name"] for p in pods} | {obj}
        ev["events"] = [e for e in self.events.get(ns, []) if e["name"] in names][-15:]
        worst = next((p for p in pods if p["name"] == ctx.get("pod")), None) or next((p for p in pods if p["waiting"] or not p["ready"]),
                                                                                       pods[0] if pods else None)
        if worst and worst["bad"]:
            prev = symptom in ("crashloop", "oomkilled", "flapping")
            ev["log_from"] = "%s / %s%s" % (worst["name"], worst["bad"], " (previous container)" if prev else "")
            ev["logs"] = self.log_text(ns, worst["name"], worst["bad"], 50, prev)
        ev["changes"] = self.rows("SELECT ts, kind, name, type, before, after FROM changes WHERE ns=? AND ts>=? ORDER BY ts DESC LIMIT 10",
                                  (ns, t - 7200))
        ev["probe_failing"] = [[n, r[1], r[2]] for n, r in self.probe.get("results", {}).items() if not r[0]][:15]
        for k in ("sample", "meta"):
            if ctx.get(k):
                ev[k] = ctx[k]
        return ev

    def open_incident(self, key, scope, symptom, ns, obj, detail, ctx, t, alert):
        d = self.diagnose(symptom, ns, obj, detail, ctx, t)
        try:
            ev = self.evidence(ns, obj, symptom, ctx, t)
        except Exception as e:
            ev = {"error": probe.err(e)}
        send = alert and alertable(ns, d["owner"])
        iid = self.exec("INSERT INTO incidents (key, scope, state, ns, obj, symptom, owner, cause, fix, playbook, detail, evidence, "
                        "change_id, opened, last_seen, clean, alerted) VALUES (?,?,'open',?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
                        (key, scope, ns, obj, symptom, d["owner"], d["cause"], d["fix"], d["playbook"], detail, json.dumps(ev),
                         d["change_id"], t, t, int(send)))
        if send:
            icon = {"infra": "🔴", "us": "🟠"}.get(d["owner"], "🟡")
            who = {"infra": "infra team", "us": "MLOps"}.get(d["owner"], "team " + d["owner"])
            self.alerts.append("%s OPEN #%d %s · %s/%s · %s\n%s\nCause: %s\nFix: %s%s" % (
                icon, iid, symptom, ns, obj, who, detail[:200], d["cause"][:300], d["fix"][:300],
                "\n%s#/incident?id=%d" % (OPS_URL, iid) if OPS_URL else ""))
        if self.integrations()["litellm"]:
            self.bg.put(("litellm", iid))
        return iid

    def close(self, r, t):
        self.exec("UPDATE incidents SET state='closed', closed=? WHERE id=?", (t, r["id"]))
        if r["alerted"]:
            self.alerts.append("✅ CLOSED #%d %s · %s/%s (open %d min)" % (r["id"], r["symptom"], r["ns"], r["obj"], (t - r["opened"]) // 60))

    def flush_alerts(self):
        """Send unsent alerts as one Telegram message through London. Every alert is kept in `outbox`: while London
        is down they wait there (and on the page) and go out together once it is back."""
        if self.alerts:
            with self.lock:
                self.conn.executemany("INSERT INTO outbox (ts, text) VALUES (?, ?)", [(time.time(), a) for a in self.alerts])
                self.conn.commit()
            self.alerts = []
        pending = self.rows("SELECT id, text FROM outbox WHERE sent IS NULL ORDER BY id")
        if not pending or not os.environ.get("PROBE_KEY") or not probe.CFG.get("receiver_url"):
            return
        n = len(pending)
        text = "\n\n".join(r["text"] for r in pending[:15]) + ("\n\n+%d more on the dashboard" % (n - 15) if n > 15 else "")
        text = ("AIP ops\n\n" + text)[:3900]
        body = json.dumps({"ts": int(time.time()), "text": text, "id": "ops-%d" % pending[-1]["id"]}).encode()  # London skips a repeat
        sig = hmac.new(os.environ["PROBE_KEY"].encode(), body, hashlib.sha256).hexdigest()
        req = urllib.request.Request(probe.CFG["receiver_url"].rsplit("/", 1)[0] + "/alert", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-Signature": sig})
        try:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=60).read()
            self.exec("UPDATE outbox SET sent=? WHERE sent IS NULL AND id<=?", (time.time(), pending[-1]["id"]))
            self.integ_err.pop("alerts", None)
        except Exception as e:
            self.integ_err["alerts"] = probe.err(e)

    def prune(self, t):
        with self.lock:
            self.conn.execute("DELETE FROM changes WHERE ts < ?", (t - RETENTION,))
            self.conn.execute("DELETE FROM incidents WHERE state='closed' AND closed < ?", (t - RETENTION,))
            self.conn.execute("DELETE FROM signatures WHERE last < ?", (t - RETENTION,))
            self.conn.execute("DELETE FROM outbox WHERE ts < ?", (t - RETENTION,))
            self.conn.execute("DELETE FROM restarts WHERE day < ?", (time.strftime("%Y-%m-%d", time.gmtime(t - RETENTION)),))
            self.conn.commit()
        self.set_meta("pruned", str(t))

    # --- reads for the web page -----------------------------------------------------------------------------------
    def log_text(self, ns, pod, container, tail, previous=False):
        base = "/api/v1/namespaces/%s/pods/%s/log?container=%s&tailLines=%d" % (ns, pod, urllib.parse.quote(container), tail)
        try:
            return kget(base + ("&previous=true" if previous else ""), raw=True)
        except KubeError as e:
            if previous and e.code == 400:  # no previous container: show the current one
                return kget(base, raw=True)
            return "(log not available: %s)" % e.args[0]

    def incident_text(self, iid):
        i = self.incident(iid)
        ev = i["evidence"]
        events = "\n".join("%s %s: %s" % (e["reason"], e["name"], e["msg"]) for e in ev.get("events", []))
        return "Incident: %s in %s/%s\nDetail: %s\nRule-based cause: %s\nEvents:\n%s\nLog (%s):\n%s" % (
            i["symptom"], i["ns"], i["obj"], i["detail"], i["cause"], events, ev.get("log_from", "-"), ev.get("logs", "")[-4000:])

    def state(self):
        t = time.time()
        incs = self.rows("SELECT id, ns, obj, symptom, owner, cause, detail, opened, last_seen, scope FROM incidents "
                         "WHERE state='open' ORDER BY opened DESC")
        changes = self.rows("SELECT * FROM changes WHERE ts>=? ORDER BY ts DESC", (t - 86400,))
        week = {r["type"]: r["n"] for r in self.rows("SELECT type, count(*) n FROM changes WHERE ts>=? AND ns NOT LIKE 'user-%' "
                                                     "GROUP BY type", (t - 7 * 86400,))}
        models = [o for o in self.inv.get(OUR_NS, {}).values() if o["kind"] == "Model"]
        sigs = self.one("SELECT count(*) n, sum(first>=?) new FROM signatures WHERE last>=?", (t - 86400, t - 86400))
        res = self.probe.get("results", {})
        probe_down = {o: sum(1 for r in res.values() if not r[0] and r[2] == o) for o in ("infra", "us", "asset", "monitor")}
        bugs = [i for i in incs if i["symptom"] in BUG]
        down = [i for i in incs if i["symptom"] in DOWN]
        rel = [c for c in changes if c["type"] in ("release", "config")]
        not_ready = [m for m in models if m["min"] and m["ready"] < m["min"]]
        bad = lambda n: "bad" if n else "ok"
        tiles = [
            {"n": 1, "title": "Bugs", "value": len(bugs), "sub": "crash loops, OOM, restarts, new errors", "status": bad(bugs),
             "link": "#/incidents?kind=bug"},
            {"n": 2, "title": "Down", "value": len(down), "sub": "services, models, endpoints not working", "status": bad(down),
             "link": "#/incidents?kind=down"},
            {"n": 3, "title": "Features (releases)", "value": len(rel), "status": bad(any(c["verdict"] == "bad" for c in rel)),
             "sub": "24 h · %d bad · %d watching" % (sum(c["verdict"] == "bad" for c in rel), sum(c["verdict"] == "watching" for c in rel)),
             "link": "#/changes?type=release"},
            {"n": 4, "title": "New services", "value": week.get("new-service", 0) + week.get("new-host", 0) + week.get("new-namespace", 0),
             "sub": "7 days · services, hostnames, namespaces", "status": "info", "link": "#/changes?type=new-service&days=7"},
            {"n": 5, "title": "New models", "value": week.get("new-model", 0), "sub": "7 days · %d model changes" % week.get("model-changed", 0),
             "status": "info", "link": "#/changes?type=new-model&days=7"},
            {"n": 6, "title": "Models", "value": "%d/%d" % (len(models) - len(not_ready), len(models)), "status": bad(not_ready),
             "sub": "ready · " + (", ".join(m["name"] for m in not_ready) or "all ready"), "link": "#/models"},
            {"n": 7, "title": "Logs", "value": sigs["n"] or 0, "sub": "error types in 24 h · %d new" % (sigs["new"] or 0),
             "status": bad(any(i["symptom"] == "new-error" for i in incs)), "link": "#/logs"},
            {"n": 8, "title": "Infra or us", "value": "%d · %d" % (probe_down["infra"], probe_down["us"] + probe_down["asset"]),
             "sub": "probe checks down: infra · ours" + (" · probe has %d problems" % probe_down["monitor"] if probe_down["monitor"] else ""),
             "status": "bad" if probe_down["infra"] or probe_down["us"] or probe_down["asset"] else "ok", "link": "#/infra"},
        ]
        unsent = self.rows("SELECT ts, text FROM outbox WHERE sent IS NULL ORDER BY id DESC LIMIT 50")
        return {"tiles": tiles, "incidents": incs, "unsent": unsent, "changes": changes[:40], "status": self.status, "now": t,
                "integrations": dict(self.integrations(), errors=self.integ_err), "probe_ts": self.probe.get("ts")}

    def incidents(self, state="open", kind=None, limit=300):
        rows = self.rows("SELECT id, ns, obj, symptom, owner, cause, detail, opened, last_seen, closed, scope FROM incidents "
                         "WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit))
        group = {"bug": BUG, "down": DOWN}.get(kind)
        return [r for r in rows if not group or r["symptom"] in group]

    def incident(self, iid):
        i = self.one("SELECT * FROM incidents WHERE id=?", (iid,))
        if not i:
            raise KeyError("no incident %s" % iid)
        i["evidence"] = json.loads(i["evidence"] or "{}")
        i["change"] = self.one("SELECT * FROM changes WHERE id=?", (i["change_id"],)) if i["change_id"] else None
        logs = [l for l in i["evidence"].get("logs", "").splitlines() if l.strip()][-5:]
        i["handoff"] = "\n".join([
            "[AIP] %s in %s/%s since %s Tehran time" % (i["symptom"], i["ns"], i["obj"],
                                                          time.strftime("%Y-%m-%d %H:%M", time.gmtime(i["opened"] + 3.5 * 3600))),
            "What we see: " + i["detail"], "Likely cause: " + i["cause"], "Suggested fix: " + i["fix"]]
            + (["Last log lines:"] + logs if logs else []) + (["Dashboard: %s#/incident?id=%d" % (OPS_URL, iid)] if OPS_URL else []))
        return i

    def changes(self, days=1, typ=None):
        rows = self.rows("SELECT * FROM changes WHERE ts>=? ORDER BY ts DESC LIMIT 500", (time.time() - days * 86400,))
        groups = {"new-service": ("new-service", "new-host", "new-namespace"), "release": ("release", "config"),
                  "new-model": ("new-model", "model-changed", "model-removed")}
        rows = [r for r in rows if not typ or r["type"] in groups.get(typ, (typ,))]
        for r in rows:
            r["info"] = json.loads(r["info"] or "{}")
        return rows

    def models(self):
        on = self.integrations()
        gpu = self.gpu_util() if on["grafana"] else {}
        llm_names = (self.litellm_models() if on["litellm"] else None)
        counts = {r["obj"]: r["n"] for r in self.rows("SELECT obj, count(*) n FROM incidents WHERE state='open' AND ns=? GROUP BY obj", (OUR_NS,))}
        out = []
        for o in sorted((o for o in self.inv.get(OUR_NS, {}).values() if o["kind"] == "Model"), key=lambda o: o["name"]):
            pods = [p for p in self.pods.get(OUR_NS, []) if p["workload"] == o["name"]]
            g = [gpu[p["name"]] for p in pods if p["name"] in gpu]
            out.append(dict(o, pods=pods, open=counts.get(o["name"], 0), test=self.model_tests.get(o["name"]), gpu=round(sum(g) / len(g), 1) if g else None,
                            litellm=None if llm_names is None else any(o["name"] == n or o["name"] in n for n in llm_names)))
        return out

    def namespaces_info(self):
        counts = {r["ns"]: r["n"] for r in self.rows("SELECT ns, count(*) n FROM incidents WHERE state='open' GROUP BY ns")}
        return [{"ns": ns, "pods": len(self.pods.get(ns, [])), "workloads": sum(o["kind"] in ("Deployment", "StatefulSet", "DaemonSet", "CronJob") for o in inv.values()),
                 "open": counts.get(ns, 0), "read": self.read_at.get(ns)} for ns, inv in sorted(self.inv.items())]

    def services(self, ns):
        restarts = {}
        for p in self.pods.get(ns, []):
            restarts[p["workload"]] = restarts.get(p["workload"], 0) + p["restarts"]
        workloads = [dict(o, restarts=restarts.get(o["name"], 0)) for o in self.inv.get(ns, {}).values() if o["kind"] != "Ingress"]
        return {"ns": ns, "read": self.read_at.get(ns), "workloads": sorted(workloads, key=lambda o: o["name"]),
                "ingresses": [o for o in self.inv.get(ns, {}).values() if o["kind"] == "Ingress"], "pods": self.pods.get(ns, []),
                "events": self.events.get(ns, [])[-30:]}

    def record_restarts(self, ns, pods, t):
        """Restart counter per workload, first and last value of each day: the day's restarts = last - first."""
        day, per = time.strftime("%Y-%m-%d", time.gmtime(t)), {}
        for p in pods:
            per[p["workload"]] = per.get(p["workload"], 0) + p["restarts"]
        with self.lock:
            self.conn.executemany("INSERT INTO restarts VALUES (?,?,?,?,?) ON CONFLICT(day, ns, wl) DO UPDATE SET last=excluded.last, "
                                  "first=min(first, excluded.first)", [(day, ns, wl, n, n) for wl, n in per.items()])
            self.conn.commit()

    def capacity(self):
        """Per namespace: CPU/RAM used vs requested, GPUs held. Pods near their memory limit, idle pods that hold resources."""
        gib, rows, near, idle, gpus = 1024 ** 3, [], [], [], []
        for ns, pods in sorted(self.pods.items()):
            live = [p for p in pods if p["phase"] == "Running"]
            if not live:
                continue
            cpu_use = sum(p.get("use_cpu") or 0 for p in live)
            mem_use = sum(p.get("use_mem") or 0 for p in live)
            g = {}
            for p in live:
                for k, n in p["res"]["gpu"].items():
                    g[k] = g.get(k, 0) + n
                    gpus.append({"ns": ns, "pod": p["name"], "workload": p["workload"], "gpu": k.replace("nvidia.com/", ""), "count": n,
                                 "cpu": p.get("use_cpu")})
                lim, use = p["res"]["mem_lim"], p.get("use_mem")
                if lim and use and use / lim >= 0.85:
                    near.append({"ns": ns, "pod": p["name"], "used_gib": round(use / gib, 2), "limit_gib": round(lim / gib, 2),
                                 "pct": round(100 * use / lim)})
                req = p["res"]["cpu_req"]
                if req >= 1 and p.get("use_cpu") is not None and p["use_cpu"] < 0.02 * req:
                    idle.append({"ns": ns, "pod": p["name"], "cpu_req": round(req, 2), "cpu_use": round(p["use_cpu"], 3),
                                 "gpu": ", ".join("%s×%d" % (k.replace("nvidia.com/", ""), n) for k, n in p["res"]["gpu"].items())})
            rows.append({"ns": ns, "pods": len(live), "cpu_use": round(cpu_use, 2), "cpu_req": round(sum(p["res"]["cpu_req"] for p in live), 2),
                         "mem_use_gib": round(mem_use / gib, 1), "mem_req_gib": round(sum(p["res"]["mem_req"] for p in live) / gib, 1),
                         "gpu": ", ".join("%s×%d" % (k.replace("nvidia.com/", ""), n) for k, n in sorted(g.items())),
                         "gpu_n": sum(g.values()), "metrics": any(p.get("use_cpu") is not None for p in live)})
        rows.sort(key=lambda r: (-r["gpu_n"], -r["cpu_use"]))
        return {"namespaces": rows, "near_limit": sorted(near, key=lambda r: -r["pct"]), "idle": sorted(idle, key=lambda r: -r["cpu_req"]),
                "gpus": sorted(gpus, key=lambda r: (r["ns"], r["pod"]))}

    def trends(self, days=14):
        """Per day: incidents opened/closed, changes, mean time to close; services that break or restart most."""
        t = time.time()
        since = t - days * 86400
        day = "strftime('%Y-%m-%d', {}, 'unixepoch', '+210 minutes')"  # Tehran calendar day
        per = {}
        for r in self.rows("SELECT %s d, count(*) n FROM incidents WHERE opened>=? GROUP BY d" % day.format("opened"), (since,)):
            per.setdefault(r["d"], {})["opened"] = r["n"]
        for r in self.rows("SELECT %s d, count(*) n, avg(closed - opened) m FROM incidents WHERE closed>=? GROUP BY d" % day.format("closed"),
                           (since,)):
            per.setdefault(r["d"], {}).update(closed=r["n"], fix_min=round(r["m"] / 60) if r["m"] else None)
        for r in self.rows("SELECT %s d, count(*) n FROM changes WHERE ts>=? GROUP BY d" % day.format("ts"), (since,)):
            per.setdefault(r["d"], {})["changes"] = r["n"]
        for r in self.rows("SELECT day d, sum(last - first) n FROM restarts WHERE day>=? GROUP BY day",
                           (time.strftime("%Y-%m-%d", time.gmtime(since)),)):
            per.setdefault(r["d"], {})["restarts"] = r["n"]
        top_inc = self.rows("SELECT ns, obj, count(*) n, sum(state='open') open FROM incidents WHERE opened>=? AND ns!='-' "
                            "GROUP BY ns, obj ORDER BY n DESC LIMIT 10", (t - 30 * 86400,))
        top_rst = self.rows("SELECT ns, wl, sum(last - first) n FROM restarts WHERE day>=? GROUP BY ns, wl HAVING n>0 ORDER BY n DESC LIMIT 10",
                            (time.strftime("%Y-%m-%d", time.gmtime(t - 7 * 86400)),))
        return {"days": [dict(v, day=k) for k, v in sorted(per.items(), reverse=True)], "top_incidents": top_inc, "top_restarts": top_rst}

    def infra(self):
        res = self.probe.get("results", {})
        return {"ts": self.probe.get("ts"), "results": [{"name": n, "ok": r[0], "detail": r[1], "owner": r[2], "meta": r[3] if len(r) > 3 else {}}
                                                        for n, r in sorted(res.items())]}
