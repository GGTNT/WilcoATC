"""The way a ground vehicle drives: curves, easing, and a heading that follows.

The first version moved a vehicle along a straight ray at a constant speed,
pointing the same way the whole time, and the position was only written
once per engine tick -- a quarter of a second when all was well and up to
ten when it was not, so a truck at four metres a second jumped six to forty
metres at a time. Measured at Madeira on one catering truck: our own writes
at 30 Hz against a 55 fps simulator left frames with no movement and a
catch-up step of 68 cm (nine times the median); the simulator's own
waypoint driver was smoother and finished 24.6 m from where it was sent.
One write per visual frame along a curve, with the vehicle frozen
(FREEZE_LATITUDE_LONGITUDE_SET and the others) so the simulator does not
drive it as well, had the largest step at three times the median and the
truck stopped 0.00 m from its spot -- unfrozen, a truck left standing had
crept 4.1 m in fifteen seconds.

So a drive is a list of :class:`Segment`: a cubic Bezier between two
times, eased (smoothstep: it pulls away and brakes rather than starting
and stopping dead), heading along the curve's tangent, or against it for a
reverse. The ramp builds them (:mod:`wilcoatc.atc.handling`); the vehicle
layer evaluates them every frame (:mod:`wilcoatc.integrations.groundvehicles`)
and the ramp evaluates the same ones for itself, so the two never disagree
about where a vehicle is.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

Point = tuple[float, float]


@dataclass(frozen=True)
class Segment:
    """One stretch of a drive: x east and z north (or degrees of longitude
    and latitude -- it does not care), from ``t0`` to ``t1``."""

    t0: float
    t1: float
    p0: Point
    p1: Point
    p2: Point
    p3: Point
    # Backing up: the vehicle faces against the way it is going.
    reverse: bool = False
    # The heading it starts from, blended into the curve's over the first
    # part of the segment, so a vehicle that has stopped facing one way
    # turns as it pulls away rather than snapping round. None: the curve's.
    heading_from: float | None = None
    # The speeds it enters and leaves at, changing evenly between; None is
    # smoothstep, from standing to standing. A drive of many pieces gives
    # each its own: eased at every joint, a bus along a service road stopped
    # dead at each of its nodes.
    v0: float | None = None
    v1: float | None = None


#: How much of a segment a starting heading is blended over.
BLEND = 0.3


def _bezier(s: Segment, t: float) -> Point:
    u = 1.0 - t
    return (u ** 3 * s.p0[0] + 3 * u * u * t * s.p1[0] + 3 * u * t * t * s.p2[0]
            + t ** 3 * s.p3[0],
            u ** 3 * s.p0[1] + 3 * u * u * t * s.p1[1] + 3 * u * t * t * s.p2[1]
            + t ** 3 * s.p3[1])


def _tangent(s: Segment, t: float) -> Point:
    u = 1.0 - t
    dx = (3 * u * u * (s.p1[0] - s.p0[0]) + 6 * u * t * (s.p2[0] - s.p1[0])
          + 3 * t * t * (s.p3[0] - s.p2[0]))
    dz = (3 * u * u * (s.p1[1] - s.p0[1]) + 6 * u * t * (s.p2[1] - s.p1[1])
          + 3 * t * t * (s.p3[1] - s.p2[1]))
    return dx, dz


def ease(u: float) -> float:
    """Smoothstep: zero speed at both ends."""
    u = min(1.0, max(0.0, u))
    return u * u * (3.0 - 2.0 * u)


def _timing(s: Segment, u: float) -> float:
    """How far along the curve a segment is, ``u`` of the way through its
    time: smoothstep, or an even change from ``v0`` to ``v1``."""
    u = min(1.0, max(0.0, u))
    if s.v0 is None or s.v1 is None or s.v0 + s.v1 <= 0:
        return ease(u)
    return (2.0 * s.v0 * u + (s.v1 - s.v0) * u * u) / (s.v0 + s.v1)


def bearing(dx: float, dz: float) -> float:
    return math.degrees(math.atan2(dx, dz)) % 360.0


def blend_heading(a: float, b: float, k: float) -> float:
    """From ``a`` to ``b`` the short way round, ``k`` of the way."""
    delta = ((b - a + 180.0) % 360.0) - 180.0
    return (a + delta * min(1.0, max(0.0, k))) % 360.0


def heading_on(s: Segment, t: float) -> float:
    """Where the vehicle points at curve parameter ``t``."""
    dx, dz = _tangent(s, t)
    if abs(dx) < 1e-12 and abs(dz) < 1e-12:
        dx, dz = s.p3[0] - s.p0[0], s.p3[1] - s.p0[1]
    heading = bearing(dx, dz)
    if s.reverse:
        heading = (heading + 180.0) % 360.0
    return heading


def evaluate(segments: list[Segment], now: float,
             last_heading: float | None = None
             ) -> tuple[float, float, float, bool] | None:
    """(x, z, heading, moving) at ``now``; None with no segments.

    Before the first segment, at its start; after the last, at its end and
    not moving.
    """
    if not segments:
        return None
    first, last = segments[0], segments[-1]
    if now <= first.t0:
        x, z = first.p0
        heading = (first.heading_from if first.heading_from is not None
                   else heading_on(first, 0.0))
        return x, z, heading, False
    if now >= last.t1:
        x, z = last.p3
        return x, z, heading_on(last, 0.999), False
    for s in segments:
        if s.t0 <= now < s.t1:
            span = max(1e-6, s.t1 - s.t0)
            u = (now - s.t0) / span
            t = _timing(s, u)
            x, z = _bezier(s, t)
            heading = heading_on(s, min(t, 0.999))
            if s.heading_from is not None and u < BLEND:
                heading = blend_heading(s.heading_from, heading, ease(u / BLEND))
            return x, z, heading, True
    # In a gap between two segments: standing at the end of the earlier.
    earlier = max((s for s in segments if s.t1 <= now), key=lambda s: s.t1)
    x, z = earlier.p3
    return x, z, heading_on(earlier, 0.999), False


def ends_at(segments: list[Segment]) -> float:
    return max((s.t1 for s in segments), default=0.0)


def _toward(a: Point, b: Point, metres: float) -> Point:
    dx, dz = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dz) or 1.0
    return a[0] + dx / length * metres, a[1] + dz / length * metres


def _along(p: Point, heading: float, metres: float) -> Point:
    rad = math.radians(heading)
    return p[0] + math.sin(rad) * metres, p[1] + math.cos(rad) * metres


#: How far along its parked heading a vehicle lines up before it stops,
#: and how far it backs out of its spot before it turns away.
LINE_UP_M = 10.0
BACK_OUT_M = 7.0
BACK_OUT_SHARE = 0.3


def drive_in(start: Point, spot: Point, heading: float, t0: float,
             t1: float) -> list[Segment]:
    """From out on the apron to its spot, arriving nose first along the
    heading it parks at."""
    lined_up = _along(spot, heading, -LINE_UP_M)
    return [Segment(t0, t1, start, _toward(start, lined_up, LINE_UP_M),
                    lined_up, spot)]


def drive_out(spot: Point, heading: float, away: Point, t0: float,
              t1: float, backs_out: bool = True) -> list[Segment]:
    """From its spot back out onto the apron: a short reverse straight out
    of the spot, then a turn away -- the heading blended, not snapped."""
    if not backs_out:
        return [Segment(t0, t1, spot, _along(spot, heading, LINE_UP_M * 0.5),
                        _toward(away, spot, LINE_UP_M), away,
                        heading_from=heading)]
    split = t0 + (t1 - t0) * BACK_OUT_SHARE
    backed = _along(spot, heading, -BACK_OUT_M)
    return [
        Segment(t0, split, spot, _along(spot, heading, -BACK_OUT_M / 3),
                _along(spot, heading, -2 * BACK_OUT_M / 3), backed,
                reverse=True),
        Segment(split, t1, backed, _toward(backed, away, LINE_UP_M * 0.6),
                _toward(away, backed, LINE_UP_M), away,
                heading_from=heading),
    ]


#: How far back from a corner a route's curve begins, at most: the corner
#: of a service road is rounded off rather than turned on the spot.
CORNER_M = 10.0
#: How fast a vehicle on a route may turn, and speed up or slow down. Taken
#: at full speed, Madeira's tighter road corners were 119 degrees a second,
#: a bus on a three-metre radius; 0.4 rad/s is about 4 m/s round 10 m.
TURN_RAD_S = 0.4
ACCEL_MS2 = 0.8
#: Never slower than this through a corner, however tight.
CRAWL_MS = 1.5


def drive_route(points: list[Point], speed: float, t0: float,
                heading_from: float | None = None, *,
                turn_rad_s: float | None = None, accel: float | None = None,
                crawl: float | None = None,
                corner_m: float | None = None) -> list[Segment]:
    """Along a polyline at up to ``speed`` metres a second: straight between
    the corners, an arc through each, from standing to standing.

    Each corner is taken no faster than :data:`TURN_RAD_S` allows on its
    radius, and every straight changes speed at :data:`ACCEL_MS2` at most:
    up to cruising, along, and down to the next corner's speed, or straight
    from one to the other where the straight is too short to cruise. A
    corner is cut back by up to :data:`CORNER_M`, never more than a third of
    either side, so two corners close together cannot overlap. The four
    limits default to a bus's; a person on foot turns and stops in a step.
    """
    turn_rad_s = TURN_RAD_S if turn_rad_s is None else turn_rad_s
    accel = ACCEL_MS2 if accel is None else accel
    crawl = CRAWL_MS if crawl is None else crawl
    corner_m = CORNER_M if corner_m is None else corner_m
    pts = [tuple(map(float, p)) for i, p in enumerate(points)
           if i == 0 or math.dist(p, points[i - 1]) > 0.05]
    if len(pts) < 2 or speed <= 0:
        return []
    # Each piece as its control points and the speed it may run through
    # at (None for a straight, which takes its speeds from its ends).
    pieces: list[tuple[tuple[Point, Point, Point, Point], float | None]] = []
    here = pts[0]
    for i in range(1, len(pts) - 1):
        before, corner, after = pts[i - 1], pts[i], pts[i + 1]
        cut = min(corner_m, math.dist(before, corner) / 3.0,
                  math.dist(corner, after) / 3.0)
        enter = _toward(corner, before, cut)
        leave = _toward(corner, after, cut)
        pieces.append((_straight(here, enter), None))
        # A circular arc through the corner. With a quadratic's handles the
        # speed through a right angle swung 4.4-6.1 m/s at 5, and the bus
        # sped up in the bend; an arc's keep it within a few per cent.
        # 4/3 tan(turn/4) R is the cubic arc's handle, R = cut / tan(turn/2).
        turn = math.radians(abs(((bearing(after[0] - corner[0], after[1] - corner[1])
                                  - bearing(corner[0] - before[0], corner[1] - before[1])
                                  + 180.0) % 360.0) - 180.0))
        if 1e-3 < turn < math.pi - 1e-3:
            radius = cut / math.tan(turn / 2.0)
            handle = (4.0 / 3.0) * math.tan(turn / 4.0) / math.tan(turn / 2.0)
            through = max(crawl, min(speed, turn_rad_s * radius))
        else:
            handle, through = 2.0 / 3.0, (speed if turn <= 1e-3 else crawl)
        pieces.append(((enter, _lerp(enter, corner, handle),
                        _lerp(leave, corner, handle), leave), through))
        here = leave
    pieces.append((_straight(here, pts[-1]), None))
    pieces = [(c, v) for c, v in pieces if math.dist(c[0], c[3]) > 0.01]
    # The speed at every joint: standing at both ends, a corner's own
    # speed either side of it.
    joints = [0.0]
    for (_, v), (_, w) in zip(pieces, pieces[1:]):
        joints.append(v if v is not None else w if w is not None else speed)
    joints.append(0.0)
    segments: list[Segment] = []
    t = t0
    for i, (controls, through) in enumerate(pieces):
        v0, v1 = joints[i], joints[i + 1]
        if through is not None:
            parts = [(controls, v0, v1)]
        else:
            parts = _straight_parts(controls[0], controls[3], v0, v1, speed,
                                    accel)
        for (a, b, c, d), u0, u1 in parts:
            length = _length(a, b, c, d)
            if u0 + u1 <= 0:
                # Standing to standing on a straight too short to speed up
                # on: smoothstep, at the mean speed it can manage.
                mean = max(0.5, min(speed, math.sqrt(accel * length) / 1.5))
                span, u0, u1 = length / mean, None, None
            else:
                span = 2.0 * length / (u0 + u1)
            segments.append(Segment(t, t + max(0.05, span), a, b, c, d,
                                    heading_from=heading_from if not segments else None,
                                    v0=u0, v1=u1))
            t += max(0.05, span)
    return segments


def _straight_parts(a: Point, d: Point, v0: float, v1: float, speed: float,
                    accel: float = ACCEL_MS2):
    """A straight from ``v0`` to ``v1``: up to ``speed``, along, and down,
    each change at ACCEL_MS2 -- or, too short to reach cruising, up to the
    fastest it can and down again."""
    length = math.dist(a, d)
    up = max(0.0, speed * speed - v0 * v0) / (2.0 * accel)
    down = max(0.0, speed * speed - v1 * v1) / (2.0 * accel)
    if up + down < length:
        p = _toward(a, d, up)
        q = _toward(d, a, down)
        marks = [(a, p, v0, speed), (p, q, speed, speed), (q, d, speed, v1)]
    else:
        # The peak where speeding up and slowing down meet.
        peak2 = (2.0 * accel * length + v0 * v0 + v1 * v1) / 2.0
        peak = max(v0, v1, math.sqrt(max(0.0, peak2)))
        reach = max(0.0, min(length, (peak * peak - v0 * v0) / (2.0 * accel)))
        p = _toward(a, d, reach)
        marks = [(a, p, v0, peak), (p, d, peak, v1)]
    return [(_straight(x, y), u0, u1) for x, y, u0, u1 in marks
            if math.dist(x, y) > 0.01]


def _lerp(a: Point, b: Point, k: float) -> Point:
    return a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k


def _straight(a: Point, b: Point) -> tuple[Point, Point, Point, Point]:
    return a, _lerp(a, b, 1 / 3), _lerp(a, b, 2 / 3), b


def _length(a: Point, b: Point, c: Point, d: Point, steps: int = 16) -> float:
    s = Segment(0.0, 1.0, a, b, c, d)
    marks = [_bezier(s, k / steps) for k in range(steps + 1)]
    return sum(math.dist(p, q) for p, q in zip(marks, marks[1:]))


__all__ = ["Segment", "evaluate", "ends_at", "ease", "drive_in", "drive_out",
           "drive_route", "bearing", "blend_heading", "heading_on"]
