"""The ground a vehicle takes up beside an aeroplane, and the way it comes in.

The ramp (:mod:`wilcoatc.atc.handling`) sent each vehicle to its spot at its
time and nothing else: at Bilbao (17 October) the caterer was sent forty
seconds into boarding, twenty after the forward belt loader, and on the
Asobo A321 the galley door and the forward hold are 3.2 m apart along the
fuselage (its interactive points, read off the model), so the two parked
trucks stood in each other. Real ramps work in order and keep out of each
other's way, which is what this module is for: each parked vehicle covers a
rectangle -- its footprint, in its own frame, from ``turnaround.json`` --
and a vehicle on its way in also covers the strip it drives along. A
vehicle due to set off whose rectangle or strip touches one already taken
waits until it is free.

Plain geometry in the layout's metres (x east, z north, headings clockwise
from north), convex polygons tested by separating axes. Nothing here knows
about time or about SimConnect.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

Point = tuple[float, float]


@dataclass(frozen=True)
class Footprint:
    """How far a vehicle reaches from its reference, in its own frame."""

    ahead_m: float
    behind_m: float
    half_width_m: float


@dataclass(frozen=True)
class Zone:
    """Ground somebody has: a convex polygon and what to call it."""

    owner: str            # who has it: "VLG5042/baggage"
    name: str             # what the log calls it: "forward hold"
    corners: tuple[Point, ...]


# What the log calls the ground each vehicle works on.
NAMES = {
    "stairs": "L1 door", "stairs_aft": "L2 door", "catering": "R1 door",
    "baggage": "forward hold", "baggage_aft": "aft hold",
    "gpu": "ground power", "fuel": "right wing", "pushback": "nose gear",
}


def rectangle(x: float, z: float, heading: float, ahead: float, behind: float,
              half_width: float, margin: float = 0.0) -> tuple[Point, ...]:
    """The corners of a rectangle about a reference facing ``heading``."""
    h = math.radians(heading)
    fx, fz = math.sin(h), math.cos(h)          # forward
    rx, rz = math.cos(h), -math.sin(h)         # right
    a, b, w = ahead + margin, behind + margin, half_width + margin
    return tuple((x + fx * along + rx * side, z + fz * along + rz * side)
                 for along, side in ((a, w), (a, -w), (-b, -w), (-b, w)))


def footprint(x: float, z: float, heading: float, size: Footprint,
              margin: float = 0.0) -> tuple[Point, ...]:
    return rectangle(x, z, heading, size.ahead_m, size.behind_m,
                     size.half_width_m, margin)


def strip(a: Point, b: Point, half_width: float) -> tuple[Point, ...]:
    """The ground a vehicle sweeps driving straight from ``a`` to ``b``."""
    dx, dz = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dz)
    if length < 1e-6:
        return rectangle(a[0], a[1], 0.0, half_width, half_width, half_width)
    heading = math.degrees(math.atan2(dx, dz))
    return rectangle(a[0], a[1], heading, length, 0.0, half_width)


def _axes(corners: tuple[Point, ...]):
    for i, (x0, z0) in enumerate(corners):
        x1, z1 = corners[(i + 1) % len(corners)]
        yield (z1 - z0, x0 - x1)


def _spread(corners, axis) -> tuple[float, float]:
    values = [x * axis[0] + z * axis[1] for x, z in corners]
    return min(values), max(values)


def overlap(a: tuple[Point, ...], b: tuple[Point, ...]) -> bool:
    """Whether two convex polygons share any ground (touching does not)."""
    for axis in list(_axes(a)) + list(_axes(b)):
        if axis == (0.0, 0.0):
            continue
        a0, a1 = _spread(a, axis)
        b0, b1 = _spread(b, axis)
        if a1 <= b0 or b1 <= a0:
            return False
    return True


def first_in_the_way(wanted: list[tuple[Point, ...]], taken: list[Zone]) -> Zone | None:
    """The first zone already taken that any of ``wanted`` touches."""
    for zone in taken:
        if any(overlap(mine, zone.corners) for mine in wanted):
            return zone
    return None


__all__ = ["Footprint", "Zone", "NAMES", "rectangle", "footprint", "strip",
           "overlap", "first_in_the_way"]
