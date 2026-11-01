"""Game controllers: which are plugged in, and which button is held.

Push-to-talk on a yoke button is read through SDL (pygame-ce). It used to be
read from whichever thread asked: the settings request rebuilt the list by
restarting SDL's joystick subsystem, push-to-talk pumped the event queue from
its own thread, and the button learner from a third. On Windows SDL hears of
a device arriving through a message window that only the thread that made it
can pump, so a yoke plugged in after the program started was never found --
and while push-to-talk held a handle the restart was skipped outright
(``JoystickPushToTalk._in_use``), because restarting invalidated that handle.
A user with a Honeycomb Alpha and Bravo saw both halves of that.

Here one thread owns SDL (:class:`Controllers`). Everything else reads what
it last saw, or hands it a job to run. A restart is harmless now, since that
thread reopens every handle itself, so a rescan is always allowed.

A controller is known by vendor and product (:func:`vendor_product`, read out
of SDL's GUID), not by its place in SDL's list, which moves whenever a device
is plugged in ahead of it. The name, then the old index, are the fallbacks.

:func:`build_report` is for the yoke on somebody else's desk: every HID
device Windows sees (through ``hid.dll``, no new dependency), what SDL lists
under each of the hints that change how it looks on Windows -- each in a
process of its own, because SDL reads its hints once -- and a capture of the
buttons pressed. One text file to attach to an issue.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import queue
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

#: Why the list is empty. The two need different things done about them.
MISSING = "missing"     # pygame-ce is not in this installation
NONE = "none"           # it is, and nothing is plugged in

#: Controllers whose report would otherwise be a guess: the vendor and
#: product, the name and the buttons the maker publishes (None: not known).
KNOWN: dict[tuple[int, int], tuple[str, int | None]] = {
    (0x294B, 0x1900): ("Honeycomb Alpha Flight Controls", 35),
    (0x294B, 0x1901): ("Honeycomb Bravo Throttle Quadrant", None),
}


@dataclass(frozen=True)
class Controller:
    """One connected game controller, as push-to-talk names it."""

    id: str             # "294b:1900", "294b:1900#2" for a second one, or the GUID
    name: str
    guid: str
    vendor: int
    product: int
    buttons: int
    index: int          # SDL's device index now; moves, so never stored alone
    instance: int = -1


class ControllerMissing(RuntimeError):
    """The controller push-to-talk is set to is not connected."""

    def __init__(self, wanted: str, connected: int):
        self.wanted = wanted
        self.connected = connected
        super().__init__(f"{wanted} is not connected "
                         f"({connected} controller(s) connected)")


class ControllersUnavailable(RuntimeError):
    """Controllers cannot be read at all here; ``reason`` says why."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def vendor_product(guid: str) -> tuple[int, int]:
    """The USB vendor and product inside an SDL 2 GUID, or (0, 0).

    SDL builds a device's GUID from its bus, then the vendor and the product,
    each little-endian and followed by two zero bytes. A GUID that is not
    shaped like that (an XInput slot, a virtual device) carries neither.
    """
    try:
        raw = bytes.fromhex(guid or "")
    except ValueError:
        return 0, 0
    if len(raw) != 16 or raw[6:8] != b"\0\0" or raw[10:12] != b"\0\0":
        return 0, 0
    vendor = int.from_bytes(raw[4:6], "little")
    product = int.from_bytes(raw[8:10], "little")
    if not vendor:
        return 0, 0
    return vendor, product


def import_pygame():
    """pygame-ce, without its banner and without a window.

    SDL refreshes button state only while its event queue is pumped, and it
    refuses to pump until the video subsystem is up; the dummy driver
    satisfies it without opening anything. An explicit setting is left alone.
    """
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    import pygame

    return pygame


class Controllers:
    """The one owner of SDL's joystick subsystem."""

    # How often the thread looks: often while a button is watched, which is
    # push-to-talk's latency, and slowly otherwise, which is still quick
    # enough for a yoke being plugged in to appear while Settings is open.
    WATCHING_HZ = 60.0
    IDLE_HZ = 5.0

    def __init__(self, importer: Callable = import_pygame,
                 threaded: bool = True):
        self._importer = importer
        self.threaded = threaded
        self._sdl = None
        self._reason = ""
        self._sticks: dict[int, object] = {}   # instance -> SDL handle
        self._order: list[int] = []            # instances in SDL's order
        self._controllers: list[Controller] = []
        self._buttons: dict[str, tuple[bool, ...]] = {}
        self._watched: dict[str, int] = {}
        self._all = 0
        self._jobs: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._stepped = threading.Condition()
        self._steps = 0
        self._started = False
        self._restarted_at = 0.0
        # When each device was last opened, by SDL instance: (monotonic
        # time, step). See SETTLE_S.
        self._opened: dict[int, tuple[float, int]] = {}
        #: Called at the start of every step; for tests, which press buttons.
        self.on_step: Callable[[], None] | None = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        if not self.threaded:
            self._open()
            return
        ready = threading.Event()

        def run():
            self._open()
            ready.set()
            while not self._stop.is_set():
                self.step()
                busy = self._watched or self._all
                time.sleep(1.0 / (self.WATCHING_HZ if busy else self.IDLE_HZ))

        self._thread = threading.Thread(target=run, name="WilcoATC-controllers",
                                        daemon=True)
        self._thread.start()
        ready.wait(10.0)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _open(self) -> None:
        try:
            self._sdl = self._importer()
        except ImportError:
            self._reason = MISSING
            return
        except Exception as exc:                       # pragma: no cover
            self._reason = str(exc)
            return
        sdl = self._sdl
        try:
            if not sdl.display.get_init():
                sdl.display.init()
        except Exception:
            # No video subsystem: pumping may still work.
            pass
        try:
            sdl.joystick.init()
            self._reopen_all()
        except Exception as exc:
            self._reason = str(exc)

    # ------------------------------------------------------- on the thread

    def step(self) -> None:
        """One look: jobs, then arrivals and departures, then the buttons."""
        if self.on_step is not None:
            self.on_step()
        while True:
            try:
                job, done = self._jobs.get_nowait()
            except queue.Empty:
                break
            try:
                done.set_result(job())
            except Exception as exc:
                done.set_exception(exc)
        sdl = self._sdl
        if sdl is not None:
            changed = self._drain(sdl)
            if changed:
                self._publish()
            self._read_buttons()
        with self._stepped:
            self._steps += 1
            self._stepped.notify_all()

    def _drain(self, sdl) -> bool:
        try:
            events = sdl.event.get()
        except Exception:
            try:
                sdl.event.pump()
            except Exception:
                pass
            return False
        added = getattr(sdl, "JOYDEVICEADDED", None)
        removed = getattr(sdl, "JOYDEVICEREMOVED", None)
        changed = False
        for event in events:
            if event.type == added:
                changed |= self._open_device(getattr(event, "device_index", -1))
            elif event.type == removed:
                instance = getattr(event, "instance_id", None)
                if instance in self._sticks:
                    del self._sticks[instance]
                    self._order.remove(instance)
                    changed = True
        return changed

    def _open_device(self, index: int) -> bool:
        try:
            stick = self._sdl.joystick.Joystick(index)
            instance = stick.get_instance_id()
        except Exception:
            return False
        if instance in self._sticks:
            return False
        self._sticks[instance] = stick
        self._order.append(instance)
        self._opened[instance] = (time.monotonic(), self._steps)
        return True

    def _reopen_all(self) -> None:
        self._sticks, self._order = {}, []
        for index in range(self._sdl.joystick.get_count()):
            self._open_device(index)
        self._publish()

    def _restart(self) -> None:
        """Read the device list again from scratch. Every handle is reopened
        here, on this thread, so nothing holding a controller id notices."""
        sdl = self._sdl
        if sdl is None:
            return
        try:
            sdl.joystick.quit()
        except Exception:
            # SDL's DirectInput backend can refuse to restart without a window
            # handle; looking again without restarting is never worse.
            pass
        try:
            sdl.joystick.init()
            self._reopen_all()
            self._reason = ""
        except Exception as exc:
            self._reason = str(exc)

    def _publish(self) -> None:
        found, counts = [], {}
        for index, instance in enumerate(self._order):
            stick = self._sticks[instance]
            try:
                name = stick.get_name()
                guid = stick.get_guid()
                buttons = stick.get_numbuttons()
            except Exception:
                continue
            vendor, product = vendor_product(guid)
            base = f"{vendor:04x}:{product:04x}" if vendor else guid
            counts[base] = counts.get(base, 0) + 1
            cid = base if counts[base] == 1 else f"{base}#{counts[base]}"
            found.append(Controller(cid, name, guid, vendor, product, buttons,
                                    index, instance))
        self._controllers = found

    def _read_buttons(self) -> None:
        wanted = {c.id: c for c in self._controllers
                  if self._all or c.id in self._watched}
        states = {}
        for cid, controller in wanted.items():
            stick = self._sticks.get(controller.instance)
            if stick is None:
                continue
            try:
                states[cid] = tuple(bool(stick.get_button(b))
                                    for b in range(controller.buttons))
            except Exception:
                # Pulled out between the event and this look; the event
                # removing it is on its way.
                continue
        self._buttons = states

    # ----------------------------------------------------- from any thread

    def _call(self, job: Callable, timeout: float = 5.0):
        if not self.threaded or self._thread is None:
            return job()
        if threading.current_thread() is self._thread:
            return job()
        done: Future = Future()
        self._jobs.put((job, done))
        return done.result(timeout=timeout)

    def list(self, rescan: bool | str = False
             ) -> tuple[list[Controller], str]:
        """The connected controllers, and why there are none when there are
        none (:data:`MISSING`, :data:`NONE`, or SDL's own error).

        Arrivals and departures are picked up on every step anyway;
        ``rescan`` also restarts the subsystem, for the device whose arrival
        SDL never announced -- ``"auto"`` only when one is due.
        """
        self.start()
        if rescan == "auto":
            rescan = self._restart_due()
        if rescan and self._sdl is not None:
            try:
                self._call(self._restart)
                self._restarted_at = time.monotonic()
            except Exception as exc:
                log.debug("controller rescan failed: %s", exc)
        found = list(self._controllers)
        if found:
            return found, ""
        return [], self._reason or NONE

    # Settings asks every two seconds, for a controller SDL never announced.
    # A restart costs about 365 ms on SDL 2.32 (measured, one controller),
    # and worse: after it SDL reads every button as up until the device
    # sends its next report, so a switch held on at rest comes back as a
    # release and then a press. Run during "Press a button", that press won:
    # a Honeycomb Alpha's learnt button went to the Bravo's detent switch and
    # the radio keyed itself (tests/test_controllers.py, TestTheRegression).
    # So never while a button is being learnt or read; arrivals SDL announces
    # are still seen at the next step.
    RESTART_EVERY_S = 2.0
    RESTART_EVERY_WATCHED_S = 10.0     # kept for callers; unused while watched

    def _restart_due(self) -> bool:
        if self._watched or self._all:
            return False
        return time.monotonic() - self._restarted_at >= self.RESTART_EVERY_S

    # How long after a device is opened its buttons are not believed: until
    # its first report SDL reads them all as up. A button seen held inside
    # this is part of what was already held, never a press.
    SETTLE_S = 0.25
    SETTLE_STEPS = 3

    def _settling(self, controller_id: str) -> bool:
        for c in self._controllers:
            if c.id == controller_id:
                at, step = self._opened.get(c.instance, (0.0, -10**9))
                return (time.monotonic() - at < self.SETTLE_S
                        or self._steps - step < self.SETTLE_STEPS)
        return False

    def find(self, controller_id: str = "", name: str = "",
             index: int | None = None) -> Controller | None:
        """The learnt controller and no other: by its id, or -- for a file
        written before ids, with none -- by the index it was saved with, as
        before. The name is for showing; matched, it moved push-to-talk to
        another device that shared it, and a missing controller must leave
        push-to-talk off and say so rather than pick a stand-in."""
        found = self._controllers
        if controller_id:
            return next((c for c in found if c.id == controller_id), None)
        if index is not None:
            return next((c for c in found if c.index == index), None)
        return None

    def watch(self, controller_id: str) -> None:
        self._watched[controller_id] = self._watched.get(controller_id, 0) + 1

    def unwatch(self, controller_id: str) -> None:
        left = self._watched.get(controller_id, 0) - 1
        if left > 0:
            self._watched[controller_id] = left
        else:
            self._watched.pop(controller_id, None)

    def pressed(self, controller_id: str, button: int) -> bool | None:
        """Whether a button is held; None when the controller is not here."""
        if not any(c.id == controller_id for c in self._controllers):
            return None
        states = self._buttons.get(controller_id, ())
        return bool(states[button]) if 0 <= button < len(states) else False

    def _wait_steps(self, count: int, timeout: float = 2.0) -> None:
        if not self.threaded or self._thread is None:
            for _ in range(count):
                self.step()
                time.sleep(0.01)
            return
        with self._stepped:
            target = self._steps + count
            self._stepped.wait_for(lambda: self._steps >= target, timeout)

    def learn(self, timeout_s: float = 8.0
              ) -> tuple[Controller, int] | None:
        """Wait for a button to be pressed on any controller: which, where.

        What is held when this starts is not the answer -- a latching switch
        or a trigger rested on would otherwise win at once -- and that is
        read only after a few looks, because until SDL's queue has moved
        every button reads as up.
        """
        self.list(rescan="auto")
        if not self._controllers:
            return None
        self._all += 1
        try:
            self._wait_steps(self.SETTLE_STEPS)
            held = {(cid, b) for cid, states in self._buttons.items()
                    for b, down in enumerate(states) if down}
            deadline = time.monotonic() + max(0.2, float(timeout_s))
            while time.monotonic() < deadline:
                self._wait_steps(1)
                for cid, states in self._buttons.items():
                    settling = self._settling(cid)
                    for b, down in enumerate(states):
                        if not down:
                            if not settling:
                                held.discard((cid, b))
                        elif (cid, b) not in held:
                            if settling:
                                # Read held as the device starts reporting:
                                # a switch at rest, not the pilot.
                                held.add((cid, b))
                                continue
                            controller = self.find(controller_id=cid)
                            if controller is not None:
                                return controller, b
                if self.threaded:
                    time.sleep(0.01)
            return None
        finally:
            self._all -= 1

    def capture(self, seconds: float = 10.0
                ) -> list[tuple[float, str, str, int, bool]]:
        """Every button that changed, for the report: (seconds in, id,
        name, button, down)."""
        self.list(rescan="auto")
        out: list[tuple[float, str, str, int, bool]] = []
        self._all += 1
        try:
            self._wait_steps(3)
            last = dict(self._buttons)
            names = {c.id: c.name for c in self._controllers}
            start = time.monotonic()
            while time.monotonic() - start < seconds:
                self._wait_steps(1)
                now = dict(self._buttons)
                for cid, states in now.items():
                    before = last.get(cid, ())
                    for b, down in enumerate(states):
                        was = before[b] if b < len(before) else False
                        if down != was:
                            out.append((round(time.monotonic() - start, 2),
                                        cid, names.get(cid, cid), b, down))
                last = now
                if self.threaded:
                    time.sleep(0.01)
        finally:
            self._all -= 1
        return out


_shared: Controllers | None = None
_shared_lock = threading.Lock()


def shared() -> Controllers:
    """The program's one hub, started on first use."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = Controllers()
        hub = _shared
    hub.start()
    return hub


class ButtonWatch:
    """One button on one controller, as press and release.

    Unplugged while held, the button is released -- a microphone left keyed
    by a cable is the worst way for this to fail -- and ``on_lost`` is told,
    once; plugged back in, ``on_back``.
    """

    def __init__(self, hub: Controllers, *, controller_id: str = "",
                 name: str = "", index: int | None = None, button: int = 0,
                 on_press: Callable[[], None] | None = None,
                 on_release: Callable[[], None] | None = None,
                 on_lost: Callable[[Controller], None] | None = None,
                 on_back: Callable[[Controller], None] | None = None,
                 poll_hz: float = 60.0):
        self.hub = hub
        self.controller_id = controller_id
        self.name = name
        self.index = index
        self.button = button
        self.on_press, self.on_release = on_press, on_release
        self.on_lost, self.on_back = on_lost, on_back
        self.poll_interval = 1.0 / max(1.0, poll_hz)
        self.controller: Controller | None = None
        self._down = False
        self._lost = False
        # A press is a release seen first, then a press. Held at the first
        # read -- a switch on at rest -- is not a press, or the radio keys
        # itself the moment the program starts.
        self._armed = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        found, reason = self.hub.list()
        if not found and reason not in ("", NONE):
            raise ControllersUnavailable(reason)
        controller = self.hub.find(self.controller_id, self.name, self.index)
        if controller is None:
            wanted = self.name or self.controller_id or f"controller {self.index}"
            raise ControllerMissing(wanted, len(found))
        self.controller = controller
        self.hub.watch(controller.id)
        if not self.hub.threaded:
            return

        def loop():
            while not self._stop.is_set():
                # Unguarded, a failure here would end the thread and the
                # button would quietly stop working for the rest of the flight.
                try:
                    self.poll()
                except Exception:
                    log.debug("push-to-talk poll failed", exc_info=True)
                time.sleep(self.poll_interval)

        self._thread = threading.Thread(target=loop, daemon=True,
                                        name="WilcoATC-ptt-button")
        self._thread.start()

    def poll(self) -> None:
        controller = self.controller
        if controller is None:
            return
        state = self.hub.pressed(controller.id, self.button)
        if state is None:
            self._armed = False
            if self._down:
                self._down = False
                if self.on_release:
                    self.on_release()
            if not self._lost:
                self._lost = True
                if self.on_lost:
                    self.on_lost(controller)
            return
        if self._lost:
            self._lost = False
            if self.on_back:
                self.on_back(controller)
        if self.hub._settling(controller.id):
            return
        if not state:
            self._armed = True
        if state and not self._down and not self._armed:
            return
        if state and not self._down:
            self._down = True
            if self.on_press:
                self.on_press()
        elif not state and self._down:
            self._down = False
            if self.on_release:
                self.on_release()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self.controller is not None:
            self.hub.unwatch(self.controller.id)


# --------------------------------------------------------------------------
# what Windows sees, without SDL
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class HidDevice:
    vendor: int
    product: int
    manufacturer: str
    product_name: str
    usage_page: int
    usage: int
    buttons: int
    path: str = ""

    @property
    def game_controller(self) -> bool:
        """Generic desktop joystick (4), gamepad (5) or multi-axis (8)."""
        return self.usage_page == 1 and self.usage in (4, 5, 8)


def hid_devices() -> list[HidDevice]:
    """Every HID interface Windows has present, through ``hid.dll``.

    ctypes and the two system libraries every Windows has, rather than a
    package: this is a diagnostic, and it has to work in the build as
    shipped. Empty anywhere else.
    """
    if sys.platform != "win32":
        return []
    try:
        return _hid_devices_windows()
    except Exception as exc:                            # pragma: no cover
        log.debug("HID enumeration failed: %s", exc)
        return []


def _hid_devices_windows() -> list[HidDevice]:
    import ctypes
    import re
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    class InterfaceData(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID),
                    ("Flags", wintypes.DWORD),
                    ("Reserved", ctypes.c_void_p)]

    class Attributes(ctypes.Structure):
        _fields_ = [("Size", ctypes.c_ulong), ("VendorID", ctypes.c_ushort),
                    ("ProductID", ctypes.c_ushort),
                    ("VersionNumber", ctypes.c_ushort)]

    class Caps(ctypes.Structure):
        _fields_ = [("Usage", ctypes.c_ushort), ("UsagePage", ctypes.c_ushort),
                    ("InputReportByteLength", ctypes.c_ushort),
                    ("OutputReportByteLength", ctypes.c_ushort),
                    ("FeatureReportByteLength", ctypes.c_ushort),
                    ("Reserved", ctypes.c_ushort * 17),
                    ("NumberLinkCollectionNodes", ctypes.c_ushort),
                    ("NumberInputButtonCaps", ctypes.c_ushort),
                    ("NumberInputValueCaps", ctypes.c_ushort),
                    ("NumberInputDataIndices", ctypes.c_ushort),
                    ("NumberOutputButtonCaps", ctypes.c_ushort),
                    ("NumberOutputValueCaps", ctypes.c_ushort),
                    ("NumberOutputDataIndices", ctypes.c_ushort),
                    ("NumberFeatureButtonCaps", ctypes.c_ushort),
                    ("NumberFeatureValueCaps", ctypes.c_ushort),
                    ("NumberFeatureDataIndices", ctypes.c_ushort)]

    class ButtonCaps(ctypes.Structure):
        # HIDP_BUTTON_CAPS, 72 bytes; the union read as its Range member.
        _fields_ = [("UsagePage", ctypes.c_ushort), ("ReportID", ctypes.c_ubyte),
                    ("IsAlias", ctypes.c_ubyte), ("BitField", ctypes.c_ushort),
                    ("LinkCollection", ctypes.c_ushort),
                    ("LinkUsage", ctypes.c_ushort),
                    ("LinkUsagePage", ctypes.c_ushort),
                    ("IsRange", ctypes.c_ubyte), ("IsStringRange", ctypes.c_ubyte),
                    ("IsDesignatorRange", ctypes.c_ubyte),
                    ("IsAbsolute", ctypes.c_ubyte),
                    ("ReportCount", ctypes.c_ushort),
                    ("Reserved2", ctypes.c_ushort),
                    ("Reserved", ctypes.c_ulong * 9),
                    ("UsageMin", ctypes.c_ushort), ("UsageMax", ctypes.c_ushort),
                    ("StringMin", ctypes.c_ushort), ("StringMax", ctypes.c_ushort),
                    ("DesignatorMin", ctypes.c_ushort),
                    ("DesignatorMax", ctypes.c_ushort),
                    ("DataIndexMin", ctypes.c_ushort),
                    ("DataIndexMax", ctypes.c_ushort)]

    hid = ctypes.WinDLL("hid")
    setupapi = ctypes.WinDLL("setupapi")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), ctypes.c_wchar_p,
                                              ctypes.c_void_p, wintypes.DWORD]
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.DWORD,
        ctypes.POINTER(InterfaceData)]
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(InterfaceData), ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, wintypes.DWORD,
                                     wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.DWORD, wintypes.DWORD,
                                     ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    for name in ("HidD_GetAttributes", "HidD_GetProductString",
                 "HidD_GetManufacturerString", "HidD_GetPreparsedData",
                 "HidD_FreePreparsedData"):
        getattr(hid, name).restype = ctypes.c_ubyte
    hid.HidD_GetAttributes.argtypes = [ctypes.c_void_p, ctypes.POINTER(Attributes)]
    hid.HidD_GetProductString.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_ulong]
    hid.HidD_GetManufacturerString.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                               ctypes.c_ulong]
    hid.HidD_GetPreparsedData.argtypes = [ctypes.c_void_p,
                                          ctypes.POINTER(ctypes.c_void_p)]
    hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
    hid.HidP_GetCaps.restype = ctypes.c_long
    hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(Caps)]
    hid.HidP_GetButtonCaps.restype = ctypes.c_long
    hid.HidP_GetButtonCaps.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_ushort),
                                       ctypes.c_void_p]

    hid_guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(hid_guid))
    DIGCF_PRESENT, DIGCF_DEVICEINTERFACE = 0x2, 0x10
    info = setupapi.SetupDiGetClassDevsW(ctypes.byref(hid_guid), None, None,
                                         DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if not info or info == ctypes.c_void_p(-1).value:
        return []
    out: list[HidDevice] = []
    detail_size = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
    try:
        i = 0
        while True:
            data = InterfaceData()
            data.cbSize = ctypes.sizeof(InterfaceData)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                    info, None, ctypes.byref(hid_guid), i, ctypes.byref(data)):
                break
            i += 1
            needed = wintypes.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                info, ctypes.byref(data), None, 0, ctypes.byref(needed), None)
            buf = ctypes.create_string_buffer(max(needed.value, detail_size + 4))
            ctypes.c_ulong.from_buffer(buf).value = detail_size
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                    info, ctypes.byref(data), buf, needed.value, None, None):
                continue
            path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
            out.append(_describe_hid(path, hid, kernel32, Attributes, Caps,
                                     ButtonCaps, re))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(info)
    return out


def _describe_hid(path, hid, kernel32, Attributes, Caps, ButtonCaps, re):
    import ctypes

    match = re.search(r"vid_([0-9a-f]{4})&pid_([0-9a-f]{4})", path, re.I)
    vendor = int(match.group(1), 16) if match else 0
    product = int(match.group(2), 16) if match else 0
    manufacturer = name = ""
    page = usage = buttons = 0
    # No access asked for: enough for attributes, strings and the report
    # descriptor, and allowed on devices another program holds open.
    handle = kernel32.CreateFileW(path, 0, 3, None, 3, 0, None)
    if handle and handle != ctypes.c_void_p(-1).value:
        try:
            attributes = Attributes()
            attributes.Size = ctypes.sizeof(Attributes)
            if hid.HidD_GetAttributes(handle, ctypes.byref(attributes)):
                vendor, product = attributes.VendorID, attributes.ProductID
            text = ctypes.create_unicode_buffer(256)
            if hid.HidD_GetProductString(handle, text, ctypes.sizeof(text)):
                name = text.value
            text = ctypes.create_unicode_buffer(256)
            if hid.HidD_GetManufacturerString(handle, text, ctypes.sizeof(text)):
                manufacturer = text.value
            preparsed = ctypes.c_void_p()
            if hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
                try:
                    caps = Caps()
                    if hid.HidP_GetCaps(preparsed, ctypes.byref(caps)) == 0x110000:
                        page, usage = caps.UsagePage, caps.Usage
                        count = ctypes.c_ushort(caps.NumberInputButtonCaps)
                        if count.value:
                            array = (ButtonCaps * count.value)()
                            if hid.HidP_GetButtonCaps(0, array, ctypes.byref(count),
                                                      preparsed) == 0x110000:
                                for cap in array[:count.value]:
                                    if cap.UsagePage != 0x09:
                                        continue
                                    buttons += (cap.UsageMax - cap.UsageMin + 1
                                                if cap.IsRange else 1)
                finally:
                    hid.HidD_FreePreparsedData(preparsed)
        finally:
            kernel32.CloseHandle(handle)
    return HidDevice(vendor, product, manufacturer, name, page, usage,
                     buttons, path)


# --------------------------------------------------------------------------
# SDL under each hint, one process each
# --------------------------------------------------------------------------

#: The configurations SDL is asked under. Its hints are read once, when the
#: joystick subsystem first starts, so each is tried in a process of its own.
PROBES: dict[str, dict[str, str]] = {
    "default": {"SDL_VIDEODRIVER": "dummy"},
    "RAWINPUT": {"SDL_VIDEODRIVER": "dummy", "SDL_JOYSTICK_RAWINPUT": "1"},
    "THREAD": {"SDL_VIDEODRIVER": "dummy", "SDL_JOYSTICK_THREAD": "1"},
    # DirectInput wants a window handle; a hidden one gives it one.
    "hidden window": {"SDL_VIDEODRIVER": "windows", "_HIDDEN": "1"},
}


def probe(name: str) -> dict:
    """What SDL lists under one configuration, in this (fresh) process."""
    settings = PROBES[name]
    for key, value in settings.items():
        if not key.startswith("_"):
            os.environ[key] = value
    os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
    started = time.perf_counter()
    try:
        import pygame

        if settings.get("_HIDDEN"):
            pygame.display.init()
            pygame.display.set_mode((1, 1), pygame.HIDDEN)
        else:
            try:
                pygame.display.init()
            except Exception:
                pass
        pygame.joystick.init()
        for _ in range(5):
            pygame.event.pump()
            time.sleep(0.05)
        devices = []
        for i in range(pygame.joystick.get_count()):
            stick = pygame.joystick.Joystick(i)
            devices.append({"name": stick.get_name(), "guid": stick.get_guid(),
                            "buttons": stick.get_numbuttons(),
                            "axes": stick.get_numaxes(),
                            "hats": stick.get_numhats()})
        return {"ok": True, "devices": devices,
                "pygame": pygame.version.ver,
                "sdl": ".".join(str(v) for v in pygame.get_sdl_version()),
                "ms": round((time.perf_counter() - started) * 1000)}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


def _self_command() -> list[str]:
    if getattr(sys, "frozen", False):
        console = Path(sys.executable).with_name("WilcoATC-console.exe")
        return [str(console if console.exists() else sys.executable)]
    return [sys.executable, "-m", "wilcoatc"]


def run_probes(timeout_s: float = 30.0) -> dict[str, dict]:
    results = {}
    flags = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW
    for name in PROBES:
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "probe.json"
            try:
                subprocess.run(_self_command() + ["controllers", "--probe", name,
                                                  "--out", str(out)],
                               timeout=timeout_s, creationflags=flags,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
                results[name] = json.loads(out.read_text(encoding="utf-8"))
            except Exception as exc:
                results[name] = {"ok": False, "error": repr(exc)}
    return results


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------

def report_folder() -> Path:
    from ..paths import log_dir

    return log_dir()


def _hex(vendor: int, product: int) -> str:
    return f"{vendor:04X}:{product:04X}"


def render_report(*, hid: list[HidDevice], probes: dict[str, dict],
                  presses: list, seconds: float, header: str = "") -> str:
    lines = ["WilcoATC controller report", "=" * 26]
    if header:
        lines += [header, ""]

    lines.append(f"Windows HID devices ({len(hid)})")
    lines.append("-" * 40)
    for device in sorted(hid, key=lambda d: (not d.game_controller,
                                             d.vendor, d.product)):
        known = KNOWN.get((device.vendor, device.product))
        note = ""
        if known:
            expected = f", {known[1]} buttons expected" if known[1] else ""
            note = f"   <- {known[0]}{expected}"
        kind = "game controller" if device.game_controller \
            else f"usage {device.usage_page:#04x}/{device.usage:#04x}"
        lines.append(f"{_hex(device.vendor, device.product)}  {kind:<22} "
                     f"{device.buttons:>3} buttons  "
                     f"{device.manufacturer} / {device.product_name}{note}")
    lines.append("")

    lines.append("SDL (pygame-ce) under each configuration")
    lines.append("-" * 40)
    sdl_seen: dict[tuple[int, int], list[str]] = {}
    for name in PROBES:
        result = probes.get(name)
        if result is None:
            lines.append(f"[{name}] not run")
            continue
        if not result.get("ok"):
            lines.append(f"[{name}] failed: {result.get('error')}")
            continue
        devices = result.get("devices", [])
        version = (f" (pygame {result['pygame']}, SDL {result['sdl']})"
                   if result.get("pygame") else "")
        lines.append(f"[{name}] {len(devices)} device(s){version}")
        for d in devices:
            vendor, product = vendor_product(d.get("guid", ""))
            sdl_seen.setdefault((vendor, product), []).append(
                f"{name}: {d.get('buttons')} buttons")
            lines.append(f"    {d.get('name')}  {_hex(vendor, product)}  "
                         f"guid {d.get('guid')}  {d.get('buttons')} buttons, "
                         f"{d.get('axes')} axes, {d.get('hats')} hats")
    lines.append("")

    lines.append("Known controllers")
    lines.append("-" * 40)
    windows = {(d.vendor, d.product): d for d in hid}
    for (vendor, product), (name, buttons) in KNOWN.items():
        where = windows.get((vendor, product))
        if where is None:
            lines.append(f"{name} ({_hex(vendor, product)}): not seen by Windows")
            continue
        sdl = sdl_seen.get((vendor, product))
        lines.append(f"{name} ({_hex(vendor, product)}): seen by Windows, "
                     f"{where.buttons} buttons in its HID report; "
                     + ("seen by SDL under " + "; ".join(sdl) if sdl
                        else "not seen by SDL under any configuration"))
    lines.append("")

    lines.append(f"Buttons pressed in {seconds:g} s")
    lines.append("-" * 40)
    if not presses:
        lines.append("(none)")
    for at, cid, name, button, down in presses:
        lines.append(f"{at:6.2f} s  {cid}  {name}  button {button} "
                     f"{'down' if down else 'up'}")
    return "\n".join(lines) + "\n"


def build_report(seconds: float = 10.0, hub: Controllers | None = None,
                 on_capture: Callable[[], None] | None = None) -> str:
    from .. import __version__
    from ..paths import FROZEN

    header = (f"WilcoATC {__version__} ({'release build' if FROZEN else 'checkout'}), "
              f"Python {platform.python_version()}, {platform.platform()}\n"
              f"Written {time.strftime('%Y-%m-%d %H:%M:%S')}")
    hid = hid_devices()
    probes = run_probes()
    if on_capture is not None:
        on_capture()
    presses = (hub or shared()).capture(seconds)
    return render_report(hid=hid, probes=probes, presses=presses,
                         seconds=seconds, header=header)


def write_report(seconds: float = 10.0, **kw) -> Path:
    text = build_report(seconds=seconds, **kw)
    folder = report_folder()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"controller-report-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    return path


__all__ = ["Controller", "ControllerMissing", "Controllers",
           "ControllersUnavailable", "ButtonWatch", "HidDevice", "KNOWN",
           "MISSING", "NONE", "PROBES", "build_report", "hid_devices",
           "probe", "render_report", "report_folder", "run_probes", "shared",
           "vendor_product", "write_report"]
