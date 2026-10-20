# Sakoo infra monitor

The London server cannot reach Sakoo (the VPN does not accept foreign IPs), and the cluster cannot reach Telegram.
So the work is split in two:

```
cluster (aip-mlops-service)                       London server 104.194.143.68
probe pod, every 60 s ── HTTP POST, HMAC-signed ──▶ receiver.py :8090 ──▶ Telegram bot
```

- **probe** (`k8s/probe.py`) runs inside the cluster and checks the infra-owned layers.
- **receiver** (`receiver.py`) checks the signature, keeps alert state, and messages Telegram when a check fails
  2 reports in a row, when it recovers, and when **no report arrives for 3 minutes** ("probe silent"). That last one
  is the alert you get when the whole cluster or its network is down.

| Check | Owner | Down means |
|---|---|---|
| `dns <name>` | infra | cluster DNS cannot resolve a service name |
| `ingress (MetalLB/traefik)` | infra | every public hostname fails TCP/TLS |
| `tls <host>` | us / infra | one hostname fails (ours), or its certificate expires within 14 days (infra) |
| `harbor health` | infra | `/api/v2.0/health` does not say healthy |
| `s3 (Ceph RGW)` | infra | `rook-ceph-rgw-core-shared-s3` does not answer |
| `kubernetes API` | infra | `/livez` does not answer |
| `node <name>` | infra | node not Ready, under memory/disk/PID pressure, or fewer GPUs allocatable than installed |
| probe silent | infra | no report for 3 min: cluster, its internet egress, or the probe pod is down |

## 1. Receiver (London server, done once)

```bash
bash ~/infra-monitor/setup.sh      # asks for the bot token (hidden), finds the chat id, sends a test message
```
It writes `secrets.env` (token, chat id, and a generated `PROBE_KEY`), and a cron entry keeps `receiver.py` running.
Logs: `~/infra-monitor/receiver.log`. Health: `curl http://104.194.143.68:8090/health`.

## 2a. Probe on an always-on machine in Iran (VPN)

The VPN routes only the internal networks `172.30.151-153.0/24`, not the public ingress IP. `probe-host.json`
therefore sends every hostname to the internal ingress `172.30.153.101` (TLS SNI stays the hostname), checks each
node's kubelet port (`172.30.153.11-15:10250`) and the Kubernetes API (`172.30.153.100:6443/livez`). It first checks
its own tunnel (`vpn_iface`); if the VPN is down you get one "probe cannot see Sakoo" alert instead of false infra alerts.

```bash
PROBE_CONFIG=probe-host.json PROBE_KEY=<from London secrets.env> python3 k8s/probe.py --print   # one round
PROBE_CONFIG=probe-host.json PROBE_KEY=... python3 k8s/probe.py                                 # loop
```
Tested 2026-10-19 from the laptop: all checks ran and the London receiver raised the right alert (chatpilot 503).

## 2b. Probe in the cluster (through GitOps, needs egress to London)

1. Copy `k8s/` into the GitOps repo next to the other aip-mlops-service apps.
2. `secret.example.yaml` → `secret.sops.yaml`, paste `PROBE_KEY` from the server's `secrets.env`, `sops -e -i`.
3. Set the image in `probe.yaml` to a pinned Harbor copy of `python:3.12-slim`.
4. Merge request → Flux applies it. The ClusterRole (read nodes) and the CiliumNetworkPolicy (egress to London :8090)
   may need the infra team's review.

Check it: `kubectl logs -n aip-mlops-service deploy/sakoo-infra-probe` should stay empty (only failures are logged),
and Telegram stays quiet until something breaks.

## Test

`python3 test_monitor.py` runs probe → receiver over HTTP: alert after 2 failures, no repeats, recovery,
rejected bad signatures, probe silent and back.
