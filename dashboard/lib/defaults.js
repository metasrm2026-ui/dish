// This deployment's settings when the hosting provider's settings (Vercel project env) have none.
// A setting there always wins: the owner changes one there, never here.
//   OPEN_ACCESS  "1": the dashboard opens straight in as the product owner, with no sign-in (the
//                owner's instruction for now: "nothing to sign up, no password: click the link and
//                view"). Set OPEN_ACCESS=0 in the project's settings to turn it off (lib/session.js;
//                docs/DASHBOARD_API.md "Open access").
export const DEFAULTS = { OPEN_ACCESS: '1' };

export const setting = name => process.env[name] ?? DEFAULTS[name];
