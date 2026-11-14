"""Read-only access check, round 3 (2026-10-31): what the station kubeconfig can read now, compared with round 2
(59 of 85 namespaces, infra namespaces 403). GET requests only; never lists secrets. Prints names and HTTP codes.
Also looks for the AIP Langfuse and Prometheus that a dashboard could use.
Run: sh access-check.sh /app/access-check2.py (mount this file too)."""
import json
import re
import sys
import time

sys.path.insert(0, "/app")
import probe  # noqa: E402

probe.TIMEOUT = 25
server, ctx = probe.kube()
ROUND2 = 59
INFRA = ["monitoring", "kube-system", "gpu-operator", "llm-serving", "studio", "flux-system", "traefik", "rook-ceph",
         "fluent", "elastic-system", "cert-manager"]
SECRETISH = re.compile(r"(key|token|secret|password|passwd)\s*[:=]\s*\S+", re.I)


def k(path, tries=2):
    for i in range(tries):
        try:
            code, body = probe.get(server + path, ctx=ctx)
            return code, (json.loads(body) if code == 200 and body[:1] in (b"{", "{") else body if code == 200 else None)
        except Exception as e:
            last = probe.err(e)
            time.sleep(2)
    return last, None


def names(obj):
    return " ".join(i["metadata"]["name"] for i in (obj or {}).get("items", [])) if isinstance(obj, dict) else ""


print("=== cluster scope")
for label, path in (("nodes", "/api/v1/nodes?limit=1"), ("pods all-ns", "/api/v1/pods?limit=1"),
                    ("ingresses all-ns", "/apis/networking.k8s.io/v1/ingresses?limit=1"),
                    ("persistentvolumes", "/api/v1/persistentvolumes?limit=1"),
                    ("storageclasses", "/apis/storage.k8s.io/v1/storageclasses"),
                    ("crds", "/apis/apiextensions.k8s.io/v1/customresourcedefinitions?limit=1"),
                    ("clusterroles", "/apis/rbac.authorization.k8s.io/v1/clusterroles?limit=1"),
                    ("metrics nodes", "/apis/metrics.k8s.io/v1beta1/nodes"),
                    ("kubeai models all-ns", "/apis/kubeai.org/v1/models")):
    code, obj = k(path)
    print("%-22s %s" % (label, code))
code, nodes = k("/api/v1/nodes")
for n in (nodes or {}).get("items", []) if isinstance(nodes, dict) else []:
    cap = n["status"].get("capacity", {})
    print("  node %-30s cpu %s mem %s gpu %s" % (n["metadata"]["name"], cap.get("cpu"), cap.get("memory"),
                                                  cap.get("nvidia.com/gpu", "-")))

code, nss = k("/api/v1/namespaces")
all_ns = sorted(i["metadata"]["name"] for i in nss["items"]) if isinstance(nss, dict) else []
print("\n=== namespaces: %s, %d" % (code, len(all_ns)))

print("\n=== per-namespace (pods | deploy | svc | configmaps | events)")
readable = []
for ns in all_ns:
    p = k("/api/v1/namespaces/%s/pods?limit=1" % ns)[0]
    if p != 200:
        if p != 403:
            print("%-30s pods %s" % (ns, p))
        continue
    readable.append(ns)
    codes = [k(path % ns)[0] for path in ("/apis/apps/v1/namespaces/%s/deployments?limit=1",
                                          "/api/v1/namespaces/%s/services?limit=1",
                                          "/api/v1/namespaces/%s/configmaps?limit=1",
                                          "/api/v1/namespaces/%s/events?limit=1")]
    print("%-30s %s | %s | %s | %s | %s" % ((ns, p) + tuple(codes)))
print("readable: %d of %d (round 2: %d)" % (len(readable), len(all_ns), ROUND2))
print("infra namespaces now readable:", " ".join(n for n in INFRA if n in readable) or "none")

for ns in ("monitoring", "langfuse", "litellm"):
    if ns not in readable:
        print("\n=== %s: not readable" % ns)
        continue
    print("\n=== %s" % ns)
    print("deployments:", names(k("/apis/apps/v1/namespaces/%s/deployments" % ns)[1]))
    print("statefulsets:", names(k("/apis/apps/v1/namespaces/%s/statefulsets" % ns)[1]))
    code, svcs = k("/api/v1/namespaces/%s/services" % ns)
    for s in (svcs or {}).get("items", []) if isinstance(svcs, dict) else []:
        print("  svc %-40s %s" % (s["metadata"]["name"], ",".join(str(p["port"]) for p in s["spec"].get("ports", []))))
    code, ings = k("/apis/networking.k8s.io/v1/namespaces/%s/ingresses" % ns)
    for i in (ings or {}).get("items", []) if isinstance(ings, dict) else []:
        print("  ingress %-36s %s" % (i["metadata"]["name"], " ".join(r.get("host", "") for r in i["spec"].get("rules", []))))
    code, cms = k("/api/v1/namespaces/%s/configmaps" % ns)
    print("configmaps:", names(cms if isinstance(cms, dict) else None))
    # LiteLLM callbacks show where usage logs go (langfuse, prometheus); print only those lines, masked.
    for cm in (cms or {}).get("items", []) if isinstance(cms, dict) else []:
        for key, val in (cm.get("data") or {}).items():
            for line in str(val).splitlines():
                if re.search(r"callback|langfuse|prometheus|otel", line, re.I):
                    print("  %s/%s: %s" % (cm["metadata"]["name"], key, SECRETISH.sub(r"\1: ***", line.strip())[:160]))

print("\n=== service proxy probes (status only)")
for label, path in (("litellm /metrics", "/api/v1/namespaces/litellm/services/litellm:4000/proxy/metrics"),
                    ("litellm /health/liveliness", "/api/v1/namespaces/litellm/services/litellm:4000/proxy/health/liveliness"),
                    ("langfuse /api/public/health", "/api/v1/namespaces/langfuse/services/langfuse-web:3000/proxy/api/public/health")):
    code, body = k(path)
    print("%-30s %s" % (label, code))
