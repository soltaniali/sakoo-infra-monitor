#!/usr/bin/env python3
"""AIP ops collector (diagnose only). Every round it reads Kubernetes through the station's VPN (GET requests only),
keeps an inventory, records changes (feature/release, new service, new model), detects symptoms (bug, down), and turns
them into incidents with cause, owner, evidence and a suggested fix. Humans do the fix. Stdlib only.

Rounds: every ROUND_S our namespace; every SLOW_EVERY rounds all readable namespaces and our pods' logs.
Data: SQLite $OPS_DATA/ops.db (default /data). Probe results: $PROBE_RESULTS (written by probe.py on this station).
Optional integrations, each on when its env is set (secrets/ops.env):
  LITELLM_URL (…/v1), LITELLM_KEY, LITELLM_MODEL     model test calls, "registered in litellm?" for models
  MLOPS_URL, MLOPS_KEY, MLOPS_SECRET                 training tasks (login and list calls only; the key is an admin key)
  CASDOOR_URL, CASDOOR_KEY, CASDOOR_SECRET           login records (read only; the key has its user's rights)
  LITELLM_READ_URL, LITELLM_READ_KEY                 read-only key (Admin Viewer): key owners, limits, old spend logs
  AI_URL (…/v1), AI_KEY, AI_MODEL                     any OpenAI-compatible LLM (free tier: Mistral, OpenRouter, ...) for log,
                                                       incident and ticket summaries; without it the LITELLM_* model is used
  GITLAB_URL, GITLAB_TOKEN, GITLAB_PROJECTS            author and MR of a change (comma-separated project paths)
  GRAFANA_URL, GRAFANA_TOKEN or GRAFANA_USER + GRAFANA_PASSWORD [, GRAFANA_DATASOURCE_UID] GPU % per model pod (DCGM_FI_DEV_GPU_UTIL)
  TICKETS_URL, TICKETS_USER, TICKETS_PASSWORD           support tickets, each checked against our data (tickets.py)
Alerts: one Telegram message per round, through the London receiver's POST /alert (HMAC with PROBE_KEY).
"""
import base64
import calendar
import collections
import concurrent.futures
import contextlib
import gzip
import hashlib
import hmac
import json
import os
import queue
import re
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import probe
import tickets

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
SSO_ORG = "organization_sharif"  # Casdoor organisation of the platform's users (built-in = admins, app = applications)
SSO_RE = re.compile(r"^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d)\.\d+ .*?subOwner = ([^,]*), subName = ([^,]*), method = \w+, "
                    r"urlPath = ([^,]*),.*?result = (\w+)", re.M)
LOG_DIR, LOG_CAP_BYTES, LOG_TAIL = DATA / "logs", 5 * 1024 ** 3, 500  # saved logs: <day>/<ns>/<pod>__<container>.log.gz
LOG_STAMP_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\S*Z ")
LOG_SECRET_RES = (  # logs can hold secrets (data-ingestion-api prints users' S3 keys): masked before they are saved or read
    re.compile(r"(?i)((?:access|secret|password|passwd|pwd|token|api[_-]?key|authorization|bearer|cookie)[\w-]*[\"']?\s*[:= ]\s*[\"']?(?:bearer\s+)?)[^\s\"',;&]{4,}"),
    re.compile(r"\b[A-Z0-9]{20}\b"), re.compile(r"(?<![\w/+])[A-Za-z0-9/+]{40}(?![\w/+=])"))
# GPU-hours held per namespace over a range: running pods only, sampled every 15 min. % (label filter, range)
GPU_HELD_H = ('sum by (namespace) (sum_over_time((sum by (namespace) (kube_pod_container_resource_requests{resource="nvidia_com_gpu",%s} '
              '* on (namespace,pod) group_left () (kube_pod_status_phase{phase="Running"} == 1)))[%s:15m])) / 4')
SLICE_HELD_H = GPU_HELD_H.replace('resource="nvidia_com_gpu"', 'resource=~"nvidia_com_mig.*"')  # MIG slices of one card: not full GPUs
CHECK_OK_PCT = 2  # two systems agree when they differ by at most this (or by 5 in small counts)
SERVICE_NS = ("jupyterhub", "mlflow", "dify", "n8nio", "zenml", "langflow", "ragflow", "openwebui", "airflow", "harbor", "gitlab")
TASK_STUCK_S = 6 * 3600  # a running task with no update for this long is marked "no update"
LLM_FAIL_MIN, LLM_FAIL_RATE = 20, 0.3  # a key with >= 20 calls in the last hour of which >= 30 % failed: incident (dashboard only)
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
    "llm-failing": ("many of this user's LLM calls fail", "open the user's page: Langfuse shows the error messages and models",
                    "Pipeline B: service is up but broken"),
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
CREATE TABLE IF NOT EXISTS state_since (key TEXT PRIMARY KEY, state TEXT, since REAL);
CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, ts REAL, text TEXT, sent REAL);
CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY, sig TEXT, data TEXT, fetched REAL, seen REAL, ai TEXT);
CREATE TABLE IF NOT EXISTS daily (day TEXT, scope TEXT, name TEXT, metric TEXT, value REAL, PRIMARY KEY (day, scope, name, metric));
CREATE TABLE IF NOT EXISTS logins (name TEXT PRIMARY KEY, ts REAL, user TEXT, result TEXT);
CREATE INDEX IF NOT EXISTS logins_user ON logins (user, ts);
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
    if ns == OUR_NS or (ns.startswith("user-") and obj.endswith("clearml-agent")):  # per-user mlops-core agents are ours
        return "us"
    return ns


def key_user(m):
    """AIP username of a LiteLLM key from its Prometheus labels: alias '<user>-<key name>', else the key's user email."""
    alias, mail = m.get("api_key_alias") or "None", m.get("user_email") or "None"
    return alias.split("-", 1)[0] if "-" in alias else mail.split("@")[0] if "@" in mail else None


TEHRAN_S = 12600  # the page's calendar is Tehran's: +03:30


def tday(t):
    return time.strftime("%Y-%m-%d", time.gmtime(t + TEHRAN_S))


def tday_start(day):
    return calendar.timegm(time.strptime(day, "%Y-%m-%d")) - TEHRAN_S


def mask_log(line):
    line = LOG_SECRET_RES[0].sub(r"\1***", tickets.mask(line))
    return LOG_SECRET_RES[2].sub("***", LOG_SECRET_RES[1].sub("***", line))


def slim_task(x, names, t):
    """What the dashboard keeps of a mlops-core task. Never its script, config or hyperparameters: they can hold secrets."""
    last = ts(x.get("last_update"))
    return {"id": x.get("id"), "name": tickets.mask(x.get("name"))[:80], "user": (names.get(x.get("user")) or "?").lower(), "status": x.get("status"), "type": x.get("type"),
            "created": ts(x.get("created")), "started": ts(x.get("started")), "ended": ts(x.get("completed")), "last": last,
            "reason": tickets.mask(x.get("status_reason"))[:120], "stuck": x.get("status") == "in_progress" and bool(last) and last < t - TASK_STUCK_S}


def login_row(r, known):
    """(record name, time, user, result) of a Casdoor login record, or None if it is no real login attempt.
    Never keeps the record's Object (it holds what was typed) or its IP (always the same internal proxy). A name that
    is no known account becomes "(unknown name)": people type passwords into the username field."""
    resp = r.get("response") or ""
    if 'status:"ok"' in resp:
        result = "ok"
    elif "wrong password" in resp or "password or code is incorrect" in resp:
        result = "wrong password"
    elif "The user: " in resp:
        result = "no such user"
    else:
        return None
    try:
        typed = json.loads(r.get("object") or "{}")
    except ValueError:
        typed = {}
    name = r.get("user") or (typed.get("username") if isinstance(typed, dict) else "") or ""
    name = str(name).rsplit("/", 1)[-1].strip().lower()
    return r.get("name"), ts(r.get("createdTime")), name if name in known else "(unknown name)", result


def slim_key(k, u, t):
    """What the dashboard shows of a LiteLLM key: owner, limits, warnings. Never the key itself, not even its hash."""
    alias, mail = k.get("key_alias") or "(no alias)", u.get("user_email") or "None"
    spend, budget, exp, active = k.get("spend") or 0, k.get("max_budget"), ts(k.get("expires")), ts(k.get("last_active"))
    flags = (["blocked"] if k.get("blocked") else []) + (["expired"] if exp and exp < t else ["expires soon"] if exp and exp < t + 14 * 86400 else [])
    if budget is not None:
        flags += ["over budget"] if spend >= budget else ["near budget"] if spend >= 0.8 * budget else []
    created = ts(k.get("created_at"))
    if "expired" not in flags and (not active or active < t - 30 * 86400) and created and created < t - 30 * 86400:
        flags.append("unused 30 d")
    # the owner LiteLLM records (ham-prd belongs to nilva); no owner record: the alias prefix
    name = re.split(r"[\s(]", (u.get("user_alias") or "").strip())[0].lower()
    return {"alias": alias, "user": name or key_user({"api_key_alias": alias, "user_email": mail}) or "?", "email": mail if "@" in mail else None,
            "spend": round(spend, 2), "budget": budget, "rpm": k.get("rpm_limit"), "tpm": k.get("tpm_limit"), "models": k.get("models") or [],
            "expires": exp, "active": active, "created": created, "flags": flags}


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
    r = {"cpu_req": 0.0, "cpu_lim": 0.0, "mem_req": 0.0, "mem_lim": 0.0, "gpu": {}}
    for c in containers:
        res = c.get("resources", {})
        req, lim = res.get("requests", {}), res.get("limits", {})
        r["cpu_req"] += qty(req.get("cpu"))
        r["cpu_lim"] += qty(lim.get("cpu"))
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
            "res": resources(spec.get("containers", [])),
            "jhub_user": p["metadata"].get("labels", {}).get("hub.jupyter.org/username"),
            "since": ts(next((c.get("lastTransitionTime") for c in st.get("conditions", []) if c["type"] == "Ready"), None))}


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
        self.states = {r["key"]: (r["state"], r["since"]) for r in self.rows("SELECT * FROM state_since")}  # liveness
        self.bg, self.tickets_busy, self.tickets_tried = queue.Queue(), False, 0
        self.task_rows = {"at": None, "rows": []}  # last good read of mlops-core's tasks
        self.checks = {"at": None, "rows": []}  # last data_check
        self.log_marks = {}  # (ns, pod, container) -> newest saved log time: scan windows overlap, nothing is saved twice
        self.key_rows = {"at": None, "rows": []}  # last good read: the VPN path to LiteLLM drops every few minutes
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
                "grafana": bool(e("GRAFANA_URL") and (e("GRAFANA_TOKEN") or e("GRAFANA_USER") and e("GRAFANA_PASSWORD"))),
                "keys": bool(e("LITELLM_READ_URL") and e("LITELLM_READ_KEY")),
                "mlops": bool(e("MLOPS_URL") and e("MLOPS_KEY") and e("MLOPS_SECRET")),
                "casdoor": bool(e("CASDOOR_URL") and e("CASDOOR_KEY") and e("CASDOOR_SECRET")),
                "tickets": tickets.enabled(), "langfuse": bool(e("LANGFUSE_URL") and e("LANGFUSE_PK") and e("LANGFUSE_SK")),
                "ai": bool(e("AI_URL") and e("AI_KEY") and e("AI_MODEL")) or bool(e("LITELLM_URL") and e("LITELLM_KEY") and e("LITELLM_MODEL"))}

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

    def llm(self, text, system=None, max_tokens=350):
        p = "AI_" if os.environ.get("AI_URL") and os.environ.get("AI_KEY") and os.environ.get("AI_MODEL") else "LITELLM_"
        url = os.environ[p + "URL"].rstrip("/") + "/chat/completions"
        models = [m.strip() for m in os.environ[p + "MODEL"].split(",") if m.strip()]
        for i, model in enumerate(models):  # free tiers: a model can be rate-limited or not in the tier; then try the next one
            try:
                body = {"model": model, "temperature": 0.2, "max_tokens": max_tokens, "messages": [
                    {"role": "system", "content": system or "You help the on-call MLOps engineer of AIP, a Kubernetes AI platform. Reply "
                                                            "in at most 5 short sentences, plain text: what failed, the most likely cause, the next step."},
                    {"role": "user", "content": text[-6000:]}]}
                r = http_json("POST", url, {"Authorization": "Bearer " + os.environ[p + "KEY"]}, body, timeout=120)
                return r["choices"][0]["message"]["content"].strip()
            except urllib.error.HTTPError as e:
                if e.code not in (403, 404, 429) or i == len(models) - 1:
                    raise
            except urllib.error.URLError:  # the host's DNS fails now and then: one more try, same model
                time.sleep(5)
                r = http_json("POST", url, {"Authorization": "Bearer " + os.environ[p + "KEY"]}, body, timeout=120)
                return r["choices"][0]["message"]["content"].strip()

    def litellm_models(self):
        url = os.environ["LITELLM_URL"].rstrip("/") + "/models"
        return self.cached("litellm", 300, lambda: [m["id"] for m in http_json(
            "GET", url, {"Authorization": "Bearer " + os.environ["LITELLM_KEY"]})["data"]])

    def prom(self, query, at=None):
        """Instant query on Grafana's Prometheus (GET); at = epoch seconds for history, else now."""
        g, tok = os.environ["GRAFANA_URL"].rstrip("/"), os.environ.get("GRAFANA_TOKEN")
        # a token, or a normal Grafana login (basic auth works: the login form is on, no SSO)
        auth = {"Authorization": "Bearer " + tok if tok else "Basic " + base64.b64encode(
            (os.environ["GRAFANA_USER"] + ":" + os.environ["GRAFANA_PASSWORD"]).encode()).decode()}
        uid = os.environ.get("GRAFANA_DATASOURCE_UID") or self.meta("grafana_uid")
        if not uid:
            try:
                ds = http_json("GET", g + "/api/datasources", auth)
            except urllib.error.HTTPError as e:
                if e.code != 403:
                    raise
                ds = list(http_json("GET", g + "/api/frontend/settings", auth)["datasources"].values())  # Viewer tokens
            uid = next(d["uid"] for d in ds if d.get("type") == "prometheus")
            self.set_meta("grafana_uid", uid)
        q = "query=" + urllib.parse.quote(query) + ("&time=%d" % at if at else "")
        return http_json("GET", "%s/api/datasources/proxy/uid/%s/api/v1/query?%s" % (g, uid, q), auth)["data"]["result"]

    def gpu_util(self):
        """{pod name: GPU utilization %} from Grafana's Prometheus (DCGM exporter)."""
        def fetch():
            out = {}
            for s in self.prom("DCGM_FI_DEV_GPU_UTIL"):
                pod = s["metric"].get("exported_pod") or s["metric"].get("pod")
                out.setdefault(pod, []).append(float(s["value"][1]))
            return {p: sum(v) / len(v) for p, v in out.items() if p}
        return self.cached("grafana", 60, fetch) or {}

    def lf(self, path, **q):
        """GET on the AIP Langfuse public API. LiteLLM traces every call there (callbacks: langfuse)."""
        auth = {"Authorization": "Basic " + base64.b64encode((os.environ["LANGFUSE_PK"] + ":" + os.environ["LANGFUSE_SK"]).encode()).decode()}
        url = os.environ["LANGFUSE_URL"].rstrip("/") + path + "?" + urllib.parse.urlencode(q)
        try:
            return http_json("GET", url, auth, timeout=120)
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError:  # flaky network: one more try
            time.sleep(3)
            return http_json("GET", url, auth, timeout=120)

    def llm_log(self, who, frm, to):
        """LLM requests of a user (every key alias '<user>-*') or of one key alias ('<user>-<key>') between frm and to
        (epoch), from Langfuse: per model and level (count, tokens, p95 latency), the most common errors, the last
        requests. Never prompts or answers. None without Langfuse."""
        if not self.integrations()["langfuse"]:
            return None
        f = [{"column": "metadata", "key": "user_api_key_alias", "type": "stringObject",
              "operator": "=" if "-" in who else "starts with", "value": who if "-" in who else who + "-"}]
        iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))

        def fetch():
            q = {"view": "observations", "dimensions": [{"field": "providedModelName"}, {"field": "level"}], "filters": f,
                 "metrics": [{"measure": "count", "aggregation": "count"}, {"measure": "totalTokens", "aggregation": "sum"},
                             {"measure": "latency", "aggregation": "p95"}], "fromTimestamp": iso(frm), "toTimestamp": iso(to)}
            models = sorted(({"model": r["providedModelName"] or "(no model)", "level": r["level"], "count": int(float(r["count_count"])),
                              "tokens": int(float(r.get("sum_totalTokens") or 0)), "p95_ms": round(float(r.get("p95_latency") or 0))}
                             for r in self.lf("/api/public/metrics", query=json.dumps(q))["data"]), key=lambda m: -m["count"])
            obs = lambda **kw: self.lf("/api/public/observations", limit=50, fromStartTime=iso(frm), toStartTime=iso(to),
                                       filter=json.dumps(f), **kw)["data"]
            slim = lambda o: {"ts": ts(o["startTime"]), "model": o.get("model"), "level": o.get("level"), "latency_s": o.get("latency"),
                              "tokens": (o.get("usage") or {}).get("total"), "key": (o.get("metadata") or {}).get("user_api_key_alias"),
                              "status": tickets.mask(o.get("statusMessage"))[:300]}
            errors = {}
            for o in map(slim, obs(level="ERROR")):  # same message with other ids/numbers = one row
                e = errors.setdefault(re.sub(r"[0-9a-f-]{16,}|\d{5,}", "…", o["status"][:160]), {"count": 0, "last": 0, "model": o["model"], "key": o["key"]})
                e["count"], e["last"] = e["count"] + 1, max(e["last"], o["ts"] or 0)
            return {"models": models, "errors": sorted(({"error": k, **v} for k, v in errors.items()), key=lambda e: -e["count"]),
                    "recent": [slim(o) for o in obs()[:20]], "from": frm, "to": to}
        return self.cached("lf:%s:%d:%d" % (who, frm // 300, to // 300), 300, fetch)

    def llm_platform(self, frm, to):
        """All LLM calls through the gateway between frm and to, per model: {model, kind, count, failed} (Langfuse)."""
        if not self.integrations()["langfuse"]:
            return None
        iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))

        def fetch():
            q = {"view": "observations", "dimensions": [{"field": "providedModelName"}, {"field": "level"}], "filters": [],
                 "metrics": [{"measure": "count", "aggregation": "count"}], "fromTimestamp": iso(frm), "toTimestamp": iso(to)}
            out = {}
            for r in self.lf("/api/public/metrics", query=json.dumps(q))["data"]:
                m = r["providedModelName"] or "(no model)"
                o = out.setdefault(m, {"model": m, "kind": tickets.llm_kind(m), "count": 0, "failed": 0})
                n = int(float(r["count_count"]))
                o["count"] += n
                o["failed"] += n if r["level"] == "ERROR" else 0
            return sorted(out.values(), key=lambda o: -o["failed"])
        return self.cached("lfp:%d:%d" % (frm // 300, to // 300), 3600, fetch)

    def sso(self):
        """{"from": epoch, "users": {user: {last, requests, logouts, denied}}} from the Casdoor log (GET pods/log, the
        running pod only, so it covers the time since that pod started). Every request names the signed-in user
        (subName); failed logins are anonymous there, so they cannot be counted per user."""
        def fetch():
            pod = next((p for p in self.pods.get("casdoor", []) if p["workload"] == "casdoor"), None)
            if not pod:
                return {}
            text = kget("/api/v1/namespaces/casdoor/pods/%s/log?tailLines=80000" % pod["name"], raw=True)
            users, first = {}, None
            for m in SSO_RE.finditer(text):
                t = calendar.timegm(time.strptime(m.group(1), "%Y/%m/%d %H:%M:%S"))  # Casdoor logs in UTC
                first = first or t
                if m.group(2) != SSO_ORG:
                    continue
                u = users.setdefault(m.group(3).lower(), {"last": 0, "requests": 0, "logouts": 0, "denied": 0})
                u["last"], u["requests"] = max(u["last"], t), u["requests"] + 1
                u["logouts"] += m.group(4) == "/api/logout"
                u["denied"] += m.group(5) == "deny"
            return {"from": first, "users": users}
        return self.cached("sso", 600, fetch) or {}

    def refresh_logins(self, t):
        """Every 5 min: Casdoor's newest login records, plus a few older pages until the history is in."""
        if not self.integrations()["casdoor"]:
            return
        known = set(self.user_pods()) | {u for p in self.people() for u in p["users"]} | {k["user"] for k in self.key_rows["rows"]}
        known |= set(self.sso().get("users", {}))
        back = int(self.meta("logins_page") or 2)
        for page in [1] + (list(range(back, back + 3)) if back else []):
            q = urllib.parse.urlencode({"p": page, "pageSize": 200, "field": "action", "value": "login", "sortField": "createdTime", "sortOrder": "descend",
                                        "accessKey": os.environ["CASDOOR_KEY"], "accessSecret": os.environ["CASDOOR_SECRET"]})
            r = http_json("GET", os.environ["CASDOOR_URL"].rstrip("/") + "/api/get-records?" + q, timeout=40)
            if r.get("status") != "ok":
                raise RuntimeError("casdoor: %s" % str(r.get("msg"))[:120])
            rows = r.get("data") or []
            for x in rows:
                row = login_row(x, known)
                if row and row[0]:
                    self.exec("INSERT OR IGNORE INTO logins VALUES (?, ?, ?, ?)", row)
            if page > 1 and (not rows or page * 200 >= (r.get("data2") or 0)):  # past the end Casdoor repeats the last page
                back = 0
                break
        self.set_meta("logins_page", str(back + 3 if back else 0))

    def logins_daily(self, frm=None, to=None):
        """Password logins per Tehran day."""
        end = to or time.time()
        out = {}
        for x in self.rows("SELECT strftime('%Y-%m-%d', ts, 'unixepoch', '+210 minutes') d, sum(result='ok') ok, sum(result!='ok') bad FROM logins WHERE ts>=? AND ts<=? GROUP BY d",
                           (frm or end - 30 * 86400, end)):
            out[x["d"]] = {"ok": x["ok"], "failed": x["bad"]}
        return out

    def logins(self, frm=None, to=None):
        """Failed logins over the platform in the range: who is being tried, and how much."""
        end = to or time.time()
        since = frm or end - 7 * 86400
        return {"from": (self.one("SELECT min(ts) t FROM logins") or {}).get("t"),
                "ok": self.one("SELECT count(*) n FROM logins WHERE ts>=? AND ts<=? AND result='ok'", (since, end))["n"],
                "failed": self.one("SELECT count(*) n FROM logins WHERE ts>=? AND ts<=? AND result!='ok'", (since, end))["n"],
                "top": self.rows("SELECT user, count(*) n, max(ts) last FROM logins WHERE ts>=? AND ts<=? AND result!='ok' GROUP BY user ORDER BY n DESC LIMIT 8", (since, end))}

    def mread(self, name, token, body):
        """A call on mlops-core. Its API is ClearML-style: list calls are POSTs too. Only auth.login and *.get_all are ever called."""
        url = os.environ["MLOPS_URL"].rstrip("/") + "/" + name
        for i in range(3):
            try:
                return http_json("POST", url, {"Authorization": token}, body, timeout=40).get("data") or {}
            except urllib.error.HTTPError:
                raise
            except OSError:
                if i == 2:
                    raise
                time.sleep(5)

    def refresh_tasks(self, t):
        """Every 5 min: mlops-core's training and data tasks, with the owner's name."""
        if not self.integrations()["mlops"]:
            return
        basic = base64.b64encode((os.environ["MLOPS_KEY"] + ":" + os.environ["MLOPS_SECRET"]).encode()).decode()
        token = "Bearer " + self.mread("auth.login", "Basic " + basic, {})["token"]
        names = {u["id"]: u.get("name") for u in self.mread("users.get_all", token, {"only_fields": ["id", "name"]}).get("users", [])}
        rows = []
        for page in range(50):
            got = self.mread("tasks.get_all", token, {"page": page, "page_size": 500, "order_by": ["-last_update"], "only_fields": [
                "id", "name", "user", "status", "type", "created", "started", "completed", "last_update", "status_reason"]}).get("tasks", [])
            rows += [slim_task(x, names, t) for x in got if x.get("status") != "created"]
            if len(got) < 500:
                break
        self.task_rows = {"at": t, "rows": rows}

    def tasks(self, frm=None, to=None):
        """The Trainings tab: tasks that were active in the range. Running and queued ones always show."""
        people, end = self.people(), to or time.time()
        rows = [dict(r, person=next((p["name"] for p in people if r["user"] in p["users"]), None)) for r in self.task_rows["rows"]
                if r["status"] in ("in_progress", "queued") or not frm or (frm <= (r["last"] or 0) <= end)]
        return {"at": self.task_rows["at"], "tasks": rows, "error": self.integ_err.get("tasks")}

    def key_owner(self, m):
        """AIP username for a key's Prometheus labels: LiteLLM's owner record when we have it, else from the labels."""
        return next((k["user"] for k in self.key_rows["rows"] if k["alias"] == m.get("api_key_alias")), None) or key_user(m)

    def lread(self, path):
        """GET on LiteLLM with the read-only key. The VPN path drops now and then: try again; an HTTP error is final."""
        url = os.environ["LITELLM_READ_URL"].rstrip("/") + path
        for i in range(3):
            try:
                return http_json("GET", url, {"Authorization": "Bearer " + os.environ["LITELLM_READ_KEY"]}, timeout=25)
            except urllib.error.HTTPError:
                raise
            except OSError:
                if i == 2:
                    raise
                time.sleep(5)

    def refresh_keys(self, t):
        """Every 5 min: all LiteLLM keys with owner, limits and warnings. Never keeps the key itself."""
        if not self.integrations()["keys"]:
            return
        users = {}
        for page in range(1, 51):
            r = self.lread("/user/list?page=%d&page_size=100" % page)
            users.update({u["user_id"]: u for u in r.get("users", [])})
            if page >= (r.get("total_pages") or 1):
                break
        rows = []
        for page in range(1, 51):
            r = self.lread("/key/list?size=100&page=%d&return_full_object=true" % page)
            rows += [slim_key(k, users.get(k.get("user_id")) or {}, t) for k in r.get("keys", []) if not (k.get("key_alias") or "").startswith("ops-dashboard")]
            if page >= (r.get("total_pages") or 1):
                break
        self.key_rows = {"at": t, "rows": rows}

    def keys(self):
        """The Keys tab: every key, its real owner name, and a warning if the name list marks the account to delete."""
        people = self.people()
        out = []
        for k in self.key_rows["rows"]:
            p = next((p for p in people if k["alias"] in p["keys"] or k["user"] in p["users"]), None)
            out.append(dict(k, person=p and p["name"], flags=k["flags"] + (["account to delete"] if p and p.get("remove") else [])))
        return {"at": self.key_rows["at"], "keys": sorted(out, key=lambda k: (-len(k["flags"]), -k["spend"])), "error": self.integ_err.get("keys")}

    def spend_count(self, frm, to, alias=None, status=None):
        fm = lambda x: urllib.parse.quote(time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(x)))
        q = "/spend/logs/ui?start_date=%s&end_date=%s&page=1&page_size=1" % (fm(frm), fm(to))
        q += "&key_alias=" + urllib.parse.quote(alias) if alias else ""
        q += "&status_filter=" + status if status else ""
        return self.lread(q).get("total") or 0

    def backfill_ticket_spend(self, t):
        """LiteLLM's calls around each ticket (older than Prometheus and Langfuse), saved once: a past window never changes.
        A few tickets per round, to keep the load on LiteLLM's database low."""
        if not self.integrations()["keys"]:
            return
        use, done = self.usage().get("users", {}), 0
        for r in self.rows("SELECT id, data FROM tickets ORDER BY id DESC"):
            d = json.loads(r["data"])
            created, (who, _) = d.get("created"), tickets.ticket_user(self, d, use)
            alias = who if who and "-" in who else None
            k = "tspend:%d:%s" % (r["id"], alias or "-")
            if not created or self.meta(k) or t - created < 3 * 3600:
                continue
            a, b = created - tickets.PLATFORM_S, created + tickets.PLATFORM_S
            out = {"total": self.spend_count(a, b), "failed": self.spend_count(a, b, status="failure")}
            if alias:
                a, b = created - tickets.NEAR_S, created + tickets.NEAR_S
                out["user_total"], out["user_failed"] = self.spend_count(a, b, alias), self.spend_count(a, b, alias, "failure")
            self.set_meta(k, json.dumps(out))
            done += 1
            if done >= 4:
                return

    def check_llm_keys(self, t):
        """A user's LLM calls failing: at least LLM_FAIL_MIN calls and LLM_FAIL_RATE of them failed in the last hour, per key
        (LiteLLM counters in Prometheus). Dashboard only, never Telegram: users' keys fail often, and many at once."""
        if not self.integrations()["grafana"]:
            return
        total, fail = {}, {}
        for s in self.prom("sum by (api_key_alias, user_email) (increase(litellm_proxy_total_requests_metric_total[1h]))"):
            total[s["metric"].get("api_key_alias")] = (float(s["value"][1]), s["metric"])
        for s in self.prom("sum by (api_key_alias, exception_status, requested_model) (increase(litellm_proxy_failed_requests_metric_total[1h]))"):
            f = fail.setdefault(s["metric"].get("api_key_alias"), {"codes": {}, "models": {}})
            v = float(s["value"][1])
            c, m = s["metric"].get("exception_status") or "?", s["metric"].get("requested_model") or "?"
            f["codes"][c] = f["codes"].get(c, 0) + v
            f["models"][m] = f["models"].get(m, 0) + v
        sym = {}
        for alias, f in fail.items():
            n, (tot, labels) = sum(f["codes"].values()), total.get(alias, (0, {}))
            who = self.key_owner(dict(labels, api_key_alias=alias))
            if not who or tot < LLM_FAIL_MIN or n < LLM_FAIL_RATE * tot:
                continue
            codes = {c: round(v) for c, v in sorted(f["codes"].items(), key=lambda x: -x[1]) if round(v)}
            models = [m for m, _ in sorted(f["models"].items(), key=lambda x: -x[1])]
            detail = "%d of %d calls failed in the last hour (%d%%): %s · models: %s" % (
                n, tot, 100 * n / tot, ", ".join("%s×%d" % kv for kv in codes.items()), ", ".join(models[:3]))
            sym["llm:" + alias] = ("llm-failing", "user-" + who, alias, detail, {"codes": codes, "models": models})
        self.observe("llm", sym, t, alert=False)

    def window(self, frm=None, to=None):
        """The asked range cut to what Prometheus holds. Percentages divide by these real hours, not by the asked ones."""
        to = to or time.time()
        frm = frm or to - 30 * 86400
        start = self.cached("prom_start", 3600, lambda: next((float(s["value"][1]) for s in self.prom("prometheus_tsdb_lowest_timestamp_seconds")), None))
        real = max(frm, start) if start else frm
        real = min(real, to - 60)
        first = self.one("SELECT min(day) d FROM daily WHERE scope='cluster'")["d"]
        covered = min(real, tday_start(first)) if first else real  # the station's daily history reaches further back than Prometheus
        d1, d2 = self.hist_days(frm, real)
        n = self.one("SELECT count(DISTINCT day) n FROM daily WHERE scope='cluster' AND day>=? AND day<=?", (d1, d2))["n"]
        hours = (to - real) / 3600
        return {"from": frm, "to": to, "real_from": real, "data_from": covered, "cut": frm < covered - 3600, "secs": int(to - real), "hours": hours,
                "hist_hours": n * 24, "all_hours": hours + n * 24, "hist": (d1, d2)}

    @staticmethod
    def hist_days(frm, real):
        """The whole Tehran days of the range that ended before Prometheus's history starts."""
        return tday(frm + (86400 - (frm + TEHRAN_S) % 86400) % 86400), tday(real - 86400)

    def day_rows(self, start, end):
        """One day's usage: [(scope, name, metric, value)]. Key rows are named '<user>\\t<alias>'."""
        span, at, out = "%ds" % int(end - start), int(end), []
        q = lambda query: self.prom(query, at)
        one = lambda query: next((float(s["value"][1]) for s in q(query)), 0.0)
        for metric, query in (("cpu_h", 'sum by (namespace) (increase(container_cpu_usage_seconds_total{container!=""}[%s])) / 3600' % span),
                              ("mem_gib", 'sum by (namespace) (avg_over_time((sum by (namespace) (container_memory_working_set_bytes{container!=""}))[%s:15m])) / 1073741824' % span),
                              ("gpu_h", GPU_HELD_H % ('namespace!=""', span)),
                              ("slice_h", SLICE_HELD_H % ('namespace!=""', span)),
                              ("gpu_util", 'avg by (namespace) (avg_over_time(DCGM_FI_DEV_GPU_UTIL{namespace!=""}[%s]))' % span),
                              ("net_gb", "sum by (namespace) (increase(container_network_receive_bytes_total[%s])) / 1000000000" % span)):
            out += [("ns", s["metric"]["namespace"], metric, float(s["value"][1])) for s in q(query) if s["metric"].get("namespace")]
        for metric, m in (("requests", "litellm_proxy_total_requests_metric_total"), ("failed", "litellm_proxy_failed_requests_metric_total"),
                          ("tokens", "litellm_total_tokens_metric_total"), ("spend", "litellm_spend_metric_total")):
            for s in q("sum by (api_key_alias, user_email) (increase(%s[%s]))" % (m, span)):
                who, alias = self.key_owner(s["metric"]), s["metric"].get("api_key_alias") or "None"
                if who and float(s["value"][1]):
                    out.append(("key", who + "\t" + alias, metric, float(s["value"][1])))
        for metric, m in (("calls", "litellm_proxy_total_requests_metric_total"), ("failed", "litellm_proxy_failed_requests_metric_total")):
            out += [("model", s["metric"]["requested_model"], metric, float(s["value"][1])) for s in q("sum by (requested_model) (increase(%s[%s]))" % (m, span))
                    if s["metric"].get("requested_model") not in (None, "None") and float(s["value"][1])]
        busy = {}
        for s in q('avg by (pod) (avg_over_time(DCGM_FI_DEV_GPU_UTIL{pod=~"model-.*"}[%s]))' % span):
            busy[s["metric"]["pod"][len("model-"):].rsplit("-", 2)[0]] = float(s["value"][1])
        out += [("model", name, "gpu_avg", v) for name, v in busy.items()]
        running = '* on (namespace,pod) group_left () (kube_pod_status_phase{phase="Running"} == 1)'
        util = [float(s["value"][1]) for s in q("avg by (UUID) (avg_over_time(DCGM_FI_DEV_GPU_UTIL[%s]))" % span)]
        for metric, value in (("gpu_total", one('sum(kube_node_status_allocatable{resource="nvidia_com_gpu"})')),
                              ("gpu_held", one('avg_over_time((sum(kube_pod_container_resource_requests{resource="nvidia_com_gpu"} %s))[%s:15m])' % (running, span))),
                              ("gpu_busy", sum(util) / len(util) if util else 0),
                              ("cpu_total", one('sum(kube_node_status_allocatable{resource="cpu"})')),
                              ("cpu_used", one('sum(rate(container_cpu_usage_seconds_total{container!=""}[%s]))' % span)),
                              ("cpu_reserved", one('avg_over_time((sum(kube_pod_container_resource_requests{resource="cpu"} %s))[%s:15m])' % (running, span))),
                              ("mem_total", one('sum(kube_node_status_allocatable{resource="memory"})') / 1024 ** 3),
                              ("mem_used", one('avg_over_time((sum(container_memory_working_set_bytes{container!=""}))[%s:15m])' % span) / 1024 ** 3),
                              ("mem_reserved", one('avg_over_time((sum(kube_pod_container_resource_requests{resource="memory"} %s))[%s:15m])' % (running, span)) / 1024 ** 3)):
            out.append(("cluster", "all", metric, value))
        return out

    def save_daily(self, t):
        """Every finished Tehran day is saved once, so history outlives Prometheus's ~10 days. A few days per round, oldest first:
        the first run also saves every whole day Prometheus still holds. Very little data: kept for good."""
        if not self.integrations()["grafana"]:
            return
        start = self.cached("prom_start", 3600, lambda: next((float(s["value"][1]) for s in self.prom("prometheus_tsdb_lowest_timestamp_seconds")), None))
        if not start:
            return
        day, today, done = tday(start + 86400), tday(t), 0
        while day < today and done < 3:
            if not self.meta("daily:" + day):
                begin = tday_start(day)
                rows = self.day_rows(begin, begin + 86400)
                with self.lock:
                    self.conn.executemany("INSERT OR REPLACE INTO daily VALUES (?, ?, ?, ?, ?)", [(day,) + r for r in rows])
                    self.conn.commit()
                self.set_meta("daily:" + day, str(t))
                done += 1
            day = tday(tday_start(day) + 86400 + 3600)

    def daily(self, scope, frm=None, to=None):
        """Per Tehran day in the range: {"days": [...], "rows": {day: {name: {metric: value}}}, "partial": today's day or None}.
        Today has no saved row yet: it comes live from Prometheus, so far. Key rows are summed per user."""
        end = to or time.time()
        frm = frm or end - 30 * 86400
        d1, d2, today = tday(frm), tday(end), tday(time.time())
        rows = {}

        def put(day, name, metric, value):
            if scope == "key":
                name = name.split("\t", 1)[0]
            cell = rows.setdefault(day, {}).setdefault(name, {})
            cell[metric] = cell.get(metric, 0) + value
        for r in self.rows("SELECT day, name, metric, value FROM daily WHERE scope=? AND day>=? AND day<=?", (scope, d1, d2)):
            put(r["day"], r["name"], r["metric"], r["value"])
        partial = None
        if d2 == today and self.integrations()["grafana"]:
            live = self.cached("day_today:%s" % today, 300, lambda: self.day_rows(tday_start(today), time.time())) or []
            for s, name, metric, value in live:
                if s == scope:
                    put(today, name, metric, value)
                    partial = today
        return {"days": sorted(rows), "rows": rows, "partial": partial}

    def usage(self, frm=None, to=None):
        """Per user in a date range, from Grafana's Prometheus: LLM calls/tokens/spend/failures per key, CPU core-hours, GPU-hours and
        how busy those GPUs were. "requests" = every call: LiteLLM's requests metric counts successes only, and a rate over it is wrong
        (checked against LiteLLM's own logs: see data_check). {"from": epoch, "users": {user: {...}}}; {} without Grafana."""
        if not self.integrations()["grafana"]:
            return {}
        w = self.window(frm, to)
        span, at = "%ds" % w["secs"], int(w["to"])

        def fetch():
            users = {}
            row = lambda who: users.setdefault(who, {"requests": 0, "tokens": 0, "spend": 0, "failed": 0, "cpu_h": 0, "gpu_h": 0, "slice_h": 0,
                                                     "keys": {}, "models": {}})
            for what, metric in (("requests", "litellm_proxy_total_requests_metric_total"), ("tokens", "litellm_total_tokens_metric_total"),
                                 ("spend", "litellm_spend_metric_total"), ("failed", "litellm_proxy_failed_requests_metric_total")):
                for s in self.prom("sum by (api_key_alias, hashed_api_key, model, requested_model, user_email) (increase(%s[%s]))" % (metric, span), at):
                    m, v = s["metric"], float(s["value"][1])
                    alias, who = m.get("api_key_alias") or "None", self.key_owner(m)
                    if not v or not who:  # master key, or no alias and no email: no user to put it on
                        continue
                    r = row(who)
                    r[what] += v
                    k = r["keys"].setdefault(alias, {"hash": m.get("hashed_api_key", ""), "requests": 0, "tokens": 0, "spend": 0, "failed": 0})
                    k[what] += v
                    model = m.get("requested_model") or m.get("model")
                    if what == "requests" and model and model != "None":
                        r["models"][model] = r["models"].get(model, 0) + v
            for s in self.prom("sum by (api_key_alias, user_email, route) (increase(litellm_proxy_total_requests_metric_total[%s]))" % span, at):
                who, v = self.key_owner(s["metric"]), round(float(s["value"][1]))
                if who and v:
                    rt = row(who).setdefault("routes", {})
                    rt[s["metric"].get("route", "?")] = rt.get(s["metric"].get("route", "?"), 0) + v
            # LiteLLM key budgets (USD): max and what is left; +Inf / missing = no limit
            for field, q in (("budget", "max by (api_key_alias) (litellm_api_key_max_budget_metric)"),
                             ("left", "min by (api_key_alias) (litellm_remaining_api_key_budget_metric)")):
                for s in self.prom(q):
                    alias, v = s["metric"].get("api_key_alias") or "None", float(s["value"][1])
                    who = self.key_owner(s["metric"])
                    if who and who in users and alias in users[who]["keys"] and v != float("inf"):
                        users[who]["keys"][alias][field] = round(v, 2)
            for what, q in (("cpu_h", 'sum by (namespace) (increase(container_cpu_usage_seconds_total{namespace=~"user-.*",container!=""}[%s])) / 3600' % span),
                            ("gpu_h", GPU_HELD_H % ('namespace=~"user-.*"', span)),  # running pods only: a pending pod holds no GPU
                            ("slice_h", SLICE_HELD_H % ('namespace=~"user-.*"', span))):
                for s in self.prom(q, at):
                    row(s["metric"]["namespace"][5:])[what] += float(s["value"][1])
            # the highest use in the range, to set against the agent's limit (Prometheus's days only)
            for field, q in (("cpu_peak", 'max_over_time((sum by (namespace) (rate(container_cpu_usage_seconds_total{namespace=~"user-.*",container!=""}[5m])))[%s:15m])' % span),
                             ("mem_peak", 'max_over_time((sum by (namespace) (container_memory_working_set_bytes{namespace=~"user-.*",container!=""}))[%s:15m]) / 1073741824' % span)):
                for s in self.prom(q, at):
                    row(s["metric"]["namespace"][5:])[field] = round(float(s["value"][1]), 1)
            for s in self.prom('avg by (namespace) (avg_over_time(DCGM_FI_DEV_GPU_UTIL{namespace=~"user-.*"}[%s]))' % span, at):
                row(s["metric"]["namespace"][5:])["gpu_util"] = round(float(s["value"][1]), 1)
            d1, d2 = w["hist"]  # days older than Prometheus: from the daily history
            for x in self.rows("SELECT name, metric, sum(value) v FROM daily WHERE scope='key' AND day>=? AND day<=? GROUP BY name, metric", (d1, d2)):
                who, alias = x["name"].split("\t", 1)
                r = row(who)
                r[x["metric"]] += x["v"]
                k = r["keys"].setdefault(alias, {"hash": "", "requests": 0, "tokens": 0, "spend": 0, "failed": 0})
                k[x["metric"]] += x["v"]
            for ns, h, u in self.ns_history(d1, d2, 'user-%'):
                r = row(ns[5:])
                held = r["gpu_h"]
                r["gpu_util"] = round((r.get("gpu_util", 0) * held + u * h) / (held + h), 1) if held + h else r.get("gpu_util")
                r["gpu_h"] = held + h
            for x in self.rows("SELECT name, sum(value) v FROM daily WHERE scope='ns' AND metric='cpu_h' AND name LIKE 'user-%' AND day>=? AND day<=? GROUP BY name", (d1, d2)):
                row(x["name"][5:])["cpu_h"] += x["v"]
            for x in self.rows("SELECT name, sum(value) v FROM daily WHERE scope='ns' AND metric='slice_h' AND name LIKE 'user-%' AND day>=? AND day<=? GROUP BY name", (d1, d2)):
                row(x["name"][5:])["slice_h"] += x["v"]
            for r in users.values():
                for d in [r] + list(r["keys"].values()):
                    # a share of all calls, so it can never pass 100 %
                    d["fail_pct"] = round(100 * min(d["failed"], d["requests"]) / d["requests"], 1) if d["requests"] else None
                    for f in ("requests", "tokens", "failed", "cpu_h", "gpu_h", "slice_h"):
                        if f in d:
                            d[f] = round(d[f])
                    d["spend"] = round(d["spend"], 2)
                r["models"] = {m: round(n) for m, n in sorted(r["models"].items(), key=lambda x: -x[1])}
            return {"from": w["data_from"], "users": users}
        return self.cached("usage:%d:%d" % (w["from"] // 300, w["to"] // 300), 300, fetch) or {}

    def ns_history(self, d1, d2, like="%"):
        """(namespace, GPU hours, how busy they were, weighted by those hours) over saved days."""
        got = {}
        for x in self.rows("SELECT day, name, metric, value FROM daily WHERE scope='ns' AND metric IN ('gpu_h','gpu_util') AND name LIKE ? AND day>=? AND day<=?", (like, d1, d2)):
            got.setdefault((x["day"], x["name"]), {})[x["metric"]] = x["value"]
        out = {}
        for (_, ns), v in got.items():
            h, u = v.get("gpu_h", 0), v.get("gpu_util", 0)
            a = out.setdefault(ns, [0, 0])
            a[0], a[1] = a[0] + h, a[1] + u * h
        return [(ns, h, b / h if h else 0) for ns, (h, b) in out.items()]

    def resources(self, frm=None, to=None):
        """The whole cluster: what we have, what holds it now, how busy it is, and every namespace's share over the range."""
        if not self.integrations()["grafana"]:
            return {}
        w = self.window(frm, to)
        span, at = "%ds" % w["secs"], int(w["to"])
        one = lambda q, t=None: next((float(s["value"][1]) for s in self.prom(q, t)), 0.0)

        def fetch():
            gpu_req = 'kube_pod_container_resource_requests{resource="nvidia_com_gpu"} * on (namespace,pod) group_left () (kube_pod_status_phase{phase="%s"} == 1)'
            total = {r: one('sum(kube_node_status_allocatable{resource="%s"})' % r) for r in ("nvidia_com_gpu", "cpu", "memory")}
            hours, gib = max(w["all_hours"], 1 / 60), 1024 ** 3
            d1, d2 = w["hist"]
            hist = {x["metric"]: x["v"] for x in self.rows("SELECT metric, avg(value) v FROM daily WHERE scope='cluster' AND day>=? AND day<=? GROUP BY metric", (d1, d2))}
            blend = lambda value, metric: (value * w["hours"] + hist.get(metric, value) * w["hist_hours"]) / max(w["all_hours"], 1 / 60)  # weighted by hours
            cards = {}  # one row per physical GPU; a GPU with no pod has no pod label
            for s in self.prom("DCGM_FI_DEV_GPU_UTIL"):
                m = s["metric"]
                cards[m.get("UUID")] = {"node": m.get("Hostname") or m.get("kubernetes_node"), "gpu": m.get("gpu"), "model": (m.get("modelName") or "").replace("NVIDIA ", ""),
                                        "ns": m.get("namespace"), "pod": m.get("pod"), "util": float(s["value"][1]), "util_avg": None, "mem_used": None, "mem_total": None}
            for s in self.prom("avg by (UUID) (avg_over_time(DCGM_FI_DEV_GPU_UTIL[%s]))" % span, at):
                if s["metric"].get("UUID") in cards:
                    cards[s["metric"]["UUID"]]["util_avg"] = round(float(s["value"][1]), 1)
            for field, q in (("mem_used", "max by (UUID) (DCGM_FI_DEV_FB_USED)"), ("mem_free", "max by (UUID) (DCGM_FI_DEV_FB_FREE)")):
                for s in self.prom(q):
                    if s["metric"].get("UUID") in cards:
                        cards[s["metric"]["UUID"]][field] = float(s["value"][1])
            for c in cards.values():
                used, free = c.pop("mem_used", None), c.pop("mem_free", None)
                c["mem_used"], c["mem_total"] = (round(used / 1024), round((used + free) / 1024)) if used is not None and free is not None else (None, None)
            rows = {}
            row = lambda ns: rows.setdefault(ns, {"ns": ns, "cpu_h": 0, "mem_gib": 0, "gpu_h": 0, "slice_h": 0, "net_gb": 0, "gpu_util": None})
            for field, q in (("cpu_h", 'sum by (namespace) (increase(container_cpu_usage_seconds_total{container!=""}[%s])) / 3600' % span),
                             ("mem_gib", 'sum by (namespace) (avg_over_time((sum by (namespace) (container_memory_working_set_bytes{container!=""}))[%s:15m])) / %d' % (span, gib)),
                             ("gpu_h", GPU_HELD_H % ('namespace!=""', span)),
                             ("slice_h", SLICE_HELD_H % ('namespace!=""', span)),
                             ("net_gb", "sum by (namespace) (increase(container_network_receive_bytes_total[%s])) / 1000000000" % span),
                             ("gpu_util", "avg by (namespace) (avg_over_time(DCGM_FI_DEV_GPU_UTIL{namespace!=\"\"}[%s]))" % span)):
                for s in self.prom(q, at):
                    ns = s["metric"].get("namespace")
                    if ns:
                        row(ns)[field] = round(float(s["value"][1]), 1)
            for x in self.rows("SELECT name, metric, sum(value) v FROM daily WHERE scope='ns' AND metric IN ('cpu_h','net_gb','slice_h') AND day>=? AND day<=? GROUP BY name, metric", (d1, d2)):
                row(x["name"])[x["metric"]] = round(row(x["name"])[x["metric"]] + x["v"], 1)
            for x in self.rows("SELECT name, avg(value) v FROM daily WHERE scope='ns' AND metric='mem_gib' AND day>=? AND day<=? GROUP BY name", (d1, d2)):
                r = row(x["name"])
                r["mem_gib"] = round((r["mem_gib"] * w["hours"] + x["v"] * w["hist_hours"]) / max(w["all_hours"], 1 / 60), 1)
            for ns, h, u in self.ns_history(d1, d2):
                r = row(ns)
                total_h = r["gpu_h"] + h
                r["gpu_util"] = round(((r["gpu_util"] or 0) * r["gpu_h"] + u * h) / total_h, 1) if total_h else r["gpu_util"]
                r["gpu_h"] = round(total_h, 1)
            for r in rows.values():
                r["cpu_pct"] = round(100 * r["cpu_h"] / (total["cpu"] * hours), 1) if total["cpu"] else None
                r["gpu_pct"] = round(100 * r["gpu_h"] / (total["nvidia_com_gpu"] * hours), 1) if total["nvidia_com_gpu"] else None
                r["kind"] = "user" if r["ns"].startswith("user-") else "models" if r["ns"] == OUR_NS else "service" if r["ns"] in SERVICE_NS else "other"
            util = [c["util_avg"] for c in cards.values() if c["util_avg"] is not None]
            # MIG slices, and everything that cannot start for lack of a GPU or a slice
            gpu_any = 'kube_pod_container_resource_requests{resource=~"nvidia_com_.*"} * on (namespace,pod) group_left () (kube_pod_status_phase{phase="%s"} == 1)'
            is_slice = lambda m: "mig" in (m.get("resource") or "")
            held_slices = [{"ns": s["metric"].get("namespace"), "pod": s["metric"].get("pod"), "n": float(s["value"][1])} for s in self.prom(gpu_any % "Running") if is_slice(s["metric"])]
            pending = [s["metric"] for s in self.prom(gpu_any % "Pending")]
            since = {}
            if pending:
                for s in self.prom('kube_pod_created{pod=~"%s"}' % "|".join(sorted({m.get("pod", "") for m in pending}))):
                    since[s["metric"].get("pod")] = float(s["value"][1])
            people = self.people()
            who = lambda ns: next((p["name"] for p in people if ns and ns.startswith("user-") and ns[5:] in p["users"]), None)
            waiting = [{"ns": m.get("namespace"), "pod": m.get("pod"), "kind": "slice" if is_slice(m) else "full GPU", "since": since.get(m.get("pod")), "person": who(m.get("namespace"))} for m in pending]
            held = one("sum(%s)" % (gpu_req % "Running"))
            return {"window": w, "gpus": sorted(cards.values(), key=lambda c: (str(c["node"]), int(c["gpu"] or 0))),
                    "slices": {"total": one('sum(kube_node_status_allocatable{resource=~"nvidia_com_mig.*"})'), "held": sum(x["n"] for x in held_slices),
                               "pods": sorted(held_slices, key=lambda x: (x["ns"] or "", x["pod"] or ""))},
                    "waiting": sorted(waiting, key=lambda x: x["since"] or 0),
                    "gpu": {"total": total["nvidia_com_gpu"], "held": held, "waiting": one("sum(%s)" % (gpu_req % "Pending")),
                            "busy_now": round(sum(c["util"] for c in cards.values()) / len(cards), 1) if cards else None,
                            "busy_avg": round(blend(sum(util) / len(util), "gpu_busy"), 1) if util else None,
                            "held_avg_pct": round(100 * blend(one("avg_over_time((sum(%s))[%s:15m])" % (gpu_req % "Running", span), at), "gpu_held") / total["nvidia_com_gpu"], 1) if total["nvidia_com_gpu"] else None},
                    "cpu": {"total": total["cpu"], "used_now": round(one('sum(rate(container_cpu_usage_seconds_total{container!=""}[5m]))'), 1),
                            "used_avg": round(blend(one('sum(rate(container_cpu_usage_seconds_total{container!=""}[%s]))' % span, at), "cpu_used"), 1),
                            "requested": round(one('sum(kube_pod_container_resource_requests{resource="cpu"} * on (namespace,pod) group_left () (kube_pod_status_phase{phase="Running"} == 1))'), 1)},
                    "mem": {"total": round(total["memory"] / gib, 1), "used_now": round(one('sum(container_memory_working_set_bytes{container!=""})') / gib, 1),
                            "used_avg": round(blend(one('avg_over_time((sum(container_memory_working_set_bytes{container!=""}))[%s:15m])' % span, at) / gib, "mem_used"), 1),
                            "requested": round(one('sum(kube_pod_container_resource_requests{resource="memory"} * on (namespace,pod) group_left () (kube_pod_status_phase{phase="Running"} == 1))') / gib, 1)},
                    "namespaces": sorted(rows.values(), key=lambda r: (-r["gpu_h"], -r["cpu_h"]))}
        return self.cached("resources:%d:%d" % (w["from"] // 300, w["to"] // 300), 300, fetch) or {}

    def data_check(self, t):
        """Hourly proof of the numbers on the page: the same figure from two systems. Calls: Prometheus against LiteLLM's own logs.
        Tokens: Prometheus against Langfuse. GPUs: DCGM against Kubernetes. Shown on Capacity."""
        on = self.integrations()
        if not on["grafana"] or t - (self.checks["at"] or 0) < 3600:
            return
        rows, end, start = [], t - 300, t - 300 - 7 * 86400  # 5 min back: LiteLLM writes its logs in batches
        total = lambda metric, alias: next((float(s["value"][1]) for s in self.prom('sum(increase(%s{api_key_alias="%s"}[%ds]))' % (metric, alias, end - start), int(end))), 0.0)
        top = [s["metric"]["api_key_alias"] for s in self.prom("topk(5, sum by (api_key_alias) (increase(litellm_proxy_total_requests_metric_total{api_key_alias!=\"None\"}[7d])))", int(end))
               if s["metric"].get("api_key_alias")]

        def add(what, name_a, a, name_b, b):
            diff = abs(a - b)
            pct = 100 * diff / max(a, b) if max(a, b) else 0
            rows.append({"what": what, "a_name": name_a, "a": round(a), "b_name": name_b, "b": round(b), "diff_pct": round(pct, 1), "ok": pct <= CHECK_OK_PCT or diff <= 5})
        for alias in top:
            if on["keys"]:
                add("LLM calls · " + alias, "Prometheus", total("litellm_proxy_total_requests_metric_total", alias), "LiteLLM logs", self.spend_count(start, end, alias))
                add("Failed calls · " + alias, "Prometheus", total("litellm_proxy_failed_requests_metric_total", alias), "LiteLLM logs", self.spend_count(start, end, alias, "failure"))
            if on["langfuse"]:
                iso = lambda x: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(x))
                q = {"view": "observations", "dimensions": [], "metrics": [{"measure": "totalTokens", "aggregation": "sum"}], "fromTimestamp": iso(start), "toTimestamp": iso(end),
                     "filters": [{"column": "metadata", "key": "user_api_key_alias", "type": "stringObject", "operator": "=", "value": alias},
                                 {"column": "type", "operator": "=", "value": "GENERATION", "type": "string"}]}
                data = self.lf("/api/public/metrics", query=json.dumps(q))["data"]
                add("Tokens · " + alias, "Prometheus", total("litellm_total_tokens_metric_total", alias), "Langfuse", float((data[0].get("sum_totalTokens") or 0) if data else 0))
        add("GPUs in the cluster", "GPU exporter", len({s["metric"].get("UUID") for s in self.prom("DCGM_FI_DEV_GPU_UTIL")}), "Kubernetes",
            next((float(s["value"][1]) for s in self.prom('sum(kube_node_status_capacity{resource="nvidia_com_gpu"})')), 0.0))
        self.checks = {"at": t, "rows": rows}

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
                elif kind == "tickets":
                    try:
                        tickets.sync(self)
                    finally:
                        self.tickets_busy = False
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
        for step in ([self.check_probe] + ([self.scan_logs, self.test_models, self.check_llm_keys, self.refresh_keys, self.backfill_ticket_spend, self.refresh_logins, self.refresh_tasks, self.data_check, self.save_daily] if slow else []) + [self.judge_changes]):
            try:
                step(t)
            except Exception as e:
                errors.append("%s: %s" % (step.__name__, probe.err(e)))
        # tried, not only synced: while the site is down, one login every SYNC_S and not one per round
        if tickets.enabled() and not self.tickets_busy and t - max(float(self.meta("tickets_synced") or 0), self.tickets_tried) > tickets.SYNC_S:
            self.tickets_busy, self.tickets_tried = True, t
            self.bg.put(("tickets", None))
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
        for key, o in inv.items():  # liveness of every workload and model: its state, since when, checked when
            if o["kind"] in ("Deployment", "StatefulSet", "DaemonSet", "Model"):
                want = o["min"] if o["kind"] == "Model" else o["desired"]
                o["state"] = "ready" if o["ready"] >= want else "not ready"
                o["since"], o["checked"] = self.mark(key, o["state"], t), t
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
            self.mark("probe:" + name, "up" if ok else "down", data["ts"])
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
                return job, kget("/api/v1/namespaces/%s/pods/%s/log?container=%s&sinceSeconds=%d&tailLines=%d&timestamps=true"
                                 % (ns, p["name"], c, ROUND_S * SLOW_EVERY + 30, LOG_TAIL), raw=True)
            except Exception:
                return job, ""
        seen = {}  # (ns, sig) -> [workload, pod, sample line, count]
        with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
            for (ns, p, c), text in ex.map(read, jobs):
                mark, fresh, newest = (ns, p["name"], c), [], None
                for line in text.splitlines():
                    if LOG_STAMP_RE.match(line):
                        stamp, line = line.split(" ", 1)
                        if stamp > self.log_marks.get(mark, ""):
                            fresh.append(stamp + " " + mask_log(line))
                            newest = max(newest or stamp, stamp)
                    if ERROR_RE.search(line[:LEVEL_ZONE]):
                        sig = signature(line) if ns == OUR_NS else signature(ns + " " + line)  # our old signatures stay valid
                        seen.setdefault((ns, sig), [p["workload"], p["name"], line[:500], 0])[3] += 1
                if fresh:
                    self.save_log(ns, p["name"], c, t, fresh)
                    self.log_marks[mark] = newest
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

    @staticmethod
    def save_log(ns, pod, container, t, lines):
        path = LOG_DIR / time.strftime("%Y-%m-%d", time.gmtime(t)) / ns / ("%s__%s.log.gz" % (pod, container))
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with gzip.open(path, "at", encoding="utf-8", errors="replace") as f:  # gzip allows appended parts
            f.write("\n".join(lines) + "\n")

    @staticmethod
    def prune_logs(t):
        """Keep 30 days and at most LOG_CAP_BYTES: the oldest days go first."""
        if not LOG_DIR.exists():
            return
        days = sorted(p for p in LOG_DIR.iterdir() if p.is_dir())
        size = {d: sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) for d in days}
        keep_from = time.strftime("%Y-%m-%d", time.gmtime(t - RETENTION))
        for d in days:
            if d.name < keep_from or sum(size.values()) > LOG_CAP_BYTES:
                shutil.rmtree(d, ignore_errors=True)
                del size[d]

    @staticmethod
    def log_archive(ns=None):
        """Saved logs: namespaces with their size, or one namespace's pods with the days saved."""
        days = sorted((p for p in LOG_DIR.iterdir() if p.is_dir()), reverse=True) if LOG_DIR.exists() else []
        out = {"from": days[-1].name if days else None, "namespaces": {}, "pods": {}}
        for d in days:
            for n in d.iterdir():
                b = sum(f.stat().st_size for f in n.iterdir())
                out["namespaces"][n.name] = out["namespaces"].get(n.name, 0) + b
                if n.name == ns:
                    for f in n.iterdir():
                        pod, container = f.name[:-len(".log.gz")].split("__", 1)
                        r = out["pods"].setdefault(pod + "/" + container, {"pod": pod, "container": container, "days": [], "bytes": 0})
                        r["days"].append(d.name)
                        r["bytes"] += f.stat().st_size
        out["pods"] = sorted(out["pods"].values(), key=lambda r: r["days"][0], reverse=True)
        return out

    @staticmethod
    def log_read(ns, pod, container, day, find="", tail=2000):
        """The last lines of one saved day, optionally only those holding `find`. Callers check the names."""
        path = LOG_DIR / day / ns / ("%s__%s.log.gz" % (pod, container))
        low = find.lower()
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            return "".join(collections.deque((x for x in f if low in x.lower()), maxlen=tail))

    def task_log(self, tid):
        """The newest log lines of one mlops-core task (kept by mlops-core, so also for pods that are gone)."""
        if not any(x["id"] == tid for x in self.task_rows["rows"]):
            raise KeyError("no such task")
        basic = base64.b64encode((os.environ["MLOPS_KEY"] + ":" + os.environ["MLOPS_SECRET"]).encode()).decode()
        token = "Bearer " + self.mread("auth.login", "Basic " + basic, {})["token"]
        r = self.mread("events.get_task_log", token, {"task": tid, "batch_size": 500, "navigate_earlier": True})
        lines = sorted(r.get("events", []), key=lambda e: e.get("timestamp") or 0)
        return {"total": r.get("total"), "lines": [{"ts": (e.get("timestamp") or 0) / 1000, "level": e.get("level"), "msg": mask_log(str(e.get("msg")))} for e in lines]}

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
        elif symptom == "llm-failing":
            codes = ctx.get("codes", {})
            five = sum(n for c, n in codes.items() if c.startswith("5"))
            if five > sum(codes.values()) / 2:
                owner, cause = "us", "the model server answers 5xx (%s): our model behind the gateway is failing" % ", ".join(ctx.get("models", [])[:3])
                fix = "check the model pods in aip-mlops-service (Models page) and LiteLLM; the user's request is not the cause"
            else:
                cause = "the gateway rejects the user's requests (%s): wrong model name, bad request or rate limit" % ", ".join(sorted(codes))
                fix = "tell the user the error from their page (Langfuse); nothing to fix on our side"
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
        if self.integrations()["ai"]:
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
        self.prune_logs(t)
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

    def incidents(self, state="open", kind=None, limit=300, frm=0, to=None):
        """Incidents open at any moment of the range."""
        rows = self.rows("SELECT id, ns, obj, symptom, owner, cause, detail, opened, last_seen, closed, scope FROM incidents "
                         "WHERE state=? AND opened<=? AND (closed IS NULL OR closed>=?) ORDER BY id DESC LIMIT ?", (state, to or time.time(), frm, limit))
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

    def changes(self, days=1, typ=None, frm=None, to=None):
        rows = self.rows("SELECT * FROM changes WHERE ts>=? AND ts<=? ORDER BY ts DESC LIMIT 500", (frm or time.time() - days * 86400, to or time.time()))
        groups = {"new-service": ("new-service", "new-host", "new-namespace"), "release": ("release", "config"),
                  "new-model": ("new-model", "model-changed", "model-removed")}
        rows = [r for r in rows if not typ or r["type"] in groups.get(typ, (typ,))]
        for r in rows:
            r["info"] = json.loads(r["info"] or "{}")
        return rows

    def models(self, frm=None, to=None):
        on = self.integrations()
        gpu = self.gpu_util() if on["grafana"] else {}
        use = self.model_use(frm, to)
        llm_names = (self.litellm_models() if on["litellm"] else None)
        counts = {r["obj"]: r["n"] for r in self.rows("SELECT obj, count(*) n FROM incidents WHERE state='open' AND ns=? GROUP BY obj", (OUR_NS,))}
        out = []
        for o in sorted((o for o in self.inv.get(OUR_NS, {}).values() if o["kind"] == "Model"), key=lambda o: o["name"]):
            pods = [p for p in self.pods.get(OUR_NS, []) if p["workload"] == o["name"]]
            g = [gpu[p["name"]] for p in pods if p["name"] in gpu]
            out.append(dict(o, pods=pods, open=counts.get(o["name"], 0), test=self.model_tests.get(o["name"]), gpu=round(sum(g) / len(g), 1) if g else None, use=use.get(o["name"]),
                            litellm=None if llm_names is None else any(o["name"] == n or o["name"] in n for n in llm_names)))
        return out

    def model_use(self, frm=None, to=None):
        """Per model in the range: all calls, failures, and how busy its GPUs were."""
        if not self.integrations()["grafana"]:
            return {}
        w = self.window(frm, to)
        span, at = "%ds" % w["secs"], int(w["to"])

        def fetch():
            out = {}
            row = lambda m: out.setdefault(m, {"calls": 0, "failed": 0, "gpu_avg": None})
            for field, metric in (("calls", "litellm_proxy_total_requests_metric_total"), ("failed", "litellm_proxy_failed_requests_metric_total")):
                for s in self.prom("sum by (requested_model) (increase(%s[%s]))" % (metric, span), at):
                    if s["metric"].get("requested_model"):
                        row(s["metric"]["requested_model"])[field] = round(float(s["value"][1]))
            for s in self.prom('avg by (pod) (avg_over_time(DCGM_FI_DEV_GPU_UTIL{pod=~"model-.*"}[%s]))' % span, at):
                name = s["metric"]["pod"][len("model-"):].rsplit("-", 2)[0]  # model-<name>-<replicaset>-<pod>
                row(name)["gpu_avg"] = round(float(s["value"][1]), 1)
            for x in self.rows("SELECT name, metric, sum(value) v FROM daily WHERE scope='model' AND metric IN ('calls','failed') AND day>=? AND day<=? GROUP BY name, metric", w["hist"]):
                row(x["name"])[x["metric"]] += round(x["v"])
            for r in out.values():
                r["fail_pct"] = round(100 * min(r["failed"], r["calls"]) / r["calls"], 1) if r["calls"] else None
            return out
        return self.cached("model_use:%d:%d" % (w["from"] // 300, w["to"] // 300), 300, fetch) or {}

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

    def mark(self, key, state, t):
        """Liveness: since when has `key` been in `state`? A new state starts a new 'since'. Returns the since time."""
        old = self.states.get(key)
        if old and old[0] == state:
            return old[1]
        self.states[key] = (state, t)
        self.exec("INSERT OR REPLACE INTO state_since VALUES (?, ?, ?)", (key, state, t))
        return t

    def record_restarts(self, ns, pods, t):
        """Restart counter per workload, first and last value of each day: the day's restarts = last - first."""
        day, per = time.strftime("%Y-%m-%d", time.gmtime(t)), {}
        for p in pods:
            per[p["workload"]] = per.get(p["workload"], 0) + p["restarts"]
        with self.lock:
            self.conn.executemany("INSERT INTO restarts VALUES (?,?,?,?,?) ON CONFLICT(day, ns, wl) DO UPDATE SET last=excluded.last, "
                                  "first=min(first, excluded.first)", [(day, ns, wl, n, n) for wl, n in per.items()])
            self.conn.commit()

    def user_pods(self):
        """{user: [(ns, pod)]}: everything running in user-<name>, plus each user's JupyterHub notebook."""
        out = {}
        for ns, pods in self.pods.items():
            for p in pods:
                who = ns[5:] if ns.startswith("user-") else p.get("jhub_user")
                if who:
                    out.setdefault(who, []).append((ns, p))
        return out

    def users(self, frm=None, to=None):
        """One row per user: what they run now, and in the date range: what it used, what broke, what changed."""
        gib, t, rows = 1024 ** 3, time.time(), []
        end = to or t
        frm = frm or end - 30 * 86400
        nss = {}
        for who, items in self.user_pods().items():
            for ns, _ in items:
                nss.setdefault(who, set()).add(ns)
        for who, items in sorted(self.user_pods().items()):
            live = [p for _, p in items if p["phase"] == "Running"]
            ns_list = sorted(nss[who])
            q = ",".join("?" * len(ns_list))
            inc = self.one("SELECT count(*) n FROM incidents WHERE state='open' AND ns IN (%s)" % q, ns_list)["n"]
            chg = self.one("SELECT count(*) n, max(ts) last FROM changes WHERE ts>=? AND ts<=? AND ns IN (%s)" % q, [frm, end] + ns_list)
            gpu = {}
            for p in live:
                for k, n in p["res"]["gpu"].items():
                    gpu[k.replace("nvidia.com/", "")] = gpu.get(k.replace("nvidia.com/", ""), 0) + n
            rows.append({"user": who, "namespaces": ns_list, "pods": len(items), "running": len(live),
                         "pending": sum(p["phase"] == "Pending" for _, p in items),
                         "kinds": sorted({"notebook" if p.get("jhub_user") else "agent" if p["workload"].endswith("clearml-agent")
                                          else "workload" for _, p in items}),
                         "cpu_use": round(sum(p.get("use_cpu") or 0 for p in live), 2),
                         "cpu_req": round(sum(p["res"]["cpu_req"] for p in live), 1),
                         "mem_use_gib": round(sum(p.get("use_mem") or 0 for p in live) / gib, 1),
                         "gpu": ", ".join("%s×%d" % kv for kv in sorted(gpu.items())),
                         "restarts": sum(p["restarts"] for _, p in items), "open": inc,
                         "changes": chg["n"], "last_change": chg["last"],
                         "started": min((p["started"] or t for _, p in items), default=None)})
        use = self.usage(frm, end).get("users", {})
        for who in sorted(set(use) - {r["user"] for r in rows}):  # API key only, nothing running
            rows.append({"user": who, "namespaces": [], "pods": 0, "running": 0, "pending": 0, "kinds": ["API key"], "cpu_use": 0,
                         "cpu_req": 0, "mem_use_gib": 0, "gpu": "", "restarts": 0, "open": 0, "changes": 0, "last_change": None,
                         "started": None})
        people, sso = self.people(), self.sso().get("users", {})
        logins = {r["user"]: r for r in self.rows("SELECT user, max(CASE WHEN result='ok' THEN ts END) last_ok, sum(result!='ok' AND ts>=? AND ts<=?) failed, "
                                                  "max(CASE WHEN result!='ok' THEN ts END) last_failed FROM logins GROUP BY user", (frm, end))}
        train = {}
        for x in self.tasks(frm, end)["tasks"]:
            c = train.setdefault(x["user"], {"running": 0, "queued": 0, "failed": 0, "completed": 0, "last": 0})
            k = {"in_progress": "running", "queued": "queued", "failed": "failed", "completed": "completed"}.get(x["status"])
            if k:
                c[k] += 1
            c["last"] = max(c["last"], x["last"] or 0)
        day = self.daily("key", frm, end)
        for r in rows:
            r["use"], r["sso"], r["login"], r["train"] = use.get(r["user"]), sso.get(r["user"]), logins.get(r["user"]), train.get(r["user"])
            r["spark"] = [round(day["rows"].get(d, {}).get(r["user"], {}).get("requests", 0)) for d in day["days"]]
            r["limit"] = self.agent_limit(r["user"])
            p = next((p for p in people if r["user"] in p["users"]), None)
            r["person"] = p and {"name": p["name"], "users": p["users"], "remove": bool(p.get("remove"))}
        # one person's namespaces next to each other (Avir: aviraip, aviraip2)
        rows = [r for r in rows if not (r["person"] and r["person"]["remove"])]  # the name list says "to delete": off this list
        return sorted(rows, key=lambda r: (r["person"]["users"][0] if r["person"] else r["user"], r["user"]))

    @staticmethod
    def people():
        """[{name, account, users, keys, remove}]: real names of accounts, from the team's name list (data/people.json, not in git).
        remove = the list says "to delete": hidden from the Users list, but we never delete anything."""
        try:
            return json.loads((DATA / "people.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def agent_limit(self, who):
        """A user's real compute limit: no quota object exists, so it is what their agent pod may use. None without an agent."""
        pods = [p for _, p in self.user_pods().get(who, []) if p["workload"].endswith("clearml-agent")]
        p = next((p for p in pods if p["phase"] == "Running"), pods[0] if pods else None)
        if not p:
            return None
        gpu = p["res"]["gpu"]
        return {"cpu": p["res"]["cpu_lim"], "mem_gib": round(p["res"]["mem_lim"] / 1024 ** 3, 1), "gpu": sum(n for k, n in gpu.items() if "mig" not in k),
                "slices": sum(n for k, n in gpu.items() if "mig" in k), "waiting": p["phase"] == "Pending", "since": p.get("created")}

    def user(self, who, frm=None, to=None):
        now = to or time.time()
        frm = frm or now - 30 * 86400
        items, use = self.user_pods().get(who), self.usage(frm, now)
        if items is None and who not in use.get("users", {}):
            raise KeyError("no such user")
        items = items or []
        ns_list = sorted({ns for ns, _ in items})
        q = ",".join("?" * len(ns_list))
        return {"user": who, "namespaces": ns_list, "pods": [dict(p, ns=ns) for ns, p in items],
                "use": use.get("users", {}).get(who), "use_from": use.get("from"), "key_rows": [k for k in self.keys()["keys"] if k["user"] == who], "range": self.window(frm, now) if self.integrations()["grafana"] else None,
                "limit": self.agent_limit(who), "sso": self.sso().get("users", {}).get(who), "sso_from": self.sso().get("from"),
                "task_rows": [x for x in self.tasks(frm, now)["tasks"] if x["user"] == who][:30],
                "login_rows": self.rows("SELECT ts, result FROM logins WHERE user=? AND ts>=? AND ts<=? ORDER BY ts DESC LIMIT 30", (who, frm, now)),
                "person": next(({"name": p["name"], "users": p["users"], "remove": bool(p.get("remove"))} for p in self.people() if who in p["users"]), None),
                "key_people": {k: p["name"] for p in self.people() for k in p["keys"]},
                "llm_log": self.llm_log(who, frm, now), "llm_error": self.integ_err.get(
                    "lf:%s:%d:%d" % (who, frm // 300, now // 300)),
                "incidents": self.rows("SELECT id, ns, obj, symptom, owner, cause, opened, closed, state FROM incidents WHERE ns IN (%s) "
                                       "ORDER BY id DESC LIMIT 50" % q, ns_list),
                "changes": self.rows("SELECT * FROM changes WHERE ns IN (%s) ORDER BY ts DESC LIMIT 50" % q, ns_list)}

    def tickets(self):
        """Support tickets, newest activity first, each with the six answers (computed now, against current data)."""
        out = []
        for r in self.rows("SELECT * FROM tickets ORDER BY id DESC"):
            d, ai = json.loads(r["data"]), json.loads(r["ai"]) if r["ai"] else None
            a = tickets.analyze(self, d, ai)
            out.append({k: d.get(k) for k in ("id", "number", "subject", "requester", "assignee", "category", "status", "priority", "last", "created")}
                       | {"fetched": r["fetched"], "seen": r["seen"], "answers": {k: {"status": v["status"], "answer": v["answer"]} for k, v in a.items()}})
        return {"tickets": sorted(out, key=lambda x: -(x["last"] or 0)), "synced": float(self.meta("tickets_synced") or 0) or None,
                "enabled": tickets.enabled(), "error": self.integ_err.get("tickets"), "ai": self.integrations()["ai"]}

    def ticket(self, tid):
        r = self.one("SELECT * FROM tickets WHERE id=?", (tid,))
        if not r:
            raise KeyError("no ticket %s" % tid)
        d, ai = json.loads(r["data"]), json.loads(r["ai"]) if r["ai"] else None
        return dict(d, fetched=r["fetched"], seen=r["seen"], ai=ai, answers=tickets.analyze(self, d, ai))

    def capacity(self, frm=None, to=None):
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
        return {"resources": self.resources(frm, to), "checks": self.checks, "namespaces": rows, "near_limit": sorted(near, key=lambda r: -r["pct"]), "idle": sorted(idle, key=lambda r: -r["cpu_req"]),
                "gpus": sorted(gpus, key=lambda r: (r["ns"], r["pod"]))}

    def trends(self, days=14, frm=None, to=None):
        """Per day in the range: incidents opened/closed, changes, mean time to close; services that break or restart most."""
        t = to or time.time()
        since = frm or t - days * 86400
        day = "strftime('%Y-%m-%d', {}, 'unixepoch', '+210 minutes')"  # Tehran calendar day
        per = {}
        for r in self.rows("SELECT %s d, count(*) n FROM incidents WHERE opened>=? AND opened<=? GROUP BY d" % day.format("opened"), (since, t)):
            per.setdefault(r["d"], {})["opened"] = r["n"]
        for r in self.rows("SELECT %s d, count(*) n, avg(closed - opened) m FROM incidents WHERE closed>=? AND closed<=? GROUP BY d" % day.format("closed"),
                           (since, t)):
            per.setdefault(r["d"], {}).update(closed=r["n"], fix_min=round(r["m"] / 60) if r["m"] else None)
        for r in self.rows("SELECT %s d, count(*) n FROM changes WHERE ts>=? AND ts<=? GROUP BY d" % day.format("ts"), (since, t)):
            per.setdefault(r["d"], {})["changes"] = r["n"]
        for r in self.rows("SELECT day d, sum(last - first) n FROM restarts WHERE day>=? AND day<=? GROUP BY day",
                           (time.strftime("%Y-%m-%d", time.gmtime(since)), time.strftime("%Y-%m-%d", time.gmtime(t)))):
            per.setdefault(r["d"], {})["restarts"] = r["n"]
        top_inc = self.rows("SELECT ns, obj, count(*) n, sum(state='open') open FROM incidents WHERE opened>=? AND opened<=? AND ns!='-' "
                            "GROUP BY ns, obj ORDER BY n DESC LIMIT 10", (since, t))
        top_rst = self.rows("SELECT ns, wl, sum(last - first) n FROM restarts WHERE day>=? AND day<=? GROUP BY ns, wl HAVING n>0 ORDER BY n DESC LIMIT 10",
                            (time.strftime("%Y-%m-%d", time.gmtime(since)), time.strftime("%Y-%m-%d", time.gmtime(t))))
        return {"days": [dict(v, day=k) for k, v in sorted(per.items(), reverse=True)], "top_incidents": top_inc, "top_restarts": top_rst}

    def infra(self):
        res = self.probe.get("results", {})
        return {"ts": self.probe.get("ts"), "results": [{"name": n, "ok": r[0], "detail": r[1], "owner": r[2], "meta": r[3] if len(r) > 3 else {},
                                                         "since": self.states.get("probe:" + n, (None, None))[1], "checked": self.probe.get("ts")}
                                                        for n, r in sorted(res.items())]}
