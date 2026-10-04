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
"""

from __future__ import annotations

import ctypes
import logging
import time

log = logging.getLogger(__name__)

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
}

# The types in the table that are light aircraft, so that when their own
# model is missing the stand-in is a light aircraft rather than an airliner.
_LIGHT_TYPES = frozenset({"C152", "C172", "C182", "PA28", "P28A", "PA34",
                          "TB20", "BE36", "DA40", "DA42", "SR22", "TBM9",
                          "PC12", "C25A"})

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

    def __init__(self, enabled: bool = True, limit: int = 12):
        self.enabled = enabled
        # A ceiling on how many are injected at once. Every one of them is a
        # drawn aeroplane in somebody's simulator, and a busy field would
        # otherwise put thirty on the ramp.
        self.limit = limit
        self._sim = None
        self._dll = None
        self._handle = None
        self._ready = False
        self._failed = False
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
        return self._ready and not self._failed

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
            self._failed = True
            return False
        if not self._listen(simconnect):
            self._failed = True
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
            self._failed = True
            return False
        self._ready = True
        return True

    def _listen(self, simconnect) -> bool:
        """Arrange to be told the id of each aeroplane the simulator creates.

        Through the shared pump rather than by claiming the callback, because
        this is no longer the only reader of it -- see :func:`install_dispatch`.
        """
        wanted = _assigned_message_id()
        refusal = _exception_message_id()

        def heard(data):
            if data.contents.dwID == refusal:
                failure = ctypes.cast(
                    data, ctypes.POINTER(_Exception)).contents
                request = self._packets.pop(failure.dwSendID, None)
                if request is None:
                    return False       # somebody else's request
                self.on_refused(request, failure.dwException)
                return True
            if data.contents.dwID != wanted:
                return False
            reply = ctypes.cast(
                data, ctypes.POINTER(_AssignedObjectId)).contents
            if reply.dwRequestID not in self._pending:
                return False           # somebody else's aeroplane
            self.on_assigned(reply.dwRequestID, reply.dwObjectID)
            return True

        self._hook = install_dispatch(simconnect, heard)
        return self._hook is not None

    def _stop_listening(self) -> None:
        remove_dispatch(self._sim, self._hook)
        self._hook = None

    def detach(self) -> None:
        """Take every aeroplane back out again."""
        for callsign in list(self._objects):
            self._remove(callsign)
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
            waiting = set(self._pending.values())
            for callsign, position in wanted.items():
                if callsign in self._objects:
                    self._move(callsign, position)
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
            log.info("the traffic could not be kept in step", exc_info=True)
            self._failed = True

    def _create(self, position: dict, now: float = 0.0) -> None:
        """Ask the simulator for one aeroplane, with the best title left."""
        operator = position.get("operator", "")
        light = operator == ""
        callsign = position["callsign"]
        painted: tuple[str, ...] = ()
        if self.liveries:
            painted = self.liveries.models(operator, position.get("type", ""))
        titles = [t for t in dict.fromkeys(
                      painted + titles_for(position.get("type", ""),
                                           light=light))
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
        start.Pitch = 0.0
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
        result = self._dll.AICreateNonATCAircraft(
            self._handle, title.encode("ascii", "ignore"),
            position["callsign"].encode("ascii", "ignore")[:12],
            start, request)
        if result != 0:
            log.debug("the simulator refused %s as %r (%s)",
                      callsign, title, result)
            self._bad_titles.add(title)
            return
        # The simulator answers with the object id on the message pump. Until
        # that arrives the aeroplane exists but cannot be moved, which is
        # harmless: it was created where it belongs.
        self._pending[request] = callsign
        self._asked_when[request] = now
        self._asked_title[request] = title
        packet = self._last_packet()
        if packet is not None:
            self._packets[packet] = request
        self._asked += 1

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

    def _remove(self, callsign: str) -> None:
        self._grounded.discard(callsign)
        object_id = self._objects.pop(callsign, None)
        if object_id is None:
            return
        try:
            self._next_request += 1
            self._dll.AIRemoveObject(self._handle, object_id,
                                     self._next_request)
        except Exception:
            log.debug("could not remove %s", callsign, exc_info=True)

    # -- what the simulator tells us ----------------------------------

    def on_assigned(self, request_id: int, object_id: int) -> None:
        """The simulator has given a created aeroplane its id.

        Called from whatever is pumping SimConnect messages. Until it happens
        the aeroplane sits where it was created, which is where it belongs.
        """
        self._made += 1
        self._refused = False
        request_id = int(request_id)
        self._forget_request(request_id)
        callsign = self._pending.pop(request_id, None)
        if callsign:
            self._objects[callsign] = int(object_id)

    def on_refused(self, request_id: int, exception: int | None) -> None:
        """The simulator would not make this one, or never answered.

        A refusal strikes the title off for the session and the flight is
        asked for again with the next one on the next sync. ``exception`` is
        None for a request that simply went unanswered, which says nothing
        about the title, so that counts against the flight instead.
        """
        request_id = int(request_id)
        title = self._asked_title.get(request_id)
        self._forget_request(request_id)
        callsign = self._pending.pop(request_id, None)
        if callsign is None:
            return
        if exception is not None and title:
            log.info("the simulator has no %r (exception %s)", title,
                     exception)
            # A refusal by name is an answer, not silence: a 2020 simulator
            # turns down every 2024 model once on its way to its own, and
            # that is not the simulator refusing to create aircraft.
            self._bad_titles.add(title)
            return
        self._tried[callsign] = self._tried.get(callsign, 0) + 1
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
        return (f"traffic in the simulator: {len(self._objects)} flying, "
                f"{len(self._pending)} being created")


__all__ = ["SimTraffic", "TITLES", "title_for", "titles_for", "FALLBACK_AIRLINER",
           "FALLBACK_LIGHT", "install_dispatch", "remove_dispatch"]
