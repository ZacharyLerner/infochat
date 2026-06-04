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
// Debug logger — prefixes all output with [RhodyRAG] for easy filtering
// ---------------------------------------------------------------------------
const _log = {
  _tag: "%c[RhodyRAG]%c",
  _tagStyle: "color:#2277b3;font-weight:700",
  _reset: "color:inherit;font-weight:normal",

  debug(...args) {
    console.debug(this._tag, this._tagStyle, this._reset, ...args);
  },
  info(...args) {
    console.info(this._tag, this._tagStyle, this._reset, ...args);
  },
  warn(...args) {
    console.warn(this._tag, this._tagStyle, this._reset, ...args);
  },
  error(...args) {
    console.error(this._tag, this._tagStyle, this._reset, ...args);
  },
  group(label) {
    console.groupCollapsed(`%c[RhodyRAG]%c ${label}`, this._tagStyle, this._reset);
  },
  groupEnd() {
    console.groupEnd();
  },
  time(label) {
    console.time(`[RhodyRAG] ${label}`);
  },
  timeEnd(label) {
    console.timeEnd(`[RhodyRAG] ${label}`);
  },
};

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
    if (raw) {
      const parsed = JSON.parse(raw);
      _log.debug(`loadSessions: found ${parsed.length} session(s) for workspace '${WORKSPACE_SLUG}'`);
      return parsed;
    }
  } catch (err) {
    _log.warn("loadSessions: failed to parse localStorage:", err);
  }
  _log.debug(`loadSessions: no sessions found for workspace '${WORKSPACE_SLUG}'`);
  return [];
}

function saveSessions(sessions) {
  try {
    localStorage.setItem(_storageKey(), JSON.stringify(sessions));
    _log.debug(`saveSessions: saved ${sessions.length} session(s)`);
  } catch (err) {
    _log.error("saveSessions: failed to write to localStorage:", err);
  }
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
  if (isBusy) {
    _log.warn("startNewChat: blocked — isBusy=true");
    return;
  }

  _log.info(`startNewChat: creating new session for workspace '${WORKSPACE_SLUG}'`);
  _log.time("startNewChat");

  try {
    const res = await fetch(`/api/chat/${WORKSPACE_SLUG}/session`, { method: "POST" });
    if (!res.ok) throw new Error(`Server returned ${res.status}`);
    const { session_id } = await res.json();
    _log.timeEnd("startNewChat");
    _log.info(`startNewChat: session created — session_id=${session_id}`);

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
    _log.timeEnd("startNewChat");
    _log.error("startNewChat: failed to create session from server:", err);
    _log.warn("startNewChat: falling back to client-only session");
    // Fallback: create a client-only session so the UI still works
    const session_id = generateUUID();
    _log.debug(`startNewChat: fallback session_id=${session_id}`);
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
  if (!session) {
    _log.warn(`selectSession: session '${sessionId}' not found in localStorage`);
    return;
  }

  _log.info(`selectSession: loading session '${sessionId}' — "${session.title || "New Chat"}" (${session.messages.length} messages)`);
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
      let userHtml = escapeHtml(msg.content);
      if (msg.filename) {
        userHtml = `<span class="user-file-badge">${_fileIcon(msg.filename)}<span>${escapeHtml(msg.filename)}</span></span>${userHtml}`;
      }
      appendMessage("user", userHtml);
    } else if (msg.role === "assistant") {
      if (!msg.content) continue; // skip empty assistant placeholders
      const html = parseMarkdown(msg.content) + buildCitations(msg.sources || []);
      appendMessage("assistant", html);
    } else if (msg.role === "email") {
      const html = buildEmailBubbleHtml(msg.to || "", msg.subject || "", msg.status || "sent");
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
  _log.debug(`loadWorkspaceSettings: fetching settings for '${slug}'`);
  try {
    const res = await fetch(`/api/workspaces/${slug}`);
    if (res.ok) {
      _workspaceSettings = await res.json();
      _log.info(`loadWorkspaceSettings: loaded — followup_enabled=${_workspaceSettings.followup_enabled} followup_count=${_workspaceSettings.followup_count} default_question_categories=${(_workspaceSettings.default_questions || []).length}`);
    } else {
      _log.warn(`loadWorkspaceSettings: server returned ${res.status} — using defaults`);
    }
  } catch (err) {
    _log.warn("loadWorkspaceSettings: network error — using defaults:", err);
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
  if (!text || isBusy) {
    if (isBusy) _log.warn("sendMessage: blocked — isBusy=true");
    return;
  }

  // Block send if a file is still being processed
  if (attachedFile && attachedFile.state === "loading") {
    _log.warn(`sendMessage: blocked — file '${attachedFile.filename}' is still processing`);
    // Give the user a brief visual hint
    const chip = fileAttachmentsEl.querySelector(".file-chip");
    if (chip) {
      chip.style.outline = "2px solid var(--uri-gold)";
      setTimeout(() => { chip.style.outline = ""; }, 1200);
    }
    return;
  }

  // If somehow there's no active session yet, create one first
  if (!activeSessionId) {
    _log.warn("sendMessage: no active session — creating one first");
    await startNewChat();
    if (!activeSessionId) {
      _log.error("sendMessage: still no session after startNewChat — aborting");
      return;
    }
  }

  dismissDefaultQuestionsBar();
  isBusy = true;
  messageInput.value = "";
  messageInput.style.height = "auto";
  sendBtn.disabled = true;

  const settings = getSettings();

  // Capture the file context before clearing (so it's used for this message only)
  const fileCtx = (attachedFile && attachedFile.state === "ready") ? { ...attachedFile } : null;

  _log.group(`sendMessage: "${text.slice(0, 60)}${text.length > 60 ? "…" : ""}"`);
  _log.debug("session_id:", activeSessionId);
  _log.debug("message length:", text.length, "chars");
  _log.debug("history turns:", (getSession(activeSessionId)?.messages || []).length);
  _log.debug("followup_enabled:", settings.followup_enabled);
  if (fileCtx) {
    _log.info("file attached:", fileCtx.filename,
      `(markdown=${fileCtx.markdown.length} chars, summary=${fileCtx.summary.length} chars)`);
    _log.debug("file summary:", fileCtx.summary);
  }
  _log.groupEnd();

  // Append user bubble immediately — include the filename if a file is attached
  let userHtml = escapeHtml(text);
  if (fileCtx) {
    userHtml = `<span class="user-file-badge">${_fileIcon(fileCtx.filename)}<span>${escapeHtml(fileCtx.filename)}</span></span>${userHtml}`;
  }
  appendMessage("user", userHtml);

  // Clear the attachment now (consumed by this message)
  if (fileCtx) removeAttachment();

  // Get the current session for history re-seeding.
  // Filter out email outcome messages — they have no content and are UI-only.
  const session = getSession(activeSessionId);
  const history = session
    ? session.messages.filter(m => m.role === "user" || m.role === "assistant")
    : [];

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
  let tokenCount = 0;
  let emailActionDetected = false; // true once EMAIL_ACTION: appears in the stream

  _log.time(`stream session=${activeSessionId}`);

  try {
    const requestBody = {
      message: text,
      session_id: activeSessionId,
      history: history,
      followup_suffix: followupSuffix,
    };
    if (fileCtx) {
      requestBody.file_context = {
        filename: fileCtx.filename,
        markdown: fileCtx.markdown,
        summary: fileCtx.summary,
      };
    }

    _log.debug(`sendMessage: POST /api/chat/${WORKSPACE_SLUG}/${activeSessionId}/stream`,
      { message_len: text.length, history_turns: history.length, has_file: !!fileCtx });

    const res = await fetch(`/api/chat/${WORKSPACE_SLUG}/${activeSessionId}/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestBody),
    });

    if (!res.ok) {
      _log.error(`sendMessage: stream endpoint returned HTTP ${res.status}`);
      throw new Error(`Server error ${res.status}`);
    }
    _log.debug("sendMessage: SSE stream opened — reading tokens…");

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

          if (chunk.error) {
            // RhodyRAG fired an event: error — surface it to the user
            _log.error("sendMessage: RhodyRAG error event:", chunk.error);
            throw new Error(chunk.error);
          }

          if (chunk.textResponse) {
            if (!started) {
              started = true;
              _log.debug("sendMessage: first token received — clearing typing indicator");
              bubble.innerHTML = "";
            }
            tokenCount++;
            fullText += chunk.textResponse;

            if (emailActionDetected) {
              // Already in email mode — just accumulate, don't touch the bubble
            } else if (fullText.includes(EMAIL_ACTION_DELIMITER)) {
              // EMAIL_ACTION: just appeared — wipe whatever was rendered and
              // replace with the drafting indicator immediately
              emailActionDetected = true;
              _log.info("sendMessage: EMAIL_ACTION detected mid-stream — switching to drafting indicator");
              bubble.innerHTML = `<span class="drafting-email-indicator"><span class="typing-indicator"><span></span><span></span><span></span></span><span class="drafting-email-label">Drafting email…</span></span>`;
              chatWindow.scrollTop = chatWindow.scrollHeight;
            } else {
              // Normal render — only show text before any EMAIL_ACTION delimiter
              const { mainText } = parseFollowUpQuestions(fullText);
              bubble.innerHTML = parseMarkdown(mainText);
              chatWindow.scrollTop = chatWindow.scrollHeight;
            }
          }

          if (chunk.sources && chunk.sources.length > 0) {
            sources = chunk.sources;
            _log.debug(`sendMessage: received ${sources.length} source(s)`, sources.map(s => s.title));
          }
        } catch (parseErr) {
          // Re-throw errors we deliberately raised above (RhodyRAG error events)
          if (parseErr.message && !parseErr.message.startsWith("Unexpected")) throw parseErr;
          _log.warn("sendMessage: failed to parse SSE chunk:", raw.slice(0, 100), parseErr);
        }
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
        } catch (parseErr) {
          _log.warn("sendMessage: failed to parse final SSE chunk:", raw.slice(0, 100), parseErr);
        }
      }
    }

    _log.timeEnd(`stream session=${activeSessionId}`);
    _log.info(`sendMessage: stream complete — ${tokenCount} token chunks, ${sources.length} source(s), response=${fullText.length} chars`);

    // Final render — extract email action first, then follow-up questions
    const { mainText: textAfterEmail, emailData } = parseEmailAction(fullText);
    const { mainText, questions } = parseFollowUpQuestions(textAfterEmail);

    if (emailData) {
      // Remove the drafting-indicator bubble entirely — the modal replaces it
      bubble.remove();
      _log.info("sendMessage: email action parsed — opening confirmation modal");
      showEmailConfirmModal(emailData);
    } else {
      // Normal render
      if (questions.length > 0) {
        _log.debug(`sendMessage: parsed ${questions.length} follow-up question(s)`);
      }

      if (sources.length > 0) {
        bubble.innerHTML = parseMarkdown(mainText) + buildCitations(sources);
      } else {
        bubble.innerHTML = parseMarkdown(mainText);
      }

      if (settings.followup_enabled && questions.length > 0) {
        const chips = buildFollowUpChips(questions);
        if (chips) chatWindow.appendChild(chips);
      }
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
      _log.debug(`sendMessage: set session title to "${currentSession.title}"`);
    }

    const userMsg = { role: "user", content: text };
    if (fileCtx) userMsg.filename = fileCtx.filename;
    currentSession.messages.push(userMsg);
    // Email responses are persisted by _persistEmailMessage after modal outcome;
    // only save a regular assistant message when there is no email action.
    if (!emailData) {
      currentSession.messages.push({
        role: "assistant",
        content: mainText,
        sources: sources.length > 0 ? sources : undefined,
      });
    }

    upsertSession(currentSession);
    _log.debug(`sendMessage: session saved — total messages=${currentSession.messages.length}`);
    renderSessionList();

  } catch (err) {
    _log.timeEnd(`stream session=${activeSessionId}`);
    _log.error("sendMessage: stream failed:", err);
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
// File attachment state
// ---------------------------------------------------------------------------
// A single file can be attached per message.
// States: null | { state: "loading", filename } | { state: "ready", filename, markdown, summary }
//                                                 | { state: "error", filename, error }

let attachedFile = null;

const fileAttachmentsEl = document.getElementById("file-attachments");
const fileInputEl       = document.getElementById("file-input");

// ---------------------------------------------------------------------------
// File type helpers
// ---------------------------------------------------------------------------

function _fileIcon(filename) {
  const ext = (filename.split(".").pop() || "").toLowerCase();
  if (["png","jpg","jpeg","gif","webp","bmp","tiff","tif"].includes(ext)) {
    // Image icon
    return `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>`;
  }
  if (ext === "pdf") {
    // PDF icon
    return `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="9" y1="13" x2="15" y2="13"/></svg>`;
  }
  // Generic document icon
  return `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>`;
}

// ---------------------------------------------------------------------------
// Chip rendering
// ---------------------------------------------------------------------------

function renderFileAttachments() {
  fileAttachmentsEl.innerHTML = "";

  if (!attachedFile) {
    fileAttachmentsEl.style.display = "none";
    return;
  }

  fileAttachmentsEl.style.display = "flex";

  const chip = document.createElement("div");
  chip.className = "file-chip";

  if (attachedFile.state === "loading") {
    chip.classList.add("loading");
    chip.innerHTML = `
      <span class="file-chip-spinner"></span>
      <span class="file-chip-name">${escapeHtml(attachedFile.filename)}</span>
      <span class="file-chip-status">Processing…</span>
    `;
  } else if (attachedFile.state === "error") {
    chip.classList.add("error");
    chip.innerHTML = `
      ${_fileIcon(attachedFile.filename)}
      <span class="file-chip-name">${escapeHtml(attachedFile.filename)}</span>
      <span class="file-chip-status">${escapeHtml(attachedFile.error)}</span>
      <button class="file-chip-remove" title="Remove" onclick="removeAttachment()">
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    `;
  } else {
    // ready
    chip.innerHTML = `
      <span class="file-chip-icon">${_fileIcon(attachedFile.filename)}</span>
      <span class="file-chip-name">${escapeHtml(attachedFile.filename)}</span>
      <button class="file-chip-remove" title="Remove attachment" onclick="removeAttachment()">
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    `;
  }

  fileAttachmentsEl.appendChild(chip);
}

function removeAttachment() {
  attachedFile = null;
  // Reset the file input so the same file can be re-selected
  fileInputEl.value = "";
  renderFileAttachments();
}

// ---------------------------------------------------------------------------
// File selection handler (fires when the hidden <input type="file"> changes)
// ---------------------------------------------------------------------------

function handleFileSelect(input) {
  const file = input.files && input.files[0];
  if (!file) {
    _log.warn("handleFileSelect: no file selected");
    return;
  }

  _log.info(`handleFileSelect: '${file.name}' (${(file.size / 1024).toFixed(1)} KB, type='${file.type}')`);

  // Replace any existing attachment immediately
  if (attachedFile) {
    _log.debug(`handleFileSelect: replacing existing attachment '${attachedFile.filename}'`);
  }
  attachedFile = { state: "loading", filename: file.name };
  renderFileAttachments();

  uploadFile(file);
}

// ---------------------------------------------------------------------------
// Upload & process the file via the backend
// ---------------------------------------------------------------------------

async function uploadFile(file) {
  const formData = new FormData();
  formData.append("file", file);

  _log.info(`uploadFile: uploading '${file.name}' to /api/upload/${WORKSPACE_SLUG}`);
  _log.time(`uploadFile: ${file.name}`);

  try {
    const res = await fetch(`/api/upload/${WORKSPACE_SLUG}`, {
      method: "POST",
      body: formData,
    });

    if (!res.ok) {
      let detail = `Server error ${res.status}`;
      try {
        const body = await res.json();
        if (body.detail) detail = body.detail;
      } catch (_) {}
      _log.error(`uploadFile: server returned HTTP ${res.status} — ${detail}`);
      throw new Error(detail);
    }

    const data = await res.json();
    _log.timeEnd(`uploadFile: ${file.name}`);
    _log.info(
      `uploadFile: success — markdown=${data.markdown?.length ?? 0} chars, summary=${data.summary?.length ?? 0} chars`,
    );
    _log.group(`uploadFile: summary — ${file.name}`);
    console.info(data.summary ?? "(none)");
    console.groupEnd();
    const mdPreview = (data.markdown ?? "").slice(0, 300);
    _log.group(`uploadFile: markdown preview (first 300 chars) — ${file.name}`);
    console.info(mdPreview + (data.markdown?.length > 300 ? "\n[...]" : ""));
    console.groupEnd();

    // data = { filename, markdown, summary }
    attachedFile = {
      state: "ready",
      filename: data.filename,
      markdown: data.markdown,
      summary: data.summary,
    };
  } catch (err) {
    _log.timeEnd(`uploadFile: ${file.name}`);
    _log.error(`uploadFile: failed for '${file.name}':`, err);
    attachedFile = {
      state: "error",
      filename: attachedFile ? attachedFile.filename : file.name,
      error: err.message || "Upload failed",
    };
  }

  renderFileAttachments();
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ---------------------------------------------------------------------------
// Email action — detection, confirmation modal, and sending
// ---------------------------------------------------------------------------

// Delimiter the LLM uses to signal an email action block.
const EMAIL_ACTION_DELIMITER = "EMAIL_ACTION:";

/**
 * Scan the full LLM response for an EMAIL_ACTION block.
 * The block is expected at the very start of the response (before any text).
 * Everything from EMAIL_ACTION: onward is stripped from mainText.
 *
 * Returns { mainText, emailData } where emailData is null if not found.
 */
function parseEmailAction(fullText) {
  const idx = fullText.indexOf(EMAIL_ACTION_DELIMITER);
  if (idx === -1) return { mainText: fullText, emailData: null };

  const mainText = fullText.slice(0, idx).trimEnd();
  const jsonBlock = fullText.slice(idx + EMAIL_ACTION_DELIMITER.length).trim();

  // Grab everything from the first { to the last } — handles multi-line LLM output
  const start = jsonBlock.indexOf("{");
  const end   = jsonBlock.lastIndexOf("}");
  if (start === -1 || end === -1 || end <= start) {
    _log.warn("parseEmailAction: EMAIL_ACTION delimiter found but no valid JSON block");
    return { mainText: fullText, emailData: null };
  }

  let raw = jsonBlock.slice(start, end + 1);

  // LLMs frequently emit literal newlines inside JSON string values, which is
  // invalid JSON. Replace any bare CR/LF that are NOT already escaped with \n.
  // Strategy: walk the string and only replace newlines that appear inside a
  // JSON string value (i.e. while inString === true).
  let sanitized = "";
  let inString = false;
  let escaped = false;
  for (let i = 0; i < raw.length; i++) {
    const ch = raw[i];
    if (escaped) {
      sanitized += ch;
      escaped = false;
      continue;
    }
    if (ch === "\\") {
      sanitized += ch;
      escaped = true;
      continue;
    }
    if (ch === '"') {
      inString = !inString;
      sanitized += ch;
      continue;
    }
    if (inString && (ch === "\n" || ch === "\r")) {
      // Replace bare newline with escaped version
      sanitized += ch === "\n" ? "\\n" : "\\r";
      continue;
    }
    sanitized += ch;
  }

  try {
    const emailData = JSON.parse(sanitized);
    _log.info("parseEmailAction: email action detected", emailData);
    return { mainText, emailData };
  } catch (err) {
    _log.warn("parseEmailAction: failed to parse email JSON:", err, sanitized.slice(0, 200));
    return { mainText: fullText, emailData: null };
  }
}

// Holds the email data and session context while the modal is open
let _pendingEmail = null; // { emailData, sessionId }

/**
 * Build the HTML for an email summary bubble (used live and on history restore).
 * status: "sent" | "cancelled"
 */
function buildEmailBubbleHtml(to, subject, status) {
  const icon = status === "sent"
    ? `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 12 4 12"/><polyline points="4 12 9 18 20 6"/></svg>`
    : `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;
  const label = status === "sent" ? "Email sent" : "Email cancelled";
  const cls   = status === "sent" ? "email-bubble-sent" : "email-bubble-cancelled";
  return `<span class="email-bubble ${cls}">${icon}<span class="email-bubble-label">${label}</span><span class="email-bubble-detail">To: ${escapeHtml(to)} &mdash; <em>${escapeHtml(subject)}</em></span></span>`;
}

/**
 * Persist an email outcome message to the active session in localStorage.
 */
function _persistEmailMessage(sessionId, to, subject, status) {
  const session = getSession(sessionId);
  if (!session) return;
  session.messages.push({
    role: "email",
    to,
    subject,
    status, // "sent" | "cancelled"
  });
  upsertSession(session);
  renderSessionList();
}

/**
 * Open the email confirmation modal pre-filled with LLM-drafted values.
 * The user can edit all fields before confirming.
 */
function showEmailConfirmModal(emailData) {
  const modal = document.getElementById("email-confirm-modal");
  if (!modal) {
    _log.error("showEmailConfirmModal: #email-confirm-modal not found in DOM");
    return;
  }

  // Capture context so cancel/confirm can write to the right session
  _pendingEmail = { emailData, sessionId: activeSessionId };

  // Pre-fill fields
  document.getElementById("email-to").value      = emailData.to      || "";
  document.getElementById("email-subject").value = emailData.subject || "";
  document.getElementById("email-body").value    = emailData.body    || "";
  document.getElementById("email-from").value    = emailData.from    || "noreply@uri.edu";

  // Clear any previous status
  const statusEl = document.getElementById("email-modal-status");
  statusEl.textContent = "";
  statusEl.className = "email-modal-status";

  // Reset buttons
  document.getElementById("email-send-btn").disabled = false;
  document.getElementById("email-send-btn").textContent = "Send Email";

  modal.classList.add("modal-visible");
  _log.info("showEmailConfirmModal: modal opened");
}

function closeEmailConfirmModal() {
  const modal = document.getElementById("email-confirm-modal");
  if (!modal) return;
  modal.classList.remove("modal-visible");

  // If modal was closed without sending, record a cancellation
  if (_pendingEmail) {
    const { emailData, sessionId } = _pendingEmail;
    const to      = document.getElementById("email-to").value.trim()      || emailData.to      || "";
    const subject = document.getElementById("email-subject").value.trim() || emailData.subject || "";
    _pendingEmail = null;

    const html = buildEmailBubbleHtml(to, subject, "cancelled");
    appendMessage("assistant", html);
    chatWindow.scrollTop = chatWindow.scrollHeight;
    _persistEmailMessage(sessionId, to, subject, "cancelled");
    _log.info("closeEmailConfirmModal: email cancelled");
  }
}

async function confirmSendEmail() {
  const to      = document.getElementById("email-to").value.trim();
  const subject = document.getElementById("email-subject").value.trim();
  const body    = document.getElementById("email-body").value.trim();
  const from    = document.getElementById("email-from").value.trim();
  const statusEl = document.getElementById("email-modal-status");
  const sendBtn  = document.getElementById("email-send-btn");

  if (!to || !subject || !body) {
    statusEl.textContent = "Please fill in all required fields.";
    statusEl.className = "email-modal-status error";
    return;
  }

  sendBtn.disabled = true;
  sendBtn.textContent = "Sending…";
  statusEl.textContent = "";
  statusEl.className = "email-modal-status";

  _log.info(`confirmSendEmail: sending to='${to}' subject='${subject}'`);

  try {
    const res = await fetch("/api/send-email", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ to, subject, body, from_addr: from }),
    });

    const data = await res.json();

    if (!res.ok) {
      throw new Error(data.detail || `Server error ${res.status}`);
    }

    _log.info("confirmSendEmail: success —", data.message);
    statusEl.textContent = "✓ " + (data.message || "Email sent successfully.");
    statusEl.className = "email-modal-status success";
    sendBtn.textContent = "Sent";

    // Capture session before clearing _pendingEmail
    const sessionId = _pendingEmail ? _pendingEmail.sessionId : activeSessionId;
    _pendingEmail = null; // clear before closeEmailConfirmModal so it won't fire cancel

    // Auto-close after a short delay
    setTimeout(() => {
      const modal = document.getElementById("email-confirm-modal");
      if (modal) modal.classList.remove("modal-visible");
    }, 2000);

    // Append confirmation bubble to the chat
    const html = buildEmailBubbleHtml(to, subject, "sent");
    appendMessage("assistant", html);
    chatWindow.scrollTop = chatWindow.scrollHeight;
    _persistEmailMessage(sessionId, to, subject, "sent");

  } catch (err) {
    _log.error("confirmSendEmail: failed:", err);
    statusEl.textContent = err.message || "Failed to send email.";
    statusEl.className = "email-modal-status error";
    sendBtn.disabled = false;
    sendBtn.textContent = "Send Email";
  }
}

// ---------------------------------------------------------------------------
// Page initialisation
// ---------------------------------------------------------------------------

// Load workspace settings (WORKSPACE_SLUG injected by Jinja2)
_log.info(`utils.js initialising — workspace='${WORKSPACE_SLUG}'`);
loadWorkspaceSettings(WORKSPACE_SLUG);

// Restore or start a session
(async function init() {
  const sessions = loadSessions();
  if (sessions.length > 0) {
    _log.info(`init: resuming most recent session '${sessions[0].session_id}'`);
    // Resume the most recent session
    selectSession(sessions[0].session_id);
  } else {
    _log.info("init: no existing sessions — starting fresh");
    // No history at all — start a fresh session automatically
    await startNewChat();
  }
})();
