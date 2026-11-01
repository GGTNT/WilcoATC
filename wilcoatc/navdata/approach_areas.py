"""The approach areas: where approach works an arrival and departure a climb.

A centre handed an arrival to approach sixty miles from the field and
departure handed a climb to the centre at 30 nm or 17,000 ft above it,
whatever the field. The real line is the approach unit's own airspace. This
reads its outline, built from the SimAware TRACON Project by
``scripts/build_approach_areas.py`` into ``approach_areas.json.gz`` beside
the database (CC BY-SA 4.0, credited in ``data/navdata/SOURCES.md``).

Three rules, the user's:

- the outline is geometry only: who the flight is handed to, and on what
  frequency, is this program's own station data (``NavDB.station_for``),
  never the file's name;
- a field's area is the one its own coordinates fall in, the smallest where
  several do (a Paris approach inside a larger Paris one), never one found
  by its name or prefix;
- the data has no ceilings: every area goes up to ``AREA_CEILING_FT``, and a
  flight is in it only inside the outline *and* below that.

A field outside every area has none, and its handoffs keep the old
distances; ``freq --audit`` lists those fields.
"""

from __future__ import annotations

import gzip
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

FILE_NAME = "approach_areas.json.gz"

# No area in the source carries a ceiling. About 17,000 ft is where
# approach airspace ends in most of the world's terminal areas and is what
# departure climbed to before; the user's figure for every area.
AREA_CEILING_FT = 17000.0


@dataclass(frozen=True)
class Area:
    """One approach unit's outline. ``name`` is the source's, for the logs
    and the audit only: nothing is ever said with it."""

    id: str
    name: str
    rings: tuple[tuple[tuple[float, float], ...], ...]   # (lon, lat)
    bbox: tuple[float, float, float, float]              # south, west, north, east
    size: float                                           # square degrees, scaled

    ceiling_ft: float = AREA_CEILING_FT

    def covers(self, lat: float, lon: float) -> bool:
        """Inside the outline, whatever the height."""
        south, west, north, east = self.bbox
        if not (south <= lat <= north and west <= lon <= east):
            return False
        return any(_in_ring(ring, lat, lon) for ring in self.rings)

    def contains(self, lat: float, lon: float, altitude_ft: float) -> bool:
        """Inside the outline and below the ceiling."""
        return altitude_ft < self.ceiling_ft and self.covers(lat, lon)


def _in_ring(ring, lat: float, lon: float) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and \
                lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _area(row: dict) -> Area | None:
    rings = tuple(tuple((float(x), float(y)) for x, y in ring)
                  for ring in row.get("rings") or () if len(ring) >= 4)
    if not rings:
        return None
    lats = [y for ring in rings for _, y in ring]
    lons = [x for ring in rings for x, _ in ring]
    scale = math.cos(math.radians((min(lats) + max(lats)) / 2.0))
    size = sum(abs(_shoelace(ring)) for ring in rings) * scale
    return Area(id=str(row.get("id") or ""), name=str(row.get("name") or ""),
                rings=rings, bbox=(min(lats), min(lons), max(lats), max(lons)),
                size=size)


def _shoelace(ring) -> float:
    return 0.5 * sum(x1 * y2 - x2 * y1
                     for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]))


class ApproachAreas:
    """Every area, and the one round a field."""

    def __init__(self, areas: list[Area], version: str = ""):
        self.areas = areas
        self.version = version
        self._fields: dict[str, Area | None] = {}

    @classmethod
    def load(cls, path: Path | str) -> "ApproachAreas":
        path = Path(path)
        try:
            data = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
        except FileNotFoundError:
            log.info("no approach areas at %s: handoffs by distance", path)
            return cls([])
        except (OSError, ValueError) as exc:
            log.warning("could not read %s: %s", path, exc)
            return cls([])
        areas = [a for a in (_area(row) for row in data.get("areas") or ())
                 if a is not None]
        return cls(areas, str(data.get("version") or ""))

    def at(self, lat: float, lon: float) -> Area | None:
        """The smallest area whose outline holds this point, or None."""
        found = [a for a in self.areas if a.covers(lat, lon)]
        return min(found, key=lambda a: a.size) if found else None

    def for_field(self, ident: str, lat: float, lon: float) -> Area | None:
        """A field's area: the smallest holding its own coordinates."""
        key = (ident or "").upper()
        if key not in self._fields:
            self._fields[key] = self.at(lat, lon)
        return self._fields[key]


__all__ = ["Area", "ApproachAreas", "AREA_CEILING_FT", "FILE_NAME"]
