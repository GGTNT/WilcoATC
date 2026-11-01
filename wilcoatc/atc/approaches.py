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

The IFR plan's Phase 4 makes that a catalogue: each procedure with its
kind -- ILS, LOC, LDA, SDF, localizer back course, RNAV with its minima
lines (LPV, LNAV/VNAV, LP, LNAV), RNP AR, VOR, VOR/DME, NDB, NDB/DME, a
circling-only procedure (VOR-A) -- its letter (ILS Z, RNAV Y) and typical
minima, and :func:`plan` picks one the way a controller does: the pilot's
request when the runway has it; else the lowest minima the aeroplane can
fly (a GA navigator flies the LPV, an airliner the LNAV/VNAV, and only an
airliner an RNP AR) among those the weather allows; the visual in good
weather in the US and Canada; and, at a runway with no instrument
approach in weather too poor for a visual, an approach to another runway
and a circle to land, its side restricted when the landing is the other
way round (FAA JO 7110.65 4-8-6). The choice is kept by the controller
(``ControllerBrain.approach_choice``) and changes only with the runway or a
request, never because the weather got known on the way in.

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
    # Every procedure, with its letter and lines (the IFR plan, Phase 4).
    procedures: list = field(default_factory=list)

    def add(self, runway: str, kind: str, faf_ft: float = 0.0,
            missed_ft: float = 0.0, suffix: str = "",
            lines: frozenset = frozenset()) -> None:
        key = normalize_runway(runway)
        self.by_runway.setdefault(key, set()).add(kind)
        if faf_ft or missed_ft:
            self.heights[(key, kind)] = (float(faf_ft or 0.0), float(missed_ft or 0.0))
        self.procedures.append(Approach(kind, key, suffix, frozenset(lines),
                                        float(faf_ft or 0.0),
                                        float(missed_ft or 0.0)))

    def add_procedure(self, approach: "Approach") -> None:
        """A procedure under its charted kind; the older ``by_runway`` gets
        the kind it was choosing between (VOR/DME is a VOR, RNP an RNAV)."""
        self.procedures.append(approach)
        if not approach.runway:
            return
        legacy = {"VOR/DME": "VOR", "NDB/DME": "NDB", "LOC BC": "LOC",
                  RNP: "RNAV"}.get(approach.kind, approach.kind)
        self.by_runway.setdefault(approach.runway, set()).add(legacy)
        if approach.faf_ft or approach.missed_ft:
            self.heights[(approach.runway, legacy)] = (approach.faf_ft,
                                                       approach.missed_ft)

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


# ----------------------------------------------------------------------
# the catalogue (the IFR plan, Phase 4)
# ----------------------------------------------------------------------

# The facility data's types under the names a procedure is charted with,
# where TYPE_NAMES above folds them into the kinds a controller used to
# choose between (MSFS SDK, APPROACH.TYPE).
KINDS = {
    1: "RNAV", 2: "VOR", 3: "NDB", 4: "ILS", 5: "LOC", 6: "SDF", 7: "LDA",
    8: "VOR/DME", 9: "NDB/DME", 10: "RNAV", 11: "LOC BC",
}
RNP = "RNP"
RNAV_LINES = ("LPV", "LNAV/VNAV", "LP", "LNAV")

# Typical minima: decision height or MDA above the touchdown zone, feet, and
# visibility, statute miles. UNVERIFIED, every one: figures typical of US
# and European charts (ILS CAT I 200 ft and 1/2 mile, 14 CFR 97 / ICAO
# Annex 6 standard; the rest the IFR plan's), not any one procedure's.
MINIMA = {
    "ILS": (200, 0.5),
    "LPV": (250, 0.75),
    RNP: (250, 0.75),
    "LNAV/VNAV": (350, 1.0),
    "LP": (400, 1.0),
    "LNAV": (450, 1.0),
    "LOC": (450, 0.75),
    "LDA": (500, 1.0),
    "SDF": (500, 1.0),
    "LOC BC": (500, 1.0),
    "VOR/DME": (500, 1.0),
    "VOR": (550, 1.0),
    "NDB/DME": (600, 1.0),
    "NDB": (650, 1.25),
}
# Circling minima and the circling area's radius by approach category.
# UNVERIFIED: the older FAA TERPS radii (1.3, 1.5, 1.7, 2.3 nm) and typical
# circling MDAs, not any one procedure's.
CIRCLING = {"A": (400, 1.0, 1.3), "B": (500, 1.0, 1.5),
            "C": (600, 1.5, 1.7), "D": (700, 2.0, 2.3)}
# A visual approach is offered in the US and Canada with at least this
# ceiling and visibility; below the basic VFR minima (1,000 ft, 3 miles) it
# is never possible. UNVERIFIED as a policy: 7110.65 7-4-2 needs a ceiling
# 500 ft above the MVA and 3 miles; this asks for weather a controller would
# actually run visuals in.
VISUAL_CEILING_FT = 3000
VISUAL_VIS_SM = 5.0
BASIC_VFR = (1000, 3.0)
VISUAL_REGIONS = ("faa",)


@dataclass(frozen=True)
class Approach:
    """One published procedure."""

    kind: str                    # ILS, RNAV, RNP, VOR/DME, LOC BC, ...
    runway: str = ""             # "" for a circling-only procedure (VOR-A)
    suffix: str = ""             # the chart's letter: ILS Z, RNAV Y, VOR-A
    lines: frozenset = frozenset()   # an RNAV's minima lines
    faf_ft: float = 0.0
    missed_ft: float = 0.0

    @property
    def circling_only(self) -> bool:
        return not self.runway

    @property
    def name(self) -> str:
        """As charted: "ILS Z", "RNAV Y", "VOR-A", "LOC BC"."""
        if self.circling_only:
            return f"{self.kind}-{self.suffix}" if self.suffix else self.kind
        return f"{self.kind} {self.suffix}" if self.suffix else self.kind

    def minima(self, lines_flown=None) -> tuple[int, float, str]:
        """(height, visibility, line) for the best line this aeroplane can
        fly, or (0, 0, "") when it can fly none of them."""
        if self.kind == "RNAV":
            flyable = [line for line in RNAV_LINES
                       if line in (self.lines or _ASSUMED_LINES)
                       and (lines_flown is None or line in lines_flown)]
            if not flyable:
                return 0, 0.0, ""
            best = min(flyable, key=lambda line: MINIMA[line][0])
            return MINIMA[best][0], MINIMA[best][1], best
        height, vis = MINIMA.get(self.kind, MINIMA["NDB"])
        return height, vis, self.kind


# An RNAV whose lines the data does not give (the simulator's facility data
# as read today): assumed to have the two lines nearly every one has.
_ASSUMED_LINES = frozenset({"LNAV/VNAV", "LNAV"})

# What each kind of aeroplane's navigator flies. UNVERIFIED: the IFR plan's
# rule -- the simulator's GA navigators (GTN/G1000) fly the LPV, an
# airliner's FMS the LNAV/VNAV, and an RNP AR is an airliner's only.
GA_LINES = frozenset({"LPV", "LP", "LNAV/VNAV", "LNAV"})
AIRLINER_LINES = frozenset({"LNAV/VNAV", "LNAV"})


@dataclass(frozen=True)
class Choice:
    """The approach a controller names, and why."""

    approach: Approach
    runway: str                  # the runway to land on
    circle_to: str = ""          # "" unless circling to another runway
    circle_side: str = ""        # "north" ... when the circle is restricted
    line: str = ""               # the minima line it is flown to
    why: str = ""

    @property
    def kind(self) -> str:
        return self.approach.kind


def _letter_rank(suffix: str) -> int:
    """Between two procedures of one kind to one runway, the one with no
    letter or Z first -- letters are given from Z backwards, so Z is the
    first published -- then Y, X..."""
    return 0 if suffix in ("", "Z") else 1 + ord("Z") - ord(suffix[:1].upper())


def ceiling_ft(weather) -> int | None:
    """The lowest broken or overcast layer, or vertical visibility, AGL."""
    if weather is None:
        return None
    layers = [height for cover, height in getattr(weather, "clouds", ())
              if cover in ("BKN", "OVC", "VV")]
    return min(layers) if layers else None


def _allows(weather, height: int, vis: float) -> bool:
    if weather is None:
        return True
    ceiling = ceiling_ft(weather)
    visibility = float(getattr(weather, "visibility_sm", 10.0) or 10.0)
    return (ceiling is None or ceiling >= height) and visibility >= vis


def _visual_weather(weather) -> bool:
    if weather is None:
        return False
    ceiling = ceiling_ft(weather)
    visibility = float(getattr(weather, "visibility_sm", 10.0) or 10.0)
    return (ceiling is None or ceiling >= VISUAL_CEILING_FT) \
        and visibility >= VISUAL_VIS_SM


def _below_basic_vfr(weather) -> bool:
    if weather is None:
        return False
    ceiling = ceiling_ft(weather)
    visibility = float(getattr(weather, "visibility_sm", 10.0) or 10.0)
    return (ceiling is not None and ceiling < BASIC_VFR[0]) \
        or visibility < BASIC_VFR[1]


def plan(known: "FieldApproaches | None", runway: str, weather=None, *,
         requested: str = "", airliner: bool = True, region: str = "icao",
         runway_headings: dict | None = None,
         ils: bool | None = None) -> Choice:
    """The approach to name for landing on ``runway``.

    ``known`` None is a field the simulator has said nothing about: the
    ILS, as before -- unless the simulator's own runway end says it has no
    ILS (``ils`` False, from its ILS ident), when it is the RNAV rather
    than a clearance onto a localizer the scenery does not have.
    ``runway_headings`` (runway -> magnetic or true heading) is what a
    circle's side is worked out from.
    """
    runway = normalize_runway(runway)
    requested = (requested or "").upper()
    if requested == VISUAL:
        return Choice(Approach(VISUAL, runway), runway, why="asked for")
    if known is None:
        if not requested and ils is False:
            # UNVERIFIED: an end with no ILS ident read as having none.
            return Choice(Approach("RNAV", runway), runway,
                          why="the simulator's runway has no ILS")
        kind = requested or "ILS"
        return Choice(Approach(kind, runway), runway,
                      why=("the simulator's runway has an ILS" if ils
                           else "nothing known"))
    flown = AIRLINER_LINES if airliner else GA_LINES
    category = "C" if airliner else "A"

    def usable(approach):
        if approach.kind == RNP and not airliner:
            return None
        height, vis, line = approach.minima(flown)
        return (height, vis, line) if line else None

    here = [a for a in known.procedures if a.runway == runway]
    if requested:
        for approach in sorted(here, key=lambda a: _letter_rank(a.suffix)):
            if requested in (approach.kind, approach.name) or (
                    requested == "RNAV" and approach.kind == RNP and airliner):
                found = usable(approach)
                return Choice(approach, runway, line=found[2] if found else "",
                              why="asked for")

    if region in VISUAL_REGIONS and _visual_weather(weather):
        return Choice(Approach(VISUAL, runway), runway,
                      why="visual weather in the US and Canada")

    flyable = [(usable(a), a) for a in here]
    flyable = [(m, a) for m, a in flyable if m is not None]
    allowed = [(m, a) for m, a in flyable if _allows(weather, m[0], m[1])]
    if allowed:
        (height, _vis, line), approach = min(
            allowed, key=lambda pair: (pair[0][0], _letter_rank(pair[1].suffix)))
        return Choice(approach, runway, line=line,
                      why=f"lowest minima the weather allows ({line} {height} ft)")
    if flyable and not _below_basic_vfr(weather):
        (height, _vis, line), approach = min(flyable, key=lambda p: p[0][0])
        return Choice(approach, runway, line=line,
                      why=f"below minima for all; the lowest ({line} {height} ft)")
    if not here and not _below_basic_vfr(weather):
        return Choice(Approach(VISUAL, runway), runway,
                      why="nothing published to this runway")

    # Weather too poor for what this runway has: an approach to another
    # runway, and a circle to land on this one.
    circle_height, circle_vis, _radius = CIRCLING[category]
    others = [(usable(a), a) for a in known.procedures if a.runway != runway]
    others = [(m, a) for m, a in others if m is not None
              and _allows(weather, max(m[0], circle_height),
                          max(m[1], circle_vis))]
    if others:
        # The circle is flown at its own minima whatever the approach, so
        # the choice is the geometry: the same strip the other way round --
        # "ILS 25, circle to land 07" -- then the approach best lined up,
        # then the lower minima.
        def circling_rank(pair):
            (height, _vis, _line), approach = pair
            return (0 if _reciprocal(approach.runway, runway) else 1,
                    _angle(approach.runway, runway, runway_headings or {}),
                    height, _letter_rank(approach.suffix))

        (_m, approach) = min(others, key=circling_rank)
        side = _circle_side(approach.runway, runway, runway_headings or {})
        return Choice(approach, runway, circle_to=runway, circle_side=side,
                      line=_m[2],
                      why=f"circling to {runway}, category {category} minima "
                          f"{circle_height} ft {circle_vis:g} sm")
    if flyable:
        (_m, approach) = min(flyable, key=lambda p: p[0][0])
        return Choice(approach, runway, line=_m[2],
                      why="below minima everywhere; the lowest named")
    return Choice(Approach(VISUAL, runway), runway,
                  why="nothing flyable anywhere at the field")


_COMPASS = ("north", "east", "south", "west")


def _reciprocal(a: str, b: str) -> bool:
    """25 and 07, 16L and 34R: one strip, the two directions."""
    if not a or not b:
        return False
    try:
        na, nb = int(a[:2]), int(b[:2])
    except ValueError:
        return False
    sides = {"L": "R", "R": "L", "C": "C", "": ""}
    return abs(na - nb) == 18 and sides.get(a[2:], "?") == b[2:]


def _angle(a: str, b: str, headings: dict) -> float:
    """How far apart two runways' directions are, degrees (0 unknown)."""
    if a not in headings or b not in headings:
        return 0.0
    return abs((headings[a] - headings[b] + 540.0) % 360.0 - 180.0)


def _circle_side(approach_runway: str, landing: str, headings: dict) -> str:
    """Where a circle is flown when it turns the landing round (7110.65
    4-8-6 restricts it to one side): the side left of the landing
    direction, a left-hand circuit's downwind. UNVERIFIED: a real
    restriction is the procedure's, from its obstacles; none is known."""
    if not approach_runway:
        return ""
    a = headings.get(approach_runway)
    b = headings.get(landing)
    if a is None or b is None:
        return ""
    if abs((a - b + 540.0) % 360.0 - 180.0) <= 90.0:
        return ""
    left = (b - 90.0) % 360.0
    return _COMPASS[int((left + 45.0) // 90.0) % 4]


def split_name(name: str) -> tuple[str, str]:
    """("ILS", "Z") out of "ILS Z", ("VOR", "A") out of "VOR-A"; a name
    with no letter comes back whole."""
    text = (name or "").strip()
    if "-" in text and len(text.rsplit("-", 1)[1]) == 1:
        base, letter = text.rsplit("-", 1)
        return base, letter.upper()
    parts = text.rsplit(" ", 1)
    if len(parts) == 2 and len(parts[1]) == 1 and parts[1].isalpha():
        return parts[0], parts[1].upper()
    return text, ""


# The kinds the older table chose between, for an altitude lookup by kind.
LEGACY_KIND = {"VOR/DME": "VOR", "NDB/DME": "NDB", "LOC BC": "LOC", RNP: "RNAV"}


def legacy_kind(name: str) -> str:
    base, _letter = split_name(name)
    return LEGACY_KIND.get(base, base)


def load_fixture(path) -> "FieldApproaches":
    """A field's approaches from a JSON list (tests/data/approaches), the
    same shape the simulator's facility data is decoded into."""
    import json
    from pathlib import Path

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    found = FieldApproaches(data["ident"])
    for row in data["approaches"]:
        found.add_procedure(Approach(
            kind=row["kind"], runway=normalize_runway(row.get("runway", ""))
            if row.get("runway") else "", suffix=row.get("suffix", ""),
            lines=frozenset(row.get("lines") or ()),
            faf_ft=float(row.get("faf_ft") or 0.0),
            missed_ft=float(row.get("missed_ft") or 0.0)))
    return found


__all__ = ["FieldApproaches", "choose", "asked_for", "runway_from",
           "TYPE_NAMES", "PREFERENCE", "VISUAL", "Approach", "Choice", "plan",
           "KINDS", "MINIMA", "CIRCLING", "RNP", "ceiling_ft", "load_fixture",
           "split_name", "legacy_kind"]
