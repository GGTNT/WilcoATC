"""Putting the other aeroplanes into the simulator.

The traffic has always existed on the radio. This is what makes it exist out of
the window: each flight is created in the simulator as an AI aircraft and then
moved, every tick, to wherever the injector says it is.

Three things make this less alarming than it sounds.

*It only ever touches what it created.* Every object is remembered by the
callsign that made it, removed when that flight finishes, and removed again on
shutdown. Nothing else in the simulator is written to.

*It fails closed.* SimConnect is reached through ctypes, so a version that
numbers something differently, or a simulator that refuses a request, has to
result in the traffic staying on the radio rather than in an exception reaching
the situation loop. Every call is guarded and a failure switches the whole
thing off rather than retrying forever.

*The aircraft are a model the simulator has.* An AI aircraft needs the title of
an installed aircraft, and the two simulators do not share one: every title
this used to ask for was a Microsoft Flight Simulator 2020 one, and 2024 has
none of them. It answered each request with CREATE_OBJECT_FAILED on a channel
nothing here read, and the panel said the simulator "will not create AI
aircraft" when it would -- it was being asked for aeroplanes it did not have.
Measured against a live 2024 installation: "Airbus A320 Neo Asobo" fails,
"Asobo PassiveAircraft A320 NEO" is created at once. So each type has a list,
the 2024 AI models first and the 2020 titles after them, a refusal is read off
the message pump, and a title refused once is not asked for again.

*It has to be told what it made.* Creating an aeroplane is a request; the
simulator answers on the message pump with the id it gave. Until that answer
arrives the aeroplane exists and cannot be moved, which is harmless because it
was created where it belonged.

*The model is a detail.* An aeroplane is its callsign; which simulator object
draws it may change. Near the pilot it is drawn with the simulator's own
model, which has doors that open; far away with FSLTL's lighter, painted
one. :mod:`wilcoatc.atc.lod` decides which, per aeroplane, from the
distance; this does the swap without a flicker -- the new object is created
on the same spot and heading, given time to load, and only then is the old
one removed -- and tells whoever asked (the doors) which object the
aeroplane is now. Without a level the aeroplane is drawn the way it always
was: FSLTL's painted model when FSLTL is installed, the simulator's own
otherwise.
"""

from __future__ import annotations

import ctypes
import logging
import math
import threading
import time

from ..atc.follow import Follower
from ..atc.lod import DETAILED, LIGHT

log = logging.getLogger(__name__)
lod_log = logging.getLogger("wilcoatc.lod")
motion_log = logging.getLogger("wilcoatc.traffic.motion")

# What to ask the simulator for, by ICAO type, in order. The 2024 AI models
# come first: they are what that simulator draws its own traffic with, they
# are light enough to have a dozen of on a ramp, and every one of them was
# read off a running installation (SimConnect_EnumerateSimObjectsAndLiveries).
# The 2020 titles follow, for the simulator that has those instead. Where
# there is no model of the type itself, the nearest one of the same size.
_AI = "Asobo PassiveAircraft "

TITLES: dict[str, tuple[str, ...]] = {
    "A20N": (_AI + "A320 NEO", "Airbus A320 Neo Asobo"),
    "A319": (_AI + "A319 CEO", "Airbus A320 Neo Asobo"),
    "A320": (_AI + "A320 CEO", "Airbus A320 Neo Asobo"),
    "A321": (_AI + "A321 CEO", "Airbus A320 Neo Asobo"),
    "A21N": (_AI + "A321 NEO", "Airbus A320 Neo Asobo"),
    "A306": (_AI + "A310-300",),
    "A333": (_AI + "A330-300", "Boeing 787-10 Asobo"),
    "A359": (_AI + "A350-900", "Boeing 787-10 Asobo"),
    "A388": (_AI + "A380-800", "Boeing 787-10 Asobo"),
    "B738": (_AI + "B737-800", "Boeing 737 MAX 8 Asobo"),
    "B38M": (_AI + "B737-Max8", "Boeing 737 MAX 8 Asobo"),
    "B37M": (_AI + "B737-Max8", "Boeing 737 MAX 8 Asobo"),
    "B752": (_AI + "B757-200",),
    "B753": (_AI + "B757-300",),
    "B763": (_AI + "B767-300ER",),
    "B77W": (_AI + "B777-300ER",
             "Boeing 777-300ER Asobo",
             "Boeing 787-10 Asobo"),
    "B77L": (_AI + "B777-200LR",
             "Boeing 777-300ER Asobo",
             "Boeing 787-10 Asobo"),
    "B788": (_AI + "B787-08", "Boeing 787-10 Asobo"),
    "B789": (_AI + "B787-09", "Boeing 787-10 Asobo"),
    "B78X": (_AI + "B787-10", "Boeing 787-10 Asobo"),
    "E190": (_AI + "E190", "Boeing 737 MAX 8 Asobo"),
    "E195": (_AI + "E195", "Boeing 737 MAX 8 Asobo"),
    "E75L": (_AI + "E175", "Boeing 737 MAX 8 Asobo"),
    "CRJ9": (_AI + "E175", "Boeing 737 MAX 8 Asobo"),
    "AT76": (_AI + "ATR72-600", "Daher TBM 930 Asobo"),
    "AT3": (_AI + "ATR42-300",),
    "DH8D": (_AI + "DHC-8 400", "Daher TBM 930 Asobo"),
    "C152": (_AI + "C152",
             "Cessna Skyhawk G1000 Asobo",
             "Cessna Skyhawk Asobo"),
    "C172": (_AI + "C172",
             "Cessna Skyhawk G1000 Asobo",
             "Cessna Skyhawk Asobo"),
    "C182": (_AI + "C182", "Cessna Skyhawk G1000 Asobo"),
    "PA28": (_AI + "C172", "Cessna Skyhawk G1000 Asobo"),
    "P28A": (_AI + "C172", "Cessna Skyhawk G1000 Asobo"),
    "PA34": (_AI + "Baron 58",),
    "TB20": (_AI + "Bonanza G36",),
    "BE36": (_AI + "Bonanza G36", "Cessna Skyhawk G1000 Asobo"),
    "DA40": (_AI + "DA40 NG",
             "Diamond DA40 NG Asobo",
             "Cessna Skyhawk G1000 Asobo"),
    "DA42": (_AI + "DA42", "Diamond DA62 Asobo", "Cessna Skyhawk G1000 Asobo"),
    "SR22": (_AI + "SR22", "Cirrus SR22 Asobo", "Cessna Skyhawk G1000 Asobo"),
    "TBM9": (_AI + "TBM930", "Daher TBM 930 Asobo"),
    "PC12": (_AI + "PC12", "Daher TBM 930 Asobo"),
    "C25A": (_AI + "Citation CJ2",),
    # No model of their own: the nearest of the same size, and never an
    # airliner (wilcoatc.atc.fleet flies them at airfields).
    "DR40": (_AI + "C172", "Cessna Skyhawk G1000 Asobo"),
    "BE58": (_AI + "Baron 58",),
    "C56X": (_AI + "Citation CJ2",),
}

# The types in the table that are light aircraft, so that when their own
# model is missing the stand-in is a light aircraft rather than an airliner.
_LIGHT_TYPES = frozenset({"C152", "C172", "C182", "PA28", "P28A", "PA34",
                          "TB20", "BE36", "DA40", "DA42", "SR22", "TBM9",
                          "PC12", "C25A", "DR40", "BE58", "C56X"})

# Anything not in the table gets one of these, by size.
FALLBACK_AIRLINER = _AI + "A320 NEO"
FALLBACK_LIGHT = _AI + "C172"
_FALLBACKS_AIRLINER = (FALLBACK_AIRLINER, "Airbus A320 Neo Asobo")
_FALLBACKS_LIGHT = (FALLBACK_LIGHT, "Cessna Skyhawk G1000 Asobo")

# What is written to each aircraft every tick.
_MOVE = (
    (b"PLANE LATITUDE", b"degrees"),
    (b"PLANE LONGITUDE", b"degrees"),
    (b"PLANE ALTITUDE", b"feet"),
    (b"PLANE HEADING DEGREES TRUE", b"degrees"),
    (b"AIRSPEED TRUE", b"knots"),
    (b"SIM ON GROUND", b"Bool"),
)

_DEF_MOVE = 4711

# What is written to one on the ground: the same, less the altitude. The
# injector's altitude is the airport's published elevation, and the surface
# is not that -- at Madeira the runway is 25 ft below it -- so an aeroplane
# written to it was lifted off the runway it was supposed to be sitting on,
# and the simulator reported it airborne. Created on the ground, the
# simulator puts it on the surface itself; left without an altitude, it
# keeps it there.
#
# Left without an altitude, it keeps *whatever* altitude it had -- which for
# an arrival is the last one it was written in the air. Every landing
# aeroplane rolled out, turned off and taxied to its stand at the height it
# crossed the fence. So the moment one comes down, it is written once in
# full, at the injector's surface altitude, and only after that without.
_MOVE_GROUND = tuple(item for item in _MOVE if item[0] != b"PLANE ALTITUDE")
_DEF_MOVE_GROUND = 4712
_REQUEST_BASE = 4700

# -- drawn every frame (wilcoatc.atc.follow) -------------------------------
# On the ground: position, heading and attitude, the altitude left to the
# simulator, which keeps an aeroplane on its own surface (see _MOVE_GROUND).
# In the air the altitude as well. The speed separately, so a simulator that
# turns it down (a DATA_ERROR names the packet) still has the rest.
_POSE_GROUND = (
    (b"PLANE LATITUDE", b"degrees"),
    (b"PLANE LONGITUDE", b"degrees"),
    (b"PLANE HEADING DEGREES TRUE", b"degrees"),
    (b"PLANE PITCH DEGREES", b"degrees"),
    (b"PLANE BANK DEGREES", b"degrees"),
)
_POSE_AIR = _POSE_GROUND + ((b"PLANE ALTITUDE", b"feet"),)
_VELOCITY = (
    (b"VELOCITY BODY Z", b"feet per second"),
    (b"AIRSPEED TRUE", b"knots"),
)
# Read back once a second: where the simulator has it, and the ground under
# it -- how far it has been moved from where it was written, and how high its
# own reference stands above the runway, which a takeoff needs the moment it
# freezes the altitude.
#
# And whether it is on its wheels. Measured at Innsbruck (18 October), every
# one of ours standing on a stand had its nose wheel off the ground --
# CONTACT POINT IS ON GROUND:0 false, the mains true -- because it was
# written at pitch 0 and each model rests nose down by its own STATIC
# PITCH (the flyable A321 0.40 degrees, FSLTL's A321 1.00, in the same
# sense as PLANE PITCH DEGREES: the pilot's Fenix, resting on its gear,
# read 0.11 against its 0.10). So the static pitch is read here, and the
# three contact points that say whether it worked.
_READ = (
    (b"PLANE LATITUDE", b"degrees"),
    (b"PLANE LONGITUDE", b"degrees"),
    (b"PLANE ALTITUDE", b"feet"),
    (b"GROUND ALTITUDE", b"feet"),
    (b"STATIC PITCH", b"degrees"),
    (b"CONTACT POINT IS ON GROUND:0", b"bool"),
    (b"CONTACT POINT IS ON GROUND:1", b"bool"),
    (b"CONTACT POINT IS ON GROUND:2", b"bool"),
)
#: A wheel still off the ground with the static pitch written: the
#: aeroplane is let down this much per reading, never more than SETTLE_MAX_FT
#: below where the simulator itself put it, and what it took is kept for
#: the model.
SETTLE_STEP_FT = 0.1
SETTLE_MAX_FT = 1.0
#: Readings after the static pitch is first written before the wheels are
#: judged: the write and the reading cross on the wire.
SETTLE_AFTER_READS = 2
_DEF_POSE_GROUND = 4850
_DEF_POSE_AIR = 4851
_DEF_VELOCITY = 4852
_DEF_READ = 4853
_EV_FREEZE_LATLON = 4801
_EV_FREEZE_ATTITUDE = 4802
_EV_FREEZE_ALTITUDE = 4803
_EV_GEAR_UP = 4804
_EV_GEAR_DOWN = 4805
_EVENTS = ((_EV_FREEZE_LATLON, b"FREEZE_LATITUDE_LONGITUDE_SET"),
           (_EV_FREEZE_ATTITUDE, b"FREEZE_ATTITUDE_SET"),
           (_EV_FREEZE_ALTITUDE, b"FREEZE_ALTITUDE_SET"),
           (_EV_GEAR_UP, b"GEAR_UP"),
           (_EV_GEAR_DOWN, b"GEAR_DOWN"))
_READ_REQUEST_BASE = 640000
_PERIOD_SECOND = 4
_PERIOD_NEVER = 0
_RECV_EVENT_FRAME = 7
_RECV_SIMOBJECT_DATA = 8
_PRIORITY_HIGHEST = 1
_EVENT_FLAG_GROUPID_IS_PRIORITY = 0x10
#: How often an aeroplane is written, by its distance from the pilot (metres
#: -> Hz). Beyond the last, FAR_HZ. Thirty-five a second is the rate the
#: vehicles proved smooth at; at ten kilometres nobody sees five.
RATES: tuple[tuple[float, float], ...] = ((3000.0, 35.0), (10000.0, 15.0))
FAR_HZ = 5.0
#: How high an aeroplane's reference stands above the ground on its wheels,
#: feet, until one of that model has been read standing where the simulator
#: put it.
STANDING_HEIGHT_FT = 7.0
#: The difference between the simulator's ground and the model's runway is
#: carried into the air and faded out by this height, and taken from
#: readings in the air only below LOW_FT, where the ground under an
#: arrival is the runway's and not the valley's.
FADE_FT = 1000.0
LOW_FT = 150.0
#: SimConnect's attitude is nose down and left wing down positive (FSUIPC's
#: documentation of the same variables: "up is negative", "right is
#: negative"); the follower's is nose up and right wing down.
PITCH_SIGN = -1.0
BANK_SIGN = -1.0


def rate_for(distance_m: float | None) -> float:
    if distance_m is None:
        return RATES[0][1]
    for within, hz in RATES:
        if distance_m <= within:
            return hz
    return FAR_HZ


# How the simulator says which aeroplane it just made. The number comes from
# the installed wrapper rather than from here, so a version that renumbers the
# messages is still understood; the literal is only what it has always been.
_RECV_ASSIGNED_OBJECT_ID = 12
_RECV_EXCEPTION = 1


def _exception_message_id() -> int:
    try:
        from SimConnect.Enum import SIMCONNECT_RECV_ID

        return int(SIMCONNECT_RECV_ID.SIMCONNECT_RECV_ID_EXCEPTION)
    except Exception:
        return _RECV_EXCEPTION


def _assigned_message_id() -> int:
    try:
        from SimConnect.Enum import SIMCONNECT_RECV_ID

        return int(SIMCONNECT_RECV_ID.SIMCONNECT_RECV_ID_ASSIGNED_OBJECT_ID)
    except Exception:
        return _RECV_ASSIGNED_OBJECT_ID

SIMCONNECT_DATATYPE_FLOAT64 = 4
SIMCONNECT_OBJECT_ID_USER = 0
SIMCONNECT_DATA_SET_FLAG_DEFAULT = 0


class _Received(ctypes.Structure):
    """The header on the front of every message the simulator sends."""

    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("dwVersion", ctypes.c_uint32),
        ("dwID", ctypes.c_uint32),
    ]


class _ObjectData(_Received):
    """SIMCONNECT_RECV_SIMOBJECT_DATA up to its data."""

    _pack_ = 1
    _fields_ = [
        ("dwRequestID", ctypes.c_uint32),
        ("dwObjectID", ctypes.c_uint32),
        ("dwDefineID", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("dwentrynumber", ctypes.c_uint32),
        ("dwoutof", ctypes.c_uint32),
        ("dwDefineCount", ctypes.c_uint32),
    ]


class _Frame(_Received):
    _pack_ = 1
    _fields_ = [
        ("uGroupID", ctypes.c_uint32),
        ("uEventID", ctypes.c_uint32),
        ("dwData", ctypes.c_uint32),
        ("fFrameRate", ctypes.c_float),
        ("fSimSpeed", ctypes.c_float),
    ]


# --------------------------------------------------------------------------
# the message pump, shared
# --------------------------------------------------------------------------
#
# There is one dispatch callback per SimConnect session and more than one
# thing here that needs to read it: this module wants the id of each aeroplane
# it creates, and :mod:`wilcoatc.integrations.simaircraft` wants the aircraft
# the simulator reports. Each installing its own callback in place of the
# wrapper's meant the second silently switched the first off, and the symptom
# of that -- traffic that is created and then never moves -- looks nothing
# like its cause.
#
# So the hook is installed once and readers are added to it. Each is called
# with the raw message and decides for itself whether it is interested; the
# wrapper's own dispatcher is always called afterwards, unchanged.


class _Pump:
    """One dispatch hook on one SimConnect session, with several readers."""

    def __init__(self, simconnect, maker, original):
        self.sim = simconnect
        self.handlers: list = []
        self.original = original
        self.original_c = getattr(simconnect, "my_dispatch_proc_rd", None)

        def dispatch(data, size, context):
            # A reader returning True has consumed the message: it was an
            # answer to a request this program made, which the wrapper has
            # never heard of and prints "Not Handled" about. Anything else
            # goes on to the wrapper untouched.
            taken = False
            for handler in list(self.handlers):
                try:
                    taken = handler(data) is True or taken
                except Exception:
                    log.debug("a message-pump reader raised", exc_info=True)
            if taken:
                return None
            return self.original(data, size, context)

        # Both the python function and the C callback have to outlive this
        # call: dropping either leaves the simulator calling freed memory.
        self.proc = dispatch
        self.proc_c = maker(dispatch)
        simconnect.my_dispatch_proc_rd = self.proc_c


#: One pump per live session, by identity. Sessions are long-lived and few.
_PUMPS: dict = {}


def install_dispatch(simconnect, handler):
    """Have ``handler`` called with every message the simulator sends.

    Returns a token to hand back to :func:`remove_dispatch`, or None if this
    wrapper has no pump to hook -- which is not an error, it is a build
    without the attributes this needs, and the caller does without.
    """
    if simconnect is None or handler is None:
        return None
    original = getattr(simconnect, "my_dispatch_proc", None)
    maker = getattr(getattr(simconnect, "dll", None), "DispatchProc", None)
    if original is None or maker is None:
        return None
    key = id(simconnect)
    pump = _PUMPS.get(key)
    if pump is None:
        try:
            pump = _Pump(simconnect, maker, original)
        except Exception:
            log.info("could not hook the simulator message pump", exc_info=True)
            return None
        _PUMPS[key] = pump
    pump.handlers.append(handler)
    return (key, handler)


# --------------------------------------------------------------------------
# the visual frame, shared
# --------------------------------------------------------------------------
#
# The traffic and the ground vehicles are both moved once a visual frame,
# and each used to subscribe to the simulator's "Frame" event itself. The
# second subscription in one session is refused (SIMCONNECT_EXCEPTION_ERROR
# at startup, every session from the day the traffic began drawing every
# frame), and from then on the vehicles' motion loop never ran: at
# Innsbruck the GSX buses were created and stood at the far end of their
# road, "0 writes so far". So the frame is subscribed once per session and
# handed to everyone who asked for it.

_FRAME_EVENT = 4800
_FRAMES: dict = {}


def subscribe_frames(simconnect, callback) -> bool:
    """Have ``callback()`` called once a visual frame, on the simulator's
    message thread. One subscription per session, however many ask."""
    if simconnect is None or callback is None:
        return False
    key = id(simconnect)
    entry = _FRAMES.get(key)
    if entry is not None and entry["sim"] is not simconnect:
        # A session gone without saying so, its id given to a new one.
        _FRAMES.pop(key, None)
        entry = None
    if entry is None:
        dll = getattr(simconnect, "dll", None)
        handle = getattr(simconnect, "hSimConnect", None)
        subscribe = getattr(dll, "SubscribeToSystemEvent", None)
        if subscribe is None or handle is None:
            return False
        callbacks: list = []

        timing: dict = {}

        def heard(data):
            if data.contents.dwID != _RECV_EVENT_FRAME:
                return False
            frame = ctypes.cast(data, ctypes.POINTER(_Frame)).contents
            if frame.uEventID != _FRAME_EVENT:
                return False
            for one in list(callbacks):
                began = time.perf_counter()
                try:
                    one()
                except Exception:
                    log.debug("a frame reader raised", exc_info=True)
                # How long each reader keeps the others waiting, for the
                # status log (frame_timing): they run one after another.
                spent = time.perf_counter() - began
                name = getattr(getattr(one, "__self__", None), "__class__", type(one)).__name__
                count, total, worst = timing.get(name, (0, 0.0, 0.0))
                timing[name] = (count + 1, total + spent, max(worst, spent))
            return True

        token = install_dispatch(simconnect, heard)
        if token is None:
            return False
        try:
            if subscribe(handle, _FRAME_EVENT, b"Frame") != 0:
                raise OSError("SimConnect refused the frame event")
        except Exception:
            log.info("the simulator will not send frames", exc_info=True)
            remove_dispatch(simconnect, token)
            return False
        entry = _FRAMES[key] = {"callbacks": callbacks, "token": token,
                                "sim": simconnect, "timing": timing}
    if callback not in entry["callbacks"]:
        entry["callbacks"].append(callback)
    return True


def frame_timing(simconnect) -> str:
    """Each frame reader's share of the frame since the last call -- calls,
    mean and worst milliseconds -- and a fresh count from here."""
    entry = _FRAMES.get(id(simconnect))
    if entry is None or not entry["timing"]:
        return "no frames"
    timing = entry["timing"]
    taken = dict(timing)
    timing.clear()
    return ", ".join(f"{name} {count} x {1000.0 * total / max(1, count):.2f} ms "
                     f"(worst {1000.0 * worst:.2f})"
                     for name, (count, total, worst) in taken.items())


def unsubscribe_frames(simconnect, callback) -> None:
    """Stop calling ``callback``; the subscription goes with the last."""
    entry = _FRAMES.get(id(simconnect))
    if entry is None:
        return
    if callback in entry["callbacks"]:
        entry["callbacks"].remove(callback)
    if entry["callbacks"]:
        return
    _FRAMES.pop(id(simconnect), None)
    remove_dispatch(simconnect, entry["token"])
    try:
        simconnect.dll.UnsubscribeFromSystemEvent(simconnect.hSimConnect,
                                                  _FRAME_EVENT)
    except Exception:
        pass


def remove_dispatch(simconnect, token) -> None:
    """Take one reader off the pump, and the pump with the last of them."""
    if not token:
        return
    key, handler = token
    pump = _PUMPS.get(key)
    if pump is None:
        return
    if handler in pump.handlers:
        pump.handlers.remove(handler)
    if pump.handlers:
        return
    try:
        pump.sim.my_dispatch_proc_rd = pump.original_c
    except Exception:
        pass
    _PUMPS.pop(key, None)


class _Exception(_Received):
    """The simulator saying a request failed, by the packet that carried it."""

    _fields_ = [
        ("dwException", ctypes.c_uint32),
        ("dwSendID", ctypes.c_uint32),
        ("dwIndex", ctypes.c_uint32),
    ]


class _AssignedObjectId(_Received):
    """The simulator naming an aeroplane it was asked to create."""

    _fields_ = [
        ("dwRequestID", ctypes.c_uint32),
        ("dwObjectID", ctypes.c_uint32),
    ]


class _OwnInitPosition(ctypes.Structure):
    """SIMCONNECT_DATA_INITPOSITION: where an aeroplane starts life.

    Only used when the SimConnect wrapper does not define the type itself;
    see :func:`init_position_type`.
    """

    _fields_ = [
        ("Latitude", ctypes.c_double),       # degrees
        ("Longitude", ctypes.c_double),      # degrees
        ("Altitude", ctypes.c_double),       # feet
        ("Pitch", ctypes.c_double),          # degrees
        ("Bank", ctypes.c_double),           # degrees
        ("Heading", ctypes.c_double),        # degrees
        ("OnGround", ctypes.c_uint32),       # 1 puts it on the ground
        ("Airspeed", ctypes.c_uint32),       # knots
    ]


def init_position_type():
    """The struct ``AICreateNonATCAircraft`` will actually accept.

    ctypes checks argument types by identity, not by layout. The wrapper
    declares ``argtypes`` for this call using *its own*
    ``SIMCONNECT_DATA_INITPOSITION``, so passing a locally defined structure
    with byte-for-byte the same fields is refused outright:

        argument 4: TypeError: expected SIMCONNECT_DATA_INITPOSITION
        instance instead of _InitPosition

    Which is exactly what happened, on every attempt, for every aeroplane --
    so "traffic will be visible in the simulator" was announced at startup
    and then quietly never worked. The wrapper's own type is used where it
    exists; the local one is the fallback for a build that does not have it.
    """
    try:
        from SimConnect.Enum import SIMCONNECT_DATA_INITPOSITION

        return SIMCONNECT_DATA_INITPOSITION
    except Exception:                                  # pragma: no cover
        return _OwnInitPosition


class _Position(ctypes.Structure):
    _fields_ = [
        ("latitude", ctypes.c_double),
        ("longitude", ctypes.c_double),
        ("altitude", ctypes.c_double),
        ("heading", ctypes.c_double),
        ("airspeed", ctypes.c_double),
        ("on_ground", ctypes.c_double),
    ]


def titles_for(type_code: str, light: bool = False) -> tuple[str, ...]:
    """Every container title worth asking for, best first.

    The type's own models, then the generic one of its size in each
    simulator, so a type with only a 2024 model is still drawn in 2020.
    """
    code = (type_code or "").upper()
    found = TITLES.get(code, ())
    small = light or code in _LIGHT_TYPES
    generic = _FALLBACKS_LIGHT if small else _FALLBACKS_AIRLINER
    return tuple(dict.fromkeys(found + generic))


class _GroundPosition(ctypes.Structure):
    _fields_ = [
        ("latitude", ctypes.c_double),
        ("longitude", ctypes.c_double),
        ("heading", ctypes.c_double),
        ("airspeed", ctypes.c_double),
        ("on_ground", ctypes.c_double),
    ]


def title_for(type_code: str, light: bool = False) -> str:
    """The container title to ask the simulator for first."""
    return titles_for(type_code, light)[0]


class SimTraffic:
    """The other aeroplanes, in the simulator.

    Give it a list of positions -- the shape :meth:`TrafficInjector.positions`
    produces -- and it creates what is new, moves what it already has, and
    removes what has gone.
    """

    def __init__(self, enabled: bool = True, limit: int = 12,
                 settle_s: float = 3.0, smooth: bool = True):
        self.enabled = enabled
        # Drawn every frame from the model's ticks (wilcoatc.atc.follow),
        # frozen so the simulator does not move them in between. Off, or on
        # a simulator that will not freeze or frame them, each is written
        # once a tick as before.
        self.smooth = smooth
        self._smooth_ready = False
        self._followers: dict[str, Follower] = {}
        self._follow_lock = threading.RLock()
        # Per callsign: what was last written, whether it is drawn in the
        # air, the gear, and when it is next due.
        self._drawn: dict[str, dict] = {}
        # The 1 Hz read-back: request -> callsign.
        self._reads: dict[int, str] = {}
        self._read_of: dict[str, int] = {}
        self._next_read = _READ_REQUEST_BASE
        # How high each model's reference stands on its wheels, as read and
        # then let down onto all of them, and the pitch it rests at.
        self._standing_ft: dict[str, float] = {}
        self._static_pitch: dict[str, float] = {}
        self._settled: set[str] = set()
        self._velocity_ok = True
        self._velocity_packets: set[int] = set()
        # Where the pilot is (lat, lon), for the write rate by distance.
        self.viewer: tuple[float, float] | None = None
        # For the line in the log that says how it is going.
        self.frames = 0
        self.frames_skipped = 0
        self.pose_writes = 0
        self._drift_m = 0.0
        self._reported_at = 0.0
        # How long a swapped-in model is given to load before the one it
        # replaces is removed. SimConnect has no "loaded" message; three
        # seconds is what a model takes on an ordinary machine (measured).
        self.settle_s = float(settle_s)
        # A ceiling on how many are injected at once. Every one of them is a
        # drawn aeroplane in somebody's simulator, and a busy field would
        # otherwise put thirty on the ramp.
        self.limit = limit
        self._sim = None
        self._dll = None
        self._handle = None
        self._ready = False
        # The writer itself out of action: never attached, or the attach
        # failed. Not the same thing as ``_failed`` below, the callsigns
        # that could not be drawn, and it used to be the same attribute.
        self._broken = False
        self._objects: dict[str, int] = {}      # callsign -> simulator id
        # The ones the simulator already has on the surface: created there,
        # or put there by a full write when they touched down. See
        # _MOVE_GROUND for why the difference matters.
        self._grounded: set[str] = set()
        self._pending: dict[int, str] = {}      # request id -> callsign
        # When each request went, so one the simulator never answers is
        # given up on rather than waited for forever.
        self._asked_when: dict[int, float] = {}
        # Which packet carried each request. A refusal names the packet, not
        # the request, and this is the only way back from one to the other.
        self._packets: dict[int, int] = {}      # packet id -> request id
        # The title each pending request asked for, how many unanswered
        # attempts each callsign has had, and the titles this simulator has
        # refused -- asked for once, never again.
        self._asked_title: dict[int, str] = {}
        # The title each drawn aeroplane was made with. The door table is
        # matched on it: the same type is an aeroplane with doors as the
        # simulator's own model and a statue as FSLTL's.
        self._titles: dict[str, str] = {}
        # The level each is drawn at ("detailed", "light", or "" for one
        # drawn with no level asked), the swaps asked for and not yet
        # answered (request -> callsign, level, title), and the swaps
        # answered and waiting for the model to load (callsign -> old
        # object, new object, when the old may go, level, title).
        self._levels: dict[str, str] = {}
        self._asked_level: dict[int, str] = {}
        self._swap_pending: dict[int, tuple[str, str, str]] = {}
        self._swapping: dict[str, tuple[int, int, float, str, str]] = {}
        # A level with no model for it is not asked for again every tick.
        self._no_model: set[tuple[str, str]] = set()
        # Overrides: type or "AIRLINE/TYPE" -> {"detailed": ..., "light": ...}.
        self.lod_models: dict = {}
        # Told when an aeroplane changes object: (callsign, old, new, title).
        self.on_swapped = None
        # Told when an object is taken out of the simulator: (object id,
        # what). Whatever is still asking after it -- the doors -- stops.
        self.on_removed = None
        self._tried: dict[str, int] = {}
        self._bad_titles: set[str] = set()
        # The flights that could not be drawn with any title. They are not
        # asked for again, and whoever runs the traffic takes them off the
        # radio: an aeroplane that is heard and not seen is the thing this
        # exists to prevent.
        self._failed: set[str] = set()
        # FSLTL's table, when it is installed: the airline's own livery is
        # asked for before anything else. See wilcoatc.integrations.fsltl.
        self.liveries = None
        # The simulator's own AI models' airline liveries, read off it once
        # it is running (wilcoatc.integrations.ailiveries), and the livery
        # each aeroplane was drawn in. Painted only through the call that
        # takes a livery, which only a 2024 library has.
        self.ai_liveries = None
        self._painted: dict[str, str] = {}
        # And a swap's, from the request to the object it becomes.
        self._swap_livery: dict[int, str] = {}
        self._swap_paint: dict[str, str] = {}
        # The livery each request asked for, for the line that says it failed.
        self._asked_livery: dict[int, str] = {}
        self._create_ex1 = None
        # How many aeroplanes have been asked for, and how many the simulator
        # has actually named. A simulator that refuses to create objects at
        # all answers the request with an exception on a channel nothing here
        # reads, so the only way to notice is that nothing ever comes back.
        self._asked = 0
        self._made = 0
        self._unanswered = 0
        self._refused = False
        self._next_request = _REQUEST_BASE
        # This client's place on the shared message pump.
        self._hook = None

    # -- setting up ---------------------------------------------------

    @property
    def available(self) -> bool:
        # Not "and not self._failed": that is the set of callsigns no model
        # could be found for, and one entry in it made the writer
        # unavailable -- sync() returned at once and every other invented
        # aeroplane stood where it was for the rest of the session.
        return self._ready and not self._broken

    @property
    def count(self) -> int:
        return len(self._objects)

    # How many aeroplanes may be asked for, with none of them appearing,
    # before the simulator is taken to be refusing. Generous, because a
    # single failure is normal -- a title that is not installed, a position
    # inside terrain -- and the thing being detected is a simulator that
    # will not do this at all.
    REFUSALS_BEFORE_GIVING_UP = 6

    @property
    def refused(self) -> bool:
        """Whether the simulator is declining to create aircraft.

        Microsoft Flight Simulator 2024 does this: the call is accepted, and
        the aeroplane never appears. Worth knowing, because the alternative
        is a panel that says traffic will be visible out of the window and
        then simply is not -- and a pilot who spends the flight looking for
        aeroplanes that were never created.
        """
        return self._refused

    def has(self, callsign: str) -> bool:
        """Whether this flight is drawn in the simulator as well as heard."""
        return callsign in self._objects

    def failed(self, callsign: str) -> bool:
        """Whether the simulator could not draw this flight at all."""
        return callsign in self._failed

    def title_of(self, callsign: str) -> str:
        """The simulator title this aeroplane was drawn with, or ''."""
        return self._titles.get(callsign, "")

    def level_of(self, callsign: str) -> str:
        """What this aeroplane is drawn as: "detailed", "light", or '' for
        one drawn without a level asked."""
        return self._levels.get(callsign, "")

    def swapping(self, callsign: str) -> bool:
        """Whether this aeroplane is between two objects right now."""
        return callsign in self._swapping or callsign in {
            c for c, _, _ in self._swap_pending.values()}

    def target_level(self, callsign: str) -> str:
        """The level this aeroplane is on its way to, or already at.

        What a position should be stamped with: stamping a swap in flight
        with the level it is *leaving* read, on the tick the swap finished,
        as an order to swap straight back -- which is how every aeroplane
        at Madeira changed model every four seconds for a whole session.
        """
        found = self._swapping.get(callsign)
        if found is not None:
            return found[3]
        for who, level, _ in self._swap_pending.values():
            if who == callsign:
                return level
        return self._levels.get(callsign, "")

    def has_detailed(self, type_code: str, operator: str = "") -> bool:
        """Whether there is a detailed model to draw this type with."""
        return bool(self._titles_at(DETAILED, type_code, operator,
                                    light=operator == ""))

    def _override(self, type_code: str, operator: str, level: str) -> tuple[str, ...]:
        code = (type_code or "").upper()
        for key in (f"{(operator or '').upper()}/{code}", code):
            entry = self.lod_models.get(key)
            if isinstance(entry, dict) and entry.get(level):
                found = entry[level]
                return tuple(found) if isinstance(found, (list, tuple)) else (str(found),)
        return ()

    def _titles_at(self, level: str, type_code: str, operator: str,
                   light: bool) -> tuple[str, ...]:
        """The titles to ask for, best first, at a level of detail.

        Detailed is the simulator's own model of the type, and nothing if
        it has none: a type with no detailed model stays light. Light is
        FSLTL's painted model first and the simulator's own after it,
        which is also what an aeroplane with no level asked is drawn as:
        LOD switched off changes nothing.
        """
        painted: tuple[str, ...] = ()
        if self.liveries:
            painted = self.liveries.models(operator, type_code)
        own = titles_for(type_code, light=light)
        if level == DETAILED:
            found = self._override(type_code, operator, DETAILED) + TITLES.get(
                (type_code or "").upper(), ())
        elif level == LIGHT:
            found = self._override(type_code, operator, LIGHT) + painted + own
        else:
            found = painted + own
        return tuple(dict.fromkeys(found))

    def _livery_for(self, title: str, operator: str, callsign: str) -> str:
        """The airline's own livery on one of the simulator's AI models,
        or "" -- for a painted FSLTL model, an airline it has none for, or
        a library that cannot ask for one."""
        if (self.ai_liveries is None or self._create_ex1 is None
                or "PassiveAircraft" not in title):
            return ""
        return self.ai_liveries.livery(title, operator, seed=callsign)

    def livery_matches(self, type_code: str, operator: str,
                       callsign: str = "") -> bool:
        """Whether this aeroplane's detailed model is painted the way its
        light one is, so a swap between them changes the model and not the
        colours: the detailed one in the airline's own livery, or no
        painted light model to differ from."""
        detailed = [t for t in self._titles_at(DETAILED, type_code, operator,
                                               light=operator == "")
                    if t not in self._bad_titles]
        if not detailed:
            return True
        if self._livery_for(detailed[0], operator, callsign):
            return True
        light = [t for t in self._titles_at(LIGHT, type_code, operator,
                                            light=operator == "")
                 if t not in self._bad_titles]
        return not light or light[0] == detailed[0]

    def livery_of(self, callsign: str) -> str:
        """The livery this aeroplane was drawn in, or '' for the default."""
        return self._painted.get(callsign, "")

    def _ask_for(self, title: str, livery: str, callsign: str, start,
                 request: int) -> int:
        """One AICreateNonATCAircraft, with the livery where there is one."""
        if livery and self._create_ex1 is not None:
            return self._create_ex1(
                self._handle, title.encode("ascii", "ignore"),
                livery.encode("ascii", "ignore"),
                callsign.encode("ascii", "ignore")[:12], start, request)
        return self._dll.AICreateNonATCAircraft(
            self._handle, title.encode("ascii", "ignore"),
            callsign.encode("ascii", "ignore")[:12], start, request)

    def _find_ex1(self):
        """The 2024 call that takes a livery, from whichever library the
        session is on: the wrapper's own entry point where it has one, the
        raw library underneath it otherwise. None on a 2020 library."""
        found = getattr(self._dll, "AICreateNonATCAircraft_EX1", None)
        if found is not None:
            return found
        raw = getattr(self._dll, "SimConnect", None)
        function = getattr(raw, "SimConnect_AICreateNonATCAircraft_EX1", None)
        if function is None:
            return None
        try:
            function.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                 ctypes.c_char_p, ctypes.c_char_p,
                                 init_position_type(), ctypes.c_uint32]
            function.restype = ctypes.c_long
        except Exception:
            return None
        # Straight to the library underneath the wrapper, so it takes the
        # session's lock itself (wilcoatc.sim.serial).
        from ..sim.serial import locked
        return locked(function)

    def objects_of(self, callsign: str) -> list[int]:
        """Every object drawing this flight right now: the one it is, and
        during a swap the one it is becoming, which is visible from the
        moment it is created and must not stand there at idle."""
        found = []
        current = self._objects.get(callsign)
        if current is not None:
            found.append(current)
        swap = self._swapping.get(callsign)
        if swap is not None and swap[1] not in found:
            found.append(swap[1])
        return found

    def object_id(self, callsign: str) -> int | None:
        """Which object the simulator drew this flight as, if it has."""
        return self._objects.get(callsign)

    def read_position(self, callsign: str) -> tuple | None:
        """Where the simulator last said it has one of ours: (lat, lon,
        altitude), read once a second; None before the first read. What
        traffic --live checks the live traffic against."""
        drawn = self._drawn.get(callsign)
        return drawn.get("read") if drawn else None

    def height_above_ground(self, callsign: str) -> float | None:
        """How high this aeroplane's reference stands off the ground now,
        metres, from the reading once a second; None in the air or before
        the first reading. Its doors hang from it: one A321 read at 3.66 m
        set every A321's stairs and belts 0.36 m low while the one being
        served stood at 4.02 (Bilbao, 17 October)."""
        drawn = self._drawn.get(callsign)
        if drawn is None or drawn.get("air") or "read_agl_ft" not in drawn:
            return None
        return drawn["read_agl_ft"] * 0.3048

    def resting_pitch(self, callsign: str) -> float:
        """The pitch this aeroplane rests at on the ground, degrees, nose
        down positive as the simulator has it; 0 where not yet read."""
        drawn = self._drawn.get(callsign) or {}
        found = drawn.get("static_pitch")
        if found is None:
            found = self._static_pitch.get(self._titles.get(callsign, ""), 0.0)
        return found

    @property
    def drawing(self) -> bool:
        """Whether aeroplanes asked for here can be expected to appear."""
        return self.available and not self._refused

    def attach(self, simconnect) -> bool:
        """Take a live SimConnect session and prepare the data definition."""
        if not self.enabled or simconnect is None:
            return False
        self._sim = simconnect
        self._handle = getattr(simconnect, "hSimConnect", None)
        self._dll = getattr(simconnect, "dll", None)
        if self._handle is None or self._dll is None:
            self._broken = True
            return False
        if not self._listen(simconnect):
            self._broken = True
            return False
        try:
            for definition, items in ((_DEF_MOVE, _MOVE),
                                      (_DEF_MOVE_GROUND, _MOVE_GROUND)):
                for name, unit in items:
                    result = self._dll.AddToDataDefinition(
                        self._handle, definition, name, unit,
                        SIMCONNECT_DATATYPE_FLOAT64, 0, -1)
                    if result != 0:
                        raise OSError(
                            f"SimConnect refused {name!r} ({result})")
        except Exception:
            log.info("the simulator would not accept the traffic definition",
                     exc_info=True)
            # The message pump was hooked a moment ago and is no use now.
            self._stop_listening()
            self._broken = True
            return False
        self._ready = True
        self._smooth_ready = self.smooth and self._prepare_smooth()
        log.info("the traffic is drawn %s", "every frame, frozen between"
                 if self._smooth_ready else "once a tick")
        self._create_ex1 = self._find_ex1()
        log.info("the traffic %s ask for a livery",
                 "can" if self._create_ex1 is not None else "cannot")
        return True

    def _prepare_smooth(self) -> bool:
        """The definitions, events and frame subscription the drawing every
        frame needs. False if any is missing: it is all or nothing."""
        dll = self._dll
        needed = ("MapClientEventToSimEvent", "TransmitClientEvent",
                  "RequestDataOnSimObject")
        if any(getattr(dll, name, None) is None for name in needed):
            return False
        try:
            for definition, items in ((_DEF_POSE_GROUND, _POSE_GROUND),
                                      (_DEF_POSE_AIR, _POSE_AIR),
                                      (_DEF_VELOCITY, _VELOCITY),
                                      (_DEF_READ, _READ)):
                for name, unit in items:
                    if dll.AddToDataDefinition(self._handle, definition, name,
                                               unit, SIMCONNECT_DATATYPE_FLOAT64,
                                               0, -1) != 0:
                        raise OSError(f"SimConnect refused {name!r}")
            for event, name in _EVENTS:
                if dll.MapClientEventToSimEvent(self._handle, event, name) != 0:
                    raise OSError(f"SimConnect refused {name!r}")
            if not subscribe_frames(self._sim, self.on_frame):
                raise OSError("no frames from the simulator")
        except Exception:
            log.info("the traffic cannot be drawn every frame; it is written "
                     "once a tick", exc_info=True)
            return False
        return True

    def _listen(self, simconnect) -> bool:
        """Arrange to be told the id of each aeroplane the simulator creates.

        Through the shared pump rather than by claiming the callback, because
        this is no longer the only reader of it -- see :func:`install_dispatch`.
        """
        wanted = _assigned_message_id()
        refusal = _exception_message_id()

        def heard(data):
            kind = data.contents.dwID
            if kind == _RECV_SIMOBJECT_DATA:
                head = ctypes.cast(data, ctypes.POINTER(_ObjectData)).contents
                if head.dwRequestID not in self._reads:
                    return False
                raw = ctypes.string_at(ctypes.addressof(head),
                                       ctypes.sizeof(_ObjectData) + 8 * len(_READ))
                values = [ctypes.c_double.from_buffer_copy(
                    raw, ctypes.sizeof(_ObjectData) + 8 * i).value
                    for i in range(len(_READ))]
                self.on_read(head.dwRequestID, *values)
                return True
            if kind == refusal:
                failure = ctypes.cast(
                    data, ctypes.POINTER(_Exception)).contents
                if failure.dwSendID in self._velocity_packets:
                    self._velocity_packets.clear()
                    if self._velocity_ok:
                        self._velocity_ok = False
                        motion_log.info(
                            "motion: the simulator will not take an AI "
                            "aeroplane's speed (exception %s); it is drawn "
                            "without it", failure.dwException)
                    return True
                request = self._packets.pop(failure.dwSendID, None)
                if request is None:
                    return False       # somebody else's request
                self.on_refused(request, failure.dwException)
                return True
            if data.contents.dwID != wanted:
                return False
            reply = ctypes.cast(
                data, ctypes.POINTER(_AssignedObjectId)).contents
            if (reply.dwRequestID not in self._pending
                    and reply.dwRequestID not in self._swap_pending):
                return False           # somebody else's aeroplane
            self.on_assigned(reply.dwRequestID, reply.dwObjectID)
            return True

        self._hook = install_dispatch(simconnect, heard)
        return self._hook is not None

    def _stop_listening(self) -> None:
        unsubscribe_frames(self._sim, self.on_frame)
        remove_dispatch(self._sim, self._hook)
        self._hook = None

    def detach(self) -> None:
        """Take every aeroplane back out again."""
        for callsign in list(self._objects):
            self._remove(callsign)
        self._swap_pending.clear()
        self._swapping.clear()
        self._pending.clear()
        self._asked_when.clear()
        self._packets.clear()
        self._asked_title.clear()
        self._stop_listening()
        self._ready = False
        self._sim = self._dll = self._handle = None

    # -- keeping the simulator in step --------------------------------

    # How long a request may go unanswered before it is taken as refused. The
    # answer normally arrives within a frame or two.
    ANSWER_WITHIN_S = 10.0
    # And how many unanswered requests one flight is allowed before it is
    # taken off the radio.
    UNANSWERED_PER_FLIGHT = 2

    def sync(self, positions, now: float | None = None) -> None:
        """Create, move and remove, so the simulator matches the radio."""
        if not self.available:
            return
        now = time.monotonic() if now is None else now
        try:
            wanted = {p["callsign"]: p for p in positions[:self.limit]}
            for callsign in list(self._objects):
                if callsign not in wanted:
                    self._remove(callsign)
            for request, asked in list(self._asked_when.items()):
                if now - asked > self.ANSWER_WITHIN_S:
                    self.on_refused(request, None)
            # A swap whose new model has loaded is finished first, so this
            # tick's position goes to the object the aeroplane now is.
            self._finish_swaps(now)
            waiting = set(self._pending.values())
            if self._smooth_ready:
                with self._follow_lock:
                    for callsign in list(self._followers):
                        if callsign not in wanted:
                            self._followers.pop(callsign, None)
            for callsign, position in wanted.items():
                if callsign in self._objects:
                    if self._smooth_ready:
                        self._follow(callsign, position, now)
                    else:
                        self._move(callsign, position)
                    level = position.get("level") or ""
                    if level and level != self.target_level(callsign):
                        self._reskin(callsign, level, position, now)
                elif (callsign not in waiting and callsign not in self._failed
                      and not self._refused):
                    # Once, and then wait for the answer. Asking again every
                    # tick made a second aeroplane each time the answer took
                    # longer than a tick, and nothing ever removed them.
                    self._create(position, now)
            # Forget the ones that have gone, so a long session does not
            # carry every callsign it ever heard.
            for callsign in list(self._failed):
                if callsign not in wanted:
                    self._failed.discard(callsign)
                    self._tried.pop(callsign, None)
        except Exception:
            self._handle_tick_failure()

    def _handle_tick_failure(self) -> None:
        """A tick that could not keep the traffic in step: the simulator has
        gone, and the writer stops rather than retrying every tick. It used
        to say so by setting ``_failed = True`` -- the set of undrawable
        callsigns, replaced by a boolean, which everything that read the set
        afterwards then raised on."""
        log.info("the traffic could not be kept in step", exc_info=True)
        self._broken = True

    def _create(self, position: dict, now: float = 0.0) -> None:
        """Ask the simulator for one aeroplane, with the best title left."""
        operator = position.get("operator", "")
        light = operator == ""
        callsign = position["callsign"]
        level = position.get("level") or ""
        titles = [t for t in self._titles_at(level, position.get("type", ""),
                                             operator, light)
                  if t not in self._bad_titles]
        if not titles and level == DETAILED:
            # No detailed model of this type: it is drawn light, which is
            # what the decision would have said had it known.
            level = LIGHT
            titles = [t for t in self._titles_at(level, position.get("type", ""),
                                                 operator, light)
                      if t not in self._bad_titles]
        if not titles:
            # Every model this could ask for has been refused by name. That
            # is a simulator with none of them, and nothing will ever appear.
            self._failed.add(callsign)
            if not self._made and not self._refused:
                self._refused = True
                log.info("the simulator has none of the aircraft models; no "
                         "traffic is invented")
            return
        if self._tried.get(callsign, 0) >= self.UNANSWERED_PER_FLIGHT:
            self._failed.add(callsign)
            return
        title = titles[0]

        start = init_position_type()()
        start.Latitude = float(position["lat"])
        start.Longitude = float(position["lon"])
        start.Altitude = float(position["altitude_ft"])
        start.Pitch = (self._static_pitch.get(title, 0.0)
                       if position.get("on_ground") else 0.0)
        start.Bank = 0.0
        start.Heading = float(position["heading"])
        start.OnGround = 1 if position.get("on_ground") else 0
        if position.get("on_ground"):
            self._grounded.add(callsign)
        else:
            self._grounded.discard(callsign)
        start.Airspeed = int(position.get("ground_speed_kt") or 0)

        self._next_request += 1
        request = self._next_request
        livery = self._livery_for(title, operator, callsign)
        result = self._ask_for(title, livery, callsign, start, request)
        if result != 0:
            # Said at INFO, with the title and livery: "no A321 ever
            # appears" is answered by this line or by its absence.
            log.info("the simulator refused to create %s as %r, livery %r "
                     "(result %s)", callsign, title, livery or "default",
                     result)
            self._bad_titles.add(title)
            return
        if livery:
            self._painted[callsign] = livery
        else:
            self._painted.pop(callsign, None)
        # The simulator answers with the object id on the message pump. Until
        # that arrives the aeroplane exists but cannot be moved, which is
        # harmless: it was created where it belongs.
        self._pending[request] = callsign
        self._asked_when[request] = now
        self._asked_title[request] = title
        self._asked_livery[request] = livery
        self._asked_level[request] = level
        packet = self._last_packet()
        if packet is not None:
            self._packets[packet] = request
        self._asked += 1

    # -- changing the model ---------------------------------------------

    def _reskin(self, callsign: str, level: str, position: dict,
                now: float) -> None:
        """Draw this aeroplane with the other model, without a flicker.

        The new object is asked for on the same spot and heading; the old
        one stays until the new one has had time to load. Nothing moves
        while it happens, which is why the decision only swaps an
        aeroplane standing on its stand.
        """
        if (callsign in self._swapping or (callsign, level) in self._no_model
                or callsign in {c for c, _, _ in self._swap_pending.values()}):
            return
        operator = position.get("operator", "")
        titles = [t for t in self._titles_at(level, position.get("type", ""),
                                             operator, light=operator == "")
                  if t not in self._bad_titles]
        if not titles:
            self._no_model.add((callsign, level))
            lod_log.info("lod: no %s model for %s (%s); it stays as it is",
                         level, callsign, position.get("type", ""))
            return
        title = titles[0]
        livery = self._livery_for(title, operator, callsign)
        if (title == self._titles.get(callsign)
                and livery == self._painted.get(callsign, "")):
            # The same model at both levels: nothing to swap, only to note.
            self._levels[callsign] = level
            return
        start = init_position_type()()
        start.Latitude = float(position["lat"])
        start.Longitude = float(position["lon"])
        start.Altitude = float(position["altitude_ft"])
        start.Pitch = (self._static_pitch.get(title, 0.0)
                       if position.get("on_ground") else 0.0)
        start.Bank = 0.0
        start.Heading = float(position["heading"])
        start.OnGround = 1 if position.get("on_ground") else 0
        start.Airspeed = 0
        self._next_request += 1
        request = self._next_request
        result = self._ask_for(title, livery, callsign, start, request)
        if result != 0:
            lod_log.info("lod: the simulator refused %s's %s model %r, livery "
                         "%r (result %s); it stays as it is", callsign, level,
                         title, livery or "default", result)
            self._bad_titles.add(title)
            return
        self._swap_livery[request] = livery
        self._asked_livery[request] = livery
        self._swap_pending[request] = (callsign, level, title)
        self._asked_when[request] = now
        self._asked_title[request] = title
        packet = self._last_packet()
        if packet is not None:
            self._packets[packet] = request
        lod_log.info("lod: %s goes %s, as %r%s", callsign, level, title,
                     f" in {livery}" if livery else "")

    def _finish_swaps(self, now: float) -> None:
        """Remove the old object of every swap whose new one has loaded."""
        for callsign, (old, new, ready_at, level, title) in list(
                self._swapping.items()):
            if ready_at is None:
                # First seen on this clock: the new model has from now.
                ready_at = now + self.settle_s
                self._swapping[callsign] = (old, new, ready_at, level, title)
            if now < ready_at:
                continue
            del self._swapping[callsign]
            self._take_out(old, f"the old model of {callsign}")
            self._objects[callsign] = new
            self._titles[callsign] = title
            self._levels[callsign] = level
            painted = self._swap_paint.pop(callsign, "")
            if painted:
                self._painted[callsign] = painted
            else:
                self._painted.pop(callsign, None)
            lod_log.info("lod: %s is now object %d (%s)", callsign, new, level)
            self._drawn.pop(callsign, None)
            self._start_reading(callsign, new)
            if self.on_swapped is not None:
                try:
                    self.on_swapped(callsign, old, new, title)
                except Exception:
                    log.debug("the swap callback raised", exc_info=True)

    def _last_packet(self) -> int | None:
        """The id of the packet just sent, which is what a refusal names."""
        getter = getattr(self._dll, "GetLastSentPacketID", None)
        if getter is None:
            return None
        try:
            packet = ctypes.c_uint32()
            if getter(self._handle, ctypes.byref(packet)) != 0:
                return None
            return int(packet.value)
        except Exception:
            return None

    def _move(self, callsign: str, position: dict) -> None:
        """Put an aeroplane where the injector says it is."""
        where = _Position()
        where.latitude = float(position["lat"])
        where.longitude = float(position["lon"])
        where.altitude = float(position["altitude_ft"])
        where.heading = float(position["heading"])
        where.airspeed = float(position.get("ground_speed_kt") or 0.0)
        where.on_ground = 1.0 if position.get("on_ground") else 0.0
        if where.on_ground and callsign in self._grounded:
            # On the surface the simulator's ground is right and the
            # published elevation is not; see _MOVE_GROUND.
            flat = _GroundPosition(where.latitude, where.longitude,
                                   where.heading, where.airspeed, 1.0)
            self._dll.SetDataOnSimObject(
                self._handle, _DEF_MOVE_GROUND, self._objects[callsign],
                SIMCONNECT_DATA_SET_FLAG_DEFAULT, 0,
                ctypes.sizeof(_GroundPosition), ctypes.byref(flat))
            return
        self._dll.SetDataOnSimObject(
            self._handle, _DEF_MOVE, self._objects[callsign],
            SIMCONNECT_DATA_SET_FLAG_DEFAULT, 0,
            ctypes.sizeof(_Position), ctypes.byref(where))
        # Touching down is the one ground write that carries an altitude;
        # taking off makes the next one do so again.
        if where.on_ground:
            self._grounded.add(callsign)
        else:
            self._grounded.discard(callsign)

    # -- drawn every frame -------------------------------------------------

    def _follow(self, callsign: str, position: dict, now: float) -> None:
        """The model's latest word on one aeroplane, for the frames."""
        motion = position.get("motion") or {
            "t": now, "lat": position["lat"], "lon": position["lon"],
            "alt": position.get("altitude_ft", 0.0),
            "heading": position.get("heading", 0.0),
            "gs": position.get("ground_speed_kt") or 0.0,
            "vs": position.get("vertical_rate_fpm") or 0.0,
            "on_ground": bool(position.get("on_ground")),
            "arriving": bool(position.get("arriving")),
            "surface_ft": position.get("altitude_ft", 0.0),
        }
        with self._follow_lock:
            follower = self._followers.get(callsign)
            if follower is None:
                self._followers[callsign] = Follower(motion, now)
            else:
                follower.update(motion, now)

    def _event(self, object_id: int, event: int, value: int) -> None:
        try:
            self._dll.TransmitClientEvent(
                self._handle, int(object_id), event, int(value),
                _PRIORITY_HIGHEST, _EVENT_FLAG_GROUPID_IS_PRIORITY)
        except Exception:
            log.debug("could not send event %s to %s", event, object_id,
                      exc_info=True)

    def _hold_still(self, object_id: int, airborne: bool) -> None:
        """Freeze an aeroplane this program made, so only its writes move
        it -- the altitude too, on the ground as in the air. Left to the
        simulator on the ground (as a vehicle's is), a frozen AI aeroplane
        was not put back on the surface: it kept whatever height it last
        had, so an arrival taxied in at the height written at its
        touchdown, in the air (Innsbruck live test). Its height on the
        ground is written from the simulator's own ground (on_read)."""
        if not self._smooth_ready:
            return
        self._event(object_id, _EV_FREEZE_LATLON, 1)
        self._event(object_id, _EV_FREEZE_ATTITUDE, 1)
        self._event(object_id, _EV_FREEZE_ALTITUDE, 1)

    def _start_reading(self, callsign: str, object_id: int) -> None:
        if not self._smooth_ready:
            return
        self._stop_reading(callsign)
        self._next_read += 1
        request = self._next_read
        try:
            self._dll.RequestDataOnSimObject(self._handle, request, _DEF_READ,
                                             int(object_id), _PERIOD_SECOND,
                                             0, 0, 0, 0)
        except Exception:
            log.debug("could not read %s back", callsign, exc_info=True)
            return
        self._reads[request] = callsign
        self._read_of[callsign] = request

    def _stop_reading(self, callsign: str) -> None:
        request = self._read_of.pop(callsign, None)
        if request is None:
            return
        self._reads.pop(request, None)
        object_id = self._objects.get(callsign)
        if object_id is None:
            return
        try:
            self._dll.RequestDataOnSimObject(self._handle, request, _DEF_READ,
                                             int(object_id), _PERIOD_NEVER,
                                             0, 0, 0, 0)
        except Exception:
            pass

    def on_read(self, request_id: int, lat: float, lon: float,
                altitude: float, ground: float,
                static_pitch: float | None = None, *contacts: float) -> None:
        """Where the simulator has one of ours, once a second; its own
        resting pitch, and whether its wheels touch."""
        callsign = self._reads.get(int(request_id))
        if callsign is None:
            return
        drawn = self._drawn.get(callsign)
        if drawn is None:
            return
        sent = drawn.get("sent")
        if sent is not None and not drawn.get("air"):
            # How far the simulator has it from where it was last written.
            # Frozen, this stays within a moving aeroplane's frame step; the
            # line in the log every minute is the evidence either way.
            north = math.radians(lat - sent[0]) * 6_371_008.8
            east = (math.radians(lon - sent[1]) * 6_371_008.8
                    * math.cos(math.radians(lat)))
            self._drift_m = max(self._drift_m, math.hypot(north, east))
        drawn["read_agl_ft"] = altitude - ground
        drawn["read"] = (lat, lon, altitude)
        title = self._titles.get(callsign, "")
        if static_pitch is not None and abs(static_pitch) < 10.0:
            drawn["static_pitch"] = static_pitch
            if title:
                self._static_pitch[title] = static_pitch
        if contacts:
            self._settle(callsign, drawn, title, contacts)
        if "model_ground" not in drawn:
            return
        if not drawn.get("air") and "ground_offset" not in drawn:
            # The first reading of one created on the ground, before any
            # height was written: where the simulator itself put it, which
            # is how high this model stands on its wheels.
            standing = altitude - ground
            title = self._titles.get(callsign, "")
            if title in self._settled:
                # One of this model has been let down onto all its wheels
                # already: where the simulator puts it is that much high.
                drawn["standing"] = self._standing_ft[title]
            elif 0.0 < standing < 40.0:
                drawn["standing"] = standing
                if title:
                    self._standing_ft[title] = standing
        if not drawn.get("air") or drawn.get("agl", 1e9) < LOW_FT:
            # The simulator's ground against the model's, here.
            offset = ground - drawn["model_ground"]
            if abs(offset) < 300.0:
                drawn["ground_offset"] = offset

    def _settle(self, callsign: str, drawn: dict, title: str,
                contacts: tuple) -> None:
        """Let a standing aeroplane down onto all its wheels: once its own
        pitch has been written and its height is ours to write, a wheel
        still off the ground lowers it a step, down to SETTLE_MAX_FT below
        where the simulator put it. The result is kept for the model, so
        the next one of it -- after a swap too -- starts on its gear."""
        sent = drawn.get("sent")
        if drawn.get("air") or not sent or len(sent) < 6 or "standing" not in drawn:
            return
        if sent[3] != self._ground_pitch(callsign, drawn, 0.0):
            return
        drawn["pitched_reads"] = drawn.get("pitched_reads", 0) + 1
        if drawn["pitched_reads"] <= SETTLE_AFTER_READS:
            return
        if all(c >= 0.5 for c in contacts[:3]):
            if not drawn.get("settled"):
                drawn["settled"] = True
                if title:
                    self._settled.add(title)
                    self._standing_ft[title] = drawn["standing"]
                log.info("traffic: %s (%s) stands on all its wheels, its "
                         "reference %.2f ft up at pitch %.2f", callsign, title,
                         drawn["standing"], sent[3])
            return
        first = drawn.setdefault("standing_at_first", drawn["standing"])
        if first - drawn["standing"] >= SETTLE_MAX_FT - 1e-6:
            return
        drawn["standing"] = max(first - SETTLE_MAX_FT,
                                drawn["standing"] - SETTLE_STEP_FT)
        if title:
            self._standing_ft[title] = drawn["standing"]

    def _ground_pitch(self, callsign: str, drawn: dict, moving_pitch: float) -> float:
        """The pitch written on the ground: the model's own at rest plus
        whatever the motion adds, rounded as it is sent."""
        rest = drawn.get("static_pitch")
        if rest is None:
            rest = self._static_pitch.get(self._titles.get(callsign, ""), 0.0)
        return round(rest + PITCH_SIGN * moving_pitch, 2)

    def _standing(self, callsign: str, drawn: dict) -> float:
        return drawn.get("standing") or self._standing_ft.get(
            self._titles.get(callsign, ""), STANDING_HEIGHT_FT)

    def _altitude(self, callsign: str, pose, drawn: dict) -> float | None:
        """The simulator's altitude for the model's: the model's height
        moved onto the simulator's ground, plus how high the aeroplane
        stands on its wheels. None on the ground until the simulator's
        ground has been read once -- until then it stays where it was put."""
        offset = drawn.get("ground_offset")
        standing = self._standing(callsign, drawn)
        if pose.on_ground:
            if offset is None:
                return None
            return pose.alt_ft + offset + standing
        agl = pose.alt_ft - pose.surface_ft
        fade = max(0.0, min(1.0, 1.0 - agl / FADE_FT))
        return pose.alt_ft + standing + (offset or 0.0) * fade

    def on_frame(self, now: float | None = None) -> None:
        """Once a visual frame, on the simulator's message thread: every
        aeroplane due a write, to where its follower has it now."""
        if not (self._smooth_ready and self.available):
            return
        now = time.monotonic() if now is None else now
        self.frames += 1
        # The frame is shared: the ground vehicles and the people are moved
        # by the next reader of this same callback. Waiting here for the
        # engine's tick to let go of the followers would hold every one of
        # them, so a frame the tick is busy in is skipped -- the next is
        # sixteen milliseconds away.
        if not self._follow_lock.acquire(blocking=False):
            self.frames_skipped += 1
            return
        try:
            for callsign, follower in list(self._followers.items()):
                object_id = self._objects.get(callsign)
                if object_id is None:
                    continue
                drawn = self._drawn.setdefault(callsign, {})
                if now < drawn.get("next", 0.0):
                    continue
                try:
                    pose = follower.pose(now)
                    self._draw(callsign, object_id, pose, drawn)
                except Exception:
                    log.debug("could not draw %s", callsign, exc_info=True)
                    drawn["next"] = now + 1.0
                    continue
                distance = None
                if self.viewer is not None:
                    north = math.radians(pose.lat - self.viewer[0]) * 6_371_008.8
                    east = (math.radians(pose.lon - self.viewer[1]) * 6_371_008.8
                            * math.cos(math.radians(pose.lat)))
                    distance = math.hypot(north, east)
                drawn["next"] = now + 1.0 / rate_for(distance)
        finally:
            self._follow_lock.release()
        if now - self._reported_at >= 60.0:
            if self._reported_at and self._followers:
                motion_log.info(
                    "motion: %d aeroplane(s) drawn, %d frame(s), %d write(s) "
                    "in the last minute; the simulator held them within "
                    "%.2f m of where they were written", len(self._followers),
                    self.frames, self.pose_writes, self._drift_m)
            self.frames = self.pose_writes = 0
            self._drift_m = 0.0
            self._reported_at = now

    def _draw(self, callsign: str, object_id: int, pose, drawn: dict) -> None:
        """Write one aeroplane's pose, only what has changed."""
        air = not pose.on_ground
        if air:
            self._grounded.discard(callsign)
        else:
            self._grounded.add(callsign)
        drawn["air"] = air
        drawn["model_ground"] = pose.surface_ft if air else pose.alt_ft
        drawn["agl"] = pose.alt_ft - pose.surface_ft
        pitch = (round(PITCH_SIGN * pose.pitch, 2) if air
                 else self._ground_pitch(callsign, drawn, pose.pitch))
        values = [round(pose.lat, 8), round(pose.lon, 8),
                  round(pose.heading % 360.0, 2),
                  pitch,
                  round(BANK_SIGN * pose.bank, 2)]
        altitude = self._altitude(callsign, pose, drawn)
        if altitude is not None:
            values.append(round(altitude, 1))
        values = tuple(values)
        if values != drawn.get("sent"):
            definition = _DEF_POSE_AIR if len(values) == 6 else _DEF_POSE_GROUND
            packed = (ctypes.c_double * len(values))(*values)
            self._dll.SetDataOnSimObject(self._handle, definition, int(object_id),
                                         SIMCONNECT_DATA_SET_FLAG_DEFAULT, 0,
                                         ctypes.sizeof(packed), ctypes.byref(packed))
            drawn["sent"] = values
            self.pose_writes += 1
        if self._velocity_ok:
            speed = round(pose.gs_kt * 2.0) / 2.0
            if speed != drawn.get("speed"):
                packed = (ctypes.c_double * 2)(speed * 1.6878099, speed)
                self._dll.SetDataOnSimObject(self._handle, _DEF_VELOCITY,
                                             int(object_id),
                                             SIMCONNECT_DATA_SET_FLAG_DEFAULT, 0,
                                             ctypes.sizeof(packed),
                                             ctypes.byref(packed))
                drawn["speed"] = speed
                packet = self._last_packet()
                if packet is not None and len(self._velocity_packets) < 8:
                    self._velocity_packets.add(packet)
        if pose.gear_down != drawn.get("gear", True):
            self._event(object_id, _EV_GEAR_DOWN if pose.gear_down
                        else _EV_GEAR_UP, 0)
        drawn["gear"] = pose.gear_down

    def _remove(self, callsign: str) -> None:
        with self._follow_lock:
            self._followers.pop(callsign, None)
        self._drawn.pop(callsign, None)
        self._stop_reading(callsign)
        self._grounded.discard(callsign)
        self._titles.pop(callsign, None)
        self._levels.pop(callsign, None)
        self._painted.pop(callsign, None)
        self._swap_paint.pop(callsign, None)
        self._no_model = {pair for pair in self._no_model if pair[0] != callsign}
        objects = [self._objects.pop(callsign, None)]
        swap = self._swapping.pop(callsign, None)
        if swap is not None:
            objects.append(swap[1])          # the one that was loading
        for request, (who, _, _) in list(self._swap_pending.items()):
            if who == callsign:
                # Answered later, it is removed then (see on_assigned).
                self._swap_pending[request] = ("", "", "")
        for object_id in objects:
            if object_id is not None:
                self._take_out(object_id, callsign)

    def _take_out(self, object_id: int, what: str) -> None:
        """AIRemoveObject, with its result in the log: a removal that is
        refused is an aeroplane left standing on a stand somebody else is
        about to be put on."""
        try:
            self._next_request += 1
            result = self._dll.AIRemoveObject(self._handle, int(object_id),
                                              self._next_request)
        except Exception:
            log.warning("could not remove object %s (%s)", object_id, what,
                        exc_info=True)
            return
        if result != 0:
            log.warning("the simulator refused to remove object %s (%s): "
                        "result %s", object_id, what, result)
        else:
            lod_log.debug("removed object %s (%s)", object_id, what)
        if self.on_removed is not None:
            try:
                self.on_removed(int(object_id), what)
            except Exception:
                log.debug("the removal callback raised", exc_info=True)

    # -- what the simulator tells us ----------------------------------

    def on_assigned(self, request_id: int, object_id: int) -> None:
        """The simulator has given a created aeroplane its id.

        Called from whatever is pumping SimConnect messages. Until it happens
        the aeroplane sits where it was created, which is where it belongs.
        """
        self._made += 1
        self._refused = False
        request_id = int(request_id)
        title = self._asked_title.get(request_id, "")
        level = self._asked_level.pop(request_id, "")
        self._asked_livery.pop(request_id, None)
        self._forget_request(request_id)
        swap = self._swap_pending.pop(request_id, None)
        livery = self._swap_livery.pop(request_id, "")
        if swap is not None:
            who, level, title = swap
            old = self._objects.get(who) if who else None
            if old is None:
                # The aeroplane went while its new model was loading.
                try:
                    self._next_request += 1
                    self._dll.AIRemoveObject(self._handle, int(object_id),
                                             self._next_request)
                except Exception:
                    log.debug("could not remove an orphan model", exc_info=True)
                return
            # When the old one may go is decided on the caller's clock, at
            # the next sync: this is the simulator's thread, with no clock
            # of the loop's to read.
            self._swapping[who] = (old, int(object_id), None, level, title)
            self._swap_paint[who] = livery
            # Swapped only standing on a stand: frozen there from the start.
            self._hold_still(int(object_id), airborne=False)
            return
        callsign = self._pending.pop(request_id, None)
        if callsign:
            self._objects[callsign] = int(object_id)
            self._titles[callsign] = title
            self._levels[callsign] = level
            self._hold_still(int(object_id),
                             airborne=callsign not in self._grounded)
            self._start_reading(callsign, int(object_id))

    def on_refused(self, request_id: int, exception: int | None) -> None:
        """The simulator would not make this one, or never answered.

        A refusal strikes the title off for the session and the flight is
        asked for again with the next one on the next sync. ``exception`` is
        None for a request that simply went unanswered, which says nothing
        about the title, so that counts against the flight instead.
        """
        request_id = int(request_id)
        title = self._asked_title.get(request_id)
        livery = self._asked_livery.pop(request_id, "") or "default"
        self._asked_level.pop(request_id, None)
        self._forget_request(request_id)
        swap = self._swap_pending.pop(request_id, None)
        self._swap_livery.pop(request_id, None)
        if swap is not None:
            # A swap refused leaves the aeroplane as it was. A title refused
            # by name is struck off; silence is tried again later.
            lod_log.info("lod: %s's new model %r, livery %r, %s; it stays as "
                         "it is", swap[0] or "a departed aeroplane", title,
                         livery, "was refused (exception %s)" % exception
                         if exception is not None else "never came")
            if exception is not None and title:
                self._bad_titles.add(title)
            return
        callsign = self._pending.pop(request_id, None)
        if callsign is None:
            return
        if exception is not None and title:
            log.info("the simulator has no %r, livery %r (exception %s); %s "
                     "is asked for with the next title", title, livery,
                     exception, callsign)
            # A refusal by name is an answer, not silence: a 2020 simulator
            # turns down every 2024 model once on its way to its own, and
            # that is not the simulator refusing to create aircraft.
            self._bad_titles.add(title)
            return
        self._tried[callsign] = self._tried.get(callsign, 0) + 1
        log.info("no answer creating %s as %r, livery %r (%d unanswered)",
                 callsign, title, livery, self._tried[callsign])
        # Silence is what a simulator that will not create anything sounds
        # like. It is judged on requests that went unanswered, not ones
        # still in flight: a busy field asks for a dozen at once, and
        # counting those called a working simulator a refusing one before
        # its first answer had had time to arrive.
        self._unanswered += 1
        if (not self._refused and self._made == 0
                and self._unanswered >= self.REFUSALS_BEFORE_GIVING_UP):
            self._refused = True
            log.info("the simulator is not creating AI aircraft; no traffic "
                     "is invented")

    def _forget_request(self, request_id: int) -> None:
        self._asked_when.pop(request_id, None)
        self._asked_title.pop(request_id, None)
        for packet, request in list(self._packets.items()):
            if request == request_id:
                del self._packets[packet]

    def describe(self) -> str:
        if not self.enabled:
            return "traffic in the simulator: off"
        if not self.available:
            return "traffic in the simulator: not connected"
        if self._refused:
            return ("traffic in the simulator: refused — this simulator will "
                    "not create AI aircraft, so no traffic is invented")
        detailed = sum(1 for level in self._levels.values() if level == DETAILED)
        return (f"traffic in the simulator: {len(self._objects)} flying, "
                f"{len(self._pending)} being created, {detailed} detailed, "
                f"{len(self._swapping)} changing model")


__all__ = ["SimTraffic", "TITLES", "title_for", "titles_for", "FALLBACK_AIRLINER",
           "FALLBACK_LIGHT", "install_dispatch", "remove_dispatch",
           "subscribe_frames", "unsubscribe_frames", "frame_timing"]
