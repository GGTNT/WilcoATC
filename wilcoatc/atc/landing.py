# -*- coding: utf-8 -*-
"""How the landing went, and whether the flight kept its schedule.

The one number every simulator pilot wants after a touchdown is the rate they
hit the runway at, and the simulator does not hand it over: ``VERTICAL SPEED``
is a live reading, and a poll a few times a second finds the aeroplane either
still in the air or already rolling out with the needle back at zero. So the
rate is taken from the two readings that straddle the contact. The one on the
ground is the nearer to the moment, and it still carries the descent on the
tick the wheels touch, so it is believed when it says the aeroplane was going
down; when it has already settled to zero or above, the last reading in the
air is used instead. That makes the figure a slightly pessimistic one -- the
air reading can be from before the last of the flare -- which is the right
way round for a number people compare.

Why not the simulator's own touchdown variable
----------------------------------------------

``PLANE TOUCHDOWN NORMAL VELOCITY`` exists, but not in every wrapper's
request table, and the source asks for every variable with a blocking round
trip. A variable the wrapper does not know costs nothing and returns nothing,
which would leave the report with no figure on exactly the installations
where nobody can see why. The two readings are always there.

What the score is made of
-------------------------

A landing is judged by more than one figure, so the score is three things a
check captain would look at, and it says what each was:

* the rate itself -- anything up to 200 fpm is as good as it gets, and the
  score falls to almost nothing by 600, which is around where an airliner's
  hard-landing inspection starts;
* whether it stayed down -- every bounce costs;
* the last thousand feet -- how much the vertical speed and the airspeed
  wandered with the gear down. A butter landing at the end of a porpoising
  approach is luck, not flying.

The schedule
------------

SimBrief writes the planned block and wheel times into the plan, as UTC. The
flight is timed against the wall clock rather than the simulator's, because a
plan is a schedule for the day it was built for and nearly everybody flies it
in real time; the time in the air is compared as well, and that comparison
holds whatever the clock says. The on-time line is the airline one: within
fifteen minutes of the schedule is on time.

Nothing here speaks. The applause belongs to the cabin
(:mod:`wilcoatc.atc.crew`) and reads :func:`touchdown_rate` from here, so the
number the pilot is shown and the number the passengers clapped for are the
same number.
"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import asdict, dataclass, field

# Below this, the cabin claps. The pilot's own rule: a touchdown gentler than
# two hundred feet a minute.
APPLAUSE_FPM = -200.0

# How long after the wheels touch the report is written. Long enough to see a
# bounce come and go, which is the one thing about a landing that is not known
# at the moment of contact.
REPORT_DELAY_S = 6.0

# A bounce is the aeroplane leaving the ground again, soon, and properly --
# not a strut flickering the on-ground flag for one reading.
BOUNCE_WINDOW_S = 8.0
BOUNCE_AGL_FT = 3.0

# How long it has to have been flying for a return to the ground to count as
# a landing rather than a hop on the runway.
AIRBORNE_MIN_S = 20.0

# The window the approach is judged over: the last thousand feet, gear down,
# and above the flare, which is where the vertical speed is supposed to change.
APPROACH_TOP_FT = 1000.0
APPROACH_BOTTOM_FT = 50.0

# The airline definition of on time, in minutes either side.
ON_TIME_MIN = 15.0
EARLY_MIN = -5.0

# How long a report is worth putting in front of somebody who has just opened
# the panel. After this it is history, not news.
FRESH_S = 300.0

# The grades, gentlest first, with the rate each one ends at.
GRADES: tuple[tuple[str, float], ...] = (
    ("butter", -60.0),
    ("smooth", -200.0),
    ("normal", -350.0),
    ("firm", -600.0),
    ("hard", -math.inf),
)


def touchdown_rate(previous, state) -> float:
    """The vertical speed the wheels met the runway at, in feet per minute.

    Negative for a descent, as the simulator reports it. See the module
    docstring for why it is these two readings and in this order.
    """
    on_the_ground = float(getattr(state, "vertical_speed_fpm", 0.0) or 0.0)
    if on_the_ground < 0.0:
        return on_the_ground
    return min(0.0, float(getattr(previous, "vertical_speed_fpm", 0.0) or 0.0))


def grade(fpm: float) -> str:
    """The word for a touchdown rate."""
    for name, floor in GRADES:
        if fpm >= floor:
            return name
    return GRADES[-1][0]


def punctuality(delay_min: float | None) -> str:
    """Early, on time or late, by the airline rule; "" with no schedule."""
    if delay_min is None:
        return ""
    if delay_min < EARLY_MIN:
        return "early"
    if delay_min <= ON_TIME_MIN:
        return "on_time"
    return "late"


def wind_components(state) -> tuple[float, float]:
    """(headwind, crosswind) in knots, headwind negative for a tailwind.

    The ambient wind is where it is coming *from*, in degrees true, which is
    what the heading is measured against too.
    """
    speed = float(getattr(state, "ambient_wind_velocity", 0.0) or 0.0)
    if speed <= 0.0:
        return 0.0, 0.0
    angle = math.radians(float(state.ambient_wind_direction)
                         - float(state.heading_true))
    return speed * math.cos(angle), abs(speed * math.sin(angle))


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def score(fpm: float, bounces: int, vs_spread: float | None,
          ias_spread: float | None) -> int:
    """Out of a hundred. Weights are in the module docstring."""
    rate = abs(fpm)
    if rate <= 200.0:
        points = 50.0
    elif rate <= 600.0:
        points = 50.0 - 40.0 * (rate - 200.0) / 400.0
    else:
        points = max(0.0, 10.0 - (rate - 600.0) / 40.0)
    points += 20.0 * _clamp(1.0 - bounces / 2.0)
    # An approach nobody saw -- a circuit flown below the window, a session
    # started on short final -- is not held against the pilot.
    if vs_spread is None:
        points += 20.0
    else:
        points += 20.0 * _clamp(1.0 - (vs_spread - 100.0) / 400.0)
    if ias_spread is None:
        points += 10.0
    else:
        points += 10.0 * _clamp(1.0 - (ias_spread - 2.0) / 8.0)
    return int(round(points))


@dataclass
class LandingReport:
    """One touchdown, measured."""

    id: int
    touchdown_at: float                 # UTC epoch seconds
    fpm: float
    grade: str
    score: int
    bounces: int = 0
    ias_kt: float = 0.0
    ground_speed_kt: float = 0.0
    headwind_kt: float = 0.0
    crosswind_kt: float = 0.0
    # How far the vertical speed and airspeed strayed over the last thousand
    # feet, as standard deviations; None when there was nothing to measure.
    vs_spread: float | None = None
    ias_spread: float | None = None
    airport: str = ""
    # The flight around it.
    off_block_at: float = 0.0
    takeoff_at: float = 0.0
    sched_out: int = 0
    sched_on: int = 0
    planned_air_s: int = 0

    @property
    def stable(self) -> bool | None:
        if self.vs_spread is None:
            return None
        return self.vs_spread <= 250.0 and (self.ias_spread or 0.0) <= 5.0

    @property
    def departure_delay_min(self) -> float | None:
        if not (self.sched_out and self.off_block_at):
            return None
        return (self.off_block_at - self.sched_out) / 60.0

    @property
    def arrival_delay_min(self) -> float | None:
        if not self.sched_on:
            return None
        return (self.touchdown_at - self.sched_on) / 60.0

    @property
    def air_s(self) -> float:
        return self.touchdown_at - self.takeoff_at if self.takeoff_at else 0.0

    def view(self, now: float | None = None) -> dict:
        """Everything the panel shows, rounded the way it shows it."""
        now = time.time() if now is None else now
        body = asdict(self)
        body.update(
            fpm=int(round(self.fpm)),
            ias_kt=int(round(self.ias_kt)),
            ground_speed_kt=int(round(self.ground_speed_kt)),
            headwind_kt=int(round(self.headwind_kt)),
            crosswind_kt=int(round(self.crosswind_kt)),
            vs_spread=(None if self.vs_spread is None
                       else int(round(self.vs_spread))),
            ias_spread=(None if self.ias_spread is None
                        else round(self.ias_spread, 1)),
            stable=self.stable,
            departure_delay_min=_minutes(self.departure_delay_min),
            departure=punctuality(self.departure_delay_min),
            arrival_delay_min=_minutes(self.arrival_delay_min),
            arrival=punctuality(self.arrival_delay_min),
            air_s=int(round(self.air_s)),
            fresh=(now - self.touchdown_at) < FRESH_S,
        )
        return body

    def describe(self) -> str:
        """One line, for the console and the log."""
        bits = [f"{int(round(self.fpm))} fpm", self.grade,
                f"score {self.score}"]
        if self.bounces:
            bits.append(f"{self.bounces} bounce{'s' if self.bounces > 1 else ''}")
        delay = self.arrival_delay_min
        if delay is not None:
            bits.append(f"{punctuality(delay).replace('_', ' ')} "
                        f"({delay:+.0f} min)")
        return ", ".join(bits)


def _minutes(value: float | None) -> int | None:
    return None if value is None else int(round(value))


@dataclass
class _Touchdown:
    """A landing that has happened and has not been written up yet."""

    at: float
    monotonic: float
    fpm: float
    ias_kt: float
    ground_speed_kt: float
    headwind_kt: float
    crosswind_kt: float
    bounces: int = 0
    # Whether the aeroplane is off the ground again, and since when, so a
    # bounce is counted once and a go-around is not counted at all.
    up_since: float = 0.0


@dataclass
class LandingMonitor:
    """Watches a flight from the stand to the runway at the other end.

    Fed the same pair of readings every tick that the controller and the crew
    are fed. It writes nothing to the radio and changes nothing; what it
    produces is :attr:`report`, once per landing.
    """

    # What the schedule is, if anybody has said: the SimBrief plan, or a
    # callable returning it, so a plan loaded after the aeroplane has pushed
    # back still counts.
    plan: object = None

    report: LandingReport | None = None
    off_block_at: float = 0.0
    takeoff_at: float = 0.0
    airborne_since: float = 0.0
    _pending: _Touchdown | None = None
    _vs: list[float] = field(default_factory=list)
    _ias: list[float] = field(default_factory=list)
    _count: int = 0
    # Whether the aeroplane has been seen shut down on the ground since the
    # last landing, which is what makes the next movement a new flight.
    _arrived: bool = False

    def follow(self, previous, state, airport: str = "",
               now: float | None = None, clock: float | None = None) -> bool:
        """One tick. Returns True when a report has just been written.

        ``now`` is the wall clock and ``clock`` the monotonic one; both are
        parameters so a test can fly a flight in no time at all.
        """
        now = time.time() if now is None else now
        clock = time.monotonic() if clock is None else clock
        if previous is None or getattr(state, "paused", False):
            return False

        if state.on_ground:
            self._on_the_ground(previous, state, now)
        else:
            self._in_the_air(previous, state, now, clock)

        if state.on_ground and not previous.on_ground:
            self._touched(previous, state, now, clock)
        return self._write_up(state, airport, clock)

    # -- before the landing ----------------------------------------------

    def _on_the_ground(self, previous, state, now: float) -> None:
        if self._arrived and state.ground_speed_kt > 2.0:
            # Shut down at the gate and now moving again: a new flight.
            self._arrived = False
            self.off_block_at = self.takeoff_at = self.airborne_since = 0.0
            self._vs.clear()
            self._ias.clear()
        if self.report is not None and not state.engines_running:
            self._arrived = True
        # Off the blocks is when it first moves. Until the first takeoff
        # only: rolling out after the landing is not a departure.
        if (not self.takeoff_at and not self.off_block_at
                and state.ground_speed_kt > 2.0):
            self.off_block_at = now

    def _in_the_air(self, previous, state, now: float, clock: float) -> None:
        if previous.on_ground:
            if not self.takeoff_at:
                self.takeoff_at = now
            if self._pending is None:
                self.airborne_since = clock
                self._vs.clear()
                self._ias.clear()
        height = state.altitude_agl_ft
        if (state.gear_down and APPROACH_BOTTOM_FT <= height <= APPROACH_TOP_FT
                and state.vertical_speed_fpm < 0):
            self._vs.append(float(state.vertical_speed_fpm))
            self._ias.append(float(state.indicated_airspeed_kt))

    # -- the landing ------------------------------------------------------

    def _touched(self, previous, state, now: float, clock: float) -> None:
        pending = self._pending
        if pending is not None:
            # Back on the ground from a bounce: the first contact is the
            # landing, and this one is what it cost.
            if pending.up_since:
                pending.bounces += 1
                pending.up_since = 0.0
            return
        if not self.airborne_since or (clock - self.airborne_since
                                       < AIRBORNE_MIN_S):
            return
        headwind, crosswind = wind_components(state)
        self._pending = _Touchdown(
            at=now, monotonic=clock,
            fpm=touchdown_rate(previous, state),
            ias_kt=previous.indicated_airspeed_kt or state.indicated_airspeed_kt,
            ground_speed_kt=(previous.ground_speed_kt
                             or state.ground_speed_kt),
            headwind_kt=headwind, crosswind_kt=crosswind,
        )

    def _write_up(self, state, airport: str, clock: float) -> bool:
        pending = self._pending
        if pending is None:
            return False
        if (not state.on_ground and not pending.up_since
                and state.altitude_agl_ft > BOUNCE_AGL_FT
                and clock - pending.monotonic <= BOUNCE_WINDOW_S):
            pending.up_since = clock
        if clock - pending.monotonic < REPORT_DELAY_S:
            return False

        self._pending = None
        # Still in the air after the wait is a touch-and-go or a go-around,
        # and the touchdown it began with is still a touchdown.
        self._count += 1
        plan = self.plan() if callable(self.plan) else self.plan
        self.report = LandingReport(
            id=self._count,
            touchdown_at=pending.at,
            fpm=pending.fpm,
            grade=grade(pending.fpm),
            score=score(pending.fpm, pending.bounces, _spread(self._vs),
                        _spread(self._ias)),
            bounces=pending.bounces,
            ias_kt=pending.ias_kt,
            ground_speed_kt=pending.ground_speed_kt,
            headwind_kt=pending.headwind_kt,
            crosswind_kt=pending.crosswind_kt,
            vs_spread=_spread(self._vs),
            ias_spread=_spread(self._ias),
            airport=airport,
            off_block_at=self.off_block_at,
            takeoff_at=self.takeoff_at,
            sched_out=int(getattr(plan, "sched_out", 0) or 0),
            sched_on=int(getattr(plan, "sched_on", 0) or 0),
            planned_air_s=int(getattr(plan, "planned_air_s", 0) or 0),
        )
        if not state.on_ground:
            # Going round: the next approach is judged on its own.
            self.airborne_since = clock
            self._vs.clear()
            self._ias.clear()
        return True


def _spread(values: list[float]) -> float | None:
    """Standard deviation, or None with too few readings to mean anything."""
    if len(values) < 4:
        return None
    return statistics.pstdev(values)


__all__ = ["APPLAUSE_FPM", "LandingMonitor", "LandingReport", "grade",
           "punctuality", "score", "touchdown_rate", "wind_components"]
