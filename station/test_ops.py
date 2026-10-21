"""Self-check for the ops collector and its web API, against a fake Kubernetes API. Run: python3 station/test_ops.py
(station/probe.py must be the build copy of k8s/probe.py.)"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

here = Path(__file__).resolve().parent
tmp = Path(tempfile.mkdtemp())
(tmp / "probe.json").write_text(json.dumps({"receiver_url": "http://127.0.0.1:9/report"}))
os.environ.update(PROBE_CONFIG=str(tmp / "probe.json"), OPS_DATA=str(tmp), PROBE_RESULTS=str(tmp / "probe-results.json"))
for k in ("PROBE_KEY", "LITELLM_URL", "GITLAB_URL", "GRAFANA_URL", "OPS_ALLOW_CIDRS"):
    os.environ.pop(k, None)
sys.path.insert(0, str(here))
import ops  # noqa: E402
import ops_web  # noqa: E402

NS, T0 = "aip-mlops-service", time.time()
iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def deploy(name, image, ready=1, replicas=1):
    c = {"name": "app", "image": "harborpilot/x/" + image, "readinessProbe": {}, "resources": {"limits": {"memory": "1Gi"}}}
    return {"metadata": {"name": name, "creationTimestamp": iso(T0 - 86400)},
            "spec": {"replicas": replicas, "template": {"spec": {"containers": [c]}}}, "status": {"readyReplicas": ready}}


def pod(name, wl, waiting=None, restarts=0, phase="Running", ready=True, last=None, labels=None, owner=None):
    cs = {"name": "app", "restartCount": restarts, "ready": ready, "state": {"waiting": {"reason": waiting}} if waiting else {"running": {}}}
    if last:
        cs["lastState"] = {"terminated": {"reason": last, "finishedAt": iso(time.time() - 60)}}
    return {"metadata": {"name": name, "creationTimestamp": iso(T0 - 3600), "labels": dict({"pod-template-hash": "abc"}, **(labels or {})),
                         "ownerReferences": [{"name": owner or wl + "-abc"}]},
            "spec": {"containers": [{"name": "app", "resources": {"limits": {"memory": "1Gi"}}}], "nodeName": "gpu-worker01"},
            "status": {"phase": phase, "startTime": iso(T0 - 3600), "containerStatuses": [cs],
                       "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def model(name, ready=1, created=None):
    return {"metadata": {"name": name, "creationTimestamp": iso(created or T0 - 86400)},
            "spec": {"engine": "VLLM", "url": "pvc://" + name, "resourceProfile": "nvidia-gpu-h100:1", "minReplicas": 1, "maxReplicas": 1},
            "status": {"replicas": {"ready": ready, "all": 1}}}


def event(obj, reason, msg):
    return {"involvedObject": {"kind": "Pod", "name": obj}, "reason": reason, "message": msg, "lastTimestamp": iso(time.time() - 30),
            "metadata": {"creationTimestamp": iso(time.time() - 30)}}


empty = lambda: {"pods": [], "deploy": [], "sts": [], "events": [], "ing": [], "models": []}
K = {NS: empty(), "llm-team": empty(), "user-x": empty(), "user-y": empty()}
LOGS = {}
K[NS].update(deploy=[deploy("training-api", "training-api:1.0")], pods=[pod("training-api-abc-1", "training-api")],
             models=[model("m1")], ing=[{"metadata": {"name": "training"}, "spec": {"rules": [{"host": "mlopsapipilot.aipsharif.ir"}]}}])
K["llm-team"].update(deploy=[deploy("vllm", "vllm:2")], pods=[pod("vllm-abc-1", "vllm")])
K["user-x"].update(deploy=[deploy("notebook", "nb:1")], pods=[pod("notebook-abc-1", "notebook")])
K["user-y"].update(deploy=[deploy("y-agent-clearml-agent", "agent:1")], pods=[pod("y-agent-clearml-agent-abc-1", "y-agent-clearml-agent")])
RES = {"pods": "pods", "deployments": "deploy", "statefulsets": "sts", "events": "events", "ingresses": "ing", "models": "models"}
calls = []


def fake_kget(path, raw=False):
    calls.append(path)
    p = path.split("?")[0]
    if p == "/api/v1/namespaces":
        return {"items": [{"metadata": {"name": n}} for n in list(K) + ["kube-system"]]}
    parts = p.strip("/").split("/")
    ns = parts[parts.index("namespaces") + 1]
    if ns == "kube-system":
        raise ops.KubeError("HTTP 403", 403)
    if p.endswith("/log"):
        if "previous=true" in path and parts[-2] not in LOGS:
            raise ops.KubeError("HTTP 400", 400)
        return LOGS.get(parts[-2], "")
    return {"items": K[ns][RES[parts[parts.index("namespaces") + 2]]]}


ops.kget = fake_kget
ops.SLOW_EVERY = 1   # read every namespace every round
col = ops.Collector()
q = lambda sql, a=(): col.rows(sql, a)
inc = lambda **w: [r for r in q("SELECT * FROM incidents") if all(r[k] == v for k, v in w.items())]

# 1. first round = baseline: no changes, no incidents, forbidden namespace skipped
col.run_round()
assert q("SELECT * FROM changes") == [] and q("SELECT * FROM incidents") == [], (q("SELECT * FROM changes"), col.status)
assert "kube-system" not in col.inv and set(col.inv) == set(K), col.inv.keys()

# 2. a release that crash-loops, another team's crash, a user's crash, a user's ClearML agent crash, no GPU for a model
K[NS]["deploy"] = [deploy("training-api", "training-api:1.1")]
K[NS]["pods"] = [pod("training-api-abc-2", "training-api", waiting="CrashLoopBackOff", restarts=5, ready=False),
                 pod("model-m2-x", "m2", phase="Pending", ready=False, labels={"model": "m2"}, owner="m2")]
K[NS]["models"] = [model("m1"), model("m2", ready=0), model("m3", ready=0, created=time.time())]
K[NS]["events"] = [event("model-m2-x", "FailedScheduling", "0/5 nodes are available: 2 Insufficient nvidia.com/gpu.")]
LOGS["training-api-abc-2"] = "Traceback (most recent call last):\nValueError: bad config"
K["llm-team"]["pods"] = [pod("vllm-abc-1", "vllm", waiting="CrashLoopBackOff", restarts=3, ready=False)]
K["user-x"]["pods"] = [pod("notebook-abc-1", "notebook", waiting="CrashLoopBackOff", restarts=3, ready=False)]
K["user-y"]["pods"] = [pod("y-agent-clearml-agent-abc-1", "y-agent-clearml-agent", waiting="CrashLoopBackOff", restarts=3, ready=False)]
col.run_round()
ch = {c["name"]: c for c in q("SELECT * FROM changes")}
assert ch["training-api"]["type"] == "release" and ch["training-api"]["before"] == "training-api:1.0", ch
assert ch["m2"]["type"] == "new-model", ch
assert ch["m3"]["type"] == "new-model" and ch["m3"]["verdict"] == "watching", ch
[tr] = inc(obj="training-api", symptom="crashloop")
assert tr["owner"] == "us" and "bad release training-api:1.0 → training-api:1.1" in tr["cause"], tr
assert "roll back training-api to training-api:1.0" in tr["fix"] and tr["alerted"] == 1, tr
ev = json.loads(tr["evidence"])
assert "ValueError: bad config" in ev["logs"] and "previous container" in ev["log_from"], ev
[v] = inc(obj="vllm")
assert v["owner"] == "llm-team" and v["alerted"] == 1 and "bad release" not in v["cause"], v
[nb] = inc(obj="notebook")
assert nb["owner"] == "user-x" and nb["alerted"] == 0, nb                  # users' own workloads: dashboard only
[ag] = inc(obj="y-agent-clearml-agent")
assert ag["owner"] == "us" and ag["alerted"] == 1, ag                       # per-user ClearML agents are ours
[m2] = inc(obj="m2", symptom="pending")
assert m2["owner"] == "infra" and "no free nvidia.com/gpu" in m2["cause"], m2
assert not inc(obj="m2", symptom="model-down") and not inc(obj="m3"), q("SELECT obj, symptom FROM incidents")  # pod explains it; m3 in grace
alerts = "\n".join(col.alerts)
assert "OPEN #%d crashloop" % tr["id"] in alerts and "RELEASE aip-mlops-service/training-api" in alerts, alerts
assert "notebook" not in alerts and "NEW MODEL aip-mlops-service/m3" in alerts, alerts
assert len(inc(obj="training-api")) == 1                                    # still one incident, not one per round

# 3. a deployment short of replicas with no pod-level reason = "down"
ops.DOWN_GRACE = 0
K["llm-team"]["deploy"].append(deploy("router", "router:1", ready=0))
col.run_round()
col.run_round()
[d] = inc(obj="router")
assert d["symptom"] == "down" and "0/1 replicas ready" in d["detail"], d
# a pod pending for days: its events expired, the scheduler message is still in the pod's PodScheduled condition
old = pod("big-abc-1", "big", phase="Pending", ready=False)
old["status"]["conditions"].append({"type": "PodScheduled", "status": "False", "message": "0/5 nodes are available: 5 Insufficient nvidia.com/gpu."})
K["llm-team"]["pods"].append(old)
col.run_round()
[big] = inc(obj="big")
assert big["owner"] == "infra" and "no free nvidia.com/gpu" in big["cause"], big
K["llm-team"]["pods"].pop()

# 4. everything fixed: incidents close after 2 clean rounds, closing alerts for the alerted ones
col.alerts.clear()
K[NS]["pods"] = [pod("training-api-abc-3", "training-api")]
K[NS]["events"], K[NS]["models"] = [], [model("m1"), model("m2"), model("m3")]
for ns, wl in (("llm-team", "vllm"), ("user-x", "notebook"), ("user-y", "y-agent-clearml-agent")):
    K[ns]["pods"] = [pod(wl + "-abc-9", wl)]
K["llm-team"]["deploy"] = [deploy("vllm", "vllm:2")]
col.run_round()
assert inc(obj="training-api", state="open"), q("SELECT obj, state, clean FROM incidents")  # 1 clean round: still open
col.run_round()
assert inc(state="open") == [], q("SELECT obj, state, clean FROM incidents")
assert "CLOSED #%d crashloop" % tr["id"] in "\n".join(col.alerts) and "notebook" not in "\n".join(col.alerts), col.alerts
assert any(c["type"] == "removed" and c["name"] == "router" for c in q("SELECT * FROM changes"))

# 5. release verdict after 15 min: bad (an incident opened on it); new model: ready
col.exec("UPDATE changes SET ts = ts - 1200")
col.judge_changes(time.time())
ch = {c["name"]: c for c in q("SELECT * FROM changes")}
assert ch["training-api"]["verdict"] == "bad" and ch["m3"]["verdict"] == "ready", ch

# 6. probe results: infra check = infra, our asset = us (with pods as evidence), no Telegram (London alerts those)
col.alerts.clear()
Path(os.environ["PROBE_RESULTS"]).write_text(json.dumps({"ts": time.time(), "results": {
    "k8s GPU capacity": [False, "2 events", "infra"], "probe VPN cluster": [True, "tun0 connected", "monitor"],
    "training-api": [False, "no ready pod", "asset", {"person": "Dariush", "value": "Critical"}]}}))
col.run_round()
[gi] = inc(symptom="infra", state="open")
[ta] = inc(symptom="endpoint", state="open")
assert gi["owner"] == "infra" and ta["owner"] == "us" and ta["ns"] == NS and "Dariush" in ta["fix"], (gi, ta)
assert json.loads(ta["evidence"])["pods"] and not col.alerts, col.alerts
col.run_round()                                                             # same probe report again: no change
assert len(inc(state="open")) == 2

# 7. log signatures: first hour learns, then a new error opens one incident; numbers do not make a new signature
assert ops.signature("ERROR db timeout after 30 s id=4f3a2b1c9d8e7f60") == ops.signature("ERROR db timeout after 31 s id=0a1b2c3d4e5f6071")
LOGS["training-api-abc-3"] = "INFO ok\nERROR old problem 1"
col.run_round()
assert not inc(symptom="new-error") and q("SELECT * FROM signatures")       # learning period: known, no incident
col.set_meta("logs_since", str(time.time() - 7200))
LOGS["training-api-abc-3"] = "ERROR old problem 2\nERROR redis connection refused to 10.0.0.5:6379"
col.run_round()
[ne] = inc(symptom="new-error")
assert "redis connection refused" in ne["detail"] and ne["obj"] == "training-api", ne
col.run_round()
assert len(inc(symptom="new-error")) == 1

st = col.state()
tiles = {t["n"]: t for t in st["tiles"]}
assert tiles[1]["value"] == 1 and tiles[2]["value"] == 2 and tiles[6]["value"] == "3/3" and tiles[8]["value"] == "1 · 1", tiles
assert col.incident(tr["id"])["handoff"].startswith("[Sakoo] crashloop in aip-mlops-service/training-api"), col.incident(tr["id"])

# --- web: login, sessions, rate limit, input checks, security headers
ops_web.COL = col
ops_web.USERS.write_text(json.dumps({"ali": ops_web.hash_pw("correct horse 1")}))
srv = ThreadingHTTPServer(("127.0.0.1", 0), ops_web.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % srv.server_address[1]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(path, body=None, cookie=""):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Cookie": cookie})
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


assert call("/api/state")[0] == 401
assert call("/api/login", {"user": "ali", "password": "wrong"})[0] == 403
assert call("/api/login", {"user": "nobody", "password": "x"})[0] == 403
code, _, hd = call("/api/login", {"user": "ali", "password": "correct horse 1"})
cookie = hd["Set-Cookie"].split(";")[0]
assert code == 200 and "HttpOnly" in hd["Set-Cookie"] and "SameSite=Strict" in hd["Set-Cookie"], hd
code, body, hd = call("/api/state", cookie=cookie)
assert code == 200 and len(json.loads(body)["tiles"]) == 8 and "default-src 'self'" in hd["Content-Security-Policy"]
assert call("/api/logs?ns=%s&pod=../etc&container=app" % NS, cookie=cookie)[0] == 400
assert call("/api/logs?ns=kube-system&pod=x&container=app", cookie=cookie)[0] == 400
code, body, _ = call("/api/logs?ns=%s&pod=training-api-abc-3&container=app&tail=5" % NS, cookie=cookie)
assert code == 200 and "redis" in json.loads(body)["text"], body
assert call("/api/incident?id=%d" % tr["id"], cookie=cookie)[0] == 200
assert call("/api/summarize", {"incident": tr["id"]}, cookie=cookie)[0] == 400        # LLM not configured
assert call("/", cookie="")[0] == 200 and call("/ops.js")[0] == 200
for _ in range(5):
    call("/api/login", {"user": "ali", "password": "wrong"})
assert call("/api/login", {"user": "ali", "password": "correct horse 1"})[0] == 429       # blocked even with the right password
assert call("/api/logout", {}, cookie=cookie)[0] == 200 and call("/api/state", cookie=cookie)[0] == 401
srv.shutdown()
assert not any("/api/v1/namespaces/" in c and c.split("?")[0].rsplit("/", 1)[-1] not in RES and not c.split("?")[0].endswith("/log")
               for c in calls), "unexpected API path"
print("ok")
