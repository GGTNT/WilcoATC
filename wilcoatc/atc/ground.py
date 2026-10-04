"""The airport on the ground: stands, taxiways, and getting between them.

The traffic used to be placed on the ground rather than moved on it. A
departure sat at a point a third of a mile from the middle of the airport --
on grass, on a runway, on the pilot's own stand -- and then appeared at the
holding point, and then on the runway, each a jump of hundreds of metres. With
the simulator drawing every one of them, that is an aeroplane teleporting past
the pilot's window, and nothing stopped it being put where the pilot already
was.

The simulator knows the airport. Its facility API gives every taxi node,
every stand, and every segment between them, with the taxiway's name (read
by :mod:`wilcoatc.integrations.taxiways`). This is that network as a graph,
and what the traffic needs from it:

* a free stand to start from or finish at, never one the pilot or another
  aeroplane is on;
* a route along real taxiways, in the taxiways' own names for the clearance;
* where to hold short of the departure runway, and where to join it;
* the first exit ahead of an aeroplane slowing down on the runway;
* moving along a route at taxi speed, pushing back off a stand first.

Everything here is in metres on a flat plane around the airport's reference
point, which is what the simulator's offsets are. An airport is a few
kilometres across, so the flat plane is good to well under a metre.

Deciding whether an aeroplane may move -- somebody ahead, an intersection
somebody else has, a runway that is not free -- is the traffic's business and
lives in :mod:`wilcoatc.atc.traffic`. This only says where things are.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

EARTH_M = 6_371_008.8

# What the simulator calls each kind of segment. Vehicle lanes and roads are
# on the same graph and are no place for an aeroplane.
PATH_TAXI = 1
PATH_RUNWAY = 2
PATH_PARKING = 3

# Which stands are gates, and which are ramps, by the simulator's own numbers.
# Fuel pads, docks and vehicle parking are not stands.
_GATES = frozenset({8, 9, 10, 15})
_RAMPS = frozenset({1, 2, 3, 4, 5, 6, 14})

# How big a stand has to be for each kind of aeroplane, by the radius the
# simulator gives it. An A320 is 36 m across; a Cessna 11.
AIRLINER_RADIUS_M = 15.0
LIGHT_RADIUS_M = 5.0

# Room to leave around somebody else when choosing a stand for a new
# aeroplane: their own stand's radius and this much again.
STAND_CLEARANCE_M = 15.0

# How far from the runway centreline an aeroplane waits to be told it may go
# on. Airliners hold further back than this at big fields; this is the floor
# that keeps a waiting tail out of the way of one landing.
HOLD_OFF_CENTRELINE_M = 45.0

# A taxiway joining the runway further than this from the threshold is an
# intersection departure. Allowed -- it is what happens -- but the one nearest
# the end is always preferred.
ENTRY_SEARCH_M = 900.0

# Taxi speeds, in metres a second. A straight is taken at about fifteen knots,
# a corner at seven, and a pushback at a walking pace.
TAXI_MS = 7.7
TURN_MS = 3.6
PUSH_MS = 1.4

# Runway segments cost this much more than taxiway ones, so a route crosses a
# runway where it has to and never uses one as a taxiway if there is another
# way round.
RUNWAY_COST = 6.0


# How far either side of a runway's centreline counts as on it. Half a wide
# runway and a little more.
RUNWAY_HALF_M = 30.0


def _to_segment(x: float, z: float, ax: float, az: float, bx: float,
                bz: float) -> float:
    """Distance from a point to a segment."""
    dx, dz = bx - ax, bz - az
    length = dx * dx + dz * dz
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((x - ax) * dx
                                                    + (z - az) * dz) / length))
    return math.hypot(x - (ax + t * dx), z - (az + t * dz))


def _heading(dx: float, dz: float) -> float:
    return math.degrees(math.atan2(dx, dz)) % 360.0


def _turn(a: float, b: float) -> float:
    return abs((b - a + 540.0) % 360.0 - 180.0)


@dataclass(frozen=True)
class Stand:
    index: int
    x: float
    z: float
    heading: float
    radius: float
    kind: int
    number: int

    @property
    def gate(self) -> bool:
        return self.kind in _GATES


@dataclass
class Taxiing:
    """An aeroplane's way along the ground, and how far along it is.

    ``points`` are metres in the layout's plane. The first ``push`` of them
    are driven backwards -- a pushback, with the nose still pointing the way
    it was parked -- and ``nodes`` are the graph nodes the points came from,
    None for a point that is not one (the start, a point on a centreline).
    """

    points: list[tuple[float, float]]
    nodes: list[object]
    push: int = 0
    index: int = 1
    # Whether the last point is on a runway centreline and the aeroplane
    # should end up pointing along it.
    face: float | None = None
    # The taxiway names, for the clearance.
    names: list[str] = field(default_factory=list)
    blocked_since: float | None = None
    # An arrival stops here, clear of the runway, until its way to the stand
    # is not somebody else's the other way (``cleared``).
    hold_at: int | None = None
    cleared: bool = False

    @property
    def done(self) -> bool:
        return self.index >= len(self.points)

    def remaining_edges(self) -> set[tuple[object, object]]:
        found = set()
        for a, b in zip(self.nodes[self.index - 1:], self.nodes[self.index:]):
            if a is not None and b is not None:
                found.add((a, b))
        return found


class GroundLayout:
    """One airport's taxi network, from the simulator."""

    def __init__(self, ident: str, latitude: float, longitude: float,
                 points: list, parkings: list, paths: list,
                 names: list[str]):
        self.ident = (ident or "").upper()
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self._cos = math.cos(math.radians(self.latitude))
        # Nodes are ("p", i) for a taxi point and ("k", j) for a stand.
        self.xy: dict[object, tuple[float, float]] = {}
        for index, (_kind, _orientation, x, z) in enumerate(points):
            self.xy[("p", index)] = (float(x), float(z))
        self.stands: list[Stand] = []
        for index, row in enumerate(parkings):
            kind, _point_kind, _name, _suffix, number, _orientation, \
                heading, radius, x, z = row[:10]
            stand = Stand(index, float(x), float(z), float(heading) % 360.0,
                          float(radius), int(kind), int(number))
            self.xy[("k", index)] = (stand.x, stand.z)
            self.stands.append(stand)
        self.names = list(names)
        # node -> [(neighbour, metres, is_runway, taxiway name)]
        self.edges: dict[object, list[tuple[object, float, bool, str]]] = {}
        self.runway_nodes: set[object] = set()
        for kind, _width, _number, _designator, start, end, name, *_ in paths:
            a = ("p", int(start))
            if kind == PATH_PARKING:
                b = ("k", int(end))
            elif kind in (PATH_TAXI, PATH_RUNWAY):
                b = ("p", int(end))
            else:
                continue
            if a not in self.xy or b not in self.xy or a == b:
                continue
            label = self.names[name] if 0 <= name < len(self.names) else ""
            runway = kind == PATH_RUNWAY
            if runway:
                self.runway_nodes.update((a, b))
            length = math.dist(self.xy[a], self.xy[b])
            self.edges.setdefault(a, []).append((b, length, runway, label))
            self.edges.setdefault(b, []).append((a, length, runway, label))
        # A node on the runway surface is a runway node whatever the segments
        # into it are called. Madeira has taxi nodes a metre off the
        # centreline that no runway segment touches, and an aeroplane was
        # driven through one of them, across the runway, in front of a
        # departure on its takeoff roll.
        segments = [(self.xy[a], self.xy[b])
                    for a in self.runway_nodes
                    for b, _, runway, _ in self.edges.get(a, ())
                    if runway]
        for node, (x, z) in self.xy.items():
            if node[0] != "p" or node in self.runway_nodes:
                continue
            if any(_to_segment(x, z, *p, *q) <= RUNWAY_HALF_M
                   for p, q in segments):
                self.runway_nodes.add(node)

    @classmethod
    def from_dict(cls, data: dict) -> "GroundLayout":
        return cls(data["ident"], data["latitude"], data["longitude"],
                   data["points"], data["parkings"], data["paths"],
                   data.get("names", []))

    def __bool__(self) -> bool:
        return bool(self.stands) and bool(self.edges)

    # -- coordinates ------------------------------------------------------

    def to_xy(self, latitude: float, longitude: float) -> tuple[float, float]:
        x = math.radians(longitude - self.longitude) * EARTH_M * self._cos
        z = math.radians(latitude - self.latitude) * EARTH_M
        return x, z

    def to_latlon(self, x: float, z: float) -> tuple[float, float]:
        latitude = self.latitude + math.degrees(z / EARTH_M)
        longitude = self.longitude + math.degrees(x / (EARTH_M * self._cos))
        return latitude, longitude

    # -- routes -----------------------------------------------------------

    def route(self, start: object, goal: object,
              avoid: set[object] | None = None) -> list[object]:
        """The cheapest way from one node to another, or [] if there is none.

        Stands are ends only: a route never cuts through somebody else's
        stand on the way past.
        """
        if start not in self.edges or goal not in self.edges:
            return []
        avoid = avoid or set()
        best = {start: 0.0}
        came: dict[object, object] = {}
        queue = [(0.0, 0, start)]
        count = 0
        while queue:
            cost, _, node = heapq.heappop(queue)
            if node == goal:
                break
            if cost > best.get(node, math.inf):
                continue
            for other, length, runway, _ in self.edges.get(node, ()):
                if other in avoid and other != goal:
                    continue
                if other[0] == "k" and other != goal:
                    continue
                step = length * (RUNWAY_COST if runway else 1.0)
                if cost + step < best.get(other, math.inf):
                    best[other] = cost + step
                    came[other] = node
                    count += 1
                    heapq.heappush(queue, (cost + step, count, other))
        if goal not in best:
            return []
        path = [goal]
        while path[-1] != start:
            path.append(came[path[-1]])
        return path[::-1]

    def names_along(self, nodes: list[object]) -> list[str]:
        """The taxiways a route uses, in order, each once."""
        found: list[str] = []
        for a, b in zip(nodes, nodes[1:]):
            for other, _length, runway, label in self.edges.get(a, ()):
                if other == b:
                    if label and not runway and (not found
                                                 or found[-1] != label):
                        found.append(label)
                    break
        return found

    def nearest_node(self, x: float, z: float,
                     taxi_only: bool = True) -> object | None:
        best, found = math.inf, None
        for node, (nx, nz) in self.xy.items():
            if node[0] != "p" or node not in self.edges:
                continue
            if taxi_only and node in self.runway_nodes:
                continue
            distance = math.hypot(nx - x, nz - z)
            if distance < best:
                best, found = distance, node
        return found

    # -- stands -----------------------------------------------------------

    def stands_for(self, airliner: bool) -> list[Stand]:
        """Every stand this size of aeroplane fits on and can taxi from."""
        wanted = AIRLINER_RADIUS_M if airliner else LIGHT_RADIUS_M
        return [stand for stand in self.stands
                if ("k", stand.index) in self.edges
                and (stand.kind in _GATES or stand.kind in _RAMPS)
                and stand.radius >= wanted]

    def free_stand(self, airliner: bool,
                   occupied: list[tuple[float, float, float]],
                   taken: set[int], choose=None) -> Stand | None:
        """A stand this aeroplane fits on that nobody is on or near.

        ``occupied`` is (x, z, radius) for everything already on the ground --
        the pilot first among them -- and ``taken`` the stands already given
        out. ``choose`` picks among the candidates; the first is taken without
        one.
        """
        candidates = []
        for stand in self.stands_for(airliner):
            if stand.index in taken:
                continue
            if any(math.hypot(stand.x - x, stand.z - z)
                   < stand.radius + radius + STAND_CLEARANCE_M
                   for x, z, radius in occupied):
                continue
            candidates.append(stand)
        if not candidates:
            return None
        if not airliner:
            # A light aeroplane goes to the light ramp while there is room
            # on it, and takes an airliner's stand only when there is not.
            small = [c for c in candidates if c.radius < AIRLINER_RADIUS_M]
            candidates = small or candidates
        return choose(candidates) if choose else candidates[0]

    # -- the runway -------------------------------------------------------

    def runway_frame(self, x: float, z: float, threshold: tuple[float, float],
                     heading: float) -> tuple[float, float]:
        """(along, across) in metres from a threshold, along its heading."""
        tx, tz = threshold
        dx, dz = x - tx, z - tz
        rad = math.radians(heading)
        along = dx * math.sin(rad) + dz * math.cos(rad)
        across = dx * math.cos(rad) - dz * math.sin(rad)
        return along, across

    def entries(self, threshold: tuple[float, float], heading: float,
                length_m: float) -> list[tuple[float, object]]:
        """Where taxiways meet this runway, as (metres from the threshold,
        node), nearest the threshold first."""
        found = []
        for node in self.runway_nodes:
            if not any(not runway for _, _, runway, _ in self.edges[node]):
                continue
            along, across = self.runway_frame(*self.xy[node], threshold,
                                              heading)
            if abs(across) > 40.0 or not -150.0 <= along <= length_m + 150.0:
                continue
            found.append((along, node))
        found.sort(key=lambda item: item[0])
        return found

    def departure(self, stand: Stand | None, start_xy: tuple[float, float],
                  threshold: tuple[float, float], heading: float,
                  length_m: float, avoid: set[object] | None = None
                  ) -> tuple[Taxiing, Taxiing] | None:
        """From a stand (or wherever it is) to the holding point, and from
        there onto the runway.

        Two legs, because they are two clearances: taxi to the holding point,
        and then -- later, when the tower says so -- line up.
        """
        entries = [(along, node) for along, node in
                   self.entries(threshold, heading, length_m)
                   if along <= ENTRY_SEARCH_M] or \
            self.entries(threshold, heading, length_m)[:1]
        if stand is not None:
            start = ("k", stand.index)
        else:
            start = self.nearest_node(*start_xy)
        if start is None:
            return None
        for _along, entry in entries:
            nodes = self.route(start, entry, avoid)
            if len(nodes) < 2:
                continue
            # Walk back from the runway to the last node far enough off the
            # centreline to wait at. Everything after it is the line-up.
            hold_at = None
            for position in range(len(nodes) - 2, -1, -1):
                _, across = self.runway_frame(*self.xy[nodes[position]],
                                              threshold, heading)
                if abs(across) >= HOLD_OFF_CENTRELINE_M \
                        and nodes[position] not in self.runway_nodes:
                    hold_at = position
                    break
            if hold_at is None or hold_at == 0 and stand is not None:
                continue
            out = nodes[:hold_at + 1]
            onto = nodes[hold_at:]
            # Line up pointing down the runway a little past the entry, on
            # the centreline, rather than stopping sideways across it.
            ex, ez = self.xy[entry]
            along, _ = self.runway_frame(ex, ez, threshold, heading)
            rad = math.radians(heading)
            lined = (threshold[0] + (along + 40.0) * math.sin(rad),
                     threshold[1] + (along + 40.0) * math.cos(rad))
            taxi_out = self._taxiing(out, start_xy if stand is None else None,
                                     push=stand is not None)
            line_up = self._taxiing(onto, None, extra=lined, face=heading)
            return taxi_out, line_up
        return None

    def vacate(self, x: float, z: float, heading: float,
               threshold: tuple[float, float], runway_heading: float,
               length_m: float, stand: Stand,
               avoid: set[object] | None = None) -> Taxiing | None:
        """Off the runway at the first exit ahead, and on to a stand."""
        along_here, _ = self.runway_frame(x, z, threshold, runway_heading)
        entries = self.entries(threshold, runway_heading, length_m)
        ahead = [(along, node) for along, node in entries
                 if along >= along_here + 20.0]
        # The first one ahead, or -- at the end of the runway -- the last one
        # behind, which is a turn round and a backtrack.
        order = ahead + sorted([item for item in entries if item not in ahead],
                               key=lambda item: -item[0])
        goal = ("k", stand.index)
        for _along, exit_node in order:
            nodes = self.route(exit_node, goal, avoid)
            if len(nodes) < 2:
                continue
            taxi = self._taxiing(nodes, (x, z))
            # The first point properly off the runway is where it waits, if
            # it has to, for the way ahead to be clear.
            for position, node in enumerate(taxi.nodes):
                if node is None or node in self.runway_nodes:
                    continue
                _, across = self.runway_frame(*self.xy[node], threshold,
                                              runway_heading)
                if abs(across) >= HOLD_OFF_CENTRELINE_M:
                    taxi.hold_at = position
                    break
            return taxi
        return None

    def to_stand(self, x: float, z: float, stand: Stand) -> Taxiing | None:
        start = self.nearest_node(x, z)
        if start is None:
            return None
        nodes = self.route(start, ("k", stand.index))
        if len(nodes) < 2:
            return None
        return self._taxiing(nodes, (x, z))

    def _taxiing(self, nodes: list[object],
                 start_xy: tuple[float, float] | None, *, push: bool = False,
                 extra: tuple[float, float] | None = None,
                 face: float | None = None) -> Taxiing:
        points = [self.xy[node] for node in nodes]
        node_list: list[object] = list(nodes)
        if start_xy is not None:
            points.insert(0, start_xy)
            node_list.insert(0, None)
        if extra is not None:
            points.append(extra)
            node_list.append(None)
        return Taxiing(points=points, nodes=node_list, push=2 if push else 0,
                       face=face, names=self.names_along(list(nodes)))

    def degree(self, node: object) -> int:
        return len(self.edges.get(node, ()))


def ahead(taxi: Taxiing, x: float, z: float,
          distance: float, step: float = 5.0) -> list[tuple[float, float]]:
    """Points along the rest of the route, every ``step`` metres, as far as
    ``distance`` -- the corridor a taxiing aeroplane is about to use."""
    found = [(x, z)]
    covered = 0.0
    px, pz = x, z
    for position in range(taxi.index, len(taxi.points)):
        tx, tz = taxi.points[position]
        gap = math.hypot(tx - px, tz - pz)
        walked = 0.0
        while walked + step <= gap and covered + step <= distance:
            walked += step
            covered += step
            found.append((px + (tx - px) * walked / gap,
                          pz + (tz - pz) * walked / gap))
        covered += gap - walked
        if covered >= distance:
            break
        px, pz = tx, tz
        found.append((px, pz))
    return found


def advance(taxi: Taxiing, x: float, z: float, heading: float,
            elapsed: float) -> tuple[float, float, float, float]:
    """Move along a route for ``elapsed`` seconds.

    Returns the new position, heading and speed in metres a second. The
    speed is chosen by what is coming: a corner within thirty metres slows it
    to turning pace, and a pushback is slow throughout.
    """
    speed = 0.0
    remaining = elapsed
    while remaining > 0 and not taxi.done:
        tx, tz = taxi.points[taxi.index]
        dx, dz = tx - x, tz - z
        gap = math.hypot(dx, dz)
        pushing = taxi.index < taxi.push
        if pushing:
            speed = PUSH_MS
        else:
            speed = TAXI_MS
            if taxi.index + 1 < len(taxi.points):
                nx, nz = taxi.points[taxi.index + 1]
                if gap < 30.0 and _turn(_heading(dx, dz),
                                        _heading(nx - tx, nz - tz)) > 30.0:
                    speed = TURN_MS
        if gap < 0.01:
            taxi.index += 1
            continue
        step = speed * remaining
        if step >= gap:
            x, z = tx, tz
            remaining -= gap / speed
            taxi.index += 1
        else:
            x, z = x + dx / gap * step, z + dz / gap * step
            remaining = 0.0
        # Pushing back, the nose points away from the way it is going.
        moving = _heading(dx, dz)
        heading = (moving + 180.0) % 360.0 if pushing else moving
    if taxi.done and taxi.face is not None:
        heading = taxi.face
    return x, z, heading, speed


__all__ = ["GroundLayout", "Stand", "Taxiing", "advance", "PATH_TAXI",
           "PATH_RUNWAY", "PATH_PARKING"]
