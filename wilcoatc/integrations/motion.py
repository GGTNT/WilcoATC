"""Everything on the apron that moves, moved by one loop.

The ground vehicles were driven every frame by the vehicle layer itself
(:mod:`wilcoatc.integrations.groundvehicles`); people, buses that drive a
long way and dock at a door, and the animation of a walking passenger need
the same loop with more in it, so it is here, for all of them.

What was measured before it was written (Madeira, 14 October):

* *Smoothness.* One write per visual frame along a curve, frozen, was the
  only way a vehicle stopped exactly on its spot (0.00 m; the simulator's
  waypoints ended 24.6 m off, a 30 Hz timer left catch-up steps of 68 cm).
* *Long drives.* A GSX bus driven by the simulator's own AI waypoints looked
  more natural to the pilot than our writes (its wheels turn either way)
  and ended 14.8 m from where it was sent. So a long leg can be the
  simulator's, and the last metres are ours: the actor is read once a
  second while the simulator drives it, and once it is close -- or has had
  its time -- it is frozen and docked along a curve to its spot.
* *People.* A GSX passenger's whole animation is one clip played from
  ``A:VELOCITY BODY Y``: the value written is the keyframe shown. Walking is
  keyframes 253-291 of CASUAL_M_0003 (one stride, 1.27 s, which suits
  1.3 m/s). Because that variable is also a real vertical speed, a person
  is frozen in altitude as well, or the first frame of the walk launches it
  into the sky. Where each model's walk is lies in ``walks.json``, found in
  its own animation by ``scripts/scan_walks.py``: the strides run from 31
  keyframes to 41 and start anywhere from 66 to 961.
* *Stairs.* Frozen in altitude, a person climbs only if it is written. Its
  height is a lift above where it stood when it was made -- read once off
  the simulator, because the field's published elevation is not the apron's
  -- and nothing is lifted before that reading is in.

And what it keeps cheap, because there will be dozens of these:

* *Rate by distance.* 35 Hz near the camera, then 15, 5, 1 -- nobody sees
  the difference between 35 and 5 Hz at four hundred metres. But SimConnect
  says where the pilot's aeroplane is, never where the camera is, and the
  distance used to be measured from the aeroplane whatever the camera was
  doing. At Bilbao (17 October) the pilot watched a boarding from away from
  their own aeroplane and the passengers were written at 15 and 5 Hz: the
  bus took 15 writes a second, against 40-100 at Madeira two days before,
  where the pilot was parked beside it. So the distance only counts while
  ``CAMERA STATE`` says the camera is on the aeroplane (cockpit, chase,
  fixed on it); with the drone, or anything else, nobody knows where the
  viewer is and everything is written at the near rate. A person keeps the
  near rate out to the far side of a stand even then: its walk is a
  keyframe, and 15 Hz of a 30 fps clip is a jerk.
* *Steady pacing.* An actor's next write is due one period after its last
  was *due*, not after the frame that made it: the other way, every period
  was rounded up to whole frames and 35 Hz at 40 fps was 20.
* *Only what changed.* Positions are rounded before they are compared, so
  an actor standing still sends nothing at all, frame after frame.
* *A budget.* At most so many writes a frame across every actor, the
  nearest first; the rest wait a frame.

It knows nothing about turnarounds. It is handed actors -- an object, a
drive in metres, maybe a simulator leg first, maybe an animation -- and
moves them.
"""

from __future__ import annotations

import ctypes
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Callable

from ..atc import paths
from .aitraffic import SIMCONNECT_DATATYPE_FLOAT64

log = logging.getLogger("wilcoatc.motion")

# This client's numbers (the vehicle poses are 4730-4735, the systems read
# 4747, the doors' points 4748; the vehicle freeze events 4740-4743).
_DEF_POSITION = 4761
_DEF_WAYPOINTS = 4762
# Three values, the shape of a position reading, so one listener reads both.
_DEF_ALTITUDE = 4763
_DEF_CHANNEL_BASE = 4770          # + channel number, one per variable
_EV_FREEZE_ALTITUDE = 4744
_EV_FREEZE_LATLON = 4745
_EV_FREEZE_ATTITUDE = 4746
# CAMERA STATE and, to fill the three slots a position reading has, the
# pilot's own latitude and longitude.
_DEF_CAMERA = 4764
_REQ_BASE = 620000
_REQ_CAMERA = 619999
_USER_OBJECT = 0
_PERIOD_SECOND = 4
_PERIOD_ONCE = 1
_PERIOD_NEVER = 0
FEET_PER_M = 3.28084
_DATATYPE_WAYPOINT = 14
_WAYPOINT_SPEED = 0x04
_WAYPOINT_ON_GROUND = 0x00100000
_EVENT_FLAG_GROUPID_IS_PRIORITY = 0x10
_PRIORITY_HIGHEST = 1

#: How often an actor is written, by how far it is from the pilot: within
#: (metres) -> Hz. Beyond the last, once a second.
RATES: tuple[tuple[float, float], ...] = ((60.0, 35.0), (180.0, 15.0),
                                          (450.0, 5.0))
FAR_HZ = 1.0
#: The same for a person: the near rate out to the far side of a stand.
PERSON_RATES: tuple[tuple[float, float], ...] = ((180.0, 35.0), (450.0, 15.0))
#: CAMERA STATE values with the camera on the pilot's own aeroplane:
#: cockpit, external/chase, fixed on the plane. Any other (drone, showcase,
#: on foot, replay...) and where the viewer is is not known.
CAMERA_AT_AIRCRAFT = frozenset({2, 3, 5})
#: The most writes made in one frame, across every actor. A write is one
#: small SetDataOnSimObject; a dozen walking passengers are three each.
WRITES_PER_FRAME = 80
#: Rounding before comparing: a tenth of a centimetre, a hundredth of a
#: degree, and the animation to a hundredth of a keyframe.
POSITION_DIGITS = 8
HEADING_DIGITS = 2
CHANNEL_DIGITS = 2
#: A simulator leg ends, and our docking starts, this close to its last
#: waypoint -- or after this long, whatever the simulator has made of it.
DOCK_WITHIN_M = 12.0
SIM_LEG_SLACK = 1.6
#: How fast the last metres are driven, docking.
DOCK_MS = 2.5


@dataclass
class Channel:
    """An animation variable written with the position: ``value(now)``."""

    name: bytes
    unit: bytes
    value: Callable[[float], float]


@dataclass
class Actor:
    """One moving object."""

    key: str
    object_id: int
    to_latlon: Callable[[float, float], tuple[float, float]]
    # Our own drive, in metres in the layout plane.
    path: list = field(default_factory=list)
    # A person: frozen in altitude too, because its animation variable is
    # also a vertical speed.
    person: bool = False
    channels: list = field(default_factory=list)
    # The simulator's leg, driven before ``path``: waypoints in metres, a
    # speed, and the inverse of ``to_latlon`` to read where it has got to.
    sim_leg: list = field(default_factory=list)
    sim_speed_kt: float = 10.0
    to_xy: Callable[[float, float], tuple[float, float]] | None = None
    # Where it ends up, for docking after a simulator leg: x, z, heading.
    final: tuple[float, float, float] | None = None
    # How far above the ground it is, in metres, as (time, metres) points
    # joined by straight lines: a passenger on the stairs. Needs ``person``.
    lift: list = field(default_factory=list)
    # -- kept by the engine ----------------------------------------------
    state: str = "ours"            # ours | sim | docking | done
    sim_since: float = 0.0
    seen: tuple[float, float, float] | None = None
    sent_position: tuple | None = None
    sent_channels: dict = field(default_factory=dict)
    next_at: float = 0.0
    frozen: bool = False
    # The altitude of the ground it stood on when made, in feet; None
    # until read.
    base_ft: float | None = None


def rate_for(distance_m: float | None, person: bool = False) -> float:
    """How often an actor this far from the viewer is written; None is a
    viewer nobody can place, and gets the near rate."""
    rates = PERSON_RATES if person else RATES
    if distance_m is None:
        return rates[0][1]
    for within, hz in rates:
        if distance_m <= within:
            return hz
    return FAR_HZ


def walking(start_keyframe: float, stride_keyframes: float,
            began: float, fps: float = 30.0) -> Callable[[float], float]:
    """A GSX person's walk cycle as a channel value: the keyframe to show
    now, looping one stride from ``began``."""
    def value(now: float) -> float:
        return start_keyframe + ((now - began) * fps) % stride_keyframes
    return value


def standing(keyframe: float = 0.0) -> Callable[[float], float]:
    return lambda now: keyframe


def walking_while(start_keyframe: float, stride_keyframes: float,
                  intervals: list[tuple[float, float]], fps: float = 30.0,
                  rest: float = 0.0) -> Callable[[float], float]:
    """The walk during each (from, to) interval, from the start of its
    stride each time; ``rest``, the clip's first pose, between. A passenger
    waiting at the foot of the stairs is not marching on the spot."""
    # An interval may carry its own rate: down a staircase the legs go
    # slower than across the apron.
    spans = sorted((float(i[0]), float(i[1]), float(i[2]) if len(i) > 2 else fps)
                   for i in intervals)

    def value(now: float) -> float:
        for began, ends, rate in spans:
            if began <= now < ends:
                return start_keyframe + ((now - began) * rate) % stride_keyframes
        return rest
    return value


def keyframed(points: list[tuple[float, float]]) -> Callable[[float], float]:
    """(time, value) points joined by straight lines, held at both ends."""
    marks = sorted((float(t), float(v)) for t, v in points)

    def value(now: float) -> float:
        if not marks:
            return 0.0
        if now <= marks[0][0]:
            return marks[0][1]
        for (t0, v0), (t1, v1) in zip(marks, marks[1:]):
            if now < t1:
                return v0 + (v1 - v0) * (now - t0) / max(1e-6, t1 - t0)
        return marks[-1][1]
    return value


class MotionEngine:
    """The one loop that moves every actor."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._dll = self._handle = None
        self._ready = False
        self._actors: dict[str, Actor] = {}
        self._channel_defs: dict[tuple[bytes, bytes], int] = {}
        self._reads: dict[int, str] = {}           # request -> actor key
        self._altitude_reads: dict[int, str] = {}  # request -> actor key
        self._next_request = _REQ_BASE
        # Where the pilot is, in the same metres as the actors -- and the
        # camera too, but only while ``camera_state`` says so.
        self.viewer_xy: tuple[float, float] | None = None
        self.camera_state: int | None = None
        self.writes = 0          # for the tests and the status line
        self._window = _Window()

    @property
    def available(self) -> bool:
        return self._ready

    def attach(self, dll, handle) -> bool:
        try:
            for name, unit in ((b"PLANE LATITUDE", b"degrees"),
                               (b"PLANE LONGITUDE", b"degrees"),
                               (b"PLANE HEADING DEGREES TRUE", b"degrees")):
                if dll.AddToDataDefinition(handle, _DEF_POSITION, name, unit,
                                           SIMCONNECT_DATATYPE_FLOAT64, 0, -1) != 0:
                    raise OSError("SimConnect refused the position")
            dll.AddToDataDefinition(handle, _DEF_WAYPOINTS, b"AI WAYPOINT LIST",
                                    b"number", _DATATYPE_WAYPOINT, 0, -1)
            for name in (b"PLANE ALTITUDE", b"PLANE ALT ABOVE GROUND",
                         b"GROUND ALTITUDE"):
                dll.AddToDataDefinition(handle, _DEF_ALTITUDE, name, b"feet",
                                        SIMCONNECT_DATATYPE_FLOAT64, 0, -1)
            for event, name in ((_EV_FREEZE_ALTITUDE, b"FREEZE_ALTITUDE_SET"),
                                (_EV_FREEZE_LATLON, b"FREEZE_LATITUDE_LONGITUDE_SET"),
                                (_EV_FREEZE_ATTITUDE, b"FREEZE_ATTITUDE_SET")):
                dll.MapClientEventToSimEvent(handle, event, name)
        except Exception:
            log.info("the motion engine could not be set up", exc_info=True)
            return False
        self._dll, self._handle, self._ready = dll, handle, True
        self._watch_camera()
        return True

    def _watch_camera(self) -> None:
        """Which camera is up, once a second, answered on the message
        thread like every other reading. Refused, the camera is never known
        and everything is written at the near rate."""
        try:
            for name, unit in ((b"CAMERA STATE", b"enum"),
                               (b"PLANE LATITUDE", b"degrees"),
                               (b"PLANE LONGITUDE", b"degrees")):
                if self._dll.AddToDataDefinition(self._handle, _DEF_CAMERA, name, unit,
                                                 SIMCONNECT_DATATYPE_FLOAT64, 0, -1) != 0:
                    raise OSError(f"SimConnect refused {name!r}")
            self._dll.RequestDataOnSimObject(self._handle, _REQ_CAMERA, _DEF_CAMERA,
                                             _USER_OBJECT, _PERIOD_SECOND, 0, 0, 0, 0)
        except Exception:
            log.info("the camera cannot be read; every actor is written at "
                     "the near rate", exc_info=True)

    def viewer(self) -> tuple[float, float] | None:
        """Where the camera is, if anybody knows: the pilot's aeroplane
        while the camera is on it, nowhere otherwise."""
        if self.camera_state in CAMERA_AT_AIRCRAFT:
            return self.viewer_xy
        return None

    # -- actors -----------------------------------------------------------

    def add(self, actor: Actor) -> None:
        old = self._actors.get(actor.key)
        if old is not None and old.object_id == actor.object_id:
            # The same object, a new drive: what was sent is still true.
            actor.sent_position, actor.sent_channels = old.sent_position, old.sent_channels
            actor.frozen = old.frozen
            actor.base_ft = old.base_ft
        self._actors[actor.key] = actor
        if actor.lift and actor.base_ft is None and self._ready:
            self._read_altitude(actor)
        if actor.sim_leg and self._ready:
            self._start_sim_leg(actor)
        elif not actor.frozen or actor.person:
            # A vehicle the vehicle layer froze the moment it existed is
            # not sent the events again on every new drive. A person is:
            # the vehicle layer freezes position and attitude, not
            # altitude, and a GSX passenger handed over as "already
            # frozen" was launched 2,600-11,800 ft up by its own walk
            # (measured at Madeira, every one of twelve).
            self._freeze(actor, True)

    def remove(self, key: str) -> None:
        actor = self._actors.pop(key, None)
        if actor is not None:
            self._stop_reading(actor)

    def has(self, key: str) -> bool:
        return key in self._actors

    def actor(self, key: str) -> Actor | None:
        return self._actors.get(key)

    def clear(self) -> None:
        for key in list(self._actors):
            self.remove(key)

    # -- the loop ---------------------------------------------------------

    def update(self, now: float | None = None) -> int:
        """One frame: every actor due, nearest first, within the budget.
        Returns how many writes were made."""
        if not self._ready:
            return 0
        began = time.perf_counter()
        now = self.clock() if now is None else now
        viewer = self.viewer()
        due = []
        for actor in list(self._actors.values()):
            if actor.state == "done" or now < actor.next_at:
                continue
            where = self._where(actor, now)
            distance = None
            if where is not None and viewer is not None:
                distance = math.hypot(where[0] - viewer[0], where[1] - viewer[1])
            due.append((distance, actor, where))
        due.sort(key=lambda item: item[0] if item[0] is not None else 0.0)
        made = deferred = 0
        for distance, actor, where in due:
            if made >= WRITES_PER_FRAME:
                deferred += 1
                continue
            sent = actor.sent_position
            made += self._step(actor, where, now)
            hz = rate_for(distance, actor.person)
            period = 1.0 / hz
            # Due one period after it was last due, so a period is not
            # rounded up to whole frames; from now if it fell behind.
            late = now - actor.next_at
            actor.next_at = (actor.next_at + period if 0.0 <= late < period
                             else now + period)
            if actor.person:
                self._window.person(actor.key, distance, hz,
                                    actor.sent_position != sent)
        self.writes += made
        self._window.frame(now, made, deferred, len(self._actors),
                           time.perf_counter() - began)
        return made

    def rates(self, now: float | None = None) -> str:
        """How the loop has run since the last call, for the status log:
        frames a second, the time a frame took, the actors, and what the
        nearest person was asked for and actually got. Starts a new window."""
        now = self.clock() if now is None else now
        window, self._window = self._window, _Window()
        return window.describe(now, self.camera_state, self.viewer() is not None)

    def _where(self, actor: Actor, now: float):
        if actor.state == "sim":
            return actor.seen[:2] if actor.seen else (
                actor.sim_leg[0] if actor.sim_leg else None)
        found = paths.evaluate(actor.path, now)
        return found[:2] if found else None

    def _step(self, actor: Actor, where, now: float) -> int:
        if actor.state == "sim":
            self._check_sim_leg(actor, now)
            return 0
        found = paths.evaluate(actor.path, now)
        if found is None:
            return 0
        x, z, heading, moving = found
        made = 0
        lat, lon = actor.to_latlon(x, z)
        position = (round(lat, POSITION_DIGITS), round(lon, POSITION_DIGITS),
                    round(heading % 360.0, HEADING_DIGITS))
        if position != actor.sent_position:
            self._write(_DEF_POSITION, actor.object_id, position)
            actor.sent_position = position
            made += 1
        channels = list(actor.channels)
        if actor.lift and actor.base_ft is not None:
            rise = keyframed(actor.lift)
            base = actor.base_ft
            channels.append(Channel(b"PLANE ALTITUDE", b"feet",
                                    lambda t: base + rise(t) * FEET_PER_M))
        for channel in channels:
            value = round(float(channel.value(now)), CHANNEL_DIGITS)
            key = (channel.name, channel.unit)
            if actor.sent_channels.get(key) != value:
                self._write(self._channel_def(key), actor.object_id, (value,))
                actor.sent_channels[key] = value
                made += 1
        if not moving and now >= paths.ends_at(actor.path) and not channels:
            actor.state = "done"
        return made

    # -- the simulator's leg, then ours ---------------------------------------

    def _start_sim_leg(self, actor: Actor) -> None:
        self._freeze(actor, False)
        count = len(actor.sim_leg)

        class Waypoint(ctypes.Structure):
            _pack_ = 1
            _fields_ = [("Latitude", ctypes.c_double), ("Longitude", ctypes.c_double),
                        ("Altitude", ctypes.c_double), ("Flags", ctypes.c_uint32),
                        ("ktsSpeed", ctypes.c_double), ("percentThrottle", ctypes.c_double)]

        points = (Waypoint * count)()
        for point, (x, z) in zip(points, actor.sim_leg):
            point.Latitude, point.Longitude = actor.to_latlon(x, z)
            point.Altitude = 0.0
            point.Flags = _WAYPOINT_SPEED | _WAYPOINT_ON_GROUND
            point.ktsSpeed = actor.sim_speed_kt
            point.percentThrottle = 0.0
        try:
            self._dll.SetDataOnSimObject(self._handle, _DEF_WAYPOINTS, actor.object_id,
                                         0, count, ctypes.sizeof(Waypoint),
                                         ctypes.byref(points))
            self._next_request += 1
            request = self._next_request
            self._dll.RequestDataOnSimObject(self._handle, request, _DEF_POSITION,
                                             actor.object_id, _PERIOD_SECOND, 0, 0, 0, 0)
            self._reads[request] = actor.key
        except Exception:
            log.debug("could not hand %s to the simulator", actor.key, exc_info=True)
            actor.sim_leg = []
            self._freeze(actor, True)
            return
        actor.state = "sim"
        actor.sim_since = self.clock()

    def _read_altitude(self, actor: Actor) -> None:
        try:
            self._next_request += 1
            request = self._next_request
            self._dll.RequestDataOnSimObject(self._handle, request, _DEF_ALTITUDE,
                                             actor.object_id, _PERIOD_ONCE, 0, 0, 0, 0)
            self._altitude_reads[request] = actor.key
        except Exception:
            log.debug("could not read where %s stands", actor.key, exc_info=True)

    def on_position(self, request_id: int, lat: float, lon: float,
                    heading: float) -> bool:
        """A reading of an actor the simulator is driving -- or, for one
        asked where it stands, its altitude, its height above the ground and
        the ground's altitude in the same three slots. True if ours."""
        if int(request_id) == _REQ_CAMERA:
            self.camera_state = int(round(lat))
            return True
        key = self._altitude_reads.pop(int(request_id), None)
        if key is not None:
            actor = self._actors.get(key)
            if actor is not None:
                # The ground under it, whatever it was made at: its altitude
                # less how far above the ground it is.
                actor.base_ft = float(lat) - max(0.0, float(lon))
            return True
        key = self._reads.get(int(request_id))
        actor = self._actors.get(key) if key else None
        if actor is None or actor.to_xy is None:
            return key is not None
        x, z = actor.to_xy(lat, lon)
        actor.seen = (x, z, heading)
        return True

    def _check_sim_leg(self, actor: Actor, now: float) -> None:
        end = actor.sim_leg[-1]
        length = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                     for a, b in zip(actor.sim_leg, actor.sim_leg[1:]))
        allowed = SIM_LEG_SLACK * length / max(0.5, actor.sim_speed_kt * 0.5144)
        close = actor.seen is not None and math.hypot(
            actor.seen[0] - end[0], actor.seen[1] - end[1]) <= DOCK_WITHIN_M
        if not close and now - actor.sim_since < allowed:
            return
        self._dock(actor, now)

    def _dock(self, actor: Actor, now: float) -> None:
        """Freeze it where the simulator left it and drive the last metres
        ourselves, to exactly where it belongs."""
        self._stop_reading(actor)
        start = actor.seen or (*actor.sim_leg[-1], 0.0)
        final = actor.final or (*actor.sim_leg[-1], start[2])
        distance = math.hypot(final[0] - start[0], final[1] - start[1])
        duration = max(1.5, distance / DOCK_MS)
        actor.path = paths.drive_in((start[0], start[1]), (final[0], final[1]),
                                    final[2], now, now + duration)
        actor.state = "docking"
        actor.sent_position = None
        self._freeze(actor, True)
        log.debug("motion: %s docks from %.1f m", actor.key, distance)

    def _stop_reading(self, actor: Actor) -> None:
        for request, key in list(self._reads.items()):
            if key == actor.key:
                del self._reads[request]
                try:
                    self._dll.RequestDataOnSimObject(
                        self._handle, request, _DEF_POSITION, actor.object_id,
                        _PERIOD_NEVER, 0, 0, 0, 0)
                except Exception:
                    pass

    # -- the simulator ----------------------------------------------------------

    def _freeze(self, actor: Actor, on: bool) -> None:
        if not self._ready:
            actor.frozen = on
            return
        events = [_EV_FREEZE_LATLON, _EV_FREEZE_ATTITUDE]
        if actor.person:
            events.append(_EV_FREEZE_ALTITUDE)
        try:
            for event in events:
                self._dll.TransmitClientEvent(
                    self._handle, actor.object_id, event, 1 if on else 0,
                    _PRIORITY_HIGHEST, _EVENT_FLAG_GROUPID_IS_PRIORITY)
        except Exception:
            log.debug("could not freeze %s", actor.key, exc_info=True)
        actor.frozen = on

    def _channel_def(self, key: tuple[bytes, bytes]) -> int:
        found = self._channel_defs.get(key)
        if found is None:
            found = _DEF_CHANNEL_BASE + len(self._channel_defs)
            self._dll.AddToDataDefinition(self._handle, found, key[0], key[1],
                                          SIMCONNECT_DATATYPE_FLOAT64, 0, -1)
            self._channel_defs[key] = found
        return found

    def _write(self, definition: int, object_id: int, values: tuple) -> None:
        packed = (ctypes.c_double * len(values))(*values)
        self._dll.SetDataOnSimObject(self._handle, definition, int(object_id), 0, 0,
                                     ctypes.sizeof(packed), ctypes.byref(packed))

    def describe(self) -> str:
        states = {}
        for actor in self._actors.values():
            states[actor.state] = states.get(actor.state, 0) + 1
        return f"motion: {len(self._actors)} actor(s) {states}, {self.writes} write(s)"


    def ours(self, request_id: int) -> bool:
        """Whether a data reading is one this engine asked for."""
        return (int(request_id) == _REQ_CAMERA or int(request_id) in self._reads
                or int(request_id) in self._altitude_reads)


class _Window:
    """What the loop did over one stretch of time, for :meth:`MotionEngine
    .rates`. Plain counters: it runs inside every frame."""

    def __init__(self):
        self.started: float | None = None
        self.frames = self.writes = self.deferred = self.actors = 0
        self.spent = self.worst = 0.0
        self.nearest: tuple[float, str, float] | None = None   # distance, key, Hz
        self.moved: dict[str, int] = {}

    def frame(self, now, writes, deferred, actors, spent) -> None:
        if self.started is None:
            self.started = now
        self.frames += 1
        self.writes += writes
        self.deferred += deferred
        self.actors = actors
        self.spent += spent
        self.worst = max(self.worst, spent)

    def person(self, key, distance, hz, moved) -> None:
        far = distance if distance is not None else 0.0
        if self.nearest is None or far < self.nearest[0]:
            self.nearest = (far, key, hz)
        if moved:
            self.moved[key] = self.moved.get(key, 0) + 1

    def describe(self, now, camera, placed) -> str:
        span = max(1e-6, now - (self.started if self.started is not None else now))
        where = "on the aeroplane" if placed else "viewer unknown, near rate"
        line = (f"actors: {self.frames / span:.1f} Hz loop, "
                f"{1000.0 * self.spent / max(1, self.frames):.2f} ms a frame "
                f"(worst {1000.0 * self.worst:.2f}), {self.actors} actor(s), "
                f"{self.writes / span:.0f} write(s)/s, {self.deferred} put off "
                f"by the budget; camera {'unread' if camera is None else camera} "
                f"({where})")
        if self.nearest is not None:
            far, key, hz = self.nearest
            at = f"{far:.0f} m" if placed else "?"
            line += (f"; nearest person {key} at {at}: {hz:.0f} Hz asked, "
                     f"moved {self.moved.get(key, 0) / span:.1f} times/s")
        return line


__all__ = ["MotionEngine", "Actor", "Channel", "walking", "walking_while",
           "keyframed", "standing", "rate_for", "RATES", "PERSON_RATES",
           "CAMERA_AT_AIRCRAFT", "WRITES_PER_FRAME"]
