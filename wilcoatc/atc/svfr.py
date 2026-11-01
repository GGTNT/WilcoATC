# -*- coding: utf-8 -*-
"""Special VFR: the weather a VFR flight may not fly in, and the way round it.

The tower released a VFR departure into a hundred-foot overcast exactly as it
did into a clear sky, because nothing between the METAR and the release ever
asked what the weather was. ``weather.metar.flight_category`` worked out VFR,
IFR and the rest and nothing read it.

This module answers three questions about a field's reported weather, each
the region's own way (``navdata.db.vfr_region``):

* whether it is VMC *in the control zone* -- the figures that let a VFR flight
  take off, land or enter the zone without anything more;
* failing that, whether Special VFR may be given at all;
* and whether it is night, which changes the second answer in some places.

The figures, and where they come from:

FAA: 14 CFR 91.155(c) and (d) -- no VFR under a ceiling below 1,000 ft or in
ground visibility below 3 statute miles in a surface area -- and 91.157 for
Special VFR: clear of clouds, flight visibility 1 SM, and (d) the same
ground visibility to take off or land. 91.157(c) allows it at night only to
an instrument-rated pilot in an aeroplane equipped for IFR; that is the
pilot's to know, not the controller's, so the controller here gives it.

Everywhere else: SERA.5005(b) for the control-zone VMC (ceiling 1,500 ft,
ground visibility 5 km) and SERA.5010 for Special VFR (ground visibility
1,500 m and ceiling 600 ft to take off, land or enter). The ICAO and metric
regions take the same figures: Annex 2 has the same control-zone VMC, and
UNVERIFIED: that their states set Special VFR minima of SERA's.

Night Special VFR was refused everywhere outside the US. SERA.5010 allows
it subject to each state's own rules, and none of those was read, so a
refusal was a rule nobody had a source for: it is given everywhere, and a
region is refused only once a source says so (``_NIGHT_REFUSED``, empty).
UNVERIFIED: that no state in any region refuses it.

What it does not model: a ceiling the controller has no METAR for (an
empty cloud list is "no ceiling"), and fields at which the FAA does not
allow Special VFR at all (14 CFR part 91 appendix D, section 3).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

STATUTE_MILE_M = 1609.34

# The control-zone VMC: (ceiling ft, ground visibility m).
_VMC = {"faa": (1000.0, 3.0 * STATUTE_MILE_M)}
_VMC_DEFAULT = (1500.0, 5000.0)

# The Special VFR minima, the same shape. The FAA has no ceiling, only
# "clear of clouds".
_SVFR = {"faa": (0.0, 1.0 * STATUTE_MILE_M)}
_SVFR_DEFAULT = (600.0, 1500.0)

# Where night Special VFR is refused, each with its source. None is known.
_NIGHT_REFUSED: frozenset[str] = frozenset()

# The sun's centre this far below the horizon begins the night: sunset for
# the FAA (91.157(c) says "between sunset and sunrise"), the end of civil
# twilight for SERA's definition of night.
_NIGHT_BELOW_DEG = {"faa": -0.833}
_NIGHT_BELOW_DEFAULT = -6.0


def ceiling_ft(weather) -> float:
    """The lowest broken, overcast or obscured layer, above the field; inf
    with none reported.

    Where the simulator's weather is the field's (``weather.source`` "sim",
    near the field: the same rule as the wind and the visibility), the
    layers are not the field's: the simulator reports none, and the ones in
    the merged weather are the real-world METAR's. Centennial's real BKN004
    refused a VFR departure out of a clear simulated sky.
    """
    if str(getattr(weather, "source", "") or "").startswith("sim"):
        return math.inf
    lowest = math.inf
    for cover, height in getattr(weather, "clouds", None) or []:
        if cover in ("BKN", "OVC", "VV"):
            lowest = min(lowest, float(height))
    return lowest


def visibility_m(weather) -> float:
    return float(getattr(weather, "visibility_sm", 10.0) or 0.0) * STATUTE_MILE_M


def is_night(lat: float, lon: float, region: str,
             when: datetime | None = None) -> bool:
    from .afis import _sun_elevation

    when = when or datetime.now(timezone.utc)
    below = _NIGHT_BELOW_DEG.get(region, _NIGHT_BELOW_DEFAULT)
    return _sun_elevation(when, lat, lon) < below


@dataclass(frozen=True)
class Conditions:
    """A field's weather against the rules of its region."""

    region: str
    ceiling_ft: float
    visibility_m: float
    vmc: bool
    night: bool
    # When ``vmc`` is False: whether Special VFR may be given, and if not,
    # which limit stops it ("visibility", "ceiling", "night").
    svfr: bool
    refused_for: str = ""

    def as_log(self) -> dict:
        return {"region": self.region,
                "ceiling_ft": (None if math.isinf(self.ceiling_ft)
                               else int(self.ceiling_ft)),
                "visibility_m": int(round(self.visibility_m)),
                "vmc": self.vmc, "night": self.night, "svfr": self.svfr,
                "refused_for": self.refused_for or None}


def conditions(weather, region: str, lat: float, lon: float,
               when: datetime | None = None) -> Conditions:
    ceiling = ceiling_ft(weather)
    seen = visibility_m(weather)
    vmc_ceiling, vmc_visibility = _VMC.get(region, _VMC_DEFAULT)
    vmc = ceiling >= vmc_ceiling and seen >= vmc_visibility
    night = is_night(lat, lon, region, when)
    if vmc:
        return Conditions(region, ceiling, seen, True, night, True)
    svfr_ceiling, svfr_visibility = _SVFR.get(region, _SVFR_DEFAULT)
    refused = ""
    if seen < svfr_visibility:
        refused = "visibility"
    elif ceiling < svfr_ceiling:
        refused = "ceiling"
    elif night and region in _NIGHT_REFUSED:
        refused = "night"
    return Conditions(region, ceiling, seen, False, night, not refused, refused)


def svfr_altitude(elevation_ft: float, ceiling_agl_ft: float,
                  vfr_ceiling_ft: float) -> float:
    """The "not above" for a Special VFR clearance, in feet AMSL.

    UNVERIFIED: the rule. A Special VFR flight stays clear of cloud, so the
    figure is two hundred feet under the cloud base, down to the hundred,
    never under five hundred feet above the field and never over the
    ceiling an ordinary release would have had (under the airspace above).
    """
    top = vfr_ceiling_ft or (elevation_ft + 1500.0)
    if math.isinf(ceiling_agl_ft):
        return float(top)
    under_cloud = math.floor((elevation_ft + ceiling_agl_ft - 200.0) / 100.0) * 100.0
    floor = math.ceil((elevation_ft + 500.0) / 100.0) * 100.0
    return float(min(top, max(floor, under_cloud)))


__all__ = ["Conditions", "ceiling_ft", "conditions", "is_night",
           "svfr_altitude", "visibility_m"]
