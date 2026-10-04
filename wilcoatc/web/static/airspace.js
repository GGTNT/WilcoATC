/*
 * The airspace map.
 *
 * A map of the world that happens to have your aeroplane on it, rather than a
 * radar picture that happens to have some airports around it. That is the
 * whole of the change from what was here before, and everything else follows
 * from it.
 *
 * What was wrong with the old one
 * -------------------------------
 *
 * It drew everything as an offset in nautical miles east and north of the
 * aircraft, computed on the server. North was up, you were in the middle, and
 * that was that: you could not pan away from yourself, you could not look at
 * where you were going, and zooming out past a couple of hundred miles gave
 * you an empty disc with four range rings on it, because there was nothing
 * under any of it. A map you cannot move is a display, and a pilot wants a
 * map.
 *
 * How this one works
 * ------------------
 *
 * Web Mercator, the same projection every aeronautical slippy map uses, with
 * a view defined by a centre in latitude and longitude and a width in
 * nautical miles. Everything drawn is projected through one function, so an
 * aeroplane, an airport, a runway threshold and a national border are all
 * placed by the same arithmetic and agree with each other at every scale.
 *
 * Mercator is chosen rather than tolerated. It is conformal: a circle of
 * airspace coverage is a circle on the paper, a runway's bearing is its real
 * bearing, and a heading you measure off the screen is the heading you would
 * fly. An equal-area projection would draw truer sizes and lie about every
 * angle, which on a chart for flying is the wrong trade.
 *
 * What is drawn depends on how far out you are, which is the other half of
 * the design. The server decides what to send (see ``_MAP_DETAIL`` in
 * server.py); this decides what to draw with it. Wide out: coastlines,
 * national borders, airspace regions, the airports somebody would actually be
 * looking for at that scale, and every aeroplane. Close in: the aerodrome,
 * its runways at their surveyed bearings, who is on frequency, and how far
 * each of them reaches.
 *
 * Nothing on it is invented. There is no terrain, no airway that is not in
 * the database, and no aeroplane that is not one you could talk to. The
 * coastlines are Natural Earth's, which is a survey and not a drawing.
 */

const Airspace = (() => {
  "use strict";

  // The drawing surface, in its own units. The SVG is scaled to whatever the
  // pane happens to be, and the viewBox is recomputed on resize so a wide
  // window shows more map rather than a stretched one.
  let W = 1000;
  let H = 700;

  const svgNS = "http://www.w3.org/2000/svg";

  // The aeroplane, which is the same mark as the application's own.
  const PLANE = "M21.5 15.6v-1.9L13.6 9V3.9a1.6 1.6 0 0 0-3.2 0V9L2.5 13.7v1.9"
    + "l7.9-2.2v4.9l-2.1 1.6v1.6l3.7-1.1 3.7 1.1v-1.6l-2.1-1.6v-4.9l7.9 2.2z";

  const INK = "#141414";
  const LAND = "#ffffff";
  const WATER = "#dfe4e9";
  const COAST = "#9aa3ac";
  const BORDER = "#c2c8cf";
  const REGION = "#a9b1b8";

  // The view: where the middle of the map is, and how big it is drawn.
  //
  // The size is held as ``k`` -- pixels per degree of longitude -- and not as
  // a width in nautical miles, and that is not a detail. A degree of
  // longitude is sixty miles at the equator and sixty times the cosine of the
  // latitude anywhere else, so holding the miles fixed means the drawing
  // scale has to change every time you pan north or south. It did, and the
  // map visibly zoomed as you dragged it: crossing Europe from forty to sixty
  // degrees north rescaled everything by a third, for no reason the person
  // dragging could see.
  //
  // Holding ``k`` fixed instead is what every slippy map does. Panning is
  // pure translation, the coastline stays the size it was, and the width in
  // miles -- which really does change with latitude on a Mercator -- is
  // derived for the scale bar rather than driving the projection.
  const view = {
    lat: 0,
    lon: 0,
    k: 0,                       // pixels per degree of longitude
    // Whether the map keeps itself centred on the aeroplane. Dragging turns
    // it off, because a map that snaps back three times a second is a map you
    // cannot read; the button turns it on again.
    follow: true,
    ready: false,

    /** How much world is across the window, in nautical miles. */
    get spanNm() {
      return (W * 60 * cosLat(this.lat)) / Math.max(1e-9, this.k);
    },
    set spanNm(nm) {
      const wanted = Math.max(MIN_SPAN_NM, Math.min(MAX_SPAN_NM, Number(nm)));
      this.k = (W * 60 * cosLat(this.lat)) / wanted;
    },
  };

  /** The cosine of a latitude, floored so the poles cannot divide by zero. */
  function cosLat(lat) {
    return Math.max(0.02, Math.cos(lat * RAD));
  }

  // How far out you can go, and how far in. The wide end is the whole world;
  // the close end is a quarter of a mile across, which in the 3D model is a
  // few stands and the aeroplanes on them -- close enough to see one push
  // back.
  const MIN_SPAN_NM = 0.25;
  const MAX_SPAN_NM = 12000;

  // What a map opens at, until somebody zooms.
  const DEFAULT_SPAN_NM = 40;

  let selected = "";
  // The coastlines: an index of what exists, and each level's rings once
  // somebody has zoomed to where they are needed.
  let world = null;
  let worldPending = null;
  const levels = new Map();
  const levelPending = new Map();

  /* --------------------------------------------------------------------
   * the projection
   * ------------------------------------------------------------------ */

  const RAD = Math.PI / 180;
  const DEG = 180 / Math.PI;

  /** Mercator northing, in the same units as longitude. */
  function mercator(lat) {
    const bounded = Math.max(-85.05, Math.min(85.05, lat));
    return Math.log(Math.tan(Math.PI / 4 + (bounded * RAD) / 2)) * DEG;
  }

  /** And back again, for turning a drag into a latitude. */
  function unmercator(y) {
    return (2 * Math.atan(Math.exp(y * RAD)) - Math.PI / 2) * DEG;
  }

  /** Pixels per degree of longitude: the drawing scale, held across a pan. */
  function scale() {
    return Math.max(1e-9, view.k);
  }

  /** A longitude difference, taken the short way round the world. */
  function wrapLon(delta) {
    let d = delta;
    while (d > 180) d -= 360;
    while (d < -180) d += 360;
    return d;
  }

  function project(lat, lon, k, midY) {
    return [
      W / 2 + wrapLon(lon - view.lon) * k,
      H / 2 - (mercator(lat) - midY) * k,
    ];
  }

  /** The screen back to the world, for zooming about the pointer. */
  function unproject(x, y, k, midY) {
    return [
      unmercator(midY + (H / 2 - y) / k),
      view.lon + (x - W / 2) / k,
    ];
  }

  /** The rectangle of the world currently on screen, with a margin. */
  function bounds(margin = 0.12) {
    const k = scale();
    const midY = mercator(view.lat);
    const halfLon = ((W / 2) / k) * (1 + margin);
    const halfY = ((H / 2) / k) * (1 + margin);
    return {
      north: Math.min(85, unmercator(midY + halfY)),
      south: Math.max(-85, unmercator(midY - halfY)),
      west: view.lon - halfLon,
      east: view.lon + halfLon,
      // The view's own width, not the fetched one: the server picks how much
      // detail to send from this, and a margin fetched so that a drag has
      // something to reveal must not also make the map coarser.
      spanNm: view.spanNm * 1.12,
    };
  }

  /* --------------------------------------------------------------------
   * moving about
   * ------------------------------------------------------------------ */

  function setCentre(lat, lon) {
    view.lat = Math.max(-85, Math.min(85, lat));
    view.lon = ((lon + 540) % 360) - 180;
    // The first position is also when the scale gets a value: it cannot be
    // set before there is a latitude, because how many miles a degree of
    // longitude is worth depends on one.
    if (!(view.k > 0)) view.spanNm = DEFAULT_SPAN_NM;
    view.ready = true;
  }

  /** Drag the map by a number of pixels. */
  function panBy(dx, dy) {
    const k = scale();
    const midY = mercator(view.lat);
    setCentre(unmercator(midY + dy / k), view.lon - dx / k);
    view.follow = false;
  }

  /**
   * Zoom, keeping the point under the pointer where it is.
   *
   * Anchoring matters more than it sounds. Zooming about the middle means
   * that reaching a specific airport is pan, zoom, pan, zoom; anchoring on
   * the pointer means you put the cursor on the place you want and turn the
   * wheel, which is one gesture.
   */
  function zoomAt(factor, x, y) {
    const k = scale();
    const midY = mercator(view.lat);
    // While the map is following the aeroplane the aeroplane is the anchor.
    // Anchoring on the pointer used to switch following off, so zooming in
    // on yourself left you behind a moment later -- the one thing somebody
    // zooming in on themselves never wants.
    const anchor = (x === undefined || y === undefined || view.follow)
      ? null : unproject(x, y, k, midY);

    // Clamped in miles rather than in k, because miles across is what a
    // person is choosing when they turn the wheel.
    view.spanNm = view.spanNm * factor;
    if (!anchor) return;

    // Put the anchor back under the pointer at the new scale. panBy takes a
    // drag, so the anchor is dragged from where it now is to the pointer --
    // both ways round. The vertical used to be the other way up, and every
    // zoom slid the map up or down by twice what it meant to correct: the
    // place under the pointer ran off the screen as you zoomed in on it.
    const k2 = scale();
    const midY2 = mercator(view.lat);
    const [nx, ny] = project(anchor[0], anchor[1], k2, midY2);
    panBy(x - nx, y - ny);
    view.follow = false;
  }

  function follow(on) {
    view.follow = on === undefined ? !view.follow : Boolean(on);
    return view.follow;
  }

  /* --------------------------------------------------------------------
   * the world under everything
   * ------------------------------------------------------------------ */

  /**
   * Load the coastlines.
   *
   * Committed with the program rather than fetched from a tile server, for
   * the same reason the nav database is: this has to work on a machine with
   * no internet, next to a simulator that also has none.
   *
   * Fetched a level at a time. Together they are six megabytes and the
   * whole-world outline is ninety kilobytes of that, so making somebody wait
   * for all of it before they can see the airport they are parked at would
   * be paying for detail at exactly the zoom where none of it is visible.
   * The index is small and comes first; a level arrives when a zoom asks for
   * it, and the map draws the best one it already has in the meantime.
   */
  function loadWorld() {
    if (world || worldPending) return worldPending;
    // The index is fetched past the cache. It is a few hundred bytes, it
    // names the files everything else is fetched from, and a stale one is
    // the difference between a map with coastlines and a map without: a
    // browser holding an older build's index asks for files that no longer
    // exist and quietly draws an empty sea. The level files themselves are
    // named per build and cache normally.
    worldPending = fetch("/world.json", { cache: "no-cache" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        world = data;
        // The coarse one always, because it is ninety kilobytes and it is
        // what the map falls back to at every scale until a finer one has
        // arrived.
        loadLevel("coarse");
        return data;
      })
      .catch(() => { world = null; return null; });
    return worldPending;
  }

  /** Fetch one resolution, once. */
  function loadLevel(name) {
    if (levels.has(name) || levelPending.has(name)) return levelPending.get(name);
    const found = world && (world.levels || []).find((l) => l.name === name);
    if (!found) return null;
    if (found.land) {
      // An index that carries the rings inline rather than naming a file.
      // Nothing builds one any more; reading it costs three lines and means
      // an old index is merely old rather than broken.
      levels.set(name, found);
      return null;
    }
    if (!found.file) return null;
    const pending = fetch(`/${found.file}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data) levels.set(name, data);
        levelPending.delete(name);
        return data;
      })
      .catch(() => { levelPending.delete(name); return null; });
    levelPending.set(name, pending);
    return pending;
  }

  // Which resolution suits which zoom. The boundaries are where the coarser
  // outline starts being visibly straighter than the coast it is drawing: at
  // forty miles across, a fifty-million-scale island is a wedge.
  function wantedLevel() {
    if (view.spanNm > 1800) return "coarse";
    if (view.spanNm > 120) return "fine";
    return "detail";
  }

  /**
   * The best outline available for this view.
   *
   * Asks for the one it wants and returns whatever is already here, which is
   * what stops the map going blank while five megabytes of coastline are on
   * their way. A coarser coast for a second is a far better answer than an
   * empty sea.
   */
  function worldLevel() {
    if (!world) return null;
    const wanted = wantedLevel();
    loadLevel(wanted);
    return levels.get(wanted)
      || levels.get("fine")
      || levels.get("coarse")
      || null;
  }

  /**
   * The land, the lakes and the borders.
   *
   * One path element for all of the land rather than one per country: this
   * redraws several times a second beside a running flight simulator, and
   * six hundred nodes in the document is six hundred nodes the browser has
   * to lay out. The rings are already simplified for the scale by the build
   * script, so nothing here has to decide what to throw away.
   */
  function drawWorld(root, k, midY) {
    const level = worldLevel();
    if (!level) return;

    const clip = { x0: -80, x1: W + 80, y0: -80, y1: H + 80 };
    const land = ringsPath(level.land, k, midY, clip);
    if (land) {
      root.appendChild(el("path", {
        d: land, fill: LAND, stroke: COAST, "stroke-width": 0.8,
        // Nonzero rather than even-odd. Adjacent countries share their
        // borders and a few of them genuinely overlap, and even-odd turns
        // any such overlap into a hole -- which is how a continent vanishes
        // because two of its countries disagree about a frontier. The
        // enclaves that even-odd would punch out are filled by their own
        // polygons anyway, so nothing is lost and the failure mode is gone.
        "fill-rule": "nonzero",
      }));
      // The borders again, on top and a shade darker, so a coast and a
      // frontier are distinguishable where they run together.
      root.appendChild(el("path", {
        d: land, fill: "none", stroke: BORDER, "stroke-width": 0.6,
        "stroke-linejoin": "round",
      }));
    }
    const lakes = ringsPath(level.lakes, k, midY, clip);
    if (lakes) {
      root.appendChild(el("path", {
        d: lakes, fill: WATER, stroke: COAST, "stroke-width": 0.5,
      }));
    }
  }

  /**
   * Many rings as one path, with anything off screen left out.
   *
   * Two things here are less obvious than they look.
   *
   * *The ring is unwrapped, not wrapped.* Taking each point's longitude the
   * short way round the world independently is the intuitive thing to do and
   * it is wrong: a country that straddles the antimeridian comes back as a
   * shape three hundred and sixty degrees wide, stretched right across the
   * map. Every point after the first is placed relative to the one before
   * it instead, so a ring stays a continuous outline wherever it sits.
   *
   * *The bounding box is tested per ring, not per point.* A ring that cannot
   * be seen costs two comparisons rather than four hundred, and at the world
   * scale most of them cannot be seen.
   */
  function ringsPath(rings, k, midY, clip) {
    if (!rings || !rings.length) return "";
    const out = [];
    for (const flat of rings) {
      if (flat.length < 6) continue;
      let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
      const points = [];
      let lon = view.lon + wrapLon(flat[0] - view.lon);
      for (let i = 0; i < flat.length; i += 2) {
        if (i) lon += wrapLon(flat[i] - flat[i - 2]);
        const x = W / 2 + (lon - view.lon) * k;
        const y = H / 2 - (mercator(flat[i + 1]) - midY) * k;
        points.push(x, y);
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
      if (maxX < clip.x0 || minX > clip.x1 || maxY < clip.y0 || minY > clip.y1) {
        continue;
      }
      // A country narrower than a pixel is a pixel; drawing its outline is
      // three hundred segments to reach the same result as a dot.
      if (maxX - minX < 1.2 && maxY - minY < 1.2) continue;
      let d = `M${points[0].toFixed(1)} ${points[1].toFixed(1)}`;
      for (let i = 2; i < points.length; i += 2) {
        d += `L${points[i].toFixed(1)} ${points[i + 1].toFixed(1)}`;
      }
      out.push(d + "Z");
    }
    return out.join("");
  }

  /**
   * The airspace regions.
   *
   * These are the flight information regions from the nav database -- the
   * same boundaries that decide which centre you would be talking to. At the
   * scales where individual airports stop being drawable they are the only
   * honest way left to label what you are looking at, and they are why the
   * wide view is a map of airspace rather than a map of the ground.
   */
  function drawRegions(root, regions, k, midY, labels) {
    if (!regions || !regions.length) return;
    const group = el("g", { class: "regions" });
    for (const region of regions) {
      for (const ring of region.rings || []) {
        if (!ring || ring.length < 3) continue;
        let d = "";
        let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
        // Unwrapped point by point, for the same reason the coastlines are:
        // an oceanic region that crosses the antimeridian is otherwise drawn
        // as a band right across the map.
        let lon = view.lon + wrapLon(ring[0][0] - view.lon);
        for (let i = 0; i < ring.length; i += 1) {
          if (i) lon += wrapLon(ring[i][0] - ring[i - 1][0]);
          const x = W / 2 + (lon - view.lon) * k;
          const y = H / 2 - (mercator(ring[i][1]) - midY) * k;
          d += `${d ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`;
          if (x < minX) minX = x;
          if (x > maxX) maxX = x;
          if (y < minY) minY = y;
          if (y > maxY) maxY = y;
        }
        if (maxX < -40 || minX > W + 40 || maxY < -40 || minY > H + 40) continue;
        group.appendChild(el("path", {
          d: d + "Z",
          fill: "none",
          stroke: REGION,
          "stroke-width": region.oceanic ? 0.7 : 1.1,
          "stroke-dasharray": region.oceanic ? "2 4" : "6 4",
          "stroke-opacity": 0.75,
        }));
      }
      const [lx, ly] = project(region.lat, region.lon, k, midY);
      if (region.name && lx > 40 && lx < W - 40 && ly > 20 && ly < H - 20) {
        labels.push({
          x: lx, y: ly, text: region.name.toUpperCase(),
          size: 9, weight: 700, fill: REGION, letter: 0.8, rank: -1,
        });
      }
    }
    root.appendChild(group);
  }

  /* --------------------------------------------------------------------
   * what is on the ground
   * ------------------------------------------------------------------ */

  /**
   * The aerodromes.
   *
   * The symbol says what kind of field it is, which is the thing a pilot is
   * reading the map for: a ring with a bar through it for a towered airport
   * with hard runways, a plain ring for an untowered one, a small dot for
   * anything smaller. Size follows the airport rather than the zoom, so a
   * major international does not shrink to the same mark as a farm strip
   * when you pull back.
   */
  function drawAirports(root, airports, k, midY, labels, detail) {
    if (!airports || !airports.length) return;
    const group = el("g", { class: "airports" });

    for (const airport of airports) {
      const [x, y] = project(airport.lat, airport.lon, k, midY);
      if (x < -30 || x > W + 30 || y < -30 || y > H + 30) continue;

      const big = airport.rank >= 3;
      // The symbol shrinks as the view widens. At forty miles an aerodrome
      // is a place with runways and the chart symbol is worth drawing; at
      // two thousand it is a dot on a continent, and a ring with a bar
      // through it at that scale is a blob that hides the coastline behind
      // it. The rank still sets the relative size, so a major international
      // never becomes the same mark as a farm strip.
      const shrink = view.spanNm > 900 ? 0.62 : (view.spanNm > 300 ? 0.8 : 1);
      const r = (big ? 6 : (airport.rank === 2 ? 4.5 : 3)) * shrink;
      const detailed = shrink === 1;

      const mark = el("g", {
        class: "mark airport",
        "data-airport": airport.ident,
        transform: `translate(${x.toFixed(1)} ${y.toFixed(1)})`,
      });
      if (airport.rank >= 2) {
        mark.appendChild(el("circle", {
          r: r.toFixed(1), fill: airport.towered ? INK : "none",
          stroke: INK, "stroke-width": detailed ? 1.4 : 1,
        }));
        if (big && detailed) {
          // The bar across a large field's ring is the chart convention for
          // one with hard runways in more than one direction.
          mark.appendChild(el("path", {
            d: `M${-r} 0H${r}`, stroke: airport.towered ? "#fff" : INK,
            "stroke-width": 1.6,
          }));
        }
      } else {
        mark.appendChild(el("circle", {
          r: r.toFixed(1), fill: "none", stroke: INK, "stroke-width": 1.1,
          "stroke-opacity": 0.7,
        }));
      }
      group.appendChild(mark);

      // Runways, where they are close enough to be longer than the symbol.
      if (detail && airport.runways && airport.runways.length) {
        for (const runway of airport.runways) {
          const [ax, ay] = project(runway.le_lat, runway.le_lon, k, midY);
          const [bx, by] = project(runway.he_lat, runway.he_lon, k, midY);
          if (Math.hypot(bx - ax, by - ay) < 6) continue;
          group.appendChild(el("path", {
            d: `M${ax.toFixed(1)} ${ay.toFixed(1)}L${bx.toFixed(1)} ${by.toFixed(1)}`,
            stroke: INK,
            "stroke-width": runway.hard ? 3.2 : 2,
            "stroke-linecap": "butt",
            "stroke-opacity": runway.hard ? 0.9 : 0.5,
            "stroke-dasharray": runway.hard ? "" : "5 3",
          }));
        }
      }

      const name = labelFor(airport);
      if (name) {
        labels.push({
          // Below the symbol. The placer grows a box downwards from the
          // anchor and then tries above it, so an anchor set above the mark
          // puts the first attempt straight over the thing it names.
          x, y: y + r + 4, text: name,
          size: big ? 11 : 10,
          weight: big ? 700 : 600,
          fill: INK, rank: airport.rank,
        });
      }
    }
    root.appendChild(group);
  }

  /**
   * What an airport is called on the map, at the space available.
   *
   * The rule is by size and by scale together, and it exists because naming
   * everything names nothing: a forty-mile view of Denver holds sixty
   * aerodromes, most of them private strips, and printing all sixty idents
   * buries the two fields a pilot is looking for under a drift of four-letter
   * codes. So the strips are drawn and not named until you are close enough
   * that there is room, and the large fields keep their names all the way
   * out.
   */
  function labelFor(airport) {
    const span = view.spanNm;
    if (airport.rank >= 3) return airport.iata || airport.ident;
    if (airport.rank >= 2) return span <= 450 ? airport.ident : "";
    if (airport.rank >= 1) return span <= 55 ? airport.ident : "";
    return span <= 15 ? airport.ident : "";
  }

  /**
   * How far each controller can hear you.
   *
   * A hairline, not a wash. Six filled discs over each other used to make a
   * grey fog that everything else had to be found inside; the one you are
   * actually tuned to is the only one drawn boldly, because that is the only
   * one whose edge you care about.
   */
  function drawCoverage(root, stations, k, midY) {
    if (!stations || !stations.length) return;
    const group = el("g", { class: "coverage" });
    const drawn = new Set();
    for (const station of stations) {
      const key = `${station.ident}:${station.position}`;
      if (drawn.has(key)) continue;
      drawn.add(key);
      const [x, y] = project(station.lat, station.lon, k, midY);
      // A range in miles becomes a radius in pixels through the same scale
      // everything else uses, which is what Mercator's conformality buys:
      // the circle stays a circle.
      const cos = Math.max(0.02, Math.cos(station.lat * RAD));
      const r = (station.range_nm / (60 * cos)) * k;
      if (r < 8 || r > W * 4) continue;
      group.appendChild(el("circle", {
        cx: x.toFixed(1), cy: y.toFixed(1), r: r.toFixed(1),
        fill: station.tuned ? INK : "none",
        "fill-opacity": station.tuned ? 0.04 : 0,
        stroke: INK,
        "stroke-width": station.tuned ? 1.4 : 0.6,
        "stroke-opacity": station.tuned ? 0.6 : 0.22,
        "stroke-dasharray": station.tuned ? "" : "3 5",
      }));
    }
    root.appendChild(group);
  }

  /**
   * Who is on frequency, and where.
   *
   * A column of position codes beside the field, with the one you are tuned
   * to filled in. Only close in: at two hundred miles out these are four
   * pieces of text stacked on a dot, and the coverage rings already say
   * everything that is legible at that scale.
   *
   * The positions are ordered the way a flight uses them -- delivery,
   * ground, tower, then the radar positions -- rather than by frequency,
   * because the question being asked is "who do I call next".
   */
  function drawStations(root, stations, k, midY, labels) {
    if (!stations || !stations.length || view.spanNm > 90) return;
    const byAirport = new Map();
    for (const station of stations) {
      if (!byAirport.has(station.ident)) byAirport.set(station.ident, []);
      byAirport.get(station.ident).push(station);
    }
    // Close in, every field's whole list is worth having. Further out only
    // the one you are talking to is, because six of these side by side is a
    // wall of digits with an airport somewhere behind it.
    const many = view.spanNm <= 55;

    for (const [, found] of byAirport) {
      found.sort((a, b) => order(a.position) - order(b.position));
      const [x, y] = project(found[0].lat, found[0].lon, k, midY);
      if (x < -60 || x > W + 60 || y < -60 || y > H + 60) continue;

      const shown = many ? found.slice(0, 6) : found.filter((s) => s.tuned);
      if (!shown.length) continue;
      // One block per field rather than one label per frequency: they belong
      // together, they move together, and the placer has one thing to fit
      // instead of six that can be split up across the map.
      labels.push({
        x, y: y + 10,
        lines: shown.map((s) => `${s.position} ${Number(s.mhz).toFixed(3)}`),
        text: shown.map((s) => `${s.position} ${s.mhz}`).join(" "),
        size: 8.5, weight: 600, fill: INK,
        opacity: shown.some((s) => s.tuned) ? 0.95 : 0.6,
        rank: shown.some((s) => s.tuned) ? 8 : 2,
        box: true,
      });
    }
  }

  // The order a flight meets the positions in, which is the order they are
  // listed in beside a field.
  const POSITIONS = ["ATIS", "AWOS", "DEL", "GND", "TWR", "DEP", "APP", "CTR",
                     "CTAF", "INFO"];

  function order(position) {
    const at = POSITIONS.indexOf(position);
    return at < 0 ? 99 : at;
  }

  /* --------------------------------------------------------------------
   * what is flying
   * ------------------------------------------------------------------ */

  /**
   * The other aeroplanes.
   *
   * Every one of these is an aeroplane you could talk to: it is on the
   * frequency, and where the simulator will have it, it is out of the
   * window. Nothing is added to fill the map up.
   */
  function drawTraffic(root, traffic, k, midY, labels) {
    if (!traffic || !traffic.length) return;
    const group = el("g", { class: "traffic" });
    const roomy = view.spanNm <= 200 && traffic.length <= 30;

    for (const aircraft of traffic) {
      // The server sends every aeroplane on the frequency, because the list
      // beside the map is about you and not about the window. The map is
      // about the window, so it drops the ones that are not in it.
      if (aircraft.in_view === false) continue;
      const [x, y] = project(aircraft.lat, aircraft.lon, k, midY);
      if (x < -20 || x > W + 20 || y < -20 || y > H + 20) continue;
      const chosen = aircraft.callsign === selected;
      const size = aircraft.on_ground ? 11 : 15;

      // Where the chosen one is going: the rest of its route, the same line
      // the 3D model lays on the ground, so zooming out does not lose it.
      if (chosen && aircraft.path && aircraft.path.length > 1) {
        const d = aircraft.path.map(([lat, lon], index) => {
          const [px, py] = project(lat, lon, k, midY);
          return `${index ? "L" : "M"}${px.toFixed(1)} ${py.toFixed(1)}`;
        }).join("");
        group.appendChild(el("path", {
          d, fill: "none", stroke: "#ffffff", "stroke-width": 7,
          "stroke-linecap": "round", "stroke-linejoin": "round",
          "stroke-opacity": 0.9,
        }));
        group.appendChild(el("path", {
          d, fill: "none", stroke: INK, "stroke-width": 3.5,
          "stroke-linecap": "round", "stroke-linejoin": "round",
        }));
      }

      const mark = el("g", {
        class: "mark aircraft",
        "data-callsign": aircraft.callsign,
        tabindex: "0",
      });
      mark.appendChild(plane(x, y, size, aircraft.heading, {
        fill: chosen ? INK : "none",
        stroke: INK,
        "stroke-width": chosen ? 1 : 1.6,
        "stroke-opacity": aircraft.on_ground ? 0.55 : 0.95,
        "stroke-linejoin": "round",
      }));

      // Where it will be in a minute, which turns a heading and a speed into
      // one glance instead of two readings.
      if (!aircraft.on_ground && aircraft.ground_speed_kt > 40) {
        const cos = Math.max(0.02, Math.cos(aircraft.lat * RAD));
        const nm = aircraft.ground_speed_kt / 60;
        const length = (nm / (60 * cos)) * k;
        if (length > 6) {
          const a = (aircraft.heading || 0) * RAD;
          group.appendChild(el("path", {
            d: `M${x.toFixed(1)} ${y.toFixed(1)}`
              + `L${(x + Math.sin(a) * length).toFixed(1)} `
              + `${(y - Math.cos(a) * length).toFixed(1)}`,
            stroke: INK, "stroke-width": 1.1, "stroke-opacity": 0.45,
          }));
        }
      }
      group.appendChild(mark);

      if (roomy || chosen) {
        const lines = [aircraft.callsign];
        if (chosen || view.spanNm <= 60) {
          lines.push(aircraft.on_ground
            ? (aircraft.runway || aircraft.type || "")
            : level(aircraft.altitude_ft));
        }
        if (chosen && aircraft.instruction) lines.push(aircraft.instruction);
        labels.push({
          x, y: y + size * 0.75 + 4, text: lines.filter(Boolean).join("  "),
          size: 9.5, weight: chosen ? 700 : 600, fill: INK,
          rank: chosen ? 9 : 4, box: true,
        });
      }
    }
    root.appendChild(group);
  }

  /**
   * You.
   *
   * Filled, ringed and never dropped for want of space, because the one
   * question a map has to answer without being searched is where you are.
   */
  function drawOwnship(root, ownship, k, midY, labels) {
    if (!ownship) return;
    const [x, y] = project(ownship.lat, ownship.lon, k, midY);
    const group = el("g", { class: "ownship" });

    // Off screen, it becomes an arrow on the edge pointing at itself. A pilot
    // who has panned away should still be able to find the way back.
    if (x < 0 || x > W || y < 0 || y > H) {
      const cx = Math.max(18, Math.min(W - 18, x));
      const cy = Math.max(18, Math.min(H - 18, y));
      const angle = Math.atan2(x - cx, cy - y) * DEG;
      group.appendChild(plane(cx, cy, 15, angle, {
        fill: INK, "fill-opacity": 0.25, stroke: INK, "stroke-width": 1,
      }));
      root.appendChild(group);
      return;
    }

    if (!ownship.on_ground && ownship.ground_speed_kt > 30) {
      const cos = Math.max(0.02, Math.cos(ownship.lat * RAD));
      const length = ((ownship.ground_speed_kt / 60) / (60 * cos)) * k;
      const a = (ownship.track || ownship.heading || 0) * RAD;
      if (length > 6) {
        group.appendChild(el("path", {
          d: `M${x.toFixed(1)} ${y.toFixed(1)}`
            + `L${(x + Math.sin(a) * length).toFixed(1)} `
            + `${(y - Math.cos(a) * length).toFixed(1)}`,
          stroke: INK, "stroke-width": 1.6, "stroke-opacity": 0.6,
        }));
      }
    }
    group.appendChild(el("circle", {
      cx: x.toFixed(1), cy: y.toFixed(1), r: 15,
      fill: "#fff", "fill-opacity": 0.85, stroke: INK,
      "stroke-width": 1.2, "stroke-opacity": 0.35,
    }));
    group.appendChild(plane(x, y, 20, ownship.heading, {
      fill: INK, stroke: INK, "stroke-width": 1, "stroke-linejoin": "round",
    }));
    root.appendChild(group);

    labels.push({
      x, y: y + 20, text: [ownship.callsign || "you",
                           ownship.on_ground ? "" : level(ownship.altitude_ft)]
        .filter(Boolean).join("  "),
      size: 10.5, weight: 800, fill: INK, rank: 10, box: true,
    });
  }

  /* --------------------------------------------------------------------
   * the words
   * ------------------------------------------------------------------ */

  /**
   * Every label, placed so that none is written over another.
   *
   * The map used to drop a label when it collided, which meant the busiest
   * part -- the circuit -- was the part with no names in it. Here the
   * important ones are placed first and the rest give way, so what is lost
   * is the least useful thing on screen rather than whatever happened to be
   * drawn last.
   */
  function drawLabels(root, labels) {
    const taken = [];
    const group = el("g", { class: "labels" });
    labels.sort((a, b) => b.rank - a.rank);

    for (const label of labels) {
      const lines = label.lines || [label.text];
      const longest = lines.reduce((n, l) => Math.max(n, l.length), 0);
      const width = longest * label.size * 0.58 + (label.box ? 8 : 4);
      const lineHeight = label.size * 1.28;
      const height = lines.length * lineHeight + (label.box ? 4 : 2);
      const spot = place(label, width, height, taken);
      if (!spot) continue;
      taken.push(spot);

      if (label.box) {
        group.appendChild(el("rect", {
          x: (spot.x0 - 1).toFixed(1), y: (spot.y0 - 1).toFixed(1),
          width: (width + 2).toFixed(1), height: (height + 2).toFixed(1),
          rx: 3, fill: "#fff", "fill-opacity": 0.84,
        }));
      }
      lines.forEach((line, index) => {
        group.appendChild(el("text", {
          x: ((spot.x0 + spot.x1) / 2).toFixed(1),
          y: (spot.y0 + (index + 1) * lineHeight - label.size * 0.28).toFixed(1),
          "text-anchor": "middle",
          "font-size": label.size,
          "font-weight": label.weight,
          fill: label.fill,
          "fill-opacity": label.opacity === undefined ? 1 : label.opacity,
          "letter-spacing": label.letter || 0,
          // Labels are read, not clicked. Letting one swallow a click on the
          // aeroplane underneath it would make the map feel broken.
          "pointer-events": "none",
        }, line));
      });
    }
    root.appendChild(group);
  }

  // Where a label may go, in order of preference: under the mark, then over
  // it, then to either side. The same order a chart uses.
  const PLACES = [
    [0, 0], [0, -1], [1, -0.4], [-1, -0.4], [1, 0.5], [-1, 0.5],
  ];

  function place(label, width, height, taken) {
    for (const [dx, dy] of PLACES) {
      const x0 = label.x - width / 2 + dx * (width / 2 + 8);
      const y0 = label.y + dy * (height + 6);
      const box = { x0, y0, x1: x0 + width, y1: y0 + height };
      if (box.x0 < 2 || box.x1 > W - 2 || box.y0 < 2 || box.y1 > H - 2) continue;
      if (taken.some((other) => overlaps(box, other))) continue;
      return box;
    }
    return null;
  }

  function overlaps(a, b) {
    return !(a.x1 < b.x0 || a.x0 > b.x1 || a.y1 < b.y0 || a.y0 > b.y1);
  }

  /* --------------------------------------------------------------------
   * the furniture
   * ------------------------------------------------------------------ */

  /**
   * The scale bar.
   *
   * A number in a corner saying "four hundred miles across" is a fact you
   * have to do arithmetic with. A bar is a fact you can hold two fingers
   * against, and on a map you can zoom continuously it is the only honest
   * way to say how big anything is.
   */
  function drawScaleBar(root, k) {
    const cos = Math.max(0.02, Math.cos(view.lat * RAD));
    const nmPerPx = (60 * cos) / k;
    const wanted = nmPerPx * (W * 0.18);
    const nice = [0.25, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000];
    let step = nice[nice.length - 1];
    for (const candidate of nice) {
      if (candidate >= wanted) { step = candidate; break; }
    }
    const length = step / nmPerPx;
    const x = 18;
    // Above the pills in the corner rather than behind them. The overlay is
    // HTML sitting on top of this drawing, so anything put in the bottom
    // fourteen pixels is simply covered up.
    const y = H - 58;
    const bar = el("g", { class: "scalebar", "pointer-events": "none" });
    bar.appendChild(el("path", {
      d: `M${x} ${y - 5}V${y}H${x + length}V${y - 5}`,
      stroke: INK, "stroke-width": 1.6, fill: "none", "stroke-opacity": 0.75,
    }));
    bar.appendChild(el("text", {
      x: x + length / 2, y: y - 8, "text-anchor": "middle",
      "font-size": 10, "font-weight": 700, fill: INK, "fill-opacity": 0.75,
    }, step < 1 ? `${step * 1000 | 0} m` : `${step} nm`));
    root.appendChild(bar);
  }

  /** Which way up the map is, which on this one is always north. */
  function drawCompass(root) {
    const x = W - 34;
    const y = 34;
    const rose = el("g", { class: "compass", "pointer-events": "none" });
    rose.appendChild(el("circle", {
      cx: x, cy: y, r: 17, fill: "#fff", "fill-opacity": 0.7,
      stroke: INK, "stroke-width": 0.8, "stroke-opacity": 0.3,
    }));
    rose.appendChild(el("path", {
      d: `M${x} ${y - 12}L${x + 5} ${y + 5}L${x} ${y + 1}L${x - 5} ${y + 5}Z`,
      fill: INK, "fill-opacity": 0.8,
    }));
    rose.appendChild(el("text", {
      x, y: y + 15, "text-anchor": "middle", "font-size": 8,
      "font-weight": 800, fill: INK, "fill-opacity": 0.6,
    }, "N"));
    root.appendChild(rose);
  }

  function drawNothing(root, message) {
    root.appendChild(el("text", {
      x: W / 2, y: H / 2, "text-anchor": "middle",
      "font-size": 13, "font-weight": 600, fill: INK, "fill-opacity": 0.4,
    }, message));
  }

  /* --------------------------------------------------------------------
   * one frame
   * ------------------------------------------------------------------ */

  /**
   * Draw the map.
   *
   * ``layers`` is what the chips at the top of the card have switched on.
   * The order is the order a chart is printed in: the ground, the airspace
   * over it, what is built on it, what is flying above that, you on top of
   * everything, and the names last so that nothing is written over.
   */
  function render(svg, data, layers, options = {}) {
    resize(svg);
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.replaceChildren();

    const ownship = data && data.ownship;
    if (view.follow && ownship) setCentre(ownship.lat, ownship.lon);
    if (!view.ready) {
      drawNothing(svg, "no aircraft position yet");
      return;
    }
    // The 3D model is drawing this view. The chart keeps the view and the
    // gestures and draws nothing, so the two never show different things.
    if (options.hidden) return;

    const k = scale();
    const midY = mercator(view.lat);
    const labels = [];

    svg.appendChild(el("rect", { x: 0, y: 0, width: W, height: H, fill: WATER }));
    drawWorld(svg, k, midY);
    if (layers.has("rings")) {
      drawRegions(svg, (data && data.boundaries) || [], k, midY, labels);
      drawCoverage(svg, (data && data.stations) || [], k, midY);
    }
    if (layers.has("airports")) {
      drawAirports(svg, (data && data.airports) || [], k, midY, labels,
                   view.spanNm <= 80);
    }
    if (layers.has("stations")) {
      drawStations(svg, (data && data.stations) || [], k, midY, labels);
    }
    if (layers.has("traffic")) {
      drawTraffic(svg, (data && data.traffic) || [], k, midY, labels);
    }
    drawOwnship(svg, ownship, k, midY, labels);
    drawLabels(svg, labels);
    drawScaleBar(svg, k);
    drawCompass(svg);
  }

  /**
   * Match the drawing units to the pane.
   *
   * Without this a wide window stretches the map, and a stretched Mercator is
   * no longer conformal -- which is the one thing it was chosen for.
   */
  function resize(svg) {
    const box = svg.getBoundingClientRect();
    if (box.width > 40 && box.height > 40) {
      W = Math.round(box.width);
      H = Math.round(box.height);
    }
  }

  /* --------------------------------------------------------------------
   * bits and pieces
   * ------------------------------------------------------------------ */

  function el(name, attrs, text) {
    const node = document.createElementNS(svgNS, name);
    for (const [k, v] of Object.entries(attrs || {})) {
      node.setAttribute(k, v);
    }
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /**
   * The aeroplane glyph, centred on a point and turned to a heading.
   *
   * Scaled about its own centre and then rotated, which keeps the mark on the
   * position rather than swinging it around one.
   */
  function plane(x, y, size, heading, attrs) {
    const s = size / 24;
    const node = el("g", {
      transform: `translate(${x.toFixed(1)} ${y.toFixed(1)}) `
        + `rotate(${(heading || 0).toFixed(0)}) scale(${s.toFixed(3)}) `
        + "translate(-12 -12)",
    });
    node.appendChild(el("path", { d: PLANE, ...attrs }));
    return node;
  }

  function level(altitudeFt) {
    const feet = Math.round((altitudeFt || 0) / 100) * 100;
    if (feet >= 18000) return `FL${String(Math.round(feet / 100)).padStart(3, "0")}`;
    return `${feet.toLocaleString()} ft`;
  }

  /** Which aeroplane is selected, and choosing one. */
  function selection() { return selected; }

  function select(callsign) {
    selected = selected === callsign ? "" : (callsign || "");
    return selected;
  }

  /** Whether a finer outline is still on its way, so the caller can redraw. */
  function loading() { return levelPending.size > 0; }

  return {
    render, resize, loadWorld, loading,
    bounds, panBy, zoomAt, setCentre, follow,
    selection, select,
    get view() { return view; },
    MIN_SPAN_NM, MAX_SPAN_NM,
  };
})();
