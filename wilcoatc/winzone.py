"""Undoing what Windows does to a zip it unpacked itself.

A file that arrives from the internet carries a mark: an alternate data
stream named ``Zone.Identifier`` recording where it came from. Windows puts
it on the downloaded zip, and its own extractor copies it onto every single
file inside. 7-Zip does not, which is the whole reason a build works for
everyone who unpacks it one way and fails for everyone who unpacks it the
other.

For nearly everything that ships here the mark is harmless. Python does not
look at it and neither does ``LoadLibrary``. The .NET Framework does: it
refuses to load a managed assembly from the internet zone. So
``Python.Runtime.dll`` never loads, pythonnet cannot resolve its entry point,
and the window fails to open with a message about
``Python.Runtime.Loader.Initialize`` that mentions neither Windows nor zips
and leaves a pilot with nothing to act on.

The marks are therefore stripped at startup, before anything managed is
loaded. Deleting an alternate data stream is deleting a file whose name
happens to contain a colon -- no API to learn, nothing to install.

Only from the managed assemblies, though, and that restraint is deliberate.
Stripping the mark off every library in the folder is what this used to do,
and a few hundred marks removed at startup by an unsigned program is one of
the behaviours antivirus watches for by name: removing the mark of the web is
how a downloaded file talks Windows out of being careful about it. The mark
only ever mattered on the assemblies the .NET Framework loads, which in
practice is Python.Runtime.dll and whatever ships beside it -- one or two
files out of six hundred. Doing the smallest thing that fixes the window
keeps the program's behaviour describable, which is the difference between an
explanation and an excuse when Defender quarantines the build.

A managed assembly is one whose PE header carries a CLR directory, which is a
fixed offset and a size field. It is read rather than guessed at from the file
name, because a hard-coded list of DLLs goes stale the first time pythonnet
ships a second one.

It can fail, and quietly: under Program Files the folder is not writable by
the pilot who launched it. That case is covered from the other side, by the
``loadFromRemoteSources`` switch in the ``.exe.config`` the build writes, and
failing both by :func:`marked`, which is what ``doctor`` reports and what the
window's error message is worded from.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterator

#: The alternate data stream Windows writes on anything it considers foreign.
STREAM = "Zone.Identifier"

#: Only what the .NET Framework may be asked to load matters. The panel's
#: HTML and the voice models carry marks too and nothing ever reads them.
SUFFIXES = ("*.dll", "*.exe")


def relevant() -> bool:
    """Whether the mark can be here at all: Windows, and a shipped build."""
    return os.name == "nt" and bool(getattr(sys, "frozen", False))


def install_dir() -> Path:
    """The folder the pilot unpacked -- the executable's own, not _MEIPASS."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _candidates(root: Path) -> Iterator[Path]:
    """Every file in the installation the framework might load."""
    for pattern in SUFFIXES:
        yield from root.glob(pattern)
    internal = root / "_internal"
    if internal.is_dir():
        for pattern in SUFFIXES:
            yield from internal.rglob(pattern)


def _stream_of(path: Path) -> str:
    return f"{path}:{STREAM}"


def _is_marked(path: Path) -> bool:
    try:
        return os.path.exists(_stream_of(path))
    except OSError:                       # pragma: no cover -- odd filesystems
        return False


#: Data directory 14 of a PE optional header is the CLR runtime header. A
#: non-zero size there is what makes a file managed, and native DLLs -- every
#: one of ONNX Runtime's, CTranslate2's and PortAudio's -- leave it zero.
_CLR_DIRECTORY = 14


def _is_managed(path: Path) -> bool:
    """Whether .NET is the thing that will load this file.

    False for anything unreadable or malformed: a file this cannot parse is
    one whose mark is left alone, which is the safe direction. The worst case
    is a window that does not open and says why, rather than a program that
    quietly strips marks off files it had no reason to touch.
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(1024)
    except OSError:                       # pragma: no cover -- defensive
        return False

    if header[:2] != b"MZ" or len(header) < 0x40:
        return False
    pe = int.from_bytes(header[0x3C:0x40], "little")
    if len(header) < pe + 24 or header[pe:pe + 4] != b"PE\0\0":
        return False

    # The optional header follows the 20-byte COFF header. Its magic says
    # whether the data directory sits at offset 96 (PE32) or 112 (PE32+),
    # which is the only thing the two layouts disagree about here.
    opt = pe + 24
    magic = int.from_bytes(header[opt:opt + 2], "little")
    if magic == 0x10B:
        directories, count_at = opt + 96, opt + 92
    elif magic == 0x20B:
        directories, count_at = opt + 112, opt + 108
    else:
        return False

    if int.from_bytes(header[count_at:count_at + 4], "little") <= _CLR_DIRECTORY:
        return False
    entry = directories + _CLR_DIRECTORY * 8
    if len(header) < entry + 8:           # pragma: no cover -- absurd header
        return False
    size = int.from_bytes(header[entry + 4:entry + 8], "little")
    return size > 0


def marked(root: Path | None = None) -> list[Path]:
    """The managed assemblies Windows has flagged as coming from the internet.

    Not every marked file: only the ones the mark stops from loading. The
    panel's HTML and the voice models carry marks too, and nothing ever reads
    them, so reporting those would be reporting hundreds of files as a problem
    to a pilot who then cannot tell which one is.

    Empty on anything but Windows, and empty on a filesystem that has no
    alternate data streams to begin with -- a build run from a FAT32 stick
    cannot carry a mark and does not need one removed.

    The cheap test comes first: on a folder that was unpacked with 7-Zip, or
    never downloaded at all, nothing is marked and no PE header is ever read.
    """
    if os.name != "nt":
        return []
    root = root or install_dir()
    if not root.is_dir():
        return []
    return [path for path in _candidates(root)
            if _is_marked(path) and _is_managed(path)]


def unblock(root: Path | None = None) -> tuple[int, int]:
    """Remove the marks. Returns how many were cleared and how many resisted.

    Never raises: this runs on the way to opening a window, and a program
    that cannot tidy up its own folder should still try to start.
    """
    cleared = failed = 0
    try:
        targets = marked(root)
    except Exception:                     # pragma: no cover -- defensive
        return 0, 0
    for path in targets:
        try:
            os.remove(_stream_of(path))
        except OSError:
            failed += 1
        else:
            cleared += 1
    return cleared, failed


__all__ = ["marked", "unblock", "install_dir", "relevant", "managed",
           "STREAM"]

#: Public under a name worth reading. The underscore version is what
#: the module uses; this is what a test and the doctor talk about.
managed = _is_managed
