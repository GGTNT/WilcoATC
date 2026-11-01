"""The filed route as a controller uses it: a line on the chart, not a list
of names.

Before this, the plan was kept as SimBrief's raw JSON and read in two
places for two lists -- (lat, lon) points for the en-route look-ahead and
named fixes for "proceed direct" -- and everything else the controller said
was measured as a great circle to the airport's reference point: when to
start the descent, when to hand to approach, how far there was still to go.
An aeroplane on a STAR that doglegs forty miles round a TMA is not forty
miles nearer for being abeam the field.

So the route is data here: every fix in order with its coordinates, the
altitude the plan crosses it at, whether it is climb, cruise or descent, the
planned time from takeoff, and the airway, SID or STAR it is reached by. On
top of that, the questions a controller asks of a plan (after LocalTC's
``route.py``, read for the design only):

- where the climb ends and the descent starts: the plan's own ``TOC`` and
  ``TOD`` when it names them, else the last climb fix and the fix before the
  first descending one;
- the arrival floor: the last altitude the plan descends to before the
  airport, rounded up to the thousand -- where "descend via" ends;
- the fixes of a SID or a STAR by name;
- how many minutes after departure the cruise is reached (the plan's TOC
  time, else its distance at a climb groundspeed, else an average rate),
  clamped to what "expect FL370 N minutes after departure" can say;
- how far out the descent starts;
- and where the aeroplane is along the route: the active leg, how far off
  it, the distance still to fly *along the route*, the next fix and the time
  to each fix still ahead.

Three loaders, in the order a flight prefers them: SimBrief's navigation log
(every fix with its coordinates), the simulator's flight plan file (``.pln``,
which in MSFS 2024 names each fix and its region but carries no coordinates,
so each is resolved), and a route string typed by hand, resolved the same
way. Resolution is the simulator's facility data first and the navaids and
airports in ``nav.sqlite`` after it; the database has no intersections and
no airways, so offline a string resolves only its navaids and an airway is
flown direct between the two fixes named either side of it -- said in the
route's ``notes`` rather than hidden.
"""

from __future__ import annotations

import logging
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable

from ..navdata.build import bearing_deg, haversine_nm

log = logging.getLogger(__name__)

CLB, CRZ, DSC = "CLB", "CRZ", "DSC"

# Without a plan's times: an average climb rate by where the cruise is, jets
# up high and turboprops and pistons lower (after LocalTC: 1,800 fpm to a
# cruise at or above 24,000 ft, 1,000 from 12,000, 600 below).
CLIMB_FPM = ((24000, 1800.0), (12000, 1000.0), (0, 600.0))
# The descent rule of thumb, three miles a thousand feet, and never a descent
# that starts inside thirty miles (after LocalTC).
TOD_NM_PER_1000FT = 3.0
TOD_MIN_NM = 30.0
# What "expect FL370 N minutes after departure" can sensibly say (after
# LocalTC): under five is not worth saying, over thirty is not a climb.
MINUTES_RANGE = (5, 30)


def climb_groundspeed(cruise_ft: float) -> float:
    """A typical average groundspeed over the whole climb, knots (after
    LocalTC: 330 to a cruise in the high levels, 200 to the middle, 110 for
    a light aeroplane)."""
    return 330.0 if cruise_ft >= 24000 else 200.0 if cruise_ft >= 12000 \
        else 110.0


@dataclass(frozen=True)
class RouteFix:
    """One point of the route."""

    ident: str
    lat: float
    lon: float
    alt_ft: int = 0
    stage: str = ""        # CLB, CRZ or DSC; "" when the plan does not say
    time_s: int = 0        # planned seconds from takeoff; 0 when not given
    via: str = ""          # airway, SID or STAR it is reached by, "DCT", ""
    kind: str = ""         # wpt, vor, ndb, apt, ltlg (a plan's TOC/TOD)
    region: str = ""       # the ICAO region the ident is unique in
    on_procedure: bool = False   # SimBrief's is_sid_star

    @property
    def is_marker(self) -> bool:
        """Not a fix anybody is cleared to: the plan's top of climb and
        descent, and the airports at either end."""
        ident = self.ident.upper()
        return ident in ("TOC", "TOD") or self.kind == "apt" \
            or (len(ident) == 4 and ident.isalpha() and self.kind in ("", "apt"))


@dataclass(frozen=True)
class Progress:
    """Where the aeroplane is along the route, at one moment."""

    leg: int                 # index of the fix the active leg runs *to*
    from_fix: RouteFix
    to_fix: RouteFix
    cross_track_nm: float    # positive right of the leg, negative left
    along_leg_nm: float      # flown of the active leg so far
    to_go_nm: float          # still to fly along the route to its last fix
    to_next_nm: float        # to the end of the active leg
    # (ident, seconds) for every fix still ahead, at the groundspeed given
    eta_s: tuple[tuple[str, float], ...] = ()

    @property
    def next_fix(self) -> RouteFix:
        return self.to_fix

    @property
    def leg_name(self) -> str:
        return f"{self.from_fix.ident}-{self.to_fix.ident}"


@dataclass
class Route:
    """The filed route, and what a controller works out from it."""

    fixes: tuple[RouteFix, ...] = ()
    origin: str = ""
    destination: str = ""
    alternate: str = ""
    sid: str = ""
    sid_runway: str = ""
    star: str = ""
    star_transition: str = ""
    star_runway: str = ""
    approach: str = ""        # e.g. "ILS W 16L" as the plan names it
    cruise_ft: int = 0
    source: str = ""          # simbrief, pln, string, sim
    # What the loader could not do, said rather than hidden: a fix nobody
    # could place, an airway flown direct for want of its points.
    notes: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return len(self.fixes) >= 2

    # ------------------------------------------------------------------
    # the plan's own answers
    # ------------------------------------------------------------------

    @property
    def top_of_climb(self) -> RouteFix | None:
        named = next((f for f in self.fixes if f.ident.upper() == "TOC"), None)
        return named or next(
            (f for f in reversed(self.fixes) if f.stage == CLB), None)

    @property
    def top_of_descent(self) -> RouteFix | None:
        named = next((f for f in self.fixes if f.ident.upper() == "TOD"), None)
        if named is not None:
            return named
        first_down = next((i for i, f in enumerate(self.fixes)
                           if f.stage == DSC), None)
        return self.fixes[first_down - 1] if first_down else None

    @property
    def arrival_floor_ft(self) -> int | None:
        """The last altitude the plan descends to before the airport: where
        "descend via" ends. Not the airport itself, which a SimBrief plan
        lists last at circuit height, but the arrival's last fix."""
        descent = [f.alt_ft for f in self.fixes
                   if f.stage == DSC and f.alt_ft > 0 and not f.is_marker]
        if not descent:
            return None
        return int(math.ceil(descent[-1] / 1000.0) * 1000)

    def procedure(self, name: str | None, stage: str) -> tuple[RouteFix, ...]:
        """The fixes of a SID (``stage`` CLB) or a STAR (DSC), in order.

        A plan that kept each fix's ``via`` names them. One that did not has
        only the stages: a SID is what is climbed through before the top of
        climb, a STAR what is flown after the top of descent.
        """
        if not name:
            return ()
        if any(f.via for f in self.fixes):
            return tuple(f for f in self.fixes
                         if f.via.upper() == name.upper() and not f.is_marker)
        if stage == CLB:
            end = next((i for i, f in enumerate(self.fixes)
                        if f.stage != CLB or f.ident.upper() == "TOC"),
                       len(self.fixes))
            return tuple(f for f in self.fixes[:end] if not f.is_marker)
        top = self.top_of_descent
        start = self.fixes.index(top) + 1 if top is not None \
            else len(self.fixes)
        return tuple(f for f in self.fixes[start:]
                     if f.stage == DSC and not f.is_marker)

    def minutes_to_cruise(self, cruise_ft: float | None = None) -> int:
        """Minutes from takeoff to the top of the climb."""
        cruise = float(cruise_ft or self.cruise_ft or 0)
        toc = self.top_of_climb
        if toc is not None and toc.time_s > 0:
            minutes = toc.time_s / 60.0
        elif toc is not None and len(self.fixes) > 1:
            minutes = (self._along(0, self.fixes.index(toc))
                       / climb_groundspeed(cruise) * 60.0)
        else:
            rate = next(fpm for floor, fpm in CLIMB_FPM if cruise >= floor)
            minutes = cruise / rate
        low, high = MINUTES_RANGE
        return int(min(max(round(minutes), low), high))

    def descent_distance_nm(self, dest_lat: float, dest_lon: float,
                            cruise_ft: float | None = None,
                            elev_ft: float = 0.0) -> float:
        """How far from the destination the descent starts: along the route
        from the plan's top of descent when it has one, else three miles a
        thousand feet, never under thirty."""
        tod = self.top_of_descent
        if tod is not None and tod in self.fixes:
            i = self.fixes.index(tod)
            last = self.fixes[-1]
            along = self._along(i, len(self.fixes) - 1)
            # The plan's last point is the airport, or near enough; if it is
            # not, the rest of the way is flown direct.
            along += haversine_nm(last.lat, last.lon, dest_lat, dest_lon)
            return along
        cruise = float(cruise_ft or self.cruise_ft or 0)
        return max(TOD_MIN_NM, (cruise - elev_ft) / 1000.0 * TOD_NM_PER_1000FT)

    # ------------------------------------------------------------------
    # where the aeroplane is on it
    # ------------------------------------------------------------------

    def progress(self, lat: float, lon: float, groundspeed_kt: float = 0.0,
                 hint: int = 1) -> Progress | None:
        """The active leg and the distance still to fly along the route.

        The leg is the one the aeroplane is nearest to across track, among
        the legs it lies alongside, searched from ``hint`` (the last active
        leg) onwards first: a route that comes back on itself -- a STAR
        that runs downwind past the field it started abeam -- must not snap
        the aeroplane back to the leg it flew twenty minutes ago.
        """
        fixes = self.fixes
        if len(fixes) < 2:
            return None
        hint = max(1, min(int(hint or 1), len(fixes) - 1))
        best = None
        for i in range(1, len(fixes)):
            a, b = fixes[i - 1], fixes[i]
            along, across, length = _leg_position(a, b, lat, lon)
            if length < 0.01:
                continue
            # Off either end of the leg, the distance is to its nearer end.
            if along < 0:
                distance = math.hypot(along, across)
            elif along > length:
                distance = math.hypot(along - length, across)
            else:
                distance = abs(across)
            # A leg already flown counts as further away than it is, so a
            # loop in the route is resolved forwards; the leg just behind the
            # hint is allowed, for a position read a moment before the turn.
            if i < hint - 1:
                distance += 50.0
            key = (distance, i < hint)
            if best is None or key < best[0]:
                best = (key, i, along, across, length)
        if best is None:
            return None
        _, i, along, across, length = best
        a, b = fixes[i - 1], fixes[i]
        flown = min(max(along, 0.0), length)
        to_next = length - flown
        to_go = to_next + self._along(i, len(fixes) - 1)
        etas = []
        if groundspeed_kt > 30:
            running = to_next
            etas.append((b.ident, running / groundspeed_kt * 3600.0))
            for j in range(i + 1, len(fixes)):
                p, q = fixes[j - 1], fixes[j]
                running += haversine_nm(p.lat, p.lon, q.lat, q.lon)
                etas.append((q.ident, running / groundspeed_kt * 3600.0))
        return Progress(leg=i, from_fix=a, to_fix=b,
                        cross_track_nm=across, along_leg_nm=flown,
                        to_go_nm=to_go, to_next_nm=to_next,
                        eta_s=tuple(etas))

    def fix(self, ident: str) -> RouteFix | None:
        ident = (ident or "").strip().upper()
        return next((f for f in self.fixes if f.ident.upper() == ident), None)

    def points(self) -> list[tuple[float, float]]:
        return [(f.lat, f.lon) for f in self.fixes]

    def described(self) -> str:
        """One line for the log: where from, by what, to where."""
        bits = [self.origin or "?"]
        if self.sid:
            bits.append(self.sid)
        named = [f.ident for f in self.fixes
                 if not f.is_marker and not f.on_procedure]
        if named:
            bits.append(" ".join(named[:12]) + (" ..." if len(named) > 12
                                                else ""))
        if self.star:
            bits.append(self.star)
        bits.append(self.destination or "?")
        return " ".join(bits)

    def _along(self, first: int, last: int) -> float:
        return sum(haversine_nm(a.lat, a.lon, b.lat, b.lon)
                   for a, b in zip(self.fixes[first:last],
                                   self.fixes[first + 1:last + 1]))


def _leg_position(a: RouteFix, b: RouteFix, lat: float, lon: float
                  ) -> tuple[float, float, float]:
    """(along, across, length) of a point against the leg a -> b, in nm.

    Flat-earth about the leg's start is plenty: a leg is at most a few
    hundred miles and the question is which leg and roughly how far off,
    not a geodesic to the metre. Across is positive right of the leg.
    """
    length = haversine_nm(a.lat, a.lon, b.lat, b.lon)
    if length < 1e-6:
        return 0.0, 0.0, 0.0
    course = math.radians(bearing_deg(a.lat, a.lon, b.lat, b.lon))
    distance = haversine_nm(a.lat, a.lon, lat, lon)
    if distance < 1e-6:
        return 0.0, 0.0, length
    angle = math.radians(bearing_deg(a.lat, a.lon, lat, lon)) - course
    return distance * math.cos(angle), distance * math.sin(angle), length


# ----------------------------------------------------------------------
# SimBrief
# ----------------------------------------------------------------------


def from_ofp(plan) -> Route:
    """The route of a SimBrief plan (:class:`integrations.simbrief.Ofp`)."""
    navlog = tuple(getattr(plan, "navlog", ()) or ())
    fixes = tuple(
        RouteFix(ident=f.ident, lat=f.lat, lon=f.lon, alt_ft=f.alt_ft,
                 stage=f.stage, time_s=f.time_s, via=_via(f.via),
                 kind=f.kind, region=f.region, on_procedure=f.on_procedure)
        for f in navlog)
    # SimBrief names the SID's last fix's ``via`` after the SID even where
    # the flag says it is no longer on it (ROUSY on the ROUS1L, measured on
    # an EBBR-LIRF plan): the name is the procedure, so it stays.
    #
    # Its log starts at the SID's first fix and ends on the destination, so
    # the origin is put in front: without it the first leg -- the one the
    # aeroplane is on from the moment it lifts off -- does not exist.
    origin = getattr(plan, "origin_position", None)
    if origin and fixes and fixes[0].ident != plan.origin:
        fixes = (RouteFix(ident=plan.origin, lat=origin[0], lon=origin[1],
                          alt_ft=int(origin[2]), stage=CLB, kind="apt"),) \
            + fixes
    return Route(
        fixes=fixes, origin=getattr(plan, "origin", ""),
        destination=getattr(plan, "destination", ""),
        alternate=getattr(plan, "alternate", ""),
        sid=getattr(plan, "sid", ""),
        sid_runway=getattr(plan, "planned_runway", ""),
        star=getattr(plan, "star", ""),
        star_runway=getattr(plan, "arrival_runway", ""),
        approach=getattr(plan, "approach", ""),
        cruise_ft=int(getattr(plan, "cruise_altitude_ft", 0) or 0),
        source="simbrief")


def _via(name: str) -> str:
    name = (name or "").strip().upper()
    return "" if name in ("DCT", "DIRECT") else name


# ----------------------------------------------------------------------
# the simulator's flight plan file
# ----------------------------------------------------------------------

#: (ident, region, kind) -> (lat, lon), or None when nobody knows the fix.
Resolver = Callable[[str, str, str], "tuple[float, float] | None"]

_PLN_KINDS = {"intersection": "wpt", "vor": "vor", "ndb": "ndb",
              "airport": "apt", "user": "wpt", "runway": "wpt"}


@dataclass
class PlnPlan:
    """What a ``.pln`` says, before anything is resolved."""

    origin: str = ""
    destination: str = ""
    cruise_ft: int = 0
    ifr: bool | None = None
    departure_runway: str = ""
    sid: str = ""
    sid_transition: str = ""
    star: str = ""
    star_transition: str = ""
    arrival_runway: str = ""
    approach: str = ""
    # (ident, region, kind, (lat, lon) or None, airway)
    waypoints: list[tuple[str, str, str, tuple[float, float] | None, str]] = \
        field(default_factory=list)


def parse_pln(text: str) -> PlnPlan:
    """Read a simulator flight plan, MSFS 2020's or 2024's.

    2020 writes every fix with a ``WorldPosition`` and the procedures on the
    waypoints themselves (``DepartureFP``, ``ArrivalFP``); 2024 writes the
    fixes by ident and region only and the procedures in their own
    ``DepartureDetails`` / ``ArrivalDetails`` / ``ApproachDetails`` blocks
    (measured on a SimBrief export for MSFS 2024, EBBR-LIRF, 27 Oct 2026).
    """
    text = text.lstrip("﻿")
    root = ET.fromstring(text)
    fp = root.find("FlightPlan.FlightPlan")
    if fp is None:
        fp = root
    plan = PlnPlan(
        origin=_t(fp, "DepartureID").upper(),
        destination=_t(fp, "DestinationID").upper(),
        cruise_ft=_int(_t(fp, "CruisingAlt")))
    rules = _t(fp, "FPType").upper()
    plan.ifr = None if not rules else rules.startswith("IFR")

    dep = fp.find("DepartureDetails")
    if dep is not None:
        plan.departure_runway = _runway(dep)
        plan.sid = _t(dep, "DepartureFP").upper()
        plan.sid_transition = _t(dep, "TransitionFP").upper()
    arr = fp.find("ArrivalDetails")
    if arr is not None:
        plan.arrival_runway = _runway(arr)
        plan.star = _t(arr, "ArrivalFP").upper()
        plan.star_transition = _t(arr, "TransitionFP").upper()
    app = fp.find("ApproachDetails")
    if app is not None:
        runway = _runway(app)
        plan.approach = " ".join(x for x in (
            _t(app, "ApproachTypeFP").upper(), _t(app, "SuffixFP").upper(),
            runway) if x)
        plan.arrival_runway = plan.arrival_runway or runway

    for wp in fp.findall("ATCWaypoint"):
        ident = _t(wp, "ICAO/ICAOIdent").upper() or (wp.get("id") or "").upper()
        region = _t(wp, "ICAO/ICAORegion").upper()
        kind = _PLN_KINDS.get(_t(wp, "ATCWaypointType").lower(), "wpt")
        where = _world_position(_t(wp, "WorldPosition"))
        airway = _t(wp, "ATCAirway").upper()
        # 2020's way of naming the procedures, on the fixes.
        plan.sid = plan.sid or _t(wp, "DepartureFP").upper()
        plan.star = plan.star or _t(wp, "ArrivalFP").upper()
        plan.approach = plan.approach or _t(wp, "ApproachTypeFP").upper()
        plan.departure_runway = plan.departure_runway or _runway(wp, "")
        if ident:
            plan.waypoints.append((ident, region, kind, where, airway))
    return plan


def from_pln(text: str, resolve: Resolver | None = None) -> Route:
    """The route of a ``.pln``, each fix placed by its own position where the
    file has one and by ``resolve`` where it does not."""
    plan = parse_pln(text)
    fixes, notes = [], []
    for ident, region, kind, where, airway in plan.waypoints:
        if where is None and resolve is not None:
            try:
                where = resolve(ident, region, kind)
            except Exception:
                log.debug("resolving %s failed", ident, exc_info=True)
                where = None
        if where is None:
            notes.append(f"{ident} could not be placed")
            continue
        fixes.append(RouteFix(ident=ident, lat=where[0], lon=where[1],
                              via=_via(airway), kind=kind, region=region))
    fixes = _with_ends(fixes, plan.origin, plan.destination, resolve)
    return Route(fixes=tuple(fixes), origin=plan.origin,
                 destination=plan.destination, sid=plan.sid,
                 sid_runway=plan.departure_runway, star=plan.star,
                 star_transition=plan.star_transition,
                 star_runway=plan.arrival_runway, approach=plan.approach,
                 cruise_ft=plan.cruise_ft, source="pln", notes=tuple(notes))


def _with_ends(fixes: list[RouteFix], origin: str, destination: str,
               resolve: Resolver | None) -> list[RouteFix]:
    """The airports at either end, so the first and last legs exist."""
    out = list(fixes)
    for ident, at_start in ((origin, True), (destination, False)):
        if not ident or resolve is None:
            continue
        if out and (out[0] if at_start else out[-1]).ident == ident:
            continue
        where = resolve(ident, "", "apt")
        if where is None:
            continue
        end = RouteFix(ident=ident, lat=where[0], lon=where[1], kind="apt")
        out = [end] + out if at_start else out + [end]
    return out


def _t(node, path: str) -> str:
    found = node.find(path)
    return (found.text or "").strip() if found is not None else ""


def _int(raw: str) -> int:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 0


_DESIGNATORS = {"LEFT": "L", "RIGHT": "R", "CENTER": "C", "CENTRE": "C",
                "NONE": "", "WATER": "W"}


def _runway(node, default: str = "") -> str:
    number = _t(node, "RunwayNumberFP")
    if not number:
        return default
    designator = _DESIGNATORS.get(_t(node, "RunwayDesignatorFP").upper(), "")
    return f"{int(number):02d}{designator}" if number.isdigit() \
        else number + designator


def _world_position(raw: str) -> tuple[float, float] | None:
    """``N50° 54' 4.91",E4° 29' 3.89",+000175.00`` as (lat, lon)."""
    if not raw:
        return None
    parts = raw.split(",")
    if len(parts) < 2:
        return None
    try:
        return _dms(parts[0]), _dms(parts[1])
    except ValueError:
        return None


def _dms(text: str) -> float:
    text = text.strip()
    sign = -1.0 if text[:1] in "SW" else 1.0
    numbers = [float(x) for x in re.findall(r"[\d.]+", text)]
    if not numbers:
        raise ValueError(text)
    deg = numbers[0] + (numbers[1] / 60.0 if len(numbers) > 1 else 0.0) \
        + (numbers[2] / 3600.0 if len(numbers) > 2 else 0.0)
    return sign * deg


# ----------------------------------------------------------------------
# a route string
# ----------------------------------------------------------------------

_AIRWAY = re.compile(r"^[A-Z]{1,2}\d{1,4}[A-Z]?$")
_PROCEDURE = re.compile(r"^[A-Z]{2,6}\d[A-Z]?$")
_SPEED_LEVEL = re.compile(r"^[NMK]\d{3,4}[FASM]\d{3,4}$")
_LATLON = re.compile(r"^(\d{2})(\d{2})?([NS])(\d{3})(\d{2})?([EW])$")


def from_string(text: str, origin: str, destination: str,
                resolve: Resolver | None, cruise_ft: int = 0) -> Route:
    """A typed route, ``ROUS1L ROUSY DCT GTQ UN852 XIBIL XIBI3A``.

    The procedures are recognised by shape at either end; an airway is
    flown direct between the fixes either side of it, because neither the
    database nor this function has its points, and the route says so.
    """
    tokens = [t for t in re.split(r"\s+", (text or "").upper().strip()) if t]
    tokens = [t.split("/")[0] for t in tokens]          # ROUSY/N0450F370
    tokens = [t for t in tokens if t and not _SPEED_LEVEL.match(t)]
    sid = star = ""
    if tokens and tokens[0] in (origin.upper(),):
        tokens = tokens[1:]
    if tokens and tokens[-1] in (destination.upper(),):
        tokens = tokens[:-1]
    if tokens and _PROCEDURE.match(tokens[0]) and len(tokens) > 1:
        sid, tokens = tokens[0], tokens[1:]
    if tokens and _PROCEDURE.match(tokens[-1]) and len(tokens) > 1:
        star, tokens = tokens[-1], tokens[:-1]
    fixes, notes, via = [], [], ""
    for token in tokens:
        if token in ("DCT", "DIRECT"):
            via = ""
            continue
        if _AIRWAY.match(token) and fixes:
            via = token
            notes.append(f"{token} flown direct: its points are not known")
            continue
        where = _latlon(token)
        if where is None and resolve is not None:
            where = resolve(token, "", "")
        if where is None:
            notes.append(f"{token} could not be placed")
            via = ""
            continue
        fixes.append(RouteFix(ident=token, lat=where[0], lon=where[1],
                              via=via))
        via = ""
    fixes = _with_ends(fixes, origin.upper(), destination.upper(), resolve)
    return Route(fixes=tuple(fixes), origin=origin.upper(),
                 destination=destination.upper(), sid=sid, star=star,
                 cruise_ft=cruise_ft, source="string", notes=tuple(notes))


def _latlon(token: str) -> tuple[float, float] | None:
    """``4620N00805E`` or ``46N008E``, the ICAO forms of a position."""
    m = _LATLON.match(token)
    if not m:
        return None
    lat = int(m.group(1)) + int(m.group(2) or 0) / 60.0
    lon = int(m.group(4)) + int(m.group(5) or 0) / 60.0
    return (-lat if m.group(3) == "S" else lat,
            -lon if m.group(6) == "W" else lon)


# ----------------------------------------------------------------------
# placing a fix
# ----------------------------------------------------------------------


def resolver(navdb, sim: Resolver | None = None,
             near: tuple[float, float] | None = None) -> Resolver:
    """The simulator's facility data first, then the navaids and airports
    in ``nav.sqlite``.

    The simulator knows every intersection in its AIRAC and the database
    knows none, so offline a ``.pln`` keeps its navaids and its airports and
    loses its intersections -- each listed in the route's notes. A navaid
    ident is not unique worldwide; the one nearest ``near`` (the departure)
    is taken, and the simulator, given the region, is never ambiguous.
    """
    def resolve(ident: str, region: str, kind: str):
        if sim is not None:
            try:
                found = sim(ident, region, kind)
            except Exception:
                log.debug("the simulator could not place %s", ident,
                          exc_info=True)
                found = None
            if found is not None:
                return found
        if kind in ("", "apt") and navdb is not None:
            airport = navdb.airport(ident)
            if airport is not None:
                return airport.lat, airport.lon
        if kind in ("", "vor", "ndb") and navdb is not None:
            row = navdb.navaid(ident, near=near)
            if row is not None:
                return float(row["lat"]), float(row["lon"])
        return None
    return resolve


__all__ = ["Route", "RouteFix", "Progress", "PlnPlan", "from_ofp",
           "from_pln", "from_string", "parse_pln", "resolver",
           "climb_groundspeed"]
