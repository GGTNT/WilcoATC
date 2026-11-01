# -*- coding: utf-8 -*-
"""The speeds a takeoff and an approach are flown at, with where each comes
from.

The first officer calls "rotate" and, on a jet, "V one" and "V two"; the
"speed" deviation call needs an approach speed; and the invented traffic
rotates at a speed too. None of it may be made up. So a speed is taken, in
this order, from:

1. what the pilot said this flight ("speeds one four five, one four seven,
   one five two", "approach speed sixty five");
2. what the model itself declares to the simulator (``DESIGN SPEED MIN
   ROTATION``, then ``DESIGN TAKEOFF SPEED``) -- the rotation speed only;
3. the table below, each entry with its source;
4. the class's figure, marked unverified -- the rotation speed only, so
   the first officer can say "rotate" and never a V1 nobody gave.

The jets are the hard case. Their speeds depend on the day's weight,
temperature, runway and flap, and come from the operator's performance
tool; no single figure is published. The table gives a jet a V1 only where
a document states a set at a stated weight (the 737-800 below), flagged
unverified because that weight is not today's, and otherwise a rotation
speed alone. A pilot who says their speeds gets them called.

This replaces ``flying.ROTATE_KT``, which had a figure per type and no
source for any of them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .aircraft_class import (AIRLINER, BUSINESS_JET, HELICOPTER, PISTON_SINGLE,
                             PISTON_TWIN, TURBOPROP_SINGLE, TURBOPROP_TWIN)


@dataclass(frozen=True)
class Speeds:
    """Indicated knots; None where nothing gives one."""

    vr: float
    v1: float | None = None
    v2: float | None = None
    # The speed the final approach is flown at (VREF plus the type's
    # additive, or the handbook's "final approach" figure).
    vapp: float | None = None
    source: str = ""
    unverified: bool = False


@dataclass(frozen=True)
class Stated:
    """What the pilot said this flight. None for what they did not."""

    v1: float | None = None
    vr: float | None = None
    v2: float | None = None
    vapp: float | None = None


def _u(vr, source, **more) -> Speeds:
    return Speeds(vr=vr, source=f"UNVERIFIED: {source}", unverified=True,
                  **more)


# The sourced entries first. Where the handbook gives a range the middle of
# it, where it gives a figure by weight the one near a typical training
# weight -- and the source says which.
TABLE: dict[str, Speeds] = {
    "C172": Speeds(
        vr=55, vapp=65,
        source="Cessna 172S Information Manual, normal procedures: rotate at "
               "55 KIAS; normal landing approach 60-70 KIAS with 30 degrees of "
               "flap (65 used), as the C172 maneuver guides of Thrust Flight "
               "and Southeastern Oklahoma State University repeat it"),
    "PA28": Speeds(
        vr=60, vapp=66,
        source="Piper PA-28-181 Archer III checklist (Middle Georgia State "
               "University, 2024): normal rotation 60 KIAS; Thrust Flight's "
               "Archer guide the same. The approach figure is UNVERIFIED",
        unverified=True),
    "DA40": Speeds(
        vr=59, vapp=70,
        source="Diamond Flight Training DA40 normal procedures card, ed. 1.3: "
               "rotation 56-67 KIAS by weight (59 at about 1,050 kg), VREF 70 "
               "KIAS at 1,000 kg; it does not replace the AFM"),
    "SR22": Speeds(
        vr=70, vapp=80,
        source="Cirrus SR22 POH as quoted by its pilots: rotate smoothly at "
               "70-73 KIAS; 80 KIAS on final"),
    "TBM9": Speeds(
        vr=90, vapp=85,
        source="TBM 930: rotate at 90 KIAS with takeoff flap (a pilot quoting "
               "the PIM, flightsim.com); VREF 83 KIAS at maximum landing "
               "weight, flown as 85 (AOPA Turbine Pilot, January 2021)"),
    "C25A": _u(105, "Citation CJ2: the AFM's speeds are by weight and none "
                    "is published; a typical rotation figure"),
    "A320": _u(140, "A320: the speeds come from the day's performance "
                    "calculation and no single figure is published; a "
                    "mid-weight rotation figure"),
    "B738": Speeds(
        v1=128, vr=132, v2=141, vapp=140,
        source="UNVERIFIED: 737-800 at 60.7 t, flaps 5 and landing flaps 30, "
               "from PMFlight's 737 Technical training aid -- not Boeing's "
               "data, and not today's weight",
        unverified=True),
}
TABLE["P28A"] = TABLE["PA28"]
TABLE["TBM8"] = TABLE["TBM7"] = replace(TABLE["TBM9"], unverified=True,
                                        source="UNVERIFIED: as the TBM 930")

# The traffic's other types, kept from the table this one replaced so a 777
# is still on the runway when an A320 has gone. None is sourced: a
# mid-range weight's rotation speed.
for _code, _vr in {
        "A319": 135, "A20N": 140, "A321": 150, "A21N": 148, "A306": 150,
        "A333": 155, "A359": 155, "A388": 160, "B38M": 148, "B37M": 145,
        "B752": 145, "B753": 150, "B763": 150, "B77W": 170, "B77L": 165,
        "B788": 155, "B789": 160, "B78X": 162, "E190": 135, "E195": 138,
        "E75L": 130, "CRJ9": 140, "AT76": 110, "AT3": 105, "DH8D": 115,
        "PC12": 80, "C152": 50, "C182": 60, "PA34": 75, "DA42": 70,
        "BE36": 70, "TB20": 70}.items():
    TABLE.setdefault(_code, _u(_vr, "a mid-range weight's rotation speed"))
del _code, _vr

# The class's figures, for a type the table does not have.
CLASS_FIGURES: dict[str, Speeds] = {
    PISTON_SINGLE: _u(55, "a light single's rotation speed", vapp=65),
    PISTON_TWIN: _u(75, "a light twin's rotation speed", vapp=85),
    TURBOPROP_SINGLE: _u(85, "a single turboprop's rotation speed", vapp=85),
    TURBOPROP_TWIN: _u(105, "a twin turboprop's rotation speed"),
    BUSINESS_JET: _u(110, "a business jet's rotation speed"),
    AIRLINER: _u(140, "an airliner's rotation speed"),
    HELICOPTER: _u(0, "a helicopter does not rotate"),
}


def table_speeds(type_code: str, aircraft_class: str = AIRLINER) -> Speeds:
    """The table's entry, or the class's figure."""
    code = (type_code or "").strip().upper()
    if code in TABLE:
        return TABLE[code]
    return CLASS_FIGURES.get(aircraft_class, CLASS_FIGURES[AIRLINER])


def speeds_for(type_code: str, aircraft_class: str, *,
               stated: Stated | None = None, min_rotation_kt: float = 0.0,
               design_takeoff_kt: float = 0.0) -> Speeds:
    """The speeds to call this flight, best evidence first."""
    found = table_speeds(type_code, aircraft_class)
    sim_vr = min_rotation_kt or design_takeoff_kt
    if sim_vr and sim_vr > 0:
        which = ("DESIGN SPEED MIN ROTATION" if min_rotation_kt
                 else "DESIGN TAKEOFF SPEED")
        found = replace(found, vr=float(sim_vr), unverified=False,
                        source=f"the simulator: the model's {which}; "
                               f"the rest from {found.source}")
    if stated is not None and any(v is not None for v in (
            stated.v1, stated.vr, stated.v2, stated.vapp)):
        found = replace(
            found,
            v1=stated.v1 if stated.v1 is not None else found.v1,
            vr=stated.vr if stated.vr is not None else found.vr,
            v2=stated.v2 if stated.v2 is not None else found.v2,
            vapp=stated.vapp if stated.vapp is not None else found.vapp,
            source=f"the pilot, this flight; the rest from {found.source}")
    return found


# Where the traffic rotates at another figure than the table's: the table
# gives a type's speeds at the weight its source states, and an airliner
# rotating at that light weight after a thousand metres looks wrong from
# the tower.
TRAFFIC_VR: dict[str, float] = {
    # UNVERIFIED: a 737-800 at about 70 t, rotating at about 145 kt.
    "B738": 145.0,
}


def rotate_kt(type_code: str) -> float | None:
    """The traffic's rotation speed, or None for a type the table does
    not have."""
    code = (type_code or "").strip().upper()
    if code in TRAFFIC_VR:
        return TRAFFIC_VR[code]
    entry = TABLE.get(code)
    return float(entry.vr) if entry is not None else None


__all__ = ["CLASS_FIGURES", "Speeds", "Stated", "TABLE", "TRAFFIC_VR", "rotate_kt",
           "speeds_for", "table_speeds"]
