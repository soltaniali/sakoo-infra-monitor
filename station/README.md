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
