// A page's record (pages/<slug>) as docs/PRODUCT.md "A page's life" has it, shared by the super's
// admin panel (routes/admin.js) and the owners' pages (routes/pages.js):
//   workspace, status (draft | going_live | live | paused | failed), name, fb_page_url, niche, language,
//   timezone, posts_per_day, premium_per_day, limits {posts_per_day, premium_per_day} (the super's),
//   socialpilot_connected {at, by} (the owner's tick), socialpilot_login_id (set only by the onboarding
//   routine), sources, playbook, docs, onboarding {started_at, job, steps: [{step, ok, detail, at}], error,
//   done_at}
import { getRecord } from './db.js';
import { isDenied } from './deny.js';
import { clean, slugify } from './http.js';

export const STATUSES = ['draft', 'going_live', 'live', 'paused', 'failed'];
export const DOC_TYPES = { templates: 'Post templates', visual: 'Visual references', tone: 'Caption language and tone' };
// docs/PRODUCT.md "Budgets": a page's defaults; the most the super can allow
export const BUDGET_DEFAULTS = { posts_per_day: 10, premium_per_day: 6 };
export const BUDGET_MAX = { posts_per_day: 40, premium_per_day: 40 };
const BUDGET_MIN = { posts_per_day: 1, premium_per_day: 0 };
export const BUDGETS = Object.keys(BUDGET_DEFAULTS);

// the onboarding routine's steps (ONBOARD.md), in order, for the live progress view
export const ONBOARD_STEPS = [
  ['link_account', 'Link the page\'s SocialPilot account'],
  ['read_documents', 'Read the documents and the page\'s recent posts'],
  ['research_sources', 'Find sources for the page\'s niche'],
  ['write_playbook', 'Write the page\'s playbook'],
  ['queue_templates', 'Prepare the post templates for approval'],
  ['first_run', 'Make the first posts'],
];

export const statusOf = p => (p && STATUSES.includes(p.status) ? p.status : 'draft');
export const isLive = p => statusOf(p) === 'live';

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
export function zoneOf(v) {           // the name to store, or null when it is not a time zone
  if (v === '' || v === 'UTC') return v;
  if (!new RegExp(ZONE.source, 'i').test(v)) return null;
  let z;
  try { z = new Intl.DateTimeFormat('en-US', { timeZone: v }).resolvedOptions().timeZone; } catch { return null; }
  z = ZONE_NEW[z] || z;
  return ZONE.test(z) ? z : null;
}
export const ZONE_BAD = 'the time zone must be a name like America/New_York or Europe/London (or UTC): pick one from the list';

// The page's Facebook address: https on facebook.com (or fb.com), stored as the full link. '' when
// none was given; null when it is not one.
export function fbUrlOf(v) {
  v = clean(v, 300);
  if (!v) return '';
  let u;
  try { u = new URL(/^https?:\/\//i.test(v) ? v : `https://${v}`); } catch { return null; }
  if (!/^(www\.|m\.|web\.)?(facebook\.com|fb\.com)$/i.test(u.hostname) || u.pathname.length < 2) return null;
  u.protocol = 'https:';
  u.hash = '';
  return u.href;
}
export const FB_BAD = 'paste the page\'s Facebook address, like https://www.facebook.com/yourpage';

// A budget as sent: a whole number in range, or an error. undefined when not sent.
export function budgetIn(k, v) {
  if (v === undefined) return { value: undefined };
  const n = Number(v);
  if (!Number.isInteger(n) || n < BUDGET_MIN[k] || n > BUDGET_MAX[k])
    return { error: `${k === 'posts_per_day' ? 'posts a day' : 'premium cards a day'} must be a whole number from ${BUDGET_MIN[k]} to ${BUDGET_MAX[k]}` };
  return { value: n };
}

// The workspace's ceilings (the super's), each page's own ceiling (the super's) and the page's value
// (an owner may lower it): what the routines work with is the lowest of the three.
export function limitsOf(p, ws) {
  const out = {};
  for (const k of BUDGETS) out[k] = Math.min(Number((p && p.limits || {})[k] ?? BUDGET_MAX[k]), Number((ws && ws.limits || {})[k] ?? BUDGET_MAX[k]));
  return out;
}
export function budgetsOf(p, ws) {
  const lim = limitsOf(p, ws), out = {};
  for (const k of BUDGETS) out[k] = Math.min(Number((p && p[k]) ?? BUDGET_DEFAULTS[k]), lim[k]);
  return out;
}

// what an owner or the super sees of a document version (never its storage paths). chars is null
// for versions uploaded before it was measured.
const LOW_TEXT_PER_PAGE = 30;
export const imageOnly = (chars, pages) => chars < LOW_TEXT_PER_PAGE * Math.max(1, pages || 0);
export const versionPublic = ({ v, filename, size, pages, chars, warning, at, by }) => ({ v, filename, size, pages,
  chars: chars ?? null, image_only: chars == null ? null : imageOnly(chars, pages), warning: warning || '', at, by });
export const activeVersion = (p, type) => { const d = ((p && p.docs) || {})[type]; return d && (d.versions || []).find(x => x.v === d.active) || null; };

export function onboardingPublic(p) {
  const o = (p && p.onboarding) || {};
  return { started_at: o.started_at || null, done_at: o.done_at || null, error: o.error || '', job: o.job || null,
           steps: (o.steps || []).map(s => ({ step: s.step, ok: s.ok !== false, detail: s.detail || '', at: s.at })) };
}

export function pagePublic(p, ws = null) {
  const docs = {};
  for (const t of Object.keys(DOC_TYPES)) {
    const d = (p.docs || {})[t] || {};
    docs[t] = { active: d.active || null, versions: (d.versions || []).map(versionPublic) };
  }
  return { slug: p.slug, name: p.name, workspace: p.workspace || null, status: statusOf(p), fb_page_url: p.fb_page_url || '',
           fb_page_id: p.fb_page_id || '', niche: p.niche || '', language: p.language || '', timezone: p.timezone || 'America/New_York',
           archived: !!p.archived, theme: p.theme || {}, ...budgetsOf(p, ws), limits: limitsOf(p, ws),
           socialpilot_connected: !!p.socialpilot_connected, socialpilot_linked: !!p.socialpilot_login_id,
           socialpilot_login_id: p.socialpilot_login_id || '', source_count: (p.sources || []).length, docs,
           onboarding: onboardingPublic(p), paused_by: statusOf(p) === 'paused' ? p.paused_by || '' : '' };
}

// The slug of a new page: unique across the product (another workspace's page of the same name gets
// -2, -3 ...), and never 8 digits in a row (lib/tenancy.js idPage reads the page out of ids by them).
export async function freeSlug(name, wanted = '') {
  const base = slugify(wanted || name).replace(/\d{8,}/g, m => m.slice(0, 7)).replace(/-+$/, '');
  if (!base) return '';
  for (let n = 1; n < 50; n++) {
    const slug = n === 1 ? base : `${base.slice(0, 56)}-${n}`;
    if (!await getRecord('pages', slug)) return slug;
  }
  return '';
}

// A new page in a workspace, in `draft`. Its budgets start at the workspace's (the super's).
export function newPage(slug, ws, b, by) {
  const lim = limitsOf(null, ws);
  return { slug, name: clean(b.name, 100), workspace: ws ? ws.id : null, status: 'draft', fb_page_url: b.fb_page_url || '',
           fb_page_id: clean(b.fb_page_id, 60), niche: clean(b.niche, 400), language: clean(b.language, 40) || 'English',
           timezone: b.timezone || 'America/New_York', theme: b.theme || {},
           posts_per_day: Math.min(BUDGET_DEFAULTS.posts_per_day, lim.posts_per_day), premium_per_day: Math.min(BUDGET_DEFAULTS.premium_per_day, lim.premium_per_day),
           limits: lim, socialpilot_login_id: '', socialpilot_connected: null, sources: [], playbook: null, docs: {},
           onboarding: null, archived: false, created_at: Date.now(), created_by: by };
}

// Change a page's budgets. super: sets the page's ceiling too (budgets above it raise it); an owner:
// only up to the ceilings, never above. Returns the error to show, or ''.
export function budgetChange(u, b, isSuperUser) {
  const set = {}, lim = {};
  for (const k of BUDGETS) {
    const r = budgetIn(k, b[k]);
    if (r.error) return { error: r.error };
    if (r.value !== undefined) set[k] = r.value;
    const l = budgetIn(k, (b.limits || {})[k]);
    if (l.error) return { error: `the most ${l.error}` };
    if (l.value !== undefined) {
      if (!isSuperUser) return { error: 'only the product owner sets the most a page may use', status: 403 };
      lim[k] = l.value;
    }
  }
  return { set, lim };
}

export function applyBudgets(cur, ws, { set, lim }, isSuperUser) {
  const n = { ...cur, limits: { ...limitsOf(cur, null), ...lim } };
  for (const k of BUDGETS) {
    if (set[k] === undefined) { n[k] = Math.min(Number(cur[k] ?? BUDGET_DEFAULTS[k]), n.limits[k]); continue; }
    if (isSuperUser) { n[k] = set[k]; if (set[k] > n.limits[k] && lim[k] === undefined) n.limits[k] = set[k]; }
    else {
      const most = limitsOf(n, ws)[k];
      if (set[k] > most) return { error: `${k === 'posts_per_day' ? 'posts a day' : 'premium cards a day'} can be at most ${most} for this page (set by the product owner)` };
      n[k] = set[k];
    }
  }
  return { page: n };
}

export async function getPage(slug) {
  return slug ? getRecord('pages', clean(slug, 60)) : null;
}

// Why a page's posts cannot go to SocialPilot now ('' when they can): docs/PRODUCT.md "Safety rules".
// Only a live page, with the account the onboarding routine linked, never one on the deny list.
export function notPublishable(p) {
  if (!p) return 'the page no longer exists';
  if (statusOf(p) === 'paused') return 'the page is paused: its posts go out once it is resumed';
  if (statusOf(p) !== 'live') return 'the page is not live yet: its posts go out once it is';
  if (!p.socialpilot_login_id) return 'the page has no SocialPilot account linked: press Go live again';
  if (isDenied(p.socialpilot_login_id)) return 'the page\'s SocialPilot account is on the deny list: it is never used';
  return '';
}
