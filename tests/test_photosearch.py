"""Subject photo search: full-name matching, sources, larger news images."""
from bore import photosearch as P


def test_full_name_matching():
    assert P.name_match("Will Campbell", "Patriots LT Will Campbell expected to miss time")
    assert not P.name_match("Will Campbell", "Campbell's soup can by Warhol")
    assert not P.name_match("Lew Smith", "Gen. Lew Wallace telegram")
    assert P.name_match("De'Zhaun Stribling", "https://49ers.com/team/players-roster/dezhaun-stribling/")
    assert P.name_match("Marvin Harrison Jr.", "Marvin Harrison catches a pass")      # Jr. is optional
    assert P.name_tokens("Marvin Harrison Jr.") == ["marvin", "harrison"]


def test_larger_versions_of_news_images():
    assert P.og_upgrade("https://site.com/wp-content/uploads/2026/10/photo-1200x675.jpg")[0] == \
        "https://site.com/wp-content/uploads/2026/10/photo.jpg"
    assert P.og_upgrade("https://static.www.nfl.com/image/upload/t_headshot_desktop/league/abc")[0] == \
        "https://static.www.nfl.com/image/upload/league/abc"
    assert P.og_upgrade("https://x.com/a.jpg?w=1200&h=675")[-1] == "https://x.com/a.jpg?w=1200&h=675"   # original last


def test_duckduckgo_results_filtered(monkeypatch):
    class R:
        def __init__(self, text="", js=None, code=200):
            self.text, self._js, self.status_code = text, js, code

        def json(self):
            return self._js
    calls = []

    class S:
        headers = {}

        def get(self, url, **k):
            calls.append(url)
            if url.endswith("i.js"):
                return R(js={"results": [
                    {"image": "https://heavy.com/a.jpg", "url": "https://heavy.com/will-campbell", "title": "Will Campbell knee",
                     "width": 2400, "height": 1350},
                    {"image": "https://www.alamy.com/b.jpg", "url": "https://www.alamy.com/x", "title": "Will Campbell", "width": 3000, "height": 2000},
                    {"image": "https://a.com/c.jpg", "url": "https://a.com/c", "title": "Will Campbell", "width": 400, "height": 300},
                    {"image": "https://a.com/d.jpg", "url": "https://a.com/soup", "title": "Campbell soup", "width": 2000, "height": 2000}]})
            return R(text="vqd='4-123'")
    monkeypatch.setattr(P.requests, "Session", lambda: S())
    monkeypatch.setattr(P.time, "sleep", lambda s: None)
    got, rep = P.ddg_images("Will Campbell Patriots", "Will Campbell", 600)
    assert [p.url for p in got] == ["https://heavy.com/a.jpg"]
    assert rep["dropped"] == {"small": 1, "other_subject": 1, "stock_or_shop": 1}


def test_bing_junk_is_reported_not_used(monkeypatch):
    import html
    import json

    class R:
        status_code = 200
        text = " ".join(f' m="{html.escape(json.dumps({"murl": f"https://x.com/{i}.jpg", "purl": "https://x.com/p", "t": "Hamburger Helper"}))}"'
                        for i in range(10))
    monkeypatch.setattr(P.requests, "get", lambda *a, **k: R)
    got, rep = P.bing_images("Carson Schwesinger Browns", "Carson Schwesinger")
    assert got == [] and "unrelated" in rep["error"]


def test_name_is_a_phrase():
    assert not P.name_match("Will Campbell", "Campbell will start at LT")
    assert P.name_match("Amon-Ra St. Brown", "Amon-Ra St. Brown scores twice")
    assert P.name_match("Josh Allen", "Josh A. Allen of the Bills")                    # a middle initial
    assert P.name_match("J.J. McCarthy", "https://site.com/jj-mccarthy-vikings.jpg")
    assert P.name_match("monarch butterfly", "Monarch butterflies migrate south")       # a plural


def test_old_surname_must_means_the_name_in_the_query():
    assert P.subject_name("Will Campbell Patriots", "Campbell") == "will campbell"
    assert P.subject_name("Will Campbell", "") == "Will Campbell"
    assert P.subject_name("monarch butterfly", "monarch butterfly") == "monarch butterfly"
    assert P.subject_name("Taylor Swift Travis Kelce", "Travis Kelce") == "Travis Kelce"


def test_overlays_are_never_stripped():
    u = "https://res.cloudinary.com/x/image/upload/t_watermark_logo/abc.jpg"
    assert P.og_upgrade(u) == [u]
    u = "https://img.site.com/a.jpg?w=1200&mark=logo.png"
    assert P.og_upgrade(u) == [u]
    assert P.unstamp("https://static.www.nfl.com/image/upload/t_headshot_desktop/league/abc") == \
        "https://static.www.nfl.com/image/upload/league/abc"


def test_a_cutout_lands_on_grey_not_black():
    from PIL import Image

    from bore.media import flatten
    im = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    assert flatten(im).getpixel((5, 5)) == (128, 128, 128)


def test_search_reports_sources_that_run_out_of_time(monkeypatch):
    import time as _t
    monkeypatch.setattr(P, "subject_news_photos", lambda name: ([], P._rep("news", name, _t.time())))
    monkeypatch.setattr(P, "ddg_images", lambda q, name, floor: (_t.sleep(3), ([], {}))[1])
    monkeypatch.setattr(P, "bing_images", lambda q, name: ([], P._rep("bing", q, _t.time(), error="unrelated results")))
    t0 = _t.time()
    got, report = P.search_subject("Will Campbell", commons=False, deadline_s=1)
    assert got == [] and _t.time() - t0 < 2.5
    assert {r["source"]: r["error"] for r in report}["duckduckgo"] == "timed out"
