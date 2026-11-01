# -*- coding: utf-8 -*-
"""Who is on the runway, and who is about to be.

The tower's view of its runway was the invented traffic's own bookkeeping --
``TrafficInjector.runway_is_busy``, and ``landing_on_top`` over its list of
arrivals. An FSLTL airliner rolling out, or the simulator's own Cessna lined
up, was not on the runway as far as the tower knew, so the pilot was cleared
to land on top of it, or cleared to take off in front of something on a
two-mile final.

This reads the picture the traffic calls read
(``ControllerBrain.contacts``, the invented traffic and everything the
simulator reports) against the runway's own geometry: an aeroplane on the
strip, and an aeroplane in the final-approach cone of an end
(:mod:`wilcoatc.atc.final`).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from .circuit import runway_frame, runway_length_nm
from .final import CONE_DEG, final_for

# On the runway: within this of the centreline. A holding point is 30 to 90 m
# off it (ICAO Annex 14 table 3-2), so the plain figure is under the smallest
# of those, and a wider one is taken only for an aeroplane pointing along
# the runway -- lined up or rolling, not waiting at the side of it.
ACROSS_NM = 0.015
ACROSS_ALIGNED_NM = 0.04
ALIGNED_DEG = 30.0
# Off the ground but still over the strip: a departure that has just
# rotated is still the runway's until it is past this.
STILL_ON_IT_FT = 100.0
# On final: in the cone, below this above the field, pointing down it.
FINAL_BELOW_FT = 3000.0


@dataclass(frozen=True)
class OnRunway:
    contact: object
    along_nm: float
    moving: bool

    def describe(self, language: str = "en") -> str:
        return self.contact.describe(language)

    @property
    def written(self) -> str:
        return self.contact.written


@dataclass(frozen=True)
class OnFinal:
    """An arrival in the final-approach cone; shaped like the invented
    traffic's flights where the controller reads those (``distance_nm``,
    ``describe``, ``type_code``)."""

    contact: object
    distance_nm: float

    def describe(self, language: str = "en") -> str:
        return self.contact.describe(language)

    def indefinite(self, language: str = "en") -> str:
        return self.contact.indefinite(language)

    @property
    def written(self) -> str:
        return self.contact.written

    @property
    def type_code(self) -> str:
        return self.contact.type_code


def _turn(a: float, b: float) -> float:
    return abs(((a - b) + 540.0) % 360.0 - 180.0)


def on_the_runway(navdb, airport, runway: str, contacts) -> list[OnRunway]:
    """Everybody on the strip of ``runway`` (either end), nearest the
    threshold first."""
    final = final_for(navdb, getattr(airport, "ident", ""), runway)
    if final is None:
        return []
    length = runway_length_nm(navdb, airport.ident, runway)
    elevation = float(getattr(airport, "elev_ft", 0.0) or 0.0)
    found = []
    for contact in contacts:
        if not contact.on_ground and contact.altitude_ft - elevation > STILL_ON_IT_FT:
            continue
        along, across = runway_frame(contact.latitude, contact.longitude,
                                     final.lat, final.lon, final.course)
        if not -0.05 <= along <= length + 0.05:
            continue
        axis = min(_turn(contact.heading, final.course),
                   _turn(contact.heading, final.course + 180.0))
        width = ACROSS_ALIGNED_NM if axis <= ALIGNED_DEG else ACROSS_NM
        if abs(across) > width:
            continue
        found.append(OnRunway(contact, along, contact.ground_speed_kt >= 5.0))
    found.sort(key=lambda one: one.along_nm)
    return found


def on_final(navdb, airport, runway: str, contacts,
             within_nm: float) -> list[OnFinal]:
    """The arrivals in ``runway``'s final-approach cone inside ``within_nm``
    of its threshold, nearest first."""
    final = final_for(navdb, getattr(airport, "ident", ""), runway)
    if final is None:
        return []
    elevation = float(getattr(airport, "elev_ft", 0.0) or 0.0)
    found = []
    for contact in contacts:
        if contact.on_ground:
            continue
        if contact.altitude_ft - elevation > FINAL_BELOW_FT:
            continue
        seen = SimpleNamespace(latitude=contact.latitude,
                               longitude=contact.longitude,
                               track_true=contact.heading,
                               heading_true=contact.heading)
        if not final.in_cone(seen, within_nm, CONE_DEG):
            continue
        found.append(OnFinal(contact, final.distance(seen)))
    found.sort(key=lambda one: one.distance_nm)
    return found


__all__ = ["OnFinal", "OnRunway", "on_final", "on_the_runway"]
