"""Read-only access check, round 2: which namespaces the station kubeconfig may read, and an inventory of
aip-mlops-service (deployments, KubeAI models, ingresses, Flux objects, Elastic). GET requests only.
Prints names, images and HTTP codes, never secrets or env. Run with access-check.sh (it starts the VPNs first)."""
import json
import sys
import time

sys.path.insert(0, "/app")
import probe  # noqa: E402

probe.TIMEOUT = 25  # the VPN is slow; round 1 timed out at 8 s on big lists
server, ctx = probe.kube()
NS = "aip-mlops-service"


def k(path, tries=3):
    """(code, parsed json or None); retries timeouts."""
    for i in range(tries):
        try:
            code, body = probe.get(server + path, ctx=ctx)
            return code, (json.loads(body) if code == 200 else None)
        except Exception as e:
            last = probe.err(e)
            time.sleep(2)
    return last, None


code, nss = k("/api/v1/namespaces")
names = sorted(i["metadata"]["name"] for i in nss["items"]) if nss else []
print("=== namespaces: %s, %d" % (code, len(names)))
print(" ".join(names))

print("\n=== per-namespace read access (pods | deployments | events | pods/log needs pods)")
readable = []
for ns in names:
    p = k("/api/v1/namespaces/%s/pods?limit=1" % ns, tries=2)[0]
    if p == 200:
        d = k("/apis/apps/v1/namespaces/%s/deployments?limit=1" % ns, tries=2)[0]
        e = k("/api/v1/namespaces/%s/events?limit=1" % ns, tries=2)[0]
        readable.append(ns)
        print("%-30s pods %s | deploy %s | events %s" % (ns, p, d, e))
    elif p != 403:
        print("%-30s pods %s" % (ns, p))
print("readable: %d of %d namespaces. Every other namespace returned 403." % (len(readable), len(names)))

print("\n=== %s deployments (name, ready/desired, image)" % NS)
code, deps = k("/apis/apps/v1/namespaces/%s/deployments" % NS)
for d in (deps or {}).get("items", []):
    st = d.get("status", {})
    imgs = ",".join(c["image"].rsplit("/", 1)[-1] for c in d["spec"]["template"]["spec"]["containers"])
    print("%-40s %s/%s  %s" % (d["metadata"]["name"], st.get("readyReplicas", 0), d["spec"].get("replicas", 0), imgs))

print("\n=== %s statefulsets" % NS)
code, sts = k("/apis/apps/v1/namespaces/%s/statefulsets" % NS)
print(code, " ".join(s["metadata"]["name"] for s in (sts or {}).get("items", [])))

print("\n=== %s KubeAI models (name, engine, replicas, resourceProfile, url)" % NS)
code, models = k("/apis/kubeai.org/v1/namespaces/%s/models" % NS)
print("HTTP", code)
for m in (models or {}).get("items", []):
    s, st = m.get("spec", {}), m.get("status", {})
    print("%-40s %-6s replicas %s/%s min %s max %s  %s  %s" % (
        m["metadata"]["name"], s.get("engine"), st.get("replicas", {}).get("ready", 0), st.get("replicas", {}).get("all", 0),
        s.get("minReplicas"), s.get("maxReplicas"), s.get("resourceProfile"), s.get("url")))

print("\n=== %s ingresses (name: hosts)" % NS)
code, ings = k("/apis/networking.k8s.io/v1/namespaces/%s/ingresses" % NS)
for i in (ings or {}).get("items", []):
    print("%-40s %s" % (i["metadata"]["name"], " ".join(r.get("host", "") for r in i["spec"].get("rules", []))))

print("\n=== Flux objects readable in %s and flux-system" % NS)
for ns in (NS, "flux-system"):
    for kind, path in (("gitrepositories", "source.toolkit.fluxcd.io/v1"), ("kustomizations", "kustomize.toolkit.fluxcd.io/v1"),
                       ("helmreleases", "helm.toolkit.fluxcd.io/v2")):
        code, objs = k("/apis/%s/namespaces/%s/%s" % (path, ns, kind), tries=2)
        print("%-18s %-16s %s %s" % (ns, kind, code, " ".join(o["metadata"]["name"] for o in (objs or {}).get("items", []))))

print("\n=== Elastic (logs) in %s" % NS)
for kind, path in (("elasticsearches", "elasticsearch.k8s.elastic.co/v1"), ("kibanas", "kibana.k8s.elastic.co/v1")):
    code, objs = k("/apis/%s/namespaces/%s/%s" % (path, NS, kind), tries=2)
    print("%-16s %s %s" % (kind, code, " ".join(o["metadata"]["name"] for o in (objs or {}).get("items", []))))

print("\n=== pod restarts in %s (pods with restarts > 0)" % NS)
code, pods = k("/api/v1/namespaces/%s/pods" % NS)
for p in (pods or {}).get("items", []):
    r = sum(c.get("restartCount", 0) for c in p.get("status", {}).get("containerStatuses", []))
    if r:
        print("%-55s restarts %d  phase %s" % (p["metadata"]["name"], r, p["status"].get("phase")))
