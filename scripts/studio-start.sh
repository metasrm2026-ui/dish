#!/usr/bin/env bash
# Start a Studio session in one step (STUDIO.md 0): packages (only if missing or not working), the pages' settings
# and reference documents, then claim the waiting jobs. An editor is waiting: every step saved counts.
set -euo pipefail
cd "$(dirname "$0")/.."
# a package or the card browser that cannot be repaired here does not stop the session: `studio jobs`
# takes only the jobs this machine can do (an IDEAS job needs no image packages, only COMPOSE needs Chromium).
# The browser download normally takes seconds: one that stalls is given 90 s, not the 10 minutes an
# IDEAS or CHECK job (which never draws) would otherwise wait for it.
BROWSER_INSTALL_TIMEOUT=90 bash scripts/ensure-deps.sh || echo "packages: not all work here; taking only the jobs this machine can do"
python -m bore.cli studio sync
python -m bore.cli studio jobs
