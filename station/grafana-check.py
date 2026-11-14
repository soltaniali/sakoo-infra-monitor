"""Read-only look at what Grafana's Prometheus holds for the ticket questions (GET only, through ops.prom).
Prints metric names, label names and a few counts; API keys appear only as LiteLLM's sha256 hashes, cut to 8 chars.
Run: docker exec sakoo-probe-station python3 /tmp/grafana-check.py"""
import json
import sys
import time

sys.path.insert(0, "/app")
import ops  # noqa: E402

col = ops.Collector.__new__(ops.Collector)
col.meta, col.set_meta = (lambda k: None), (lambda k, v: None)
q = col.prom


def show(title, query, fmt=lambda m: json.dumps(m), limit=15):
    try:
        rows = q(query)
    except Exception as e:
        print("%-38s ERROR %s" % (title, e))
        return []
    print("%-38s %d series" % (title, len(rows)))
    for r in rows[:limit]:
        print("   ", fmt(r["metric"]), r["value"][1])
    return rows


lo = q("prometheus_tsdb_lowest_timestamp_seconds")
if lo:
    print("prometheus data starts:", time.strftime("%Y-%m-%d %H:%M", time.gmtime(float(lo[0]["value"][1]))), "UTC")
show("litellm metric names", 'count by (__name__) ({__name__=~"litellm_.*(requests|tokens|spend|failed).*_total"})', limit=30)
rows = show("labels of litellm_spend_metric_total", "topk(1, litellm_spend_metric_total)",
            fmt=lambda m: sorted(k for k in m), limit=1)
show("spend by key alias, team (30d)",
     'topk(25, sum by (api_key_alias, team_alias, hashed_api_key) (increase(litellm_spend_metric_total[30d])))',
     fmt=lambda m: (m.get("api_key_alias"), m.get("team_alias"), (m.get("hashed_api_key") or "")[:8]), limit=25)
show("requests by model (30d)",
     'topk(20, sum by (model) (increase(litellm_proxy_total_requests_metric_total[30d])))', fmt=lambda m: m.get("model"), limit=20)
show("failures by model, code (30d)",
     'topk(20, sum by (model, exception_status) (increase(litellm_proxy_failed_requests_metric_total[30d])))',
     fmt=lambda m: (m.get("model"), m.get("exception_status")), limit=20)
show("user-* namespaces with pods", 'count by (namespace) (kube_pod_info{namespace=~"user-.*"})', fmt=lambda m: m["namespace"], limit=40)
show("CPU cores per user ns (now)",
     'sum by (namespace) (rate(container_cpu_usage_seconds_total{namespace=~"user-.*",container!=""}[5m]))',
     fmt=lambda m: m["namespace"], limit=40)
show("GPU requested per ns", 'sum by (namespace) (kube_pod_container_resource_requests{resource="nvidia_com_gpu"})',
     fmt=lambda m: m["namespace"], limit=40)
show("DCGM labels", "topk(1, DCGM_FI_DEV_GPU_UTIL)", fmt=lambda m: sorted(m), limit=1)
show("pod restarts 24h by ns", 'topk(15, sum by (namespace) (increase(kube_pod_container_status_restarts_total[24h])) > 0)',
     fmt=lambda m: m["namespace"])
