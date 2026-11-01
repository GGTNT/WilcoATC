"""Passengers between an aeroplane and the bus that takes them to the terminal.

The ramp (:mod:`wilcoatc.atc.handling`) put stairs at the door of every
parked aeroplane and nobody ever came down them. The simulator has no person
that walks when told to -- its own humans slide along their waypoints in
2024 -- and GSX's, which do walk, are driven by GSX alone. Measured on the
way here (Madeira, 14 October): a GSX passenger's whole animation is one
clip played from a variable this program can write, its altitude can be
frozen and written, and a GSX bus can be driven along the airport's service
roads exactly enough to stop beside the stairs (see
:mod:`wilcoatc.integrations.motion` and :func:`wilcoatc.atc.paths.drive_route`).

So this plans one bus run at an aeroplane's forward stairs: the bus along
the roads from three hundred metres out, round to the stairs from ahead of
the nose (the last stretch is straight, and from a road behind the
aeroplane a straight line goes under its wing), parked nose to tail with
its left-hand doors to the stairs; its doors opening; the passengers one
by one between the door and the bus -- down the stairs and in, or out and
up -- while the seats behind the windows fill or empty; the doors shutting;
and the bus turning away from the aeroplane, never towards it, and back the
way it came. Everything is planned once, on one clock, as drives and
keyframes; the vehicle layer only plays it. It knows nothing of SimConnect.

A dozen people stand for a hundred and fifty. Every one of them is an
object in the simulator, and an apron with a column of a hundred and fifty
figures walking at one aeroplane is a cost nobody would pay for a sight
that a dozen makes as well.

Only a narrowbody: the bus parks sixteen metres out from the centreline,
inside a widebody's wing.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from . import paths

BUS = "bus"
PASSENGER = "passenger"

DEBOARD = "deboard"
BOARD = "board"

# -- the stairs ------------------------------------------------------------
# In the stairs' own frame, metres along the way they face (towards the
# fuselage): the top platform, the bottom step, and the doorway beyond the
# platform where a passenger appears or is lost from sight.
STAIRS_TOP_M = 2.0
STAIRS_FOOT_M = -5.5
DOOR_IN_M = 1.6
# Standing on the bottom step is not the ground.
FOOT_LIFT_M = 0.2

# -- the bus (GSX's Cobus 3000, read off its model) ---------------------------
# The left-hand doors, metres ahead of its reference point, and how far out
# to the side they are; a passenger stands a little outside one to get on.
BUS_DOORS_FORWARD = (1.9, -1.3, -5.1)
BUS_DOOR_SIDE_M = 1.37
STEP_OUT_M = 0.9
STEP_IN_M = 0.6
#: Every seat a passenger can be seen in, behind the windows.
SEATS = 44
#: How far beyond the stairs' foot the bus's centreline is.
BUS_OUT_M = 6.0
#: How far ahead of its spot the bus lines up before it pulls in.
LINE_UP_M = 22.0
#: Where it comes from, along the roads.
ROAD_FROM_M = 300.0
BUS_MS = 6.0
#: The doors' travel, either way.
DOORS_S = 2.5
#: The largest aeroplane a bus parks beside (see the module docstring).
LONGEST_M = 45.0
#: The bus's body about its reference, for keeping it off the aeroplanes:
#: half its length and half its width, metres. And how much room it leaves.
BUS_HALF_LENGTH_M = 7.0
BUS_HALF_WIDTH_M = 1.5
CLEARANCE_M = 1.5
#: The ways out it may take, tightest first: in the aeroplane's frame,
#: (outward of its spot, forward of its spot) for each point of the turn.
#: The first plan swung sixteen metres out, which at Madeira is where the
#: next stand's wingtip is.
WAYS_OUT: tuple[tuple[tuple[float, float], ...], ...] = (
    ((3.0, -4.0), (9.0, -4.0), (12.0, 2.0), (12.0, LINE_UP_M)),
    ((4.0, -6.0), (12.0, -6.0), (16.0, 2.0), (16.0, LINE_UP_M)),
)

# -- the people ------------------------------------------------------------
WALK_MS = 1.3
STAIRS_MS = 0.8
#: One passenger after another, this far apart, give or take a little.
EVERY_S = 3.0
#: Made this long before they are first seen to move, so the simulator has
#: answered and their ground has been read before they climb.
SPAWN_LEAD_S = 2.0
#: Taken away this long after they are out of sight.
GONE_AFTER_S = 0.5

_PEOPLE_LIMITS = dict(turn_rad_s=3.0, accel=1.5, crawl=0.5, corner_m=1.0)


@dataclass
class Walker:
    """One passenger on the way between the door and the bus."""

    key: str
    appears: float
    path: list
    # (from, to, speed) while the legs move; standing still between.
    walk: list
    # (time, metres above the ground): the stairs.
    lift: list
    gone: float


@dataclass
class Run:
    """One bus at one aeroplane, and the people it brings or takes."""

    callsign: str
    kind: str                      # DEBOARD | BOARD
    planned: float
    bus_path: list
    arrives: float
    leaves: float
    gone: float
    doors: list                    # (time, 0..100) of the left-hand doors
    seats: list                    # (time, seats filled)
    walkers: list[Walker] = field(default_factory=list)

    @property
    def people_done(self) -> float:
        """When the last of them is off the stairs: the stairs wait for it."""
        return max((w.gone for w in self.walkers), default=self.arrives)

    def finished(self, now: float) -> bool:
        return now >= self.gone

    def bus_out(self, now: float) -> bool:
        return self.planned <= now < self.gone


def _frame(x: float, z: float, heading: float, right: float,
           forward: float) -> tuple[float, float]:
    """A point ``right`` of and ``forward`` of (x, z) facing ``heading``."""
    h = math.radians(heading)
    return (x + right * math.cos(h) + forward * math.sin(h),
            z - right * math.sin(h) + forward * math.cos(h))


def _local(x: float, z: float, heading: float, px: float,
           pz: float) -> tuple[float, float]:
    """(right, forward) of a point in a frame at (x, z) facing ``heading``."""
    h = math.radians(heading)
    dx, dz = px - x, pz - z
    return (dx * math.cos(h) - dz * math.sin(h),
            dx * math.sin(h) + dz * math.cos(h))


def _join(*legs: list) -> list:
    return [segment for leg in legs for segment in leg]


def _inside(footprint: tuple[float, float, float, float],
            x: float, z: float) -> bool:
    """Whether a point is on an aeroplane: its fuselage, its wings, its
    tailplane, with CLEARANCE_M round each. ``footprint`` is (x, z, heading,
    fuselage length); the reference point is four tenths of the way back,
    and a span is near enough the length on everything this flies. A wing
    drawn as a rectangle from root to tip put a bus parked clear of an
    A320's leading edge inside it."""
    ax, az, heading, length = footprint
    right, forward = _local(ax, az, heading, x, z)
    right = abs(right) - CLEARANCE_M
    span = max(10.0, 0.95 * length) / 2.0
    if right < 2.5 and -0.6 * length - CLEARANCE_M < forward < 0.4 * length + CLEARANCE_M:
        return True
    if right < span:
        # Swept: the leading edge is a fifth of the length further aft at
        # the tip than at the root (an A320's, about eight metres).
        out = max(0.0, right) / span
        leading = (0.05 - 0.25 * out) * length + CLEARANCE_M
        trailing = (-0.30 - 0.03 * out) * length - CLEARANCE_M
        if trailing < forward < leading:
            return True
    return right < 0.18 * length and -0.6 * length - CLEARANCE_M < forward < -0.45 * length


def clear(drive: list, footprints: list) -> bool:
    """Whether a bus on this drive keeps its whole body off every aeroplane,
    looked at every half second."""
    if not drive or not footprints:
        return True
    t, end = drive[0].t0, paths.ends_at(drive)
    while True:
        x, z, heading, _ = paths.evaluate(drive, t)
        for forward in (-BUS_HALF_LENGTH_M, 0.0, BUS_HALF_LENGTH_M):
            for right in (-BUS_HALF_WIDTH_M, BUS_HALF_WIDTH_M):
                px, pz = _frame(x, z, heading, right, forward)
                if any(_inside(f, px, pz) for f in footprints):
                    return False
        if t >= end:
            return True
        t = min(end, t + 0.5)


class Planner:
    """Plans runs. ``visible`` is how many passengers each run shows."""

    def __init__(self, visible: int = 12, seed: int | None = None):
        self.visible = max(0, int(visible))
        self._seed = seed
        # What the last plan came to, for the log: why there is no bus is
        # the one question the apron cannot answer by looking at it.
        self.why = ""

    def fits(self, length: float, light: bool) -> bool:
        return not light and length <= LONGEST_M and self.visible > 0

    def plan(self, kind: str, callsign: str, ac_x: float, ac_z: float,
             ac_heading: float, length: float, sill: float,
             stairs: tuple[float, float, float], layout,
             now: float, footprints: list | None = None) -> Run | None:
        """A run at the stairs standing at ``stairs`` (x, z, the way they
        face), or None where the roads give no way in, or no way in or out
        that keeps clear of every aeroplane in ``footprints`` (x, z,
        heading, fuselage length -- this one and every other)."""
        rng = random.Random(f"{self._seed}:{callsign}:{kind}")
        sx, sz, facing = stairs
        foot = _frame(sx, sz, facing, 0.0, STAIRS_FOOT_M)
        top = _frame(sx, sz, facing, 0.0, STAIRS_TOP_M)
        door = _frame(sx, sz, facing, 0.0, STAIRS_TOP_M + DOOR_IN_M)
        # The bus, in the aeroplane's frame: beyond the foot, its middle door
        # level with it, facing the tail.
        foot_right, foot_forward = _local(ac_x, ac_z, ac_heading, *foot)
        # The passengers' doors are on the left, and so are the stairs; a
        # bus on the right would have its own doors facing the wrong way.
        if foot_right >= 0.0:
            self.why = "its stairs are on the right"
            return None
        side = -1.0
        bus_right = foot_right + side * BUS_OUT_M
        bus_heading = (ac_heading + 180.0) % 360.0
        # Facing the tail on the aeroplane's left, the bus's left-hand side
        # is the one towards the aeroplane. Its middle door is BUS_DOORS_
        # FORWARD[1] ahead of its reference, which faces aft.
        bus_forward = foot_forward + BUS_DOORS_FORWARD[1]
        spot = _frame(ac_x, ac_z, ac_heading, bus_right, bus_forward)
        lined_up = _frame(ac_x, ac_z, ac_heading, bus_right,
                          bus_forward + LINE_UP_M)
        nose = 0.4 * length

        def ahead_of_the_nose(point) -> bool:
            right, forward = _local(ac_x, ac_z, ac_heading, *point)
            return forward > nose + 5.0 and right * side > -5.0

        start = layout.road_start(lined_up, ROAD_FROM_M, ending=ahead_of_the_nose) \
            if layout is not None else None
        road = layout.road_route(start, lined_up, ending=ahead_of_the_nose) \
            if start is not None else []
        if not road:
            self.why = "no service road reaches it from ahead of its nose"
            return None
        first = paths.bearing(road[1][0] - road[0][0], road[1][1] - road[0][1]) \
            if len(road) > 1 else bus_heading
        t0 = now + SPAWN_LEAD_S
        arriving = paths.drive_route(road + [lined_up, spot], BUS_MS, t0,
                                     heading_from=first)
        footprints = list(footprints or ()) + [(ac_x, ac_z, ac_heading, length)]
        if not arriving or not clear(arriving, footprints):
            self.why = "no way in along the roads that keeps clear of the aeroplanes"
            return None
        arrives = paths.ends_at(arriving)
        opened = arrives + 1.0
        seats_each = min(SEATS, 3 * self.visible) / max(1, self.visible)

        walkers: list[Walker] = []
        seats: list[tuple[float, float]] = []
        filled = 0.0 if kind == DEBOARD else min(SEATS, round(seats_each * self.visible))
        seats.append((now, filled))
        clock = opened + DOORS_S + 1.0
        for i in range(self.visible):
            bus_door = BUS_DOORS_FORWARD[i % len(BUS_DOORS_FORWARD)]
            outside = _frame(*spot, bus_heading, -(BUS_DOOR_SIDE_M + STEP_OUT_M), bus_door)
            inside = _frame(*spot, bus_heading, -STEP_IN_M, bus_door)
            key = f"{callsign}/{PASSENGER}{i}"
            if kind == DEBOARD:
                walker = self._walk(key, clock, [door, top, foot], [foot, outside, inside],
                                    sill, down=True)
                seat_at = walker.gone - GONE_AFTER_S
            else:
                walker = self._walk(key, clock, [foot, top, door], [inside, outside, foot],
                                    sill, down=False)
                seat_at = walker.appears + 0.5
            walkers.append(walker)
            filled = filled + seats_each if kind == DEBOARD else filled - seats_each
            seats.append((seat_at, max(0.0, min(SEATS, round(filled)))))
            clock += EVERY_S * rng.uniform(0.7, 1.4)
        people_done = max(w.gone for w in walkers) if walkers else opened
        closing = people_done + 2.0
        leaves = closing + DOORS_S + 1.0
        doors = [(now, 0.0), (opened, 0.0), (opened + DOORS_S, 100.0),
                 (closing, 100.0), (closing + DOORS_S, 0.0)]
        # Away from the aeroplane: a turn to the outside, round to face the
        # way it came, then the roads back to where it started -- the
        # tightest turn that keeps clear of everybody.
        leaving = None
        for way in WAYS_OUT:
            turning = [_frame(ac_x, ac_z, ac_heading, bus_right + side * out,
                              bus_forward + ahead) for out, ahead in way]
            drive = paths.drive_route([spot] + turning + road[::-1], BUS_MS, leaves,
                                      heading_from=bus_heading)
            if clear(drive, footprints):
                leaving = drive
                break
        if leaving is None:
            self.why = "no way out that keeps clear of the aeroplanes"
            return None
        gone = paths.ends_at(leaving) + 1.0
        self.why = (f"a bus from {math.dist(road[0], spot):.0f} m along the roads, "
                    f"{len(walkers)} passengers, back in {gone - now:.0f} s")
        return Run(callsign, kind, now, _join(arriving, leaving), arrives, leaves,
                   gone, doors, seats, walkers)

    def _walk(self, key: str, appears: float, stairs_leg: list, ground_leg: list,
              sill: float, down: bool) -> Walker:
        """Down the stairs then across to the bus, or across then up."""
        moves = SPAWN_LEAD_S
        start = appears + moves
        if down:
            first = paths.drive_route(stairs_leg, STAIRS_MS, start, **_PEOPLE_LIMITS)
            second = paths.drive_route(ground_leg, WALK_MS, paths.ends_at(first),
                                       **_PEOPLE_LIMITS)
            climb = first
        else:
            first = paths.drive_route(ground_leg, WALK_MS, start, **_PEOPLE_LIMITS)
            second = paths.drive_route(stairs_leg, STAIRS_MS, paths.ends_at(first),
                                       **_PEOPLE_LIMITS)
            climb = second
        path = _join(first, second)
        walk = [(first[0].t0, paths.ends_at(first),
                 STAIRS_MS if down else WALK_MS),
                (second[0].t0, paths.ends_at(second),
                 WALK_MS if down else STAIRS_MS)]
        lift = self._lift(climb, stairs_leg, sill, down)
        # Before the first step, already at the height it starts at.
        lift.insert(0, (appears, lift[0][1]))
        return Walker(key, appears, path, walk, lift,
                      paths.ends_at(path) + GONE_AFTER_S)

    @staticmethod
    def _lift(leg: list, points: list, sill: float, down: bool) -> list:
        """Height on the stairs, by how far along their run a passenger is.
        ``points`` is the stairs leg: the door and the top then the foot
        going down, the foot then the top and the door going up."""
        top, foot = (points[1], points[2]) if down else (points[1], points[0])
        run = math.dist(top, foot) or 1.0
        marks = []
        t, end = leg[0].t0, paths.ends_at(leg)
        while True:
            x, z, _, _ = paths.evaluate(leg, t)
            # Along the stairs from the foot, as a share of their run; past
            # the top (the doorway) it is the sill.
            along = ((x - foot[0]) * (top[0] - foot[0])
                     + (z - foot[1]) * (top[1] - foot[1])) / (run * run)
            height = FOOT_LIFT_M + (sill - FOOT_LIFT_M) * min(1.0, max(0.0, along))
            if along <= 0.0:
                height = 0.0
            marks.append((t, round(height, 3)))
            if t >= end:
                break
            t = min(end, t + 0.25)
        return marks


__all__ = ["Planner", "Run", "Walker", "clear", "BUS", "PASSENGER", "DEBOARD", "BOARD",
           "SEATS", "BUS_DOORS_FORWARD"]
