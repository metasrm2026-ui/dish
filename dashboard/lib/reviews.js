// Review ledger: every editor verdict on a post, kept (encrypted, append-only) so the routines can
// learn from all of them, not only the drafts still stored (drafts are deleted after 14 days).
//   verdict: approved | rejected | cleared (selected posts by hand; clears by age or at midnight are not
//            verdicts) | restored | withdrawn (Not okay on an approved post SocialPilot refused)
//            | studio_pick (an editor chose the topic in the Studio; recorded again, with the photo,
//            when they set its image: the newest entry per post and verdict wins) | studio_sent
//            | feedback (an editor sent a post back with a note to redo it)
//   photo: the photo the post used (which candidate, what kind; Studio picks also the candidates
//          shown above it, `passed`), so the routine learns which photos editors keep
import { appendLog, readLogs } from './db.js';
import { createdFromId } from './store.js';

const cut = (s, n) => String(s ?? '').replace(/\s+/g, ' ').trim().slice(0, n);

export function photoEntry(p) {
  if (!p || typeof p !== 'object') return null;
  const one = x => ({ source: cut(x.source, 40), note: cut(x.note, 30), consensus: Number(x.consensus) || 0,
                      flags: (Array.isArray(x.flags) ? x.flags : []).slice(0, 4).map(f => cut(f, 80)) });
  return { ...one(p), rank: Number.isInteger(p.rank) ? p.rank : null, of: Number(p.of) || 0, via: cut(p.via, 160),
           ...(Array.isArray(p.passed) && p.passed.length ? { passed: p.passed.slice(0, 6).map(one) } : {}) };
}

// drafts made before they recorded `photo`: the chosen one among the candidates their brief kept
function draftPhoto(d) {
  if (d.photo) return d.photo;
  const photos = (d.brief || {}).photos, i = (d.copy || {}).photo_index;
  return Array.isArray(photos) && Number.isInteger(i) && i >= 0 && i < photos.length ? { ...photos[i], rank: i, of: photos.length } : null;
}

export function reviewEntry(verdict, d, extra = {}) {
  const copy = d.copy || {};
  const photo = photoEntry(extra.photo || draftPhoto(d));
  return {
    verdict, page: cut(d.page, 60), draft_id: cut(d.id || d.draft_id, 120),
    kind: cut(d.kind || copy.kind || extra.kind, 40), kind_rank: cut(d.kind_rank || extra.kind_rank, 40),
    template: cut(copy.template || d.template, 40),
    headline: cut(d.headline || d.headline_hint, 160), route: cut((d.verification || {}).route || d.route, 20),
    excerpt: cut(d.excerpt || d.caption_final || d.caption || d.summary, 240),
    niche_fit: Number.isFinite(Number(d.niche_fit)) ? Number(d.niche_fit) : null,
    created_at_unix: d.created_at_unix || (d.id ? createdFromId(d.id) : null) || null,
    reason: cut(extra.reason, 60), detail: cut(extra.detail, 60), note: cut(extra.note, 300), by: cut(extra.by, 120),
    decided_at: extra.decided_at || Date.now(), ...(extra.backfill ? { backfill: true } : {}),
    ...(photo ? { photo } : {}),
  };
}

export async function recordReview(verdict, draft, extra = {}) {
  try { await appendLog('review', reviewEntry(verdict, draft, extra)); } catch (e) { /* never block a decision */ }
}

// one entry per post and verdict; every redo request is its own (a draft whose regeneration failed
// goes back to Waiting and can be sent back again with new words)
export const reviewKey = e => `${e.draft_id}|${e.verdict}` + (e.verdict === 'feedback' ? `|${Number(e.decided_at) || 0}` : '');

// the newest entry per key wins (a re-backfill can improve older entries)
export async function readReviews(days = 180) {
  const all = await readLogs('review', Math.min(Math.max(1, Number(days) || 180), 365));
  const seen = new Set();
  return all.filter(e => { const k = reviewKey(e); if (seen.has(k)) return false; seen.add(k); return true; });
}
