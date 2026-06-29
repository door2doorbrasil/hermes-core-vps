#!/usr/bin/env bash
set -euo pipefail

REPO="${1:-door2doorbrasil/hermes-core-vps}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_ENV_FILE="${LOCAL_ENV_FILE:-$ROOT_DIR/../local-hermes/stacks/hermes-agent-fi18/.env}"
STATE_FILE="${STATE_FILE:-$ROOT_DIR/.last-hostinger-secrets.json}"

if ! command -v gh >/dev/null 2>&1; then
  echo "gh CLI not found on PATH" >&2
  exit 1
fi

if [[ ! -f "$LOCAL_ENV_FILE" ]]; then
  echo "Local Hermes env file not found: $LOCAL_ENV_FILE" >&2
  exit 1
fi

if [[ ! -f "$STATE_FILE" ]]; then
  echo "Hostinger state file not found: $STATE_FILE" >&2
  exit 1
fi

tmp_json="$(mktemp)"
cleanup() {
  rm -f "$tmp_json"
}
trap cleanup EXIT

python3 - <<'PY' "$LOCAL_ENV_FILE" "$STATE_FILE" >"$tmp_json"
import json
import sys
from pathlib import Path

env_file = Path(sys.argv[1])
state_file = Path(sys.argv[2])

env_data = {}
for raw_line in env_file.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    env_data[key.strip()] = value.strip()

state = json.loads(state_file.read_text(encoding="utf-8"))

def env(name: str, default: str = "") -> str:
    return env_data.get(name, default)

payload = {
    "ACME_EMAIL": env("ACME_EMAIL", "sales@polarsinergy.com"),
    "API_SERVER_KEY": env("API_SERVER_KEY", state.get("api_server_key", "")),
    "BUY_IMAP_HOST": env("BUY_IMAP_HOST"),
    "BUY_IMAP_PASSWORD": env("BUY_IMAP_PASSWORD"),
    "BUY_IMAP_PORT": env("BUY_IMAP_PORT"),
    "BUY_IMAP_SSL": env("BUY_IMAP_SSL", "true"),
    "BUY_IMAP_USERNAME": env("BUY_IMAP_USERNAME"),
    "BUY_SMTP_HOST": env("BUY_SMTP_HOST"),
    "BUY_SMTP_PASSWORD": env("BUY_SMTP_PASSWORD"),
    "BUY_SMTP_PORT": env("BUY_SMTP_PORT"),
    "BUY_SMTP_SSL": env("BUY_SMTP_SSL", "true"),
    "BUY_SMTP_USERNAME": env("BUY_SMTP_USERNAME"),
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD": state.get("dashboard_password", ""),
    "HERMES_DASHBOARD_BASIC_AUTH_SECRET": state.get("api_server_key", ""),
    "HERMES_DASHBOARD_BASIC_AUTH_USERNAME": state.get("dashboard_username", "admin"),
    "LITELLM_MASTER_KEY": env("LITELLM_MASTER_KEY", state.get("api_server_key", "")),
    "OPENAI_API_KEY": env("OPENAI_API_KEY"),
    "SALES_IMAP_HOST": env("SALES_IMAP_HOST"),
    "SALES_IMAP_PASSWORD": env("SALES_IMAP_PASSWORD"),
    "SALES_IMAP_PORT": env("SALES_IMAP_PORT"),
    "SALES_IMAP_SSL": env("SALES_IMAP_SSL", "true"),
    "SALES_IMAP_USERNAME": env("SALES_IMAP_USERNAME"),
    "SALES_SMTP_HOST": env("SALES_SMTP_HOST"),
    "SALES_SMTP_PASSWORD": env("SALES_SMTP_PASSWORD"),
    "SALES_SMTP_PORT": env("SALES_SMTP_PORT"),
    "SALES_SMTP_SSL": env("SALES_SMTP_SSL", "true"),
    "SALES_SMTP_USERNAME": env("SALES_SMTP_USERNAME"),
    "HERMES_WHATSAPP_ALLOWED_USERS": env("WHATSAPP_ALLOWED_USERS"),
}

required = [
    "API_SERVER_KEY",
    "BUY_IMAP_HOST",
    "BUY_IMAP_PASSWORD",
    "BUY_IMAP_PORT",
    "BUY_IMAP_USERNAME",
    "BUY_SMTP_HOST",
    "BUY_SMTP_PASSWORD",
    "BUY_SMTP_PORT",
    "BUY_SMTP_USERNAME",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD",
    "HERMES_DASHBOARD_BASIC_AUTH_SECRET",
    "HERMES_DASHBOARD_BASIC_AUTH_USERNAME",
    "LITELLM_MASTER_KEY",
    "OPENAI_API_KEY",
]
missing = [key for key in required if not payload.get(key)]
if missing:
    raise SystemExit("Missing required secret sources: " + ", ".join(missing))

print(json.dumps(payload, ensure_ascii=True))
PY

while IFS= read -r line; do
  key="${line%%=*}"
  value="${line#*=}"
  if [[ -z "$value" ]]; then
    continue
  fi
  printf '%s' "$value" | gh secret set "$key" -R "$REPO"
  echo "synced $key"
done < <(
  python3 - <<'PY' "$tmp_json"
import json
import sys
data = json.loads(open(sys.argv[1], encoding="utf-8").read())
for key in sorted(data):
    value = str(data[key] or "")
    print(f"{key}={value}")
PY
)
