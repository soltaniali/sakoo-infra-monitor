# HANDOFF — infra-monitor

Read `AGENTS.md` first (layout, commands, rules).

## Current state (2026-11-14)

- Station (AIP-IRAN `~/station`, container `sakoo-probe-station`) runs the local `station/ops.py` (deployed 2026-11-14; backup `~/station/ops.py.bak-20261111`).
- Uncommitted work listed in `AGENTS.md` §2, plus the Assignees change below.

## Recent changes

- 2026-11-14 — Every OPEN/CLOSED alert ends with `Assignees: <name>` (`ops.assignee`, `Collector.is_model`). Infra/capacity → `Infra`; otherwise the person from `assets` in `station/probe-station.json` by exact asset/Service name, then name prefix; KubeAI models → the `kubeai` asset's person; fallback `MLOps` (owner `us`) or the team name.

## Open work / next steps

- `llm-proxy` (LiteLLM) alerts say `MLOps` on purpose (Ali, 2026-11-14): no single owner.
- Deploy a code change: `scp station/ops.py AIP-IRAN:~/station/ops.py`, then `ssh AIP-IRAN docker exec sakoo-probe-station pkill -f ops_web.py` (restarts in 10 s).
- Commit the uncommitted work in logical pieces.
