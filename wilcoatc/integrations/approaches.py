"""Asking the simulator which approaches an airport publishes.

The navigation data has runways and frequencies and no procedures, so every
approach was "the ILS" (see :mod:`wilcoatc.atc.approaches`). The simulator's
facility data has the airport's approaches, each with a type and the runway
it serves, and this reads them the same way
:mod:`wilcoatc.integrations.taxiways` reads the taxiways: one session of its
own, one request, the answer decoded against a fixed block shape and thrown
away whole if the shape is wrong.

The fields and their values are the SDK's (AddToFacilityDefinition, APPROACH:
TYPE, SUFFIX, RUNWAY_NUMBER and RUNWAY_DESIGNATOR, each an INT32; FAF_ALTITUDE
in metres and MISSED_ALTITUDE in feet, each a FLOAT32 -- the units are the
documentation's, odd as the pair is, and each altitude is only used inside a
sane band above the field, see ``ControllerBrain.intercept_altitude``). The
block size has not been checked against a running simulator the way the
taxiway sizes were; a block of any other size leaves the airport unknown --
which is the ILS, exactly as before -- rather than read wrongly.
"""

from __future__ import annotations

import logging
import struct

from ..atc.approaches import TYPE_NAMES, FieldApproaches, runway_from
from ..logs import decision
from .taxiways import ask_facility

log = logging.getLogger(__name__)

_APPROACH = 5          # SIMCONNECT_FACILITY_DATA_TYPE

_FIELDS = (
    b"OPEN AIRPORT",
    b"OPEN APPROACH", b"TYPE", b"SUFFIX", b"RUNWAY_NUMBER",
    b"RUNWAY_DESIGNATOR", b"FAF_ALTITUDE", b"MISSED_ALTITUDE",
    b"CLOSE APPROACH",
    b"CLOSE AIRPORT",
)
_SHAPE = "<iiiiff"
FT_PER_M = 3.28084


def decode(blocks: dict[int, list[bytes]], ident: str) -> FieldApproaches | None:
    """The approaches out of the raw blocks, or None if they are not the
    shape this was written against."""
    found = FieldApproaches(ident)
    size = struct.calcsize(_SHAPE)
    for raw in blocks.get(_APPROACH, []):
        if len(raw) != size:
            log.info("%s: a %d-byte approach block where %d were expected; "
                     "not using the simulator's approaches", ident, len(raw), size)
            return None
        kind, _suffix, number, designator, faf_m, missed_ft = \
            struct.unpack(_SHAPE, raw)
        name = TYPE_NAMES.get(kind)
        runway = runway_from(number, designator)
        decision("approach_data", airport=ident, runway=runway, type=name or kind,
                 faf_raw_m=round(faf_m, 1), missed_raw=round(missed_ft, 1),
                 kept=bool(name and runway))
        if name and runway:
            found.add(runway, name, faf_ft=max(0.0, faf_m) * FT_PER_M,
                      missed_ft=max(0.0, missed_ft))
    return found


class SimApproaches:
    """The simulator's published approaches, one airport at a time."""

    TIMEOUT_S = 10.0

    def __init__(self, dll_path: str = ""):
        self.dll_path = dll_path
        self._cache: dict[str, FieldApproaches | None] = {}

    def approaches(self, ident: str) -> FieldApproaches | None:
        ident = (ident or "").strip().upper()
        if not ident:
            return None
        if ident in self._cache:
            return self._cache[ident]
        try:
            blocks = ask_facility(self.dll_path, b"WilcoATC approaches",
                                  _FIELDS, ident, self.TIMEOUT_S)
            found = None if blocks is None else decode(blocks, ident)
        except Exception:
            log.info("asking the simulator for %s's approaches raised", ident,
                     exc_info=True)
            found = None
        self._cache[ident] = found
        return found


__all__ = ["SimApproaches", "decode"]
