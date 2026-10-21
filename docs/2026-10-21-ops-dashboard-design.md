# Sakoo ops dashboard: design record (2026-10-21)

## Goal
One page for the whole MLOps team that detects the 8 maintenance events on Sakoo (سکو) automatically and diagnoses them:
cause, owner, evidence, suggested fix. Other teams also change Sakoo (features, models, services), so every symptom is
shown next to the changes that happened just before it, with the owning team.

## Decisions (Ali)
- **Where:** on the probe station in Iran (`AIP-IRAN`, 85.198.48.95), inside the probe container, so it shares the
  one VPN session per profile. Logs and inventory stay in Iran; only short alert texts go to Telegram through London.
- **Who:** the whole MLOps team, one account each (scrypt-hashed, `ops_web.py adduser`).
- **Automation level L1, diagnose only:** no write access is used. The collector sends GET requests only; humans fix.
- **Optional integrations** are shown only when their env is set: litellm (summaries), GitLab (who changed it),
  Grafana (GPU %).

## Access found (2026-10-21, `station/access-check.py`)
The station kubeconfig reads pods, logs, deployments, events and ingresses in 59 of 85 namespaces, and KubeAI models in
`aip-mlops-service`. Infra namespaces (monitoring, flux-system, rook-ceph, …) and cluster-wide lists are 403. The same
certificate can also write in `aip-mlops-service`, which is why the code has a single GET function (`ops.kget`) and the
admins are asked for a read-only ServiceAccount.

## Event → signal
| # | Event | Signal |
|---|---|---|
| 1 | bug | CrashLoopBackOff, OOMKilled, restarts, failing probe, new error signature in our logs |
| 2 | down | ready < desired, model ready < minReplicas, Pending, image pull, probe endpoint checks |
| 3 | feature | image tag / pod template diff; verdict after 15 min |
| 4 | new service | new workload / hostname / namespace; hygiene gaps listed |
| 5 | new model | new / changed KubeAI Model; ready within 30 min |
| 6 | model info | Models page |
| 7 | logs | Logs page, error signatures |
| 8 | infra or us | probe checks by owner; capacity, Ceph and Harbor causes go to infra |

## Not done (on purpose)
- No fixes from the page (L2/L3 were offered; L1 chosen).
- Polling instead of the Kubernetes watch API: fine for 59 namespaces every 5 min over the VPN; switch to watch if the
  VPN traffic matters.
- Port on 127.0.0.1 with SSH tunnels; publishing it to a team network needs TLS files and `OPS_ALLOW_CIDRS` (README).
