# -*- coding: utf-8 -*-
"""Recorded cabin announcements, from a folder the pilot fills.

The synthesised crew are a fallback. What people actually want in the cabin
is a recording -- their airline's real one, or one of the many packs made for
the Fenix A320 -- and there are a lot of those packs about already. So this
reads them, in the layout and with the naming rules Fenix defined, and hands
the crew a file to play instead of a sentence to say.

The layout, which is Fenix's
-----------------------------

    <any folder>/Announcements/<AIRLINE>/BoardingWelcome.ogg

``<AIRLINE>`` is the operator's ICAO designator -- BAW, EJU, DLH -- and is
how a pack is chosen for the flight you are on. Files sitting directly in
``Announcements``, or in a folder called ``default``, are used for any
flight, which is what makes a single pack work in an aeroplane that has no
airline at all.

That last part is the one deliberate difference. Fenix picks the folder from
``icao_airline`` in aircraft.cfg, which only exists on a liveried airliner.
Here it comes from the callsign, and a flight without one falls through to
the default folder -- so a pack works in a Cessna as well as in an A320,
which is the whole point of not building this into one aeroplane.

The names, which are also Fenix's
---------------------------------

Every file is named for the moment it belongs to, with optional tags in
square brackets:

    SafetyBriefing.ogg
    SafetyBriefing[2].ogg              a variant, chosen once per flight
    BoardingWelcome[Morning].ogg       06:00-12:00 local
    BoardingWelcome[EJU][Evening].ogg  that airline, that part of the day
    AfterTakeoff[A319].ogg             that aircraft type

A file is a candidate only if *every* tag on it applies to this flight. Among
the candidates the most specific wins -- most tags matched -- and a tie is
broken by a number that is fixed for the flight, so a numbered variant is
chosen once and then stays. Hearing a different voice for each half of the
same announcement is worse than hearing the same one twice.

Formats
-------

Fenix requires ``.ogg``. Anything libsndfile can read is accepted here --
Ogg Vorbis, Opus, WAV, FLAC, MP3 -- because there is no reason to make
somebody convert a file they already have, and the packs in the wild are
Ogg anyway.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

# The folder a pack lives under, from Fenix's own layout.
FOLDER = "Announcements"

# Airline folders that apply to any flight.
ANY_AIRLINE = ("default", "generic", "common", "all")

# What can be read. Ogg is what the packs are; the rest are here because
# refusing a WAV somebody already has would be pure ceremony.
SUFFIXES = (".ogg", ".wav", ".flac", ".mp3", ".opus")

# The moments Fenix names, with what each one is for. The crew director maps
# its own cues onto these, so a pack written for an A320 plays in anything.
#
# Taken from the Fenix cabin announcements guide. The triggers there are
# theirs; where this program already had a moment of its own for the same
# announcement, that is the one used -- see CUE_RECORDINGS in crew_lines.
#
# One name is not theirs. A Fenix pack has no captain in it, because in that
# aeroplane the captain is the person holding the sidestick; here there is a
# captain, and he makes a welcome announcement during the push. So
# CaptainWelcome is added rather than borrowed, and it is at the end of the
# list to keep the borrowed ones together and in Fenix's own order.
EVENTS: tuple[str, ...] = (
    "BoardingWelcome",          # boarding, repeated while it goes on
    "BoardingMusic",            # under the boarding, after the welcome
    "BoardingComplete",         # everybody is aboard
    "ArmDoors",                 # doors to automatic and cross check
    "PreSafetyBriefing",        # the call before the demonstration
    "SafetyBriefing",           # the demonstration
    "CabinDimTakeoff",          # lights down for a night departure
    "CrewSeatsTakeoff",         # cabin crew, take your seats
    "CallCabinSecureTakeoff",   # and the answer back
    "AfterTakeoff",             # the climb, seatbelt sign on
    "FastenSeatbelt",           # turbulence
    "DescentSeatbelts",         # the descent
    "CrewSeatsLanding",         # cabin crew, seats for landing
    "CallCabinSecureLanding",   # and the answer back
    "AfterLanding",             # welcome to wherever this is
    "DisarmDoors",              # doors to manual
    "DisembarkStarted",         # goodbye
    "CaptainWelcome",           # the captain, during the pushback
    "CabinWelcome",             # the cabin manager, once the push is over
)

# The parts of the day a [Morning] tag means, as Fenix defines them.
PARTS_OF_DAY: tuple[tuple[str, int, int], ...] = (
    ("night", 0, 6),
    ("morning", 6, 12),
    ("afternoon", 12, 18),
    ("evening", 18, 24),
)

_NAME = re.compile(r"^([A-Za-z0-9_ -]+?)\s*((?:\[[^\]]*\])*)$")
_TAG = re.compile(r"\[([^\]]*)\]")


def _entries(folder: Path) -> list[Path]:
    """What is in a folder, or nothing if it cannot be read.

    A pack can sit on a network share or a drive that has gone away, and a
    missing folder is a fact about the pilot's disk rather than an error in
    this program.
    """
    try:
        return sorted(folder.iterdir())
    except OSError:
        return []


@dataclass(frozen=True)
class Recording:
    """One file, with the name broken into what it is and when it applies."""

    path: Path
    event: str                      # normalised: lower case, no spaces
    airline: str                    # the folder it came from, or ""
    tags: tuple[str, ...]           # lower case, in the order written

    @property
    def variants(self) -> tuple[str, ...]:
        """The numbered tags: which alternative of the same thing this is."""
        return tuple(t for t in self.tags if t.isdigit())

    @property
    def conditions(self) -> tuple[str, ...]:
        """The tags that have to be true, rather than merely chosen."""
        return tuple(t for t in self.tags if not t.isdigit())


@dataclass(frozen=True)
class Flight:
    """What a recording is being chosen for."""

    airline: str = ""               # ICAO designator, e.g. "BAW"
    aircraft: str = ""              # ICAO type, e.g. "A319"
    hour: int = 12                  # local hour at the aerodrome, 0-23
    refuelling: bool = False
    # Fixed for the flight, so a numbered variant is picked once and stays.
    seed: int = 0

    @property
    def part_of_day(self) -> str:
        for name, start, end in PARTS_OF_DAY:
            if start <= self.hour < end:
                return name
        return "afternoon"


def normalise(event: str) -> str:
    return "".join(str(event).split()).lower()


def parse(path: Path, airline: str = "") -> Recording | None:
    """Read a file name into an event and its tags."""
    match = _NAME.match(path.stem.strip())
    if not match:
        return None
    event, rest = match.groups()
    tags = tuple(t.strip().lower() for t in _TAG.findall(rest or "") if t.strip())
    name = normalise(event)
    if not name:
        return None
    return Recording(path=path, event=name, airline=airline.upper(), tags=tags)


class AnnouncementLibrary:
    """Every recording the pilot has put where this can find it.

    Scanned rather than configured: the folder is the interface. Dropping a
    file in and flying again is the whole of the setup, and there is nothing
    to keep in step with it.
    """

    # How long a scan is trusted before the folder is looked at again. Long
    # enough not to walk a directory on every announcement, short enough that
    # adding a file and flying again picks it up.
    RESCAN_S = 30.0

    def __init__(self, roots: Iterable[Path] = ()):
        self.roots = [Path(r) for r in roots if r]
        self._by_event: dict[str, list[Recording]] = {}
        self._scanned_at = 0.0
        self._lock = threading.Lock()
        self._counted = 0

    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        self._maybe_scan()
        return bool(self._by_event)

    @property
    def count(self) -> int:
        self._maybe_scan()
        return self._counted

    def airlines(self) -> list[str]:
        """Which packs are installed, for the settings screen to report."""
        self._maybe_scan()
        found = {r.airline for group in self._by_event.values() for r in group}
        return sorted(a for a in found if a)

    # ------------------------------------------------------------------

    def forget(self) -> None:
        """Read the folder again on the next question.

        For a pilot who has just dropped a pack in and is looking at the
        settings screen, and for a folder that has just been renamed.
        """
        with self._lock:
            self._by_event = {}
            self._scanned_at = 0.0
            self._counted = 0

    def _maybe_scan(self) -> None:
        now = time.monotonic()
        if self._by_event and (now - self._scanned_at) < self.RESCAN_S:
            return
        with self._lock:
            if self._by_event and (time.monotonic() - self._scanned_at) < self.RESCAN_S:
                return
            self._scanned_at = time.monotonic()
            self._by_event = self._scan()
            self._counted = sum(len(v) for v in self._by_event.values())

    def _scan(self) -> dict[str, list[Recording]]:
        found: dict[str, list[Recording]] = {}
        bases = []
        for root in self.roots:
            for base in self._folders(root):
                if base not in bases:
                    bases.append(base)
        known = set(bases)

        for base in bases:
            # Files directly under an Announcements folder apply to any
            # flight; a subfolder is an airline.
            self._take(base, "", found)
            try:
                children = sorted(p for p in base.iterdir() if p.is_dir())
            except OSError:
                continue
            for child in children:
                # ...unless the subfolder is itself an Announcements folder,
                # which happens whenever somebody's outer folder is called
                # "announcements" too. Reading it as an airline named
                # ANNOUNCEMENTS is the sort of thing nobody would ever
                # think to look for.
                if child in known or child.name.lower() == FOLDER.lower():
                    continue
                airline = "" if child.name.lower() in ANY_AIRLINE else child.name
                self._take(child, airline, found)
        return found

    @staticmethod
    def _folders(root: Path) -> list[Path]:
        """Every ``Announcements`` folder under, or at, a configured path.

        Pointing at the pack, at the folder above it, or at a whole community
        folder full of packs all work, because all three are things people
        will do and none of them is wrong. So is dropping files loose into
        the folder this program made, which is what somebody who has not
        read the note in it will do.
        """
        try:
            if not root.is_dir():
                return []
        except OSError:
            return []

        out: list[Path] = []
        if root.name.lower() == FOLDER.lower():
            out.append(root)
        here = root / FOLDER
        try:
            if here.is_dir():
                out.append(here)
        except OSError:
            pass
        if out:
            return out
        # A community folder, holding several packs.
        try:
            for child in sorted(root.iterdir()):
                if child.is_dir() and (child / FOLDER).is_dir():
                    out.append(child / FOLDER)
        except OSError:
            return []
        if out:
            return out
        # Or the pilot has named a folder that simply holds the files, with
        # no Announcements folder anywhere near it.
        #
        # Which is what naming an airline folder inside somebody else's pack
        # looks like, and it is a thing people do: a pack ships its files
        # under a folder named for the airline it was recorded for, and a
        # pilot who does not fly for that airline points the setting at it so
        # they can use it anyway. Read as a layout that was not found, that
        # setting did nothing at all and said nothing about it. Read as what
        # it plainly is -- "these files, please" -- it works, and the files
        # apply to every flight because the pilot has said so by naming them.
        if any(path.is_file() and path.suffix.lower() in SUFFIXES
               for path in _entries(root)):
            return [root]
        return []

    @staticmethod
    def _take(folder: Path, airline: str,
              into: dict[str, list[Recording]]) -> None:
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            return
        for path in entries:
            if not path.is_file() or path.suffix.lower() not in SUFFIXES:
                continue
            found = parse(path, airline)
            if found is None:
                continue
            into.setdefault(found.event, []).append(found)

    # ------------------------------------------------------------------

    def has(self, event: str) -> bool:
        self._maybe_scan()
        return bool(self._by_event.get(normalise(event)))

    def pick(self, event: str, flight: Flight) -> Recording | None:
        """The best recording for a moment, or None.

        Best means: every tag applies, and of those, the one with the most
        tags matched. A tie is settled by the flight's own number rather
        than at random, so a numbered variant is chosen once and then stays
        for the rest of the flight.
        """
        self._maybe_scan()
        candidates = self._by_event.get(normalise(event))
        if not candidates:
            return None

        scored: list[tuple[int, int, Recording]] = []
        for found in candidates:
            score = self._score(found, flight)
            if score is None:
                continue
            # The airline folder is worth more than any single tag: a pack
            # made for this operator beats a generic file with the right
            # time of day on it.
            scored.append((score + (10 if found.airline else 0),
                           len(found.tags), found))
        if not scored:
            return None

        best = max(s for s, _n, _r in scored)
        shortlist = [r for s, _n, r in scored if s == best]
        if len(shortlist) == 1:
            return shortlist[0]
        shortlist.sort(key=lambda r: r.path.name.lower())
        return shortlist[flight.seed % len(shortlist)]

    @staticmethod
    def _score(found: Recording, flight: Flight) -> int | None:
        """How well a file fits, or None if one of its tags rules it out."""
        if found.airline and found.airline != (flight.airline or "").upper():
            return None
        score = 0
        for tag in found.conditions:
            if tag == flight.part_of_day:
                score += 1
            elif tag == "refueling" or tag == "refuelling":
                if not flight.refuelling:
                    return None
                score += 1
            elif tag == (flight.aircraft or "").lower():
                score += 1
            elif tag == (flight.airline or "").lower():
                score += 1
            elif tag in _EVERY_PART_OF_DAY:
                return None            # a different part of the day
            else:
                # A tag naming an airline or a type that is not this one.
                return None
        return score


_EVERY_PART_OF_DAY = {name for name, _s, _e in PARTS_OF_DAY}


# --------------------------------------------------------------------------
# playing one
# --------------------------------------------------------------------------


def load(path: Path):
    """Read a recording as float32 samples, and its sample rate.

    Kept out of the library itself so that scanning a folder never opens a
    file: the panel asks what is installed on every settings screen, and
    decoding fifty announcements to answer that would be absurd.

    Anything louder than full scale is scaled down to it, and nothing else is
    touched. That is not this program second-guessing somebody's mastering --
    it is the opposite. A decoded Ogg can overshoot unity, which real packs do
    routinely, and the player has to clip what it cannot fit; a pack peaking
    at twice full scale arrives as a distorted announcement rather than a loud
    one. Turning it down is the only reading of "play it as it was recorded"
    that survives contact with the output stage.
    """
    import numpy as np
    import soundfile as sf

    audio, rate = sf.read(str(path), dtype="float32", always_2d=True)
    if audio.shape[1] > 2:
        audio = audio[:, :2]
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 1.0:
        audio = audio / peak
    return np.ascontiguousarray(audio), int(rate)


def flight_seed(text: str) -> int:
    """A number fixed for one flight, from something stable about it."""
    return int(hashlib.sha1((text or "wilcoatc").encode("utf-8")).hexdigest()[:8], 16)


__all__ = [
    "AnnouncementLibrary", "Recording", "Flight", "EVENTS", "FOLDER",
    "SUFFIXES", "load", "parse", "normalise", "flight_seed",
]
