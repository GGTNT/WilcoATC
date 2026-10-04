"""Asking the simulator for an airport's stands and taxiways.

The ground half of the traffic (:mod:`wilcoatc.atc.ground`) drives aeroplanes
along real taxiways from real stands, and this is where those come from: the
same facility API that :mod:`wilcoatc.integrations.facilities` reads the
frequencies out of, asked for four more things -- the taxi points, the
parking spots, the paths between them, and the taxiway names.

The answer is one message per item, each a fixed block in the order the
fields were asked for. The sizes were read off a running Microsoft Flight
Simulator 2024 at Madeira (502 points of 16 bytes, 19 stands of 40, 542 paths
of 28, 4 names of 32) and every item is checked against them: a block of the
wrong size means a simulator that numbers things differently, and the airport
is left without a layout -- placed, as it always was -- rather than read
wrongly.

Like the frequency reader it opens its own session, so a slow answer cannot
hold up the one flying the aeroplane, and it needs a SimConnect with the
facility API (see :func:`wilcoatc.integrations.facilities.find_simconnect`).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import logging
import struct
import time

from ..atc.ground import GroundLayout
from .facilities import find_simconnect

log = logging.getLogger(__name__)

# The facility data types, from the SDK header.
_AIRPORT, _TAXI_POINT, _TAXI_PARKING, _TAXI_PATH, _TAXI_NAME = 0, 14, 15, 16, 17

# What is asked for, in order. The order is the layout of each block.
_FIELDS = (
    b"OPEN AIRPORT", b"LATITUDE", b"LONGITUDE", b"ALTITUDE",
    b"OPEN TAXI_POINT", b"TYPE", b"ORIENTATION", b"BIAS_X", b"BIAS_Z",
    b"CLOSE TAXI_POINT",
    b"OPEN TAXI_PARKING", b"TYPE", b"TAXI_POINT_TYPE", b"NAME", b"SUFFIX",
    b"NUMBER", b"ORIENTATION", b"HEADING", b"RADIUS", b"BIAS_X", b"BIAS_Z",
    b"CLOSE TAXI_PARKING",
    b"OPEN TAXI_PATH", b"TYPE", b"WIDTH", b"RUNWAY_NUMBER",
    b"RUNWAY_DESIGNATOR", b"START", b"END", b"NAME_INDEX",
    b"CLOSE TAXI_PATH",
    b"OPEN TAXI_NAME", b"NAME", b"CLOSE TAXI_NAME",
    b"CLOSE AIRPORT",
)

# Each block's shape, and so its size.
_SHAPES = {
    _AIRPORT: "<ddd",
    _TAXI_POINT: "<iiff",
    _TAXI_PARKING: "<iiiiiiffff",
    _TAXI_PATH: "<ifiiiiI",
}
_NAME_BYTES = 32

_RECV_EXCEPTION = 1
_RECV_QUIT = 3
_RECV_FACILITY_DATA = 28
_RECV_FACILITY_DATA_END = 29
_DEF_ID = 0x5711
_REQ_ID = 0x5712


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


def decode(blocks: dict[int, list[bytes]], ident: str) -> GroundLayout | None:
    """A layout out of the raw blocks, or None if they are not the shape
    this was written against."""
    try:
        airport = blocks.get(_AIRPORT) or []
        if not airport:
            return None
        latitude, longitude, _altitude = struct.unpack(
            _SHAPES[_AIRPORT], airport[0][:struct.calcsize(_SHAPES[_AIRPORT])])
        rows: dict[int, list] = {}
        for kind in (_TAXI_POINT, _TAXI_PARKING, _TAXI_PATH):
            shape = _SHAPES[kind]
            size = struct.calcsize(shape)
            rows[kind] = []
            for raw in blocks.get(kind, []):
                if len(raw) != size:
                    log.info("%s: a %d-byte block where %d were expected; "
                             "not using the simulator's taxiways", ident,
                             len(raw), size)
                    return None
                rows[kind].append(list(struct.unpack(shape, raw)))
        names = [raw[:_NAME_BYTES].split(b"\0")[0].decode("utf-8", "replace")
                 for raw in blocks.get(_TAXI_NAME, [])]
    except struct.error:
        log.info("%s: the taxiway data did not decode", ident, exc_info=True)
        return None
    layout = GroundLayout(ident, latitude, longitude, rows[_TAXI_POINT],
                          rows[_TAXI_PARKING], rows[_TAXI_PATH], names)
    return layout if layout else None


class SimGround:
    """The simulator's taxi network, one airport at a time."""

    TIMEOUT_S = 15.0

    def __init__(self, dll_path: str = ""):
        self.dll_path = dll_path
        self._cache: dict[str, GroundLayout | None] = {}

    def layout(self, ident: str) -> GroundLayout | None:
        """The airport's stands and taxiways, or None if it has none or the
        simulator cannot be asked."""
        ident = (ident or "").strip().upper()
        if not ident:
            return None
        if ident in self._cache:
            return self._cache[ident]
        try:
            found = self._ask(ident)
        except Exception:
            log.info("asking the simulator for %s's taxiways raised", ident,
                     exc_info=True)
            found = None
        self._cache[ident] = found
        return found

    def _ask(self, ident: str) -> GroundLayout | None:
        blocks = ask_facility(self.dll_path, b"WilcoATC taxiways", _FIELDS,
                              ident, self.TIMEOUT_S)
        return None if blocks is None else decode(blocks, ident)


def ask_facility(dll_path: str, client: bytes, fields, ident: str,
                 timeout_s: float = 15.0) -> dict[int, list[bytes]] | None:
    """Ask the simulator for one airport's facility data.

    ``fields`` is the definition, in order, and the answer is the raw blocks
    by facility data type, or None if the simulator could not be asked or
    gave up. Each call opens its own session, so a slow answer never holds
    up the one flying the aeroplane. Shared by the taxiways and the
    approaches, which ask for different things the same way.
    """
    library = find_simconnect(dll_path)
    if library is None:
        return None
    dll = ctypes.WinDLL(str(library))
    dll.SimConnect_Open.argtypes = [
        ctypes.POINTER(w.HANDLE), ctypes.c_char_p, w.HWND, w.DWORD,
        w.HANDLE, w.DWORD]
    dll.SimConnect_AddToFacilityDefinition.argtypes = [
        w.HANDLE, w.DWORD, ctypes.c_char_p]
    dll.SimConnect_RequestFacilityData.argtypes = [
        w.HANDLE, w.DWORD, w.DWORD, ctypes.c_char_p, ctypes.c_char_p]
    dll.SimConnect_GetNextDispatch.argtypes = [
        w.HANDLE, ctypes.POINTER(ctypes.POINTER(_Recv)),
        ctypes.POINTER(w.DWORD)]
    dll.SimConnect_Close.argtypes = [w.HANDLE]
    handle = w.HANDLE()
    if dll.SimConnect_Open(ctypes.byref(handle), client,
                           None, 0, None, 0) != 0:
        return None
    try:
        for field in fields:
            if dll.SimConnect_AddToFacilityDefinition(
                    handle, _DEF_ID, field) != 0:
                return None
        if dll.SimConnect_RequestFacilityData(
                handle, _DEF_ID, _REQ_ID, ident.encode("ascii", "ignore"),
                b"") != 0:
            return None
        blocks: dict[int, list[bytes]] = {}
        data = ctypes.POINTER(_Recv)()
        size = w.DWORD()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if dll.SimConnect_GetNextDispatch(
                    handle, ctypes.byref(data), ctypes.byref(size)) != 0 \
                    or not data:
                time.sleep(0.01)
                continue
            which = data.contents.dwID
            if which == _RECV_FACILITY_DATA:
                block = ctypes.cast(
                    data, ctypes.POINTER(_FacilityData)).contents
                length = block.dwSize - ctypes.sizeof(_FacilityData)
                raw = ctypes.string_at(
                    ctypes.addressof(block) + ctypes.sizeof(_FacilityData),
                    max(0, length))
                blocks.setdefault(int(block.Type), []).append(raw)
            elif which == _RECV_FACILITY_DATA_END:
                break
            elif which in (_RECV_EXCEPTION, _RECV_QUIT):
                return None
        return blocks
    finally:
        dll.SimConnect_Close(handle)


__all__ = ["SimGround", "decode", "ask_facility"]
