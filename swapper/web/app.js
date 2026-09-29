"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

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
  toast._h = setTimeout(() => t.classList.remove("show"), 2400);
}

const fmtCtx = (n) => !n ? "?" : n >= 1e6 ? `${+(n / 1e6).toFixed(2)}M` : `${Math.round(n / 1024)}K`;
const fmtDur = (s) => {
  s = Math.max(0, Math.round(s));
  const m = Math.floor(s / 60), r = s % 60;
  return m ? `${m}m ${String(r).padStart(2, "0")}s` : `${r}s`;
};

let me = null;        // {username, role}
let isDemo = false;
let activeTab = "models";

/* ───────────── gate: one-time setup, then sign-in ───────────── */

let setupMode = false;

async function boot() {
  let s;
  try { s = await api("/api/session"); } catch (e) { return showGateError(e.message); }
  isDemo = !!s.demo;
  if (s.user) { me = { username: s.user, role: s.role }; return showDash(); }
  setupMode = s.setup_needed;
  $("gate").hidden = false;
  $("dash").hidden = true;
  $("gate-title").textContent = setupMode ? "Welcome to your Spark" : "Spark Swapper";
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

function showDash() {
  $("gate").hidden = true;
  $("dash").hidden = false;
  $("who").textContent = `${me.username}${me.role === "admin" ? " · admin" : ""}`;
  $("tab-users").hidden = me.role !== "admin";
  switchTab("models");
  poll();
  if (isDemo) toast("Demo mode — sample data, nothing is actually installed.");
}

function switchTab(name) {
  activeTab = name;
  for (const t of document.querySelectorAll(".tab")) t.classList.toggle("on", t.dataset.tab === name);
  $("pane-models").hidden = name !== "models";
  $("pane-provision").hidden = name !== "provision";
  $("pane-users").hidden = name !== "users";
  if (name === "provision") pollProvisioning();
  if (name === "users" && me.role === "admin") loadUsers();
  if (name !== "models") stopPoll();
  if (name === "models") poll();
}

document.querySelectorAll(".tab").forEach((t) =>
  t.addEventListener("click", () => switchTab(t.dataset.tab)));

/* ───────────── models (status + swap) ───────────── */

let status = null;
let pollTimer = null;
let credsShown = false;
let lastLogLen = -1;

function stopPoll() { clearTimeout(pollTimer); pollTimer = null; }

async function poll() {
  if (activeTab !== "models") { stopPoll(); return; }
  clearTimeout(pollTimer);
  try {
    status = await api("/api/status");
    render();
  } catch (e) {
    if (e.status === 401) return boot();
    $("now-title").textContent = "Can't reach the Spark";
    $("now-sub").textContent = e.message;
    $("now-dot").className = "pulse bad";
  }
  const busy = status && (status.busy || status.loading);
  pollTimer = setTimeout(poll, busy ? 1500 : 5000);
}

function render() {
  const s = status;
  if (!s) return;
  const models = Object.values(s.models).sort((a, b) => a.order - b.order);
  const job = s.job;
  const running = job && job.state === "running";

  const dot = $("now-dot");
  if (running) {
    const t = s.models[job.target];
    dot.className = "pulse busy";
    $("now-eyebrow").textContent = "Switching";
    $("now-title").textContent = `Moving to ${t ? t.title : job.target}`;
    $("now-sub").textContent = "Sit back — this runs on its own. You can close this page.";
  } else if (s.active) {
    const m = s.models[s.active];
    dot.className = "pulse ok";
    $("now-eyebrow").textContent = "Now serving";
    $("now-title").textContent = m.title;
    $("now-sub").textContent = `${fmtCtx(m.served_context || m.context)} context · ${m.served_id}`;
  } else if (s.loading) {
    dot.className = "pulse busy";
    $("now-eyebrow").textContent = "Starting up";
    $("now-title").textContent = s.models[s.loading].title;
    $("now-sub").textContent = "Loading weights into memory…";
  } else {
    dot.className = "pulse";
    $("now-eyebrow").textContent = "Idle";
    $("now-title").textContent = "No model is running";
    $("now-sub").textContent = "Pick one below to start it.";
  }

  const cards = $("cards");
  cards.replaceChildren();
  for (const m of models) {
    const c = el("article", "card");
    const isActive = s.active === m.key && !running;
    const isLoading = (running && job.target === m.key) || (!running && s.loading === m.key);
    if (isActive) c.classList.add("active");
    if (isLoading) c.classList.add("loading");
    c.append(el("h3", "", m.title), el("div", "engine", m.engine), el("p", "muted", m.tagline));
    const chips = el("div", "chips");
    chips.append(el("span", "chip", `${fmtCtx(m.context)} context`));
    if (m.vision) chips.append(el("span", "chip", "Vision"));
    if (isActive) chips.append(el("span", "chip on", "Serving now"));
    if (isLoading) chips.append(el("span", "chip wait", "Loading"));
    c.append(chips, el("div", "spacer"));
    const b = el("button", isActive ? "btn ghost" : "btn primary",
      isActive ? "Running" : isLoading ? "Loading…" : "Switch to this");
    b.disabled = isActive || running;
    b.addEventListener("click", () => confirmSwap(m));
    c.append(b);
    cards.append(c);
  }

  renderJob(job);

  const mem = s.memory;
  const used = mem.total ? (mem.total - mem.available) / mem.total : 0;
  $("mem-bar").style.width = `${Math.round(used * 100)}%`;
  $("mem-text").textContent = `${(mem.total - mem.available).toFixed(0)} / ${mem.total.toFixed(0)} GiB`;
  $("h-nginx").className = `dot ${s.nginx ? "ok" : "bad"}`;
  $("h-gw").className = `dot ${s.gateway ? "ok" : "bad"}`;
  $("h-oc").textContent = s.openclaw_primary ? `OpenClaw → ${s.openclaw_primary}` : "";
}

function renderJob(job) {
  const p = $("progress");
  if (!job) { p.hidden = true; return; }
  p.hidden = false;
  const s = status;
  const t = s.models[job.target];
  const title = job.state === "running" ? `Switching to ${t ? t.title : job.target}`
    : job.state === "done" ? `Switched to ${t ? t.title : job.target}` : `Couldn't switch to ${t ? t.title : job.target}`;
  $("progress-title").textContent = title;
  const end = job.finished || Date.now() / 1000;
  $("progress-time").textContent = (job.state === "running" ? "Elapsed " : "Took ") + fmtDur(end - job.started)
    + (job.state === "running" && job.target === "flash" ? " · usually ~11 min" : "")
    + (job.state === "running" && job.target === "standard" ? " · usually 3–6 min" : "");

  const ol = $("steps");
  ol.replaceChildren();
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
    ol.append(li);
  }

  const note = $("progress-note");
  const msg = job.state === "failed" ? [job.error, job.note].filter(Boolean).join(" ") : job.note;
  note.hidden = !msg || job.state === "running";
  note.className = `note${job.state === "failed" ? " bad" : ""}`;
  note.textContent = msg || "";

  if (job.log_total !== lastLogLen) {
    lastLogLen = job.log_total;
    const pre = $("log");
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
    pre.textContent = job.log.map((l) => {
      const d = new Date(l.t * 1000);
      return `${d.toLocaleTimeString([], { hour12: false })}  ${l.m}`;
    }).join("\n");
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  }
  if (job.state === "failed") $("log-wrap").open = true;
}

function confirmSwap(m) {
  const cur = status.active ? status.models[status.active].title : null;
  $("confirm-title").textContent = `Switch to ${m.title}?`;
  const eta = m.key === "flash" ? "about 11 minutes" : "a few minutes";
  $("confirm-body").textContent =
    (cur ? `${cur} will stop, then ` : "") +
    `${m.title} loads (${eta}). OpenClaw is repointed automatically, and the API at :11443 keeps the same address and key. ` +
    `If the new model fails to start, the previous one is brought back.`;
  const d = $("confirm");
  d.returnValue = "";
  d.showModal();
  d.addEventListener("close", async function once() {
    d.removeEventListener("close", once);
    if (d.returnValue !== "ok") return;
    try {
      await api("/api/swap", { target: m.key });
      lastLogLen = -1;
      toast(`Switching to ${m.title}…`);
      poll();
    } catch (e) { toast(e.message); }
  });
}

/* ───────────── provisioning ───────────── */

const PROV = [
  { key: "git", label: "git", desc: "Needed to clone the model recipes and OpenClaw.", action: "git" },
  { key: "docker", label: "Docker + NVIDIA GPU passthrough", desc: "Hosts the model containers.", action: "docker" },
  { key: "sglang_recipe", label: "Qwen3.8 27B (SGLang) recipe", desc: "MiaAI-Lab/Qwen3.8-27B-SGLang-DGX-Spark", action: "sglang_recipe" },
  { key: "flash_recipe", label: "Qwen3.8 Flash Next (vLLM) recipe", desc: "MiaAI-Lab/Qwen3.8-Flash-Next-Single-DGX-Spark", action: "flash_recipe" },
  { key: "openclaw", label: "OpenClaw", desc: "Agent gateway; the swapper repoints it at the active model.", action: "openclaw" },
  { key: "ssl", label: "Self-signed SSL / nginx TLS front", desc: "Cert for this page + the :11443 model front.", action: "ssl" },
];

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
  for (const item of PROV) {
    const st = rep[item.key] || {};
    const present = item.key === "ssl" ? st.present : !!st.present;
    const row = el("div", "prov-row");
    const left = el("div", "prov-left");
    left.append(el("div", "prov-label", item.label));
    left.append(el("div", "muted small", item.desc));
    const chip = el("span", `chip ${present ? "on" : "off"}`, present ? (item.key === "ssl" && st.present ? "present" : "present") : "needs install");
    if (!present && item.key === "git" && st.detail) chip.textContent = st.detail;
    left.append(chip);
    row.append(left);
    const btn = el("button", "btn " + (present ? "ghost" : "primary"), present ? "Re-run / verify" : "Install");
    btn.disabled = running;
    btn.addEventListener("click", () => startInstall(item));
    row.append(btn);
    box.append(row);
  }
  renderProvJob(job);
}

function startInstall(item) {
  $("confirm-title").textContent = `Install ${item.label}?`;
  $("confirm-body").textContent = item.action === "docker"
    ? "Installs Docker, the NVIDIA Container Toolkit and configures GPU passthrough. This can take a while and may need a reboot."
    : item.action === "openclaw"
      ? "Installs Node.js (if missing) and the openclaw npm package. It does not run `openclaw init` — you still configure it."
      : item.action === "ssl"
        ? "Generates/keeps the self-signed cert and checks the nginx :11443 TLS front."
        : "Runs the install steps and streams the log here.";
  const d = $("confirm");
  d.returnValue = "";
  d.showModal();
  d.addEventListener("close", async function once() {
    d.removeEventListener("close", once);
    if (d.returnValue !== "ok") return;
    try {
      const r = await api("/api/install", { action: item.action });
      toast(`Starting: ${item.label}`);
      provLastLogLen = -1;
      pollProvisioning();
    } catch (e) { toast(e.message); }
  });
}

function renderProvJob(job) {
  const p = $("prov-job");
  if (!job) { p.hidden = true; return; }
  p.hidden = false;
  const name = (job.target || "").replace("install:", "");
  const title = job.state === "running" ? `Installing ${name}`
    : job.state === "done" ? `Finished: ${name}` : `Failed: ${name}`;
  $("prov-job-title").textContent = title;
  const end = job.finished || Date.now() / 1000;
  $("prov-job-time").textContent = (job.state === "running" ? "Elapsed " : "Took ") + fmtDur(end - job.started);
  const note = $("prov-job-note");
  note.hidden = job.state !== "failed";
  note.textContent = job.error || "";
  if (job.log_total !== provLastLogLen) {
    provLastLogLen = job.log_total;
    const pre = $("prov-log");
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
    pre.textContent = job.log.map((l) => {
      const d = new Date(l.t * 1000);
      return `${d.toLocaleTimeString([], { hour12: false })}  ${l.m}`;
    }).join("\n");
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  }
  if (job.state === "failed") $("prov-log-wrap").open = true;
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
  $("reset-pw-who").textContent = `New password for ${username}. This signs out their existing sessions.`;
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

function confirmRemove(u) {
  $("confirm-title").textContent = `Remove ${u.username}?`;
  $("confirm-body").textContent = "They lose access immediately. Existing sessions are signed out.";
  const d = $("confirm");
  d.returnValue = "";
  d.showModal();
  d.addEventListener("close", async function once() {
    d.removeEventListener("close", once);
    if (d.returnValue !== "ok") return;
    try {
      await api("/api/users/remove", { username: u.username });
      toast("User removed.");
      loadUsers();
    } catch (e) { toast(e.message); }
  });
}

/* ───────────── credentials ───────────── */

function credRow(label, value, { secret = false, link = false, src = "" } = {}) {
  const row = el("div", "cred");
  row.append(el("div", "k", label));
  const code = el("code", "", value || "—");
  row.append(code);
  const actions = el("div", "row");
  if (value) {
    const copy = el("button", "btn ghost small", "Copy");
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(value); toast("Copied"); }
      catch (_) {
        const r = document.createRange(); r.selectNodeContents(code);
        const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
        toast("Selected — press ⌘C / Ctrl+C");
      }
    });
    actions.append(copy);
    if (link) {
      const a = el("a", "btn ghost small", "Open");
      a.href = value; a.target = "_blank"; a.rel = "noopener noreferrer";
      actions.append(a);
    }
  }
  row.append(actions);
  if (src) row.append(el("div", "src", src));
  if (secret) row.dataset.secret = "1";
  return row;
}

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
      credRow("OpenClaw token", c.openclaw.token, { secret: true, src: "gateway.auth.token in ~/.openclaw/openclaw.json" }),
      ...(c.openclaw.password ? [credRow("OpenClaw password", c.openclaw.password, { secret: true })] : []),
      credRow("OpenClaw dashboard", c.openclaw.dashboard_url, { link: true, src: "Signs you straight in with the token" }),
      credRow("Model API key", c.api.key, {
        secret: true,
        src: c.api.found_in.length ? `Same key in: ${c.api.found_in.join(", ")}` : "Passed to SGLang / vLLM as --api-key",
      }),
      credRow("API base URL", c.api.base_url, { src: "OpenAI-compatible · self-signed TLS via nginx" }),
      credRow("Model ID", c.api.model || "(no model running)"),
      ...Object.entries(c.api.other_keys || {}).map(([k, files]) =>
        credRow("Other key seen", k, { secret: true, src: `in ${files.join(", ")}` })),
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
    toast("Password changed. Your other sessions were signed out.");
  } catch (e) { $("pw-err").textContent = e.message; }
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !$("dash").hidden) {
    if (activeTab === "models") poll();
    if (activeTab === "provision") pollProvisioning();
  }
});

boot();
