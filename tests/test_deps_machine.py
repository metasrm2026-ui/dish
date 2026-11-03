"""The machine tests: a package can load and still not work, and the browser every card is drawn in
is not a pip package at all (with the browser folder emptied, ensure-deps used to say "already
installed" while every card render failed)."""
import importlib.metadata
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bore import deps

ROOT = Path(__file__).resolve().parent.parent
NO_BROWSER = "Error: BrowserType.launch: Executable doesn't exist at /opt/pw-browsers/chromium_headless_shell-1194/chrome-linux/headless_shell"


@pytest.fixture
def tests(monkeypatch):
    """The machine tests, made to pass or fail at will: tests['chromium'] = 'why' fails it."""
    out, calls = {}, []
    monkeypatch.setattr(deps, "_TRIED", {})
    for fn, name, _, _ in deps.SELFTESTS:
        def run(fn=fn, name=name):
            calls.append(fn)
            if out.get(name):
                raise RuntimeError(out[name])
        monkeypatch.setattr(deps, fn, run)
    out["calls"] = calls
    return out


def test_compose_jobs_need_a_browser_that_starts(tests):
    tests["chromium"] = NO_BROWSER
    assert "chromium" in deps.problems(deps.NEEDS["compose"])
    assert "Executable doesn't exist" in deps.problems(deps.NEEDS["compose"])["chromium"]
    assert deps.problems(deps.NEEDS["images"]) == {} and deps.problems(deps.NEEDS["ideas"]) == {}
    assert deps.problems() == {}                       # the package check alone tries no machine test
    assert tests["calls"].count("selftest_render") == 1     # once per process: a launch can take its timeout


def test_jobs_that_read_text_on_images_need_ocr_that_reads(tests):
    tests["ocr"] = "OCR read nothing on a test card saying 42"     # rapidocr imports, but cannot read
    for kind in ("compose", "images"):        # COMPOSE reads its card back (`routine verify`), IMAGES checks photos
        assert "OCR read nothing" in deps.problems(deps.NEEDS[kind]).get("ocr", ""), kind
    assert deps.problems(deps.NEEDS["check"]) == {} and deps.problems(deps.NEEDS["ideas"]) == {}


def test_a_failing_test_names_the_package_to_install_again(tests, capsys):
    tests["image files"] = "OSError: encoder jpeg not available"
    tests["chromium"] = NO_BROWSER
    assert deps.main(["--selftest"]) == 0
    assert capsys.readouterr().out.split() == ["Pillow>=10.0"]       # chromium: no requirement line
    assert deps.main(["--selftest", "--why"]) == 1
    out = capsys.readouterr().out
    assert "FAIL image files" in out and "FAIL chromium" in out and "ok   ocr" in out
    assert deps.main([]) == 0 and capsys.readouterr().out == ""     # without --selftest: imports only
    assert deps.main(["--only", "chromium", "--why"]) == 1           # what ensure-deps asks before installing it
    assert deps.main(["--only", "requests,PyYAML", "--selftest", "--why"]) == 0   # the publisher: no machine tests


def test_a_test_is_not_tried_while_a_package_it_uses_does_not_load(tests, monkeypatch, capsys):
    monkeypatch.setattr(deps, "CHECKS", [("bore_no_such_module", "Pillow"), ("json", "playwright")])
    bad = deps.problems(["Pillow", "image files", "playwright", "chromium"])
    assert list(bad) == ["Pillow"] and "selftest_image" not in tests["calls"]
    assert deps.main(["--selftest", "--why"]) == 1
    assert "--   image files" in capsys.readouterr().out


def test_versions_other_than_the_tested_ones_are_named(tmp_path, monkeypatch, capsys):
    """A setup script that installed without constraints.txt: everything works, but not on the
    versions the tests passed with. Said (packages and what they need), not hidden."""
    here = f'python_version == "{sys.version_info.major}.{sys.version_info.minor}"'
    have = importlib.metadata.version("urllib3")
    (tmp_path / "constraints.txt").write_text(f"""# pins
requests==0.0.1; {here}
urllib3==0.0.2; {here}
idna=={importlib.metadata.version("idna")}; {here}
PyYAML==0.0.3; python_version == "2.7"
bore-not-installed==1.0; {here}
""")
    monkeypatch.setattr(deps, "CONSTRAINTS", tmp_path / "constraints.txt")
    out = deps.drift(["requests", "PyYAML"])
    assert out == [f"requests {importlib.metadata.version('requests')} (constraints.txt: 0.0.1)",
                   f"urllib3 {have} (constraints.txt: 0.0.2)"]          # what requests needs counts too
    assert deps.main(["--only", "requests", "--drift"]) == 0
    assert capsys.readouterr().out.splitlines()[0].startswith("requests ")
    monkeypatch.setattr(deps, "CONSTRAINTS", tmp_path / "missing.txt")
    assert deps.drift(["requests"]) == []


def test_the_machine_tests_pass_on_a_working_machine():
    deps._TRIED.clear()
    assert deps._tried("selftest_image") == "" and deps._tried("selftest_ocr") == ""
    why = deps._tried("selftest_render")
    if "Executable doesn't exist" in why:
        pytest.skip("no Chromium on this machine (`python -m playwright install --only-shell chromium`)")
    assert why == ""


def ensure_deps(tmp_path, browser_installs: bool = True, pip_fixes: bool = True, working: bool = False,
                drift: str = "", stall: bool = False, script: str = "ensure-deps.sh", env: dict | None = None):
    """Run scripts/ensure-deps.sh (or `script`) with a stand-in `python` that logs what it was asked: the
    browser does not start until `playwright install` ran (and then only if browser_installs; stall: the
    download hangs); Pillow fails the machine test until pip installed it again. working: both work from
    the start. drift: what `bore.deps --drift` says."""
    bin_ = tmp_path / "bin"
    bin_.mkdir(parents=True)
    state, log = tmp_path / "state", tmp_path / "log"
    state.mkdir()
    if working:
        (state / "browser").touch()
        (state / "pillow").touch()
    (bin_ / "python").write_text(f"""#!/usr/bin/env bash
echo "$*" >> {log}
s={state}
case "$*" in
  "-c "*) echo 3.11.15 ;;
  "-m playwright install"*) echo "browser timeout ${{BROWSER_INSTALL_TIMEOUT:-unset}}" >> {log}
                            {'exec sleep 30' if stall else ':'}
                            echo "Chromium Headless Shell 141.0 (playwright build v1194) downloaded to /opt/pw-browsers/x"
                            {'touch $s/browser' if browser_installs else 'echo "Error: download failed"'} ;;
  "-m pip install"*) echo "Collecting Pillow"; echo "error: subprocess-exited-with-error"
                     {'case "$*" in *--ignore-installed*) touch $s/pillow;; esac' if pip_fixes else ':'}
                     echo "Successfully installed pillow-12.3.0" ;;
  "-m bore.deps --only chromium --why") [ -e $s/browser ] || {{ echo "FAIL chromium  (Executable doesn't exist)"; exit 1; }} ;;
  "-m bore.deps --selftest --why") [ -e $s/browser ] && [ -e $s/pillow ] || {{ echo "FAIL something"; exit 1; }} ;;
  "-m bore.deps --selftest") [ -e $s/pillow ] || echo "Pillow>=10.0" ;;
  "-m bore.deps --requirements") echo "Pillow>=10.0" ;;
  "-m bore.deps --drift") {f'echo "{drift}"' if drift else ':'} ;;
  "-m bore.cli studio "*) ;;
  *) echo "unexpected: $*" >&2; exit 9 ;;
esac
""")
    (bin_ / "python").chmod(0o755)
    r = subprocess.run(["bash", str(ROOT / "scripts" / script)], capture_output=True, text=True,
                       env={**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", **(env or {})}, timeout=60)
    return r, log.read_text()


def test_a_working_machine_installs_nothing_and_says_which_versions_are_not_the_tested_ones(tmp_path):
    r, log = ensure_deps(tmp_path, working=True, drift="pillow 12.4.0 (constraints.txt: 12.3.0)")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "packages: already installed and working (python 3.11.15, " in r.stdout
    assert "packages: not the tested version: pillow 12.4.0 (constraints.txt: 12.3.0)" in r.stdout
    assert "-m pip" not in log and "-m playwright" not in log           # the fast path: checks only


def test_a_browser_download_that_stalls_is_given_up_in_time(tmp_path):
    started = time.monotonic()
    r, _ = ensure_deps(tmp_path, stall=True, env={"BROWSER_INSTALL_TIMEOUT": "1"})
    assert r.returncode == 1 and "the card browser still does not start" in r.stdout
    assert time.monotonic() - started < 20                              # not the stalled download's 30 s


def test_a_studio_session_waits_briefly_for_the_browser(tmp_path):
    """An editor waits for every Studio session; IDEAS and CHECK jobs never draw a card."""
    r, log = ensure_deps(tmp_path, script="studio-start.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "browser timeout 90" in log and "-m bore.cli studio jobs" in log
    r, log = ensure_deps(tmp_path / "hourly")
    assert "browser timeout unset" in log                               # the hourly run: up to 600 s


def test_ensure_deps_installs_the_browser_and_pinned_packages(tmp_path):
    r, log = ensure_deps(tmp_path, browser_installs=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "the card browser (Chromium) does not start; installing it" in r.stdout and "downloaded to" in r.stdout
    assert "installing again: Pillow" in r.stdout and "error: subprocess-exited-with-error" in r.stdout  # pip's reason kept
    assert "packages: ready in" in r.stdout and "python 3.11.15" in r.stdout
    pips = [line for line in log.splitlines() if line.startswith("-m pip install")]
    assert len(pips) == 2 and all("-c constraints.txt" in p for p in pips)      # tested versions, not the newest
    assert "-m playwright install --only-shell chromium" in log


def test_ensure_deps_fails_loudly_when_the_browser_cannot_be_installed(tmp_path):
    r, _ = ensure_deps(tmp_path, browser_installs=False)
    assert r.returncode == 1
    assert "the card browser still does not start" in r.stdout and "FAIL chromium" in r.stdout


def test_ensure_deps_says_what_still_fails_after_installing_again(tmp_path):
    r, _ = ensure_deps(tmp_path, browser_installs=True, pip_fixes=False)
    assert r.returncode == 1 and "still not working after installing them again (python 3.11.15)" in r.stdout
