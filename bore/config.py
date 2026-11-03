"""Configuration loading. Credentials come only from the environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
# Brand data (pages, sources, playbooks, reference document text) is not in the repository: the
# routines pull it from the dashboard's encrypted store into this gitignored folder at the start
# of every run (`bore studio sync`). config/ keeps only examples and dummy data.
PRIVATE = ROOT / "private"


def load_env(path: Path | None = None) -> None:
    """Minimal .env reader: KEY=VALUE lines, does not override the real environment."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def secret(name: str) -> str:
    load_env()
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"{name} is not set. Put it in .env (see .env.example).")
    return value


@dataclass
class Page:
    slug: str
    name: str
    facebook_url: str = ""
    fk_slug: str = ""
    timezone: str = "America/New_York"
    x_handles: list[str] = field(default_factory=list)
    websites: list[str] = field(default_factory=list)
    feeds: list[str] = field(default_factory=list)        # RSS / Atom feeds read every hour (free)
    subreddits: list[str] = field(default_factory=list)
    subreddits_proposed: list[str] = field(default_factory=list)
    web_searches: list[str] = field(default_factory=list)
    tier_one: list[str] = field(default_factory=list)     # reporters only, never official/outlet feeds
    officials: list[str] = field(default_factory=list)    # league/team/outlet feeds
    unresolved: list[str] = field(default_factory=list)   # workbook cells we could not parse
    leagues: list[str] = field(default_factory=list)
    sources_origin: str = ""                              # "assumed from ..." when auto-built
    notes: str = ""                                       # human observations; NOT page DNA
    exclude_terms: list[str] = field(default_factory=list)  # off-topic for this page
    x_handles_secondary: list[str] = field(default_factory=list)  # rotated, capped (see schedule)
    subreddits_rotation: list[str] = field(default_factory=list)  # a few per Reddit pass
    web_rotation: list[str] = field(default_factory=list)         # a few extra searches per web pass
    priority_terms_file: str = ""   # YAML with `priority_terms`: names that earn a small score boost
    hourly: bool = True             # the hourly routine makes posts for the approval queue (else Studio only)

    @classmethod
    def from_dict(cls, d: dict) -> "Page":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        page = cls(**known)
        page.x_handles = [h.lower().lstrip("@") for h in page.x_handles]
        page.tier_one = [h.lower().lstrip("@") for h in page.tier_one]
        page.officials = [h.lower().lstrip("@") for h in page.officials]
        page.subreddits = [s.lower().removeprefix("r/") for s in page.subreddits]
        page.x_handles_secondary = [h.lower().lstrip("@") for h in page.x_handles_secondary
                                    if h.lower().lstrip("@") not in page.x_handles]
        page.subreddits_rotation = [s.lower().removeprefix("r/") for s in page.subreddits_rotation]
        page.timezone = _page_zone(page)
        return page

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


DEFAULT_TIMEZONE = Page.__dataclass_fields__["timezone"].default


def _page_zone(page: Page) -> str:
    """The page's time zone as this machine knows it (old names as their new ones: the routines'
    machines have no alias names). One it does not know, e.g. "Eastern" typed by hand, is the
    default with a degraded warning: every page's posts are worked out in one loop, so one bad
    zone must not stop the others."""
    from zoneinfo import ZoneInfo
    from .publish import ZONE_ALIASES
    name = str(page.timezone or "").strip() or DEFAULT_TIMEZONE
    name = ZONE_ALIASES.get(name, name)
    try:
        ZoneInfo(name)
        return name
    except (ValueError, KeyError, OSError):
        from . import ops
        import sys
        why = f"page {page.slug}: unknown time zone {str(page.timezone)[:30]!r}, used {DEFAULT_TIMEZONE}"
        print(f"WARNING: {why}: set it under Admin > Pages", file=sys.stderr)
        ops.degraded(why)
        return DEFAULT_TIMEZONE


def priority_terms(page: Page) -> list[str]:
    """Names (people, shows, franchises) the page's source sheet marks as its core subjects."""
    if not page.priority_terms_file:
        return []
    path = Path(page.priority_terms_file)
    path = path if path.is_absolute() else ROOT / path
    if not path.exists():
        return []
    with open(path) as f:
        return list((yaml.safe_load(f) or {}).get("priority_terms") or [])


def load_config(path: Path | None = None) -> dict:
    path = path or ROOT / "config" / "config.yaml"
    with open(path) as f:
        cfg = yaml.safe_load(f)
    cfg["_root"] = str(ROOT)
    return cfg


def pages_path() -> Path:
    return PRIVATE / "pages.yaml" if (PRIVATE / "pages.yaml").exists() else ROOT / "config" / "pages.yaml"


def load_pages(path: Path | None = None) -> list[Page]:
    path = path or pages_path()
    if not path.exists():
        return []
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return [Page.from_dict(p) for p in data.get("pages", [])]


def save_pages(pages: list[Page], path: Path | None = None) -> None:
    path = path or pages_path()
    header = (
        "# Pages and their sources. Written by `bore add-page` / `bore dna` / `bore import-workbook`;\n"
        "# safe to edit by hand. sources_origin says when a list was assumed from the page's posts.\n"
        "# tier_one: reporters only (never league/team/outlet feeds).\n"
    )
    with open(path, "w") as f:
        f.write(header)
        yaml.safe_dump({"pages": [p.to_dict() for p in pages]}, f, sort_keys=False, allow_unicode=True)


def resolve(cfg: dict, rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else Path(cfg["_root"]) / p
