#!/usr/bin/env bash
# Sets the password people send to the bot with "/login <password>" to get alerts. Stores only a salted hash.
# Run on the London server: bash ~/infra-monitor/set-bot-password.sh
set -euo pipefail
cd "$(dirname "$0")"
umask 077
a=""; while [ -z "$a" ]; do read -rsp "New bot password (min 10 chars, hidden): " a; echo; done   # skip stray empty lines
b=""; while [ -z "$b" ]; do read -rsp "Again: " b; echo; done
[ "$a" = "$b" ] || { echo "The two entries differ."; exit 1; }
[ ${#a} -ge 10 ] || { echo "Too short."; exit 1; }
h=$(BOTPW="$a" python3 -c 'import hashlib,os,secrets; s=secrets.token_bytes(16); print("pbkdf2$%s$%s" % (s.hex(), hashlib.pbkdf2_hmac("sha256", os.environ["BOTPW"].encode(), s, 200000).hex()))')
{ grep -v '^BOT_PASSWORD_HASH=' secrets.env; echo "BOT_PASSWORD_HASH='$h'"; } > secrets.env.new && mv secrets.env.new secrets.env

pkill -f '^python3 receiver.py$' || true   # restart with the new hash; cron would also restart it within a minute
sleep 1
set -a; . ./secrets.env; set +a
nohup python3 receiver.py >> receiver.log 2>&1 &
sleep 2
curl -s http://127.0.0.1:8090/health
echo "Done. Anyone who sends  /login <password>  to the bot now gets every alert. /logout stops them."
