function generateUUID() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, c => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}
let sessionId = generateUUID();
const chatWindow = document.getElementById("chat-window");
const messageInput = document.getElementById("message-input");
const sendBtn = document.getElementById("send-btn");
const defaultQuestionsBar = document.getElementById("default-questions-bar");

// ========== Settings ==========

// Cached workspace settings — loaded async on page init from the API.
// Falls back to safe defaults so the page works even before the fetch resolves.
let _workspaceSettings = { followup_enabled: false, followup_count: 3, default_questions: [], welcome_text: "Send a message to get started." };

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
    // Network error — keep defaults, don't break the chat
  }
  chatWindow.dataset.emptyText = _workspaceSettings.welcome_text || "Send a message to get started.";
  renderDefaultQuestionsBar();
}

// ========== Default Questions Bar ==========

let defaultQuestionsBarDismissed = false;

function dismissDefaultQuestionsBar() {
  if (defaultQuestionsBarDismissed) return;
  defaultQuestionsBarDismissed = true;
  defaultQuestionsBar.style.display = "none";
}

function renderDefaultQuestionsBar() {
  const categories = _workspaceSettings.default_questions || [];
  defaultQuestionsBar.innerHTML = "";

  const populated = categories.filter(c => c.questions && c.questions.length > 0);
  if (populated.length === 0) {
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

// Kick off settings load — WORKSPACE_SLUG is injected by Jinja2
loadWorkspaceSettings(WORKSPACE_SLUG);

// Parse markdown and ensure all links open in a new tab
function parseMarkdown(text) {
  const html = marked.parse(text);
  return html.replace(/<a /g, '<a target="_blank" rel="noopener noreferrer" ');
}

// Send on Enter key
messageInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

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

  // Deduplicate by title
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

// ========== Follow-up Questions ==========

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
      // Remove the chips so they don't linger
      container.remove();
      sendMessage(q);
    });
    chipsRow.appendChild(btn);
  });

  container.appendChild(chipsRow);
  return container;
}

// ========== Send Message ==========

async function sendMessage(overrideText) {
  const text = overrideText !== undefined ? overrideText : messageInput.value.trim();
  if (!text) return;

  dismissDefaultQuestionsBar();

  messageInput.value = "";
  sendBtn.disabled = true;

  const settings = getSettings();

  appendMessage("user", escapeHtml(text));
  const bubble = appendMessage("assistant", '<span class="typing-indicator"><span></span><span></span><span></span></span>');

  // Build the message to send — keep the clean question separate from the
  // follow-up suffix so the RAG backend uses only the question for retrieval.
  let messageToSend = text;
  let followupSuffix = "";
  if (settings.followup_enabled) {
    followupSuffix = buildFollowUpPromptSuffix(settings.followup_count);
  }

  let fullText = "";
  let started = false;
  let sources = [];

  try {
    const res = await fetch(`/api/chat/${WORKSPACE_SLUG}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: messageToSend, session_id: sessionId, reset: false, followup_suffix: followupSuffix }),
    });

    if (!res.ok) throw new Error(`Server error ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      // Split on SSE event boundaries (\n\n) so embedded newlines in JSON don't fragment events
      const events = buffer.split("\n\n");
      buffer = events.pop(); // last item may be an incomplete event

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
            // Render live — strip delimiter block while streaming so it doesn't flash
            const { mainText } = parseFollowUpQuestions(fullText);
            bubble.innerHTML = parseMarkdown(mainText);
            chatWindow.scrollTop = chatWindow.scrollHeight;
          }

          // Sources arrive on the final chunk
          if (chunk.sources && chunk.sources.length > 0) {
            sources = chunk.sources;
          }
        } catch (_) {
          // malformed chunk — skip
        }
      }
    }

    // Flush any remaining complete event left in the buffer after stream ends
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
        } catch (_) {
          // malformed — skip
        }
      }
    }

    // Final render — parse out follow-up questions
    const { mainText, questions } = parseFollowUpQuestions(fullText);

    if (sources.length > 0) {
      bubble.innerHTML = parseMarkdown(mainText) + buildCitations(sources);
    } else {
      bubble.innerHTML = parseMarkdown(mainText);
    }

    // Append follow-up chips below the bubble (outside it)
    if (settings.followup_enabled && questions.length > 0) {
      const chips = buildFollowUpChips(questions);
      if (chips) {
        chatWindow.appendChild(chips);
      }
    }

  } catch (err) {
    bubble.classList.add("error");
    bubble.innerHTML = `Error: ${escapeHtml(err.message)}`;
  } finally {
    sendBtn.disabled = false;
    messageInput.focus();
    chatWindow.scrollTop = chatWindow.scrollHeight;
  }
}

async function resetChat() {
  // Generate a fresh session ID so AnythingLLM treats this as a brand-new conversation
  const oldSessionId = sessionId;
  sessionId = generateUUID();

  try {
    await fetch(`/api/chat/${WORKSPACE_SLUG}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: "", session_id: oldSessionId, reset: true }),
    });
  } catch (_) {
    // best-effort
  }

  // Clear messages
  chatWindow.innerHTML = "";

  // Reload settings from the API (picks up any changes made in Settings page)
  // then re-show the default questions bar
  defaultQuestionsBarDismissed = false;
  await loadWorkspaceSettings(WORKSPACE_SLUG);
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
