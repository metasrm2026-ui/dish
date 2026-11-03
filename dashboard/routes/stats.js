// GET (routine or reviewer) ?since=<unix>&page=<slug>: draft counts by current status, so the
// routine can count what each page already has today against its daily cap (totals: all of them).
// `publisher`: whether SocialPilot is currently unavailable (held: approved posts are waiting for
// it) or refusing posts (the latest publish failure is newer than the latest success), with its
// error, so the dashboard can tell editors before they approve.
import { isRoutine, isReviewer, deny } from '../lib/auth.js';
import { listAll, groupDrafts, getJson, pageOf } from '../lib/store.js';
import { listRecords } from '../lib/db.js';
import { socialpilotHealth, needsAction } from './result.js';
import { pageScope, inScope } from '../lib/tenancy.js';

// what the routine learned, for the editors' banner: kind labels and counts only (no examples),
// what their notes keep saying and the photos they keep rejecting
async function lessonsSummary(scope) {
  const pages = Object.fromEntries((await listRecords('pages')).filter(p => inScope(scope, p.slug)).map(p => [p.slug, p.name]));
  return (await listRecords('lessons')).filter(l => pages[l.page]).map(l => {
    const kinds = Object.values(l.kinds || {}).filter(k => k.n >= 5);
    const themes = (l.note_themes || []).slice(0, 3);
    const photos = Object.values(l.photos || {}).filter(f => f.rule).slice(0, 2);
    const lines = [
      ...[
        ...kinds.filter(k => k.status === 'avoid' || (k.mult && k.mult <= 0.85)).map(k => `fewer ${String(k.label || '').toLowerCase()} (${k.approved}/${k.n} okay)`),
        ...kinds.filter(k => k.status === 'prefer' || (k.mult && k.mult >= 1.15)).map(k => `more ${String(k.label || '').toLowerCase()} (${k.approved}/${k.n} okay)`),
        ...Object.entries(l.templates || {}).filter(([, t]) => t.status === 'avoid').map(([n, t]) => `no '${n}' cards (${t.approved}/${t.n} okay)`),
      ].slice(0, 4),
      ...(themes.length ? [`your notes: ${themes.map(t => `${t.label} (${t.count})`).join(', ')}`] : []),
      ...photos.map(f => `fewer photos like ${f.label} (${f.image_rejected >= 2 ? `${f.image_rejected} of ${f.approved + f.image_rejected} not okay for the image`
                                                                       : `passed over ${f.passed}×, picked ${f.picked}× in the Studio`})`),
    ];
    return { page: l.page, page_name: pages[l.page], lines };
  });
}

// bore/publish.py OWN_MESSAGES, and the waits result.js ends that are not SocialPilot refusing posts
const OWN_FAILURE = /^(superseded by|publisher run interrupted|not delivered|unscheduled by socialpilot|waited too long|earlier copy|created but not|page has no socialpilot account|lookups? failed|scheduled on the wrong socialpilot account|the publisher was not allowed)/i;
const OWN_READS = 8;       // status bodies read at most per request

export async function publisherHealth(grouped, now = Date.now(), health = null) {
  const sp = health || await socialpilotHealth();
  const waiting = grouped.filter(d => d.status === 'approved').length;
  // SocialPilot itself was down at the last try: approved posts wait and are tried about hourly.
  // With nothing left to try and no recent try, it may be back already (stale): the next approval tests it.
  if (sp.down) {
    const error = String(sp.error || '').slice(0, 300);
    return { blocked: true, held: true, stale: !waiting && now - (sp.last_try_at || 0) > 2 * 3600e3,
             locked: /\blocked\b/i.test(error), error, since: sp.since || null,
             last_failure_at: sp.last_try_at || null, last_ok_at: sp.last_ok_at || null, waiting,
             hold_hours: Number(process.env.PUBLISH_HOLD_HOURS || 12) };
  }
  const since = now - 7 * 86400e3;
  let ok = null;
  const after = [];
  for (const d of grouped) for (const s of d.statuses) {
    if (s.at >= since && (s.status === 'scheduled' || s.status === 'posted') && (!ok || s.at > ok.at)) ok = s;
  }
  for (const d of grouped) for (const s of d.statuses) if (s.status === 'failed' && s.at >= since && (!ok || s.at > ok.at)) after.push(s);
  // Only SocialPilot refusing a post means "posts you approve now will fail": not the publisher's
  // own verdicts (a page with no account, a post SocialPilot lost, a copy on the wrong account, an
  // earlier copy, lookups that failed). The newest few are read; older ones count as failures.
  after.sort((a, b) => b.at - a.at);
  const read = await Promise.all(after.slice(0, OWN_READS).map(s => getJson(s.url).catch(() => ({}))));
  let fail = null, body = {}, failsAfterOk = Math.max(0, after.length - OWN_READS);
  read.forEach((b, i) => {
    if (b.check_facebook || OWN_FAILURE.test(String(b.error || '').trim())) return;
    failsAfterOk++;
    if (!fail) { fail = after[i]; body = b; }
  });
  if (!fail) return { blocked: false, last_ok_at: ok ? ok.at : null };
  const error = String(body.error || '').slice(0, 300);
  return { blocked: true, locked: /\blocked\b/i.test(error), error, last_failure_at: fail.at, last_ok_at: ok ? ok.at : null,
           failed_since_ok: failsAfterOk };
}

export default async function handler(req, res) {
  if (!isRoutine(req) && !isReviewer(req)) return deny(res);
  const since = Number(req.query.since || 0);
  const page = req.query.page ? String(req.query.page) : null;
  const counts = {}, totals = {};      // totals: whenever made, as the review page's tabs list them
  // a person counts only the posts of the pages they may use (lib/tenancy.js: their workspace's, an
  // editor's own), and sees only their notes and lessons; the product owner and the routines every page
  const scope = await pageScope(req);
  const theirs = pg => inScope(scope, pg);
  const grouped = groupDrafts(await listAll()).filter(d => theirs(d.page));
  for (const d of grouped) {
    if ((page && d.page !== page) || !theirs(d.page)) continue;
    totals[d.status] = (totals[d.status] || 0) + 1;
    if (d.createdAt < since) continue;
    const c = (counts[d.page] ||= {});
    c[d.status] = (c[d.status] || 0) + 1;
  }
  // needs_action: what only a person can do in SocialPilot or on Facebook (result.js), shown in red.
  // Notes that could not be read are said so (needs_action_unknown), never shown as no notes.
  const notes = await needsAction({ strict: true }).catch(() => null);
  const needs_action = Object.fromEntries(Object.entries(notes || {}).filter(([id]) => theirs(pageOf(id))));   // by the id: notes from before the '-2' fix carry a wrong page
  res.status(200).json({ since, counts, totals, publisher: { ...(await publisherHealth(grouped)), needs_action, ...(notes ? {} : { needs_action_unknown: true }) },
                        lessons: isReviewer(req) ? await lessonsSummary(scope).catch(() => []) : undefined });
}
