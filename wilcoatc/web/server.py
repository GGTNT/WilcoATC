"""The graphical interface, served locally.

A browser page rather than a desktop window, for two reasons that matter when
flying: the simulator is usually fullscreen on the main monitor, and a browser
page can live on a second screen, a tablet on the same network, or a phone
propped next to the yoke.

The audio devices stay on the machine running the engine. When the page's
push-to-talk is pressed it keys *that* microphone, exactly as the physical key
does, so both work at once and neither is special.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..atc.language import LANGUAGE_NAMES, languages_for
from ..atc.written import as_written
from ..navdata.channels import channel_name, same_channel
from ..navdata.db import STATION_RANGE_NM
from ..audio.announcements import EVENTS as ANNOUNCEMENT_EVENTS
from ..engine import Engine, Event, announcement_roots
from ..health import report as health_report
from ..install import Installer, SIZES_MB as SETUP_STEPS
from ..logs import METRICS, RING, SESSION_ID, diagnostics

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"


class PanelFiles(StaticFiles):
    """The panel's own files, always revalidated.

    Every one of these changes when the program is updated, and they are
    served over loopback where a conditional request costs nothing. The
    default heuristic freshness is what makes an updated WilcoATC open with
    the previous version's interface -- a new engine talking to an old page,
    which is worse than either on its own, and which the pilot can only fix by
    knowing to press ctrl-shift-R.

    ``no-cache`` is not ``no-store``: the browser keeps the file and asks
    whether it has changed. Unchanged is a 304 and no bytes.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["cache-control"] = "no-cache"
        return response


class Broadcaster:
    """Fans engine events out to every connected browser.

    The engine runs on its own threads and knows nothing about asyncio, so
    events are handed across with ``call_soon_threadsafe`` and buffered. A page
    that connects late is sent the backlog, so opening the interface
    mid-flight shows the conversation so far rather than an empty screen.
    """

    def __init__(self, history: int = 300):
        self.clients: set[WebSocket] = set()
        self.history: deque[dict] = deque(maxlen=history)
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def publish(self, payload: dict) -> None:
        """Called from any thread."""
        with self._lock:
            self.history.append(payload)
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(asyncio.create_task, self._send(payload))
        except RuntimeError:
            pass

    async def _send(self, payload: dict) -> None:
        dead = []
        for client in list(self.clients):
            try:
                await client.send_json(payload)
            except Exception:
                dead.append(client)
        for client in dead:
            self.clients.discard(client)

    def backlog(self) -> list[dict]:
        with self._lock:
            return list(self.history)


def create_app(engine: Engine, broadcaster: Broadcaster) -> FastAPI:
    app = FastAPI(title="wilcoatc", docs_url=None, redoc_url=None)

    # The panel inside the simulator is served by the simulator, from a
    # coui:// address, and every request it makes here is therefore a
    # cross-origin one. Without this it is refused by the browser engine
    # before it reaches the application, and the panel reports that WilcoATC
    # is not running while WilcoATC is running.
    #
    # Opening this up costs nothing that was not already open: the server is
    # bound to the loopback interface and nothing off this machine can reach
    # it to be allowed anything.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    async def _startup() -> None:
        broadcaster.bind(asyncio.get_running_loop())
        # Push the aircraft state a few times a second so the panel stays live
        # without the page polling for it.
        asyncio.create_task(_state_pump(engine, broadcaster))

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------

    @app.get("/api/state")
    async def state() -> dict:
        return snapshot(engine)

    @app.get("/api/frequencies")
    async def frequencies(ident: str = "") -> dict:
        """Stations at an airport, or everything within range of the aircraft."""
        db = engine.navdb
        if ident:
            airport = db.airport(ident)
            if airport is None:
                return {"airport": ident, "stations": []}
            stations = db.stations(airport.ident)
            name = airport.name
        else:
            position = engine.state
            if not position.has_position:
                return {"airport": "", "stations": []}
            stations = db.all_stations_near(
                position.latitude, position.longitude, max_nm=40
            )
            nearest = db.home_airport(position.latitude, position.longitude)
            if nearest is None:
                nearest = db.nearest_airport(position.latitude, position.longitude,
                                             max_nm=40)
            name = nearest.spoken if nearest else ""

        order = ["ATIS", "AWOS", "DEL", "GND", "TWR", "DEP", "APP", "CTR",
                 "CTAF", "INFO"]
        rows = [
            {
                # The channel as a radio selects it, which is what the charts
                # and the simulator show. Tuning it works either way: the
                # match is made on the carrier, not on the digits.
                "mhz": channel_name(station.mhz),
                "carrier": round(station.mhz, 3),
                "position": station.position,
                "callsign": station.callsign,
                "ident": station.ident,
                "distance_nm": round(station.distance_nm, 1),
            }
            # Nearest field first: the airport you are standing on matters
            # more than one nine miles away that happens to publish a tower
            # frequency alphabetically earlier.
            for station in sorted(
                stations,
                key=lambda s: (round(s.distance_nm),
                               order.index(s.position) if s.position in order else 99,
                               s.priority, s.mhz),
            )
        ]
        return {"airport": name, "stations": rows[:60]}

    @app.get("/api/airspace")
    async def airspace(range_nm: float = 40.0) -> dict:
        """What is around the aircraft, for the map.

        The fields within range, who is on frequency at each, how far their
        coverage reaches -- and the other aeroplanes, which are the same ones
        that are talking on the radio and, when the simulator will have them,
        the same ones that are out of the window. Nothing here is invented for
        the map alone.

        Positions come back as offsets in nautical miles east and north of the
        aircraft, so the page can plot them without doing spherical geometry.
        """
        state = engine.state
        if not state.has_position:
            return {"range_nm": range_nm, "centre": None,
                    "airports": [], "stations": [], "traffic": []}

        db = engine.navdb
        reach = max(5.0, min(float(range_nm), 400.0))
        lat, lon = state.latitude, state.longitude

        def offsets(to_lat: float, to_lon: float) -> dict:
            from ..navdata.db import bearing_deg, haversine_nm
            import math

            distance = haversine_nm(lat, lon, to_lat, to_lon)
            bearing = bearing_deg(lat, lon, to_lat, to_lon)
            return {
                "distance_nm": round(distance, 1),
                "bearing": round(bearing),
                "east_nm": round(distance * math.sin(math.radians(bearing)), 2),
                "north_nm": round(distance * math.cos(math.radians(bearing)), 2),
            }

        home = db.home_airport(lat, lon)
        airports = []
        stations = []
        drawn_runways = 0
        for airport, _distance in db.nearest_airports(lat, lon, limit=40, max_nm=reach):
            where = offsets(airport.lat, airport.lon)
            # Close enough that the runways are worth drawing to scale. The
            # map used to draw the same schematic cross at every field, which
            # said an aerodrome was there and nothing else. Real geometry says
            # which way the traffic is pointing, which is most of what a
            # pilot is looking at the map for.
            runways = []
            if (where["distance_nm"] <= RUNWAY_DETAIL_NM
                    and drawn_runways < RUNWAY_DETAIL_FIELDS):
                runways = _runway_shapes(db, airport.ident, offsets)
                if runways:
                    drawn_runways += 1
            airports.append({
                "ident": airport.ident,
                "name": airport.name,
                "spoken": airport.spoken,
                "towered": airport.is_towered,
                "home": bool(home and home.ident == airport.ident),
                "runways": runways,
                **where,
            })
            for station in db.stations(airport.ident):
                span = STATION_RANGE_NM.get(station.position, 40.0)
                stations.append({
                    "ident": station.ident,
                    "position": station.position,
                    "mhz": channel_name(station.mhz),
                    "carrier": round(station.mhz, 3),
                    "callsign": station.callsign,
                    "range_nm": span,
                    "in_range": where["distance_nm"] <= span,
                    # Compared as channels rather than as numbers, or the two
                    # names for one carrier would look like two stations.
                    "tuned": bool(engine.station
                                  and same_channel(engine.station.mhz,
                                                   station.mhz)
                                  and engine.station.ident == station.ident),
                    **where,
                })

        stations.sort(key=lambda s: (s["distance_nm"], s["position"]))

        # The other aeroplanes, in the same offsets as everything else. Only
        # what is within the drawn range, nearest first, so a busy field does
        # not push the map past what it can show.
        traffic = []
        for flight in engine.traffic.positions():
            where = offsets(flight["lat"], flight["lon"])
            if where["distance_nm"] > reach:
                continue
            traffic.append({**flight, **where,
                            "label": PHASE_LABELS.get(flight["phase"],
                                                      flight["phase"]),
                            "order": _order_view(flight.get("order")),
                            # An observed aeroplane is in the simulator by
                            # definition: it was read out of it.
                            "in_simulator": bool(
                                flight.get("observed")
                                or engine.simtraffic.has(flight["callsign"]))})
        traffic.sort(key=lambda f: f["distance_nm"])

        return {
            "range_nm": reach,
            "centre": {
                "latitude": round(lat, 4),
                "longitude": round(lon, 4),
                "heading": round(state.heading_true),
                "altitude_ft": round(state.altitude_ft),
                # So the map can draw the same trend vector on you that it
                # draws on everybody else.
                "ground_speed_kt": round(state.ground_speed_kt),
                "vertical_rate_fpm": round(state.vertical_speed_fpm),
                "on_ground": state.on_ground,
                "callsign": (engine.session.aircraft.callsign.written
                             if engine.session else ""),
            },
            "airports": airports,
            "stations": stations[:80],
            "traffic": traffic[:40],
        }

    @app.get("/api/map")
    async def geographic_map(
        north: float = 0.0, south: float = 0.0,
        west: float = 0.0, east: float = 0.0,
        span_nm: float = 0.0,
    ) -> dict:
        """What is inside a rectangle of the world, for the geographic map.

        The other map endpoint answers "what is around me", in miles east and
        north of the aeroplane. This one answers "what is inside this
        window", in latitude and longitude -- which is the question a map you
        can pan away from your own aircraft has to be able to ask.

        What comes back depends on how much of the world the window covers,
        and that is the whole design. A view three hundred miles across can
        draw every aerodrome in it; a view three thousand miles across cannot,
        and filling it with forty thousand grey dots would be worse than
        drawing none. So each scale gets what is legible at that scale:
        airspace regions and the major fields when wide, everything down to
        the grass strips and the runway geometry when close.
        """
        db = engine.navdb
        south, north = min(south, north), max(south, north)
        if north == south:
            return {"span_nm": span_nm, "airports": [], "boundaries": [],
                    "traffic": [], "stations": [], "ownship": _ownship(engine)}

        # How much of the world is on screen, which is what everything below
        # is decided by.
        span = span_nm or max(1.0, (north - south) * 60.0)
        detail = _detail_for(span)

        airports = []
        for airport in db.airports_in_box(south, north, west, east,
                                          min_rank=detail["min_rank"],
                                          limit=detail["airports"]):
            entry = {
                "ident": airport.ident,
                "name": airport.name,
                "spoken": airport.spoken,
                "iata": airport.iata,
                "municipality": airport.municipality,
                "lat": round(airport.lat, 5),
                "lon": round(airport.lon, 5),
                "rank": airport.size_rank,
                "scheduled": bool(airport.scheduled),
                "towered": airport.is_towered,
                "runways": [],
            }
            airports.append(entry)

        # Runway geometry, for the few fields close enough that it would be
        # visible. Drawn in real coordinates, so an aeroplane on final is
        # lined up with the strip rather than merely near the airport.
        if detail["runways"]:
            for entry in airports[:detail["runway_fields"]]:
                entry["runways"] = _runway_lines(db, entry["ident"])

        boundaries = []
        if detail["boundaries"]:
            for found in db.boundaries_in_box(south, north, west, east,
                                              limit=detail["boundaries"]):
                boundaries.append({
                    "id": found["id"],
                    "name": found["name"],
                    "oceanic": found["oceanic"],
                    "lat": round(found["lat"], 3),
                    "lon": round(found["lon"], 3),
                    # Thinned to what is drawable at this scale. A region
                    # outline surveyed to a tenth of a mile is the same three
                    # pixels on a map of a continent.
                    "rings": [_thin(ring, detail["thin"])
                              for ring in found["rings"]],
                })

        # Every aeroplane, not only the ones inside the window.
        #
        # These are the aircraft on your frequency: the injector only ever
        # creates them around you, so the whole set is small and it is the
        # answer to "who am I sharing this airspace with". Filtering them by
        # the map window would empty the traffic list the moment you panned
        # away to look at where you were going, which is exactly when you
        # would want to keep an eye on it. The map does its own filtering
        # when it draws.
        traffic = [
            {**flight, "in_view": _inside(flight["lat"], flight["lon"], south,
                                          north, west, east)}
            for flight in _traffic_view(engine)]

        # Who is on frequency at the fields in view, but only where the fields
        # themselves are drawn individually. At a continental scale a station
        # mark is a dot on a dot.
        stations = []
        if detail["stations"]:
            tuned = engine.station
            for entry in airports[:detail["station_fields"]]:
                for station in db.stations(entry["ident"]):
                    stations.append({
                        "ident": station.ident,
                        "position": station.position,
                        "mhz": channel_name(station.mhz),
                        "carrier": round(station.mhz, 3),
                        "callsign": station.callsign,
                        "range_nm": STATION_RANGE_NM.get(station.position, 40.0),
                        "lat": entry["lat"], "lon": entry["lon"],
                        "tuned": bool(tuned
                                      and same_channel(tuned.mhz, station.mhz)
                                      and tuned.ident == station.ident),
                    })

        return {
            "span_nm": round(span, 1),
            "detail": detail["name"],
            "airports": airports,
            "boundaries": boundaries,
            "stations": stations[:200],
            "traffic": traffic[:200],
            "ownship": _ownship(engine),
            # Who can hear you, which is a question about the aeroplane and
            # not about the window. It has to come back whatever the map is
            # looking at: panning away to see where you are going must not
            # empty the list of the people you are talking to.
            "nearby": _within_reach(engine),
        }

    @app.get("/api/live")
    async def live() -> dict:
        """The aeroplanes and you, and nothing else, for the 3D map.

        Asked for every second while the 3D view is open, so it carries only
        what moves. The airport under them is ``/api/scene``, asked once.
        """
        return {"traffic": _traffic_view(engine)[:200],
                "ownship": _ownship(engine)}

    @app.get("/api/scene")
    async def scene(lat: float = 0.0, lon: float = 0.0) -> dict:
        """The airport nearest a point, in three dimensions' worth of detail.

        Its runways, at their surveyed ends and published width; and, where
        the simulator has said, every stand and every taxiway segment -- the
        same network the traffic is driven along. The buildings are not in
        any of it: the panel raises them behind the stands, where terminals
        are, and says nothing about them it does not know.
        """
        db = engine.navdb
        airport = db.home_airport(lat, lon) or db.nearest_airport(
            lat, lon, max_nm=15.0, min_rank=2)
        if airport is None:
            return {"ident": ""}
        runways = []
        for runway in db.runways(airport.ident):
            if runway.closed or None in (runway.le_lat, runway.le_lon,
                                         runway.he_lat, runway.he_lon):
                continue
            runways.append({
                "le_ident": runway.le_ident, "he_ident": runway.he_ident,
                "le_lat": runway.le_lat, "le_lon": runway.le_lon,
                "he_lat": runway.he_lat, "he_lon": runway.he_lon,
                "width_m": round(max(float(runway.width_ft or 0.0), 75.0)
                                 * 0.3048, 1),
                "hard": runway.is_hard,
            })
        layout = engine.ground_layout(airport.ident)
        ground = None
        if layout is not None:
            ground = {"taxiways": [], "stands": []}
            seen = set()
            for a, edges in layout.edges.items():
                for b, _length, is_runway, name in edges:
                    if (b, a) in seen or is_runway:
                        continue
                    seen.add((a, b))
                    la = layout.to_latlon(*layout.xy[a])
                    lb = layout.to_latlon(*layout.xy[b])
                    ground["taxiways"].append([
                        round(la[0], 7), round(la[1], 7),
                        round(lb[0], 7), round(lb[1], 7),
                        1 if (a[0] == "k" or b[0] == "k") else 0, name])
            from ..atc.ground import _RAMPS

            for stand in layout.stands:
                # Aeroplane stands only. Vehicle parking and fuel pads are in
                # the same list, and a terminal raised behind a fuel pad was
                # a building standing on a taxiway.
                if not stand.gate and stand.kind not in _RAMPS:
                    continue
                slat, slon = layout.to_latlon(stand.x, stand.z)
                ground["stands"].append({
                    "lat": round(slat, 7), "lon": round(slon, 7),
                    "heading": round(stand.heading, 1),
                    "radius": stand.radius, "gate": stand.gate,
                    "number": stand.number})
        return {
            "ident": airport.ident, "name": airport.name,
            "lat": airport.lat, "lon": airport.lon,
            "elevation_ft": airport.elev_ft or 0.0,
            "runways": runways, "ground": ground,
        }

    @app.get("/api/atis")
    async def atis_text(ident: str = "", language: str = "") -> dict:
        """The current ATIS as text, without transmitting it.

        The panel shows the wording on the Frequencies screen; playing it is a
        separate action, because reading it and hearing it are different
        things to want.
        """
        target = (ident or "").upper() or None
        if target is None:
            station = engine.station
            target = station.ident if station else ""
        if not target:
            return {"ok": False}
        report = await asyncio.to_thread(engine.atis_for, target, language or None)
        if report is None:
            return {"ok": False, "ident": target}
        return {
            "ok": True,
            "ident": target,
            "letter": report.letter,
            # Read on the screen, so written the way a pilot writes.
            "text": as_written(report.text),
            "runways": list(getattr(report, "landing_runways", []) or []),
        }

    # ------------------------------------------------------------------
    # the log
    # ------------------------------------------------------------------

    @app.get("/api/logs")
    async def logs(limit: int = 200, level: str = "") -> dict:
        """The most recent log records, newest last.

        Read from the ring in memory rather than from the file. The browser
        cannot reach the disk, and giving it a path to read would be a worse
        answer than serving what is already in this process.
        """
        return {
            "session": SESSION_ID,
            "records": RING.tail(min(max(1, limit), 1000), level),
        }

    @app.get("/api/transcript")
    async def transcript(limit: int = 40) -> dict:
        """What has been said on the radio, newest last.

        For the panel inside the simulator, which polls rather than holding a
        connection open. The window on this machine is fed the same
        transmissions as they happen, over the socket, and does not use this.
        """
        kept = list(engine.transcript)[-max(1, min(limit, 200)):]
        return {"transmissions": kept}

    # The installer is per-app rather than per-request: it owns a worker
    # thread and a 1.5 GB download, and two of those must never exist at once.
    # Every change it reports goes out on the same socket the panel is already
    # listening to, so the bar moves without the page asking.
    def _setup_changed(state: dict) -> None:
        """Tell the panel, and tell the engine when the voices have landed.

        The engine reads the voice folder at startup and keeps the answer, so
        a download that finishes while it is running counted for nothing until
        the program was restarted -- which is exactly what the panel used to
        have to say. Running the step again over a finished install is
        harmless: the refresh is a directory listing.
        """
        broadcaster.publish({"kind": "setup", "setup": state})
        voices = next((step for step in state.get("steps", [])
                       if step.get("key") == "voices"), None)
        if not voices or state.get("running"):
            return
        if voices.get("state") not in ("done", "skipped"):
            return

        def pick_up() -> None:
            # Off the installer's thread: loading Kokoro is 300 MB and a
            # couple of seconds, and the panel should not be waiting on it to
            # be told the download finished.
            try:
                engine.voices_installed()
            except Exception:                        # pragma: no cover
                log.debug("could not pick up the new voices", exc_info=True)

        threading.Thread(target=pick_up, name="wilcoatc-voices",
                         daemon=True).start()

    installer = Installer(engine.config, on_change=_setup_changed)

    @app.get("/api/setup")
    async def setup_state() -> dict:
        """What is installed, and what the installer is doing about it."""
        return installer.state()

    @app.post("/api/setup")
    async def setup_start(body: dict | None = None) -> dict:
        """Fetch the missing pieces, or the named ones.

        Returns immediately: the work is minutes long and the answer the page
        wants is "yes, it has begun", after which it watches the socket.
        """
        body = body or {}
        steps = body.get("steps") or installer.missing() or list(SETUP_STEPS)
        started = installer.start(
            [str(key) for key in steps],
            force=bool(body.get("force")),
            all_voices=bool(body.get("all_voices")),
            hq=bool(body.get("hq")),
        )
        return {"started": started, "setup": installer.state()}

    @app.post("/api/setup/cancel")
    async def setup_cancel() -> dict:
        installer.cancel()
        return {"setup": installer.state()}

    @app.get("/api/doctor")
    async def doctor_view() -> dict:
        """The same report ``wilcoatc doctor`` prints, for the panel.

        Every check touches something slow -- a database, a sound card, the
        simulator, the weather service -- so it runs on a worker thread rather
        than blocking the event loop and every other request with it.
        """
        return await asyncio.to_thread(health_report, engine.config)

    @app.get("/api/diagnostics")
    async def diagnostics_view() -> dict:
        """What to paste into a bug report.

        Deliberately not the configuration: that holds the API key, and a
        pilot reporting a fault should not be reporting their credentials.
        """
        return diagnostics()

    @app.post("/api/metrics/reset")
    async def reset_metrics() -> dict:
        METRICS.reset()
        return {"ok": True}

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------

    @app.post("/api/ptt/learn")
    async def learn_ptt_button() -> dict:
        """Wait for a controller button, and say which one was pressed.

        The joystick is read here rather than in the browser: the button that
        matters is on a yoke the panel may be running on a different machine
        from, and the pilot should not have to have the window in focus to
        press it.

        Only reports it. Saving goes through the ordinary settings path, so
        the same rules about what needs a restart apply.
        """
        from ..audio.io import JoystickPushToTalk

        found = await asyncio.to_thread(JoystickPushToTalk.learn, 8.0)
        if found is None:
            devices, reason = JoystickPushToTalk.scan()
            return {"ok": False, "reason": reason or "nothing pressed",
                    "devices": len(devices)}
        index, button, name = found
        return {"ok": True, "index": index, "button": button, "name": name}

    @app.get("/api/settings")
    async def settings() -> dict:
        """The configuration the panel can change, and the choices for it."""
        return {"config": _settings_view(engine),
                "devices": _devices(engine.config.voice.engine),
                "announcements": _announcements(engine)}

    @app.post("/api/announcements/rescan")
    async def rescan_announcements() -> dict:
        """Look at the folder again, now.

        The library re-reads itself every half minute anyway, which is right
        while flying. Somebody who has just dropped a pack in and is standing
        at the settings screen should not have to wait for it.
        """
        engine.announcements.roots = announcement_roots(engine.config)
        engine.announcements.forget()
        return {"ok": True, "announcements": _announcements(engine)}

    @app.post("/api/settings")
    async def update_settings(payload: dict) -> dict:
        """Apply a change, and write it to config.yaml so it survives a restart.

        Some of these take effect on the next transmission and some need the
        engine restarted; ``restart_required`` says which, rather than letting
        the pilot wonder why the recogniser is still in the old language.
        """
        changed = _apply_settings(engine, payload)
        if not changed:
            return {"ok": False, "error": "nothing recognised in that change"}
        if any(name.startswith("ptt.") for name in changed):
            # The listener is built from these, so it has to be built again.
            # Off the event loop: stopping one joins its polling thread.
            await asyncio.to_thread(engine.rebind_ptt)
        try:
            engine.config.save()
        except Exception as exc:
            log.warning("could not write config.yaml: %s", exc)
            return {"ok": False, "error": f"could not save: {exc}",
                    "config": _settings_view(engine)}
        return {
            "ok": True,
            "changed": sorted(changed),
            "restart_required": sorted(changed & RESTART_REQUIRED),
            "config": _settings_view(engine),
        }

    @app.post("/api/sound-test")
    async def sound_test() -> dict:
        """Prove the audio path, in the voice of whoever is on frequency."""
        station = engine.station
        if station is None:
            return {"ok": False, "error": "tune a frequency first"}
        language = engine._language_for(station.ident, engine.last_language)
        p = engine.brain.for_station(station, language)
        session = engine.session
        if session is None:
            return {"ok": False, "error": "no flight yet"}
        session.aircraft.language = "fr" if getattr(p, "dialect", "") == "fr" else "en"
        text = p.radio_check(session.aircraft)
        engine.emit("atc", text, station.callsign)
        await asyncio.to_thread(engine.speak, text, station, False, language)
        return {"ok": True, "text": text}

    @app.post("/api/crew-test")
    async def crew_test() -> dict:
        """Hear the crew, without waiting for a flight to reach a cue.

        Picking a voice you cannot hear is picking blind, and the alternative
        to this button is taxiing out to find out whether the first officer
        sounds right.
        """
        from ..atc.crew_lines import CABIN_PA, FIRST_OFFICER, INTERCOM, PURSER, Line

        if not engine.config.immersion.enabled:
            return {"ok": False, "error": "the crew are switched off"}
        engine.cabin.start()
        session = engine.session
        if session is not None:
            engine.crew.begin(session, engine.last_language)

        lines = []
        if engine.config.immersion.copilot:
            lines.append(Line(FIRST_OFFICER,
                              "Flight deck check complete, ready when you are.",
                              INTERCOM))
        if engine.config.immersion.cabin:
            lines.append(Line(PURSER,
                              "Ladies and gentlemen, welcome aboard. The cabin "
                              "crew are completing their checks.",
                              CABIN_PA, chime="up"))
        if not lines:
            return {"ok": False, "error": "nobody is switched on"}
        for line in lines:
            await asyncio.to_thread(engine.crew.say, line)
        return {"ok": True, "text": lines[0].text}

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    @app.post("/api/ptt/{action}")
    async def ptt(action: str) -> dict:
        """Key the microphone on the machine running the engine."""
        if action == "start":
            engine.begin_transmission()
        elif action == "stop":
            engine.end_transmission()
        else:
            return {"ok": False, "error": "expected start or stop"}
        return {"ok": True, "recording": engine.microphone.recording}

    @app.post("/api/say")
    async def say(payload: dict) -> dict:
        """Type a transmission instead of speaking it."""
        text = (payload.get("text") or "").strip()
        if not text:
            return {"ok": False}
        language = payload.get("language") or None
        engine.emit("pilot", text)
        await asyncio.to_thread(engine.handle_text, text, language)
        return {"ok": True}

    @app.post("/api/tune")
    async def tune(payload: dict) -> dict:
        """Tune a frequency here, and in the aeroplane.

        Both, always. The panel is a radio the pilot is allowed to use, so
        picking a station on the Frequencies screen sets COM1 in the
        simulator through the same event the knob in the cockpit sends.
        """
        try:
            mhz = float(payload.get("mhz"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad frequency"}
        standby = bool(payload.get("standby"))
        radio = int(payload.get("radio") or 1)

        ok, why = engine.set_radio(mhz, radio=radio, standby=standby)
        if not ok:
            return {"ok": False, "error": why}
        # Whether the aircraft's own radio moved, rather than only this
        # program's idea of it. A stand-in source reports itself connected --
        # it is, there is a state to read -- so asking it that question gets
        # a yes that means nothing.
        in_sim = engine.state.source == "simconnect" and engine.state.connected
        if radio == 1 and not standby:
            station = engine.tune(mhz)
            return {"ok": True, "station": station.callsign if station else "",
                    "in_simulator": in_sim}
        return {"ok": True, "station": "", "in_simulator": in_sim}

    @app.post("/api/swap")
    async def swap(payload: dict) -> dict:
        """Swap active and standby, in the aeroplane as well as here."""
        radio = int((payload or {}).get("radio") or 1)
        ok, why = engine.swap_radio(radio)
        if not ok:
            return {"ok": False, "error": why}
        if radio == 1:
            station = engine.tune(engine.state.com1_active)
            return {"ok": True, "station": station.callsign if station else ""}
        return {"ok": True, "station": ""}

    @app.post("/api/squawk")
    async def squawk(payload: dict) -> dict:
        """Dial a transponder code into the aircraft."""
        ok, why = engine.set_squawk(str((payload or {}).get("code") or ""))
        return {"ok": ok, "error": "" if ok else why}

    @app.post("/api/traffic/spawn")
    async def spawn_traffic() -> dict:
        """Line an AI departure up on the runway in use."""
        ok, callsign, why = engine.spawn_on_runway()
        return {"ok": ok, "callsign": callsign, "error": why}

    @app.post("/api/simbrief")
    async def simbrief(payload: dict) -> dict:
        """Fill the flight in from the latest SimBrief plan."""
        from ..integrations.simbrief import SimBriefError

        user = (payload.get("username") or "").strip()
        if user:
            engine.config.simbrief.username = user
        try:
            described = await asyncio.to_thread(engine.load_flight_plan, user)
        except SimBriefError as why:
            return {"ok": False, "error": str(why)}
        except Exception as exc:                    # pragma: no cover
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "plan": described,
                "settings": _settings_view(engine)}

    @app.post("/api/atis")
    async def atis(payload: dict) -> dict:
        ident = (payload.get("ident") or "").upper() or None
        language = payload.get("language") or None
        report = await asyncio.to_thread(engine.play_atis, ident, language)
        if report is None:
            return {"ok": False}
        return {"ok": True, "letter": report.letter,
                "text": as_written(report.text)}

    @app.post("/api/flight")
    async def flight(payload: dict) -> dict:
        """Set the details the simulator cannot supply."""
        for key in ("callsign", "aircraft_type", "departure", "destination"):
            if key in payload and payload[key] is not None:
                setattr(engine.config.flight, key, str(payload[key]).strip())
        if "cruise_altitude_ft" in payload:
            try:
                engine.config.flight.cruise_altitude_ft = int(
                    payload["cruise_altitude_ft"] or 0
                )
            except (TypeError, ValueError):
                pass
        await asyncio.to_thread(engine.begin_session)
        return {"ok": True, "state": snapshot(engine)}

    # ------------------------------------------------------------------
    # live stream
    # ------------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket(socket: WebSocket) -> None:
        await socket.accept()
        broadcaster.clients.add(socket)
        try:
            await socket.send_json({"kind": "snapshot", "state": snapshot(engine)})
            for payload in broadcaster.backlog():
                await socket.send_json(payload)
            while True:
                await socket.receive_text()   # the page only listens
        except WebSocketDisconnect:
            pass
        except Exception:
            log.debug("websocket closed", exc_info=True)
        finally:
            broadcaster.clients.discard(socket)

    app.mount("/", PanelFiles(directory=str(STATIC), html=True), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html",
                            headers={"cache-control": "no-cache"})

    return app


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------
#
# Only a subset of config.yaml is exposed. The rest is either a path, a model
# size that wants a download, or a tuning constant that nobody should be moving
# from a web page in flight.

# Settings the running engine cannot pick up on its own. The recogniser and the
# voices are built once at startup, so changing what they are is a restart.
RESTART_REQUIRED: set[str] = {
    "speech.languages", "speech.model", "audio.input_device",
    "audio.output_device", "ai.provider", "ai.model", "voice.seed",
}


def _settings_view(engine: Engine) -> dict[str, Any]:
    config = engine.config
    return {
        "audio": {
            "input_device": config.audio.input_device,
            "output_device": config.audio.output_device,
            "volume": round(config.audio.volume, 2),
            "radio_effects": config.audio.radio_effects,
        },
        "ptt": {
            "mode": config.ptt.mode,
            "key": config.ptt.key,
            "joystick_index": config.ptt.joystick_index,
            "joystick_button": config.ptt.joystick_button,
        },
        "speech": {
            "model": config.speech.model,
            # Empty means the language follows the aeroplane, which is the
            # default and is not something the interface offers to change.
            "languages": list(config.speech.languages),
        },
        "ui": {"language": config.ui.language,
               "landing_report": config.ui.landing_report},
        "simbrief": {"username": config.simbrief.username},
        "gsx": {"enabled": config.gsx.enabled,
                "bridge": config.gsx.bridge,
                "lvar_file": config.gsx.lvar_file},
        "traffic": {
            "enabled": config.traffic.enabled,
            "density": round(config.traffic.density, 2),
        },
        "network": {
            "provider": config.network.provider,
            "use_online_atis": config.network.use_online_atis,
        },
        "atc": {
            "dialect": config.atc.dialect,
            "strict_icao_digits": config.atc.strict_icao_digits,
            "proactive_handoff": config.atc.proactive_handoff,
        },
        "voice": {"seed": config.voice.seed},
        # The operator, as opposed to the radio. The chain itself is measured
        # and stays out of the panel; what is offered is how much of the
        # person holding the transmit bar comes with it.
        "delivery": {
            "imperfections": {
                "enabled": config.delivery.imperfections.enabled,
                "breath_db": config.delivery.imperfections.breath_db,
                "wind_db": config.delivery.imperfections.wind_db,
                "hesitation_chance":
                    config.delivery.imperfections.hesitation_chance,
                "mid_hesitation_chance":
                    config.delivery.imperfections.mid_hesitation_chance,
            },
        },
        "immersion": {
            "enabled": config.immersion.enabled,
            "copilot": config.immersion.copilot,
            "copilot_voice": config.immersion.copilot_voice,
            "copilot_gender": config.immersion.copilot_gender,
            "callouts": config.immersion.callouts,
            "confirmations": config.immersion.confirmations,
            "checklists": config.immersion.checklists,
            "radio_readbacks": config.immersion.radio_readbacks,
            "cabin": config.immersion.cabin,
            "cabin_voice": config.immersion.cabin_voice,
            "cabin_gender": config.immersion.cabin_gender,
            "briefings": config.immersion.briefings,
            "announcements": config.immersion.announcements,
            "service": config.immersion.service,
            "chimes": config.immersion.chimes,
            "boarding_music": config.immersion.boarding_music,
            "applause": config.immersion.applause,
            "realism": config.immersion.realism,
            "bilingual": config.immersion.bilingual,
            "recordings": config.immersion.recordings,
            "recordings_dir": config.immersion.recordings_dir,
            "volume": round(config.immersion.volume, 2),
            "airline": config.immersion.airline,
            "captain_name": config.immersion.captain_name,
            "interphone": config.immersion.interphone,
            "verbosity": config.immersion.verbosity,
            "language": config.immersion.language,
        },
        "gsx": {
            "enabled": config.gsx.enabled,
            "control": config.gsx.control,
            "voice": config.gsx.voice,
        },
        "sim": {
            "enabled": config.sim.enabled,
            "frequencies_from_sim": config.sim.frequencies_from_sim,
        },
        "logging": {
            "level": config.logging.level,
            "console_level": config.logging.console_level,
            "structured": config.logging.structured,
            "radio": config.logging.radio,
            "keep_days": config.logging.keep_days,
        },
        "ai": {"provider": config.ai.provider, "model": config.ai.model},
        "weather": {"online": config.weather.online,
                    "prefer_sim": config.weather.prefer_sim},
        "flight": {
            "callsign": config.flight.callsign,
            "aircraft_type": config.flight.aircraft_type,
            "departure": config.flight.departure,
            "destination": config.flight.destination,
            "cruise_altitude_ft": config.flight.cruise_altitude_ft,
            "ifr": config.flight.ifr,
        },
    }


# What each editable setting is, so a value from a browser is coerced rather
# than trusted. Anything not named here is refused.
_SETTING_TYPES: dict[str, str] = {
    "audio.input_device": "device", "audio.output_device": "device",
    "audio.volume": "float", "audio.radio_effects": "bool",
    "ptt.mode": "str", "ptt.key": "str",
    "ptt.joystick_index": "int", "ptt.joystick_button": "int",
    "speech.model": "str", "speech.languages": "languages",
    "atc.dialect": "str", "atc.strict_icao_digits": "bool",
    "atc.proactive_handoff": "bool",
    "traffic.enabled": "bool", "traffic.density": "float",
    "ui.language": "str", "ui.landing_report": "bool",
    "simbrief.username": "str", "simbrief.load_on_start": "bool",
    "gsx.enabled": "bool", "gsx.bridge": "str", "gsx.lvar_file": "str",
    "gsx.control": "bool", "gsx.voice": "bool",
    "voice.seed": "str",
    "delivery.imperfections.enabled": "bool",
    "delivery.imperfections.breath_db": "float",
    "delivery.imperfections.wind_db": "float",
    "delivery.imperfections.hesitation_chance": "float",
    "delivery.imperfections.mid_hesitation_chance": "float",
    "immersion.enabled": "bool", "immersion.copilot": "bool",
    "immersion.copilot_voice": "str", "immersion.copilot_gender": "str",
    "immersion.callouts": "bool", "immersion.confirmations": "bool",
    "immersion.checklists": "bool", "immersion.radio_readbacks": "bool",
    "immersion.cabin": "bool", "immersion.cabin_voice": "str",
    "immersion.cabin_gender": "str", "immersion.briefings": "bool",
    "immersion.announcements": "bool", "immersion.service": "bool",
    "immersion.chimes": "bool", "immersion.volume": "float",
    "immersion.boarding_music": "bool", "immersion.applause": "bool",
    "immersion.realism": "bool", "immersion.bilingual": "bool",
    "immersion.recordings": "bool", "immersion.recordings_dir": "str",
    "immersion.airline": "str", "immersion.verbosity": "str",
    "immersion.captain_name": "str", "immersion.interphone": "bool",
    "immersion.language": "str",
    "sim.enabled": "bool", "sim.frequencies_from_sim": "bool",
    "ai.provider": "str", "ai.model": "str",
    "weather.online": "bool", "weather.prefer_sim": "bool",
    "flight.callsign": "str", "flight.aircraft_type": "str",
    "flight.departure": "str", "flight.destination": "str",
    "flight.cruise_altitude_ft": "int", "flight.ifr": "bool",
    "network.provider": "str", "network.use_online_atis": "bool",
    "logging.level": "str", "logging.console_level": "str",
    "logging.structured": "bool", "logging.radio": "bool",
    "logging.keep_days": "int",
}


def _coerce(kind: str, value: Any) -> Any:
    if kind == "bool":
        return bool(value)
    if kind == "int":
        return int(value or 0)
    if kind == "float":
        return float(value)
    if kind == "device":
        # Null means the system default, which is not the same as index zero.
        if value in (None, "", "default"):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return str(value)
    if kind == "languages":
        # An empty list is the normal setting: it means the languages follow
        # the aeroplane rather than being pinned. A non-empty one always keeps
        # English, which is worked everywhere.
        codes = [str(c).lower()[:2] for c in (value or []) if str(c).strip()]
        if codes and "en" not in codes:
            codes.insert(0, "en")
        return codes
    return str(value).strip()


def _apply_settings(engine: Engine, payload: dict) -> set[str]:
    """Write the named settings onto the live config. Returns what changed."""
    changed: set[str] = set()
    for path, value in (payload or {}).items():
        kind = _SETTING_TYPES.get(path)
        if kind is None:
            continue
        # Walk the whole path rather than splitting once. Most settings are
        # "section.field", but the delivery layers are a section of sections
        # -- "delivery.imperfections.enabled" -- and a single partition put
        # the rest of the path in the field name and silently dropped it.
        *sections, field_name = path.split(".")
        target = engine.config
        for step in sections:
            target = getattr(target, step, None)
            if target is None:
                break
        if target is None or not hasattr(target, field_name):
            continue
        try:
            coerced = _coerce(kind, value)
        except (TypeError, ValueError):
            continue
        if getattr(target, field_name) == coerced:
            continue
        setattr(target, field_name, coerced)
        changed.add(path)

    # A few take effect immediately, and it is worth the special case: nobody
    # should have to restart to turn the radio effects off or the volume down.
    if "audio.volume" in changed:
        engine.player.volume = engine.config.audio.volume
    if changed & {"logging.level", "logging.console_level",
                  "logging.structured", "logging.radio"}:
        from ..logs import configure

        configure(engine.config, force=True)
    if changed & {"flight.callsign", "flight.aircraft_type",
                  "flight.departure", "flight.destination",
                  "flight.cruise_altitude_ft", "flight.ifr"}:
        engine.begin_session()

    # The crew are switched on and off while the flight is running, so both
    # directions have to do something now rather than at the next restart.
    if "immersion.enabled" in changed:
        if engine.config.immersion.enabled:
            engine.cabin.start()
            if engine.session is not None:
                engine.crew.begin(engine.session, engine.last_language)
        else:
            # Silence, immediately. Turning the cabin off and then listening
            # to the rest of an announcement is not turning it off.
            engine.crew.stop()
    if "immersion.volume" in changed:
        engine.cabin.volume = engine.config.immersion.volume
    # The crew's voices are read line by line, but a line in hand was
    # rendered in the old voice and the crew's language is settled when the
    # flight begins -- so without this, a new cabin voice was not heard
    # until something already queued had run out, or not at all.
    if changed & {"immersion.copilot_voice", "immersion.copilot_gender",
                  "immersion.cabin_voice", "immersion.cabin_gender",
                  "immersion.language"}:
        engine.crew.voices_changed()
    if "immersion.recordings_dir" in changed:
        engine.announcements.roots = announcement_roots(engine.config)
        engine.announcements.forget()

    # Asking for the simulator is asking for it now, not on the next launch.
    if "sim.enabled" in changed:
        engine.want_simulator(engine.config.sim.enabled)

    # The delivery layers are read once, at construction, and every rendering
    # is cached -- so without both halves of this a pilot turns the wind off,
    # hears it on the next four transmissions, and reasonably concludes the
    # switch does nothing. The cache is the expensive half and is thrown away
    # rather than filtered: the settings changed for every voice at once.
    if any(path.startswith("delivery.") for path in changed):
        from ..audio.imperfections import from_config as imperfections_for
        from ..audio.prosody import from_config as prosody_for
        engine.synth.imperfections = imperfections_for(
            engine.config.delivery.imperfections)
        engine.synth.prosody = prosody_for(engine.config.delivery.prosody)
        engine.synth.forget()
    return changed


def _announcements(engine: Engine) -> dict[str, Any]:
    """What recorded announcements are installed, and where they are.

    The folder is the whole of the setup, so the settings screen has to be
    able to say whether anything was found in it. "I put the files there and
    nothing happened" is otherwise unanswerable.
    """
    library = engine.announcements
    try:
        found = library.count
        packs = library.airlines()
    except Exception as exc:
        log.debug("could not read the announcements folder: %s", exc)
        found, packs = 0, []
    return {
        "folders": [str(root) for root in library.roots],
        "count": found,
        "airlines": packs,
        "events": list(ANNOUNCEMENT_EVENTS),
    }


def _devices(engine_name: str = "auto") -> dict[str, Any]:
    """What the machine can record from and play to, for the settings screen."""
    inputs: list[dict] = []
    outputs: list[dict] = []
    try:
        from ..audio.io import list_audio_devices

        found_in, found_out = list_audio_devices()
        inputs = [{"index": i, "name": n} for i, n in found_in]
        outputs = [{"index": i, "name": n} for i, n in found_out]
    except Exception as exc:
        log.debug("audio devices unavailable: %s", exc)

    joysticks: list[dict] = []
    # Why there are none, when there are none. Telling somebody who has just
    # installed pygame-ce to install pygame-ce is worse than saying nothing.
    reason = ""
    # Controllers this computer has met before but cannot see now. Naming the
    # missing yoke is the difference between "nothing found" and "your yoke is
    # not switched on", which is the thing the pilot actually needs told.
    known: list[str] = []
    try:
        from ..audio.io import JoystickPushToTalk

        found, reason = JoystickPushToTalk.scan()
        joysticks = [{"index": i, "name": n, "buttons": b}
                     for i, n, b in found]
        here = {n for _i, n, _b in found}
        known = [n for n in JoystickPushToTalk.remembered() if n not in here]
    except Exception as exc:
        reason = str(exc)
        log.debug("controllers unavailable: %s", exc)

    # The voices actually installed, so the crew pickers offer people who
    # exist. A catalogue entry that has not been downloaded is not a choice,
    # it is a silence waiting to happen.
    voices: list[dict] = []
    try:
        from ..audio.voices import VoiceRegistry

        for spec in VoiceRegistry(engine=engine_name).available():
            voices.append({
                "key": spec.key,
                "name": spec.speaker_name or spec.name.replace("_", " ").title(),
                "gender": spec.gender,
                "accent": spec.accent,
                "language": spec.language,
            })
        voices.sort(key=lambda v: (v["language"], v["accent"], v["name"]))
    except Exception as exc:
        log.debug("voices unavailable: %s", exc)

    return {"inputs": inputs, "outputs": outputs, "joysticks": joysticks,
            "joystick_reason": reason, "joystick_known": known,
            "voices": voices}


# --------------------------------------------------------------------------


# What each phase is called on a strip. Short, because the column is narrow and
# because this is what a controller would write on one.
PHASE_LABELS = {
    "clearance": "clearance", "taxi": "taxiing", "holding": "holding short",
    "ready": "ready for departure", "waiting": "holding short",
    "lineup": "lining up", "takeoff": "departing", "departed": "climbing out",
    "inbound": "inbound", "joining": "joining", "final": "on final",
    "landed": "landing roll", "taxi_in": "taxiing in",
    # The circuit, and the aeroplane that has just been sent round it again.
    "upwind": "upwind", "crosswind": "crosswind", "downwind": "downwind",
    "base": "base leg", "go_around": "going around",
}


# The English for each kind of instruction. The window overrides it from its
# own table when it is being read in something else; this is what it falls
# back to, and it is the same arrangement PHASE_LABELS has.
ORDER_LABELS = {
    "clearance": "cleared", "taxi": "taxi", "hold_short": "hold short",
    "line_up": "line up and wait", "takeoff": "cleared takeoff",
    "land": "cleared to land", "touch_and_go": "cleared for the option",
    "go_around": "go around", "extend": "extend downwind",
    "continue": "continue approach", "pattern": "join circuit",
    "handoff": "contact next", "taxi_in": "taxi to stand",
    "turn_out": "on course", "heading": "heading", "altitude": "climb to",
    "speed": "speed", "resume": "resume own navigation",
}


def _order_view(order: dict | None) -> dict[str, Any]:
    """What an aeroplane was last told, for the window.

    The kind travels as a token so the window can say it in the language it is
    being read in, and the English travels with it as the fallback for the
    window that is being read in English.
    """
    kind = (order or {}).get("kind", "")
    return {
        "kind": kind,
        "detail": (order or {}).get("detail", ""),
        "label": ORDER_LABELS.get(kind, kind.replace("_", " ")),
    }


# How close a field has to be for its runways to be drawn to scale, and how
# many fields get that treatment. Beyond a few miles a runway is shorter than
# the symbol for the aerodrome and drawing it adds nothing.
RUNWAY_DETAIL_NM = 18.0
RUNWAY_DETAIL_FIELDS = 4


# What the geographic map is given at each scale.
#
# Every row is one honest answer to "what can a person actually see here".
# The temptation at the wide end is to send everything and let the browser
# sort it out; forty thousand aerodromes is four megabytes of JSON and a grey
# smear, so the wide end sends the fields somebody would be looking for at
# that scale and the airspace they sit in.
#
#   up_to_nm      how wide the view is, in nautical miles across
#   min_rank      the smallest airport worth drawing (3 large, 2 medium,
#                 1 small, 0 heliports and strips)
#   airports      how many come back at most
#   boundaries    how many airspace regions, 0 for none
#   thin          keep one point in N of a region outline
#   runways       whether runway geometry is drawn at all
_MAP_DETAIL: tuple[dict[str, Any], ...] = (
    {"name": "aerodrome", "up_to_nm": 25, "min_rank": 0, "airports": 60,
     "boundaries": 0, "thin": 1, "runways": True, "runway_fields": 8,
     "stations": True, "station_fields": 12},
    {"name": "local", "up_to_nm": 80, "min_rank": 1, "airports": 200,
     "boundaries": 0, "thin": 1, "runways": True, "runway_fields": 4,
     "stations": True, "station_fields": 25},
    {"name": "area", "up_to_nm": 250, "min_rank": 1, "airports": 300,
     "boundaries": 12, "thin": 2, "runways": False, "runway_fields": 0,
     "stations": True, "station_fields": 40},
    {"name": "regional", "up_to_nm": 700, "min_rank": 2, "airports": 320,
     "boundaries": 30, "thin": 3, "runways": False, "runway_fields": 0,
     "stations": False, "station_fields": 0},
    {"name": "continental", "up_to_nm": 2500, "min_rank": 3, "airports": 300,
     "boundaries": 60, "thin": 5, "runways": False, "runway_fields": 0,
     "stations": False, "station_fields": 0},
    {"name": "world", "up_to_nm": 1e9, "min_rank": 3, "airports": 220,
     "boundaries": 90, "thin": 9, "runways": False, "runway_fields": 0,
     "stations": False, "station_fields": 0},
)


def _detail_for(span_nm: float) -> dict[str, Any]:
    """Which row of the detail table a view of this width gets."""
    for level in _MAP_DETAIL:
        if span_nm <= level["up_to_nm"]:
            return level
    return _MAP_DETAIL[-1]


def _inside(lat: float, lon: float, south: float, north: float,
            west: float, east: float) -> bool:
    """Whether a point is in a rectangle, the date line included.

    A window that spans 180 degrees has a west edge numerically east of its
    east edge. Everything that tests a longitude has to know that, and the
    ones that do not are the ones that go blank halfway across the Pacific.
    """
    if not (south <= lat <= north):
        return False
    west = ((west + 180.0) % 360.0) - 180.0
    east = ((east + 180.0) % 360.0) - 180.0
    lon = ((lon + 180.0) % 360.0) - 180.0
    if west > east:
        return lon >= west or lon <= east
    return west <= lon <= east


def _thin(ring: list, step: int) -> list:
    """Every ``step``-th point of an outline, with the ends kept.

    Crude next to a real simplification, and right here for the same reason
    the real one is right in the build script: this runs while a flight is in
    progress, several times a second, and the cheapest correct answer wins.
    An airspace boundary is a legal fiction with no fine detail to lose.
    """
    if step <= 1 or len(ring) <= 8:
        return [[round(float(x), 3), round(float(y), 3)] for x, y in ring]
    kept = ring[::step]
    if kept[-1] is not ring[-1]:
        kept = kept + [ring[-1]]
    return [[round(float(x), 3), round(float(y), 3)] for x, y in kept]


def _traffic_view(engine: Engine) -> list[dict[str, Any]]:
    """Every aeroplane on the frequency, as the maps draw it.

    With the rest of its route (``path``) and the facts the card beside it
    shows, which the engine knows and the panel only puts into words.
    """
    traffic = []
    for flight in engine.traffic.positions():
        traffic.append({
            **flight,
            "lat": round(flight["lat"], 6),
            "lon": round(flight["lon"], 6),
            "label": PHASE_LABELS.get(flight["phase"], flight["phase"]),
            "order": _order_view(flight.get("order")),
            "in_simulator": bool(
                flight.get("observed")
                or engine.simtraffic.has(flight["callsign"])),
        })
    return traffic


def _runway_lines(db, ident: str) -> list[dict[str, Any]]:
    """Runways at a field, in real coordinates, for the geographic map."""
    lines = []
    for runway in db.runways(ident):
        if runway.closed:
            continue
        if (runway.le_lat is None or runway.le_lon is None
                or runway.he_lat is None or runway.he_lon is None):
            continue
        lines.append({
            "le_ident": runway.le_ident,
            "he_ident": runway.he_ident,
            "length_ft": round(runway.length_ft or 0.0),
            "hard": runway.is_hard,
            "le_lat": round(runway.le_lat, 6), "le_lon": round(runway.le_lon, 6),
            "he_lat": round(runway.he_lat, 6), "he_lon": round(runway.he_lon, 6),
        })
    return lines


def _within_reach(engine: Engine) -> list[dict[str, Any]]:
    """Every station whose coverage actually reaches the aircraft.

    Centred on the aeroplane rather than on the map, and computed here rather
    than filtered in the page, because the page only has what is inside the
    window -- and the window is exactly what this must not depend on.
    """
    state = engine.state
    if not state.has_position:
        return []
    db = engine.navdb
    tuned = engine.station
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, float]] = set()
    for station in db.all_stations_near(state.latitude, state.longitude,
                                        max_nm=STATION_REACH_NM):
        reach = STATION_RANGE_NM.get(station.position, 40.0)
        if station.distance_nm > reach:
            continue
        key = (station.ident, station.position, round(station.mhz, 3))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "ident": station.ident,
            "position": station.position,
            "mhz": channel_name(station.mhz),
            "carrier": round(station.mhz, 3),
            "callsign": station.callsign,
            "distance_nm": round(station.distance_nm, 1),
            "range_nm": reach,
            "tuned": bool(tuned and same_channel(tuned.mhz, station.mhz)
                          and tuned.ident == station.ident),
        })
    out.sort(key=lambda s: s["distance_nm"])
    return out[:60]


# The widest coverage any position has, so the search for who can hear you
# starts from a box big enough to contain all of them.
STATION_REACH_NM = max(STATION_RANGE_NM.values()) if STATION_RANGE_NM else 200.0


def _ownship(engine: Engine) -> dict[str, Any] | None:
    """Where you are, for a map that is not necessarily centred on you."""
    state = engine.state
    if not state.has_position:
        return None
    session = engine.session
    return {
        "lat": round(state.latitude, 5),
        "lon": round(state.longitude, 5),
        "heading": round(state.heading_true),
        "track": round(state.track_true or state.heading_true),
        "altitude_ft": round(state.altitude_ft),
        "ground_speed_kt": round(state.ground_speed_kt),
        "vertical_rate_fpm": round(state.vertical_speed_fpm),
        "on_ground": state.on_ground,
        "callsign": (session.aircraft.callsign.written if session else ""),
        "type": state.type_code or engine.config.flight.aircraft_type,
        "squawk": state.transponder_code,
        "com1": round(state.com1_active, 3),
        # Where you are going, as a straight line: the map draws it the way
        # it draws everybody else's, and a flight plan's airways are not
        # something this knows.
        "destination": (session.destination.ident
                        if session and session.destination else ""),
        "destination_lat": (session.destination.lat
                            if session and session.destination else None),
        "destination_lon": (session.destination.lon
                            if session and session.destination else None),
        "origin": (session.departure.ident
                   if session and session.departure else ""),
    }


def _runway_shapes(db, ident: str, offsets) -> list[dict[str, Any]]:
    """Every runway at a field, as the two ends the map draws between.

    Only the ones with both ends surveyed. A runway with one published
    coordinate would have to be drawn from a guess about where the other end
    is, and a guessed runway on a map that is otherwise all fact is worse than
    no runway at all.
    """
    shapes = []
    for runway in db.runways(ident):
        if runway.closed:
            continue
        if (runway.le_lat is None or runway.le_lon is None
                or runway.he_lat is None or runway.he_lon is None):
            continue
        low = offsets(runway.le_lat, runway.le_lon)
        high = offsets(runway.he_lat, runway.he_lon)
        shapes.append({
            "le_ident": runway.le_ident,
            "he_ident": runway.he_ident,
            "length_ft": round(runway.length_ft or 0.0),
            "hard": runway.is_hard,
            "le_east_nm": low["east_nm"], "le_north_nm": low["north_nm"],
            "he_east_nm": high["east_nm"], "he_north_nm": high["north_nm"],
        })
    return shapes


def _traffic_strips(engine: Engine) -> list[dict[str, Any]]:
    """The other aeroplanes, as the panel shows them."""
    station = engine.station
    here = station.position if station is not None else ""
    strips = []
    for flight in engine.traffic.positions():
        strips.append({
            "callsign": flight["callsign"],
            "type": flight["type"],
            "operator": flight["operator"],
            "language": flight["language"],
            "phase": flight["phase"],
            "label": PHASE_LABELS.get(flight["phase"], flight["phase"]),
            "arriving": flight["arriving"],
            "runway": flight["runway"],
            "distance_nm": flight["distance_nm"],
            "altitude_ft": flight["altitude_ft"],
            "on_ground": flight["on_ground"],
            # Whether this one is on the frequency the pilot is listening to,
            # so the panel can show who can actually be heard.
            "on_frequency": bool(here) and flight["position"] == here,
            "ifr": flight.get("ifr", True),
            "in_simulator": engine.simtraffic.has(flight["callsign"]),
            # What the tower last told it, and what it is doing about it.
            "order": _order_view(flight.get("order")),
            "heading": flight.get("heading", 0),
            "vertical_rate_fpm": flight.get("vertical_rate_fpm", 0),
        })
    strips.sort(key=lambda f: (not f["on_frequency"], not f["arriving"],
                               f["distance_nm"]))
    return strips[:12]


def snapshot(engine: Engine) -> dict[str, Any]:
    """Everything the panel shows, in one object."""
    state = engine.state
    session = engine.session
    station = engine.station

    # Weather and the ATIS belong to a field, not to a frequency. With nothing
    # tuned the panel still has somewhere to report from: the field you are
    # standing on, or the nearest one.
    ident = station.ident if station is not None else ""
    if not ident and state.has_position:
        home = engine.navdb.home_airport(state.latitude, state.longitude)
        if home is None:
            home = engine.navdb.nearest_airport(state.latitude, state.longitude,
                                                max_nm=40.0, min_rank=1)
        ident = home.ident if home else ""

    weather = None
    atis_letter = ""
    airport = engine.navdb.airport(ident) if ident else None
    if ident:
        try:
            wx = engine.weather_for(ident)
            weather = {
                "wind_dir": round(wx.wind_dir),
                "wind_kt": round(wx.wind_kt),
                "gust_kt": round(wx.gust_kt),
                "qnh_hpa": round(wx.qnh_hpa),
                "altimeter_inhg": round(wx.altimeter_inhg, 2),
                "temperature_c": round(wx.temperature_c),
                "visibility_sm": round(wx.visibility_sm, 1),
            }
            report = engine.atis_for(ident)
            atis_letter = report.letter if report else ""
        except Exception:
            log.debug("weather unavailable for the panel", exc_info=True)

    return {
        "connected": state.connected,
        "source": state.source,
        "aircraft": {
            "callsign": session.aircraft.callsign.written if session else "",
            "spoken": session.aircraft.callsign.spoken_full if session else "",
            # The simulator names the type; without one, what the pilot filed.
            "type": state.type_code or engine.config.flight.aircraft_type,
            "wake": session.aircraft.wake if session else "",
        },
        "position": {
            "latitude": round(state.latitude, 4),
            "longitude": round(state.longitude, 4),
            "altitude_ft": round(state.altitude_ft),
            "heading": round(state.heading_true),
            "ground_speed_kt": round(state.ground_speed_kt),
            "on_ground": state.on_ground,
            "squawk": state.transponder_code,
        },
        "radio": {
            "com1": round(state.com1_active, 3),
            "com1_standby": round(state.com1_standby, 3),
            "station": station.callsign if station else "",
            "station_ident": station.ident if station else "",
            "station_short": station.callsign_short if station else "",
            "airport": airport.spoken if airport else "",
            "airport_name": airport.name if airport else "",
            "here": ident,
            "position": station.position if station else "",
            "distance_nm": round(station.distance_nm, 1) if station else 0,
            # Which way the field is, for the panel in the simulator: its one
            # circular face is a bearing ring and a needle resting at north
            # would say the airport was straight ahead.
            "bearing": (round(state.bearing_to(airport.lat, airport.lon))
                        if station and airport and state.has_position else None),
        },
        "flight": {
            "departure": session.departure.ident if session and session.departure else "",
            "destination": (
                session.destination.ident if session and session.destination else ""
            ),
            "phase": session.phase.value if session else "",
            "cruise_altitude_ft": engine.config.flight.cruise_altitude_ft,
            "assigned_altitude_ft": round(session.assigned_altitude_ft) if session else 0,
            "runway": (
                session.departure_runway or session.arrival_runway if session else ""
            ),
            "pending": session.pending.kind if session and session.pending else "",
            "cleared_takeoff": bool(session and session.cleared_takeoff),
            "cleared_landing": bool(session and session.cleared_landing),
            "cleared_runway": session.cleared_runway if session else "",
            "squawk": session.squawk if session else "",
        },
        "languages": [
            {"code": code, "name": LANGUAGE_NAMES.get(code, code)}
            for code in engine.languages
        ],
        "language": engine.last_language,
        "worked_here": (
            languages_for(station.ident) if station else engine.languages
        ),
        "weather": weather,
        "atis_letter": atis_letter,
        "transmitting": engine.microphone.recording,
        "playing": engine.player.busy,
        "ground": (engine.gsx.state.describe()
                   if getattr(engine, "gsx", None) is not None
                   and engine.gsx.state.available else ""),
        "understanding": engine.understanding.describe(),
        # The bare model name, so the panel can put its own sentence around it
        # in whatever language it is being read in. Empty means rules only.
        "understanding_model": (
            f"{engine.understanding.provider.name} "
            f"({getattr(engine.understanding.provider, 'model', '?')})"
            if engine.understanding.model_available else ""
        ),
        # The other aeroplanes the controller is working, in the order a
        # controller would care about them: the arrivals by how close they
        # are, then everybody on the ground.
        "traffic": _traffic_strips(engine),
        "stats": engine.understanding.stats.summary(),
        "ptt_key": engine.config.ptt.key,
        "ptt_mode": engine.config.ptt.mode,
        "radio_effects": engine.config.audio.radio_effects,
        "session_s": round(time.monotonic() - engine.started_at),
        # The last landing, measured, for the card that comes up after the
        # touchdown. The panel shows each one once, by its id, and only while
        # it is fresh -- a window opened an hour after the flight is not the
        # moment to announce how it landed.
        "landing": _landing_view(engine),
    }


def _landing_view(engine: Engine) -> dict[str, Any] | None:
    monitor = getattr(engine, "landing", None)
    report = getattr(monitor, "report", None)
    if report is None or not engine.config.ui.landing_report:
        return None
    return report.view()


async def _state_pump(engine: Engine, broadcaster: Broadcaster) -> None:
    """Push the aircraft state to the page a few times a second."""
    last: dict | None = None
    while True:
        await asyncio.sleep(0.4)
        try:
            current = snapshot(engine)
        except Exception:
            log.debug("state snapshot failed", exc_info=True)
            continue
        if current != last:
            last = current
            broadcaster.publish({"kind": "state", "state": current})


def event_sink(broadcaster: Broadcaster):
    """Adapt engine events onto the broadcaster."""

    def sink(event: Event) -> None:
        broadcaster.publish({
            "kind": "event",
            # The key and its parameters, so the panel can say this in the
            # language it is being read in; the text is the English fallback.
            "key": event.key,
            "params": event.params,
            "type": event.kind,
            "text": event.text,
            "speaker": event.speaker,
            "at": event.at,
            # What the panel can do about it, when there is something to do.
            "action": event.action,
        })

    return sink


# --------------------------------------------------------------------------
# the panel before there is anything to fly with
# --------------------------------------------------------------------------


class _ConfigOnly:
    """Just enough of an engine for the settings view to read the config."""

    def __init__(self, config):
        self.config = config


def create_setup_app(config, broadcaster: Broadcaster) -> FastAPI:
    """The panel on a machine where nothing has been downloaded yet.

    This exists because of a circle the program used to be unable to get out
    of. The engine cannot start without the navigation database, so on a fresh
    install the window did not open at all -- it printed "run wilcoatc setup"
    to a terminal, which is the one place the pilot who most needs that
    message is not looking. They double-clicked an icon and nothing appeared.

    So when the database is missing the same page is served by this instead:
    no engine, no radio, no aeroplane, and the three things that are any use
    without one -- what is installed, a button to fetch it, and the report that
    says what is wrong. When the download finishes the program is restarted and
    comes up properly.
    """
    app = FastAPI(title="wilcoatc setup", docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_credentials=False,
        allow_methods=["*"], allow_headers=["*"],
    )

    installer = Installer(
        config,
        on_change=lambda state: broadcaster.publish(
            {"kind": "setup", "setup": state}),
    )

    @app.on_event("startup")
    async def _startup() -> None:
        broadcaster.bind(asyncio.get_running_loop())

    @app.get("/api/setup")
    async def setup_state() -> dict:
        return installer.state()

    @app.post("/api/setup")
    async def setup_start(body: dict | None = None) -> dict:
        body = body or {}
        steps = body.get("steps") or installer.missing() or list(SETUP_STEPS)
        started = installer.start([str(key) for key in steps],
                                  force=bool(body.get("force")),
                                  all_voices=bool(body.get("all_voices")),
                                  hq=bool(body.get("hq")))
        return {"started": started, "setup": installer.state()}

    @app.post("/api/setup/cancel")
    async def setup_cancel() -> dict:
        installer.cancel()
        return {"setup": installer.state()}

    @app.get("/api/doctor")
    async def doctor_view() -> dict:
        return await asyncio.to_thread(health_report, config)

    @app.get("/api/settings")
    async def settings() -> dict:
        """Read-only, so the page opens in the pilot's own language.

        There is nothing to save it to: the engine that would apply a change
        does not exist yet. POST is simply not here, and the page reports a
        failed save rather than pretending one worked.
        """
        return {"config": _settings_view(_ConfigOnly(config)),
                "devices": {"inputs": [], "outputs": [], "joysticks": [],
                            "joystick_reason": "", "joystick_known": [],
                            "voices": []},
                "announcements": {"folders": [], "count": 0,
                                  "airlines": [],
                                  "events": list(ANNOUNCEMENT_EVENTS)}}

    @app.websocket("/ws")
    async def websocket(socket: WebSocket) -> None:
        """No snapshot and no transcript: there is no flight. Only the
        installer's progress, which is the whole reason the socket is open."""
        await socket.accept()
        broadcaster.clients.add(socket)
        try:
            await socket.send_json({"kind": "setup", "setup": installer.state()})
            while True:
                await socket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            log.debug("websocket closed", exc_info=True)
        finally:
            broadcaster.clients.discard(socket)

    app.mount("/", PanelFiles(directory=str(STATIC), html=True), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html",
                            headers={"cache-control": "no-cache"})

    return app


def serve_setup(config, broadcaster: Broadcaster, host: str, port: int) -> None:
    """Run the setup-only panel. Blocks until interrupted."""
    import uvicorn

    app = create_setup_app(config, broadcaster)
    uvicorn.Server(uvicorn.Config(app, host=host, port=port,
                                  log_level="warning", access_log=False)).run()


def serve(engine: Engine, broadcaster: Broadcaster, host: str, port: int) -> None:
    """Run the server. Blocks until interrupted."""
    import uvicorn

    app = create_app(engine, broadcaster)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning",
                            access_log=False)
    uvicorn.Server(config).run()
