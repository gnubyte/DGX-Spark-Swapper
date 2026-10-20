"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

/* Plain text with **bold** and `code` spans, built without innerHTML. */
function rich(tag, text, cls) {
  const n = el(tag, cls);
  for (const part of String(text).split(/(\*\*[^*]+\*\*|`[^`]+`)/)) {
    if (!part) continue;
    if (part.startsWith("**")) n.append(el("strong", "", part.slice(2, -2)));
    else if (part.startsWith("`")) n.append(el("code", "", part.slice(1, -1)));
    else n.append(document.createTextNode(part));
  }
  return n;
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Swapper": "1" },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, { credentials: "same-origin", ...opts });
  let data = {};
  try { data = await r.json(); } catch (_) { /* empty */ }
  if (!r.ok) {
    const e = new Error(data.error || `Request failed (${r.status})`);
    e.status = r.status;
    throw e;
  }
  return data;
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._h);
  toast._h = setTimeout(() => t.classList.remove("show"), 2600);
}

async function copyText(value, node) {
  try { await navigator.clipboard.writeText(value); toast("Copied"); }
  catch (_) {
    if (node) {
      const r = document.createRange(); r.selectNodeContents(node);
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
    }
    toast("Selected — press ⌘C / Ctrl+C");
  }
}

const fmtCtx = (n) => {
  if (!n) return "?";
  if (n < 1e6) return `${Math.round(n / 1024)}K`;
  const mib = n / 1048576;   // 1,048,576 and 1,000,000 are both "1M" to people
  return Math.abs(mib - Math.round(mib)) < 0.06 ? `${Math.round(mib)}M` : `${+(n / 1e6).toFixed(2)}M`;
};
const fmtDur = (s) => {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), r = s % 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  return m ? `${m}m ${String(r).padStart(2, "0")}s` : `${r}s`;
};
const fmtWhen = (t) => {
  if (!t) return "never";
  const d = new Date(t * 1000), ago = (Date.now() - d) / 1000;
  if (ago < 90) return "just now";
  if (ago < 3600) return `${Math.round(ago / 60)} min ago`;
  if (ago < 86400) return `${Math.round(ago / 3600)} h ago`;
  return d.toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" });
};
const fmtRate = (n) => n >= 100 ? `${Math.round(n)}` : `${+n.toFixed(1)}`;
// Disk sizes the way docker and Hugging Face print them: decimal GB / TB.
const fmtBytes = (n) => {
  if (n == null) return "?";
  for (const [u, v] of [["TB", 1e12], ["GB", 1e9], ["MB", 1e6], ["KB", 1e3]]) {
    if (n >= v) { const x = n / v; return `${x >= 100 ? Math.round(x) : x >= 10 ? x.toFixed(1) : x.toFixed(2)} ${u}`; }
  }
  return `${n} B`;
};

let me = null;        // {username, role}
let session = {};     // version, platform, platform_label, hostname, demo
let isDemo = false;
let activeTab = "models";

/* ───────────── gate: one-time setup, then sign-in ───────────── */

let setupMode = false;

async function boot() {
  let s;
  try { s = await api("/api/session"); } catch (e) { return showGateError(e.message); }
  session = s;
  isDemo = !!s.demo;
  if (s.user) { me = { username: s.user, role: s.role }; return showDash(); }
  setupMode = s.setup_needed;
  $("gate").hidden = false;
  $("dash").hidden = true;
  $("gate-title").textContent = setupMode ? `Welcome to your ${s.platform === "amd-strix-halo" ? "Strix Halo" : "Spark"}` : "Spark Swapper";
  $("gate-sub").textContent = setupMode
    ? "Create the admin login. This happens once — this account can add other users later."
    : "Sign in to view and switch models.";
  $("gate-pass2-wrap").hidden = !setupMode;
  $("gate-pass").autocomplete = setupMode ? "new-password" : "current-password";
  $("gate-btn").textContent = setupMode ? "Create admin login" : "Sign in";
  $("gate-user").focus();
}

function showGateError(msg) { $("gate").hidden = false; $("gate-err").textContent = msg; }

$("gate-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("gate-err").textContent = "";
  const username = $("gate-user").value.trim();
  const password = $("gate-pass").value;
  if (setupMode && password !== $("gate-pass2").value) {
    $("gate-err").textContent = "The two passwords don't match.";
    return;
  }
  $("gate-btn").disabled = true;
  try {
    const r = await api(setupMode ? "/api/setup" : "/api/login", { username, password });
    me = { username, role: r.role || (setupMode ? "admin" : "user") };
    $("gate-pass").value = $("gate-pass2").value = "";
    showDash();
  } catch (e) {
    $("gate-err").textContent = e.message;
    if (e.status === 409) setTimeout(boot, 800); // someone already created it
  } finally {
    $("gate-btn").disabled = false;
  }
});

/* ───────────── app shell / tabs ───────────── */

const TABS = ["models", "storage", "connect", "provision", "users", "settings"];
// Sections inside a tab that links may point at: #vision opens the Connect tab there.
const SECTION_TABS = new Map([["vision", "connect"]]);
// Legacy anchors that older guides and bookmarks still use. The Vision apps were called
// DSH before they were renamed, and their section here was #dsh — keep it working.
const LEGACY_ANCHORS = new Map([["dsh", "vision"]]);
const sectionId = (id) => LEGACY_ANCHORS.get(id) || id;

function showDash() {
  $("gate").hidden = true;
  $("dash").hidden = false;
  const admin = me.role === "admin";
  $("who").textContent = `${me.username}${admin ? " · admin" : ""}`;
  $("tab-users").hidden = !admin;
  $("tab-settings").hidden = !admin;
  $("plat").textContent = session.platform_label || "";
  $("ver").textContent = `Spark Swapper ${session.version || ""}${session.hostname ? ` · ${session.hostname}` : ""}`;
  const wanted = sectionId((location.hash || "").slice(1));
  if (SECTION_TABS.has(wanted)) switchTab(SECTION_TABS.get(wanted), wanted);
  else switchTab(TABS.includes(wanted) && (admin || !["users", "settings"].includes(wanted)) ? wanted : "models");
  if (isDemo) toast("Demo mode — sample data, nothing is actually installed.");
}

function switchTab(name, anchor) {
  activeTab = name;
  for (const t of document.querySelectorAll(".tab")) {
    t.classList.toggle("on", t.dataset.tab === name);
    t.setAttribute("aria-selected", t.dataset.tab === name ? "true" : "false");
  }
  for (const tab of TABS) $(`pane-${tab === "provision" ? "provision" : tab}`).hidden = name !== tab;
  history.replaceState(null, "", `#${name}`);
  if (name === "models") poll(); else stopPoll();
  if (name === "storage") loadStorage(); else clearTimeout(storageTimer);
  if (name === "connect") loadConnect(anchor);
  if (name === "provision") pollProvisioning();
  if (name === "users" && me.role === "admin") loadUsers();
  if (name === "settings" && me.role === "admin") { loadUpdates(); loadHfToken(); }
}

document.querySelectorAll(".tab").forEach((t) =>
  t.addEventListener("click", () => switchTab(t.dataset.tab)));

document.addEventListener("click", (ev) => {
  const a = ev.target.closest("[data-goto]");
  if (!a) return;
  ev.preventDefault();
  const anchor = (a.getAttribute("href") || "").slice(1);
  switchTab(a.dataset.goto, anchor !== a.dataset.goto ? anchor : null);
});

/* ───────────── models (status + swap + install) ───────────── */

let status = null;
let pollTimer = null;
let credsShown = false;
let lastLogLen = -1;
let lastInstallLogLen = -1;

function stopPoll() { clearTimeout(pollTimer); pollTimer = null; }

async function poll() {
  if (activeTab !== "models") { stopPoll(); return; }
  clearTimeout(pollTimer);
  try {
    status = await api("/api/status");
    render();
  } catch (e) {
    if (e.status === 401) return boot();
    $("now-title").textContent = "Can't reach the box";
    $("now-sub").textContent = e.message;
    $("now-dot").className = "pulse bad";
  }
  const busy = status && (status.busy || status.loading || (status.install_job && status.install_job.state === "running"));
  pollTimer = setTimeout(poll, busy ? 1500 : 5000);
}

function modelTitle(key) {
  const m = status && status.models[key];
  return m ? m.title : key;
}

function perfChips(m) {
  const out = [];
  const p = m.perf;
  if (m.measured && m.measured.decode) {
    const c = el("span", "chip measured", `${fmtRate(m.measured.decode)} tok/s measured here`);
    c.title = `Measured by Spark Swapper on this box ${fmtWhen(m.measured.at)} (one request, thinking off).`;
    out.push(c);
  }
  if (p && p.decode) {
    const text = p.estimate ? `≈${p.range || fmtRate(p.decode)} tok/s (estimate)` : `~${fmtRate(p.decode)} tok/s`;
    const c = el("span", `chip speed${p.estimate ? " est" : ""}`, text);
    c.title = `One request at a time. Source: ${p.source || "the recipe"}.`;
    out.push(c);
    if (p.decode_concurrent && p.concurrency) {
      const c2 = el("span", `chip speed${p.estimate ? " est" : ""}`, `${fmtRate(p.decode_concurrent)} tok/s across ${p.concurrency}`);
      c2.title = `Total throughput with ${p.concurrency} requests at once. Source: ${p.source || "the recipe"}.`;
      out.push(c2);
    }
  }
  return out;
}

function render() {
  const s = status;
  if (!s) return;
  const models = Object.values(s.models).sort((a, b) => a.order - b.order);
  const job = s.job;
  const running = job && job.state === "running";
  const installing = s.install_job && s.install_job.state === "running" ? s.install_job : null;

  // update badge + note
  const upd = s.update;
  $("upd-dot").hidden = !(upd && upd.available);
  const note = $("upd-note");
  note.hidden = !(upd && upd.available);
  if (upd && upd.available) {
    note.replaceChildren(document.createTextNode(`Spark Swapper ${upd.latest} is available. `));
    const a = el("a", "", upd.auto_install ? "It installs on Sunday — or update now." : "Update it on the Settings tab.");
    a.href = "#settings"; a.dataset.goto = "settings";
    note.append(a);
  }

  const dot = $("now-dot");
  if (running) {
    dot.className = "pulse busy";
    $("now-eyebrow").textContent = "Switching";
    $("now-title").textContent = `Moving to ${modelTitle(job.target)}`;
    $("now-sub").textContent = "Sit back — this runs on its own. You can close this page.";
  } else if (s.active) {
    const m = s.models[s.active];
    dot.className = "pulse ok";
    $("now-eyebrow").textContent = "Now serving";
    $("now-title").textContent = m.title;
    const rate = m.measured && m.measured.decode ? ` · ${fmtRate(m.measured.decode)} tok/s here` : "";
    $("now-sub").textContent = `${fmtCtx(m.served_context || m.context)} context · ${m.served_id}${rate}`;
  } else if (s.loading) {
    dot.className = "pulse busy";
    $("now-eyebrow").textContent = "Starting up";
    $("now-title").textContent = s.models[s.loading].title;
    $("now-sub").textContent = "Loading weights into memory…";
  } else {
    dot.className = "pulse";
    $("now-eyebrow").textContent = "Idle";
    $("now-title").textContent = "No model is running";
    $("now-sub").textContent = models.some((m) => m.installed) ? "Pick one below to start it." : "Install a model below to get started.";
  }

  const cards = $("cards");
  cards.replaceChildren();
  for (const m of models) cards.append(card(m, s, running, installing));

  renderJob($("progress"), job, {
    title: $("progress-title"), time: $("progress-time"), steps: $("steps"), note: $("progress-note"),
    log: $("log"), logWrap: $("log-wrap"),
  }, "swap");
  const ij = s.install_job;
  const modelJob = ij && (ij.target.startsWith("install:model:") || ij.target.startsWith("delete:")) ? ij : null;
  renderJob($("minstall"), modelJob, {
    title: $("minstall-title"), time: $("minstall-time"), steps: $("minstall-steps"), note: $("minstall-note"),
    log: $("minstall-log"), logWrap: $("minstall-log-wrap"),
  }, "install");
  $("models-toolbar").hidden = !(me && me.role === "admin");

  const disk = s.disk;
  $("disk-meter").hidden = !disk;
  if (disk && disk.total) {
    $("disk-bar").style.width = `${Math.round((disk.total - disk.free) / disk.total * 100)}%`;
    $("disk-bar").parentElement.className = `bar${disk.free < 40e9 ? " bad" : disk.free < 120e9 ? " warn" : ""}`;
    $("disk-text").textContent = `${fmtBytes(disk.free)} free of ${fmtBytes(disk.total)}`;
  }

  const mem = s.memory;
  const used = mem.total ? (mem.total - mem.available) / mem.total : 0;
  $("mem-bar").style.width = `${Math.round(used * 100)}%`;
  $("mem-text").textContent = `${(mem.total - mem.available).toFixed(0)} / ${mem.total.toFixed(0)} GiB`;
  const front = s.front || { up: s.nginx, enforces_key: false };
  $("h-nginx").className = `dot ${front.up ? (front.enforces_key ? "ok" : "warn") : "bad"}`;
  $("h-nginx-label").textContent = front.up && !front.enforces_key ? "HTTPS front :11443 (doesn't check the key)" : "HTTPS front :11443";
  $("h-gw").className = `dot ${s.gateway ? "ok" : "bad"}`;
  $("h-oc").textContent = s.openclaw_primary ? `OpenClaw → ${s.openclaw_primary}` : "";
  const gpu = $("h-gpu");
  gpu.hidden = !s.gpu;
  if (s.gpu) gpu.textContent = `GPU may use ${Math.round(s.gpu.total_gib)} GiB`;
}

function card(m, s, running, installing) {
  const c = el("article", "card");
  const isActive = s.active === m.key && !running;
  const isLoading = (running && s.job.target === m.key) || (!running && s.loading === m.key);
  const isInstalling = installing && installing.target === `install:model:${m.key}`;
  if (isActive) c.classList.add("active");
  if (isLoading || isInstalling) c.classList.add("loading");
  if (!m.installed) c.classList.add("missing");

  const head = el("div", "head");
  head.append(el("h3", "", m.title));
  const tags = el("div", "tags");
  if (m.recommended) tags.append(el("span", "tag", "Recommended"));
  else if (m.superseded_by) tags.append(el("span", "tag old", "Older recipe"));
  if (m.experimental) tags.append(el("span", "tag exp", "Experimental"));
  if (tags.childNodes.length) head.append(tags);
  c.append(head, el("div", "engine", m.engine), el("p", "muted", m.tagline));

  const chips = el("div", "chips");
  chips.append(el("span", "chip", `${fmtCtx(m.context)} context`));
  for (const p of perfChips(m)) chips.append(p);
  if (m.vision) chips.append(el("span", "chip", "Images & video"));
  if (m.disk_bytes) chips.append(el("span", "chip disk", `${fmtBytes(m.disk_bytes)} on disk`));
  if (isActive) chips.append(el("span", "chip on", "Serving now"));
  if (isLoading) chips.append(el("span", "chip wait", "Loading"));
  if (isInstalling) chips.append(el("span", "chip wait", "Installing"));
  else if (!m.installed) chips.append(el("span", "chip off", "Not installed"));
  c.append(chips);

  const more = el("details");
  more.append(el("summary", "", "Details"));
  const p = m.perf || {};
  const lines = [];
  if (p.prefill) lines.push(`Reads prompts at about ${Math.round(p.prefill).toLocaleString()} tokens/s.`);
  if (p.code) lines.push(`On code it decodes at about ${fmtRate(p.code)} tok/s.`);
  if (p.source) lines.push(`Speeds: ${p.source}.`);
  if (m.eta) lines.push(`Switching to it takes ${m.eta}.`);
  if (!m.installed && m.install && m.install.time) lines.push(`Install: ${m.install.time}${m.install.size_gb ? ` (~${m.install.size_gb} GB of disk)` : ""}.`);
  if (!m.installed && m.install_detail) lines.push(`Missing: ${m.install_detail}.`);
  if (m.anthropic_api) lines.push("Also speaks the Anthropic Messages API (for Claude Code and similar tools).");
  lines.push(...(m.notes || []));
  for (const l of lines) more.append(el("p", "", l));
  if (m.repo) {
    const a = el("a", "", "The recipe ↗");
    a.href = m.repo; a.target = "_blank"; a.rel = "noopener noreferrer";
    const pa = el("p"); pa.append(a); more.append(pa);
  }
  c.append(more, el("div", "spacer"));

  const actions = el("div", "actions");
  if (m.installed) {
    const b = el("button", isActive ? "btn ghost" : "btn primary",
      isActive ? "Running" : isLoading ? "Loading…" : "Switch to this");
    b.disabled = isActive || running;
    b.addEventListener("click", () => confirmSwap(m));
    actions.append(b);
  } else {
    const b = el("button", "btn primary", isInstalling ? "Installing…" : "Install");
    b.disabled = !!installing;
    b.addEventListener("click", () => confirmInstall(m));
    actions.append(b);
    if (m.install && (m.install.size_gb || m.install.time)) {
      actions.append(el("span", "meta", [m.install.size_gb ? `~${m.install.size_gb} GB` : "", (m.install.time || "").split(":")[0]].filter(Boolean).join(" · ")));
    }
  }
  if (me && me.role === "admin" && (m.installed || m.disk_bytes > 0 || m.custom)) {
    const d = el("button", "btn ghost small danger", m.installed || m.disk_bytes > 0 ? "Delete…" : "Remove…");
    d.disabled = isActive || isLoading || running || !!installing;
    d.title = isActive ? "Switch to another model first." : "Take it off this box and free its disk space.";
    d.addEventListener("click", () => deleteModel(m));
    actions.append(d);
  }
  c.append(actions);
  return c;
}

/* "Installing X", "Deleted X", "Couldn't remove Y": the headline of an install, delete or removal job. */
function jobTitle(job) {
  const t = job.target || "";
  const run = job.state === "running", done = job.state === "done";
  if (t.startsWith("install:model:")) {
    const n = modelTitle(t.slice("install:model:".length));
    return run ? `Installing ${n}` : done ? `Installed ${n}` : `Couldn't install ${n}`;
  }
  if (t.startsWith("delete:")) {
    const n = modelTitle(t.slice("delete:".length));
    return run ? `Deleting ${n}` : done ? `Deleted ${n}` : `Couldn't delete ${n}`;
  }
  if (t.startsWith("delete-other:")) {
    const [, kind, ...rest] = t.split(":");
    const n = kind === "builder" ? "Docker's build cache" : rest.join(":");
    return run ? `Removing ${n}` : done ? `Removed ${n}` : `Couldn't remove ${n}`;
  }
  const name = t.replace("install:", "").replace("model:", "model ");
  return run ? `Installing ${name}` : done ? `Finished: ${name}` : `Failed: ${name}`;
}

function renderJob(box, job, ids, kind) {
  if (!job) { box.hidden = true; return; }
  box.hidden = false;
  let title;
  if (kind === "swap") {
    const t = modelTitle(job.target);
    title = job.state === "running" ? `Switching to ${t}` : job.state === "done" ? `Switched to ${t}` : `Couldn't switch to ${t}`;
  } else {
    title = jobTitle(job);
  }
  ids.title.textContent = title;
  const end = job.finished || Date.now() / 1000;
  const m = status && status.models[job.target];
  ids.time.textContent = (job.state === "running" ? "Elapsed " : "Took ") + fmtDur(end - job.started)
    + (kind === "swap" && job.state === "running" && m && m.eta ? ` · usually ${m.eta}` : "");

  ids.steps.replaceChildren();
  for (const st of job.steps) {
    const li = el("li", st.state);
    li.append(el("span", "ic"));
    const body = el("div");
    body.append(el("div", "", st.label));
    if (st.detail) body.append(el("div", "d", st.detail));
    li.append(body);
    if (st.started) {
      const e = (st.finished || Date.now() / 1000) - st.started;
      if (st.state !== "pending" && e >= 1) li.append(el("span", "t", fmtDur(e)));
    }
    ids.steps.append(li);
  }

  const msg = job.state === "failed" ? [job.error, job.note].filter(Boolean).join(" ") : job.note;
  ids.note.hidden = !msg || (job.state === "running" && !job.note);
  ids.note.className = `note${job.state === "failed" ? " bad" : ""}`;
  ids.note.textContent = msg || "";

  const last = kind === "swap" ? lastLogLen : kind === "storage" ? stLastLogLen : lastInstallLogLen;
  if (job.log_total !== last) {
    if (kind === "swap") lastLogLen = job.log_total;
    else if (kind === "storage") stLastLogLen = job.log_total;
    else lastInstallLogLen = job.log_total;
    writeLog(ids.log, job.log);
  }
  if (job.state === "failed") ids.logWrap.open = true;
}

function writeLog(pre, log) {
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
  pre.textContent = log.map((l) => {
    const d = new Date(l.t * 1000);
    return `${d.toLocaleTimeString([], { hour12: false })}  ${l.m}`;
  }).join("\n");
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

function ask(title, body, okLabel) {
  return new Promise((resolve) => {
    $("confirm-title").textContent = title;
    $("confirm-body").textContent = body;
    $("confirm-ok").textContent = okLabel || "Confirm";
    const d = $("confirm");
    d.returnValue = "";
    d.showModal();
    d.addEventListener("close", function once() {
      d.removeEventListener("close", once);
      resolve(d.returnValue === "ok");
    });
  });
}

async function confirmSwap(m) {
  const cur = status.active ? status.models[status.active].title : null;
  const ok = await ask(`Switch to ${m.title}?`,
    (cur ? `${cur} will stop, then ` : "") +
    `${m.title} loads (${m.eta || "a few minutes"}). OpenClaw is repointed automatically, and the API at :11443 keeps the same address and key. ` +
    "If the new model fails to start, the previous one is brought back.", "Switch");
  if (!ok) return;
  try {
    await api("/api/swap", { target: m.key });
    lastLogLen = -1;
    toast(`Switching to ${m.title}…`);
    poll();
  } catch (e) { toast(e.message); }
}

async function confirmInstall(m) {
  const i = m.install || {};
  const ok = await ask(`Install ${m.title}?`,
    `${i.time || "This downloads the model and sets it up."}${i.size_gb ? ` It needs about ${i.size_gb} GB of free disk.` : ""} ` +
    "It runs on the box by itself — you can close this page — and the model that's serving now keeps serving." +
    (i.note ? ` ${i.note}` : ""), "Install");
  if (!ok) return;
  try {
    await api("/api/models/install", { key: m.key });
    lastInstallLogLen = -1;
    toast(`Installing ${m.title}…`);
    poll();
  } catch (e) { toast(e.message); }
}

/* ───────────── storage: disk space, deleting models ───────────── */

let storageTimer = null;
let stLastLogLen = -1;

function showDialog(d) {
  return new Promise((resolve) => {
    d.returnValue = "";
    d.showModal();
    d.addEventListener("close", function once() {
      d.removeEventListener("close", once);
      resolve(d.returnValue === "ok");
    });
  });
}

async function loadStorage(fresh) {
  clearTimeout(storageTimer);
  if (activeTab !== "storage") return;
  let data, job;
  try {
    [data, job] = await Promise.all([api(`/api/storage${fresh ? "?fresh=1" : ""}`), api("/api/provisioning/job").then((r) => r.job)]);
  } catch (e) {
    if (e.status === 401) return boot();
    toast(e.message);
    return;
  }
  renderStorage(data);
  renderStorageJob(job);
  const busy = data.scanning || (job && job.state === "running");
  storageTimer = setTimeout(() => loadStorage(false), busy ? 1500 : 20000);
}

$("btn-rescan").addEventListener("click", () => { toast("Measuring…"); loadStorage(true); });

function renderStorageJob(job) {
  const recent = job && (job.state === "running" || Date.now() / 1000 - (job.finished || 0) < 600);
  renderJob($("st-job"), recent ? job : null, {
    title: $("st-job-title"), time: $("st-job-time"), steps: $("st-job-steps"), note: $("st-job-note"),
    log: $("st-job-log"), logWrap: $("st-job-log-wrap"),
  }, "storage");
}

const PART_KIND = { weights: "Model weights", image: "Docker image", recipe: "Recipe (git checkout)", cache: "Caches and state" };

function renderStorage(d) {
  const adm = me && me.role === "admin";
  const modelsBytes = d.models_total || 0;
  const otherBytes = (d.other || []).filter((o) => o.kind !== "builder").reduce((a, o) => a + (o.bytes || 0), 0);
  const home = Math.max(0, d.disks.findIndex((x) => x.roles.some((r) => r.label.startsWith("Hugging Face"))));

  const box = $("st-disks");
  box.replaceChildren();
  d.disks.forEach((disk, i) => {
    const w = el("div", "st-disk");
    const line = el("div", "st-line");
    line.append(rich("span", `**${disk.mount}** · ${fmtBytes(disk.used)} used of ${fmtBytes(disk.total)}`),
      el("span", disk.free < 40e9 ? "neg" : "muted", `${fmtBytes(disk.free)} free`));
    const models = i === home ? modelsBytes : 0, other = i === home ? otherBytes : 0;
    const system = Math.max(0, disk.used - models - other);
    const bar = el("div", "st-bar");
    for (const [cls, n] of [["seg-models", models], ["seg-other", other], ["seg-system", system]]) {
      const seg = el("i", cls);
      seg.style.width = `${Math.min(100, n / disk.total * 100).toFixed(2)}%`;
      bar.append(seg);
    }
    const legend = el("div", "st-legend");
    legend.append(el("span", "l-models", `Models ${fmtBytes(models)}`),
      el("span", "l-other", `Other model files ${fmtBytes(other)}`),
      el("span", "l-system", `Everything else ${fmtBytes(system)}`));
    w.append(line, bar, legend, el("div", "st-roles", disk.roles.map((r) => `${r.label}: ${r.path}`).join(" · ")));
    box.append(w);
  });
  if (!d.disks.length) box.append(el("p", "muted", "Couldn't read the disks."));

  const dock = d.docker && d.docker.Images
    ? ` Docker images take ${fmtBytes(d.docker.Images.bytes)} (${fmtBytes(d.docker.Images.reclaimable)} of it isn't used by a container).` : "";
  $("st-status").textContent = d.scanning ? "Measuring what each model takes… this can take a minute on a big drive."
    : d.error ? `Couldn't finish measuring: ${d.error}`
      : d.at ? `Measured ${fmtWhen(d.at)}.${dock}` : "";

  const list = $("st-models");
  list.replaceChildren();
  const ms = (d.models || []).filter((m) => m.bytes > 0).sort((a, b) => b.bytes - a.bytes);
  if (!ms.length) list.append(el("p", "muted", d.scanning || !d.at ? "Measuring…" : "No models are installed yet."));
  const max = Math.max(1, ...ms.map((m) => m.bytes));
  for (const m of ms) {
    const row = el("div", "st-model");
    const line = el("div", "line");
    const name = el("div");
    name.append(el("span", "name", m.title));
    if (m.custom) name.append(document.createTextNode(" "), el("span", "chip", "Added by you"));
    line.append(name, el("div", "size", fmtBytes(m.bytes)));
    if (adm) {
      const del = el("button", "btn ghost small danger", m.bytes_freed > 0 ? `Free up ${fmtBytes(m.bytes_freed)}` : "Delete…");
      del.title = "See what would be removed before anything is.";
      del.addEventListener("click", () => deleteModel({ key: m.key, title: m.title, custom: m.custom }));
      line.append(del);
    }
    const mini = el("div", "minibar");
    const fill = el("i");
    fill.style.width = `${(m.bytes / max * 100).toFixed(1)}%`;
    mini.append(fill);
    const more = el("details");
    more.append(el("summary", "", `${m.parts.length} thing${m.parts.length === 1 ? "" : "s"} it uses`));
    const ul = el("ul", "st-parts");
    for (const p of m.parts) {
      const li = el("li");
      const left = el("span", "", `${PART_KIND[p.kind] || p.kind}: ${p.label}`);
      if (p.shared_with.length) left.append(el("span", "share", `also: ${p.shared_with.join(", ")}`));
      li.append(left, el("span", "", fmtBytes(p.bytes)));
      ul.append(li);
    }
    more.append(ul);
    row.append(line, mini, more);
    list.append(row);
  }

  const other = (d.other || []);
  $("st-other-panel").hidden = !other.length;
  const ob = $("st-other");
  ob.replaceChildren();
  for (const o of other) {
    const row = el("div", "st-other-row");
    const name = el("div");
    name.append(el("div", "name", o.label));
    name.append(el("div", "muted small", [o.kind === "hf" ? "Downloaded weights" : o.kind === "image" ? "Docker image" : "Docker", o.note].filter(Boolean).join(" · ")));
    row.append(name, el("div", "size", fmtBytes(o.bytes)));
    if (adm && o.removable) {
      const rm = el("button", "btn ghost small danger", "Remove…");
      rm.addEventListener("click", () => removeOther(o));
      row.append(rm);
    } else row.append(el("span"));
    ob.append(row);
  }
}

function listInto(box, heading, items, keep) {
  box.replaceChildren();
  if (!items.length) return;
  box.append(el("h4", "", heading));
  const ul = el("ul", "del-list");
  for (const p of items) {
    const li = el("li");
    const left = el("span");
    left.append(document.createTextNode(p.label));
    const why = [PART_KIND[p.kind] || p.kind];
    if (keep && p.kept_for && p.kept_for.length) why.push(`also used by ${p.kept_for.join(", ")}`);
    left.append(el("span", "why", why.join(" · ")));
    li.append(left, el("span", "", fmtBytes(p.bytes)));
    ul.append(li);
  }
  box.append(ul);
}

async function deleteModel(m) {
  let plan;
  try { plan = await api(`/api/storage/plan?key=${encodeURIComponent(m.key)}`); }
  catch (e) { return toast(e.message); }
  const files = plan.remove.length > 0;
  $("del-title").textContent = `${files || plan.installed ? "Delete" : "Remove"} ${plan.title}?`;
  $("del-blockers").replaceChildren(...plan.blockers.map((b) => el("p", "note bad slim", b)));
  $("del-intro").textContent = files
    ? `This takes it off the box and frees about ${fmtBytes(plan.bytes)}. Its measured speed is forgotten${plan.custom ? ", it leaves the list of models," : ""} and its entry in OpenClaw's config is tidied. You can install it again later.`
    : plan.custom ? "Nothing is downloaded for it; it only leaves the list of models." : "None of its files are on the disk, so there's nothing to free.";
  listInto($("del-remove"), "Will be removed", plan.remove, false);
  listInto($("del-keep"), "Stays, because another installed model uses it", plan.keep, true);
  const ok = $("del-ok");
  ok.disabled = plan.blockers.length > 0;
  ok.textContent = files ? `Delete and free ${fmtBytes(plan.bytes)}` : "Remove";
  if (!await showDialog($("del"))) return;
  try {
    await api("/api/models/delete", { key: m.key });
    toast(`Deleting ${plan.title}…`);
    lastInstallLogLen = stLastLogLen = -1;
    if (activeTab === "storage") loadStorage(true); else poll();
  } catch (e) { toast(e.message); }
}

async function removeOther(o) {
  const what = o.kind === "builder" ? "Docker's build cache" : o.label;
  const why = o.kind === "hf" ? `This deletes the downloaded weights (${fmtBytes(o.bytes)}). You can download them again later.`
    : o.kind === "image" ? `Removes this docker image (${fmtBytes(o.bytes)}).`
      : "Clears the layers docker keeps from earlier image builds. Nothing that's installed is affected; the next build is just slower.";
  if (!await ask(`Remove ${what}?`, why, "Remove")) return;
  try {
    await api("/api/storage/remove", { kind: o.kind, name: o.name });
    toast("Removing…");
    stLastLogLen = -1;
    loadStorage(true);
  } catch (e) { toast(e.message); }
}

/* ───────────── add a model from Hugging Face ───────────── */

let hfInfo = null;
let hfUrl = "";
let hfAction = null;

$("btn-hf").addEventListener("click", () => {
  $("hf-url").value = "";
  $("hf-err").textContent = "";
  $("hf-result").replaceChildren();
  $("hf-add").hidden = true;
  hfInfo = hfAction = null;
  $("hf").showModal();
  $("hf-url").focus();
});
$("hf-look").addEventListener("click", hfLookup);
// Enter in any field of the form must not fall through to its first submit button ("Close").
$("hf-form").addEventListener("keydown", (ev) => {
  if (ev.key !== "Enter" || ev.target.tagName !== "INPUT" || ev.target.type === "checkbox") return;
  ev.preventDefault();
  if (ev.target.id === "hf-url") hfLookup();
});

async function hfLookup() {
  const url = $("hf-url").value.trim();
  $("hf-err").textContent = "";
  $("hf-add").hidden = true;
  $("hf-result").replaceChildren();
  hfInfo = hfAction = null;
  if (!url) return;
  const look = $("hf-look");
  look.disabled = true;
  look.textContent = "Looking…";
  try {
    hfUrl = url;
    hfInfo = await api("/api/hf/inspect", { url });
    renderHf(hfInfo);
  } catch (e) { $("hf-err").textContent = e.message; }
  finally { look.disabled = false; look.textContent = "Look it up"; }
}

function renderHf(r) {
  const box = $("hf-result");
  box.replaceChildren();
  const card = el("div", "hf-card");
  card.append(el("h4", "", r.title));
  const link = el("a", "sub", `huggingface.co/${r.repo}`);
  link.href = `https://huggingface.co/${r.repo}`; link.target = "_blank"; link.rel = "noopener noreferrer";
  card.append(link);
  const kv = el("dl", "kv");
  const row = (k, v) => { if (v) kv.append(el("dt", "", k), el("dd", "", v)); };
  row("Format", [r.quantization || r.format, r.architecture].filter(Boolean).join(" · "));
  row("Size", r.gguf && r.gguf.length
    ? `${fmtBytes(r.size_bytes)} to ${fmtBytes(r.size_max_bytes)}, depending on the quantization you pick`
    : `${fmtBytes(r.size_bytes)} in ${r.files} files`);
  row("Context", r.context_native ? `${fmtCtx(r.context_native)} tokens (the model's own limit)` : "");
  row("License", r.license);
  const small = r.gguf && r.gguf.length ? " at the smallest quantization" : "";
  row("Memory", r.memory_gib ? `about ${r.memory_gib} GiB needed${small}${r.memory_total_gib ? `, this box has ${Math.round(r.memory_total_gib)}` : ""}` : "");
  row("Disk", `${fmtBytes(r.size_bytes)} to download${small}, ${fmtBytes(r.disk_free)} free`);
  row("Runs on", r.profile_label || "no engine here can run it");
  card.append(kv);
  box.append(card);
  for (const w of r.warnings) box.append(el("p", "note warn slim", w));
  for (const p of r.problems) box.append(el("p", "note bad slim", p));

  const add = $("hf-add");
  if (r.catalog_key) {
    box.append(rich("p", `**${r.catalog_title}** in the model list already runs this${r.catalog_installed ? " and it's installed." : "."}`, "note slim"));
    add.textContent = r.catalog_installed ? "Switch to it" : `Install ${r.catalog_title}`;
    add.hidden = false;
    hfAction = { type: r.catalog_installed ? "switch" : "install", key: r.catalog_key };
    return;
  }
  if (!r.supported) return;

  const opts = el("div", "hf-opts");
  const field = (label, input) => { const l = el("label", "", label); l.append(input); return l; };
  const title = el("input"); title.id = "hf-title"; title.value = r.title; title.maxLength = 80;
  opts.append(field("Name on the card", title));
  const ctx = el("input"); ctx.id = "hf-ctx"; ctx.type = "number"; ctx.step = "1024"; ctx.min = "2048";
  ctx.value = r.context_default; if (r.context_native) ctx.max = r.context_native;
  opts.append(field(`Context window (tokens${r.context_native ? `, up to ${r.context_native.toLocaleString()}` : ""}) — bigger uses more memory`, ctx));
  if (r.gguf && r.gguf.length) {
    const sel = el("select"); sel.id = "hf-gguf";
    for (const g of r.gguf) sel.append(Object.assign(el("option", "", `${g.label} — ${fmtBytes(g.bytes)}`), { value: g.id }));
    sel.selectedIndex = Math.floor(r.gguf.length / 2);
    opts.append(field("Which file", sel));
  }
  if (r.profile === "vllm-spark") {
    const adv = el("details");
    adv.append(el("summary", "", "Advanced: more vLLM arguments"));
    const extra = el("input"); extra.id = "hf-extra"; extra.placeholder = "--tool-call-parser hermes --enable-auto-tool-choice";
    extra.spellcheck = false;
    adv.append(field("Extra arguments for vllm serve", extra));
    const trust = el("input"); trust.id = "hf-trust"; trust.type = "checkbox";
    const tl = el("label", "check"); tl.append(trust, document.createTextNode(" Let the model run its own code (trust remote code). Only for repos you trust."));
    adv.append(tl);
    opts.append(adv);
  }
  box.append(opts);
  add.textContent = "Add and install";
  add.hidden = false;
  hfAction = { type: "add" };
}

$("hf-add").addEventListener("click", async (ev) => {
  ev.preventDefault();
  if (!hfAction || !hfInfo) return;
  const btn = $("hf-add");
  btn.disabled = true;
  $("hf-err").textContent = "";
  try {
    if (hfAction.type === "switch") {
      $("hf").close();
      return confirmSwap(status.models[hfAction.key]);
    }
    if (hfAction.type === "install") {
      await api("/api/models/install", { key: hfAction.key });
    } else {
      const body = { url: hfUrl, title: $("hf-title").value.trim(), context: +$("hf-ctx").value || undefined };
      if ($("hf-gguf")) body.gguf = $("hf-gguf").value;
      if ($("hf-extra")) { body.extra_args = $("hf-extra").value; body.trust_remote_code = $("hf-trust").checked; }
      await api("/api/hf/add", body);
    }
    $("hf").close();
    lastInstallLogLen = -1;
    toast("Installing — it runs on the box by itself; you can close this page.");
    switchTab("models");
  } catch (e) {
    $("hf-err").textContent = e.message;
    poll();
  } finally { btn.disabled = false; }
});

async function loadHfToken() {
  try {
    const r = await api("/api/hf/token");
    $("hf-token-status").textContent = r.set ? `A token is set (${r.source}).` : "No token is set. Public models don't need one.";
    $("btn-hf-token-clear").hidden = r.source !== "settings";
  } catch (e) { if (e.status === 401) boot(); }
}

$("btn-hf-token").addEventListener("click", async () => {
  try {
    await api("/api/hf/token", { token: $("hf-token").value.trim() });
    $("hf-token").value = "";
    toast("Saved.");
    loadHfToken();
  } catch (e) { toast(e.message); }
});

$("btn-hf-token-clear").addEventListener("click", async () => {
  try { await api("/api/hf/token", { token: "" }); toast("Removed."); loadHfToken(); }
  catch (e) { toast(e.message); }
});

/* ───────────── connect ───────────── */

let connectInfo = null;
let toolKey = "env";

async function loadConnect(anchor) {
  try {
    const [info, toks] = await Promise.all([api("/api/connect"), api("/api/tokens")]);
    connectInfo = info;
    renderApiRows(info);
    renderTools();
    renderTokens(toks.tokens || []);
    $("fp").textContent = info.fingerprint || "(no certificate yet)";
    const target = anchor && $(sectionId(anchor));
    if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    if (e.status === 401) return boot();
    toast(e.message);
  }
}

function credRow(label, value, { link = false, src = "" } = {}) {
  const row = el("div", "cred");
  row.append(el("div", "k", label));
  const code = el("code", "", value || "—");
  row.append(code);
  const actions = el("div", "row");
  if (value) {
    const copy = el("button", "btn ghost small", "Copy");
    copy.addEventListener("click", () => copyText(value, code));
    actions.append(copy);
    if (link) {
      const a = el("a", "btn ghost small", "Open");
      a.href = value; a.target = "_blank"; a.rel = "noopener noreferrer";
      actions.append(a);
    }
  }
  row.append(actions);
  if (src) row.append(el("div", "src", src));
  return row;
}

function renderApiRows(info) {
  const weak = info.api_key === "vllm-local";
  const warn = el("p", "note slim warn");
  warn.hidden = !weak;
  warn.textContent = "This box uses the default API key, vllm-local, which anyone can guess. Set your own \"api_key\" in " +
    "/etc/spark-swapper/models.json, restart Spark Swapper (sudo systemctl restart spark-swapper), run Provisioning ▸ " +
    "HTTPS front again, then switch models once.";
  $("api-rows").replaceChildren(warn,
    credRow("Base URL", info.api_base, { src: "OpenAI-compatible · HTTPS with this box's certificate" }),
    credRow("API key", info.api_key, { src: "Required on every request (Authorization: Bearer …)" }),
    credRow("Model ID", info.model || "", { src: info.model ? `Serving now${info.context ? ` · ${fmtCtx(info.context)} context` : ""} — it changes when you switch models` : "No model is serving right now" }),
  );
}

$("btn-test").addEventListener("click", async () => {
  const box = $("test-result");
  box.hidden = false;
  box.className = "note slim";
  box.textContent = "Testing…";
  try {
    const r = await api("/api/connect/test");
    const parts = [];
    if (r.with_key === 200) parts.push(`✓ With the key it answers${r.models.length ? ` (${r.models.join(", ")})` : ""}.`);
    else if (r.with_key === 502 || r.with_key === 503) parts.push("The front answers, but no model is serving right now.");
    else if (r.with_key == null) parts.push(`✗ Nothing answers on :${r.port}. Set up the HTTPS front on the Provisioning tab.`);
    else parts.push(`✗ With the key it answered ${r.with_key}.`);
    if (r.without_key === 401) parts.push("✓ Without the key it's refused, as it should be.");
    else if (r.without_key != null) parts.push(`⚠ Without the key it answered ${r.without_key} — anyone on your network can use it. Run Provisioning ▸ HTTPS front.`);
    box.className = `note slim${parts.some((p) => p.startsWith("✗") || p.startsWith("⚠")) ? " warn" : ""}`;
    box.textContent = parts.join(" ");
  } catch (e) { box.className = "note slim bad"; box.textContent = e.message; }
});

$("btn-code").addEventListener("click", async () => {
  const label = $("tok-label").value.trim() || "Vision";
  try {
    const r = await api("/api/tokens/create", { label });
    $("code").value = r.code;
    $("code-box").hidden = false;
    $("code").focus(); $("code").select();
    $("tok-label").value = "";
    const toks = await api("/api/tokens");
    renderTokens(toks.tokens || []);
  } catch (e) { toast(e.message); }
});

$("btn-copy-code").addEventListener("click", () => copyText($("code").value, $("code")));

function renderTokens(list) {
  const body = $("tok-body");
  body.replaceChildren();
  $("tok-empty").hidden = list.length > 0;
  $("tok-table").hidden = list.length === 0;
  for (const t of list) {
    const tr = el("tr");
    tr.append(el("td", "", t.label), el("td", "muted", t.user), el("td", "muted", fmtWhen(t.created)), el("td", "muted", fmtWhen(t.last_used)));
    const td = el("td", "row end");
    const rm = el("button", "btn ghost small danger", "Revoke");
    rm.addEventListener("click", async () => {
      if (!await ask(`Revoke “${t.label}”?`, "That app stops working with this box until you give it a new connection code.", "Revoke")) return;
      try { await api("/api/tokens/revoke", { id: t.id }); toast("Revoked."); loadConnect(); }
      catch (e) { toast(e.message); }
    });
    td.append(rm);
    tr.append(td);
    body.append(tr);
  }
}

function snip(code, caption) {
  const wrap = el("div", "snip");
  if (caption) wrap.append(el("div", "cap", caption));
  const pre = el("pre", "", code);
  const b = el("button", "btn ghost small", "Copy");
  b.addEventListener("click", () => copyText(code, pre));
  wrap.append(pre, b);
  return wrap;
}

function steps(items) {
  const ol = el("ol");
  for (const i of items) ol.append(rich("li", i));
  return ol;
}

/* Each tool: label + a function building its instructions from the live values. */
const TOOLS = [
  { key: "env", label: "Any tool (settings)", build: (v) => [
    rich("p", "Most tools ask for the same three things. Paste these where the tool asks for an **OpenAI-compatible** provider:"),
    snip(`Base URL:  ${v.base}\nAPI key:   ${v.key}\nModel:     ${v.model}`),
    rich("p", "Tools that read the standard environment variables pick them up from your shell:"),
    snip(`export OPENAI_BASE_URL="${v.base}"\nexport OPENAI_API_KEY="${v.key}"\nexport OPENAI_MODEL="${v.model}"`, "macOS / Linux"),
    snip(`setx OPENAI_BASE_URL "${v.base}"\nsetx OPENAI_API_KEY "${v.key}"\nsetx OPENAI_MODEL "${v.model}"`, "Windows (new terminals pick these up)"),
  ] },
  { key: "vision", label: "Vision", build: () => [
    rich("p", "Vision for Mac and Vision for Windows connect with a **connection code** — no copying of addresses or keys, and they can switch models on this box for you."),
    (() => { const p = el("p"); const a = el("a", "", "Create a connection code ↑"); a.href = "#vision"; a.dataset.goto = "connect"; p.append(a); return p; })(),
  ] },
  { key: "curl", label: "curl", build: (v) => [
    snip(`curl --cacert spark-swapper.crt ${v.base}/chat/completions \\\n  -H "Authorization: Bearer ${v.key}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model": "${v.model}", "messages": [{"role": "user", "content": "Hello!"}]}'`),
    rich("p", "Download the certificate first (button at the bottom of this page), or leave out `--cacert` once your computer trusts it."),
  ] },
  { key: "python", label: "Python", build: (v) => [
    snip("pip install openai httpx", "Install"),
    snip(`import httpx\nfrom openai import OpenAI\n\nclient = OpenAI(\n    base_url="${v.base}",\n    api_key="${v.key}",\n    http_client=httpx.Client(verify="spark-swapper.crt"),  # this box's certificate\n)\nreply = client.chat.completions.create(\n    model="${v.model}",\n    messages=[{"role": "user", "content": "Hello!"}],\n)\nprint(reply.choices[0].message.content)`),
  ] },
  { key: "node", label: "JavaScript", build: (v) => [
    snip("npm install openai", "Install"),
    snip(`import OpenAI from "openai";\n\nconst client = new OpenAI({\n  baseURL: "${v.base}",\n  apiKey: "${v.key}",\n});\nconst reply = await client.chat.completions.create({\n  model: "${v.model}",\n  messages: [{ role: "user", content: "Hello!" }],\n});\nconsole.log(reply.choices[0].message.content);`, "app.mjs"),
    snip("NODE_EXTRA_CA_CERTS=./spark-swapper.crt node app.mjs", "Run it (Node keeps its own certificate list)"),
  ] },
  { key: "continue", label: "Continue", build: (v) => [
    rich("p", "In VS Code or JetBrains, open Continue's **config.yaml** (the gear icon ▸ Open configuration file) and add:"),
    snip(`models:\n  - name: ${v.model} on ${v.boxName}\n    provider: openai\n    model: ${v.model}\n    apiBase: ${v.base}\n    apiKey: ${v.key}\n    roles: [chat, edit, apply]\n    requestOptions:\n      caBundlePath: /path/to/spark-swapper.crt`),
  ] },
  { key: "cline", label: "Cline · Roo · Kilo", build: (v) => [
    steps([
      "Open the extension's settings (the gear icon in its panel).",
      "API Provider: **OpenAI Compatible**.",
      `Base URL: \`${v.base}\``,
      `API Key: \`${v.key}\``,
      `Model ID: \`${v.model}\``,
      v.context ? `Context window: \`${v.context}\`` : "Context window: the model's, from the Models tab.",
      "Trust this box's certificate on your computer (below) — VS Code extensions use the system's trusted certificates.",
    ]),
  ] },
  { key: "aider", label: "Aider", build: (v) => [
    snip(`export SSL_CERT_FILE="$HOME/spark-swapper.crt"   # or trust it system-wide\naider --openai-api-base ${v.base} \\\n      --openai-api-key ${v.key} \\\n      --model openai/${v.model}`),
  ] },
  { key: "opencode", label: "OpenCode", build: (v) => [
    rich("p", "Add a provider to **opencode.json** (in your project or `~/.config/opencode/`):"),
    snip(JSON.stringify({
      $schema: "https://opencode.ai/config.json",
      provider: { spark: { npm: "@ai-sdk/openai-compatible", name: v.boxName, options: { baseURL: v.base, apiKey: v.key },
        models: { [v.model]: { name: v.model } } } },
    }, null, 2)),
    snip(`export NODE_EXTRA_CA_CERTS="$HOME/spark-swapper.crt"\nopencode`, "Then start it and pick the model with /models"),
  ] },
  { key: "qwen", label: "Qwen Code", build: (v) => [
    snip(`export OPENAI_BASE_URL="${v.base}"\nexport OPENAI_API_KEY="${v.key}"\nexport OPENAI_MODEL="${v.model}"\nexport NODE_EXTRA_CA_CERTS="$HOME/spark-swapper.crt"\nqwen`),
  ] },
  { key: "claude", label: "Claude Code", build: (v) => v.anthropic ? [
    rich("p", `The model serving now also speaks the Anthropic Messages API, so Claude Code can use it:`),
    snip(`export ANTHROPIC_BASE_URL="${v.base.replace(/\/v1$/, "")}"\nexport ANTHROPIC_AUTH_TOKEN="${v.key}"\nexport ANTHROPIC_MODEL="${v.model}"\nexport NODE_EXTRA_CA_CERTS="$HOME/spark-swapper.crt"\nclaude`),
    rich("p", "This works only while a model that speaks that API is serving (**Qwen3.8 Flash Next** or **Nemotron 3.5 Bolt**, both on TensorFold). With other models, use an OpenAI-compatible tool."),
  ] : [
    rich("p", "Claude Code speaks the Anthropic Messages API. Of the models here, **Qwen3.8 Flash Next** and **Nemotron 3.5 Bolt** (both TensorFold) serve it — switch to one of them and this tab shows the settings."),
  ] },
  { key: "openclaw", label: "OpenClaw", build: () => [
    rich("p", "Nothing to set up: whenever you switch models, Spark Swapper points OpenClaw at the new one (its primary, image, compaction and memory-flush models) and restarts the gateway."),
    rich("p", "Its dashboard sign-in link is under **Keys & connection** on the Models tab."),
  ] },
];

function renderTools() {
  const info = connectInfo;
  if (!info) return;
  const v = {
    base: info.api_base, key: info.api_key, model: info.model || "<model id>", context: info.context,
    boxName: info.name || "Spark", anthropic: !!info.anthropic_api,
  };
  const pills = $("tool-pills");
  pills.replaceChildren();
  for (const t of TOOLS) {
    const b = el("button", `pill${t.key === toolKey ? " on" : ""}`, t.label);
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", t.key === toolKey ? "true" : "false");
    b.addEventListener("click", () => { toolKey = t.key; renderTools(); });
    pills.append(b);
  }
  const tool = TOOLS.find((t) => t.key === toolKey) || TOOLS[0];
  const body = el("div", "tool");
  body.append(...tool.build(v));
  $("tool-body").replaceChildren(body);
}

/* ───────────── provisioning ───────────── */

const CONFIRM = {
  docker: "Installs Docker, the NVIDIA Container Toolkit and configures GPU passthrough. This can take a while and may need a reboot.",
  openclaw: "Installs Node.js (if missing) and the openclaw npm package. Afterwards, run `openclaw onboard` once as your user.",
  ssl: "Sets up nginx on :11443 with this page's certificate. It forwards /v1/ to the model and requires the API key, and the rest to the OpenClaw gateway.",
  vulkan: "Installs Mesa's Vulkan driver (RADV), the Vulkan loader and vulkaninfo from Ubuntu's packages.",
  llamacpp: "Downloads the latest llama.cpp Vulkan build from Unsloth's GitHub releases, checks its SHA-256 and installs it under /var/lib/spark-swapper. Running models switch to it the next time they start.",
  gpumem: "Adds ttm.pages_limit to the kernel's boot settings (GRUB) so the GPU may map up to ~108 GiB of the 128 GB. The original file is kept as /etc/default/grub.spark-swapper.bak. It takes effect after you reboot the box.",
};

let provTimer = null;
let provLastLogLen = -1;

async function pollProvisioning() {
  clearTimeout(provTimer);
  if (activeTab !== "provision") return;
  let rep, job;
  try {
    rep = await api("/api/provisioning");
    job = (await api("/api/provisioning/job")).job;
  } catch (e) {
    if (e.status === 401) return boot();
    return;
  }
  renderProv(rep, job);
  const running = job && job.state === "running";
  provTimer = setTimeout(pollProvisioning, running ? 1200 : 8000);
}

function renderProv(rep, job) {
  const box = $("prov");
  box.replaceChildren();
  const running = job && job.state === "running";
  const items = Object.entries(rep).sort((a, b) => (a[1].order ?? 99) - (b[1].order ?? 99));
  for (const [key, st] of items) {
    const present = !!st.present;
    const row = el("div", "prov-row");
    const left = el("div", "prov-left");
    left.append(el("div", "prov-label", st.label || key));
    left.append(el("div", "muted small", st.desc || ""));
    const chip = el("span", `chip ${present ? "on" : "off"}`, present ? "ready" : st.pending_reboot ? "reboot needed" : "needs install");
    left.append(chip);
    if (st.detail) left.append(el("div", "muted small", st.detail));
    row.append(left);
    const btn = el("button", "btn " + (present ? "ghost" : "primary"), present ? (key === "llamacpp" ? "Update" : "Re-run / verify") : "Install");
    btn.disabled = running || me.role !== "admin";
    if (me.role !== "admin") btn.title = "Only an admin can run provisioning.";
    btn.addEventListener("click", () => startInstall(key, st));
    row.append(btn);
    box.append(row);
  }
  renderProvJob(job);
}

async function startInstall(key, st) {
  const ok = await ask(`${st.present ? "Re-run" : "Install"}: ${st.label || key}?`,
    CONFIRM[key] || "Runs the install steps and streams the log here.", key === "gpumem" ? "Change the boot setting" : "Go");
  if (!ok) return;
  try {
    await api("/api/install", { action: key });
    toast(`Starting: ${st.label || key}`);
    provLastLogLen = -1;
    pollProvisioning();
  } catch (e) { toast(e.message); }
}

function renderProvJob(job) {
  const p = $("prov-job");
  if (!job) { p.hidden = true; return; }
  p.hidden = false;
  $("prov-job-title").textContent = jobTitle(job);
  const end = job.finished || Date.now() / 1000;
  $("prov-job-time").textContent = (job.state === "running" ? "Elapsed " : "Took ") + fmtDur(end - job.started);
  const ol = $("prov-steps");
  ol.replaceChildren();
  for (const st of job.steps) {
    const li = el("li", st.state);
    li.append(el("span", "ic"));
    const body = el("div"); body.append(el("div", "", st.label));
    if (st.detail) body.append(el("div", "d", st.detail));
    li.append(body); ol.append(li);
  }
  const note = $("prov-job-note");
  const msg = job.state === "failed" ? job.error : job.note;
  note.hidden = !msg;
  note.className = `note${job.state === "failed" ? " bad" : ""}`;
  note.textContent = msg || "";
  if (job.log_total !== provLastLogLen) {
    provLastLogLen = job.log_total;
    writeLog($("prov-log"), job.log);
  }
  if (job.state === "failed") $("prov-log-wrap").open = true;
}

/* ───────────── settings: updates ───────────── */

let updTimer = null;

async function loadUpdates() {
  clearTimeout(updTimer);
  if (activeTab !== "settings") return;
  let u;
  try { u = await api("/api/updates"); } catch (e) {
    if (e.status === 401) return boot();
    if (e.status) { toast(e.message); return; }
    $("upd-status").textContent = "Spark Swapper is restarting…";
    updTimer = setTimeout(waitForRestart, 2000);
    return;
  }
  renderUpdates(u);
  renderAbout();
  if (u.busy || (u.last_result && u.last_result.state === "running")) updTimer = setTimeout(loadUpdates, 1500);
}

function renderUpdates(u) {
  $("upd-current").textContent = `This is ${u.current}`;
  const st = $("upd-status");
  if (u.available) st.textContent = `Version ${u.latest} is available${u.published ? ` (released ${new Date(u.published).toLocaleDateString()})` : ""}.`;
  else if (u.latest) st.textContent = `Up to date. The latest release is ${u.latest}.`;
  else st.textContent = u.last_error || "Not checked yet.";
  if (u.last_check) st.textContent += ` Last checked ${fmtWhen(u.last_check)}.`;
  $("upd-check").checked = !!u.check;
  $("upd-auto").checked = !!u.auto_install;
  $("upd-auto").disabled = !u.check;
  $("upd-next").textContent = (u.signed_only ? "Only signed releases are installed. " : "") + (!u.can_install ? `Automatic installs are off here: ${u.why_not}`
    : u.check ? `Next check: ${new Date(u.next_check * 1000).toLocaleString([], { weekday: "long", hour: "2-digit", minute: "2-digit" })}${u.auto_install ? " — a newer version installs right away." : " — you'll see it here."}`
      : "Weekly checks are off. Use Check now whenever you like.");
  const inst = $("btn-upd-install");
  inst.hidden = !u.available;
  inst.disabled = u.busy || !u.can_install;
  $("btn-upd-check").disabled = u.busy;
  const res = $("upd-result");
  const r = u.last_result;
  res.hidden = !r;
  if (r) {
    const when = r.finished ? ` ${fmtWhen(r.finished)}` : "";
    res.className = `note slim${r.state === "failed" || r.state === "rolled_back" ? " bad" : ""}`;
    res.textContent = r.state === "ok" ? `Updated from ${r.from} to ${r.to}${when}.`
      : r.state === "running" ? `Updating from ${r.from} to ${r.to}…`
        : r.state === "rolled_back" ? `The update to ${r.to} didn't come up, so ${r.from} was put back${when}. ${r.message || ""}`
          : `The update to ${r.to} failed${when}: ${r.message || "see the log"}.`;
  }
  $("upd-notes-wrap").hidden = !(u.available && u.notes);
  $("upd-notes").textContent = u.notes || "";
  const lines = [...(u.log || []).map((l) => `${new Date(l.t * 1000).toLocaleTimeString([], { hour12: false })}  ${l.m}`), ...(u.last_log || [])];
  $("upd-log").textContent = lines.join("\n") || "Nothing yet.";
  $("upd-dot").hidden = !u.available;
}

function renderAbout() {
  const rows = [
    ["Hardware", session.platform_label || session.platform || "?"],
    ["Host name", session.hostname || "?"],
    ["Version", session.version || "?"],
    ["Models offered", status ? `${Object.keys(status.models).length} (for this hardware)` : "—"],
  ];
  $("about").replaceChildren(...rows.map(([k, v]) => {
    const r = el("div", "cred"); r.append(el("div", "k", k), el("div", "", v)); return r;
  }));
}

async function saveUpdateSettings() {
  try {
    const u = await api("/api/updates/settings", { check: $("upd-check").checked, auto_install: $("upd-auto").checked });
    renderUpdates(u);
    toast("Saved.");
  } catch (e) { toast(e.message); }
}
$("upd-check").addEventListener("change", saveUpdateSettings);
$("upd-auto").addEventListener("change", saveUpdateSettings);

$("btn-upd-check").addEventListener("click", async () => {
  $("btn-upd-check").disabled = true;
  try { renderUpdates(await api("/api/updates/check", {})); }
  catch (e) { toast(e.message); }
  finally { $("btn-upd-check").disabled = false; }
});

$("btn-upd-install").addEventListener("click", async () => {
  const ok = await ask("Update Spark Swapper now?",
    "It downloads the new version, checks it, and restarts this page (about a minute). Models keep serving. If the new version doesn't start, the current one is put back.", "Update");
  if (!ok) return;
  try {
    await api("/api/updates/install", {});
    toast("Updating…");
    loadUpdates();
  } catch (e) { toast(e.message); }
});

async function waitForRestart() {
  try {
    const s = await api("/api/session");
    if (s.version && s.version !== session.version) {
      toast(`Updated to ${s.version}.`);
      setTimeout(() => location.reload(), 1200);
      return;
    }
    session = s;
    loadUpdates();
  } catch (_) {
    updTimer = setTimeout(waitForRestart, 2000);
  }
}

/* ───────────── users (admin) ───────────── */

async function loadUsers() {
  let r;
  try { r = await api("/api/users"); } catch (e) { if (e.status === 401) boot(); return; }
  const body = $("users-body");
  body.replaceChildren();
  for (const u of r.users) {
    const tr = el("tr");
    tr.append(el("td", "", u.username + (u.username === me.username ? " (you)" : "")));
    tr.append(el("td", "", u.role));
    tr.append(el("td", "muted", new Date(u.created * 1000).toLocaleDateString()));
    const acts = el("td", "row end");
    const reset = el("button", "btn ghost small", "Reset password");
    reset.addEventListener("click", () => openReset(u.username));
    acts.append(reset);
    if (u.username !== me.username) {
      const rm = el("button", "btn ghost small danger", "Remove");
      rm.addEventListener("click", () => confirmRemove(u));
      acts.append(rm);
    }
    tr.append(acts);
    body.append(tr);
  }
}

$("btn-add-user").addEventListener("click", () => {
  $("au-err").textContent = "";
  $("au-name").value = $("au-pass").value = "";
  $("add-user").showModal();
});

$("au-ok").addEventListener("click", async (ev) => {
  ev.preventDefault();
  try {
    await api("/api/users/add", {
      username: $("au-name").value.trim(),
      password: $("au-pass").value,
      role: $("au-role").value,
    });
    $("add-user").close();
    toast("User created.");
    loadUsers();
  } catch (e) { $("au-err").textContent = e.message; }
});

let resetFor = null;
function openReset(username) {
  resetFor = username;
  $("reset-pw-who").textContent = `New password for ${username}. This signs out their existing sessions and app connections.`;
  $("rp-err").textContent = "";
  $("rp-pass").value = "";
  $("reset-pw").showModal();
}

$("rp-ok").addEventListener("click", async (ev) => {
  ev.preventDefault();
  try {
    await api("/api/users/reset", { username: resetFor, password: $("rp-pass").value });
    $("reset-pw").close();
    toast(`Password reset for ${resetFor}.`);
  } catch (e) { $("rp-err").textContent = e.message; }
});

async function confirmRemove(u) {
  if (!await ask(`Remove ${u.username}?`, "They lose access immediately. Existing sessions and app connections are signed out.", "Remove")) return;
  try {
    await api("/api/users/remove", { username: u.username });
    toast("User removed.");
    loadUsers();
  } catch (e) { toast(e.message); }
}

/* ───────────── credentials ───────────── */

$("btn-creds").addEventListener("click", async () => {
  const box = $("creds");
  if (credsShown) {
    box.hidden = true; box.replaceChildren(); credsShown = false;
    $("btn-creds").textContent = "Show"; $("creds-hint").hidden = false;
    return;
  }
  try {
    const c = await api("/api/credentials");
    box.replaceChildren(
      credRow("OpenClaw token", c.openclaw.token, { src: "gateway.auth.token in ~/.openclaw/openclaw.json" }),
      ...(c.openclaw.password ? [credRow("OpenClaw password", c.openclaw.password)] : []),
      credRow("OpenClaw dashboard", c.openclaw.dashboard_url, { link: true, src: "Signs you straight in with the token" }),
      credRow("Model API key", c.api.key, {
        src: c.api.found_in.length ? `Same key in: ${c.api.found_in.join(", ")}` : "Required by the HTTPS front on :11443",
      }),
      credRow("API base URL", c.api.base_url, { src: "OpenAI-compatible · self-signed TLS via nginx" }),
      credRow("Model ID", c.api.model || "(no model running)"),
      ...Object.entries(c.api.other_keys || {}).map(([k, files]) =>
        credRow("Other key seen", k, { src: `in ${files.join(", ")}` })),
    );
    box.hidden = false; credsShown = true;
    $("btn-creds").textContent = "Hide"; $("creds-hint").hidden = true;
  } catch (e) { toast(e.message); }
});

/* ───────────── account ───────────── */

$("btn-logout").addEventListener("click", async () => {
  try { await api("/api/logout", {}); } catch (_) { /* ignore */ }
  me = null;
  stopPoll();
  boot();
});

$("btn-password").addEventListener("click", () => {
  $("pw-err").textContent = "";
  $("pw-cur").value = $("pw-new").value = "";
  $("pw").showModal();
});

$("pw-ok").addEventListener("click", async (ev) => {
  ev.preventDefault();
  try {
    await api("/api/password", { current: $("pw-cur").value, new: $("pw-new").value });
    $("pw").close();
    toast("Password changed. Your other sessions and app connections were signed out.");
  } catch (e) { $("pw-err").textContent = e.message; }
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !$("dash").hidden) {
    if (activeTab === "models") poll();
    if (activeTab === "storage") loadStorage();
    if (activeTab === "provision") pollProvisioning();
    if (activeTab === "settings") loadUpdates();
  }
});

boot();
