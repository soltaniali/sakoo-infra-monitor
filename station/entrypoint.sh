#!/bin/sh
# Start both VPNs as split tunnels, then run the probe. OpenVPN reconnects by itself; the probe reports a VPN that is
# down as "probe cannot see Sakoo" instead of false infra alerts.
#   cluster VPN   -> tun0: internal networks 172.30.151-153.0/24 (pushed by the server)
#   endpoints VPN -> tun1: the server pushes only the public ingress IP; a default route is refused
set -eu
S=/secrets
for f in cluster.ovpn endpoints.ovpn cluster-auth.txt kubeconfig probe.env; do
  [ -s "$S/$f" ] || { echo "missing $S/$f (see README)"; exit 1; }
done

common="--pull-filter ignore redirect-gateway --pull-filter ignore dhcp-option --script-security 1 --verb 3 --auth-nocache"
openvpn --config "$S/cluster.ovpn" --dev tun0 --auth-user-pass "$S/cluster-auth.txt" $common \
  --data-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305:AES-256-CBC > /tmp/vpn-cluster.log 2>&1 &
openvpn --config "$S/endpoints.ovpn" --dev tun1 $common \
  --data-ciphers AES-256-GCM:AES-128-GCM:AES-256-CBC > /tmp/vpn-endpoints.log 2>&1 &

for i in $(seq 60); do
  [ -e /sys/class/net/tun0 ] && [ -e /sys/class/net/tun1 ] && break
  sleep 1
done
echo "--- cluster VPN";   grep -E "Initialization Sequence|AUTH|TLS Error|PUSH_REPLY|error" /tmp/vpn-cluster.log | cut -c1-200 | tail -5
echo "--- endpoints VPN"; grep -E "Initialization Sequence|AUTH|TLS Error|PUSH_REPLY|error" /tmp/vpn-endpoints.log | cut -c1-200 | tail -5
ip -br addr | grep tun || true

set -a; . "$S/probe.env"; set +a
export PROBE_CONFIG=/app/probe-station.json
if [ "${1:-}" = "--print" ]; then exec python3 -u probe.py --print; fi
# ops dashboard + collector (diagnose only, GET-only to Kubernetes) in this container, so it shares the VPN tunnels
if [ -f /app/ops_web.py ]; then
  ( set +e; while true; do python3 -u /app/ops_web.py; echo "[ops] exited ($?), restart in 10 s"; sleep 10; done ) &   # set -e would end the loop
fi
exec python3 -u probe.py
