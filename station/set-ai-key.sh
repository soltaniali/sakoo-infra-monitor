#!/bin/sh
# Writes a free-tier LLM API key into secrets/ops.env (typed hidden), then restarts the station and tests one call.
# Used for AI summaries of logs, incidents and support tickets. Run: ssh -t AIP-IRAN sh station/set-ai-key.sh
# Providers the Iran station can reach (2026-10-31): Mistral, OpenRouter, SambaNova, Z AI. Gemini, Groq, Cerebras, Cohere are blocked.
set -eu
cd "$(dirname "$0")"
umask 077
touch secrets/ops.env

setv() {  # replace one NAME=value line
  grep -v "^$1=" secrets/ops.env > secrets/ops.env.new || true
  printf '%s=%s\n' "$1" "$2" >> secrets/ops.env.new
  mv secrets/ops.env.new secrets/ops.env
}

echo "1) Mistral     key: https://console.mistral.ai/api-keys   (default)"
echo "2) OpenRouter  key: https://openrouter.ai/settings/keys    (free models end in :free)"
echo "3) SambaNova   key: https://cloud.sambanova.ai/apis"
printf 'Provider [1]: ' >&2; read -r p
case "${p:-1}" in
  2) url=https://openrouter.ai/api/v1; model=deepseek/deepseek-chat-v3.1:free ;;
  3) url=https://api.sambanova.ai/v1; model=Meta-Llama-3.3-70B-Instruct ;;
  *) url=https://api.mistral.ai/v1; model=ministral-14b-latest,ministral-8b-latest,mistral-small-latest ;;   # small: often rate-limited on free tier
esac
printf 'Model(s), comma = fallback order [%s]: ' "$model" >&2; read -r m
setv AI_URL "$url"
setv AI_MODEL "${m:-$model}"
stty -echo; printf 'API key (hidden, Enter = keep current): ' >&2; read -r v; stty echo; echo >&2
[ -z "$v" ] || setv AI_KEY "$v"
docker compose up -d
echo "waiting for the VPN, then testing..."
for i in $(seq 30); do docker exec sakoo-probe-station test -e /sys/class/net/tun0 2>/dev/null && break; sleep 2; done
sleep 8
docker exec sakoo-probe-station python3 /app/ops_web.py check
