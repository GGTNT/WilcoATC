"""The ground handling around the other aeroplanes: who visits, where, when.

An invented aeroplane that parks sits on its stand with nothing around it.
The simulator sends its own baggage loaders, caterers and fuel trucks only to
aircraft its ATC owns; the traffic here is created as non-ATC objects (it has
to be, because this program flies them and the simulator's ATC must not), so
the ramp around them was empty, and an apron of twelve airliners with not one
vehicle on it looks like a model, not an airport. FSLTL's aeroplanes are not
touched: they are the simulator's ATC aircraft and it services them itself.

This module decides the *turnaround*: which vehicles call on which aeroplane,
when each one sets off, where it parks beside the fuselage, which way it
faces, and when it drives away. It knows nothing about SimConnect. It takes
the aeroplanes standing on stands and hands back a list of vehicle positions
every tick; :mod:`wilcoatc.integrations.groundvehicles` puts those in the
simulator, the same way :mod:`wilcoatc.integrations.aitraffic` does the
aeroplanes.

Where a vehicle parks is measured from the aeroplane, not from the stand.
The first version took the SDK's airport-services sample at its word and put
each vehicle a stand-radius out at an angle off the nose; those figures turn
out to be where the simulator *spawns* a service before it drives to the
door, and the result was a full turnaround standing twenty metres in front
of an Edelweiss with nothing at its door. So the spots are now offsets in
the aeroplane's own frame -- right of and forward of its reference point,
scaled by the type's fuselage length -- and were calibrated against where
GSX puts its own stairs and loaders around a Fenix A321 on a running 2024:
the forward stairs 13 m ahead of the reference point and 5.6 m to its left,
the aft stairs 20 m behind and 7 m left, the loaders on the right. The
reference point of an airliner model sits near the wing, about four tenths
of the way back, which is why the forward door is a third of the length
ahead of it. Right is where an airliner's cargo and galley doors are; the
stairs go to the left, where the passengers' doors are.

Nothing pops. A vehicle sets off from forty-odd metres out along the same
ray and drives in at a walking-plus pace, and drives out the same way before
it is taken away. An aeroplane that leaves its stand has every vehicle on it
set off at once, and the stand is empty by the time it has turned onto the
taxiway. An aeroplane that was standing there when the pilot loaded in has
been there for some unknown while, so its turnaround is started at a random
point in the past: some of the apron is mid-turnaround and some has finished,
which is what an apron looks like.

There is a budget. Every vehicle is a drawn object in somebody's simulator,
and twelve aeroplanes with five vehicles each is sixty objects for a ramp the
pilot can see a sixth of. So at most ``limit`` are out at once, the ones
nearest the pilot first, and nothing is sent to an aeroplane further away
than ``range_m``; a visit that cannot have a vehicle in its window is simply
missed, which is also what happens at real airports.

With the schedule (``schedule=True``, which is what ``traffic.lod.mode:
schedule`` turns on) the doors have a day rather than being open from the
first frame to the push. An arrival's stairs come half a minute after it
stops -- the engines are winding down, and that half minute is when its
model can be swapped with every door still shut -- and leave with the last
of the deplaning round, so its doors shut and it stands closed up until it
is booked as a departure. An aeroplane found on its stand when the pilot
loaded in is one of those, closed up. Between turnarounds it is still
worked, by the vehicles that need no door (power, fuel), so the apron is
not dead. Boarding brings the stairs, the loaders and the caterer back, and
the close-up is the one a departure always had: holds, then doors.

The order and the timings are data, per kind of aeroplane
(``turnaround.json``: narrowbody, widebody, regional, light), and a step
may wait for a moment rather than a clock: at Bilbao (17 October) the
caterer came forty seconds into boarding, twenty after the forward loader,
and the two trucks stood in each other at an A321 whose galley door is 3.2
m from its forward hold. Now the loaders go to the holds as soon as the
aeroplane stops, the caterer and the fuel only once the passengers are off
(``deboarded``), the caterer first when it boards, and the fuel truck is
sent away when boarding starts unless the profile allows fuelling with
passengers. And each vehicle reserves the ground it parks on and the strip
it drives in along (:mod:`wilcoatc.atc.zones`): one whose ground is taken
stands at a waiting point out along its approach, logged as "<vehicle>
waits for <other> (zone ...)", until the other has gone; one whose straight
way in is blocked tries a few bearings either side first. GSX's airport
profiles were looked at for those waiting points and hold none -- they keep
where GSX's own vehicles *work*, not where they wait -- so the waiting point
is computed, and ``Standing.waits`` is where a better source would go.

With passengers on (``passengers`` > 0, ``traffic.handling.passengers``),
an arrival whose stairs are at its door is met by a bus, and a boarding
aeroplane's passengers are brought by one (:mod:`wilcoatc.atc.passengers`
plans the run). The stairs stay until the last of them is off them, which
the tug already waits for. Only so many buses are out at once, nearest the
pilot first, and an aeroplane the roads give no clear way to gets none.
"""

from __future__ import annotations

import json
import logging
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from . import baggage as bags
from . import passengers as people
from . import zones

log = logging.getLogger(__name__)

# The services, with where each one parks in the aeroplane's frame:
# (metres to the right of the reference point, forward of it as a fraction
# of the fuselage length, forward of it in metres on top, heading relative
# to the aeroplane's once parked). Measured against GSX's placement around a
# real A321 (see the module docstring), and the fraction is what keeps the
# stairs at the forward door of a Dash 8 and of a 777 alike.
PUSHBACK = "pushback"
BAGGAGE = "baggage"
BAGGAGE_AFT = "baggage_aft"
CATERING = "catering"
GPU = "gpu"
STAIRS = "stairs"
STAIRS_AFT = "stairs_aft"
FUEL = "fuel"

SPOTS: dict[str, tuple[float, float, float, float]] = {
    # On the nose gear, facing the aeroplane. Placed by eye at Madeira (the
    # GSX tug at an A321): 0.275 of the length ahead of the reference
    # point. It was half the length and three metres, thirteen metres out.
    PUSHBACK: (0.0, 0.275, 0.0, 180.0),
    # The forward and aft holds, right side, belt nosing in.
    BAGGAGE: (5.5, 0.12, 0.0, -90.0),
    BAGGAGE_AFT: (5.5, -0.35, 0.0, -90.0),
    # The forward galley door, right side, backed up to it.
    CATERING: (5.0, 0.30, 0.0, 90.0),
    # By the nose gear on the right, where the ground power socket is.
    GPU: (4.5, 0.40, 0.0, 45.0),
    # The forward and aft passenger doors, left side, platform to the door.
    STAIRS: (-5.5, 0.30, 0.0, 90.0),
    STAIRS_AFT: (-5.5, -0.42, 0.0, 90.0),
    # Under the right wing's leading edge, nose forward.
    FUEL: (11.0, -0.02, 0.0, 0.0),
}

# The vehicles that have the same shape as another and borrow its titles.
BASE_KIND: dict[str, str] = {STAIRS_AFT: STAIRS, BAGGAGE_AFT: BAGGAGE}

# From this length up an airliner has a second passenger door and a second
# hold worth sending something to: a 737 or an A320 does, an E175 or a
# Dash 8 does not.
TWO_DOORS_FROM_M = 35.0

# Fuselage lengths, in metres, for the types the traffic flies. Anything
# else is taken for an A320 if it is an airliner and a Cessna if it is not.
LENGTHS: dict[str, float] = {
    "A318": 31.4, "A319": 33.8, "A19N": 33.8, "A320": 37.6, "A20N": 37.6,
    "A321": 44.5, "A21N": 44.5, "A306": 54.1, "A332": 58.8, "A333": 63.7,
    "A339": 63.7, "A343": 63.7, "A346": 75.4, "A359": 66.8, "A35K": 73.8,
    "A388": 72.7, "B712": 37.8, "B733": 33.4, "B734": 36.4, "B735": 31.0,
    "B736": 31.2, "B737": 33.6, "B738": 39.5, "B739": 42.1, "B37M": 35.6,
    "B38M": 39.5, "B39M": 42.2, "B744": 70.7, "B748": 76.3, "B752": 47.3,
    "B753": 54.4, "B762": 48.5, "B763": 54.9, "B764": 61.4, "B772": 63.7,
    "B77L": 63.7, "B773": 73.9, "B77W": 73.9, "B788": 56.7, "B789": 62.8,
    "B78X": 68.3, "E170": 29.9, "E175": 31.7, "E75L": 31.7, "E190": 36.2,
    "E195": 38.7, "E290": 36.2, "E295": 41.5, "CRJ2": 26.8, "CRJ7": 32.3,
    "CRJ9": 36.2, "CRJX": 39.1, "AT43": 22.7, "AT45": 22.7, "AT46": 22.7,
    "AT72": 27.2, "AT75": 27.2, "AT76": 27.2, "DH8A": 22.3, "DH8B": 22.3,
    "DH8C": 25.7, "DH8D": 32.8, "MD82": 45.1, "MD83": 45.1, "MD88": 45.1,
    "MD11": 61.2, "SF34": 19.7, "F100": 35.5, "BCS1": 35.0, "BCS3": 38.7,
}
AIRLINER_LENGTH_M = 37.6
LIGHT_LENGTH_M = 8.3


def fuselage_length(type_code: str, light: bool = False) -> float:
    """How long this type is, for where its doors are."""
    code = (type_code or "").upper()
    if code in LENGTHS:
        return LENGTHS[code]
    return LIGHT_LENGTH_M if light else AIRLINER_LENGTH_M


# The height of the forward passenger door's sill above the apron, in
# metres, which is what the stairs and the caterer are raised to. Published
# figures, rounded; the hold sill is about half of it on every type.
SILLS: dict[str, float] = {
    "A318": 3.4, "A319": 3.4, "A19N": 3.4, "A320": 3.4, "A20N": 3.4,
    "A321": 3.4, "A21N": 3.4, "A306": 4.8, "A332": 5.0, "A333": 5.0,
    "A339": 5.0, "A343": 5.0, "A346": 5.0, "A359": 5.3, "A35K": 5.3,
    "A388": 5.3, "B712": 2.6, "B733": 2.7, "B734": 2.7, "B735": 2.7,
    "B736": 2.7, "B737": 2.7, "B738": 2.7, "B739": 2.7, "B37M": 2.7,
    "B38M": 2.7, "B39M": 2.7, "B744": 5.0, "B748": 5.0, "B752": 3.4,
    "B753": 3.4, "B762": 4.4, "B763": 4.4, "B764": 4.4, "B772": 5.2,
    "B77L": 5.2, "B773": 5.2, "B77W": 5.2, "B788": 5.0, "B789": 5.0,
    "B78X": 5.0, "E170": 2.2, "E175": 2.2, "E75L": 2.2, "E190": 2.4,
    "E195": 2.4, "E290": 2.6, "E295": 2.6, "CRJ2": 1.7, "CRJ7": 1.7,
    "CRJ9": 1.7, "CRJX": 1.7, "AT43": 1.5, "AT45": 1.5, "AT46": 1.5,
    "AT72": 1.5, "AT75": 1.5, "AT76": 1.5, "DH8A": 1.7, "DH8B": 1.7,
    "DH8C": 1.7, "DH8D": 1.7, "MD82": 2.4, "MD83": 2.4, "MD88": 2.4,
    "MD11": 5.0, "SF34": 1.4, "F100": 2.6, "BCS1": 2.8, "BCS3": 2.8,
}
AIRLINER_SILL_M = 3.4
LIGHT_SILL_M = 1.0
HOLD_SILL_RATIO = 0.52
# A belt loader is about this long from its pivot to the end of the belt,
# and the pivot is this high off the ground.
BELT_M = 5.5
BELT_PIVOT_M = 0.6


def door_sill(type_code: str, light: bool = False) -> float:
    """How high this type's forward door is."""
    code = (type_code or "").upper()
    if code in SILLS:
        return SILLS[code]
    return LIGHT_SILL_M if light else AIRLINER_SILL_M


def bag_direction(arrived: bool, unloaded: bool, status: str) -> str:
    """Which way the bags go at a hold: out of it (``baggage.UNLOAD``) for
    an aeroplane that has come in and not been unloaded yet, into it
    (``LOAD``) for everything else -- one boarding, or departing, or
    found standing. Both the train and GSX's handler go by it: at
    Innsbruck the handler was unloading through a boarding."""
    if arrived and not unloaded and status not in (BOARDING, DEPARTING):
        return bags.UNLOAD
    return bags.LOAD


def pose(kind: str, sill: float, fitting: dict | None = None
         ) -> dict[str, float]:
    """What a vehicle of this kind is set to once it is at the aeroplane.

    ``sill`` is the type's forward door, from the table; ``fitting`` is
    this vehicle's own door or hold, read off the model where it could be
    (:func:`wilcoatc.atc.servicelayout.fittings_for`): its ``sill`` and,
    for a loader whose belt is known, the ``belt_deg`` that puts the end
    of the belt at it.

    The variables are the ones the simulator's own vehicle models animate
    on (``ModelBehaviorDefs/Asobo/Misc/GroundVehicles.xml``): the stairs and
    the caterer have a height in metres and a deployment in per cent, the
    loader a belt angle, the fuel and power units a hose, the tug a
    coupling. Each is written as its target and its current value both,
    because the model's own code only walks current towards target while
    the model is being drawn, and a set of stairs that was out of view
    would otherwise stand retracted until looked at.
    """
    base = BASE_KIND.get(kind, kind)
    fitting = fitting or {}
    if base in (STAIRS, CATERING):
        # A staircase goes only as high as its model does.
        sill = float(fitting.get("elevation", fitting.get("sill", sill)))
    if base == STAIRS:
        return {"BOARDINGRAMP ELEVATION TARGET": sill,
                "BOARDINGRAMP ELEVATION CURRENT": sill,
                "BOARDINGRAMP ORIENTATION TARGET": 100.0,
                "BOARDINGRAMP ORIENTATION CURRENT": 100.0}
    if base == CATERING:
        return {"CATERINGTRUCK ELEVATION TARGET": sill,
                "CATERINGTRUCK ELEVATION CURRENT": sill,
                "CATERINGTRUCK OPENING TARGET": 100.0,
                "CATERINGTRUCK OPENING CURRENT": 100.0}
    if base == BAGGAGE:
        angle = fitting.get("belt_deg")
        if angle is None:
            hold = float(fitting.get("sill", sill * HOLD_SILL_RATIO))
            rise = max(0.0, hold - BELT_PIVOT_M)
            angle = math.degrees(math.asin(min(1.0, rise / BELT_M)))
        angle = float(angle)
        return {"BAGGAGELOADER ANGLE TARGET": angle,
                "BAGGAGELOADER ANGLE CURRENT": angle}
    if base == FUEL:
        return {"FUELTRUCK HOSE DEPLOYED": 100.0}
    if base == GPU:
        return {"GROUNDPOWERUNIT HOSE DEPLOYED": 100.0}
    if base == PUSHBACK:
        return {"PUSHBACK ATTACHED": 1.0}
    return {}

# How far out a vehicle starts from, and how fast it drives. Forty-five
# metres is across the apron road; four metres a second is a truck that is
# not in a hurry, and it makes the drive about eleven seconds.
APPROACH_M = 45.0
DRIVE_MS = 4.0
DRIVE_S = APPROACH_M / DRIVE_MS

# The turnaround, per kind of aeroplane, is data: turnaround.json beside this
# module (see its _comment). The stairs are at the doors the moment an
# aeroplane stops and stay until it boards, and are not budgeted (see
# Handling._admit); the loaders go to the holds straight away; the caterer
# and the fuel wait for the passengers to be off. Then the aeroplane rests,
# and is worked again: a parked aeroplane that was turned round once and
# then sat dead for the rest of the session was the thing the pilot asked
# about ("baggage handling and catering are missing").
TIMELINE_FILE = Path(__file__).with_name("turnaround.json")
START = "start"
DEBOARDED = "deboarded"


@dataclass(frozen=True)
class Step:
    """One vehicle in a round: what it waits for, how long after, and how
    long it stays (None: until the departure closes up)."""

    kind: str
    after: str
    delay: float
    serve: float | None


@dataclass(frozen=True)
class Profile:
    """One kind of aeroplane's turnaround."""

    name: str
    deboard_s: float
    fuel_with_passengers: bool
    turnaround: tuple[Step, ...]
    quiet: tuple[Step, ...]
    boarding: tuple[Step, ...]
    # The schedule's arrival: the stairs this long after it stops (the
    # engines winding down, and the moment the model may be swapped with
    # its doors shut), and staying this long, so the doors shut with the
    # last of the deplaning round.
    arrival_stairs: tuple[float, float] = (30.0, 1250.0)
    refuel_chance: float = 1.0


def _steps(rows) -> tuple[Step, ...]:
    return tuple(Step(str(row["service"]), str(row.get("after", START)),
                      float(row.get("delay_s", 0.0)),
                      None if row.get("serve_s") is None else float(row["serve_s"]))
                 for row in rows or ())


def load_timelines(path: Path | str | None = None) -> dict:
    """turnaround.json, read into profiles, footprints and the type table."""
    raw = json.loads(Path(path or TIMELINE_FILE).read_text(encoding="utf-8"))
    profiles = {}
    for name, row in raw.get("profiles", {}).items():
        stairs = row.get("arrival_stairs") or {}
        profiles[name] = Profile(
            name, float(row.get("deboard_s", 0.0)),
            bool(row.get("fuel_with_passengers", False)),
            _steps(row.get("turnaround")), _steps(row.get("quiet")),
            _steps(row.get("boarding")),
            (float(stairs.get("delay_s", 30.0)), float(stairs.get("serve_s", 1250.0))),
            float(row.get("refuel_chance", 1.0)))
    footprints = {kind: zones.Footprint(float(f["ahead_m"]), float(f["behind_m"]),
                                        float(f["half_width_m"]))
                  for kind, f in raw.get("footprints", {}).items()}
    return {"profiles": profiles, "types": dict(raw.get("types", {})),
            "baggage": dict(raw.get("baggage", {})),
            "airliner": raw.get("airliner", "narrowbody"),
            "light": raw.get("light", "light"),
            "footprints": footprints, "margin": float(raw.get("margin_m", 0.0))}


TIMELINES = load_timelines()


def profile_for(type_code: str, light: bool = False,
                table: dict | None = None) -> Profile:
    """Which turnaround this type gets: its own (a business jet's airstair,
    a Cessna's fuel truck), else a light aircraft's or an airliner's."""
    table = table or TIMELINES
    name = table["types"].get((type_code or "").upper())
    if name in LIGHT_PROFILES:
        return table["profiles"][name]
    if light:
        # Flown as a light aircraft, whatever the type says.
        return table["profiles"][table["light"]]
    return table["profiles"][name or table["airliner"]]


# The profiles that are not an airliner's: no stairs, no loaders, no
# caterer, no passengers walked or bussed, no bags.
LIGHT_PROFILES = frozenset({"light", "business"})


def is_light(type_code: str, private: bool = False,
             table: dict | None = None) -> bool:
    """Whether this aeroplane is handled as a light aircraft rather than an
    airliner.

    By its type: the turnaround table's own entry first, then the general-
    aviation fleet. It used to be ``performance_for(...) is LIGHT``, and any
    type with a rotation speed of its own -- the C172, the Bonanza, the TBM
    -- is a copy of LIGHT, not LIGHT itself: a Cessna at Kortrijk was sent
    a caterer, two loaders and two sets of stairs. ``private`` (no airline)
    makes a type nobody has listed light, as it always did.
    """
    from . import fleet
    from .flying import _LIGHT_TYPES

    table = table or TIMELINES
    code = (type_code or "").upper()
    name = table["types"].get(code)
    if name:
        return name in LIGHT_PROFILES
    if code in fleet.GA_TYPES or code in _LIGHT_TYPES:
        return True
    return bool(private) and not code


# How long a parked aeroplane rests between one round of handling and the
# next.
REST_S = 600.0
# A departure: the last bags go on for this long, the holds shut, the
# stairs leave and the passenger doors shut behind them, and the tug comes
# this long after every door has been read shut. The first version called
# the tug thirty seconds after boarding while a loader stood at an open
# hold for five minutes more; with the doors gating the push, that was a
# departure waiting five minutes with a tug on its nose.
LOADING_S = 75.0
#: The holds shut this long before the boarding ends and the passenger doors
#: follow them: the last cart is done by then.
HOLDS_SHUT_BEFORE_S = 30.0
TUG_AFTER_READY_S = 5.0
# The ground each parked vehicle covers, and the clearance kept all round
# (turnaround.json); a vehicle with no footprint of its own gets the default.
FOOTPRINTS: dict = TIMELINES["footprints"]
MARGIN_M: float = TIMELINES["margin"]
DEFAULT_FOOTPRINT = zones.Footprint(3.0, 3.0, 1.3)
# How wide an airliner's body is taken to be, half of it, for keeping a way
# in from crossing it; and how far away another aeroplane's vehicles can
# still be in the way.
FUSELAGE_HALF_M = 2.5
NEIGHBOURS_M = 150.0
# The bearings a vehicle tries for its way in when the straight one is
# blocked, in that order: the straight one, then either side.
BEARINGS = (0.0, 20.0, -20.0, 40.0, -40.0, 60.0, -60.0, 80.0, -80.0, 100.0, -100.0)
# Which vehicles must be gone before the tug is called: the ones at a door.
_AT_A_DOOR = frozenset({STAIRS, STAIRS_AFT, BAGGAGE, BAGGAGE_AFT, CATERING})
# Each time is jittered by up to this much either way, so two aeroplanes that
# parked together are not serviced in lock-step.
JITTER = 0.2

# The visits that come whatever the budget says: the tug, because a
# departure that pushes with nothing on its nose is the one thing a pilot on
# the next stand is certain to notice; the stairs, because an aeroplane is
# meant to have them the moment it appears, and one set per parked aeroplane
# is a known cost. Both are still kept inside ``range_m`` of the pilot.
_UNBUDGETED = frozenset({PUSHBACK, STAIRS, STAIRS_AFT})
_PRIORITY = {PUSHBACK: 0.0, STAIRS: 0.5, STAIRS_AFT: 0.5}

# Which door each vehicle works, by role. Which interactive point that is
# on a given model is the door table's business (integrations/doors.json):
# the first version numbered them here, and on the A321 "door 3" was the
# second passenger door, not the hold the loader was standing at. The
# stairs take the left-hand passenger doors, the loaders the holds, the
# caterer the forward galley door on the right.
DOOR_ROLE = {STAIRS: "pax_l1", STAIRS_AFT: "pax_l2", BAGGAGE: "cargo_fwd",
             BAGGAGE_AFT: "cargo_aft", CATERING: "pax_r1"}

# What a vehicle is doing: for the simulator (moving or not) and the tests.
WAITING = "waiting"
# Out, but standing at its waiting point: its ground or its way in is taken.
HOLDING = "holding"
ARRIVING = "arriving"
SERVING = "serving"
LEAVING = "leaving"
GONE = "gone"

# The aeroplane's own situation, as the traffic reports it.
PARKED = "parked"          # standing at the gate, going nowhere yet
ARRIVED = "arrived"        # has just taxied in and stopped
DEPARTING = "departing"    # boarded, waiting to push
BOARDING = "boarding"      # the schedule's boarding: doors open, not yet off


@dataclass
class Standing:
    """One of this program's aeroplanes on a stand, as the turnaround sees it.

    ``since`` is when it parked, or 0 when nobody knows -- an aeroplane that
    was set out on the apron when the pilot loaded in.
    """

    callsign: str
    x: float
    z: float
    heading: float
    radius: float
    status: str
    since: float = 0.0
    light: bool = False
    gate: bool = False
    # The ICAO type, for how long it is and so where its doors are.
    type_code: str = ""
    # Somebody else's aeroplane -- FSLTL's, the simulator's own -- rather
    # than one this program drew. Handled the same; never told to push.
    observed: bool = False
    # Which object it is in the simulator, for the doors; 0 if not known.
    object_id: int = 0
    # The simulator title it was drawn with, which is what says whether
    # its model has doors that move, and which point is which door.
    title: str = ""
    # Where each vehicle goes on this model, from its own doors
    # (wilcoatc.atc.servicelayout): role -> (right, forward, heading
    # relative to the aeroplane), metres. None: by fractions of length.
    spots: dict | None = None
    # What each vehicle is raised to on this model, from its own doors:
    # role -> {"sill": metres off the apron, "belt_deg": ...}. None: the
    # type's table (see pose).
    fittings: dict | None = None
    # Where a vehicle waits while its ground is taken, by kind: (x, z,
    # heading) in the layout's metres. None: out along its approach.
    waits: dict | None = None
    # The airline's ICAO code, for how many bags its passengers bring, and
    # when a boarding aeroplane's doors close (the holds first), which the
    # loading has to be done by: never later, whatever the bags.
    operator: str = ""
    closes_at: float | None = None


@dataclass
class Visit:
    """One vehicle's call on one aeroplane.

    ``serve_s`` is how long it stays once it has parked; None is until the
    aeroplane leaves (the tug). ``in_place`` says it was already there when
    the pilot arrived, so if it is admitted on the tick that made it, it is
    drawn parked rather than driven in across an apron they are looking at.
    """

    kind: str
    arrive_at: float
    serve_s: float | None
    started: float | None = None
    # When it was sent away early, and how far in it had got (1.0 is all the
    # way), so a vehicle turned round half way drives back from where it was.
    leaving: float | None = None
    came: float = 1.0
    gone: bool = False
    in_place: bool = False
    # Part of an arrival's deplaning round, which is the schedule's arrival
    # window: it is open while any of these at a door has not gone.
    arrival: bool = False
    # What it waits for (START, DEBOARDED) and how long after; arrive_at is
    # infinite until that moment is known. ``begun`` is when its round
    # began, and ``deplaning`` whether that round has passengers to get off.
    after: str = START
    delay: float = 0.0
    begun: float = 0.0
    deplaning: bool = False
    # Standing at a waiting point since then, because its ground or its way
    # in was taken; where (x, z, heading); and who it is waiting for.
    holding: float | None = None
    wait_at: tuple[float, float, float] | None = None
    waiting_for: str = ""
    # The bearing it drives in along, when the straight one was blocked.
    bearing: float | None = None

    def leaves_at(self) -> float | None:
        if self.leaving is not None:
            return self.leaving
        if self.started is not None and self.serve_s is not None:
            return self.started + DRIVE_S + self.serve_s
        return None

    def state(self, now: float) -> str:
        if self.gone:
            return GONE
        if self.started is None:
            return WAITING if self.holding is None else HOLDING
        leaves = self.leaves_at()
        if leaves is not None and now >= leaves:
            return LEAVING if now < leaves + DRIVE_S * self.came else GONE
        if now < self.started + DRIVE_S:
            return ARRIVING
        return SERVING

    def expired(self, now: float) -> bool:
        """Whether a visit never started has missed its window."""
        return (self.started is None and self.holding is None
                and self.serve_s is not None
                and now > self.arrive_at + self.serve_s)

    def send_away(self, now: float) -> None:
        state = self.state(now)
        if state in (WAITING, HOLDING):
            self.gone = True
        elif state == ARRIVING:
            self.came = min(1.0, max(0.0, (now - self.started) / DRIVE_S))
            self.leaving = now
        elif state == SERVING:
            self.leaving = now


@dataclass
class Turnaround:
    """Everything that calls on one aeroplane while it stands there."""

    callsign: str
    x: float
    z: float
    heading: float
    radius: float
    light: bool
    gate: bool
    status: str
    # The fuselage length, which is what the spots are scaled by, and the
    # forward door's height, which is what the stairs are raised to.
    length: float = AIRLINER_LENGTH_M
    sill: float = AIRLINER_SILL_M
    # From the model's own doors, where known; see Standing.spots and
    # Standing.fittings.
    spots: dict | None = None
    fittings: dict | None = None
    # Its kind of turnaround (turnaround.json), and where its vehicles wait
    # if somebody knows better than the approach (see Standing.waits).
    profile: Profile | None = None
    waits: dict | None = None
    # When the next round of handling may start, once this one is over.
    rest_until: float | None = None
    visits: list[Visit] = field(default_factory=list)
    # When the aeroplane last stood here. Once it has gone its vehicles drive
    # off, and the turnaround is kept until the last of them has.
    seen: float = 0.0
    present: bool = True
    # Whether it taxied in while the pilot was here, rather than being found
    # on its stand: only a real arrival has passengers to get off.
    arrived: bool = False
    # Who is waiting for whom, as last logged: vehicle -> blocker.
    waiting: dict = field(default_factory=dict)
    # The buses (wilcoatc.atc.passengers.Run), and which kinds of run have
    # been planned or tried, so an aeroplane the roads cannot reach is not
    # tried again every tick.
    runs: list = field(default_factory=list)
    tried: set = field(default_factory=set)
    # The baggage trains (wilcoatc.atc.baggage.Run), and whether its bags
    # have been taken off yet: the first run at an arrival unloads, every
    # one after it loads.
    bag_runs: list = field(default_factory=list)
    unloaded: bool = False
    # Carts still owed, by (hold, way), counted once from the flight's bags;
    # and how many trains each hold has had.
    bag_left: dict = field(default_factory=dict)
    bag_trains: dict = field(default_factory=dict)
    type_code: str = ""
    operator: str = ""
    closes_at: float | None = None

    def has(self, kind: str, now: float) -> bool:
        """Whether a vehicle of this kind is still owed or still here."""
        return any(v.kind == kind and v.state(now) != GONE
                   for v in self.visits)

    def active(self, now: float) -> list[Visit]:
        """The visits with a vehicle out: driving in, parked, driving off."""
        return [v for v in self.visits
                if v.state(now) in (ARRIVING, SERVING, LEAVING, HOLDING)]

    def send_everyone_away(self, now: float) -> None:
        for visit in self.visits:
            visit.send_away(now)

    def finished(self, now: float) -> bool:
        return not self.present and all(
            v.state(now) == GONE for v in self.visits) and all(
            run.finished(now) for run in self.runs + self.bag_runs)

    def spot(self, kind: str) -> tuple[float, float, float, float]:
        """Where a vehicle of this kind parks: x, z, heading, and the bearing
        it drives in along, which is straight out from the aeroplane.

        From the model's own doors where they were read (``spots``),
        otherwise from the fractions of length in :data:`SPOTS`.
        """
        if self.spots and kind in self.spots:
            right, forward, facing = self.spots[kind]
        else:
            right, fraction, extra, facing = SPOTS[kind]
            forward = fraction * self.length + extra
        heading = math.radians(self.heading)
        # Forward is along the heading; right is ninety degrees clockwise
        # of it. x is east and z is north.
        x = self.x + right * math.cos(heading) + forward * math.sin(heading)
        z = self.z - right * math.sin(heading) + forward * math.cos(heading)
        bearing = math.degrees(math.atan2(x - self.x, z - self.z)) % 360.0
        return x, z, (self.heading + facing) % 360.0, bearing

    def approach(self, visit: Visit) -> tuple[float, float]:
        """Where its drive in starts: out along its bearing."""
        sx, sz, _, bearing = self.spot(visit.kind)
        if visit.bearing is not None:
            bearing = visit.bearing
        return (sx + APPROACH_M * math.sin(math.radians(bearing)),
                sz + APPROACH_M * math.cos(math.radians(bearing)))

    def wait_spot(self, visit: Visit) -> tuple[float, float, float]:
        """Where it stands while its ground is taken: where somebody said
        (``waits``), else the start of its way in, facing its spot."""
        if self.waits and BASE_KIND.get(visit.kind, visit.kind) in self.waits:
            return tuple(self.waits[BASE_KIND.get(visit.kind, visit.kind)])
        ax, az = self.approach(visit)
        sx, sz, _, _ = self.spot(visit.kind)
        return ax, az, paths.bearing(sx - ax, sz - az)

    def parked_zone(self, visit: Visit, margin: float = 0.0) -> zones.Zone:
        sx, sz, facing, _ = self.spot(visit.kind)
        size = FOOTPRINTS.get(visit.kind) or DEFAULT_FOOTPRINT
        return zones.Zone(f"{self.callsign}/{visit.kind}",
                          zones.NAMES.get(visit.kind, visit.kind),
                          zones.footprint(sx, sz, facing, size, margin))

    def way_in(self, visit: Visit, start=None) -> list[tuple]:
        """The strip it drives along, from its waiting point or its
        approach to its spot."""
        sx, sz, _, _ = self.spot(visit.kind)
        if start is None:
            start = visit.wait_at[:2] if visit.wait_at else self.approach(visit)
        size = FOOTPRINTS.get(visit.kind) or DEFAULT_FOOTPRINT
        return [zones.strip(start, (sx, sz), size.half_width_m + MARGIN_M)]

    def fuselage(self) -> tuple:
        """The aeroplane's own body, which no way in may cross."""
        return zones.rectangle(self.x, self.z, self.heading, self.length * 0.45,
                               self.length * 0.55, FUSELAGE_HALF_M)

    def path(self, visit: Visit, now: float) -> list[paths.Segment]:
        """The drive this vehicle is on, or has just finished: in, along a
        curve that lines it up on the heading it parks at, nose first;
        out, a short reverse straight off its spot and a turn away. In
        metres in the layout plane, on the same clock as everything else.
        """
        state = visit.state(now)
        if state in (WAITING, GONE) or visit.started is None:
            return []
        sx, sz, facing, _ = self.spot(visit.kind)
        ax, az = self.approach(visit)
        # From where it waited, if it did; from out on its approach if not.
        come_from = visit.wait_at[:2] if visit.wait_at else (ax, az)
        arriving = paths.drive_in(come_from, (sx, sz), facing, visit.started,
                                  visit.started + DRIVE_S)
        leaves = visit.leaves_at()
        if leaves is None or now < leaves:
            return arriving
        span = DRIVE_S * visit.came
        if visit.came >= 0.999:
            return paths.drive_out((sx, sz), facing, (ax, az), leaves,
                                   leaves + span)
        # Turned round on the way in: away from wherever it had got to.
        got = paths.evaluate(arriving, leaves)
        qx, qz, heading, _ = got
        return paths.drive_out((qx, qz), heading, (ax, az), leaves,
                               leaves + max(span, 1.0), backs_out=False)

    def place(self, visit: Visit, now: float) -> tuple[float, float, float, bool] | None:
        """Where this vehicle is right now: x, z, heading, moving.

        None when there is nothing to draw.
        """
        state = visit.state(now)
        if state in (WAITING, GONE):
            return None
        if state == HOLDING:
            x, z, heading = visit.wait_at
            return x, z, heading, False
        if state == SERVING:
            sx, sz, facing, _ = self.spot(visit.kind)
            return sx, sz, facing, False
        found = paths.evaluate(self.path(visit, now), now)
        if found is None:
            return None
        x, z, heading, _ = found
        return x, z, heading, True


class Handling:
    """The turnarounds on an apron, and where every vehicle on it is."""

    #: How many buses may be out at once, across the apron.
    BUSES = 2
    #: And how many baggage trains.
    TRAINS = 2

    def __init__(self, seed: int | None = None, limit: int = 8,
                 range_m: float = 700.0, schedule: bool = False,
                 time_scale: float = 1.0, passengers: int = 0,
                 baggage: bool = False):
        self.limit = max(0, int(limit))
        # The bags: a train at each loader, and the bags on its belt.
        self.bag_planner = bags.Planner(baggage, seed)
        self.bag_table = TIMELINES.get("baggage", {})
        # How many passengers each bus run shows; 0 is no buses at all.
        self.planner = people.Planner(passengers, seed)
        # Where the pilot's own aeroplane is (x, z, heading, length), for
        # keeping the buses off it. Set by the engine.
        self.pilot_footprint: tuple[float, float, float, float] | None = None
        self.schedule = schedule
        # A test setting (traffic.debug.time_scale): every timing of the
        # turnaround divided by it. The drive in and out is not -- a truck
        # at five times the pace is not a faster turnaround, it is a blur.
        self.time_scale = max(0.01, float(time_scale))
        self.range_m = float(range_m)
        self._seed = seed
        self._turns: dict[str, Turnaround] = {}
        # The field's published elevation, which is what a vehicle is
        # created at; the simulator then puts it on the surface itself.
        self.elevation_ft = 0.0

    # -- what is out there ---------------------------------------------

    @property
    def turnarounds(self) -> list[Turnaround]:
        return list(self._turns.values())

    def vehicles_out(self, now: float) -> int:
        return sum(len(t.active(now)) for t in self._turns.values())

    def budgeted_out(self, now: float) -> int:
        """The vehicles out that count against the budget: everything but
        the tugs and the stairs."""
        return sum(1 for t in self._turns.values() for v in t.active(now)
                   if v.kind not in _UNBUDGETED)

    def doors(self, now: float) -> dict[str, set[str]]:
        """Which of each aeroplane's doors are open, by role, by the key
        the turnaround knows the aeroplane by.

        A door is open while the vehicle for it is at the aeroplane or on
        its way: the forward passenger door with the stairs, the aft one
        with the aft stairs, the holds with their loaders, the galley door
        with the caterer. Roles, not numbers: which point that is on this
        model is the door table's business.
        """
        found: dict[str, set[str]] = {}
        for turn in self._turns.values():
            open_: set[str] = set()
            for visit in turn.visits:
                if visit.state(now) not in (ARRIVING, SERVING):
                    continue
                role = DOOR_ROLE.get(visit.kind)
                if role is not None:
                    open_.add(role)
            found[turn.callsign] = open_
        return found

    def arrival_ends_at(self, callsign: str) -> float | None:
        """When the last of the deplaning round will have left the doors,
        as planned now; None with no round. For the debug status."""
        turn = self._turns.get(callsign)
        if turn is None:
            return None
        ends = []
        for visit in turn.visits:
            if not (visit.arrival and visit.kind in DOOR_ROLE) or visit.gone:
                continue
            leaves = visit.leaves_at()
            if leaves is None and visit.serve_s is not None:
                leaves = visit.arrive_at + DRIVE_S + visit.serve_s
            # A step still waiting for the passengers to be off has no time.
            if leaves is not None and not math.isinf(leaves):
                ends.append(leaves)
        return max(ends) if ends else None

    def tug_due_at(self, callsign: str) -> float | None:
        """When the tug sets off for this departure, once it has been
        called; None before that. For the debug status."""
        turn = self._turns.get(callsign)
        if turn is None:
            return None
        for visit in turn.visits:
            if visit.kind == PUSHBACK and not visit.gone:
                return visit.arrive_at
        return None

    def arrival_open(self, callsign: str, now: float) -> bool:
        """Whether this aeroplane's deplaning round is still going: some
        vehicle of it at a door, or still owed to one. The schedule's
        arrival window, as far as the ramp is concerned; the doors reading
        shut is the other half."""
        turn = self._turns.get(callsign)
        if turn is None:
            return False
        return any(v.arrival and v.kind in DOOR_ROLE and v.state(now) != GONE
                   for v in turn.visits)
    def _rng(self, callsign: str) -> random.Random:
        # Per aeroplane and stable, so a flight's turnaround is the same
        # whichever tick first sees it, and a fixed seed repeats a session.
        return random.Random(f"{self._seed}:{callsign}")

    def _jitter(self, rng: random.Random, seconds: float) -> float:
        return seconds * rng.uniform(1.0 - JITTER, 1.0 + JITTER) / self.time_scale

    # -- learning about the aeroplanes --------------------------------

    def _begin(self, one: Standing, now: float) -> Turnaround:
        rng = self._rng(one.callsign)
        turn = Turnaround(one.callsign, one.x, one.z, one.heading,
                          one.radius, one.light, one.gate, one.status,
                          length=fuselage_length(one.type_code, one.light),
                          sill=(one.fittings or {}).get(STAIRS, {}).get(
                              "sill", door_sill(one.type_code, one.light)),
                          seen=now, spots=one.spots, fittings=one.fittings,
                          profile=profile_for(one.type_code, one.light),
                          waits=one.waits,
                          arrived=one.since > 0.0 and one.status in (ARRIVED, PARKED),
                          type_code=one.type_code, operator=one.operator,
                          closes_at=one.closes_at)
        if one.status == DEPARTING:
            # Boarded before anybody looked: the tug is all that is owed.
            self._board(turn, rng, now)
            return turn
        if self.schedule:
            if one.status == BOARDING:
                self._open_for_boarding(turn, rng, now)
            elif one.since > 0.0:
                # Blocked in: the deplaning round, from when it stopped.
                self._plan(turn, rng, one.since, now, arrival=True,
                           deplaning=True)
            else:
                # Found closed up on its stand: only what needs no door.
                self._plan(turn, rng, now, now, in_place=True, quiet=True)
            return turn
        # One that has just arrived parked when it says; one found standing
        # there is worked from the moment it is found, with its stairs
        # already up.
        started = one.since if one.since > 0.0 else now
        found_there = one.status == PARKED and one.since <= 0.0
        self._plan(turn, rng, started, now, in_place=found_there,
                   deplaning=turn.arrived)
        return turn

    def _plan(self, turn: Turnaround, rng: random.Random, started: float,
              now: float, in_place: bool = False,
              stairs: bool = True, arrival: bool = False,
              quiet: bool = False, deplaning: bool = False) -> None:
        """Lay out a round of handling from ``started``.

        Stairs at a gate too: a jetway would be the thing there, and nothing
        docks one to an aeroplane that is not the simulator's own ATC
        aircraft, so the passengers would otherwise have no way off. The
        aft stairs and the aft loader only on a type long enough to have
        the doors for them.

        ``arrival`` is the schedule's deplaning round: the stairs come once
        the engines are off and go with the last bags, and every visit is
        marked as the arrival's. ``quiet`` is the schedule's round between
        turnarounds, with nothing that opens a door. ``deplaning`` says
        there are passengers to get off, which is what a step waiting for
        ``deboarded`` waits for; without them it is the round's start.
        """
        profile = turn.profile or profile_for("", turn.light)
        plan = profile.quiet if quiet else profile.turnaround
        if rng.random() > profile.refuel_chance:
            plan = ()
        deplaning = deplaning and stairs and not quiet
        for step in plan:
            kind, after, serve = step.kind, step.delay, step.serve
            if kind in (STAIRS, STAIRS_AFT) and not stairs:
                continue
            if kind in (STAIRS_AFT, BAGGAGE_AFT) and turn.length < TWO_DOORS_FROM_M:
                continue
            if arrival and kind in (STAIRS, STAIRS_AFT):
                after += profile.arrival_stairs[0]
                serve = profile.arrival_stairs[1]
            delay = self._jitter(rng, after)
            waits = step.after == DEBOARDED and deplaning
            visit = Visit(kind, math.inf if waits else started + delay,
                          None if serve is None else self._jitter(rng, serve),
                          arrival=arrival, after=step.after, delay=delay,
                          begun=started, deplaning=deplaning)
            if waits:
                turn.visits.append(visit)
                continue
            if visit.expired(now):
                visit.gone = True
            elif visit.arrive_at < now - DRIVE_S or (
                    in_place and visit.arrive_at <= now):
                # Already there when the pilot loaded in. Still subject to
                # the budget, which is why it is not started here.
                visit.in_place = True
            turn.visits.append(visit)

    def _rest_and_repeat(self, turn: Turnaround, now: float) -> None:
        """Once a round of handling is over, rest, then start the next."""
        if turn.status != PARKED or turn.light:
            return
        timed = [v for v in turn.visits if v.serve_s is not None]
        if any(v.state(now) != GONE for v in timed):
            turn.rest_until = None
            return
        if turn.rest_until is None:
            turn.rest_until = now + REST_S / self.time_scale
            return
        if now >= turn.rest_until:
            turn.rest_until = None
            turn.visits = [v for v in turn.visits if v.state(now) != GONE]
            self._plan(turn, self._rng(turn.callsign), now, now, stairs=False,
                       quiet=self.schedule)

    def _open_for_boarding(self, turn: Turnaround, rng: random.Random,
                           now: float) -> None:
        """The schedule's boarding: stairs, loaders and the caterer to the
        doors, staying until the departure closes up -- which is
        :meth:`_board`'s business, holds first and doors last, as it always
        was."""
        turn.rest_until = None
        profile = turn.profile or profile_for("", turn.light)
        if not profile.fuel_with_passengers:
            for visit in turn.visits:
                if visit.kind == FUEL and visit.state(now) != GONE:
                    visit.send_away(now)
                    log.info("handling: %s/%s leaves: %s is boarding",
                             turn.callsign, FUEL, turn.callsign)
        if turn.light:
            return
        for step in profile.boarding:
            kind = step.kind
            if kind in (STAIRS_AFT, BAGGAGE_AFT) and turn.length < TWO_DOORS_FROM_M:
                continue
            if turn.has(kind, now):
                continue
            turn.visits.append(Visit(
                kind, now + self._jitter(rng, step.delay),
                None if step.serve is None else self._jitter(rng, step.serve),
                begun=now))

    def _board(self, turn: Turnaround, rng: random.Random, now: float) -> None:
        """A departure: the last bags go on, the holds shut, then the
        stairs go and the passenger doors shut behind them.

        The tug is not called here: it comes once every door has been read
        shut (see :meth:`_update`), which is what a real crew waits for. No
        power unit, caterer or fuel either: the aeroplane has had its
        turnaround, and a vehicle that stays until the push would sit in a
        budget slot the tug needs.
        """
        turn.rest_until = None
        loader = None
        for visit in turn.visits:
            if visit.kind in (BAGGAGE, BAGGAGE_AFT):
                if visit.state(now) in (ARRIVING, SERVING):
                    # Already at the hold: it finishes the last bags and
                    # goes, rather than loading for the rest of its visit.
                    visit.leaving = min(visit.leaves_at() or float("inf"),
                                        now + self._jitter(rng, LOADING_S))
                    loader = visit
                    for run in turn.bag_runs:
                        if run.hold == visit.kind and not run.finished(now):
                            bags.cut(run, now)
                elif visit.state(now) in (WAITING, HOLDING):
                    visit.gone = True
            elif visit.kind not in (STAIRS, STAIRS_AFT, PUSHBACK):
                visit.send_away(now)
        if loader is None and not turn.light:
            loader = Visit(BAGGAGE, now + self._jitter(rng, 5.0),
                           self._jitter(rng, LOADING_S))
            turn.visits.append(loader)
        # The stairs stay until the holds are shut, then go: holds first,
        # passenger doors last, which is the order a ramp closes up in.
        holds_shut = loader.leaves_at() if loader is not None else None
        for visit in turn.visits:
            if visit.kind not in (STAIRS, STAIRS_AFT):
                continue
            state = visit.state(now)
            if holds_shut is None or state in (WAITING, HOLDING):
                visit.send_away(now)
            elif state in (ARRIVING, SERVING):
                visit.leaving = holds_shut

    def _call_the_tug(self, turn: Turnaround, now: float,
                      ready: set[str] | None) -> None:
        """The tug, once nothing is at a door and every door reads shut.

        ``ready`` is what the doors say; None means nobody is reading them,
        and the vehicles leaving is all there is to wait for.
        """
        if turn.has(PUSHBACK, now):
            return
        if any(v.kind in _AT_A_DOOR
               and v.state(now) in (WAITING, HOLDING, ARRIVING, SERVING)
               for v in turn.visits):
            return
        if ready is not None and turn.callsign not in ready:
            return
        turn.visits.append(Visit(
            PUSHBACK, now + self._jitter(self._rng(turn.callsign),
                                         TUG_AFTER_READY_S), None))

    def _update(self, turn: Turnaround, one: Standing, now: float,
                ready: set[str] | None = None) -> None:
        turn.seen = now
        turn.present = True
        turn.x, turn.z, turn.heading = one.x, one.z, one.heading
        if one.spots is not None:
            # The doors, once read; they arrive a tick or two after the
            # aeroplane does.
            turn.spots = one.spots
        if one.fittings is not None:
            turn.fittings = one.fittings
            door = one.fittings.get(STAIRS, {}).get("sill")
            if door is not None:
                # The passengers climb to the door the stairs are at.
                turn.sill = door
        if one.waits is not None:
            turn.waits = one.waits
        turn.closes_at = one.closes_at
        if one.status == DEPARTING and turn.status != DEPARTING:
            self._board(turn, self._rng(one.callsign), now)
        elif (one.status == BOARDING and turn.status != BOARDING
              and self.schedule):
            self._open_for_boarding(turn, self._rng(one.callsign), now)
        turn.status = one.status
        if turn.status == DEPARTING:
            self._call_the_tug(turn, now, ready)
        self._rest_and_repeat(turn, now)

    # -- the tick ------------------------------------------------------

    def positions(self, standing: list[Standing], layout,
                  user_xy: tuple[float, float] | None,
                  now: float, ready: set[str] | None = None) -> list[dict]:
        """Where every vehicle is, for the simulator.

        ``standing`` is every one of this program's aeroplanes on a stand
        right now; one that was here last tick and is not now has left, and
        its vehicles drive off. ``layout`` turns metres into degrees.
        ``ready`` is the aeroplanes whose doors have been read shut, which
        is what a departure's tug waits for; None when nobody is reading
        the doors.
        """
        here = {one.callsign: one for one in standing}
        for callsign, one in here.items():
            turn = self._turns.get(callsign)
            if turn is None:
                self._turns[callsign] = self._begin(one, now)
            else:
                self._update(turn, one, now, ready)
        for callsign, turn in self._turns.items():
            if callsign not in here and turn.present:
                turn.present = False
                turn.send_everyone_away(now)
        for callsign in [c for c, t in self._turns.items() if t.finished(now)]:
            del self._turns[callsign]

        for turn in self._turns.values():
            self._resolve(turn, now)
        self._admit(user_xy, now)
        self._buses(layout, user_xy, now)
        self._trains(user_xy, now)

        found: list[dict] = []
        if layout is None:
            return found
        for turn in self._turns.values():
            for visit in turn.visits:
                placed = turn.place(visit, now)
                if placed is None:
                    continue
                x, z, heading, moving = placed
                lat, lon = layout.to_latlon(x, z)
                found.append({
                    "id": f"{turn.callsign}/{visit.kind}",
                    "callsign": turn.callsign,
                    "kind": visit.kind,
                    "pose": pose(visit.kind, turn.sill,
                                 (turn.fittings or {}).get(visit.kind)),
                    # The model chosen for this door, where one was.
                    "title": (turn.fittings or {}).get(visit.kind, {}).get("title", ""),
                    "lat": round(lat, 7),
                    "lon": round(lon, 7),
                    "heading": round(heading, 1),
                    "altitude_ft": self.elevation_ft,
                    "moving": moving,
                    "state": visit.state(now),
                    # The whole drive, for whoever moves it every frame:
                    # in metres, with the means to turn them into degrees.
                    "path": (turn.path(visit, now) if moving else []),
                    "to_latlon": layout.to_latlon,
                })
                if visit.kind in (BAGGAGE, BAGGAGE_AFT):
                    # Where it stands, for GSX's handler the vehicle layer
                    # puts on it once it exists (groundvehicles.OPERATORS);
                    # his clip is his, not the loader's -- and which way
                    # he works: the train's at this hold, or what one
                    # sent now would do.
                    found[-1]["path"] = turn.path(visit, now)
                    # The one frame its belt's bags and its handler play
                    # (wilcoatc.atc.baggage): the train's at this hold, or
                    # his idle pose with no cart there.
                    working = [r for r in turn.bag_runs
                               if r.hold == visit.kind and not r.finished(now)]
                    frames = working[-1].frames if working else [(0.0, bags.IDLE)]
                    found[-1]["frames"] = frames
                    if working and not placed[3]:
                        found[-1]["channels"] = [
                            ("WAGON BACK LINK ORIENTATION", "number", frames),
                            ("WAGON FRONT LINK ORIENTATION", "number",
                             working[-1].belt)]
            for run in turn.runs:
                found.extend(self._run_positions(turn, run, layout, now))
            for run in turn.bag_runs:
                found.extend(self._train_positions(turn, run, layout, now))
        return found

    # -- the bags --------------------------------------------------------

    def _trains(self, user_xy, now: float) -> None:
        """A train at every loader that is in place, nearest the pilot
        first, as many as may be out; and every loader kept until its last
        bag is off the belt."""
        if not self.bag_planner.enabled:
            return
        out = sum(1 for t in self._turns.values() for r in t.bag_runs if r.out(now))
        due = []
        for turn in self._turns.values():
            if not turn.present or turn.light:
                continue
            for visit in turn.visits:
                if visit.kind not in (BAGGAGE, BAGGAGE_AFT) or visit.state(now) != SERVING:
                    continue
                # Only at GSX's loader, whose belt carries the bags itself:
                # the simulator's own works without any, never with
                # suitcases floating up a belt of somebody's reckoning.
                if not (turn.fittings or {}).get(visit.kind, {}).get("belt"):
                    continue
                if any(r.hold == visit.kind and not r.finished(now)
                       for r in turn.bag_runs):
                    continue
                way = bag_direction(turn.arrived, turn.unloaded, turn.status)
                if self._carts_left(turn, visit.kind, way) <= 0:
                    continue
                key = (f"{visit.kind}@{visit.started:.1f}#{way}"
                       f"{turn.bag_trains.get(visit.kind, 0)}")
                if key in turn.tried:
                    continue
                distance = (math.hypot(turn.x - user_xy[0], turn.z - user_xy[1])
                            if user_xy is not None else 0.0)
                if user_xy is not None and distance > self.range_m:
                    continue
                due.append((distance, turn.callsign, visit.kind, turn, visit, key))
        due.sort(key=lambda item: item[:3])
        for _, _, _, turn, visit, key in due:
            if out >= self.TRAINS:
                break
            kind = bag_direction(turn.arrived, turn.unloaded, turn.status)
            unload = kind == bags.UNLOAD
            left = self._carts_left(turn, visit.kind, kind)
            lx, lz, facing, _ = turn.spot(visit.kind)
            # A departure's bags are on before its holds shut, or not at
            # all: the departure is never kept waiting for them.
            deadline = (turn.closes_at - HOLDS_SHUT_BEFORE_S
                        if not unload and turn.closes_at is not None else None)
            index = turn.bag_trains.get(visit.kind, 0)
            run = self.bag_planner.plan(
                kind, turn.callsign, visit.kind, turn.x, turn.z, turn.heading,
                (lx, lz, facing), now, min(left, bags.MAX_CARTS), deadline, index)
            if run is None:
                turn.tried.add(key)
                turn.bag_left[(visit.kind, kind)] = 0
                log.info("baggage: %s %s -- %d cart(s) left behind, %s", turn.callsign,
                         visit.kind, left, self.bag_planner.why)
                if unload:
                    turn.unloaded = True
                continue
            # Its ground and its way in, kept clear of everybody but its own
            # loader; taken, it is not sent this tick and is asked again.
            owner = f"{turn.callsign}/{visit.kind}/train"
            blocker = zones.first_in_the_way(
                [zone.corners for zone in self._train_zones(run, owner, now, whole=True)],
                self._taken(turn, now, besides=(visit, owner)))
            if blocker is not None:
                self._say_wait(turn, key, owner, blocker)
                continue
            turn.tried.add(key)
            turn.bag_trains[visit.kind] = index + 1
            if run.cycles < run.asked:
                # Cut short by the doors: what did not fit stays behind.
                log.info("baggage: %s %s -- truncated: %d of %d cart(s) before "
                         "the holds shut, %d left behind", turn.callsign, visit.kind,
                         run.cycles, left, left - run.cycles)
                turn.bag_left[(visit.kind, kind)] = 0
            else:
                turn.bag_left[(visit.kind, kind)] = left - run.cycles
            if unload and turn.bag_left[(visit.kind, kind)] <= 0:
                turn.unloaded = True
            log.info("baggage: %s %s -- %s", turn.callsign, visit.kind,
                     self.bag_planner.why)
            turn.bag_runs.append(run)
            out += 1
        for turn in self._turns.values():
            turn.bag_runs = [r for r in turn.bag_runs if not r.finished(now)]
            # A loader with carts still owed stays for the next train: an
            # arrival's until they are off, a departure's only while one
            # more fits before its holds shut.
            way = bag_direction(turn.arrived, turn.unloaded, turn.status)
            closes = (turn.closes_at - HOLDS_SHUT_BEFORE_S
                      if way == bags.LOAD and turn.closes_at is not None else None)
            for visit in turn.visits:
                if (visit.kind not in (BAGGAGE, BAGGAGE_AFT)
                        or visit.state(now) != SERVING
                        or turn.bag_left.get((visit.kind, way), 0) <= 0
                        or turn.status == DEPARTING):
                    continue
                if closes is not None and now + bags.CYCLE_S + 2 * bags.WALK_S > closes:
                    # No other train fits: what is still owed stays behind.
                    log.info("baggage: %s %s -- truncated: %d cart(s) left behind, "
                             "the holds shut in %.0f s", turn.callsign, visit.kind,
                             turn.bag_left[(visit.kind, way)], closes - now)
                    turn.bag_left[(visit.kind, way)] = 0
                    continue
                leaves = visit.leaves_at()
                if leaves is not None and leaves < now + 30.0:
                    visit.leaving = now + 30.0
            for run in turn.bag_runs:
                hold = run.bags_done + 2.0
                for visit in turn.visits:
                    if visit.kind != run.hold or visit.state(now) not in (ARRIVING, SERVING):
                        continue
                    leaves = visit.leaves_at()
                    if leaves is not None and leaves < hold:
                        visit.leaving = hold

    def _carts_left(self, turn: Turnaround, hold: str, way: str) -> int:
        """Carts still owed at this hold this way, counted the first time
        from the flight: its seats, its load and its airline's bags."""
        key = (hold, way)
        if key not in turn.bag_left:
            # One count for the flight, split between its holds.
            rng = random.Random(f"{self._seed}:{turn.callsign}:{way}")
            profile = turn.profile.name if turn.profile is not None else ""
            forward, aft, why = bags.bags_for(turn.type_code, turn.operator, profile,
                                              self.bag_table, rng)
            for each, count in ((BAGGAGE, forward), (BAGGAGE_AFT, aft)):
                turn.bag_left[(each, way)] = bags.carts_for(count)
            log.info("baggage: %s, %s: %s -- %d cart(s) forward, %d aft, of %d bags each",
                     turn.callsign, way, why, turn.bag_left[(BAGGAGE, way)],
                     turn.bag_left[(BAGGAGE_AFT, way)], bags.BAGS_PER_CART)
        return turn.bag_left[key]

    def _train_positions(self, turn: Turnaround, run, layout, now: float) -> list[dict]:
        """The tractor and its carts, each with its whole plan
        (``timeline``), like a bus run; the bags are the loader's own."""
        found = []
        common = {"callsign": turn.callsign, "altitude_ft": self.elevation_ft,
                  "to_latlon": layout.to_latlon, "timeline": True, "pose": {},
                  "state": run.kind}
        if run.out(now):
            vehicles = [(bags.TRACTOR, f"{turn.callsign}/{run.hold}/tractor",
                         run.tractor_path, [])]
            for k, (drive, counts) in enumerate(zip(run.cart_paths, run.cart_counts)):
                vehicles.append((bags.CART, f"{turn.callsign}/{run.hold}/cart{k}", drive,
                                 [("WAGON BACK LINK ORIENTATION", "number", counts)]))
            for kind, key, drive, channels in vehicles:
                x, z, heading, moving = paths.evaluate(drive, now)
                lat, lon = layout.to_latlon(x, z)
                found.append(dict(common, **{
                    "id": key, "kind": kind, "lat": round(lat, 7), "lon": round(lon, 7),
                    "heading": round(heading, 1), "moving": moving, "path": drive,
                    "channels": channels}))
        return found

    # -- the buses -------------------------------------------------------

    def _buses(self, layout, user_xy, now: float) -> None:
        """Plan the buses that are due, nearest the pilot first, and keep
        every aeroplane's stairs until its passengers are off them."""
        if not self.planner.visible or layout is None:
            return
        out = sum(1 for t in self._turns.values() for r in t.runs if r.bus_out(now))
        due = []
        for turn in self._turns.values():
            if not turn.present or not self.planner.fits(turn.length, turn.light):
                continue
            stairs = next((v for v in turn.visits if v.kind == STAIRS
                           and v.state(now) == SERVING), None)
            if stairs is None:
                continue
            if turn.status in (BOARDING, DEPARTING):
                kind = people.BOARD
            elif turn.arrived:
                kind = people.DEBOARD
            else:
                continue
            if kind in turn.tried:
                continue
            distance = (math.hypot(turn.x - user_xy[0], turn.z - user_xy[1])
                        if user_xy is not None else 0.0)
            if user_xy is not None and distance > self.range_m:
                continue
            due.append((distance, turn.callsign, turn, kind))
        due.sort(key=lambda item: item[:2])
        for _, _, turn, kind in due:
            if out >= self.BUSES:
                break
            turn.tried.add(kind)
            footprints = [(t.x, t.z, t.heading, t.length)
                          for t in self._turns.values()
                          if t.present and t is not turn]
            if self.pilot_footprint is not None:
                footprints.append(self.pilot_footprint)
            sx, sz, facing, _ = turn.spot(STAIRS)
            run = self.planner.plan(kind, turn.callsign, turn.x, turn.z,
                                    turn.heading, turn.length, turn.sill,
                                    (sx, sz, facing), layout, now,
                                    footprints=footprints)
            log.info("passengers: %s %s -- %s", turn.callsign,
                     "getting off" if kind == people.DEBOARD else "boarding",
                     self.planner.why if run is not None
                     else f"no bus: {self.planner.why}")
            if run is not None:
                turn.runs.append(run)
                out += 1
        for turn in self._turns.values():
            turn.runs = [r for r in turn.runs if not r.finished(now)]
            for run in turn.runs:
                hold = run.people_done + 3.0
                for visit in turn.visits:
                    if visit.kind != STAIRS or visit.state(now) not in (ARRIVING, SERVING):
                        continue
                    leaves = visit.leaves_at()
                    if leaves is not None and leaves < hold:
                        visit.leaving = hold

    def _run_positions(self, turn: Turnaround, run, layout, now: float) -> list[dict]:
        """The bus and whoever of its passengers is out, for the simulator.
        Each carries its whole plan (``timeline``): the vehicle layer hands
        it to the motion engine once and leaves it to play."""
        found = []
        common = {"callsign": turn.callsign, "altitude_ft": self.elevation_ft,
                  "to_latlon": layout.to_latlon, "timeline": True, "pose": {}}
        if run.bus_out(now):
            x, z, heading, moving = paths.evaluate(run.bus_path, now)
            lat, lon = layout.to_latlon(x, z)
            found.append(dict(common, **{
                "id": f"{turn.callsign}/{people.BUS}/{run.kind}",
                "kind": people.BUS, "lat": round(lat, 7), "lon": round(lon, 7),
                "heading": round(heading, 1), "moving": moving, "state": run.kind,
                "path": run.bus_path,
                "channels": [("PLANE TOUCHDOWN PITCH DEGREES", "number", run.doors),
                             ("WAGON BACK LINK ORIENTATION", "number", run.seats)],
            }))
        for walker in run.walkers:
            if not walker.appears <= now < walker.gone:
                continue
            x, z, heading, moving = paths.evaluate(walker.path, now)
            lat, lon = layout.to_latlon(x, z)
            found.append(dict(common, **{
                "id": walker.key, "kind": people.PASSENGER,
                "lat": round(lat, 7), "lon": round(lon, 7),
                "heading": round(heading, 1), "moving": moving, "state": run.kind,
                "person": True, "path": walker.path, "walk": walker.walk,
                "lift": walker.lift,
            }))
        return found

    # -- what waits for what ---------------------------------------------

    def _resolve(self, turn: Turnaround, now: float) -> None:
        """Give a time to every step whose moment has come."""
        for visit in turn.visits:
            if visit.gone or not math.isinf(visit.arrive_at):
                continue
            when = self._deboarded(turn, visit, now)
            if when is not None:
                visit.arrive_at = when + visit.delay

    def _deboarded(self, turn: Turnaround, visit: Visit, now: float) -> float | None:
        """When the last passenger of this visit's round got off, once they
        have: the forward stairs at the door plus the profile's deboarding
        time, or the bus's last walker if a bus came for them. None until
        then."""
        if not visit.deplaning:
            return visit.begun
        stairs = next((v for v in turn.visits if v.kind == STAIRS
                       and v.begun == visit.begun), None)
        if stairs is None or (stairs.gone and stairs.started is None):
            return visit.begun
        if stairs.started is None:
            return None
        profile = turn.profile or profile_for("", turn.light)
        done = stairs.started + DRIVE_S + profile.deboard_s / self.time_scale
        for run in turn.runs:
            if run.kind == people.DEBOARD:
                done = max(done, run.people_done)
        return done if now >= done else None

    def _taken(self, turn: Turnaround, now: float, besides=()) -> list[zones.Zone]:
        """The ground already taken around this aeroplane and its
        neighbours: every vehicle out, where it parks, and the strip it is
        driving along if it is driving; every baggage train out."""
        taken: list[zones.Zone] = []
        for other in self._turns.values():
            if math.hypot(other.x - turn.x, other.z - turn.z) > NEIGHBOURS_M:
                continue
            for visit in other.visits:
                if any(visit is one for one in besides):
                    continue
                state = visit.state(now)
                if state not in (ARRIVING, SERVING, LEAVING):
                    continue
                zone = other.parked_zone(visit)
                taken.append(zone)
                if state != SERVING:
                    start = None
                    if state == LEAVING:
                        start = other.approach(visit)
                    taken.extend(zones.Zone(zone.owner, f"{zone.name} way in", strip)
                                 for strip in other.way_in(visit, start))
            for run in other.bag_runs:
                owner = f"{other.callsign}/{run.hold}/train"
                if owner in [one for one in besides if isinstance(one, str)] \
                        or not run.out(now):
                    continue
                taken.extend(self._train_zones(run, owner, now))
        return taken

    def _train_zones(self, run, owner: str, now: float,
                     whole: bool = False) -> list[zones.Zone]:
        """A baggage train's ground: where it stands, and the strips it
        drives along while it is driving (all of them, ``whole``, for one
        about to set off)."""
        name = zones.NAMES.get(run.hold, run.hold) + " train"
        x, z, heading, _ = paths.evaluate(run.tractor_path, run.arrives)
        size = FOOTPRINTS.get("bag_train") or DEFAULT_FOOTPRINT
        # As long as its carts: a tractor and up to bags.MAX_CARTS of them.
        size = zones.Footprint(size.ahead_m,
                               max(size.behind_m,
                                   bags.SPACING_M * len(run.cart_paths) + 3.5),
                               size.half_width_m)
        found = [zones.Zone(owner, name, zones.footprint(x, z, heading, size))]
        driving = whole or now < run.arrives or now >= run.leaves
        if driving:
            spans = [(run.tractor_path[0].t0, run.arrives)]
            if whole or now >= run.leaves:
                spans.append((run.leaves, paths.ends_at(run.tractor_path)))
            if not whole:
                spans = [span for span in spans if span[0] <= now < span[1]
                         or now < span[0]]
            for t0, t1 in spans:
                steps = max(2, int((t1 - t0) / 2.0))
                points = [paths.evaluate(run.tractor_path, t0 + (t1 - t0) * k / steps)[:2]
                          for k in range(steps + 1)]
                found.extend(zones.Zone(owner, f"{name} way in",
                                        zones.strip(a, b, size.half_width_m + MARGIN_M))
                             for a, b in zip(points, points[1:]))
        return found

    def _in_the_way(self, turn: Turnaround, visit: Visit,
                    now: float) -> zones.Zone | None:
        """What keeps this vehicle from setting off now: something on the
        ground it parks on, or -- on every way in it could take -- on the
        strip it would drive along. A free way in found on another bearing
        is kept on the visit. None: it may go."""
        taken = self._taken(turn, now, besides=(visit,))
        mine = turn.parked_zone(visit, MARGIN_M)
        blocker = zones.first_in_the_way([mine.corners], taken)
        if blocker is not None:
            return blocker
        if visit.wait_at is not None:
            # From where it waited: that is its way in.
            return zones.first_in_the_way(turn.way_in(visit), taken)
        _, _, _, straight = turn.spot(visit.kind)
        body = turn.fuselage()
        first = None
        for turn_by in BEARINGS:
            visit.bearing = None if turn_by == 0 else (straight + turn_by) % 360.0
            way = turn.way_in(visit, turn.approach(visit))
            if any(zones.overlap(strip, body) for strip in way):
                continue
            blocker = zones.first_in_the_way(way, taken)
            if blocker is None:
                return None
            first = first or blocker
        visit.bearing = None
        return first

    def _clear_bearing(self, turn: Turnaround, visit: Visit,
                       now: float) -> float | None:
        """The first bearing whose way in crosses neither the aeroplane
        nor anything taken, leaving out what stands on its own spot -- that
        is what it waits for. None: the straight one, or nothing better."""
        taken = self._taken(turn, now, besides=(visit,))
        mine = turn.parked_zone(visit, MARGIN_M).corners
        others = [zone for zone in taken if not zones.overlap(mine, zone.corners)]
        _, _, _, straight = turn.spot(visit.kind)
        body = turn.fuselage()
        for turn_by in BEARINGS:
            visit.bearing = None if turn_by == 0 else (straight + turn_by) % 360.0
            way = turn.way_in(visit, turn.approach(visit))
            if any(zones.overlap(strip, body) for strip in way):
                continue
            if zones.first_in_the_way(way, others) is None:
                bearing = visit.bearing
                visit.bearing = None
                return bearing
        visit.bearing = None
        return None

    def _say_wait(self, turn: Turnaround, key: str, who: str,
                  blocker: zones.Zone) -> bool:
        """Log a wait once, and again only if it is now for somebody else."""
        said = f"{blocker.owner}|{blocker.name}"
        waiting = turn.waiting.get(key)
        turn.waiting[key] = said
        if waiting == said:
            return False
        log.info("handling: %s waits for %s (zone %s)", who, blocker.owner,
                 blocker.name)
        return True

    def _admit(self, user_xy, now: float) -> None:
        """Set off the vehicles that are due, nearest the pilot first, as far
        as the budget allows -- and as far as the ground is free: one whose
        spot or way in is taken stands at its waiting point until it is."""
        out = self.budgeted_out(now)
        due: list[tuple[float, float, Turnaround, Visit]] = []
        for turn in self._turns.values():
            if not turn.present:
                continue
            distance = (math.hypot(turn.x - user_xy[0], turn.z - user_xy[1])
                        if user_xy is not None else 0.0)
            for visit in turn.visits:
                state = visit.state(now)
                if state == HOLDING:
                    due.append((_PRIORITY.get(visit.kind, 1.0), distance, turn, visit))
                    continue
                if state != WAITING or visit.arrive_at > now:
                    continue
                if visit.expired(now):
                    visit.gone = True
                    continue
                if user_xy is not None and distance > self.range_m:
                    continue
                # The tug is what a departure is waiting for, and the
                # stairs are what a parked aeroplane is seen with;
                # everything else is scenery and queues behind them.
                due.append((_PRIORITY.get(visit.kind, 1.0),
                            distance, turn, visit))
        due.sort(key=lambda item: (item[0], item[1], item[2].callsign,
                                   item[3].kind))
        for _, _, turn, visit in due:
            holding = visit.state(now) == HOLDING
            # The tug is owed whatever the budget says: a departure that
            # pushes back with nothing on its nose is the one visit a pilot
            # on the next stand is certain to notice, and departures are few.
            # One already at its waiting point has its place in the budget.
            if not holding and out >= self.limit and visit.kind not in _UNBUDGETED:
                continue
            who = f"{turn.callsign}/{visit.kind}"
            blocker = self._in_the_way(turn, visit, now)
            if blocker is not None:
                if not holding:
                    visit.holding = now
                    # Wait at the start of a way in that is clear of all
                    # but whoever is on its spot, so the way is free when
                    # the spot is: waiting on a blocked one, a caterer kept
                    # out by the loader was then kept out by the power unit.
                    visit.bearing = self._clear_bearing(turn, visit, now)
                    visit.wait_at = turn.wait_spot(visit)
                    visit.in_place = False
                    if visit.kind not in _UNBUDGETED:
                        out += 1
                self._say_wait(turn, who, who, blocker)
                continue
            if holding:
                log.info("handling: %s goes in after %.0f s waiting", who,
                         now - visit.holding)
                turn.waiting.pop(who, None)
                visit.started = now
                continue
            # One that was already there is drawn where it stands, on the
            # tick that found it; any later and it would appear from nowhere
            # in front of the pilot, so from then on it drives in like
            # everybody else.
            visit.started = now - DRIVE_S if visit.in_place else now
            visit.in_place = False
            if visit.kind not in _UNBUDGETED:
                out += 1
        for _, _, _, visit in due:
            visit.in_place = False


__all__ = ["Handling", "Standing", "Turnaround", "Visit", "SPOTS",
           "PUSHBACK", "BAGGAGE", "BAGGAGE_AFT", "CATERING", "GPU", "STAIRS",
           "STAIRS_AFT", "FUEL", "BASE_KIND", "pose", "door_sill",
           "PARKED", "ARRIVED", "DEPARTING", "BOARDING",
           "WAITING", "HOLDING", "ARRIVING", "SERVING", "LEAVING", "GONE",
           "APPROACH_M", "DRIVE_S", "Profile", "Step", "profile_for",
           "load_timelines", "START", "DEBOARDED"]
