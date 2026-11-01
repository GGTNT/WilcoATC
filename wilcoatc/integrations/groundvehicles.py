"""Putting the ground handling into the simulator.

:mod:`wilcoatc.atc.handling` says where every baggage loader, caterer, fuel
truck, power unit, set of stairs and tug on the apron is, every tick. This
creates each one in the simulator as a simulated object, moves it while it is
driving, and takes it away when its visit is over -- the same shape as
:mod:`wilcoatc.integrations.aitraffic` does for the aeroplanes, through the
same shared message pump, with the same three rules: it only ever touches
what it created, it fails closed, and it has to be told what it made.

The one real difference is what to ask for. An aeroplane has a type and a
model to match it; a vehicle has only a *title*, and the two simulators do
not agree on what the titles are. The 2020 ones are documented and are
written down here (``ASO_Catering_Truck_01`` and so on). The 2024 ones are
not, and its packages are streamed in a form that cannot be read off the
disk -- but it has a call, ``SimConnect_EnumerateSimObjectsAndLiveries``,
that lists every ground object it has. So when a SimConnect library new
enough to make that call is on the machine (the one the facility reader
already looks for), a running simulator is asked, on a worker, what ground
vehicles it has, and the answers are matched by name: anything with
"catering" in it is a catering truck. What the simulator lists comes first;
the documented 2020 titles follow; a title refused by name is not asked for
again; and a kind of vehicle with no title left is simply not drawn, which
leaves the rest of the apron exactly as it was.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import json
import logging
import math
import threading
import time
import zlib
from pathlib import Path

from .aitraffic import (SIMCONNECT_DATATYPE_FLOAT64,
                        SIMCONNECT_DATA_SET_FLAG_DEFAULT, _AssignedObjectId,
                        _Exception, _Received, _assigned_message_id,
                        _exception_message_id, init_position_type,
                        install_dispatch, remove_dispatch,
                        subscribe_frames, unsubscribe_frames, frame_timing)
from .doors import TOGGLE_EXIT_EVENT  # noqa: F401  (where it used to live)
from ..atc import paths
from ..atc import baggage as bags
from ..atc import passengers as people
from .motion import Actor, Channel, MotionEngine, keyframed, walking_while
from .facilities import find_simconnect
from ..atc import handling

log = logging.getLogger(__name__)

#: The ground vehicles to ask for when the simulator cannot be asked what it
#: has, best first, by what the turnaround calls them. The ``ASO_`` names are
#: the documented 2020 ones, spelled as the SDK spells them (the loader really
#: is mixed case); the rest were read off a running 2024 (it lists 35,000
#: ground objects, and these are the stock ones among them). A title the
#: simulator does not have is refused by name once and never asked for again.
TITLES: dict[str, tuple[str, ...]] = {
    handling.GPU: ("ASO_Ground_Power_Unit", "Car Ground Power Unit"),
    handling.STAIRS: ("ASO_Boarding_Stairs", "ASO_Boarding_Stairs_Yellow",
                      "ASO_Boarding_Stairs_Red", "Microsoft_Truck_EUR_Boarding"),
    handling.BAGGAGE: ("Aso_Baggage_Loader_01", "Microsoft_Loader_Ramp_02",
                       "ASO_BaggageTruck01", "ASO_Baggage_Cart01"),
    handling.CATERING: ("ASO_Catering_Truck_01",),
    handling.FUEL: ("ASO_FuelTruck01_White", "ASO_FuelTruck02_White",
                    "ASO_FuelTruck01_Black", "ASO_FuelTruck02_Black",
                    "Microsoft_Truck_Fuel_Long_02_White", "Fuel Truck Long 01"),
    handling.PUSHBACK: ("ASO_Pushback_Blue", "ASO_Pushback_White",
                        "Microsoft_Pushback_03", "ASO_Tug01_White"),
}

#: What in a listed title says which kind of vehicle it is, best first. Lower
#: case; a title has to contain one of them, and the earlier the match the
#: better the title. "Tug" is a pushback and "cart" is not a loader, so a
#: baggage cart is matched on "loader" or "truck" only.
KEYWORDS: dict[str, tuple[str, ...]] = {
    handling.GPU: ("power unit", "power_unit", "groundpower", "gpu"),
    handling.STAIRS: ("boarding_stairs", "boarding stairs", "stairs",
                      "staircase", "boarding"),
    handling.BAGGAGE: ("baggage_loader", "baggage loader", "baggageloader",
                       "loader_ramp", "baggagetruck", "baggage truck",
                       "loader"),
    handling.CATERING: ("catering_truck", "catering truck", "catering"),
    handling.FUEL: ("fueltruck", "fuel truck", "fuel_truck", "truck_fuel",
                    "fuel"),
    handling.PUSHBACK: ("pushback", "tug"),
}

#: Who made a listed vehicle, and so how far down the list it goes. The
#: simulator's own come first: they are what its ground services use and
#: they are always there. GSX's (``FSDT_``) are real vehicles too, in every
#: handling agent's livery -- fifteen thousand of them at one airport --
#: and are taken when nothing stock matches. Everything else is last.
_STOCK = ("aso_", "asobo", "microsoft_")
_GSX = ("fsdt_",)

# GSX Pro's own vehicles, by kind: the base title of each model, without a
# handling agent's livery. Read off a running 2024 with GSX Pro installed
# (EnumerateSimObjectsAndLiveries, 14 October): 34,578 GSX objects, almost
# all of them liveries -- FSDT_Staircase_CDS_2438 alone comes in 697. Only
# titles are used: nothing of GSX's is read or copied, and on a machine
# without GSX these are simply not listed and never asked for.
GSX_TITLES: dict[str, tuple[str, ...]] = {
    # Which one serves a door is chosen by its sill (servicelayout.
    # choose_stairs); these are the fallbacks, best first. FW2458PE is the
    # title the simulator lists, not FW2458.
    handling.STAIRS: ("FSDT_Staircase_CDS_2438", "FSDT_Staircase_CDS_2445",
                      "FSDT_Staircase_FW2458PE", "FSDT_Staircase_TLD_ABS-1740"),
    # World of Jetways' belt loader, in the livery GSX pairs with the
    # handler (see OPERATORS); its belt is in service_layout.json.
    handling.BAGGAGE: ("FSDT_Tug_660_DEFAULT_FULL_YELLOW",),
    handling.CATERING: ("FSDT_Catering_01",),
    handling.FUEL: ("FSDT_FuelTruck_7296", "FSDT_FuelTruck_Mercedes_Econic"),
    handling.GPU: ("FSDT_GPU_TLD_406", "FSDT_GPU_Hobart_4400"),
    handling.PUSHBACK: ("FSDT_TPX_100_E", "FSDT_TPX_200"),
}
#: People who work on a vehicle, drawn as an object of their own on top of
#: it: GSX's belt-loader handler. FSDT_Baggage_Loader_Man_02 is not a
#: loader -- its model (read from GSX Pro's package, 15 October) is the
#: handler, FSDT_Driver_02, standing a metre up and 3.7 m behind its
#: origin, and eleven suitcases, and nothing else. Asked for as the
#: loader, it was a man standing in the air beside the hold (Innsbruck
#: live test). His loader is World of Jetways' FSDT_Tug_660, whose sim.cfg
#: attaches him (``couatl.attach_object``, no offset): its belt pivots
#: 1.03 m up and 2.23 m behind its origin, so 3.7 m behind and a metre up
#: is the step at the foot of the belt, between the carts and the belt
#: (read 17 October; on the stock loader he stood beside it, Bilbao).
#: He is added only once the simulator has named the loader, it has
#: stopped at the hold and its belt has read back raised; if the loader
#: never comes, neither does he. The neutral full-colour variant, because
#: the bare title is created and stays invisible (seen at Madeira).
OPERATORS: dict[str, tuple[str, ...]] = {
    handling.BAGGAGE: ("FSDT_Baggage_Loader_Man_02_DEFAULT_FULL_YELLOW",),
}
#: The vehicles each operator was made to stand on. GSX's handler on any
#: other model would be standing on nothing again.
OPERATOR_VEHICLES: dict[str, tuple[str, ...]] = {
    handling.BAGGAGE: ("FSDT_Tug_660_DEFAULT_FULL_YELLOW",),
}
#: Where the operator stands relative to its vehicle's reference: right,
#: forward (metres) and turned (degrees). Zero until placed by eye.
OPERATOR_OFFSET: dict[str, tuple[float, float, float]] = {}
#: An operator's id is its vehicle's with this after it.
OPERATOR_SUFFIX = "/operator"
#: The handler's work is the loader's own frame, the one its belt's bags
#: play too (wilcoatc.atc.baggage, ``frames`` with each loader's position):
#: on ``WAGON BACK LINK ORIENTATION``, his model's clip variable.
OPERATOR_VAR = ("WAGON BACK LINK ORIENTATION", "number")
#: The package whose presence says GSX Pro is installed.
GSX_PACKAGE = "fsdreamteam-gsx-pro"

#: The bus whose doors and seats :mod:`wilcoatc.atc.passengers` was laid out
#: on. Another bus has its doors elsewhere, so none is taken in its place.
BUS_TITLES: tuple[str, ...] = ("FSDT_Cobus_3000",)
#: The baggage train (wilcoatc.atc.baggage): GSX's where listed, the
#: simulator's own otherwise. It only comes to GSX's own loader, whose belt
#: and handler carry the bags; there are no suitcases of this program's.
TRAIN_TITLES: dict[str, tuple[str, ...]] = {
    bags.TRACTOR: ("FSDT_Tug_Endurance",),
    bags.CART: ("FSDT_BaggageWagon_Open",),
}
STOCK_TRAIN_TITLES: dict[str, tuple[str, ...]] = {
    bags.TRACTOR: ("ASO_BaggageTruck01",),
    bags.CART: ("ASO_Baggage_Cart01", "ASO_Baggage_Cart02"),
}
#: How far one stride of a passenger's walk carries it, metres: an adult's
#: and a child's. With the stride's keyframes this is the pace the clip was
#: made at, and the clip is played faster or slower to match the pace it
#: is walked at, so the feet do not slide.
STRIDE_M = 1.5
CHILD_STRIDE_M = 1.1
_CHILDREN = ("cboy", "cgirl", "kid_")
#: The variables the bus's doors (the left-hand ones, towards the stairs)
#: and its seated passengers are drawn from, read off its model.
BUS_DOORS_VAR = (b"PLANE TOUCHDOWN PITCH DEGREES", b"number")
BUS_SEATS_VAR = (b"WAGON BACK LINK ORIENTATION", b"number")
WALK_VAR = (b"VELOCITY BODY Y", b"meters per second")


def load_walks(path: Path | None = None) -> dict[str, tuple[int, int]]:
    """title -> (first keyframe of a stride, keyframes per stride), from
    ``walks.json`` beside this module (``scripts/scan_walks.py`` makes it).
    Empty when it is missing: nobody walks."""
    path = path or Path(__file__).with_name("walks.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {title: (int(w["start"]), int(w["stride"]))
                for title, w in data.get("walks", {}).items()}
    except (OSError, ValueError, KeyError, TypeError):
        log.info("no passenger walks at %s; nobody walks", path)
        return {}


def natural_pace(title: str, stride: int) -> float:
    """The pace a stride of this many keyframes was made at, m/s."""
    metres = CHILD_STRIDE_M if any(c in title.lower() for c in _CHILDREN) else STRIDE_M
    return metres / (max(1, stride) / 30.0)


def gsx_installed(folders=None) -> bool:
    """Whether GSX Pro is in one of the simulator's Community folders."""
    if folders is None:
        try:
            from .fsltl import packages_folders
            folders = packages_folders()
        except Exception:
            return False
    for packages in folders:
        try:
            for community in Path(packages).glob("Community*"):
                if (community / GSX_PACKAGE).is_dir():
                    return True
        except OSError:
            continue
    return False
#: And the ones that are not vehicles at all: an aircraft add-on's own
#: stairs, carts and fuel truck, built to dock with that aeroplane and no
#: other, which the listing cannot tell from the real thing.
_AIRCRAFT_PARTS = ("pmdg", "ini_", "inibuilds", "fenix", "fnx", "fbw",
                   "flybywire", "a32nx", "headwind", "toliss")

# What is written to a vehicle while it drives. No altitude, ever: created
# on the ground the simulator puts a vehicle on the surface itself and keeps
# it there, and the published elevation it would otherwise be written to is
# not the surface (see aitraffic._MOVE_GROUND).
_MOVE = (
    (b"PLANE LATITUDE", b"degrees"),
    (b"PLANE LONGITUDE", b"degrees"),
    (b"PLANE HEADING DEGREES TRUE", b"degrees"),
)
_DEF_MOVE = 4721

# How each kind of vehicle is posed once it is at the aeroplane: the
# variables its model animates on, in a fixed order, which is the order the
# values are packed in. Measured on a running 2024: every one of these is
# accepted on a created vehicle and reads back as written. The values come
# with each position from :func:`wilcoatc.atc.handling.pose`.
POSE_VARS: dict[str, tuple[tuple[bytes, bytes], ...]] = {
    handling.STAIRS: ((b"BOARDINGRAMP ELEVATION TARGET", b"meters"),
                      (b"BOARDINGRAMP ELEVATION CURRENT", b"meters"),
                      (b"BOARDINGRAMP ORIENTATION TARGET", b"percent"),
                      (b"BOARDINGRAMP ORIENTATION CURRENT", b"percent")),
    handling.CATERING: ((b"CATERINGTRUCK ELEVATION TARGET", b"meters"),
                        (b"CATERINGTRUCK ELEVATION CURRENT", b"meters"),
                        (b"CATERINGTRUCK OPENING TARGET", b"percent"),
                        (b"CATERINGTRUCK OPENING CURRENT", b"percent")),
    handling.BAGGAGE: ((b"BAGGAGELOADER ANGLE TARGET", b"degrees"),
                       (b"BAGGAGELOADER ANGLE CURRENT", b"degrees")),
    handling.FUEL: ((b"FUELTRUCK HOSE DEPLOYED", b"percent"),),
    handling.GPU: ((b"GROUNDPOWERUNIT HOSE DEPLOYED", b"percent"),),
    handling.PUSHBACK: ((b"PUSHBACK ATTACHED", b"bool"),),
}
_DEF_POSE: dict[str, int] = {
    kind: 4730 + index for index, kind in enumerate(POSE_VARS)}
_POSE_READ_BASE = 660000
#: How long a pose's read-back is waited for before a handler goes on
#: without it (a simulator that answers nothing still gets its people).
POSE_ANSWER_S = 5.0

_RECV_SIMOBJECT_DATA = 8
# Driving, measured at Madeira on one catering truck (see atc/paths.py): a
# vehicle the simulator is left to itself creeps -- 4.1 m in fifteen
# seconds, standing still -- so each is frozen once it exists, and a
# driving one has its position written every visual frame, not every
# engine tick (which runs from a quarter of a second to ten).
# Not the altitude: the simulator keeps a vehicle on the surface itself,
# and a frozen height would leave it in the air on a sloping apron.
_FREEZE_EVENTS = (b"FREEZE_LATITUDE_LONGITUDE_SET", b"FREEZE_ATTITUDE_SET")
# Where the simulator really has a bus, read twice after it is made: at
# Innsbruck every bus was created and named and not one could be seen.
_DEF_PROBE = 4749
_PROBE_REQUEST_BASE = 650000
_PERIOD_ONCE = 1
#: When a bus is looked for, seconds after it was named.
PROBE_AFTER_S = (5.0, 30.0)
_EV_FREEZE_BASE = 4740
_EV_FRAME = 4743
_RECV_EVENT_FRAME = 7


class _ObjectData(_Received):
    """SIMCONNECT_RECV_SIMOBJECT_DATA up to its data."""

    _pack_ = 1
    _fields_ = [
        ("dwRequestID", ctypes.c_uint32),
        ("dwObjectID", ctypes.c_uint32),
        ("dwDefineID", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("dwentrynumber", ctypes.c_uint32),
        ("dwoutof", ctypes.c_uint32),
        ("dwDefineCount", ctypes.c_uint32),
    ]


class _Frame(_Received):
    """SIMCONNECT_RECV_EVENT_FRAME: once a visual frame."""

    _pack_ = 1
    _fields_ = [
        ("uGroupID", ctypes.c_uint32),
        ("uEventID", ctypes.c_uint32),
        ("dwData", ctypes.c_uint32),
        ("fFrameRate", ctypes.c_float),
        ("fSimSpeed", ctypes.c_float),
    ]

# GroupID is a priority when this flag is set; the highest.
_EVENT_FLAG_GROUPID_IS_PRIORITY = 0x10
_PRIORITY_HIGHEST = 1
_REQUEST_BASE = 4800

# The enumeration, from the 2024 SDK header: the message that answers it and
# the object type that means a ground vehicle. Counted off the header rather
# than guessed; the wrapper's own enumeration predates both.
_RECV_ENUMERATE_SIMOBJECT_AND_LIVERY_LIST = 38
_SIMOBJECT_TYPE_GROUND = 5
_REQ_ENUMERATE = 4799


class _Where(ctypes.Structure):
    _fields_ = [
        ("latitude", ctypes.c_double),
        ("longitude", ctypes.c_double),
        ("heading", ctypes.c_double),
    ]


class _ListHeader(_Received):
    """SIMCONNECT_RECV_LIST_TEMPLATE: the count in front of a list reply."""

    _fields_ = [
        ("dwRequestID", ctypes.c_uint32),
        ("dwArraySize", ctypes.c_uint32),
        ("dwEntryNumber", ctypes.c_uint32),
        ("dwOutOf", ctypes.c_uint32),
    ]


class _Listed(ctypes.Structure):
    """SIMCONNECT_ENUMERATE_SIMOBJECT_LIVERY: one object the simulator has."""

    _fields_ = [
        ("title", ctypes.c_char * 256),
        ("livery", ctypes.c_char * 256),
    ]


def read_pairs(data: bytes) -> tuple[list[tuple[str, str]], bool]:
    """The (title, livery) entries in one enumeration message, and whether
    it is the last.

    ``data`` is the whole message. The header says how many entries follow
    and which transmission of how many this is; a list too long for one
    message arrives in several.
    """
    header = ctypes.sizeof(_ListHeader)
    if len(data) < header:
        return [], True
    head = _ListHeader.from_buffer_copy(data[:header])
    pairs: list[tuple[str, str]] = []
    size = ctypes.sizeof(_Listed)
    for index in range(int(head.dwArraySize)):
        start = header + index * size
        if start + size > len(data):
            break
        one = _Listed.from_buffer_copy(data[start:start + size])
        title = one.title.split(b"\x00", 1)[0].decode("utf-8", "ignore").strip()
        livery = one.livery.split(b"\x00", 1)[0].decode("utf-8", "ignore").strip()
        if title:
            pairs.append((title, livery))
    last = int(head.dwEntryNumber) + 1 >= int(head.dwOutOf)
    return pairs, last


def read_listing(data: bytes) -> tuple[list[str], bool]:
    """The titles in one enumeration message, and whether it is the last."""
    pairs, last = read_pairs(data)
    return [title for title, _ in pairs], last


def _rank(title: str, kind: str) -> tuple[int, int, int] | None:
    """Where a listed title goes for a kind: lower is better, None is never.

    The simulator's own first, GSX's second, anybody else's third; within
    that the earliest keyword that matches, and then the shortest title,
    because ``FSDT_Pushback_03`` is the vehicle and
    ``FSDT_Pushback_03_ASOBO_norwich`` is one of its thousand liveries.
    """
    lower = title.lower()
    if any(lower.startswith(vendor) for vendor in _AIRCRAFT_PARTS):
        return None
    words = KEYWORDS[kind]
    found = [index for index, word in enumerate(words) if word in lower]
    if not found:
        return None
    if kind == handling.BAGGAGE and "tug" in lower:
        return None          # a baggage tug is a tug
    if kind == handling.PUSHBACK and "tug_660" in lower:
        return None          # GSX's Tug 660 is a belt loader
    if "_man" in lower or "_woman" in lower or "operator" in lower:
        return None          # a person, not a vehicle (see OPERATORS)
    # The simulator's own are prefixed, or -- the 2024 ones -- plain words
    # with spaces ("Car Ground Power Unit"); an add-on's carry its name.
    if lower.startswith(_STOCK) or "_" not in title:
        tier = 0
    elif lower.startswith(_GSX):
        tier = 1
    else:
        tier = 2
    return tier, found[0], len(title)


def choose_titles(listed, documented: dict[str, tuple[str, ...]] = TITLES,
                  prefer_gsx: bool = False) -> dict[str, tuple[str, ...]]:
    """What to ask for, by kind: what the simulator listed, best first, then
    the book. ``prefer_gsx`` puts GSX Pro's own model of each kind first,
    where the simulator lists it.

    A listed title that matches no kind is ignored; one that is an aircraft
    add-on's own part is left out; a kind nothing was listed for keeps the
    documented titles alone.
    """
    chosen: dict[str, list[tuple[tuple[int, int, int], str]]] = {
        kind: [] for kind in documented}
    for title in dict.fromkeys(listed or ()):
        for kind in documented:
            rank = _rank(title, kind)
            if rank is not None:
                chosen[kind].append((rank, title))
                break
    listed_set = set(listed or ())
    found = {}
    for kind in documented:
        ranked = [title for _, title in sorted(chosen[kind])]
        if prefer_gsx:
            # GSX's own model of each kind, where the simulator has it,
            # before everything else.
            ranked = [t for t in GSX_TITLES.get(kind, ()) if t in listed_set] + ranked
        found[kind] = tuple(dict.fromkeys(ranked + list(documented[kind])))
    return found


def enumerate_ground_vehicles(dll_path: str = "", timeout_s: float = 6.0
                              ) -> list[str] | None:
    """Every ground vehicle a running simulator has, or None if it cannot say.

    Its own short session on a library new enough to have the call, like
    the facility readers; closed before it returns. None for no library, no
    simulator, or no answer -- all of which mean "use the documented titles".
    """
    pairs = enumerate_simobjects(_SIMOBJECT_TYPE_GROUND, dll_path, timeout_s)
    return None if pairs is None else [title for title, _ in pairs]


def enumerate_simobjects(kind: int, dll_path: str = "", timeout_s: float = 6.0,
                         name: bytes = b"WilcoATC vehicles"
                         ) -> list[tuple[str, str]] | None:
    """Every (title, livery) of one kind of object a running simulator has,
    or None if it cannot say. ``kind`` is SimConnect's object type: 2 the
    aircraft, 5 the ground vehicles."""
    library = find_simconnect(dll_path)
    if library is None:
        return None
    try:
        dll = ctypes.WinDLL(str(library))
        if not hasattr(dll, "SimConnect_EnumerateSimObjectsAndLiveries"):
            return None
        dll.SimConnect_Open.argtypes = [
            ctypes.POINTER(w.HANDLE), ctypes.c_char_p, w.HWND, w.DWORD,
            w.HANDLE, w.DWORD]
        dll.SimConnect_EnumerateSimObjectsAndLiveries.argtypes = [
            w.HANDLE, w.DWORD, w.DWORD]
        dll.SimConnect_GetNextDispatch.argtypes = [
            w.HANDLE, ctypes.POINTER(ctypes.POINTER(_Received)),
            ctypes.POINTER(w.DWORD)]
        dll.SimConnect_Close.argtypes = [w.HANDLE]
        handle = w.HANDLE()
        if dll.SimConnect_Open(ctypes.byref(handle), name,
                               None, 0, None, 0) != 0:
            return None
    except Exception:
        log.info("could not open a session to list the simulator's objects",
                 exc_info=True)
        return None
    found: list[tuple[str, str]] = []
    answered = False
    try:
        if dll.SimConnect_EnumerateSimObjectsAndLiveries(
                handle, _REQ_ENUMERATE, int(kind)) != 0:
            return None
        data = ctypes.POINTER(_Received)()
        size = w.DWORD()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if dll.SimConnect_GetNextDispatch(
                    handle, ctypes.byref(data), ctypes.byref(size)) != 0 \
                    or not data:
                time.sleep(0.02)
                continue
            if data.contents.dwID != _RECV_ENUMERATE_SIMOBJECT_AND_LIVERY_LIST:
                continue
            raw = ctypes.string_at(data, int(size.value))
            pairs, last = read_pairs(raw)
            found.extend(pairs)
            answered = True
            if last:
                break
    except Exception:
        log.info("listing the simulator's objects raised", exc_info=True)
        return None
    finally:
        try:
            dll.SimConnect_Close(handle)
        except Exception:
            pass
    return found if answered else None


class SimVehicles:
    """The ground handling, in the simulator.

    Give it the positions :meth:`wilcoatc.atc.handling.Handling.positions`
    produces and it creates what is new, moves what is driving, and removes
    what has gone.
    """

    # How long a request may go unanswered before it counts against the
    # vehicle, and how many unanswered requests end a vehicle's chances.
    ANSWER_WITHIN_S = 10.0
    UNANSWERED_PER_VEHICLE = 2
    # How many may be asked for, with none appearing, before this simulator
    # is taken to be refusing ground vehicles altogether.
    REFUSALS_BEFORE_GIVING_UP = 6
    # How long the worker listing the simulator's vehicles is waited for
    # before the documented titles are used without it.
    DISCOVERY_S = 8.0

    def __init__(self, enabled: bool = True, limit: int = 8,
                 dll_path: str = "", discover: bool = True,
                 prefer_gsx: bool = False, people: bool = False,
                 people_limit: int = 24, walks: dict | None = None):
        self.enabled = enabled
        # The buses and the passengers (wilcoatc.atc.passengers): GSX's
        # alone, because nothing else in the simulator walks, whichever
        # vehicles the pilot prefers. Their own ceiling, so a dozen people
        # at one aeroplane do not take every other aeroplane's loader.
        self.people = people
        self.people_limit = max(0, int(people_limit))
        self.walks = load_walks() if walks is None else dict(walks)
        self._title_of: dict[str, str] = {}         # vehicle id -> title
        self._said_none: set[str] = set()
        # What each actor was handed, so a changed plan is handed again.
        self._clips_of: dict[str, tuple] = {}
        # The buses to look for (vehicle id -> when), and the readings
        # asked for (request -> vehicle id).
        self._probe_due: dict[str, list[float]] = {}
        self._probes: dict[int, str] = {}
        self._next_probe = _PROBE_REQUEST_BASE
        self._probe_ready = False
        self._said_alone: set[str] = set()
        # Requests for the walking passengers and the bags, not logged.
        self._quiet: set[int] = set()
        # GSX Pro's own vehicles first, where it is installed (see
        # GSX_TITLES); the simulator's stock ones otherwise.
        self.prefer_gsx = prefer_gsx
        # What the simulator listed, kept so the choice can be made again
        # when the pilot changes it from the panel.
        self._listed: list[str] = []
        self.limit = max(0, int(limit))
        self.dll_path = dll_path
        self.discover = discover
        self.titles: dict[str, tuple[str, ...]] = dict(TITLES)
        self._sim = None
        self._dll = None
        self._handle = None
        self._ready = False
        self._failed = False
        self._refused = False
        self._hook = None
        self._objects: dict[str, int] = {}          # vehicle id -> object
        self._pending: dict[int, str] = {}          # request -> vehicle id
        self._asked_when: dict[int, float] = {}
        self._asked_title: dict[int, str] = {}
        self._packets: dict[int, int] = {}          # packet -> request
        self._tried: dict[str, int] = {}
        self._bad_titles: set[str] = set()
        self._given_up: set[str] = set()            # vehicle ids
        self._moving: set[str] = set()
        # The vehicles that have been posed, and whether posing works.
        self._posed: set[str] = set()
        self._pose_ready = False
        # Each pose is read back once (POSE_VARS in their own order), and a
        # refusal names its packet: request/packet -> vehicle. A vehicle
        # whose pose read back as written is raised; when it was posed.
        self._pose_reads: dict[int, tuple[str, str, list[float]]] = {}
        self._pose_packets: dict[int, str] = {}
        self._next_pose_read = _POSE_READ_BASE
        self._raised: set[str] = set()
        self._posed_at: dict[str, float] = {}
        self._made = 0
        self._unanswered = 0
        self._next_request = _REQUEST_BASE
        self._discovering: threading.Thread | None = None
        self._discovery_began = 0.0
        self._discovered_at = 0.0
        self._lock = threading.Lock()
        # The drives being made, by vehicle: segments in metres and the
        # function that turns metres into degrees. Written by the engine's
        # tick, read every frame on the simulator's message thread.
        self._paths: dict[str, tuple[list, object]] = {}
        self._write_lock = threading.Lock()
        # Whether frames are coming and vehicles can be frozen; without
        # both, a vehicle is moved once a tick as it always was.
        self._frames = False
        self._freeze = False
        self.frames_seen = 0
        # Everything that moves, moved by one loop (wilcoatc.integrations
        # .motion): the vehicles here, and the people and buses after them.
        self.motion = MotionEngine()

    # -- setting up ---------------------------------------------------

    @property
    def available(self) -> bool:
        return self._ready and not self._failed

    @property
    def refused(self) -> bool:
        return self._refused

    @property
    def count(self) -> int:
        return len(self._objects)

    def has(self, vehicle_id: str) -> bool:
        return vehicle_id in self._objects

    def attach(self, simconnect) -> bool:
        """Take the live session, prepare the definition, and ask what
        vehicles this simulator has."""
        if not self.enabled or simconnect is None:
            return False
        self._sim = simconnect
        self._handle = getattr(simconnect, "hSimConnect", None)
        self._dll = getattr(simconnect, "dll", None)
        if self._handle is None or self._dll is None:
            self._failed = True
            return False
        if not hasattr(self._dll, "AICreateSimulatedObject"):
            # A wrapper without the call: nothing to do and nothing wrong.
            self._failed = True
            return False
        if not self._listen(simconnect):
            self._failed = True
            return False
        try:
            for name, unit in _MOVE:
                result = self._dll.AddToDataDefinition(
                    self._handle, _DEF_MOVE, name, unit,
                    SIMCONNECT_DATATYPE_FLOAT64, 0, -1)
                if result != 0:
                    raise OSError(f"SimConnect refused {name!r} ({result})")
        except Exception:
            log.info("the simulator would not accept the vehicle definition",
                     exc_info=True)
            self._stop_listening()
            self._failed = True
            return False
        # The poses are their own definitions, and their own failure: a
        # simulator that will not take them still draws the vehicles, at
        # whatever height they come.
        try:
            for kind, variables in POSE_VARS.items():
                for name, unit in variables:
                    if self._dll.AddToDataDefinition(
                            self._handle, _DEF_POSE[kind], name, unit,
                            SIMCONNECT_DATATYPE_FLOAT64, 0, -1) != 0:
                        raise OSError(f"SimConnect refused {name!r}")
            self._pose_ready = True
        except Exception:
            log.info("the simulator would not accept the vehicle poses; "
                     "the vehicles stay as they come", exc_info=True)
        try:
            for name, unit in ((b"PLANE LATITUDE", b"degrees"),
                               (b"PLANE LONGITUDE", b"degrees"),
                               (b"PLANE ALTITUDE", b"feet"),
                               (b"GROUND ALTITUDE", b"feet")):
                if self._dll.AddToDataDefinition(self._handle, _DEF_PROBE, name, unit,
                                                 SIMCONNECT_DATATYPE_FLOAT64, 0, -1) != 0:
                    raise OSError(f"SimConnect refused {name!r}")
            self._probe_ready = getattr(self._dll, "RequestDataOnSimObject",
                                        None) is not None
        except Exception:
            log.debug("the buses cannot be looked for", exc_info=True)
        # Driving smoothly: frozen vehicles, moved every frame. Its own
        # failure too -- without it they move once a tick, as before.
        try:
            mapper = getattr(self._dll, "MapClientEventToSimEvent", None)
            subscribe = getattr(self._dll, "SubscribeToSystemEvent", None)
            if mapper is not None and getattr(self._dll, "TransmitClientEvent",
                                              None) is not None:
                self._freeze = all(
                    mapper(self._handle, _EV_FREEZE_BASE + i, name) == 0
                    for i, name in enumerate(_FREEZE_EVENTS))
            if subscribe is not None:
                # The one subscription of the session, shared with the
                # traffic: a second one is refused (see subscribe_frames).
                self._frames = subscribe_frames(simconnect, self.on_frame)
        except Exception:
            log.info("the simulator will not freeze or frame the vehicles; "
                     "they move once a tick", exc_info=True)
            self._frames = self._freeze = False
        self._ready = True
        if self.smooth:
            self.motion.attach(self._dll, self._handle)
        if self.discover:
            self._start_discovery()
        return True

    def _start_discovery(self) -> None:
        def run():
            listed = None
            try:
                listed = enumerate_ground_vehicles(self.dll_path)
            except Exception:
                log.debug("listing the ground vehicles raised", exc_info=True)
            with self._lock:
                if listed:
                    self._listed = list(listed)
                    self.titles = choose_titles(listed,
                                                prefer_gsx=self.prefer_gsx)
                    log.info("the simulator lists %d ground objects; %s",
                             len(listed),
                             ", ".join(f"{kind}: {titles[0]}"
                                       for kind, titles in self.titles.items()
                                       if titles))
                else:
                    log.info("the simulator could not be asked for its "
                             "ground vehicles; the documented titles are used")
                self._discovered_at = time.monotonic()

        self._discovering = threading.Thread(
            target=run, name="WilcoATC-vehicles", daemon=True)
        self._discovered_at = 0.0
        self._discovery_began = time.monotonic()
        self._discovering.start()

    @property
    def discovering(self) -> bool:
        """Whether the simulator is still being asked what it has."""
        thread = self._discovering
        if thread is None or not thread.is_alive():
            return False
        return time.monotonic() - self._discovery_began < self.DISCOVERY_S

    def _listen(self, simconnect) -> bool:
        wanted = _assigned_message_id()
        refusal = _exception_message_id()

        def heard(data):
            if data.contents.dwID == _RECV_SIMOBJECT_DATA:
                head = ctypes.cast(data, ctypes.POINTER(_ObjectData)).contents
                if head.dwRequestID in self._pose_reads:
                    count = len(POSE_VARS[self._pose_reads[head.dwRequestID][1]])
                    raw = ctypes.string_at(ctypes.addressof(head),
                                           ctypes.sizeof(_ObjectData) + 8 * count)
                    values = [ctypes.c_double.from_buffer_copy(
                        raw, ctypes.sizeof(_ObjectData) + 8 * i).value
                        for i in range(count)]
                    self.on_pose_read(head.dwRequestID, values)
                    return True
                if head.dwRequestID in self._probes:
                    raw = ctypes.string_at(ctypes.addressof(head),
                                           ctypes.sizeof(_ObjectData) + 32)
                    values = [ctypes.c_double.from_buffer_copy(
                        raw, ctypes.sizeof(_ObjectData) + 8 * i).value
                        for i in range(4)]
                    self.on_probe(head.dwRequestID, *values)
                    return True
                if not self.motion.ours(head.dwRequestID):
                    return False
                raw = ctypes.string_at(ctypes.addressof(head), ctypes.sizeof(_ObjectData) + 24)
                lat, lon, heading = (ctypes.c_double.from_buffer_copy(
                    raw, ctypes.sizeof(_ObjectData) + 8 * i).value for i in range(3))
                return self.motion.on_position(head.dwRequestID, lat, lon, heading)
            if data.contents.dwID == refusal:
                failure = ctypes.cast(
                    data, ctypes.POINTER(_Exception)).contents
                posed = self._pose_packets.pop(failure.dwSendID, None)
                if posed is not None:
                    log.info("vehicles: the simulator refused the pose of %s "
                             "(exception %s)", posed, failure.dwException)
                    return True
                request = self._packets.pop(failure.dwSendID, None)
                if request is None:
                    return False
                self.on_refused(request, failure.dwException)
                return True
            if data.contents.dwID != wanted:
                return False
            reply = ctypes.cast(
                data, ctypes.POINTER(_AssignedObjectId)).contents
            if reply.dwRequestID not in self._pending:
                return False
            self.on_assigned(reply.dwRequestID, reply.dwObjectID)
            return True

        self._hook = install_dispatch(simconnect, heard)
        return self._hook is not None

    def _stop_listening(self) -> None:
        unsubscribe_frames(self._sim, self.on_frame)
        remove_dispatch(self._sim, self._hook)
        self._hook = None

    def detach(self) -> None:
        """Take every vehicle back out again."""
        for vehicle_id in list(self._objects):
            self._remove(vehicle_id)
        self._pending.clear()
        self._asked_when.clear()
        self._asked_title.clear()
        self._packets.clear()
        self._moving.clear()
        self._paths.clear()
        self.motion.clear()
        self._stop_listening()
        self._ready = False
        self._sim = self._dll = self._handle = None

    # -- keeping the simulator in step --------------------------------

    def sync(self, positions, now: float | None = None) -> None:
        """Create, move and remove, so the apron matches the turnaround."""
        if not self.available:
            return
        now = time.monotonic() if now is None else now
        try:
            vehicles = [p for p in positions if not p.get("timeline")]
            timed = [p for p in positions if p.get("timeline")] if self.people else []
            wanted = {p["id"]: p for p in vehicles[:self.limit]
                      + timed[:self.people_limit]}
            for one in self._operators(list(wanted.values()), now):
                wanted[one["id"]] = one
            for vehicle_id in list(self._objects):
                if vehicle_id not in wanted:
                    self._remove(vehicle_id)
            for request, asked in list(self._asked_when.items()):
                if now - asked > self.ANSWER_WITHIN_S:
                    self.on_refused(request, None)
            waiting = set(self._pending.values())
            for vehicle_id, position in wanted.items():
                if vehicle_id in self._objects:
                    self._follow(vehicle_id, position)
                    # Standing at its waiting point is standing, not at the
                    # aeroplane: its stairs or its belt go up at the door.
                    if (not position.get("moving")
                            and position.get("state") != handling.HOLDING
                            and vehicle_id not in self._posed):
                        self._pose(vehicle_id, position, now)
                elif (vehicle_id not in waiting
                      and vehicle_id not in self._given_up
                      and not self._refused and not self.discovering):
                    self._create(position, now)
            self._ask_probes(now)
            for vehicle_id in list(self._given_up):
                if vehicle_id not in wanted:
                    self._given_up.discard(vehicle_id)
                    self._tried.pop(vehicle_id, None)
        except Exception:
            log.info("the ground handling could not be kept in step",
                     exc_info=True)
            self._failed = True

    def _operators(self, vehicles: list[dict], now: float) -> list[dict]:
        """GSX's people on the vehicles they work on: one for every vehicle
        of their kind that the simulator has named, that is one of the
        models they were made to stand on, and that has stopped -- never
        before, and never on a vehicle that did not come."""
        if not self.prefer_gsx:
            return []
        with self._lock:
            listed = set(self._listed)
        found = []
        for position in vehicles:
            vehicle_id = position["id"]
            kind = handling.BASE_KIND.get(position.get("kind", ""),
                                          position.get("kind", ""))
            titles = [t for t in OPERATORS.get(kind, ())
                      if (not listed or t in listed) and t not in self._bad_titles]
            if not titles:
                continue
            if vehicle_id not in self._objects:
                if vehicle_id in self._given_up and vehicle_id not in self._said_alone:
                    self._said_alone.add(vehicle_id)
                    log.info("vehicles: no handler for %s: its loader was never "
                             "created", vehicle_id)
                continue
            # Not at its waiting point either: the clip is loading bags. And
            # not before the belt is up at the hold, or he throws the first
            # bags at a belt still lying on the ground.
            if position.get("moving") or position.get("state") == handling.HOLDING:
                continue
            if self._pose_ready and not self.raised(vehicle_id, now):
                continue
            title = self._title_of.get(vehicle_id, "")
            if title not in OPERATOR_VEHICLES.get(kind, ()):
                if vehicle_id not in self._said_alone:
                    self._said_alone.add(vehicle_id)
                    log.info("vehicles: no handler on %s: it is a %r, not a "
                             "loader he was made to stand on", vehicle_id, title)
                continue
            right, forward, turn = OPERATOR_OFFSET.get(titles[0], (0.0, 0.0, 0.0))
            lat, lon = float(position["lat"]), float(position["lon"])
            heading = float(position["heading"])
            if right or forward:
                h = math.radians(heading)
                north = forward * math.cos(h) - right * math.sin(h)
                east = forward * math.sin(h) + right * math.cos(h)
                lat += math.degrees(north / 6_371_008.8)
                lon += math.degrees(east / (6_371_008.8 * math.cos(math.radians(lat))))
            frames = position.get("frames") or [(0.0, bags.IDLE)]
            found.append({
                "id": vehicle_id + OPERATOR_SUFFIX, "kind": kind,
                "operator_of": vehicle_id, "titles": titles,
                "lat": lat, "lon": lon, "heading": (heading + turn) % 360.0,
                "altitude_ft": position.get("altitude_ft", 0.0),
                "moving": False, "path": position.get("path") or [],
                "to_latlon": position.get("to_latlon"),
                # The loader's frame: what he carries is what rides its belt.
                "channels": [OPERATOR_VAR + (frames,)],
            })
        return found

    def _ask_probes(self, now: float) -> None:
        """Read where the simulator has each bus that is due a look."""
        if not self._probe_ready:
            return
        for vehicle_id, due in list(self._probe_due.items()):
            object_id = self._objects.get(vehicle_id)
            if object_id is None:
                self._probe_due.pop(vehicle_id, None)
                continue
            if not due or now < due[0]:
                continue
            due.pop(0)
            if not due:
                self._probe_due.pop(vehicle_id, None)
            self._next_probe += 1
            try:
                self._dll.RequestDataOnSimObject(self._handle, self._next_probe,
                                                 _DEF_PROBE, int(object_id),
                                                 _PERIOD_ONCE, 0, 0, 0, 0)
                self._probes[self._next_probe] = vehicle_id
            except Exception:
                log.debug("could not look for %s", vehicle_id, exc_info=True)

    def on_probe(self, request_id: int, lat: float, lon: float,
                 altitude: float, ground: float) -> None:
        """Where the simulator has a bus, against where it is being put."""
        vehicle_id = self._probes.pop(int(request_id), None)
        if vehicle_id is None:
            return
        where = ""
        actor = self.motion.actor(vehicle_id)
        if actor is not None and actor.to_latlon is not None:
            found = paths.evaluate(actor.path, time.monotonic())
            if found is not None:
                wlat, wlon = actor.to_latlon(found[0], found[1])
                north = math.radians(lat - wlat) * 6_371_008.8
                east = (math.radians(lon - wlon) * 6_371_008.8
                        * math.cos(math.radians(lat)))
                where = (f"; it is being put at {wlat:.6f}, {wlon:.6f}, "
                         f"{math.hypot(north, east):.1f} m from there"
                         f" ({actor.state}, {self.motion.writes} writes so far)")
        else:
            where = "; nothing is driving it"
        log.info("vehicles: %s is at %.6f, %.6f, %.0f ft, the ground under it "
                 "%.0f ft (%.1f ft above it)%s", vehicle_id, lat, lon, altitude,
                 ground, altitude - ground, where)

    def set_prefer_gsx(self, prefer: bool) -> None:
        """Change between GSX's vehicles and the stock ones, now. A vehicle
        already out keeps its model; the next one of its kind is the other."""
        with self._lock:
            self.prefer_gsx = bool(prefer)
            if self._listed:
                self.titles = choose_titles(self._listed,
                                            prefer_gsx=self.prefer_gsx)

    def listed_gsx(self):
        """GSX's titles the simulator lists, where GSX is preferred: a set,
        None for any (not listed yet, the book stands), empty for none."""
        if not self.prefer_gsx:
            return set()
        with self._lock:
            listed = set(self._listed)
        return {t for t in listed if t.startswith("FSDT_")} if listed else None

    def title_for(self, kind: str) -> str:
        """The title a vehicle of this kind will be drawn with, for its
        model's own placement correction."""
        titles = self._titles_for(kind)
        return titles[0] if titles else ""

    def _titles_for(self, kind: str, vehicle_id: str = "") -> list[str]:
        if vehicle_id.endswith(OPERATOR_SUFFIX):
            return [t for t in OPERATORS.get(handling.BASE_KIND.get(kind, kind), ())
                    if t not in self._bad_titles]
        if kind in (people.BUS, people.PASSENGER):
            return self._people_titles(kind, vehicle_id)
        if kind in (bags.TRACTOR, bags.CART):
            return self._train_titles(kind, vehicle_id)
        with self._lock:
            titles = self.titles.get(handling.BASE_KIND.get(kind, kind), ())
        return [t for t in titles if t not in self._bad_titles]

    def _people_titles(self, kind: str, vehicle_id: str) -> list[str]:
        """The bus, or a passenger: GSX's, as far as the simulator lists
        them, and for a passenger a different one per person -- the same
        person every time, by their id -- so a dozen are not twelve twins."""
        with self._lock:
            listed = set(self._listed)
        wanted = BUS_TITLES if kind == people.BUS else tuple(sorted(self.walks))
        titles = [t for t in wanted if (not listed or t in listed)
                  and t not in self._bad_titles]
        if kind == people.PASSENGER and titles:
            turn = zlib.crc32(vehicle_id.encode("utf-8")) % len(titles)
            titles = titles[turn:] + titles[:turn]
        return titles

    def _train_titles(self, kind: str, vehicle_id: str) -> list[str]:
        """A tractor or a cart: GSX's first where the simulator lists it and
        the pilot prefers GSX, the stock one after."""
        with self._lock:
            listed = set(self._listed)
        if self.prefer_gsx:
            wanted = TRAIN_TITLES[kind] + STOCK_TRAIN_TITLES[kind]
        else:
            wanted = STOCK_TRAIN_TITLES[kind]
        titles = [t for t in wanted if (not listed or t in listed)
                  and t not in self._bad_titles]
        return titles

    def _create(self, position: dict, now: float) -> None:
        vehicle_id = position["id"]
        titles = self._titles_for(position.get("kind", ""), vehicle_id)
        chosen = position.get("title")
        if chosen and chosen not in self._bad_titles:
            # Chosen for this aeroplane's door (a staircase by its sill).
            titles = [chosen] + [t for t in titles if t != chosen]
        if not titles:
            # Every title for this kind has been refused: this simulator
            # has no such vehicle, and the apron does without it.
            kind = position.get("kind", "")
            if kind in (people.BUS, people.PASSENGER) and kind not in self._said_none:
                self._said_none.add(kind)
                log.info("no %s to draw: the simulator lists none of %s", kind,
                         "GSX's bus" if kind == people.BUS else "GSX's walking passengers")
            self._given_up.add(vehicle_id)
            return
        if self._tried.get(vehicle_id, 0) >= self.UNANSWERED_PER_VEHICLE:
            self._given_up.add(vehicle_id)
            return
        title = titles[0]
        start = init_position_type()()
        start.Latitude = float(position["lat"])
        start.Longitude = float(position["lon"])
        start.Altitude = float(position.get("altitude_ft") or 0.0)
        start.Pitch = 0.0
        start.Bank = 0.0
        start.Heading = float(position["heading"])
        start.OnGround = 1
        start.Airspeed = 0
        self._next_request += 1
        request = self._next_request
        said = log.debug if self._is_person(position) else log.info
        # Written down before asking: the answer comes on the simulator's
        # message thread and can arrive before this one has gone on, and a
        # request it does not know is an object nobody drives.
        if said is log.debug:
            self._quiet.add(request)
        self._pending[request] = vehicle_id
        self._asked_when[request] = now
        self._asked_title[request] = title
        result = self._dll.AICreateSimulatedObject(
            self._handle, title.encode("ascii", "ignore"), start, request)
        # At once: the frame writes on the other thread send packets too.
        packet = self._last_packet() if result == 0 else None
        if result != 0:
            said("vehicles: the simulator refused %s as %r (result %s)",
                 vehicle_id, title, result)
            self._bad_titles.add(title)
            self._pending.pop(request, None)
            self._asked_when.pop(request, None)
            self._asked_title.pop(request, None)
            self._quiet.discard(request)
            return
        said("vehicles: asked for %s as %r at %.6f, %.6f, %.0f ft, heading %.0f",
             vehicle_id, title, start.Latitude, start.Longitude, start.Altitude,
             start.Heading)
        if packet is not None:
            self._packets[packet] = request
        if position.get("moving"):
            self._moving.add(vehicle_id)

    @staticmethod
    def _is_person(position: dict) -> bool:
        """The walking passengers and the bags are too many to log one by
        one; a vehicle, and a handler on one, are not."""
        return position.get("kind") == people.PASSENGER

    def _last_packet(self) -> int | None:
        getter = getattr(self._dll, "GetLastSentPacketID", None)
        if getter is None:
            return None
        try:
            packet = ctypes.c_uint32()
            if getter(self._handle, ctypes.byref(packet)) != 0:
                return None
            return int(packet.value)
        except Exception:
            return None

    @property
    def smooth(self) -> bool:
        """Whether vehicles are driven every frame (and frozen) rather than
        moved once a tick."""
        return self._frames and self._freeze

    def _follow(self, vehicle_id: str, position: dict) -> None:
        """Move a vehicle that is driving, and once more when it stops.

        Driving smoothly, the drive is handed to :meth:`on_frame` and this
        only puts it at its final place once it has arrived. A parked one is
        left alone either way: frozen, it stays exactly where it was put.
        """
        if position.get("timeline") or (position.get("channels")
                                         and not position.get("moving")):
            # A bus, a person, or a loader standing at work: its whole plan.
            self._play(vehicle_id, position)
            return
        moving = bool(position.get("moving"))
        path = position.get("path") or []
        if moving and self.smooth and path and self.motion.available:
            current = self.motion.actor(vehicle_id)
            if current is None or current.path != path or \
                    current.object_id != self._objects[vehicle_id]:
                self.motion.add(Actor(vehicle_id, self._objects[vehicle_id],
                                      position.get("to_latlon"), path=list(path),
                                      frozen=self._freeze))
            self._moving.add(vehicle_id)
            return
        self.motion.remove(vehicle_id)
        self._paths.pop(vehicle_id, None)
        if not moving and vehicle_id not in self._moving:
            return
        self._write(self._objects[vehicle_id], float(position["lat"]),
                    float(position["lon"]), float(position["heading"]))
        if moving:
            self._moving.add(vehicle_id)
        else:
            self._moving.discard(vehicle_id)

    def _play(self, vehicle_id: str, position: dict) -> None:
        """A bus or a passenger: its whole visit, planned once, handed to the
        motion engine the first time and left to it -- parked or walking,
        it is still being written (its doors, its seats, its legs)."""
        if not (self.smooth and self.motion.available):
            return
        current = self.motion.actor(vehicle_id)
        object_id = self._objects[vehicle_id]
        path = position.get("path") or []
        # The same plan is handed over once; a new one (a loader's next
        # train, a run cut short) replaces it.
        plan = tuple((name, id(points), len(points))
                     for name, _, points in position.get("channels") or ())
        if (current is not None and current.object_id == object_id
                and current.path == path and self._clips_of.get(vehicle_id) == plan):
            return
        self._clips_of[vehicle_id] = plan
        channels = [Channel(name.encode("ascii") if isinstance(name, str) else name,
                            unit.encode("ascii") if isinstance(unit, str) else unit,
                            keyframed(points))
                    for name, unit, points in position.get("channels") or ()]
        person = bool(position.get("person"))
        if person:
            title = self._title_of.get(vehicle_id, "")
            start, stride = self.walks.get(title, (0, 0))
            if stride:
                pace = natural_pace(title, stride)
                spans = [(a, b, 30.0 * speed / pace)
                         for a, b, speed in position.get("walk") or ()]
                channels.append(Channel(*WALK_VAR, walking_while(start, stride, spans)))
        self.motion.add(Actor(vehicle_id, object_id, position.get("to_latlon"),
                              path=list(path), person=person, channels=channels,
                              lift=list(position.get("lift") or ()),
                              frozen=self._freeze))

    def _write(self, object_id: int, lat: float, lon: float,
               heading: float) -> None:
        where = _Where(lat, lon, heading % 360.0)
        with self._write_lock:
            self._dll.SetDataOnSimObject(
                self._handle, _DEF_MOVE, object_id,
                SIMCONNECT_DATA_SET_FLAG_DEFAULT, 0, ctypes.sizeof(_Where),
                ctypes.byref(where))

    def on_frame(self, now: float | None = None) -> None:
        """Once a visual frame, on the simulator's message thread: every
        driving vehicle to where its drive has it now."""
        self.frames_seen += 1
        if not self.available:
            return
        now = time.monotonic() if now is None else now
        if self.motion.available:
            self.motion.update(now)
            return
        for vehicle_id, (path, to_latlon) in list(self._paths.items()):
            object_id = self._objects.get(vehicle_id)
            if object_id is None or to_latlon is None:
                self._paths.pop(vehicle_id, None)
                continue
            found = paths.evaluate(path, now)
            if found is None:
                self._paths.pop(vehicle_id, None)
                continue
            x, z, heading, moving = found
            try:
                lat, lon = to_latlon(x, z)
                self._write(object_id, lat, lon, heading)
            except Exception:
                log.debug("could not drive %s", vehicle_id, exc_info=True)
                self._paths.pop(vehicle_id, None)
                continue
            if not moving and now >= paths.ends_at(path):
                self._paths.pop(vehicle_id, None)

    def rates(self) -> str:
        """The motion loop's figures since the last call, and each frame
        reader's share of the frame, for the status log."""
        if not (self.available and self.motion.available):
            return ""
        return f"{self.motion.rates()}; frame readers: {frame_timing(self._sim)}"

    def _hold_still(self, object_id: int) -> None:
        """Freeze a vehicle the moment it exists: it is this program that
        drives it, and the simulator left to itself creeps it away."""
        if not self._freeze:
            return
        try:
            for i in range(len(_FREEZE_EVENTS)):
                self._dll.TransmitClientEvent(
                    self._handle, int(object_id), _EV_FREEZE_BASE + i, 1,
                    _PRIORITY_HIGHEST, _EVENT_FLAG_GROUPID_IS_PRIORITY)
        except Exception:
            log.debug("could not freeze a vehicle", exc_info=True)

    def _pose(self, vehicle_id: str, position: dict,
              now: float | None = None) -> None:
        """Raise the stairs to the door, the belt to the hold, deploy the
        hoses: once, when the vehicle has stopped at the aeroplane.

        Logged at INFO either way, and read back once: whether a write to
        a created vehicle takes is the first thing a belt lying flat on the
        ground asks (Bilbao, 17 October: it did, 12.3 degrees)."""
        self._posed.add(vehicle_id)
        self._posed_at[vehicle_id] = time.monotonic() if now is None else now
        if not self._pose_ready:
            return
        kind = handling.BASE_KIND.get(position.get("kind", ""),
                                      position.get("kind", ""))
        variables = POSE_VARS.get(kind)
        values = position.get("pose") or {}
        if not variables or not values:
            self._raised.add(vehicle_id)       # nothing to raise
            return
        packed = (ctypes.c_double * len(variables))(
            *[float(values.get(name.decode(), 0.0)) for name, _ in variables])
        try:
            result = self._dll.SetDataOnSimObject(
                self._handle, _DEF_POSE[kind], self._objects[vehicle_id],
                SIMCONNECT_DATA_SET_FLAG_DEFAULT, 0, ctypes.sizeof(packed),
                ctypes.byref(packed))
        except Exception:
            log.info("the simulator will not pose vehicles", exc_info=True)
            self._pose_ready = False
            return
        asked = ", ".join(f"{name.decode()} {value:.2f}"
                          for (name, _), value in zip(variables, packed))
        if result != 0:
            log.info("vehicles: the simulator refused the pose of %s (%s): %s",
                     vehicle_id, result, asked)
            return
        packet = self._last_packet()
        if packet is not None:
            self._pose_packets[packet] = vehicle_id
        log.info("vehicles: %s posed as %r: %s", vehicle_id,
                 self._title_of.get(vehicle_id, ""), asked)
        if getattr(self._dll, "RequestDataOnSimObject", None) is None:
            return
        self._next_pose_read += 1
        try:
            if self._dll.RequestDataOnSimObject(
                    self._handle, self._next_pose_read, _DEF_POSE[kind],
                    self._objects[vehicle_id], _PERIOD_ONCE, 0, 0, 0, 0) == 0:
                self._pose_reads[self._next_pose_read] = (
                    vehicle_id, kind, list(packed))
        except Exception:
            log.debug("could not read back the pose of %s", vehicle_id,
                      exc_info=True)

    def on_pose_read(self, request_id: int, values: list[float]) -> None:
        """A pose as the simulator has it, against what was written."""
        asked = self._pose_reads.pop(int(request_id), None)
        if asked is None:
            return
        vehicle_id, kind, wanted = asked
        took = all(abs(a - b) < 0.05 for a, b in zip(values, wanted))
        if took and vehicle_id in self._objects:
            self._raised.add(vehicle_id)
        log.info("vehicles: %s pose reads back %s: %s", vehicle_id,
                 "as written" if took else "NOT as written",
                 ", ".join(f"{name.decode()} {got:.2f} (asked {want:.2f})"
                           for (name, _), got, want
                           in zip(POSE_VARS[kind], values, wanted)))

    def raised(self, vehicle_id: str, now: float | None = None) -> bool:
        """Whether a vehicle's pose has taken: read back as written, or
        written and not answered within POSE_ANSWER_S."""
        if vehicle_id in self._raised:
            return True
        at = self._posed_at.get(vehicle_id)
        if at is None:
            return False
        now = time.monotonic() if now is None else now
        return now - at >= POSE_ANSWER_S

    def _remove(self, vehicle_id: str) -> None:
        self._probe_due.pop(vehicle_id, None)
        self._clips_of.pop(vehicle_id, None)
        self._said_alone.discard(vehicle_id)
        self._moving.discard(vehicle_id)
        self._paths.pop(vehicle_id, None)
        self.motion.remove(vehicle_id)
        self._posed.discard(vehicle_id)
        self._raised.discard(vehicle_id)
        self._posed_at.pop(vehicle_id, None)
        self._title_of.pop(vehicle_id, None)
        object_id = self._objects.pop(vehicle_id, None)
        if object_id is None:
            return
        try:
            self._next_request += 1
            self._dll.AIRemoveObject(self._handle, object_id,
                                     self._next_request)
        except Exception:
            log.debug("could not remove %s", vehicle_id, exc_info=True)

    # -- what the simulator tells us ----------------------------------

    def on_assigned(self, request_id: int, object_id: int) -> None:
        self._made += 1
        self._refused = False
        request_id = int(request_id)
        title = self._asked_title.get(request_id, "")
        self._forget(request_id)
        vehicle_id = self._pending.pop(request_id, None)
        if vehicle_id:
            self._objects[vehicle_id] = int(object_id)
            self._title_of[vehicle_id] = title
            self._hold_still(object_id)
            if request_id not in self._quiet:
                log.info("vehicles: %s is object %d (%r)", vehicle_id,
                         int(object_id), title)
            if "/bus/" in vehicle_id:
                named = time.monotonic()
                self._probe_due[vehicle_id] = [named + t for t in PROBE_AFTER_S]
        self._quiet.discard(request_id)

    def on_refused(self, request_id: int, exception: int | None) -> None:
        """A title refused by name is struck off; silence counts against the
        vehicle, and enough of it against the simulator."""
        request_id = int(request_id)
        title = self._asked_title.get(request_id)
        self._forget(request_id)
        vehicle_id = self._pending.pop(request_id, None)
        if vehicle_id is None:
            return
        if exception is not None and title:
            log.info("the simulator has no %r (exception %s)", title,
                     exception)
            self._bad_titles.add(title)
            return
        self._tried[vehicle_id] = self._tried.get(vehicle_id, 0) + 1
        self._unanswered += 1
        if request_id not in self._quiet:
            log.info("vehicles: no answer creating %s as %r (%d unanswered)",
                     vehicle_id, title, self._tried[vehicle_id])
        self._quiet.discard(request_id)
        if (not self._refused and self._made == 0
                and self._unanswered >= self.REFUSALS_BEFORE_GIVING_UP):
            self._refused = True
            log.info("the simulator is not creating ground vehicles; the "
                     "apron is left as it is")

    def _forget(self, request_id: int) -> None:
        self._asked_when.pop(request_id, None)
        self._asked_title.pop(request_id, None)
        for packet, request in list(self._packets.items()):
            if request == request_id:
                del self._packets[packet]

    def describe(self) -> str:
        if not self.enabled:
            return "ground handling in the simulator: off"
        if not self.available:
            return "ground handling in the simulator: not connected"
        if self._refused:
            return ("ground handling in the simulator: refused — this "
                    "simulator will not create ground vehicles")
        return (f"ground handling in the simulator: {len(self._objects)} "
                f"vehicles, {len(self._pending)} being created")


__all__ = ["SimVehicles", "TITLES", "KEYWORDS", "POSE_VARS", "choose_titles",
           "read_listing", "read_pairs", "enumerate_ground_vehicles",
           "enumerate_simobjects"]
