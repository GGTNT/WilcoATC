# -*- coding: utf-8 -*-
"""Which kind of aeroplane the pilot is flying, for the first officer.

A checklist and a callout belong to a class of aeroplane, not to a type: a
C172 and a DA40 run the same before-takeoff run-up, an A320 and a 737 call
"eighty knots" and "V one" alike, and nobody calls V1 in a Cessna. Seven
classes: piston single, piston twin, turboprop single, turboprop twin,
business jet, airliner and helicopter.

Nothing in the program classified the pilot's own aeroplane before this.
``fleet.py`` chooses aeroplanes for a field's invented traffic from short
lists, and its "business" list has two turboprops in it (PC12, TBM9);
``handling.is_light`` asks only whether the ground crew bring stairs. So the
class is decided here, from the best evidence there is, in this order:

1. What the simulator says about the model it is flying: ``CATEGORY``
   ("Helicopter"), ``ENGINE TYPE`` and ``NUMBER OF ENGINES``. That is the
   aeroplane itself, whatever its ``ATC MODEL`` claims -- an add-on that
   files itself as "C172" with a turbine conversion is a turboprop.
2. The type's ICAO Doc 8643 description (``L1P`` is a landplane with one
   piston engine), for the types a simulator pilot flies.
3. For a jet, whether it is a business jet: the business jets
   ``turnaround.json`` already lists, else an airline callsign (an airliner)
   or none (a private jet).

With none of it -- a type nobody knows, before the simulator has answered
-- the class is a piston single, the commonest aeroplane in the hobby.
UNVERIFIED: that default is a guess about who flies, not a measurement.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PISTON_SINGLE = "piston_single"
PISTON_TWIN = "piston_twin"
TURBOPROP_SINGLE = "turboprop_single"
TURBOPROP_TWIN = "turboprop_twin"
BUSINESS_JET = "business_jet"
AIRLINER = "airliner"
HELICOPTER = "helicopter"

CLASSES: tuple[str, ...] = (PISTON_SINGLE, PISTON_TWIN, TURBOPROP_SINGLE,
                            TURBOPROP_TWIN, BUSINESS_JET, AIRLINER, HELICOPTER)

#: Callouts only for these: no checklist is written for them yet.
CALLOUTS_ONLY = frozenset({HELICOPTER})

# ``ENGINE TYPE`` as the simulator enumerates it: 0 piston, 1 jet, 2 none,
# 3 helicopter turbine, 4 unsupported, 5 turboprop -- the SimConnect
# reference, as the wrapper's own request table (SimConnect/RequestList.py)
# copies it. UNVERIFIED on a running 2024: the SDK's files only show the
# variable's name.
SIM_PISTON, SIM_JET, SIM_NONE, SIM_HELO_TURBINE, SIM_UNSUPPORTED, SIM_TURBOPROP = range(6)

# ICAO Doc 8643 (Aircraft Type Designators) descriptions: the kind of
# aircraft (L landplane, A amphibian, H helicopter), the number of engines,
# and their kind (P piston, T turboprop/turboshaft, J jet). The types are
# the ones the simulator's own fleet, ``fleet.py`` and the title table in
# ``sim/source.py`` name; anything else reaches the simulator's answer or
# the airliner prefixes below.
DOC_8643: dict[str, str] = {
    # piston singles
    "C150": "L1P", "C152": "L1P", "C170": "L1P", "C172": "L1P",
    "C177": "L1P", "C182": "L1P", "C206": "L1P", "C210": "L1P",
    "P28A": "L1P", "PA28": "L1P", "P28R": "L1P", "PA18": "L1P",
    "J3": "L1P", "PA24": "L1P", "PA46": "L1P", "SR20": "L1P", "SR22": "L1P",
    "DA40": "L1P", "DR40": "L1P", "BE36": "L1P", "BE35": "L1P",
    "M20P": "L1P", "M20T": "L1P", "TB20": "L1P", "TB10": "L1P",
    "BL8": "L1P", "A5": "A1P", "CUB": "L1P", "SAVG": "L1P", "VL3": "L1P",
    "C42": "L1P", "DV20": "L1P", "PTS2": "L1P", "EXTR": "L1P", "AT3": "L1P",
    "RV7": "L1P", "RV8": "L1P", "RV10": "L1P", "CRUZ": "L1P",
    # piston twins
    "DA42": "L2P", "DA62": "L2P", "PA34": "L2P", "PA44": "L2P",
    "BE58": "L2P", "BE55": "L2P", "C310": "L2P", "C340": "L2P",
    "P68": "L2P",
    # turboprop singles
    "TBM9": "L1T", "TBM8": "L1T", "TBM7": "L1T", "PC12": "L1T",
    "PC6T": "L1T", "C208": "L1T", "KODI": "L1T", "AT8T": "L1T",
    "PC21": "L1T", "PC9": "L1T", "M600": "L1T",
    # turboprop twins
    "B350": "L2T", "BE20": "L2T", "BE9L": "L2T", "C441": "L2T",
    "DHC6": "L2T", "AT43": "L2T", "AT45": "L2T", "AT72": "L2T",
    "AT76": "L2T", "DH8A": "L2T", "DH8B": "L2T", "DH8C": "L2T",
    "DH8D": "L2T", "SF34": "L2T", "JS41": "L2T", "C130": "L4T",
    "P180": "L2T",
    # jets the business list below does not have
    "SF50": "L1J",
    # helicopters
    "R22": "H1P", "R44": "H1P", "R66": "H1T", "CBRI": "H1P",
    "AS50": "H1T", "EC30": "H1T", "B06": "H1T", "B407": "H1T",
    "EC35": "H2T", "EC45": "H2T", "H145": "H2T", "H160": "H2T",
    "S76": "H2T", "A109": "H2T", "A139": "H2T", "B412": "H2T",
}

# The airliners' designators begin like this; all of them are jets.
_AIRLINER_PREFIXES = ("A3", "A2", "B7", "B3", "BCS", "A19", "A20", "A21",
                      "E17", "E19", "E29", "E75", "CRJ", "MD8", "MD9", "MD1",
                      "B46", "RJ", "F70", "F100", "SU9", "C919", "A22")

TURNAROUND = Path(__file__).with_name("turnaround.json")


@lru_cache(maxsize=1)
def business_jets() -> frozenset[str]:
    """The business jets the ground handling already knows, plus a few."""
    try:
        table = json.loads(TURNAROUND.read_text(encoding="utf-8"))
        listed = {code for code, profile in table.get("types", {}).items()
                  if profile == "business"}
    except (OSError, ValueError):
        listed = set()
    return frozenset(listed | {"SF50", "HA4T", "C25M"})


@dataclass(frozen=True)
class AircraftClass:
    name: str
    #: What decided it, for ``logs crew``.
    why: str
    #: Nothing was known: the commonest class for want of anything better,
    #: which is no evidence against what the pilot said
    #: (``profile.FlightProfile.questioned``).
    guessed: bool = False

    @property
    def checklists(self) -> bool:
        return self.name not in CALLOUTS_ONLY


def _from_description(description: str) -> str:
    kind, engines, power = description[0], int(description[1]), description[2]
    if kind == "H":
        return HELICOPTER
    if power == "P":
        return PISTON_SINGLE if engines == 1 else PISTON_TWIN
    if power == "T":
        return TURBOPROP_SINGLE if engines == 1 else TURBOPROP_TWIN
    return ""                      # a jet: business or airliner, decided below


def _jet(code: str, engines: int, airline: bool, said: str) -> AircraftClass:
    if code in business_jets():
        return AircraftClass(BUSINESS_JET, f"{said}, {code} is a business jet")
    if code.startswith(_AIRLINER_PREFIXES):
        return AircraftClass(AIRLINER, f"{said}, {code} is an airliner")
    if engines >= 3:
        return AircraftClass(AIRLINER, f"{said}, {engines} engines")
    if airline:
        return AircraftClass(AIRLINER, f"{said}, an airline callsign")
    return AircraftClass(BUSINESS_JET, f"{said}, no airline")


def classify(type_code: str = "", *, engine_type: int | None = None,
             engines: int = 0, category: str = "",
             airline: bool = False) -> AircraftClass:
    """The class of the pilot's aeroplane.

    ``engine_type``, ``engines`` and ``category`` are the simulator's
    ``ENGINE TYPE``, ``NUMBER OF ENGINES`` and ``CATEGORY``, None/0/"" when
    it has not said; ``airline`` is whether the flight has an airline
    callsign.
    """
    code = (type_code or "").strip().upper()
    if (category or "").strip().lower() == "helicopter" \
            or engine_type == SIM_HELO_TURBINE:
        return AircraftClass(HELICOPTER, "the simulator: a helicopter")
    if engine_type in (SIM_PISTON, SIM_TURBOPROP) and engines > 0:
        piston = engine_type == SIM_PISTON
        name = ((PISTON_SINGLE if engines == 1 else PISTON_TWIN) if piston
                else (TURBOPROP_SINGLE if engines == 1 else TURBOPROP_TWIN))
        kind = "piston" if piston else "turboprop"
        return AircraftClass(name, f"the simulator: {engines} {kind}")
    if engine_type == SIM_JET:
        return _jet(code, engines, airline, "the simulator: a jet")

    description = DOC_8643.get(code)
    if description:
        name = _from_description(description)
        if name:
            return AircraftClass(name, f"ICAO Doc 8643: {code} is {description}")
        return _jet(code, int(description[1]), airline,
                    f"ICAO Doc 8643: {code} is {description}")
    if code in business_jets() or code.startswith(_AIRLINER_PREFIXES):
        return _jet(code, 0, airline, "the type")
    return AircraftClass(PISTON_SINGLE,
                         f"nothing known about {code or 'the type'}: "
                         "the commonest class", guessed=True)


def of_state(state) -> AircraftClass:
    """The class from an :class:`~wilcoatc.sim.state.AircraftState`."""
    engine_type = getattr(state, "engine_type", -1)
    return classify(state.type_code,
                    engine_type=None if engine_type < 0 else engine_type,
                    engines=getattr(state, "engine_count", 0),
                    category=getattr(state, "category", ""),
                    airline=bool(getattr(state, "airline", "")))


__all__ = ["AIRLINER", "AircraftClass", "BUSINESS_JET", "CLASSES", "DOC_8643",
           "HELICOPTER", "PISTON_SINGLE", "PISTON_TWIN", "TURBOPROP_SINGLE",
           "TURBOPROP_TWIN", "classify", "of_state"]
