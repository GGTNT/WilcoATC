"""The aeroplane drawn, following the aeroplane flown.

The traffic is flown by :mod:`wilcoatc.atc.traffic` once an engine tick, and
for as long as this program has drawn traffic it was also *written* to the
simulator once a tick. The tick is nominally a quarter of a second and in a
real session is anything from that to nine seconds (the log of 15 October at
Innsbruck: nine ticks of 2.9 to 8.0 s in twelve minutes). So an airliner
taxiing at fifteen knots was put down every few metres, its heading snapped
round at every taxiway node, and on the takeoff roll -- a hundred and fifty
knots, seventy-seven metres a second -- each write was tens of metres from
the last and a slow tick hundreds: the takeoff was a teleport, not a roll.
And between writes nothing held the aeroplane still, so the simulator moved
it on its own and the next write pulled it back.

This draws it in between, every visual frame, from what the model last said.
Two ideas, both from the way networked games draw each other's players:

*Predict.* From the last tick's position, heading, speed, how fast those were
changing and -- on a taxiway -- the route ahead, where the model will have
got to by now. On the ground that is a distance along the route, its corners
rounded to the curve a nosewheel draws; in the air and on a runway it is the
aeroplane carried on along its turn, its acceleration and its climb.

*Blend.* When the next tick arrives, the prediction is a little off. The
difference is not jumped: it is kept as an offset that dies away over most of
a second, so the drawn aeroplane eases on to the model's.

And the attitude the motion implies, which the model has no use for and the
window does: the nose up at the rotation rate from Vr, the climb's pitch from
its flight path, a bank from the rate of turn, the flare, the nose let down
after the touchdown, the gear up after a positive climb and down on approach.

Nothing here knows about SimConnect; :mod:`wilcoatc.integrations.aitraffic`
writes what it gives. Times are the model's clock (``time.monotonic``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .flying import LIFTOFF_AFTER_S, ROTATE_DEG_S

EARTH_M = 6_371_008.8
KT_MS = 0.514444
FT_M = 0.3048
G = 9.80665

#: A tick that has not come for this long is not predicted past: whatever the
#: model is doing by then, guessing further is worse than waiting.
MAX_PREDICT_S = 6.0
#: How long a correction takes to die away (to a third), on the ground and
#: in the air.
BLEND_GROUND_S = 0.7
BLEND_AIR_S = 1.2
#: A correction bigger than this is not a correction but a relocation -- a
#: new model swapped in, a flight placed somewhere else -- and is jumped.
SNAP_M = 250.0
SNAP_FT = 600.0
#: A taxiway corner is drawn as a curve this far either side of the node, at
#: most: the arc the nosewheel draws, not the angle in the graph.
FILLET_M = 28.0
#: Corners sharper than this are a stop and a change of direction.
CUSP_DEG = 120.0
_ARC_PIECES = 10

#: Pitch, degrees, nose up positive. Rotation at the model's rate; the nose
#: let down after the touchdown more slowly than it came up.
ROTATE_CAP_DEG = 10.0
PITCH_UP_DEG_S = 3.0
PITCH_DOWN_DEG_S = 2.0
DEROTATE_DEG_S = 1.5
PITCH_MIN_DEG = -4.0
PITCH_MAX_DEG = 17.0
#: Angle of attack added to the flight path: climbing out at V2+10 an
#: airliner is at about seven degrees, on the approach at about five.
AOA_CLIMB_DEG = 7.0
AOA_DESCENT_DEG = 5.0
#: Bank: from the rate of turn, no more than this, no faster than this.
BANK_MAX_DEG = 25.0
BANK_DEG_S = 6.0
#: The rate of turn read off two ticks is believed up to this.
TURN_MAX_AIR_DEG_S = 5.0
TURN_MAX_GROUND_DEG_S = 15.0
#: Gear: up once climbing past this height, this long after the wheels left;
#: down on the way in below this height.
GEAR_UP_AGL_FT = 50.0
GEAR_UP_AFTER_S = 2.0
GEAR_UP_VS_FPM = 300.0
GEAR_DOWN_AGL_FT = 2000.0


@dataclass
class Pose:
    """Where to draw an aeroplane now, and how."""

    lat: float
    lon: float
    alt_ft: float
    heading: float
    pitch: float          # degrees, nose up positive
    bank: float           # degrees, right wing down positive
    gs_kt: float
    vs_fpm: float
    on_ground: bool
    gear_down: bool
    # The model's runway height under it, for the injector to tie the
    # model's heights to the simulator's ground.
    surface_ft: float = 0.0


def _wrap(angle: float) -> float:
    return (angle + 540.0) % 360.0 - 180.0


def _heading(dx: float, dz: float) -> float:
    return math.degrees(math.atan2(dx, dz)) % 360.0


def _towards(value: float, wanted: float, rate: float, dt: float) -> float:
    step = rate * dt
    if abs(wanted - value) <= step:
        return wanted
    return value + step if wanted > value else value - step


class _Route:
    """A taxi route with its corners rounded, measured along its length.

    ``points`` start at the aeroplane; ``push`` of the legs after it are
    driven backwards. Evaluated by distance: position, and the direction of
    motion, which turns smoothly through each curve.
    """

    def __init__(self, points: list[tuple[float, float]], push: int = 0):
        pts = [points[0]]
        for p in points[1:]:
            if math.hypot(p[0] - pts[-1][0], p[1] - pts[-1][1]) > 0.05:
                pts.append(p)
            elif len(pts) <= push:
                push -= 1
        self.cusps: list[float] = []
        self.push_end = 0.0
        dense: list[tuple[float, float]] = [pts[0]]
        cumulative = 0.0
        # Where each original vertex ends up along the route, to know where
        # the pushback stops.
        for i in range(1, len(pts)):
            a, b = pts[i - 1], pts[i]
            if i < len(pts) - 1:
                c = pts[i + 1]
                d1 = (b[0] - a[0], b[1] - a[1])
                d2 = (c[0] - b[0], c[1] - b[1])
                l1, l2 = math.hypot(*d1), math.hypot(*d2)
                turn = abs(_wrap(_heading(*d2) - _heading(*d1)))
                cusp = turn > CUSP_DEG or (push and i == push)
                if 2.0 < turn and not cusp:
                    half = math.radians(turn) / 2.0
                    t = min(FILLET_M * math.tan(half), 0.45 * l1, 0.45 * l2)
                    start = (b[0] - d1[0] / l1 * t, b[1] - d1[1] / l1 * t)
                    end = (b[0] + d2[0] / l2 * t, b[1] + d2[1] / l2 * t)
                    dense.append(start)
                    for k in range(1, _ARC_PIECES):
                        u = k / _ARC_PIECES
                        # A quadratic curve on the corner: close enough to the
                        # circle at these angles, and tangent at both ends.
                        dense.append((
                            (1 - u) ** 2 * start[0] + 2 * (1 - u) * u * b[0] + u * u * end[0],
                            (1 - u) ** 2 * start[1] + 2 * (1 - u) * u * b[1] + u * u * end[1]))
                    dense.append(end)
                    continue
                dense.append(b)
                if cusp:
                    self.cusps.append(self._length(dense))
                    if push and i == push:
                        self.push_end = self.cusps[-1]
                continue
            dense.append(b)
        if push and push >= len(pts) - 1:
            self.push_end = self._length(dense)
        self.points = dense
        self.cumulative = [0.0]
        for a, b in zip(dense, dense[1:]):
            cumulative += math.hypot(b[0] - a[0], b[1] - a[1])
            self.cumulative.append(cumulative)
        self.length = cumulative
        self.headings = [_heading(b[0] - a[0], b[1] - a[1])
                         for a, b in zip(dense, dense[1:])] or [0.0]

    @staticmethod
    def _length(points) -> float:
        return sum(math.hypot(b[0] - a[0], b[1] - a[1])
                   for a, b in zip(points, points[1:]))

    def at(self, s: float) -> tuple[float, float, float]:
        """Position and direction of motion ``s`` metres along."""
        s = max(0.0, min(self.length, s))
        cum = self.cumulative
        # The segment containing s.
        lo, hi = 0, len(cum) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if cum[mid] <= s:
                lo = mid
            else:
                hi = mid
        a, b = self.points[lo], self.points[min(hi, len(self.points) - 1)]
        span = cum[hi] - cum[lo] if hi > lo else 0.0
        u = (s - cum[lo]) / span if span > 0 else 0.0
        x = a[0] + (b[0] - a[0]) * u
        z = a[1] + (b[1] - a[1]) * u
        # The direction, interpolated between segment middles so it turns
        # smoothly through a curve rather than in steps -- but never across
        # a cusp, where the aeroplane stops and goes the other way.
        here = self.headings[min(lo, len(self.headings) - 1)]
        mid = cum[lo] + span / 2.0
        if s < mid and lo > 0 and not self._cusp_at(cum[lo]):
            prev = self.headings[lo - 1]
            prev_mid = (cum[lo - 1] + cum[lo]) / 2.0
            w = (s - prev_mid) / max(1e-6, mid - prev_mid)
            here = (prev + _wrap(here - prev) * w) % 360.0
        elif s >= mid and lo + 1 < len(self.headings) and not self._cusp_at(cum[hi]):
            nxt = self.headings[lo + 1]
            next_mid = (cum[hi] + cum[min(hi + 1, len(cum) - 1)]) / 2.0
            w = (s - mid) / max(1e-6, next_mid - mid)
            here = (here + _wrap(nxt - here) * w) % 360.0
        return x, z, here

    def project(self, x: float, z: float) -> float:
        """How far along the route its nearest point to (x, z) is."""
        best, found = float("inf"), 0.0
        for i, (a, b) in enumerate(zip(self.points, self.points[1:])):
            dx, dz = b[0] - a[0], b[1] - a[1]
            span = dx * dx + dz * dz
            u = 0.0 if span == 0 else max(0.0, min(1.0, ((x - a[0]) * dx
                                                        + (z - a[1]) * dz) / span))
            gap = math.hypot(x - (a[0] + u * dx), z - (a[1] + u * dz))
            if gap < best:
                best, found = gap, self.cumulative[i] + u * math.sqrt(span)
        return found

    def _cusp_at(self, s: float) -> bool:
        return any(abs(s - c) < 0.01 for c in self.cusps)


@dataclass
class _Sample:
    t: float
    x: float
    z: float
    alt: float
    heading: float
    gs: float              # knots
    vs: float              # feet a minute
    on_ground: bool
    arriving: bool = False
    light: bool = False
    rotate_at: float | None = None
    surface: float = 0.0
    route: _Route | None = None
    face: float | None = None
    start: float = 0.0     # how far along its route the model is
    omega: float = 0.0     # degrees a second
    accel: float = 0.0     # metres a second squared
    extra: dict = field(default_factory=dict)


class Follower:
    """One aeroplane, drawn smoothly from the model's ticks."""

    def __init__(self, motion: dict, now: float):
        self.lat0 = float(motion["lat"])
        self.lon0 = float(motion["lon"])
        self._cos = max(0.05, math.cos(math.radians(self.lat0)))
        self._sample: _Sample | None = None
        self._offset = (0.0, 0.0, 0.0, 0.0)
        self._offset_at = now
        self._pitch: float | None = None
        self._bank = 0.0
        self._gear: bool | None = None
        self._was_on_ground: bool | None = None
        self._liftoff_at: float | None = None
        self._last_now: float | None = None
        self.update(motion, now)

    # -- the local plane ---------------------------------------------------

    def _xy(self, lat: float, lon: float) -> tuple[float, float]:
        return (math.radians(lon - self.lon0) * EARTH_M * self._cos,
                math.radians(lat - self.lat0) * EARTH_M)

    def _latlon(self, x: float, z: float) -> tuple[float, float]:
        return (self.lat0 + math.degrees(z / EARTH_M),
                self.lon0 + math.degrees(x / (EARTH_M * self._cos)))

    # -- ticks ---------------------------------------------------------------

    def update(self, motion: dict, now: float) -> None:
        """The model's latest word on this aeroplane."""
        t = float(motion.get("t") or now)
        previous = self._sample
        if previous is not None and t <= previous.t:
            return
        x, z = self._xy(float(motion["lat"]), float(motion["lon"]))
        sample = _Sample(
            t=t, x=x, z=z, alt=float(motion.get("alt", 0.0)),
            heading=float(motion.get("heading", 0.0)) % 360.0,
            gs=max(0.0, float(motion.get("gs", 0.0))),
            vs=float(motion.get("vs", 0.0)),
            on_ground=bool(motion.get("on_ground", True)),
            arriving=bool(motion.get("arriving", False)),
            light=bool(motion.get("light", False)),
            rotate_at=motion.get("rotate_at"),
            surface=float(motion.get("surface_ft", motion.get("alt", 0.0))),
            face=motion.get("face"))
        route = motion.get("route") or []
        if route and sample.on_ground:
            behind = [self._xy(lat, lon) for lat, lon in motion.get("behind") or []]
            ahead = [self._xy(lat, lon) for lat, lon in route]
            push = int(motion.get("push") or 0)
            if behind and not push:
                # Built from where it came from, so the curve round a corner
                # does not shrink as the aeroplane closes on the node; the
                # model is placed on it by its nearest point.
                sample.route = _Route(behind + ahead)
                sample.start = sample.route.project(x, z)
            else:
                sample.route = _Route([(x, z)] + ahead, push)
        if previous is not None and 0.0 < t - previous.t < 3.0:
            gap = t - previous.t
            limit = (TURN_MAX_GROUND_DEG_S if sample.on_ground
                     else TURN_MAX_AIR_DEG_S)
            sample.omega = max(-limit, min(limit, _wrap(
                sample.heading - previous.heading) / gap))
            sample.accel = max(-3.0, min(3.0, (sample.gs - previous.gs)
                                         * KT_MS / gap))
        if previous is None or self._last_now is None:
            self._sample = sample
            self._offset = (0.0, 0.0, 0.0, 0.0)
            self._offset_at = now
            return
        drawn = self._raw(now)
        self._sample = sample
        fresh = self._predict(sample, now)
        dx, dz = drawn[0] - fresh[0], drawn[1] - fresh[1]
        dalt = drawn[2] - fresh[2]
        dhead = _wrap(drawn[3] - fresh[3])
        if math.hypot(dx, dz) > SNAP_M or abs(dalt) > SNAP_FT:
            dx = dz = dalt = dhead = 0.0
        self._offset = (dx, dz, dalt, dhead)
        self._offset_at = now

    # -- where the model will have got to -----------------------------------

    def _predict(self, s: _Sample, now: float):
        """(x, z, alt, heading, speed kt, turn rate deg/s) at ``now``."""
        dt = max(0.0, min(MAX_PREDICT_S, now - s.t))
        v = s.gs * KT_MS
        a = s.accel
        if a < 0.0 and v + a * dt < 0.0:
            dt_run = v / -a
            distance = v * dt_run / 2.0
            speed = 0.0
        else:
            distance = max(0.0, v * dt + 0.5 * a * dt * dt)
            speed = max(0.0, v + a * dt)
        if s.route is not None:
            route = s.route
            distance = min(s.start + distance, route.length)
            if route.length > 0.5 and (s.gs > 0.05 or distance > s.start):
                x, z, moving = route.at(distance)
                heading = (moving + 180.0) % 360.0 if distance < route.push_end \
                    else moving
                if distance >= route.length - 0.05 and s.face is not None:
                    heading = float(s.face)
            else:
                x, z, heading = s.x, s.z, s.heading
            return x, z, s.alt, heading, speed / KT_MS, 0.0
        turn = s.omega * dt
        heading = (s.heading + turn) % 360.0
        mean = math.radians(s.heading + turn / 2.0)
        half = math.radians(turn) / 2.0
        chord = distance * (math.sin(half) / half if abs(half) > 1e-6 else 1.0)
        x = s.x + chord * math.sin(mean)
        z = s.z + chord * math.cos(mean)
        alt = s.alt if s.on_ground else s.alt + s.vs / 60.0 * dt
        if not s.on_ground and s.vs < 0.0:
            alt = max(alt, s.surface)
        return x, z, alt, heading, speed / KT_MS, s.omega

    def _raw(self, now: float):
        s = self._sample
        x, z, alt, heading, speed, omega = self._predict(s, now)
        tau = BLEND_GROUND_S if s.on_ground else BLEND_AIR_S
        fade = math.exp(-max(0.0, now - self._offset_at) / tau)
        ox, oz, oalt, ohead = self._offset
        return (x + ox * fade, z + oz * fade, alt + oalt * fade,
                (heading + ohead * fade) % 360.0, speed, omega)

    # -- drawing -------------------------------------------------------------

    def pose(self, now: float) -> Pose:
        """Where and how to draw it now. Called once a frame, in order."""
        s = self._sample
        x, z, alt, heading, speed, omega = self._raw(now)
        dt = 0.0 if self._last_now is None else max(0.0, min(0.5, now - self._last_now))
        self._last_now = now
        on_ground = s.on_ground
        if (on_ground and s.rotate_at is not None
                and now >= s.rotate_at + LIFTOFF_AFTER_S):
            on_ground = False            # the model will say so next tick
        if self._was_on_ground and not on_ground:
            self._liftoff_at = now
        self._was_on_ground = on_ground

        pitch_cap = PITCH_MAX_DEG * (0.7 if s.light else 1.0)
        if on_ground:
            if s.rotate_at is not None and speed > 1.0:
                wanted = min(ROTATE_CAP_DEG * (0.8 if s.light else 1.0),
                             ROTATE_DEG_S * max(0.0, now - s.rotate_at))
                up, down = PITCH_UP_DEG_S, DEROTATE_DEG_S
            else:
                wanted, up, down = 0.0, PITCH_UP_DEG_S, DEROTATE_DEG_S
        else:
            vs = s.vs
            gamma = math.degrees(math.atan2(vs * FT_M / 60.0,
                                            max(30.0, speed * KT_MS)))
            w = max(0.0, min(1.0, (vs + 300.0) / 1500.0))
            aoa = AOA_DESCENT_DEG + (AOA_CLIMB_DEG - AOA_DESCENT_DEG) * w
            if s.light:
                aoa *= 0.6
            wanted = max(PITCH_MIN_DEG, min(pitch_cap, gamma + aoa))
            up, down = PITCH_UP_DEG_S, PITCH_DOWN_DEG_S
        if self._pitch is None:
            self._pitch = wanted
        elif wanted > self._pitch:
            self._pitch = min(wanted, self._pitch + up * dt)
        else:
            self._pitch = max(wanted, self._pitch - down * dt)

        if on_ground:
            bank_wanted = 0.0
        else:
            bank_wanted = math.degrees(math.atan(
                speed * KT_MS * math.radians(omega) / G))
            bank_wanted = max(-BANK_MAX_DEG, min(BANK_MAX_DEG, bank_wanted))
        self._bank = _towards(self._bank, bank_wanted, BANK_DEG_S, dt)

        agl = alt - s.surface
        if on_ground:
            gear = True
        elif self._gear is None:
            gear = s.arriving and agl < GEAR_DOWN_AGL_FT
        elif s.arriving:
            gear = self._gear or agl < GEAR_DOWN_AGL_FT
        else:
            gear = self._gear
            if (gear and s.vs > GEAR_UP_VS_FPM and agl > GEAR_UP_AGL_FT
                    and (self._liftoff_at is None
                         or now - self._liftoff_at >= GEAR_UP_AFTER_S)):
                gear = False
        self._gear = gear

        lat, lon = self._latlon(x, z)
        return Pose(lat=lat, lon=lon, alt_ft=alt, heading=heading,
                    pitch=self._pitch, bank=self._bank, gs_kt=speed,
                    vs_fpm=0.0 if on_ground else s.vs, on_ground=on_ground,
                    gear_down=gear, surface_ft=s.surface)

    @property
    def sample_time(self) -> float:
        return self._sample.t if self._sample else 0.0


__all__ = ["Follower", "Pose", "MAX_PREDICT_S", "SNAP_M"]
