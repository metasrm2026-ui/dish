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


def test_the_headshot_rule_names_no_layout_as_the_way_round_a_blocked_photo():
    """A tight headshot (the face fills the frame) is BLOCKED in every layout alike: the renderer
    already tries the headline at the top and the bottom. The docs once sent a BLOCKED headshot to
    another template, where it was blocked too; and which layouts a page has is its playbook's, not
    this file's. So the rule names no layout, and says to skip with --photo."""
    import re
    rule = ROUTINE[ROUTINE.index("An official headshot"):ROUTINE.index("For news about someone's career now")]
    assert not re.findall(r"(?:use it|switch) (?:in|to) `(\w+)`", rule), "no layout named as the way round a BLOCKED photo"
    assert "BLOCKED" in rule and "skip with `--photo`" in rule


def test_the_routines_docs_name_no_page_of_their_own():
    """Each page's rules come from its playbook and documents (docs/PRODUCT.md): the routines' docs carry
    no page name, account or example from any page."""
    import re
    for doc in ("ROUTINE.md", "STUDIO.md", "PUBLISH.md", "TOPICS.md", "ONBOARD.md", "README.md"):
        text = (ROOT / doc).read_text()
        assert not re.search(r"\b\d{7}\b", text), f"{doc}: a SocialPilot account id"
        assert "Asia/Kolkata" not in text and " IST" not in text, f"{doc}: another deployment's time zone"


def test_editors_requests_are_never_skipped_in_the_docs():
    assert "Never skip a LINK silently" not in ROUTINE and "Never skip a REGEN silently" not in ROUTINE
    assert ROUTINE.count("stays NOT DONE until it is uploaded") == 2
    # step 6, the one a session follows to clear NOT DONE: skip (refused for a LINK/REGEN) is for backups
    step6 = ROUTINE[ROUTINE.index("If `check` prints NOT DONE"):ROUTINE.index("## 7.")]
    assert "routine link-fail" in step6 and "routine regen-fail" in step6 and "never skipped" in step6
    assert "(write + upload, or skip with a reason)" not in step6
