"""Story images: stills of the story's video, page policy, embedded posts, consensus, checks."""
import io

from PIL import Image

from bore import stills as S
from bore.config import Page
from bore.media import Photo, media_orig, story_photos


def jpeg(color=(200, 50, 50), size=(1280, 720), stripes=False, rows=False) -> bytes:
    im = Image.new("RGB", size, color)
    if stripes:
        for x in range(0, size[0], 40):
            im.paste((20, 20, 20), (x, 0, x + 20, size[1]))
    if rows:                                                  # a different picture
        for y in range(0, size[1], 90):
            im.paste((240, 240, 240), (0, y, size[0], y + 30))
        im.paste((10, 10, 120), (0, 0, size[0] // 3, size[1]))
    b = io.BytesIO()
    im.save(b, "JPEG")
    return b.getvalue()


def test_page_policy():
    assert S.video_policy(Page(slug="h", name="H", leagues=["nfl"])) == "never"
    assert S.video_policy(Page(slug="c", name="C", leagues=["entertainment"])) == "prefer"
    assert S.video_policy(Page(slug="x", name="X")) == "allow"


def test_video_thumbnails_full_size_and_sports_pages_drop_frames():
    assert media_orig("https://pbs.twimg.com/amplify_video_thumb/1/img/abc.jpg") == \
        "https://pbs.twimg.com/amplify_video_thumb/1/img/abc?format=jpg&name=orig"
    assert media_orig("https://pbs.twimg.com/media/Gx.jpg").endswith("name=orig")
    m = [{"platform": "x", "author": "nfl", "url": "https://x.com/nfl/status/1", "engagement": 9,
          "metrics": {"media": [{"url": "https://pbs.twimg.com/amplify_video_thumb/1/img/a.jpg", "type": "video"},
                                {"url": "https://pbs.twimg.com/media/b.jpg", "type": "photo"}]}}]
    assert len(story_photos(m, policy="allow")) == 2
    (only,) = story_photos(m, policy="never")
    assert only.note == "" and only.via == "in the post"


def test_embedded_posts_found_in_an_article():
    html = '''<blockquote class="twitter-tweet"><a href="https://twitter.com/PopBase/status/2109068876667552158">x</a></blockquote>
      <div data-tweet-id="2109068876667552999"></div><a href="https://www.instagram.com/p/DPq1aBcDeF/">ig</a>
      <iframe src="https://www.youtube.com/embed/aXlKOOUQ0c0"></iframe>
      <script type="application/ld+json">{"@type": "VideoObject", "name": "Tiger attack at Yorkshire park", "thumbnailUrl": "/v.jpg",
       "uploadDate": "2026-10-11T08:00:00Z"}</script>'''
    e = S.page_embeds("https://www.example.com/news/tiger", html)
    assert e["tweets"] == ["2109068876667552158", "2109068876667552999"]
    assert e["instagram"] == ["DPq1aBcDeF"] and e["youtube"] == ["aXlKOOUQ0c0"]
    assert e["videos"][0]["thumb"] == "https://www.example.com/v.jpg"
    assert S._js_token("2109068876667552158") and "." not in S._js_token("2109068876667552158")
    j = {"id_str": "2109068876667552158", "user": {"screen_name": "PopBase"},
         "mediaDetails": [{"media_url_https": "https://pbs.twimg.com/amplify_video_thumb/9/img/p.jpg", "type": "video",
                           "original_info": {"width": 1080, "height": 1920}}],
         "parent": {"id_str": "2109068876667550000", "user": {"screen_name": "AlexKTalks"},
                    "mediaDetails": [{"media_url_https": "https://pbs.twimg.com/media/q.jpg", "type": "photo"}]}}
    a, b = S._post_photos(j, "embedded in example.com")
    assert (a.note, a.credit, a.via, a.w) == ("video frame", "@PopBase", "post embedded in example.com", 1080)
    assert b.via == "replied-to post embedded in example.com" and b.post_url.endswith("/status/2109068876667550000")


def test_video_story_detection_and_terms():
    tiger = [{"platform": "web", "title": "Internet outraged as video of Tiger attacking man in England shared",
              "text": "", "metrics": {"categories": ["Tiger"]}}]
    assert S.is_video_story(tiger)
    assert not S.is_video_story([{"platform": "web", "title": "Tobey Maguire officiates ex-wife's wedding", "text": ""}])
    emb = [Photo("u", "x", "@a", note="video frame", via="post embedded in example.com")]
    assert S.is_video_story([{"platform": "web", "title": "Blueface proposes", "text": ""}], emb)
    reply_only = [Photo("u", "x", "@a", note="video frame", via="replied-to post embedded in example.com")]
    assert not S.is_video_story([{"platform": "web", "title": "Ashley St. Clair responds", "text": ""}], reply_only)
    assert S.story_terms(tiger)[:2] == ["Tiger", "Internet"] or "Tiger" in S.story_terms(tiger)


def test_copies_of_the_same_frame_count_as_consensus():
    a, b, c = (Photo(f"u{i}", "x", f"@acct{i}", 1280, 720, jpeg(stripes=(i < 2), rows=(i == 2)), note="video frame",
                     via="post embedded in x.com" if i == 0 else "X search") for i in range(3))
    for p in (a, b, c):
        p.phash = S.phash(Image.open(io.BytesIO(p.data)))
    groups = S._clusters([a, b, c])
    assert len(groups) == 2 and {p.url for p in groups[0]} == {"u0", "u1"}
    assert groups[0][0].consensus == 2                       # 2 accounts (being embedded is ranked apart)
    assert S.ham(a.phash, b.phash) == 0 and S.ham(a.phash, c.phash) > 10


def test_not_a_video_story_returns_nothing(monkeypatch):
    monkeypatch.setattr(S, "embedded_photos", lambda *a, **k: [])
    assert S.video_stills([{"platform": "web", "url": "https://p.com/a", "title": "Tobey Maguire officiates", "text": ""}],
                          "prefer") == []
    assert S.video_stills([{"platform": "web", "url": "https://p.com/a", "title": "viral video", "text": ""}], "never") == []


def test_stills_ranked_with_flags(monkeypatch):
    frame = Photo("https://pbs.twimg.com/amplify_video_thumb/1/img/a.jpg", "x", "@one", 1280, 720,
                  note="video frame", via="post embedded in example.com", post_url="https://x.com/one/status/1")
    monkeypatch.setattr(S, "embedded_photos", lambda *a, **k: [frame])
    monkeypatch.setattr(S, "fetch", lambda p: (setattr(p, "data", jpeg(stripes=True)) or p))
    monkeypatch.setattr(S, "inspect_still", lambda p, sports=False, ocr=True: p)
    got = S.video_stills([{"platform": "web", "url": "https://p.com/a", "title": "Video of tiger attack: man mauled",
                           "text": ""}], "prefer", frames=False, ocr=False)
    assert len(got) == 1 and got[0].via == "post embedded in example.com"
    assert any(f.startswith("violent incident") for f in got[0].flags)
    assert not S.SENSITIVE_RE.search("Brandi Glanville shares the death of her father Guy")


def test_lint_refuses_flagged_photos_and_takes_720_for_frames():
    from bore.guard import lint
    brief = {"page": "demo-sports", "posts": [{"text": "Jeanty ran for 120 yards."}], "verification": {"route": "corroborated"},
             "photos": [{"size": "1280x720", "file": "photos/0.jpg", "note": "video frame", "flags": []},
                        {"size": "1600x1200", "file": "photos/1.jpg", "flags": ["scoreboard"]}]}
    copy = {"niche_fit": 9, "template": "report", "headline_lines": ["JEANTY", "RUNS WILD"], "photo_index": 0,
            "people_in_photo": ["Ashton Jeanty"], "caption": "Jeanty ran for 120 yards."}
    errs = lint(brief, copy, {})["errors"]
    assert not any("floor" in e for e in errs)                 # a 720p frame of the video is fine
    assert any("flagged 'scoreboard'" in e for e in lint(brief, {**copy, "photo_index": 1}, {})["errors"])


def test_subject_links_and_list_posts():
    from bore.media import list_post, subject_of, x_links
    assert subject_of([{"title": "Sean ‘Diddy’ Combs taken away in ambulance", "text": "Combs at Fort Dix; Fort Dix said"}]) \
        == "Sean ‘Diddy’ Combs"
    assert subject_of([{"title": "#49ers Dre Greenlaw ejected", "text": ""}, {"title": "", "text": "Dre Greenlaw suspended"}]) \
        == "Dre Greenlaw"
    assert subject_of([{"title": "Are RJ and Zende on equal footing?", "text": ""}]) == ""
    assert list_post({"text": "Top WR grades: Mike Evans 91.4, Matthew Golden 88.9, Christian Watson 84.2"})
    assert not list_post({"text": "Mike Evans on his exchange with Trevon Diggs"})
    with_media = {"text": "Story https://t.co/abc https://t.co/media1", "metrics": {"media": [{"url": "x"}]}}
    assert x_links(with_media) == ["https://t.co/abc"]
    assert x_links({"text": "https://t.co/abc", "metrics": {"links": []}}) == []


def test_linked_article_photos(monkeypatch):
    from bore import media
    monkeypatch.setattr(media, "resolve_tco", lambda u: {"https://t.co/a": "https://www.example.org/nfl/story",
                                                          "https://t.co/b": "https://x.com/someone/status/1"}.get(u, ""))
    monkeypatch.setattr(media, "og_image", lambda u: "https://img.example.org/lead.jpg")
    m = [{"platform": "x", "author": "rapsheet", "url": "https://x.com/rapsheet/status/9", "engagement": 5,
          "text": "Greenlaw suspended https://t.co/a https://t.co/b", "metrics": {"links": ["https://t.co/a", "https://t.co/b"]}}]
    (ph,) = media.linked_photos(m)
    assert (ph.source, ph.credit, ph.via) == ("article", "example.org", "the linked example.org article")


def test_consensus_query_outlets_and_pictures():
    import io as _io

    from PIL import Image as _Image

    from bore.consensus import Art, build_query, hashes, n_outlets, same_picture
    assert build_query("“His death is not your content”: Internet outraged as video of Tiger attacking man in England shared") \
        .split()[:2] == ["Tiger", "England"]
    assert "Blueface" in build_query('"This gotta be a prank": Internet reacts as Blueface and Chrisean Rock get engaged')
    a = Art("https://abcnews.go.com/x", "Man killed by tiger at UK wildlife park", "abc news", "abcnews.go.com", 0, "bing", 0, oid="abcnews")
    b = Art("https://www.yahoo.com/y", "Man killed by tiger at UK wildlife park", "abc news on msn", "yahoo.com", 0, "bing", 1, oid="abcnews")
    c = Art("https://ndtv.com/z", "Tiger mauls man in England", "ndtv", "ndtv.com", 0, "ddg", 2, oid="ndtv")
    assert n_outlets([a, b, c]) == 2                     # a syndicated copy counts once
    import random
    rnd = random.Random(7)
    im = _Image.new("RGB", (1200, 675), (90, 110, 70))
    for _ in range(400):                                  # a textured picture
        x, y, w, h = rnd.randrange(1150), rnd.randrange(640), rnd.randrange(10, 90), rnd.randrange(10, 90)
        im.paste(tuple(rnd.randrange(256) for _ in range(3)), (x, y, x + w, y + h))
    other = im.transpose(_Image.FLIP_TOP_BOTTOM).rotate(90, expand=True).resize((1200, 675))
    crop = im.crop((60, 30, 1140, 645)).resize((800, 456))
    a.h, c.h = hashes(im), hashes(crop)
    from bore.consensus import _prep
    a.f, c.f = _prep(im), _prep(crop)
    assert same_picture(a, c)                             # a resized crop is the same picture
    b.h, b.f = hashes(other), _prep(other)
    assert not same_picture(a, b)


def test_outlets_are_origins_and_relevance_needs_the_event():
    from bore.consensus import Art, _outlet_id, event_vocab, n_outlets
    from bore.textutil import tokens
    yahoo_unknown = Art("https://www.yahoo.com/a", "Tiger kills man at park", "Yahoo", "yahoo.com", 0, "bing", 0)
    msn_abc = Art("https://www.msn.com/b", "Man killed by tiger", "ABC News on MSN", "msn.com", 0, "bing", 1)
    yahoo_cle = Art("https://www.yahoo.com/c", "Police respond to zoo", "Yahoo", "yahoo.com", 0, "ddg", 2,
                    img="https://s.yimg.com/x/zenfs.com/en/cleveland_com_123/abc.jpg")
    assert _outlet_id(yahoo_unknown) == "" and _outlet_id(msn_abc) == "abcnews" and _outlet_id(yahoo_cle) == "cleveland"
    nbc = Art("https://www.nbcnews.com/x", "Tiger kills man who entered its enclosure at English wildlife park", "NBC", "nbcnews.com", 0, "bing", 0)
    nyt = Art("https://www.nytimes.com/y", "Man Dies After Entering Tiger Enclosure at Wildlife Park in England", "NYT", "nytimes.com", 0, "bing", 1)
    cle = Art("https://www.cleveland.com/z", "Police lights generic story", "Cleveland", "cleveland.com", 0, "bing", 2)
    for a in (nbc, nyt, cle, yahoo_unknown, yahoo_cle):
        a.oid = _outlet_id(a)
    assert n_outlets([nbc, nyt]) == 2                 # independent outlets, similar titles: both count
    assert n_outlets([cle, yahoo_cle]) == 1           # Cleveland.com and its Yahoo copy: one outlet
    assert n_outlets([nbc, yahoo_unknown]) == 1       # an aggregator copy of unknown origin never counts
    head = "Who is Jennifer Meyer's husband Geoffrey Ogunlesi? All about the billionaire who married Tobey Maguire's ex-wife"
    titles = {"people.com": "Tobey Maguire officiates ex-wife Jennifer Meyer's wedding", "us.com": "Tobey Maguire officiates wedding",
              "e.com": "Jennifer Meyer marries Geoffrey Ogunlesi, Tobey officiates", "x.com": "Tobey officiates the wedding"}
    vocab = event_vocab(head, titles)
    assert set(tokens("Tobey Maguire officiates wedding")) & vocab
    assert not set(tokens("Betting Markets Think Tobey Maguire's Spider-Man Is Swinging Into Avengers")) & vocab


def test_floors_order_and_violent_flag():
    from bore.consensus import VIOLENT_RE
    from bore.media import Photo, best, floor_for
    news = Photo("u1", "news", "abc", consensus=3)
    assert floor_for(news, 800) == 600 and floor_for(Photo("u", "x", "@a", note="video frame"), 800) == 720
    assert floor_for(Photo("u", "web", "p.com"), 800) == 800
    small = Photo("a", "news", "abc", 1200, 675, b"x", consensus=3)
    big = Photo("b", "web", "p.com", 2400, 1600, b"x")
    assert [p.url for p in best([small, big], 800, keep_order=True)] == ["a", "b"]
    assert [p.url for p in best([small, big], 800)] == ["b", "a"]
    assert VIOLENT_RE.search("Man mauled to death by tiger") and not VIOLENT_RE.search("Brandi's father Guy dies at 77")
