"""Where each ground vehicle goes on a given aeroplane, from its own doors.

The first spots were hand-tuned fractions of the fuselage length, measured
against GSX's around one Fenix A321 and then stretched to every other
type -- which put the stairs a metre or two off the door of most of them,
and the loaders by the wing. But the simulator knows where every model's
doors are: ``INTERACTIVE POINT TYPE/POSX/POSY/POSZ/HEADING:n``, read per
object. Measured at Madeira, on the flyable A321, the PassiveAircraft A321
CEO and FSLTL's A321 alike:

* type 0 is a passenger door, 1 a cargo hold, 2 an emergency exit, 3 a fuel
  point (on the wing), 4 ground power (under the nose), 99 an unused slot;
* x is to the right of the reference point and z forward of it, in feet;
  the heading is the way the door faces out (-90 a left door, +90 right).

So a vehicle's spot is worked out from the door it serves: a set of stairs
at the foremost left passenger door, the caterer at the foremost right one,
a loader at each end hold, the fuel truck at the right wing's fuel point,
the power unit at the socket. ``standoff_m`` out from the door along the
way it faces, ``along_m`` forward along the fuselage, and the vehicle
pointed at the door (stairs, loaders) or backed up to it (the caterer).

Each vehicle *model* then has its own origin and its own idea of forward:
a correction per title, in the vehicle's own frame, from
``service_layout.json`` beside this module, which is what
``scripts/place_vehicle.py`` writes when a vehicle is nudged into place by
hand. A model that reports no points -- or a type nobody has read -- falls
back to the per-type table there, then to the fractions of length.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

PASSENGER = 0
CARGO = 1
EMERGENCY = 2
FUEL_POINT = 3
POWER = 4
UNUSED = 99

FEET = 0.3048
LAYOUT_FILE = Path(__file__).with_name("service_layout.json")


@dataclass(frozen=True)
class Point:
    """One interactive point, in metres, in the aeroplane's frame."""

    index: int
    kind: int
    right: float
    up: float
    forward: float
    heading: float


def points_from_reading(values: list[float]) -> list[Point]:
    """From the raw reading: per point TYPE, POSX, POSY, POSZ, HEADING in
    that order, positions in feet. Unused slots are left out."""
    found = []
    for index in range(len(values) // 5):
        kind, x, y, z, heading = values[index * 5:index * 5 + 5]
        kind = int(round(kind))
        if kind == UNUSED or (x == 0.0 and y == 0.0 and z == 0.0):
            continue
        found.append(Point(index, kind, x * FEET, y * FEET, z * FEET,
                           float(heading)))
    return found


# Which door each kind of vehicle serves, from a model's points.
def _left(points, kind):
    return [p for p in points if p.kind == kind and p.right < 0.0]


def _right(points, kind):
    return [p for p in points if p.kind == kind and p.right > 0.0]


def door_for(role: str, points: list[Point]) -> Point | None:
    """The point a vehicle in this role goes to, or None."""
    if role == "stairs":
        found = _left(points, PASSENGER)
        return max(found, key=lambda p: p.forward) if found else None
    if role == "stairs_aft":
        found = _left(points, PASSENGER)
        if len(found) < 2:
            return None
        return min(found, key=lambda p: p.forward)
    if role == "catering":
        found = _right(points, PASSENGER)
        return max(found, key=lambda p: p.forward) if found else None
    if role == "baggage":
        found = [p for p in points if p.kind == CARGO]
        return max(found, key=lambda p: p.forward) if found else None
    if role == "baggage_aft":
        # The main aft hold, not the bulk door behind it: the foremost of
        # the holds well aft of the forward one (the flyable A321 has its
        # aft hold at -10.5 m and the bulk door at -14.1).
        found = [p for p in points if p.kind == CARGO]
        if len(found) < 2:
            return None
        fwd = max(found, key=lambda p: p.forward)
        aft = [p for p in found if p.forward < fwd.forward - 3.0]
        return max(aft, key=lambda p: p.forward) if aft else None
    if role == "fuel":
        found = [p for p in points if p.kind == FUEL_POINT]
        right = [p for p in found if p.right > 0.0]
        return (right or found or [None])[0]
    if role == "gpu":
        found = [p for p in points if p.kind == POWER]
        return found[0] if found else None
    if role == "pushback":
        # The tug at the nose gear, which is where the ground power socket
        # is when a model puts it on the centreline: on the flyable A321
        # the socket is at +12.54 m and the tug, placed by eye at Madeira,
        # belongs at +12.25.
        found = [p for p in points if p.kind == POWER
                 and abs(p.right) < NOSE_GEAR_WITHIN_M]
        return found[0] if found else None
    return None


def load_layout(path: Path | str | None = None) -> dict:
    """The layout table, or an empty one: roles, vehicles, types."""
    where = Path(path) if path else LAYOUT_FILE
    try:
        table = json.loads(where.read_text(encoding="utf-8"))
        return table if isinstance(table, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        log.info("the service layout %s could not be read", where, exc_info=True)
        return {}


def _rotate(right: float, forward: float, degrees: float) -> tuple[float, float]:
    """A vector in a frame turned ``degrees`` clockwise, back into the
    aeroplane's (right, forward)."""
    rad = math.radians(degrees)
    return (right * math.cos(rad) + forward * math.sin(rad),
            -right * math.sin(rad) + forward * math.cos(rad))


#: A power point this close to the centreline is under the nose, by the
#: nose gear; one further out (the PassiveAircraft A321's, 5.8 m right) is
#: not, and says nothing about where the gear is.
NOSE_GEAR_WITHIN_M = 1.0

#: Closer to the centreline than this, a point has no side of its own (the
#: power socket under the nose) and its heading is taken as it comes.
CENTRELINE_M = 0.3


def outward(door: Point) -> float:
    """The way out from a door, relative to the aeroplane.

    Its own heading where that agrees with the side it is on. Not always:
    the flyable A321's bulk hold, right of the centreline, says -90; and a
    hold's point sits under the fuselage, so it is served from its side
    whatever it says.
    """
    side = 90.0 if door.right > 0.0 else -90.0
    if door.kind == CARGO:
        return side
    if abs(door.right) < CENTRELINE_M:
        return door.heading
    if math.sin(math.radians(door.heading)) * door.right <= 0.0:
        return side
    return door.heading


def spot_from_door(door: Point, rule: dict) -> tuple[float, float, float]:
    """(right, forward, heading relative to the aeroplane) for a vehicle
    serving ``door`` under ``rule``."""
    heading_out = outward(door)
    out = math.radians(heading_out)
    standoff = float(rule.get("standoff_m", 3.0))
    along = float(rule.get("along_m", 0.0))
    right = door.right + standoff * math.sin(out)
    forward = door.forward + standoff * math.cos(out) + along
    facing = rule.get("facing", "door")
    if facing == "door":
        heading = (heading_out + 180.0) % 360.0
    elif facing == "away":
        heading = heading_out % 360.0
    else:
        heading = float(facing) % 360.0
    return right, forward, heading


#: Which entry of ``sill_below_point_m`` a point's kind reads.
KIND_NAMES = {PASSENGER: "passenger", CARGO: "cargo"}


def sill_of(door: Point, standing_m: float | None, table: dict,
            pitch_deg: float = 0.0) -> float | None:
    """How high above the apron this door's sill is, from the model's own
    point: the reference's height above the ground plus the point's POSY,
    less ``sill_below_point_m`` for its kind. None without the height.

    On the flyable A321 that puts L1 3.96 m off the apron, half a metre
    above the published sill of an A321, and its forward hold at 2.38 m
    against about 2.05 -- so the first guess was that a point is mid-door,
    and 0.55 and 0.3 m were taken off. Markers at both, at Bilbao (17
    October), showed otherwise: each point sat on the floor of its open
    door, and the sill markers hung below the doors on the skin. The model
    stands higher than the real aeroplane; its points are its sills. The
    drop is kept, at zero, for a model whose points are not.

    Then it turned out to be held 1.2 ft too high and nose up, nose wheel
    in the air (Innsbruck, 18 October). On its wheels, at its own resting
    pitch (``pitch_deg``, nose down positive, as the simulator gives it:
    the A321's 0.40), a door 14 m ahead is 0.1 m lower than its POSY says
    and one 17 m behind 0.12 m higher: L1 3.49, L2 3.66, the holds 1.92
    and 2.01 -- an A321's own sills, near enough."""
    if standing_m is None:
        return None
    drops = table.get("sill_below_point_m", {})
    drop = float(drops.get(KIND_NAMES.get(door.kind, ""), 0.0))
    pitch = math.radians(pitch_deg)
    up = door.up * math.cos(pitch) - door.forward * math.sin(pitch)
    return standing_m + up - drop


def belt(loader: dict, sill: float) -> tuple[float, float] | None:
    """The belt angle that puts a loader's belt end at ``sill``, and how far
    ahead of the loader's origin the end then is: (degrees, metres). None
    when the end cannot reach it.

    ``loader`` is the model's belt from its own cfg -- the pivot that stays
    put while the belt tilts and the end when the belt lies flat, both as
    (up, forward) from the origin -- as GSX's Tug 660 gives them in its
    ``[BaggageLoader]`` section."""
    py = float(loader["pivot_up_m"])
    pz = float(loader["pivot_forward_m"])
    dy = float(loader["end_up_m"]) - py
    dz = float(loader["end_forward_m"]) - pz
    length = math.hypot(dy, dz)
    if length <= 0.0:
        return None
    ratio = (sill - py) / length
    if not -1.0 <= ratio <= 1.0:
        return None
    flat = math.atan2(dy, dz)
    angle = math.asin(ratio) - flat
    limit = math.radians(float(loader.get("max_angle_deg", 90.0)))
    angle = max(0.0, min(limit, angle))
    reach = pz + length * math.cos(angle + flat)
    return math.degrees(angle), reach


def docked_loader(door: Point, sill: float, loader: dict, rule: dict
                  ) -> tuple[tuple[float, float, float], float] | None:
    """A belt loader square to the fuselage at a hold: its origin where the
    belt's end, raised to the sill, stops ``belt_gap_m`` short of the hold
    point, and the belt angle that does it. ((right, forward, heading), the
    angle), or None when the belt cannot reach the sill.

    The hold's point is on the skin: the flyable A321's sit 0.6 m right of
    the centreline, which is where the lower lobe is at sill height."""
    found = belt(loader, sill)
    if found is None:
        return None
    angle, reach = found
    heading_out = outward(door)
    out = math.radians(heading_out)
    standoff = reach + float(rule.get("belt_gap_m", 0.5))
    right = door.right + standoff * math.sin(out)
    forward = door.forward + standoff * math.cos(out) + float(rule.get("along_m", 0.0))
    return (right, forward, (heading_out + 180.0) % 360.0), angle


def _between(xs: list, ys: list, x: float) -> float:
    """``ys`` at ``x``, straight between the points of ``xs``."""
    x = max(xs[0], min(xs[-1], x))
    for (x0, y0), (x1, y1) in zip(zip(xs, ys), zip(xs[1:], ys[1:])):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / ((x1 - x0) or 1.0)
    return ys[-1]


def choose_stairs(sill: float, table: dict, listed=None) -> tuple[str, str]:
    """The staircase for a door this high: (title, why). GSX's, the first
    in the table the simulator lists whose own range (``fits``, GSX's
    condition on the sill) holds it; else the simulator's own stairs.

    The CDS 2438 that every door was given reaches 3.879 m; GSX itself
    stops using it at 3.60, and the flyable A321 held off its wheels had
    its doors at 3.96 (Innsbruck, 18 October)."""
    models = table.get("stairs_models", {})
    fallback = table.get("stairs_fallback", "")
    for title, model in models.items():
        if listed is not None and title not in listed:
            continue
        low, high = model.get("fits", (model["heights"][0], model["heights"][-1]))
        if low <= sill <= high:
            return title, f"GSX uses it for sills {low:.2f}-{high:.2f} m"
    if listed is not None and not any(t in listed for t in models):
        return fallback, "the simulator lists none of GSX's staircases"
    return fallback, f"no GSX staircase is made for a {sill:.2f} m sill"


def docked_stairs(door: Point, sill: float, model: dict, rule: dict
                  ) -> tuple[tuple[float, float, float], float]:
    """A staircase square to its door: facing the way the door does, its
    platform at the sill (as high as the model goes) and the platform's
    front edge ``stairs_gap_m`` from the door point, which is on the skin.
    ((right, forward, heading), the elevation to pose it at)."""
    heights, reach = model["heights"], model["reach"]
    elevation = max(heights[0], min(heights[-1], sill))
    ahead = _between(heights, reach, elevation)
    heading_out = outward(door)
    out = math.radians(heading_out)
    standoff = ahead + float(rule.get("stairs_gap_m", 0.15))
    right = door.right + standoff * math.sin(out)
    forward = door.forward + standoff * math.cos(out)
    return (right, forward, (heading_out + 180.0) % 360.0), elevation


def stairs_top(model: dict, elevation: float) -> float:
    """How far ahead of a staircase's origin its platform's front edge is
    at this elevation, metres."""
    return _between(model["heights"], model["reach"], elevation)


def fittings_for(points: list[Point], standing_m: float | None, table: dict,
                 titles: dict[str, str] | None = None,
                 pitch_deg: float = 0.0, listed=None) -> dict[str, dict]:
    """Per role, what its vehicle is raised to at this model: ``sill`` (m
    above the apron) for the stairs, the caterer and the loaders, and for a
    loader whose belt is known (``loaders`` in the table) ``belt_deg`` and
    the spot that puts the belt's end at the hold. The stairs get their
    model too (``title``, ``why``, :func:`choose_stairs` among ``listed``,
    the titles the simulator has; None is any) and, for a staircase whose
    geometry is known, its ``spot`` and ``elevation``."""
    found: dict[str, dict] = {}
    if standing_m is None:
        return found
    loaders = table.get("loaders", {})
    for role, rule in table.get("roles", {}).items():
        door = door_for(role, points)
        if door is None or door.kind not in KIND_NAMES:
            continue
        sill = sill_of(door, standing_m, table, pitch_deg)
        if sill is None:
            continue
        fitting = {"sill": sill}
        if door.kind == PASSENGER and role in ("stairs", "stairs_aft"):
            title, why = choose_stairs(sill, table, listed)
            fitting["title"], fitting["why"] = title, why
            model = table.get("stairs_models", {}).get(title)
            if model:
                fitting["spot"], fitting["elevation"] = docked_stairs(
                    door, sill, model, rule)
        loader = loaders.get((titles or {}).get(role, ""))
        if door.kind == CARGO and loader:
            docked = docked_loader(door, sill, loader, rule)
            if docked is not None:
                fitting["spot"], fitting["belt_deg"] = docked
                _, reach = belt(loader, sill)
                fitting["belt"] = {
                    "foot_m": float(loader["pivot_forward_m"]),
                    "foot_up_m": float(loader["pivot_up_m"]),
                    "top_m": reach, "top_up_m": sill,
                }
        found[role] = fitting
    return found


def corrected(spot: tuple[float, float, float], vehicle: dict | None
              ) -> tuple[float, float, float]:
    """A spot moved by a vehicle model's own correction: metres along its
    own forward and right, and a turn, because a model's origin is where its
    maker put it and its forward is whichever way its maker drew it."""
    if not vehicle:
        return spot
    right, forward, heading = spot
    dr, df = _rotate(float(vehicle.get("right_m", 0.0)),
                     float(vehicle.get("forward_m", 0.0)), heading)
    return (right + dr, forward + df,
            (heading + float(vehicle.get("heading_deg", 0.0))) % 360.0)


def spots_for(points: list[Point], table: dict, titles: dict[str, str] | None = None,
              type_code: str = "",
              fallback: dict[str, tuple[float, float, float]] | None = None,
              fittings: dict[str, dict] | None = None
              ) -> dict[str, tuple[float, float, float]]:
    """Every role that has a spot on this aeroplane: (right, forward,
    heading relative to it), in metres. From its doors first, then the
    per-type table; a role in neither is left to the fractions of length.
    ``titles`` is the vehicle title each role will be drawn with, for its
    model's correction. ``fallback`` is the fractions-of-length spot for
    each role, used where neither has one -- and corrected all the same:
    a vehicle model's origin is where its maker put it whatever the spot
    was worked out from (the stock tug is turned round on an FSLTL A321,
    which reports no power socket, as on any other). A role in
    ``fittings`` with a ``spot`` of its own (a loader docked by its belt,
    :func:`fittings_for`) goes there as it is: it was worked out from the
    model's own geometry, so there is nothing to correct."""
    roles = table.get("roles", {})
    vehicles = table.get("vehicles", {})
    by_type = table.get("types", {}).get((type_code or "").upper(), {})
    found: dict[str, tuple[float, float, float]] = {}
    for role in dict.fromkeys(list(roles) + list(fallback or {})):
        rule = roles.get(role, {})
        docked = (fittings or {}).get(role, {}).get("spot")
        if docked is not None:
            found[role] = tuple(float(v) for v in docked)
            continue
        door = door_for(role, points) if role in roles else None
        if door is not None:
            spot = spot_from_door(door, rule)
        elif role in by_type:
            right, forward, heading = by_type[role]
            spot = (float(right), float(forward), float(heading))
        elif fallback and role in fallback:
            spot = tuple(float(v) for v in fallback[role])
        else:
            continue
        title = (titles or {}).get(role, "")
        found[role] = corrected(spot, vehicles.get(title))
    return found


__all__ = ["Point", "points_from_reading", "door_for", "spot_from_door",
           "corrected", "spots_for", "load_layout", "LAYOUT_FILE",
           "sill_of", "belt", "docked_loader", "fittings_for",
           "choose_stairs", "docked_stairs", "stairs_top",
           "PASSENGER", "CARGO", "FUEL_POINT", "POWER"]
