#!/usr/bin/env bash
# The routines' Python packages, installed only when missing or not working: a session whose
# environment setup script already installed them (see README) skips this entirely. Uses
# `python -m pip`, never bare pip (in the cloud containers bare pip can belong to another Python).
#
#   bash scripts/ensure-deps.sh            everything (hourly posts, topic feed, Studio)
#   bash scripts/ensure-deps.sh publish    only what the SocialPilot publisher needs (no image packages)
#   BROWSER_INSTALL_TIMEOUT=90 ...         give a browser download that stalls this many seconds (default 600):
#                                          scripts/studio-start.sh, where an editor waits, passes a short one
#
# "Installed" is not enough: the cloud machines carry a system Pillow built for another Python,
# which pip counts as installed but which does not load (bore/deps.py). Packages like that are
# installed again next to it with --ignore-installed; ours comes first on the path. Loading is not
# enough either: the machine tests save an image, read a tiny card with OCR and start Chromium,
# the browser every card is drawn in (not a pip package: playwright installs it).
# Versions come from constraints.txt (the ones the tests passed with), never just the newest.
set -euo pipefail
cd "$(dirname "$0")/.."
case "${1:-}" in
  "") only="" ;;
  publish) only="requests,PyYAML" ;;
  *) echo "usage: bash scripts/ensure-deps.sh [publish]" >&2; exit 2 ;;
esac
deps() { python -m bore.deps ${only:+--only "$only"} "$@"; }
# only real requirement lines go to pip (an import that fails may print anything)
# (a crash of the check itself stops the script: it must never read as "already installed")
broken() { local out; out=$(deps --selftest) || return 1; printf '%s\n' "$out" | grep -Fxf requirements.txt || true; }
# pip without -q, keeping only what it installed and why it refused: the run log shows what a fresh machine lacked
install() { local s=$SECONDS; python -m pip install --disable-pip-version-check --prefer-binary -c constraints.txt "$@" 2>&1 \
              | grep -E '^(Successfully installed|ERROR|error:)' || true; echo "packages: pip took $((SECONDS - s))s"; }
names() { printf '%s\n' "$1" | sed 's/[<>=!~; ].*//' | sort -u | paste -sd ' '; }
# versions other than the tested ones (installed without constraints.txt, e.g. by a setup script run outside
# the repository folder): they work, the checks passed, so they stay; said here so the drift shows in the run log
drift() { { deps --drift 2>/dev/null || true; } | sed 's/^/packages: not the tested version: /'; }
py=$(python -c 'import sys; print(sys.version.split()[0])')
t=$SECONDS
if deps --selftest --why > /dev/null 2>&1; then
  echo "packages: already installed and working (python $py, $((SECONDS - t))s)"
  drift
  exit 0
fi
bad=$(broken)
if [ -n "$bad" ]; then
  req=$(mktemp)
  trap 'rm -f "$req"' EXIT
  deps --requirements > "$req"            # what the routines use (requirements.txt also has pytest, for development)
  echo "packages: not all work: $(names "$bad"); installing what is missing"
  install -r "$req"                       # the check below decides
  bad=$(broken)
  prev="" user=""
  for _ in 1 2 3 4 5 6; do
    [ -n "$bad" ] || break
    where=""
    if [ "$bad" = "$prev" ]; then         # installed again and still broken: a copy ahead of ours on the path
      [ -z "$user" ] || break             # (the user site comes first)
      where="--user" user=1
    fi
    echo "packages: installed but not working, installing again${where:+ ($where)}: $(names "$bad")"
    printf '%s\n' "$bad" > "$req"
    install --ignore-installed $where -r "$req"
    prev=$bad
    bad=$(broken)                         # packages that only failed because of this one load now
  done
  if [ -n "$bad" ]; then
    echo "packages: still not working after installing them again (python $py):"
    deps --selftest --why || true
    exit 1
  fi
fi
# the browser: it lives in PLAYWRIGHT_BROWSERS_PATH, outside pip's view (cards render in the headless shell)
if [ -z "$only" ] && ! python -m bore.deps --only chromium --why > /dev/null 2>&1; then
  echo "packages: the card browser (Chromium) does not start; installing it"
  timeout "${BROWSER_INSTALL_TIMEOUT:-600}" python -m playwright install --only-shell chromium 2>&1 \
    | grep -E 'downloaded to|[Ee]rror' || true
  if ! python -m bore.deps --only chromium --why; then
    echo "packages: the card browser still does not start (python $py): cards cannot be drawn on this machine"
    exit 1
  fi
fi
drift
echo "packages: ready in $((SECONDS - t))s (python $py)"
