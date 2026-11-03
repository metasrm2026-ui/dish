"""dashboard/deploy.sh and scripts/setup-vercel.sh, run against a scratch repository with npm, npx (the
Vercel CLI) and curl faked (nothing reaches Vercel): only a commit is deployed, to the product's own
project, the tests run first, the CLI version is pinned, version.json carries the commit, and the
deploy fails unless the public address then serves that commit. The setup creates what is missing
(never a key the project already has) and prints the super user's set-password link."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "dashboard" / "deploy.sh"
SETUP = ROOT / "scripts" / "setup-vercel.sh"

FAKES = {
    "npm": 'echo "npm $*" >> "$FAKE_LOG"; [ "$1" != test ] || exit "${NPM_FAIL:-0}"',
    "npx": '\n'.join([
        'echo "npx $*" >> "$FAKE_LOG"',
        'case "$*" in *" deploy "*)',
        '  cp public/version.json "$FAKE_OUT/version.json"; ls > "$FAKE_OUT/files"',
        '  [ -z "${DEPLOY_MOVES_ALIAS:-}" ] || touch "$FAKE_OUT/moved"',                           # a CLI that moves the address itself
        '  if [ -n "${NO_URL:-}" ]; then echo "Error: something new"',
        '  elif [ -n "${BARE_URL:-}" ]; then printf "https://bore-studio-xyz9.vercel.app"',        # run by hand, piped: no JSON
        '  else echo \'{"deployment": {"url": "https://bore-studio-abc.vercel.app"}}\'; fi ;;',
        '*" alias ls "*) [ -z "${NO_ALIASES:-}" ] || exit 1',                                      # where the aliases point now
        '  now=bore-studio-old1.vercel.app; [ ! -e "$FAKE_OUT/moved" ] || now=bore-studio-abc.vercel.app',
        '  printf "%s\\n" "  source  url  age" "  bore-studio-x.vercel.app  other.test  9d" "  $now  bore.test  2d" ;;',
        '*" link "*) mkdir -p .vercel; echo \'{"projectId": "prj_1", "projectName": "bore-studio"}\' > .vercel/project.json ;;',
        '*" env ls "*) cat "$FAKE_ENV" 2>/dev/null ;;',                                           # the names the project has
        '*" env add "*) v="$(cat)"; set -- $*; echo "$5" >> "$FAKE_ENV"; echo "$5=$v" >> "$FAKE_OUT/env" ;;',
        '*" blob create-store "*) echo BLOB_READ_WRITE_TOKEN >> "$FAKE_ENV" ;;',
        'esac']),
    "curl": '\n'.join([
        'echo "curl $*" >> "$FAKE_LOG"',
        'case "$*" in *bootstrap*) printf \'{"setup_link": "https://bore.test/reset#u1.tok"}\\n%s\' "${BOOT_CODE:-200}" ;;',
        '*) cat "$FAKE_LIVE" ;; esac']),
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
    (root / "scripts").mkdir()
    shutil.copy(SETUP, root / "scripts" / "setup-vercel.sh")
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
           "FAKE_LOG": str(tmp_path / "log"), "FAKE_OUT": str(out), "FAKE_LIVE": str(tmp_path / "live"),
           "FAKE_ENV": str(tmp_path / "envnames")}
    (tmp_path / "log").write_text("")
    return {"root": root, "dash": d, "env": env, "out": out, "log": tmp_path / "log", "live": tmp_path / "live",
            "commit": git("rev-parse", "HEAD")}


def _deploy(r, **env):
    return subprocess.run(["bash", str(r["dash"] / "deploy.sh")], env={**r["env"], **env}, capture_output=True, text=True,
                          timeout=60)


def test_a_clean_commit_is_tested_deployed_with_its_version_and_checked_live(repo):
    repo["live"].write_text(json.dumps({"commit": repo["commit"], "api": 4}, separators=(",", ":")))
    p = _deploy(repo)
    assert p.returncode == 0, p.stderr
    assert f"serves {repo['commit']}" in p.stdout
    log = repo["log"].read_text().splitlines()
    assert log[0] == "npm test", "the tests run before anything is deployed"
    assert any(x.startswith("npx -y vercel@65.0.0 deploy --prod") for x in log), "a pinned Vercel CLI"
    assert any(x == "npx -y vercel@65.0.0 alias set https://bore-studio-abc.vercel.app bore.test --token tok" for x in log)
    assert any(x.startswith("curl -fsS https://bore.test/version.json") for x in log)
    assert json.loads((repo["out"] / "version.json").read_text()) == {"commit": repo["commit"], "api": 4}
    assert "test" not in (repo["out"] / "files").read_text().split(), "only what the site needs is deployed"

    # run by hand in a terminal (no agent): the CLI prints the bare deployment URL, no JSON
    p = _deploy(repo, BARE_URL="1")
    assert p.returncode == 0, p.stderr
    assert "npx -y vercel@65.0.0 alias set https://bore-studio-xyz9.vercel.app bore.test --token tok" in repo["log"].read_text()


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
    repo["live"].write_text(json.dumps({"commit": repo["commit"], "api": 4}, separators=(",", ":")))
    p = _deploy(repo)
    assert p.returncode == 0, p.stderr
    assert "deploy: bore.test pointed at https://bore-studio-old1.vercel.app before this deploy" in p.stdout
    assert "npx -y vercel@65.0.0 alias set https://bore-studio-old1.vercel.app bore.test --token $VERCEL_TOKEN" in p.stdout
    log = repo["log"].read_text().splitlines()
    deployed = next(i for i, x in enumerate(log) if " deploy " in x)
    assert log.index("npx -y vercel@65.0.0 alias ls --token tok") < deployed < next(i for i, x in enumerate(log) if " alias set " in x)
    # listed before the deploy: were a deploy ever to move the address itself, it still names the
    # old deployment, not the new one (a rollback to that would change nothing)
    p = _deploy(repo, DEPLOY_MOVES_ALIAS="1")
    assert p.returncode == 0 and (repo["out"] / "moved").exists(), p.stderr
    assert "deploy: bore.test pointed at https://bore-studio-old1.vercel.app before this deploy" in p.stdout
    p = _deploy(repo, NO_ALIASES="1")
    assert p.returncode == 0 and "could not tell which deployment bore.test points at now" in p.stderr


def test_only_the_products_own_project_at_the_address_it_is_given(repo):
    repo["live"].write_text(json.dumps({"commit": repo["commit"], "api": 4}, separators=(",", ":")))
    env = {k: v for k, v in repo["env"].items() if k != "DASHBOARD_ALIAS"}
    p = subprocess.run(["bash", str(repo["dash"] / "deploy.sh")], env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode != 0 and "set DASHBOARD_ALIAS" in p.stderr and repo["log"].read_text() == "", "no default address"
    (repo["dash"] / ".vercel" / "project.json").write_text('{"projectId": "prj_9", "projectName": "some-other-app"}')
    p = _deploy(repo)
    assert p.returncode == 1 and "linked to the Vercel project some-other-app, not bore-studio" in p.stderr
    assert "npx" not in repo["log"].read_text(), "nothing deployed to another project"
    (repo["dash"] / ".vercel" / "project.json").unlink()
    p = _deploy(repo, VERCEL_SCOPE="demo-team")
    assert p.returncode == 0, p.stderr
    log = repo["log"].read_text()
    assert "npx -y vercel@65.0.0 link --yes --project bore-studio --scope demo-team --token tok" in log
    assert "deploy --prod --yes --scope demo-team --token tok" in log


def _setup(r, **env):
    return subprocess.run(["bash", str(r["root"] / "scripts" / "setup-vercel.sh")],
                          env={**r["env"], "SUPER_EMAIL": "owner@demo.example", **env}, capture_output=True, text=True, timeout=60)


def test_setup_creates_the_project_storage_and_keys_deploys_and_prints_the_set_password_link(repo):
    repo["live"].write_text(json.dumps({"commit": repo["commit"], "api": 4}, separators=(",", ":")))
    (repo["dash"] / ".vercel" / "project.json").unlink()
    p = _setup(repo)
    assert p.returncode == 0, p.stderr
    log = repo["log"].read_text().splitlines()
    assert "npx -y vercel@65.0.0 project add bore-studio --token tok" in log
    assert "npx -y vercel@65.0.0 blob create-store bore-studio-blob --access public --yes --environment production " \
           "--environment preview --environment development --token tok" in log
    added = dict(x.split("=", 1) for x in (repo["out"] / "env").read_text().splitlines())
    assert set(added) == {"DATA_KEY", "SESSION_SECRET", "ROUTINE_SECRET"}
    import base64
    assert len(base64.b64decode(added["DATA_KEY"])) == 32 and len(added["SESSION_SECRET"]) >= 32 and len(added["ROUTINE_SECRET"]) == 64
    secrets = repo["dash"] / ".vercel" / "studio-secrets.env"
    assert dict(x.split("=", 1) for x in secrets.read_text().splitlines()) == added
    assert secrets.stat().st_mode & 0o077 == 0, "readable by the owner only"
    assert not any(v in p.stdout + p.stderr for v in added.values()), "a generated value is never printed"
    deployed = next(i for i, x in enumerate(log) if " deploy --prod" in x)
    boot = next(i for i, x in enumerate(log) if "bootstrap" in x)
    assert deployed < boot and f"Authorization: Bearer {added['ROUTINE_SECRET']}" in log[boot]
    assert "https://bore.test/api/routine/bootstrap" in log[boot] and "owner@demo.example" in log[boot]
    assert "https://bore.test/reset#u1.tok" in p.stdout
    assert "ROUTINE_FIRE_URL/ROUTINE_FIRE_TOKEN STUDIO_FIRE_URL/STUDIO_FIRE_TOKEN ONBOARD_FIRE_URL/ONBOARD_FIRE_TOKEN" in p.stdout


def test_setup_again_keeps_every_key_the_project_has(repo):
    repo["live"].write_text(json.dumps({"commit": repo["commit"], "api": 4}, separators=(",", ":")))
    names = Path(repo["env"]["FAKE_ENV"])
    names.write_text("DATA_KEY\nSESSION_SECRET\nROUTINE_SECRET\nBLOB_READ_WRITE_TOKEN\nSTUDIO_FIRE_URL\nSTUDIO_FIRE_TOKEN\n")
    p = _setup(repo)                                            # the routine secret is not on this machine
    assert p.returncode == 1 and "run again with ROUTINE_SECRET set" in p.stderr
    assert not (repo["out"] / "env").exists() and "blob create-store" not in repo["log"].read_text()
    assert "DATA_KEY is already set in the project: kept" in p.stdout
    p = _setup(repo, ROUTINE_SECRET="s" * 64, BOOT_CODE="409")
    assert p.returncode == 0 and "a user already exists" in p.stdout
    assert "STUDIO_FIRE_URL" not in p.stdout.split("still for you to add")[-1]
    p = _setup(repo, SUPER_EMAIL='x", "role": "admin')
    assert p.returncode == 2 and "not an email address" in p.stderr
