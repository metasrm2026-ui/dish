// Post Studio: login, page selection, the 8-step workflow (topics -> ideas -> visual -> caption
// -> check -> post) and the admin panel. Plain JS, hash routes, server state only (no secrets,
// prompts or reference documents ever reach the browser).
'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const S = { me: null, pages: [], page: null, batch: null, poll: null, feed: null, picked: new Map(), hours: 12,
            unsent: new Map(), saving: Promise.resolve(), nav: 0, health: null, healthAt: 0, failed: new Map(), retries: new Map(), asked: 0, applied: 0,
            bases: new Map(), clash: new Map(), clashBy: new Map(), writing: new Map(), lostSend: new Map() };
try { S.page = localStorage.getItem('studioPage'); S.hours = Number(localStorage.getItem('studioHours')) || 12; } catch (e) {}

// shown long enough to read (a long message, e.g. an error with what to do, stays longer); a tap closes it,
// and so does a tap anywhere else, which still reaches what it was meant for
function toast(msg) { const t = $('#toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(t._h);
  if (!t._away) { t._away = true; document.addEventListener('pointerdown', e => { if (!t.contains(e.target)) t.classList.remove('show'); }, true); }
  t.onclick = () => t.classList.remove('show'); t._h = setTimeout(() => t.classList.remove('show'), Math.max(3000, 70 * String(msg).length)); }
function zoom(u) { $('#zoomimg').src = u; $('#zoom').showModal(); }
const ago = ms => { if (!ms) return ''; const m = Math.round((Date.now() - ms) / 60000); if (m < 1) return 'just now'; if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60); return h < 48 ? `${h}h ago` : `${Math.floor(h / 24)} days ago`; };
const when = ms => ms ? new Date(ms).toLocaleString([], { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '';

async function api(path, opts = {}) {
  let r;
  try {
    r = await fetch(path, { credentials: 'same-origin', ...opts,
      headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined });
  } catch (e) {                                                      // no answer at all (no status): the browser's own text is not shown
    throw Object.assign(new Error('No connection: check the internet connection, then try again'), { offline: true });
  }
  const j = await r.json().catch(() => ({}));
  if (r.status === 401 && !path.startsWith('/api/auth/')) {
    keepDrafts();
    S.me = null;
    const h = location.hash.replace(/^#/, '');      // the screen to come back to once signed in again
    // (a second refusal while the sign-in screen is up, e.g. a field's save as it goes, keeps its next=)
    // the reason is also what is thrown: a button's own toast of it (Send, Skip check) says the same
    const why = `Signed out after a break: sign in to carry on${unsaved() ? ' (your text is kept)' : ''}`;
    if (!h.startsWith('/login')) {
      go(h ? `/login?next=${encodeURIComponent(h)}&why=idle` : '/login?why=idle');
      toast(why);
    }
    throw Object.assign(new Error(why), { status: 401 });
  }
  if (r.status === 403 && /^\/api\/(studio|admin)\//.test(path) && S.me) {
    const lost = await accessLost();
    if (lost) throw Object.assign(new Error(lost), { body: j, status: 403 });
  }
  // (storage's status code is left out of its words: "storage is not answering (503): ...")
  // the platform's own page, not ours (e.g. a request cut off after 60 s): what was asked may have been done (lost)
  if (!r.ok && !j.error && r.status >= 500) throw Object.assign(new Error(opts.method && opts.method !== 'GET'
    ? 'The dashboard did not answer in time: it may have gone through, so look again before trying again' : 'The dashboard did not answer in time: try again in a minute'),
    { body: j, status: r.status, lost: true });
  if (!r.ok) throw Object.assign(new Error(j.error ? String(j.error).replace(/ \((?:\d{3}|no connection)\)(?=:)/, '') : `HTTP ${r.status}`), { body: j, status: r.status });   // (body: e.g. what to confirm first)
  return j;
}
const go = p => { if (location.hash !== '#' + p) location.hash = p; else route(); };
// What the admin changed by hand on the screen (an input or change event marks it): admin forms send
// only those fields, so a value another admin saved meanwhile is never sent back from a stale form
function markDirty(e) { const el = e.target; if (el.closest && el.closest('#main') && el.type !== 'file') el.dataset.dirty = '1'; }
document.addEventListener('input', markDirty, true);
document.addEventListener('change', markDirty, true);
// Signed out (idle too long) on an admin screen: what was changed and not saved is kept for this tab
// and put back once they are signed in again on the same screen, to be saved
const draftKey = el => { const tr = el.closest('tr'), host = el.parentElement.closest('[id]');
  return [host && host.id, tr ? Object.values(tr.dataset).join(',') : '', el.name || el.id || el.dataset.ov || el.dataset.k || '', el.type === 'checkbox' ? el.value : ''].join('|'); };
function keepDrafts() {
  if (!S.me || !location.hash.startsWith('#/admin/')) return;
  const d = [...document.querySelectorAll('#main [data-dirty]')].filter(el => !el.matches('[data-active]'))   // (a source switched on or off is sent at once)
    .map(el => [draftKey(el), el.type === 'checkbox' ? el.checked : el.value]);
  if (d.length) try { sessionStorage.setItem('drafts', JSON.stringify({ hash: location.hash, at: Date.now(), user: S.me.user.id, d })); } catch (e) {}
}
function restoreDrafts() {
  let saved = null;
  try { saved = JSON.parse(sessionStorage.getItem('drafts')); } catch (e) {}
  if (!saved || saved.hash !== location.hash) return;
  try { sessionStorage.removeItem('drafts'); } catch (e) {}
  if (Date.now() - saved.at > 6 * 3600e3 || !S.me || saved.user !== S.me.user.id) return;   // (another admin signed in here: not theirs)
  const els = [...document.querySelectorAll('#main input, #main textarea, #main select')];
  let n = 0;
  for (const [k, v] of saved.d) {
    const el = els.find(x => draftKey(x) === k);
    if (!el) continue;
    if (el.type === 'checkbox') el.checked = v; else el.value = v;
    el.dataset.dirty = '1'; n++;
  }
  if (n) toast('You were signed out: your changes are back, press Save');
}
// Refused: an admin may have just taken a page or the admin role away. The header and the screen
// are drawn again from the access the user has now, and they are told what changed.
async function accessLost() {
  const before = S.me;
  try { await loadMe(); } catch (e) { return ''; }
  if (!S.me) { go('/login'); return 'Please sign in'; }
  const msg = accessTaken(before);
  if (msg) { toast(msg); setTimeout(route); }
  return msg;
}
// what was taken away since `before` (the user's details asked again), in the words they are told
function accessTaken(before) {
  if (!before || !S.me) return '';
  const gone = before.pages.filter(p => !S.pages.some(x => x.slug === p.slug)).map(p => p.name);
  return before.user.role === 'super' && S.me.user.role !== 'super' ? 'You are no longer an admin: ask an admin if you still need it'
    : gone.length ? `You no longer have access to ${gone.join(', ')}: ask an admin if you still need it` : '';
}

// ------------------------------------------------------------------ files -> base64 (downscaled)
function fileToB64(file, maxSide = 2000) {
  return new Promise((resolve, reject) => {
    if (file.type === 'application/pdf') {
      const fr = new FileReader(); fr.onload = () => resolve({ b64: fr.result.split(',')[1], type: file.type }); fr.onerror = reject; fr.readAsDataURL(file); return;
    }
    const img = new Image();
    img.onload = () => {
      const k = Math.min(1, maxSide / Math.max(img.width, img.height));
      const c = document.createElement('canvas'); c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
      c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
      resolve({ b64: c.toDataURL('image/jpeg', 0.92).split(',')[1], type: 'image/jpeg', w: c.width, h: c.height });
    };
    img.onerror = () => reject(new Error('could not read that image'));
    img.src = URL.createObjectURL(file);
  });
}

// ------------------------------------------------------------------ header
function header() {
  const u = S.me && S.me.user;
  if (!u) { $('#top').innerHTML = `<div class="bar"><span class="brand">Post Studio</span></div>`; return; }
  const h = location.hash;
  const nav = [['#/studio', 'Studio'], ['/review.html', 'Hourly approvals'], ['#/templates', 'Templates'], ...(u.role === 'super' ? [['#/admin/users', 'Admin']] : [])];
  $('#top').innerHTML = `<div class="bar"><span class="brand">Post Studio</span>
    <nav class="nav">${nav.map(([href, l]) => `<a href="${href}" class="${h.startsWith(href) || (href === '#/studio' && h.startsWith('#/studio')) ? 'on' : ''}">${l}</a>`).join('')}</nav>
    ${S.pages.length ? `<label class="pagebadge">Page <select id="pagesel">${S.pages.map(p => `<option value="${esc(p.slug)}" ${p.slug === S.page ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select></label>` : ''}
    <span class="me">${esc(u.email)} · ${esc(u.role)} <a href="#/password">Password</a> <a href="#" id="logout">Sign out</a></span></div>
    <div id="health">${healthBanner()}</div>`;
  const sel = $('#pagesel');
  if (sel) sel.onchange = () => { setPage(sel.value); if (location.hash.startsWith('#/studio/b/')) go('/studio'); else route(); };
  $('#logout').onclick = async e => { e.preventDefault(); await api('/api/auth/logout', { method: 'POST', body: {} }).catch(() => {}); S.me = null; go('/login'); };
}
function setPage(slug) { S.page = slug; S.feed = null; S.picked.clear(); try { localStorage.setItem('studioPage', slug); } catch (e) {} }

// The pipeline's health (/api/health): what is not working right now (hourly posts, the topic feed,
// the publisher, Studio jobs, editors' requests), so nobody finds out from an empty queue. Asked at
// most once a minute (the server keeps its answer 5 minutes) and drawn when it arrives.
const LEVEL_PILL = { red: 'no', amber: 'warn', green: 'ok' };
function healthBanner() {
  const h = S.health, rs = (h && h.reasons) || [];
  if (!h || !['amber', 'red'].includes(h.status) || !rs.length) return '';
  const red = h.status === 'red', shown = rs.slice(0, 3);
  return `<div class="bar"><div class="job ${red ? 'err' : ''}" style="flex:1">${red ? '⛔ Part of the pipeline is not working' : '⚠ Part of the pipeline is running with problems'}:
    ${shown.map(r => esc(r.plain || r.text)).join(' · ')}${rs.length > shown.length ? ` · and ${rs.length - shown.length} more` : ''}
    ${S.me && S.me.user && S.me.user.role === 'super' ? ' <a href="#/admin/health">Details</a>' : ''}</div></div>`;
}
function loadHealth() {
  if (!S.me || Date.now() - S.healthAt < 60e3) return;
  S.healthAt = Date.now();
  api('/api/health').then(h => { S.health = h; const el = $('#health'); if (el) el.innerHTML = healthBanner(); }).catch(() => {});
}

// ------------------------------------------------------------------ auth screens
function loginView(next, why) {
  // (why=idle: sent here by a refusal, the review page's too; the line stays while they sign in)
  $('#main').innerHTML = `<form class="login panel" id="f"><h1>Sign in</h1>
    ${why === 'idle' ? '<div class="muted" data-why>Signed out after a break: sign in to carry on.</div>' : ''}
    <input type="email" name="email" placeholder="Email" autocomplete="username" required autofocus>
    <input type="password" name="password" placeholder="Password" autocomplete="current-password" required>
    <button class="btn primary">Sign in</button><a href="#/forgot" class="muted">Forgot password?</a></form>`;
  $('#f').onsubmit = async e => {
    e.preventDefault();
    const btn = e.target.querySelector('button');
    if (btn.disabled) return;
    btn.disabled = true; btn.textContent = 'Signing in…';
    try { await api('/api/auth/login', { method: 'POST', body: { email: e.target.email.value, password: e.target.password.value } });
          await loadMe(); if (next === 'review') location.href = '/review.html';
          else go(next && next.startsWith('/') && !next.startsWith('/login') ? next : '/studio'); }
    catch (err) { toast(err.message); btn.disabled = false; btn.textContent = 'Sign in'; }
  };
}
function forgotView() {
  $('#main').innerHTML = `<form class="login panel" id="f"><h1>Reset password</h1><div class="muted">Enter your email. You'll get a reset link, or your admin can send you one from the admin panel.</div>
    <input type="email" name="email" placeholder="Email" required autofocus><button class="btn primary">Send</button><a href="#/login" class="muted">Back to sign in</a></form>`;
  $('#f').onsubmit = async e => {
    e.preventDefault();
    const btn = e.target.querySelector('button');
    if (btn.disabled) return;
    btn.disabled = true; btn.textContent = 'Sending…';
    const r = await api('/api/auth/forgot', { method: 'POST', body: { email: e.target.email.value } }).catch(err => ({ message: err.message, failed: true }));
    toast(r.message);
    // each link sent replaces the one before: a second tap at once would kill the link just emailed
    setTimeout(() => { btn.disabled = false; btn.textContent = 'Send'; }, r.failed ? 0 : 5000);
  };
}
const LINK_WAYS = '<div><a href="#/forgot" class="muted">Send me a new link</a> · <a href="#/login" class="muted">Sign in</a></div>';
function setPasswordView(token) {
  $('#main').innerHTML = `<form class="login panel" id="f"><h1>${token ? 'Set your password' : 'Change your password'}</h1>
    <div class="muted">At least 10 characters, with letters and a number.</div>
    ${token ? '' : '<input type="password" name="current" placeholder="Current password" required autocomplete="current-password">'}
    <input type="password" name="pw" placeholder="New password" required autocomplete="new-password">
    <input type="password" name="pw2" placeholder="Repeat new password" required autocomplete="new-password">
    <button class="btn primary">Save</button>${token ? LINK_WAYS : ''}</form>`;
  $('#f').onsubmit = async e => {
    e.preventDefault();
    const f = e.target, btn = f.querySelector('button');
    if (btn.disabled) return;                                        // (a second tap would spend the used link again)
    if (f.pw.value !== f.pw2.value) return toast('The two passwords differ');
    btn.disabled = true; btn.textContent = 'Saving…';
    try {
      if (token) await api('/api/auth/reset', { method: 'POST', body: { token, password: f.pw.value } });
      else await api('/api/auth/password', { method: 'POST', body: { current: f.current.value, password: f.pw.value } });
      toast('Password saved'); history.replaceState(null, '', '#/studio'); await loadMe(); go('/studio');
    } catch (err) {
      btn.disabled = false; btn.textContent = 'Save';
      if (token && err.body && err.body.expired) {
        $('#main').innerHTML = `<div class="login panel"><h1>This link no longer works</h1>
          <div class="muted">It has expired (links last 48 hours) or was already used. Ask for a new link, or sign in if you have set your password.</div>${LINK_WAYS}</div>`;
      } else toast(err.message);
    }
  };
}
async function loadMe() {
  try {
    const r = await api('/api/auth/me');
    S.me = r; S.pages = r.pages || []; S.meAt = Date.now();
    if (!S.pages.some(p => p.slug === S.page)) setPage(S.pages[0] ? S.pages[0].slug : null);
    retryNow();                                                      // (signed in again: the saves that waited for it)
  } catch (e) { if (e.status !== 401) throw e; S.me = null; }   // storage or the network not answering is not signed out
}

// ------------------------------------------------------------------ studio home
async function studioHome() {
  // the Studio opens on today's stories; unfinished batches (if any) sit above them
  const nav = S.nav;
  if (!S.pages.length) {                                             // (an admin may have given one since: asked again first)
    await loadMe().catch(() => {});
    if (nav !== S.nav) return;
    if (!S.me) return route();
    header();
    if (!S.pages.length) { $('#main').innerHTML = `<div class="empty">No pages are assigned to you yet. Ask an admin.</div>`; return; }
  }
  const [r] = await Promise.all([api(`/api/studio/batches?page=${encodeURIComponent(S.page)}`).catch(e => ({ batches: [], failed: e })), topicsView()]);
  const open = r.batches.filter(b => !b.done);
  if ((!open.length && !r.failed) || nav !== S.nav || !location.hash.match(/^#\/studio\/?$/)) return;
  const box = document.createElement('div');
  box.className = 'panel'; box.style.marginBottom = '14px';
  // (not listed is not gone: the editor's half-done batch is still there)
  if (r.failed) {
    box.dataset.failed = '';
    // (fixed words: storage's own message carries a status code, and was written for a change refused)
    const why = r.failed.offline ? 'No connection: they are still there. Check the internet connection, then try again.'
      : r.failed.status === 401 || r.failed.status === 403 ? r.failed.message : 'they are still there. Try again in a minute.';
    box.innerHTML = `<div class="row"><span>Could not load your unfinished batches right now: ${esc(why)}</span><span class="spacer"></span>
      <button class="btn small" id="b-again">Try again</button></div>`;
    $('#main').prepend(box);
    $('#b-again').onclick = () => route();
    return;
  }
  box.innerHTML = `<div class="row"><h3>Unfinished batches</h3><span class="spacer"></span>
    <label class="muted"><input type="checkbox" id="b-all"> Select all</label>
    <button class="btn small danger" id="b-clear" disabled>Clear selected</button></div>
    <div style="overflow-x:auto"><table class="batches"><tr><th></th><th>Started</th><th>By</th><th>Stories</th><th></th></tr>${open.map(b => `<tr>
    <td>${b.cleared ? '' : `<input type="checkbox" data-bid="${esc(b.id)}">`}</td><td>${when(b.created_at)}</td><td>${esc(b.user)}</td>
    <td>${b.posts.map(p => `<div>${esc(cut(p.headline, 80))} <span class="pill">${esc(p.status.replace('_', ' '))}</span></div>`).join('')}</td>
    <td>${b.cleared ? `<span class="muted" data-cleared>${esc(b.cleared.by || 'Someone')} cleared it ${esc(ago(b.cleared.at))}</span> · <button class="btn small" data-restore-bid="${esc(b.id)}">Restore</button>`
      : `<a class="btn small" href="#/studio/b/${esc(b.id)}/${resumeStep(b.posts)}">Continue</a>`}</td></tr>`).join('')}</table></div>`;
  $('#main').prepend(box);
  // your batch a colleague cleared (listed for a day): taken back, it is listed as before
  box.querySelectorAll('[data-restore-bid]').forEach(bn => bn.onclick = async () => {
    try { await api(`/api/studio/batches/${encodeURIComponent(bn.dataset.restoreBid)}/restore`, { method: 'POST', body: {} }); toast('Restored'); studioHome(); }
    catch (e) { toast(e.message); }
  });
  const boxes = () => [...box.querySelectorAll('[data-bid]')];
  const sync = () => { const n = boxes().filter(c => c.checked).length; $('#b-clear').disabled = !n; $('#b-clear').textContent = n ? `Clear selected (${n})` : 'Clear selected'; };
  boxes().forEach(c => c.onchange = sync);
  $('#b-all').onchange = e => { boxes().forEach(c => { c.checked = e.target.checked; }); sync(); };
  $('#b-clear').onclick = async () => {
    const ids = boxes().filter(c => c.checked).map(c => c.dataset.bid);
    // a colleague's batch may be the one they are working in right now (Select all takes theirs too)
    const theirs = [...new Set(open.filter(b => ids.includes(b.id) && b.user !== S.me.user.email).map(b => b.user))];
    const them = ids.length > 1 ? 'them' : 'it';
    if (!ids.length || !confirm(`Clear ${ids.length} batch${ids.length > 1 ? 'es' : ''}? `
                                + (theirs.length ? `${ids.length > 1 ? 'Some were' : 'It was'} started by ${theirs.join(', ')}, who may still be working on ${them} (they will see that you cleared ${them}). ` : '')
                                + 'Their unfinished posts are dropped (nothing posted is affected).')) return;
    try { const r = await api('/api/studio/batches/clear', { method: 'POST', body: { ids } }); toast(`Cleared ${r.cleared}`); studioHome(); }
    catch (e) { toast(e.message); }
  };
}
// a headline cut to fit, at a word, and shown as cut
const cut = (t, n) => (t.length > n ? `${t.slice(0, n - 1).replace(/\s+\S*$/, '')}…` : t);
function resumeStep(posts) {
  const st = posts.map(p => p.status);
  if (st.some(s => s === 'draft')) return 'ideas';
  if (st.some(s => s === 'ideated' || s === 'visual_ready')) return 'visual';
  if (st.some(s => s === 'captioned')) return 'check';
  return 'post';
}

// ------------------------------------------------------------------ 4.3 topic selection
async function topicsView() {
  if (!S.page) return go('/studio');
  const nav = S.nav;
  const page = S.pages.find(p => p.slug === S.page);
  $('#main').innerHTML = `<div class="row"><h2>Pick up to 5 stories · ${esc(page.name)}</h2></div>
    <div class="panel grid">
      <div class="row"><span class="muted">Published in the last</span>
        ${[3, 12, 24].map(h => `<button class="btn small ${h === S.hours ? 'primary' : ''}" data-h="${h}">${h} hours</button>`).join('')}
        <span class="spacer"></span><span class="muted" id="upd"></span></div>
      <form class="row" id="urlf"><input type="url" name="url" placeholder="…or paste a link (news, X, Reddit)" style="flex:1 1 260px"><button class="btn">Add link</button></form>
      <div id="list" class="muted">Loading…</div>
      <div class="row"><span class="muted" id="count"></span><span class="spacer"></span><button class="btn primary" id="cont" disabled>Continue to ideas</button></div>
    </div>`;
  document.querySelectorAll('[data-h]').forEach(b => b.onclick = () => { S.hours = Number(b.dataset.h); try { localStorage.setItem('studioHours', S.hours); } catch (e) {} topicsView(); });
  $('#urlf').onsubmit = async e => {
    e.preventDefault();
    const f = e.target, btn = f.querySelector('button');
    if (btn.disabled) return;                                        // (reading the link: up to 9 s)
    if (S.picked.size >= 5) return toast('5 stories selected already');
    let href = '';
    try { href = new URL(f.url.value.trim()).href; } catch (err) {}
    if (href && [...S.picked.values(), ...(S.feedExtra || [])].some(x => x.url === href)) { f.reset(); return toast('That link is added already'); }
    btn.disabled = true; btn.textContent = 'Reading…';
    try { const r = await api('/api/studio/url', { method: 'POST', body: { page: S.page, url: f.url.value } });
          if (nav !== S.nav) return;
          S.picked.set(r.story.id, r.story); S.feedExtra = [r.story, ...(S.feedExtra || []).filter(x => x.id !== r.story.id)]; f.reset(); drawTopics(); }
    catch (err) { toast(err.message); }
    finally { btn.disabled = false; btn.textContent = 'Add link'; }
  };
  $('#cont').onclick = async () => {
    $('#cont').disabled = true;
    try { const r = await api('/api/studio/batches', { method: 'POST', body: { page: S.page, hours: S.hours, stories: [...S.picked.values()] } });
          S.picked.clear(); S.feedExtra = []; go(`/studio/b/${r.batch.id}/ideas`); }
    catch (err) { toast(err.message); $('#cont').disabled = false; }
  };
  // (the editor may have left for another screen while the list loaded: it is not drawn there)
  try {
    const feed = await api(`/api/studio/feed?page=${encodeURIComponent(S.page)}&hours=${S.hours}`);
    if (nav !== S.nav) return;
    S.feed = feed; S.feed.fallback = '';
    if (!S.feed.stories.length) {                   // never an empty list: show the latest stories
      const all = await api(`/api/studio/feed?page=${encodeURIComponent(S.page)}&hours=0`);
      if (nav !== S.nav) return;
      if (all.stories.length) { S.feed = { ...all, fallback: `Nothing new in the last ${S.hours} hours, showing the latest stories.` }; }
    }
  } catch (e) {
    if (nav !== S.nav) return;
    $('#list').innerHTML = `${esc(e.message)} <button class="btn small" id="list-again">Try again</button>`;
    $('#list-again').onclick = () => route();
    return;
  }
  drawTopics();
}
function drawTopics() {
  // stories picked in another time window stay listed (ticked), so nothing goes into the batch unseen
  const inFeed = new Set((S.feed ? S.feed.stories : []).map(s => s.id));
  const stories = [...(S.feedExtra || []).filter(s => s.from_url), ...[...S.picked.values()].filter(s => !s.from_url && !inFeed.has(s.id)),
                   ...(S.feed ? S.feed.stories : [])];
  $('#upd').textContent = (S.feed && S.feed.fallback ? S.feed.fallback + ' ' : '')
    + (S.feed && S.feed.updated_at ? `Stories refreshed ${ago(S.feed.updated_at)} (every hour)` : 'The story list is prepared by the hourly routine');
  const full = S.picked.size >= 5;
  $('#list').innerHTML = stories.length ? stories.map(s => {
    const on = S.picked.has(s.id);
    return `<label class="story ${on ? 'sel' : ''}"><input type="checkbox" data-id="${esc(s.id)}" ${on ? 'checked' : ''} ${!on && full ? 'disabled' : ''}>
      <div><div class="h">${esc(s.headline)}</div><div class="muted">${s.sources.map(x => esc(x.name)).join(' · ')}${s.from_url ? ' · pasted link' : ''}</div>
      ${s.summary ? `<div class="muted">${esc(fullStop(s.summary.length > 220 ? s.summary.slice(0, 220) : s.summary))}</div>` : ''}
      ${s.learned ? `<div class="muted" style="font-size:12px" title="From your team's Okay / Not okay reviews">↺ ${esc(s.learned)}</div>` : ''}</div>
      <div class="muted">${s.published_at ? ago(s.published_at) : ''}</div></label>`;
  }).join('') : `<div class="empty">No stories in this window yet.</div>`;
  $('#count').textContent = `${S.picked.size} of 5 selected`;
  $('#cont').disabled = !S.picked.size;
  document.querySelectorAll('#list input[type=checkbox]').forEach(cb => cb.onchange = () => {
    const s = stories.find(x => x.id === cb.dataset.id);
    if (cb.checked) S.picked.set(s.id, s); else S.picked.delete(s.id);
    drawTopics();
  });
}

// ------------------------------------------------------------------ batch steps
const STEPS = [['ideas', 'Ideas'], ['visual', 'Visual'], ['caption', 'Caption'], ['check', 'Check'], ['post', 'Post']];
// The batch on screen (S.batch) takes an answer only for itself, and only if no answer to a later
// request has come first: a slow answer for a step or batch the editor has left, or to an earlier
// click, never replaces it (taps went to the wrong batch, or the wrong photo showed as picked).
// n: the request's number (asked()), taken when it was sent.
const asked = () => ++S.asked;
function adopt(n, b) {
  if (!b || location.hash.split('/')[3] !== b.id || (S.batch && S.batch.id === b.id && n < S.applied)) return false;
  S.applied = n; S.batch = b; S.batchAt = Date.now();
  return true;
}
// A load sent while a change to the batch was on its way (a photo tap) may have read it from before
// that change: the change's own answer brings the batch instead.
async function loadBatch(id) {
  const n = asked(), during = !!S.writing.get(id) && !!S.batch && S.batch.id === id;
  const r = await api(`/api/studio/batches/${encodeURIComponent(id)}`);
  if (!during && adopt(n, r.batch) && S.batch.page !== S.page && S.pages.some(p => p.slug === S.batch.page)) { setPage(S.batch.page); header(); }
  return r.batch;
}
function stepper(id, cur) {
  return `<div class="steps">${STEPS.map(([k, l], i) => `<a href="#/studio/b/${esc(id)}/${k}" class="${k === cur ? 'on' : ''}">${i + 1}. ${l}</a>`).join('')}</div>${S.batch && S.batch.id === id ? clearedNote() : ''}`;
}
// A batch someone cleared from the Studio home (perhaps a colleague, while it was being worked on):
// who did and when, and the way back. Its waiting steps were stopped, and new ones are refused
// until it is restored, so nothing is queued only to vanish.
function clearedNote() {
  const c = S.batch && S.batch.cleared;
  // a colleague cleared the editor's own batch: their Studio home still lists it for a day
  const theirs = c && S.me && S.batch.user === S.me.user.email && c.by !== S.me.user.email;
  return c ? `<div class="job err" style="margin-bottom:10px">${esc(c.by || 'Someone')} cleared this batch ${esc(ago(c.at))}: its waiting steps were stopped and it is no longer listed ${theirs ? 'for your colleagues (your Studio home shows it for a day)' : 'on the Studio home'}.
    <button class="btn small" data-restore>Restore it</button> to keep working on it.</div>` : '';
}
// the post's job line, then a line for each job still under way behind it (the editor searched for
// photos while the visual was being made)
const jobLine = p => [p.job, ...underWay(p).filter(j => j !== p.job)].map(j => lineFor(p, j)).join('');
function lineFor(p, j) {
  if (!j) return '';
  const label = { ideas: 'Writing ideas', images: 'Searching images', compose: 'Making the visual and caption', check: 'Checking the post' }[j.kind] || 'Working';
  const going = j.status === 'running' || !j.status;                  // (one behind the line: under way)
  if (j.status === 'queued') return `<div class="job">${label}: waiting for the Studio routine to start (usually 2-4 minutes; it is restarted automatically if it stalls). This page updates by itself.</div>`;
  if (going && j.photo && p.base_image && j.photo !== p.base_image.url)
    return `<div class="job">${label} from the photo picked before: it will not be used. Press Make visual for the photo you picked now.</div>`;
  // the session doing it stopped answering (the server marks it: running past 20 minutes, no claim since)
  if (going && j.stale) return `<div class="job err">${label} is taking far longer than it should: the Studio session doing it stopped answering. It is restarted automatically; ${j.kind === 'compose' ? 'press Start again' : 'press the button again'} to begin a new one now.</div>`;
  if (going) return `<div class="job">${label}… (Studio routine; this page updates by itself)</div>`;
  // (detail: the Studio machine's own error, shown to admins only)
  if (j.status === 'failed' || j.error) return `<div class="job err">${label} failed: ${esc(j.error || 'unknown error')}${j.detail && S.me && S.me.user.role === 'super'
    ? `<div class="muted">For the admin: ${esc(j.detail)}</div>` : ''}</div>`;
  if (j.status === 'cancelled')                                        // (stopped when someone cleared the batch)
    return `<div class="job">${label} was stopped${S.batch && S.batch.cleared ? ' when the batch was cleared: restore the batch (above), then press the button again.' : ': press the button again.'}</div>`;
  if (j.note) return `<div class="job">${label}: ${esc(j.note)}</div>`;      // a result that came too late to use
  return '';
}
// a summary that stops mid-sentence is shown as cut, never as if it were the whole story
function fullStop(t) { t = String(t || '').trim(); return !t || (/([.!?"'”’)\]।。！？]|\p{Extended_Pictographic}\uFE0F?)$/u.test(t) && !/(\.\.\.|…)$/.test(t)) ? t : `${t.replace(/(\.\.\.|…)$/, '')}…`; }
// the check made right after the visual (pre-check): under way for up to 10 minutes, then treated as not run
const autoChecking = c => c && c.status === 'running' && Date.now() - (c.at || 0) < 10 * 60e3;
const isSent = p => !!(p.publish && ['approved', 'publishing', 'scheduled', 'posted'].includes(p.publish.state));
const SEND_MS = 2 * 60e3;                // a Send still making its draft after this was cut short (routes/studio.js)
// The post's jobs under way: the one on its line, and those behind it (p.under_way) whose result is
// still wanted (no newer job of their kind; image searches add up)
const newestJob = (p, kind) => (p.jobs && p.jobs[kind]) || (p.job && p.job.kind === kind ? p.job.id : null);
const underWay = p => [...(p.job && ['queued', 'running'].includes(p.job.status) ? [p.job] : []),
  ...(p.under_way || []).filter(j => j.kind === 'images' || [null, j.id].includes(newestJob(p, j.kind)))];
const busy = b => b.posts.some(p => underWay(p).length || autoChecking(p.check) || (p.check && p.check.status === 'queued'));
// a visual is being made for the post, from the photo it has now (Make visual waits for it); one
// whose session stopped answering does not hold the button: Start again replaces it
const composing = p => underWay(p).some(j => j.kind === 'compose' && !j.stale && !(j.photo && p.base_image && j.photo !== p.base_image.url));
const stalled = p => !composing(p) && underWay(p).some(j => j.kind === 'compose' && j.stale);

// ------------------------------------------------------------------ while the editor types
// What an editor has typed and not sent or saved yet (My idea, a regenerate note, an image search,
// a caption on its way to the server, a post time), by post and field: every redraw draws it again.
// Fields carry data-keep="<field>" inside a card with data-p="<post id>".
const keyOf = el => `${el.closest('[data-p]').dataset.p}:${el.dataset.keep}`;
const unsent = (pid, field, drawn) => (S.unsent.has(`${pid}:${field}`) ? S.unsent.get(`${pid}:${field}`) : drawn);
// A caption or hashtags field: the text it was drawn with is what the editor types over (S.bases),
// saved with what they typed, so the server never saves it over one saved since elsewhere (another
// window, a colleague). Such a save is refused: theirs is kept in S.clash and shown under the
// field, with the editor's own text still in it, until they choose one.
const typedOver = (pid, field, drawn) => { if (!S.unsent.has(`${pid}:${field}`)) S.bases.set(`${pid}:${field}`, drawn); return unsent(pid, field, drawn); };
const same = (a, c) => String(a ?? '').replace(/\s+/g, ' ').trim() === String(c ?? '').replace(/\s+/g, ' ').trim();
function clashed(pid, now) {
  [['cap', now.caption], ['tags', (now.hashtags || []).join(' ')]].forEach(([f, theirs]) => {
    const k = `${pid}:${f}`;
    if (!(S.unsent.has(k) && !same(theirs, S.bases.get(k)) && !same(theirs, S.unsent.get(k)))) return;
    S.clash.set(k, theirs);
    if (now.by === 'visual') S.clashBy.set(k, 'visual'); else S.clashBy.delete(k);   // (the new visual's own caption, not a colleague's)
  });
}
function keepTyping() {
  document.querySelectorAll('#main [data-keep]').forEach(el =>
    el.addEventListener(el.type === 'radio' ? 'change' : 'input', () => S.unsent.set(keyOf(el), el.value)));
}
// Captions and hashtags save as the editor types (a moment after the last key) and when they leave
// the field. One save at a time, so an older text never lands after a newer one. A save that fails
// (no connection, storage not answering, signed out) is tried again later and later while the field
// still holds that text, and the field says it is not saved; one that is refused (a caption too
// long) says why and waits for the editor. Leaving the page with text not saved asks first.
const CAPTION_MAX = 5000;
function autosave(card, pid) {
  const bid = S.batch.id;                                            // (saved to this batch, wherever the editor is by then)
  card.querySelectorAll('[data-cap], [data-tags]').forEach(el => {
    const field = el.matches('[data-cap]') ? 'caption' : 'hashtags', k = keyOf(el);
    let timer;
    const save = loud => { clearTimeout(timer); return saveField(bid, pid, field, k, loud); };
    el.addEventListener('input', () => { S.unsent.set(k, el.value); clearTimeout(timer); timer = setTimeout(() => save(false), 800); });
    el.addEventListener('change', () => save(true));
    const count = field === 'caption' && card.querySelector('[data-count]');
    if (!count) return;
    // how long the caption is, and that a longer one is not saved (never cut)
    const recount = () => { const n = Array.from(el.value.trim()).length;
      count.textContent = n > CAPTION_MAX ? `${n.toLocaleString('en-US')} characters: at most ${CAPTION_MAX.toLocaleString('en-US')} can be posted, shorten it`
        : `${n.toLocaleString('en-US')} / ${CAPTION_MAX.toLocaleString('en-US')} characters`;
      count.classList.toggle('over', n > CAPTION_MAX); };
    el.addEventListener('input', recount); recount();
  });
  markUnsaved();
}
function saveField(bid, pid, field, k, loud) {
  return (S.saving = S.saving.then(async () => {
    const v = S.unsent.get(k);
    if (v === undefined) return;                                     // saved already (the newest text is always the one sent)
    try {
      await patchPost(pid, { [field]: v, [`base_${field}`]: S.bases.get(k) }, bid);
      S.bases.set(k, v);
      if (S.unsent.get(k) === v) S.unsent.delete(k);
      S.failed.delete(k); S.clash.delete(k); clearTimeout((S.retries.get(k) || {}).timer); S.retries.delete(k);
      if (loud) toast('Saved');
    } catch (err) {
      if (err.body && err.body.now) {                                // saved elsewhere since it was drawn
        clashed(pid, err.body.now); clearTimeout((S.retries.get(k) || {}).timer); S.retries.delete(k);
        if (S.clash.has(k)) S.failed.delete(k); else S.failed.set(k, `Not saved: ${err.message}`);
        loadBatch(bid).catch(() => {});
        return markUnsaved();
      }
      const later = !err.status || err.status >= 500 || err.status === 401;
      S.failed.set(k, later ? 'Not saved yet: trying again by itself (keep this page open)' : `Not saved: ${err.message}`);
      if (later) retryLater(bid, pid, field, k); else if (loud) toast(err.message);
    }
    markUnsaved();
  }));
}
function retryLater(bid, pid, field, k) {
  const r = S.retries.get(k) || { n: 0 };
  clearTimeout(r.timer);
  r.n++;
  r.run = () => (S.me ? saveField(bid, pid, field, k, false) : retryLater(bid, pid, field, k));   // (signed out: once they are back)
  r.timer = setTimeout(r.run, Math.min(60e3, 2000 * 2 ** (r.n - 1)));
  S.retries.set(k, r);
}
// every save waiting to be tried again, now (back online, or signed in again)
function retryNow() { S.retries.forEach(r => { clearTimeout(r.timer); r.run(); }); }
window.addEventListener('online', retryNow);
// each field whose save failed says so, below it; one saved elsewhere meanwhile shows theirs, to choose
function markUnsaved() {
  document.querySelectorAll('#main [data-cap], #main [data-tags]').forEach(el => {
    const k = keyOf(el), theirs = S.unsent.has(k) ? S.clash.get(k) : undefined;
    const msg = S.unsent.has(k) && (theirs !== undefined ? 'clash' : S.failed.get(k));
    let note = el.nextElementSibling && el.nextElementSibling.matches('[data-unsaved]') ? el.nextElementSibling : null;
    if (!msg) { if (note) note.remove(); return; }
    if (!note) { note = document.createElement('div'); note.className = 'job err'; note.dataset.unsaved = ''; el.after(note); }
    if (theirs === undefined) { note.textContent = msg; return; }
    const field = el.matches('[data-cap]') ? 'caption' : 'hashtags', pid = k.split(':')[0];
    note.innerHTML = `${S.clashBy.get(k) === 'visual' ? `Not saved: the new visual came with its own ${field === 'caption' ? 'caption' : 'hashtags'} while you typed. Theirs:`
      : `Not saved: other ${field === 'caption' ? 'words' : 'hashtags'} were saved here meanwhile (another window, or a colleague). Theirs:`}
      <div class="fb" style="margin:6px 0"><div class="txt">${esc(theirs)}</div></div>
      Yours is still in the box. <button class="btn small" data-mine>Keep mine</button> <button class="btn small" data-theirs>Use theirs</button>`;
    note.querySelector('[data-mine]').onclick = () => { S.bases.set(k, theirs); S.clash.delete(k); saveField(S.batch.id, pid, field, k, true); };
    note.querySelector('[data-theirs]').onclick = () => {
      el.value = theirs; el.dispatchEvent(new Event('input'));       // (the preview and the count follow)
      S.unsent.delete(k); S.bases.set(k, theirs); S.clash.delete(k); S.failed.delete(k); markUnsaved();
    };
  });
}
// a post sent (by a colleague, or in another window) before the editor's change to its caption was
// saved: the change is dropped, the field shows what was sent, and the editor is told once
function sentOver(b) {
  b.posts.filter(p => isSent(p) && ['cap', 'tags'].some(f => S.unsent.has(`${p.id}:${f}`))).forEach(p => {
    ['cap', 'tags'].forEach(f => { const k = `${p.id}:${f}`; S.unsent.delete(k); S.failed.delete(k); S.clash.delete(k); clearTimeout((S.retries.get(k) || {}).timer); S.retries.delete(k); });
    toast(`Your change to the caption of “${cut(p.story.headline, 60)}” was not sent: the post had already been sent. It shows what was sent.`);
  });
}
const unsaved = () => [...S.unsent.keys()].some(k => /:(cap|tags)$/.test(k));
window.addEventListener('beforeunload', e => { if (unsaved() && !S.reloading) { e.preventDefault(); e.returnValue = ''; } });
// the editor is in a field (or pressing a button), or a field holds text a redraw would not draw again
document.addEventListener('pointerdown', () => { S.pressed = Date.now(); }, true);
['pointerup', 'pointercancel'].forEach(e => document.addEventListener(e, () => { S.pressed = 0; }, true));   // (the click's own answer is drawn at once)
function editing() {
  const a = document.activeElement;
  if (a && $('#main').contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return true;
  if (Date.now() - (S.pressed || 0) < 1000) return true;
  return [...document.querySelectorAll('#main input, #main textarea, #main select')].some(el => !(el.dataset.keep || el.type === 'file'
    || (/^(checkbox|radio)$/.test(el.type) ? el.checked === el.defaultChecked
      : el.tagName === 'SELECT' ? [...el.options].every(o => o.selected === o.defaultSelected) : el.value === el.defaultValue)));
}
// A step follows its jobs while they run (every 10 s; the Post step its sending: pollMs). It is drawn
// again only when the batch changed, and never over the editor, after a poll or after one of their
// own actions alike: while they are in a field (or pressing a button), only each post's job lines and
// Make visual button change in place, the batch is still loaded every poll while something is under
// way, and the step is drawn once they leave the field. A load that fails is tried again later and
// later (up to about a minute), never sooner than the poll: a storage outage is not made worse.
const POLL_MS = 10000;
const drawnAs = b => JSON.stringify(b.posts) + busy(b) + JSON.stringify(b.cleared || null);
const lineOf = p => `<div data-line style="display:contents">${jobLine(p)}</div>`;
function pollMs(view, b) {                                           // how often a step follows the batch (0: not at all)
  if (view !== postView) return busy(b) ? POLL_MS : 0;
  // the Post step: while a post is being scheduled; slowly when every such post is waiting for
  // SocialPilot (that can last hours); and like every step while a job runs (a visual being made
  // again shows there before it can be sent)
  const sending = b.posts.filter(p => p.publish && ['approved', 'publishing'].includes(p.publish.state));
  const ms = sending.length ? (sending.every(p => p.publish.held) ? 5 * 60000 : 20000) : 0;
  // (a post with no visual yet: one made in another tab, or by a colleague, lets Send go)
  const waiting = b.posts.some(p => !p.final_image_url && !isSent(p));
  return busy(b) || waiting ? Math.min(ms || POLL_MS, POLL_MS) : ms;
}
const current = f => !!f && f === S.follow && f.nav === S.nav && location.hash.includes(f.id);
function follow(id, view) {
  clearTimeout(S.poll);
  const rs = $('#main [data-restore]');                              // (every step ends here: its cleared note's button)
  if (rs) rs.onclick = async () => {
    try { adopt(asked(), (await api(`/api/studio/batches/${encodeURIComponent(id)}/restore`, { method: 'POST', body: {} })).batch); toast('Restored: carry on'); again(view, id)(); }
    catch (err) { toast(err.message); }
  };
  S.drawn = drawnAs(S.batch);
  S.follow = { id, view, ms: pollMs(view, S.batch), nav: S.nav, fails: 0, retryAt: 0 };
  if (S.follow.ms) S.poll = setTimeout(tick, S.follow.ms);
}
async function reload(f) {                                           // false when it failed (tried again later and later)
  try { await loadBatch(f.id); f.fails = 0; f.retryAt = 0; return true; }
  catch (e) { f.fails++; f.retryAt = Date.now() + Math.min(60e3, Math.max(f.ms, POLL_MS) * 2 ** (f.fails - 1)) * (1 + Math.random() / 4); return false; }
}
async function tick() {
  const f = S.follow;
  if (!current(f)) return;
  loadHealth(); checkVersion();                                      // (at most once a minute)
  const loaded = await reload(f);
  if (!current(f)) return;
  if (!loaded) { S.poll = setTimeout(tick, f.retryAt - Date.now()); return; }
  if (drawnAs(S.batch) === S.drawn) { S.poll = setTimeout(tick, Math.max(f.ms, POLL_MS)); return; }   // nothing new
  drawWhenFree(f);
}
// the step drawn again with S.batch: now, or once the editor leaves the field they are in
function drawWhenFree(f) {
  if (!current(f)) return;
  clearTimeout(S.poll);
  drawLines();
  if (!editing()) return f.view(f.id, true);
  const wait = async () => {
    if (!current(f)) return;
    if (!editing()) return f.view(f.id, true);
    const ms = pollMs(f.view, S.batch);                              // nothing under way: it only waits for them
    if (ms && Date.now() - (S.batchAt || 0) >= ms && Date.now() >= f.retryAt && await reload(f) && current(f)) drawLines();
    if (current(f)) S.poll = setTimeout(wait, 1000);
  };
  S.poll = setTimeout(wait, 1000);
}
// The step drawn again after an editor's action (its answer is in S.batch), the same way, unless they
// have moved on meanwhile (a field they leave by going to another step saves on the way out: its
// answer must not draw the old step back)
function again(view, id) {
  const nav = S.nav;
  return () => {
    if (nav !== S.nav || !location.hash.includes(id)) return;
    if (!(S.follow && S.follow.id === id && S.follow.nav === nav)) S.follow = { id, view, ms: 0, nav, fails: 0, retryAt: 0 };
    drawWhenFree(S.follow);
  };
}
function drawLines() {
  document.querySelectorAll('#main [data-p]').forEach(card => {
    const p = S.batch.posts.find(x => x.id === card.dataset.p);
    if (!p) return;
    const line = card.querySelector('[data-line]');
    if (line) line.innerHTML = jobLine(p);
    const mk = card.querySelector('[data-make]');
    if (mk) { makeButton(mk, p); if (card.dataset.picking) mk.disabled = true; }
    // (Send waits for a visual being made, or made at all, and shown on the card: Send says which it shows)
    const sd = card.querySelector('[data-send]'), nv = card.querySelector('[data-novisual]');
    if (sd && !sd.dataset.busy) sd.disabled = composing(p) || !p.final_image_url || !card.dataset.visual;
    if (nv) nv.innerHTML = noVisual(p, card.dataset.visual);
  });
}
// Back to a tab left open (another tab or window may have changed the batch): the step follows it
// at once, drawn again only if it changed and never over the editor
document.addEventListener('visibilitychange', async () => {
  const f = S.follow;
  if (document.visibilityState !== 'visible' || !current(f)) return;
  if (await reload(f) && current(f) && drawnAs(S.batch) !== S.drawn) drawWhenFree(f);
});
// the Post step's line under a post with no visual on its card (a visual made meanwhile shows once the
// step is drawn again: at once, or when the editor leaves the field they are in)
function noVisual(p, shown) {
  if (shown || composing(p)) return '';
  return p.final_image_url ? '<div class="muted">A visual was made meanwhile: it shows here in a moment.</div>'
    : `<div class="muted">No visual yet: make it in <a href="#/studio/b/${esc(S.batch.id)}/visual">step 2 (Visual)</a> first.</div>`;
}
function makeButton(mk, p) {
  mk.disabled = !(p.idea && p.base_image) || composing(p) || isSent(p);
  mk.textContent = makeLabel(p);
}
const makeLabel = p => (composing(p) ? 'Making the visual…' : stalled(p) ? 'Start again' : p.final_image_url ? 'Regenerate visual' : 'Make visual');
// bid: the batch the post is in (the one on screen when the editor acted)
async function patchPost(pid, body, bid = S.batch.id) {
  const n = asked();
  const r = await writing(bid, () => api(`/api/studio/batches/${encodeURIComponent(bid)}/posts/${encodeURIComponent(pid)}`, { method: 'PATCH', body }));
  adopt(n, r.batch);
  return r.batch;
}
// the changes to each batch on their way (loadBatch)
async function writing(bid, fn) {
  S.writing.set(bid, (S.writing.get(bid) || 0) + 1);
  try { return await fn(); } finally { S.writing.set(bid, S.writing.get(bid) - 1); }
}
async function run(kind, postIds, feedback) {
  const n = asked(), bid = S.batch.id;
  const r = await writing(bid, () => api(`/api/studio/batches/${encodeURIComponent(bid)}/run`, { method: 'POST', body: { kind, post_ids: postIds, feedback } }))
    .catch(e => { if (e.body && e.body.cleared && S.batch.id === bid) S.batch = { ...S.batch, cleared: e.body.cleared }; throw e; });   // (drawn with its note)
  adopt(n, r.batch);
  // (Make all visuals: the posts whose idea's template was taken out of use are named, the others made)
  toast(`${r.already ? 'Already under way: this page updates by itself' : r.fired ? 'Started' : 'Queued for the Studio routine'}${r.skipped ? `. ${r.skipped}` : ''}`);
  return r;
}
const storyHead = p => `<div><h3>${esc(p.story.headline)}</h3><div class="muted">${p.story.sources.map(s => s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.name)}</a>` : esc(s.name)).join(' · ')}
  ${p.story.published_at ? ' · ' + ago(p.story.published_at) : ''}</div>${p.story.summary ? `<div style="margin-top:6px">${esc(fullStop(p.story.summary))}</div>` : ''}</div>`;

// the approved template's preview, beside an idea that names one (kept in the browser a few minutes)
const tplThumb = (page, spec) => `<img class="tplthumb" alt="how the template looks" loading="lazy" src="/api/templates/image?page=${encodeURIComponent(page)}&id=${encodeURIComponent(spec)}&v=in_use&kind=preview" onerror="this.remove()">`;

// 4.4 visual ideation
async function ideasView(id, fromPoll) {
  const nav = S.nav;
  if (!fromPoll) await loadBatch(id);
  if (nav !== S.nav) return;                                         // they went to another screen meanwhile
  const redraw = again(ideasView, id);
  const b = S.batch;
  // every post has an idea, or My idea typed for it (saved on the way to the next step)
  const typed = p => !!(S.unsent.get(`${p.id}:own`) || '').trim();
  const ready = () => S.batch.posts.every(p => p.idea || typed(p));
  $('#main').innerHTML = stepper(id, 'ideas') + `<div class="grid">${b.posts.map(p => {
    const ideas = p.story.ideas || [];
    const ch = p.idea && p.idea.choice;
    return `<div class="card" data-p="${esc(p.id)}">${storyHead(p)}${lineOf(p)}
      <div class="ideas">${ideas.map((idea, i) => `<div class="idea ${ch === i + 1 ? 'sel' : ''}" data-choice="${i + 1}">
          <div class="t">Idea ${i + 1}: ${esc(idea.title)}</div><div class="tpl">Template: ${esc(idea.template)}</div>${idea.template_spec ? tplThumb(b.page, idea.template_spec) : ''}<div>${esc(idea.description)}</div></div>`).join('')}
        <div class="idea ${ch === 'own' ? 'sel' : ''}" data-choice="own"><div class="t">My idea</div>
          <textarea rows="3" placeholder="Describe your own image concept" data-keep="own">${esc(unsent(p.id, 'own', ch === 'own' ? p.idea.text : ''))}</textarea></div></div>
      <div class="row"><button class="btn small" data-regen>Regenerate ideas</button>${!ideas.length && !(p.job && p.job.status !== 'done') ? '<span class="muted">No ideas yet: regenerate, or write your own.</span>' : ''}</div></div>`;
  }).join('')}</div>
  <div class="row" style="margin-top:14px"><span class="spacer"></span><button class="btn primary" id="cont" ${ready() ? '' : 'disabled'}>Continue to visuals</button></div>`;
  keepTyping();
  document.querySelectorAll('.card').forEach(card => {
    const pid = card.dataset.p;
    const own = async t => { await patchPost(pid, { idea: { choice: 'own', text: t } }, id); if ((S.unsent.get(`${pid}:own`) || '').trim() === t) S.unsent.delete(`${pid}:own`); };
    card.querySelectorAll('.idea').forEach(el => el.onclick = async e => {
      // one pick at a time, shown at once: the last one tapped is the one saved
      if (e.target.tagName === 'TEXTAREA' || card.dataset.picking) return;
      const c = el.dataset.choice;
      if (c === 'own' && !el.querySelector('textarea').value.trim()) { el.querySelector('textarea').focus(); return; }
      card.dataset.picking = '1';
      card.querySelectorAll('.idea').forEach(o => o.classList.toggle('sel', o === el));
      try { if (c === 'own') await own(el.querySelector('textarea').value.trim());
            else await patchPost(pid, { idea: { choice: Number(c) } }, id);
      } catch (err) { toast(err.message); }
      delete card.dataset.picking; redraw();
    });
    const ta = card.querySelector('textarea');
    ta.addEventListener('input', () => { $('#cont').disabled = !ready(); });
    ta.onchange = async () => { if (ta.value.trim()) { await own(ta.value.trim()).catch(e => toast(e.message)); redraw(); } };
    card.querySelector('[data-regen]').onclick = async () => { await run('ideas', [pid]).catch(e => toast(e.message)); redraw(); };
  });
  $('#cont').onclick = async () => {
    // one tap: My idea typed and not saved yet is saved, then on to the visuals
    $('#cont').disabled = true;
    try { for (const p of S.batch.posts) if (typed(p) && !p.idea) await patchPost(p.id, { idea: { choice: 'own', text: S.unsent.get(`${p.id}:own`).trim() } }, id); }
    catch (err) { toast(err.message); $('#cont').disabled = !ready(); return; }
    go(`/studio/b/${id}/visual`);
  };
  follow(id, ideasView);
}

// whose face a candidate shows (bore/faceid.py): compared with the subject's ESPN headshot or
// Wikipedia photo; nothing when it can't tell or there is no such photo of them
const FACE_REF = { espn: 'ESPN headshot', wikipedia: 'Wikipedia photo' };
function faceLine(c) {
  if (!c.id_name || !['match', 'mismatch'].includes(c.id_match)) return '';
  const why = `Face compared with ${esc(c.id_name)}’s ${FACE_REF[c.id_ref] || 'reference photo'} (similarity ${Number(c.id_score || 0).toFixed(2)})`;
  return c.id_match === 'match'
    ? `<div class="src warn" style="color:var(--ok)" title="${why}">✓ matches ${esc(c.id_name)}</div>`
    : `<div class="src warn" style="color:var(--no)" title="${why}">⚠ may not be ${esc(c.id_name)}</div>`;
}

// 4.5 visual creation
async function visualView(id, fromPoll) {
  const nav = S.nav;
  if (!fromPoll) await loadBatch(id);
  if (nav !== S.nav) return;                                         // they went to another screen meanwhile
  const redraw = again(visualView, id);
  const b = S.batch;
  const ready = b.posts.every(p => p.final_image_url);
  $('#main').innerHTML = stepper(id, 'visual') + `<div class="row" style="margin-bottom:12px"><span class="muted">Set a base image for each story, then make the visuals.</span><span class="spacer"></span>
    <button class="btn" id="all">Make all visuals</button></div><div class="grid">${b.posts.map(p => {
    const cands = [...(p.story.images || []), ...(p.extra_images || [])];
    const sent = isSent(p), off = sent ? 'disabled' : '';          // (sent: nothing here changes it any more)
    return `<div class="card" data-p="${esc(p.id)}">${storyHead(p)}
      <div class="muted idealine">${p.idea && p.idea.template_spec ? tplThumb(b.page, p.idea.template_spec) : ''}Idea: ${p.idea ? esc(p.idea.title || 'My idea') + ' — ' + esc(p.idea.text) : '<b>none chosen</b> (go back to Ideas)'}</div>${lineOf(p)}
      ${sent ? '<div class="muted" data-sent>Sent to SocialPilot: it can no longer be changed here.</div>' : ''}
      <div class="two"><div class="grid">
        <div class="final">${p.final_image_url ? `<img src="${esc(p.final_image_url)}" alt="finished post" onclick="zoom('${esc(p.final_image_url)}')">` :
          p.base_image ? `<img src="${esc(p.base_image.url)}" alt="base image" onclick="zoom('${esc(p.base_image.url)}')"><div class="muted">Base image (${esc(p.base_image.source)}${p.base_image.credit ? ': ' + esc(p.base_image.credit) : ''})</div>` :
          '<div class="paste">No base image yet</div>'}</div>
        <div class="row"><button class="btn primary small" data-make ${p.idea && p.base_image && !composing(p) && !sent ? '' : 'disabled'}>${makeLabel(p)}</button></div>
        ${p.final_image_url && !sent ? `<textarea rows="2" placeholder="Optional: what to change when regenerating" data-note data-keep="note">${esc(unsent(p.id, 'note', ''))}</textarea>` : ''}
      </div><div class="grid">
        <div><b>Add image</b> <span class="muted">upload, or paste from the clipboard into this card</span></div>
        <label class="paste" style="cursor:${sent ? 'default' : 'pointer'}"><input type="file" accept="image/*" hidden data-file ${off}>Choose a file or paste an image</label>
        <div class="row"><b>AI selects an image</b></div>
        <form class="row" data-searchf><input type="text" name="q" data-keep="q" value="${esc(unsent(p.id, 'q', p.story.subject || ''))}" placeholder="Who should be in the photo?" style="flex:1 1 240px" ${off}>
          <button class="btn small" data-search ${off}>Search more images</button></form>
        <div class="cands ${sent ? 'off' : ''}">${cands.length ? cands.map(c => `<div class="cand ${p.base_image && p.base_image.url === c.url ? 'sel' : ''}" data-url="${esc(c.url)}">
            <img src="${esc(c.url)}" alt="" loading="lazy" referrerpolicy="no-referrer"><div class="src" title="${esc([c.credit, c.via, c.post_url].filter(Boolean).join(' · '))}">${esc(c.source)}${c.credit ? ': ' + esc(c.credit) : ''}${c.size ? ' · ' + esc(c.size) : ''}</div>
            ${c.via || c.consensus > 1 ? `<div class="src">${c.note === 'video frame' ? '▶ ' : ''}${esc(c.via || '')}${c.consensus > 1 ? ` · used by ${c.consensus}` : ''}${c.post_url ? ` · <a href="${esc(c.post_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">open</a>` : ''}</div>` : ''}
            ${faceLine(c)}
            ${(c.flags || []).length ? `<div class="src warn" style="color:var(--warn)">⚠ ${(c.flags || []).map(esc).join(' · ')}</div>` : ''}</div>`).join('')
            : '<div class="muted">No candidates yet: search for more.</div>'}</div>
        <div class="muted">Search images can be copyrighted: check the source before using one.</div>
      </div></div></div>`;
  }).join('')}</div><div class="row" style="margin-top:14px"><a class="btn" href="#/studio/b/${esc(id)}/ideas">Back</a><span class="spacer"></span>
    <button class="btn primary" id="cont" ${ready ? '' : 'disabled'}>Continue to captions</button></div>`;
  keepTyping();
  document.querySelectorAll('.card').forEach(card => {
    const pid = card.dataset.p;
    const post = () => S.batch.posts.find(x => x.id === pid);
    if (isSent(post())) return;
    // a finished visual (and its check) is replaced only once the editor says so
    const replaces = () => !post().final_image_url || confirm('This replaces the finished visual and its check. Use this photo?');
    const upload = async file => {
      if (!replaces()) return;
      try { const f = await fileToB64(file); await patchPost(pid, { base_image_b64: f.b64, base_image_type: f.type }, id); redraw(); toast('Base image set'); }
      catch (err) { toast(err.message); }
    };
    // (emptied at once: the same file chosen again, after Cancel or an error, is chosen again)
    card.querySelector('[data-file]').onchange = e => { const f = e.target.files[0]; e.target.value = ''; if (f) upload(f); };
    card.addEventListener('paste', e => { const it = [...e.clipboardData.items].find(i => i.type.startsWith('image/')); if (it) { e.preventDefault(); upload(it.getAsFile()); } });
    card.tabIndex = 0;
    // one pick at a time, shown at once: the last photo tapped is the one saved (the one already
    // chosen: nothing to do)
    card.querySelectorAll('.cand').forEach(c => c.onclick = async () => {
      if (card.dataset.picking || c.classList.contains('sel') || !replaces()) return;
      card.dataset.picking = '1';
      card.querySelector('[data-make]').disabled = true;              // (until the photo is saved)
      card.querySelectorAll('.cand').forEach(o => o.classList.toggle('sel', o === c));
      card.querySelector('.cands').classList.add('off');
      await patchPost(pid, { base_image_url: c.dataset.url }, id).catch(e => toast(e.message));
      delete card.dataset.picking; redraw();
    });
    card.querySelector('[data-searchf]').onsubmit = async e => { e.preventDefault();
      // sent with Enter, the cursor would stay in the box, and a box the editor is in is never drawn
      // over: the photos found would not show until they clicked elsewhere
      e.target.q.blur();
      const q = e.target.q.value.trim();
      await run('images', [pid], q ? `Search for photos of: ${q}` : '').catch(err => toast(err.message)); redraw(); };
    const mk = card.querySelector('[data-make]');
    mk.onclick = async () => {
      mk.disabled = true;                                              // one click, one visual
      const note = card.querySelector('[data-note]');
      try { await run('compose', [pid], note ? note.value : ''); S.unsent.delete(`${pid}:note`); } catch (e) { toast(e.message); }
      redraw();
    };
  });
  $('#all').onclick = async () => {                                    // one job per post (the server makes them)
    const ids = S.batch.posts.filter(p => p.idea && p.base_image && !p.final_image_url && !composing(p)).map(p => p.id);
    if (!ids.length) return toast('Set an idea and a base image first (or everything is made or being made already)');
    $('#all').disabled = true;
    await run('compose', ids).catch(e => toast(e.message)); redraw();
  };
  $('#cont').onclick = () => go(`/studio/b/${id}/caption`);
  follow(id, visualView);
}

// 4.6 caption and hashtags
async function captionView(id, fromPoll) {
  const nav = S.nav;
  if (!fromPoll) await loadBatch(id);
  if (nav !== S.nav) return;                                         // they went to another screen meanwhile
  const redraw = again(captionView, id);
  const b = S.batch;
  sentOver(b);
  $('#main').innerHTML = stepper(id, 'caption') + `<div class="muted" style="margin-bottom:10px">Captions and hashtags are written in the page's tone with the visual. Edit freely: changes save as you type, and a visual made again keeps your words.</div>
    <div class="grid">${b.posts.map(p => `<div class="card" data-p="${esc(p.id)}"><div class="two">
      <div class="final">${p.final_image_url ? `<img src="${esc(p.final_image_url)}" alt="" onclick="zoom('${esc(p.final_image_url)}')">` : '<div class="paste">No visual yet</div>'}</div>
      <div class="grid">${storyHead(p)}${lineOf(p)}${isSent(p) ? '<div class="muted">Sent to SocialPilot: it can no longer be changed here.</div>' : ''}
        <label class="muted">Caption</label><textarea rows="9" data-cap data-keep="cap" ${isSent(p) ? 'readonly' : ''}>${esc(isSent(p) ? p.caption : typedOver(p.id, 'cap', p.caption))}</textarea>
        ${isSent(p) ? '' : '<div class="muted count" data-count></div>'}
        <label class="muted">Hashtags</label><input type="text" data-tags data-keep="tags" ${isSent(p) ? 'readonly' : ''} value="${esc(isSent(p) ? (p.hashtags || []).join(' ') : typedOver(p.id, 'tags', (p.hashtags || []).join(' ')))}">
        ${p.suggested ? `<div class="job">The new visual came with its own caption; yours is kept. Theirs:<div class="fb" style="margin:6px 0"><div class="txt">${esc(p.suggested.caption)}\n\n${esc((p.suggested.hashtags || []).join(' '))}</div></div>
          <button class="btn small" data-use>Use this caption</button> <button class="btn small" data-dismiss>Keep mine</button></div>` : ''}
      </div></div></div>`).join('')}</div>
    <div class="row" style="margin-top:14px"><a class="btn" href="#/studio/b/${esc(id)}/visual">Back</a><span class="spacer"></span>
      <button class="btn primary" id="cont" ${b.posts.every(p => p.caption) ? '' : 'disabled'}>Continue to check</button></div>`;
  keepTyping();
  document.querySelectorAll('.card').forEach(card => {
    const pid = card.dataset.p;
    if (!isSent(b.posts.find(p => p.id === pid))) autosave(card, pid);
    const offer = how => async () => {
      try { await patchPost(pid, { suggested: how }); S.unsent.delete(`${pid}:cap`); S.unsent.delete(`${pid}:tags`); redraw(); }
      catch (err) { toast(err.message); }
    };
    if (card.querySelector('[data-use]')) { card.querySelector('[data-use]').onclick = offer('use'); card.querySelector('[data-dismiss]').onclick = offer('dismiss'); }
  });
  $('#cont').onclick = () => go(`/studio/b/${id}/check`);
  follow(id, captionView);
}

// 4.7 post check (optional, advisory)
const CHECKS = [['factual', 'Factual correctness'], ['timing', 'Relevant right now'], ['grammar', 'Grammar and typos'], ['other', 'Other errors fans may spot'], ['guidelines', 'Facebook guidelines']];
const findings = res => `<div class="checks">${CHECKS.map(([k, l]) => { const items = res[k] || [];
  return `<div class="c"><b>${l}</b><div>${items.length ? items.map(i => `<div class="pill warn" style="white-space:normal">${esc(i)}</div>`).join(' ') : '<span class="pill ok">No issues</span>'}</div></div>`; }).join('')}</div>`;
async function checkView(id, fromPoll) {
  const nav = S.nav;
  if (!fromPoll) await loadBatch(id);
  if (nav !== S.nav) return;                                         // they went to another screen meanwhile
  const redraw = again(checkView, id);
  const b = S.batch;
  const settled = b.posts.every(p => p.check && ['done', 'skipped'].includes(p.check.status));
  $('#main').innerHTML = stepper(id, 'check') + `<div class="row" style="margin-bottom:12px"><span class="muted">An independent reviewer (a separate Claude session with web search, which never sees how the post was made) checks each post. Advisory only.</span>
    <span class="spacer"></span><button class="btn" id="runall">Run check</button><button class="btn" id="skip">${b.posts.some(p => p.check && p.check.status === 'done') ? 'Skip remaining checks' : 'Skip check'}</button></div>
    <div class="grid">${b.posts.map(p => {
      const c = p.check || {};
      const res = c.results || {};
      return `<div class="card" data-p="${esc(p.id)}"><div class="two">
        <div class="final">${p.final_image_url ? `<img src="${esc(p.final_image_url)}" alt="" onclick="zoom('${esc(p.final_image_url)}')">` : ''}</div>
        <div class="grid">${storyHead(p)}${lineOf(p)}
          ${c.status === 'done' ? `${findings(res)}
            ${c.auto && !c.stale ? '<div class="muted">Checked automatically right after the visual was made.</div>' : ''}
            ${c.stale ? '<div class="job">Edited since the check: run it again to re-check.</div>' : ''}`
          : c.status === 'skipped' ? '<span class="pill">Check skipped</span>'
          // (checked again: the last check's findings stay until the new one replaces them)
          : c.status === 'queued' ? (c.previous && c.previous.results ? `<div class="muted" data-previous>Previous check (shown until the new one is ready):</div>${findings(c.previous.results)}${c.previous.stale ? '<div class="job">Edited since that check.</div>' : ''}` : '')
          : autoChecking(c) ? '<div class="job">Checking automatically: ready about 2 minutes after the visual. This page updates by itself.</div>'
          : '<span class="muted">Not checked yet.</span>'}
          <div class="row"><a class="btn small" href="#/studio/b/${esc(id)}/visual">Fix image</a><a class="btn small" href="#/studio/b/${esc(id)}/caption">Fix caption</a>
            ${autoChecking(c) || isSent(p) ? '' : `<button class="btn small" data-one>${c.status === 'done' ? 'Re-run check' : 'Check this post'}</button>`}</div>
        </div></div></div>`;
    }).join('')}</div>
    <div class="row" style="margin-top:14px"><a class="btn" href="#/studio/b/${esc(id)}/caption">Back</a><span class="spacer"></span>
      <button class="btn primary" id="cont" ${settled ? '' : 'disabled'}>Continue to posting</button></div>`;
  // (each button is off while its request runs: a second tap would send it again)
  $('#runall').onclick = async e => {                     // posts being checked automatically are not checked twice
    const ids = b.posts.filter(p => p.final_image_url && !autoChecking(p.check) && !isSent(p)).map(p => p.id);   // (a post sent: no longer changes)
    if (!ids.length) return toast(b.posts.every(isSent) ? 'Every post is sent already.' : 'Already being checked automatically: this page updates by itself.');
    if (e.target.disabled) return;
    e.target.disabled = true;
    await run('check', ids).catch(err => { toast(err.message); e.target.disabled = false; }); redraw(); };
  $('#skip').onclick = async e => {
    if (e.target.disabled) return;
    e.target.disabled = true;
    const r = await api(`/api/studio/batches/${encodeURIComponent(id)}/skip-check`, { method: 'POST', body: {} }).catch(err => { toast(err.message); e.target.disabled = false; });
    if (r) { adopt(asked(), r.batch); go(`/studio/b/${id}/post`); } };
  document.querySelectorAll('[data-one]').forEach(btn => btn.onclick = async () => {
    if (btn.disabled) return;
    btn.disabled = true;
    await run('check', [btn.closest('.card').dataset.p]).catch(e => { toast(e.message); btn.disabled = false; }); redraw(); });
  $('#cont').onclick = () => go(`/studio/b/${id}/post`);
  follow(id, checkView);
}

// "This page posted this already": a post the page sent in the last 36 hours that shares names
// with this story (a note, never a block: editors may want a follow-up)
const NOT_NAMES = new Set(('The A An And But Or For Of On In At To With From After Before Over Into About Up Out As By This That These Those '
  + 'What Why How When Where Who Which His Her Their Its Our Your We They He She It You New Report Reports Breaking Watch Video Live Update '
  + 'Here Just Now Today Week Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April May June July August '
  + 'September October November December').split(' '));
const names = t => new Set((String(t || '').match(/\b[A-Z][A-Za-z'’.-]{2,}/g) || []).map(w => w.replace(/(['’]s|[.'’-])+$/, ''))
  .filter(w => w.length >= 3 && !NOT_NAMES.has(w)));
function postedLately(p, recent) {
  const mine = [...names(p.story.headline)];
  return (recent || []).find(r => {
    if (r.id.endsWith(`-studio-${p.id}`)) return false;              // this post itself
    const words = new Set(String(r.text || '').toLowerCase().match(/[a-z0-9]+/g) || []);
    const shared = mine.filter(w => words.has(w.toLowerCase()) || words.has(`${w.toLowerCase()}s`));
    return shared.length >= 2 && shared.some(w => w.length >= 5 && w !== w.toUpperCase());
  }) || null;
}

// A post whose last try failed in a way that does not prove it is missing (SocialPilot lost track of
// it, or the call broke off: review.html mayExist), and the questions asked before it is sent again,
// as on the review page. c: what the server asks to confirm ({facebook, note: the red note's text,
// caption_changed + old_caption: the caption that copy has, now_at: when "Post now" really goes out}).
// A copy made then waits in SocialPilot for its time (an hour or more), so it is not on the page yet.
const mayExist = err => /not delivered|interrupted|created but not|lookups? failed|error uploading media|timed? ?out|timeout/i.test(err || '');
const nowLabel = hint => (hint.now_at ? `(earliest free slot: ${when(hint.now_at)})` : `(earliest free slot, at least ${hint.min_lead_minutes || 60} min from now)`);
function lookedFirst(c) {
  if (c.facebook && !confirm('This post may already be on Facebook, or waiting in SocialPilot for its time (SocialPilot lost track of it, or the call broke off). '
      + (c.caption_changed ? `You changed the caption since, so the publisher's own check cannot find that copy: look for the old caption, “${c.old_caption}…”. ` : '')
      + 'Did you look in SocialPilot (Queue and Unscheduled) and on the page, and it is in neither?')) return false;
  if (c.note && !confirm(`This post has a red note: “${c.note}”. Did you do what it says in SocialPilot first (delete the copy)?`)) return false;
  if (c.now_at && !confirm(`Post now goes out at ${when(c.now_at)}, later than this page said: posts were queued for the page since. Send it for then?`)) return false;
  return true;
}

// 4.8 posting confirmation
async function postView(id, fromPoll) {
  const nav = S.nav;
  if (!fromPoll) await loadBatch(id);
  if (nav !== S.nav) return;                                         // they went to another screen meanwhile
  const redraw = again(postView, id);
  const b = S.batch;
  const page = S.pages.find(p => p.slug === b.page) || { name: b.page };
  let hint = { next: Date.now() + 3600e3, gap_minutes: 45 };
  // what the page sent lately (the "posted Xh ago" note) on the step's first draw only: it is up to
  // 30 drafts read, and a post being sent draws the step again every 20 s
  const recent = !fromPoll || !S.recent || S.recent.page !== b.page;
  try { hint = await api(`/api/studio/schedule-hint?page=${encodeURIComponent(b.page)}${recent ? '&recent=1' : ''}`); } catch (e) {}
  if (hint.recent) S.recent = { page: b.page, list: hint.recent };
  let sp = {};
  try { sp = (await api(`/api/stats?since=${Math.floor(Date.now() / 1000)}`)).publisher || {}; } catch (e) {}
  if (nav !== S.nav) return;                                         // they went to another screen meanwhile
  if (fromPoll && editing()) return drawWhenFree(S.follow);          // they started typing meanwhile
  const spWarn = sp.held ? `<div class="job err" style="margin-bottom:10px">⚠ ${sp.stale ? 'SocialPilot refused posts at the last try (it may be back)' : sp.locked ? 'The SocialPilot account is locked' : 'SocialPilot is unavailable'}: posts you send now wait and are scheduled automatically once it is back (tried every hour; still waiting ${sp.hold_hours || 12} hours after their time, they fail and you can send them again). Last error: ${esc(sp.error || 'unknown')}</div>`
    : sp.blocked ? `<div class="job err" style="margin-bottom:10px">⚠ ${sp.locked ? 'The SocialPilot account is locked' : 'SocialPilot is refusing posts'}: posts you send now will fail until ${sp.locked ? 'the account owner fixes it in SocialPilot (Settings → Billing / subscription)' : 'it is fixed'}. Last error: ${esc(sp.error || 'unknown')}</div>` : '';
  const local = ms => { const d = new Date(ms); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0, 16); };
  sentOver(b);
  let slot = hint.next;
  $('#main').innerHTML = stepper(id, 'post') + spWarn + `<div class="muted" style="margin-bottom:10px">Scheduled posts default to ${hint.gap_minutes} minutes after the latest post already queued for ${esc(page.name)}, each one after the previous. SocialPilot publishes them; nothing goes out without you pressing Send.</div>
    <div class="grid">${b.posts.map(p => {
      const pub = p.publish;
      const sent = pub && ['approved', 'publishing', 'scheduled', 'posted'].includes(pub.state);
      const at = slot; if (!sent) slot += hint.gap_minutes * 60e3;
      const mode = unsent(p.id, 'mode', 'schedule'), seen = !sent && postedLately(p, S.recent && S.recent.page === b.page ? S.recent.list : []);
      // the preview shows the text in the fields (what Send sends), as the editor types it
      const capNow = sent ? p.caption : typedOver(p.id, 'cap', p.caption), tagsNow = sent ? (p.hashtags || []).join(' ') : typedOver(p.id, 'tags', (p.hashtags || []).join(' '));
      const gone = pub && pub.state === 'rejected';                 // (withdrawn on the review page, or marked Not okay there)
      // data-visual: the visual this card shows; Send says so, and the server refuses another one
      return `<div class="card" data-p="${esc(p.id)}" data-visual="${esc(p.final_image_url || '')}"><div class="two">
        <div class="fb preview"><div class="head">${esc(page.name)}</div><div class="txt">${esc(capNow)}\n\n${esc(tagsNow)}</div>
          ${p.final_image_url ? `<img src="${esc(p.final_image_url)}" alt="" onclick="zoom('${esc(p.final_image_url)}')">` : ''}</div>
        <div class="grid"><h3>${esc(p.story.headline)}</h3>${lineOf(p)}
          ${sent ? `<div><span class="pill ${pub.state === 'posted' ? 'ok' : 'warn'}">${pub.state === 'posted' ? 'Posted' : pub.state === 'scheduled' ? 'Scheduled' : pub.held ? 'Waiting for SocialPilot' : 'Sending to SocialPilot'}</span>
                     ${pub.scheduled_for ? ' for ' + when(Date.parse(pub.scheduled_for)) : pub.requested_at ? ' requested for ' + when(pub.requested_at) : ''}</div>`
          : `${pub && pub.state === 'failed' ? `<div class="job err">SocialPilot error: ${esc(pub.error || 'failed')}. Fix and send again.</div>` : ''}
            ${pub && pub.state === 'failed' && mayExist(pub.error) ? '<div class="job err"><b>It may already be on Facebook.</b> Look on the page before you retry it, or it could go out twice.</div>' : ''}
            ${gone ? `<div class="job err" data-withdrawn>${pub.withdrawn ? 'Withdrawn' : 'Marked Not okay'}${pub.by ? ` by ${esc(pub.by)}` : ''} on the review page${pub.reason && !/^withdrawn$/i.test(pub.reason) ? `: ${esc(pub.reason)}` : ''}, so it was not posted. Send posts it again.</div>` : ''}
            ${seen ? `<div class="job" data-seen>This page ${seen.status === 'posted' ? 'posted' : 'sent'} a similar story ${esc(ago(seen.at))}: “${esc(seen.headline)}”. Only a note: send it if this one is new.</div>` : ''}
            <label class="muted">Caption</label><textarea rows="6" data-cap data-keep="cap">${esc(capNow)}</textarea>
            <div class="muted count" data-count></div>
            <label class="muted">Hashtags</label><input type="text" data-tags data-keep="tags" value="${esc(tagsNow)}">
            <a href="#/studio/b/${esc(id)}/visual" class="muted">Edit image</a>
            <div class="row"><label><input type="radio" name="m-${esc(p.id)}" value="schedule" data-keep="mode" ${mode === 'schedule' ? 'checked' : ''}> Schedule</label>
              <input type="datetime-local" data-at data-keep="at" value="${esc(unsent(p.id, 'at', local(at)))}">
              <label><input type="radio" name="m-${esc(p.id)}" value="now" data-keep="mode" ${mode === 'now' ? 'checked' : ''}> Post now <span class="muted" data-now="${esc(hint.now_at || '')}">${esc(nowLabel(hint))}</span></label></div>
            <div data-novisual style="display:contents">${noVisual(p, p.final_image_url)}</div>
            ${S.lostSend.has(p.id) ? `<div class="job err" data-lost>${esc(S.lostSend.get(p.id))}</div>` : ''}
            <div class="row"><button class="btn ok" data-send ${composing(p) || !p.final_image_url ? 'disabled' : ''}>${pub && pub.state === 'failed' ? 'Retry' : 'Send'}</button></div>`}
        </div></div></div>`;
    }).join('')}</div>
    <div class="row" style="margin-top:14px"><a class="btn" href="#/studio/b/${esc(id)}/check">Back</a><span class="spacer"></span><a class="btn" href="#/studio">Done</a></div>`;
  keepTyping();
  document.querySelectorAll('.card').forEach(card => {
    const pid = card.dataset.p;
    const cap = card.querySelector('[data-cap]');
    if (!cap) return;
    autosave(card, pid);
    const tags = card.querySelector('[data-tags]'), txt = card.querySelector('.preview .txt');
    [cap, tags].forEach(el => el.addEventListener('input', () => { txt.textContent = `${cap.value}\n\n${tags.value}`; }));
    card.querySelector('[data-send]').onclick = async e => {
      e.target.disabled = true; e.target.dataset.busy = '1';
      S.lostSend.delete(pid); card.querySelector('[data-lost]')?.remove();
      const done = () => { e.target.disabled = false; delete e.target.dataset.busy; };
      const mode = card.querySelector(`input[name="m-${pid}"]:checked`).value;
      const at = new Date(card.querySelector('[data-at]').value).getTime();
      // the time the page suggested, left as it was: the server moves it later if the hour has
      // started running out since the page was drawn (never sooner)
      const asDrawn = !S.unsent.has(`${pid}:at`);
      // the time "Post now" said: the server asks first if it goes out later (the page's queue filled)
      const label = card.querySelector('[data-now]'), shown = Number(label.dataset.now) || 0;
      const send = confirmed => api(`/api/studio/batches/${encodeURIComponent(id)}/publish`, { method: 'POST',
        body: { post_id: pid, mode, at, default: asDrawn, ...(mode === 'now' && shown ? { shown_at: shown } : {}), ...(confirmed ? { confirmed: true } : {}),
                // what this card shows: a visual made, or a caption saved elsewhere, since it was drawn is refused
                visual: card.dataset.visual, caption: cap.value, hashtags: tags.value } });
      try { await S.saving;                                          // (a save on its way lands first)
            if (S.clash.has(`${pid}:cap`) || S.clash.has(`${pid}:tags`)) {
              toast(`${[`${pid}:cap`, `${pid}:tags`].some(k => S.clash.has(k) && S.clashBy.get(k) === 'visual') ? 'The new visual came with its own caption'
                : 'The caption was changed elsewhere'} while you typed: choose yours or theirs below it first`); done(); return;
            }
            // only what the editor typed here is saved first, over the text it was typed over: a
            // caption saved in another window since this card was drawn is never overwritten unseen
            const typed = [['cap', 'caption', cap], ['tags', 'hashtags', tags]].filter(([f]) => S.unsent.has(`${pid}:${f}`));
            if (typed.length) await patchPost(pid, Object.fromEntries(typed.flatMap(([f, field, el]) => [[field, el.value], [`base_${field}`, S.bases.get(`${pid}:${f}`)]])), id);
            typed.forEach(([f, , el]) => S.bases.set(`${pid}:${f}`, el.value));
            ['cap', 'tags'].forEach(f => S.unsent.delete(`${pid}:${f}`));
            // a post whose last try may have reached SocialPilot or Facebook, or has a red note: the
            // server asks first what the review page asks before a retry
            const r = await send(false).catch(async err => {
              const c = err.body && err.body.confirm;
              if (!c) throw err;
              if (c.now_at) document.querySelectorAll('#main [data-now]').forEach(l => { l.dataset.now = c.now_at; l.textContent = nowLabel({ now_at: c.now_at }); });
              if (!lookedFirst(c)) return null;
              return send(true);
            });
            if (!r) { done(); return; }
            adopt(asked(), r.batch);
            ['at', 'mode'].forEach(f => S.unsent.delete(`${pid}:${f}`));
            const x = r.batch.posts.find(y => y.id === pid), t = x && x.publish && x.publish.requested_at;
            toast(t ? `Sent: scheduled for ${when(t)}${mode === 'now' ? ' (the earliest allowed time)' : ''}` : 'Sent');
            redraw(); }
      catch (err) {
        // the answer was lost (the connection dropped, or the request was cut off): it may have been
        // sent; the page finds out before Send can be pressed again, and says what it found
        if (err.offline || err.lost) {
          toast(err.offline ? 'Connection lost: checking whether it was sent…' : 'The dashboard did not answer in time: checking whether it was sent…');
          // a Send the server is still making the draft for ('sending'): read again until it is sent
          // or no longer under way (a Send cut short is left 'sending': after SEND_MS it is not)
          const lostAt = Date.now(), still = x => !!(x && x.publish && ['sending', 'pending'].includes(x.publish.state)) && Date.now() - lostAt < SEND_MS;
          let x, told = false;
          for (let wait = 3000; nav === S.nav; ) {
            try { await loadBatch(id); x = S.batch.posts.find(y => y.id === pid); if (isSent(x) || !still(x)) break; }
            catch (e2) { wait = Math.min(30e3, wait * 2); }
            if (still(x) && !told) {
              told = true;
              S.lostSend.set(pid, 'It may still be on its way: this page shows when it is sent. Do not make this story again.');
              card.querySelector('[data-send]').insertAdjacentHTML('beforebegin', `<div class="job" data-lost>${esc(S.lostSend.get(pid))}</div>`);
              toast(S.lostSend.get(pid));
            }
            await new Promise(ok => setTimeout(ok, wait));
          }
          if (nav !== S.nav) return;
          // (the answer on the card too: the toast is gone in seconds, perhaps before the editor looks)
          if (isSent(x)) S.lostSend.delete(pid); else S.lostSend.set(pid, `Not sent: the ${err.offline ? 'connection dropped' : 'dashboard did not answer in time'}. Press Send again.`);
          toast(isSent(x) ? (x.publish.requested_at ? `It was sent: scheduled for ${when(x.publish.requested_at)}` : 'It was sent') : 'It was not sent: press Send again');
          delete e.target.dataset.busy; redraw(); return;
        }
        toast(err.message); done();
        if (err.body && err.body.now) { clashed(pid, err.body.now); markUnsaved(); }   // (typed over a caption saved elsewhere)
        // its draft was sent again on the review page meanwhile, or the post changed since this card
        // was drawn (a visual made, a caption saved elsewhere): show it as it is now
        if (/^already sent/.test(err.message) || (err.body && err.body.changed)) { await loadBatch(id).catch(() => {}); redraw(); }
      }
    };
  });
  follow(id, postView);                                              // while a post is being scheduled (pollMs)
}

// ------------------------------------------------------------------ admin
function adminTabs(cur) {
  return `<div class="steps">${[['users', 'Users'], ['pages', 'Pages'], ['templates', 'Templates'], ['health', 'Health'], ['audit', 'Audit log'], ['usage', 'Usage']].map(([k, l]) =>
    `<a href="#/admin/${k}" class="${k === cur ? 'on' : ''}">${l}</a>`).join('')}</div>`;
}
// a button is off while its request runs: a second click would send it twice (a second reset link
// kills the first, a second invite answers "that user already exists")
async function oneAtATime(btn, fn) {
  if (btn.disabled) return;
  btn.disabled = true;
  try { await fn(); } finally { btn.disabled = false; }
}
async function adminUsers() {
  const nav = S.nav;
  const [{ users }, { pages }] = await Promise.all([api('/api/admin/users'), api('/api/admin/pages')]);
  if (nav !== S.nav) return;                                         // they moved on meanwhile
  const shown = pages.filter(p => !p.archived);      // archived pages are kept on the server, not offered here
  const pageOpts = sel => shown.map(p => `<label style="margin-right:10px"><input type="checkbox" value="${esc(p.slug)}" ${sel.includes(p.slug) ? 'checked' : ''}> ${esc(p.name)}</label>`).join('');
  const rowHtml = u => `<tr data-u="${esc(u.id)}">
      <td>${esc(u.email)}<div class="muted">${esc(u.name)}${u.active ? '' : ' · <span class="pill no">deactivated</span>'}${u.locked ? ' · <span class="pill warn">locked</span>' : ''}${u.reset_requested ? ` · asked for a reset ${ago(u.reset_requested)}` : ''}</div></td>
      <td><select data-role><option value="editor" ${u.role === 'editor' ? 'selected' : ''}>Editor</option><option value="owner" ${u.role === 'owner' ? 'selected' : ''}>Owner</option><option value="super" ${u.role === 'super' ? 'selected' : ''}>Admin</option></select></td>
      <td data-pages>${pageOpts(u.pages)}</td><td class="muted">${u.last_login ? ago(u.last_login) : 'never'}</td>
      <td><button class="btn small" data-save>Save</button> <button class="btn small" data-reset>Reset link</button> <button class="btn small ${u.active ? 'danger' : ''}" data-act>${u.active ? 'Deactivate' : 'Activate'}</button></td></tr>`;
  $('#main').innerHTML = adminTabs('users') + `<div class="grid">
    <form class="panel grid" id="inv"><h3>Invite a user</h3><div class="row"><input type="email" name="email" placeholder="Email" required><input type="text" name="name" placeholder="Name">
      <select name="role"><option value="editor">Editor</option><option value="super">Admin</option></select></div>
      <div>${pageOpts([])}</div><div class="row"><button class="btn primary">Invite</button></div><div id="invout"></div></form>
    <div class="panel"><div class="adminscroll"><table id="users"><tr><th>User</th><th>Role</th><th>Pages</th><th>Last sign-in</th><th></th></tr>${users.map(rowHtml).join('')}</table></div></div></div>`;
  // one row drawn again (the others keep what the admin ticked and has not saved yet)
  // (`sent`: what was just saved from it; a box ticked or the role changed while that save was under
  // way stays as the admin set it, still to be saved)
  const draw = (u, tr, sent = { pages: {} }) => {
    const t = document.createElement('tbody'); t.innerHTML = rowHtml(u);
    const row = t.firstElementChild;
    if (tr) {
      tr.querySelectorAll('[data-pages] input[data-dirty]').forEach(c => {
        const n = [...row.querySelectorAll('[data-pages] input')].find(x => x.value === c.value);
        if (n && n.checked !== c.checked && sent.pages[c.value] !== c.checked) { n.checked = c.checked; n.dataset.dirty = '1'; }
      });
      const was = tr.querySelector('[data-role]'), now = row.querySelector('[data-role]');
      if (was.dataset.dirty && was.value !== now.value && sent.role !== was.value) { now.value = was.value; now.dataset.dirty = '1'; }
      tr.replaceWith(row);
    } else $('#users tbody').appendChild(row);
    wire(row);
  };
  $('#inv').onsubmit = e => {
    e.preventDefault(); const f = e.target;
    oneAtATime(f.querySelector('button.primary'), async () => {
      try { const r = await api('/api/admin/users', { method: 'POST', body: { email: f.email.value, name: f.name.value, role: f.role.value,
              pages: [...f.querySelectorAll('input[type=checkbox]:checked')].map(c => c.value) } });
            // the link stays until the admin leaves this tab: it cannot be shown again
            $('#invout').innerHTML = `<div class="muted">${r.emailed ? 'Invitation emailed. ' : ''}Send this one-time link to ${esc(r.user.email)} (valid 48 hours):</div><div class="linkbox">${esc(r.setup_link)}</div>`;
            const nu = { ...r.user, last_login: null, reset_requested: null, locked: false };
            users.push(nu); draw(nu); f.email.value = ''; f.name.value = ''; } catch (err) { toast(err.message); }
    });
  };
  function wire(tr) {
    const id = tr.dataset.u, me = () => users.find(x => x.id === id);
    tr.querySelector('[data-save]').onclick = e => oneAtATime(e.target, async () => {
      const u = me(), role = tr.querySelector('[data-role]');
      // only what was changed in this row (the role, each page ticked or unticked) and differs from how
      // the row was drawn: what another admin changed meanwhile is kept, and the row shows it once saved
      const boxes = [...tr.querySelectorAll('[data-pages] input[data-dirty]')].filter(c => c.checked !== u.pages.includes(c.value));
      const body = {}, add = boxes.filter(c => c.checked).map(c => c.value), remove = boxes.filter(c => !c.checked).map(c => c.value);
      if (role.dataset.dirty && role.value !== u.role) body.role = role.value;
      if (add.length) body.pages_add = add;
      if (remove.length) body.pages_remove = remove;
      if (!Object.keys(body).length) return toast('Nothing to save: nothing was changed in this row');
      const sent = { pages: Object.fromEntries(boxes.map(c => [c.value, c.checked])), role: body.role };
      try { const r = await api(`/api/admin/users/${id}`, { method: 'PATCH', body }); Object.assign(u, r.user); draw(u, tr, sent); toast('Saved'); }
      catch (err) { role.value = u.role; delete role.dataset.dirty; toast(err.message); }
    });
    tr.querySelector('[data-reset]').onclick = e => oneAtATime(e.target, async () => {
      try { const r = await api(`/api/admin/users/${id}/reset`, { method: 'POST', body: {} });
            prompt(r.emailed ? 'Emailed. The one-time link (48 hours):' : 'Send this one-time link (48 hours):', r.setup_link); } catch (err) { toast(err.message); }
    });
    tr.querySelector('[data-act]').onclick = e => oneAtATime(e.target, async () => {
      const u = me();
      if (u.active && !confirm(`Deactivate ${u.email}? They are signed out at once.`)) return;
      try { const r = await api(`/api/admin/users/${id}`, { method: 'PATCH', body: { active: !u.active } }); Object.assign(u, r.user); draw(u, tr); }
      catch (err) { toast(err.message); }
    });
  }
  document.querySelectorAll('tr[data-u]').forEach(wire);
}
async function adminPages() {
  const nav = S.nav;
  const { pages } = await api('/api/admin/pages');
  if (nav !== S.nav) return;
  $('#main').innerHTML = adminTabs('pages') + `<div class="grid">
    <form class="panel grid" id="add"><h3>Add a Facebook page</h3><div class="row"><input type="text" name="name" placeholder="Page name" required>
      <input type="text" name="fb_page_id" placeholder="Facebook page ID"><input type="text" name="socialpilot_login_id" placeholder="SocialPilot account (loginId)">
      <input type="text" name="timezone" placeholder="Time zone (e.g. America/New_York)" list="tzlist">${zoneList()}</div><div class="row"><button class="btn primary">Add page</button></div></form>
    <div class="panel"><div class="adminscroll"><table><tr><th>Page</th><th>SocialPilot</th><th>Sources</th><th>Reference documents</th><th></th></tr>${pages.map(p => `<tr>
      <td><b>${esc(p.name)}</b> ${p.archived ? '<span class="pill">archived</span>' : ''}<div class="muted">${esc(p.slug)} · FB ${esc(p.fb_page_id || '—')}</div></td>
      <td>${esc(p.socialpilot_login_id || '—')}</td><td>${p.source_count}</td>
      <td>${Object.entries(p.docs).map(([t, d]) => { const a = d.versions.find(v => v.v === d.active);
        return `<div class="muted">${esc(t)}: ${d.active ? 'v' + d.active : '<span class="pill warn">missing</span>'}${a && a.image_only ? ' <span class="pill warn">almost no text</span>' : ''}</div>`; }).join('')}</td>
      <td><a class="btn small" href="#/admin/page/${esc(p.slug)}">Manage</a></td></tr>`).join('')}</table></div></div></div>`;
  $('#add').onsubmit = e => { e.preventDefault(); const f = e.target;
    oneAtATime(f.querySelector('button.primary'), async () => {
      try { const r = await api('/api/admin/pages', { method: 'POST', body: { name: f.name.value, fb_page_id: f.fb_page_id.value, socialpilot_login_id: f.socialpilot_login_id.value, timezone: f.timezone.value } });
            await loadMe(); header(); go(`/admin/page/${r.page.slug}`); } catch (err) { toast(err.message); } }); };   // in the page picker at once
}
// the time zones to pick from (the server stores old names as their new one)
function zoneList() {
  let zones = [];
  try { zones = Intl.supportedValuesOf('timeZone'); } catch (e) {}
  return `<datalist id="tzlist">${['UTC', ...zones].map(z => `<option value="${esc(z)}">`).join('')}</datalist>`;
}
// Posts per day as the routine makes them: config.yaml posting.daily_cap (20), the same for every
// page. The stored per-page field (8 for both pages, the old default) is used by nothing, so it is
// shown read-only until the owner decides what it should mean (wiring it up as it is would cut
// both pages to 8 a day).
const DAILY_POSTS = 20;
async function adminPage(slug) {
  const nav = S.nav;
  const [{ pages, doc_types, doc_max_bytes }, src] = await Promise.all([api('/api/admin/pages'), api(`/api/admin/pages/${slug}/sources`)]);
  if (nav !== S.nav || location.hash.replace(/\/+$/, '') !== `#/admin/page/${slug}`) return;     // (also when drawn again after a change)
  const p = pages.find(x => x.slug === slug);
  if (!p) return ($('#main').innerHTML = '<div class="empty">No such page</div>');
  const th = p.theme || {};
  $('#main').innerHTML = adminTabs('pages') + `<h2>${esc(p.name)}</h2><div class="grid">
    <form class="panel grid" id="set"><h3>Settings</h3><div class="row">
      <label class="muted">Name <input type="text" name="name" value="${esc(p.name)}"></label>
      <label class="muted">Facebook page ID <input type="text" name="fb_page_id" value="${esc(p.fb_page_id)}"></label>
      <label class="muted">SocialPilot loginId <input type="text" name="socialpilot_login_id" value="${esc(p.socialpilot_login_id)}"></label>
      <label class="muted">Time zone <input type="text" name="timezone" value="${esc(p.timezone)}" list="tzlist"></label>${zoneList()}
      <span class="muted" title="Set in the routine's settings (config.yaml posting.daily_cap) for every page; the owner decides what a per-page setting should mean">Posts per day: <b>${DAILY_POSTS}</b> (the hourly routine's cap for every page; not changed here)</span></div>
      <div class="row"><label class="muted">Accent <input type="color" name="accent" value="${esc(th.accent || '#ffe01b')}"></label>
        <label class="muted">Background <input type="color" name="bg" value="${esc(th.bg || '#0a0a0a')}"></label>
        <label class="muted">Text <input type="color" name="ink" value="${esc(th.ink || '#ffffff')}"></label>
        <label class="muted">Logo text <input type="text" name="logo_text" value="${esc(th.logo_text || '')}" placeholder="shown on posts"></label></div>
      <div class="row"><button class="btn primary">Save settings</button><span class="spacer"></span>
        <button type="button" class="btn ${p.archived ? '' : 'danger'}" id="arch">${p.archived ? 'Restore page' : 'Archive page'}</button></div></form>
    <div class="panel grid"><h3>News sources</h3><div class="muted">Only admins see this list. Websites (searched as site:domain), X accounts (https://x.com/handle), subreddits (https://www.reddit.com/r/name/) and web searches (type <b>search</b>, e.g. <i>nfl injury news today</i>). Collected every hour.</div>
      <table><tr><th>URL</th><th>Type</th><th>Label</th><th>Active</th><th></th></tr>${src.sources.map(s => `<tr data-s="${esc(s.id)}"><td style="word-break:break-all">${esc(s.url)}</td><td>${esc(s.type)}</td>
        <td>${esc(s.label || '')}</td><td><input type="checkbox" data-active ${s.active !== false ? 'checked' : ''}></td><td><button class="btn small danger" data-del>Remove</button></td></tr>`).join('')}</table>
      <form class="row" id="addsrc"><input type="text" name="url" placeholder="https://… or search words" required style="flex:1"><select name="type">${src.types.map(t => `<option>${t}</option>`).join('')}</select>
        <input type="text" name="label" placeholder="Label"><button class="btn">Add source</button></form></div>
    <div class="panel grid"><h3>Reference documents</h3><div class="muted">PDFs up to ${fmtBytes(doc_max_bytes || 50e6)}, sent in parts. Stored encrypted; only the server and the routines read them (the routines get the text and a picture of each page). Earlier versions are kept for rollback; nobody can download them here.</div>
      ${Object.entries(doc_types).map(([t, label]) => { const d = p.docs[t], key = `${slug}/${t}`;
        return `<div class="card" data-t="${esc(t)}" data-doc="${esc(key)}"><div class="row"><b>${esc(label)}</b><span class="spacer"></span>
          <label class="btn small" ${docBusy(key) ? 'style="opacity:.5;pointer-events:none"' : ''}>Upload new version<input type="file" accept="application/pdf,.pdf" hidden data-up ${docBusy(key) ? 'disabled' : ''}></label></div>
          <div data-docnote>${docNoteHtml(key)}</div>
          ${d.versions.length ? `<div class="adminscroll"><table><tr><th>Version</th><th>File</th><th>Size</th><th>Pages</th><th>Text read</th><th>Uploaded</th><th></th></tr>${d.versions.slice().reverse().map(v => `<tr><td>v${v.v} ${v.v === d.active ? '<span class="pill ok">active</span>' : ''}</td>
            <td>${esc(v.filename)}</td><td>${fmtBytes(v.size)}</td><td>${v.pages || '?'}</td><td>${docTextCell(v)}</td><td class="muted wrap">${when(v.at)} · ${esc(v.by)}</td>
            <td>${v.v === d.active ? '' : `<button class="btn small" data-roll="${v.v}" ${docBusy(key) ? 'disabled' : ''}>Make active</button>`}</td></tr>`).join('')}</table></div>` : '<div class="muted">Not uploaded yet.</div>'}</div>`; }).join('')}</div></div>`;
  const FIELDS = ['name', 'fb_page_id', 'socialpilot_login_id', 'timezone'], THEME = { accent: '#ffe01b', bg: '#0a0a0a', ink: '#ffffff', logo_text: '' };
  // each field as drawn (as its input shows it: a colour as #rrggbb): one typed in and put back is not a change
  const drawn = {}, asShown = (el, v) => { const i = document.createElement('input'); i.type = el.type; i.value = v; return i.value; };
  for (const k of [...FIELDS, ...Object.keys(THEME)]) drawn[k] = $('#set')[k].value;
  $('#set').onsubmit = e => { e.preventDefault(); const f = e.target;
    oneAtATime(f.querySelector('button.primary'), async () => {
      // only the fields the admin changed here: another admin's change to the others is kept
      const body = {}, theme = {};
      for (const k of [...FIELDS, ...Object.keys(THEME)]) if (f[k].value === drawn[k]) delete f[k].dataset.dirty;
      for (const k of FIELDS) if (f[k].dataset.dirty) body[k] = f[k].value;
      for (const k of Object.keys(THEME)) if (f[k].dataset.dirty) theme[k] = f[k].value;
      if (Object.keys(theme).length) body.theme = theme;
      if (!Object.keys(body).length) return toast('Nothing to save: no setting was changed');
      try { const r = await api(`/api/admin/pages/${slug}`, { method: 'PATCH', body });
            Object.assign(p, r.page); Object.assign(th, r.page.theme);
            if (f.isConnected) {
              $('#main h2').textContent = r.page.name;
              // the form shows what is stored now (another admin's change too, a name cut to length),
              // except a field typed in again while this was saved
              for (const k of [...FIELDS, ...Object.keys(THEME)]) {
                const sent = k in THEME ? theme[k] : body[k];
                if (sent !== undefined && f[k].value === sent) delete f[k].dataset.dirty;
                const now = (k in THEME ? (r.page.theme || {})[k] || THEME[k] : r.page[k]) || '';
                if (!f[k].dataset.dirty) f[k].value = now;
                drawn[k] = asShown(f[k], now);
              }
            }
            toast('Saved'); await loadMe(); header(); } catch (err) { toast(err.message); } }); };
  $('#arch').onclick = e => oneAtATime(e.target, async () => { if (!confirm(p.archived ? 'Restore this page?' : 'Archive this page? Editors will no longer see it.')) return;
    await api(`/api/admin/pages/${slug}`, { method: 'PATCH', body: { archived: !p.archived } }).catch(e => toast(e.message)); await loadMe(); header(); await adminPage(slug); });
  $('#addsrc').onsubmit = e => { e.preventDefault(); const f = e.target;
    oneAtATime(f.querySelector('button'), async () => {   // (one click, one source: a second would add it twice)
      try { await api(`/api/admin/pages/${slug}/sources`, { method: 'POST', body: { url: f.url.value, type: f.type.value, label: f.label.value } }); await adminPage(slug); }
      catch (err) { toast(err.message); } }); };
  document.querySelectorAll('tr[data-s]').forEach(tr => {
    const sid = tr.dataset.s;
    tr.querySelector('[data-active]').onchange = e => api(`/api/admin/pages/${slug}/sources/${sid}`, { method: 'PATCH', body: { active: e.target.checked } }).then(() => toast('Saved')).catch(err => toast(err.message));
    tr.querySelector('[data-del]').onclick = e => oneAtATime(e.target, async () => { if (confirm('Remove this source?')) { await api(`/api/admin/pages/${slug}/sources/${sid}`, { method: 'DELETE', body: {} }).catch(e => toast(e.message)); await adminPage(slug); } });
  });
  document.querySelectorAll('.card[data-t]').forEach(card => {
    const t = card.dataset.t, key = card.dataset.doc;
    card.querySelector('[data-up]').onchange = e => {
      const file = e.target.files[0]; e.target.value = ''; if (!file || docBusy(key)) return;
      const d = p.docs[t], before = d.versions.find(v => v.v === d.active);
      uploadDoc(slug, t, file, before).finally(() => { if (location.hash === `#/admin/page/${slug}`) adminPage(slug); });
    };
    wireDocNote(key);
    card.querySelectorAll('[data-roll]').forEach(b => b.onclick = () => oneAtATime(b, async () => {
      // an upload under way makes its own version active when it finishes: its note and lock stay
      if (docBusy(key)) return toast('Wait until the upload finishes: it makes its own version active');
      const r = await api(`/api/admin/pages/${slug}/docs/${t}`, { method: 'PATCH', body: { active: Number(b.dataset.roll) } }).catch(e => { toast(e.message); return null; });
      // as after an upload: its templates are converted (or could not be queued: say so)
      if (r && !docBusy(key)) DOC_NOTES.set(key, { kind: r.conversion && !r.conversion.job ? 'warn' : 'ok', text: `v${b.dataset.roll} is now active.${r.conversion
        ? ` ${r.conversion.job ? DOC_CONVERTING : r.conversion.error}` : ''}` });
      await adminPage(slug); }));
  });
  await Promise.all([sheetPanel(slug), lessonsPanel(slug)]);
}

// ------------------------------------------------------------------ reference documents (admins)
// A document goes up in parts (start, part..., finish: routes/admin.js): one request carries at most
// 4.5 MB and template books run to 40 MB. What happened stays on the document's card until the next
// upload or Close: the old 3-second toast left nobody knowing the Oct 23 upload never arrived.
const DOC_NOTES = new Map();          // `${slug}/${type}` -> {kind: busy | ok | warn | err, text, done, total}
const fmtBytes = n => n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round((n || 0) / 1e3))} KB`;
const docBusy = key => (DOC_NOTES.get(key) || {}).kind === 'busy';
// what is lost when a document's text cannot be read (the routines get page pictures of the first two: bore/studio.py)
const DOC_READS = { templates: 'The routines also see each page as a picture, but the ideas step reads the templates\' names and word limits from the text',
                    visual: 'The routines also see each page as a picture, but the rules for the look and the photos are read from the text',
                    tone: 'Captions are written from this document\'s text (the routines get no pictures of it)' };
const DOC_CONVERTING = 'The Studio routine converts its templates now: they appear under Templates for the page\'s editors to approve.';
const NOTE_STYLE = { ok: 'style="background:var(--ok-bg);color:var(--ok)"', warn: '', err: '' };
function docNoteHtml(key) {
  const n = DOC_NOTES.get(key);
  if (!n) return '';
  if (n.kind === 'busy') return `<div class="job" style="background:var(--soft);color:inherit"><progress max="${n.total || 1}" value="${n.done || 0}" style="width:100%"></progress>
    <div>${esc(n.text)}</div><div class="muted">Keep this tab open until it finishes.</div></div>`;
  return `<div class="job ${n.kind === 'err' ? 'err' : ''}" ${NOTE_STYLE[n.kind] || ''} role="${n.kind === 'err' ? 'alert' : 'status'}">
    ${n.kind === 'ok' ? '✓' : '⚠'} ${esc(n.text)} <button type="button" class="btn small" data-docclose>Close</button></div>`;
}
function docTextCell(v) {
  if (v.warning) return `<span class="pill warn" title="${esc(v.warning)}">text not read</span>`;
  if (v.chars == null) return '<span class="muted" title="uploaded before this was measured">?</span>';
  return `${v.chars.toLocaleString()} characters${v.image_only ? ' <span class="pill warn" title="the pages are pictures: the ideas step reads almost nothing of them">almost no text</span>' : ''}`;
}
function wireDocNote(key) {
  const box = document.querySelector(`[data-doc="${CSS.escape(key)}"] [data-docnote]`);
  const close = box && box.querySelector('[data-docclose]');
  if (close) close.onclick = () => { DOC_NOTES.delete(key); drawDocNote(key); };
}
function drawDocNote(key) {           // the card may be gone (the admin moved on): the note waits for the next render
  const card = document.querySelector(`[data-doc="${CSS.escape(key)}"]`);
  if (!card) return;
  card.querySelector('[data-docnote]').innerHTML = docNoteHtml(key);
  const up = card.querySelector('[data-up]'), busy = docBusy(key);
  up.disabled = busy;
  Object.assign(up.closest('label').style, { opacity: busy ? '.5' : '', pointerEvents: busy ? 'none' : '' });
  card.querySelectorAll('[data-roll]').forEach(b => { b.disabled = busy; });
  wireDocNote(key);
}
window.addEventListener('beforeunload', e => { if ([...DOC_NOTES.values()].some(n => n.kind === 'busy')) { e.preventDefault(); e.returnValue = ''; } });

const blobToB64 = blob => new Promise((resolve, reject) => {
  const fr = new FileReader();
  fr.onload = () => resolve(String(fr.result).split(',')[1] || '');
  fr.onerror = () => reject(new Error('the browser could not read the file: pick it again'));
  fr.readAsDataURL(blob);
});
async function fileSha256(file) {     // the server checks the joined parts against it
  if (!(window.crypto && crypto.subtle)) return '';
  const h = new Uint8Array(await crypto.subtle.digest('SHA-256', await file.arrayBuffer()));
  return [...h].map(b => b.toString(16).padStart(2, '0')).join('');
}
// a dropped connection or a busy server is tried again (after 1, 2 and 4 s); a refusal is not
async function docRetry(what, fn) {
  for (let i = 0; ; i++) {
    try { return await fn(); } catch (e) {
      const transient = !e.status || e.status >= 500 || e.status === 429;
      if (!transient || i >= 3) throw Object.assign(e, { what, transient });
      await new Promise(r => setTimeout(r, 1000 * 2 ** i));
    }
  }
}
async function uploadDoc(slug, t, file, before) {
  const key = `${slug}/${t}`, base = `/api/admin/pages/${slug}/docs/${t}`;
  const note = n => { DOC_NOTES.set(key, n); drawDocNote(key); };
  const kept = before ? `: v${before.v} is still the active version` : '';
  try {
    note({ kind: 'busy', text: `Checking ${file.name}…`, done: 0, total: 1 });
    if (await file.slice(0, 5).text() !== '%PDF-') throw new Error('that file is not a PDF: export the document as a PDF and upload that');
    const st = await docRetry('starting the upload', () => api(`${base}/start`, { method: 'POST', body: { filename: file.name, size: file.size } }));
    const sha256 = await fileSha256(file).catch(() => '');
    for (let i = 0; i < st.parts; i++) {
      note({ kind: 'busy', text: `Sending ${file.name}: part ${i + 1} of ${st.parts} (${i ? fmtBytes(i * st.part_bytes) : '0 KB'} of ${fmtBytes(file.size)} sent)`, done: i, total: st.parts + 1 });
      const data_b64 = await blobToB64(file.slice(i * st.part_bytes, (i + 1) * st.part_bytes));
      await docRetry(`sending part ${i + 1} of ${st.parts}`, () => api(`${base}/part`, { method: 'POST', body: { upload: st.upload, index: i, data_b64 } }));
    }
    note({ kind: 'busy', text: `All ${fmtBytes(file.size)} sent: joining the parts and reading the text…`, done: st.parts, total: st.parts + 1 });
    const r = await docRetry('reading the text', () => api(`${base}/finish`, { method: 'POST',
      body: { upload: st.upload, parts: st.parts, size: file.size, sha256, filename: file.name } }));
    const v = r.version, what = `${file.name} is uploaded as v${v.v} and is now active: ${v.pages || '?'} page${v.pages === 1 ? '' : 's'}, ${fmtBytes(v.size)}`;
    const back = before ? ` Or make v${before.v} active again below.` : '';
    if (v.warning) note({ kind: 'warn', text: `${what}, but its text could not be read (${v.warning}). ${DOC_READS[t]}: export the PDF again from the design tool and upload it.${back}` });
    else if (v.image_only) note({ kind: 'warn', text: `${what}, but almost no text could be read from it (${v.chars} characters): its pages are pictures (the text was flattened or outlined when it was exported). ${DOC_READS[t]}: export it again as a PDF with live text and upload that.${back}` });
    else note({ kind: 'ok', text: `${what}, ${v.chars.toLocaleString()} characters of text read.` });
    // its templates are converted for the editors to approve (routes/templates.js)
    if (r.conversion) { const n = DOC_NOTES.get(key); note({ ...n, text: `${n.text} ${r.conversion.job ? DOC_CONVERTING : r.conversion.error}` }); }
  } catch (e) {
    const why = e.transient ? `${e.what} failed 4 times (${e.message}): check the internet connection, then upload the file again` : e.message;
    note({ kind: 'err', text: `${file.name} was not uploaded: ${why}. Nothing was changed${kept}.` });
  }
}

// ------------------------------------------------------------------ learned from reviews (admins)
async function lessonsPanel(slug) {
  const box = document.createElement('div');
  box.className = 'panel grid'; box.id = 'lessons';
  $('#main .grid').appendChild(box);
  let rec;
  try { rec = (await api(`/api/admin/pages/${slug}/lessons`)).lessons; } catch (e) { box.textContent = e.message; return; }
  if (!box.isConnected) return;                                      // (the screen was drawn again meanwhile)
  const q = sel => box.querySelector(sel);
  const pct = x => x == null ? '–' : Math.round(x * 100) + '%';
  const kinds = Object.entries((rec && rec.kinds) || {}).sort((a, b) => (b[1].n || 0) - (a[1].n || 0));
  const ov = { ...((rec && rec.overrides) || {}) };
  box.innerHTML = `<h3>Learned from reviews</h3>
    <div class="muted">Built once a day by the hourly routine from every Okay / Not okay (and, weakly, hand clears and Studio picks), the editors' notes and the photos they keep.
      Story kinds editors approve rank higher, kinds they reject rank lower and the routine is told to skip them unless a story is clearly different.
      ${rec && rec.built_at ? `Last built ${when(rec.built_at)} from ${rec.reviews_used || 0} reviews; overall approval ${pct(rec.p0)}.` : 'Not built yet: the next hourly run builds it.'}
      ${rec && rec.reset_at ? ` Reset ${when(rec.reset_at)} (older reviews ignored).` : ''}</div>
    ${rec && rec.freshness && Object.keys(rec.freshness).length ? `<div class="muted">Approval by how long a post waited for review: ${['under 4h', '4-10h', '10h+'].filter(b => rec.freshness[b]).map(b => `${b} <b>${pct(rec.freshness[b].rate)}</b> (${rec.freshness[b].approved + rec.freshness[b].rejected})`).join(' · ')}. Posts reviewed sooner are approved more often: stale news reads as a weak story.</div>` : ''}
    ${rec && rec.other && Object.keys(rec.other.not_okay_reasons || {}).length ? `<div class="muted">Not okay for other reasons (not counted against story kinds): ${Object.entries(rec.other.not_okay_reasons).map(([k, v]) => `${esc(k.slice(0, 40))} ${v}`).join(' · ')}</div>` : ''}
    ${rec && rec.coverage_7d != null && rec.coverage_7d < 0.3 ? `<div class="job err">Only ${pct(rec.coverage_7d)} of this page's posts were reviewed in the last 7 days: the routine can't learn from posts nobody marks Okay or Not okay.</div>` : ''}
    ${kinds.length ? `<div style="overflow-x:auto"><table><tr><th>Story kind</th><th>Okay</th><th>Not okay</th><th>Approval</th><th>Ranking weight</th><th>Status</th><th>Your weight</th></tr>
      ${kinds.map(([k, v]) => `<tr><td title="${esc([...(v.examples?.approved || []).map(h => '✓ ' + h), ...(v.examples?.rejected || []).map(h => '✗ ' + h)].join('\n'))}">${esc(v.label || k)}</td>
        <td>${v.approved ?? 0}</td><td>${v.rejected ?? 0}</td><td>${pct(v.rate)}</td><td>×${(v.mult ?? 1).toFixed(2)}</td>
        <td><span class="pill ${v.status === 'avoid' ? 'no' : v.status === 'prefer' ? 'ok' : ''}">${esc(v.status || 'mixed')}</span></td>
        <td><input type="number" step="0.1" min="0.3" max="1.5" data-ov="${esc(k)}" value="${ov[k] ?? ''}" placeholder="auto" style="width:70px"></td></tr>`).join('')}</table></div>`
      : '<div class="muted">No kinds yet.</div>'}
    ${lessonNotes(rec)}${lessonPhotos(rec)}
    ${(rec && rec.rules || []).length ? `<div><b>Rules the routine gets</b><ul>${rec.rules.map(r => `<li>${esc(r)}</li>`).join('')}</ul></div>` : ''}
    <label class="muted">Notes for the routine (it follows these, e.g. "practice participation updates are too minor unless the player is a star")</label>
    <textarea rows="3" id="ls-notes" maxlength="2000">${esc((rec && rec.admin_notes) || '')}</textarea>
    <div class="row"><button class="btn primary" id="ls-save">Save notes and weights</button><span class="spacer"></span>
      <button class="btn danger" id="ls-reset">Reset learning</button></div>`;
  q('#ls-save').onclick = e => oneAtATime(e.target, async () => {
    // only what was changed here (a weight cleared goes back to auto): another admin's note or weights are kept
    const body = {}, overrides = {};
    let far = '';
    box.querySelectorAll('[data-ov]').forEach(i => {
      const was = ov[i.dataset.ov] ?? '';
      if (i.value === String(was)) return;
      if (i.value === '') { overrides[i.dataset.ov] = null; return; }
      const v = Number(i.value);
      if (!(v >= 0.3 && v <= 1.5)) far = far || i.value;
      overrides[i.dataset.ov] = v;
    });
    if (far) return toast(`Weights go from 0.3 to 1.5 (${far} is outside): nothing was saved`);
    if (Object.keys(overrides).length) body.overrides = overrides;
    if (q('#ls-notes').value !== ((rec && rec.admin_notes) || '')) body.admin_notes = q('#ls-notes').value;
    if (!Object.keys(body).length) return toast('Nothing to save: no note or weight was changed');
    try { const r = await api(`/api/admin/pages/${slug}/lessons`, { method: 'PUT', body });
          rec = r.lessons; Object.keys(ov).forEach(k => delete ov[k]); Object.assign(ov, rec.overrides || {});
          box.querySelectorAll('[data-ov]').forEach(i => { i.value = ov[i.dataset.ov] ?? ''; });     // as saved
          q('#ls-notes').value = rec.admin_notes || '';
          toast('Saved: used from the next hourly run'); }
    catch (e) { toast(e.message); }
  });
  q('#ls-reset').onclick = e => oneAtATime(e.target, async () => {
    if (!confirm('Reset learning for this page? Reviews made before now will be ignored; your notes and weights are kept.')) return;
    try { await api(`/api/admin/pages/${slug}/lessons/reset`, { method: 'POST', body: { confirm: true } }); lessonsPanel_reload(slug); } catch (e) { toast(e.message); }
  });
}
function lessonsPanel_reload(slug) { document.getElementById('lessons')?.remove(); lessonsPanel(slug); }
// editors' notes (Not okay + note, redo requests): quoted to the routine; recurring themes become rules
function lessonNotes(rec) {
  const notes = (rec && rec.notes) || [], themes = (rec && rec.note_themes) || [];
  if (!notes.length) return '<div class="muted">No editors\' notes yet: a few words with Not okay teach the routine the most.</div>';
  return `<div><b>Editors' notes the routine gets</b> <span class="muted">(newest first, six weeks, the latest ${Math.min(notes.length, 8)} in every brief; older ones with posts of their kind or card template)</span>
    ${themes.length ? `<div class="muted">Recurring: ${themes.map(t => `<b>${esc(t.label)}</b> ${t.count}`).join(' · ')} (each is a rule below)</div>` : ''}
    <ul>${notes.map(n => `<li>“${esc(n.text)}”${n.times > 1 ? ` <b>×${n.times}</b>` : ''} <span class="muted">${esc([n.reason || (n.verdict === 'feedback' ? 'redo request' : ''),
      n.kind_label || n.kind, n.template ? `'${n.template}' card` : '', n.headline].filter(Boolean).join(' · '))} · ${when(n.at)}</span></li>`).join('')}</ul></div>`;
}
// which photos editors keep: hourly Okay vs Not okay for the image, Studio picks vs candidates passed over
function lessonPhotos(rec) {
  const rows = Object.entries((rec && rec.photos) || {}).sort((a, b) => (b[1].n || 0) - (a[1].n || 0));
  if (!rows.length) return '';
  return `<div style="overflow-x:auto"><b>Photos</b> <span class="muted">(the weight only orders candidates that passed the same checks, by at most 10%; neutral until 5 recent reviews)</span>
    <table><tr><th>Photo</th><th>Okay</th><th>Not okay: image</th><th>Studio picked</th><th>Passed over</th><th>Weight</th></tr>
    ${rows.map(([k, v]) => `<tr><td>${esc(v.label || k)}${v.rule ? ' <span class="pill no">rule</span>' : ''}${v.same_as ? ` <span class="muted">(always with ${esc((rec.photos[v.same_as] || {}).label || v.same_as)}: weighed there)</span>` : ''}</td><td>${v.approved ?? 0}</td><td>${v.image_rejected ?? 0}</td>
      <td>${v.picked ?? 0}</td><td>${v.passed ?? 0}</td><td>×${(v.mult ?? 1).toFixed(2)}</td></tr>`).join('')}</table></div>`;
}

// ------------------------------------------------------------------ source sheet (admins only)
const SHEET_COLS = [['category', 'Category'], ['entities', 'Names to watch (boosted)'], ['websites', 'Websites'], ['x', 'X accounts'],
                    ['reddit', 'Subreddits'], ['fb_benchmarks', 'Facebook benchmarks']];
function csvCell(v) { const t = Array.isArray(v) ? v.join('; ') : String(v ?? ''); return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t; }
function parseCsv(text) {
  const rows = []; let row = [], cell = '', q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) { if (c === '"' && text[i + 1] === '"') { cell += '"'; i++; } else if (c === '"') q = false; else cell += c; }
    else if (c === '"') q = true; else if (c === ',') { row.push(cell); cell = ''; }
    else if (c === '\n' || c === '\r') { if (c === '\r' && text[i + 1] === '\n') i++; row.push(cell); rows.push(row); row = []; cell = ''; }
    else cell += c;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  return rows.filter(r => r.some(x => x.trim()));
}
async function sheetPanel(slug) {
  const box = document.createElement('div');
  box.className = 'panel grid'; box.id = 'sheet';
  $('#main .grid').appendChild(box);
  let data;
  try { data = await api(`/api/admin/pages/${slug}/sheet`); } catch (e) { box.textContent = e.message; return; }
  if (!box.isConnected) return;                                      // (the screen was drawn again meanwhile)
  const q = sel => box.querySelector(sel);
  const draw = () => {
    box.innerHTML = `<div class="row"><h3>Source sheet</h3><span class="spacer"></span>
        <button class="btn small" id="sh-dl">Download CSV</button>
        <label class="btn small">Upload CSV<input type="file" accept=".csv,text/csv" hidden id="sh-up"></label></div>
      <div class="muted">The page's categories and what each covers. Story ranking boosts the names listed here
        (${data.boosted} names now). The <b>News sources</b> table above is what is actually collected every hour.
        Only admins see this; every view is in the audit log. Lists: one per line (or separated by ;).</div>
      <div style="overflow-x:auto"><table class="sheet"><tr>${SHEET_COLS.map(([, l]) => `<th>${l}</th>`).join('')}<th></th></tr>
        ${data.categories.map((c, i) => `<tr data-i="${i}">${SHEET_COLS.map(([k]) => `<td>${k === 'category'
          ? `<input type="text" data-k="${k}" value="${esc(c[k] || '')}">`
          : `<textarea rows="3" data-k="${k}">${esc((c[k] || []).join('\n'))}</textarea>`}</td>`).join('')}
          <td><button class="btn small danger" data-rm>Remove</button></td></tr>`).join('')}</table></div>
      <div class="row"><button class="btn small" id="sh-add">+ Category</button></div>
      <label class="muted">Names NOT to boost (brands, platforms...), one per line</label>
      <textarea rows="3" id="sh-ex">${esc((data.priority_excluded || []).join('\n'))}</textarea>
      <div class="row"><button class="btn primary" id="sh-save">Save source sheet</button><span class="muted">${esc(data.note || '')}</span></div>`;
    const read = () => {
      box.querySelectorAll('tr[data-i]').forEach(tr => { const c = data.categories[Number(tr.dataset.i)];
        tr.querySelectorAll('[data-k]').forEach(el => { c[el.dataset.k] = el.tagName === 'INPUT' ? el.value : el.value.split(/\n+/).map(x => x.trim()).filter(Boolean); }); });
      data.priority_excluded = q('#sh-ex').value.split(/\n+/).map(x => x.trim()).filter(Boolean);
    };
    box.querySelectorAll('[data-rm]').forEach(b => b.onclick = () => { read(); data.categories.splice(Number(b.closest('tr').dataset.i), 1); draw(); });
    q('#sh-add').onclick = () => { read(); data.categories.push({ category: 'New category', entities: [], websites: [], x: [], reddit: [], fb_benchmarks: [] }); draw(); };
    q('#sh-save').onclick = e => oneAtATime(e.target, async () => {
      read();
      try { const r = await api(`/api/admin/pages/${slug}/sheet`, { method: 'PUT', body: { categories: data.categories, priority_excluded: data.priority_excluded } });
            data = { ...data, ...r }; draw(); toast(`Saved: ${r.categories.length} categories, ${r.boosted} names boosted (used from the next hourly run)`); }
      catch (e) { toast(e.message); }
    });
    q('#sh-dl').onclick = () => {
      read();
      const csv = [SHEET_COLS.map(([, l]) => csvCell(l)).join(','), ...data.categories.map(c => SHEET_COLS.map(([k]) => csvCell(c[k])).join(','))].join('\n');
      const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' })); a.download = `${slug}-source-sheet.csv`; a.click();
    };
    q('#sh-up').onchange = async e => {
      const f = e.target.files[0]; if (!f) return;
      const rows = parseCsv(await f.text());
      const body = rows[0] && /categor/i.test(rows[0][0]) ? rows.slice(1) : rows;
      if (!body.length) return toast('No rows in that CSV');
      if (!confirm(`Replace the sheet with ${body.length} categories from ${f.name}? Save to keep it.`)) return;
      data.categories = body.map(r => Object.fromEntries(SHEET_COLS.map(([k], j) => [k, k === 'category' ? (r[j] || '').trim() : (r[j] || '').split(/[;\n]+/).map(x => x.trim()).filter(Boolean)])));
      draw(); toast('Loaded: check it, then Save source sheet');
    };
  };
  draw();
}
// what the health status says now, with every reason (the banner shows the first three)
async function adminHealth() {
  const nav = S.nav;
  let h;
  try { h = await api('/api/health'); }
  catch (e) { if (nav === S.nav) $('#main').innerHTML = adminTabs('health') + `<div class="panel muted">The health status is not available: ${esc(e.message)}</div>`; return; }
  S.health = h; S.healthAt = Date.now();
  const el = $('#health'); if (el) el.innerHTML = healthBanner();
  if (nav !== S.nav) return;
  const rs = h.reasons || [];
  $('#main').innerHTML = adminTabs('health') + `<div class="panel grid"><div class="row"><h3>Pipeline health</h3>
      <span class="pill ${LEVEL_PILL[h.status] || ''}">${esc(h.status)}</span><span class="spacer"></span>
      <span class="muted">Checked ${esc(ago(h.checked_at))} (worked out again every 5 minutes)</span></div>
    ${rs.length ? `<div class="adminscroll"><table><tr><th>Level</th><th>What is wrong</th><th>Since</th><th>Code</th></tr>${rs.map(r => `<tr>
      <td><span class="pill ${LEVEL_PILL[r.level] || ''}">${esc(r.level)}</span></td>
      <td>${esc(r.text)}${r.admin ? `<div>${esc(r.admin)}</div>` : ''}<div class="muted">Editors see: ${r.plain ? esc(r.plain) : 'nothing (for the admin only)'}</div>${(r.ids || []).length ? `<div class="muted">${r.ids.map(esc).join(' · ')}</div>` : ''}</td>
      <td class="muted">${r.since ? esc(ago(r.since)) : ''}</td><td class="muted">${esc(r.code)}</td></tr>`).join('')}</table></div>`
      : '<div class="muted">Everything is working: hourly posts, the topic feed, the publisher, Studio jobs and editors\' requests.</div>'}</div>`;
}
async function adminAudit() {
  const nav = S.nav;
  const { entries } = await api('/api/admin/audit?days=7');
  if (nav !== S.nav) return;
  $('#main').innerHTML = adminTabs('audit') + `<div class="panel"><div class="muted" style="margin-bottom:8px">Last 7 days: sign-ins, admin changes, reference document and source access (including the routines' reads), posts sent.</div>
    <div class="adminscroll"><table><tr><th>When</th><th>Who</th><th>What</th><th>Details</th></tr>${entries.map(e => { const { at, actor, action, ...rest } = e;
      return `<tr><td class="muted">${when(at)}</td><td>${esc(actor)}</td><td>${esc(action)}</td><td class="muted wrap">${esc(JSON.stringify(rest))}</td></tr>`; }).join('')}</table></div></div>`;
}
async function adminUsage() {
  const nav = S.nav;
  const { entries } = await api('/api/admin/usage?days=7');
  if (nav !== S.nav) return;
  const tot = {};
  for (const e of entries) { const k = `${e.tool}|${e.page || '—'}`; tot[k] = tot[k] || { units: 0, unit: e.unit, cost: 0, n: 0 }; tot[k].units += e.units || 0; tot[k].cost += e.cost_usd || 0; tot[k].n++; }
  $('#main').innerHTML = adminTabs('usage') + `<div class="panel"><div class="muted" style="margin-bottom:8px">Every AI, search and SocialPilot call is logged with page and user (last 7 days). The cost dashboard (Phase 2) builds on this.</div>
    <table><tr><th>Tool</th><th>Page</th><th>Calls</th><th>Units</th><th>Cost (USD)</th></tr>${Object.entries(tot).map(([k, v]) => { const [tool, page] = k.split('|');
      return `<tr><td>${esc(tool)}</td><td>${esc(page)}</td><td>${v.n}</td><td>${Math.round(v.units * 100) / 100} ${esc(v.unit || '')}</td><td>${v.cost ? v.cost.toFixed(3) : '—'}</td></tr>`; }).join('')}</table></div>`;
}

// ------------------------------------------------------------------ post templates (the page's editors, admins)
// The page's templates as the card renderer draws them (routes/templates.js): the Studio routine
// converts each template of the page's templates document and draws a preview of it; the page's
// editors compare it with the document's page and approve it, or reject it with a note (it is
// converted again with the note), or take it out of use (nothing is converted). Only approved templates
// are offered in the ideas (beside the usual layouts) and drawn. The document's own pages: admins only.
const TPL_STATUS = { pending: ['warn', 'waiting for approval'], approved: ['ok', 'approved'], rejected: ['no', 'rejected'],
                     replaced: ['', 'replaced'], retired: ['', 'out of use'] };
const TPL_REASON = { new_document: 'the new templates document', admin: 'Convert templates again', rejected: 'a rejected template' };
function tplJobLine(j) {
  const why = j.reason.split('+').map(r => TPL_REASON[r] || r).join(' and ');
  const res = j.result || {}, failed = Object.entries(res.failed || {});
  if (j.status === 'queued') return `<div class="job">Converting the templates (for ${esc(why)}): waiting for the Studio routine to start. This page updates by itself.</div>`;
  if (j.status === 'running') return `<div class="job">Converting the templates (for ${esc(why)})…${j.sent ? ` ${j.sent} sent so far: they appear below as they arrive.` : ''} This page updates by itself.</div>`;
  if (j.status === 'failed') return `<div class="job err">Converting the templates failed: ${esc(j.error || 'unknown error')}</div>`;
  if (j.status === 'done') return `<div class="muted">Converted ${ago(j.done_at)}: ${(res.sent || []).length} template(s) sent.${failed.length
    ? ` Not converted: ${failed.map(([id, why]) => `${esc(id)} (${esc(why)})`).join('; ')}.` : ''}</div>`;
  return '';
}
function tplCard(r, t) {
  const slug = r.page, x = t.versions[0], [cls, label] = TPL_STATUS[x.status] || ['', x.status];
  const src = (v, kind, n) => `/api/templates/image?page=${encodeURIComponent(slug)}&id=${encodeURIComponent(t.id)}&v=${v.v}&kind=${kind}${n ? `&n=${n}` : ''}`;
  const older = r.doc && x.doc_version && x.doc_version !== r.doc.version;
  const pages = x.doc_pages.length > 1 ? `pages ${x.doc_pages.join(', ')}` : `page ${x.doc_pages[0] || '?'}`;
  return `<div class="card" data-tpl="${esc(t.id)}" data-page="${esc(slug)}"><div class="row"><b>${esc(t.name)}</b><span class="muted">${esc(t.id)} · version ${x.v}</span>
      <span class="pill ${cls}">${label}</span>${t.in_use ? `<span class="pill ok">in use: version ${t.in_use}</span>` : '<span class="pill">not in use</span>'}
      ${older ? `<span class="pill warn">from document v${x.doc_version}; the active one is v${r.doc.version}</span>` : ''}
      ${t.dropped ? `<span class="pill warn">not in templates document v${r.doc.version}: take it out of use if the new document dropped it</span>` : ''}</div>
    <div class="muted">${esc(t.use_when)}</div>
    <div class="muted">From templates document v${x.doc_version ?? '?'}, ${pages} · converted ${ago(x.at)}${x.decided_by ? ` · ${esc(x.status)} by ${esc(x.decided_by)} ${ago(x.decided_at)}` : ''}</div>
    ${x.status === 'rejected' && x.note ? `<div class="job err">Rejected: ${esc(x.note)}</div>` : ''}
    <div class="tplpair">${r.can_see_document ? `<figure><figcaption class="muted">The templates document (${pages})</figcaption>
        ${x.pages.map(n => `<img src="${src(x, 'page', n)}" alt="document page ${n}" loading="lazy" onclick="zoom(this.src)">`).join('')}</figure>` : ''}
      <figure><figcaption class="muted">How the Studio draws it (made-up text, a stand-in photo)</figcaption>
        ${x.preview ? `<img src="${src(x, 'preview')}" alt="preview" loading="lazy" onclick="zoom(this.src)">` : ''}
        ${x.limits ? `<div class="tpllimits"><b>Drawn more simply than the document:</b> ${esc(x.limits)}</div>` : ''}</figure></div>
    ${x.status === 'replaced' ? '' : `<div class="row">${x.status !== 'approved' ? `<button class="btn ok small" data-approve>${
        { rejected: 'Approve anyway', retired: 'Use it again' }[x.status] || 'Approve'}</button>` : ''}
      ${['pending', 'approved'].includes(x.status) ? `<textarea rows="1" data-note maxlength="1000" style="flex:1;min-width:200px" placeholder="${x.status === 'approved'
        ? 'Something wrong? Say what: it is converted again with your note' : 'What is wrong? It is converted again with your note'}"></textarea>
      <button class="btn small danger" data-reject>${x.status === 'approved' ? 'Convert again' : 'Reject'}</button>` : ''}
      ${x.status === 'approved' || t.dropped ? '<button class="btn small" data-retire>Take out of use</button>' : ''}</div>`}
    ${t.versions.length > 1 ? `<details><summary class="muted">Earlier versions (${t.versions.length - 1})</summary><table>${t.versions.slice(1).map(y => `<tr>
      <td>version ${y.v}</td><td><span class="pill ${(TPL_STATUS[y.status] || [''])[0]}">${esc((TPL_STATUS[y.status] || ['', y.status])[1])}</span></td>
      <td class="muted">document v${y.doc_version ?? '?'}</td><td>${esc(y.note)}</td><td class="muted">${ago(y.at)}</td></tr>`).join('')}</table></details>` : ''}</div>`;
}
async function templatesView(admin, fromPoll) {
  const nav = S.nav;
  if (!S.pages.length) { $('#main').innerHTML = (admin ? adminTabs('templates') : '') + '<div class="empty">No pages are assigned to you yet. Ask an admin.</div>'; return; }
  const slug = S.page;
  const r = await api(`/api/templates?page=${encodeURIComponent(slug)}${fromPoll ? '&poll=1' : ''}`);
  if (nav !== S.nav) return;                                         // they moved on meanwhile
  const open = !!r.job && ['queued', 'running'].includes(r.job.status);
  const seen = JSON.stringify([r.templates, r.job, r.doc]);
  // a conversion under way: followed every 15 s, drawn again only when something changed, never
  // over a note being typed
  const later = () => { clearTimeout(S.poll); if (open) S.poll = setTimeout(() => { if (nav === S.nav) templatesView(admin, true).catch(() => later()); }, 15000); };
  if (fromPoll && (seen === S.tplSeen || editing())) return later();
  S.tplSeen = seen;
  const name = (S.pages.find(p => p.slug === slug) || {}).name || slug;
  const waiting = r.templates.filter(t => t.versions[0].status === 'pending').length;
  // notes typed and not sent yet survive the redraw (after a decision on another template)
  const kept = new Map([...document.querySelectorAll('#main [data-tpl]')].filter(c => c.dataset.page === slug)
    .map(c => [c.dataset.tpl, (c.querySelector('[data-note]') || {}).value || '']));
  $('#main').innerHTML = (admin ? adminTabs('templates') : '') + `<div class="grid">
    <div class="panel grid"><div class="row"><h3 style="margin:0">Post templates: ${esc(name)}</h3>
        ${waiting ? `<span class="pill warn">${waiting} waiting for approval</span>` : ''}<span class="spacer"></span>
        ${r.can_convert ? `<button class="btn" id="tpl-convert" ${open || !r.doc ? 'disabled' : ''}>Convert templates again</button>` : ''}</div>
      <div class="muted">The Studio routine turns each template of the page's templates document into a template it can draw, and draws it with made-up text on a stand-in photo. Compare each one with the template you know, then approve it, or reject it and say what is wrong: it is converted again with your note. Approved templates are offered in the Studio's ideas next to the page's usual layouts, never instead of them. The format draws boxes of photo and text: a cut-out subject, a number behind the subject, angled splits or callout lines come out as plain boxes, so judge the preview as it is drawn.</div>
      <div>${r.doc ? `Templates document: v${r.doc.version} · ${esc(r.doc.filename)} · ${r.doc.pages || '?'} pages` : '<span class="pill warn">no templates document</span> An admin uploads it under Admin &gt; Pages.'}</div>
      ${r.job ? tplJobLine(r.job) : ''}</div>
    ${r.templates.length ? r.templates.map(t => tplCard(r, t)).join('')
      : `<div class="empty">${open ? 'Each template appears here as soon as it is converted.' : 'No templates converted yet.'}</div>`}</div>`;
  const act = async (id, v, action, note, wasInUse) => {
    try {
      await api(`/api/templates/${encodeURIComponent(slug)}/${encodeURIComponent(id)}/${v}/${action}`, { method: 'POST', body: note ? { note } : {} });
      toast({ approve: 'Approved: posts may use it from the routines\' next run', retire: 'Taken out of use: nothing is converted again' }[action]
            || (wasInUse ? 'Out of use until its new version is approved: it is converted again with your note' : 'Rejected: it is converted again with your note'));
      const sent = [...document.querySelectorAll('#main [data-tpl]')].find(c => c.dataset.tpl === id);
      if (sent && sent.querySelector('[data-note]')) sent.querySelector('[data-note]').value = '';   // (its note went with it)
    } catch (e) { toast(e.message); }
    if (nav === S.nav) templatesView(admin);
  };
  document.querySelectorAll('#main [data-tpl]').forEach(card => {
    const t = r.templates.find(x => x.id === card.dataset.tpl), v = t.versions[0].v;
    if (kept.get(t.id) && card.querySelector('[data-note]')) card.querySelector('[data-note]').value = kept.get(t.id);
    const ok = card.querySelector('[data-approve]'), no = card.querySelector('[data-reject]'), off = card.querySelector('[data-retire]');
    if (ok) ok.onclick = () => { ok.disabled = true; act(t.id, v, 'approve'); };
    if (off) off.onclick = () => {
      if (!confirm('Take this template out of use? Posts stop using it at the routines\' next run; nothing is converted again. You can use it again later.')) return;
      off.disabled = true; act(t.id, t.in_use || v, 'retire', (card.querySelector('[data-note]') || {}).value || '');
    };
    if (no) no.onclick = () => {
      const note = card.querySelector('[data-note]').value.trim();
      if (note.length < 3) { card.querySelector('[data-note]').focus(); return toast('Say what is wrong first: it is converted again with your note'); }
      // the one in use: out of use from now on, until its new version is approved
      if (t.versions[0].status === 'approved' && !confirm('This template stops being used now, until its new version is approved. Convert it again?')) return;
      no.disabled = true; act(t.id, v, 'reject', note, t.versions[0].status === 'approved');
    };
  });
  const conv = $('#tpl-convert');
  if (conv) conv.onclick = async () => {
    if (!confirm('Convert every template of the document again? The new versions wait for approval; the ones in use stay in use until then.')) return;
    conv.disabled = true;
    try { const c = await api(`/api/templates/${encodeURIComponent(slug)}/convert`, { method: 'POST', body: {} }); toast(c.joined ? 'Already waiting for the Studio routine' : 'Queued for the Studio routine'); }
    catch (e) { toast(e.message); }
    if (nav === S.nav) templatesView(admin);
  };
  later();
}

// ------------------------------------------------------------------ router
// The dashboard deployed since this tab opened (deploy.sh writes version.json): the tab's code is
// out of date, and a step it does not know about would fail without saying why. Checked on each
// screen and poll, at most once a minute; the dev server has no version.json (nothing is shown).
function checkVersion() {
  if (Date.now() - (S.versionAt || 0) < 60e3) return;
  S.versionAt = Date.now();
  fetch(`/version.json?t=${Date.now()}`, { cache: 'no-store' }).then(r => (r.ok ? r.json() : null)).then(v => {
    if (!v || !v.commit) return;
    if (!S.version) S.version = v.commit;
    else if (v.commit !== S.version && !$('#newversion')) {
      document.body.insertAdjacentHTML('afterbegin', `<div class="newversion" id="newversion" role="status">A new version is available: reload
        <button class="btn small" id="newversion-go">Reload</button></div>`);
      $('#newversion-go').onclick = async () => {
        await S.saving;                                              // (a caption on its way is saved first)
        if (unsaved() && !confirm('Some of your text is not saved yet (the connection or storage is not answering). Reload anyway? It would be lost.')) return;
        S.reloading = true; location.reload();
      };
    }
  }).catch(() => {});
}

async function route() {
  clearTimeout(S.poll); S.nav++;                     // a poll still under way for the last screen draws nothing
  const nav = S.nav;
  if (!S.retrying) S.routeFails = 0;                 // (counted while it tries again by itself)
  S.retrying = false;
  checkVersion();
  const h = location.hash.replace(/^#/, '') || '/studio';
  const reset = location.hash.match(/^#\/?reset=(.+)$/);
  if (reset) { header(); return setPasswordView(decodeURIComponent(reset[1])); }
  if (h.startsWith('/forgot')) { header(); return forgotView(); }
  if (!S.me) {
    try { await loadMe(); } catch (e) {
      header();
      // the server's own words when it sent some (not a bare status code, e.g. a timeout page)
      // (storage not answering: this screen tries again by itself, not "in a few minutes")
      const why = e.status === 503 && e.body && e.body.error ? 'The dashboard\'s storage is not answering just now: this screen tries again by itself.'
        : e.body && e.body.error ? e.message
        : e.status ? 'The dashboard is not answering: try again in a minute.' : 'The dashboard is not answering: check the internet connection.';
      $('#main').innerHTML = `<div class="empty" data-failed>${esc(why)}
        If you were signed in, you still are. <button class="btn" id="again">Try again</button></div>`;
      $('#again').onclick = () => route();
      // as the other screens: without a connection, or with storage not answering, it tries again by itself
      S.routeFails = (S.routeFails || 0) + 1;
      if (e.offline || e.status === 503) setTimeout(() => { if (nav === S.nav) { S.retrying = true; route(); } }, Math.min(60e3, 5000 * 2 ** (S.routeFails - 1)));
      return;
    }
  }
  // a page or the admin role given (or taken away) since: asked again at most once a minute, and at
  // once when an admin screen is opened without the admin role (it may have just been given)
  else if (Date.now() - (S.meAt || 0) > 60e3 || (h.startsWith('/admin') && S.me.user.role !== 'super')) {
    const before = S.me;
    await loadMe().catch(() => {});
    const msg = accessTaken(before);
    if (msg && nav === S.nav) toast(msg);
  }
  if (nav !== S.nav) return;
  if (!S.me) { header(); return loginView(h.startsWith('/login') ? decodeURIComponent((h.match(/next=([^&]+)/) || [])[1] || '') : h, (h.match(/[?&]why=(\w+)/) || [])[1]); }
  if (S.me.user.must_change || h === '/password') { header(); return setPasswordView(null); }
  if (h.startsWith('/login')) return go('/studio');
  header();
  loadHealth();
  const parts = h.split('/').filter(Boolean);
  try {
    if (parts[0] === 'studio' && parts[1] === 'b') {
      // another step: the one on screen goes at once (its buttons would act on what it showed) while this one loads
      const step = parts[3] || 'ideas', on = $('#main .steps a.on');
      if (!on || on.getAttribute('href') !== `#/studio/b/${parts[2]}/${step}`) $('#main').innerHTML = stepper(parts[2], step) + '<div class="empty">Loading…</div>';
      return await ({ ideas: ideasView, visual: visualView, caption: captionView, check: checkView, post: postView }[parts[3]] || ideasView)(parts[2]);
    }
    if (parts[0] === 'studio' && parts[1] === 'topics') return await topicsView();
    if (parts[0] === 'templates') return await templatesView(false);
    if (parts[0] === 'admin' && S.me.user.role === 'super') {
      if (parts[1] === 'page') await adminPage(parts[2]);
      else if (parts[1] === 'templates') await templatesView(true);
      else await ({ users: adminUsers, pages: adminPages, health: adminHealth, audit: adminAudit, usage: adminUsage }[parts[1]] || adminUsers)();
      if (nav === S.nav) restoreDrafts();                            // (signed out while typing here)
      return;
    }
    return await studioHome();
  } catch (e) {
    // (an answer for a screen the editor has left is not drawn over the one they are on)
    if (nav !== S.nav) return;
    // the step bar stays, with what went wrong and a way to try again; without a connection, or with
    // storage not answering, it tries again by itself (later and later) and when the phone is back online
    const step = parts[0] === 'studio' && parts[1] === 'b' && parts[2] ? stepper(parts[2], parts[3] || 'ideas') : '';
    $('#main').innerHTML = `${step}<div class="empty" data-failed>${esc(e.message)}<div style="margin-top:10px"><button class="btn" id="tryagain">Try again</button></div></div>`;
    $('#tryagain').onclick = () => route();
    S.routeFails = (S.routeFails || 0) + 1;
    if (e.offline || e.status === 503) setTimeout(() => { if (nav === S.nav) { S.retrying = true; route(); } }, Math.min(60e3, 5000 * 2 ** (S.routeFails - 1)));
    return;
  }
}
window.addEventListener('online', () => { if ($('#main [data-failed]')) route(); });
window.addEventListener('hashchange', route);
route();
