#!/usr/bin/env bash
# One-time Google Cloud setup for shorts-engine:
#   - a dedicated Google Cloud project with YouTube Data API v3 enabled
#   - an API key restricted to the YouTube Data API, written to .env (never printed)
# Runs in its own gcloud configuration, so your default gcloud account/project are left alone.
#
# usage: deploy/gcloud-setup.sh <google-account-email> [project-id]
#
# Nothing account- or project-specific is hard-coded. Arguments or environment variables:
#   GCLOUD_ACCOUNT   Google account that owns the channel (alternative to the 1st argument)
#   GCLOUD_PROJECT   existing or new project id           (alternative to the 2nd argument)
#   GCLOUD_CONFIG    gcloud configuration name            (default: shorts-engine)
#   PROJECT_PREFIX   prefix for a generated project id    (default: shorts-engine)
#   PROJECT_NAME     display name for a new project       (default: shorts-engine)
# Without a project id, the configuration's current project is used; otherwise a new
# "<PROJECT_PREFIX>-<random hex>" project is created.
set -euo pipefail

USAGE="usage: deploy/gcloud-setup.sh <google-account-email> [project-id]"
ACCOUNT="${1:-${GCLOUD_ACCOUNT:-}}"
[ -n "$ACCOUNT" ] || { echo "$USAGE" >&2; exit 2; }
CONFIG="${GCLOUD_CONFIG:-shorts-engine}"
PROJECT_PREFIX="${PROJECT_PREFIX:-shorts-engine}"
PROJECT_NAME="${PROJECT_NAME:-shorts-engine}"
KEY_LABEL="shorts-engine trends"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export CLOUDSDK_ACTIVE_CONFIG_NAME="$CONFIG"

gcloud config configurations describe "$CONFIG" >/dev/null 2>&1 \
  || gcloud config configurations create "$CONFIG" --no-activate >/dev/null
if [ -z "$(gcloud auth list --filter="account:$ACCOUNT" --format="value(account)")" ]; then
  gcloud auth login "$ACCOUNT"
fi
gcloud config set account "$ACCOUNT" >/dev/null 2>&1

PROJECT="${2:-${GCLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}}"
if [ -z "$PROJECT" ]; then
  PROJECT="${PROJECT_PREFIX}-$(python3 -c 'import secrets; print(secrets.token_hex(3))')"
fi
if ! gcloud projects describe "$PROJECT" >/dev/null 2>&1; then
  echo "Creating project $PROJECT ..."
  gcloud projects create "$PROJECT" --name="$PROJECT_NAME"
fi
gcloud config set project "$PROJECT" >/dev/null 2>&1

echo "Enabling YouTube Data API v3 ..."
gcloud services enable youtube.googleapis.com apikeys.googleapis.com

key_name() {
  gcloud services api-keys list --filter="displayName=\"$KEY_LABEL\"" --limit=1 --format="value(name)"
}
if [ -z "$(key_name)" ]; then
  echo "Creating API key restricted to YouTube Data API ..."
  gcloud services api-keys create --display-name="$KEY_LABEL" \
    --api-target=service=youtube.googleapis.com >/dev/null 2>&1
fi
KEY="$(gcloud services api-keys get-key-string "$(key_name)" --format="value(keyString)")"

[ -f "$ROOT/.env" ] || cp "$ROOT/.env.example" "$ROOT/.env"
YT_KEY="$KEY" python3 - "$ROOT/.env" <<'EOF'
import os, re, sys
path, key = sys.argv[1], os.environ["YT_KEY"]
text = open(path).read()
line = f"YOUTUBE_API_KEY={key}"
text = re.sub(r"(?m)^YOUTUBE_API_KEY=.*$", line, text) if re.search(r"(?m)^YOUTUBE_API_KEY=", text) else text + f"\n{line}\n"
open(path, "w").write(text)
EOF
chmod 600 "$ROOT/.env"
mkdir -p "$ROOT/secrets" && chmod 700 "$ROOT/secrets"

cat <<EOF

Done.
  project : $PROJECT
  account : $ACCOUNT
  API key : saved to .env as YOUTUBE_API_KEY (restricted to YouTube Data API v3)

Next, in the browser (signed in as $ACCOUNT):
  1. OAuth consent : https://console.cloud.google.com/auth/overview?project=$PROJECT
  2. Desktop client: https://console.cloud.google.com/auth/clients/create?project=$PROJECT
     -> download JSON to: $ROOT/secrets/client_secret.json
  3. Then run       : .venv/bin/shorts-engine auth
EOF
