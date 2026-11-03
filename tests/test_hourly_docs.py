"""The hourly routine reads the page's caption-tone and visual-reference documents, as Studio does
(Oct 31: they were uploaded for a page and only Studio's briefs listed them)."""
import json

from bore import routine as R
from bore import studio as S


def test_the_hourly_brief_lists_the_pages_tone_and_visual_documents(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "ROOT", tmp_path)
    docs = tmp_path / "private" / "docs" / "demo"
    docs.mkdir(parents=True)
    (docs / "tone.txt").write_text("Short sentences. A number in every claim.")
    (docs / "visual.txt").write_text("Dark backgrounds, team colours.")
    (docs / "templates.txt").write_text("fifteen formats")
    (docs / "empty.txt").write_text("")
    meta = {"demo": {"docs": {t: {"version": 2, "file": f"private/docs/demo/{t}.txt"} for t in ("tone", "visual", "templates")}},
            "other": {"docs": {"tone": {"version": 1, "file": "private/docs/other/tone.txt"}}}}   # not synced here
    monkeypatch.setattr(S, "studio_meta", lambda: meta)
    assert R.page_docs("demo") == {"tone": {"file": "private/docs/demo/tone.txt", "version": 2},
                                   "visual": {"file": "private/docs/demo/visual.txt", "version": 2}}
    assert R.page_docs("other") == {}                     # a listed file that is missing is left out
    meta["demo"]["docs"]["tone"]["file"] = "private/docs/demo/empty.txt"
    assert list(R.page_docs("demo")) == ["visual"]         # an image-only document (no text) too
    assert R.page_docs("nobody") == {}
    json.dumps(R.page_docs("demo"))                       # goes into brief.json as is
