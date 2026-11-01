"""Placing a named fix from the simulator's own navigation data.

A flight plan saved by MSFS 2024 names every fix by ident and ICAO region
and gives no coordinates (measured on a SimBrief export for 2024,
EBBR-LIRF), and ``nav.sqlite`` holds navaids and airports but no
intersections. The simulator has all of them, in whatever AIRAC it is
flying, so a fix is asked for the way the taxiways and the approaches are
(:func:`integrations.taxiways.ask_facility`): one session, one request,
the answer thrown away whole if it is not the expected shape.

Fields and types from the SDK's AddToFacilityDefinition: WAYPOINT, VOR and
NDB each open with LATITUDE and LONGITUDE, FLOAT64 degrees. The data type
numbers (VOR 19, NDB 20, WAYPOINT 21) are the SDK enum's order.
# UNVERIFIED: not yet measured against a running simulator; a block of any
# other size leaves the fix unplaced, which is what the database alone
# would have done.
"""

from __future__ import annotations

import logging
import struct

from .taxiways import ask_facility

log = logging.getLogger(__name__)

_SHAPE = "<dd"
_KINDS = {"vor": b"VOR", "ndb": b"NDB", "wpt": b"WAYPOINT", "": b"WAYPOINT"}


def _fields(kind: bytes) -> tuple[bytes, ...]:
    return (b"OPEN " + kind, b"LATITUDE", b"LONGITUDE", b"CLOSE " + kind)


class SimFixes:
    """Fix positions from the simulator, cached for the session."""

    TIMEOUT_S = 4.0

    def __init__(self, dll_path: str = ""):
        self.dll_path = dll_path
        self._cache: dict[tuple[str, str, str], tuple[float, float] | None] = {}

    def __call__(self, ident: str, region: str, kind: str
                 ) -> tuple[float, float] | None:
        """(lat, lon) of a fix, or None. Airports are the database's."""
        if kind == "apt":
            return None
        key = (ident.upper(), region.upper(), kind)
        if key in self._cache:
            return self._cache[key]
        found = None
        # An intersection is asked for as one; a fix of unknown kind -- a
        # typed route -- as a waypoint first, which in the simulator's data
        # every navaid also is.
        for which in ([_KINDS[kind]] if kind in ("vor", "ndb", "wpt")
                      else [b"WAYPOINT", b"VOR", b"NDB"]):
            try:
                blocks = ask_facility(self.dll_path, b"WilcoATC fixes",
                                      _fields(which), key[0], self.TIMEOUT_S,
                                      region=key[1])
            except Exception:
                log.info("asking the simulator for %s raised", ident,
                         exc_info=True)
                blocks = None
            found = decode(blocks)
            if found is not None:
                break
        self._cache[key] = found
        return found


def decode(blocks: dict[int, list[bytes]] | None
           ) -> tuple[float, float] | None:
    """The first block that is two doubles and a position on the earth."""
    for raws in (blocks or {}).values():
        for raw in raws:
            if len(raw) != struct.calcsize(_SHAPE):
                continue
            lat, lon = struct.unpack(_SHAPE, raw)
            if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat or lon):
                return lat, lon
    return None


__all__ = ["SimFixes", "decode"]
