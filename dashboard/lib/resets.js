// One-time links for setting / resetting a password (invites and "forgot password").
// The link carries a random token; only its hash is stored, it expires after 48 hours, and it
// works once. Emails are sent when RESEND_API_KEY and MAIL_FROM are set; otherwise the admin
// copies the link from the admin panel.
import crypto from 'node:crypto';
import { updateRecord } from './db.js';

const HOURS = 48;
const sha = t => crypto.createHash('sha256').update(t).digest('hex');

export async function issueReset(user, origin) {
  const token = crypto.randomBytes(24).toString('base64url');
  await updateRecord('users', user.id, cur => cur && { ...cur, reset: { hash: sha(token), exp: Date.now() + HOURS * 3600e3 } });
  return `${origin}/#reset=${user.id}.${token}`;
}

export function resetValid(user, token) {
  const r = user && user.reset;
  if (!r || r.exp < Date.now()) return false;
  const a = Buffer.from(sha(token)), b = Buffer.from(r.hash);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

export async function sendMail(to, subject, text) {
  const key = process.env.RESEND_API_KEY, from = process.env.MAIL_FROM;
  if (!key || !from) return false;
  const r = await fetch('https://api.resend.com/emails', {
    method: 'POST', headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ from, to, subject, text }),
  });
  return r.ok;
}
