"""GET-only: why a dataset registers as 'already owned by the user' but is missing from the registered list.
data-ingestion-api creates the ownership record in mlasset-registry-api and only enqueues a ClearML task; the real
ingest runs on the user's ClearML agent. This prints log lines about the dataset from both sides, with S3 keys
redacted. Run: see access-check.sh, passing /app/dataset-check.py as $1."""
import json
import re
import sys
import time
import urllib.parse

sys.path.insert(0, "/app")
import probe  # noqa: E402

probe.TIMEOUT = 25
server, ctx = probe.kube()
DATASET, TASK = "final_book_dataset_parquet", "1e50ff9d"
PAT = re.compile(r"%s|%s" % (re.escape(DATASET), TASK), re.I)
SECRET = re.compile(r"\b[A-Z0-9]{20}\b|\b[A-Za-z0-9/+_-]{40,}\b")  # data-ingestion-api prints S3 key pairs


def k(path, raw=False):
    for _ in range(3):
        try:
            code, body = probe.get(server + path, ctx=ctx)
            return code, (body.decode("utf-8", "replace") if raw else json.loads(body)) if code == 200 else None
        except Exception as e:
            last = probe.err(e)
            time.sleep(2)
    return last, None


# Round 3: the user's k8s-glue never logged the task, so find which agent (if any) serves the queue.
code, agent = k("/api/v1/namespaces/user-aviraip/pods/aviraip-agent-clearml-agent-7995bb8b8-ftc4t/log?container=k8s-glue&tailLines=200", raw=True)
print("=== user-aviraip k8s-glue full log, HTTP %s" % code)
print("\n".join(SECRET.sub("<redacted>", l[:400]) for l in (agent or "").splitlines() if not re.search(r"secret|key|token|pass", l, re.I)))

code, nss = k("/api/v1/namespaces")
TARGETS = []
for ns in sorted(i["metadata"]["name"] for i in (nss or {}).get("items", [])):
    code, pods = k("/api/v1/namespaces/%s/pods" % ns)
    if code == 200 and any(re.search(r"clearml|agent|ingest|processor", p["metadata"]["name"]) for p in pods["items"]):
        TARGETS.append((ns, r"clearml|agent|ingest|processor"))
print("\n=== namespaces with ClearML/agent pods:", [t[0] for t in TARGETS])

for ns, want in TARGETS:
    code, pods = k("/api/v1/namespaces/%s/pods" % ns)
    pods = [p for p in (pods or {}).get("items", []) if re.search(want, p["metadata"]["name"])]
    print("\n=== %s pods: HTTP %s, %s" % (ns, code, [(p["metadata"]["name"], p["status"].get("phase"), p["status"].get("startTime")) for p in pods]))
    for p in pods:
        name = p["metadata"]["name"]
        for c in p["spec"]["containers"]:
            for prev in ("false", "true"):
                code, text = k("/api/v1/namespaces/%s/pods/%s/log?container=%s&tailLines=20000&previous=%s"
                               % (ns, name, urllib.parse.quote(c["name"]), prev), raw=True)
                lines = (text or "").splitlines()
                hits = [i for i, l in enumerate(lines) if PAT.search(l)]
                if not hits:
                    continue
                print("--- %s/%s previous=%s: %d lines, %d matches (last 80 with context)" % (name, c["name"], prev, len(lines), len(hits)))
                shown = set()
                for i in hits[-80:]:
                    for j in range(max(0, i - 3), min(len(lines), i + 4)):
                        if j not in shown:
                            shown.add(j)
                            print(SECRET.sub("<redacted>", lines[j][:400]))
