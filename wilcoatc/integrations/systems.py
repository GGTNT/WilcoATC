"""The engines and lights of this program's own aeroplanes, in the simulator.

:mod:`wilcoatc.atc.systems` decides what each aeroplane's engines and lights
should be; this makes the simulator agree, the way
:mod:`wilcoatc.integrations.doors` does for the doors, and on the same
terms: only this program's own objects, never the pilot's, read back on the
object, a few tries and then left alone.

What works was measured on a running 2024 before any of this was written (a
TAP A321, FSLTL's model, at Madeira): ``ENGINE_AUTO_SHUTDOWN`` aimed at the
object had combustion 0 and N1 0 within fifteen seconds; ``ENGINE_AUTO_START``
had them back at 30 % in the same time; ``NAV_/BEACON_/STROBES_/TAXI_/
LANDING_LIGHTS_SET`` with 1 turned each light on, read back on the object
and seen from the cockpit beside it. No variable had to be written. The SET
events take the value they set, so unlike the door toggle they can be sent
again without undoing themselves; the reading is there to know when to stop
sending, and to notice a model that ignores one.
"""

from __future__ import annotations

import ctypes
import logging
import time
from dataclasses import dataclass, field

from ..atc.systems import FIELDS, Systems
from .aitraffic import (SIMCONNECT_DATATYPE_FLOAT64, _Received,
                        install_dispatch, remove_dispatch)

log = logging.getLogger("wilcoatc.systems")

# This client's numbers, clear of everybody else's (doors are 47xx-49xx and
# count upwards from there, so the requests here start far away).
_EV_BASE = 4730
_DEF_READ = 4747
_REQ_BASE = 600000
_RECV_SIMOBJECT_DATA = 8
_PERIOD_ONCE = 1
_EVENT_FLAG_GROUPID_IS_PRIORITY = 0x10
_PRIORITY_HIGHEST = 1

#: The events, by what they move. Engines take no value; lights take 0/1.
EVENTS = {
    "engines_on": b"ENGINE_AUTO_START",
    "engines_off": b"ENGINE_AUTO_SHUTDOWN",
    "nav": b"NAV_LIGHTS_SET",
    "beacon": b"BEACON_LIGHTS_SET",
    "strobe": b"STROBES_SET",
    "taxi": b"TAXI_LIGHTS_SET",
    "landing": b"LANDING_LIGHTS_SET",
}
#: What is read back, in this order.
READ = (
    (b"GENERAL ENG COMBUSTION:1", b"bool"),
    (b"TURB ENG N1:1", b"percent"),
    (b"LIGHT NAV", b"bool"),
    (b"LIGHT BEACON", b"bool"),
    (b"LIGHT STROBE", b"bool"),
    (b"LIGHT TAXI", b"bool"),
    (b"LIGHT LANDING", b"bool"),
)
#: How often each aeroplane is read, and how old a reading may be and still
#: be acted on.
READ_EVERY_S = 5.0
STALE_S = 3 * READ_EVERY_S
#: How long after an event its effect is waited for before it is sent
#: again: engines take fifteen seconds to wind up or down (measured), a
#: light a moment.
SETTLE_S = {"engines": 20.0}
SETTLE_LIGHT_S = 4.0
#: Sends an aeroplane may ignore before that light or the engines are left.
TRIES = 3
#: Turbine N1 above this is running, whatever the combustion flag says.
RUNNING_N1 = 5.0


def read_as_systems(values: list[float]) -> Systems:
    combustion, n1, nav, beacon, strobe, taxi, landing = (list(values) + [0.0] * 7)[:7]
    return Systems(engines=combustion >= 0.5 or n1 > RUNNING_N1,
                   nav=nav >= 0.5, beacon=beacon >= 0.5, strobe=strobe >= 0.5,
                   taxi=taxi >= 0.5, landing=landing >= 0.5)


@dataclass
class Plan:
    """One aeroplane: what is wanted, what was read, what was sent."""

    object_id: int
    key: str
    goal: Systems
    reading: Systems | None = None
    read_at: float | None = None
    asked_at: float = -1e9
    sent_at: dict[str, float] = field(default_factory=dict)
    sent_for: dict[str, bool] = field(default_factory=dict)
    tries: dict[str, int] = field(default_factory=dict)
    deaf: set[str] = field(default_factory=set)


class SimSystems:
    """Engines and lights, kept to what the traffic says."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._sim = self._dll = self._handle = None
        self._hook = None
        self._ready = False
        self._refused = False
        self._plans: dict[int, Plan] = {}
        self._asked: dict[int, int] = {}
        self._next_request = _REQ_BASE
        self._events: dict[str, int] = {}
        # The pilot's own aeroplane, which nothing here ever touches.
        self.user_object_id: int | None = None

    @property
    def available(self) -> bool:
        return self.enabled and self._ready and not self._refused

    def attach(self, simconnect) -> bool:
        if not self.enabled or simconnect is None:
            return False
        dll = getattr(simconnect, "dll", None)
        handle = getattr(simconnect, "hSimConnect", None)
        if dll is None or handle is None:
            return False
        self._sim, self._dll, self._handle = simconnect, dll, handle
        try:
            mapper = getattr(dll, "MapClientEventToSimEvent", None)
            if mapper is None or getattr(dll, "TransmitClientEvent", None) is None:
                raise OSError("this wrapper cannot send events to an object")
            for offset, (what, name) in enumerate(EVENTS.items()):
                if mapper(handle, _EV_BASE + offset, name) != 0:
                    raise OSError(f"SimConnect refused {name!r}")
                self._events[what] = _EV_BASE + offset
            for name, unit in READ:
                if dll.AddToDataDefinition(handle, _DEF_READ, name, unit,
                                           SIMCONNECT_DATATYPE_FLOAT64, 0, -1) != 0:
                    raise OSError(f"SimConnect refused {name!r}")
        except Exception:
            log.info("the simulator would not take the engine and light "
                     "events; the traffic keeps them as it is", exc_info=True)
            self._sim = self._dll = self._handle = None
            return False
        if not self._listen(simconnect):
            self._sim = self._dll = self._handle = None
            return False
        self._ready = True
        return True

    def _listen(self, simconnect) -> bool:
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
                ("values", ctypes.c_double * len(READ)),
            ]

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
        remove_dispatch(self._sim, self._hook)
        self._hook = None
        self._plans.clear()
        self._asked.clear()
        self._ready = False
        self._sim = self._dll = self._handle = None

    # -- what the traffic asks --------------------------------------------

    def is_the_pilot(self, object_id) -> bool:
        if object_id is None:
            return True
        object_id = int(object_id)
        return object_id == 0 or (self.user_object_id is not None
                                  and object_id == int(self.user_object_id))

    def sync(self, wanted: dict[int, tuple[str, Systems]],
             now: float | None = None) -> None:
        """One call a tick: object id -> (key, what it should be). Objects
        no longer in it are forgotten: they have gone, or are somebody
        else's now."""
        if not self.available:
            return
        now = time.monotonic() if now is None else now
        try:
            for object_id in [o for o in self._plans if o not in wanted]:
                del self._plans[object_id]
            for object_id, (key, goal) in wanted.items():
                if self.is_the_pilot(object_id):
                    continue
                plan = self._plans.get(int(object_id))
                if plan is None:
                    plan = self._plans[int(object_id)] = Plan(int(object_id), key, goal)
                elif plan.goal != goal:
                    plan.goal = goal
                    plan.tries.clear()
                    plan.deaf.clear()
                plan.key = key
            self.tick(now)
        except Exception:
            log.info("the engines and lights could not be kept in step",
                     exc_info=True)
            self._refused = True

    def tick(self, now: float) -> None:
        for plan in list(self._plans.values()):
            if now - plan.asked_at >= READ_EVERY_S:
                self._ask(plan, now)
            fresh = plan.read_at is not None and now - plan.read_at <= STALE_S
            for name in FIELDS:
                want = getattr(plan.goal, name)
                if name in plan.deaf:
                    continue
                if fresh and getattr(plan.reading, name) == want:
                    plan.tries.pop(name, None)
                    continue
                if not fresh and plan.sent_for.get(name) == want:
                    continue            # sent; waiting to read whether it took
                settle = SETTLE_S.get(name, SETTLE_LIGHT_S)
                if (plan.sent_for.get(name) == want
                        and now - plan.sent_at.get(name, -1e9) < settle):
                    continue
                tries = plan.tries.get(name, 0)
                if tries >= TRIES:
                    plan.deaf.add(name)
                    log.warning("systems: %s's %s does not answer after %d "
                                "sends; left alone", plan.key or plan.object_id,
                                name, TRIES)
                    continue
                if self._send(plan, name, want):
                    plan.sent_at[name] = now
                    plan.sent_for[name] = want
                    plan.tries[name] = tries + 1

    def _send(self, plan: Plan, name: str, on: bool) -> bool:
        if self.is_the_pilot(plan.object_id):
            return False
        if name == "engines":
            event, value = self._events["engines_on" if on else "engines_off"], 0
        else:
            event, value = self._events[name], 1 if on else 0
        try:
            result = self._dll.TransmitClientEvent(
                self._handle, plan.object_id, event, value,
                _PRIORITY_HIGHEST, _EVENT_FLAG_GROUPID_IS_PRIORITY)
        except Exception:
            log.info("the simulator will not take the engine and light "
                     "events", exc_info=True)
            self._refused = True
            return False
        if result != 0:
            return False
        log.debug("systems: %s %s %s", plan.key or plan.object_id, name,
                  "on" if on else "off")
        return True

    def _ask(self, plan: Plan, now: float) -> None:
        self._next_request += 1
        request = self._next_request
        try:
            result = self._dll.RequestDataOnSimObject(
                self._handle, request, _DEF_READ, plan.object_id,
                _PERIOD_ONCE, 0, 0, 0, 0)
        except Exception:
            log.info("the simulator will not report an aeroplane's engines "
                     "and lights", exc_info=True)
            self._refused = True
            return
        if result == 0:
            self._asked[request] = plan.object_id
            plan.asked_at = now

    def on_reading(self, request_id: int, object_id: int,
                   values: list[float]) -> None:
        self._asked.pop(int(request_id), None)
        plan = self._plans.get(int(object_id))
        if plan is None:
            return
        plan.reading = read_as_systems(values)
        plan.read_at = plan.asked_at

    def plan(self, object_id: int | None) -> Plan | None:
        return None if object_id is None else self._plans.get(int(object_id))

    def describe(self) -> str:
        if not self.enabled:
            return "engines and lights: off"
        if not self.available:
            return "engines and lights: not connected"
        return f"engines and lights: {len(self._plans)} aeroplane(s)"


__all__ = ["SimSystems", "EVENTS", "READ", "read_as_systems"]
