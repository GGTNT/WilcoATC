# -*- coding: utf-8 -*-
"""Which aeroplanes a field has: an airport's airliners or an airfield's
light aircraft, and the rule that decided.

Flown at Kortrijk-Wevelgem, a 1,900 m general-aviation field with no
scheduled airline: its ramp and circuit were A321s. That flight had the test
setting ``traffic.debug.force_types: [A321]`` on, but the rule underneath
was thin as well. "Airline field" was ``size_rank >= 3`` alone -- the
OurAirports type, nothing about whether anybody flies a schedule there --
the light fleet was one list with a helicopter in it and the business jets
mixed in at any field however short its runway, and nothing said which rule
had chosen. The name was never read, and must not be: Kortrijk is
"Kortrijk-Wevelgem International Airport".

So, in this order (:func:`classify`):

* a size given by hand (the suite, the panel) decides as before: rank 3 and
  up is an airline field;
* a field with scheduled service (OurAirports' ``scheduled_service``) is an
  airline field;
* a large airport with no scheduled-service flag but a 2,500 m runway is
  taken for one whose flag is missing;
* everything else is an airfield: light singles, a few twins, and business
  aircraft only where the runway is 1,500 m or more on a medium field.

UNVERIFIED: the business-aviation test. Nothing read says where customs or a
business terminal is, so a 1,500 m runway on a field OurAirports calls medium
stands in for it. UNVERIFIED: every share below -- chosen, not measured.

The same rule says how busy the field is. Kortrijk had four aeroplanes on
the move and sixteen in the status, where an AFIS field like it sees one or
two movements every ten to fifteen minutes. The size rank cannot tell it
from Centennial, one of the busiest general-aviation fields in the United
States: both are rank 2. What can is a tower. So an airfield is
``"airfield"`` without one -- an AFIS, a traffic frequency -- and
``"ga_field"`` with one, and each has how many aeroplanes are in play at
once and how often one starts (``in_play``, ``movement_gap_s``); an airline
field is ``"airport"`` and keeps the injector's own figures.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..navdata.db import NavDB

FT_PER_M = 3.28084

# The light singles of every general-aviation field, and the Robin that only
# Europe has. No helicopter: they flew the same circuit as the Cessnas.
SINGLES: tuple[str, ...] = (
    "C172", "C152", "C182", "P28A", "PA28", "DA40", "SR22", "BE36", "TB20",
)
EUROPE_SINGLES: tuple[str, ...] = ("DR40",)
TWINS: tuple[str, ...] = ("DA42", "PA34", "BE58")
BUSINESS: tuple[str, ...] = ("C25A", "C56X", "PC12", "TBM9")
GA_TYPES: tuple[str, ...] = SINGLES + EUROPE_SINGLES + TWINS + BUSINESS

# Of an airfield's aeroplanes: a few twins, and where the runway takes them a
# small share of business aircraft. UNVERIFIED: the figures.
TWIN_SHARE = 0.10
BUSINESS_SHARE = 0.06

# The runway that admits business aircraft, and the one that makes a large
# airport with no scheduled-service flag an airline field all the same.
BUSINESS_RUNWAY_M = 1500.0
AIRLINE_RUNWAY_M = 2500.0

# How many of an airfield's VFR flights are circuits, by what the field is:
# a strip is mostly circuits, a field with business traffic mostly comes and
# goes. UNVERIFIED: the figures; 0.45 was the one share used everywhere.
CIRCUITS_STRIP = 0.60
CIRCUITS_AIRFIELD = 0.45
CIRCUITS_BUSY_AIRFIELD = 0.35
# The size rank at and above which a field is an airline field when a size
# is given by hand.
AIRLINE_RANK = 3

# How much traffic a general-aviation field has: aeroplanes in play at once
# (moving, or booked to leave the apron), and seconds between one starting
# and the next, both before ``traffic.density``. UNVERIFIED: chosen from
# what was asked for after Kortrijk (an airfield two or three in play and a
# movement about every eight minutes; a towered field four to six), not
# measured at any field.
AIRFIELD_IN_PLAY = 3
AIRFIELD_MOVEMENT_GAP_S = 480.0
GA_FIELD_IN_PLAY = 5
GA_FIELD_MOVEMENT_GAP_S = 240.0

# How many aeroplanes stand on the apron, by kind of field, out of an
# airport's. ``traffic.parked`` is the airport's figure; the others are made
# from it in this proportion, so turning it down turns every field down.
# UNVERIFIED: chosen (an airfield four to six, a towered field eight), not
# counted at any field.
PARKED_BY_SIZE = {"airfield": 5, "ga_field": 8, "airport": 12}


def parked_for(kind: "FieldKind | None", configured: int) -> int:
    """How many to park at this field, given ``traffic.parked``."""
    size = kind.size if kind is not None else "airport"
    share = PARKED_BY_SIZE.get(size, PARKED_BY_SIZE["airport"])
    return int(round(max(0, configured) * share / PARKED_BY_SIZE["airport"]))


@dataclass(frozen=True)
class FieldKind:
    """What a field's traffic is, and why."""

    airline: bool
    business: bool
    circuit_share: float
    rule: str
    scheduled: bool = False
    size_rank: int = 0
    runway_m: float = 0.0
    europe: bool = False
    # "airport", "ga_field" (a tower) or "airfield" (none), and the traffic
    # that goes with it; None for an airport, which keeps the injector's.
    size: str = "airport"
    towered: bool = False
    in_play: int | None = None
    movement_gap_s: float | None = None

    def log_fields(self) -> dict:
        return {"airline": self.airline, "business_jets": self.business,
                "circuit_share": self.circuit_share, "rule": self.rule,
                "scheduled": self.scheduled, "size_rank": self.size_rank,
                "runway_m": round(self.runway_m), "size": self.size,
                "towered": self.towered, "in_play": self.in_play,
                "movement_gap_s": (round(self.movement_gap_s)
                                   if self.movement_gap_s else None)}


def longest_runway_m(navdb: NavDB, ident: str) -> float:
    lengths = [float(rw.length_ft or 0.0) for rw in navdb.runways(ident)]
    return max(lengths, default=0.0) / FT_PER_M


def classify(navdb: NavDB, ident: str, size_rank: int | None = None) -> FieldKind:
    airport = navdb.airport(ident)
    rank = int(size_rank if size_rank is not None
               else (airport.size_rank if airport else 3))
    scheduled = bool(airport.scheduled) if airport else False
    runway_m = longest_runway_m(navdb, ident)
    europe = (ident or "").upper()[:1] in ("E", "L")
    common = dict(scheduled=scheduled, size_rank=rank, runway_m=runway_m,
                  europe=europe)
    if size_rank is not None:
        airline = rank >= AIRLINE_RANK
        rule = f"size rank {rank} given by hand"
    elif scheduled:
        airline, rule = True, "scheduled service"
    elif rank >= AIRLINE_RANK and runway_m >= AIRLINE_RUNWAY_M:
        airline = True
        rule = (f"large airport, {runway_m:.0f} m runway, "
                "no scheduled-service flag")
    else:
        airline = False
        rule = f"no scheduled service, size rank {rank}, {runway_m:.0f} m runway"
    if airline:
        return FieldKind(True, False, 0.0, rule, **common)
    business = rank >= 2 and runway_m >= BUSINESS_RUNWAY_M
    if business:
        rule += ", business aircraft"
    share = (CIRCUITS_STRIP if rank <= 1
             else CIRCUITS_BUSY_AIRFIELD if business else CIRCUITS_AIRFIELD)
    towered = any(station.position == "TWR"
                  for station in navdb.stations(ident))
    if towered:
        traffic = dict(size="ga_field", towered=True,
                       in_play=GA_FIELD_IN_PLAY,
                       movement_gap_s=GA_FIELD_MOVEMENT_GAP_S)
        rule += ", a tower"
    else:
        traffic = dict(size="airfield", towered=False,
                       in_play=AIRFIELD_IN_PLAY,
                       movement_gap_s=AIRFIELD_MOVEMENT_GAP_S)
        rule += ", no tower"
    return FieldKind(False, business, share, rule, **common, **traffic)


def light_type(kind: FieldKind, roll) -> str:
    """One aeroplane of an airfield's fleet. ``roll`` is a ``random.Random``,
    so the injector's seed decides."""
    draw = roll.random()
    if kind.business:
        if draw < BUSINESS_SHARE:
            return roll.choice(BUSINESS)
        draw -= BUSINESS_SHARE
    if draw < TWIN_SHARE:
        return roll.choice(TWINS)
    singles = SINGLES + (EUROPE_SINGLES if kind.europe else ())
    return roll.choice(singles)


__all__ = ["FieldKind", "classify", "light_type", "longest_runway_m",
           "SINGLES", "EUROPE_SINGLES", "TWINS", "BUSINESS", "GA_TYPES"]
