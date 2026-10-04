# -*- coding: utf-8 -*-
"""Where an aeroplane is relative to the runway it is landing on.

Every arrival clearance used to be a radius round the airport reference
point: the approach clearance at eighteen miles, the handoff to the tower at
fifteen, the landing clearance at ten. A circle has no idea which way the
runway points, so an aeroplane nine miles the wrong side of the field -- on
the downwind, or flying past it -- was cleared to land on 25R exactly as if
it were on final.

A clearance belongs to a place on the approach instead: the *final-approach
cone*, the wedge either side of the extended centreline on the approach side
of the threshold, measured from the threshold rather than from the middle of
the airport. This module is that geometry and nothing else -- whether the
aeroplane is in the wedge, how far from the threshold it is, and whether it
is pointing down the runway -- so the arrival watch, the tower and the engine
all ask the same question.

The thresholds and headings are the navigation data's: the paved end of the
runway and its true heading. A displaced threshold is not modelled; it moves
the answer by a few hundred feet at most.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..navdata.build import bearing_deg, haversine_nm
from ..navdata.db import normalize_runway

# The half-width of the wedge. Thirty degrees either side of the final
# course is wider than any localizer and narrower than a base leg, which is
# the distinction the clearances care about.
CONE_DEG = 30.0
# Pointing down the runway: a track within this of the final course. Wider
# than the cone, because an aeroplane intercepting from a base is in the
# wedge before it has finished turning.
ALIGNED_DEG = 45.0


@dataclass(frozen=True)
class Final:
    """One landing direction: where its threshold is and which way it goes."""

    runway: str
    lat: float
    lon: float
    course: float          # true

    def distance(self, state) -> float:
        """Miles from the aeroplane to the threshold."""
        return haversine_nm(state.latitude, state.longitude, self.lat, self.lon)

    def offset(self, state) -> float:
        """Degrees between the aeroplane and the extended centreline, seen
        from the threshold: zero on the centreline, ninety abeam, 180 over
        the far end."""
        if self.distance(state) < 0.05:
            return 0.0
        towards = bearing_deg(self.lat, self.lon, state.latitude, state.longitude)
        outbound = (self.course + 180.0) % 360.0
        return abs(((towards - outbound) + 540.0) % 360.0 - 180.0)

    def approach_side(self, state) -> bool:
        """Before the threshold rather than past it."""
        return self.offset(state) < 90.0

    def aligned(self, state, tolerance: float = ALIGNED_DEG) -> bool:
        """Tracking down the runway rather than across or away from it."""
        track = course_over_ground(state)
        if track is None:
            return False
        return abs(((track - self.course) + 540.0) % 360.0 - 180.0) <= tolerance

    def in_cone(self, state, within_nm: float, half_angle: float = CONE_DEG,
                aligned: bool = True) -> bool:
        """In the wedge, inside a range of the threshold, and -- unless told
        otherwise -- pointing down it."""
        if self.distance(state) > within_nm:
            return False
        if self.offset(state) > half_angle:
            return False
        return self.aligned(state) if aligned else True


def course_over_ground(state) -> float | None:
    """The way the aeroplane is going.

    The ground track where the source has one, and the heading where it does
    not: a stand-in source leaves the track at zero. A real track of exactly
    north therefore reads as the heading, which is within the drift of it.
    """
    track = getattr(state, "track_true", None)
    heading = getattr(state, "heading_true", None)
    if track:
        return float(track)
    if heading is not None:
        return float(heading)
    return float(track) if track is not None else None


def final_for(navdb, ident: str, runway: str) -> Final | None:
    """The landing direction ``runway`` at ``ident``, or None if the data
    does not have that end with a position."""
    if not ident or not runway:
        return None
    wanted = normalize_runway(runway)
    for rw in navdb.runways(ident):
        for end, heading, lat, lon in rw.ends():
            if normalize_runway(end) != wanted:
                continue
            if lat is None or lon is None:
                return None
            if heading is None:
                from ..navdata.db import _heading_from_ident
                from ..navdata.magvar import to_true, variation

                magnetic = _heading_from_ident(end)
                if magnetic is None:
                    return None
                heading = to_true(magnetic, variation(lat, lon))
            return Final(end, float(lat), float(lon), float(heading))
    return None


__all__ = ["Final", "final_for", "course_over_ground", "CONE_DEG", "ALIGNED_DEG"]
