#!/usr/bin/env bash
# Write .env for the CLI from the cloud environment's variables, never showing a value (the routines'
# setup step 2). The product's variables are METOMATE_DASHBOARD_URL and METOMATE_ROUTINE_SECRET;
# the earlier names DISH_DASHBOARD_URL / DISH_ROUTINE_SECRET are still read while an environment
# has them. APIFY_TOKEN is added when it is set. .env is gitignored: never commit it.
set -euo pipefail
cd "$(dirname "$0")/.."
url="${METOMATE_DASHBOARD_URL:-${DISH_DASHBOARD_URL:-}}"
secret="${METOMATE_ROUTINE_SECRET:-${DISH_ROUTINE_SECRET:-}}"
missing=()
[ -n "$url" ] || missing+=(METOMATE_DASHBOARD_URL)
[ -n "$secret" ] || missing+=(METOMATE_ROUTINE_SECRET)
if [ ${#missing[@]} -gt 0 ]; then
  echo "routine-env: the environment's variables are missing: ${missing[*]} (add them in the cloud environment's settings)" >&2
  exit 1
fi
umask 077
printf 'DASHBOARD_URL=%s\nROUTINE_SECRET=%s\n' "$url" "$secret" > .env
[ -z "${APIFY_TOKEN:-}" ] || printf 'APIFY_TOKEN=%s\n' "$APIFY_TOKEN" >> .env
echo "routine-env: .env written (dashboard address and routine secret$([ -z "${APIFY_TOKEN:-}" ] || echo ', Apify token'))"
