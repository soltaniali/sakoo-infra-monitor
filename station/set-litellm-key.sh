#!/bin/sh
# Stores a READ-ONLY LiteLLM key for the ops dashboard in secrets/ops.env (typed hidden).
# The key must belong to a user with the role proxy_admin_viewer ("Admin Viewer" in the LiteLLM UI):
# anything else, a master key included, is refused and not stored. It does not turn on the model test calls.
# Run: ssh -t AIP-IRAN sh station/set-litellm-key.sh
set -eu
cd "$(dirname "$0")"
umask 077
touch secrets/ops.env

setv() {  # replace one NAME=value line
  grep -v "^$1=" secrets/ops.env > secrets/ops.env.new || true
  printf '%s=%s\n' "$1" "$2" >> secrets/ops.env.new
  mv secrets/ops.env.new secrets/ops.env
}

url=https://rmgpilot.aipsharif.ir
stty -echo; printf 'Read-only LiteLLM key (hidden): ' >&2; read -r v; stty echo; echo >&2
[ -n "$v" ] || { echo "no key typed, nothing changed"; exit 1; }

# GET only. The key goes through stdin, so it is not in any process list.
printf '%s' "$v" | docker exec -i sakoo-probe-station python3 -c "
import json, ssl, sys, time, urllib.error, urllib.parse, urllib.request
base, key = '$url', sys.stdin.read().strip()
cx = ssl._create_unverified_context()
def get(path):
    req = urllib.request.Request(base + path, headers={'Authorization': 'Bearer ' + key})
    for i in range(4):  # the VPN path drops a connection now and then; a refused key (HTTP error) is final
        try:
            return json.loads(urllib.request.urlopen(req, timeout=30, context=cx).read())
        except urllib.error.HTTPError:
            raise
        except (OSError, TimeoutError) as e:
            print('  %s: try %d failed (%s)' % (path.split('?')[0], i + 1, e))
            if i == 3:
                raise
            time.sleep(5)
step = '/key/info'
try:
    who = get('/key/info')['info'].get('user_id')
    step = '/user/info'
    role = get('/user/info?user_id=' + urllib.parse.quote(str(who))).get('user_info', {}).get('user_role')
except Exception as e:
    print('REFUSED at %s, nothing stored:' % step, e)
    sys.exit(1)
print('key user role:', role)
if role != 'proxy_admin_viewer':
    print('REFUSED: the role must be proxy_admin_viewer. Nothing stored. Create a key for an Admin Viewer user.')
    sys.exit(1)
for path in ('/key/list?size=1&return_full_object=true', '/user/list?page=1&page_size=1', '/team/list', '/v2/model/info', '/user/daily/activity?start_date=2026-10-01&end_date=2026-10-02', '/spend/logs?start_date=2026-07-01&end_date=2026-07-02'):
    try:
        get(path)
        print('can read', path.split('?')[0])
    except urllib.error.HTTPError as e:
        print('HTTP', e.code, path.split('?')[0])
    except Exception as e:
        print('failed', path.split('?')[0], type(e).__name__)
"
setv LITELLM_READ_URL "$url"
setv LITELLM_READ_KEY "$v"
docker compose up -d
echo "stored. The key can only read; tell Claude it is set."
