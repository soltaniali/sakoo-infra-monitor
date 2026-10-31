#!/bin/sh
# Writes your Grafana login into secrets/ops.env (password typed hidden), restarts the station, and shows what that
# login can read: dashboards, datasources, and which Prometheus jobs exist (kube-state-metrics, DCGM, LiteLLM...).
# Grafana's Prometheus scrapes the whole cluster, so this covers what the kubeconfig cannot read. Reads only (GET).
# Run: ssh -t AIP-IRAN sh station/set-grafana-login.sh
set -eu
cd "$(dirname "$0")"
umask 077
touch secrets/ops.env

setv() {  # replace one NAME=value line
  grep -v "^$1=" secrets/ops.env > secrets/ops.env.new || true
  printf '%s=%s\n' "$1" "$2" >> secrets/ops.env.new
  mv secrets/ops.env.new secrets/ops.env
}

setv GRAFANA_URL https://monitoringprod.aip.sharif.ir
printf 'Grafana username: ' >&2; read -r u
[ -z "$u" ] || setv GRAFANA_USER "$u"
stty -echo; printf 'Grafana password (hidden, Enter = keep current): ' >&2; read -r v; stty echo; echo >&2
[ -z "$v" ] || setv GRAFANA_PASSWORD "$v"
docker compose up -d
sleep 5
docker exec sakoo-probe-station python3 -c '
import base64, json, os, ssl, urllib.parse, urllib.request
g = os.environ["GRAFANA_URL"].rstrip("/")
auth = {"Authorization": "Basic " + base64.b64encode((os.environ["GRAFANA_USER"] + ":" + os.environ["GRAFANA_PASSWORD"]).encode()).decode()}
cx = ssl._create_unverified_context()
def get(p):
    return json.loads(urllib.request.urlopen(urllib.request.Request(g + p, headers=auth), timeout=30, context=cx).read())
me = get("/api/user")
print("login OK:", me.get("login"), "| role in org:", get("/api/user/orgs")[0].get("role"))
print("dashboards:", ", ".join(d["title"] for d in get("/api/search?type=dash-db&limit=200")) or "none")
ds = list(get("/api/frontend/settings")["datasources"].values())
print("datasources:", ", ".join("%s (%s)" % (d["name"], d["type"]) for d in ds))
for d in ds:
    if d["type"] != "prometheus":
        continue
    try:
        r = get("/api/datasources/proxy/uid/%s/api/v1/query?query=%s" % (d["uid"], urllib.parse.quote("count by (job) (up)")))
        print("Prometheus %s jobs:" % d["name"], ", ".join(sorted(s["metric"].get("job", "?") for s in r["data"]["result"])))
        r = get("/api/datasources/proxy/uid/%s/api/v1/label/__name__/values" % d["uid"])
        print("  litellm metrics:", ", ".join(n for n in r["data"] if n.startswith("litellm_"))[:1500] or "none")
    except Exception as e:
        print("Prometheus %s query: %s" % (d["name"], e))'
