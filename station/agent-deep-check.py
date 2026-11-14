"""GET-only, round 5 of the 'already owned' dataset bug: what k9s would show beyond pod logs.
1. Every pod in aip-mlops-service (status, restarts) - finds the ClearML apiserver (mlops-core) pods.
2. ClearML apiserver logs: counts of agent/queue API calls and any lines about aviraip or the stuck task.
3. agent-service / agent-resizer logs (they create per-user agents and, likely, their queues).
4. Events in user namespaces with a broken agent (Pending agents, aviraip).
5. ConfigMaps in user-aviraip (agent config / pod template), secret-looking lines dropped.
No exec, no writes; S3 keys and tokens are redacted. Run with access-check.sh as $1."""
import collections
import json
import re
import sys
import time
import urllib.parse

sys.path.insert(0, "/app")
import probe  # noqa: E402

probe.TIMEOUT = 25
server, ctx = probe.kube()
NS = "aip-mlops-service"
SECRET = re.compile(r"\b[A-Z0-9]{20}\b|\b[A-Za-z0-9/+_.-]{40,}\b")
HIDE = re.compile(r"secret|key|token|pass|credential", re.I)
ABOUT = re.compile(r"aviraip|1e50ff9d|final_book", re.I)
ENDPOINTS = ["queues.get_next_task", "queues.get_all", "queues.create", "queues.add_task", "tasks.enqueue",
             "workers.register", "workers.status_report", "workers.unregister", "workers.get_all"]


def k(path, raw=False):
    for _ in range(3):
        try:
            code, body = probe.get(server + path, ctx=ctx)
            return code, (body.decode("utf-8", "replace") if raw else json.loads(body)) if code == 200 else None
        except Exception as e:
            last = probe.err(e)
            time.sleep(2)
    return last, None


def clean(line):
    return SECRET.sub("<redacted>", line[:400])


def logs(ns, pod, container, tail=20000):
    code, text = k("/api/v1/namespaces/%s/pods/%s/log?container=%s&tailLines=%d&timestamps=true"
                   % (ns, pod, urllib.parse.quote(container), tail), raw=True)
    return code, (text or "").splitlines()


def show(lines, pat, last=60, ctx_lines=2):
    hits = [i for i, l in enumerate(lines) if pat.search(l)]
    shown = set()
    for i in hits[-last:]:
        for j in range(max(0, i - ctx_lines), min(len(lines), i + ctx_lines + 1)):
            if j not in shown and not HIDE.search(lines[j]):
                shown.add(j)
                print("    " + clean(lines[j]))
    return len(hits)


print("=== 1. %s pods (name, phase, restarts, start)" % NS)
code, pods = k("/api/v1/namespaces/%s/pods" % NS)
pods = (pods or {}).get("items", [])
for p in pods:
    rs = sum(c.get("restartCount", 0) for c in p["status"].get("containerStatuses", []))
    print("  %-55s %-9s restarts %-3s %s" % (p["metadata"]["name"], p["status"].get("phase"), rs, p["status"].get("startTime")))

for p in pods:
    name = p["metadata"]["name"]
    if not re.search(r"mlops-core|apiserver|agent-service|agent-resizer", name) or "fileserver" in name:
        continue
    for c in p["spec"]["containers"]:
        code, lines = logs(NS, name, c["name"])
        print("\n=== %s/%s: HTTP %s, %d lines (%s .. %s)" % (name, c["name"], code, len(lines),
              lines[0][:30] if lines else "-", lines[-1][:30] if lines else "-"))
        counts = collections.Counter(e for l in lines for e in ENDPOINTS if e in l)
        if counts:
            print("  API call counts:", dict(counts))
            for e in ("tasks.enqueue", "queues.get_next_task", "workers.register", "queues.create"):
                last = [l for l in lines if e in l][-3:]
                for l in last:
                    print("    last %s: %s" % (e, clean(l)))
        print("  lines about aviraip / the task:")
        n = show(lines, ABOUT)
        print("  (%d matches)" % n)
        print("  errors, last 40:")
        n = show(lines, re.compile(r"Traceback|ERROR|Exception|not found|invalid|failed", re.I), last=40, ctx_lines=1)
        print("  (%d matches)" % n)
        if re.search(r"agent-service|agent-resizer", name):
            print("  queue / agent creation lines, last 40:")
            show(lines, re.compile(r"queue|creat|deploy|helm|agent", re.I), last=40, ctx_lines=0)

print("\n=== 4. events in user namespaces")
for ns in ("user-aviraip", "user-amraei", "user-arman", "user-nimazolfagharian", "user-zarei", "user-nilvatest1"):
    code, ev = k("/api/v1/namespaces/%s/events" % ns)
    items = sorted((ev or {}).get("items", []), key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")
    print("  %s: HTTP %s, %d events" % (ns, code, len(items)))
    for e in items[-8:]:
        print("    %s %s %s/%s: %s" % (e.get("lastTimestamp") or e.get("eventTime"), e.get("reason"),
              e["involvedObject"].get("kind"), e["involvedObject"].get("name"), clean(e.get("message", ""))))
    for p in (k("/api/v1/namespaces/%s/pods" % ns)[1] or {}).get("items", []):
        if p["status"].get("phase") != "Running":
            for cond in p["status"].get("conditions", []):
                if cond.get("status") != "True":
                    print("    pod %s condition %s: %s %s" % (p["metadata"]["name"], cond["type"], cond.get("reason"), cond.get("message")))

print("\n=== 5. user-aviraip configmaps")
code, cms = k("/api/v1/namespaces/user-aviraip/configmaps")
print("  HTTP", code)
for cm in (cms or {}).get("items", []):
    name = cm["metadata"]["name"]
    print("  --- %s keys %s" % (name, list((cm.get("data") or {}).keys())))
    if name == "kube-root-ca.crt":
        continue
    for key, val in (cm.get("data") or {}).items():
        print("    [%s]" % key)
        for l in val.splitlines()[:80]:
            if not HIDE.search(l):
                print("      " + clean(l))
