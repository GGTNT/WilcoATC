"""The real aeroplanes in the roster, and the invented ones in the gaps.

Why this exists
---------------

The feed (``integrations/adsb.py``) says where the real aeroplanes are. This
turns them into what the traffic already works: each live flight is handed
to :meth:`TrafficInjector.set_observed` beside FSLTL's, as one more
aeroplane somebody else is flying, so it holds the runway, takes its place
in the landing sequence and is on the strips with no second traffic system
behind it. A live flight is one more way to fill the roster.

Two things are decided here.

**What counts against what.** The field's density (``fleet.py``, the
injector's in-play figure) is how many airliners an airline field has
moving at once, or how many light aircraft a general-aviation field has. A
real aeroplane counts against the invented ones only in its own category --
``airline``, ``cargo`` or ``ga`` -- because counted in a raw number, a
couple of real Cessnas crossing a big airport's zone would stop the invented
airliners filling its stands (the pilot's own rule, 30 Oct 2026). And only
what works the field counts: ``set_observed`` already leaves out what is
overflying.

**What happens when the feed goes quiet.** For ``fallback_after_s`` (60 s)
nothing changes: the live flights are carried on from their last position,
so pulling the feed is not heard. After that they leave the roster and the
invented traffic takes the whole density back, at its own pace, except an
arrival low on final near the field: that one is flown down the final to
the runway and off it, rather than vanishing a mile short (after LocalTC,
which lands one "that goes quiet low on final" within 15 nm). When the feed
comes back the live flights rejoin, and the invented ones over the density
finish what they are doing rather than being taken away.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..navdata.build import bearing_deg, haversine_nm, project
from . import fleet as fleets
from .operators import FREIGHT, by_icao

AIRLINE, CARGO, GA = "airline", "cargo", "ga"
CATEGORIES = (AIRLINE, CARGO, GA)

# Operators that fly only freight. The invented traffic's freight carriers
# are the ones with the FREIGHT fleet; these are the real ones a feed is
# likely to carry round the reference fields that the table does not have.
CARGO_ICAO = frozenset({
    "FDX", "UPS", "GEC", "BOX", "DHK", "BCS", "DHX", "CLX", "GTI", "ABW",
    "CKS", "ABX", "ATN", "SWN", "MPH", "TAY", "NPT", "QAC", "AHK", "CAO",
    "CKK", "CLU", "ICV", "KZR", "PAC", "SQC", "WGN", "EAT", "FPO", "RCF",
})
# ADS-B emitter categories that are never an airliner: light and small
# aircraft, rotorcraft, gliders, balloons, ultralights.
LIGHT_CATEGORIES = frozenset({"A1", "A2", "A7", "B1", "B2", "B4", "B6"})

# How long a position is carried on without a new one (after LocalTC: a
# minute in the air, a few seconds on the ground).
CARRY_AIR_S = 60.0
CARRY_GROUND_S = 8.0
# Who is landed by us when the feed goes quiet (after LocalTC: within 15 nm),
# and how low and how pointed at the field it has to be to be on final
# rather than passing. UNVERIFIED: the height and the angle, chosen.
LAND_WITHIN_NM = 15.0
LAND_BELOW_FT = 5000.0
LAND_POINTED_DEG = 40.0
# Down the final on the usual three degrees, and off the runway at this.
GLIDE_FT_PER_NM = 318.0
VACATED_KT = 20.0
DECELERATE_KT_S = 3.0
# One landed by us leaves the roster this long after it has slowed down.
LANDED_GONE_S = 60.0
# The simulator's object ids are small; a live flight's is its address,
# moved clear of them.
LIVE_ID_BASE = 1 << 32
# How much of each aeroplane's past is kept beyond the delay it is drawn at.
HISTORY_S = 60.0
# One not heard again is let go this long after it can no longer be carried.
GONE_AFTER_S = 5.0
# On the ground: moving at all, and pushed back -- slow, and going the other
# way from the nose -- which keeps the nose where it was rather than turning
# the aeroplane round to face its push (after LocalTC: the nose is the
# heading, "kept while it's pushed back"). UNVERIFIED: the figures.
MOVING_KT = 3.0
PUSHBACK_KT = 8.0
PUSHBACK_DEG = 120.0


def _wrap(angle: float) -> float:
    return (angle + 540.0) % 360.0 - 180.0


@dataclass(frozen=True)
class Sample:
    """One report of one aeroplane, with the nose worked out."""

    t: float
    lat: float
    lon: float
    alt: float | None
    track: float
    gs: float
    vs: float
    on_ground: bool
    heading: float

    @classmethod
    def of(cls, f, before: "Sample | None") -> "Sample":
        heading = f.track
        if f.on_ground and before is not None:
            if f.gs_kt < MOVING_KT:
                heading = before.heading        # standing: where it pointed
            elif (f.gs_kt < PUSHBACK_KT
                  and abs(_wrap(f.track - before.heading)) > PUSHBACK_DEG):
                heading = before.heading        # pushed back, tail first
        return cls(f.t, f.lat, f.lon, f.alt_ft, f.track, f.gs_kt,
                   f.vs_fpm, f.on_ground, heading)


def _between(a: Sample, b: Sample, t: float, elevation: float) -> tuple:
    """Where it was at ``t`` between reports ``a`` and ``b``: a cubic
    Hermite through both, with each one's own speed and track, so the path
    is a curve and its speed what it was flying."""
    span = max(1e-3, b.t - a.t)
    u = min(1.0, max(0.0, (t - a.t) / span))
    m_per_deg = 111_320.0
    cos = math.cos(math.radians(a.lat))
    bx = (b.lon - a.lon) * m_per_deg * cos
    by = (b.lat - a.lat) * m_per_deg

    def velocity(s: Sample) -> tuple[float, float]:
        ms = s.gs * 0.514444
        rad = math.radians(s.track)
        return ms * math.sin(rad) * span, ms * math.cos(rad) * span

    (vax, vay), (vbx, vby) = velocity(a), velocity(b)
    h10 = u ** 3 - 2 * u ** 2 + u
    h01 = -2 * u ** 3 + 3 * u ** 2
    h11 = u ** 3 - u ** 2
    if a.on_ground or b.on_ground:
        # On a taxiway the speeds are too rough to steer by; straight between
        # the two, and the taxiway itself does the rest.
        x, y = bx * u, by * u
    else:
        x = h10 * vax + h01 * bx + h11 * vbx
        y = h10 * vay + h01 * by + h11 * vby
    lat = a.lat + y / m_per_deg
    lon = a.lon + x / (m_per_deg * cos)
    on_ground = a.on_ground if u < 0.5 else b.on_ground
    if on_ground:
        alt = elevation
    else:
        alt_a = a.alt if a.alt is not None else elevation
        alt_b = b.alt if b.alt is not None else elevation
        alt = alt_a + (alt_b - alt_a) * u
    heading = (a.heading + _wrap(b.heading - a.heading) * u) % 360.0
    gs = a.gs + (b.gs - a.gs) * u
    vs = 0.0 if on_ground else a.vs + (b.vs - a.vs) * u
    return lat, lon, alt, heading, gs, vs, on_ground


def _as_flight(one: "LiveSeen"):
    """A drawn position, in the shape _on_final reads."""
    from types import SimpleNamespace

    return SimpleNamespace(
        on_ground=one.on_ground,
        alt_ft=None if one.on_ground else one.altitude_ft,
        lat=one.latitude, lon=one.longitude, track=one.heading)


def category_of(callsign: str, type_code: str = "",
                adsb_category: str = "", operator=None) -> str:
    """``airline``, ``cargo`` or ``ga`` for one aeroplane."""
    written = (callsign or "").strip().upper()
    airline_code = (written[:3] if len(written) >= 4 and written[:3].isalpha()
                    and written[3].isdigit() else "")
    if operator is None and airline_code:
        operator = by_icao(airline_code)
    if airline_code in CARGO_ICAO or (operator is not None
                                      and operator.fleet == FREIGHT):
        return CARGO
    light = (adsb_category in LIGHT_CATEGORIES
             or (type_code or "").upper() in fleets.GA_TYPES)
    if airline_code and not light:
        return AIRLINE
    return GA


def flight_category(flight) -> str:
    """The category of a strip (traffic.Flight), invented or not."""
    known = getattr(flight, "category", "")
    if known:
        return known
    return category_of(flight.written, flight.type_code,
                       operator=getattr(flight, "operator", None))


def field_categories(kind) -> tuple[str, ...]:
    """What a field's invented traffic is: airliners and freighters at an
    airline field, light aircraft at the others."""
    if kind is None or kind.airline:
        return (AIRLINE, CARGO)
    return (GA,)


def cargo_share(roster) -> float:
    """How much of an airline field's invented traffic is freight, from the
    weights the roster draws its operators with."""
    total = sum(c.weight for c in roster)
    if total <= 0:
        return 0.0
    return sum(c.weight for c in roster if c.operator.fleet == FREIGHT) / total


def wanted_by_category(kind, total: int, roster=()) -> dict[str, int]:
    """The field's density split by category."""
    out = {c: 0 for c in CATEGORIES}
    if kind is None or kind.airline:
        cargo = int(round(total * cargo_share(roster)))
        out[CARGO] = cargo
        out[AIRLINE] = total - cargo
    else:
        out[GA] = total
    return out


def room(wanted: dict, counted: dict) -> dict[str, int]:
    """How many more invented aeroplanes each category has room for."""
    return {c: max(0, wanted.get(c, 0) - counted.get(c, 0))
            for c in CATEGORIES}


@dataclass
class LiveSeen:
    """A live flight in the shape set_observed reads (simaircraft.Seen)."""

    object_id: int
    callsign: str
    type_code: str
    latitude: float
    longitude: float
    altitude_ft: float
    heading: float
    ground_speed_kt: float
    vertical_speed_fpm: float
    on_ground: bool
    registration: str = ""
    title: str = ""
    at: float = 0.0
    source: str = "live"
    category: str = ""
    origin: str = ""
    destination: str = ""
    hex: str = ""
    # Flown down the final by us, the feed having gone quiet.
    landed_by_us: bool = False
    # Where the real one is now, ``delay_s`` ahead of what is drawn: what it
    # is about to do, for the controllers (Phase 4).
    lead: "LiveSeen | None" = None
    # Which way it is moving, which on the ground is not always the way it
    # points: a pushback goes tail first.
    track: float = 0.0
    # Held by our tower (LiveRoster.hold): drawn standing where it was.
    held: bool = False
    squawk: str = ""
    # Which source the feed had it from, for logs radio.
    feed: str = ""

    @property
    def positioned(self) -> bool:
        return self.latitude != 0.0 or self.longitude != 0.0


def written_callsign(flight) -> str:
    """What the radio calls it: the callsign sent, else the registration
    with its dash taken out, which is how a light aeroplane is called."""
    written = (flight.callsign or "").strip().upper()
    if written:
        return written
    return (flight.registration or "").replace("-", "").strip().upper()


class LiveRoster:
    """The live flights as the traffic sees them, held through a quiet feed.

    ``update`` takes each answer of the feed; ``seen`` gives, for a moment,
    the flights to hand to ``set_observed``. Wall-clock time throughout,
    because a position is timed by the source's clock.

    **Shown behind the feed** (``delay_s``, 30 s in a session, after
    LocalTC): each aeroplane is drawn where it was ``delay_s`` ago, between
    two positions already heard, never guessed into the unknown. So a new
    flight appears ``delay_s`` after it is first heard, a position that
    arrives late moves nothing that is already drawn, and what it is about
    to do is known before it does it (``LiveSeen.lead``, where the real one
    is now, for the controllers). Between two reports the path is a curve
    through both with each one's own speed and track (a cubic Hermite), so a
    new report is never a correction to ease out and the aeroplane never
    looks faster or slower than it was flying.
    """

    def __init__(self, fallback_after_s: float = 60.0, delay_s: float = 0.0):
        self.fallback_after_s = float(fallback_after_s)
        self.delay_s = max(0.0, float(delay_s))
        self.flights: dict[str, object] = {}     # hex -> latest adsb.Flight
        self.history: dict[str, list[Sample]] = {}
        self.routes: dict[str, tuple] = {}       # hex -> (origin, destination)
        self.answered_at: float | None = None
        self.carried: dict[str, LiveSeen] = {}   # landed by us
        self._landed_at: dict[str, float] = {}
        # Held by our tower or giving way (hold/release): when each was
        # stopped, and how far behind the feed each has fallen by holds
        # already over.
        self._held: dict[str, tuple[float, float]] = {}   # hex -> (now, shown)
        self._behind: dict[str, float] = {}

    # -- held by our tower -------------------------------------------------

    def hold(self, hex_: str, now: float) -> None:
        """Stop drawing ``hex_`` where it is: held short by our tower, or
        giving way to the pilot. Its history is kept, so when it is let go
        it carries on from there, only further behind the feed."""
        if hex_ not in self._held and hex_ in self.history:
            self._held[hex_] = (now, self._shown_at(hex_, now))

    def release(self, hex_: str, now: float) -> None:
        held = self._held.pop(hex_, None)
        if held is not None:
            self._behind[hex_] = self._behind.get(hex_, 0.0) + (now - held[0])

    def is_held(self, hex_: str) -> bool:
        return hex_ in self._held

    def _shown_at(self, hex_: str, now: float) -> float:
        held = self._held.get(hex_)
        if held is not None:
            return held[1]
        return now - self.delay_s - self._behind.get(hex_, 0.0)

    # -- the feed ------------------------------------------------------

    def update(self, flights, answered_at: float | None, routes=None) -> None:
        """The feed's last answer, and when it came. An answer already
        taken is ignored; a newer one adds each aeroplane's position to its
        history."""
        if answered_at is None or answered_at == self.answered_at:
            return
        if not flights and self.flights:
            # Nobody at all, a moment after a sky full: a source's bad
            # answer far more often than an empty sky, so it is taken as
            # silence -- held through the minute -- rather than emptying
            # the frequency at once.
            return
        self.answered_at = answered_at
        heard = {f.hex: f for f in flights}
        for hex_, f in heard.items():
            samples = self.history.setdefault(hex_, [])
            if samples and f.t <= samples[-1].t + 0.2:
                # The same report again (a source repeats a position it has
                # not refreshed): nothing new to draw through.
                continue
            samples.append(Sample.of(f, samples[-1] if samples else None))
            # What is no longer needed to draw it delay_s behind -- or as
            # far behind as holds have put it, or where it is held.
            keep_after = (min(f.t - self.delay_s - self._behind.get(hex_, 0.0),
                              self._held.get(hex_, (0.0, math.inf))[1])
                          - HISTORY_S)
            while len(samples) > 2 and samples[1].t < keep_after:
                samples.pop(0)
        self.flights.update(heard)
        self.routes.update(routes or {})
        # Heard again: it is theirs to fly once more.
        for hex_ in list(self.carried):
            if hex_ in heard:
                del self.carried[hex_]
                self._landed_at.pop(hex_, None)

    def quiet_for(self, now: float) -> float:
        return math.inf if self.answered_at is None else now - self.answered_at

    def falling_back(self, now: float) -> bool:
        return self.quiet_for(now) > self.fallback_after_s

    # -- what the traffic is handed --------------------------------------

    def seen(self, now: float, field) -> list[LiveSeen]:
        """The live flights at ``now``, each where it was ``delay_s`` ago.

        ``field`` is the injector's field (latitude, longitude,
        elevation_ft), or None away from any.
        """
        shown_at = now - self.delay_s
        if not self.falling_back(now):
            out = []
            for hex_ in list(self.history):
                at = self._shown_at(hex_, now)
                one = self._at(hex_, at, field)
                if one is None:
                    continue
                if one == "gone":
                    if hex_ in self._held:
                        continue          # held: kept however long it takes
                    self._forget(hex_)
                    continue
                if hex_ in self._held:
                    one.ground_speed_kt, one.vertical_speed_fpm = 0.0, 0.0
                    one.held = True
                # What it will be doing when it is drawn delay_s from now:
                # its own timeline, so a held one is still announced ahead.
                lead = self._at(hex_, at + self.delay_s, field)
                one.lead = lead if isinstance(lead, LiveSeen) else None
                out.append(one)
            return out
        if field is not None and self.history:
            # The moment the fallback starts: who is on final, from where
            # they are drawn.
            for hex_ in list(self.history):
                one = self._at(hex_, shown_at, field)
                if isinstance(one, LiveSeen) and hex_ not in self.carried \
                        and self._on_final(_as_flight(one), field):
                    one.landed_by_us = True
                    self.carried[hex_] = one
            for hex_ in list(self.history):
                self._forget(hex_)
        return [self._land(one, now, field) for one in list(self.carried.values())
                if not self._gone(one, now)]

    def _forget(self, hex_: str) -> None:
        self.history.pop(hex_, None)
        self.flights.pop(hex_, None)
        self._held.pop(hex_, None)
        self._behind.pop(hex_, None)

    def _at(self, hex_: str, t: float, field):
        """Where ``hex_`` was at ``t``: between the two reports either side,
        carried on past the last for a minute in the air and a few seconds
        on the ground, then held; None before its first report, "gone" once
        it has not been heard for longer than it can be carried."""
        samples = self.history.get(hex_) or []
        f = self.flights.get(hex_)
        if not samples or f is None:
            return "gone"
        if t < samples[0].t:
            return None
        elevation = getattr(field, "elevation_ft", 0.0) if field else 0.0
        later = next((i for i, s in enumerate(samples) if s.t > t), None)
        if later is None:
            last = samples[-1]
            horizon = CARRY_GROUND_S if last.on_ground else CARRY_AIR_S
            if t - last.t > horizon + self.delay_s + GONE_AFTER_S:
                return "gone"
            dt = min(t - last.t, horizon)
            lat, lon = last.lat, last.lon
            if last.gs > 0 and dt > 0:
                lat, lon = project(lat, lon, last.track, last.gs * dt / 3600.0)
            alt = (elevation if last.on_ground or last.alt is None
                   else last.alt + last.vs * dt / 60.0)
            pose = (lat, lon, alt, last.heading, last.gs,
                    0.0 if last.on_ground else last.vs, last.on_ground)
            track = last.track
        else:
            a, b = samples[later - 1], samples[later]
            pose = _between(a, b, t, elevation)
            u = min(1.0, max(0.0, (t - a.t) / max(1e-3, b.t - a.t)))
            track = (a.track + _wrap(b.track - a.track) * u) % 360.0
        lat, lon, alt, heading, gs, vs, on_ground = pose
        written = written_callsign(f)
        origin, destination = self.routes.get(hex_, ("", ""))
        return LiveSeen(
            object_id=LIVE_ID_BASE + _address(hex_),
            callsign=written, type_code=f.type,
            latitude=lat, longitude=lon, altitude_ft=alt,
            heading=heading, ground_speed_kt=gs, vertical_speed_fpm=vs,
            on_ground=on_ground, registration=f.registration,
            at=t, category=category_of(written, f.type, f.category),
            origin=origin, destination=destination, hex=hex_, track=track,
            squawk=f.squawk)

    @staticmethod
    def _on_final(f, field) -> bool:
        """Low, near and pointed at the field: the one that would otherwise
        vanish a mile short of the runway."""
        if f.on_ground or f.alt_ft is None:
            return False
        to_go = haversine_nm(f.lat, f.lon, field.latitude, field.longitude)
        if to_go > LAND_WITHIN_NM:
            return False
        if f.alt_ft - field.elevation_ft > LAND_BELOW_FT:
            return False
        towards = bearing_deg(f.lat, f.lon, field.latitude, field.longitude)
        off = abs((f.track - towards + 540.0) % 360.0 - 180.0)
        return off <= LAND_POINTED_DEG

    def _land(self, one: LiveSeen, now: float, field) -> LiveSeen:
        """Flown on from where it was last heard: down the final at three
        degrees to the field, then slowed to taxi speed on the runway."""
        dt = max(0.0, now - one.at)
        if dt <= 0 or field is None:
            return one
        if not one.on_ground:
            to_go = haversine_nm(one.latitude, one.longitude,
                                 field.latitude, field.longitude)
            step = one.ground_speed_kt * dt / 3600.0
            # Steered at the field as it goes, so it arrives rather than
            # passes it by the few degrees it was pointed off.
            one.heading = bearing_deg(one.latitude, one.longitude,
                                      field.latitude, field.longitude)
            if step >= to_go:
                one.latitude, one.longitude = field.latitude, field.longitude
                one.altitude_ft = field.elevation_ft
                one.on_ground = True
                one.vertical_speed_fpm = 0.0
                self._landed_at.setdefault(one.hex, now)
            else:
                one.latitude, one.longitude = project(
                    one.latitude, one.longitude, one.heading, step)
                glide = field.elevation_ft + (to_go - step) * GLIDE_FT_PER_NM
                one.altitude_ft = max(field.elevation_ft,
                                      min(one.altitude_ft, glide))
                one.vertical_speed_fpm = -one.ground_speed_kt * GLIDE_FT_PER_NM / 60.0
        else:
            slowed = max(VACATED_KT, one.ground_speed_kt - DECELERATE_KT_S * dt)
            one.latitude, one.longitude = project(
                one.latitude, one.longitude, one.heading,
                (one.ground_speed_kt + slowed) / 2.0 * dt / 3600.0)
            one.ground_speed_kt = slowed
        one.at = now
        one.landed_by_us = True
        return one

    def _gone(self, one: LiveSeen, now: float) -> bool:
        landed = self._landed_at.get(one.hex)
        if landed is None or now - landed < LANDED_GONE_S:
            return False
        self.carried.pop(one.hex, None)
        return True


def _address(hex_: str) -> int:
    try:
        return int(hex_, 16)
    except ValueError:
        return abs(hash(hex_)) % (1 << 24)


__all__ = ["AIRLINE", "CARGO", "GA", "CATEGORIES", "LiveRoster", "LiveSeen",
           "category_of", "flight_category", "field_categories",
           "wanted_by_category", "room", "cargo_share", "written_callsign"]
