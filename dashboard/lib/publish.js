// What happens when a draft is approved.
//   PUBLISH_MODE=routine  (default) queue it and fire the SocialPilot publisher routine through
//                         its API trigger (ROUTINE_FIRE_URL + ROUTINE_FIRE_TOKEN). The routine
//                         publishes via the SocialPilot connector and reports back to /api/result.
//   PUBLISH_MODE=webhook  POST the post to PUBLISH_WEBHOOK_URL (e.g. Zapier -> SocialPilot)
//   PUBLISH_MODE=dry_run  mark it posted without sending anything
import { fire } from './fire.js';

// The approval itself is done when this runs: waking the publisher is only a nudge, and the :35
// backstop run publishes the draft anyway. So it never throws (a throw here once moved approved
// posts to Failed and told editors SocialPilot was refusing posts) and gives up after 8 s.
export async function fireRoutine(draftId) {
  if (!process.env.ROUTINE_FIRE_URL || !process.env.ROUTINE_FIRE_TOKEN)
    return { fired: false, note: 'ROUTINE_FIRE_URL / ROUTINE_FIRE_TOKEN not set: the backstop run will publish it' };
  const r = await fire('ROUTINE', `Publish approved draft ${draftId} (and any other approved drafts in the queue).`);
  return r.fired ? r : { ...r, note: 'the publisher could not be woken now: its next run (by :35 past the hour) publishes it' };
}

export async function publish(draft) {
  const mode = process.env.PUBLISH_MODE || 'routine';
  if (mode === 'routine') return { mode, queued: true, ...(await fireRoutine(draft.id)) };
  if (mode === 'dry_run') return { mode, ok: true, note: 'dry run: nothing was sent' };
  if (mode === 'webhook') {
    if (!process.env.PUBLISH_WEBHOOK_URL) throw new Error('PUBLISH_WEBHOOK_URL is not set');
    // the page's own account, linked by the onboarding routine (routes/publish.js read it from the live page)
    const r = await fetch(process.env.PUBLISH_WEBHOOK_URL, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ draft_id: draft.id, page: draft.page, page_name: draft.page_name, socialpilot_account: draft.socialpilot_login_id || '',
        caption: draft.caption_final || draft.caption, image_url: draft.image_url,
        image_urls: [draft.image_url, ...(draft.extra_images || [])] }) });
    const text = await r.text();
    if (!r.ok) throw new Error(`webhook ${r.status}: ${text.slice(0, 300)}`);
    return { mode, ok: true, response: text.slice(0, 500) };
  }
  throw new Error(`unknown PUBLISH_MODE ${mode}`);
}
