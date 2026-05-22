/* =====================================================
   RhodyRAG Admin — app.js
   All requests go to the same origin (FastAPI on :3001).
   ===================================================== */

// Use same origin so this works regardless of host/port
const API_BASE = '';

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

const globalSettingsForm = document.getElementById('global-settings-form');
const globalSettingsMsg  = document.getElementById('global-settings-msg');

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
  const stored = localStorage.getItem('rhodyrag_api_key');
  if (stored) {
    apiKey = stored;
    return true;
  }
  return false;
}

function saveApiKey(key) {
  apiKey = key;
  localStorage.setItem('rhodyrag_api_key', key);
}

function clearApiKey() {
  apiKey = '';
  localStorage.removeItem('rhodyrag_api_key');
}

async function validateKey(key) {
  try {
    const res = await fetch(API_BASE + '/settings', {
      headers: { 'X-API-Key': key },
    });
    return res.status !== 403;
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
  showView('workspaces');
  loadWorkspaces();
}

// =====================================================
// NAVIGATION
// =====================================================

function showView(name) {
  viewWorkspaces.classList.add('hidden');
  viewSettings.classList.add('hidden');
  viewWorkspaceDetail.classList.add('hidden');

  navBtns.forEach(b => b.classList.toggle('active', b.dataset.view === name));

  if (name === 'workspaces') {
    viewWorkspaces.classList.remove('hidden');
  } else if (name === 'settings') {
    viewSettings.classList.remove('hidden');
    loadGlobalSettings();
  } else if (name === 'workspace-detail') {
    viewWorkspaceDetail.classList.remove('hidden');
  }
}

navBtns.forEach(btn => {
  btn.addEventListener('click', () => showView(btn.dataset.view));
});

// =====================================================
// TABS (workspace detail)
// =====================================================

tabBtns.forEach(btn => {
  btn.addEventListener('click', () => {
    tabBtns.forEach(b => b.classList.remove('active'));
    tabPanels.forEach(p => p.classList.add('hidden'));
    btn.classList.add('active');
    document.getElementById(btn.dataset.tab).classList.remove('hidden');
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

function openWorkspace(slug, name) {
  currentWorkspaceSlug = slug;
  currentWorkspaceName = name;
  workspaceDetailName.textContent = name;

  // Reset tabs to first
  tabBtns.forEach(b => b.classList.remove('active'));
  tabPanels.forEach(p => p.classList.add('hidden'));
  tabBtns[0].classList.add('active');
  document.getElementById('ws-settings').classList.remove('hidden');

  // Clear query result
  queryAnswer.textContent = '';
  querySources.innerHTML = '';
  queryResult.classList.add('hidden');
  queryInput.value = '';

  showView('workspace-detail');
  loadWorkspaceSettings(slug);
  loadDocList(slug);
}

backToWorkspaces.addEventListener('click', () => {
  currentWorkspaceSlug = null;
  showView('workspaces');
  loadWorkspaces();
});

// =====================================================
// WORKSPACE SETTINGS
// =====================================================

async function loadWorkspaceSettings(slug) {
  const res = await apiFetch(`/workspace/${slug}`);
  if (!res.ok) return;
  const ws = await res.json();
  fillForm(workspaceSettingsForm, ws);
}

workspaceSettingsForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const data = formToObj(workspaceSettingsForm, [
    'llm_model', 'api_key', 'temperature', 'top_n',
    'similarity_threshold', 'system_prompt', 'embed_api_key',
  ]);
  castNumbers(data, ['temperature', 'top_n', 'similarity_threshold']);

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

  // Pre-fill with current global defaults so the admin sees what will be used
  const res = await apiFetch('/settings');
  if (res.ok) {
    const settings = await res.json();
    fillForm(createWorkspaceForm, settings);
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
    'embed_model', 'embed_api_key', 'system_prompt',
  ]);
  castNumbers(data, ['temperature', 'top_n', 'similarity_threshold', 'chunk_size', 'chunk_overlap']);

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
      } else if (eventName === 'sources') {
        try { renderSources(JSON.parse(data)); } catch {}
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
  querySubmitBtn.disabled = false;
  querySubmitBtn.textContent = 'Ask';
});

function renderSources(sources) {
  querySources.innerHTML = '';
  if (!sources || !sources.length) {
    querySources.innerHTML = '<p class="muted">No sources returned.</p>';
    return;
  }
  sources.forEach(s => {
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

// =====================================================
// GLOBAL SETTINGS
// =====================================================

async function loadGlobalSettings() {
  const res = await apiFetch('/settings');
  if (!res.ok) return;
  const settings = await res.json();
  fillForm(globalSettingsForm, settings);
}

globalSettingsForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const data = formToObj(globalSettingsForm, [
    'llm_model', 'api_key', 'temperature', 'top_n',
    'similarity_threshold', 'chunk_size', 'chunk_overlap',
    'embed_model', 'embed_api_key', 'system_prompt',
  ]);
  castNumbers(data, ['temperature', 'top_n', 'similarity_threshold', 'chunk_size', 'chunk_overlap']);

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
    if (el) el.value = val ?? '';
  });
}

function formToObj(form, fields) {
  const obj = {};
  fields.forEach(f => {
    const el = form.elements[f];
    if (el && el.value !== '') obj[f] = el.value;
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
