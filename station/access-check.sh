#!/bin/sh
# Start both VPNs like entrypoint.sh, then run the read-only access check. Usage (in station/):
#   docker compose run --rm --entrypoint sh -v "${PWD}/access-check.sh:/app/access-check.sh:ro" -v "${PWD}/access-check.py:/app/access-check.py:ro" probe /app/access-check.sh
S=/secrets
common="--pull-filter ignore redirect-gateway --pull-filter ignore dhcp-option --script-security 1 --verb 3 --auth-nocache"
openvpn --config "$S/cluster.ovpn" --dev tun0 --auth-user-pass "$S/cluster-auth.txt" $common \
  --data-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305:AES-256-CBC > /tmp/vpn-cluster.log 2>&1 &
openvpn --config "$S/endpoints.ovpn" --dev tun1 $common \
  --data-ciphers AES-256-GCM:AES-128-GCM:AES-256-CBC > /tmp/vpn-endpoints.log 2>&1 &
for i in $(seq 60); do [ -e /sys/class/net/tun0 ] && [ -e /sys/class/net/tun1 ] && break; sleep 1; done
sleep 3
ip -br addr | grep tun || { echo "VPN did not come up"; tail -5 /tmp/vpn-cluster.log; exit 1; }
PROBE_CONFIG=/app/probe-station.json python3 -u /app/access-check.py
