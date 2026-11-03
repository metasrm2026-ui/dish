// Workspaces and roles (docs/PRODUCT.md "Roles"). A workspace is one tester's account: its pages and
// its editors (db/workspaces/<id>). Users carry `workspace` and `role`:
//   super   the product owner: every workspace (workspace: null)
//   owner   a tester: their workspace's pages, documents, editors and settings
//   editor  invited by an owner: the pages of their workspace they were given (user.pages)
// Every list, count and direct id is filtered here on the server: a page belongs to exactly one
// workspace (page.workspace), and anything else (a draft, a job, a batch, a link, a note) belongs to
// the page its id or record names.
import { listRecords, getRecord } from './db.js';

// 'admin' is what users created before roles had these names were given: the product owner
export const roleOf = u => (u ? (u.role === 'admin' ? 'super' : u.role || 'editor') : '');
export const isSuper = u => roleOf(u) === 'super';
export const isOwner = u => roleOf(u) === 'owner';

// May this user work on this page (read it, review its posts, use Studio)?
export function mayUse(u, p) {
  if (!u || !p) return false;
  if (isSuper(u)) return true;
  if (!u.workspace || p.workspace !== u.workspace) return false;
  return isOwner(u) || (u.pages || []).includes(p.slug);
}

// May this user manage this page (settings, documents, go live, pause, editors)?
export const mayManage = (u, p) => !!u && !!p && (isSuper(u) || (isOwner(u) && !!u.workspace && p.workspace === u.workspace));

// Why this user may not use the page, as an answer: another workspace's page is no page at all (404:
// its id says nothing), one of their own workspace's they were not given is said so (403).
export const refusal = (u, p) => (p && u && u.workspace && p.workspace === u.workspace
  ? { status: 403, error: 'you do not have access to this page' } : { status: 404, error: 'no such page' });

// The pages a request may see: null for everything (a routine, the super), else the set of slugs.
// Kept on the request: one listing per request, however many checks it makes.
export async function pageScope(req) {
  if (!req.user || isSuper(req.user)) return null;
  if (!req._scope) req._scope = new Set((await listRecords('pages')).filter(p => mayUse(req.user, p)).map(p => p.slug));
  return req._scope;
}
export const inScope = (scope, slug) => !scope || scope.has(slug);

// The page a page-owned id names: drafts <page>-<YYYYMMDDHHMM>-<slug>, jobs, batches and links
// <page>-<ms>-<rand>. A page's slug never holds 8 digits in a row (lib/pages.js), so the first such
// run ends it.
export function idPage(id) {
  const m = String(id || '').match(/^(.*?)-\d{12,13}-/);
  return m ? m[1] : '';
}

export async function getWorkspace(id) {
  return id ? getRecord('workspaces', id) : null;
}
