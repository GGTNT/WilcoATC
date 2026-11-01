# -*- coding: utf-8 -*-
"""Circuits, reporting points and airspace volumes, from files.

The tower used to know none of these. Every circuit was left-hand at a
thousand feet, no reporting point existed anywhere, and the class of the
airspace round a field was guessed from the size of the field
(``ControllerBrain._airspace_class``). Nothing open publishes them for the
whole world, so they come from files beside the database, each with its
source on every row:

``circuits*.csv``   ident, runway, side (left/right), height_ft_agl, source
``vrps*.csv``       ident, name, code, lat, lon, kind, altitude_ft, source
``airspace*.csv``   one volume a row; see AIRSPACE_HEADER for the columns

The ``*_faa.csv`` files are generated from FAA NASR by
``scripts/build_nasr.py`` and are not edited; the plain ones are kept by
hand, and a hand row wins over a generated one for the same key. The files
are read in the order given, later ones winning. ``SOURCES.md`` beside them
says where each came from and under what licence.
"""

from __future__ import annotations

import csv
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


def _rows(path: Path):
    """The data rows of a CSV that may carry ``#`` comment lines."""
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        lines = [ln for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
    return list(csv.DictReader(lines))


def _int(text) -> int:
    try:
        return int(float(str(text).strip()))
    except (TypeError, ValueError):
        return 0


def _float(text) -> float | None:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# circuits
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class CircuitData:
    """What is published for one runway end's circuit. Either half may be
    missing: "" for the side, 0 for the height."""

    side: str
    height_ft_agl: int
    source: str


def load_circuits(paths) -> dict[tuple[str, str], CircuitData]:
    table: dict[tuple[str, str], CircuitData] = {}
    for path in paths:
        for r in _rows(Path(path)):
            ident = (r.get("ident") or "").strip().upper()
            runway = (r.get("runway") or "").strip().upper()
            side = (r.get("side") or "").strip().lower()
            if not ident or not runway:
                continue
            if side not in ("left", "right"):
                side = ""
            table[(ident, runway)] = CircuitData(
                side, _int(r.get("height_ft_agl")), (r.get("source") or "").strip())
    return table


# ----------------------------------------------------------------------
# reporting points
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class ReportingPoint:
    """A visual reporting point.

    ``name`` is what is said on the radio ("November", "Sierra", "the
    reservoir"); ``code`` is the identifier a navigation database uses
    (the FAA's VPxxx). A point with no name is never said -- a controller does
    not read a five-letter code out -- but is still there for the map.
    """

    ident: str
    name: str
    code: str
    lat: float
    lon: float
    kind: str = "both"           # entry, exit, both, holding or svfr
    altitude_ft: int = 0
    source: str = ""

    @property
    def spoken(self) -> bool:
        return bool(self.name)

    @property
    def for_arrivals(self) -> bool:
        return self.kind in ("entry", "both")

    @property
    def for_departures(self) -> bool:
        return self.kind in ("exit", "both")

    @property
    def for_svfr(self) -> bool:
        """A Special VFR reporting point: Special VFR routes go by these,
        and ordinary VFR only where the field has no other point."""
        return self.kind == "svfr"


def load_vrps(paths) -> dict[str, list[ReportingPoint]]:
    """By field. A hand file's points for a field replace a generated file's
    for that field, rather than being added to them."""
    table: dict[str, list[ReportingPoint]] = {}
    for path in paths:
        mine: dict[str, list[ReportingPoint]] = {}
        for r in _rows(Path(path)):
            ident = (r.get("ident") or "").strip().upper()
            lat, lon = _float(r.get("lat")), _float(r.get("lon"))
            if not ident or lat is None or lon is None:
                continue
            kind = (r.get("kind") or "both").strip().lower()
            mine.setdefault(ident, []).append(ReportingPoint(
                ident, (r.get("name") or "").strip(), (r.get("code") or "").strip(),
                lat, lon, kind if kind in ("entry", "exit", "both", "holding", "svfr")
                else "both",
                _int(r.get("altitude_ft")), (r.get("source") or "").strip()))
        table.update(mine)
    return table


# ----------------------------------------------------------------------
# airspace
# ----------------------------------------------------------------------

AIRSPACE_COLUMNS = ("id", "name", "class", "airport", "lower_ft", "lower_ref",
                    "upper_ft", "upper_ref", "shape", "lat", "lon",
                    "radius_nm", "points", "hours", "source")
AIRSPACE_HEADER = ",".join(AIRSPACE_COLUMNS) + "\n"


def _nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3440.065 * 2 * math.asin(min(1.0, math.sqrt(a)))


@dataclass(frozen=True)
class Volume:
    """One volume of airspace: a class, a floor, a ceiling and a shape.

    Limits are in feet with a reference: SFC (the ground, for a floor), AGL
    (above the ground under the aeroplane), MSL. A circle is a centre and a
    radius in nautical miles; a polygon is its corners in order.
    """

    id: str
    name: str
    cls: str
    airport: str
    lower_ft: int
    lower_ref: str
    upper_ft: int
    upper_ref: str
    shape: str
    lat: float = 0.0
    lon: float = 0.0
    radius_nm: float = 0.0
    ring: tuple = ()
    hours: str = ""
    source: str = ""
    bbox: tuple = field(default=(0.0, 0.0, 0.0, 0.0), compare=False)

    def floor_msl(self, ground_ft: float = 0.0) -> float:
        if self.lower_ref == "SFC":
            return -1e9
        return self.lower_ft + (ground_ft if self.lower_ref == "AGL" else 0.0)

    def ceiling_msl(self, ground_ft: float = 0.0) -> float:
        return self.upper_ft + (ground_ft if self.upper_ref == "AGL" else 0.0)

    def covers(self, lat: float, lon: float) -> bool:
        """Inside the outline, whatever the height."""
        south, west, north, east = self.bbox
        if not (south <= lat <= north and west <= lon <= east):
            return False
        if self.shape == "circle":
            return _nm(lat, lon, self.lat, self.lon) <= self.radius_nm
        inside = False
        ring = self.ring
        j = len(ring) - 1
        for i in range(len(ring)):
            yi, xi = ring[i]
            yj, xj = ring[j]
            if (xi > lon) != (xj > lon) and \
                    lat < (yj - yi) * (lon - xi) / ((xj - xi) or 1e-12) + yi:
                inside = not inside
            j = i
        return inside

    def contains(self, lat: float, lon: float, altitude_ft: float,
                 ground_ft: float = 0.0) -> bool:
        """Inside the outline, between the floor (inclusive) and the
        ceiling (exclusive -- "up to but not including")."""
        if not self.floor_msl(ground_ft) <= altitude_ft < self.ceiling_msl(ground_ft):
            return False
        return self.covers(lat, lon)


def _volume(r: dict) -> Volume | None:
    shape = (r.get("shape") or "").strip().lower()
    cls = (r.get("class") or "").strip().upper()[:1]
    if cls not in "ABCDEFG" or not cls:
        return None
    lower_ref = (r.get("lower_ref") or "MSL").strip().upper()
    upper_ref = (r.get("upper_ref") or "MSL").strip().upper()
    common = dict(
        id=(r.get("id") or "").strip(), name=(r.get("name") or "").strip(),
        cls=cls, airport=(r.get("airport") or "").strip().upper(),
        lower_ft=_int(r.get("lower_ft")), lower_ref=lower_ref,
        upper_ft=_int(r.get("upper_ft")), upper_ref=upper_ref,
        hours=(r.get("hours") or "").strip(), source=(r.get("source") or "").strip())
    if shape == "circle":
        lat, lon = _float(r.get("lat")), _float(r.get("lon"))
        radius = _float(r.get("radius_nm"))
        if lat is None or lon is None or not radius:
            return None
        dlat = radius / 60.0
        dlon = radius / (60.0 * max(0.01, math.cos(math.radians(lat))))
        return Volume(shape="circle", lat=lat, lon=lon, radius_nm=radius,
                      bbox=(lat - dlat, lon - dlon, lat + dlat, lon + dlon),
                      **common)
    if shape == "polygon":
        ring = []
        for pair in (r.get("points") or "").split(";"):
            parts = pair.split()
            if len(parts) != 2:
                return None
            lat, lon = _float(parts[0]), _float(parts[1])
            if lat is None or lon is None:
                return None
            ring.append((lat, lon))
        if len(ring) < 3:
            return None
        lats = [p[0] for p in ring]
        lons = [p[1] for p in ring]
        return Volume(shape="polygon", ring=tuple(ring),
                      bbox=(min(lats), min(lons), max(lats), max(lons)), **common)
    return None


def load_airspace(paths) -> list[Volume]:
    """Every volume in the files. A row that cannot be read is logged and
    skipped: one typing slip in a hand file must not lose the rest."""
    found: list[Volume] = []
    for path in paths:
        for number, r in enumerate(_rows(Path(path)), start=1):
            volume = _volume(r)
            if volume is None:
                log.warning("%s row %d is not a volume this can read: %s",
                            Path(path).name, number, r.get("id") or r.get("name"))
                continue
            found.append(volume)
    return found


__all__ = ["AIRSPACE_COLUMNS", "AIRSPACE_HEADER", "CircuitData",
           "ReportingPoint", "Volume", "load_airspace", "load_circuits",
           "load_vrps"]
