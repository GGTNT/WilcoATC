/*
 * The panel's behaviour.
 *
 * Four screens over one live connection. The engine pushes its state a few
 * times a second and every transmission as it happens, so nothing here polls
 * except the two screens that ask for a list the engine has no reason to send
 * unprompted: the frequency browser and the airspace map.
 *
 * Everything that keys the microphone or tunes the radio goes back to the
 * engine, because the audio devices and the aeroplane live there, not in this
 * window.
 */

const $ = (id) => document.getElementById(id);
const on = (node, type, fn) => node && node.addEventListener(type, fn);

const app = {
  screen: "comms",
  state: null,
  settings: null,
  devices: null,
  socket: null,
  // The map's own state. Where it is looking lives in the map module, which
  // owns it because panning and zooming have to survive a redraw that
  // arrives while a finger is still down. This is only what the page around
  // it needs: which layers are on, and whether a fetch is already in flight.
  layers: new Set(["stations", "rings", "airports", "traffic"]),
  mapBusy: false,
  mapAt: 0,
  freqScope: "near",
  freqQuery: "",
  atis: null,
  setup: null,
  doctor: null,
  keyed: false,
  timers: {},
  // Which touchdown the landing card was last shown for, and in which
  // language it was drawn, so each landing comes up once and a language
  // change redraws it rather than bringing it back.
  landingAt: 0,
  landingLang: "",
  // Which settings section is open: the last one this browser looked at.
  section: (() => {
    try { return localStorage.getItem("wilco.section") || ""; } catch (error) { return ""; }
  })(),
};

// -------------------------------------------------------------- helpers ---

// A sector frequency made up for want of a published one (atc/enroute.py)
// says so beside the station's name, so a pilot who knows the real one can
// report it rather than take the made-up one for real.
function invented(radio) {
  return radio.station && radio.invented
    ? ` · ${t("comms.invented_freq", "frequency invented")}` : "";
}

function text(node, value) {
  if (node && node.textContent !== value) node.textContent = value;
}

function mhz(value) {
  return value ? Number(value).toFixed(3) : "---.---";
}

function feet(value) {
  return value ? `${Math.round(value).toLocaleString()} ft` : "—";
}

/**
 * What an aeroplane was last told, in the language this window is being read
 * in.
 *
 * The engine sends the kind of instruction and, where there is one, the
 * number in it -- "heading", "270" -- rather than a sentence, because the
 * sentence would be in English and everything else on the page would not be.
 */
function order(value) {
  if (!value || !value.kind) return "";
  // The engine sends the English with the token, which is what a window being
  // read in English uses. Everything else comes out of the table.
  const word = t(`order.${value.kind}`, value.label || value.kind);
  return value.detail ? `${word} ${value.detail}` : word;
}

function zulu(at) {
  const d = at ? new Date(at * 1000) : new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}Z`;
}

function clock(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
}

async function post(path, body) {
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    return await response.json();
  } catch (error) {
    return { ok: false, error: String(error) };
  }
}

async function get(path) {
  try {
    const response = await fetch(path);
    return await response.json();
  } catch (error) {
    return null;
  }
}

/** A short-lived note under a settings card, so a save is visibly a save. */
function note(id, message, warning) {
  const node = $(id);
  if (!node) return;
  node.textContent = message;
  node.classList.toggle("is-warning", Boolean(warning));
  clearTimeout(app.timers[id]);
  app.timers[id] = setTimeout(() => {
    node.textContent = "";
    node.classList.remove("is-warning");
  }, warning ? 9000 : 2600);
}

/** One line, over everything, for the answer to something the pilot just did. */
function toast(message) {
  const node = $("toast");
  if (!node || !message) return;
  node.textContent = message;
  node.classList.add("is-shown");
  clearTimeout(app.timers.toast);
  app.timers.toast = setTimeout(() => node.classList.remove("is-shown"), 4200);
}

/** Why the runway button did nothing, by the reason the engine gives. */
const SPAWN_REFUSED = {
  traffic_off: ["comms.spawn_traffic_off", "Other traffic is switched off in Settings."],
  no_field: ["comms.spawn_no_field", "Tune a station at an airport first."],
  no_runway: ["comms.spawn_no_runway", "This airport has no runway to put an aircraft on."],
  not_in_sim: ["comms.spawn_not_in_sim", "The simulator is not creating AI aircraft, so none can be put on the runway."],
  no_callsign: ["comms.spawn_failed", "Could not put an aircraft on the runway."],
};

/** Why a test button (traffic.debug.triggers) did nothing. */
const DEBUG_REFUSED = {
  traffic_off: ["comms.spawn_traffic_off", "Other traffic is switched off in Settings."],
  no_field: ["comms.spawn_no_field", "Tune a station at an airport first."],
  no_runway: ["comms.spawn_no_runway", "This airport has no runway to put an aircraft on."],
  not_in_sim: ["comms.spawn_not_in_sim", "The simulator is not creating AI aircraft, so none can be put on the runway."],
  nobody_parked: ["comms.debug_nobody_parked", "No parked aircraft to board."],
  no_free_stand: ["comms.debug_no_free_stand", "No free stand for an arrival."],
};

async function debugTrigger(path) {
  const result = await post(path);
  if (result.ok) {
    toast(result.callsign);
    return;
  }
  const [key, english] = DEBUG_REFUSED[result.error]
    || ["comms.debug_failed", "That cannot be done right now."];
  toast(t(key, english));
}

// ------------------------------------------------------------- routing ---

const SCREENS = {
  comms: { key: "comms.title", title: "Communications" },
  airspace: { key: "airspace.title", title: "Airspace" },
  frequencies: { key: "frequencies.title", title: "Frequencies" },
  settings: { key: "settings.title", title: "Settings" },
};

// Short for the catalogue lookup. Everything the script writes into the page
// goes through it; everything the markup carries is translated in place by
// i18n.js, which is why most of the page needs nothing here.
const t = (key, fallback) => I18N.t(key, fallback);

function show(name) {
  if (!SCREENS[name]) return;
  app.screen = name;
  for (const button of document.querySelectorAll(".nav-item")) {
    const active = button.dataset.screen === name;
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  for (const section of document.querySelectorAll(".screen")) {
    section.hidden = section.id !== `screen-${name}`;
  }
  text($("page-title"), t(SCREENS[name].key, SCREENS[name].title));
  paintHeader();
  paintClock();

  if (name === "frequencies") loadFrequencies();
  if (name === "airspace") loadAirspace();
  if (name === "settings") { loadSettings(); loadSetup(); }
  pollSticks(name === "settings");
}

// ------------------------------------------------- push-to-talk controllers ---

// A controller is saved by who it is -- vendor and product, then its name --
// and not by its place in the list, which moves whenever another device is
// plugged in ahead of it. The index goes along for an older program.
function stickChoice(stick) {
  return { "ptt.joystick_id": stick.id, "ptt.joystick_name": stick.name,
           "ptt.joystick_index": stick.index };
}

// The learnt controller and no other -- the same rule as the server's
// Controllers.find: by its id, or by the index of a file written before ids.
// Never by name: a Bravo shares a maker with the Alpha it was not learnt on.
function configuredStick(ptt) {
  const sticks = ((app.devices || {}).joysticks) || [];
  if (ptt.joystick_id) return sticks.find((j) => j.id === ptt.joystick_id);
  return sticks.find((j) => j.index === ptt.joystick_index);
}

function paintSticks() {
  const devices = app.devices || {};
  const ptt = (app.settings || {}).ptt || {};
  const sticks = $("set-joystick");
  if (!sticks) return;
  sticks.replaceChildren();
  const list = devices.joysticks || [];
  const add = (value, label) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    sticks.appendChild(option);
  };
  for (const stick of list) {
    add(stick.id, `${stick.name} · ${stick.id} · ${t("set.n_buttons", "{n} buttons")
      .replace("{n}", stick.buttons)}`);
  }
  const chosen = configuredStick(ptt);
  if (chosen) {
    sticks.value = chosen.id;
  } else if (ptt.joystick_id || ptt.joystick_name) {
    // Set, and not plugged in: said by name, and kept, so plugging it back
    // in needs nothing chosen again.
    add("", t("set.controller_not_connected", "{name} (not connected)")
      .replace("{name}", [ptt.joystick_name, ptt.joystick_id]
        .filter(Boolean).join(" · ")));
    sticks.value = "";
  } else if (!list.length) {
    // Three different things go wrong here and they need three different
    // answers; the library being absent is a broken installation, not an
    // empty USB port, and is never answered with a command to type.
    const why = devices.joystick_reason || "none";
    add("", why === "missing"
      ? t("set.joystick_missing", "Joystick support is missing from this installation. Please report it.")
      : why === "none"
        ? t("set.no_controller_found", "no controller connected")
        : `${t("set.no_controller_found", "no controller connected")} (${why})`);
    sticks.value = "";
  }

  // A yoke that is switched off looks exactly like a yoke that is not
  // supported. Naming it says which, so nobody goes hunting for a driver
  // when the answer is a USB lead.
  const known = devices.joystick_known || [];
  const offline = $("joystick-known");
  offline.hidden = !known.length;
  offline.textContent = known.length
    ? t("set.controller_offline",
        "Known to this computer but not connected: {names}")
        .replace("{names}", known.join(", "))
    : "";
}

async function refreshSticks() {
  const listed = await get("/api/controllers");
  if (!listed || !app.devices) return;
  const before = JSON.stringify([app.devices.joysticks, app.devices.joystick_reason,
                                 app.devices.joystick_known]);
  app.devices.joysticks = listed.joysticks;
  app.devices.joystick_reason = listed.reason;
  app.devices.joystick_known = listed.known;
  const after = JSON.stringify([listed.joysticks, listed.reason, listed.known]);
  // Repainted only when something changed, so an open list is not closed
  // under the pilot's pointer every two seconds.
  if (before !== after) {
    paintSticks();
    if (app.onboard) paintObPtt();
  }
}

// A yoke plugged in while Settings is open appears without a reload.
function pollSticks(on) {
  clearInterval(app.stickPoll);
  app.stickPoll = on ? setInterval(() => {
    if (((app.settings || {}).ptt || {}).mode === "joystick") refreshSticks();
  }, 2000) : null;
}

// --------------------------------------------------------- live updates ---

function connect() {
  const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
  const socket = new WebSocket(url);
  app.socket = socket;

  socket.onopen = () => link(true);
  socket.onclose = () => {
    link(false);
    // The engine may just be restarting. Keep trying, quietly.
    setTimeout(connect, 1500);
  };
  socket.onmessage = (message) => {
    const payload = JSON.parse(message.data);
    if (payload.kind === "snapshot" || payload.kind === "state") {
      apply(payload.state);
    } else if (payload.kind === "event") {
      addTurn(payload);
      // A failure the pilot can actually fix from here. A red line in the
      // transcript scrolls away; the dialog that fixes it does not.
      if (payload.action === "setup") loadSetup(true);
      // Said rather than typed: whatever was in the box is not part of it,
      // and left there it went out on the end of the next typed call.
      if (payload.action === "spoken") $("say").value = "";
    } else if (payload.kind === "setup") {
      // The installer runs on its own thread and reports as it goes, so the
      // bars move without the page asking for anything.
      app.setup = payload.setup;
      paintSetup();
      paintComponents();
      if (app.onboard && app.onboard.step === OB_DOWNLOADS) paintObDownloads();
    } else if (payload.kind === "update") {
      app.update = payload.update;
      paintUpdate();
      maybeOfferUpdate();
    }
  };
}

/**
 * Whether the engine is answering. Shown in the mic pill, which is the one
 * thing on every screen that depends on it: a "Mic ready" over a dead
 * connection would be a lie.
 */
function link(up) {
  app.linkUp = up;
  $("mic-pill").classList.toggle("is-down", !up);
  if (!up) {
    $("mic-pill").hidden = false;
    text($("mic-text"), t("link.reconnecting", "reconnecting…"));
  } else if (app.state) paintHeader();
}

/** The UTC clock in the page head, every screen but Settings. */
function paintClock() {
  const clock = $("zulu-clock");
  if (!clock) return;
  clock.hidden = app.screen === "settings";
  text(clock, zulu());
}

function apply(state) {
  const previous = app.state;
  app.state = state;
  paintHeader();
  paintComms();
  paintSettingsLive();
  paintLanding(state.landing);

  // A frequency change is the one thing that invalidates both lists.
  const before = previous && previous.radio ? previous.radio.com1 : null;
  if (before !== null && state.radio.com1 !== before) {
    if (app.screen === "frequencies") loadFrequencies();
    if (app.screen === "airspace") loadAirspace();
    app.atis = null;
    if (app.screen === "frequencies") loadAtis();
  }

  // The aeroplane has moved, and the map follows it. Redrawn from what is
  // already here: a position update is not a reason to ask the server for
  // the same rectangle of the world again.
  if (app.screen === "airspace" && Airspace.view.follow && app.airspace
      && app.airspace.ownship && state.position) {
    app.airspace.ownship = {
      ...app.airspace.ownship,
      lat: state.position.latitude,
      lon: state.position.longitude,
      heading: state.position.heading,
      altitude_ft: state.position.altitude_ft,
      ground_speed_kt: state.position.ground_speed_kt,
      on_ground: state.position.on_ground,
    };
    paintAirspace();
  }
}

// ---------------------------------------------------------------- head ---

function paintHeader() {
  const state = app.state;
  if (!state) return;
  paintClock();

  const micPill = $("mic-pill");
  const talking = state.transmitting;
  const playing = state.playing;
  micPill.classList.toggle("is-live", Boolean(talking || playing));
  if (app.linkUp === false) return;
  micPill.hidden = !(talking || playing);
  text($("mic-text"), talking ? t("mic.transmitting", "Transmitting")
    : playing ? t("mic.receiving", "Receiving") : t("mic.ready", "Mic ready"));

  const aircraft = state.aircraft || {};
  text($("ptt-key"), state.ptt_key || "`");
}

// --------------------------------------------------------------- comms ---

function paintComms() {
  const state = app.state;
  if (!state) return;
  const radio = state.radio || {};
  const flight = state.flight || {};

  text($("active-mhz"), mhz(radio.com1));
  text($("active-pos"), radio.position || "");
  const who = $("active-who");
  who.classList.toggle("is-off", !radio.station);
  text(who.lastElementChild,
       (radio.station || t("comms.nobody_here", "nobody on this frequency"))
       + invented(radio));

  // Four bars, one dropped for every quarter of the station's range used up.
  const reach = radio.station ? Math.max(1, radio.distance_nm) : 0;
  const bars = radio.station ? Math.max(1, 4 - Math.floor(reach / 15)) : 0;
  [...$("signal").children].forEach((bar, index) => {
    bar.classList.toggle("on", index < bars);
  });

  text($("standby-mhz"), mhz(radio.com1_standby));
  text($("tile-heard"), String((state.stats && state.stats.total) || 0));

  text($("strip-callsign"), (state.aircraft || {}).callsign || "—");
  text($("strip-type"), (state.aircraft || {}).type || "—");
  text($("strip-squawk"), flight.squawk || (state.position || {}).squawk || "—");
  text($("strip-runway"), flight.runway || "—");
  text($("strip-altitude"), feet((state.position || {}).altitude_ft));
  text($("strip-assigned"), flight.assigned_altitude_ft
    ? feet(flight.assigned_altitude_ft) : "—");
  text($("strip-route"), `${flight.departure || "—"} → ${flight.destination || "—"}`);
  text($("phase-badge"), (flight.phase || "—").replace(/_/g, " "));

  // The banner is for the one thing the controller is waiting on.
  const banner = $("banner");
  if (flight.pending) {
    banner.hidden = false;
    const label = flight.pending.replace(/_/g, " ");
    text($("banner-text"), `${label} — read it back`);
  } else if (flight.cleared_takeoff) {
    banner.hidden = false;
    text($("banner-text"), `Cleared for takeoff · runway ${flight.cleared_runway || flight.runway}`);
  } else if (flight.cleared_landing) {
    banner.hidden = false;
    text($("banner-text"), `Cleared to land · runway ${flight.cleared_runway || flight.runway}`);
  } else {
    banner.hidden = true;
  }

  paintPhrases();
  paintTraffic();
  paintLive();
}

/**
 * The bay of strips: the other aeroplanes the controller is working.
 *
 * One strip each, side by side, the way a bay of paper strips sits in front of
 * a controller. Whoever is on the tuned frequency comes first and is marked,
 * because those are the ones that can be heard. A filled dot means the
 * aeroplane is in the simulator as well, so it can be looked for out of the
 * window.
 */
/**
 * The live traffic in one line: whether the feed answers, from where, how
 * old its last answer is, and how much of the traffic is real. Under the
 * settings card's switch, and over the chart's traffic list.
 */
const LIVE_STATUS = {
  live: ["live.live", "Live"],
  empty: ["live.empty", "No aircraft near"],
  down: ["live.down", "No source answering, invented traffic in use"],
  starting: ["live.starting", "Starting"],
};

function liveLine(live) {
  if (!live || live.status === "off") return "";
  if (live.status === "fsltl") return t("live.fsltl", "FSLTL flies the traffic");
  const status = LIVE_STATUS[live.status]
    ? t(...LIVE_STATUS[live.status]) : live.status;
  if (live.status === "down" || live.status === "starting") return status;
  return fill(t("live.state",
                "{status}: {source}, {age} s old · {live} real, {invented} invented"),
              { status, source: live.source || "—",
                age: live.age_s == null ? "—" : Math.round(live.age_s),
                live: live.live ?? 0, invented: live.invented ?? 0 });
}

function paintLive() {
  const live = app.state && app.state.live;
  text($("live-readout"), liveLine(live));
}

function paintTraffic() {
  const bay = $("traffic-rows");
  if (!bay) return;
  const traffic = (app.state && app.state.traffic) || [];

  const here = traffic.filter((a) => a.on_frequency).length;
  text($("traffic-count"), traffic.length
    ? `${here} ${t("comms.on_frequency", "on this frequency")}`
    : "");

  bay.replaceChildren();
  if (!traffic.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = t("comms.no_traffic", "no other traffic");
    bay.appendChild(empty);
    return;
  }

  for (const aircraft of traffic) {
    const strip = document.createElement("div");
    strip.className = "strip-card";
    if (aircraft.on_frequency) strip.classList.add("is-here");
    if (aircraft.in_simulator) strip.classList.add("in-sim");

    const doing = t(`traffic.${aircraft.phase}`, aircraft.label || aircraft.phase);
    // What matters about this one: how far out if it is coming in, which
    // runway if it is on the ground, how high if it is neither.
    const where = aircraft.arriving && !aircraft.on_ground
      ? `${aircraft.distance_nm} nm`
      : aircraft.on_ground
        ? (aircraft.runway || aircraft.type)
        : feet(aircraft.altitude_ft);
    // And what the tower last told it, which is the difference between an
    // aeroplane that is on final and one that has just been sent around.
    const told = order(aircraft.order);
    strip.innerHTML = `
      <div class="who">${aircraft.callsign}</div>
      <div class="doing">${doing}${where ? ` · ${where}` : ""}</div>
      ${told ? `<div class="told">${told}</div>` : ""}`;
    bay.appendChild(strip);
  }
}

// The phrases a pilot actually needs next, chosen from the phase of flight.
// They are shortcuts for typing, not new phraseology: each one is a sentence
// the intent parser already understands.
const PHRASES = {
  preflight: ["request clearance", "request pushback", "radio check"],
  cleared: ["ready to taxi", "request pushback"],
  pushback: ["ready to taxi"],
  taxi_out: ["holding short, ready for departure", "say again"],
  holding_short: ["holding short, ready for departure"],
  lined_up: ["ready for departure"],
  takeoff: ["with you, climbing", "request climb"],
  departure: ["with you, climbing", "request climb", "request direct"],
  enroute: ["request climb", "request descent", "request direct"],
  descent: ["request descent", "request approach", "field in sight"],
  approach: ["request approach", "field in sight", "going around"],
  landing: ["going around", "request taxi to parking"],
  landed: ["request taxi to parking"],
  taxi_in: ["request taxi to parking"],
};

// A VFR flight has none of the en-route IFR calls ("with you, climbing",
// "request direct" were offered in a circuit at Kortrijk). In the air in a
// circuit it gets the circuit's calls; otherwise the VFR ones for its phase.
// Each is a sentence the intent parser understands
// (tests/test_vfr_quick_replies.py checks it).
const VFR_CIRCUIT_PHRASES = [
  "downwind", "final", "touch and go", "full stop", "runway vacated",
  "leaving the zone",
];
const VFR_PHRASES = {
  preflight: ["request taxi", "radio check"],
  cleared: ["request taxi"],
  taxi_out: ["ready for departure", "say again"],
  holding_short: ["ready for departure"],
  lined_up: ["lined up"],
  takeoff: ["leaving the zone", "downwind"],
  departure: ["leaving the zone", "request frequency change"],
  enroute: ["inbound for landing", "request frequency change"],
  descent: ["inbound for landing", "downwind"],
  approach: ["downwind", "final", "full stop"],
  landing: ["full stop", "runway vacated"],
  landed: ["runway vacated", "request taxi to parking"],
  taxi_in: ["request taxi to parking"],
};
const AIRBORNE = new Set(["takeoff", "departure", "enroute", "descent",
                          "approach", "landing"]);

function phrasesFor(flight) {
  const phase = flight.phase || "preflight";
  if ((flight.rules || "IFR") !== "VFR") {
    return [phase, PHRASES[phase] || ["say again", "radio check"]];
  }
  if (flight.pattern_work && AIRBORNE.has(phase)) {
    return ["vfr-circuit", VFR_CIRCUIT_PHRASES];
  }
  return [`vfr-${phase}`, VFR_PHRASES[phase] || ["say again", "radio check"]];
}

function paintPhrases() {
  const [phase, wanted] = phrasesFor(app.state.flight || {});
  const box = $("phrases");
  if (box.dataset.phase === phase) return;
  box.dataset.phase = phase;
  box.replaceChildren();
  wanted.forEach((phrase, index) => {
    const button = document.createElement("button");
    button.className = index === 0 ? "phrase primary" : "phrase";
    button.textContent = phrase;
    button.onclick = () => transmit(phrase);
    box.appendChild(button);
  });
  const again = document.createElement("button");
  again.className = "phrase";
  again.textContent = "say again";
  again.onclick = () => transmit(t("comms.say_again", "say again"));
  box.appendChild(again);
}

// "{airport}" and friends, replaced by what the engine sent. A "language"
// parameter is an ISO code rather than text, so it is named in the language
// of the window: a German reader is told "Französisch", not "fr".
function fill(template, params) {
  let out = template;
  for (const [name, value] of Object.entries(params || {})) {
    const shown = name === "language" ? t(`lang.${value}`, value) : value;
    out = out.split(`{${name}}`).join(shown);
  }
  return out;
}

const TURN_KIND = {
  pilot: "from-pilot", atc: "from-atc", atis: "from-atc",
  // Somebody else's exchange. It is on your frequency and you heard it, so it
  // is in the transcript, but it is not addressed to you and reads that way.
  traffic: "from-atc is-traffic",
  // The people in this aeroplane. Same side as the controller, because it
  // is somebody talking to you, but marked out: none of it is on the radio,
  // and a first officer's callout must never be mistaken for a clearance.
  crew: "from-atc is-crew",
  info: "from-system", error: "from-system is-error",
};

const NAMED_KINDS = new Set(["pilot", "atc", "atis", "traffic", "crew"]);

function addTurn(payload) {
  const list = $("transcript");
  const item = document.createElement("li");
  item.className = `turn ${TURN_KIND[payload.type] || "from-system"}`;

  if (NAMED_KINDS.has(payload.type)) {
    const who = document.createElement("div");
    who.className = "who";
    who.textContent = `${payload.speaker || (payload.type === "pilot"
      ? t("comms.you", "You") : "ATC")} · ${zulu(payload.at)}`;
    item.appendChild(who);
  }

  const said = document.createElement("div");
  said.className = "said";
  // An engine notice carries a key and its parameters; a transmission does
  // not, because a transmission is already in the language it was said in.
  said.textContent = payload.key
    ? fill(t(payload.key, payload.text), payload.params)
    : payload.text;
  item.appendChild(said);

  list.appendChild(item);
  while (list.children.length > 200) list.removeChild(list.firstChild);
  list.scrollTop = list.scrollHeight;
  text($("transcript-note"),
       `${list.children.length} ${t("comms.messages", "messages")}`);
}

// -------------------------------------------------------- transmitting ---

async function transmit(what) {
  const input = $("say");
  const said = (what !== undefined ? what : input.value).trim();
  if (!said) return;
  if (what === undefined) input.value = "";
  // No language is named. Which language a transmission is in is read off the
  // words by the engine -- against the languages the tuned facility actually
  // works -- exactly as the recogniser reads it off the sound, so typing and
  // speaking behave the same way.
  await post("/api/say", { text: said, language: null });
}

function keyDown() {
  if (app.keyed) return;
  app.keyed = true;
  $("ptt").classList.add("is-keyed");
  post("/api/ptt/start");
}

function keyUp() {
  if (!app.keyed) return;
  app.keyed = false;
  $("ptt").classList.remove("is-keyed");
  post("/api/ptt/stop");
}

// ------------------------------------------------------------ airspace ---

/**
 * Fetch what is inside the map's window, and draw it.
 *
 * The map asks by rectangle rather than by radius, because it can be panned
 * away from the aeroplane -- so what has to be fetched is what is on screen,
 * not what is near you. The request is skipped while one is already in
 * flight: at five frames a second on a continental view, overlapping fetches
 * are how a map gets slower the further out you go.
 */
async function loadAirspace(force) {
  if (app.screen !== "airspace" && !force) return;
  if (app.mapBusy) return;
  app.mapBusy = true;
  try {
    // Well beyond the window, so a drag reveals airports that are already
    // here rather than empty land waiting for the fetch that follows it.
    const box = Airspace.bounds(0.4);
    const query = `north=${box.north.toFixed(4)}&south=${box.south.toFixed(4)}`
      + `&west=${box.west.toFixed(4)}&east=${box.east.toFixed(4)}`
      + `&span_nm=${box.spanNm.toFixed(1)}`;
    const data = await get(`/api/map?${query}`);
    if (data) {
      app.airspace = data;
      app.mapAt = Date.now();
    }
  } finally {
    app.mapBusy = false;
  }
  paintAirspace();
}

/**
 * Redraw from what is already here.
 *
 * Panning and zooming go through this rather than through a fetch, so the
 * map moves under the hand at the frame rate of the browser and the new data
 * arrives when it arrives. A map that waits for the network before it moves
 * feels broken however fast the network is.
 */
function repaintAirspace() {
  // Once a frame, however many events asked. A mouse reports its position
  // a few hundred times a second and every one of them redrew the whole
  // chart -- coastlines, labels, the list beside it -- which is what made
  // dragging the map feel like dragging it through treacle.
  if (!app.mapFrame) {
    app.mapFrame = requestAnimationFrame(() => {
      app.mapFrame = 0;
      paintMap();
    });
  }
  scheduleMapFetch();
}

/** Ask the server for the new window once the hand has stopped. */
function scheduleMapFetch() {
  clearTimeout(app.timers.mapFetch);
  app.timers.mapFetch = setTimeout(() => loadAirspace(), 180);
}

/**
 * The chart, or the model, and nothing else.
 *
 * What a moving map needs each frame. The list beside it, the card and the
 * counters follow when the fetch that repaintAirspace schedules comes back,
 * which is a fifth of a second after the hand stops.
 */
function paintMap() {
  const data = app.airspace;
  // The map draws the instruction beside the aeroplane, so it is put into
  // words here where the language is known rather than in the drawing.
  for (const aircraft of (data && data.traffic) || []) {
    aircraft.instruction = order(aircraft.order);
    aircraft.intent = t(`traffic.${aircraft.phase}`,
                        aircraft.label || aircraft.phase);
  }
  const model = wantsModel();
  Airspace.render($("map"), data, app.layers, { hidden: model });
  if (window.Airspace3D && app.modelReady) {
    window.Airspace3D.show(model);
    if (model) {
      loadScene();
      window.Airspace3D.rebuildPaths();
    }
  }
  // The scale and whether it is following: cheap, and wrong the moment the
  // map moves if they wait for the rest.
  paintMapChrome(data);
}

function paintAirspace() {
  const data = app.airspace;
  paintMap();
  paintAirspaceTraffic(data);
  paintCraftCard();

  const rows = $("airspace-rows");
  rows.replaceChildren();
  // Who can hear you, which the engine answers from the aeroplane's own
  // position. Deliberately not filtered out of what is on the map: panning
  // away to look at where you are going must not empty the list of the
  // people you are talking to.
  const stations = (data && data.nearby) || [];
  text($("airspace-count"), `${stations.length} ${t("map.in_range", "in range")}`);
  text($("tile-stations"), String(stations.length));

  if (!stations.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = (data && data.ownship)
      ? t("map.no_controller", "no controller within range of you")
      : t("map.no_position", "no aircraft position yet");
    rows.appendChild(empty);
    return;
  }

  for (const station of stations.slice(0, 40)) {
    rows.appendChild(stationRow(station, {
      bottom: `${station.position} · ${Number(station.mhz).toFixed(3)}`,
      trailTop: `${station.distance_nm} nm`,
      trailBottom: `${station.range_nm} nm reach`,
    }));
  }
}

// ------------------------------------------------------------ the model ---

/**
 * The airport in three dimensions, below a few miles across.
 *
 * The model is drawn by airspace3d.js and looks where the chart looks, so all
 * this does is decide when it is on, keep it fed, and hand it the gestures.
 * It is a module and may arrive after this script, which is why it announces
 * itself rather than being assumed.
 */
function setupModel() {
  const model = window.Airspace3D;
  if (!model || app.modelReady !== undefined) return;
  app.modelReady = model.init({
    canvas: $("map3d"),
    labels: $("map3d-labels"),
    view: () => Airspace.view,
    selected: () => Airspace.selection(),
    followed: () => app.followCallsign || "",
    pick: (callsign) => {
      Airspace.select(callsign);
      paintAirspace();
    },
  });
  if (!app.modelReady) {
    const chip = $("map-3d");
    if (chip) chip.hidden = true;
  }
  paintAirspace();
}
window.addEventListener("airspace3d", setupModel);

function wantsModel() {
  if (!app.modelReady || !app.show3d || app.screen !== "airspace") return false;
  const view = Airspace.view;
  return Boolean(view.ready) && view.spanNm <= 8;
}

/** The airport under the middle of the view, fetched when it changes. */
async function loadScene() {
  const view = Airspace.view;
  const model = window.Airspace3D;
  if (!model || app.sceneBusy) return;
  const have = model.airport;
  const away = have && have.ident
    ? distanceFrom({ lat: view.lat, lon: view.lon }, have) : Infinity;
  // Asked again while the taxiways have not come yet: the simulator is
  // read in the background, and the first answer is often runways only.
  const stale = have && have.ident && !have.ground
    && Date.now() - (app.sceneAt || 0) > 8000;
  if (away < 4 && !stale) return;
  app.sceneBusy = true;
  try {
    const data = await get(`/api/scene?lat=${view.lat.toFixed(5)}&lon=${view.lon.toFixed(5)}`);
    app.sceneAt = Date.now();
    if (data && data.ident) model.setAirport(data);
  } finally {
    app.sceneBusy = false;
  }
}

/** The aeroplanes, every second while the model is on. */
async function loadLive() {
  const model = window.Airspace3D;
  if (!model || !model.active || app.liveBusy) return;
  app.liveBusy = true;
  try {
    const data = await get("/api/live");
    if (!data) return;
    app.live = data;
    if (app.airspace) {
      app.airspace.traffic = data.traffic;
      app.airspace.ownship = data.ownship;
    }
    const view = Airspace.view;
    const followed = app.followCallsign
      && data.traffic.find((a) => a.callsign === app.followCallsign);
    if (followed) {
      Airspace.setCentre(followed.lat, followed.lon);
    } else if (view.follow && data.ownship) {
      Airspace.setCentre(data.ownship.lat, data.ownship.lon);
    }
    model.setTraffic(data.traffic, data.ownship);
    paintCraftCard();
  } finally {
    app.liveBusy = false;
  }
}

// ------------------------------------------------------------- the card ---

/** A translated line with its blanks filled. */
function fill(line, values) {
  return String(line).replace(/\{(\w+)\}/g, (_, key) => values[key] ?? "");
}

/** A named point on a route, in words. */
function waypointText(point) {
  switch (point.kind) {
    case "hold": return fill(t("craft.wp_hold", "Holding point {runway}"), { runway: point.value });
    case "runway": return fill(t("craft.wp_runway", "Runway {runway}"), { runway: point.value });
    case "threshold": return fill(t("craft.wp_threshold", "Landing runway {runway}"), { runway: point.value });
    case "stand": return fill(t("craft.wp_stand", "Stand {stand}"), { stand: point.value });
    case "destination": return fill(t("craft.wp_destination", "On course for {place}"), { place: point.value });
    case "leg": return t(`traffic.${point.value}`, point.value);
    default: return point.value || "";
  }
}

/**
 * Everything about the aeroplane you clicked.
 *
 * Over the map, top right, where the reference puts its panel: who it is and
 * what it is, where it is and how fast, what it is doing and was last told,
 * and the rest of its way as a timeline. Black and white; the only filled
 * shapes are the ones that say "this one" and "the end".
 */
function paintCraftCard() {
  const card = $("craft-card");
  if (!card) return;
  const chosen = Airspace.selection();
  const traffic = (app.airspace && app.airspace.traffic) || [];
  const aircraft = traffic.find((a) => a.callsign === chosen);
  if (!chosen || !aircraft) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  const you = app.airspace && app.airspace.ownship;
  const away = you ? distanceFrom(you, aircraft) : null;
  const up = Math.round(aircraft.vertical_rate_fpm || 0);
  const doing = t(`traffic.${aircraft.phase}`, aircraft.label || aircraft.phase);
  const told = order(aircraft.order);
  const from = aircraft.origin_icao || aircraft.origin || "";
  const to = aircraft.destination_icao || aircraft.destination || "";
  const make = [aircraft.make, aircraft.type].filter(Boolean).join(" · ");
  const points = aircraft.waypoints || [];

  const esc = (value) => String(value ?? "").replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const cell = (key, value) => `<div><div class="k">${esc(key)}</div><div class="v">${esc(value)}</div></div>`;

  card.innerHTML = `
    <div class="craft-head">
      <div class="names">
        <div class="craft-callsign">${esc(aircraft.callsign)}</div>
        <div class="craft-spoken">${esc(aircraft.telephony || aircraft.spoken || "")}</div>
      </div>
      <button class="craft-close" id="craft-close" title="${esc(t("craft.close", "Close"))}">×</button>
    </div>
    <button class="craft-follow${app.followCallsign === chosen ? " is-on" : ""}" id="craft-follow">${esc(t("craft.follow", "Follow this aircraft"))}</button>
    <div class="craft-tags">
      ${make ? `<span class="craft-tag">${esc(make)}</span>` : ""}
      <span class="craft-tag">${esc(aircraft.airline || t("craft.private", "Private"))}</span>
      <span class="craft-tag${aircraft.in_simulator ? " ink" : ""}">${esc(aircraft.in_simulator
        ? t("craft.in_sim", "In the simulator") : t("craft.radio_only", "On the radio only"))}</span>
    </div>
    <div class="craft-grid">
      ${cell(t("craft.altitude", "Altitude"), aircraft.on_ground ? "GND" : feet(aircraft.altitude_ft))}
      ${cell(t("craft.speed", "Speed"), `${Math.round(aircraft.ground_speed_kt || 0)} kt`)}
      ${cell(t("craft.heading", "Heading"), `${String(Math.round(aircraft.heading || 0) % 360).padStart(3, "0")}°`)}
      ${cell(t("craft.vertical", "Climb"), aircraft.on_ground ? "—" : `${up > 0 ? "+" : ""}${up} fpm`)}
      ${cell(t("craft.runway", "Runway"), aircraft.runway || "—")}
      ${cell(t("craft.squawk", "Squawk"), aircraft.squawk || "—")}
    </div>
    <div class="craft-doing">${esc(doing)}${told ? ` <span class="told">· ${esc(told)}</span>` : ""}</div>
    ${from || to ? `
    <div class="craft-trip">
      <span>${esc(from || "—")}</span><span class="line"></span><span>${esc(to || "—")}</span>
    </div>
    <div class="craft-trip"><span class="soft">${esc(aircraft.origin || "")}</span><span class="line" style="background:none"></span><span class="soft">${esc(aircraft.destination || "")}</span></div>` : ""}
    ${points.length ? `
    <div class="k" style="font-size:11px;font-weight:700;letter-spacing:.8px;color:var(--muted)">${esc(t("craft.route", "Route"))}</div>
    <div class="craft-route">
      ${points.map((point, index) => `
        <div class="craft-stop${index === 0 ? " next" : ""}${index === points.length - 1 ? " end" : ""}">
          <div class="dot"></div><div class="what">${esc(waypointText(point))}</div>
        </div>`).join("")}
    </div>` : ""}
    ${away !== null ? `<div class="craft-foot">${esc(fill(t("craft.from_you", "{nm} nm from you"), { nm: away.toFixed(1) }))}</div>` : ""}`;
  const close = $("craft-close");
  if (close) {
    close.onclick = () => {
      if (app.followCallsign === chosen) app.followCallsign = "";
      Airspace.select(chosen);
      paintAirspace();
    };
  }
  // The camera rides on this one until you drag the map or ask to be
  // followed yourself again.
  const follow = $("craft-follow");
  if (follow) {
    follow.onclick = () => {
      app.followCallsign = app.followCallsign === chosen ? "" : chosen;
      if (app.followCallsign) Airspace.follow(false);
      paintAirspace();
    };
  }
}

/** How far an aeroplane is from you, in nautical miles. */
function distanceFrom(you, aircraft) {
  if (!you) return 0;
  const R = 3440.065;
  const rad = Math.PI / 180;
  const dLat = (aircraft.lat - you.lat) * rad;
  const dLon = (aircraft.lon - you.lon) * rad;
  const a = Math.sin(dLat / 2) ** 2
    + Math.cos(you.lat * rad) * Math.cos(aircraft.lat * rad)
      * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
}

/** The readouts around the map: how wide the view is, and where it is. */
function paintMapChrome(data) {
  const view = Airspace.view;
  const across = view.spanNm >= 100
    ? Math.round(view.spanNm / 10) * 10
    : view.spanNm < 1.95
      ? Math.round(view.spanNm * 10) / 10
      : Math.round(view.spanNm);
  text($("map-scale"), `${across} ${t("map.across", "nm across · north up")}`);

  const follow = $("map-follow");
  if (follow) {
    follow.setAttribute("aria-pressed", view.follow ? "true" : "false");
  }
  const where = $("map-where");
  if (where) {
    where.textContent = view.follow
      ? t("map.following", "following the aircraft")
      : `${coordinate(view.lat, "NS")}  ${coordinate(view.lon, "EW")}`;
  }
}

/** A latitude or a longitude, written the way a chart writes one. */
function coordinate(value, hemispheres) {
  const sign = value < 0 ? hemispheres[1] : hemispheres[0];
  const size = Math.abs(value);
  const degrees = Math.floor(size);
  const minutes = Math.round((size - degrees) * 60);
  return `${sign}${degrees}°${String(minutes).padStart(2, "0")}'`;
}

/**
 * The aeroplanes on the map, as a list you can pick from.
 *
 * The map answers "where is everybody"; this answers "who are they and what
 * did the tower just tell them". Clicking one selects it, on both.
 */
function paintAirspaceTraffic(data) {
  text($("airspace-live"), liveLine(data && data.live));
  const rows = $("airspace-traffic");
  if (!rows) return;
  // Ordered by how close they are to the aeroplane, which is the order a
  // pilot cares about them in and is not the order they arrive in.
  const traffic = ((data && data.traffic) || [])
    .map((a) => ({ ...a, away: distanceFrom(data.ownship, a) }))
    .sort((a, b) => a.away - b.away)
    .slice(0, 24);
  const chosen = Airspace.selection();

  text($("airspace-traffic-count"), traffic.length
    ? `${traffic.length} ${t("map.in_range", "in range")}` : "");

  rows.replaceChildren();
  if (!traffic.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = t("comms.no_traffic", "no other traffic");
    rows.appendChild(empty);
    return;
  }

  for (const aircraft of traffic) {
    const row = document.createElement("button");
    row.className = `row${aircraft.callsign === chosen ? " is-tuned" : ""}`;
    const told = aircraft.instruction;
    row.innerHTML = `
      <div class="lead">
        <div class="top">${aircraft.callsign}</div>
        <div class="bottom">${told || aircraft.intent}</div>
      </div>
      <div class="trail">
        <div class="top">${aircraft.away.toFixed(1)} nm</div>
        <div class="bottom">${aircraft.on_ground
          ? (aircraft.runway || aircraft.type)
          : feet(aircraft.altitude_ft)}</div>
      </div>`;
    row.onclick = () => { Airspace.select(aircraft.callsign); paintAirspace(); };
    rows.appendChild(row);
  }
}

function stationRow(station, parts) {
  const row = document.createElement("button");
  row.className = `row${station.tuned ? " is-tuned" : ""}`;
  row.innerHTML = `
    <div class="lead">
      <div class="top">${station.callsign}</div>
      <div class="bottom">${parts.bottom}</div>
    </div>
    <div class="trail">
      <div class="top">${parts.trailTop}</div>
      <div class="bottom">${parts.trailBottom}</div>
    </div>`;
  row.onclick = () => tune(station.mhz);
  return row;
}

async function tune(value) {
  const result = await post("/api/tune", { mhz: value });
  if (!result.ok) {
    // With the simulator connected, COM1 belongs to the aeroplane. Saying so
    // is more use than a button that silently does nothing.
    toast(result.error || "could not tune that frequency");
    return;
  }
  toast(result.station ? `${mhz(value)} · ${result.station}`
                       : `${mhz(value)} · nobody is listening here`);
  if (app.screen === "frequencies") { app.atis = null; loadAtis(); }
}

// -------------------------------------------------------- frequencies ---

// The carrier a channel sits on, in kHz: navdata/channels.py carrier_khz.
// .x05 .x30 .x55 .x80 are the 8.33 names of the 25 kHz carrier 5 kHz below.
function carrierKhz(mhz) {
  const khz = Math.round(Number(mhz) * 1000);
  return [5, 30, 55, 80].includes(khz % 100) ? khz - 5 : khz;
}

function sameChannel(a, b) {
  return Boolean(a && b) && carrierKhz(a) === carrierKhz(b);
}

async function loadFrequencies() {
  const ident = app.freqScope === "search" ? app.freqQuery.trim().toUpperCase() : "";
  const data = await get(`/api/frequencies${ident ? `?ident=${encodeURIComponent(ident)}` : ""}`);
  app.frequencies = data;
  paintFrequencies();
  loadAtis();
}

function paintFrequencies() {
  const rows = $("freq-rows");
  rows.replaceChildren();
  const data = app.frequencies;
  const tuned = app.state ? (app.state.radio || {}).com1 : 0;

  let stations = (data && data.stations) || [];
  const query = app.freqQuery.trim().toLowerCase();
  if (query && app.freqScope === "near") {
    stations = stations.filter((s) =>
      s.callsign.toLowerCase().includes(query)
      || s.ident.toLowerCase().includes(query)
      || s.position.toLowerCase().includes(query)
      || String(s.mhz).includes(query));
  }

  if (!stations.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = app.freqScope === "search"
      ? "type an airport code above"
      : "nothing published within range of you";
    rows.appendChild(empty);
    return;
  }

  let field = null;
  for (const station of stations) {
    if (station.ident !== field) {
      field = station.ident;
      const head = document.createElement("div");
      head.className = "group-head";
      head.innerHTML = `<div class="name">${station.ident}</div>
        <div class="meta">${station.distance_nm} nm</div>`;
      rows.appendChild(head);
    }

    // By channel, not by digits: 126.630 shown is 126.625 tuned.
    const isTuned = sameChannel(station.carrier || station.mhz, tuned);
    const row = document.createElement("div");
    row.className = `row${isTuned ? " is-tuned" : ""}`;
    row.innerHTML = `
      <div class="pos-tag">${station.position}</div>
      <div class="lead">
        <div class="top">${station.mhz.toFixed(3)}</div>
        <div class="bottom">${station.callsign}</div>
      </div>`;
    const button = document.createElement("button");
    button.className = "tune";
    button.textContent = isTuned ? "Tuned" : "Tune";
    button.onclick = () => tune(station.mhz);
    row.appendChild(button);
    rows.appendChild(row);
  }

  const radio = (app.state || {}).radio || {};
  text($("fq-active-mhz"), mhz(radio.com1));
  text($("fq-active-pos"), radio.position || "");
  const who = $("fq-active-who");
  who.classList.toggle("is-off", !radio.station);
  text(who.lastElementChild,
       (radio.station || t("freq.nobody", "nobody listening")) + invented(radio));
  text($("fq-standby-mhz"), mhz(radio.com1_standby));
}

async function loadAtis() {
  const radio = (app.state || {}).radio || {};
  const ident = app.freqScope === "search" && app.freqQuery.trim()
    ? app.freqQuery.trim().toUpperCase()
    : radio.station_ident || radio.here || "";
  if (!ident) {
    app.atis = null;
    paintAtis();
    return;
  }
  const report = await get(`/api/atis?ident=${encodeURIComponent(ident)}`);
  app.atis = report;
  paintAtis();
}

function paintAtis() {
  const report = app.atis;
  const ok = report && report.ok;
  text($("atis-letter"), ok ? report.letter : "—");
  text($("atis-title"), ok ? `${report.ident} ATIS` : "ATIS");
  text($("atis-sub"), ok ? `information ${report.letter} · ${zulu()}`
                         : "nothing broadcast here");
  text($("atis-text"), ok ? report.text
    : "Tune a field with an ATIS and its wording appears here.");

  const play = $("play-atis");
  play.disabled = !ok;
  play.style.opacity = ok ? "1" : "0.4";

  const chips = $("atis-chips");
  chips.replaceChildren();
  const weather = (app.state || {}).weather;
  const items = [];
  if (ok && report.runways && report.runways.length) {
    items.push(`RWY ${report.runways.join(" ")}`);
  }
  if (weather) {
    items.push(`QNH ${weather.qnh_hpa}`);
    items.push(weather.wind_kt
      ? `${String(weather.wind_dir).padStart(3, "0")}° / ${weather.wind_kt} kt`
      : "wind calm");
  }
  for (const item of items) {
    const chip = document.createElement("div");
    chip.className = "chip";
    chip.textContent = item;
    chips.appendChild(chip);
  }
}

function paintWeather() {
  const weather = (app.state || {}).weather;
  const radio = (app.state || {}).radio || {};
  text($("metar-label"), radio.station_ident ? `${radio.station_ident} weather` : "Weather");
  text($("metar-time"), weather ? zulu() : "");
  if (!weather) {
    ["wx-wind", "wx-qnh", "wx-vis", "wx-temp"].forEach((id) => text($(id), "—"));
    return;
  }
  text($("wx-wind"), weather.wind_kt
    ? `${String(weather.wind_dir).padStart(3, "0")}° / ${weather.wind_kt} kt`
    : "calm");
  text($("wx-qnh"), `${weather.qnh_hpa}`);
  text($("wx-vis"), `${weather.visibility_sm} sm`);
  text($("wx-temp"), `${weather.temperature_c}°C`);
}

// ------------------------------------------------------------ settings ---

async function loadSettings() {
  const data = await get("/api/settings");
  if (!data) return;
  app.settings = data.config;
  app.devices = data.devices;
  app.version = data.version || "";
  app.setupOnly = Boolean(data.setup_only);
  if (data.version) text($("app-version"), `v${data.version}`);
  app.announcements = data.announcements;
  paintSettings();
}

function fillDevices(select, devices, current) {
  select.replaceChildren();
  const auto = document.createElement("option");
  auto.value = "";
  auto.textContent = t("set.system_default", t("set.system_default", "System default"));
  select.appendChild(auto);
  for (const device of devices || []) {
    const option = document.createElement("option");
    option.value = String(device.index);
    option.textContent = `${device.index} · ${device.name}`;
    select.appendChild(option);
  }
  select.value = current === null || current === undefined ? "" : String(current);
}

function toggle(id, value) {
  const node = $(id);
  if (node) node.setAttribute("aria-pressed", value ? "true" : "false");
}

function chips(containerId, attribute, value) {
  for (const chip of $(containerId).querySelectorAll(".chip")) {
    chip.setAttribute("aria-pressed",
      chip.dataset[attribute] === String(value) ? "true" : "false");
  }
}

function paintSettings() {
  const config = app.settings;
  if (!config) return;

  fillDevices($("set-input"), app.devices.inputs, config.audio.input_device);
  fillDevices($("set-output"), app.devices.outputs, config.audio.output_device);
  toggle("set-radio-effects", config.audio.radio_effects);
  toggle("set-landing-report", config.ui.landing_report);
  toggle("set-check-updates", config.ui.check_updates);
  paintLook(LOOK.apply(lookFrom(config.ui)));
  $("set-volume").value = Math.round(config.audio.volume * 100);
  text($("volume-value"), `${Math.round(config.audio.volume * 100)}%`);

  chips("ptt-modes", "mode", config.ptt.mode);
  $("set-ptt-key").value = config.ptt.key;
  $("ptt-key-block").hidden = config.ptt.mode !== "keyboard";
  $("ptt-stick-block").hidden = config.ptt.mode !== "joystick";
  paintSticks();
  $("set-joystick-button").value = config.ptt.joystick_button;

  paintLanguage();
  paintInterfaceLanguages(config.ui && config.ui.language);
  chips("dialect-chips", "dialect", config.flight.region);
  $("set-simbrief").value = (config.simbrief && config.simbrief.username) || "";
  toggle("set-gsx", config.gsx ? config.gsx.enabled : true);
  toggle("set-gsx-control", config.gsx ? config.gsx.control : true);
  toggle("set-gsx-voice", config.gsx ? config.gsx.voice : true);
  toggle("set-traffic", config.traffic.enabled);
  toggle("set-traffic-gsx", config.traffic.gsx_vehicles);
  if ($("set-traffic-gsx")) {
    // Nothing to choose without GSX Pro: shown, but not pressable.
    $("set-traffic-gsx").disabled = !config.traffic.gsx_installed;
  }
  for (const id of ["debug-board", "debug-arrival"]) {
    if ($(id)) $(id).hidden = !config.traffic.debug_triggers;
  }
  chips("density-chips", "density", String(config.traffic.density));
  chips("callsign-chips", "callsigns", config.traffic.callsigns || "auto");
  chips("source-chips", "source", config.traffic.source_resolved || "wilcoatc");
  toggle("set-traffic-live", config.traffic.live ? config.traffic.live.enabled : false);
  if ($("set-traffic-live")) {
    // With FSLTL flying the traffic there is no live feed to switch on.
    $("set-traffic-live").disabled = config.traffic.source_resolved === "fsltl";
  }
  toggle("set-strict-digits", config.atc.strict_icao_digits);
  toggle("set-handoff", config.atc.proactive_handoff);
  toggle("set-radio-power", config.atc.radio_needs_power);
  toggle("set-wx-online", config.weather.online);
  toggle("set-wx-sim", config.weather.prefer_sim);
  if (config.sim) toggle("set-sim", config.sim.enabled);
  paintImmersion(config.immersion);
  paintOperator(config.delivery);
}

/**
 * The rest of the aeroplane, on the settings screen.
 *
 * One switch at the top decides whether any of it happens, and everything
 * under it goes dim when that switch is off. Dim rather than hidden: a
 * feature you cannot see is a feature nobody knows they have, and the point
 * of this card is that a pilot can look at it and see what they would be
 * getting before they turn it on.
 */
function paintImmersion(immersion) {
  if (!immersion) return;
  toggle("set-immersion", immersion.enabled);
  toggle("set-immersion-fo", immersion.enabled);
  toggle("set-copilot", immersion.copilot);
  toggle("set-callouts", immersion.callouts);
  toggle("set-confirmations", immersion.confirmations);
  toggle("set-checklists", immersion.checklists);
  toggle("set-readbacks", immersion.radio_readbacks);
  toggle("set-cabin", immersion.cabin);
  toggle("set-briefings", immersion.briefings);
  toggle("set-announcements", immersion.announcements);
  toggle("set-service", immersion.service);
  toggle("set-chimes", immersion.chimes);
  toggle("set-boarding-music", immersion.boarding_music);
  toggle("set-applause", immersion.applause);
  toggle("set-crew-realism", immersion.realism);
  toggle("set-bilingual", immersion.bilingual);
  toggle("set-recordings", immersion.recordings);
  toggle("set-interphone", immersion.interphone);
  chips("verbosity-chips", "verbosity", immersion.verbosity);

  const folder = $("set-recordings-dir");
  if (folder && document.activeElement !== folder) {
    folder.value = immersion.recordings_dir || "";
  }
  paintAnnouncements();

  const volume = $("set-crew-volume");
  if (volume) volume.value = Math.round(immersion.volume * 100);
  text($("crew-volume-value"), `${Math.round(immersion.volume * 100)}%`);
  const airline = $("set-airline");
  if (airline && document.activeElement !== airline) {
    airline.value = immersion.airline || "";
  }
  const captain = $("set-captain-name");
  if (captain && document.activeElement !== captain) {
    captain.value = immersion.captain_name || "";
  }

  fillVoices($("set-copilot-voice"), immersion.copilot_voice,
             immersion.copilot_gender);
  fillVoices($("set-cabin-voice"), immersion.cabin_voice,
             immersion.cabin_gender);

  for (const card of document.querySelectorAll(".card.immersion")) {
    card.classList.toggle("is-off", !immersion.enabled);
  }
}

/**
 * What is in the announcements folder.
 *
 * The folder is the whole of the setup, so this has to be able to say
 * whether anything was found in it. "I put the files there and nothing
 * happened" is otherwise a question with no answer on screen.
 */
function paintAnnouncements() {
  const box = $("announcements-readout");
  const found = app.announcements;
  if (!box || !found) return;

  if (!found.count) {
    // The folder list can be absent as well as empty -- on a first run there
    // is no engine to have looked for one -- and reading a length off that
    // threw, which took the whole settings screen down with it.
    const folders = found.folders || [];
    box.textContent = t("set.no_recordings", "nothing in the folder yet")
      + (folders.length ? ` · ${folders[folders.length - 1]}` : "");
    return;
  }
  const packs = (found.airlines || []).length
    ? (found.airlines || []).join(", ")
    : t("set.any_airline", "any airline");
  box.textContent = `${found.count} `
    + t("set.recordings_found", "recordings")
    + ` · ${packs}`;
}

/**
 * The voice picker.
 *
 * Casting is automatic and stable everywhere else in this program, which is
 * right for a controller: nobody chooses who is working Kennedy Tower. Your
 * own first officer is the one place where being able to say "not that one"
 * is worth having, because you are going to be listening to them for four
 * hours.
 *
 * The choices are the voices installed on this machine, plus "a man" and "a
 * woman", which pick automatically inside that half of the pool.
 */
/**
 * Where a voice is from, in the reader's language: "German", "British
 * English". The browser knows every language's name offline, so no catalogue
 * has to carry a hundred of them.
 */
function voiceCountry(voice) {
  const tags = { us: "en-US", gb: "en-GB", "pt-br": "pt-BR", "es-419": "es-419" };
  const tag = tags[voice.accent]
    || (voice.accent === voice.language ? voice.language
        : `${voice.language}-${voice.accent.toUpperCase()}`);
  let name = tag;
  try {
    name = new Intl.DisplayNames([I18N.language], { type: "language" }).of(tag) || tag;
  } catch (error) { /* an old browser: the code is still readable */ }
  return name.charAt(0).toLocaleUpperCase(I18N.language) + name.slice(1);
}

// The languages a crew is most often heard in come first, English and then
// French; the rest follow in the reader's alphabetical order.
const VOICE_LANGUAGE_ORDER = ["en", "fr", "de", "es", "it", "pt", "nl"];

/** The installed voices as [{label, voices}], one group per country. */
function voiceGroups() {
  const voices = (app.devices && app.devices.voices) || [];
  const groups = new Map();
  for (const voice of voices) {
    const label = voiceCountry(voice);
    if (!groups.has(label)) {
      groups.set(label, { label, language: voice.language, home: false, voices: [] });
    }
    const group = groups.get(label);
    // A language's own country ahead of its others: Spain before Latin
    // America, Portugal before Brazil.
    if (voice.accent === voice.language) group.home = true;
    group.voices.push(voice);
  }
  const rank = (language) => {
    const at = VOICE_LANGUAGE_ORDER.indexOf(language);
    return at < 0 ? VOICE_LANGUAGE_ORDER.length : at;
  };
  return [...groups.values()].sort((a, b) =>
    rank(a.language) - rank(b.language)
    || (rank(a.language) === VOICE_LANGUAGE_ORDER.length
        ? a.language.localeCompare(b.language) : 0)
    || Number(b.home) - Number(a.home)
    || a.label.localeCompare(b.label, I18N.language));
}

/** "Annika · woman": what a voice is called in a picker. */
function voiceLabel(voice) {
  return `${voice.person || voice.name} · `
    + `${voice.gender === "f" ? t("set.woman", "woman") : t("set.man", "man")}`;
}

/**
 * The crew voice options: the three casts, then each installed voice by its
 * first name, under its country. Built again only when the list or the
 * reader's language changed, because a select rebuilt under the pointer
 * closes on whoever is choosing from it.
 */
function fillVoiceOptions(select) {
  const voices = (app.devices && app.devices.voices) || [];
  const stamp = `${I18N.language}|${voices.map((v) => v.key).join(",")}`;
  if (select.dataset.stamp === stamp) return;
  select.dataset.stamp = stamp;
  select.textContent = "";
  const fixed = [
    ["auto", t("set.voice_auto", "Cast from the aircraft")],
    ["m", t("set.voice_male", "A man")],
    ["f", t("set.voice_female", "A woman")],
  ];
  for (const [value, label] of fixed) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.appendChild(option);
  }
  for (const group of voiceGroups()) {
    const node = document.createElement("optgroup");
    node.label = group.label;
    for (const voice of group.voices) {
      const option = document.createElement("option");
      option.value = voice.key;
      option.textContent = voiceLabel(voice);
      node.appendChild(option);
    }
    select.appendChild(node);
  }
}

function fillVoices(select, chosen, gender) {
  if (!select) return;
  fillVoiceOptions(select);
  // A gender was asked for rather than a person: that is what the select
  // shows, because it is what was chosen.
  if (gender === "m" || gender === "f") select.value = gender;
  else select.value = chosen && chosen !== "auto" ? chosen : "auto";
  // A voice named in the config that is not installed on this machine has no
  // option to select, and the browser would silently show the first one.
  if (!select.value) select.value = "auto";
}

// What the tug and the jetway are doing, when anything is saying.
function paintGround() {
  const box = $("ground-readout");
  if (!box) return;
  const described = (app.state && app.state.ground) || "";
  text(box, described || t("set.ground_none", "nothing connected"));
}

// The panel's own language, which is a preference: it is about the person
// reading, not about where the aeroplane is. "Follow the system" is the
// default and is what almost everybody wants.
function paintInterfaceLanguages(chosen) {
  const select = $("set-ui-language");
  if (!select) return;
  if (!select.options.length) {
    const auto = document.createElement("option");
    auto.value = "auto";
    auto.textContent = t("set.follow_system", "Follow the system");
    select.appendChild(auto);
    for (const [code, name] of I18N.LANGUAGES) {
      const option = document.createElement("option");
      option.value = code;
      option.textContent = name;
      select.appendChild(option);
    }
  } else {
    select.options[0].textContent = t("set.follow_system", "Follow the system");
  }
  select.value = chosen || "auto";
}

// Which languages are live on the radio is not a setting: it follows the
// aeroplane. This only reports what is being listened for right now.
function paintLanguage() {
  const box = $("language-readout");
  if (!box) return;
  // Named in the language of the window, not in the language of the radio:
  // a German reader is told "Englisch und Französisch", not "English and
  // French" and not "Deutsch und Französisch".
  const live = (app.state && app.state.languages) || [];
  const names = live.map((l) => t(`lang.${l.code}`, l.name || l.code.toUpperCase()));
  const joiner = ` ${t("lang.and", "and")} `;
  text(box, names.length ? names.join(joiner) : t("lang.en", "English"));
}

function paintSettingsLive() {
  const state = app.state;
  if (!state) return;
  paintLanguage();
  paintGround();
  paintWeather();
  const line = $("sim-line");
  if (line) {
    // A stand-in source reports itself connected, because it is: the engine
    // has a state to read. It is not a simulator, though, and saying so would
    // send somebody looking for a fault in MSFS that is not there.
    const linked = state.source === "simconnect" && state.connected;
    line.classList.toggle("is-off", !linked);
    text(line.lastElementChild, linked
      ? t("set.sim_connected", "Microsoft Flight Simulator · connected")
      : t("set.sim_stand_in", "no simulator — flying on a stand-in position"));
    text($("sim-session"), clock(state.session_s));
    text($("sim-source"), state.source || "—");
    // The engine reports which model is doing the understanding, if any; the
    // sentence around it belongs to whoever is reading the window.
    const model = state.understanding_model;
    text($("understanding"), model
      ? t("set.rules_plus", "Understanding: rules, with {model} for anything they miss.")
          .replace("{model}", model)
      : t("set.rules_only", "Understanding: rules only."));
    const stats = state.stats || {};
    text($("stats"), stats.total
      ? `${stats.rules || 0} ${t("set.by_rules", "by the rules")}`
        + ` · ${stats.ai || 0} ${t("set.by_model", "by the model")}`
        + ` · ${stats.failed || 0} ${t("set.not_understood", "not understood")}`
      : "");
  }
}

async function save(changes, noteId) {
  const result = await post("/api/settings", changes);
  if (!result.ok) {
    note(noteId, result.error || "could not save", true);
    return;
  }
  app.settings = result.config;
  paintSettings();
  if (result.restart_required.length) {
    note(noteId, t("set.saved_restart",
                   "saved — restart WilcoATC for this to take effect"), true);
  } else {
    note(noteId, t("set.saved", "saved"));
  }
}

// ------------------------------------------------------ settings sections ---

/**
 * The list of sections beside the settings, and which one is open.
 *
 * Built from the cards rather than written out, so it can never list a
 * section that is not there or miss one that is: each card says what it is
 * (data-section) and which icon it gets (data-icon), and its own heading is
 * its name -- already in the reader's language, because i18n.js translated it.
 */
const SECTION_ICONS = {
  gear: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.8 1.2V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-2.9-1.2l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0-1.2-2.9H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.2-2.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 10 3.1V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0 1.2 2.9H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  palette: "M12 22a10 10 0 1 1 10-10c0 2.8-2.2 4-4 4h-2a2 2 0 0 0-1.5 3.3A1.7 1.7 0 0 1 12 22zM7.5 11.5h.01M10.5 7.5h.01M15.5 8.5h.01",
  plan: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M8 13h8M8 17h5",
  cloud: "M17.5 19H9a7 7 0 1 1 6.7-9h1.8a4.5 4.5 0 1 1 0 9z",
  speaker: "M11 5L6 9H2v6h4l5 4V5zM15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14",
  person: "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
  mic: "M9 3h6v11a3 3 0 0 1-6 0V3zM5 11a7 7 0 0 0 14 0M12 18v3",
  radio: "M4 16a9 9 0 0 1 16 0M7.5 18.5a5 5 0 0 1 9 0M12 20h.01",
  truck: "M3 6h11v9H3zM14 9h4l3 3v3h-7zM7 18.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3zM17 18.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z",
  plane: "M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z",
  screen: "M3 4h18v12H3zM8 20h8M12 16v4",
  box: "M21 8l-9-5-9 5v8l9 5 9-5zM3 8l9 5 9-5M12 13v8",
  cabin: "M7 4v9a2 2 0 0 0 2 2h7M7 9h6M16 15l2 5M9 15l-1 5",
  headset: "M3 18v-6a9 9 0 0 1 18 0v6M21 19a2 2 0 0 1-2 2h-1v-6h3zM3 19a2 2 0 0 0 2 2h1v-6H3z",
};

function sectionCards() {
  return [...document.querySelectorAll(".settings-grid > .card[data-section]")];
}

function buildSettingsNav() {
  const nav = $("settings-nav");
  if (!nav) return;
  const heading = document.createElement("div");
  heading.className = "heading";
  heading.textContent = t("set.preferences", "Preferences");
  const items = sectionCards().map((card) => {
    const button = document.createElement("button");
    button.className = "section-item";
    button.dataset.section = card.dataset.section;
    const path = SECTION_ICONS[card.dataset.icon] || SECTION_ICONS.gear;
    button.innerHTML = '<span class="icon"><svg width="16" height="16" viewBox="0 0 24 24" '
      + 'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
      + `stroke-linejoin="round"><path d="${path}"/></svg></span>`
      + '<span class="name"></span><span class="count"></span>';
    const title = card.querySelector(".card-head h2");
    text(button.querySelector(".name"), title ? title.textContent.trim() : card.dataset.section);
    text(button.querySelector(".count"),
         String(card.querySelectorAll(".setting, .field-block").length || ""));
    on(button, "click", () => openSection(card.dataset.section));
    return button;
  });
  nav.replaceChildren(heading, ...items);
  openSection(app.section);
}

function openSection(name) {
  const cards = sectionCards();
  if (!cards.length) return;
  const wanted = cards.some((card) => card.dataset.section === name)
    ? name : cards[0].dataset.section;
  app.section = wanted;
  for (const card of cards) card.classList.toggle("is-open", card.dataset.section === wanted);
  for (const button of document.querySelectorAll(".section-item")) {
    button.setAttribute("aria-current", button.dataset.section === wanted ? "true" : "false");
  }
  try { localStorage.setItem("wilco.section", wanted); } catch (error) { /* fine */ }
}

// ------------------------------------------------------------------ look ---

/**
 * Personalisation: the theme, the accent, the room behind the glass.
 *
 * Painted by look.js, which this only feeds. A change is shown the moment it
 * is picked and saved after; config.yaml is what the next window opens in.
 */
function lookFrom(ui) {
  return LOOK.clean({
    theme: ui.theme, accent: ui.accent, scene: ui.scene,
    blur: ui.blur, lights: ui.lights, grid: ui.grid, gradient: ui.gradient,
  });
}

// The same three pickers are on Settings, Personalisation and on the
// first-run setup's second screen.
const LOOK_THEMES = "#look-themes .look, #ob-look-themes .look";
const LOOK_SCENES = "#look-scenes .look, #ob-look-scenes .look";

function paintLook(look) {
  for (const button of document.querySelectorAll(LOOK_THEMES)) {
    button.setAttribute("aria-pressed", button.dataset.theme === look.theme ? "true" : "false");
  }
  for (const button of document.querySelectorAll(LOOK_SCENES)) {
    button.setAttribute("aria-pressed", button.dataset.scene === look.scene ? "true" : "false");
  }
  for (const swatch of document.querySelectorAll(".swatches .swatch[data-accent]")) {
    swatch.setAttribute("aria-pressed", swatch.dataset.accent === look.accent ? "true" : "false");
  }
  for (const id of ["look-custom", "ob-look-custom"]) {
    if ($(id)) $(id).value = look.accent;
  }
  toggle("look-lights", look.lights);
  toggle("look-grid", look.grid);
  toggle("look-gradient", look.gradient);
  if ($("look-blur")) $("look-blur").value = String(look.blur);
  text($("look-blur-value"), `${look.blur} px`);
}

/** Show it now, and save whatever differs from what is saved already. */
function pickLook(changes) {
  const saved = app.settings ? lookFrom(app.settings.ui) : LOOK.current;
  const next = LOOK.apply({ ...LOOK.current, ...changes });
  paintLook(next);
  const differs = {};
  for (const [name, value] of Object.entries(next)) {
    if (saved[name] !== value) differs[`ui.${name}`] = value;
  }
  if (Object.keys(differs).length) save(differs, "saved-look");
}

function wireLook() {
  for (const swatches of [$("look-swatches"), $("ob-look-swatches")]) {
    if (!swatches) continue;
    const custom = swatches.querySelector(".custom");
    for (const colour of LOOK.SWATCHES) {
      const swatch = document.createElement("button");
      swatch.className = "swatch";
      swatch.dataset.accent = colour;
      swatch.style.background = colour;
      swatch.setAttribute("aria-label", colour);
      on(swatch, "click", () => pickLook({ accent: colour }));
      swatches.insertBefore(swatch, custom);
    }
  }
  // Each scene drawn in its own two lights, over the room's own colour.
  for (const button of document.querySelectorAll(LOOK_SCENES)) {
    const scene = LOOK.SCENES[button.dataset.scene];
    const [one, two] = scene || ["color-mix(in oklch, var(--c1) 40%, transparent)",
                                 "color-mix(in oklch, var(--c4) 28%, transparent)"];
    button.querySelector(".preview").style.background =
      `radial-gradient(60px 40px at 90% 0%, ${one}, transparent),`
      + ` radial-gradient(60px 40px at 0% 100%, ${two}, transparent), rgb(var(--bg-rgb))`;
    on(button, "click", () => pickLook({ scene: button.dataset.scene }));
  }
  for (const button of document.querySelectorAll(LOOK_THEMES)) {
    on(button, "click", () => pickLook({ theme: button.dataset.theme }));
  }
  // The colour picker reports as it is dragged; it is shown as it moves and
  // saved once it is let go.
  for (const id of ["look-custom", "ob-look-custom"]) {
    on($(id), "input", (e) => LOOK.apply({ ...LOOK.current, accent: e.target.value }));
    on($(id), "change", (e) => pickLook({ accent: e.target.value }));
  }
  on($("look-lights"), "click", () => pickLook({ lights: !LOOK.current.lights }));
  on($("look-grid"), "click", () => pickLook({ grid: !LOOK.current.grid }));
  on($("look-gradient"), "click", () => pickLook({ gradient: !LOOK.current.gradient }));
  on($("look-blur"), "input", (e) => {
    LOOK.apply({ ...LOOK.current, blur: Number(e.target.value) });
    text($("look-blur-value"), `${e.target.value} px`);
  });
  on($("look-blur"), "change", (e) => pickLook({ blur: Number(e.target.value) }));
  on($("look-reset"), "click", () => pickLook({ ...LOOK.DEFAULT }));
  // The chart is drawn in the theme's colours, so it is drawn again.
  document.addEventListener("look:changed", () => {
    if (app.screen === "airspace") repaintAirspace();
  });
}

// ------------------------------------------------------------- changelog ---

/**
 * What changed lately, newest first. The entries are changelog.json, which
 * ships with the window; they are read when the dialog opens rather than at
 * start, because nobody needs them to fly.
 */
async function openChangelog() {
  const list = $("changelog-list");
  if (!app.changelog) {
    const entries = await get("/changelog.json");
    app.changelog = Array.isArray(entries) ? entries : [];
  }
  const format = new Intl.DateTimeFormat(I18N.language,
    { day: "numeric", month: "short", year: "numeric" });
  list.replaceChildren(...app.changelog.map((entry) => {
    const item = document.createElement("li");
    const when = document.createElement("div");
    when.className = "when";
    const date = new Date(`${entry.date}T12:00:00Z`);
    when.textContent = Number.isNaN(date.getTime()) ? entry.date : format.format(date);
    const what = document.createElement("div");
    what.className = "what";
    what.textContent = entry.title || "";
    const detail = document.createElement("div");
    detail.className = "detail";
    detail.textContent = entry.detail || "";
    item.append(when, what, detail);
    return item;
  }));
  $("changelog-dialog").showModal();
}

function wireChangelog() {
  on($("open-changelog"), "click", openChangelog);
  on($("close-changelog"), "click", () => $("changelog-dialog").close());
  // A click on the dim around the card closes it, as in the design: the
  // dialog element itself is only ever the target outside its content.
  on($("changelog-dialog"), "click", (event) => {
    if (event.target === event.currentTarget) event.currentTarget.close();
  });
}

// ----------------------------------------------------------- immersion ---

/**
 * The switches for the rest of the aeroplane.
 *
 * A flat table rather than a handler each, because there are fourteen of
 * them and they all do exactly the same thing: flip a boolean and save it.
 * The interesting behaviour is elsewhere -- what any of them means is in
 * crew.py, and none of it can change what the controller says.
 */
const IMMERSION_SWITCHES = {
  "set-immersion": "immersion.enabled",
  // The same switch, at the head of the first officer's card.
  "set-immersion-fo": "immersion.enabled",
  "set-copilot": "immersion.copilot",
  "set-callouts": "immersion.callouts",
  "set-confirmations": "immersion.confirmations",
  "set-checklists": "immersion.checklists",
  "set-readbacks": "immersion.radio_readbacks",
  "set-cabin": "immersion.cabin",
  "set-briefings": "immersion.briefings",
  "set-announcements": "immersion.announcements",
  "set-service": "immersion.service",
  "set-chimes": "immersion.chimes",
  "set-boarding-music": "immersion.boarding_music",
  "set-applause": "immersion.applause",
  "set-crew-realism": "immersion.realism",
  "set-bilingual": "immersion.bilingual",
  "set-recordings": "immersion.recordings",
  "set-interphone": "immersion.interphone",
};

/**
 * Fetch the latest SimBrief plan and write it over the flight.
 *
 * Shared by the card on the settings screen and the button in the flight
 * editor, because they do the same thing to the same three fields and a
 * second copy would be a second set of error messages. Returns the settings
 * the engine came back with, or null.
 */
async function importSimbrief(noteId) {
  const username = ($("set-simbrief").value || "").trim();
  if (!username) {
    // The username is the whole of the setup, so saying "failed" here would
    // be true and useless.
    note(noteId, t("set.simbrief_no_user",
                   "Put your SimBrief username in Settings first."), true);
    return null;
  }
  note(noteId, t("set.simbrief_loading", "fetching…"));
  const answer = await post("/api/simbrief", { username });
  if (answer && answer.ok) {
    app.settings = answer.settings;
    paintSettings();
    note(noteId, answer.plan);
    return answer.settings;
  }
  note(noteId, (answer && answer.error) || "failed", true);
  return null;
}

/** Put a flight onto the editor's fields, leaving anything it does not name. */
function fillFlightForm(flight) {
  const form = $("flight-form");
  for (const [name, value] of Object.entries(flight || {})) {
    if (form.elements[name]) form.elements[name].value = value || "";
  }
}

/**
 * The person on the radio, on the settings screen.
 *
 * Three switches over one config section. Breathing and the wind are levels
 * rather than flags in ``config.yaml`` -- the panel has no business offering
 * a dB slider in flight -- so the switch sends the default level or the one
 * that means "off", and reads back as on when the level is audible.
 */
const OPERATOR_OFF_DB = -90.0;
const OPERATOR_LEVELS = {
  "set-breathing": ["delivery.imperfections.breath_db", -26.0],
  "set-wind": ["delivery.imperfections.wind_db", -42.0],
};

function paintOperator(delivery) {
  const operator = (delivery || {}).imperfections;
  if (!operator) return;
  toggle("set-operator", operator.enabled);
  for (const [id, [path]] of Object.entries(OPERATOR_LEVELS)) {
    toggle(id, operator[path.split(".").pop()] > OPERATOR_OFF_DB);
  }
  toggle("set-hesitation", operator.hesitation_chance > 0
                           || operator.mid_hesitation_chance > 0);
  // Dim rather than hide, the same way the immersion card does it: a
  // setting you cannot see is a setting nobody knows they have.
  const card = document.querySelector(".card.operator");
  if (card) card.classList.toggle("is-off", !operator.enabled);
}

function wireOperator() {
  on($("set-operator"), "click", (event) => {
    const next = event.currentTarget.getAttribute("aria-pressed") !== "true";
    save({ "delivery.imperfections.enabled": next }, "saved-operator");
  });
  for (const [id, [path, level]] of Object.entries(OPERATOR_LEVELS)) {
    on($(id), "click", (event) => {
      const next = event.currentTarget.getAttribute("aria-pressed") !== "true";
      save({ [path]: next ? level : OPERATOR_OFF_DB }, "saved-operator");
    });
  }
  // Both fillers move together. They are one thing to a listener -- somebody
  // saying "uh" -- and two settings only because they happen in different
  // places in the sentence.
  on($("set-hesitation"), "click", (event) => {
    const next = event.currentTarget.getAttribute("aria-pressed") !== "true";
    save({
      "delivery.imperfections.hesitation_chance": next ? 0.05 : 0.0,
      "delivery.imperfections.mid_hesitation_chance": next ? 0.06 : 0.0,
    }, "saved-operator");
  });
}

/** "saved" is said on the card that was touched: the first officer and the
 * cabin are two sections, and only one of them is on screen. */
function immersionNote(element) {
  const card = element && element.closest(".card");
  const box = card && card.querySelector(".saved");
  return box ? box.id : "saved-immersion";
}

function wireImmersion() {
  for (const [id, path] of Object.entries(IMMERSION_SWITCHES)) {
    on($(id), "click", (event) => {
      const next = event.currentTarget.getAttribute("aria-pressed") !== "true";
      save({ [path]: next }, immersionNote(event.currentTarget));
    });
  }

  for (const chip of ($("verbosity-chips") || { querySelectorAll: () => [] })
       .querySelectorAll(".chip")) {
    on(chip, "click", () =>
      save({ "immersion.verbosity": chip.dataset.verbosity }, "saved-immersion"));
  }

  // The two voice pickers. "A man" and "a woman" are a gender rather than a
  // person, so they clear whatever voice was pinned; naming a voice clears
  // the gender for the same reason. Sending both would be asking for a
  // specific person and also for anybody of the other sex.
  const pickers = [
    ["set-copilot-voice", "immersion.copilot_voice", "immersion.copilot_gender"],
    ["set-cabin-voice", "immersion.cabin_voice", "immersion.cabin_gender"],
  ];
  for (const [id, voicePath, genderPath] of pickers) {
    on($(id), "change", (event) => {
      const value = event.target.value;
      const where = immersionNote(event.target);
      if (value === "m" || value === "f") {
        save({ [voicePath]: "auto", [genderPath]: value }, where);
      } else {
        save({ [voicePath]: value, [genderPath]: "auto" }, where);
      }
    });
  }

  const volume = $("set-crew-volume");
  on(volume, "input", () => {
    text($("crew-volume-value"), `${volume.value}%`);
  });
  on(volume, "change", () =>
    save({ "immersion.volume": Number(volume.value) / 100 }, "saved-immersion"));

  const airline = $("set-airline");
  let airlineTimer = null;
  on(airline, "input", () => {
    clearTimeout(airlineTimer);
    airlineTimer = setTimeout(
      () => save({ "immersion.airline": airline.value.trim() },
                 "saved-immersion"), 700);
  });

  const captain = $("set-captain-name");
  let captainTimer = null;
  on(captain, "input", () => {
    clearTimeout(captainTimer);
    captainTimer = setTimeout(
      () => save({ "immersion.captain_name": captain.value.trim() },
                 "saved-immersion"), 700);
  });

  const folder = $("set-recordings-dir");
  let folderTimer = null;
  on(folder, "input", () => {
    clearTimeout(folderTimer);
    folderTimer = setTimeout(async () => {
      await save({ "immersion.recordings_dir": folder.value.trim() },
                 "saved-immersion");
      // The folder has changed, so what is in it has too.
      const found = await post("/api/announcements/rescan", {});
      if (found.ok) { app.announcements = found.announcements; }
      paintAnnouncements();
    }, 800);
  });

  on($("announcements-rescan"), "click", async () => {
    const found = await post("/api/announcements/rescan", {});
    if (found.ok) app.announcements = found.announcements;
    paintAnnouncements();
    toast(app.announcements && app.announcements.count
      ? `${app.announcements.count} ${t("set.recordings_found", "recordings")}`
      : t("set.no_recordings", "nothing in the folder yet"));
  });

  // The voice in the picker, not the one saved: hearing somebody is how
  // the choice is made.
  on($("fo-listen"), "click", async () => {
    const answer = await post("/api/voice-preview", { voice: $("set-copilot-voice").value });
    toast(answer && answer.ok ? answer.text : (answer && answer.error) || "failed");
  });

  on($("crew-test"), "click", async () => {
    const result = await post("/api/crew-test", {});
    toast(result.ok ? result.text
                    : (result.error || t("set.crew_off",
                                         "turn the crew on first")));
  });
}

// ------------------------------------------------------------ map input ---

/**
 * Pan, zoom, and pick things off the map.
 *
 * Everything here moves the view first and fetches afterwards. The map draws
 * from data that is already in the page, so dragging is as smooth as the
 * browser can redraw and the server is asked for the new window only once
 * the hand has stopped -- which is what makes a map that can be thrown
 * across a continent feel like a map rather than like a form submission.
 */
/**
 * The map in motion: a zoom easing towards where the wheel sent it, and a
 * throw that carries on after the hand lets go.
 *
 * Every zoom used to be a jump, and every wheel event its own jump -- so a
 * trackpad pinch, which sends dozens of events, zoomed a continent in half a
 * second, and a mouse zoomed in lurches. Now each gesture only says how far
 * it wants to go (``zoom`` is the logarithm of the factor still to apply)
 * and the frames get there, a fixed share of the way each time.
 */
const motion = {
  zoom: 0,
  at: null,          // [x, y] in map units to zoom about, or null: the middle
  vx: 0,             // a throw, in map units per millisecond
  vy: 0,
  last: 0,
  frame: 0,
};

// How much of the zoom still to go is done each 60th of a second: quick to
// start, gentle to land, and over in about a quarter of a second.
const ZOOM_EASE = 0.3;
// How quickly a throw dies away: the time for it to lose two thirds of its
// speed. What the common slippy maps use, and what hands expect.
const THROW_DECAY_MS = 325;
// A mouse notch is a hundred pixels on most systems. This makes one about
// half as wide again, and a trackpad pinch -- small deltas, many of them --
// the same distance for the same movement of the fingers.
const WHEEL_PX_PER_DOUBLING = 170;
const PINCH_PX_PER_DOUBLING = 100;

function animateMap() {
  if (!motion.frame) motion.frame = requestAnimationFrame(stepMap);
}

function stopMotion() {
  motion.vx = 0;
  motion.vy = 0;
  motion.zoom = 0;
}

/** Ask for a zoom by ``log`` (natural log of the factor), about a point. */
function zoomMap(log, x, y) {
  // Never more than eight times either way still outstanding, so a flick of
  // a free-spinning wheel does not carry on zooming long after it stopped.
  motion.zoom = Math.max(-2.08, Math.min(2.08, motion.zoom + log));
  motion.at = x === undefined ? null : [x, y];
  animateMap();
}

function stepMap(time) {
  motion.frame = 0;
  const dt = motion.last ? Math.min(64, time - motion.last) : 16.7;
  motion.last = time;
  let busy = false;

  if (motion.zoom) {
    const share = 1 - Math.pow(1 - ZOOM_EASE, dt / 16.7);
    const step = Math.abs(motion.zoom) < 0.003 ? motion.zoom : motion.zoom * share;
    const before = Airspace.view.k;
    const model = window.Airspace3D;
    if (model && model.active) {
      // The model zooms about its middle: under a tilted camera the point
      // beneath the pointer is not where the chart thinks it is.
      Airspace.zoomAt(Math.exp(step));
    } else if (motion.at) {
      Airspace.zoomAt(Math.exp(step), motion.at[0], motion.at[1]);
    } else {
      Airspace.zoomAt(Math.exp(step));
    }
    // At the end of the range there is nowhere left to go, and the rest of
    // the zoom is thrown away rather than waited out.
    motion.zoom = Airspace.view.k === before ? 0 : motion.zoom - step;
    busy = busy || motion.zoom !== 0;
  }

  if (motion.vx || motion.vy) {
    Airspace.panBy(motion.vx * dt, motion.vy * dt);
    const decay = Math.exp(-dt / THROW_DECAY_MS);
    motion.vx *= decay;
    motion.vy *= decay;
    if (Math.hypot(motion.vx, motion.vy) < 0.01) {
      motion.vx = 0;
      motion.vy = 0;
    }
    busy = busy || Boolean(motion.vx || motion.vy);
  }

  paintMap();
  scheduleMapFetch();
  if (busy) animateMap();
  else motion.last = 0;
}

function wireMap() {
  const map = $("map");
  if (!map) return;

  Airspace.loadWorld().then(() => {
    if (app.screen === "airspace") paintAirspace();
  });

  // A finer coastline arrives after the zoom that asked for it, so the map
  // has to be told to draw again. Cheap, and it stops for good once every
  // level the session needs has landed.
  setInterval(() => {
    if (app.screen === "airspace" && Airspace.loading()) paintAirspace();
  }, 700);

  // The SVG is drawn in its own units and displayed at whatever size the
  // pane happens to be, so a pointer moved by one screen pixel has not moved
  // the map by one map pixel. Without this the drag lags the finger on a
  // small window and outruns it on a large one.
  const ratio = () => {
    const box = map.getBoundingClientRect();
    if (!box.width) return 1;
    const drawn = (map.viewBox && map.viewBox.baseVal && map.viewBox.baseVal.width)
      || box.width;
    return drawn / box.width;
  };

  // --- dragging, and pinching ---
  //
  // Every finger and every button comes through the same pointer events, so
  // the two gestures are one piece of bookkeeping: one pointer down is a
  // drag, two is a pinch. The panel is meant to work on a tablet propped
  // beside the yoke, and a map you cannot pinch on a tablet is a map you
  // cannot zoom at all -- there is no wheel out there.
  const down = new Map();
  let dragging = null;
  let pinch = null;
  let moved = 0;
  // The last few movements of a drag, for how fast it was going when the
  // hand let go: (time, dx, dy) in map units.
  let trail = [];

  const spread = () => {
    const [a, b] = [...down.values()];
    return {
      gap: Math.hypot(a.x - b.x, a.y - b.y),
      x: (a.x + b.x) / 2,
      y: (a.y + b.y) / 2,
    };
  };

  // The right button, or Shift with the left, turns and tilts the model.
  const turning = (event) => Boolean(window.Airspace3D && window.Airspace3D.active
    && (event.button === 2 || event.shiftKey));
  on(map, "contextmenu", (event) => {
    if (window.Airspace3D && window.Airspace3D.active) event.preventDefault();
  });

  on(map, "pointerdown", (event) => {
    if (event.pointerType === "mouse" && event.button !== 0
        && !(event.button === 2 && window.Airspace3D && window.Airspace3D.active)) return;
    down.set(event.pointerId, { x: event.clientX, y: event.clientY });
    try { map.setPointerCapture(event.pointerId); } catch (error) { /* fine */ }
    // A hand on the map catches it: a throw or a zoom still going stops.
    stopMotion();
    trail = [];

    if (down.size === 2) {
      // A second finger: the drag becomes a pinch, and the gap between them
      // becomes the zoom.
      dragging = null;
      pinch = spread();
      map.classList.remove("is-dragging");
      return;
    }
    if (down.size === 1) {
      dragging = { x: event.clientX, y: event.clientY, id: event.pointerId,
                   turn: turning(event) };
      moved = 0;
      map.classList.add("is-dragging");
    }
  });

  on(map, "pointermove", (event) => {
    if (!down.has(event.pointerId)) return;
    down.set(event.pointerId, { x: event.clientX, y: event.clientY });
    const r = ratio();

    if (pinch && down.size === 2) {
      const now = spread();
      if (pinch.gap > 8 && now.gap > 8) {
        const box = map.getBoundingClientRect();
        Airspace.zoomAt(pinch.gap / now.gap,
                        (now.x - box.left) * r, (now.y - box.top) * r);
        // And the fingers may have moved as well as spread.
        Airspace.panBy((now.x - pinch.x) * r, (now.y - pinch.y) * r);
      }
      pinch = now;
      moved += 20;              // never a click
      repaintAirspace();
      return;
    }

    if (!dragging || event.pointerId !== dragging.id) return;
    const dx = event.clientX - dragging.x;
    const dy = event.clientY - dragging.y;
    dragging.x = event.clientX;
    dragging.y = event.clientY;
    moved += Math.abs(dx) + Math.abs(dy);
    const model = window.Airspace3D;
    if (model && model.active) {
      if (dragging.turn) {
        model.turn(dx, dy);
      } else {
        // On the model a drag moves the ground under the hand, whichever
        // way the camera has been turned.
        const to = model.panPixels(dx, dy);
        if (to) {
          Airspace.follow(false);
          app.followCallsign = "";
          Airspace.setCentre(to.lat, to.lon);
        }
      }
      repaintAirspace();
      return;
    }
    Airspace.panBy(dx * r, dy * r);
    trail.push([event.timeStamp, dx * r, dy * r]);
    while (trail.length > 2 && event.timeStamp - trail[0][0] > 100) trail.shift();
    repaintAirspace();
  });

  // How fast the drag was going as it ended, or nothing if the hand had
  // already stopped: a map that lurches off after being put down carefully
  // is worse than one that never glides.
  const throwOf = (at) => {
    const recent = trail.filter(([t]) => at - t <= 80);
    if (recent.length < 2 || at - recent[recent.length - 1][0] > 50) return null;
    const span = Math.max(16, at - recent[0][0]);
    const sx = recent.reduce((sum, [, x]) => sum + x, 0);
    const sy = recent.reduce((sum, [, , y]) => sum + y, 0);
    const vx = sx / span;
    const vy = sy / span;
    // Anything slower than a few pixels a frame is a hand coming to rest.
    return Math.hypot(vx, vy) > 0.25 ? [vx, vy] : null;
  };

  const release = (event) => {
    if (event) {
      down.delete(event.pointerId);
      try {
        if (map.hasPointerCapture(event.pointerId)) {
          map.releasePointerCapture(event.pointerId);
        }
      } catch (error) { /* the pointer is already gone */ }
    } else {
      down.clear();
    }
    if (down.size < 2) pinch = null;
    if (down.size === 0) {
      const model = window.Airspace3D;
      const thrown = dragging && !dragging.turn && event
        && !(model && model.active) ? throwOf(event.timeStamp) : null;
      if (thrown) {
        [motion.vx, motion.vy] = thrown;
        animateMap();
      }
      trail = [];
      dragging = null;
      map.classList.remove("is-dragging");
    } else if (down.size === 1 && !dragging) {
      // A finger lifted out of a pinch: what is left goes back to a drag,
      // from where it is now rather than from where the pinch started.
      const [id] = [...down.keys()];
      const at = down.get(id);
      dragging = { x: at.x, y: at.y, id };
      map.classList.add("is-dragging");
    }
  };
  on(map, "pointerup", release);
  on(map, "pointercancel", release);

  // --- the wheel ---
  //
  // It zooms, about the pointer, and eases there. That is what every map a
  // pilot has used does with it.
  //
  // It used to try to tell a trackpad from a mouse by the size of the
  // numbers, and pan when it thought it saw a trackpad. The numbers do not
  // say: a high-resolution or free-spinning wheel, smooth scrolling, and
  // most display scalings on Windows send small deltas too, and on all of
  // them turning the wheel scrolled the map up and down instead of zooming.
  // What a trackpad does that a wheel cannot is move sideways, so a swipe
  // that is mostly sideways still pans; a pinch arrives with ctrlKey set,
  // whatever the hardware, and zooms at its own rate.
  on(map, "wheel", (event) => {
    event.preventDefault();
    const box = map.getBoundingClientRect();
    const r = ratio();
    const unit = event.deltaMode === 1 ? 16 : (event.deltaMode === 2 ? 400 : 1);
    const dx = event.deltaX * unit;
    const dy = event.deltaY * unit;
    const model = window.Airspace3D;

    if (!event.ctrlKey && Math.abs(dx) > Math.abs(dy)) {
      stopMotion();
      const to = model && model.active ? model.panPixels(-dx, -dy) : null;
      if (to) {
        Airspace.follow(false);
        Airspace.setCentre(to.lat, to.lon);
      } else {
        Airspace.panBy(-dx * r, -dy * r);
      }
      repaintAirspace();
      return;
    }
    if (!dy) return;
    const per = event.ctrlKey ? PINCH_PX_PER_DOUBLING : WHEEL_PX_PER_DOUBLING;
    // One event is never more than a doubling, whatever the wheel reports.
    const doublings = Math.max(-1, Math.min(1, dy / per));
    motion.vx = 0;
    motion.vy = 0;
    zoomMap(doublings * Math.LN2,
            (event.clientX - box.left) * r, (event.clientY - box.top) * r);
  }, { passive: false });

  // --- picking ---
  on(map, "click", (event) => {
    // A drag ends in a click. A map that selected whatever happened to be
    // under the finger at the end of a throw would be unusable.
    if (moved > 6) { moved = 0; return; }
    const model = window.Airspace3D;
    if (model && model.active) {
      // An aeroplane selects it; empty ground puts the card away. (select()
      // toggles, so each case is asked for the way it has to be.)
      const callsign = model.pick(event.clientX, event.clientY);
      const chosen = Airspace.selection();
      if (callsign && callsign !== chosen) Airspace.select(callsign);
      else if (!callsign && chosen) Airspace.select(chosen);
      paintAirspace();
      return;
    }
    const mark = event.target.closest
      ? event.target.closest("[data-callsign]") : null;
    Airspace.select(mark ? mark.dataset.callsign : "");
    paintAirspace();
  });

  // Double-clicking an airport goes to it, which is the one gesture a map
  // covered in names ought to have.
  on(map, "dblclick", (event) => {
    const mark = event.target.closest
      ? event.target.closest("[data-airport]") : null;
    if (!mark) {
      // Anywhere else, closer -- or wider with Shift -- about that point.
      // The standard gesture, and the only way to zoom with one hand on a
      // tablet.
      event.preventDefault();
      const box = map.getBoundingClientRect();
      const r = ratio();
      zoomMap(event.shiftKey ? Math.LN2 : -Math.LN2,
              (event.clientX - box.left) * r, (event.clientY - box.top) * r);
      return;
    }
    const airport = ((app.airspace || {}).airports || [])
      .find((a) => a.ident === mark.dataset.airport);
    if (!airport) return;
    Airspace.follow(false);
    Airspace.setCentre(airport.lat, airport.lon);
    if (Airspace.view.spanNm > 25) Airspace.view.spanNm = 12;
    repaintAirspace();
  });

  // --- the buttons ---
  on($("map-3d"), "click", () => {
    app.show3d = !app.show3d;
    try { localStorage.setItem("wilco.map3d", app.show3d ? "1" : "0"); } catch (error) { /* fine */ }
    $("map-3d").setAttribute("aria-pressed", app.show3d ? "true" : "false");
    if (window.Airspace3D && app.modelReady) window.Airspace3D.resetOrbit();
    paintAirspace();
  });
  on($("zoom-in"), "click", () => zoomMap(Math.log(0.5)));
  on($("zoom-out"), "click", () => zoomMap(Math.log(2)));
  on($("map-follow"), "click", () => {
    app.followCallsign = "";
    const following = Airspace.follow();
    if (following && app.airspace && app.airspace.ownship) {
      Airspace.setCentre(app.airspace.ownship.lat, app.airspace.ownship.lon);
    }
    repaintAirspace();
  });

  // --- the keyboard, for a machine with no wheel and for anybody who
  //     cannot use a pointer at all ---
  on(map, "keydown", (event) => {
    const step = event.shiftKey ? 160 : 60;
    if (event.key === "ArrowLeft") Airspace.panBy(step, 0);
    else if (event.key === "ArrowRight") Airspace.panBy(-step, 0);
    else if (event.key === "ArrowUp") Airspace.panBy(0, step);
    else if (event.key === "ArrowDown") Airspace.panBy(0, -step);
    else if (event.key === "+" || event.key === "=") zoomMap(Math.log(0.5));
    else if (event.key === "-") zoomMap(Math.log(2));
    else return;
    event.preventDefault();
    repaintAirspace();
  });

  // A pane that changes shape changes how much world is in it.
  if (window.ResizeObserver) {
    new ResizeObserver(() => {
      if (app.screen === "airspace") repaintAirspace();
    }).observe(map);
  }
}

// ---------------------------------------------------------------- wiring --

function wire() {
  for (const button of document.querySelectorAll(".nav-item")) {
    on(button, "click", () => show(button.dataset.screen));
  }

  wireLook();
  wireUpdate();
  wireTour();
  wireOnboarding();
  buildSettingsNav();

  wireComponents();

  // --- comms ---
  on($("send"), "click", () => transmit());
  on($("say"), "keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); transmit(); }
  });
  const ptt = $("ptt");
  on(ptt, "pointerdown", (event) => { event.preventDefault(); ptt.setPointerCapture(event.pointerId); keyDown(); });
  on(ptt, "pointerup", keyUp);
  on(ptt, "pointercancel", keyUp);
  // The swap button on the radio, which is the button on a real one: it
  // exchanges active and standby in the aircraft rather than tuning a
  // number this page happened to be showing.
  on($("swap"), "click", async () => {
    const result = await post("/api/swap", { radio: 1 });
    if (!result.ok) toast(result.error || t("comms.no_swap", "could not swap"));
  });

  // An AI departure lined up on the runway in use, for a pilot who wants
  // something to look at, or to wait behind. The tower works it from there.
  on($("spawn-runway"), "click", async () => {
    const result = await post("/api/traffic/spawn");
    if (result.ok) return;
    const [key, english] = SPAWN_REFUSED[result.error]
      || ["comms.spawn_failed", "Could not put an aircraft on the runway."];
    toast(t(key, english));
  });

  // The test buttons, there only with traffic.debug.triggers.
  on($("debug-board"), "click", () => debugTrigger("/api/debug/board_nearest"));
  on($("debug-arrival"), "click", () => debugTrigger("/api/debug/arrival_nearest"));

  // The space bar keys the microphone, unless you are typing into something.
  on(document, "keydown", (event) => {
    if (event.code !== "Space" || event.repeat) return;
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName)) return;
    // Nor while the first-run setup is up: the space bar there is a button.
    if (app.onboard) return;
    event.preventDefault();
    keyDown();
  });
  on(document, "keyup", (event) => {
    if (event.code === "Space") keyUp();
  });

  // --- airspace ---
  for (const chip of document.querySelectorAll("[data-layer]")) {
    on(chip, "click", () => {
      const layer = chip.dataset.layer;
      if (app.layers.has(layer)) app.layers.delete(layer); else app.layers.add(layer);
      chip.setAttribute("aria-pressed", app.layers.has(layer) ? "true" : "false");
      paintAirspace();
    });
  }
  // Everything the map itself responds to: dragging, the wheel, the zoom
  // keys, and picking an aeroplane off it.
  wireMap();

  // --- frequencies ---
  for (const tab of document.querySelectorAll(".tab")) {
    on(tab, "click", () => {
      app.freqScope = tab.dataset.scope;
      for (const other of document.querySelectorAll(".tab")) {
        other.setAttribute("aria-selected", other === tab ? "true" : "false");
      }
      loadFrequencies();
    });
  }
  let searchTimer = null;
  on($("freq-search"), "input", (event) => {
    app.freqQuery = event.target.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      if (app.freqScope === "search") loadFrequencies();
      else paintFrequencies();
    }, 220);
  });
  on($("play-atis"), "click", async () => {
    if (!(app.atis && app.atis.ok)) return;
    const result = await post("/api/atis", { ident: app.atis.ident });
    toast(result.ok ? `broadcasting information ${result.letter}`
                    : "nothing to broadcast here");
  });

  // --- settings ---
  on($("set-input"), "change", (e) => save({ "audio.input_device": e.target.value }, "saved-audio"));
  on($("set-output"), "change", (e) => save({ "audio.output_device": e.target.value }, "saved-audio"));
  on($("set-radio-effects"), "click", (e) => {
    const next = e.currentTarget.getAttribute("aria-pressed") !== "true";
    save({ "audio.radio_effects": next }, "saved-audio");
  });
  on($("set-landing-report"), "click", (e) => {
    const next = e.currentTarget.getAttribute("aria-pressed") !== "true";
    save({ "ui.landing_report": next }, "saved-interface");
  });
  on($("set-volume"), "input", (e) => text($("volume-value"), `${e.target.value}%`));
  on($("set-volume"), "change", (e) =>
    save({ "audio.volume": Number(e.target.value) / 100 }, "saved-audio"));
  on($("sound-test"), "click", async () => {
    const result = await post("/api/sound-test");
    toast(result.ok ? "listen for the controller"
                    : (result.error || "tune a frequency first"));
  });

  for (const chip of $("ptt-modes").querySelectorAll(".chip")) {
    on(chip, "click", () => save({ "ptt.mode": chip.dataset.mode }, "saved-ptt"));
  }
  on($("set-ptt-key"), "change", (e) => save({ "ptt.key": e.target.value }, "saved-ptt"));
  on($("rebind"), "click", () => {
    const button = $("rebind");
    const idle = button.textContent;
    button.textContent = t("set.listening", "listening…");
    button.disabled = true;
    const capture = (event) => {
      event.preventDefault();
      document.removeEventListener("keydown", capture, true);
      button.textContent = idle;
      button.disabled = false;
      const name = keyName(event);
      $("set-ptt-key").value = name;
      save({ "ptt.key": name }, "saved-ptt");
    };
    document.addEventListener("keydown", capture, true);
  });
  on($("set-joystick"), "change", (e) => {
    const stick = (app.devices.joysticks || []).find((j) => j.id === e.target.value);
    if (stick) save(stickChoice(stick), "saved-ptt");
  });
  on($("set-joystick-button"), "change", (e) =>
    save({ "ptt.joystick_button": Number(e.target.value) }, "saved-ptt"));

  // Binding a yoke button is pressing it. The joystick is read on the server,
  // so the window does not have to have focus and the yoke does not have to
  // be on the machine showing the panel.
  on($("rebind-button"), "click", async () => {
    const button = $("rebind-button");
    const idle = button.textContent;
    button.textContent = t("set.listening", "listening…");
    button.disabled = true;
    try {
      const found = await post("/api/ptt/learn", {});
      if (found && found.ok) {
        $("set-joystick-button").value = found.button;
        await save({ ...stickChoice(found), "ptt.joystick_button": found.button },
                   "saved-ptt");
        await refreshSticks();
      } else if (found && !found.devices) {
        note("saved-ptt",
             t("set.no_controller_found", "no controller connected"), true);
      } else {
        note("saved-ptt", t("set.nothing_pressed", "nothing was pressed"),
             true);
      }
    } finally {
      button.textContent = idle;
      button.disabled = false;
    }
  });

  // One file for a controller this program cannot see on somebody else's
  // desk: what Windows and SDL list, and the buttons pressed for ten
  // seconds. Written on the machine the controllers are plugged into.
  on($("controller-report"), "click", async () => {
    const button = $("controller-report");
    const status = $("controller-report-status");
    button.disabled = true;
    status.hidden = false;
    status.textContent = t("set.controller_report_running",
                           "Press every button on your controllers now — 10 seconds…");
    try {
      const done = await post("/api/controllers/report", {});
      status.textContent = done && done.ok
        ? t("set.controller_report_saved", "Report saved: {path}").replace("{path}", done.path)
        : t("set.controller_report_failed", "The report could not be written: {reason}")
          .replace("{reason}", (done && done.reason) || "?");
    } finally {
      button.disabled = false;
    }
  });

  for (const chip of $("dialect-chips").querySelectorAll(".chip")) {
    on(chip, "click", () => save({ "flight.region": chip.dataset.dialect }, "saved-atc"));
  }
  on($("set-simbrief"), "change", (e) =>
    save({ "simbrief.username": e.target.value.trim() }, "saved-simbrief"));
  on($("simbrief-load"), "click", () => importSimbrief("saved-simbrief"));
  for (const [id, path] of Object.entries({
    "set-gsx": "gsx.enabled",
    "set-gsx-control": "gsx.control",
    "set-gsx-voice": "gsx.voice",
  })) {
    on($(id), "click", (e) => {
      const next = e.currentTarget.getAttribute("aria-pressed") !== "true";
      save({ [path]: next }, "saved-gsx");
    });
  }
  on($("set-ui-language"), "change", async (event) => {
    const wanted = event.target.value;
    await I18N.apply(wanted === "auto" ? I18N.detect() : wanted);
    save({ "ui.language": wanted }, "saved-interface");
  });
  for (const chip of $("density-chips").querySelectorAll(".chip")) {
    on(chip, "click", () =>
      save({ "traffic.density": Number(chip.dataset.density) }, "saved-traffic"));
  }
  for (const chip of $("callsign-chips").querySelectorAll(".chip")) {
    on(chip, "click", () =>
      save({ "traffic.callsigns": chip.dataset.callsigns }, "saved-traffic"));
  }
  for (const chip of $("source-chips").querySelectorAll(".chip")) {
    on(chip, "click", () =>
      save({ "traffic.source": chip.dataset.source }, "saved-traffic"));
  }
  const switches = {
    "set-strict-digits": "atc.strict_icao_digits",
    "set-handoff": "atc.proactive_handoff",
    "set-radio-power": "atc.radio_needs_power",
    "set-traffic": "traffic.enabled",
    "set-traffic-gsx": "traffic.handling.gsx_vehicles",
    "set-traffic-live": "traffic.live.enabled",
    "set-wx-online": "weather.online",
    "set-wx-sim": "weather.prefer_sim",
    "set-sim": "sim.enabled",
  };
  for (const [id, path] of Object.entries(switches)) {
    const noteId = id.startsWith("set-wx") ? "saved-weather"
      : id === "set-traffic" || id === "set-traffic-gsx"
        || id === "set-traffic-live" ? "saved-traffic"
      : id === "set-sim" ? "saved-sim" : "saved-atc";
    on($(id), "click", (e) => {
      const next = e.currentTarget.getAttribute("aria-pressed") !== "true";
      save({ [path]: next }, noteId);
    });
  }

  wireImmersion();
  wireOperator();
  wireChangelog();

  // --- the flight editor, reachable from the pencil on the flight strip ---
  on($("edit-flight"), "click", () => {
    note("saved-flight-simbrief", "");
    fillFlightForm((app.settings || {}).flight || {});
    $("flight-dialog").showModal();
  });
  // The import is here rather than only on the settings screen because this
  // is the form it fills in. Asking somebody to go and find another screen,
  // press a button there and come back is the "typing it twice" the SimBrief
  // card exists to avoid.
  on($("flight-simbrief"), "click", async () => {
    const settings = await importSimbrief("saved-flight-simbrief");
    if (settings && settings.flight) fillFlightForm(settings.flight);
  });
  // A route the simulator wrote, for a flight with no SimBrief plan. The
  // window cannot hand the server a path, so the file's text is sent.
  on($("flight-pln"), "click", () => $("flight-pln-file").click());
  on($("flight-pln-file"), "change", async (event) => {
    const file = event.target.files && event.target.files[0];
    event.target.value = "";
    if (!file) return;
    note("saved-flight-simbrief", t("flight.pln_loading", "reading the plan…"));
    const answer = await post("/api/route_file", { text: await file.text() });
    if (answer && answer.ok) {
      app.settings = answer.settings;
      paintSettings();
      if (answer.settings && answer.settings.flight) {
        fillFlightForm(answer.settings.flight);
      }
      note("saved-flight-simbrief", answer.plan);
      return;
    }
    note("saved-flight-simbrief",
         answer && answer.not_a_plan
           ? t("flight.pln_bad", "That file is not a flight plan the simulator wrote.")
           : (answer && answer.error) || "failed", true);
  });
  on($("flight-form"), "submit", async (event) => {
    if (event.submitter && event.submitter.value !== "save") return;
    const data = Object.fromEntries(new FormData($("flight-form")).entries());
    await post("/api/flight", data);
    loadSettings();
  });
}

/** espeak-free key naming, matching what pynput calls the same key. */
function keyName(event) {
  const named = {
    Backquote: "grave", Backslash: "backslash", CapsLock: "caps_lock",
    ControlLeft: "ctrl_l", ControlRight: "ctrl_r", AltLeft: "alt_l",
    AltRight: "alt_r", ShiftLeft: "shift_l", ShiftRight: "shift_r",
    Space: "space", Tab: "tab", Enter: "enter", Escape: "esc",
  };
  if (named[event.code]) return named[event.code];
  if (/^Key[A-Z]$/.test(event.code)) return event.code.slice(3).toLowerCase();
  if (/^Digit\d$/.test(event.code)) return event.code.slice(5);
  if (/^F\d{1,2}$/.test(event.code)) return event.code.toLowerCase();
  return event.key.toLowerCase();
}

// --------------------------------------------------- components and setup ---
/*
 * The three big downloads, and the report on whether everything works.
 *
 * Both of these existed only as terminal commands -- `wilcoatc setup` and
 * `wilcoatc doctor` -- which is no use to somebody who installed this by
 * double-clicking an exe and has never opened a command prompt. That pilot is
 * also the one most likely to need them: a fresh machine with nothing
 * downloaded yet is exactly the machine where the program cannot explain
 * itself.
 */

const SETUP_NAMES = {
  navdata: ["setup.navdata", "Navigation data"],
  voices: ["setup.voices", "Controller voices"],
  speech: ["setup.speech", "Speech recognition"],
};

const SETUP_STATES = {
  pending: ["setup.pending", "waiting"],
  running: ["setup.running", "downloading"],
  done: ["setup.step_done", "done"],
  failed: ["setup.failed", "failed"],
  skipped: ["setup.skipped", "already there"],
};

/** One row, mark plus name plus detail, for both lists below. */
function markRow(className, name, detail) {
  const row = document.createElement("div");
  row.className = className;
  const mark = document.createElement("span");
  mark.className = "mark";
  const body = document.createElement("div");
  body.className = "body";
  const title = document.createElement("div");
  title.className = "name";
  title.textContent = name;
  body.append(title);
  if (detail !== undefined) {
    const line = document.createElement("div");
    line.className = "detail";
    line.textContent = detail;
    body.append(line);
  }
  row.append(mark, body);
  return row;
}

/** What is installed, on the Components card in the settings. */
// ------------------------------------------------------- performance ---

/*
 * The cloned voices' model, as two profiles rather than a model's name.
 * Normal is Chatterbox Turbo: about the time it takes to say a clearance on a
 * CPU, which is why it is the one recommended. High is the standard model:
 * closer to the words (it loses half as many to the recogniser) and about
 * twice as slow without a graphics card. Measured, see CLAUDE.md, "The
 * cloning tier". Shown only where the build can clone at all; anything else
 * set by hand (multilingual, flash) leaves neither card lit.
 */
// Each profile's words are looked up when drawn, so a language changed
// while the screen is open is followed.
const PERF_PROFILES = [
  ["turbo", () => t("perf.normal", "Normal"),
   () => t("perf.normal_why", "Quick: on a processor alone, a clearance arrives in about the time it takes to say it."), true],
  ["standard", () => t("perf.high", "High"),
   () => t("perf.high_why", "The closest to the words, but on a processor alone a clearance takes about twice as long to arrive."), false],
];

function perfNode(tag, className, content) {
  const node = document.createElement(tag);
  node.className = className;
  if (content) node.textContent = content;
  return node;
}

function paintPerf(boxId, shown, where) {
  const box = $(boxId);
  if (!box) return;
  box.hidden = !shown;
  if (!shown) return;
  const current = app.settings && app.settings.voice ? app.settings.voice.hq_model : "";
  box.textContent = "";
  const head = perfNode("div", "perf-head");
  head.append(perfNode("div", "perf-title", t("perf.title", "Performance profile")),
              perfNode("div", "perf-why", t("perf.why", "For the cloned controller voices.")));
  const cards = perfNode("div", "perf-cards");
  for (const [model, name, why, recommended] of PERF_PROFILES) {
    const card = perfNode("button", "perf-card");
    card.type = "button";
    card.setAttribute("aria-pressed", String(current === model));
    const top = perfNode("span", "perf-top");
    top.append(perfNode("span", "perf-name", name()));
    if (recommended) top.append(perfNode("span", "perf-badge", t("perf.recommended", "Recommended")));
    card.append(top, perfNode("span", "perf-text", why()));
    on(card, "click", () => pickPerf(model, where));
    cards.append(card);
  }
  box.append(head, cards);
}

/**
 * A profile picked. Written like any setting; where the controllers are
 * already clones and this model is not on disk yet, it is fetched at once,
 * and the engine switches over when it arrives (nothing downloaded needs a
 * restart). From the first-run setup it is only written: the download on
 * that screen fetches whichever model is chosen.
 */
async function pickPerf(model, where) {
  if (where === "ob") {
    await obSave({ "voice.hq_model": model });
    paintObDownloads();
    return;
  }
  await save({ "voice.hq_model": model }, "saved-components");
  const setup = await loadSetup();
  if (setup && setup.hq_engine && setup.extras && !setup.extras.hq && !setup.running) {
    await post("/api/setup", { steps: ["voices"], hq: true });
    note("saved-components", t("perf.downloading", "Downloading this profile's voice model…"));
  }
}

function paintComponents() {
  const box = $("parts");
  const state = app.setup;
  if (!state) return;
  paintComponentsAlert();
  paintPerf("set-perf", Boolean(state.hq_available), "settings");
  if (!box) return;

  box.textContent = "";
  for (const key of Object.keys(SETUP_NAMES)) {
    const here = Boolean(state.installed[key]);
    const [nameKey, nameFallback] = SETUP_NAMES[key];
    const row = document.createElement("div");
    row.className = here ? "part" : "part is-missing";
    const mark = document.createElement("span");
    mark.className = "mark";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = t(nameKey, nameFallback);
    const size = document.createElement("span");
    size.className = "size";
    size.textContent = here ? t("setup.installed", "installed")
                            : `${state.sizes_mb[key]} MB`;
    row.append(mark, name, size);
    box.append(row);
  }

  const button = $("setup-open");
  if (button) {
    button.textContent = state.missing.length
      ? t("set.get_components", "Download what is missing")
      : t("set.get_again", "Check again");
  }

}

/**
 * The bar above every screen, when a download is missing.
 *
 * The Components card says the same thing, and says it on a screen nobody
 * opens until something has already gone wrong. Without the voices there is
 * no ATC at all, so it is said where a pilot is actually looking, and it
 * stays there until the download happens rather than scrolling away with the
 * transcript.
 */
function paintComponentsAlert() {
  const bar = $("components-alert");
  const state = app.setup;
  if (!bar) return;
  const missing = (state && state.missing) || [];
  if (!missing.length || (state && state.running)) {
    bar.hidden = true;
    return;
  }
  const names = missing.map((key) => {
    const named = SETUP_NAMES[key];
    return named ? t(named[0], named[1]) : key;
  });
  const megabytes = missing.reduce(
    (total, key) => total + ((state.sizes_mb || {})[key] || 0), 0);
  text($("components-alert-title"), missing.includes("voices")
    ? t("alert.no_voices", "The controllers have no voice yet")
    : t("alert.missing_title", "Something has not downloaded"));
  text($("components-alert-text"),
       `${names.join(" · ")} — ${megabytes} MB`);
  bar.hidden = false;
}

/** The setup dialog: three bars and one decision. */
function paintSetup() {
  const state = app.setup;
  const box = $("setup-steps");
  if (!box || !state) return;

  // While a run is going the server owns the list, because it knows which
  // steps were asked for. Before one, the list is whatever is not installed,
  // or everything when the pilot opened this with nothing missing.
  const planned = state.missing.length ? state.missing : Object.keys(SETUP_NAMES);
  const steps = state.steps.length ? state.steps
    : planned.map((key) => ({ key, state: "pending", progress: 0, detail: "" }));

  box.textContent = "";
  for (const step of steps) {
    const named = SETUP_NAMES[step.key];
    const stated = SETUP_STATES[step.state];
    // Work whose length is not knowable sweeps rather than sitting at zero,
    // because a bar that has not moved in four minutes reads as a hang.
    const waiting = step.progress < 0;
    const row = document.createElement("div");
    row.className = `step is-${step.state}${waiting ? " is-waiting" : ""}`;

    const top = document.createElement("div");
    top.className = "top";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = named ? t(named[0], named[1]) : step.key;
    const status = document.createElement("span");
    status.className = "state";
    status.textContent = stated ? t(stated[0], stated[1]) : step.state;
    top.append(name, status);

    const bar = document.createElement("div");
    bar.className = "bar";
    const fill = document.createElement("i");
    if (!waiting) {
      const pct = Math.max(0, Math.min(1, step.progress)) * 100;
      fill.style.width = `${Math.round(pct)}%`;
    }
    bar.append(fill);

    const detail = document.createElement("div");
    detail.className = "detail";
    detail.textContent = step.error || step.detail || "";

    row.append(top, bar, detail);
    box.append(row);
  }

  const failed = steps.filter((step) => step.state === "failed");
  const note = $("setup-note");
  if (note) {
    if (failed.length) {
      note.hidden = false;
      note.className = "setup-note is-bad";
      note.textContent = t("setup.retry",
        "Something did not download. Check the connection and try again: what already arrived is kept.");
    } else if (state.running) {
      note.hidden = false;
      note.className = "setup-note";
      note.textContent = t("setup.leave_open",
        "You can close this and carry on. The download keeps going.");
    } else {
      note.hidden = true;
    }
  }

  // The cloned voices are offered only by a build that can clone, and not
  // again once the controllers already are clones.
  hide($("setup-hq-label"), !state.hq_available || state.hq_engine);
  // Nor an extra that is already here: ticking it fetched nothing and looked
  // like a box that did nothing.
  const extras = state.extras || {};
  hide($("setup-all-voices") && $("setup-all-voices").closest("label"),
       extras.all_voices);
  hide($("setup-vatglasses") && $("setup-vatglasses").closest("label"),
       extras.vatglasses);

  const finished = !state.running && state.steps.length > 0;
  hide($("setup-start"), state.running || (finished && failed.length === 0));
  hide($("setup-cancel"), !state.running);
  hide($("setup-later"), state.running || finished);
  hide($("setup-done"), state.running || !finished);
  const start = $("setup-start");
  if (start) {
    start.textContent = failed.length ? t("setup.again", "Try again")
                                      : t("setup.start", "Download");
  }
}

function hide(node, hidden) {
  if (node) node.hidden = Boolean(hidden);
}

async function loadSetup(openIfMissing) {
  const state = await get("/api/setup");
  if (!state) return null;
  app.setup = state;
  paintComponents();
  paintSetup();
  if (openIfMissing && state.missing.length) openSetup();
  return state;
}

/**
 * Put the setup dialog up, waiting for the way to be clear.
 *
 * Only one dialog can be modal at a time, and on a first run this one follows
 * the startup notice. Asked for while that is still open -- or still closing
 * -- the browser refuses, and the prompt silently never appears on the one
 * launch where it is the whole point. So it waits, briefly and a fixed number
 * of times, rather than trying once and giving up.
 */
function openSetup(attempt) {
  const dialog = $("setup-dialog");
  if (!dialog || !dialog.showModal || dialog.open) return;
  const tries = attempt || 0;
  const blocked = [...document.querySelectorAll("dialog")]
    .some((other) => other !== dialog && other.open);
  if (!blocked) {
    paintSetup();
    try {
      dialog.showModal();
      return;
    } catch (error) {
      // fall through to the retry
    }
  }
  if (tries < 20) setTimeout(() => openSetup(tries + 1), 150);
}

/** The diagnostics report, which is mostly read in order to be pasted. */
async function loadDoctor() {
  const box = $("doctor-checks");
  if (box) {
    box.textContent = "";
    box.append(markRow("check is-warn", t("doctor.checking", "Checking…")));
  }
  app.doctor = await get("/api/doctor");
  paintDoctor();
}

function paintDoctor() {
  const box = $("doctor-checks");
  const report = app.doctor;
  if (!box) return;
  box.textContent = "";
  if (!report) {
    box.append(markRow("check is-error",
                       t("doctor.failed", "Could not run the checks")));
    return;
  }
  for (const check of report.checks) {
    box.append(markRow(`check is-${check.status}`, check.name, check.detail));
  }
}

/** The report as text, which is the form it goes into a bug report in. */
function doctorText() {
  const report = app.doctor;
  if (!report) return "";
  return report.checks
    .map((check) => `${check.status.toUpperCase().padEnd(5)} ${check.name}: ${check.detail}`)
    .join("\n");
}

function wireComponents() {
  on($("setup-open"), "click", () => { loadSetup(); openSetup(); });
  on($("components-alert-fix"), "click", () => { loadSetup(); openSetup(); });
  on($("setup-later"), "click", () => $("setup-dialog").close());
  on($("setup-done"), "click", () => {
    const state = app.setup;
    $("setup-dialog").close();
    loadSetup();
    // The voices are picked up by the running engine, so that download needs
    // no restart. The navigation database and the recogniser are opened once
    // at startup and still do, so say so only when one of those was fetched.
    const restarts = (state ? state.steps : [])
      .filter((step) => step.state === "done" && step.key !== "voices");
    toast(restarts.length
      ? t("setup.restart", "Downloaded. Restart WilcoATC to use it.")
      : t("setup.ready", "Downloaded. The controllers have their voices."));
  });
  on($("setup-cancel"), "click", async () => {
    const result = await post("/api/setup/cancel");
    if (result && result.setup) { app.setup = result.setup; paintSetup(); }
  });
  on($("setup-start"), "click", async () => {
    // The extras are the one thing on this screen that is a choice rather
    // than a necessity, so they are asked for by name -- the same request
    // the first-run setup makes.
    const ticked = (id) => Boolean($(id) && $(id).checked);
    const body = setupRequest({ all_voices: ticked("setup-all-voices"),
                                hq: ticked("setup-hq"),
                                vatglasses: ticked("setup-vatglasses") });
    const result = await post("/api/setup", body);
    if (result && result.setup) {
      app.setup = result.setup;
      paintSetup();
      paintComponents();
    }
    if (result && result.started === false) {
      toast(t("setup.already", "already running"));
    }
  });

  on($("doctor-open"), "click", () => {
    const dialog = $("doctor-dialog");
    if (!dialog || !dialog.showModal) return;
    if (!dialog.open) dialog.showModal();
    loadDoctor();
  });
  on($("doctor-again"), "click", () => loadDoctor());
  on($("doctor-close"), "click", () => $("doctor-dialog").close());
  on($("doctor-copy"), "click", async () => {
    const body = doctorText();
    if (!body) return;
    try {
      await navigator.clipboard.writeText(body);
      toast(t("doctor.copied", "copied"));
    } catch (error) {
      // Clipboard access is refused on an insecure origin in some browsers,
      // and this page is plain http over loopback. Selecting the text is the
      // fallback that always works.
      const box = $("doctor-checks");
      if (box) {
        const range = document.createRange();
        range.selectNodeContents(box);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
      }
      toast(t("doctor.select", "select it and copy"));
    }
  });
}

// ------------------------------------------------------- landing report ---

/*
 * The card a few seconds after touchdown.
 *
 * The engine measures the landing and writes it up once a bounce has had time
 * to show itself; this only draws what it sent. Each touchdown comes up once,
 * and only while it is fresh -- the engine says which -- so a window opened
 * an hour after the flight does not greet anybody with how it landed. It goes
 * away by itself after a while because the pilot is busy, and a card that
 * had to be dismissed would be one more thing to do on the rollout.
 */

const LANDING_SHOWN_MS = 90000;

function landingGrade(grade) {
  switch (grade) {
    case "butter": return t("landing.grade_butter", "Butter");
    case "smooth": return t("landing.grade_smooth", "Smooth");
    case "normal": return t("landing.grade_normal", "Normal");
    case "firm": return t("landing.grade_firm", "Firm");
    default: return t("landing.grade_hard", "Hard");
  }
}

function landingPunctuality(word) {
  switch (word) {
    case "early": return t("landing.early", "Early");
    case "on_time": return t("landing.on_time", "On time");
    default: return t("landing.late", "Late");
  }
}

// Signed minutes with a real minus, because "-3" in a proportional font is
// a hyphen and reads as a range.
function signedMinutes(value) {
  const sign = value > 0 ? "+" : value < 0 ? "−" : "±";
  return `${sign}${Math.abs(value)} min`;
}

function duration(seconds) {
  const minutes = Math.round((seconds || 0) / 60);
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return h ? `${h} h ${String(m).padStart(2, "0")} min` : `${m} min`;
}

function paintLanding(report) {
  const card = $("landing-card");
  if (!card) return;
  if (!report) {
    // Switched off in the settings while it was up.
    if (!card.hidden) hideLanding();
    return;
  }
  const fresh = report.touchdown_at !== app.landingAt;
  if (fresh && !report.fresh) return;
  if (!fresh && (card.hidden || app.landingLang === I18N.language)) return;

  app.landingAt = report.touchdown_at;
  app.landingLang = I18N.language;
  drawLanding(card, report);
  if (fresh) {
    card.hidden = false;
    requestAnimationFrame(() => card.classList.add("is-shown"));
    clearTimeout(app.timers.landing);
    app.timers.landing = setTimeout(hideLanding, LANDING_SHOWN_MS);
  }
}

function hideLanding() {
  const card = $("landing-card");
  clearTimeout(app.timers.landing);
  card.classList.remove("is-shown");
  setTimeout(() => { card.hidden = true; }, 260);
}

function drawLanding(card, report) {
  const el = (tag, className, content) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  };
  const rows = (pairs) => {
    const list = el("dl");
    for (const [label, value] of pairs) {
      list.append(el("dt", "", label), el("dd", "", value));
    }
    return list;
  };

  const head = el("div", "lr-head");
  const title = el("span", "", t("landing.title", "Landing report")
                   + (report.airport ? ` · ${report.airport}` : ""));
  const close = el("button", "lr-close", "×");
  close.type = "button";
  close.setAttribute("aria-label", t("landing.close", "Close"));
  close.addEventListener("click", hideLanding);
  head.append(title, close);

  // The figure, which is what everybody opens this for.
  const hero = el("div", "lr-hero");
  const figure = el("div");
  const rate = el("div", "lr-rate",
                  report.fpm < 0 ? `−${Math.abs(report.fpm)}` : `${report.fpm}`);
  rate.append(el("small", "", "fpm"));
  figure.append(rate, el("div", "lr-grade", landingGrade(report.grade)));
  const score = el("div", "lr-score", t("landing.score", "Score"));
  score.append(el("b", "", `${report.score}/100`));
  hero.append(figure, score);

  const meter = el("div", "lr-meter");
  const bar = el("i");
  bar.style.width = `${Math.max(0, Math.min(100, report.score))}%`;
  meter.append(bar);

  const wind = [];
  if (report.headwind_kt > 0) {
    wind.push(`${report.headwind_kt} kt ${t("landing.headwind", "headwind")}`);
  } else if (report.headwind_kt < 0) {
    wind.push(`${-report.headwind_kt} kt ${t("landing.tailwind", "tailwind")}`);
  }
  if (report.crosswind_kt > 0) {
    wind.push(`${report.crosswind_kt} kt ${t("landing.crosswind", "crosswind")}`);
  }
  const details = [
    [t("landing.bounces", "Bounces"), String(report.bounces)],
    [t("landing.speed", "Touchdown speed"), `${report.ias_kt} kt`],
  ];
  if (wind.length) details.push([t("landing.wind", "Wind"), wind.join(" · ")]);
  if (report.stable !== null && report.stable !== undefined) {
    details.push([t("landing.approach", "Approach"),
                  `${report.stable ? t("landing.stable", "Stable")
                                   : t("landing.unsteady", "Unsteady")}`
                  + ` (±${report.vs_spread} fpm)`]);
  }

  card.replaceChildren(head, hero, meter, rows(details));

  // The flight around it, against the plan when there is one.
  const schedule = [];
  if (report.departure) {
    schedule.push([t("landing.departure", "Departure"),
                   `${zulu(report.off_block_at)} · `
                   + `${signedMinutes(report.departure_delay_min)} · `
                   + landingPunctuality(report.departure)]);
  }
  if (report.arrival) {
    schedule.push([t("landing.arrival", "Arrival"),
                   `${zulu(report.touchdown_at)} · `
                   + `${signedMinutes(report.arrival_delay_min)} · `
                   + landingPunctuality(report.arrival)]);
  }
  if (report.air_s > 0) {
    let flown = duration(report.air_s);
    if (report.planned_air_s > 0) {
      flown += ` (${fill(t("landing.planned", "planned {time}"),
                          { time: duration(report.planned_air_s) })})`;
    }
    schedule.push([t("landing.flight_time", "Flight time"), flown]);
  }
  if (schedule.length) {
    card.append(el("h3", "", t("landing.schedule", "Schedule")), rows(schedule));
  }
  if (!report.sched_on) {
    card.append(el("p", "lr-note",
                   t("landing.no_plan",
                     "Load a SimBrief plan to compare this flight against its schedule.")));
  }
}

// -------------------------------------------------------------- update ---

/*
 * A newer version on GitHub (wilcoatc/update.py). The engine asks once at
 * startup; the answer arrives on the socket or from /api/update, and the
 * dialog is put up once per launch, after the notice and the first-run
 * screens, never on top of another dialog or the tour. Nothing is downloaded
 * until the pilot presses Update.
 */
// Why the program cannot replace itself, by the key the server sends.
const UPDATE_WHY = {
  "update.why_checkout": () => t("update.why_checkout", "This copy runs from the source code: update it with git, or download the new version."),
  "update.why_readonly": () => t("update.why_readonly", "WilcoATC cannot write to its own folder, so it cannot update itself. Download the new version and unpack it over this one."),
  "update.why_no_asset": () => t("update.why_no_asset", "This release has no download for this edition yet."),
};

function megabytes(bytes) {
  return `${Math.round((bytes || 0) / 1048576)} MB`;
}

function paintUpdate() {
  const state = app.update;
  if (!state) return;
  const release = state.release || {};

  // The line in Settings, Interface.
  let status = "";
  if (state.checking) status = t("update.checking", "Checking…");
  else if (state.error) status = t("update.check_failed", "Could not reach GitHub.");
  else if (state.checked && state.available)
    status = fill(t("update.status_new", "Version {latest} is available (this is {current})."),
                  { latest: release.version, current: state.current });
  else if (state.checked)
    status = fill(t("update.status_current", "Up to date (version {current})."),
                  { current: state.current });
  text($("update-status"), status);

  if (!$("update-dialog")) return;
  text($("update-versions"),
       fill(t("update.versions", "WilcoATC {latest} is available. You have {current}."),
            { latest: release.version || "", current: state.current }));
  const notes = $("update-notes");
  notes.hidden = !release.notes;
  text(notes, release.notes || "");

  const phase = state.phase;
  const working = phase === "downloading" || phase === "applying";
  $("update-progress").hidden = !(working || phase === "ready" || phase === "failed");
  const waiting = phase === "downloading" && state.progress < 0;
  $("update-step").className = `step is-${phase === "failed" ? "failed" : phase === "ready" ? "done" : "running"}${waiting ? " is-waiting" : ""}`;
  $("update-fill").style.width = waiting ? "" :
    `${Math.round(Math.max(0, Math.min(1, state.progress)) * 100)}%`;
  text($("update-amount"), state.total ? `${megabytes(state.received)} / ${megabytes(state.total)}` : "");
  text($("update-detail"), phase === "failed" ? (state.error || "") :
       waiting ? t("update.unpacking", "Unpacking…") : (release.asset || ""));

  const note = $("update-note");
  let said = "";
  let bad = false;
  if (!state.can_install && state.why_not) said = UPDATE_WHY[state.why_not] ? UPDATE_WHY[state.why_not]() : "";
  if (phase === "ready") said = t("update.ready", "Ready. WilcoATC will close and start again on the new version; your settings and voices are kept.");
  if (phase === "applying") said = t("update.restarting", "Restarting…");
  if (phase === "failed") {
    said = t("update.failed", "The update could not be downloaded. Nothing was changed; try again later.");
    bad = true;
  }
  note.hidden = !said;
  note.className = `setup-note${bad ? " is-bad" : ""}`;
  text(note, said);

  $("update-start").hidden = !state.can_install || working || phase === "ready";
  $("update-start").textContent = phase === "failed" ? t("update.retry", "Try again") : t("update.start", "Update");
  $("update-page").hidden = state.can_install;
  $("update-restart").hidden = phase !== "ready";
  $("update-cancel").hidden = phase !== "downloading";
  $("update-skip").hidden = working || phase === "ready";
  $("update-later").hidden = phase === "applying";
}

/** Put the dialog up if there is something to offer and the way is clear. */
function maybeOfferUpdate(force) {
  const state = app.update;
  const dialog = $("update-dialog");
  if (!state || !state.available || !dialog || dialog.open) return;
  const version = state.release && state.release.version;
  if (!force && (app.updateOffered || !app.noticeDone || state.skipped === version)) return;
  const busy = [...document.querySelectorAll("dialog")].some((d) => d.open) || app.tour;
  if (busy) {
    setTimeout(() => maybeOfferUpdate(force), 1000);
    return;
  }
  app.updateOffered = true;
  paintUpdate();
  try { dialog.showModal(); } catch (error) { /* another dialog won the race */ }
}

async function checkForUpdate() {
  text($("update-status"), t("update.checking", "Checking…"));
  const state = await post("/api/update/check");
  if (!state || state.ok === false) return;
  app.update = state;
  paintUpdate();
  if (state.available) maybeOfferUpdate(true);
}

function wireUpdate() {
  const dialog = $("update-dialog");
  on($("update-later"), "click", () => dialog.close());
  on($("update-skip"), "click", async () => {
    const version = app.update && app.update.release && app.update.release.version;
    const state = await post("/api/update/skip", { version });
    if (state && state.ok !== false) app.update = state;
    dialog.close();
  });
  on($("update-start"), "click", async () => {
    const result = await post("/api/update/download");
    if (result && result.update) { app.update = result.update; paintUpdate(); }
  });
  on($("update-cancel"), "click", () => post("/api/update/cancel"));
  on($("update-restart"), "click", async () => {
    const result = await post("/api/update/apply");
    if (result && result.update) { app.update = result.update; paintUpdate(); }
  });
  on($("update-page"), "click", () => {
    const release = app.update && app.update.release;
    window.open((release && release.page) || "https://github.com/GGTNT/WilcoATC/releases/latest", "_blank");
  });
  on($("update-check-now"), "click", checkForUpdate);
  on($("set-check-updates"), "click", (e) => {
    const next = e.currentTarget.getAttribute("aria-pressed") !== "true";
    save({ "ui.check_updates": next }, "saved-interface");
  });
  get("/api/update").then((state) => {
    if (!state) return;
    app.update = state;
    paintUpdate();
    maybeOfferUpdate();
  });
}

// -------------------------------------------------------- startup notice ---

/**
 * The notice that goes up before anything else.
 *
 * It is shown on every launch rather than remembered, because what it is
 * warning about keeps changing: this is alpha, and an acknowledgement given a
 * fortnight ago was for different software.
 *
 * Continue stays disabled until the box is ticked, and Escape does not
 * dismiss it, so the acknowledgement has to be given rather than skipped.
 */
function showNotice() {
  const dialog = $("notice");
  const agree = $("notice-agree");
  const proceed = $("notice-continue");
  if (!dialog || !dialog.showModal) return;

  agree.checked = false;
  proceed.disabled = true;
  on(agree, "change", () => { proceed.disabled = !agree.checked; });
  // A dialog closes on Escape by default, which would let the notice be
  // waved away without answering it.
  on(dialog, "cancel", (event) => event.preventDefault());
  // One modal at a time. The notice has to be answered first, and the setup
  // dialog -- if anything is missing -- comes up behind it rather than on top
  // of it, which a browser would refuse anyway.
  on(dialog, "close", () => {
    $("say").focus({ preventScroll: true });
    // The first-run setup until it has been finished -- the downloads are
    // its second screen -- and otherwise the download prompt if anything is
    // missing. Then the tour on a first run, which waits for either to close.
    maybeOnboard().then((shown) => {
      if (!shown) loadSetup(true);
      setTimeout(maybeStartTour, 300);
      // A newer version waits for all of that: it is offered last.
      app.noticeDone = true;
      setTimeout(maybeOfferUpdate, 1500);
    });
  });

  dialog.showModal();
  agree.focus({ preventScroll: true });
}

// ------------------------------------------------------ first-run setup ---

/*
 * The six screens a pilot is walked through on the first launch.
 *
 * Every choice on them is an ordinary setting, written through the same
 * /api/settings path as the Settings screen, so there is nothing here the
 * pilot cannot change again afterwards and nothing that has to be kept in
 * step with it. The downloads are the installer's, watched over the same
 * socket as the download dialog. On a machine with nothing downloaded the
 * page is served by the setup-only server, which has no engine: it writes
 * the same settings to the file for the engine to start on.
 */

// The revision of these screens. Raised when one is added or changes what it
// asks, so that a pilot who finished an older set is walked through this one.
// 2: Appearance and First officer (2026-10-29).
const OB_VERSION = 2;

const OB_STEPS = [
  ["ob.step_welcome", "Welcome"],
  ["ob.step_look", "Appearance"],
  ["ob.step_downloads", "Downloads"],
  ["ob.step_microphone", "Microphone"],
  ["ob.step_ptt", "Push to talk"],
  ["ob.step_fo", "First officer"],
  ["ob.step_plan", "Flight plan"],
  ["ob.step_ready", "Ready"],
];

// Screens other code has to find, by name rather than by number, so a screen
// added before one does not send the pilot to the wrong place.
const OB_DOWNLOADS = OB_STEPS.findIndex(([key]) => key === "ob.step_downloads") + 1;
const OB_FIRST_OFFICER = OB_STEPS.findIndex(([key]) => key === "ob.step_fo") + 1;

/* What each required download is, under its name. */
const OB_PARTS = {
  navdata: ["ob.navdata_why", "Every airport, runway and frequency in the world"],
  voices: ["ob.voices_why", "The voices your controllers speak with"],
  speech: ["ob.speech_why", "The model that hears what you say"],
};

/* The keys a name cannot be shown as typed. */
const OB_KEY_SHOWN = {
  grave: "`", space: "Space", backslash: "\\", caps_lock: "Caps",
  ctrl_l: "Ctrl", ctrl_r: "Ctrl R", alt_l: "Alt", alt_r: "Alt Gr",
  shift_l: "Shift", shift_r: "Shift R", tab: "Tab", enter: "Enter",
};

const OB_BARS = 32;

/*
 * The flag beside each language of the window, as the mock-up has them.
 * Bundled rather than fetched (vendor/flags, from flag-icons, MIT), because
 * the window has to work with no network. A language is not a country, so
 * this is the flag a reader of it is most likely to look for: the United
 * Kingdom for English, Catalonia and Galicia for theirs, Taiwan for
 * traditional Chinese.
 */
const OB_FLAGS = {
  en: "gb", ar: "sa", bg: "bg", ca: "es-ct", cs: "cz", da: "dk", de: "de",
  el: "gr", es: "es", et: "ee", fa: "ir", fi: "fi", fr: "fr", gl: "es-ga",
  he: "il", hi: "in", hr: "hr", hu: "hu", id: "id", it: "it", ja: "jp",
  ko: "kr", lt: "lt", lv: "lv", ms: "my", nb: "no", nl: "nl", pl: "pl",
  pt: "pt", ro: "ro", ru: "ru", sk: "sk", sl: "si", sr: "rs", sv: "se",
  th: "th", tr: "tr", uk: "ua", vi: "vn", "zh-Hans": "cn", "zh-Hant": "tw",
};

function obFlag(code) {
  const img = obEl("img", "flag");
  img.src = `/vendor/flags/${OB_FLAGS[code] || "gb"}.svg`;
  img.alt = "";
  return img;
}

const OB_ICONS = {
  mic: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>',
  headset: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 14v-4a9 9 0 0 1 18 0v4"/><rect x="3" y="13" width="4" height="7" rx="1.5"/><rect x="17" y="13" width="4" height="7" rx="1.5"/></svg>',
  chevron: '<svg class="chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M6 9l6 6 6-6"/></svg>',
};

function obEl(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}

/** Whether to put the setup up by itself: once, until it is finished. */
/**
 * Whether the first-run setup comes up, and where.
 *
 * Not finished, or finished in an older revision: from the start. Finished
 * and current, but something is still not downloaded -- the pilot said "not
 * now", or a download failed -- the same screens open on Downloads, rather
 * than the bare download dialog a pilot coming from the setup has never seen.
 */
async function maybeOnboard() {
  if (!app.settings) await loadSettings();
  if (!app.settings) return false;
  const ui = app.settings.ui;
  if (!ui.onboarding_done || (ui.onboarding_version || 0) < OB_VERSION) {
    openOnboarding();
    return true;
  }
  const setup = app.setup || await loadSetup();
  if (setup && setup.missing && setup.missing.length) {
    openOnboarding(OB_DOWNLOADS);
    return true;
  }
  return false;
}


function openOnboarding(startAt) {
  const dialog = $("onboard");
  if (!dialog || !dialog.showModal || dialog.open) return;
  const first = startAt || 1;
  app.onboard = {
    step: first, reached: first, open: "", binding: false, learning: false,
    level: 0, levelOk: true, quiet: 0, error: "", restart: new Set(),
    extras: { all_voices: false, vatglasses: false, hq: false },
    importing: false, plan: null, planError: "",
  };
  const logo = document.querySelector(".brand .mark");
  if (logo && !$("ob-logo").firstChild) $("ob-logo").append(logo.cloneNode(true));
  if (!app.setup) loadSetup();
  // The devices may have been read before a headset was plugged in.
  loadSettings().then(paintOnboarding);
  paintOnboarding();
  dialog.showModal();
}

function obGo(step) {
  const ob = app.onboard;
  if (!ob) return;
  ob.step = Math.max(1, Math.min(OB_STEPS.length, step));
  ob.reached = Math.max(ob.reached, ob.step);
  ob.open = "";
  ob.binding = false;
  // On a first run the voices are downloading behind the screens before
  // this one: ask again for what is on disk when the picker comes up.
  if (ob.step === OB_FIRST_OFFICER) loadSettings().then(paintOnboarding);
  paintOnboarding();
  $("onboard").scrollTop = 0;
}

/** A setting written from the setup, keeping what needs a restart. */
async function obSave(changes) {
  const result = await post("/api/settings", changes);
  const ob = app.onboard;
  if (!result || !result.ok) {
    toast((result && result.error) || "could not save");
    return false;
  }
  app.settings = result.config;
  if (ob) for (const path of result.restart_required || []) ob.restart.add(path);
  paintSettings();
  return true;
}

async function finishOnboarding() {
  await obSave({ "ui.onboarding_done": true, "ui.onboarding_version": OB_VERSION });
  app.onboard = null;
  $("onboard").close();
  setTimeout(maybeStartTour, 300);
}

function paintOnboarding() {
  const ob = app.onboard;
  if (!ob || !$("onboard")) return;
  const config = app.settings;

  // The steps down the side. Any screen already seen can be gone back to.
  const nav = $("ob-steps");
  nav.textContent = "";
  OB_STEPS.forEach((pair, index) => {
    const n = index + 1;
    const button = obEl("button");
    const done = n < ob.reached && n !== ob.step;
    if (n <= ob.reached) button.classList.add("is-reached");
    if (done) button.classList.add("is-done");
    if (n === ob.step) button.setAttribute("aria-current", "step");
    button.append(obEl("span", "dot", done ? "✓" : String(n)),
                  obEl("span", "", t(pair[0], pair[1])));
    button.addEventListener("click", () => { if (n <= ob.reached) obGo(n); });
    nav.append(button);
  });
  text($("ob-version"), app.version ? `v${app.version}` : "");

  for (const page of document.querySelectorAll(".ob-page")) {
    page.hidden = Number(page.dataset.page) !== ob.step;
  }

  // The language of the window, top right.
  const here = I18N.LANGUAGES.find(([code]) => code === I18N.language)
    || I18N.LANGUAGES[0];
  text($("ob-lang-code"), here[0].split("-")[0].toUpperCase());
  $("ob-lang").title = here[1];
  const flag = `/vendor/flags/${OB_FLAGS[here[0]] || "gb"}.svg`;
  if ($("ob-lang-flag").getAttribute("src") !== flag) $("ob-lang-flag").src = flag;
  $("ob-lang").setAttribute("aria-expanded", ob.open === "lang" ? "true" : "false");
  const menu = $("ob-lang-menu");
  menu.hidden = ob.open !== "lang";
  if (!menu.hidden && !menu.childElementCount) {
    for (const [code, name] of I18N.LANGUAGES) {
      const item = obEl("button");
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", code === I18N.language ? "true" : "false");
      item.append(obFlag(code), obEl("span", "ob-item", name),
                  obEl("span", "check", code === I18N.language ? "✓" : ""));
      item.addEventListener("click", async () => {
        ob.open = "";
        menu.textContent = "";
        await I18N.apply(code);
        obSave({ "ui.language": code });
      });
      menu.append(item);
    }
  } else if (menu.hidden) {
    menu.textContent = "";
  }

  if (ob.step === 2) paintLook(LOOK.current);
  if (ob.step === OB_DOWNLOADS) paintObDownloads();
  if (ob.step === 4 && config) paintObDevices();
  if (ob.step === 5 && config) paintObPtt();
  if (ob.step === 6 && config) paintObFirstOfficer();
  if (ob.step === 7 && config) paintObPlan();
  if (ob.step === 8 && config) paintObSummary();

  const back = $("ob-back");
  back.classList.toggle("is-hidden", ob.step === 1);
  const last = ob.step === OB_STEPS.length;
  text($("ob-next-label"), ob.step === 1 ? t("ob.start", "Get started")
    : last ? t("ob.open", "Open WilcoATC") : t("ob.continue", "Continue"));
}

/** What is being downloaded, and the extras that are a choice. */
function paintObDownloads() {
  const ob = app.onboard;
  const state = app.setup;
  const box = $("ob-parts");
  if (!state) return;
  box.textContent = "";
  const running = Boolean(state.running);
  const steps = Object.fromEntries((state.steps || []).map((s) => [s.key, s]));

  for (const key of Object.keys(SETUP_NAMES)) {
    const here = Boolean(state.installed[key]);
    const step = steps[key];
    const live = step && (step.state === "running" || step.state === "pending")
      && running;
    const failed = step && step.state === "failed";
    const row = obEl("button", "ob-part");
    row.disabled = true;
    row.classList.add(here ? "is-here" : "is-on");
    if (live) row.classList.add("is-running");
    if (failed) row.classList.add("is-failed");
    const named = SETUP_NAMES[key];
    const why = OB_PARTS[key];
    const textBox = obEl("span", "text");
    textBox.append(obEl("span", "name", t(named[0], named[1])),
                   obEl("span", "why", `${t(why[0], why[1])} · ${state.sizes_mb[key]} MB`));
    let status;
    if (here && !(live && step.state === "running")) status = t("ob.installed", "Installed");
    else if (failed) status = t("setup.failed", "failed");
    else if (live && step.state === "running") {
      status = step.progress < 0 ? t("ob.downloading", "Downloading…")
        : `${Math.floor(step.progress * 100)}%`;
    } else if (live) status = t("setup.pending", "waiting");
    else status = `${state.sizes_mb[key]} MB`;
    row.append(obEl("span", "box", "✓"), textBox, obEl("span", "state", status));
    if (live && step.state === "running") {
      const bar = obEl("i", "progress");
      if (step.progress < 0) bar.classList.add("is-waiting");
      else bar.style.width = `${Math.round(step.progress * 100)}%`;
      row.append(bar);
    }
    box.append(row);
  }

  // The optional ones, with the words the download dialog already has.
  const extras = [
    ["all_voices", "setup.extra_voices", "Add every country's accent",
     "setup.extra_voices_why", "2.3 GB more. Controllers abroad speak English with their own country's accent; without it they use a neutral British voice."],
    ["vatglasses", "setup.vatglasses", "Add the en-route sectors (VatGlasses)",
     "setup.vatglasses_licence", "Made by the VatGlasses contributors and shared under CC BY-NC-SA 4.0: for non-commercial use only. Downloaded on its own, never part of WilcoATC. Without it, centre calls use the published boundaries."],
  ];
  if (state.hq_available && !state.hq_engine) {
    extras.push(["hq", "setup.hq_voices", "Use cloned controller voices",
                 "setup.hq_voices_why", "2 GB more. The most natural voices, but slow without a graphics card: a long clearance can take a minute to arrive."]);
  }
  // Each extra rides on the step it belongs to, so while that step runs the
  // ticked extra shows its bar.
  const carrier = { all_voices: "voices", hq: "voices", vatglasses: "navdata" };
  const present = state.extras || {};
  for (const [key, nameKey, name, whyKey, why] of extras) {
    // The cloned voices downloaded and not in use are still a choice:
    // asking for them is what switches the controllers over.
    const here = Boolean(present[key]) && key !== "hq";
    const on = !here && ob.extras[key];
    const step = steps[carrier[key]];
    const live = on && running && step && step.state === "running";
    const row = obEl("button", `ob-part is-optional${here ? " is-here" : ""}${on ? " is-on" : ""}${live ? " is-running" : ""}`);
    row.disabled = running || here;
    row.setAttribute("aria-pressed", on ? "true" : "false");
    const textBox = obEl("span", "text");
    textBox.append(obEl("span", "name", t(nameKey, name)),
                   obEl("span", "why", t(whyKey, why)));
    let status = t("ob.optional", "Optional");
    if (here || (key === "hq" && present.hq)) status = t("ob.installed", "Installed");
    if (on && running) status = live ? t("ob.downloading", "Downloading…") : t("setup.pending", "waiting");
    row.append(obEl("span", "box", here || on ? "✓" : ""), textBox,
               obEl("span", "state", status));
    if (live) {
      const bar = obEl("i", "progress");
      if (step.progress < 0) bar.classList.add("is-waiting");
      else bar.style.width = `${Math.round(step.progress * 100)}%`;
      row.append(bar);
    }
    row.addEventListener("click", () => {
      ob.extras[key] = !ob.extras[key];
      paintObDownloads();
    });
    box.append(row);
  }

  // The profile for the cloned voices, once they are asked for or in use:
  // chosen before the download, because it decides which model is fetched.
  paintPerf("ob-perf", Boolean(state.hq_available
                               && (ob.extras.hq || state.hq_engine)), "ob");

  const failed = (state.steps || []).some((s) => s.state === "failed");
  const wanted = obWanted();
  const megabytes = state.missing.reduce(
    (total, key) => total + (state.sizes_mb[key] || 0), 0);
  const button = $("ob-download");
  let label;
  if (running) label = t("ob.downloading", "Downloading…");
  else if (failed) label = t("setup.again", "Try again");
  else if (!wanted) label = t("ob.up_to_date", "Up to date");
  else if (megabytes + obExtrasMb()) {
    label = fill(t("ob.download_mb", "Download · {mb} MB"),
                 { mb: megabytes + obExtrasMb() });
  } else label = t("ob.apply", "Apply");
  text($("ob-download-label"), label);
  button.disabled = running || (!wanted && !failed);

  const note = $("ob-dl-note");
  note.classList.toggle("is-bad", failed);
  text(note, failed
    ? t("setup.retry", "Something did not download. Check the connection and try again: what already arrived is kept.")
    : running ? t("ob.note_busy", "You can carry on: the downloads keep going in the background.")
    : t("ob.note", "Anything here can be fetched later from Settings, Components."));
}

/** Whether pressing Download would fetch anything. */
function obWanted() {
  const state = app.setup;
  const ob = app.onboard;
  if (!state || !ob) return false;
  return state.missing.length > 0
    || Object.keys(ob.extras).some((key) => obExtraWanted(key));
}

/** A ticked extra that is not already here. */
function obExtraWanted(key) {
  const ob = app.onboard;
  const present = (app.setup && app.setup.extras) || {};
  if (!ob || !ob.extras[key]) return false;
  // The cloned voices on disk are still worth asking for while the
  // controllers are not using them.
  return key === "hq" ? !app.setup.hq_engine : !present[key];
}

/** What the ticked extras still have to fetch. */
function obExtrasMb() {
  const present = (app.setup && app.setup.extras) || {};
  const sizes = (app.setup && app.setup.extras_mb) || {};
  return Object.keys(sizes)
    .filter((key) => obExtraWanted(key) && !present[key])
    .reduce((total, key) => total + sizes[key], 0);
}

/**
 * The installer's request, built as the download dialog builds it: the
 * extras that ride on a step ask for that step even when it is installed.
 */
function setupRequest(extras) {
  const body = {};
  const missing = (app.setup && app.setup.missing) || [];
  if (extras.all_voices) {
    body.all_voices = true;
    body.steps = [...new Set([...missing, "voices"])];
  }
  if (extras.hq && app.setup && app.setup.hq_available) {
    body.hq = true;
    body.steps = [...new Set([...(body.steps || missing), "voices"])];
  }
  if (extras.vatglasses) {
    body.vatglasses = true;
    body.steps = [...new Set([...(body.steps || missing), "navdata"])];
  }
  return body;
}

async function obDownload() {
  const ob = app.onboard;
  if (!ob || !obWanted() && !(app.setup.steps || []).some((s) => s.state === "failed")) return;
  const wanted = Object.fromEntries(
    Object.keys(ob.extras).map((key) => [key, obExtraWanted(key)]));
  const result = await post("/api/setup", setupRequest(wanted));
  if (result && result.setup) {
    app.setup = result.setup;
    paintSetup();
    paintComponents();
    paintOnboarding();
  }
}

/** One device picker: a button and the list it opens. */
function obPicker(id, icon, devices, current, path) {
  const ob = app.onboard;
  const box = $(id);
  const open = ob.open === id;
  box.classList.toggle("is-open", open);
  box.parentElement.classList.toggle("is-open", open);
  box.textContent = "";
  const options = [{ value: "", name: t("set.system_default", "System default"), sub: "Windows" },
    ...(devices || []).map((d) => ({ value: String(d.index), name: d.name, sub: `#${d.index}` }))];
  const chosen = current === null || current === undefined ? "" : String(current);
  const picked = options.find((o) => o.value === chosen)
    || { value: chosen, name: chosen, sub: "" };

  const button = obEl("button");
  button.setAttribute("aria-haspopup", "listbox");
  button.setAttribute("aria-expanded", open ? "true" : "false");
  const glyph = obEl("span", "icon");
  glyph.innerHTML = OB_ICONS[icon];
  button.append(glyph, obEl("span", "value", picked.name));
  button.insertAdjacentHTML("beforeend", OB_ICONS.chevron);
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    ob.open = open ? "" : id;
    paintOnboarding();
  });
  box.append(button);
  if (!open) return;

  const menu = obEl("div", "ob-menu");
  menu.setAttribute("role", "listbox");
  for (const option of options) {
    const item = obEl("button");
    item.setAttribute("role", "option");
    const selected = option.value === picked.value;
    item.setAttribute("aria-selected", selected ? "true" : "false");
    const label = obEl("span", "ob-item");
    label.append(obEl("div", "", option.name));
    const sub = obEl("div", "ob-note", option.sub);
    label.append(sub);
    item.append(label, obEl("span", "check", selected ? "✓" : ""));
    item.addEventListener("click", async (event) => {
      event.stopPropagation();
      ob.open = "";
      paintOnboarding();
      await obSave({ [path]: option.value });
      paintOnboarding();
    });
    menu.append(item);
  }
  box.append(menu);
}

function paintObDevices() {
  const config = app.settings;
  const devices = app.devices || {};
  obPicker("ob-input", "mic", devices.inputs, config.audio.input_device,
           "audio.input_device");
  obPicker("ob-output", "headset", devices.outputs, config.audio.output_device,
           "audio.output_device");
  paintObLevel();
}

function paintObLevel() {
  const ob = app.onboard;
  const bars = $("ob-bars");
  if (bars.childElementCount !== OB_BARS) {
    bars.textContent = "";
    for (let i = 0; i < OB_BARS; i += 1) bars.append(obEl("span"));
  }
  // A speaking voice peaks well under full scale; the square root spreads
  // it over the bar the way a level meter in dB would.
  const level = Math.min(1, Math.sqrt(ob.level) * 1.15);
  [...bars.children].forEach((bar, i) => {
    const lit = i / OB_BARS < level;
    bar.className = lit ? (i > OB_BARS - 6 ? "is-hot" : "is-on") : "";
    bar.style.height = lit
      ? `${30 + Math.abs(Math.sin(i * 1.7 + level * 9)) * 70}%` : "18%";
  });
  const word = $("ob-level-word");
  let say;
  let tone;
  if (!ob.levelOk) {
    say = t("ob.level_error", "This microphone would not open");
    tone = "is-bad";
  } else if (ob.level > 0.95) {
    say = t("ob.level_loud", "Too loud");
    tone = "is-loud";
  } else if (ob.level > 0.03) {
    say = t("ob.level_good", "Good");
    tone = "is-good";
  } else {
    say = ob.quiet > 40 ? t("ob.level_none", "Nothing heard yet") : "";
    tone = "";
  }
  text(word, say);
  word.className = tone;
}

/** The meter, polled while the microphone screen is up and only then. */
async function obPollLevel() {
  const ob = app.onboard;
  if (!ob || ob.step !== 4 || ob.polling) return;
  ob.polling = true;
  const device = app.settings ? app.settings.audio.input_device : "";
  const reading = await get(`/api/mic/level?device=${encodeURIComponent(device ?? "")}`);
  ob.polling = false;
  if (app.onboard !== ob) return;
  if (!reading) { ob.levelOk = true; return; }
  ob.levelOk = Boolean(reading.ok);
  const peak = Number(reading.peak) || 0;
  // Fast up, slow down, as a meter's needle.
  ob.level = Math.max(peak, ob.level * 0.82);
  ob.quiet = peak > 0.03 ? 0 : ob.quiet + 1;
  if (ob.step === 4) paintObLevel();
}

function obKeyShown(name) {
  if (!name) return "—";
  return OB_KEY_SHOWN[name] || name.toUpperCase();
}

function paintObPtt() {
  const ob = app.onboard;
  const ptt = app.settings.ptt;
  for (const button of $("ob-modes").querySelectorAll("button")) {
    button.setAttribute("aria-pressed", button.dataset.mode === ptt.mode ? "true" : "false");
  }
  const rebind = $("ob-rebind");
  rebind.hidden = ptt.mode === "none";
  let key;
  let title;
  let hint;
  if (ptt.mode === "none") {
    key = "—";
    title = t("ob.no_key", "No key");
    hint = t("ob.hint_off", "Use the microphone button in the window, or type what you would say.");
  } else if (ptt.mode === "joystick") {
    const stick = configuredStick(ptt);
    key = ob.learning ? "…" : String(ptt.joystick_button);
    title = ob.learning ? t("ob.press_button", "Press the button on your yoke or stick")
      : `${stick ? stick.name : t("set.controller", "Controller")} · ${t("set.button", "Button")} ${ptt.joystick_button}`;
    hint = ob.error || t("ob.hint_hold", "Hold it while you speak, with the simulator in front.");
  } else {
    key = ob.binding ? "…" : obKeyShown(ptt.key);
    title = ob.binding ? t("ob.press_key", "Press the key you want to use")
      : t("ob.ptt_key", "Push-to-talk key");
    hint = t("ob.hint_hold", "Hold it while you speak, with the simulator in front.");
  }
  text($("ob-key"), key);
  text($("ob-key-title"), title);
  text($("ob-key-hint"), hint);
  text(rebind, ob.binding || ob.learning ? t("ob.cancel", "Cancel") : t("ob.change", "Change"));
  rebind.disabled = ob.learning;
}

async function obRebind() {
  const ob = app.onboard;
  const ptt = app.settings.ptt;
  ob.error = "";
  if (ptt.mode === "keyboard") {
    ob.binding = !ob.binding;
    paintObPtt();
    return;
  }
  if (ptt.mode !== "joystick" || ob.learning) return;
  ob.learning = true;
  paintObPtt();
  const found = await post("/api/ptt/learn", {});
  ob.learning = false;
  if (found && found.ok) {
    await obSave({ ...stickChoice(found), "ptt.joystick_button": found.button });
    await loadSettings();
  } else if (found && !found.devices) {
    ob.error = t("set.no_controller_found", "no controller connected");
  } else {
    ob.error = t("set.nothing_pressed", "nothing was pressed");
  }
  paintOnboarding();
}

/** The key, while the setup is listening for one. */
function obCaptureKey(event) {
  const ob = app.onboard;
  if (!ob || !ob.binding) return false;
  event.preventDefault();
  event.stopPropagation();
  ob.binding = false;
  if (event.key !== "Escape") obSave({ "ptt.key": keyName(event) }).then(paintOnboarding);
  paintObPtt();
  return true;
}

/** The first officer: whether there is one, who, and how they sound. */
function paintObFirstOfficer() {
  const ob = app.onboard;
  const immersion = app.settings.immersion;
  const on = Boolean(immersion.enabled && immersion.copilot);
  toggle("ob-fo-on", on);
  $("ob-fo-pick").classList.toggle("is-off", !on);
  paintObVoicePicker();
  const listen = $("ob-fo-listen");
  text(listen, ob.listening ? t("ob.fo_listening", "Playing…") : t("ob.fo_listen", "Listen"));
  listen.disabled = ob.listening;
  const voices = (app.devices && app.devices.voices) || [];
  const note = $("ob-fo-note");
  note.classList.toggle("is-bad", Boolean(ob.foError));
  let say = "";
  if (ob.foError) say = ob.foError;
  else if (ob.foHeard) say = `“${ob.foHeard}”`;
  else if (!voices.length) {
    say = t("ob.fo_wait", "The voices are still downloading: for now you can choose a man, a woman or a cast from the aircraft, and pick a person later in Settings, First officer.");
  }
  text(note, say);
}

/** What the first officer's picker holds: a voice key, "m", "f" or "auto". */
function obFoValue() {
  const immersion = app.settings.immersion;
  if (immersion.copilot_gender === "m" || immersion.copilot_gender === "f") {
    return immersion.copilot_gender;
  }
  const key = immersion.copilot_voice || "auto";
  const here = ((app.devices && app.devices.voices) || []).some((v) => v.key === key);
  return here ? key : "auto";
}

/**
 * The voice picker on the setup's first officer screen: the setup's own
 * menu, as the microphone has, with each language's name over its people.
 * The browser's own list could not be themed -- its group headings came out
 * white on white in the dark window.
 */
function paintObVoicePicker() {
  const ob = app.onboard;
  const box = $("ob-fo-voice");
  const open = ob.open === "ob-fo-voice";
  box.classList.toggle("is-open", open);
  box.closest(".ob-field").classList.toggle("is-open", open);
  box.textContent = "";
  const fixed = [
    { value: "auto", label: t("set.voice_auto", "Cast from the aircraft") },
    { value: "m", label: t("set.voice_male", "A man") },
    { value: "f", label: t("set.voice_female", "A woman") },
  ];
  const groups = voiceGroups();
  const current = obFoValue();
  let shown = (fixed.find((f) => f.value === current) || {}).label;
  for (const group of groups) {
    const voice = group.voices.find((v) => v.key === current);
    if (voice) shown = `${voiceLabel(voice)} · ${group.label}`;
  }

  const button = obEl("button");
  button.setAttribute("aria-haspopup", "listbox");
  button.setAttribute("aria-expanded", open ? "true" : "false");
  const glyph = obEl("span", "icon");
  glyph.innerHTML = OB_ICONS.headset;
  button.append(glyph, obEl("span", "value", shown || fixed[0].label));
  button.insertAdjacentHTML("beforeend", OB_ICONS.chevron);
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    ob.open = open ? "" : "ob-fo-voice";
    paintOnboarding();
  });
  box.append(button);
  if (!open) return;

  const menu = obEl("div", "ob-menu");
  menu.setAttribute("role", "listbox");
  const item = (value, label) => {
    const node = obEl("button");
    node.setAttribute("role", "option");
    const selected = value === current;
    node.setAttribute("aria-selected", selected ? "true" : "false");
    node.append(obEl("span", "ob-item", label), obEl("span", "check", selected ? "✓" : ""));
    node.addEventListener("click", async (event) => {
      event.stopPropagation();
      ob.open = "";
      ob.foHeard = "";
      ob.foError = "";
      paintOnboarding();
      await obSave(value === "m" || value === "f"
        ? { "immersion.copilot_voice": "auto", "immersion.copilot_gender": value }
        : { "immersion.copilot_voice": value, "immersion.copilot_gender": "auto" });
      paintOnboarding();
    });
    return node;
  };
  for (const f of fixed) menu.append(item(f.value, f.label));
  for (const group of groups) {
    const heading = obEl("div", "ob-menu-group", group.label);
    heading.setAttribute("role", "presentation");
    menu.append(heading);
    for (const voice of group.voices) menu.append(item(voice.key, voiceLabel(voice)));
  }
  box.append(menu);
  const chosen = menu.querySelector('[aria-selected="true"]');
  if (chosen) chosen.scrollIntoView({ block: "nearest" });
}

async function obListen() {
  const ob = app.onboard;
  if (!ob || ob.listening) return;
  ob.listening = true;
  ob.foError = "";
  paintObFirstOfficer();
  const answer = await post("/api/voice-preview", { voice: obFoValue() });
  ob.listening = false;
  if (answer && answer.ok) ob.foHeard = answer.text;
  else {
    ob.foHeard = "";
    ob.foError = t("ob.fo_cannot", "That voice could not be played. Have the voices finished downloading?");
  }
  if (app.onboard === ob) paintObFirstOfficer();
}

function paintObPlan() {
  const ob = app.onboard;
  const input = $("ob-simbrief");
  if (document.activeElement !== input && !input.value) {
    input.value = app.settings.simbrief.username || "";
  }
  const button = $("ob-import");
  text(button, ob.importing ? t("ob.importing", "Importing…")
    : ob.plan ? t("ob.reimport", "Import again") : t("ob.import", "Import latest plan"));
  button.disabled = ob.importing;
  const error = $("ob-plan-error");
  error.hidden = !ob.planError;
  text(error, ob.planError);

  const box = $("ob-ofp");
  box.hidden = !ob.plan;
  box.textContent = "";
  if (!ob.plan) return;
  const plan = ob.plan;
  const cells = [
    [t("strip.callsign", "Callsign"), plan.callsign || "—"],
    [t("ob.route", "Route"), [plan.departure, plan.destination].filter(Boolean).join(" → ") || "—"],
    [t("set.aircraft_type", "Aircraft type"), plan.aircraft_type || "—"],
    [t("set.cruise", "Cruise altitude"), plan.cruise_altitude_ft
      ? `FL${String(Math.round(plan.cruise_altitude_ft / 100)).padStart(3, "0")}` : "—"],
  ];
  for (const [k, v] of cells) {
    const cell = obEl("div");
    cell.append(obEl("span", "k", k), obEl("span", "v", v));
    box.append(cell);
  }
}

async function obImport() {
  const ob = app.onboard;
  const username = ($("ob-simbrief").value || "").trim();
  if (!username || ob.importing) {
    if (!username) { ob.planError = t("ob.simbrief_empty", "Type your SimBrief username or ID first."); paintObPlan(); }
    return;
  }
  ob.importing = true;
  ob.planError = "";
  paintObPlan();
  await obSave({ "simbrief.username": username });
  const answer = await post("/api/simbrief", { username });
  ob.importing = false;
  if (answer && answer.ok) {
    ob.plan = answer.flight || null;
    if (answer.settings) app.settings = answer.settings;
    paintSettings();
  } else {
    ob.plan = null;
    ob.planError = (answer && answer.error) || t("setup.failed", "failed");
  }
  if (app.onboard === ob) paintObPlan();
}

/** The last screen: what was chosen, in a list. */
function paintObSummary() {
  const ob = app.onboard;
  const config = app.settings;
  const state = app.setup;
  const box = $("ob-summary");
  box.textContent = "";

  let downloads = "";
  if (state) {
    const named = (key) => t(SETUP_NAMES[key][0], SETUP_NAMES[key][1]);
    if (state.running) downloads = t("ob.downloading", "Downloading…");
    else if (state.missing.length) {
      downloads = fill(t("ob.still_missing", "Still to download: {names}"),
                       { names: state.missing.map(named).join(", ") });
    } else downloads = t("ob.all_here", "Everything is here");
  }
  const devices = app.devices || {};
  const input = (devices.inputs || []).find((d) => String(d.index) === String(config.audio.input_device));
  const ptt = config.ptt;
  const pttShown = ptt.mode === "none" ? t("set.off", "Off")
    : ptt.mode === "joystick"
      ? `${t("set.joystick", "Joystick")} · ${t("set.button", "Button")} ${ptt.joystick_button}`
      : `${t("set.keyboard", "Keyboard")} · ${obKeyShown(ptt.key)}`;
  const immersion = config.immersion || {};
  let foShown = t("set.off", "Off");
  if (immersion.enabled && immersion.copilot) {
    const pinned = ((app.devices && app.devices.voices) || [])
      .find((v) => v.key === immersion.copilot_voice);
    if (pinned) foShown = `${pinned.person || pinned.name} · ${voiceCountry(pinned)}`;
    else if (immersion.copilot_gender === "m") foShown = t("set.voice_male", "A man");
    else if (immersion.copilot_gender === "f") foShown = t("set.voice_female", "A woman");
    else foShown = t("set.voice_auto", "Cast from the aircraft");
  }
  const flight = config.flight || {};
  const plan = ob.plan || (flight.callsign ? flight : null);
  const planShown = plan
    ? [plan.callsign, [plan.departure, plan.destination].filter(Boolean).join(" → ")]
        .filter(Boolean).join(" · ")
    : t("ob.manual", "Entered by hand");

  const look = LOOK.current;
  const lookShown = `${t(`set.look_${look.theme}`, look.theme)}`
    + ` · ${t(`set.look_scene_${look.scene}`, look.scene)}`;

  for (const [k, v] of [
    [t("ob.step_look", "Appearance"), lookShown],
    [t("ob.step_downloads", "Downloads"), downloads],
    [t("set.microphone", "Microphone"), input ? input.name : t("set.system_default", "System default")],
    [t("set.ptt", "Push to talk"), pttShown],
    [t("ob.step_fo", "First officer"), foShown],
    [t("ob.step_plan", "Flight plan"), planShown],
  ]) {
    const row = obEl("div", "ob-sum");
    row.append(obEl("span", "k", k), obEl("span", "v", v));
    box.append(row);
  }

  // A first run is served without an engine, so it starts with the next
  // launch; on a running one, a new sound card is opened at startup.
  const note = $("ob-restart");
  let say = "";
  if (app.setupOnly) {
    say = t("ob.restart_first", "Restart WilcoATC once the downloads have finished: the radio starts with them.");
  } else if (ob.restart.size) {
    say = t("set.saved_restart", "saved — restart WilcoATC for this to take effect");
  }
  note.hidden = !say;
  text(note, say);
}

function wireOnboarding() {
  const dialog = $("onboard");
  if (!dialog) return;
  // Not waved away with Escape: the last screen is the way out, and every
  // screen before it can be passed with Continue.
  on(dialog, "cancel", (event) => {
    event.preventDefault();
    if (app.onboard && app.onboard.open) { app.onboard.open = ""; paintOnboarding(); }
  });
  on(dialog, "click", () => {
    if (app.onboard && app.onboard.open) { app.onboard.open = ""; paintOnboarding(); }
  });
  on($("ob-lang"), "click", (event) => {
    event.stopPropagation();
    const ob = app.onboard;
    ob.open = ob.open === "lang" ? "" : "lang";
    paintOnboarding();
  });
  on($("ob-back"), "click", () => obGo(app.onboard.step - 1));
  on($("ob-next"), "click", () => {
    const ob = app.onboard;
    if (ob.step < OB_STEPS.length) obGo(ob.step + 1);
    else finishOnboarding();
  });
  on($("ob-download"), "click", obDownload);
  for (const button of $("ob-modes").querySelectorAll("button")) {
    on(button, "click", async () => {
      app.onboard.binding = false;
      app.onboard.error = "";
      await obSave({ "ptt.mode": button.dataset.mode });
      paintOnboarding();
    });
  }
  on($("ob-rebind"), "click", obRebind);
  // Ahead of everything else, so the key being chosen is not also the
  // space bar keying the microphone.
  document.addEventListener("keydown", obCaptureKey, true);
  on($("ob-fo-on"), "click", async () => {
    const immersion = app.settings.immersion;
    const on = immersion.enabled && immersion.copilot;
    // Turning the first officer on here is also turning the crew on: the
    // setup has no other place to do it, and a voice for nobody is no use.
    await obSave(on ? { "immersion.copilot": false }
                    : { "immersion.enabled": true, "immersion.copilot": true });
    paintOnboarding();
  });
  on($("ob-fo-listen"), "click", obListen);
  on($("ob-import"), "click", obImport);
  on($("ob-simbrief"), "keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); obImport(); }
  });
  on($("start-onboarding"), "click", openOnboarding);
  on(document, "i18n:changed", paintOnboarding);
  setInterval(obPollLevel, 120);
}

// --------------------------------------------------------- guided tour ---

/**
 * The first-run walk round the window.
 *
 * Each step names the screen it is on, the part of it to light (or none,
 * for the opening and closing words), and its words as [key, English]
 * pairs, so the reader's language is looked up when the step is shown and
 * a language changed half way through is followed. ``card`` lights the
 * card the target sits in rather than the target alone.
 *
 * It runs once: when it is finished or skipped, ``ui.tour_done`` is saved to
 * config.yaml, because the desktop window runs in private mode and would
 * forget anything kept in the browser. Settings, Interface shows it again.
 */
const TOUR_STEPS = [
  { screen: "comms", target: null,
    title: ["tour.welcome_title", "Welcome to WilcoATC"],
    text: ["tour.welcome_text", "Air traffic control for your flight, running on this computer. Tune a frequency in COM1, hold your push-to-talk key, speak, and the controller answers. This tour takes about a minute."] },
  { screen: "comms", target: ".sidebar",
    title: ["tour.screens_title", "Four screens"],
    text: ["tour.screens_text", "Comms is where you talk. Airspace is the map, Frequencies the list of who you can call, and Settings everything else."] },
  { screen: "comms", target: "#transcript", card: true,
    title: ["tour.transcript_title", "The frequency, written down"],
    text: ["tour.transcript_text", "Everything said on the frequency, by you and by the controllers, in the order it was said."] },
  { screen: "comms", target: ".compose",
    title: ["tour.talk_title", "Talking"],
    text: ["tour.talk_text", "Hold your push-to-talk key and speak; release it to send. You can also type what you would say and press Enter. The key is chosen in Settings, Push to talk."] },
  { screen: "comms", target: "#phrases",
    title: ["tour.phrases_title", "What to say next"],
    text: ["tour.phrases_text", "Suggestions for where you are in the flight. Click one to send it as it is."] },
  { screen: "comms", target: ".card.station",
    title: ["tour.station_title", "Who is listening"],
    text: ["tour.station_text", "The frequency in COM1 and who answers on it. Below it, the standby frequency and the button that swaps the two, as the radio in the cockpit does."] },
  { screen: "comms", target: "#edit-flight", card: true,
    title: ["tour.strip_title", "Your flight strip"],
    text: ["tour.strip_text", "Callsign, aircraft, squawk, runway and route, as the controllers know them. The pencil changes them, and your SimBrief plan can fill them in."] },
  { screen: "airspace", target: "#map", card: true,
    title: ["tour.airspace_title", "The map"],
    text: ["tour.airspace_text", "Airports, the stations whose frequencies reach you, and the other traffic. Zoom in close to a field and it becomes a 3D model of the airport."] },
  { screen: "frequencies", target: "#freq-rows", card: true,
    title: ["tour.frequencies_title", "Frequencies"],
    text: ["tour.frequencies_text", "Every published frequency within reach, nearest first. Click one to tune it in COM1, in the aircraft as well."] },
  { screen: "settings", target: "#settings-nav",
    title: ["tour.settings_title", "Settings"],
    text: ["tour.settings_text", "The voices, the microphone and the push-to-talk key, the look of this window and more. This tour can be shown again from Interface."] },
  { screen: "comms", target: null,
    title: ["tour.done_title", "Ready to fly"],
    text: ["tour.done_text", "Start the simulator, find the frequency of your airport under Frequencies, tune it and make your first call. Have a good flight."] },
];

const tourWords = (pair) => t(pair[0], pair[1]);

function tourTarget(step) {
  if (!step.target) return null;
  const found = document.querySelector(step.target);
  const node = found && step.card ? (found.closest(".card") || found) : found;
  if (!node) return null;
  const box = node.getBoundingClientRect();
  // An empty suggestion row, a card not drawn yet: nothing to point at, so
  // the step is said in the middle of the window instead.
  return box.width > 4 && box.height > 4 ? box : null;
}

/** Light the target and put the card beside it, inside the window. */
function placeTour() {
  const tour = app.tour;
  if (!tour) return;
  const step = TOUR_STEPS[tour.index];
  const ring = $("tour-ring");
  const card = $("tour-card");
  const box = tourTarget(step);
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const w = card.offsetWidth;
  const h = card.offsetHeight;
  const gap = 16;
  const margin = 16;
  ring.classList.toggle("is-none", !box);
  if (!box) {
    card.style.left = `${Math.max(margin, (vw - w) / 2)}px`;
    card.style.top = `${Math.max(margin, (vh - h) / 2)}px`;
    return;
  }
  const pad = 6;
  ring.style.left = `${box.left - pad}px`;
  ring.style.top = `${box.top - pad}px`;
  ring.style.width = `${box.width + 2 * pad}px`;
  ring.style.height = `${box.height + 2 * pad}px`;

  const clampTop = (top) => Math.min(Math.max(margin, top), vh - h - margin);
  const clampLeft = (left) => Math.min(Math.max(margin, left), vw - w - margin);
  let left;
  let top;
  if (box.right + pad + gap + w <= vw - margin) {
    left = box.right + pad + gap;              // beside it, on the right
    top = clampTop(box.top);
  } else if (box.left - pad - gap - w >= margin) {
    left = box.left - pad - gap - w;           // on the left
    top = clampTop(box.top);
  } else if (box.bottom + pad + gap + h <= vh - margin) {
    left = clampLeft(box.left);                // under it
    top = box.bottom + pad + gap;
  } else if (box.top - pad - gap - h >= margin) {
    left = clampLeft(box.left);                // over it
    top = box.top - pad - gap - h;
  } else {
    // It fills the window, as the map does: the card goes in its corner.
    left = vw - w - margin - 8;
    top = vh - h - margin - 8;
  }
  card.style.left = `${left}px`;
  card.style.top = `${top}px`;
}

function paintTour() {
  const tour = app.tour;
  if (!tour) return;
  const step = TOUR_STEPS[tour.index];
  const last = tour.index === TOUR_STEPS.length - 1;
  if (app.screen !== step.screen) show(step.screen);
  text($("tour-count"), fill(t("tour.count", "{n} of {total}"),
                             { n: tour.index + 1, total: TOUR_STEPS.length }));
  text($("tour-title"), tourWords(step.title));
  text($("tour-text"), tourWords(step.text));
  hide($("tour-back"), tour.index === 0);
  hide($("tour-skip"), last);
  text($("tour-next"), last ? t("tour.done", "Finish") : t("tour.next", "Next"));
  // After the screen has been drawn, which is when the target has a size.
  requestAnimationFrame(() => requestAnimationFrame(placeTour));
  $("tour-next").focus({ preventScroll: true });
}

function startTour() {
  const overlay = $("tour");
  if (!overlay || app.tour) return;
  app.tour = { index: 0 };
  overlay.hidden = false;
  paintTour();
}

function stepTour(by) {
  const tour = app.tour;
  if (!tour) return;
  const next = tour.index + by;
  if (next < 0) return;
  if (next >= TOUR_STEPS.length) { endTour(); return; }
  tour.index = next;
  paintTour();
}

/** Finished or skipped, it is not shown by itself again. */
function endTour() {
  if (!app.tour) return;
  app.tour = null;
  $("tour").hidden = true;
  show(TOUR_STEPS[0].screen);              // back where it started
  if (!app.settings || !app.settings.ui.tour_done) {
    post("/api/settings", { "ui.tour_done": true }).then((result) => {
      if (result && result.ok) app.settings = result.config;
    });
  }
  $("say").focus({ preventScroll: true });
}

/**
 * Start the tour on a first run, once the way is clear: after the startup
 * notice, and after the download dialog if anything was missing -- which can
 * take minutes, so it waits for as long as a dialog is open.
 */
async function maybeStartTour() {
  if (!app.settings) await loadSettings();
  if (!app.settings || app.settings.ui.tour_done || app.tour) return;
  const busy = [...document.querySelectorAll("dialog")].some((d) => d.open);
  if (busy) {
    setTimeout(maybeStartTour, 500);
    return;
  }
  startTour();
}

function wireTour() {
  on($("tour-next"), "click", () => stepTour(1));
  on($("tour-back"), "click", () => stepTour(-1));
  on($("tour-skip"), "click", endTour);
  on($("start-tour"), "click", startTour);
  on(window, "resize", placeTour);
  on(document, "keydown", (event) => {
    if (!app.tour) return;
    if (event.key === "Escape") { event.preventDefault(); endTour(); }
    else if (event.key === "ArrowRight") { event.preventDefault(); stepTour(1); }
    else if (event.key === "ArrowLeft") { event.preventDefault(); stepTour(-1); }
  });
  // The words are looked up as each step is shown; a language changed
  // while the tour is open is followed at once.
  on(document, "i18n:changed", paintTour);
}

// --------------------------------------------------------------- start ---

// The language is settled before anything is drawn, so the first paint is
// already in the right words rather than flashing English first.
(async () => {
  let wanted = "auto";
  try {
    const settings = await get("/api/settings");
    wanted = (settings && settings.config.ui && settings.config.ui.language)
      || "auto";
    // And the look, before the first paint of anything else.
    if (settings && settings.config.ui) LOOK.apply(lookFrom(settings.config.ui));
  } catch (error) {
    wanted = "auto";
  }
  await I18N.apply(wanted === "auto" ? I18N.detect() : wanted);
  begin();
})();

// Anything already on screen is redrawn when the language changes.
document.addEventListener("i18n:changed", () => {
  if (app.screen) text($("page-title"),
                       t(SCREENS[app.screen].key, SCREENS[app.screen].title));
  if (app.state) apply(app.state);
  if (app.settings) paintSettings();
  if (app.setup) { paintComponents(); paintSetup(); }
  // The section names are the cards' headings, which have just changed.
  buildSettingsNav();
});

function begin() {
wire();
showNotice();
show("comms");
connect();
get("/api/state").then((state) => state && apply(state));
loadSettings();
// What is installed, so the Components card is right the first time the
// settings are opened. The first-run prompt itself waits for the notice.
loadSetup();
loadAirspace(true);
setInterval(() => {
  if (app.screen === "airspace") loadAirspace();
}, 5000);
// The 3D model moves every frame and is told where everybody is every
// second; between the two it carries each aeroplane along on its own speed.
try { app.show3d = localStorage.getItem("wilco.map3d") !== "0"; } catch (error) { app.show3d = true; }
if ($("map-3d")) $("map-3d").setAttribute("aria-pressed", app.show3d ? "true" : "false");
setupModel();
setInterval(loadLive, 1000);
// The clock turns over on the minute whether or not the engine says anything.
paintClock();
setInterval(paintClock, 5000);
}
