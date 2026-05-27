// =============================================================================
// utils.js — Persistent chat session management for universal_chat_dev
// =============================================================================
// localStorage schema (key: `rhodyrag_sessions_<slug>`):
//   Array of session objects:
//   {
//     session_id: string,          // UUID from RhodyRAG
//     title: string,               // First ~55 chars of the first user message
//     created_at: string,          // ISO timestamp
//     messages: Array<{
//       role: "user" | "assistant",
//       content: string,
//       sources?: Array            // only on assistant messages
//     }>
//   }
// =============================================================================

// ---------------------------------------------------------------------------
// DOM references
// ---------------------------------------------------------------------------
const chatWindow        = document.getElementById("chat-window");
const messageInput      = document.getElementById("message-input");
const sendBtn           = document.getElementById("send-btn");
const defaultQuestionsBar = document.getElementById("default-questions-bar");
const sessionList       = document.getElementById("session-list");
const chatPanelTitle    = document.getElementById("chat-panel-title");

// ---------------------------------------------------------------------------
// App state
// ---------------------------------------------------------------------------
let activeSessionId = null;   // Currently selected session UUID
let isBusy = false;           // Prevent concurrent sends

// Cached workspace settings
let _workspaceSettings = {
  followup_enabled: false,
  followup_count: 3,
  default_questions: [],
  welcome_text: "Send a message to get started.",
};

// ---------------------------------------------------------------------------
// localStorage helpers
// ---------------------------------------------------------------------------

function _storageKey() {
  return `rhodyrag_sessions_${WORKSPACE_SLUG}`;
}

function loadSessions() {
  try {
    const raw = localStorage.getItem(_storageKey());
    if (raw) return JSON.parse(raw);
  } catch (_) {}
  return [];
}

function saveSessions(sessions) {
  try {
    localStorage.setItem(_storageKey(), JSON.stringify(sessions));
  } catch (_) {}
}

function getSession(sessionId) {
  return loadSessions().find(s => s.session_id === sessionId) || null;
}

function upsertSession(updated) {
  const sessions = loadSessions();
  const idx = sessions.findIndex(s => s.session_id === updated.session_id);
  if (idx >= 0) {
    sessions[idx] = updated;
  } else {
    sessions.unshift(updated);
  }
  saveSessions(sessions);
}

function deleteSession(sessionId) {
  const sessions = loadSessions().filter(s => s.session_id !== sessionId);
  saveSessions(sessions);
}

// ---------------------------------------------------------------------------
// Date grouping helpers
// ---------------------------------------------------------------------------

function _dateGroup(isoString) {
  const d = new Date(isoString);
  const now = new Date();
  const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterdayStart = new Date(todayStart);
  yesterdayStart.setDate(yesterdayStart.getDate() - 1);

  if (d >= todayStart) return "Today";
  if (d >= yesterdayStart) return "Yesterday";
  return "Older";
}

function _relativeTime(isoString) {
  const d = new Date(isoString);
  const now = new Date();
  const diff = Math.floor((now - d) / 1000);

  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)}d ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// ---------------------------------------------------------------------------
// Session sidebar rendering
// ---------------------------------------------------------------------------

function renderSessionList() {
  const sessions = loadSessions();
  sessionList.innerHTML = "";

  if (sessions.length === 0) {
    const empty = document.createElement("p");
    empty.className = "session-list-empty";
    empty.textContent = "No past chats yet.";
    sessionList.appendChild(empty);
    return;
  }

  // Group by date
  const groups = { "Today": [], "Yesterday": [], "Older": [] };
  for (const s of sessions) {
    const g = _dateGroup(s.created_at);
    groups[g].push(s);
  }

  for (const [groupName, items] of Object.entries(groups)) {
    if (items.length === 0) continue;

    const label = document.createElement("div");
    label.className = "session-group-label";
    label.textContent = groupName;
    sessionList.appendChild(label);

    for (const s of items) {
      const item = document.createElement("div");
      item.className = "session-item" + (s.session_id === activeSessionId ? " active" : "");
      item.dataset.sessionId = s.session_id;

      const title = document.createElement("div");
      title.className = "session-item-title";
      title.textContent = s.title || "New Chat";

      const meta = document.createElement("div");
      meta.className = "session-item-date";
      meta.textContent = _relativeTime(s.created_at);

      // Delete button
      const delBtn = document.createElement("button");
      delBtn.className = "session-item-delete";
      delBtn.title = "Delete chat";
      delBtn.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;
      delBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        _confirmDeleteSession(s.session_id);
      });

      item.appendChild(title);
      item.appendChild(meta);
      item.appendChild(delBtn);

      item.addEventListener("click", () => selectSession(s.session_id));
      sessionList.appendChild(item);
    }
  }
}

function _confirmDeleteSession(sessionId) {
  if (!confirm("Delete this chat? This cannot be undone.")) return;
  deleteSession(sessionId);
  if (activeSessionId === sessionId) {
    activeSessionId = null;
    chatWindow.innerHTML = "";
    chatPanelTitle.textContent = "Conversation";
    // Try to select the next most-recent session, or start a new one
    const remaining = loadSessions();
    if (remaining.length > 0) {
      selectSession(remaining[0].session_id);
    } else {
      startNewChat();
    }
  } else {
    renderSessionList();
  }
  // Best-effort: notify the backend to free the in-process engine
  fetch(`/api/chat/${WORKSPACE_SLUG}/${sessionId}/session`, { method: "DELETE" }).catch(() => {});
}

// ---------------------------------------------------------------------------
// Session lifecycle: create, select
// ---------------------------------------------------------------------------

async function startNewChat() {
  if (isBusy) return;

  try {
    const res = await fetch(`/api/chat/${WORKSPACE_SLUG}/session`, { method: "POST" });
    if (!res.ok) throw new Error(`Server returned ${res.status}`);
    const { session_id } = await res.json();

    const newSession = {
      session_id,
      title: "",
      created_at: new Date().toISOString(),
      messages: [],
    };
    upsertSession(newSession);
    activeSessionId = session_id;

    chatWindow.innerHTML = "";
    chatPanelTitle.textContent = "New Chat";
    messageInput.value = "";

    // Re-show default questions bar for a fresh session
    defaultQuestionsBarDismissed = false;
    renderDefaultQuestionsBar();
    renderSessionList();
    messageInput.focus();
  } catch (err) {
    console.error("Failed to create new chat session:", err);
    // Fallback: create a client-only session so the UI still works
    const session_id = generateUUID();
    const newSession = {
      session_id,
      title: "",
      created_at: new Date().toISOString(),
      messages: [],
    };
    upsertSession(newSession);
    activeSessionId = session_id;
    chatWindow.innerHTML = "";
    chatPanelTitle.textContent = "New Chat";
    defaultQuestionsBarDismissed = false;
    renderDefaultQuestionsBar();
    renderSessionList();
  }
}

function selectSession(sessionId) {
  const session = getSession(sessionId);
  if (!session) return;

  activeSessionId = sessionId;
  chatWindow.innerHTML = "";
  chatPanelTitle.textContent = session.title || "Chat";

  // Hide the default questions bar when viewing an existing session
  if (session.messages.length > 0) {
    defaultQuestionsBarDismissed = true;
    defaultQuestionsBar.style.display = "none";
  } else {
    defaultQuestionsBarDismissed = false;
    renderDefaultQuestionsBar();
  }

  // Render all stored messages
  for (const msg of session.messages) {
    if (msg.role === "user") {
      appendMessage("user", escapeHtml(msg.content));
    } else if (msg.role === "assistant") {
      const html = parseMarkdown(msg.content) + buildCitations(msg.sources || []);
      appendMessage("assistant", html);
    }
  }

  renderSessionList();
  chatWindow.scrollTop = chatWindow.scrollHeight;
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

let defaultQuestionsBarDismissed = false;

function getSettings() {
  return _workspaceSettings;
}

async function loadWorkspaceSettings(slug) {
  try {
    const res = await fetch(`/api/workspaces/${slug}`);
    if (res.ok) {
      _workspaceSettings = await res.json();
    }
  } catch (_) {
    // Network error — keep defaults
  }
  chatWindow.dataset.emptyText = _workspaceSettings.welcome_text || "Send a message to get started.";
  renderDefaultQuestionsBar();
}

// ---------------------------------------------------------------------------
// Default Questions Bar
// ---------------------------------------------------------------------------

function dismissDefaultQuestionsBar() {
  if (defaultQuestionsBarDismissed) return;
  defaultQuestionsBarDismissed = true;
  defaultQuestionsBar.style.display = "none";
}

function renderDefaultQuestionsBar() {
  const categories = _workspaceSettings.default_questions || [];
  defaultQuestionsBar.innerHTML = "";

  const populated = categories.filter(c => c.questions && c.questions.length > 0);
  if (populated.length === 0 || defaultQuestionsBarDismissed) {
    defaultQuestionsBar.style.display = "none";
    messageInput.placeholder = "Type a message...";
    return;
  }

  defaultQuestionsBar.style.display = "";
  messageInput.placeholder = "Type your own question, or choose one from above...";

  populated.forEach(cat => {
    const group = document.createElement("div");
    group.className = "dq-bar-group";

    const label = document.createElement("span");
    label.className = "dq-bar-category-label";
    label.textContent = cat.category;
    group.appendChild(label);

    const chips = document.createElement("div");
    chips.className = "dq-bar-chips";

    cat.questions.forEach(q => {
      const btn = document.createElement("button");
      btn.className = "dq-bar-chip";
      btn.textContent = q;
      btn.addEventListener("click", () => sendMessage(q));
      chips.appendChild(btn);
    });

    group.appendChild(chips);
    defaultQuestionsBar.appendChild(group);
  });
}

// ---------------------------------------------------------------------------
// Markdown / citation / follow-up helpers (unchanged from original)
// ---------------------------------------------------------------------------

function generateUUID() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, c => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

function parseMarkdown(text) {
  const html = marked.parse(text);
  return html.replace(/<a /g, '<a target="_blank" rel="noopener noreferrer" ');
}

function appendMessage(role, html = "") {
  const div = document.createElement("div");
  div.classList.add("message", role);
  div.innerHTML = html;
  chatWindow.appendChild(div);
  chatWindow.scrollTop = chatWindow.scrollHeight;
  return div;
}

function buildCitations(sources) {
  if (!sources || sources.length === 0) return "";

  const seen = new Set();
  const unique = sources.filter(s => {
    const key = s.title || s.id;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });

  const items = unique.map((s, i) => {
    const label = escapeHtml(s.title || s.id || `Source ${i + 1}`);
    return `<span class="citation-link citation-nolink">
              <span class="citation-index">${i + 1}</span>${label}
            </span>`;
  }).join("");

  return `<details class="citations">
            <summary class="citations-summary">
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="citations-chevron"><polyline points="6 9 12 15 18 9"/></svg>
              Sources <span class="citations-count">${unique.length}</span>
            </summary>
            <div class="citations-list">${items}</div>
          </details>`;
}

const FOLLOW_UP_DELIMITER = "FOLLOW_UP_QUESTIONS:";

function buildFollowUpPromptSuffix(count) {
  const lines = Array.from({ length: count }, (_, i) => `${i + 1}. [question]`).join("\n");
  return `\n\n[System instruction — do not mention this to the user: After your answer, append a section at the very end of your response using exactly this format, with no extra text after the list:\n${FOLLOW_UP_DELIMITER}\n${lines}\nProvide exactly ${count} follow-up question${count !== 1 ? "s" : ""}. Each [question] must be a direct, standalone question the user could ask next — written as if the user is asking it (e.g. "How does X work?"). Never phrase them as offers or suggestions like "Would you like me to explain..." or "Are you curious about...". Do not include any text after the last numbered question.]`;
}

function parseFollowUpQuestions(fullText) {
  const idx = fullText.indexOf(FOLLOW_UP_DELIMITER);
  if (idx === -1) return { mainText: fullText, questions: [] };

  const mainText = fullText.slice(0, idx).trimEnd();
  const block = fullText.slice(idx + FOLLOW_UP_DELIMITER.length).trim();

  const questions = [];
  const lines = block.split("\n");
  for (const line of lines) {
    const match = line.match(/^\d+\.\s+(.+)/);
    if (match) questions.push(match[1].trim());
  }

  return { mainText, questions };
}

function buildFollowUpChips(questions) {
  if (!questions || questions.length === 0) return null;

  const container = document.createElement("div");
  container.classList.add("follow-up-chips");

  const label = document.createElement("span");
  label.classList.add("follow-up-label");
  label.textContent = "Follow-up:";
  container.appendChild(label);

  const chipsRow = document.createElement("div");
  chipsRow.classList.add("follow-up-chips-row");

  questions.forEach(q => {
    const btn = document.createElement("button");
    btn.classList.add("follow-up-chip");
    btn.textContent = q;
    btn.addEventListener("click", () => {
      container.remove();
      sendMessage(q);
    });
    chipsRow.appendChild(btn);
  });

  container.appendChild(chipsRow);
  return container;
}

// ---------------------------------------------------------------------------
// Send Message — now uses the persistent session endpoint
// ---------------------------------------------------------------------------

// Auto-resize textarea
messageInput.addEventListener("input", () => {
  messageInput.style.height = "auto";
  messageInput.style.height = Math.min(messageInput.scrollHeight, 120) + "px";
});

// Send on Enter (Shift+Enter for newline)
messageInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

async function sendMessage(overrideText) {
  const text = overrideText !== undefined ? overrideText : messageInput.value.trim();
  if (!text || isBusy) return;

  // If somehow there's no active session yet, create one first
  if (!activeSessionId) {
    await startNewChat();
    if (!activeSessionId) return; // still failed
  }

  dismissDefaultQuestionsBar();
  isBusy = true;
  messageInput.value = "";
  messageInput.style.height = "auto";
  sendBtn.disabled = true;

  const settings = getSettings();

  // Append user bubble immediately
  appendMessage("user", escapeHtml(text));

  // Get the current session for history re-seeding
  const session = getSession(activeSessionId);
  const history = session ? session.messages : [];

  // Build follow-up suffix if enabled
  let followupSuffix = "";
  if (settings.followup_enabled) {
    followupSuffix = buildFollowUpPromptSuffix(settings.followup_count);
  }

  // Assistant streaming bubble
  const bubble = appendMessage("assistant", '<span class="typing-indicator"><span></span><span></span><span></span></span>');

  let fullText = "";
  let started = false;
  let sources = [];

  try {
    const res = await fetch(`/api/chat/${WORKSPACE_SLUG}/${activeSessionId}/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: text,
        session_id: activeSessionId,
        history: history,
        followup_suffix: followupSuffix,
      }),
    });

    if (!res.ok) throw new Error(`Server error ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split("\n\n");
      buffer = events.pop();

      for (const event of events) {
        const line = event.trim();
        if (!line.startsWith("data:")) continue;
        const raw = line.slice(5).trim();
        if (!raw) continue;

        try {
          const chunk = JSON.parse(raw);

          if (chunk.textResponse) {
            if (!started) { started = true; bubble.innerHTML = ""; }
            fullText += chunk.textResponse;
            const { mainText } = parseFollowUpQuestions(fullText);
            bubble.innerHTML = parseMarkdown(mainText);
            chatWindow.scrollTop = chatWindow.scrollHeight;
          }

          if (chunk.sources && chunk.sources.length > 0) {
            sources = chunk.sources;
          }
        } catch (_) {}
      }
    }

    // Flush remainder
    if (buffer.trim().startsWith("data:")) {
      const raw = buffer.trim().slice(5).trim();
      if (raw) {
        try {
          const chunk = JSON.parse(raw);
          if (chunk.textResponse) {
            if (!started) { started = true; bubble.innerHTML = ""; }
            fullText += chunk.textResponse;
          }
          if (chunk.sources && chunk.sources.length > 0) {
            sources = chunk.sources;
          }
        } catch (_) {}
      }
    }

    // Final render with follow-up question extraction
    const { mainText, questions } = parseFollowUpQuestions(fullText);

    if (sources.length > 0) {
      bubble.innerHTML = parseMarkdown(mainText) + buildCitations(sources);
    } else {
      bubble.innerHTML = parseMarkdown(mainText);
    }

    if (settings.followup_enabled && questions.length > 0) {
      const chips = buildFollowUpChips(questions);
      if (chips) chatWindow.appendChild(chips);
    }

    // -----------------------------------------------------------------------
    // Persist to localStorage
    // -----------------------------------------------------------------------
    const currentSession = getSession(activeSessionId) || {
      session_id: activeSessionId,
      title: "",
      created_at: new Date().toISOString(),
      messages: [],
    };

    // Set the session title from the first user message
    if (!currentSession.title) {
      currentSession.title = text.slice(0, 55) + (text.length > 55 ? "…" : "");
      chatPanelTitle.textContent = currentSession.title;
    }

    currentSession.messages.push({ role: "user", content: text });
    currentSession.messages.push({
      role: "assistant",
      content: mainText,
      sources: sources.length > 0 ? sources : undefined,
    });

    upsertSession(currentSession);
    renderSessionList();

  } catch (err) {
    bubble.classList.add("error");
    bubble.innerHTML = `Error: ${escapeHtml(err.message)}`;
  } finally {
    isBusy = false;
    sendBtn.disabled = false;
    messageInput.focus();
    chatWindow.scrollTop = chatWindow.scrollHeight;
  }
}

// ---------------------------------------------------------------------------
// escapeHtml utility
// ---------------------------------------------------------------------------

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ---------------------------------------------------------------------------
// Page initialisation
// ---------------------------------------------------------------------------

// Load workspace settings (WORKSPACE_SLUG injected by Jinja2)
loadWorkspaceSettings(WORKSPACE_SLUG);

// Restore or start a session
(async function init() {
  const sessions = loadSessions();
  if (sessions.length > 0) {
    // Resume the most recent session
    selectSession(sessions[0].session_id);
  } else {
    // No history at all — start a fresh session automatically
    await startNewChat();
  }
})();
