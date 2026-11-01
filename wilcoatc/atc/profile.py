# -*- coding: utf-8 -*-
"""What kind of flying this is, as the pilot said it.

Everything that differs between an airline flight and a light aeroplane's
used to be deduced, in two places that did not agree: the first officer
classed the aeroplane from the simulator (``aircraft_class``), and the cabin,
the callsign, the stand and the cruise level asked whether the callsign was
shaped like an airline's and the type was in ``speech.is_airliner``'s list.
Neither ever kept the cabin out of a Cessna -- the purser welcomed the
passengers of a C172 whenever ``immersion.cabin`` was on -- and the region
of phraseology was the controller's ICAO prefix with nothing the pilot could
say against it.

So the pilot says it, once, and this is the answer every one of those places
reads (``FlightSession.profile``): the profile (``flight.profile``), and the
region (``flight.region``, "auto" for the field's own). The rules stay where
they always were, ``flight.ifr`` at the start and ``FlightSession.ifr`` for
the flight, because an explicit radio request still changes them.

The deductions are not gone. Inside a profile the simulator still decides
*which* aeroplane of that kind this is -- a piston single or a business jet
for general aviation, a jet or a turboprop for an airliner -- and where what
it reports is not of the kind at all, it is a question for the pilot
(:meth:`FlightProfile.questioned`), never an answer.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import aircraft_class as ac

AIRLINER = "airliner"
GENERAL_AVIATION = "general_aviation"
HELICOPTER = "helicopter"
PROFILES: tuple[str, ...] = (AIRLINER, GENERAL_AVIATION, HELICOPTER)

AUTO = "auto"
#: The regions ``navdata.db.vfr_region`` knows, which is what can be pinned.
REGIONS: tuple[str, ...] = (AUTO, "faa", "icao", "uk", "fr", "de", "metric")

# The classes each profile can be. A turboprop twin is in both: an ATR is an
# airliner and a King Air is not, and the simulator reports both alike.
_FAMILY: dict[str, frozenset[str]] = {
    AIRLINER: frozenset({ac.AIRLINER, ac.TURBOPROP_TWIN}),
    GENERAL_AVIATION: frozenset({ac.PISTON_SINGLE, ac.PISTON_TWIN,
                                 ac.TURBOPROP_SINGLE, ac.TURBOPROP_TWIN,
                                 ac.BUSINESS_JET}),
    HELICOPTER: frozenset({ac.HELICOPTER}),
}
# What a profile is while the simulator reports something outside it, until
# the pilot answers the question.
_BASE: dict[str, str] = {AIRLINER: ac.AIRLINER,
                         GENERAL_AVIATION: ac.PISTON_SINGLE,
                         HELICOPTER: ac.HELICOPTER}
# And the profile a class suggests, for that question.
_SUGGESTED: dict[str, str] = {ac.AIRLINER: AIRLINER,
                              ac.HELICOPTER: HELICOPTER}


def suggested(found: ac.AircraftClass) -> str:
    """The profile a pilot in this aeroplane would most likely pick."""
    return _SUGGESTED.get(found.name, GENERAL_AVIATION)


@dataclass(frozen=True)
class FlightProfile:
    kind: str = AIRLINER
    region: str = AUTO

    @classmethod
    def of(cls, flight) -> "FlightProfile":
        """From ``config.flight``; anything unknown is the default."""
        kind = getattr(flight, "profile", AIRLINER)
        region = getattr(flight, "region", AUTO)
        return cls(kind if kind in PROFILES else AIRLINER,
                   region if region in REGIONS else AUTO)

    @property
    def airliner(self) -> bool:
        return self.kind == AIRLINER

    @property
    def cabin(self) -> bool:
        """Whether there is a cabin crew and passengers to address."""
        return self.kind == AIRLINER

    @property
    def pinned(self) -> bool:
        return self.region != AUTO

    # --- the aeroplane -------------------------------------------------

    def classify(self, type_code: str = "", **sim) -> ac.AircraftClass:
        """The class of the pilot's aeroplane, inside this profile."""
        found = ac.classify(type_code, airline=self.airliner, **sim)
        return self.within(found)

    def within(self, found: ac.AircraftClass) -> ac.AircraftClass:
        if found.name in _FAMILY[self.kind]:
            return found
        return ac.AircraftClass(
            _BASE[self.kind],
            f"the {self.kind.replace('_', ' ')} profile ({found.why})")

    def questioned(self, found: ac.AircraftClass) -> str:
        """The profile to offer when the simulator disagrees, or "".

        A class nothing was known about is no disagreement: it is the
        commonest class for want of anything better, not a report.
        """
        if found.guessed or found.name in _FAMILY[self.kind]:
            return ""
        return suggested(found)

    # --- the region ----------------------------------------------------

    def region_for(self, ident: str = "") -> str:
        """The VFR wording's region at a field: the pinned one, or its own."""
        if self.pinned:
            return self.region
        from ..navdata.db import vfr_region

        return vfr_region(ident or "")

    def dialect_for(self, ident: str = "") -> str:
        """FAA or ICAO numbers and pressures. A pinned FAA is FAA
        everywhere; any other pinned region is ICAO's."""
        if self.pinned:
            return "faa" if self.region == "faa" else "icao"
        from ..navdata.db import dialect_for

        return dialect_for(ident or "")


#: What a session built with nothing said is: the airliner, its own field's
#: region, which is what the program did before the pilot could say.
DEFAULT = FlightProfile()

__all__ = ["AIRLINER", "AUTO", "DEFAULT", "FlightProfile", "GENERAL_AVIATION",
           "HELICOPTER", "PROFILES", "REGIONS", "suggested"]
