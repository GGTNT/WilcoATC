"""The real aeroplanes around the pilot, from the free ADS-B sources.

Why this exists
---------------

The other aeroplanes on the frequency are invented (``atc/traffic.py``): good
radio, and fiction. The Ryanair on final at Charleroi is not the one on final
right now, and a pilot with a flight tracker on the second screen sees two
different airports. The real ones are public. adsb.lol and adsb.fi serve,
with no key and no account, every ADS-B aircraft within 250 nm of a point, and
adsbdb.com gives a callsign's route and an address's type. This is the feed
that reads them. What is done with a live flight -- the roster, the injector,
the controllers -- is elsewhere: here a flight is only what the sources said.

Designed after LocalTC's ``traffic/feed.py`` (read for the design, nothing
copied): the same sources, one area request every 6 s, a source that says it
is busy left alone 90 s, one that does not answer left alone 30 s doubling to
10 minutes, positions older than 30 s dropped, each position timed by the
source's own clock. Two things differ, both measured
(``NOTES_LIVE_TRAFFIC.md``):

- **One source at a time.** LocalTC alternates sources from one request to the
  next. The two networks see the same aircraft in the air but not the same
  ones on the ground (Fiumicino, 30 Oct 2026: none within 3 nm at adsb.lol,
  seven at adsb.fi), so alternating made the parked aeroplanes appear and
  vanish every 6 s. The feed starts on the source that sees the most of the
  ground round the field, stays on it, and uses another only while that one
  rests.
- **A key is never asked for.** adsb.lol says a key will one day be required.
  A source answering 401 or 403 is dropped for the session, and the others
  carry on.

What leaves the machine is the pilot's position, rounded to four places, and
a radius: nothing else, to anybody. adsbdb is asked a callsign or an address
the sources have just broadcast, and its answers are kept on this PC
(``data/adsb_cache.json``), a route for 20 hours and an unknown for 3, so a
flight number is asked about once a day at most.

Every call here is blocking and runs off the engine thread
(:class:`LiveFeedThread`); the engine only ever reads the last answer.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from ..navdata.build import haversine_nm

log = logging.getLogger(__name__)

USER_AGENT = "WilcoATC (https://github.com/GGTNT/WilcoATC)"

# One area request this often (after LocalTC): a sixth of adsb.fi's documented
# one request a second.
POLL_S = 6.0
MAX_NM = 250.0                  # the most the sources serve at once
DEFAULT_NM = 60.0
TIMEOUT_S = 8.0
STALE_S = 30.0                  # a position older than this is not used (after LocalTC)
# A source's clock trusted only this close to ours (after LocalTC); measured
# 1-3 s behind on both, 30 Oct 2026.
CLOCK_SLACK_S = 120.0
BUSY_S = 90.0                   # HTTP 429: left alone this long (after LocalTC)
DOWN_S, DOWN_MAX_S = 30.0, 600.0  # no answer: this long, doubling, to this (after LocalTC)
ROUTE_TTL_S = 20 * 3600.0       # a flight number flies one route all day (after LocalTC)
UNKNOWN_TTL_S = 3 * 3600.0      # (after LocalTC)
TYPE_TTL_S = 30 * 86400.0       # an airframe keeps its type
# The ground counted when the feed chooses its source: the field's own
# stands and taxiways, not the neighbour's.
GROUND_NM = 3.0
# adsbdb asked this many times per area answer at most, one after the other.
LOOKUPS_PER_POLL = 4

PUBLIC_SOURCES = (
    ("adsb.lol", "https://api.adsb.lol/v2/lat/{lat:.4f}/lon/{lon:.4f}/dist/{nm:.0f}"),
    ("adsb.fi", "https://opendata.adsb.fi/api/v2/lat/{lat:.4f}/lon/{lon:.4f}/dist/{nm:.0f}"),
)
ROUTE_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"
AIRCRAFT_URL = "https://api.adsbdb.com/v0/aircraft/{hex}"

LIVE, EMPTY, DOWN, STARTING = "live", "empty", "down", "starting"


class SourceError(Exception):
    """A source did not give an answer that can be used; ``status`` is the
    HTTP status when there was one."""

    def __init__(self, text: str, status: int = 0):
        super().__init__(text)
        self.status = status


@dataclass(frozen=True)
class Flight:
    """One real aeroplane, as a source reported it."""

    hex: str                    # the ICAO 24-bit address: the same airframe from one answer to the next
    callsign: str               # "RYR77QM", "OOABC"; "" when it is not sending one
    registration: str
    type: str                   # ICAO type, "" unknown
    category: str               # ADS-B emitter category, "A3" large
    lat: float
    lon: float
    alt_ft: float | None        # pressure altitude; None on the ground or not sent
    on_ground: bool
    gs_kt: float
    track: float
    vs_fpm: float
    squawk: str
    t: float                    # when it was there, time.time()

    @property
    def airline(self) -> str:
        """The operator's ICAO code from an airline callsign ("RYR77QM" ->
        "RYR"), "" for a registration."""
        cs = self.callsign
        return cs[:3] if len(cs) >= 4 and cs[:3].isalpha() and cs[3].isdigit() \
            else ""


def _number(value, default=None):
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def parse(data, now: float | None = None) -> list[Flight]:
    """A readsb-style answer as flights.

    adsb.lol puts the list under ``ac`` and its ``now`` in milliseconds;
    adsb.fi under ``aircraft`` and in seconds. Each position is timed by the
    source's clock less its ``seen_pos``, when that clock is within two
    minutes of ours: an answer a source held a few seconds before sending it
    would otherwise be that much behind. Anything without an address or a
    numeric position, and anything older than :data:`STALE_S`, is left out
    rather than guessed at.
    """
    if not isinstance(data, dict):
        raise SourceError("not an object")
    ours = time.time() if now is None else now
    clock = ours
    server = _number(data.get("now"))
    if server and server > 0:
        server_s = server / 1000.0 if server > 1e11 else server
        if abs(server_s - ours) < CLOCK_SLACK_S:
            clock = server_s
    listed = data.get("ac")
    if listed is None:
        listed = data.get("aircraft")
    if listed is None:
        raise SourceError("no aircraft list")
    out = []
    for a in listed if isinstance(listed, list) else ():
        if not isinstance(a, dict):
            continue
        hex_ = str(a.get("hex") or "").lower().lstrip("~")
        lat, lon = _number(a.get("lat")), _number(a.get("lon"))
        if not hex_ or lat is None or lon is None:
            continue
        seen = _number(a.get("seen_pos"), _number(a.get("seen"), 0.0))
        if seen > STALE_S:
            continue
        alt = a.get("alt_baro")
        ground = alt == "ground"
        out.append(Flight(
            hex=hex_,
            callsign=str(a.get("flight") or "").strip().upper(),
            registration=str(a.get("r") or "").strip().upper(),
            type=str(a.get("t") or "").strip().upper(),
            category=str(a.get("category") or ""),
            lat=lat, lon=lon,
            alt_ft=None if ground else _number(alt),
            on_ground=ground,
            gs_kt=_number(a.get("gs"), 0.0),
            track=_number(a.get("track"), _number(a.get("true_heading"), 0.0)),
            vs_fpm=_number(a.get("baro_rate"), _number(a.get("geom_rate"), 0.0)),
            squawk=str(a.get("squawk") or ""),
            t=clock - seen))
    return out


def http_get(url: str, timeout_s: float = TIMEOUT_S):
    """GET ``url`` as JSON, or SourceError with the status."""
    import requests

    try:
        r = requests.get(url, timeout=timeout_s, headers={
            "User-Agent": USER_AGENT, "Accept": "application/json"})
    except requests.RequestException as exc:
        raise SourceError(str(exc)) from exc
    if r.status_code != 200:
        raise SourceError(f"HTTP {r.status_code}", r.status_code)
    try:
        return r.json()
    except ValueError as exc:
        raise SourceError("not JSON", r.status_code) from exc


@dataclass
class Source:
    name: str
    url: str                    # {lat} {lon} {nm} filled in where present
    resting_until: float = 0.0
    why: str = ""
    failures: int = 0
    dropped: bool = False

    def address(self, lat: float, lon: float, nm: float) -> str:
        return self.url.format(lat=lat, lon=lon, nm=nm)


@dataclass(frozen=True)
class FeedState:
    """What the panel and ``decision()`` say about the feed."""

    status: str                 # starting | live | empty | down
    source: str = ""
    age_s: float | None = None  # since the last answer
    count: int = 0
    resting: tuple = ()         # ((name, seconds left, why), ...)
    dropped: tuple = ()         # names, a key asked for


class Feed:
    """The flights within ``radius_nm`` of a point, from one source at a time.

    ``poll`` makes at most one area request, except the first, which asks
    every source once and keeps the one that sees the most aeroplanes on the
    ground round the point. Nothing here sleeps: :class:`LiveFeedThread`
    decides when to call it, and a test calls it with a clock of its own.
    """

    def __init__(self, sources=None, fetch: Callable | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 radius_nm: float = DEFAULT_NM,
                 wall: Callable[[], float] = time.time):
        self.sources = [Source(n, u) for n, u in (sources or PUBLIC_SOURCES)]
        # The clock a position is timed against (the source's own, when it
        # is within two minutes of this one).
        self._wall = wall
        # Looked up at each call, so a test can stand in for the network.
        self._fetch = fetch or (lambda url: http_get(url))
        self._clock = clock
        self.radius_nm = min(max(float(radius_nm), 5.0), MAX_NM)
        self.chosen: Source | None = None
        self.flights: list[Flight] = []
        self.source = ""
        self.answered_at: float | None = None
        self.status = STARTING

    # -- the sources ---------------------------------------------------

    def _resting(self, s: Source, now: float) -> bool:
        return s.dropped or s.resting_until > now

    def _rest(self, s: Source, exc: SourceError, now: float) -> None:
        if exc.status in (401, 403):
            # A key now asked for: never asked of the pilot, so this source
            # is gone for the session.
            s.dropped, s.why = True, f"HTTP {exc.status}: a key is asked for"
            log.info("live traffic: %s wants a key; not used this session",
                     s.name)
            return
        if exc.status == 429:
            s.resting_until, s.why = now + BUSY_S, "busy (HTTP 429)"
        else:
            s.failures += 1
            s.resting_until = now + min(DOWN_MAX_S,
                                        DOWN_S * 2 ** (s.failures - 1))
            s.why = f"no answer ({exc})"
        log.info("live traffic: %s %s; left alone %.0f s", s.name, s.why,
                 s.resting_until - now)

    def _ask(self, s: Source, lat: float, lon: float, now: float):
        try:
            flights = parse(self._fetch(s.address(lat, lon, self.radius_nm)),
                            now=self._wall())
        except SourceError as exc:
            self._rest(s, exc, now)
            return None
        except Exception as exc:                    # pragma: no cover - defensive
            self._rest(s, SourceError(str(exc)), now)
            return None
        s.failures, s.why = 0, ""
        return flights

    @staticmethod
    def ground_near(flights, lat: float, lon: float) -> int:
        return sum(1 for f in flights if f.on_ground
                   and haversine_nm(lat, lon, f.lat, f.lon) <= GROUND_NM)

    def _choose(self, lat: float, lon: float, now: float):
        best = None
        for s in self.sources:
            if self._resting(s, now):
                continue
            flights = self._ask(s, lat, lon, now)
            if flights is None:
                continue
            score = self.ground_near(flights, lat, lon)
            if best is None or score > best[0]:
                best = (score, s, flights)
        if best is None:
            return None
        self.chosen = best[1]
        log.info("live traffic: %s chosen (%d on the ground round the field)",
                 best[1].name, best[0])
        return best[1], best[2]

    # -- one round -----------------------------------------------------

    def poll(self, lat: float, lon: float) -> FeedState:
        now = self._clock()
        answer = None
        if self.chosen is None:
            answer = self._choose(lat, lon, now)
        else:
            # The chosen source while it can answer, else the next one in
            # order that is not resting.
            order = [self.chosen] + [s for s in self.sources
                                     if s is not self.chosen]
            for s in order:
                if self._resting(s, now):
                    continue
                flights = self._ask(s, lat, lon, now)
                if flights is not None:
                    answer = (s, flights)
                    break
        if answer is None:
            if all(self._resting(s, now) for s in self.sources):
                self.status = DOWN
                self.flights = []
            return self.state()
        source, flights = answer
        self.source, self.flights, self.answered_at = \
            source.name, flights, now
        self.status = LIVE if flights else EMPTY
        return self.state()

    def state(self) -> FeedState:
        now = self._clock()
        return FeedState(
            status=self.status, source=self.source,
            age_s=(round(now - self.answered_at, 1)
                   if self.answered_at is not None else None),
            count=len(self.flights),
            resting=tuple((s.name, round(s.resting_until - now), s.why)
                          for s in self.sources
                          if not s.dropped and s.resting_until > now),
            dropped=tuple(s.name for s in self.sources if s.dropped))


def sources_for(own_url: str = "") -> tuple:
    """The public sources, then the pilot's own receiver when there is one
    (a local readsb's ``aircraft.json``, or anything that answers in its
    shape)."""
    sources = PUBLIC_SOURCES
    if own_url:
        sources = sources + (("own", own_url),)
    return sources


# --------------------------------------------------------------------------
# routes and types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    origin: str                 # ICAO
    destination: str
    destination_name: str = ""  # the city, as a clearance says it


class FlightBook:
    """Each callsign's route and each address's type and registration, from
    adsbdb.com, kept on this PC.

    Asked one at a time, a few per area answer, only for what is not known:
    a route only for an airline callsign, a type only where the position did
    not carry one. An unknown callsign is an answer too (HTTP 404, "unknown
    callsign"), kept 3 hours rather than asked again every 6 seconds.
    """

    def __init__(self, path: Path | None = None, fetch: Callable | None = None,
                 clock: Callable[[], float] = time.time):
        self.path = path
        self._fetch = fetch or (lambda url: http_get(url))
        self._clock = clock
        self.routes: dict[str, list] = {}   # callsign -> [asked, origin, dest, name]
        self.aircraft: dict[str, list] = {}  # hex -> [asked, type, registration]
        self._busy_until = 0.0
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.routes = {k: list(v) for k, v in data.get("routes", {}).items()}
            self.aircraft = {k: list(v)
                             for k, v in data.get("aircraft", {}).items()}
        except (OSError, ValueError, AttributeError):
            log.debug("adsb cache unreadable; starting empty", exc_info=True)

    def save(self) -> None:
        if self.path is None:
            return
        now = self._clock()
        data = {
            "routes": {k: v for k, v in self.routes.items()
                       if now - v[0] < (ROUTE_TTL_S if v[1] else UNKNOWN_TTL_S)},
            "aircraft": {k: v for k, v in self.aircraft.items()
                         if now - v[0] < TYPE_TTL_S},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            log.debug("could not write the adsb cache", exc_info=True)

    def route(self, callsign: str) -> Route | None:
        got = self.routes.get(callsign)
        if not got or not got[1]:
            return None
        return Route(got[1], got[2], got[3] if len(got) > 3 else "")

    def type_of(self, flight: Flight) -> str:
        if flight.type:
            return flight.type
        got = self.aircraft.get(flight.hex)
        return got[1] if got else ""

    def registration_of(self, flight: Flight) -> str:
        if flight.registration:
            return flight.registration
        got = self.aircraft.get(flight.hex)
        return got[2] if got and len(got) > 2 else ""

    def _route_due(self, f: Flight, now: float) -> bool:
        if not f.airline:
            return False
        got = self.routes.get(f.callsign)
        if got is None:
            return True
        ttl = ROUTE_TTL_S if got[1] else UNKNOWN_TTL_S
        return now - got[0] >= ttl

    def _type_due(self, f: Flight, now: float) -> bool:
        if f.type and f.registration:
            return False
        got = self.aircraft.get(f.hex)
        return got is None or now - got[0] >= (TYPE_TTL_S if got[1]
                                                else UNKNOWN_TTL_S)

    def look_up(self, flights, limit: int = LOOKUPS_PER_POLL) -> int:
        """Ask for what is not known yet, ``limit`` requests at most, one
        after the other. Returns how many were made."""
        now = self._clock()
        if now < self._busy_until:
            return 0
        asked = 0
        for f in flights:
            if asked >= limit:
                break
            if self._route_due(f, now):
                asked += 1
                got = self._ask_route(f.callsign)
                if got is None:
                    break
                self.routes[f.callsign] = [now, *got]
            if asked < limit and self._type_due(f, now):
                asked += 1
                got = self._ask_aircraft(f.hex)
                if got is None:
                    break
                self.aircraft[f.hex] = [now, *got]
        if asked:
            self.save()
        return asked

    def _answer(self, url: str):
        """The answer, {} for "not known", None to stop asking for now."""
        try:
            return self._fetch(url)
        except SourceError as exc:
            if exc.status == 404:
                return {}
            if exc.status == 429:
                self._busy_until = self._clock() + BUSY_S
            else:
                self._busy_until = self._clock() + DOWN_S
            return None

    def _ask_route(self, callsign: str):
        data = self._answer(ROUTE_URL.format(callsign=callsign))
        if data is None:
            return None
        try:
            r = data["response"]["flightroute"]
            dest = r["destination"]
            return [str(r["origin"].get("icao_code") or "").upper(),
                    str(dest.get("icao_code") or "").upper(),
                    str(dest.get("municipality") or "")]
        except (KeyError, TypeError, AttributeError):
            return ["", "", ""]

    def _ask_aircraft(self, hex_: str):
        data = self._answer(AIRCRAFT_URL.format(hex=hex_))
        if data is None:
            return None
        try:
            a = data["response"]["aircraft"]
            return [str(a.get("icao_type") or "").upper(),
                    str(a.get("registration") or "").upper()]
        except (KeyError, TypeError, AttributeError):
            return ["", ""]

    def complete(self, flight: Flight) -> Flight:
        """The flight with the type and registration filled in from here."""
        return replace(flight, type=self.type_of(flight),
                       registration=self.registration_of(flight))


# --------------------------------------------------------------------------
# off the engine thread
# --------------------------------------------------------------------------


class LiveFeedThread:
    """Asks the feed every ``poll_s`` from a thread of its own, round
    wherever ``centre()`` says the pilot is, and keeps the last answer for
    the engine to read. ``centre`` returning None skips a round: no position,
    nothing sent."""

    def __init__(self, feed: Feed, book: FlightBook | None,
                 centre: Callable[[], tuple | None], poll_s: float = POLL_S,
                 wall: Callable[[], float] = time.time):
        self.feed = feed
        self.book = book
        self.centre = centre
        self._wall = wall
        self.poll_s = max(float(poll_s), POLL_S)
        self._lock = threading.Lock()
        self._flights: list[Flight] = []
        self._routes: dict[str, tuple] = {}
        # When the flights held were answered, on the wall clock the
        # positions are timed by; None before any answer.
        self._answered_wall: float | None = None
        self._state = feed.state()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="live-traffic")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self.book is not None:
            self.book.save()

    def round(self) -> FeedState:
        """One request (and its look-ups), on whatever thread calls it."""
        where = self.centre()
        if where is None:
            return self.state
        before = self.feed.answered_at
        state = self.feed.poll(*where)
        flights = list(self.feed.flights)
        routes: dict[str, tuple] = {}
        if self.book is not None and flights:
            try:
                self.book.look_up(flights)
                flights = [self.book.complete(f) for f in flights]
                for f in flights:
                    route = self.book.route(f.callsign)
                    if route is not None:
                        routes[f.hex] = (route.origin, route.destination)
            except Exception:                       # pragma: no cover - defensive
                log.debug("adsbdb look-up failed", exc_info=True)
        with self._lock:
            self._state = state
            if self.feed.answered_at != before:
                # Only an answer replaces what is held: a round in which
                # nobody answered leaves the last one to be carried on.
                self._flights, self._routes = flights, routes
                self._answered_wall = self._wall()
        return state

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.round()
            except Exception:                       # pragma: no cover - defensive
                log.debug("live traffic round failed", exc_info=True)
            self._stop.wait(max(0.0, self.poll_s - (time.monotonic() - started)))

    @property
    def flights(self) -> list[Flight]:
        with self._lock:
            return list(self._flights)

    @property
    def routes(self) -> dict[str, tuple]:
        """hex -> (origin, destination), from adsbdb."""
        with self._lock:
            return dict(self._routes)

    @property
    def answered_wall(self) -> float | None:
        with self._lock:
            return self._answered_wall

    @property
    def state(self) -> FeedState:
        with self._lock:
            return self._state


__all__ = ["Flight", "Feed", "FeedState", "FlightBook", "LiveFeedThread",
           "Route", "SourceError", "parse", "sources_for", "LIVE", "EMPTY",
           "DOWN", "STARTING", "USER_AGENT", "POLL_S"]
