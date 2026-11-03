"""The GitHub workflows. CI (test.yml) runs both suites on every push to the routines' branch and
on every pull request. The hourly health check (health.yml), its script run in Node with the
dashboard and the GitHub API faked: one issue, opened at once for red and after 3 bad checks for
amber, closed when green, with reason codes and ids only (never the texts)."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
HARNESS = """
const calls = [], sc = JSON.parse(process.env.SCENARIO);
globalThis.fetch = async (url, opts) => {
  calls.push(['fetch', opts.method, new URL(url).pathname, opts.headers.Authorization === 'Bearer the-secret']);
  const r = sc.replies[new URL(url).pathname];
  if (!r) throw Object.assign(new Error('unreachable'), { name: 'TypeError' });
  return new Response(JSON.stringify(r.body), { status: r.status || 200 });
};
const core = { notice: m => calls.push(['notice', m]), info: m => calls.push(['info', m]) };
const github = { paginate: async () => sc.issues, rest: { issues: { listForRepo: {},
  createComment: async a => calls.push(['comment', a]), update: async a => calls.push(['update', a]), create: async a => calls.push(['create', a]) } } };
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
await new AsyncFunction('core', 'github', 'context', process.env.SCRIPT)(core, github, { repo: { owner: 'o', repo: 'r' } });
console.log(JSON.stringify(calls));
"""

REASON = {"code": "hourly_stale", "level": "red", "text": "No hourly post run seen for 3h: SecretSourceXYZ", "since": 1792600000000,
          "ids": ["demo-page-202610211405-a-story"]}


def _run(tmp_path, health=None, watchdog=None, issues=(), secret="the-secret", status=200, dashboard="https://dash.test/"):
    if not shutil.which("node"):
        pytest.skip("node not available")
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "health.yml").read_text())
    step = wf["jobs"]["health"]["steps"][0]
    assert step["uses"].startswith("actions/github-script@")
    replies = {}
    if health is not None:
        replies["/api/routine/health"] = {"body": health, "status": status}
    if watchdog is not None:
        replies["/api/routine/watchdog"] = {"body": watchdog, "status": status}
    (tmp_path / "h.mjs").write_text(HARNESS)
    env = {**os.environ, "SCRIPT": step["with"]["script"], "SECRET": secret, "DASHBOARD": dashboard,
           "SCENARIO": json.dumps({"replies": replies, "issues": list(issues)})}
    p = subprocess.run(["node", str(tmp_path / "h.mjs")], env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


def _kinds(calls):
    return [c[0] for c in calls if c[0] not in ("fetch", "info")]


def test_red_opens_one_issue_with_codes_and_ids_only(tmp_path):
    calls = _run(tmp_path, {"status": "red", "reasons": [REASON], "checked_at": 1}, {"streak": 1, "open_jobs": 0})
    assert [c[1:3] for c in calls if c[0] == "fetch"] == [["POST", "/api/routine/watchdog"], ["GET", "/api/routine/health"]]
    assert all(c[3] for c in calls if c[0] == "fetch"), "the routine secret goes in the header"
    create = [c[1] for c in calls if c[0] == "create"]
    assert len(create) == 1 and create[0]["title"] == "Bore Studio health: RED"
    assert "`hourly_stale`" in create[0]["body"] and "`demo-page-202610211405-a-story`" in create[0]["body"]
    assert "SecretSourceXYZ" not in create[0]["body"] and "No hourly" not in create[0]["body"], "never the texts"


def test_amber_waits_for_three_bad_checks_and_a_change_of_status_is_a_comment(tmp_path):
    amber = {"status": "amber", "reasons": [{**REASON, "level": "amber", "code": "topics_stale"}], "checked_at": 1}
    assert _kinds(_run(tmp_path, amber, {"streak": 2})) == []
    assert _kinds(_run(tmp_path, amber, {"streak": 3})) == ["create"]
    open_amber = [{"number": 7, "title": "Bore Studio health: AMBER"}]
    calls = _run(tmp_path, {"status": "red", "reasons": [REASON], "checked_at": 1}, {"streak": 4}, open_amber)
    assert _kinds(calls) == ["update", "comment"]
    assert calls[-2][1]["title"] == "Bore Studio health: RED" and calls[-1][1]["body"] == "Now RED."
    assert _kinds(_run(tmp_path, amber, {"streak": 5}, [{"number": 7, "title": "Bore Studio health: AMBER"}])) == ["update"]


def test_green_closes_the_issue_an_unreachable_dashboard_is_red_and_no_secret_is_a_notice(tmp_path):
    calls = _run(tmp_path, {"status": "green", "reasons": [], "checked_at": 1}, {"streak": 0}, [{"number": 7, "title": "Bore Studio health: RED"}])
    assert _kinds(calls) == ["comment", "update"] and calls[-1][1]["state"] == "closed"
    assert _kinds(_run(tmp_path, {"status": "green", "reasons": [], "checked_at": 1}, {"streak": 0})) == []
    calls = _run(tmp_path)                                              # neither call answers
    assert _kinds(calls) == ["create"] and "`dashboard_unreachable`" in calls[-1][1]["body"]
    assert "`health: TypeError`" in calls[-1][1]["body"] and "`watchdog: TypeError`" in calls[-1][1]["body"]
    calls = _run(tmp_path, {"error": "not allowed"}, {"error": "not allowed"}, status=401)   # a wrong secret: the issue says so
    assert _kinds(calls) == ["create"] and "`health: HTTP 401`" in calls[-1][1]["body"] and "`watchdog: HTTP 401`" in calls[-1][1]["body"]
    calls = _run(tmp_path, secret="")
    assert _kinds(calls) == ["notice"] and not [c for c in calls if c[0] == "fetch"]
    calls = _run(tmp_path, dashboard="")                                # no address of another deployment to fall back on
    assert _kinds(calls) == ["notice"] and not [c for c in calls if c[0] == "fetch"]


def test_the_watchdogs_fresh_status_is_the_one_judged(tmp_path):
    """The watchdog works the status out when it is called; another instance's GET can answer with an
    older one (storage refusing reads, it gives what it knew before), and a stale green would close the
    issue during an outage. The GET is asked only when the watchdog gave no status."""
    red = {"status": "red", "reasons": [{**REASON, "code": "storage_unreadable"}], "checked_at": 2}
    stale = {"status": "green", "reasons": [], "checked_at": 1}
    calls = _run(tmp_path, stale, {"streak": 1, "open_jobs": 0, "health": red}, [{"number": 7, "title": "Bore Studio health: RED"}])
    assert [c[1:3] for c in calls if c[0] == "fetch"] == [["POST", "/api/routine/watchdog"]]
    assert _kinds(calls) == ["update"] and "`storage_unreadable`" in calls[-1][1]["body"], "not closed by a stale green"
    calls = _run(tmp_path, stale, {"streak": 0, "health": {"status": "unknown"}}, [{"number": 7, "title": "Bore Studio health: RED"}])
    assert [c[2] for c in calls if c[0] == "fetch"] == ["/api/routine/watchdog", "/api/routine/health"]
    assert _kinds(calls) == ["comment", "update"] and calls[-1][1]["state"] == "closed"


def test_ci_runs_both_suites_on_the_routines_branch_and_pull_requests_with_the_routines_python():
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "test.yml").read_text())
    on = wf.get("on", wf.get(True))                     # PyYAML reads the key `on` as True
    branch = yaml.safe_load((ROOT / "routines" / "triggers.yaml").read_text())["branch"]
    assert on["push"]["branches"] == [branch] and "pull_request" in on, "pushes to other branches do not use up minutes"
    assert wf["concurrency"]["cancel-in-progress"] is True
    py = wf["jobs"]["python"]["steps"]
    assert next(s for s in py if "setup-python" in s.get("uses", ""))["with"]["python-version"] == "3.11"
    runs = "\n".join(s.get("run", "") for s in py)
    assert "-r requirements.txt" in runs and "-c constraints.txt" in runs and "python -m pytest -q" in runs
    assert "playwright install --with-deps chromium" in runs, "the card tests draw in Chromium"
    assert any("ms-playwright" in str(s.get("with", {}).get("path", "")) for s in py), "the browser download is cached"
    # the Python suite has the dashboard's packages, or the run-report contract test is skipped
    names = [s.get("uses", "") or s.get("run", "") for s in py]
    assert any(n.startswith("actions/setup-node@") for n in names)
    assert names.index("npm ci --prefix dashboard") < names.index("python -m pytest -q")
    node = wf["jobs"]["dashboard"]
    assert next(s for s in node["steps"] if "setup-node" in s.get("uses", ""))["with"]["node-version"] == 22
    assert node["defaults"]["run"]["working-directory"] == "dashboard"
    assert [s["run"] for s in node["steps"] if "run" in s] == ["npm ci", "npm test"]


def test_workflows_run_on_a_pinned_runner_image():
    """ubuntu-latest moved to 26.04 on Oct 19 2026 and the first CI run failed installing Chromium:
    a moving image changes under the workflow like an unpinned package (a new one is a reviewed bump)."""
    for name in ("test.yml", "health.yml"):
        wf = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())
        for job, spec in wf["jobs"].items():
            assert "latest" not in str(spec["runs-on"]), f"{name} {job}: pin the runner image"
