"""What an invented aeroplane's engines and lights should be doing.

Every AI aeroplane the simulator creates comes with its engines running and
its lights off, and stays that way: a dozen airliners parked at the gates,
all of them at idle, all night, and a departure pushing back dark. Measured
at Madeira on a TAP A321 (FSLTL's model): the simulator obeys the ordinary
events aimed at the object -- ENGINE_AUTO_SHUTDOWN and ENGINE_AUTO_START,
NAV_/BEACON_/STROBES_/TAXI_/LANDING_LIGHTS_SET -- and reads them back on the
object; no variable has to be written. That is the job of
:mod:`wilcoatc.integrations.systems`. This is only the decision: given where
an aeroplane is in its day, which of them should be on.

The day, as an airline runs it:

*On the stand,* engines off and the navigation lights on -- the aeroplane
is powered, by the ground or the APU. Nothing else.

*Ready to go,* the beacon comes on once every door reads shut: the signal to
the ramp that engines are about to start. They start during the push.

*Taxiing out,* taxi light. *Lining up,* strobes and landing lights.
*Airborne,* the taxi light goes off and the landing lights stay on below
ten thousand feet.

*Arriving,* the same in reverse: landing lights below ten thousand, taxi
light from final. Off the runway the strobes and landing lights go off and
the taxi light stays. On the stand the taxi light goes off, the engines run
for a minute or two more (cooling, the APU coming on), and only then shut
down; the beacon goes off with them.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

#: Below this, the landing lights are on, airborne.
LANDING_LIGHTS_BELOW_FT = 10000.0
#: How long the engines run after block-in, before they are shut down: a
#: minute to two, the same for the same aeroplane every time.
SHUTDOWN_AFTER_S = (60.0, 120.0)

_ON_STAND = frozenset({"static", "booked", "boarding"})
_TAXI_OUT = frozenset({"taxi", "holding", "ready", "waiting", "clearance"})
_AIRBORNE_OUT = frozenset({"takeoff", "departed"})
_AIRBORNE_IN = frozenset({"inbound", "joining", "go_around", "downwind",
                          "base", "crosswind", "upwind"})


@dataclass(frozen=True)
class Systems:
    """Engines and lights, as wanted or as read."""

    engines: bool = False
    nav: bool = False
    beacon: bool = False
    strobe: bool = False
    taxi: bool = False
    landing: bool = False

    def differences(self, other: "Systems") -> list[str]:
        return [name for name in FIELDS
                if getattr(self, name) != getattr(other, name)]


FIELDS = ("engines", "nav", "beacon", "strobe", "taxi", "landing")


def shutdown_delay(callsign: str) -> float:
    """How long this aeroplane's engines run after block-in."""
    low, high = SHUTDOWN_AFTER_S
    share = (zlib.crc32(callsign.encode("utf-8")) % 1000) / 1000.0
    return low + (high - low) * share


def wanted(kind: str, phase: str, now: float, *, parked_at: float = 0.0,
           pushing: bool = False, doors_shut: bool = True,
           altitude_agl_ft: float = 0.0, on_ground: bool = True,
           callsign: str = "") -> Systems:
    """The engines and lights an aeroplane should have now.

    ``kind`` is :meth:`TrafficInjector.apron_state`'s word for where it is
    ("static", "booked", "boarding", "departing", "arrived", "arriving",
    "moving"), ``phase`` its flight phase. ``doors_shut`` is the door
    layer's answer, which is what the beacon waits for.
    """
    if kind in _ON_STAND:
        return Systems(nav=True)
    if kind == "departing" and not pushing:
        # Boarded, on the stand: the beacon once the doors read shut.
        return Systems(nav=True, beacon=doors_shut)
    if pushing:
        return Systems(engines=True, nav=True, beacon=True)
    if kind == "arrived":
        if parked_at and now - parked_at < shutdown_delay(callsign):
            return Systems(engines=True, nav=True, beacon=True)
        return Systems(nav=True)
    low = altitude_agl_ft < LANDING_LIGHTS_BELOW_FT
    if phase in _TAXI_OUT:
        return Systems(engines=True, nav=True, beacon=True, taxi=True)
    if phase == "lineup":
        return Systems(engines=True, nav=True, beacon=True, strobe=True,
                       taxi=True, landing=True)
    if phase in _AIRBORNE_OUT:
        airborne = not on_ground
        return Systems(engines=True, nav=True, beacon=True, strobe=True,
                       taxi=not airborne, landing=low)
    if phase in ("final", "landed"):
        return Systems(engines=True, nav=True, beacon=True, strobe=True,
                       taxi=True, landing=True)
    if phase == "taxi_in":
        return Systems(engines=True, nav=True, beacon=True, taxi=True)
    if phase in _AIRBORNE_IN or not on_ground:
        return Systems(engines=True, nav=True, beacon=True, strobe=True,
                       landing=low)
    # On the ground, moving, and none of the above: running, and seen.
    return Systems(engines=True, nav=True, beacon=True)


__all__ = ["Systems", "FIELDS", "wanted", "shutdown_delay",
           "LANDING_LIGHTS_BELOW_FT", "SHUTDOWN_AFTER_S"]
