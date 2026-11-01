"""The doors and holds of the other aeroplanes.

An aeroplane with stairs at it and its doors shut is a model; one with the
forward door open and the hold open under the loader is an aeroplane being
worked. The simulator does this for its own ATC aircraft and for nothing
else, so this does it for the traffic this program draws.

What was established on a running 2024, and is not questioned here:

* A door on a particular AI aeroplane is opened with the key event
  ``TOGGLE_AIRCRAFT_EXIT`` transmitted *to that object*, with the door's
  number as the parameter -- the interactive point's index plus one. It is
  a toggle, so the state is always read before it is sent.
* The state is the simvar ``INTERACTIVE POINT OPEN:n``, read per object in
  ``percent over 100``: 0 shut, 1 open, in between while it swings. Writing
  it is accepted and ignored; the event is the only way.
* Each aeroplane's doors are its own. Opening one on A does nothing to B.
* Which index is which door is a property of the model, not of the type:
  on the Asobo A321 the passenger doors are 0-3 and the holds 6 and 7.
  That is data, and it lives in ``doors.json`` beside this module, matched
  on the simulator title. FSLTL's FAIB models have no door animation at
  all -- the simulator says the door is open and nothing moves -- so they
  are excluded by name, and a title with no entry has its doors left alone.
* An aeroplane created with ``AICreateParkedATCAircraft`` has its doors
  shut again by the simulator. Everything this program draws is a non-ATC
  aircraft, which is why it can be done at all.

The first version of this lived inside the vehicle injector with the door
numbers written into the turnaround (door 3 was "the hold"); on the A321 it
was opening the second passenger door while the loader stood at a shut
hold. Hence the table, and hence the roles: the turnaround says *which
door* it wants open -- ``pax_l1``, ``cargo_fwd`` -- and the table says what
number that is on this model.

Per aeroplane there is a small state machine, ``at_gate -> turnaround ->
closing -> ready_for_push``, derived from what is wanted and what the doors
actually read rather than from a timetable: the timetable is the
turnaround's business (:mod:`wilcoatc.atc.handling`). What this adds is the
one answer the traffic needs from it, :meth:`SimDoors.can_push`: every door
read shut. A door that does not answer three toggles is given up on and
said so once; a closing that has gone on past a limit is forced -- one more
toggle, a warning, and the aeroplane is released -- because a departure
that never pushes is worse than one that pushes with a door ajar.

Nothing here blocks. Readings are asked for and arrive on the shared
message pump; a tick looks at what has arrived.
"""

from __future__ import annotations

import ctypes
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .aitraffic import (SIMCONNECT_DATATYPE_FLOAT64, _Received,
                        install_dispatch, remove_dispatch)

log = logging.getLogger("wilcoatc.doors")

TOGGLE_EXIT_EVENT = b"TOGGLE_AIRCRAFT_EXIT"
_EV_TOGGLE_EXIT = 4722
_DEF_DOORS = 4745
_REQ_BASE = 4900
_RECV_SIMOBJECT_DATA = 8
_PERIOD_ONCE = 1
# GroupID is a priority when this flag is set; the highest.
_EVENT_FLAG_GROUPID_IS_PRIORITY = 0x10
_PRIORITY_HIGHEST = 1

#: How old a reading may be before the aeroplane is asked again, and how
#: old one may be and still be acted on. A toggle on a stale reading is a
#: blind toggle: the door somebody shut in the meantime goes open again.
READ_EVERY_S = 4.0
STALE_S = 3 * READ_EVERY_S
#: How long a door is left alone after a toggle: a swing and a little.
#: Toggling it again sooner reverses it half way.
SWING_S = 15.0
#: Shut is at or under this, open is at or over one minus it; anything in
#: between is a door on the move (percent over 100).
SETTLED = 0.01
#: Toggles a door may ignore before it is given up on.
TRIES = 3
#: How long a departure's doors may go on closing before they are forced.
FORCE_AFTER_S = 300.0

#: The word in the title of every one of the simulator's own AI models.
PASSIVE = "passiveaircraft"

# The phases, as the prompt names them.
AT_GATE = "at_gate"
TURNAROUND = "turnaround"
CLOSING = "closing"
READY = "ready_for_push"

_BUNDLED = Path(__file__).with_name("doors.json")


def is_hold(role: str) -> bool:
    """A role beginning ``cargo`` is a hold; everything else a door."""
    return role.lower().startswith("cargo")


class DoorTable:
    """Which interactive point is which door, per model, by title."""

    def __init__(self, entries: dict[str, dict] | None = None,
                 source: Path | None = None):
        self.entries: dict[str, dict] = {
            name: entry for name, entry in (entries or {}).items()
            if not name.startswith("_") and isinstance(entry, dict)
            and isinstance(entry.get("doors"), dict)}
        self.source = source

    @classmethod
    def parse(cls, text: str, source: Path | None = None) -> "DoorTable":
        return cls(json.loads(text), source)

    @classmethod
    def load(cls, path: str | Path = "") -> "DoorTable":
        """The table at ``path``, or the one shipped beside this module.

        A table that cannot be read is an empty one, logged: the doors stay
        as they are and everything else carries on.
        """
        where = Path(path) if path else _BUNDLED
        try:
            return cls.parse(where.read_text(encoding="utf-8"), where)
        except Exception:
            log.info("the door table %s could not be read; doors are left "
                     "alone", where, exc_info=True)
            return cls({}, where)

    def __len__(self) -> int:
        return len(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)

    def match(self, title: str) -> tuple[str, dict[str, int]] | None:
        """The entry for this simulator title: its name and role -> index.

        Case does not matter. An excluded word rules a title out before a
        matched one lets it in, so ``FSLTL_FAIB_A321_SWR`` matches nothing
        although it contains ``A321``.

        And a passive model never takes a flyable model's entry, nor the
        other way round. "A321" is the flyable A321, whose doors are at the
        points the entry names; "Asobo PassiveAircraft A321 CEO" is another
        model, whose points at those indices toggle and read open while no
        door on it moves (measured at Madeira, 14 October) -- and the entry's
        "A321" was a substring of both. An entry is for the passive models
        only if its own match words say "PassiveAircraft".
        """
        text = (title or "").lower()
        if not text:
            return None
        passive = PASSIVE in text
        for name, entry in self.entries.items():
            if any(word.lower() in text for word in entry.get("exclude", ())):
                continue
            for_passive = any(PASSIVE in word.lower()
                              for word in entry.get("match", ()))
            if passive != for_passive:
                continue
            if any(word.lower() in text for word in entry.get("match", ())):
                return name, {role: int(index)
                              for role, index in entry["doors"].items()}
        return None

    @property
    def indices(self) -> tuple[int, ...]:
        """Every interactive point any entry names, in order. This is what
        is read off every handled aeroplane, whichever model it is."""
        found: set[int] = set()
        for entry in self.entries.values():
            found.update(int(index) for index in entry["doors"].values())
        return tuple(sorted(found))


@dataclass
class DoorPlan:
    """One aeroplane's doors: what is wanted, what is read, what was tried."""

    object_id: int
    key: str
    model: str
    doors: dict[str, int]                       # role -> index
    goal: dict[int, bool] = field(default_factory=dict)      # index -> open?
    reading: dict[int, float] = field(default_factory=dict)  # index -> 0..1
    read_at: float | None = None
    asked_at: float = -1e9
    toggled_at: dict[int, float] = field(default_factory=dict)
    tries: dict[int, int] = field(default_factory=dict)
    deaf: set[int] = field(default_factory=set)
    # When every door was first wanted shut after some had been open, for
    # the forcing limit; and whether that limit was reached.
    closing_since: float | None = None
    forced: bool = False
    was_open: bool = False
    # The doors this program opened and has not yet seen shut.
    opened: set[int] = field(default_factory=set)

    def index_of(self, role: str) -> int | None:
        return self.doors.get(role)

    def is_open(self, index: int) -> bool | None:
        """True open, False shut, None unknown or still swinging."""
        value = self.reading.get(index)
        if value is None:
            return None
        if value >= 1.0 - SETTLED:
            return True
        if value <= SETTLED:
            return False
        return None

    def wants_open(self) -> bool:
        return any(self.goal.values())

    def all_shut(self) -> bool:
        """Every door this knows about reads shut, or cannot be moved."""
        if self.forced:
            return True
        for index in self.doors.values():
            if index in self.deaf:
                continue
            if self.is_open(index) is not False:
                return False
        return True

    def phase(self) -> str:
        if self.wants_open():
            return TURNAROUND
        if not self.all_shut():
            return CLOSING
        return READY if self.was_open else AT_GATE

    def can_push(self) -> bool:
        return not self.wants_open() and self.all_shut()


class SimDoors:
    """The doors, in the simulator.

    Give :meth:`sync` the aeroplanes being handled -- object id, title, the
    turnaround's key for it and the roles that should be open -- every tick,
    and it reads, toggles, retries and gives up on its own time. Ask
    :meth:`can_push` before a departure moves.
    """

    def __init__(self, enabled: bool = True,
                 table: DoorTable | None = None,
                 force_after_s: float = FORCE_AFTER_S):
        self.enabled = enabled
        self.table = table if table is not None else DoorTable.load()
        self.force_after_s = force_after_s
        self._sim = None
        self._dll = None
        self._handle = None
        self._hook = None
        self._ready = False
        self._refused = False
        self._indices: tuple[int, ...] = ()
        self._plans: dict[int, DoorPlan] = {}
        # The pilot's own aeroplane, as the simulator names it (IS USER SIM,
        # read by wilcoatc.integrations.simaircraft). Never registered,
        # never toggled: on 13 October its doors were opened by this layer
        # because it had been taken for a parked airliner.
        self.user_object_id: int | None = None
        self._asked: dict[int, int] = {}            # request -> object
        self._next_request = _REQ_BASE
        self._unmatched: set[str] = set()           # titles said once
        self._seen = False
        self._toggled = False

    # -- setting up ---------------------------------------------------

    @property
    def available(self) -> bool:
        return self.enabled and self._ready and not self._refused

    def attach(self, simconnect) -> bool:
        """Map the event and declare the readings, on the live session."""
        if not self.enabled or simconnect is None:
            return False
        if not self.table:
            log.info("the door table is empty; no aeroplane's doors are "
                     "touched")
            return False
        dll = getattr(simconnect, "dll", None)
        handle = getattr(simconnect, "hSimConnect", None)
        if dll is None or handle is None:
            return False
        self._sim, self._dll, self._handle = simconnect, dll, handle
        try:
            mapper = getattr(dll, "MapClientEventToSimEvent", None)
            sender = getattr(dll, "TransmitClientEvent", None)
            reader = getattr(dll, "RequestDataOnSimObject", None)
            if mapper is None or sender is None or reader is None:
                raise OSError("this wrapper cannot send events to an object")
            if mapper(handle, _EV_TOGGLE_EXIT, TOGGLE_EXIT_EVENT) != 0:
                raise OSError("SimConnect refused the exit event")
            indices = self.table.indices
            for index in indices:
                name = f"INTERACTIVE POINT OPEN:{index}".encode()
                if dll.AddToDataDefinition(
                        handle, _DEF_DOORS, name, b"percent over 100",
                        SIMCONNECT_DATATYPE_FLOAT64, 0, -1) != 0:
                    raise OSError(f"SimConnect refused {name!r}")
            self._indices = indices
        except Exception:
            log.info("the simulator would not take the door event; doors "
                     "stay as they are", exc_info=True)
            self._sim = self._dll = self._handle = None
            return False
        if not self._listen(simconnect):
            self._sim = self._dll = self._handle = None
            return False
        self._ready = True
        log.info("doors: %d model(s) in the table from %s, reading %d "
                 "interactive points", len(self.table),
                 self.table.source or "memory", len(self._indices))
        return True

    def _listen(self, simconnect) -> bool:
        count = len(self._indices)

        class _Reply(_Received):
            _pack_ = 1
            _fields_ = [
                ("dwRequestID", ctypes.c_uint32),
                ("dwObjectID", ctypes.c_uint32),
                ("dwDefineID", ctypes.c_uint32),
                ("dwFlags", ctypes.c_uint32),
                ("dwentrynumber", ctypes.c_uint32),
                ("dwoutof", ctypes.c_uint32),
                ("dwDefineCount", ctypes.c_uint32),
                ("values", ctypes.c_double * count),
            ]

        self._reply_type = _Reply

        def heard(data):
            if data.contents.dwID != _RECV_SIMOBJECT_DATA:
                return False
            reply = ctypes.cast(data, ctypes.POINTER(_Reply)).contents
            if reply.dwRequestID not in self._asked:
                return False
            self.on_reading(reply.dwRequestID, reply.dwObjectID,
                            list(reply.values))
            return True

        self._hook = install_dispatch(simconnect, heard)
        return self._hook is not None

    def detach(self) -> None:
        """Shut what this opened, on the way out.

        There is no pump to wait for a reading, so the last one is used
        however old it is, and a door this opened and has no reading of is
        toggled shut on trust: the one blind toggle, and it is on the way
        out.
        """
        if self._ready:
            try:
                for plan in list(self._plans.values()):
                    for index in sorted(plan.opened):
                        state = plan.is_open(index)
                        if state is True or state is None:
                            self._toggle(plan, index)
            except Exception:
                log.debug("could not shut the doors", exc_info=True)
        self._plans.clear()
        self._asked.clear()
        remove_dispatch(self._sim, self._hook)
        self._hook = None
        self._ready = False
        self._sim = self._dll = self._handle = None

    # -- what is wanted -----------------------------------------------

    def is_the_pilot(self, object_id) -> bool:
        """Whether this object is the pilot's own aeroplane, or SimConnect's
        name for it (object 0, SIMCONNECT_OBJECT_ID_USER)."""
        if object_id is None:
            return False
        object_id = int(object_id)
        return object_id == 0 or (self.user_object_id is not None
                                  and object_id == int(self.user_object_id))

    def register(self, object_id: int, title: str, key: str = "") -> bool:
        """Know this aeroplane's doors. False if its model has no entry,
        and always for the pilot's own aeroplane."""
        object_id = int(object_id)
        if self.is_the_pilot(object_id):
            if self._plans.pop(object_id, None) is not None:
                log.warning("doors: object %d is the pilot's own aeroplane; "
                            "its doors are not this program's", object_id)
            return False
        plan = self._plans.get(object_id)
        found = self.table.match(title)
        if found is None:
            if plan is not None:
                self._plans.pop(object_id, None)
            if title and title not in self._unmatched:
                self._unmatched.add(title)
                log.info("doors: no entry for %r; its doors are left alone",
                         title)
            return False
        model, doors = found
        if plan is not None and plan.model == model:
            plan.key = key or plan.key
            return True
        self._plans[object_id] = DoorPlan(object_id, key, model, doors)
        log.info("doors: %s (object %d) is a %s", key or "aeroplane",
                 object_id, model)
        return True

    def want(self, object_id: int, roles: set[str], now: float) -> None:
        """Which of this aeroplane's doors should be open, by role."""
        plan = self._plans.get(int(object_id))
        if plan is None:
            return
        wanted = {plan.doors[role] for role in roles if role in plan.doors}
        for index in plan.doors.values():
            before = plan.goal.get(index)
            after = index in wanted
            if before != after:
                plan.goal[index] = after
                plan.tries.pop(index, None)
                log.info("doors: %s %s %s", plan.key or plan.object_id,
                         "opening" if after else "closing",
                         self._role_of(plan, index))
        if plan.wants_open():
            plan.was_open = True
            plan.closing_since = None
            plan.forced = False
        elif plan.was_open and plan.closing_since is None:
            plan.closing_since = now

    def forget(self, object_id: int) -> None:
        self._plans.pop(int(object_id), None)

    def carry(self, old_id: int, new_id: int, title: str) -> None:
        """The aeroplane has changed object (a model swap): its doors go
        with it. What was wanted open is wanted open on the new object,
        and reopened once it reads shut there; what was tried and given
        up on the old model is tried afresh on the new one. The old object
        is gone, so nothing is owed to it.
        """
        plan = self._plans.pop(int(old_id), None)
        if plan is None:
            return
        if not self.register(new_id, title, plan.key):
            # The new model has no doors to speak of (FSLTL's): the
            # aeroplane is now one whose doors are left alone.
            return
        fresh = self._plans[int(new_id)]
        roles = {role for role, index in plan.doors.items()
                 if plan.goal.get(index)}
        fresh.was_open = plan.was_open
        fresh.goal = {fresh.doors[role]: True for role in roles
                      if role in fresh.doors}
        log.info("doors: %s carried to object %d, reopening %s",
                 plan.key or old_id, int(new_id),
                 sorted(roles) if roles else "nothing")

    def sync(self, wanted: dict[int, tuple[str, str, set[str]]],
             now: float | None = None) -> None:
        """One call a tick: ``wanted`` is object id -> (title, key, roles
        that should be open). An aeroplane no longer in it has its doors
        shut and is forgotten once they are."""
        if not self.available:
            return
        now = time.monotonic() if now is None else now
        try:
            for object_id, (title, key, roles) in wanted.items():
                if self.register(object_id, title, key):
                    self.want(object_id, set(roles), now)
            for object_id in self._plans:
                if object_id not in wanted:
                    self.want(object_id, set(), now)
            self.tick(now)
            # Forgotten once its doors are read shut, after the tick that
            # may have read them so.
            for object_id, plan in list(self._plans.items()):
                if (object_id not in wanted and plan.can_push()
                        and not plan.opened):
                    self._plans.pop(object_id, None)
        except Exception:
            log.info("the doors could not be kept in step", exc_info=True)
            self._refused = True

    # -- the tick -------------------------------------------------------

    def tick(self, now: float) -> None:
        """Read, compare, toggle, retry, give up, force."""
        for plan in list(self._plans.values()):
            if plan.read_at is None or now - plan.read_at >= READ_EVERY_S:
                self._ask(plan, now)
            if plan.read_at is None or now - plan.read_at > STALE_S:
                continue        # nothing fresh to act on: asked, not answered
            for index, want_open in plan.goal.items():
                if index in plan.deaf:
                    continue
                state = plan.is_open(index)
                if state is None:
                    continue                   # swinging, or unknown
                if state == want_open:
                    plan.tries.pop(index, None)
                    if not want_open:
                        plan.opened.discard(index)
                    continue
                if now - plan.toggled_at.get(index, -1e9) < SWING_S:
                    continue
                tries = plan.tries.get(index, 0)
                if tries >= TRIES:
                    plan.deaf.add(index)
                    log.warning("doors: %s %s does not answer after %d "
                                "toggles; left alone",
                                plan.key or plan.object_id,
                                self._role_of(plan, index), TRIES)
                    continue
                if self._toggle(plan, index):
                    plan.toggled_at[index] = now
                    plan.tries[index] = tries + 1
                    if want_open:
                        plan.opened.add(index)
                    # Read it again before touching it again.
                    plan.reading.pop(index, None)
            if (plan.closing_since is not None and not plan.forced
                    and not plan.wants_open() and not plan.all_shut()
                    and now - plan.closing_since >= self.force_after_s):
                plan.forced = True
                still = [self._role_of(plan, i) for i in plan.doors.values()
                         if plan.is_open(i) is not False and i not in plan.deaf]
                log.warning("doors: %s still has %s open after %.0f s; "
                            "forcing the close and releasing it",
                            plan.key or plan.object_id, ", ".join(still),
                            self.force_after_s)
                for index in plan.doors.values():
                    if plan.is_open(index) is not False and index not in plan.deaf:
                        self._toggle(plan, index)
                        plan.toggled_at[index] = now
        if self._refused:
            return

    def _ask(self, plan: DoorPlan, now: float) -> None:
        if now - plan.asked_at < READ_EVERY_S:
            return
        self._next_request += 1
        request = self._next_request
        try:
            result = self._dll.RequestDataOnSimObject(
                self._handle, request, _DEF_DOORS, plan.object_id,
                _PERIOD_ONCE, 0, 0, 0, 0)
        except Exception:
            log.info("the simulator will not report an aeroplane's doors",
                     exc_info=True)
            self._refused = True
            return
        if result != 0:
            return
        self._asked[request] = plan.object_id
        plan.asked_at = now

    def on_reading(self, request_id: int, object_id: int,
                   values: list[float]) -> None:
        """One aeroplane's interactive points, off the message pump."""
        self._asked.pop(int(request_id), None)
        plan = self._plans.get(int(object_id))
        if plan is None:
            return
        plan.reading = {index: float(value)
                        for index, value in zip(self._indices, values)}
        plan.read_at = time.monotonic() if plan.asked_at < 0 else plan.asked_at
        if not self._seen:
            self._seen = True
            log.info("doors: the first aeroplane's points read back as %s",
                     {i: round(v, 2) for i, v in plan.reading.items()})

    def _toggle(self, plan: DoorPlan, index: int) -> bool:
        if self.is_the_pilot(plan.object_id):
            # Belt and braces: nothing reaches the pilot's doors, whatever
            # put a plan here.
            self._plans.pop(plan.object_id, None)
            return False
        if not self._toggled:
            self._toggled = True
            log.info("doors: toggling the first door, point %d of object %d",
                     index, plan.object_id)
        try:
            result = self._dll.TransmitClientEvent(
                self._handle, plan.object_id, _EV_TOGGLE_EXIT, index + 1,
                _PRIORITY_HIGHEST, _EVENT_FLAG_GROUPID_IS_PRIORITY)
        except Exception:
            log.info("the simulator will not take the door event; doors "
                     "stay as they are", exc_info=True)
            self._refused = True
            return False
        if result != 0:
            log.debug("the simulator refused point %d of %d (%s)", index,
                      plan.object_id, result)
            return False
        return True

    @staticmethod
    def _role_of(plan: DoorPlan, index: int) -> str:
        for role, found in plan.doors.items():
            if found == index:
                return role
        return f"point {index}"

    # -- what the traffic asks ----------------------------------------

    def can_push(self, object_id: int | None) -> bool:
        """Whether this aeroplane may move: every door it has read shut.

        An aeroplane with no plan -- unknown model, doors switched off, no
        simulator -- is not held back by doors nobody is working.
        """
        if object_id is None:
            return True
        plan = self._plans.get(int(object_id))
        return True if plan is None else plan.can_push()

    def moving(self, object_id: int | None, now: float) -> bool:
        """Whether any of this aeroplane's doors is on the move: read
        between shut and open, or toggled less than a swing ago and not
        read since. What a model swap must never happen during -- the new
        object would start with that door wherever its model starts it.
        """
        if object_id is None:
            return False
        plan = self._plans.get(int(object_id))
        if plan is None:
            return False
        for index in plan.doors.values():
            if index in plan.deaf:
                continue
            value = plan.reading.get(index)
            if value is not None and SETTLED < value < 1.0 - SETTLED:
                return True
            if now - plan.toggled_at.get(index, -1e9) < SWING_S:
                return True
        return False

    def open_now(self, object_id: int | None) -> bool:
        """Whether any door is open or wanted open on this aeroplane."""
        if object_id is None:
            return False
        plan = self._plans.get(int(object_id))
        return plan is not None and (plan.wants_open() or not plan.all_shut())

    def phase(self, object_id: int) -> str | None:
        plan = self._plans.get(int(object_id))
        return None if plan is None else plan.phase()

    def plan(self, object_id: int) -> DoorPlan | None:
        return self._plans.get(int(object_id))

    def describe(self) -> str:
        if not self.enabled:
            return "doors: off"
        if not self.available:
            return "doors: not connected"
        return (f"doors: {len(self._plans)} aeroplane(s), "
                f"{len(self.table)} model(s) known")


__all__ = ["DoorTable", "DoorPlan", "SimDoors", "is_hold", "AT_GATE",
           "TURNAROUND", "CLOSING", "READY"]
