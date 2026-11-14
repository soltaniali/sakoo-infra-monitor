"""GET-only: which queues each per-user ClearML k8s-glue agent listens to, and whether it ever pulled a task.
aviraip's agent logged 'Listening to queues:' with an empty list, so its enqueued dataset task never ran.
Prints the deployment args and QUEUE env values (never other env values). Run with access-check.sh as $1."""
import json
import re
import sys
import time

sys.path.insert(0, "/app")
import probe  # noqa: E402

probe.TIMEOUT = 25
server, ctx = probe.kube()
SECRET = re.compile(r"\b[A-Z0-9]{20}\b|\b[A-Za-z0-9/+_-]{40,}\b")
TASK = re.compile(r"pulling|launch|task id|new task|pod.*created|clearml-id-|error|exception", re.I)


def k(path, raw=False):
    for _ in range(3):
        try:
            code, body = probe.get(server + path, ctx=ctx)
            return code, (body.decode("utf-8", "replace") if raw else json.loads(body)) if code == 200 else None
        except Exception as e:
            last = probe.err(e)
            time.sleep(2)
    return last, None


code, nss = k("/api/v1/namespaces")
for ns in sorted(i["metadata"]["name"] for i in (nss or {}).get("items", []) if i["metadata"]["name"].startswith("user-")):
    code, deps = k("/apis/apps/v1/namespaces/%s/deployments" % ns)
    for d in (deps or {}).get("items", []):
        if "clearml-agent" not in d["metadata"]["name"]:
            continue
        print("\n=== %s/%s" % (ns, d["metadata"]["name"]))
        for c in d["spec"]["template"]["spec"]["containers"]:
            print("  %s cmd %s args %s" % (c["name"], c.get("command"), SECRET.sub("<redacted>", str(c.get("args")))))
            print("  queue env:", {e["name"]: e.get("value", "<from secret/configmap>") for e in c.get("env", []) if "QUEUE" in e["name"].upper()})
    code, pods = k("/api/v1/namespaces/%s/pods" % ns)
    for p in (pods or {}).get("items", []):
        if "clearml-agent" not in p["metadata"]["name"]:
            continue
        for c in p["spec"]["containers"]:
            code, text = k("/api/v1/namespaces/%s/pods/%s/log?container=%s&tailLines=5000" % (ns, p["metadata"]["name"], c["name"]), raw=True)
            lines = (text or "").splitlines()
            q = next((i for i, l in enumerate(lines) if "Listening to queues" in l), None)
            queues = [l.strip() for l in lines[q + 1:q + 6] if l.strip()] if q is not None else ["<no 'Listening' line>"]
            tasks = [SECRET.sub("<redacted>", l[:300]) for l in lines if TASK.search(l)]
            print("  pod %s/%s: %d log lines, queues %s, task/error lines %d" % (p["metadata"]["name"], c["name"], len(lines), queues, len(tasks)))
            for l in tasks[-5:]:
                print("    " + l)
