"""Asking the simulator for an airport's runways.

Why this exists
---------------

Frequencies, taxiways, stands, approaches and pattern altitudes came from
the simulator when it was running, and the runways did not: they came from
OurAirports' ``runways.csv``, and the runway in use, the taxi plan's
crossings and hold-shorts, the final-approach cone, the landing report and
the circuit were all measured against it. Where the scenery differs -- a
runway renumbered since, one open in the simulator and closed in the world,
a threshold moved, a runway one of them does not have -- the pilot was
cleared onto a runway that was not there, or told "runway 25" under signs
that said 26. The simulator's runways are the ones the aeroplane rolls on.

So each airport is asked for its runways once, the way
:mod:`wilcoatc.integrations.approaches` asks for its approaches: one session
of its own, one request, the answer decoded against a fixed block shape and
thrown away whole if the shape is wrong, as the taxiway reader does. What
comes back is the database's own :class:`wilcoatc.navdata.db.Runway`
(``source="sim"``), so nothing downstream needs to know where a runway came
from; ``NavDB.set_sim_runways`` puts them ahead of the database's, and
``navdata/runway_cache.py`` keeps them for the sessions without the
simulator.

The fields are LocalTC's set (``sim_bridge/facilities.py``, read for the
design, nothing copied): the centre, its height, the true heading of the
primary end, length and width in metres, the pattern altitude, the surface,
both ends' numbers and designators, and both ends' ILS idents. The ends are
worked out from the centre, half the length either way.

Four more requests follow, one each, in exactly the shapes MSFS 2024
answered at the sim session of 30 Oct 2026 (``scripts/sim_session.py
runways``; EBBR, LIRF, EBKT, every one accepted): each end's displaced
threshold (``PRIMARY_THRESHOLD``/``SECONDARY_THRESHOLD`` children, one
``LENGTH`` in metres each, answered as blocks of type 23, primary then
secondary, runway by runway), each end's closed flag (one byte each), each
end's ILS type and region, and the slope (``TRUE_SLOPE`` in degrees, signed
along the primary end's heading: EBBR 01 to 19 is -0.425, which is the 70 ft
the database has between them). Each is used only when its blocks match the
runways one for one, by number; one that fails or answers in another shape
leaves its fact as the base had it -- no displacement, open, level -- rather
than the airport unknown. Kept as separate requests because that is what
was measured; folded into one, the shape would not have been.

The pattern altitude is read out of the same answer. The documentation does
not say what it is measured from; read as a height above the field it is
used only inside a plausible circuit band (CIRCUIT_BAND_FT), and anything
else is left alone.
"""

from __future__ import annotations

import logging
import math
import struct

from ..atc.approaches import runway_from
from ..logs import decision
from ..navdata.build import project
from .taxiways import ask_facility

log = logging.getLogger(__name__)

_RUNWAY = 1            # SIMCONNECT_FACILITY_DATA_TYPE

FIELDS = (
    b"OPEN AIRPORT",
    b"OPEN RUNWAY",
    b"LATITUDE", b"LONGITUDE", b"ALTITUDE", b"HEADING", b"LENGTH", b"WIDTH",
    b"PATTERN_ALTITUDE", b"SURFACE",
    b"PRIMARY_NUMBER", b"PRIMARY_DESIGNATOR",
    b"SECONDARY_NUMBER", b"SECONDARY_DESIGNATOR",
    b"PRIMARY_ILS_ICAO", b"SECONDARY_ILS_ICAO",
    b"CLOSE RUNWAY",
    b"CLOSE AIRPORT",
)
SHAPE = "<dddffffiiiii8s8s"
FT_PER_M = 3.28084
# A circuit is flown between these heights above the field (FAA AIM 4-3-3:
# 600 to 1,500 ft for propeller aircraft; turbine circuits a little higher).
CIRCUIT_BAND_FT = (500.0, 2000.0)

# The SDK's runway surfaces, as the database spells them, so a runway's
# is_hard reads the same whichever side it came from. UNVERIFIED: the
# numbering, from the SDK's facility documentation, not yet read back.
SURFACES = {
    0: "CON", 1: "GRS", 2: "WATER", 3: "GRS", 4: "ASP", 5: "GRS", 6: "GRS",
    7: "TURF", 8: "SNOW", 9: "ICE", 10: "URBAN", 11: "FOREST", 12: "DIRT",
    13: "CORAL", 14: "GRAVEL", 15: "OIL", 16: "MATS", 17: "BIT", 18: "BRICK",
    19: "MACADAM", 20: "PLANKS", 21: "SAND", 22: "SHALE", 23: "TAR",
}


_THRESHOLD = 23       # the threshold children's facility data type

# The extra requests, as measured: (fields, runway block shape).
EXTRAS = {
    "thresholds": ((b"OPEN AIRPORT", b"OPEN RUNWAY", b"PRIMARY_NUMBER",
                    b"OPEN PRIMARY_THRESHOLD", b"LENGTH",
                    b"CLOSE PRIMARY_THRESHOLD",
                    b"OPEN SECONDARY_THRESHOLD", b"LENGTH",
                    b"CLOSE SECONDARY_THRESHOLD",
                    b"CLOSE RUNWAY", b"CLOSE AIRPORT"), "<i"),
    "closed": ((b"OPEN AIRPORT", b"OPEN RUNWAY", b"PRIMARY_NUMBER",
                b"PRIMARY_CLOSED", b"SECONDARY_CLOSED",
                b"CLOSE RUNWAY", b"CLOSE AIRPORT"), "<i??"),
    "ils_type": ((b"OPEN AIRPORT", b"OPEN RUNWAY", b"PRIMARY_NUMBER",
                  b"PRIMARY_ILS_TYPE", b"SECONDARY_ILS_TYPE",
                  b"PRIMARY_ILS_REGION", b"SECONDARY_ILS_REGION",
                  b"CLOSE RUNWAY", b"CLOSE AIRPORT"), "<iii8s8s"),
    "slope": ((b"OPEN AIRPORT", b"OPEN RUNWAY", b"PRIMARY_NUMBER",
               b"SLOPE", b"TRUE_SLOPE", b"CLOSE RUNWAY", b"CLOSE AIRPORT"),
              "<iff"),
}


def merge_extras(rows: list[dict], extras: dict, ident: str) -> list[dict]:
    """Each extra request's facts added to the base rows, where its blocks
    match them one for one by number; an extra that does not is left out
    (and said, at INFO)."""
    numbers = [int(row["primary"][:2]) if row["primary"] else 0 for row in rows]
    for name, blocks in (extras or {}).items():
        if name not in EXTRAS or blocks is None:
            continue
        shape = EXTRAS[name][1]
        runway_blocks = blocks.get(_RUNWAY, [])
        try:
            unpacked = [struct.unpack(shape, raw) for raw in runway_blocks]
        except struct.error:
            log.info("%s: the %s answer is not the shape measured; not used",
                     ident, name)
            continue
        if [u[0] for u in unpacked] != numbers:
            log.info("%s: the %s answer does not match the runways; not used",
                     ident, name)
            continue
        if name == "thresholds":
            lengths = blocks.get(_THRESHOLD, [])
            if len(lengths) != 2 * len(rows):
                log.info("%s: %d thresholds for %d runways; not used",
                         ident, len(lengths), len(rows))
                continue
            for i, row in enumerate(rows):
                row["primary_displaced_m"] = struct.unpack("<f", lengths[2 * i])[0]
                row["secondary_displaced_m"] = struct.unpack(
                    "<f", lengths[2 * i + 1])[0]
        elif name == "closed":
            for row, (_n, primary, secondary) in zip(rows, unpacked):
                row["primary_closed"], row["secondary_closed"] = primary, secondary
        elif name == "ils_type":
            for row, (_n, primary, secondary, _r1, _r2) in zip(rows, unpacked):
                # 0 is no navaid at that end. Anything else ("V", 86) is a
                # VOR-family navaid -- an ILS's localizer or a VOR: the type
                # does not tell them apart, so the ident is kept as it came.
                if not primary:
                    row["primary_ils"] = ""
                if not secondary:
                    row["secondary_ils"] = ""
        elif name == "slope":
            for row, (_n, _slope, true_slope) in zip(rows, unpacked):
                row["true_slope_deg"] = true_slope
    return rows


def _text(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", "ignore").strip()


def decode_blocks(blocks: dict[int, list[bytes]], ident: str) -> list[dict] | None:
    """Each runway block as a dict of its fields, or None if any block is
    not the shape this was written against."""
    size = struct.calcsize(SHAPE)
    rows = []
    for raw in blocks.get(_RUNWAY, []):
        if len(raw) != size:
            log.info("%s: a %d-byte runway block where %d were expected; "
                     "not using the simulator's runways", ident, len(raw), size)
            return None
        (lat, lon, alt_m, heading, length_m, width_m, pattern_m, surface,
         number, designator, other, other_designator,
         ils, other_ils) = struct.unpack(SHAPE, raw)
        rows.append({
            "lat": lat, "lon": lon, "alt_m": alt_m, "heading": heading,
            "length_m": length_m, "width_m": width_m, "pattern_m": pattern_m,
            "surface": surface, "primary": runway_from(number, designator),
            "secondary": runway_from(other, other_designator),
            "primary_ils": _text(ils), "secondary_ils": _text(other_ils)})
    return rows


def runways_from(rows: list[dict], ident: str):
    """The decoded blocks as the database's Runway rows (source "sim")."""
    from ..navdata.db import Runway

    out = []
    for row in rows:
        if not row["primary"] or not row["secondary"] or row["length_m"] <= 0:
            continue
        half_m = row["length_m"] / 2.0
        heading = row["heading"] % 360.0
        back = (heading + 180.0) % 360.0
        # The primary end's threshold is where its heading starts from:
        # half the runway behind the centre.
        le_lat, le_lon = project(row["lat"], row["lon"], back, half_m / 1852.0)
        he_lat, he_lon = project(row["lat"], row["lon"], heading,
                                 half_m / 1852.0)
        # Each end at its own height: the centre's, less (or plus) half the
        # rise along the primary heading.
        rise_m = math.tan(math.radians(row.get("true_slope_deg", 0.0))) * half_m
        le_elev = (row["alt_m"] - rise_m) * FT_PER_M
        he_elev = (row["alt_m"] + rise_m) * FT_PER_M
        surface = SURFACES.get(row["surface"], "")
        primary_closed = bool(row.get("primary_closed", False))
        secondary_closed = bool(row.get("secondary_closed", False))
        out.append(Runway(
            ident=ident, le_ident=row["primary"], he_ident=row["secondary"],
            length_ft=row["length_m"] * FT_PER_M,
            width_ft=row["width_m"] * FT_PER_M,
            surface=surface,
            # UNVERIFIED: no lighting field in this shape; a hard runway is
            # taken as lit, which is what nearly every one the simulator
            # draws with a number is.
            lighted=surface in ("CON", "ASP", "BIT", "TAR"),
            closed=primary_closed and secondary_closed,
            le_lat=le_lat, le_lon=le_lon, le_heading=heading,
            he_lat=he_lat, he_lon=he_lon, he_heading=back,
            le_elev_ft=le_elev, he_elev_ft=he_elev, source="sim",
            le_ils=row["primary_ils"], he_ils=row["secondary_ils"],
            le_displaced_ft=row.get("primary_displaced_m", 0.0) * FT_PER_M,
            he_displaced_ft=row.get("secondary_displaced_m", 0.0) * FT_PER_M,
            le_closed=primary_closed, he_closed=secondary_closed))
    return tuple(out)


# The approach kinds that fly an end's localizer: an ILS, or a localizer
# approach to the same transmitter with the glideslope left out.
LOCALIZER_KINDS = {"ILS", "LOC"}


def with_ils_kinds(runways, approaches):
    """Each end's navaid told apart by the simulator's own approaches.

    The runway's ILS type is 86 ("V") for a localizer and a VOR alike (sim
    session, 30 Oct 2026), so the ident alone cannot say which an end has.
    The approaches can: an ILS or localizer approach to the end is a
    localizer, anything else there is a VOR (the pilot's rule). Unchanged
    when the approaches are not known."""
    from dataclasses import replace

    from ..navdata.db import normalize_runway

    if approaches is None:
        return runways
    by_runway = getattr(approaches, "by_runway", {}) or {}

    def kind(end: str, ident: str) -> str:
        if not ident:
            return ""
        kinds = by_runway.get(normalize_runway(end), set())
        return "ILS" if kinds & LOCALIZER_KINDS else "VOR"

    return tuple(replace(rw, le_ils_kind=kind(rw.le_ident, rw.le_ils),
                         he_ils_kind=kind(rw.he_ident, rw.he_ils))
                 for rw in runways)


def patterns_from(rows: list[dict], ident: str) -> dict[str, int]:
    """Runway end -> pattern height in feet, inside the circuit band."""
    found: dict[str, int] = {}
    for row in rows:
        feet = row["pattern_m"] * FT_PER_M
        kept = CIRCUIT_BAND_FT[0] <= feet <= CIRCUIT_BAND_FT[1]
        ends = [row["primary"], row["secondary"]]
        decision("pattern_altitude", airport=ident, runways=ends,
                 raw_m=round(row["pattern_m"], 1), kept=kept)
        if not kept:
            continue
        for end in ends:
            if end:
                found[end] = int(round(feet / 50.0) * 50)
    return found


def decode(blocks: dict[int, list[bytes]], ident: str) -> dict[str, int] | None:
    """Runway end -> pattern height in feet, or None if the blocks are not
    the shape this was written against."""
    rows = decode_blocks(blocks, ident)
    return None if rows is None else patterns_from(rows, ident)


class SimRunways:
    """The simulator's runways, one airport at a time, asked once."""

    TIMEOUT_S = 10.0

    def __init__(self, dll_path: str = "", ask=ask_facility):
        self.dll_path = dll_path
        self._ask = ask
        self._rows: dict[str, list[dict] | None] = {}

    def _read(self, ident: str):
        ident = (ident or "").strip().upper()
        if not ident:
            return None
        if ident not in self._rows:
            try:
                blocks = self._ask(self.dll_path, b"WilcoATC runways",
                                   FIELDS, ident, self.TIMEOUT_S)
                rows = None if blocks is None else decode_blocks(blocks, ident)
                if rows:
                    extras = {}
                    for name, (fields, _shape) in EXTRAS.items():
                        try:
                            extras[name] = self._ask(
                                self.dll_path, b"WilcoATC runways", fields,
                                ident, self.TIMEOUT_S)
                        except Exception:
                            log.info("asking for %s's %s raised", ident, name,
                                     exc_info=True)
                    rows = merge_extras(rows, extras, ident)
                self._rows[ident] = rows
            except Exception:
                log.info("asking the simulator for %s's runways raised",
                         ident, exc_info=True)
                self._rows[ident] = None
        return self._rows[ident]

    def for_airport(self, ident: str):
        """The field's runways as the simulator has them, or None when the
        simulator is not there, has none, or answers in another shape."""
        rows = self._read(ident)
        if not rows:
            return None
        found = runways_from(rows, ident.strip().upper())
        return found or None

    def patterns(self, ident: str) -> dict[str, int] | None:
        rows = self._read(ident)
        return None if rows is None else patterns_from(rows,
                                                        ident.strip().upper())


__all__ = ["SimRunways", "decode", "decode_blocks", "runways_from",
           "patterns_from", "merge_extras", "with_ils_kinds", "FIELDS",
           "SHAPE", "EXTRAS", "CIRCUIT_BAND_FT"]
