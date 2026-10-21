# Probe station (VPNs + probe in one container)

Runs the Sakoo infra probe on any machine in Iran that can reach both VPN servers and the London receiver.
The same image and `compose.yaml` run on Windows (Docker Desktop) and on Linux. Runs on the Iran host 85.198.48.95 (`AIP-IRAN`, user mlops, ~/station, since 2026-10-21). Earlier: 213.233.184.128 (removed), first tested on Ali's laptop.

```
container: tun0 = cluster VPN (se-user15, 172.30.151-153.0/24)  ─┐
           tun1 = endpoints VPN (pilot-user03, 185.220.226.23)  ─┼─ probe.py, every 60 s ──▶ London :8090 ──▶ Telegram
           kubeconfig (core@prod, read events)                  ─┘
```

| Check | Path | Owner when it fails |
|---|---|---|
| `probe VPN cluster` / `probe VPN endpoints` | own tunnel | monitor (not Sakoo) |
| `ingress (MetalLB/traefik)` | all hosts via 172.30.153.101 fail | infra |
| `host <name>` | one host fails or returns 5xx | us |
| `node <name> (kubelet)` | 172.30.153.11-15:10250 | infra |
| `kubernetes API` | 172.30.153.100:6443/livez | infra |
| `k8s GPU/CPU/RAM capacity`, `node not ready`, `volumes (Ceph)`, `Harbor reachable` | Warning events, last 10 min, in `event_namespaces` | infra |
| `public edge (public IP/firewall/NAT)` | internal path works, all hosts via 185.220.226.23 fail | infra |

## Files in `secrets/` (never commit; `.gitignore` covers them)

| File | What | Who |
|---|---|---|
| `cluster.ovpn` | cluster VPN profile | copied from Downloads |
| `endpoints.ovpn` | endpoints VPN profile | copied from Downloads |
| `kubeconfig` | core@prod client certificate | copied from Downloads |
| `cluster-auth.txt` | VPN username + password | **you**, with `set-vpn-auth.ps1` / `set-vpn-auth.sh` |
| `probe.env` | `PROBE_KEY=` shared with London (`~/infra-monitor/probe.key`) | generated |

## Install on the Iran Linux host

```bash
# 1. Docker
curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER   # log out and in once
# 2. copy this folder (with secrets/) to the host, e.g. scp -r station user@host:~/
cd ~/station
sh set-vpn-auth.sh                         # type the VPN password (hidden)
cp ../k8s/probe.py . 2>/dev/null || true   # probe.py must sit next to compose.yaml (mounted into the container)
docker compose up -d --build               # no Docker Hub (Iran)? build on another machine, then:
                                           #   docker save sakoo-probe-station:1 | gzip > img.tgz ; scp ; gunzip -c img.tgz | docker load
                                           #   docker compose up -d --no-build
docker compose run --rm probe --print      # one round, printed
docker compose logs -f                     # running log
```
Then stop the laptop copy (`docker compose down` in this folder on the laptop), so only one station reports.

On the laptop: `docker compose up -d --build` in this folder. It stops when the laptop sleeps, which London reports as
"probe silent".

Updating the probe: copy the new `k8s/probe.py` to `station/probe.py` on the host, then `docker compose restart`.
The probe takes its clock from the London receiver, so a host whose NTP is blocked (this one) still works.

## Ops dashboard (same container)

A web page for the MLOps team that watches the 8 maintenance events and **diagnoses** them: cause, owner, evidence and a
suggested fix. It never changes the cluster: the collector sends only GET requests to Kubernetes; humans do the fix.

```
probe.py ──▶ /data/probe-results.json ─┐
ops.py (collector, every 60 s) ────────┼─▶ /data/ops.db ─▶ ops_web.py :8443 (login) ─▶ ops.html / ops.js
  our namespace every round,           │
  all 59 readable namespaces + our     └─▶ new incidents / changes ─▶ London POST /alert ─▶ Telegram
  pods' logs every 5th round
```

| # | Event | How it is detected | Page |
|---|---|---|---|
| 1 | bug in a service | CrashLoopBackOff, OOMKilled, 3+ restarts in 15 min, failing probes, new error signature in our logs | Incidents |
| 2 | service down | replicas ready < desired, KubeAI model ready < minReplicas, Pending pods, image pull, probe endpoint checks | Incidents |
| 3 | feature added | image tag or pod template changed; judged "healthy" or "bad" 15 min later | Changes |
| 4 | new service | new Deployment/StatefulSet (with missing readiness probe / limits / tag listed), new hostname, new namespace | Changes |
| 5 | new model | new or changed KubeAI Model; "ready" or "not ready" within 30 min | Changes, Models |
| 6 | model info | engine, profile, source, replicas, pods, restarts (+ GPU %, litellm registration when configured) | Models |
| 7 | logs | live log of any pod in the readable namespaces, errors-only filter (+ LLM summary when configured) | Logs |
| 8 | infra or us | the probe's checks grouped by owner | Infra or us |

A symptom within 30 min after a change of the same object is linked to it ("bad release a → b", fix: roll back).
Owner: `aip-mlops-service` and per-user ClearML agents = MLOps; capacity, Ceph, Harbor and probe infra checks = infra team;
any other namespace = its team. Incidents in `user-*` namespaces are on the page but not sent to Telegram (except our agents).
An incident closes after 2 clean rounds (a new log error: after 30 quiet minutes). The first hour only learns the error
signatures that already exist.

Use it (each person once: an account; every time: an SSH tunnel, because the port listens on the host's 127.0.0.1 only):

```bash
ssh -t AIP-IRAN docker exec -it sakoo-probe-station python3 /app/ops_web.py adduser <name>   # password asked twice, hidden
ssh -N -L 8443:127.0.0.1:8443 AIP-IRAN                                                      # then open http://localhost:8443
ssh AIP-IRAN docker exec sakoo-probe-station python3 /app/ops_web.py status                # stored state, read-only
```

Optional integrations: put them in `secrets/ops.env` (never commit), then `docker compose up -d`. A missing one only hides its column.

```bash
LITELLM_URL=https://<litellm host>/v1     LITELLM_KEY=...   LITELLM_MODEL=<model alias>   # summaries
GITLAB_URL=https://gitprod.aip.sharif.ir  GITLAB_TOKEN=<read_api token>  GITLAB_PROJECTS=prod/prod-cluster,prod/core-mlops
GRAFANA_URL=https://monitoringprod.aip.sharif.ir  GRAFANA_TOKEN=<Viewer service-account token>  # GRAFANA_DATASOURCE_UID=...
```

Opening it to the team network instead of SSH: create `secrets/ops-tls.crt` + `secrets/ops-tls.key` (HTTPS is then on),
set `OPS_ALLOW_CIDRS=<team ranges>` in `secrets/ops.env`, change the port to `"8443:8443"` in `compose.yaml`.
Never publish it to the whole internet: it shows logs of every readable namespace.

Updating: copy the changed `ops*.py`, `ops.html`, `ops.js` to `~/station`, then
`docker exec sakoo-probe-station pkill -f ops_web.py` (it restarts in 10 s; the probe and VPNs keep running).
Test: `python3 station/test_ops.py` (fake cluster; needs `station/probe.py` = copy of `k8s/probe.py`).
