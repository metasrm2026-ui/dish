// /api/templates/*  a page's post templates as template specs (bore/tspec.py, docs/TEMPLATE_SPEC.md).
// The Studio routine converts each template of the page's templates document into a spec (a
// 'templates' Studio job, STUDIO.md TEMPLATES) and sends it with a preview drawn from it and the
// document page(s) it was made from. An editor of the page (or its owner) approves it, or rejects it
// with a note: it is then converted again with that note. Only an approved version is ever drawn:
// the routines get the approved specs in their config (`studio sync`).
//   GET  templates?page=                                 the page's templates, the document version, the conversion job
//   GET  templates/image?page&id&v&kind=preview|page&n=  a template's preview (v=in_use: the approved one), or (admins)
//                                                        the document page it came from
//   POST templates/<page>/<id>/<v>/approve               this version is the one the routines draw
//   POST templates/<page>/<id>/<v>/reject   {note}       not this one (out of use if it was): converted again with the note
//   POST templates/<page>/<id>/<v>/retire   {note?}      out of use, nothing converted (one the new document dropped)
//   POST templates/<page>/convert                        the page's owner: convert the whole document again
// The previews and pages are pictures of the page's confidential templates document: stored
// encrypted, the document's pages shown to the page's owner (and the super) only and never cached by the browser (a preview: a
// few minutes, in the viewer's own cache). The audit log
// and job records name a template by its neutral ref (t1, t2 ...), never by its name or id (both
// come from the document).
import crypto from 'node:crypto';
import { getRecord, updateRecord, createRecord, listBlobs, putFile, getFile, appendLog } from '../lib/db.js';
import { put, del } from '../lib/blob.js';
import { mayUse, mayManage, refusal } from '../lib/tenancy.js';
import { fire } from '../lib/fire.js';
import { audit } from '../lib/audit.js';
import { randomId } from '../lib/crypto.js';
import { ok, bad, clean } from '../lib/http.js';
import { failStalled, JOB_STALE_MS } from './studio.js';

const SPEC_ID = /^[a-z0-9][a-z0-9_-]{0,47}$/;        // bore/tspec.py ID_RE: what copy.json names
const KEEP = 6;                                       // versions kept per template, and always the one in use
const SPEC_BYTES = 60_000;
const IMAGE_BYTES = 1_500_000;                        // a preview or a document page (JPEG/PNG): 4.5 MB per request
const MAX_DOC_PAGES = 4;                              // document pages one template is drawn on
const OPEN = ['queued', 'running'];
const NUDGE_MS = 4 * 60e3;
const RESTARTS = 5;
const isImage = buf => buf.length > 8 && (buf.subarray(0, 3).toString('hex') === 'ffd8ff' || buf.subarray(0, 4).toString('hex') === '89504e47');
const imageType = buf => (buf.subarray(0, 4).toString('hex') === '89504e47' ? 'image/png' : 'image/jpeg');
const sha = s => crypto.createHash('sha256').update(s).digest('hex');

class Refused extends Error {
  constructor(message, status = 400) { super(message); this.status = status; }
}

// the active templates document of a page ({version, filename, pages}), or null
export function activeDoc(p) {
  const d = p && (p.docs || {}).templates;
  const v = d && (d.versions || []).find(x => x.v === d.active);
  return v ? { version: v.v, filename: v.filename, pages: v.pages || 0 } : null;
}

const newest = t => t.versions[t.versions.length - 1];
// a template's neutral name in the audit log: given in the order the templates arrived, and stored
// (an older record has none until it is next written: its place among the ids, as it was audited)
const refOf = (rec, id) => (rec && rec.specs && rec.specs[id] && rec.specs[id].ref)
  || `t${Object.keys((rec && rec.specs) || {}).sort().indexOf(id) + 1}`;
const refNo = t => Number(String((t && t.ref) || '').slice(1)) || 0;
const nextRef = specs => `t${Object.values(specs || {}).reduce((m, t) => Math.max(m, refNo(t)), 0) + 1}`;
// the record's templates each with a stored ref: one without keeps the place it was audited by, unless
// that is taken (never two templates with one ref, never a ref that moves when a template is added)
function withRefs(rec) {
  const specs = { ...((rec && rec.specs) || {}) };
  if (Object.values(specs).every(t => t.ref)) return rec;
  const used = new Set(Object.values(specs).map(t => t.ref).filter(Boolean));
  Object.keys(specs).sort().forEach((id, i) => {
    if (specs[id].ref) return;
    let n = i + 1;
    if (used.has(`t${n}`)) n = Math.max(...[...used].map(r => Number(r.slice(1)) || 0)) + 1;
    used.add(`t${n}`);
    specs[id] = { ...specs[id], ref: `t${n}` };
  });
  return { ...rec, specs };
}
const versionOut = x => ({ v: x.v, name: x.name, status: x.status, note: x.note || '', doc_version: x.doc_version ?? null,
  limits: clean((x.spec || {}).limits, 400),
  doc_pages: x.doc_pages || [], pages: (x.pages || []).map(y => y.n), preview: !!x.preview, at: x.at, job: x.job || '',
  decided_by: x.decided_by || '', decided_at: x.decided_at || null });

// What the page's editors see: every template, newest version first (no storage paths, no spec).
// Waiting for a decision first, then in use, then the rest; by name within each. dropped: in use, but
// not converted from the active document although its whole conversion is done (a new document left it
// out): offered to be taken out of use. While that conversion waits or runs, nothing is.
function templatesOut(rec, doc, job) {
  const c = rec && rec.converted;
  const whole = !!(doc && c && c.doc_version === doc.version)
    && !(job && job.kind === 'templates' && OPEN.includes(job.status) && job.only == null);
  const rank = t => (newest(t).status === 'pending' ? 0 : t.in_use ? 1 : 2);
  return Object.values((rec && rec.specs) || {}).filter(t => t.versions.length)
    .sort((a, c) => rank(a) - rank(c) || String(newest(a).name).localeCompare(String(newest(c).name)))
    .map(t => ({ id: t.id, name: newest(t).name, use_when: newest(t).use_when, visual_lane: newest(t).visual_lane,
                 in_use: t.in_use || null, versions: t.versions.slice().reverse().map(versionOut),
                 dropped: !!(whole && t.in_use && !(c.failed || []).includes(t.id) && !t.versions.some(x => x.doc_version === doc.version)) }));
}

// The routines' copy: each template's approved version (the spec the card renderer draws).
export function approvedSpecs(rec) {
  const out = [];
  for (const t of Object.values((rec && rec.specs) || {})) {
    const x = t.in_use && t.versions.find(y => y.v === t.in_use);
    if (x) out.push({ id: t.id, v: x.v, doc_version: x.doc_version ?? null, spec: x.spec });
  }
  return out.sort((a, c) => a.id.localeCompare(c.id));
}

// What a conversion session is given about the page's templates so far: names, statuses and the
// editors' notes, and each one's newest spec (a rejected one is converted again from it).
export function templatesForJob(rec) {
  return Object.values((rec && rec.specs) || {}).filter(t => t.versions.length).map(t => {
    const x = newest(t);
    return { id: t.id, name: x.name, in_use: t.in_use || null, status: x.status, note: x.note || '', v: x.v,
             doc_version: x.doc_version ?? null, doc_pages: x.doc_pages || [], spec: x.spec };
  });
}

// A conversion job for the page (STUDIO.md TEMPLATES): everything (only: null) or some templates
// (a rejected one, with the editor's note). One the Studio routine has not taken yet takes this
// request in, so one session does both; otherwise a new one is queued and the routine woken.
// A whole conversion finished: from now on the screen can say which templates in use the document left
// out (one that could not be converted is not one it left out).
export async function markConverted(job, result) {
  await updateRecord('templates', job.page, cur => cur && { ...cur, converted: { doc_version: job.doc_version ?? null, job: job.id, at: Date.now(),
                                                                                 failed: Object.keys((result && result.failed) || {}) } });
}

export async function queueConversion(by, slug, { reason, doc_version = null, only = null, notes = {} }) {
  const rec = await getRecord('templates', slug);
  const last = rec && rec.job && rec.job.id ? await getRecord('jobs', rec.job.id).catch(() => null) : null;
  if (last && last.kind === 'templates' && last.status === 'queued') {
    let joined = false;
    const job = await updateRecord('jobs', last.id, cur => {
      joined = false;
      if (!cur || cur.status !== 'queued') return null;
      joined = true;
      return { ...cur, only: cur.only && only ? [...new Set([...cur.only, ...only])] : null, notes: { ...(cur.notes || {}), ...notes },
               doc_version: doc_version ?? cur.doc_version, reason: cur.reason === reason ? reason : `${cur.reason}+${reason}` };
    });
    if (joined) return { job, joined: true, fired: false };
  }
  const job = { id: `${slug}-${Date.now()}-${randomId(4)}`, kind: 'templates', batch_id: null, page: slug, post_ids: [],
                user: by, status: 'queued', created_at: Date.now(), reason, doc_version, only, notes };
  await createRecord('jobs', job.id, job);
  await updateRecord('templates', slug, cur => ({ ...(cur || { page: slug, specs: {} }), job: { id: job.id, at: job.created_at, reason } }));
  const f = await fire('STUDIO', `Studio job ${job.id} (templates) for ${slug}: convert the page's post templates (STUDIO.md TEMPLATES).`);
  // a session may have claimed it while the routine was starting (never undo that)
  await updateRecord('jobs', job.id, cur => (cur && cur.status === 'queued' ? { ...cur, fired: !!f.fired, fire_status: f.status || null, fire_error: f.error || '' } : null));
  return { job, joined: false, fired: !!f.fired };
}

// A templates job nobody took (the session never started) or whose session stopped answering
// (running 20 minutes since it last sent a template): the routine is started again every 4 minutes,
// at most 5 times, then the job fails with a message. A templates job has no batch for an editor's
// page to follow (routes/studio.js nudgeJobs), so the Templates screen and the hourly watchdog do this.
export async function nudgeTemplates(job) {
  if (!job || job.kind !== 'templates' || !OPEN.includes(job.status)) return false;
  const due = job.status === 'queued' ? Math.max(job.created_at || 0, job.released_at || 0) + NUDGE_MS : (job.started_at || 0) + JOB_STALE_MS;
  if (Date.now() < due) return false;
  const nudges = (await listBlobs(`job-nudges/${job.id}/`)).sort((x, y) => new Date(y.uploadedAt) - new Date(x.uploadedAt));
  if (nudges.length && Date.now() - new Date(nudges[0].uploadedAt) < NUDGE_MS) return false;
  if (nudges.length >= RESTARTS)
    return failStalled(job.id, `The Studio routine could not convert the templates after ${RESTARTS} restarts. `
                               + 'An admin can press Convert templates again; if it keeps failing, tell the admin.');
  try {
    await put(`job-nudges/${job.id}/${nudges.length}.json`, JSON.stringify({ at: Date.now(), status: job.status }),
              { access: 'public', contentType: 'application/json', addRandomSuffix: false, allowOverwrite: false });
  } catch (e) { return false; }                                       // another request is restarting it
  const f = await fire('STUDIO', `Studio job ${job.id} (templates) for ${job.page} is still waiting: process the queued Studio jobs (STUDIO.md).`);
  await appendLog('studio_nudges', { job: job.id, kind: job.kind, page: job.page, status: job.status, n: nudges.length + 1,
                                     fired: !!f.fired, fire_status: f.status || null, fire_error: f.error || '' }).catch(() => {});
  return false;
}

function imageIn(b64, what) {
  const buf = Buffer.from(String(b64 || ''), 'base64');
  if (!isImage(buf)) throw new Refused(`${what} must be a JPEG or PNG image`);
  if (buf.length > IMAGE_BYTES) throw new Refused(`${what} is ${(buf.length / 1e6).toFixed(1)} MB: send it as a JPEG under 1.5 MB`, 413);
  return buf;
}

// POST /api/routine/templates (bore/studio.py send_template): one template's spec, its preview and the
// document page(s) it was made from, as a new version waiting for the editors. The same spec sent
// again by the same job (its answer lost) is the same version. Each one is also the conversion job's
// sign of life: a document of many templates takes longer than the 20 minutes after which a job's
// session counts as gone (its job would be offered to another session), so each template sent
// counts as a new start.
export async function saveFromRoutine(b) {
  const page = clean(b.page, 60), job = clean(b.job, 120);
  const p = page && await getRecord('pages', page);
  if (!p) throw new Refused('no such page', 404);
  const jr = job ? await getRecord('jobs', job) : null;
  if (job && !(jr && jr.kind === 'templates' && jr.page === page)) throw new Refused('job: not a templates job of this page', 409);
  const spec = b.spec;
  if (!spec || typeof spec !== 'object' || Array.isArray(spec)) throw new Refused('spec must be the template spec (an object)');
  const text = JSON.stringify(spec);
  if (text.length > SPEC_BYTES) throw new Refused('the spec is too large', 413);
  if (!SPEC_ID.test(String(spec.id || ''))) throw new Refused('spec.id must be 1-48 lower-case letters, digits, - or _');
  for (const k of ['name', 'use_when']) if (!clean(spec[k], 400)) throw new Refused(`spec.${k} is required`);
  const docPages = (Array.isArray(b.doc_pages) ? b.doc_pages : []).map(Number).filter(n => Number.isInteger(n) && n >= 1 && n <= 999);
  if (!docPages.length || docPages.length > MAX_DOC_PAGES) throw new Refused(`doc_pages: the 1-${MAX_DOC_PAGES} document pages the template was made from`);
  const preview = imageIn(b.preview_b64, 'the preview');
  const pages = (Array.isArray(b.pages) ? b.pages : []).slice(0, MAX_DOC_PAGES).map(x => ({ n: Number(x && x.n), buf: imageIn(x && x.image_b64, `page ${x && x.n}`) }));
  if (!pages.length || pages.some(x => !docPages.includes(x.n))) throw new Refused('pages: the picture of each of doc_pages');
  const hash = sha(text);
  const sameAs = rec => { const t = rec && rec.specs && rec.specs[spec.id]; return t && t.versions.find(x => x.job === job && x.sha === hash); };
  const before = sameAs(await getRecord('templates', page));
  if (job && before) return { id: spec.id, v: before.v, status: before.status, again: true };
  // the pictures first (encrypted), then the record in one quick step on its newest copy
  const dir = `templates/${page}/${spec.id}/${Date.now()}-${randomId(4)}`;
  let made = null, again = false, dropped = [];
  const store = async () => {
    const previewPath = await putFile(`${dir}/preview.enc`, preview);
    const stored = [];
    for (const x of pages) stored.push({ n: x.n, file: await putFile(`${dir}/page-${String(x.n).padStart(3, '0')}.enc`, x.buf) });
    await updateRecord('templates', page, cur => {
      made = null; again = false; dropped = [];
      const base = withRefs(cur || { page, specs: {} });
      const t = (base.specs || {})[spec.id] || { id: spec.id, in_use: null, versions: [], ref: nextRef(base.specs) };
      const same = job && sameAs(base);
      if (same) { made = same; again = true; return null; }
      made = { v: t.versions.reduce((m, x) => Math.max(m, x.v), 0) + 1, name: clean(spec.name, 120), use_when: clean(spec.use_when, 400),
               visual_lane: clean(spec.visual_lane, 20) || 'clean', spec, sha: hash, status: 'pending', note: '',
               doc_version: Number(b.doc_version) || null, doc_pages: docPages, preview: previewPath, pages: stored, job, at: Date.now() };
      // an older version still waiting is replaced by this one: the editors decide on the newest
      let versions = [...t.versions.map(x => (x.status === 'pending' ? { ...x, status: 'replaced' } : x)), made];
      if (versions.length > KEEP) {
        dropped = versions.filter((x, i) => !(i >= versions.length - KEEP || x.v === t.in_use));
        versions = versions.filter(x => !dropped.includes(x));
      }
      return { ...base, specs: { ...(base.specs || {}), [spec.id]: { ...t, versions } } };
    });
  };
  // this request's copies of the pictures go when the record does not take them (a duplicate, a failed write)
  const drop = async () => { const files = await listBlobs(`${dir}/`).catch(() => []); if (files.length) await del(files.map(f => f.url)).catch(() => {}); };
  try { await store(); } catch (e) { await drop(); throw e; }
  if (again) await drop();
  // the pictures of versions no longer kept go too (they are of the confidential document)
  for (const x of dropped) {
    const dir = String(x.preview || '').replace(/\/[^/]*$/, '/');
    const files = dir.startsWith(`templates/${page}/`) ? await listBlobs(dir).catch(() => []) : [];
    if (files.length) await del(files.map(f => f.url)).catch(() => {});
  }
  if (!again && job) await updateRecord('jobs', job, cur => (cur && cur.kind === 'templates' && cur.status === 'running'
    ? { ...cur, started_at: Date.now(), sent: (cur.sent || 0) + 1 } : null)).catch(() => {});
  if (!again) await audit('routine', 'template_received', { page, template: refOf(await getRecord('templates', page), spec.id),
                                                            version: made.v, doc_version: made.doc_version });
  return { id: spec.id, v: made.v, status: made.status, ...(again ? { again: true } : {}) };
}

// approve, reject or retire one version, on the newest copy of the record (a decision made meanwhile on
// another template is kept; one on this version first makes this one a 409). Retired: out of use (or
// never to be), and nothing converted again; approving it puts it back in use.
async function decide(u, slug, id, v, action, note) {
  const approve = action === 'approve';
  let found = false, why = '', was = null;
  const rec = await updateRecord('templates', slug, cur => {
    found = false; why = ''; was = null;
    cur = cur && withRefs(cur);
    const t = cur && cur.specs && cur.specs[id];
    const x = t && t.versions.find(y => y.v === v);
    if (!x) return null;
    found = true; was = x.status;
    const from = { approve: ['pending', 'rejected', 'retired'], reject: ['pending', 'approved'], retire: ['pending', 'approved'] }[action];
    if (!from.includes(x.status))
      { why = x.status === 'replaced' ? 'a newer version replaced this one: decide on that one' : `this version is already ${x.status === 'retired' ? 'out of use' : x.status}`; return null; }
    const at = Date.now();
    const versions = t.versions.map(y => {
      if (y.v === v) return { ...y, status: { approve: 'approved', reject: 'rejected', retire: 'retired' }[action], note: approve ? '' : note,
                              decided_by: u.email, decided_at: at };
      // approved: the one in use before, and any older one still waiting, are replaced
      if (approve && (y.status === 'approved' || (y.status === 'pending' && y.v < v))) return { ...y, status: 'replaced' };
      return y;
    });
    const inUse = approve ? v : t.in_use === v ? null : t.in_use;
    return { ...cur, specs: { ...cur.specs, [id]: { ...t, versions, in_use: inUse } } };
  });
  if (!found) throw new Refused('no such template version', 404);
  if (why) throw new Refused(why, 409);
  await audit(u, `template_${{ approve: 'approved', reject: 'rejected', retire: 'retired' }[action]}`,
              { page: slug, template: refOf(rec, id), version: v, ...(approve ? {} : { was }) });
  return rec;
}

// The page's owner (and the super) sees the document's own pages and can convert it again; its editors
// compare with the previews.
async function pageOut(u, slug) {
  const [p, rec] = await Promise.all([getRecord('pages', slug), getRecord('templates', slug)]);
  const owner = mayManage(u, p);
  let job = rec && rec.job && rec.job.id ? await getRecord('jobs', rec.job.id).catch(() => null) : null;
  if (job && await nudgeTemplates(job).catch(() => false)) job = await getRecord('jobs', job.id).catch(() => job);
  return { page: slug, doc: activeDoc(p), templates: templatesOut(rec, activeDoc(p), job), can_convert: owner,
           can_see_document: owner,
           job: job ? { id: job.id, status: job.status, reason: job.reason || '', created_at: job.created_at, started_at: job.started_at || null,
                        done_at: job.done_at || null, error: job.error || '', only: job.only || null, sent: job.sent || 0,
                        result: job.result || null } : null };
}

export default async function handler(req, res) {
  const u = req.user;
  if (!u) return bad(res, 'sign in first', 401);
  if (u.must_change) return bad(res, 'set a new password first', 403);
  const [first, id, v, action] = req.subpath;
  const b = req.body || {};
  try {
    if (req.method === 'GET' && !first) {
      const slug = clean(req.query.page, 60);
      const p = slug && await getRecord('pages', slug);
      if (!p || p.archived) return bad(res, 'no such page', 404);
      if (!mayUse(u, p)) { const r = refusal(u, p); return bad(res, r.error, r.status); }
      const out = await pageOut(u, slug);
      // the images on the screen are part of this view (the screen following a conversion is not a new
      // one): an admin's includes the document's pages beside them, one entry for all of them
      if (!req.query.poll) await audit(u, 'templates_viewed', { page: slug, templates: out.templates.length,
        ...(out.can_see_document ? { document_pages: out.templates.reduce((n, t) => n + ((t.versions[0] || {}).pages || []).length, 0) } : {}) });
      return ok(res, out);
    }
    if (req.method === 'GET' && first === 'image') {
      const slug = clean(req.query.page, 60);
      const pg = slug && await getRecord('pages', slug);
      if (!pg || !mayUse(u, pg)) { const r = refusal(u, pg); return bad(res, r.error, r.status); }
      // a picture of the confidential document's own page: its owner and the super only (editors compare with the preview)
      const page = req.query.kind !== 'preview';
      if (page && !mayManage(u, pg)) return bad(res, 'only the page\'s owner sees the templates document', 403);
      const rec = await getRecord('templates', slug);
      const id = clean(req.query.id, 60), t = rec && rec.specs && rec.specs[id];
      const x = t && t.versions.find(y => y.v === (req.query.v === 'in_use' ? t.in_use : Number(req.query.v)));
      const path = !x ? '' : page ? ((x.pages || []).find(y => y.n === Number(req.query.n)) || {}).file : x.preview;
      const buf = path ? await getFile(path) : null;
      if (!buf) return bad(res, 'no such picture', 404);
      res.setHeader('Content-Type', imageType(buf));
      // the document's own page is never kept on the device; a preview (the template drawn on a stand-in
      // photo) for a few minutes, so the Studio's idea cards are not fetched again at every redraw
      res.setHeader('Cache-Control', page ? 'private, no-store' : 'private, max-age=300');
      return res.status(200).send(buf);
    }
    const slug = clean(first, 60);
    if (req.method !== 'POST' || !slug) return bad(res, 'not found', 404);
    const p = await getRecord('pages', slug);
    if (!p || p.archived) return bad(res, 'no such page', 404);
    if (!mayUse(u, p)) { const r = refusal(u, p); return bad(res, r.error, r.status); }
    if (id === 'convert' && !v) {
      if (!mayManage(u, p)) return bad(res, 'only the page\'s owner can convert the templates again', 403);
      const doc = activeDoc(p);
      if (!doc) return bad(res, 'the page has no templates document: upload it first (Admin > Pages)');
      const r = await queueConversion(u.email, slug, { reason: 'admin', doc_version: doc.version });
      await audit(u, 'templates_convert', { page: slug, job: r.job.id, doc_version: doc.version });
      return ok(res, { ...(await pageOut(u, slug)), queued: r.job.id, joined: r.joined, fired: r.fired });
    }
    if (id && v && ['approve', 'reject', 'retire'].includes(action)) {
      const note = clean(b.note, 1000);
      if (action === 'reject' && note.length < 3) return bad(res, 'say what is wrong with it: it is converted again with your note');
      await decide(u, slug, clean(id, 60), Number(v), action, note);
      let queued = null;
      if (action === 'reject') {
        const doc = activeDoc(p);
        queued = (await queueConversion(u.email, slug, { reason: 'rejected', doc_version: doc ? doc.version : null,
                                                         only: [clean(id, 60)], notes: { [clean(id, 60)]: note } })).job.id;
      }
      return ok(res, { ...(await pageOut(u, slug)), ...(queued ? { queued } : {}) });
    }
    return bad(res, 'not found', 404);
  } catch (e) {
    if (e instanceof Refused) return bad(res, e.message, e.status);
    throw e;
  }
}
