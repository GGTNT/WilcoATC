/*
 * The airport, close up and in three dimensions.
 *
 * The flat map is a chart, and a chart is right for the wide view: coasts,
 * regions, who is on frequency and how far they reach. Close in it stops
 * being enough. A departure pushing back, the one taxiing behind it, an
 * arrival on a two-mile final and where each of them is about to go are a
 * picture of an airport, and an airport is a thing with height.
 *
 * So below a few miles across the chart hands over to this: the same view
 * (the same centre, the same span, the same aeroplane selected), drawn as a
 * white architectural model. Runways and taxiways from the simulator's own
 * layout, stands where the stands are, terminals raised behind the stands --
 * the one thing here that is not surveyed, and drawn plain so it does not
 * pretend to be -- and every aeroplane as a model that moves between the
 * updates the way the real one does, with the rest of its route laid out in
 * front of it.
 *
 * Black and white, like the rest of the panel. The only ink is what matters:
 * you, the aeroplane you picked, and where that one is going.
 *
 * It owns no state about the view. Where it looks and what is selected
 * belong to the chart (Airspace), and this reads them every frame, so the
 * two can hand over at any moment without either jumping.
 */

import * as THREE from "./vendor/three.module.min.js";

const RAD = Math.PI / 180;
const FT = 0.3048;
const NM = 1852;

// The palette: paper, and ink.
// The base board is a shade under the models, so white buildings and white
// aeroplanes stand off it the way they do on an architect's table; the
// paving steps down in grey from apron to runway.
const PAPER = 0xdcdbd7;
const GROUND = 0xd9d8d4;
const APRON = 0xc9c8c4;
const TAXIWAY = 0xbab9b5;
const RUNWAY = 0x8f8e8b;
const PAINT = 0xf4f4f2;
const CLAY = 0xf6f5f2;
const INK = 0x141414;
const GREY = 0x8d8d8a;

// Below this many miles across, the airport is drawn in three dimensions.
export const SPAN_NM = 8;

// Buildings are raised higher than life, the way an architect's model is:
// at true proportion a terminal half a kilometre long and sixteen metres
// high is a kerb, and it is the height that makes a model read.
const TALL = 1.8;

const Airspace3D = (() => {
  let canvas = null;
  let labelsLayer = null;
  let renderer = null;
  let scene = null;
  let camera = null;
  let sun = null;
  let ok = false;
  let active = false;
  let frame = 0;

  // Where the model's metres are measured from. Moved to the airport when
  // one arrives, so the numbers stay small and the shadows stay sharp.
  const origin = { lat: 0, lon: 0, elevationFt: 0, set: false };

  // The camera's own freedom: which way it looks and how far it leans.
  // North up and forty-eight degrees off the vertical until somebody turns
  // it, which reads as a map that has been tilted rather than a game.
  const orbit = { bearing: 0, pitch: 48 };

  let airport = null;          // the scene, as it came from the server
  let airportGroup = null;
  const craft = new Map();     // callsign -> the model and where it is going
  let own = null;              // you
  let pathsGroup = null;
  const labels = new Map();    // callsign -> its label element

  let getView = () => null;
  let getSelected = () => "";
  let getFollowed = () => "";
  let onPick = () => {};

  /* --------------------------------------------------------------------
   * setting up
   * ------------------------------------------------------------------ */

  function init(options) {
    canvas = options.canvas;
    labelsLayer = options.labels;
    getView = options.view;
    getSelected = options.selected;
    onPick = options.pick || onPick;
    getFollowed = options.followed || getFollowed;
    try {
      renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    } catch (error) {
      // No WebGL: the chart simply stays a chart at every scale.
      ok = false;
      return false;
    }
    renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    // No tone mapping: the palette is chosen as it should appear, and ACES
    // pulled every grey towards the same pale one.
    renderer.toneMapping = THREE.NoToneMapping;

    scene = new THREE.Scene();
    scene.background = new THREE.Color(PAPER);
    scene.fog = new THREE.Fog(PAPER, 9000, 30000);

    camera = new THREE.PerspectiveCamera(38, 1, 2, 120000);

    scene.add(new THREE.HemisphereLight(0xffffff, 0xa9a7a1, 1.05));
    sun = new THREE.DirectionalLight(0xffffff, 2.9);
    sun.castShadow = true;
    sun.shadow.mapSize.set(2048, 2048);
    sun.shadow.bias = -0.0004;
    sun.shadow.normalBias = 0.6;
    sun.shadow.radius = 4;
    scene.add(sun);
    scene.add(sun.target);

    // Pushed back in the depth buffer, so the paving laid on it a few
    // centimetres up always wins. Without this a tilted view lost the
    // runway to the ground under it and kept only the paint on top.
    const ground = new THREE.Mesh(
      new THREE.PlaneGeometry(400000, 400000),
      new THREE.MeshStandardMaterial({ color: GROUND, roughness: 1,
                                       polygonOffset: true,
                                       polygonOffsetFactor: 4,
                                       polygonOffsetUnits: 16 }));
    ground.rotation.x = -Math.PI / 2;
    ground.receiveShadow = true;
    scene.add(ground);

    // A faint grid, the way a model's base board is ruled.
    const grid = new THREE.GridHelper(40000, 100, 0xcfceca, 0xd2d1cd);
    grid.position.y = 0.02;
    grid.material.transparent = true;
    grid.material.opacity = 0.5;
    scene.add(grid);

    pathsGroup = new THREE.Group();
    scene.add(pathsGroup);
    ok = true;
    return true;
  }

  function available() { return ok; }

  /* --------------------------------------------------------------------
   * coordinates
   * ------------------------------------------------------------------ */

  function local(lat, lon, altitudeFt = null) {
    const cos = Math.cos(origin.lat * RAD);
    const x = (lon - origin.lon) * 111320 * cos;
    const z = -(lat - origin.lat) * 110540;
    const y = altitudeFt === null
      ? 0 : Math.max(0, (altitudeFt - origin.elevationFt) * FT);
    return new THREE.Vector3(x, y, z);
  }

  function toLatLon(x, z) {
    const cos = Math.cos(origin.lat * RAD);
    return { lat: origin.lat - z / 110540, lon: origin.lon + x / (111320 * cos) };
  }

  function setOrigin(lat, lon, elevationFt) {
    origin.lat = lat;
    origin.lon = lon;
    origin.elevationFt = elevationFt || 0;
    origin.set = true;
    // Everything placed against the old origin is placed again.
    for (const entry of craft.values()) entry.placed = false;
  }

  /* --------------------------------------------------------------------
   * the airport
   * ------------------------------------------------------------------ */

  const mats = {};
  function mat(name, color, extra = {}) {
    if (!mats[name]) {
      mats[name] = new THREE.MeshStandardMaterial({
        color, roughness: 0.92, metalness: 0, ...extra });
    }
    return mats[name];
  }

  /** A flat strip between two points on the ground. */
  function strip(a, b, width, material, y, group) {
    const length = a.distanceTo(b);
    if (length < 0.5) return;
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(width, 0.25, length),
                                material);
    mesh.position.set((a.x + b.x) / 2, y, (a.z + b.z) / 2);
    mesh.rotation.y = Math.atan2(b.x - a.x, b.z - a.z);
    mesh.receiveShadow = true;
    group.add(mesh);
    return mesh;
  }

  function disc(at, radius, material, y, group) {
    const mesh = new THREE.Mesh(new THREE.CylinderGeometry(radius, radius, 0.25, 20),
                                material);
    mesh.position.set(at.x, y, at.z);
    mesh.receiveShadow = true;
    group.add(mesh);
  }

  function setAirport(data) {
    if (!ok || !data || !data.ident) return;
    if (airport && airport.ident === data.ident
        && Boolean(airport.ground) === Boolean(data.ground)) return;
    airport = data;
    setOrigin(data.lat, data.lon, data.elevation_ft);
    if (airportGroup) {
      scene.remove(airportGroup);
      airportGroup.traverse((node) => node.geometry && node.geometry.dispose());
    }
    airportGroup = new THREE.Group();

    for (const runway of data.runways || []) drawRunway(runway, airportGroup);

    const ground = data.ground;
    if (ground) {
      for (const [la, lo, lb, lob, toStand] of ground.taxiways) {
        const a = local(la, lo);
        const b = local(lb, lob);
        const width = toStand ? 14 : 22;
        strip(a, b, width, mat("taxi", TAXIWAY), 0.5, airportGroup);
        disc(a, width / 2, mat("taxi", TAXIWAY), 0.5, airportGroup);
        disc(b, width / 2, mat("taxi", TAXIWAY), 0.5, airportGroup);
      }
      for (const stand of ground.stands) {
        const at = local(stand.lat, stand.lon);
        disc(at, Math.max(8, stand.radius * 1.25), mat("apron", APRON), 0.25,
             airportGroup);
      }
      raiseTerminals(ground.stands, airportGroup);
    }
    scene.add(airportGroup);
  }

  function drawRunway(runway, group) {
    const a = local(runway.le_lat, runway.le_lon);
    const b = local(runway.he_lat, runway.he_lon);
    const width = runway.width_m || 45;
    strip(a, b, width, mat("runway", RUNWAY), 0.8, group);
    const length = a.distanceTo(b);
    const dir = new THREE.Vector3().subVectors(b, a).normalize();
    const side = new THREE.Vector3(-dir.z, 0, dir.x);
    const paint = mat("paint", PAINT);

    // The centreline, dashed the way it is painted.
    for (let d = 90; d < length - 90; d += 50) {
      const p = a.clone().addScaledVector(dir, d);
      const q = a.clone().addScaledVector(dir, Math.min(d + 30, length - 90));
      strip(p, q, 0.9, paint, 1.05, group);
    }
    // The threshold bars at each end, and the number beyond them.
    for (const [end, towards, ident] of [[a, dir, runway.le_ident],
                                         [b, dir.clone().negate(), runway.he_ident]]) {
      const bars = 8;
      for (let i = 0; i < bars; i += 1) {
        const offset = (i - (bars - 1) / 2) * (width * 0.8 / bars);
        const start = end.clone().addScaledVector(towards, 6)
          .addScaledVector(side, offset);
        strip(start, start.clone().addScaledVector(towards, 30),
              width * 0.8 / bars * 0.55, paint, 1.05, group);
      }
      const label = number(ident, width);
      label.position.copy(end.clone().addScaledVector(towards, 62));
      label.position.y = 1.1;
      label.rotation.set(-Math.PI / 2, 0,
                         Math.atan2(towards.x, -towards.z) + Math.PI);
      group.add(label);
    }
  }

  /** A runway number, painted flat. */
  function number(text, width) {
    const size = 256;
    const art = document.createElement("canvas");
    art.width = size;
    art.height = size;
    const pen = art.getContext("2d");
    pen.fillStyle = "#fbfbfa";
    pen.font = "800 150px 'Plus Jakarta Sans', 'Segoe UI', sans-serif";
    pen.textAlign = "center";
    pen.textBaseline = "middle";
    pen.fillText(String(text || ""), size / 2, size / 2);
    const texture = new THREE.CanvasTexture(art);
    texture.colorSpace = THREE.SRGBColorSpace;
    const plane = new THREE.Mesh(
      new THREE.PlaneGeometry(width * 0.7, width * 0.7),
      new THREE.MeshStandardMaterial({ map: texture, transparent: true,
                                       roughness: 1 }));
    plane.receiveShadow = true;
    return plane;
  }

  /**
   * The terminals, raised behind the stands.
   *
   * Not surveyed -- the simulator does not say where the buildings are --
   * and so drawn as plain blocks where a terminal has to be: across the
   * noses of a row of stands. A row is stands that face the same way and
   * sit beside each other. Gates get an air bridge.
   */
  function raiseTerminals(stands, group) {
    const clay = mat("clay", CLAY);
    const rows = [];
    for (const stand of stands) {
      const at = local(stand.lat, stand.lon);
      const nose = new THREE.Vector3(Math.sin(stand.heading * RAD), 0,
                                     -Math.cos(stand.heading * RAD));
      const anchor = at.clone().addScaledVector(nose, stand.radius + 14);
      const row = rows.find((r) => Math.abs(((r.heading - stand.heading + 540)
                                            % 360) - 180) < 30
                            && r.anchors.some((p) => p.distanceTo(anchor) < 90));
      if (row) {
        row.anchors.push(anchor);
        row.stands.push({ stand, at, nose });
      } else {
        rows.push({ heading: stand.heading, anchors: [anchor],
                    stands: [{ stand, at, nose }] });
      }
    }
    let biggest = null;
    for (const row of rows) {
      const nose = row.stands[0].nose;
      const across = new THREE.Vector3(-nose.z, 0, nose.x);
      const centre = row.anchors.reduce((s, p) => s.add(p), new THREE.Vector3())
        .multiplyScalar(1 / row.anchors.length);
      const spread = row.anchors.map((p) => p.clone().sub(centre).dot(across));
      const length = Math.max(40, Math.max(...spread) - Math.min(...spread) + 50);
      const light = row.stands.every(({ stand }) => stand.radius < 12);
      const depth = light ? 22 : 42;
      const height = (light ? 8 : 16) * TALL;
      const box = new THREE.Mesh(new THREE.BoxGeometry(length, height, depth), clay);
      const middle = (Math.max(...spread) + Math.min(...spread)) / 2;
      box.position.copy(centre.clone().addScaledVector(across, middle)
        .addScaledVector(nose, depth / 2));
      box.position.y = height / 2;
      box.rotation.y = Math.atan2(across.x, across.z) - Math.PI / 2;
      box.castShadow = true;
      box.receiveShadow = true;
      group.add(box);
      // A roof edge, which is what makes a block read as a building.
      const roof = new THREE.Mesh(new THREE.BoxGeometry(length - 6, 1.2, depth - 6),
                                  clay);
      roof.position.copy(box.position);
      roof.position.y = height + 0.6;
      roof.rotation.y = box.rotation.y;
      roof.castShadow = true;
      group.add(roof);

      for (const { stand, at, nose: n } of row.stands) {
        if (!stand.gate && stand.radius < 15) continue;
        const from = at.clone().addScaledVector(n, stand.radius + 14);
        const to = at.clone().addScaledVector(n, Math.max(6, stand.radius * 0.45));
        const bridge = new THREE.Mesh(
          new THREE.BoxGeometry(3.4, 3.4 * TALL * 0.8, from.distanceTo(to)), clay);
        bridge.position.set((from.x + to.x) / 2, 5 * TALL * 0.8,
                            (from.z + to.z) / 2);
        bridge.rotation.y = Math.atan2(to.x - from.x, to.z - from.z);
        bridge.castShadow = true;
        group.add(bridge);
      }
      if (!biggest || length > biggest.length) {
        biggest = { length, box, across, nose, depth, height };
      }
    }
    if (biggest) raiseTower(biggest, group);
  }

  /** The tower, beside the biggest terminal: a shaft and a glass cab. */
  function raiseTower(beside, group) {
    const clay = mat("clay", CLAY);
    const at = beside.box.position.clone()
      .addScaledVector(beside.across, beside.length / 2 + 30)
      .addScaledVector(beside.nose, beside.depth * 0.3);
    const tall = 44 * TALL;
    const shaft = new THREE.Mesh(new THREE.CylinderGeometry(6, 7.5, tall, 28), clay);
    shaft.position.set(at.x, tall / 2, at.z);
    const cab = new THREE.Mesh(new THREE.CylinderGeometry(13, 10, 9, 36), clay);
    cab.position.set(at.x, tall + 4.5, at.z);
    const glass = new THREE.Mesh(new THREE.CylinderGeometry(13.2, 13.2, 3.4, 36),
                                 mat("glass", 0x3a3a3a, { roughness: 0.4 }));
    glass.position.set(at.x, tall + 5.5, at.z);
    const roof = new THREE.Mesh(new THREE.CylinderGeometry(14.5, 14.5, 1.6, 36), clay);
    roof.position.set(at.x, tall + 9.8, at.z);
    for (const part of [shaft, cab, glass, roof]) {
      part.castShadow = true;
      part.receiveShadow = true;
      group.add(part);
    }
  }

  /* --------------------------------------------------------------------
   * the aeroplanes
   * ------------------------------------------------------------------ */

  /** An aeroplane, facing north, made of a handful of shapes. */
  function model(kind, material) {
    const group = new THREE.Group();
    const light = kind === "light";
    const heavy = kind === "heavy";
    const length = light ? 8.5 : heavy ? 64 : 38;
    const span = light ? 11 : heavy ? 60 : 35.8;
    const radius = light ? 0.65 : heavy ? 3.1 : 2.0;

    const body = new THREE.Mesh(
      new THREE.CapsuleGeometry(radius, length - radius * 2, 6, 14), material);
    body.rotation.x = Math.PI / 2;
    body.position.y = radius + (light ? 0.9 : 1.6);
    group.add(body);

    const wing = (width, root, tip, sweep, thickness, y, z) => {
      const shape = new THREE.Shape();
      shape.moveTo(0, 0);
      shape.lineTo(width / 2, sweep);
      shape.lineTo(width / 2, sweep + tip);
      shape.lineTo(0, root);
      shape.lineTo(-width / 2, sweep + tip);
      shape.lineTo(-width / 2, sweep);
      shape.closePath();
      const mesh = new THREE.Mesh(
        new THREE.ExtrudeGeometry(shape, { depth: thickness, bevelEnabled: false }),
        material);
      mesh.rotation.x = Math.PI / 2;
      mesh.position.set(0, y, z);
      return mesh;
    };
    const wingY = light ? radius * 2 + 1.0 : radius + 1.2;
    group.add(wing(span, length * (light ? 0.17 : 0.22), length * 0.06,
                   light ? 0 : length * 0.17, light ? 0.25 : 0.5,
                   wingY, -length * (light ? 0.12 : 0.05)));
    group.add(wing(span * 0.34, length * 0.12, length * 0.05, length * 0.06,
                   0.3, radius * 1.4 + 1.6, length * 0.36));
    const fin = new THREE.Shape();
    fin.moveTo(0, 0);
    fin.lineTo(length * 0.16, 0);
    fin.lineTo(length * 0.22, length * (light ? 0.16 : 0.24));
    fin.lineTo(length * 0.13, length * (light ? 0.16 : 0.24));
    fin.closePath();
    const tail = new THREE.Mesh(
      new THREE.ExtrudeGeometry(fin, { depth: 0.35, bevelEnabled: false }), material);
    tail.rotation.y = -Math.PI / 2;
    tail.position.set(0.17, radius * 1.6 + 1.2, length * 0.28);
    group.add(tail);

    if (!light) {
      const engines = heavy ? [-0.34, -0.18, 0.18, 0.34] : [-0.3, 0.3];
      for (const at of engines) {
        const engine = new THREE.Mesh(
          new THREE.CylinderGeometry(radius * 0.62, radius * 0.55, length * 0.12, 16),
          material);
        engine.rotation.x = Math.PI / 2;
        engine.position.set(span * at, radius + 0.4,
                            -length * 0.08 + Math.abs(at) * length * 0.2);
        group.add(engine);
      }
    }
    group.traverse((node) => {
      if (node.isMesh) { node.castShadow = true; node.receiveShadow = true; }
    });
    return group;
  }

  function kindOf(aircraft) {
    const type = String(aircraft.type || "").toUpperCase();
    if (/^(C1|C2|PA|P28|DA4|SR2|TB|BE3|R44)/.test(type)) return "light";
    if (/^(B74|B77|B78|A33|A34|A35|A38|B76|MD1)/.test(type)) return "heavy";
    return "airliner";
  }

  function setTraffic(traffic, ownship) {
    if (!ok) return;
    const now = performance.now();
    const seen = new Set();
    for (const aircraft of traffic || []) {
      seen.add(aircraft.callsign);
      let entry = craft.get(aircraft.callsign);
      if (!entry) {
        const kind = kindOf(aircraft);
        entry = {
          kind,
          white: model(kind, mat("white", CLAY)),
          ink: model(kind, mat("ink", INK, { roughness: 0.6 })),
          pos: null, heading: aircraft.heading || 0, placed: false,
        };
        entry.node = new THREE.Group();
        entry.node.add(entry.white);
        entry.node.add(entry.ink);
        scene.add(entry.node);
        craft.set(aircraft.callsign, entry);
      }
      entry.data = aircraft;
      entry.at = now;
    }
    for (const [callsign, entry] of craft) {
      if (seen.has(callsign)) continue;
      scene.remove(entry.node);
      craft.delete(callsign);
      const label = labels.get(callsign);
      if (label) { label.remove(); labels.delete(callsign); }
    }

    if (ownship) {
      if (!own) {
        own = { node: model(kindOf(ownship), mat("ink", INK, { roughness: 0.6 })),
                pos: null, heading: ownship.heading || 0 };
        const ring = new THREE.Mesh(new THREE.RingGeometry(1, 1.25, 48),
                                    new THREE.MeshBasicMaterial({
                                      color: INK, transparent: true, opacity: 0.35,
                                      side: THREE.DoubleSide }));
        ring.rotation.x = -Math.PI / 2;
        own.ring = ring;
        scene.add(own.node);
        scene.add(ring);
      }
      own.data = ownship;
      own.at = now;
    }
    rebuildPaths();
  }

  /** Where an aeroplane is by now, from where it was and how it was going. */
  function predicted(data, since) {
    const t = Math.min(4, Math.max(0, since / 1000));
    const speed = (data.ground_speed_kt || 0) * NM / 3600;
    const heading = (data.heading || 0) * RAD;
    const base = local(data.lat, data.lon,
                       data.on_ground ? null : data.altitude_ft);
    base.x += Math.sin(heading) * speed * t;
    base.z -= Math.cos(heading) * speed * t;
    if (!data.on_ground) {
      base.y = Math.max(0, base.y + (data.vertical_rate_fpm || 0) * FT / 60 * t);
    }
    return base;
  }

  function place(entry, now, scale) {
    const want = predicted(entry.data, now - entry.at);
    if (!entry.pos || !entry.placed || entry.pos.distanceTo(want) > 400) {
      entry.pos = want.clone();
      entry.placed = true;
    } else {
      entry.pos.lerp(want, 0.18);
    }
    const target = entry.data.heading || 0;
    const turn = ((target - entry.heading + 540) % 360) - 180;
    entry.heading = (entry.heading + turn * 0.12 + 360) % 360;
    entry.node.position.copy(entry.pos);
    entry.node.rotation.set(0, -entry.heading * RAD, 0, "YXZ");
    if (!entry.data.on_ground) {
      // A little of what it is doing: nose up in the climb, a bank in the
      // turn. Enough to read, not enough to be a flight model.
      const climb = Math.max(-6, Math.min(8, (entry.data.vertical_rate_fpm || 0) / 250));
      entry.node.rotation.x = climb * RAD;
      entry.node.rotation.z = Math.max(-25, Math.min(25, -turn * 1.5)) * RAD;
    }
    entry.node.scale.setScalar(scale);
  }

  /* --------------------------------------------------------------------
   * where they are going
   * ------------------------------------------------------------------ */

  function rebuildPaths() {
    if (!pathsGroup) return;
    for (const child of [...pathsGroup.children]) {
      pathsGroup.remove(child);
      if (child.geometry) child.geometry.dispose();
    }
    const view = getView();
    const span = view ? view.spanNm * NM : 8000;
    const width = Math.max(1.6, span / 700);
    const chosen = getSelected();
    for (const [callsign, entry] of craft) {
      const path = entry.data.path || [];
      if (path.length < 2) continue;
      const mine = callsign === chosen;
      ribbon(path, mine ? width * 1.6 : width,
             mine ? INK : GREY, mine ? 1 : 0.55, mine);
      if (mine) {
        for (const waypoint of entry.data.waypoints || []) {
          diamond(local(waypoint.lat, waypoint.lon), width * 3.2);
        }
      }
    }
    if (own && own.data && own.data.destination_lat != null) {
      const from = [own.data.lat, own.data.lon, own.data.altitude_ft];
      const to = [own.data.destination_lat, own.data.destination_lon,
                  own.data.altitude_ft];
      ribbon([from, towards(from, to, 60)], width * 1.3, INK, 0.9, true);
    }
  }

  /** At most so many miles of a great circle, for a line that is far away. */
  function towards(from, to, maxNm) {
    const a = local(from[0], from[1]);
    const b = local(to[0], to[1]);
    const d = a.distanceTo(b);
    const limit = maxNm * NM;
    if (d <= limit) return to;
    const p = a.clone().lerp(b, limit / d);
    const ll = toLatLon(p.x, p.z);
    return [ll.lat, ll.lon, to[2]];
  }

  /**
   * The rest of a route, as a band laid along it: on the ground where it
   * is on the ground, in the air where it is in the air, with its shadow
   * under it so the height reads.
   */
  function ribbon(path, width, color, opacity, shadowed) {
    const points = path.map(([lat, lon, alt]) => {
      const p = local(lat, lon, alt);
      p.y = Math.max(0.6, p.y);
      return p;
    });
    const material = new THREE.MeshBasicMaterial({
      color, transparent: opacity < 1, opacity, depthWrite: opacity >= 1 });
    const curve = new THREE.CatmullRomCurve3(points, false, "centripetal", 0.1);
    const segments = Math.min(400, Math.max(16, points.length * 12));
    const tube = new THREE.Mesh(
      new THREE.TubeGeometry(curve, segments, width / 2, 6, false), material);
    pathsGroup.add(tube);
    if (shadowed && points.some((p) => p.y > 5)) {
      const flat = points.map((p) => new THREE.Vector3(p.x, 0.5, p.z));
      const shade = new THREE.Mesh(
        new THREE.TubeGeometry(new THREE.CatmullRomCurve3(flat, false,
                                                          "centripetal", 0.1),
                               segments, width * 0.45, 4, false),
        new THREE.MeshBasicMaterial({ color: INK, transparent: true,
                                      opacity: 0.12, depthWrite: false }));
      pathsGroup.add(shade);
    }
  }

  function diamond(at, size) {
    const mesh = new THREE.Mesh(
      new THREE.OctahedronGeometry(size / 2, 0),
      new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.6 }));
    mesh.position.set(at.x, Math.max(at.y, 0) + size / 2 + 0.5, at.z);
    const edge = new THREE.LineSegments(
      new THREE.EdgesGeometry(mesh.geometry),
      new THREE.LineBasicMaterial({ color: INK }));
    mesh.add(edge);
    pathsGroup.add(mesh);
  }

  /* --------------------------------------------------------------------
   * the camera
   * ------------------------------------------------------------------ */

  function aim() {
    const view = getView();
    if (!view || !view.ready) return null;
    const box = canvas.getBoundingClientRect();
    const width = Math.max(40, box.width);
    const height = Math.max(40, box.height);
    if (canvas.width !== Math.round(width * renderer.getPixelRatio())
        || canvas.height !== Math.round(height * renderer.getPixelRatio())) {
      renderer.setSize(width, height, false);
    }
    camera.aspect = width / height;
    if (!origin.set) setOrigin(view.lat, view.lon, 0);
    // Following, the camera rides on the aeroplane as it is drawn, which
    // moves every frame; the chart's centre only moves once a second.
    const followed = craft.get(getFollowed());
    const target = followed && followed.pos
      ? new THREE.Vector3(followed.pos.x, 0, followed.pos.z)
      : view.follow && own && own.pos
        ? new THREE.Vector3(own.pos.x, 0, own.pos.z)
        : local(view.lat, view.lon);
    const span = view.spanNm * NM;
    const hfov = 2 * Math.atan(Math.tan(camera.fov * RAD / 2) * camera.aspect);
    const distance = span / (2 * Math.tan(hfov / 2));
    const pitch = orbit.pitch * RAD;
    const bearing = orbit.bearing * RAD;
    camera.position.set(
      target.x - Math.sin(bearing) * Math.sin(pitch) * distance,
      Math.cos(pitch) * distance,
      target.z + Math.cos(bearing) * Math.sin(pitch) * distance);
    camera.up.set(0, 1, 0);
    camera.lookAt(target);
    camera.far = distance * 12;
    // As far out as it can be: the camera is never nearer than a few
    // hundred metres, and the depth buffer's precision is spent between
    // near and far.
    camera.near = Math.max(2, distance / 40);
    camera.updateProjectionMatrix();
    scene.fog.near = distance * 2.2;
    scene.fog.far = distance * 7;

    // The sun, from the south-west and fairly low, so a building throws a
    // shadow as long as it is tall -- which is most of what makes a white
    // model read -- and sized to what is on screen so the edges stay crisp.
    const reach = span * 0.9;
    sun.position.set(target.x - reach * 0.8, reach * 0.75, target.z + reach * 0.55);
    sun.target.position.copy(target);
    const cam = sun.shadow.camera;
    cam.left = -reach; cam.right = reach; cam.top = reach; cam.bottom = -reach;
    cam.near = 1; cam.far = reach * 4;
    cam.updateProjectionMatrix();
    return { target, span, distance };
  }

  /* --------------------------------------------------------------------
   * drawing
   * ------------------------------------------------------------------ */

  function draw() {
    frame = 0;
    if (!active) return;
    const aimed = aim();
    if (aimed) {
      const now = performance.now();
      // Aeroplanes are drawn a little larger than life when the view is
      // wide, or a mile of airport would show them as specks.
      const scale = Math.min(9, Math.max(1, aimed.span / 1300));
      const chosen = getSelected();
      for (const [callsign, entry] of craft) {
        place(entry, now, scale);
        const mine = callsign === chosen;
        entry.white.visible = !mine;
        entry.ink.visible = mine;
      }
      if (own && own.data) {
        place(own, now, scale);
        const pulse = (now / 1600) % 1;
        own.ring.position.set(own.pos.x, 0.7, own.pos.z);
        own.ring.scale.setScalar((18 + pulse * 30) * scale);
        own.ring.material.opacity = 0.35 * (1 - pulse);
      }
      renderer.render(scene, camera);
      placeLabels(chosen);
    }
    frame = requestAnimationFrame(draw);
  }

  function placeLabels(chosen) {
    const box = canvas.getBoundingClientRect();
    const keep = new Set();
    const entries = [...craft.entries()];
    for (const [callsign, entry] of entries) {
      if (!entry.pos) continue;
      const p = entry.pos.clone();
      p.y += 30 * entry.node.scale.x;
      const v = p.project(camera);
      if (v.z > 1 || Math.abs(v.x) > 1.05 || Math.abs(v.y) > 1.05) continue;
      keep.add(callsign);
      let label = labels.get(callsign);
      if (!label) {
        label = document.createElement("button");
        label.className = "map3d-label";
        label.addEventListener("click", (event) => {
          event.stopPropagation();
          onPick(callsign);
        });
        labelsLayer.appendChild(label);
        labels.set(callsign, label);
      }
      const data = entry.data;
      const level = data.on_ground
        ? (data.runway && /takeoff|lineup/.test(data.phase)
          ? data.runway : "GND")
        : levelText(data.altitude_ft);
      label.textContent = `${callsign} · ${level}`;
      label.classList.toggle("is-chosen", callsign === chosen);
      label.style.transform = `translate(${((v.x + 1) / 2 * box.width).toFixed(1)}px,`
        + ` ${((1 - v.y) / 2 * box.height).toFixed(1)}px) translate(-50%, -100%)`;
    }
    for (const [callsign, label] of labels) {
      label.hidden = !keep.has(callsign);
    }
  }

  function levelText(feet) {
    const rounded = Math.round((feet || 0) / 100) * 100;
    if (rounded >= 18000) return `FL${String(Math.round(rounded / 100)).padStart(3, "0")}`;
    return `${rounded.toLocaleString()} ft`;
  }

  function show(on) {
    if (!ok) return;
    if (on === active) return;
    active = on;
    canvas.hidden = !on;
    labelsLayer.hidden = !on;
    if (on && !frame) frame = requestAnimationFrame(draw);
    if (!on && frame) { cancelAnimationFrame(frame); frame = 0; }
  }

  /* --------------------------------------------------------------------
   * the hand
   * ------------------------------------------------------------------ */

  /** Which aeroplane is under a point on the screen, if any. */
  function pick(clientX, clientY) {
    if (!ok || !active) return "";
    const box = canvas.getBoundingClientRect();
    const pointer = new THREE.Vector2(
      ((clientX - box.left) / box.width) * 2 - 1,
      -((clientY - box.top) / box.height) * 2 + 1);
    const ray = new THREE.Raycaster();
    ray.setFromCamera(pointer, camera);
    let best = "";
    let bestDistance = Infinity;
    for (const [callsign, entry] of craft) {
      if (!entry.pos) continue;
      // Generous: an aeroplane is small, and a pick is a finger.
      const reach = 30 * entry.node.scale.x;
      const distance = ray.ray.distanceToPoint(entry.pos);
      if (distance < reach && distance < bestDistance) {
        best = callsign;
        bestDistance = distance;
      }
    }
    return best;
  }

  /** A drag in screen pixels, as a move of the centre on the ground. */
  function panPixels(dx, dy) {
    const view = getView();
    if (!view) return null;
    const box = canvas.getBoundingClientRect();
    const metresPerPixel = view.spanNm * NM / Math.max(40, box.width);
    const bearing = orbit.bearing * RAD;
    // Screen right is the camera's right, (cos b, -sin b) in east and north;
    // screen up is where it faces, (sin b, cos b), stretched by the tilt
    // because a pixel up the screen covers more ground than one across it.
    // The map follows the hand, so the centre moves the other way.
    const stretch = 1 / Math.max(0.35, Math.cos(orbit.pitch * RAD));
    const east = -dx * Math.cos(bearing) + dy * Math.sin(bearing) * stretch;
    const north = dx * Math.sin(bearing) + dy * Math.cos(bearing) * stretch;
    const cos = Math.cos(view.lat * RAD);
    return {
      lat: view.lat - (north * metresPerPixel) / 110540,
      lon: view.lon + (east * metresPerPixel) / (111320 * cos),
    };
  }

  function turn(dx, dy) {
    orbit.bearing = (orbit.bearing + dx * 0.3 + 360) % 360;
    orbit.pitch = Math.max(0, Math.min(72, orbit.pitch - dy * 0.25));
  }

  function resetOrbit() { orbit.bearing = 0; orbit.pitch = 48; }

  return {
    init, available, show, setAirport, setTraffic, rebuildPaths, pick,
    panPixels, turn, resetOrbit,
    get active() { return active; },
    get airport() { return airport; },
    get orbit() { return orbit; },
  };
})();

window.Airspace3D = Airspace3D;
window.dispatchEvent(new Event("airspace3d"));
