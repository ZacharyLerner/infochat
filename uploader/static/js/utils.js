/* ========== Upload: Drop Zone + Auto-Submit ========== */

const dropZone = document.getElementById("dropZone");
const fileInput = document.getElementById("fileInput");
const uploadCounter = document.getElementById("uploadCounter");
const fileDisplay = document.querySelector(".file_display");
const uploadUrl = dropZone.dataset.url;
const MAX_UPLOAD_BYTES = parseInt(dropZone.dataset.maxBytes, 10) || Infinity;
const WORKSPACE_ID = document.getElementById("scrapeJobsList").dataset.workspaceId;

let isUploading = false;
let isScraping = false;

// Format bytes into a human-readable string
function formatBytes(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

// Warn before closing/navigating away during an active upload or scrape
window.addEventListener("beforeunload", (e) => {
    if (isUploading || isScraping) {
        e.preventDefault();
    }
});

// Click the zone to open file picker
dropZone.addEventListener("click", () => fileInput.click());

// Drag visual feedback
dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropZone.classList.add("dragover");
});
dropZone.addEventListener("dragleave", () => {
    dropZone.classList.remove("dragover");
});

// Drop files -> upload immediately
dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("dragover");
    if (e.dataTransfer.files.length) {
        uploadFiles(e.dataTransfer.files);
    }
});

// File picker selection -> upload immediately
fileInput.addEventListener("change", () => {
    if (fileInput.files.length) {
        uploadFiles(fileInput.files);
        fileInput.value = "";
    }
});

async function uploadFiles(files) {
    isUploading = true;
    const formData = new FormData();
    const totalFiles = files.length;
    let completedCount = 0;
    let failedCount = 0;

    const rows = {};
    const steps = { uploaded: 25, queued: 40, converted: 50, embedded: 75, done: 100 };
    const statusLabels = {
        uploaded: "Uploading...",
        queued: "Sending for conversion...",
        converted: "Converting...",
        embedded: "Embedding...",
        done: "Complete",
    };

    function esc(str) {
        const d = document.createElement("div");
        d.textContent = str;
        return d.innerHTML;
    }

    function updateCounter() {
        let text = `Uploading ${totalFiles} file${totalFiles > 1 ? "s" : ""} \u2014 ${completedCount}/${totalFiles} complete`;
        if (failedCount > 0) text += ` (${failedCount} failed)`;
        uploadCounter.textContent = text;
    }

    updateCounter();

    function markRowError(row, message) {
        row.classList.remove("uploading");
        row.classList.add("upload-error");
        row.querySelector(".file-info .date").textContent = message || "Upload failed";
        const indicator = row.querySelector(".upload-indicator");
        if (indicator) indicator.remove();
        failedCount++;
        updateCounter();
    }

    let validCount = 0;

    for (const file of files) {
        const row = document.createElement("div");
        row.className = "file_card uploading";
        row.innerHTML = `
            <div class="file-info">
                <div class="filename">${esc(file.name)}</div>
                <div class="date">Waiting...</div>
            </div>
            <div class="upload-indicator">
                <div class="progress-bar-track">
                    <div class="progress-bar-fill"></div>
                </div>
                <span class="progress-label">0%</span>
            </div>
        `;
        fileDisplay.prepend(row);
        rows[file.name] = row;

        if (file.size > MAX_UPLOAD_BYTES) {
            const limitStr = formatBytes(MAX_UPLOAD_BYTES);
            const sizeStr = formatBytes(file.size);
            markRowError(row, `File is ${sizeStr} \u2014 exceeds ${limitStr} limit`);
        } else {
            formData.append("uploaded_files", file);
            validCount++;
        }
    }

    fileDisplay.closest(".section-body").scrollTop = 0;

    if (validCount === 0) {
        uploadCounter.textContent = `${failedCount} file${failedCount > 1 ? "s" : ""} rejected (too large)`;
        setTimeout(() => { uploadCounter.textContent = ""; }, 4000);
        isUploading = false;
        return;
    }

    const res = await fetch(uploadUrl, { method: "POST", body: formData });

    if (!res.ok) {
        for (const [name, row] of Object.entries(rows)) {
            if (!row.classList.contains("upload-error")) {
                markRowError(row, "Upload failed");
            }
        }
        uploadCounter.textContent = "Upload failed";
        setTimeout(() => { uploadCounter.textContent = ""; }, 3000);
        isUploading = false;
        return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();

        for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            const payload = line.slice(6).trim();
            if (!payload) continue;

            if (payload === "[DONE]") {
                let summary = `${completedCount} file${completedCount !== 1 ? "s" : ""} uploaded`;
                if (failedCount > 0) summary += `, ${failedCount} failed`;
                uploadCounter.textContent = summary;
                setTimeout(() => { uploadCounter.textContent = ""; }, 3000);
                rebuildCheckboxes();
                refreshFilterBar();
                isUploading = false;
                return;
            }

            try {
                const event = JSON.parse(payload);
                const row = rows[event.file];
                if (!row) continue;

                if (event.status === "error") {
                    markRowError(row, event.message || "Processing failed");
                } else if (event.status === "done") {
                    completedCount++;
                    updateCounter();
                    if (event.converting) {
                        // Uploaded and queued: keep the progress row until conversion finishes
                        row.dataset.key = event.key;
                        row.dataset.filename = event.name;
                        row.dataset.extension = event.original_extension || "";
                        row.dataset.status = "converting";
                        setConversionProgress(row, "converting");
                        pollConverting();
                    } else {
                        transformToCompleted(row, event);
                    }
                } else {
                    const fill = row.querySelector(".progress-bar-fill");
                    const label = row.querySelector(".progress-label");
                    const dateEl = row.querySelector(".file-info .date");
                    const pct = steps[event.status] || 0;
                    fill.style.width = pct + "%";
                    label.textContent = pct + "%";
                    dateEl.textContent = statusLabels[event.status] || event.status;
                }
            } catch (e) {
                console.warn("Skipping unparseable payload:", payload);
            }
        }
    }
}

function transformToCompleted(row, event) {
    const fileId = event.location;
    const displayName = event.name;
    const ext = (event.original_extension || "").replace(".", "");

    function esc(str) {
        const d = document.createElement("div");
        d.textContent = str;
        return d.innerHTML;
    }

    const now = new Date();
    const dateStr = now.toLocaleString("en-US", {
        month: "short", day: "numeric", year: "numeric",
    });

    row.className = "file_card just-completed";
    row.setAttribute("data-extension", event.original_extension || "");
    row.setAttribute("data-filename", displayName);
    row.setAttribute("data-date", now.toISOString());
    row.setAttribute("data-key", event.key || "");
    row.setAttribute("data-status", "ready");
    row.innerHTML = `
        <input type="checkbox" class="file-checkbox" data-id="${esc(fileId)}">
        <div class="file-info">
            <div class="filename">${esc(displayName)}</div>
            <div class="date">${dateStr}</div>
        </div>
        <button class="btn-delete" data-id="${esc(fileId)}">Delete</button>
    `;

    attachDeleteHandler(row.querySelector(".btn-delete"));
    applyCurrentFilter();

    row.addEventListener("animationend", () => {
        row.classList.remove("just-completed");
    }, { once: true });
}

/* ========== Conversion status ========== */

// Non-text uploads are converted by docling-pipeline in the background. Their rows keep the
// upload progress bar, moved along by the server's file record: converting (50%) ->
// embedding (75%) -> ready (the normal row) or failed (row with the error).
const CONVERSION_STEPS = { converting: 50, embedding: 75 };
let convertingPoll = null;

function convertingLabel(ext) {
    // PDFs (OCR on scans, layout and table models) are where conversion takes minutes
    return (ext || "").toLowerCase() === ".pdf"
        ? "Converting… PDFs and scanned documents can take a few minutes."
        : "Converting…";
}

function setConversionProgress(row, status) {
    const pct = CONVERSION_STEPS[status];
    row.querySelector(".progress-bar-fill").style.width = pct + "%";
    row.querySelector(".progress-label").textContent = pct + "%";
    row.querySelector(".file-info .date").textContent =
        status === "embedding" ? "Embedding…" : convertingLabel(row.dataset.extension);
}

function pollConverting() {
    if (convertingPoll) return;
    convertingPoll = setInterval(async () => {
        const rows = document.querySelectorAll(
            '.file_card[data-status="converting"], .file_card[data-status="embedding"]');
        if (!rows.length) {
            clearInterval(convertingPoll);
            convertingPoll = null;
            return;
        }
        let files;
        try {
            const res = await fetch(`${BASE}/api/v1/workspaces/${WORKSPACE_ID}`);
            if (!res.ok) return;
            files = (await res.json()).files;
        } catch (e) {
            return;
        }
        const byKey = Object.fromEntries(files.map(f => [f.key, f]));
        rows.forEach(row => {
            const f = byKey[row.dataset.key];
            if (!f || f.status === row.dataset.status) return;
            if (f.status in CONVERSION_STEPS) {
                row.dataset.status = f.status;
                setConversionProgress(row, f.status);
                return;
            }
            transformToCompleted(row, {
                location: f.id || f.key, key: f.key, name: f.filename,
                original_extension: f.original_extension,
            });
            if (f.status === "failed") {
                row.dataset.status = "failed";
                const dateEl = row.querySelector(".file-info .date");
                dateEl.textContent = "Conversion failed: " + (f.error || "unknown error");
                dateEl.title = f.error || "";
                dateEl.classList.add("file-error");
            }
        });
    }, 5000);
}

pollConverting();

/* ========== Delete Handlers ========== */

function attachDeleteHandler(btn) {
    btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm("Delete this document?")) return;

        const res = await fetch(`${BASE}/${WORKSPACE_ID}/delete/${btn.dataset.id}`, { method: "DELETE" });

        if (res.ok) {
            const card = btn.closest(".file_card");
            card.remove();
            rebuildCheckboxes();
            updateSelection();
            refreshFilterBar();
        }
    });
}

document.querySelectorAll(".file_display .btn-delete").forEach(attachDeleteHandler);

/* Heading shows how many files are listed; rows come and go from several places, so watch the list */
new MutationObserver(() => {
    const n = fileDisplay.querySelectorAll(".file_card").length;
    document.getElementById("fileCount").textContent = `${n} file${n === 1 ? "" : "s"}`;
}).observe(fileDisplay, { childList: true });

/* ========== Checkboxes: Shift-Click Multi-Select ========== */

const bulkActions = document.getElementById("bulkActions");
const selectedCountEl = document.getElementById("selectedCount");
let checkboxes = Array.from(fileDisplay.querySelectorAll(".file-checkbox"));
let lastCheckedIndex = null;

function rebuildCheckboxes() {
    checkboxes = Array.from(fileDisplay.querySelectorAll(".file-checkbox"));
    lastCheckedIndex = null;

    checkboxes.forEach((cb, index) => {
        const newCb = cb.cloneNode(true);
        cb.parentNode.replaceChild(newCb, cb);
        checkboxes[index] = newCb;

        newCb.addEventListener("click", (e) => {
            if (e.shiftKey && lastCheckedIndex !== null) {
                const start = Math.min(lastCheckedIndex, index);
                const end = Math.max(lastCheckedIndex, index);
                const checked = newCb.checked;
                for (let i = start; i <= end; i++) checkboxes[i].checked = checked;
            }
            lastCheckedIndex = index;
            updateSelection();
        });
    });
}

function updateSelection() {
    const selected = checkboxes.filter((cb) => cb.checked);
    checkboxes.forEach((cb) => {
        cb.closest(".file_card").classList.toggle("selected", cb.checked);
    });
    if (selected.length > 0) {
        bulkActions.classList.add("visible");
        selectedCountEl.textContent = selected.length;
    } else {
        bulkActions.classList.remove("visible");
    }
    syncSelectAllCheckbox();
}

rebuildCheckboxes();

/* ========== Select All ========== */

const selectAllCb = document.getElementById("selectAllCb");

selectAllCb.addEventListener("change", () => {
    const isChecked = selectAllCb.checked;
    checkboxes.forEach((cb) => {
        const card = cb.closest(".file_card");
        if (!card.classList.contains("filtered-out")) cb.checked = isChecked;
    });
    updateSelection();
});

function syncSelectAllCheckbox() {
    const visible = checkboxes.filter((cb) => !cb.closest(".file_card").classList.contains("filtered-out"));
    if (visible.length === 0) {
        selectAllCb.checked = false;
        selectAllCb.indeterminate = false;
        return;
    }
    const checkedCount = visible.filter((cb) => cb.checked).length;
    if (checkedCount === 0) {
        selectAllCb.checked = false;
        selectAllCb.indeterminate = false;
    } else if (checkedCount === visible.length) {
        selectAllCb.checked = true;
        selectAllCb.indeterminate = false;
    } else {
        selectAllCb.checked = false;
        selectAllCb.indeterminate = true;
    }
}

/* ========== Bulk Delete ========== */

const deleteOverlay = document.getElementById("deleteOverlay");
const deleteOverlayTitle = document.getElementById("deleteOverlayTitle");
const deleteOverlayMsg = document.getElementById("deleteOverlayMsg");
const bulkDeleteBtn = document.getElementById("bulkDeleteBtn");

bulkDeleteBtn.addEventListener("click", async () => {
    const selected = checkboxes.filter((cb) => cb.checked);
    const fileIds = selected.map((cb) => cb.dataset.id);
    if (!fileIds.length) return;

    const count = fileIds.length;
    if (!confirm(`Delete ${count} document${count > 1 ? "s" : ""}?`)) return;

    bulkDeleteBtn.disabled = true;
    bulkDeleteBtn.innerHTML = '<span class="btn-spinner"></span>Deleting...';
    deleteOverlayTitle.textContent = `Deleting ${count} file${count > 1 ? "s" : ""}...`;
    deleteOverlayMsg.textContent = count >= 5
        ? "This may take a moment for larger selections."
        : "Removing from workspace...";
    deleteOverlay.classList.add("visible");

    const res = await fetch(`${BASE}/${WORKSPACE_ID}/delete-bulk`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file_ids: fileIds }),
    });

    if (res.ok) {
        const result = await res.json();
        const deletedIds = new Set(result.deleted || []);
        selected.forEach((cb) => {
            if (deletedIds.has(cb.dataset.id)) cb.closest(".file_card").remove();
        });
        rebuildCheckboxes();
        updateSelection();
        refreshFilterBar();
    } else {
        alert("Failed to delete selected documents.");
    }

    bulkDeleteBtn.disabled = false;
    bulkDeleteBtn.textContent = "Delete selected";
    deleteOverlay.classList.remove("visible");
});

/* ==========================================================================
   WEBSITES — Add
   One box takes one address or several. One address can include the pages it
   links to; those are found first so the count can be confirmed.
   ========================================================================== */

const scrapeArea = document.getElementById("scrapeArea");
const scrapeBtn = document.getElementById("scrapeBtn");
const scrapeUrlInput = document.getElementById("scrapeUrl");
const scrapeLinksOpt = document.getElementById("scrapeLinksOpt");
const scrapeLinksCb = document.getElementById("scrapeLinks");
const scrapeLinksText = document.getElementById("scrapeLinksText");
const scrapeMsg = document.getElementById("scrapeMsg");
const scrapeJobsList = document.getElementById("scrapeJobsList");

const discoverUrl = scrapeArea.dataset.discoverUrl;
const jobsUrl = scrapeArea.dataset.jobsUrl;

// shortcut: following links has no page limit by choice; 10000 only stops a runaway crawl
const LINKS_MAX_PAGES = 10000;

// Escape HTML to prevent XSS
function escHtml(str) {
    const d = document.createElement("div");
    d.textContent = str;
    return d.innerHTML;
}

const plural = (n, word, many = word + "s") => `${n} ${n === 1 ? word : many}`;

// People type "its.uri.edu/help"; the backend wants a full URL
const withScheme = (raw) => (/^https?:\/\//i.test(raw) ? raw : "https://" + raw);

// The address as a URL, or null while it is not yet one
function parsedUrl(raw) {
    try {
        const u = new URL(withScheme(raw));
        return u.hostname.includes(".") ? u : null;
    } catch (_) {
        return null;
    }
}

// Rough client-side split; the server does the real parsing of a list
function entries() {
    const seen = new Set();
    return scrapeUrlInput.value
        .split(/[\s,]+/)
        .map((t) => t.trim().replace(/#.*$/, ""))
        .filter((t) => t && !/^(\d+[.)]|[-*•])$/.test(t) && !seen.has(t.toLowerCase()) && seen.add(t.toLowerCase()));
}

const shortUrl = (u) => u.hostname.replace(/^www\./, "") + u.pathname;

// The saved website's name, renamed later: "its.uri.edu – zoom", or "its.uri.edu list"
function suggestedName(list) {
    const u = parsedUrl(list[0] || "");
    if (!u) return "Untitled";
    const host = u.hostname.replace(/^www\./, "");
    if (list.length > 1) return `${host} list`;
    const last = u.pathname.split("/").filter(Boolean).pop();
    return last ? `${host} – ${last}` : host;
}

function setMsg(html, isError = false) {
    scrapeMsg.innerHTML = html;
    scrapeMsg.classList.toggle("is-error", isError);
    // While the found pages wait for confirmation, its button is the only one to press
    scrapeBtn.disabled = !!scrapeMsg.querySelector("#scrapeConfirmBtn");
}

function updateForm() {
    const list = entries();
    const u = list.length === 1 ? parsedUrl(list[0]) : null;
    // Links are followed from one address
    scrapeLinksOpt.hidden = list.length > 1;
    scrapeLinksText.textContent = u ? `Include the pages ${shortUrl(u)} links to` : "Include the pages it links to";
    scrapeBtn.textContent = list.length > 1 ? `Add ${list.length} pages` : "Add";
    // Grow with the pasted lines, up to the CSS max-height
    scrapeUrlInput.style.height = "auto";
    scrapeUrlInput.style.height = scrapeUrlInput.scrollHeight + 2 + "px";
}

scrapeUrlInput.addEventListener("input", () => { updateForm(); setMsg(""); });
scrapeLinksCb.addEventListener("change", () => setMsg(""));
// Enter adds; Shift+Enter starts a new line
scrapeUrlInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); scrapeBtn.click(); }
});

function busy(btn, label) {
    btn.disabled = true;
    btn.innerHTML = `<span class="btn-spinner"></span> ${label}&hellip;`;
}

scrapeBtn.addEventListener("click", async () => {
    const list = entries();
    if (!list.length || (list.length === 1 && !parsedUrl(list[0]))) {
        setMsg("Enter a web address, such as its.uri.edu/help/", true);
        scrapeUrlInput.focus();
        return;
    }
    const name = suggestedName(list);

    if (list.length > 1) {
        await saveAndRun({ mode: "list", urls: scrapeUrlInput.value, name, schedule_interval: null });
        return;
    }
    const base_url = withScheme(list[0]);
    if (!scrapeLinksCb.checked) {
        await saveAndRun({ mode: "single", base_url, max_depth: 0, name, schedule_interval: null });
        return;
    }

    // Linked pages can be many: find them first and let the person confirm the count.
    // One level deep, on the same site.
    const crawl = { mode: "depth", base_url, max_depth: 1, max_pages: LINKS_MAX_PAGES, allow_offsite: false };
    busy(scrapeBtn, "Finding pages");
    try {
        const res = await fetch(discoverUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(crawl),
        });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Server error ${res.status}`);
        const n = ((await res.json()).urls || []).length;
        const where = escHtml(shortUrl(parsedUrl(list[0])));
        if (!n) {
            setMsg(`No pages that can be added were found at ${where}.`, true);
        } else {
            setMsg(`Found ${plural(n, "page")}: ${where} and the pages it links to.
                <button type="button" class="btn-scrape-submit" id="scrapeConfirmBtn">Add ${plural(n, "page")}</button>
                <button type="button" class="btn-text" id="scrapeCancelBtn">Cancel</button>`);
            document.getElementById("scrapeCancelBtn").addEventListener("click", () => setMsg(""));
            const confirmBtn = document.getElementById("scrapeConfirmBtn");
            confirmBtn.addEventListener("click", () => saveAndRun({ ...crawl, name, schedule_interval: null }, confirmBtn));
            confirmBtn.focus();
        }
    } catch (err) {
        setMsg("Could not look for pages: " + escHtml(err.message), true);
    } finally {
        updateForm();   // restores the button label
        scrapeBtn.disabled = !!scrapeMsg.querySelector("#scrapeConfirmBtn");
    }
});

// Save the website, add its row, then run it there
async function saveAndRun(jobPayload, btn = scrapeBtn) {
    isScraping = true;
    busy(btn, "Adding");
    let job;
    try {
        const res = await fetch(jobsUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(jobPayload),
        });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Server error ${res.status}`);
        job = await res.json();
    } catch (err) {
        isScraping = false;
        btn.disabled = false;
        btn.textContent = btn === scrapeBtn ? "Add" : "Try again";
        setMsg("Could not add: " + escHtml(err.message), true);
        return;
    }

    scrapeUrlInput.value = "";
    scrapeLinksCb.checked = false;
    scrapeBtn.disabled = false;
    updateForm();
    setMsg("");
    isScraping = false;
    const row = insertJobRow(job, true);
    row.scrollIntoView({ block: "nearest", behavior: "smooth" });
    row.classList.add("site-just-added");
    row.addEventListener("animationend", () => row.classList.remove("site-just-added"), { once: true });
    await runJobById(job.id);
}

updateForm();

/* ==========================================================================
   SAVED WEBSITES
   A row per website: name, page count (failures in red), last update.
   Opening it shows its actions and pages.
   ========================================================================== */

// Exact time, for tooltips
function formatJobDate(isoStr) {
    if (!isoStr) return "Never";
    return new Date(isoStr).toLocaleString("en-US", {
        month: "short", day: "numeric", year: "numeric",
        hour: "numeric", minute: "2-digit",
    });
}

// The date as a file row shows it: "Nov 16, 2026"
const formatDay = (isoStr) => new Date(isoStr).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });

const SCHEDULES = [["", "never"], ["daily", "every day"], ["weekly", "every week"], ["monthly", "every month"]];
// Websites set to hourly before it was dropped keep it, so their menu doesn't show the wrong choice
const scheduleOptions = (current) => (current === "hourly" ? [...SCHEDULES, ["hourly", "every hour"]] : SCHEDULES);

// With none, a line saying what goes here
function showEmptyIfNone() {
    const n = scrapeJobsList.querySelectorAll(".site-row").length;
    if (!n) scrapeJobsList.innerHTML = '<p class="site-empty">No websites yet. Pages you add here can be searched in chat.</p>';
}

function insertJobRow(job, startRunning = false) {
    scrapeJobsList.querySelector(".site-empty")?.remove();

    const id = escHtml(job.id);
    const row = document.createElement("div");
    row.className = "site-row" + (startRunning ? " job-running" : "");
    row.dataset.jobId = job.id;
    row.dataset.url = `${BASE}/${WORKSPACE_ID}/scrape/jobs/${job.id}`;
    row.innerHTML = `
        <div class="site-head">
            <button type="button" class="site-toggle" aria-expanded="false" aria-controls="site-${id}" aria-label="Show pages">
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="chevron-icon" aria-hidden="true"><polyline points="9 6 15 12 9 18"/></svg>
            </button>
            <div class="file-info">
                <div class="filename site-name"></div>
                <div class="date site-status"></div>
            </div>
            <button type="button" class="btn-delete btn-job-delete">Delete</button>
        </div>
        <div class="site-detail" id="site-${id}" hidden>
            <div class="site-actions">
                <button type="button" class="btn-job-run">Update now</button>
                <label class="site-schedule">Updates
                    <select class="scrape-job-schedule-select">${scheduleOptions(job.schedule_interval).map(([v, l]) =>
                        `<option value="${v}"${(job.schedule_interval || "") === v ? " selected" : ""}>${l}</option>`).join("")}</select>
                </label>
                <button type="button" class="btn-text btn-job-rename">Rename</button>
            </div>
            <div class="site-pages"></div>
        </div>
    `;

    scrapeJobsList.prepend(row);
    showEmptyIfNone();
    updateJobRow(row, job);
    attachJobRowHandlers(row);
    return row;
}

function updateJobRow(row, job) {
    const nameEl = row.querySelector(".site-name");
    nameEl.textContent = job.name;
    nameEl.title = job.mode === "list" ? (job.urls || []).join("\n") : job.base_url || "";

    const total = job.page_count || 0;
    const failed = job.failed_count || 0;
    const converting = job.converting_count || 0;
    // Where a file shows its date: page count, failures in red, then the last update
    const statusEl = row.querySelector(".site-status");
    statusEl.innerHTML = job.is_running || converting
        ? `<span class="job-running-spinner" aria-hidden="true"></span> ${converting ? `Adding ${plural(converting, "page")}` : "Updating"}&hellip;`
        : [total ? plural(total, "page") : "No pages",
           failed ? `<span class="is-failed">${failed} failed</span>` : "",
           job.last_scraped_at ? escHtml(formatDay(job.last_scraped_at)) : ""].filter(Boolean).join(" &middot; ");
    statusEl.title = [job.last_scraped_at && `Updated ${formatJobDate(job.last_scraped_at)}`,
                      job.schedule_interval && job.next_scrape_at && `Next update ${formatJobDate(job.next_scrape_at)}`].filter(Boolean).join("\n");

    row.classList.toggle("job-converting", !!converting);
    row.classList.toggle("job-running", !!job.is_running);
    if (job.is_running || converting) startPollingIfNeeded();
}

function attachJobRowHandlers(row) {
    // The whole row opens it, like clicking a file row; the arrow is the keyboard control
    const toggle = row.querySelector(".site-toggle");
    row.querySelector(".site-head").addEventListener("click", (e) => {
        if (e.target.closest(".btn-job-delete")) return;
        if (toggle.getAttribute("aria-expanded") === "true") {
            toggle.setAttribute("aria-expanded", "false");
            row.querySelector(".site-detail").hidden = true;
        } else {
            openJobPages(row);
        }
    });

    row.querySelector(".btn-job-run").addEventListener("click", () => runJobById(row.dataset.jobId));

    row.querySelector(".scrape-job-schedule-select").addEventListener("change", async (e) => {
        try {
            const res = await fetch(row.dataset.url, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ schedule_interval: e.target.value || null }),
            });
            if (!res.ok) throw new Error(`Server error ${res.status}`);
            updateJobRow(row, await res.json());
        } catch (err) {
            alert("Could not change how often this website updates: " + err.message);
        }
    });

    // Rename: the name is only a label, so a prompt is enough
    row.querySelector(".btn-job-rename").addEventListener("click", async () => {
        const current = row.querySelector(".site-name").textContent;
        const name = prompt("New name for this website:", current)?.trim();
        if (!name || name === current) return;
        try {
            const res = await fetch(row.dataset.url, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name }),
            });
            if (!res.ok) throw new Error(`Server error ${res.status}`);
            updateJobRow(row, await res.json());
        } catch (err) {
            alert("Could not rename: " + err.message);
        }
    });

    row.querySelector(".btn-job-delete").addEventListener("click", async () => {
        if (!confirm("Delete this website? The chat will stop using its pages.")) return;
        deleteOverlayTitle.textContent = "Removing website...";
        deleteOverlayMsg.textContent = "Removing its pages from the chat...";
        deleteOverlay.classList.add("visible");
        try {
            const res = await fetch(row.dataset.url, { method: "DELETE" });
            if (!res.ok) throw new Error(`Server error ${res.status}`);
            row.remove();
            showEmptyIfNone();
        } catch (err) {
            alert("Could not remove this website: " + err.message);
        } finally {
            deleteOverlay.classList.remove("visible");
        }
    });
}

/* ---------- Pages of a website ---------- */

const PAGE_SEARCH_MIN = 15;   // a search box earns its place only on a long list

// Open the row and (re)load its pages
async function openJobPages(row) {
    row.querySelector(".site-toggle").setAttribute("aria-expanded", "true");
    row.querySelector(".site-detail").hidden = false;
    const listEl = row.querySelector(".site-pages");
    if (!listEl.childElementCount) listEl.innerHTML = '<p class="site-note">Loading pages&hellip;</p>';
    try {
        const res = await fetch(`${row.dataset.url}/pages`);
        if (!res.ok) throw new Error(`Server error ${res.status}`);
        renderJobPages(listEl, await res.json(), row);
    } catch (e) {
        listEl.innerHTML = `<p class="site-note">Could not load pages: ${escHtml(e.message)}</p>`;
    }
}

function renderJobPages(listEl, pages, row) {
    if (!pages.length) {
        listEl.innerHTML = '<p class="site-note">No pages yet. Use <strong>Update now</strong> to add them.</p>';
        return;
    }
    const searchable = pages.length >= PAGE_SEARCH_MIN;
    listEl.innerHTML = `
        ${searchable ? '<input type="search" class="site-search" placeholder="Search pages" aria-label="Search pages">' : ""}
        <ul class="site-page-list"></ul>
    `;
    const ul = listEl.querySelector(".site-page-list");

    function pageItem(page) {
        const li = document.createElement("li");
        const failed = page.status === "failed";
        const pending = page.status === "converting" || page.status === "embedding";
        li.className = "site-page" + (failed ? " is-failed" : "");
        const url = page.source_url || page.filename;
        li.innerHTML = `
            <a class="site-page-url" href="${escHtml(url)}" target="_blank" rel="noopener noreferrer" title="${escHtml(url)}">${escHtml(url.replace(/^https?:\/\/(www\.)?/, ""))}</a>
            <span class="site-page-when">${pending ? "adding&hellip;" : ""}</span>
            <button type="button" class="btn-delete btn-page-remove">Delete</button>
            ${failed ? `
                <p class="site-page-error">${escHtml(page.error || "Unknown error")}</p>
                <p class="site-page-fixes">
                    <button type="button" class="btn-text btn-page-retry">Try again</button>
                    <button type="button" class="btn-text btn-page-upload" title="Save the page from your browser (File › Save As…), then upload that file">Upload a file instead</button>
                </p>` : ""}
        `;
        li.querySelector(".btn-page-retry")?.addEventListener("click", () => runJobById(row.dataset.jobId));
        li.querySelector(".btn-page-upload")?.addEventListener("click", () => {
            dropZone.scrollIntoView({ block: "center", behavior: "smooth" });
            fileInput.click();
        });
        li.querySelector(".btn-page-remove").addEventListener("click", async (e) => {
            if (!confirm("Delete this page? The chat will stop using it.")) return;
            e.currentTarget.disabled = true;
            const res = await fetch(`${BASE}/${WORKSPACE_ID}/delete/${encodeURIComponent(page.id)}`, { method: "DELETE" });
            if (!res.ok) {
                alert("Could not remove this page.");
                e.currentTarget.disabled = false;
                return;
            }
            pages.splice(pages.indexOf(page), 1);
            renderJobPages(listEl, pages, row);
            refreshJobRow(row);
        });
        return li;
    }

    function render(q = "") {
        q = q.toLowerCase();
        const shown = q ? pages.filter((p) => (p.source_url || p.filename || "").toLowerCase().includes(q)) : pages;
        ul.replaceChildren(...shown.map(pageItem));   // failed pages come first from the server
        if (!shown.length) ul.innerHTML = '<li class="site-note">No matching pages.</li>';
    }
    listEl.querySelector(".site-search")?.addEventListener("input", (e) => render(e.target.value));
    render();
}

/* ---------- Run a website's update (streamed) ---------- */

async function runJobById(jobId) {
    const row = scrapeJobsList.querySelector(`[data-job-id="${CSS.escape(jobId)}"]`);
    if (!row) return;
    const status = row.querySelector(".site-status");
    const runBtn = row.querySelector(".btn-job-run");
    const say = (text) => { status.innerHTML = `<span class="job-running-spinner" aria-hidden="true"></span> ${escHtml(text)}`; };

    row.classList.add("job-running");
    runBtn.disabled = true;
    say("Finding pages…");

    try {
        const res = await fetch(`${row.dataset.url}/run`, { method: "POST" });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Server error ${res.status}`);

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let total = 0;
        let done = 0;

        while (true) {
            const { done: finished, value } = await reader.read();
            if (finished) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split("\n");
            buffer = lines.pop();

            for (const line of lines) {
                if (!line.startsWith("data: ")) continue;
                const payload = line.slice(6).trim();
                if (!payload || payload === "[DONE]") continue;
                try {
                    const event = JSON.parse(payload);
                    if (event.status === "discovered") {
                        total = event.count || 0;
                        say(`Checking ${plural(total, "page")}…`);
                    } else if (["new", "changed", "unchanged", "removed", "queued", "error"].includes(event.status) && event.url) {
                        done++;
                        if (total) say(`Checking ${done} of ${total}…`);
                    } else if (event.status === "error") {
                        say("Update failed: " + (event.message || "unknown error"));
                    }
                } catch (e) {
                    console.warn("Skipping unparseable job event:", payload);
                }
            }
        }
    } catch (err) {
        alert("Could not update this website: " + err.message);
    } finally {
        row.classList.remove("job-running");
        runBtn.disabled = false;
        // Counts, failures and the open page list come from the server
        await refreshJobRow(row);
        if (row.querySelector(".site-toggle").getAttribute("aria-expanded") === "true") openJobPages(row);
    }
}

async function refreshJobRow(row) {
    try {
        const res = await fetch(`${BASE}/${WORKSPACE_ID}/scrape/jobs`);
        if (!res.ok) return;
        const job = (await res.json()).find((j) => j.id === row.dataset.jobId);
        if (job) updateJobRow(row, job);
    } catch (e) {
        // Non-critical — ignore
    }
}

/* ---------- Background job polling ---------- */
// While any website is running (a scheduler run) or has pages still converting,
// poll every 5 seconds so its status catches up.

let jobPoll = null;

function startPollingIfNeeded() {
    if (jobPoll) return;
    if (!scrapeJobsList.querySelector(".site-row.job-running, .site-row.job-converting")) return;

    jobPoll = setInterval(async () => {
        try {
            const res = await fetch(`${BASE}/${WORKSPACE_ID}/scrape/jobs`);
            if (!res.ok) return;
            const jobMap = Object.fromEntries((await res.json()).map((j) => [j.id, j]));
            let busyAny = false;
            scrapeJobsList.querySelectorAll(".site-row").forEach((row) => {
                const job = jobMap[row.dataset.jobId];
                if (!job) return;
                const wasBusy = row.classList.contains("job-converting");
                updateJobRow(row, job);
                if (job.is_running || job.converting_count) busyAny = true;
                // Its pages finished: an open list shows their new state
                else if (wasBusy && row.querySelector(".site-toggle").getAttribute("aria-expanded") === "true") openJobPages(row);
            });
            if (!busyAny) { clearInterval(jobPoll); jobPoll = null; }
        } catch (e) {
            // ignore
        }
    }, 5000);
}

// Draw the saved websites, newest first (each row is prepended)
[...SCRAPE_JOBS].reverse().forEach((job) => insertJobRow(job));
showEmptyIfNone();

/* ========== Upload Search Bar ========== */

let uploadSearchTerm = "";
const uploadSearchBar = document.getElementById("uploadSearchBar");
if (uploadSearchBar) {
    uploadSearchBar.addEventListener("input", () => {
        uploadSearchTerm = uploadSearchBar.value.trim().toLowerCase();
        applyCurrentFilter();
    });
}

/* ========== Extension Filter Bar ========== */

let activeFilter = "all";

function refreshFilterBar() {
    const cards = Array.from(fileDisplay.querySelectorAll(".file_card:not(.uploading):not(.upload-error)"));
    const extSet = new Set();
    cards.forEach((card) => {
        const ext = card.dataset.extension;
        if (ext) extSet.add(ext);
    });

    const filterBar = document.getElementById("filterBar");
    const exts = Array.from(extSet).sort();

    filterBar.innerHTML = "";

    const allPill = document.createElement("button");
    allPill.className = "filter-pill" + (activeFilter === "all" ? " active" : "");
    allPill.dataset.ext = "all";
    allPill.textContent = "All";
    filterBar.appendChild(allPill);

    exts.forEach((ext) => {
        const pill = document.createElement("button");
        pill.className = "filter-pill" + (activeFilter === ext ? " active" : "");
        pill.dataset.ext = ext;
        pill.textContent = ext.replace(".", "").toUpperCase();
        filterBar.appendChild(pill);
    });

    attachFilterHandlers();
}

function attachFilterHandlers() {
    document.querySelectorAll("#filterBar .filter-pill").forEach((pill) => {
        pill.addEventListener("click", () => {
            activeFilter = pill.dataset.ext;
            document.querySelectorAll("#filterBar .filter-pill").forEach((p) => p.classList.remove("active"));
            pill.classList.add("active");
            applyCurrentFilter();
        });
    });
}

function applyCurrentFilter() {
    const cards = fileDisplay.querySelectorAll(".file_card");
    cards.forEach((card) => {
        const ext = card.dataset.extension || "";
        const filename = (card.dataset.filename || "").toLowerCase();
        const matchesExt = activeFilter === "all" || ext === activeFilter;
        const matchesSearch = !uploadSearchTerm || filename.includes(uploadSearchTerm);

        if (matchesExt && matchesSearch) {
            card.classList.remove("filtered-out");
        } else {
            card.classList.add("filtered-out");
        }
    });

    checkboxes.forEach((cb) => {
        const card = cb.closest(".file_card");
        if (card.classList.contains("filtered-out")) cb.checked = false;
    });

    updateSelection();
}

attachFilterHandlers();

/* ========== Sort ========== */

const sortSelect = document.getElementById("sortSelect");

sortSelect.addEventListener("change", () => {
    applySort(sortSelect.value);
});

function applySort(mode) {
    const cards = Array.from(fileDisplay.querySelectorAll(".file_card:not(.uploading):not(.upload-error)"));

    cards.sort((a, b) => {
        switch (mode) {
            case "az": return (a.dataset.filename || "").toLowerCase().localeCompare((b.dataset.filename || "").toLowerCase());
            case "za": return (b.dataset.filename || "").toLowerCase().localeCompare((a.dataset.filename || "").toLowerCase());
            case "newest": return (b.dataset.date || "").localeCompare(a.dataset.date || "");
            case "oldest": return (a.dataset.date || "").localeCompare(b.dataset.date || "");
            case "ext": {
                const cmp = (a.dataset.extension || "").toLowerCase().localeCompare((b.dataset.extension || "").toLowerCase());
                if (cmp !== 0) return cmp;
                return (a.dataset.filename || "").toLowerCase().localeCompare((b.dataset.filename || "").toLowerCase());
            }
            default: return 0;
        }
    });

    cards.forEach((card) => fileDisplay.appendChild(card));
    rebuildCheckboxes();
}

