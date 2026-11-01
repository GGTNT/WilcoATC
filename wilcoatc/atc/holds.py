"""Holding an arrival (the IFR plan, Phase 6).

Nothing ever held: an arrival into a field below every approach's minima,
or behind a queue the runway could not take, was descended, vectored and
cleared as if neither were so. A controller holds it instead, over the
arrival's last fix, for a reason, with a time to expect further clearance,
and lets it go when the reason has gone. After LocalTC's ``holds.py`` (read
for the design only) and the IFR plan:

- the reason: more arrivals ahead than the runway takes, the destination
  below the minima of every approach the aeroplane can fly, or the pilot's
  request (the runways closed is not here: nothing says a runway is);
- the fix: the STAR's last fix, else the route's last fix, 15-60 nm out;
- the pattern: inbound towards the field, right turns, one-minute legs at
  or below 14,000 ft, a minute and a half above (ICAO Doc 8168, FAA AIM
  5-3-8);
- an EFC time, the sequence said again when it changes, and "cleared to
  leave the hold, proceed direct ..." when the reason is gone;
- watched as flown: a flight that wanders off the racetrack is asked.

Everything here is a pure function; arrival.py holds the state on the
session and says the words.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from ..navdata.build import bearing_deg, haversine_nm

# The hold fix, this far from the field (the IFR plan).
HOLD_FIX_NM = (15.0, 60.0)
# Legs: a minute at or below this, a minute and a half above (Doc 8168
# Vol I, holding procedures; FAA AIM 5-3-8 j).
ONE_MINUTE_BELOW_FT = 14000.0
# Arrivals ahead the runway takes before a flight is held: a runway lands
# one every two minutes or so, and this many is a quarter of an hour's
# queue. UNVERIFIED: a choice.
SEQUENCE_HOLD_AHEAD = 6
# In Europe a queue is absorbed by speed and vectors first, the hold last
# (the pilot, flying into Rome: six ahead held them at once). Ten is twenty
# minutes of queue. UNVERIFIED: a choice, as the six is.
SEQUENCE_HOLD_AHEAD_ICAO = 10
ARRIVAL_SPACING_MIN = 2.0
# A place in the sequence said again at most this often, and a new EFC only
# when it has moved more than EFC_MOVED_MIN: said at every change it was
# "number twelve ... number thirteen" every twenty seconds. UNVERIFIED: the
# pilot's two to three minutes.
SEQUENCE_REPEAT_S = 150.0
EFC_MOVED_MIN = 2
# Below minima the delay is the weather's, not the queue's: an EFC this far
# ahead, renewed when it comes. UNVERIFIED: a choice.
WEATHER_EFC_MIN = 20
# The hold is issued this far before the fix.
ISSUE_BEFORE_NM = 25.0
# Entered: within this of the fix. Off the racetrack: further than the
# pattern can take a flight from its fix.
ENTERED_NM = 2.0
OFF_PATTERN_MARGIN_NM = 4.0

_CARDINAL = ("north", "north-east", "east", "south-east", "south",
             "south-west", "west", "north-west")


@dataclass
class Hold:
    """One hold, kept on the session while it lasts."""

    fix: str
    lat: float
    lon: float
    inbound_true: float
    side: str                     # "north-west": which side of the fix
    leg_minutes: float
    altitude_ft: float
    reason: str                   # "weather", "sequence", "requested"
    efc_hhmm: str
    number: int = 0               # the place in the sequence, 0 unknown
    entered: bool = False
    asked_off: bool = False
    issued_at: float = field(default_factory=lambda: time.time())
    # When the place and the time were last said.
    said_at: float = field(default_factory=lambda: time.time())


def hold_fix(star_fixes, route_fixes, field_lat: float, field_lon: float):
    """The fix to hold over: the STAR's last fix when it is 15-60 nm from
    the field, else the route's last fix in that ring; None without one."""
    def in_ring(fix) -> bool:
        out = haversine_nm(fix.lat, fix.lon, field_lat, field_lon)
        return HOLD_FIX_NM[0] <= out <= HOLD_FIX_NM[1]

    star = [f for f in star_fixes or () if not f.is_marker]
    if star and in_ring(star[-1]):
        return star[-1]
    candidates = [f for f in route_fixes or ()
                  if not f.is_marker and f.kind != "apt" and in_ring(f)]
    if not candidates:
        # A STAR ending inside 15 nm: its last fix that is in the ring.
        candidates = [f for f in star if in_ring(f)]
    return candidates[-1] if candidates else None


def inbound_course(fix_lat, fix_lon, field_lat, field_lon) -> float:
    """The inbound leg points at the field (true)."""
    return bearing_deg(fix_lat, fix_lon, field_lat, field_lon)


def side_of_fix(inbound_true: float) -> str:
    """Which side of the fix the racetrack lies: away from the field."""
    away = (inbound_true + 180.0) % 360.0
    return _CARDINAL[int((away + 22.5) // 45.0) % 8]


def leg_minutes(altitude_ft: float) -> float:
    return 1.0 if altitude_ft <= ONE_MINUTE_BELOW_FT else 1.5


def efc(now_epoch: float, minutes: float) -> str:
    """The time to expect further clearance, UTC, as HHMM."""
    stamp = time.gmtime(now_epoch + minutes * 60.0)
    return f"{stamp.tm_hour:02d}{stamp.tm_min:02d}"


def sequence_delay_min(ahead: int) -> float:
    return max(ARRIVAL_SPACING_MIN, ahead * ARRIVAL_SPACING_MIN)


def sequence_hold_ahead(ident: str) -> int:
    """How many ahead before the queue is held: the FAA's six, more where
    speed and vectors take the queue first."""
    from ..navdata.db import dialect_for

    return (SEQUENCE_HOLD_AHEAD if dialect_for(ident) == "faa"
            else SEQUENCE_HOLD_AHEAD_ICAO)


def minutes_apart(a_hhmm: str, b_hhmm: str) -> int:
    """How far apart two times of day are, in minutes, across midnight."""
    a = int(a_hhmm[:2]) * 60 + int(a_hhmm[2:])
    b = int(b_hhmm[:2]) * 60 + int(b_hhmm[2:])
    apart = abs(a - b) % 1440
    return min(apart, 1440 - apart)


def below_every_minima(known, runway: str, weather, airliner: bool = True) -> bool:
    """The destination's weather under the minima of every approach to
    ``runway`` the aeroplane can fly -- and of a circle from any other.
    Nothing known about the field is the ILS's 200 ft and half a mile."""
    from .approaches import AIRLINER_LINES, CIRCLING, GA_LINES, MINIMA, RNP, \
        _allows

    if weather is None:
        return False
    flown = AIRLINER_LINES if airliner else GA_LINES
    if known is None or not known.procedures:
        height, vis = MINIMA["ILS"]
        return not _allows(weather, height, vis)
    circle = CIRCLING["C" if airliner else "A"]
    for approach in known.procedures:
        if approach.kind == RNP and not airliner:
            continue
        height, vis, line = approach.minima(flown)
        if not line:
            continue
        if approach.runway != runway:
            height, vis = max(height, circle[0]), max(vis, circle[1])
        if _allows(weather, height, vis):
            return False
    return True


def off_the_pattern(hold: Hold, lat: float, lon: float, speed_kt: float) -> bool:
    """Further from the fix than a racetrack at this speed reaches."""
    leg_nm = max(speed_kt, 150.0) / 60.0 * hold.leg_minutes
    turn_nm = max(speed_kt, 150.0) / (math.pi * 60.0)    # a rate-one turn
    reach = leg_nm + 2.0 * turn_nm + OFF_PATTERN_MARGIN_NM
    return haversine_nm(lat, lon, hold.lat, hold.lon) > reach


__all__ = ["Hold", "hold_fix", "inbound_course", "side_of_fix",
           "leg_minutes", "efc", "below_every_minima", "off_the_pattern",
           "sequence_delay_min", "sequence_hold_ahead", "minutes_apart",
           "SEQUENCE_HOLD_AHEAD", "SEQUENCE_HOLD_AHEAD_ICAO",
           "SEQUENCE_REPEAT_S", "EFC_MOVED_MIN", "WEATHER_EFC_MIN",
           "ISSUE_BEFORE_NM", "ENTERED_NM"]
