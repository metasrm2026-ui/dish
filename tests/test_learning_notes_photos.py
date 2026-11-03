"""Learning from the editors' own words (notes) and from the photos they keep: notes quoted to the
routine and grouped into rules, photo lessons as rules and as a tie-breaking weight on candidates."""
import json

from bore import lessons as L
from bore.media import Photo

NOW = 1_791_850_000.0
DAY = 86400
NFL = {"niche": "NFL"}


def rv(verdict, headline, page="huddle", days_ago=1, **kw):
    return {"verdict": verdict, "page": page, "headline": headline, "decided_at": (NOW - days_ago * DAY) * 1000, **kw}


# ------------------------------------------------------------------ notes
def test_notes_are_quoted_newest_first_without_repeats_old_ones_fade():
    reviews = [rv("rejected", "A LIMITED IN PRACTICE", reason="Weak story", note="practice updates are too minor", days_ago=3),
               rv("rejected", "B LIMITED IN PRACTICE", reason="Weak story", note="Practice updates are  too minor!", days_ago=1),
               rv("rejected", "C SIGNS", reason="Weak story", note="nobody follows this team", days_ago=2),
               rv("rejected", "D SIGNS", reason="Weak story", note="from two months ago", days_ago=60),       # faded
               rv("withdrawn", "E SIGNS", note="SocialPilot was locked"),                                      # not about the post
               rv("rejected", "F SIGNS", page="culture-rush", reason="Weak story", note="another page's note"),
               rv("feedback", "G SIGNS", reason="Redo", note="make the photo bigger", days_ago=0.5)]
    rec = L.build("huddle", reviews, NFL, NOW)
    notes = rec["notes"]
    assert [n["text"] for n in notes] == ["make the photo bigger", "Practice updates are too minor!",
                                          "nobody follows this team"]
    assert notes[1]["times"] == 2 and notes[1]["kind"] == "injury_practice"
    assert notes[0]["verdict"] == "feedback" and notes[0]["themes"] == ["photo"]
    many = [rv("rejected", f"P{i} SIGNS", reason="Weak story", note=f"note number {i} about {w}", days_ago=i / 10)
            for i, w in enumerate("abcdefghijklmnopqrstuvwxyz")]
    big = L.build("huddle", many, NFL, NOW)
    assert len(big["notes"]) == L.NOTE_KEEP and big["notes"][0]["text"].startswith("note number 0")
    assert len(L.for_brief(big, "transaction")["notes"]) == L.NOTE_BRIEF


def test_a_note_follows_its_kind_and_template_into_the_brief():
    reviews = [rv("rejected", "JEANTY LIMITED IN PRACTICE", reason="Image quality", note="wrong player in the photo",
                  template="report"),
               rv("rejected", "WHO IS BETTER?", reason="Weak story", note="debates need a close photo", template="topic",
                  kind_rank="other")]
    older = [rv("rejected", f"N{i} SIGNS", reason="Weak story", note=f"newer note {i} on signings", kind_rank="transaction",
                days_ago=i / 100) for i in range(L.NOTE_BRIEF)]          # push both out of the newest 8
    rec = L.build("huddle", reviews + older, NFL, NOW)
    part = L.for_brief(rec, "injury_practice")
    assert part["this_kind"]["notes"] and part["this_kind"]["notes"][0].startswith('"wrong player in the photo" (Image quality')
    assert "'report' card" in part["this_kind"]["notes"][0]
    assert L.for_brief(rec, "transaction")["this_kind"]["notes"] == []
    assert part["template_notes"] == {"topic": [part["template_notes"]["topic"][0]]}     # the kind's note isn't repeated
    assert part["template_notes"]["topic"][0].startswith('"debates need a close photo"')
    assert len(part["notes"]) == L.NOTE_BRIEF
    # while they are among the newest, they are quoted there only, with their kind and template
    recent = L.for_brief(L.build("huddle", reviews, NFL, NOW), "injury_practice")
    assert len(recent["notes"]) == 2 and recent["this_kind"]["notes"] == [] and recent["template_notes"] == {}
    assert "'report' card" in recent["notes"][0] and "Image quality" in recent["notes"][0]


def test_a_brief_quotes_each_note_once_within_a_cap():
    many = [rv("rejected", f"P{i} SIGNS", reason="Weak story", note=f"note {i} " + "x" * 280, template=f"t{i % 10}",
               kind_rank="transaction" if i % 2 else "other", days_ago=i / 10) for i in range(30)]
    rec = L.build("huddle", many, NFL, NOW)
    part = L.for_brief(rec, "transaction")
    quoted = part["notes"] + part["this_kind"]["notes"] + [q for qs in part["template_notes"].values() for q in qs]
    assert len(quoted) == len(set(quoted)) <= L.NOTE_BRIEF + L.NOTE_PER + L.NOTE_TPL
    assert sum(map(len, part["template_notes"].values())) <= L.NOTE_TPL
    assert sum(map(len, quoted)) < 8000          # was 20k+ when every template carried its own copies


def test_recurring_note_themes_become_rules():
    reviews = [rv("rejected", "A SIGNS", reason="Image quality", note="blurry photo"),
               rv("rejected", "B SIGNS", reason="Image quality", note="wrong player pictured", days_ago=2),
               rv("rejected", "C SIGNS", reason="Caption off-voice", note="too many emojis in the caption")]
    rec = L.build("huddle", reviews, NFL, NOW)
    assert [t["id"] for t in rec["note_themes"]] == ["photo"] and rec["note_themes"][0]["count"] == 2
    rule = next(r for r in rec["rules"] if r.startswith("EDITORS' NOTES"))
    assert "about the photo" in rule and '"blurry photo"' in rule
    assert not any("caption" in r for r in rec["rules"])             # one note is not a theme yet
    assert L.themes_of("wrong player in the photo") == ["photo"]      # not "facts"
    assert "facts" in L.themes_of("wrong date, he signed in May")
    assert "timing" not in L.themes_of("the 25-year-old rookie") and "timing" in L.themes_of("old news by now")


def test_notes_about_the_photo_or_caption_never_teach_which_stories_to_skip():
    assert L.themes_of("weak photo", "Image quality") == ["photo"]
    assert L.themes_of("boring pic", "Redo") == ["photo"]                    # a redo about the picture
    assert L.themes_of("meh pic, too dark", "Redo") == ["photo"]
    assert L.themes_of("old photo", "") == ["photo"]
    assert L.themes_of("boring story, and the pic is meh", "Redo") == ["photo", "minor"]   # names the story
    assert L.themes_of("nobody cares", "Image quality") == [] and L.themes_of("nobody cares", "Weak story") == ["minor"]
    assert L.themes_of("victim name is misspelled", "Caption off-voice") == ["text"]
    assert L.themes_of("misspelled name") == ["text"]
    assert L.themes_of("dead wrong headline", "Redo") == ["text", "facts"]
    assert L.themes_of("the victim was 19 and died at home", "Weak story") == []   # true crime names victims anyway
    assert L.themes_of("too soon, insensitive", "Weak story") == ["taste"] and L.themes_of("time-sensitive deal") == []
    reviews = [rv("rejected", "A KILLED", page="culture-rush", reason="Image quality", note="weak photo"),
               rv("rejected", "B KILLED", page="culture-rush", reason="Image quality", note="meh pic, too dark", days_ago=2),
               rv("rejected", "C KILLED", page="culture-rush", reason="Caption off-voice", note="victim's name is misspelled"),
               rv("rejected", "D KILLED", page="culture-rush", reason="Wrong / unverified facts", note="the victim was 34")]
    rec = L.build("culture-rush", reviews, {"niche": "celebrities"}, NOW)
    assert [t["id"] for t in rec["note_themes"]] == ["photo"]
    assert not any("minor stories" in r or "sensitive stories" in r for r in rec["rules"])
    assert rec["notes"][0]["themes"] == ["photo"]


def test_old_ledger_entries_and_old_records_still_work():
    bare = [{"verdict": "approved", "page": "huddle", "decided_at": (NOW - DAY) * 1000},
            {"verdict": "rejected", "page": "huddle", "reason": "Image quality"},
            {"verdict": "studio_pick", "page": "huddle", "headline": "X"}]
    rec = L.build("huddle", bare, NFL, NOW)
    assert rec["notes"] == [] and rec["photos"] == {} and rec["note_themes"] == []
    assert rec["other"]["studio_photo_picks"] == 0
    old = {"kinds": {"quote": {"label": "Quote", "approved": 3, "n": 4}}, "rules": ["PREFER Quote"]}   # built before notes
    part = L.for_brief(old, "quote")
    assert part["notes"] == [] and part["this_kind"]["notes"] == [] and part["template_notes"] == {}
    assert L.photo_weight(old, Photo("u", "x", "@a")) == 1.0


# ------------------------------------------------------------------ photos
FRAME = {"source": "x", "note": "video frame", "flags": ["soft (sharpness 30)"], "rank": 0, "of": 3}
LEAD = {"source": "web", "rank": 1, "of": 3}


def test_photo_features_carry_no_names():
    assert L.photo_features(FRAME) == ["flag:soft", "frame", "source:x"]
    ph = Photo("u", "news", "outlet.example.com", consensus=3, flags=["watermark: example", "file photo (uploaded 2024-05)",
                                                                     "not checked yet", "photo of Jane Doe (search)"])
    assert L.photo_features(ph) == ["consensus", "flag:file photo", "flag:subject search", "flag:watermark", "source:news"]
    assert L.photo_features({"source": "upload"}) == ["source:uploaded"] and L.photo_features(None) == []
    cut = "photo of Jean-Baptiste Alexandre Montgomery-Fitzwilliam the Third of Kansas City (search)"[:80]
    assert L.flag_key(cut) == "subject search"                         # a long name cut short: still no name
    assert L.photo_features({"source": "previous post's photo"}) == []  # a regeneration's label, not a source
    assert L.photo_features({"source": "story", "note": "video frame"}) == ["frame"]


def test_image_rejections_make_a_photo_rule_and_a_small_weight():
    reviews = ([rv("rejected", f"P{i} OUT", reason="Image quality", photo=FRAME) for i in range(4)]
               + [rv("approved", f"Q{i} SIGNS", photo=FRAME) for i in range(2)]
               + [rv("approved", f"R{i} SIGNS", photo=LEAD) for i in range(20)]
               + [rv("rejected", f"S{i} SIGNS", reason="Weak story", photo=LEAD) for i in range(10)])   # not about the photo
    rec = L.build("huddle", reviews, NFL, NOW)
    f = rec["photos"]["frame"]
    assert (f["approved"], f["image_rejected"], f["rule"]) == (2, 4, True)
    assert rec["photos"]["source:web"]["image_rejected"] == 0 and rec["photos"]["source:web"]["approved"] == 20
    photo_rules = [r for r in rec["rules"] if r.startswith("PHOTO")]
    assert photo_rules == rec["photo_rules"] and len(photo_rules) == 1     # frame, soft and X posts: one rule
    assert "editors rejected 4 of 6 posts whose photo was a video frame" in photo_rules[0]
    assert 0.9 <= L.photo_weight(rec, FRAME) < 1.0 < L.photo_weight(rec, LEAD) <= 1.1
    # seen on exactly the same posts: one piece of evidence, weighed (and shown as a rule) once
    assert [k for k, v in rec["photos"].items() if v.get("rule")] == ["frame"]
    assert rec["photos"]["source:x"]["same_as"] == "frame" and rec["photos"]["source:x"]["mult"] == 1.0
    assert rec["photos"]["flag:soft"]["mult"] == 1.0 and rec["photos"]["frame"]["mult"] < 1.0
    assert L.photo_weight(rec, {"source": "x"}) == 1.0                # an X photo that isn't a frame: not demoted
    assert rec["other"]["image_rejections"][0]["photo"] == ["flag:soft", "frame", "source:x"]
    # too few to say anything: neutral
    few = L.build("huddle", [rv("rejected", "P OUT", reason="Image quality", photo=FRAME)] * 2
                  + [rv("approved", "Q SIGNS", photo=LEAD)], NFL, NOW)
    assert few["photos"]["frame"]["mult"] == 1.0 and not few["photo_rules"] and L.photo_weight(few, FRAME) == 1.0


def test_studio_picks_and_the_candidates_passed_over():
    news = {"source": "news", "consensus": 3, "rank": 1, "of": 4, "passed": [FRAME]}
    reviews = [rv("studio_pick", f"S{i}", photo=news, draft_id=f"b:{i}") for i in range(7)] + \
              [rv("studio_pick", "U", photo={"source": "uploaded", "rank": None, "of": 3, "passed": [FRAME, LEAD]})]
    rec = L.build("huddle", reviews, NFL, NOW)
    assert rec["photos"]["frame"]["passed"] == 8 and rec["photos"]["frame"]["picked"] == 0
    assert rec["photos"]["consensus"]["picked"] == 7
    assert not any("passed over" in r for r in rec["rules"])          # a pass weighs half: 8 aren't enough yet
    more = L.build("huddle", reviews + [rv("studio_pick", f"T{i}", photo=news, draft_id=f"c:{i}") for i in range(3)], NFL, NOW)
    assert any("in the Studio editors passed over a video frame / flagged 'soft' / a photo from an X post 11 times "
               "and picked it 0 times" in r for r in more["rules"])
    assert L.photo_weight(more, {"source": "news", "consensus": 3}) > 1.0 > L.photo_weight(more, FRAME)
    assert rec["other"]["studio_photo_picks"] == 8 and rec["other"]["studio_uploads"] == 1
    assert rec["reviews_used"] == 0                                    # picks still don't move story kinds


def _learned(**mults):
    return {"photos": {k.replace("__", ":"): {"mult": v} for k, v in mults.items()}}


def test_the_weight_only_breaks_ties_in_the_studio_and_search_order():
    from bore import studio
    from bore.photosearch import sharp_first
    members = [{"url": "https://x.example.com/1", "text": "Jane Doe signs"}]
    frame = Photo("f", "x", "@a", 1600, 1200, note="video frame", post_url="https://x.example.com/1")
    lead = Photo("w", "web", "example.com", 1600, 1200, post_url="https://x.example.com/1")
    soft = Photo("s", "web", "example.com", 1600, 1200, post_url="https://x.example.com/1", flags=["soft (sharpness 20)"])
    rank = lambda learned: [p.url for p in sorted([frame, soft, lead], key=lambda p: studio._image_rank(
        p, "Jane Doe", members, set(), learned))]
    assert rank(None) == ["f", "w", "s"]                               # no lessons: the order found
    assert rank(_learned(frame=0.8)) == ["w", "f", "s"]                # equal on every check: the learned one first
    assert rank(_learned(frame=0.8, flag__soft=1.2, source__web=1.2)) == ["w", "f", "s"]   # never past a check
    small = Photo("m", "search", "example.org", 700, 600, flags=["small: may look soft on a card"])
    big = Photo("b", "search", "example.org", 1600, 1200)
    other = Photo("o", "commons", "Wikimedia Commons", 1600, 1200)
    assert [p.url for p in sharp_first([small, big, other])] == ["b", "o", "m"]
    assert [p.url for p in sharp_first([small, big, other], _learned(source__commons=1.2, source__search=0.8))] == ["o", "b", "m"]


def test_tie_break_moves_only_within_equal_keys():
    a, b, c = (Photo(u, s, "", consensus=n) for u, s, n in (("a", "news", 3), ("b", "x", 2), ("c", "news", 2)))
    learned = _learned(source__x=0.8, source__news=1.2)
    assert [p.url for p in L.tie_break([a, b, c], None, key=lambda p: p.consensus)] == ["a", "b", "c"]
    assert [p.url for p in L.tie_break([a, b, c], learned, key=lambda p: p.consensus)] == ["a", "c", "b"]


def test_story_images_nudge_own_photos_of_about_the_same_size(cfg, monkeypatch):
    from bore import cli, media
    big_x = Photo("x1", "x", "@a", 1600, 1000)
    web1 = Photo("w1", "web", "example.com", 1500, 1000)
    web2 = Photo("w2", "web", "example.com", 1000, 800)
    monkeypatch.setattr(media, "story_photos", lambda *a, **k: [big_x, web1, web2])
    monkeypatch.setattr(media, "best", lambda photos, *a, **k: list(photos))   # already largest first
    monkeypatch.setattr(media, "graphic", lambda *a, **k: "")
    monkeypatch.setattr(cli, "IMAGE_DEADLINE", {"at": 0})
    urls = lambda learned: [p.url for p in cli._story_images([], "never", cfg, learned=learned)]
    assert urls(None) == ["x1", "w1", "w2"]
    assert urls(_learned(source__x=0.8)) == ["w1", "x1", "w2"]              # 1.6M x 0.9 < 1.5M
    assert urls(_learned(source__x=0.8, source__web=1.2)) == ["w1", "x1", "w2"]   # 0.8M never passes 1.6M


def test_draft_records_which_photo_it_used(tmp_path):
    from bore import routine as R
    d = tmp_path / "huddle-202610130000-x"
    d.mkdir()
    photos = [{"index": 0, "file": "photos/0.jpg", "url": "https://img.example.com/0.jpg", "source": "web", "credit": "example.com"},
              {"index": 1, "file": "photos/1.jpg", "url": "https://img.example.com/1.mp4#t=3", "source": "x", "credit": "@a",
               "note": "video frame", "via": "frame at 3s of the video in @a's post", "flags": ["soft (sharpness 30)"],
               "consensus": 2}]
    brief = {"id": d.name, "page": "huddle", "page_name": "Huddle", "posts": [], "verification": {"route": "corroborated"},
             "photos": photos}
    (d / "brief.json").write_text(json.dumps(brief))
    (d / "copy.json").write_text(json.dumps({"caption": "c", "headline_lines": ["A"], "photo_index": 1}))
    (d / "card.jpg").write_bytes(b"\xff\xd8\xff")
    p = R.draft_payload(d)
    assert p["source_photo"] == photos[1]["url"]
    assert p["photo"] == {"rank": 1, "of": 2, "source": "x", "via": photos[1]["via"], "note": "video frame",
                          "flags": ["soft (sharpness 30)"], "consensus": 2}
    assert p["brief"]["photos"][1]["flags"] == ["soft (sharpness 30)"] and p["brief"]["photos"][0]["credit"] == "example.com"
    (d / "copy.json").write_text(json.dumps({"caption": "c", "headline_lines": ["A"], "photo_index": -1}))
    assert "photo" not in R.draft_payload(d) and R.draft_payload(d)["source_photo"] == ""


class _Dash:
    def __init__(self, rec, latest):
        self.rec, self.latest, self.saved, self.asked = rec, latest, [], []

    def lessons(self, page):
        return self.rec

    def reviews(self, days):
        self.asked.append(days)
        return self.latest

    def made_since(self, since):
        return {}

    def save_lessons(self, page, rec):
        self.saved.append(rec)


def test_a_new_note_rebuilds_the_lessons_within_the_hour(monkeypatch, tmp_path):
    from types import SimpleNamespace
    monkeypatch.setattr(L, "PRIVATE", tmp_path)
    page = [SimpleNamespace(slug="huddle")]
    built = L.build("huddle", [], NFL, NOW - 3600)
    note = rv("feedback", "A SIGNS", reason="Redo", note="use a closer photo", days_ago=0.01)
    dash = _Dash(built, [note])
    L.refresh(dash, page, now=NOW, quiet=True)
    assert len(dash.saved) == 1 and dash.saved[0]["notes"][0]["text"] == "use a closer photo"
    later = _Dash(dash.saved[0], [note])                      # nothing new since: no rebuild
    L.refresh(later, page, now=NOW + 60, quiet=True)
    assert later.saved == [] and later.asked == [2]
    assert not L.new_notes(built, [rv("approved", "B", days_ago=0.01)], "huddle")              # no note
    assert not L.new_notes(built, [{**note, "page": "culture-rush"}], "huddle")               # another page
    assert not L.new_notes(built, [rv("withdrawn", "C", note="locked", days_ago=0.01)], "huddle")
