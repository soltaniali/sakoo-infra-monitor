# AGENTS.md — AIP (Sakoo) infra monitor + ops dashboard

Operating manual for any coding agent (Cursor, opencode, Codex, DeepSeek,
Claude Code, …). Read next: [README.md](README.md) (probe → receiver →
Telegram), [station/README.md](station/README.md) (probe station, ops
dashboard), `docs/2026-10-21-ops-dashboard-design.md` (decisions).

**Last updated:** 2026-11-14

## 1. What it is

Monitoring for the AIP (formerly "Sakoo", سکو) MLOps Kubernetes cluster that
the MLOps team (namespace `aip-mlops-service`) uses but does not own.

```
Iran probe station 85.198.48.95 (AIP-IRAN, user mlops, ~/station, Docker)
  tun0 cluster VPN 172.30.151-153.0/24 + tun1 endpoints VPN + read kubeconfig
  probe.py every 60 s ──HMAC POST──▶ London 104.194.143.68 receiver.py :8090 ──▶ Telegram bot
  ops.py collector (GET only) ─▶ ops.db ─▶ ops_web.py :8443 (login) ─▶ ops.html/ops.js
```

- `k8s/probe.py` — checks: DNS, ingress, TLS, Harbor, Ceph S3, k8s API, nodes/GPUs, 29 hostnames, 23 MLOps assets, capacity events, VPN self-check. `k8s/` also deploys it in-cluster via GitOps (optional).
- `receiver.py` — HMAC check, alert state (fail 2×, recovery, "probe silent" 3 min), Telegram bot (`/status`, `/dashboard`, `/channel @name`), `history.db` uptime dashboard.
- `dashboard.py` — uptime history page.
- `station/` — container (OpenVPN ×2 + probe + ops), `ops.py` collector (8 maintenance events: bug, down, feature, new service, new model, model info, logs, infra-or-us), `ops_web.py` (scrypt users, `adduser`), `tickets.py` (support tickets vs monitoring data), integrations: GitLab, Grafana, Langfuse, LiteLLM keys, Casdoor logins, mlops-core trainings. `set-*.sh` scripts write secrets interactively.
- Public HTTPS for the ops page: `85.198.48.95:8443` (`OPS_ALLOW_CIDRS`).

## 2. Status snapshot (2026-11-14)

- Remote `github.com/soltaniali/sakoo-infra-monitor`, branch `main`, last pushed commit `2a5705a` (2026-11-14, shared namespaces → MLOps). Local commit `233ef77` (new-user alert/tile, GPU holder names) deployed and pushed.
- Automation level **L1: diagnose only.** The station kubeconfig can write in
  `aip-mlops-service`; code uses a single GET helper (`ops.kget`). A read-only ServiceAccount was requested from the admins.

## 3. Commands

```bash
python3 test_monitor.py                         # probe → receiver over HTTP: alerts, recovery, bad signature, probe silent
cd station && python3 test_ops.py               # ops collector tests
PROBE_CONFIG=probe-host.json PROBE_KEY=... python3 k8s/probe.py --print   # one probe round
# on the Iran host (~/station):
docker compose up -d --build ; docker compose run --rm probe --print ; docker compose logs -f
# updating the probe: copy k8s/probe.py to station/probe.py on the host, docker compose restart
```

No Docker Hub from Iran: build elsewhere, `docker save | gzip`, scp, `docker load`, `up --no-build`.

## 4. Rules

- Never commit `station/secrets/`, `secrets.env`, `*.ovpn`, kubeconfigs, tokens, `station/data/`. Never print them.
- The collector and checks send **GET requests only** to the cluster. No fixes from the page.
- Only short alert texts leave Iran (to London/Telegram); logs and inventory stay on the station.
- Visible text says "AIP", not "Sakoo". Times shown in Tehran time.
- Owner of every failure is explicit (infra / us / capacity / monitor) — keep that mapping when adding checks.
- Every alert ends with `Assignees:` (`ops.assignee`): Infra for infra/capacity, else the person from `assets` in `probe-station.json` (name, Service or prefix match; KubeAI models → the `kubeai` person), else MLOps — also for shared namespaces like gitlab, casdoor (Ali, 2026-11-14).
- Commits: plain imperative sentence, often prefixed by area (`Ops: …`, `Station: …`, `Dashboard: …`).

## 5. Traps

- VPN accepts no foreign IPs; London cannot reach the cluster; cluster cannot reach Telegram.
- The Iran host's NTP is blocked: the probe takes its clock from the receiver.
- `set -e` once ended the ops restart loop; keep `entrypoint.sh` loop alive.
- A "connected" VPN tunnel can pass no traffic: the probe compares the internal and public paths before blaming apps.
- Only one station may report at a time (stop the laptop copy).

## 6. Change log

- 2026-11-14 — new `user-*` namespace = `new-user` change: Telegram alert + overview tile 9; Capacity GPU list names the holder workload/model. `233ef77`, deployed.
- 2026-11-14 — shared namespaces (gitlab, casdoor, …) assigned to MLOps instead of the namespace name.
- 2026-11-14 — alerts carry `Assignees:` with exact person names from the asset list; deployed to the station.
- 2026-11-13 — `AGENTS.md` added.
- 2026-11-14 `ea35e6c` — new station checks (agents, datasets, Grafana, Langfuse, allowed hosts), ops report (fa).
- 2026-11-08 `e2c8872` — per-user timeline with day-by-day event list.
- 2026-11-04 — GPU slices, GPU wait list, agent limits; daily usage history/charts; date range + whole-cluster view; pod logs saved on station; mlops-core trainings.
- 2026-11-01…03 — Casdoor login records; LiteLLM keys tab; tickets checks; users page by person; Grafana/Langfuse per-user usage.
- 2026-10-26…31 — Sakoo → AIP rename; Users page; Capacity/Trends tabs; `/channel` forwarding; Tickets tab.
- 2026-10-21 — ops dashboard on the probe station (8 events), public HTTPS 8443, station moved to 85.198.48.95, uptime history dashboard, 29 hostnames, 23 assets.
- 2026-10-20 `8f506df` — first version: probe station, receiver, Telegram bot.

Update §2 and add a line here on every change.
