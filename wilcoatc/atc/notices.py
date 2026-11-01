# -*- coding: utf-8 -*-
"""What the first officer notices without being asked: a second pair of
eyes.

The callouts (:mod:`wilcoatc.atc.callouts`) say where the aeroplane is;
these say what the pilot has forgotten, the way a colleague would --
"gear's still up", "we're still on standby on the transponder" -- never as
an order. Each is said once, and only when it matters:

* at line-up: the transponder still on standby; the flaps outside what the
  class's own before-takeoff check allows; the parking brake on with
  takeoff power;
* on final: the landing light off; the gear still up at five hundred feet
  -- for an aeroplane seen to move its gear this flight, or one whose class
  always retracts it (a C172 and a Caravan have none to forget);
* after the controller: the altimeter still off the QNH they gave, after
  :data:`ALTIMETER_AFTER_S` and below the transition altitude; the
  frequency they gave in neither box after :data:`FREQUENCY_AFTER_S`;
* the fuel: below the reserve the pilot stated; still on a tank running
  low while the other is full; the selector off with the engine running.

A variable a study-level add-on does not publish (its file in
``checklists/types/``) is never noticed: a notice from a value nobody
drives is a false alarm, and false alarms teach a pilot to stop listening.

Nothing here speaks; :meth:`Notices.watch` returns what is due, and the
crew say it after the radio, writing down the ones the radio held back.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..navdata.channels import same_channel
from .aircraft_class import (AIRLINER, BUSINESS_JET, PISTON_SINGLE,
                             PISTON_TWIN, TURBOPROP_SINGLE, TURBOPROP_TWIN)
from .callouts import Call
from .checklist import holds, _hpa
from .checklist_files import Check
from .session import Phase

# How long after the controller gave a QNH or a frequency the first officer
# says it is not set. UNVERIFIED: chosen, as the time a pilot takes to set
# one with the readback done.
ALTIMETER_AFTER_S = 30.0
FREQUENCY_AFTER_S = 20.0
# The gear call, at this height on final.
GEAR_FT = 500.0
# Final, for the landing light: below this on an approach.
FINAL_BELOW_FT = 1000.0
# Takeoff power, per cent of the throttle's travel.
TAKEOFF_POWER_PCT = 90.0
# A tank running low: under this, or under this share of the other side.
LOW_TANK_GAL = 1.0
LOW_TANK_SHARE = 0.25
OTHER_TANK_GAL = 2.0
# An altimeter this far off the QNH is not set.
ALTIMETER_OFF_HPA = 1.0

# The classes whose gear always retracts; the others only once it is seen
# to move.
RETRACTABLE = frozenset({PISTON_TWIN, TURBOPROP_TWIN, BUSINESS_JET, AIRLINER})

# FUEL TANK SELECTOR's left and right tanks (left, right, left main, right
# main). UNVERIFIED on a running 2024: the SDK's enumeration.
LEFT_TANKS = frozenset({2, 19})
RIGHT_TANKS = frozenset({3, 20})

#: The notices worth breaking a quiet cockpit for.
SAFETY = frozenset({"gear_up_final", "fuel_selector_off", "fuel_reserve",
                    "parking_brake_power", "xpdr_standby"})


@dataclass
class Watching:
    """What the notices depend on beyond the aircraft state."""

    aircraft_class: str = PISTON_SINGLE
    phase: Phase = Phase.PREFLIGHT
    leg: str = ""
    # What a study-level add-on does not publish.
    unchecked: frozenset = frozenset()
    # The class's before-takeoff flaps check, if it has one.
    takeoff_flaps: Check | None = None
    # The QNH the controller gave (hPa or inches) and when; the transition
    # altitude, above which the altimeter is on standard.
    qnh: float = 0.0
    qnh_given_at: float = 0.0
    transition_altitude_ft: float = 18000.0
    # The frequency the controller gave, and when.
    frequency: float = 0.0
    frequency_given_at: float = 0.0
    # The reserve the pilot stated, by volume or by weight.
    reserve_gal: float = 0.0
    reserve_lb: float = 0.0


class Notices:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.begin()

    def begin(self) -> None:
        self._said: set = set()
        self._gear_moved = False

    def go_around(self) -> None:
        """A new approach: the final's notices are due again."""
        self._said -= {"gear_up_final", "landing_light_off"}

    def _once(self, key, cue: str, why: str = "", **values) -> list[Call]:
        if key in self._said:
            return []
        self._said.add(key)
        return [Call(cue, values, why)]

    def watch(self, previous, state, w: Watching) -> list[Call]:
        if previous is None or state is None:
            return []
        if (not state.on_ground and bool(previous.gear_down)
                != bool(state.gear_down)):
            self._gear_moved = True
        out: list[Call] = []
        out += self._line_up(state, w)
        out += self._final(previous, state, w)
        out += self._controller(state, w)
        out += self._fuel(state, w)
        return out

    @staticmethod
    def _known(var: str, w: Watching) -> bool:
        return var not in w.unchecked

    # -- at line-up -------------------------------------------------------

    def _line_up(self, state, w: Watching) -> list[Call]:
        out: list[Call] = []
        lined_up = w.phase is Phase.LINED_UP or (
            w.phase is Phase.TAKEOFF and state.on_ground)
        if not lined_up:
            return out
        if (self._known("transponder_state", w)
                and getattr(state, "transponder_state", -1) in (0, 1)):
            out += self._once("xpdr_standby", "xpdr_standby",
                              why=f"TRANSPONDER STATE {state.transponder_state}")
        if (w.takeoff_flaps is not None and self._known("flaps_index", w)
                and not holds(w.takeoff_flaps, state.flaps_index,
                              w.takeoff_flaps.value)):
            out += self._once("flaps_takeoff", "flaps_takeoff",
                              why=f"flaps notch {state.flaps_index}")
        if (state.on_ground and state.parking_brake
                and self._known("parking_brake", w)
                and (getattr(state, "throttle_pct", 0.0) or 0.0)
                >= TAKEOFF_POWER_PCT):
            out += self._once("parking_brake_power", "parking_brake_power",
                              why=f"throttle {state.throttle_pct:.0f}%")
        return out

    # -- on final ---------------------------------------------------------

    def _final(self, previous, state, w: Watching) -> list[Call]:
        out: list[Call] = []
        if state.on_ground:
            return out
        height = float(getattr(state, "altitude_agl_ft", 0.0) or 0.0)
        approaching = (w.phase in (Phase.APPROACH, Phase.LANDING)
                       and state.vertical_speed_fpm < 0)
        on_final = (w.leg or "") == "final" or (
            approaching and 0.0 < height < FINAL_BELOW_FT)
        if (on_final and self._known("landing_light", w)
                and not state.landing_light):
            out += self._once("landing_light_off", "landing_light_off")
        retractable = (w.aircraft_class in RETRACTABLE) or self._gear_moved
        if ((on_final or approaching) and retractable
                and self._known("gear_down", w) and not state.gear_down
                and 0.0 < height <= GEAR_FT and state.vertical_speed_fpm < 0):
            out += self._once("gear_up_final", "gear_up_final",
                              why=f"gear handle up at {height:.0f} ft")
        return out

    # -- after the controller -------------------------------------------

    def _controller(self, state, w: Watching) -> list[Call]:
        out: list[Call] = []
        now = self.clock()
        setting = float(getattr(state, "kohlsman_hpa", 0.0) or 0.0)
        if (w.qnh and setting and self._known("kohlsman_hpa", w)
                and now - w.qnh_given_at >= ALTIMETER_AFTER_S
                and (state.on_ground or (state.altitude_ft or 0.0)
                     < w.transition_altitude_ft)
                and abs(_hpa(setting) - _hpa(w.qnh)) > ALTIMETER_OFF_HPA):
            out += self._once(("altimeter", w.qnh, w.qnh_given_at),
                              "altimeter_not_set", qnh=w.qnh, set=setting,
                              why=f"set {setting:.1f}, given {w.qnh}")
        if w.frequency and now - w.frequency_given_at >= FREQUENCY_AFTER_S:
            # By channel: 126.630 on the radio is the 126.625 given, and
            # the first officer said it was not set.
            tuned = any(same_channel(getattr(state, box, 0.0) or 0.0,
                                     w.frequency, 0)
                        for box in ("com1_active", "com1_standby",
                                    "com2_active", "com2_standby")
                        if getattr(state, box, 0.0))
            if not tuned:
                out += self._once(("frequency", w.frequency,
                                   w.frequency_given_at),
                                  "frequency_not_tuned",
                                  frequency=w.frequency)
        return out

    # -- the fuel -----------------------------------------------------------

    def _fuel(self, state, w: Watching) -> list[Call]:
        out: list[Call] = []
        if state.on_ground:
            return out
        gal = float(getattr(state, "fuel_total_gal", 0.0) or 0.0)
        lb = float(getattr(state, "fuel_total_lb", 0.0) or 0.0)
        if ((w.reserve_gal and 0.0 < gal < w.reserve_gal)
                or (w.reserve_lb and 0.0 < lb < w.reserve_lb)):
            out += self._once("fuel_reserve", "fuel_reserve",
                              why=f"{gal:.1f} gal / {lb:.0f} lb")
        if not self._known("fuel_selector", w):
            return out
        selector = getattr(state, "fuel_selector", -1)
        if selector == 0 and state.engines_running:
            out += self._once("selector_off", "fuel_selector_off")
        left = float(getattr(state, "fuel_left_gal", 0.0) or 0.0)
        right = float(getattr(state, "fuel_right_gal", 0.0) or 0.0)
        for tanks, this, other, side in ((LEFT_TANKS, left, right, "left"),
                                         (RIGHT_TANKS, right, left, "right")):
            if selector in tanks and other > OTHER_TANK_GAL and (
                    this < LOW_TANK_GAL or this < other * LOW_TANK_SHARE):
                out += self._once(("tank", selector), "wrong_tank", side=side,
                                  why=f"{this:.1f} gal against {other:.1f}")
        return out


__all__ = ["ALTIMETER_AFTER_S", "FREQUENCY_AFTER_S", "Notices", "SAFETY",
           "Watching"]
