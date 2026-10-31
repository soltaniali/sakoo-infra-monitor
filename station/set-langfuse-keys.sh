#!/bin/sh
# Writes the AIP Langfuse project API keys into secrets/ops.env (secret key typed hidden), then restarts the station.
# Create the keys in Langfuse: project Settings > API Keys > Create new API keys (public pk-lf-..., secret sk-lf-...).
# The station only reads with them (GET /api/public/...). LiteLLM sends every call there (callbacks: langfuse).
# Run: ssh -t AIP-IRAN sh station/set-langfuse-keys.sh
set -eu
cd "$(dirname "$0")"
umask 077
touch secrets/ops.env

setv() {  # replace one NAME=value line
  grep -v "^$1=" secrets/ops.env > secrets/ops.env.new || true
  printf '%s=%s\n' "$1" "$2" >> secrets/ops.env.new
  mv secrets/ops.env.new secrets/ops.env
}

url=https://langfusepilot.aipsharif.ir
printf 'Langfuse URL [%s]: ' "$url" >&2; read -r u
setv LANGFUSE_URL "${u:-$url}"
printf 'Public key (pk-lf-...): ' >&2; read -r pk
[ -z "$pk" ] || setv LANGFUSE_PK "$pk"
stty -echo; printf 'Secret key (sk-lf-..., hidden, Enter = keep current): ' >&2; read -r sk; stty echo; echo >&2
[ -z "$sk" ] || setv LANGFUSE_SK "$sk"
docker compose up -d
echo "testing (GET /api/public/projects)..."
sleep 5
docker exec sakoo-probe-station python3 -c '
import base64, json, os, ssl, urllib.request
req = urllib.request.Request(os.environ["LANGFUSE_URL"].rstrip("/") + "/api/public/projects", headers={
    "Authorization": "Basic " + base64.b64encode((os.environ["LANGFUSE_PK"] + ":" + os.environ["LANGFUSE_SK"]).encode()).decode()})
for p in json.loads(urllib.request.urlopen(req, timeout=30, context=ssl._create_unverified_context()).read())["data"]:
    print("OK project:", p["name"])'
