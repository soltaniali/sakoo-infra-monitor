"""Read-only look at the AIP Langfuse project (GET /api/public/...): what LiteLLM writes per request, so we know
which fields can tie a request to a user/key. Prints field names and a few masked values; never inputs/outputs.
Run: docker exec sakoo-probe-station python3 /tmp/langfuse-check.py"""
import base64
import json
import os
import ssl
import urllib.parse
import urllib.request

U = os.environ["LANGFUSE_URL"].rstrip("/")
AUTH = {"Authorization": "Basic " + base64.b64encode((os.environ["LANGFUSE_PK"] + ":" + os.environ["LANGFUSE_SK"]).encode()).decode()}
CX = ssl._create_unverified_context()


def get(path, **q):
    url = U + path + ("?" + urllib.parse.urlencode(q) if q else "")
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=AUTH), timeout=60, context=CX).read())


print("projects:", [p["name"] for p in get("/api/public/projects")["data"]])
t = get("/api/public/traces", limit=3)
print("traces total:", t["meta"].get("totalItems"))
for tr in t["data"][:2]:
    print("trace fields:", sorted(tr))
    print("  name", tr.get("name"), "| userId", tr.get("userId"), "| tags", tr.get("tags"), "| ts", tr.get("timestamp"))
    print("  metadata keys:", sorted((tr.get("metadata") or {}).keys()))
o = get("/api/public/observations", limit=3, type="GENERATION")
for ob in o["data"][:2]:
    print("observation fields:", sorted(ob))
    md = ob.get("metadata") or {}
    print("  model", ob.get("model"), "| level", ob.get("level"), "| status", (ob.get("statusMessage") or "")[:120],
          "| latency", ob.get("latency"), "| usage", ob.get("usage") or ob.get("usageDetails"))
    for k, v in sorted(md.items()):
        s = json.dumps(v, ensure_ascii=False)
        print("   md %s = %s" % (k, s[:140] if "key" not in k.lower() or "alias" in k.lower() else "<%d chars>" % len(s)))
# errors: do failed calls show up, with which fields
e = get("/api/public/observations", limit=2, level="ERROR")
print("ERROR observations:", e["meta"].get("totalItems"))
for ob in e["data"][:2]:
    print("  ", ob.get("startTime"), ob.get("model"), (ob.get("statusMessage") or "")[:200])
# daily metrics API (v1): traces and usage by userId
q = {"view": "observations", "dimensions": [{"field": "userId"}], "metrics": [{"measure": "count", "aggregation": "count"}],
     "filters": [], "fromTimestamp": "2026-10-01T00:00:00Z", "toTimestamp": "2026-11-01T00:00:00Z"}
try:
    m = get("/api/public/metrics", query=json.dumps(q))
    print("metrics by userId:", m["data"][:15])
except Exception as ex:
    print("metrics API:", ex)
