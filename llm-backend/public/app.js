/* =====================================================
   Infochat Admin — app.js
   All requests go to the same origin (FastAPI on :3001).
   ===================================================== */

// Same origin, under the path prefix the server set in <base href> (e.g. /admin); '' at the root.
const BASE = new URL(document.baseURI).pathname.replace(/\/$/, '');
const API_BASE = BASE + '/api';

// The current URL path without BASE, as the router's /workspaces, /workspace/... scheme sees it.
function appPath() {
  const p = window.location.pathname;
  return (p.startsWith(BASE) ? p.slice(BASE.length) : p) || '/';
}

// =====================================================
// STATE
// =====================================================
let loggedIn = false;  // the session itself is an HttpOnly cookie set by /auth/login
let currentWorkspaceSlug = null;
let currentWorkspaceName = '';
let activeStreamController = null;

// =====================================================
// DOM REFS
// =====================================================
const loginScreen       = document.getElementById('login-screen');
const appScreen         = document.getElementById('app-screen');
const loginForm         = document.getElementById('login-form');
const apiKeyInput       = document.getElementById('api-key-input');
const loginError        = document.getElementById('login-error');
const logoutBtn         = document.getElementById('logout-btn');

const navBtns           = document.querySelectorAll('.nav-btn');
const viewWorkspaces    = document.getElementById('view-workspaces');
const viewSettings      = document.getElementById('view-settings');
const viewActivity      = document.getElementById('view-activity');
const viewWorkspaceDetail = document.getElementById('view-workspace-detail');

const newWorkspaceBtn   = document.getElementById('new-workspace-btn');
const workspaceTable    = document.getElementById('workspace-table');
const workspaceTableBody= document.getElementById('workspace-table-body');
const workspaceListEmpty= document.getElementById('workspace-list-empty');

const createModal       = document.getElementById('create-workspace-modal');
const closeModalBtn     = document.getElementById('close-modal-btn');
const createWorkspaceForm = document.getElementById('create-workspace-form');
const createWorkspaceMsg  = document.getElementById('create-workspace-msg');

const backToWorkspaces  = document.getElementById('back-to-workspaces');
const workspaceDetailName = document.getElementById('workspace-detail-name');

const tabBtns           = document.querySelectorAll('.tab-btn');
const tabPanels         = document.querySelectorAll('.tab-panel');

const workspaceSettingsForm = document.getElementById('workspace-settings-form');
const wsSettingsMsg     = document.getElementById('ws-settings-msg');

const embedForm         = document.getElementById('embed-form');
const embedFileInput    = document.getElementById('embed-file-input');
const embedStatus       = document.getElementById('embed-status');
const docTable          = document.getElementById('doc-table');
const docTableBody      = document.getElementById('doc-table-body');
const docListEmpty      = document.getElementById('doc-list-empty');

const queryForm         = document.getElementById('query-form');
const queryInput        = document.getElementById('query-input');
const querySubmitBtn    = document.getElementById('query-submit-btn');
const queryClearBtn     = document.getElementById('query-clear-btn');
const queryResult       = document.getElementById('query-result');
const queryAnswer       = document.getElementById('query-answer');
const querySources      = document.getElementById('query-sources');
const queryRewriteDebug = document.getElementById('query-rewrite-debug');
const queryRewrittenText= document.getElementById('query-rewritten-text');

const globalSettingsForm = document.getElementById('global-settings-form');
const globalSettingsMsg  = document.getElementById('global-settings-msg');

const logEntries    = document.getElementById('log-entries');
const logEmpty      = document.getElementById('log-empty');
const logCount      = document.getElementById('log-count');
const clearLogBtn   = document.getElementById('clear-log-btn');
const logDateFilter = document.getElementById('log-date-filter');
const logDateClear  = document.getElementById('log-date-clear');
const logMoreBtn    = document.getElementById('log-more-btn');

// =====================================================
// HELPERS
// =====================================================

function showMsg(el, text, type = 'neutral') {
  el.textContent = text;
  el.className = 'status-msg' + (type !== 'neutral' ? ' ' + type : '');
  if (type === 'success') {
    setTimeout(() => { el.textContent = ''; el.className = 'status-msg'; }, 3000);
  }
}

async function apiFetch(path, options = {}) {
  const res = await fetch(API_BASE + path, {
    ...options,
    headers: {
      'Accept': 'application/json',
      'X-Infochat-Source': 'admin-ui',
      ...(options.headers || {}),
    },
  });
  // The only 403 the API sends is "not logged in": the session expired or was ended
  if (res.status === 403 && loggedIn) {
    loggedIn = false;
    showLogin();
  }
  return res;
}

// =====================================================
// AUTH
// =====================================================

/** True if the browser holds a valid admin session cookie. */
async function checkSession() {
  try {
    const res = await fetch(API_BASE + '/auth/verify', { headers: { 'Accept': 'application/json' } });
    return res.ok;
  } catch {
    return false;
  }
}

/** Exchange the admin key for a session cookie; the key itself is not kept. */
async function logIn(key) {
  try {
    const res = await fetch(API_BASE + '/auth/login', {
      method: 'POST',
      headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify({ key }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

loginForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const key = apiKeyInput.value.trim();
  if (!key) return;

  loginError.textContent = 'Verifying...';
  loginError.classList.remove('hidden');
  loginError.style.color = '#757575';

  if (await logIn(key)) {
    loggedIn = true;
    showApp();
  } else {
    loginError.textContent = 'Invalid API key or server unreachable.';
    loginError.style.color = '#c0392b';
  }
});

logoutBtn.addEventListener('click', async () => {
  loggedIn = false;
  await fetch(API_BASE + '/auth/logout', { method: 'POST' }).catch(() => {});
  showLogin();
});

function showLogin() {
  appScreen.classList.add('hidden');
  loginScreen.classList.remove('hidden');
  apiKeyInput.value = '';
  loginError.classList.add('hidden');
}

function showApp() {
  loginScreen.classList.add('hidden');
  appScreen.classList.remove('hidden');
  // Restore the view for the current URL (handles direct loads, refreshes,
  // and back/forward after login).  If the path is just '/', redirect to /workspaces.
  router();
}

// =====================================================
// ROUTING (History API)
// =====================================================

// Tab name → panel element id mapping
const TAB_PANELS = {
  settings:  'ws-settings',
  documents: 'ws-documents',
  query:     'ws-query',
  log:       'ws-log',
};
// Reverse: panel id → URL tab segment
const PANEL_TO_TAB = Object.fromEntries(Object.entries(TAB_PANELS).map(([k, v]) => [v, k]));

/**
 * Navigate to a new path, pushing a history entry and triggering the router.
 * Pass { replace: true } to replace instead of push (used during init).
 */
function navigateTo(path, { replace = false } = {}) {
  if (replace) {
    history.replaceState(null, '', BASE + path);
  } else {
    history.pushState(null, '', BASE + path);
  }
  router();
}

/**
 * Parse the current pathname and activate the correct view/tab.
 * Must only be called when the user is already authenticated.
 *
 * URL scheme:
 *   /                          → redirect to /workspaces
 *   /workspaces                → workspace list
 *   /settings                  → global settings
 *   /workspace/{slug}          → workspace detail, settings tab
 *   /workspace/{slug}/settings → workspace detail, settings tab
 *   /workspace/{slug}/documents→ workspace detail, documents tab
 *   /workspace/{slug}/query    → workspace detail, query tab
 *   /workspace/{slug}/log      → workspace detail, log tab
 */
async function router() {
  const path = appPath();

  // Root → workspaces
  if (path === '/' || path === '') {
    navigateTo('/workspaces', { replace: true });
    return;
  }

  if (path === '/workspaces') {
    _showViewDOM('workspaces');
    loadWorkspaces();
    return;
  }

  if (path === '/activity') {
    _showViewDOM('activity');
    loadActivity();
    return;
  }

  if (path === '/settings') {
    _showViewDOM('settings');
    loadGlobalSettings();
    return;
  }

  // /workspace/{slug}[/{tab}]
  const wsMatch = path.match(/^\/workspace\/([^/]+)(?:\/([^/]*))?$/);
  if (wsMatch) {
    const slug = decodeURIComponent(wsMatch[1]);
    const tab  = wsMatch[2] || 'settings';

    // Validate tab name
    const panelId = TAB_PANELS[tab] || 'ws-settings';

    // Fetch workspace to get name (and detect 404)
    const res = await apiFetch(`/workspace/${slug}`);
    if (!res.ok) {
      // Workspace not found — redirect to list
      navigateTo('/workspaces', { replace: true });
      return;
    }
    const ws = await res.json();

    // Clear query result when switching to a different workspace
    if (slug !== currentWorkspaceSlug) {
      if (activeStreamController) {
        activeStreamController.abort();
        activeStreamController = null;
      }
      queryAnswer.textContent = '';
      querySources.innerHTML = '';
      queryResult.classList.add('hidden');
      queryRewriteDebug.classList.add('hidden');
      queryRewrittenText.textContent = '';
      queryInput.value = '';
      querySubmitBtn.disabled = false;
      querySubmitBtn.textContent = 'Ask';
    }

    // Update state
    currentWorkspaceSlug = slug;
    currentWorkspaceName = ws.name;
    workspaceDetailName.textContent = ws.name;

    // Activate the right tab in DOM (without pushing a new URL)
    _activateTab(panelId);

    _showViewDOM('workspace-detail');
    loadWorkspaceSettings(slug);
    loadDocList(slug);
    if (panelId === 'ws-log') loadQueryLog(slug);
    return;
  }

  // Unknown path → workspaces
  navigateTo('/workspaces', { replace: true });
}

/** Internal: toggle the three top-level view sections without touching the URL. */
function _showViewDOM(name) {
  viewWorkspaces.classList.add('hidden');
  viewSettings.classList.add('hidden');
  viewActivity.classList.add('hidden');
  viewWorkspaceDetail.classList.add('hidden');

  navBtns.forEach(b => b.classList.toggle('active', b.dataset.view === name));

  if (name === 'workspaces') {
    viewWorkspaces.classList.remove('hidden');
  } else if (name === 'settings') {
    viewSettings.classList.remove('hidden');
  } else if (name === 'activity') {
    viewActivity.classList.remove('hidden');
  } else if (name === 'workspace-detail') {
    viewWorkspaceDetail.classList.remove('hidden');
  }
}

/** Internal: activate a tab panel by its element id without touching the URL. */
function _activateTab(panelId) {
  tabBtns.forEach(b => {
    b.classList.toggle('active', b.dataset.tab === panelId);
  });
  tabPanels.forEach(p => {
    p.classList.toggle('hidden', p.id !== panelId);
  });
}

// =====================================================
// NAVIGATION
// =====================================================

/**
 * Public showView — updates the URL and switches the view.
 * For workspace-detail, prefer openWorkspace() or the tab handlers instead.
 */
function showView(name) {
  if (name === 'workspaces') {
    navigateTo('/workspaces');
  } else if (name === 'settings') {
    navigateTo('/settings');
  } else if (name === 'activity') {
    navigateTo('/activity');
  } else {
    // workspace-detail without a slug context — just switch DOM
    _showViewDOM(name);
  }
}

navBtns.forEach(btn => {
  btn.addEventListener('click', () => showView(btn.dataset.view));
});

// Browser back/forward
window.addEventListener('popstate', () => {
  if (loggedIn) router();
});

// =====================================================
// TABS (workspace detail)
// =====================================================

tabBtns.forEach(btn => {
  btn.addEventListener('click', () => {
    if (!currentWorkspaceSlug) return;
    const tab = PANEL_TO_TAB[btn.dataset.tab] || 'settings';
    // Push the tab into the URL — popstate/router will handle DOM activation.
    // Use replace if we're already on a workspace URL to avoid stacking tab
    // entries in history (navigating between tabs shouldn't fill up history).
    const currentIsWorkspace = /^\/workspace\//.test(appPath());
    navigateTo(`/workspace/${encodeURIComponent(currentWorkspaceSlug)}/${tab}`,
               { replace: currentIsWorkspace });
  });
});

// =====================================================
// WORKSPACES LIST
// =====================================================

async function loadWorkspaces() {
  const res = await apiFetch('/workspaces');
  if (!res.ok) {
    workspaceListEmpty.textContent = 'Failed to load workspaces.';
    workspaceListEmpty.classList.remove('hidden');
    workspaceTable.classList.add('hidden');
    return;
  }
  const workspaces = await res.json();
  renderWorkspaceTable(workspaces);
}

// Dates as the uploader shows them: "Oct 26, 2026", and with the time where it matters
const formatDay = (iso) => new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
const formatDateTime = (iso) => new Date(iso).toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' });

function renderWorkspaceTable(workspaces) {
  workspaceTableBody.innerHTML = '';

  if (!workspaces.length) {
    workspaceListEmpty.classList.remove('hidden');
    workspaceTable.classList.add('hidden');
    return;
  }

  workspaceListEmpty.classList.add('hidden');
  workspaceTable.classList.remove('hidden');

  workspaces.forEach(ws => {
    // The whole row opens the workspace (as a website row does in the uploader); the name is
    // the keyboard control. Chat and Upload open the other apps; Delete shows on hover.
    const tr = document.createElement('tr');
    tr.className = 'ws-row';
    tr.innerHTML = `
      <td><button class="ws-open" data-slug="${escHtml(ws.slug)}" data-name="${escHtml(ws.name)}">${escHtml(ws.name)}</button></td>
      <td><code title="${escHtml(ws.slug)}">${escHtml(ws.slug)}</code></td>
      <td title="${escHtml(ws.llm_model || '')}">${escHtml(ws.llm_model || '—')}</td>
      <td>
        <a class="action-link" href="/chat/${encodeURIComponent(ws.slug)}" target="_blank" rel="noopener">Chat</a>
        <a class="action-link" href="/upload/${encodeURIComponent(ws.slug)}" target="_blank" rel="noopener">Upload</a>
        <button class="action-link danger delete-ws" data-slug="${escHtml(ws.slug)}" data-name="${escHtml(ws.name)}">Delete</button>
      </td>
    `;
    tr.addEventListener('click', (e) => {
      if (e.target.closest('a, .delete-ws')) return;
      openWorkspace(ws.slug, ws.name);
    });
    workspaceTableBody.appendChild(tr);
  });

  document.querySelectorAll('.delete-ws').forEach(btn => {
    btn.addEventListener('click', () => {
      if (confirm(`Delete workspace "${btn.dataset.name}"?\n\nThis will permanently remove all embeddings and cannot be undone.`)) {
        deleteWorkspace(btn.dataset.slug);
      }
    });
  });
}

async function deleteWorkspace(slug) {
  const res = await apiFetch(`/workspace/${slug}`, { method: 'DELETE' });
  if (res.ok) {
    loadWorkspaces();
  } else {
    const err = await res.json().catch(() => ({}));
    alert(`Failed to delete workspace: ${err.detail || res.status}`);
  }
}

// =====================================================
// WORKSPACE DETAIL
// =====================================================

function openWorkspace(slug, name, tab) {
  // Navigate to the URL — router() will handle loading data and switching DOM.
  // If a specific tab is given, navigate to /workspace/{slug}/{tab},
  // otherwise land on the settings tab (default).
  const tabSegment = tab && TAB_PANELS[tab] ? `/${tab}` : '';
  navigateTo(`/workspace/${encodeURIComponent(slug)}${tabSegment}`);
}

backToWorkspaces.addEventListener('click', () => {
  currentWorkspaceSlug = null;
  navigateTo('/workspaces');
});

// =====================================================
// WORKSPACE SETTINGS
// =====================================================

async function loadWorkspaceSettings(slug) {
  const [wsRes, defaultsRes] = await Promise.all([
    apiFetch(`/workspace/${slug}`),
    apiFetch('/defaults'),
  ]);
  if (!wsRes.ok) return;
  const ws = await wsRes.json();
  setAccessKeyShown(false);
  fillForm(workspaceSettingsForm, ws);
  loadSettingsModels(workspaceSettingsForm, wsSettingsMsg, ws, slug);

  let defaults = null;
  if (defaultsRes.ok) {
    defaults = await defaultsRes.json();
    workspaceSettingsForm._promptDefaults = defaults;

    // Fill blank prompts with built-in defaults
    const spEl = workspaceSettingsForm.elements['system_prompt'];
    const webEnabled = workspaceSettingsForm.elements['searxng_enabled'].checked;
    if (spEl && !spEl.value) {
      spEl.value = webEnabled ? defaults.default_system_prompt_web : defaults.default_system_prompt_rag;
    }
    const rpEl = workspaceSettingsForm.elements['rewrite_prompt'];
    if (rpEl && !rpEl.value) rpEl.value = defaults.default_rewrite_prompt;
  }

  // Attach the searxng toggle → swap system prompt listener (idempotent via flag)
  if (!workspaceSettingsForm._toggleListenerAttached) {
    workspaceSettingsForm._toggleListenerAttached = true;
    workspaceSettingsForm.elements['searxng_enabled'].addEventListener('change', function () {
      const d = workspaceSettingsForm._promptDefaults;
      if (!d) return;
      const spEl = workspaceSettingsForm.elements['system_prompt'];
      if (!spEl) return;
      const cur = spEl.value;
      if (cur === d.default_system_prompt_rag || cur === d.default_system_prompt_web) {
        spEl.value = this.checked ? d.default_system_prompt_web : d.default_system_prompt_rag;
      }
    });
  }
}

const accessKeyToggle = document.getElementById('access-key-toggle');
function setAccessKeyShown(shown) {
  workspaceSettingsForm.elements['access_key'].type = shown ? 'text' : 'password';
  accessKeyToggle.textContent = shown ? 'Hide' : 'Show';
}
accessKeyToggle.addEventListener('click', () => {
  setAccessKeyShown(workspaceSettingsForm.elements['access_key'].type === 'password');
});

workspaceSettingsForm.elements['api_key'].addEventListener('change', () => {
  loadSettingsModels(workspaceSettingsForm, wsSettingsMsg, currentModels(workspaceSettingsForm), currentWorkspaceSlug);
});

workspaceSettingsForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const data = formToObj(workspaceSettingsForm, [
    'llm_model', 'api_key', 'temperature', 'top_n',
    'similarity_threshold', 'system_prompt', 'embed_api_key',
    'searxng_num_results', 'searxng_query_suffix', 'rewrite_model', 'rewrite_prompt',
  ]);
  castNumbers(data, ['temperature', 'top_n', 'similarity_threshold', 'searxng_num_results']);
  data.searxng_enabled = workspaceSettingsForm.elements['searxng_enabled'].checked ? 1 : 0;

  showMsg(wsSettingsMsg, 'Saving...');
  const res = await apiFetch(`/workspace/${currentWorkspaceSlug}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });

  if (res.ok) {
    showMsg(wsSettingsMsg, 'Saved.', 'success');
  } else {
    const err = await res.json().catch(() => ({}));
    showMsg(wsSettingsMsg, `Error: ${err.detail || res.status}`, 'error');
  }
});

// =====================================================
// CREATE WORKSPACE
// =====================================================

newWorkspaceBtn.addEventListener('click', async () => {
  createWorkspaceForm.reset();
  createWorkspaceMsg.textContent = '';
  createModal.classList.remove('hidden');

  // Pre-fill with current global defaults so the admin sees what will be used.
  // Prompts stay blank: a blank prompt falls back to the global default.
  resetModelSelects();
  showCreateStep(1);
  const res = await apiFetch('/settings');
  if (res.ok) {
    const { system_prompt, rewrite_prompt, api_key_hint, embed_api_key_hint, ...settings } = await res.json();
    fillForm(createWorkspaceForm, settings);
  }
});

// --- Create-workspace steps: 1 name & keys, 2 models & settings, 3 other ---
const CREATE_STEP_TITLES = {
  1: 'Name and keys',
  2: 'Models and retrieval',
  3: 'Web search and rewriting',
};
const createBackBtn   = document.getElementById('create-back-btn');
const createNextBtn   = document.getElementById('create-next-btn');
const createSubmitBtn = document.getElementById('create-submit-btn');
const createStepLabel = document.getElementById('create-step-label');
let createStep = 1;

function showCreateStep(n) {
  createStep = n;
  createWorkspaceForm.querySelectorAll('.wizard-step').forEach(el => {
    el.classList.toggle('hidden', Number(el.dataset.step) !== n);
  });
  createStepLabel.textContent = `Step ${n} of 3 \u00b7 ${CREATE_STEP_TITLES[n]}`;
  createBackBtn.classList.toggle('hidden', n === 1);
  createNextBtn.classList.toggle('hidden', n === 3);
  createSubmitBtn.classList.toggle('hidden', n !== 3);
  createWorkspaceMsg.textContent = '';
}

// Populate the model dropdowns with whatever the entered API key can access
const _REWRITE_OFF_OPTION = '<option value="">Disabled</option>';
const _DEFAULT_OPTION = '<option value="">Use global default</option>';

function setSelectOptions(select, models, defaultOption = _DEFAULT_OPTION) {
  select.innerHTML = defaultOption;
  models.forEach(m => select.add(new Option(m, m)));
}

// Settings pages: fill the model dropdowns from the key in the API Key field, or the
// saved key when it's blank. The saved choice stays selected even if the key can't use it.
const _SETTINGS_MODEL_FIELDS = [
  ['llm_model', 'llm_models'],
  ['embed_model', 'embedding_models'],
  ['rewrite_model', 'llm_models'],
];

async function loadSettingsModels(form, msgEl, current, workspace) {
  const selects = _SETTINGS_MODEL_FIELDS
    .map(([name, list]) => [form.elements[name], list, current[name] || ''])
    .filter(([el]) => el && el.tagName === 'SELECT');
  // Show the saved choice right away, so saving before the list loads keeps it
  const defaults = new Map(selects.map(([el]) => [el, el.options[0].outerHTML]));
  selects.forEach(([el, , value]) => {
    setSelectOptions(el, value ? [value] : [], defaults.get(el));
    el.value = value;
  });

  const res = await apiFetch('/models', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ api_key: form.elements['api_key'].value.trim(), workspace }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    showMsg(msgEl, `Could not load models: ${err.detail || res.status}`, 'error');
    return;
  }
  const models = await res.json();
  selects.forEach(([el, list, value]) => {
    setSelectOptions(el, models[list], defaults.get(el));
    if (value && !models[list].includes(value)) el.add(new Option(`${value} (not available to this key)`, value));
    el.value = value;
  });
}

function currentModels(form) {
  return Object.fromEntries(_SETTINGS_MODEL_FIELDS.map(([name]) => [name, form.elements[name].value]));
}

function resetModelSelects() {
  ['llm_model', 'embed_model'].forEach(name => {
    createWorkspaceForm.elements[name].innerHTML = _DEFAULT_OPTION;
  });
  createWorkspaceForm.elements['rewrite_model'].innerHTML = _REWRITE_OFF_OPTION;
}

// Returns true when the dropdowns were filled; shows an error on step 1 if not.
async function loadModelOptions() {
  const key = createWorkspaceForm.elements['api_key'].value.trim();
  const res = await apiFetch('/models', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ api_key: key }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    showMsg(createWorkspaceMsg, `Could not load models: ${err.detail || res.status}`, 'error');
    return false;
  }
  const { llm_models, embedding_models } = await res.json();
  setSelectOptions(createWorkspaceForm.elements['llm_model'], llm_models);
  setSelectOptions(createWorkspaceForm.elements['embed_model'], embedding_models);
  setSelectOptions(createWorkspaceForm.elements['rewrite_model'], llm_models, _REWRITE_OFF_OPTION);
  return true;
}

async function createNext() {
  if (createStep === 1) {
    const nameEl = createWorkspaceForm.elements['name'];
    const keyEl = createWorkspaceForm.elements['api_key'];
    if (!nameEl.value.trim()) {
      showMsg(createWorkspaceMsg, 'Enter a workspace name.', 'error');
      nameEl.focus();
      return;
    }
    if (!keyEl.value.trim()) {
      showMsg(createWorkspaceMsg, 'Enter an API key to load the available models.', 'error');
      keyEl.focus();
      return;
    }
    createNextBtn.disabled = true;
    createNextBtn.classList.add('loading');
    createNextBtn.textContent = 'Loading models...';
    createWorkspaceMsg.textContent = '';
    let ok = false;
    try {
      ok = await loadModelOptions();
    } catch (err) {
      showMsg(createWorkspaceMsg, `Could not load models: ${err.message}`, 'error');
    } finally {
      createNextBtn.disabled = false;
      createNextBtn.classList.remove('loading');
      createNextBtn.textContent = 'Next';
    }
    if (!ok) return;
  }
  showCreateStep(createStep + 1);
}

createNextBtn.addEventListener('click', createNext);
createBackBtn.addEventListener('click', () => showCreateStep(createStep - 1));

closeModalBtn.addEventListener('click', () => {
  createModal.classList.add('hidden');
});

createWorkspaceForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  // Enter on an earlier step advances instead of creating
  if (createStep < 3) return createNext();
  const data = formToObj(createWorkspaceForm, [
    'name', 'llm_model', 'api_key', 'temperature', 'top_n',
    'similarity_threshold',
    'embed_model', 'embed_api_key', 'system_prompt',
    'searxng_num_results', 'searxng_query_suffix', 'rewrite_model', 'rewrite_prompt',
  ]);
  castNumbers(data, ['temperature', 'top_n', 'similarity_threshold', 'searxng_num_results']);
  data.searxng_enabled = createWorkspaceForm.elements['searxng_enabled'].checked ? 1 : 0;

  showMsg(createWorkspaceMsg, 'Creating...');
  const res = await apiFetch('/workspace', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });

  if (res.ok) {
    const ws = await res.json();
    createModal.classList.add('hidden');
    openWorkspace(ws.slug, ws.name);
  } else {
    const err = await res.json().catch(() => ({}));
    showMsg(createWorkspaceMsg, `Error: ${err.detail || res.status}`, 'error');
  }
});

// =====================================================
// EMBED (upload file)
// =====================================================

embedForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const file = embedFileInput.files[0];
  if (!file) return;

  showMsg(embedStatus, 'Uploading and embedding...');

  const formData = new FormData();
  formData.append('file', file);

  const res = await apiFetch(`/workspace/${currentWorkspaceSlug}/embed`, {
    method: 'POST',
    body: formData,
  });

  if (res.ok) {
    const result = await res.json();
    showMsg(embedStatus, `Embedded ${result.chunks_embedded} chunks.`, 'success');
    embedFileInput.value = '';
    loadDocList(currentWorkspaceSlug);
  } else {
    const err = await res.json().catch(() => ({}));
    showMsg(embedStatus, `Error: ${err.detail || res.status}`, 'error');
  }
});

// =====================================================
// DOC LIST
// =====================================================

async function loadDocList(slug) {
  const res = await apiFetch(`/docs/${slug}`);
  if (!res.ok) {
    docListEmpty.textContent = 'Failed to load documents.';
    docListEmpty.classList.remove('hidden');
    docTable.classList.add('hidden');
    return;
  }
  const docs = await res.json();
  renderDocTable(docs, slug);
}

function renderDocTable(docs, slug) {
  docTableBody.innerHTML = '';

  if (!docs.length) {
    docListEmpty.classList.remove('hidden');
    docTable.classList.add('hidden');
    return;
  }

  docListEmpty.classList.add('hidden');
  docTable.classList.remove('hidden');

  docs.forEach(doc => {
    const tr = document.createElement('tr');
    const date = doc.uploaded_at ? formatDay(doc.uploaded_at) : '—';
    tr.innerHTML = `
      <td>${escHtml(doc.filename)}</td>
      <td><code>${escHtml(doc.doc_id)}</code></td>
      <td>${doc.chunks_embedded ?? '—'}</td>
      <td title="${escHtml(doc.uploaded_at ? formatDateTime(doc.uploaded_at) : '')}">${escHtml(date)}</td>
      <td>
        <button class="action-link danger delete-doc"
          data-slug="${escHtml(slug)}"
          data-doc-id="${escHtml(doc.doc_id)}"
          data-filename="${escHtml(doc.filename)}">
          Delete
        </button>
      </td>
    `;
    docTableBody.appendChild(tr);
  });

  document.querySelectorAll('.delete-doc').forEach(btn => {
    btn.addEventListener('click', () => {
      if (confirm(`Delete "${btn.dataset.filename}" and remove all its embeddings?`)) {
        deleteDoc(btn.dataset.slug, btn.dataset.docId);
      }
    });
  });
}

async function deleteDoc(slug, docId) {
  const res = await apiFetch(`/workspace/${slug}/embed/${encodeURIComponent(docId)}`, {
    method: 'DELETE',
  });

  if (res.ok) {
    loadDocList(slug);
  } else {
    const err = await res.json().catch(() => ({}));
    alert(`Failed to delete: ${err.detail || res.status}`);
  }
}

// =====================================================
// QUERY (streaming SSE)
// =====================================================

queryForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const question = queryInput.value.trim();
  if (!question) return;

  if (activeStreamController) {
    activeStreamController.abort();
    activeStreamController = null;
  }

  queryAnswer.textContent = '';
  querySources.innerHTML = '';
  queryRewriteDebug.classList.add('hidden');
  queryRewrittenText.textContent = '';
  queryResult.classList.remove('hidden');
  querySubmitBtn.disabled = true;
  querySubmitBtn.textContent = 'Asking...';

  const controller = new AbortController();
  activeStreamController = controller;

  try {
    const res = await apiFetch(`/workspace/${currentWorkspaceSlug}/query/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question }),
      signal: controller.signal,
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      queryAnswer.textContent = `Error: ${err.detail || res.status}`;
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    // SSE-spec-compliant parser: accumulate per-event fields, dispatch on blank line
    let eventName = '';
    let dataLines = [];

    function dispatchEvent() {
      if (!dataLines.length) return;
      const data = dataLines.join('\n');
      if (eventName === 'token') {
        queryAnswer.textContent += data.replace(/\\n/g, '\n');
      } else if (eventName === 'rewritten_query') {
        queryRewrittenText.textContent = data.replace(/\\n/g, '\n');
        queryRewriteDebug.classList.remove('hidden');
      } else if (eventName === 'sources') {
        try { renderSources(JSON.parse(data)); } catch {}
      } else if (eventName === 'error') {
        queryAnswer.textContent = `Error: ${data}`;
      }
      eventName = '';
      dataLines = [];
    }

    while (true) {
      const { done, value } = await reader.read();
      if (done) { dispatchEvent(); break; }

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (line === '') {
          dispatchEvent();
        } else if (line.startsWith('event:')) {
          eventName = line.slice(6).trim();
        } else if (line.startsWith('data:')) {
          dataLines.push(line.slice(5).replace(/^ /, ''));
        }
      }
    }
  } catch (err) {
    if (err.name !== 'AbortError') {
      queryAnswer.textContent = `Stream error: ${err.message}`;
    }
  } finally {
    activeStreamController = null;
    querySubmitBtn.disabled = false;
    querySubmitBtn.textContent = 'Ask';
    // Refresh log tab if it's currently visible
    const logTab = document.getElementById('ws-log');
    if (logTab && !logTab.classList.contains('hidden') && currentWorkspaceSlug) {
      loadQueryLog(currentWorkspaceSlug);
    }
  }
});

queryClearBtn.addEventListener('click', () => {
  if (activeStreamController) {
    activeStreamController.abort();
    activeStreamController = null;
  }
  queryInput.value = '';
  queryAnswer.textContent = '';
  querySources.innerHTML = '';
  queryResult.classList.add('hidden');
  queryRewriteDebug.classList.add('hidden');
  queryRewrittenText.textContent = '';
  querySubmitBtn.disabled = false;
  querySubmitBtn.textContent = 'Ask';
});

// "Title › Section" for a document source. Docling heading paths usually start
// with the page title, so headings equal to the title are skipped.
function sourceLabel(s) {
  const title = s.title || s.filename || 'Unknown';
  const section = (s.headings || []).filter(h => !title.startsWith(h)).join(' › ');
  return section ? `${title} › ${section}` : title;
}

// Only http(s) links are rendered as anchors.
function safeHttpUrl(url) {
  return /^https?:\/\//i.test(url || '') ? url : '';
}

// ' source-cited' / ' source-uncited' from a source's cited flag; '' for old entries without one.
function citedClass(s) {
  return s.cited === true ? ' source-cited' : s.cited === false ? ' source-uncited' : '';
}

function renderSources(sources) {
  querySources.innerHTML = '';

  // Normalize: old flat-array format vs new {documents, web} format
  const docs = Array.isArray(sources) ? sources : (sources.documents || []);
  const web  = Array.isArray(sources) ? []      : (sources.web       || []);

  if (!docs.length && !web.length) {
    querySources.innerHTML = '<p class="muted">No sources returned.</p>';
    return;
  }

  // --- Document sources ---
  if (docs.length) {
    const header = document.createElement('p');
    header.className = 'sources-section-label';
    header.textContent = 'Documents';
    querySources.appendChild(header);

    // Passages the answer cited ([n]) come first; the rest are shown muted.
    // Sources without a `cited` flag (older responses) are shown as before.
    const ordered = [...docs].sort((a, b) => (b.cited === true) - (a.cited === true));
    ordered.forEach(s => {
      const div = document.createElement('div');
      div.className = 'source-item' + citedClass(s);
      const label = escHtml(sourceLabel(s));
      const url = safeHttpUrl(s.uri);
      div.innerHTML = `
        <div class="source-meta">
          ${s.n ? `<span class="source-number">[${s.n}]</span>` : ''}
          ${url
            ? `<a href="${escHtml(url)}" target="_blank" rel="noopener noreferrer" class="web-source-title">${label}</a>`
            : `<span>${label}</span>`}
          <span>Score: ${typeof s.score === 'number' ? s.score.toFixed(3) : '—'}</span>
        </div>
        ${url ? `<div class="source-url">${escHtml(url)}</div>` : ''}
        <div class="source-text">${escHtml(s.text || '')}</div>
      `;
      querySources.appendChild(div);
    });
  }

  // --- Web sources ---
  if (web.length) {
    const header = document.createElement('p');
    header.className = 'sources-section-label web-label';
    header.textContent = 'Web results';
    querySources.appendChild(header);

    // Same ordering as documents: cited results first, the rest muted
    const orderedWeb = [...web].sort((a, b) => (b.cited === true) - (a.cited === true));
    orderedWeb.forEach(r => {
      const div = document.createElement('div');
      div.className = 'source-item web-source-item' + citedClass(r);
      const url = safeHttpUrl(r.url);
      const title = escHtml(r.title || r.url || 'Web result');
      div.innerHTML = `
        <div class="source-meta">
          ${r.n ? `<span class="source-number">[${r.n}]</span>` : ''}
          ${url
            ? `<a href="${escHtml(url)}" target="_blank" rel="noopener noreferrer" class="web-source-title">${title}</a>`
            : `<span>${title}</span>`}
        </div>
        <div class="source-url">${escHtml(r.url || '')}</div>
        <div class="source-text">${escHtml(r.snippet || '')}</div>
      `;
      querySources.appendChild(div);
    });
  }
}

// =====================================================
// QUERY LOG
// =====================================================

// Logs are fetched 20 at a time, newest first. `logNextCursor` is the server's
// cursor for the next (older) page; the date filter is a local calendar day.
let logNextCursor = null;
let logShownCount = 0;

async function loadQueryLog(slug, { append = false } = {}) {
  const params = new URLSearchParams({ limit: '20' });
  if (append && logNextCursor) params.set('before', logNextCursor);
  if (logDateFilter.value) {
    const [y, m, d] = logDateFilter.value.split('-').map(Number);
    params.set('start', new Date(y, m - 1, d).toISOString());
    params.set('end', new Date(y, m - 1, d + 1).toISOString());
  }
  const res = await apiFetch(`/workspace/${slug}/logs?${params}`);
  if (!res.ok) return;
  const { entries, next } = await res.json();
  logNextCursor = next;
  renderQueryLog(entries, append);
}

function renderQueryLog(entries, append = false) {
  if (!append) {
    logEntries.innerHTML = '';
    logShownCount = 0;
  }
  logShownCount += entries.length;
  logMoreBtn.classList.toggle('hidden', !logNextCursor);
  logDateClear.classList.toggle('hidden', !logDateFilter.value);

  if (!logShownCount) {
    logEmpty.textContent = logDateFilter.value ? 'No queries logged on this date.' : 'No queries logged yet.';
    logEmpty.classList.remove('hidden');
    logCount.textContent = '';
    return;
  }

  logEmpty.classList.add('hidden');
  logCount.textContent = `Showing ${logShownCount} entr${logShownCount === 1 ? 'y' : 'ies'}`;

  entries.forEach(entry => {
    const card = document.createElement('div');
    card.className = 'log-entry';

    // A chat conversation is one entry with a list of turns; a single-shot
    // query is one entry that is itself the only turn.
    const turns = entry.turns || [entry];
    const first = turns[0] || {};

    // Format timestamp (latest activity)
    const lastTs = entry.updated_at || entry.timestamp;
    const tsStr = lastTs ? formatDateTime(lastTs) : '—';
    const durStr = entry.duration_ms != null ? `${(entry.duration_ms / 1000).toFixed(1)}s` : '';
    const turnsStr = entry.turns ? `${turns.length} turn${turns.length === 1 ? '' : 's'}` : '';

    // Header (always visible, clickable to expand)
    const header = document.createElement('div');
    header.className = 'log-entry-header';
    const rewroteNote = (!entry.turns && first.rewritten_query && first.rewritten_query !== first.question)
      ? `<div class="log-entry-rewritten-preview">&rarr; ${escHtml(first.rewritten_query)}</div>`
      : '';
    header.innerHTML = `
      <div class="log-entry-left">
        <div class="log-entry-question">${escHtml(first.question || '')}</div>
        ${rewroteNote}
      </div>
      <div class="log-entry-meta">
        <span class="log-entry-size">${escHtml(turnsStr || durStr)}</span>
        <span class="log-entry-time">${escHtml(tsStr)}</span>
      </div>
    `;

    // Body (hidden by default)
    const body = document.createElement('div');
    body.className = 'log-entry-body hidden';
    turns.forEach((turn, i) => {
      if (entry.turns) {
        const q = document.createElement('div');
        q.innerHTML = `<div class="log-section-label">Turn ${i + 1} &middot; Question</div>
          <div class="log-answer">${escHtml(turn.question || '')}</div>`;
        body.appendChild(q);
      }
      appendLogTurn(body, turn);
    });

    // Toggle expand/collapse on header click
    header.addEventListener('click', () => {
      body.classList.toggle('hidden');
    });

    card.appendChild(header);
    card.appendChild(body);
    logEntries.appendChild(card);
  });
}

// Rewritten query, answer and sources for one question/answer turn.
function appendLogTurn(body, turn) {
  // Rewritten query (show whenever present)
  if (turn.rewritten_query) {
    const rw = document.createElement('div');
    rw.innerHTML = `<div class="log-section-label">Rewritten query</div>
      <div class="log-rewritten">${escHtml(turn.rewritten_query)}</div>`;
    body.appendChild(rw);
  }

  // Answer
  const answerDiv = document.createElement('div');
  answerDiv.innerHTML = `<div class="log-section-label">Answer</div>
    <div class="log-answer">${escHtml(turn.answer || '')}</div>`;
  body.appendChild(answerDiv);

  // Document sources
  const docs = (turn.sources && turn.sources.documents) || [];
  if (docs.length) {
    const docsDiv = document.createElement('div');
    // Cited first, as in the Query tab, so each group gets one label
    const byCited = (a, b) => (b.cited === true) - (a.cited === true);
    const chips = [...docs].sort(byCited).map(d =>
      `<span class="log-source-chip${citedClass(d)}">${d.n ? `[${d.n}] ` : ''}${escHtml(sourceLabel(d))}${typeof d.score === 'number' ? ' · ' + d.score.toFixed(3) : ''}</span>`
    ).join('');
    docsDiv.innerHTML = `<div class="log-section-label">Document sources</div>
      <div class="log-source-chips">${chips}</div>`;
    body.appendChild(docsDiv);
  }

  // Web sources
  const web = (turn.sources && turn.sources.web) || [];
  if (web.length) {
    const webDiv = document.createElement('div');
    const links = [...web].sort((a, b) => (b.cited === true) - (a.cited === true)).map(r =>
      `<a class="log-web-link${citedClass(r)}" href="${escHtml(safeHttpUrl(r.url))}" target="_blank" rel="noopener noreferrer">${r.n ? `[${r.n}] ` : ''}${escHtml(r.title || r.url || 'Web result')}</a>`
    ).join('');
    webDiv.innerHTML = `<div class="log-section-label">Web sources</div>
      <div class="log-web-links">${links}</div>`;
    body.appendChild(webDiv);
  }
}

clearLogBtn.addEventListener('click', async () => {
  if (!currentWorkspaceSlug) return;
  if (!confirm('Clear all log entries for this workspace? This cannot be undone.')) return;
  const res = await apiFetch(`/workspace/${currentWorkspaceSlug}/logs`, { method: 'DELETE' });
  if (res.ok || res.status === 204) {
    logNextCursor = null;
    renderQueryLog([]);
  }
});

logDateFilter.addEventListener('change', () => {
  if (currentWorkspaceSlug) loadQueryLog(currentWorkspaceSlug);
});

logDateClear.addEventListener('click', () => {
  logDateFilter.value = '';
  if (currentWorkspaceSlug) loadQueryLog(currentWorkspaceSlug);
});

logMoreBtn.addEventListener('click', () => {
  if (currentWorkspaceSlug) loadQueryLog(currentWorkspaceSlug, { append: true });
});

// =====================================================
// GLOBAL SETTINGS
// =====================================================

async function loadGlobalSettings() {
  const [settingsRes, defaultsRes] = await Promise.all([
    apiFetch('/settings'),
    apiFetch('/defaults'),
  ]);
  if (!settingsRes.ok) return;
  const settings = await settingsRes.json();
  fillForm(globalSettingsForm, settings);
  loadSettingsModels(globalSettingsForm, globalSettingsMsg, settings);

  let defaults = null;
  if (defaultsRes.ok) {
    defaults = await defaultsRes.json();
    globalSettingsForm._promptDefaults = defaults;

    // Fill blank prompts with built-in defaults
    const spEl = globalSettingsForm.elements['system_prompt'];
    const webEnabled = globalSettingsForm.elements['searxng_enabled'].checked;
    if (spEl && !spEl.value) {
      spEl.value = webEnabled ? defaults.default_system_prompt_web : defaults.default_system_prompt_rag;
    }
    const rpEl = globalSettingsForm.elements['rewrite_prompt'];
    if (rpEl && !rpEl.value) rpEl.value = defaults.default_rewrite_prompt;
  }

  // Attach the searxng toggle → swap system prompt listener (idempotent via flag)
  if (!globalSettingsForm._toggleListenerAttached) {
    globalSettingsForm._toggleListenerAttached = true;
    globalSettingsForm.elements['searxng_enabled'].addEventListener('change', function () {
      const d = globalSettingsForm._promptDefaults;
      if (!d) return;
      const spEl = globalSettingsForm.elements['system_prompt'];
      if (!spEl) return;
      const cur = spEl.value;
      if (cur === d.default_system_prompt_rag || cur === d.default_system_prompt_web) {
        spEl.value = this.checked ? d.default_system_prompt_web : d.default_system_prompt_rag;
      }
    });
  }
}

globalSettingsForm.elements['api_key'].addEventListener('change', () => {
  loadSettingsModels(globalSettingsForm, globalSettingsMsg, currentModels(globalSettingsForm));
});

globalSettingsForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const data = formToObj(globalSettingsForm, [
    'llm_model', 'api_key', 'temperature', 'top_n',
    'similarity_threshold',
    'embed_model', 'embed_api_key', 'system_prompt',
    'searxng_num_results', 'searxng_query_suffix', 'rewrite_model', 'rewrite_prompt',
  ]);
  castNumbers(data, ['temperature', 'top_n', 'similarity_threshold', 'searxng_num_results']);
  data.searxng_enabled = globalSettingsForm.elements['searxng_enabled'].checked ? 1 : 0;

  showMsg(globalSettingsMsg, 'Saving...');
  const res = await apiFetch('/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });

  if (res.ok) {
    showMsg(globalSettingsMsg, 'Saved.', 'success');
  } else {
    const err = await res.json().catch(() => ({}));
    showMsg(globalSettingsMsg, `Error: ${err.detail || res.status}`, 'error');
  }
});

// =====================================================
// FORM UTILITIES
// =====================================================

function fillForm(form, obj) {
  Object.entries(obj).forEach(([key, val]) => {
    // API keys come back blank with a "<field>_hint" (e.g. "…a1b2"); a blank
    // field isn't sent on save, so the stored key is kept.
    if (key.endsWith('_hint')) {
      const target = form.elements[key.slice(0, -'_hint'.length)];
      if (target) target.placeholder = val ? `Saved key ${val} (leave blank to keep)` : '';
      return;
    }
    const el = form.elements[key];
    if (!el) return;
    if (el.type === 'checkbox') {
      el.checked = !!val;
    } else {
      el.value = val ?? '';
    }
  });
}

// Fields that should always be included even when empty (to allow clearing them
// back to the built-in default at runtime)
const _ALWAYS_INCLUDE = new Set(['rewrite_model', 'rewrite_prompt', 'system_prompt', 'searxng_query_suffix']);

function formToObj(form, fields) {
  const obj = {};
  fields.forEach(f => {
    const el = form.elements[f];
    if (!el) return;
    if (el.value !== '' || _ALWAYS_INCLUDE.has(f)) obj[f] = el.value;
  });
  return obj;
}

function castNumbers(obj, fields) {
  fields.forEach(f => {
    if (obj[f] !== undefined && obj[f] !== '') {
      const n = Number(obj[f]);
      if (!isNaN(n)) obj[f] = n;
    }
  });
}

function escHtml(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// =====================================================
// ACTIVITY (all workspaces, from the CloudWatch activity log)
// =====================================================

const activityFilters = document.getElementById('activity-filters');
const activityMsg     = document.getElementById('activity-msg');
const activityCounts  = document.getElementById('activity-counts');
const activityEvents  = document.getElementById('activity-events');
let activityWorkspaces = [];
let activityRequest = 0;

const EVENT_LABELS = {
  chat: 'Chat', query: 'Query', upload: 'Upload', delete_document: 'Document deleted',
  workspace_create: 'Workspace created', workspace_update: 'Workspace updated',
  workspace_delete: 'Workspace deleted', settings_update: 'Global settings updated',
};

function fillSelect(select, options, allLabel) {
  const current = select.value;
  select.innerHTML = `<option value="">${escHtml(allLabel)}</option>` +
    options.map(([value, label]) => `<option value="${escHtml(value)}">${escHtml(label)}</option>`).join('');
  // Keep the choice if it is still offered
  select.value = options.some(([v]) => v === current) ? current : '';
}

/** Only list workspaces under the chosen key prefix. */
function fillWorkspaceFilter() {
  const prefix = activityFilters.elements['key_prefix'].value;
  const list = activityWorkspaces.filter(w => !prefix || w.key_prefix === prefix);
  fillSelect(activityFilters.elements['workspace'], list.map(w => [w.slug, w.name]), 'All workspaces');
}

async function loadActivity() {
  const f = activityFilters.elements;
  const params = new URLSearchParams({ hours: f['hours'].value, limit: 200 });
  for (const name of ['key_prefix', 'workspace', 'event']) if (f[name].value) params.set(name, f[name].value);

  const request = ++activityRequest;
  activityMsg.textContent = 'Loading...';
  const res = await apiFetch(`/activity?${params}`);
  if (request !== activityRequest) return;  // a newer filter change won
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    activityMsg.textContent = err.detail || `Could not load activity (${res.status}).`;
    activityCounts.innerHTML = activityEvents.innerHTML = '';
    return;
  }
  const data = await res.json();
  activityWorkspaces = data.workspaces;
  fillSelect(f['key_prefix'], data.key_prefixes.map(p => [p, p]), 'All key prefixes');
  fillWorkspaceFilter();
  renderActivityCounts(data.counts);
  renderActivityEvents(data.events);
  activityMsg.textContent = data.events.length >= 200 ? 'Showing the newest 200 events.' : '';
}

function renderActivityCounts(counts) {
  // One row per key prefix + workspace; events folded into columns
  const rows = new Map();
  for (const c of counts) {
    const key = `${c.key_prefix || ''}|${c.workspace || ''}`;
    if (!rows.has(key)) rows.set(key, { key_prefix: c.key_prefix, workspace: c.workspace, name: c.workspace_name,
                                        chat: 0, query: 0, upload: 0, other: 0, total: 0 });
    const r = rows.get(key);
    const n = Number(c.count) || 0;
    const col = ['chat', 'query', 'upload'].includes(c.event) ? c.event : 'other';
    r[col] += n;
    r.total += n;
  }
  const sorted = [...rows.values()].sort((a, b) => b.total - a.total);
  activityCounts.innerHTML = sorted.length ? sorted.map(r => `<tr>
      <td>${escHtml(r.key_prefix || '—')}</td>
      <td>${escHtml(r.name || r.workspace || '—')}</td>
      <td class="num">${r.chat}</td><td class="num">${r.query}</td><td class="num">${r.upload}</td>
      <td class="num">${r.other}</td><td class="num"><strong>${r.total}</strong></td>
    </tr>`).join('') : '<tr><td colspan="7" class="muted">No activity in this range.</td></tr>';
}

function activityDetails(e) {
  const parts = [];
  if (e.filename) parts.push(e.filename);
  if (e.chunks) parts.push(`${e.chunks} chunk${e.chunks == 1 ? '' : 's'}`);
  if (e.duration_ms) parts.push(`${(Number(e.duration_ms) / 1000).toFixed(1)} s`);
  if (e.has_attachment) parts.push('with attachment');
  if (e.changed) parts.push(`changed: ${String(e.changed).replace(/[\[\]"]/g, '')}`);
  return parts.join(' · ');
}

function renderActivityEvents(events) {
  activityEvents.innerHTML = events.length ? events.map(e => `<tr>
      <td>${escHtml(formatDateTime(e['@timestamp'].replace(' ', 'T') + 'Z'))}</td>
      <td>${escHtml(EVENT_LABELS[e.event] || e.event)}</td>
      <td title="${escHtml(e.workspace || '')}">${escHtml(e.workspace_name || e.workspace || '—')}</td>
      <td>${escHtml(e.key_prefix || '—')}</td>
      <td>${escHtml(e.caller || '')}</td>
      <td>${escHtml(e.source || '')}</td>
      <td>${escHtml(activityDetails(e))}</td>
    </tr>`).join('') : '<tr><td colspan="7" class="muted">No events in this range.</td></tr>';
}

activityFilters.addEventListener('submit', (e) => { e.preventDefault(); loadActivity(); });
activityFilters.addEventListener('change', (e) => {
  if (e.target.name === 'key_prefix') fillWorkspaceFilter();
  loadActivity();
});

// =====================================================
// INIT
// =====================================================

(async function init() {
  // Older versions kept the admin key itself in sessionStorage; don't leave it behind
  try { sessionStorage.removeItem('infochat_api_key'); } catch {}
  if (await checkSession()) {
    loggedIn = true;
    showApp();
    return;
  }
  showLogin();
})();
