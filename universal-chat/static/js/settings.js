// ========== Settings API ==========

const MIN_COUNT = 1;
const MAX_COUNT = 5;

async function fetchWorkspace(slug) {
  const res = await fetch(`${BASE}/api/workspaces/${slug}`);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`Failed to load settings (${res.status})`);
  return res.json();
}

async function ensureWorkspace(slug) {
  let ws = await fetchWorkspace(slug);
  if (!ws) {
    const res = await fetch(`${BASE}/api/workspaces`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug, name: slug, welcome_text: "Send a message to get started.", followup_enabled: false, followup_count: 3, default_questions: [] }),
    });
    if (!res.ok) throw new Error(`Failed to create workspace (${res.status})`);
    ws = await res.json();
  }
  return ws;
}

async function saveWorkspace(slug, payload) {
  const res = await fetch(`${BASE}/api/workspaces/${slug}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Failed to save settings (${res.status})`);
  return res.json();
}

// ========== Page Init ==========

const enabledCheckbox  = document.getElementById("followup-enabled");
const emailCheckbox    = document.getElementById("email-enabled");
const countDisplay     = document.getElementById("count-display");
const countRow         = document.getElementById("followup-count-row");
const decrementBtn     = document.getElementById("count-decrement");
const incrementBtn     = document.getElementById("count-increment");
const saveBtn          = document.getElementById("save-btn");
const savedMsg         = document.getElementById("saved-msg");
const categoriesList   = document.getElementById("dq-categories-list");
const newCategoryInput = document.getElementById("dq-new-category-input");
const addCategoryBtn   = document.getElementById("dq-add-category-btn");
const wsNameInput      = document.getElementById("workspace-name-input");
const slugDisplay      = document.getElementById("settings-slug-display");
const welcomeTextInput = document.getElementById("welcome-text-input");
const cannedDelayInput = document.getElementById("canned-delay-input");
const backBtn          = document.getElementById("back-btn");
const modal            = document.getElementById("unsaved-modal");
const modalStayBtn     = document.getElementById("modal-stay-btn");
const modalLeaveBtn    = document.getElementById("modal-leave-btn");

let currentCount = 3;
let currentCategories = [];

// ========== Dirty tracking ==========

let _savedSnapshot = null;
let _savedClean = false;

function _snapshot() {
  return JSON.stringify({
    name: wsNameInput.value.trim(),
    welcome_text: welcomeTextInput.value.trim(),
    followup_enabled: enabledCheckbox.checked,
    followup_count: currentCount,
    email_enabled: emailCheckbox.checked,
    canned_delay_seconds: cannedDelaySeconds(),
    default_questions: currentCategories,
  });
}

function cannedDelaySeconds() {
  return Math.min(30, Math.max(0, Number(cannedDelayInput.value) || 0));
}

function isDirty() {
  if (_savedClean) return false;
  if (_savedSnapshot === null) {
    // Settings haven't loaded yet — check if the user has typed anything at all
    return wsNameInput.value.trim() !== "" ||
           welcomeTextInput.value.trim() !== "" ||
           enabledCheckbox.checked ||
           emailCheckbox.checked ||
           currentCategories.length > 0;
  }
  return _snapshot() !== _savedSnapshot;
}

function markClean() {
  _savedSnapshot = _snapshot();
  _savedClean = false; // reset so future edits are tracked again
}

function applyToUI(ws) {
  wsNameInput.value = ws.name || "";
  if (slugDisplay) slugDisplay.textContent = ws.slug;
  welcomeTextInput.value = ws.welcome_text || "";
  enabledCheckbox.checked = ws.followup_enabled;
  currentCount = ws.followup_count;
  countDisplay.textContent = currentCount;
  countRow.classList.toggle("disabled", !ws.followup_enabled);
  decrementBtn.disabled = !ws.followup_enabled || currentCount <= MIN_COUNT;
  incrementBtn.disabled = !ws.followup_enabled || currentCount >= MAX_COUNT;
  emailCheckbox.checked = ws.email_enabled || false;
  cannedDelayInput.value = ws.canned_delay_seconds ?? 1;
  currentCategories = JSON.parse(JSON.stringify(ws.default_questions || []));
  renderAllCategories();
  markClean();
}

// Load on page init
if (!WORKSPACE_SLUG) {
  savedMsg.textContent = "No workspace slug provided.";
  savedMsg.classList.add("visible");
  saveBtn.disabled = true;
} else {
  ensureWorkspace(WORKSPACE_SLUG)
    .then(applyToUI)
    .catch(err => {
      savedMsg.textContent = err.message;
      savedMsg.classList.add("visible");
    });
}

// ========== Unsaved-changes guard ==========

function navigateBack() {
  window.location.href = `${BASE}/${encodeURIComponent(WORKSPACE_SLUG)}`;
}

function showModal() {
  modal.setAttribute("aria-hidden", "false");
  modal.classList.add("modal-visible");
  modalLeaveBtn.focus();
}

function hideModal() {
  modal.setAttribute("aria-hidden", "true");
  modal.classList.remove("modal-visible");
}

backBtn.addEventListener("click", () => {
  if (isDirty()) {
    showModal();
  } else {
    navigateBack();
  }
});

modalStayBtn.addEventListener("click", hideModal);

modalLeaveBtn.addEventListener("click", () => {
  hideModal();
  navigateBack();
});

modal.addEventListener("click", (e) => {
  if (e.target === modal) hideModal();
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && modal.classList.contains("modal-visible")) hideModal();
});

window.addEventListener("beforeunload", (e) => {
  if (isDirty()) {
    e.preventDefault();
    e.returnValue = "";
  }
});

// ========== Toggle ==========

enabledCheckbox.addEventListener("change", () => {
  const enabled = enabledCheckbox.checked;
  countRow.classList.toggle("disabled", !enabled);
  decrementBtn.disabled = !enabled || currentCount <= MIN_COUNT;
  incrementBtn.disabled = !enabled || currentCount >= MAX_COUNT;
});

// ========== Count Picker ==========

decrementBtn.addEventListener("click", () => {
  if (currentCount > MIN_COUNT) {
    currentCount--;
    countDisplay.textContent = currentCount;
    decrementBtn.disabled = currentCount <= MIN_COUNT;
    incrementBtn.disabled = false;
  }
});

incrementBtn.addEventListener("click", () => {
  if (currentCount < MAX_COUNT) {
    currentCount++;
    countDisplay.textContent = currentCount;
    incrementBtn.disabled = currentCount >= MAX_COUNT;
    decrementBtn.disabled = false;
  }
});

// ========== Default Questions Editor ==========

function renderAllCategories() {
  categoriesList.innerHTML = "";
  if (currentCategories.length === 0) {
    const empty = document.createElement("p");
    empty.className = "dq-empty-state";
    empty.textContent = "No categories yet. Add one below.";
    categoriesList.appendChild(empty);
    return;
  }
  currentCategories.forEach((cat, catIdx) => {
    categoriesList.appendChild(buildCategoryBlock(cat, catIdx));
  });
}

function buildCategoryBlock(cat, catIdx) {
  const block = document.createElement("div");
  block.className = "dq-category-block";

  const header = document.createElement("div");
  header.className = "dq-category-header";

  const nameWrap = document.createElement("div");
  nameWrap.className = "dq-category-name-wrap";

  const chevron = document.createElement("span");
  chevron.className = "dq-chevron rotated";
  chevron.innerHTML = `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>`;

  const catNameInput = document.createElement("input");
  catNameInput.type = "text";
  catNameInput.className = "dq-category-name-input";
  catNameInput.value = cat.category;
  catNameInput.maxLength = 40;
  catNameInput.addEventListener("input", (e) => {
    currentCategories[catIdx].category = e.target.value;
  });

  nameWrap.appendChild(chevron);
  nameWrap.appendChild(catNameInput);

  const deleteBtn = document.createElement("button");
  deleteBtn.className = "dq-delete-btn dq-delete-category-btn";
  deleteBtn.title = "Delete category";
  deleteBtn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>`;
  deleteBtn.addEventListener("click", () => {
    currentCategories.splice(catIdx, 1);
    renderAllCategories();
  });

  header.appendChild(nameWrap);
  header.appendChild(deleteBtn);
  block.appendChild(header);

  const body = document.createElement("div");
  body.className = "dq-category-body";

  cat.questions.forEach((q, qIdx) => {
    body.appendChild(buildQuestionRow(q, catIdx, qIdx));
  });

  const addRow = document.createElement("div");
  addRow.className = "dq-add-question-row";

  const qInput = document.createElement("input");
  qInput.type = "text";
  qInput.className = "dq-text-input dq-question-input";
  qInput.placeholder = "Add a question...";
  qInput.maxLength = 120;

  const addQBtn = document.createElement("button");
  addQBtn.className = "dq-add-question-btn";
  addQBtn.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg> Add`;

  const doAdd = () => {
    const val = qInput.value.trim();
    if (!val) return;
    currentCategories[catIdx].questions.push(val);
    qInput.value = "";
    renderAllCategories();
  };

  addQBtn.addEventListener("click", doAdd);
  qInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); doAdd(); }
  });

  addRow.appendChild(qInput);
  addRow.appendChild(addQBtn);
  body.appendChild(addRow);

  let collapsed = false;
  nameWrap.addEventListener("click", (e) => {
    if (e.target === catNameInput) return;
    collapsed = !collapsed;
    body.classList.toggle("collapsed", collapsed);
    chevron.classList.toggle("rotated", !collapsed);
  });

  block.appendChild(body);
  return block;
}

// A question is a plain string (asks the LLM) or { question, answer } (canned answer)
function buildQuestionRow(question, catIdx, qIdx) {
  const canned = typeof question === "string" ? null : question;
  const wrap = document.createElement("div");
  const row = document.createElement("div");
  row.className = "dq-question-row";
  wrap.appendChild(row);

  const dragHandle = document.createElement("span");
  dragHandle.className = "dq-drag-handle";
  dragHandle.title = "Drag to reorder";
  dragHandle.innerHTML = `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="5" r="1"/><circle cx="9" cy="12" r="1"/><circle cx="9" cy="19" r="1"/><circle cx="15" cy="5" r="1"/><circle cx="15" cy="12" r="1"/><circle cx="15" cy="19" r="1"/></svg>`;

  const qEditInput = document.createElement("input");
  qEditInput.type = "text";
  qEditInput.className = "dq-text-input dq-question-edit-input";
  qEditInput.value = canned ? canned.question : question;
  qEditInput.maxLength = 120;
  qEditInput.addEventListener("input", (e) => {
    if (canned) canned.question = e.target.value;
    else currentCategories[catIdx].questions[qIdx] = e.target.value;
  });

  const addAnswerBtn = document.createElement("button");
  addAnswerBtn.className = "dq-add-question-btn";
  addAnswerBtn.title = "Reply with a fixed answer instead of asking the AI";
  addAnswerBtn.innerHTML = `${PLUS_ICON} Add answer`;
  addAnswerBtn.addEventListener("click", () => {
    const obj = { question: qEditInput.value, answer: "" };
    currentCategories[catIdx].questions[qIdx] = obj;
    answerDrafts.set(obj, "");
    renderAllCategories();
  });

  const removeBtn = document.createElement("button");
  removeBtn.className = "dq-delete-btn dq-delete-question-btn";
  removeBtn.title = "Remove question";
  removeBtn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;
  removeBtn.addEventListener("click", () => {
    currentCategories[catIdx].questions.splice(qIdx, 1);
    renderAllCategories();
  });

  row.appendChild(dragHandle);
  row.appendChild(qEditInput);
  if (!canned) row.appendChild(addAnswerBtn);
  row.appendChild(removeBtn);

  if (canned) wrap.appendChild(answerDrafts.has(canned) ? buildAnswerEditor(canned, catIdx, qIdx) : buildAnswerCard(canned, catIdx, qIdx));
  return wrap;
}

// Open answer editors, keyed by the question object; the draft only reaches the data on Save
const answerDrafts = new WeakMap();
const PLUS_ICON = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>`;

function revertToQuestion(canned, catIdx, qIdx) {
  currentCategories[catIdx].questions[qIdx] = canned.question;
  answerDrafts.delete(canned);
  renderAllCategories();
}

function buildAnswerEditor(canned, catIdx, qIdx) {
  const panel = document.createElement("div");
  panel.className = "dq-answer-panel";

  const answerInput = document.createElement("textarea");
  answerInput.className = "dq-answer-input";
  answerInput.placeholder = "Answer shown when this question is clicked (Markdown supported)";
  answerInput.maxLength = 4000;
  answerInput.rows = 4;
  answerInput.value = answerDrafts.get(canned);

  const actions = document.createElement("div");
  actions.className = "dq-answer-actions";

  const cancelBtn = document.createElement("button");
  cancelBtn.className = "dq-answer-btn";
  cancelBtn.textContent = "Cancel";
  cancelBtn.addEventListener("click", () => {
    if (!canned.answer) return revertToQuestion(canned, catIdx, qIdx);
    answerDrafts.delete(canned);
    renderAllCategories();
  });

  const saveAnswerBtn = document.createElement("button");
  saveAnswerBtn.className = "dq-answer-btn dq-answer-btn-primary";
  saveAnswerBtn.textContent = "Save answer";
  const syncSave = () => { saveAnswerBtn.disabled = answerInput.value.trim() === ""; };
  syncSave();
  const doSave = () => {
    if (saveAnswerBtn.disabled) return;
    canned.answer = answerInput.value.trim();
    answerDrafts.delete(canned);
    renderAllCategories();
  };
  answerInput.addEventListener("input", () => { answerDrafts.set(canned, answerInput.value); syncSave(); });
  answerInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); doSave(); }
  });
  saveAnswerBtn.addEventListener("click", doSave);

  actions.appendChild(cancelBtn);
  actions.appendChild(saveAnswerBtn);
  panel.appendChild(answerInput);
  panel.appendChild(actions);
  requestAnimationFrame(() => answerInput.focus());
  return panel;
}

function buildAnswerCard(canned, catIdx, qIdx) {
  const card = document.createElement("div");
  card.className = "dq-answer-card";

  const label = document.createElement("span");
  label.className = "dq-answer-label";
  label.textContent = "Fixed answer";

  // Rendered the way the chat page shows it
  const text = document.createElement("div");
  text.className = "dq-answer-text";
  text.innerHTML = marked.parse(canned.answer);

  const editBtn = document.createElement("button");
  editBtn.className = "dq-answer-icon-btn";
  editBtn.title = "Edit answer";
  editBtn.setAttribute("aria-label", "Edit answer");
  editBtn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>`;
  editBtn.addEventListener("click", () => {
    answerDrafts.set(canned, canned.answer);
    renderAllCategories();
  });

  const deleteBtn = document.createElement("button");
  deleteBtn.className = "dq-delete-btn";
  deleteBtn.title = "Delete answer";
  deleteBtn.setAttribute("aria-label", "Delete answer");
  deleteBtn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>`;
  deleteBtn.addEventListener("click", () => {
    confirmDeleteAnswer(canned.question, () => revertToQuestion(canned, catIdx, qIdx));
  });

  const head = document.createElement("div");
  head.className = "dq-answer-card-head";
  head.appendChild(label);
  head.appendChild(editBtn);
  head.appendChild(deleteBtn);

  card.appendChild(head);
  card.appendChild(text);
  return card;
}

// ---- Delete-answer confirmation ----
const deleteAnswerModal = document.getElementById("delete-answer-modal");
const deleteAnswerName  = document.getElementById("delete-answer-question");
let _onConfirmDeleteAnswer = null;

function confirmDeleteAnswer(question, onConfirm) {
  _onConfirmDeleteAnswer = onConfirm;
  deleteAnswerName.textContent = question;
  deleteAnswerModal.setAttribute("aria-hidden", "false");
  deleteAnswerModal.classList.add("modal-visible");
  document.getElementById("delete-answer-cancel-btn").focus();
}

function hideDeleteAnswerModal() {
  _onConfirmDeleteAnswer = null;
  deleteAnswerModal.setAttribute("aria-hidden", "true");
  deleteAnswerModal.classList.remove("modal-visible");
}

document.getElementById("delete-answer-cancel-btn").addEventListener("click", hideDeleteAnswerModal);
document.getElementById("delete-answer-confirm-btn").addEventListener("click", () => {
  const onConfirm = _onConfirmDeleteAnswer;
  hideDeleteAnswerModal();
  onConfirm?.();
});
deleteAnswerModal.addEventListener("click", (e) => {
  if (e.target === deleteAnswerModal) hideDeleteAnswerModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && deleteAnswerModal.classList.contains("modal-visible")) hideDeleteAnswerModal();
});

// ---- Add category ----
const doAddCategory = () => {
  const name = newCategoryInput.value.trim();
  if (!name) return;
  currentCategories.push({ category: name, questions: [] });
  newCategoryInput.value = "";
  renderAllCategories();
  categoriesList.lastElementChild?.scrollIntoView({ behavior: "smooth", block: "start" });
};

addCategoryBtn.addEventListener("click", doAddCategory);
newCategoryInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); doAddCategory(); }
});

// ========== Save ==========

saveBtn.addEventListener("click", async () => {
  const cleanedCategories = currentCategories
    .filter(cat => cat.category.trim() !== "")
    .map(cat => ({
      category: cat.category.trim(),
      questions: cat.questions
        .map(q => {
          if (typeof q === "string") return q.trim();
          const question = q.question.trim();
          const answer = q.answer.trim();
          // An empty answer falls back to a normal LLM question
          return answer ? { question, answer } : question;
        })
        .filter(q => (typeof q === "string" ? q : q.question) !== ""),
    }));

  const payload = {
    name: wsNameInput.value.trim() || WORKSPACE_SLUG,
    welcome_text: welcomeTextInput.value.trim() || "Send a message to get started.",
    followup_enabled: enabledCheckbox.checked,
    followup_count: currentCount,
    email_enabled: emailCheckbox.checked,
    canned_delay_seconds: cannedDelaySeconds(),
    default_questions: cleanedCategories,
  };

  saveBtn.disabled = true;
  try {
    await saveWorkspace(WORKSPACE_SLUG, payload);
    _savedClean = true;
    savedMsg.textContent = "Saved";
    savedMsg.classList.add("visible");
    setTimeout(() => {
      window.location.href = `${BASE}/${encodeURIComponent(WORKSPACE_SLUG)}`;
    }, 700);
  } catch (err) {
    savedMsg.textContent = err.message;
    savedMsg.classList.add("visible");
    setTimeout(() => savedMsg.classList.remove("visible"), 3000);
    saveBtn.disabled = false;
  }
});
