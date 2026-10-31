#!/bin/sh
# Writes the ticket site login into secrets/ops.env (password typed hidden, never shown or kept in history), then restarts
# the station so the Tickets tab starts reading. Run with a terminal: ssh -t AIP-IRAN sh station/set-ticket-login.sh
set -eu
cd "$(dirname "$0")"
umask 077
touch secrets/ops.env

setv() {  # replace one NAME=value line
  grep -v "^$1=" secrets/ops.env > secrets/ops.env.new || true
  printf '%s=%s\n' "$1" "$2" >> secrets/ops.env.new
  mv secrets/ops.env.new secrets/ops.env
}

setv TICKETS_URL https://support.aict-lab.ir
printf 'Ticket site user (email): ' >&2; read -r u; [ -z "$u" ] || setv TICKETS_USER "$u"
stty -echo; printf 'Ticket site password (hidden, Enter = keep current): ' >&2; read -r v; stty echo; echo >&2
[ -z "$v" ] || setv TICKETS_PASSWORD "$v"
echo "secrets/ops.env now sets: $(cut -d= -f1 secrets/ops.env | tr '\n' ' ')"
docker compose up -d
echo "waiting for the VPN, then testing the login..."
for i in $(seq 30); do docker exec sakoo-probe-station test -e /sys/class/net/tun0 2>/dev/null && break; sleep 2; done
sleep 8
docker exec sakoo-probe-station python3 -c "
import os, tickets
s = tickets.Site(os.environ['TICKETS_URL'], os.environ['TICKETS_USER'], os.environ['TICKETS_PASSWORD'])
s.login(); print('ticket site: login ok,', len(s.tickets()), 'tickets')"
