/*
 * The installer's six screens. Everything that touches the disk is asked of
 * Python through window.pywebview.api (wilcoatc/installer/app.py); this file
 * only draws. BOOT is put in by app.py: the language and its catalogue, the
 * version, the folder to suggest, the licence text.
 */
"use strict";

const $ = (id) => document.getElementById(id);
let S = window.BOOT.strings || {};

function t(key, params) {
  let text = S[key] !== undefined ? S[key] : key;
  for (const [name, value] of Object.entries(params || {})) {
    text = text.split(`{${name}}`).join(String(value));
  }
  return text;
}

function size(bytes) {
  const lang = window.BOOT.lang || "en";
  if (bytes >= 1e9) {
    return `${new Intl.NumberFormat(lang, { maximumFractionDigits: 1 }).format(bytes / 1e9)} ${t("unit.gb")}`;
  }
  return `${new Intl.NumberFormat(lang, { maximumFractionDigits: 0 }).format(bytes / 1e6)} ${t("unit.mb")}`;
}

const STEPS = ["rail.welcome", "rail.licence", "rail.location", "rail.sim", "rail.install", "rail.finish"];
const OPTIONS = [
  ["desktop", "opt.desktop", "opt.desktop_sub"],
  ["start_menu", "opt.start", "opt.start_sub"],
  ["updates", "opt.updates", "opt.updates_sub"],
];

const state = {
  step: 0,
  agree: false,
  check: null,              // the folder as Python resolved it
  checking: 0,
  opts: { desktop: true, start_menu: true, updates: true, toolbar_panel: true },
  communities: [],          // where the toolbar panel would go
  sims: null,
  job: null,
  launch: true,
  installedTo: "",
  language: "",             // picked here; empty means Windows' own
};

// Written right to left: the whole window flips.
const RTL = new Set(["ar", "he", "fa"]);

let api = null;

// --------------------------------------------------------------- drawing ---

/** Every word on the page, again: on start and whenever the language changes. */
function paintTexts() {
  const lang = window.BOOT.lang || "en";
  document.documentElement.lang = lang;
  document.documentElement.dir = RTL.has(lang) ? "rtl" : "ltr";
  for (const node of document.querySelectorAll("[data-t]")) node.textContent = t(node.dataset.t);
  for (const node of document.querySelectorAll("[data-aria]")) node.setAttribute("aria-label", t(node.dataset.aria));
  $("version").textContent = window.BOOT.licence_name
    ? `v${window.BOOT.version} · ${window.BOOT.licence_name}` : `v${window.BOOT.version}`;
  $("licence").textContent = window.BOOT.licence || "";
  $("welcome-text").textContent = window.BOOT.upgrade
    ? t("welcome.upgrade", { version: window.BOOT.version, path: window.BOOT.path })
    : t("welcome.text", { version: window.BOOT.version });
  $("repo").textContent = (window.BOOT.repo || "").replace(/^https:\/\//, "");
  $("language").value = lang;
}

function setLanguage(code) {
  const table = (window.BOOT.catalogues || {})[code];
  if (!table) return;
  S = table;
  window.BOOT.lang = code;
  state.language = code;
  paintTexts();
  paint();
  if (api && api.set_title) api.set_title(t("window.title"));
}

function paintStatic() {
  $("path").value = window.BOOT.path || "";
  const picker = $("language");
  picker.textContent = "";
  for (const [code, name] of window.BOOT.languages || [["en", "English"]]) {
    const option = document.createElement("option");
    option.value = code;
    option.textContent = name;
    picker.append(option);
  }
  picker.addEventListener("change", () => setLanguage(picker.value));

  const options = $("options");
  options.textContent = "";
  for (const [key, label, sub] of OPTIONS) {
    const button = document.createElement("button");
    button.className = "option";
    button.setAttribute("role", "switch");
    button.dataset.key = key;
    button.innerHTML = '<span class="track"><span class="knob"></span></span>' +
      '<span class="option-text"><span class="option-label"></span><span class="option-sub"></span></span>';
    // Keyed, so paintTexts says them again in another language.
    button.querySelector(".option-label").dataset.t = label;
    button.querySelector(".option-sub").dataset.t = sub;
    button.addEventListener("click", () => {
      state.opts[key] = !state.opts[key];
      paint();
    });
    options.append(button);
  }
  paintTexts();
}

/** A long folder as its last few names: "…\Roaming\WilcoATC". */
function shortPath(path, max = 46) {
  if (path.length <= max) return path;
  const parts = path.split("\\");
  let tail = parts.pop();
  while (parts.length && (parts[parts.length - 1].length + tail.length + 3) <= max) {
    tail = `${parts.pop()}\\${tail}`;
  }
  return `…\\${tail}`;
}

function paintRail() {
  const rail = $("rail");
  rail.textContent = "";
  STEPS.forEach((key, i) => {
    const item = document.createElement("div");
    item.className = "rail-item" + (i < state.step ? " is-done" : i === state.step ? " is-current" : "");
    const dot = document.createElement("span");
    dot.className = "rail-dot";
    dot.textContent = i < state.step ? "✓" : String(i + 1);
    const label = document.createElement("span");
    label.className = "rail-label";
    label.textContent = t(key);
    item.append(dot, label);
    rail.append(item);
  });
}

function paintLocation() {
  const c = state.check;
  const note = $("path-note");
  $("path").classList.toggle("is-bad", Boolean(c && c.problems.length));
  if (!c) {
    note.textContent = "";
    return;
  }
  $("disk-name").textContent = c.drive ? t("location.disk", { drive: c.drive }) : "";
  $("disk-need").textContent = t("location.need", { need: size(c.needed), free: size(c.free) });
  const total = c.total || 1;
  $("disk-used").style.width = `${Math.max(0, Math.min(100, (total - c.free) / total * 100))}%`;
  $("disk-new").style.width = `${Math.max(0, Math.min(100, (c.needed + c.later) / total * 100))}%`;
  // One or two lines at most: the problem, or what will happen to the folder,
  // or -- when there is nothing to say about it -- what the first launch adds.
  let lines;
  if (c.problems.length) lines = c.problems.map((k) => t(k));
  else if (c.notes.length) lines = c.notes.map((k) => t(k));
  else lines = [t("location.later", { later: size(c.later) })];
  if (c.path && c.notes.includes("path.subfolder") && !c.problems.length) {
    lines.push(t("location.into", { path: shortPath(c.path) }));
  }
  note.textContent = lines.join(" ");
  note.title = c.path || "";
  note.classList.toggle("is-bad", c.problems.length > 0);
}

function paintSims() {
  const box = $("sims");
  box.textContent = "";
  const rows = [["steam", "sim.steam"], ["store", "sim.store"]];
  for (const [key, label] of rows) {
    const found = state.sims && state.sims.find((s) => s.key === key);
    const searching = !state.sims;
    const row = document.createElement("div");
    row.className = "sim" + (found && found.found ? " is-found" : "");
    const icon = document.createElement("span");
    icon.className = "sim-icon";
    icon.textContent = searching ? "…" : found && found.found ? "✓" : "–";
    const text = document.createElement("span");
    text.className = "sim-text";
    const name = document.createElement("span");
    name.className = "sim-name";
    name.textContent = t(label);
    const where = document.createElement("span");
    where.className = "sim-path mono";
    where.textContent = searching ? t("sim.searching")
      : found && found.found ? (found.path || t("sim.found")) : t("sim.not_installed");
    where.title = where.textContent;
    text.append(name, where);
    const stated = document.createElement("span");
    stated.className = "sim-state";
    stated.textContent = searching ? t("sim.state_searching") : found && found.found ? t("sim.found") : t("sim.not_found");
    row.append(icon, text, stated);
    box.append(row);
  }
  const any = state.sims && state.sims.some((s) => s.found);
  $("sim-note").textContent = !state.sims ? t("sim.note_checking") : any ? t("sim.note_found") : t("sim.note_none");

  // The toolbar panel, where there is a Community folder to put it in.
  const where = state.communities;
  $("panel-box").hidden = !state.sims || !where.length;
  $("panel-option").setAttribute("aria-checked", String(state.opts.toolbar_panel));
  if (where.length) {
    const sub = $("panel-sub");
    sub.textContent = t("sim.panel_sub", { path: where.map((p) => shortPath(p, 40)).join(", ") });
    sub.title = where.join(" · ");
  }
}

function phaseOf(job) {
  if (!job) return t("install.phase_prepare");
  if (job.state === "done") return t("install.phase_done");
  if (job.state === "failed") return t(job.reason || "err.generic");
  if (job.total && job.done >= job.total) return t("install.phase_shortcuts");
  const name = job.name || "";
  if (!name) return t("install.phase_prepare");
  if (name.startsWith("_internal")) return t("install.phase_engine");
  if (name.startsWith("data/")) return t("install.phase_data");
  return t("install.phase_files");
}

function paintInstall() {
  const job = state.job;
  const pct = job && job.total ? Math.min(100, job.done / job.total * 100) : 0;
  const failed = job && job.state === "failed";
  $("install-title").textContent = job && job.state === "done" ? t("install.done_title")
    : failed ? t("install.failed_title") : t("install.title");
  $("pct").textContent = `${Math.floor(pct)}%`;
  $("progress-fill").style.width = `${failed ? 100 : pct}%`;
  $("progress-fill").classList.toggle("is-failed", failed);
  $("phase").textContent = phaseOf(job);
  $("phase").classList.toggle("is-bad", failed);
  const log = $("log");
  log.textContent = "";
  // Each file by its name in the program's folder: the folder itself is long
  // enough to push every name off the end of the line, and it is said once,
  // at the end.
  for (const name of (job && job.files) || []) {
    const line = document.createElement("span");
    line.textContent = `+ ${name.split("/").join("\\")}`;
    log.append(line);
  }
  if (job && job.state === "done") {
    const line = document.createElement("span");
    line.className = "is-done";
    line.textContent = t("install.installed_to", { path: shortPath(state.installedTo, 60) });
    line.title = state.installedTo;
    log.append(line);
  }
  if (failed && job.detail) {
    const line = document.createElement("span");
    line.className = "is-bad";
    line.textContent = job.detail;
    log.append(line);
  }
}

/** What happened to the toolbar panel, on the last screen. */
function paintFinish() {
  const job = state.job || {};
  const done = job.panel || [];
  const failed = job.panel_failed || [];
  const line = $("finish-panel");
  let said = "";
  if (failed.length) said = t("err.panel", { path: failed.map((p) => shortPath(p, 40)).join(", ") });
  else if (done.length) said = t("finish.panel_done");
  line.hidden = !said;
  line.textContent = said;
}

function canGoOn() {
  switch (state.step) {
    case 1: return state.agree;
    case 2: return Boolean(state.check && !state.check.problems.length && !state.checking);
    case 3: return Boolean(state.sims);
    case 4: return Boolean(state.job && state.job.state === "failed");
    default: return true;
  }
}

function paint() {
  paintRail();
  for (const section of document.querySelectorAll(".step")) {
    section.classList.toggle("is-shown", Number(section.dataset.step) === state.step);
  }
  $("agree").setAttribute("aria-checked", String(state.agree));
  $("agree-box").textContent = state.agree ? "✓" : "";
  $("launch").setAttribute("aria-checked", String(state.launch));
  $("launch-box").textContent = state.launch ? "✓" : "";
  for (const option of document.querySelectorAll(".option")) {
    option.setAttribute("aria-checked", String(state.opts[option.dataset.key]));
  }
  if (state.step === 2) paintLocation();
  if (state.step === 3) paintSims();
  if (state.step === 4) paintInstall();
  if (state.step === 5) paintFinish();

  const st = state.step;
  const running = st === 4 && !(state.job && state.job.state === "failed");
  $("back").classList.toggle("is-hidden", !(st > 0 && st < 4));
  const next = $("next");
  next.textContent = st === 2 ? t("btn.install") : st === 3 ? t("btn.install_now")
    : st === 4 ? (running ? t("btn.installing") : t("btn.retry"))
    : st === 5 ? (state.launch ? t("btn.launch") : t("btn.finish")) : t("btn.next");
  next.disabled = !canGoOn();
  $("close").disabled = running;
  $("minimize").disabled = false;
}

// ------------------------------------------------------------- the steps ---

let checkTimer = 0;
function checkPath() {
  clearTimeout(checkTimer);
  state.checking += 1;
  paint();
  checkTimer = setTimeout(async () => {
    const asked = $("path").value;
    const answer = await api.check_path(asked);
    state.checking = Math.max(0, state.checking - 1);
    if ($("path").value !== asked) return;       // typed on since
    state.check = answer;
    paint();
  }, 250);
}

async function findSims() {
  state.sims = null;
  paint();
  // The two rows are seen searching for a moment, as the design has them:
  // an answer that arrives in a millisecond reads as not having looked.
  const [found] = await Promise.all([api.simulators(), new Promise((r) => setTimeout(r, 900))]);
  if (state.step !== 3) return;
  state.sims = (found && found.sims) || [];
  state.communities = (found && found.communities) || [];
  paint();
}

async function startInstall() {
  state.job = { state: "running", done: 0, total: 0, files: [] };
  paint();
  const result = await api.install($("path").value,
                                   Object.assign({ language: state.language }, state.opts));
  if (!result || !result.ok) {
    if (result && result.problems) {          // the folder changed under us
      state.job = null;
      go(2);
      checkPath();
      return;
    }
    state.job = await api.progress();
    paint();
    return;
  }
  state.installedTo = result.path;
  poll();
}

async function poll() {
  const job = await api.progress();
  state.job = job;
  paint();
  if (job.state === "running") {
    setTimeout(poll, 120);
  } else if (job.state === "done") {
    setTimeout(() => { if (state.step === 4) go(5); }, 700);
  }
}

function go(step) {
  state.step = step;
  paint();
  if (step === 2) checkPath();
  if (step === 3) findSims();
  if (step === 4) startInstall();
  const focus = step === 2 ? $("path") : $("next");
  if (focus && !focus.disabled) focus.focus({ preventScroll: true });
}

async function next() {
  if (!canGoOn()) return;
  const st = state.step;
  if (st === 4) { go(4); return; }                  // try again
  if (st === 5) {
    if (state.launch) await api.launch();
    api.quit();
    return;
  }
  go(st + 1);
}

function askQuit() {
  if (state.step === 4 && !(state.job && state.job.state === "failed")) return;
  if (state.step >= 4) { api.quit(); return; }       // installed, or failed
  $("quit").hidden = false;
  $("quit-stay").focus();
}

// ---------------------------------------------------------------- wiring ---

function wire() {
  $("next").addEventListener("click", next);
  $("back").addEventListener("click", () => { if (state.step > 0 && state.step < 4) go(state.step - 1); });
  $("agree").addEventListener("click", () => { state.agree = !state.agree; paint(); });
  $("launch").addEventListener("click", () => { state.launch = !state.launch; paint(); });
  $("panel-option").addEventListener("click", () => {
    state.opts.toolbar_panel = !state.opts.toolbar_panel;
    paint();
  });
  $("path").addEventListener("input", checkPath);
  $("path").addEventListener("keydown", (e) => { if (e.key === "Enter") next(); });
  $("browse").addEventListener("click", async () => {
    const chosen = await api.browse($("path").value);
    if (!chosen) return;
    // A picked folder is shown as the folder it will really be, WilcoATC
    // inside it where it has other things in it, as installers do.
    const answer = await api.check_path(chosen);
    $("path").value = (answer && answer.path) || chosen;
    checkPath();
  });
  $("minimize").addEventListener("click", () => api.minimize());
  $("close").addEventListener("click", askQuit);
  $("quit-stay").addEventListener("click", () => { $("quit").hidden = true; });
  $("quit-go").addEventListener("click", () => api.quit());
  $("repo").addEventListener("click", (e) => { e.preventDefault(); api.open_url(window.BOOT.repo); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (!$("quit").hidden) $("quit").hidden = true;
      else askQuit();
    }
  });
}

function start() {
  api = window.pywebview.api;
  paintStatic();
  wire();
  paint();
  if (!window.BOOT.payload) {
    // A damaged download: nothing to install, and it says so first.
    state.step = 4;
    state.job = { state: "failed", reason: "err.payload", files: [] };
    paint();
  }
}

if (window.pywebview && window.pywebview.api) start();
else window.addEventListener("pywebviewready", start, { once: true });
