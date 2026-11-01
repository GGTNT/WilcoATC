# -*- coding: utf-8 -*-
"""Where an aeroplane is in the circuit, worked out from where it is.

The tower used to take the pilot's word for it. The leg came from the words
of the last transmission and from nothing else, the join was "downwind"
unless the aeroplane was more than twice PATTERN_JOIN_NM away (then it was
straight-in, from whichever side), and a pilot who said "downwind" while
lined up on a two-mile final was told to report base. No controller with a
window works like that.

This module answers the question from the aeroplane's position, track,
height and vertical speed, in the runway's own frame: along the centreline
from the landing threshold, and across it towards the circuit side. The
same frame is what the AI traffic in :mod:`wilcoatc.atc.traffic` flies its
circuits in, and the shape of that circuit -- how far upwind before the
crosswind turn, how wide the downwind, how far past the threshold before
base -- was defined there. It is defined here now and the traffic imports
it, so the tower and the aeroplanes it watches agree on where a downwind is.

The circuit side and height default to left-hand at 1,000 ft above the
field, the light-aircraft figure in the FAA AIM 4-3-3 and the usual one in
the UK and French AIPs. Per-runway data replaces both later (Phase 4 of the
VFR plan); nothing here assumes the default.

Geometry only. What the tower says about it lives in the controller and in
:mod:`wilcoatc.atc.arrival`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..navdata.build import project
from ..navdata.db import haversine_nm, normalize_runway
from .flying import offset_from_track

# The shape of the circuit, in miles from the threshold: how far out before
# the crosswind turn, how far to the side the downwind is flown, how far past
# the threshold before turning base, and how close to the centreline counts as
# established on final. Measured off the AI traffic's light aircraft, which
# is where these were first written.
CIRCUIT_UPWIND_NM = 1.6
CIRCUIT_WIDTH_NM = 1.1
CIRCUIT_BASE_NM = 1.4
CIRCUIT_CENTRELINE_NM = 0.2

DEFAULT_SIDE = "left"
DEFAULT_HEIGHT_AGL_FT = 1000.0
# When the data has no runway length. A light-aircraft runway, give or take.
DEFAULT_LENGTH_NM = 0.8

# The legs a tower sequences. Overhead, deadside and outside are where an
# aeroplane is, not a leg of the circuit it is flying.
LEGS = ("upwind", "crosswind", "downwind", "base", "final")

# "Final" is reported within 4 NM of touchdown and "long final" between 4
# and 8 NM (ICAO Doc 4444, chapter 12, the aerodrome control phraseology;
# the paragraph number was not checked). Inside a mile is what every manual
# means by short final.
LONG_FINAL_NM = 4.0
FINAL_LIMIT_NM = 8.0
SHORT_FINAL_NM = 1.0

# How far outside the circuit's own box an aeroplane can be and still be
# called on a leg of it. Wider than the drawn circuit, because nobody flies
# it to the yard.
_MARGIN_NM = 1.5
# A track within this of a leg's direction is flying that leg.
_TRACK_TOLERANCE_DEG = 45.0
# Within this of the runway's middle, above the circuit, is overhead.
_OVERHEAD_NM = 0.7
_OVERHEAD_ABOVE_FT = 300.0
# Above the circuit by more than this, an aeroplane is not in it whatever
# its position says.
_ABOVE_THE_CIRCUIT_FT = 700.0
# The final cone, as wide as final.py's, plus a little slack close in so a
# wide turn on to final is still final.
_FINAL_CONE_DEG = 30.0
_FINAL_SLACK_NM = 0.3


def runway_frame(lat: float, lon: float, threshold_lat: float,
                 threshold_lon: float, course: float) -> tuple[float, float]:
    """``(along, across)`` in miles from a threshold.

    Along is positive in the direction of landing (and of takeoff off the
    same end); across is positive to the right of it.
    """
    return offset_from_track(lat, lon, threshold_lat, threshold_lon, course)


def side_sign(side: str) -> float:
    """+1 where the circuit side is to the right of the runway, -1 left."""
    return 1.0 if (side or "").lower() == "right" else -1.0


def join_point(threshold_lat: float, threshold_lon: float, course: float,
               side: str) -> tuple[float, float]:
    """Where the downwind starts: abeam the upwind end, out on the circuit
    side. An aeroplane told to join the downwind flies to this point."""
    lat, lon = project(threshold_lat, threshold_lon, course, CIRCUIT_UPWIND_NM)
    return project(lat, lon, (course + 90.0 * side_sign(side)) % 360.0,
                   CIRCUIT_WIDTH_NM)


def _angle(a: float, b: float) -> float:
    """The smaller angle between two directions, in degrees."""
    return abs(((a - b) + 540.0) % 360.0 - 180.0)


@dataclass(frozen=True)
class Position:
    """Where one aeroplane is, as the tower sees it."""

    leg: str              # a leg in LEGS, or overhead, deadside, outside
    stage: str            # early/midfield/late on the downwind; long/short final
    along_nm: float       # from the threshold, positive towards the far end
    out_nm: float         # from the centreline, positive on the circuit side
    height_agl_ft: float
    inside: bool          # inside the circuit's volume
    climbing_away: bool   # climbing low on final or upwind: round again
    to_go_nm: float       # flown distance to the threshold round the circuit

    @property
    def in_circuit(self) -> bool:
        return self.leg in LEGS

    def describe(self) -> str:
        """For the log: "midfield downwind", "short final", "overhead"."""
        return f"{self.stage} {self.leg}".strip()


# Legs a pilot can report while the tower's picture says the neighbouring
# one: a pilot turning base is still late downwind on the geometry, and one
# turning final is still on base. Those are not corrected.
_NEIGHBOURS = {
    "upwind": {"upwind", "crosswind"},
    "crosswind": {"upwind", "crosswind", "downwind"},
    "downwind": {"crosswind", "downwind", "base"},
    "base": {"downwind", "base", "final"},
    "final": {"base", "final"},
}


def agrees(reported: str, seen: str) -> bool:
    """Whether a reported leg is close enough to the computed one to accept.

    Only a leg of the circuit can be contradicted. Outside it, or overhead,
    the tower has nothing better than the pilot's word.
    """
    reported = (reported or "").lower()
    if seen not in LEGS or reported not in _NEIGHBOURS:
        return True
    return seen in _NEIGHBOURS[reported]


@dataclass(frozen=True)
class Circuit:
    """One runway's circuit: where its threshold is, which way it lands,
    which side the circuit is flown on and at what height."""

    runway: str
    lat: float
    lon: float
    course: float                 # true, the landing direction
    side: str = DEFAULT_SIDE
    height_agl_ft: float = DEFAULT_HEIGHT_AGL_FT
    elevation_ft: float = 0.0
    length_nm: float = DEFAULT_LENGTH_NM

    @property
    def sign(self) -> float:
        return side_sign(self.side)

    def frame(self, lat: float, lon: float) -> tuple[float, float]:
        """``(along, out)``: out is positive on the circuit side."""
        along, across = runway_frame(lat, lon, self.lat, self.lon, self.course)
        return along, across * self.sign

    def midpoint(self) -> tuple[float, float]:
        return project(self.lat, self.lon, self.course, self.length_nm / 2.0)

    def locate(self, state) -> Position:
        """Where an :class:`~wilcoatc.sim.state.AircraftState` is."""
        from .final import course_over_ground

        track = course_over_ground(state)
        agl = getattr(state, "altitude_agl_ft", 0.0) or None
        return self.locate_at(
            state.latitude, state.longitude, float(state.altitude_ft or 0.0),
            track, float(getattr(state, "vertical_speed_fpm", 0.0) or 0.0),
            agl_ft=agl)

    def locate_at(self, lat: float, lon: float, altitude_ft: float,
                  track: float | None, vs_fpm: float = 0.0,
                  agl_ft: float | None = None) -> Position:
        """Where an aeroplane is, from the raw numbers -- the user's own
        state, or one of the AI traffic's flights."""
        along, out = self.frame(lat, lon)
        height = (float(agl_ft) if agl_ft is not None
                  else float(altitude_ft) - self.elevation_ft)
        leg, stage = self._leg(lat, lon, along, out, height, track)
        inside = self._inside(leg, along, height)
        climbing = (vs_fpm > 300.0 and leg in ("final", "upwind")
                    and height < self.height_agl_ft + 200.0)
        return Position(leg, stage, along, out, height, inside, climbing,
                        self._to_go(leg, along, out))

    # ------------------------------------------------------------------

    def _leg(self, lat, lon, along, out, height, track) -> tuple[str, str]:
        length = self.length_nm
        mid_lat, mid_lon = self.midpoint()
        if (haversine_nm(lat, lon, mid_lat, mid_lon) <= _OVERHEAD_NM
                and height >= self.height_agl_ft + _OVERHEAD_ABOVE_FT):
            return "overhead", ""
        if height > self.height_agl_ft + _ABOVE_THE_CIRCUIT_FT and not (
                along < 0 and -along > LONG_FINAL_NM):
            # Above the circuit and not out on a long final, where an
            # aeroplane can be high for its distance and still be on it.
            return "outside", ""

        aligned = track is not None and _angle(track, self.course) <= _TRACK_TOLERANCE_DEG
        reversed_ = track is not None and _angle(track, self.course + 180.0) <= _TRACK_TOLERANCE_DEG
        towards_centre = (self.course - 90.0 * self.sign) % 360.0
        inbound = track is not None and _angle(track, towards_centre) <= _TRACK_TOLERANCE_DEG
        outbound = track is not None and _angle(track, towards_centre + 180.0) <= _TRACK_TOLERANCE_DEG

        # Final: before the threshold, in the cone, pointing down the runway.
        if along < 0.1 and -along <= FINAL_LIMIT_NM and (aligned or track is None):
            limit = max(_FINAL_SLACK_NM,
                        -along * math.tan(math.radians(_FINAL_CONE_DEG)))
            if abs(out) <= limit:
                miles = max(0.0, -along)
                stage = ("short" if miles <= SHORT_FINAL_NM
                         else "long" if miles > LONG_FINAL_NM else "")
                return "final", stage

        far_end = length + CIRCUIT_UPWIND_NM + _MARGIN_NM
        near_end = -(CIRCUIT_BASE_NM + _MARGIN_NM + 1.0)
        in_box = near_end <= along <= far_end

        # Upwind: over the runway or beyond it, on the centreline, going away.
        if 0.0 <= along <= far_end and abs(out) <= 0.5 and (aligned or track is None):
            return "upwind", ""

        wide = CIRCUIT_WIDTH_NM + _MARGIN_NM
        if 0.3 <= out <= wide and in_box:
            if reversed_:
                return "downwind", self._downwind_stage(along)
            if inbound and along < length * 0.5:
                return "base", ""
            if outbound and along > length * 0.5:
                return "crosswind", ""
            # A track that says nothing (a turn, or no track at all): the
            # position decides.
            if along < -0.3:
                return "base", ""
            if along > length + 0.3:
                return "crosswind", ""
            return "downwind", self._downwind_stage(along)
        if -wide <= out <= -0.3 and -_MARGIN_NM <= along <= length + _MARGIN_NM:
            return "deadside", ""
        return "outside", ""

    def _downwind_stage(self, along: float) -> str:
        """Early abeam the upwind end, late abeam the threshold or past it."""
        if along > self.length_nm * 0.7:
            return "early"
        if along < self.length_nm * 0.25:
            return "late"
        return "midfield"

    def _inside(self, leg: str, along: float, height: float) -> bool:
        if height > self.height_agl_ft + _ABOVE_THE_CIRCUIT_FT:
            return False
        if leg == "final":
            return -along <= CIRCUIT_BASE_NM + _MARGIN_NM
        return leg in ("upwind", "crosswind", "downwind", "base")

    def _to_go(self, leg: str, along: float, out: float) -> float:
        """How far round the circuit the aeroplane still has to fly to the
        threshold, which is what orders a sequence: the one with less to go
        is ahead, whichever leg each of them is on."""
        width, base, length = CIRCUIT_WIDTH_NM, CIRCUIT_BASE_NM, self.length_nm
        turn_back = length + CIRCUIT_UPWIND_NM
        if leg == "final":
            return max(0.0, -along)
        if leg == "base":
            return max(0.0, out) + max(base, -along)
        if leg == "downwind":
            return max(along + base, 0.0) + max(out, 0.0) + max(base, -along)
        if leg == "crosswind":
            return (max(width - out, 0.0) + along + base + width + base)
        if leg == "upwind":
            return (max(turn_back - along, 0.0) + width
                    + turn_back + base + width + base)
        # Not in the circuit: behind everything that is.
        return 99.0


def runway_length_nm(navdb, ident: str, runway: str) -> float:
    """Threshold to threshold, where the data has both ends."""
    wanted = normalize_runway(runway)
    for rw in navdb.runways(ident):
        ends = list(rw.ends())
        names = [normalize_runway(e[0]) for e in ends]
        if wanted not in names or len(ends) < 2:
            continue
        (_n1, _h1, lat1, lon1), (_n2, _h2, lat2, lon2) = ends[0], ends[1]
        if None in (lat1, lon1, lat2, lon2):
            return DEFAULT_LENGTH_NM
        return haversine_nm(lat1, lon1, lat2, lon2) or DEFAULT_LENGTH_NM
    return DEFAULT_LENGTH_NM


def circuit_for(navdb, ident: str, runway: str, side: str = DEFAULT_SIDE,
                height_agl_ft: float = DEFAULT_HEIGHT_AGL_FT) -> Circuit | None:
    """The circuit to ``runway`` at ``ident``, or None without a threshold."""
    from .final import final_for

    final = final_for(navdb, ident, runway)
    if final is None:
        return None
    airport = navdb.airport(ident)
    elevation = float(getattr(airport, "elev_ft", 0.0) or 0.0)
    return Circuit(final.runway, final.lat, final.lon, final.course,
                   side or DEFAULT_SIDE, height_agl_ft, elevation,
                   runway_length_nm(navdb, ident, runway))


# ----------------------------------------------------------------------
# the join
# ----------------------------------------------------------------------

# Where an overhead join is the standard way in: the UK (CAP 413 and the
# CAA's "Standard Overhead Join" safety sense leaflet) and France ("verticale
# terrain", in the DGAC VFR procedures). Elsewhere an aeroplane on the dead
# side is joined to the downwind and crosses to it -- the FAA's 45-degree
# entry (AIM 4-3-3) is how the pilot flies that, not what the tower says.
OVERHEAD_REGIONS = frozenset({"uk", "fr"})


def choose_join(here: Position, circuit: Circuit, region: str = "icao") -> str:
    """Which way in, from where the aeroplane is now.

    ``straight_in``, ``base``, ``downwind``, ``crosswind`` or ``overhead``.
    Replaces "downwind, unless further than twelve miles": a Cessna five
    miles out on the extended centreline was sent round the whole circuit,
    and one twelve miles out on the dead side was told to fly straight in.
    """
    if here.leg == "final":
        return "straight_in"
    if here.leg in ("downwind", "base", "crosswind"):
        return here.leg
    along, out = here.along_nm, here.out_nm
    on_circuit_side = out >= 0.3
    if along < -0.5:
        # On the approach side of the field. Within the cone off the
        # extended centreline, straight in; out to the circuit side, a base.
        offset = math.degrees(math.atan2(abs(out), -along))
        if offset <= _FINAL_CONE_DEG:
            return "straight_in"
        if on_circuit_side:
            return "base"
    elif on_circuit_side:
        if along > circuit.length_nm + 0.5:
            return "crosswind"
        return "downwind"
    # The dead side, or overhead, or beyond the upwind end on the centreline.
    if region in OVERHEAD_REGIONS:
        return "overhead"
    return "downwind"


__all__ = [
    "CIRCUIT_UPWIND_NM", "CIRCUIT_WIDTH_NM", "CIRCUIT_BASE_NM",
    "CIRCUIT_CENTRELINE_NM", "DEFAULT_SIDE", "DEFAULT_HEIGHT_AGL_FT", "LEGS",
    "Circuit", "Position", "agrees", "choose_join", "circuit_for",
    "join_point", "runway_frame", "runway_length_nm", "side_sign",
]
