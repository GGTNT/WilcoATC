# -*- coding: utf-8 -*-
"""Which instrument approach a runway actually has.

Every arrival was "the ILS": the ATIS announced it, approach told the pilot to
expect it and then cleared it, at every runway in the world. The navigation
data has no approach procedures at all, so there was nothing to check against,
and Innsbruck 26 -- which has no ILS -- was cleared for one.

The simulator knows. Its facility data lists each airport's published
approaches by type and runway (read by
:mod:`wilcoatc.integrations.approaches`, once per airport), and this module
turns that list into the one approach a controller names:

* what the pilot asked for, when the runway has it -- "request RNAV 25L";
* otherwise the ILS, then an RNAV, then whatever non-precision approach
  there is;
* and a visual approach when the runway has nothing published, which is
  never an ILS that does not exist.

Without the simulator nothing is known about any runway, and the answer is
the old one -- the ILS -- because the alternative would be telling every pilot
at a major airport to fly a visual approach. That is the one case left where
the answer can be wrong, and it is wrong exactly as before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..navdata.db import normalize_runway

# The facility data's approach types (MSFS SDK, AddToFacilityDefinition,
# APPROACH.TYPE), as a controller names them. GPS is the old name for what is
# now charted as RNAV (GNSS); a VOR/DME approach is flown and named as a VOR.
TYPE_NAMES = {
    1: "RNAV", 2: "VOR", 3: "NDB", 4: "ILS", 5: "LOC", 6: "SDF", 7: "LDA",
    8: "VOR", 9: "NDB", 10: "RNAV", 11: "LOC",
}

# Best first: a precision approach, then the RNAV that most runways now have,
# then the older non-precision ones.
PREFERENCE = ("ILS", "RNAV", "LOC", "LDA", "SDF", "VOR", "NDB")

VISUAL = "VISUAL"

# What a pilot may ask for, as heard, mapped to the name used here.
_ASKED = re.compile(
    r"\b(ils|rnav|gps|rnp|vor|ndb|loc|localizer|localiser|visual)\b")
_ASKED_NAMES = {
    "ils": "ILS", "rnav": "RNAV", "gps": "RNAV", "rnp": "RNAV", "vor": "VOR",
    "ndb": "NDB", "loc": "LOC", "localizer": "LOC", "localiser": "LOC",
    "visual": VISUAL,
}


@dataclass
class FieldApproaches:
    """The approaches one airport publishes, by runway end."""

    ident: str
    by_runway: dict[str, set[str]] = field(default_factory=dict)
    # The final approach fix and missed-approach altitudes, in feet, by
    # (runway, type); zero where the simulator did not say.
    heights: dict[tuple[str, str], tuple[float, float]] = field(default_factory=dict)

    def add(self, runway: str, kind: str, faf_ft: float = 0.0,
            missed_ft: float = 0.0) -> None:
        key = normalize_runway(runway)
        self.by_runway.setdefault(key, set()).add(kind)
        if faf_ft or missed_ft:
            self.heights[(key, kind)] = (float(faf_ft or 0.0), float(missed_ft or 0.0))

    def altitudes(self, runway: str, kind: str = "") -> tuple[float, float]:
        """(final approach fix, missed approach) for a runway's approach, in
        feet; the first of its approaches that has them when ``kind`` is not
        given or has none."""
        key = normalize_runway(runway)
        if kind and (key, kind) in self.heights:
            return self.heights[(key, kind)]
        for preferred in PREFERENCE:
            if (key, preferred) in self.heights:
                return self.heights[(key, preferred)]
        return 0.0, 0.0

    def types(self, runway: str) -> set[str]:
        return set(self.by_runway.get(normalize_runway(runway), set()))


def choose(available: set[str] | None, requested: str = "") -> str:
    """The approach to name for a runway.

    ``available`` is None when nothing is known about the runway, which keeps
    the ILS; an empty set is a runway known to have nothing, which is a
    visual approach.
    """
    requested = (requested or "").upper()
    if requested == VISUAL:
        return VISUAL
    if available is None:
        return requested or "ILS"
    if requested and requested in available:
        return requested
    for kind in PREFERENCE:
        if kind in available:
            return kind
    return VISUAL


def asked_for(text: str) -> str:
    """The approach type a pilot named, or empty."""
    found = _ASKED.search((text or "").lower())
    return _ASKED_NAMES.get(found.group(1), "") if found else ""


def runway_from(number: int, designator: int) -> str:
    """A runway ident out of the facility data's two numbers.

    Numbers above 36 are compass points for approaches to no runway (a
    circling approach), which have no end to land on and are dropped.
    """
    if not 1 <= int(number) <= 36:
        return ""
    side = {1: "L", 2: "R", 3: "C"}.get(int(designator), "")
    return f"{int(number):02d}{side}"


__all__ = ["FieldApproaches", "choose", "asked_for", "runway_from",
           "TYPE_NAMES", "PREFERENCE", "VISUAL"]
