"""dashboard/deploy.sh, run against a scratch repository with npm, npx (the Vercel CLI) and curl
faked: only a commit is deployed, the tests run first, the CLI version is pinned, version.json
carries the commit, and the deploy fails unless the public address then serves that commit."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "dashboard" / "deploy.sh"

FAKES = {
    "npm": 'echo "npm $*" >> "$FAKE_LOG"; [ "$1" != test ] || exit "${NPM_FAIL:-0}"',
    "npx": '\n'.join([
        'echo "npx $*" >> "$FAKE_LOG"',
        'case "$*" in *" deploy "*)',
        '  cp public/version.json "$FAKE_OUT/version.json"; ls > "$FAKE_OUT/files"',
        '  [ -z "${DEPLOY_MOVES_ALIAS:-}" ] || touch "$FAKE_OUT/moved"',                           # a CLI that moves the address itself
        '  if [ -n "${NO_URL:-}" ]; then echo "Error: something new"',
        '  elif [ -n "${BARE_URL:-}" ]; then printf "https://bore-dashboard-xyz9.vercel.app"',     # run by hand, piped: no JSON
        '  else echo \'{"deployment": {"url": "https://bore-dashboard-abc.vercel.app"}}\'; fi ;;',
        '*" alias ls "*) [ -z "${NO_ALIASES:-}" ] || exit 1',                                      # where the aliases point now
        '  now=bore-dashboard-old1.vercel.app; [ ! -e "$FAKE_OUT/moved" ] || now=bore-dashboard-abc.vercel.app',
        '  printf "%s\\n" "  source  url  age" "  bore-dashboard-x.vercel.app  other.test  9d" "  $now  bore.test  2d" ;;',
        'esac']),
    "curl": 'echo "curl $*" >> "$FAKE_LOG"; cat "$FAKE_LIVE"',
    "sleep": ':',
}


@pytest.fixture
def repo(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git not available")
    root = tmp_path / "repo"
    d = root / "dashboard"
    for sub in ("api", "lib", "routes", "public", ".vercel", "node_modules", "test"):
        (d / sub).mkdir(parents=True)
    shutil.copy(DEPLOY, d / "deploy.sh")
    for f, text in (("api/main.js", "//"), ("lib/db.js", "//"), ("routes/x.js", "//"), ("public/index.html", "<html>"),
                    ("package.json", "{}"), ("package-lock.json", "{}"), ("vercel.json", "{}"), (".vercel/project.json", "{}"),
                    ("test/a.test.mjs", "//")):
        (d / f).write_text(text)
    (root / ".gitignore").write_text("dashboard/node_modules/\ndashboard/.vercel/\n")
    git = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name, body in FAKES.items():
        (bin_ / name).write_text(f"#!/usr/bin/env bash\n{body}\n")
        (bin_ / name).chmod(0o755)
    out = tmp_path / "out"
    out.mkdir()
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "VERCEL_TOKEN": "tok", "DASHBOARD_ALIAS": "bore.test",
           "FAKE_LOG": str(tmp_path / "log"), "FAKE_OUT": str(out), "FAKE_LIVE": str(tmp_path / "live")}
    (tmp_path / "log").write_text("")
    return {"root": root, "dash": d, "env": env, "out": out, "log": tmp_path / "log", "live": tmp_path / "live",
            "commit": git("rev-parse", "HEAD")}


def _deploy(r, **env):
    return subprocess.run(["bash", str(r["dash"] / "deploy.sh")], env={**r["env"], **env}, capture_output=True, text=True,
                          timeout=60)


def test_a_clean_commit_is_tested_deployed_with_its_version_and_checked_live(repo):
    repo["live"].write_text(json.dumps({"commit": repo["commit"], "api": 3}, separators=(",", ":")))
    p = _deploy(repo)
    assert p.returncode == 0, p.stderr
    assert f"serves {repo['commit']}" in p.stdout
    log = repo["log"].read_text().splitlines()
    assert log[0] == "npm test", "the tests run before anything is deployed"
    assert any(x.startswith("npx -y vercel@65.0.0 deploy --prod") for x in log), "a pinned Vercel CLI"
    assert any(x == "npx -y vercel@65.0.0 alias set https://bore-dashboard-abc.vercel.app bore.test --token tok" for x in log)
    assert any(x.startswith("curl -fsS https://bore.test/version.json") for x in log)
    assert json.loads((repo["out"] / "version.json").read_text()) == {"commit": repo["commit"], "api": 3}
    assert "test" not in (repo["out"] / "files").read_text().split(), "only what the site needs is deployed"

    # run by hand in a terminal (no agent): the CLI prints the bare deployment URL, no JSON
    p = _deploy(repo, BARE_URL="1")
    assert p.returncode == 0, p.stderr
    assert "npx -y vercel@65.0.0 alias set https://bore-dashboard-xyz9.vercel.app bore.test --token tok" in repo["log"].read_text()


def test_uncommitted_changes_failing_tests_a_missed_alias_and_an_old_live_copy_all_fail(repo):
    repo["live"].write_text(json.dumps({"commit": "0" * 40, "api": 1}))
    p = _deploy(repo)
    assert p.returncode == 1 and "does not serve" in p.stderr, "the address still serves the old deployment"

    p = _deploy(repo, NO_URL="1")
    assert p.returncode == 1 and "was NOT moved" in p.stderr
    assert not any("alias set" in x for x in repo["log"].read_text().splitlines()[-3:])

    repo["log"].write_text("")
    p = _deploy(repo, NPM_FAIL="1")
    assert p.returncode != 0 and "npx" not in repo["log"].read_text(), "failing tests stop the deploy"

    (repo["dash"] / "routes" / "x.js").write_text("// edited")
    repo["log"].write_text("")
    p = _deploy(repo)
    assert p.returncode == 1 and "uncommitted changes" in p.stderr and repo["log"].read_text() == ""


def test_the_deployment_to_roll_back_to_is_printed_before_the_address_moves(repo):
    """A rollback points the address back at the deployment it served before (README, "Rolling back"):
    deploy.sh says which one that was before moving it. A listing that fails does not stop the deploy."""
    repo["live"].write_text(json.dumps({"commit": repo["commit"], "api": 3}, separators=(",", ":")))
    p = _deploy(repo)
    assert p.returncode == 0, p.stderr
    assert "deploy: bore.test pointed at https://bore-dashboard-old1.vercel.app before this deploy" in p.stdout
    assert "npx -y vercel@65.0.0 alias set https://bore-dashboard-old1.vercel.app bore.test --token $VERCEL_TOKEN" in p.stdout
    log = repo["log"].read_text().splitlines()
    deployed = next(i for i, x in enumerate(log) if " deploy " in x)
    assert log.index("npx -y vercel@65.0.0 alias ls --token tok") < deployed < next(i for i, x in enumerate(log) if " alias set " in x)
    # listed before the deploy: were a deploy ever to move the address itself, it still names the
    # old deployment, not the new one (a rollback to that would change nothing)
    p = _deploy(repo, DEPLOY_MOVES_ALIAS="1")
    assert p.returncode == 0 and (repo["out"] / "moved").exists(), p.stderr
    assert "deploy: bore.test pointed at https://bore-dashboard-old1.vercel.app before this deploy" in p.stdout
    p = _deploy(repo, NO_ALIASES="1")
    assert p.returncode == 0 and "could not tell which deployment bore.test points at now" in p.stderr
