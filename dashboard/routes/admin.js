// /api/admin/*  (the product owner only: role super) testers and their workspaces, users, pages, sources,
// reference documents, the product's settings, audit and usage logs. A page's owner manages it in routes/pages.js, with the
// same sections (pageSection).
// Section 5: everything that varies by brand is configured here, with no code change.
// Section 6: sources and reference documents are stored encrypted; documents are never
// downloadable from the UI (metadata only); every view and change is audited.
import crypto from 'node:crypto';
import { getRecord, putRecord, updateRecord, createRecord, listRecords, listBlobs, mapLimit, readBlob, blobKey, putFile, readLogs,
         READ_CONCURRENCY } from '../lib/db.js';
import { del } from '../lib/blob.js';
import { userId, publicUser, hashPassword, allUsers, OPEN_WORKSPACE } from '../lib/session.js';
import { issueReset, sendMail } from '../lib/resets.js';
import { audit } from '../lib/audit.js';
import { randomId, decrypt } from '../lib/crypto.js';
import { ok, bad, clean, slugify, siteOrigin } from '../lib/http.js';
import { isSuper, roleOf } from '../lib/tenancy.js';
import { DOC_TYPES, zoneOf, ZONE_BAD, fbUrlOf, FB_BAD, pagePublic as pageView, versionPublic, imageOnly, freeSlug, newPage,
         budgetIn, budgetChange, applyBudgets, BUDGETS, BUDGET_DEFAULTS } from '../lib/pages.js';
import { queueConversion } from './templates.js';

export { DOC_TYPES };
export const SOURCE_TYPES = ['website', 'social', 'reddit', 'search'];
// A source as sent (by the owner, or proposed by the onboarding routine): {src} or {error}
export function sourceIn(x) {
  x = x && typeof x === 'object' ? x : {};
  const type = SOURCE_TYPES.includes(x.type) ? x.type : 'website';
  let url;
  if (type === 'search') url = clean(x.url, 200);                     // a web search the collector runs
  else try { url = new URL(clean(x.url, 500)).href; } catch { return { error: 'enter a full URL (https://...)' }; }
  if (!url) return { error: 'enter the search words' };
  return { src: { id: randomId(6), url, type, label: clean(x.label, 100), active: x.active !== false, added_at: Date.now() } };
}
const MAX_DOC_BYTES = 3_200_000;   // a one-request upload: the request body limit is 4.5 MB; base64 adds a third
// Larger documents go up in parts (start, part..., finish): the 4.5 MB limit is per request, and the
// editors' template books (full-page examples) run to 40 MB. The parts stay stored as they came
// (encrypted) and are the document; the routine downloads it part by part (a response has the same limit).
export const PART_BYTES = 2_500_000;          // of the PDF per part: 3.3 MB as base64
export const MAX_UPLOAD_BYTES = 50_000_000;
const MAX_PARTS = Math.ceil(MAX_UPLOAD_BYTES / PART_BYTES);
const UPLOAD_ID = /^[A-Za-z0-9_-]{16}$/;      // randomId(12)
const ABANDONED_MS = 24 * 3600e3;             // parts of an upload that never finished are removed after a day
const EXTRACT_MS = 30e3;                      // reading the text: past this, the version is saved without it (60 s per request)
const mb = n => `${(n / 1e6).toFixed(1)} MB`;
const isPdf = buf => buf.subarray(0, 5).toString() === '%PDF-';
const NOT_PDF = 'that file is not a PDF: export the document as a PDF and upload that';
const partPath = (slug, type, upload, i) => `docs/${slug}/${type}/u-${upload}/part-${String(i).padStart(3, '0')}.enc`;
const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const ROLES = ['super', 'owner', 'editor'];
// a page's SocialPilot account is linked by the onboarding routine only (routes/routine.js onboard)
const LOGIN_BY_ROUTINE = 'the page\'s SocialPilot account is linked by the onboarding routine (Go live), never typed in';

// chars: the text the pages hold (not counting the page markers), for the image-only warning
export async function extractPdfText(buf) {
  const { extractText, getDocumentProxy } = await import('unpdf');
  const pdf = await getDocumentProxy(new Uint8Array(buf));
  const { totalPages, text } = await extractText(pdf, { mergePages: false });
  return { pages: totalPages, chars: text.reduce((n, t) => n + String(t || '').trim().length, 0),
           text: text.map((t, i) => `--- page ${i + 1} ---\n${t}`).join('\n') };
}

// what an owner or the super sees of a page (lib/pages.js), never its storage paths
const pagePublic = p => pageView(p);

// An upload refused: the message says what to do; the audit log keeps that it was tried (the Oct 23
// templates upload left no trace, so nobody could tell it had never arrived).
class UploadError extends Error {
  constructor(message, status = 400, code = 'refused') { super(message); this.status = status; this.code = code; }
}

async function withTimeout(p, ms) {
  let t;
  try { return await Promise.race([p, new Promise((_, no) => { t = setTimeout(() => no(new Error('reading the text took too long')), ms); })]); }
  finally { clearTimeout(t); }
}

// The whole PDF, checked: read its text and make it the active version. files: where its stored
// parts are. upload: the upload's id; a finish sent again (its answer lost) returns the version it made.
async function saveVersion(u, slug, type, buf, files, { filename, upload }) {
  if (!isPdf(buf)) throw new UploadError(NOT_PDF, 400, 'not_pdf');
  let ex = { pages: 0, chars: 0, text: '' };
  try { ex = await withTimeout(extractPdfText(buf), EXTRACT_MS); } catch (e) { ex = { pages: 0, chars: 0, text: '', error: String(e.message || e) }; }
  const txt = await putFile(`docs/${slug}/${type}/u-${upload}/text.txt.enc`, Buffer.from(ex.text || ''), { overwrite: true });
  const sha256 = crypto.createHash('sha256').update(buf).digest('hex');
  let made = null, again = false;
  const rec = await updateRecord('pages', slug, cur => {
    if (!cur) throw new UploadError('that page no longer exists', 404, 'no_page');
    const docs = { ...(cur.docs || {}) };
    const d = { versions: [], ...(docs[type] || {}) };
    const same = d.versions.find(x => x.upload === upload);
    if (same) { made = same; again = true; return null; }
    const v = d.versions.reduce((m, x) => Math.max(m, x.v), 0) + 1;
    made = { v, filename: clean(filename, 120) || `${type}.pdf`, size: buf.length, pages: ex.pages, chars: ex.chars, sha256,
             warning: ex.error ? `the text could not be read (${clean(ex.error, 160)})` : '', at: Date.now(), by: u.email,
             upload, parts: files, txt };
    docs[type] = { ...d, versions: [...d.versions, made], active: v };
    return { ...cur, docs };
  });
  if (!again) await audit(u, 'reference_uploaded', { page: slug, type, version: made.v, size: buf.length, pages: ex.pages,
                                                   chars: ex.chars, parts: files.length });
  // a finish sent again whose first try died before queueing the conversion (a slow PDF read): queued now;
  // one whose first answer was lost after queueing it: said again, as the first answer said it
  const conversion = type !== 'templates' ? undefined : (!again || !await conversionOf(slug, made.v))
    ? await convertTemplates(u, slug, made.v, 'new_document') : { job: (await getRecord('templates', slug))?.job?.id || 'queued' };
  return { page: pagePublic(rec || await getRecord('pages', slug)), version: versionPublic(made), extracted_chars: made.chars, warning: made.warning,
           ...(conversion ? { conversion } : {}) };
}

// Was this templates document version queued for conversion (its job), or converted already?
async function conversionOf(slug, v) {
  const tpl = await getRecord('templates', slug);
  const job = tpl && tpl.job && tpl.job.id ? await getRecord('jobs', tpl.job.id).catch(() => null) : null;
  return (job && job.doc_version === v) || Object.values((tpl && tpl.specs) || {}).some(t => t.versions.some(x => x.doc_version === v));
}

// A templates document made active: the Studio routine turns its templates into template specs for
// the editors to approve (routes/templates.js). The version is saved whatever happens here: a job
// that could not be queued says so, and an admin can press Convert templates again.
async function convertTemplates(u, slug, v, reason) {
  try {
    const r = await queueConversion(u.email, slug, { reason, doc_version: v });
    return { job: r.job.id, fired: r.fired };
  } catch (e) {
    console.error(`templates conversion for ${slug} v${v}:`, e && e.message);   // the admin reads the note: never the error itself
    return { job: null, error: 'The templates could not be queued for conversion: press Convert templates again under Templates.' };
  }
}

// Join the parts of an upload (start gave its id), check them against the file the browser read
// (size, SHA-256) and save the version.
async function finishUpload(u, slug, type, b) {
  const upload = String(b.upload || ''), n = Number(b.parts), size = Number(b.size);
  if (!UPLOAD_ID.test(upload)) throw new UploadError('the upload was not started: upload the file again');
  if (!Number.isInteger(n) || n < 1 || n > MAX_PARTS) throw new UploadError('the number of parts is wrong: upload the file again');
  const found = new Map();
  for (const x of await listBlobs(`docs/${slug}/${type}/u-${upload}/part-`)) {
    const m = x.pathname.match(/\/part-(\d{3})\.enc$/);
    if (m) found.set(Number(m[1]), x);
  }
  const missing = [...Array(n).keys()].filter(i => !found.has(i));
  if (missing.length) throw new UploadError(`part ${missing[0] + 1} of ${n} did not arrive: upload the file again`, 400, 'missing_part');
  if (found.size !== n) throw new UploadError('more parts arrived than the file has: upload the file again', 400, 'extra_parts');
  const blobs = [...Array(n).keys()].map(i => found.get(i));
  const buf = Buffer.concat(await mapLimit(blobs, READ_CONCURRENCY, x => readBlob(x.url, blobKey(x), decrypt)));
  const sha = String(b.sha256 || '').toLowerCase();
  if ((size && buf.length !== size) || (sha && crypto.createHash('sha256').update(buf).digest('hex') !== sha))
    throw new UploadError('the parts that arrived do not add up to the file you picked: upload it again', 400, 'mismatch');
  return saveVersion(u, slug, type, buf, blobs.map(x => x.pathname), { filename: b.filename, upload });
}

// parts of uploads that never finished (the browser closed, a part never came): gone after a day
async function pruneUploads(slug, type, d) {
  const keep = new Set((d.versions || []).map(x => x.upload).filter(Boolean));
  const old = (await listBlobs(`docs/${slug}/${type}/u-`)).filter(x => {
    const m = x.pathname.match(/\/u-([^/]+)\//);
    return m && !keep.has(m[1]) && Date.now() - new Date(x.uploadedAt).getTime() > ABANDONED_MS;
  });
  if (old.length) await del(old.map(x => x.url)).catch(() => {});
}

// The page's own settings sections, for the super (Admin > Pages) and the page's owner
// (routes/pages.js): the source sheet, lessons, sources and reference documents. The caller has
// checked that u may manage p.
export async function pageSection(req, res, u, p, sub, subId, step) {
  const id = p.slug, b = req.body || {};
  // the page's source sheet: categories with the names, outlets, accounts and subreddits the
  // page covers. Names in it rank stories higher (minus the ones excluded from the boost).
  // Encrypted with the page record; viewing it is audited; the super and the page's owner only.
  // what the routine learned from the editors' reviews (built daily by the hourly routine).
  // Admins see it, add notes the routine follows, override a kind's weight, or reset learning.
  if (sub === 'lessons') {
    const rec = (await getRecord('lessons', id)) || null;
    if (req.method === 'GET') {
      await audit(u, 'lessons_viewed', { page: id });
      return ok(res, { lessons: rec });
    }
    if (req.method === 'PUT') {
      // overrides: only the kinds sent (null or '' puts a kind back on auto), merged into the newest
      // copy, so two admins' weights and notes, and the routine's rebuild, are all kept
      const set = {}, off = [];
      if (b.overrides && typeof b.overrides === 'object') {
        for (const [k, v] of Object.entries(b.overrides).slice(0, 40)) {
          const n = typeof v === 'number' || (typeof v === 'string' && v.trim()) ? Number(v) : NaN;
          if (v === null || v === '') off.push(clean(k, 40));
          else if (!Number.isFinite(n)) return bad(res, `weights are numbers from 0.3 to 1.5 (${clean(k, 40)} was not a number): nothing was saved`);
          else {
            if (n < 0.3 || n > 1.5) return bad(res, `weights go from 0.3 to 1.5 (${clean(k, 40)} was ${n}): nothing was saved`);
            set[clean(k, 40)] = n;
          }
        }
      }
      const next = await updateRecord('lessons', id, cur => {
        const n = { ...(cur || { page: id }) };
        if (b.admin_notes !== undefined) n.admin_notes = clean(b.admin_notes, 2000);
        if (b.overrides && typeof b.overrides === 'object') {
          n.overrides = { ...(n.overrides || {}), ...set };
          for (const k of off) delete n.overrides[k];
        }
        return n;
      });
      await audit(u, 'lessons_changed', { page: id, fields: Object.keys(b) });
      return ok(res, { lessons: next });
    }
    if (req.method === 'POST' && subId === 'reset') {
      if (b.confirm !== true) return bad(res, 'confirm the reset');
      // the learning goes; the admins' notes and weights stay
      const next = await updateRecord('lessons', id, cur => ({ page: id, reset_at: Date.now(), admin_notes: (cur && cur.admin_notes) || '',
                                                               overrides: (cur && cur.overrides) || {}, kinds: {}, rules: [] }));
      await audit(u, 'lessons_reset', { page: id });
      return ok(res, { lessons: next });
    }
  }

  if (sub === 'sheet') {
    const pl = p.pipeline || {};
    const sheet = pl.source_sheet || { categories: [] };
    const cats = sheet.categories || [];
    const all = [...new Set(cats.flatMap(c => c.entities || []))];
    const excluded = sheet.priority_excluded
      || (sheet.priority_terms ? all.filter(n => !sheet.priority_terms.includes(n)) : []);
    if (req.method === 'GET') {
      await audit(u, 'source_sheet_viewed', { page: id });
      return ok(res, { categories: cats, priority_excluded: excluded, boosted: all.filter(n => !excluded.includes(n)).length,
                       note: sheet._source || '' });
    }
    if (req.method === 'PUT') {
      const list = v => (Array.isArray(v) ? v : String(v || '').split(/[\n;]+/)).map(x => clean(x, 120)).filter(Boolean).slice(0, 2000);
      const categories = (b.categories || []).slice(0, 200).map(c => ({ category: clean(c.category, 120), entities: list(c.entities),
        websites: list(c.websites), x: list(c.x), reddit: list(c.reddit), fb_benchmarks: list(c.fb_benchmarks) })).filter(c => c.category);
      const ex = list(b.priority_excluded);
      const exLow = new Set(ex.map(x => x.toLowerCase()));
      const terms = [...new Set(categories.flatMap(c => c.entities))].filter(n => !exLow.has(n.toLowerCase()));
      // on the newest copy of the page: a settings save or an upload meanwhile is kept
      await updateRecord('pages', id, cur => cur && { ...cur, pipeline: { ...(cur.pipeline || {}), priority_terms: terms,
        source_sheet: { ...((cur.pipeline || {}).source_sheet || {}), categories, priority_excluded: ex, priority_terms: terms,
                        edited_at: Date.now(), edited_by: u.email } } });
      await audit(u, 'source_sheet_changed', { page: id, categories: categories.length, boosted: terms.length });
      return ok(res, { categories, priority_excluded: ex, boosted: terms.length });
    }
  }

  // sources (encrypted with the page record; listing them is an audited view)
  if (sub === 'sources') {
    if (req.method === 'GET') {
      await audit(u, 'sources_viewed', { page: id });
      return ok(res, { sources: p.sources || [], types: SOURCE_TYPES });
    }
    // the change, made on the newest copy of the page (a settings save or another source meanwhile is kept)
    let change;
    if (req.method === 'POST' && !subId) {
      const r = sourceIn({ ...b, active: true });
      if (r.error) return bad(res, r.error);
      change = sources => [...sources, r.src];
    } else {
      if (!(p.sources || []).some(s => s.id === subId)) return bad(res, 'no such source', 404);
      let url;
      if (req.method === 'PATCH' && b.url) { try { url = new URL(clean(b.url, 500)).href; } catch { return bad(res, 'enter a full URL'); } }
      change = sources => sources.flatMap(s => {
        if (s.id !== subId) return [s];
        if (req.method === 'DELETE') return [];
        if (req.method !== 'PATCH') return [s];
        const n = { ...s };
        if (typeof b.active === 'boolean') n.active = b.active;
        if (b.label !== undefined) n.label = clean(b.label, 100);
        if (b.type && SOURCE_TYPES.includes(b.type)) n.type = b.type;
        if (url) n.url = url;
        return [n];
      });
    }
    const rec = await updateRecord('pages', id, cur => cur && { ...cur, sources: change(cur.sources || []) });
    if (!rec) return bad(res, 'no such page', 404);
    const sources = rec.sources || [];
    await audit(u, 'sources_changed', { page: id, method: req.method, count: sources.length });
    return ok(res, { sources });
  }

  // reference documents: upload a new version, or roll back to an earlier one.
  //   POST docs/<type>/start   {filename, size}               -> {upload, part_bytes, parts}
  //   POST docs/<type>/part    {upload, index, data_b64}      one part (part_bytes of the PDF), sent again if its answer is lost
  //   POST docs/<type>/finish  {upload, parts, size, sha256, filename} -> {page, version, extracted_chars, warning}
  //   POST docs/<type>         {filename, data_b64}           a small PDF in one request (up to 3.2 MB)
  if (sub === 'docs') {
    const type = subId;
    if (!DOC_TYPES[type]) return bad(res, `document type must be one of ${Object.keys(DOC_TYPES).join(', ')}`);
    const d = { versions: [], ...((p.docs || {})[type] || {}) };
    try {
      if (req.method === 'POST' && step === 'part') {
        const upload = String(b.upload || ''), index = Number(b.index);
        if (!UPLOAD_ID.test(upload)) throw new UploadError('the upload was not started: upload the file again');
        if (!Number.isInteger(index) || index < 0 || index >= MAX_PARTS) throw new UploadError('that part is outside the file: upload the file again');
        const buf = Buffer.from(String(b.data_b64 || ''), 'base64');
        if (!buf.length) throw new UploadError('that part arrived empty: upload the file again');
        if (buf.length > PART_BYTES) throw new UploadError(`a part holds at most ${PART_BYTES} bytes: reload the page and upload again`, 413);
        if (index === 0 && !isPdf(buf)) throw new UploadError(NOT_PDF, 400, 'not_pdf');
        await putFile(partPath(id, type, upload, index), buf, { overwrite: true });
        return ok(res, { index, bytes: buf.length });
      }
      if (req.method === 'POST' && step === 'start') {
        const size = Number(b.size);
        if (!(size > 0)) throw new UploadError('that file is empty: pick the PDF again');
        if (size > MAX_UPLOAD_BYTES) throw new UploadError(`this PDF is ${mb(size)} and the limit is ${mb(MAX_UPLOAD_BYTES)}: `
          + 'export it with smaller images, or split it into two documents', 413, 'too_large');
        await pruneUploads(id, type, d);
        const upload = randomId(12);
        await audit(u, 'reference_upload_started', { page: id, type, size, filename: clean(b.filename, 120) });
        return ok(res, { upload, part_bytes: PART_BYTES, parts: Math.ceil(size / PART_BYTES) });
      }
      if (req.method === 'POST' && step === 'finish') return ok(res, await finishUpload(u, id, type, b));
      if (req.method === 'POST' && !step) {
        const buf = Buffer.from(String(b.data_b64 || ''), 'base64');
        if (!buf.length) throw new UploadError('attach the PDF');
        if (buf.length > MAX_DOC_BYTES) throw new UploadError('PDF too large for one request (max 3 MB): upload it from the page\'s settings, which send it in parts', 413, 'too_large');
        if (!isPdf(buf)) throw new UploadError(NOT_PDF, 400, 'not_pdf');
        const upload = randomId(12);
        const path = await putFile(partPath(id, type, upload, 0), buf);
        return ok(res, await saveVersion(u, id, type, buf, [path], { filename: b.filename, upload }));
      }
    } catch (e) {
      if (!(e instanceof UploadError)) throw e;
      await audit(u, 'reference_upload_failed', { page: id, type, step: step || 'single', reason: e.code });
      return bad(res, e.message, e.status);
    }
    if (req.method === 'PATCH') {                                   // {active: v}
      const v = Number(b.active);
      if (!d.versions.some(x => x.v === v)) return bad(res, 'no such version');
      // on the newest copy: an upload finishing meanwhile keeps its version
      const rec = await updateRecord('pages', id, cur => ({ ...cur, docs: { ...cur.docs, [type]: { ...cur.docs[type], active: v } } }));
      await audit(u, 'reference_rolled_back', { page: id, type, version: v });
      // a templates document none of whose templates were converted yet (those of a version made
      // active again are there already)
      const tpl = type === 'templates' ? await getRecord('templates', id) : null;
      const done = tpl && Object.values(tpl.specs || {}).some(t => t.versions.some(x => x.doc_version === v));
      const conversion = type === 'templates' && !done ? await convertTemplates(u, id, v, 'new_document') : undefined;
      return ok(res, { page: pagePublic(rec), ...(conversion ? { conversion } : {}) });
    }
  }
  return bad(res, 'not found', 404);
}

// Users of a workspace: the super invites testers (owners) and anyone else; owners invite their editors
// (routes/workspace.js). pages: only the workspace's own.
export async function workspacePages(wsId, pages) {
  const mine = new Set((await listRecords('pages')).filter(p => p.workspace === wsId).map(p => p.slug));
  const asked = (Array.isArray(pages) ? pages : []).map(String);
  const foreign = asked.filter(s => !mine.has(s));
  return { pages: asked.filter(s => mine.has(s)), foreign };
}

// the one workspace all these pages belong to, or '' (none given, or pages of several)
async function pagesWorkspace(pages) {
  const asked = new Set((Array.isArray(pages) ? pages : []).map(String));
  const ws = new Set((await listRecords('pages')).filter(p => asked.has(p.slug)).map(p => p.workspace || ''));
  return asked.size && ws.size === 1 ? [...ws][0] : '';
}

export async function inviteUser(req, u, { email, name, role, workspace, pages }) {
  email = clean(email, 200).toLowerCase();
  if (!EMAIL.test(email)) return { error: 'enter a valid email' };
  const uid = userId(email);
  if (await getRecord('users', uid)) return { error: 'that user already exists', status: 409 };
  const nu = { id: uid, email, name: clean(name, 100), role, workspace: role === 'super' ? null : workspace, pages: role === 'editor' ? pages : [],
               active: true, must_change: true, created_at: Date.now(), invited_by: u.email, pw: hashPassword(randomId(24)) };
  await putRecord('users', uid, nu);
  const link = await issueReset(nu, siteOrigin(req));
  const emailed = await sendMail(email, 'Your account', `You have been invited. Set your password here (valid 48 hours):\n${link}`);
  await audit(u, 'user_invited', { target: email, role, ...(nu.workspace ? { workspace: nu.workspace } : {}) });
  return { user: publicUser(nu), setup_link: link, emailed };
}

// a workspace's id: from its name, unique
async function freeWorkspaceId(name) {
  let base = slugify(name) || 'workspace';
  if (base === OPEN_WORKSPACE) base = `${base}-tester`;             // the open-access owner's own (lib/session.js)
  for (let n = 1; n < 50; n++) {
    const id = n === 1 ? base : `${base.slice(0, 56)}-${n}`;
    if (!await getRecord('workspaces', id)) return id;
  }
  return `${base.slice(0, 40)}-${randomId(4).toLowerCase()}`;
}
const CONNECT_MAX = 3000;
export const settingsPublic = rec => ({ socialpilot_connect: (rec && rec.socialpilot_connect) || '', changed_at: (rec && rec.changed_at) || null,
                                        changed_by: (rec && rec.changed_by) || '' });
const workspacePublic = (w, users = [], pages = []) => ({ id: w.id, name: w.name, owner: w.owner_email || '', limits: w.limits || {},
  created_at: w.created_at || null, pages: pages.filter(p => p.workspace === w.id).map(p => p.slug),
  users: users.filter(x => x.workspace === w.id).length });

export default async function handler(req, res) {
  const u = req.user;
  if (!u) return bad(res, 'sign in first', 401);            // a session that ended: the screen asks to sign in again
  // the admin panel is the product owner's; owners manage their workspace in routes/pages.js and routes/workspace.js
  if (!isSuper(u)) return bad(res, 'only the product owner can open this', 403);
  const [what, id, sub, subId] = req.subpath;
  const b = req.body || {};

  // ---------------------------------------------------------------- testers and their workspaces
  // POST testers {email, name, workspace_name, posts_per_day?, premium_per_day?}: a workspace and its
  // owner's invite (a set-password link). The budgets are the most each of its pages may use.
  if (what === 'testers' && req.method === 'POST' && !id) {
    const wsName = clean(b.workspace_name, 100) || clean(b.name, 100);
    if (!wsName) return bad(res, 'name the workspace');
    const limits = { ...BUDGET_DEFAULTS };
    for (const k of BUDGETS) {
      const r = budgetIn(k, b[k]);
      if (r.error) return bad(res, r.error);
      if (r.value !== undefined) limits[k] = r.value;
    }
    const email = clean(b.email, 200).toLowerCase();
    if (!EMAIL.test(email)) return bad(res, 'enter a valid email');
    if (await getRecord('users', userId(email))) return bad(res, 'that user already exists', 409);
    const ws = { id: await freeWorkspaceId(wsName), name: wsName, limits, owner_id: userId(email), owner_email: email,
                 created_at: Date.now(), created_by: u.email };
    await createRecord('workspaces', ws.id, ws);
    const r = await inviteUser(req, u, { email, name: b.name, role: 'owner', workspace: ws.id, pages: [] });
    if (r.error) return bad(res, r.error, r.status);
    await audit(u, 'tester_invited', { target: email, workspace: ws.id, limits });
    return ok(res, { workspace: workspacePublic(ws), ...r });
  }
  if (what === 'workspaces' || (what === 'testers' && req.method === 'GET')) {
    if (req.method === 'GET' && !id) {
      const [all, users, pages] = await Promise.all([listRecords('workspaces'), allUsers(), listRecords('pages')]);
      return ok(res, { workspaces: all.sort((a, c) => (a.created_at || 0) - (c.created_at || 0)).map(w => workspacePublic(w, users, pages)) });
    }
    // PATCH workspaces/<id> {name?, limits: {posts_per_day?, premium_per_day?}}: the most each of its pages may use
    if (what === 'workspaces' && req.method === 'PATCH' && id) {
      const lim = {};
      for (const k of BUDGETS) {
        const r = budgetIn(k, (b.limits || {})[k] ?? b[k]);
        if (r.error) return bad(res, r.error);
        if (r.value !== undefined) lim[k] = r.value;
      }
      const next = await updateRecord('workspaces', id, cur => cur && { ...cur, ...(b.name !== undefined && clean(b.name, 100) ? { name: clean(b.name, 100) } : {}),
                                                                     limits: { ...(cur.limits || {}), ...lim } });
      if (!next) return bad(res, 'no such workspace', 404);
      await audit(u, 'workspace_changed', { workspace: id, limits: next.limits });
      return ok(res, { workspace: workspacePublic(next) });
    }
  }

  // ---------------------------------------------------------------- users
  if (what === 'users') {
    if (req.method === 'GET') return ok(res, { users: (await allUsers()).map(x => ({ ...publicUser(x),
      last_login: x.last_login || null, reset_requested: x.reset_requested || null, locked: (x.locked_until || 0) > Date.now() })) });
    if (req.method === 'POST' && !id) {                              // invite
      const role = b.role === 'admin' ? 'super' : ROLES.includes(b.role) ? b.role : 'editor';
      let workspace = null, pages = [];
      if (role !== 'super') {
        // not named (the users screen sends only the pages): the workspace of the pages given, when they are all one's
        workspace = clean(b.workspace, 80) || await pagesWorkspace(b.pages);
        if (!workspace || !await getRecord('workspaces', workspace)) return bad(res, 'choose the workspace the user belongs to');
        const w = await workspacePages(workspace, b.pages);
        if (w.foreign.length) return bad(res, `not a page of that workspace: ${w.foreign[0]}`);
        pages = w.pages;
      }
      const r = await inviteUser(req, u, { email: b.email, name: b.name, role, workspace, pages });
      if (r.error) return bad(res, r.error, r.status);
      return ok(res, r);
    }
    const target = id && await getRecord('users', id);
    if (!target) return bad(res, 'no such user', 404);
    if (req.method === 'PATCH') {
      const role = b.role === undefined ? undefined : b.role === 'admin' ? 'super' : ROLES.includes(b.role) ? b.role : 'editor';
      if (target.id === u.id && ((role && role !== 'super') || b.active === false)) return bad(res, 'you cannot remove your own product owner access');
      const workspace = b.workspace !== undefined ? clean(b.workspace, 80) || null : undefined;
      if (workspace && !await getRecord('workspaces', workspace)) return bad(res, 'no such workspace');
      const ws = workspace !== undefined ? workspace : target.workspace || null;
      // an owner or an editor belongs to a workspace, and is given only its pages
      if ((role || roleOf(target)) !== 'super' && !ws) return bad(res, 'choose the workspace the user belongs to');
      const add = Array.isArray(b.pages_add) ? b.pages_add.map(String) : [], remove = Array.isArray(b.pages_remove) ? b.pages_remove.map(String) : [];
      const sent = [...(Array.isArray(b.pages) ? b.pages.map(String) : []), ...add];
      const w = ws ? await workspacePages(ws, sent) : { foreign: sent };
      if (w.foreign.length) return bad(res, `not a page of the user's workspace: ${w.foreign[0]}`);
      // archived pages are not offered in the row, so a list sent from it never removes them
      const archived = Array.isArray(b.pages) ? new Set((await listRecords('pages')).filter(p => p.archived).map(p => p.slug)) : null;
      // only what was sent, on the newest copy: another admin's change, or the user signing in, is kept
      const next = await updateRecord('users', id, cur => {
        if (!cur) return null;
        const n = { ...cur };
        if (role) n.role = role;
        if (workspace !== undefined) { n.workspace = workspace; if (workspace !== (cur.workspace || null)) n.pages = []; }
        if (n.role === 'super' && !cur.open_access) n.workspace = null;   // (the open-access owner keeps its own)
        if (archived) n.pages = [...new Set([...b.pages.map(String), ...(cur.pages || []).filter(s => archived.has(s))])];
        if (add.length || remove.length) n.pages = [...new Set([...(n.pages || []).filter(s => !remove.includes(s)), ...add])];
        if (typeof b.active === 'boolean') { n.active = b.active; if (!b.active) n.sessions_after = Date.now(); }
        if (b.name !== undefined) n.name = clean(b.name, 100);
        return n;
      });
      if (!next) return bad(res, 'no such user', 404);
      await audit(u, 'user_changed', { target: target.email, role: next.role, pages: next.pages, active: next.active, workspace: next.workspace || null });
      return ok(res, { user: publicUser(next) });
    }
    if (req.method === 'POST' && sub === 'reset') {
      if (target.active === false) return bad(res, 'this user is deactivated: press Activate first, then send the link');
      const link = await issueReset(target, siteOrigin(req));
      const emailed = await sendMail(target.email, 'Reset your password', `Set a new password here (valid 48 hours, one use):\n${link}`);
      await audit(u, 'user_reset_link', { target: target.email });
      return ok(res, { setup_link: link, emailed });
    }
  }

  // ---------------------------------------------------------------- pages
  if (what === 'pages') {
    if (req.method === 'GET' && !id) {
      const ws = Object.fromEntries((await listRecords('workspaces')).map(w => [w.id, w]));
      return ok(res, { pages: (await listRecords('pages')).map(p => pageView(p, ws[p.workspace])), doc_types: DOC_TYPES,
                       doc_max_bytes: MAX_UPLOAD_BYTES });
    }
    if (req.method === 'POST' && !id) {
      const r = await createPage(u, b, clean(b.workspace, 80), { exact: true });
      return r.error ? bad(res, r.error, r.status) : ok(res, r);
    }
    const p = id && await getRecord('pages', id);
    if (!p) return bad(res, 'no such page', 404);
    if (!sub && req.method === 'PATCH') {
      const r = await changePage(u, p, b);
      return r.error ? bad(res, r.error, r.status) : ok(res, r);
    }
    return pageSection(req, res, u, p, sub, subId, req.subpath[4]);
  }

  // ---------------------------------------------------------------- the product's own settings
  // settings/product {socialpilot_connect}: the text owners read on a page's "Connect in SocialPilot"
  // step (how to reach the product's SocialPilot and add their page there), written by the super
  if (what === 'settings' && !id) {
    if (req.method === 'GET') return ok(res, { settings: settingsPublic(await getRecord('settings', 'product')) });
    if (req.method === 'PATCH') {
      if (b.socialpilot_connect === undefined) return bad(res, 'send socialpilot_connect');
      const text = String(b.socialpilot_connect ?? '').replace(/\r\n?/g, '\n').slice(0, CONNECT_MAX).trim();
      const next = await updateRecord('settings', 'product', cur => ({ ...(cur || {}), socialpilot_connect: text, changed_at: Date.now(), changed_by: u.email }));
      await audit(u, 'settings_changed', { fields: ['socialpilot_connect'] });
      return ok(res, { settings: settingsPublic(next) });
    }
  }

  // ---------------------------------------------------------------- logs
  if (what === 'audit' && req.method === 'GET') {
    await audit(u, 'audit_viewed');
    return ok(res, { entries: (await readLogs('audit', Math.min(Number(req.query.days || 7), 31))).slice(0, 500) });
  }
  if (what === 'usage' && req.method === 'GET') {
    return ok(res, { entries: (await readLogs('usage', Math.min(Number(req.query.days || 7), 31))).slice(0, 2000) });
  }
  return bad(res, 'not found', 404);
}

// A new page in a workspace (the owner's own; the super names one). exact: a slug that is taken is
// refused (the super's panel names it), else the next free one is taken.
export async function createPage(u, b, workspace, { exact = false } = {}) {
  if (!clean(b.name)) return { error: 'name the page' };
  const ws = workspace && await getRecord('workspaces', workspace);
  if (!ws) return { error: 'choose the workspace the page belongs to' };
  let slug;
  if (exact) {
    slug = slugify(b.slug || b.name).replace(/\d{8,}/g, m => m.slice(0, 7));
    if (slug && await getRecord('pages', slug)) return { error: 'a page with that id exists', status: 409 };
  } else slug = await freeSlug(b.name);
  if (!slug) return { error: 'name the page' };
  if (clean(b.socialpilot_login_id, 30)) return { error: LOGIN_BY_ROUTINE };
  const zone = zoneOf(clean(b.timezone, 60));
  if (zone == null) return { error: ZONE_BAD };
  const fb = fbUrlOf(b.fb_page_url);
  if (fb == null) return { error: FB_BAD };
  const p = newPage(slug, ws, { ...b, timezone: zone || 'America/New_York', fb_page_url: fb }, u.email);
  const set = budgetChange(u, b, isSuper(u));
  if (set.error) return set;
  const withBudgets = applyBudgets(p, ws, set, isSuper(u));
  if (withBudgets.error) return withBudgets;
  await createRecord('pages', slug, withBudgets.page);
  await audit(u, 'page_added', { page: slug, workspace: ws.id });
  return { page: pageView(withBudgets.page, ws) };
}

// A page's settings: only the fields sent, on the newest copy (another person's change, a source added
// or a document version an upload just finished are kept). The page's SocialPilot account is never set
// here (the onboarding routine links it); the budgets are the super's to raise.
export async function changePage(u, p, b) {
  if (b.socialpilot_login_id !== undefined) return { error: LOGIN_BY_ROUTINE };
  const set = {};
  for (const k of ['name', 'fb_page_id', 'language']) if (b[k] !== undefined) set[k] = clean(b[k], 100);
  if (b.niche !== undefined) set.niche = clean(b.niche, 400);
  if (set.name === '') return { error: 'name the page' };
  if (b.timezone !== undefined && (set.timezone = zoneOf(clean(b.timezone, 60))) == null) return { error: ZONE_BAD };
  if (b.fb_page_url !== undefined && (set.fb_page_url = fbUrlOf(b.fb_page_url)) == null) return { error: FB_BAD };
  const budgets = budgetChange(u, b, isSuper(u));
  if (budgets.error) return budgets;
  const ws = p.workspace ? await getRecord('workspaces', p.workspace) : null;
  let refused = null;
  const next = await updateRecord('pages', p.slug, cur => {
    refused = null;
    if (!cur) return null;
    let n = { ...cur, ...set };
    if (b.theme) n.theme = { ...cur.theme, ...b.theme };
    if (typeof b.archived === 'boolean') { if (!isSuper(u)) { refused = { error: 'only the product owner archives pages', status: 403 }; return null; } n.archived = b.archived; }
    if (b.playbook !== undefined) n.playbook = b.playbook;       // structured page guidance (JSON), the routine's first draft edited
    // the owner's tick: they connected the page in SocialPilot (the steps are on the page)
    if (typeof b.socialpilot_connected === 'boolean') n.socialpilot_connected = b.socialpilot_connected ? { at: Date.now(), by: u.email } : null;
    const r = applyBudgets(n, ws, budgets, isSuper(u));
    if (r.error) { refused = r; return null; }
    n = r.page;
    return n;
  });
  if (refused) return refused;
  if (!next) return { error: 'no such page', status: 404 };
  await audit(u, 'page_changed', { page: p.slug, fields: Object.keys(b) });
  return { page: pageView(next, ws) };
}
