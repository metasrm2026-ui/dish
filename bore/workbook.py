"""Parse the source workbook: one sheet per page; columns X, Website, Reddit, Facebook, Instagram.
Cells are inconsistent (profile URLs, post URLs, plain names). For any x.com/twitter.com URL the
first path segment is the handle and the rest is ignored."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from openpyxl import load_workbook

from .config import Page

RESERVED_X = {"i", "intent", "search", "home", "hashtag", "explore", "share", "status", "home.php"}
HANDLE_RE = re.compile(r"^@?([A-Za-z0-9_]{1,15})$")
COLUMN_KEYS = {
    "x": ("x", "twitter", "x/twitter", "twitter/x", "x (twitter)"),
    "website": ("website", "websites", "web", "site", "sites"),
    "reddit": ("reddit", "subreddit", "subreddits"),
    "facebook": ("facebook", "fb"),
    "instagram": ("instagram", "ig"),
}


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def parse_x(cell: str) -> tuple[str | None, str | None]:
    """(handle, None) or (None, unresolved_text)."""
    s = cell.strip()
    if not s:
        return None, None
    if re.search(r"(twitter\.com|x\.com)", s, re.I):
        if not s.lower().startswith("http"):
            s = "https://" + s
        seg = [p for p in urlsplit(s).path.split("/") if p]
        if seg and seg[0].lower() not in RESERVED_X and HANDLE_RE.match(seg[0]):
            return seg[0].lower(), None
        return None, cell
    m = HANDLE_RE.match(s)
    if m:
        return m.group(1).lower(), None
    return None, cell            # a plain name like "Adam Schefter": needs a human to map


def parse_reddit(cell: str) -> str | None:
    m = re.search(r"(?:reddit\.com)?/?r/([A-Za-z0-9_]+)", cell.strip())
    return m.group(1).lower() if m else None


def header_map(row) -> dict[str, int]:
    out = {}
    for idx, v in enumerate(row):
        name = str(v or "").strip().lower()
        for key, names in COLUMN_KEYS.items():
            if key not in out and (name in names or name.startswith(names[0] + " ")):
                out[key] = idx
    return out


def split_cell(v) -> list[str]:
    if v is None:
        return []
    out = []
    for chunk in re.split(r"[\n,;|]+", str(v)):
        out += [p.strip() for p in re.split(r"\s+(?=https?://|www\.|@|r/)", chunk) if p.strip()]
    return out


def parse_workbook(path: str, default_tz: str = "America/New_York") -> list[Page]:
    wb = load_workbook(path, read_only=True, data_only=True)
    pages = []
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
        hdr_idx, hmap = 0, {}
        for i, row in enumerate(rows[:10]):          # header might not be row 1
            hmap = header_map(row)
            if len(hmap) >= 2:
                hdr_idx = i
                break
        page = Page(slug=slugify(ws.title), name=ws.title.strip(), timezone=default_tz)
        for row in rows[hdr_idx + 1:]:
            def col(key):
                i = hmap.get(key)
                return split_cell(row[i]) if i is not None and i < len(row) else []
            for c in col("x"):
                h, bad = parse_x(c)
                if h and h not in page.x_handles:
                    page.x_handles.append(h)
                elif bad:
                    page.unresolved.append(f"x: {bad.strip()}")
            for c in col("website"):
                c = c.strip()
                if c and c not in page.websites:
                    page.websites.append(c)
            for c in col("reddit"):
                s = parse_reddit(c)
                if s and s not in page.subreddits:
                    page.subreddits.append(s)
            for c in col("facebook"):
                if "facebook.com" in c and not page.facebook_url:
                    page.facebook_url = c.strip()
        pages.append(page)
    return pages
