#!/usr/bin/env bash
# One-shot setup of the receiver on the London server. Run: bash ~/infra-monitor/setup.sh
# Asks for the Telegram bot token (hidden). No sudo needed.
set -euo pipefail
cd "$(dirname "$0")"
umask 077

if [ ! -s secrets.env ] || ! grep -q '^TG_TOKEN=.\+' secrets.env; then
  read -rsp "Bot token (input hidden): " TG_TOKEN; echo
  echo "Send any message to the bot (or in the group the bot is in), then press Enter."; read -r _
  TG_CHAT_ID=$(curl -s "https://api.telegram.org/bot${TG_TOKEN}/getUpdates" | python3 -c 'import json,sys; u=json.load(sys.stdin).get("result",[]); ids=[x[k]["chat"]["id"] for x in u for k in ("message","channel_post","my_chat_member") if k in x]; print(ids[-1] if ids else "")')
  [ -n "$TG_CHAT_ID" ] || { echo "No message found. Send one to the bot and run setup.sh again."; exit 1; }
  PROBE_KEY=$(cat probe.key 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(32))')  # probe.key: shared with the probe station
  printf 'TG_TOKEN=%s\nTG_CHAT_ID=%s\nPROBE_KEY=%s\n' "$TG_TOKEN" "$TG_CHAT_ID" "$PROBE_KEY" > secrets.env
  echo "chat id: $TG_CHAT_ID"
fi
set -a; . ./secrets.env; set +a
python3 receiver.py --test

# keep the receiver running: start now, after reboot, and every minute if it died
start="cd $PWD && set -a && . ./secrets.env && set +a && (pgrep -f '^python3 receiver.py$' >/dev/null || nohup python3 receiver.py >> receiver.log 2>&1 &)"
( crontab -l 2>/dev/null | grep -v 'receiver.py' ; echo "@reboot $start"; echo "* * * * * $start" ) | crontab -
pkill -f '^python3 receiver.py$' || true   # replace a dry-run receiver, if any
bash -c "$start"; sleep 2
curl -s http://127.0.0.1:8090/health
echo "Receiver running. Put this key in the cluster Secret (k8s/secret.example.yaml), it is in secrets.env as PROBE_KEY."
echo "Optional, stops the unused VPN client: sudo systemctl disable --now openvpn-client@sakoo"
