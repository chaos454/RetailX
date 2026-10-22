const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const content = $("#page-content");
const state = {
  page: "overview", settings: null, runtime: null, summary: null,
  productPage: 0, productSearch: "", productCategory: "", productStock: "all",
  alertStatus: "active", alertSource: "all", activeJob: null, renderToken: 0,
  reportStart: new Date().toISOString().slice(0, 10), reportEnd: new Date().toISOString().slice(0, 10),
};
const pageNames = { overview: "Overview", shopper: "Shopper analytics", queue: "Queue management", inventory: "Inventory", alerts: "Alerts", reports: "Reports", settings: "Settings" };

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}
function dateLabel(value, includeTime = true) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return esc(value);
  return new Intl.DateTimeFormat(undefined, includeTime ? { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" } : { month: "short", day: "numeric", year: "numeric" }).format(date);
}
function money(value) { return new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 2 }).format(Number(value || 0)); }
function number(value) { return new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 }).format(Number(value || 0)); }
function empty(text) { return `<div class="empty-inline">${esc(text)}</div>`; }
function statusPill(severity, text) { return `<span class="status-pill ${esc(severity || "neutral")}">${esc(text)}</span>`; }
function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`; el.textContent = message;
  $("#toast-region").append(el);
  setTimeout(() => el.remove(), 3800);
}
function privacyNoticeDismissed() {
  try { return localStorage.getItem("retailx-privacy-notice-dismissed") === "1"; }
  catch { return false; }
}
function dismissPrivacyNotice() {
  try { localStorage.setItem("retailx-privacy-notice-dismissed", "1"); } catch {}
  $("#privacy-notice").hidden = true;
  if ($("#privacy-dialog").open) $("#privacy-dialog").close();
}
$("#privacy-notice").hidden = privacyNoticeDismissed();
$("#privacy-dismiss").addEventListener("click", dismissPrivacyNotice);
$$('[data-action="privacy-preferences"]').forEach(button => button.addEventListener("click", () => $("#privacy-dialog").showModal()));
$$('[data-action="close-privacy"]').forEach(button => button.addEventListener("click", () => $("#privacy-dialog").close()));
$$('[data-action="dismiss-privacy"]').forEach(button => button.addEventListener("click", dismissPrivacyNotice));
async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !(options.body instanceof Blob) && typeof options.body !== "string") {
    headers["Content-Type"] = "application/json"; options.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, { ...options, headers });
  const type = response.headers.get("content-type") || "";
  const payload = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) throw new Error(payload?.error || payload || `Request failed (${response.status})`);
  return payload;
}
function setTheme(theme, persist = true) {
  const selected = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = selected;
  $("#theme-icon").textContent = selected === "dark" ? "☼" : "☾";
  document.querySelector('meta[name="theme-color"]').content = selected === "dark" ? "#101114" : "#F4EFE6";
  if (persist) api("/api/settings", { method: "POST", body: { theme: selected } }).then(s => state.settings = s).catch(() => {});
}
function shellHeader(page) {
  state.page = page;
  $("#current-section").textContent = pageNames[page] || "Overview";
  $$(".nav-item[data-page]").forEach(button => {
    const active = button.dataset.page === page;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  });
  $("#sidebar").classList.remove("open");
  $("#mobile-menu").setAttribute("aria-expanded", "false");
}
function pageHeader(kicker, title, description, actions = "") {
  return `<div class="page-header"><div><div class="eyebrow">${esc(kicker)}</div><h1>${esc(title)}</h1><p>${esc(description)}</p></div><div class="header-actions">${actions}</div></div>`;
}
function metric(label, value, foot, icon, footClass = "") {
  return `<article class="metric-card"><div class="metric-top"><span>${esc(label)}</span><span class="metric-icon" aria-hidden="true">${icon}</span></div><strong class="metric-value">${value}</strong><div class="metric-foot ${footClass}">${foot}</div></article>`;
}
function alertRows(items) {
  if (!items?.length) return empty("No active alerts. Conditions will appear here when thresholds are crossed.");
  return items.map(item => `<div class="alert-row"><span class="alert-bullet ${esc(item.severity)}"></span><div class="alert-copy"><div class="alert-title">${esc(item.title)}</div><div class="alert-desc">${esc(item.description)}</div><div class="alert-time">${dateLabel(item.last_seen)}</div></div>${item.status === "active" ? `<button class="button button-small" data-action="resolve-alert" data-id="${esc(item.id)}">Resolve</button>` : ""}</div>`).join("");
}
function drawTrafficChart(points) {
  const canvas = $("#traffic-chart");
  if (!canvas) return;
  const chartSummary = points?.length
    ? `${points.length} sampled frames. Peak ${number(Math.max(...points.map(point => Number(point.people_count))))} people detected in one frame. Counts are not unique visitors.`
    : "No sampled frame observations are available.";
  canvas.setAttribute("aria-label", chartSummary);
  const summaryNode = $("#traffic-summary"); if (summaryNode) summaryNode.textContent = chartSummary;
  const box = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(box.width * dpr)); canvas.height = Math.max(1, Math.floor(box.height * dpr));
  const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr);
  const w = box.width, h = box.height;
  const cs = getComputedStyle(document.documentElement);
  const muted = cs.getPropertyValue("--faint").trim(), border = cs.getPropertyValue("--border").trim(), accent = cs.getPropertyValue("--accent").trim();
  ctx.clearRect(0, 0, w, h); ctx.font = "10px Consolas, monospace"; ctx.fillStyle = muted;
  if (!points?.length) return;
  const values = points.map(p => Number(p.people_count));
  const max = Math.max(1, ...values) * 1.2;
  const left = 32, right = 8, top = 12, bottom = 25;
  const plotW = w - left - right, plotH = h - top - bottom;
  ctx.strokeStyle = border; ctx.lineWidth = 1;
  for (let n = 0; n <= 3; n++) {
    const y = top + plotH * n / 3; ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(w - right, y); ctx.stroke();
    ctx.fillText(String(Math.round(max * (1 - n / 3))), 0, y + 3);
  }
  const xy = values.map((v, i) => ({ x: left + (values.length === 1 ? plotW / 2 : i * plotW / (values.length - 1)), y: top + plotH * (1 - v / max) }));
  const grad = ctx.createLinearGradient(0, top, 0, h - bottom); grad.addColorStop(0, accent + "44"); grad.addColorStop(1, accent + "00");
  ctx.beginPath(); xy.forEach((p, i) => i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y));
  ctx.lineTo(xy.at(-1).x, h - bottom); ctx.lineTo(xy[0].x, h - bottom); ctx.closePath(); ctx.fillStyle = grad; ctx.fill();
  ctx.beginPath(); xy.forEach((p, i) => i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)); ctx.strokeStyle = accent; ctx.lineWidth = 2; ctx.stroke();
  xy.forEach(p => { ctx.beginPath(); ctx.arc(p.x, p.y, 3, 0, Math.PI * 2); ctx.fillStyle = accent; ctx.fill(); });
  ctx.fillStyle = muted;
  points.forEach((point, i) => { if (i % Math.ceil(points.length / 6) === 0) { const seconds = Math.floor(point.timestamp_seconds); const label = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`; ctx.fillText(label, left + (points.length === 1 ? plotW / 2 : i * plotW / (points.length - 1)) - 14, h - 5); } });
}

async function renderOverview() {
  const [summary, runtime] = await Promise.all([api("/api/summary"), api("/api/runtime")]);
  state.summary = summary; state.runtime = runtime;
  const queueText = summary.queue_estimate == null ? "—" : number(summary.queue_estimate);
  const queueFoot = summary.queue_observed_at ? `Last observed ${dateLabel(summary.queue_observed_at)}` : "No video observations yet";
  const lastUpdate = summary.latest_job ? `${esc(summary.latest_job.status)} · ${dateLabel(summary.latest_job.updated_at)}` : "No processing jobs recorded";
  content.innerHTML = `${pageHeader("STORE PULSE", "Good to see you.", `Your operations snapshot for ${new Date().toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" })}.`, `<button class="button" data-page="reports">View daily report <span aria-hidden="true">↗</span></button><button class="button button-primary" data-page="inventory">＋ Add product</button>`)}
    <div class="metric-grid grid">
      ${metric("Products tracked", number(summary.products_total), summary.products_total ? "Current saved inventory" : "No inventory has been added", "▤")}
      ${metric("Low stock", number(summary.low_stock), summary.low_stock ? "At or below reorder threshold" : "No low-stock products", "⌁", summary.low_stock ? "bad" : "")}
      ${metric("Queue estimate", queueText, queueFoot, "≋", summary.queue_estimate != null && summary.queue_estimate > summary.queue_threshold ? "bad" : "")}
      ${metric("Active alerts", number(summary.active_alerts), summary.active_alerts ? `${number(summary.out_of_stock)} out of stock · review needed` : "No active conditions", "♢", summary.active_alerts ? "bad" : "")}
    </div>
    <div class="two-col">
      <section class="chart-card"><div class="panel-head"><div><h2>People detected in the latest video</h2><p>Sampled-frame counts by video time · not unique visitors</p></div><span class="status-pill neutral">${summary.traffic_trend.length ? "RECORDED DATA" : "AWAITING VIDEO"}</span></div><div class="chart-wrap"><canvas id="traffic-chart" role="img" aria-describedby="traffic-summary"></canvas><p class="sr-only" id="traffic-summary"></p>${summary.traffic_trend.length ? "" : `<div class="chart-empty">Upload and process a video to see observed traffic.<br>Nothing is presented as live data.</div>`}</div></section>
      <section class="panel"><div class="panel-head"><div><h2>Active alerts</h2><p>Inventory and queue conditions</p></div><button class="button button-small" data-page="alerts">View all</button></div><div class="panel-body alert-list">${alertRows(summary.alerts)}</div></section>
    </div>
    <div class="two-col">
      <section class="panel"><div class="panel-head"><div><h2>Recent stock movements</h2><p>Changes saved in this workspace</p></div><button class="button button-small" data-page="inventory">Open inventory</button></div><div class="panel-body activity-list">${summary.recent_activity.length ? summary.recent_activity.map(row => `<div class="activity-row"><div><div class="activity-name">${esc(row.product_name)}${row.source === "sample" ? `<span class="sample-tag">sample</span>` : ""}</div><div class="activity-meta">${esc(row.sku)} · ${esc(row.reason)}</div></div><span class="delta ${row.delta > 0 ? "positive" : "negative"}">${row.delta > 0 ? "+" : ""}${number(row.delta)}</span><span class="activity-meta">${dateLabel(row.created_at)}</span></div>`).join("") : empty("Stock movements will appear here after an inventory change.")}</div></section>
      <section class="panel"><div class="panel-head"><div><h2>Processing status</h2><p>Local video pipeline</p></div>${statusPill(summary.latest_job?.status === "complete" ? "" : summary.latest_job?.status === "failed" ? "critical" : "neutral", summary.latest_job ? summary.latest_job.status : "idle")}</div><div class="panel-body"><div class="setting-line"><div class="setting-label"><strong>Latest job</strong><small>${lastUpdate}</small></div></div><div class="setting-line"><div class="setting-label"><strong>Detection model</strong><small>${esc(runtime.model_name)} · ${runtime.ultralytics_available ? "runtime installed" : "runtime not installed"}</small></div></div><div class="setting-line"><div class="setting-label"><strong>Quick actions</strong><small>Start with saved data or a local video</small></div></div><div class="quick-actions"><button class="button" data-page="shopper">Process video</button><button class="button" data-page="queue">Configure queue</button><button class="button" data-page="reports">Build report</button></div></div></section>
    </div>`;
  drawTrafficChart(summary.traffic_trend);
}

async function renderShopper() {
  const [jobs, runtime] = await Promise.all([api("/api/jobs"), api("/api/runtime")]);
  state.runtime = runtime;
  if (!state.activeJob && jobs.items.length) state.activeJob = jobs.items[0].id;
  let selected = null;
  if (state.activeJob) {
    try { selected = await api(`/api/jobs/${state.activeJob}`); }
    catch { state.activeJob = null; }
  }
  const supports = runtime.opencv_available && runtime.ultralytics_available;
  content.innerHTML = `${pageHeader("SHOPPER ANALYTICS", "See movement as it happens.", "Upload a local video to detect people in sampled frames. Counts are observations, not unique visitors.", `<a class="button" href="/api/export/observations.csv">Export observations CSV ↓</a>`)}
    <div class="two-col" style="margin-top:0">
      <section class="panel"><div class="panel-head"><div><h2>Analyze a video</h2><p>Processing runs locally on this device</p></div>${statusPill(supports ? "" : "warning", supports ? "MODEL RUNTIME READY" : "SETUP NEEDED")}</div><div class="panel-body">
        <label class="dropzone" id="video-dropzone" for="video-file"><span><span class="upload-symbol">↑</span><strong>Choose or drop a video file</strong><small>MP4, MOV, AVI, MKV or WebM · up to 200 MB</small><small>First model use may need internet to download ${esc(runtime.model_name)}.</small></span></label><input class="file-input-hidden" id="video-file" type="file" accept="video/mp4,video/quicktime,video/x-msvideo,video/x-matroska,video/webm,.mp4,.mov,.avi,.mkv,.webm">
        <div class="callout" style="margin-top:13px"><strong>Honest counts.</strong> Each result is the number of person detections in one sampled frame. Repeated detections can include the same person; this does not measure unique visitors, demographics, identity or dwell time.</div>
        <div id="selected-job">${selected ? jobDetailMarkup(selected) : `<div class="empty-inline">No video jobs yet. Add a file to begin local inference.</div>`}</div>
      </div></section>
      <section class="panel"><div class="panel-head"><div><h2>Processing history</h2><p>Saved jobs and their status</p></div><span class="mono muted">${jobs.items.length} JOB${jobs.items.length === 1 ? "" : "S"}</span></div><div class="panel-body">${jobs.items.length ? jobs.items.map(job => `<div class="job-row"><div><div class="job-title">${esc(job.filename)}</div><div class="activity-meta">${dateLabel(job.created_at)} · ${job.sampled_frames} sampled frames</div>${["queued","processing"].includes(job.status) ? `<div class="progress-track"><div class="progress-fill" style="width:${Math.round(job.progress)}%"></div></div>` : ""}${job.status === "failed" ? `<div class="alert-desc">${esc(job.error)}</div>` : ""}</div>${statusPill(job.status === "complete" ? "" : job.status === "failed" ? "critical" : "neutral", job.status)}<div class="job-actions"><button class="button button-small" data-action="select-job" data-id="${esc(job.id)}">Open</button>${["queued","processing"].includes(job.status) ? "" : `<button class="button button-small button-danger" data-action="delete-job" data-id="${esc(job.id)}" data-name="${esc(job.filename)}">Delete</button>`}</div></div>`).join("") : empty("Processing history will be retained on this device.")}</div></section>
    </div>`;
  const input = $("#video-file");
  input.addEventListener("change", () => { if (input.files?.[0]) uploadVideo(input.files[0]); });
  const drop = $("#video-dropzone");
  drop.addEventListener("dragover", e => { e.preventDefault(); drop.classList.add("dragover"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("dragover"));
  drop.addEventListener("drop", e => { e.preventDefault(); drop.classList.remove("dragover"); if (e.dataTransfer.files?.[0]) uploadVideo(e.dataTransfer.files[0]); });
}
function jobDetailMarkup(job) {
  if (job.status === "failed") return `<div class="video-result"><strong>Processing stopped</strong><p class="alert-desc">${esc(job.error || "The video could not be processed.")}</p><div class="activity-meta">Uploaded video remains local on this device.</div></div>`;
  if (job.status === "queued" || job.status === "processing") return `<div class="video-result"><div class="queue-copy"><strong>${esc(job.status === "queued" ? "Waiting to process" : "Processing video")}</strong><small>${Math.round(job.progress)}% · ${job.sampled_frames} frames sampled</small></div><div class="progress-track"><div class="progress-fill" style="width:${Math.round(job.progress)}%"></div></div><button class="button button-small" style="margin-top:11px" data-action="cancel-job" data-id="${esc(job.id)}">Cancel processing</button></div>`;
  if (job.status === "canceled") return `<div class="video-result"><strong>Processing canceled</strong><p class="activity-meta">Partial observations were retained.</p></div>`;
  return `<div class="video-result"><div class="metric-grid grid" style="grid-template-columns:repeat(3,minmax(0,1fr))">${metric("Peak frame count", job.peak_count == null ? "—" : number(job.peak_count), "Highest sampled frame", "⌁")}${metric("Average per frame", job.average_count == null ? "—" : Number(job.average_count).toFixed(1), "Across sampled frames", "∿")}${metric("Frames sampled", number(job.sampled_frames), `At ${dateLabel(job.updated_at)}`, "▧")}</div>
    ${job.output_url ? `<video controls preload="metadata" style="width:100%;max-height:340px;margin-top:14px;border-radius:8px;background:#08090b" src="${esc(job.output_url)}"></video>` : ""}
    <div class="observation-table"><table><thead><tr><th>Video time</th><th>People detected</th><th>Queue region</th></tr></thead><tbody>${job.observations.map(row => `<tr><td>${Number(row.timestamp_seconds).toFixed(1)}s</td><td>${number(row.people_count)}</td><td>${row.queue_count == null ? "—" : number(row.queue_count)}</td></tr>`).join("")}</tbody></table></div>
    <div class="activity-meta" style="margin-top:8px">Counts are model detections in sampled frames; camera angle and occlusion affect estimates.</div></div>`;
}
async function uploadVideo(file) {
  const ext = file.name.split(".").pop().toLowerCase();
  if (!["mp4", "mov", "avi", "mkv", "webm"].includes(ext)) return toast("Choose an MP4, MOV, AVI, MKV or WebM file.", "error");
  if (file.size <= 0 || file.size > 200 * 1024 * 1024) return toast("Video must be smaller than 200 MB.", "error");
  try {
    const job = await api(`/api/videos?filename=${encodeURIComponent(file.name)}`, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file });
    state.activeJob = job.id; toast("Video received. Local processing has started."); await render("shopper");
  } catch (error) { toast(error.message, "error"); }
}

async function renderQueue() {
  const [summary, settings, runtime, jobs] = await Promise.all([api("/api/summary"), api("/api/settings"), api("/api/runtime"), api("/api/jobs")]);
  state.settings = settings; state.runtime = runtime;
  const latest = jobs.items.find(job => job.status === "complete");
  let job = null;
  if (latest) { try { job = await api(`/api/jobs/${latest.id}`); } catch {} }
  const current = summary.queue_estimate;
  const exceeded = current != null && current > settings.queue_threshold;
  const region = settings.queue_region;
  content.innerHTML = `${pageHeader("QUEUE MANAGEMENT", "Keep the line in view.", "Estimate detections inside a configurable video region. Counts depend on camera angle, occlusion and model performance.")}
    <div class="two-col" style="margin-top:0">
      <section class="panel"><div class="panel-head"><div><h2>Latest queue estimate</h2><p>From the most recently processed sampled frame</p></div>${statusPill(current == null ? "neutral" : exceeded ? "warning" : "", current == null ? "NO OBSERVATION" : exceeded ? "ABOVE THRESHOLD" : "WITHIN THRESHOLD")}</div>
      <div class="panel-body"><div class="queue-banner"><div class="queue-number">${current == null ? "—" : number(current)}</div><div class="queue-copy"><strong>${current == null ? "No queue observation yet" : "People detected in region"}</strong><small>${summary.queue_observed_at ? `Observed ${dateLabel(summary.queue_observed_at)}` : "Process a video to record an estimate"}</small></div><span class="mono muted">LIMIT ${settings.queue_threshold}</span></div>
      <div class="callout" style="margin-top:14px">This is an estimated count of person detections whose bounding-box centers fall inside the selected region. It does not predict wait time or identify individuals.</div>
      ${job ? `<div class="section-label">LATEST VIDEO · ${esc(job.filename)}</div><div class="observation-table"><table><thead><tr><th>Time</th><th>In region</th><th>People in frame</th></tr></thead><tbody>${job.observations.map(row => `<tr><td>${Number(row.timestamp_seconds).toFixed(1)}s</td><td>${number(row.queue_count || 0)}</td><td>${number(row.people_count)}</td></tr>`).join("")}</tbody></table></div>` : empty("Queue observations will appear here after processing a video.")}</div></section>
      <section class="panel"><div class="panel-head"><div><h2>Monitoring region</h2><p>Normalized frame coordinates · save before processing</p></div></div><div class="panel-body"><label class="setting-label" for="queue-threshold"><strong>Alert threshold</strong><small>Alert when estimated people exceed this number.</small></label><input class="input" id="queue-threshold" type="number" min="1" max="500" value="${settings.queue_threshold}" style="margin-top:9px;width:100%"><div class="section-label">REGION BOUNDS</div>
        <div class="queue-config">${rangeControl("Region left", "region-x", region.x, 0, 90)}${rangeControl("Region top", "region-y", region.y, 0, 90)}${rangeControl("Region width", "region-width", region.width, 10, 100)}${rangeControl("Region height", "region-height", region.height, 10, 100)}</div>
        <button class="button button-primary" style="margin-top:13px" data-action="save-queue">Save queue settings</button></div></section>
    </div><div class="section-label">MODEL STATUS</div><div class="panel panel-body"><div class="info-line"><span>Model</span><strong>${esc(runtime.model_name)}</strong></div><div class="info-line"><span>Compute</span><strong>${esc(runtime.device)}</strong></div><div class="info-line"><span>Detection engine</span><strong>${runtime.ultralytics_available && runtime.opencv_available ? "Ready" : "Dependencies not installed"}</strong></div></div>`;
  $$("#page-content input[type=range]").forEach(input => input.addEventListener("input", () => { $(`[data-value="${input.id}"]`).textContent = `${input.value}%`; input.setAttribute("aria-valuetext", `${input.value}%`); constrainRegion(input.id); }));
}
function rangeControl(label, id, normalized, min, max) {
  const value = Math.round(normalized * 100);
  return `<div class="range-control"><label for="${id}">${esc(label)}<span data-value="${id}">${value}%</span></label><input type="range" id="${id}" min="${min}" max="${max}" value="${value}" aria-valuetext="${value}%"></div>`;
}
function constrainRegion(changed) {
  const x = Number($("#region-x").value), y = Number($("#region-y").value);
  const width = $("#region-width"), height = $("#region-height");
  width.max = String(Math.max(10, 100 - x)); if (Number(width.value) > Number(width.max)) width.value = width.max;
  height.max = String(Math.max(10, 100 - y)); if (Number(height.value) > Number(height.max)) height.value = height.max;
  if (changed === "region-x" && x === 90) width.value = 10;
  if (changed === "region-y" && y === 90) height.value = 10;
  $(`[data-value="${width.id}"]`).textContent = `${width.value}%`; width.setAttribute("aria-valuetext", `${width.value}%`);
  $(`[data-value="${height.id}"]`).textContent = `${height.value}%`; height.setAttribute("aria-valuetext", `${height.value}%`);
}

async function renderInventory() {
  const [result, summary] = await Promise.all([api(`/api/products?search=${encodeURIComponent(state.productSearch)}&category=${encodeURIComponent(state.productCategory)}&stock=${state.productStock}&limit=25&offset=${state.productPage * 25}`), api("/api/summary")]);
  state.summary = summary;
  const pages = Math.max(1, Math.ceil(result.total / 25));
  const table = result.items.length ? result.items.map(item => {
    const stock = item.quantity === 0 ? statusPill("critical", "OUT OF STOCK") : item.reorder_threshold > 0 && item.quantity <= item.reorder_threshold ? statusPill("warning", "LOW STOCK") : statusPill("", "IN STOCK");
    return `<tr><td><div class="table-primary">${esc(item.name)}${item.is_sample ? `<span class="sample-tag">sample</span>` : ""}</div><div class="table-secondary">${esc(item.sku)}</div></td><td>${esc(item.category)}</td><td class="stock-cell">${number(item.quantity)} ${esc(item.unit)}<div class="table-secondary">Reorder at ${number(item.reorder_threshold)}</div></td><td>${money(item.price)}</td><td>${stock}</td><td><div class="table-actions"><button class="button button-small" title="Adjust stock" data-action="adjust-product" data-id="${esc(item.id)}" data-name="${esc(item.name)}">± Adjust</button><button class="button button-small" title="Edit product" data-action="edit-product" data-id="${esc(item.id)}">Edit</button><button class="button button-small button-danger" title="Delete product" data-action="delete-product" data-id="${esc(item.id)}" data-name="${esc(item.name)}">×</button></div></td></tr>`;
  }).join("") : `<tr><td colspan="6"><div class="empty-state"><div><div class="empty-state-mark">▤</div><h3>${state.productSearch || state.productCategory || state.productStock !== "all" ? "No matching products" : "Inventory is empty"}</h3><p>${state.productSearch || state.productCategory || state.productStock !== "all" ? "Try changing your filters, or clear the search." : "Add your own verified products or load the clearly marked sample dataset to explore the inventory workflow."}</p>${!state.productSearch && state.productStock === "all" ? `<button class="button button-primary" data-action="open-add-product">＋ Add first product</button>` : ""}</div></div></td></tr>`;
  content.innerHTML = `${pageHeader("INVENTORY", "Stock, without the guesswork.", `${number(result.total)} products · ${number(summary.low_stock)} low stock · ${number(summary.out_of_stock)} out of stock`, `<button class="button" data-action="load-sample">Load sample dataset</button><a class="button" href="/api/export/inventory.csv">Export CSV ↓</a><button class="button button-primary" data-action="open-add-product">＋ Add product</button>`)}
    <section class="table-panel"><div class="toolbar"><div class="toolbar-left"><input class="input search-input" id="inventory-search" value="${esc(state.productSearch)}" placeholder="Search name or SKU" aria-label="Search inventory"><select class="input" id="inventory-category" aria-label="Filter by category"><option value="">All categories</option>${result.categories.map(c => `<option value="${esc(c)}" ${state.productCategory === c ? "selected" : ""}>${esc(c)}</option>`).join("")}</select><select class="input" id="inventory-stock" aria-label="Filter by stock status"><option value="all">All stock status</option><option value="low" ${state.productStock === "low" ? "selected" : ""}>Low stock</option><option value="out" ${state.productStock === "out" ? "selected" : ""}>Out of stock</option><option value="healthy" ${state.productStock === "healthy" ? "selected" : ""}>Healthy</option></select></div><div class="toolbar-right"><label class="button" for="inventory-import">Import CSV ↑</label><input class="file-input-hidden" id="inventory-import" type="file" accept=".csv,text/csv" aria-label="Import inventory CSV"></div></div><div class="table-scroll"><table><thead><tr><th scope="col">Product</th><th scope="col">Category</th><th scope="col">Quantity</th><th scope="col">Unit price</th><th scope="col">Stock state</th><th scope="col" style="text-align:right">Actions</th></tr></thead><tbody>${table}</tbody></table></div><div class="pagination"><span>Showing ${result.total ? state.productPage * 25 + 1 : 0}–${Math.min((state.productPage + 1) * 25, result.total)} of ${result.total}</span><div class="pagination-actions"><button class="page-number" data-action="product-page" data-page="${Math.max(0, state.productPage - 1)}" ${state.productPage <= 0 ? "disabled" : ""} aria-label="Previous page">‹</button><span class="mono muted" style="align-self:center;padding:0 5px">${state.productPage + 1} / ${pages}</span><button class="page-number" data-action="product-page" data-page="${Math.min(pages - 1, state.productPage + 1)}" ${state.productPage >= pages - 1 ? "disabled" : ""} aria-label="Next page">›</button></div></div></section>
    <div class="section-label">INVENTORY MOVEMENT HISTORY</div><section class="panel"><div class="panel-head"><div><h2>Recent movements</h2><p>Initial stock, adjustments and validated imports</p></div><a class="button button-small" href="/api/export/movements.csv">Export</a></div><div class="panel-body" id="movement-list">Loading movements…</div></section>`;
  const search = $("#inventory-search");
  let searchTimer;
  search.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.productSearch = search.value; state.productPage = 0; renderInventory(); }, 240); });
  $("#inventory-category").addEventListener("change", e => { state.productCategory = e.target.value; state.productPage = 0; renderInventory(); });
  $("#inventory-stock").addEventListener("change", e => { state.productStock = e.target.value; state.productPage = 0; renderInventory(); });
  $("#inventory-import").addEventListener("change", e => { if (e.target.files?.[0]) importInventory(e.target.files[0]); });
  api("/api/movements").then(data => {
    const node = $("#movement-list"); if (!node) return;
    node.innerHTML = data.items.length ? `<div class="activity-list">${data.items.slice(0, 10).map(m => `<div class="activity-row"><div><div class="activity-name">${esc(m.product_name)}${m.source === "sample" ? `<span class="sample-tag">sample</span>` : ""}</div><div class="activity-meta">${esc(m.sku)} · ${esc(m.reason)} · ${esc(m.source)}</div></div><span class="delta ${m.delta >= 0 ? "positive" : "negative"}">${m.delta > 0 ? "+" : ""}${number(m.delta)}</span><span class="activity-meta">${dateLabel(m.created_at)}</span></div>`).join("")}</div>` : empty("No movements have been saved yet.");
  }).catch(() => {});
}
async function importInventory(file) {
  if (file.size > 2_800_000) return toast("CSV must be smaller than 2.8 MB.", "error");
  try {
    const result = await api("/api/products/import", { method: "POST", body: await file.text(), headers: { "Content-Type": "text/csv;charset=utf-8" } });
    toast(`CSV imported: ${result.added} added, ${result.updated} updated.`); await render("inventory");
  } catch (error) { toast(error.message, "error"); }
}

async function renderAlerts() {
  const data = await api(`/api/alerts?status=${state.alertStatus}&source=${state.alertSource}`);
  const rows = data.items.length ? data.items.map(item => `<tr><td>${statusPill(item.severity, item.severity)}</td><td><div class="table-primary">${esc(item.title)}</div><div class="table-secondary">${esc(item.description)}</div></td><td>${esc(item.source)}</td><td>${dateLabel(item.first_seen)}</td><td>${item.status === "resolved" ? statusPill("neutral", `resolved ${dateLabel(item.resolved_at, false)}`) : statusPill("warning", "active")}</td><td style="text-align:right">${item.status === "active" ? `<button class="button button-small" data-action="resolve-alert" data-id="${esc(item.id)}">Resolve</button>` : ""}</td></tr>`).join("") : `<tr><td colspan="6"><div class="empty-state"><div><div class="empty-state-mark">♢</div><h3>No ${esc(state.alertStatus === "all" ? "matching" : state.alertStatus)} alerts</h3><p>Stock conditions and queue threshold events are recorded here, with repeated active conditions combined.</p></div></div></td></tr>`;
  content.innerHTML = `${pageHeader("ALERTS CENTER", "A single view of what needs attention.", "Inventory thresholds and queue events are stored with their resolution history.", `<a class="button" href="/api/export/alerts.csv">Export history ↓</a>`)}
    <section class="table-panel"><div class="toolbar"><div class="toolbar-left"><div class="segmented" role="group" aria-label="Filter alerts by status">${["active","resolved","all"].map(x => `<button class="segment ${state.alertStatus === x ? "active" : ""}" aria-pressed="${state.alertStatus === x}" data-action="alert-status" data-value="${x}">${x === "all" ? "All" : x[0].toUpperCase() + x.slice(1)}</button>`).join("")}</div><select class="input" id="alert-source" aria-label="Filter alerts by source"><option value="all">All sources</option><option value="inventory" ${state.alertSource === "inventory" ? "selected" : ""}>Inventory</option><option value="queue" ${state.alertSource === "queue" ? "selected" : ""}>Queue</option></select></div><span class="mono muted">${data.items.length} RECORD${data.items.length === 1 ? "" : "S"}</span></div><div class="table-scroll"><table><thead><tr><th scope="col">Severity</th><th scope="col">Alert</th><th scope="col">Source</th><th scope="col">First seen</th><th scope="col">State</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead><tbody>${rows}</tbody></table></div></section>
    <div class="callout" style="margin-top:13px"><strong>Deduplication.</strong> A condition stays as one active alert while it continues. After resolution, a later recurrence creates a fresh alert record.</div>`;
  $("#alert-source").addEventListener("change", e => { state.alertSource = e.target.value; renderAlerts(); });
}

async function renderReports() {
  const report = await api(`/api/report?start=${encodeURIComponent(state.reportStart)}&end=${encodeURIComponent(state.reportEnd)}`);
  const { totals, observations, movements, alerts } = report;
  content.innerHTML = `${pageHeader("REPORTS", "The day, on one receipt.", "Generated from saved inventory, sampled-frame observations and alert history.", `<button class="button" data-action="print-report">Print report</button><a class="button" href="/api/export/observations.csv?start=${state.reportStart}&end=${state.reportEnd}">Export observations CSV ↓</a>`)}
    <div class="toolbar no-print" style="padding:0 0 15px;border:0"><div class="toolbar-left"><label class="mono muted" for="report-start">FROM</label><input class="input date-input" id="report-start" type="date" value="${esc(state.reportStart)}"><label class="mono muted" for="report-end">TO</label><input class="input date-input" id="report-end" type="date" value="${esc(state.reportEnd)}"><button class="button button-small" data-action="apply-report">Apply dates</button></div><div class="toolbar-right"><a class="button button-small" href="/api/export/movements.csv?start=${encodeURIComponent(state.reportStart)}&end=${encodeURIComponent(state.reportEnd)}">Movements CSV</a><a class="button button-small" href="/api/export/alerts.csv?start=${encodeURIComponent(state.reportStart)}&end=${encodeURIComponent(state.reportEnd)}">Alerts CSV</a><a class="button button-small" href="/api/export/inventory.csv">Inventory CSV</a></div></div>
    <article class="report-paper"><div class="receipt-top"><div><div class="receipt-brand">RETAIL<span style="color:var(--text)">X</span></div><div class="receipt-title">STORE INTELLIGENCE · DAILY SUMMARY</div></div><div class="receipt-meta">REPORT WINDOW<br>${esc(state.reportStart)} — ${esc(state.reportEnd)}<br>GENERATED ${new Date().toLocaleString()}</div></div>
    <section class="receipt-section"><h3>01 / Store summary</h3><div class="receipt-total"><span>Products currently tracked</span><strong>${number(totals.products)}</strong></div><div class="receipt-total"><span>Low-stock items now</span><strong>${number(totals.low_stock)}</strong></div><div class="receipt-total"><span>Out-of-stock items now</span><strong>${number(totals.out_of_stock)}</strong></div><div class="receipt-total"><span>Stock movements in window</span><strong>${number(totals.movements)}</strong></div></section>
    <section class="receipt-section"><h3>02 / Shopper frame observations</h3><div class="receipt-total"><span>Sampled frames</span><strong>${number(totals.observations)}</strong></div><div class="receipt-total"><span>Average detections per sampled frame</span><strong>${totals.average_people == null ? "—" : Number(totals.average_people).toFixed(2)}</strong></div><div class="receipt-total"><span>Peak observed frame count</span><strong>${totals.peak_people == null ? "—" : number(totals.peak_people)}</strong></div><div class="receipt-total"><span>Queue threshold events</span><strong>${number(totals.queue_events)}</strong></div><p class="activity-meta">Frame detections are repeated observations and do not represent unique visitors.</p></section>
    <section class="receipt-section"><h3>03 / Recent stock movement</h3>${movements.length ? movements.slice(0, 10).map(m => `<div class="receipt-total"><span>${esc(m.product_name)} · ${esc(m.reason)}</span><strong>${m.delta > 0 ? "+" : ""}${number(m.delta)} → ${number(m.quantity_after)}</strong></div>`).join("") : `<div class="activity-meta">No stock movements were recorded in this period.</div>`}</section>
    <section class="receipt-section"><h3>04 / Alert history</h3>${alerts.length ? alerts.slice(0, 10).map(a => `<div class="receipt-total"><span>${esc(a.severity.toUpperCase())} · ${esc(a.title)} · ${esc(a.status)}</span><strong>${dateLabel(a.first_seen, false)}</strong></div>`).join("") : `<div class="activity-meta">No alerts were recorded in this period.</div>`}</section>
    <div class="barcode" aria-hidden="true">${Array.from({ length: 46 }, (_, i) => `<i style="height:${i % 5 === 0 ? 80 : 100}%"></i>`).join("")}</div><div class="receipt-foot">RETAILX · LOCAL PROTOTYPE · ${esc(state.reportStart)}—${esc(state.reportEnd)}<br>Estimates depend on video angle, occlusion and model performance.<br>Thank you for making smarter store decisions.</div></article>`;
}

async function renderSettings() {
  const [settings, runtime] = await Promise.all([api("/api/settings"), api("/api/runtime")]);
  state.settings = settings; state.runtime = runtime;
  setTheme(settings.theme, false);
  content.innerHTML = `${pageHeader("SETTINGS", "Tune the way RetailX works.", "Configuration is saved in this device’s local database; no credentials are exposed in the browser.")}
    <div class="settings-layout"><section class="settings-card"><h2>Appearance</h2><p>Choose the palette used across every page.</p><div class="setting-line"><div class="setting-label"><strong>Theme</strong><small>Persisted across restarts</small></div><div class="theme-options"><button class="theme-choice ${settings.theme === "dark" ? "active" : ""}" data-action="set-theme" data-value="dark"><i class="theme-swatch"></i>Midnight</button><button class="theme-choice ${settings.theme === "light" ? "active" : ""}" data-action="set-theme" data-value="light"><i class="theme-swatch light"></i>Ivory</button></div></div></section>
    <section class="settings-card"><h2>Video processing</h2><p>Frames are decoded and analyzed on this computer.</p><div class="setting-line"><label class="setting-label" for="setting-sample"><strong>Sample interval</strong><small>Seconds between analyzed frames</small></label><select id="setting-sample" class="input"><option value="0.5" ${settings.video_sample_seconds === .5 ? "selected" : ""}>0.5 sec</option><option value="1" ${settings.video_sample_seconds === 1 ? "selected" : ""}>1 sec</option><option value="2" ${settings.video_sample_seconds === 2 ? "selected" : ""}>2 sec</option><option value="5" ${settings.video_sample_seconds === 5 ? "selected" : ""}>5 sec</option></select></div><div class="setting-line"><label class="setting-label" for="setting-confidence"><strong>Detection confidence</strong><small>Higher can reduce uncertain detections</small></label><select id="setting-confidence" class="input"><option value="0.15" ${settings.confidence_threshold === .15 ? "selected" : ""}>15%</option><option value="0.25" ${settings.confidence_threshold === .25 ? "selected" : ""}>25%</option><option value="0.4" ${settings.confidence_threshold === .4 ? "selected" : ""}>40%</option><option value="0.6" ${settings.confidence_threshold === .6 ? "selected" : ""}>60%</option></select></div><div class="setting-line"><div class="setting-label"><strong>Maximum upload</strong><small>Supported formats: MP4, MOV, AVI, MKV, WebM</small></div><strong>200 MB</strong></div></section>
    <section class="settings-card"><h2>Queue thresholds</h2><p>Update the count alert limit. Region bounds are configured on Queue Management.</p><div class="setting-line"><label class="setting-label" for="setting-threshold"><strong>Alert when detections exceed</strong><small>People in configured region</small></label><input id="setting-threshold" class="input" type="number" min="1" max="500" value="${settings.queue_threshold}"></div><button class="button button-primary" data-action="save-settings">Save processing settings</button></section>
    <section class="settings-card"><h2>Model and device</h2><p>Capability status checked in the local Python runtime.</p><div class="info-line"><span>Pretrained model</span><strong>${esc(runtime.model_name)}</strong></div><div class="info-line"><span>OpenCV</span><strong>${runtime.opencv_available ? "Available" : "Not installed"}</strong></div><div class="info-line"><span>Ultralytics</span><strong>${runtime.ultralytics_available ? "Available" : "Not installed"}</strong></div><div class="info-line"><span>Compute device</span><strong>${esc(runtime.device)}</strong></div><div class="notice warning" style="margin-top:11px">The model is loaded only during processing. Its first use may need internet access to fetch weights. GPU use is reported only when PyTorch confirms CUDA is available; otherwise CPU is used.</div></section>
    <section class="settings-card"><h2>Privacy and data retention</h2><p>Local video storage and the prototype’s limits.</p><div class="info-line"><span>Database and video folder</span><strong class="mono">${esc(runtime.storage)}</strong></div><div class="setting-line"><label class="setting-label" for="setting-retention"><strong>Retention preference</strong><small>Saved as a reminder; not an automatic deletion schedule</small></label><select id="setting-retention" class="input"><option value="7" ${settings.retention_days === 7 ? "selected" : ""}>7 days</option><option value="30" ${settings.retention_days === 30 ? "selected" : ""}>30 days</option><option value="90" ${settings.retention_days === 90 ? "selected" : ""}>90 days</option><option value="365" ${settings.retention_days === 365 ? "selected" : ""}>1 year</option></select></div><div class="notice">Uploaded footage and annotated videos stay on this device until their job is deleted. Inventory, movements, alerts, and settings remain until removed. This prototype does not send footage to an inference service. No face recognition, identity matching or demographics are implemented. This prototype has not been independently security-audited.</div></section>
    <section class="settings-card"><h2>Application</h2><p>Running as a local, single-user demonstration.</p><div class="info-line"><span>Build</span><strong>RetailX local prototype</strong></div><div class="info-line"><span>Storage</span><strong>SQLite · persistent on this device</strong></div><div class="info-line"><span>API address</span><strong>http://127.0.0.1:8000</strong></div><div class="info-line"><span>Supabase cloud</span><strong>Not connected</strong></div></section></div>`;
}

async function render(page = state.page) {
  const token = ++state.renderToken;
  shellHeader(page);
  content.innerHTML = `<div class="empty-state"><div><div class="empty-state-mark">×</div><h3>Loading ${esc(pageNames[page])}</h3><p>Reading saved local data.</p></div></div>`;
  try {
    if (page === "overview") await renderOverview();
    else if (page === "shopper") await renderShopper();
    else if (page === "queue") await renderQueue();
    else if (page === "inventory") await renderInventory();
    else if (page === "alerts") await renderAlerts();
    else if (page === "reports") await renderReports();
    else if (page === "settings") await renderSettings();
    if (token !== state.renderToken) return;
    if (page === "overview") drawTrafficChart(state.summary?.traffic_trend);
    await refreshShellStatus();
  } catch (error) {
    if (token !== state.renderToken) return;
    content.innerHTML = `${pageHeader("LOCAL WORKSPACE", pageNames[page], "The application could not read its saved data.")}<div class="notice warning">${esc(error.message)}. Check that the local RetailX server is running, then refresh this page.</div>`;
  }
}
async function refreshShellStatus() {
  try {
    const [summary] = await Promise.all([api("/api/summary")]);
    state.summary = summary;
    const count = $("#nav-alert-count"); count.textContent = summary.active_alerts; count.hidden = !summary.active_alerts;
    $("#header-status").textContent = summary.latest_job && ["queued", "processing"].includes(summary.latest_job.status) ? `Video ${summary.latest_job.status}` : "Local data";
    $("#footer-updated").textContent = `Updated ${new Date().toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`;
  } catch {}
}

function openProduct(item = null) {
  const dialog = $("#product-dialog"), form = $("#product-form");
  $("#product-form-error").hidden = true; $("#product-form-error").textContent = "";
  form.reset(); form.elements.id.value = item?.id || "";
  $("#product-modal-title").textContent = item ? "Edit product" : "Add product";
  for (const name of ["sku","name","category","quantity","unit","price","reorder_threshold"]) {
    form.elements[name].value = item?.[name] ?? ({ unit: "units", quantity: 0, price: 0, reorder_threshold: 5 }[name] ?? "");
  }
  dialog.showModal();
}
async function editProduct(id) {
  const result = await api(`/api/products/${id}`);
  openProduct(result.item);
}
function openAdjust(id, name) {
  const form = $("#adjust-form"); form.reset(); form.elements.id.value = id;
  $("#adjust-form-error").hidden = true; $("#adjust-form-error").textContent = "";
  $("#adjust-product-name").textContent = name;
  $("#adjust-dialog").showModal();
}
async function resolveAlert(id) {
  await api(`/api/alerts/${id}/resolve`, { method: "POST" });
  toast("Alert resolved."); await render(state.page);
}
async function saveQueue() {
  const x = Number($("#region-x").value) / 100, y = Number($("#region-y").value) / 100;
  const width = Number($("#region-width").value) / 100, height = Number($("#region-height").value) / 100;
  const threshold = Number($("#queue-threshold").value);
  const updated = await api("/api/settings", { method: "POST", body: { queue_threshold: threshold, queue_region: { x, y, width, height } } });
  state.settings = updated; toast("Queue region and threshold saved."); await render("queue");
}
async function saveProcessingSettings() {
  const settings = await api("/api/settings", { method: "POST", body: { video_sample_seconds: Number($("#setting-sample").value), confidence_threshold: Number($("#setting-confidence").value), queue_threshold: Number($("#setting-threshold").value), retention_days: Number($("#setting-retention").value) } });
  state.settings = settings; toast("Processing settings saved."); await render("settings");
}
async function reportDates() {
  const start = $("#report-start").value, end = $("#report-end").value;
  if (start && end && start > end) return toast("Start date must come before end date.", "error");
  state.reportStart = start; state.reportEnd = end; await render("reports");
}

content.addEventListener("click", async event => {
  const pageButton = event.target.closest("[data-page]");
  if (pageButton) { await render(pageButton.dataset.page); return; }
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  const { action, id, value } = button.dataset;
  try {
    if (action === "open-add-product") openProduct();
    else if (action === "adjust-product") openAdjust(id, button.dataset.name);
    else if (action === "edit-product") await editProduct(id);
    else if (action === "delete-product") {
      if (confirm(`Delete ${button.dataset.name}? Its movement history will remain; this cannot be undone.`)) {
        await api(`/api/products/${id}/delete`, { method: "POST" }); toast("Product deleted."); await render("inventory");
      }
    } else if (action === "product-page") { state.productPage = Number(button.dataset.page); await render("inventory"); }
    else if (action === "load-sample") {
      if (confirm("Add the clearly labeled sample dataset? Existing SKUs will be skipped.")) {
        const result = await api("/api/products/sample", { method: "POST" }); toast(`${result.label}: ${result.added} products added.`); await render("inventory");
      }
    } else if (action === "resolve-alert") await resolveAlert(id);
    else if (action === "alert-status") { state.alertStatus = value; await render("alerts"); }
    else if (action === "select-job") { state.activeJob = id; await render("shopper"); }
    else if (action === "cancel-job") { await api(`/api/jobs/${id}/cancel`, { method: "POST" }); toast("Cancellation requested."); await render("shopper"); }
    else if (action === "delete-job") {
      if (confirm(`Permanently delete ${button.dataset.name}, its local source/output videos, and its observations?`)) {
        await api(`/api/jobs/${id}/delete`, { method: "POST" });
        state.activeJob = null; toast("Video job and managed files deleted."); await render("shopper");
      }
    }
    else if (action === "save-queue") await saveQueue();
    else if (action === "set-theme") { setTheme(value); toast(`${value === "dark" ? "Midnight Platinum" : "Ivory & Burgundy"} theme saved.`); await render("settings"); }
    else if (action === "save-settings") await saveProcessingSettings();
    else if (action === "print-report") window.print();
    else if (action === "apply-report") await reportDates();
  } catch (error) { toast(error.message, "error"); }
});

$("#product-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget, data = Object.fromEntries(new FormData(form));
  const id = data.id; delete data.id;
  for (const key of ["quantity", "reorder_threshold", "price"]) data[key] = Number(data[key]);
  try {
    await api(id ? `/api/products/${id}` : "/api/products", { method: id ? "PUT" : "POST", body: data });
    $("#product-dialog").close(); toast(id ? "Product updated." : "Product added."); await render("inventory");
  } catch (error) { const message = $("#product-form-error"); message.textContent = error.message; message.hidden = false; message.focus(); }
});
$("#adjust-form").addEventListener("submit", async event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.currentTarget));
  try {
    await api(`/api/products/${data.id}/adjust`, { method: "POST", body: { delta: Number(data.delta), reason: data.reason } });
    $("#adjust-dialog").close(); toast("Stock movement saved."); await render("inventory");
  } catch (error) { const message = $("#adjust-form-error"); message.textContent = error.message; message.hidden = false; message.focus(); }
});
$$(".close-modal").forEach(button => button.addEventListener("click", () => button.closest("dialog").close()));
$("#theme-toggle").addEventListener("click", async () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  setTheme(next); if (state.page === "settings") await render("settings");
});
$("#mobile-menu").addEventListener("click", () => { const open = $("#sidebar").classList.toggle("open"); $("#mobile-menu").setAttribute("aria-expanded", String(open)); });
$("#sidebar").addEventListener("click", event => { const item = event.target.closest("[data-page]"); if (item) render(item.dataset.page); });
window.addEventListener("resize", () => { if (state.page === "overview") drawTrafficChart(state.summary?.traffic_trend); });

let brandIntroRun = 0;
let brandIntroAnimations = [];
function startBrandIntro() {
  const run = ++brandIntroRun;
  brandIntroAnimations.forEach(animation => animation.cancel());
  brandIntroAnimations = [];
  const intro = $("#brand-intro");
  const appShell = $(".app-shell");
  const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
  if (!intro || !appShell) return;
  if (reduceMotion) { intro.hidden = true; appShell.inert = false; return; }

  const center = $("#brand-intro-center");
  const retail = $("#brand-intro-retail");
  const letters = retail ? $$(".brand-intro-letter", retail) : [];
  const x = $("#brand-intro-x");
  const trail = $("#brand-intro-trail");
  if (!center?.animate || !letters.length || letters.some(letter => !letter.animate) || !x?.animate || !trail?.animate) { intro.hidden = true; appShell.inert = false; return; }

  intro.hidden = false;
  intro.style.opacity = "";
  appShell.inert = true;
  let finished = false;
  let running = [];
  function finish() {
    if (finished || run !== brandIntroRun) return;
    finished = true;
    running.forEach(animation => animation.cancel());
    brandIntroAnimations = [];
    intro.hidden = true;
    appShell.inert = false;
  }
  const centerRect = center.getBoundingClientRect();
  const xRect = x.getBoundingClientRect();
  const distance = xRect.right - centerRect.left;
  try {
    running.push(x.animate(
      [
        { transform: `translateX(${-distance}px)`, opacity: 0 },
        { opacity: 1, offset: .12 },
        { transform: "translateX(0)", opacity: 1 },
      ],
      { duration: 1200, easing: "cubic-bezier(.75,0,.15,1)", fill: "both" },
    ));
    running.push(trail.animate(
      [
        { opacity: 0, transform: "scaleX(.2)" },
        { opacity: .85, transform: "scaleX(1)", offset: .45 },
        { opacity: 0, transform: "scaleX(.3)" },
      ],
      { duration: 1200, easing: "ease-out", fill: "both" },
    ));
    letters.forEach((letter, index) => {
      running.push(letter.animate(
        [{ opacity: 0, transform: "translateX(-.35em)" }, { opacity: 1, transform: "translateX(0)" }],
        { duration: 380, delay: 1250 + index * 75, easing: "cubic-bezier(.2,.7,.2,1)", fill: "both" },
      ));
    });
    running.push($(".brand-intro-caption").animate(
      [{ opacity: 0, transform: "translateY(5px)" }, { opacity: 1, transform: "translateY(0)" }],
      { duration: 350, delay: 2050, easing: "ease-out", fill: "forwards" },
    ));
    const exit = intro.animate(
      [{ opacity: 1 }, { opacity: 0 }],
      { duration: 340, delay: 2500, easing: "ease-out", fill: "forwards" },
    );
    running.push(exit);
    brandIntroAnimations = running;
    exit.finished.then(finish, finish);
    window.setTimeout(finish, 3000);
  } catch {
    running.forEach(animation => animation.cancel());
    brandIntroAnimations = [];
    intro.hidden = true;
    appShell.inert = false;
  }
}

window.addEventListener("pageshow", event => {
  if (event.persisted) startBrandIntro();
});

async function boot() {
  try {
    state.settings = await api("/api/settings"); setTheme(state.settings.theme, false);
    await refreshShellStatus(); await render("overview");
    setInterval(async () => {
      await refreshShellStatus();
      if (state.page === "shopper" && state.activeJob) {
        try {
          const job = await api(`/api/jobs/${state.activeJob}`);
          if (["queued", "processing"].includes(job.status)) render("shopper");
        } catch {}
      }
    }, 2500);
  } catch (error) {
    content.innerHTML = `<div class="empty-state"><div><div class="empty-state-mark">×</div><h3>RetailX server is not connected</h3><p>${esc(error.message)}. Start the local server with the command in README.md.</p></div></div>`;
  }
}
startBrandIntro();
boot();
