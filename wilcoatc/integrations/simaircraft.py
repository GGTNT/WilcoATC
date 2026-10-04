"""The aeroplanes somebody else put in the simulator.

:mod:`wilcoatc.integrations.aitraffic` is the other direction: it creates
aircraft so that the invented traffic can be seen. This reads the aircraft that
are already there -- FSLTL, AIG, the simulator's own live traffic, anything
that injects AI -- so that the controller works the aeroplanes the pilot can
actually see out of the window.

Why it is worth the trouble
---------------------------

A pilot running FSLTL has a full airport: aeroplanes on the taxiways, a stream
on the approach, somebody lining up in front of them. Before this, the
controller could not see a single one of them. It cleared you to land over the
top of a 737 sitting on the threshold, sequenced you number one behind six
aeroplanes, and put its own invented traffic on the radio alongside a field
that was already full. The frequency and the window were two different
airports.

What it can and cannot do
-------------------------

It can see them and it cannot instruct them. FSLTL flies its aeroplanes from a
real-world schedule and nothing here can tell one to go around. So an observed
aeroplane is treated as weather rather than as a subordinate: it occupies the
runway, it takes its place in the landing sequence, it is the "Airbus on a four
mile final" the user is told to follow -- and what the controller says to it is
a description of what it is already doing, never an instruction it would have
to obey. See :meth:`wilcoatc.atc.traffic.TrafficInjector.set_observed`.

How it reads them
-----------------

``SimConnect_RequestDataOnSimObjectType`` with the AIRCRAFT type and a radius,
which is the one call that enumerates AI aircraft. The answers arrive on the
message pump as separate messages, one per aeroplane, so this hooks the pump
the same way the injector does -- and through the same shared hook, because
two callbacks each replacing the other's is how one of them silently stops
being called.

Everything is guarded and it fails closed. A simulator that will not answer,
a wrapper that numbers its messages differently, an installation with no
SimConnect at all: each of those has to end with the radio behaving exactly as
it did before, not with an exception in the situation loop.
"""

from __future__ import annotations

import ctypes
import logging
import time
from dataclasses import dataclass

from .aitraffic import install_dispatch, remove_dispatch

log = logging.getLogger(__name__)

# The SimConnect object type that means "aircraft", and the message that
# carries an answer to a by-type request. Both are read from the installed
# wrapper where it has them, so a build that renumbers them is still
# understood; the literals are what they have always been.
_SIMOBJECT_TYPE_AIRCRAFT = 2
_RECV_SIMOBJECT_DATA_BYTYPE = 9

def _datatype(name: str, fallback: int) -> int:
    """One of SimConnect's data types, from the installed wrapper.

    Asked rather than written down. The numbers are not guessable and getting
    one wrong is not an error: ``STRING32`` is 6 and ``STRINGV`` is 11, and
    asking for the second while unpacking the first gives fifty aeroplanes
    whose callsigns are right and whose every other field is the tail of the
    previous one. Which is exactly what happened.
    """
    try:
        from SimConnect.Enum import SIMCONNECT_DATATYPE

        return int(getattr(SIMCONNECT_DATATYPE, f"SIMCONNECT_DATATYPE_{name}"))
    except Exception:
        return fallback


SIMCONNECT_DATATYPE_FLOAT64 = _datatype("FLOAT64", 4)
SIMCONNECT_DATATYPE_STRING32 = _datatype("STRING32", 6)
SIMCONNECT_DATATYPE_STRING64 = _datatype("STRING64", 7)

# Our own ids, unique inside this client only.
_DEF_SEEN = 2461
_REQ_SEEN = 2461

#: How far out to look, in metres. Sixty nautical miles: far enough to have the
#: whole approach in it and near enough that a busy region does not answer with
#: four hundred aeroplanes on every poll.
RADIUS_M = 111_000

#: How often to ask. The answer is one message per aeroplane through the
#: message pump, so this is the expensive call in the integration -- and an
#: aeroplane on final moves about a fifth of a mile in two seconds, which is
#: well inside what any of this is used for.
POLL_S = 2.0

#: How long an aeroplane that has stopped being reported is kept. A by-type
#: request answers with whatever the simulator has loaded at that instant, and
#: it drops one for a moment often enough -- an object being reloaded, a
#: livery swapping -- that believing the first absence would make the whole
#: field flicker.
FORGET_S = 12.0

# What to ask about each aeroplane. Strings first because SimConnect packs the
# reply in the order the definition was built, and the reader below unpacks it
# the same way. Both are fixed-width -- a variable-length string would make
# every field after it depend on the length of the one before, and there is no
# way to walk that from a structure.
#
# The model gets the longer field because it is not always a designator: the
# simulator's own aircraft answer with a localisation key, and
# ``TT:ATCCOM.AC_MODEL_B738.0.text`` is thirty characters before anybody has
# added a livery name to it.
# What is asked of every aeroplane the simulator is holding.
#
# The airline and the flight number are asked for alongside the tail number,
# and the reason is that a tail number is not a callsign. An airliner in the
# simulator has all three, and reading only the first made every one of them
# spell its registration on the radio: an El Al 787 into Heathrow called
# itself "four Xray Echo Delta Charlie", which is a thing no airliner has ever
# said. The rebuild is the same one the user's own aeroplane already gets --
# see ``SimConnectSource._derive`` -- so the traffic and the flight deck now
# agree about what an aeroplane is called.
_FIELDS: tuple[tuple[str, str, int], ...] = (
    ("ATC ID", "", SIMCONNECT_DATATYPE_STRING32),
    ("ATC MODEL", "", SIMCONNECT_DATATYPE_STRING64),
    ("PLANE LATITUDE", "degrees", SIMCONNECT_DATATYPE_FLOAT64),
    ("PLANE LONGITUDE", "degrees", SIMCONNECT_DATATYPE_FLOAT64),
    ("PLANE ALTITUDE", "feet", SIMCONNECT_DATATYPE_FLOAT64),
    ("PLANE HEADING DEGREES TRUE", "degrees", SIMCONNECT_DATATYPE_FLOAT64),
    ("GROUND VELOCITY", "knots", SIMCONNECT_DATATYPE_FLOAT64),
    ("VERTICAL SPEED", "feet per minute", SIMCONNECT_DATATYPE_FLOAT64),
    ("SIM ON GROUND", "bool", SIMCONNECT_DATATYPE_FLOAT64),
    # Last, and both strings, so the numeric block above keeps the offsets it
    # has always had. A definition and a structure that disagree read one
    # aeroplane as another aeroplane's longitude, and appending is the change
    # that cannot do that.
    ("ATC AIRLINE", "", SIMCONNECT_DATATYPE_STRING64),
    ("ATC FLIGHT NUMBER", "", SIMCONNECT_DATATYPE_STRING32),
)


class _Header(ctypes.Structure):
    """The header on the front of every message the simulator sends."""

    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("dwVersion", ctypes.c_uint32),
        ("dwID", ctypes.c_uint32),
    ]


class _ByTypeMessage(_Header):
    """SIMCONNECT_RECV_SIMOBJECT_DATA, with the data area spelled out.

    The declaration ends where the C one says ``dwData``, and what follows is
    the definition built from :data:`_FIELDS`, in the order it was built --
    which is the order SimConnect packs the reply in. Change one and the other
    has to change with it; there is no length or name in the reply to check
    against, so a definition and a structure that disagree read one aeroplane
    as another aeroplane's longitude.
    """

    _pack_ = 1
    _fields_ = [
        ("dwRequestID", ctypes.c_uint32),
        ("dwObjectID", ctypes.c_uint32),
        ("dwDefineID", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("dwentrynumber", ctypes.c_uint32),
        ("dwoutof", ctypes.c_uint32),
        ("dwDefineCount", ctypes.c_uint32),
        ("callsign", ctypes.c_char * 32),
        ("model", ctypes.c_char * 64),
        ("latitude", ctypes.c_double),
        ("longitude", ctypes.c_double),
        ("altitude_ft", ctypes.c_double),
        ("heading", ctypes.c_double),
        ("ground_speed_kt", ctypes.c_double),
        ("vertical_speed_fpm", ctypes.c_double),
        ("on_ground", ctypes.c_double),
        ("airline", ctypes.c_char * 64),
        ("flight_number", ctypes.c_char * 32),
    ]


def radio_callsign(registration: str, airline: str, flight_number: str,
                   model: str = "") -> str:
    """What this aeroplane calls itself. See :mod:`wilcoatc.atc.speech`.

    ``model`` is the simulator's ATC MODEL, which is only sometimes a type
    designator; one that is not says nothing, and the airline is believed.
    """
    from ..atc.speech import radio_callsign as build
    from ..atc.traffic import observed_type

    return build(registration, airline, flight_number, observed_type(model))


def _bytype_message_id() -> int:
    try:
        from SimConnect.Enum import SIMCONNECT_RECV_ID

        return int(SIMCONNECT_RECV_ID.SIMCONNECT_RECV_ID_SIMOBJECT_DATA_BYTYPE)
    except Exception:
        return _RECV_SIMOBJECT_DATA_BYTYPE


def _text(raw) -> str:
    """One of the fixed-width strings, without its padding or its rubbish.

    Cut at the first NUL rather than stripped: what follows one in a
    fixed-width field is whatever was in the buffer, and a callsign with the
    tail of somebody else's registration on the end of it is worse than a
    short one. Then only the characters a callsign or a designator can
    actually contain, because that is the whole of what this is read for.
    """
    try:
        text = bytes(raw).split(b"\x00", 1)[0].decode("utf-8", "ignore")
    except Exception:                                # pragma: no cover
        return ""
    return "".join(c for c in text if c.isprintable()).strip()


@dataclass
class Seen:
    """One aeroplane the simulator is holding that this program did not make."""

    object_id: int
    callsign: str
    type_code: str
    latitude: float
    longitude: float
    altitude_ft: float
    heading: float
    ground_speed_kt: float
    vertical_speed_fpm: float
    on_ground: bool
    at: float = 0.0
    # The tail number, kept beside the callsign rather than in place of it.
    # They are the same string for a private aeroplane and different strings
    # for an airliner, and the radio wants the second while the map and the
    # window want the first.
    registration: str = ""

    @property
    def positioned(self) -> bool:
        return self.latitude != 0.0 or self.longitude != 0.0


class SimAircraft:
    """What the simulator has, that this program did not put there.

    Poll it and it returns the aeroplanes it can currently see. Everything
    about it is best effort: with no simulator, no wrapper, or a simulator
    that refuses the request, it reports nothing and the invented traffic
    carries on exactly as before.
    """

    def __init__(self, enabled: bool = True, radius_m: int = RADIUS_M):
        self.enabled = enabled
        self.radius_m = radius_m
        self._sim = None
        self._dll = None
        self._handle = None
        self._ready = False
        self._failed = False
        self._hook = None
        self._asked = 0
        self._answered = 0
        # Everything seen recently, by simulator object id. Kept between polls
        # so that an aeroplane the simulator forgets to mention for one cycle
        # does not vanish off the frequency and come back.
        self._seen: dict[int, Seen] = {}
        self._polled_at = 0.0
        # The user's own aeroplane, which the simulator reports alongside the
        # AI and which must never be treated as traffic.
        self.own_callsign = ""

    # -- setting up ---------------------------------------------------

    @property
    def available(self) -> bool:
        return self._ready and not self._failed

    @property
    def count(self) -> int:
        return len(self._seen)

    def attach(self, simconnect) -> bool:
        """Take a live SimConnect session and build the data definition."""
        if not self.enabled or simconnect is None:
            return False
        self._sim = simconnect
        self._handle = getattr(simconnect, "hSimConnect", None)
        self._dll = getattr(simconnect, "dll", None)
        if self._handle is None or self._dll is None:
            self._failed = True
            return False

        wanted = _bytype_message_id()

        def heard(data):
            if data.contents.dwID != wanted:
                return
            try:
                reply = ctypes.cast(
                    data, ctypes.POINTER(_ByTypeMessage)).contents
            except Exception:
                log.debug("could not read an aircraft reply", exc_info=True)
                return
            if reply.dwRequestID != _REQ_SEEN:
                return
            self.on_aircraft(reply)
            # Ours, and the wrapper has never heard of it: left to pass
            # through, it printed "Event ID: 2461 Not Handled." to the console
            # once per aeroplane per poll, which at a busy field is fifty
            # lines every two seconds.
            return True

        self._hook = install_dispatch(simconnect, heard)
        if self._hook is None:
            self._failed = True
            return False

        try:
            for name, unit, kind in _FIELDS:
                result = self._dll.AddToDataDefinition(
                    self._handle, _DEF_SEEN, name.encode("utf-8"),
                    unit.encode("utf-8"), kind, 0, -1)
                if result != 0:
                    raise OSError(f"SimConnect refused {name!r} ({result})")
        except Exception:
            log.info("the simulator would not accept the traffic definition",
                     exc_info=True)
            self.detach()
            self._failed = True
            return False

        self._ready = True
        return True

    def detach(self) -> None:
        """Stop listening, and forget everything seen."""
        if self._hook is not None:
            remove_dispatch(self._sim, self._hook)
        self._hook = None
        self._seen.clear()
        self._ready = False
        self._sim = self._dll = self._handle = None

    # -- reading them -------------------------------------------------

    def on_aircraft(self, reply) -> None:
        """One aeroplane, off the message pump.

        Called from the simulator's own thread, so it does nothing but record
        what it was told. Anything that has to reason about it happens on the
        situation loop, in :meth:`poll`.
        """
        try:
            registration = _text(reply.callsign)
            callsign = radio_callsign(
                registration,
                _text(getattr(reply, "airline", b"")),
                _text(getattr(reply, "flight_number", b"")),
                _text(getattr(reply, "model", b"")),
            )
            seen = Seen(
                object_id=int(reply.dwObjectID),
                callsign=callsign,
                registration=registration,
                type_code=_text(reply.model),
                latitude=float(reply.latitude),
                longitude=float(reply.longitude),
                altitude_ft=float(reply.altitude_ft),
                heading=float(reply.heading) % 360.0,
                ground_speed_kt=float(reply.ground_speed_kt),
                vertical_speed_fpm=float(reply.vertical_speed_fpm),
                on_ground=bool(reply.on_ground),
                at=time.monotonic(),
            )
        except Exception:
            log.debug("could not understand an aircraft reply", exc_info=True)
            return
        self._answered += 1
        if not seen.positioned:
            return
        # The user is in this list too. An aeroplane being sequenced behind
        # itself is the one mistake this integration could make that would be
        # visible from the cockpit.
        if callsign and callsign.upper() == (self.own_callsign or "").upper():
            return
        self._seen[seen.object_id] = seen

    def request(self) -> bool:
        """Ask the simulator what it is holding. The answers arrive later."""
        if not self.available:
            return False
        try:
            result = self._dll.RequestDataOnSimObjectType(
                self._handle, _REQ_SEEN, _DEF_SEEN, int(self.radius_m),
                _SIMOBJECT_TYPE_AIRCRAFT)
        except Exception:
            log.info("the simulator refused a traffic request", exc_info=True)
            self._failed = True
            return False
        if result != 0:
            self._failed = True
            return False
        self._asked += 1
        return True

    def poll(self, now: float | None = None) -> list[Seen]:
        """The aeroplanes the simulator is holding, as far as it has said.

        Asks again at most every :data:`POLL_S`, and returns what has come
        back since -- which on the first call is nothing, because the answers
        are messages and the pump has not run yet. That is not a failure: the
        next pass has them.
        """
        now = time.monotonic() if now is None else now
        if not self.available:
            return []
        if now - self._polled_at >= POLL_S:
            self._polled_at = now
            self.request()
        stale = [key for key, seen in self._seen.items()
                 if now - seen.at > FORGET_S]
        for key in stale:
            del self._seen[key]
        return list(self._seen.values())

    def describe(self) -> str:
        """One line for the console and the diagnostics."""
        if not self.enabled:
            return "simulator traffic: not being read"
        if not self.available:
            return "simulator traffic: nothing connected"
        if self._asked and not self._answered:
            return ("simulator traffic: asked for, nothing reported -- "
                    "no injector is running, or the simulator will not "
                    "enumerate AI")
        return f"simulator traffic: {len(self._seen)} aircraft"


__all__ = ["SimAircraft", "Seen", "RADIUS_M", "POLL_S", "FORGET_S"]
