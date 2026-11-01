"""How high a departure is cleared, and how the climb goes on from there.

Every clearance said "climb to 5000" whatever the field -- Brussels at 175
ft, Denver at 5,400 -- and the departure controller then climbed the flight
straight to its cruise, so the centres above had nothing left to do but say
"maintain FL370" to an aeroplane passing FL120 on its way up. A real climb
is given in pieces: the clearance's initial altitude, departure's steps up
to the top of its own airspace, then each centre's habitual level, then the
cruise. The pieces here are after LocalTC (``_initial_altitude``,
``_climb_step``, ``_climb_due``, ``_leaving_departure``; read for the design
only), with the thresholds the IFR plan gives:

- the initial altitude is the field plus 3,000 ft rounded up, one of a few
  steps above that chosen by a stable hash of the field, the SID, the runway
  and the callsign -- the same every time for the same departure, and not
  5,000 everywhere -- and never below the terrain's minimum sector altitude;
- departure climbs a flight to the top of its airspace, 17,000 ft or the
  cruise if that is lower, a few thousand feet at a time, the next step
  given as the flight comes within 1,500 ft of the last while still
  climbing, so the climb never stops;
- each centre has its own usual intermediate level, FL230 to FL280 by a hash
  of its name, then the cruise;
- departure hands the flight to the centre when it leaves departure's area
  (17,000 ft above the field or 30 nm out, until Phase 3 brings the real
  outlines), or as it nears departure's top with more climb to come.

Everything is a pure function of what it is handed, so the controller, the
engine and the tests all ask the same question the same way.
"""

from __future__ import annotations

import math
import zlib

# The initial altitude: at least this, at most this (unless the field itself
# is that high), and one of these steps above field + 3,000 (after LocalTC:
# 4,000, 10,000 and 0/1,000/2,000/4,000/5,000).
INITIAL_MIN_FT = 4000
INITIAL_MAX_FT = 10000
INITIAL_STEPS_FT = (0, 1000, 2000, 4000, 5000)

# Departure works a flight this high and no higher; the centres do the rest.
DEPARTURE_TOP_FT = 17000
# Departure's steps, added to where the flight is (after LocalTC: 6,000 to
# 12,000; 3,000 at a time "would really congest" the frequency).
DEPARTURE_STEPS_FT = (6000, 8000, 10000, 12000)
# A step this short of departure's top is given as the top itself.
LAST_STEP_FT = 4000
# This close below its altitude, still climbing, a departure gets the next
# step at once (the IFR plan: "within ~1,500 ft of the last one").
DEPARTURE_REACHING_FT = 1500
# Climbing, by the vertical speed: anything less is levelling off.
CLIMBING_FPM = 300

# A centre's usual intermediate level on the way up, one per centre, used
# when the cruise is at least CENTRE_STEP_GAP_FT above it (after LocalTC).
CENTRE_STEPS_FT = (23000, 24000, 26000, 28000)
CENTRE_STEP_GAP_FT = 4000
# Within this of the level given, the next climb is on its way after a wait
# that is each centre's own pace (after LocalTC: 1,000 ft, 10 to 60 s).
CENTRE_REACHING_FT = 1000
CENTRE_WAIT_S = (10.0, 60.0)

# Without the departure area's outline: departure is finished with a flight
# this high above its field or this far from it (the IFR plan, Phase 1).
DEPARTURE_ENDS_AGL_FT = 17000.0
DEPARTURE_ENDS_NM = 30.0
# Nearly at departure's top: within this of it, still climbing, with more
# climb to come, the flight goes to the centre before it levels off.
NEAR_TOP_FT = 2500


def _hash(*parts) -> int:
    return zlib.crc32("|".join(str(p) for p in parts).encode("utf-8"))


def initial_altitude(field_elev_ft: float, field: str, sid: str = "",
                     runway: str = "", callsign: str = "",
                     floor_ft: float = 0.0) -> int:
    """The altitude the IFR clearance climbs a departure to.

    ``floor_ft`` is the terrain's MSA-style floor round the field
    (:func:`navdata.terrain.floor_ft`); a step below it is lifted to it,
    rounded up to the thousand.
    """
    elevation = max(0.0, float(field_elev_ft or 0.0))
    base = max(INITIAL_MIN_FT, int(math.ceil((elevation + 3000) / 1000.0)) * 1000)
    steps = [base + extra for extra in INITIAL_STEPS_FT
             if base + extra <= max(INITIAL_MAX_FT, base)]
    chosen = steps[_hash("initial", field, sid, runway, callsign) % len(steps)]
    if floor_ft and chosen < floor_ft:
        chosen = int(math.ceil(floor_ft / 1000.0)) * 1000
    return chosen


def departure_top(cruise_ft: float) -> int:
    """How high departure takes a flight: its airspace's top or the cruise."""
    cruise = int(cruise_ft or 0)
    return min(cruise, DEPARTURE_TOP_FT) if cruise else DEPARTURE_TOP_FT


def departure_step(station: str, callsign: str, assigned_ft: float,
                   here_ft: float, cruise_ft: float) -> int:
    """Departure's next altitude for a flight at ``here_ft``.

    A few thousand feet above where the flight is or was last given, the
    pick the same for the same controller and flight; the last bit of the
    way to the top in one go.
    """
    top = departure_top(cruise_ft)
    assigned = int(assigned_ft or 0)
    here = max(float(assigned), float(here_ft or 0.0))
    pick = DEPARTURE_STEPS_FT[_hash(station, callsign, assigned)
                              % len(DEPARTURE_STEPS_FT)]
    step = int(math.ceil((here + pick) / 1000.0)) * 1000
    return top if top - step < LAST_STEP_FT else min(step, top)


def centre_step(station: str, here_ft: float, assigned_ft: float,
                cruise_ft: float) -> int:
    """A centre's next altitude: its own usual level while the cruise is
    well above it and the flight well below it, else the cruise."""
    cruise = int(cruise_ft or 0)
    step = CENTRE_STEPS_FT[_hash(station) % len(CENTRE_STEPS_FT)]
    if cruise - step >= CENTRE_STEP_GAP_FT \
            and step > max(float(here_ft or 0), float(assigned_ft or 0)) + 2000:
        return step
    return cruise


def centre_wait_s(station: str) -> float:
    """How long a centre lets a flight sit near its level before the next
    climb: each its own, the same every time."""
    low, high = CENTRE_WAIT_S
    return low + (_hash("wait", station) % 100) / 100.0 * (high - low)


def departure_step_due(here_ft: float, vs_fpm: float, assigned_ft: float,
                       cruise_ft: float) -> bool:
    """Departure owes the next step: climbing, nearly at what it was given,
    and what it was given is not yet departure's top."""
    if not assigned_ft or not cruise_ft:
        return False
    if assigned_ft >= departure_top(cruise_ft):
        return False
    if vs_fpm < CLIMBING_FPM:
        return False
    return here_ft >= assigned_ft - DEPARTURE_REACHING_FT


def leaving_departure(agl_ft: float, out_nm: float, here_ft: float,
                      vs_fpm: float, assigned_ft: float,
                      cruise_ft: float, in_area: bool | None = None,
                      area_ceiling_ft: float = 0.0) -> str:
    """Why departure is finished with a flight, or "" while it is not.

    Above its airspace, beyond its reach, or nearly at its top with more
    climb to come -- the last so the climb goes on without a level-off at
    17,000 ft while the handoff waits. ``in_area`` is whether the flight is
    inside the departure field's approach area (navdata.approach_areas), or
    None where the field has none: with an area, its outline and its
    ceiling are the airspace, not 30 nm and 17,000 ft above the field.
    """
    if in_area is not None:
        if here_ft > area_ceiling_ft:
            return "above the area"
        if not in_area:
            return "out of the area"
    else:
        if agl_ft >= DEPARTURE_ENDS_AGL_FT:
            return "above"
        if out_nm >= DEPARTURE_ENDS_NM:
            return "away"
    cruise = float(cruise_ft or 0)
    assigned = float(assigned_ft or 0)
    if (assigned and cruise > assigned + 1000
            and assigned >= departure_top(cruise)
            and here_ft >= assigned - NEAR_TOP_FT and vs_fpm > CLIMBING_FPM):
        return "near_top"
    return ""


__all__ = ["initial_altitude", "departure_top", "departure_step",
           "centre_step", "centre_wait_s", "departure_step_due",
           "leaving_departure", "DEPARTURE_TOP_FT"]
