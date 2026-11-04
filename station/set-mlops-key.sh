#!/bin/sh
# Stores a mlops-core API key for the ops dashboard in secrets/ops.env (secret typed hidden), after testing it.
# The API is ClearML-style: its read calls are POSTs, so "read only" here means only login and list calls, never a change.
# Run: ssh -t AIP-IRAN sh station/set-mlops-key.sh
set -eu
cd "$(dirname "$0")"
umask 077
touch secrets/ops.env

setv() {  # replace one NAME=value line
  grep -v "^$1=" secrets/ops.env > secrets/ops.env.new || true
  printf '%s=%s\n' "$1" "$2" >> secrets/ops.env.new
  mv secrets/ops.env.new secrets/ops.env
}

url=https://mlopsapipilot.aipsharif.ir/core
printf 'Access key: ' >&2; read -r k
stty -echo; printf 'Secret key (hidden): ' >&2; read -r v; stty echo; echo >&2
[ -n "$k" ] && [ -n "$v" ] || { echo "key or secret empty, nothing changed"; exit 1; }

# The secret goes through stdin, so it is not in any process list. Prints counts and field names, no values.
printf '%s\n%s' "$k" "$v" | docker exec -i sakoo-probe-station python3 -c "
import base64, collections, json, ssl, sys, time, urllib.error, urllib.request
k, v = sys.stdin.read().split('\n', 1)
cx = ssl._create_unverified_context()
def call(name, body, auth):
    req = urllib.request.Request('$url/' + name, json.dumps(body).encode(), {'Content-Type': 'application/json', 'Authorization': auth})
    for i in range(4):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=30, context=cx).read()).get('data') or {}
        except urllib.error.HTTPError as e:
            print('REFUSED at', name, 'HTTP', e.code, e.read()[:200].decode('utf-8', 'replace'))
            sys.exit(1)
        except OSError as e:
            print('  try', i + 1, 'failed:', type(e).__name__)
            time.sleep(5)
    print('REFUSED: cannot reach mlops-core, nothing stored')
    sys.exit(1)
token = call('auth.login', {}, 'Basic ' + base64.b64encode((k.strip() + ':' + v.strip()).encode()).decode()).get('token')
if not token:
    print('REFUSED: no token from auth.login, nothing stored')
    sys.exit(1)
bearer = 'Bearer ' + token
me = call('users.get_current_user', {}, bearer).get('user') or {}
print('works. role:', me.get('role'), '| company users see:', bool(me.get('company')))
t = call('tasks.get_all', {'page': 0, 'page_size': 500, 'order_by': ['-last_update'], 'only_fields': ['id', 'user', 'status', 'type', 'last_update', 'project', 'queue']}, bearer).get('tasks') or []
print('tasks in newest 500:', len(t), '| distinct users:', len({x.get('user') for x in t}), '| statuses:', dict(collections.Counter(x.get('status') for x in t)))
print('types:', dict(collections.Counter(x.get('type') for x in t)))
"
setv MLOPS_URL "$url"
setv MLOPS_KEY "$k"
setv MLOPS_SECRET "$v"
docker compose up -d
echo "stored. Tell Claude it is set."
