"""Radar vectors onto a final approach (the IFR plan, Phase 5).

Approach cleared an arrival only once it happened to be in the final's
cone inside eighteen miles; an aeroplane whose STAR ended anywhere else --
O'Hare's WYNDE3 on the far side of 27C, Heathrow's ALESO 1H at Bovingdon --
was never turned towards the runway at all, and was cleared, if ever, by
luck. This is the pattern an approach controller flies it round instead,
after LocalTC's ``vectors.py`` (read for the design only):

- everything in the runway's own frame -- ``out`` along the extended
  centreline from the threshold, positive on the approach side, ``side``
  across it, positive right of the landing direction;
- an arrival out beyond the base turn and inside a 35-degree cone goes
  straight in, pointed at a spot it can join the final from at 30 degrees;
- anywhere else it joins a downwind ``offset_nm`` off the centreline on its
  own side, flies it outbound, turns base abeam ``base_nm`` out and is
  given a 30-degree intercept to a gate ``gate_nm`` out;
- the pattern only moves forward and keeps its side, so drifting over a
  line never turns a flight back;
- too high for the miles still to fly, it goes out on a downwind first;
- a visual approach's last vector is the final itself, given a turn's
  radius and the time to say it ahead of the centreline.

The figures are LocalTC's, which the IFR plan names: a jet's pattern is a
9 nm gate, a 17 nm base and a 6 nm downwind, under 160 kt 5, 10 and 3.5.
Everything here is a pure function of where the aeroplane is.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..navdata import terrain
from ..navdata.build import haversine_nm, project
from .procedures import frame


@dataclass(frozen=True)
class Pattern:
    gate_nm: float               # the final is joined no closer than this
    base_nm: float               # the base leg is this far out
    offset_nm: float             # the downwind is this far off the centreline
    cone_deg: float = 35.0       # straight in, inside this of the final
    lead_nm: float = 2.0         # the base turn is given this early


JET = Pattern(gate_nm=9.0, base_nm=17.0, offset_nm=6.0)
SLOW = Pattern(gate_nm=5.0, base_nm=10.0, offset_nm=3.5, lead_nm=1.0)
SLOW_KT = 160.0

# Close to the centreline and far enough out: the 30-degree cut; this close,
# already on it.
INTERCEPT_SIDE_NM = 3.5
ON_CENTRELINE_NM = 0.5
INTERCEPT_DEG = 30.0
# Further than this off the final's direction, the aeroplane is going away.
AWAY_DEG = 135.0
# Stepping down: this much a mile still to fly, above the field.
FT_PER_NM = 250.0
# Too high: more than this above what the miles can lose -- a steep 400 ft
# a mile from far out, the 3-degree path close in.
TOO_HIGH_MARGIN_FT = 1500.0
STEEP_FT_PER_NM = 400.0
STEEP_FROM_NM = 20.0
GLIDE_FT_PER_NM = 318.0
# A visual approach's final vector: a turn's radius ahead, plus the time to
# say it and the pilot to turn the knob, plus a little.
VISUAL_LEAD_S = 10.0
VISUAL_LEAD_NM = 0.2
AUTOPILOT_BANK_DEG = 25.0
# "Fly present heading" when the new heading is this close to the present.
PRESENT_HEADING_DEG = 10.0
# The minimum vectoring altitude this program can know: the terrain grid's
# floor round points along the new heading. UNVERIFIED: an approximation of
# an MVA chart, which this program does not have -- the highest terrain
# within 3 nm of a point 3, 6 and 9 nm ahead, plus the MSA's margin.
MVA_AHEAD_NM = (3.0, 6.0, 9.0)
MVA_RADIUS_NM = 3.0

ORDER = {"join": 0, "downwind": 1, "straight_in": 2, "base": 2,
         "intercept": 3, "final": 4}


@dataclass(frozen=True)
class Vector:
    heading_true: float
    leg: str                     # straight_in, join, downwind, base, intercept, final
    track_nm: float              # flying distance left to the threshold this way
    side: float = 1.0            # +1 the pattern is right of the landing direction
    join_nm: float = 0.0         # where it meets the final, when it does


def pattern_for(speed_kt: float) -> Pattern:
    return SLOW if 0 < speed_kt < SLOW_KT else JET


def turn_radius_nm(speed_kt: float) -> float:
    """A turn's radius: rate one, or an autopilot's 25 degrees of bank when
    that is wider (an airliner at 210 kt)."""
    rate_one = speed_kt / 188.5
    banked = speed_kt ** 2 / (11.26 * math.tan(math.radians(AUTOPILOT_BANK_DEG))) \
        / 6076.1
    return max(rate_one, banked)


def _heading_to(final, out, side, to_out, to_side) -> float:
    """True heading from one runway-frame point to another."""
    course = math.radians(final.course)
    d_out, d_side = to_out - out, to_side - side
    # out grows away from the threshold against the landing course; side
    # grows to the right of it.
    east = -d_out * math.sin(course) + d_side * math.cos(course)
    north = -d_out * math.cos(course) - d_side * math.sin(course)
    return math.degrees(math.atan2(east, north)) % 360.0


def _off(a: float, b: float) -> float:
    return abs((a - b + 540.0) % 360.0 - 180.0)


def vector(final, lat: float, lon: float, *, speed_kt: float = 0.0,
           heading_true: float | None = None, after: str = "",
           side: float | None = None, visual: bool = False) -> Vector:
    """The next leg onto ``final``. ``after`` is the leg last given and
    ``side`` the side its pattern is on: the pattern goes forward only."""
    p = pattern_for(speed_kt)
    out, lateral = frame(final, lat, lon)
    inbound = final.course % 360.0
    outbound = (inbound + 180.0) % 360.0
    patterned = after in ("join", "downwind", "base")
    s = side if (patterned and side) else (1.0 if lateral >= 0 else -1.0)
    off = lateral * s if patterned else abs(lateral)
    away = heading_true is not None and _off(heading_true, inbound) > AWAY_DEG

    def cut_in() -> Vector:
        if abs(lateral) < ON_CENTRELINE_NM:
            # Already on it: the final's own heading, not a cut across it.
            return Vector(inbound, "intercept", out, 1.0, out)
        from_side = 1.0 if lateral >= 0 else -1.0
        join = out - abs(lateral) / math.tan(math.radians(INTERCEPT_DEG))
        return Vector((inbound - INTERCEPT_DEG * from_side) % 360.0, "intercept",
                      2.0 * abs(lateral) + max(join, 0.0), from_side,
                      max(join, 0.0))

    # Through the centreline on its base: back to it from where it is.
    if after == "base" and side is not None and lateral * side < 0:
        return cut_in()
    close_in = abs(lateral) <= INTERCEPT_SIDE_NM and out >= p.gate_nm - 2.0
    if visual and (after in ("intercept", "final") or (close_in and not away)):
        from_side = 1.0 if lateral >= 0 else -1.0
        current = heading_true if heading_true is not None \
            else (inbound - 90.0 * from_side) % 360.0
        to_turn = _off(current, inbound)
        speed = max(speed_kt, 120.0)
        closing = speed * math.sin(math.radians(min(to_turn, 90.0))) \
            * VISUAL_LEAD_S / 3600.0
        lead = turn_radius_nm(speed) * (1.0 - math.cos(math.radians(to_turn))) \
            + closing + VISUAL_LEAD_NM
        if after == "final" or abs(lateral) <= lead:
            return Vector(inbound, "final", out + abs(lateral), from_side, out)
        return Vector((inbound - INTERCEPT_DEG * from_side) % 360.0, "base",
                      2.0 * abs(lateral) + out, from_side)
    if after == "intercept" or (close_in and not away):
        return cut_in()
    if close_in:
        from_side = 1.0 if lateral >= 0 else -1.0
        return Vector((inbound - 90.0 * from_side) % 360.0, "base",
                      abs(lateral) + out, from_side)
    to_centreline = (inbound - 90.0 * s) % 360.0
    if after == "base" or (patterned and out >= p.base_nm - p.lead_nm):
        return Vector(to_centreline, "base", abs(off) + max(out, p.gate_nm), s)
    if not patterned and out >= p.base_nm \
            and off <= out * math.tan(math.radians(p.cone_deg)):
        join = max(p.gate_nm,
                   out - off / math.tan(math.radians(INTERCEPT_DEG)))
        return Vector(_heading_to(final, out, lateral, join, 0.0),
                      "straight_in", math.hypot(out - join, off) + join, s, join)
    if out >= p.base_nm:
        return Vector(to_centreline, "base", off + out, s)
    if after == "downwind" or abs(off - p.offset_nm) <= 2.0:
        return Vector(outbound, "downwind",
                      (p.base_nm - out) + abs(off) + p.base_nm, s)
    ahead = min(p.base_nm, out + max(2.0, abs(off - p.offset_nm)))
    return Vector(_heading_to(final, out, lateral, ahead, s * p.offset_nm),
                  "join", math.hypot(ahead - out, off - p.offset_nm)
                  + (p.base_nm - ahead) + p.offset_nm + p.base_nm, s)


def too_high(leg: Vector, altitude_ft: float, elev_ft: float,
             slow: bool = False) -> bool:
    """More than 1,500 ft above what the miles still to fly can lose."""
    rate = STEEP_FT_PER_NM if leg.track_nm > STEEP_FROM_NM and not slow \
        else GLIDE_FT_PER_NM
    return altitude_ft - elev_ft > leg.track_nm * rate + TOO_HIGH_MARGIN_FT


def extended(final, lat: float, lon: float) -> Vector:
    """Too high to turn in: outbound on a downwind to make the room."""
    out, lateral = frame(final, lat, lon)
    s = 1.0 if lateral >= 0 else -1.0
    return Vector((final.course + 180.0) % 360.0, "downwind",
                  out + abs(lateral), s)


def step_altitude(track_nm: float, elev_ft: float, floor_ft: float) -> int:
    """Down to 250 ft a mile still to fly above the field, in whole
    thousands, never below ``floor_ft``."""
    wanted = int((elev_ft + track_nm * FT_PER_NM) // 1000 * 1000)
    return int(max(floor_ft, wanted))


def mva_ft(lat: float, lon: float, heading_true: float, elev_ft: float) -> float:
    """The terrain floor along a new heading (see MVA_AHEAD_NM)."""
    worst = 0.0
    for miles in MVA_AHEAD_NM:
        ahead = project(lat, lon, heading_true, miles)
        worst = max(worst, terrain.floor_ft(ahead[0], ahead[1], elev_ft,
                                            MVA_RADIUS_NM))
    return worst


def present_heading(heading_true: float | None, wanted: float) -> bool:
    """Already pointing about right: "fly present heading"."""
    return heading_true is not None and _off(heading_true, wanted) \
        <= PRESENT_HEADING_DEG


def joining_final(final, lat: float, lon: float, heading_true: float | None,
                  within_nm: float = 20.0, side_nm: float = 3.0) -> bool:
    """Joining the final by itself: near the centreline, inside 20 miles,
    pointing inbound (after LocalTC: 3 nm, 20 nm, within 100 degrees)."""
    out, lateral = frame(final, lat, lon)
    if not (1.0 <= out <= within_nm and abs(lateral) <= side_nm):
        return False
    return heading_true is None or _off(heading_true, final.course) <= 100.0


__all__ = ["Pattern", "Vector", "JET", "SLOW", "vector", "too_high",
           "extended", "step_altitude", "mva_ft", "present_heading",
           "joining_final", "turn_radius_nm", "ORDER"]
