# -*- coding: utf-8 -*-
"""Which callout is due, by the class of aeroplane and the rules flown.

The first officer used to make one set of calls for every aeroplane, an
airliner's: "eighty knots, V one, rotate" in a Cessna, at a V1 guessed from
the type's first letters. Callouts belong to the class and to the rules:

* **A light aeroplane** (a piston single or twin) on its takeoff: "airspeed
  alive", "rotate" at the type's rotation speed, "positive rate", "five
  hundred".
* **A turboprop or a jet**: "eighty knots" ("one hundred knots" on an
  Airbus, as its SOP has it), "V one" only where a V1 is known (the pilot
  said it, or the table has one), "rotate", "V two", "positive rate", and
  "gear up" once the gear is seen to move.
* **An IFR flight**: "one thousand to go" to an assigned level, "localizer
  alive" and "glideslope alive", and on the way down "one thousand", "five
  hundred", "approaching minimums", "minimums" -- the thousand and the five
  hundred for every turboprop and jet whatever the rules, as the
  stabilised-approach calls they are.
* **A VFR flight**: the circuit, from :mod:`wilcoatc.atc.circuit` --
  downwind, abeam the numbers, base, and "final, runway 24, cleared" or
  "... not cleared yet" from the controller's own state; 50/30/10 in the
  flare when it is asked for; "runway vacated".
* **The deviations, any flight**: "speed" (ten knots off the approach
  speed, below a thousand feet), "sink rate" (more than a thousand feet a
  minute below a thousand feet), "bank" (more than thirty degrees below a
  thousand feet), "altitude" (three hundred feet off a level that had been
  reached). Each once, then not again for :data:`DEVIATION_EVERY_S`.

A helicopter gets the deviations and the IFR heights, none of the takeoff.

Nothing here speaks. :meth:`Callouts.watch` returns the cues due, and the
crew say them -- after the radio, never over it, and not at all once the
moment has gone (:mod:`wilcoatc.atc.crew`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .aircraft_class import (AIRLINER, BUSINESS_JET, HELICOPTER, PISTON_SINGLE,
                             PISTON_TWIN, TURBOPROP_SINGLE, TURBOPROP_TWIN)
from .session import Phase
from .vspeeds import Speeds

LIGHT = frozenset({PISTON_SINGLE, PISTON_TWIN})
HEAVY = frozenset({TURBOPROP_SINGLE, TURBOPROP_TWIN, BUSINESS_JET, AIRLINER})

# The airspeed indicator comes off its stop: UNVERIFIED, chosen as where a
# light aeroplane's ASI starts reading.
AIRSPEED_ALIVE_KT = 30.0
EIGHTY_KT = 80.0
ONE_HUNDRED_KT = 100.0
# A climb is a climb above this, feet a minute, and this high above where
# it began.
POSITIVE_RATE_FPM = 200.0
POSITIVE_RATE_FT = 30.0
# The gear is called up only this soon after the positive rate.
GEAR_UP_WITHIN_S = 120.0
FIVE_HUNDRED_FT = 500.0
ONE_THOUSAND_FT = 1000.0
THOUSAND_TO_GO_FT = 1000.0
# A needle off its stop: the CDI's full scale is 127, the glideslope's 119.
NEEDLE_ALIVE = 100.0
# Minimums with none stated: a CAT I decision height. UNVERIFIED as the
# day's: it is the commonest, not the pilot's.
DEFAULT_MINIMUMS_FT = 200.0
APPROACHING_MINIMUMS_FT = 100.0
# The deviations (Phase 2.3 of the plan): below this height, these limits.
DEVIATION_BELOW_FT = 1000.0
SPEED_LIMIT_KT = 10.0
SINK_LIMIT_FPM = 1000.0
BANK_LIMIT_DEG = 30.0
ALTITUDE_LIMIT_FT = 300.0
# A level is reached once within this of it; only then is leaving it a
# deviation, so a climb to a new level is not one.
ALTITUDE_CAPTURED_FT = 200.0
DEVIATION_EVERY_S = 30.0
# The speed call is not made in the flare, where the speed is meant to go.
SPEED_ABOVE_FT = 50.0
FLARE_HEIGHTS = (("fifty", 50.0), ("thirty", 30.0), ("ten", 10.0))

AIRBUS = ("A3", "A2", "A19", "A20", "A21", "A22")


@dataclass
class Situation:
    """What the calls depend on beyond the aircraft state."""

    aircraft_class: str
    type_code: str = ""
    rules: str = "vfr"
    speeds: Speeds | None = None
    phase: Phase = Phase.PREFLIGHT
    assigned_altitude_ft: float = 0.0
    # As the pilot gave it; a figure above the field's height plus a
    # hundred feet is a decision altitude, otherwise a decision height.
    minimums: float | None = None
    field_elevation_ft: float | None = None
    runway: str = ""
    cleared_to_land: bool = False
    # Where the circuit puts the aeroplane (circuit.Position.leg/stage).
    leg: str = ""
    stage: str = ""
    flare: bool = False


@dataclass(frozen=True)
class Call:
    cue: str
    values: dict = field(default_factory=dict)
    # Why, for logs crew.
    why: str = ""


def _crossed_up(before: float, now: float, mark: float) -> bool:
    return before < mark <= now


def _crossed_down(before: float, now: float, mark: float) -> bool:
    return before > mark >= now


class Callouts:
    """The callouts of one flight. Each is made once per takeoff, circuit or
    approach; the deviations once per :data:`DEVIATION_EVERY_S`."""

    def __init__(self, clock=time.monotonic,
                 deviation_every_s: float = DEVIATION_EVERY_S):
        self.clock = clock
        self.deviation_every_s = deviation_every_s
        self.begin()

    def begin(self) -> None:
        self._said: set[str] = set()
        self._deviation_at: dict[str, float] = {}
        self._rate_armed = False
        self._lowest = 0.0
        self._rate_at: float | None = None
        self._captured = 0.0
        self._assigned = 0.0
        self._leg = ""
        self._stage = ""
        self._landed = False
        # The aeroplane's height above its own wheels, from what the
        # simulator reported for it standing on the ground.
        self._wheels_ft = 0.0

    # -- the pilot ------------------------------------------------------

    def go_around(self) -> list[Call]:
        """The pilot called a go-around: "go around, flaps", and the climb's
        calls and the next approach's are due again."""
        for cue in ("positive_rate", "gear_up", "one_thousand",
                    "five_hundred", "approaching_minimums", "minimums",
                    "localizer_alive", "glideslope_alive", "five_hundred_up",
                    "final_cleared", "final_not_cleared", "fifty", "thirty",
                    "ten", "v_two"):
            self._said.discard(cue)
        self._rate_armed = True
        self._lowest = 1e9
        self._rate_at = None
        self._leg = ""
        return [Call("go_around", why="the pilot called it")]

    # -- the aeroplane -----------------------------------------------------

    def watch(self, previous, state, situation: Situation) -> list[Call]:
        """The calls due between ``previous`` and ``state``."""
        if previous is None or state is None:
            return []
        out: list[Call] = []
        cls = situation.aircraft_class
        if state.on_ground:
            self._wheels_ft = float(getattr(state, "altitude_agl_ft", 0.0)
                                    or 0.0)
        if previous.on_ground and not state.on_ground:
            # From the ground, not from the first reading in the air, which
            # may already be fifty feet up.
            self._rate_armed = True
            self._lowest = self._height(previous)
            self._landed = False
        if not previous.on_ground and state.on_ground:
            self._landed = True

        if cls != HELICOPTER:
            out += self._takeoff(previous, state, situation)
        out += self._climb(previous, state, situation)
        out += self._approach(previous, state, situation)
        if situation.rules == "vfr":
            out += self._circuit(previous, state, situation)
        out += self._deviations(previous, state, situation)
        return out

    def _once(self, cue: str, why: str = "", **values) -> list[Call]:
        if cue in self._said:
            return []
        self._said.add(cue)
        return [Call(cue, values, why)]

    def _height(self, state) -> float:
        agl = float(getattr(state, "altitude_agl_ft", 0.0) or 0.0)
        return max(0.0, agl - self._wheels_ft) if agl > 0 else 0.0

    # -- the takeoff -----------------------------------------------------

    def _takeoff(self, previous, state, s: Situation) -> list[Call]:
        out: list[Call] = []
        speeds = s.speeds
        before = previous.indicated_airspeed_kt or 0.0
        now = state.indicated_airspeed_kt or 0.0
        light = s.aircraft_class in LIGHT
        airbus = (s.type_code or "").upper().startswith(AIRBUS)
        rolling = state.on_ground and not self._landed and s.phase not in (
            Phase.LANDING, Phase.LANDED, Phase.TAXI_IN, Phase.PARKED)
        if rolling:
            if light and _crossed_up(before, now, AIRSPEED_ALIVE_KT):
                out += self._once("airspeed_alive")
            if not light:
                mark, cue = ((ONE_HUNDRED_KT, "one_hundred") if airbus
                             else (EIGHTY_KT, "eighty"))
                if _crossed_up(before, now, mark):
                    out += self._once(cue)
            if (not light and speeds is not None and speeds.v1
                    and _crossed_up(before, now, speeds.v1)):
                out += self._once("v_one", why=speeds.source)
            if (speeds is not None and speeds.vr
                    and _crossed_up(before, now, speeds.vr)):
                out += self._once("rotate", why=speeds.source)
        if not state.on_ground and not light and speeds is not None \
                and speeds.v2 and "v_one" in self._said \
                and _crossed_up(before, now, speeds.v2):
            out += self._once("v_two", why=speeds.source)
        return out

    # -- the climb ----------------------------------------------------------

    def _climb(self, previous, state, s: Situation) -> list[Call]:
        out: list[Call] = []
        height = self._height(state)
        if self._rate_armed and not state.on_ground:
            self._lowest = min(self._lowest, height)
            if (s.aircraft_class != HELICOPTER
                    and state.vertical_speed_fpm > POSITIVE_RATE_FPM
                    and height - self._lowest >= POSITIVE_RATE_FT):
                self._rate_armed = False
                self._rate_at = self.clock()
                out += self._once("positive_rate")
        if (self._rate_at is not None and previous.gear_down
                and not state.gear_down
                and self.clock() - self._rate_at <= GEAR_UP_WITHIN_S):
            out += self._once("gear_up", why="the gear handle went up")
        if (s.aircraft_class in LIGHT and not state.on_ground
                and state.vertical_speed_fpm > 0 and self._rate_at is not None
                and _crossed_up(self._height(previous), height,
                                FIVE_HUNDRED_FT)):
            out += self._once("five_hundred_up")
        # A level the controller gave, a thousand feet before it.
        assigned = s.assigned_altitude_ft or 0.0
        if assigned != self._assigned:
            self._assigned = assigned
            self._captured = 0.0
            self._said.discard("thousand_to_go")
        if s.rules == "ifr" and assigned and not state.on_ground:
            before = abs(assigned - (previous.altitude_ft or 0.0))
            now = abs(assigned - (state.altitude_ft or 0.0))
            if _crossed_down(before, now, THOUSAND_TO_GO_FT) \
                    and abs(state.vertical_speed_fpm) > 300:
                out += self._once("thousand_to_go", level=int(assigned))
        return out

    # -- the approach ---------------------------------------------------

    def _minimums(self, s: Situation, previous, state):
        """(before, now, minimums) on one scale, or None: a decision
        altitude against the altitude, a decision height against the
        height. A figure above the field plus a hundred feet is an altitude;
        with the field's elevation unknown, only one above 2,000 ft is."""
        if s.rules != "ifr":
            return None
        figure = s.minimums
        if figure is not None:
            floor = (s.field_elevation_ft + 100.0
                     if s.field_elevation_ft is not None else 2000.0)
            if figure > floor:
                return (float(previous.altitude_ft or 0.0),
                        float(state.altitude_ft or 0.0), float(figure))
        return (self._height(previous), self._height(state),
                float(figure if figure is not None else DEFAULT_MINIMUMS_FT))

    def _approach(self, previous, state, s: Situation) -> list[Call]:
        out: list[Call] = []
        if state.on_ground:
            return out
        descending = state.vertical_speed_fpm < -100
        if s.rules == "ifr" and state.vertical_speed_fpm < 300:
            if (state.nav1_has_localizer and abs(state.nav1_cdi) < NEEDLE_ALIVE
                    and (abs(previous.nav1_cdi) >= NEEDLE_ALIVE
                         or not previous.nav1_has_localizer)):
                out += self._once("localizer_alive")
            if (state.nav1_has_glideslope
                    and abs(state.nav1_gsi) < NEEDLE_ALIVE
                    and (abs(previous.nav1_gsi) >= NEEDLE_ALIVE
                         or not previous.nav1_has_glideslope)):
                out += self._once("glideslope_alive")
        if not descending:
            return out
        before, now = self._height(previous), self._height(state)
        if s.aircraft_class in HEAVY or s.rules == "ifr":
            if _crossed_down(before, now, ONE_THOUSAND_FT):
                out += self._once("one_thousand")
            if _crossed_down(before, now, FIVE_HUNDRED_FT):
                out += self._once("five_hundred")
        scale = self._minimums(s, previous, state)
        if scale is not None:
            was, here, minimums = scale
            if _crossed_down(was, here, minimums + APPROACHING_MINIMUMS_FT):
                out += self._once("approaching_minimums")
            if _crossed_down(was, here, minimums):
                out += self._once("minimums", why=f"minimums {minimums:.0f}")
        return out

    # -- the circuit ------------------------------------------------------

    def _circuit(self, previous, state, s: Situation) -> list[Call]:
        out: list[Call] = []
        leg, stage = (s.leg or ""), (s.stage or "")
        if leg in ("upwind", "crosswind") and self._leg not in ("", "upwind",
                                                                "crosswind"):
            # Round again: the circuit's calls are due once more.
            for cue in ("downwind", "abeam", "base", "final_cleared",
                        "final_not_cleared", "fifty", "thirty", "ten"):
                self._said.discard(cue)
        if not state.on_ground:
            if leg == "downwind" and self._leg != "downwind":
                out += self._once("downwind")
            if leg == "downwind" and stage == "late" and self._stage != "late":
                out += self._once("abeam")
            if leg == "base" and self._leg != "base":
                out += self._once("base")
            if leg == "final" and self._leg != "final":
                cue = ("final_cleared" if s.cleared_to_land
                       else "final_not_cleared")
                out += self._once(cue, runway=s.runway)
            if s.flare and s.aircraft_class in LIGHT \
                    and state.vertical_speed_fpm < 0:
                before, now = self._height(previous), self._height(state)
                for cue, mark in FLARE_HEIGHTS:
                    if _crossed_down(before, now, mark):
                        out += self._once(cue)
        if (self._landed and state.on_ground and s.phase is Phase.TAXI_IN):
            out += self._once("runway_vacated")
        self._leg, self._stage = leg, stage
        return out

    # -- the deviations ---------------------------------------------------

    def _deviation(self, cue: str, why: str) -> list[Call]:
        now = self.clock()
        at = self._deviation_at.get(cue)
        if at is not None and now - at < self.deviation_every_s:
            return []
        self._deviation_at[cue] = now
        return [Call(cue, why=why)]

    def _deviations(self, previous, state, s: Situation) -> list[Call]:
        out: list[Call] = []
        if state.on_ground:
            return out
        height = self._height(state)
        low = 0.0 < height < DEVIATION_BELOW_FT
        if low and state.vertical_speed_fpm < -SINK_LIMIT_FPM:
            out += self._deviation("sink_rate",
                                   f"{state.vertical_speed_fpm:.0f} fpm at "
                                   f"{height:.0f} ft")
        if low and abs(getattr(state, "bank_deg", 0.0) or 0.0) > BANK_LIMIT_DEG:
            out += self._deviation("bank", f"{state.bank_deg:.0f} degrees at "
                                           f"{height:.0f} ft")
        target = None
        if s.speeds is not None and s.speeds.vapp:
            target = float(s.speeds.vapp)
        elif getattr(state, "ap_airspeed_kt", 0.0):
            target = float(state.ap_airspeed_kt)
        on_final = (low and state.vertical_speed_fpm < 0
                    and height > SPEED_ABOVE_FT) or (
            s.leg == "final" and height > SPEED_ABOVE_FT)
        # No speed read is no speed call: zero is the simulator not saying.
        if target and on_final and state.indicated_airspeed_kt and abs(
                (state.indicated_airspeed_kt or 0.0) - target) > SPEED_LIMIT_KT:
            out += self._deviation("speed",
                                   f"{state.indicated_airspeed_kt:.0f} kt "
                                   f"against {target:.0f}")
        assigned = s.assigned_altitude_ft or 0.0
        if assigned:
            off = abs((state.altitude_ft or 0.0) - assigned)
            if off <= ALTITUDE_CAPTURED_FT:
                self._captured = assigned
            elif self._captured == assigned and off > ALTITUDE_LIMIT_FT:
                out += self._deviation("altitude",
                                       f"{off:.0f} ft off {assigned:.0f}")
        return out


__all__ = ["Call", "Callouts", "DEVIATION_EVERY_S", "Situation"]
