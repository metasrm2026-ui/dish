#!/usr/bin/env bash
# Deploy Bore Studio's dashboard to its own Vercel project (bore-studio), and check that its public
# address (DASHBOARD_ALIAS, e.g. bore-studio.vercel.app or the product's own domain) then serves it.
# The routines run the Python of the latest push; the dashboard runs whatever was deployed last, so
# a deploy must be a known commit and be checked:
#   1. refuses uncommitted changes in dashboard/ (what is deployed is a commit, nothing else)
#   2. runs the dashboard tests
#   3. deploys a copy of that commit (git archive: outside git, so Vercel does not block the deployment
#      on the commit author), with public/version.json {commit, api} in it
#   4. prints the deployment the dashboard's address points at before the deploy (the one to go back
#      to: README, "Rolling back"), then points it at the new one, with a pinned Vercel CLI
#   5. fails unless that address now serves version.json with this commit
# Needs VERCEL_TOKEN and DASHBOARD_ALIAS (VERCEL_SCOPE: the Vercel team, if any). The folder is linked
# to the bore-studio project (and refused when linked to any other). Deploy before pushing Python that
# needs the new API. First time: scripts/setup-vercel.sh creates the project, its storage and settings.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
API_LEVEL=4                       # raise it when the Python starts depending on a new endpoint (4: onboarding)
VERCEL=vercel@65.0.0              # pinned, bumped on purpose: a newer CLI once stopped moving the alias
PROJECT=dish                      # this product's own Vercel project, never another deployment's
host="${DASHBOARD_ALIAS:?set DASHBOARD_ALIAS to the address of the dashboard, e.g. $PROJECT.vercel.app}"
scope=(); [ -z "${VERCEL_SCOPE:-}" ] || scope=(--scope "$VERCEL_SCOPE")
cd "$here"
if [ -n "$(git status --porcelain -- .)" ]; then
  echo "deploy: dashboard/ has uncommitted changes; commit them first (only a commit is deployed):" >&2
  git status --short -- . >&2
  exit 1
fi
: "${VERCEL_TOKEN:?set VERCEL_TOKEN}"
if [ ! -f .vercel/project.json ]; then
  NODE_USE_ENV_PROXY=1 npx -y "$VERCEL" link --yes --project "$PROJECT" "${scope[@]}" --token "$VERCEL_TOKEN"
  [ -f .vercel/project.json ] || { echo "deploy: could not link dashboard/ to the Vercel project $PROJECT" >&2; exit 1; }
fi
linked="$(grep -o '"projectName": *"[^"]*"' .vercel/project.json | cut -d'"' -f4 || true)"
if [ -n "$linked" ] && [ "$linked" != "$PROJECT" ]; then
  echo "deploy: dashboard/ is linked to the Vercel project $linked, not $PROJECT: remove dashboard/.vercel and run again" >&2
  exit 1
fi
commit="$(git rev-parse HEAD)"
[ -d node_modules ] || npm ci
npm test
tmp="$(mktemp -d)"
git -C "$(git rev-parse --show-toplevel)" archive --format=tar HEAD:dashboard \
  api lib routes public package.json package-lock.json vercel.json | tar -x -C "$tmp"
# the SocialPilot deny list goes with it: the dashboard refuses those accounts itself (lib/deny.js)
mkdir -p "$tmp/config"
git -C "$(git rev-parse --show-toplevel)" show HEAD:config/socialpilot.yaml > "$tmp/config/socialpilot.yaml" 2>/dev/null \
  || echo "deploy: config/socialpilot.yaml is missing: the dashboard refuses only the accounts in SOCIALPILOT_BLOCKED_IDS" >&2
cp -r .vercel "$tmp"/
printf '{"commit":"%s","api":%s}\n' "$commit" "$API_LEVEL" > "$tmp/public/version.json"
cd "$tmp"
# where the address points before this deploy: a rollback points it back there (Vercel's own rollback
# moves only the project's domains, not this alias). Listed before deploying, so it is the old
# deployment even if a deploy ever moves the address itself. Both streams are read (the CLI prints
# most of its output on stderr); a listing that fails is said, never a stop.
prev="$(NODE_USE_ENV_PROXY=1 npx -y "$VERCEL" alias ls "${scope[@]}" --token "$VERCEL_TOKEN" 2>&1 | awk -v h="$host" '
  { hit = 0; src = ""                                  # the line "<deployment> <alias> <age>" whose alias is this address
    for (i = 1; i <= NF; i++) { f = $i; sub(/^https?:\/\//, "", f)
      if (f == h) hit = 1; else if (src == "" && f ~ /\.vercel\.app$/) src = f }
    if (hit && src != "") { print src; exit } }' || true)"
if [ -n "$prev" ]; then
  echo "deploy: $host pointed at https://$prev before this deploy. To roll back:"
  echo "  npx -y $VERCEL alias set https://$prev $host --token \$VERCEL_TOKEN"
else
  echo "deploy: could not tell which deployment $host points at now: note it in Vercel (Deployments) before rolling back" >&2
fi
NODE_USE_ENV_PROXY=1 npx -y "$VERCEL" deploy --prod --yes "${scope[@]}" --token "$VERCEL_TOKEN" | tee deploy.out
# the CLI aliases only the team domain: point the dashboard's address at this deployment too. Its
# URL is in the JSON the CLI prints for an agent, or the bare https://... it prints when piped
url="$(grep -o '"url": "https://[^"]*"' deploy.out | head -1 | cut -d'"' -f4 || true)"
[ -n "$url" ] || url="$(grep -oE 'https://[A-Za-z0-9.-]+\.vercel\.app' deploy.out | head -1 || true)"
if [ -z "$url" ]; then
  echo "deploy: no deployment URL in the Vercel CLI's output: $host was NOT moved to the new deployment" >&2
  exit 1
fi
NODE_USE_ENV_PROXY=1 npx -y "$VERCEL" alias set "$url" "$host" "${scope[@]}" --token "$VERCEL_TOKEN"
live=""
for _ in 1 2 3 4 5 6; do
  live="$(curl -fsS "https://$host/version.json?t=$(date +%s)" 2>/dev/null || true)"
  if printf '%s' "$live" | grep -q "\"commit\":\"$commit\""; then
    echo "deploy: https://$host serves $commit (API level $API_LEVEL)"
    exit 0
  fi
  sleep 5
done
echo "deploy: https://$host does not serve $commit (it answered: ${live:-nothing}): the deploy or the alias did not take" >&2
exit 1
