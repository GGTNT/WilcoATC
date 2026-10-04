"""Where things are, whether this is a checkout or a shipped application.

Two roots, and the difference between them matters.

The *bundle* root holds what ships with the program and never changes: the
panel's HTML and its translations, the navigation database, the default
config. Running from a checkout it is the repository; frozen by PyInstaller it
is the folder the executable sits in, and on Windows that folder may well be
read-only.

The *data* root holds what the program writes: the pilot's config.yaml, the
voices and the recogniser it downloads, the METAR cache, the logs. It sits
beside the executable, which keeps an installation portable -- copy the folder
to a stick and the voices travel with it. When that folder cannot be written
to, because the program was installed under Program Files or is being run from
a read-only medium, it falls back to %LOCALAPPDATA%\\WilcoATC and everything
keeps working.

Nothing here depends on the current working directory. A shortcut launched
from the desktop starts in C:\\Windows\\system32 as often as not, and a program
that writes its logs relative to the working directory writes them there.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "WilcoATC"

#: True when running from a PyInstaller bundle rather than a source checkout.
FROZEN = bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """The read-only resources that ship with the program.

    In a onedir bundle PyInstaller puts data files under ``_MEIPASS``, which is
    the ``_internal`` folder next to the executable. From a checkout it is the
    repository root, one level above the ``wilcoatc`` package.
    """
    if FROZEN:
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _install_dir() -> Path:
    """The folder the pilot sees: where the executable, or the checkout, is."""
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-test"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def _fallback_root() -> Path:
    """Per-user storage, for when the installation folder is read-only."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_NAME
        return Path.home() / "AppData" / "Local" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / "wilcoatc"
    return Path.home() / ".local" / "share" / "wilcoatc"


_data_root: Path | None = None


def data_root() -> Path:
    """The writable root: beside the executable, or per-user if that fails.

    ``WILCOATC_DATA`` overrides it outright, which is what to set when the
    voices live on another drive.
    """
    global _data_root
    if _data_root is not None:
        return _data_root

    override = os.environ.get("WILCOATC_DATA")
    if override:
        chosen = Path(override).expanduser().resolve()
    else:
        beside = _install_dir()
        chosen = beside if _writable(beside) else _fallback_root()

    chosen.mkdir(parents=True, exist_ok=True)
    _data_root = chosen
    return chosen


def data_dir(*parts: str) -> Path:
    """A folder under the writable root, created if it is not there yet."""
    path = data_root().joinpath("data", *parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def resource(*parts: str) -> Path:
    """A file or folder that ships with the program.

    Falls back to the writable root when the resource is not in the bundle,
    which is what lets a downloaded navigation database sit beside a shipped
    one without either of them knowing about the other.
    """
    candidate = bundle_root().joinpath(*parts)
    if candidate.exists():
        return candidate
    return data_root().joinpath(*parts)


# The specific places, named once so nothing has to spell them again.

def resolve(value: str | Path) -> Path:
    """Turn a configured path into a real one.

    Paths in config.yaml are written relative -- ``data/voices`` -- and have
    always been read relative to the working directory, which is only ever
    right when the program is started from its own folder. They are anchored
    to the data root instead. An absolute path in the config is left alone,
    which is how a pilot puts the voices on another drive.
    """
    path = Path(value).expanduser()
    return path if path.is_absolute() else (data_root() / path)


def command_hint(args: str = "") -> str:
    """How to type a command, on the installation this actually is.

    A message telling a pilot who installed a program to run
    ``python -m wilcoatc setup`` is telling them to install Python, which is
    the one thing a shipped build exists to avoid.
    """
    base = "WilcoATC-console.exe" if FROZEN else "python -m wilcoatc"
    return f"{base} {args}".strip()


def config_path() -> Path:
    return data_root() / "config.yaml"


def voice_dir() -> Path:
    return data_dir("voices")


def model_dir() -> Path:
    return data_dir("models")


def navdata_dir() -> Path:
    """The navigation data: the database, its sources, and local corrections.

    It is not put inside the bundle even though it ships with a build. The
    database is rebuilt by ``wilcoatc setup`` and corrected by hand through
    overrides.csv, and neither can be done to a file sealed in a read-only
    executable. The build simply copies it in beside the program.
    """
    return data_dir("navdata")


def announcements_dir() -> Path:
    """Where the pilot drops recorded cabin announcements.

    Beside the program rather than inside it, and created empty on first
    launch, because a folder that does not exist is a feature nobody
    discovers. It is laid out the way the Fenix A320 lays its packs out --
    ``Announcements/<AIRLINE>/`` -- so a pack made for that aeroplane can be
    dropped in whole and works in any of them.
    """
    path = data_root() / "announcements"
    path.mkdir(parents=True, exist_ok=True)
    inner = path / "Announcements"
    inner.mkdir(parents=True, exist_ok=True)
    guide = path / "README.txt"
    if not guide.exists():
        try:
            guide.write_text(ANNOUNCEMENTS_README, encoding="utf-8")
        except OSError:
            pass
    return path


#: Written into the announcements folder the first time it is made, because a
#: folder with nothing in it and no explanation is a folder nobody uses.
ANNOUNCEMENTS_README = """Recorded cabin announcements
============================

Put audio files in the Announcements folder beside this one and WilcoATC will
play them instead of its synthesised cabin crew.

    announcements/
      Announcements/
        BoardingWelcome.ogg          played for any flight
        SafetyBriefing.ogg
        BAW/                         played only when the callsign is BAW...
          BoardingWelcome.ogg
        EJU/
          BoardingWelcome.ogg

The layout and the file names are the ones the Fenix A320 uses, so a sound
pack made for that aeroplane can be dropped in whole -- and it will then work
in any aircraft, which is the point. A folder named "default" is the same as
putting the files loose in Announcements.

If you already have a pack somewhere else, do not copy it: put its path into
Settings > Immersion > Where they are. Pointing at the pack, at the folder
above it, or at a whole community folder full of packs all work.

The moments, and the file name for each
---------------------------------------

  BoardingWelcome           boarding has begun
  BoardingMusic             under the boarding, after the welcome
  BoardingComplete          everybody is aboard
  ArmDoors                  doors to automatic and cross check
  PreSafetyBriefing         the call before the demonstration
  SafetyBriefing            the safety demonstration
  CabinDimTakeoff           lights down for a night departure
  CrewSeatsTakeoff          cabin crew, take your seats for departure
  CallCabinSecureTakeoff    and the answer back
  AfterTakeoff              the climb, seatbelt sign on
  FastenSeatbelt            turbulence
  DescentSeatbelts          the descent has begun
  CrewSeatsLanding          cabin crew, take your seats for landing
  CallCabinSecureLanding    and the answer back
  AfterLanding              welcome to wherever you have arrived
  DisarmDoors               doors to manual and cross check
  DisembarkStarted          goodbye

Anything with no file falls back to the synthesised crew, so a partial pack
is fine. A moment that has neither is simply silent.

Choosing between several files
------------------------------

Tags in square brackets narrow when a file is used. A file is only considered
if every tag on it applies, and the most specific one wins.

  SafetyBriefing[1].ogg        one of several, picked once per flight and
  SafetyBriefing[2].ogg        then kept for the rest of it
  BoardingWelcome[Morning].ogg 06:00-12:00; also Night, Afternoon, Evening
  BoardingWelcome[EJU].ogg     that operator
  AfterTakeoff[A319].ogg       that aircraft type
  BoardingWelcome[EJU][Evening].ogg     tags combine

Formats
-------

Ogg Vorbis (.ogg) is what the packs use and what Fenix requires. WAV, FLAC,
Opus and MP3 are also read here.
"""


def log_dir() -> Path:
    path = data_root() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path
