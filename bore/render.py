"""One PDF briefing covering every page. Freshness is re-checked here (gate #2)."""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

from .select import PageResult, Trend


def age_str(now: float, t: float) -> str:
    h = (now - t) / 3600
    return f"{h * 60:.0f}m" if h < 1 else f"{h:.1f}h"


def gate(trends: list[Trend], now: float, max_age_hours: float) -> list[Trend]:
    """Drop any trend whose newest item is past the hard age limit, and any stale member."""
    cutoff = now - max_age_hours * 3600
    out = []
    for t in trends:
        t.cluster.members = [m for m in t.cluster.members
                             if (m.get("created_at") or m.get("collected_at") or 0) >= cutoff]
        if t.cluster.members:
            out.append(t)
    return out


def render(results: list[tuple[str, PageResult]], cfg: dict, path: Path, now: float | None = None) -> Path:
    now = now or time.time()
    max_age = cfg["clustering"]["max_age_hours"]
    ss = getSampleStyleSheet()
    h1, h2, body = ss["Heading1"], ss["Heading2"], ss["BodyText"]
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=10, textColor=colors.HexColor("#555555"))
    watch = ParagraphStyle("watch", parent=body, fontSize=8.5, leading=11)
    tag = {"corroborated": "#1b7f3b", "scoop": "#b35c00"}
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(path), pagesize=letter, leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            title="Sports trend briefing")
    stamp = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M")
    story = [Paragraph(f"Trend briefing: {stamp}", h1)]
    for name, res in results:
        picks = gate(res.shortlist, now, max_age)
        verified = sum(1 for t in picks[:res.quota] if t.route != "watchlist")
        story.append(Paragraph(f"<b>{escape(name)}</b>: {min(len(picks), res.quota)}/{res.quota} picks "
                               f"({verified} verified) · ", body))
        story.append(Paragraph(f"<b>{len(gate(res.trends, now, max_age))}</b> trends, "
                               f"<b>{len(gate(res.watchlist, now, max_age))}</b> watchlist: {escape(name)}",
                               body))
    for name, res in results:
        story += [PageBreak(), Paragraph(escape(name), h1),
                  Paragraph(f"{res.item_count} items in window", small), Spacer(1, 6)]
        picks = gate(res.shortlist, now, max_age)
        if picks:
            story.append(Paragraph(f"POST THESE: {res.quota} for this briefing, then backups", h2))
        for i, t in enumerate(picks, 1):
            cl = t.cluster
            lead = cl.lead()
            status = {"corroborated": ("#1b7f3b", "VERIFIED"), "scoop": ("#b35c00", "TIER-ONE SCOOP"),
                      "watchlist": ("#b00020", "VERIFY BEFORE POSTING (single source)")}[t.route]
            tag_txt = "BACKUP · " if i > res.quota else ""
            u = lead.get("url") or ""
            story.append(Paragraph(
                f"<b>{i}. {escape(t.headline[:200])}</b><br/>{tag_txt}"
                f"<font color='{status[0]}'><b>{status[1]}</b></font> · {len(cl.sources)} source(s) · "
                f"{cl.engagement:,} engagements · {age_str(now, cl.last_seen)} ago"
                + (f" · <link href='{escape(u)}'>source</link>" if u else ""), body))
            story.append(Spacer(1, 5))
        trends = gate(res.trends, now, max_age)
        if trends:
            story += [Spacer(1, 8), Paragraph("All verified trends", h2)]
        for i, t in enumerate(trends, 1):
            cl = t.cluster
            label = "CORROBORATED" if t.route == "corroborated" else "TIER-ONE SCOOP"
            dev = f" · <b>DEVELOPED</b>: {escape(t.developed)}" if t.developed else ""
            story.append(Paragraph(
                f"<b>{i}. {escape(t.headline[:220])}</b><br/>"
                f"<font color='{tag[t.route]}'><b>{label}</b></font> · score {t.score:.1f} · "
                f"{len(cl.sources)} sources · {', '.join(sorted(cl.platforms))} · "
                f"{cl.engagement:,} engagements · first seen {age_str(now, cl.first_seen)} ago{dev}", body))
            story.append(Paragraph("Sources: " + escape(", ".join(sorted(cl.sources))[:600]), small))
            links = [m["url"] for m in sorted(cl.members, key=lambda m: -int(m.get("engagement") or 0))
                     if m.get("url")][:4]
            for u in links:
                story.append(Paragraph(f"<link href='{escape(u)}'>{escape(u[:110])}</link>", small))
            story.append(Spacer(1, 8))
        if not trends:
            esc = res.escalation or {}
            if esc.get("done"):
                story.append(Paragraph(
                    "No trend cleared verification <b>after escalation</b> (searched: "
                    + escape("; ".join(esc.get("queries", []))) + ").", body))
            else:
                story.append(Paragraph(
                    "<b>ESCALATION PENDING</b>: not concluding 'nothing'. Searches still to run: "
                    + escape("; ".join(esc.get("queries", []))), body))
        wl = gate(res.watchlist, now, max_age)
        if wl:
            story += [Spacer(1, 6), Paragraph("Watchlist: NOT YET CORROBORATED", h2)]
            for t in wl:
                cl = t.cluster
                lead = cl.lead()
                src = lead.get("author") or lead.get("subreddit") or ""
                u = lead.get("url") or ""
                story.append(Paragraph(
                    f"• {escape(t.headline[:160])} <font color='#777777'>({escape(src)}, "
                    f"{lead.get('platform')}, {cl.engagement:,} eng, {age_str(now, cl.last_seen)} ago)</font> "
                    + (f"<link href='{escape(u)}'>link</link>" if u else ""), watch))
    doc.build(story)
    return path
