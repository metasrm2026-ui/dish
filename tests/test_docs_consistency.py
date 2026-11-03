"""The routine's instructions say what the code and settings do (the same fact restated in prose
went stale: ROUTINE.md kept 'posts go out 07:00-23:00 New York' for a week after the owner turned
audience hours off on Oct 13, and told the session both to use the official headshot rather than
skip and never to switch templates for a photo problem; then it named a template that blocks the same
headshot, and step 6 still said to skip what `routine skip` now refuses)."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ROUTINE = (ROOT / "ROUTINE.md").read_text()


def test_audience_hours_are_described_only_when_they_are_set():
    sp = yaml.safe_load((ROOT / "config" / "socialpilot.yaml").read_text()) or {}
    if not sp.get("audience_hours"):
        for stale in ("07:00-23:00", "holds late-night approvals", "10-24\n  hours"):
            assert stale not in ROUTINE, stale


def test_the_headshot_rule_sends_the_session_only_where_the_renderer_can_place_it():
    """A tight headshot (ESPN-style 600x436, the face 34% x 60% of it) is BLOCKED in Demo Sports's report
    and quote alike: quote's clear area is no bigger and the photo is scaled by its height in both.
    So the docs may name a template as the way round a BLOCKED headshot only if the renderer
    actually keeps the face clear in it (the Oct 21 wording sent the session to quote)."""
    import io
    import re

    from PIL import Image

    from bore.cards import CardSpec, plan_layout
    buf = io.BytesIO()
    Image.new("RGB", (600, 436), "grey").save(buf, "JPEG")
    clear = {tpl for tpl in ("report", "quote") for focus in (None, (0.5, 0.42))
             if not plan_layout("demo-sports", CardSpec(template=tpl, headline_lines=["X"], photos=[buf.getvalue()],
                                                   face_box=(0.33, 0.12, 0.34, 0.6), focus=focus, face_size=0.6))}
    rule = ROUTINE[ROUTINE.index("The official NFL headshot"):ROUTINE.index("For injury or trade news")]
    named = set(re.findall(r"(?:use it|switch) (?:in|to) `(\w+)`", rule))
    assert named <= clear, f"the docs send a blocked headshot to {named - clear}, where it is blocked too"
    if not clear:
        assert "BLOCKED" in rule and "skip with `--photo`" in rule


def test_editors_requests_are_never_skipped_in_the_docs():
    assert "Never skip a LINK silently" not in ROUTINE and "Never skip a REGEN silently" not in ROUTINE
    assert ROUTINE.count("stays NOT DONE until it is uploaded") == 2
    # step 6, the one a session follows to clear NOT DONE: skip (refused for a LINK/REGEN) is for backups
    step6 = ROUTINE[ROUTINE.index("If `check` prints NOT DONE"):ROUTINE.index("## 7.")]
    assert "routine link-fail" in step6 and "routine regen-fail" in step6 and "never skipped" in step6
    assert "(write + upload, or skip with a reason)" not in step6
