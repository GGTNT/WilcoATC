"""How high the ground is around a point, offline, for the descent floor.

A controller never descends an arrival below the minimum sector altitude:
the highest obstacle within 25 NM of the field plus a margin -- 1000 ft, or
2000 ft over mountainous terrain (ICAO Doc 8168, PANS-OPS). The program first
took field elevation plus 3000 ft, which is five thousand feet into the ridges
round Innsbruck, and then the ground directly under the aircraft plus 2000,
which is the valley floor until the moment it is a ridge: no protection where
it was needed and a floor that jumped with every crest overflown.

This answers for an area instead. ``terrain_max.bin`` is the highest terrain
in every eighth-of-a-degree cell of the world, built once by
``scripts/build_terrain_grid.py`` from the USGS GMTED2010 maximum statistic
(public domain), 1.6 MB. A floor is the highest cell any part of which lies
within the radius, so it is conservative by up to a cell's width -- the same
grid-MORA idea charts use -- and it moves only as whole cells enter or leave
the circle, never with the ground under one point. A quarter of a degree was
tried first and reached fifteen miles past the circle; an eighth halves that.

What it is not: obstacles (masts, buildings) are not in a terrain model, and
a published MSA is often lower than a grid allows -- Zurich reads 11,500 ft
here because of a summit 33 NM away whose cell corner is at 24. The hand-
kept ``minimum_altitudes.csv`` overrides the field figure for that reason.
GMTED stops at 56S, so Antarctica reads as sea level here.
"""

from __future__ import annotations

import math
import struct
import threading
import zlib
from pathlib import Path

GRID = Path(__file__).with_name("terrain_max.bin")
FT_PER_M = 3.28084

# The MSA circle and the two margins over it (PANS-OPS). Terrain more than
# this far above the reference counts as mountainous.
RADIUS_NM = 25.0
MARGIN_FT = 1000.0
MOUNTAIN_MARGIN_FT = 2000.0
MOUNTAINOUS_ABOVE_FT = 3000.0

_lock = threading.Lock()
_grid: "tuple[float, int, int, bytes] | None" = None
_missing = False


def _load():
    """The grid, read once: (cell size, rows, columns, big-endian int16)."""
    global _grid, _missing
    with _lock:
        if _grid is None and not _missing:
            try:
                raw = GRID.read_bytes()
            except OSError:
                _missing = True
                return None
            if raw[:4] != b"WTMX":
                _missing = True
                return None
            _version, rows, cols, cell_mdeg, _ = struct.unpack(">HHHHi", raw[4:16])
            _grid = (cell_mdeg / 1000.0, rows, cols, zlib.decompress(raw[16:]))
        return _grid


def available() -> bool:
    return _load() is not None


def highest_ft(lat: float, lon: float, radius_nm: float = RADIUS_NM) -> float:
    """The highest terrain within ``radius_nm`` of a point, in feet.

    Every cell any part of which is inside the circle counts. Zero where
    there is no grid, which leaves the elevation rule on its own.
    """
    grid = _load()
    if grid is None:
        return 0.0
    cell, rows, cols, data = grid
    dlat = radius_nm / 60.0
    coslat = max(0.05, math.cos(math.radians(lat)))
    dlon = min(180.0, radius_nm / (60.0 * coslat))
    r0 = max(0, int(math.floor((lat - dlat + 90.0) / cell)))
    r1 = min(rows - 1, int(math.floor((lat + dlat + 90.0) / cell)))
    c0 = int(math.floor((lon - dlon + 180.0) / cell))
    c1 = int(math.floor((lon + dlon + 180.0) / cell))
    best = 0
    for r in range(r0, r1 + 1):
        south = r * cell - 90.0
        near_lat = min(max(lat, south), south + cell)
        for c in range(c0, c1 + 1):
            west = c * cell - 180.0
            near_lon = min(max(lon, west), west + cell)
            # Nearest point of the cell, on a flat earth: at 25 NM the
            # difference from a great circle is a fraction of a mile.
            dy = (near_lat - lat) * 60.0
            dx = (near_lon - lon) * 60.0 * coslat
            if dx * dx + dy * dy > radius_nm * radius_nm:
                continue
            index = 2 * (r * cols + (c % cols))
            value = int.from_bytes(data[index:index + 2], "big", signed=True)
            best = max(best, value)
    return best * FT_PER_M


def floor_ft(lat: float, lon: float, reference_ft: float,
             radius_nm: float = RADIUS_NM) -> float:
    """A minimum altitude for the circle round a point, MSA-style.

    The highest terrain plus 1000 ft, or plus 2000 ft where that terrain is
    more than 3000 ft above ``reference_ft`` -- the field being flown to --
    rounded up to the next hundred feet. Zero without a grid.
    """
    terrain = highest_ft(lat, lon, radius_nm)
    if terrain <= 0.0:
        return 0.0
    margin = (MOUNTAIN_MARGIN_FT if terrain - reference_ft > MOUNTAINOUS_ABOVE_FT
              else MARGIN_FT)
    return float(math.ceil((terrain + margin) / 100.0) * 100)


__all__ = ["highest_ft", "floor_ft", "available", "RADIUS_NM", "GRID"]
