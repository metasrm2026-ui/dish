// /api/admin/*  (admins only) users, pages, sources, reference documents, audit and usage logs.
// Section 5: everything that varies by brand is configured here, with no code change.
// Section 6: sources and reference documents are stored encrypted; documents are never
// downloadable from the UI (metadata only); every view and change is audited.
import crypto from 'node:crypto';
import { getRecord, putRecord, updateRecord, listRecords, listBlobs, mapLimit, readBlob, blobKey, putFile, readLogs,
         READ_CONCURRENCY } from '../lib/db.js';
import { del } from '../lib/blob.js';
import { userId, publicUser, hashPassword, allUsers } from '../lib/session.js';
import { issueReset, sendMail } from '../lib/resets.js';
import { audit } from '../lib/audit.js';
import { randomId, decrypt } from '../lib/crypto.js';
import { ok, bad, clean, slugify, siteOrigin } from '../lib/http.js';
import { queueConversion } from './templates.js';

export const DOC_TYPES = { templates: 'Post templates', visual: 'Visual references', tone: 'Caption language and tone' };
const SOURCE_TYPES = ['website', 'social', 'reddit', 'search'];
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
// fewer characters a page than this: the pages are pictures (text flattened or outlined in the
// design tool), so the ideas step, which reads the text, sees almost nothing of the templates
const LOW_TEXT_PER_PAGE = 30;
export const imageOnly = (chars, pages) => chars < LOW_TEXT_PER_PAGE * Math.max(1, pages || 0);
const mb = n => `${(n / 1e6).toFixed(1)} MB`;
const isPdf = buf => buf.subarray(0, 5).toString() === '%PDF-';
const NOT_PDF = 'that file is not a PDF: export the document as a PDF and upload that';
const partPath = (slug, type, upload, i) => `docs/${slug}/${type}/u-${upload}/part-${String(i).padStart(3, '0')}.enc`;

// A page's SocialPilot account is its numeric loginId (SocialPilot's account list). The owner's
// rule: "Keaton Caleb" (2662937) is never posted to, so it can never be a page's account either.
const NEVER_LOGIN_IDS = ['2662937', ...String(process.env.SOCIALPILOT_BLOCKED_IDS || '').split(',').map(s => s.trim()).filter(Boolean)];
function loginError(v) {
  if (v === '') return '';
  if (!/^\d{1,15}$/.test(v)) return 'the SocialPilot account is its loginId: a number, from SocialPilot\'s account list';
  if (NEVER_LOGIN_IDS.includes(v)) return 'that SocialPilot account must never be used: set the page\'s own account';
  return '';
}

// A page's time zone as the routines read it (Python's zoneinfo, without the old alias names): an
// Area/City name. Intl alone is not enough: it also takes 'EST' and 'US/Eastern', which the routines'
// machines do not know. Old names (the browser's list still has some) are stored as their new one.
const ZONE = /^(Africa|America|Antarctica|Arctic|Asia|Atlantic|Australia|Europe|Indian|Pacific)(\/[A-Za-z0-9_+-]+)+$/;
const ZONE_NEW = { 'Asia/Calcutta': 'Asia/Kolkata', 'Asia/Katmandu': 'Asia/Kathmandu', 'Asia/Saigon': 'Asia/Ho_Chi_Minh',
  'Asia/Rangoon': 'Asia/Yangon', 'Asia/Dacca': 'Asia/Dhaka', 'Europe/Kiev': 'Europe/Kyiv', 'Africa/Asmera': 'Africa/Asmara',
  'America/Buenos_Aires': 'America/Argentina/Buenos_Aires', 'America/Catamarca': 'America/Argentina/Catamarca',
  'America/Cordoba': 'America/Argentina/Cordoba', 'America/Jujuy': 'America/Argentina/Jujuy', 'America/Mendoza': 'America/Argentina/Mendoza',
  'America/Godthab': 'America/Nuuk', 'America/Indianapolis': 'America/Indiana/Indianapolis', 'America/Louisville': 'America/Kentucky/Louisville',
  'Atlantic/Faeroe': 'Atlantic/Faroe', 'Pacific/Enderbury': 'Pacific/Kanton', 'Pacific/Ponape': 'Pacific/Pohnpei', 'Pacific/Truk': 'Pacific/Chuuk' };
// Intl ignores letter case but zoneinfo does not, so the name is stored as Intl spells it.
function zoneOf(v) {                  // the name to store, or null when it is not a time zone
  if (v === '' || v === 'UTC') return v;
  if (!new RegExp(ZONE.source, 'i').test(v)) return null;
  let z;
  try { z = new Intl.DateTimeFormat('en-US', { timeZone: v }).resolvedOptions().timeZone; } catch { return null; }
  z = ZONE_NEW[z] || z;
  return ZONE.test(z) ? z : null;
}
const ZONE_BAD = 'the time zone must be a name like America/New_York or Europe/London (or UTC): pick one from the list';

// chars: the text the pages hold (not counting the page markers), for the image-only warning
export async function extractPdfText(buf) {
  const { extractText, getDocumentProxy } = await import('unpdf');
  const pdf = await getDocumentProxy(new Uint8Array(buf));
  const { totalPages, text } = await extractText(pdf, { mergePages: false });
  return { pages: totalPages, chars: text.reduce((n, t) => n + String(t || '').trim().length, 0),
           text: text.map((t, i) => `--- page ${i + 1} ---\n${t}`).join('\n') };
}

// what the admin sees of a version (never its storage paths). chars is null for versions uploaded
// before it was measured.
const versionPublic = ({ v, filename, size, pages, chars, warning, at, by }) => ({ v, filename, size, pages,
  chars: chars ?? null, image_only: chars == null ? null : imageOnly(chars, pages), warning: warning || '', at, by });

function pagePublic(p) {
  const docs = {};
  for (const t of Object.keys(DOC_TYPES)) {
    const d = (p.docs || {})[t] || {};
    docs[t] = { active: d.active || null, versions: (d.versions || []).map(versionPublic) };
  }
  return { slug: p.slug, name: p.name, fb_page_id: p.fb_page_id || '', socialpilot_login_id: p.socialpilot_login_id || '',
           timezone: p.timezone || 'America/New_York', archived: !!p.archived, theme: p.theme || {},
           posts_per_day: p.posts_per_day || 8, source_count: (p.sources || []).length, docs };
}

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

export default async function handler(req, res) {
  const u = req.user;
  if (!u) return bad(res, 'sign in first', 401);            // a session that ended: the screen asks to sign in again
  if (u.role !== 'admin') return bad(res, 'admins only', 403);
  const [what, id, sub, subId] = req.subpath;
  const b = req.body || {};

  // ---------------------------------------------------------------- users
  if (what === 'users') {
    if (req.method === 'GET') return ok(res, { users: (await allUsers()).map(x => ({ ...publicUser(x),
      last_login: x.last_login || null, reset_requested: x.reset_requested || null, locked: (x.locked_until || 0) > Date.now() })) });
    if (req.method === 'POST' && !id) {                              // invite
      const email = clean(b.email, 200).toLowerCase();
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return bad(res, 'enter a valid email');
      const uid = userId(email);
      if (await getRecord('users', uid)) return bad(res, 'that user already exists', 409);
      const nu = { id: uid, email, name: clean(b.name, 100), role: b.role === 'admin' ? 'admin' : 'editor',
                   pages: (b.pages || []).map(String), active: true, must_change: true, created_at: Date.now(),
                   pw: hashPassword(randomId(24)) };
      await putRecord('users', uid, nu);
      const link = await issueReset(nu, siteOrigin(req));
      const emailed = await sendMail(email, 'Your account', `You have been invited. Set your password here (valid 48 hours):\n${link}`);
      await audit(u, 'user_invited', { target: email, role: nu.role });
      return ok(res, { user: publicUser(nu), setup_link: link, emailed });
    }
    const target = id && await getRecord('users', id);
    if (!target) return bad(res, 'no such user', 404);
    if (req.method === 'PATCH') {
      if (target.id === u.id && ((b.role && b.role !== 'admin') || b.active === false)) return bad(res, 'you cannot remove your own admin access');
      // archived pages are not offered in the row, so a list sent from it never removes them
      const archived = Array.isArray(b.pages) ? new Set((await listRecords('pages')).filter(p => p.archived).map(p => p.slug)) : null;
      // or only the pages ticked and unticked in the row: another admin's grant meanwhile is kept
      const add = Array.isArray(b.pages_add) ? b.pages_add.map(String) : [], remove = Array.isArray(b.pages_remove) ? b.pages_remove.map(String) : [];
      // only what was sent, on the newest copy: another admin's change, or the user signing in, is kept
      const next = await updateRecord('users', id, cur => {
        if (!cur) return null;
        const n = { ...cur };
        if (b.role) n.role = b.role === 'admin' ? 'admin' : 'editor';
        if (archived) n.pages = [...new Set([...b.pages.map(String), ...(cur.pages || []).filter(s => archived.has(s))])];
        if (add.length || remove.length) n.pages = [...new Set([...(n.pages || []).filter(s => !remove.includes(s)), ...add])];
        if (typeof b.active === 'boolean') { n.active = b.active; if (!b.active) n.sessions_after = Date.now(); }
        if (b.name !== undefined) n.name = clean(b.name, 100);
        return n;
      });
      if (!next) return bad(res, 'no such user', 404);
      await audit(u, 'user_changed', { target: target.email, role: next.role, pages: next.pages, active: next.active });
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
    if (req.method === 'GET' && !id) return ok(res, { pages: (await listRecords('pages')).map(pagePublic), doc_types: DOC_TYPES,
                                                                 doc_max_bytes: MAX_UPLOAD_BYTES });
    if (req.method === 'POST' && !id) {
      const slug = slugify(b.slug || b.name);
      if (!slug || !clean(b.name)) return bad(res, 'name the page');
      if (await getRecord('pages', slug)) return bad(res, 'a page with that id exists', 409);
      const loginBad = loginError(clean(b.socialpilot_login_id, 30));
      if (loginBad) return bad(res, loginBad);
      const zone = zoneOf(clean(b.timezone, 60));
      if (zone == null) return bad(res, ZONE_BAD);
      const p = { slug, name: clean(b.name, 100), fb_page_id: clean(b.fb_page_id, 60), socialpilot_login_id: clean(b.socialpilot_login_id, 30),
                  timezone: zone || 'America/New_York', theme: b.theme || {}, posts_per_day: Number(b.posts_per_day) || 8,
                  sources: [], docs: {}, archived: false, created_at: Date.now() };
      await putRecord('pages', slug, p);
      await audit(u, 'page_added', { page: slug });
      return ok(res, { page: pagePublic(p) });
    }
    const p = id && await getRecord('pages', id);
    if (!p) return bad(res, 'no such page', 404);

    if (!sub && req.method === 'PATCH') {
      const set = {};
      for (const k of ['name', 'fb_page_id', 'socialpilot_login_id', 'timezone']) if (b[k] !== undefined) set[k] = clean(b[k], 100);
      if (set.name === '') return bad(res, 'name the page');
      const loginBad = set.socialpilot_login_id !== undefined && loginError(set.socialpilot_login_id);
      if (loginBad) return bad(res, loginBad);
      if (set.timezone !== undefined && (set.timezone = zoneOf(set.timezone)) == null) return bad(res, ZONE_BAD);
      // only the fields sent, on the newest copy: another admin's change, a source added or a
      // document version an upload just finished are kept
      const next = await updateRecord('pages', id, cur => {
        if (!cur) return null;
        const n = { ...cur, ...set };
        if (b.theme) n.theme = { ...cur.theme, ...b.theme };
        if (b.posts_per_day !== undefined) n.posts_per_day = Math.max(1, Math.min(40, Number(b.posts_per_day) || 8));
        if (typeof b.archived === 'boolean') n.archived = b.archived;
        if (b.playbook !== undefined) n.playbook = b.playbook;       // structured page guidance (JSON)
        return n;
      });
      if (!next) return bad(res, 'no such page', 404);
      await audit(u, 'page_changed', { page: id, fields: Object.keys(b) });
      return ok(res, { page: pagePublic(next) });
    }

    // the page's source sheet: categories with the names, outlets, accounts and subreddits the
    // page covers. Names in it rank stories higher (minus the ones excluded from the boost).
    // Encrypted with the page record; viewing it is audited; admins only.
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
        let url;
        const type = SOURCE_TYPES.includes(b.type) ? b.type : 'website';
        if (type === 'search') url = clean(b.url, 200);                 // a web search the collector runs
        else try { url = new URL(clean(b.url, 500)).href; } catch { return bad(res, 'enter a full URL (https://...)'); }
        if (!url) return bad(res, 'enter the search words');
        const src = { id: randomId(6), url, type, label: clean(b.label, 100), active: true, added_at: Date.now() };
        change = sources => [...sources, src];
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
      const type = subId, step = req.subpath[4];
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
          if (buf.length > MAX_DOC_BYTES) throw new UploadError('PDF too large for one request (max 3 MB): upload it from the admin page, which sends it in parts', 413, 'too_large');
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
