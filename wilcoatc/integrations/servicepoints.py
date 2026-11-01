"""Where a model's doors are, read off the simulator once per model.

Every aeroplane of one model has the same doors, so the interactive points
(type, position, heading; see :mod:`wilcoatc.atc.servicelayout`) are read
off the first object drawn with a title and kept for the title: one
reading per model per session, not per aeroplane per tick. A model that
answers nothing, or only unused slots, is remembered as having no points,
and its vehicles go where the layout's tables put them.

With the points, how high the model's reference stands above the ground
(``PLANE ALT ABOVE GROUND``): a point's POSY is from that reference, so the
two together are how high each door is, which is what the stairs, the
caterer and the belt are raised to. Read on the same object at the same
moment, standing on its stand, so it is this model's own.
"""

from __future__ import annotations

import ctypes
import logging
import time

from ..atc.servicelayout import FEET, Point, points_from_reading
from .aitraffic import (SIMCONNECT_DATATYPE_FLOAT64, _Received,
                        install_dispatch, remove_dispatch)

log = logging.getLogger("wilcoatc.doors")

POINTS = 16
_FIELDS = ((b"TYPE", b"enum"), (b"POSX", b"feet"), (b"POSY", b"feet"),
           (b"POSZ", b"feet"), (b"HEADING", b"degrees"))
#: After the points: the reference's height above the ground.
_STANDING = (b"PLANE ALT ABOVE GROUND", b"feet")
#: A height outside this (metres) is not one standing on its wheels.
STANDING_M = (0.3, 15.0)
_DEF_POINTS = 4748
_REQ_BASE = 610000
_RECV_SIMOBJECT_DATA = 8
_PERIOD_ONCE = 1
#: How long a reading is waited for before the object is asked again, and
#: how many objects of one title are tried before it is taken to have none.
ANSWER_WITHIN_S = 5.0
TRIES = 3


class SimServicePoints:
    """The interactive points of every model on the apron, by title."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._sim = self._dll = self._handle = None
        self._hook = None
        self._ready = False
        self._by_title: dict[str, list[Point]] = {}
        self._standing: dict[str, float] = {}
        self._asked: dict[int, tuple[str, float]] = {}     # request -> title, when
        self._tries: dict[str, int] = {}
        self._next_request = _REQ_BASE

    @property
    def available(self) -> bool:
        return self.enabled and self._ready

    def attach(self, simconnect) -> bool:
        if not self.enabled or simconnect is None:
            return False
        dll = getattr(simconnect, "dll", None)
        handle = getattr(simconnect, "hSimConnect", None)
        if dll is None or handle is None or getattr(
                dll, "RequestDataOnSimObject", None) is None:
            return False
        try:
            for index in range(POINTS):
                for name, unit in _FIELDS:
                    if dll.AddToDataDefinition(
                            handle, _DEF_POINTS,
                            b"INTERACTIVE POINT " + name + b":%d" % index, unit,
                            SIMCONNECT_DATATYPE_FLOAT64, 0, -1) != 0:
                        raise OSError("SimConnect refused an interactive point")
            if dll.AddToDataDefinition(handle, _DEF_POINTS, *_STANDING,
                                       SIMCONNECT_DATATYPE_FLOAT64, 0, -1) != 0:
                raise OSError("SimConnect refused the height above the ground")
        except Exception:
            log.info("the simulator will not say where the doors are; the "
                     "vehicles go by the layout tables", exc_info=True)
            return False

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
                ("values", ctypes.c_double * (POINTS * len(_FIELDS) + 1)),
            ]

        def heard(data):
            if data.contents.dwID != _RECV_SIMOBJECT_DATA:
                return False
            reply = ctypes.cast(data, ctypes.POINTER(_Reply)).contents
            if reply.dwRequestID not in self._asked:
                return False
            self.on_reading(reply.dwRequestID, list(reply.values))
            return True

        self._hook = install_dispatch(simconnect, heard)
        if self._hook is None:
            return False
        self._sim, self._dll, self._handle = simconnect, dll, handle
        self._ready = True
        return True

    def detach(self) -> None:
        remove_dispatch(self._sim, self._hook)
        self._hook = None
        self._asked.clear()
        self._ready = False
        self._sim = self._dll = self._handle = None

    def points(self, title: str) -> list[Point] | None:
        """This model's points: a list (empty for none), or None while
        nobody has asked or the answer is not in yet."""
        return self._by_title.get(title)

    def standing(self, title: str) -> float | None:
        """How high this model's reference stands above the ground, metres,
        or None where it was not read or was not a height on the ground."""
        return self._standing.get(title)

    def ask(self, object_id: int, title: str, now: float | None = None) -> None:
        """Read this object's points if its model has not been read yet."""
        if not self.available or not title or not object_id:
            return
        if title in self._by_title:
            return
        now = time.monotonic() if now is None else now
        if any(t == title and now - at < ANSWER_WITHIN_S
               for t, at in self._asked.values()):
            return
        tries = self._tries.get(title, 0)
        if tries >= TRIES:
            self._by_title[title] = []
            log.info("doors: %r says nothing about its doors; its vehicles "
                     "go by the layout tables", title)
            return
        self._tries[title] = tries + 1
        self._next_request += 1
        request = self._next_request
        try:
            result = self._dll.RequestDataOnSimObject(
                self._handle, request, _DEF_POINTS, int(object_id),
                _PERIOD_ONCE, 0, 0, 0, 0)
        except Exception:
            log.debug("could not ask for a model's doors", exc_info=True)
            return
        if result == 0:
            self._asked[request] = (title, now)

    def on_reading(self, request_id: int, values: list[float]) -> None:
        asked = self._asked.pop(int(request_id), None)
        if asked is None:
            return
        title, _ = asked
        count = POINTS * len(_FIELDS)
        found = points_from_reading(values[:count])
        self._by_title[title] = found
        height = None
        if len(values) > count:
            height = values[count] * FEET
            if STANDING_M[0] <= height <= STANDING_M[1]:
                self._standing[title] = height
        # Right/up/forward and, where the height is known, how high each
        # point is off the apron: the numbers a geometry fault is found in.
        log.info("doors: %r has %d interactive point(s), its reference "
                 "%s above the ground: %s", title, len(found),
                 f"{height:.2f} m" if height is not None else "not read",
                 ", ".join(f"{p.index}:{p.kind}@{p.right:+.2f}/{p.up:+.2f}/"
                           f"{p.forward:+.2f} hdg {p.heading:.0f}"
                           + (f" ({height + p.up:.2f} m up)"
                              if title in self._standing else "")
                           for p in found))


__all__ = ["SimServicePoints", "POINTS"]
