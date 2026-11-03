"""Studio jobs: one session per job, summaries never cut mid-sentence, and a fact check that is
ready when the editor gets to it."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from bore import studio as S


def test_a_summary_cut_mid_sentence_is_written_again(tmp_path):
    assert S.summary_cut("When Jaxson Dart went down for the season, the Giants brought in McCarthy. And while some of that was due to a")
    assert S.summary_cut("") and S.summary_cut("Cut here…")
    assert not S.summary_cut("The Giants brought in J.J. McCarthy after Jaxson Dart went down.")
    assert S.summary_cut("Some of that was due to a...") and S.summary_cut("Some of that was due to a […]")
    assert not S.summary_cut("What a win 🏈") and not S.summary_cut("शानदार जीत।")   # emoji, Hindi full stop
    d = tmp_path / "p1"
    d.mkdir()
    ideas = [{"title": "A", "template": "headline", "layout": "headline", "description": "x"},
             {"title": "B", "template": "quote", "layout": "quote", "description": "y"}]
    (d / "ideas.json").write_text(json.dumps(ideas))
    brief = {"page": "demo-sports", "studio": {"summary_needed": True}}
    (d / "brief.json").write_text(json.dumps(brief))
    with pytest.raises(ValueError, match="summary"):
        S.post_result(d, "ideas")
    (d / "summary.json").write_text(json.dumps({"headline": "H", "summary": "When Jaxson Dart went down, the Giants turned to J.J. McCarthy."}))
    assert S.post_result(d, "ideas")["summary"].endswith("McCarthy.")


def test_a_job_another_session_took_is_skipped(monkeypatch, capsys):
    posted = []

    def post(dash, path, body):
        posted.append(path)
        if path.endswith("/j1"):
            raise RuntimeError("/api/routine/jobs/j1: HTTP 409 already running: another session has it")
        return {}
    monkeypatch.setattr(S, "_get", lambda dash, path, **k: {"jobs": [{"id": "j1", "kind": "ideas", "page": "demo-sports", "posts": []},
                                                                     {"id": "j2", "kind": "ideas", "page": "demo-sports", "posts": []}]})
    monkeypatch.setattr(S, "_post", post)
    monkeypatch.setattr(S, "write_job", lambda job, cfg: S.WORK / job["id"])
    monkeypatch.setattr(S, "_get", lambda dash, path, **k: {"jobs": [{"id": f"j{i}", "kind": "ideas", "page": "demo-sports", "posts": []}
                                                                     for i in (1, 2, 3)]})
    out = S.pull_jobs({}, dash=object())
    assert [p.name for p in out] == ["j2"] and "SKIP   j1" in capsys.readouterr().out   # one job per call
    assert posted == ["jobs/j1", "jobs/j2"]


def test_precheck_right_after_composing(tmp_path, monkeypatch):
    jd = tmp_path / "demo-sports-1-x"
    d = jd / "p1"
    d.mkdir(parents=True)
    (jd / "job.json").write_text(json.dumps({"id": "demo-sports-1-x", "kind": "compose", "page": "demo-sports"}))
    (d / "brief.json").write_text(json.dumps({"page_name": "Demo Sports", "studio": {"caption": "old", "hashtags": []}}))
    (d / "copy.json").write_text(json.dumps({"caption": "McCarthy is in the QB room.\n\n#Giants #NFL"}))
    Image.new("RGB", (1080, 1350)).save(d / "card.jpg")
    S.precheck(jd)
    req = (d / "review_request.md").read_text()
    assert "McCarthy is in the QB room." in req and "at most 6 searches" in req
    (d / "check.json").write_text(json.dumps({k: [] for k in ("factual", "timing", "grammar", "other", "guidelines")}))
    sent = []
    monkeypatch.setattr(S, "_post", lambda dash, path, body: sent.append((path, body)) or {})
    S.precheck(jd, send=True, dash=object())
    (path, body), = sent
    r = body["posts"]["p1"]
    assert path == "jobs/demo-sports-1-x" and body["precheck"] and "status" not in body
    assert set(r["check"]) == {"factual", "timing", "grammar", "other", "guidelines"}   # the dashboard knows what it checked
    assert r["checked_caption"] == "McCarthy is in the QB room.\n#Giants #NFL"          # and which text it was


def test_jobs_this_machine_cannot_do_are_left_and_a_job_it_could_not_prepare_goes_back(monkeypatch, capsys):
    from bore import deps
    posted = []
    jobs = [{"id": "j1", "kind": "compose", "page": "demo-sports", "posts": []}, {"id": "j2", "kind": "ideas", "page": "demo-sports", "posts": []}]
    monkeypatch.setattr(S, "_get", lambda dash, path, **k: {"jobs": jobs})
    answer = {"status": "queued"}
    monkeypatch.setattr(S, "_post", lambda dash, path, body: posted.append((path, body)) or answer)
    monkeypatch.setattr(S, "write_job", lambda job, cfg: S.WORK / job["id"])
    monkeypatch.setattr(S, "_session_id", lambda: "a1b2c3d4e5f6")
    # the machine's Pillow does not load: the COMPOSE job is left for another session, the IDEAS job
    # (no image packages) is taken
    monkeypatch.setattr(deps, "problems", lambda names=None: {"Pillow": "ImportError: cannot import name '_imaging'"} if names else {})
    out = S.pull_jobs({}, dash=object())
    assert [p.name for p in out] == ["j2"]
    assert posted == [("jobs/j1", {"left": True, "error": "packages not loading: Pillow: ImportError: cannot import name '_imaging'",
                                   "session": "a1b2c3d4e5f6"}),
                      ("jobs/j2", {"status": "running"})]          # the dashboard counts the sessions that left it
    assert "LEAVE  j1" in capsys.readouterr().out
    # nothing this machine can do: no claim, and the message says what to run
    monkeypatch.setattr(S, "_get", lambda dash, path, **k: {"jobs": jobs[:1]})
    posted.clear()
    with pytest.raises(SystemExit, match="no job taken.*Packages not loading.*ensure-deps.*studio jobs"):
        S.pull_jobs({}, dash=object())
    assert [b for _, b in posted] == [{"left": True, "error": "packages not loading: Pillow: ImportError: cannot import name '_imaging'",
                                       "session": "a1b2c3d4e5f6"}]
    # a job claimed and then not prepared goes back to the queue (with why), and the next job is taken
    monkeypatch.setattr(deps, "problems", lambda names=None: {})
    monkeypatch.setattr(S, "_get", lambda dash, path, **k: {"jobs": jobs})
    posted.clear()

    def write(job, cfg):
        if job["id"] == "j1":
            raise AttributeError("'NoneType' object has no attribute 'get'")
        return S.WORK / job["id"]
    monkeypatch.setattr(S, "write_job", write)
    out = S.pull_jobs({}, dash=object())
    assert [p.name for p in out] == ["j2"]
    assert posted == [("jobs/j1", {"status": "running"}),
                      ("jobs/j1", {"status": "queued", "release": True, "error": "AttributeError: 'NoneType' object has no attribute 'get'"}),
                      ("jobs/j2", {"status": "running"})]
    got = capsys.readouterr()
    assert "BACK   j1" in got.out and "Traceback" in got.err          # the session sees where it failed
    monkeypatch.setattr(S, "_get", lambda dash, path, **k: {"jobs": jobs[:1]})
    with pytest.raises(SystemExit, match="no job taken.*Could not prepare j1.*back in the queue"):
        S.pull_jobs({}, dash=object())
    answer["status"] = "failed"                                         # the third try: the editor was told
    with pytest.raises(SystemExit, match="the editor was told; do not retry"):
        S.pull_jobs({}, dash=object())


def test_a_connection_problem_with_the_editors_image_hands_the_job_back(monkeypatch, tmp_path):
    import io

    import requests
    monkeypatch.setattr(S.time, "sleep", lambda s: None)
    calls = []

    class Resp:
        def __init__(self, code, content=b""):
            self.status_code, self.content = code, content

    def get(url, **k):
        calls.append(url)
        if "timeout" in url:
            raise requests.ConnectTimeout("timed out")
        if "flaky" in url and len(calls) < 3:
            return Resp(503)
        if "gone" in url:
            return Resp(404)
        buf = io.BytesIO()
        Image.new("RGB", (40, 30)).save(buf, "JPEG")
        return Resp(200, buf.getvalue())
    monkeypatch.setattr(requests, "get", get)
    assert S._download("https://x/gone.jpg", tmp_path / "a.jpg", required=True) is None    # the image's fault: the editor is told
    assert S._download("https://x/timeout.jpg", tmp_path / "b.jpg") is None and len(calls) == 4   # an extra photo: skipped
    with pytest.raises(ConnectionError, match="timed out"):                                 # the editor's photo: handed back
        S._download("https://x/timeout.jpg", tmp_path / "c.jpg", required=True)
    calls.clear()
    assert S._download("https://x/flaky.jpg", tmp_path / "d.jpg", required=True) == (40, 30)   # a server hiccup: tried again


def test_packages_are_checked_by_loading_them(monkeypatch):
    from bore import deps
    monkeypatch.setattr(deps, "CHECKS", [("json", "simplejson"), ("bore_no_such_module", "Pillow")])
    assert list(deps.problems()) == ["Pillow"]
    # reportlab and rapidocr fail while Pillow does: only Pillow is installed again
    bad = {"Pillow": "ImportError: cannot import name '_imaging' from 'PIL' (/usr/lib/python3/dist-packages/PIL/__init__.py)",
           "reportlab": "ImportError: cannot import name '_imaging' from 'PIL'", "numpy": "ImportError: numpy core failed"}
    monkeypatch.setattr(deps, "CHECKS", [("reportlab.pdfgen.canvas", "reportlab"), ("PIL.Image", "Pillow"), ("numpy", "numpy")])
    assert set(deps.causes(bad)) == {"Pillow", "numpy"}
    assert set(deps.causes({"Pillow": "fails over numpy", "numpy": "fails over PIL"})) == {"Pillow", "numpy"}   # never none
    # a failure that does not name its cause: the package metadata says what it needs
    needs = {"rapidocr-onnxruntime": {"opencv-python", "numpy", "pillow"}, "opencv-python": {"numpy"}}
    monkeypatch.setattr(deps, "_requires", lambda name: needs.get(name, set()))
    monkeypatch.setattr(deps, "CHECKS", [("cv2", "opencv-python"), ("rapidocr_onnxruntime", "rapidocr-onnxruntime")])
    same = "ImportError: numpy.core.multiarray failed to import"
    assert list(deps.causes({"opencv-python": same, "rapidocr-onnxruntime": same})) == ["opencv-python"]
    lines = deps.requirement_lines(["Pillow", "rapidocr-onnxruntime"])
    assert "Pillow>=10.0" in lines and any(line.startswith("rapidocr-onnxruntime") and "python_version" in line for line in lines)


def test_the_publisher_checks_only_its_own_packages(capsys):
    from bore import deps
    assert deps.main(["--only", "requests,PyYAML", "--requirements"]) == 0
    assert capsys.readouterr().out.split() == ["requests>=2.31", "PyYAML>=6.0"]
    assert deps.main(["--only", "requests,PyYAML", "--why"]) == 0
    assert "Pillow" not in capsys.readouterr().out          # a broken Pillow never holds up scheduled posts


def test_what_a_failing_import_prints_never_reaches_the_requirement_list(monkeypatch, tmp_path, capfd):
    from bore import deps
    (tmp_path / "noisy_cv.py").write_text("import os, sys\nprint('OpenCV bindings requires numpy')\nos.write(1, b'pip install numpy\\n')\n"
                                          "raise ImportError('numpy.core.multiarray failed to import')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(deps, "CHECKS", [("noisy_cv", "opencv-python"), ("json", "requests")])
    assert list(deps.problems()) == ["opencv-python"]
    got = capfd.readouterr()
    assert got.out == "" and "OpenCV" not in got.err
    assert deps.main([]) == 0
    assert capfd.readouterr().out.split() == ["opencv-python>=4.5.1.48"]


def test_a_check_says_which_caption_it_reviewed(tmp_path):
    """The dashboard marks a check out of date when the editor changed the caption after the
    reviewer read it: it needs the exact text the reviewer was given."""
    d = tmp_path / "p1"
    d.mkdir()
    brief = {"page_name": "Demo Sports", "studio": {"caption": "  Line one.\n\nLine two --- with dashes\n---\nand a rule  ",
                                                    "hashtags": ["#A", "#B"]}}
    (d / "review_request.md").write_text(S.review_request(d, brief))
    (d / "check.json").write_text(json.dumps({k: [] for k in ("factual", "timing", "grammar", "other", "guidelines")}))
    out = S.post_result(d, "check")
    assert out["checked_caption"] == "Line one.\n\nLine two --- with dashes\n---\nand a rule\n#A #B"
    (d / "review_request.md").unlink()               # an older job folder: the dashboard falls back to its own copy
    assert "checked_caption" not in S.post_result(d, "check")


def test_the_session_id_is_kept_for_the_whole_session(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "WORK", tmp_path / "studio")
    a = S._session_id()
    assert len(a) == 12 and S._session_id() == a           # the same machine leaving again is counted once
    (tmp_path / "studio" / ".session").write_text("not an id")
    assert S._session_id() != a and len(S._session_id()) == 12


def _composed(d, copy, brief):
    """A post folder whose card has been made, linted, verified and looked at."""
    d.mkdir(parents=True)
    (d / "brief.json").write_text(json.dumps(brief))
    (d / "copy.json").write_text(json.dumps(copy))
    Image.new("RGB", (1080, 1350)).save(d / "card.jpg")
    for f in ("lint.json", "ocr.json", "verdict.json"):
        (d / f).write_text(json.dumps({"pass": True}))


def test_a_card_says_which_photo_it_was_built_on(tmp_path):
    """A session that took the job over may have been given another photo than the one that finishes
    late: the dashboard keeps only a card built on the editor's current photo."""
    d = tmp_path / "p1"
    _composed(d, {"photo_index": 0, "caption": "x"},
              {"page": "demo", "studio": {"base_image": {"url": "https://x/chosen.jpg"}},
               "photos": [{"index": 0, "url": "https://x/chosen.jpg"}]})
    assert S.post_result(d, "compose")["photo"] == "https://x/chosen.jpg"


def test_the_precheck_reviews_the_caption_that_will_be_posted(tmp_path):
    """A visual made again keeps the caption the editor wrote (the new one is only offered): the
    automatic check reviews theirs, not the one just composed."""
    jd = tmp_path / "demo-sports-2-x"
    _composed(jd / "p1", {"caption": "The routine's new caption."},
              {"page_name": "Demo Sports", "studio": {"caption": "The editor's own words.", "hashtags": ["#Mine"], "caption_kept": True}})
    _composed(jd / "p2", {"caption": "The routine's new caption."},
              {"page_name": "Demo Sports", "studio": {"caption": "", "hashtags": [], "caption_kept": False}})
    (jd / "job.json").write_text(json.dumps({"id": "demo-sports-2-x", "kind": "compose", "page": "demo-sports"}))
    S.precheck(jd)
    assert S._checked_caption(jd / "p1" / "review_request.md") == "The editor's own words.\n#Mine"
    assert S._checked_caption(jd / "p2" / "review_request.md") == "The routine's new caption."


def test_write_job_says_when_the_editor_wrote_the_caption(monkeypatch, tmp_path):
    import bore.routine as R
    monkeypatch.setattr(S, "WORK", tmp_path)
    monkeypatch.setattr(S, "studio_meta", lambda: {})
    monkeypatch.setattr(S, "_page_templates", lambda page: [])
    monkeypatch.setattr(R, "load_playbook", lambda page: {})
    story = {"headline": "H", "summary": "S.", "sources": []}
    job = {"id": "j9", "kind": "images", "page": "demo-sports", "batch_id": "b",
           "posts": [{"id": "p1", "story": story, "caption": "Mine", "caption_edited_at": 1}, {"id": "p2", "story": story}]}
    jd = S.write_job(job, {})
    kept = [json.loads((jd / p / "brief.json").read_text())["studio"]["caption_kept"] for p in ("p1", "p2")]
    assert kept == [True, False]


def test_the_checked_caption_is_the_text_the_dashboard_posts(tmp_path):
    """The dashboard shows a check as out of date when the text it reviewed is not the post's text:
    the reviewer's copy of a caption (rendered here) and the dashboard's (dashboard/lib/caption.js, as
    it keeps a compose result) must be the same text, or every automatic check reads 'edited since'."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is needed to run the dashboard's caption code")
    caption_js = (Path(__file__).resolve().parent.parent / "dashboard" / "lib" / "caption.js").as_uri()
    cases = [{"caption": "McCarthy is in the QB room.\n\n#Giants #NFL"},                      # tags on the last line
             {"caption": "  Two lines.\n\nThe second one.  ", "hashtags": ["Giants", "#NFL", " #Spaced "]},
             {"caption": "No tags at all."},
             {"caption": "", "hashtags": ["#OnlyTags"]},
             {"caption": "Too many tags.", "hashtags": [f"#t{i}" for i in range(35)] + ["", "#" + "x" * 70]}]
    jd = tmp_path / "demo-sports-3-x"
    for i, copy in enumerate(cases):
        _composed(jd / f"p{i}", copy, {"page_name": "Demo Sports", "studio": {"caption": "", "hashtags": []}})
    (jd / "job.json").write_text(json.dumps({"id": "demo-sports-3-x", "kind": "compose", "page": "demo-sports"}))
    S.precheck(jd)
    for i, copy in enumerate(cases):
        cap, tags = S._split_caption(copy)              # what post_result sends with the card
        js = (f"import {{ routineCaption, routineTags, postText }} from {json.dumps(caption_js)};"
              "const r = JSON.parse(process.argv[1]);"
              "console.log(JSON.stringify(postText({ caption: routineCaption(r.caption), hashtags: routineTags(r.hashtags) })));")
        out = subprocess.run([node, "--input-type=module", "-e", js, json.dumps({"caption": cap, "hashtags": tags})],
                             capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout) == S._checked_caption(jd / f"p{i}" / "review_request.md"), copy
