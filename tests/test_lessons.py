"""Learning from the editors' reviews: story kinds, the daily lessons record, ranking weights, lint."""
import time

from bore import lessons as L
from bore.kinds import CELEBRITY, SPORTS, kind_for, profile_for

NOW = 1_791_850_000.0
DAY = 86400


def rv(verdict, headline, page="huddle", days_ago=1, **kw):
    return {"verdict": verdict, "page": page, "headline": headline, "decided_at": (NOW - days_ago * DAY) * 1000, **kw}


def test_kinds_from_headlines():
    assert kind_for("JEANTY LIMITED IN PRACTICE", "", SPORTS) == "injury_practice"
    assert kind_for("MAYFIELD RULED OUT", "", SPORTS) == "injury_status"
    assert kind_for("SEAHAWKS SIGN JOE MIXON", "", SPORTS) == "transaction"
    assert kind_for("THORNTON CARTED OFF", "", SPORTS) == "injury_ingame"
    assert kind_for("BLUEFACE PROPOSES TO CHRISEAN ROCK", "", CELEBRITY) == "relationship"
    assert kind_for("Star stuns on the red carpet", "", CELEBRITY) == "red_carpet_fluff"
    assert kind_for("NOTHING HERE", "", SPORTS) == "other"
    assert profile_for("huddle", {"niche": "NFL news"}) is SPORTS
    assert profile_for("culture-rush", {"niche": "celebrity, viral, true crime"}) is CELEBRITY
    own = profile_for("x", {"kinds": [{"id": "pets", "label": "Pets", "match": [r"\bdog\b"]}]})
    assert kind_for("A dog wins", "", own) == "pets" and kind_for("A cat", "", own) == "other"


def test_rejected_kind_is_weighted_down_and_marked_avoid():
    reviews = ([rv("rejected", f"PLAYER {i} LIMITED IN PRACTICE") for i in range(13)]
               + [rv("approved", f"PLAYER {i} LIMITED IN PRACTICE") for i in range(3)]
               + [rv("approved", f"TEAM {i} SIGNS A RECEIVER") for i in range(12)]
               + [rv("rejected", f"TEAM {i} SIGNS A KICKER") for i in range(4)])
    rec = L.build("huddle", reviews, {"niche": "NFL"}, NOW)
    prac, trans = rec["kinds"]["injury_practice"], rec["kinds"]["transaction"]
    assert (prac["approved"], prac["rejected"], prac["status"]) == (3, 13, "avoid")
    assert prac["mult"] < 0.8 and trans["mult"] > 1.0 and trans["status"] == "prefer"
    assert rec["avoid"] == ["injury_practice"]
    assert any(r.startswith("AVOID Practice participation update: editors approved 3 of 16") for r in rec["rules"])
    assert L.weight(rec, "injury_practice") == prac["mult"] and L.weight(rec, "unknown") == 1.0 and L.weight({}, "x") == 1.0


def test_small_samples_old_reviews_clears_and_reset():
    few = L.build("huddle", [rv("rejected", "A LIMITED IN PRACTICE"), rv("rejected", "B LIMITED IN PRACTICE")],
                  {"niche": "NFL"}, NOW)
    assert few["kinds"]["injury_practice"]["mult"] == 1.0          # fewer than 5 reviews: no change yet
    old = L.build("huddle", [rv("rejected", "A LIMITED IN PRACTICE", days_ago=42)] + [rv("approved", "X SIGNS", days_ago=0)],
                  {"niche": "NFL"}, NOW)
    assert old["reviews_used"] == 2 and old["p0"] > 0.5            # a 6-week-old rejection weighs 1/4
    mixed = L.build("huddle", [rv("cleared", "A SIGNS", by="ed"), rv("approved", "B SIGNS", by="ed"),
                               rv("studio_pick", "C SIGNS"), {**rv("approved", "D SIGNS"), "verdict": "auto_cleared"}],
                    {"niche": "NFL"}, NOW)
    t = mixed["kinds"]["transaction"]
    assert (t["approved"], t["cleared"]) == (1, 1) and mixed["reviews_used"] == 2       # Studio picks aren't verdicts
    assert mixed["other"]["studio_picks"] == 1
    reset = L.build("huddle", [rv("rejected", "A SIGNS", days_ago=3), rv("approved", "B SIGNS", days_ago=1)],
                    {"niche": "NFL"},
                    NOW, prev={"reset_at": (NOW - 2 * DAY) * 1000, "overrides": {"transaction": 1.4}})
    assert reset["reviews_used"] == 1 and reset["kinds"]["transaction"]["mult"] == 1.4   # admin override wins
    assert L.weight({"overrides": {"quote": 0.6}, "kinds": {"quote": {"mult": 1.2}}}, "quote") == 0.6


def test_template_rule_and_brief_part():
    reviews = [rv("rejected", f"WHO IS BETTER {i}?", template="topic") for i in range(9)] + \
              [rv("approved", f"X SIGNS {i}", template="report") for i in range(9)]
    rec = L.build("huddle", reviews, {"niche": "NFL"}, NOW)
    assert rec["templates"]["topic"]["status"] == "avoid"
    part = L.for_brief(rec, "transaction")
    assert "topic" in part["avoid_templates"] and part["this_kind"]["approved"] == 9
    assert L.stale(None) and L.stale({"built_at": (NOW - 25 * 3600) * 1000}, NOW) and not L.stale({"built_at": NOW * 1000}, NOW)


def test_lint_refuses_an_avoid_kind_without_a_reason():
    from bore.guard import lint
    brief = {"page": "huddle", "posts": [{"text": "Jeanty was limited in practice on Wednesday."}],
             "verification": {"route": "corroborated"}, "photos": [{"size": "1600x1200", "file": "photos/0.jpg"}],
             "kinds": ["injury_practice", "other"],
             "lessons": {"this_kind": {"kind": "injury_practice", "label": "Practice participation update", "approved": 3, "n": 16,
                                       "status": "avoid"}, "avoid_templates": ["topic"]}}
    copy = {"niche_fit": 9, "template": "report", "headline_lines": ["JEANTY", "LIMITED"], "photo_index": 0,
            "people_in_photo": ["Ashton Jeanty"], "kind": "injury_practice", "caption": "Jeanty was limited in practice."}
    errs = lint(brief, copy, {})["errors"]
    assert any("approved 3 of 16" in e for e in errs)
    assert not any("approved 3 of 16" in e for e in lint(brief, {**copy, "lesson_override": "he is the starting RB"}, {})["errors"])
    assert any("'topic' template" in e for e in lint(brief, {**copy, "template": "topic"}, {})["errors"])
    assert any("not one of" in e for e in lint(brief, {**copy, "kind": "nonsense"}, {})["errors"])


def test_ranking_uses_the_learned_weight(cfg, monkeypatch, tmp_path):
    from bore import select as S
    from bore.config import Page
    page = Page(slug="demo-nfl", name="Demo NFL")
    t0 = time.time()
    items = [{"id": f"p{i}", "platform": "web", "url": f"https://a{i}.com/x", "author": f"a{i}.com", "engagement": 0,
              "title": "Jeanty limited in practice Wednesday with an ankle issue", "text": "", "created_at": t0 - 600,
              "collected_at": t0, "metrics": {}} for i in range(2)] + \
            [{"id": f"q{i}", "platform": "web", "url": f"https://b{i}.com/y", "author": f"b{i}.com", "engagement": 0,
              "title": "Raiders sign veteran receiver to a one-year contract", "text": "", "created_at": t0 - 600,
              "collected_at": t0, "metrics": {}} for i in range(2)]
    monkeypatch.setattr(L, "PRIVATE", tmp_path)
    monkeypatch.setattr("bore.routine.load_playbook", lambda slug: {"niche": "NFL"})
    base = {t.kind: t.score for t in S.select_page(page, items, cfg, [], t0, quota=2).trends}
    L.save(page.slug, {"kinds": {"injury_practice": {"mult": 0.5}, "transaction": {"mult": 1.3}}})
    learned = {t.kind: (t.score, t.mult) for t in S.select_page(page, items, cfg, [], t0, quota=2).trends}
    assert set(base) == {"injury_practice", "transaction"}
    assert learned["injury_practice"][1] == 0.5 and abs(learned["injury_practice"][0] - base["injury_practice"] * 0.5) < 1e-6
    assert abs(learned["transaction"][0] - base["transaction"] * 1.3) < 1e-6


def test_only_story_verdicts_move_the_weights():
    base = [rv("approved", f"TEAM {i} SIGNS A RECEIVER", by="ed") for i in range(8)] + \
           [rv("rejected", f"TEAM {i} SIGNS A KICKER", by="ed") for i in range(4)]
    m0 = L.build("huddle", base, {"niche": "NFL"}, NOW)["kinds"]["transaction"]["mult"]
    noise = [rv("rejected", "TEAM X SIGNS A PUNTER", reason="Image quality"),          # about the photo
             rv("rejected", "TEAM Y SIGNS A PUNTER", reason="Duplicate"),
             rv("rejected", "TEAM Y SIGNS A PUNTER", reason="Old news by now"),
             rv("withdrawn", "TEAM Z SIGNS A PUNTER"),                                  # SocialPilot refused it
             rv("cleared", "TEAM W SIGNS A PUNTER", by="someone-else"),                 # no review session
             rv("approved", "TEAM V SIGNS A PUNTER", route="manual")]                   # an editor's link
    rec = L.build("huddle", base + noise, {"niche": "NFL"}, NOW)
    assert rec["kinds"]["transaction"]["mult"] == m0
    assert rec["other"]["not_okay_reasons"] == {"Image quality": 1, "Duplicate": 1, "Old news by now": 1}
    assert rec["other"]["image_rejections"][0]["headline"] == "TEAM X SIGNS A PUNTER" and rec["other"]["withdrawn"] == 1
    # a clear that an editor undid (restored) doesn't count either
    cl = rv("cleared", "TEAM U SIGNS A PUNTER", by="ed", draft_id="d1")
    assert L.build("huddle", base + [cl], {"niche": "NFL"}, NOW)["kinds"]["transaction"]["cleared"] == 1
    assert L.build("huddle", base + [cl, rv("restored", "", draft_id="d1")], {"niche": "NFL"}, NOW)["kinds"]["transaction"]["cleared"] == 0


def test_stale_rejections_weigh_half_and_freshness_is_reported():
    t0 = (NOW - DAY) * 1000
    fast = [{**rv("rejected", f"P{i} LIMITED IN PRACTICE"), "created_at_unix": t0 / 1000 - 3600} for i in range(6)]
    slow = [{**rv("rejected", f"P{i} LIMITED IN PRACTICE"), "created_at_unix": t0 / 1000 - 15 * 3600} for i in range(6)]
    ok = [{**rv("approved", f"Q{i} SIGNS"), "created_at_unix": t0 / 1000 - 3600} for i in range(6)]
    a = L.build("huddle", fast + ok, {"niche": "NFL"}, NOW)
    b = L.build("huddle", slow + ok, {"niche": "NFL"}, NOW)
    assert b["kinds"]["injury_practice"]["smoothed"] > a["kinds"]["injury_practice"]["smoothed"]
    assert b["freshness"]["10h+"] == {"approved": 0, "rejected": 6, "rate": 0.0}


def test_kind_rank_wins_and_old_evidence_fades():
    e = {**rv("rejected", "SOMETHING ELSE ENTIRELY"), "kind_rank": "injury_practice", "kind": "other"}
    assert "injury_practice" in L.build("huddle", [e] * 6, {"niche": "NFL"}, NOW)["kinds"]
    old = [rv("rejected", f"Star {i} stuns on the red carpet", page="culture-rush", days_ago=40) for i in range(7)] + \
          [rv("approved", f"Star {i} engaged", page="culture-rush", days_ago=40) for i in range(7)]
    rec = L.build("culture-rush", old, {"niche": "celebrity"}, NOW)
    assert rec["kinds"]["red_carpet_fluff"]["mult"] == 1.0    # 7 verdicts from 6 weeks ago: not enough now


def test_kinds_fixes():
    assert kind_for('Shanahan: Deebo Samuel expected to miss "at least a couple of weeks"', "", SPORTS) == "injury_longterm"
    assert kind_for('Mike Evans: "I just kept playing"', "", SPORTS) == "quote"
    assert kind_for("Seahawks sign RB to the practice squad", "", SPORTS) == "transaction"
    assert kind_for("KYLE HAMILTON PICKS IT OFF", "", SPORTS) == "live_ingame"
    assert kind_for("JONES QUESTIONABLE TO RETURN", "", SPORTS) == "live_ingame"


def test_lint_avoid_kind_cannot_be_relabelled():
    from bore.guard import lint
    brief = {"page": "huddle", "kind": "injury_practice", "posts": [{"text": "Jeanty was limited in practice."}],
             "verification": {"route": "corroborated"}, "photos": [{"size": "1600x1200", "file": "photos/0.jpg"}],
             "kinds": ["injury_practice", "other"],
             "lessons": {"avoid_kinds": ["injury_practice"], "this_kind": {"kind": "injury_practice", "approved": 3, "n": 16,
                                                                          "status": "avoid", "label": "Practice"}}}
    copy = {"niche_fit": 9, "template": "report", "headline_lines": ["JEANTY", "LIMITED"], "photo_index": 0,
            "people_in_photo": ["Ashton Jeanty"], "kind": "other", "caption": "Jeanty was limited in practice."}
    assert any("lesson_override" in e for e in lint(brief, copy, {})["errors"])


def test_kind_boost_fades_instead_of_switching_off(cfg, monkeypatch, tmp_path):
    from bore import select as S
    from bore.config import Page
    page = Page(slug="demo-nfl", name="Demo NFL")
    t0 = time.time()
    items = [{"id": f"p{i}", "platform": "web", "url": f"https://a{i}.com/x", "author": f"a{i}.com", "engagement": 0,
              "title": "Raiders sign veteran receiver to a one-year contract", "text": "", "created_at": t0 - 600,
              "collected_at": t0, "metrics": {}} for i in range(2)]
    monkeypatch.setattr(L, "PRIVATE", tmp_path)
    monkeypatch.setattr("bore.routine.load_playbook", lambda slug: {"niche": "NFL", "prefilter": {"boost": [r"\bsign"]}})
    scores = {}
    for m in (1.0, 0.99, 0.9, 0.8):
        L.save(page.slug, {"kinds": {"transaction": {"mult": m}}})
        (t,) = S.select_page(page, items, cfg, [], t0, quota=1).trends + S.select_page(page, items, cfg, [], t0, quota=1).watchlist[:0]
        scores[m] = t.score / m
    kw = cfg["scoring"]["weights"]["kind"]
    assert abs((scores[1.0] - scores[0.8]) - kw) < 1e-6                 # full boost at 1.0, none at 0.8
    assert abs((scores[0.99] - scores[0.8]) - kw * 0.95) < 1e-6
