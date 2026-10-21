// ========== Settings API ==========

const MIN_COUNT = 1;
const MAX_COUNT = 5;

async function fetchWorkspace(slug) {
  const res = await fetch(`/api/workspaces/${slug}`);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`Failed to load settings (${res.status})`);
  return res.json();
}

async function ensureWorkspace(slug) {
  let ws = await fetchWorkspace(slug);
  if (!ws) {
    const res = await fetch(`/api/workspaces`, {
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
  const res = await fetch(`/api/workspaces/${slug}`, {
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
    default_questions: currentCategories,
  });
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
  window.location.href = `/${encodeURIComponent(WORKSPACE_SLUG)}`;
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

function buildQuestionRow(question, catIdx, qIdx) {
  const row = document.createElement("div");
  row.className = "dq-question-row";

  const dragHandle = document.createElement("span");
  dragHandle.className = "dq-drag-handle";
  dragHandle.title = "Drag to reorder";
  dragHandle.innerHTML = `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="5" r="1"/><circle cx="9" cy="12" r="1"/><circle cx="9" cy="19" r="1"/><circle cx="15" cy="5" r="1"/><circle cx="15" cy="12" r="1"/><circle cx="15" cy="19" r="1"/></svg>`;

  const qEditInput = document.createElement("input");
  qEditInput.type = "text";
  qEditInput.className = "dq-text-input dq-question-edit-input";
  qEditInput.value = question;
  qEditInput.maxLength = 120;
  qEditInput.addEventListener("input", (e) => {
    currentCategories[catIdx].questions[qIdx] = e.target.value;
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
  row.appendChild(removeBtn);
  return row;
}

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
      questions: cat.questions.filter(q => q.trim() !== "").map(q => q.trim()),
    }));

  const payload = {
    name: wsNameInput.value.trim() || WORKSPACE_SLUG,
    welcome_text: welcomeTextInput.value.trim() || "Send a message to get started.",
    followup_enabled: enabledCheckbox.checked,
    followup_count: currentCount,
    email_enabled: emailCheckbox.checked,
    default_questions: cleanedCategories,
  };

  saveBtn.disabled = true;
  try {
    await saveWorkspace(WORKSPACE_SLUG, payload);
    _savedClean = true;
    savedMsg.textContent = "Saved";
    savedMsg.classList.add("visible");
    setTimeout(() => {
      window.location.href = `/${encodeURIComponent(WORKSPACE_SLUG)}`;
    }, 700);
  } catch (err) {
    savedMsg.textContent = err.message;
    savedMsg.classList.add("visible");
    setTimeout(() => savedMsg.classList.remove("visible"), 3000);
    saveBtn.disabled = false;
  }
});
