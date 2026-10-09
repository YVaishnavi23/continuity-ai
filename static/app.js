/* Continuity: one simple input -> one chosen output -> quiet memory updates. */
const state = {
  projects: [],
  project: null,
  projectId: null,
  memories: [],
  runs: [],
  attachedFiles: [],
  outputType: null,
  currentResult: null,
  busy: false,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const outputLabels = {
  summary: "Summary",
  final_conclusion: "Final conclusion",
  project_status: "Project status",
};
const typeLabels = {
  goal: "Goal", decision: "Current approach / decision", progress: "Completed / approved",
  next_step: "Pending / next step", constraint: "Constraint", term: "Project term",
  question: "Needs clarification", other: "Project memory",
};
const acceptedExtensions = new Set([".txt", ".md", ".csv", ".json", ".pdf", ".docx", ".png", ".jpg", ".jpeg", ".webp", ".gif"]);
const maxFileBytes = 8 * 1024 * 1024;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[ch]);
}
function normalise(value) { return String(value ?? "").trim().toLowerCase().replace(/\s+/g, " "); }
function fileExtension(name) { const match = String(name).toLowerCase().match(/(\.[^.]+)$/); return match ? match[1] : ""; }

async function api(url, options = {}) {
  const config = { ...options, headers: { ...(options.headers || {}) } };
  if (config.body && !(config.body instanceof FormData) && typeof config.body !== "string") {
    config.headers["Content-Type"] = "application/json";
    config.body = JSON.stringify(config.body);
  }
  const response = await fetch(url, config);
  const kind = response.headers.get("content-type") || "";
  const payload = kind.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = payload && typeof payload === "object" ? payload.detail : payload;
    throw new Error(typeof detail === "string" ? detail : `Request failed (${response.status}).`);
  }
  return payload;
}

function showToast(message, kind = "info") {
  const root = $("#toast");
  if (!root) return;
  root.textContent = message;
  root.className = `toast toast-${kind}`;
  root.classList.remove("hidden");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => root.classList.add("hidden"), 6000);
}
function showDialog(id) {
  const dialog = document.getElementById(id);
  if (!dialog) return;
  if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", "open");
}
function closeDialog(id) {
  const dialog = document.getElementById(id);
  if (dialog?.open && typeof dialog.close === "function") dialog.close(); else dialog?.removeAttribute("open");
}

function renderMarkdown(markdown) {
  const safe = escapeHtml(markdown || "");
  const lines = safe.split(/\r?\n/);
  const output = [];
  let list = false;
  const closeList = () => { if (list) { output.push("</ul>"); list = false; } };
  const inline = text => text
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`(.+?)`/g, "<code>$1</code>");
  for (const line of lines) {
    if (/^###\s+/.test(line)) { closeList(); output.push(`<h3>${inline(line.replace(/^###\s+/, ""))}</h3>`); }
    else if (/^##\s+/.test(line)) { closeList(); output.push(`<h2>${inline(line.replace(/^##\s+/, ""))}</h2>`); }
    else if (/^#\s+/.test(line)) { closeList(); output.push(`<h2>${inline(line.replace(/^#\s+/, ""))}</h2>`); }
    else if (/^\s*[-*]\s+/.test(line)) {
      if (!list) { output.push("<ul>"); list = true; }
      output.push(`<li>${inline(line.replace(/^\s*[-*]\s+/, ""))}</li>`);
    } else if (!line.trim()) { closeList(); }
    else { closeList(); output.push(`<p>${inline(line)}</p>`); }
  }
  closeList();
  return output.join("") || "<p>No result was returned.</p>";
}

function setBusy(value) {
  state.busy = value;
  $("#processButton").disabled = value || !state.outputType;
  $("#processButton").innerHTML = value
    ? '<span>Processing…</span><span class="button-spinner"></span>'
    : '<span>Process & remember</span><span aria-hidden="true">↑</span>';
  $("#workingState").classList.toggle("hidden", !value);
}

function syncHeader() {
  const select = $("#projectSelect");
  const current = state.projectId == null ? "" : String(state.projectId);
  select.innerHTML = `<option value="">New project on first send</option>` + state.projects.map(p =>
    `<option value="${p.id}" ${String(p.id) === current ? "selected" : ""}>${escapeHtml(p.name)}</option>`).join("");
  $("#newProjectNameWrap").classList.toggle("hidden", Boolean(state.project));
  $("#memoryButton").disabled = !state.projectId;
  const saved = state.memories.filter(m => m.status === "approved").length;
  const pending = state.memories.filter(m => m.status === "candidate").length;
  const badge = $("#memoryCount");
  badge.textContent = String(pending);
  badge.classList.toggle("hidden", pending === 0);
  $("#memoryButton").title = `${saved} saved memories${pending ? `, ${pending} need input` : ""}`;
}

async function loadHealth() {
  try {
    const result = await api("/api/health");
    const badge = $("#aiStatus");
    if (result.llm_configured) {
      badge.textContent = "AI processing enabled";
      badge.classList.add("ai-on");
      $("#inputTip").textContent = "I'll interpret the material, update memory and ask only about important uncertainty.";
    } else {
      badge.textContent = "Basic local mode";
      badge.classList.remove("ai-on");
      $("#inputTip").textContent = "Text and documents work locally. Add an API key for AI interpretation and image understanding.";
    }
  } catch {
    $("#aiStatus").textContent = "Backend not connected";
  }
}

async function loadProjects(preferredId = null) {
  state.projects = await api("/api/projects");
  const stored = localStorage.getItem("continuity.projectId");
  const candidate = preferredId ?? stored;
  if (candidate && state.projects.some(p => Number(p.id) === Number(candidate))) {
    state.projectId = Number(candidate);
  } else {
    state.projectId = state.projects[0]?.id ?? null;
  }
  await loadActiveProject();
}

async function loadActiveProject() {
  if (!state.projectId) {
    state.project = null;
    state.memories = [];
    state.runs = [];
    state.currentResult = null;
    localStorage.removeItem("continuity.projectId");
    syncHeader();
    renderResult();
    return;
  }
  localStorage.setItem("continuity.projectId", String(state.projectId));
  const id = state.projectId;
  const [project, memories, runs] = await Promise.all([
    api(`/api/projects/${id}`),
    api(`/api/projects/${id}/memories`),
    api(`/api/projects/${id}/runs`),
  ]);
  if (Number(state.projectId) !== Number(id)) return;
  state.project = project;
  state.memories = memories;
  state.runs = runs;
  state.currentResult = runs.length ? {
    output: runs[0].output_content,
    output_type: runs[0].output_type,
    method: runs[0].method,
    memory: { saved: runs[0].auto_saved_count, needs_input: runs[0].needs_input_count, duplicates_skipped: 0 },
    warning: "",
    created_at: runs[0].created_at,
  } : null;
  syncHeader();
  renderResult();
  renderMemoryDialog();
}

function renderResult() {
  const result = state.currentResult;
  const section = $("#resultSection");
  const resume = $("#resumeSection");
  if (!result) {
    section.classList.add("hidden");
    resume.classList.toggle("hidden", !state.project);
    return;
  }
  section.classList.remove("hidden");
  resume.classList.toggle("hidden", !state.project);
  $("#resultTitle").textContent = outputLabels[result.output_type] || "Result";
  $("#resultMode").textContent = result.method === "openai" ? "AI-generated" : "Basic local mode";
  $("#resultContent").innerHTML = renderMarkdown(result.output);
  const memory = result.memory || { saved: result.auto_saved_count || 0, needs_input: result.needs_input_count || 0, duplicates_skipped: 0 };
  const saved = Number(memory.saved || 0);
  const pending = Number(memory.needs_input || 0);
  const skipped = Number(memory.duplicates_skipped || 0);
  $("#memoryUpdateTitle").textContent = saved ? `Project memory updated · ${saved} item${saved === 1 ? "" : "s"} saved` : "Project memory checked";
  const parts = [];
  parts.push(pending ? `${pending} item${pending === 1 ? "" : "s"} need${pending === 1 ? "s" : ""} your input` : "No unclear items need your attention");
  if (skipped) parts.push(`${skipped} duplicate${skipped === 1 ? "" : "s"} skipped`);
  $("#memoryUpdateDetail").textContent = parts.join(" · ");
  $("#reviewNeedsButton").classList.toggle("hidden", pending === 0);
  $("#processingWarning").textContent = result.warning || "";
  $("#processingWarning").classList.toggle("hidden", !result.warning);
}

function renderAttachmentList() {
  const root = $("#attachmentList");
  if (!state.attachedFiles.length) {
    root.innerHTML = "";
    root.classList.add("hidden");
    return;
  }
  root.classList.remove("hidden");
  root.innerHTML = state.attachedFiles.map((file, index) => `<span class="attachment-chip"><span aria-hidden="true">${file.type.startsWith("image/") ? "▧" : "▤"}</span><span class="attachment-chip-name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</span><span>${Math.max(1, Math.round(file.size / 1024))} KB</span><button class="attachment-remove" type="button" data-remove-file="${index}" aria-label="Remove ${escapeHtml(file.name)}">×</button></span>`).join("");
}

function addFiles(fileList) {
  const incoming = [...fileList];
  for (const file of incoming) {
    const ext = fileExtension(file.name);
    if (!acceptedExtensions.has(ext)) { showToast(`Unsupported file type: ${file.name}`, "error"); continue; }
    if (file.size > maxFileBytes) { showToast(`${file.name} is larger than 8 MB.`, "error"); continue; }
    if (state.attachedFiles.length >= 8) { showToast("You can attach up to 8 files at a time.", "error"); break; }
    if (!state.attachedFiles.some(existing => existing.name === file.name && existing.size === file.size)) state.attachedFiles.push(file);
  }
  renderAttachmentList();
}

function outputTitleFromMode(mode) { return outputLabels[mode] || "Project status"; }

async function processMaterial(event) {
  event.preventDefault();
  const text = $("#materialText").value.trim();
  if (!state.outputType) {
    showToast("Choose Summary, Final conclusion or Project status first.", "error");
    return;
  }
  if (!text && !state.attachedFiles.length) {
    showToast("Paste some text or attach a document/image first.", "error");
    return;
  }
  setBusy(true);
  try {
    const form = new FormData();
    form.append("text", text);
    form.append("output_type", state.outputType);
    if (state.projectId) form.append("project_id", String(state.projectId));
    else form.append("project_name", $("#firstProjectName").value.trim());
    state.attachedFiles.forEach(file => form.append("files", file, file.name));
    const result = await api("/api/process", { method: "POST", body: form });
    state.projectId = Number(result.project.id);
    state.project = result.project;
    localStorage.setItem("continuity.projectId", String(state.projectId));
    state.projects = await api("/api/projects");
    await loadActiveProject();
    state.currentResult = result;
    renderResult();
    $("#materialText").value = "";
    $("#firstProjectName").value = "";
    state.attachedFiles = [];
    $("#fileInput").value = "";
    renderAttachmentList();
    syncHeader();
    showToast("Done. Your result is ready and project memory has been checked.", "success");
    $("#resultSection").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showToast(error.message || "Couldn't process this material.", "error");
  } finally {
    setBusy(false);
  }
}

async function createProject(event) {
  event.preventDefault();
  const name = $("#projectName").value.trim();
  const goal = $("#projectGoal").value.trim();
  if (!name) return;
  try {
    const project = await api("/api/projects", { method: "POST", body: { name, goal } });
    closeDialog("projectDialog");
    $("#projectName").value = "";
    $("#projectGoal").value = "";
    state.projects = await api("/api/projects");
    await loadProjects(project.id);
    $("#materialText").focus();
    showToast(`Created ${project.name}. Add your conversation whenever you're ready.`, "success");
  } catch (error) { showToast(error.message, "error"); }
}

async function resumeFromMemory() {
  if (!state.projectId) return;
  setBusy(true);
  try {
    const result = await api(`/api/projects/${state.projectId}/resume-context`, { method: "POST", body: {} });
    state.currentResult = {
      output: result.content,
      output_type: "project_status",
      method: "memory",
      memory: { saved: 0, needs_input: state.memories.filter(m => m.status === "candidate").length, duplicates_skipped: 0 },
      warning: (result.warnings || []).join("\n"),
    };
    renderResult();
    $("#resultMode").textContent = "From saved memory";
    $("#resultSection").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) { showToast(error.message, "error"); }
  finally { setBusy(false); }
}

function renderMemoryDialog() {
  const root = $("#memoryDialogBody");
  const saved = state.memories.filter(m => m.status === "approved").slice(0, 30);
  const pending = state.memories.filter(m => m.status === "candidate").slice(0, 30);
  $("#memoryDialogTitle").textContent = state.project ? `${state.project.name} · Saved memory` : "Saved memory";
  if (!state.project) {
    root.innerHTML = '<div class="empty-memory">Choose or create a project to view its saved memory.</div>';
    return;
  }
  const goalHtml = state.project.goal ? `<article class="memory-item"><div class="memory-item-top"><span class="memory-kind">Project goal</span><span class="memory-status">Saved</span></div><p>${escapeHtml(state.project.goal)}</p></article>` : "";
  const savedHtml = saved.length ? saved.map(m => `
    <article class="memory-item">
      <div class="memory-item-top"><span class="memory-kind">${escapeHtml(typeLabels[m.memory_type] || "Memory")}</span><span class="memory-status">Saved</span></div>
      <p>${escapeHtml(m.content)}</p>
      ${m.rationale ? `<div class="memory-rationale">${escapeHtml(m.rationale)}</div>` : ""}
      <details class="memory-evidence"><summary>Source and details</summary><pre>${escapeHtml(`Source: ${m.source_title || "manual entry"}\nLast updated: ${m.updated_at || "unknown"}\n${m.source_quote ? `Excerpt: ${m.source_quote}` : "No exact text excerpt stored."}`)}</pre></details>
    </article>`).join("") : '<div class="empty-memory">Nothing has been saved yet. Process a conversation to build project memory.</div>';
  const pendingHtml = pending.length ? pending.map(m => {
    const conflict = m.conflict_status === "unresolved" && m.conflict_content;
    const controls = conflict
      ? `<div class="memory-item-actions"><button class="button button-light" data-memory-action="keep-existing" data-id="${m.id}">Keep current decision</button><button class="button button-primary" data-memory-action="accept-new" data-id="${m.id}">Use new decision</button><button class="button button-light" data-memory-action="later" data-id="${m.id}">Leave for later</button></div>`
      : `<div class="memory-item-actions"><button class="button button-primary" data-memory-action="keep" data-id="${m.id}">Keep in memory</button><button class="button button-light" data-memory-action="dismiss" data-id="${m.id}">Dismiss</button></div>`;
    return `<article class="memory-item"><div class="memory-item-top"><span class="memory-kind">${escapeHtml(typeLabels[m.memory_type] || "Memory")}</span><span class="memory-status pending">Needs input</span></div><p>${escapeHtml(m.content)}</p>${m.rationale ? `<div class="memory-rationale">${escapeHtml(m.rationale)}</div>` : ""}${conflict ? `<div class="memory-rationale"><strong>Current decision:</strong> ${escapeHtml(m.conflict_content)}</div>` : ""}<details class="memory-evidence"><summary>Source and details</summary><pre>${escapeHtml(`Source: ${m.source_title || "manual entry"}\nExcerpt: ${m.source_quote || "No exact source excerpt was found."}`)}</pre></details>${controls}<div class="memory-error hidden" id="memory-error-${m.id}"></div></article>`;
  }).join("") : '<div class="empty-memory">Nothing needs your input right now. Clear information is saved automatically.</div>';
  root.innerHTML = `<section class="memory-section"><div class="memory-section-heading"><h3>Current memory</h3><span>${saved.length} shown</span></div>${goalHtml}${savedHtml}</section><section class="memory-section"><div class="memory-section-heading"><h3>Only when something is unclear</h3><span>${pending.length} item${pending.length === 1 ? "" : "s"}</span></div>${pendingHtml}</section>`;
}

async function memoryAction(button) {
  const id = Number(button.dataset.id);
  const action = button.dataset.memoryAction;
  const row = button.closest(".memory-item");
  $$("button", row).forEach(b => b.disabled = true);
  try {
    if (action === "keep") await api(`/api/memories/${id}/approve`, { method: "POST", body: {} });
    else if (action === "dismiss") await api(`/api/memories/${id}/reject`, { method: "POST", body: { reason: "Dismissed from the simplified memory panel." } });
    else if (action === "keep-existing") await api(`/api/memories/${id}/resolve-conflict`, { method: "POST", body: { choice: "keep_existing", reason: "User kept the existing project decision." } });
    else if (action === "accept-new") await api(`/api/memories/${id}/resolve-conflict`, { method: "POST", body: { choice: "accept_new", reason: "User chose the new project decision." } });
    else if (action === "later") await api(`/api/memories/${id}/resolve-conflict`, { method: "POST", body: { choice: "leave_unresolved", reason: "User deferred this decision." } });
    await loadActiveProject();
    showToast(action === "dismiss" || action === "keep-existing" ? "Removed from current memory." : action === "later" ? "We'll leave this unresolved for now." : "Project memory updated.", "success");
  } catch (error) {
    $$("button", row).forEach(b => b.disabled = false);
    let err = $(`#memory-error-${id}`, row);
    if (!err) { err = document.createElement("div"); err.id = `memory-error-${id}`; err.className = "dialog-alert"; row.appendChild(err); }
    err.textContent = error.message;
    err.classList.remove("hidden");
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    showToast("Copied to clipboard.", "success");
  } catch {
    const area = document.createElement("textarea");
    area.value = text; document.body.appendChild(area); area.select();
    const ok = document.execCommand("copy"); area.remove();
    showToast(ok ? "Copied to clipboard." : "Copy failed. Select the result and copy it manually.", ok ? "success" : "error");
  }
}

function handleClick(event) {
  const output = event.target.closest("[data-output]");
  if (output) {
    state.outputType = output.dataset.output;
    $("#inputTip").textContent = state.outputType === "summary"
      ? "I'll pull out the key points and remove repetition."
      : state.outputType === "final_conclusion"
        ? "I'll identify the bottom line and any important caveats."
        : "I'll organize the goal, current approach, completed work and what remains.";
    $("#processButton").disabled = state.busy || !state.outputType;
    $$("[data-output]").forEach(button => {
      const selected = button.dataset.output === state.outputType;
      button.classList.toggle("selected", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
    return;
  }
  const remove = event.target.closest("[data-remove-file]");
  if (remove) {
    state.attachedFiles.splice(Number(remove.dataset.removeFile), 1);
    renderAttachmentList();
    return;
  }
  const close = event.target.closest("[data-close-dialog]");
  if (close) { closeDialog(close.dataset.closeDialog); return; }
  const memoryActionButton = event.target.closest("[data-memory-action]");
  if (memoryActionButton) { memoryAction(memoryActionButton); return; }
}

function bindEvents() {
  $("#processForm").addEventListener("submit", processMaterial);
  $("#projectForm").addEventListener("submit", createProject);
  $("#newProjectButton").addEventListener("click", () => showDialog("projectDialog"));
  $("#memoryButton").addEventListener("click", () => { renderMemoryDialog(); showDialog("memoryDialog"); });
  $("#attachButton").addEventListener("click", () => $("#fileInput").click());
  $("#fileInput").addEventListener("change", event => { addFiles(event.target.files); event.target.value = ""; });
  $("#projectSelect").addEventListener("change", async event => {
    state.projectId = event.target.value ? Number(event.target.value) : null;
    state.currentResult = null;
    try { await loadActiveProject(); } catch (error) { showToast(error.message, "error"); }
  });
  $("#resumeButton").addEventListener("click", resumeFromMemory);
  $("#copyResultButton").addEventListener("click", () => copyText(state.currentResult?.output || ""));
  $("#clearResultButton").addEventListener("click", () => { $("#resultSection").classList.add("hidden"); state.currentResult = null; });
  $("#reviewNeedsButton").addEventListener("click", () => { renderMemoryDialog(); showDialog("memoryDialog"); });
  document.addEventListener("click", handleClick);
  document.addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      if (!state.busy && document.activeElement === $("#materialText")) $("#processForm").requestSubmit();
    }
  });
  const drop = $("#dropZone");
  ["dragenter", "dragover"].forEach(name => drop.addEventListener(name, event => { event.preventDefault(); drop.classList.add("drag-active"); }));
  ["dragleave", "drop"].forEach(name => drop.addEventListener(name, event => { event.preventDefault(); drop.classList.remove("drag-active"); }));
  drop.addEventListener("drop", event => addFiles(event.dataTransfer.files));
  $("#materialText").addEventListener("paste", event => {
    const images = [...(event.clipboardData?.items || [])].filter(item => item.kind === "file" && item.type.startsWith("image/")).map(item => item.getAsFile()).filter(Boolean);
    if (images.length) addFiles(images.map((file, index) => new File([file], `pasted-image-${Date.now()}-${index}.png`, { type: file.type || "image/png" })));
  });
}

async function init() {
  bindEvents();
  await loadHealth();
  try { await loadProjects(); }
  catch (error) { showToast(`Couldn't connect to the backend: ${error.message}`, "error"); }
}

document.addEventListener("DOMContentLoaded", init);
