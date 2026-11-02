#!/bin/sh
# Stores a Casdoor access key + secret for the ops dashboard in secrets/ops.env (secret typed hidden), after testing it.
# The key has all the rights of the user it belongs to (here: Ali's own admin user, no other user can be added).
# The station only reads login records. Run: ssh -t AIP-IRAN sh station/set-casdoor-key.sh
set -eu
cd "$(dirname "$0")"
umask 077
touch secrets/ops.env

setv() {  # replace one NAME=value line
  grep -v "^$1=" secrets/ops.env > secrets/ops.env.new || true
  printf '%s=%s\n' "$1" "$2" >> secrets/ops.env.new
  mv secrets/ops.env.new secrets/ops.env
}

url=https://iampilot.aipsharif.ir
printf 'Access key: ' >&2; read -r k
stty -echo; printf 'Access secret (hidden): ' >&2; read -r v; stty echo; echo >&2
[ -n "$k" ] && [ -n "$v" ] || { echo "key or secret empty, nothing changed"; exit 1; }

# GET only. The secret goes through stdin, so it is not in any process list. Prints counts and field names, no values.
printf '%s\n%s' "$k" "$v" | docker exec -i sakoo-probe-station python3 -c "
import json, ssl, sys, time, urllib.parse, urllib.request
k, v = sys.stdin.read().split('\n', 1)
q = urllib.parse.urlencode({'accessKey': k.strip(), 'accessSecret': v.strip(), 'p': 1, 'pageSize': 1, 'owner': 'organization_sharif'})
for i in range(4):
    try:
        r = json.loads(urllib.request.urlopen('$url/api/get-records?' + q, timeout=30, context=ssl._create_unverified_context()).read())
        break
    except OSError as e:
        print('  try', i + 1, 'failed:', type(e).__name__)
        if i == 3:
            sys.exit(1)
        time.sleep(5)
if r.get('status') != 'ok':
    print('REFUSED, nothing stored:', str(r.get('msg'))[:200])
    sys.exit(1)
print('works. records:', r.get('data2'))
"
setv CASDOOR_URL "$url"
setv CASDOOR_KEY "$k"
setv CASDOOR_SECRET "$v"
docker compose up -d
echo "stored. Tell Claude it is set."
