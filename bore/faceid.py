"""Whose face is in a photo? A story about one person must not get a photo of someone else (a Mike
Evans story was given a Brock Purdy photo; a Chrisean Rock search returned mostly Blueface).

Faces (YuNet, bore/guard.py) are compared with a reference face of the subject by SFace (OpenCV
Model Zoo, Apache-2.0, int8: assets/models/):

  reference  NFL players: the ESPN headshot (only ever the reference, never a candidate);
             others: the lead image of the person's Wikipedia article. Nothing else: without a
             portrait (no free Wikipedia photo, e.g. Chrisean Rock) every photo stays neutral. The
             face most candidates share is no reference: when a partner dominates the results
             (mostly Blueface), it is the partner's. Cached per name for the process; a lookup the
             network cut short (an error, HTTP 429 or 5xx) is tried again.
  match      cosine >= 0.363 (a face not too blurred to tell): no wrong person reached it in the
             bake-off (170 judged photos)
  mismatch   a clear face (SFace's raw feature length, its quality, >= 8.5), every clear face
             below 0.25 and every other face below 0.2: 'may not be <Name>'
  neutral    no face, faces too small or unclear to tell, or in between. A helmet or a face
             turned away scores low for the right player too (only ~34% of right-person photos
             of lesser-known players reached the match line), so photos are ranked by it and
             flagged, never dropped on a score.

Measured on the bake-off's judged photos with this pipeline and these references: right person
86 match, 30 neutral, 0 'may not be' (of 116 with a face); wrong person 0 match, 5 'may not be',
6 neutral (of 11 with a face, all NFL: no wrong-person celebrity photo there had a reference). The
thresholds were chosen on those same photos, so this is in-sample: editors' verdicts on flagged
photos are the real test. A low score alone (no quality gate) flagged 14 right-person photos
(helmets, profiles) to catch 9 wrong ones. Live, the Brock Purdy press-conference photo on a Mike
Evans search scored 0.23 (a clear face); the lowest clear right-person face was 0.25 (a face
swollen by illness). About 0.15 s per photo. Without the model file nothing is labelled and
nothing fails.
"""
from __future__ import annotations

import io
import os
import queue
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path

MODEL = Path(__file__).resolve().parent.parent / "assets" / "models" / "face_recognition_sface_2021dec_int8.onnx"
MATCH = 0.363        # bake-off: 0 wrong-person photos at or above it
MISMATCH = 0.25      # every clear face below it: someone else (clear wrong faces scored at most 0.23)
DOUBT = 0.2          # ...unless a blurred face scores above this one (it may be the subject's)
CLEAR = 8.5          # raw feature length of a recognisable face: matched faces had a median of 10.5,
                     # right-person faces that scored low (helmets, profiles) at most 7.9
MATCH_Q = 7.0        # ...and of a face that may match at all: every bake-off match was >= 7.3, while two
                     # grainy faces on one of our cards (two people) scored 0.45 with each other
MAX_SIDE = 1280      # faces are found on the photo scaled to this (plenty for a face)
MIN_FACE = 40        # px tall: smaller faces never make a 'may not be' call
TINY = 20            # px tall: not compared at all
RANK = {"match": 0, "": 1, "mismatch": 2}
PERSON_RE = re.compile(
    r"\(born\b|\(\d{4}\s*[–-]\s*\d{4}\)|\b(actor|actress|singer|rapper|musician|songwriter|comedian|personality|"
    r"model|influencer|youtuber|streamer|presenter|host|player|footballer|quarterback|coach|athlete|wrestler|boxer|"
    r"fighter|politician|businessman|businesswoman|entrepreneur|socialite|author|journalist|chef|dancer|director|"
    r"producer|executive|commissioner|referee|criminal|murderer|killer|governor|president|senator|congress(?:wo)?man|"
    r"mayor|judge|king|queen|prince|princess|lawyer|attorney|activist|pastor|founder|ceo)\b", re.I)

_RECS: queue.SimpleQueue = queue.SimpleQueue()   # SFace nets, one per concurrent user (a net is not thread-safe)
_REFS: dict = {}
_REFS_LOCK = threading.Lock()
_REF_POOL = ThreadPoolExecutor(2, thread_name_prefix="face-ref")


def available() -> bool:
    return MODEL.exists()


def person_name(name: str) -> str:
    """The name to look up and show: no quoted nickname or possessive ("Sean ‘Diddy’ Combs" ->
    "Sean Combs", "Davante Adams’" -> "Davante Adams")."""
    name = re.sub(r"(?<=\s)[‘“\"'][^‘’“”\"']+[’”\"'](?=\s)", " ", f" {name or ''} ")
    return re.sub(r"['’]s?$", "", " ".join(name.split()))


def _recognizer():
    try:
        return _RECS.get_nowait()
    except queue.Empty:
        import cv2
        return cv2.FaceRecognizerSF.create(str(MODEL), "")


def faces(data: bytes) -> list:
    """(height px, quality, unit-length SFace feature) of each face in an image, largest first.
    Quality is the raw feature's length: low for a helmet, a profile or a blurred face."""
    import cv2
    import numpy as np
    from PIL import Image, ImageOps

    from .guard import detect_face_rows
    from .media import flatten
    im = flatten(ImageOps.exif_transpose(Image.open(io.BytesIO(data))))
    im.thumbnail((MAX_SIDE, MAX_SIDE))
    bgr = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
    rows = [r for r in detect_face_rows(bgr, min_score=0.7) if r[3] >= TINY]
    if not rows:
        return []
    rec = _recognizer()
    try:
        out = []
        for r in rows:
            f = rec.feature(rec.alignCrop(bgr, r)).flatten().astype("float32")
            n = float(np.linalg.norm(f))
            out.append((float(r[3]), n, f / (n or 1.0)))
    finally:
        _RECS.put(rec)
    return sorted(out, key=lambda x: -x[0])


def clear(face) -> bool:
    """Big and recognisable enough to say whose it is."""
    return face[0] >= MIN_FACE and face[1] >= CLEAR


def judge(ref, fs: list) -> tuple[str, float | None]:
    """('match' | 'mismatch' | '', the best face's cosine, or None when no face was compared)."""
    if ref is None or not fs:
        return "", None
    sims = [(float(ref @ x[2]), x[1], clear(x)) for x in fs]          # (cosine, quality, clear)
    best = max(s for s, _, _ in sims)
    if any(s >= MATCH and q >= MATCH_Q for s, q, _ in sims):
        return "match", best
    if any(c for _, _, c in sims) and all(s < (MISMATCH if c else DOUBT) for s, _, c in sims):
        return "mismatch", best
    return "", best


# ------------------------------------------------------------------ the subject's face
def _portrait(data: bytes):
    """The face of a reference image: the largest one, clear, and clearly the largest (a
    portrait, not a group)."""
    fs = faces(data)
    if not fs or not clear(fs[0]) or (len(fs) > 1 and fs[1][0] > 0.6 * fs[0][0]):
        return None
    return fs[0][2]


def _wiki_ua() -> dict:
    """Wikimedia asks for a user agent that names the client (with WIKIMEDIA_UA, a contact): a
    browser's from a shared IP gets HTTP 429 sooner."""
    return {"User-Agent": os.environ.get("WIKIMEDIA_UA") or "BorePostStudio/1.0 (photo search for editors)"}


def _download(url: str, timeout: float) -> bytes | None:
    """The image, or None when there is none (404, an empty file). A busy server (HTTP 429, 5xx)
    raises: the caller must not remember it as 'no reference'."""
    from urllib.parse import urlsplit

    import requests

    from .media import UA
    host = urlsplit(url).hostname or ""
    r = requests.get(url, timeout=timeout,
                     headers=_wiki_ua() if host.endswith(("wikimedia.org", "wikipedia.org")) else UA)
    if r.status_code == 429 or r.status_code >= 500:
        raise OSError(f"HTTP {r.status_code} from {host}")
    return r.content if r.status_code == 200 and len(r.content) > 1000 else None


_WIKI = {"api_rests_until": 0.0}


def wikipedia_image(name: str, timeout: float = 6) -> tuple[str, bool | None]:
    """(lead image URL, is a person) from the English Wikipedia article titled with the name: the
    thumbnail URL exactly as the API gives it (960 px). Person None: no article, or a disambiguation
    page. While the query API is rate-limiting us (HTTP 429, seen from shared IPs), the page
    summary API answers instead (a 330 px thumbnail)."""
    from urllib.parse import quote

    import requests
    ua = _wiki_ua()
    if time.time() >= _WIKI["api_rests_until"]:
        r = requests.get("https://en.wikipedia.org/w/api.php", headers=ua, timeout=timeout, params={
            "action": "query", "titles": name, "redirects": 1, "prop": "pageimages|pageprops|description",
            "piprop": "thumbnail", "pithumbsize": 960, "ppprop": "disambiguation", "format": "json", "formatversion": 2})
        if r.status_code != 429:
            r.raise_for_status()
            for pg in (r.json().get("query") or {}).get("pages") or []:
                if pg.get("missing") or pg.get("invalid") or "disambiguation" in (pg.get("pageprops") or {}):
                    return "", None
                person = bool(PERSON_RE.search(pg.get("description") or ""))
                return ((pg.get("thumbnail") or {}).get("source") or "") if person else "", person
            return "", None
        _WIKI["api_rests_until"] = time.time() + max(300.0, float(r.headers.get("Retry-After") or 0))
    r = requests.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(name.strip().replace(' ', '_'))}",
                     headers=ua, timeout=timeout)
    if r.status_code == 404:
        return "", None
    r.raise_for_status()
    pg = r.json()
    if pg.get("type") == "disambiguation":
        return "", None
    person = bool(PERSON_RE.search(pg.get("description") or ""))
    return ((pg.get("thumbnail") or {}).get("source") or "") if person else "", person


def reference(name: str, sports: bool = False, player: dict | None = None, timeout: float = 6) -> tuple:
    """(unit feature or None, 'espn' | 'wikipedia' | '') for the subject. sports: an NFL player's
    ESPN headshot first (player: photosearch.nfl_player's answer when the caller has one; None
    asks ESPN here). Cached per name; a lookup the network cut short (an error, HTTP 429 or 5xx)
    is not, so the next story about that name tries again."""
    name = person_name(name)
    key = (name.lower(), sports)
    with _REFS_LOCK:
        if key in _REFS:
            return _REFS[key]
    got, final = (None, ""), True
    if sports:
        try:
            if player is None:              # strict: a failed ESPN search raises instead of reading as 'not a player'
                from .photosearch import nfl_player
                player = nfl_player(name, strict=True)
            headshot = player.get("headshot") or ""
            data = _download(headshot, timeout) if headshot else None
            f = _portrait(data) if data else None
            if f is not None:
                got = (f, "espn")
        except Exception:
            final = False
    if got[0] is None:
        try:
            url, _ = wikipedia_image(name, timeout)
            data = _download(url, timeout) if url else None
            f = _portrait(data) if data else None
            if f is not None:
                got = (f, "wikipedia")
        except Exception:
            final = False
    if final or got[0] is not None:
        with _REFS_LOCK:
            _REFS[key] = got
    return got


def start_reference(name: str, sports: bool = False, player: dict | None = None) -> Future:
    """Look the subject's face up in the background (a search starts it alongside its sources)."""
    return _REF_POOL.submit(reference, name, sports, player)


# ------------------------------------------------------------------ labelling candidates
def label(photos: list, name: str, sports: bool = False, deadline_s: float = 8, ref: Future | None = None) -> dict:
    """Compare each downloaded photo's faces with the subject's: sets ph.id_match ('match' |
    'mismatch' | ''), ph.id_score, ph.id_name and ph.id_ref (where the reference face came from).
    Photos not compared in time stay neutral. Returns a report line for the search output."""
    t0 = time.time()
    name = person_name(name)
    rep = {"source": "faces", "name": name, "reference": "", "match": 0, "mismatch": 0, "neutral": 0}
    todo = [ph for ph in photos if ph.data]
    if not (name and todo and available()):
        return {**rep, "neutral": len(photos), "error": "" if available() else "no face model", "ms": 0}
    ref = ref or start_reference(name, sports)
    ex = ThreadPoolExecutor(4)
    jobs = [ex.submit(faces, ph.data) for ph in todo]
    late = len(wait(jobs, timeout=max(0.5, t0 + deadline_s - time.time())).not_done)
    ex.shutdown(wait=False, cancel_futures=True)
    found = {id(ph): j.result() for ph, j in zip(todo, jobs) if j.done() and not j.cancelled() and not j.exception()}
    try:
        feat, how = ref.result(timeout=max(0.1, t0 + deadline_s - time.time()))
    except Exception:
        feat, how = None, ""
    rep["reference"] = how or "none"         # none: every photo stays neutral (no guessing from the candidates)
    for ph in photos:
        m, s = judge(feat, found.get(id(ph)) or [])
        if s is not None:
            ph.id_match, ph.id_score, ph.id_name, ph.id_ref = m, round(s, 3), name, how
        rep[m or "neutral"] += 1
    rep["ms"] = int((time.time() - t0) * 1000)
    if late:
        rep["error"] = f"{late} photo(s) not compared in time"
    return rep


def id_rank(ph) -> int:
    """Matched first, then neutral, then 'may not be' (a stable sort keeps the order within each)."""
    return RANK.get(getattr(ph, "id_match", ""), 1)


def fields(ph) -> dict:
    """A photo's comparison for briefs and the dashboard (nothing when no face was compared)."""
    if not getattr(ph, "id_name", ""):
        return {}
    return {"id_match": ph.id_match, "id_score": round(ph.id_score, 2), "id_name": ph.id_name, "id_ref": ph.id_ref}


def describe(rep: dict) -> str:
    """One line for a search report."""
    if rep.get("error") == "no face model":
        return "faces: not compared (no face model)"
    return (f"faces vs {rep.get('name')} ({rep.get('reference') or 'none'} reference): {rep['match']} match, "
            f"{rep['mismatch']} may not be, {rep['neutral']} neutral"
            + (f"; {rep['error']}" if rep.get("error") else "") + f" ({rep.get('ms', 0) / 1000:.1f}s)")


__all__ = ["available", "faces", "judge", "reference", "start_reference", "label", "id_rank", "fields", "describe",
           "wikipedia_image", "MATCH", "MISMATCH"]
