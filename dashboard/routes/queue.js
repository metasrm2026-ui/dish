// GET (routine) what the SocialPilot publisher needs:
//   queue:     approved drafts to schedule (oldest approval first), with approved_at. ever_sent: the
//              draft went to SocialPilot before (an earlier claim; or the draft it was resent from,
//              resent_from), so it may exist there already: the publisher looks for it
//              (prior_post_id first) before creating it again. held_scope: what a held draft waits for.
//              resent_from: a breaker flag left by that draft's failure is not this one's (bore/cli.py)
//   scheduled: drafts already scheduled (page, scheduled_for, post_id) - for spacing, and for
//              confirming delivery once their time has passed (caption_start, SocialPilot's own
//              post id and account when recorded)
//   socialpilot: {down, error, since, last_try_at, last_ok_at} - while it is down, held drafts
//              wait for the next hourly try (bore/publish.py split_waiting)
import { isRoutine, deny } from '../lib/auth.js';
import { listAll, groupDrafts, loadDraft, getJson } from '../lib/store.js';
import { getRecord, mapLimit, READ_CONCURRENCY } from '../lib/db.js';
import { socialpilotHealth } from './result.js';

const SENT = ['publishing', 'scheduled', 'posted'];

export default async function handler(req, res) {
  if (!isRoutine(req)) return deny(res);
  const all = groupDrafts(await listAll());
  const pick = st => all.filter(d => st.includes(d.status));
  const pages = {};
  const loginOf = async slug => {                      // the page's SocialPilot account (admin panel); null: unreadable
    if (!(slug in pages)) pages[slug] = await getRecord('pages', slug).then(p => (p && p.socialpilot_login_id) || '').catch(() => null);
    return pages[slug];
  };
  // the SocialPilot post an earlier claim made (its latest scheduled or posted record)
  const priorPost = async d => {
    const s = [...d.statuses].reverse().find(x => x.status === 'scheduled' || x.status === 'posted');
    const r = s ? ((await getJson(s.url).catch(() => ({}))).result || {}) : {};
    return r.sp_post_id || r.post_id || null;
  };
  const queue = await mapLimit(pick(['approved']), READ_CONCURRENCY, async d => {   // a few at a time: hundreds at once get refused
    const x = await loadDraft(d.id);
    const login = await loginOf(x.page);
    // a post sent again under a new id (resent_from: the Studio's resend) counts what the old one did
    const prev = x.resent_from ? all.find(o => o.id === x.resent_from) : null;
    const sentOf = g => !!g && g.statuses.some(s => SENT.includes(s.status));
    const sent = sentOf(d) || sentOf(prev);
    // Studio posts carry the time the editor asked for (ms) and whether it was "post now"
    return { id: x.id, page: x.page, page_name: x.page_name, status: x.status, approved_at: x.approved_at,
             first_approved_at: x.first_approved_at, holds: x.holds, held_error: x.holds ? (x.statusBody.error || '') : '',
             held_scope: x.holds ? x.held_scope || '' : '',            // '' SocialPilot, 'account', 'draft' or 'run'
             caption: x.caption_final, image_url: x.image_url, requested_at: x.requested_at, mode: x.publish_mode,
             image_urls: [x.image_url, ...(x.extra_images || [])],     // all of them, in order (one for most posts)
             studio: !!x.studio, socialpilot_login_id: login || '', ...(login === null ? { login_error: true } : {}),
             update_of: x.update_of || null, created_at: x.created_at || null, resent_from: x.resent_from || null,
             ever_sent: sent, prior_post_id: sent ? (sentOf(d) && await priorPost(d)) || (sentOf(prev) && await priorPost(prev)) || null : null };
  });
  queue.sort((a, b) => (a.first_approved_at || a.approved_at || 0) - (b.first_approved_at || b.approved_at || 0));
  const scheduled = await mapLimit(pick(['scheduled']), READ_CONCURRENCY, async d => {
    const x = await loadDraft(d.id);
    const r = (x.statusBody && x.statusBody.result) || {};
    // sp_post_id: the id ViewPost takes: the queue entry's, else CreatePost's own (the same kind of id)
    return { id: x.id, page: x.page, page_name: x.page_name, scheduled_for: r.scheduled_for || null, post_id: r.post_id || null,
             sp_post_id: r.sp_post_id || r.post_id || null, sp_login_id: r.sp_login_id || null, socialpilot_login_id: (await loginOf(x.page)) || '',
             caption_start: String(x.caption_final || '').slice(0, 40), update_of: x.update_of || null };
  });
  // claimed by a publisher run that has not reported back yet (its planned time counts as taken)
  const in_progress = await mapLimit(pick(['publishing']), READ_CONCURRENCY, async d => {
    const x = await loadDraft(d.id);
    const r = (x.statusBody && x.statusBody.result) || {};
    return { id: x.id, page: x.page, page_name: x.page_name, scheduled_for: r.scheduled_for || null, claimed_at: x.statusBody && x.statusBody.at,
             caption: x.caption_final, socialpilot_login_id: (await loginOf(x.page)) || '', update_of: x.update_of || null };
  });
  res.status(200).json({ queue, scheduled, in_progress, socialpilot: await socialpilotHealth() });
}
