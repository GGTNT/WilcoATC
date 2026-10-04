"""What the ground handling is doing, and how it is asked for anything.

GSX Pro drives the tug, the jetway, the fuel truck, the caterers and the
stairs, and it publishes what it is doing in a set of local variables. That
matters to a controller for one reason: an aeroplane still connected to a tug
cannot taxi, and asking it to is the kind of instruction that makes the whole
thing feel scripted rather than watched.

Two ways in, in order of preference.

*The variables themselves.* GSX's ``FSDT_GSX_*`` local variables say exactly
where each service has got to. Reading a local variable needs a bridge -- the
simulator does not expose them over plain SimConnect -- and the usual one is
the MobiFlight WASM module, which many people already have installed for other
add-ons. If it is there, this uses it.

*What the aeroplane is doing.* Failing that, the ordinary simulator variables
still say a great deal: an aeroplane moving backwards on the ground with the
engines off is being pushed, whoever is pushing it. This works with GSX, with
the built-in pushback, and with anything else, and it needs nothing installed.

Both are optional. With neither, the radio behaves exactly as it did before.

Asking for something
--------------------

Watching is half of it. A controller who has just said "push-back approved"
and then leaves the pilot to go and find the tug themselves has broken the one
illusion this program exists for, so the approval starts the push -- and a
pilot who says "ground crew, request boarding" gets boarding, because by then
the aeroplane already has a voice interface to everything else.

GSX has no "start pushback" variable, and no "start boarding" one either.
What it has is its menu, and two variables that work it:
``FSDT_GSX_MENU_OPEN`` shows it and ``FSDT_GSX_MENU_CHOICE`` picks an entry by
number. The numbers are not fixed -- the menu is built from what is available
at that stand -- so guessing one is how an approval to push ends up ordering
catering. GSX also writes the menu it is showing to a text file, and that is
what is read here: the menu is opened, the file says what is on it, and the
entry that actually says what was asked for is the one chosen. Nothing is
guessed and nothing is chosen from a menu that was not read.

A menu is not one level deep
----------------------------

Asking for a push opens a second menu asking which way to leave the stand, and
GSX asks again, later, whether the flight deck is ready. So a request is not
one choice, it is a *walk*: a list of steps, each of which is a set of words
to look for in whatever menu is on screen at that point. A step that finds
nothing within a few seconds ends the walk rather than guessing, and a step
marked optional ends it successfully -- the push has been requested, and the
submenu that never appeared was a submenu this stand does not have.

The walk is driven from :meth:`GsxLink.poll`, which the situation loop is
already calling, because GSX draws each menu in its own time and nothing here
may block waiting for it.

That needs a bridge that can write, which in practice means MobiFlight. With
no such bridge -- or with a GSX that does not offer a push at this stand --
the simulator's own pushback is used instead, which is the built-in tug and is
also what GSX drives when it is set to take the default pushback over. There
is no such fallback for boarding or catering: those are GSX or nothing, and
saying so is better than pretending.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# The variables GSX publishes. Reading them needs a bridge; the names are the
# same whichever bridge it is.
LVARS: tuple[str, ...] = (
    # Whether GSX's own script engine is up. Everything else here is zero
    # until it is, and a zero that means "not started yet" reads exactly like
    # a zero that means "nothing available at this stand".
    "FSDT_GSX_COUATL_STARTED",
    "FSDT_GSX_BOARDING_STATE",
    "FSDT_GSX_DEBOARDING_STATE",
    "FSDT_GSX_REFUELING_STATE",
    "FSDT_GSX_CATERING_STATE",
    "FSDT_GSX_DEPARTURE_STATE",
    "FSDT_GSX_DEICING_STATE",
    "FSDT_GSX_JETWAY",
    "FSDT_GSX_STAIRS",
    "FSDT_GSX_NUMPASSENGERS_BOARDING_TOTAL",
    "FSDT_GSX_NUMPASSENGERS_DEBOARDING_TOTAL",
    "FSDT_GSX_BOARDING_CARGO_PERCENT",
    "FSDT_GSX_DEBOARDING_CARGO_PERCENT",
    # The hose, which is the difference between a fuel truck that has parked
    # next to the aeroplane and one that is actually attached to it.
    "FSDT_GSX_FUELHOSE_CONNECTED",
)

# The two variables that work GSX's menu. Written, not read: the menu is how
# a service is asked for, and asking is the only thing this program does to
# GSX rather than with it.
MENU_OPEN = "FSDT_GSX_MENU_OPEN"
MENU_CHOICE = "FSDT_GSX_MENU_CHOICE"

# How long GSX is given to draw each menu after it has been asked for. It is a
# file write behind a script engine, not a network call; six seconds is many
# times longer than it takes and still short enough that a pilot who has been
# told the push is approved has not started wondering.
MENU_WAIT_S = 6.0

# How long a menu has to have been sitting unchanged before it counts as the
# menu that is on screen rather than as the one that was.
#
# This exists because of the half-second after a choice. The file is rewritten
# by the simulator a moment later, and a walk that matched the stale copy in
# between would choose the same entry twice -- the second time into whatever
# menu had come up. So a choice is normally made only from a menu that has
# changed since the last one. The exception is a menu that has not changed
# because nothing needed to change: GSX was already showing it. After three
# seconds of a file that nobody has touched, what is in it is what is on
# screen, and waiting longer only means a pilot waiting longer.
MENU_SETTLED_S = 3.0

# How GSX numbers the progress of a service. The same six values are used for
# boarding, deboarding, refuelling, catering, de-icing and pushback.
NOT_AVAILABLE = 1
CALLABLE = 2
REQUESTED = 3
RUNNING = 4
COMPLETED = 5
FINISHED = 6

STATE_NAMES: dict[int, str] = {
    NOT_AVAILABLE: "not available",
    CALLABLE: "available",
    REQUESTED: "requested",
    RUNNING: "in progress",
    COMPLETED: "completed",
    FINISHED: "finished",
}


# --------------------------------------------------------------------------
# reading a menu
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One level of GSX's menu, and what to look for in it.

    ``want`` is matched anywhere in an entry and ``avoid`` vetoes it, because
    the same menu carries the entry that stops a service as well as the one
    that starts it -- "Cancel pushback" sits next to "Request pushback" and
    contains every word of it.

    ``required`` is what tells a submenu that never appeared from a service
    this stand has not got. The first step of every walk is required: nothing
    was asked for if it did not match. The steps after it are the ones GSX
    may or may not put up, and a walk that ends there has still done its job.
    """

    want: tuple[str, ...]
    avoid: tuple[str, ...] = ()
    required: bool = True


# The words that stop something, which are never the words that start it.
NOT_PUSHBACK_WORDS: tuple[str, ...] = ("cancel", "stop", "abort")
PUSHBACK_WORDS: tuple[str, ...] = ("pushback", "push back", "push-back")

# What to look for, per thing that can be asked for. The names are
# :mod:`wilcoatc.atc.interphone`'s, so a word heard on the interphone is a
# menu entry here with nothing in between to translate it.
#
# GSX's menu is written in English whatever the simulator's language is, which
# is why these are not translated and must not be.
SERVICES: dict[str, tuple[Step, ...]] = {
    "pushback": (
        Step(PUSHBACK_WORDS, NOT_PUSHBACK_WORDS),
        # Which way off the stand. GSX asks this at some stands and not at
        # others, and where it does ask, the entry that means "however this
        # gate normally does it" is the one to take: choosing a direction on
        # the pilot's behalf is a decision, and taking the default is not.
        Step(("straight", "default", "continue", "no preference"),
             ("cancel",), required=False),
    ),
    "stop_pushback": (
        Step(("cancel pushback", "stop pushback", "abort pushback",
              "cancel push back", "stop push back"),),
    ),
    # The second menu of a push, which GSX puts up once the tug is attached
    # and which is the pilot saying the brakes are off.
    "confirm": (
        Step(("yes", "ready", "confirm", "continue"),
             ("no", "not ready", "cancel", "wait", "abort")),
    ),
    "boarding": (
        Step(("boarding",), ("deboarding", "de-boarding", "cancel", "stop")),
    ),
    "deboarding": (
        Step(("deboarding", "de-boarding", "disembark"), ("cancel", "stop")),
    ),
    "refuel": (
        Step(("refuel", "refuelling", "refueling", "fuel"),
             ("cancel", "stop", "hose")),
    ),
    "catering": (
        Step(("catering",), ("cancel", "stop")),
    ),
    "jetway": (
        Step(("jetway", "jet way", "jetbridge", "jet bridge", "boarding bridge"),
             ("cancel",)),
    ),
    "stairs": (
        Step(("stairs", "steps"), ("cancel",)),
    ),
    "deice": (
        Step(("de-icing", "deicing", "de icing", "deice", "de-ice"),
             ("cancel", "stop")),
    ),
    "gpu": (
        Step(("ground power", "gpu", "external power"), ("cancel",)),
    ),
}


def entry_choice(entries, step: Step) -> int:
    """Which entry of this menu matches the step, or -1 if none does.

    Matched on the words rather than on a position. GSX builds its menu from
    what the stand offers, so the entry that was third at one gate is second
    at the next -- and the menu that has none of them in it at all is the one
    where choosing a number would call something else entirely.
    """
    for index, entry in enumerate(entries or ()):
        low = str(entry).lower()
        if not any(word in low for word in step.want):
            continue
        if any(word in low for word in step.avoid):
            continue
        return index
    return -1


def pushback_choice(entries) -> int:
    """Which entry starts a pushback, or -1 if this menu offers none."""
    return entry_choice(entries, SERVICES["pushback"][0])


def _state(value) -> int:
    try:
        found = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return found if 1 <= found <= 6 else 0


@dataclass
class GsxState:
    """Where the ground handling has got to.

    Every field defaults to the answer that changes nothing, so a state read
    from nothing at all is a state the controller can safely ignore.
    """

    available: bool = False
    source: str = ""                  # which way in produced this

    # Whether GSX's own scripts are running. False with ``available`` true
    # means the variables were read and GSX has not started, which is a
    # different thing from GSX not being installed.
    running: bool = False

    boarding: int = 0
    deboarding: int = 0
    refuelling: int = 0
    catering: int = 0
    pushback: int = 0
    deicing: int = 0

    jetway_connected: bool = False
    stairs_connected: bool = False
    fuel_hose: bool = False
    passengers: int = 0
    cargo_percent: float = 0.0
    at: float = field(default_factory=time.monotonic)

    # -- what any of it means to a controller -------------------------

    @property
    def being_pushed(self) -> bool:
        """On the tug right now, so not able to taxi."""
        return self.pushback in (REQUESTED, RUNNING)

    @property
    def pushback_done(self) -> bool:
        return self.pushback in (COMPLETED, FINISHED)

    @property
    def connected_to_the_stand(self) -> bool:
        """Something is physically attached to the aeroplane."""
        return self.jetway_connected or self.stairs_connected

    @property
    def being_serviced(self) -> bool:
        """A service is running that would stop the aeroplane moving."""
        return any(value == RUNNING for value in
                   (self.boarding, self.deboarding, self.refuelling,
                    self.catering, self.deicing))

    @property
    def can_taxi(self) -> bool:
        """Whether the aeroplane could actually move if it were told to."""
        if not self.available:
            return True                # nothing known, so nothing in the way
        return not (self.being_pushed or self.connected_to_the_stand
                    or self.being_serviced or self.fuel_hose)

    def why_not(self) -> str:
        """The reason it cannot move, in words a controller would use."""
        if self.being_pushed:
            return "still on the tug"
        if self.jetway_connected:
            return "still on the jetway"
        if self.stairs_connected:
            return "still on the stairs"
        if self.fuel_hose or self.refuelling == RUNNING:
            return "still refuelling"
        if self.boarding == RUNNING:
            return "still boarding"
        if self.deboarding == RUNNING:
            return "still deboarding"
        if self.catering == RUNNING:
            return "still catering"
        if self.deicing == RUNNING:
            return "still de-icing"
        return ""

    def describe(self) -> str:
        """One line for the console."""
        if not self.available:
            return "ground handling: nothing connected"
        parts = []
        for name, value in (("boarding", self.boarding),
                            ("deboarding", self.deboarding),
                            ("refuelling", self.refuelling),
                            ("catering", self.catering),
                            ("de-icing", self.deicing),
                            ("pushback", self.pushback)):
            if value in (REQUESTED, RUNNING, COMPLETED):
                parts.append(f"{name} {STATE_NAMES[value]}")
        if self.jetway_connected:
            parts.append("jetway connected")
        if self.stairs_connected:
            parts.append("stairs connected")
        return f"ground handling ({self.source}): " + (
            ", ".join(parts) if parts else "idle")


def interpret(lvars: dict, source: str = "GSX") -> GsxState:
    """Turn a bag of local variables into something a controller can use."""
    if not lvars:
        return GsxState()

    def number(name: str, default: float = 0.0) -> float:
        try:
            return float(lvars.get(name, default))
        except (TypeError, ValueError):
            return default

    return GsxState(
        available=True,
        source=source,
        running=number("FSDT_GSX_COUATL_STARTED") >= 1,
        boarding=_state(lvars.get("FSDT_GSX_BOARDING_STATE")),
        deboarding=_state(lvars.get("FSDT_GSX_DEBOARDING_STATE")),
        refuelling=_state(lvars.get("FSDT_GSX_REFUELING_STATE")),
        catering=_state(lvars.get("FSDT_GSX_CATERING_STATE")),
        pushback=_state(lvars.get("FSDT_GSX_DEPARTURE_STATE")),
        deicing=_state(lvars.get("FSDT_GSX_DEICING_STATE")),
        jetway_connected=number("FSDT_GSX_JETWAY") >= 4,
        stairs_connected=number("FSDT_GSX_STAIRS") >= 4,
        fuel_hose=number("FSDT_GSX_FUELHOSE_CONNECTED") >= 1,
        passengers=int(number("FSDT_GSX_NUMPASSENGERS_BOARDING_TOTAL")),
        cargo_percent=number("FSDT_GSX_BOARDING_CARGO_PERCENT"),
    )


def from_aircraft(state) -> GsxState:
    """What the aeroplane itself says, when no bridge is available.

    An aeroplane on the ground, rolling, with the engines off and the parking
    brake released is being pushed by somebody, and for the purpose of not
    clearing it to taxi it does not matter who. This is less precise than
    reading GSX directly -- it says nothing about boarding or the jetway -- and
    it needs nothing installed and works with any ground handling add-on.
    """
    if state is None or not getattr(state, "on_ground", False):
        return GsxState()

    speed = abs(float(getattr(state, "ground_speed_kt", 0.0) or 0.0))
    running = bool(getattr(state, "engines_running", False))
    parked = bool(getattr(state, "parking_brake", False))

    pushing = 0.4 < speed < 12.0 and not running and not parked
    return GsxState(
        available=pushing,
        source="the aeroplane",
        pushback=RUNNING if pushing else 0,
    )


def menu_file() -> Path | None:
    r"""Where GSX writes the menu it is currently showing.

    ``%APPDATA%\Virtuali\GSX\<sim>\menu``, one line per entry with the
    title first. Which ``<sim>`` folder is used has changed between releases,
    so the most recently written one wins rather than a name being pinned:
    a pilot who has both simulators installed has both folders, and the one
    that is running is the one being written to.
    """
    base = Path(os.environ.get("APPDATA", "")) / "Virtuali" / "GSX"
    try:
        found = [p for p in base.glob("*/menu") if p.is_file()]
    except OSError:
        return None
    if not found:
        return None
    return max(found, key=lambda p: p.stat().st_mtime)


def read_menu(path) -> list[str]:
    """The entries GSX is showing, without the title line.

    The file is written by a script engine in the simulator while this is
    reading it, so a half-written or missing file is normal and is not an
    error: it means the menu is not up yet, and the caller looks again.
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = [line.strip() for line in text.splitlines()]
    return lines[1:] if lines else []


def menu_now() -> tuple:
    """What GSX is showing, and when it last said so.

    The modification time is half of it. A menu redrawn with the same entries
    -- which is what GSX does when the same menu comes back up -- is a new
    menu, and reading only the text cannot tell it from the old one.
    """
    path = menu_file()
    if path is None:
        return (0.0, ())
    try:
        stamp = path.stat().st_mtime
    except OSError:
        stamp = 0.0
    return (stamp, tuple(read_menu(path)))


@dataclass
class _Walk:
    """A request part way through GSX's menu."""

    service: str
    steps: tuple[Step, ...]
    at: int = 0
    started: float = 0.0
    # The menu the last choice was made from, with the time it was written.
    # A choice does not redraw the file instantly, so without this the same
    # entry is chosen twice and the second choice lands in whatever menu
    # came up.
    chosen: tuple = (0.0, ())


class GsxLink:
    """Keeps the ground handling state current, however it can be read.

    Tries the local variables first and falls back to what the aeroplane is
    doing. Polls slowly: a tug is not a fast-moving thing and this runs on the
    situation loop alongside everything else.
    """

    POLL_S = 1.0

    def __init__(self, bridge=None, enabled: bool = True,
                 control: bool = True, tug=None):
        self.enabled = enabled
        # Whether the radio may start a push as well as watch one. Separate
        # from ``enabled`` because they are different questions: a pilot can
        # want a controller that can see the ramp without wanting one that
        # reaches into it.
        self.control = control
        self.bridge = bridge
        # The simulator's own pushback, supplied by whoever owns the sim link.
        # Used when GSX cannot be reached or has no push to offer.
        self.tug = tug
        self.state = GsxState()
        self._checked = 0.0
        self._announced = ""
        # The request being walked through the menu, if there is one.
        self._walk: _Walk | None = None
        self._notice = ""

    def attach(self, bridge) -> None:
        """Give it a way to read local variables."""
        self.bridge = bridge

    # ------------------------------------------------------------------
    # asking for something
    # ------------------------------------------------------------------

    @property
    def can_ask(self) -> bool:
        """Whether anything can actually be asked for through the menu.

        Asked rather than discovered by a write that quietly does nothing,
        because the answer decides what the ground crew say back: a promise
        that could not be kept is worse than an apology.
        """
        if not (self.enabled and self.control):
            return False
        bridge = self.bridge
        return bool(bridge is not None and getattr(bridge, "can_write", False)
                    and menu_file() is not None)

    def request(self, service: str, now: float | None = None) -> str:
        """Ask GSX for one service, and say in words how it was asked for.

        Returns "" when nothing could be asked -- no bridge that can write, no
        GSX, or a service this does not know how to ask for. The menu is
        *opened* here and chosen from later, once the file says what is on it;
        see :meth:`_advance`.

        Never raises. A request that could not be made is one the pilot makes
        themselves through the menu, which is where they were before.
        """
        steps = SERVICES.get(service)
        if steps is None or not self.can_ask:
            return ""
        now = time.monotonic() if now is None else now
        try:
            if not self.bridge.write(MENU_OPEN, 1):
                return ""
        except Exception:
            log.debug("could not open the GSX menu", exc_info=True)
            return ""
        # Whatever was already being walked is abandoned: the pilot has asked
        # for something else, and two walks in one menu would choose each
        # other's entries.
        self._walk = _Walk(service=service, steps=steps, started=now,
                           chosen=menu_now())
        return f"asking GSX for {service.replace('_', ' ')}"

    def start_pushback(self, now: float | None = None) -> str:
        """Ask for the push, and say in words how it was asked for.

        Two ways, and the first is preferred only where it is actually
        precise. Everything GSX cannot do falls to the simulator's own
        pushback, which needs no bridge and is what GSX drives when it is set
        to take the default pushback over.
        """
        if not (self.enabled and self.control):
            return ""
        if self.request("pushback", now):
            return "calling the tug through GSX"
        return self._sim_pushback()

    def _sim_pushback(self) -> str:
        """The simulator's own tug, for want of anything more exact."""
        if self.tug is None:
            return ""
        try:
            if self.tug():
                return "calling the tug"
        except Exception:
            log.debug("the simulator refused the pushback", exc_info=True)
        return ""

    def _advance(self, now: float) -> None:
        """Walk the request one step further through GSX's menu.

        Split from :meth:`request` because GSX draws each menu in its own time
        and this runs on the situation loop, which is already coming round.
        Nothing is chosen from a menu that has not been read, and a menu with
        nothing matching in it is left alone rather than guessed at -- an
        approval to push that ordered catering would be worse than one that
        did nothing at all.
        """
        walk = self._walk
        if walk is None:
            return
        showing = menu_now()
        entries = showing[1]
        step = walk.steps[walk.at]

        # Only ever from a menu that is not the one just chosen from, or from
        # one that has been sitting still long enough to be the one on screen.
        # See MENU_SETTLED_S.
        fresh = (showing != walk.chosen
                 or now - walk.started >= MENU_SETTLED_S)
        if entries and fresh:
            choice = entry_choice(entries, step)
            if choice >= 0:
                walk.chosen = showing
                walk.at += 1
                walk.started = now
                if walk.at >= len(walk.steps):
                    self._walk = None
                try:
                    self.bridge.write(MENU_CHOICE, choice)
                except Exception:
                    log.debug("could not choose from the GSX menu",
                              exc_info=True)
                    self._walk = None
                    self._failed(walk.service, walk.at <= 1)
                return

        if now - walk.started < MENU_WAIT_S:
            return

        # Out of time. The menu is left showing rather than dismissed:
        # closing it is another write into a menu whose contents are not what
        # was expected, and a menu on screen is something the pilot can see
        # and use.
        self._walk = None
        if not step.required:
            return                    # the submenu this stand does not have
        self._failed(walk.service, True)

    def _failed(self, service: str, say_so: bool) -> None:
        """Nothing in GSX's menu answered to the name of what was asked for."""
        if not say_so:
            return
        if service == "pushback":
            self._notice = self._sim_pushback() or (
                "GSX has no pushback at this stand")
            return
        self._notice = (f"GSX has no {service.replace('_', ' ')} "
                        f"at this stand")

    def notice(self) -> str:
        """Anything the ramp has to report since it was last asked. Once."""
        found, self._notice = self._notice, ""
        return found

    @property
    def asking(self) -> str:
        """What is part way through the menu, by name, or ""."""
        return self._walk.service if self._walk is not None else ""

    # ------------------------------------------------------------------

    def poll(self, aircraft_state=None, now: float | None = None) -> GsxState:
        """Read the state, at most once a second."""
        if not self.enabled:
            self.state = GsxState()
            return self.state
        now = time.monotonic() if now is None else now
        # Ahead of the throttle: a pilot who has been cleared to push is
        # waiting on this, and it is a file read.
        self._advance(now)
        if now - self._checked < self.POLL_S:
            return self.state
        self._checked = now

        lvars = {}
        if self.bridge is not None:
            try:
                lvars = self.bridge.read(LVARS) or {}
            except Exception:
                log.debug("could not read the GSX variables", exc_info=True)
                lvars = {}
        self.state = interpret(lvars) if lvars else from_aircraft(aircraft_state)
        return self.state

    def changed(self) -> str:
        """A one-line description, but only when it has actually changed."""
        described = self.state.describe()
        if described == self._announced:
            return ""
        self._announced = described
        return described


__all__ = [
    "GsxState", "GsxLink", "Step", "SERVICES", "interpret", "from_aircraft",
    "LVARS", "STATE_NAMES", "NOT_AVAILABLE", "CALLABLE", "REQUESTED",
    "RUNNING", "COMPLETED", "FINISHED", "MENU_OPEN", "MENU_CHOICE",
    "MENU_WAIT_S", "MENU_SETTLED_S", "menu_file", "menu_now", "read_menu",
    "pushback_choice",
    "entry_choice", "PUSHBACK_WORDS", "NOT_PUSHBACK_WORDS",
]
