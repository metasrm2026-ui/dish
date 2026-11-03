// Audit log (section 6): who did what to protected assets, and when. Usage log (section 7):
// every AI, search and SocialPilot call with page, user and units, for the Phase 2 cost view.
import { appendLog } from './db.js';

// a person's entry carries their workspace, so an owner's audit view lists their own workspace's
// entries (routes/admin.js); a routine's names the page it was about
export async function audit(actor, action, details = {}) {
  await appendLog('audit', { actor: actor ? (actor.email || actor) : 'anonymous', action,
                             ...(actor && actor.workspace ? { workspace: actor.workspace } : {}), ...details });
}

export async function usage(entry) {
  // {tool, page, user, units, unit, cost_usd, detail}
  await appendLog('usage', entry);
}
