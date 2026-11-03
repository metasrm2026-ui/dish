// /api/auth/*  login, logout, who am I, change password, reset with a one-time link, forgot.
import { login, clearCookie, publicUser, userId, hashPassword, passwordProblem, openAccess } from '../lib/session.js';
import { getRecord, updateRecord, listRecords } from '../lib/db.js';
import { issueReset, resetValid, sendMail } from '../lib/resets.js';
import { audit } from '../lib/audit.js';
import { ok, bad, clean, siteOrigin } from '../lib/http.js';
import { mayUse, mayManage } from '../lib/tenancy.js';
import { statusOf, budgetsOf } from '../lib/pages.js';

// the pages this person may use (lib/tenancy.js): their workspace's, an editor's own; the super's: all
// posts_per_day: the page's budget (lib/pages.js budgetsOf), what the approvals page counts a day against
async function myPages(u) {
  const [pages, ws] = await Promise.all([listRecords('pages'), listRecords('workspaces')]);
  const wsOf = Object.fromEntries(ws.map(w => [w.id, w]));
  return pages.filter(p => !p.archived && mayUse(u, p)).map(p => ({ slug: p.slug, name: p.name, theme: p.theme || {}, status: statusOf(p),
    workspace: p.workspace || null, can_manage: mayManage(u, p), posts_per_day: budgetsOf(p, wsOf[p.workspace]).posts_per_day }));
}

export default async function handler(req, res) {
  const [op] = req.subpath;
  const b = req.body || {};
  if (op === 'login' && req.method === 'POST') {
    const r = await login(clean(b.email, 200), String(b.password || ''), res);
    await audit(clean(b.email, 200), r.error ? 'login_failed' : 'login');
    if (r.error) return bad(res, r.error, r.status);
    return ok(res, { user: publicUser(r.user) });
  }
  if (op === 'logout') {
    res.setHeader('Set-Cookie', clearCookie());
    return ok(res);
  }
  if (op === 'me') {
    if (!req.user) return bad(res, 'sign in first', 401);
    const ws = req.user.workspace ? await getRecord('workspaces', req.user.workspace) : null;
    // open_access: nobody signs in here now (lib/session.js): the screens show no sign-in or sign-out
    return ok(res, { user: publicUser(req.user), pages: await myPages(req.user),
                     workspace: ws ? { id: ws.id, name: ws.name, limits: ws.limits || {} } : null, open_access: openAccess() });
  }
  if (op === 'password' && req.method === 'POST') {
    const u = req.user;
    if (!u) return bad(res, 'sign in first', 401);
    const r = await login(u.email, String(b.current || ''), res);
    // a lock is said as it is (the right password cannot help until it ends); the count stays, so a
    // stolen session cannot guess the password without limit
    if (r.error) return r.status === 429 ? bad(res, r.error.replace(/^wrong email or password/, 'current password is wrong'), 429)
                                         : bad(res, 'current password is wrong', 403);
    const problem = passwordProblem(b.password);
    if (problem) return bad(res, problem);
    await updateRecord('users', u.id, cur => cur && { ...cur, failed: 0, pw: hashPassword(b.password), must_change: false, sessions_after: Date.now() });
    await login(u.email, String(b.password), res);   // fresh cookie
    await audit(u, 'password_changed');
    return ok(res);
  }
  if (op === 'reset' && req.method === 'POST') {
    const [id, token] = String(b.token || '').split('.');
    const u = id && await getRecord('users', id);
    // (expired: the screen offers a new link and Sign in instead of the same form again)
    if (!u || !resetValid(u, token || '')) return res.status(400).json({ error: 'this link has expired or was already used', expired: true });
    if (u.active === false) return bad(res, 'this account is deactivated: ask your admin to activate it', 403);
    const problem = passwordProblem(b.password);
    if (problem) return bad(res, problem);
    await updateRecord('users', u.id, cur => cur && { ...cur, pw: hashPassword(b.password), reset: null, must_change: false, failed: 0,
                                                      locked_until: 0, sessions_after: Date.now(), reset_requested: null });
    await audit(u, 'password_reset');
    const r = await login(u.email, String(b.password), res);
    return ok(res, { user: publicUser(r.user) });
  }
  if (op === 'forgot' && req.method === 'POST') {
    // Same answer whether or not the address exists.
    const u = await getRecord('users', userId(clean(b.email, 200)));
    if (u && u.active !== false) {
      const link = await issueReset(u, siteOrigin(req));
      const sent = await sendMail(u.email, 'Reset your password', `Set a new password here (valid 48 hours, one use):\n${link}`);
      await updateRecord('users', u.id, cur => cur && { ...cur, reset_requested: Date.now(), reset_emailed: sent });
      await audit(u, 'password_reset_requested', { emailed: sent });
    }
    return ok(res, { message: 'If that address has an account, a reset link was emailed or your admin can send you one.' });
  }
  return bad(res, 'not found', 404);
}
