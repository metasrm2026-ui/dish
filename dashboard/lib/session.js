// Login, sessions and roles (section 4.1).
//   Users are encrypted records (db/users). Passwords: scrypt with a per-user salt.
//   Session: HttpOnly cookie signed with SESSION_SECRET, expiring after SESSION_IDLE_MINUTES
//   without activity (default 120) and after 12 hours in any case.
//   Failed logins: 5 in a row lock the account for 15 minutes.
//   OPEN_ACCESS=1 (the owner's choice for now: "nothing to sign up, no password: click the link and
//   view"): a request without a session is the product owner "Owner" (openUser). docs/DASHBOARD_API.md, "Open access".
import crypto from 'node:crypto';
import { getRecord, updateRecord, listRecords } from './db.js';
import { roleOf, mayUse } from './tenancy.js';
import { isRoutine } from './auth.js';
import { audit } from './audit.js';
import { setting } from './defaults.js';

const IDLE_MIN = Number(process.env.SESSION_IDLE_MINUTES || 120);
const MAX_AGE_H = 12;
const MAX_FAILS = 5, LOCK_MIN = 15;
const COOKIE = 'bore_s';

export const userId = email => crypto.createHash('sha256').update(String(email).trim().toLowerCase()).digest('hex').slice(0, 24);

export function hashPassword(pw, salt = crypto.randomBytes(16).toString('base64')) {
  return { salt, hash: crypto.scryptSync(String(pw), salt, 32).toString('base64') };
}

function checkPassword(pw, rec) {
  if (!rec) { crypto.scryptSync(String(pw), 'dummy-salt', 32); return false; }   // same time either way
  const h = crypto.scryptSync(String(pw), rec.salt, 32);
  return crypto.timingSafeEqual(h, Buffer.from(rec.hash, 'base64'));
}

export function passwordProblem(pw) {
  pw = String(pw || '');
  if (pw.length < 10) return 'use at least 10 characters';
  if (!/[a-z]/i.test(pw) || !/\d/.test(pw)) return 'use letters and at least one number';
  return '';
}

function secret() {
  const s = process.env.SESSION_SECRET;
  if (!s || s.length < 32) throw new Error('SESSION_SECRET is not set');
  return s;
}
const sign = v => crypto.createHmac('sha256', secret()).update(v).digest('base64url');

function cookieFor(u, iat) {
  const payload = Buffer.from(JSON.stringify({ u, iat, last: Date.now() })).toString('base64url');
  return `${COOKIE}=${payload}.${sign(payload)}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${IDLE_MIN * 60}`;
}

export const clearCookie = () => `${COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0`;

function readCookie(req) {
  const raw = (req.headers.cookie || '').split(/;\s*/).find(c => c.startsWith(COOKIE + '='));
  if (!raw) return null;
  const [payload, sig] = raw.slice(COOKIE.length + 1).split('.');
  if (!payload || !sig) return null;
  const good = sign(payload);
  if (good.length !== sig.length || !crypto.timingSafeEqual(Buffer.from(good), Buffer.from(sig))) return null;
  try { return JSON.parse(Buffer.from(payload, 'base64url').toString()); } catch { return null; }
}

export function publicUser(u) {
  return u && { id: u.id, email: u.email, name: u.name || '', role: roleOf(u), workspace: u.workspace || null, pages: u.pages || [],
                active: u.active !== false, must_change: !!u.must_change, ...(u.open_access ? { open_access: true } : {}) };
}

// The signed-in user, or null. Refreshes the idle timer (sets a new cookie on `res`). With open
// access, a request without a (valid) session is the open-access owner instead.
export async function currentUser(req, res) {
  if (req._user !== undefined) return req._user;
  req._user = null;
  const u = await sessionUser(req, res) || (openAccess() && !isRoutine(req) ? await openUser() : null);
  req._user = u || null;
  return req._user;
}

async function sessionUser(req, res) {
  const c = readCookie(req);
  if (!c) return null;
  const now = Date.now();
  if (now - c.last > IDLE_MIN * 60e3 || now - c.iat > MAX_AGE_H * 3600e3) return null;
  const u = await getRecord('users', c.u);
  if (!u || u.active === false) return null;
  if (u.sessions_after && c.iat < u.sessions_after) return null;   // signed out everywhere / password changed
  if (res && now - c.last > 60e3) res.setHeader('Set-Cookie', cookieFor(u.id, c.iat));
  return u;
}

// Open access: on while OPEN_ACCESS is "1" (lib/defaults.js sets it for this deployment; the owner turns
// it off with OPEN_ACCESS=0 in the hosting provider's settings). The open-access owner is a super user named Owner with a
// workspace of its own, made on first use: workspaces, roles and every safety rule apply to it as to
// anyone. Signing in still works (a tester's session is the tester's), a routine's request is never a
// person's (it carries ROUTINE_SECRET), and a deactivated Owner opens nothing.
export const openAccess = () => String(setting('OPEN_ACCESS')).trim() === '1';
export const OPEN_EMAIL = () => String(process.env.OPEN_ACCESS_EMAIL || 'owner@studio.invalid').trim().toLowerCase();
export const OPEN_WORKSPACE = 'studio-owner';                // never a tester's (routes/admin.js freeWorkspaceId)

export async function openUser() {
  const email = OPEN_EMAIL(), id = userId(email);
  const have = await getRecord('users', id);
  if (have) return have.active === false ? null : have;
  await updateRecord('workspaces', OPEN_WORKSPACE, cur => (cur ? null : { id: OPEN_WORKSPACE, name: 'Owner', limits: {}, owner_id: id,
                                                                          owner_email: email, created_at: Date.now(), created_by: 'open access' }));
  // on the newest copy: two first requests at once make one Owner
  let made = false;
  const u = await updateRecord('users', id, cur => {
    made = !cur;
    return cur ? null : { id, email, name: 'Owner', role: 'super', workspace: OPEN_WORKSPACE, pages: [], active: true, must_change: false,
                          open_access: true, created_at: Date.now(), pw: hashPassword(crypto.randomBytes(24).toString('base64')) };
  });
  if (made) await audit('open access', 'open_owner_created', { target: email, workspace: OPEN_WORKSPACE });
  return u.active === false ? null : u;
}

export async function login(email, password, res) {
  const id = userId(email);
  const u = await getRecord('users', id);
  const now = Date.now();
  if (u && u.locked_until && u.locked_until > now) {
    return { error: `too many attempts: try again in ${Math.ceil((u.locked_until - now) / 60e3)} min`, status: 429 };
  }
  const ok = checkPassword(password, u && u.pw);
  // the right password on a deactivated account: said as it is, and not counted as a wrong try
  if (ok && u.active === false) return { error: 'this account is deactivated: ask your admin to activate it', status: 403 };
  if (!ok) {
    if (u) {
      // on the newest copy (an admin's change meanwhile is kept); a lock that has ended starts the count again
      let fails = 0;
      await updateRecord('users', id, cur => {
        if (!cur) return null;
        fails = (cur.locked_until && cur.locked_until <= now ? 0 : (cur.failed || 0)) + 1;
        return { ...cur, failed: fails, locked_until: fails >= MAX_FAILS ? now + LOCK_MIN * 60e3 : 0 };
      });
      if (fails >= MAX_FAILS) return { error: `wrong email or password: too many attempts, try again in ${LOCK_MIN} min`, status: 429 };
    }
    return { error: 'wrong email or password', status: 401 };
  }
  // on the newest copy: a deactivation or role change made while this sign-in ran is kept
  await updateRecord('users', id, cur => cur && { ...cur, failed: 0, locked_until: 0, last_login: now, reset_requested: null });
  res.setHeader('Set-Cookie', cookieFor(id, now));
  return { user: u };
}

// page: its record when the caller has read it already
export async function canUsePage(u, slug, page) {
  if (!u || !slug) return false;
  return mayUse(u, page && page.slug === slug ? page : await getRecord('pages', slug));
}

export async function allUsers() {
  return (await listRecords('users')).sort((a, b) => (a.email < b.email ? -1 : 1));
}
