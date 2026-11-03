from bore.guard import lint, verify

PORTER_SRC = ("Steelers CB Joey Porter Jr. appears unlikely to play Sunday against the Bengals, but he is trending "
              "in the right direction and a trade remains possible.")


def _brief(page, text, route="corroborated", photos=None):
    return {"page": page, "posts": [{"text": text}], "verification": {"route": route},
            "photos": photos or [{"size": "1536x1024"}], "playbook": {}}


def _huddle_copy(**kw):
    c = {"niche_fit": 9, "template": "report", "label": "REPORT", "headline_lines": ["JOEY PORTER JR.", "STILL OUT SUNDAY"],
         "footer": "TRADE STILL POSSIBLE", "photo_index": 0, "people_in_photo": ["Joey Porter Jr."],
         "caption": "Joey Porter Jr. appears unlikely to play Sunday, per Tom Pelissero.\n\nRide it out? 👇\n\n"
                    "#HereWeGo #Steelers #NFL #JoeyPorterJr"}
    c.update(kw)
    return c


def test_overstated_hedge_is_blocked():  # last run's Huddle card
    res = lint(_brief("huddle", PORTER_SRC, "watchlist"), _huddle_copy())
    assert not res["pass"] and any("hedged" in e for e in res["errors"])


def test_hedged_card_passes():
    res = lint(_brief("huddle", PORTER_SRC, "watchlist"), _huddle_copy(headline_lines=["JOEY PORTER JR.", "LIKELY OUT SUNDAY"]))
    assert res["pass"], res["errors"]


def test_single_source_caption_needs_attribution():
    c = _huddle_copy(headline_lines=["JOEY PORTER JR.", "LIKELY OUT SUNDAY"],
                     caption="Joey Porter Jr. is unlikely to play Sunday.\n\nRide it out? 👇\n\n#HereWeGo #Steelers #NFL #JoeyPorterJr")
    assert any("attribute" in e for e in lint(_brief("huddle", PORTER_SRC, "watchlist"), c)["errors"])


SWIFT_SRC = "Patient Zero debuted at #1 on the Global Spotify chart with 13.7 million streams; 6.12 million in the US."


def _cr_copy(**kw):
    c = {"niche_fit": 9, "template": "headline", "headline_lines": ["PATIENT ZERO", "HITS #1", "GLOBALLY"],
         "dek": "13.7 million streams on debut day", "stat_label": "Global Spotify debut", "stat_value": "13.7M streams",
         "photo_index": 0, "people_in_photo": ["Taylor Swift"],
         "caption": "TAYLOR SWIFT BREAKS ANOTHER RECORD 👑\n\n13.7 million streams.\n\nBest drop in years? 👀\n\n"
                    "#TaylorSwift #PatientZero #Spotify #CultureRush #PopCulture"}
    c.update(kw)
    return c


def test_repeated_number_is_blocked():  # last run's Culture Rush card
    res = lint(_brief("culture-rush", SWIFT_SRC), _cr_copy())
    assert not res["pass"] and any("repeats" in e for e in res["errors"])


def test_unsourced_number_is_blocked():
    res = lint(_brief("culture-rush", SWIFT_SRC), _cr_copy(stat_label="", stat_value="", dek="15 million streams on day one"))
    assert any("does not appear in the sources" in e for e in res["errors"])


def test_clean_culture_rush_copy_passes():
    res = lint(_brief("culture-rush", SWIFT_SRC), _cr_copy(stat_label="", stat_value=""))
    assert res["pass"], res["errors"]


def test_soft_photo_is_blocked():
    res = lint(_brief("culture-rush", SWIFT_SRC, photos=[{"size": "720x1280"}]), _cr_copy(stat_label="", stat_value=""))
    assert any("below" in e for e in res["errors"])


def _fake_ocr(lines):
    return lambda _path: [(l, 0.98) for l in lines]


def test_verify_catches_invented_and_repeated_text():
    brief = _brief("culture-rush", SWIFT_SRC)
    copy = _cr_copy(stat_label="", stat_value="")
    ocr = _fake_ocr(["CULTURE", "RUSH", "NEW RECORD", "PATIENT ZERO", "HITS#1", "GLOBALLY", "13.7 million streams",
                     "on debut day", "Global Spotify debut", "13.7M streams"])
    res = verify("x.jpg", brief, copy, ocr)
    assert not res["pass"]
    assert any("appears more times" in e or "unexpected number" in e for e in res["errors"])
    assert any("NEW RECORD" in w for w in res["warnings"])


def test_verify_catches_misspelling():
    copy = _huddle_copy(headline_lines=["GIANTS HUNTING", "FOR A QB"], footer="JAXSON DART INJURED")
    res = verify("x.jpg", _brief("huddle", "x"), copy, _fake_ocr(["REL-CAPS", "GIANTS HUNTING", "FOR A QB", "JAXSON DART INJURED"]))
    assert not res["pass"] and any("REPORT" in e for e in res["errors"])


def test_verify_passes_clean_card_with_jersey_number():
    copy = _huddle_copy(headline_lines=["JOEY PORTER JR.", "LIKELY OUT SUNDAY"])
    res = verify("x.jpg", _brief("huddle", PORTER_SRC), copy,
                 _fake_ocr(["REPORT", "JOEY PORTER JR.", "LIKELY OUT SUNDAY", "24", "TRADE STILL POSSIBLE"]))
    assert res["pass"], res["errors"]


def test_text_on_a_face_is_blocked():
    copy = _huddle_copy(headline_lines=["GIANTS HUNTING", "FOR A QB"], footer="JAXSON DART INJURED")
    ocr = lambda _p: [("REPORT", 0.99, (420, 30, 660, 140)), ("GIANTS HUNTING", 0.99, (60, 150, 1020, 300)),
                      ("FOR A QB", 0.99, (230, 330, 850, 490)), ("JAXSON DART INJURED", 0.99, (220, 1190, 860, 1260))]
    face = [(400, 180, 290, 410, 0.95)]   # the face sits under the headline
    res = verify("x.jpg", _brief("huddle", "x"), copy, ocr, faces_override=face, source_faces_override=face)
    assert not res["pass"] and any("overlaps a face" in e for e in res["errors"])


def test_face_covered_on_card_is_blocked():
    copy = _huddle_copy(headline_lines=["GIANTS HUNTING", "FOR A QB"], footer="JAXSON DART INJURED")
    ocr = lambda _p: [("REPORT", 0.99, (0, 0, 1, 1)), ("GIANTS HUNTING", 0.99, (0, 0, 1, 1)),
                      ("FOR A QB", 0.99, (0, 0, 1, 1)), ("JAXSON DART INJURED", 0.99, (0, 0, 1, 1))]
    res = verify("x.jpg", _brief("huddle", "x"), copy, ocr, faces_override=[], source_faces_override=[(1, 1, 50, 50, 0.9)])
    assert any("no face is visible on the card" in e for e in res["errors"])


def test_text_on_the_chest_is_fine():
    copy = _huddle_copy(headline_lines=["GIANTS HUNTING", "FOR A QB"], footer="JAXSON DART INJURED")
    ocr = lambda _p: [("REPORT", 0.99, (420, 640, 660, 745)), ("GIANTS HUNTING", 0.99, (60, 760, 1020, 900)),
                      ("FOR A QB", 0.99, (230, 930, 850, 1090)), ("JAXSON DART INJURED", 0.99, (220, 1190, 860, 1260))]
    face = [(395, 176, 289, 413, 0.95)]
    assert verify("x.jpg", _brief("huddle", "x"), copy, ocr, faces_override=face, source_faces_override=face)["pass"]


def test_footer_overstatement_is_blocked():  # 11:05 run: "HE'LL ... RETURN" from "he can ... return"
    src = ("Jayden Daniels is doing everything he can to avoid season-ending elbow surgery; he can brace his "
           "elbow and return within the coming weeks, though surgery remains a strong possibility.")
    c = _huddle_copy(headline_lines=["JAYDEN DANIELS AVOIDS", "SURGERY FOR NOW"], footer="HE'LL BRACE IT AND RETURN",
                     people_in_photo=["Jayden Daniels"],
                     caption="Per Tom Pelissero, Daniels hopes to avoid surgery.\n\nGood news? 👇\n\n#HTTC #Commanders #JaydenDaniels #NFL")
    res = lint(_brief("huddle", src, "watchlist"), c)
    assert any("RETURN" in e for e in res["errors"])
    c["footer"] = "HE CAN BRACE IT AND RETURN"
    assert lint(_brief("huddle", src, "watchlist"), c)["pass"]
