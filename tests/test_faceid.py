"""Whose face is in a photo (bore/faceid.py): the rules, the reference face, the ranking in the
searches and feeds, and what editors and the routine see. No network: features are synthetic or
come from the repository's own sample images."""
import io
import random
import threading
from pathlib import Path

import numpy as np
import pytest

from bore import faceid, guard, studio
from bore import photosearch as P
from bore.faceid import reference as real_reference
from bore.media import Photo

ROOT = Path(__file__).resolve().parent.parent


def vec(i: int) -> np.ndarray:
    v = np.zeros(128, "float32")
    v[i] = 1.0
    return v


SUBJECT, OTHER, THIRD = vec(0), vec(1), vec(2)


def face(f, h=120.0, q=10.0):
    return (h, q, f)


def jpeg(seed: int) -> bytes:
    from PIL import Image
    rnd = random.Random(seed)
    im = Image.new("L", (90, 80))
    im.putdata([rnd.randrange(256) for _ in range(90 * 80)])
    buf = io.BytesIO()
    im.convert("RGB").resize((1600, 1200)).save(buf, "JPEG")
    return buf.getvalue()


def photo(i: int, credit: str = "") -> Photo:
    return Photo(f"https://img.example.com/{i}.jpg", "search", credit or f"site{i}.example.com", 1600, 1200, jpeg(i),
                 post_url=f"https://site{i}.example.com/a")


def fake_faces(monkeypatch, by_url: dict, ref=SUBJECT, how="wikipedia", reference=None):
    """faces() by photo (keyed by its data) and the reference face, no model or network needed."""
    table = {}

    def faces(data):
        return table.get(data, [])
    monkeypatch.setattr(faceid, "faces", faces)
    monkeypatch.setattr(faceid, "available", lambda: True)
    monkeypatch.setattr(faceid, "reference", reference or (lambda name, sports=False, player=None, timeout=6: (ref, how)))
    return lambda photos: table.update({ph.data: by_url.get(ph.url, []) for ph in photos})


# ------------------------------------------------------------------ the rules
def test_match_mismatch_and_neutral():
    assert faceid.judge(SUBJECT, [face(SUBJECT)])[0] == "match"
    assert faceid.judge(SUBJECT, [face(OTHER), face(SUBJECT * 0.6 + OTHER * 0.8)])[0] == "match"   # cos 0.6
    # a clear face of someone else: may not be them
    m, s = faceid.judge(SUBJECT, [face(OTHER)])
    assert m == "mismatch" and s == pytest.approx(0.0)
    # a helmet / profile (low quality), a tiny face, or a score in between: can't tell
    assert faceid.judge(SUBJECT, [face(OTHER, q=6.0)])[0] == ""
    assert faceid.judge(SUBJECT, [face(OTHER, h=30)])[0] == ""
    assert faceid.judge(SUBJECT, [face(SUBJECT * 0.3 + OTHER * 0.954)])[0] == ""
    # the press-conference photo of a teammate (a clear face at 0.23) may not be them, unless a
    # blurred face in it scores as high (that one may be the subject)
    teammate = face(SUBJECT * 0.23 + OTHER * 0.973)
    assert faceid.judge(SUBJECT, [teammate])[0] == "mismatch"
    assert faceid.judge(SUBJECT, [teammate, face(SUBJECT * 0.22 + THIRD * 0.975, q=6.0)])[0] == ""
    # no face, or no reference: neutral, and no score
    assert faceid.judge(SUBJECT, []) == ("", None)
    assert faceid.judge(None, [face(OTHER)]) == ("", None)
    # a match needs a face that is not too blurred to tell (grainy faces look alike)
    assert faceid.judge(SUBJECT, [face(SUBJECT, q=5.0)])[0] == ""


# ------------------------------------------------------------------ labelling and ranking
def test_label_sets_fields_and_never_drops(monkeypatch):
    photos = [photo(i) for i in range(4)]
    feed = fake_faces(monkeypatch, {photos[0].url: [face(OTHER)], photos[1].url: [face(SUBJECT)],
                                    photos[2].url: [face(OTHER, q=6)], photos[3].url: []})
    feed(photos)
    rep = faceid.label(photos, "Jane Example")
    assert [p.id_match for p in photos] == ["mismatch", "match", "", ""]
    assert photos[1].id_name == "Jane Example" and photos[1].id_ref == "wikipedia" and photos[1].id_score == 1.0
    assert photos[3].id_name == ""                       # no face: nothing compared, nothing shown
    assert (rep["match"], rep["mismatch"], rep["neutral"], rep["reference"]) == (1, 1, 2, "wikipedia")
    assert [p.url for p in sorted(photos, key=faceid.id_rank)] == [photos[i].url for i in (1, 2, 3, 0)]
    assert faceid.fields(photos[0]) == {"id_match": "mismatch", "id_score": 0.0, "id_name": "Jane Example",
                                        "id_ref": "wikipedia"}
    assert faceid.fields(photos[3]) == {}


def test_no_portrait_labels_nothing_even_when_one_face_dominates(monkeypatch):
    # a search for someone with no ESPN or Wikipedia photo that returns mostly their partner: the
    # most common face is the partner's, so it must not become the reference (it would mark the
    # partner's photos 'matches' and the subject's own 'may not be')
    photos = [photo(i) for i in range(8)]
    by = {**{photos[i].url: [face(OTHER)] for i in range(5)}, photos[5].url: [face(SUBJECT), face(OTHER)],
          photos[6].url: [face(SUBJECT)], photos[7].url: [face(SUBJECT)]}
    fake_faces(monkeypatch, by, ref=None, how="")(photos)
    rep = faceid.label(photos, "Jane Example")
    assert rep["reference"] == "none" and (rep["match"], rep["mismatch"], rep["neutral"]) == (0, 0, 8)
    assert not any(p.id_match or p.id_name for p in photos)
    assert [p.url for p in sorted(photos, key=faceid.id_rank)] == [p.url for p in photos]       # order kept


def test_names_are_looked_up_without_nicknames_or_possessives():
    from bore.media import subject_of
    assert faceid.person_name("Sean ‘Diddy’ Combs") == "Sean Combs"
    assert faceid.person_name("Davante Adams’") == "Davante Adams"
    assert faceid.person_name("Frankie Example’s") == "Frankie Example"
    assert faceid.person_name("D'Andre O'Example") == "D'Andre O'Example"
    assert subject_of([{"title": "Davante Adams’ 87th red zone catch", "text": "Davante Adams scores"}]) == "Davante Adams"


def test_no_model_labels_nothing_and_fails_nothing(monkeypatch):
    monkeypatch.setattr(faceid, "MODEL", ROOT / "assets" / "models" / "missing.onnx")
    photos = [photo(1)]
    rep = faceid.label(photos, "Jane Example")
    assert rep["error"] == "no face model" and photos[0].id_match == "" and photos[0].id_name == ""
    assert "not compared" in faceid.describe(rep)


def search_with(monkeypatch, photos, by_url, sports=False, player=None, reference=None):
    import time as _t
    monkeypatch.setattr(P, "subject_news_photos", lambda name: ([], P._rep("news", name, _t.time())))
    monkeypatch.setattr(P, "ddg_images", lambda q, name, floor: (list(photos), P._rep("duckduckgo", q, _t.time(), 4, 4)))
    monkeypatch.setattr(P, "bing_images", lambda q, name: ([], P._rep("bing", q, _t.time())))
    monkeypatch.setattr(P, "nfl_player", lambda name: player or {})
    import bore.consensus
    import bore.media
    monkeypatch.setattr(bore.media, "graphic", lambda ph, sports=True: "")
    monkeypatch.setattr(bore.consensus, "_seam", lambda im: 0.0)
    fake_faces(monkeypatch, by_url, reference=reference)(photos)
    return P.search_subject("Jane Example", commons=False, official=False, deadline_s=5, sports=sports)


def test_search_ranks_the_subjects_face_first_and_keeps_every_photo(monkeypatch):
    photos = [photo(i) for i in range(4)]
    got, report = search_with(monkeypatch, photos, {photos[0].url: [face(OTHER)], photos[1].url: [face(OTHER, q=6)],
                                                    photos[2].url: [face(SUBJECT)], photos[3].url: []})
    assert [p.url for p in got] == [photos[i].url for i in (2, 1, 3, 0)]
    assert [p.id_match for p in got] == ["match", "", "", "mismatch"]
    faces_rep = next(r for r in report if r.get("source") == "faces")
    assert (faces_rep["match"], faces_rep["mismatch"], faces_rep["neutral"]) == (1, 1, 2)
    P.print_report(report)     # prints the faces line


def test_espn_headshot_is_only_the_reference(monkeypatch):
    seen = {}

    def reference(name, sports=False, player=None, timeout=6):
        seen.update(name=name, sports=sports, player=player)
        return SUBJECT, "espn"
    photos = [photo(i) for i in range(2)]
    player = {"name": "Jane Example", "team": "Example Team", "headshot": "https://img.example.com/headshot.png"}
    got, _ = search_with(monkeypatch, photos, {photos[0].url: [face(SUBJECT)]}, sports=True, player=player,
                         reference=reference)
    assert seen == {"name": "Jane Example", "sports": True, "player": player}     # one ESPN lookup, shared
    assert "https://img.example.com/headshot.png" not in [p.url for p in got]
    assert got[0].id_ref == "espn"


# ------------------------------------------------------------------ the reference face
def test_reference_prefers_espn_then_wikipedia_and_is_cached(monkeypatch):
    monkeypatch.setattr(faceid, "reference", real_reference)
    monkeypatch.setattr(faceid, "_REFS", {})
    calls = []
    monkeypatch.setattr(faceid, "_download", lambda url, timeout: calls.append(url) or url.encode())
    monkeypatch.setattr(faceid, "_portrait", lambda data: SUBJECT if b"headshot" in data or b"wiki" in data else None)
    monkeypatch.setattr(faceid, "wikipedia_image", lambda name, timeout=6: ("https://upload.example.org/wiki.jpg", True))
    pl = {"name": "Jane Example", "team": "Example Team", "headshot": "https://img.example.com/headshot.png"}
    f, how = faceid.reference("Jane Example", sports=True, player=pl)
    assert how == "espn" and float(f @ SUBJECT) == 1.0
    assert faceid.reference("jane  example", sports=True)[1] == "espn" and len(calls) == 1      # cached per name
    # not a player (or no headshot): the Wikipedia lead image
    assert faceid.reference("Jane Example", sports=True, player={})[1] == "espn"                # still the cached one
    assert faceid.reference("John Example", sports=True, player={})[1] == "wikipedia"
    assert faceid.reference("Joan Example")[1] == "wikipedia"


def test_a_network_failure_is_not_cached(monkeypatch):
    monkeypatch.setattr(faceid, "reference", real_reference)
    monkeypatch.setattr(faceid, "_REFS", {})
    tries = []

    def down(name, timeout=6):
        tries.append(name)
        raise OSError("network down")
    monkeypatch.setattr(faceid, "wikipedia_image", down)
    assert faceid.reference("Jane Example") == (None, "")
    faceid.reference("Jane Example")
    assert len(tries) == 2


def test_a_busy_image_server_is_not_remembered_as_no_reference(monkeypatch):
    import requests
    monkeypatch.setattr(faceid, "reference", real_reference)
    monkeypatch.setattr(faceid, "_REFS", {})
    monkeypatch.setattr(faceid, "wikipedia_image", lambda name, timeout=6: ("https://upload.wikimedia.org/j.jpg", True))
    monkeypatch.setattr(faceid, "_portrait", lambda data: SUBJECT)
    status, agents = [429], []

    def get(url, headers=None, timeout=None):
        agents.append(headers["User-Agent"])
        return type("R", (), {"status_code": status[0], "content": b"x" * 2000})()
    monkeypatch.setattr(requests, "get", get)
    assert faceid.reference("Jane Example") == (None, "")
    status[0] = 200                                     # the server recovered: asked again, not served from the cache
    assert faceid.reference("Jane Example")[1] == "wikipedia"
    assert all(a.startswith("BorePostStudio") for a in agents)      # Wikimedia's own user agent for its images
    # a clean answer that has no image is remembered (no lookup again for the rest of the run)
    status[0] = 404
    assert faceid.reference("Joan Example") == (None, "") and faceid.reference("Joan Example") == (None, "")
    assert len(agents) == 3


def test_an_espn_search_that_failed_is_asked_again(monkeypatch):
    import requests
    monkeypatch.setattr(faceid, "reference", real_reference)
    monkeypatch.setattr(faceid, "_REFS", {})
    monkeypatch.setattr(faceid, "wikipedia_image", lambda name, timeout=6: ("", None))
    asked = []

    def get(url, **k):
        asked.append(url)
        raise requests.ConnectionError("down")
    monkeypatch.setattr(requests, "get", get)
    assert P.nfl_player("Jane Example") == {}                       # callers that only want the team: as before
    with pytest.raises(requests.ConnectionError):
        P.nfl_player("Jane Example", strict=True)
    faceid.reference("Jane Example", sports=True)
    faceid.reference("Jane Example", sports=True)
    assert len(asked) == 4                                          # not cached as 'not a player'


def test_wikipedia_lead_image_only_for_a_person(monkeypatch):
    import requests

    def answer(page):
        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"query": {"pages": [page]}}
        return lambda *a, **k: R()
    thumb = "https://upload.example.org/thumb/a/ab/Jane.jpg/960px-Jane.jpg?utm_source=x"
    monkeypatch.setattr(requests, "get", answer({"title": "Jane Example", "description": "American singer (born 1990)",
                                                  "thumbnail": {"source": thumb, "width": 960}}))
    assert faceid.wikipedia_image("Jane Example") == (thumb, True)                 # the URL exactly as given
    monkeypatch.setattr(requests, "get", answer({"title": "Jane Example", "description": "Topics referred to by the same term",
                                                  "pageprops": {"disambiguation": ""}}))
    assert faceid.wikipedia_image("Jane Example") == ("", None)
    monkeypatch.setattr(requests, "get", answer({"title": "Example City", "description": "City in Example County",
                                                  "thumbnail": {"source": thumb}}))
    assert faceid.wikipedia_image("Example City") == ("", False)
    monkeypatch.setattr(requests, "get", answer({"title": "Nobody Example", "missing": True}))
    assert faceid.wikipedia_image("Nobody Example") == ("", None)


def test_wikipedia_rate_limit_falls_back_to_the_page_summary(monkeypatch):
    import requests
    monkeypatch.setattr(faceid, "_WIKI", {"api_rests_until": 0.0})
    asked = []
    thumb = "https://upload.example.org/thumb/a/ab/Jane.jpg/330px-Jane.jpg?utm_source=x"

    def get(url, **k):
        asked.append(url.split("/")[3] if "/api/rest_v1/" not in url else "summary")

        class R:
            status_code = 429 if "api.php" in url else 200
            headers = {"Retry-After": "8"}

            def raise_for_status(self):
                pass

            def json(self):
                return {"type": "standard", "description": "American television personality (born 1980)",
                        "thumbnail": {"source": thumb, "width": 330}}
        return R()
    monkeypatch.setattr(requests, "get", get)
    assert faceid.wikipedia_image("Jane Example") == (thumb, True)
    assert faceid.wikipedia_image("Joan Example") == (thumb, True)
    assert asked == ["w", "summary", "summary"]           # the query API rests after a 429


# ------------------------------------------------------------------ detector rows and threads
def test_detector_rows_per_thread_and_old_shape(monkeypatch):
    import cv2
    made = []

    class Det:
        def setInputSize(self, size):
            self.size = size

        def setScoreThreshold(self, s):
            pass

        def detect(self, im):
            return 1, np.array([[10, 20, 30, 40, *range(10), 0.93]], "float32")

    def create(*a):
        made.append(threading.get_ident())
        return Det()
    monkeypatch.setattr(cv2, "FaceDetectorYN", type("Y", (), {"create": staticmethod(create)}))
    out = {}

    def run(k):
        out[k] = (guard.detect_face_rows(np.zeros((50, 60, 3), "uint8")), guard.detect_faces(np.zeros((50, 60, 3), "uint8")))
    ts = [threading.Thread(target=run, args=(k,)) for k in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(made) == 2                       # 4 calls on 2 threads: one detector per thread, reused within it
    rows, old = out[0]
    assert len(rows[0]) == 15 and old == [(10.0, 20.0, 30.0, 40.0, pytest.approx(0.93))]


@pytest.mark.skipif(not faceid.available(), reason="face model not present")
def test_real_model_matches_a_face_with_itself_not_another_person():
    from PIL import Image, ImageOps
    card = (ROOT / "docs/samples/culture-rush-taylor-travis.jpg").read_bytes()
    fs = faceid.faces(card)
    assert len(fs) == 2 and fs[0][0] > fs[1][0]
    ref = fs[1][2]                                                 # the smaller face on the card
    buf = io.BytesIO()
    ImageOps.mirror(Image.open(io.BytesIO(card)).convert("RGB")).save(buf, "JPEG", quality=90)
    assert faceid.judge(ref, faceid.faces(buf.getvalue()))[0] == "match"
    other = faceid.faces((ROOT / "docs/samples/huddle-giants-qb-template.jpg").read_bytes())
    m, s = faceid.judge(ref, other)
    assert m != "match" and s < faceid.MATCH


# ------------------------------------------------------------------ what editors and the routine see
def test_image_out_and_studio_rank_carry_the_face(monkeypatch):
    p = photo(1)
    p.id_match, p.id_score, p.id_name, p.id_ref = "mismatch", 0.0412, "Jane Example", "espn"
    out = studio.image_out(p)
    assert out["id_match"] == "mismatch" and out["id_score"] == 0.04 and out["id_name"] == "Jane Example"
    members = [{"url": p.post_url, "text": "Jane Example signs"}]
    shared = Photo("u4", "news", "example.org", 1200, 800, consensus=3)
    plain = Photo("u5", "x", "@a", 1600, 1200, post_url="https://x.example.com/2")
    matched = Photo("u6", "x", "@b", 1600, 1200, post_url="https://x.example.com/3", id_match="match", id_name="Jane Example")
    ranked = sorted([p, plain, shared, matched], key=lambda x: studio._image_rank(x, "Jane Example", members, set()))
    # the outlets' picture, the subject's face, then unknown ones (a 'may not be' face goes last only
    # after the cut to six, so it never costs a photo its place: see the feed test below)
    assert [x.url for x in ranked] == ["u4", "u6", p.url, "u5"]


def test_lint_warns_when_the_chosen_face_may_be_someone_else():
    brief = {"page": "huddle", "posts": [{"text": "Jane Example signs a deal"}], "verification": {"route": "corroborated"},
             "photos": [{"size": "1600x1200", "id_match": "mismatch", "id_name": "Jane Example", "id_score": 0.04}],
             "playbook": {}}
    copy = {"niche_fit": 9, "template": "report", "headline_lines": ["JANE EXAMPLE", "SIGNS"], "photo_index": 0,
            "people_in_photo": ["Jane Example"], "caption": "Jane Example signs.\n\nGood move? 👇\n\n#Example"}
    from bore.guard import lint
    assert any("may not be Jane Example" in w for w in lint(brief, copy)["warnings"])
    brief["photos"][0]["id_match"] = "match"
    assert not any("may not be" in w for w in lint(brief, copy)["warnings"])
    brief["photos"][0]["id_match"] = "mismatch"
    brief["verification"]["route"] = "studio"            # the editor's chosen photo is theirs to judge
    assert not any("may not be" in w for w in lint(brief, copy)["warnings"])


# ------------------------------------------------------------------ the Studio feed and the hourly briefs
def feed_trend():
    from types import SimpleNamespace as NS
    members = [{"platform": "x", "url": "https://x.com/a/status/1", "engagement": 5, "created_at": 1_800_000_000,
                "text": "Jane Example signs with the Example Team"},
               {"platform": "x", "url": "https://x.com/b/status/2", "engagement": 3, "created_at": 1_800_000_000,
                "text": "Jane Example deal done"}]
    return NS(key="k1", headline="Jane Example signs", cluster=NS(members=members))


def test_feed_searches_by_name_when_every_photo_may_be_someone_else(monkeypatch):
    import bore.media
    import bore.photosearch
    a, b = photo(1, "@a"), photo(2, "@b")
    a.post_url, b.post_url = "https://x.com/a/status/1", "https://x.com/b/status/2"
    found = photo(3)
    fake_faces(monkeypatch, {a.url: [face(OTHER)], b.url: [face(OTHER)], found.url: [face(SUBJECT)]})([a, b, found])
    monkeypatch.setattr(bore.media, "story_photos", lambda members, **k: [a, b])
    monkeypatch.setattr(bore.media, "graphic", lambda ph, sports=True: "")
    searched = []

    def search(query, name, **k):
        searched.append(name)
        faceid.label([found], name)                     # the real search labels what it returns
        return [found], []
    monkeypatch.setattr(bore.photosearch, "search_subject", search)
    out = studio._feed_images(feed_trend(), "never", [], same=[], searches={"left": 1})
    assert searched == ["Jane Example"]
    assert [i["url"] for i in out] == [found.url, a.url, b.url]          # nothing dropped, the subject first
    assert [i.get("id_match") for i in out] == ["match", "mismatch", "mismatch"]


def test_hourly_photos_put_may_not_be_last_within_the_run_time(monkeypatch):
    import time as _t

    from bore import cli
    photos = [photo(i) for i in range(3)]
    fake_faces(monkeypatch, {photos[0].url: [face(OTHER)], photos[1].url: [], photos[2].url: [face(SUBJECT)]})(photos)
    monkeypatch.setitem(cli.IMAGE_DEADLINE, "at", 0.0)
    order = list(photos)
    cli._faces(order, "Jane Example", "never")
    assert [p.url for p in order] == [photos[i].url for i in (1, 2, 0)]     # tiers kept, 'may not be' last
    monkeypatch.setitem(cli.IMAGE_DEADLINE, "at", _t.time() - 1)              # the run's image time is used up
    fresh = [photo(i) for i in range(3)]
    cli._faces(fresh, "Jane Example", "never")
    assert not any(p.id_name for p in fresh)


def test_feed_keeps_a_may_not_be_photo_among_the_six_and_puts_it_last(monkeypatch):
    import bore.media
    photos = [photo(i, f"@u{i}") for i in range(7)]
    for i, p in enumerate(photos):
        p.source, p.post_url = "x", f"https://x.com/u{i}/status/{i}"
    fake_faces(monkeypatch, {photos[0].url: [face(OTHER)]})(photos)
    monkeypatch.setattr(bore.media, "story_photos", lambda members, **k: list(photos))
    monkeypatch.setattr(bore.media, "graphic", lambda ph, sports=True: "")
    out = studio._feed_images(feed_trend(), "never", [], same=[])
    # the six the checks chose: a face that may be someone else's moves last, it is not dropped
    assert [i["url"] for i in out] == [photos[i].url for i in (1, 2, 3, 4, 5, 0)]
    assert out[-1]["id_match"] == "mismatch"


def test_hourly_story_keeps_a_flagged_top_tier_photo(monkeypatch):
    import bore.media
    import bore.stills
    from bore import cli
    stills = [photo(i) for i in range(6)]
    for i, p in enumerate(stills):
        p.ref_match = 0.9 if i < 3 else 0.0           # three stills match the outlets' picture
    same, own = [photo(10 + i) for i in range(3)], [photo(20 + i) for i in range(3)]
    fake_faces(monkeypatch, {stills[0].url: [face(OTHER)]})(stills + same + own)
    monkeypatch.setattr(bore.media, "story_photos", lambda members, **k: list(own))
    monkeypatch.setattr(bore.media, "best", lambda photos, floor, limit=3, **k: list(photos)[:limit])
    monkeypatch.setattr(bore.media, "graphic", lambda ph, sports=True: "")
    monkeypatch.setattr(bore.stills, "video_stills", lambda *a, **k: list(stills))
    monkeypatch.setitem(cli.IMAGE_DEADLINE, "at", 0.0)
    cfg = {"posts": {"min_photo_short_side": 600, "allow_video_frames": True, "video_stills": {}}}
    out = cli._story_images(feed_trend().cluster.members, "prefer", cfg, same=same)
    # the first six by tier (matched stills, then the outlets' picture); the flagged still moves last
    assert [p.url for p in out] == [p.url for p in stills[1:3] + same + stills[:1]]


def test_feed_story_checked_before_face_matching_only_gets_faces(monkeypatch):
    import bore.media
    a, b, c = photo(1), photo(2), photo(3)
    fake_faces(monkeypatch, {a.url: [face(OTHER)], b.url: [face(SUBJECT)]})([a, b, c])
    data = {p.url: p.data for p in (a, b)}                       # c no longer downloads

    def fetch(ph, timeout=20):
        ph.data = data.get(ph.url)
        return ph if ph.data else None
    monkeypatch.setattr(bore.media, "fetch", fetch)
    stored = [{"url": p.url, "source": "x", "credit": "@a", "flags": ["small: may look soft on a card"]} for p in (a, b, c)]
    out = studio._feed_faces(feed_trend(), "never", stored)
    assert [i["url"] for i in out] == [b.url, c.url, a.url]                 # its checks kept, 'may not be' last
    assert [i.get("id_match") for i in out] == ["match", None, "mismatch"]
    assert all(i["flags"] == ["small: may look soft on a card"] for i in out)
