"""The pilot's taxi, along the airport's real taxiways.

The controller used to invent it. :func:`wilcoatc.atc.controller.taxi_route`
hashes the airport and the runway into two or three letters, and the
hold-short was any runway more than thirty degrees off the assigned one,
whether the route went near it or not. It was the same every time, which is
what a pilot needs from it, and it had nothing to do with the airport diagram,
which is what a pilot checks it against. Meanwhile the AI traffic taxiing past
the window drove the simulator's own taxiways (:mod:`wilcoatc.atc.ground`), so
the one aeroplane at the field following an invented route was the pilot's.

This plans the pilot's taxi on that same graph:

* out: from the stand the aeroplane is on (or the nearest taxi node, once it
  has pushed) to the departure runway's holding point, preferring the entry
  at the threshold, exactly as :meth:`GroundLayout.departure` does for the
  traffic;
* in: from wherever the aeroplane has stopped after vacating to a real stand
  nobody is on;
* in both, the taxiways in the order they are used, said the way a controller
  says them, and every runway the route crosses -- the first of which is the
  hold-short.

Runways are found from the published runway geometry, not from the
simulator's runway segments. The simulator names a runway segment by one
end's number in an encoding of its own, and Madeira has taxi nodes on the
runway surface that no runway segment touches (see :mod:`wilcoatc.atc.ground`);
a route that crosses a runway's centreline is crossing that runway whatever
the segments are called.

Nothing here talks to the simulator or the engine. The layout is handed in,
and a plan that cannot be made is returned as a reason rather than raised, so
the controller can say why it fell back to the invented route.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field

from ..navdata.db import normalize_runway
from .ground import GroundLayout, Stand

# How far outside a stand's own radius the aeroplane's reference point can be
# and still be on that stand. The simulator's position is the aeroplane's
# reference point, not its nose wheel, and on a gate an A320 sits some
# fifteen metres off the stand's point.
ON_STAND_M = 20.0

# How far from the nearest taxi node the aeroplane can be and still be
# taxiing on this layout. Further than that it is on grass, on an apron the
# simulator did not describe, or at another field, and a route from the
# nearest node would start somewhere the pilot cannot see.
ON_LAYOUT_M = 250.0

# A leg shorter than this that is neither the first nor the last is the
# graph's own noise -- the few metres of a crossing taxiway an aeroplane
# passes over at a junction -- and is not said. A real short connector, a
# rapid exit or a link between parallels, is longer than this.
SHORT_LEG_M = 30.0

# The most taxiways said in one instruction. Chosen, not published: neither
# FAA JO 7110.65 3-7-2 nor ICAO Doc 4444 12.3.4 sets a number, but five is
# about what a pilot can write down and read back in one breath. Beyond five
# the clearance is cut, the way a controller gives a long taxi in two: "taxi
# via the first five, hold short of the sixth", and the rest once the
# aeroplane is there (split_route; TaxiPlan.split_at). Dropping the shortest
# middle legs, as was done before, left a route the pilot could not follow.
MAX_TAXIWAYS = 5

# How much longer a way round another aeroplane may be than the ordinary
# route before the ordinary route is given anyway. Chosen: half as far again
# is a different taxiway round a block; more than that is a tour of the
# airport to avoid somebody who will have moved by the time the pilot is
# there.
DETOUR = 1.5

# What a taxiway is called on the radio: a letter or two, perhaps a number,
# perhaps a letter after it (Heathrow's A10E, de Gaulle's D2A), or a word and
# a number (Heathrow's LINK 11). The simulator's name table also has labels
# that are not taxiway designators at all (a scenery author's "PARKING", an
# empty string), and a leg with one of those is treated as unnamed rather
# than spelt out.
_DESIGNATOR = re.compile(r"^(?:[A-Z]{1,3}[0-9]{0,3}[A-Z]?|[A-Z]{3,8} [0-9]{1,3})$")
_PREFIXES = ("TAXIWAY ", "TWY ", "TWY")


# A designator's taxiway and its number: "A6" is taxiway A's sixth link, "N2"
# taxiway N's second, "OUT3" a taxiway called OUT's third, "LINK 11" Link's
# eleventh. The scenery labels each segment, so a route along A crossed "A6",
# "A7" and "A8" -- three names the pilot was given for one taxiway.
_FAMILY = re.compile(r"^([A-Z]+?)\s?([0-9]+[A-Z]?)$")


def family(name: str) -> str:
    """The taxiway a designator belongs to: ``A6`` -> ``A``, ``OUT3`` ->
    ``OUT``, ``LINK 11`` -> ``LINK``; a name with no number is its own."""
    found = _FAMILY.match((name or "").strip().upper())
    return found.group(1) if found else (name or "").strip().upper()


def designator(label: str) -> str:
    """A taxiway's spoken designator, or "" if the label is not one."""
    text = (label or "").strip().upper()
    for prefix in _PREFIXES:
        if text.startswith(prefix) and len(text) > len(prefix):
            text = text[len(prefix):].strip()
            break
    return text if _DESIGNATOR.match(text) else ""


@dataclass(frozen=True)
class RunwayLine:
    """One runway as a centreline in the layout's plane."""

    ends: tuple[str, str]          # normalised idents, low end first
    a: tuple[float, float]
    b: tuple[float, float]
    half_width_m: float

    @property
    def name(self) -> str:
        """The end a hold-short names. The low end, as the published runway
        designation starts with it ("4L/22R"); nothing in the data says
        which way the runway being crossed is in use."""
        return self.ends[0]


@dataclass
class TaxiPlan:
    """A taxi instruction's worth of route."""

    names: list[str]
    nodes: list[object]
    # Every runway the route crosses, in the order it reaches them.
    crossings: list[str]
    # Where it goes: a stand for a taxi in, None for a taxi out.
    stand: Stand | None = None
    # The route as (latitude, longitude), for the map.
    points: list[tuple[float, float]] = field(default_factory=list)
    # Where each name starts, as an index into ``nodes``.
    starts: list[int] = field(default_factory=list)
    # The route cut at MAX_TAXIWAYS (split_route): where the aeroplane is
    # held short, as (latitude, longitude), for the rest to be given there.
    split_at: tuple[float, float] | None = None

    @property
    def hold_short(self) -> str:
        return self.crossings[0] if self.crossings else ""

    @property
    def first(self) -> list[str]:
        return split_route(self.names)[0]

    @property
    def hold_at(self) -> str:
        return split_route(self.names)[1]

    @property
    def rest(self) -> list[str]:
        return split_route(self.names)[2]


def runway_lines(layout: GroundLayout, runways) -> list[RunwayLine]:
    """The published runways, as centrelines in the layout's plane.

    ``runways`` are :class:`wilcoatc.navdata.db.Runway` rows, or anything with
    the same idents, end coordinates and width. Runways without coordinates
    for both ends are left out: a crossing found against a guessed centreline
    would be a hold-short short of nothing.
    """
    found = []
    for runway in runways:
        coords = (getattr(runway, "le_lat", None), getattr(runway, "le_lon", None),
                  getattr(runway, "he_lat", None), getattr(runway, "he_lon", None))
        if None in coords or not runway.le_ident or not runway.he_ident:
            continue
        width_m = max(float(getattr(runway, "width_ft", 0.0) or 0.0), 100.0) \
            * 0.3048
        found.append(RunwayLine(
            (normalize_runway(runway.le_ident), normalize_runway(runway.he_ident)),
            layout.to_xy(coords[0], coords[1]),
            layout.to_xy(coords[2], coords[3]),
            width_m / 2.0))
    # Sorted so the same airport gives the same list whatever order the
    # database returned it in.
    found.sort(key=lambda line: line.ends)
    return found


def _point_to_segment(p, a, b) -> float:
    dx, dz = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dz * dz
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx
                                                   + (p[1] - a[1]) * dz) / length))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dz))


def _cross(o, a, b) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _segment_gap(p, q, a, b) -> float:
    """The closest two segments come; zero where they cross."""
    d1, d2 = _cross(a, b, p), _cross(a, b, q)
    d3, d4 = _cross(p, q, a), _cross(p, q, b)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) \
            and d1 != 0 and d2 != 0 and d3 != 0 and d4 != 0:
        return 0.0
    return min(_point_to_segment(p, a, b), _point_to_segment(q, a, b),
               _point_to_segment(a, p, q), _point_to_segment(b, p, q))


def crossings(points: list[tuple[float, float]], lines: list[RunwayLine],
              exclude: set[str] = frozenset()) -> list[str]:
    """Every runway a route goes onto or across, in the order it gets there.

    ``exclude`` are runway idents (either end) the route is allowed on
    without a crossing clearance: the departure runway, which it is going to,
    and the arrival runway, which it is coming off.
    """
    excluded = {normalize_runway(e) for e in exclude if e}
    found: list[tuple[int, str]] = []
    for line in lines:
        if excluded & set(line.ends):
            continue
        for index, (p, q) in enumerate(zip(points, points[1:])):
            if _segment_gap(p, q, line.a, line.b) <= line.half_width_m:
                found.append((index, line.name))
                break
    found.sort()
    return [name for _, name in found]


def spoken_route(layout: GroundLayout, nodes: list[object], *,
                 inbound: bool = False) -> list[tuple[str, int]]:
    """The taxiways a route uses, the way a controller says them, each with
    the index in ``nodes`` where it starts.

    Each segment is its taxiway (``A6``, ``A7`` -> ``A``) and the same
    taxiway is said once however many segments it is cut into; its number is
    kept only where it names a place -- the holding point at the end of a
    route out, the runway exit at the start of a route in (the pilot's rule,
    30 Oct 2026). Unnamed connectors are not said, the few metres of a
    crossing taxiway passed over at a junction are not said at all, and a
    taxiway rejoined later is said once, where it first appears.
    """
    legs: list[list] = []          # [family, metres, first name, last name, start]
    for index, (a, b) in enumerate(zip(nodes, nodes[1:])):
        for other, length, runway, label in layout.edges.get(a, ()):
            if other != b:
                continue
            name = "" if runway else designator(label)
            if not name:
                break
            kind = family(name)
            if legs and legs[-1][0] == kind:
                legs[-1][1] += length
                legs[-1][3] = name
            else:
                legs.append([kind, length, name, name, index])
            break
    if len(legs) > 2:
        legs = [legs[0]] + [leg for leg in legs[1:-1]
                            if leg[1] >= SHORT_LEG_M] + [legs[-1]]
        # Dropping a short leg can bring the same taxiway together again.
        joined: list[list] = []
        for leg in legs:
            if joined and joined[-1][0] == leg[0]:
                joined[-1][1] += leg[1]
                joined[-1][3] = leg[3]
            else:
                joined.append(leg)
        legs = joined
    merged: list[list] = []
    for leg in legs:
        if any(seen[0] == leg[0] for seen in merged):
            continue
        merged.append(leg)
    out = []
    for i, (kind, _metres, first, last, start) in enumerate(merged):
        if inbound and i == 0:
            said = first                    # the runway exit
        elif not inbound and i == len(merged) - 1:
            said = last                     # the holding point
        else:
            said = kind
        out.append((said, start))
    return out


def spoken_legs(layout: GroundLayout, nodes: list[object], *,
                inbound: bool = False) -> list[str]:
    """The names of :func:`spoken_route`, without where each starts."""
    return [name for name, _ in spoken_route(layout, nodes, inbound=inbound)]


def split_route(names: list[str], cap: int = MAX_TAXIWAYS
                ) -> tuple[list[str], str, list[str]]:
    """``(said now, held short of, said once there)``: the whole route when
    it is ``cap`` taxiways or fewer; else the first ``cap``, the next one to
    hold short of, and the rest from there."""
    names = list(names)
    if len(names) <= cap:
        return names, "", []
    return names[:cap], names[cap], names[cap:]


# The taxiways whose names are words, said as words, per field: the data
# file (data/navdata/taxiway_words.csv: airport, label, word, source), and
# otherwise speech.taxiway's own rule -- three letters or more with a vowel
# in them are a word ("OUT" is "Out", not "Oscar Uniform Tango").
_WORDS: dict[str, dict[str, str]] | None = None


def _words() -> dict[str, dict[str, str]]:
    global _WORDS
    if _WORDS is None:
        import csv

        from ..paths import navdata_dir

        found: dict[str, dict[str, str]] = {}
        try:
            path = navdata_dir() / "taxiway_words.csv"
            with open(path, encoding="utf-8", newline="") as handle:
                rows = [line for line in handle if not line.startswith("#")]
            for row in csv.DictReader(rows):
                ident = (row.get("airport") or "").strip().upper()
                label = (row.get("label") or "").strip().upper()
                word = (row.get("word") or "").strip().upper()
                if ident and label and word:
                    found.setdefault(ident, {})[label] = word
        except OSError:
            pass
        _WORDS = found
    return _WORDS


def say_words(airport: str, names) -> list[str]:
    """``names`` with each taxiway the field calls by a word spelled as
    that word ("OUT3" -> "OUTER 3" at Brussels), for the phrase to read;
    the names themselves -- what the map and the log carry -- are kept."""
    table = _words().get((airport or "").strip().upper(), {})
    out = []
    for name in names:
        found = _FAMILY.match((name or "").strip().upper())
        kind = found.group(1) if found else (name or "").strip().upper()
        word = table.get(kind)
        if word is None:
            out.append(name)
        elif word == "-":
            # Spelled, whatever it looks like: its letters apart, so no
            # rule takes them for a word.
            out.append(" ".join(name.replace(" ", "")))
        elif found:
            out.append(f"{word} {found.group(2)}")
        else:
            out.append(word)
    return out


def stand_under(layout: GroundLayout, x: float, z: float) -> Stand | None:
    """The stand the aeroplane is sitting on, or None.

    Only a stand the route can start from counts, and the nearest wins
    (lowest index on a tie, so it never depends on float noise).
    """
    best: tuple[float, int] | None = None
    found = None
    for stand in layout.stands:
        if ("k", stand.index) not in layout.edges:
            continue
        distance = math.hypot(stand.x - x, stand.z - z)
        if distance > stand.radius + ON_STAND_M:
            continue
        key = (round(distance, 3), stand.index)
        if best is None or key < best:
            best, found = key, stand
    return found


def _on_layout(layout: GroundLayout, x: float, z: float) -> bool:
    node = layout.nearest_node(x, z)
    if node is None:
        return False
    nx, nz = layout.xy[node]
    return math.hypot(nx - x, nz - z) <= ON_LAYOUT_M


def _length(legs) -> float:
    """Metres from the start to the runway, line-up included."""
    return sum(math.dist(p, q) for leg in legs
               for p, q in zip(leg.points, leg.points[1:]))


def _threshold(lines: list[RunwayLine], runway: str):
    """(threshold xy, heading, length) for a runway end, or None."""
    wanted = normalize_runway(runway)
    for line in lines:
        if wanted not in line.ends:
            continue
        start, end = (line.a, line.b) if wanted == line.ends[0] \
            else (line.b, line.a)
        heading = math.degrees(math.atan2(end[0] - start[0],
                                          end[1] - start[1])) % 360.0
        return start, heading, math.dist(start, end)
    return None


def avoid_near(layout: GroundLayout,
               obstacles: list[tuple[float, float, float]]) -> set[object]:
    """Taxi nodes another aeroplane on the ground is sitting on.

    ``obstacles`` are (x, z, radius) in the layout's plane. A route never
    goes through a stand anyway (:meth:`GroundLayout.route`); this is the
    taxiway an AI aeroplane is stopped on.
    """
    found = set()
    for node, (nx, nz) in layout.xy.items():
        if node[0] != "p":
            continue
        if any(math.hypot(nx - x, nz - z) <= radius for x, z, radius in obstacles):
            found.add(node)
    return found


def plan_departure(layout: GroundLayout, latitude: float, longitude: float,
                   runway: str, lines: list[RunwayLine],
                   avoid: set[object] = frozenset()
                   ) -> tuple[TaxiPlan | None, str]:
    """From where the aeroplane is to the holding point of ``runway``.

    Returns the plan and "" or None and why there is none. ``avoid`` (taxi
    nodes another aeroplane is stopped on) is kept clear of where there is a
    reasonable way round (``DETOUR``) and not otherwise: an aeroplane stopped
    on a taxiway now will usually have moved by the time the pilot gets
    there, and no route at all is worse than one through where it was.
    """
    end = _threshold(lines, runway)
    if end is None:
        return None, f"runway {runway} has no published position"
    threshold, heading, length_m = end
    x, z = layout.to_xy(latitude, longitude)
    stand = stand_under(layout, x, z)
    if stand is None and not _on_layout(layout, x, z):
        return None, "the aeroplane is not on the simulator's taxiways"
    legs = layout.departure(stand, (x, z), threshold, heading, length_m)
    if avoid and legs is not None:
        around = layout.departure(stand, (x, z), threshold, heading,
                                  length_m, avoid=set(avoid))
        # Round somebody only by a way a controller would give. Avoiding an
        # aeroplane stopped at the entry found a two-kilometre backtrack from
        # the far end of the runway; the ordinary route, with them gone by
        # the time the pilot gets there, is the better instruction.
        if around is not None and _length(around) <= DETOUR * _length(legs):
            legs = around
    if legs is None:
        where = f"stand {stand.number}" if stand is not None else "here"
        return None, f"no taxiway path from {where} to runway {runway}"
    taxi_out = legs[0]
    nodes = [node for node in taxi_out.nodes if node is not None]
    reciprocal = next((line.ends for line in lines
                       if normalize_runway(runway) in line.ends), ())
    plan = _plan(layout, nodes, taxi_out.points, lines,
                 {runway, *reciprocal}, inbound=False)
    return plan, ""


def _plan(layout: GroundLayout, nodes, points, lines, exclude, *,
          inbound: bool, stand=None) -> TaxiPlan:
    said = spoken_route(layout, nodes, inbound=inbound)
    names = [name for name, _ in said]
    starts = [start for _, start in said]
    split_at = None
    if len(names) > MAX_TAXIWAYS:
        node = nodes[starts[MAX_TAXIWAYS]]
        split_at = layout.to_latlon(*layout.xy[node])
    return TaxiPlan(
        names=names, nodes=nodes,
        crossings=crossings(points, lines, exclude=exclude),
        stand=stand, points=[layout.to_latlon(*p) for p in points],
        starts=starts, split_at=split_at)


def _stable(*parts: str) -> int:
    return int.from_bytes(
        hashlib.sha256("|".join(parts).encode("utf-8")).digest()[:6], "big")


def plan_arrival(layout: GroundLayout, latitude: float, longitude: float,
                 lines: list[RunwayLine], *, airliner: bool, key: str,
                 occupied: list[tuple[float, float, float]] = (),
                 taken: set[int] = frozenset(),
                 runway: str = "") -> tuple[TaxiPlan | None, str]:
    """From where the aeroplane has stopped to a free stand.

    ``occupied`` and ``taken`` are what :meth:`GroundLayout.free_stands`
    takes: every other aeroplane on the ground, and the stands handed out.
    ``key`` decides which free stand -- the flight's callsign, so the same
    flight is given the same stand every time it lands here, the way a
    schedule gives it the same gate -- and a stand that cannot be reached is
    passed over for the next. ``runway`` is the one just vacated, which the
    route may still be on without being told to cross it.
    """
    x, z = layout.to_xy(latitude, longitude)
    if not _on_layout(layout, x, z):
        return None, "the aeroplane is not on the simulator's taxiways"
    candidates = layout.free_stands(airliner, list(occupied), set(taken))
    if not candidates:
        return None, "no free stand for this aeroplane"
    first = _stable(layout.ident, key) % len(candidates)
    reciprocal = next((line.ends for line in lines
                       if normalize_runway(runway) in line.ends), ()) \
        if runway else ()
    for stand in candidates[first:] + candidates[:first]:
        taxi = layout.to_stand(x, z, stand)
        if taxi is None:
            continue
        nodes = [node for node in taxi.nodes if node is not None]
        return _plan(layout, nodes, taxi.points, lines, {runway, *reciprocal},
                     inbound=True, stand=stand), ""
    return None, "no taxiway path to any free stand"


__all__ = ["TaxiPlan", "RunwayLine", "runway_lines", "crossings",
           "spoken_legs", "stand_under", "avoid_near", "plan_departure",
           "plan_arrival", "designator", "MAX_TAXIWAYS"]
