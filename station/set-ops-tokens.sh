#!/bin/sh
# Writes the GitLab and Grafana settings into secrets/ops.env (tokens typed hidden, never shown or kept in history),
# then restarts the station so the ops dashboard picks them up. Run with a terminal: ssh -t AIP-IRAN sh station/set-ops-tokens.sh
set -eu
cd "$(dirname "$0")"
umask 077
touch secrets/ops.env

setv() {  # replace one NAME=value line
  grep -v "^$1=" secrets/ops.env > secrets/ops.env.new || true
  printf '%s=%s\n' "$1" "$2" >> secrets/ops.env.new
  mv secrets/ops.env.new secrets/ops.env
}
secret() {  # hidden input; empty keeps the current value
  stty -echo; printf '%s (hidden, Enter = keep current): ' "$1" >&2; read -r v; stty echo; echo >&2
  [ -z "$v" ] || setv "$1" "$v"
}

setv GITLAB_URL https://gitprod.aip.sharif.ir
setv GITLAB_PROJECTS prod/prod-cluster,prod/core-mlops
setv GRAFANA_URL https://monitoringprod.aip.sharif.ir
secret GITLAB_TOKEN
secret GRAFANA_TOKEN
echo "secrets/ops.env now sets: $(cut -d= -f1 secrets/ops.env | tr '\n' ' ')"
docker compose up -d
echo "waiting for the VPN, then testing the tokens..."
for i in $(seq 30); do docker exec sakoo-probe-station test -e /sys/class/net/tun0 2>/dev/null && break; sleep 2; done
sleep 8
docker exec sakoo-probe-station python3 /app/ops_web.py check
