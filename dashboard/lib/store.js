// Draft storage on Vercel Blob. Records are append-only so the CDN never serves a stale status:
//   drafts/<id>/draft.json          content (caption, sources, check, page ...); written again only
//                                   when an upload is retried or a Studio Send is finished
//   drafts/<id>/card-<ts>.jpg       the infographic, one path per version (newest wins)
//   drafts/<id>/extra-<n>-<ts>.jpg  a post "as is" with several images: image n+1, same version as the card
//   drafts/<id>/s-<ts>-<status>.json one file per status change (approved caption, publish result)
// The latest status is read from the listing (metadata API, not the CDN).
import { put, list, del } from './blob.js';
import { readBlob, blobKey } from './db.js';

// Just after draft.json is written again the CDN may still hand out the old copy, so it is kept in
// memory (readBlob) only once it has settled; status files and cards are never written again.
const SETTLED_MS = 10 * 60e3;

// regenerate: a reviewer left feedback; the next hourly routine rebuilds the post.
// regenerated: superseded by the rebuilt draft (status body names it: {new_id}).
// cleared: an editor (or the midnight clean-up) removed it from Waiting. Not a rejection: posts
// an editor selects and clears count as a weak "not this one" (review ledger); clears by age don't.
export const STATUSES = ['pending', 'approved', 'publishing', 'scheduled', 'posted', 'failed', 'rejected',
  'regenerate', 'regenerated', 'cleared'];

// Waiting posts made before the last midnight in CLEAR_TZ (the editors' time zone) are cleared.
export function lastMidnight(tz = process.env.CLEAR_TZ || 'Asia/Kolkata', now = Date.now()) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', { timeZone: tz, hourCycle: 'h23',
    hour: 'numeric', minute: 'numeric', second: 'numeric' }).formatToParts(new Date(now)).map(p => [p.type, Number(p.value)]));
  return Math.floor((now - ((parts.hour * 60 + parts.minute) * 60 + parts.second) * 1000) / 1000);   // unix seconds
}

export async function listAll(prefix = 'drafts/') {
  const out = [];
  let cursor;
  do {
    const r = await list({ prefix, cursor, limit: 1000 });
    out.push(...r.blobs);
    cursor = r.hasMore ? r.cursor : undefined;
  } while (cursor);
  return out;
}

export function groupDrafts(blobs) {
  const by = {};
  for (const b of blobs) {
    const m = b.pathname.match(/^drafts\/([^/]+)\/(.+)$/);
    if (!m) continue;
    const [, id, file] = m;
    const d = (by[id] ||= { id, statuses: [] });
    if (file === 'draft.json') { d.draftUrl = b.url; d.draftKey = Date.now() - new Date(b.uploadedAt) > SETTLED_MS ? blobKey(b) : null; }
    else if (/^card(-\d+)?\.jpg$/.test(file)) {
      // each card version has its own path (images are cached for 30 days): newest wins
      const v = Number((file.match(/-(\d+)\./) || [0, 0])[1]);
      if (!d.imageUrl || v >= (d.imageV || 0)) { d.imageUrl = b.url; d.imageV = v; }
    }
    else if (/^extra-\d+-\d+\.jpg$/.test(file)) {
      const [, n, v] = file.match(/^extra-(\d+)-(\d+)\.jpg$/);
      (d.extras ||= []).push({ n: Number(n), v: Number(v), url: b.url });
    }
    else {
      const s = file.match(/^s-(\d+)-([a-z]+)\.json$/);
      if (s) d.statuses.push({ at: Number(s[1]), status: s[2], url: b.url, key: blobKey(b) });
    }
  }
  for (const d of Object.values(by)) {
    d.statuses.sort((a, b) => a.at - b.at);
    // the current version's other images, in order (an older upload's extras don't count)
    d.extraUrls = (d.extras || []).filter(x => x.v === (d.imageV || 0)).sort((a, b) => a.n - b.n).map(x => x.url);
    const last = d.statuses[d.statuses.length - 1];
    d.status = last ? last.status : 'pending';
    d.createdAt = createdFromId(d.id);
    d.page = pageOf(d.id);
  }
  return Object.values(by).filter(d => d.draftUrl);
}

// ids look like <page>-<YYYYMMDDHHMM>-<slug>; a page's slug can hold '-2' itself (top-25-hoops)
export function pageOf(id) {
  const m = id.match(/^(.*?)-\d{12}-/);
  return m ? m[1] : id.split('-2')[0];
}
export function createdFromId(id) {
  const m = id.match(/-(\d{12})-/);
  if (!m) return 0;
  const s = m[1];
  return Date.UTC(+s.slice(0, 4), +s.slice(4, 6) - 1, +s.slice(6, 8), +s.slice(8, 10), +s.slice(10, 12)) / 1000;
}

// key: the blob's listing key (blobKey), so an unchanged file is read once per instance
export async function getJson(url, key) {
  return readBlob(url, key, buf => JSON.parse(buf.toString('utf8')));
}

export async function setStatus(id, status, body = {}) {
  if (!STATUSES.includes(status)) throw new Error(`bad status ${status}`);
  const at = Date.now();
  await put(`drafts/${id}/s-${at}-${status}.json`, JSON.stringify({ status, at, ...body }), {
    access: 'public', contentType: 'application/json', addRandomSuffix: false,
  });
  return at;
}

// One verdict per status: an editor's decision (or the midnight clean-up) first takes a create-only
// claim on the status it was made from (its time, at), so two people deciding the same post at the
// same moment cannot both win. null: this one won; else who has it ({by, decision, at}).
// The winner gives its claim back if it could not write the status (releaseDecision). One older than
// a request can run (60 s) with no status after it was abandoned: the next claim on that status takes
// the next name (<at>-1.json ...), so two people taking it over still cannot both win.
// w: this call's own token, so a claim that landed although its answer was lost is still this one's.
// One by the same person for the same decision under a minute old, with no status written after it,
// is theirs too: their earlier press whose answer was lost and could not be listed either (a 503).
const ABANDONED_MS = 90e3, OWN_MS = 60e3;
export async function claimDecision(id, at, by, decision) {
  const w = Math.random().toString(36).slice(2, 10);
  for (let n = 0; ; n++) {
    const name = `decisions/${id}/${at || 0}${n ? '-' + n : ''}.json`;
    try {
      await put(name, JSON.stringify({ by, decision, at: Date.now(), w }),
                { access: 'public', contentType: 'application/json', addRandomSuffix: false, allowOverwrite: false });
      return null;
    } catch (e) {
      const [b] = (await list({ prefix: name })).blobs.filter(x => x.pathname === name);
      if (!b) throw e;                                              // storage failing: not someone else's verdict
      const c = (await getJson(b.url).catch(() => null)) || {};
      if (c.w === w) return null;
      if (c.by === by && c.decision === decision && Date.now() - c.at < OWN_MS && !await statusSince(id, c.at)) return null;
      if (!(Date.now() - c.at > ABANDONED_MS) || n >= 20) return c;
    }
  }
}
async function statusSince(id, t) {
  return (await list({ prefix: `drafts/${id}/s-` })).blobs.some(b => Number((b.pathname.match(/\/s-(\d+)-/) || [])[1]) >= t);
}
// the status could not be written after this claim was won: the post can be decided again
export async function releaseDecision(id, at) {
  const mine = new RegExp(`^${`decisions/${id}/${at || 0}`.replace(/[^\w/-]/g, '\\$&')}(-\\d+)?\\.json$`);
  const urls = (await list({ prefix: `decisions/${id}/${at || 0}` })).blobs.filter(b => mine.test(b.pathname)).map(b => b.url);
  if (urls.length) await del(urls);
}
// setStatus after a won claim on the status at: a write that fails gives the claim back
export async function setDecided(id, at, status, body) {
  try { return await setStatus(id, status, body); }
  catch (e) { await releaseDecision(id, at).catch(() => {}); throw e; }
}
// how a lost claim reads to the editor: "already approved by ed@example.com just now"
export function decidedBy(c) {
  const did = { approve: 'approved', reject: 'marked Not okay', regenerate: 'sent back to be regenerated', clear: 'cleared',
                retry: 'sent to the publisher again' }[c.decision] || 'decided';
  return `already ${did}${c.by ? ' by ' + c.by : ''}${Date.now() - c.at < 120e3 ? ' just now' : ''}`;
}

export async function saveDraft(draft, imageBuffer, extraBuffers = []) {
  const id = draft.id;
  // a new path per version: browsers and the CDN cache images for 30 days, so overwriting
  // card.jpg left editors looking at the old card
  const v = Date.now();
  for (const [i, buf] of extraBuffers.entries()) {          // an as-is post's other images first
    await put(`drafts/${id}/extra-${i + 1}-${v}.jpg`, buf, { access: 'public', contentType: 'image/jpeg', addRandomSuffix: false });
  }
  const img = await put(`drafts/${id}/card-${v}.jpg`, imageBuffer, {
    access: 'public', contentType: 'image/jpeg', addRandomSuffix: false,
  });
  await put(`drafts/${id}/draft.json`, JSON.stringify({ ...draft, image_url: img.url }), {
    access: 'public', contentType: 'application/json', addRandomSuffix: false, allowOverwrite: true,
  });
  return img.url;
}

// A reviewer's reference image for a regeneration request.
export async function saveReference(id, buffer, contentType = 'image/jpeg') {
  const r = await put(`drafts/${id}/ref-${Date.now()}.jpg`, buffer, {
    access: 'public', contentType, addRandomSuffix: false,
  });
  return r.url;
}

export async function loadDraft(id) {
  const blobs = await listAll(`drafts/${id}/`);
  const [d] = groupDrafts(blobs);
  if (!d) return null;
  const draft = await getJson(d.draftUrl, d.draftKey);
  const latest = d.statuses[d.statuses.length - 1];
  const statusBody = latest ? await getJson(latest.url, latest.key) : {};
  const approved = [...d.statuses].reverse().find(x => x.status === 'approved');
  const approvedBody = approved ? (approved === latest ? statusBody : await getJson(approved.url, approved.key).catch(() => ({}))) : {};
  // a draft held while SocialPilot was down is approved again each time: first_approved_at keeps
  // the editor's approval time (unix seconds), holds counts the tries
  return { ...draft, status: d.status, image_url: d.imageUrl, extra_images: d.extraUrls || [], caption_final: statusBody.caption || approvedBody.caption || draft.caption, statusBody,
           approved_at: approved ? Math.floor(approved.at / 1000) : null, approved_ms: approved ? approved.at : null,
           first_approved_at: approvedBody.first_approved_at || (approved ? Math.floor(approved.at / 1000) : null),
           holds: approvedBody.holds || 0, approved_by: approvedBody.by || '',
           // what a held draft waits for, and how often its lookups failed (routes/result.js hold)
           held_scope: approvedBody.held_scope || '', lookup_fails: approvedBody.lookup_fails || 0,
           requested_at: approvedBody.requested_at || null, publish_mode: approvedBody.mode || null };
}

export async function deleteOlderThan(days) {
  const cutoff = Date.now() / 1000 - days * 86400;
  const blobs = await listAll();
  const old = blobs.filter(b => {
    const m = b.pathname.match(/^drafts\/([^/]+)\//);
    return m && createdFromId(m[1]) && createdFromId(m[1]) < cutoff;
  });
  for (let i = 0; i < old.length; i += 100) await del(old.slice(i, i + 100).map(b => b.url));
  return old.length;
}
