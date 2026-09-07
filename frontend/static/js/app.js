/* ═══════════════════════════════════════════════════════════════════════
   CC ES Analyzer — Frontend Application
   ═══════════════════════════════════════════════════════════════════════ */

/* ── Constants ────────────────────────────────────────────────────────── */
const LS_ACTIVE  = 'cc_es_active_conn';   // last-used connection settings
const LS_PROFILES = 'cc_es_profiles';     // saved named profiles

/* ── Base path (reverse-proxy prefix support) ───────────────────────────
   The app may be served at "/" (direct, e.g. :8801) OR under a path prefix
   behind nginx (e.g. http://<host>/cc_es_analyzer/). Derive the prefix from
   the page URL so every API/static request stays inside it. */
const APP_BASE = new URL('.', window.location.href).pathname.replace(/\/$/, '');
/** Prefix an absolute app path ("/api/...", "/static/...") with APP_BASE. */
function appUrl(path) {
  return path.startsWith('/') ? APP_BASE + path : path;
}

/* ── Capability policy ──────────────────────────────────────────────────
   Which features this deployment carries (GET /api/policy, backed by
   core/policy.py). Used ONLY to avoid offering an action the server
   would refuse — the real control is that a disabled capability has no
   route to call. Never gate anything on this alone.
   Defaults to "on" so a failed policy fetch degrades to today's behaviour
   rather than blanking the UI; the server still answers 404. */
let CAPS = null;

function can(capId) {
  if (!CAPS) return true;
  return CAPS[capId] === undefined ? true : !!CAPS[capId];
}

/* ══════════════════════════════════════════════════════════════════════════
   LOGIN + SESSION LIFETIME  (embedded profile)

   Two mechanisms that only make sense together. The container is meant to be
   OFF between uses: nothing restarts it, it stops itself when its window
   closes, and while it is up it wants a password. Everything below is the
   browser half; enforcement is core/auth.py and core/lifecycle.py, and every
   /api path answers 401 on its own. This code makes the door visible — it is
   not the lock.
   ══════════════════════════════════════════════════════════════════════════ */

let _loginShown = false;
let _appStarted = false;
let _lifeTimer = null;
let _lifePrompt = null;      // the open extend dialog, so it is never doubled
let _lifeStopped = false;

/** Show the login screen and keep the app from booting behind it. */
function showLoginScreen(message) {
  _loginShown = true;
  document.getElementById('loginScreen')?.classList.remove('d-none');
  const err = document.getElementById('loginError');
  if (err) {
    err.textContent = message || '';
    err.classList.toggle('d-none', !message);
  }
  setTimeout(() => document.getElementById('loginPass')?.focus(), 0);
}

function hideLoginScreen() {
  _loginShown = false;
  document.getElementById('loginScreen')?.classList.add('d-none');
}

/** The container has gone. Nothing in this page can bring it back. */
function showStoppedScreen() {
  if (_lifeStopped) return;
  _lifeStopped = true;
  if (_lifeTimer) { clearInterval(_lifeTimer); _lifeTimer = null; }
  _lifePrompt?.remove();
  _lifePrompt = null;
  hideLoginScreen();
  document.getElementById('stoppedScreen')?.classList.remove('d-none');
}

async function submitLogin(ev) {
  ev?.preventDefault();
  const btn = document.getElementById('loginBtn');
  const user = document.getElementById('loginUser')?.value || '';
  const pass = document.getElementById('loginPass')?.value || '';
  if (btn) { btn.disabled = true; btn.textContent = 'Signing in…'; }

  const res = await api('/api/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: user, password: pass }),
  });

  if (btn) { btn.disabled = false; btn.textContent = 'Sign in'; }
  if (!res || res.error) {
    const err = document.getElementById('loginError');
    if (err) { err.textContent = res?.error || 'Sign-in failed.'; err.classList.remove('d-none'); }
    const pw = document.getElementById('loginPass');
    if (pw) { pw.value = ''; pw.focus(); }
    return;
  }

  hideLoginScreen();
  const pw = document.getElementById('loginPass');
  if (pw) pw.value = '';
  await startApp();

  // The offer to change a shipped password, made once. Deliberately AFTER the
  // app is up: an engineer who came here to look at a failing CC should reach
  // it first and be asked about hygiene second.
  if (res.using_default && !res.change_offered) {
    setTimeout(() => promptChangePassword(true), 600);
  }
}

async function doLogout() {
  await api('/api/auth/logout', { method: 'POST' });
  location.reload();
}

/** Change the appliance password. `offer` marks the one-time first-login ask,
 *  which may be declined; opened from the menu it is a plain change. */
function promptChangePassword(offer) {
  document.querySelector('.rt-modal-overlay.rt-pw')?.remove();
  const wrap = document.createElement('div');
  wrap.className = 'rt-modal-overlay rt-pw';
  wrap.innerHTML = `<div class="rt-modal" style="min-width:22rem;width:26rem;max-width:95vw;">
      <div class="rt-modal-title"><i class="bi bi-key me-1"></i>${
        offer ? 'This CC is using its default password' : 'Change password'}</div>
      <div class="rt-modal-body" style="white-space:normal;">
        ${offer ? `<div class="small text-secondary mb-2">Anyone who knows the shipped
             default can sign in to this CC. Changing it now is the single most useful
             thing you can do on this screen — but you can keep it.</div>` : ''}
        <label class="small fw-semibold">Current password</label>
        <input type="password" class="form-control form-control-sm pw-cur mb-2" autocomplete="current-password">
        <label class="small fw-semibold">New password</label>
        <input type="password" class="form-control form-control-sm pw-new mb-2" autocomplete="new-password">
        <label class="small fw-semibold">Confirm new password</label>
        <input type="password" class="form-control form-control-sm pw-new2" autocomplete="new-password">
        <div class="small text-secondary mt-2">At least 8 characters. Stored hashed,
          and it survives a container restart.</div>
        <div class="pw-err small text-danger mt-2 d-none"></div>
      </div>
      <div class="rt-modal-actions">
        <button class="btn btn-sm btn-primary" data-a="save">Change password</button>
        <button class="btn btn-sm btn-outline-secondary" data-a="skip">${
          offer ? 'Keep the current one' : 'Cancel'}</button>
      </div></div>`;
  document.body.appendChild(wrap);
  setTimeout(() => wrap.querySelector('.pw-cur')?.focus(), 0);

  const fail = (msg) => {
    const el = wrap.querySelector('.pw-err');
    el.textContent = msg;
    el.classList.remove('d-none');
  };

  wrap.addEventListener('click', async (e) => {
    const act = e.target.closest('button')?.dataset.a;
    if (!act) return;
    if (act === 'skip') {
      // Record the decline so the offer is made once, not at every sign-in. It
      // does NOT count as hardening: the server still reports the default is in
      // place, and still says so in its log at every startup.
      if (offer) await api('/api/auth/keep-default', { method: 'POST' });
      wrap.remove();
      if (offer) showToast('Keeping the default password — change it any time from the menu', 'bg-warning');
      return;
    }
    const cur = wrap.querySelector('.pw-cur').value;
    const nw  = wrap.querySelector('.pw-new').value;
    const nw2 = wrap.querySelector('.pw-new2').value;
    if (nw !== nw2) return fail('The two new passwords do not match.');
    const res = await api('/api/auth/password', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ current: cur, new: nw }),
    });
    if (!res || res.error) return fail(res?.error || 'Could not change the password.');
    wrap.remove();
    showToast('Password changed', 'bg-success');
  });
}

/* ── The countdown ─────────────────────────────────────────────────────── */

function _fmtCountdown(sec) {
  const m = Math.floor(sec / 60), s = sec % 60;
  return m >= 60
    ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`
    : `${m}:${String(s).padStart(2, '0')}`;
}

async function pollSessionLifetime() {
  let st;
  try {
    st = await api('/api/session/lifetime');
  } catch {
    return;                                   // transient; the next tick retries
  }
  // A stopped container answers nothing at all. That is the expected end state
  // here, not an error worth a toast.
  if (!st || st.error) { if (_appStarted) showStoppedScreen(); return; }
  if (!st.enabled) return;

  const chip = document.getElementById('sessionChip');
  if (chip) {
    chip.classList.remove('d-none');
    chip.classList.toggle('warn', !!st.warning && !st.draining);
    chip.classList.toggle('drain', !!st.draining);
    // The server is the authority; the local tick only fills the gaps between
    // polls, so every answer re-seeds it and clock drift can never accumulate.
    _lifeSecondsLeft = st.seconds_left;
    if (st.draining) {
      chip.textContent = 'stops when work finishes';
      chip.title = 'The window has closed. This container stops as soon as the '
                 + 'running work finishes — click to keep it open.';
    } else {
      _paintSessionChip();
    }
  }

  renderDrainBar(st);

  // The SERVER decides when the prompt is due, so every open tab agrees and a
  // browser with a skewed clock cannot sail past the warning window.
  if ((st.warning || st.draining) && !_lifePrompt) promptExtendSession(false, st);
  if (st.stopping) showStoppedScreen();
}

/** Persistent bar while the box has expired and is waiting on running work. */
function renderDrainBar(st) {
  const host = document.getElementById('drainBarHost');
  if (!host) return;
  if (!st.draining) { host.innerHTML = ''; return; }
  const jobs = (st.running_jobs || []);
  host.innerHTML = `<div class="session-drain-bar">
      <i class="bi bi-hourglass-split"></i>
      <span>This window has closed. <b>The container will stop as soon as the
        ${jobs.length ? `${jobs.length} running ${jobs.length > 1 ? 'operations finish' : 'operation finishes'}`
                      : 'current work finishes'}</b> — it is not being interrupted.</span>
      <button class="btn btn-sm btn-warning py-0 px-2 ms-auto" onclick="extendSession()">
        Keep it open for another hour</button>
    </div>`;
}

async function extendSession() {
  const res = await api('/api/session/extend', { method: 'POST' });
  if (!res || res.error) {
    showToast('Could not extend: ' + (res?.error || 'request failed'), 'bg-danger');
    return false;
  }
  _lifePrompt?.remove();
  _lifePrompt = null;
  showToast(`Extended — this container now runs for another ${res.window_minutes || 60} minutes`,
            'bg-success');
  pollSessionLifetime();
  return true;
}

/** The T-minus prompt. `manual` = the user clicked the chip themselves. */
function promptExtendSession(manual, st) {
  if (_lifePrompt) return;
  const left = st?.seconds_left;
  const draining = !!st?.draining;
  const jobs = (st?.running_jobs || []);

  const wrap = document.createElement('div');
  wrap.className = 'rt-modal-overlay rt-life';
  wrap.innerHTML = `<div class="rt-modal" style="min-width:22rem;width:28rem;max-width:95vw;">
      <div class="rt-modal-title"><i class="bi bi-hourglass-split me-1"></i>${
        draining ? 'Stopping when the current work finishes' : 'This session is about to end'}</div>
      <div class="rt-modal-body" style="white-space:normal;">
        ${draining
          ? `<p>The window has closed.${jobs.length
              ? ` <b>${jobs.length} operation${jobs.length > 1 ? 's are' : ' is'} still running</b>,
                 so nothing is being interrupted — the container stops as soon as
                 ${jobs.length > 1 ? 'they finish' : 'it finishes'}.`
              : ''}</p>
             <p class="mb-0">Keep it open if you still need it.</p>`
          : `<p>This container stops ${left != null
                ? `in <b class="life-remaining">${_fmtCountdown(left)}</b>` : 'shortly'} and nothing restarts it
             automatically.</p>
             <p class="mb-0">Extend it if you are still working.</p>`}
        <div class="small text-secondary mt-2">
          Once stopped, it takes a <span class="font-monospace">docker start cc-admin</span>
          on the CC to bring it back.</div>
      </div>
      <div class="rt-modal-actions">
        <button class="btn btn-sm btn-warning" data-a="extend">Keep it open for another hour</button>
        <button class="btn btn-sm btn-outline-secondary" data-a="let">Let it stop</button>
      </div></div>`;
  document.body.appendChild(wrap);
  _lifePrompt = wrap;

  // The number in the dialog ticks as well. A countdown that sits still while
  // someone reads it looks like a page that has stopped responding — which is
  // an unfortunate impression for a dialog whose whole subject is whether this
  // thing is about to shut down.
  const liveEl = wrap.querySelector('.life-remaining');
  if (liveEl && !draining) {
    const paint = () => {
      if (!wrap.isConnected) { clearInterval(t); return; }
      liveEl.textContent = _fmtCountdown(Math.max(0, _lifeSecondsLeft ?? 0));
    };
    const t = setInterval(paint, 1000);
    paint();
  }

  wrap.addEventListener('click', async (e) => {
    const act = e.target.closest('button')?.dataset.a;
    if (!act) return;
    if (act === 'extend') { await extendSession(); return; }
    // "Let it stop" is not a shutdown command — it only dismisses. Doing
    // nothing has the same effect, which is the point of a time box.
    wrap.remove();
    _lifePrompt = null;
  });
}

/* The chip counts down every SECOND, but the server is only asked every 15.
   Polling once a second to animate a clock would be fifteen times the requests
   for information the browser can work out for itself; showing a number that
   sits still for fifteen seconds reads as a frozen page. So the server remains
   the authority — every poll re-seeds this — and between polls the browser
   just decrements its own copy. */
let _lifeSecondsLeft = null;
let _lifeTick = null;

function _startLifeTick() {
  if (_lifeTick) return;
  _lifeTick = setInterval(() => {
    if (_lifeSecondsLeft == null || _lifeStopped) return;
    if (_lifeSecondsLeft > 0) _lifeSecondsLeft -= 1;
    _paintSessionChip();
  }, 1000);
}

function _paintSessionChip() {
  const chip = document.getElementById('sessionChip');
  if (!chip || _lifeSecondsLeft == null) return;
  if (chip.classList.contains('drain')) return;   // draining shows text, not a clock
  chip.textContent = _fmtCountdown(Math.max(0, _lifeSecondsLeft));
  chip.title = `This container stops in ${_fmtCountdown(Math.max(0, _lifeSecondsLeft))} `
             + 'unless extended. Click to extend.';
}

function startLifetimePolling() {
  if (_lifeTimer) return;
  pollSessionLifetime();
  _lifeTimer = setInterval(pollSessionLifetime, 15000);
  _startLifeTick();
}

/* ══════════════════════════════════════════════════════════════════════════
   CONNECTIVITY  — can this CC reach the services it depends on?

   Several CC features fetch from the internet: signature updates, the ERT
   Active Attackers Feed, GeoDB location updates, licence activation. When one
   silently stops working, the first question is whether the box can reach the
   outside world at all — and today that is answered by SSHing in and running
   `wget services.radware.com`, a step the knowledge base documents verbatim.

   The screen shows each stage — DNS, TCP, TLS, HTTP — separately, because
   which one failed decides WHO fixes it: DNS is the resolver, TCP is the
   firewall, TLS is almost always the customer's inspecting proxy. "Cannot
   reach it" identifies none of those.
   ══════════════════════════════════════════════════════════════════════════ */

let _connData = null;

const _CONN_SEV = {
  ok:      { cls: 'success', icon: 'bi-check-circle-fill',       label: 'reachable' },
  unknown: { cls: 'secondary', icon: 'bi-question-circle-fill',  label: 'unknown' },
  warn:    { cls: 'warning', icon: 'bi-exclamation-triangle-fill', label: 'problem' },
  crit:    { cls: 'danger',  icon: 'bi-x-octagon-fill',          label: 'unreachable' },
};

const _STAGE_LABEL = {
  dns:  'DNS',
  tcp:  'TCP',
  tls:  'TLS',
  http: 'HTTP',
};

async function loadConnectivity(manual) {
  const results = document.getElementById('connResults');
  const banner = document.getElementById('connBanner');
  if (!results) return;
  if (manual || !_connData) {
    results.innerHTML = `<div class="text-center text-secondary py-4">
        <span class="spinner-border spinner-border-sm me-2"></span>
        Checking each destination — DNS, TCP, TLS, then HTTP…</div>`;
    if (banner) banner.innerHTML = '';
  }

  const data = await api('/api/diag/connectivity');
  if (!data || data.error) {
    results.innerHTML = `<div class="alert alert-danger py-2 small mb-0">${
      esc(data?.error || 'The connectivity check could not run.')}</div>`;
    return;
  }
  _connData = data;
  renderConnectivity(data);
}

function renderConnectivity(data) {
  const sev = _CONN_SEV[data.severity] || _CONN_SEV.unknown;
  const banner = document.getElementById('connBanner');
  if (banner) {
    // Standalone, this screen reports the ENGINEER'S laptop, which may sit on
    // the open internet while the CC being debugged reaches nothing. Said
    // first, and in its own alert, because a caveat in grey small text under a
    // green tick is a caveat nobody reads.
    const warn = data.vantage?.warning
      ? `<div class="alert alert-warning py-2 mb-2 d-flex align-items-start gap-2">
           <i class="bi bi-exclamation-triangle-fill fs-5"></i>
           <div><div class="fw-semibold">These results are about this machine, not the CC</div>
           <div class="small">${esc(data.vantage.warning)}</div></div></div>`
      : '';
    banner.innerHTML = warn + `<div class="alert alert-${sev.cls} py-2 mb-0 d-flex align-items-center gap-2">
        <i class="bi ${sev.icon} fs-5"></i>
        <div>
          <div class="fw-semibold">${esc(data.headline || '')}</div>
          <div class="small opacity-75">Probed from ${esc(data.vantage_point || 'this container')}
            — strong evidence about this appliance, though not necessarily the
            same network path as the service that fetches the feed.</div>
        </div></div>`;
  }

  const meta = document.getElementById('connMeta');
  if (meta) meta.textContent = `checked ${new Date().toLocaleTimeString()}`;

  const dot = document.getElementById('connNavDot');
  if (dot) dot.className = 'sys-dot ms-1 sys-' + (data.severity || 'unknown');

  document.getElementById('connResults').innerHTML =
    (data.results || []).map(_connCard).join('');
}

function _connCard(r) {
  const sev = _CONN_SEV[r.severity] || _CONN_SEV.unknown;
  const stages = (r.stages || []).map(s => {
    const ss = _CONN_SEV[s.status] || _CONN_SEV.unknown;
    return `<div class="d-flex align-items-start gap-2 py-1">
        <span class="badge bg-${ss.cls}" style="min-width:3.4rem;">${esc(_STAGE_LABEL[s.stage] || s.stage)}</span>
        <div class="small flex-grow-1">
          ${esc(s.detail || ss.label)}
          ${s.ms != null ? `<span class="text-secondary ms-1">· ${s.ms} ms</span>` : ''}
        </div></div>`;
  }).join('');

  // Provenance: the knowledge-base articles that establish this as a real
  // dependency. An engineer must be able to check why the tool believes this
  // matters, the same way a corrective action will have to cite its source.
  const src = (r.sources || []).length
    ? `<div class="small text-secondary mt-2">Source: KB ${r.sources.map(esc).join(', ')}</div>` : '';

  return `<div class="card mb-2 shadow-sm">
      <div class="card-body py-2 px-3">
        <div class="d-flex align-items-center gap-2 flex-wrap">
          <i class="bi ${sev.icon} text-${sev.cls}"></i>
          <span class="fw-semibold">${esc(r.label)}</span>
          <span class="font-monospace small text-secondary">${esc(r.host)}:${r.port}</span>
          ${r.critical ? '' : '<span class="badge bg-secondary">informational</span>'}
          ${r.proxy?.in_use ? `<span class="badge bg-info text-dark" title="${esc(r.proxy.url)}">via proxy</span>` : ''}
          ${r.vantage === 'cc'
            ? '<span class="badge bg-success-subtle text-success border border-success" title="This check ran on the CC itself, over SSH — so it describes the appliance, not the machine running CC Admin.">probed on the CC</span>'
            : '<span class="badge bg-secondary" title="This check ran in the CC Admin process, so it describes the machine CC Admin runs on.">probed locally</span>'}
          <span class="ms-auto small text-${sev.cls}">${esc(r.headline || '')}</span>
        </div>
        <div class="small text-secondary mt-1">${esc(r.purpose || '')}</div>
        ${r.caveat ? `<div class="small text-warning mt-1"><i class="bi bi-info-circle me-1"></i>${esc(r.caveat)}</div>` : ''}
        <div class="mt-2 border-top pt-2">${stages}</div>
        ${src}
      </div></div>`;
}

/* Rail tree: which groups the user collapsed. Persisted because re-collapsing
   the tree on every page load would make collapsing not worth doing. */
const LS_DB_TREE = 'cc_admin_db_tree';

function _dbTreeState() {
  try { return JSON.parse(localStorage.getItem(LS_DB_TREE)) || {}; }
  catch { return {}; }
}

/** Collapse/expand a datastore group in the rail. Groups nest, so collapsing
 *  "Databases" hides the stores, and collapsing a store hides its screens. */
function toggleDbGroup(id, force) {
  const btn = document.getElementById(`db-${id}-toggle`);
  const kids = document.getElementById(`db-${id}-children`);
  if (!btn || !kids) return;
  const collapsed = force === undefined ? !kids.classList.contains('collapsed') : !!force;
  kids.classList.toggle('collapsed', collapsed);
  btn.classList.toggle('collapsed', collapsed);
  if (force === undefined) {
    const st = _dbTreeState();
    st[id] = collapsed;
    try { localStorage.setItem(LS_DB_TREE, JSON.stringify(st)); } catch { /* private mode */ }
  }
}

/** Re-apply the persisted collapse state. Default is expanded. */
function initDbTree() {
  const st = _dbTreeState();
  for (const id of ['root', 'es', 'maria', 'pg']) if (st[id]) toggleDbGroup(id, true);
}

/** The count badge on "Databases" must be what is actually listed, not a
 *  literal — a hardcoded 2 becomes a lie the moment a store is gated off. */
function syncDbCount() {
  const badge = document.getElementById('db-count');
  if (!badge) return;
  badge.textContent = String(
    document.querySelectorAll('#db-root-children > .ops-db-toggle:not(.d-none)').length);
}

/** Mirror connection state onto the Elasticsearch group's status dot. */
function syncDbStatus() {
  const dot = document.getElementById('db-es-dot');
  if (dot) dot.className = 'conn-dot ops-db-dot ' + (isConnected ? 'connected' : 'disconnected');
}

/** Apply the capability policy to the chrome. Called once the policy is in. */
function applyPolicyToChrome() {
  // Embedded on a CC there is exactly one datastore — the one running beside
  // the app — so a connection screen offers a choice that does not exist.
  if (!can('es.connect')) {
    document.getElementById('nav-connection')?.classList.add('d-none');
    document.getElementById('sidebarConnBox')
      ?.querySelector('[onclick*="connection"]')?.classList.add('d-none');
    document.querySelector('#connectedPill [onclick*="disconnect"]')?.classList.add('d-none');
    // Reachable only when the co-located ES is down — the one moment the tool
    // is least able to explain itself. Sending the user to a connection screen
    // that does not exist here wastes the trip: nothing is theirs to configure,
    // the datastore beside them is simply not answering.
    document.getElementById('disconnectedConfigure')?.remove();
    const pill = document.querySelector('#disconnectedPill .text-secondary');
    if (pill) pill.textContent = 'Elasticsearch not responding';
  }

  // Embedded, the tool is a service of the CC: it arrives in the ISO/OVA and
  // upgrades when the CC upgrades, so there is nothing for a user to update
  // here and /api/update/* is not registered at all. Show the version — it is
  // what identifies the build — but strip the update affordance from it, or
  // the navbar keeps a clickable element whose dialog can only 404.
  if (!can('app.self_update')) {
    const ver = document.getElementById('appVersion');
    if (ver) {
      ver.textContent = window.APP_VERSION ? 'v' + window.APP_VERSION : '';
      ver.title = 'Installed version — upgrades with the CC';
      ver.removeAttribute('onclick');
      ver.style.cursor = 'default';
    }
    document.getElementById('updateBtn')?.remove();
  }

  // A datastore whose routes are not registered must not appear in the tree at
  // all. Hiding the node rather than disabling it: an entry that 404s on click
  // is worse than no entry.
  if (!can('maria.read')) {
    document.getElementById('db-maria-toggle')?.classList.add('d-none');
    document.getElementById('db-maria-children')?.classList.add('d-none');
  }
  if (!can('maria.query.raw')) {
    document.getElementById('nav-mariaquery')?.classList.add('d-none');
  }
  if (!can('pg.read')) {
    document.getElementById('db-pg-toggle')?.classList.add('d-none');
    document.getElementById('db-pg-children')?.classList.add('d-none');
  }
  if (!can('pg.query.raw')) {
    document.getElementById('nav-pgquery')?.classList.add('d-none');
  }

  // Embedded, POST /api/indices/create is not registered: a CC builds its own
  // indices from its templates, and an engineer wanting a scratch index wants
  // it on their own machine, not on a customer's appliance. The "Possible"
  // button beside this one stays — reading the catalog of families this CC
  // could produce is diagnosis, and it is the half worth keeping here.
  // Account controls exist only where a login does.
  if (CAPS && window.APP_AUTH_REQUIRED) {
    document.getElementById('btnLogout')?.classList.remove('d-none');
    document.getElementById('btnChangePassword')?.classList.remove('d-none');
  }

  if (!can('es.index.create')) {
    document.getElementById('btnAddIndex')?.remove();
    // The sidebar carries a second, smaller one beside the "Indices" label.
    document.getElementById('btnSidebarCreateIndex')?.remove();
  }

  // The index detail view carries its own copies of these actions, and they
  // are the ones that matter: the dashboard's Add menu is a front door, but
  // opening any index put the fabricator one click away regardless of profile.
  // Removed rather than disabled — a dead control with no explanation reads as
  // a bug, and there is nothing to explain here because the endpoint behind it
  // does not exist in this deployment.
  if (!can('es.artificial')) {
    document.getElementById('btnIndexArtificial')?.remove();
  }
  if (!can('es.index.duplicate')) {
    document.getElementById('btnIndexDuplicate')?.remove();
  }
  // Embedded, the sanctioned way to put data back on a CC is an archive
  // restore, not a CSV of unknown provenance — see es.doc.import.
  if (!can('es.doc.import')) {
    document.getElementById('btnIndexImportCsv')?.remove();
  }
  if (!can('es.index.delete')) {
    document.querySelector('#view-index [onclick="deleteCurrentIndex()"]')?.remove();
  }
  syncDbCount();
}

async function loadPolicy() {
  try {
    const p = await api('/api/policy');
    if (p && p.capabilities) {
      CAPS = {};
      for (const [id, v] of Object.entries(p.capabilities)) CAPS[id] = !!v.enabled;
      window.APP_PROFILE = p.profile;
      window.APP_VERSION = p.version || '';
    }
  } catch { /* leave CAPS null — everything stays offered */ }
}

/* ── App state ──────────────────────────────────────────────────────────── */
let allIndices   = [];
let chartCategory = null;
let chartTimeline = null;
let isConnected  = false;

// Summary charts
let chartSummaryTimeline = null;
let chartSummaryCategory = null;
let chartSummaryTraffic  = null;
let summaryData          = null;
let summaryGranularity   = 'day';

/* ══════════════════════════════════════════════════════════════════════════
   VIEW ROUTING
   ══════════════════════════════════════════════════════════════════════════ */
let currentView = 'connection';   // which .view-panel is visible (for auto-refresh)

function showView(name) {
  currentView = name;
  document.querySelectorAll('.view-panel').forEach(p => p.classList.add('d-none'));
  const panel = document.getElementById(`view-${name}`);
  if (panel) panel.classList.remove('d-none');

  // Highlight sidebar button
  document.querySelectorAll('.sidebar-nav-btn').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById(`nav-${name}`);
  if (btn) btn.classList.add('active');

  // Restore the Query Editor's own results viewer when returning to it.
  if (name === 'query' && typeof activateViewer === 'function' && activeViewer !== 'query') {
    activateViewer('query');
    renderResultViews();
  }

  // Lazy-load per view
  // System health is NOT gated on isConnected: three of its four checks reach
  // the host, not Elasticsearch, and "the CC is up but ES is down" is precisely
  // the situation this screen exists to show.
  if (name === 'system') loadSystemHealth();
  // Also not gated on isConnected: whether this CC can reach the outside
  // world has nothing to do with whether Elasticsearch is answering, and
  // an ES outage is one of the times you most want to ask.
  if (name === 'connectivity') loadConnectivity();
  if (name === 'attacks'  && isConnected) loadAttacks();
  if (name === 'dashboard'&& isConnected) loadClusterHealth();
  if (name === 'summary'  && isConnected) loadSummary();
  // Sort options depend on the index pattern's real date fields.
  if (name === 'query' && isConnected && typeof loadSortFields === 'function') loadSortFields();

  // MariaDB does not ride the ES connection — it is a separate store with its
  // own reachability, so these must NOT be gated on isConnected.
  if (name === 'maria' && !mariaSchemas.length) loadMariaSchemas();
  if (name === 'mariaquery' && !mariaSchemas.length) loadMariaSchemas();
  // Same reasoning as MariaDB above: PostgreSQL is its own store.
  if (name === 'pg' && !pgDatabases.length) loadPgDatabases();
  if (name === 'pgquery' && !pgDatabases.length) loadPgDatabases();
}

/* ══════════════════════════════════════════════════════════════════════════
   REFRESH + AUTO-REFRESH — every screen can reload its data manually or on a
   fixed interval (the per-view select persists in localStorage). The interval
   only fires while its view is the visible one and a connection is active.
   ══════════════════════════════════════════════════════════════════════════ */
const LS_AUTOREFRESH = 'cc_es_autorefresh_';
const _autoTimers = {};   // view name -> setInterval handle

const REFRESHERS = {
  system:    (manual) => loadSystemHealth(!!manual),
  connectivity: (manual) => loadConnectivity(!!manual),
  dashboard: () => { loadClusterHealth(); loadIndices(); },
  summary:   () => loadSummary(),
  attacks:   () => loadAttacks(),
  index:     () => refreshCurrentIndex(),
  query:     () => refreshQueryResults(),
};

function refreshView(view, manual = true) {
  if (view === 'query' && !(_queryBaseItems && _queryBaseItems.length)) {
    if (manual) showToast('Run a query first — nothing to refresh', 'bg-warning');
    return;
  }
  // `manual` is passed through so a refresher can tell a button press from a
  // timer tick. Only System Health cares: a manual press should say so on
  // screen, a tick must leave the last good reading alone if it fails.
  REFRESHERS[view]?.(manual);
}

/** Refresh whichever results viewer is active (used by the pop-out window). */
function refreshActiveViewer() {
  refreshView(activeViewer === 'index' ? 'index' : 'query');
}

/** Re-run the last executed Query-Editor query (single or multi-index). */
function refreshQueryResults() {
  if (perIndexQueries.length > 1) runMultiQuery(includedPerIndexQueries());
  else runQuery();
}

function onAutoRefreshChanged(view, sel) {
  const secs = parseInt(sel.value) || 0;
  localStorage.setItem(LS_AUTOREFRESH + view, String(secs));
  startAutoRefresh(view, secs);
  showToast(secs ? `Auto-refresh every ${secs}s (while this screen is open)` : 'Auto-refresh off', 'bg-info');
}

function startAutoRefresh(view, secs) {
  if (_autoTimers[view]) { clearInterval(_autoTimers[view]); delete _autoTimers[view]; }
  if (!secs) return;
  _autoTimers[view] = setInterval(() => {
    if (currentView !== view) return;
    // System Health does NOT require an Elasticsearch connection: three of its
    // four checks reach the CC's host, and "ES is the thing that is down" is
    // exactly when an engineer leaves this screen refreshing.
    if (view !== 'system' && !isConnected) return;
    refreshView(view, false);
  }, secs * 1000);
}

/** Restore saved auto-refresh intervals into the selects and start timers. */
function initAutoRefresh() {
  document.querySelectorAll('.auto-refresh-select').forEach(sel => {
    const view  = sel.id.replace('autoRefresh-', '');
    const saved = parseInt(localStorage.getItem(LS_AUTOREFRESH + view) || '0') || 0;
    sel.value = String(saved);
    startAutoRefresh(view, saved);
  });
}

/* ══════════════════════════════════════════════════════════════════════════
   UI LAYOUT — sidebar toggle + resizable query/results panes
   ══════════════════════════════════════════════════════════════════════════ */
const LS_SIDEBAR   = 'cc_es_sidebar_collapsed';
const LS_QUERYSPLIT = 'cc_es_query_split';

/** Show / hide the left main-menu sidebar (persisted across reloads). */
function toggleSidebar() {
  const sb = document.getElementById('sidebar');
  if (!sb) return;
  const collapsed = sb.classList.toggle('collapsed');
  localStorage.setItem(LS_SIDEBAR, collapsed ? '1' : '0');
}

/** Restore saved layout prefs (sidebar state + query split width). */
function initUiPrefs() {
  const sb = document.getElementById('sidebar');
  if (sb && localStorage.getItem(LS_SIDEBAR) === '1') sb.classList.add('collapsed');

  const leftPane = document.getElementById('qeditQueryPane');
  const savedW   = localStorage.getItem(LS_QUERYSPLIT);
  if (leftPane && savedW) leftPane.style.width = savedW;
}

/** Wire up the drag handle that resizes the query vs. results panes. */
function initQuerySplitter() {
  const split  = document.getElementById('qeditSplit');
  const gutter = document.getElementById('qeditGutter');
  const left   = document.getElementById('qeditQueryPane');
  if (!split || !gutter || !left) return;

  let dragging = false;

  gutter.addEventListener('mousedown', (e) => {
    dragging = true;
    gutter.classList.add('dragging');
    document.body.classList.add('qedit-resizing');
    e.preventDefault();
  });

  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const rect = split.getBoundingClientRect();
    const MIN  = 260;                      // keep both panes usable
    const max  = rect.width - MIN - 10;    // 10 = gutter width
    let w = e.clientX - rect.left;
    w = Math.max(MIN, Math.min(max, w));
    left.style.width = w + 'px';
  });

  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    gutter.classList.remove('dragging');
    document.body.classList.remove('qedit-resizing');
    if (left.style.width) localStorage.setItem(LS_QUERYSPLIT, left.style.width);
  });
}

/* ── Results pop-out window ───────────────────────────────────────────────── */
let resultsWindow = null;

/* ── Results views: JSON / Table / CSV ────────────────────────────────────── */
// Two independent viewers (Query Editor + Index Detail) share all the rendering
// and control code; only their container element ids + data/state differ.
const RV_QUERY = { pre: 'queryResults', table: 'queryResultsTable', csv: 'queryResultsCsv',
                   btns: ['rv-json', 'rv-table', 'rv-csv'], meta: 'queryMeta' };
const RV_INDEX = { pre: 'idxResults', table: 'idxResultsTable', csv: 'idxResultsCsv',
                   btns: ['iv2-json', 'iv2-table', 'iv2-csv'], meta: 'idxMeta' };

let lastResultHits  = [];    // hit objects for the ACTIVE viewer
let lastResultTotal = 0;     // total docs MATCHING (may exceed loaded rows)
let lastResultJson  = '';    // text shown in the JSON view
let lastResultCols  = [];    // FROZEN column order (so deleting a field doesn't reorder)
let resultView      = 'json';// 'json' | 'table' | 'csv'
let RV = RV_QUERY;           // active viewer's container ids
let activeContextItems = () => currentQueryItems();  // [{index,query_body}] for export/bulk

const _viewers = {
  query: { ids: RV_QUERY, ctx: () => currentQueryItems(), state: _blankViewerState() },
  index: { ids: RV_INDEX, ctx: () => indexContextItems(), state: _blankViewerState() },
};
let activeViewer = 'query';

function _blankViewerState() {
  return { hits: [], total: 0, json: '', cols: [], view: 'json',
           sort: { col: null, dir: 'asc' }, filters: {},
           selRows: new Set(), selCols: new Set() };
}

/** Save the live globals back into the current viewer, then load another's. */
function activateViewer(name) {
  const cur = _viewers[activeViewer].state;
  cur.hits = lastResultHits; cur.total = lastResultTotal; cur.json = lastResultJson;
  cur.cols = lastResultCols; cur.view = resultView; cur.sort = tableSort; cur.filters = tableFilters;
  cur.selRows = selectedRows; cur.selCols = selectedCols;

  activeViewer = name;
  const v = _viewers[name], s = v.state;
  lastResultHits = s.hits; lastResultTotal = s.total; lastResultJson = s.json;
  lastResultCols = s.cols; resultView = s.view; tableSort = s.sort; tableFilters = s.filters;
  selectedRows = s.selRows; selectedCols = s.selCols;
  RV = v.ids; activeContextItems = v.ctx;
}

/** Write text into the active JSON pane and mirror it to the pop-out. */
function setQueryResults(text) {
  lastResultJson = text;
  const el = document.getElementById(RV.pre);
  if (el) el.textContent = text;
  syncResultsPopout();
}

/** Store the hit rows from a response and refresh the active view. */
function captureResults(data) {
  lastResultHits = Array.isArray(data?.hits) ? data.hits : [];
  lastResultTotal = data?.total ?? data?.total_hits ?? lastResultHits.length;
  lastResultJson = JSON.stringify(data, null, 2);
  lastResultCols = resultColumns(lastResultHits);   // freeze the column order
  tableSort = { col: null, dir: 'asc' };   // reset sort/filter for the new result set
  tableFilters = {};
  if (typeof _distinctCache !== 'undefined') _distinctCache.clear();  // drop cached value lists
  hiddenColumns = new Set();                // fresh result set → all columns visible
  if (typeof selectedRows !== 'undefined') { selectedRows.clear(); selectedCols.clear(); }
  renderResultViews();
}

/** Stable column list: the frozen order + any keys added later (appended). */
function currentColumns() {
  const cols = lastResultCols.slice();
  const seen = new Set(cols);
  for (const h of lastResultHits)
    for (const k of Object.keys(h)) if (!seen.has(k)) { seen.add(k); cols.push(k); }
  return cols;
}

/* ── Column visibility (display only — never touches the data or filters) ──── */
let hiddenColumns   = new Set();   // column names the user chose to hide
let mappedFieldNames = new Set();  // field names declared in the index mapping

/** Meta columns are always available and are never treated as "unmapped". */
function isMetaColumn(c) { return c === '_id' || c === '_index'; }

/** True when a column is NOT declared in the index mapping (mapping known). */
function isUnmappedColumn(c) {
  if (isMetaColumn(c)) return false;
  if (!mappedFieldNames.size) return false;   // mapping unknown → treat all as mapped
  return !mappedFieldNames.has(c);
}

/** Columns actually rendered = frozen order minus the ones the user hid. */
function visibleColumns() {
  return currentColumns().filter(c => !hiddenColumns.has(c));
}


/** Context items for the Index Detail viewer (whole index, match_all). */
function indexContextItems() {
  if (!_currentIndexName) return null;
  return [{ index: _currentIndexName, query_body: { query: { match_all: {} } } }];
}

/** Ordered column list: _id, _index first, then keys in first-seen order. */
function resultColumns(hits) {
  const seen = new Set(), cols = [];
  for (const k of ['_id', '_index']) if (hits.some(h => k in h)) { seen.add(k); cols.push(k); }
  for (const h of hits) for (const k of Object.keys(h)) if (!seen.has(k)) { seen.add(k); cols.push(k); }
  return cols;
}

/** Scalar → string; objects/arrays → compact JSON. */
function cellValue(v) {
  if (v == null) return '';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

/* ── Date columns: display human-readable, match on the raw epoch value ─────
 * CC stores dates as epoch-millis. We keep the RAW value everywhere it's used
 * for matching/filtering/sending to ES, and only format it for DISPLAY (table
 * cells, filter labels, CSV view). */
let mappedDateFields = new Set();   // date-typed fields of the index-detail index

/** Date column names for the active viewer. Index viewer → the index mapping's
 *  date fields; Query editor → the union of each executed group's date_fields. */
function activeDateColumns() {
  if (activeViewer === 'index') return mappedDateFields;
  if (activeViewer === 'query' && Array.isArray(perIndexQueries)) {
    const s = new Set();
    for (const q of perIndexQueries) for (const f of (q.date_fields || [])) s.add(f);
    return s;
  }
  return new Set();
}

/** Format an epoch-millis (or 10-digit epoch-seconds) value as UTC
 *  'YYYY-MM-DD HH:mm:ss UTC'. Returns null when v isn't a usable epoch. */
function fmtEpochDisplay(v) {
  if (v == null || v === '') return null;
  const s = String(v).trim();
  if (!/^\d{10,}$/.test(s)) return null;       // not a bare epoch number
  let n = Number(s);
  if (!Number.isFinite(n)) return null;
  if (n < 1e12) n *= 1000;                       // 10-digit seconds → millis
  const d = new Date(n);
  if (isNaN(d.getTime())) return null;
  const p = (x) => String(x).padStart(2, '0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} `
       + `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())} UTC`;
}

/** Display string for a cell: date columns are formatted human-readable,
 *  everything else uses cellValue. `isDate` is precomputed by the caller. */
function displayCell(v, isDate) {
  if (isDate) { const s = fmtEpochDisplay(v); if (s != null) return s; }
  return cellValue(v);
}

/** Return a parsed object/array if the value is (or encodes) JSON, else null. */
function asJsonObject(v) {
  if (v && typeof v === 'object') return v;
  if (typeof v === 'string') {
    const t = v.trim();
    if ((t.startsWith('{') && t.endsWith('}')) || (t.startsWith('[') && t.endsWith(']'))) {
      try {
        const parsed = JSON.parse(t);
        if (parsed && typeof parsed === 'object') return parsed;
      } catch (_) {}
    }
  }
  return null;
}

/** CSV text for `hits`.
 *
 *  On screen, date columns render human-readable. Files written to disk pass
 *  `rawDates` so they carry the value Elasticsearch actually stores (epoch
 *  millis): a readable date re-imports as a STRING, which a real CC mapping
 *  rejects outright and a fresh index silently maps as text — breaking time
 *  filters and date sorting on it. Keep exports round-trippable. */
function buildResultsCsv(hits, cols, rawDates = false) {
  if (!hits.length) return '';
  cols = cols || resultColumns(hits);
  const dateCols = rawDates ? new Set() : activeDateColumns();
  const esc = (s) => /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  const lines = [cols.map(esc).join(',')];
  for (const h of hits) lines.push(cols.map(c => esc(displayCell(h[c], dateCols.has(c)))).join(','));
  return lines.join('\r\n');
}

/** Switch the visible results view and (re)render it. */
function setResultView(mode) {
  resultView = mode;
  const map = { json: RV.btns[0], table: RV.btns[1], csv: RV.btns[2] };
  ['json', 'table', 'csv'].forEach(m => {
    document.getElementById(map[m])?.classList.toggle('active', m === mode);
  });
  renderResultViews();
}

/** Build the results table HTML (shared by the main pane and the pop-out). */
function resultsTableHtml(hits) {
  if (!hits.length) return '<div class="text-secondary p-2">No rows to display.</div>';
  const cols = resultColumns(hits);
  const dateCols = activeDateColumns();
  return `<table class="table table-sm table-striped table-hover mb-0" style="white-space:nowrap;">
      <thead class="table-dark"><tr>${cols.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead>
      <tbody>${hits.map(h =>
        `<tr>${cols.map(c => `<td>${esc(displayCell(h[c], dateCols.has(c)))}</td>`).join('')}</tr>`).join('')}</tbody>
    </table>`;
}

function renderResultViews() {
  const pre   = document.getElementById(RV.pre);
  const tbl   = document.getElementById(RV.table);
  const csvEl = document.getElementById(RV.csv);
  if (!pre || !tbl || !csvEl) return;

  pre.classList.toggle('d-none',   resultView !== 'json');
  tbl.classList.toggle('d-none',   resultView !== 'table');
  csvEl.classList.toggle('d-none', resultView !== 'csv');

  if (resultView === 'json') {
    pre.textContent = lastResultJson;
  } else if (resultView === 'table') {
    renderMainResultsTable();
  } else if (resultView === 'csv') {
    csvEl.textContent = lastResultHits.length ? buildResultsCsv(lastResultHits, visibleColumns()) : 'No rows to display.';
  }
  syncResultsPopout();   // keep the detached window in sync with the active view
  syncResultActionButtons();
}

/** "Modify results" / "Delete results" act on what the query matched, so they
 *  stay disabled until a query has actually returned rows. Only the Query
 *  Editor has them (the Index Detail viewer has its own row tools). */
function syncResultActionButtons() {
  const on = activeViewer === 'query' && lastResultHits.length > 0;
  ['btnModifyResults', 'btnDeleteResults'].forEach(id => {
    const b = document.getElementById(id);
    if (!b) return;
    if (!b.dataset.t) b.dataset.t = b.title;     // stash BEFORE overwriting
    b.disabled = !on;
    b.title = on ? b.dataset.t : 'Run a query that returns documents first';
  });
}

/* ── Rich table: sticky header, per-column sort + value filter, cell edit ── */
let tableSort    = { col: null, dir: 'asc' };
let tableFilters = {};   // { colName: Set(value strings) }
let writeMode    = false; // edit/delete controls are gated behind this (off by default)
const SCOPE_AUTO_LIMIT = 10000; // ≤ this many matches → apply to all without asking

/** Toggle write mode — enables the in-cell edit/delete controls (ES writes). */
function toggleWriteMode() {
  writeMode = !writeMode;
  if (!writeMode && typeof selectedRows !== 'undefined') { selectedRows.clear(); selectedCols.clear(); }
  for (const b of document.querySelectorAll('.js-write-toggle')) {
    b.classList.toggle('btn-warning', writeMode);
    b.classList.toggle('btn-outline-secondary', !writeMode);
    b.innerHTML = writeMode
      ? '<i class="bi bi-unlock me-1"></i>Write'
      : '<i class="bi bi-lock me-1"></i>Read-only';
  }
  renderResultViews();   // refresh main + pop-out (controls appear/disappear)
}

const jsq    = (s) => String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'");
const cssId  = (s) => 'flt-' + String(s).replace(/[^a-zA-Z0-9_-]/g, '_');

/** Rows passing every active filter EXCEPT the one on `exceptCol`.
 *  Used for Excel-style cascading filter option lists. */
function rowsPassingFiltersExcept(exceptCol) {
  let rows = lastResultHits;
  for (const [col, set] of Object.entries(tableFilters)) {
    if (col === exceptCol) continue;
    if (set && set.size) rows = rows.filter(r => set.has(cellValue(r[col])));
  }
  return rows;
}

/** Distinct values a column's filter should offer. Excel-style: only values that
 *  still exist in rows already narrowed by the OTHER active filters.
 *  This is the INSTANT (loaded-rows) list; toggleColFilter then augments it with
 *  the full distinct set fetched from Elasticsearch. */
function columnUniqueValues(col) {
  const set = new Set();
  for (const r of rowsPassingFiltersExcept(col)) set.add(cellValue(r[col]));
  return [...set].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
}

/** Indices to aggregate over for a column's distinct-value list.
 *  In the Query Editor viewer, use the executed query's index patterns so the
 *  aggregation covers the WHOLE pattern (not just concrete indices that happen
 *  to appear in the loaded page). Otherwise use the loaded rows' _index values,
 *  falling back to the current index-detail index. */
function filterContextIndices() {
  if (activeViewer === 'query' && _queryBaseItems && _queryBaseItems.length) {
    const set = new Set(_queryBaseItems.map(it => it.index).filter(Boolean));
    if (set.size) return [...set];
  }
  const set = new Set();
  for (const r of lastResultHits) if (r && r._index) set.add(r._index);
  if (!set.size && _currentIndexName) set.add(_currentIndexName);
  return [...set];
}

// Cache distinct-value fetches so re-opening a filter is instant.
// Key = indices | field | cascading-filters signature.
const _distinctCache = new Map();

/** Fetch the FULL distinct value set for `col` from Elasticsearch via a terms
 *  aggregation, honoring the other active filters (Excel-style cascading over
 *  the whole index rather than only the loaded page). Returns string values. */
async function fetchColumnDistinct(col) {
  const indices = filterContextIndices();
  if (!indices.length) return [];
  if (col === '_id' || col === '_index') return [];   // meta cols: use loaded values

  const clauses = buildFilterMustClauses(col);        // cascading: other filters
  // In the Query Editor viewer with a single base query, constrain the distinct
  // values to docs the base query matches — so selecting one never dead-ends.
  if (activeViewer === 'query' && _queryBaseItems && _queryBaseItems.length === 1) {
    const base = _queryBaseItems[0].query_body?.query;
    if (base && !base.match_all) clauses.unshift(base);
  }
  const query = clauses.length ? { bool: { must: clauses } } : { match_all: {} };
  const key   = JSON.stringify([indices, col, clauses]);
  if (_distinctCache.has(key)) return _distinctCache.get(key);

  try {
    const res = await api('/api/indices/field-values', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ indices, field: col, query, size: 1000 }),
    });
    const vals = Array.isArray(res?.values) ? res.values.map(String) : [];
    _distinctCache.set(key, vals);
    return vals;
  } catch (e) {
    return [];
  }
}

function tableDisplayRows() {
  let rows = lastResultHits.slice();
  for (const [col, set] of Object.entries(tableFilters)) {
    if (set && set.size) rows = rows.filter(r => set.has(cellValue(r[col])));
  }
  if (tableSort.col != null) {
    const c = tableSort.col, dir = tableSort.dir === 'desc' ? -1 : 1;
    rows.sort((a, b) => {
      const va = cellValue(a[c]), vb = cellValue(b[c]);
      const na = parseFloat(va), nb = parseFloat(vb);
      const numeric = !isNaN(na) && !isNaN(nb) && va.trim() !== '' && vb.trim() !== '';
      const cmp = numeric ? (na - nb) : va.localeCompare(vb, undefined, { numeric: true });
      return cmp * dir;
    });
  }
  return rows;
}

const ROWSEP = '||';
const rowKey = (r) => `${r._index}${ROWSEP}${r._id}`;
let selectedRows = new Set();   // keys "index\0id"
let selectedCols = new Set();   // column names (editable fields)

/** Full interactive table HTML — shared by the main pane and the pop-out.
 *  All onclick handlers use bare function names; the pop-out window has these
 *  bound to the opener's functions, so the same markup works in both. */
function buildInteractiveTableHtml() {
  if (!lastResultHits.length) return '<div class="text-secondary p-2">No rows to display.</div>';

  const cols = currentColumns();          // frozen order — deleting a field won't reorder
  const shownCols = cols.filter(c => !hiddenColumns.has(c));   // display-only visibility
  const rows = tableDisplayRows();
  const dateCols = activeDateColumns();   // date columns → human-readable display
  const editable = (c) => c !== '_id' && c !== '_index';
  const sel = writeMode;   // selection/edit UI only in write mode

  for (const c of [...selectedCols]) if (!shownCols.includes(c)) selectedCols.delete(c);
  const allVisibleSelected = sel && rows.length > 0 && rows.every(r => selectedRows.has(rowKey(r)));

  const headSelTh = sel
    ? `<th class="rt-selcol"><input type="checkbox" ${allVisibleSelected ? 'checked' : ''}
         onclick="toggleAllRows(this)" title="Select all visible rows"/></th>` : '';

  const ths = shownCols.map(c => {
    const arrow    = tableSort.col === c ? (tableSort.dir === 'asc' ? '▲' : '▼') : '';
    const filtered = tableFilters[c] && tableFilters[c].size;
    const colChk = (sel && editable(c))
      ? `<input type="checkbox" class="rt-colsel" ${selectedCols.has(c) ? 'checked' : ''}
           onclick="toggleColSel('${jsq(c)}', this)" title="Select column"/>` : '';
    // Active-filter summary shown on its OWN line below the name so it can never hide it.
    // Date columns are shown human-readable (raw epoch is kept for matching).
    const filterSummary = filtered
      ? [...tableFilters[c]].map(v => displayCell(v, dateCols.has(c))).join(', ') : '';
    const filterInfo = filtered
      ? `<div class="rt-th-filterval" title="Filtered to: ${esc(filterSummary)}">= ${esc(filterSummary)}</div>`
      : '';
    return `<th class="rt-th${filtered ? ' filtered' : ''}">
        <div class="rt-th-row">
          ${colChk}
          <span class="rt-sort" onclick="toggleTableSort('${jsq(c)}')">${esc(c)} <span class="rt-arrow">${arrow}</span></span>
          <button class="rt-funnel" onclick="toggleColFilter(event,'${jsq(c)}')" title="Filter values">
            <i class="bi bi-funnel${filtered ? '-fill' : ''}"></i>
          </button>
        </div>
        ${filterInfo}
        <div class="rt-filter d-none" id="${cssId(c)}"></div>
      </th>`;
  }).join('');

  const body = rows.map((r, ri) => {
    const id = r._id, idx = r._index;
    const canEdit = sel && id != null && idx != null;
    const key = rowKey(r);
    const rowChk = sel
      ? `<td class="rt-selcol"><input type="checkbox" ${selectedRows.has(key) ? 'checked' : ''}
           onclick="toggleRowSel('${jsq(key)}', this)"/></td>` : '';
    return '<tr>' + rowChk + shownCols.map(c => {
      if (!editable(c)) return `<td>${esc(displayCell(r[c], dateCols.has(c)))}</td>`;

      // Field absent from this document → red-gray cell with tooltip + "add" control.
      if (!(c in r)) {
        const addBtn = canEdit ? `<span class="rt-cellctrl">
            <button onclick="editCell('${jsq(id)}','${jsq(idx)}','${jsq(c)}', this)" title="Add this field"><i class="bi bi-plus-lg"></i></button>
          </span>` : '';
        return `<td class="rt-cell rt-cell-missing" title="This field does not exist in this document">
          <span class="rt-missing">—</span>${addBtn}</td>`;
      }

      const isJson = asJsonObject(r[c]) !== null;
      const val = esc(displayCell(r[c], dateCols.has(c)));
      let ctrlBtns = '';
      if (isJson) {
        ctrlBtns += `<button onclick="showJsonCell(${ri},'${jsq(c)}', this)" title="View as pretty JSON"><i class="bi bi-braces"></i></button>`;
      }
      if (canEdit) {
        ctrlBtns += `<button onclick="editCell('${jsq(id)}','${jsq(idx)}','${jsq(c)}', this)" title="Edit value"><i class="bi bi-pencil"></i></button>
          <button class="rt-del" onclick="deleteCell('${jsq(id)}','${jsq(idx)}','${jsq(c)}', this)" title="Delete field from document"><i class="bi bi-trash"></i></button>`;
      }
      const ctrls = ctrlBtns ? `<span class="rt-cellctrl">${ctrlBtns}</span>` : '';
      return `<td class="rt-cell"><span class="rt-val" title="${val}">${val}</span>${ctrls}</td>`;
    }).join('') + '</tr>';
  }).join('');

  const bar = selectionBarHtml();
  return `<div id="rtSelBar" class="${bar ? 'rt-selbar' : ''}">${bar}</div>
     ${fieldVisibilityBannerHtml()}
     <table class="table table-sm table-striped table-hover mb-0 rt-table">
       <thead><tr>${headSelTh}${ths}</tr></thead><tbody>${body}</tbody></table>`;
}

/** Banner above the table summarising hidden columns (esp. unmapped ones). */
function fieldVisibilityBannerHtml() {
  const hidden = currentColumns().filter(c => hiddenColumns.has(c));
  if (!hidden.length) return '';
  const unmapped = hidden.filter(isUnmappedColumn).length;
  const mapped   = hidden.length - unmapped;
  let parts = [];
  if (mapped)   parts.push(`${mapped} field${mapped > 1 ? 's' : ''}`);
  if (unmapped) parts.push(`${unmapped} unmapped field${unmapped > 1 ? 's' : ''}`);
  const summary = parts.join(' + ');
  return `<div class="rt-fieldbanner">
      <i class="bi bi-eye-slash"></i>
      <span>${summary} hidden</span>
      <button class="btn btn-sm btn-link p-0" onclick="showAllColumns()">Show all</button>
      <button class="btn btn-sm btn-link p-0" onclick="openFieldVisibility(this)">Manage fields…</button>
    </div>`;
}

/* ── Column-visibility controls (view only) ──────────────────────────────── */

/** Show every column again. */
function showAllColumns() {
  hiddenColumns.clear();
  refreshTables();
}

/** Hide/unhide a single column and re-render live. */
function setColumnHidden(col, hide) {
  if (hide) hiddenColumns.add(col); else hiddenColumns.delete(col);
  refreshTables();
  renderFieldVisibilityBody();
}

/** Hide (or reveal) every unmapped column at once. */
function setUnmappedHidden(hide) {
  for (const c of currentColumns()) {
    if (!isUnmappedColumn(c)) continue;
    if (hide) hiddenColumns.add(c); else hiddenColumns.delete(c);
  }
  refreshTables();
  renderFieldVisibilityBody();
}

/** Check/uncheck every mapped column. */
function setAllMappedHidden(hide) {
  for (const c of currentColumns()) {
    if (isUnmappedColumn(c) || isMetaColumn(c)) continue;
    if (hide) hiddenColumns.add(c); else hiddenColumns.delete(c);
  }
  refreshTables();
  renderFieldVisibilityBody();
}

/** Document the Field Visibility modal currently lives in (main or pop-out). */
let _fieldVisDoc = document;

/** Open the "Field Visibility" picker. Pass the clicked element (or nothing)
 *  — the modal renders in that element's document, so the same button works
 *  in the main window and in the results pop-out. */
function openFieldVisibility(el) {
  const doc = (el && el.ownerDocument) || document;
  const existing = doc.querySelector('.rt-modal-overlay.rt-fieldvis');
  if (existing) { existing.remove(); return; }
  _fieldVisDoc = doc;
  const wrap = doc.createElement('div');
  wrap.className = 'rt-modal-overlay rt-fieldvis';
  wrap.innerHTML = `<div class="rt-modal rt-modal-fields">
      <div class="rt-modal-title"><i class="bi bi-eye me-1"></i>Field Visibility</div>
      <input class="form-control form-control-sm rt-fieldvis-search mb-2"
             placeholder="search fields…" oninput="renderFieldVisibilityBody()"/>
      <div class="rt-fieldvis-body"></div>
      <div class="rt-modal-actions">
        <button class="btn btn-sm btn-secondary" data-act="close">Close</button>
      </div>
    </div>`;
  doc.body.appendChild(wrap);
  const done = () => { wrap.remove(); doc.removeEventListener('keydown', onKey); };
  const onKey = (e) => { if (e.key === 'Escape') done(); };
  doc.addEventListener('keydown', onKey);
  wrap.addEventListener('click', (e) => {
    if (e.target === wrap || e.target.dataset.act === 'close') done();
  });
  renderFieldVisibilityBody();
}

/** (Re)draw the checklist inside the open Field Visibility modal. */
function renderFieldVisibilityBody() {
  const doc = (_fieldVisDoc && !_fieldVisDoc.defaultView?.closed) ? _fieldVisDoc : document;
  const host = doc.querySelector('.rt-fieldvis-body');
  if (!host) return;
  const searchEl = doc.querySelector('.rt-fieldvis-search');
  const q = (searchEl?.value || '').toLowerCase();

  const cols = currentColumns().filter(isColumnPickable);
  const mapped   = cols.filter(c => !isUnmappedColumn(c));
  const unmapped = cols.filter(isUnmappedColumn);

  const mappedHiddenCount = mapped.filter(c => hiddenColumns.has(c)).length;
  const allMappedShown    = mappedHiddenCount === 0;
  const unmappedHidden    = unmapped.length && unmapped.every(c => hiddenColumns.has(c));

  const row = (c) => {
    if (q && !c.toLowerCase().includes(q)) return '';
    const shown = !hiddenColumns.has(c);
    const badge = isUnmappedColumn(c)
      ? `<span class="rt-field-badge" title="Not declared in the index mapping">unmapped</span>` : '';
    return `<label class="rt-field-row">
        <input type="checkbox" ${shown ? 'checked' : ''}
               onchange="setColumnHidden('${jsq(c)}', !this.checked)"/>
        <span class="rt-field-name">${esc(c)}</span>${badge}
      </label>`;
  };

  let html = `<div class="rt-field-group">
      <label class="rt-field-master">
        <input type="checkbox" ${allMappedShown ? 'checked' : ''}
               onchange="setAllMappedHidden(!this.checked)"/>
        <span>All mapped fields (${mapped.length})</span>
      </label>
      ${mapped.map(row).join('')}
    </div>`;

  if (unmapped.length) {
    html += `<div class="rt-field-group rt-field-group-unmapped">
        <label class="rt-field-master">
          <input type="checkbox" ${!unmappedHidden ? 'checked' : ''}
                 onchange="setUnmappedHidden(!this.checked)"/>
          <span>Unmapped fields (${unmapped.length})</span>
        </label>
        ${unmapped.map(row).join('')}
      </div>`;
  }
  host.innerHTML = html;
}

/** _id / _index are structural — keep them out of the picker checklist. */
function isColumnPickable(c) { return !isMetaColumn(c); }

/* ── Aggregate results by field(s) ────────────────────────────────────────── */

/** Columns whose loaded values look numeric (candidates for the metric). */
function numericColumns() {
  const cols = currentColumns().filter(c => c !== '_id' && c !== '_index');
  return cols.filter(c => lastResultHits.some(h =>
    typeof h[c] === 'number' && !Number.isNaN(h[c])));
}

/** Open the group-by aggregation dialog (doc-aware — works in the pop-out). */
function openAggregateDialog(el) {
  const doc = (el && el.ownerDocument) || document;
  doc.querySelector('.rt-modal-overlay.rt-aggregate')?.remove();

  const items = activeContextItems();
  if (!items) { showToast('Invalid query JSON — cannot aggregate', 'bg-danger'); return; }
  const cols = currentColumns().filter(c => c !== '_id');
  if (!cols.length) { showToast('Run a query first — no fields to aggregate', 'bg-warning'); return; }
  const numeric = numericColumns();

  const wrap = doc.createElement('div');
  wrap.className = 'rt-modal-overlay rt-aggregate';
  wrap.innerHTML = `<div class="rt-modal" style="min-width:520px;max-width:860px;">
      <div class="rt-modal-title"><i class="bi bi-bar-chart me-1"></i>Aggregate results</div>
      <div class="rt-modal-body">
        <label class="small text-secondary mb-1">Group by field(s) — nested in the order checked</label>
        <div class="agg-fields rt-fieldvis-body mb-2" style="max-height:180px;overflow:auto;">
          ${cols.map(c => `<label class="rt-field-row">
              <input type="checkbox" value="${esc(c)}"/>
              <span class="rt-field-name">${esc(c)}</span>
            </label>`).join('')}
        </div>
        <div class="d-flex align-items-center gap-2 mb-2 flex-wrap">
          <label class="small text-secondary mb-0">Metric (numeric field)</label>
          <select class="form-select form-select-sm agg-metric" style="width:200px;">
            <option value="">— count only —</option>
            ${numeric.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('')}
          </select>
          <label class="small text-secondary mb-0 ms-2">Max groups</label>
          <input type="number" class="form-control form-control-sm agg-size" value="100" min="1" max="1000" style="width:90px;"/>
        </div>
        <div class="agg-status small text-secondary d-none"></div>
        <div class="agg-results mt-2" style="max-height:320px;overflow:auto;"></div>
      </div>
      <div class="rt-modal-actions">
        <button class="btn btn-sm btn-primary" data-act="run"><i class="bi bi-play-fill me-1"></i>Aggregate</button>
        <button class="btn btn-sm btn-outline-success d-none" data-act="csv"><i class="bi bi-download me-1"></i>CSV</button>
        <button class="btn btn-sm btn-secondary" data-act="close">Close</button>
      </div>
    </div>`;
  doc.body.appendChild(wrap);

  let lastAgg = null;   // {rows, cols} of the last run, for the CSV download

  const done = () => { wrap.remove(); doc.removeEventListener('keydown', onKey); };
  const onKey = (e) => { if (e.key === 'Escape') done(); };
  doc.addEventListener('keydown', onKey);

  const status = (msg) => {
    const s = wrap.querySelector('.agg-status');
    s.textContent = msg || '';
    s.classList.toggle('d-none', !msg);
  };

  async function run() {
    const groupBy = [...wrap.querySelectorAll('.agg-fields input:checked')].map(b => b.value);
    if (!groupBy.length) { status('Check at least one field to group by.'); return; }
    const metric = wrap.querySelector('.agg-metric').value;
    const size   = Math.max(1, Math.min(1000, parseInt(wrap.querySelector('.agg-size').value) || 100));

    // Aggregate what the user is LOOKING at: base query + active column filters.
    const filterClauses = buildFilterMustClauses();
    const merged = items.map(it => ({
      index: it.index,
      query_body: { query: mergeQueryWithFilters(
        (it.query_body || {}).query || { match_all: {} }, filterClauses) },
    }));

    status('Aggregating…');
    const res = await api('/api/query/aggregate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ per_index_queries: merged, group_by: groupBy,
                             metric_field: metric, size }),
    });
    if (!res || res.error) { status('✗ ' + (res?.error || 'unknown error')); return; }

    const hasMetric = !!metric && res.rows.some(r => 'sum' in r);
    const outCols = [...groupBy, 'count', ...(hasMetric ? ['sum', 'avg', 'min', 'max'] : [])];
    lastAgg = { rows: res.rows, cols: outCols };
    status(`${res.rows.length.toLocaleString()} group(s)`
      + (hasMetric ? ` · metric: ${metric}` : '')
      + (res.truncated ? ' · ⚠ truncated — raise Max groups' : ''));
    wrap.querySelector('[data-act="csv"]').classList.toggle('d-none', !res.rows.length);
    wrap.querySelector('.agg-results').innerHTML = res.rows.length
      ? `<table class="table table-sm table-striped table-hover mb-0" style="white-space:nowrap;font-size:0.78rem;">
          <thead class="table-dark"><tr>${outCols.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead>
          <tbody>${res.rows.map(r =>
            `<tr>${outCols.map(c => `<td>${esc(r[c] ?? '')}</td>`).join('')}</tr>`).join('')}</tbody>
        </table>`
      : '<div class="text-secondary p-2">No groups found.</div>';
  }

  function downloadCsv() {
    if (!lastAgg || !lastAgg.rows.length) return;
    const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    const blob = new Blob([buildResultsCsv(lastAgg.rows, lastAgg.cols, true)],
                          { type: 'text/csv;charset=utf-8' });
    const a = doc.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `cc_aggregation_${ts}.csv`;
    doc.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
  }

  wrap.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) { if (e.target === wrap) done(); return; }
    const act = b.getAttribute('data-act');
    if (act === 'run') run();
    else if (act === 'csv') downloadCsv();
    else if (act === 'close') done();
  });
}


function renderMainResultsTable() {
  const host = document.getElementById(RV.table);
  if (host) host.innerHTML = buildInteractiveTableHtml();
}

/* Re-render the active view in the main window AND the pop-out. */
function refreshTables() { renderResultViews(); }

/** Inner HTML of the selection action bar, derived from selection state. */
function selectionBarHtml() {
  const nRows = selectedRows.size, cols = [...selectedCols];
  if (!nRows && !cols.length) return '';
  let html = '';
  if (nRows) {
    html += `<span class="me-2"><b>${nRows}</b> row${nRows > 1 ? 's' : ''} selected</span>
      <button class="btn btn-sm btn-danger py-0 px-2 me-3" onclick="deleteSelectedRows(this)">
        <i class="bi bi-trash me-1"></i>Delete document${nRows > 1 ? 's' : ''}</button>`;
  }
  if (cols.length) {
    const where = nRows ? `on ${nRows} selected row${nRows > 1 ? 's' : ''}` : 'whole column';
    html += `<span class="me-2">Field${cols.length > 1 ? 's' : ''} <b>${cols.map(esc).join(', ')}</b> (${where})</span>
      <button class="btn btn-sm btn-warning py-0 px-2 me-1" onclick="columnFieldOp('set', this)">
        <i class="bi bi-pencil me-1"></i>Edit</button>
      <button class="btn btn-sm btn-danger py-0 px-2 me-3" onclick="columnFieldOp('delete', this)">
        <i class="bi bi-trash me-1"></i>Delete field</button>`;
  }
  html += `<button class="btn btn-sm btn-outline-secondary py-0 px-2" onclick="clearSelection()">Clear</button>`;
  return html;
}

/* ── Selection state + bulk actions ──────────────────────────────────────── */
function toggleRowSel(key, cb) {
  if (cb.checked) selectedRows.add(key); else selectedRows.delete(key);
  refreshTables();
}
function toggleAllRows(cb) {
  for (const r of tableDisplayRows()) {
    const k = rowKey(r);
    if (cb.checked) selectedRows.add(k); else selectedRows.delete(k);
  }
  refreshTables();
}
function toggleColSel(col, cb) {
  if (cb.checked) selectedCols.add(col); else selectedCols.delete(col);
  refreshTables();
}

function clearSelection() {
  selectedRows.clear(); selectedCols.clear();
  refreshTables();
}

/** Build [{index, query_body}] for the current query (multi or single). */
function currentQueryItems() {
  if (perIndexQueries.length > 1) {
    // Only the groups the user kept — export / delete / modify must act on
    // exactly what "Run All" ran, never on skipped indices.
    return includedPerIndexQueries().map(p => ({ index: p.index, query_body: p.query_body }));
  }
  let qb;
  try { qb = JSON.parse(document.getElementById('queryBody').value); }
  catch (e) { return null; }
  return [{ index: document.getElementById('queryIndex').value.trim(), query_body: qb }];
}

async function deleteSelectedRows(el) {
  if (!writeMode) { showToast('Enable Write mode first', 'bg-warning'); return; }
  const doc = el ? el.ownerDocument : document;
  const docs = [...selectedRows].map(k => { const [index, id] = k.split(ROWSEP); return { index, id }; });
  if (!docs.length) return;
  if (!await confirmSharedCc(`delete ${docs.length} document(s)`, doc)) return;

  // When a column filter is active and more docs match the same filter
  // server-side than are loaded here (Show limit smaller than the match total),
  // offer a choice: delete just the selected rows, or every doc matching the
  // filter (scrolled server-side). Mirrors the download flow.
  const hasFilters    = Object.keys(tableFilters).some(k => tableFilters[k]?.size);
  const loaded        = lastResultHits.length;
  const totalMatching = lastResultTotal || loaded;
  const moreOnServer  = hasFilters && loaded < totalMatching && !!_currentIndexName;

  let scope = 'selected';
  if (moreOnServer) {
    const choice = await uiChoice(doc, {
      title: 'Delete filtered documents',
      message: `${docs.length.toLocaleString()} doc(s) are selected here, but `
             + `${totalMatching.toLocaleString()} docs match the same filter server-side `
             + `(only ${loaded.toLocaleString()} are loaded). This permanently deletes `
             + `documents and cannot be undone. What do you want to delete?`,
      buttons: [
        { value: 'selected', text: `Selected only (${docs.length.toLocaleString()})`,     cls: 'btn-danger' },
        { value: 'all',      text: `All matching filter (${totalMatching.toLocaleString()})`, cls: 'btn-danger' },
        { value: null,       text: 'Cancel', cls: 'btn-outline-secondary' },
      ],
    });
    if (choice == null) return;
    scope = choice;
  } else if (!await uiConfirm(doc, { title: `Delete ${docs.length} document(s) from Elasticsearch?`,
      message: 'This permanently deletes the selected documents and cannot be undone.',
      okText: 'Delete', danger: true })) {
    return;
  }

  let res;
  if (scope === 'all') {
    const must = buildFilterMustClauses();
    const query_body = must.length ? { query: { bool: { must } } } : { query: { match_all: {} } };
    res = await api('/api/docs/bulk-delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope: 'all', per_index_queries: [{ index: _currentIndexName, query_body }] }),
    });
  } else {
    res = await api('/api/docs/bulk-delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope: 'selected', docs }),
    });
  }
  if (!res || res.error) { showToast('Delete failed: ' + (res?.error || 'unknown'), 'bg-danger'); return; }

  if (scope === 'all') {
    selectedRows.clear();
    showToast(`Deleted ${res.deleted} document(s) matching the filter`, 'bg-success');
    if (typeof refreshCurrentIndex === 'function') refreshCurrentIndex();   // loaded slice is now stale
  } else {
    const keyset = new Set(selectedRows);
    lastResultHits = lastResultHits.filter(r => !keyset.has(rowKey(r)));
    selectedRows.clear();
    renderResultViews();
    showToast(`Deleted ${res.deleted} document(s)`, 'bg-success');
  }
}

/* ── Doc-aware modals (render in the window the user is working in) ────────── */

/** Text-input modal → resolves to the string, or null on cancel. */
function uiPrompt(doc, opts) {
  doc = doc || document;
  return new Promise(resolve => {
    const wrap = doc.createElement('div');
    wrap.className = 'rt-modal-overlay';
    wrap.innerHTML = `<div class="rt-modal">
        <div class="rt-modal-title">✎ ${esc(opts.title || 'Edit')}</div>
        <input class="form-control rt-modal-input" value="${esc(opts.value ?? '')}"/>
        <div class="rt-modal-actions">
          <button class="btn btn-sm btn-primary" data-ok="1">${esc(opts.okText || 'Save')}</button>
          <button class="btn btn-sm btn-outline-secondary" data-ok="0">Cancel</button>
        </div></div>`;
    doc.body.appendChild(wrap);
    const input = wrap.querySelector('.rt-modal-input');
    const done = (v) => { wrap.remove(); doc.removeEventListener('keydown', onKey); resolve(v); };
    const onKey = (e) => { if (e.key === 'Escape') done(null); else if (e.key === 'Enter') done(input.value); };
    doc.addEventListener('keydown', onKey);
    wrap.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b) { done(b.getAttribute('data-ok') === '1' ? input.value : null); return; }
      if (e.target === wrap) done(null);
    });
    setTimeout(() => { input.focus(); input.select(); }, 0);
  });
}

/** Confirm modal → resolves to true/false. */
function uiConfirm(doc, opts) {
  doc = doc || document;
  return new Promise(resolve => {
    const danger = !!opts.danger;
    const wrap = doc.createElement('div');
    wrap.className = 'rt-modal-overlay';
    wrap.innerHTML = `<div class="rt-modal">
        <div class="rt-modal-title">${danger ? '🗑' : '❓'} ${esc(opts.title || 'Confirm')}</div>
        ${opts.message ? `<div class="rt-modal-body">${esc(opts.message)}</div>` : ''}
        <div class="rt-modal-actions">
          <button class="btn btn-sm ${danger ? 'btn-danger' : 'btn-primary'}" data-ok="1">${esc(opts.okText || 'OK')}</button>
          <button class="btn btn-sm btn-outline-secondary" data-ok="0">Cancel</button>
        </div></div>`;
    doc.body.appendChild(wrap);
    const done = (v) => { wrap.remove(); doc.removeEventListener('keydown', onKey); resolve(v); };
    const onKey = (e) => { if (e.key === 'Escape') done(false); };
    doc.addEventListener('keydown', onKey);
    wrap.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b) { done(b.getAttribute('data-ok') === '1'); return; }
      if (e.target === wrap) done(false);
    });
  });
}

/** Multi-button choice modal → resolves to the chosen button's `value`
 *  (or `null` when dismissed via Escape / backdrop click). */
function uiChoice(doc, opts) {
  doc = doc || document;
  const buttons = opts.buttons || [{ value: true, text: 'OK', cls: 'btn-primary' }];
  return new Promise(resolve => {
    const wrap = doc.createElement('div');
    wrap.className = 'rt-modal-overlay';
    const btnHtml = buttons.map((b, i) =>
      `<button class="btn btn-sm ${b.cls || 'btn-outline-secondary'}" data-idx="${i}">${esc(b.text)}</button>`
    ).join('');
    // The icon is an option because this modal is no longer only used for
    // downloads: a three-way "download and delete / delete / cancel" led with
    // a download arrow over a destructive question, which is the wrong signal
    // on the one dialog that most needs the right one.
    wrap.innerHTML = `<div class="rt-modal">
        <div class="rt-modal-title">${opts.icon || '⬇'} ${esc(opts.title || 'Choose')}</div>
        ${opts.message ? `<div class="rt-modal-body">${esc(opts.message)}</div>` : ''}
        <div class="rt-modal-actions">${btnHtml}</div></div>`;
    doc.body.appendChild(wrap);
    const done = (v) => { wrap.remove(); doc.removeEventListener('keydown', onKey); resolve(v); };
    const onKey = (e) => { if (e.key === 'Escape') done(null); };
    doc.addEventListener('keydown', onKey);
    wrap.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b) { done(buttons[+b.getAttribute('data-idx')].value); return; }
      if (e.target === wrap) done(null);
    });
  });
}

/** Open a read-only pretty-JSON viewer for an object/array cell value. */
function showJsonCell(ri, field, el) {
  const doc = el && el.ownerDocument ? el.ownerDocument : document;
  const rows = tableDisplayRows();
  const r = rows[ri];
  if (!r) return;
  showJsonModal(doc, {
    title: field, value: r[field],
    id: r._id, index: r._index, field,
  });
}

/** Pretty-print a JSON value in a modal. When write mode is on and the row is
 *  editable, the JSON can be edited in place and saved back to Elasticsearch. */
function showJsonModal(doc, opts) {
  doc = doc || document;
  const { title, value, id, index, field } = opts || {};
  const editable = !!(writeMode && id != null && index != null &&
                      field && field !== '_id' && field !== '_index');

  const obj = asJsonObject(value);
  const pretty = obj !== null ? JSON.stringify(obj, null, 2) : String(value ?? '');

  const wrap = doc.createElement('div');
  wrap.className = 'rt-modal-overlay';
  wrap.innerHTML = `<div class="rt-modal rt-modal-json">
      <div class="rt-modal-title"><i class="bi bi-braces me-1"></i>${esc(title || 'JSON')}</div>
      <div class="rt-json-host"></div>
      <div class="rt-json-err text-danger small mb-2 d-none"></div>
      <div class="rt-modal-actions"></div>
    </div>`;
  doc.body.appendChild(wrap);

  const host    = wrap.querySelector('.rt-json-host');
  const errEl   = wrap.querySelector('.rt-json-err');
  const actions = wrap.querySelector('.rt-modal-actions');
  let editing = false;

  const done = () => { wrap.remove(); doc.removeEventListener('keydown', onKey); };
  const onKey = (e) => { if (e.key === 'Escape' && !editing) done(); };
  doc.addEventListener('keydown', onKey);

  function showErr(msg) {
    errEl.textContent = msg || '';
    errEl.classList.toggle('d-none', !msg);
  }

  function render() {
    showErr('');
    if (editing) {
      host.innerHTML = `<textarea class="rt-json-edit" spellcheck="false"></textarea>`;
      host.querySelector('.rt-json-edit').value = pretty;
      actions.innerHTML = `
        <button class="btn btn-sm btn-outline-secondary" data-act="format"><i class="bi bi-magic me-1"></i>Format</button>
        <button class="btn btn-sm btn-primary" data-act="save"><i class="bi bi-save me-1"></i>Save</button>
        <button class="btn btn-sm btn-outline-secondary" data-act="view">Cancel</button>`;
      setTimeout(() => host.querySelector('.rt-json-edit')?.focus(), 0);
    } else {
      host.innerHTML = `<pre class="rt-json-pre">${esc(pretty)}</pre>`;
      actions.innerHTML = `
        <button class="btn btn-sm btn-outline-primary" data-act="copy"><i class="bi bi-clipboard me-1"></i>Copy</button>
        ${editable ? `<button class="btn btn-sm btn-warning" data-act="edit"><i class="bi bi-pencil me-1"></i>Edit</button>` : ''}
        <button class="btn btn-sm btn-secondary" data-act="close">Close</button>`;
    }
  }

  async function save() {
    const text = host.querySelector('.rt-json-edit').value;
    let parsed;
    try {
      parsed = JSON.parse(text);
    } catch (e) {
      showErr('Invalid JSON: ' + e.message);
      return;
    }
    done();
    await applyDocChange(id, index, field, 'set', parsed);
  }

  wrap.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) { if (e.target === wrap && !editing) done(); return; }
    const act = b.getAttribute('data-act');
    if (act === 'copy') {
      const nav = (doc.defaultView && doc.defaultView.navigator) || navigator;
      try { nav.clipboard.writeText(pretty); b.innerHTML = '<i class="bi bi-check2 me-1"></i>Copied'; } catch (_) {}
    } else if (act === 'edit') {
      editing = true; render();
    } else if (act === 'view') {
      editing = false; render();
    } else if (act === 'format') {
      const ta = host.querySelector('.rt-json-edit');
      try { ta.value = JSON.stringify(JSON.parse(ta.value), null, 2); showErr(''); }
      catch (err) { showErr('Invalid JSON: ' + err.message); }
    } else if (act === 'save') {
      save();
    } else if (act === 'close') {
      done();
    }
  });

  render();
}

/** Modal asking whether to apply a column op to the viewable rows or ALL hits.
 *  Resolves to 'all' | 'visible' | null (cancel). */
function chooseScopeDialog(doc, verb, fields, visibleCount, totalCount, warn) {
  doc = doc || document;
  return new Promise(resolve => {
    const danger = verb.toLowerCase().startsWith('delete');
    const wrap = doc.createElement('div');
    wrap.className = 'rt-modal-overlay';
    const note = warn ? `<div class="rt-modal-note">
        <i class="bi bi-exclamation-triangle-fill"></i>
        Over ${(10000).toLocaleString()} documents match. Choosing <b>viewable</b> changes only the
        loaded rows — the rest stay unchanged, so the displayed data will be
        <b>inconsistent</b> with Elasticsearch. Reload the affected index/indices afterward to refresh.
      </div>` : '';
    wrap.innerHTML = `
      <div class="rt-modal">
        <div class="rt-modal-title">${danger ? '🗑' : '✎'} ${esc(verb)} field${fields.length > 1 ? 's' : ''}
          <span class="text-info">${esc(fields.join(', '))}</span></div>
        <div class="rt-modal-body">
          You are viewing <b>${visibleCount}</b> of <b>${totalCount.toLocaleString()}</b>
          document(s) matching the query.<br>Apply this change to:
        </div>
        ${note}
        <div class="rt-modal-actions">
          <button class="btn btn-sm ${danger ? 'btn-danger' : 'btn-warning'}" data-c="all">
            ALL ${totalCount.toLocaleString()} matching</button>
          <button class="btn btn-sm btn-outline-primary" data-c="visible">
            Only ${visibleCount} viewable</button>
          <button class="btn btn-sm btn-outline-secondary" data-c="">Cancel</button>
        </div>
      </div>`;
    doc.body.appendChild(wrap);
    const done = (v) => { wrap.remove(); doc.removeEventListener('keydown', onKey); resolve(v); };
    const onKey = (e) => { if (e.key === 'Escape') done(null); };
    doc.addEventListener('keydown', onKey);
    wrap.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b) { done(b.getAttribute('data-c') || null); return; }
      if (e.target === wrap) done(null);
    });
  });
}

async function columnFieldOp(op, el) {
  if (!writeMode) { showToast('Enable Write mode first', 'bg-warning'); return; }
  const doc = el ? el.ownerDocument : document;
  const cols = [...selectedCols];
  if (!cols.length) return;

  let value = null;
  if (op === 'set') {
    value = await uiPrompt(doc, { title: `Set value for field(s) [${cols.join(', ')}]`, value: '', okText: 'Set' });
    if (value === null) return;
  }

  const rowsSelected = selectedRows.size > 0;
  let payloadBase, localPredicate;

  if (rowsSelected) {
    const docs = [...selectedRows].map(k => { const [index, id] = k.split(ROWSEP); return { index, id }; });
    const verb = op === 'delete' ? 'Delete' : 'Set';
    if (!await uiConfirm(doc, { title: `${verb} field(s) [${cols.join(', ')}] on ${docs.length} selected document(s)?`,
                                okText: verb, danger: op === 'delete' })) return;
    payloadBase = { scope: 'selected', docs };
    const idset = new Set(selectedRows);
    localPredicate = (r) => idset.has(rowKey(r));
  } else {
    // Whole column. Up to SCOPE_AUTO_LIMIT matching docs → just apply to all
    // (single confirm). Beyond that → ask all-vs-viewable, warning that
    // "viewable" leaves the loaded data inconsistent with Elasticsearch.
    const visible = tableDisplayRows();
    const verb  = op === 'delete' ? 'Delete' : 'Set';
    const totalMatching = Math.max(lastResultTotal || 0, visible.length);

    let choice;
    if (totalMatching <= visible.length) {
      // Everything matching is already on screen → no viewed/all distinction.
      if (!await uiConfirm(doc, { title: `${verb} field(s) [${cols.join(', ')}] on all ${visible.length} document(s)?`,
                                  okText: verb, danger: op === 'delete' })) return;
      choice = 'all';
    } else {
      // More match than are shown → let the user pick viewed vs all
      // (warn about inconsistency only for very large sets).
      choice = await chooseScopeDialog(doc, verb, cols, visible.length, totalMatching,
                                       /*warn=*/ totalMatching > SCOPE_AUTO_LIMIT);
      if (!choice) return;
    }

    if (choice === 'all') {
      const items = activeContextItems();
      if (!items) { showToast('Invalid query JSON — cannot target all docs', 'bg-danger'); return; }
      payloadBase = { scope: 'all', per_index_queries: items };
      localPredicate = () => true;   // every loaded row matches the query
    } else {
      const docs = visible.filter(r => r._id != null && r._index != null)
                          .map(r => ({ index: r._index, id: r._id }));
      payloadBase = { scope: 'selected', docs, _partial: true };
      const vis = new Set(visible.map(rowKey));
      localPredicate = (r) => vis.has(rowKey(r));
    }
  }

  if (!await confirmSharedCc(`bulk-edit ${cols.length} field(s)`)) return;

  let total = 0;
  const partial = payloadBase._partial;
  delete payloadBase._partial;
  for (const field of cols) {
    const res = await api('/api/docs/bulk-field', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...payloadBase, field, op, value }),
    });
    if (!res || res.error) { showToast(`Failed on "${field}": ` + (res?.error || 'unknown'), 'bg-danger'); return; }
    total += res.updated || 0;
  }

  // Reflect locally on the affected loaded rows.
  for (const r of lastResultHits) {
    if (!localPredicate(r)) continue;
    for (const c of cols) { if (op === 'delete') delete r[c]; else r[c] = value; }
  }
  clearSelection();
  renderResultViews();
  showToast(`${op === 'delete' ? 'Deleted' : 'Updated'} ${total} field value(s)`, 'bg-success');
  if (partial) {
    await uiConfirm(doc, { title: 'Applied to visible rows only',
      message: 'Other matching documents were NOT changed, so the loaded data is now out of sync '
             + 'with Elasticsearch. Reload the affected index/indices (re-run the query) to refresh.',
      okText: 'Got it' });
  }
}

function toggleTableSort(col) {
  if (tableSort.col === col) {
    if (tableSort.dir === 'asc') tableSort.dir = 'desc';
    else { tableSort.col = null; }            // asc → desc → off
  } else { tableSort.col = col; tableSort.dir = 'asc'; }
  refreshTables();
}

/** One `<label>` per value for the filter list, checked per the selection.
 *  Checkbox VALUE is always the raw value (used for matching); the visible
 *  label is human-readable for date columns. */
function _filterListHtml(col, uniques, sel) {
  const all = !sel;
  const isDate = activeDateColumns().has(col);
  return uniques.map(v => {
    const checked = (all || sel.has(v)) ? 'checked' : '';
    const label = v === '' ? '(empty)' : displayCell(v, isDate);
    return `<label><input type="checkbox" value="${esc(v)}" ${checked}/><span>${esc(label)}</span></label>`;
  }).join('');
}

async function toggleColFilter(ev, col) {
  ev.stopPropagation();
  const doc = ev.target.ownerDocument;        // works in the main window OR the pop-out
  const panel = doc.getElementById(cssId(col));
  if (!panel) return;
  const wasOpen = !panel.classList.contains('d-none');
  doc.querySelectorAll('.rt-filter').forEach(p => p.classList.add('d-none'));
  if (wasOpen) return;

  const sel = tableFilters[col];
  const all = !sel;
  const loaded = columnUniqueValues(col);     // instant list from loaded rows
  panel.innerHTML = `
    <input class="rt-filter-search" placeholder="search values…" oninput="rtFilterSearch(this)"/>
    <label class="rt-filter-all">
      <input type="checkbox" onchange="rtFilterToggleAll(this)" ${all ? 'checked' : ''}/>
      <span>(Select all)</span>
    </label>
    <div class="rt-filter-loading text-secondary" style="font-size:.68rem;padding:2px 6px;">
      <span class="spinner-border spinner-border-sm" style="width:.7rem;height:.7rem;"></span> loading all values…
    </div>
    <div class="rt-filter-list">${_filterListHtml(col, loaded, sel)}</div>
    <div class="rt-filter-actions">
      <button class="btn btn-sm btn-primary py-0" onclick="applyColFilter('${jsq(col)}', this)">Apply</button>
      <button class="btn btn-sm btn-outline-light py-0" onclick="clearColFilter('${jsq(col)}')">Clear</button>
    </div>`;
  panel.classList.remove('d-none');

  // Augment with the FULL distinct set from Elasticsearch (whole index, and
  // Excel-style cascading-aware) so a small loaded page doesn't hide values.
  const full = await fetchColumnDistinct(col);
  if (panel.classList.contains('d-none')) return;   // closed while awaiting
  const listEl = panel.querySelector('.rt-filter-list');
  panel.querySelector('.rt-filter-loading')?.remove();
  if (!listEl || !full.length) return;

  // Preserve any picks the user made during the load, then union server values
  // with the loaded ones so nothing already visible disappears.
  const currentlyChecked = new Set(
    [...listEl.querySelectorAll('input[type=checkbox]')].filter(b => b.checked).map(b => b.value));
  const merged = [...new Set([...full, ...loaded])]
    .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  const selNow = tableFilters[col];
  const allNow = !selNow;
  const isDateCol = activeDateColumns().has(col);
  listEl.innerHTML = merged.map(v => {
    const isChecked = allNow ? true : (selNow.has(v) || currentlyChecked.has(v));
    const label = v === '' ? '(empty)' : displayCell(v, isDateCol);
    return `<label><input type="checkbox" value="${esc(v)}" ${isChecked ? 'checked' : ''}/><span>${esc(label)}</span></label>`;
  }).join('');
  // Re-apply any active search term to the freshly rendered list.
  const search = panel.querySelector('.rt-filter-search');
  if (search && search.value) rtFilterSearch(search);
}

function rtFilterSearch(inp) {
  const q = inp.value.toLowerCase();
  inp.closest('.rt-filter').querySelectorAll('.rt-filter-list label').forEach(l => {
    l.style.display = l.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
}

/** "(Select all)" master checkbox — sets every visible value checkbox to match. */
function rtFilterToggleAll(master) {
  master.closest('.rt-filter').querySelectorAll('.rt-filter-list label').forEach(l => {
    if (l.style.display === 'none') return;            // only affect visible (searched) items
    const cb = l.querySelector('input[type=checkbox]');
    if (cb) cb.checked = master.checked;
  });
}

function applyColFilter(col, btn) {
  // The "(Select all)" master isn't a value box — read only the value checkboxes.
  const doc = (btn && btn.ownerDocument) || document;
  const panel = doc.getElementById(cssId(col));
  const boxes = [...panel.querySelectorAll('.rt-filter-list input[type=checkbox]')];
  const checked = boxes.filter(b => b.checked).map(b => b.value);
  if (checked.length === 0 || checked.length === boxes.length) delete tableFilters[col];
  else tableFilters[col] = new Set(checked);
  commitFilters();
}

function clearColFilter(col) {
  delete tableFilters[col];
  commitFilters();
}

/** Clear ALL column filters at once */
function clearAllFilters() {
  tableFilters = {};
  commitFilters();
  showToast('All filters cleared', 'bg-info');
}

/** Apply the current filters. When the loaded page is only a slice of the
 *  matching docs, resolve the filters against Elasticsearch (so values absent
 *  from the loaded rows still match) — this works on ANY field of ANY index,
 *  in both the Index viewer and the Query Editor results. When every matching
 *  doc is already loaded, filter locally (instant). */
function commitFilters() {
  updateFilterButtonState();
  if (activeViewer === 'index' && _currentIndexName &&
      lastResultHits.length < (_indexFullTotal || 0)) {
    applyIndexFiltersServerSide();
    return;
  }
  if (activeViewer === 'query' && _queryBaseItems &&
      lastResultHits.length < (_queryTotalMatching || 0)) {
    applyQueryFiltersServerSide();
    return;
  }
  refreshTables();
  updateShowingCount();
}

/** Combine a base query with the active column-filter clauses (AND semantics). */
function mergeQueryWithFilters(baseQuery, filterClauses) {
  const must = [];
  if (baseQuery && !baseQuery.match_all) must.push(baseQuery);
  must.push(...filterClauses);
  if (!must.length) return { match_all: {} };
  if (must.length === 1) return must[0];
  return { bool: { must } };
}

/** Re-run the Query Editor results with the active column filters merged into
 *  each per-index query, preserving the filters + column visibility. */
async function applyQueryFiltersServerSide() {
  if (!_queryBaseItems || !_queryBaseItems.length) { refreshTables(); return; }
  // Snapshot filters/visibility — captureResults() resets them.
  const savedFilters = {};
  for (const [c, s] of Object.entries(tableFilters)) if (s?.size) savedFilters[c] = [...s];
  const savedHidden = [...hiddenColumns];

  const filterClauses = buildFilterMustClauses();
  const items = _queryBaseItems.map(it => {
    const qb = { ...(it.query_body || {}) };
    qb.query = mergeQueryWithFilters(qb.query || { match_all: {} }, filterClauses);
    return { index: it.index, query_body: qb };
  });
  const size = querySizeValue();
  const sortDir = document.querySelector('input[name="sortDir"]:checked')?.value ?? 'desc';

  const metaEl = document.getElementById('queryMeta');
  if (metaEl) metaEl.textContent = 'Filtering…';

  const data = await api('/api/query/multi-run', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ per_index_queries: items, size, sort_direction: sortDir }),
  });
  if (!data || data.error) {
    showToast('Filter query failed: ' + (data?.error || 'unknown'), 'bg-danger');
    return;
  }

  const matchTotal = (data.per_index_meta || []).reduce((s, m) => s + (m.total || 0), 0);
  captureResults(data);                     // freezes columns, clears filters/visibility
  lastResultTotal = matchTotal || data.total_hits || lastResultHits.length;
  tableFilters = {};
  for (const [c, vals] of Object.entries(savedFilters)) tableFilters[c] = new Set(vals);
  const present = new Set(currentColumns());
  hiddenColumns = new Set(savedHidden.filter(c => present.has(c)));
  renderResultViews();
  updateFilterButtonState();
  if (metaEl) {
    const loaded = lastResultHits.length;
    metaEl.textContent = `${loaded.toLocaleString()} shown of ${(matchTotal).toLocaleString()} matching`;
  }
}

/** Re-query the current index with the active column filters as an ES query,
 *  preserving the filters + column visibility across the reload. */
async function applyIndexFiltersServerSide() {
  if (!_currentIndexName) return;
  // Snapshot filters/visibility — captureResults() resets them.
  const savedFilters = {};
  for (const [c, s] of Object.entries(tableFilters)) if (s?.size) savedFilters[c] = [...s];
  const savedHidden = [...hiddenColumns];

  const must  = buildFilterMustClauses();
  const query = must.length ? { bool: { must } } : { match_all: {} };
  const size  = parseInt(document.getElementById('sampleSizeSelect')?.value || '10');
  const body  = { query };
  if (_indexSortField) body.sort = [{ [_indexSortField]: { order: 'desc' } }];

  const metaEl = document.getElementById(RV.meta);
  if (metaEl && activeViewer === 'index') metaEl.textContent = 'Filtering…';

  const data = await api('/api/query', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ index: _currentIndexName, body, size }),
  });
  if (!data || data.error) {
    showToast('Filter query failed: ' + (data?.error || 'unknown'), 'bg-danger');
    return;
  }

  captureResults(data);                     // freezes columns, clears filters/visibility
  tableFilters = {};
  for (const [c, vals] of Object.entries(savedFilters)) tableFilters[c] = new Set(vals);
  const present = new Set(currentColumns());
  hiddenColumns = new Set(savedHidden.filter(c => present.has(c)));
  renderResultViews();
  updateFilterButtonState();
  updateShowingCount();
}

/** Update the "Showing / Total" stat card and meta text based on current filters */
function updateShowingCount() {
  // tableDisplayRows() returns an ARRAY of row objects — use its length, not the array itself.
  const visibleCount = tableDisplayRows().length;
  const totalRows = lastResultHits.length;
  const totalInIndex = lastResultTotal || totalRows;

  // Update the stat card if it exists
  const statEl = document.getElementById('showingTotalStat');
  if (statEl) {
    const hasFilters = Object.keys(tableFilters).some(k => tableFilters[k]?.size);
    if (hasFilters) {
      statEl.innerHTML = `<span class="text-warning">${visibleCount.toLocaleString()}</span> / ${totalInIndex.toLocaleString()}`;
    } else {
      statEl.textContent = `${totalRows.toLocaleString()} / ${totalInIndex.toLocaleString()}`;
    }
  }

  // Update meta text
  const metaEl = document.getElementById(RV.meta);
  if (metaEl && activeViewer === 'index') {
    const hasFilters = Object.keys(tableFilters).some(k => tableFilters[k]?.size);
    const filterNote = hasFilters ? ` · ${visibleCount.toLocaleString()} shown after filtering` : '';
    metaEl.textContent = `${totalRows.toLocaleString()} loaded of ${totalInIndex.toLocaleString()}${filterNote}`;
  }
}

/** Update filter button visual state */
function updateFilterButtonState() {
  const hasFilters = Object.keys(tableFilters).some(k => tableFilters[k]?.size);

  // Update both Index Detail and Query Editor Clear Filters buttons
  ['btnClearFilters', 'btnClearFiltersQuery'].forEach(btnId => {
    const clearBtn = document.getElementById(btnId);
    if (clearBtn) {
      clearBtn.classList.toggle('btn-outline-danger', !hasFilters);
      clearBtn.classList.toggle('btn-danger', hasFilters);
      if (hasFilters) {
        const filterCount = Object.keys(tableFilters).filter(k => tableFilters[k]?.size).length;
        clearBtn.innerHTML = `<i class="bi bi-x-circle me-1"></i>Clear Filters <span class="badge bg-light text-danger ms-1">${filterCount}</span>`;
      } else {
        clearBtn.innerHTML = `<i class="bi bi-x-circle me-1"></i>Clear Filters`;
      }
    }
  });

  ['btnQueryFromFilters', 'btnQueryFromFiltersQuery'].forEach(btnId => {
    const queryBtn = document.getElementById(btnId);
    if (queryBtn) {
      queryBtn.classList.toggle('btn-outline-info', !hasFilters);
      queryBtn.classList.toggle('btn-info', hasFilters);
    }
  });
}

/** Generate ES query from active table filters and populate query editor */
function generateQueryFromFilters() {
  const activeFilters = Object.entries(tableFilters).filter(([_, v]) => v?.size > 0);

  if (!activeFilters.length) {
    showToast('No active filters to convert', 'bg-warning');
    return;
  }

  const mustClauses = [];

  for (const [field, values] of activeFilters) {
    const valArray = [...values];
    // Analyzed text fields need their exact sub-field, or the query returns 0.
    const f = exactField(field);
    if (valArray.length === 1) {
      // Single value: use term query
      mustClauses.push({ term: { [f]: valArray[0] } });
    } else {
      // Multiple values: use terms query
      mustClauses.push({ terms: { [f]: valArray } });
    }
  }

  const query = {
    query: {
      bool: {
        must: mustClauses
      }
    }
  };
  // Sort by the index's known date field (from the sample response) — never a
  // hardcoded name that may not exist. The backend also validates sort fields.
  if (activeViewer === 'index' && _indexSortField) {
    query.sort = [{ [_indexSortField]: { order: 'desc' } }];
  }

  // Populate the query editor
  const queryBodyEl = document.getElementById('queryBody');
  const queryIndexEl = document.getElementById('queryIndex');

  if (queryBodyEl) {
    queryBodyEl.value = JSON.stringify(query, null, 2);
  }

  // Set index pattern from the current context: Index Detail → that index's
  // pattern; Query Editor → the executed query's index pattern(s).
  if (queryIndexEl) {
    if (activeViewer === 'index' && _currentIndexName) {
      // e.g. "adc-network-hourly-ty-...-687" -> "adc-network-hourly*"
      // The wildcard is appended WITHOUT a separating dash: an index whose name
      // is exactly the base (e.g. "alert-sid-0") is matched by "alert-sid-0*"
      // but not by "alert-sid-0-*", which requires at least one more character.
      const baseName = _currentIndexName.split('-ty-')[0].replace(/-+$/, '');
      queryIndexEl.value = baseName + '*';
    } else if (activeViewer === 'query' && _queryBaseItems?.length) {
      queryIndexEl.value = [...new Set(_queryBaseItems.map(it => it.index).filter(Boolean))].join(',');
    }
  }

  // This query comes entirely from the table filters — drop any leftover NL
  // context (free-text prompt, its interpretation, the translated multi-index
  // plan, suggestions, attack-type picks, time range) so nothing stale lingers
  // or gets run instead of the filter-derived query.
  const nlInput = document.getElementById('nlQueryInput');
  if (nlInput) nlInput.value = '';
  const infoEl = document.getElementById('nlInterpretation');
  if (infoEl) { infoEl.classList.add('d-none'); infoEl.innerHTML = ''; }
  perIndexQueries = [];
  renderPerIndexQueries([]);           // shows the single textarea (keeps queryBody)
  fieldSuggestions = [];
  renderSuggestions([]);
  if (typeof clearAttackTypeSelection === 'function') clearAttackTypeSelection();
  if (typeof clearTimeRange === 'function') clearTimeRange();

  // Switch to query view
  showView('query');
  showToast(`Query generated with ${activeFilters.length} filter(s). Edit indices and run.`, 'bg-success');
}

/* Close any open filter dropdown when clicking elsewhere (main window). */
document.addEventListener('click', (e) => {
  if (e.target.closest('.rt-filter') || e.target.closest('.rt-funnel')) return;
  document.querySelectorAll('.rt-filter:not(.d-none)').forEach(p => p.classList.add('d-none'));
});

/* ── Cell edit / delete (writes back to Elasticsearch) ─────────────────── */
async function editCell(id, index, field, el) {
  const doc = el ? el.ownerDocument : document;
  const row = lastResultHits.find(r => r._id === id && r._index === index);
  const cur = row ? row[field] : '';
  const shown = cur == null ? '' : (typeof cur === 'object' ? JSON.stringify(cur) : String(cur));
  const input = await uiPrompt(doc, { title: `Set "${field}" for _id ${id}`, value: shown, okText: 'Save' });
  if (input === null) return;                 // cancelled
  await applyDocChange(id, index, field, 'set', input, doc);
}

async function deleteCell(id, index, field, el) {
  const doc = el ? el.ownerDocument : document;
  const ok = await uiConfirm(doc, { title: `Delete field "${field}"?`,
    message: `Remove "${field}" from document _id ${id}. This deletes the field from the ES document.`,
    okText: 'Delete', danger: true });
  if (!ok) return;
  await applyDocChange(id, index, field, 'delete', null, doc);
}

async function applyDocChange(id, index, field, op, value, doc) {
  if (!writeMode) { showToast('Enable Write mode first', 'bg-warning'); return; }
  if (!await confirmSharedCc(
        `${op === 'delete' ? 'delete' : 'edit'} field "${field}" on a document`, doc)) return;
  const res = await api('/api/doc/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ index, id, field, op, value }),
  });
  if (!res || res.error) { showToast('Update failed: ' + (res?.error || 'unknown'), 'bg-danger'); return; }

  const row = lastResultHits.find(r => r._id === id && r._index === index);
  if (row) {
    if (op === 'delete') delete row[field];
    else row[field] = res.value;
  }
  renderResultViews();
  showToast(op === 'delete' ? `Removed "${field}"` : `Updated "${field}"`, 'bg-success');
}

/** Download the current results — CSV for table/csv views, JSON otherwise.
 *  When column filters are active AND not every matching doc is loaded (Size
 *  limit smaller than the total matching in the index), the user is asked
 *  whether to download just the rows shown here or ALL docs matching the same
 *  filter criteria server-side. */
async function downloadResults() {
  // "Download shown docs" → only the rows currently visible after column
  // filtering/sorting (the funnel filters act as the query here). Every field
  // that matches is exported, regardless of column show/hide visibility.
  const rows = tableDisplayRows();
  if (!rows.length) { showToast('No rows to export', 'bg-warning'); return; }

  const hasFilters   = Object.keys(tableFilters).some(k => tableFilters[k]?.size);
  const loaded       = lastResultHits.length;
  const totalInIndex = lastResultTotal || loaded;
  // More docs could match server-side than we loaded → offer the choice.
  const moreOnServer = hasFilters && loaded < totalInIndex && !!_currentIndexName;

  if (moreOnServer) {
    const choice = await uiChoice(document, {
      title: 'Download filtered docs',
      message: `${rows.length.toLocaleString()} doc(s) match your filters among the `
             + `${loaded.toLocaleString()} loaded here, but the index holds `
             + `${totalInIndex.toLocaleString()} docs total. More may match the same `
             + `filters server-side. What do you want to download?`,
      buttons: [
        { value: 'shown', text: `Shown only (${rows.length.toLocaleString()})`, cls: 'btn-primary' },
        { value: 'all',   text: 'All matching (server-side)', cls: 'btn-info' },
        { value: null,    text: 'Cancel', cls: 'btn-outline-secondary' },
      ],
    });
    if (choice == null) return;
    if (choice === 'all') { await exportFilteredMatches(); return; }
    // choice === 'shown' → fall through to local download
  }
  downloadRowsLocally(rows);
}

/** Write the given rows to a file in the browser (CSV for table/csv, JSON otherwise). */
function downloadRowsLocally(rows) {
  const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  let content, mime, ext;
  if (resultView === 'json') {
    content = JSON.stringify(rows, null, 2);
    mime = 'application/json'; ext = 'json';
  } else {
    // rawDates: the file must re-import cleanly (screen views stay readable).
    content = buildResultsCsv(rows, currentColumns(), true);
    mime = 'text/csv'; ext = 'csv';
  }
  const blob = new Blob([content], { type: mime + ';charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `cc_query_results_${ts}.${ext}`;
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
}

/* ── Delete / Modify the whole result set ────────────────────────────────────
   Delete-by-query and update-by-query, in two steps: pick the scope (the rows
   on screen vs every doc the query matches), then approve an exact count that
   the server counts for us — never an estimate. */

/** Rows currently on screen after column filters, as {index,id} doc refs. */
function _shownDocRefs() {
  return tableDisplayRows()
    .filter(r => r._id != null && r._index != null)
    .map(r => ({ index: r._index, id: r._id }));
}

/** Exact number of docs the active query matches, straight from ES. */
async function _matchingTotal(items) {
  const d = await api('/api/docs/count', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ per_index_queries: items }),
  });
  if (!d || d.error) throw new Error(d?.error || 'count failed');
  return d.total;
}

/** Step 1 of both flows: resolve the scope and the exact document count.
 *  Returns {scope, count, payload} or null when the user backed out. */
async function _resolveResultScope(verb) {
  const items = activeContextItems();
  if (!items || !items.length) {
    showToast('Invalid query JSON — cannot target the matching docs', 'bg-danger');
    return null;
  }
  const shown = _shownDocRefs();
  let total;
  try {
    total = await _matchingTotal(items);
  } catch (e) {
    showToast('Could not count matching documents: ' + e.message, 'bg-danger');
    return null;
  }
  if (!total && !shown.length) { showToast('Nothing matches this query', 'bg-warning'); return null; }

  let scope = 'all';
  if (total > shown.length && shown.length) {
    const c = await uiChoice(document, {
      title: `${verb} — which documents?`,
      message: `${shown.length.toLocaleString()} document(s) are shown here, but this `
             + `query matches ${total.toLocaleString()} in Elasticsearch.`,
      buttons: [
        { value: 'shown', text: `Only the ${shown.length.toLocaleString()} shown`, cls: 'btn-primary' },
        { value: 'all',   text: `All ${total.toLocaleString()} matching`, cls: 'btn-warning' },
        { value: null,    text: 'Cancel', cls: 'btn-outline-secondary' },
      ],
    });
    if (!c) return null;
    scope = c;
  }
  return scope === 'all'
    ? { scope: 'all', count: total, payload: { scope: 'all', per_index_queries: items } }
    : { scope: 'shown', count: shown.length, payload: { scope: 'selected', docs: shown } };
}

/** Delete every document the query matched (or just the shown ones). */
async function deleteResults() {
  const sel = await _resolveResultScope('Delete results');
  if (!sel) return;
  if (!await confirmSharedCc(`delete ${sel.count.toLocaleString()} document(s)`)) return;

  const typed = await uiPrompt(document, {
    title: `Delete ${sel.count.toLocaleString()} document(s) — type DELETE to confirm`,
    value: '', okText: 'Delete',
  });
  if (typed == null) return;
  if (typed.trim().toUpperCase() !== 'DELETE') {
    showToast('Confirmation did not match — nothing was deleted', 'bg-warning'); return;
  }
  showToast(`Deleting ${sel.count.toLocaleString()} document(s)…`, 'bg-secondary');
  const res = await api('/api/docs/bulk-delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(sel.payload),
  });
  if (!res || res.error) { showToast('Delete failed: ' + (res?.error || 'unknown'), 'bg-danger'); return; }
  showToast(`Deleted ${(res.deleted ?? 0).toLocaleString()} document(s)`, 'bg-success');
  runQuery();                       // re-run so the view matches Elasticsearch
}

/** Set one or more field values across the matched documents. */
async function modifyResults() {
  const fields = [...new Set([...visibleColumns(), ...currentColumns()])]
    .filter(c => c && !['_id', '_index'].includes(c));
  if (!fields.length) { showToast('No fields to modify', 'bg-warning'); return; }

  const values = await _modifyFieldsDialog(fields);
  if (!values) return;                                  // cancelled
  const names = Object.keys(values);
  if (!names.length) { showToast('No values entered — nothing to modify', 'bg-warning'); return; }

  const sel = await _resolveResultScope('Modify results');
  if (!sel) return;
  if (!await confirmSharedCc(`modify ${names.length} field(s) on ${sel.count.toLocaleString()} document(s)`)) return;

  const preview = names.map(f => `• ${f} = ${values[f]}`).join('\n');
  const ok = await uiConfirm(document, {
    title: `Modify ${sel.count.toLocaleString()} document(s)?`,
    message: `These field(s) will be set on every one of them:\n${preview}\n\n`
           + `All other fields keep their current values.`,
    okText: 'Modify', danger: true,
  });
  if (!ok) return;
  showToast(`Modifying ${sel.count.toLocaleString()} document(s)…`, 'bg-secondary');
  const res = await api('/api/docs/bulk-update', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...sel.payload, fields: values }),
  });
  if (!res || res.error) { showToast('Modify failed: ' + (res?.error || 'unknown'), 'bg-danger'); return; }
  showToast(`Modified ${(res.updated ?? 0).toLocaleString()} document(s)`, 'bg-success');
  runQuery();
}

/** Field list with one value box each. Resolves to {field: value} for the
 *  boxes that were filled in, or null when cancelled. Blank = leave alone. */
function _modifyFieldsDialog(fields) {
  return new Promise(resolve => {
    const wrap = document.createElement('div');
    wrap.className = 'rt-modal-overlay';
    wrap.innerHTML = `<div class="rt-modal" style="min-width:560px;width:720px;max-width:95vw;
          max-height:88vh;display:flex;flex-direction:column;">
        <div class="rt-modal-title" style="flex:0 0 auto;">✎ Modify results — set field values</div>
        <div class="rt-modal-body" style="flex:0 0 auto;white-space:normal;">
          Type a value for each field you want to change. <b>Fields left blank stay as they are.</b>
        </div>
        <input type="text" class="form-control form-control-sm mrf-filter mb-2" style="flex:0 0 auto;"
               placeholder="Filter fields…">
        <div class="border border-secondary rounded" style="flex:1 1 auto;min-height:0;overflow:auto;">
          <table class="table table-sm mb-0" style="font-size:0.8rem;">
            <thead class="table-dark" style="position:sticky;top:0;z-index:2;">
              <tr><th style="width:45%;">Field</th><th>New value</th></tr></thead>
            <tbody>${fields.map(f => `<tr class="mrf-row" data-field="${esc(f)}">
              <td class="font-monospace">${esc(f)}</td>
              <td><input type="text" class="form-control form-control-sm mrf-val"
                         data-field="${esc(f)}" placeholder="leave blank = unchanged"></td>
            </tr>`).join('')}</tbody>
          </table>
        </div>
        <div class="rt-modal-actions" style="flex:0 0 auto;">
          <span class="mrf-count small text-info me-auto">no fields set</span>
          <button class="btn btn-sm btn-warning" data-ok="1">Continue</button>
          <button class="btn btn-sm btn-outline-secondary" data-ok="0">Cancel</button>
        </div></div>`;
    document.body.appendChild(wrap);

    const collect = () => {
      const out = {};
      wrap.querySelectorAll('.mrf-val').forEach(i => {
        if (i.value !== '') out[i.dataset.field] = i.value;
      });
      return out;
    };
    const done = (v) => { wrap.remove(); document.removeEventListener('keydown', onKey); resolve(v); };
    const onKey = (e) => { if (e.key === 'Escape') done(null); };
    document.addEventListener('keydown', onKey);
    wrap.addEventListener('input', (e) => {
      if (e.target.classList.contains('mrf-filter')) {
        const q = e.target.value.trim().toLowerCase();
        wrap.querySelectorAll('.mrf-row').forEach(r => {
          r.classList.toggle('d-none', !!q && !r.dataset.field.toLowerCase().includes(q));
        });
        return;
      }
      const n = Object.keys(collect()).length;
      wrap.querySelector('.mrf-count').textContent =
        n ? `${n} field(s) will be set` : 'no fields set';
    });
    wrap.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b) { done(b.getAttribute('data-ok') === '1' ? collect() : null); return; }
      if (e.target === wrap) done(null);
    });
    setTimeout(() => wrap.querySelector('.mrf-val')?.focus(), 0);
  });
}

/* ── Exact-match field resolution ────────────────────────────────────────────
   CC templates map many string fields as ANALYZED text with a `.raw` keyword
   sub-field. A term query is not analyzed, so `term: {applicationId: "798:80"}`
   matches nothing — the analyzer indexed 7/79/798/9/98/8/80/0, never the whole
   value. `applicationId.raw` holds it verbatim. The server tells us which
   fields need that redirect; we cache the map per index context. */
let _exactFieldMap = {};          // {field: field.raw} — only fields that differ
let _exactFieldKey = '';          // index context the map was fetched for

async function loadExactFieldMap(indices) {
  const list = (indices || []).filter(Boolean);
  const key = list.join(',');
  if (!key || key === _exactFieldKey) return _exactFieldMap;
  try {
    const d = await api('/api/indices/exact-fields', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ indices: list }),
    });
    if (d && !d.error) { _exactFieldMap = d.map || {}; _exactFieldKey = key; }
  } catch { /* leave the previous map in place */ }
  return _exactFieldMap;
}

/** Field name to use in a term/terms clause (…".raw" when analyzed). */
function exactField(field) {
  return _exactFieldMap[field] || field;
}

/** Build ES bool.must clauses from the active table filters (same mapping as
 *  "Query from Filters"): single value → term, multiple values → terms.
 *  Pass `exceptCol` to omit one column (used for cascading value lists). */
function buildFilterMustClauses(exceptCol) {
  const dateCols = activeDateColumns();
  return Object.entries(tableFilters)
    .filter(([col, v]) => col !== exceptCol && v?.size > 0)
    .map(([field, values]) => {
      // Date columns hold raw epoch-millis strings — send them as NUMBERS so
      // ES matches the date field reliably (string terms can miss on ES 1.x).
      const coerce = dateCols.has(field)
        ? (s) => (/^\d{10,}$/.test(String(s)) ? Number(s) : s) : (s) => s;
      const arr = [...values].map(coerce);
      const f = dateCols.has(field) ? field : exactField(field);
      return arr.length === 1 ? { term: { [f]: arr[0] } } : { terms: { [f]: arr } };
    });
}

/** Export EVERY doc in the current index matching the active filter criteria,
 *  scrolling ES server-side (ignores the Size limit). */
async function exportFilteredMatches() {
  if (!_currentIndexName) { showToast('No index in context', 'bg-danger'); return; }
  const must = buildFilterMustClauses();
  const query_body = must.length
    ? { query: { bool: { must } } }
    : { query: { match_all: {} } };
  await runServerExport([{ index: _currentIndexName, query_body }]);
}

/** Export ALL matching docs by scrolling ES server-side (ignores Size limit). */
async function exportAll() {
  const items = activeContextItems();
  if (!items) { showToast('Invalid query JSON — cannot export', 'bg-danger'); return; }
  await runServerExport(items);
}

/** Shared server-side export: POST per-index queries, stream back a file. */
async function runServerExport(items) {
  const format = resultView === 'json' ? 'json' : 'csv';
  const payload = { per_index_queries: items, format };

  const btns = [...document.querySelectorAll('.js-export-all')];
  const orig = btns.map(b => b.innerHTML);
  btns.forEach(b => { b.disabled = true; b.innerHTML = '<span class="spinner-border spinner-border-sm" style="width:0.8rem;height:0.8rem;"></span>'; });
  try {
    const res = await fetch(appUrl('/api/query/export'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const t = await res.text();
      showToast('Export failed: ' + t.slice(0, 200), 'bg-danger');
      return;
    }
    const blob = await res.blob();
    const cd = res.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename="?([^"]+)"?/);
    const name = m ? m[1] : `cc_export.${format}`;
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
    // Streamed response: any rows sent before a server-side failure are kept in
    // the file. JSON exports carry a trailing "truncated"/"error" field.
    showToast(`Exported ${(blob.size / 1024).toFixed(0)} KB`, 'bg-success');
  } catch (e) {
    showToast('Export error: ' + e.message, 'bg-danger');
  } finally {
    btns.forEach((b, i) => { b.disabled = false; b.innerHTML = orig[i]; });
  }
}

/** Build the pop-out body HTML for the currently active view. */
function popoutBodyHtml() {
  if (resultView === 'table') {
    return buildInteractiveTableHtml();   // same interactive table as the main pane
  }
  if (resultView === 'csv') {
    const csv = lastResultHits.length ? buildResultsCsv(lastResultHits, currentColumns()) : 'No rows to display.';
    return `<pre class="csv">${esc(csv)}</pre>`;
  }
  return `<pre>${esc(lastResultJson || '')}</pre>`;
}

/** Push the current results (in the active view) into the detached window. */
function syncResultsPopout() {
  if (!resultsWindow || resultsWindow.closed) return;
  const doc = resultsWindow.document;
  const out = doc.getElementById('out');
  const metaEl = doc.getElementById('meta');
  if (out)    out.innerHTML = popoutBodyHtml();
  if (metaEl) metaEl.textContent = document.getElementById(RV.meta)?.textContent || '';
  // Reflect the active view + write mode on the pop-out's own toolbar.
  ['json', 'table', 'csv'].forEach(m =>
    doc.getElementById('po-' + m)?.classList.toggle('active', m === resultView));
  const wb = doc.getElementById('po-write');
  if (wb) {
    wb.classList.toggle('btn-warning', writeMode);
    wb.classList.toggle('btn-outline-light', !writeMode);
    wb.innerHTML = writeMode ? '<i class="bi bi-unlock me-1"></i>Write'
                             : '<i class="bi bi-lock me-1"></i>Read-only';
  }
}

/** Open the results in a separate browser window for easier viewing. */
function popOutResults() {
  if (resultsWindow && !resultsWindow.closed) { resultsWindow.focus(); syncResultsPopout(); return; }
  resultsWindow = window.open('', 'cc_es_results', 'width=820,height=800,scrollbars=yes,resizable=yes');
  if (!resultsWindow) { showToast('Pop-up blocked — allow pop-ups for this site', 'bg-danger'); return; }
  resultsWindow.document.write(`<!DOCTYPE html><html lang="en" data-bs-theme="dark"><head><meta charset="utf-8"/>
    <title>CC ES Analyzer — Results</title>
    <link href="${appUrl('/static/vendor/bootstrap.min.css')}" rel="stylesheet"/>
    <link href="${appUrl('/static/vendor/bootstrap-icons.min.css')}" rel="stylesheet"/>
    <link rel="stylesheet" href="${appUrl('/static/css/style.css')}"/>
    <style>
      body{margin:0;background:#1e2530;color:#c9d1d9;font-family:Consolas,'Courier New',monospace;}
      header{background:#11161d;padding:8px 12px;border-bottom:1px solid #343a40;
             font-size:.8rem;color:#8aa;display:flex;gap:10px;align-items:center;}
      #meta{color:#9ab;font-size:.75rem;margin-right:auto;}
      #out{padding:10px;}
      #out pre{margin:0;font-size:.8rem;white-space:pre-wrap;word-break:break-all;}
      #out pre.csv{white-space:pre;word-break:normal;overflow:auto;}
      #out table{font-size:.74rem;white-space:nowrap;}
    </style></head><body>
    <header>
      <strong style="color:#5cc8ff;">Query Results</strong><span id="meta"></span>
      <div class="btn-group btn-group-sm" role="group">
        <button type="button" class="btn btn-outline-light py-0 px-2" id="po-json" onclick="setResultView('json')">JSON</button>
        <button type="button" class="btn btn-outline-light py-0 px-2" id="po-table" onclick="setResultView('table')">Table</button>
        <button type="button" class="btn btn-outline-light py-0 px-2" id="po-csv" onclick="setResultView('csv')">CSV</button>
      </div>
      <button class="btn btn-sm btn-outline-success py-0 px-2" onclick="downloadResults()" title="Download shown"><i class="bi bi-download"></i></button>
      <button class="btn btn-sm btn-outline-primary py-0 px-2 js-export-all" onclick="exportAll()" title="Export ALL matching"><i class="bi bi-cloud-download me-1"></i>All</button>
      <button class="btn btn-sm btn-outline-light py-0 px-2" id="po-write" onclick="toggleWriteMode()" title="Toggle edit mode"><i class="bi bi-lock me-1"></i>Read-only</button>
      <button class="btn btn-sm btn-outline-light py-0 px-2" onclick="openFieldVisibility(this)" title="Choose which fields (columns) to show"><i class="bi bi-eye me-1"></i>Fields</button>
      <button class="btn btn-sm btn-outline-warning py-0 px-2" onclick="openAggregateDialog(this)" title="Group the matching docs by selected field(s)"><i class="bi bi-bar-chart"></i></button>
      <button class="btn btn-sm btn-outline-danger py-0 px-2" onclick="clearAllFilters()" title="Clear all column filters"><i class="bi bi-x-circle me-1"></i>Clear Filters</button>
      <button class="btn btn-sm btn-outline-info py-0 px-2" onclick="generateQueryFromFilters()" title="Create ES query from active filters (opens in the main window)"><i class="bi bi-funnel"></i></button>
      <button class="btn btn-sm btn-outline-light py-0 px-2" onclick="refreshActiveViewer()" title="Reload the data"><i class="bi bi-arrow-clockwise"></i></button>
    </header>
    <div id="out"><pre>No results yet…</pre></div></body></html>`);
  resultsWindow.document.close();

  // Bind the interactive handlers into the pop-out's global scope so the shared
  // markup's onclick names resolve (they run in the main window's context,
  // updating shared state and re-rendering BOTH windows via refreshTables).
  ['editCell', 'deleteCell', 'toggleRowSel', 'toggleAllRows', 'toggleColSel',
   'toggleTableSort', 'toggleColFilter', 'rtFilterSearch', 'rtFilterToggleAll',
   'applyColFilter', 'clearColFilter', 'clearSelection', 'deleteSelectedRows',
   'columnFieldOp', 'applySuggestionField', 'dismissSuggestion',
   'setResultView', 'downloadResults', 'exportAll', 'toggleWriteMode',
   'showJsonCell', 'showJsonModal', 'clearAllFilters', 'generateQueryFromFilters',
   'openFieldVisibility', 'renderFieldVisibilityBody', 'setColumnHidden',
   'setUnmappedHidden', 'setAllMappedHidden', 'showAllColumns', 'refreshActiveViewer',
   'openAggregateDialog']
    .forEach(fn => { try { resultsWindow[fn] = window[fn]; } catch (_) {} });

  // Close filter dropdowns on outside click inside the pop-out.
  resultsWindow.document.addEventListener('click', (e) => {
    if (e.target.closest('.rt-filter') || e.target.closest('.rt-funnel')) return;
    resultsWindow.document.querySelectorAll('.rt-filter:not(.d-none)').forEach(p => p.classList.add('d-none'));
  });

  syncResultsPopout();
}

/* ══════════════════════════════════════════════════════════════════════════
   CONNECTION — FORM HELPERS
   ══════════════════════════════════════════════════════════════════════════ */

/** Read current scheme radio value */
function getScheme() {
  return document.querySelector('input[name="connScheme"]:checked')?.value || 'http';
}

/** Fill the form fields from a settings object */
function fillForm(s) {
  document.getElementById('connLabel').value = s.label || '';
  document.getElementById('connHost').value  = s.host  || '';
  document.getElementById('connPort').value  = s.port  || 9200;
  document.getElementById('connUser').value  = s.user  || '';
  document.getElementById('connPass').value  = s.password || '';
  document.getElementById('connVerify').checked = !!s.verify_certs;
  const schemeRadio = document.querySelector(`input[name="connScheme"][value="${s.scheme || 'http'}"]`);
  if (schemeRadio) schemeRadio.checked = true;
  // SSH fallback — default to enabled
  document.getElementById('connSshEnabled').checked = s.ssh_enabled !== false;  // default true
  document.getElementById('connSshUser').value = s.ssh_user || '';
  document.getElementById('connSshPass').value = s.ssh_password || '';
  document.getElementById('connSshPort').value = s.ssh_port || 22;
  toggleSshFields();
}

/** Read form into a settings object */
function readForm() {
  return {
    label:        document.getElementById('connLabel').value.trim(),
    host:         document.getElementById('connHost').value.trim(),
    port:         parseInt(document.getElementById('connPort').value) || 9200,
    scheme:       getScheme(),
    user:         document.getElementById('connUser').value.trim(),
    password:     document.getElementById('connPass').value,
    verify_certs: document.getElementById('connVerify').checked,
    ssh_enabled:  document.getElementById('connSshEnabled').checked,
    ssh_user:     document.getElementById('connSshUser').value.trim(),
    ssh_password: document.getElementById('connSshPass').value,
    ssh_port:     parseInt(document.getElementById('connSshPort').value) || 22,
  };
}

function clearForm() {
  fillForm({ port: 9200, scheme: 'http', ssh_port: 22, ssh_enabled: true });
  document.getElementById('formTitle').textContent = 'New Connection';
  hideFeedback();
}

/** Show/hide the SSH credential fields based on the enable checkbox */
function toggleSshFields() {
  const on = document.getElementById('connSshEnabled').checked;
  document.getElementById('sshFields').classList.toggle('d-none', !on);
}

function toggleSshPassVis() {
  const inp  = document.getElementById('connSshPass');
  const icon = document.getElementById('sshPassEyeIcon');
  if (inp.type === 'password') {
    inp.type = 'text';
    icon.className = 'bi bi-eye-slash';
  } else {
    inp.type = 'password';
    icon.className = 'bi bi-eye';
  }
}

function togglePassVis() {
  const inp  = document.getElementById('connPass');
  const icon = document.getElementById('passEyeIcon');
  if (inp.type === 'password') {
    inp.type = 'text';
    icon.className = 'bi bi-eye-slash';
  } else {
    inp.type = 'password';
    icon.className = 'bi bi-eye';
  }
}

/* ══════════════════════════════════════════════════════════════════════════
   CONNECTION — CONNECT / DISCONNECT
   ══════════════════════════════════════════════════════════════════════════ */

async function doConnect() {
  const settings = readForm();

  if (!settings.host) {
    showFeedback('danger', '<i class="bi bi-exclamation-triangle me-2"></i>Please enter the CC machine IP / hostname.');
    return;
  }

  // Update button state to "connecting"
  const btn = document.getElementById('btnConnect');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Connecting…';
  setNavbarConnecting();
  const sshNote = settings.ssh_enabled
    ? ' <span class="text-secondary">(SSH port-open enabled as fallback)</span>' : '';
  showFeedback('info', '<i class="bi bi-hourglass-split me-2"></i>Connecting to Elasticsearch…' + sshNote);

  try {
    const res  = await fetch(appUrl('/api/connect'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings),
    });
    const data = await res.json();

    if (data.connected) {
      // Persist as last-used
      localStorage.setItem(LS_ACTIVE, JSON.stringify(settings));
      isConnected = true;
      onConnected(settings, data);
      const sshMsg = data.ssh_tunnel
        ? `<br><small class="text-warning"><i class="bi bi-hdd-network me-1"></i>Connected via SSH tunnel: <code>${(data.tunnel||'').replace(/</g,'&lt;')}</code></small>`
        : data.ssh_opened_port
        ? `<br><small class="text-warning"><i class="bi bi-unlock me-1"></i>ES port opened via SSH: <code>${(data.ssh_command||'').replace(/</g,'&lt;')}</code></small>`
        : '';
      showFeedback('success',
        `<i class="bi bi-check-circle-fill me-2"></i>Connected! ` +
        `<strong>${data.cluster_name}</strong> — ES ${data.es_version}` + sshMsg
      );
      // Auto-navigate to dashboard after short delay
      setTimeout(() => showView('dashboard'), 900);
      refreshAll();
    } else {
      isConnected = false;
      onDisconnected();

      // Provide more specific error messages
      let errorMsg = data.error || 'Connection failed';
      let errorHtml = `<i class="bi bi-x-circle-fill me-2"></i>${errorMsg}`;

      // Check for SSH-specific errors
      if (errorMsg.includes('SSH') || errorMsg.includes('ssh')) {
        if (errorMsg.includes('authentication')) {
          errorHtml = `<i class="bi bi-x-circle-fill me-2"></i>SSH authentication failed. Check the SSH username and password.`;
        } else if (!settings.ssh_user || !settings.ssh_password) {
          errorHtml = `<i class="bi bi-x-circle-fill me-2"></i>ES port isn't reachable directly. SSH credentials are required to tunnel to it.
            <br><small class="text-secondary">Please provide the SSH username and password.</small>`;
        }
      } else if (errorMsg.includes('Connection refused') || errorMsg.includes('timed out') || errorMsg.includes('port')) {
        if (settings.ssh_enabled && (!settings.ssh_user || !settings.ssh_password)) {
          errorHtml = `<i class="bi bi-x-circle-fill me-2"></i>ES port appears blocked. SSH credentials are needed to tunnel to it.
            <br><small class="text-secondary">SSH is enabled but username/password are missing.</small>`;
        }
      }

      showFeedback('danger', errorHtml);
    }
  } catch (e) {
    isConnected = false;
    onDisconnected();
    showFeedback('danger', `<i class="bi bi-x-circle-fill me-2"></i>Network error: ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-plug-fill me-1"></i>Connect';
  }
}

function disconnect() {
  isConnected = false;
  localStorage.removeItem(LS_ACTIVE);
  onDisconnected();
  showToast('Disconnected from Elasticsearch', 'bg-secondary');
  showView('connection');
}

/* ══════════════════════════════════════════════════════════════════════════
   CONNECTION — UI STATE
   ══════════════════════════════════════════════════════════════════════════ */

function onConnected(settings, info) {
  syncDbStatus();
  const displayHost = settings.label || settings.host;
  const cluster     = info.cluster_name || '';
  const version     = info.es_version   || '';
  const machineStr  = `${settings.host}:${settings.port}`;

  // Navbar pill — the MACHINE we are pointed at, and nothing about a
  // particular store on it. Cluster name and engine version moved to the
  // Elasticsearch node in the sidebar tree: they describe one datastore, and
  // once Postgres and MariaDB sit beside it a single version up here could
  // only ever be right about one of them.
  document.getElementById('connectedPill').classList.remove('d-none');
  document.getElementById('disconnectedPill').classList.add('d-none');
  document.getElementById('pillMachine').textContent = displayHost;

  // Standalone, MariaDB lives on the machine we just connected to, so its
  // reachability changes with this connection — re-probe rather than leaving
  // the node reporting what was true for the previous CC.
  loadMariaHealth();
  mariaSchemas = [];        // they belonged to the previous CC
  mariaTableList = [];
  mariaSchema = ''; mariaTable = '';
  // The detail pane too, or the new CC's screen opens showing the previous
  // one's columns and rows under a heading that names neither.
  mariaColumns = []; mariaSample = null; mariaRelations = null;

  // Same reasoning, same reset, for PostgreSQL.
  loadPgHealth();
  pgDatabases = []; pgTableList = [];
  pgDatabase = ''; pgTable = '';
  pgColumns = []; pgSample = null; pgRelations = null;

  // The store's own identity, on the store's own row.
  const meta = document.getElementById('db-es-meta');
  if (meta) {
    meta.textContent = [cluster, version && `ES ${version}`].filter(Boolean).join(' · ');
    meta.title = meta.textContent;   // full text when the sidebar truncates it
  }

  // Sidebar box — machine identity only, so it earns its space in standalone
  // (which host am I on?) and is redundant embedded, where the answer is
  // always "this CC" and the navbar already says so.
  const box = document.getElementById('sidebarConnBox');
  if (can('es.connect')) {
    box.classList.remove('d-none');
    document.getElementById('sidebarMachine').textContent = displayHost;
    document.getElementById('sidebarCluster').textContent = machineStr;
  } else {
    box.classList.add('d-none');
  }
}

function onDisconnected() {
  document.getElementById('connectedPill').classList.add('d-none');
  document.getElementById('disconnectedPill').classList.remove('d-none');
  document.getElementById('sidebarConnBox').classList.add('d-none');
  // Clear the store's identity with the connection — a stale cluster name and
  // version under a disconnected node reads as if it were still live.
  const meta = document.getElementById('db-es-meta');
  if (meta) { meta.textContent = ''; meta.title = ''; }
  // "Connect to see indices" is an instruction only where connecting is a
  // thing the user does. Embedded it is not — the connection is the compose
  // file's — so say what is actually true.
  document.getElementById('sidebarIndices').innerHTML = can('es.connect')
    ? '<div class="text-secondary small px-2 py-2">Connect to see indices</div>'
    : '<div class="text-secondary small px-2 py-2">Indices unavailable</div>';
  allIndices = [];
}

function setNavbarConnecting() {
  // Flash the disconnected pill to a "connecting" state
  const pill = document.getElementById('disconnectedPill');
  const dot  = pill.querySelector('.conn-dot');
  if (dot) { dot.classList.remove('disconnected'); dot.classList.add('connecting'); }
}

/* ══════════════════════════════════════════════════════════════════════════
   SAVED PROFILES
   ══════════════════════════════════════════════════════════════════════════ */

function loadProfiles() {
  try { return JSON.parse(localStorage.getItem(LS_PROFILES) || '[]'); }
  catch { return []; }
}

function saveProfiles(profiles) {
  localStorage.setItem(LS_PROFILES, JSON.stringify(profiles));
}

function saveProfile() {
  const s = readForm();
  if (!s.host) {
    showFeedback('warning', '<i class="bi bi-exclamation-triangle me-2"></i>Enter a host before saving.');
    return;
  }
  const profiles = loadProfiles();
  // Avoid duplicate hosts — update if same host:port exists
  const existing = profiles.findIndex(p => p.host === s.host && p.port === s.port);
  if (existing >= 0) {
    profiles[existing] = s;
  } else {
    profiles.push(s);
  }
  saveProfiles(profiles);
  renderProfiles();
  showFeedback('success', '<i class="bi bi-bookmark-check me-2"></i>Profile saved!');
}

function deleteProfile(index) {
  const profiles = loadProfiles();
  profiles.splice(index, 1);
  saveProfiles(profiles);
  renderProfiles();
}

function loadProfileIntoForm(index) {
  const profiles = loadProfiles();
  const p = profiles[index];
  if (p) {
    fillForm(p);
    document.getElementById('formTitle').textContent = `Edit — ${p.label || p.host}`;
    hideFeedback();
  }
}

function renderProfiles() {
  const profiles = loadProfiles();
  const container = document.getElementById('savedProfilesList');
  const badge = document.getElementById('profileCount');
  badge.textContent = profiles.length;

  if (!profiles.length) {
    container.innerHTML = '<p class="text-secondary small text-center py-2 mb-0">No saved profiles yet</p>';
    return;
  }

  container.innerHTML = profiles.map((p, i) => `
    <div class="profile-item border-bottom" onclick="loadProfileIntoForm(${i})">
      <i class="bi bi-hdd-network text-info"></i>
      <div>
        <div class="profile-name">${esc(p.label || p.host)}</div>
        <div class="profile-host">${esc(p.scheme)}://${esc(p.host)}:${p.port}${p.user ? ' · ' + esc(p.user) : ''}</div>
      </div>
      <div class="d-flex gap-1 ms-auto">
        <button class="btn btn-sm btn-link text-primary p-0 btn-del"
                onclick="event.stopPropagation(); connectFromProfile(${i})" title="Connect">
          <i class="bi bi-plug-fill"></i>
        </button>
        <button class="btn btn-sm btn-link text-danger p-0 btn-del"
                onclick="event.stopPropagation(); deleteProfile(${i})" title="Delete">
          <i class="bi bi-trash3"></i>
        </button>
      </div>
    </div>`).join('');
}

async function connectFromProfile(index) {
  loadProfileIntoForm(index);
  await doConnect();
}

/* ═════════════════════════════════════��════════════════════════════════════
   DATA LOADING
   ══════════════════════════════════════════════════════════════════════════ */

async function refreshAll() {
  await Promise.all([loadClusterHealth(), loadIndices(), loadAttackSummary()]);
}

async function loadClusterHealth() {
  try {
    const data = await api('/api/health');
    if (!data.connected) return;
    setText('c-status', data.status?.toUpperCase() || '—');
    document.getElementById('c-status').className = `stat-value health-${data.status}`;
    setText('c-nodes',      data.number_of_nodes   ?? '—');
    setText('c-shards',     data.active_shards      ?? '—');
    setText('c-unassigned', data.unassigned_shards  ?? '—');
    setText('c-version',    data.es_version         ?? '—');

    // ── Explain a non-green status / unassigned shards in a COPYABLE popover ──
    const notGreen = data.status && data.status !== 'green';
    _setHealthPopover('statusCard', notGreen, () => _statusReasonText(data));
    const hasUnassigned = (data.unassigned_shards || 0) > 0 ||
                          (data.unassigned_detail || []).length > 0;
    _setHealthPopover('unassignedCard', hasUnassigned, () => _unassignedReasonText(data));
  } catch (_) {}
}

/** Human-readable "why is the cluster not green" text (copyable). */
function _statusReasonText(d) {
  const lines = [`Cluster status: ${(d.status || '?').toUpperCase()}`
                 + (d.cluster_name ? `  (cluster: ${d.cluster_name})` : '')];
  const idx = d.unhealthy_indices || [];
  if (!idx.length) {
    lines.push('', 'No per-index detail reported by Elasticsearch.');
  } else {
    lines.push('', `${idx.length} index(es) not green:`);
    for (const i of idx) {
      lines.push(`  • ${i.index} — ${(i.status || '').toUpperCase()}`
        + ` (unassigned ${i.unassigned_shards ?? 0}, active ${i.active_shards ?? 0}`
        + `${i.initializing_shards ? `, initializing ${i.initializing_shards}` : ''}`
        + `${i.relocating_shards ? `, relocating ${i.relocating_shards}` : ''})`);
    }
  }
  const det = d.unassigned_detail || [];
  if (det.length) {
    lines.push('', 'Unassigned/relocating shards:');
    for (const s of det) lines.push(`  • ${s.index} [shard ${s.shard} ${s.type}]`
      + ` ${s.state}${s.reason ? ` — ${s.reason}` : ''}`);
  }
  return lines.join('\n');
}

/** Everything about the unassigned shards (copyable). */
function _unassignedReasonText(d) {
  const det = d.unassigned_detail || [];
  const lines = [`Unassigned shards: ${d.unassigned_shards ?? det.length}`];
  if (!det.length) { lines.push('', 'No per-shard detail reported.'); return lines.join('\n'); }
  lines.push('');
  for (const s of det) {
    lines.push(`• ${s.index}`);
    lines.push(`    shard ${s.shard} · ${s.type} · ${s.state}`);
    if (s.reason) lines.push(`    reason: ${s.reason}`);
    if (s.node)   lines.push(`    node: ${s.node}`);
  }
  const replicaOnly = det.every(s => s.type === 'replica');
  if (replicaOnly) {
    lines.push('', 'All unassigned shards are REPLICAS — common on a single-node',
                   'cluster (no second node to hold the copy). Data is fully',
                   'available; the cluster is yellow rather than red.');
  }
  return lines.join('\n');
}

/** Attach (or remove) a hover popover on a stat card. `active` gates it so a
 *  green cluster shows nothing. `textFn` builds the copyable text lazily. */
function _setHealthPopover(cardId, active, textFn) {
  const card = document.getElementById(cardId);
  if (!card) return;
  card._healthText = active ? textFn : null;
  card.classList.toggle('health-card-active', !!active);
  if (card._healthBound) return;              // handlers attached once
  card._healthBound = true;

  let pop = null, hideTimer = null;
  const clear = () => { if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; } };
  const hide  = () => { clear(); if (pop) { pop.remove(); pop = null; } };
  const scheduleHide = () => { clear(); hideTimer = setTimeout(hide, 250); };

  const show = () => {
    if (!card._healthText) return;
    clear();
    if (pop) return;
    const text = card._healthText();
    pop = document.createElement('div');
    pop.className = 'health-popover';
    pop.innerHTML = `<div class="health-popover-head">
        <span>Details</span>
        <button class="health-copy" title="Copy to clipboard"><i class="bi bi-clipboard"></i> Copy</button>
      </div>
      <pre class="health-popover-body"></pre>`;
    pop.querySelector('.health-popover-body').textContent = text;
    pop.querySelector('.health-copy').addEventListener('click', async () => {
      const btn = pop.querySelector('.health-copy');
      try { await navigator.clipboard.writeText(text); }
      catch { const r = document.createRange(); r.selectNodeContents(pop.querySelector('.health-popover-body'));
              const s = getSelection(); s.removeAllRanges(); s.addRange(r); document.execCommand('copy'); }
      btn.innerHTML = '<i class="bi bi-check2"></i> Copied';
      setTimeout(() => { if (btn.isConnected) btn.innerHTML = '<i class="bi bi-clipboard"></i> Copy'; }, 1500);
    });
    pop.addEventListener('mouseenter', clear);
    pop.addEventListener('mouseleave', scheduleHide);
    document.body.appendChild(pop);
    const r = card.getBoundingClientRect();
    pop.style.top  = `${window.scrollY + r.bottom + 6}px`;
    pop.style.left = `${window.scrollX + r.left}px`;
  };

  card.addEventListener('mouseenter', show);
  card.addEventListener('mouseleave', scheduleHide);
}

async function loadIndices() {
  try {
    const data = await api('/api/indices?cc_only=false');
    if (data.error) return;
    allIndices = data.indices || [];
    renderSidebarIndices(allIndices);
    renderIndicesTable(allIndices);
  } catch (_) {}
}

/** Categories the user has collapsed in the sidebar (persisted). */
const LS_CAT_COLLAPSED = 'cc_es_cat_collapsed';
let collapsedCats = new Set(JSON.parse(localStorage.getItem(LS_CAT_COLLAPSED) || '[]'));

const _catSlug = (cat) => 'cat_' + cat.replace(/[^a-z0-9]+/gi, '_');

function toggleCategory(cat) {
  if (collapsedCats.has(cat)) collapsedCats.delete(cat);
  else collapsedCats.add(cat);
  localStorage.setItem(LS_CAT_COLLAPSED, JSON.stringify([...collapsedCats]));

  const slug = _catSlug(cat);
  const body = document.getElementById('cat-body-' + slug);
  const chev = document.getElementById('cat-chev-' + slug);
  const collapsed = collapsedCats.has(cat);
  if (body) body.classList.toggle('d-none', collapsed);
  if (chev) chev.className = 'bi ms-auto ' + (collapsed ? 'bi-chevron-right' : 'bi-chevron-down');
}

function renderSidebarIndices(indices) {
  const container = document.getElementById('sidebarIndices');
  if (!indices.length) {
    container.innerHTML = '<div class="text-secondary small px-2 py-2">No indices found</div>';
    return;
  }
  const groups = {};
  for (const idx of indices) {
    const cat = idx.cc_meta?.category || 'Other';
    (groups[cat] = groups[cat] || []).push(idx);
  }
  let html = '';
  for (const [cat, list] of Object.entries(groups)) {
    const collapsed = collapsedCats.has(cat);
    const slug      = _catSlug(cat);
    const catArg    = cat.replace(/'/g, "\\'");
    html += `<div class="category-header cat-toggle d-flex align-items-center" onclick="toggleCategory('${catArg}')">
      <span>${esc(cat)}</span>
      <span class="cat-count ms-2">${list.length}</span>
      <i id="cat-chev-${slug}" class="bi ms-auto ${collapsed ? 'bi-chevron-right' : 'bi-chevron-down'}"></i>
    </div>`;
    html += `<div id="cat-body-${slug}" class="${collapsed ? 'd-none' : ''}">`;
    for (const idx of list) {
      const hColor = idx.health === 'green' ? '#198754' : idx.health === 'yellow' ? '#ffc107' : '#dc3545';
      html += `<button class="index-btn" onclick="showIndexDetail('${esc(idx.name)}')" title="${esc(idx.name)}">
        <span class="dot" style="background:${hColor};"></span>${esc(idx.name)}</button>`;
    }
    html += `</div>`;
  }
  container.innerHTML = html;
}

/* ── Dashboard indices: search filter + multi-select deletion ─────────────── */
let _dashboardIndexFilter = '';
let selectedIndices = new Set();      // index names checked for bulk deletion

/* Above this many documents in a selection, CSV export is not offered at all —
   only the native ES snapshot. See exportSelectedIndices() for why withdrawing
   the option beats recommending against it. */
const CSV_EXPORT_DOC_LIMIT = 50_000;

function onDashboardIndexSearch(inp) {
  _dashboardIndexFilter = (inp.value || '').toLowerCase();
  renderIndicesTable(allIndices);
}

function toggleIndexSel(name, cb) {
  if (cb.checked) selectedIndices.add(name); else selectedIndices.delete(name);
  updateDeleteSelectedBtn();
}

function toggleAllIndexSel(cb) {
  for (const idx of _dashboardVisibleIndices(allIndices)) {
    if (cb.checked) selectedIndices.add(idx.name); else selectedIndices.delete(idx.name);
  }
  renderIndicesTable(allIndices);
}

function updateDeleteSelectedBtn() {
  const n = selectedIndices.size;
  for (const [btnId, cntId] of [['btnDeleteSelected', 'delSelCount'],
                                ['btnExportSelected', 'expSelCount']]) {
    const cnt = document.getElementById(cntId);
    if (cnt) cnt.textContent = n;
    document.getElementById(btnId)?.classList.toggle('d-none', n === 0);
  }
}

/** Export the checked indices. Server archive is the default (works for ANY
 *  size — the backend scrolls ES and gzips the CSV; the browser only downloads
 *  the finished file). Direct browser download remains for small indices. */
async function exportSelectedIndices() {
  const names = [...selectedIndices];
  if (!names.length) return;
  // Snapshot (native ES, fast) is recommended for big selections; CSV for small.
  const docsIn = (n) => allIndices.find(i => i.name === n)?.docs_count || 0;
  const totalDocs = names.reduce((s, n) => s + docsIn(n), 0);
  const snapRec = totalDocs > 10_000;

  // Past this, BOTH CSV routes are withdrawn rather than merely un-recommended.
  // Scrolling every document into a .csv.gz is linear in document count and the
  // browser route holds the whole thing in a tab; at this size the honest
  // options are one. Offering a control that will realistically time out — and
  // that an engineer will pick because it is familiar — costs them the twenty
  // minutes before it fails, which is worse than not offering it.
  // The rule is the SELECTION total, which subsumes "any one index is over":
  // if a single index exceeds the limit, so does any selection containing it.
  const biggest = names.reduce((m, n) => Math.max(m, docsIn(n)), 0);
  const csvBlocked = totalDocs > CSV_EXPORT_DOC_LIMIT;

  const mode = await uiChoice(document, {
    title: `Export ${names.length} ${names.length > 1 ? 'indices' : 'index'} `
         + `— ${totalDocs.toLocaleString()} docs`,
    message: csvBlocked
      ? 'Snapshot archive: native ES snapshot on the ES machine, zipped and pulled to '
        + 'this server. It is the only method offered for this selection — '
        + `${totalDocs.toLocaleString()} documents`
        + (biggest > CSV_EXPORT_DOC_LIMIT
            ? ` (largest single index ${biggest.toLocaleString()})`
            : '')
        + ` is past the ${CSV_EXPORT_DOC_LIMIT.toLocaleString()}-document limit for CSV, `
        + 'which scrolls every document one page at a time and would not finish in a '
        + 'reasonable time. Select fewer or smaller indices to use CSV.'
      : 'Snapshot archive: native ES snapshot on the ES machine, zipped and pulled to '
        + 'this server — fastest for large data (millions of docs); the recommended method. '
        + 'CSV archive: the backend scrolls every document into <index>.csv.gz — '
        + 'universal but far slower at scale. '
        + 'Direct download streams through this browser tab — small indices only.',
    buttons: [
      { value: 'snapshot', text: 'Snapshot archive' + (snapRec ? ' (recommended for this size)' : ''),
        cls: snapRec ? 'btn-info' : 'btn-outline-info' },
      ...(csvBlocked ? [] : [
        { value: 'server',   text: 'CSV archive on server' + (snapRec ? '' : ' (recommended)'),
          cls: snapRec ? 'btn-outline-info' : 'btn-info' },
        { value: 'browser',  text: 'Direct browser download', cls: 'btn-outline-primary' },
      ]),
      { value: null,       text: 'Cancel', cls: 'btn-outline-secondary' },
    ],
  });
  if (!mode) return;
  if (mode === 'snapshot') { await snapshotSelectedIndices(names); return; }
  if (mode === 'server') {
    const res = await api('/api/exports', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ indices: names }),
    });
    if (!res || res.error) { showToast('Export failed to start: ' + (res?.error || 'unknown'), 'bg-danger'); return; }
    showToast(`Archiving ${names.length} ${names.length > 1 ? 'indices' : 'index'} on the server…`, 'bg-info');
    openArchivesPanel();
    return;
  }
  await exportSelectedIndicesBrowser(names);
}

/* ── Snapshot archives (native ES snapshots over SSH) ─────────────────────── */

/** Modal asking for the ES machine's SSH credentials.
 *  Resolves to {user, password, remember} or null. */
function sshCredsModal(host, why) {
  return new Promise(resolve => {
    const wrap = document.createElement('div');
    wrap.className = 'rt-modal-overlay';
    wrap.innerHTML = `<div class="rt-modal" style="min-width:420px;">
        <div class="rt-modal-title"><i class="bi bi-key me-1"></i>SSH login to ${esc(host)}</div>
        <div class="rt-modal-body">
          <div class="small text-secondary mb-2">${esc(why || 'Snapshot archives move files on the ES machine — a root login is required.')}</div>
          <label class="form-label small mb-0">User</label>
          <input class="form-control form-control-sm mb-2 ssh-user" value="root">
          <label class="form-label small mb-0">Password</label>
          <input type="password" class="form-control form-control-sm mb-2 ssh-pass">
          <div class="form-check">
            <input type="checkbox" class="form-check-input ssh-remember" id="sshRemember" checked>
            <label class="form-check-label small" for="sshRemember">Remember on this server (encrypted)</label>
          </div>
        </div>
        <div class="rt-modal-actions">
          <button class="btn btn-sm btn-primary" data-ok="1">Connect</button>
          <button class="btn btn-sm btn-outline-secondary" data-ok="0">Cancel</button>
        </div></div>`;
    document.body.appendChild(wrap);
    const done = (v) => { wrap.remove(); document.removeEventListener('keydown', onKey); resolve(v); };
    const grab = () => ({
      user: wrap.querySelector('.ssh-user').value.trim() || 'root',
      password: wrap.querySelector('.ssh-pass').value,
      remember: wrap.querySelector('.ssh-remember').checked,
    });
    const onKey = (e) => { if (e.key === 'Escape') done(null); else if (e.key === 'Enter') done(grab()); };
    document.addEventListener('keydown', onKey);
    wrap.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b) { done(b.getAttribute('data-ok') === '1' ? grab() : null); return; }
      if (e.target === wrap) done(null);
    });
    setTimeout(() => wrap.querySelector('.ssh-pass').focus(), 0);
  });
}

/** POST to a snapshot endpoint, handling the need-credentials handshake
 *  (prompt → retry with ssh creds). Returns the final response or null. */
async function _snapshotApiWithCreds(url, payload) {
  let res = await api(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  while (res && res.need_credentials) {
    const creds = await sshCredsModal(res.host, res.error);
    if (!creds) return null;
    res = await api(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...payload, ssh: creds }),
    });
  }
  return res;
}

/** Snapshot-archive the given indices under a user-chosen significant name. */
async function snapshotSelectedIndices(names) {
  let snapName = null;
  for (;;) {
    snapName = await uiPrompt(document, {
      title: 'Snapshot archive name (e.g. belnet_recovery)',
      value: snapName || '', okText: 'Create snapshot' });
    if (snapName == null) return;
    snapName = snapName.trim();
    if (/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(snapName)) break;
    showToast('Name must be letters/digits/-/_ (max 64, start with letter or digit)', 'bg-warning');
  }
  const res = await _snapshotApiWithCreds('/api/exports/snapshot',
    { indices: names, name: snapName });
  if (!res) return;
  if (res.error) { showToast('Snapshot failed to start: ' + res.error, 'bg-danger'); return; }
  showToast(`Snapshotting ${names.length} ${names.length > 1 ? 'indices' : 'index'} as "${snapName}"…`, 'bg-info');
  openArchivesPanel();
}

/** Restore a snapshot archive (.zip) into the machine of the connected ES:
 *  shows the embedded index list, warns about ones that already exist. */
async function snapshotRestoreFlow(name) {
  const info = await api(`/api/exports/meta/${encodeURIComponent(name)}`);
  const meta = info?.meta || {};
  const indices = meta.indices || [];

  // With a known index list, let the user restore a SUBSET; without one we can
  // only offer the whole snapshot (ES resolves the real list server-side).
  let chosen = [];
  if (indices.length) {
    chosen = await _snapshotIndexPicker(name, indices, meta);
    if (!chosen) return false;                       // cancelled
  } else {
    const ok = await uiConfirm(document, {
      title: `Restore snapshot "${name}" on the connected ES machine?`,
      message: 'Index list unknown (no embedded metadata) — every index in the '
             + 'snapshot will be restored with its original name. '
             + (meta.source ? `Taken from ${meta.source}.` : ''),
      okText: 'Restore' });
    if (!ok) return false;
  }

  if (!await confirmSharedCc(`restore snapshot "${name}" into this CC`)) return false;
  const body = { filename: name };
  // Send the subset only when it IS a subset — an empty list means "all".
  if (chosen.length && chosen.length !== indices.length) body.indices = chosen;
  const res = await _snapshotApiWithCreds('/api/exports/snapshot/restore', body);
  if (!res) return false;
  if (res.error) { showToast('Snapshot restore failed to start: ' + res.error, 'bg-danger'); return false; }
  showToast(`Restoring snapshot "${name}"…`, 'bg-info');
  refreshArchivesPanel();
  return true;
}

/** Checkbox list of a snapshot's indices. Resolves to the chosen names, or
 *  null when cancelled. Indices that already exist here are pre-unchecked and
 *  flagged — a native restore cannot overwrite an existing index. */
function _snapshotIndexPicker(name, indices, meta) {
  return new Promise(resolve => {
    const exists = new Set(indices.filter(ix => allIndices.some(i => i.name === ix)));
    const wrap = document.createElement('div');
    wrap.className = 'rt-modal-overlay';
    wrap.innerHTML = `<div class="rt-modal" style="min-width:600px;width:820px;max-width:95vw;
          max-height:90vh;display:flex;flex-direction:column;">
        <div class="rt-modal-title" style="flex:0 0 auto;">
          <i class="bi bi-box-arrow-in-down me-1"></i>Restore “${esc(name)}” — choose indices</div>
        <div class="rt-modal-body" style="flex:0 0 auto;white-space:normal;">
          This snapshot holds <b>${indices.length}</b> ${indices.length > 1 ? 'indices' : 'index'}${
            meta.source ? `, taken from <b>${esc(meta.source)}</b>` : ''}.
          They are restored under their original names.
          ${exists.size ? `<div class="text-warning mt-1">
            <i class="bi bi-exclamation-triangle-fill me-1"></i>${exists.size} already exist on this
            machine and are unchecked — a restore cannot overwrite an existing index
            (delete it first, or leave it out).</div>` : ''}
        </div>
        <div class="d-flex gap-2 align-items-center mb-2" style="flex:0 0 auto;">
          <input type="text" class="form-control form-control-sm sip-filter" placeholder="Filter…" style="max-width:220px;">
          <button class="btn btn-sm btn-outline-secondary py-0" data-act="all">Select all</button>
          <button class="btn btn-sm btn-outline-secondary py-0" data-act="none">Select none</button>
          ${exists.size ? `<button class="btn btn-sm btn-outline-secondary py-0" data-act="new">Only new</button>` : ''}
        </div>
        <div class="border border-secondary rounded" style="flex:1 1 auto;min-height:0;overflow:auto;">
          <table class="table table-sm table-hover mb-0" style="font-size:0.8rem;">
            <tbody>${indices.map((ix, i) => `<tr class="sip-row" data-name="${esc(ix)}">
              <td style="width:32px;"><input type="checkbox" class="sip-cb" data-i="${i}"
                  ${exists.has(ix) ? '' : 'checked'}></td>
              <td class="font-monospace">${esc(ix)}</td>
              <td class="text-end">${exists.has(ix)
                  ? '<span class="badge bg-warning text-dark">exists here</span>' : ''}</td>
            </tr>`).join('')}</tbody>
          </table>
        </div>
        <div class="rt-modal-actions" style="flex:0 0 auto;">
          <span class="sip-count small text-info me-auto"></span>
          <button class="btn btn-sm btn-primary" data-ok="1">Restore selected</button>
          <button class="btn btn-sm btn-outline-secondary" data-ok="0">Cancel</button>
        </div></div>`;
    document.body.appendChild(wrap);

    const boxes = () => [...wrap.querySelectorAll('.sip-cb')];
    const picked = () => boxes().filter(b => b.checked)
                                .map(b => indices[+b.dataset.i]);
    const sync = () => {
      const n = picked().length;
      wrap.querySelector('.sip-count').textContent =
        `${n} of ${indices.length} selected`;
      wrap.querySelector('[data-ok="1"]').disabled = n === 0;
    };
    const done = (v) => { wrap.remove(); document.removeEventListener('keydown', onKey); resolve(v); };
    const onKey = (e) => { if (e.key === 'Escape') done(null); };
    document.addEventListener('keydown', onKey);
    wrap.addEventListener('input', (e) => {
      if (e.target.classList.contains('sip-filter')) {
        const q = e.target.value.trim().toLowerCase();
        wrap.querySelectorAll('.sip-row').forEach(r =>
          r.classList.toggle('d-none', !!q && !r.dataset.name.toLowerCase().includes(q)));
        return;
      }
      sync();
    });
    wrap.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) { if (e.target === wrap) done(null); return; }
      const act = b.dataset.act;
      if (act === 'all')  { boxes().forEach(c => c.checked = true);  sync(); return; }
      if (act === 'none') { boxes().forEach(c => c.checked = false); sync(); return; }
      if (act === 'new')  { boxes().forEach(c => c.checked = !exists.has(indices[+c.dataset.i])); sync(); return; }
      if (b.hasAttribute('data-ok')) done(b.getAttribute('data-ok') === '1' ? picked() : null);
    });
    sync();
  });
}

/** Direct client-side export (small indices): folder picker where supported,
 *  otherwise regular downloads named "<index>.csv". */
async function exportSelectedIndicesBrowser(names) {

  // Ask for the destination folder where supported.
  let dirHandle = null;
  if (window.showDirectoryPicker) {
    try {
      dirHandle = await window.showDirectoryPicker({ mode: 'readwrite' });
    } catch (e) {
      if (e && e.name === 'AbortError') return;   // user cancelled the picker
      dirHandle = null;                           // not permitted → fallback
    }
  }
  if (!dirHandle) {
    showToast('Folder picker unavailable — files will go to the browser\'s Downloads folder', 'bg-info');
  }

  let done = 0; const failures = [];
  for (const name of names) {
    showToast(`Exporting ${done + failures.length + 1}/${names.length}: ${name}…`, 'bg-secondary');
    try {
      const res = await fetch(appUrl('/api/query/export'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          per_index_queries: [{ index: name, query_body: { query: { match_all: {} } } }],
          format: 'csv',
          max_rows: 10_000_000,      // "all documents" — effectively uncapped
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 150)}`);

      if (dirHandle) {
        // Stream the response straight into "<index>.csv" in the chosen folder.
        const fileHandle = await dirHandle.getFileHandle(`${name}.csv`, { create: true });
        await res.body.pipeTo(await fileHandle.createWritable());
      } else {
        const blob = await res.blob();
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = `${name}.csv`;
        document.body.appendChild(a); a.click();
        setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
      }
      done++;
    } catch (e) {
      failures.push(`${name}: ${e.message || e}`);
    }
  }

  showToast(`Exported ${done}/${names.length} ${names.length === 1 ? 'index' : 'indices'}`
    + (dirHandle ? ` to "${dirHandle.name}"` : '')
    + (failures.length ? ` — ${failures.length} failed` : ''),
    failures.length ? 'bg-warning' : 'bg-success');
  if (failures.length) console.warn('Index export failures:', failures);
}

async function deleteSelectedIndices() {
  const names = [...selectedIndices];
  if (!names.length) return;
  if (!await confirmSharedCc(`delete ${names.length} ${names.length > 1 ? 'indices' : 'index'}`)) return;
  const ok = await uiConfirm(document, {
    title: `Delete ${names.length} ${names.length > 1 ? 'indices' : 'index'}?`,
    message: 'This permanently deletes: ' + names.join(', '),
    okText: 'Delete', danger: true,
  });
  if (!ok) return;
  let deleted = 0; const failures = [];
  for (const name of names) {
    const res = await api(`/api/indices/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (res && !res.error) { deleted++; selectedIndices.delete(name); }
    else failures.push(`${name}: ${res?.error || 'unknown'}`);
  }
  showToast(`Deleted ${deleted} ${deleted === 1 ? 'index' : 'indices'}`
    + (failures.length ? ` — ${failures.length} failed` : ''),
    failures.length ? 'bg-warning' : 'bg-success');
  if (failures.length) console.warn('Bulk index delete failures:', failures);
  if (names.includes(_currentIndexName)) _currentIndexName = null;
  await loadIndices();
}

/* ── Archives panel — server-side exports, downloads, restore/upload ──────── */
let _archivesTimer = null;
const _archSelected = new Set();   // archive names checked in the panel
let _archFilter = '';              // filter typed in the panel (name/type/source)
let _archLastFiles = [];           // last file list fetched (re-render on filter)
let _archSort = { key: 'name', dir: 1 };   // clickable column-header sort

function closeArchivesPanel() {
  if (_archivesTimer) { clearInterval(_archivesTimer); _archivesTimer = null; }
  document.querySelector('.rt-modal-overlay.rt-archives')?.remove();
}

function openArchivesPanel() {
  closeArchivesPanel();
  _archSelected.clear();
  _archFilter = '';
  _archLastFiles = [];
  const wrap = document.createElement('div');
  wrap.className = 'rt-modal-overlay rt-archives';
  // Resizable (drag the bottom-right corner) and never taller than the
  // viewport: the body scrolls, the title and footer buttons stay reachable.
  wrap.innerHTML = `<div class="rt-modal" style="min-width:620px;width:780px;max-width:95vw;
        max-height:90vh;min-height:260px;display:flex;flex-direction:column;
        resize:both;overflow:hidden;">
      <div class="rt-modal-title" style="flex:0 0 auto;"><i class="bi bi-archive me-1"></i>Index Archives (on server)</div>
      <div class="rt-modal-body" style="flex:1 1 auto;min-height:0;overflow:auto;">
        <div class="arch-jobs mb-2" style="max-height:45vh;overflow:auto;"></div>
        <div class="arch-bulk d-flex gap-1 mb-1 align-items-center">
          <input type="text" class="form-control form-control-sm arch-filter"
                 placeholder="Filter name / type / source…" style="max-width:200px;"
                 title="Matches archive name, type (snap/csv) and source machine">
          <span class="arch-filtercount small text-secondary me-1"></span>
          <button class="btn btn-sm btn-outline-success" data-act="dl-sel" disabled>
            <i class="bi bi-download me-1"></i>Download selected (<span class="arch-selcount">0</span>)</button>
          <button class="btn btn-sm btn-outline-info" data-act="restore-sel" disabled>
            <i class="bi bi-box-arrow-in-up me-1"></i>Restore selected</button>
          <button class="btn btn-sm btn-outline-danger" data-act="del-sel" disabled>
            <i class="bi bi-trash me-1"></i>Delete selected</button>
        </div>
        <div class="arch-files" style="overflow:auto;max-height:52vh;"></div>
      </div>
      <div class="rt-modal-actions" style="flex:0 0 auto;">
        <button class="btn btn-sm btn-outline-primary" data-act="upload">
          <i class="bi bi-upload me-1"></i>Upload archive(s)…</button>
        <button class="btn btn-sm btn-secondary" data-act="close">Close</button>
      </div>
    </div>`;
  document.body.appendChild(wrap);
  wrap.addEventListener('click', (e) => {
    const th = e.target.closest('th[data-sort]');
    if (th) {                                  // toggle column sort (same col → flip)
      const key = th.getAttribute('data-sort');
      _archSort = { key, dir: _archSort.key === key ? -_archSort.dir : 1 };
      _renderArchFiles(wrap);
      return;
    }
    const b = e.target.closest('button');
    if (!b) { if (e.target === wrap) closeArchivesPanel(); return; }
    const act = b.getAttribute('data-act');
    if (act === 'close') closeArchivesPanel();
    else if (act === 'upload') uploadArchive();
    else if (act === 'dl-sel') downloadArchives([..._archSelected]);
    else if (act === 'restore-sel') restoreArchives([..._archSelected]);
    else if (act === 'del-sel') deleteArchives([..._archSelected]);
  });
  // Checkbox selection (delegated so it survives the 2 s table re-render).
  wrap.addEventListener('change', (e) => {
    const cb = e.target;
    if (cb.classList?.contains('arch-sel')) {
      cb.checked ? _archSelected.add(cb.dataset.name) : _archSelected.delete(cb.dataset.name);
      _updateArchBulkButtons(wrap);
    } else if (cb.classList?.contains('arch-sel-all')) {
      wrap.querySelectorAll('.arch-sel').forEach(x => {
        x.checked = cb.checked;
        cb.checked ? _archSelected.add(x.dataset.name) : _archSelected.delete(x.dataset.name);
      });
      _updateArchBulkButtons(wrap);
    }
  });
  // Filter box lives in the once-rendered toolbar so typing keeps focus while
  // the table re-renders (both on input and on the 2 s poll).
  wrap.querySelector('.arch-filter').addEventListener('input', (e) => {
    _archFilter = e.target.value.trim().toLowerCase();
    _renderArchFiles(wrap);
  });
  refreshArchivesPanel();
  _archivesTimer = setInterval(refreshArchivesPanel, 2000);
}

function _updateArchBulkButtons(wrap) {
  wrap = wrap || document.querySelector('.rt-modal-overlay.rt-archives');
  if (!wrap) return;
  const n = _archSelected.size;
  wrap.querySelector('.arch-selcount').textContent = n;
  wrap.querySelectorAll('.arch-bulk button').forEach(b => { b.disabled = !n; });
  const all = wrap.querySelector('.arch-sel-all');
  const boxes = [...wrap.querySelectorAll('.arch-sel')];
  if (all) all.checked = boxes.length > 0 && boxes.every(b => b.checked);
}

const _fmtBytes = (n) => n >= 1 << 30 ? (n / (1 << 30)).toFixed(2) + ' GB'
                       : n >= 1 << 20 ? (n / (1 << 20)).toFixed(1) + ' MB'
                       : (n / 1024).toFixed(1) + ' KB';

async function refreshArchivesPanel() {
  const wrap = document.querySelector('.rt-modal-overlay.rt-archives');
  if (!wrap) { closeArchivesPanel(); return; }
  let data;
  try { data = await api('/api/exports'); } catch (e) { return; }
  if (!data || data.error) return;

  // ── Jobs (running first) ──────────────────────────────────────────────────
  const jobsEl = wrap.querySelector('.arch-jobs');
  // Errors stay visible until the user dismisses them (acknowledge);
  // done/cancelled cards auto-hide after 5 min (or on dismiss).
  const jobs = (data.jobs || []).filter(j =>
    j.status === 'running' || j.status === 'error' ||
    (j.finished_at && (Date.now() - Date.parse(j.finished_at)) < 5 * 60_000));
  jobsEl.innerHTML = jobs.length ? jobs.map(j => {
    const badge = j.status === 'running'
      ? '<span class="badge bg-info text-dark">running</span>'
      : j.status === 'done'
        ? '<span class="badge bg-success">done</span>'
        : j.status === 'cancelled'
          ? '<span class="badge bg-secondary">cancelled</span>'
          : `<span class="badge bg-danger" title="${esc(j.error || '')}">error</span>`;
    const cancelBtn = j.status === 'running'
      ? `<button class="btn btn-sm btn-outline-warning py-0 px-1 ms-auto"
                 onclick="cancelArchiveJob('${jsq(j.id)}')"
                 title="Stop this job">Cancel</button>`
      : `<button class="btn btn-sm btn-outline-secondary py-0 px-1 ms-auto"
                 onclick="ackArchiveJob('${jsq(j.id)}')"
                 title="Dismiss this ${j.status === 'error' ? 'error ' : ''}message">✕</button>`;
    const items = j.items.map(it => {
      const pct = it.total ? Math.min(100, Math.round(it.done / it.total * 100)) : null;
      let label;
      if (it.phase) {                        // snapshot jobs: phase + typed progress
        const prog = it.unit === 'bytes'
          ? (it.total ? `${_fmtBytes(it.done)} / ${_fmtBytes(it.total)}` : _fmtBytes(it.done))
          : it.total != null
            ? `${it.done.toLocaleString()} / ${it.total.toLocaleString()} ${it.unit || ''}`
            : '';
        label = `<span class="badge bg-dark border">${esc(it.phase)}</span> ${prog}`.trim();
      } else {
        label = it.total != null
          ? `${it.done.toLocaleString()} / ${(it.total ?? 0).toLocaleString()} docs`
          : `${it.done.toLocaleString()} docs`;
      }
      return `<div class="small">${esc(it.index)} — ${label}
          ${pct != null ? `<div class="progress" style="height:5px;">
            <div class="progress-bar ${j.status === 'error' ? 'bg-danger' : 'bg-info'}" style="width:${pct}%"></div>
          </div>` : ''}</div>`;
    }).join('');
    const err = j.status === 'error' && j.error
      ? `<div class="small text-danger">${esc(j.error)}</div>` : '';
    return `<div class="border border-secondary rounded p-2 mb-1">
        <div class="d-flex align-items-center gap-2">
          <i class="bi ${j.kind === 'snapshot' ? 'bi-camera'
                       : j.kind === 'snap-restore' ? 'bi-camera-reels'
                       : j.kind === 'export' ? 'bi-box-arrow-down' : 'bi-box-arrow-in-up'}"></i>
          <span class="small fw-semibold">${j.kind}</span>${badge}${cancelBtn}
        </div>${items}${err}</div>`;
  }).join('') : '';

  // ── Archive files ─────────────────────────────────────────────────────────
  _archLastFiles = data.files || [];
  // Drop selections for files that no longer exist on the server.
  const names = new Set(_archLastFiles.map(f => f.name));
  for (const n of [..._archSelected]) if (!names.has(n)) _archSelected.delete(n);
  _renderArchFiles(wrap);
}

/** Render the archive-files table, applying the filter (matches name, type
 *  and source) and the clickable-header sort. The select-all checkbox acts on
 *  the VISIBLE (filtered) rows — filter then one click. */
function _renderArchFiles(wrap) {
  const filesEl = wrap.querySelector('.arch-files');
  const typeLabel = (f) => f.type === 'snapshot' ? 'snap' : 'csv';
  let files = _archFilter
    ? _archLastFiles.filter(f =>
        f.name.toLowerCase().includes(_archFilter) ||
        typeLabel(f).includes(_archFilter) || (f.type || '').includes(_archFilter) ||
        (f.source || '').toLowerCase().includes(_archFilter))
    : [..._archLastFiles];
  const { key, dir } = _archSort;
  files.sort((a, b) => {
    const av = key === 'size' ? (a.size || 0)
             : key === 'type' ? typeLabel(a)
             : (a[key] || '').toLowerCase();
    const bv = key === 'size' ? (b.size || 0)
             : key === 'type' ? typeLabel(b)
             : (b[key] || '').toLowerCase();
    return (av < bv ? -1 : av > bv ? 1 : 0) * dir;
  });
  wrap.querySelector('.arch-filtercount').textContent =
    _archFilter ? `${files.length}/${_archLastFiles.length}` : '';
  const sortTh = (k, label, cls = '') =>
    `<th data-sort="${k}" class="${cls}" style="cursor:pointer;user-select:none;"
         title="Sort by ${label.toLowerCase()}">${label}${
         key === k ? (dir === 1 ? ' ▲' : ' ▼') : ''}</th>`;
  filesEl.innerHTML = files.length
    ? `<table class="table table-sm table-hover mb-0" style="font-size:0.78rem;">
        <thead class="table-dark"><tr>
          <th style="width:1.6rem;"><input type="checkbox" class="form-check-input arch-sel-all"
              title="Select all${_archFilter ? ' filtered results' : ''}"></th>
          ${sortTh('type', 'Type')}${sortTh('name', 'Archive')}${sortTh('source', 'Source')}
          ${sortTh('size', 'Size', 'text-end')}${sortTh('mtime', 'Created (UTC)')}
          <th class="text-end">Actions</th></tr></thead>
        <tbody>${files.map(f => `<tr>
          <td><input type="checkbox" class="form-check-input arch-sel" data-name="${esc(f.name)}"
                     ${_archSelected.has(f.name) ? 'checked' : ''}></td>
          <td><span class="badge ${f.type === 'snapshot' ? 'bg-warning text-dark' : 'bg-secondary'}"
                  title="${f.type === 'snapshot' ? 'Native ES snapshot archive' : 'Document (CSV) archive'}"
                  >${f.type === 'snapshot' ? 'SNAP' : 'CSV'}</span></td>
          <td class="font-monospace">${esc(f.name)}</td>
          <td title="Machine the data was exported from">${esc(f.source || '—')}</td>
          <td class="text-end">${_fmtBytes(f.size)}</td>
          <td>${esc((f.mtime || '').replace('T', ' ').slice(0, 19))}</td>
          <td class="text-end text-nowrap">
            <a class="btn btn-sm btn-outline-success py-0 px-1 me-1"
               href="${appUrl('/api/exports/download/' + encodeURIComponent(f.name))}" download
               title="Download this archive"><i class="bi bi-download"></i></a>
            <button class="btn btn-sm btn-outline-info py-0 px-1 me-1"
                    onclick="restoreArchives(['${jsq(f.name)}'])"
                    title="Restore into an index on the connected ES"><i class="bi bi-box-arrow-in-up"></i></button>
            <button class="btn btn-sm btn-outline-danger py-0 px-1"
                    onclick="deleteArchives(['${jsq(f.name)}'])"
                    title="Delete this archive from the server"><i class="bi bi-trash"></i></button>
          </td></tr>`).join('')}</tbody>
      </table>`
    : _archLastFiles.length
      ? `<div class="text-secondary small p-2">No archives match "${esc(_archFilter)}".</div>`
      : '<div class="text-secondary small p-2">No archives yet — check indices on the dashboard and use "Export selected".</div>';
  _updateArchBulkButtons(wrap);
}

/** Ask the backend to stop a running export/restore job. */
async function cancelArchiveJob(jobId) {
  const res = await api(`/api/exports/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
  if (!res || res.error) { showToast('Cancel failed: ' + (res?.error || 'unknown'), 'bg-danger'); return; }
  showToast(res.note || 'Job cancelling — completed archives are kept', 'bg-info');
  refreshArchivesPanel();
}

/** Acknowledge (dismiss) a finished job card — errors stay until dismissed. */
async function ackArchiveJob(jobId) {
  const res = await api(`/api/exports/jobs/${encodeURIComponent(jobId)}`, { method: 'DELETE' });
  if (!res || res.error) { showToast('Dismiss failed: ' + (res?.error || 'unknown'), 'bg-danger'); return; }
  refreshArchivesPanel();
}

/** Download several archives — one browser download per file (the browser may
 *  ask to allow multiple downloads from this site; that's expected). */
async function downloadArchives(names) {
  for (const name of names) {
    const a = document.createElement('a');
    a.href = appUrl('/api/exports/download/' + encodeURIComponent(name));
    a.download = name;
    document.body.appendChild(a); a.click();
    setTimeout(() => a.remove(), 0);
    await new Promise(r => setTimeout(r, 400));   // let each download register
  }
  showToast(`Started ${names.length} download${names.length > 1 ? 's' : ''}`, 'bg-success');
}

/** Ask for a target index name per archive/file. The default is always the
 *  file-name stem (archive X.csv.gz → index X). One entry → a plain prompt;
 *  several → per-file choice with "Keep default for ALL" to skip the rest of
 *  the prompts. Returns [{i, target}] (skipped entries omitted) or null when
 *  the whole operation was cancelled. `labels` = [{name, detail?}]. */
async function _chooseRestoreTargets(labels, okText) {
  const out = [];
  let keepAll = false;
  for (let i = 0; i < labels.length; i++) {
    const { name, detail } = labels[i];
    const stem = name.replace(/\.csv(\.gz)?$/, '');
    let target = stem;
    if (labels.length === 1) {
      const t = await uiPrompt(document, {
        title: `Restore "${name}"${detail || ''} — target index name`,
        value: stem, okText: okText || 'Restore' });
      if (t == null || !t.trim()) return null;
      target = t.trim();
    } else if (!keepAll) {
      const choice = await uiChoice(document, {
        title: `${i + 1}/${labels.length}: ${name}${detail || ''}`,
        message: `Target index name (from the file name): "${stem}"`,
        buttons: [
          { value: 'keep',     text: `Keep "${stem}"`,        cls: 'btn-info' },
          { value: 'keep-all', text: 'Keep defaults for ALL', cls: 'btn-outline-info' },
          { value: 'modify',   text: 'Modify…',               cls: 'btn-outline-primary' },
          { value: 'skip',     text: 'Skip this one',         cls: 'btn-outline-secondary' },
        ] });
      if (!choice) return null;                       // Escape/backdrop → abort all
      if (choice === 'skip') continue;
      if (choice === 'keep-all') keepAll = true;
      if (choice === 'modify') {
        const t = await uiPrompt(document, {
          title: `Target index for "${name}"`, value: stem, okText: 'OK' });
        if (t == null || !t.trim()) continue;         // no name → skip this one
        target = t.trim();
      }
    }
    out.push({ i, target });
  }
  return out.length ? out : null;
}

/** Read the header row of a plain .csv the user picked (first 64 KB is plenty).
 *  Returns the column names, or null when it can't be read. */
async function _csvHeader(file) {
  try {
    const head = await file.slice(0, 65536).text();
    // The export writes a "#cc-es-archive …" comment line before the header.
    const line = head.split(/\r?\n/).find(l => l.trim() && !l.startsWith('#'));
    if (!line) return null;
    return line.split(',').map(s => s.trim().replace(/^"|"$/g, ''));
  } catch (_) {
    return null;
  }
}

/** Ask where the imported/restored documents get their `_id` from.
 *  Resolves to the column name ('' = let ES generate), or null when cancelled.
 *  `columns` (optional) is the file's header — used to validate a custom pick. */
async function _chooseIdSource(title, columns) {
  const hasId = !columns || columns.includes('_id');
  const buttons = [];
  if (hasId) {
    buttons.push({ value: '_id', text: 'Keep the file\'s _id', cls: 'btn-primary' });
  }
  buttons.push({ value: '', text: 'Let Elasticsearch generate ids',
                 cls: hasId ? 'btn-outline-primary' : 'btn-primary' });
  buttons.push({ value: '_pick', text: 'Take the id from a field…', cls: 'btn-outline-info' });
  buttons.push({ value: null, text: 'Cancel', cls: 'btn-outline-secondary' });
  const choice = await uiChoice(document, {
    title,
    message: (hasId
      ? 'Keeping the id means a document that already exists is OVERWRITTEN rather than '
      + 'duplicated. Generating ids always adds new documents. '
      : 'This file has no _id column. ')
      + 'Some CC families keep the id in a field as well — on dp-attack-raw* the _id IS '
      + 'the attackIpsId — so you can point at that field instead.',
    buttons,
  });
  if (choice == null) return null;
  if (choice !== '_pick') return choice;
  const guess = (columns || []).find(c => /^attackIpsId$/i.test(c)) || '';
  const col = await uiPrompt(document, {
    title: 'Field holding the document id',
    value: guess, okText: 'Use this field' });
  if (col == null || !col.trim()) return null;
  const c = col.trim();
  if (columns && !columns.includes(c)) {
    showToast(`"${c}" is not a column in this file`, 'bg-danger');
    return null;
  }
  return c;
}

/** Put the chosen id source on a restore FormData. `col` is '' for
 *  ES-generated ids — sent as an explicit `generate_ids` flag, because FastAPI
 *  resolves an empty-string Form value back to the field's default ('_id'). */
function _appendIdSource(fd, col) {
  if (col === '') fd.append('generate_ids', 'true');
  else fd.append('id_column', col);
}

/** One combined warning for restore targets that already exist in ES. */
async function _confirmExistingTargets(targets) {
  const existing = [...new Set(targets.filter(t => allIndices.some(ix => ix.name === t)))];
  if (!existing.length) return true;
  return uiConfirm(document, {
    title: existing.length > 1
      ? `${existing.length} target indices already exist`
      : `Index "${existing[0]}" already exists`,
    message: 'Restored documents will be ADDED to: ' + existing.join(', ')
           + '. Docs with the same _id are overwritten.',
    okText: 'Restore anyway' });
}

/** Restore one or more server-side archives into the connected ES.
 *  Snapshot (.zip) archives use the native flow (original index names, SSH);
 *  CSV archives prompt for target index names as before. */
async function restoreArchives(names) {
  if (!names.length) return;
  if (!await confirmSharedCc(`restore ${names.length} archive(s) into this CC`)) return;
  const zips = names.filter(n => n.endsWith('.zip'));
  names = names.filter(n => !n.endsWith('.zip'));
  for (const z of zips) {
    if (!await snapshotRestoreFlow(z)) break;   // cancelled → stop the batch
  }
  if (!names.length) return;
  const chosen = await _chooseRestoreTargets(names.map(n => ({ name: n })));
  if (!chosen) return;
  if (!await _confirmExistingTargets(chosen.map(c => c.target))) return;
  const idCol = await _chooseIdSource(
    `Restore ${chosen.length} archive(s) — document ids`);
  if (idCol === null) return;
  let started = 0; const failures = [];
  for (const c of chosen) {
    const fd = new FormData();
    fd.append('filename', names[c.i]);
    fd.append('target', c.target);
    _appendIdSource(fd, idCol);
    const res = await api('/api/exports/restore', { method: 'POST', body: fd });
    if (res && !res.error) started++;
    else failures.push(`${names[c.i]}: ${res?.error || 'unknown'}`);
  }
  showToast(`Started ${started} restore${started === 1 ? '' : 's'}`
    + (failures.length ? ` — ${failures.length} failed` : ''),
    failures.length ? 'bg-warning' : 'bg-info');
  if (failures.length) console.warn('Restore failures:', failures);
  refreshArchivesPanel();
}

/** Upload one or more .csv/.csv.gz archives (e.g. exported on another machine)
 *  and restore each into the ES this analyzer is connected to. Default index
 *  name = the file name stem. */
function uploadArchive() {
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.multiple = true;
  inp.accept = '.gz,.csv,.zip,application/gzip,text/csv,application/zip';
  inp.onchange = async () => {
    let files = [...(inp.files || [])];
    if (!files.length) return;
    // Snapshot zips: upload (save) first, then run the native restore flow —
    // no target-name prompt (a snapshot restores its original index names).
    const zipFiles = files.filter(f => f.name.endsWith('.zip'));
    files = files.filter(f => !f.name.endsWith('.zip'));
    for (const f of zipFiles) {
      showToast(`Uploading and checking ${f.name}…`, 'bg-secondary');
      const fd = new FormData();
      fd.append('file', f, f.name);
      const res = await api('/api/exports/restore', { method: 'POST', body: fd });
      if (!res || res.error) {
        // The server validates the zip and refuses to keep a broken one.
        showToast(`Upload of ${f.name} rejected: ` + (res?.error || 'unknown'), 'bg-danger');
        continue;
      }
      refreshArchivesPanel();
      const v = res.validation || {};
      const summary = [
        v.integrity ? `Integrity: ${v.integrity}.` : '',
        v.indices?.length ? `${v.indices.length} index/indices inside.` : '',
        v.meta?.source ? `Taken from ${v.meta.source}.` : '',
        (v.warnings || []).join(' '),
      ].filter(Boolean).join(' ');
      // Stored is the safe default — restoring writes indices into this CC.
      const next = await uiChoice(document, {
        title: `"${f.name}" uploaded and verified`,
        message: `${summary}\n\nIt is now in the Archives repository. Restore it into the connected CC now?`,
        buttons: [
          { value: 'keep',    text: 'Keep in archives only', cls: 'btn-primary' },
          { value: 'restore', text: 'Restore now…',          cls: 'btn-warning' },
        ],
      });
      if (next === 'restore' && !await snapshotRestoreFlow(res.saved || f.name)) break;
    }
    if (!files.length) return;
    const chosen = await _chooseRestoreTargets(
      files.map(f => ({ name: f.name, detail: ` (${_fmtBytes(f.size)})` })),
      'Upload & Restore');
    if (!chosen) return;
    if (!await _confirmExistingTargets(chosen.map(c => c.target))) return;
    // Plain .csv uploads expose their header; .csv.gz is gzipped, so the
    // dialog falls back to assuming the standard export shape.
    const plain = files[chosen[0].i];
    const idCol = await _chooseIdSource(
      `Upload & restore ${chosen.length} archive(s) — document ids`,
      plain && plain.name.endsWith('.csv') ? await _csvHeader(plain) : null);
    if (idCol === null) return;
    let started = 0; const failures = [];
    for (const c of chosen) {
      const f = files[c.i];
      showToast(`Uploading ${f.name}…`, 'bg-secondary');
      const fd = new FormData();
      fd.append('file', f, f.name);
      fd.append('target', c.target);
      _appendIdSource(fd, idCol);
      const res = await api('/api/exports/restore', { method: 'POST', body: fd });
      if (res && !res.error) started++;
      else failures.push(`${f.name}: ${res?.error || 'unknown'}`);
    }
    showToast(`Started ${started} restore${started === 1 ? '' : 's'}`
      + (failures.length ? ` — ${failures.length} failed` : ''),
      failures.length ? 'bg-warning' : 'bg-info');
    if (failures.length) console.warn('Upload failures:', failures);
    refreshArchivesPanel();
  };
  inp.click();
}

/** Delete one or more archives from the server (indices are not touched). */
async function deleteArchives(names) {
  if (!names.length) return;
  const ok = await uiConfirm(document, {
    title: names.length > 1 ? `Delete ${names.length} archives?` : `Delete archive "${names[0]}"?`,
    message: 'Removes from the server: ' + names.join(', ')
           + '. Indices in Elasticsearch are not touched.',
    okText: 'Delete', danger: true });
  if (!ok) return;
  let deleted = 0; const failures = [];
  for (const name of names) {
    const res = await api(`/api/exports/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (res && !res.error) { deleted++; _archSelected.delete(name); }
    else failures.push(`${name}: ${res?.error || 'unknown'}`);
  }
  showToast(`Deleted ${deleted} archive${deleted === 1 ? '' : 's'}`
    + (failures.length ? ` — ${failures.length} failed` : ''),
    failures.length ? 'bg-warning' : 'bg-success');
  if (failures.length) console.warn('Archive delete failures:', failures);
  refreshArchivesPanel();
}

/** The rows the dashboard table currently shows (search filter applied). */
/* ── Index time ranges ──────────────────────────────────────────────────────
   The window an index covers, derived server-side from its "-sl-<N>" suffix
   and its family's slice length (modules/es/discovery.slice_window). Rendered
   in UTC and labelled as such, because the slice arithmetic IS in UTC and a
   range silently shown in the browser's zone would not line up with the index
   name an engineer is reading beside it. */

let _dashboardRangeFrom = null;   // ms, inclusive; null = unbounded
let _dashboardRangeTo   = null;   // ms, exclusive; null = unbounded
let _dashboardRangeHidden = 0;    // indices dropped for having no derivable range

function _fmtUtcMinute(ms) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getUTCDate()}/${d.getUTCMonth() + 1}/${d.getUTCFullYear()} `
       + `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
}

/** "13/8/2026 00:00 > 20/8/2026 00:00 (UTC)", or null when not derivable. */
function fmtIndexRange(tr) {
  if (!tr || !tr.start_ms || !tr.end_ms) return null;
  return `${_fmtUtcMinute(tr.start_ms)} > ${_fmtUtcMinute(tr.end_ms)} (UTC)`;
}

/** Does an index's window overlap the filter window at all?
 *  Half-open [start, end) on both sides, so an index ending exactly when the
 *  filter starts does NOT count — it holds no document inside the filter. */
function _rangeOverlaps(tr) {
  if (_dashboardRangeFrom === null && _dashboardRangeTo === null) return true;
  if (!tr || !tr.start_ms || !tr.end_ms) return false;   // caller counts these
  if (_dashboardRangeTo !== null && tr.start_ms >= _dashboardRangeTo) return false;
  if (_dashboardRangeFrom !== null && tr.end_ms <= _dashboardRangeFrom) return false;
  return true;
}

function _dashboardVisibleIndices(indices) {
  let out = indices;
  if (_dashboardIndexFilter) {
    out = out.filter(i =>
      i.name.toLowerCase().includes(_dashboardIndexFilter) ||
      (i.cc_meta?.category || '').toLowerCase().includes(_dashboardIndexFilter));
  }
  _dashboardRangeHidden = 0;
  if (_dashboardRangeFrom !== null || _dashboardRangeTo !== null) {
    // Count ONLY the indices dropped because they cannot be dated — not every
    // index the filter excluded. An index that simply falls outside the window
    // was answered correctly and needs no explanation; one the tool could not
    // place is a gap in the answer, and the engineer should know it exists
    // rather than conclude the data is not on this machine.
    const undatable = (tr) => !tr || !tr.start_ms || !tr.end_ms;
    _dashboardRangeHidden = out.filter(i => undatable(i.time_range)).length;
    out = out.filter(i => _rangeOverlaps(i.time_range));
  }
  return out;
}

/** Read the two date inputs into the filter state and re-render. */
function applyIndexRangeFilter() {
  const parse = (id) => {
    const v = document.getElementById(id)?.value;
    if (!v) return null;
    // datetime-local has no zone; the slice arithmetic is UTC, and the column
    // says UTC, so the typed value is read as UTC to match what is on screen.
    const ms = Date.parse(v + 'Z');
    return Number.isNaN(ms) ? null : ms;
  };
  _dashboardRangeFrom = parse('idxRangeFrom');
  _dashboardRangeTo   = parse('idxRangeTo');
  renderIndicesTable(allIndices);
}

function clearIndexRangeFilter() {
  _dashboardRangeFrom = _dashboardRangeTo = null;
  const a = document.getElementById('idxRangeFrom'), b = document.getElementById('idxRangeTo');
  if (a) a.value = ''; if (b) b.value = '';
  renderIndicesTable(allIndices);
}

function renderIndicesTable(indices) {
  const tbody = document.getElementById('indicesTableBody');
  // Drop selections for indices that no longer exist.
  const known = new Set(indices.map(i => i.name));
  for (const n of [...selectedIndices]) if (!known.has(n)) selectedIndices.delete(n);

  const visible = _dashboardVisibleIndices(indices);
  const selAll = document.getElementById('idxSelAll');
  if (selAll) selAll.checked = visible.length > 0 && visible.every(i => selectedIndices.has(i.name));
  updateDeleteSelectedBtn();

  if (!visible.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="text-center text-secondary py-3">${
      indices.length ? 'No indices match the search' : 'No indices found'}</td></tr>`;
  } else {
    tbody.innerHTML = visible.map(idx => {
      const hClass = idx.health === 'green' ? 'success' : idx.health === 'yellow' ? 'warning' : 'danger';
      const cat = idx.cc_meta?.category || '<span class="text-secondary">—</span>';
      const rangeTxt = fmtIndexRange(idx.time_range);
      return `<tr onclick="showIndexDetail('${esc(idx.name)}')">
        <td onclick="event.stopPropagation()">
          <input type="checkbox" ${selectedIndices.has(idx.name) ? 'checked' : ''}
                 onclick="toggleIndexSel('${jsq(idx.name)}', this)"/></td>
        <td class="fw-semibold">${esc(idx.name)}</td>
        <td><span class="badge bg-${hClass}">${idx.health || '?'}</span></td>
        <td>${(idx.docs_count ?? 0).toLocaleString()}</td>
        <td>${idx.store_size || '—'}</td>
        <td class="text-nowrap small${rangeTxt ? '' : ' text-secondary'}"
            title="${rangeTxt ? 'Derived from the -sl-N suffix in the index name and the family slice length'
                              : 'No slice number in the name, or the family slice length is unknown'}">
          ${rangeTxt ? esc(rangeTxt) : '—'}</td>
        <td>${cat}</td>
        <td class="text-end text-nowrap">
          ${can('es.index.duplicate') ? `<button class="btn btn-sm btn-outline-info py-0 px-1 me-1"
                  onclick="event.stopPropagation(); duplicateIndex('${jsq(idx.name)}')"
                  title="Duplicate this index (optionally shifting dates)"><i class="bi bi-copy"></i></button>` : ''}
          <button class="btn btn-sm btn-outline-danger py-0 px-1 me-1"
                  onclick="event.stopPropagation(); deleteIndexByName('${jsq(idx.name)}')"
                  title="Delete this index"><i class="bi bi-trash"></i></button>
          <i class="bi bi-chevron-right text-secondary"></i>
        </td>
      </tr>`;
    }).join('');
  }
  const note = document.getElementById('idxRangeNote');
  if (note) {
    note.textContent = _dashboardRangeHidden
      ? `${_dashboardRangeHidden} ${_dashboardRangeHidden > 1 ? 'indices' : 'index'} hidden — no derivable time range`
      : '';
    note.classList.toggle('d-none', !_dashboardRangeHidden);
  }
  if (indicesView === 'csv') setIndicesView('csv');   // keep CSV view in sync
}

/* ── Dashboard indices: Table / CSV views ──────────────────────────────────── */
let indicesView = 'table';

/** Flatten the indices list into plain rows for CSV/table export. */
function indicesRows() {
  return (allIndices || []).map(i => ({
    name: i.name, health: i.health, status: i.status,
    docs_count: i.docs_count, store_size: i.store_size,
    primaries: i.primaries, replicas: i.replicas,
    category: i.cc_meta?.category || '',
    time_range_utc: fmtIndexRange(i.time_range) || '',
  }));
}

function setIndicesView(mode) {
  indicesView = mode;
  document.getElementById('iv-table')?.classList.toggle('active', mode === 'table');
  document.getElementById('iv-csv')?.classList.toggle('active', mode === 'csv');
  const wrap  = document.getElementById('indicesTableWrap');
  const csvEl = document.getElementById('indicesCsv');
  wrap?.classList.toggle('d-none', mode !== 'table');
  csvEl?.classList.toggle('d-none', mode !== 'csv');
  if (mode === 'csv' && csvEl) {
    const rows = indicesRows();
    csvEl.textContent = rows.length ? buildResultsCsv(rows) : 'No indices.';
  }
}

function downloadIndicesCsv() {
  const rows = indicesRows();
  if (!rows.length) { showToast('No indices to export', 'bg-warning'); return; }
  const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const blob = new Blob([buildResultsCsv(rows, null, true)], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `cc_indices_${ts}.csv`;
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
}

/* ── Index detail ─────────────────────────────────────────────────────────── */
let _currentIndexName = null;
let _preservedIndexFilters = {};  // Preserved filters when refreshing/changing Show count
let _preservedHiddenColumns = []; // Preserved column visibility across a reload
let _indexFullTotal = 0;          // total docs in the current index (match_all count)
let _indexSortField = null;       // date field the sample is sorted by (desc)

function refreshCurrentIndex() {
  if (_currentIndexName) {
    // Preserve current filters before reload.
    // NOTE: tableFilters values are Set objects — JSON.stringify would turn them
    // into empty {} and silently drop every filter, so serialise them as arrays.
    _preservedIndexFilters = {};
    for (const [col, set] of Object.entries(tableFilters)) {
      if (set && set.size) _preservedIndexFilters[col] = [...set];
    }
    _preservedHiddenColumns = [...hiddenColumns];   // keep the user's column visibility
    showIndexDetail(_currentIndexName, true);  // true = preserve filters
  }
}

/* ── Create / delete index · import CSV ─────────────────────────────────────── */

/** Prompt for a name and create a new (empty) index, then open it. */
async function createIndex() {
  const name = await uiPrompt(document, {
    title: 'Create new index — enter a lowercase name',
    value: '', okText: 'Create',
  });
  if (name == null) return;
  const clean = name.trim();
  if (!clean) return;
  const res = await api('/api/indices/create', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: clean }),
  });
  if (!res || res.error) { showToast('Create failed: ' + (res?.error || 'unknown'), 'bg-danger'); return; }
  showToast(`Index "${res.name}" created`, 'bg-success');
  await loadIndices();
  showIndexDetail(res.name);
}

/** "+Add" entry point: empty index by name, or a possible CC index from the
 *  live catalog filled with artificial data. */
async function addIndexChoice() {
  // Each branch offers only what this deployment can actually carry out. The
  // menu is built from capabilities rather than shown-and-disabled because a
  // choice that leads to a 404 is worse than a choice that is absent.
  if (!can('es.index.create')) return openPossibleIndexPicker();
  if (!can('es.artificial')) {
    const only = await uiChoice(document, {
      title: 'Add index',
      message: 'Create an empty index by name, or look at the CC index families '
             + 'this machine could produce (discovered live from its index '
             + 'templates).',
      buttons: [
        { value: 'empty',   text: 'Empty index',      cls: 'btn-primary' },
        { value: 'catalog', text: 'Possible indices', cls: 'btn-outline-info' },
        { value: null,      text: 'Cancel',           cls: 'btn-outline-secondary' },
      ],
    });
    if (only === 'empty') return createIndex();
    if (only === 'catalog') return openPossibleIndexPicker();
    return;
  }
  const choice = await uiChoice(document, {
    title: 'Add index',
    message: 'Create an empty index by name, or pick one of the CC indices this '
           + 'machine can create (discovered live from its index templates) and '
           + 'fill it with artificial data.',
    buttons: [
      { value: 'catalog', text: 'CC index + artificial data', cls: 'btn-warning' },
      { value: 'empty',   text: 'Empty index',                cls: 'btn-primary' },
      { value: null,      text: 'Cancel',                     cls: 'btn-outline-secondary' },
    ],
  });
  if (choice === 'empty') return createIndex();
  if (choice === 'catalog') return openPossibleIndexPicker();
}

/* ── Fetching-data script generator ──────────────────────────────────────── */

/** Paste indices → download a standalone script that snapshots them on a CC
 *  machine, producing the same zip the Archives panel makes. Used when the
 *  analyzer cannot reach that machine (no SSH, isolated site, customer box). */
function openFetchScriptDialog() {
  document.querySelector('.rt-modal-overlay.rt-fetchscript')?.remove();
  const wrap = document.createElement('div');
  wrap.className = 'rt-modal-overlay rt-fetchscript';
  wrap.innerHTML = `<div class="rt-modal" style="min-width:600px;width:780px;max-width:95vw;
        max-height:90vh;display:flex;flex-direction:column;">
      <div class="rt-modal-title" style="flex:0 0 auto;">
        <i class="bi bi-file-earmark-code me-1"></i>Fetching data script generator</div>
      <div class="rt-modal-body" style="flex:0 0 auto;white-space:normal;">
        Paste the indices to archive — <b>separated by commas or new lines</b>.
        You'll be asked for an archive name, then the script downloads.
        <div class="small text-secondary mt-1">
          Copy it to the CC machine and run it as root (<span class="font-monospace">sh fetch_&lt;name&gt;.sh</span>).
          It needs only <span class="font-monospace">sh</span>, <span class="font-monospace">curl</span>
          and <span class="font-monospace">zip</span>, and leaves a
          <span class="font-monospace">&lt;name&gt;.zip</span> you can upload here via
          <b>Archives → Upload archive</b>.</div>
      </div>
      <textarea class="form-control fs-indices" spellcheck="false"
                style="flex:1 1 auto;min-height:190px;font-family:monospace;font-size:0.8rem;"
                placeholder="dp-attack-raw-ty-dos-sid-0-sl-1472&#10;dp-hourly-applications-ty-dp-hourly-applications-sid-0-sl-2948&#10;&#10;…or: index-a, index-b, index-c"></textarea>
      <div class="d-flex align-items-center gap-2 mt-2" style="flex:0 0 auto;">
        <span class="fs-count small text-info me-auto">no indices yet</span>
        <label class="small text-secondary d-flex align-items-center gap-1"
               title="ES endpoint as seen FROM the CC machine — localhost is almost always right">
          ES URL on that machine:
          <input type="text" class="form-control form-control-sm fs-esurl"
                 value="http://localhost:9200" style="width:15rem;"></label>
      </div>
      <div class="rt-modal-actions" style="flex:0 0 auto;">
        <button class="btn btn-sm btn-warning" data-act="gen">
          <i class="bi bi-download me-1"></i>Generate</button>
        <button class="btn btn-sm btn-secondary" data-act="close">Close</button>
      </div></div>`;
  document.body.appendChild(wrap);

  const ta = wrap.querySelector('.fs-indices');
  const parse = () => [...new Set(ta.value.split(/[\s,]+/).map(s => s.trim()).filter(Boolean))];
  const sync = () => {
    const n = parse().length;
    wrap.querySelector('.fs-count').textContent =
      n ? `${n} ${n > 1 ? 'indices' : 'index'} to archive` : 'no indices yet';
  };
  ta.addEventListener('input', sync);
  wrap.addEventListener('click', async (e) => {
    if (e.target === wrap) { wrap.remove(); return; }
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.act === 'close') { wrap.remove(); return; }
    if (b.dataset.act !== 'gen') return;

    const indices = parse();
    if (!indices.length) { showToast('Paste at least one index name', 'bg-warning'); return; }
    const name = await uiPrompt(document, {
      title: 'Archive name — letters, digits, "-" and "_" (becomes <name>.zip)',
      value: '', okText: 'Generate',
    });
    if (name == null) return;
    const clean = name.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(clean)) {
      showToast('Invalid archive name', 'bg-danger'); return;
    }
    const res = await fetch(appUrl('/api/exports/script'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: clean, indices,
                             es_url: wrap.querySelector('.fs-esurl').value.trim() }),
    });
    // Errors come back as JSON; success is the script itself.
    if ((res.headers.get('Content-Type') || '').includes('application/json')) {
      const err = await res.json();
      showToast('Script generation failed: ' + (err.error || 'unknown'), 'bg-danger');
      return;
    }
    const text = await res.text();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type: 'text/x-shellscript' }));
    a.download = `fetch_${clean}.sh`;
    document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
    showToast(`fetch_${clean}.sh downloaded — run it on the CC machine`, 'bg-success');
    wrap.remove();
  });
  setTimeout(() => ta.focus(), 0);
}

/* ── Possible-indices picker (live catalog) ───────────────────────────────── */

function _fmtSliceMin(m) {
  if (m == null) return '—';
  if (m % 1440 === 0) { const d = m / 1440; return d === 1 ? '1 day' : `${d} days`; }
  if (m % 60 === 0)   { const h = m / 60;   return h === 1 ? '1 hour' : `${h} hours`; }
  return `${m} min`;
}

/** Doc type actually used in constructed names — the pattern/prefix-derived
 *  token embedded in example_now (template NAMES are unreliable: product bug). */
function _possibleDerivedType(f) {
  return (f.example_now?.match(/-ty-(.+?)-sid-/) || [])[1] || null;
}

/** Index name for "now" for family f with the chosen doc type. */
function _possibleName(f, docType) {
  if (f.slice_minutes && docType) {
    const prefix = (f.example_now || '').split('-ty-')[0]
      || f.index_pattern.replace(/-?\*$/, '').replace(/-ty-.*$/, '');
    const sl = Math.floor(Date.now() / (f.slice_minutes * 60000));
    return `${prefix}-ty-${docType}-sid-0-sl-${sl}`;
  }
  return f.example_now || '';
}

/** Modal listing every index family the connected machine can create
 *  (GET /api/indices/possible).
 *
 *  Two modes, decided by capability rather than by the caller, so every entry
 *  point gets the right one:
 *    - fill mode   — selecting a family leads into the artificial-data dialog
 *                    for a name constructed for the current time slice.
 *    - read-only   — the same catalog as a REFERENCE. This is the embedded
 *                    profile's version: what families can this CC produce,
 *                    what is each one's slice length, how many template
 *                    fields, and has it produced one yet. Answering that
 *                    changes nothing on the appliance, which is why it is
 *                    available on a customer's box when creating is not. */
async function openPossibleIndexPicker() {
  // Not a parameter: the picker is reachable from the toolbar, from the Add
  // menu and from the empty state, and the answer must be the same at each.
  const canFill = can('es.artificial');
  document.querySelector('.rt-modal-overlay.rt-possible')?.remove();
  const wrap = document.createElement('div');
  wrap.className = 'rt-modal-overlay rt-possible';
  wrap.innerHTML = `<div class="rt-modal" style="min-width:640px;width:900px;max-width:95vw;
        max-height:92vh;display:flex;flex-direction:column;resize:both;overflow:hidden;">
      <div class="rt-modal-title" style="flex:0 0 auto;">
        <i class="bi bi-collection me-1"></i>Possible CC indices — <span class="pp-src">discovering…</span>
        <button class="btn btn-sm btn-outline-secondary py-0 px-1 ms-2" data-act="refresh"
                title="Re-run the live discovery (templates + appconfig + live indices)">
          <i class="bi bi-arrow-clockwise"></i></button></div>
      <div class="rt-modal-body pp-body" style="flex:1 1 auto;min-height:0;overflow:hidden;
           display:flex;flex-direction:column;">
        <div class="text-center text-secondary py-4">
          <span class="spinner-border spinner-border-sm me-2"></span>
          Discovering possible indices from the cluster…</div>
      </div>
      <div class="rt-modal-actions" style="flex:0 0 auto;">
        <span class="pp-count small text-secondary me-auto"></span>
        ${canFill ? `<button class="btn btn-sm btn-warning" data-act="continue" disabled>
          <i class="bi bi-magic me-1"></i>Create artificial data</button>` : ''}
        <button class="btn btn-sm btn-secondary" data-act="close">Close</button>
      </div></div>`;
  document.body.appendChild(wrap);
  wrap.addEventListener('click', (e) => { if (e.target === wrap) wrap.remove(); });
  wrap.querySelector('[data-act="close"]').onclick = () => wrap.remove();

  let selected = null;

  const load = async (refresh) => {
    const body = wrap.querySelector('.pp-body');
    body.innerHTML = `<div class="text-center text-secondary py-4">
        <span class="spinner-border spinner-border-sm me-2"></span>
        Discovering possible indices from the cluster…</div>`;
    const data = await api('/api/indices/possible' + (refresh ? '?refresh=true' : ''));
    if (!data || data.error) {
      body.innerHTML = `<div class="text-danger p-3">Discovery failed: ${esc(data?.error || 'request failed')}</div>`;
      return;
    }
    wrap.querySelector('.pp-src').textContent = data.meta?.source || 'live catalog';
    wrap.querySelector('.pp-count').textContent =
      `${(data.families || []).length} possible index families · `
      + `${(data.families || []).filter(f => f.live_example).length} with live indices`;
    render(data);
  };

  const render = (data) => {
    const fams = data.families || [];
    const body = wrap.querySelector('.pp-body');
    body.innerHTML = `
      <input type="text" class="form-control form-control-sm pp-search mb-2" style="flex:0 0 auto;"
             placeholder="Filter by name, family, category, doc type…">
      <div class="pp-list border border-secondary rounded" style="flex:1 1 auto;min-height:0;overflow:auto;">
        <table class="table table-sm table-hover mb-0" style="font-size:0.78rem;">
          <thead class="table-dark" style="position:sticky;top:0;z-index:2;"><tr>
            <th>Index family</th><th>Prefix</th><th class="text-center">Slice</th>
            <th class="text-center">Doc types</th><th class="text-center">Fields</th>
            <th class="text-center">Live</th></tr></thead>
          <tbody></tbody></table></div>
      <div class="pp-detail border border-secondary rounded mt-2 p-2 d-none"
           style="flex:0 0 auto;max-height:34vh;overflow:auto;"></div>`;

    const tbody = body.querySelector('tbody');
    const rowsHtml = [];
    let lastCat = null;
    fams.forEach((f, i) => {
      if (f.category !== lastCat) {
        lastCat = f.category;
        rowsHtml.push(`<tr class="pp-cat"><td colspan="6" class="fw-semibold small"
            style="background:rgba(120,130,150,.15);">${esc(f.category)}</td></tr>`);
      }
      const unknown = !f.slice_minutes && /unknown/.test(f.slice_source || '');
      rowsHtml.push(`<tr class="pp-row" data-i="${i}" style="cursor:pointer;"
          data-text="${esc((f.display + ' ' + f.family + ' ' + f.category + ' '
                            + (f.doc_types || []).join(' ')).toLowerCase())}">
        <td>${f.color ? `<span class="me-1" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${esc(f.color)};"></span>` : ''}${esc(f.display)}</td>
        <td class="font-monospace text-secondary">${esc(f.family)}</td>
        <td class="text-center${unknown ? ' text-warning' : ''}" title="${esc(f.slice_source || '')}">
          ${unknown ? '?' : _fmtSliceMin(f.slice_minutes)}</td>
        <td class="text-center">${(f.doc_types || []).length || 1}</td>
        <td class="text-center">${f.field_count}</td>
        <td class="text-center">${f.live_example ? '<span class="badge bg-success" title="' + esc(f.live_example) + '">live</span>' : '<span class="badge bg-secondary">new</span>'}</td>
      </tr>`);
    });
    tbody.innerHTML = rowsHtml.join('');

    body.querySelector('.pp-search').oninput = (e) => {
      const q = e.target.value.trim().toLowerCase();
      let curCat = null;                       // hide category headers with no visible rows
      const cats = [];
      tbody.querySelectorAll('tr').forEach(tr => {
        if (tr.classList.contains('pp-cat')) { curCat = tr; cats.push([tr, 0]); return; }
        const show = !q || tr.dataset.text.includes(q);
        tr.classList.toggle('d-none', !show);
        if (show && cats.length) cats[cats.length - 1][1]++;
      });
      cats.forEach(([tr, n]) => tr.classList.toggle('d-none', n === 0));
    };

    tbody.addEventListener('click', (e) => {
      const tr = e.target.closest('tr.pp-row');
      if (!tr) return;
      tbody.querySelectorAll('tr.pp-row.table-active').forEach(r => r.classList.remove('table-active'));
      tr.classList.add('table-active');
      select(fams[+tr.dataset.i]);
    });
  };

  const select = (f) => {
    selected = f;
    const det = wrap.querySelector('.pp-detail');
    det.classList.remove('d-none');
    const derived = _possibleDerivedType(f);
    const types = (f.doc_types || []).length > 1 ? f.doc_types
                : [derived || (f.doc_types || [])[0] || f.family];
    const defType = derived && types.includes(derived) ? derived : types[0];
    const unknown = !f.slice_minutes && /unknown/.test(f.slice_source || '');
    det.innerHTML = `
      <div class="small mb-1"><b>${esc(f.display)}</b>
        <span class="font-monospace text-secondary">(${esc(f.family)})</span>
        ${f.description ? ` — ${esc(f.description)}` : ''}</div>
      <div class="small text-secondary mb-1">
        Slice: <b>${_fmtSliceMin(f.slice_minutes)}</b> · ${esc(f.slice_source || '')}
        · ${(f.doc_types || []).length > 1
            ? `up to ${f.field_count} template fields (each doc type has its own template — the dialog shows the exact list)`
            : `${f.field_count} template fields`}
        ${f.live_example ? ` · latest live: <span class="font-monospace">${esc(f.live_example)}</span>` : ' · no live index yet'}</div>
      ${unknown ? `<div class="small text-warning mb-1"><i class="bi bi-exclamation-triangle me-1"></i>
          Slice length is UNKNOWN on this machine (no live index, no config record) —
          the slice window will be guessed from the slice number; double-check the name below.</div>` : ''}
      ${f.doc_type_note ? `<div class="small text-warning mb-1"><i class="bi bi-exclamation-triangle me-1"></i>${esc(f.doc_type_note)}</div>` : ''}
      ${canFill ? `
      <div class="d-flex gap-2 align-items-center mb-1 flex-wrap">
        ${types.length > 1 ? `<span class="small fw-semibold">Doc type</span>
          <select class="form-select form-select-sm pp-type" style="width:16rem;">
            ${types.map(t => `<option value="${esc(t)}"${t === defType ? ' selected' : ''}>${esc(t)}</option>`).join('')}
          </select>` : ''}
        <span class="small fw-semibold">Index name</span>
        <input type="text" class="form-control form-control-sm pp-name font-monospace"
               style="min-width:24rem;flex:1 1 auto;" value="${esc(_possibleName(f, defType))}"
               placeholder="${esc(f.index_pattern)}">
      </div>
      <div class="small text-secondary">The name targets the CURRENT time slice; ES applies the
        family's template (mappings) automatically when the index is first written.</div>`
      : `
      <div class="small mb-1"><span class="fw-semibold">Name pattern</span>
        <span class="font-monospace text-secondary ms-1">${esc(f.index_pattern)}</span></div>
      ${types.length > 1 ? `<div class="small mb-1"><span class="fw-semibold">Doc types</span>
        <span class="font-monospace text-secondary ms-1">${types.map(esc).join(', ')}</span></div>` : ''}
      <div class="small text-secondary">This CC writes these itself, from the family's template.
        Listed here so you can tell an index that is missing from one that was never
        expected on this machine.</div>`}`;
    if (!canFill) return;
    const nameInp = det.querySelector('.pp-name');
    const typeSel = det.querySelector('.pp-type');
    if (typeSel) typeSel.onchange = () => { nameInp.value = _possibleName(f, typeSel.value); sync(); };
    const btn = wrap.querySelector('[data-act="continue"]');
    const sync = () => { btn.disabled = !nameInp.value.trim(); };
    nameInp.oninput = sync;
    sync();
  };

  // Optional chaining throughout: in read-only mode the continue button was
  // never rendered, and a picker that threw here would take the catalog with it.
  const contBtn = wrap.querySelector('[data-act="continue"]');
  if (contBtn) contBtn.onclick = () => {
    const name = wrap.querySelector('.pp-name')?.value.trim();
    if (!name || !selected) return;
    wrap.remove();
    createArtificialData(name);
  };
  wrap.querySelector('[data-act="refresh"]').onclick = () => {
    selected = null;
    if (contBtn) contBtn.disabled = true;
    load(true);
  };

  await load(false);
}

/** Delete any index by name (typed confirmation) — used from the detail view
 *  header and from the dashboard table's per-row action. */
async function deleteIndexByName(name) {
  if (!name) return;
  if (!await confirmSharedCc(`delete index "${name}"`)) return;
  const typed = await uiPrompt(document, {
    title: `Delete index — type "${name}" to confirm`,
    value: '', okText: 'Delete',
  });
  if (typed == null) return;
  if (typed.trim() !== name) { showToast('Name did not match — deletion cancelled', 'bg-warning'); return; }
  const res = await api(`/api/indices/${encodeURIComponent(name)}`, { method: 'DELETE' });
  if (!res || res.error) { showToast('Delete failed: ' + (res?.error || 'unknown'), 'bg-danger'); return; }
  showToast(`Index "${name}" deleted`, 'bg-success');
  if (_currentIndexName === name) {
    _currentIndexName = null;
    showView('dashboard');
  }
  await loadIndices();
}

/** Delete the index currently shown in the detail view (typed confirmation). */
async function deleteCurrentIndex() {
  await deleteIndexByName(_currentIndexName);
}

/** Digit sections of an index name: [{start, len, value, context}]. The
 *  context shows the digits with their surrounding name chunk (e.g. "sid-0"). */
function indexNameDigitSections(name) {
  return [...name.matchAll(/\d+/g)].map(m => {
    const start = m.index, len = m[0].length;
    const before = name.slice(0, start).match(/[a-z]+[-_.]*$/i)?.[0] || '';
    return { start, len, value: parseInt(m[0]),
             context: `${before}${m[0]}` };
  });
}

/** Build the target name for copy k: each digit section stepped by step×k
 *  (clamped at 0). If nothing steps, fall back to "<base>-copy<k>". */
function buildDuplicateName(sourceName, sections, steps, k) {
  if (!steps.some(s => s)) return `${sourceName}-copy${k}`;
  let out = '', pos = 0;
  sections.forEach((sec, i) => {
    out += sourceName.slice(pos, sec.start);
    out += String(Math.max(0, sec.value + (steps[i] || 0) * k));
    pos = sec.start + sec.len;
  });
  return out + sourceName.slice(pos);
}

/** Modal collecting duplicate options. Resolves to
 *  {copies, steps[], target, shift_amount, shift_unit, shift_direction} or null. */
function duplicateIndexDialog(sourceName) {
  const sections = indexNameDigitSections(sourceName);
  return new Promise(resolve => {
    const wrap = document.createElement('div');
    wrap.className = 'rt-modal-overlay';
    const sectionRows = sections.map((sec, i) => `
        <div class="d-flex align-items-center gap-2 mb-1">
          <span class="badge bg-secondary font-monospace">${esc(sec.context)}</span>
          <span class="small text-secondary">step per copy</span>
          <input type="number" class="form-control form-control-sm dup-step" data-i="${i}"
                 value="0" style="width:90px;" title="Positive = increase, negative = decrease"/>
        </div>`).join('');
    wrap.innerHTML = `<div class="rt-modal" style="min-width:480px;max-width:640px;">
        <div class="rt-modal-title"><i class="bi bi-copy me-1"></i>Duplicate index "${esc(sourceName)}"</div>
        <div class="rt-modal-body">
          <div class="d-flex align-items-center gap-2 mb-2">
            <label class="small text-secondary mb-0">Number of copies</label>
            <input type="number" class="form-control form-control-sm dup-copies" value="1" min="1" max="100" style="width:90px;"/>
          </div>
          ${sections.length ? `
            <label class="small text-secondary mb-1">Step the name's number sections per copy (0 = keep)</label>
            ${sectionRows}` : ''}
          <div class="dup-single-name">
            <label class="small text-secondary mb-1">New index name (lowercase)</label>
            <input class="form-control form-control-sm mb-2 dup-target" value="${esc(sourceName)}-copy"/>
          </div>
          <div class="dup-preview small text-info mb-2 d-none" style="word-break:break-all;"></div>
          <label class="small text-secondary mb-1">Shift all date fields per copy (0 = exact copy; copy k shifts k × gap)</label>
          <div class="d-flex gap-2 align-items-center">
            <input type="number" class="form-control form-control-sm dup-amount" value="0" min="0" style="width:90px;"/>
            <select class="form-select form-select-sm dup-unit" style="width:110px;">
              <option value="minutes">minutes</option>
              <option value="hours">hours</option>
              <option value="days" selected>days</option>
              <option value="weeks">weeks</option>
              <option value="months">months</option>
            </select>
            <select class="form-select form-select-sm dup-dir" style="width:110px;">
              <option value="past" selected>in the past</option>
              <option value="future">in the future</option>
            </select>
          </div>
          <div class="small text-secondary mt-1">A "month" is a fixed 30 days — every document in a copy shifts by the same offset.</div>
        </div>
        <div class="rt-modal-actions">
          <button class="btn btn-sm btn-info" data-ok="1"><i class="bi bi-copy me-1"></i>Duplicate</button>
          <button class="btn btn-sm btn-outline-secondary" data-ok="0">Cancel</button>
        </div></div>`;
    document.body.appendChild(wrap);

    const readSteps  = () => sections.map((_, i) =>
      parseInt(wrap.querySelector(`.dup-step[data-i="${i}"]`)?.value) || 0);
    const readCopies = () => Math.max(1, Math.min(100,
      parseInt(wrap.querySelector('.dup-copies').value) || 1));

    // Single-name input applies only to the plain 1-copy/no-step case; loop
    // mode generates names — show a live preview of the first/last instead.
    const updatePreview = () => {
      const copies = readCopies(), steps = readSteps();
      const looping = copies > 1 || steps.some(s => s);
      wrap.querySelector('.dup-single-name').classList.toggle('d-none', looping);
      const prev = wrap.querySelector('.dup-preview');
      prev.classList.toggle('d-none', !looping);
      if (looping) {
        const first = buildDuplicateName(sourceName, sections, steps, 1);
        const last  = buildDuplicateName(sourceName, sections, steps, copies);
        prev.innerHTML = copies > 1
          ? `<i class="bi bi-arrow-return-right me-1"></i>${esc(first)} … ${esc(last)} (${copies} copies)`
          : `<i class="bi bi-arrow-return-right me-1"></i>${esc(first)}`;
      }
    };
    wrap.querySelectorAll('.dup-copies, .dup-step').forEach(el =>
      el.addEventListener('input', updatePreview));
    updatePreview();

    const done = (v) => { wrap.remove(); document.removeEventListener('keydown', onKey); resolve(v); };
    const read = () => ({
      copies:          readCopies(),
      steps:           readSteps(),
      target:          wrap.querySelector('.dup-target').value.trim(),
      shift_amount:    Math.max(0, parseInt(wrap.querySelector('.dup-amount').value) || 0),
      shift_unit:      wrap.querySelector('.dup-unit').value,
      shift_direction: wrap.querySelector('.dup-dir').value,
    });
    const onKey = (e) => { if (e.key === 'Escape') done(null); };
    document.addEventListener('keydown', onKey);
    wrap.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b) { done(b.getAttribute('data-ok') === '1' ? read() : null); return; }
      if (e.target === wrap) done(null);
    });
    setTimeout(() => { const inp = wrap.querySelector('.dup-target'); inp?.focus(); inp?.select(); }, 0);
  });
}

/** Duplicate an index N times (with per-copy name stepping + cumulative
 *  date-shift), calling the duplicate endpoint once per copy. */
async function duplicateIndex(name) {
  if (!name) return;
  const opts = await duplicateIndexDialog(name);
  if (!opts) return;
  if (!await confirmSharedCc(`duplicate index "${name}"`)) return;

  const sections = indexNameDigitSections(name);
  const looping  = opts.copies > 1 || opts.steps.some(s => s);
  if (!looping && !opts.target) { showToast('Enter a name for the new index', 'bg-warning'); return; }

  let created = 0, lastTarget = '';
  for (let k = 1; k <= opts.copies; k++) {
    const target = looping ? buildDuplicateName(name, sections, opts.steps, k) : opts.target;
    showToast(`Copying ${k}/${opts.copies}: "${name}" → "${target}"…`, 'bg-secondary');
    const res = await api(`/api/indices/${encodeURIComponent(name)}/duplicate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        target,
        shift_amount:    opts.shift_amount * k,     // cumulative: copy k = k × gap
        shift_unit:      opts.shift_unit,
        shift_direction: opts.shift_direction,
      }),
    });
    if (!res || res.error) {
      showToast(`Copy ${k}/${opts.copies} ("${target}") failed: ${res?.error || 'unknown'}`
        + (created ? ` — ${created} cop${created > 1 ? 'ies' : 'y'} already created` : ''), 'bg-danger');
      break;
    }
    created++;
    lastTarget = res.target;
    if (res.failed) showToast(`"${target}": ${res.failed} doc(s) failed to index`, 'bg-warning');
  }

  if (created) {
    showToast(`Created ${created} cop${created > 1 ? 'ies' : 'y'} of "${name}"`, 'bg-success');
    await loadIndices();
    if (created === 1) showIndexDetail(lastTarget);
  }
}

/** Pick a CSV file and import its rows as documents into the current index. */
function importCsvToIndex() {
  const name = _currentIndexName;
  if (!name) return;
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.accept = '.csv,text/csv';
  inp.onchange = () => { const f = inp.files && inp.files[0]; if (f) doImportCsv(name, f); };
  inp.click();
}

async function doImportCsv(indexName, file) {
  if (!await confirmSharedCc(`import CSV into "${indexName}"`)) return;
  const ok = await uiConfirm(document, {
    title: `Import into "${indexName}"?`,
    message: `Add rows from "${file.name}" (${(file.size / 1024).toFixed(1)} KB) as documents.`,
    okText: 'Import',
  });
  if (!ok) return;
  const idCol = await _chooseIdSource(
    `Import into "${indexName}" — document ids`, await _csvHeader(file));
  if (idCol === null) return;
  showToast(`Importing ${file.name}…`, 'bg-secondary');
  const fd = new FormData();
  fd.append('file', file, file.name);
  let res;
  try {
    res = await api(`/api/indices/${encodeURIComponent(indexName)}/import`
                    + `?id_column=${encodeURIComponent(idCol)}`,
                    { method: 'POST', body: fd });
  } catch (e) {
    showToast('Import failed: ' + e, 'bg-danger'); return;
  }
  if (!res || res.error) { showToast('Import failed: ' + (res?.error || 'unknown'), 'bg-danger'); return; }
  const msg = `Imported ${res.indexed}/${res.rows} row(s)`
    + (res.id_column ? ` (_id from ${res.id_column})` : ' (ES-generated ids)')
    + (res.failed ? ` — ${res.failed} failed` : '');
  showToast(msg, res.failed ? 'bg-warning' : 'bg-success');
  if (res.failed && Array.isArray(res.errors) && res.errors.length) {
    console.warn('CSV import errors (first few):', res.errors);
    // Show them too: a toast saying "N failed" with the reason hidden in the
    // console leaves no way to tell a mapping clash from a bad column.
    showImportErrors(indexName, res);
  }
  refreshCurrentIndex();
}

/** Why an import rejected documents — Elasticsearch's own message per failure.
 *  Reasons are almost always a value that does not fit the target mapping
 *  (a date field given a non-date, a number field given text, …). */
function showImportErrors(indexName, res) {
  document.querySelector('.rt-modal-overlay.rt-import-errors')?.remove();
  const wrap = document.createElement('div');
  wrap.className = 'rt-modal-overlay rt-import-errors';
  const rows = (res.errors || []).map(e =>
    `<li class="mb-1 font-monospace" style="font-size:0.76rem;word-break:break-word;">${esc(e)}</li>`).join('');
  wrap.innerHTML = `<div class="rt-modal" style="min-width:520px;width:760px;max-width:95vw;">
      <div class="rt-modal-title"><i class="bi bi-exclamation-triangle text-warning me-1"></i>
        ${res.failed} of ${res.rows} row(s) rejected by
        <span class="font-monospace">${esc(indexName)}</span></div>
      <div class="rt-modal-body" style="max-height:50vh;overflow:auto;">
        <div class="small text-secondary mb-2">Elasticsearch refused these documents — the
          reason is normally a value that does not match the field's mapping. Showing the
          first ${(res.errors || []).length}:</div>
        <ul class="mb-0 ps-3">${rows}</ul>
      </div>
      <div class="rt-modal-actions">
        <button class="btn btn-sm btn-outline-secondary" data-act="copy">Copy</button>
        <button class="btn btn-sm btn-primary" data-act="close">Close</button>
      </div></div>`;
  document.body.appendChild(wrap);
  const close = () => wrap.remove();
  wrap.addEventListener('click', (e) => {
    if (e.target === wrap) return close();
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.act === 'close') close();
    if (b.dataset.act === 'copy') {
      navigator.clipboard?.writeText((res.errors || []).join('\n'));
      showToast('Errors copied', 'bg-secondary');
    }
  });
}

/* ── Artificial data generator ───────────────────────────────────────────── */
const _AD_UNIT_S = { seconds: 1, minutes: 60, hours: 3600, days: 86400, weeks: 604800 };
let _adJobTimer = null;

function _adUnitOptions(selected) {
  return Object.keys(_AD_UNIT_S).map(u =>
    `<option value="${u}"${u === selected ? ' selected' : ''}>${u}</option>`).join('');
}

/** Field-aware example values for the "values" placeholder — known CC fields
 *  get real examples, otherwise the guess follows the name/type. */
function _adPlaceholder(name, type) {
  const leaf = (name || 'value').split('.').pop();
  const l = leaf.toLowerCase();
  const known = {
    protocol: 'e.g. TCP, UDP',            risk: 'e.g. High, Medium, Low',
    status: 'e.g. Started, Terminated',   direction: 'e.g. In, Out',
    packettype: 'e.g. Regular, Fragmented',
    countrycode: 'e.g. US, DE',           trapversion: 'e.g. V8',
  };
  if (known[l]) return known[l];
  if (/port$/.test(l)) return 'e.g. 80, 443';
  if (/ip$|address$|addr$/.test(l)) return 'e.g. 10.1.2.3, 10.1.2.4';
  if (type === 'boolean') return 'e.g. true, false';
  if (/^(long|integer|short|byte|double|float|half_float|scaled_float)$/.test(type)) return 'e.g. 100, 2500';
  return `e.g. ${leaf}1, ${leaf}2`;
}

/** Random-value kind from name + mapping type — mirror of the backend's
 *  _rand_kind (modules/es/routers/artificial.py); keep the two in sync. */
function _adRandKind(name, type) {
  const l = (name || '').split('.').pop().toLowerCase();
  const isNum = /^(long|integer|short|byte|double|float|half_float|scaled_float)$/.test(type);
  if (/port$/.test(l)) return 'port';
  if (type === 'boolean') return 'bool';
  if (/ip$|address$|addr$/.test(l) && !isNum) return 'ip';
  if (/^(long|integer|short|byte)$/.test(type)) return 'int';
  if (isNum) return 'float';
  return 'token';
}

/** Inner HTML of a field row's value cell for the given mode. */
function _adValueCellHtml(name, type, mode) {
  const leaf = (name || 'value').split('.').pop();
  if (mode === 'random') {
    const kind = _adRandKind(name, type);
    if (kind === 'ip')   return `<span class="small text-secondary">random IPv4, e.g. 84.12.5.77</span>`;
    if (kind === 'bool') return `<span class="small text-secondary">random true / false</span>`;
    if (kind === 'token') return `<span class="small text-secondary">random from</span>
        <span class="font-monospace small">${esc(leaf)}1 … ${esc(leaf)}</span><input type="number" min="1"
        class="form-control form-control-sm ad-rpool" value="10" style="width:4.5rem;"
        title="Pool size — values are picked from ${esc(leaf)}1 … ${esc(leaf)}N">`;
    const [lo, hi] = kind === 'port' ? [1, 65535] : [0, 1000];
    return `<span class="small text-secondary">random ${kind === 'port' ? 'port' : 'number'}</span>
        <input type="number" class="form-control form-control-sm ad-rmin" value="${lo}" style="width:6rem;" title="Minimum">
        <span class="small text-secondary">–</span>
        <input type="number" class="form-control form-control-sm ad-rmax" value="${hi}" style="width:6rem;" title="Maximum">`;
  }
  if (mode === 'increment') {
    return `<input type="text" class="form-control form-control-sm ad-iprefix" placeholder="prefix (opt.)"
        style="width:7rem;" title="Optional text before the number, e.g. '14-' → 14-1, 14-2, …">
      <span class="small text-secondary">start</span>
      <input type="number" class="form-control form-control-sm ad-istart" value="1" style="width:7rem;">
      <span class="small text-secondary">+</span>
      <input type="number" class="form-control form-control-sm ad-istep" value="1" style="width:5rem;"
             title="Step added for every generated document">
      <span class="small text-secondary">per doc</span>`;
  }
  return `<input type="text" class="form-control form-control-sm ad-vals" data-field="${esc(name)}"
                 placeholder="${esc(_adPlaceholder(name, type))}">`;
}

function _adModeSelect() {
  return `<select class="form-select form-select-sm ad-mode" style="width:6.8rem;flex:0 0 auto;"
      title="values: fixed list (cartesian product) · random: fresh random value per document · increment: counter per document">
    <option value="list">values</option>
    <option value="random">random</option>
    <option value="increment">increment</option>
  </select>`;
}

/** ISO week number (1–53) for a Date, read in UTC. */
function _isoWeek(d) {
  const t = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
  const dayNum = (t.getUTCDay() + 6) % 7;             // Mon=0 … Sun=6
  t.setUTCDate(t.getUTCDate() - dayNum + 3);          // nearest Thursday
  const firstThu = new Date(Date.UTC(t.getUTCFullYear(), 0, 4));
  const fdN = (firstThu.getUTCDay() + 6) % 7;
  firstThu.setUTCDate(firstThu.getUTCDate() - fdN + 3);
  return 1 + Math.round((t - firstThu) / (7 * 86400000));
}

/** Preview-only mirror of the backend modules/es/routers/artificial.py::_derive — computes
 *  a derived value from an epoch-millis timestamp (UTC + tz offset minutes).
 *  The authoritative computation is server-side; this drives the live preview. */
function _derivePreview(rule, tsMs, tzMin) {
  if (rule === 'epoch_millis')  return tsMs;
  if (rule === 'epoch_seconds') return Math.floor(tsMs / 1000);
  const d = new Date(tsMs + (tzMin || 0) * 60000);   // shift, then read UTC parts
  const isoWd = ((d.getUTCDay() + 6) % 7) + 1;        // Mon=1 … Sun=7
  switch (rule) {
    case 'weekday_iso':   return isoWd;
    case 'weekday_mon0':  return isoWd - 1;
    case 'weekday_sun0':  return d.getUTCDay();        // Sun=0 … Sat=6
    case 'weekday_sun1':  return d.getUTCDay() + 1;    // Sun=1 … Sat=7
    case 'hour':          return d.getUTCHours();
    case 'minute':        return d.getUTCMinutes();
    case 'second':        return d.getUTCSeconds();
    case 'minute_of_day': return d.getUTCHours() * 60 + d.getUTCMinutes();
    case 'day_of_month':  return d.getUTCDate();
    case 'month':         return d.getUTCMonth() + 1;
    case 'month0':        return d.getUTCMonth();
    case 'quarter':       return Math.floor(d.getUTCMonth() / 3) + 1;
    case 'year':          return d.getUTCFullYear();
    case 'day_of_year': {
      const start = Date.UTC(d.getUTCFullYear(), 0, 0);
      return Math.floor((Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()) - start) / 86400000);
    }
    case 'week_of_year':  return _isoWeek(d);
    default:              return '';
  }
}

/** Slice-aware synthetic-data wizard: granularity (+round), main/other date
 *  fields with gaps, time span, per-field value lists (cartesian product),
 *  existence-skip on insert, and confirmation before spilling into
 *  neighbouring "-sl-N" indices. */
async function createArtificialData(indexName) {
  if (!indexName) return;
  const info = await api('/api/artificial/info/' + encodeURIComponent(indexName));
  if (!info || info.error) {
    showToast('Artificial data: ' + (info?.error || 'request failed'), 'bg-danger');
    return;
  }
  const sl = info.slice;
  const dateFields = info.date_fields || [];
  const mainGuess = info.main_field_guess || dateFields[0] || '';
  const gsec = info.granularity_seconds || 3600;
  const bestUnit = gsec % 604800 === 0 ? 'weeks' : gsec % 86400 === 0 ? 'days'
                 : gsec % 3600 === 0 ? 'hours' : gsec % 60 === 0 ? 'minutes' : 'seconds';
  const fmtIso = (iso) => (iso || '').replace('T', ' ').slice(0, 16) + ' UTC';

  if (_adJobTimer) { clearInterval(_adJobTimer); _adJobTimer = null; }
  document.querySelector('.rt-modal-overlay.rt-artificial')?.remove();
  const wrap = document.createElement('div');
  wrap.className = 'rt-modal-overlay rt-artificial';
  wrap.innerHTML = `<div class="rt-modal" style="min-width:640px;width:820px;max-width:95vw;
        max-height:92vh;min-height:300px;display:flex;flex-direction:column;
        resize:both;overflow:hidden;">
      <div class="rt-modal-title" style="flex:0 0 auto;">
        <i class="bi bi-magic me-1"></i>Create artificial data — <span class="font-monospace">${esc(indexName)}</span></div>
      <div class="rt-modal-body ad-body" style="flex:1 1 auto;min-height:0;overflow:auto;"></div>
      <div class="rt-modal-actions" style="flex:0 0 auto;">
        <span class="ad-estimate small text-info me-auto"></span>
        <button class="btn btn-sm btn-warning" data-act="run"><i class="bi bi-magic me-1"></i>Create data</button>
        <button class="btn btn-sm btn-secondary" data-act="close">Close</button>
      </div></div>`;

  const sliceSrc = sl?.source
    ? (sl.guessed
        ? ` <span class="badge bg-warning text-dark" title="${esc(sl.source)}">slice length guessed</span>`
        : ` <span title="${esc(sl.source)}"><i class="bi bi-patch-check text-success"></i></span>`)
    : '';
  const sliceLine = sl
    ? `Slice <b>${sl.number}</b> (${esc(sl.portion_label)}${sliceSrc}): ${fmtIso(sl.start_iso)} → ${fmtIso(sl.end_iso)}`
    : 'No "-sl-N" suffix detected — all data goes into this index.';
  const newNote = info.exists === false
    ? ` · <span class="text-info">new index — created on first write, template mappings apply</span>`
    : (info.docs_count != null ? ` · ${info.docs_count.toLocaleString()} docs now` : '');

  const body = wrap.querySelector('.ad-body');
  body.innerHTML = `
    <div class="small text-secondary mb-2">${sliceLine}${newNote}</div>

    <div class="d-flex gap-2 align-items-center mb-2 flex-wrap">
      <span class="small fw-semibold" style="min-width:110px;">Granularity</span>
      <input type="number" min="1" class="form-control form-control-sm ad-gran-n" value="${gsec / _AD_UNIT_S[bestUnit]}" style="width:5.5rem;">
      <select class="form-select form-select-sm ad-gran-u" style="width:8rem;">${_adUnitOptions(bestUnit)}</select>
      <label class="small d-flex align-items-center gap-1 ms-2" title="Align timestamps to unit boundaries (1:00, 2:00 / :00, :15 …)">
        <input type="checkbox" class="form-check-input ad-round" checked> round time</label>
    </div>

    <div class="d-flex gap-2 align-items-center mb-1 flex-wrap">
      <span class="small fw-semibold" style="min-width:110px;">Main date field</span>
      ${dateFields.length
        ? `<select class="form-select form-select-sm ad-main" style="width:16rem;">
            ${dateFields.map(f => `<option value="${esc(f)}"${f === mainGuess ? ' selected' : ''}>${esc(f)}</option>`).join('')}
          </select>`
        : `<input type="text" class="form-control form-control-sm ad-main" value="timestamp"
                  style="width:16rem;" title="No date fields in the mapping yet — type the field name">`}
    </div>
    <div class="ad-others ms-4 mb-2"></div>

    <div class="mb-2">
      <span class="small fw-semibold d-block mb-1">Time span</span>
      <div class="ms-3">
        <label class="small d-flex gap-1 align-items-center">
          <input type="radio" name="ad-span" value="slice" ${sl ? 'checked' : 'disabled'}>
          Now → beginning of the slice window${sl ? ` (${fmtIso(sl.start_iso)})` : ' (needs a -sl-N index)'}
        </label>
        <label class="small d-flex gap-1 align-items-center flex-wrap">
          <input type="radio" name="ad-span" value="relative" ${sl ? '' : 'checked'}>
          Now → <input type="number" min="1" class="form-control form-control-sm ad-rel-n" value="1" style="width:4.5rem;">
          <select class="form-select form-select-sm ad-rel-u" style="width:8rem;">${_adUnitOptions('days')}</select> ago
        </label>
        <label class="small d-flex gap-1 align-items-center flex-wrap">
          <input type="radio" name="ad-span" value="absolute"> Absolute (UTC):
          <input type="datetime-local" class="form-control form-control-sm ad-abs-from" style="width:14rem;"> →
          <input type="datetime-local" class="form-control form-control-sm ad-abs-to" style="width:14rem;">
        </label>
      </div>
    </div>

    <div class="d-flex gap-2 align-items-center mb-1">
      <span class="small fw-semibold">Field values</span>
      <input type="text" class="form-control form-control-sm ad-field-filter" placeholder="Filter fields…" style="max-width:180px;">
      <button class="btn btn-sm btn-outline-secondary py-0" data-act="add-field"
              title="Add a field that is not in the mapping yet">+ Add field</button>
      <span class="small text-secondary">values: comma-separated list → one doc per combination per time step · random / increment: filled per document</span>
      <label class="small text-secondary d-flex align-items-center gap-1 ms-auto"
             title="A field left blank is filled with random values chosen from its MAPPING TYPE — ip for an address, a port number, true/false for a boolean, a number in range, or tokens. Unchecked, a blank field is omitted from the documents entirely.">
        <input type="checkbox" class="ad-autofill" checked>
        auto-fill blank fields by type
      </label>
    </div>
    <div class="ad-fields border border-secondary rounded" style="max-height:38vh;overflow:auto;">
      <table class="table table-sm mb-0" style="font-size:0.78rem;">
        <thead class="table-dark"><tr><th style="width:28%;">Field</th><th style="width:10%;">Type</th><th>Values</th></tr></thead>
        <tbody>${(info.fields || []).map(f => `<tr class="ad-frow" data-field="${esc(f.name)}" data-type="${esc(f.type || '')}">
          <td class="font-monospace">${esc(f.name)}</td>
          <td class="text-secondary">${esc(f.type || '')}</td>
          <td><div class="d-flex gap-1 align-items-center">
            ${_adModeSelect()}
            <span class="ad-valcell d-flex gap-1 align-items-center flex-grow-1">${_adValueCellHtml(f.name, f.type || '', 'list')}</span>
          </div></td>
        </tr>`).join('')}</tbody>
      </table>
    </div>

    <div class="d-flex gap-2 align-items-center mt-2 mb-1 flex-wrap">
      <span class="small fw-semibold">Dependency rules</span>
      <button class="btn btn-sm btn-outline-secondary py-0" data-act="add-derived">+ Add rule</button>
      <span class="small text-secondary">a field COMPUTED from a timestamp (e.g. day = weekday, hourOfDay = hour)</span>
      <label class="small d-flex gap-1 align-items-center ms-auto" title="Timezone for day/hour derivation (0 = UTC)">
        TZ offset (min):
        <input type="number" class="form-control form-control-sm ad-tz" value="0" style="width:5.5rem;"></label>
    </div>
    <div class="ad-derived"></div>
    <div class="ad-derived-preview small text-info mb-1"></div>

    <div class="d-flex gap-2 align-items-center mt-2 mb-1 flex-wrap">
      <span class="small fw-semibold">Document _id</span>
      <select class="form-select form-select-sm ad-idmode" style="width:19rem;"
              title="Where each document's _id comes from">
        ${(info.id_modes || [{ id: 'auto', label: 'Auto — Elasticsearch generates the id' }])
          .map(m => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join('')}
      </select>
      <span class="ad-idcell d-flex gap-1 align-items-center flex-grow-1"></span>
    </div>
    <div class="ad-id-preview small text-info mb-1"></div>
    <datalist id="adIdFields"></datalist>
    <datalist id="adDerivedFields">${(info.fields || []).map(f => `<option value="${esc(f.name)}">`).join('')}</datalist>`;
  document.body.appendChild(wrap);

  const deriveRuleOptions = (sel) => (info.derive_rules || []).map(r =>
    `<option value="${esc(r.id)}"${r.id === sel ? ' selected' : ''}>${esc(r.label)}</option>`).join('');

  const addDerivedRow = (pf = {}) => {
    const mainNow = wrap.querySelector('.ad-main').value;
    const srcFields = [...new Set([mainNow, ...dateFields].filter(Boolean))];
    wrap.querySelector('.ad-derived').insertAdjacentHTML('beforeend',
      `<div class="ad-drow d-flex gap-2 align-items-center mb-1 flex-wrap">
        <input type="text" list="adDerivedFields" class="form-control form-control-sm ad-dfield"
               placeholder="field (e.g. day)" style="width:11rem;" value="${esc(pf.field || '')}">
        <span class="small text-secondary">=</span>
        <select class="form-select form-select-sm ad-drule" style="width:15rem;">${deriveRuleOptions(pf.rule)}</select>
        <span class="small text-secondary">from</span>
        <select class="form-select form-select-sm ad-dsource" style="width:11rem;">
          ${srcFields.map(f => `<option value="${esc(f)}"${f === (pf.source || mainNow) ? ' selected' : ''}>${esc(f)}</option>`).join('')}
        </select>
        <button class="btn btn-sm btn-outline-danger py-0 ad-drm" title="Remove rule">✕</button>
      </div>`);
    updateDerivedPreview();
    refreshIdFieldList();
  };

  const refreshDerivedSources = () => {
    const mainNow = wrap.querySelector('.ad-main').value;
    const srcFields = [...new Set([mainNow, ...dateFields].filter(Boolean))];
    wrap.querySelectorAll('.ad-drow .ad-dsource').forEach(sel => {
      const cur = sel.value;
      const keep = srcFields.includes(cur) ? cur : mainNow;
      sel.innerHTML = srcFields.map(f =>
        `<option value="${esc(f)}"${f === keep ? ' selected' : ''}>${esc(f)}</option>`).join('');
    });
  };

  const derivedRules = () => [...wrap.querySelectorAll('.ad-drow')].map(r => ({
    field:  r.querySelector('.ad-dfield').value.trim(),
    rule:   r.querySelector('.ad-drule').value,
    source: r.querySelector('.ad-dsource').value,
  })).filter(d => d.field && d.rule);

  // First planned step's timestamp (ms) — mirrors the backend's rounding so the
  // preview shows the value the first document will actually get.
  const firstStepTs = () => {
    const g = granSeconds();
    if (!g) return null;
    const mode = wrap.querySelector('input[name="ad-span"]:checked')?.value;
    let startS;
    if (mode === 'slice' && sl) startS = sl.start;
    else if (mode === 'relative') startS = Date.now() / 1000 - spanSeconds();
    else { const f = Date.parse(wrap.querySelector('.ad-abs-from').value + ':00Z') / 1000;
           startS = isNaN(f) ? null : f; }
    if (startS == null) return null;
    const t = wrap.querySelector('.ad-round').checked ? Math.ceil(startS / g) * g : startS;
    return Math.round(t * 1000);
  };

  const updateDerivedPreview = () => {
    const rows = [...wrap.querySelectorAll('.ad-drow')];
    const box = wrap.querySelector('.ad-derived-preview');
    if (!rows.length) { box.textContent = ''; return; }
    const tsMs = firstStepTs();
    if (tsMs == null) { box.textContent = '(set a valid time span to preview)'; return; }
    const tz = parseInt(wrap.querySelector('.ad-tz').value) || 0;
    const when = new Date(tsMs).toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
    const parts = rows.map(r => {
      const f = r.querySelector('.ad-dfield').value.trim();
      return f ? `${f} = ${_derivePreview(r.querySelector('.ad-drule').value, tsMs, tz)}` : null;
    }).filter(Boolean);
    box.textContent = parts.length ? `First doc (${when}):  ${parts.join('   ·   ')}` : '';
  };

  const renderOthers = () => {
    const main = wrap.querySelector('.ad-main').value;
    const box = wrap.querySelector('.ad-others');
    const others = dateFields.filter(f => f !== main);
    // Preserve previous inputs when the main field changes.
    const prev = {};
    box.querySelectorAll('.ad-other-row').forEach(r => {
      prev[r.dataset.field] = {
        on: r.querySelector('.ad-other-on').checked,
        n: r.querySelector('.ad-gap-n').value,
        u: r.querySelector('.ad-gap-u').value,
      };
    });
    box.innerHTML = others.map(f => {
      const p = prev[f] || { on: true, n: 0, u: 'seconds' };
      return `<div class="ad-other-row d-flex gap-2 align-items-center mb-1" data-field="${esc(f)}">
        <label class="small d-flex gap-1 align-items-center" style="min-width:14rem;">
          <input type="checkbox" class="form-check-input ad-other-on" ${p.on ? 'checked' : ''}>
          <span class="font-monospace">${esc(f)}</span></label>
        <span class="small text-secondary">= main +</span>
        <input type="number" class="form-control form-control-sm ad-gap-n" value="${p.n}" style="width:5.5rem;">
        <select class="form-select form-select-sm ad-gap-u" style="width:8rem;">${_adUnitOptions(p.u)}</select>
      </div>`;
    }).join('') || '<div class="small text-secondary">no other date fields</div>';
  };
  renderOthers();

  const granSeconds = () =>
    (parseFloat(wrap.querySelector('.ad-gran-n').value) || 0) *
    _AD_UNIT_S[wrap.querySelector('.ad-gran-u').value];

  const spanSeconds = () => {
    const mode = wrap.querySelector('input[name="ad-span"]:checked')?.value;
    if (mode === 'slice' && sl) {
      return Math.max(0, Math.min(Date.now() / 1000, sl.end) - sl.start);
    }
    if (mode === 'relative') {
      return (parseFloat(wrap.querySelector('.ad-rel-n').value) || 0) *
             _AD_UNIT_S[wrap.querySelector('.ad-rel-u').value];
    }
    const f = Date.parse(wrap.querySelector('.ad-abs-from').value + ':00Z');
    const t = Date.parse(wrap.querySelector('.ad-abs-to').value + ':00Z');
    return (isNaN(f) || isNaN(t)) ? 0 : Math.max(0, (t - f) / 1000);
  };

  /** One spec per configured field row: {field, mode:'list', values} |
   *  {field, mode:'random', kind, min, max, pool} |
   *  {field, mode:'increment', prefix, start, step}. */
  const autofillBlanks = () => !!wrap.querySelector('.ad-autofill')?.checked;

  const fieldSpecs = () => [...wrap.querySelectorAll('.ad-frow')].map(r => {
    const field = r.dataset.field || r.querySelector('.ad-fname')?.value.trim() || '';
    if (!field) return null;
    const mode = r.querySelector('.ad-mode')?.value || 'list';
    if (mode === 'list') {
      const values = (r.querySelector('.ad-vals')?.value || '')
        .split(',').map(s => s.trim()).filter(Boolean);
      if (values.length) return { field, mode, values };
      // Blank used to mean "omit this field", which quietly produced documents
      // missing half their mapping — realistic-looking until something queried
      // a field that was never written. Filling it from the field's TYPE gives
      // a document with the shape the template promises. Still opt-out, because
      // omitting a field is occasionally the point of the exercise.
      //
      // Random rather than a constant: a thousand documents sharing one source
      // address is not test data, it is one document repeated.
      if (!autofillBlanks()) return null;
      return { field, mode: 'random', kind: _adRandKind(field, r.dataset.type || ''),
               min: null, max: null, pool: 10, autofilled: true };
    }
    if (mode === 'random') {
      const num = (cls) => { const v = parseFloat(r.querySelector(cls)?.value); return isNaN(v) ? null : v; };
      return { field, mode, kind: _adRandKind(field, r.dataset.type || ''),
               min: num('.ad-rmin'), max: num('.ad-rmax'),
               pool: parseInt(r.querySelector('.ad-rpool')?.value) || 10 };
    }
    return { field, mode,
             prefix: r.querySelector('.ad-iprefix')?.value || '',
             start: parseFloat(r.querySelector('.ad-istart')?.value) || 0,
             step: parseFloat(r.querySelector('.ad-istep')?.value) || 1 };
  }).filter(Boolean);

  // Only list-mode fields multiply the doc count (random/increment fill per doc).
  const valueLists = () => fieldSpecs().filter(s => s.mode === 'list');

  const updateEstimate = () => {
    const g = granSeconds();
    const steps = g > 0 ? Math.floor(spanSeconds() / g) : 0;
    const combos = valueLists().reduce((p, fv) => p * fv.values.length, 1);
    wrap.querySelector('.ad-estimate').textContent =
      `≈ ${steps.toLocaleString()} steps × ${combos.toLocaleString()} combo(s) = ` +
      `${(steps * combos).toLocaleString()} docs`;
  };
  updateEstimate();

  /* ── Document _id ──────────────────────────────────────────────────────────
   * Mirrors modules/es/routers/artificial.py::_build_id_rule / _doc_id: the id can only be
   * built from fields this job actually writes, so the picker and the preview
   * are both driven by the live form state. */

  /** Every field the current form will write into each document. */
  const idFieldNames = () => {
    const names = new Set();
    const main = wrap.querySelector('.ad-main')?.value.trim();
    if (main) names.add(main);
    wrap.querySelectorAll('.ad-other-row').forEach(r => {
      if (r.querySelector('.ad-other-on').checked) names.add(r.dataset.field);
    });
    fieldSpecs().forEach(s => names.add(s.field));      // blank lists are auto-filled or dropped
    derivedRules().forEach(d => names.add(d.field));
    return [...names].filter(Boolean).sort();
  };

  const refreshIdFieldList = () => {
    const dl = wrap.querySelector('#adIdFields');
    if (dl) dl.innerHTML = idFieldNames().map(f => `<option value="${esc(f)}">`).join('');
  };

  const renderIdCell = () => {
    const cell = wrap.querySelector('.ad-idcell');
    const mode = wrap.querySelector('.ad-idmode').value;
    if (mode === 'field') {
      const names = idFieldNames();
      const guess = names.find(f => /^attackIpsId$/i.test(f)) || '';
      cell.innerHTML = `<input type="text" list="adIdFields" class="form-control form-control-sm ad-idfield"
             style="width:15rem;" placeholder="e.g. attackIpsId" value="${esc(guess)}">
        <span class="small text-secondary">the field's value becomes the _id</span>`;
    } else if (mode === 'template') {
      cell.innerHTML = `<input type="text" class="form-control form-control-sm ad-idtpl"
             style="min-width:16rem;flex:1 1 auto;" placeholder="{attackIpsId}">
        <span class="small text-secondary" title="{n} document number · {ts} main timestamp (ms) · {index} target index"
          >{field} placeholders · also {n}, {ts}, {index}</span>`;
    } else {
      cell.innerHTML = '<span class="small text-secondary">Elasticsearch assigns a unique '
        + 'id to every document (duplicates are always added, never overwritten).</span>';
    }
    refreshIdFieldList();
    updateIdPreview();
  };

  /** Field values the FIRST generated document will carry — drives the preview. */
  const firstDocValues = () => {
    const tsMs = firstStepTs();
    const vals = {};
    const main = wrap.querySelector('.ad-main').value.trim();
    if (main && tsMs != null) vals[main] = tsMs;
    wrap.querySelectorAll('.ad-other-row').forEach(r => {
      if (!r.querySelector('.ad-other-on').checked || tsMs == null) return;
      const gap = (parseFloat(r.querySelector('.ad-gap-n').value) || 0) *
                  _AD_UNIT_S[r.querySelector('.ad-gap-u').value];
      vals[r.dataset.field] = tsMs + Math.round(gap * 1000);
    });
    for (const s of fieldSpecs()) {
      if (s.mode === 'list')           vals[s.field] = s.values[0];
      else if (s.mode === 'increment') vals[s.field] = `${s.prefix}${s.start}`;
      else                             vals[s.field] = '‹random›';
    }
    const tz = parseInt(wrap.querySelector('.ad-tz').value) || 0;
    if (tsMs != null) {
      for (const d of derivedRules()) vals[d.field] = _derivePreview(d.rule, tsMs, tz);
    }
    return { vals, tsMs };
  };

  const updateIdPreview = () => {
    const box = wrap.querySelector('.ad-id-preview');
    if (!box) return;
    const mode = wrap.querySelector('.ad-idmode').value;
    if (mode === 'auto') { box.textContent = ''; box.className = 'ad-id-preview small text-info mb-1'; return; }
    const { vals, tsMs } = firstDocValues();
    let out, bad = false;
    if (mode === 'field') {
      const f = wrap.querySelector('.ad-idfield')?.value.trim();
      if (!f) { box.textContent = '(choose the field holding the id)'; return; }
      bad = !(f in vals);
      out = bad ? `"${f}" is not generated by this job — give it a value above` : String(vals[f]);
    } else {
      const tpl = wrap.querySelector('.ad-idtpl')?.value || '';
      if (!/\{[^{}]+\}/.test(tpl)) {
        box.textContent = '(add at least one {field} placeholder — a constant id '
                        + 'would leave a single document)';
        return;
      }
      out = tpl.replace(/\{([^{}]+)\}/g, (_m, k) => {
        k = k.trim();
        if (k === 'n')     return '0';
        if (k === 'ts')    return tsMs == null ? '{ts}' : String(tsMs);
        if (k === 'index') return indexName;
        if (k in vals)     return String(vals[k]);
        bad = true;
        return `⟨${k}?⟩`;
      });
    }
    box.className = 'ad-id-preview small mb-1 ' + (bad ? 'text-warning' : 'text-info');
    box.textContent = (bad ? '⚠ ' : 'First doc _id:  ') + out;
  };

  /** The doc_id payload for POST /api/artificial. */
  const idRule = () => {
    const mode = wrap.querySelector('.ad-idmode').value;
    if (mode === 'field') {
      return { mode, field: wrap.querySelector('.ad-idfield')?.value.trim() || '' };
    }
    if (mode === 'template') {
      return { mode, template: wrap.querySelector('.ad-idtpl')?.value.trim() || '' };
    }
    return { mode: 'auto' };
  };
  renderIdCell();

  // Editing a span sub-input auto-selects ITS radio, so the value the user
  // types actually takes effect. Without this, typing "8 days" in the relative
  // row while the "slice window" radio stayed selected silently clipped the
  // span to the current slice (no spill into neighbouring indices).
  const selectSpan = (mode) => {
    const r = wrap.querySelector(`input[name="ad-span"][value="${mode}"]`);
    if (r && !r.disabled && !r.checked) { r.checked = true; updateEstimate(); }
  };
  wrap.addEventListener('focusin', (e) => {
    const c = e.target.classList;
    if (c?.contains('ad-rel-n') || c?.contains('ad-rel-u')) selectSpan('relative');
    else if (c?.contains('ad-abs-from') || c?.contains('ad-abs-to')) selectSpan('absolute');
  });

  wrap.addEventListener('input', (e) => {
    const c = e.target.classList;
    if (c?.contains('ad-field-filter')) {
      const q = e.target.value.trim().toLowerCase();
      wrap.querySelectorAll('.ad-frow').forEach(r => {
        r.style.display = !q || r.dataset.field.toLowerCase().includes(q) ? '' : 'none';
      });
      return;
    }
    if (c?.contains('ad-fname')) {
      // Custom row in random mode: the kind follows the typed name — re-render
      // the value cell only when the guessed kind actually changes.
      const tr = e.target.closest('tr.ad-frow');
      const mode = tr.querySelector('.ad-mode')?.value;
      const cell = tr.querySelector('.ad-valcell');
      if (mode === 'random' && cell) {
        const kind = _adRandKind(e.target.value.trim(), '');
        if (cell.dataset.kind !== kind) {
          cell.dataset.kind = kind;
          cell.innerHTML = _adValueCellHtml(e.target.value.trim(), '', 'random');
        }
      }
      if (mode === 'list') {
        const inp = cell?.querySelector('.ad-vals');
        if (inp) inp.placeholder = _adPlaceholder(e.target.value.trim(), '');
      }
    }
    if (c?.contains('ad-rel-n') || c?.contains('ad-rel-u')) selectSpan('relative');
    else if (c?.contains('ad-abs-from') || c?.contains('ad-abs-to')) selectSpan('absolute');
    updateEstimate();
    updateDerivedPreview();          // dep-rule preview follows the time span
    refreshIdFieldList();
    updateIdPreview();
  });
  wrap.addEventListener('change', (e) => {
    const c = e.target.classList;
    if (c?.contains('ad-main')) { renderOthers(); refreshDerivedSources(); }
    if (c?.contains('ad-rel-u')) selectSpan('relative');
    if (c?.contains('ad-mode')) {
      // Swap the value cell to the chosen mode's inputs (kind follows the
      // field name + type — custom rows resolve from the typed name).
      const tr = e.target.closest('tr.ad-frow');
      const name = tr.dataset.field || tr.querySelector('.ad-fname')?.value.trim() || '';
      tr.querySelector('.ad-valcell').innerHTML =
        _adValueCellHtml(name, tr.dataset.type || '', e.target.value);
    }
    if (c?.contains('ad-idmode')) { renderIdCell(); return; }   // re-renders + previews
    updateEstimate();
    updateDerivedPreview();
    refreshIdFieldList();
    updateIdPreview();
  });

  const close = () => {
    if (_adJobTimer) { clearInterval(_adJobTimer); _adJobTimer = null; }
    wrap.remove();
  };
  wrap.addEventListener('click', async (e) => {
    if (e.target === wrap) { close(); return; }
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.act === 'close') close();
    else if (b.dataset.act === 'run') submitArtificial(false);
    else if (b.dataset.act === 'add-field') {
      wrap.querySelector('.ad-fields tbody')?.insertAdjacentHTML('beforeend',
        `<tr class="ad-frow" data-field="" data-type="">
          <td><input type="text" class="form-control form-control-sm ad-fname" placeholder="field name"></td>
          <td class="text-secondary">custom</td>
          <td><div class="d-flex gap-1 align-items-center">
            ${_adModeSelect()}
            <span class="ad-valcell d-flex gap-1 align-items-center flex-grow-1">${_adValueCellHtml('', '', 'list')}</span>
          </div></td>
        </tr>`);
    }
    else if (b.dataset.act === 'add-derived') { addDerivedRow(); }
    else if (b.classList.contains('ad-drm')) {
      b.closest('.ad-drow')?.remove();
      updateDerivedPreview(); refreshIdFieldList(); updateIdPreview();
    }
    else if (b.dataset.act === 'cancel-job' && b.dataset.job) {
      await api(`/api/exports/jobs/${encodeURIComponent(b.dataset.job)}/cancel`, { method: 'POST' });
    }
  });

  async function submitArtificial(confirmSpill) {
    const g = granSeconds();
    if (!g || g < 1) { showToast('Granularity must be at least 1 second', 'bg-danger'); return; }
    // Ask once, on the first attempt — a spill re-submit is the same action.
    if (!confirmSpill && !await confirmSharedCc(`write artificial data into "${indexName}"`)) return;
    const mode = wrap.querySelector('input[name="ad-span"]:checked')?.value || 'relative';
    const payload = {
      index: indexName,
      main_field: wrap.querySelector('.ad-main').value,
      granularity_seconds: g,
      round_time: wrap.querySelector('.ad-round').checked,
      other_dates: [...wrap.querySelectorAll('.ad-other-row')]
        .filter(r => r.querySelector('.ad-other-on').checked)
        .map(r => ({ field: r.dataset.field,
                     gap_seconds: (parseFloat(r.querySelector('.ad-gap-n').value) || 0) *
                                  _AD_UNIT_S[r.querySelector('.ad-gap-u').value] })),
      span_mode: mode,
      span_seconds: mode === 'relative'
        ? (parseFloat(wrap.querySelector('.ad-rel-n').value) || 0) *
          _AD_UNIT_S[wrap.querySelector('.ad-rel-u').value]
        : 0,
      span_from: wrap.querySelector('.ad-abs-from').value,
      span_to: wrap.querySelector('.ad-abs-to').value,
      fields: fieldSpecs(),
      derived: derivedRules(),
      doc_id: idRule(),
      tz_offset_minutes: parseInt(wrap.querySelector('.ad-tz').value) || 0,
      confirm_spill: confirmSpill,
    };
    const res = await api('/api/artificial', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res || res.error) { showToast('Artificial data: ' + (res?.error || 'failed'), 'bg-danger'); return; }

    if (res.needs_confirm) {
      const rows = res.targets.map(t =>
        `<tr><td class="font-monospace">${esc(t.index)}</td>
             <td class="text-end">${t.docs.toLocaleString()}</td>
             <td>${t.exists ? '<span class="badge bg-success">exists</span>'
                            : '<span class="badge bg-warning text-dark">will be CREATED</span>'}</td></tr>`).join('');
      body.insertAdjacentHTML('afterbegin', `<div class="ad-confirm border border-warning rounded p-2 mb-2">
          <div class="small fw-semibold text-warning mb-1">
            <i class="bi bi-exclamation-triangle me-1"></i>The time span extends beyond this index's slice window</div>
          <table class="table table-sm mb-2" style="font-size:0.78rem;">
            <thead><tr><th>Target index</th><th class="text-end">Docs</th><th></th></tr></thead>
            <tbody>${rows}</tbody></table>
          <button class="btn btn-sm btn-warning me-1" data-act="confirm-spill">Insert into all listed indices</button>
          <button class="btn btn-sm btn-outline-secondary" data-act="drop-confirm">Back</button>
        </div>`);
      const conf = body.querySelector('.ad-confirm');
      conf.querySelector('[data-act="confirm-spill"]').onclick = () => { conf.remove(); submitArtificial(true); };
      conf.querySelector('[data-act="drop-confirm"]').onclick = () => conf.remove();
      return;
    }

    // ── Job started — switch to progress view ────────────────────────────────
    const jobId = res.job_id;
    body.innerHTML = `<div class="small mb-2">Job <span class="font-monospace">${esc(jobId)}</span> —
        ${res.planned.toLocaleString()} document(s) planned.</div>
      <div class="ad-progress"></div>`;
    wrap.querySelector('[data-act="run"]').outerHTML =
      `<button class="btn btn-sm btn-outline-warning" data-act="cancel-job" data-job="${esc(jobId)}">Cancel job</button>`;
    const prog = body.querySelector('.ad-progress');
    const poll = async () => {
      const j = await api(`/api/exports/jobs/${encodeURIComponent(jobId)}`);
      if (!j || j.error) return;
      prog.innerHTML = j.items.map(it => {
        const pct = it.total ? Math.min(100, Math.round(it.done / it.total * 100)) : 0;
        return `<div class="border border-secondary rounded p-2 mb-1 small">
          <div class="d-flex gap-2 align-items-center">
            <span class="font-monospace">${esc(it.index)}</span>
            <span class="badge bg-dark border">${esc(it.phase || '')}</span>
            <span class="ms-auto">${it.inserted.toLocaleString()} inserted ·
              ${it.skipped.toLocaleString()} skipped${it.failed ? ` · <span class="text-danger">${it.failed} failed</span>` : ''}
              · ${it.done.toLocaleString()}/${(it.total ?? 0).toLocaleString()}</span></div>
          <div class="progress mt-1" style="height:5px;">
            <div class="progress-bar ${j.status === 'error' ? 'bg-danger' : 'bg-warning'}" style="width:${pct}%"></div>
          </div></div>`;
      }).join('');
      if (j.status !== 'running') {
        clearInterval(_adJobTimer); _adJobTimer = null;
        const badge = j.status === 'done' ? 'bg-success' : j.status === 'cancelled' ? 'bg-secondary' : 'bg-danger';
        prog.insertAdjacentHTML('beforeend',
          `<div class="mt-2"><span class="badge ${badge}">${esc(j.status)}</span>
             ${j.error ? `<span class="small text-danger ms-2">${esc(j.error)}</span>` : ''}</div>`);
        wrap.querySelector('[data-act="cancel-job"]')?.remove();
        if (typeof refreshCurrentIndex === 'function' && _currentIndexName === indexName) refreshCurrentIndex();
      }
    };
    _adJobTimer = setInterval(poll, 1000);
    poll();
  }
}

async function showIndexDetail(indexName, preserveFilters = false) {
  _currentIndexName = indexName;
  showView('index');
  activateViewer('index');
  loadExactFieldMap([indexName]);      // resolve analyzed fields for filtering
  document.getElementById('indexDetailTitle').textContent = indexName;
  document.getElementById('indexStatCards').innerHTML = '<div class="text-secondary small">Loading stats…</div>';
  setQueryResults('Loading…');

  const size = parseInt(document.getElementById('sampleSizeSelect')?.value || '10');

  const [stats, sample] = await Promise.all([
    api(`/api/indices/${encodeURIComponent(indexName)}/stats`),
    api(`/api/indices/${encodeURIComponent(indexName)}/sample?size=${size}`),
  ]);

  const meta    = stats.cc_meta || {};
  const storeMB = stats.store_bytes ? (stats.store_bytes / 1e6).toFixed(1) + ' MB' : '—';
  const shownCount = sample.hits?.length ?? 0;
  const totalCount = sample.total ?? stats.docs_count ?? 0;
  // Remember the full index size + sort field so column filters can decide
  // whether to filter the loaded rows locally or re-query Elasticsearch.
  _indexFullTotal = stats.docs_count ?? sample.total ?? 0;
  _indexSortField = sample.sort_field || null;
  // Remember which fields are declared in the mapping so the field-visibility
  // picker can tell mapped vs. unmapped columns apart.
  mappedFieldNames = new Set(Array.isArray(stats.mapping_field_names) ? stats.mapping_field_names : []);
  mappedDateFields = new Set(Array.isArray(stats.mapping_date_fields) ? stats.mapping_date_fields : []);
  document.getElementById('indexStatCards').innerHTML = `
    <div class="col-auto"><div class="stat-card"><div class="stat-value">${(stats.docs_count ?? 0).toLocaleString()}</div><div class="stat-label">Documents</div></div></div>
    <div class="col-auto"><div class="stat-card"><div class="stat-value text-warning">${(stats.docs_deleted ?? 0).toLocaleString()}</div><div class="stat-label">Deleted Docs</div></div></div>
    <div class="col-auto"><div class="stat-card"><div class="stat-value text-info">${storeMB}</div><div class="stat-label">Store Size</div></div></div>
    <div class="col-auto"><div class="stat-card stat-card-action" onclick="openFieldVisibility()" title="Choose which fields to show/hide"><div class="stat-value">${stats.mapping_fields ?? '—'} <i class="bi bi-eye stat-card-icon"></i></div><div class="stat-label">Mapped Fields</div></div></div>
    <div class="col-auto"><div class="stat-card"><div class="stat-value text-success" id="showingTotalStat">${shownCount.toLocaleString()} / ${totalCount.toLocaleString()}</div><div class="stat-label">Showing / Total</div></div></div>
    ${meta.description ? `<div class="col-12"><div class="alert alert-info py-2 small mb-0"><strong>${esc(meta.display || indexName)}</strong> — ${esc(meta.description)}</div></div>` : ''}`;

  const metaEl = document.getElementById('idxMeta');
  if (sample.error) {
    lastResultHits = []; lastResultTotal = 0; lastResultJson = '✗ ' + sample.error;
    selectedRows.clear(); selectedCols.clear();
    if (metaEl) metaEl.textContent = '';
    renderResultViews();
    return;
  }
  if (metaEl) {
    metaEl.textContent = `${shownCount.toLocaleString()} shown of ${totalCount.toLocaleString()}`
      + (sample.sort_field ? ` · sorted by ${sample.sort_field} desc` : '');
  }

  // Capture results but restore preserved filters if requested
  if (preserveFilters) {
    // Temporarily store the filters to restore after captureResults resets them
    const filtersToRestore = _preservedIndexFilters;
    const hiddenToRestore  = _preservedHiddenColumns;
    captureResults(sample);
    // Restore preserved filters (stored as arrays of values)
    tableFilters = {};
    for (const [col, values] of Object.entries(filtersToRestore)) {
      if (Array.isArray(values)) {
        tableFilters[col] = new Set(values);
      } else if (values && values.size !== undefined) {
        tableFilters[col] = new Set(values);
      } else if (values && typeof values === 'object') {
        tableFilters[col] = new Set(Object.values(values));
      }
    }
    // Restore preserved column visibility (only for columns still present)
    const present = new Set(currentColumns());
    hiddenColumns = new Set((hiddenToRestore || []).filter(c => present.has(c)));
    _preservedIndexFilters = {};
    _preservedHiddenColumns = [];
    // If filters are active but the sample is only a slice of the index, the
    // loaded rows can't represent all matches — re-query Elasticsearch instead.
    if (Object.keys(tableFilters).length && lastResultHits.length < _indexFullTotal) {
      await applyIndexFiltersServerSide();
    } else {
      renderResultViews();
      updateFilterButtonState();
      updateShowingCount();
    }
  } else {
    captureResults(sample);
    updateFilterButtonState();
  }
}

/* ── Attack summary charts ───────────────────────────────────────────────── */
async function loadAttackSummary() {
  // The dashboard attack charts were removed; skip if the canvases are absent.
  if (!document.getElementById('chartAttackCategory') &&
      !document.getElementById('chartAttackTimeline')) return;
  try {
    const data = await api('/api/cc/attacks/summary');
    if (data.error) return;
    renderCategoryChart(data.by_category || []);
    renderTimelineChart(data.attacks_over_time || []);
  } catch (_) {}
}

function renderCategoryChart(buckets) {
  const el = document.getElementById('chartAttackCategory');
  if (!el) return;
  const ctx = el.getContext('2d');
  if (chartCategory) chartCategory.destroy();
  if (!buckets.length) return;
  chartCategory = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: buckets.map(b => b.key || 'Unknown'),
      datasets: [{ data: buckets.map(b => b.count),
        backgroundColor: ['#e74c3c','#e67e22','#f1c40f','#2ecc71','#3498db','#9b59b6','#1abc9c','#e91e63','#607d8b'] }],
    },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'right', labels: { font: { size: 11 } } } } },
  });
}

function renderTimelineChart(buckets) {
  const el = document.getElementById('chartAttackTimeline');
  if (!el) return;
  const ctx = el.getContext('2d');
  if (chartTimeline) chartTimeline.destroy();
  if (!buckets.length) return;
  chartTimeline = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: buckets.map(b => _dayStr(b.date)),
      datasets: [{ label: 'Attacks', data: buckets.map(b => b.count),
        backgroundColor: 'rgba(220,53,69,.6)', borderColor: '#dc3545', borderWidth: 1 }],
    },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { x: { ticks: { maxTicksLimit: 10 } } } },
  });
}

/* ── Attacks view ────────────────────────────────────────────────────────── */
// The table shows only GENERIC fields common to every attack type (ID, type,
// start/end, device IP, status); clicking a row opens the full drill-down
// across all "dp-" / "attack-data" indices for that attack ID.
/* ── Attacks view: loaded set + per-column filters ─────────────────────────── */
let _attacksAll = [];                    // the loaded attacks (pre-filter)
const ATTACK_COLS = {                    // column key on the attack object → meta
  attackIpsId: { label:'Attack ID',  kind:'text' },
  attackType:  { label:'Type',       kind:'text' },
  startTime:   { label:'Start Time', kind:'date' },
  endTime:     { label:'End Time',   kind:'date' },
  deviceIp:    { label:'Device IP',  kind:'text' },
  status:      { label:'Status',     kind:'text' },
};
// Operators offered per column kind. "contains" stays the default because it
// is what the screen did before and what most searches want; the rest exist
// because "contains" cannot express the two questions people actually asked —
// exclude a noisy device, and bound a window.
const ATTACK_TEXT_OPS = [
  ['contains',  'contains'],
  ['ncontains', 'does not contain'],
  ['eq',        'equals'],
  ['neq',       'does not equal'],
  ['starts',    'starts with'],
  ['ends',      'ends with'],
  ['empty',     'is empty'],
];

const ATTACK_DATE_OPS = [
  ['gte',     'after or equal ( ≥ )'],
  ['gt',      'after ( > )'],
  ['lte',     'before or equal ( ≤ )'],
  ['lt',      'before ( < )'],
  ['eq',      'exact ( = the minute )'],
  ['between', 'between ( from → to )'],
];

let attackColFilters = {};               // col → {kind:'text',op,v} | {kind:'date',op,v,v2}
let _attacksPage = 0;                    // 0-based page index into the FILTERED rows
let _attacksPageSize = 100;              // 0 = show everything on one page
let _attacksMatchedTotal = 0;            // total matching server-side (may exceed loaded)

/** Build the /cc/attacks query string from the active column filters, so the
 *  backend can find matches beyond the loaded page. */
function _attackQueryString() {
  const f = attackColFilters, p = new URLSearchParams();
  const hasF = Object.keys(f).length > 0;
  p.set('size', hasF ? '1000' : '500');
  // The server-side filters NARROW what is fetched; the client then applies the
  // full operator set to what came back. A narrowing filter is only safe when
  // every row the user wants is inside it, so a POSITIVE operator may be sent
  // (equals / starts / ends are all subsets of "contains") and a NEGATIVE one
  // may not: asking the server for rows containing "x" and then showing the
  // ones that do NOT contain "x" would return nothing, and the screen would
  // report "no matches" for a filter that should have matched most of the box.
  const SENDABLE_TEXT_OPS = new Set(['contains', 'eq', 'starts', 'ends']);
  const sendText = (key, spec) => {
    if (!spec?.v?.trim()) return;
    if (!SENDABLE_TEXT_OPS.has(spec.op || 'contains')) return;
    p.set(key, spec.v.trim());
  };
  sendText('attack_id', f.attackIpsId);
  sendText('type',      f.attackType);
  sendText('device_ip', f.deviceIp);
  sendText('status',    f.status);

  for (const [col, key] of [['startTime','start'], ['endTime','end']]) {
    const df = f[col];
    if (!df?.v) continue;
    // "between" has no server-side operator, so send its LOWER bound as >=:
    // a superset of the range, which the client then trims to the range.
    let op = df.op, val = df.v;
    if (op === 'between') {
      const a = new Date(df.v).getTime(), b = new Date(df.v2 || '').getTime();
      if (isNaN(a) || isNaN(b)) continue;
      op = 'gte';
      val = a <= b ? df.v : df.v2;
    }
    const ms = new Date(val).getTime();
    if (!isNaN(ms)) { p.set(key + '_op', op); p.set(key + '_val', String(ms)); }
  }
  return p.toString();
}

async function loadAttacks() {
  const tbody = document.getElementById('attacksTableBody');
  const meta  = document.getElementById('attacksMeta');
  if (!tbody) return;
  if (meta) meta.innerHTML = '<span class="spinner-border spinner-border-sm me-1" style="width:.8rem;height:.8rem;"></span> Loading…';

  const data = await api('/api/cc/attacks?' + _attackQueryString());
  if (data.error) {
    tbody.innerHTML = `<tr><td colspan="6" class="text-center text-danger">${esc(data.error)}</td></tr>`;
    if (meta) meta.textContent = '';
    return;
  }
  _attacksAll = data.attacks || [];
  _attacksMatchedTotal = data.matched_total ?? _attacksAll.length;

  const hasF = Object.keys(attackColFilters).length > 0;
  const hdr  = document.querySelector('#view-attacks h5');
  if (hdr) {
    hdr.innerHTML =
      `<i class="bi bi-shield-exclamation me-2 text-danger"></i>Recent Attacks` +
      ` <span class="badge bg-danger ms-2">${_attacksAll.length.toLocaleString()} shown</span>` +
      ` <span class="badge bg-secondary ms-1">${(_attacksMatchedTotal || 0).toLocaleString()} ${hasF ? 'match' : 'attacks total'}</span>`;
  }
  renderAttacks();
}

/** Apply the current filters: instant client refine of the loaded set, then a
 *  server fetch that brings in matches beyond the loaded page. */
function applyAttackFilters() {
  _attacksPage = 0;
  renderAttacks();     // instant feedback on what's already loaded
  loadAttacks();       // authoritative: fetch all matches from the backend
}

/** Epoch-ms for an attack date value (number, numeric string, or ISO / local
 *  datetime-local string). Returns null when unparseable. */
function _attackMs(v) {
  if (v == null || v === '') return null;
  if (typeof v === 'number') return v < 1e12 ? v * 1000 : v;
  const s = String(v).trim();
  if (/^\d{10,}$/.test(s)) { const n = Number(s); return n < 1e12 ? n * 1000 : n; }
  const t = Date.parse(s);               // ISO (UTC) or datetime-local (local) → absolute ms
  return isNaN(t) ? null : t;
}

/** True if an attack passes every active column filter. */
function _attackMatches(a) {
  for (const [col, f] of Object.entries(attackColFilters)) {
    if (!f) continue;
    if (f.kind === 'text') {
      const hay = String(a[col] ?? '').toLowerCase();
      const needle = (f.v || '').trim().toLowerCase();
      const op = f.op || 'contains';
      if (op === 'empty') { if (hay !== '') return false; continue; }
      if (!needle) continue;
      switch (op) {
        case 'contains':  if (!hay.includes(needle)) return false; break;
        case 'ncontains': if (hay.includes(needle)) return false; break;
        case 'eq':        if (hay !== needle) return false; break;
        case 'neq':       if (hay === needle) return false; break;
        case 'starts':    if (!hay.startsWith(needle)) return false; break;
        case 'ends':      if (!hay.endsWith(needle)) return false; break;
      }
    } else {                              // date operators
      if (!f.v) continue;
      const am = _attackMs(a[col]);
      const im = _attackMs(f.v);
      if (am == null || im == null) { if (im != null) return false; continue; }
      if (f.op === 'between') {
        const im2 = _attackMs(f.v2);
        if (im2 == null) continue;                 // half-typed range filters nothing
        // Inclusive at both ends: the user picked two wall-clock times off the
        // table and expects the rows they can see at those times to be in.
        const [lo, hi] = im <= im2 ? [im, im2] : [im2, im];
        if (am < lo || am > hi) return false;
        continue;
      }
      switch (f.op) {
        case 'gt':  if (!(am >  im)) return false; break;
        case 'gte': if (!(am >= im)) return false; break;
        case 'lt':  if (!(am <  im)) return false; break;
        case 'lte': if (!(am <= im)) return false; break;
        case 'eq':  if (Math.floor(am/60000) !== Math.floor(im/60000)) return false; break;  // same minute
      }
    }
  }
  return true;
}

/** Render the attacks table from _attacksAll through the active filters. */
function renderAttacks() {
  const tbody = document.getElementById('attacksTableBody');
  if (!tbody) return;
  const shown = _attacksAll.filter(_attackMatches);

  // reflect active state on each column funnel
  Object.keys(ATTACK_COLS).forEach(col => {
    document.getElementById('atkf-' + col)?.classList.toggle('active', !!attackColFilters[col]);
  });

  // meta line: filtered count (+ server-side match total when it exceeds the page) + clear-all
  const nF = Object.keys(attackColFilters).length;
  const meta = document.getElementById('attacksMeta');
  if (meta) {
    let m = `Showing <b>${shown.length.toLocaleString()}</b> of ${_attacksAll.length.toLocaleString()} loaded`;
    if (nF) {
      if (_attacksMatchedTotal > _attacksAll.length)
        m += ` · <b>${_attacksMatchedTotal.toLocaleString()}</b> match server-side (first ${_attacksAll.length.toLocaleString()} loaded)`;
      else
        m += ` · ${_attacksMatchedTotal.toLocaleString()} match`;
      m += ` · <a href="#" onclick="clearAllAttackFilters();return false;">Clear ${nF} filter${nF>1?'s':''}</a>`;
    }
    meta.innerHTML = m;
  }

  if (!_attacksAll.length) { tbody.innerHTML = '<tr><td colspan="6" class="text-center text-secondary py-3">No attacks found</td></tr>'; _renderAttackPager(0, 0); return; }
  if (!shown.length)       { tbody.innerHTML = '<tr><td colspan="6" class="text-center text-secondary py-3">No attacks match the filters</td></tr>'; _renderAttackPager(0, 0); return; }

  // Page the FILTERED rows, not the loaded ones: the count under the pager has
  // to agree with the count in the meta line above it, or the two read as a
  // contradiction. Clamped rather than reset, so narrowing a filter while on
  // page 9 lands on the last page instead of silently jumping to the first.
  const pageSize = _attacksPageSize > 0 ? _attacksPageSize : shown.length;
  const pageCount = Math.max(1, Math.ceil(shown.length / pageSize));
  if (_attacksPage >= pageCount) _attacksPage = pageCount - 1;
  if (_attacksPage < 0) _attacksPage = 0;
  const from = _attacksPage * pageSize;
  const pageRows = shown.slice(from, from + pageSize);
  _renderAttackPager(shown.length, pageCount, from, pageRows.length);

  tbody.innerHTML = pageRows.map(a => {
    const typeBadge = a.attackType
      ? `<span class="badge bg-info text-dark">${esc(a.attackType)}</span>`
      : '—';
    const stBadge = a.status
      ? `<span class="badge ${a.status === 'Ongoing' ? 'bg-danger'
                            : a.status === 'Terminated' ? 'bg-secondary'
                            : 'bg-warning text-dark'}">${esc(a.status)}</span>`
      : '<span class="text-secondary">—</span>';
    return `<tr style="cursor:pointer;" onclick="showAttackDetails('${jsq(a.attackIpsId || '')}')"
                title="Click for all data recorded for this attack">
      <td class="font-monospace small">${esc(a.attackIpsId || '—')}</td>
      <td>${typeBadge}</td>
      <td class="text-nowrap">${fmtTime(a.startTime)}</td>
      <td class="text-nowrap">${fmtTime(a.endTime)}</td>
      <td>${esc(a.deviceIp || '—')}</td>
      <td>${stBadge}</td>
    </tr>`;
  }).join('');
}

/** Open (or toggle) the filter popover for an attacks-table column. */
function toggleAttackFilter(col, btn) {
  const open = document.querySelector('.atk-filter-pop');
  const same = open && open.dataset.col === col;
  open?.remove();
  if (same) return;                       // second click on same funnel → close

  const meta = ATTACK_COLS[col]; if (!meta) return;
  const cur  = attackColFilters[col] || {};
  const pop  = document.createElement('div');
  pop.className = 'atk-filter-pop';
  pop.dataset.col = col;

  if (meta.kind === 'text') {
    const op = cur.op || 'contains';
    pop.innerHTML =
      `<div class="afp-title">Filter ${esc(meta.label)}</div>
       <select class="form-select form-select-sm afp-op">${ATTACK_TEXT_OPS.map(
         o => `<option value="${o[0]}"${o[0] === op ? ' selected' : ''}>${esc(o[1])}</option>`).join('')}</select>
       <input class="form-control form-control-sm afp-text mt-1" placeholder="value…" value="${esc(cur.v || '')}">
       <div class="afp-actions">
         <button class="btn btn-sm btn-primary" data-a="apply">Apply</button>
         <button class="btn btn-sm btn-outline-secondary" data-a="clear">Clear</button>
       </div>`;
  } else {
    const op = cur.op || 'gte';
    const between = op === 'between';
    pop.innerHTML =
      `<div class="afp-title">Filter ${esc(meta.label)}</div>
       <select class="form-select form-select-sm afp-op">${ATTACK_DATE_OPS.map(
         o => `<option value="${o[0]}"${o[0] === op ? ' selected' : ''}>${esc(o[1])}</option>`).join('')}</select>
       <input type="datetime-local" step="1" class="form-control form-control-sm afp-date mt-1"
              value="${esc(cur.v || '')}">
       <input type="datetime-local" step="1"
              class="form-control form-control-sm afp-date2 mt-1${between ? '' : ' d-none'}"
              value="${esc(cur.v2 || '')}" placeholder="to">
       <div class="afp-hint">Times match your local timezone (as shown in the table).</div>
       <div class="afp-actions">
         <button class="btn btn-sm btn-primary" data-a="apply">Apply</button>
         <button class="btn btn-sm btn-outline-secondary" data-a="clear">Clear</button>
       </div>`;
  }
  document.body.appendChild(pop);
  const r = btn.getBoundingClientRect();
  pop.style.top  = (window.scrollY + r.bottom + 5) + 'px';
  pop.style.left = (window.scrollX + Math.max(8, Math.min(r.left, window.innerWidth - 258))) + 'px';

  pop.addEventListener('click', (e) => {
    const act = e.target.closest('button')?.dataset.a;
    if (!act) return;
    if (act === 'clear') {
      delete attackColFilters[col];
    } else if (meta.kind === 'text') {
      const v = pop.querySelector('.afp-text').value;
      const op = pop.querySelector('.afp-op').value;
      // "is empty" is the one operator with nothing to type.
      if (v.trim() || op === 'empty') attackColFilters[col] = { kind:'text', op, v };
      else delete attackColFilters[col];
    } else {
      const v  = pop.querySelector('.afp-date').value;
      const v2 = pop.querySelector('.afp-date2')?.value || '';
      const op = pop.querySelector('.afp-op').value;
      if (v && (op !== 'between' || v2)) attackColFilters[col] = { kind:'date', op, v, v2 };
      else if (!v) delete attackColFilters[col];
      else showToast('Between needs both a from and a to time', 'bg-warning');
    }
    pop.remove();
    applyAttackFilters();
  });
  pop.querySelector('.afp-text')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') pop.querySelector('[data-a="apply"]').click();
  });
  pop.querySelector('.afp-op')?.addEventListener('change', (e) => {
    // The second date box only exists for "between"; the text box is pointless
    // for "is empty". Toggling rather than rebuilding keeps what was typed.
    pop.querySelector('.afp-date2')?.classList.toggle('d-none', e.target.value !== 'between');
    pop.querySelector('.afp-text')?.classList.toggle('d-none', e.target.value === 'empty');
  });
  setTimeout(() => pop.querySelector('input')?.focus(), 0);
}

/** Draw the pager under the attacks table. Hidden when everything fits. */
function _renderAttackPager(total, pageCount, from = 0, count = 0) {
  const el = document.getElementById('attacksPager');
  if (!el) return;
  if (!total) { el.innerHTML = ''; el.classList.add('d-none'); return; }
  el.classList.remove('d-none');
  const page = _attacksPage;
  const sizes = [50, 100, 250, 500, 0];
  el.innerHTML = `
    <div class="d-flex align-items-center gap-2 flex-wrap py-1">
      <span class="small text-secondary">
        ${total ? `${(from + 1).toLocaleString()}–${(from + count).toLocaleString()}` : '0'}
        of ${total.toLocaleString()}</span>
      <div class="btn-group btn-group-sm ms-2">
        <button class="btn btn-outline-secondary py-0 px-2" ${page === 0 ? 'disabled' : ''}
                onclick="goAttackPage(0)" title="First page">&laquo;</button>
        <button class="btn btn-outline-secondary py-0 px-2" ${page === 0 ? 'disabled' : ''}
                onclick="goAttackPage(${page - 1})" title="Previous page">&lsaquo;</button>
        <button class="btn btn-outline-secondary py-0 px-2 disabled">
          ${page + 1} / ${pageCount}</button>
        <button class="btn btn-outline-secondary py-0 px-2" ${page >= pageCount - 1 ? 'disabled' : ''}
                onclick="goAttackPage(${page + 1})" title="Next page">&rsaquo;</button>
        <button class="btn btn-outline-secondary py-0 px-2" ${page >= pageCount - 1 ? 'disabled' : ''}
                onclick="goAttackPage(${pageCount - 1})" title="Last page">&raquo;</button>
      </div>
      <label class="small text-secondary d-flex align-items-center gap-1 ms-2">
        rows
        <select class="form-select form-select-sm py-0" style="width:5.5rem;"
                onchange="setAttackPageSize(this.value)">
          ${sizes.map(n => `<option value="${n}"${n === _attacksPageSize ? ' selected' : ''}>${n || 'all'}</option>`).join('')}
        </select>
      </label>
    </div>`;
}

function goAttackPage(n) {
  _attacksPage = n;
  renderAttacks();
  document.getElementById('attacksTableBody')?.scrollIntoView({ block: 'start' });
}

function setAttackPageSize(v) {
  _attacksPageSize = parseInt(v, 10) || 0;
  _attacksPage = 0;                       // a new page size makes the old index meaningless
  renderAttacks();
}

/** Any change to the filters puts the user back on page one — staying on
 *  page 7 of a result set they just redefined shows them rows they did not
 *  ask for and an empty table as often as not. */
function clearAllAttackFilters() { attackColFilters = {}; _attacksPage = 0; applyAttackFilters(); }

// Close an open attacks filter popover when clicking elsewhere.
document.addEventListener('click', (e) => {
  if (e.target.closest('.atk-filter-pop') || e.target.closest('.atk-funnel')) return;
  document.querySelector('.atk-filter-pop')?.remove();
});

/** Copy text to the clipboard and flash the button that asked for it.
 *  navigator.clipboard is unavailable on a plain-HTTP origin in some browsers,
 *  and this tool is regularly reached over http on a lab network, so the
 *  execCommand path is a real fallback rather than legacy politeness. */
async function copyTextToClipboard(text, btn, okLabel = 'Copied') {
  let ok = true;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand('copy');
      ta.remove();
    } catch { ok = false; }
  }
  if (btn) {
    const orig = btn.innerHTML;
    btn.innerHTML = ok ? `<i class="bi bi-check2 me-1"></i>${okLabel}`
                       : '<i class="bi bi-x-lg me-1"></i>Copy failed';
    setTimeout(() => { if (btn.isConnected) btn.innerHTML = orig; }, 1500);
  }
  if (!ok) showToast('Could not reach the clipboard — select the text and copy manually', 'bg-warning');
  return ok;
}

/** Drill-down: every document about one attack ID, searched across all
 *  indices whose names contain "dp-" or "attack-data", grouped per index. */
async function showAttackDetails(attackId) {
  if (!attackId) return;
  document.querySelector('.rt-modal-overlay.rt-attack-details')?.remove();
  const wrap = document.createElement('div');
  wrap.className = 'rt-modal-overlay rt-attack-details';
  wrap.innerHTML = `<div class="rt-modal" style="min-width:640px;width:860px;max-width:95vw;
        max-height:90vh;min-height:220px;display:flex;flex-direction:column;
        resize:both;overflow:hidden;">
      <div class="rt-modal-title" style="flex:0 0 auto;">
        <i class="bi bi-crosshair me-1"></i>Attack <span class="font-monospace">${esc(attackId)}</span> — full record</div>
      <div class="rt-modal-body atk-body" style="flex:1 1 auto;min-height:0;overflow:auto;">
        <div class="text-center py-4 text-secondary">
          <span class="spinner-border spinner-border-sm me-2"></span>
          Searching "dp-" and "attack-data" indices…</div>
      </div>
      <div class="rt-modal-actions" style="flex:0 0 auto;">
        <!-- Copying the whole record is the point of opening this: the next
             step is almost always pasting it into a ticket or a chat with R&D,
             and doing that by hand from a dozen collapsed groups is why people
             screenshot it instead. Disabled until the data arrives. -->
        <button class="btn btn-sm btn-outline-primary atk-copy-json" data-act="copy-json" disabled
                title="Copy every document, from every index, as JSON">
          <i class="bi bi-clipboard me-1"></i>Copy all (JSON)</button>
        <button class="btn btn-sm btn-outline-primary atk-copy-text" data-act="copy-text" disabled
                title="Copy every document as plain key: value text, grouped by index">
          <i class="bi bi-clipboard me-1"></i>Copy all (text)</button>
        <button class="btn btn-sm btn-secondary" data-act="close">Close</button>
      </div></div>`;
  document.body.appendChild(wrap);
  wrap.addEventListener('click', (e) => {
    if (e.target === wrap || e.target.closest('button')?.dataset.act === 'close') wrap.remove();
  });

  const data = await api('/api/cc/attacks/' + encodeURIComponent(attackId) + '/details');
  const body = wrap.querySelector('.atk-body');
  if (!body) return;                       // modal closed while loading
  if (!data || data.error) {
    body.innerHTML = `<div class="text-danger small p-2">${esc(data?.error || 'request failed')}</div>`;
    return;
  }
  if (!data.indices?.length) {
    body.innerHTML = `<div class="text-secondary small p-2">No documents found for
      ${data.id_forms.map(f => `<code>${esc(f)}</code>`).join(' / ')}
      in any "dp-" / "attack-data" index.</div>`;
    return;
  }
  const fmtVal = (v) => v === null || v === undefined ? '—'
    : typeof v === 'object' ? JSON.stringify(v) : String(v);
  body.innerHTML = `
    <div class="small text-secondary mb-2">Found <b>${data.returned}</b>${
      data.total > data.returned ? ` of ${data.total.toLocaleString()}` : ''} document(s)
      for ID forms ${data.id_forms.map(f => `<code>${esc(f)}</code>`).join(' / ')}
      in ${data.indices.length} ${data.indices.length > 1 ? 'indices' : 'index'}.</div>` +
    data.indices.map(g => `
      <details class="mb-2 border border-secondary rounded p-2" ${data.indices.length === 1 ? 'open' : ''}>
        <summary class="small fw-semibold" style="cursor:pointer;">${esc(g.index)}
          <span class="badge bg-info text-dark ms-1">${g.count} doc${g.count > 1 ? 's' : ''}</span>
          <button class="btn btn-sm btn-outline-secondary py-0 px-1 ms-2 atk-copy-one"
                  data-index="${esc(g.index)}" onclick="event.preventDefault();event.stopPropagation();"
                  title="Copy just this index&apos;s documents as JSON"
                  style="font-size:0.7rem;"><i class="bi bi-clipboard"></i></button></summary>
        ${g.docs.map(d => `<table class="table table-sm mb-2 mt-2" style="font-size:0.75rem;">
            <tbody>${Object.entries(d).map(([k, v]) => `<tr>
              <td class="text-secondary" style="width:30%;">${esc(k)}</td>
              <td class="font-monospace" style="word-break:break-all;">${esc(fmtVal(v))}</td>
            </tr>`).join('')}</tbody>
          </table>`).join('<hr class="my-1">')}
      </details>`).join('');

  // ── Copy wiring ──────────────────────────────────────────────────────────
  // Built from the DATA, not by scraping the DOM: the groups are collapsed
  // <details> and half of them have never been rendered open, so reading the
  // table cells back would copy whatever happened to be expanded.
  const asText = () => data.indices.map(g =>
    [`── ${g.index} (${g.count} doc${g.count > 1 ? 's' : ''}) ──`]
      .concat(g.docs.map(d => Object.entries(d)
        .map(([k, v]) => `${k}: ${fmtVal(v)}`).join('\n')))
      .join('\n\n')
  ).join('\n\n');

  const header = `Attack ${attackId} — ${data.returned} document(s) in `
               + `${data.indices.length} index/indices\n\n`;

  const jsonBtn = wrap.querySelector('.atk-copy-json');
  const textBtn = wrap.querySelector('.atk-copy-text');
  if (jsonBtn) {
    jsonBtn.disabled = false;
    jsonBtn.onclick = () => copyTextToClipboard(JSON.stringify(data, null, 2), jsonBtn);
  }
  if (textBtn) {
    textBtn.disabled = false;
    textBtn.onclick = () => copyTextToClipboard(header + asText(), textBtn);
  }
  wrap.querySelectorAll('.atk-copy-one').forEach(btn => {
    btn.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();        // must not toggle the <details> it sits in
      const group = data.indices.find(g => g.index === btn.dataset.index);
      if (group) copyTextToClipboard(JSON.stringify(group, null, 2), btn, '');
    };
  });
}

/* ══════════════════════════════════════════════════════════════════════════
   SUMMARY ANALYTICS VIEW
   ══════════════════════════════════════════════════════════════════════════ */

const CAT_COLORS = [
  '#e74c3c','#e67e22','#f1c40f','#2ecc71','#3498db',
  '#9b59b6','#1abc9c','#e91e63','#607d8b','#00bcd4','#ff5722',
];

/** Histogram-bucket date → 'YYYY-MM-DD'. Tolerates epoch-ms numbers (older
 *  ES versions omit key_as_string) — a raw number used to crash rendering. */
function _dayStr(d) {
  if (typeof d === 'number') { try { return new Date(d).toISOString().slice(0, 10); } catch { return String(d); } }
  return String(d || '').slice(0, 10);
}

async function downloadSummaryJson() {
  const btn = document.getElementById('btnDownloadJson');
  const origHtml = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Building…';

  try {
    const res = await fetch(appUrl('/api/cc/summary/export'));
    if (!res.ok) throw new Error('HTTP ' + res.status);

    // Extract filename from Content-Disposition header
    const cd = res.headers.get('Content-Disposition') || '';
    const match = cd.match(/filename="?([^"]+)"?/);
    const filename = match ? match[1] : 'cc_summary.json';

    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    showToast(`Downloaded ${filename}`, 'bg-success');
  } catch (e) {
    showToast('Download failed: ' + e.message, 'bg-danger');
  } finally {
    btn.disabled = false;
    btn.innerHTML = origHtml;
  }
}

async function loadSummary() {  const data = await api('/api/cc/summary');
  if (data.error) {
    showToast('Summary error: ' + data.error, 'bg-danger');
    return;
  }
  summaryData = data;

  // ── Stat cards ──────────────────────────────────────────────────────────
  setText('s-totalAttacks', (data.total_attacks ?? 0).toLocaleString());
  const dur = data.duration_overall || {};
  setText('s-avgDur', dur.avg_s != null ? dur.avg_s.toLocaleString() : '—');
  setText('s-maxDur', dur.max_s != null ? dur.max_s.toLocaleString() : '—');
  setText('s-maxBps',  data.max_attack_bps != null ? fmtBps(data.max_attack_bps) : '—');
  const gaps = data.inter_attack_gaps?.overall || {};
  setText('s-avgGap',  gaps.avg_s  != null ? gaps.avg_s.toLocaleString()  : '—');
  const tr = data.traffic || {};
  setText('s-trafficAvg', tr.avg_bps != null ? fmtBps(tr.avg_bps) : '—');

  // Each widget renders independently — one bad payload must not blank the
  // rest of the page (a traffic-date crash once emptied 5 widgets at once).
  const _widget = (label, fn) => {
    try { fn(); }
    catch (e) { console.error(`summary widget "${label}" failed:`, e);
                showToast(`Summary widget "${label}" failed: ${e.message}`, 'bg-danger'); }
  };

  // ── Charts ──────────────────────────────────────────────────────────────
  _widget('attacks over time', () => renderSummaryTimeline(data));
  _widget('attack categories', () => renderSummaryCategory(data.by_category || []));
  _widget('traffic over time', () => renderSummaryTraffic(tr.by_day || []));

  // ── Risk / Status mini-lists ─────────────────────────────────────────────
  _widget('by risk',   () => renderKeyValueList('summaryRiskList',   data.by_risk   || [], 'risk'));
  _widget('by status', () => renderKeyValueList('summaryStatusList', data.by_status || [], 'status'));

  _widget('duration + gap tables', () => renderSummaryTables(data));
}

function renderSummaryTables(data) {
  // ── Duration table ────────────────────────────────────────────────────────
  const tbody1 = document.getElementById('durationByCatBody');
  if (!data.duration_by_cat?.length) {
    tbody1.innerHTML = '<tr><td colspan="7" class="text-center text-secondary py-2">No data</td></tr>';
  } else {
    tbody1.innerHTML = data.duration_by_cat.map(r => `<tr>
      <td><span class="badge" style="background:${catColor(r.category)}">${esc(r.category)}</span></td>
      <td class="text-center">${(r.count ?? 0).toLocaleString()}</td>
      <td class="text-end">${r.min_s ?? '—'}</td>
      <td class="text-end fw-semibold">${r.avg_s ?? '—'}</td>
      <td class="text-end text-danger">${r.max_s ?? '—'}</td>
      <td class="text-end">${r.avg_bps != null ? fmtBps(r.avg_bps) : '—'}</td>
      <td class="text-end text-warning">${r.max_bps != null ? fmtBps(r.max_bps) : '—'}</td>
    </tr>`).join('');
  }

  // ── Gap table ──────────────────────────────────────────────────────────────
  const tbody2 = document.getElementById('gapsByCatBody');
  const gapsByCat = data.inter_attack_gaps?.by_category || {};
  const overallGap = data.inter_attack_gaps?.overall || {};
  const rows = [
    { scope: '⬛ Overall (all categories)', ...overallGap, _overall: true },
    ...Object.entries(gapsByCat)
              .sort((a, b) => (b[1].attack_count || 0) - (a[1].attack_count || 0))
              .map(([cat, g]) => ({ scope: cat, ...g })),
  ];
  tbody2.innerHTML = rows.map(r => {
    const boldClass = r._overall ? 'fw-bold table-active' : '';
    const scopeCell = r._overall
      ? `<td class="${boldClass}">${esc(r.scope)}</td>`
      : `<td><span class="badge" style="background:${catColor(r.scope)}">${esc(r.scope)}</span></td>`;
    return `<tr class="${boldClass}">
      ${scopeCell}
      <td class="text-center">${(r.attack_count ?? '—').toLocaleString?.() ?? '—'}</td>
      <td class="text-center">${(r.gap_count   ?? '—').toLocaleString?.() ?? '—'}</td>
      <td class="text-end">${r.min_s ?? '—'}</td>
      <td class="text-end fw-semibold">${r.avg_s ?? '—'}</td>
      <td class="text-end text-danger">${r.max_s ?? '—'}</td>
    </tr>`;
  }).join('');
}

function setTimeGranularity(gran) {
  summaryGranularity = gran;
  ['day','week','month'].forEach(g => {
    const btn = document.getElementById('btnGran' + g.charAt(0).toUpperCase() + g.slice(1));
    if (btn) btn.classList.toggle('active', g === gran);
  });
  if (summaryData) renderSummaryTimeline(summaryData);
}

function renderSummaryTimeline(data) {
  const buckets = (summaryGranularity === 'week'  ? data.attacks_by_week  :
                   summaryGranularity === 'month' ? data.attacks_by_month :
                                                    data.attacks_by_day) || [];
  const ctx = document.getElementById('chartSummaryTimeline').getContext('2d');
  if (chartSummaryTimeline) chartSummaryTimeline.destroy();
  if (!buckets.length) return;
  chartSummaryTimeline = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: buckets.map(b => _dayStr(b.date)),
      datasets: [{
        label: 'Attacks',
        data: buckets.map(b => b.count),
        backgroundColor: 'rgba(220,53,69,.7)',
        borderColor: '#dc3545',
        borderWidth: 1,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { x: { ticks: { maxTicksLimit: 15 } } },
    },
  });
}

function renderSummaryCategory(buckets) {
  const ctx = document.getElementById('chartSummaryCategory').getContext('2d');
  if (chartSummaryCategory) chartSummaryCategory.destroy();
  if (!buckets.length) return;
  chartSummaryCategory = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: buckets.map(b => b.key),
      datasets: [{
        data: buckets.map(b => b.count),
        backgroundColor: buckets.map((_, i) => CAT_COLORS[i % CAT_COLORS.length]),
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'right', labels: { font: { size: 10 }, boxWidth: 12 } } },
    },
  });
}

function renderSummaryTraffic(byDay) {
  const ctx = document.getElementById('chartSummaryTraffic').getContext('2d');
  if (chartSummaryTraffic) chartSummaryTraffic.destroy();
  if (!byDay.length) return;
  chartSummaryTraffic = new Chart(ctx, {
    type: 'line',
    data: {
      labels: byDay.map(b => _dayStr(b.date)),
      datasets: [{
        label: 'Avg bps',
        data: byDay.map(b => b.avg_bps || 0),
        borderColor: '#0dcaf0', backgroundColor: 'rgba(13,202,240,.15)',
        fill: true, tension: 0.3, pointRadius: 3,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { x: { ticks: { maxTicksLimit: 12 } } },
    },
  });
}

function renderKeyValueList(containerId, items, type) {
  const el = document.getElementById(containerId);
  if (!items.length) { el.innerHTML = '<span class="text-secondary small">No data</span>'; return; }
  const total = items.reduce((s, i) => s + (i.count || 0), 0);
  const colors = { High: 'danger', Medium: 'warning', Low: 'success',
                   Terminated: 'secondary', Active: 'danger', Unknown: 'dark' };
  el.innerHTML = items.map(i => {
    const pct = total ? Math.round((i.count / total) * 100) : 0;
    const badgeC = colors[i.key] || 'info';
    return `<div class="d-flex justify-content-between align-items-center mb-1">
      <span class="badge bg-${badgeC}">${esc(i.key)}</span>
      <span class="small text-secondary">${i.count.toLocaleString()} <span class="text-muted">(${pct}%)</span></span>
    </div>
    <div class="progress mb-2" style="height:4px;">
      <div class="progress-bar bg-${badgeC}" style="width:${pct}%"></div>
    </div>`;
  }).join('');
}

/* ── Helpers ────────────────────────────────────────────────────────────── */
function fmtBps(bps) {
  if (bps == null) return '—';
  if (bps >= 1e9) return (bps / 1e9).toFixed(2) + ' Gbps';
  if (bps >= 1e6) return (bps / 1e6).toFixed(2) + ' Mbps';
  if (bps >= 1e3) return (bps / 1e3).toFixed(1) + ' Kbps';
  return bps.toFixed(0) + ' bps';
}

function catColor(cat) {
  const cats = ['DNS','WebDDoS','BehavioralDOS','SynFlood','Intrusions',
                'Anomalies','AntiScanning','ACL','StatefulACL','DOSShield','TrafficFilters'];
  const idx = cats.indexOf(cat);
  return idx >= 0 ? CAT_COLORS[idx] : '#6c757d';
}

/* ── Query editor ────────────────────────────────────────────────────────── */

/** Parse the Size input. Unlike `parseInt(...) || 10`, an explicit 0 is kept
 *  (size 0 = totals/aggregations only) — only empty/invalid falls back. */
function querySizeValue(dflt = 10) {
  const n = parseInt(document.getElementById('querySize')?.value);
  return Number.isNaN(n) ? dflt : Math.max(0, n);
}

async function runQuery() {
  const index = document.getElementById('queryIndex').value.trim();
  const size  = querySizeValue();
  let body;
  try { body = JSON.parse(document.getElementById('queryBody').value); }
  catch (e) { setQueryResults('✗ Invalid JSON: ' + e.message); return; }

  setQueryResults('Running…');
  document.getElementById('queryMeta').textContent = '';
  captureResults({ hits: [] });
  loadExactFieldMap([index]);          // for column filters on the results

  const data = await api('/api/query', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ index, body, size }),
  });
  if (data.error) {
    setQueryResults('✗ Error: ' + data.error);
    return;
  }
  // The server may have substituted/dropped a sort on a nonexistent field.
  if (data.sort_note) showToast(data.sort_note, 'bg-info');
  document.getElementById('queryMeta').textContent =
    `${(data.total ?? 0).toLocaleString()} hits · ${data.took_ms ?? '?'} ms`;
  setQueryResults(JSON.stringify(data, null, 2));
  captureResults(data);
  // Remember the executed query so column filters can re-query ES server-side.
  _queryBaseItems     = [{ index, query_body: body }];
  _queryTotalMatching = data.total ?? 0;
}

function formatQuery() {
  try {
    const el = document.getElementById('queryBody');
    el.value = JSON.stringify(JSON.parse(el.value), null, 2);
  } catch (_) {}
}

const TEMPLATES = {
  matchAll:      { query: { match_all: {} }, sort: [{ startTime: { order: 'desc' } }] },
  activeAttacks: { query: { term: { 'status.keyword': 'active' } }, sort: [{ startTime: { order: 'desc' } }] },
  aggCategory:   { size: 0, aggs: { by_category: { terms: { field: 'category.keyword', size: 20 } } } },
};
function loadTemplate(name) {
  const tpl = TEMPLATES[name];
  if (tpl) document.getElementById('queryBody').value = JSON.stringify(tpl, null, 2);
}

/* ── Natural language query translator ──────────────────────────────────────── */
let perIndexQueries = [];   // [{index, date_fields, query_body}, ...]

// Base query context + true matching total for the Query Editor results viewer,
// so table column filters can re-query ES when the loaded page is only a slice.
let _queryBaseItems     = null;  // [{index, query_body}] actually executed
let _queryTotalMatching = 0;     // sum of real per-index match totals

/** Default query shown in the editor (also what Clear resets to). */
const DEFAULT_QUERY_BODY = {
  query: { match_all: {} },
  sort: [{ startTime: { order: 'desc' } }],
};

/** Reset the generated-query state: JSON body, multi-index plan, suggestions,
 *  interpretation line. (Used by the Clear button and by an empty Translate.) */
function resetGeneratedQuery() {
  document.getElementById('queryBody').value = JSON.stringify(DEFAULT_QUERY_BODY, null, 2);
  perIndexQueries = [];
  renderPerIndexQueries([]);
  fieldSuggestions = [];
  renderSuggestions([]);
  const infoEl = document.getElementById('nlInterpretation');
  if (infoEl) { infoEl.classList.add('d-none'); infoEl.innerHTML = ''; }
}

/** Full Query-Editor reset: query JSON, free text, types, time range, plan. */
function clearQueryEditor() {
  document.getElementById('nlQueryInput').value = '';
  resetGeneratedQuery();
  clearAttackTypeSelection();
  clearTimeRange();
  showToast('Query editor cleared', 'bg-info');
}

async function translateNlQuery() {
  const text   = document.getElementById('nlQueryInput').value.trim();
  const index  = document.getElementById('queryIndex').value.trim() || 'dp-attack-raw-*';
  const infoEl = document.getElementById('nlInterpretation');
  const types  = [...selectedAttackTypes];

  // Time range — both bounds per Start/End, validated before translating.
  const t = readTimeRange();
  const timeErr = timeRangeError();
  if (timeErr) { showToast(timeErr, 'bg-danger'); showTimeRangePicker(); return; }
  const hasTime = !!(t.startAfter || t.startBefore || t.endAfter || t.endBefore);

  // Sort
  const sortHint = document.getElementById('sortHint')?.value     ?? 'start';
  const sortDir  = document.querySelector('input[name="sortDir"]:checked')?.value ?? 'desc';

  // Nothing to translate from → clear the generated query instead of keeping
  // a stale one (the user emptied the free text and hit Translate).
  if (!text && !types.length && !hasTime) {
    resetGeneratedQuery();
    showToast('Nothing to translate — query reset to default', 'bg-info');
    return;
  }

  infoEl.className = 'small text-secondary px-1';
  infoEl.textContent = '⏳ Translating…';
  infoEl.classList.remove('d-none');

  try {
    const data = await api('/api/query/translate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text,
        index,
        attack_types:    types,
        start_after:     t.startAfter,
        start_before:    t.startBefore,
        end_after:       t.endAfter,
        end_before:      t.endBefore,
        sort_hint:       sortHint,
        sort_direction:  sortDir,
      }),
    });

    if (data.error) {
      infoEl.className = 'small text-danger px-1';
      infoEl.textContent = '✗ ' + data.error;
      return;
    }

    // Store per-index queries
    perIndexQueries = data.per_index_queries || [];

    // Show first query in the textarea
    if (perIndexQueries.length > 0) {
      document.getElementById('queryBody').value =
        JSON.stringify(perIndexQueries[0].query_body, null, 2);
    }

    // Render multi-index accordion + Run All button
    renderPerIndexQueries(perIndexQueries);

    // Render field suggestions (ambiguous references needing confirmation)
    fieldSuggestions = data.suggestions || [];
    renderSuggestions(fieldSuggestions);

    // Show interpretation. A criterion the server could NOT match to a field is
    // dropped from the query — without saying so the user reads "Interpreted
    // as: X" above a query that actually matches everything, so any warning is
    // shown right next to it (and the badges for dropped criteria are struck
    // through so it's obvious which ones didn't make it).
    const dropped = new Set((data.unresolved || []).map(u => String(u.label).toLowerCase()));
    const isDropped = (s) => [...dropped].some(d => String(s).toLowerCase().startsWith(d));
    if (data.interpreted?.length) {
      infoEl.className = 'small px-1 ' + (data.warning ? 'text-warning' : 'text-success');
      infoEl.innerHTML =
        `<i class="bi ${data.warning ? 'bi-exclamation-triangle-fill' : 'bi-check-circle'} me-1"></i>` +
        '<strong>Interpreted as:</strong> ' +
        data.interpreted.map(s => {
          const bad = isDropped(s);
          const cls = bad ? 'bg-danger bg-opacity-25 text-danger border-danger'
                          : 'bg-success bg-opacity-25 text-success border-success';
          return `<span class="badge border ${cls} me-1"${bad ? ' style="text-decoration:line-through"' +
                  ' title="Not applied — no such field in this index"' : ''}>${esc(s)}</span>`;
        }).join('');
    } else {
      infoEl.className = 'small text-warning px-1';
      infoEl.textContent = '⚠ No filters matched — using match_all';
    }
    if (data.warning) {
      infoEl.innerHTML += `<div class="alert alert-warning py-1 px-2 mt-1 mb-0 small">
        <i class="bi bi-exclamation-triangle-fill me-1"></i>${esc(data.warning)}</div>`;
    }
  } catch (e) {
    infoEl.className = 'small text-danger px-1';
    infoEl.textContent = '✗ Network error: ' + e.message;
  }
}

function renderPerIndexQueries(queries) {
  const section   = document.getElementById('perIndexQueriesSection');
  const list      = document.getElementById('perIndexQueriesList');
  const countBadge = document.getElementById('perIndexCount');
  const btnAll    = document.getElementById('btnRunAll');
  const multi     = !!queries && queries.length > 1;

  // Single-query controls (textarea + Run Query + Format) belong to one-index mode.
  const singleEls = ['queryBody', 'btnRunSingle', 'btnFormat'].map(id => document.getElementById(id));
  singleEls.forEach(el => el && el.classList.toggle('d-none', multi));
  if (btnAll) btnAll.classList.toggle('d-none', !multi);
  if (!section || !list) return;

  if (!multi) {                       // one index/type → use the single textarea
    section.classList.add('d-none');
    return;
  }

  // Multi-index → hide the single textarea, show one editable query per index.
  section.classList.remove('d-none');

  // Each group can be excluded from "Run All". A group whose query ended up as
  // match_all matches EVERY document in that index — usually because none of
  // the typed criteria exist there — so it is flagged and offered for removal.
  queries.forEach(q => { if (q.include === undefined) q.include = true; });
  const matchAllIdx = queries
    .map((q, i) => (isMatchAllQuery(q.query_body) ? i : -1))
    .filter(i => i >= 0 && queries[i].include);

  if (countBadge) {
    const on = queries.filter(q => q.include).length;
    countBadge.textContent = on === queries.length
      ? `${queries.length} groups` : `${on} of ${queries.length} groups`;
  }

  // The suggestion only makes sense once field references are settled — while
  // suggestions are pending, a match_all may just be "not resolved yet".
  const pending = (typeof fieldSuggestions !== 'undefined' && fieldSuggestions.length);
  const banner = !matchAllIdx.length ? '' : pending
    ? `<div class="alert alert-secondary py-1 px-2 small mb-1">
         ${matchAllIdx.length} group(s) currently match every document — pick the right
         field above first, then re-translate.</div>`
    : `<div class="alert alert-warning py-1 px-2 small mb-1 d-flex align-items-center gap-2">
         <i class="bi bi-exclamation-triangle-fill"></i>
         <span class="flex-grow-1"><b>${matchAllIdx.length} of ${queries.length}</b> groups match
           <b>every document</b> in their index — none of your criteria exist there.</span>
         <button class="btn btn-sm btn-outline-warning py-0 px-2" onclick="dropMatchAllQueries()">
           Skip those ${matchAllIdx.length}
         </button>
       </div>`;

  list.innerHTML = banner + queries.map((q, i) => {
    const sortEntry  = q.query_body?.sort?.[0] || {};
    const sortField  = Object.keys(sortEntry)[0] || '';
    const sortOrder  = sortEntry[sortField]?.order || 'desc';
    const dates      = q.date_fields || [];
    const matchAll   = isMatchAllQuery(q.query_body);
    const off        = !q.include;
    // Each index family has its own date fields, so the sort belongs per group
    // rather than to the one global picker — this plan may mix startTime,
    // timeStamp and indices with no date field at all.
    const sortOpts = ['<option value="">no sort</option>'].concat(
      dates.map(f => `<option value="${esc(f)}"${f === sortField ? ' selected' : ''}>${esc(f)}</option>`)
    ).join('');
    return `<div class="border rounded mb-1 ${off ? 'border-secondary' : matchAll ? 'border-warning' : 'border-secondary'}"
                 data-group="${i}" style="font-size:0.78rem;overflow:hidden;${off ? 'opacity:.55;' : ''}">
      <div class="d-flex align-items-center px-2 py-1 gap-2"
           style="background:rgba(255,255,255,0.04);">
        <input type="checkbox" class="form-check-input mt-0" ${q.include ? 'checked' : ''}
               onchange="togglePerIndexQuery(${i}, this.checked)"
               title="Include this index when running the plan"/>
        <span class="flex-grow-1 d-flex align-items-center gap-2"
              style="cursor:pointer;" onclick="togglePerIndexBody(${i})">
          <i class="bi bi-layers text-info"></i>
          <span class="text-info fw-semibold">${esc(q.index)}</span>
          ${matchAll ? '<span class="badge bg-warning text-dark" title="This query has no filter — it returns every document in the index">matches ALL docs</span>' : ''}
        </span>
        <span class="d-flex align-items-center gap-1" title="${dates.length
            ? 'Sort this index by one of its own date fields'
            : 'This index has no date field — it cannot be sorted by time'}">
          <span class="text-secondary small">sort:</span>
          <select class="form-select form-select-sm py-0 perindex-sort" ${dates.length ? '' : 'disabled'}
                  style="width:auto;font-size:0.72rem;background:#2a2a2a;color:#ddd;border-color:rgba(255,255,255,0.2);"
                  onchange="setPerIndexSort(${i}, this.value, null)">${sortOpts}</select>
          <button class="btn btn-sm btn-outline-secondary py-0 px-1 perindex-dir"
                  ${sortField ? '' : 'disabled'} style="font-size:0.7rem;"
                  title="Ascending / descending"
                  onclick="setPerIndexSort(${i}, null, '${sortOrder === 'asc' ? 'desc' : 'asc'}')"
          >${sortOrder === 'asc' ? '▲ Asc' : '▼ Desc'}</button>
        </span>
        <i class="bi bi-chevron-down text-secondary" style="cursor:pointer;"
           onclick="togglePerIndexBody(${i})" title="Show / hide the query"></i>
      </div>
      <div class="m-0 p-2 d-none perindex-body" style="background:#111;">
        <textarea class="form-control perindex-query" data-idx="${i}" spellcheck="false"
                  oninput="updatePerIndexQuery(${i}, this)"
                  style="font-size:0.72rem;font-family:monospace;min-height:170px;resize:vertical;
                         background:#0d0d0d;color:#e6e6e6;border-color:#333;">${esc(JSON.stringify(q.query_body, null, 2))}</textarea>
        <div class="perindex-err small text-danger mt-1 d-none"></div>
      </div>
    </div>`;
  }).join('');

  syncRunAllButton();
}

/** True when a query body has no filter at all (returns the whole index). */
function isMatchAllQuery(body) {
  const q = body?.query;
  if (!q) return true;
  if (q.match_all) return true;
  // bool with only a match_all must (what translate builds for "nothing matched")
  const b = q.bool;
  if (b && !b.filter && !b.should && !b.must_not) {
    const must = Array.isArray(b.must) ? b.must : (b.must ? [b.must] : []);
    return must.length === 1 && !!must[0].match_all;
  }
  return false;
}

/** Groups the user kept — what Run All actually executes. */
function includedPerIndexQueries() {
  return perIndexQueries.filter(q => q.include !== false);
}

function togglePerIndexQuery(i, on) {
  if (perIndexQueries[i]) perIndexQueries[i].include = !!on;
  renderPerIndexQueries(perIndexQueries);
}

function togglePerIndexBody(i) {
  document.querySelector(`#perIndexQueriesList [data-group="${i}"] .perindex-body`)
    ?.classList.toggle('d-none');
}

/** Set one group's sort field and/or direction (null keeps the current one).
 *  Updates in place rather than re-rendering, so an expanded query stays open. */
function setPerIndexSort(i, field, order) {
  const q = perIndexQueries[i];
  if (!q) return;
  const body = q.query_body || (q.query_body = {});
  const cur       = body.sort?.[0] || {};
  const curField  = Object.keys(cur)[0] || '';
  const curOrder  = cur[curField]?.order || 'desc';
  const nextField = field === null ? curField : field;
  const nextOrder = order === null ? curOrder : order;

  if (!nextField) delete body.sort;
  else body.sort = [{ [nextField]: { order: nextOrder } }];

  const row = document.querySelector(`#perIndexQueriesList [data-group="${i}"]`);
  const ta  = row?.querySelector('textarea.perindex-query');
  if (ta) ta.value = JSON.stringify(body, null, 2);
  const dir = row?.querySelector('.perindex-dir');
  if (dir) {
    dir.disabled = !nextField;
    dir.textContent = nextOrder === 'asc' ? '▲ Asc' : '▼ Desc';
    dir.setAttribute('onclick',
      `setPerIndexSort(${i}, null, '${nextOrder === 'asc' ? 'desc' : 'asc'}')`);
  }
  const sel = row?.querySelector('select.perindex-sort');
  if (sel && sel.value !== nextField) sel.value = nextField;
}

function dropMatchAllQueries() {
  let n = 0;
  perIndexQueries.forEach(q => {
    if (q.include !== false && isMatchAllQuery(q.query_body)) { q.include = false; n++; }
  });
  renderPerIndexQueries(perIndexQueries);
  showToast(`Skipped ${n} index(es) whose query matched everything`, 'bg-warning');
}

/** Run All reflects how many groups are actually selected. */
function syncRunAllButton() {
  const btn = document.getElementById('btnRunAll');
  if (!btn) return;
  const n = includedPerIndexQueries().length;
  btn.disabled = n === 0;
  btn.innerHTML = `<i class="bi bi-play-circle-fill me-1"></i>Run ${n} ${n === 1 ? 'Index' : 'Indices'}`;
}

/** Keep an edited per-index query in sync; flag invalid JSON inline. */
function updatePerIndexQuery(i, el) {
  const errEl = el.parentElement.querySelector('.perindex-err');
  try {
    const parsed = JSON.parse(el.value);
    if (perIndexQueries[i]) perIndexQueries[i].query_body = parsed;
    el.style.borderColor = '#333';
    if (errEl) errEl.classList.add('d-none');
  } catch (e) {
    el.style.borderColor = '#dc3545';
    if (errEl) { errEl.textContent = '✗ Invalid JSON: ' + e.message; errEl.classList.remove('d-none'); }
  }
}

/* ── Field suggestions (ambiguous references the user must confirm) ─────────── */
let fieldSuggestions = [];

const _OP_SYMBOL = { eq: '=', contains: 'contains', neq: '≠', ncontains: 'not-contains' };

function _suggestionValues(s) {
  return (Array.isArray(s.values) && s.values.length) ? s.values : [s.value];
}

function _suggestionLabel(s) {
  if (s.kind === 'exists') return `${s.label} ${s.present ? 'exists' : 'missing'}`;
  const vals = _suggestionValues(s);
  const shown = vals.length > 1 ? `[${vals.join(', ')}]` : vals[0];
  return `${s.label} ${_OP_SYMBOL[s.op] || '='} ${shown}`;
}

/** Inject a clause into a query_body's bool (wrapping non-bool queries). */
function injectClause(qb, clause, where) {
  qb.query = qb.query || { match_all: {} };
  let q = qb.query;
  if (!q.bool) {
    q = q.match_all ? { bool: { must: [] } } : { bool: { must: [q] } };
    qb.query = q;
  }
  q.bool[where] = q.bool[where] || [];
  q.bool[where].push(clause);
  if (where === 'must_not' && !(q.bool.must && q.bool.must.length)) {
    q.bool.must = [{ match_all: {} }];
  }
}

/** Build the ES clause for a chosen candidate field (multi-value → terms/should). */
function buildSuggestionClause(s, field) {
  if (s.kind === 'exists') return { exists: { field } };
  const vals = _suggestionValues(s);
  if (s.op === 'contains' || s.op === 'ncontains') {
    return vals.length > 1
      ? { bool: { should: vals.map(v => ({ wildcard: { [field]: `*${v}*` } })), minimum_should_match: 1 } }
      : { wildcard: { [field]: `*${vals[0]}*` } };
  }
  return vals.length > 1 ? { terms: { [field]: vals } } : { match: { [field]: vals[0] } };
}

/** Apply a specific candidate field for suggestion i. */
function applySuggestionField(i, field) {
  const s = fieldSuggestions[i];
  if (!s || s._applied) return;
  const item = perIndexQueries.find(p => p.index === s.index);
  if (item) {
    injectClause(item.query_body, buildSuggestionClause(s, field), s.where);
    if (perIndexQueries[0]?.index === s.index) {
      document.getElementById('queryBody').value =
        JSON.stringify(perIndexQueries[0].query_body, null, 2);
    }
    renderPerIndexQueries(perIndexQueries);
  }
  s._applied = true;
  s._appliedField = field;
  renderSuggestions(fieldSuggestions);
}

function dismissSuggestion(i) {
  if (fieldSuggestions[i]) fieldSuggestions[i]._dismissed = true;
  renderSuggestions(fieldSuggestions);
}

function renderSuggestions(list) {
  const box = document.getElementById('fieldSuggestions');
  if (!box) return;
  const pending = (list || []).filter(s => !s._applied && !s._dismissed);
  if (!pending.length) { box.classList.add('d-none'); box.innerHTML = ''; return; }

  box.classList.remove('d-none');
  box.innerHTML = `
    <div class="rounded p-2" style="background:rgba(255,193,7,0.08);border:1px solid rgba(255,193,7,0.35);">
      <div class="d-flex align-items-center gap-2 mb-2">
        <i class="bi bi-question-circle text-warning"></i>
        <span class="small fw-semibold text-warning">Field suggestions — pick a field to apply, or dismiss</span>
      </div>
      ${list.map((s, i) => {
        if (s._applied || s._dismissed) return '';
        const cands = (s.candidates || []);
        const btns = cands.length
          ? cands.map(c =>
              `<button class="btn btn-sm btn-warning py-0 px-2" onclick="applySuggestionField(${i}, '${esc(c.field)}')"
                       title="matched ${c.score}/${s.total} words">
                 <i class="bi bi-check-lg me-1"></i>${esc(c.field)}
                 <span class="opacity-75" style="font-size:0.65rem;">${c.score}/${s.total}</span>
               </button>`).join('')
          : `<span class="text-secondary fst-italic">no similar field found</span>`;
        return `<div class="d-flex align-items-center gap-2 mb-1 flex-wrap" style="font-size:0.78rem;">
          <span class="text-secondary">No field matches</span>
          <span class="badge bg-secondary">${esc(_suggestionLabel(s))}</span>
          <span class="text-secondary">in</span>
          <span class="text-info">${esc(s.index)}</span>
          <span class="text-secondary">— did you mean:</span>
          ${btns}
          <button class="btn btn-sm btn-outline-secondary py-0 px-2 ms-auto" onclick="dismissSuggestion(${i})" title="Dismiss">
            <i class="bi bi-x-lg"></i>
          </button>
        </div>`;
      }).join('')}
    </div>`;
}

async function runMultiQuery(queries) {
  if (!queries || !queries.length) return;
  const size = querySizeValue();
  loadExactFieldMap(queries.map(q => q.index));
  const metaEl = document.getElementById('queryMeta');
  setQueryResults(`⏳ Running ${queries.length} index queries…`);
  metaEl.textContent = '';
  captureResults({ hits: [] });

  try {
    const data = await api('/api/query/multi-run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        per_index_queries: queries,
        size,
        sort_direction: document.querySelector('input[name="sortDir"]:checked')?.value ?? 'desc',
      }),
    });

    if (data.error) {
      setQueryResults('✗ Error: ' + data.error);
      return;
    }

    // Build per-index meta line
    const metaParts = (data.per_index_meta || []).map(m =>
      m.error
        ? `${m.index.split('-ty-')[0]}: ✗ ${m.error}`
        : `${m.index.split('-ty-')[0]}: ${(m.total||0).toLocaleString()} total, ${m.returned} shown, ${m.took_ms}ms`
    );
    metaEl.textContent =
      `${(data.total_hits ?? 0).toLocaleString()} hits across ${queries.length} indices  ·  ${metaParts.join('  |  ')}`;
    setQueryResults(JSON.stringify(data, null, 2));
    captureResults(data);
    // Remember the executed queries + true match total for server-side filtering.
    _queryBaseItems     = queries.map(q => ({ index: q.index, query_body: q.query_body }));
    _queryTotalMatching = (data.per_index_meta || []).reduce((s, m) => s + (m.total || 0), 0);
  } catch (e) {
    setQueryResults('✗ Network error: ' + e.message);
  }
}

/* ── Time-range picker ───────────────────────────────────────────────────── */
function showTimeRangePicker() {
  const picker = document.getElementById('timeRangePicker');
  if (!picker) return;
  picker.classList.remove('d-none');
  onTimeRangeChanged();
}

function hideTimeRangePicker() {
  document.getElementById('timeRangePicker')?.classList.add('d-none');
}

function toggleTimeRangePicker() {
  const picker = document.getElementById('timeRangePicker');
  if (!picker) return;
  if (picker.classList.contains('d-none')) showTimeRangePicker();
  else hideTimeRangePicker();
}

const TIME_RANGE_IDS = ['startAfterValue', 'startBeforeValue', 'endAfterValue', 'endBeforeValue'];

/** Read the four time-range bounds { startAfter, startBefore, endAfter, endBefore }. */
function readTimeRange() {
  const v = (id) => document.getElementById(id)?.value || '';
  return { startAfter: v('startAfterValue'), startBefore: v('startBeforeValue'),
           endAfter:   v('endAfterValue'),   endBefore:   v('endBeforeValue') };
}

/** Validate the bounds: within each row, "after" must not exceed "before".
 *  Returns an error string, or '' when legal. datetime-local values compare
 *  correctly as strings (ISO format). Also paints the offending inputs red. */
function timeRangeError() {
  const t = readTimeRange();
  const mark = (ids, bad) => ids.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('is-invalid', bad);
  });
  let err = '';
  const startBad = !!(t.startAfter && t.startBefore && t.startAfter > t.startBefore);
  const endBad   = !!(t.endAfter   && t.endBefore   && t.endAfter   > t.endBefore);
  mark(['startAfterValue', 'startBeforeValue'], startBad);
  mark(['endAfterValue', 'endBeforeValue'], endBad);
  if (startBad) err = 'Illegal Started-At range — "after" is later than "before".';
  else if (endBad) err = 'Illegal Ended-At range — "after" is later than "before".';
  return err;
}

function clearTimeRange() {
  TIME_RANGE_IDS.forEach(id => {
    const el = document.getElementById(id);
    if (el) { el.value = ''; el.classList.remove('is-invalid'); }
  });
  document.getElementById('timeRangeLabel')?.classList.add('d-none');
  document.getElementById('timeRangeError')?.classList.add('d-none');
  _updateTimeRangeBtn();
}

function onTimeRangeChanged() {
  const t     = readTimeRange();
  const label = document.getElementById('timeRangeLabel');
  const errEl = document.getElementById('timeRangeError');

  const err = timeRangeError();
  if (errEl) {
    errEl.textContent = err;
    errEl.classList.toggle('d-none', !err);
  }

  if (label) {
    const fmt = (v) => v.replace('T', ' ');
    const parts = [];
    if (t.startAfter)  parts.push(`Started ≥ ${fmt(t.startAfter)}`);
    if (t.startBefore) parts.push(`Started ≤ ${fmt(t.startBefore)}`);
    if (t.endAfter)    parts.push(`Ended ≥ ${fmt(t.endAfter)}`);
    if (t.endBefore)   parts.push(`Ended ≤ ${fmt(t.endBefore)}`);
    if (parts.length && !err) { label.textContent = parts.join('  ·  '); label.classList.remove('d-none'); }
    else                      { label.classList.add('d-none'); }
  }
  _updateTimeRangeBtn();
}

function _updateTimeRangeBtn() {
  const t   = readTimeRange();
  const btn = document.getElementById('btnTimeRange');
  if (!btn) return;
  const active = !!(t.startAfter || t.startBefore || t.endAfter || t.endBefore);
  btn.innerHTML = active
    ? `<i class="bi bi-calendar-check-fill me-1 text-warning"></i>Time`
    : `<i class="bi bi-calendar-range me-1"></i>Time`;
}

/* ── Sort picker ─────────────────────────────────────────────────────────── */
function onSortChanged() {
  const hint = document.getElementById('sortHint')?.value ?? '';
  const grp  = document.getElementById('sortDirGroup');
  // Hide direction buttons when "None" is selected
  if (grp) grp.style.opacity = hint ? '1' : '0.35';
}

/* ── Sort field picker ──────────────────────────────────────────────────────
 * Offer the date fields the index pattern ACTUALLY has. There is no universal
 * startTime/endTime in CC: dp-attack-raw-* has startTime+endTime, other
 * families have timeStamp, day, or nothing at all — and asking ES to sort on a
 * field an index lacks fails the whole search. Default stays "None". */
let _sortFieldsKey = '';

async function loadSortFields(force) {
  const sel = document.getElementById('sortHint');
  const hintEl = document.getElementById('sortFieldHint');
  const pattern = (document.getElementById('queryIndex')?.value || '').trim();
  if (!sel) return;
  if (!pattern) { _sortFieldsKey = ''; return; }
  if (pattern === _sortFieldsKey && !force) return;

  const list = pattern.split(',').map(s => s.trim()).filter(Boolean);
  let dates = [];
  try {
    const d = await api('/api/indices/exact-fields', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ indices: list }),
    });
    if (d && !d.error) dates = d.dates || [];
  } catch { /* leave the picker as-is; the query just goes unsorted */ }
  _sortFieldsKey = pattern;

  const keep = sel.value;
  sel.innerHTML = '<option value="">None</option>' +
    dates.map(f => `<option value="${esc(f)}">${esc(f)}</option>`).join('');
  // Keep the user's choice when it still exists in the new pattern.
  sel.value = dates.includes(keep) ? keep : '';
  onSortChanged();
  if (hintEl) {
    hintEl.textContent = dates.length
      ? `${dates.length} date field(s) in this pattern`
      : 'no date fields in this pattern — results are unsorted';
    hintEl.className = 'small fst-italic ' + (dates.length ? 'text-secondary' : 'text-warning');
  }
}

/* ── Attack-type picker ──────────────────────────────────────────────────── */
let availableAttackTypes = [];
let selectedAttackTypes  = new Set();

const TYPE_FALLBACK = [
  'DNS','WebDDoS','BehavioralDOS','SynFlood','Intrusions',
  'Anomalies','AntiScanning','ACL','StatefulACL','DOSShield','TrafficFilters',
];

async function loadAttackTypes() {
  // Already loaded — just re-render
  if (availableAttackTypes.length) { renderAttackTypeChips(); return; }
  try {
    const data = await api('/api/cc/attack-types');
    availableAttackTypes = data.types?.length ? data.types : TYPE_FALLBACK;
  } catch (_) {
    availableAttackTypes = TYPE_FALLBACK;
  }
  renderAttackTypeChips();
}

function renderAttackTypeChips() {
  const container = document.getElementById('attackTypeChips');
  if (!container) return;
  if (!availableAttackTypes.length) {
    container.innerHTML = '<span class="text-secondary small fst-italic">No types available</span>';
    return;
  }
  container.innerHTML = availableAttackTypes.map(t => {
    const sel  = selectedAttackTypes.has(t);
    const cls  = sel
      ? 'bg-primary text-white'
      : 'text-secondary border border-secondary';
    return `<span class="badge px-2 py-1 ${cls}"
      style="cursor:pointer;user-select:none;font-size:0.78rem;background:${sel ? '' : 'rgba(255,255,255,0.05)'}"
      onclick="toggleAttackType('${t}')">${esc(t)}</span>`;
  }).join('');

  // Update selected-types label
  const label = document.getElementById('selectedTypesLabel');
  if (label) {
    const sel = [...selectedAttackTypes];
    if (sel.length) {
      label.innerHTML =
        `<i class="bi bi-check2-circle me-1"></i><strong>${sel.length}</strong> type${sel.length > 1 ? 's' : ''} selected: ` +
        sel.map(t => `<span class="badge bg-primary me-1">${esc(t)}</span>`).join('');
      label.classList.remove('d-none');
    } else {
      label.classList.add('d-none');
    }
  }

  // Reflect selection count on the Types button
  const btn = document.getElementById('btnTypePicker');
  if (btn) {
    const n = selectedAttackTypes.size;
    btn.innerHTML = n
      ? `<i class="bi bi-tags-fill me-1 text-primary"></i>Types <span class="badge bg-primary ms-1">${n}</span>`
      : `<i class="bi bi-tags me-1"></i>Types`;
  }
}

function toggleAttackType(type) {
  if (selectedAttackTypes.has(type)) selectedAttackTypes.delete(type);
  else selectedAttackTypes.add(type);
  renderAttackTypeChips();
}

function clearAttackTypeSelection() {
  selectedAttackTypes.clear();
  renderAttackTypeChips();
}

function showAttackTypePicker() {
  const picker = document.getElementById('attackTypePicker');
  if (!picker) return;
  picker.classList.remove('d-none');
  loadAttackTypes();
}

function hideAttackTypePicker() {
  document.getElementById('attackTypePicker')?.classList.add('d-none');
}

function toggleAttackTypePicker() {
  const picker = document.getElementById('attackTypePicker');
  if (!picker) return;
  if (picker.classList.contains('d-none')) showAttackTypePicker();
  else hideAttackTypePicker();
}


/* ── Sidebar search ──────────────────────────────────────────────────────── */
document.getElementById('indexSearch').addEventListener('input', function () {
  const q = this.value.toLowerCase();
  renderSidebarIndices(allIndices.filter(i => i.name.toLowerCase().includes(q)));
});

/* ── NL input: auto-show pickers when trigger words are typed ─── */
document.getElementById('nlQueryInput').addEventListener('input', function () {
  const v = this.value;
  // Attack type picker
  if (/\btype\b|\bcategor/i.test(v)) showAttackTypePicker();

  // Time range picker
  if (/started\s+at|ended\s+at|start\s+time|end\s+time|since\b|\bbefore\b|\bafter\b|\btime\s+range\b/i.test(v)) {
    showTimeRangePicker();
  }
});

/* ══════════════════════════════════════════════════════════════════════════
   FEEDBACK & TOAST
   ══════════════════════════════════════════════════════════════════════════ */
function showFeedback(type, html) {
  const el = document.getElementById('connFeedback');
  el.className = `mt-3 alert alert-${type} py-2`;
  el.innerHTML = html;
  el.classList.remove('d-none');
}
function hideFeedback() {
  document.getElementById('connFeedback').classList.add('d-none');
}
function showToast(msg, bgClass = 'bg-dark') {
  const toastEl = document.getElementById('toastMsg');
  document.getElementById('toastBody').textContent = msg;
  toastEl.className = `toast align-items-center text-white border-0 ${bgClass}`;
  bootstrap.Toast.getOrCreateInstance(toastEl, { delay: 3000 }).show();
}

/* ══════════════════════════════════════════════════════════════════════════
   PRESENCE — who else is working on this CC
   ══════════════════════════════════════════════════════════════════════════
   The server identifies each browser by a session cookie and tracks which ES
   target it is connected to. We poll for co-users so we can (a) show them in
   the navbar, (b) warn BEFORE this user changes data, and (c) surface the
   notifications the server queued when THEY changed data. */

let _presence = { you: null, peers: [] };
let _presenceTimer = null;
const PRESENCE_POLL_MS = 7000;

function _peerLine(p) {
  const bits = [p.ip];
  if (p.hostname && p.hostname !== p.label) bits.push(p.hostname);
  if (p.agent) bits.push(p.agent);
  const idle = p.idle_seconds > 60
    ? ` · idle ${Math.floor(p.idle_seconds / 60)}m` : '';
  return `${p.label} (${bits.filter(Boolean).join(' · ')})${idle}`;
}

function renderPresence() {
  const peers = _presence.peers || [];
  const btn = document.getElementById('presencePeers');
  const cnt = document.getElementById('presenceCount');
  const me = document.getElementById('presenceMe');
  if (!btn || !me) return;
  if (peers.length) {
    btn.classList.remove('d-none');
    // Amber once someone else is here — this is the "be careful" signal.
    btn.className = 'btn btn-sm py-0 px-2 btn-warning';
    cnt.textContent = `${peers.length} other${peers.length > 1 ? 's' : ''} on this CC`;
    btn.title = 'Also working on this CC:\n' + peers.map(p => '• ' + _peerLine(p)).join('\n');
  } else {
    btn.classList.add('d-none');
  }
  const you = _presence.you;
  me.textContent = you ? `you: ${you.label}` : 'you';
  me.title = you
    ? `You are shown to others as "${you.label}"`
      + `\nIP ${you.ip}${you.hostname ? ' · ' + you.hostname : ''}`
      + `${you.agent ? ' · ' + you.agent : ''}\n\nClick to set a display name.`
    : 'Click to set a display name';
}

/** Peer-activity notifications are important and must not be missed, so they
 *  get a persistent banner rather than a 3-second toast. */
function showPeerNotification(n) {
  let box = document.getElementById('peerAlerts');
  if (!box) {
    box = document.createElement('div');
    box.id = 'peerAlerts';
    box.className = 'peer-alerts';
    document.body.appendChild(box);
  }
  const when = new Date((n.ts || 0) * 1000).toLocaleTimeString();
  const el = document.createElement('div');
  el.className = 'peer-alert';
  el.innerHTML = `<i class="bi bi-exclamation-triangle-fill me-2"></i>
    <div class="flex-grow-1">
      <div><b>${esc(n.actor)}</b> ${esc(n.action)}
        ${n.detail ? `<span class="font-monospace">${esc(n.detail)}</span>` : ''}
        on the CC you are working on.</div>
      <div class="small text-secondary">${esc(n.actor_ip || '')} · ${esc(when)}
        — your view may be out of date; refresh before you act on it.</div>
    </div>
    <button class="btn btn-sm btn-outline-light py-0 px-1 ms-2">✕</button>`;
  el.querySelector('button').onclick = () => el.remove();
  box.appendChild(el);
  setTimeout(() => el.remove(), 60000);
}

async function pollPresence() {
  try {
    const d = await api('/api/presence');
    if (!d || d.error) return;
    _presence = { you: d.you, peers: d.peers || [] };
    renderPresence();
    (d.notifications || []).forEach(showPeerNotification);
  } catch { /* transient — next tick retries */ }
}

function startPresence() {
  if (_presenceTimer) return;
  pollPresence();
  _presenceTimer = setInterval(pollPresence, PRESENCE_POLL_MS);
}

function showPresenceDetail() {
  const peers = _presence.peers || [];
  uiChoice(document, {
    title: `${peers.length} other user${peers.length > 1 ? 's' : ''} on this CC`,
    message: peers.length
      ? peers.map(p => '• ' + _peerLine(p)).join('\n')
      : 'Nobody else is connected to this CC right now.',
    buttons: [{ value: null, text: 'Close', cls: 'btn-secondary' }],
  });
}

async function promptDisplayName() {
  const cur = _presence.you?.name || '';
  const name = await uiPrompt(document, {
    title: 'Your display name — shown to other users on the same CC',
    value: cur, okText: 'Save',
  });
  if (name == null) return;
  const d = await api('/api/presence/name', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name.trim() }),
  });
  if (d && !d.error) { _presence = { you: d.you, peers: d.peers || [] }; renderPresence(); }
}

/* Once the user has acknowledged "others are on this CC", don't re-prompt for
 * every cell edit. The acknowledgement is keyed by WHO is present, so it
 * lapses the moment a different user joins — and expires on its own. */
let _sharedCcAck = { key: '', until: 0 };
const SHARED_ACK_MS = 5 * 60 * 1000;

/** Gate every data-changing action: when other users are on this CC, name them
 *  and make the user confirm. Returns true to proceed.
 *  `action` is a short description, e.g. 'delete index "foo"'.
 *  `doc` targets the pop-out results window when the action started there —
 *  these handlers run in the main window's context, so the dialog would
 *  otherwise open behind the window the user is looking at. */
async function confirmSharedCc(action, doc) {
  doc = doc || document;
  let peers = [];
  try {
    const d = await api('/api/presence/peers');       // live, not the poll cache
    peers = d?.peers || [];
    _presence.peers = peers; renderPresence();
  } catch { /* if presence is unavailable, don't block the user's work */ }
  if (!peers.length) return true;

  const key = peers.map(p => p.sid_short).sort().join(',');
  if (_sharedCcAck.key === key && Date.now() < _sharedCcAck.until) return true;

  const names = peers.map(p => '• ' + _peerLine(p)).join('\n');
  const ok = await uiConfirm(doc, {
    title: '⚠ You are about to change data on a shared CC',
    message: `You are about to ${action}.\n\n`
           + `${peers.length} other user${peers.length > 1 ? 's are' : ' is'} `
           + `working on this same CC right now:\n${names}\n\n`
           + `They will be notified of this change. Continue?`,
    okText: 'Yes, continue', danger: true,
  });
  if (ok) _sharedCcAck = { key, until: Date.now() + SHARED_ACK_MS };
  return !!ok;
}

/* ══════════════════════════════════════════════════════════════════════════
   UTILITIES
   ══════════════════════════════════════════════════════════════════════════ */
/** Fetch + parse JSON. A non-JSON body — a bare "Internal Server Error" from an
 *  unhandled exception, or a proxy/gateway error page — is reported as the
 *  app's usual {error} shape instead of throwing a SyntaxError at the caller,
 *  which surfaced to users as "Unexpected token 'I', "Internal S"...". */
async function api(url, opts = {}) {
  const res = await fetch(appUrl(url), opts);
  const text = await res.text();
  try {
    return JSON.parse(text);
  } catch (_) {
    if (res.ok && !text.trim()) return {};          // empty 2xx body
    const detail = text.trim().slice(0, 200);
    return { error: res.ok
      ? `unexpected non-JSON response from ${url}${detail ? `: ${detail}` : ''}`
      : `HTTP ${res.status}${res.statusText ? ' ' + res.statusText : ''}`
        + (detail ? ` — ${detail}` : '') };
  }
}
function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}
function fmtTime(ts) {
  if (!ts) return '—';
  try { return new Date(ts).toLocaleString(); } catch { return String(ts); }
}
function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/* ══════════════════════════════════════════════════════════════════════════
   MARIADB — the CC's relational store
   ══════════════════════════════════════════════════════════════════════════
   Deliberately not a schema tree with 175 leaves. The left pane is the curated
   catalog (which schema holds what), and only once you pick one do its tables
   appear. Same shape as the ES side: the curation is the product, the browse
   screens on top of it are generic.

   Every endpoint answers {error: ...} at HTTP 200 for operational failures —
   an unreachable database is a normal state of a CC being debugged — so these
   handlers check for `.error` rather than relying on a rejected fetch. */

let mariaSchemas   = [];
let mariaTableList = [];
let mariaSchema    = '';     // selected schema
let mariaTable     = '';     // selected table
let mariaColumns   = [];     // column definitions for the selected table
let mariaSample    = null;   // last /sample payload for the selected table
let mariaRelations = null;   // last /keys payload for the selected table

/* Which columns are hidden, per table: 'schema.table' -> [names]. Kept per
   table because the useful subset of device_interface says nothing about the
   useful subset of quartz's triggers, and one global hidden-set would apply
   the wrong one to both. */
let mariaHiddenCols = _mariaLoad('ccadmin.maria.hiddenCols', {});
/* Collapsed state of the detail sections, and the pane widths. Remembered
   because both are a working preference, not a per-visit choice. */
let mariaCollapsed  = _mariaLoad('ccadmin.maria.collapsed', {});
let mariaPaneWidths = _mariaLoad('ccadmin.maria.paneWidths', null);
/* Heights the user has dragged the detail sections to, in px, by section id.
   Relations in particular can run to dozens of rows on a well-connected table
   and the default cap only ever shows the first few. */
let mariaSectionH   = _mariaLoad('ccadmin.maria.sectionH', {});
/* Relations ticked for the join builder, as "out:<i>" / "in:<i>" into the
   current table's relations payload. Deliberately NOT persisted: it belongs to
   one table and one question, and a stale selection restored under a different
   table would build a join nobody asked for. */
let mariaJoinSel = new Set();

function _mariaLoad(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; }
  catch { return fallback; }
}
function _mariaSave(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode */ }
}

function _mariaTableKey() { return `${mariaSchema}.${mariaTable}`; }
function _mariaHiddenSet() {
  return new Set(mariaHiddenCols[_mariaTableKey()] || []);
}

/* Delegated once on the panes rather than per row: the lists are re-rendered
   on every filter keystroke, and re-binding hundreds of listeners each time is
   both wasteful and easy to leak. */
function initMariaPanes() {
  document.getElementById('mariaSchemas')?.addEventListener('click', ev => {
    const btn = ev.target.closest('[data-schema]');
    if (btn) selectMariaSchema(btn.dataset.schema);
  });
  document.getElementById('mariaTables')?.addEventListener('click', ev => {
    const btn = ev.target.closest('[data-table]');
    if (btn) selectMariaTable(btn.dataset.table);
  });
  // Blob cells appear in both the sample pane and the query results, so the
  // listener goes on document rather than being duplicated per pane.
  document.addEventListener('click', ev => {
    const btn = ev.target.closest('[data-blob-qs]');
    if (btn) { ev.preventDefault(); showBlobViewer(btn.dataset.blobQs); }
  });

  const detail = document.getElementById('mariaDetail');
  detail?.addEventListener('click', ev => {
    const head = ev.target.closest('[data-sect]');
    if (head) { toggleMariaSection(head.dataset.sect); return; }
    // Jump to a related table from the relations block.
    const jump = ev.target.closest('[data-goto-table]');
    if (jump) { selectMariaTable(jump.dataset.gotoTable); return; }
    if (ev.target.closest('[data-maria-cols]')) { openMariaColumnPicker(); return; }
    if (ev.target.closest('[data-maria-popout]')) { popOutSqlResults('maria-table'); return; }
    if (ev.target.closest('[data-join-build]')) { openMariaJoinBuilder(); return; }
    if (ev.target.closest('[data-join-clear]')) {
      mariaJoinSel.clear(); renderMariaDetail(); return;
    }
  });
  detail?.addEventListener('change', ev => {
    const tick = ev.target.closest('[data-joinsel]');
    if (!tick) return;
    if (tick.checked) mariaJoinSel.add(tick.dataset.joinsel);
    else mariaJoinSel.delete(tick.dataset.joinsel);
    renderMariaDetail();
  });
  // Double-click to edit, so a single click can still select text in a cell.
  detail?.addEventListener('dblclick', ev => {
    const td = ev.target.closest('td[data-editcol]');
    if (td) beginMariaCellEdit(td);
    const hs = ev.target.closest('.sql-hsplit');
    if (hs) resetMariaSectionHeight(hs.dataset.hsplit);
  });

  // Delegated, because the detail pane is rebuilt on every table change and
  // per-render binding would leak a listener each time.
  detail?.addEventListener('pointerdown', ev => {
    const hs = ev.target.closest('.sql-hsplit');
    if (hs) startMariaSectionDrag(hs, ev);
  });
  detail?.addEventListener('keydown', ev => {
    const hs = ev.target.closest('.sql-hsplit');
    if (!hs || (ev.key !== 'ArrowUp' && ev.key !== 'ArrowDown')) return;
    ev.preventDefault();
    const body = document.querySelector(`[data-sect-body="${hs.dataset.hsplit}"]`);
    const step = (ev.shiftKey ? 40 : 12) * (ev.key === 'ArrowDown' ? 1 : -1);
    _setMariaSectionHeight(hs.dataset.hsplit, body,
                           body.getBoundingClientRect().height + step);
  });

  initMariaSplitters();
  initMariaQueryScreen();
}

/* ── SQL Query screen ─────────────────────────────────────────────────────
   The conditions panel mirrors whatever is in the editor, so it stays correct
   whether the SQL arrived from the join builder, the wizard, or typing. */
function initMariaQueryScreen() {
  const box = document.getElementById('mariaQuerySql');
  // debounced: re-parsing on every keystroke of a 15-line join is wasted work
  // and makes the panel flicker mid-word.
  let t = null;
  box?.addEventListener('input', () => {
    clearTimeout(t);
    t = setTimeout(syncMariaQueryConditions, 250);
  });

  const conds = document.getElementById('mariaQueryConds');
  conds?.addEventListener('click', ev => {
    const del = ev.target.closest('[data-cond-del]');
    if (del) { removeMariaCondition(Number(del.dataset.condDel)); return; }
    if (ev.target.closest('[data-cond-add]')) addMariaCondition(conds);
  });
  conds?.addEventListener('keydown', ev => {
    // Enter anywhere in the add-row commits it, rather than requiring the mouse.
    if (ev.key === 'Enter' && ev.target.closest('.mq-cond-row')) {
      ev.preventDefault();
      addMariaCondition(conds);
    }
  });
  conds?.addEventListener('change', ev => {
    const op = ev.target.closest('.mq-newop');
    if (!op) return;
    const none = op.value === 'IS NULL' || op.value === 'IS NOT NULL';
    conds.querySelector('.mq-newval')?.classList.toggle('d-none', none);
  });

  // The editor / results splitter, same contract as the detail sections.
  const sp = document.querySelector('[data-mqsplit]');
  const editor = document.querySelector('.mq-editor');
  if (!sp || !editor) return;

  const saved = _mariaLoad('ccadmin.maria.editorH', null);
  if (saved) editor.style.height = saved + 'px';

  const clamp = (wanted) => {
    const res = document.querySelector('.mq-results');
    const slack = res ? Math.max(0, res.getBoundingClientRect().height - 140) : 0;
    const max = editor.getBoundingClientRect().height + slack;
    const h = Math.round(Math.min(max, Math.max(130, wanted)));
    editor.style.height = h + 'px';
    _mariaSave('ccadmin.maria.editorH', h);
  };

  sp.addEventListener('dblclick', () => {
    editor.style.height = '';
    _mariaSave('ccadmin.maria.editorH', null);
  });
  sp.addEventListener('keydown', ev => {
    if (ev.key !== 'ArrowUp' && ev.key !== 'ArrowDown') return;
    ev.preventDefault();
    clamp(editor.getBoundingClientRect().height
          + (ev.shiftKey ? 40 : 12) * (ev.key === 'ArrowDown' ? 1 : -1));
  });
  sp.addEventListener('pointerdown', ev => {
    ev.preventDefault();
    const startY = ev.clientY, startH = editor.getBoundingClientRect().height;
    sp.setPointerCapture?.(ev.pointerId);
    sp.classList.add('dragging');
    document.body.classList.add('sql-resizing-y');
    const onMove = e => clamp(startH + e.clientY - startY);
    const onUp = () => {
      sp.removeEventListener('pointermove', onMove);
      sp.removeEventListener('pointerup', onUp);
      sp.removeEventListener('pointercancel', onUp);
      sp.classList.remove('dragging');
      document.body.classList.remove('sql-resizing-y');
    };
    sp.addEventListener('pointermove', onMove);
    sp.addEventListener('pointerup', onUp);
    sp.addEventListener('pointercancel', onUp);
  });
}

/* ── Vertical resizing of the detail sections ─────────────────────────────
   The relations block can run to dozens of rows on a well-connected table,
   and a fixed cap means scrolling a small box inside a large empty pane. */
function startMariaSectionDrag(hs, ev) {
  ev.preventDefault();
  const id   = hs.dataset.hsplit;
  const body = document.querySelector(`[data-sect-body="${id}"]`);
  if (!body) return;
  const startY = ev.clientY;
  const startH = body.getBoundingClientRect().height;

  hs.setPointerCapture?.(ev.pointerId);
  hs.classList.add('dragging');
  document.body.classList.add('sql-resizing-y');

  const onMove = e => _setMariaSectionHeight(id, body, startH + e.clientY - startY);
  const onUp = () => {
    hs.removeEventListener('pointermove', onMove);
    hs.removeEventListener('pointerup', onUp);
    hs.removeEventListener('pointercancel', onUp);
    hs.classList.remove('dragging');
    document.body.classList.remove('sql-resizing-y');
    _mariaSave('ccadmin.maria.sectionH', mariaSectionH);
  };
  hs.addEventListener('pointermove', onMove);
  hs.addEventListener('pointerup', onUp);
  hs.addEventListener('pointercancel', onUp);
}

/** Apply a height, clamped so the rows grid keeps a usable floor. Growing a
 *  section without that ceiling squeezes the grid to nothing — the same
 *  failure the flex chain was fixed for, just reached by dragging. */
function _setMariaSectionHeight(id, body, wanted) {
  const rows = document.querySelector('.sql-rows-section');
  const slack = rows ? Math.max(0, rows.getBoundingClientRect().height - 132) : 0;
  const max = body.getBoundingClientRect().height + slack;
  const h = Math.round(Math.min(max, Math.max(48, wanted)));
  body.style.height = h + 'px';
  mariaSectionH[id] = h;
  _mariaSave('ccadmin.maria.sectionH', mariaSectionH);
}

function resetMariaSectionHeight(id) {
  delete mariaSectionH[id];
  _mariaSave('ccadmin.maria.sectionH', mariaSectionH);
  const body = document.querySelector(`[data-sect-body="${id}"]`);
  if (body) body.style.height = '';
}

/* ── Resizable panes ──────────────────────────────────────────────────────
   Pointer events rather than mouse events, and setPointerCapture, so a fast
   drag that outruns the cursor keeps delivering moves to the splitter instead
   of dropping them on whatever element the pointer crossed. */
function initMariaSplitters() {
  const strip = document.getElementById('mariaPanes');
  if (!strip) return;
  const panes = [document.getElementById('mariaPaneSchemas'),
                 document.getElementById('mariaPaneTables')];

  if (Array.isArray(mariaPaneWidths)) {
    panes.forEach((p, i) => { if (p && mariaPaneWidths[i]) p.style.width = mariaPaneWidths[i] + 'px'; });
  }

  strip.querySelectorAll('.sql-splitter').forEach(sp => {
    const idx  = parseInt(sp.dataset.split, 10);
    const pane = panes[idx];
    if (!pane) return;

    // Reset to the CSS defaults, for when a drag has left the layout unusable.
    sp.addEventListener('dblclick', () => {
      panes.forEach(p => { if (p) p.style.width = ''; });
      mariaPaneWidths = null;
      _mariaSave('ccadmin.maria.paneWidths', null);
    });

    sp.addEventListener('pointerdown', ev => {
      ev.preventDefault();
      const startX = ev.clientX;
      const startW = pane.getBoundingClientRect().width;
      sp.setPointerCapture(ev.pointerId);
      sp.classList.add('dragging');
      document.body.classList.add('sql-resizing');

      const onMove = e => {
        // Floors keep a pane from being dragged to nothing (from which it
        // cannot be dragged back); the ceiling keeps the detail pane usable.
        const max = Math.max(160, strip.getBoundingClientRect().width - 320);
        pane.style.width = Math.min(max, Math.max(140, startW + e.clientX - startX)) + 'px';
      };
      const onUp = () => {
        sp.removeEventListener('pointermove', onMove);
        sp.removeEventListener('pointerup', onUp);
        sp.removeEventListener('pointercancel', onUp);
        sp.classList.remove('dragging');
        document.body.classList.remove('sql-resizing');
        mariaPaneWidths = panes.map(p => p ? Math.round(p.getBoundingClientRect().width) : 0);
        _mariaSave('ccadmin.maria.paneWidths', mariaPaneWidths);
      };
      sp.addEventListener('pointermove', onMove);
      sp.addEventListener('pointerup', onUp);
      sp.addEventListener('pointercancel', onUp);
    });

    // Keyboard: a splitter that can only be dragged is unreachable without a
    // pointer, and these panes are the whole navigation of the screen.
    sp.addEventListener('keydown', ev => {
      const step = ev.shiftKey ? 40 : 12;
      if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return;
      ev.preventDefault();
      const w = pane.getBoundingClientRect().width + (ev.key === 'ArrowRight' ? step : -step);
      const max = Math.max(160, strip.getBoundingClientRect().width - 320);
      pane.style.width = Math.min(max, Math.max(140, w)) + 'px';
      mariaPaneWidths = panes.map(p => p ? Math.round(p.getBoundingClientRect().width) : 0);
      _mariaSave('ccadmin.maria.paneWidths', mariaPaneWidths);
    });
  });
}

function toggleMariaSection(name) {
  mariaCollapsed[name] = !mariaCollapsed[name];
  _mariaSave('ccadmin.maria.collapsed', mariaCollapsed);
  const head = document.querySelector(`[data-sect="${name}"]`);
  const body = document.querySelector(`[data-sect-body="${name}"]`);
  head?.classList.toggle('collapsed', !!mariaCollapsed[name]);
  body?.classList.toggle('d-none', !!mariaCollapsed[name]);
  // The resize handle belongs to the section, so it goes away with it —
  // otherwise a collapsed section leaves a grab handle that resizes nothing.
  document.querySelector(`.sql-hsplit[data-hsplit="${name}"]`)
    ?.classList.toggle('d-none', !!mariaCollapsed[name]);
}

/** Decoded view of one binary column value. */
async function showBlobViewer(qs) {
  const body = document.getElementById('blobViewerBody');
  const dl   = document.getElementById('blobViewerDownload');
  if (dl) dl.href = appUrl('/api/maria/blob?' + qs);
  body.innerHTML = '<div class="text-secondary small p-3">Decoding…</div>';
  const modal = new bootstrap.Modal(document.getElementById('blobViewerModal'));
  modal.show();

  const d = await api('/api/maria/blob/preview?' + qs);
  if (!d || d.error) {
    body.innerHTML = `<div class="alert alert-warning py-2 px-3 small mb-0">`
      + `${esc((d && d.error) || 'could not decode')}</div>`;
    return;
  }

  document.getElementById('blobViewerTitle').textContent =
    `${d.schema}.${d.table}.${d.column}`;

  const header = `<div class="small text-secondary mb-2">
      ${esc(d.label)} · ${_fmtBytes(d.size)}</div>`;

  let main = '';
  if (d.json !== null && d.json !== undefined) {
    // The payload, pretty-printed. This is the thing worth reading; the
    // serialisation framing around it is not.
    main = `<div class="small fw-semibold text-secondary mb-1">JSON payload</div>
      <pre class="bg-body-tertiary p-2 rounded" style="font-size:.75rem;max-height:45vh;
           overflow:auto;white-space:pre-wrap;word-break:break-word;">${
        esc(JSON.stringify(d.json, null, 2))}</pre>`;
  } else if (d.text) {
    main = `<pre class="bg-body-tertiary p-2 rounded" style="font-size:.75rem;
             max-height:45vh;overflow:auto;white-space:pre-wrap;">${esc(d.text)}</pre>`;
  }

  const strings = (d.strings || []).length
    ? `<details ${d.json ? '' : 'open'} class="mt-2">
         <summary class="small text-secondary">Readable strings (${d.strings.length})</summary>
         <pre class="bg-body-tertiary p-2 rounded mt-1" style="font-size:.72rem;
              max-height:30vh;overflow:auto;white-space:pre-wrap;">${
           esc(d.strings.join('\n'))}</pre>
       </details>`
    : '';

  const nothing = (!main && !strings)
    ? '<div class="text-secondary small">Nothing readable in these bytes — '
      + 'download it if you need the raw content.</div>' : '';

  body.innerHTML = header + main + strings + nothing;
}

/** Version + reachability onto the MariaDB node in the rail. */
async function loadMariaHealth() {
  if (!can('maria.read')) return;
  const dot  = document.getElementById('db-maria-dot');
  const meta = document.getElementById('db-maria-meta');
  const d = await api('/api/maria/health');
  const ok = !!(d && d.connected);
  if (dot) dot.className = 'conn-dot ops-db-dot ' + (ok ? 'connected' : 'disconnected');
  if (meta) {
    // Trim MariaDB's long build suffix ("11.8.6-MariaDB-ubu2404"): the rail has
    // ~150px and the distro tag is not what anyone is checking.
    const v = ok ? String(d.version || '').split('-')[0] : '';
    meta.textContent = ok ? (v ? `MariaDB ${v}` : 'connected') : 'not responding';
    meta.title = ok ? `${d.version || ''} · ${d.user || ''} (${d.credential_source || ''})`
                    : (d && d.error) || 'not responding';
  }
  const badge = document.getElementById('mariaServer');
  if (badge) badge.textContent = ok ? `${d.version} · ${d.host}:${d.port}` : '';

  // The account button: always offered once a CC is connected, whether or
  // not the connection succeeded — a wrong account is exactly the reason
  // someone would open it, and hiding the fix behind a working connection
  // would be backwards.
  const acctBtn = document.getElementById('mariaAccountBtn');
  const acctLabel = document.getElementById('mariaAccountLabel');
  if (acctBtn) acctBtn.classList.toggle('d-none', !d || !d.host);
  if (acctLabel) acctLabel.textContent = (d && d.user) ? d.user : 'account';
}

function _mariaError(id, msg) {
  const box = document.getElementById(id);
  if (!box) return;
  box.classList.toggle('d-none', !msg);
  box.textContent = msg || '';
}

/** The account modal. Discovery (modules/maria/credentials.py) tries hard,
 *  but two lab CCs already needed two entirely different conventions to find
 *  the right one — an operator who knows better needs to be able to just say
 *  so, without a redeploy. Reads /api/maria/credentials fresh on every open
 *  rather than reusing loadMariaHealth()'s payload, because that endpoint
 *  also reports whether an override is set, which the health check does not. */
async function openMariaCredentialsModal() {
  document.querySelector('.rt-modal-overlay.rt-mariacreds')?.remove();
  const wrap = document.createElement('div');
  wrap.className = 'rt-modal-overlay rt-mariacreds';
  wrap.innerHTML = `<div class="rt-modal" style="max-width:420px;">
      <div class="rt-modal-title"><i class="bi bi-key me-1"></i>MariaDB account</div>
      <div class="rt-modal-body">
        <div class="small text-secondary mb-3" data-role="status">Loading…</div>
        <div class="mb-2">
          <label class="form-label small mb-1">Username</label>
          <input type="text" class="form-control form-control-sm" data-role="user"
                 autocomplete="off">
        </div>
        <div class="mb-1">
          <label class="form-label small mb-1">Password</label>
          <input type="password" class="form-control form-control-sm" data-role="pass"
                 autocomplete="new-password">
        </div>
        <div class="form-text mb-0" style="font-size:.7rem;">
          Saved for this CC only, and used the next time this tool connects —
          no restart needed. Leave both fields as shown and click Save to
          re-save what discovery already found; use Clear to go back to
          automatic discovery.
        </div>
      </div>
      <div class="rt-modal-actions">
        <button class="btn btn-sm btn-outline-danger me-auto" data-act="clear">Clear override</button>
        <button class="btn btn-sm btn-outline-secondary" data-act="cancel">Cancel</button>
        <button class="btn btn-sm btn-primary" data-act="save">Save</button>
      </div>
    </div>`;
  document.body.appendChild(wrap);

  const done = () => { wrap.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = e => { if (e.key === 'Escape') done(); };
  document.addEventListener('keydown', onKey);
  wrap.addEventListener('click', e => { if (e.target === wrap) done(); });
  wrap.querySelector('[data-act="cancel"]').addEventListener('click', done);

  const status = wrap.querySelector('[data-role="status"]');
  const userBox = wrap.querySelector('[data-role="user"]');
  const passBox = wrap.querySelector('[data-role="pass"]');
  let host = '';

  const d = await api('/api/maria/credentials');
  if (!d || !d.host) {
    status.textContent = (d && d.error) || 'no CC is connected — connect to one first.';
    wrap.querySelector('[data-act="save"]').disabled = true;
    wrap.querySelector('[data-act="clear"]').disabled = true;
    return;
  }
  host = d.host;
  userBox.value = d.effective_user || '';
  status.innerHTML = d.override.set
    ? `<b>${esc(host)}</b> — using an operator-set override (<code>${esc(d.override.user)}</code>).`
    : `<b>${esc(host)}</b> — currently using <code>${esc(d.effective_user)}</code>, `
      + `${esc(d.effective_source)}.`;
  wrap.querySelector('[data-act="clear"]').classList.toggle('d-none', !d.override.set);

  wrap.querySelector('[data-act="save"]').addEventListener('click', async () => {
    const user = userBox.value.trim();
    const password = passBox.value;
    if (!user || !password) {
      showToast('Both a username and a password are required', 'bg-warning');
      return;
    }
    const r = await api('/api/maria/credentials', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user, password }),
    });
    if (!r || r.error) {
      showToast((r && r.error) || 'could not save the override', 'bg-danger');
      return;
    }
    showToast(`MariaDB account for ${host} set to ${user}`, 'bg-success');
    done();
    loadMariaHealth();
    if (currentView === 'maria' || currentView === 'mariaquery') loadMariaSchemas();
  });

  wrap.querySelector('[data-act="clear"]').addEventListener('click', async () => {
    const r = await api('/api/maria/credentials', { method: 'DELETE' });
    if (!r || r.error) {
      showToast((r && r.error) || 'could not clear the override', 'bg-danger');
      return;
    }
    showToast('MariaDB account override cleared — back to automatic discovery', 'bg-info');
    done();
    loadMariaHealth();
    if (currentView === 'maria' || currentView === 'mariaquery') loadMariaSchemas();
  });
}

async function loadMariaSchemas() {
  const showSystem = !!document.getElementById('mariaShowSystem')?.checked;
  const pane = document.getElementById('mariaSchemas');
  if (pane) pane.innerHTML = '<div class="text-secondary small p-3">Loading…</div>';

  const d = await api(`/api/maria/schemas?include_system=${showSystem}`);
  if (!d || d.error) {
    _mariaError('mariaError', (d && d.error) || 'could not list schemas');
    if (pane) pane.innerHTML = '<div class="text-secondary small p-3">—</div>';
    return;
  }
  _mariaError('mariaError', '');
  mariaSchemas = d.schemas || [];
  renderMariaSchemas();
  _fillMariaQuerySchemas();

  // "Refresh" means refresh what is actually on screen, not just this pane.
  // Without this, a table's row-estimate (InnoDB's own approximation, only
  // updated when MariaDB next runs ANALYZE) could look permanently stuck at
  // whatever it read the moment the schema was first opened, and an already-
  // open table's own sample rows would go stale the moment something changed
  // the data behind them — confirmed live: a table written to right after
  // being opened kept showing its old row count no matter how many times
  // Refresh was clicked, because Refresh never actually asked about it again.
  if (mariaSchema) await _refreshMariaTableList(mariaSchema);
  if (mariaTable) await selectMariaTable(mariaTable);
}

/** Re-fetch the table list for `name` WITHOUT selectMariaSchema's side
 *  effects (clearing the open table, resetting the search box) — those are
 *  right for a deliberate schema change, wrong for a background refresh of
 *  the schema that is already open. */
async function _refreshMariaTableList(name) {
  const d = await api(`/api/maria/tables?schema=${encodeURIComponent(name)}`);
  if (mariaSchema !== name) return;   // superseded by a schema change meanwhile
  if (!d || d.error) return;          // keep showing the last good list
  mariaTableList = d.tables || [];
  renderMariaTables();
}

function renderMariaSchemas() {
  const pane = document.getElementById('mariaSchemas');
  if (!pane) return;
  if (!mariaSchemas.length) {
    pane.innerHTML = '<div class="text-secondary small p-3">No schemas.</div>';
    return;
  }
  // Identifiers go in a data- attribute and are read back through the DOM,
  // never interpolated into an onclick. esc() escapes < > & but NOT quotes, so
  // a table or schema name containing an apostrophe — which MySQL permits in a
  // backtick-quoted identifier — would otherwise close the attribute and run
  // as script. Nothing on this CC is named that way today; the point is that
  // the names come from the database rather than from us, so the rendering
  // must not depend on what they happen to contain.
  pane.innerHTML = mariaSchemas.map(s => `
    <button class="sql-item ${s.name === mariaSchema ? 'active' : ''}"
            data-schema="${esc(s.name)}">
      <div class="d-flex align-items-center gap-2">
        <span class="fw-semibold">${esc(s.title)}</span>
        ${s.catalogued ? '' : '<span class="badge bg-warning-subtle text-warning-emphasis" '
          + 'style="font-size:.6rem;" title="Not in the curated catalog — worth adding">new</span>'}
        <span class="ms-auto text-secondary" style="font-size:.68rem;">
          ${s.tables} tbl · ${s.size_mb} MB
        </span>
      </div>
      ${s.description
        ? `<div class="sql-desc">${esc(s.description)}</div>`
        : `<div class="sql-desc font-monospace">${esc(s.name)}</div>`}
    </button>`).join('');
}

async function selectMariaSchema(name) {
  mariaSchema = name;
  mariaTable  = '';
  // Drop the previous schema's tables NOW rather than leaving them on screen
  // under the new schema's heading while the request is in flight — that reads
  // as "these are quartz's tables" when they are still vision_ng's.
  mariaTableList = [];
  renderMariaSchemas();
  document.getElementById('mariaTablesTitle').textContent = `Tables — ${name}`;
  document.getElementById('mariaDetailTitle').textContent = 'Table';
  document.getElementById('mariaDetail').innerHTML =
    '<div class="text-secondary small p-3">Pick a table.</div>';
  const search = document.getElementById('mariaTableSearch');
  if (search) search.value = '';        // a filter from the last schema is not meant for this one

  const pane = document.getElementById('mariaTables');
  pane.innerHTML = '<div class="text-secondary small p-3">Loading…</div>';
  const d = await api(`/api/maria/tables?schema=${encodeURIComponent(name)}`);

  // Two clicks in quick succession: the first response can land after the
  // second, and without this the pane would end up showing the schema the user
  // did NOT select — with a heading naming the one they did.
  if (mariaSchema !== name) return;

  if (!d || d.error) {
    pane.innerHTML = `<div class="text-danger small p-3">${esc((d && d.error) || 'failed')}</div>`;
    return;
  }
  mariaTableList = d.tables || [];
  renderMariaTables();
}

function renderMariaTables() {
  const pane = document.getElementById('mariaTables');
  if (!pane) return;
  const q = (document.getElementById('mariaTableSearch')?.value || '').toLowerCase();
  const rows = mariaTableList.filter(t => !q || t.name.toLowerCase().includes(q));
  if (!rows.length) {
    pane.innerHTML = '<div class="text-secondary small p-3">No matching tables.</div>';
    return;
  }
  pane.innerHTML = rows.map(t => `
    <button class="sql-item ${t.name === mariaTable ? 'active' : ''}"
            data-table="${esc(t.name)}">
      <div class="d-flex align-items-center gap-2">
        <span class="font-monospace" style="font-size:.75rem;">${esc(t.name)}</span>
        <span class="ms-auto text-secondary" style="font-size:.68rem;"
              title="InnoDB row counts are estimates">~${t.row_estimate} rows</span>
      </div>
      ${t.comment ? `<div class="sql-desc">${esc(t.comment)}</div>` : ''}
    </button>`).join('');
}

async function selectMariaTable(name) {
  mariaTable = name;
  // The ticked relations belong to the table being left, and their indices
  // mean something different in the next table's payload.
  mariaJoinSel.clear();
  renderMariaTables();
  document.getElementById('mariaDetailTitle').textContent = `${mariaSchema}.${name}`;
  const pane = document.getElementById('mariaDetail');
  pane.innerHTML = '<div class="text-secondary small p-3">Loading…</div>';

  const qs = `schema=${encodeURIComponent(mariaSchema)}&table=${encodeURIComponent(name)}`;
  const [cols, sample, keys] = await Promise.all([
    api(`/api/maria/columns?${qs}`),
    api(`/api/maria/sample?${qs}&size=25`),
    api(`/api/maria/keys?${qs}`),
  ]);

  if (mariaTable !== name) return;      // superseded by a later click

  if (cols && cols.error) {
    pane.innerHTML = `<div class="text-danger small p-3">${esc(cols.error)}</div>`;
    return;
  }

  mariaColumns   = cols.columns || [];
  mariaSample    = (sample && !sample.error) ? sample : null;
  mariaRelations = (keys && !keys.error) ? keys : null;
  renderMariaDetail(sample && sample.error ? sample.error : '');
}

/** The detail pane: columns, relations, rows. Split from selectMariaTable so
 *  hiding a column or editing a cell can redraw without re-fetching. */
function renderMariaDetail(sampleError) {
  const pane = document.getElementById('mariaDetail');
  if (!pane) return;

  const rowsHtml = sampleError
    ? `<div class="text-danger small p-2">${esc(sampleError)}</div>`
    : _mariaTable(mariaSample?.columns || [], mariaSample?.rows || [],
                  mariaSample?.truncated,
                  {schema: mariaSchema, table: mariaTable,
                   primaryKey: mariaSample?.primary_key || [],
                   blobColumns: mariaSample?.blob_columns || [],
                   hidden: _mariaHiddenSet(), editable: true});

  const hidden = _mariaHiddenSet();
  const total  = (mariaSample?.columns || []).length;
  const shown  = total - [...hidden].filter(c => (mariaSample?.columns || []).includes(c)).length;

  pane.innerHTML = `
    <div class="sql-detail-section">
      ${_mariaHead('columns', `Columns (${mariaColumns.length})`)}
      <div class="sql-detail-body-section ${mariaCollapsed.columns ? 'd-none' : ''}"
           data-sect-body="columns"${_mariaSectionStyle('columns')}>${_mariaColumnsTable()}</div>
    </div>
    ${_mariaHSplit('columns')}

    <div class="sql-detail-section">
      ${_mariaHead('relations', _mariaRelationsLabel())}
      <div class="sql-detail-body-section ${mariaCollapsed.relations ? 'd-none' : ''}"
           data-sect-body="relations"${_mariaSectionStyle('relations')}>${_mariaRelations()}</div>
    </div>
    ${_mariaHSplit('relations')}

    <div class="sql-rows-section">
      <div class="sql-detail-head">
        <span>First rows</span>
        <span class="text-secondary" style="text-transform:none;font-weight:500;">
          ${shown === total ? `${total} columns` : `${shown} of ${total} columns`}
        </span>
        <button class="btn btn-sm btn-outline-secondary ms-auto py-0 px-2"
                data-maria-cols="1" style="font-size:.7rem;text-transform:none;"
                title="Choose which columns to show">
          <i class="bi bi-eye me-1"></i>Columns
        </button>
        <button class="btn btn-sm btn-outline-secondary py-0 px-2"
                data-maria-popout="1" style="font-size:.7rem;text-transform:none;"
                title="Open these rows in a separate window">
          <i class="bi bi-box-arrow-up-right"></i>
        </button>
      </div>
      <div class="sql-grid-scroll">${rowsHtml}</div>
    </div>`;
  syncSqlPopout();
}

/** A dragged height for a section, if it has one. */
function _mariaSectionStyle(id) {
  const h = mariaSectionH[id];
  return (h && !mariaCollapsed[id]) ? ` style="height:${h}px;"` : '';
}

/** The grab handle under a section. Pointless on a collapsed one — there is
 *  nothing to resize — so it is simply not rendered. */
function _mariaHSplit(id) {
  if (mariaCollapsed[id]) return '';
  return `<div class="sql-hsplit" data-hsplit="${id}" role="separator"
               tabindex="0" aria-orientation="horizontal"
               title="Drag to resize · double-click to reset"></div>`;
}

function _mariaHead(id, label) {
  return `<button class="sql-detail-head ${mariaCollapsed[id] ? 'collapsed' : ''}"
                  data-sect="${id}" aria-expanded="${!mariaCollapsed[id]}">
            <span>${esc(label)}</span>
            <i class="bi bi-chevron-down ops-caret"></i>
          </button>`;
}

function _mariaColumnsTable() {
  if (!mariaColumns.length)
    return '<div class="text-secondary small p-2">No columns.</div>';
  const body = mariaColumns.map(c => `
    <tr>
      <td class="font-monospace">${esc(c.name)}</td>
      <td class="text-secondary">${esc(c.type)}</td>
      <td>${_mariaKeyBadge(c)}</td>
      <td class="text-secondary">${c.nullable === 'YES' ? 'null' : ''}</td>
    </tr>`).join('');
  return `<table class="table table-sm table-hover mb-0" style="font-size:.74rem;">
      <thead class="table-light"><tr>
        <th>Name</th><th>Type</th><th>Key</th><th></th>
      </tr></thead><tbody>${body}</tbody></table>`;
}

/** A key badge that says what the key IS, not just that there is one.
 *  "MUL" alone is the least informative thing information_schema reports —
 *  it means "first column of some non-unique index" and never says which. */
function _mariaKeyBadge(col) {
  if (!col.key_type) return '';
  const idx = (mariaRelations?.indexes || [])
    .filter(i => i.columns.includes(col.name));
  const tip = idx.length
    ? idx.map(i => `${i.name} (${i.columns.join(', ')})`).join('\n')
    : {PRI: 'Primary key', UNI: 'Unique index', MUL: 'Indexed, non-unique'}[col.key_type] || '';
  const tone = col.key_type === 'PRI' ? 'primary' : 'secondary';
  return `<span class="badge bg-${tone}-subtle text-${tone}-emphasis"
                style="font-size:.6rem;" title="${esc(tip)}">${esc(col.key_type)}</span>`;
}

function _mariaRelationsLabel() {
  const r = mariaRelations;
  if (!r) return 'Keys & relations';
  const n = (r.outbound?.length || 0) + (r.inbound?.length || 0);
  return n ? `Keys & relations (${n} declared)` : 'Keys & relations';
}

function _mariaRelations() {
  const r = mariaRelations;
  if (!r) return '<div class="text-secondary small p-2">—</div>';

  const idx = (r.indexes || []).map(i => `
    <div class="sql-rel-row">
      <span class="badge bg-${i.primary ? 'primary' : 'secondary'}-subtle
                   text-${i.primary ? 'primary' : 'secondary'}-emphasis"
            style="font-size:.6rem;">${i.primary ? 'PRIMARY' : (i.unique ? 'UNIQUE' : 'INDEX')}</span>
      <span class="ms-1 text-secondary">${esc(i.primary ? '' : i.name)}</span>
      <span class="ms-1">${i.columns.map(c => `<span class="sql-chip">${esc(c)}</span>`).join(' + ')}</span>
    </div>`).join('');

  const link = (schema, table, label) =>
    schema === mariaSchema
      ? `<button class="sql-chip sql-chip-link" data-goto-table="${esc(table)}"
                 title="Open ${esc(table)}">${esc(label)}</button>`
      : `<span class="sql-chip">${esc(schema)}.${esc(label)}</span>`;

  // A tick box per declared relation: these ARE join conditions, so selecting
  // them is the whole of composing the join.
  const tick = (id) => `<input type="checkbox" class="sql-rel-tick"
      data-joinsel="${id}" ${mariaJoinSel.has(id) ? 'checked' : ''}
      title="Include this relation in a join query">`;

  const out = (r.outbound || []).map((f, i) => `
    <div class="sql-rel-row">
      ${tick('out:' + i)}
      <i class="bi bi-arrow-right-short text-primary"></i>
      ${f.columns.map(c => `<span class="sql-chip">${esc(c)}</span>`).join(' + ')}
      <span class="text-secondary mx-1">references</span>
      ${link(f.ref_schema, f.ref_table, f.ref_table)}
      <span class="text-secondary">.</span>
      ${f.ref_columns.map(c => `<span class="sql-chip">${esc(c)}</span>`).join(' + ')}
    </div>`).join('');

  const inb = (r.inbound || []).map((f, i) => `
    <div class="sql-rel-row">
      ${tick('in:' + i)}
      <i class="bi bi-arrow-left-short text-success"></i>
      ${link(f.from_schema, f.from_table, f.from_table)}
      <span class="text-secondary">.</span>
      ${f.columns.map(c => `<span class="sql-chip">${esc(c)}</span>`).join(' + ')}
      <span class="text-secondary mx-1">references</span>
      ${f.ref_columns.map(c => `<span class="sql-chip">${esc(c)}</span>`).join(' + ')}
    </div>`).join('');

  // The inferred half. Kept visually and verbally distinct from the declared
  // half above: this is a guess from column names, and an engineer acting on
  // it needs to know that before they treat it as the data model.
  const cand = (r.candidates || []).map(c => `
    <div class="sql-rel-row">
      <span class="sql-chip">${esc(c.column)}</span>
      <span class="text-secondary mx-1">also in ${c.count} table${c.count === 1 ? '' : 's'}:</span>
      ${c.tables.map(t => link(mariaSchema, t, t)).join(' ')}
      ${c.truncated ? '<span class="text-secondary"> …</span>' : ''}
    </div>`).join('');

  const section = (title, html, note) => html
    ? `<div class="px-2 pt-2 pb-1 small fw-semibold text-secondary">${esc(title)}</div>
       ${note ? `<div class="px-2 pb-1 text-secondary" style="font-size:10.5px;">${esc(note)}</div>` : ''}
       ${html}` : '';

  const body = section('Indexes', idx)
    + section('References out', out)
    + section('Referenced by', inb)
    + section('Possibly related', cand,
        'Matched on column name, not on a declared constraint — a strong hint '
        + 'about where to look next, not a guarantee that the values line up.');

  if (!body) return '<div class="text-secondary small p-2">No keys on this table.</div>';

  const n = mariaJoinSel.size;
  const bar = (out.length || inb.length)
    ? `<div class="sql-join-bar">
         <i class="bi bi-diagram-2 me-1"></i>
         <span>${n ? `${n} relation${n === 1 ? '' : 's'} selected` : 'Tick relations to build a join'}</span>
         <button class="btn btn-sm btn-primary py-0 px-2 ms-auto" data-join-build="1"
                 ${n ? '' : 'disabled'} style="font-size:.7rem;">
           <i class="bi bi-hammer me-1"></i>Build join query
         </button>
         ${n ? `<button class="btn btn-sm btn-link py-0 px-1 text-secondary"
                  data-join-clear="1" style="font-size:.7rem;">Clear</button>` : ''}
       </div>` : '';

  const none = (!out.length && !inb.length)
    ? `<div class="px-2 py-1 text-secondary" style="font-size:10.5px;">
         This schema declares no FOREIGN KEY constraints on this table, so the
         relationships below are inferred rather than read from the catalog.
       </div>` : '';
  return bar + none + body;
}

/* ── Join builder ─────────────────────────────────────────────────────────
   The relations panel already holds the join conditions; ticking them is the
   whole of composing the query. The output goes to the SQL screen as text the
   engineer can read and edit, rather than running something they never saw —
   this is a debugging tool, and a query you cannot inspect is not a finding
   you can defend. */

/** Backtick-quote one identifier. Names come from the database, so a stray
 *  backtick in one must not be able to end the quote. */
function _q(name) { return '`' + String(name).replace(/`/g, '') + '`'; }

/** A readable, unique alias per joined table. The table's own name where it
 *  can be, because `password.row_id` reads and a bare `t3.row_id` does not. */
function _mariaAlias(name, taken) {
  let base = String(name).replace(/[^A-Za-z0-9_]/g, '_') || 't';
  let alias = base, n = 2;
  while (taken.has(alias)) alias = `${base}_${n++}`;
  taken.add(alias);
  return alias;
}

/** The relations currently ticked, resolved against the payload. */
function _mariaSelectedJoins() {
  const r = mariaRelations || {};
  const picked = [];
  for (const id of mariaJoinSel) {
    const [side, idx] = id.split(':');
    const f = (side === 'out' ? r.outbound : r.inbound)?.[Number(idx)];
    if (f) picked.push({ side, f });
  }
  return picked;
}

/** Build the SELECT. `opts` = {joinType, where[], limit, groups, hidden}.
 *
 * `groups` is _mariaJoinColumnCatalog()'s output — one entry per table in
 * the query, base first, then one per selected join in the same order
 * _mariaSelectedJoins() produces them (both iterate the same Set, so the
 * order always lines up). `hidden` is a Set of "alias.column" keys the
 * column picker turned off.
 *
 * Every table's FULL column list is selected by default now, not just the
 * join key — a join exists to answer questions about the OTHER table, and
 * `user_settings.name` is a far more likely thing to want than
 * `user_settings.row_id` alone. `SELECT a.*, b.*` was ruled out for the same
 * reason as before (the disambiguated names would depend on the driver, not
 * the query), so each column is still selected and aliased explicitly —
 * there are just more of them, and the picker is what keeps a 26+10-column
 * join from being unreadable by default.
 */
function _buildMariaJoinSql(opts) {
  const taken = new Set();
  const baseAlias = _mariaAlias(mariaTable, taken);
  const jt = opts.joinType === 'INNER' ? 'INNER JOIN' : 'LEFT JOIN';
  const hidden = opts.hidden || new Set();
  const groups = opts.groups || [];

  const baseCols = (groups[0]?.columns || mariaColumns.map(c => c.name))
    .filter(c => !hidden.has(`${baseAlias}.${c}`));
  // A query with no columns at all is not one anyone meant to run — if every
  // base column got hidden, fall back to *, rather than produce empty SQL.
  const selects = baseCols.length
    ? baseCols.map(c => `${_q(baseAlias)}.${_q(c)}`)
    : [`${_q(baseAlias)}.*`];
  const joins = [];

  let gi = 1;
  for (const { side, f } of _mariaSelectedJoins()) {
    // Outbound: we hold the foreign key and point at their key.
    // Inbound:  they hold the foreign key and point back at ours.
    const otherSchema = side === 'out' ? f.ref_schema : f.from_schema;
    const otherTable  = side === 'out' ? f.ref_table  : f.from_table;
    const alias = _mariaAlias(otherTable, taken);

    const on = f.columns.map((c, i) => {
      const mine  = side === 'out' ? c : f.ref_columns[i];
      const their = side === 'out' ? f.ref_columns[i] : c;
      return `${_q(baseAlias)}.${_q(mine)} = ${_q(alias)}.${_q(their)}`;
    }).join(' AND ');

    joins.push(`  ${jt} ${_q(otherSchema)}.${_q(otherTable)} AS ${_q(alias)}\n`
             + `    ON ${on}`);

    // Unlike the base table, a joined table selecting NOTHING is a legitimate
    // choice — "does a match exist" without wanting any of its columns — so
    // no *-fallback here.
    const group = groups[gi++];
    const allCols = group?.columns || (side === 'out' ? f.ref_columns : f.columns);
    for (const c of allCols.filter(c => !hidden.has(`${alias}.${c}`))) {
      selects.push(`${_q(alias)}.${_q(c)} AS ${_q(alias + '__' + c)}`);
    }
  }

  const where = (opts.where || []).filter(w => w.sql).map(w => w.sql);
  const lines = [
    'SELECT ' + selects.join(',\n       '),
    `  FROM ${_q(mariaSchema)}.${_q(mariaTable)} AS ${_q(baseAlias)}`,
    ...joins,
  ];
  if (where.length) lines.push(' WHERE ' + where.join('\n   AND '));
  lines.push(` LIMIT ${Math.max(1, parseInt(opts.limit, 10) || 200)}`);
  return lines.join('\n');
}

/** Quote a literal for the generated SQL. The endpoint is read-only and
 *  rejects anything that is not a SELECT, so this is about the query being
 *  CORRECT — an unescaped apostrophe in a name is a syntax error, not a
 *  vulnerability — but doubling quotes is the right habit either way. */
function _sqlLit(v) { return `'${String(v).replace(/'/g, "''")}'`; }

function _mariaWhereSql(col, op, val) {
  if (!col) return '';
  if (op === 'IS NULL' || op === 'IS NOT NULL') return `${col} ${op}`;
  if (op === 'IN') {
    const parts = String(val).split(',').map(s => s.trim()).filter(Boolean);
    return parts.length ? `${col} IN (${parts.map(_sqlLit).join(', ')})` : '';
  }
  if (val === '') return '';
  return `${col} ${op} ${_sqlLit(val)}`;
}

/* Column lists for tables we have joined to, kept for the session. The join
   builder needs every column of each joined table, not just the join keys —
   filtering on `user_settings.name` is a far more likely question than
   filtering on the foreign key you just joined through. */
const _mariaColCache = {};

async function _mariaJoinColumnCatalog() {
  const taken = new Set();
  const baseAlias = _mariaAlias(mariaTable, taken);
  const groups = [{ alias: baseAlias, table: mariaTable,
                    columns: mariaColumns.map(c => c.name) }];

  for (const { side, f } of _mariaSelectedJoins()) {
    const schema = side === 'out' ? f.ref_schema : f.from_schema;
    const table  = side === 'out' ? f.ref_table  : f.from_table;
    const alias  = _mariaAlias(table, taken);
    const key = `${schema}.${table}`;

    if (!_mariaColCache[key]) {
      const d = await api(`/api/maria/columns?schema=${encodeURIComponent(schema)}`
                        + `&table=${encodeURIComponent(table)}`);
      // A failed lookup falls back to the join columns rather than dropping
      // the table from the picker entirely — some filter beats none.
      _mariaColCache[key] = (d && !d.error && d.columns)
        ? d.columns.map(c => c.name)
        : (side === 'out' ? f.ref_columns : f.columns);
    }
    groups.push({ alias, table, columns: _mariaColCache[key] });
  }
  return groups;
}

/** <optgroup>s so a 26-column base table and a 10-column joined one stay
 *  distinguishable in one dropdown. */
function _mariaColumnOptions(groups) {
  return groups.map(g => `<optgroup label="${esc(g.alias)}">`
    + g.columns.map(c => {
        const v = `${_q(g.alias)}.${_q(c)}`;
        return `<option value="${esc(v)}">${esc(g.alias)}.${esc(c)}</option>`;
      }).join('')
    + '</optgroup>').join('');
}

/** Distinct values for a qualified column, taken from the rows already loaded
 *  in the sample pane. Only the base table has rows here, so a joined table's
 *  column simply gets no suggestions rather than a wrong set. */
function _mariaSampleValues(qualified, baseAlias) {
  const prefix = `${_q(baseAlias)}.`;
  if (!qualified.startsWith(prefix)) return [];
  const col = qualified.slice(prefix.length).replace(/^`|`$/g, '');
  const seen = new Set();
  for (const r of (mariaSample?.rows || [])) {
    const v = r[col];
    if (v === null || v === undefined) continue;
    if (typeof v === 'object') continue;      // a blob marker is not a value
    seen.add(String(v));
    if (seen.size >= 40) break;
  }
  return [...seen].sort();
}

async function openMariaJoinBuilder() {
  if (!mariaJoinSel.size) return;
  document.querySelector('.rt-modal-overlay.rt-mariajoin')?.remove();

  // Fetched before the dialog is drawn so the filter dropdown is complete the
  // moment it appears — a picker that fills in a moment later is one people
  // open, see the wrong list in, and close.
  const colGroups = await _mariaJoinColumnCatalog();

  const wrap = document.createElement('div');
  wrap.className = 'rt-modal-overlay rt-mariajoin';
  const picked = _mariaSelectedJoins();
  const fanOut = picked.some(p => p.side === 'in');

  // `width`, not max-width: .rt-modal sets width:min(440px,92vw), which a
  // max-width can never widen. And white-space:normal because .rt-modal-body
  // is pre-line for plain-text messages, which mangles real markup.
  wrap.innerHTML = `<div class="rt-modal" style="width:min(900px,94vw);">
      <div class="rt-modal-title"><i class="bi bi-diagram-2 me-1"></i>Join from
        <span class="font-monospace">${esc(mariaSchema)}.${esc(mariaTable)}</span></div>
      <div class="rt-modal-body" style="white-space:normal;">
        <div class="d-flex align-items-center gap-2 mb-2 flex-wrap">
          <span class="small text-secondary">${picked.length} relation${picked.length === 1 ? '' : 's'}</span>
          <select class="form-select form-select-sm mj-jointype" style="width:120px;font-size:.75rem;">
            <option value="LEFT">LEFT JOIN</option>
            <option value="INNER">INNER JOIN</option>
          </select>
          <label class="small text-secondary mb-0 ms-2">Limit</label>
          <input type="number" min="1" max="10000" value="200"
                 class="form-control form-control-sm mj-limit" style="width:90px;font-size:.75rem;">
        </div>

        ${fanOut ? `<div class="alert alert-warning py-1 px-2 small mb-2">
            A <b>Referenced by</b> relation is one-to-many: each
            <span class="font-monospace">${esc(mariaTable)}</span> row repeats once per
            matching child row, so counts over the base table will be inflated.
          </div>` : ''}

        <!-- The filter block is a titled panel with a row already in it, not a
             button someone has to find first: adding a WHERE is the common
             case, so it should cost nothing to discover. -->
        <div class="mj-filters mb-2">
          <div class="mj-filters-head">
            <i class="bi bi-funnel me-1"></i>
            <span>Filters</span>
            <span class="text-secondary fw-normal ms-1" style="text-transform:none;">
              — combined with AND; leave the value empty to ignore a row</span>
            <button class="btn btn-sm btn-outline-primary py-0 px-2 ms-auto mj-addwhere"
                    style="font-size:.72rem;"><i class="bi bi-plus-lg me-1"></i>Add condition</button>
          </div>
          <div class="mj-where"></div>
        </div>

        <div class="d-flex align-items-center gap-2 mb-1">
          <span class="small fw-semibold text-secondary">Generated SQL — edit freely</span>
          <button class="btn btn-sm btn-outline-secondary py-0 px-2 ms-auto mj-cols"
                  style="font-size:.7rem;" title="Choose which columns to include, per table">
            <i class="bi bi-eye me-1"></i>Columns
          </button>
        </div>
        <!-- wrap=off: soft-wrapping breaks the indentation that makes a join
             readable, which is the whole point of showing it. -->
        <textarea class="form-control font-monospace mj-sql" rows="12" wrap="off"
                  spellcheck="false" style="font-size:.74rem;white-space:pre;
                  overflow-x:auto;"></textarea>
        <div class="form-text" style="font-size:.7rem;">
          Read-only: it runs through the same SELECT-only guard as the SQL screen.
        </div>
      </div>
      <div class="rt-modal-actions">
        <button class="btn btn-sm btn-primary" data-act="run">
          <i class="bi bi-play-fill me-1"></i>Run in SQL Query</button>
        <button class="btn btn-sm btn-outline-secondary" data-act="copy">Copy</button>
        <button class="btn btn-sm btn-outline-secondary" data-act="close">Close</button>
      </div>
    </div>`;
  document.body.appendChild(wrap);

  const sqlBox = wrap.querySelector('.mj-sql');
  let touched = false;                 // stop regenerating over a hand-edit
  sqlBox.addEventListener('input', () => { touched = true; });

  // Which "alias.column" pairs the picker turned off — transient to this one
  // dialog, the same way mariaJoinSel itself does not survive closing it.
  const hiddenJoinCols = new Set();

  const regen = () => {
    if (touched) return;
    sqlBox.value = _buildMariaJoinSql({
      joinType: wrap.querySelector('.mj-jointype').value,
      limit: wrap.querySelector('.mj-limit').value,
      groups: colGroups,
      hidden: hiddenJoinCols,
      where: [...wrap.querySelectorAll('.mj-wrow')].map(row => ({
        sql: _mariaWhereSql(row.querySelector('.mj-col').value,
                            row.querySelector('.mj-op').value,
                            row.querySelector('.mj-val')?.value ?? ''),
      })),
    });
  };

  let listSeq = 0;
  const addWhere = () => {
    const listId = `mj-vals-${Date.now()}-${listSeq++}`;
    const row = document.createElement('div');
    row.className = 'mj-wrow d-flex align-items-center gap-1 mb-1';
    row.innerHTML = `
      <select class="form-select form-select-sm mj-col font-monospace"
              style="font-size:.72rem;flex:1 1 auto;min-width:0;">
        ${_mariaColumnOptions(colGroups)}
      </select>
      <select class="form-select form-select-sm mj-op" style="width:120px;font-size:.72rem;">
        ${['=', '!=', 'LIKE', 'IN', '>', '<', '>=', '<=', 'IS NULL', 'IS NOT NULL']
          .map(o => `<option>${o}</option>`).join('')}
      </select>
      <input type="text" class="form-control form-control-sm mj-val" placeholder="value"
             list="${listId}" style="font-size:.72rem;">
      <datalist id="${listId}"></datalist>
      <button class="btn btn-sm btn-link text-secondary py-0 px-1 mj-del"
              title="Remove"><i class="bi bi-x-lg"></i></button>`;
    wrap.querySelector('.mj-where').appendChild(row);

    // Suggest the values actually present in the rows on screen. Saves both
    // the typing and the guess about spelling — `MsspUser` is not something
    // anyone gets right from memory — while staying a suggestion, so a value
    // that is not in the sample can still be typed.
    const colSel = row.querySelector('.mj-col');
    const syncList = () => {
      const dl = row.querySelector('datalist');
      dl.innerHTML = _mariaSampleValues(colSel.value, colGroups[0].alias)
        .map(v => `<option value="${esc(v)}"></option>`).join('');
    };
    colSel.addEventListener('change', syncList);
    syncList();
    // IS NULL takes no value — hiding the box stops it looking like the value
    // was ignored.
    const op = row.querySelector('.mj-op');
    const syncVal = () => {
      const none = op.value === 'IS NULL' || op.value === 'IS NOT NULL';
      row.querySelector('.mj-val').classList.toggle('d-none', none);
      row.querySelector('.mj-val').placeholder = op.value === 'IN' ? 'a, b, c' : 'value';
    };
    op.addEventListener('change', syncVal);
    syncVal();
  };

  wrap.addEventListener('input', regen);
  wrap.addEventListener('change', regen);
  wrap.addEventListener('click', async ev => {
    if (ev.target.closest('.mj-cols')) {
      // Flat, but built table-by-table in order — base first, then each join
      // in the order they were ticked — so it reads as grouped without
      // needing the picker itself to know about groups at all.
      const columns = colGroups.flatMap(g => g.columns.map(c => `${g.alias}.${c}`));
      _openColumnPicker({
        title: `Columns — ${mariaTable} join`,
        columns, locked: [], hidden: hiddenJoinCols,
        onChange: (hidden) => {
          hiddenJoinCols.clear();
          for (const h of hidden) hiddenJoinCols.add(h);
          regen();
        },
      });
      return;
    }
    if (ev.target.closest('.mj-addwhere')) { addWhere(); regen(); return; }
    if (ev.target.closest('.mj-del')) {
      ev.target.closest('.mj-wrow').remove(); regen(); return;
    }
    const act = ev.target.closest('[data-act]')?.dataset.act;
    if (act === 'close' || ev.target === wrap) { close(); return; }
    if (act === 'copy') {
      try { await navigator.clipboard.writeText(sqlBox.value); } catch { sqlBox.select(); }
      return;
    }
    if (act === 'run') {
      const sql = sqlBox.value.trim();
      close();
      showView('mariaquery');
      const sel = document.getElementById('mariaQuerySchema');
      if (sel) {
        // Every table in the generated SQL is schema-qualified, so this only
        // has to TELL THE TRUTH about where the query is aimed. Setting a
        // value with no matching option silently leaves it on "(none)", so
        // add it rather than let the screen misreport the target.
        if (![...sel.options].some(o => o.value === mariaSchema)) {
          sel.add(new Option(mariaSchema, mariaSchema));
        }
        sel.value = mariaSchema;
      }
      document.getElementById('mariaQuerySql').value = sql;
      document.getElementById('mariaQueryLimit').value =
        wrap.querySelector('.mj-limit')?.value || 200;
      runMariaQuery();
    }
  });

  const onKey = e => { if (e.key === 'Escape') close(); };
  function close() { wrap.remove(); document.removeEventListener('keydown', onKey); }
  document.addEventListener('keydown', onKey);

  // One empty condition to start with. It contributes nothing to the SQL until
  // a value is typed, so it costs an empty row and saves a click plus the
  // question of whether filtering is possible at all.
  addWhere();
  regen();
  wrap.querySelector('.mj-val')?.focus();
}

/* ── Column visibility ────────────────────────────────────────────────────
   Same modal furniture as the ES field picker (rt-modal-overlay / rt-field-row)
   so the two screens behave identically — this is the same job, and learning
   it twice would be the wrong kind of variety. */
function openMariaColumnPicker() {
  const pk = mariaSample?.primary_key || [];
  const cols = (mariaSample?.columns || []).length
    ? mariaSample.columns : mariaColumns.map(c => c.name);
  _openColumnPicker({
    title: `Columns — ${mariaTable}`,
    columns: cols,
    locked: pk,
    hidden: _mariaHiddenSet(),
    onChange: (hidden) => {
      mariaHiddenCols[_mariaTableKey()] = [...hidden];
      _mariaSave('ccadmin.maria.hiddenCols', mariaHiddenCols);
      renderMariaDetail();
    },
  });
}

/**
 * One column picker for both MariaDB screens.
 *
 * `locked` columns are always shown and their checkbox is disabled — the
 * browser's primary key is how a row is addressed, so hiding it would break
 * blob download and cell editing.
 *
 * Bulk actions matter more than they look: on a 30-column join, picking the
 * three columns you care about means unticking twenty-seven. "Unselect all"
 * turns that into three clicks, and the search box narrows the list first so
 * "select all" can act on just the matches.
 */
function _openColumnPicker(opts) {
  // `doc` lets this same picker be opened from the SQL results pop-out
  // window (popOutSqlResults below) rather than only the main document —
  // the modal then belongs to whichever window the user is actually looking
  // at instead of yanking their focus back to the other one.
  const doc = opts.doc || document;
  doc.querySelector('.rt-modal-overlay.rt-mariacols')?.remove();
  const locked = new Set(opts.locked || []);
  let hidden = new Set(opts.hidden || []);

  const wrap = doc.createElement('div');
  wrap.className = 'rt-modal-overlay rt-mariacols';
  wrap.innerHTML = `<div class="rt-modal rt-modal-fields">
      <div class="rt-modal-title"><i class="bi bi-eye me-1"></i>${esc(opts.title)}</div>
      <input class="form-control form-control-sm rt-mariacols-search mb-2"
             placeholder="search columns…"/>
      <div class="d-flex align-items-center gap-2 mb-2">
        <button class="btn btn-sm btn-outline-secondary py-0 px-2" data-act="all"
                style="font-size:.72rem;">Select all</button>
        <button class="btn btn-sm btn-outline-secondary py-0 px-2" data-act="none"
                style="font-size:.72rem;">Unselect all</button>
        <span class="text-secondary rt-mariacols-count" style="font-size:.7rem;"></span>
      </div>
      <div class="rt-fieldvis-body rt-mariacols-body"></div>
      <div class="rt-modal-actions">
        <button class="btn btn-sm btn-secondary" data-act="close">Close</button>
      </div>
    </div>`;
  doc.body.appendChild(wrap);

  const done = () => { wrap.remove(); doc.removeEventListener('keydown', onKey); };
  const onKey = e => { if (e.key === 'Escape') done(); };
  doc.addEventListener('keydown', onKey);

  // What the search box is currently narrowing to. The bulk buttons act on
  // THESE, so "search 'date' then Unselect all" hides only the date columns —
  // a bulk action that ignored the filter in front of it would be a trap.
  const visible = () => {
    const q = (wrap.querySelector('.rt-mariacols-search').value || '').toLowerCase();
    return (opts.columns || []).filter(c => !q || c.toLowerCase().includes(q));
  };

  const draw = () => {
    const host = wrap.querySelector('.rt-mariacols-body');
    const list = visible();
    host.innerHTML = list.map(c => {
      const isLocked = locked.has(c);
      return `<label class="rt-field-row"${isLocked
          ? ' title="Part of the primary key — always shown, because it is how a row is identified"' : ''}>
        <input type="checkbox" data-col="${esc(c)}"
               ${isLocked ? 'checked disabled' : (hidden.has(c) ? '' : 'checked')}/>
        <span class="rt-field-name">${esc(c)}</span>
        ${isLocked ? '<span class="rt-field-badge">key</span>' : ''}
      </label>`;
    }).join('') || '<div class="text-secondary small p-2">No matching columns.</div>';

    const total = (opts.columns || []).length;
    wrap.querySelector('.rt-mariacols-count').textContent =
      `${total - hidden.size} of ${total} shown`
      + (list.length !== total ? ` · ${list.length} matching` : '');
  };

  const apply = () => { opts.onChange(new Set(hidden)); draw(); };

  wrap.querySelector('.rt-mariacols-search').addEventListener('input', draw);
  wrap.addEventListener('change', ev => {
    const cb = ev.target.closest('input[data-col]');
    if (!cb) return;
    if (cb.checked) hidden.delete(cb.dataset.col); else hidden.add(cb.dataset.col);
    apply();
  });
  wrap.addEventListener('click', ev => {
    const act = ev.target.closest('[data-act]')?.dataset.act;
    if (act === 'close' || ev.target === wrap) { done(); return; }
    if (act === 'all')  { visible().forEach(c => hidden.delete(c)); apply(); }
    if (act === 'none') { visible().forEach(c => { if (!locked.has(c)) hidden.add(c); }); apply(); }
  });
  draw();
}

/* Byte sizes are formatted by _fmtBytes, already defined for the archive
   screens — one formatter, so a blob and an export report sizes the same way. */

/** Shared result-grid renderer for sample rows and query results.
 *  `ctx` (optional) carries {schema, table, primaryKey} — present only for a
 *  table sample, which is the one case where a row can be addressed well
 *  enough to download a binary column from it. */
function _mariaTable(columns, rows, truncated, ctx) {
  if (!rows.length) return '<div class="text-secondary small p-2">No rows.</div>';
  const pk = (ctx && ctx.primaryKey) || [];
  const hidden = (ctx && ctx.hidden) || new Set();
  // Primary-key columns are never hidden: they are how a row is addressed for
  // a blob download or an edit, and a grid where the key is invisible makes
  // every row look interchangeable.
  const visible = columns.filter(c => !hidden.has(c) || pk.includes(c));
  if (!visible.length)
    return '<div class="text-secondary small p-2">Every column is hidden — '
         + 'use Columns to bring some back.</div>';

  // Offered only when the capability is on AND this render is a table sample
  // (query results are a join or a projection, where a row does not map back
  // to one editable table row).
  const canEdit = !!(ctx && ctx.editable) && can('maria.write') && pk.length > 0;

  const head = visible.map(c => `<th class="text-nowrap">${esc(c)}</th>`).join('');
  const body = rows.map(r => '<tr>' + visible.map(c => {
    const v = r[c];
    // null is a fact about the row, not an empty cell — say so, or a NULL and
    // an empty string look identical and mean very different things.
    if (v === null || v === undefined) {
      // Still editable when the capability is on: a column that is NULL today
      // is exactly the one an engineer needs to set, and skipping it here made
      // empty fields permanently unfillable.
      if (!canEdit || !_mariaColEditable(c))
        return '<td class="text-secondary fst-italic">null</td>';
      const nkey = {}; for (const k of pk) nkey[k] = r[k];
      return `<td class="text-secondary fst-italic sql-cell-editable"`
           + ` title="NULL&#10;Double-click to edit" data-editcol="${esc(c)}"`
           + ` data-rk="${esc(JSON.stringify(nkey))}" data-val="" data-null="1">null</td>`;
    }

    // Binary column. The server sends a marker rather than the bytes, because
    // a BLOB is not text — quartz's JOB_DATA is a serialised Java object, and
    // decoding it as UTF-8 is what used to make this row a 500.
    if (v && typeof v === 'object' && v.__blob__) {
      const size = _fmtBytes(v.bytes || 0);
      if (!v.bytes) return `<td class="text-secondary fst-italic">empty blob</td>`;
      // The server only serves a download for columns it counts as binary. Any
      // other type that happens to arrive as bytes must not be offered one, or
      // the eye and download controls lead straight to a refusal — which is
      // how bit(1) flags came to render as 0.0 KB download links.
      const blobCols = (ctx && ctx.blobColumns) || null;
      if (blobCols && !blobCols.includes(c))
        return `<td class="text-secondary" title="Binary value on a `
             + `non-binary column — not downloadable">binary · ${size}</td>`;
      // Downloadable only when the row can actually be named.
      if (!pk.length || !ctx)
        return `<td class="text-secondary" title="No primary key, so this row `
             + `cannot be addressed for download">binary · ${size}</td>`;
      const key = {}; for (const k of pk) key[k] = r[k];
      const qs = 'schema=' + encodeURIComponent(ctx.schema)
        + '&table=' + encodeURIComponent(ctx.table)
        + '&column=' + encodeURIComponent(c)
        + '&key=' + encodeURIComponent(JSON.stringify(key));
      // View first, download second. These blobs are Java-serialised objects
      // wrapping a JSON payload, so the file on its own is unreadable — the
      // decoded view is what someone actually came for.
      return `<td class="text-nowrap">
                <button class="btn btn-link btn-sm p-0 text-decoration-none"
                        data-blob-qs="${esc(qs)}" title="View ${esc(c)} (${size})">
                  <i class="bi bi-eye me-1"></i>${size}</button>
                <a href="${esc(appUrl('/api/maria/blob?' + qs))}" download
                   class="ms-2 text-secondary" title="Download raw bytes">
                   <i class="bi bi-download"></i></a></td>`;
    }

    const s = String(v);
    const shown = esc(s.length > 80 ? s.slice(0, 80) + '…' : s);
    if (!canEdit || !_mariaColEditable(c)) {
      return `<td class="text-nowrap" title="${esc(s)}">${shown}</td>`;
    }
    const key = {}; for (const k of pk) key[k] = r[k];
    return `<td class="text-nowrap sql-cell-editable" title="${esc(s)}&#10;`
         + `Double-click to edit" data-editcol="${esc(c)}"`
         + ` data-rk="${esc(JSON.stringify(key))}"`
         + ` data-val="${esc(s)}">${shown}</td>`;
  }).join('') + '</tr>').join('');
  // The caller either owns the scroller (the browser's rows pane, the query
  // results card) or it does not, in which case one is supplied here.
  const ownScroller = !!(ctx && (ctx.editable || ctx.scroll));
  return `
    <div style="${ownScroller ? '' : 'overflow:auto;max-height:60vh;'}">
      <table class="table table-sm table-hover mb-0 font-monospace sql-grid" style="font-size:.72rem;">
        <thead><tr>${head}</tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>
    ${truncated ? '<div class="small text-warning-emphasis px-2 py-1">'
      + 'More rows exist — this result was capped.</div>' : ''}`;
}

/* ── Editing one cell ─────────────────────────────────────────────────────
   Mirrors the server's rules in modules/maria/writes.py so a cell that cannot
   be written is never offered as editable. The server re-checks every one of
   them — this decides what to OFFER, not what is PERMITTED, and the two must
   not be confused: a client-side rule is a courtesy, never a control. */
function _mariaColEditable(name) {
  const c = mariaColumns.find(x => x.name === name);
  if (!c) return false;
  if (c.key_type === 'PRI') return false;
  const extra = (c.extra || '').toLowerCase();
  if (extra.includes('auto_increment') || extra.includes('generated')) return false;
  const t = (c.data_type || '').toLowerCase();
  return !['blob', 'tinyblob', 'mediumblob', 'longblob', 'binary', 'varbinary'].includes(t);
}

/** Swap a cell for an input. Enter commits, Escape and blur abandon. */
function beginMariaCellEdit(td) {
  if (td.querySelector('input')) return;
  const original = td.dataset.val ?? '';
  const wasNull  = td.dataset.null === '1';
  const width = Math.max(td.getBoundingClientRect().width, 90);
  td.innerHTML = `<input class="sql-cell-input" style="width:${Math.round(width)}px"`
               + `${wasNull ? ' placeholder="NULL"' : ''}>`;
  const input = td.querySelector('input');
  input.value = original;
  input.focus();
  input.select();

  let settled = false;
  const revert = () => {
    if (settled) return;
    settled = true;
    td.innerHTML = _mariaCellHtml(original, wasNull);
  };
  input.addEventListener('keydown', ev => {
    if (ev.key === 'Escape') { ev.preventDefault(); revert(); }
    else if (ev.key === 'Enter') {
      ev.preventDefault();
      if (settled) return;
      settled = true;
      commitMariaCellEdit(td, original, input.value, wasNull);
    }
  });
  // Clicking away abandons rather than saves: a write to a live CC should
  // never happen because someone's focus moved.
  input.addEventListener('blur', revert);
}

/** How a cell reads when it is not being edited. */
function _mariaCellHtml(value, isNull) {
  if (isNull) return '<em>null</em>';
  return esc(value.length > 80 ? value.slice(0, 80) + '…' : value);
}

async function commitMariaCellEdit(td, before, after, wasNull) {
  const col = td.dataset.editcol;
  const key = JSON.parse(td.dataset.rk);
  td.innerHTML = _mariaCellHtml(before, wasNull);

  // A NULL cell left empty is unchanged; typing '' into a non-null one is a
  // real change (to the empty string), which is NOT the same as NULL.
  if (after === before && !(wasNull && after !== '')) return;

  const keyText = Object.entries(key).map(([k, v]) => `${k} = ${v}`).join(' AND ');
  const ok = await _mariaConfirmEdit({
    target: `${mariaSchema}.${mariaTable}.${col}`,
    where: keyText, before: wasNull ? 'NULL' : before, after,
  });
  if (!ok) return;

  const d = await api('/api/maria/cell', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      schema: mariaSchema, table: mariaTable, column: col, key,
      value: after, expected: wasNull ? null : before, expected_null: !!wasNull,
    }),
  });

  if (!d || d.error) {
    _mariaError('mariaError', (d && d.error) || 'the edit did not go through');
    return;
  }
  _mariaError('mariaError', '');
  // Update the row we hold rather than refetching the table: a re-read would
  // reorder rows under the user and lose their place in a 117-row grid.
  const row = (mariaSample?.rows || []).find(
    r => Object.entries(key).every(([k, v]) => String(r[k]) === String(v)));
  if (row) row[col] = after;
  renderMariaDetail();
  document.querySelectorAll(`td[data-editcol="${CSS.escape(col)}"]`).forEach(cell => {
    if (cell.dataset.rk === td.dataset.rk) cell.classList.add('sql-cell-edited');
  });
}

/** The confirmation. Shows the row, the old and new value, and the statement —
 *  an engineer should be able to see exactly what is about to run before it
 *  runs against an appliance someone else depends on. */
function _mariaConfirmEdit(o) {
  return new Promise(resolve => {
    const wrap = document.createElement('div');
    wrap.className = 'rt-modal-overlay';
    const stmt = `UPDATE ${mariaSchema}.${mariaTable}\n   SET ${o.target.split('.').pop()} = `
      + `'${o.after}'\n WHERE ${o.where}\n LIMIT 1;`;
    wrap.innerHTML = `<div class="rt-modal" style="max-width:560px;">
        <div class="rt-modal-title">⚠ Edit a row on this CC</div>
        <div class="rt-modal-body">
          <div class="small mb-2">
            This changes live data in the CC's configuration database. It is
            not reversible from here.
          </div>
          <table class="table table-sm mb-2" style="font-size:.76rem;">
            <tr><td class="text-secondary">Cell</td>
                <td class="font-monospace">${esc(o.target)}</td></tr>
            <tr><td class="text-secondary">Row</td>
                <td class="font-monospace">${esc(o.where)}</td></tr>
            <tr><td class="text-secondary">From</td>
                <td class="font-monospace">${esc(o.before) || '<em>empty</em>'}</td></tr>
            <tr><td class="text-secondary">To</td>
                <td class="font-monospace fw-semibold">${esc(o.after) || '<em>empty</em>'}</td></tr>
          </table>
          <pre class="bg-body-tertiary p-2 rounded mb-0" style="font-size:.72rem;
               white-space:pre-wrap;">${esc(stmt)}</pre>
        </div>
        <div class="rt-modal-actions">
          <button class="btn btn-sm btn-warning" data-ok="1">Apply the change</button>
          <button class="btn btn-sm btn-outline-secondary" data-ok="0">Cancel</button>
        </div></div>`;
    document.body.appendChild(wrap);
    const done = v => { wrap.remove(); document.removeEventListener('keydown', onKey); resolve(v); };
    const onKey = e => { if (e.key === 'Escape') done(false); };
    document.addEventListener('keydown', onKey);
    wrap.addEventListener('click', e => {
      const b = e.target.closest('button');
      if (b) { done(b.getAttribute('data-ok') === '1'); return; }
      if (e.target === wrap) done(false);
    });
  });
}

/* ══════════════════════════════════════════════════════════════════════════
   Query wizard — for engineers who know the CC but not SQL
   ══════════════════════════════════════════════════════════════════════════
   Pick a table, pick columns, add conditions, get a statement. The statement
   is always shown before it runs: the point is to remove the need to REMEMBER
   syntax, not to hide what is being executed.

   Only SELECT is offered, and the other two are shown greyed with the reason
   rather than omitted — someone who came looking for UPDATE should find out
   why it is not there instead of concluding the tool is unfinished. */

async function openMariaQueryWizard() {
  document.querySelector('.rt-modal-overlay.rt-mariawiz')?.remove();
  if (!mariaSchemas.length) await loadMariaSchemas();

  const wrap = document.createElement('div');
  wrap.className = 'rt-modal-overlay rt-mariawiz';
  wrap.innerHTML = `<div class="rt-modal" style="width:min(900px,94vw);">
      <div class="rt-modal-title"><i class="bi bi-magic me-1"></i>Build a query</div>
      <div class="rt-modal-body" style="white-space:normal;">

        <div class="d-flex align-items-center gap-2 mb-2 flex-wrap">
          <div class="btn-group btn-group-sm" role="group">
            <input type="radio" class="btn-check" name="wizVerb" id="wizSelect" checked>
            <label class="btn btn-outline-primary py-0 px-3" for="wizSelect"
                   style="font-size:.75rem;">SELECT</label>
            <input type="radio" class="btn-check" name="wizVerb" id="wizUpdate" disabled>
            <label class="btn btn-outline-secondary py-0 px-3 disabled" for="wizUpdate"
                   style="font-size:.75rem;"
                   title="Not available — see the note below">UPDATE</label>
            <input type="radio" class="btn-check" name="wizVerb" id="wizDelete" disabled>
            <label class="btn btn-outline-secondary py-0 px-3 disabled" for="wizDelete"
                   style="font-size:.75rem;"
                   title="Not available — see the note below">DELETE</label>
          </div>
          <label class="small text-secondary mb-0 ms-2">Schema</label>
          <select class="form-select form-select-sm wiz-schema" style="width:170px;font-size:.75rem;">
            ${mariaSchemas.map(s => `<option value="${esc(s.name)}"
              ${s.name === mariaSchema ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}
          </select>
          <label class="small text-secondary mb-0 ms-1">Table</label>
          <select class="form-select form-select-sm wiz-table"
                  style="width:220px;font-size:.75rem;"><option>loading…</option></select>
        </div>

        <div class="alert alert-secondary py-1 px-2 small mb-2" style="font-size:.72rem;">
          <b>UPDATE and DELETE are not offered.</b> This screen runs through a
          read-only connection, so the server would refuse them. Changing data
          needs the per-row edit in the table browser, which is separately
          gated and audited.
        </div>

        <div class="mj-filters mb-2">
          <div class="mj-filters-head">
            <i class="bi bi-list-columns me-1"></i><span>Columns</span>
            <span class="text-secondary fw-normal ms-1" style="text-transform:none;">
              — none ticked means all</span>
            <button class="btn btn-sm btn-outline-secondary py-0 px-2 ms-auto wiz-cols-none"
                    style="font-size:.7rem;">Unselect all</button>
            <button class="btn btn-sm btn-outline-secondary py-0 px-2 wiz-cols-all"
                    style="font-size:.7rem;">Select all</button>
          </div>
          <div class="wiz-cols" style="max-height:120px;overflow:auto;padding:6px 8px;
               display:flex;flex-wrap:wrap;gap:4px 12px;"></div>
        </div>

        <div class="mj-filters mb-2">
          <div class="mj-filters-head">
            <i class="bi bi-funnel me-1"></i><span>Conditions</span>
            <span class="text-secondary fw-normal ms-1" style="text-transform:none;">
              — combined with AND</span>
            <button class="btn btn-sm btn-outline-primary py-0 px-2 ms-auto wiz-addwhere"
                    style="font-size:.7rem;"><i class="bi bi-plus-lg me-1"></i>Add condition</button>
          </div>
          <div class="wiz-where"></div>
        </div>

        <div class="d-flex align-items-center gap-2 mb-2 flex-wrap">
          <label class="small text-secondary mb-0">Order by</label>
          <select class="form-select form-select-sm wiz-order" style="width:200px;font-size:.75rem;">
            <option value="">(none)</option>
          </select>
          <select class="form-select form-select-sm wiz-dir" style="width:90px;font-size:.75rem;">
            <option value="ASC">ASC</option><option value="DESC">DESC</option>
          </select>
          <label class="small text-secondary mb-0 ms-2">Limit</label>
          <input type="number" min="1" max="10000" value="200"
                 class="form-control form-control-sm wiz-limit" style="width:90px;font-size:.75rem;">
        </div>

        <div class="small fw-semibold text-secondary mb-1">Generated SQL</div>
        <textarea class="form-control font-monospace wiz-sql" rows="7" wrap="off"
                  spellcheck="false" readonly
                  style="font-size:.74rem;white-space:pre;overflow-x:auto;"></textarea>
      </div>
      <div class="rt-modal-actions">
        <button class="btn btn-sm btn-primary" data-act="run">
          <i class="bi bi-play-fill me-1"></i>Run</button>
        <button class="btn btn-sm btn-outline-primary" data-act="use">Put in editor</button>
        <button class="btn btn-sm btn-outline-secondary" data-act="close">Close</button>
      </div>
    </div>`;
  document.body.appendChild(wrap);

  const done = () => { wrap.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = e => { if (e.key === 'Escape') done(); };
  document.addEventListener('keydown', onKey);

  let columns = [];

  const regen = () => {
    const schema = wrap.querySelector('.wiz-schema').value;
    const table  = wrap.querySelector('.wiz-table').value;
    if (!table) { wrap.querySelector('.wiz-sql').value = ''; return; }
    const picked = [...wrap.querySelectorAll('.wiz-cols input:checked')]
      .map(cb => cb.dataset.col);
    const cols = picked.length && picked.length !== columns.length
      ? picked.map(c => _q(c)).join(',\n       ') : '*';

    const where = [...wrap.querySelectorAll('.wiz-wrow')].map(r =>
      _mariaWhereSql(_q(r.querySelector('.wiz-col').value),
                     r.querySelector('.wiz-op').value,
                     r.querySelector('.wiz-val')?.value ?? '')).filter(Boolean);

    const order = wrap.querySelector('.wiz-order').value;
    const lines = [`SELECT ${cols}`, `  FROM ${_q(schema)}.${_q(table)}`];
    if (where.length) lines.push(' WHERE ' + where.join('\n   AND '));
    if (order) lines.push(` ORDER BY ${_q(order)} ${wrap.querySelector('.wiz-dir').value}`);
    lines.push(` LIMIT ${Math.max(1, parseInt(wrap.querySelector('.wiz-limit').value, 10) || 200)}`);
    wrap.querySelector('.wiz-sql').value = lines.join('\n');
  };

  const addWhere = () => {
    const row = document.createElement('div');
    row.className = 'wiz-wrow d-flex align-items-center gap-1 mb-1';
    row.innerHTML = `
      <select class="form-select form-select-sm wiz-col font-monospace"
              style="font-size:.72rem;flex:1 1 auto;min-width:0;">
        ${columns.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('')}
      </select>
      <select class="form-select form-select-sm wiz-op" style="width:120px;font-size:.72rem;">
        ${['=', '!=', 'LIKE', 'IN', '>', '<', '>=', '<=', 'IS NULL', 'IS NOT NULL']
          .map(o => `<option>${o}</option>`).join('')}
      </select>
      <input type="text" class="form-control form-control-sm wiz-val" placeholder="value"
             style="font-size:.72rem;width:180px;">
      <button class="btn btn-sm btn-link text-secondary py-0 px-1 wiz-del"
              title="Remove"><i class="bi bi-x-lg"></i></button>`;
    wrap.querySelector('.wiz-where').appendChild(row);
    const op = row.querySelector('.wiz-op');
    op.addEventListener('change', () => {
      const none = op.value === 'IS NULL' || op.value === 'IS NOT NULL';
      row.querySelector('.wiz-val').classList.toggle('d-none', none);
    });
  };

  const loadColumns = async () => {
    const schema = wrap.querySelector('.wiz-schema').value;
    const table  = wrap.querySelector('.wiz-table').value;
    const host = wrap.querySelector('.wiz-cols');
    if (!table) { host.innerHTML = ''; columns = []; return; }
    host.innerHTML = '<span class="text-secondary small">loading…</span>';
    const d = await api(`/api/maria/columns?schema=${encodeURIComponent(schema)}`
                      + `&table=${encodeURIComponent(table)}`);
    columns = (d && !d.error && d.columns) ? d.columns.map(c => c.name) : [];
    host.innerHTML = columns.map(c => `
      <label class="d-flex align-items-center gap-1" style="font-size:.72rem;">
        <input type="checkbox" data-col="${esc(c)}">
        <span class="font-monospace">${esc(c)}</span>
      </label>`).join('') || '<span class="text-secondary small">no columns</span>';
    wrap.querySelector('.wiz-order').innerHTML = '<option value="">(none)</option>'
      + columns.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
    wrap.querySelector('.wiz-where').innerHTML = '';   // stale columns
    regen();
  };

  const loadTables = async () => {
    const schema = wrap.querySelector('.wiz-schema').value;
    const sel = wrap.querySelector('.wiz-table');
    sel.innerHTML = '<option>loading…</option>';
    const d = await api(`/api/maria/tables?schema=${encodeURIComponent(schema)}`);
    const tables = (d && !d.error && d.tables) ? d.tables : [];
    sel.innerHTML = tables.map(t => `<option value="${esc(t.name)}"
      ${t.name === mariaTable ? 'selected' : ''}>${esc(t.name)}</option>`).join('')
      || '<option value="">(no tables)</option>';
    await loadColumns();
  };

  wrap.addEventListener('change', async ev => {
    if (ev.target.closest('.wiz-schema')) { await loadTables(); return; }
    if (ev.target.closest('.wiz-table'))  { await loadColumns(); return; }
    regen();
  });
  wrap.addEventListener('input', regen);
  wrap.addEventListener('click', ev => {
    if (ev.target.closest('.wiz-addwhere')) { addWhere(); regen(); return; }
    if (ev.target.closest('.wiz-del')) { ev.target.closest('.wiz-wrow').remove(); regen(); return; }
    if (ev.target.closest('.wiz-cols-all')) {
      wrap.querySelectorAll('.wiz-cols input').forEach(cb => cb.checked = true); regen(); return;
    }
    if (ev.target.closest('.wiz-cols-none')) {
      wrap.querySelectorAll('.wiz-cols input').forEach(cb => cb.checked = false); regen(); return;
    }
    const act = ev.target.closest('[data-act]')?.dataset.act;
    if (act === 'close' || ev.target === wrap) { done(); return; }
    if (act === 'use' || act === 'run') {
      const sql = wrap.querySelector('.wiz-sql').value;
      const schema = wrap.querySelector('.wiz-schema').value;
      done();
      const sel = document.getElementById('mariaQuerySchema');
      if (sel) {
        if (![...sel.options].some(o => o.value === schema)) sel.add(new Option(schema, schema));
        sel.value = schema;
      }
      document.getElementById('mariaQueryLimit').value =
        wrap.querySelector('.wiz-limit').value || 200;
      _mariaQuerySetSql(sql);
      if (act === 'run') runMariaQuery();
    }
  });

  await loadTables();
  addWhere();
  regen();
}

/* ══════════════════════════════════════════════════════════════════════════
   Editing a query's WHERE clause from the UI
   ══════════════════════════════════════════════════════════════════════════
   Rewriting SQL text is where a helpful feature turns destructive, so the
   rule here is: understand the statement or refuse to touch it. Everything
   below works on a character map that knows which positions are inside a
   string, a backtick-quoted identifier or a comment, and how deep in
   parentheses they are — a plain `split(' AND ')` would happily cut a query
   in half at an AND inside a quoted value or a subquery. */

function _sqlMap(sql) {
  const depth = new Array(sql.length).fill(0);
  const code  = new Array(sql.length).fill(true);
  let d = 0, i = 0;
  while (i < sql.length) {
    const c = sql[i], n = sql[i + 1];
    if (c === '-' && n === '-') {
      while (i < sql.length && sql[i] !== '\n') { code[i] = false; depth[i] = d; i++; }
      continue;
    }
    if (c === '#') {
      while (i < sql.length && sql[i] !== '\n') { code[i] = false; depth[i] = d; i++; }
      continue;
    }
    if (c === '/' && n === '*') {
      const end = sql.indexOf('*/', i + 2);
      const stop = end === -1 ? sql.length : end + 2;
      while (i < stop) { code[i] = false; depth[i] = d; i++; }
      continue;
    }
    if (c === "'" || c === '"' || c === '`') {
      const quote = c;
      code[i] = false; depth[i] = d; i++;
      while (i < sql.length) {
        if (sql[i] === '\\' && quote !== '`') { code[i] = false; depth[i] = d; i++; }
        else if (sql[i] === quote) {
          // A doubled quote is an escaped one, not the end.
          if (sql[i + 1] === quote) { code[i] = false; depth[i] = d; i++; }
          else { code[i] = false; depth[i] = d; i++; break; }
        }
        code[i] = false; depth[i] = d; i++;
      }
      continue;
    }
    if (c === '(') { depth[i] = d; d++; i++; continue; }
    if (c === ')') { d = Math.max(0, d - 1); depth[i] = d; i++; continue; }
    depth[i] = d; i++;
  }
  return { depth, code };
}

/** First match of `pattern` that is real code at paren depth 0. */
function _sqlFindTop(sql, map, pattern, from = 0) {
  const re = new RegExp('\\b(?:' + pattern + ')\\b', 'gi');
  re.lastIndex = from;
  let m;
  while ((m = re.exec(sql)) !== null) {
    if (map.code[m.index] && map.depth[m.index] === 0) {
      return { start: m.index, end: m.index + m[0].length };
    }
  }
  return null;
}

// Clauses that can follow a WHERE. The WHERE body ends at the first of these.
const _SQL_TAIL = 'GROUP\\s+BY|HAVING|ORDER\\s+BY|LIMIT|PROCEDURE|INTO|UNION|WINDOW';

/**
 * Split a statement into { head, conditions[], tail } so the UI can edit the
 * conditions and put it back together.
 *
 * `editable` is false — with a reason shown to the user — whenever the WHERE
 * is something this cannot safely round-trip. Refusing is the feature: a
 * mangled query against a customer's CC is worse than no button.
 */
function parseMariaWhere(sql) {
  const map = _sqlMap(sql);
  const tail = _sqlFindTop(sql, map, _SQL_TAIL);
  const where = _sqlFindTop(sql, map, 'WHERE');

  if (!where) {
    // No WHERE yet: conditions can still be ADDED, spliced in before the
    // first trailing clause.
    return { editable: true, hasWhere: false, conditions: [],
             head: sql.slice(0, tail ? tail.start : sql.length).replace(/\s+$/, ''),
             tail: tail ? sql.slice(tail.start) : '' };
  }

  const bodyEnd = (tail && tail.start > where.end) ? tail.start : sql.length;
  const body = sql.slice(where.end, bodyEnd);
  const bmap = _sqlMap(body);

  if (_sqlFindTop(body, bmap, 'OR')) {
    return { editable: false,
             reason: 'This WHERE mixes OR with AND. Removing one part could '
                   + 'change what the rest means, so the conditions are shown '
                   + 'read-only — edit the SQL directly.' };
  }

  // Split on top-level AND — except the one belonging to a BETWEEN, which is
  // part of that condition, not a separator. Splitting there would let someone
  // delete half of `a BETWEEN 1 AND 2` and be handed `a BETWEEN 1`.
  const parts = [];
  const re = /\b(AND|BETWEEN)\b/gi;
  let m, last = 0, pendingBetween = false;
  while ((m = re.exec(body)) !== null) {
    if (!bmap.code[m.index] || bmap.depth[m.index] !== 0) continue;
    if (m[0].toUpperCase() === 'BETWEEN') { pendingBetween = true; continue; }
    if (pendingBetween) { pendingBetween = false; continue; }   // the BETWEEN's own AND
    parts.push(body.slice(last, m.index));
    last = m.index + m[0].length;
  }
  parts.push(body.slice(last));

  const conditions = parts.map(p => p.trim()).filter(Boolean);
  if (!conditions.length) {
    return { editable: false, reason: 'The WHERE clause is empty.' };
  }
  return { editable: true, hasWhere: true, conditions,
           head: sql.slice(0, where.start).replace(/\s+$/, ''),
           tail: sql.slice(bodyEnd) };
}

/** Put a statement back together from edited conditions. */
function buildMariaWhere(parsed, conditions) {
  const body = conditions.filter(c => c && c.trim());
  const tail = parsed.tail ? '\n ' + parsed.tail.trim() : '';
  if (!body.length) return parsed.head + tail;
  return parsed.head + '\n WHERE ' + body.join('\n   AND ') + tail;
}

/** Redraw the conditions panel from whatever is currently in the editor. */
function syncMariaQueryConditions() {
  const host = document.getElementById('mariaQueryConds');
  const sql = document.getElementById('mariaQuerySql')?.value || '';
  if (!host) return;

  if (!sql.trim()) { host.classList.add('d-none'); host.innerHTML = ''; return; }

  let p;
  try { p = parseMariaWhere(sql); }
  catch { host.classList.add('d-none'); return; }

  host.classList.remove('d-none');

  if (!p.editable) {
    host.innerHTML = `<div class="mq-cond-row text-secondary">
        <i class="bi bi-info-circle"></i><span>${esc(p.reason)}</span></div>`;
    return;
  }

  const rows = p.conditions.map((c, i) => `
    <div class="mq-cond-row">
      <span class="mq-cond-join">${i === 0 ? 'WHERE' : 'AND'}</span>
      <span class="mq-cond-text flex-grow-1" title="${esc(c)}">${esc(c)}</span>
      <button class="btn btn-sm btn-link text-secondary py-0 px-1"
              data-cond-del="${i}" title="Remove this condition">
        <i class="bi bi-x-lg"></i></button>
    </div>`).join('');

  host.innerHTML = rows + `
    <div class="mq-cond-row">
      <span class="mq-cond-join">${p.conditions.length ? 'AND' : 'WHERE'}</span>
      <input class="form-control form-control-sm mq-newcol font-monospace"
             list="mqColList" placeholder="column"
             style="font-size:.72rem;flex:1 1 auto;min-width:0;">
      <datalist id="mqColList">${
        (_mariaQueryCols || []).map(c => `<option value="${esc(c)}"></option>`).join('')}</datalist>
      <select class="form-select form-select-sm mq-newop" style="width:120px;font-size:.72rem;">
        ${['=', '!=', 'LIKE', 'IN', '>', '<', '>=', '<=', 'IS NULL', 'IS NOT NULL']
          .map(o => `<option>${o}</option>`).join('')}
      </select>
      <input class="form-control form-control-sm mq-newval" placeholder="value"
             style="font-size:.72rem;width:160px;">
      <button class="btn btn-sm btn-outline-primary py-0 px-2" data-cond-add="1"
              style="font-size:.72rem;">Add</button>
    </div>`;
}

function _mariaQuerySetSql(sql) {
  const box = document.getElementById('mariaQuerySql');
  box.value = sql;
  syncMariaQueryConditions();
}

function removeMariaCondition(idx) {
  const sql = document.getElementById('mariaQuerySql').value;
  const p = parseMariaWhere(sql);
  if (!p.editable) return;
  const next = p.conditions.slice();
  next.splice(idx, 1);
  _mariaQuerySetSql(buildMariaWhere(p, next));
}

function addMariaCondition(host) {
  const col = host.querySelector('.mq-newcol')?.value.trim();
  const op  = host.querySelector('.mq-newop')?.value;
  const val = host.querySelector('.mq-newval')?.value ?? '';
  if (!col) return;
  // Bare names get quoted; anything already qualified or quoted is left alone,
  // so `t`.`c` and expressions both survive.
  const colSql = /[`.(\s]/.test(col) ? col : _q(col);
  const cond = _mariaWhereSql(colSql, op, val);
  if (!cond) return;
  const sql = document.getElementById('mariaQuerySql').value;
  const p = parseMariaWhere(sql);
  if (!p.editable) return;
  _mariaQuerySetSql(buildMariaWhere(p, [...p.conditions, cond]));
}

function _fillMariaQuerySchemas() {
  const sel = document.getElementById('mariaQuerySchema');
  if (!sel) return;
  const keep = sel.value;
  sel.innerHTML = '<option value="">(none)</option>'
    + mariaSchemas.map(s => `<option value="${esc(s.name)}">${esc(s.name)}</option>`).join('');
  // Default to the largest non-system schema — on a CC that is vision_ng, and
  // starting there saves the first click of nearly every session.
  sel.value = keep || (mariaSchemas.find(s => !s.system)?.name || '');
}

/* Last result set, kept so the column picker and re-renders do not need to
   re-run the query — running a join twice to hide a column is both slow and,
   on a live CC, a second real load. */
let _mariaQueryCols = [];
let _mariaQueryRows = [];
let _mariaQueryMeta = null;
let _mariaQueryHidden = new Set();

async function runMariaQuery() {
  const sql = (document.getElementById('mariaQuerySql')?.value || '').trim();
  if (!sql) return;
  const schema = document.getElementById('mariaQuerySchema')?.value || '';
  const limit = parseInt(document.getElementById('mariaQueryLimit')?.value, 10) || null;

  const out  = document.getElementById('mariaQueryResults');
  const meta = document.getElementById('mariaQueryMeta');
  out.innerHTML = '<div class="text-secondary small p-3">Running…</div>';
  meta.textContent = '';
  _mariaError('mariaQueryError', '');

  const d = await api('/api/maria/query', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sql, schema_: schema, limit }),
  });

  if (!d || d.error) {
    _mariaError('mariaQueryError', (d && d.error) || 'query failed');
    out.innerHTML = '<div class="text-secondary small p-3">—</div>';
    document.getElementById('mariaQueryColsBtn')?.classList.add('d-none');
    document.getElementById('mariaQueryPopoutBtn')?.classList.add('d-none');
    return;
  }

  _mariaQueryCols = d.columns || [];
  _mariaQueryRows = d.rows || [];
  _mariaQueryMeta = d;
  // A hidden column from the previous query means nothing for this one unless
  // the name is still there; keeping only the survivors avoids a column
  // vanishing for a reason nobody can see.
  _mariaQueryHidden = new Set([..._mariaQueryHidden].filter(c => _mariaQueryCols.includes(c)));

  document.getElementById('mariaQueryColsBtn')
    ?.classList.toggle('d-none', !_mariaQueryCols.length);
  document.getElementById('mariaQueryPopoutBtn')
    ?.classList.toggle('d-none', !_mariaQueryCols.length);
  // The datalist behind the "add condition" row comes from the result columns.
  syncMariaQueryConditions();
  renderMariaQueryResults();
}

function renderMariaQueryResults() {
  const out  = document.getElementById('mariaQueryResults');
  const meta = document.getElementById('mariaQueryMeta');
  const d = _mariaQueryMeta;
  if (!out || !d) return;

  const shown = _mariaQueryCols.length - _mariaQueryHidden.size;
  meta.textContent = `${d.count} row(s) · ${d.took_ms} ms`
    + (d.truncated ? ` · capped at ${d.row_cap}` : '')
    + (_mariaQueryHidden.size ? ` · ${shown} of ${_mariaQueryCols.length} columns` : '');

  out.innerHTML = _mariaTable(_mariaQueryCols, _mariaQueryRows, d.truncated,
                              { hidden: _mariaQueryHidden, scroll: true });
  syncSqlPopout();
}

/** Column picker for the query results. Same furniture as the browser's, plus
 *  the two bulk actions — starting from nothing and ticking three columns is
 *  the common case on a 30-column join, and unticking 27 is not a workflow. */
function openMariaQueryColumnPicker() {
  _openColumnPicker({
    title: 'Columns — query results',
    columns: _mariaQueryCols,
    locked: [],
    hidden: _mariaQueryHidden,
    onChange: (hidden) => { _mariaQueryHidden = hidden; renderMariaQueryResults(); },
  });
}

/* ══════════════════════════════════════════════════════════════════════════
   PostgreSQL — Databases & Tables, and the SQL Query screen
   ══════════════════════════════════════════════════════════════════════════
   Deliberately its own set of functions rather than a generalisation of the
   MariaDB ones above: MariaDB's screen is shipped and tested, and threading a
   backend parameter through ~2000 lines of closures over global state (drag
   handles, the join builder, the WHERE wizard) risked a subtle regression
   there for a save on typing here. What IS shared is the generic furniture
   that was already parameterised before PostgreSQL existed —
   _openColumnPicker() (takes its target as `opts`) and the sql-* CSS classes
   (already structural, not MariaDB-specific).

   Two things MariaDB's screen has that this one does not, both deliberately
   deferred rather than ported: the visual join builder and the WHERE-clause
   wizard (openMariaQueryWizard and the regex-based mini SQL parser behind
   it — _sqlMap/_sqlFindTop/parseMariaWhere). Porting a hand-rolled SQL parser
   to a second dialect and re-verifying its edge cases is a project of its
   own; the raw SQL box below is still the full read-only escape hatch, typed
   rather than point-and-click for now. The relations panel below also skips
   the join tick-boxes for the same reason — nothing here builds a join, so
   nothing offers to select one.

   PostgreSQL's own difference from MariaDB shapes the naming throughout:
   one connection sees exactly one DATABASE (no `USE`), so what MariaDB calls
   a schema is a database here — see modules/pg/catalog.py. */

let pgDatabases  = [];
let pgTableList  = [];
let pgDatabase   = '';       // selected database
let pgTable      = '';       // selected table
let pgColumns    = [];       // column definitions for the selected table
let pgSample     = null;     // last /sample payload for the selected table
let pgRelations  = null;     // last /keys payload for the selected table

/* Same per-table hidden-column and layout memory as MariaDB, under its own
   storage keys so the two stores' preferences do not collide. */
let pgHiddenCols = _mariaLoad('ccadmin.pg.hiddenCols', {});
let pgCollapsed  = _mariaLoad('ccadmin.pg.collapsed', {});
let pgPaneWidths = _mariaLoad('ccadmin.pg.paneWidths', null);
let pgSectionH   = _mariaLoad('ccadmin.pg.sectionH', {});

function _pgTableKey() { return `${pgDatabase}.${pgTable}`; }
function _pgHiddenSet() { return new Set(pgHiddenCols[_pgTableKey()] || []); }

function initPgPanes() {
  document.getElementById('pgSchemas')?.addEventListener('click', ev => {
    const btn = ev.target.closest('[data-schema]');
    if (btn) selectPgDatabase(btn.dataset.schema);
  });
  document.getElementById('pgTables')?.addEventListener('click', ev => {
    const btn = ev.target.closest('[data-table]');
    if (btn) selectPgTable(btn.dataset.table);
  });
  document.addEventListener('click', ev => {
    const btn = ev.target.closest('[data-pg-blob-qs]');
    if (btn) { ev.preventDefault(); showPgBlobViewer(btn.dataset.pgBlobQs); }
  });

  const detail = document.getElementById('pgDetail');
  detail?.addEventListener('click', ev => {
    const head = ev.target.closest('[data-sect]');
    if (head) { togglePgSection(head.dataset.sect); return; }
    const jump = ev.target.closest('[data-goto-table]');
    if (jump) { selectPgTable(jump.dataset.gotoTable); return; }
    if (ev.target.closest('[data-pg-cols]')) { openPgColumnPicker(); return; }
    if (ev.target.closest('[data-pg-popout]')) { popOutSqlResults('pg-table'); return; }
  });
  detail?.addEventListener('dblclick', ev => {
    const td = ev.target.closest('td[data-editcol]');
    if (td) beginPgCellEdit(td);
    const hs = ev.target.closest('.sql-hsplit');
    if (hs) resetPgSectionHeight(hs.dataset.hsplit);
  });
  detail?.addEventListener('pointerdown', ev => {
    const hs = ev.target.closest('.sql-hsplit');
    if (hs) startPgSectionDrag(hs, ev);
  });
  detail?.addEventListener('keydown', ev => {
    const hs = ev.target.closest('.sql-hsplit');
    if (!hs || (ev.key !== 'ArrowUp' && ev.key !== 'ArrowDown')) return;
    ev.preventDefault();
    const body = document.querySelector(`[data-sect-body="${hs.dataset.hsplit}"]`);
    const step = (ev.shiftKey ? 40 : 12) * (ev.key === 'ArrowDown' ? 1 : -1);
    _setPgSectionHeight(hs.dataset.hsplit, body,
                        body.getBoundingClientRect().height + step);
  });

  initPgSplitters();
  initPgQueryScreen();
}

function initPgQueryScreen() {
  // The editor / results splitter, same contract as MariaDB's SQL screen.
  const sp = document.querySelector('#view-pgquery [data-mqsplit]');
  const editor = document.querySelector('#view-pgquery .mq-editor');
  if (!sp || !editor) return;

  const saved = _mariaLoad('ccadmin.pg.editorH', null);
  if (saved) editor.style.height = saved + 'px';

  const clamp = (wanted) => {
    const res = document.querySelector('#view-pgquery .mq-results');
    const slack = res ? Math.max(0, res.getBoundingClientRect().height - 140) : 0;
    const max = editor.getBoundingClientRect().height + slack;
    const h = Math.round(Math.min(max, Math.max(130, wanted)));
    editor.style.height = h + 'px';
    _mariaSave('ccadmin.pg.editorH', h);
  };

  sp.addEventListener('dblclick', () => {
    editor.style.height = '';
    _mariaSave('ccadmin.pg.editorH', null);
  });
  sp.addEventListener('keydown', ev => {
    if (ev.key !== 'ArrowUp' && ev.key !== 'ArrowDown') return;
    ev.preventDefault();
    clamp(editor.getBoundingClientRect().height
          + (ev.shiftKey ? 40 : 12) * (ev.key === 'ArrowDown' ? 1 : -1));
  });
  sp.addEventListener('pointerdown', ev => {
    ev.preventDefault();
    const startY = ev.clientY, startH = editor.getBoundingClientRect().height;
    sp.setPointerCapture?.(ev.pointerId);
    sp.classList.add('dragging');
    document.body.classList.add('sql-resizing-y');
    const onMove = e => clamp(startH + e.clientY - startY);
    const onUp = () => {
      sp.removeEventListener('pointermove', onMove);
      sp.removeEventListener('pointerup', onUp);
      sp.removeEventListener('pointercancel', onUp);
      sp.classList.remove('dragging');
      document.body.classList.remove('sql-resizing-y');
    };
    sp.addEventListener('pointermove', onMove);
    sp.addEventListener('pointerup', onUp);
    sp.addEventListener('pointercancel', onUp);
  });
}

function startPgSectionDrag(hs, ev) {
  ev.preventDefault();
  const id   = hs.dataset.hsplit;
  const body = document.querySelector(`[data-sect-body="${id}"]`);
  if (!body) return;
  const startY = ev.clientY;
  const startH = body.getBoundingClientRect().height;

  hs.setPointerCapture?.(ev.pointerId);
  hs.classList.add('dragging');
  document.body.classList.add('sql-resizing-y');

  const onMove = e => _setPgSectionHeight(id, body, startH + e.clientY - startY);
  const onUp = () => {
    hs.removeEventListener('pointermove', onMove);
    hs.removeEventListener('pointerup', onUp);
    hs.removeEventListener('pointercancel', onUp);
    hs.classList.remove('dragging');
    document.body.classList.remove('sql-resizing-y');
    _mariaSave('ccadmin.pg.sectionH', pgSectionH);
  };
  hs.addEventListener('pointermove', onMove);
  hs.addEventListener('pointerup', onUp);
  hs.addEventListener('pointercancel', onUp);
}

function _setPgSectionHeight(id, body, wanted) {
  const rows = document.querySelector('#pgDetail .sql-rows-section');
  const slack = rows ? Math.max(0, rows.getBoundingClientRect().height - 132) : 0;
  const max = body.getBoundingClientRect().height + slack;
  const h = Math.round(Math.min(max, Math.max(48, wanted)));
  body.style.height = h + 'px';
  pgSectionH[id] = h;
  _mariaSave('ccadmin.pg.sectionH', pgSectionH);
}

function resetPgSectionHeight(id) {
  delete pgSectionH[id];
  _mariaSave('ccadmin.pg.sectionH', pgSectionH);
  const body = document.querySelector(`[data-sect-body="${id}"]`);
  if (body) body.style.height = '';
}

function initPgSplitters() {
  const strip = document.getElementById('pgPanes');
  if (!strip) return;
  const panes = [document.getElementById('pgPaneSchemas'),
                 document.getElementById('pgPaneTables')];

  if (Array.isArray(pgPaneWidths)) {
    panes.forEach((p, i) => { if (p && pgPaneWidths[i]) p.style.width = pgPaneWidths[i] + 'px'; });
  }

  strip.querySelectorAll('.sql-splitter').forEach(sp => {
    const idx  = parseInt(sp.dataset.split, 10);
    const pane = panes[idx];
    if (!pane) return;

    sp.addEventListener('dblclick', () => {
      panes.forEach(p => { if (p) p.style.width = ''; });
      pgPaneWidths = null;
      _mariaSave('ccadmin.pg.paneWidths', null);
    });

    sp.addEventListener('pointerdown', ev => {
      ev.preventDefault();
      const startX = ev.clientX;
      const startW = pane.getBoundingClientRect().width;
      sp.setPointerCapture(ev.pointerId);
      sp.classList.add('dragging');
      document.body.classList.add('sql-resizing');

      const onMove = e => {
        const max = Math.max(160, strip.getBoundingClientRect().width - 320);
        pane.style.width = Math.min(max, Math.max(140, startW + e.clientX - startX)) + 'px';
      };
      const onUp = () => {
        sp.removeEventListener('pointermove', onMove);
        sp.removeEventListener('pointerup', onUp);
        sp.removeEventListener('pointercancel', onUp);
        sp.classList.remove('dragging');
        document.body.classList.remove('sql-resizing');
        pgPaneWidths = panes.map(p => p ? Math.round(p.getBoundingClientRect().width) : 0);
        _mariaSave('ccadmin.pg.paneWidths', pgPaneWidths);
      };
      sp.addEventListener('pointermove', onMove);
      sp.addEventListener('pointerup', onUp);
      sp.addEventListener('pointercancel', onUp);
    });

    sp.addEventListener('keydown', ev => {
      const step = ev.shiftKey ? 40 : 12;
      if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return;
      ev.preventDefault();
      const w = pane.getBoundingClientRect().width + (ev.key === 'ArrowRight' ? step : -step);
      const max = Math.max(160, strip.getBoundingClientRect().width - 320);
      pane.style.width = Math.min(max, Math.max(140, w)) + 'px';
      pgPaneWidths = panes.map(p => p ? Math.round(p.getBoundingClientRect().width) : 0);
      _mariaSave('ccadmin.pg.paneWidths', pgPaneWidths);
    });
  });
}

function togglePgSection(name) {
  pgCollapsed[name] = !pgCollapsed[name];
  _mariaSave('ccadmin.pg.collapsed', pgCollapsed);
  const head = document.querySelector(`#pgDetail [data-sect="${name}"]`);
  const body = document.querySelector(`#pgDetail [data-sect-body="${name}"]`);
  head?.classList.toggle('collapsed', !!pgCollapsed[name]);
  body?.classList.toggle('d-none', !!pgCollapsed[name]);
  document.querySelector(`#pgDetail .sql-hsplit[data-hsplit="${name}"]`)
    ?.classList.toggle('d-none', !!pgCollapsed[name]);
}

/** Decoded view of one bytea column value. Same modal and same decoding
 *  (modules/maria/blobs.py is generic byte analysis, reused unchanged by
 *  modules/pg) as showBlobViewer(), pointed at /api/pg instead of
 *  /api/maria — kept as its own function rather than a shared one with a
 *  prefix argument so neither screen's blob button has to carry which
 *  backend it belongs to beyond its own data attribute name. */
async function showPgBlobViewer(qs) {
  const body = document.getElementById('blobViewerBody');
  const dl   = document.getElementById('blobViewerDownload');
  if (dl) dl.href = appUrl('/api/pg/blob?' + qs);
  body.innerHTML = '<div class="text-secondary small p-3">Decoding…</div>';
  const modal = new bootstrap.Modal(document.getElementById('blobViewerModal'));
  modal.show();

  const d = await api('/api/pg/blob/preview?' + qs);
  if (!d || d.error) {
    body.innerHTML = `<div class="alert alert-warning py-2 px-3 small mb-0">`
      + `${esc((d && d.error) || 'could not decode')}</div>`;
    return;
  }

  document.getElementById('blobViewerTitle').textContent =
    `${d.database}.${d.table}.${d.column}`;

  const header = `<div class="small text-secondary mb-2">
      ${esc(d.label)} · ${_fmtBytes(d.size)}</div>`;

  let main = '';
  if (d.json !== null && d.json !== undefined) {
    main = `<div class="small fw-semibold text-secondary mb-1">JSON payload</div>
      <pre class="bg-body-tertiary p-2 rounded" style="font-size:.75rem;max-height:45vh;
           overflow:auto;white-space:pre-wrap;word-break:break-word;">${
        esc(JSON.stringify(d.json, null, 2))}</pre>`;
  } else if (d.text) {
    main = `<pre class="bg-body-tertiary p-2 rounded" style="font-size:.75rem;
             max-height:45vh;overflow:auto;white-space:pre-wrap;">${esc(d.text)}</pre>`;
  }

  const strings = (d.strings || []).length
    ? `<details ${d.json ? '' : 'open'} class="mt-2">
         <summary class="small text-secondary">Readable strings (${d.strings.length})</summary>
         <pre class="bg-body-tertiary p-2 rounded mt-1" style="font-size:.72rem;
              max-height:30vh;overflow:auto;white-space:pre-wrap;">${
           esc(d.strings.join('\n'))}</pre>
       </details>`
    : '';

  const nothing = (!main && !strings)
    ? '<div class="text-secondary small">Nothing readable in these bytes — '
      + 'download it if you need the raw content.</div>' : '';

  body.innerHTML = header + main + strings + nothing;
}

/** Version + reachability onto the PostgreSQL node in the rail. */
async function loadPgHealth() {
  if (!can('pg.read')) return;
  const dot  = document.getElementById('db-pg-dot');
  const meta = document.getElementById('db-pg-meta');
  const d = await api('/api/pg/health');
  const ok = !!(d && d.connected);
  if (dot) dot.className = 'conn-dot ops-db-dot ' + (ok ? 'connected' : 'disconnected');
  if (meta) {
    // "PostgreSQL 18.3 (Debian 18.3-1.pgdg13+1) on x86_64-pc-linux-gnu, ..." —
    // trim to the part anyone reading a ~150px rail is actually checking.
    const v = ok ? String(d.version || '').split(' on ')[0] : '';
    meta.textContent = ok ? (v || 'connected') : 'not responding';
    meta.title = ok ? `${d.version || ''} · ${d.user || ''} (${d.credential_source || ''})`
                    : (d && d.error) || 'not responding';
  }
  const badge = document.getElementById('pgServer');
  if (badge) badge.textContent = ok ? `${d.host}:${d.port}` : '';
}

function _pgError(id, msg) {
  const box = document.getElementById(id);
  if (!box) return;
  box.classList.toggle('d-none', !msg);
  box.textContent = msg || '';
}

async function loadPgDatabases() {
  const showSystem = !!document.getElementById('pgShowSystem')?.checked;
  const pane = document.getElementById('pgSchemas');
  if (pane) pane.innerHTML = '<div class="text-secondary small p-3">Loading…</div>';

  const d = await api(`/api/pg/databases?include_system=${showSystem}`);
  if (!d || d.error) {
    _pgError('pgError', (d && d.error) || 'could not list databases');
    if (pane) pane.innerHTML = '<div class="text-secondary small p-3">—</div>';
    return;
  }
  _pgError('pgError', '');
  pgDatabases = d.databases || [];
  renderPgDatabases();
  _fillPgQuerySchemas();

  // Same reasoning as loadMariaSchemas: "Refresh" means refresh what is
  // actually on screen. Without this, a table's row-estimate (n_live_tup,
  // only updated when PostgreSQL next runs autovacuum/ANALYZE) could look
  // permanently stuck, and an already-open table's own sample rows would go
  // stale the moment something changed the data behind them.
  if (pgDatabase) await _refreshPgTableList(pgDatabase);
  if (pgTable) await selectPgTable(pgTable);
}

/** Re-fetch the table list for `name` WITHOUT selectPgDatabase's side
 *  effects (clearing the open table, resetting the search box) — those are
 *  right for a deliberate database change, wrong for a background refresh of
 *  the database that is already open. */
async function _refreshPgTableList(name) {
  const d = await api(`/api/pg/tables?database=${encodeURIComponent(name)}`);
  if (pgDatabase !== name) return;   // superseded by a database change meanwhile
  if (!d || d.error) return;         // keep showing the last good list
  pgTableList = d.tables || [];
  renderPgTables();
}

function renderPgDatabases() {
  const pane = document.getElementById('pgSchemas');
  if (!pane) return;
  if (!pgDatabases.length) {
    pane.innerHTML = '<div class="text-secondary small p-3">No databases.</div>';
    return;
  }
  pane.innerHTML = pgDatabases.map(s => `
    <button class="sql-item ${s.name === pgDatabase ? 'active' : ''}"
            data-schema="${esc(s.name)}">
      <div class="d-flex align-items-center gap-2">
        <span class="fw-semibold">${esc(s.title)}</span>
        ${s.catalogued ? '' : '<span class="badge bg-warning-subtle text-warning-emphasis" '
          + 'style="font-size:.6rem;" title="Not in the curated catalog — worth adding">new</span>'}
        <span class="ms-auto text-secondary" style="font-size:.68rem;">
          ${s.size_mb} MB
        </span>
      </div>
      ${s.description
        ? `<div class="sql-desc">${esc(s.description)}</div>`
        : `<div class="sql-desc font-monospace">${esc(s.name)}</div>`}
    </button>`).join('');
}

async function selectPgDatabase(name) {
  pgDatabase = name;
  pgTable = '';
  pgTableList = [];
  renderPgDatabases();
  document.getElementById('pgTablesTitle').textContent = `Tables — ${name}`;
  document.getElementById('pgDetailTitle').textContent = 'Table';
  document.getElementById('pgDetail').innerHTML =
    '<div class="text-secondary small p-3">Pick a table.</div>';
  const search = document.getElementById('pgTableSearch');
  if (search) search.value = '';

  const pane = document.getElementById('pgTables');
  pane.innerHTML = '<div class="text-secondary small p-3">Loading…</div>';
  const d = await api(`/api/pg/tables?database=${encodeURIComponent(name)}`);

  if (pgDatabase !== name) return;   // superseded by a later click

  if (!d || d.error) {
    pane.innerHTML = `<div class="text-danger small p-3">${esc((d && d.error) || 'failed')}</div>`;
    return;
  }
  pgTableList = d.tables || [];
  renderPgTables();
}

function renderPgTables() {
  const pane = document.getElementById('pgTables');
  if (!pane) return;
  const q = (document.getElementById('pgTableSearch')?.value || '').toLowerCase();
  const rows = pgTableList.filter(t => !q || t.name.toLowerCase().includes(q));
  if (!rows.length) {
    pane.innerHTML = '<div class="text-secondary small p-3">No matching tables.</div>';
    return;
  }
  pane.innerHTML = rows.map(t => `
    <button class="sql-item ${t.name === pgTable ? 'active' : ''}"
            data-table="${esc(t.name)}">
      <div class="d-flex align-items-center gap-2">
        <span class="font-monospace" style="font-size:.75rem;">${esc(t.name)}</span>
        <span class="ms-auto text-secondary" style="font-size:.68rem;"
              title="n_live_tup is refreshed by autovacuum/ANALYZE, not a live count">~${t.row_estimate} rows</span>
      </div>
      ${t.comment ? `<div class="sql-desc">${esc(t.comment)}</div>` : ''}
    </button>`).join('');
}

async function selectPgTable(name) {
  pgTable = name;
  renderPgTables();
  document.getElementById('pgDetailTitle').textContent = `${pgDatabase}.${name}`;
  const pane = document.getElementById('pgDetail');
  pane.innerHTML = '<div class="text-secondary small p-3">Loading…</div>';

  const qs = `database=${encodeURIComponent(pgDatabase)}&table=${encodeURIComponent(name)}`;
  const [cols, sample, keys] = await Promise.all([
    api(`/api/pg/columns?${qs}`),
    api(`/api/pg/sample?${qs}&size=25`),
    api(`/api/pg/keys?${qs}`),
  ]);

  if (pgTable !== name) return;

  if (cols && cols.error) {
    pane.innerHTML = `<div class="text-danger small p-3">${esc(cols.error)}</div>`;
    return;
  }

  pgColumns   = cols.columns || [];
  pgSample    = (sample && !sample.error) ? sample : null;
  pgRelations = (keys && !keys.error) ? keys : null;
  renderPgDetail(sample && sample.error ? sample.error : '');
}

function renderPgDetail(sampleError) {
  const pane = document.getElementById('pgDetail');
  if (!pane) return;

  const rowsHtml = sampleError
    ? `<div class="text-danger small p-2">${esc(sampleError)}</div>`
    : _pgTable(pgSample?.columns || [], pgSample?.rows || [], pgSample?.truncated,
              {database: pgDatabase, table: pgTable,
               primaryKey: pgSample?.primary_key || [],
               blobColumns: pgSample?.blob_columns || [],
               hidden: _pgHiddenSet(), editable: true});

  const hidden = _pgHiddenSet();
  const total  = (pgSample?.columns || []).length;
  const shown  = total - [...hidden].filter(c => (pgSample?.columns || []).includes(c)).length;

  pane.innerHTML = `
    <div class="sql-detail-section">
      ${_pgHead('columns', `Columns (${pgColumns.length})`)}
      <div class="sql-detail-body-section ${pgCollapsed.columns ? 'd-none' : ''}"
           data-sect-body="columns"${_pgSectionStyle('columns')}>${_pgColumnsTable()}</div>
    </div>
    ${_pgHSplit('columns')}

    <div class="sql-detail-section">
      ${_pgHead('relations', _pgRelationsLabel())}
      <div class="sql-detail-body-section ${pgCollapsed.relations ? 'd-none' : ''}"
           data-sect-body="relations"${_pgSectionStyle('relations')}>${_pgRelations()}</div>
    </div>
    ${_pgHSplit('relations')}

    <div class="sql-rows-section">
      <div class="sql-detail-head">
        <span>First rows</span>
        <span class="text-secondary" style="text-transform:none;font-weight:500;">
          ${shown === total ? `${total} columns` : `${shown} of ${total} columns`}
        </span>
        <button class="btn btn-sm btn-outline-secondary ms-auto py-0 px-2"
                data-pg-cols="1" style="font-size:.7rem;text-transform:none;"
                title="Choose which columns to show">
          <i class="bi bi-eye me-1"></i>Columns
        </button>
        <button class="btn btn-sm btn-outline-secondary py-0 px-2"
                data-pg-popout="1" style="font-size:.7rem;text-transform:none;"
                title="Open these rows in a separate window">
          <i class="bi bi-box-arrow-up-right"></i>
        </button>
      </div>
      <div class="sql-grid-scroll">${rowsHtml}</div>
    </div>`;
  syncSqlPopout();
}

function _pgSectionStyle(id) {
  const h = pgSectionH[id];
  return (h && !pgCollapsed[id]) ? ` style="height:${h}px;"` : '';
}

function _pgHSplit(id) {
  if (pgCollapsed[id]) return '';
  return `<div class="sql-hsplit" data-hsplit="${id}" role="separator"
               tabindex="0" aria-orientation="horizontal"
               title="Drag to resize · double-click to reset"></div>`;
}

function _pgHead(id, label) {
  return `<button class="sql-detail-head ${pgCollapsed[id] ? 'collapsed' : ''}"
                  data-sect="${id}" aria-expanded="${!pgCollapsed[id]}">
            <span>${esc(label)}</span>
            <i class="bi bi-chevron-down ops-caret"></i>
          </button>`;
}

function _pgColumnsTable() {
  if (!pgColumns.length)
    return '<div class="text-secondary small p-2">No columns.</div>';
  const body = pgColumns.map(c => `
    <tr>
      <td class="font-monospace">${esc(c.name)}</td>
      <td class="text-secondary">${esc(c.data_type)}</td>
      <td>${_pgKeyBadge(c)}</td>
      <td class="text-secondary">${c.nullable === 'YES' ? 'null' : ''}</td>
    </tr>`).join('');
  return `<table class="table table-sm table-hover mb-0" style="font-size:.74rem;">
      <thead class="table-light"><tr>
        <th>Name</th><th>Type</th><th>Key</th><th></th>
      </tr></thead><tbody>${body}</tbody></table>`;
}

/** Same badge as MariaDB's, but PostgreSQL's columns endpoint only ever marks
 *  PRI (see modules/pg/routers/browse.py::pg_columns) — there is no MUL/UNI
 *  equivalent surfaced there today, so the tooltip falls back to the index
 *  list alone. */
function _pgKeyBadge(col) {
  if (!col.key_type) return '';
  const idx = (pgRelations?.indexes || []).filter(i => i.columns.includes(col.name));
  const tip = idx.length
    ? idx.map(i => `${i.name} (${i.columns.join(', ')})`).join('\n')
    : 'Primary key';
  return `<span class="badge bg-primary-subtle text-primary-emphasis"
                style="font-size:.6rem;" title="${esc(tip)}">${esc(col.key_type)}</span>`;
}

function _pgRelationsLabel() {
  const r = pgRelations;
  if (!r) return 'Keys & relations';
  const n = (r.outbound?.length || 0) + (r.inbound?.length || 0);
  return n ? `Keys & relations (${n} declared)` : 'Keys & relations';
}

/** Same three-part answer as MariaDB's _mariaRelations(), minus the join
 *  tick-boxes and the "Build join query" bar — see this file's PostgreSQL
 *  section header for why the join builder itself is not here yet. */
function _pgRelations() {
  const r = pgRelations;
  if (!r) return '<div class="text-secondary small p-2">—</div>';

  const idx = (r.indexes || []).map(i => `
    <div class="sql-rel-row">
      <span class="badge bg-${i.primary ? 'primary' : 'secondary'}-subtle
                   text-${i.primary ? 'primary' : 'secondary'}-emphasis"
            style="font-size:.6rem;">${i.primary ? 'PRIMARY' : (i.unique ? 'UNIQUE' : 'INDEX')}</span>
      <span class="ms-1 text-secondary">${esc(i.primary ? '' : i.name)}</span>
      <span class="ms-1">${i.columns.map(c => `<span class="sql-chip">${esc(c)}</span>`).join(' + ')}</span>
    </div>`).join('');

  const link = (database, table, label) =>
    database === pgDatabase
      ? `<button class="sql-chip sql-chip-link" data-goto-table="${esc(table)}"
                 title="Open ${esc(table)}">${esc(label)}</button>`
      : `<span class="sql-chip">${esc(database)}.${esc(label)}</span>`;

  const out = (r.outbound || []).map(f => `
    <div class="sql-rel-row">
      <i class="bi bi-arrow-right-short text-primary"></i>
      ${f.columns.map(c => `<span class="sql-chip">${esc(c)}</span>`).join(' + ')}
      <span class="text-secondary mx-1">references</span>
      ${link(f.ref_schema, f.ref_table, f.ref_table)}
      <span class="text-secondary">.</span>
      ${f.ref_columns.map(c => `<span class="sql-chip">${esc(c)}</span>`).join(' + ')}
    </div>`).join('');

  const inb = (r.inbound || []).map(f => `
    <div class="sql-rel-row">
      <i class="bi bi-arrow-left-short text-success"></i>
      ${link(f.from_schema, f.from_table, f.from_table)}
      <span class="text-secondary">.</span>
      ${f.columns.map(c => `<span class="sql-chip">${esc(c)}</span>`).join(' + ')}
      <span class="text-secondary mx-1">references</span>
      ${f.ref_columns.map(c => `<span class="sql-chip">${esc(c)}</span>`).join(' + ')}
    </div>`).join('');

  const cand = (r.candidates || []).map(c => `
    <div class="sql-rel-row">
      <span class="sql-chip">${esc(c.column)}</span>
      <span class="text-secondary mx-1">also in ${c.count} table${c.count === 1 ? '' : 's'}:</span>
      ${c.tables.map(t => link(pgDatabase, t, t)).join(' ')}
      ${c.truncated ? '<span class="text-secondary"> …</span>' : ''}
    </div>`).join('');

  const section = (title, html, note) => html
    ? `<div class="px-2 pt-2 pb-1 small fw-semibold text-secondary">${esc(title)}</div>
       ${note ? `<div class="px-2 pb-1 text-secondary" style="font-size:10.5px;">${esc(note)}</div>` : ''}
       ${html}` : '';

  const body = section('Indexes', idx)
    + section('References out', out)
    + section('Referenced by', inb)
    + section('Possibly related', cand,
        'Matched on column name, not on a declared constraint — a strong hint '
        + 'about where to look next, not a guarantee that the values line up.');

  if (!body) return '<div class="text-secondary small p-2">No keys on this table.</div>';

  const none = (!out.length && !inb.length)
    ? `<div class="px-2 py-1 text-secondary" style="font-size:10.5px;">
         This database declares no FOREIGN KEY constraints on this table, so
         the relationships below are inferred rather than read from the catalog.
       </div>` : '';
  return none + body;
}

function openPgColumnPicker() {
  const pk = pgSample?.primary_key || [];
  const cols = (pgSample?.columns || []).length
    ? pgSample.columns : pgColumns.map(c => c.name);
  _openColumnPicker({
    title: `Columns — ${pgTable}`,
    columns: cols,
    locked: pk,
    hidden: _pgHiddenSet(),
    onChange: (hidden) => {
      pgHiddenCols[_pgTableKey()] = [...hidden];
      _mariaSave('ccadmin.pg.hiddenCols', pgHiddenCols);
      renderPgDetail();
    },
  });
}

/** Shared result-grid renderer for sample rows and query results — same
 *  contract as _mariaTable(), adapted for PostgreSQL's query-string keys
 *  (`database` rather than `schema`) and endpoint prefix. */
function _pgTable(columns, rows, truncated, ctx) {
  if (!rows.length) return '<div class="text-secondary small p-2">No rows.</div>';
  const pk = (ctx && ctx.primaryKey) || [];
  const hidden = (ctx && ctx.hidden) || new Set();
  const visible = columns.filter(c => !hidden.has(c) || pk.includes(c));
  if (!visible.length)
    return '<div class="text-secondary small p-2">Every column is hidden — '
         + 'use Columns to bring some back.</div>';

  const canEdit = !!(ctx && ctx.editable) && can('pg.write') && pk.length > 0;

  const head = visible.map(c => `<th class="text-nowrap">${esc(c)}</th>`).join('');
  const body = rows.map(r => '<tr>' + visible.map(c => {
    const v = r[c];
    if (v === null || v === undefined) {
      if (!canEdit || !_pgColEditable(c))
        return '<td class="text-secondary fst-italic">null</td>';
      const nkey = {}; for (const k of pk) nkey[k] = r[k];
      return `<td class="text-secondary fst-italic sql-cell-editable"`
           + ` title="NULL&#10;Double-click to edit" data-editcol="${esc(c)}"`
           + ` data-rk="${esc(JSON.stringify(nkey))}" data-val="" data-null="1">null</td>`;
    }

    // bytea column. The server sends a marker rather than the bytes — same
    // reasoning as MariaDB's BLOB marker, and the shape is identical
    // (modules/pg/client.py::_jsonable mirrors modules/maria/client.py's).
    if (v && typeof v === 'object' && v.__blob__) {
      const size = _fmtBytes(v.bytes || 0);
      if (!v.bytes) return `<td class="text-secondary fst-italic">empty blob</td>`;
      const blobCols = (ctx && ctx.blobColumns) || null;
      if (blobCols && !blobCols.includes(c))
        return `<td class="text-secondary" title="Binary value on a `
             + `non-binary column — not downloadable">binary · ${size}</td>`;
      if (!pk.length || !ctx)
        return `<td class="text-secondary" title="No primary key, so this row `
             + `cannot be addressed for download">binary · ${size}</td>`;
      const key = {}; for (const k of pk) key[k] = r[k];
      const qs = 'database=' + encodeURIComponent(ctx.database)
        + '&table=' + encodeURIComponent(ctx.table)
        + '&column=' + encodeURIComponent(c)
        + '&key=' + encodeURIComponent(JSON.stringify(key));
      return `<td class="text-nowrap">
                <button class="btn btn-link btn-sm p-0 text-decoration-none"
                        data-pg-blob-qs="${esc(qs)}" title="View ${esc(c)} (${size})">
                  <i class="bi bi-eye me-1"></i>${size}</button>
                <a href="${esc(appUrl('/api/pg/blob?' + qs))}" download
                   class="ms-2 text-secondary" title="Download raw bytes">
                   <i class="bi bi-download"></i></a></td>`;
    }

    const s = String(v);
    const shown = esc(s.length > 80 ? s.slice(0, 80) + '…' : s);
    if (!canEdit || !_pgColEditable(c)) {
      return `<td class="text-nowrap" title="${esc(s)}">${shown}</td>`;
    }
    const key = {}; for (const k of pk) key[k] = r[k];
    return `<td class="text-nowrap sql-cell-editable" title="${esc(s)}&#10;`
         + `Double-click to edit" data-editcol="${esc(c)}"`
         + ` data-rk="${esc(JSON.stringify(key))}"`
         + ` data-val="${esc(s)}">${shown}</td>`;
  }).join('') + '</tr>').join('');
  const ownScroller = !!(ctx && (ctx.editable || ctx.scroll));
  return `
    <div style="${ownScroller ? '' : 'overflow:auto;max-height:60vh;'}">
      <table class="table table-sm table-hover mb-0 font-monospace sql-grid" style="font-size:.72rem;">
        <thead><tr>${head}</tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>
    ${truncated ? '<div class="small text-warning-emphasis px-2 py-1">'
      + 'More rows exist — this result was capped.</div>' : ''}`;
}

/* ── Editing one cell ─────────────────────────────────────────────────────
   Mirrors the server's rules in modules/pg/writes.py, the same way MariaDB's
   client-side check mirrors modules/maria/writes.py — this decides what to
   OFFER, not what is PERMITTED, and the server re-checks every one of these. */
function _pgColEditable(name) {
  const c = pgColumns.find(x => x.name === name);
  if (!c) return false;
  if (c.key_type === 'PRI') return false;
  if ((c.is_identity || '').toUpperCase() === 'YES') return false;
  if (String(c.default_value || '').startsWith('nextval(')) return false;
  if ((c.is_generated || '').toUpperCase() === 'ALWAYS' || c.generation_expression) return false;
  return (c.data_type || '').toLowerCase() !== 'bytea';
}

function beginPgCellEdit(td) {
  if (td.querySelector('input')) return;
  const original = td.dataset.val ?? '';
  const wasNull  = td.dataset.null === '1';
  const width = Math.max(td.getBoundingClientRect().width, 90);
  td.innerHTML = `<input class="sql-cell-input" style="width:${Math.round(width)}px"`
               + `${wasNull ? ' placeholder="NULL"' : ''}>`;
  const input = td.querySelector('input');
  input.value = original;
  input.focus();
  input.select();

  let settled = false;
  const revert = () => {
    if (settled) return;
    settled = true;
    td.innerHTML = _pgCellHtml(original, wasNull);
  };
  input.addEventListener('keydown', ev => {
    if (ev.key === 'Escape') { ev.preventDefault(); revert(); }
    else if (ev.key === 'Enter') {
      ev.preventDefault();
      if (settled) return;
      settled = true;
      commitPgCellEdit(td, original, input.value, wasNull);
    }
  });
  input.addEventListener('blur', revert);
}

function _pgCellHtml(value, isNull) {
  if (isNull) return '<em>null</em>';
  return esc(value.length > 80 ? value.slice(0, 80) + '…' : value);
}

async function commitPgCellEdit(td, before, after, wasNull) {
  const col = td.dataset.editcol;
  const key = JSON.parse(td.dataset.rk);
  td.innerHTML = _pgCellHtml(before, wasNull);

  if (after === before && !(wasNull && after !== '')) return;

  const keyText = Object.entries(key).map(([k, v]) => `${k} = ${v}`).join(' AND ');
  const ok = await _pgConfirmEdit({
    target: `${pgDatabase}.${pgTable}.${col}`,
    where: keyText, before: wasNull ? 'NULL' : before, after,
  });
  if (!ok) return;

  const d = await api('/api/pg/cell', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      database: pgDatabase, table: pgTable, column: col, key,
      value: after, expected: wasNull ? null : before, expected_null: !!wasNull,
    }),
  });

  if (!d || d.error) {
    _pgError('pgError', (d && d.error) || 'the edit did not go through');
    return;
  }
  _pgError('pgError', '');
  const row = (pgSample?.rows || []).find(
    r => Object.entries(key).every(([k, v]) => String(r[k]) === String(v)));
  if (row) row[col] = after;
  renderPgDetail();
  document.querySelectorAll(`td[data-editcol="${CSS.escape(col)}"]`).forEach(cell => {
    if (cell.dataset.rk === td.dataset.rk) cell.classList.add('sql-cell-edited');
  });
}

/** Same confirmation as MariaDB's, with the statement it shows adjusted to
 *  what the server actually runs: double-quoted identifiers, and no LIMIT —
 *  PostgreSQL has no LIMIT clause on UPDATE (modules/pg/writes.py addresses
 *  the row by its full primary key instead, which is unique by definition). */
function _pgConfirmEdit(o) {
  return new Promise(resolve => {
    const wrap = document.createElement('div');
    wrap.className = 'rt-modal-overlay';
    const stmt = `UPDATE "public"."${pgTable}"\n   SET "${o.target.split('.').pop()}" = `
      + `'${o.after}'\n WHERE ${o.where};`;
    wrap.innerHTML = `<div class="rt-modal" style="max-width:560px;">
        <div class="rt-modal-title">⚠ Edit a row on this CC</div>
        <div class="rt-modal-body">
          <div class="small mb-2">
            This changes live data in the CC's configuration database. It is
            not reversible from here.
          </div>
          <table class="table table-sm mb-2" style="font-size:.76rem;">
            <tr><td class="text-secondary">Cell</td>
                <td class="font-monospace">${esc(o.target)}</td></tr>
            <tr><td class="text-secondary">Row</td>
                <td class="font-monospace">${esc(o.where)}</td></tr>
            <tr><td class="text-secondary">From</td>
                <td class="font-monospace">${esc(o.before) || '<em>empty</em>'}</td></tr>
            <tr><td class="text-secondary">To</td>
                <td class="font-monospace fw-semibold">${esc(o.after) || '<em>empty</em>'}</td></tr>
          </table>
          <pre class="bg-body-tertiary p-2 rounded mb-0" style="font-size:.72rem;
               white-space:pre-wrap;">${esc(stmt)}</pre>
        </div>
        <div class="rt-modal-actions">
          <button class="btn btn-sm btn-warning" data-ok="1">Apply the change</button>
          <button class="btn btn-sm btn-outline-secondary" data-ok="0">Cancel</button>
        </div></div>`;
    document.body.appendChild(wrap);
    const done = v => { wrap.remove(); document.removeEventListener('keydown', onKey); resolve(v); };
    const onKey = e => { if (e.key === 'Escape') done(false); };
    document.addEventListener('keydown', onKey);
    wrap.addEventListener('click', e => {
      const b = e.target.closest('button');
      if (b) { done(b.getAttribute('data-ok') === '1'); return; }
      if (e.target === wrap) done(false);
    });
  });
}

function _fillPgQuerySchemas() {
  const sel = document.getElementById('pgQuerySchema');
  if (!sel) return;
  const keep = sel.value;
  sel.innerHTML = '<option value="">(none)</option>'
    + pgDatabases.map(s => `<option value="${esc(s.name)}">${esc(s.name)}</option>`).join('');
  sel.value = keep || (pgDatabases.find(s => !s.system)?.name || '');
}

let _pgQueryCols = [];
let _pgQueryRows = [];
let _pgQueryMeta = null;
let _pgQueryHidden = new Set();

async function runPgQuery() {
  const sql = (document.getElementById('pgQuerySql')?.value || '').trim();
  if (!sql) return;
  const database = document.getElementById('pgQuerySchema')?.value || '';
  const limit = parseInt(document.getElementById('pgQueryLimit')?.value, 10) || null;

  const out  = document.getElementById('pgQueryResults');
  const meta = document.getElementById('pgQueryMeta');
  out.innerHTML = '<div class="text-secondary small p-3">Running…</div>';
  meta.textContent = '';
  _pgError('pgQueryError', '');

  const d = await api('/api/pg/query', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sql, database, limit }),
  });

  if (!d || d.error) {
    _pgError('pgQueryError', (d && d.error) || 'query failed');
    out.innerHTML = '<div class="text-secondary small p-3">—</div>';
    document.getElementById('pgQueryColsBtn')?.classList.add('d-none');
    document.getElementById('pgQueryPopoutBtn')?.classList.add('d-none');
    return;
  }

  _pgQueryCols = d.columns || [];
  _pgQueryRows = d.rows || [];
  _pgQueryMeta = d;
  _pgQueryHidden = new Set([..._pgQueryHidden].filter(c => _pgQueryCols.includes(c)));

  document.getElementById('pgQueryColsBtn')
    ?.classList.toggle('d-none', !_pgQueryCols.length);
  document.getElementById('pgQueryPopoutBtn')
    ?.classList.toggle('d-none', !_pgQueryCols.length);
  renderPgQueryResults();
}

function renderPgQueryResults() {
  const out  = document.getElementById('pgQueryResults');
  const meta = document.getElementById('pgQueryMeta');
  const d = _pgQueryMeta;
  if (!out || !d) return;

  const shown = _pgQueryCols.length - _pgQueryHidden.size;
  meta.textContent = `${d.count} row(s) · ${d.took_ms} ms`
    + (d.truncated ? ` · capped at ${d.row_cap}` : '')
    + (_pgQueryHidden.size ? ` · ${shown} of ${_pgQueryCols.length} columns` : '');

  out.innerHTML = _pgTable(_pgQueryCols, _pgQueryRows, d.truncated,
                           { hidden: _pgQueryHidden, scroll: true });
  syncSqlPopout();
}

function openPgQueryColumnPicker() {
  _openColumnPicker({
    title: 'Columns — query results',
    columns: _pgQueryCols,
    locked: [],
    hidden: _pgQueryHidden,
    onChange: (hidden) => { _pgQueryHidden = hidden; renderPgQueryResults(); },
  });
}

/** PostgreSQL identifier quoting — double quotes, doubled to escape one
 *  embedded, the same rule _q() applies with backticks for MariaDB. */
function _pgQuote(name) { return '"' + String(name).replace(/"/g, '""') + '"'; }

/** "Build query" for PostgreSQL — same wizard as openMariaQueryWizard(),
 *  adapted for what is actually different here: identifiers are double-quoted
 *  not backtick-quoted, a table lives unqualified in the selected database's
 *  `public` schema rather than behind a `schema.table` prefix (PostgreSQL has
 *  no cross-database FROM — the database picker already scopes the
 *  connection, matching the bare-name placeholder the query box itself
 *  shows), and the column/table catalogs come off /api/pg/*, not /api/maria/*.
 *  _mariaWhereSql/_sqlLit are reused as-is — building `col op value` and
 *  quoting a string literal are the same problem in both dialects. */
async function openPgQueryWizard() {
  document.querySelector('.rt-modal-overlay.rt-pgquerywiz')?.remove();
  if (!pgDatabases.length) await loadPgDatabases();

  const wrap = document.createElement('div');
  wrap.className = 'rt-modal-overlay rt-pgquerywiz';
  wrap.innerHTML = `<div class="rt-modal" style="width:min(900px,94vw);">
      <div class="rt-modal-title"><i class="bi bi-magic me-1"></i>Build a query</div>
      <div class="rt-modal-body" style="white-space:normal;">

        <div class="d-flex align-items-center gap-2 mb-2 flex-wrap">
          <div class="btn-group btn-group-sm" role="group">
            <input type="radio" class="btn-check" name="pgWizVerb" id="pgWizSelect" checked>
            <label class="btn btn-outline-primary py-0 px-3" for="pgWizSelect"
                   style="font-size:.75rem;">SELECT</label>
            <input type="radio" class="btn-check" name="pgWizVerb" id="pgWizUpdate" disabled>
            <label class="btn btn-outline-secondary py-0 px-3 disabled" for="pgWizUpdate"
                   style="font-size:.75rem;"
                   title="Not available — see the note below">UPDATE</label>
            <input type="radio" class="btn-check" name="pgWizVerb" id="pgWizDelete" disabled>
            <label class="btn btn-outline-secondary py-0 px-3 disabled" for="pgWizDelete"
                   style="font-size:.75rem;"
                   title="Not available — see the note below">DELETE</label>
          </div>
          <label class="small text-secondary mb-0 ms-2">Database</label>
          <select class="form-select form-select-sm wiz-schema" style="width:170px;font-size:.75rem;">
            ${pgDatabases.map(s => `<option value="${esc(s.name)}"
              ${s.name === pgDatabase ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}
          </select>
          <label class="small text-secondary mb-0 ms-1">Table</label>
          <select class="form-select form-select-sm wiz-table"
                  style="width:220px;font-size:.75rem;"><option>loading…</option></select>
        </div>

        <div class="alert alert-secondary py-1 px-2 small mb-2" style="font-size:.72rem;">
          <b>UPDATE and DELETE are not offered.</b> This screen runs through a
          read-only connection, so the server would refuse them. Changing data
          needs the per-row edit in the table browser, which is separately
          gated and audited.
        </div>

        <div class="mj-filters mb-2">
          <div class="mj-filters-head">
            <i class="bi bi-list-columns me-1"></i><span>Columns</span>
            <span class="text-secondary fw-normal ms-1" style="text-transform:none;">
              — none ticked means all</span>
            <button class="btn btn-sm btn-outline-secondary py-0 px-2 ms-auto wiz-cols-none"
                    style="font-size:.7rem;">Unselect all</button>
            <button class="btn btn-sm btn-outline-secondary py-0 px-2 wiz-cols-all"
                    style="font-size:.7rem;">Select all</button>
          </div>
          <div class="wiz-cols" style="max-height:120px;overflow:auto;padding:6px 8px;
               display:flex;flex-wrap:wrap;gap:4px 12px;"></div>
        </div>

        <div class="mj-filters mb-2">
          <div class="mj-filters-head">
            <i class="bi bi-funnel me-1"></i><span>Conditions</span>
            <span class="text-secondary fw-normal ms-1" style="text-transform:none;">
              — combined with AND</span>
            <button class="btn btn-sm btn-outline-primary py-0 px-2 ms-auto wiz-addwhere"
                    style="font-size:.7rem;"><i class="bi bi-plus-lg me-1"></i>Add condition</button>
          </div>
          <div class="wiz-where"></div>
        </div>

        <div class="d-flex align-items-center gap-2 mb-2 flex-wrap">
          <label class="small text-secondary mb-0">Order by</label>
          <select class="form-select form-select-sm wiz-order" style="width:200px;font-size:.75rem;">
            <option value="">(none)</option>
          </select>
          <select class="form-select form-select-sm wiz-dir" style="width:90px;font-size:.75rem;">
            <option value="ASC">ASC</option><option value="DESC">DESC</option>
          </select>
          <label class="small text-secondary mb-0 ms-2">Limit</label>
          <input type="number" min="1" max="10000" value="200"
                 class="form-control form-control-sm wiz-limit" style="font-size:.75rem;width:90px;">
        </div>

        <div class="small fw-semibold text-secondary mb-1">Generated SQL</div>
        <textarea class="form-control font-monospace wiz-sql" rows="7" wrap="off"
                  spellcheck="false" readonly
                  style="font-size:.74rem;white-space:pre;overflow-x:auto;"></textarea>
      </div>
      <div class="rt-modal-actions">
        <button class="btn btn-sm btn-primary" data-act="run">
          <i class="bi bi-play-fill me-1"></i>Run</button>
        <button class="btn btn-sm btn-outline-primary" data-act="use">Put in editor</button>
        <button class="btn btn-sm btn-outline-secondary" data-act="close">Close</button>
      </div>
    </div>`;
  document.body.appendChild(wrap);

  const done = () => { wrap.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = e => { if (e.key === 'Escape') done(); };
  document.addEventListener('keydown', onKey);

  let columns = [];

  const regen = () => {
    const table = wrap.querySelector('.wiz-table').value;
    if (!table) { wrap.querySelector('.wiz-sql').value = ''; return; }
    const picked = [...wrap.querySelectorAll('.wiz-cols input:checked')]
      .map(cb => cb.dataset.col);
    const cols = picked.length && picked.length !== columns.length
      ? picked.map(c => _pgQuote(c)).join(',\n       ') : '*';

    const where = [...wrap.querySelectorAll('.wiz-wrow')].map(r =>
      _mariaWhereSql(_pgQuote(r.querySelector('.wiz-col').value),
                     r.querySelector('.wiz-op').value,
                     r.querySelector('.wiz-val')?.value ?? '')).filter(Boolean);

    const order = wrap.querySelector('.wiz-order').value;
    const lines = [`SELECT ${cols}`, `  FROM ${_pgQuote(table)}`];
    if (where.length) lines.push(' WHERE ' + where.join('\n   AND '));
    if (order) lines.push(` ORDER BY ${_pgQuote(order)} ${wrap.querySelector('.wiz-dir').value}`);
    lines.push(` LIMIT ${Math.max(1, parseInt(wrap.querySelector('.wiz-limit').value, 10) || 200)}`);
    wrap.querySelector('.wiz-sql').value = lines.join('\n');
  };

  const addWhere = () => {
    const row = document.createElement('div');
    row.className = 'wiz-wrow d-flex align-items-center gap-1 mb-1';
    row.innerHTML = `
      <select class="form-select form-select-sm wiz-col font-monospace"
              style="font-size:.72rem;flex:1 1 auto;min-width:0;">
        ${columns.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('')}
      </select>
      <select class="form-select form-select-sm wiz-op" style="width:120px;font-size:.72rem;">
        ${['=', '!=', 'LIKE', 'IN', '>', '<', '>=', '<=', 'IS NULL', 'IS NOT NULL']
          .map(o => `<option>${o}</option>`).join('')}
      </select>
      <input type="text" class="form-control form-control-sm wiz-val" placeholder="value"
             style="font-size:.72rem;width:180px;">
      <button class="btn btn-sm btn-link text-secondary py-0 px-1 wiz-del"
              title="Remove"><i class="bi bi-x-lg"></i></button>`;
    wrap.querySelector('.wiz-where').appendChild(row);
    const op = row.querySelector('.wiz-op');
    op.addEventListener('change', () => {
      const none = op.value === 'IS NULL' || op.value === 'IS NOT NULL';
      row.querySelector('.wiz-val').classList.toggle('d-none', none);
    });
  };

  const loadColumns = async () => {
    const database = wrap.querySelector('.wiz-schema').value;
    const table  = wrap.querySelector('.wiz-table').value;
    const host = wrap.querySelector('.wiz-cols');
    if (!table) { host.innerHTML = ''; columns = []; return; }
    host.innerHTML = '<span class="text-secondary small">loading…</span>';
    const d = await api(`/api/pg/columns?database=${encodeURIComponent(database)}`
                      + `&table=${encodeURIComponent(table)}`);
    columns = (d && !d.error && d.columns) ? d.columns.map(c => c.name) : [];
    host.innerHTML = columns.map(c => `
      <label class="d-flex align-items-center gap-1" style="font-size:.72rem;">
        <input type="checkbox" data-col="${esc(c)}">
        <span class="font-monospace">${esc(c)}</span>
      </label>`).join('') || '<span class="text-secondary small">no columns</span>';
    wrap.querySelector('.wiz-order').innerHTML = '<option value="">(none)</option>'
      + columns.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
    wrap.querySelector('.wiz-where').innerHTML = '';   // stale columns
    regen();
  };

  const loadTables = async () => {
    const database = wrap.querySelector('.wiz-schema').value;
    const sel = wrap.querySelector('.wiz-table');
    sel.innerHTML = '<option>loading…</option>';
    const d = await api(`/api/pg/tables?database=${encodeURIComponent(database)}`);
    const tables = (d && !d.error && d.tables) ? d.tables : [];
    sel.innerHTML = tables.map(t => `<option value="${esc(t.name)}"
      ${t.name === pgTable ? 'selected' : ''}>${esc(t.name)}</option>`).join('')
      || '<option value="">(no tables)</option>';
    await loadColumns();
  };

  wrap.addEventListener('change', async ev => {
    if (ev.target.closest('.wiz-schema')) { await loadTables(); return; }
    if (ev.target.closest('.wiz-table'))  { await loadColumns(); return; }
    regen();
  });
  wrap.addEventListener('input', regen);
  wrap.addEventListener('click', ev => {
    if (ev.target.closest('.wiz-addwhere')) { addWhere(); regen(); return; }
    if (ev.target.closest('.wiz-del')) { ev.target.closest('.wiz-wrow').remove(); regen(); return; }
    if (ev.target.closest('.wiz-cols-all')) {
      wrap.querySelectorAll('.wiz-cols input').forEach(cb => cb.checked = true); regen(); return;
    }
    if (ev.target.closest('.wiz-cols-none')) {
      wrap.querySelectorAll('.wiz-cols input').forEach(cb => cb.checked = false); regen(); return;
    }
    const act = ev.target.closest('[data-act]')?.dataset.act;
    if (act === 'close' || ev.target === wrap) { done(); return; }
    if (act === 'use' || act === 'run') {
      const sql = wrap.querySelector('.wiz-sql').value;
      const database = wrap.querySelector('.wiz-schema').value;
      done();
      const sel = document.getElementById('pgQuerySchema');
      if (sel) {
        if (![...sel.options].some(o => o.value === database)) sel.add(new Option(database, database));
        sel.value = database;
      }
      document.getElementById('pgQueryLimit').value =
        wrap.querySelector('.wiz-limit').value || 200;
      document.getElementById('pgQuerySql').value = sql;
      if (act === 'run') runPgQuery();
    }
  });

  await loadTables();
  addWhere();
  regen();
}

/* ══════════════════════════════════════════════════════════════════════════
   SQL results — dock-aside viewer (MariaDB + PostgreSQL)
   ══════════════════════════════════════════════════════════════════════════
   The same convenience ES's results pop-out offers — keep the data visible
   in its own window while working elsewhere — for the four SQL result grids:
   table content and query results, for both stores.

   Deliberately a fresh, smaller build rather than a parameterisation of ES's
   pop-out (frontend/static/js/app.js's RV_QUERY/RV_INDEX machinery). That one
   carries write-mode, ES-query-generation-from-filters and group-by
   aggregation — none of which have a SQL equivalent — plus row/column
   selection and sort/filter state per viewer. Threading "not applicable
   here" through all of it would cost more than it would share. What IS
   shared: _openColumnPicker (already generic), the sql-* CSS classes, and
   the same window.open shell shape.

   One popout window, reused across all four sources — the same way ES
   reuses one window between the Query Editor and Index Detail — selected by
   `sqlActiveViewerKey`. Its buttons call back via `window.opener.<fn>()`
   rather than copying function references onto the popout the way ES does:
   simpler to reason about, since every action explicitly says which
   document it means to touch (sqlResultsWindow.document) instead of relying
   on which window a copied closure happened to remember.
*/
let sqlResultsWindow = null;
let sqlPopoutView = 'table';        // 'table' | 'json' | 'csv'
let sqlActiveViewerKey = null;      // key into _sqlViewers

const _sqlViewers = {
  'maria-table': {
    label: () => `${mariaSchema}.${mariaTable}`,
    rows: () => mariaSample?.rows || [],
    cols: () => mariaSample?.columns || [],
    hidden: () => _mariaHiddenSet(),
    onHiddenChange: (hidden) => {
      mariaHiddenCols[_mariaTableKey()] = [...hidden];
      _mariaSave('ccadmin.maria.hiddenCols', mariaHiddenCols);
      renderMariaDetail();
    },
    refresh: () => selectMariaTable(mariaTable),
  },
  'maria-query': {
    label: () => 'MariaDB query',
    rows: () => _mariaQueryRows,
    cols: () => _mariaQueryCols,
    hidden: () => _mariaQueryHidden,
    onHiddenChange: (hidden) => { _mariaQueryHidden = hidden; renderMariaQueryResults(); },
    refresh: () => runMariaQuery(),
  },
  'pg-table': {
    label: () => `${pgDatabase}.${pgTable}`,
    rows: () => pgSample?.rows || [],
    cols: () => pgSample?.columns || [],
    hidden: () => _pgHiddenSet(),
    onHiddenChange: (hidden) => {
      pgHiddenCols[_pgTableKey()] = [...hidden];
      _mariaSave('ccadmin.pg.hiddenCols', pgHiddenCols);
      renderPgDetail();
    },
    refresh: () => selectPgTable(pgTable),
  },
  'pg-query': {
    label: () => 'PostgreSQL query',
    rows: () => _pgQueryRows,
    cols: () => _pgQueryCols,
    hidden: () => _pgQueryHidden,
    onHiddenChange: (hidden) => { _pgQueryHidden = hidden; renderPgQueryResults(); },
    refresh: () => runPgQuery(),
  },
};

function _sqlCurrentViewer() {
  return sqlActiveViewerKey ? _sqlViewers[sqlActiveViewerKey] : null;
}

/** One cell, as plain text — shared by the table view and CSV export. A blob
 *  marker (modules/maria and modules/pg both send `{__blob__:true,bytes:N}`
 *  rather than raw bytes) gets a description instead of [object Object];
 *  the live preview/download button next to it in the MAIN grid is not
 *  reproduced here — this window is a read-only convenience view, not a
 *  second interactive copy of the screen. */
function _sqlCellText(v) {
  if (v === null || v === undefined) return 'null';
  if (typeof v === 'object') {
    return v.__blob__ ? `<binary: ${_fmtBytes(v.bytes || 0)}>` : JSON.stringify(v);
  }
  const s = String(v);
  return s.length > 200 ? s.slice(0, 200) + '…' : s;
}

/** Read-only grid — no cell editing, no blob viewer button, for the same
 *  reason _sqlCellText doesn't reproduce one: this window is for looking at
 *  data conveniently, not a second place to change it. */
function _sqlPopoutTableHtml(cols, rows, hidden) {
  if (!rows.length) return '<div class="text-secondary p-3">No rows.</div>';
  const visible = cols.filter(c => !hidden.has(c));
  if (!visible.length) return '<div class="text-secondary p-3">Every column is hidden.</div>';
  const head = visible.map(c => `<th class="text-nowrap">${esc(c)}</th>`).join('');
  const body = rows.map(r => '<tr>' + visible.map(c => {
    const v = r[c];
    const cls = (v === null || v === undefined) ? ' class="text-secondary fst-italic"' : '';
    return `<td${cls}>${esc(_sqlCellText(v))}</td>`;
  }).join('') + '</tr>').join('');
  return `<table class="table table-sm table-striped table-dark mb-0 font-monospace"
               style="font-size:.78rem;white-space:nowrap;">
      <thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

/** CSV text for the current viewer — its own small builder rather than ES's
 *  buildResultsCsv(), which is tied to hit/mapping-aware helpers (date
 *  columns, resultColumns()) that a plain SQL row does not have. */
function _sqlBuildCsv(cols, rows, hidden) {
  const visible = cols.filter(c => !hidden.has(c));
  const escCsv = (s) => /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  const cellRaw = (v) => {
    if (v === null || v === undefined) return '';
    if (typeof v === 'object') return v.__blob__ ? `<binary: ${_fmtBytes(v.bytes || 0)}>` : JSON.stringify(v);
    return String(v);
  };
  const lines = [visible.map(escCsv).join(',')];
  for (const r of rows) lines.push(visible.map(c => escCsv(cellRaw(r[c]))).join(','));
  return lines.join('\r\n');
}

function sqlPopoutBodyHtml() {
  const v = _sqlCurrentViewer();
  if (!v) return '<div class="text-secondary p-3">Nothing to show yet.</div>';
  const rows = v.rows(), cols = v.cols(), hidden = v.hidden();
  if (sqlPopoutView === 'json') {
    return rows.length
      ? `<pre class="p-2 mb-0" style="white-space:pre-wrap;word-break:break-word;">${esc(JSON.stringify(rows, null, 2))}</pre>`
      : '<div class="text-secondary p-3">No rows.</div>';
  }
  if (sqlPopoutView === 'csv') {
    return rows.length
      ? `<pre class="p-2 mb-0" style="white-space:pre;overflow:auto;">${esc(_sqlBuildCsv(cols, rows, hidden))}</pre>`
      : '<div class="text-secondary p-3">No rows.</div>';
  }
  return _sqlPopoutTableHtml(cols, rows, hidden);
}

/** Refresh the popout's content, title and toolbar state from whichever
 *  viewer is currently selected. Called after every render of any of the
 *  four source screens (harmless when the popout is closed, or open on a
 *  DIFFERENT viewer than the one that just changed — _sqlCurrentViewer()
 *  only reflects data for the selected key). */
function syncSqlPopout() {
  if (!sqlResultsWindow || sqlResultsWindow.closed) return;
  const v = _sqlCurrentViewer();
  const doc = sqlResultsWindow.document;
  const title = doc.getElementById('title');
  const meta  = doc.getElementById('meta');
  const out   = doc.getElementById('out');
  if (title) title.textContent = v ? v.label() : '';
  if (meta)  meta.textContent  = v ? `${v.rows().length} row(s)` : '';
  if (out)   out.innerHTML = sqlPopoutBodyHtml();
  ['table', 'json', 'csv'].forEach(m =>
    doc.getElementById('po-' + m)?.classList.toggle('active', m === sqlPopoutView));
}

function setSqlPopoutView(view) {
  sqlPopoutView = view;
  syncSqlPopout();
}

/** Re-run whatever produced the current viewer's rows (re-select the table,
 *  or re-run the query) and reflect the fresh data in the popout. */
async function refreshSqlPopoutSource() {
  const v = _sqlCurrentViewer();
  if (!v) return;
  await v.refresh();
  syncSqlPopout();
}

/** _openColumnPicker already takes a `doc` — this just points it at the
 *  popout's own document, so the modal appears in the window the user is
 *  actually looking at rather than yanking focus back to the main one. */
function openSqlPopoutColumns() {
  const v = _sqlCurrentViewer();
  if (!v || !sqlResultsWindow || sqlResultsWindow.closed) return;
  _openColumnPicker({
    doc: sqlResultsWindow.document,
    title: `Columns — ${v.label()}`,
    columns: v.cols(),
    locked: [],
    hidden: v.hidden(),
    onChange: (hidden) => { v.onHiddenChange(hidden); syncSqlPopout(); },
  });
}

function downloadSqlPopoutResults() {
  const v = _sqlCurrentViewer();
  if (!v) return;
  const rows = v.rows(), cols = v.cols(), hidden = v.hidden();
  if (!rows.length) { showToast('No rows to export', 'bg-warning'); return; }
  const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  let content, mime, ext;
  if (sqlPopoutView === 'json') {
    content = JSON.stringify(rows, null, 2);
    mime = 'application/json'; ext = 'json';
  } else {
    content = _sqlBuildCsv(cols, rows, hidden);
    mime = 'text/csv'; ext = 'csv';
  }
  const blob = new Blob([content], { type: mime + ';charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `sql_results_${ts}.${ext}`;
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
}

/** Open (or focus) the shared SQL results window, showing `key`'s data. */
function popOutSqlResults(key) {
  sqlActiveViewerKey = key;
  if (sqlResultsWindow && !sqlResultsWindow.closed) { sqlResultsWindow.focus(); syncSqlPopout(); return; }
  sqlResultsWindow = window.open('', 'cc_sql_results', 'width=860,height=800,scrollbars=yes,resizable=yes');
  if (!sqlResultsWindow) { showToast('Pop-up blocked — allow pop-ups for this site', 'bg-danger'); return; }
  sqlResultsWindow.document.write(`<!DOCTYPE html><html lang="en" data-bs-theme="dark"><head><meta charset="utf-8"/>
    <title>CC Admin — SQL Results</title>
    <link href="${appUrl('/static/vendor/bootstrap.min.css')}" rel="stylesheet"/>
    <link href="${appUrl('/static/vendor/bootstrap-icons.min.css')}" rel="stylesheet"/>
    <link rel="stylesheet" href="${appUrl('/static/css/style.css')}"/>
    <style>
      body{margin:0;background:#1e2530;color:#c9d1d9;font-family:Consolas,'Courier New',monospace;}
      header{background:#11161d;padding:8px 12px;border-bottom:1px solid #343a40;
             font-size:.8rem;color:#8aa;display:flex;gap:10px;align-items:center;flex-wrap:wrap;}
      #meta{color:#9ab;font-size:.75rem;margin-right:auto;}
      #out{padding:10px;overflow:auto;}
      .btn-group .btn.active{background:#0052CC;color:#fff;border-color:#0052CC;}
    </style></head><body>
    <header>
      <strong id="title" style="color:#5cc8ff;"></strong><span id="meta"></span>
      <div class="btn-group btn-group-sm" role="group">
        <button type="button" class="btn btn-outline-light py-0 px-2" id="po-table"
                onclick="window.opener.setSqlPopoutView('table')">Table</button>
        <button type="button" class="btn btn-outline-light py-0 px-2" id="po-json"
                onclick="window.opener.setSqlPopoutView('json')">JSON</button>
        <button type="button" class="btn btn-outline-light py-0 px-2" id="po-csv"
                onclick="window.opener.setSqlPopoutView('csv')">CSV</button>
      </div>
      <button class="btn btn-sm btn-outline-success py-0 px-2"
              onclick="window.opener.downloadSqlPopoutResults()" title="Download shown">
        <i class="bi bi-download"></i></button>
      <button class="btn btn-sm btn-outline-light py-0 px-2"
              onclick="window.opener.openSqlPopoutColumns()" title="Choose which columns to show">
        <i class="bi bi-eye me-1"></i>Columns</button>
      <button class="btn btn-sm btn-outline-light py-0 px-2"
              onclick="window.opener.refreshSqlPopoutSource()" title="Reload the data">
        <i class="bi bi-arrow-clockwise"></i></button>
    </header>
    <div id="out"><pre class="p-3 text-secondary mb-0">Loading…</pre></div></body></html>`);
  sqlResultsWindow.document.close();
  syncSqlPopout();
}

/* ══════════════════════════════════════════════════════════════════════════
   UPDATES — "a newer version of this tool is in the repository"
   ══════════════════════════════════════════════════════════════════════════
   The server checks the repository in the background (core/updater.py);
   here we just render what it found and, when the deployment supports it, ask
   it to pull + restart. The update restarts the app for everyone, so the other
   connected users are notified by the server before it happens. */

let _update = null;
const UPDATE_POLL_MS = 10 * 60 * 1000;

function _verLabel(v) {
  const ver = (v && v.version) || '?';
  return v && v.commit ? `${ver} (${v.commit})` : ver;
}

function renderUpdateBadge() {
  const btn = document.getElementById('updateBtn');
  const txt = document.getElementById('updateBtnText');
  const ver = document.getElementById('appVersion');
  const u = _update;
  if (ver) {
    const installed = (u && u.current && u.current.version) || '';
    ver.textContent = installed ? 'v' + installed : '';
    let tip = u ? `Installed: ${_verLabel(u.current)}\nUpdate source: ${u.mode}`
                : 'Installed version';
    if (installed === '0.0.0') {
      tip += '\n\nThis build reports no version: the VERSION file is missing '
           + 'from the image. Pull the latest code and rebuild.';
    }
    if (u && u.ok === false && u.error) {
      tip += `\n\nUpdate check failed: ${u.error}`;
    }
    tip += '\n\nClick for update details.';
    ver.title = tip;
    // Flag a build that can't report its version, and a check that isn't working.
    ver.className = (installed === '0.0.0' || (u && u.ok === false))
      ? 'text-warning' : 'text-secondary';
  }
  if (!btn || !txt) return;
  if (u && u.update_available) {
    btn.classList.remove('d-none');
    txt.textContent = `Update to ${(u.latest && u.latest.version) || 'new version'}`;
    btn.title = `A newer version is available: ${_verLabel(u.latest)}`
      + (u.behind ? ` — ${u.behind} change(s) since yours` : '');
  } else {
    btn.classList.add('d-none');
  }
}

async function pollUpdate() {
  try {
    const d = await api('/api/update/status');
    // `error` here means the REMOTE check failed (no host agent, no Bitbucket
    // credentials, network down) — the INSTALLED version is local knowledge and
    // is still in the payload, so it must still be displayed. Treating any
    // error as a dead response left the navbar with no version at all, which
    // looks like the app doesn't report one.
    if (d && d.current) { _update = d; renderUpdateBadge(); }
  } catch { /* transient — next tick retries */ }
}

function startUpdateChecks() {
  pollUpdate();
  setInterval(pollUpdate, UPDATE_POLL_MS);
}

function _updateDialogHtml(u) {
  const changes = (u.changes || []).slice(0, 10);
  const changeHtml = changes.length
    ? `<div class="mt-2"><div class="fw-semibold small mb-1">What's new</div>
         <ul class="small mb-0 ps-3">${changes.map(c =>
           `<li><span class="font-monospace text-secondary">${esc(c.hash)}</span> ${esc(c.message)}</li>`).join('')}</ul></div>`
    : '';
  const rows = [
    ['Installed', _verLabel(u.current)],
    ['Available', _verLabel(u.latest)],
    u.behind ? ['Changes', `${u.behind} commit(s) ahead of this deployment`] : null,
    ['Source', `${esc(u.remote || '')}${u.branch ? '/' + esc(u.branch) : ''} · checked ${fmtTime((u.checked_at || 0) * 1000)}`],
  ].filter(Boolean);
  const table = rows.map(([k, v]) =>
    `<tr><td class="text-secondary pe-3">${esc(k)}</td><td>${esc(v)}</td></tr>`).join('');
  let note = '';
  if (!u.ok && u.error) {
    note = `<div class="alert alert-warning py-2 px-2 small mt-2 mb-0">Could not check: ${esc(u.error)}</div>`;
  } else if (u.update_available && !u.can_apply) {
    const repo = u.repo || '<the checkout on the server>';
    note = `<div class="alert alert-secondary py-2 px-2 small mt-2 mb-0">
        One-click update isn't available here — ${esc(u.cannot_apply_reason || '')}.<br/>
        On the server run:<br/>
        <code>cd ${esc(repo)} &amp;&amp; git pull &amp;&amp; docker compose up -d</code></div>`;
  } else if (u.update_available) {
    note = `<div class="alert alert-warning py-2 px-2 small mt-2 mb-0">
        Updating pulls the new code and <b>restarts the app for everyone</b>
        (about a minute). Other connected users are told first.</div>`;
  }
  return `<table class="small mb-0"><tbody>${table}</tbody></table>${changeHtml}${note}
    <div id="updateProgress" class="mt-2"></div>`;
}

function showUpdateDialog() {
  const u = _update || { current: { version: '?' }, latest: {} };
  document.querySelector('.rt-modal-overlay.update-modal')?.remove();
  const wrap = document.createElement('div');
  wrap.className = 'rt-modal-overlay update-modal';
  wrap.innerHTML = `<div class="rt-modal" style="max-width:640px;">
      <div class="rt-modal-title"><i class="bi bi-arrow-up-circle-fill me-2"></i>
        ${u.update_available ? 'A newer version is available' : 'CC ES Analyzer is up to date'}</div>
      <div class="rt-modal-body" id="updateBody">${_updateDialogHtml(u)}</div>
      <div class="rt-modal-actions">
        ${u.can_apply ? '<button class="btn btn-sm btn-success" data-act="apply"><i class="bi bi-download me-1"></i>Update now</button>' : ''}
        <button class="btn btn-sm btn-outline-info" data-act="check">Check again</button>
        <button class="btn btn-sm btn-outline-secondary" data-act="close">Close</button>
      </div></div>`;
  document.body.appendChild(wrap);
  const close = () => { wrap.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey);
  wrap.addEventListener('click', async (e) => {
    if (e.target === wrap) return close();
    const b = e.target.closest('button[data-act]');
    if (!b) return;
    const act = b.dataset.act;
    if (act === 'close') return close();
    if (act === 'check') {
      b.disabled = true; b.textContent = 'Checking…';
      try { _update = await api('/api/update/check', { method: 'POST' }); } catch { /* shown below */ }
      renderUpdateBadge();
      close(); showUpdateDialog();
      return;
    }
    if (act === 'apply') {
      wrap.querySelectorAll('button[data-act]').forEach(x => { x.disabled = true; });
      runUpdate(wrap);
    }
  });
}

/** Kick off the update and follow it through the restart. */
async function runUpdate(wrap) {
  const box = wrap.querySelector('#updateProgress');
  const say = (html) => { box.innerHTML = html; };
  say('<div class="small text-info"><span class="spinner-border spinner-border-sm me-2"></span>Requesting update…</div>');

  let res;
  try { res = await api('/api/update/apply', { method: 'POST' }); }
  catch (e) { res = { error: String(e) }; }
  if (!res || res.error) {
    say(`<div class="alert alert-danger py-2 px-2 small mb-0">${esc(res?.error || 'update failed')}</div>`);
    wrap.querySelectorAll('button[data-act="close"]').forEach(x => { x.disabled = false; });
    return;
  }

  const started = Date.now();
  const stepIcon = { ok: '✔', error: '✖', running: '…' };
  let restarting = false;
  while (Date.now() - started < 12 * 60 * 1000) {
    await new Promise(r => setTimeout(r, 2500));
    let job = null;
    // Anything short of a clean job payload means the app is still coming back:
    // fetch rejects while the port is closed, and a proxy in front answers 502
    // with an HTML page, which api() now reports as {error} rather than throwing.
    try { job = await api('/api/update/job'); restarting = !!(job && job.error); }
    catch { restarting = true; }          // expected while the container restarts

    const steps = (job?.steps || []).map(s =>
      `<div class="small"><span class="font-monospace">${stepIcon[s.status] || '·'}</span>
         ${esc(s.name)}${s.output ? ` <span class="text-secondary">— ${esc(String(s.output).slice(0, 120))}</span>` : ''}</div>`
    ).join('');
    const head = restarting
      ? '<div class="small text-warning"><span class="spinner-border spinner-border-sm me-2"></span>Restarting the app…</div>'
      : `<div class="small text-info"><span class="spinner-border spinner-border-sm me-2"></span>${esc(job?.state || 'working')}…</div>`;
    say(head + steps);

    if (job && job.state === 'done') {
      say(steps + `<div class="alert alert-success py-2 px-2 small mt-2 mb-0">
          Updated. Reload the page to load the new version.
          <button class="btn btn-sm btn-success ms-2 py-0" onclick="location.reload()">Reload now</button></div>`);
      return;
    }
    if (job && job.state === 'error') {
      say(steps + `<div class="alert alert-danger py-2 px-2 small mt-2 mb-0">${esc(job.error || 'update failed')}</div>`);
      wrap.querySelectorAll('button[data-act="close"]').forEach(x => { x.disabled = false; });
      return;
    }
    if (job && job.restart_required) {
      say(steps + `<div class="alert alert-warning py-2 px-2 small mt-2 mb-0">
          Code updated. Restart the service to load it.</div>`);
      return;
    }
  }
  say('<div class="alert alert-warning py-2 px-2 small mb-0">Still running — check the server logs '
    + '(<code>journalctl -u cc-es-analyzer-updater</code>).</div>');
}

/* ══════════════════════════════════════════════════════════════════════════
   SYSTEM HEALTH — the landing page
   ══════════════════════════════════════════════════════════════════════════
   Three tiles, one verdict each, and a banner that is the most severe of them.
   The whole screen exists to answer the first question a support engineer has
   on a CyberController — "is this box actually working" — before they open a
   datastore.

   The rule the server follows and this screen must not undo: a check that
   COULD NOT RUN is `unknown`, not `ok`. Grey, never green. A dashboard that
   reports health because it failed to look is worse than no dashboard, because
   somebody acts on it.

   Four of the controls here are deliberately dead — delete a file, delete a RED
   index, repair a table, recreate the database. They are drawn rather than
   hidden so the shape of the tool is honest about where it is going, and each
   one says WHY it is off when clicked. Their routes do not exist on the server
   and the host agent has no operation that would carry them out, so there is
   nothing behind them to reach. */

let _sysSummary = null;      // last /api/system/summary payload — the ONE source
let _sysDetail  = '';        // which drilldown is open: containers|storage|databases
let _sysLargestPoll = null;  // interval handle for the largest-files scan
let _sysInFlight = false;    // a check is running; do not start a second

/* Work the user did INSIDE a drilldown, kept across re-renders.
 *
 * The tiles refresh from the server every tick, but a filesystem scan and a
 * container log are not part of that payload — they were asked for separately,
 * they are expensive (a scan of a full disk is minutes), and re-running them on
 * a timer would be absurd. Without somewhere to put them they were simply lost:
 * every auto-refresh redrew the drilldown and the results the engineer was
 * reading vanished, so the screen actively punished you for leaving it on.
 *
 * Held with the time they were taken, and re-rendered with that time shown, so
 * a panel that is deliberately NOT live never pretends otherwise. */
let _sysScan = null;         // {mount, files, at} — last largest-files scan
let _sysOpenLog = null;      // {name, log, lines, at} — last container log read

/* The capability behind each dead control, and the sentence shown when someone
   clicks it. Kept here rather than fetched because /api/policy carries only
   on/off — and a user who clicks a grey button deserves a reason, not a
   shrug. Mirrors the notes in modules/system/__init__.py. */
const SYS_LOCKED = {
  'system.storage.delete': 'Deleting files is switched off on this instance. '
    + 'It needs TWO keys, both set on the CC itself: '
    + 'capability.system.storage.delete=true in '
    + '/opt/radware/mgt-server/properties/cc_admin.properties (then recreate '
    + 'the container), and the host agent started with --allow-delete. '
    + 'Only logs, heap dumps and zips can ever be removed this way.',
  'system.es.delete_index': 'Deleting an index is not built yet. It is usually '
    + 'the fastest way back to a green cluster, and always a data loss.',
  'system.maria.repair': 'Repairing a table is not built yet. It runs '
    + 'mariadb-check --repair in place — non-disruptive, but it writes to a '
    + 'production database.',
  'system.maria.recreate': 'Recreating the schemas is not built yet. It wraps '
    + "the CC's own repair_mysql_db.sh: it stops vision and loses everything "
    + 'written since the last nightly dump.',
};

const SYS_ICON = { ok: 'bi-check-circle-fill', warn: 'bi-exclamation-triangle-fill',
                   crit: 'bi-x-octagon-fill', unknown: 'bi-question-circle-fill' };
const SYS_WORD = { ok: 'This CC is healthy', warn: 'This CC needs attention',
                   crit: 'This CC has a problem', unknown: 'Cannot tell yet' };

function _sysClass(sev) { return 'sys-' + (SYS_ICON[sev] ? sev : 'unknown'); }

/** A control that is switched off, drawn as a real button so the workflow is
 *  visible, with its reason one click away. */
function sysLocked(capId, label, icon) {
  return `<button class="btn btn-sm btn-outline-secondary py-0 px-2 sys-locked"
            onclick="event.stopPropagation();sysLockedNote('${capId}')"
            title="Not available — click to see why">
            <i class="bi ${icon} me-1"></i>${esc(label)}</button>`;
}

function sysLockedNote(capId) {
  showToast(SYS_LOCKED[capId] || 'This action is not available on this instance.',
            'bg-secondary');
}

function fmtBytes(n) {
  const bytes = Number(n) || 0;
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let i = 0, v = bytes;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

/* ── Loading and the tiles ─────────────────────────────────────────────── */

async function loadSystemHealth(manual = false) {
  const banner = document.getElementById('sysBanner');
  if (!banner) return;

  // One request at a time. An auto-refresh tick that fires while the previous
  // check is still walking the host would queue a second one behind it and the
  // screen would start showing answers out of order.
  if (_sysInFlight) return;
  _sysInFlight = true;
  document.getElementById('sysBusy')?.classList.remove('d-none');

  let data;
  try {
    // NOTHING on screen changes until the response arrives. The old readings
    // stay put, correct as of their timestamp, and are replaced in one step —
    // no blanking, no spinner over the content, no scroll jump. A dashboard
    // that empties itself every 10 seconds is a dashboard nobody leaves open.
    data = await api('/api/system/summary');
  } finally {
    _sysInFlight = false;
    document.getElementById('sysBusy')?.classList.add('d-none');
  }

  if (!data || data.error) {
    // Only a MANUAL re-check repaints on failure. An auto tick that cannot
    // reach the server leaves the last good reading alone — a transient blip
    // should not wipe the screen an engineer is reading.
    if (manual || !_sysSummary) {
      _sysPaint('unknown', 'Cannot tell yet',
                (data && data.error) || 'the health check did not answer');
    }
    return;
  }
  _sysSummary = data;

  const panes = data.panes || {};
  for (const key of ['containers', 'storage', 'databases']) {
    const pane = panes[key] || { severity: 'unknown', headline: '—' };
    const tile = document.getElementById('sysTile-' + key);
    const dot  = document.getElementById('sysDot-' + key);
    if (tile) tile.className = 'sys-tile ' + _sysClass(pane.severity)
                             + (_sysDetail === key ? ' active' : '');
    if (dot) dot.className = 'sys-dot ' + _sysClass(pane.severity);
    setText('sysHead-' + key, pane.headline || '—');
  }

  const worst = data.state || 'unknown';
  _sysPaint(worst, SYS_WORD[worst] || SYS_WORD.unknown, _sysSubtitle(data));

  // The host-access warning. Three grey tiles with no explanation read as a
  // broken screen; naming the missing agent turns it into a task.
  const host = data.hostexec || {};
  const warn = document.getElementById('sysHostexecWarn');
  if (warn) {
    const missing = !host.ok;
    warn.classList.toggle('d-none', !missing);
    if (missing) {
      setText('sysHostexecMsg',
        `The container checks, the disk check and the MariaDB check all need `
        + `access to the CC's host, and this instance has none — ${host.detail || 'no backend'}. `
        + (host.hint || ''));
    }
  }

  // The server's own clock for the measurement, not the browser's for the
  // render — they differ by the round trip, and on a slow host that is seconds.
  const at = data.checked_at ? new Date(data.checked_at * 1000) : new Date();
  setText('sysCheckedAt', `checked ${at.toLocaleTimeString()} · ${data.took_ms || 0} ms`);

  // The open drilldown is re-rendered from THIS payload, so the table and the
  // tile above it always describe the same instant.
  if (_sysDetail) openSystemDetail(_sysDetail, true);
}

function _sysSubtitle(data) {
  const panes = data.panes || {};
  const bad = ['containers', 'storage', 'databases']
    .filter(k => (panes[k] || {}).severity !== 'ok')
    .map(k => (panes[k] || {}).headline)
    .filter(Boolean);
  if (!bad.length) return 'Containers, storage and both databases all check out.';
  // Deduplicated: when the host is unreachable all three panes carry the same
  // sentence, and a banner that says it three times reads as three problems.
  return [...new Set(bad)].join(' · ');
}

function _sysPaint(sev, title, sub) {
  const cls = _sysClass(sev);
  const banner = document.getElementById('sysBanner');
  if (banner) banner.className = 'sys-banner ' + cls;
  const icon = document.getElementById('sysBannerIcon');
  if (icon) icon.className = 'bi ' + (SYS_ICON[sev] || SYS_ICON.unknown) + ' sys-banner-icon';
  setText('sysBannerTitle', title);
  setText('sysBannerSub', sub);
  const navDot = document.getElementById('sysNavDot');
  if (navDot) {
    // ms-1 is markup, not state — reassigning className wholesale dropped it
    // and the dot sat flush against the label.
    navDot.className = 'sys-dot ms-1 ' + cls;
    navDot.title = title;
  }
}

/* ── Drilldowns ────────────────────────────────────────────────────────── */

function closeSystemDetail() {
  _sysDetail = '';
  if (_sysLargestPoll) { clearInterval(_sysLargestPoll); _sysLargestPoll = null; }
  document.getElementById('sysDetailCard')?.classList.add('d-none');
  document.querySelectorAll('.sys-tile').forEach(t => t.classList.remove('active'));
}

/** Open (or re-render) a drilldown.
 *
 *  Renders from the summary payload the tiles were drawn from — it carries the
 *  full rows, not just headlines. Deliberately NOT a second fetch: the tile and
 *  the table underneath it must describe the same instant, and asking the host
 *  twice is what let them disagree. */
async function openSystemDetail(which, silent = false) {
  const card = document.getElementById('sysDetailCard');
  if (!card) return;
  _sysDetail = which;
  card.classList.remove('d-none');
  document.querySelectorAll('.sys-tile').forEach(t => t.classList.remove('active'));
  document.getElementById('sysTile-' + which)?.classList.add('active');
  document.getElementById('sysDetailTools').innerHTML = '';

  const titles = {
    containers: '<i class="bi bi-boxes me-2 text-info"></i>Containers',
    storage:    '<i class="bi bi-hdd me-2 text-info"></i>Storage',
    databases:  '<i class="bi bi-database-check me-2 text-info"></i>Databases',
  };
  document.getElementById('sysDetailTitle').innerHTML = titles[which] || which;

  if (!_sysSummary) {
    if (!silent) {
      document.getElementById('sysDetailBody').innerHTML =
        '<div class="p-4 text-center text-secondary small">Loading…</div>';
    }
    await loadSystemHealth();
    if (_sysDetail !== which || !_sysSummary) return;
  }

  const data = (_sysSummary.panes || {})[which] || {};
  // A re-render under the user's hands must not throw away where they were —
  // an auto-refresh that scrolls a 36-row table back to the top every 10
  // seconds is worse than no auto-refresh.
  const scroller = document.querySelector('#sysDetailBody .sys-detail-scroll');
  const scrollTop = scroller ? scroller.scrollTop : 0;

  if (which === 'containers') renderSysContainers(data);
  else if (which === 'storage') renderSysStorage(data);
  else renderSysDatabases(data);

  if (scrollTop) {
    const again = document.querySelector('#sysDetailBody .sys-detail-scroll');
    if (again) again.scrollTop = scrollTop;
  }
}

function _sysError(message) {
  return `<div class="p-4 text-center text-secondary small">
            <i class="bi bi-exclamation-triangle me-2"></i>${esc(message)}</div>`;
}

function _sysBadge(sev, label) {
  return `<span class="sys-badge ${_sysClass(sev)}">${esc(label || sev)}</span>`;
}

/* ── Containers ────────────────────────────────────────────────────────── */

let _sysShowAllContainers = false;

/** The badge word. Taken from the STATUS, not the severity: "unhealthy" and
 *  "exited" are both red, and calling a running-but-failing service "down"
 *  sends an engineer looking for a container that is right there. */
function _sysStateWord(row) {
  const s = (row.status || '').toLowerCase();
  if (row.missing) return 'missing';
  if (/\(unhealthy\)/.test(s)) return 'unhealthy';
  if (s.startsWith('exited') || s.startsWith('dead')) return 'down';
  if (s.startsWith('restarting')) return 'restarting';
  if (/health:\s*starting/.test(s)) return 'starting';
  if (s.startsWith('created')) return 'created';
  if (s.startsWith('paused')) return 'paused';
  return row.severity === 'ok' ? 'running' : '?';
}

function renderSysContainers(data) {
  const body = document.getElementById('sysDetailBody');
  if (data.error) { body.innerHTML = _sysError(data.error); return; }

  const rows = data.rows || [];
  const bad = rows.filter(r => r.severity !== 'ok');
  // Default to the ones that need attention: on a CC that is 36 rows of "Up 8
  // days" and one that matters, and scrolling for it is the whole problem.
  const shown = (_sysShowAllContainers || !bad.length) ? rows : bad;

  document.getElementById('sysDetailTools').innerHTML = `
    <div class="form-check form-switch m-0">
      <input class="form-check-input" type="checkbox" id="sysAllContainers"
             ${_sysShowAllContainers || !bad.length ? 'checked' : ''}
             ${bad.length ? '' : 'disabled'}
             onchange="_sysShowAllContainers=this.checked;renderSysContainers((_sysSummary.panes||{}).containers||{})">
      <label class="form-check-label small text-secondary" for="sysAllContainers">
        Show all ${rows.length}</label>
    </div>`;

  body.innerHTML = `
    ${_sysExpectedNote(data)}
    <div class="sys-detail-scroll">
      <table class="table table-sm table-hover sys-detail-table mb-0">
        <thead class="table-light" style="position:sticky;top:0;z-index:2;">
          <tr><th style="width:110px;">State</th><th>Service</th><th>Container</th>
              <th>Status</th><th style="width:190px;" class="text-end">Log</th></tr>
        </thead>
        <tbody>
          ${shown.map(r => `
            <tr>
              <td>${_sysBadge(r.severity, _sysStateWord(r))}</td>
              <td class="fw-semibold">${esc(r.service || r.name)}</td>
              <td class="text-secondary sys-path">${esc(r.name || '—')}</td>
              <td class="text-secondary">${esc(r.status)}</td>
              <td class="text-end text-nowrap">
                ${r.name ? `
                <button class="btn btn-sm btn-outline-primary py-0 px-2"
                        onclick="openContainerLog('${esc(r.name)}')">
                  <i class="bi bi-journal-text me-1"></i>View</button>
                <button class="btn btn-sm btn-outline-secondary py-0 px-2"
                        onclick="downloadContainerLog('${esc(r.name)}')"
                        title="Download the last 5000 lines — attach it to a ticket">
                  <i class="bi bi-download"></i></button>`
                : `<span class="text-secondary" style="font-size:0.72rem;">no container to read</span>`}
              </td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>
    ${_sysMariaRecreateNote(rows)}
    <div id="sysLogPane"></div>`;

  // The log the engineer was reading is not part of the health payload, so a
  // re-render would otherwise close it under their hands every 10 seconds.
  _paintContainerLog();
}

/** The count line, and the honest caveat when we could not learn what SHOULD
 *  be running. "34 running" means nothing without "of 36". */
function _sysExpectedNote(data) {
  const expected = (data.expected || []).length;
  const rows = (data.rows || []).length;
  if (data.expected_error) {
    return `<div class="px-3 py-2 border-bottom text-secondary" style="font-size:0.74rem;">
        <i class="bi bi-info-circle me-2"></i>Showing the ${rows} containers that
        exist. This CC could not be asked which services its COMPOSE_PROFILES
        require — ${esc(data.expected_error)} — so a service with no container
        at all would not be listed here.</div>`;
  }
  if (!expected) return '';
  const running = (data.rows || []).filter(r => r.severity === 'ok').length;
  return `<div class="px-3 py-2 border-bottom text-secondary" style="font-size:0.74rem;">
      <i class="bi bi-list-check me-2"></i>${running} of ${expected} services
      running${running < expected ? `, ${expected - running} not` : ''}. The expected list comes from COMPOSE_PROFILES in the CC's
      <code>.env</code>, so services belonging to profiles this appliance does
      not run are not counted.</div>`;
}

/** The one place the heaviest action belongs: a MariaDB container that will not
 *  start is exactly the case repair_mysql_db.sh exists for. Offered here, and
 *  only when it applies, rather than as a permanent button nobody should press. */
function _sysMariaRecreateNote(rows) {
  const maria = rows.find(r => /mariadb/i.test(r.service || r.name)
                            && r.severity === 'crit');
  if (!maria) return '';
  return `<div class="alert alert-danger m-3 py-2 px-3" style="font-size:0.8rem;">
      <div class="fw-semibold mb-1"><i class="bi bi-database-exclamation me-2"></i>
        ${esc(maria.name || maria.service)} is not running</div>
      <div class="mb-2">When the MariaDB container will not start, the CC's own
        procedure recreates the schemas and restores the last nightly dump.</div>
      ${sysLocked('system.maria.recreate', 'Recreate DB & restore last backup', 'bi-arrow-counterclockwise')}
    </div>`;
}

async function openContainerLog(name) {
  const pane = document.getElementById('sysLogPane');
  if (!pane) return;
  pane.innerHTML = `<div class="p-3 text-secondary small">Reading ${esc(name)}…</div>`;
  const data = await api(`/api/system/containers/${encodeURIComponent(name)}/logs?lines=500`);
  if (data.error) { pane.innerHTML = `<div class="p-3">${_sysError(data.error)}</div>`; return; }
  _sysOpenLog = { name, log: data.log || '', lines: data.lines, at: Date.now() };
  _paintContainerLog();
  pane.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

/** Draw whatever log is currently open. Called both when it is first read and
 *  after every re-render, so an auto-refresh no longer closes it. */
function _paintContainerLog() {
  const pane = document.getElementById('sysLogPane');
  if (!pane) return;
  if (!_sysOpenLog) { pane.innerHTML = ''; return; }
  const { name, log, lines, at } = _sysOpenLog;
  pane.innerHTML = `
    <div class="d-flex align-items-center gap-2 px-3 py-2 border-top border-bottom bg-light">
      <span class="fw-semibold small"><i class="bi bi-journal-text me-2"></i>${esc(name)}</span>
      <span class="text-secondary" style="font-size:0.72rem;">last ${lines} lines,
        read ${new Date(at).toLocaleTimeString()}</span>
      <button class="btn btn-sm btn-outline-primary py-0 px-2 ms-auto"
              onclick="openContainerLog('${esc(name)}')" title="Read it again">
        <i class="bi bi-arrow-clockwise"></i></button>
      <button class="btn btn-sm btn-outline-secondary py-0 px-2"
              onclick="downloadContainerLog('${esc(name)}')">
        <i class="bi bi-download me-1"></i>Download 5000 lines</button>
      <button class="btn btn-sm btn-outline-secondary py-0 px-2"
              onclick="closeContainerLog()">
        <i class="bi bi-x-lg"></i></button>
    </div>
    <pre class="sys-log">${esc(log || '(the container has logged nothing)')}</pre>`;
}

function closeContainerLog() {
  _sysOpenLog = null;
  _paintContainerLog();
}

function downloadContainerLog(name) {
  // A plain navigation, not fetch+blob: the server sets Content-Disposition and
  // the browser handles a multi-megabyte log better than we would.
  window.location = appUrl(
    `/api/system/containers/${encodeURIComponent(name)}/logs/download?lines=5000`);
}

/* ── Storage ───────────────────────────────────────────────────────────── */

function renderSysStorage(data) {
  const body = document.getElementById('sysDetailBody');
  if (data.error) { body.innerHTML = _sysError(data.error); return; }

  const rows = data.rows || [];
  body.innerHTML = `
    <div class="sys-detail-scroll">
      <table class="table table-sm table-hover sys-detail-table mb-0">
        <thead class="table-light" style="position:sticky;top:0;z-index:2;">
          <tr><th style="width:96px;">State</th><th>Mounted on</th><th>Device</th>
              <th style="width:150px;">Used</th><th style="width:110px;" class="text-end">Free</th>
              <th style="width:180px;" class="text-end">Biggest files</th></tr>
        </thead>
        <tbody>
          ${rows.map(r => `
            <tr>
              <td>${_sysBadge(r.severity, r.pct + '%')}</td>
              <td class="fw-semibold">${esc(r.mount)}</td>
              <td class="text-secondary sys-path">${esc(r.device)}</td>
              <td>
                <div class="sys-bar ${_sysClass(r.severity)}"><span style="width:${r.pct}%"></span></div>
                <span class="text-secondary" style="font-size:0.7rem;">
                  ${fmtBytes(r.used_kb * 1024)} of ${fmtBytes(r.size_kb * 1024)}</span>
              </td>
              <td class="text-end text-secondary">${fmtBytes(r.avail_kb * 1024)}</td>
              <td class="text-end">
                <button class="btn btn-sm btn-outline-primary py-0 px-2"
                        onclick="scanLargestFiles('${esc(r.mount)}')"
                        title="Walk this filesystem and list its 20 largest files">
                  <i class="bi bi-search me-1"></i>Scan</button>
              </td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>
    <div class="px-3 py-2 border-top text-secondary" style="font-size:0.72rem;">
      Amber at ${data.warn_pct}%, red at ${data.crit_pct}%. Container overlay
      filesystems are hidden — a CC reports about fifty of them, all describing
      the same disk.
    </div>
    <div id="sysLargestPane"></div>`;

  // A filesystem scan takes minutes and is not part of the health payload, so
  // without this every auto-refresh threw away the result the engineer asked
  // for — the screen punished you for leaving it on. A scan still RUNNING gets
  // its spinner back for the same reason.
  if (_sysScanning) _paintScanning(_sysScanning);
  else if (_sysScan) renderLargestFiles(_largestPane(), _sysScan.mount, _sysScan.files);
}

let _sysScanning = '';       // the mount a scan is currently walking, if any

/** Where the scan panel's contents go. Looked up by id EVERY time rather than
 *  captured once: an auto-refresh re-renders the storage drilldown, which
 *  replaces this element, and a captured reference would keep writing into a
 *  detached node — the results would land nowhere and the scan would look like
 *  it hung. */
function _largestPane() {
  return document.getElementById('sysLargestPane');
}

function _paintScanning(mount) {
  const pane = _largestPane();
  if (pane) pane.innerHTML = `<div class="p-3 small text-secondary">
      <span class="spinner-border spinner-border-sm me-2"></span>
      Walking ${esc(mount)} — this reads every inode on the filesystem and can
      take a few minutes on a full one.</div>`;
}

async function scanLargestFiles(mount) {
  if (!_largestPane()) return;
  if (_sysLargestPoll) { clearInterval(_sysLargestPoll); _sysLargestPoll = null; }
  _sysScan = null;
  _sysScanning = mount;
  _paintScanning(mount);
  _largestPane().scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  const start = await api(`/api/system/storage/largest?mount=${encodeURIComponent(mount)}&n=20`);
  if (start.error) {
    _sysScanning = '';
    const pane = _largestPane();
    if (pane) pane.innerHTML = `<div class="p-3">${_sysError(start.error)}</div>`;
    return;
  }

  // Polled rather than held open: the walk is 11 seconds on an idle lab CC and
  // minutes on the full one somebody is actually asking about, which is longer
  // than a browser will hold a fetch without looking hung.
  _sysLargestPoll = setInterval(async () => {
    const job = await api(`/api/system/storage/largest/${start.job}`);
    if (!job || job.status === 'running') return;
    clearInterval(_sysLargestPoll); _sysLargestPoll = null;
    _sysScanning = '';
    const pane = _largestPane();
    if (!pane) return;                  // the user navigated away mid-scan
    if (job.error || job.status === 'error') {
      pane.innerHTML = `<div class="p-3">${_sysError(job.error || 'the scan failed')}</div>`;
      return;
    }
    _sysScan = { mount, files: job.files || [], at: Date.now() };
    renderLargestFiles(pane, mount, _sysScan.files);
  }, 1500);
}

/** The largest-files table.
 *
 *  Every row carries a verdict from the server (modules/system/safety.py): a
 *  log, heap dump or zip is deletable, and everything else says why not. That
 *  matters more than it sounds, because the list is sorted by SIZE and the
 *  biggest files on a CC are usually a Lucene segment or MariaDB's Aria log —
 *  the two things that must never be removed sit right at the top, next to the
 *  2 GB stale log that genuinely should be. */
function renderLargestFiles(pane, mount, files) {
  if (!files.length) {
    pane.innerHTML = `<div class="p-3 text-secondary small">No files found on ${esc(mount)}.</div>`;
    return;
  }
  _sysLargestFiles = files;
  const armed = can('system.storage.delete');
  const live = files.filter(f => !f.deleted);
  const deletable = live.filter(f => f.deletable).length;
  // The scan's own timestamp, not the page's. This panel is a snapshot of a
  // walk that took minutes; showing it under a header that says "checked 3
  // seconds ago" would be a lie of composition.
  const at = _sysScan && _sysScan.mount === mount ? _sysScan.at : Date.now();

  pane.innerHTML = `
    <div class="d-flex align-items-center gap-2 px-3 py-2 border-top border-bottom bg-light">
      <span class="fw-semibold small"><i class="bi bi-sort-down me-2"></i>
        ${files.length} largest files on ${esc(mount)}</span>
      <span class="text-secondary" style="font-size:0.72rem;">
        scanned ${new Date(at).toLocaleTimeString()}</span>
      <button class="btn btn-sm btn-outline-primary py-0 px-2"
              onclick="scanLargestFiles('${esc(mount)}')" title="Walk it again">
        <i class="bi bi-arrow-clockwise"></i></button>
      <span class="text-secondary ms-auto" style="font-size:0.72rem;">
        ${armed
          ? `${deletable} of ${live.length} can be removed from here — logs, heap dumps and zips only`
          : 'Deleting is not enabled on this instance — see why on any row'}</span>
      <button class="btn btn-sm btn-outline-secondary py-0 px-2"
              onclick="_sysScan=null;document.getElementById('sysLargestPane').innerHTML='';">
        <i class="bi bi-x-lg"></i></button>
    </div>
    <div class="sys-detail-scroll">
      <table class="table table-sm table-hover sys-detail-table mb-0">
        <tbody>
          ${files.map((f, i) => `
            <tr id="sysFileRow-${i}"${f.deleted ? ' style="opacity:.45;"' : ''}>
              <td style="width:90px;" class="text-end fw-semibold">${fmtBytes(f.bytes)}</td>
              <td class="sys-path"${f.deleted ? ' style="text-decoration:line-through;"' : ''}>${esc(f.path)}
                ${(f.deletable || f.deleted) ? '' : `<div class="text-secondary" style="font-size:0.7rem;">
                    <i class="bi bi-shield-lock me-1"></i>${esc(f.reason)}</div>`}
              </td>
              <td style="width:120px;" class="text-end">
                ${_sysDeleteButton(f, i, armed)}</td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
}

let _sysLargestFiles = [];

function _sysDeleteButton(file, index, armed) {
  // Already gone. The row is kept, struck through, rather than removed: the
  // scan is a snapshot, and silently dropping rows out of it would make the
  // list disagree with the count beside it and with what the engineer
  // remembers doing.
  if (file.deleted) {
    return `<span class="text-success" style="font-size:0.72rem;">
              <i class="bi bi-check-lg me-1"></i>deleted</span>`;
  }
  // Refused by the rules — the reason is already on the row, so the control
  // is simply absent rather than a dead button that invites a click.
  if (!file.deletable) {
    return `<span class="text-secondary" style="font-size:0.72rem;">
              <i class="bi bi-dash-circle"></i></span>`;
  }
  // Downloading is its own capability and does not depend on deletion: taking
  // a copy of a log is useful whether or not this instance may remove it.
  const download = can('system.storage.download')
    ? `<button class="btn btn-sm btn-outline-secondary py-0 px-2"
         onclick="downloadHostFile(${index})" title="Download this file">
         <i class="bi bi-download"></i></button>` : '';

  // Deletable in principle, but this instance is not allowed to. Locked, with
  // the reason a click away — the same treatment every other gated action gets.
  if (!armed) {
    return `${download} ${sysLocked('system.storage.delete', 'Delete', 'bi-trash')}`;
  }
  return `${download}
    <button class="btn btn-sm btn-outline-danger py-0 px-2"
            onclick="confirmDeleteFile(${index})">
      <i class="bi bi-trash me-1"></i>Delete</button>`;
}

/** Download a file from the CC, streamed straight to disk.
 *
 *  A plain navigation rather than fetch+blob: the server sets
 *  Content-Disposition and the browser writes to disk as bytes arrive, which
 *  matters when the file is a 300 MB log. The blob route would hold the whole
 *  thing in memory first. */
function downloadHostFile(index) {
  const file = _sysLargestFiles[index];
  if (!file) return;
  window.location = appUrl(
    `/api/system/storage/download?path=${encodeURIComponent(file.path)}`);
}

/** Confirm, naming the file and its size in full.
 *
 *  The path is spelled out rather than summarised as "this file": these are
 *  long, similar-looking paths deep inside overlay directories, and the whole
 *  risk of this feature is deleting the row next to the one you meant. */
async function confirmDeleteFile(index) {
  const file = _sysLargestFiles[index];
  if (!file) return;
  const canGrab = can('system.storage.download');

  // "Download and delete" first, and it is the primary button. Deleting a log
  // you have not kept is a one-way door, and the moment somebody notices they
  // needed it is always after it is gone — so the safe path is the default one
  // and the bare Delete sits beside it for when the file is genuinely junk.
  const choice = await uiChoice(document, {
    title: 'Delete this file?',
    icon: '🗑',
    message: `${file.path}\n\n${fmtBytes(file.bytes)}\n\n`
           + `This removes it from the CC immediately and cannot be undone.`
           + (canGrab
              ? `\n\n"Download and delete" saves a copy to this computer first `
                + `and only deletes once the whole file has arrived.`
              : ''),
    buttons: [
      ...(canGrab ? [{ value: 'both', text: 'Download and delete', cls: 'btn-primary' }] : []),
      { value: 'delete', text: 'Delete', cls: 'btn-danger' },
      { value: null, text: 'Cancel', cls: 'btn-outline-secondary' },
    ],
  });

  if (choice === 'delete') deleteFile(index);
  else if (choice === 'both') downloadThenDelete(index);
}

/** Save the file to this computer, and delete it ONLY once every byte has
 *  arrived.
 *
 *  fetch-into-a-blob rather than a plain navigation, which is the opposite of
 *  the standalone Download button and deliberately so: a navigation gives the
 *  page no completion signal, so the delete would fire while the transfer was
 *  still in flight — and if it then failed, the file would be gone and the copy
 *  incomplete. That is the one outcome this button exists to prevent. The cost
 *  is that the file passes through memory, so the size is stated up front. */
async function downloadThenDelete(index) {
  const file = _sysLargestFiles[index];
  if (!file) return;
  const row = document.getElementById(`sysFileRow-${index}`);
  const btn = row?.querySelector('.btn-outline-danger');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>'; }
  showToast(`Downloading ${fmtBytes(file.bytes)} — the file is deleted only `
            + `once it has all arrived.`, 'bg-info');

  try {
    const res = await fetch(appUrl(
      `/api/system/storage/download?path=${encodeURIComponent(file.path)}`));
    if (!res.ok) throw new Error(`HTTP ${res.status} ${await res.text()}`);
    const blob = await res.blob();

    // The stream can carry its own failure in-band: mid-transfer there is no
    // status code left to change, so the server appends a marker instead. A
    // truncated copy must not authorise a delete.
    if (blob.size < file.bytes) {
      throw new Error(`only ${fmtBytes(blob.size)} of ${fmtBytes(file.bytes)} `
                    + `arrived — the file has NOT been deleted`);
    }

    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = file.path.split('/').pop();
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (err) {
    showToast(`Download failed: ${err.message}`, 'bg-danger');
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-trash me-1"></i>Delete'; }
    return;
  }

  await deleteFile(index);
}

async function deleteFile(index) {
  const file = _sysLargestFiles[index];
  if (!file) return;
  const row = document.getElementById(`sysFileRow-${index}`);
  const btn = row?.querySelector('button');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>'; }

  const res = await api('/api/system/storage/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: file.path }),
  });

  if (res.error) {
    showToast(res.error, 'bg-danger');
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-trash me-1"></i>Delete'; }
    return;
  }

  // Marked on the CACHED row, not just in the DOM. The next auto-refresh
  // re-renders this table from _sysScan, and a purely visual strike-through
  // would have been undone — the file would reappear with a live Delete button
  // beside it, which is how someone ends up deleting the row below it.
  file.deleted = true;
  if (_sysScan) {
    const cached = _sysScan.files.find(f => f.path === file.path);
    if (cached) cached.deleted = true;
  }
  if (row) {
    row.style.opacity = '0.45';
    row.querySelector('.sys-path').style.textDecoration = 'line-through';
    row.querySelector('td:last-child').innerHTML =
      '<span class="text-success" style="font-size:0.72rem;"><i class="bi bi-check-lg me-1"></i>deleted</span>';
  }
  // The open-file case, stated plainly. Deleting a log a process still holds
  // frees nothing until that process restarts, and an engineer who then looks
  // at `df`, sees no change and concludes the tool lied has been failed by us.
  showToast(res.was_open
    ? `Deleted ${fmtBytes(res.bytes)} — but a running process still has this `
      + `file open, so the space will not appear in df until it restarts.`
    : `Deleted ${fmtBytes(res.bytes)}.`,
    res.was_open ? 'bg-warning' : 'bg-success');

  // Re-read the filesystem figures; the tile above should move.
  loadSystemHealth(true);
}

/* ── Databases ─────────────────────────────────────────────────────────── */

function renderSysDatabases(data) {
  const body = document.getElementById('sysDetailBody');
  const es = data.elasticsearch || {};
  const maria = data.mariadb || {};

  const esRows = [...(es.red || []).map(r => ({ ...r, sev: 'crit' })),
                  ...(es.yellow || []).map(r => ({ ...r, sev: 'warn' }))];

  const esSection = es.error
    ? _sysError(es.error)
    : (!esRows.length
        ? `<div class="p-3 text-secondary small"><i class="bi bi-check-circle me-2 text-success"></i>
             ${esc(es.headline || 'every index is green')}.
           </div>`
        : `<table class="table table-sm table-hover sys-detail-table mb-0">
             <thead class="table-light"><tr>
               <th style="width:96px;">Health</th><th>Index</th>
               <th style="width:100px;" class="text-end">Docs</th>
               <th style="width:100px;" class="text-end">Size</th>
               <th style="width:120px;" class="text-end">Action</th></tr></thead>
             <tbody>
               ${esRows.map(r => `
                 <tr>
                   <td>${_sysBadge(r.sev, r.health)}</td>
                   <td class="fw-semibold sys-path">${esc(r.index)}</td>
                   <td class="text-end text-secondary">${esc(r['docs.count'] ?? '—')}</td>
                   <td class="text-end text-secondary">${esc(r['store.size'] ?? '—')}</td>
                   <td class="text-end">
                     ${sysLocked('system.es.delete_index', 'Delete', 'bi-trash')}</td>
                 </tr>`).join('')}
             </tbody></table>`);

  const corrupt = maria.corrupt || [];
  const mariaSection = maria.error
    ? _sysError(maria.error)
    : (!corrupt.length
        ? `<div class="p-3 text-secondary small"><i class="bi bi-check-circle me-2 text-success"></i>
             ${esc(maria.headline || 'all tables check out')}.</div>`
        : `<table class="table table-sm table-hover sys-detail-table mb-0">
             <thead class="table-light"><tr>
               <th style="width:96px;">State</th><th>Table</th><th>What the check said</th>
               <th style="width:120px;" class="text-end">Action</th></tr></thead>
             <tbody>
               ${corrupt.map(t => `
                 <tr>
                   <td>${_sysBadge('crit', 'corrupt')}</td>
                   <td class="fw-semibold sys-path">${esc(t.schema)}.${esc(t.table)}</td>
                   <td class="text-secondary" style="font-size:0.74rem;">${esc(t.status)}</td>
                   <td class="text-end">
                     ${sysLocked('system.maria.repair', 'Repair', 'bi-wrench')}</td>
                 </tr>`).join('')}
             </tbody></table>
           <div class="px-3 py-2 border-top" style="font-size:0.74rem;">
             If in-place repair is not enough — or the container will not start
             at all — the CC's own procedure recreates the schemas and restores
             the last nightly dump:
             ${sysLocked('system.maria.recreate', 'Recreate DB & restore last backup', 'bi-arrow-counterclockwise')}
           </div>`);

  body.innerHTML = `
    <div class="px-3 py-2 bg-light border-bottom fw-semibold small">
      <i class="bi bi-search me-2"></i>Elasticsearch — index health</div>
    ${esSection}
    <div class="px-3 py-2 bg-light border-top border-bottom fw-semibold small">
      <i class="bi bi-database me-2"></i>MariaDB — table integrity
      ${maria.checked ? `<span class="text-secondary fw-normal ms-2"
        style="font-size:0.72rem;">${maria.checked} tables checked</span>` : ''}</div>
    ${mariaSection}`;
}

/* ══════════════════════════════════════════════════════════════════════════
   INIT — auto-connect from localStorage on page load
   ══════════════════════════════════════════════════════════════════════════ */
/** Boot the application proper. Split out of init() so that a deployment
 *  requiring a login can hold everything here until one succeeds — the app
 *  must not fire a screenful of requests that will all answer 401, both
 *  because it looks broken and because it buries the real reason. */
async function startApp() {
  if (_appStarted) return;
  _appStarted = true;
  // First — the rest of the UI is built from what this deployment may do.
  await loadPolicy();
  applyPolicyToChrome();
  initDbTree();
  initMariaPanes();
  initPgPanes();
  initUiPrefs();
  initQuerySplitter();
  initAutoRefresh();
  renderProfiles();
  startPresence();
  if (can('app.self_update')) startUpdateChecks();
  // Its own store with its own reachability — probed independently of ES, and
  // not awaited, so a slow or dead MariaDB cannot hold up the whole app.
  loadMariaHealth();
  loadPgHealth();

  // Wherever we land, land on System Health. It answers the question an
  // engineer opens this tool with — "is this CC working" — and it is the one
  // screen that still says something useful when Elasticsearch is the thing
  // that is broken.
  //
  // Embedded on a CC the server is already bound to the Elasticsearch running
  // beside it (ES_HOST in the compose file), so there is nothing to restore
  // and nothing to ask. Without this a fresh browser on the appliance would
  // land on a connection form for a choice it does not have — the first thing
  // a support engineer would have to click past.
  if (!can('es.connect')) {
    const health = await api('/api/health');
    if (health && health.connected) {
      isConnected = true;
      // Label it for what it is. Echoing the cluster name as the "machine"
      // renders as "vision-es · vision-es", which tells the engineer nothing.
      onConnected({ label: 'This CC', host: '', port: '', scheme: 'http' }, {
        cluster_name: health.cluster_name,
        es_version:   health.es_version,
      });
      syncDbStatus();
      showView('system');
      refreshAll();
      return;
    }
    // ES beside us is not answering. Still terminal: there is no saved
    // connection to fall back to and no form worth showing, so stop here
    // rather than falling through to the standalone path below and landing
    // on a connection screen this profile has already hidden.
    isConnected = false;
    onDisconnected();
    syncDbStatus();
    showView('system');
    return;
  }

  // Try to restore last-used connection
  const saved = localStorage.getItem(LS_ACTIVE);
  if (saved) {
    try {
      const settings = JSON.parse(saved);
      fillForm(settings);

      // Check if ES is still reachable with existing server-side state
      const health = await api('/api/health');
      if (health.connected) {
        isConnected = true;
        onConnected(settings, {
          cluster_name: health.cluster_name,
          es_version:   health.es_version,
        });
        showView('system');
        refreshAll();
        return;
      }

      // Server lost state (e.g. restart) — reconnect silently
      const res  = await fetch(appUrl('/api/connect'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(settings),
      });
      const data = await res.json();
      if (data.connected) {
        isConnected = true;
        onConnected(settings, data);
        showView('system');
        refreshAll();
        return;
      }
    } catch (_) {}
  }

  // No saved connection — show connection settings view
  isConnected = false;
  onDisconnected();
  showView('connection');
}

/* ── Boot ────────────────────────────────────────────────────────────────
   Ask who we have to be before doing anything. Where a login is required the
   app is held back entirely rather than started and then 401'd: a screen full
   of failed panels hides the one thing the user needs to see, which is a
   password box. */
(async function boot() {
  document.getElementById('loginForm')?.addEventListener('submit', submitLogin);

  // The countdown runs even on the login screen — someone should not type a
  // password into a window that is four minutes from closing.
  startLifetimePolling();

  let st = null;
  try { st = await api('/api/auth/state'); } catch { /* handled below */ }
  window.APP_AUTH_REQUIRED = !!(st && st.required);

  if (st && st.required && !st.authenticated) {
    showLoginScreen();
    return;
  }
  await startApp();
})();

/* ── On-line help ────────────────────────────────────────────────────────────
   Every screen carries a "?" Help button that opens a modal with comprehensive,
   context-specific guidance. Content is authored HTML kept in HELP_CONTENT,
   keyed by the same view names used by showView(). */
const HELP_CONTENT = {
  connectivity: {
    title: 'Connectivity', icon: 'bi-globe2',
    body: `
      <p>Several things a CC does depend on reaching the internet — signature
      updates, the ERT Active Attackers Feed, GeoDB location updates, licence
      activation. When one of them quietly stops working, the first thing worth
      establishing is whether the appliance can reach the outside world at all,
      so that you can either <b>eliminate or confirm</b> that as the cause
      before going any further.</p>

      <p>This screen replaces the manual version of that step — SSHing in and
      running <span class="font-monospace">wget services.radware.com</span>.</p>

      <h6>Why the stages are shown separately</h6>
      <p>Because <b>which stage failed decides who fixes it</b>. "Cannot reach
      it" identifies nobody.</p>
      <ul>
        <li><b>DNS</b> — the name does not resolve. The resolver or the search
          domain configuration, not the firewall.</li>
        <li><b>TCP</b> — it resolves but the connection is refused or times
          out. A firewall or a proxy in the path. A timeout usually means
          packets are being dropped rather than refused.</li>
        <li><b>TLS</b> — it connects but the handshake fails. Very often the
          customer's TLS-inspecting proxy, whose CA this appliance does not
          trust. The fix is to trust that CA, <i>not</i> to open the
          firewall.</li>
        <li><b>HTTP</b> — everything below worked and the request itself
          failed.</li>
      </ul>

      <h6>Results that look like failures but are not</h6>
      <p>An <b>HTTP 401 or 403</b> counts as <i>reachable</i>. The request
      arrived, was understood and was answered — which proves the path works.
      The feed bucket answers 403 to an unauthenticated request by design.
      Treating that as a network fault is how an afternoon gets spent on a
      firewall that was never the problem.</p>

      <h6>What the result does and does not prove</h6>
      <p>The probe runs from the <b>CC Admin container</b>. That is strong
      evidence about the appliance, but it is not necessarily the identical
      network path used by the service that actually fetches the feed. The
      banner says so on every run rather than letting you assume otherwise.</p>

      <p>If this deployment egresses through a <b>proxy</b>, the DNS, TCP and
      TLS stages are skipped and marked unknown — through a proxy they would
      describe the proxy rather than the destination, and a confident answer
      about the wrong machine is worse than none.</p>

      <h6>Provenance</h6>
      <p>Each destination cites the knowledge-base articles that establish it
      as a real dependency. Nothing here was guessed, and you can check the
      reasoning.</p>`,
  },
  system: {
    title: 'System Health', icon: 'bi-heart-pulse',
    body: `
      <p>The landing page, and the answer to the first question anyone has about a
         CyberController: <b>is this box actually working?</b> Four checks, and one
         overall state that is the most severe of them.</p>
      <h6>What is checked</h6>
      <ul>
        <li><b>Containers</b> — every service in the CC's <code>docker-compose</code>.
            <i>Unhealthy</i>, <i>Exited</i> and <i>Dead</i> are red; <i>Restarting</i>,
            <i>health: starting</i>, <i>Created</i> and <i>Paused</i> are amber.
            A service that is <i>Up</i> and declares no healthcheck counts as
            healthy — several CC services have none.</li>
        <li><b>Storage</b> — the host's real filesystems. Amber at 80% used, red at
            90%. The container <code>overlay</code> mounts are hidden: a CC reports
            about fifty of them and every one describes the same disk.</li>
        <li><b>Databases</b> — Elasticsearch index health and MariaDB table
            integrity, in one tile. Any RED index is red, any yellow index is
            amber, and <b>no index is exempt</b> — <code>appconfig2</code>
            included. It is often yellow on a single-node CC, which asks for a
            replica it cannot place; that is a true statement about the cluster
            and it belongs on the screen rather than being quietly excused. Any
            corrupt MariaDB table is red.</li>
      </ul>
      <h6>Keeping it open</h6>
      <p><b>Auto</b> re-runs every check on a 10, 30 or 60 second timer, and the
         setting is remembered per browser. Nothing on the page is cleared while
         a check runs: the request goes out in the background and the readings
         you are looking at are replaced in one step when the answer comes back
         — no blank screen, no scroll jump, and an open drilldown keeps its
         place. If a tick cannot reach the server the last good reading stays,
         rather than the screen wiping itself over a blip.</p>
      <p>The tiles and the table below them come from the <i>same</i> response,
         so they always describe the same instant. The timestamp beside
         <b>Re-check</b> is when the CC was actually measured.</p>
      <h6>Grey is not green</h6>
      <p>A tile shows grey — <i>unknown</i> — when the check could not run at all.
         That is not a pass. Three of the four checks need access to the CC's
         <b>host</b>, which the app cannot see from inside its own container, so
         they go through a small root agent installed on the CC
         (<code>deploy/host_agent.py --install</code>) or, running the tool
         remotely, over the SSH connection you gave for Elasticsearch. When that
         access is missing the banner says so.</p>
      <h6>Drilling in</h6>
      <ul>
        <li><b>Containers</b> — shows the services needing attention first. <b>View</b>
            reads the last 500 log lines in place; <b>Download</b> saves the last
            5000 as a file to attach to a ticket.</li>
        <li><b>Storage</b> — <b>Scan</b> walks one filesystem and lists its 20
            largest files. On a full disk this takes a few minutes; it reads every
            inode on that filesystem.</li>
        <li><b>Databases</b> — the RED/yellow indices, and any corrupt table with
            what the check said about it.</li>
      </ul>
      <h6>Downloading a file</h6>
      <p>Every file the tool will delete, it will also hand you a copy of — the
         two use the same allowlist on purpose. The <b>↓</b> on a row streams it
         straight to disk. In the delete dialog, <b>Download and delete</b>
         saves the copy first and only removes the file once every byte has
         arrived; if the transfer is short or fails, nothing is deleted.</p>
      <p>The file is pulled through the host agent in chunks — about 14 MB/s in
         practice, so a 300 MB log takes around 20 seconds — with a 2 GB ceiling.
         Anything larger has to be copied off with <code>scp</code>.</p>
      <h6>Deleting a file</h6>
      <p>Only <b>logs, heap dumps and zips</b> can be removed from here. Everything
         else says why not on the row itself, and the reasons are specific:
         <i>it is MariaDB's Aria transaction log</i>, <i>it is an OpenSearch index
         file</i>, <i>it is a backup the DB recovery procedure restores from</i>.
         That restriction is not politeness — the list is sorted by size, and on
         a CC the biggest files are usually datastore internals sitting directly
         above the stale log you actually want. Anything else has to be done on
         the machine, deliberately.</p>
      <p>It needs <b>two keys, both on the CC</b>:
         <code>capability.system.storage.delete=true</code> in
         <code>/opt/radware/mgt-server/properties/cc_admin.properties</code>
         (then recreate the container), <i>and</i> the host agent started with
         <code>--allow-delete</code>. Unlocking the capability alone does
         nothing — the host still refuses, which is the point.</p>
      <p>If the file was still open by a running process, the tool says so:
         the space does not return to <code>df</code> until that process
         restarts.</p>
      <h6>Why some buttons do nothing</h6>
      <p>Delete an index, repair a table and recreate the database are drawn but
         not built yet. Click one and it tells you exactly that. They are shown
         rather than hidden so the workflow is visible — and so nobody wonders
         whether the tool quietly did one of them.</p>`,
  },
  connection: {
    title: 'Connection', icon: 'bi-hdd-network',
    body: `
      <p>Connect the analyzer to a CyberController machine's Elasticsearch / OpenSearch cluster so every other screen has data to work with.</p>
      <h6>Key fields</h6>
      <ul>
        <li><b>Host / Port / Scheme</b> — the ES endpoint (default port <code>9200</code>, scheme <code>http</code> or <code>https</code>).</li>
        <li><b>User / Password</b> — optional HTTP basic auth.</li>
        <li><b>Verify certs</b> — TLS certificate verification, off by default for self-signed CC clusters.</li>
        <li><b>SSH fallback</b> — if the ES port can't be reached directly, the app can SSH in to open the port on the box's firewall, or open an SSH tunnel and forward ES traffic through it.</li>
      </ul>
      <h6>How to connect</h6>
      <ol>
        <li>Fill in the host and (if needed) credentials, or pick a <b>Saved Profile</b>.</li>
        <li>Click <b>Connect</b> — the app pings ES and stores the working connection.</li>
        <li>The top bar shows a green pill with the machine, cluster name and ES version once connected.</li>
      </ol>
      <h6>Working alongside other people</h6>
      <ul>
        <li>Your connection is <b>yours alone</b> — each browser gets its own session, so colleagues can work on different CC machines at the same time without affecting each other.</li>
        <li>When someone else is connected to the <b>same</b> CC, an amber <b>“N others on this CC”</b> badge appears in the top bar; hover or click it to see who (name, IP, hostname, browser).</li>
        <li>Click the <b>“you: …”</b> button beside it to set the display name others see instead of your IP.</li>
        <li>Before you change anything on a shared CC — delete, edit, import, restore, generate — you get a warning naming the other users, and <b>they are notified</b> of what you did.</li>
      </ul>
      <h6>Keeping the tool up to date</h6>
      <ul>
        <li>The installed version is shown next to the app name in the top bar. <b>Click it</b> for update details — where the check looks, when it last ran, and what changed.</li>
        <li>When the repository has a newer version, a green <b>Update to x.y.z</b> button appears there. It opens the same dialog with <b>Update now</b>.</li>
        <li>Updating pulls the new code and restarts the app <b>for everyone</b> (about a minute) — the other connected users are notified first. If the server can't update itself, the dialog shows the exact command to run instead.</li>
        <li>An <b>amber</b> version means something is off — hover it. Either the update check failed (usually no update agent on that host, so it falls back to the repository API and gets refused), or the build reports <code>0.0.0</code>, which means its <code>VERSION</code> file is missing and it needs a rebuild.</li>
      </ul>
      <p class="help-tip">The analyzer talks to ES over raw HTTP (not elasticsearch-py), so it works with the older / proxied ES versions common on CC deployments.</p>`,
  },
  dashboard: {
    title: 'Cluster Dashboard', icon: 'bi-speedometer2',
    body: `
      <p>A live overview of the connected cluster's health plus a browsable catalog of all CyberController indices.</p>
      <h6>Cluster cards</h6>
      <ul>
        <li><b>Status</b> — green / yellow / red. When it isn't green, <b>hover the card</b> for the exact reason (non-green indices) in a copyable tooltip.</li>
        <li><b>Nodes</b>, <b>Active Shards</b>, <b>ES Version</b>.</li>
        <li><b>Unassigned Shards</b> — hover for per-shard detail (index, shard, primary/replica, allocation reason), copyable.</li>
      </ul>
      <h6>CC Indices Overview</h6>
      <ul>
        <li><b>Search</b> filters the index list as you type.</li>
        <li>Each index is annotated with its CC <b>category</b> (DP Attacks, EAAF, ADC, …) so you know what it holds.</li>
        <li><b>Checkboxes</b> select indices → <b>Export selected</b> (one CSV per index) or <b>Delete selected</b>.</li>
        <li><b>Archives</b> — server-side compressed exports: create, download, and restore/upload index archives. Uploaded snapshot <code>.zip</code>s are verified before being stored, and you choose whether to keep them or restore straight away. Restoring a snapshot lists the indices inside it so you can restore <b>all or just some</b> — ones that already exist here are flagged and unchecked, since a native restore cannot overwrite them.</li>
        <li><b>Document ids on restore</b> (CSV archives only) — before a restore starts you pick where each <code>_id</code> comes from: keep the archived <code>_id</code> so a repeat restore overwrites rather than duplicates, let Elasticsearch generate fresh ids, or take the id from a field such as <code>attackIpsId</code>. Snapshot <code>.zip</code>s are unaffected — a native restore always keeps the original ids.</li>
        <li><b>Fetching data script generator</b> — for CC machines this app cannot reach (no SSH, isolated site). Paste index names, pick an archive name, and download a standalone <code>sh</code> script. Run it on that machine as root and it performs the same snapshot flow locally, leaving a <code>&lt;name&gt;.zip</code> you can upload here. It needs only <code>sh</code>, <code>curl</code> and <code>zip</code>.</li>
        <li><b>Add</b> creates a new index — either an <i>empty</i> one by name, or a <i>possible CC index</i> picked from the live catalog and filled via the artificial-data dialog. Absent where this deployment cannot create indices.</li>
        <li><b>Possible</b> opens that same catalog on its own: every index family this machine's templates can produce, with real slice sizes, field counts and whether a live index exists yet. Read-only, and always available — it is how you tell an index that is <i>missing</i> from one that was never expected on this machine.</li>
        <li><b>Click any row</b> to open its Index Detail screen.</li>
      </ul>
      <p class="help-tip">Auto-refresh (top-right) keeps the health and counts current without manual refreshes.</p>`,
  },
  summary: {
    title: 'Summary Analytics', icon: 'bi-bar-chart-line',
    body: `
      <p>Aggregate attack analytics across the whole cluster — the same figures a CC techSupport bundle reports.</p>
      <h6>Stat ribbon (sticks to the top while scrolling)</h6>
      <ul>
        <li>Total Attacks · Avg / Max Duration · Peak Attack Bandwidth · Avg Gap Between Attacks · Avg Traffic.</li>
      </ul>
      <h6>Charts &amp; tables</h6>
      <ul>
        <li><b>Attacks Over Time</b> — switch Day / Week / Month granularity.</li>
        <li><b>Attack Categories</b> doughnut, <b>By Risk</b>, <b>By Status</b>, <b>Traffic Over Time</b>.</li>
        <li><b>Duration Stats per Attack Category</b> and <b>Inter-Attack Gap Stats</b> tables.</li>
      </ul>
      <h6>Export</h6>
      <ul>
        <li><b>Download JSON</b> saves the full analytics payload. The standalone script <code>scripts/generate_cc_summary.py</code> produces the byte-identical JSON on a CC host (localhost:9200) for techSupport automation.</li>
      </ul>
      <p class="help-tip">Widgets render independently — if one metric is missing in the data the rest still display.</p>`,
  },
  attacks: {
    title: 'Attacks View', icon: 'bi-shield-exclamation',
    body: `
      <p>A flat list of recent attacks using only the fields common to every attack type, so mixed attack sources line up in one table.</p>
      <h6>Columns</h6>
      <ul>
        <li>Attack ID · Type · Start Time · End Time · Device IP · Status (all times human-readable).</li>
      </ul>
      <h6>Controls</h6>
      <ul>
        <li><b>Status filter</b> — narrows to a single status (values are discovered from the loaded attacks).</li>
        <li><b>Refresh</b> and <b>Auto-refresh</b>.</li>
      </ul>
      <h6>Drill-down</h6>
      <ul>
        <li><b>Click any row</b> to open a details modal that searches every <code>*dp-*</code> and <code>*attack-data*</code> index for that attack ID (matching both the dash and underscore ID forms) and groups the matching documents per index — the full picture for that one attack.</li>
      </ul>`,
  },
  query: {
    title: 'Query Editor', icon: 'bi-terminal',
    body: `
      <p>Build and run Elasticsearch queries — either by typing plain English or by editing the raw JSON.</p>
      <h6>Natural-language box</h6>
      <ul>
        <li>Type criteria in plain English, then <b>Translate</b>. Field names, IPs, attack IDs and quoted values are recognised automatically. The box is multi-line — one criterion per line reads best; <kbd>Enter</kbd> translates, <kbd>Shift</kbd>+<kbd>Enter</kbd> adds a line.</li>
        <li>A criterion that matches <b>no field in the index</b> is not silently ignored: its badge is struck through and a warning says it was not applied — including when that leaves the query matching every document.</li>
        <li><b>OR within a field, AND across fields:</b> <i>"sourceIp is A or B or C and policyName is pol5"</i> → <code>(sourceIp IN [A,B,C]) AND (policyName = pol5)</code>. OR-ed values collapse into one <code>terms</code> clause.</li>
        <li><b>Negation:</b> <i>"is not"</i>, <i>"not"</i>, <i>"without"</i>, <i>"except"</i> put that criterion under <code>must_not</code> — for ordinary fields and for category / risk / status alike. A negation belongs to <b>its own criterion only</b>: in <i>"policy name is not pol16"</i> ⏎ <i>"attack id is 11-…"</i> the attack id is a positive match. Separate criteria with a new line, a comma, or <i>and</i> — a line ending (or starting) with <i>or</i> continues the previous one instead.</li>
        <li><b>Types</b> picker filters by attack category; <b>Time</b> sets a start/end range (each with lower and upper bounds).</li>
        <li><b>Sort by</b> lists the date fields the current index pattern <i>actually has</i> (they differ per family — <code>startTime</code>/<code>endTime</code>, <code>timestamp</code>, <code>raisedTime</code>…) and defaults to <b>None</b>. Sorting on a field an index lacks makes Elasticsearch reject the whole search, so nothing is assumed; change the pattern and the list follows.</li>
        <li><b>Interpreted as…</b> shows exactly how your text was understood; <b>Field suggestions</b> appear when a reference is ambiguous, so you can pick the right field.</li>
      </ul>
      <h6>Running</h6>
      <ul>
        <li>Edit the <b>JSON body</b> directly and set the <b>index pattern</b> (comma-separated, wildcards allowed). <b>Run</b> a single query or run the multi-index plan.</li>
        <li>In a multi-index plan each group has a <b>checkbox</b> — untick one to leave that index out. Groups whose query came out as <code>match_all</code> are badged <b>matches ALL docs</b> (your criteria don't exist in that index) and a one-click <b>Skip those N</b> drops them. The Run button shows how many groups are selected, and export / delete / modify act only on those.</li>
        <li>Each group also has its <b>own sort</b>: the dropdown beside it lists that index's date fields (they differ per family) and the ▼/▲ button flips the direction. Groups with no date field show <i>no sort</i> and the control is disabled. The global <b>Sort by</b> only seeds the plan when you translate; after that, set the sort per index here.</li>
      </ul>
      <h6>Working with results</h6>
      <ul>
        <li>View as <b>JSON / Table / CSV</b>; per-column <b>funnel filters</b>; <b>Query from Filters</b> turns the active filters into a fresh ES query; <b>Aggregate</b> groups by field(s).</li>
        <li><b>Export</b> the shown rows or all matching docs (server-side scroll). <b>Write mode</b> enables editing / deleting documents.</li>
      </ul>
      <h6>Acting on the whole result set</h6>
      <p>Two buttons appear beside the view switcher once a query has returned rows — delete-by-query and update-by-query, each in two steps.</p>
      <ul>
        <li><b>Modify results</b> — lists every field with a value box. Fill in one or more; <b>fields left blank keep their current values</b>. You then choose the scope and approve.</li>
        <li><b>Delete results</b> — removes the matched documents; you must type <code>DELETE</code> to confirm.</li>
        <li>Both ask whether to act on <b>only the rows shown</b> or <b>all documents the query matches</b>, and the count in the prompt is counted by Elasticsearch at that moment — not an estimate from the loaded page.</li>
        <li>On a shared CC you are also warned who else is connected, and they are notified of the change.</li>
      </ul>`,
  },
  index: {
    title: 'Index Detail', icon: 'bi-table',
    body: `
      <p>Inspect and manage a single index — its mapping, a document sample, and bulk operations.</p>
      <h6>Stat cards</h6>
      <ul>
        <li>Documents · Deleted Docs · Store Size · Mapped Fields · Showing / Total.</li>
      </ul>
      <h6>Sample documents</h6>
      <ul>
        <li>View as <b>JSON / Table / CSV</b>; set the <b>Show</b> size; the table has a sticky header and fills the screen.</li>
        <li><b>Funnel filters</b> per column, sortable headers, <b>Fields</b> to show/hide columns. Date fields display human-readable while still matching on the stored value.</li>
        <li><b>Dates in exported CSVs</b> — on screen dates are human-readable, but every downloaded CSV carries the value Elasticsearch stores (epoch millis), so an export can be re-imported as-is. A readable date would otherwise come back as a <i>string</i>: a real CC mapping rejects it (<code>failed to parse field … of type [date]</code>), and a brand-new index silently maps it as text, which breaks time filters and date sorting on that index.</li>
        <li><b>Older CSVs still import</b> — files exported before that change carry <code>2026-07-14 08:16:33 UTC</code>, and the importer converts that form back to epoch millis. Note it only ever held whole seconds, so those rows come back rounded to the second; a fresh export keeps the exact millisecond.</li>
        <li><b>Query from Filters</b>, <b>Aggregate</b>, and <b>Export</b> (shown rows or all matching docs). Query from Filters also fills the index pattern for you, derived from this index's name — <code>dp-attack-raw-ty-…</code> becomes <code>dp-attack-raw*</code>, and an index with no suffix at all (<code>alert-sid-0</code>) becomes <code>alert-sid-0*</code>, which still matches it. Edit the pattern freely before running.</li>
        <li>Sorting uses a date field this index really has (taken from its mapping), never an assumed <code>startTime</code>.</li>
        <li>Some CC fields (e.g. <code>applicationId</code> on ADC indices) are mapped as <i>analyzed text</i> with an exact <code>.raw</code> twin. Filtering and <b>Query from Filters</b> automatically target <code>&lt;field&gt;.raw</code> for those — an exact match on the analyzed field itself would return nothing, because the analyzer splits the value into fragments.</li>
      </ul>
      <h6>Index actions (top-right)</h6>
      <ul>
        <li><b>Artificial data</b> — generate synthetic documents into the index; slice-aware, skips already-existing data, and supports <b>dependency rules</b> (derive fields such as day / hourOfDay from a timestamp). Each field can take a fixed <b>values</b> list (cartesian product), a <b>random</b> value per document (type-aware: IP addresses, ports, numeric ranges, true/false, or name-based tokens), or an <b>increment</b> counter per document (optional prefix, start, step — e.g. an attack ID increasing by one).</li>
        <li><b>Document _id</b> (artificial data) — by default Elasticsearch assigns each id. Some CC families store the id in a field too: on <code>dp-attack-raw*</code> the <code>_id</code> <i>is</i> the <code>attackIpsId</code>. Choose <b>Copy a generated field</b> to reuse that field's value, or <b>Template</b> to build the id from several — <code>{attackIpsId}</code>, <code>{poId}-{attackIpsId}</code>, also <code>{n}</code> (document number), <code>{ts}</code> (main timestamp in millis) and <code>{index}</code>. A preview shows the first document's id, and only fields this run actually writes may be used.</li>
        <li><b>Import CSV</b> — load documents from a CSV (same shape as export). You choose where each <code>_id</code> comes from: keep the file's <code>_id</code> (re-importing then overwrites instead of duplicating), let Elasticsearch generate ids, or take the id from any column (e.g. <code>attackIpsId</code>). The chosen column still stays in the document body.</li>
        <li><b>Duplicate</b> — copy the index to a new name, optionally shifting all date fields.</li>
        <li><b>Delete Index</b> — remove the index permanently.</li>
      </ul>
      <h6>Write mode</h6>
      <ul>
        <li>Enables in-place cell edits, field deletes, and document deletes. When a filter matches more docs than are loaded, delete offers <b>Selected only</b> vs <b>All matching filter</b>.</li>
      </ul>`,
  },
  maria: {
    title: 'MariaDB — Schemas & Tables', icon: 'bi-diagram-3',
    body: `
      <p>Browse the CC's MariaDB — read-only, at the transaction level as well
      as the SQL, so nothing here can change data on a customer's appliance.</p>
      <h6>Three panes</h6>
      <ul>
        <li><b>Schemas</b> — curated, so 175 tables in <code>vision_ng</code>
          is not the answer you get to "where do I look". A schema this file
          has never catalogued still shows, badged <b>new</b> rather than
          hidden.</li>
        <li><b>Tables</b> — filterable; the row count is InnoDB's own
          estimate (<code>information_schema.tables.table_rows</code>), only
          refreshed when MariaDB next runs <code>ANALYZE</code>, so it can lag
          a table you just wrote to. <b>Refresh</b> re-asks for the current
          schema's table list and, if a table is open, its detail too.</li>
        <li><b>Detail</b> — columns, keys &amp; relations, and the first rows,
          each independently resizable and collapsible.</li>
      </ul>
      <h6>Keys &amp; relations</h6>
      <p>Declared <code>FOREIGN KEY</code>s split into <b>References out</b>
      (this table's own) and <b>Referenced by</b> (other tables pointing at
      this one — what tells you a row cannot simply be deleted). When a
      schema declares none, <b>Possibly related</b> lists same-named indexed
      columns elsewhere — a labelled <i>guess</i> from column names, never a
      substitute for a real constraint.</p>
      <h6>Join builder</h6>
      <p>Tick relations, then <b>Build join query</b>. Every column of every
      joined table is included by default, not just the join key — a join
      exists to answer questions about the <i>other</i> table. Use
      <b>Columns</b> in that dialog to narrow it down per table before
      running; the generated SQL is always shown and editable, never run
      unseen.</p>
      <h6>Columns and pop-out</h6>
      <p><b>Columns</b> on the rows section chooses which to display for the
      open table. The <i class="bi bi-box-arrow-up-right"></i> button opens
      the same rows in a separate window — Table / JSON / CSV, its own
      Columns picker, Download and Refresh — handy for keeping data visible
      while you work elsewhere.</p>
      <h6>Binary columns</h6>
      <p>A BLOB is shown as a size, not text — on this CC it is typically a
      serialised Java object. <b>View</b> decodes what can be read out of it
      (readable strings, and the JSON payload when one is found inside);
      <b>Download</b> gets the raw bytes.</p>
      <h6>Editing a cell</h6>
      <p>Off by default everywhere — it needs an operator to unlock it via a
      property file on the CC, because the identity and audit trail it should
      sit behind do not exist yet. When it is on: one column of one row,
      addressed by its full primary key, refused on key / binary / generated
      columns, and refused if the row changed since you loaded it.</p>
      <h6>Which account reaches this CC</h6>
      <p>Discovered automatically — the CC's own <code>mysql</code> wrapper,
      or (on appliances that use MariaDB's <code>unix_socket</code>
      authentication instead) the account's password read from the container's
      own environment. The <i class="bi bi-key"></i> button beside the version
      badge shows which account is in effect and lets you override it for this
      CC if discovery ever gets it wrong — effective on the next connection,
      no restart needed.</p>`,
  },
  mariaquery: {
    title: 'MariaDB — SQL Query', icon: 'bi-terminal',
    body: `
      <p>The escape hatch, not the front door — for the join the curated
      screens do not cover. Still read-only: enforced by the server at the
      transaction level, so this adds no privilege the browse screen does
      not already have.</p>
      <ul>
        <li>Only <code>SELECT</code>, <code>SHOW</code>,
          <code>DESCRIBE</code>, <code>EXPLAIN</code> and <code>WITH</code>
          are accepted, and only one statement at a time.</li>
        <li>Set the <b>schema</b> the query is aimed at and an optional
          <b>row limit</b>; the result reports how long it took, since "is
          this slow?" is otherwise invisible from this screen.</li>
        <li><b>Build query</b> opens a wizard for picking a table, columns
          and conditions without needing to remember SQL syntax — the
          statement it produces is always shown before running.</li>
        <li><b>Columns</b> chooses which result columns to display; the
          <i class="bi bi-box-arrow-up-right"></i> pop-out works the same way
          as the browse screen's.</li>
      </ul>`,
  },
  pg: {
    title: 'PostgreSQL — Databases & Tables', icon: 'bi-diagram-3',
    body: `
      <p>Browse the CC's PostgreSQL — read-only, the same guarantee as the
      MariaDB screen and for the same reason.</p>
      <h6>Why "databases", not "schemas"</h6>
      <p>PostgreSQL has no <code>USE</code> statement: one connection sees
      exactly one database, unlike MariaDB where one connection sees every
      schema on the server. So the left pane lists databases, and switching
      between them opens a fresh connection rather than just re-scoping one.
      Within a database, this CC keeps its own tables in the <code>public</code>
      schema.</p>
      <ul>
        <li><b>Databases</b> — curated the same way as MariaDB's schema list;
          an uncatalogued one is still shown, badged <b>new</b>.</li>
        <li><b>Tables</b> — the row count is PostgreSQL's own estimate
          (<code>pg_stat_user_tables.n_live_tup</code>), only refreshed by
          autovacuum or <code>ANALYZE</code>, so it can lag a table you just
          wrote to. <b>Refresh</b> re-asks for the current database's table
          list and, if a table is open, its detail too.</li>
        <li><b>Detail</b> — columns, keys &amp; relations, and the first
          rows, same layout as MariaDB's.</li>
      </ul>
      <h6>Keys &amp; relations</h6>
      <p>Same three-part answer as MariaDB: declared foreign keys as
      <b>References out</b> / <b>Referenced by</b>, plus a labelled
      <b>Possibly related</b> guess from column names when a database
      declares no constraints at all.</p>
      <p>There is no visual join builder here yet — porting MariaDB's join
      builder and its WHERE-condition wizard to a second SQL dialect is its
      own project. The SQL Query screen is still the full read-only escape
      hatch for a join, just typed rather than point-and-click for now.</p>
      <h6>Columns and pop-out</h6>
      <p>Same as MariaDB's: <b>Columns</b> chooses which to display, and the
      <i class="bi bi-box-arrow-up-right"></i> button opens the same rows in
      a separate window with its own view switching, column picker, download
      and refresh.</p>
      <h6>Binary columns</h6>
      <p>A <code>bytea</code> column is shown as a size, not text — the same
      decoder as MariaDB's BLOBs (this CC serialises Java objects into both).
      <b>View</b> decodes what can be read out of it; <b>Download</b> gets the
      raw bytes.</p>
      <h6>Editing a cell</h6>
      <p>Off by default everywhere, unlocked the same way as MariaDB's — an
      operator's property file on the CC, because the identity and audit
      trail it should sit behind do not exist yet.</p>`,
  },
  pgquery: {
    title: 'PostgreSQL — SQL Query', icon: 'bi-terminal',
    body: `
      <p>The escape hatch, not the front door — same role as MariaDB's SQL
      Query screen. Read-only, enforced by the server at the transaction
      level via <code>BEGIN READ ONLY</code>, not just by checking the
      statement's leading keyword.</p>
      <ul>
        <li>Only <code>SELECT</code>, <code>WITH</code>,
          <code>EXPLAIN</code>, <code>SHOW</code>, <code>TABLE</code> and
          <code>VALUES</code> are accepted, and only one statement at a
          time.</li>
        <li>Set the <b>database</b> the query is aimed at and an optional
          <b>row limit</b>; the result reports how long it took.</li>
        <li>No visual query wizard yet, unlike MariaDB's — this box is still
          the full escape hatch, just typed rather than point-and-click.</li>
        <li><b>Columns</b> chooses which result columns to display; the
          <i class="bi bi-box-arrow-up-right"></i> pop-out works the same way
          as the browse screen's.</li>
      </ul>`,
  },
};

/** Open the online-help modal for a screen (keys match showView names). */
function showHelp(screen) {
  const c = HELP_CONTENT[screen];
  if (!c) return;
  document.querySelector('.help-overlay')?.remove();
  const wrap = document.createElement('div');
  wrap.className = 'help-overlay';
  wrap.innerHTML =
    '<div class="help-modal" role="dialog" aria-modal="true">' +
      '<div class="help-head">' +
        '<div class="help-title"><i class="bi ' + c.icon + ' me-2"></i>' + esc(c.title) + ' — Help</div>' +
        '<button class="btn-close btn-close-white help-close" aria-label="Close" title="Close (Esc)"></button>' +
      '</div>' +
      '<div class="help-body">' + c.body + '</div>' +
      '<div class="help-foot">' +
        '<span class="text-secondary small">Press <kbd>Esc</kbd> or click outside to close · every screen has its own <i class="bi bi-question-circle"></i> Help.</span>' +
        '<button class="btn btn-sm btn-primary help-close">Got it</button>' +
      '</div>' +
    '</div>';
  document.body.appendChild(wrap);
  const close = () => { wrap.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey);
  wrap.addEventListener('click', (e) => {
    if (e.target === wrap || e.target.closest('.help-close')) close();
  });
}

