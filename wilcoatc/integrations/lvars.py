"""Reading a local variable out of the simulator, and writing one back.

Local variables -- the ``L:`` namespace -- are how add-ons like GSX publish
what they are doing, and they are the one thing plain SimConnect cannot read.
Getting at them needs something inside the simulator to hand them out. Two
things commonly are:

*The MobiFlight WASM module.* A small add-on many people already have for
hardware panels. It exposes a SimConnect *client data* channel: you write the
name of a variable into a command area and it writes the value into a data
area. :class:`MobiFlightBridge` speaks that protocol.

*Anything, through a file.* :class:`FileBridge` reads a small JSON object of
name to number, refreshed as often as whatever writes it likes. It exists
because it cannot fail in an interesting way, and because it lets somebody with
FSUIPC, a Lua script or a bespoke gauge feed this program in five lines.

Both are optional and both fail quietly. A bridge that cannot connect reports
that it has nothing, and the caller carries on with what the aeroplane itself
says.

Writing is the same channel in the other direction, and only MobiFlight has
it: the module takes a line of the simulator's own calculator code, so setting
a variable is ``1 (>L:NAME)`` handed over as a command. A file cannot do it --
this program reads that file and does not own it -- so :attr:`Bridge.can_write`
exists to be asked rather than discovered by a write that quietly does
nothing. It is what lets :mod:`wilcoatc.integrations.gsx` ask for a service
through GSX's own menu where it can, and fall back to the simulator's
pushback where it cannot.

What the module actually sends back
-----------------------------------

This used to be written as though registering a variable were enough, and it
is not: the values arrive as *messages*, and nothing was reading them. The
symptom was the worst kind -- a bridge that connected, reported itself
available, and answered every read with zero, which is indistinguishable from
a stand where nothing is happening. Three things were wrong and all three had
to be right before a single value could arrive.

*Somebody has to pump the messages.* Client data is delivered on the dispatch
queue like everything else in SimConnect. This opens its own connection and
runs its own pump, rather than borrowing the one flying the aeroplane: the
wrapper that owns that one does not know what a client data message is and
drops it, and a slow answer here must not stall the situation loop.

*The values are floats, not doubles, and one definition each.* The module
writes each variable as a four-byte float at its own offset, and a client that
declares one eight-byte block reads the first two variables as one number and
everything after it as rubbish.

*Two clients must not share a channel.* The module lets a client ask for its
own set of areas by name, and this asks. Without that, two programs reading
local variables at once overwrite each other's list of them -- and what that
looks like from here is variables that read zero for no reason.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import json
import logging
import struct
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


class Bridge:
    """Somewhere local variables can be read from."""

    name = "none"
    #: Why there is nothing here, in words, when a way in was tried and
    #: failed. "GSX does nothing" has four causes and only one of them is
    #: the module everybody is told to install, so the diagnostic says which
    #: rather than always naming the commonest.
    trouble = ""

    @property
    def available(self) -> bool:
        return False

    @property
    def can_write(self) -> bool:
        """Whether :meth:`write` does anything. Asked before it is used."""
        return False

    def read(self, names) -> dict:
        """The current value of each name, as far as it is known."""
        return {}

    def write(self, name: str, value: float) -> bool:
        """Set one local variable. False when this bridge cannot."""
        return False

    def close(self) -> None:
        pass


class FileBridge(Bridge):
    """Local variables from a JSON file somebody else keeps up to date.

    The file is a flat object, and anything that can write one can drive this::

        {"FSDT_GSX_DEPARTURE_STATE": 4, "FSDT_GSX_BOARDING_STATE": 5}

    It is re-read when its modification time changes, so writing it every
    second costs nothing here.

    One-way. Whatever keeps the file is the thing talking to the simulator;
    writing a number into somebody else's file would reach nothing, so
    :attr:`can_write` stays false and the caller uses another way.
    """

    name = "file"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._stamp = 0.0
        self._values: dict[str, float] = {}

    @property
    def available(self) -> bool:
        return self.path.exists()

    def read(self, names=None) -> dict:
        try:
            stamp = self.path.stat().st_mtime
        except OSError:
            return {}
        if stamp != self._stamp:
            self._stamp = stamp
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                log.debug("could not read %s", self.path, exc_info=True)
                return self._values
            if isinstance(loaded, dict):
                self._values = {
                    str(k): float(v) for k, v in loaded.items()
                    if isinstance(v, (int, float))
                }
        if names is None:
            return dict(self._values)
        return {n: self._values[n] for n in names if n in self._values}


# --------------------------------------------------------------------------
# the MobiFlight WASM module
# --------------------------------------------------------------------------
#
# The protocol is three SimConnect client data areas with fixed names. The
# client writes a command string into one, the module answers in another, and
# the value of each registered variable arrives in the third as a four-byte
# float at the offset it was registered at.

MF_COMMAND = "MobiFlight.Command"
MF_RESPONSE = "MobiFlight.Response"
MF_LVARS = "MobiFlight.LVars"

MESSAGE_SIZE = 1024
MAX_VARS = 290                      # what the module allocates room for
VALUE_SIZE = 4                      # a float, which is what the module writes
NUL = b"\x00"                       # what a string in a fixed-width area ends at

# Our own ids. They only have to be unique inside this client, and the second
# set is for the channel the module makes when a client asks for one of its
# own.
_AREA_COMMAND, _AREA_RESPONSE, _AREA_LVARS = 1971, 1972, 1973
_AREA_MINE = 1981                   # +0 command, +1 response, +2 lvars
_DEF_COMMAND, _DEF_RESPONSE = 1971, 1972
_DEF_LVAR_BASE = 2000               # one definition per variable, plus index
_REQ_RESPONSE = 1971
_REQ_LVAR_BASE = 2000               # likewise, so a message names its variable
# A second set, for the private channel. Not the same numbers: adding to a
# definition that already exists makes it longer rather than replacing it, so
# a channel claimed twice under one id describes a block of two thousand
# bytes and every message against it is then the wrong shape.
_DEF_COMMAND_MINE, _DEF_RESPONSE_MINE = 1981, 1982
_REQ_RESPONSE_MINE = 1982

# How far into a client data message the payload starts: the three words of
# the header and the seven the message itself carries.
_PAYLOAD_AT = 40

SIMCONNECT_CLIENTDATA_PERIOD_ON_SET = 3
SIMCONNECT_CLIENT_DATA_REQUEST_FLAG_CHANGED = 1

# The messages we care about, from the SDK header.
_RECV_EXCEPTION = 1
_RECV_QUIT = 3
_RECV_CLIENT_DATA = 16

# Where a SimConnect library is usually found. The one bundled with the Python
# wrapper is enough for this -- client data has been in SimConnect since FSX,
# unlike the facility API -- so it is tried as well as the SDK's.
_SEARCH: tuple[str, ...] = (
    r"C:\MSFS 2024 SDK\SimConnect SDK\lib\SimConnect.dll",
    r"C:\MSFS SDK\SimConnect SDK\lib\SimConnect.dll",
    r"C:\MSFS2024 SDK\SimConnect SDK\lib\SimConnect.dll",
    r"C:\Program Files\MSFS SDK\SimConnect SDK\lib\SimConnect.dll",
)

# The call that decides whether a library is any use here.
_NEEDS = "SimConnect_MapClientDataNameToID"


class _Recv(ctypes.Structure):
    """The header on the front of every message the simulator sends."""

    _fields_ = [("dwSize", w.DWORD), ("dwVersion", w.DWORD),
                ("dwID", w.DWORD)]


class _ClientData(_Recv):
    """SIMCONNECT_RECV_CLIENT_DATA, with the payload spelled out.

    The declaration ends where the C one says ``dwData``. What follows is
    whatever the definition this message answers asked for: four bytes for a
    variable's value, a whole message buffer for a response string.
    """

    _pack_ = 1
    _fields_ = [
        ("dwRequestID", w.DWORD),
        ("dwObjectID", w.DWORD),
        ("dwDefineID", w.DWORD),
        ("dwFlags", w.DWORD),
        ("dwentrynumber", w.DWORD),
        ("dwoutof", w.DWORD),
        ("dwDefineCount", w.DWORD),
        ("data", ctypes.c_ubyte * MESSAGE_SIZE),
    ]


def find_simconnect(extra: str = "") -> Path | None:
    """A SimConnect library that can carry client data, if there is one.

    The wrapper's own bundled copy counts, and is tried last so that an SDK
    install -- which is newer and is what the simulator itself uses -- wins
    where a pilot has one.
    """
    candidates = [extra] if extra else []
    candidates += list(_SEARCH)
    try:
        import SimConnect as _wrapper

        here = Path(_wrapper.__file__).parent
        candidates += [str(here / "SimConnect.dll"),
                       str(here / "SimConnectDLL" / "SimConnect.dll")]
    except Exception:
        pass

    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        import os

        path = Path(os.path.expandvars(candidate))
        if not path.is_file():
            continue
        try:
            library = ctypes.WinDLL(str(path))
            getattr(library, _NEEDS)
        except (OSError, AttributeError):
            continue
        return path
    return None


def _declare(dll) -> None:
    """Spell out the argument types of every call this uses.

    Not optional. ``fEpsilon`` is a float, and a float passed to an
    undeclared ctypes function on this platform goes in the wrong register --
    so the definition is built with an epsilon of whatever happened to be
    there, and a value that changes by less than it never arrives.
    """
    dll.SimConnect_Open.argtypes = [
        ctypes.POINTER(w.HANDLE), ctypes.c_char_p, w.HWND, w.DWORD,
        w.HANDLE, w.DWORD]
    dll.SimConnect_Close.argtypes = [w.HANDLE]
    dll.SimConnect_MapClientDataNameToID.argtypes = [
        w.HANDLE, ctypes.c_char_p, w.DWORD]
    dll.SimConnect_AddToClientDataDefinition.argtypes = [
        w.HANDLE, w.DWORD, w.DWORD, w.DWORD, ctypes.c_float, w.DWORD]
    dll.SimConnect_RequestClientData.argtypes = [
        w.HANDLE, w.DWORD, w.DWORD, w.DWORD, ctypes.c_int, w.DWORD,
        w.DWORD, w.DWORD, w.DWORD]
    dll.SimConnect_SetClientData.argtypes = [
        w.HANDLE, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD,
        ctypes.c_void_p]
    dll.SimConnect_GetNextDispatch.argtypes = [
        w.HANDLE, ctypes.POINTER(ctypes.POINTER(_Recv)),
        ctypes.POINTER(w.DWORD)]


class MobiFlightBridge(Bridge):
    """Local variables through the MobiFlight WASM module.

    Everything here is best effort. The module may not be installed, may be a
    version that names its areas differently, or may simply not answer; in
    every one of those cases this reports itself unavailable and the caller
    uses something else. It never raises into the situation loop.
    """

    name = "MobiFlight"

    # How long the module is given to answer the request for a channel of our
    # own. It is a message round trip inside one machine; a second is already
    # generous, and failing it is not fatal -- the shared channel still works,
    # it is only shared.
    CLIENT_WAIT_S = 1.5
    # How often the pump looks for a message. Values arrive when they change,
    # and a tug is not a fast-moving thing.
    PUMP_S = 0.05

    def __init__(self, client: str = "WilcoATC", dll_path: str = "",
                 dll=None, handle=None):
        self.client = "".join(c for c in (client or "WilcoATC")
                              if c.isalnum() or c in "_-") or "WilcoATC"
        self.dll_path = dll_path
        self._dll = dll
        self._handle = handle
        self._own_connection = dll is None
        self._ready = False
        self._failed = False
        self.trouble = ""
        self._stop = threading.Event()
        self._pump: threading.Thread | None = None
        self._lock = threading.Lock()
        # The variables asked for, in the order they were asked for, which is
        # the order the module writes them in.
        self._registered: list[str] = []
        self._values: dict[str, float] = {}
        # Which set of areas is in use: ours if the module gave us one, and
        # the shared one otherwise.
        self._area_command = _AREA_COMMAND
        self._area_lvars = _AREA_LVARS
        self._def_command = _DEF_COMMAND
        self._answers: list[str] = []

    # -- setting up ---------------------------------------------------

    def open(self) -> bool:
        """Connect, claim the areas, and ask for a channel of our own."""
        if self._ready:
            return True
        if self._failed:
            return False
        try:
            if self._own_connection:
                found = find_simconnect(self.dll_path)
                if found is None:
                    log.info("no SimConnect library for the local variables")
                    self.trouble = "no SimConnect library was found"
                    self._failed = True
                    return False
                self._dll = ctypes.WinDLL(str(found))
                _declare(self._dll)
                handle = w.HANDLE()
                if self._dll.SimConnect_Open(
                        ctypes.byref(handle),
                        f"WilcoATC {self.client}".encode("ascii", "ignore"),
                        None, 0, None, 0) != 0:
                    # The commonest answer by a mile, and it is not a fault:
                    # there is nothing to connect to yet.
                    self.trouble = "the simulator is not running"
                    self._failed = True
                    return False
                self._handle = handle
            if not self._claim(MF_COMMAND, MF_RESPONSE, MF_LVARS,
                               _AREA_COMMAND, _AREA_RESPONSE, _AREA_LVARS):
                self.trouble = ("the simulator would not open a client data "
                                "channel")
                self._shut()
                self._failed = True
                return False
        except Exception as exc:
            log.info("the MobiFlight bridge could not start", exc_info=True)
            self.trouble = f"the channel could not be opened ({exc})"
            self._shut()
            self._failed = True
            return False

        self._ready = True
        self._start_pump()
        self._ask_for_a_channel()
        return True

    def connect(self, simconnect_handle=None, dll=None) -> bool:
        """Kept for the caller that used to hand over the flying session.

        The session is no longer borrowed -- see the module docstring -- so
        both arguments are ignored and this is :meth:`open` by another name.
        """
        return self.open()

    def _claim(self, command: str, response: str, lvars: str,
               area_command: int, area_response: int, area_lvars: int,
               def_command: int = _DEF_COMMAND,
               def_response: int = _DEF_RESPONSE,
               req_response: int = _REQ_RESPONSE) -> bool:
        """Map one set of three areas, and subscribe to the answers."""
        dll, handle = self._dll, self._handle
        ok = 0
        for name, area in ((command, area_command), (response, area_response),
                           (lvars, area_lvars)):
            ok |= dll.SimConnect_MapClientDataNameToID(
                handle, name.encode("ascii"), area)
        # The command and the response are one message-sized block each.
        ok |= dll.SimConnect_AddToClientDataDefinition(
            handle, def_command, 0, MESSAGE_SIZE, 0.0, 0)
        ok |= dll.SimConnect_AddToClientDataDefinition(
            handle, def_response, 0, MESSAGE_SIZE, 0.0, 0)
        if ok != 0:
            log.debug("SimConnect refused a client data call (%s)", ok)
            return False
        # Hear what the module says back. Without this the handshake below
        # can only ever time out.
        try:
            dll.SimConnect_RequestClientData(
                handle, area_response, req_response, def_response,
                SIMCONNECT_CLIENTDATA_PERIOD_ON_SET, 0, 0, 0, 0)
        except Exception:
            log.debug("could not subscribe to the module's answers",
                      exc_info=True)
            return False
        self._area_command = area_command
        self._area_lvars = area_lvars
        self._def_command = def_command
        return True

    def _ask_for_a_channel(self) -> None:
        """Ask the module for a set of areas nobody else is writing to.

        Where the module is old enough not to answer, the shared channel is
        kept: sharing it works, it is only fragile if something else is doing
        the same thing at the same time.
        """
        self._answers.clear()
        if not self._send(f"MF.Clients.Add.{self.client}"):
            return
        wanted = f"{self.client}.Finished"
        deadline = time.monotonic() + self.CLIENT_WAIT_S
        while time.monotonic() < deadline:
            if any(wanted in answer for answer in list(self._answers)):
                break
            time.sleep(self.PUMP_S)
        else:
            log.info("the MobiFlight module kept us on the shared channel")
            return
        if self._claim(f"{self.client}.Command", f"{self.client}.Response",
                       f"{self.client}.LVars",
                       _AREA_MINE, _AREA_MINE + 1, _AREA_MINE + 2,
                       _DEF_COMMAND_MINE, _DEF_RESPONSE_MINE,
                       _REQ_RESPONSE_MINE):
            log.info("MobiFlight gave us our own channel")

    @property
    def available(self) -> bool:
        return self._ready and not self._failed

    # -- the pump -----------------------------------------------------

    def _start_pump(self) -> None:
        if self._pump is not None:
            return
        self._stop.clear()
        self._pump = threading.Thread(target=self._run, name="lvars",
                                      daemon=True)
        self._pump.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                while self.pump_once():
                    pass
            except Exception:
                log.debug("the local variable pump raised", exc_info=True)
            self._stop.wait(self.PUMP_S)

    def pump_once(self) -> bool:
        """Take one message off the queue. True if there was one.

        Public because it is the whole of what a test needs to drive: a fake
        library that hands back one message and a bridge that turns it into a
        number is the entire contract with the module.
        """
        if self._dll is None or self._handle is None:
            return False
        data = ctypes.POINTER(_Recv)()
        size = w.DWORD()
        if self._dll.SimConnect_GetNextDispatch(
                self._handle, ctypes.byref(data), ctypes.byref(size)) != 0:
            return False
        if not data:
            return False
        which = data.contents.dwID
        if which == _RECV_CLIENT_DATA:
            self._heard(ctypes.cast(data,
                                    ctypes.POINTER(_ClientData)).contents)
        elif which == _RECV_QUIT:
            self._ready = False
        elif which == _RECV_EXCEPTION:
            # Not fatal and not silent. The commonest one is a definition the
            # module has never heard of, which is a name in the config rather
            # than a fault here.
            log.debug("SimConnect refused something on the lvar channel")
        return True

    def _heard(self, message) -> None:
        """One client data message: a variable's value, or an answer.

        Never reads further into the message than the message goes. The
        structure declares a payload big enough for a whole command string,
        because the response area is that size -- but a four-byte value
        arrives in a message forty-four bytes long, and reading a kilobyte
        out of it would be reading somebody else's memory. So the header's
        own length is what bounds every read here.
        """
        carried = max(0, int(message.dwSize) - _PAYLOAD_AT)
        request = int(message.dwRequestID)
        if request in (_REQ_RESPONSE, _REQ_RESPONSE_MINE):
            raw = bytes(message.data[:min(carried, MESSAGE_SIZE)])
            answer = raw.split(NUL, 1)[0].decode("ascii", "ignore").strip()
            if answer:
                self._answers.append(answer)
                del self._answers[:-16]
            return
        if carried < VALUE_SIZE:
            return
        index = request - _REQ_LVAR_BASE
        value = struct.unpack("<f", bytes(message.data[:VALUE_SIZE]))[0]
        with self._lock:
            if not (0 <= index < len(self._registered)):
                return
            self._values[self._registered[index]] = float(value)

    # -- talking to it ------------------------------------------------

    def _send(self, command: str) -> bool:
        if self._dll is None or self._handle is None:
            return False
        buffer = ctypes.create_string_buffer(
            command.encode("ascii", "ignore")[:MESSAGE_SIZE - 1], MESSAGE_SIZE)
        try:
            result = self._dll.SimConnect_SetClientData(
                self._handle, self._area_command, self._def_command, 0, 0,
                MESSAGE_SIZE, ctypes.cast(buffer, ctypes.c_void_p))
            return result == 0
        except Exception:
            log.debug("could not send %r", command, exc_info=True)
            self._failed = True
            return False

    def register(self, names) -> bool:
        """Ask the module to publish these variables.

        Each gets its own definition, at its own offset, subscribed
        separately -- so a message says which variable it carries and a
        variable that never changes again keeps the last value it had.
        """
        if not self.open():
            return False
        with self._lock:
            wanted = [n for n in names
                      if n not in self._registered
                      and len(self._registered) < MAX_VARS]
            start = len(self._registered)
            self._registered.extend(wanted)
        if not wanted:
            return True

        dll, handle = self._dll, self._handle
        for offset, name in enumerate(wanted, start=start):
            try:
                if dll.SimConnect_AddToClientDataDefinition(
                        handle, _DEF_LVAR_BASE + offset, offset * VALUE_SIZE,
                        VALUE_SIZE, 0.0, 0) != 0:
                    raise OSError("the definition was refused")
                dll.SimConnect_RequestClientData(
                    handle, self._area_lvars, _REQ_LVAR_BASE + offset,
                    _DEF_LVAR_BASE + offset,
                    SIMCONNECT_CLIENTDATA_PERIOD_ON_SET,
                    SIMCONNECT_CLIENT_DATA_REQUEST_FLAG_CHANGED, 0, 0, 0)
            except Exception:
                log.debug("could not subscribe to %s", name, exc_info=True)
                self._failed = True
                return False
            # The module is told last, so that the subscription is already in
            # place when the first value is written.
            if not self._send(f"MF.SimVars.Add.(L:{name})"):
                return False
        return True

    @property
    def can_write(self) -> bool:
        return self.available

    def write(self, name: str, value: float) -> bool:
        """Set a local variable, through the module's calculator code.

        The module has no "set variable" command. What it has is a command
        that runs a line of the simulator's own calculator code, and setting
        a variable is one of those -- ``1 (>L:FSDT_GSX_MENU_OPEN)``. An
        integral value is sent without its decimal point, because a GSX menu
        choice is an index and ``2.0`` is not one.
        """
        if not self.open():
            return False
        number = float(value)
        literal = str(int(number)) if number.is_integer() else repr(number)
        return self._send(f"MF.SimVars.Set.{literal} (>L:{name})")

    def read(self, names=None) -> dict:
        """Whatever the module has published, by name.

        Only the variables that have actually arrived. A name that was asked
        for and has never been sent is left out rather than reported as zero:
        the caller reads a missing name as "nothing known", and a zero as
        "GSX says there is nothing at this stand", and those are opposite
        answers.
        """
        if names:
            self.register(names)
        if not self.available:
            return {}
        with self._lock:
            found = dict(self._values)
        if names is not None:
            wanted = set(names)
            found = {n: v for n, v in found.items() if n in wanted}
        return found

    # -- shutting down ------------------------------------------------

    def _shut(self) -> None:
        if self._own_connection and self._dll is not None and self._handle:
            try:
                self._dll.SimConnect_Close(self._handle)
            except Exception:
                pass
        self._handle = None

    def close(self) -> None:
        self._ready = False
        self._stop.set()
        pump, self._pump = self._pump, None
        if pump is not None and pump.is_alive():
            pump.join(timeout=1.0)
        self._shut()


def open_bridge(kind: str = "auto", path: str = "", simconnect=None,
                client: str = "WilcoATC") -> Bridge:
    """The best bridge available, or one that politely has nothing.

    ``auto`` prefers a file if one is configured and present, because a file
    that exists was put there on purpose, and otherwise tries MobiFlight.

    ``simconnect`` is the session flying the aeroplane. It is accepted and
    not used: the MobiFlight channel needs its own connection and its own
    message pump, and borrowing this one meant the values were delivered to a
    dispatcher that drops them.
    """
    kind = (kind or "auto").lower()

    if kind in ("file", "auto") and path:
        bridge = FileBridge(path)
        if bridge.available or kind == "file":
            return bridge

    nothing = Bridge()
    if kind in ("mobiflight", "auto"):
        bridge = MobiFlightBridge(client=client)
        if bridge.open():
            return bridge
        # Why it did not work travels with the nothing, so that the
        # diagnostic can name the actual cause rather than the commonest one.
        nothing.trouble = bridge.trouble
        bridge.close()

    return nothing


__all__ = ["Bridge", "FileBridge", "MobiFlightBridge", "open_bridge",
           "find_simconnect", "MF_COMMAND", "MF_RESPONSE", "MF_LVARS",
           "MESSAGE_SIZE", "MAX_VARS", "VALUE_SIZE"]
