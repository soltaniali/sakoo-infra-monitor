# HANDOFF — infra-monitor

Read `AGENTS.md` first (layout, commands, rules).

## Current state (2026-11-17)

- Station (AIP-IRAN `~/station`, container `sakoo-probe-station`) runs the local `station/ops.py`, `ops_web.py`, `ops.js`, `tickets.py` (deployed 2026-11-17; backups `~/station/*.bak-20261117`).
- London (`ssh mlops`, `~/infra-monitor`) runs the local `receiver.py` (deployed 2026-11-17; backup `receiver.py.bak-20261117`).
- `37bb7ca` committed, not pushed. GPU holder backfill runs on the station (3 days per round, from 2026-11-07).

## Recent changes

- 2026-11-17 — London receiver probe alerts (INFRA DOWN, MLOPS ISSUE, RECOVERED, …) now end with `Assignees:` (`receiver.assignee`: infra/capacity → Infra, else asset person, else MLOps); deployed to London (backup `receiver.py.bak-20261117`). GPU holder history: `daily` scope `gpu` (name `node#idx	ns	workload`, metrics `held_h`, `busy`) from DCGM pod labels in 5-min steps; backfilled for the days Prometheus still holds (meta `dailygpu:<day>`, from 2026-11-07). `ops.gpu_holders` + `/api/gpu_holders` + Capacity section "Who held each GPU" (hours, % of range, free %, main holder per day, per-holder GPU-days). Weekly/monthly = 7 d / 30 d range buttons; a full month exists from ~2026-12-07. Station deployed (backups `*.bak-20261117`). Asked by Mohammad Aghaei (mlops), 2026-11-17.
- 2026-11-14 — Tickets tab `Error: StopIteration` fixed: Prometheus 3.11 drops `__name__` after `increase()`, so `tickets.key_usage` now runs one query per metric (`USAGE_Q` dict). Page cache now warms at start: default 7-day pages (`DEFAULT_PAGES`) plus pages opened before the restart (`/data/page-keys.json`). Deployed; backups `tickets.py.bak-20261114`, `ops_web.py.bak2-20261114`.
- 2026-11-14 — Speed: page data (users, capacity, models, tickets, trends, …) is cached 15 min in `ops_web.py` (`page_data`, `PAGE_TTL`), served at once and refreshed in the background (`page_warmer`; pages opened in the last 2 days). A range ending now is one cache entry (`page_key`). Live/cheap endpoints stay uncached (`LIVE`: state, incidents, logs…), so errors show and alert at once (collector still every 60 s). JSON and ops.js are gzipped (ops.js 100 KB → 30 KB). Measured before on the station (7 d): users 14 s, capacity 14 s, tickets 19 s, models 4 s. Deployed; backups `~/station/ops_web.py.bak-20261114`, `ops.js.bak2-20261114`. After a restart the first visit of each page is still slow once.
- 2026-11-14 — New user = new `user-*` namespace: change type `new-user`, Telegram `🆕 NEW USER user-x/user-x: x (Real name)`, overview tile 9 "New users" (7 days), filter on Changes. Capacity "Each GPU" shows the pod's workload / KubeAI model (or user's real name) instead of only the namespace (`cards[].service`).
- 2026-11-14 — Every OPEN/CLOSED alert ends with `Assignees: <name>` (`ops.assignee`, `Collector.is_model`). Infra/capacity → `Infra`; otherwise the person from `assets` in `station/probe-station.json` by exact asset/Service name, then name prefix; KubeAI models → the `kubeai` asset's person; fallback `MLOps`, also for shared namespaces like gitlab and casdoor (Ali: "consider them as mlops for now").

## Open work / next steps

- `llm-proxy` (LiteLLM) alerts say `MLOps` on purpose (Ali, 2026-11-14): no single owner.
- Deploy a code change: `scp station/ops.py AIP-IRAN:~/station/ops.py`, then `ssh AIP-IRAN docker exec sakoo-probe-station pkill -f ops_web.py` (restarts in 10 s).
- Deploy the receiver: `scp receiver.py mlops:~/infra-monitor/`, then `ssh mlops pkill -f '^python3 receiver.py$'` (cron restarts it within 1 min).
- Push `37bb7ca` when Ali says so.
- Check the Capacity "Who held each GPU" section in the browser; if holder names from pod names look wrong, switch `ops.pod_workload` to `kube_pod_owner`.
