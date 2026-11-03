// /api/studio/*  the editor workflow (sections 2 and 4): page -> topics -> ideas -> visual ->
// caption -> optional check -> schedule / post. Every AI step is done by a Claude routine:
// the hourly routine prepares each page's story feed (summaries, two visual ideas, candidate
// photos) so topic selection and ideation are instant; composing, captions and the check run as
// Studio jobs (STUDIO.md), picked up by the Studio routine.
import { getRecord, updateRecord, createRecord, listRecords, listBlobs, putImage, appendLog, fetchBlob, mapLimit, READ_CONCURRENCY, StorageChallenged } from '../lib/db.js';
import { put } from '../lib/blob.js';
import { recordReview } from '../lib/reviews.js';
import { canUsePage } from '../lib/session.js';
import { saveDraft, setStatus, loadDraft, listAll, groupDrafts, getJson } from '../lib/store.js';
import { fire } from '../lib/fire.js';
import { audit } from '../lib/audit.js';
import { createHash } from 'node:crypto';
import { randomId } from '../lib/crypto.js';
import { ok, bad, clean } from '../lib/http.js';
import { postText, cutText, CAPTION_MAX } from '../lib/caption.js';
import { needsAction, setNeedsAction } from './result.js';
import { takeBack } from './decide.js';
import { approvedSpecs } from './templates.js';

const MAX_STORIES = 5;
const GAP_MIN = Number(process.env.SCHEDULE_GAP_MINUTES || 45);
// Posts are never sent straight away: "Post now" means the earliest allowed slot. The owner's rule
// is one hour after approval; lower STUDIO_MIN_LEAD_MINUTES (and the publisher's
// studio_min_lead_minutes) to make "Post now" quicker.
const MIN_LEAD_MIN = Number(process.env.STUDIO_MIN_LEAD_MINUTES || 60);
const UA = { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36' };

export const POST_STATUS = ['draft', 'ideated', 'visual_ready', 'captioned', 'checked', 'check_skipped', 'scheduled', 'published', 'failed'];
// publish states of a post that is with the publisher ('sending': a Send has started, see publish)
const SENT = ['approved', 'publishing', 'scheduled', 'posted'];
const isImage = buf => buf.length > 8 && (buf.subarray(0, 3).toString('hex') === 'ffd8ff' || buf.subarray(0, 4).toString('hex') === '89504e47');
const since = ms => { const m = Math.round((Date.now() - (ms || 0)) / 60e3); return m < 1 ? 'just now' : m < 120 ? `${m} min ago` : `${Math.round(m / 60)} hours ago`; };

// an idea drawn with one of the page's templates, taken out of use since the ideas were made: its
// visual can only fail (the routine draws approved templates only, and never another one instead)
const RETIRED = 'the template of this idea was taken out of use: pick the other idea, write your own, or regenerate the ideas';
async function retiredSpec(page, spec) {
  return !!spec && !approvedSpecs(await getRecord('templates', page)).some(t => t.id === spec);
}

function storyOut(s) {
  return { id: s.id, headline: s.headline, summary: s.summary || '', sources: (s.sources || []).map(x => ({ name: x.name, url: x.url })),
           published_at: s.published_at || null, url: s.url || '', score: s.score || 0, from_url: !!s.from_url,
           kind: s.kind || '', learned: s.learned || '', subject: s.subject || '',
           ideas: (s.ideas || []).map(i => ({ title: i.title, template: i.template, layout: i.layout || '', description: i.description,
                                              ...(i.template_spec ? { template_spec: i.template_spec } : {}) })), images: (s.images || []).map(i => ({ url: i.url, source: i.source, credit: i.credit || '', size: i.size || '',
             via: i.via || '', post_url: i.post_url || '', note: i.note || '', consensus: i.consensus || 0, flags: i.flags || [], ...faceId(i) })),
           images_v: s.images_v || 0 };
}

// Whose face a candidate shows (bore/faceid.py): match = the story's subject, mismatch = may not be
// them (a clear face that is someone else's); neutral images carry nothing.
export function faceId(i) {
  if (!['match', 'mismatch'].includes(i.id_match) || !i.id_name) return {};
  return { id_match: i.id_match, id_score: Math.round((Number(i.id_score) || 0) * 100) / 100, id_name: clean(i.id_name, 80),
           id_ref: ['espn', 'wikipedia'].includes(i.id_ref) ? i.id_ref : '' };
}

async function pageFor(req, res, slug) {
  const p = slug && await getRecord('pages', slug);
  if (!p || p.archived) { bad(res, 'no such page', 404); return null; }
  if (!await canUsePage(req.user, slug)) { bad(res, 'you do not have access to this page', 403); return null; }
  return p;
}

async function batchFor(req, res, id) {
  const bt = id && await getRecord('batches', id);
  if (!bt) { bad(res, 'no such batch', 404); return null; }
  if (!await canUsePage(req.user, bt.page)) { bad(res, 'you do not have access to this page', 403); return null; }
  return bt;
}

// The status a post has reached, from what it holds (so it can never get out of step).
export function postStatus(p) {
  if (p.publish && p.publish.state === 'posted') return 'published';
  if (p.publish && p.publish.state === 'failed') return 'failed';
  if (p.publish && ['approved', 'publishing', 'scheduled'].includes(p.publish.state)) return 'scheduled';
  if (p.check && p.check.status === 'done') return 'checked';
  if (p.check && p.check.status === 'skipped') return 'check_skipped';
  if (p.final_image_url && p.caption) return 'captioned';
  if (p.final_image_url) return 'visual_ready';
  if (p.idea) return 'ideated';
  return 'draft';
}

// The photo an editor set for a post: which candidate (its rank among those shown) or their own
// upload. With `cands`, also the candidates shown above it (the first three for an upload): the
// ones they looked past. The routine learns from both which photos editors keep.
function pickedPhoto(p, cands) {
  const bi = p.base_image;
  if (!bi) return null;
  const up = bi.source === 'upload', rank = Number.isInteger(bi.rank) ? bi.rank : null;
  return { source: up ? 'uploaded' : bi.source || '', via: bi.via || '', note: bi.note || '', flags: bi.flags || [],
           consensus: bi.consensus || 0, rank, of: bi.of || 0, ...(cands ? { passed: cands.slice(0, up ? 3 : rank || 0) } : {}) };
}

// Refresh publish states from the review store (the publisher routine reports there).
async function withPublishState(bt) {
  const drafts = {};
  for (const p of bt.posts) {
    if (!p.publish || !p.publish.draft_id || p.publish.state === 'posted') continue;
    const d = await loadDraft(p.publish.draft_id).catch(() => null);
    if (d) drafts[p.publish.draft_id] = d;
  }
  const refresh = b => {
    let changed = false;
    for (const p of b.posts) {
      const d = p.publish && p.publish.state !== 'posted' && drafts[p.publish.draft_id];
      if (!d) continue;
      const r = (d.statusBody && d.statusBody.result) || {};
      const sb = d.statusBody || {};
      // withdrawn on the review page: who did and why, so the Studio does not show it as never sent
      const next = { ...p.publish, state: d.status, scheduled_for: r.scheduled_for || p.publish.scheduled_for || null,
                     error: sb.error || '', held: !!sb.held,
                     ...(d.status === 'rejected' ? { withdrawn: !!sb.withdrawn, by: clean(sb.by, 120), reason: clean(sb.reason, 200) } : {}) };
      if (JSON.stringify(next) !== JSON.stringify(p.publish)) { p.publish = next; changed = true; }
    }
    for (const p of b.posts) { const s = postStatus(p); if (s !== p.status) { p.status = s; changed = true; } }
    return changed;
  };
  if (!refresh(bt)) return bt;
  // the drafts took a while to read: the change is made on the newest copy of the batch
  return (await updateRecord('batches', bt.id, cur => (cur && refresh(cur) ? cur : null))) || bt;
}

// Sites cut their preview text (og:description) at ~160 characters, often mid-sentence. Keep the
// whole sentences, or mark the cut with "…"; the Studio routine writes the full summary (ideas job).
// A full stop after an initial or an abbreviation (J.J., Jr., St., U.S.) does not end a sentence.
const ABBREV = /(?:\b[A-Z]|\b(?:Jr|Sr|St|Mr|Mrs|Ms|Dr|vs|No|Gen|Lt|Sgt|Inc|Co|Ltd|U\.S|U\.K))\.$/;
export const isCut = t => !t || /(\.\.\.|…|\[…\]|\[\.\.\.\])$/.test(t) || !/([.!?"'”’)\]।。！？]|\p{Extended_Pictographic}\uFE0F?)$/u.test(t);
export function wholeSentences(text) {
  const t = decodeEntities(String(text || '')).trim();
  if (!t || !isCut(t)) return t;
  let end = -1;
  for (const m of t.matchAll(/[.!?](?=\s+["'“‘]?[A-Z0-9])/g)) if (!ABBREV.test(t.slice(0, m.index + 1))) end = m.index;
  const kept = end >= 60 ? t.slice(0, end + 1) : t.replace(/(\.\.\.|…)$/, '');
  return end >= 60 ? kept : `${kept}…`;
}
export function decodeEntities(t) {
  const named = { amp: '&', quot: '"', apos: "'", lt: '<', gt: '>', nbsp: ' ', hellip: '…', rsquo: '’', lsquo: '‘', rdquo: '”', ldquo: '“', mdash: '—', ndash: '–' };
  return String(t || '').replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(Number(d))).replace(/&([a-z]+);/gi, (m, n) => named[n.toLowerCase()] ?? m);
}

async function readUrl(url) {
  const u = new URL(url);
  const host = u.hostname.replace(/^www\./, '');
  // one id per link: the same link pasted twice is one story
  const story = { id: `url-${createHash('sha1').update(u.href).digest('hex').slice(0, 10)}`, url: u.href, headline: '', summary: '', sources: [{ name: host, url: u.href }],
                  published_at: null, from_url: true, ideas: [], images: [] };
  const ctl = AbortSignal.timeout(9000);
  try {
    if (/(^|\.)(x|twitter)\.com$/.test(host)) {
      const r = await fetch(`https://publish.twitter.com/oembed?omit_script=1&url=${encodeURIComponent(u.href)}`, { signal: ctl, headers: UA });
      const j = await r.json();
      const text = String(j.html || '').replace(/<a[^>]*>pic\.twitter\.com\/\w+<\/a>/g, '').replace(/<br\s*\/?>/g, '\n').replace(/<[^>]+>/g, ' ')
        .replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/\s+/g, ' ').trim();
      story.headline = text.slice(0, 200);
      story.summary = text.slice(0, 400);
      story.sources = [{ name: `@${u.pathname.split('/')[1] || 'x'}`, url: u.href }];
    } else {
      const r = await fetch(u.href, { signal: ctl, headers: UA, redirect: 'follow' });
      const html = (await r.text()).slice(0, 600000);
      const meta = k => { const m = html.match(new RegExp(`<meta[^>]+(?:property|name)=["']${k}["'][^>]*content=(["'])(.*?)\\1`, 'i'))
                                    || html.match(new RegExp(`<meta[^>]+content=(["'])(.*?)\\1[^>]+(?:property|name)=["']${k}["']`, 'i'));
                          return m ? decodeEntities(m[2]) : ''; };
      story.headline = meta('og:title') || (html.match(/<title[^>]*>([^<]+)/i) || [])[1] || '';
      story.summary = wholeSentences(meta('og:description') || meta('description'));
      story.published_at = Date.parse(meta('article:published_time')) || null;
      const img = meta('og:image') || meta('twitter:image');
      if (img) story.images = [{ url: new URL(img, u.href).href, source: 'link', credit: host }];
    }
  } catch { /* the Studio routine reads it properly (ideas job) */ }
  story.headline = clean(story.headline, 300) || u.href;
  return story;
}

// A job a session claimed but has not finished in 20 minutes (the longest real run took 11) is
// offered again: the session died.
export const JOB_STALE_MS = 20 * 60e3;
export { postText };
const OPEN = ['queued', 'running'];
// A post with the publisher: routine results no longer change it. So does a Send under way (it has
// read the post's visual and caption and is making the draft: under a minute), but not a 'sending'
// left behind by a Send cut short: that post was not sent, and the editor may still change it.
const SEND_MS = 2 * 60e3;
export const isSent = p => !!(p.publish && SENT.includes(p.publish.state));
export const sendingNow = p => !!(p.publish && ['sending', 'pending'].includes(p.publish.state) && Date.now() - (p.publish.sending_at || 0) < SEND_MS);
export const sentOrSending = p => isSent(p) || sendingNow(p);
export { SENT };
// a failure that does not prove the post is missing (review.html mayExist): SocialPilot lost track
// of it, or the call broke off
export const mayExist = sb => !!sb.check_facebook
  || /not delivered|interrupted|created but not|lookups? failed|error uploading media|timed? ?out|timeout/i.test(sb.error || '');
// The newest job of each kind queued for a post (p.jobs): a result of an older one is out of date.
// Posts from before p.jobs: the job on its line, when it is of that kind.
export const newestJob = (p, kind) => (p.jobs && p.jobs[kind]) || (p.job && p.job.kind === kind ? p.job.id : null);
// a job's result is still wanted for the post: no newer job of its kind was queued there (image
// searches add up: each one's photos are kept)
export const wanted = (p, job) => { const n = job.kind === 'images' ? null : newestJob(p, job.kind); return !n || n === job.id; };
// A check asked for again keeps the finished one before it (previous) while it waits: a re-check that
// fails, is stopped or brings nothing for the post gives it back, findings and all
export const queuedCheck = c => { const prev = c && c.status === 'done' ? c : c && c.previous; return { status: 'queued', ...(prev ? { previous: prev } : {}) }; };
export const checkBack = c => (c && c.previous) || { status: 'none' };
// what a job's session was given for a post when it claimed it (the result is checked against it)
export const madeFrom = p => ({ photo: p.base_image ? p.base_image.url : '', visual: p.final_image_url || '', text: postText(p) });

// A job nobody picked up (a session that failed to start or is still starting after 4 minutes, or
// one that finished just before it was queued), or one a session took and then dropped, is started
// again while the editor's page waits for it: every 4 minutes, at most 5 times. Each restart is a
// file only one page can create (job-nudges/<id>/<n>.json): two editors' pages polling at once
// start one session, and the job record is never written from here.
const NUDGE_MS = 4 * 60e3;
const claimedLately = async id => (await listBlobs(`job-claims/${id}/`)).some(c => Date.now() - new Date(c.uploadedAt) < JOB_STALE_MS);

// A job no Studio machine could start or finish (sessions that never started, machines that could not
// take it, a session that died with it) fails with a message, instead of leaving the editor waiting.
// Never while a session has it: queued, or running past the 20 minutes, and no claim younger than that.
// detail: what went wrong technically, kept for the admin (the editor reads error)
export async function failStalled(id, error, detail = '') {
  const job = await getRecord('jobs', id).catch(() => null);
  if (!job || !(job.status === 'queued' || (job.status === 'running' && Date.now() - (job.started_at || 0) > JOB_STALE_MS))) return false;
  if (await claimedLately(id)) return false;
  const msg = clean(error, 600);
  // the record first: a post that misses the change is put right from it on the next poll (settle)
  const rec = await updateRecord('jobs', id, cur => (cur && OPEN.includes(cur.status) ? { ...cur, status: 'failed', error: msg, ...(detail ? { detail: clean(detail, 300) } : {}), done_at: Date.now() } : null));
  if (!rec || rec.status !== 'failed') return false;
  await settle(job.batch_id, rec);
  return true;
}

export async function nudgeJobs(bt, seen = new Map()) {   // true when it failed a job (the batch changed); also the watchdog's (routes/health.js)
  let failed = false;
  // the jobs the posts wait for: the one on each post's line, and any still under way behind another
  // job's line (p.under_way: the editor searched for photos while the visual was being made); true
  // also when one that ended was put right on its posts. seen: the open ones as read (for the page)
  const ids = [...new Set(bt.posts.flatMap(p => [...(p.job && OPEN.includes(p.job.status) ? [p.job.id] : []), ...(p.under_way || []).map(j => j.id)]))];
  const restarted = [];
  // a job whose reads storage refused waits for the next round, and never keeps the jobs counted
  // before it from their session (each restart counted with none started used up their 5)
  for (const id of ids) try {
    const job = await getRecord('jobs', id).catch(() => undefined);
    if (job === undefined) continue;                                   // storage did not answer: next time
    if (!job || !OPEN.includes(job.status)) { failed = await settle(bt.id, job || { id }) || failed; continue; }
    seen.set(id, job);
    // replaced by a newer job of its kind on every post, or of a batch the editors cleared (its
    // result would not be used): never restarted, and cancelled once no session has it
    if (bt.cleared || bt.posts.filter(p => job.post_ids.includes(p.id)).every(p => !wanted(p, job))) {
      if (job.status === 'running' && (Date.now() - (job.started_at || 0) < JOB_STALE_MS || await claimedLately(id))) continue;
      const rec = await updateRecord('jobs', id, cur => (cur && cur.status === job.status ? { ...cur, status: 'cancelled', done_at: Date.now() } : null));
      if (rec && rec.status === 'cancelled') failed = await settle(bt.id, rec) || failed;
      continue;
    }
    const due = job.status === 'queued' ? Math.max(job.created_at || 0, job.released_at || 0) + NUDGE_MS   // handed back: its session takes it again
      : (job.started_at || 0) + JOB_STALE_MS;
    if (Date.now() < due) continue;
    const nudges = (await listBlobs(`job-nudges/${id}/`)).sort((x, y) => new Date(y.uploadedAt) - new Date(x.uploadedAt));
    if (nudges.length && Date.now() - new Date(nudges[0].uploadedAt) < NUDGE_MS) continue;
    if (nudges.length >= 5) {                                          // restarted 5 times: tell the editor
      // how long the editor waited (a session that died with the job: about 40 minutes)
      const waited = job.created_at ? ` (about ${Math.max(1, Math.round((Date.now() - job.created_at) / 60e3))} minutes)` : '';
      failed = await failStalled(id, `The Studio routine could not do this step after 5 restarts${waited}. `
                                     + 'Press the button again; if it keeps failing, tell the admin.') || failed;
      continue;
    }
    try {
      await put(`job-nudges/${id}/${nudges.length}.json`, JSON.stringify({ at: Date.now(), status: job.status }),
                { access: 'public', contentType: 'application/json', addRandomSuffix: false, allowOverwrite: false });
    } catch (e) { continue; }                                          // another page is restarting it
    restarted.push({ job, n: nudges.length + 1 });
  } catch (e) { /* storage did not answer: this job next time */ }
  // One session for all of them: it takes the waiting jobs one after another, and the next round
  // starts one more if they still wait (5 visuals waiting used to start 5 sessions every 4 minutes,
  // up to 30 per click, most of which found nothing to do). Each job keeps its own count of restarts.
  if (restarted.length) {
    const f = await fire('STUDIO', `Studio job${restarted.length > 1 ? 's' : ''} ${restarted.map(r => `${r.job.id} (${r.job.kind})`).join(', ')} for ${bt.page} `
                                   + `${restarted.length > 1 ? 'are' : 'is'} still waiting: process the queued Studio jobs (STUDIO.md).`);
    for (const { job, n } of restarted)
      await appendLog('studio_nudges', { job: job.id, kind: job.kind, page: job.page, status: job.status, n,
                                          fired: !!f.fired, fire_status: f.status || null, fire_error: f.error || '' }).catch(() => {});
  }
  return failed;
}

// A job a session took and then stopped answering (running past the 20 minutes, no claim since):
// the page says so and offers to start it again (openJob cancels and replaces it), instead of
// waiting up to 40 minutes for its restarts to run out. Only in the answer: nothing is written.
export function markStalled(bt, seen) {
  const stalled = j => { const job = j && seen.get(j.id); return !!job && job.status === 'running' && Date.now() - (job.started_at || 0) > JOB_STALE_MS; };
  const mark = j => (stalled(j) ? { ...j, stale: true } : j);
  if (!bt.posts.some(p => stalled(p.job) || (p.under_way || []).some(stalled))) return bt;
  return { ...bt, posts: bt.posts.map(p => ({ ...p, job: mark(p.job), ...(p.under_way ? { under_way: p.under_way.map(mark) } : {}) })) };
}

// A job that ended (done, failed, cancelled; or whose record is gone) while a post still follows it.
// A line that still says queued or running (the result's write was lost) takes the record's status,
// and a result that never arrived says so, so the editor presses the button again instead of
// watching it spin for ever. A job under way behind another job's line is no longer followed; if it
// failed or its result was lost, its line comes back once the post's own line is idle.
const arrived = (p, job) => (job.kind === 'compose' ? p.card_job === job.id : job.kind === 'ideas' ? !!(p.story.ideas || []).length
  : job.kind === 'check' ? !!(p.check && p.check.status === 'done') : true);
async function settle(batchId, job) {
  if (!batchId) return false;                                          // a page's templates job: no posts follow it
  const ended = !!job.status && !OPEN.includes(job.status);
  let changed = false;
  await updateRecord('batches', batchId, bt => {
    changed = false;
    if (!bt) return null;
    for (const p of bt.posts) {
      const behind = (p.under_way || []).some(j => j.id === job.id);
      if (behind) { p.under_way = p.under_way.filter(j => j.id !== job.id); if (!p.under_way.length) delete p.under_way; changed = true; }
      if (!ended) continue;
      const note = job.status === 'done' && job.dropped && p.id in job.dropped ? job.dropped[p.id] : undefined;   // not used, on purpose
      const lost = job.status === 'done' && note === undefined && !arrived(p, job);
      const line = p.job && p.job.id === job.id && OPEN.includes(p.job.status);
      const back = behind && !(p.job && OPEN.includes(p.job.status)) && wanted(p, job) && (job.status === 'failed' || lost || !!note);
      if (!line && !back) continue;
      p.job = { id: job.id, kind: job.kind, status: job.status, ...(note ? { note } : {}), ...(job.detail ? { detail: job.detail } : {}),
                error: job.status === 'failed' ? job.error || 'unknown error' : lost ? 'the result did not arrive: press the button again' : '' };
      if (job.kind === 'check' && p.check && p.check.status === 'queued') p.check = checkBack(p.check);
      p.status = postStatus(p);
      changed = true;
    }
    return changed ? bt : null;
  });
  return changed;
}

// A job of this kind still open (queued or running) for the post: pressing the button again starts
// nothing, one session is on it already. Except: a visual being made from a photo the editor has
// replaced since (one from the new photo is wanted; the old one's result is dropped when it lands),
// an image search for other words (both run, their photos add up), and a job nobody is doing any
// more (running past the 20 minutes with no claim since, or waiting longer than its restarts take):
// that one is cancelled and a new one starts, so the button never stops working. One that other posts
// of the batch still wait for is left to them (nudgeJobs restarts it): this post gets a new one.
async function openJob(p, kind, feedback = '', bt = null) {
  const id = p && newestJob(p, kind);
  const job = id && await getRecord('jobs', id).catch(() => null);
  if (!job || !OPEN.includes(job.status)) return false;
  const from = (job.made_from || {})[p.id];
  if (kind === 'compose' && from && from.photo !== (p.base_image ? p.base_image.url : '')) return false;
  if (kind === 'images' && (job.feedback || '') !== feedback) return false;
  const idle = job.status === 'queued' ? Date.now() - Math.max(job.created_at || 0, job.released_at || 0) > 6 * NUDGE_MS
    : Date.now() - (job.started_at || 0) > JOB_STALE_MS && !await claimedLately(id);
  if (!idle) return true;
  if (bt && bt.posts.some(o => o.id !== p.id && job.post_ids.includes(o.id) && newestJob(o, kind) === id)) return false;
  await updateRecord('jobs', id, cur => (cur && cur.status === job.status && cur.started_at === job.started_at && cur.released_at === job.released_at
    ? { ...cur, status: 'cancelled', done_at: Date.now(), error: 'nobody was doing it: replaced by a new one' } : null));
  return false;
}

// edit(post): any other change to the job's posts, made in the same write (returns the saved batch).
// Posts with this kind of job open are left out (open); with none left, no job is made (job: null).
// The routine is started for it afterwards (wake), so several jobs are written first and then all
// started at once.
async function queueJob(req, bt, kind, postIds, extra = {}, edit = null) {
  const open = [];
  for (const pid of postIds) if (await openJob(bt.posts.find(p => p.id === pid), kind, extra.feedback || '', bt)) open.push(pid);
  const ids = postIds.filter(pid => !open.includes(pid));
  if (!ids.length) return { job: null, fired: false, batch: bt, open };
  const job = { id: `${bt.page}-${Date.now()}-${randomId(4)}`, kind, batch_id: bt.id, page: bt.page, post_ids: ids,
                user: req.user.email, status: 'queued', created_at: Date.now(), ...extra };
  await createRecord('jobs', job.id, job);
  // a post whose newest job of this kind changed since it was read: another click queued one first
  const before = Object.fromEntries(ids.map(pid => [pid, newestJob(bt.posts.find(p => p.id === pid), kind)]));
  let taken = [];
  const batch = await updateRecord('batches', bt.id, cur => {
    taken = [];
    if (!cur) return null;
    for (const p of cur.posts) if (ids.includes(p.id) && newestJob(p, kind) === before[p.id]) {
      if (edit) edit(p);
      // a job still under way whose line this one takes is followed from the post until it ends
      // (restarted if it stalls, its failure shown): never left with nobody waiting for it
      if (p.job && OPEN.includes(p.job.status))
        p.under_way = [...(p.under_way || []).filter(j => j.id !== p.job.id), { id: p.job.id, kind: p.job.kind, ...(p.job.photo ? { photo: p.job.photo } : {}) }].slice(-6);
      p.job = { id: job.id, kind, status: 'queued', error: '' };
      p.jobs = { ...(p.jobs || {}), [kind]: job.id };
      taken.push(p.id);
    }
    return taken.length ? cur : null;
  });
  if (taken.length < ids.length) {
    await updateRecord('jobs', job.id, cur => (cur && cur.status === 'queued'
      ? { ...cur, post_ids: taken, ...(taken.length ? {} : { status: 'cancelled', done_at: Date.now() }) } : null));
    if (!taken.length) return { job: null, batch: batch || bt, open: postIds };
  }
  return { job, batch: batch || bt, open };
}
async function wake(job) {
  const f = await fire('STUDIO', `Studio job ${job.id} (${job.kind}) for ${job.page}: process the queued Studio jobs (STUDIO.md).`);
  // a session may have claimed the job while the routine was starting (never undo that)
  await updateRecord('jobs', job.id, cur => (cur && cur.status === 'queued' ? { ...cur, fired: !!f.fired, fire_status: f.status || null, fire_error: f.error || '' } : null));
  return !!f.fired;
}

// The page's posts already with the publisher and when each goes out (the time SocialPilot has,
// else the one asked for). except: a Studio post's own draft (a Send cut short, sent again).
async function queuedTimes(slug, except = '', all = null) {
  const drafts = (all || groupDrafts(await listAll())).filter(d => d.page === slug && ['approved', 'publishing', 'scheduled'].includes(d.status)
                                                          && !(except && d.id.endsWith(`-studio-${except}`)));
  const out = [];
  for (const d of drafts) {
    const x = await loadDraft(d.id);
    const r = (x.statusBody && x.statusBody.result) || {};
    out.push({ d, x, t: Date.parse(r.scheduled_for || '') || (x.statusBody && x.statusBody.requested_at) || 0 });
  }
  return out;
}
// the earliest time from t that is at least GAP_MIN from every one of these (the publisher keeps a
// Studio time as it is given: the gap between a page's posts is kept here)
function freeSlot(t, taken) {
  for (let moved = true; moved;) {
    moved = false;
    for (const s of taken) if (Math.abs(t - s) < GAP_MIN * 60e3) { t = s + GAP_MIN * 60e3; moved = true; }
  }
  return t;
}

// A post sent again: its earlier drafts, newest first (the one it was last sent as, then the one each
// was sent again from: resent_from), at most 6. went: a publisher run claimed it, so a copy of it may
// be in SocialPilot (one that failed before a run took it, e.g. no account, cannot be there).
async function earlierDrafts(id) {
  const out = [];
  for (let at = id; at && out.length < 6 && !out.some(d => d.id === at);) {
    const d = await loadDraft(at);
    if (!d) break;
    const [g] = groupDrafts(await listAll(`drafts/${d.id}/`));
    out.push({ ...d, went: !!g && g.statuses.some(s => ['publishing', 'scheduled', 'posted'].includes(s.status)) });
    at = d.resent_from || '';
  }
  return out;
}
// one that may be in SocialPilot or on Facebook: it failed in a way that does not prove it is missing,
// or was retired (the post sent again) after such a failure
const mayBeThere = d => (d.status === 'failed' ? mayExist(d.statusBody || {}) : d.status === 'rejected' && !!(d.statusBody || {}).may_exist);
// hashtags as the editor types them ("eagles, #Hurts"): as they are kept
const TAGS_MAX = 30;
const tagsOf = (v, max = TAGS_MAX) => (Array.isArray(v) ? v : String(v ?? '').split(/[\s,]+/)).map(t => String(t).trim()).filter(Boolean)
  .map(t => (t.startsWith('#') ? t : `#${t}`)).slice(0, max);
const sameTags = (a, c) => tagsOf(a, Infinity).join(' ') === tagsOf(c || [], Infinity).join(' ');
const sameText = (a, c) => String(a || '').replace(/\s+/g, ' ').trim() === String(c || '').replace(/\s+/g, ' ').trim();
// a draft the review page redid (feedback) whose new draft was turned down: the Studio may send its post
const turnedDown = async id => { const d = id && await loadDraft(id); return !d || ['rejected', 'cleared'].includes(d.status); };

// The post's earlier draft, once this Send's draft is with the publisher (not before: a Send cut
// short leaves it in Failed, where reviewers see that the post did not go out). Still failed, it is
// retired, so the review page cannot send it as well (decide.js also refuses one whose post moved
// on). Approved again there while this Send was being made (two people at the same moment), it is
// taken back as Withdraw does; if a publisher run has it already, that one goes out and this Send's
// draft is taken back instead. Returns the draft the post goes out as.
async function settleEarlier(oldId, draftId, by) {
  const was = await loadDraft(oldId);
  const why = `resent from the Studio as ${draftId}`;
  if (was && was.status === 'failed')
    await setStatus(was.id, 'rejected', { reason: why, withdrawn: true, by, ...(mayExist(was.statusBody || {}) ? { may_exist: true } : {}) });
  if (!was || !SENT.includes(was.status) || (was.status === 'approved' && await takeBack(was, why, by))) return draftId;
  // its claim is taken: by the review page taking it back itself (decide.js), or by a run sending it
  const [c] = was.status === 'approved' ? await listBlobs(`claims/${was.id}/${was.approved_ms || 0}.json`) : [];
  if (c && /^withdrawn/.test((await getJson(c.url).catch(() => ({}))).run || '')) return draftId;
  const mine = await loadDraft(draftId);
  if (mine && mine.status === 'approved' && await takeBack(mine, `its earlier draft ${oldId} was sent again on the review page at the same moment`, by))
    return oldId;
  await setNeedsAction(draftId, was.page, `sent twice at the same moment (from the Studio, and on the review page as ${oldId}): delete one of the two copies in SocialPilot`);
  return draftId;
}

export default async function handler(req, res) {
  const u = req.user;
  if (!u) return bad(res, 'sign in first', 401);
  if (u.must_change) return bad(res, 'set a new password first', 403);
  const [what, id, sub, subId] = req.subpath;
  const b = req.body || {};

  if (what === 'pages' && req.method === 'GET') {
    const pages = (await listRecords('pages')).filter(p => !p.archived && (u.role === 'admin' || (u.pages || []).includes(p.slug)));
    return ok(res, { pages: pages.map(p => ({ slug: p.slug, name: p.name, theme: p.theme || {} })) });
  }

  // 4.3 topic selection: the page's feed (prepared by the hourly routine), filtered by window
  if (what === 'feed' && req.method === 'GET') {
    const p = await pageFor(req, res, String(req.query.page || ''));
    if (!p) return;
    const hours = [0, 3, 12, 24].includes(Number(req.query.hours)) ? Number(req.query.hours) : 12;
    const feed = await getRecord('feed', p.slug);
    const since = hours ? Date.now() - hours * 3600e3 : 0;          // 0: everything in the feed
    const stories = ((feed && feed.stories) || []).filter(s => (s.published_at || 0) >= since)
      .sort((a, c) => (c.score || 0) - (a.score || 0)).map(storyOut);
    return ok(res, { stories, updated_at: feed ? feed.updated_at : null, hours });
  }

  // 4.3 option 2: a pasted URL becomes a story (read here quickly; the routine reads it in full)
  if (what === 'url' && req.method === 'POST') {
    const p = await pageFor(req, res, String(b.page || ''));
    if (!p) return;
    let url;
    try { url = new URL(clean(b.url, 1000)).href; } catch { return bad(res, 'paste a full link starting with https://'); }
    return ok(res, { story: storyOut(await readUrl(url)) });
  }

  // suggested schedule slots for a page (4.8): after the latest post already queued for it
  if (what === 'schedule-hint' && req.method === 'GET') {
    const p = await pageFor(req, res, String(req.query.page || ''));
    if (!p) return;
    const all = groupDrafts(await listAll());
    const queued = await queuedTimes(p.slug, '', all);
    const latest = Math.max(0, ...queued.map(q => q.t));
    const first = Math.max(Date.now() + Math.max(60, MIN_LEAD_MIN) * 60e3, latest ? latest + GAP_MIN * 60e3 : 0);
    // now_at: the time "Post now" gets if sent now (publish below), shown on its label: with the
    // page's queue full that is hours away, not the hour the label used to promise
    const hint = { next: first, gap_minutes: GAP_MIN, latest_scheduled: latest || null, min_lead_minutes: MIN_LEAD_MIN,
                   now_at: freeSlot(Date.now() + MIN_LEAD_MIN * 60e3, queued.map(q => q.t).filter(Boolean)) };
    if (req.query.recent !== '1') return ok(res, hint);
    // with ?recent=1 (the Post step's first draw, not each redraw while a post is being sent): what
    // the page sent lately, so it can say "this page posted this Xh ago" (a note, never a block:
    // editors may want a follow-up). Bounded: the newest 30 posted in 36 hours, read 8 at a time
    // (each draft once per instance once it has settled)
    const posted = all.filter(d => d.page === p.slug && d.status === 'posted' && d.createdAt >= Date.now() / 1000 - 36 * 3600)
      .sort((a, c) => c.createdAt - a.createdAt).slice(0, 30);
    const sentAt = d => (d.statuses.length ? d.statuses[d.statuses.length - 1].at : d.createdAt * 1000);
    const recentOf = (d, x) => ({ id: d.id, status: d.status, at: sentAt(d), headline: clean((x.brief && x.brief.headline_hint) || x.headline, 200),
                                  text: clean(`${(x.brief && x.brief.headline_hint) || ''} ${x.headline || ''} ${x.caption || ''}`, 600) });
    const recent = queued.map(q => recentOf(q.d, q.x));
    const read = await mapLimit(posted, READ_CONCURRENCY, d => getJson(d.draftUrl, d.draftKey).catch(() => null));
    posted.forEach((d, i) => { if (read[i]) recent.push(recentOf(d, read[i])); });
    return ok(res, { ...hint, recent });
  }

  if (what === 'batches' && id === 'clear' && req.method === 'POST') {   // editors clear unfinished batches
    const ids = (b.ids || []).map(String).slice(0, 200);
    let n = 0;
    for (const bid of ids) {
      const bt = await getRecord('batches', bid);
      if (!bt || bt.cleared || !await canUsePage(u, bt.page)) continue;
      const waiting = new Set();
      for (const p of bt.posts) {                                      // drop its waiting Studio jobs
        for (const j of [...(p.job && p.job.status === 'queued' ? [p.job] : []), ...(p.under_way || [])]) {
          await updateRecord('jobs', j.id, job => (job && job.status === 'queued' ? { ...job, status: 'cancelled', done_at: Date.now() } : null));
          waiting.add(j.id);
        }
      }
      await updateRecord('batches', bt.id, cur => {
        if (!cur) return null;
        cur.cleared = { at: Date.now(), by: u.email };
        for (const p of cur.posts) if (p.job && waiting.has(p.job.id)) p.job = { ...p.job, status: 'cancelled' };
        return cur;
      });
      n++;
    }
    await audit(u, 'batches_cleared', { count: n });
    return ok(res, { cleared: n });
  }

  if (what === 'batches') {
    if (req.method === 'GET' && !id) {
      const slug = String(req.query.page || '');
      // a batch a colleague cleared stays on its editor's list for a day, saying who cleared it (to
      // restore it): they may not have been on its page to see the note there
      const listed = x => !x.cleared || (x.user === u.email && x.cleared.by !== u.email && Date.now() - (x.cleared.at || 0) < 24 * 3600e3);
      const all = (await listRecords('batches')).filter(x => listed(x) && (!slug || x.page === slug) && (u.role === 'admin' || (u.pages || []).includes(x.page)));
      // a post with the publisher may have failed since its editor left: its batch is unfinished again
      // (each such draft's state is read from its listing, one read; a batch whose draft moved on is
      // read as opening it does, and saved)
      const newest = all.sort((a, c) => c.created_at - a.created_at).slice(0, 30);
      const out = newest.flatMap(x => x.posts.filter(p => p.publish && p.publish.draft_id && ['approved', 'publishing', 'scheduled'].includes(p.publish.state)).map(p => [x, p]));
      const moved = new Set((await mapLimit(out, READ_CONCURRENCY, async ([x, p]) => {
        const [g] = groupDrafts(await listAll(`drafts/${p.publish.draft_id}/`).catch(() => []));
        return g && g.status !== p.publish.state ? x : null;
      })).filter(Boolean));
      const shown = await mapLimit(newest, READ_CONCURRENCY, x => (moved.has(x) ? withPublishState(x).catch(() => x) : x));
      return ok(res, { batches: shown
        .map(x => ({ id: x.id, page: x.page, user: x.user, created_at: x.created_at, done: x.posts.every(p => ['published', 'scheduled'].includes(p.status)),
                     ...(x.cleared ? { cleared: { by: x.cleared.by, at: x.cleared.at } } : {}),
                     posts: x.posts.map(p => ({ id: p.id, headline: p.story.headline, status: p.status })) })) });
    }
    if (req.method === 'POST' && !id) {                              // new batch from selected stories
      const p = await pageFor(req, res, String(b.page || ''));
      if (!p) return;
      const feed = await getRecord('feed', p.slug);
      const byId = Object.fromEntries(((feed && feed.stories) || []).map(s => [s.id, s]));
      // a story picked twice (a link pasted again, a double click) is one post; a pasted link of a
      // feed story, picked before or after it, too: the feed's copy is kept
      const picked = (b.stories || []).filter(Boolean).map(s => (s.from_url ? s : (byId[s.id] || s)));   // feed stories: the server's copy
      const stories = picked.filter((s, i, all) => !all.some((o, j) => j !== i
          && ((j < i && o.id === s.id) || (s.url && s.from_url && o.url === s.url && (!o.from_url || j < i)))))
        .slice(0, MAX_STORIES + 1);
      if (!stories.length) return bad(res, 'select at least one story');
      if (stories.length > MAX_STORIES) return bad(res, `select at most ${MAX_STORIES} stories`);
      const posts = stories.map(story => {
        return { id: randomId(6), story: storyOut(story), idea: null, base_image: null, final_image_url: '', card: null,
                 caption: '', hashtags: [], check: { status: 'none' }, publish: null, schedule: null, status: 'draft', job: null };
      });
      const bt = { id: `${p.slug}-${Date.now()}-${randomId(4)}`, page: p.slug, user: u.email, user_id: u.id,
                   created_at: Date.now(), window: Number(b.hours) || null, posts };
      await createRecord('batches', bt.id, bt);
      await audit(u, 'batch_started', { page: p.slug, batch: bt.id, stories: posts.length });
      for (const x of posts) await recordReview('studio_pick', { ...x.story, page: p.slug, id: `${bt.id}:${x.id}` }, { by: u.email });
      const needIdeas = posts.filter(x => !(x.story.ideas || []).length).map(x => x.id);
      const job = needIdeas.length ? await queueJob(req, bt, 'ideas', needIdeas) : null;
      if (job && job.job) await wake(job.job);
      return ok(res, { batch: job ? job.batch : bt, ideas_job: job && job.job && job.job.id });
    }
    const bt = await batchFor(req, res, id);
    if (!bt) return;
    if (req.method === 'GET' && !sub) {
      const seen = new Map();
      const failed = await nudgeJobs(bt, seen).catch(() => false);
      return ok(res, { batch: markStalled(await withPublishState(failed ? await getRecord('batches', bt.id) : bt), seen) });   // never the copy from before
    }

    // a batch someone cleared (from the Studio home) is taken back: its page shows who cleared it
    if (sub === 'restore' && req.method === 'POST') {
      const saved = await updateRecord('batches', bt.id, cur => {
        if (!cur || !cur.cleared) return null;
        cur.restored = { at: Date.now(), by: u.email, cleared: cur.cleared };
        delete cur.cleared;
        return cur;
      });
      await audit(u, 'batch_restored', { page: bt.page, batch: bt.id, cleared_by: bt.cleared ? bt.cleared.by : '' });
      return ok(res, { batch: saved });
    }

    const posts = bt.posts;
    const post = sub === 'posts' ? posts.find(x => x.id === subId) : null;
    if (sub === 'posts' && !post) return bad(res, 'no such post', 404);

    if (sub === 'posts' && req.method === 'PATCH') {
      const sent = x => x.publish && ['approved', 'publishing', 'scheduled', 'posted'].includes(x.publish.state);
      if (sent(post)) return bad(res, 'already sent to SocialPilot', 409);
      // a Send that did not finish may have handed the post to the publisher already (its approval
      // and draft written, the answer lost): what goes out no longer changes here, or the Studio
      // would show words that are not the ones sent
      if (post.publish && post.publish.draft_id && ['sending', 'pending'].includes(post.publish.state)) {
        const [d] = await listAll(`drafts/${post.publish.draft_id}/`).then(groupDrafts, () => [null]);
        if (d === null) return bad(res, 'storage is not answering: try again in a minute', 503);
        if (d && d.draftUrl && SENT.includes(d.status))
          return bad(res, 'it is with the publisher already (its Send did not finish): press Send to finish it; to change it after that, withdraw it on the review page and send it again', 409);
      }
      // checked (and an upload stored) first; the change itself is made on the newest copy of the batch
      const choice = b.idea && b.idea.choice;                          // {choice: 1|2|'own', text}
      if (b.idea !== undefined && choice === 'own' && !clean(b.idea.text, 2000)) return bad(res, 'describe your idea');
      if (b.idea !== undefined && (choice === 1 || choice === 2) && !(post.story.ideas || [])[choice - 1]) return bad(res, 'that idea is not ready yet');
      // a caption too long is refused, never cut: the text that goes out is the one the editor wrote
      const chars = b.caption !== undefined ? Array.from(String(b.caption).trim()).length : 0;
      if (chars > CAPTION_MAX) return bad(res, `captions can be at most ${CAPTION_MAX.toLocaleString('en-US')} characters (yours has ${chars.toLocaleString('en-US')}): shorten it`);
      const tags = b.hashtags !== undefined ? tagsOf(b.hashtags, Infinity).length : 0;   // (refused too, never cut)
      if (tags > TAGS_MAX) return bad(res, `posts can have at most ${TAGS_MAX} hashtags (yours has ${tags}): remove some`);
      if (b.idea !== undefined && (choice === 1 || choice === 2) && await retiredSpec(bt.page, post.story.ideas[choice - 1].template_spec))
        return bad(res, RETIRED, 409);
      let upload = '';
      if (b.base_image_b64) {                                          // upload or clipboard paste
        const buf = Buffer.from(String(b.base_image_b64), 'base64');
        if (buf.length > 3_300_000) return bad(res, 'image too large (max ~3 MB)', 413);
        const magic = buf.subarray(0, 12).toString('hex');
        if (!(magic.startsWith('ffd8ff') || magic.startsWith('89504e47') || (magic.startsWith('52494646') && buf.subarray(8, 12).toString() === 'WEBP')))
          return bad(res, 'that file is not a JPEG, PNG or WebP image');
        upload = await putImage(`studio/${bt.id}/base`, buf, clean(b.base_image_type, 40) || 'image/jpeg');
      } else if (b.base_image_url && ![...(post.story.images || []), ...(post.extra_images || [])].some(i => i.url === b.base_image_url))
        return bad(res, 'pick one of the candidate images');
      let x = null, cands = [], late = false, moved = '', clash = false;
      const saved = await updateRecord('batches', bt.id, cur => {
        x = cur && cur.posts.find(y => y.id === post.id);
        late = !!(x && sent(x));
        if (!x || late) return null;
        // typed over the text the field was drawn with (base_caption, base_hashtags): one saved
        // since (another window, a colleague) is never overwritten unseen
        clash = (b.caption !== undefined && b.base_caption !== undefined && !sameText(b.base_caption, x.caption) && !sameText(b.caption, x.caption))
          || (b.hashtags !== undefined && b.base_hashtags !== undefined && !sameTags(b.base_hashtags, x.hashtags) && !sameTags(b.hashtags, x.hashtags));
        if (clash) return null;
        if (b.idea !== undefined) {
          const idea = (x.story.ideas || [])[choice - 1], seen = (post.story.ideas || [])[choice - 1];
          if (choice === 'own') x.idea = { choice: 'own', text: clean(b.idea.text, 2000), template: clean(b.idea.template, 60) };
          else if (choice === 1 || choice === 2) {
            // new ideas arrived while this was saved: not the idea the editor clicked
            if (!idea || idea.title !== seen.title || idea.description !== seen.description) { moved = 'the ideas'; return null; }
            // template_spec: the page's own template (an approved spec), which the visual is drawn with
            x.idea = { choice, text: idea.description, template: idea.template, layout: idea.layout || '', title: idea.title,
                       ...(idea.template_spec ? { template_spec: idea.template_spec } : {}) };
          } else x.idea = null;
        }
        cands = [...(x.story.images || []), ...(x.extra_images || [])];   // as the editor sees them
        const rank = upload ? -1 : cands.findIndex(i => i.url === b.base_image_url);
        if (b.base_image_url && !upload && rank < 0) { moved = 'the photos'; return null; }   // a new image search left it out
        if (upload) {
          x.base_image = { url: upload, source: 'upload', credit: u.email, rank: null, of: cands.length };
          x.final_image_url = ''; x.check = { status: 'none' };
        } else if (b.base_image_url && !(x.base_image && x.base_image.url === b.base_image_url)) {   // a candidate (the one already set: nothing changes)
          const cand = cands[rank];
          // which candidate it was and what it was: the routine learns which photos editors pick
          x.base_image = { url: cand.url, source: cand.source, credit: cand.credit || '', via: cand.via || '', note: cand.note || '',
                           flags: cand.flags || [], consensus: cand.consensus || 0, rank, of: cands.length };
          x.final_image_url = ''; x.check = { status: 'none' };
        }
        const text = postText(x);
        if (b.caption !== undefined) x.caption = cutText(b.caption, CAPTION_MAX);
        if (b.hashtags !== undefined) x.hashtags = tagsOf(b.hashtags);
        // the editor's own words: a visual made again keeps them (its caption is offered beside them)
        if (postText(x) !== text) x.caption_edited_at = Date.now();
        if (b.suggested === 'use' && x.suggested) {
          x.caption = x.suggested.caption; x.hashtags = x.suggested.hashtags;
          delete x.caption_edited_at;
        }
        if (b.suggested === 'use' || b.suggested === 'dismiss') delete x.suggested;
        // a check is out of date while the text differs from the one it reviewed (taking the caption
        // it reviewed makes it current again); a check that does not know its text, once changed
        // (a re-check still waiting: the check it gives back if it fails, is stopped or skipped)
        const c = x.check.status === 'queued' && x.check.previous ? x.check.previous : x.check;
        if (c.status === 'done' && c.text !== undefined) c.stale = c.text !== postText(x);
        else if (postText(x) !== text && c.status === 'done') c.stale = true;
        x.status = postStatus(x);
        return cur;
      });
      if (!x) return bad(res, 'no such post', 404);
      if (late) return bad(res, 'already sent to SocialPilot', 409);
      if (clash) {
        // the caption in place is the one the last visual made came with (it replaces a caption the
        // editor had not edited yet): no other window or colleague was involved
        const by = !x.caption_edited_at && x.card_text === postText(x) ? 'visual' : 'edit';
        return res.status(409).json({ error: by === 'visual' ? 'the new visual came with its own caption while you typed: both are shown, choose one'
                                        : 'the caption was changed elsewhere (another window, or a colleague) while you typed: both are shown, choose one',
                                      changed: true, now: { caption: x.caption, hashtags: x.hashtags || [], by } });
      }
      if (moved) return bad(res, `${moved} changed while you picked: look again and pick again`, 409);
      if (b.base_image_b64 || (b.base_image_url && !(post.base_image && post.base_image.url === b.base_image_url)))   // the pick again, now with its photo
        await recordReview('studio_pick', { ...x.story, page: bt.page, id: `${bt.id}:${x.id}` }, { by: u.email, photo: pickedPhoto(x, cands) });
      return ok(res, { batch: saved });
    }

    if (sub === 'run' && req.method === 'POST') {                     // start an AI step as a Studio job
      const kind = String(b.kind || '');
      if (!['ideas', 'images', 'compose', 'check'].includes(kind)) return bad(res, 'unknown step');
      // the jobs of a cleared batch are cancelled (nudgeJobs): never queued only to vanish
      if (bt.cleared) return res.status(409).json({ error: `${bt.cleared.by || 'someone'} cleared this batch ${since(bt.cleared.at)}: restore it (at the top of the page) to keep working on it`,
                                                    cleared: bt.cleared });
      const asked = (b.post_ids && b.post_ids.length ? b.post_ids : posts.map(x => x.id)).filter(pid => posts.some(x => x.id === pid));
      // a post sent (or being sent) no longer changes: its result would be thrown away (a batch-wide
      // Run check still checks the others)
      const ids = asked.filter(pid => !sentOrSending(posts.find(x => x.id === pid)));
      if (asked.length && !ids.length) return bad(res, 'already sent to SocialPilot: it can no longer be changed here', 409);
      const retired = [];
      for (const pid of ids) {
        const x = posts.find(y => y.id === pid);
        if (kind === 'compose' && !(x.idea && x.base_image)) return bad(res, `choose an idea and a base image for "${x.story.headline.slice(0, 60)}" first`);
        if (kind === 'compose' && await retiredSpec(bt.page, x.idea.template_spec)) retired.push(x);
        if (kind === 'check' && !x.final_image_url) return bad(res, 'make the visual first');
      }
      // Make all visuals: the others are made, those named in the answer are not (nothing to make: refused)
      if (retired.length === ids.length && retired.length) return bad(res, `"${retired[0].story.headline.slice(0, 60)}": ${RETIRED}`, 409);
      const skipped = retired.length ? `Not made: ${retired.map(x => `"${x.story.headline.slice(0, 60)}"`).join(', ')}: ${RETIRED}` : '';
      ids.splice(0, ids.length, ...ids.filter(pid => !retired.some(x => x.id === pid)));
      // a visual per job: each post gets its own session and shows as soon as it is made (one
      // session making five in a row took past 20 minutes, and another then took the job over)
      let batch = bt;
      const jobs = [], open = [];
      for (const group of kind === 'compose' ? ids.map(pid => [pid]) : [ids]) {
        const r = await queueJob(req, batch, kind, group, { feedback: clean(b.feedback, 2000) },
                                 kind === 'check' ? x => { x.check = queuedCheck(x.check); } : null);
        batch = r.batch; open.push(...r.open);
        if (r.job) jobs.push(r.job);
      }
      // every job written first, then the routine started for all of them at once (Make all visuals
      // used to wait for one trigger call after another)
      const fired = (await Promise.all(jobs.map(wake))).some(Boolean);
      return ok(res, { job: jobs.length ? jobs[0].id : null, jobs: jobs.map(j => j.id), fired, open, already: !jobs.length, batch, ...(skipped ? { skipped } : {}) });
    }

    if (sub === 'skip-check' && req.method === 'POST') {
      const saved = await updateRecord('batches', bt.id, cur => {
        if (!cur) return null;
        for (const x of cur.posts) if (!b.post_ids || b.post_ids.includes(x.id)) {
          // only the checks still to come: a finished one keeps its findings
          // (a re-check still waiting: the check before it, with its findings)
          if (x.final_image_url && x.check && x.check.status === 'queued' && x.check.previous) { x.check = x.check.previous; x.status = postStatus(x); }
          else if (x.final_image_url && !(x.check && x.check.status === 'done')) { x.check = { status: 'skipped', at: Date.now() }; x.status = postStatus(x); }
        }
        return cur;
      });
      return ok(res, { batch: saved });
    }

    if (sub === 'publish' && req.method === 'POST') {                 // 4.8: post now or schedule
      const x = posts.find(y => y.id === b.post_id);
      if (!x) return bad(res, 'no such post', 404);
      if (!x.final_image_url) return bad(res, 'No visual yet: make it in step 2 (Visual) first');
      if (!x.caption) return bad(res, 'No caption yet: write one first');
      if (x.publish && SENT.includes(x.publish.state)) return bad(res, 'already sent', 409);
      // Send is the editor's approval of what their page shows: a visual made, or a caption saved
      // (another window, a colleague) since it was drawn is never sent unseen
      if (b.visual !== undefined && b.visual !== x.final_image_url)
        return res.status(409).json({ error: 'the visual changed since this page was drawn: look at it again before sending', changed: true });
      if (b.caption !== undefined && (!sameText(b.caption, x.caption) || !sameTags(b.hashtags, x.hashtags)))
        return res.status(409).json({ error: 'the caption was changed elsewhere (another window, or a colleague) since this page was drawn: look at it again before sending', changed: true });
      const mode = b.mode === 'now' ? 'now' : 'schedule';
      let at = mode === 'now' ? Date.now() + MIN_LEAD_MIN * 60e3 : Number(b.at);
      // "Post now", and the time the page suggested left as it was, are times the system chose: the
      // earliest allowed slot from then, at least MIN_LEAD from now and GAP_MIN from the page's other
      // queued posts (the page drew its suggestions minutes ago, from the posts queued then). Only
      // ever later, never sooner. A time the editor picked is kept as it is.
      if (mode === 'now' || (mode === 'schedule' && b.default === true)) {
        const taken = (await queuedTimes(bt.page, x.id)).map(q => q.t).filter(Boolean);
        at = freeSlot(Math.max(at || 0, Date.now() + MIN_LEAD_MIN * 60e3), taken);
      }
      if (mode === 'schedule' && !(at >= Date.now() + (MIN_LEAD_MIN - 2) * 60e3))
        return bad(res, `pick a time at least ${MIN_LEAD_MIN} minutes from now (posts are never sent straight away)`);
      const page = await getRecord('pages', bt.page);
      // the editor's card, read before anything is saved: storage refusing reads answers 503 here,
      // never a security-check page sent to SocialPilot as the post's image
      const img = await fetchBlob(x.final_image_url);
      if (!isImage(img)) return bad(res, 'storage did not hand over the visual: nothing was sent, try again in a few minutes', 503);
      // The post's earlier drafts as the publisher has them now (the batch may not say so yet): the one
      // it was last sent as (a Send cut short: the one that Send replaces), then each one it was sent
      // again from. One the review page approved again is with the publisher already, and one it is
      // redoing (feedback) comes back there. One that failed in a way that may have reached SocialPilot
      // or Facebook, or that has a red note for a person, is sent again only once the editor says they
      // looked or did what it says: the page asks what the review page asks. It also asks when "Post
      // now" goes out later than its label said (the page's queue filled up since it was drawn).
      const sending = !!x.publish && ['sending', 'pending'].includes(x.publish.state);
      const prevId = (x.publish && (sending ? x.publish.replaces || x.publish.resent_from : x.publish.draft_id)) || '';
      const earlier = prevId ? await earlierDrafts(prevId) : [];
      const out = earlier.find(d => SENT.includes(d.status));
      if (out) return bad(res, `already sent: its draft was retried (on the review page) and is ${out.status} now`, 409);
      const redo = earlier.find(d => d.status === 'regenerate' || d.status === 'regenerated');
      if (redo && !(redo.status === 'regenerated' && await turnedDown(redo.statusBody.new_id)))
        return bad(res, 'its draft is being redone on the review page (feedback): approve or reject it there', 409);
      const text = [x.caption.trim(), (x.hashtags || []).join(' ')].filter(Boolean).join('\n\n');
      const notes = earlier.length ? await needsAction({ strict: true }) : {};
      const note = earlier.map(d => notes[d.id]).find(Boolean);
      const unsure = earlier.filter(mayBeThere);
      // the publisher looks for an earlier copy by the start of its caption: one changed since hides it
      const changed = unsure.find(d => !sameText(d.caption_final, text));
      const moved = mode === 'now' && Number(b.shown_at) > 0 && at - Number(b.shown_at) > 5 * 60e3;
      if ((unsure.length || note || moved) && b.confirmed !== true)
        return res.status(409).json({ error: unsure.length ? 'it may already be on Facebook: look on the page first' : note ? 'do what its red note says first'
                                        : 'its earliest free slot is later than the page said: confirm the new time',
                                      confirm: { facebook: !!unsure.length, note: note ? note.text : '',
                                                 ...(changed ? { caption_changed: true, old_caption: String(changed.caption_final || '').slice(0, 60) } : {}),
                                                 ...(moved ? { now_at: at } : {}) } });
      // the post is marked as being sent, with its draft's id, before the draft is made: a Send cut
      // short (storage refusing a read after the approval) is finished by the next Send, with the
      // same draft, never sent twice
      const fresh = `${bt.page}-${new Date().toISOString().replace(/\D/g, '').slice(0, 12)}-studio-${x.id}`;
      let draftId = '', resumed = false, late = false, resentFrom = '', replaces = '';
      await updateRecord('batches', bt.id, cur => {
        const y = cur && cur.posts.find(p => p.id === x.id);
        if (!y) return null;
        late = !!(y.publish && SENT.includes(y.publish.state));
        if (late) return null;
        resumed = !!(y.publish && y.publish.draft_id && ['sending', 'pending'].includes(y.publish.state));
        draftId = resumed ? y.publish.draft_id : fresh;
        // sent again after a failure: the draft it replaces (retired once this one is approved), and
        // the one the publisher looks for first: the newest earlier draft a run claimed, whose copy
        // may be in SocialPilot (a draft that failed before a run took it, e.g. no account, cannot be
        // there, but the one it was sent again from may be). A Send cut short keeps what it had.
        const went = earlier.find(d => d.went && d.id !== draftId);
        replaces = resumed ? y.publish.replaces || y.publish.resent_from || ''
          : y.publish && y.publish.draft_id && y.publish.draft_id !== draftId ? y.publish.draft_id : '';
        resentFrom = resumed ? y.publish.resent_from || '' : went ? went.id : replaces;
        y.publish = { draft_id: draftId, state: 'sending', sending_at: Date.now(), mode, requested_at: at, scheduled_for: null, error: '',
                      ...(resentFrom ? { resent_from: resentFrom } : {}), ...(replaces ? { replaces } : {}) };
        return cur;
      });
      if (late) return bad(res, 'already sent', 409);
      // a draft the earlier Send already handed to the publisher is not made or approved again; one
      // still waiting for a publisher run whose words are not the ones shown now is approved again
      // with these (the approval below compares them)
      const made = resumed ? await loadDraft(draftId) : null;
      const handed = !!made && [...SENT, 'failed'].includes(made.status) && !(made.status === 'approved' && !sameText(made.caption_final, text));
      const state = handed ? made.status : 'approved';
      const photo = pickedPhoto(x);
      if (!handed) try {
        // the approval first: a draft whose approval was not written would look like an hourly post
        // waiting for review (a draft without draft.json is listed nowhere; the next Send makes it,
        // approved once: a Send cut short after the approval does not approve the same post again)
        const last = resumed && (await listAll(`drafts/${draftId}/`)).filter(f => /\/s-\d+-approved\.json$/.test(f.pathname)).pop();
        const approved = last && await getJson(last.url).catch(() => null);
        if (!(approved && approved.caption === text && approved.requested_at === at && approved.mode === mode)) await setStatus(draftId, 'approved', { caption: text, requested_at: at, mode, by: u.email });
        await saveDraft({ id: draftId, page: bt.page, page_name: page.name, caption: text, headline: x.story.headline,
                          sources: x.story.sources.map(s => s.url).filter(Boolean), verification: { route: 'studio', sources: x.story.sources.map(s => s.name) },
                          engine: 'studio', studio: { batch: bt.id, post: x.id, user: u.email }, source_photo: x.base_image && x.base_image.url,
                          ...(photo ? { photo } : {}), ...(resentFrom ? { resent_from: resentFrom } : {}) }, img);
      } catch (e) {
        // (storage's security check on reads has its own plain message; a refused write is this one)
        if (e instanceof StorageChallenged && e.checkpoint) throw e;
        // a write whose answer was lost may have landed: an approved draft with its draft.json is with
        // the publisher (storage not answering this either: it may be)
        const [d] = await listAll(`drafts/${draftId}/`).then(groupDrafts, () => [{ draftUrl: '?', status: 'approved' }]);
        if (d && d.draftUrl && SENT.includes(d.status))
          return bad(res, 'storage did not confirm the post: it may already be with the publisher, so press Send again to finish it (it is not sent twice)', 503);
        return bad(res, 'storage did not save the post: nothing was sent, press Send again', 503);
      }
      // they looked on Facebook: its "look on the page" note is settled (decide.js settleNote); any
      // other red note stays until someone marks it done
      for (const d of unsure) if (d.statusBody && d.statusBody.check_facebook) await setNeedsAction(d.id, d.page, '');
      const goes = replaces && replaces !== draftId ? await settleEarlier(replaces, draftId, u.email) : draftId;
      const saved = await updateRecord('batches', bt.id, cur => {
        const y = cur && cur.posts.find(p => p.id === x.id);
        if (!y || !y.publish || y.publish.draft_id !== draftId) return null;
        if (goes !== draftId) y.publish = { draft_id: goes, state: 'approved', scheduled_for: null, error: '' };   // that one goes out
        else if (y.publish.state === 'sending' || y.publish.state === 'pending')
          y.publish = { ...y.publish, state, ...(handed && made.requested_at ? { requested_at: made.requested_at, mode: made.publish_mode || mode } : {}) };
        y.status = postStatus(y);
        return cur;
      });
      if (goes !== draftId) return bad(res, 'already sent: its draft was retried on the review page at the same moment', 409);
      await audit(u, mode === 'now' ? 'post_now' : 'post_scheduled', { page: bt.page, batch: bt.id, post: x.id, post_at: at });
      await recordReview('studio_sent', { ...x.story, page: bt.page, id: draftId, template: (x.card || {}).layout || '' }, { by: u.email, photo });
      const f = await fire('ROUTINE', `Publish approved draft ${draftId} (and any other approved drafts in the queue).`);
      return ok(res, { batch: saved || bt, fired: f.fired });
    }
  }

  if (what === 'jobs' && req.method === 'GET' && id) {
    const job = await getRecord('jobs', id);
    if (!job || !await canUsePage(u, job.page)) return bad(res, 'no such job', 404);
    return ok(res, { job: { id: job.id, kind: job.kind, status: job.status, error: job.error || '', created_at: job.created_at, done_at: job.done_at || null } });
  }
  return bad(res, 'not found', 404);
}
