"""One SimConnect call at a time, from whichever thread makes it.

SimConnect is not thread-safe, and this program talks to it from two
threads: the engine's tick makes its requests, and the wrapper's message
thread pumps the replies -- and, since the ground vehicles are driven every
frame (wilcoatc.integrations.groundvehicles), also writes their positions
from inside that pump. From that change on, the pipe to the simulator broke
every so often (0xC00000B0, STATUS_PIPE_DISCONNECTED, every call failing at
once, "Lost the simulator" in the panel): the log has thirty-six of them,
none before 14:13 on 14 October and every one after, both live drops with
vehicles driving. Two threads writing into one pipe interleave.

So every entry point of the session's library is wrapped in one re-entrant
lock: the wrapper's own CallDispatch takes it, and so does every call this
program makes, wherever it is made from. Re-entrant, because a reply is
handled inside CallDispatch, on the thread that holds the lock, and may
itself call SimConnect (a vehicle's position from the frame event).
"""

from __future__ import annotations

import functools
import threading

#: The one lock every SimConnect call on the program's session takes.
LOCK = threading.RLock()


def locked(function):
    """``function``, called only while holding :data:`LOCK`."""
    if getattr(function, "_wilco_locked", False):
        return function

    @functools.wraps(function)
    def call(*args, **kwargs):
        with LOCK:
            return function(*args, **kwargs)

    call._wilco_locked = True
    return call


def serialize(dll) -> int:
    """Wrap every SimConnect entry point on the wrapper's library object, in
    place. Returns how many were wrapped. The ctypes functions are told
    apart by having ``argtypes``; data (the event enums, ``DispatchProc``)
    is left alone."""
    wrapped = 0
    for name in dir(dll):
        if name.startswith("_") or name == "DispatchProc":
            continue
        try:
            value = getattr(dll, name)
        except Exception:
            continue
        if callable(value) and hasattr(value, "argtypes"):
            try:
                setattr(dll, name, locked(value))
                wrapped += 1
            except Exception:
                continue
    return wrapped


__all__ = ["LOCK", "locked", "serialize"]
