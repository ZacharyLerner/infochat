/* =====================================================
   RhodyRAG Admin — app.js
   All requests go to the same origin (FastAPI on :3001).
   ===================================================== */

// Use same origin so this works regardless of host/port
const API_BASE = '/api';

// =====================================================
// STATE
// =====================================================
let apiKey = '';
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
      'X-API-Key': apiKey,
      ...(options.headers || {}),
    },
  });
  return res;
}

// =====================================================
// AUTH
// =====================================================

function loadApiKey() {
  const stored = sessionStorage.getItem('rhodyrag_api_key');
  if (stored) {
    apiKey = stored;
    return true;
  }
  return false;
}

function saveApiKey(key) {
  apiKey = key;
  sessionStorage.setItem('rhodyrag_api_key', key);
}

function clearApiKey() {
  apiKey = '';
  sessionStorage.removeItem('rhodyrag_api_key');
}

async function validateKey(key) {
  try {
    const res = await fetch('/api/settings', {
      headers: { 'Accept': 'application/json', 'X-API-Key': key },
    });
    if (res.status === 403) return false;
    const ct = res.headers.get('content-type') || '';
    return res.ok && ct.includes('application/json');
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

  const valid = await validateKey(key);
  if (valid) {
    saveApiKey(key);
    showApp();
  } else {
    loginError.textContent = 'Invalid API key or server unreachable.';
    loginError.style.color = '#c0392b';
  }
});

logoutBtn.addEventListener('click', () => {
  clearApiKey();
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
    history.replaceState(null, '', path);
  } else {
    history.pushState(null, '', path);
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
  const path = window.location.pathname;

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
  viewWorkspaceDetail.classList.add('hidden');

  navBtns.forEach(b => b.classList.toggle('active', b.dataset.view === name));

  if (name === 'workspaces') {
    viewWorkspaces.classList.remove('hidden');
  } else if (name === 'settings') {
    viewSettings.classList.remove('hidden');
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
  if (apiKey) router();
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
    const currentIsWorkspace = /^\/workspace\//.test(window.location.pathname);
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
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><strong>${escHtml(ws.name)}</strong></td>
      <td><code>${escHtml(ws.slug)}</code></td>
      <td>${escHtml(ws.llm_model || '—')}</td>
      <td>
        <button class="action-link open-ws" data-slug="${escHtml(ws.slug)}" data-name="${escHtml(ws.name)}">Open</button>
        <span class="action-sep"> · </span>
        <button class="action-link danger delete-ws" data-slug="${escHtml(ws.slug)}" data-name="${escHtml(ws.name)}">Delete</button>
      </td>
    `;
    workspaceTableBody.appendChild(tr);
  });

  document.querySelectorAll('.open-ws').forEach(btn => {
    btn.addEventListener('click', () => openWorkspace(btn.dataset.slug, btn.dataset.name));
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
  fillForm(workspaceSettingsForm, ws);

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

workspaceSettingsForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const data = formToObj(workspaceSettingsForm, [
    'llm_model', 'api_key', 'temperature', 'top_n',
    'similarity_threshold', 'system_prompt', 'embed_api_key', 'max_tokens',
    'searxng_num_results', 'searxng_query_suffix', 'rewrite_model', 'rewrite_prompt',
  ]);
  castNumbers(data, ['temperature', 'top_n', 'similarity_threshold', 'max_tokens', 'searxng_num_results']);
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

  // Fetch global settings and built-in defaults in parallel
  const [settingsRes, defaultsRes] = await Promise.all([
    apiFetch('/settings'),
    apiFetch('/defaults'),
  ]);

  let defaults = null;
  if (defaultsRes.ok) {
    defaults = await defaultsRes.json();
    createWorkspaceForm._promptDefaults = defaults;
  }

  if (settingsRes.ok) {
    const settings = await settingsRes.json();
    fillForm(createWorkspaceForm, settings);

    // Fill blank prompts with built-in defaults
    if (defaults) {
      const spEl = createWorkspaceForm.elements['system_prompt'];
      const webEnabled = createWorkspaceForm.elements['searxng_enabled'].checked;
      if (spEl && !spEl.value) {
        spEl.value = webEnabled ? defaults.default_system_prompt_web : defaults.default_system_prompt_rag;
      }
      const rpEl = createWorkspaceForm.elements['rewrite_prompt'];
      if (rpEl && !rpEl.value) rpEl.value = defaults.default_rewrite_prompt;
    }
  }

  // Attach the searxng toggle → swap system prompt listener (idempotent via flag)
  if (!createWorkspaceForm._toggleListenerAttached) {
    createWorkspaceForm._toggleListenerAttached = true;
    createWorkspaceForm.elements['searxng_enabled'].addEventListener('change', function () {
      const d = createWorkspaceForm._promptDefaults;
      if (!d) return;
      const spEl = createWorkspaceForm.elements['system_prompt'];
      if (!spEl) return;
      const cur = spEl.value;
      if (cur === d.default_system_prompt_rag || cur === d.default_system_prompt_web) {
        spEl.value = this.checked ? d.default_system_prompt_web : d.default_system_prompt_rag;
      }
    });
  }
});

closeModalBtn.addEventListener('click', () => {
  createModal.classList.add('hidden');
});

createModal.addEventListener('click', (e) => {
  if (e.target === createModal) createModal.classList.add('hidden');
});

createWorkspaceForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const data = formToObj(createWorkspaceForm, [
    'name', 'llm_model', 'api_key', 'temperature', 'top_n',
    'similarity_threshold', 'chunk_size', 'chunk_overlap',
    'embed_model', 'embed_api_key', 'system_prompt', 'max_tokens',
    'searxng_num_results', 'searxng_query_suffix', 'rewrite_model', 'rewrite_prompt',
  ]);
  castNumbers(data, ['temperature', 'top_n', 'similarity_threshold', 'chunk_size', 'chunk_overlap', 'max_tokens', 'searxng_num_results']);
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
    const date = doc.uploaded_at ? new Date(doc.uploaded_at).toLocaleString() : '—';
    tr.innerHTML = `
      <td>${escHtml(doc.filename)}</td>
      <td><code>${escHtml(doc.doc_id)}</code></td>
      <td>${doc.chunks_embedded ?? '—'}</td>
      <td>${date}</td>
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

    docs.forEach(s => {
      const div = document.createElement('div');
      div.className = 'source-item';
      div.innerHTML = `
        <div class="source-meta">
          <span>${escHtml(s.filename || 'Unknown')}</span>
          <span>Score: ${typeof s.score === 'number' ? s.score.toFixed(3) : '—'}</span>
        </div>
        <div class="source-text">${escHtml(s.text || '')}</div>
      `;
      querySources.appendChild(div);
    });
  }

  // --- Web sources ---
  if (web.length) {
    const header = document.createElement('p');
    header.className = 'sources-section-label web-label';
    header.textContent = 'Web Results';
    querySources.appendChild(header);

    web.forEach(r => {
      const div = document.createElement('div');
      div.className = 'source-item web-source-item';
      div.innerHTML = `
        <div class="source-meta">
          <a href="${escHtml(r.url || '')}" target="_blank" rel="noopener noreferrer" class="web-source-title">${escHtml(r.title || r.url || 'Web result')}</a>
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

async function loadQueryLog(slug) {
  const res = await apiFetch(`/workspace/${slug}/logs`);
  if (!res.ok) return;
  const entries = await res.json();
  renderQueryLog(entries);
}

function renderQueryLog(entries) {
  logEntries.innerHTML = '';

  if (!entries.length) {
    logEmpty.classList.remove('hidden');
    logCount.textContent = '';
    return;
  }

  logEmpty.classList.add('hidden');
  logCount.textContent = `${entries.length} entr${entries.length === 1 ? 'y' : 'ies'}`;

  entries.forEach(entry => {
    const card = document.createElement('div');
    card.className = 'log-entry';

    // Format timestamp
    const ts = entry.timestamp ? new Date(entry.timestamp) : null;
    const tsStr = ts ? ts.toLocaleString() : '—';
    const durStr = entry.duration_ms != null ? `${(entry.duration_ms / 1000).toFixed(1)}s` : '';

    // Header (always visible, clickable to expand)
    const header = document.createElement('div');
    header.className = 'log-entry-header';
    const rewroteNote = (entry.rewritten_query && entry.rewritten_query !== entry.question)
      ? `<div class="log-entry-rewritten-preview">&rarr; ${escHtml(entry.rewritten_query)}</div>`
      : '';
    header.innerHTML = `
      <div class="log-entry-left">
        <div class="log-entry-question">${escHtml(entry.question || '')}</div>
        ${rewroteNote}
      </div>
      <div class="log-entry-meta">
        <span>${escHtml(tsStr)}</span>
        ${durStr ? `<span class="log-duration-badge">${escHtml(durStr)}</span>` : ''}
      </div>
    `;

    // Body (hidden by default)
    const body = document.createElement('div');
    body.className = 'log-entry-body hidden';

    // Rewritten query (show whenever present)
    if (entry.rewritten_query) {
      const rw = document.createElement('div');
      rw.innerHTML = `<div class="log-section-label">Rewritten Query</div>
        <div class="log-rewritten">${escHtml(entry.rewritten_query)}</div>`;
      body.appendChild(rw);
    }

    // Answer
    const answerDiv = document.createElement('div');
    answerDiv.innerHTML = `<div class="log-section-label">Answer</div>
      <div class="log-answer">${escHtml(entry.answer || '')}</div>`;
    body.appendChild(answerDiv);

    // Document sources
    const docs = (entry.sources && entry.sources.documents) || [];
    if (docs.length) {
      const docsDiv = document.createElement('div');
      const chips = docs.map(d =>
        `<span class="log-source-chip">${escHtml(d.filename || 'Unknown')}${typeof d.score === 'number' ? ' · ' + d.score.toFixed(3) : ''}</span>`
      ).join('');
      docsDiv.innerHTML = `<div class="log-section-label">Document Sources</div>
        <div class="log-source-chips">${chips}</div>`;
      body.appendChild(docsDiv);
    }

    // Web sources
    const web = (entry.sources && entry.sources.web) || [];
    if (web.length) {
      const webDiv = document.createElement('div');
      const links = web.map(r =>
        `<a class="log-web-link" href="${escHtml(r.url || '')}" target="_blank" rel="noopener noreferrer">${escHtml(r.title || r.url || 'Web result')}</a>`
      ).join('');
      webDiv.innerHTML = `<div class="log-section-label">Web Sources</div>
        <div class="log-web-links">${links}</div>`;
      body.appendChild(webDiv);
    }

    // Toggle expand/collapse on header click
    header.addEventListener('click', () => {
      body.classList.toggle('hidden');
    });

    card.appendChild(header);
    card.appendChild(body);
    logEntries.appendChild(card);
  });
}

clearLogBtn.addEventListener('click', async () => {
  if (!currentWorkspaceSlug) return;
  if (!confirm('Clear all log entries for this workspace? This cannot be undone.')) return;
  const res = await apiFetch(`/workspace/${currentWorkspaceSlug}/logs`, { method: 'DELETE' });
  if (res.ok || res.status === 204) {
    renderQueryLog([]);
  }
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

globalSettingsForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const data = formToObj(globalSettingsForm, [
    'llm_model', 'api_key', 'temperature', 'top_n',
    'similarity_threshold', 'chunk_size', 'chunk_overlap',
    'embed_model', 'embed_api_key', 'system_prompt', 'max_tokens',
    'searxng_num_results', 'searxng_query_suffix', 'rewrite_model', 'rewrite_prompt',
  ]);
  castNumbers(data, ['temperature', 'top_n', 'similarity_threshold', 'chunk_size', 'chunk_overlap', 'max_tokens', 'searxng_num_results']);
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
// INIT
// =====================================================

(async function init() {
  if (loadApiKey()) {
    const valid = await validateKey(apiKey);
    if (valid) {
      showApp();
      return;
    } else {
      clearApiKey();
    }
  }
  showLogin();
})();
