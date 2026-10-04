"""Asking the simulator what an airport's frequencies actually are.

The worldwide dataset this ships with is community-maintained and it is thin.
Charles de Gaulle carries nine frequencies in it; the simulator has thirty, and
four of the nine are not frequencies the simulator has ever heard of. A pilot
comparing the panel against the sim's own ATC menu sees two different airports,
and the sim is the one they have to talk to.

So when the simulator is running, it is the authority. This asks it directly,
through SimConnect's facility API, and what comes back replaces what was
published for that airport.

Two things make that harder than it sounds.

*The facility API is not in every SimConnect.* The library bundled with the
Python wrapper is an old one that exports the aircraft calls and not
``SimConnect_RequestFacilityData``. A modern one ships with the MSFS SDK and
with the simulator itself, so this looks for one that has the call and does
nothing at all when it cannot find one. A missing library is a reason to fall
back to the published data, never a reason to fail.

*The answer arrives in pieces.* A facility request is answered by one message
per frequency on the simulator's dispatch queue, followed by an end marker.
This opens its own SimConnect session for that rather than borrowing the one
flying the aeroplane, so a slow reply cannot stall the situation loop, and it
closes it again when it is done.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# The call that decides whether a library is any use to us.
NEEDS = "SimConnect_RequestFacilityData"

# Where a SimConnect with the facility API is usually found. The SDK is the
# reliable one; the others are where the simulator and its add-ons put theirs.
SEARCH: tuple[str, ...] = (
    r"C:\MSFS 2024 SDK\SimConnect SDK\lib\SimConnect.dll",
    r"C:\MSFS SDK\SimConnect SDK\lib\SimConnect.dll",
    r"C:\MSFS2024 SDK\SimConnect SDK\lib\SimConnect.dll",
    r"C:\Program Files\MSFS SDK\SimConnect SDK\lib\SimConnect.dll",
    r"C:\Program Files (x86)\Steam\steamapps\common"
    r"\MicrosoftFlightSimulator2024\SimConnect.dll",
    r"C:\Program Files (x86)\Steam\steamapps\common"
    r"\MicrosoftFlightSimulator\SimConnect.dll",
)

# What the simulator calls each kind of frequency, and what we call it. The
# numbers are SimConnect's own; they were read back from a running simulator
# and checked against fields whose frequencies are known.
POSITIONS: dict[int, str] = {
    1: "ATIS",
    2: "CTAF",       # MULTICOM, which is a common traffic frequency
    3: "CTAF",       # UNICOM, likewise
    4: "CTAF",
    5: "GND",
    6: "TWR",
    7: "DEL",
    8: "APP",
    9: "DEP",
    10: "CTR",
    11: "INFO",      # flight service
    12: "AWOS",
    13: "AWOS",      # ASOS, which is the same thing to a pilot
    14: "DEL",       # clearance pre-taxi
    15: "DEL",       # remote clearance delivery
}

# SimConnect message ids, from the SDK header.
_RECV_EXCEPTION = 1
_RECV_QUIT = 3
_RECV_FACILITY_DATA = 28
_RECV_FACILITY_DATA_END = 29

_DEF_ID = 0x5701
_REQ_ID = 0x5702


class _Recv(ctypes.Structure):
    _fields_ = [("dwSize", w.DWORD), ("dwVersion", w.DWORD), ("dwID", w.DWORD)]


class _FacilityData(_Recv):
    _fields_ = [
        ("UserRequestId", w.DWORD),
        ("UniqueRequestId", w.DWORD),
        ("ParentUniqueRequestId", w.DWORD),
        ("Type", w.DWORD),
        ("IsListItem", w.DWORD),
        ("ItemIndex", w.DWORD),
        ("ListSize", w.DWORD),
    ]


@dataclass(frozen=True)
class Frequency:
    """One frequency, as the simulator has it."""

    position: str            # our name for it: GND, TWR, APP...
    mhz: float               # the channel as the radio shows it
    name: str                # what the simulator calls the facility
    kind: int                # the simulator's own type number


def find_simconnect(extra: str = "") -> Path | None:
    """A SimConnect library that can answer facility questions, if there is one.

    ``extra`` is tried first, so a pilot with the library somewhere unusual can
    say where in the config rather than being told it is missing.
    """
    candidates = [extra] if extra else []
    candidates += list(SEARCH)
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        path = Path(os.path.expandvars(candidate))
        if not path.is_file():
            continue
        try:
            library = ctypes.WinDLL(str(path))
            getattr(library, NEEDS)
        except (OSError, AttributeError):
            continue
        return path
    return None


class SimFacilities:
    """The simulator's own view of what is on the radio at an airport."""

    # A facility request is answered in well under a second on a running
    # simulator. Past this it is not coming.
    TIMEOUT_S = 8.0

    def __init__(self, dll_path: str = ""):
        self.dll_path = dll_path
        self._dll = None
        self._handle = None
        self._cache: dict[str, list[Frequency]] = {}
        self._failed = False

    @property
    def available(self) -> bool:
        return self._handle is not None and not self._failed

    def open(self) -> bool:
        """Find a library, connect, and describe the shape we want back."""
        if self.available:
            return True
        found = find_simconnect(self.dll_path)
        if found is None:
            log.info("no SimConnect library with the facility API; "
                     "the published frequencies will be used")
            return False
        try:
            dll = ctypes.WinDLL(str(found))
            dll.SimConnect_Open.argtypes = [
                ctypes.POINTER(w.HANDLE), ctypes.c_char_p, w.HWND,
                w.DWORD, w.HANDLE, w.DWORD]
            dll.SimConnect_AddToFacilityDefinition.argtypes = [
                w.HANDLE, w.DWORD, ctypes.c_char_p]
            dll.SimConnect_RequestFacilityData.argtypes = [
                w.HANDLE, w.DWORD, w.DWORD, ctypes.c_char_p, ctypes.c_char_p]
            dll.SimConnect_GetNextDispatch.argtypes = [
                w.HANDLE, ctypes.POINTER(ctypes.POINTER(_Recv)),
                ctypes.POINTER(w.DWORD)]
            dll.SimConnect_Close.argtypes = [w.HANDLE]

            handle = w.HANDLE()
            if dll.SimConnect_Open(ctypes.byref(handle), b"WilcoATC facilities",
                                   None, 0, None, 0) != 0:
                return False

            # Every frequency on the airport: what kind it is, what it is, and
            # what the simulator calls it.
            for field in (b"OPEN AIRPORT", b"OPEN FREQUENCY", b"TYPE",
                          b"FREQUENCY", b"NAME", b"CLOSE FREQUENCY",
                          b"CLOSE AIRPORT"):
                if dll.SimConnect_AddToFacilityDefinition(
                        handle, _DEF_ID, field) != 0:
                    dll.SimConnect_Close(handle)
                    log.info("the simulator would not accept the facility "
                             "definition")
                    return False
        except Exception:
            log.info("could not open a facility connection", exc_info=True)
            return False

        self._dll = dll
        self._handle = handle
        return True

    def close(self) -> None:
        if self._dll is not None and self._handle is not None:
            try:
                self._dll.SimConnect_Close(self._handle)
            except Exception:
                log.debug("closing the facility connection raised",
                          exc_info=True)
        self._dll = self._handle = None
        self._failed = False

    def frequencies(self, icao: str) -> list[Frequency]:
        """Every frequency the simulator has at this airport.

        Empty means it could not be asked or the airport is not in the
        simulator, which are both reasons to keep the published data.
        """
        icao = (icao or "").strip().upper()
        if not icao:
            return []
        if icao in self._cache:
            return self._cache[icao]
        if not self.available:
            return []

        try:
            found = self._ask(icao)
        except Exception:
            log.info("asking the simulator about %s raised", icao,
                     exc_info=True)
            self._failed = True
            return []
        self._cache[icao] = found
        return found

    def _ask(self, icao: str) -> list[Frequency]:
        dll, handle = self._dll, self._handle
        if dll.SimConnect_RequestFacilityData(
                handle, _DEF_ID, _REQ_ID, icao.encode("ascii", "ignore"),
                b"") != 0:
            return []

        found: list[Frequency] = []
        data = ctypes.POINTER(_Recv)()
        size = w.DWORD()
        deadline = time.monotonic() + self.TIMEOUT_S
        while time.monotonic() < deadline:
            if dll.SimConnect_GetNextDispatch(
                    handle, ctypes.byref(data), ctypes.byref(size)) != 0 \
                    or not data:
                time.sleep(0.01)
                continue
            which = data.contents.dwID
            if which == _RECV_FACILITY_DATA:
                one = self._read(data)
                if one is not None:
                    found.append(one)
            elif which == _RECV_FACILITY_DATA_END:
                break
            elif which in (_RECV_EXCEPTION, _RECV_QUIT):
                break
        return found

    @staticmethod
    def _read(data) -> Frequency | None:
        """One frequency out of one message.

        The payload is the fields in the order they were asked for: two
        four-byte integers and then a name that runs to its terminator.
        """
        block = ctypes.cast(data, ctypes.POINTER(_FacilityData)).contents
        length = block.dwSize - ctypes.sizeof(_FacilityData)
        if length < 8:
            return None
        payload = ctypes.cast(
            ctypes.addressof(block) + ctypes.sizeof(_FacilityData),
            ctypes.POINTER(ctypes.c_char))
        raw = bytes(payload[:length])
        kind, hertz = ctypes.cast(
            raw, ctypes.POINTER(ctypes.c_int32 * 2)).contents[:]
        position = POSITIONS.get(int(kind))
        if position is None or hertz <= 0:
            return None
        name = raw[8:].split(b"\x00")[0].decode("utf-8", "replace").strip()
        return Frequency(position=position,
                         mhz=round(hertz / 1_000_000.0, 3),
                         name=name, kind=int(kind))

    def describe(self) -> str:
        if not self.available:
            return "airport frequencies: from the published data"
        return (f"airport frequencies: from the simulator "
                f"({len(self._cache)} airports read)")


__all__ = ["SimFacilities", "Frequency", "find_simconnect", "POSITIONS",
           "SEARCH", "NEEDS"]
