// Start a routine through its API trigger.
//   STUDIO: the Studio jobs routine (STUDIO_FIRE_URL / STUDIO_FIRE_TOKEN). Never the publisher:
//           publishing stays in its own routine. Without a token, jobs wait for the next run.
//   ROUTINE: the SocialPilot publisher (ROUTINE_FIRE_URL / ROUTINE_FIRE_TOKEN).
//   ONBOARD: the onboarding routine, when an owner presses Go live (ONBOARD_FIRE_URL / ONBOARD_FIRE_TOKEN).
// Never throws, and gives up after FIRE_TIMEOUT_MS: waking a routine is a nudge (the hourly runs
// pick the work up anyway), so it must never fail or hold up what the editor just did.
const FIRE_TIMEOUT_MS = 8000;

export async function fire(which, text) {
  const url = process.env[`${which}_FIRE_URL`], token = process.env[`${which}_FIRE_TOKEN`];
  if (!url || !token) return { fired: false };
  try {
    const r = await fetch(url, { method: 'POST', signal: AbortSignal.timeout(FIRE_TIMEOUT_MS),
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, 'anthropic-version': '2023-06-01' },
      body: JSON.stringify({ text }) });
    return { fired: r.ok, status: r.status, ...(r.ok ? {} : { response: (await r.text().catch(() => '')).slice(0, 300) }) };
  } catch (e) {
    return { fired: false, error: String((e && e.message) || e) };
  }
}
