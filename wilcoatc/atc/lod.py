"""Which model each parked aeroplane is drawn with: a detailed one near the
pilot, a light one far away.

Two kinds of model draw the invented traffic. The simulator's own AI models
(``Asobo PassiveAircraft ...``) have doors and holds that open, and cost
what a full aeroplane costs. FSLTL's models are painted in the airline's
colours and are light enough to have sixty of on an apron, and have no door
animation at all. A pilot at the gate wants the first on the stands beside
them and the second everywhere else, which is what this decides: the
*level* of detail per aeroplane, from how far it is from the pilot.

What it is careful about:

*The aeroplane is the same aeroplane.* Its identity -- callsign, flight,
plan, where it is in the controller's sequence -- is the traffic's, and the
simulator object is a detail of drawing it. A swap changes the object and
nothing else; the radio never hears of it.

*Hysteresis.* Detailed inside one distance, light beyond a further one, and
between the two whatever it already is, so a pilot taxiing along the apron
does not have the stands flickering as the thresholds pass over them.

*A cap.* At most ``limit`` detailed aeroplanes, the nearest first. The next
nearest stays light however close it is.

*Only when nothing is moving.* A swap is a new object created and the old
one removed, and done to an aeroplane taxiing or flying it is a jump. Only
an aeroplane stationary on its stand is swapped; everything else keeps the
model it has until it parks.

*Not all at once.* A swap costs the simulator a model load, so they are
spaced: so many a second, nearest first.

*Or by the schedule* (``mode: schedule``). Distance alone gives a detailed
model to an aeroplane that has been parked with its doors shut for an
hour, which is a model load spent on doors that will never move. In this
mode an aeroplane is detailed only in its turnaround window -- from
on-block, or from shortly before boarding, until its doors read shut for
the push -- and only near the pilot and under the cap. Outside it, light.
The window is worked out by the engine from the traffic, the handling and
the doors (:data:`WINDOWS`); this decides from it.

Two things it is careful about that distance alone did not need:

*The livery.* The simulator's own models are painted for only a couple of
dozen airlines (measured off a running 2024: Qatar, KLM, easyJet, Condor...
no TAP, no Ryanair), so for most aeroplanes the two models are two
different liveries, and a swap in front of the pilot is a repaint. A swap
that would change the livery is made only when the pilot is further than
``hide_m`` from the aeroplane; one that cannot be hidden is not made, and
the aeroplane keeps the model it has for this turnaround. ``livery:
match`` goes further and never draws an aeroplane detailed in a livery
that is not its own.

*The doors.* Never a swap with a door moving; and with a door open --
the stairs already there -- only where it cannot be seen. The swap is
meant to happen before the doors open: on block-in, before the stairs
arrive, or in the minutes between a departure being booked and its
boarding starting. The best swap is none: an arrival whose stand is near
the pilot is created detailed and never changes at the gate.

It knows nothing about SimConnect. :class:`wilcoatc.integrations.aitraffic
.SimTraffic` does the swap itself -- new object first, old one removed once
the new one has had time to load -- and carries the doors over.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

log = logging.getLogger("wilcoatc.lod")

DETAILED = "detailed"
LIGHT = "light"

DISTANCE = "distance"
SCHEDULE = "schedule"
MODES = (DISTANCE, SCHEDULE)

# Where an aeroplane is in its day, for the schedule mode.
#: On its stand from block-in until the deplaning round is over and its
#: doors read shut.
ARRIVAL = "arrival"
#: Standing on a stand outside any turnaround, doors shut.
PARKED = "parked"
#: Booked as a departure, boarding not yet started: the moment to swap.
PRE_BOARDING = "pre_boarding"
#: Boarding, until every door reads shut.
BOARDING = "boarding"
#: Doors read shut, waiting for the tug.
CLOSED = "closed"
#: Pushing, taxiing, flying.
MOVING = "moving"
WINDOWS = (ARRIVAL, PARKED, PRE_BOARDING, BOARDING, CLOSED, MOVING)
_IN_WINDOW = frozenset({ARRIVAL, PRE_BOARDING, BOARDING})

# Which liveries a detailed model may be drawn in: any (its own where the
# simulator paints one, the house livery otherwise), or only its own.
LIVERY_ANY = "any"
LIVERY_MATCH = "match"
LIVERIES = (LIVERY_ANY, LIVERY_MATCH)

#: How far past the detail radius an aeroplane may be before it is
#: lightened: the same band distance mode keeps between its two thresholds,
#: so a pilot taxiing along the radius does not have the stands flickering.
BAND_M = 500.0


@dataclass
class Candidate:
    """One invented aeroplane, as far as the decision needs to know it."""

    callsign: str
    x: float
    z: float
    # What it is drawn as now, or "" for one not yet drawn.
    level: str = ""
    # Stationary on its stand: the only state a swap is allowed in.
    stationary: bool = False
    # Whether a detailed model exists for it at all. A type with none
    # stays light wherever it is.
    has_detailed: bool = True
    # Between two objects right now, on its way to ``level``. Never
    # swapped again until that is over, but counted: a swap in flight
    # that did not count against the cap let the next nearest aeroplane
    # in, and the two then took turns for the rest of the session.
    busy: bool = False
    # The schedule mode's view of it; distance mode reads none of these.
    # Where it is in its day (one of WINDOWS).
    window: str = PARKED
    # A door open, or wanted open (the stairs are there): swapped only
    # where the pilot cannot see it.
    doors_open: bool = False
    # A door between shut and open, or toggled a moment ago: never swapped.
    doors_moving: bool = False
    # Whether its detailed model is painted the way its light one is, so a
    # swap changes the model and not the colours.
    livery_matches: bool = True


def wanted_level(one: Candidate, distance: float, radius_m: float,
                 livery: str = LIVERY_ANY) -> str:
    """What an aeroplane should be drawn as, by its window, before the cap.

    Detailed in its window and inside the radius; light outside its window
    or beyond the radius and its band; whatever it is now inside the band,
    while it moves, and once its doors are shut for the push -- it is about
    to leave, and a swap then is a model load for a minute of standing
    still.
    """
    current = one.level or LIGHT
    if one.busy:
        return current
    if not one.has_detailed or (livery == LIVERY_MATCH
                                and not one.livery_matches):
        return LIGHT
    if one.window in (MOVING, CLOSED):
        return current
    if one.window not in _IN_WINDOW:
        return LIGHT
    if distance < radius_m:
        return DETAILED
    if distance > radius_m + BAND_M:
        return LIGHT
    return current


def may_swap(one: Candidate, distance: float, hide_m: float) -> bool:
    """Whether a swap may be made to this aeroplane now, in schedule mode.

    Standing still, not mid-swap, no door moving -- and where the swap
    would show, as a new livery or as doors that were open on the old model
    being opened again on the new one, only out of the pilot's sight.
    """
    if not one.stationary or one.busy or one.doors_moving:
        return False
    hidden = distance > hide_m
    if not one.livery_matches and not hidden:
        return False
    if one.doors_open and not hidden:
        return False
    return True


class Lod:
    """The level-of-detail decision, one tick at a time."""

    def __init__(self, enabled: bool = True, detailed_m: float = 1500.0,
                 light_m: float = 2500.0, limit: int = 6,
                 swaps_per_s: float = 0.5, mode: str = DISTANCE,
                 radius_m: float = 3000.0, hide_m: float = 1200.0,
                 livery: str = LIVERY_MATCH):
        self.enabled = enabled
        self.mode = mode if mode in MODES else DISTANCE
        self.radius_m = max(0.0, float(radius_m))
        self.hide_m = max(0.0, float(hide_m))
        self.livery = livery if livery in LIVERIES else LIVERY_MATCH
        self.detailed_m = float(detailed_m)
        # The far threshold is never inside the near one: that is a band of
        # negative width, and every aeroplane in it would want both.
        self.light_m = max(float(light_m), self.detailed_m)
        self.limit = max(0, int(limit))
        self.swaps_per_s = max(0.0, float(swaps_per_s))
        self._last_swap: float | None = None
        self._said = False

    # -- for a new aeroplane --------------------------------------------

    @property
    def scheduled(self) -> bool:
        return self.enabled and self.mode == SCHEDULE

    def level_for_new(self, distance_m: float | None, detailed_now: int,
                      has_detailed: bool = True, window: str = PARKED,
                      livery_matches: bool = True) -> str:
        """What to draw an aeroplane as when it is first created.

        In schedule mode ``distance_m`` is to where it will turn round -- an
        arrival's reserved stand, not the approach it is on -- and
        ``window`` the turnaround it will be in there: an arrival with a
        stand near the pilot is created detailed, so it never has to be
        swapped at the gate. A new aeroplane has no old livery to be seen
        changing, so only ``livery: match`` asks about it.
        """
        if not self.enabled:
            return ""
        if self.mode == SCHEDULE:
            if (has_detailed and window in _IN_WINDOW
                    and (self.livery != LIVERY_MATCH or livery_matches)
                    and distance_m is not None and distance_m < self.radius_m
                    and detailed_now < self.limit):
                return DETAILED
            return LIGHT
        if (has_detailed and distance_m is not None
                and distance_m < self.detailed_m
                and detailed_now < self.limit):
            return DETAILED
        return LIGHT

    # -- for the ones already drawn --------------------------------------

    def decide(self, candidates: list[Candidate],
               user_xy: tuple[float, float] | None,
               now: float) -> list[tuple[str, str]]:
        """The swaps to make this tick: (callsign, level), nearest first.

        Nothing with no pilot position to measure from, nothing while
        switched off, and never more than the rate allows.
        """
        if not self.enabled or user_xy is None or not candidates:
            return []
        allowed = self._allowance(now)
        if allowed == 0:
            return []
        if self.mode == SCHEDULE:
            return self._done(self._decide_scheduled(candidates, user_xy),
                              allowed, now)

        by_distance = sorted(
            ((math.hypot(c.x - user_xy[0], c.z - user_xy[1]), c)
             for c in candidates), key=lambda item: (item[0], item[1].callsign))
        # What each one would be, by distance alone, keeping what it is
        # inside the band.
        wanted: dict[str, str] = {}
        for distance, one in by_distance:
            if one.busy:
                wanted[one.callsign] = one.level or LIGHT
            elif not one.has_detailed:
                wanted[one.callsign] = LIGHT
            elif distance < self.detailed_m:
                wanted[one.callsign] = DETAILED
            elif distance > self.light_m:
                wanted[one.callsign] = LIGHT
            else:
                wanted[one.callsign] = one.level or LIGHT
        # Then the cap: the nearest ``limit`` of those keep it, the rest
        # go light however close they are.
        # A swap in flight to detailed holds its place whatever its
        # distance; it is not going to be undone.
        kept = sum(1 for _, one in by_distance
                   if one.busy and wanted[one.callsign] == DETAILED)
        for distance, one in by_distance:
            if one.busy or wanted[one.callsign] != DETAILED:
                continue
            if kept < self.limit:
                kept += 1
            else:
                wanted[one.callsign] = LIGHT

        swaps: list[tuple[str, str]] = []
        # Lightening first: it frees a model load before the next one is
        # asked for. Then detail, nearest first.
        for level in (LIGHT, DETAILED):
            for distance, one in by_distance:
                # One drawn with no level asked was drawn the light way.
                if wanted[one.callsign] != level or (one.level or LIGHT) == level:
                    continue
                if not one.stationary or one.busy:
                    continue           # moving, or mid-swap: it keeps what it has
                swaps.append((one.callsign, level))
        return self._done(swaps, allowed, now)

    def _decide_scheduled(self, candidates: list[Candidate],
                          user_xy: tuple[float, float]
                          ) -> list[tuple[str, str]]:
        """The schedule mode's swaps, before the rate: the window, then the
        cap, then whether each swap may be made where the pilot is."""
        by_distance = sorted(
            ((math.hypot(c.x - user_xy[0], c.z - user_xy[1]), c)
             for c in candidates), key=lambda item: (item[0], item[1].callsign))
        wanted = {one.callsign: wanted_level(one, distance, self.radius_m,
                                             self.livery)
                  for distance, one in by_distance}

        # The cap. What cannot change -- mid-swap, moving, shut for the
        # push -- holds its place first, but only near the pilot: a detailed
        # departure climbing out ten miles away costs the apron nothing.
        def fixed(one: Candidate) -> bool:
            return one.busy or one.window in (MOVING, CLOSED)

        reach = self.radius_m + BAND_M
        kept = sum(1 for distance, one in by_distance
                   if fixed(one) and distance <= reach
                   and wanted[one.callsign] == DETAILED)
        for distance, one in by_distance:
            if fixed(one) or wanted[one.callsign] != DETAILED:
                continue
            if kept < self.limit:
                kept += 1
            else:
                wanted[one.callsign] = LIGHT
        swaps: list[tuple[str, str]] = []
        for level in (LIGHT, DETAILED):
            for distance, one in by_distance:
                if wanted[one.callsign] != level or (one.level or LIGHT) == level:
                    continue
                if not may_swap(one, distance, self.hide_m):
                    continue           # it keeps the model it has, for now
                swaps.append((one.callsign, level))
        return swaps

    def _done(self, swaps: list[tuple[str, str]], allowed: int,
              now: float) -> list[tuple[str, str]]:
        swaps = swaps[:allowed]
        if swaps:
            self._last_swap = now
            if not self._said:
                self._said = True
                log.info("lod: the first swap, %s to %s", swaps[0][0],
                         swaps[0][1])
        return swaps

    def _allowance(self, now: float) -> int:
        """How many swaps the rate permits this tick."""
        if self.swaps_per_s <= 0.0:
            return 0
        if self._last_swap is None:
            # Nothing owed yet: a second's worth, and at least one.
            return max(1, int(self.swaps_per_s))
        interval = 1.0 / self.swaps_per_s
        return int((now - self._last_swap) // interval)


__all__ = ["Lod", "Candidate", "DETAILED", "LIGHT", "DISTANCE", "SCHEDULE",
           "ARRIVAL", "PARKED", "PRE_BOARDING", "BOARDING", "CLOSED",
           "MOVING", "WINDOWS", "LIVERY_ANY", "LIVERY_MATCH", "wanted_level",
           "may_swap"]
