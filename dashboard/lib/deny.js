// SocialPilot accounts this product never uses, whatever a page record says (docs/PRODUCT.md "Safety
// rules"): the deny list of config/socialpilot.yaml (blocked_login_ids), bundled with the deployment
// (deploy.sh, vercel.json includeFiles) and read from the repository in a checkout, plus
// SOCIALPILOT_BLOCKED_IDS (comma separated) from the hosting provider's settings. A routine may send
// its own copy (`deny`) with a request: those are refused too, never fewer.
import { readFileSync } from 'node:fs';

const FILES = [new URL('../config/socialpilot.yaml', import.meta.url), new URL('../../config/socialpilot.yaml', import.meta.url)];
let fromFile = null;

// the ids of `blocked_login_ids: [1, 2]` (or the list form, one `- 1` per line)
export function parseDeny(text) {
  const m = String(text || '').match(/^blocked_login_ids:\s*(\[[^\]]*\]|\n(?:\s+-\s*.+\n?)*)/m);
  if (!m) return [];
  return (m[1].match(/\d{1,15}/g) || []).map(String);
}

function fileIds() {
  if (fromFile) return fromFile;
  fromFile = [];
  for (const f of process.env.SOCIALPILOT_DENY_FILE ? [process.env.SOCIALPILOT_DENY_FILE] : FILES) {
    try { fromFile = parseDeny(readFileSync(f, 'utf8')); break; } catch { /* not in this layout */ }
  }
  return fromFile;
}

export function denyList(sent = []) {
  const env = String(process.env.SOCIALPILOT_BLOCKED_IDS || '').split(',').map(s => s.trim()).filter(Boolean);
  const extra = (Array.isArray(sent) ? sent : []).map(x => String(x).trim()).filter(x => /^\d{1,15}$/.test(x));
  return [...new Set([...fileIds(), ...env, ...extra])];
}

export const isDenied = (id, sent = []) => !!id && denyList(sent).includes(String(id).trim());

// tests: read the file again (SOCIALPILOT_DENY_FILE changed)
export function resetDeny() { fromFile = null; }
