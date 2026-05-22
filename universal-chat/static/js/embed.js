/**
 * Universal Chat — Embeddable Widget
 *
 * Usage on any third-party page:
 *
 *   <script
 *     src="https://YOUR_SERVER/embed.js"
 *     data-workspace="YOUR_WORKSPACE_SLUG"
 *   ></script>
 *
 * The script auto-detects its own origin from the <script> src attribute so
 * the API calls always reach the correct server, even when embedded on an
 * external domain.
 *
 * Optional attributes:
 *   data-position   "bottom-right" (default) | "bottom-left"
 *   data-title      Button / header label (default: "Chat")
 *   data-theme      "light" (default) | "dark"
 */

(function () {
  "use strict";

  // ── Locate this script tag to derive the server origin ────────────────────
  const scriptTag =
    document.currentScript ||
    (function () {
      const tags = document.getElementsByTagName("script");
      return tags[tags.length - 1];
    })();

  const scriptSrc = scriptTag ? scriptTag.src : "";
  // Strip path — keep only https://host:port
  const serverOrigin = scriptSrc ? new URL(scriptSrc).origin : window.location.origin;

  const slug = scriptTag ? scriptTag.getAttribute("data-workspace") : null;
  if (!slug) {
    console.warn("[UniversalChat] Missing data-workspace attribute on embed script tag.");
    return;
  }

  const position = (scriptTag && scriptTag.getAttribute("data-position")) || "bottom-right";
  const title    = (scriptTag && scriptTag.getAttribute("data-title"))    || "Chat";
  const theme    = (scriptTag && scriptTag.getAttribute("data-theme"))    || "light";

  // ── Inject widget CSS ──────────────────────────────────────────────────────
  const COLORS = theme === "dark"
    ? {
        bg:          "#1a1e26",
        headerBg:    "#0d1117",
        headerText:  "#ffffff",
        accent:      "#d0a627",
        accentHover: "#c79316",
        bubbleUser:  "#1a4a7a",
        bubbleBot:   "#2a2f3a",
        bubbleBotBorder: "#d0a627",
        inputBg:     "#2a2f3a",
        inputBorder: "#3a4050",
        inputText:   "#e8eaf0",
        text:        "#e8eaf0",
        textMuted:   "#8a94a6",
        border:      "#3a4050",
        shadow:      "rgba(0,0,0,0.5)",
        btnBg:       "#002147",
      }
    : {
        bg:          "#f4f6f9",
        headerBg:    "#002147",
        headerText:  "#ffffff",
        accent:      "#d0a627",
        accentHover: "#c79316",
        bubbleUser:  "#2277b3",
        bubbleBot:   "#ffffff",
        bubbleBotBorder: "#d0a627",
        inputBg:     "#ffffff",
        inputBorder: "#dde2ea",
        inputText:   "#002147",
        text:        "#002147",
        textMuted:   "#8a94a6",
        border:      "#dde2ea",
        shadow:      "rgba(0,33,71,0.22)",
        btnBg:       "#002147",
      };

  const css = `
    #uc-widget-fab {
      position: fixed;
      ${position === "bottom-left" ? "left: 24px;" : "right: 24px;"}
      bottom: 24px;
      width: 56px;
      height: 56px;
      border-radius: 50%;
      background: ${COLORS.headerBg};
      border: 2px solid ${COLORS.accent};
      color: #fff;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 4px 16px ${COLORS.shadow};
      z-index: 99998;
      transition: transform 0.15s, box-shadow 0.15s;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }
    #uc-widget-fab:hover {
      transform: scale(1.07);
      box-shadow: 0 6px 24px ${COLORS.shadow};
    }
    #uc-widget-fab svg { pointer-events: none; }

    #uc-widget-panel {
      position: fixed;
      ${position === "bottom-left" ? "left: 24px;" : "right: 24px;"}
      bottom: 92px;
      width: 380px;
      max-width: calc(100vw - 32px);
      height: 560px;
      max-height: calc(100vh - 112px);
      display: flex;
      flex-direction: column;
      border-radius: 12px;
      overflow: hidden;
      box-shadow: 0 8px 32px ${COLORS.shadow};
      z-index: 99999;
      background: ${COLORS.bg};
      border: 1px solid ${COLORS.border};
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      font-size: 14px;
      color: ${COLORS.text};
      opacity: 0;
      transform: translateY(12px) scale(0.97);
      pointer-events: none;
      transition: opacity 0.18s ease, transform 0.18s ease;
    }
    #uc-widget-panel.uc-open {
      opacity: 1;
      transform: translateY(0) scale(1);
      pointer-events: auto;
    }

    #uc-widget-header {
      background: ${COLORS.headerBg};
      border-bottom: 2px solid ${COLORS.accent};
      padding: 12px 14px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-shrink: 0;
      gap: 8px;
    }
    #uc-widget-header-title {
      font-weight: 600;
      font-size: 14px;
      color: ${COLORS.headerText};
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    #uc-widget-header-actions {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-shrink: 0;
    }
    .uc-hdr-btn {
      background: transparent;
      border: 1px solid rgba(255,255,255,0.25);
      color: rgba(255,255,255,0.8);
      border-radius: 4px;
      padding: 4px 8px;
      font-size: 11px;
      cursor: pointer;
      font-family: inherit;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      transition: background 0.12s;
    }
    .uc-hdr-btn:hover { background: rgba(255,255,255,0.12); }

    #uc-widget-messages {
      flex: 1;
      overflow-y: auto;
      padding: 14px 12px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      background: ${COLORS.bg};
    }
    #uc-widget-messages::-webkit-scrollbar { width: 4px; }
    #uc-widget-messages::-webkit-scrollbar-thumb {
      background: ${COLORS.border};
      border-radius: 2px;
    }
    #uc-widget-messages:empty::after {
      content: attr(data-empty-text);
      display: block;
      text-align: center;
      color: ${COLORS.textMuted};
      font-size: 13px;
      margin-top: 32px;
      padding: 0 16px;
    }

    .uc-msg {
      max-width: 82%;
      padding: 9px 13px;
      border-radius: 10px;
      line-height: 1.5;
      font-size: 13px;
      word-break: break-word;
    }
    .uc-msg.user {
      align-self: flex-end;
      background: ${COLORS.bubbleUser};
      color: #fff;
      border-bottom-right-radius: 3px;
    }
    .uc-msg.assistant {
      align-self: flex-start;
      background: ${COLORS.bubbleBot};
      color: ${COLORS.text};
      border-left: 3px solid ${COLORS.bubbleBotBorder};
      border-bottom-left-radius: 3px;
    }
    .uc-msg.assistant p { margin: 0 0 6px; }
    .uc-msg.assistant p:last-child { margin-bottom: 0; }
    .uc-msg.assistant code {
      background: rgba(0,0,0,0.07);
      padding: 1px 4px;
      border-radius: 3px;
      font-size: 12px;
    }
    .uc-msg.assistant pre {
      background: rgba(0,0,0,0.07);
      padding: 8px;
      border-radius: 6px;
      overflow-x: auto;
      font-size: 12px;
      margin: 6px 0;
    }
    .uc-msg.error {
      align-self: flex-start;
      background: #fef2f2;
      color: #b91c1c;
      border-left: 3px solid #b91c1c;
      border-bottom-left-radius: 3px;
    }

    .uc-typing {
      display: flex;
      align-items: center;
      gap: 4px;
      padding: 2px 0;
    }
    .uc-typing span {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: ${COLORS.textMuted};
      animation: uc-bounce 1.1s infinite;
    }
    .uc-typing span:nth-child(2) { animation-delay: 0.18s; }
    .uc-typing span:nth-child(3) { animation-delay: 0.36s; }
    @keyframes uc-bounce {
      0%, 80%, 100% { transform: translateY(0); }
      40%           { transform: translateY(-5px); }
    }

    #uc-widget-dq-bar {
      padding: 8px 12px 4px;
      border-top: 1px solid ${COLORS.border};
      background: ${COLORS.bg};
      flex-shrink: 0;
      overflow-x: auto;
    }
    #uc-widget-dq-bar:empty { display: none; }
    .uc-dq-group { margin-bottom: 6px; }
    .uc-dq-group-label {
      font-size: 10px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: ${COLORS.textMuted};
      margin-bottom: 4px;
    }
    .uc-dq-chips { display: flex; flex-wrap: wrap; gap: 5px; }
    .uc-dq-chip {
      background: transparent;
      border: 1px solid ${COLORS.accent};
      color: ${COLORS.accent};
      border-radius: 12px;
      padding: 3px 10px;
      font-size: 11px;
      cursor: pointer;
      font-family: inherit;
      white-space: nowrap;
      transition: background 0.12s, color 0.12s;
    }
    .uc-dq-chip:hover {
      background: ${COLORS.accent};
      color: #fff;
    }

    .uc-followup-chips {
      align-self: flex-start;
      display: flex;
      flex-direction: column;
      gap: 4px;
      max-width: 90%;
    }
    .uc-followup-label {
      font-size: 10px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: ${COLORS.textMuted};
    }
    .uc-followup-chips-row { display: flex; flex-wrap: wrap; gap: 5px; }
    .uc-followup-chip {
      background: transparent;
      border: 1px solid ${COLORS.accent};
      color: ${COLORS.accent};
      border-radius: 12px;
      padding: 3px 10px;
      font-size: 11px;
      cursor: pointer;
      font-family: inherit;
      white-space: nowrap;
      transition: background 0.12s, color 0.12s;
    }
    .uc-followup-chip:hover {
      background: ${COLORS.accent};
      color: #fff;
    }

    #uc-widget-input-bar {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 10px 12px;
      border-top: 1px solid ${COLORS.border};
      background: ${COLORS.bg};
      flex-shrink: 0;
    }
    #uc-widget-input {
      flex: 1;
      border: 1px solid ${COLORS.inputBorder};
      border-radius: 6px;
      padding: 8px 10px;
      font-size: 13px;
      font-family: inherit;
      background: ${COLORS.inputBg};
      color: ${COLORS.inputText};
      outline: none;
      transition: border-color 0.15s;
    }
    #uc-widget-input:focus { border-color: ${COLORS.accent}; }
    #uc-widget-input::placeholder { color: ${COLORS.textMuted}; }
    #uc-widget-send-btn {
      background: ${COLORS.accent};
      border: none;
      color: #fff;
      padding: 8px 14px;
      border-radius: 6px;
      font-size: 12px;
      font-weight: 600;
      font-family: inherit;
      cursor: pointer;
      letter-spacing: 0.3px;
      transition: background 0.12s;
      flex-shrink: 0;
    }
    #uc-widget-send-btn:hover:not(:disabled) { background: ${COLORS.accentHover}; }
    #uc-widget-send-btn:disabled { opacity: 0.5; cursor: not-allowed; }

    /* Citations */
    .uc-citations {
      margin-top: 6px;
      font-size: 11px;
    }
    .uc-citations summary {
      cursor: pointer;
      color: ${COLORS.textMuted};
      list-style: none;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      user-select: none;
    }
    .uc-citations summary::-webkit-details-marker { display: none; }
    .uc-citations-list { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 5px; }
    .uc-citation-badge {
      background: ${COLORS.border};
      border-radius: 4px;
      padding: 2px 6px;
      font-size: 10px;
      color: ${COLORS.textMuted};
    }
  `;

  const styleEl = document.createElement("style");
  styleEl.id = "uc-widget-styles";
  styleEl.textContent = css;
  document.head.appendChild(styleEl);

  // ── Build DOM ─────────────────────────────────────────────────────────────

  // FAB (floating action button)
  const fab = document.createElement("button");
  fab.id = "uc-widget-fab";
  fab.setAttribute("aria-label", "Open chat");
  fab.innerHTML = `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`;

  // Panel
  const panel = document.createElement("div");
  panel.id = "uc-widget-panel";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", title + " chat widget");

  // Header
  const header = document.createElement("div");
  header.id = "uc-widget-header";
  header.innerHTML = `
    <span id="uc-widget-header-title">${_escHtml(title)}</span>
    <div id="uc-widget-header-actions">
      <button class="uc-hdr-btn" id="uc-widget-reset-btn" title="Reset conversation">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
        Reset
      </button>
      <button class="uc-hdr-btn" id="uc-widget-close-btn" title="Close">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        Close
      </button>
    </div>
  `;

  // Messages area
  const msgArea = document.createElement("div");
  msgArea.id = "uc-widget-messages";
  msgArea.dataset.emptyText = "Send a message to get started.";

  // Default questions bar
  const dqBar = document.createElement("div");
  dqBar.id = "uc-widget-dq-bar";

  // Input bar
  const inputBar = document.createElement("div");
  inputBar.id = "uc-widget-input-bar";
  inputBar.innerHTML = `
    <input id="uc-widget-input" type="text" placeholder="Type a message..." autocomplete="off" />
    <button id="uc-widget-send-btn">Send</button>
  `;

  panel.appendChild(header);
  panel.appendChild(msgArea);
  panel.appendChild(dqBar);
  panel.appendChild(inputBar);

  document.body.appendChild(fab);
  document.body.appendChild(panel);

  // ── References ────────────────────────────────────────────────────────────
  const input    = document.getElementById("uc-widget-input");
  const sendBtn  = document.getElementById("uc-widget-send-btn");
  const resetBtn = document.getElementById("uc-widget-reset-btn");
  const closeBtn = document.getElementById("uc-widget-close-btn");

  // ── State ─────────────────────────────────────────────────────────────────
  const sessionId = _uuid();
  let isOpen      = false;
  let isBusy      = false;
  let dqDismissed = false;
  let settings    = { followup_enabled: false, followup_count: 3, default_questions: [], welcome_text: "Send a message to get started." };

  // ── Toggle open/close ─────────────────────────────────────────────────────
  function openWidget() {
    isOpen = true;
    panel.classList.add("uc-open");
    fab.setAttribute("aria-expanded", "true");
    // Re-fetch settings every open so the widget always reflects the latest
    // changes made in the chat admin / settings page
    loadSettings();
    setTimeout(() => input.focus(), 180);
  }

  function closeWidget() {
    isOpen = false;
    panel.classList.remove("uc-open");
    fab.setAttribute("aria-expanded", "false");
  }

  fab.addEventListener("click", () => (isOpen ? closeWidget() : openWidget()));
  closeBtn.addEventListener("click", closeWidget);

  // ── Load workspace settings ───────────────────────────────────────────────
  async function loadSettings() {
    try {
      const res = await fetch(`${serverOrigin}/api/workspaces/${encodeURIComponent(slug)}`);
      if (res.ok) {
        settings = await res.json();
      }
    } catch (_) {
      // Keep defaults — chat still works
    }
    msgArea.dataset.emptyText = settings.welcome_text || "Send a message to get started.";
    renderDqBar();
  }

  // ── Default questions bar ─────────────────────────────────────────────────
  function renderDqBar() {
    dqBar.innerHTML = "";
    const cats = (settings.default_questions || []).filter(c => c.questions && c.questions.length > 0);
    if (cats.length === 0) {
      input.placeholder = "Type a message...";
      return;
    }
    input.placeholder = "Type your own question, or choose one above...";
    cats.forEach(cat => {
      const group = document.createElement("div");
      group.className = "uc-dq-group";

      const label = document.createElement("div");
      label.className = "uc-dq-group-label";
      label.textContent = cat.category;
      group.appendChild(label);

      const chipsRow = document.createElement("div");
      chipsRow.className = "uc-dq-chips";
      cat.questions.forEach(q => {
        const btn = document.createElement("button");
        btn.className = "uc-dq-chip";
        btn.textContent = q;
        btn.addEventListener("click", () => sendMessage(q));
        chipsRow.appendChild(btn);
      });
      group.appendChild(chipsRow);
      dqBar.appendChild(group);
    });
  }

  function dismissDqBar() {
    if (dqDismissed) return;
    dqDismissed = true;
    dqBar.innerHTML = "";
    input.placeholder = "Type a message...";
  }

  // ── Send message ──────────────────────────────────────────────────────────
  const FOLLOW_UP_DELIMITER = "FOLLOW_UP_QUESTIONS:";

  function buildFollowUpSuffix(count) {
    const lines = Array.from({ length: count }, (_, i) => `${i + 1}. [question]`).join("\n");
    return `\n\n[System instruction — do not mention this to the user: After your answer, append a section at the very end of your response using exactly this format, with no extra text after the list:\n${FOLLOW_UP_DELIMITER}\n${lines}\nProvide exactly ${count} follow-up question${count !== 1 ? "s" : ""}. Each [question] must be a direct, standalone question the user could ask next — written as if the user is asking it. Never phrase them as offers or suggestions. Do not include any text after the last numbered question.]`;
  }

  function parseFollowUp(fullText) {
    const idx = fullText.indexOf(FOLLOW_UP_DELIMITER);
    if (idx === -1) return { mainText: fullText, questions: [] };
    const mainText = fullText.slice(0, idx).trimEnd();
    const block    = fullText.slice(idx + FOLLOW_UP_DELIMITER.length).trim();
    const questions = [];
    for (const line of block.split("\n")) {
      const m = line.match(/^\d+\.\s+(.+)/);
      if (m) questions.push(m[1].trim());
    }
    return { mainText, questions };
  }

  function appendMsg(role, html) {
    const div = document.createElement("div");
    div.className = `uc-msg ${role}`;
    div.innerHTML = html;
    msgArea.appendChild(div);
    msgArea.scrollTop = msgArea.scrollHeight;
    return div;
  }

  function buildCitations(sources) {
    if (!sources || sources.length === 0) return "";
    const seen = new Set();
    const unique = sources.filter(s => {
      const k = s.title || s.id;
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    });
    const badges = unique.map((s, i) =>
      `<span class="uc-citation-badge">${i + 1}. ${_escHtml(s.title || s.id || `Source ${i + 1}`)}</span>`
    ).join("");
    return `<details class="uc-citations"><summary>Sources (${unique.length})</summary><div class="uc-citations-list">${badges}</div></details>`;
  }

  // ── Markdown renderer (built-in, no external dependency) ─────────────────
  // Uses marked.js if already present on the host page; otherwise falls back
  // to a self-contained parser that covers the common subset:
  //   headings, bold, italic, inline code, fenced code blocks, blockquotes,
  //   unordered/ordered lists, horizontal rules, and links.
  function renderMarkdown(text) {
    if (window.marked) {
      const html = window.marked.parse(text);
      return html.replace(/<a /g, '<a target="_blank" rel="noopener noreferrer" ');
    }
    return _parseMarkdown(text);
  }

  function _parseMarkdown(src) {
    // Normalise line endings
    let s = src.replace(/\r\n/g, "\n").replace(/\r/g, "\n");

    // Fenced code blocks (``` ... ```)
    s = s.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
      const cls = lang ? ` class="language-${_escHtml(lang)}"` : "";
      return `<pre><code${cls}>${_escHtml(code.replace(/^\n|\n$/g, ""))}</code></pre>`;
    });

    // Split into blocks on blank lines, process each
    const blocks = s.split(/\n{2,}/);
    const out = blocks.map(block => {
      // Already an HTML block (from fenced code above) — pass through
      if (/^<(pre|ul|ol|blockquote|hr)/.test(block.trim())) return block;

      // Horizontal rule
      if (/^(\*{3,}|-{3,}|_{3,})\s*$/.test(block.trim())) return "<hr>";

      // Headings
      if (/^#{1,6} /.test(block)) {
        return block.replace(/^(#{1,6}) (.+)/gm, (_, hashes, content) => {
          const level = hashes.length;
          return `<h${level}>${_inlineMarkdown(content)}</h${level}>`;
        });
      }

      // Blockquote
      if (/^> /.test(block)) {
        const inner = block.replace(/^> ?/gm, "");
        return `<blockquote>${_inlineMarkdown(inner)}</blockquote>`;
      }

      // Unordered list
      if (/^[\*\-\+] /.test(block)) {
        const items = block
          .split("\n")
          .filter(l => /^[\*\-\+] /.test(l))
          .map(l => `<li>${_inlineMarkdown(l.replace(/^[\*\-\+] /, ""))}</li>`)
          .join("");
        return `<ul>${items}</ul>`;
      }

      // Ordered list
      if (/^\d+\. /.test(block)) {
        const items = block
          .split("\n")
          .filter(l => /^\d+\. /.test(l))
          .map(l => `<li>${_inlineMarkdown(l.replace(/^\d+\. /, ""))}</li>`)
          .join("");
        return `<ol>${items}</ol>`;
      }

      // Paragraph — preserve single newlines as <br>
      const para = _inlineMarkdown(block.trim()).replace(/\n/g, "<br>");
      return `<p>${para}</p>`;
    });

    return out.join("\n");
  }

  function _inlineMarkdown(s) {
    // Inline code (must run before bold/italic to avoid mangling backtick content)
    s = s.replace(/`([^`]+)`/g, (_, code) => `<code>${_escHtml(code)}</code>`);
    // Bold + italic  ***text***  or  ___text___
    s = s.replace(/(\*{3}|_{3})(.+?)\1/g, "<strong><em>$2</em></strong>");
    // Bold  **text**  or  __text__
    s = s.replace(/(\*{2}|_{2})(.+?)\1/g, "<strong>$2</strong>");
    // Italic  *text*  or  _text_
    s = s.replace(/(\*|_)(.+?)\1/g, "<em>$2</em>");
    // Links [text](url)
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    // Strikethrough ~~text~~
    s = s.replace(/~~(.+?)~~/g, "<del>$1</del>");
    return s;
  }

  async function sendMessage(overrideText) {
    const text = overrideText !== undefined ? overrideText : input.value.trim();
    if (!text || isBusy) return;

    dismissDqBar();
    input.value = "";
    isBusy = true;
    sendBtn.disabled = true;

    appendMsg("user", _escHtml(text));
    const bubble = appendMsg(
      "assistant",
      `<div class="uc-typing"><span></span><span></span><span></span></div>`
    );

    let messageToSend = text;
    let followupSuffix = "";
    if (settings.followup_enabled) {
      followupSuffix = buildFollowUpSuffix(settings.followup_count);
    }

    let fullText = "";
    let started  = false;
    let sources  = [];

    try {
      const res = await fetch(`${serverOrigin}/api/chat/${encodeURIComponent(slug)}`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ message: messageToSend, session_id: sessionId, reset: false, followup_suffix: followupSuffix }),
      });

      if (!res.ok) throw new Error(`Server error ${res.status}`);

      const reader  = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer    = "";

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
              const { mainText } = parseFollowUp(fullText);
              bubble.innerHTML = renderMarkdown(mainText);
              msgArea.scrollTop = msgArea.scrollHeight;
            }
            if (chunk.sources && chunk.sources.length > 0) sources = chunk.sources;
          } catch (_) {}
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
            if (chunk.sources && chunk.sources.length > 0) sources = chunk.sources;
          } catch (_) {}
        }
      }

      const { mainText, questions } = parseFollowUp(fullText);
      const citHtml = buildCitations(sources);
      bubble.innerHTML = renderMarkdown(mainText) + citHtml;

      if (settings.followup_enabled && questions.length > 0) {
        const chipsEl = document.createElement("div");
        chipsEl.className = "uc-followup-chips";

        const lbl = document.createElement("span");
        lbl.className = "uc-followup-label";
        lbl.textContent = "Follow-up:";
        chipsEl.appendChild(lbl);

        const row = document.createElement("div");
        row.className = "uc-followup-chips-row";
        questions.forEach(q => {
          const btn = document.createElement("button");
          btn.className = "uc-followup-chip";
          btn.textContent = q;
          btn.addEventListener("click", () => {
            chipsEl.remove();
            sendMessage(q);
          });
          row.appendChild(btn);
        });
        chipsEl.appendChild(row);
        msgArea.appendChild(chipsEl);
      }
    } catch (err) {
      bubble.className = "uc-msg error";
      bubble.innerHTML = `Error: ${_escHtml(err.message)}`;
    } finally {
      isBusy = false;
      sendBtn.disabled = false;
      input.focus();
      msgArea.scrollTop = msgArea.scrollHeight;
    }
  }

  // ── Reset ─────────────────────────────────────────────────────────────────
  async function resetChat() {
    try {
      await fetch(`${serverOrigin}/api/chat/${encodeURIComponent(slug)}`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ message: "", session_id: sessionId, reset: true }),
      });
    } catch (_) {}
    msgArea.innerHTML = "";
    dqDismissed = false;
    renderDqBar();
  }

  resetBtn.addEventListener("click", resetChat);

  // ── Keyboard ──────────────────────────────────────────────────────────────
  input.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  // ── Helpers ───────────────────────────────────────────────────────────────
  function _escHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function _uuid() {
    if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, c => {
      const r = (Math.random() * 16) | 0;
      return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
    });
  }

  // ── Init ──────────────────────────────────────────────────────────────────
  loadSettings();
})();
