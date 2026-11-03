// /api/pages/*  a workspace's pages, for their owner (and the super): docs/PRODUCT.md "A page's life".
//   GET    pages                         the pages the user may see (editors: theirs, read only)
//   POST   pages                         {name, fb_page_url?, niche?, language?, timezone?} a new page in the
//                                        owner's workspace (the super: {workspace}), in `draft`
//   GET    pages/<slug>                  the page, its documents, budgets and onboarding
//   PATCH  pages/<slug>                  settings: {name, fb_page_url, niche, language, timezone, posts_per_day,
//                                        premium_per_day, playbook, socialpilot_connected (the tick)}
//   *      pages/<slug>/docs|sources|lessons|sheet/...   as Admin > Pages (routes/admin.js pageSection)
//   POST   pages/<slug>/go-live          the red button: checks, `going_live`, an onboard job, the
//                                        onboarding routine woken (ONBOARD_FIRE_URL / ONBOARD_FIRE_TOKEN)
//   POST   pages/<slug>/retry            Try again, after the onboarding failed (the same checks)
//   POST   pages/<slug>/pause | resume   live <-> paused (a paused page: no hourly runs, no publishing,
//                                        Studio read only)
//   GET    pages/<slug>/onboarding       the status and each step, for the live progress view
// A page of another workspace is "no such page" (404), whatever its id: nothing says it exists.
import { getRecord, updateRecord, createRecord, listRecords } from '../lib/db.js';
import { audit } from '../lib/audit.js';
import { fire } from '../lib/fire.js';
import { randomId } from '../lib/crypto.js';
import { ok, bad, clean } from '../lib/http.js';
import { isSuper, isOwner, mayUse, mayManage } from '../lib/tenancy.js';
import { pagePublic, statusOf, activeVersion, onboardingPublic, ONBOARD_STEPS, DOC_TYPES, budgetsOf } from '../lib/pages.js';
import { pageSection, createPage, changePage, MAX_UPLOAD_BYTES } from './admin.js';

// What Go live needs (docs/PRODUCT.md "Go live"), in the words the owner reads: [field, what to do]
export function goLiveMissing(p) {
  return [
    !p.fb_page_url && ['fb_page_url', 'add the page\'s Facebook address'],
    !clean(p.niche) && ['niche', 'describe the page\'s niche in a sentence'],
    !activeVersion(p, 'templates') && ['templates', 'upload the post templates document'],
    !activeVersion(p, 'tone') && ['tone', 'upload the caption language and tone document'],
    !p.socialpilot_connected && ['socialpilot_connected', 'connect the page in SocialPilot (the steps are on this page), then tick "I have connected this page in SocialPilot"'],
  ].filter(Boolean);
}

// A page someone may open here: theirs to see (mayUse) and, for any change, theirs to manage.
async function pageFor(u, slug, manage) {
  const p = slug && await getRecord('pages', clean(slug, 60));
  if (!p || !(manage ? mayManage(u, p) : mayUse(u, p))) return null;
  return p;
}

function onboardingOut(p, job) {
  const o = onboardingPublic(p);
  const seen = Object.fromEntries(o.steps.map(s => [s.step, s]));      // the newest report of each step
  return { page: p.slug, status: statusOf(p), ...o,
           expected: ONBOARD_STEPS.map(([step, label]) => ({ step, label, ...(seen[step] ? { ok: seen[step].ok, detail: seen[step].detail, at: seen[step].at } : {}) })),
           job: job ? { id: job.id, status: job.status, created_at: job.created_at, started_at: job.started_at || null,
                        fired: job.fired ?? null, done_at: job.done_at || null } : null };
}

// Go live and Try again: the checks, then `going_live` with a new onboard job, written in one step on
// the newest copy of the page (two presses at once make one job), then the routine is woken.
async function goLive(req, res, u, p, again) {
  const from = again ? ['failed'] : ['draft', 'failed'];
  if (!from.includes(statusOf(p)))
    return bad(res, statusOf(p) === 'going_live' ? 'this page is going live already: follow the steps below'
      : statusOf(p) === 'live' ? 'this page is live already' : statusOf(p) === 'paused' ? 'this page is paused: press Resume' : 'press Go live first', 409);
  const missing = goLiveMissing(p);
  if (missing.length) return res.status(400).json({ error: `Before going live: ${missing.map(m => m[1]).join('; ')}.`, missing: missing.map(m => m[0]) });
  const ws = p.workspace && await getRecord('workspaces', p.workspace);
  if (!ws) return bad(res, 'this page belongs to no workspace: ask the product owner', 409);
  const job = { id: `${p.slug}-${Date.now()}-${randomId(4).replace(/[^A-Za-z0-9]/g, 'x')}`, kind: 'onboard', page: p.slug, workspace: p.workspace,
                status: 'queued', created_at: Date.now(), by: u.email };
  let refused = '';
  const next = await updateRecord('pages', p.slug, cur => {
    refused = '';
    if (!cur) return null;
    if (!from.includes(statusOf(cur))) { refused = statusOf(cur); return null; }
    if (goLiveMissing(cur).length) { refused = 'changed'; return null; }
    return { ...cur, status: 'going_live', onboarding: { started_at: job.created_at, job: job.id, steps: [], error: '', by: u.email } };
  });
  if (!next) return bad(res, 'no such page', 404);
  if (refused) return bad(res, refused === 'going_live' ? 'this page is going live already: follow the steps below' : 'the page changed meanwhile: reload it and try again', 409);
  await createRecord('onboard', job.id, job);
  const f = await fire('ONBOARD', `Onboard page ${p.slug} (job ${job.id}): follow ONBOARD.md.`);
  // a session may have claimed the job while the routine was starting (never undo that)
  const saved = await updateRecord('onboard', job.id, cur => (cur && cur.status === 'queued' ? { ...cur, fired: !!f.fired, fire_status: f.status || null, fire_error: f.error || '' } : null));
  await audit(u, again ? 'page_retried' : 'page_go_live', { page: p.slug, workspace: p.workspace, job: job.id, fired: !!f.fired });
  return ok(res, { page: pagePublic(next, ws), onboarding: onboardingOut(next, saved || job), fired: !!f.fired });
}

async function pauseOrResume(res, u, p, pause) {
  const from = pause ? 'live' : 'paused';
  let was = '', theirs = false;
  const next = await updateRecord('pages', p.slug, cur => {
    was = statusOf(cur);
    // a page the product owner paused is theirs to resume
    theirs = !pause && !!(cur && cur.paused_by_super) && !isSuper(u);
    if (!cur || was !== from || theirs) return null;
    return pause ? { ...cur, status: 'paused', paused_at: Date.now(), paused_by: u.email, paused_by_super: isSuper(u) }
                 : { ...cur, status: 'live', resumed_at: Date.now(), resumed_by: u.email, paused_by: '', paused_by_super: false };
  });
  if (theirs) return bad(res, 'the product owner paused this page: ask them to resume it', 403);
  if (was !== from) return bad(res, pause ? (was === 'paused' ? 'this page is paused already' : 'only a live page can be paused')
                                          : (was === 'live' ? 'this page is live already' : 'only a paused page can be resumed'), 409);
  await audit(u, pause ? 'page_paused' : 'page_resumed', { page: p.slug, workspace: p.workspace });
  return ok(res, { page: pagePublic(next, p.workspace && await getRecord('workspaces', p.workspace)) });
}

export default async function handler(req, res) {
  const u = req.user;
  if (!u) return bad(res, 'sign in first', 401);
  if (u.must_change) return bad(res, 'set a new password first', 403);
  const [slug, sub, subId, step] = req.subpath;
  const b = req.body || {};

  if (!slug && req.method === 'GET') {
    const ws = Object.fromEntries((await listRecords('workspaces')).filter(w => isSuper(u) || w.id === u.workspace).map(w => [w.id, w]));
    const pages = (await listRecords('pages')).filter(p => mayUse(u, p) && (isSuper(u) || !p.archived));
    return ok(res, { pages: pages.map(p => (mayManage(u, p) ? pagePublic(p, ws[p.workspace])
                                            : { slug: p.slug, name: p.name, status: statusOf(p), theme: p.theme || {}, ...budgetsOf(p, ws[p.workspace]) })),
                     doc_types: DOC_TYPES, doc_max_bytes: MAX_UPLOAD_BYTES, steps: ONBOARD_STEPS.map(([step, label]) => ({ step, label })) });
  }
  if (!slug && req.method === 'POST') {
    if (!isSuper(u) && !isOwner(u)) return bad(res, 'only the workspace owner adds pages', 403);
    if (isOwner(u) && !u.workspace) return bad(res, 'your account has no workspace: ask the product owner', 403);
    // the super names the workspace, or the page is their own workspace's (the open-access owner's)
    const r = await createPage(u, b, isSuper(u) ? clean(b.workspace, 80) || u.workspace : u.workspace);
    return r.error ? bad(res, r.error, r.status) : ok(res, r);
  }

  const manage = !(req.method === 'GET' && (!sub || sub === 'onboarding'));
  const p = await pageFor(u, slug, manage);
  if (!p) return bad(res, 'no such page', 404);

  if (!sub && req.method === 'GET') {
    const ws = p.workspace && await getRecord('workspaces', p.workspace);
    if (!mayManage(u, p)) return ok(res, { page: { slug: p.slug, name: p.name, status: statusOf(p), theme: p.theme || {} } });
    return ok(res, { page: { ...pagePublic(p, ws), playbook: p.playbook || null }, missing: goLiveMissing(p).map(m => m[0]),
                     doc_types: DOC_TYPES, doc_max_bytes: MAX_UPLOAD_BYTES });
  }
  if (!sub && req.method === 'PATCH') {
    const r = await changePage(u, p, b);
    return r.error ? bad(res, r.error, r.status) : ok(res, r);
  }
  if (sub === 'onboarding' && req.method === 'GET') {
    const job = p.onboarding && p.onboarding.job ? await getRecord('onboard', p.onboarding.job).catch(() => null) : null;
    return ok(res, { onboarding: onboardingOut(p, job) });
  }
  if (req.method === 'POST' && (sub === 'go-live' || sub === 'retry')) return goLive(req, res, u, p, sub === 'retry');
  if (req.method === 'POST' && (sub === 'pause' || sub === 'resume')) return pauseOrResume(res, u, p, sub === 'pause');
  if (['docs', 'sources', 'lessons', 'sheet'].includes(sub)) return pageSection(req, res, u, p, sub, subId, step);
  return bad(res, 'not found', 404);
}
