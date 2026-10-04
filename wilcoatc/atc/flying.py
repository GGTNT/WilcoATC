"""What an aeroplane does between transmissions.

The traffic on the frequency used to be a timetable. A flight sat in a phase
until a timer fired, and where it was drawn came from which phase that was, so
an aeroplane cleared for takeoff appeared four hundred yards down the runway
because the phase had changed, not because it had rolled.

This is the other half: the instruction arrives, the crew reads it back, and
from that moment the aeroplane is *flying it*. A go-around is not a phase, it
is a nose coming up and a rate of climb; "turn left heading two seven zero" is
three degrees a second for the fifty seconds it takes. What the map draws and
what the simulator injects is the result of that, not a lookup.

Three ideas, and nothing else in here:

*A target.* Where the aeroplane is trying to get to -- a heading, an altitude,
a speed. Empty fields mean "carry on as you are", which is what an aeroplane
does when nobody has said anything.

*A performance.* How fast it can get there. A Cessna turns twice as fast as an
A320 and climbs at a third the rate, and both of those are visible on the map
within ten seconds of an instruction, so they are worth having right.

*A step.* One slice of time. Turn a little, climb a little, accelerate a
little, then move along the heading at whatever speed that left. Integrating
rather than interpolating is what lets an aeroplane be interrupted halfway
through a turn and do something else, which is the whole point.

Nothing here knows about controllers, phraseology or the radio. It is given a
target and a number of seconds, and it moves an aeroplane.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..navdata.build import project

# A standard rate turn: three degrees a second, two minutes for a circle. What
# every airliner flies below 250 knots and what every published procedure
# assumes.
RATE_ONE = 3.0

# Light aircraft turn faster and are allowed to: rate one at 90 knots is about
# fifteen degrees of bank, and a Cessna in the circuit is doing more than that.
RATE_LIGHT = 5.0

# Below this the aeroplane is stopped rather than creeping, which stops a
# holding point from drifting over ten minutes of rounding error.
STOPPED_KT = 0.4


@dataclass(frozen=True)
class Performance:
    """Roughly what a class of aeroplane can do.

    Rounded numbers, not a flight model. The point is that a Cessna and an
    A320 do not do the same thing when told the same thing -- one rotates at
    sixty knots and climbs at seven hundred feet a minute, the other at a
    hundred and forty and two and a half thousand -- and that both of them
    take a plausible time about it.
    """

    turn_rate: float = RATE_ONE          # degrees per second
    climb_fpm: float = 2200.0
    # Enough to hold a three-degree path at the speed this type flies the
    # arrival at, with room to spare. An aeroplane that cannot come down as
    # fast as the glide path descends floats above it for the whole approach.
    descend_fpm: float = 2000.0
    accelerate_kt_s: float = 3.0         # knots per second
    decelerate_kt_s: float = 2.0
    # The speeds each phase of flight is flown at.
    taxi_kt: float = 14.0
    rotate_kt: float = 145.0
    climb_kt: float = 220.0
    cruise_kt: float = 280.0
    approach_kt: float = 140.0
    # How hard it stops once it is down.
    braking_kt_s: float = 4.5
    # What it climbs to before anybody has said otherwise.
    pattern_agl_ft: float = 1500.0


AIRLINER = Performance()

TURBOPROP = Performance(
    turn_rate=RATE_ONE, climb_fpm=1500.0, descend_fpm=1600.0,
    accelerate_kt_s=2.6, decelerate_kt_s=1.8,
    rotate_kt=105.0, climb_kt=170.0, cruise_kt=230.0, approach_kt=115.0,
    braking_kt_s=4.0, pattern_agl_ft=1500.0)

LIGHT = Performance(
    turn_rate=RATE_LIGHT, climb_fpm=750.0, descend_fpm=900.0,
    accelerate_kt_s=2.2, decelerate_kt_s=2.2,
    taxi_kt=10.0, rotate_kt=60.0, climb_kt=85.0, cruise_kt=110.0,
    approach_kt=70.0, braking_kt_s=3.5, pattern_agl_ft=1000.0)

# Which types fly like what. Anything unlisted is judged by the company it
# keeps: a flight with an operator is an airliner, a registration is not.
_LIGHT_TYPES = frozenset({
    "C152", "C172", "C182", "PA28", "P28A", "PA34", "DA40", "DA42", "SR22",
    "BE36", "TB20", "AT3", "R44", "C25A",
})
_TURBOPROP_TYPES = frozenset({"AT76", "DH8D", "TBM9", "PC12"})


def performance_for(type_code: str, light: bool = False) -> Performance:
    """How this type flies."""
    code = (type_code or "").upper()
    if code in _LIGHT_TYPES:
        return LIGHT
    if code in _TURBOPROP_TYPES:
        return TURBOPROP
    if not code and light:
        return LIGHT
    return LIGHT if light and code not in _TURBOPROP_TYPES else AIRLINER


@dataclass
class Target:
    """What the aeroplane is currently trying to do.

    ``None`` everywhere means nobody has said anything and it carries on as it
    is, which is exactly what an aeroplane does. A field that is set is a thing
    somebody asked for, and the aeroplane works towards it at the rate its
    performance allows.
    """

    heading: float | None = None
    altitude_ft: float | None = None
    speed_kt: float | None = None
    # Which way round to a commanded heading. Empty turns the short way, which
    # is what an aeroplane does when the direction is not specified.
    turn: str = ""
    # Held still on the ground: a holding point, a lined-up aeroplane waiting
    # for its clearance, a stand.
    hold: bool = False
    # What this is, in one word, for the strip and the map. Not used by the
    # model; it is what the pilot sees when they ask what that aeroplane is
    # doing.
    intent: str = ""

    def clear(self) -> None:
        self.heading = self.altitude_ft = self.speed_kt = None
        self.turn = ""
        self.hold = False


# Every kind of instruction an aeroplane can be given. The window needs a word
# for each of them in each language it is read in, so the list is here rather
# than implied by whatever the traffic happens to have said this session.
ORDERS: tuple[str, ...] = (
    "clearance", "taxi", "hold_short", "line_up", "takeoff", "land",
    "touch_and_go", "go_around", "extend", "continue", "pattern", "handoff",
    "taxi_in", "turn_out", "heading", "altitude", "speed", "resume",
)


@dataclass
class Command:
    """One thing a controller told this aeroplane, and what became of it.

    It is recorded when the instruction goes out and applied when the readback
    finishes, which is why an aeroplane starts its turn a beat after you hear
    it acknowledge one rather than on the controller's last syllable.
    """

    kind: str                       # "takeoff", "go_around", "heading", ...
    at: float = 0.0                 # when it takes effect, monotonic seconds
    text: str = ""                  # what was said, for the strip
    values: dict = field(default_factory=dict)
    applied: bool = False

    @property
    def detail(self) -> str:
        """The number in the instruction, where there is one.

        A heading, a level or a speed is the same three digits in every
        language, so it travels as digits and the window puts its own word in
        front of them. Everything else is the kind alone.
        """
        values = self.values
        if self.kind == "heading":
            return f"{int(values.get('heading', 0)) % 360:03d}"
        if self.kind == "altitude":
            return f"{int(values.get('altitude_ft', 0)):,}"
        if self.kind == "speed":
            return f"{int(values.get('speed_kt', 0))}"
        return ""

    def as_dict(self) -> dict:
        """What the window is told about this instruction.

        The kind rather than the words for it. The window is read in
        forty-one languages and the aeroplanes on the map are named in all of
        them; an English "go around" underneath a translated everything else
        would be the one thing on the page that had not been thought about.
        """
        return {"kind": self.kind, "detail": self.detail}


# ----------------------------------------------------------------------
# moving an aeroplane
# ----------------------------------------------------------------------


def turn_towards(heading: float, wanted: float, rate: float, dt: float,
                 direction: str = "") -> float:
    """Swing a heading towards another one, no faster than ``rate``.

    ``direction`` forces the long way round where a controller asked for it:
    "turn right heading three six zero" from 010 is 350 degrees of turn and
    the aeroplane flies every one of them.
    """
    change = (wanted - heading + 540.0) % 360.0 - 180.0
    if direction == "left" and change > 0:
        change -= 360.0
    elif direction == "right" and change < 0:
        change += 360.0
    limit = rate * dt
    if abs(change) > limit:
        change = limit if change > 0 else -limit
    return (heading + change) % 360.0


def _towards(value: float, wanted: float, up: float, down: float,
             dt: float) -> float:
    """Move a number towards another at one rate going up and another down."""
    if wanted > value:
        return min(wanted, value + up * dt)
    return max(wanted, value - down * dt)


def step(flight, target: Target, performance: Performance, dt: float,
         ground_elevation_ft: float = 0.0) -> None:
    """Fly one slice of time.

    ``flight`` is anything carrying the six numbers an aeroplane is made of --
    latitude, longitude, altitude_ft, heading, ground_speed_kt, on_ground --
    which is what a :class:`~wilcoatc.atc.traffic.Flight` carries. It is moved
    in place.
    """
    if dt <= 0.0:
        return

    if target.hold:
        # Told to stop, and stopping takes a moment: an aeroplane rolling to
        # a holding point does not become stationary the instant the word is
        # said.
        flight.ground_speed_kt = max(
            0.0, flight.ground_speed_kt - performance.braking_kt_s * dt)
        if flight.ground_speed_kt <= STOPPED_KT:
            flight.ground_speed_kt = 0.0
            flight.vertical_rate_fpm = 0.0
            return
    elif target.speed_kt is not None:
        flight.ground_speed_kt = _towards(
            flight.ground_speed_kt, max(0.0, target.speed_kt),
            performance.accelerate_kt_s,
            performance.braking_kt_s if flight.on_ground
            else performance.decelerate_kt_s,
            dt)

    if target.heading is not None:
        # On the ground below taxi speed an aeroplane is steered, not turned,
        # and it can pivot faster than it can bank.
        rate = (performance.turn_rate * 3.0
                if flight.on_ground and flight.ground_speed_kt < 40.0
                else performance.turn_rate)
        flight.heading = turn_towards(flight.heading, target.heading, rate, dt,
                                      target.turn)

    if target.altitude_ft is not None:
        before = flight.altitude_ft
        wanted = max(ground_elevation_ft, target.altitude_ft)
        flight.altitude_ft = _towards(
            flight.altitude_ft, wanted,
            performance.climb_fpm / 60.0, performance.descend_fpm / 60.0, dt)
        flight.vertical_rate_fpm = (flight.altitude_ft - before) / dt * 60.0
    else:
        flight.vertical_rate_fpm = 0.0

    if flight.ground_speed_kt > 0.0:
        distance = flight.ground_speed_kt * (dt / 3600.0)
        flight.latitude, flight.longitude = project(
            flight.latitude, flight.longitude, flight.heading, distance)


def glide_altitude(elevation_ft: float, distance_nm: float,
                   ft_per_nm: float = 318.0) -> float:
    """Where a three-degree path is, that far from the threshold."""
    return elevation_ft + max(0.0, distance_nm) * ft_per_nm


def offset_from_track(lat: float, lon: float, from_lat: float, from_lon: float,
                      track: float) -> tuple[float, float]:
    """How far along a track a point is, and how far off to the side of it.

    Both in nautical miles, the sideways one positive to the right. This is
    what says whether an aeroplane is on the runway rather than merely near
    the airport, and what an interception has left to close.
    """
    from ..navdata.db import bearing_deg, haversine_nm

    distance = haversine_nm(from_lat, from_lon, lat, lon)
    if distance <= 0.0:
        return 0.0, 0.0
    relative = math.radians(bearing_deg(from_lat, from_lon, lat, lon) - track)
    return distance * math.cos(relative), distance * math.sin(relative)


__all__ = [
    "Performance", "Target", "Command", "ORDERS",
    "AIRLINER", "TURBOPROP", "LIGHT",
    "performance_for", "step", "turn_towards", "glide_altitude",
    "offset_from_track", "RATE_ONE", "RATE_LIGHT",
]
