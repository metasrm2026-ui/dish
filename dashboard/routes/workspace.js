// /api/workspace/*  a tester's own workspace (role owner; the super names one with ?workspace=):
//   GET   workspace                      the workspace, its pages and its people
//   POST  workspace/editors              {email, name, pages: [slug]} an editor's invite (a set-password link)
//   PATCH workspace/editors/<id>         {pages?, pages_add?, pages_remove?, active?, name?}
//   POST  workspace/editors/<id>/reset   a new set-password link
//   GET   workspace/audit?days=          the workspace's own audit entries
// Only the workspace's own pages can be given, and only its own editors changed: anyone else is
// "no such user" (404), whatever the id.
import { getRecord, updateRecord, listRecords, readLogs } from '../lib/db.js';
import { publicUser, allUsers } from '../lib/session.js';
import { issueReset, sendMail } from '../lib/resets.js';
import { audit } from '../lib/audit.js';
import { ok, bad, clean, siteOrigin } from '../lib/http.js';
import { isSuper, isOwner, roleOf, idPage } from '../lib/tenancy.js';
import { pagePublic } from '../lib/pages.js';
import { inviteUser, workspacePages } from './admin.js';

const userOut = x => ({ ...publicUser(x), last_login: x.last_login || null, locked: (x.locked_until || 0) > Date.now() });

// An audit entry of this workspace: one a person of it made, or one about one of its pages (a routine's)
export const auditOf = (e, wsId, slugs) => e.workspace === wsId || (e.page && slugs.has(e.page)) || (e.job && slugs.has(idPage(e.job)));

export default async function handler(req, res) {
  const u = req.user;
  if (!u) return bad(res, 'sign in first', 401);
  if (u.must_change) return bad(res, 'set a new password first', 403);
  if (!isOwner(u) && !isSuper(u)) return bad(res, 'only the workspace owner can open this', 403);
  const wsId = isSuper(u) ? clean(req.query.workspace || (req.body || {}).workspace, 80) || u.workspace : u.workspace;
  const ws = wsId && await getRecord('workspaces', wsId);
  if (!ws) return bad(res, 'no such workspace', 404);
  const [what, id, sub] = req.subpath;
  const b = req.body || {};

  if (!what && req.method === 'GET') {
    const [pages, users] = await Promise.all([listRecords('pages'), allUsers()]);
    return ok(res, { workspace: { id: ws.id, name: ws.name, limits: ws.limits || {}, owner: ws.owner_email || '' },
                     pages: pages.filter(p => p.workspace === ws.id && !p.archived).map(p => pagePublic(p, ws)),
                     users: users.filter(x => x.workspace === ws.id).map(userOut) });
  }

  if (what === 'audit' && req.method === 'GET') {
    const slugs = new Set((await listRecords('pages')).filter(p => p.workspace === ws.id).map(p => p.slug));
    await audit(u, 'audit_viewed', { workspace: ws.id });
    const entries = (await readLogs('audit', Math.min(Number(req.query.days || 7), 31))).filter(e => auditOf(e, ws.id, slugs)).slice(0, 500);
    return ok(res, { entries });
  }

  if (what === 'editors') {
    if (req.method === 'POST' && !id) {
      const w = await workspacePages(ws.id, b.pages);
      if (w.foreign.length) return bad(res, 'choose pages of your workspace');
      const r = await inviteUser(req, u, { email: b.email, name: b.name, role: 'editor', workspace: ws.id, pages: w.pages });
      return r.error ? bad(res, r.error, r.status) : ok(res, r);
    }
    const target = id && await getRecord('users', String(id));
    // an owner changes only the editors of their own workspace
    if (!target || target.workspace !== ws.id || roleOf(target) !== 'editor') return bad(res, 'no such user', 404);
    if (req.method === 'PATCH' && !sub) {
      const add = Array.isArray(b.pages_add) ? b.pages_add.map(String) : [], remove = Array.isArray(b.pages_remove) ? b.pages_remove.map(String) : [];
      const w = await workspacePages(ws.id, [...(Array.isArray(b.pages) ? b.pages : []), ...add]);
      if (w.foreign.length) return bad(res, 'choose pages of your workspace');
      const next = await updateRecord('users', target.id, cur => {
        if (!cur || cur.workspace !== ws.id) return null;
        const n = { ...cur };
        if (Array.isArray(b.pages)) n.pages = w.pages.filter(s => b.pages.map(String).includes(s));
        if (add.length || remove.length) n.pages = [...new Set([...(n.pages || []).filter(s => !remove.includes(s)), ...add])];
        if (typeof b.active === 'boolean') { n.active = b.active; if (!b.active) n.sessions_after = Date.now(); }
        if (b.name !== undefined) n.name = clean(b.name, 100);
        return n;
      });
      if (!next) return bad(res, 'no such user', 404);
      await audit(u, 'user_changed', { target: target.email, pages: next.pages, active: next.active });
      return ok(res, { user: userOut(next) });
    }
    if (req.method === 'POST' && sub === 'reset') {
      if (target.active === false) return bad(res, 'this editor is deactivated: activate them first, then send the link');
      const link = await issueReset(target, siteOrigin(req));
      const emailed = await sendMail(target.email, 'Reset your password', `Set a new password here (valid 48 hours, one use):\n${link}`);
      await audit(u, 'user_reset_link', { target: target.email });
      return ok(res, { setup_link: link, emailed });
    }
  }
  return bad(res, 'not found', 404);
}
