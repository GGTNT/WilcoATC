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
};

// -------------------------------------------------------------- helpers ---

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

  if (name === "frequencies") loadFrequencies();
  if (name === "airspace") loadAirspace();
  if (name === "settings") { loadSettings(); loadSetup(); }
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
    } else if (payload.kind === "setup") {
      // The installer runs on its own thread and reports as it goes, so the
      // bars move without the page asking for anything.
      app.setup = payload.setup;
      paintSetup();
      paintComponents();
    }
  };
}

function link(up) {
  const node = $("link-state");
  node.classList.toggle("is-down", !up);
  text($("link-text"), up ? t("link.connected", "engine connected")
                        : t("link.reconnecting", "reconnecting…"));
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
  const radio = state.radio || {};

  const where = radio.airport
    ? `${radio.airport} · ${radio.here} · ${zulu()}`
    : (state.source === "simconnect"
        ? `${t("head.no_position", "no position yet")} · ${zulu()}`
        : `${t("head.no_simulator", "no simulator")} · ${zulu()}`);
  text($("page-sub"), where);

  const micPill = $("mic-pill");
  const talking = state.transmitting;
  const playing = state.playing;
  micPill.classList.toggle("is-live", Boolean(talking || playing));
  text($("mic-text"), talking ? t("mic.transmitting", "Transmitting")
    : playing ? t("mic.receiving", "Receiving") : t("mic.ready", "Mic ready"));

  text($("tuned-pill"), radio.station
    ? `${mhz(radio.com1)} · ${radio.station}`
    : `${mhz(radio.com1)} · ${t("freq.nobody", "nobody listening")}`);

  const aircraft = state.aircraft || {};
  text($("callsign"), aircraft.callsign || "—");
  text($("callsign-spoken"), aircraft.spoken || "");
  text($("initials"), (aircraft.callsign || "—").slice(0, 2).toUpperCase());
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
       radio.station || t("comms.nobody_here", "nobody on this frequency"));

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

function paintPhrases() {
  const phase = (app.state.flight || {}).phase || "preflight";
  const wanted = PHRASES[phase] || ["say again", "radio check"];
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

    const isTuned = Math.abs(station.mhz - tuned) < 0.005;
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
       radio.station || t("freq.nobody", "nobody listening"));
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
  $("set-volume").value = Math.round(config.audio.volume * 100);
  text($("volume-value"), `${Math.round(config.audio.volume * 100)}%`);

  chips("ptt-modes", "mode", config.ptt.mode);
  $("set-ptt-key").value = config.ptt.key;
  $("ptt-key-block").hidden = config.ptt.mode !== "keyboard";
  $("ptt-stick-block").hidden = config.ptt.mode !== "joystick";
  const sticks = $("set-joystick");
  sticks.replaceChildren();
  if (!(app.devices.joysticks || []).length) {
    // Three different things go wrong here and they need three different
    // answers. Telling somebody who has just installed pygame-ce to install
    // pygame-ce is how a working setup comes to look broken.
    const why = app.devices.joystick_reason || "missing";
    const option = document.createElement("option");
    option.textContent = why === "missing"
      ? t("set.no_controller", "no controller found — pip install pygame-ce")
      : why === "none"
        ? t("set.no_controller_found", "no controller connected — plug one in and reopen this screen")
        : `${t("set.no_controller_found", "no controller connected")} (${why})`;
    option.value = "0";
    sticks.appendChild(option);
  }
  for (const stick of app.devices.joysticks || []) {
    const option = document.createElement("option");
    option.value = String(stick.index);
    option.textContent = `${stick.name} · ${stick.buttons} buttons`;
    sticks.appendChild(option);
  }
  sticks.value = String(config.ptt.joystick_index);
  $("set-joystick-button").value = config.ptt.joystick_button;

  // A yoke that is switched off looks exactly like a yoke that is not
  // supported. Naming it says which, so nobody goes hunting for a driver
  // when the answer is a USB lead.
  const known = app.devices.joystick_known || [];
  const offline = $("joystick-known");
  offline.hidden = !known.length;
  offline.textContent = known.length
    ? t("set.controller_offline",
        "Known to this computer but not connected: {names}")
        .replace("{names}", known.join(", "))
    : "";

  paintLanguage();
  paintInterfaceLanguages(config.ui && config.ui.language);
  chips("dialect-chips", "dialect", config.atc.dialect);
  $("set-simbrief").value = (config.simbrief && config.simbrief.username) || "";
  toggle("set-gsx", config.gsx ? config.gsx.enabled : true);
  toggle("set-gsx-control", config.gsx ? config.gsx.control : true);
  toggle("set-gsx-voice", config.gsx ? config.gsx.voice : true);
  toggle("set-traffic", config.traffic.enabled);
  chips("density-chips", "density", String(config.traffic.density));
  chips("callsign-chips", "callsigns", config.traffic.callsigns || "auto");
  toggle("set-strict-digits", config.atc.strict_icao_digits);
  toggle("set-handoff", config.atc.proactive_handoff);
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

  const card = document.querySelector(".card.immersion");
  if (card) card.classList.toggle("is-off", !immersion.enabled);
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
function fillVoices(select, chosen, gender) {
  if (!select) return;
  if (!select.options.length) {
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
    for (const voice of app.devices.voices || []) {
      const option = document.createElement("option");
      option.value = voice.key;
      option.textContent = `${voice.name} · ${voice.accent.toUpperCase()}`
        + ` · ${voice.gender === "f" ? t("set.woman", "woman")
                                     : t("set.man", "man")}`;
      select.appendChild(option);
    }
  }
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

function wireImmersion() {
  for (const [id, path] of Object.entries(IMMERSION_SWITCHES)) {
    on($(id), "click", (event) => {
      const next = event.currentTarget.getAttribute("aria-pressed") !== "true";
      save({ [path]: next }, "saved-immersion");
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
      if (value === "m" || value === "f") {
        save({ [voicePath]: "auto", [genderPath]: value }, "saved-immersion");
      } else {
        save({ [voicePath]: value, [genderPath]: "auto" }, "saved-immersion");
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

  // The space bar keys the microphone, unless you are typing into something.
  on(document, "keydown", (event) => {
    if (event.code !== "Space" || event.repeat) return;
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName)) return;
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
  on($("set-joystick"), "change", (e) =>
    save({ "ptt.joystick_index": Number(e.target.value) }, "saved-ptt"));
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
        $("set-joystick").value = String(found.index);
        $("set-joystick-button").value = found.button;
        await save({
          "ptt.joystick_index": found.index,
          "ptt.joystick_button": found.button,
        }, "saved-ptt");
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

  for (const chip of $("dialect-chips").querySelectorAll(".chip")) {
    on(chip, "click", () => save({ "atc.dialect": chip.dataset.dialect }, "saved-atc"));
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
  const switches = {
    "set-strict-digits": "atc.strict_icao_digits",
    "set-handoff": "atc.proactive_handoff",
    "set-traffic": "traffic.enabled",
    "set-wx-online": "weather.online",
    "set-wx-sim": "weather.prefer_sim",
    "set-sim": "sim.enabled",
  };
  for (const [id, path] of Object.entries(switches)) {
    const noteId = id.startsWith("set-wx") ? "saved-weather"
      : id === "set-traffic" ? "saved-traffic"
      : id === "set-sim" ? "saved-sim" : "saved-atc";
    on($(id), "click", (e) => {
      const next = e.currentTarget.getAttribute("aria-pressed") !== "true";
      save({ [path]: next }, noteId);
    });
  }

  wireImmersion();
  wireOperator();

  // --- the flight editor, reachable from the identity in the sidebar ---
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
function paintComponents() {
  const box = $("parts");
  const state = app.setup;
  if (!state) return;
  paintComponentsAlert();
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
    // The extra voices are the one thing on this screen that is a choice
    // rather than a necessity, so they are asked for by name.
    const extra = $("setup-all-voices");
    const result = await post("/api/setup",
      extra && extra.checked ? { all_voices: true } : {});
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
    loadSetup(true);
  });

  dialog.showModal();
  agree.focus({ preventScroll: true });
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
}
