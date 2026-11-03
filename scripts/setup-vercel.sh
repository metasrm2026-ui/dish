#!/usr/bin/env bash
# Set up Dish's own Vercel project from nothing, once (README "Deploy"), then deploy it and
# create the super user:
#   1. the project dish (created when missing), and dashboard/ linked to it
#   2. a Blob store (public access, as the dashboard stores encrypted data), connected to the project:
#      it sets BLOB_READ_WRITE_TOKEN. Skipped when the project already has one.
#   3. the settings it generates: DATA_KEY (32 bytes, base64: the key every stored record is encrypted
#      with), SESSION_SECRET and ROUTINE_SECRET. One the project already has is NEVER replaced (a new
#      DATA_KEY would make every stored record unreadable). The generated values are written only to
#      dashboard/.vercel/studio-secrets.env (gitignored, readable by you only), never printed.
#      The routines' fire URLs and tokens (ROUTINE_FIRE_*, STUDIO_FIRE_*, ONBOARD_FIRE_*: the publisher,
#      Studio jobs and onboarding triggers) are the owner's to add once the triggers exist; listed at the end.
#   4. deploy (dashboard/deploy.sh: tested, the commit, checked live)
#   5. the super user: POST /api/routine/bootstrap (only while no user exists), which prints the
#      set-password link for SUPER_EMAIL.
# Needs VERCEL_TOKEN, DASHBOARD_ALIAS (the dashboard's address) and SUPER_EMAIL (SUPER_NAME optional;
# VERCEL_SCOPE: the Vercel team, if any). Running it again is safe: it only adds what is missing.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
VERCEL=vercel@65.0.0              # the same pinned CLI as dashboard/deploy.sh
PROJECT=dish
STORE="$PROJECT-blob"
FIRES=(ROUTINE_FIRE STUDIO_FIRE ONBOARD_FIRE)
: "${VERCEL_TOKEN:?set VERCEL_TOKEN}"
: "${DASHBOARD_ALIAS:?set DASHBOARD_ALIAS to the address of the dashboard, e.g. $PROJECT.vercel.app}"
: "${SUPER_EMAIL:?set SUPER_EMAIL to the product owner email (the super user)}"
[[ "$SUPER_EMAIL" =~ ^[^@\"\\[:space:]]+@[^@\"\\[:space:]]+\.[^@\"\\[:space:]]+$ ]] || { echo "setup: SUPER_EMAIL is not an email address" >&2; exit 2; }
SUPER_NAME="$(printf '%s' "${SUPER_NAME:-Owner}" | tr -d '"\\')"
scope=(); [ -z "${VERCEL_SCOPE:-}" ] || scope=(--scope "$VERCEL_SCOPE")
v() { NODE_USE_ENV_PROXY=1 npx -y "$VERCEL" "$@" "${scope[@]}" --token "$VERCEL_TOKEN"; }
cd "$here/dashboard"

# 1. the project, and this folder linked to it
if [ ! -f .vercel/project.json ]; then
  v project add "$PROJECT" > /dev/null 2>&1 || echo "setup: project $PROJECT already exists (or could not be created): linking to it"
  v link --yes --project "$PROJECT"
fi
[ -f .vercel/project.json ] || { echo "setup: dashboard/ is not linked to $PROJECT" >&2; exit 1; }
linked="$(grep -o '"projectName": *"[^"]*"' .vercel/project.json | cut -d'"' -f4 || true)"
if [ -n "$linked" ] && [ "$linked" != "$PROJECT" ]; then
  echo "setup: dashboard/ is linked to the Vercel project $linked, not $PROJECT: remove dashboard/.vercel and run again" >&2
  exit 1
fi

# what the project has already (names only: values are never read back)
have="$(v env ls production 2>&1 || true)"
has() { printf '%s\n' "$have" | grep -qw -- "$1"; }

# 2. storage
if has BLOB_READ_WRITE_TOKEN; then
  echo "setup: the project already has a Blob store"
else
  v blob create-store "$STORE" --access public --yes --environment production --environment preview --environment development
fi

# 3. the generated settings
secrets_file=.vercel/studio-secrets.env
(umask 077; touch "$secrets_file")
b64() { head -c "$1" /dev/urandom | base64 | tr -d '\n'; }
hex() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
for name in DATA_KEY SESSION_SECRET ROUTINE_SECRET; do
  if has "$name"; then
    echo "setup: $name is already set in the project: kept"
    continue
  fi
  case "$name" in
    DATA_KEY) value="$(b64 32)" ;;
    SESSION_SECRET) value="$(b64 48)" ;;
    ROUTINE_SECRET) value="$(hex 32)" ;;
  esac
  printf '%s' "$value" | v env add "$name" --environment production --sensitive --yes > /dev/null
  (umask 077; { grep -v "^$name=" "$secrets_file" || true; printf '%s=%s\n' "$name" "$value"; } > "$secrets_file.new")
  mv "$secrets_file.new" "$secrets_file"
  chmod 600 "$secrets_file"
  echo "setup: $name generated and set (its value is in dashboard/$secrets_file)"
done

# 4. deploy
DASHBOARD_ALIAS="$DASHBOARD_ALIAS" bash "$here/dashboard/deploy.sh"

# 5. the super user
secret="${ROUTINE_SECRET:-$(grep '^ROUTINE_SECRET=' "$secrets_file" | cut -d= -f2- || true)}"
if [ -z "$secret" ]; then
  echo "setup: ROUTINE_SECRET was set before this run: run again with ROUTINE_SECRET set to create the super user" >&2
  exit 1
fi
body="$(printf '{"email":"%s","name":"%s"}' "$SUPER_EMAIL" "$SUPER_NAME")"
answer="$(curl -sS -X POST "https://$DASHBOARD_ALIAS/api/routine/bootstrap" -H "Authorization: Bearer $secret" \
          -H 'Content-Type: application/json' --data "$body" -w '\n%{http_code}' || true)"
code="$(printf '%s' "$answer" | tail -n1)"
link="$(printf '%s' "$answer" | grep -o '"setup_link": *"[^"]*"' | cut -d'"' -f4 || true)"
if [ "$code" = 200 ] && [ -n "$link" ]; then
  echo "setup: the super user $SUPER_EMAIL is created. Set its password here (the link works once):"
  echo "  $link"
elif [ "$code" = 409 ]; then
  echo "setup: a user already exists: no super user created (use Forgot password on the sign-in page)"
else
  echo "setup: creating the super user failed (HTTP ${code:-none}): run this script again" >&2
  exit 1
fi
missing=()
for f in "${FIRES[@]}"; do has "${f}_URL" && has "${f}_TOKEN" || missing+=("${f}_URL/${f}_TOKEN"); done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "setup: still for you to add in Vercel (Settings > Environment Variables), once the routines' triggers exist,"
  echo "  then deploy again: ${missing[*]} (README \"The five routines\")"
fi
echo "setup: put ROUTINE_SECRET (dashboard/$secrets_file) in the routines' environment secrets with DASHBOARD_URL=https://$DASHBOARD_ALIAS and APIFY_TOKEN"
