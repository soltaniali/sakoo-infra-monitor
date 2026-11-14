# HANDOFF — infra-monitor

Read `AGENTS.md` first (layout, commands, rules).

## Current state (2026-11-14)

- Station (AIP-IRAN `~/station`, container `sakoo-probe-station`) runs the local `station/ops.py` (deployed 2026-11-14; backup `~/station/ops.py.bak-20261111`).
- All work committed and pushed (`2a5705a`).

## Recent changes

- 2026-11-14 — Every OPEN/CLOSED alert ends with `Assignees: <name>` (`ops.assignee`, `Collector.is_model`). Infra/capacity → `Infra`; otherwise the person from `assets` in `station/probe-station.json` by exact asset/Service name, then name prefix; KubeAI models → the `kubeai` asset's person; fallback `MLOps`, also for shared namespaces like gitlab and casdoor (Ali: "consider them as mlops for now").

## Open work / next steps

- `llm-proxy` (LiteLLM) alerts say `MLOps` on purpose (Ali, 2026-11-14): no single owner.
- Deploy a code change: `scp station/ops.py AIP-IRAN:~/station/ops.py`, then `ssh AIP-IRAN docker exec sakoo-probe-station pkill -f ops_web.py` (restarts in 10 s).
- Nothing uncommitted (2026-11-14).
