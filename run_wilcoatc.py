"""The entry point a frozen build starts from.

PyInstaller needs a script, not a module, and the windowed build needs one
more thing than ``python -m wilcoatc`` does: a place for output to go. A GUI
executable on Windows has no console attached, so ``sys.stdout`` is None, and
the first library that writes a progress line to it brings the program down
before the window ever appears. Everything is pointed at the void instead --
the log file is where a frozen build's account of itself belongs.

It is also the last moment at which the folder can be repaired before
anything is loaded from it. Windows' own zip extractor marks every file it
unpacks as coming from the internet, and the .NET Framework will not load a
managed assembly wearing that mark, so the window dies on Python.Runtime.dll
long before it is anyone's fault. The marks come off here, first thing.
"""

from __future__ import annotations

import multiprocessing
import os
import sys


def _replace_missing_streams() -> None:
    """Give stdout, stderr and stdin somewhere to be when Windows gives none."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8", errors="replace")


def _unblock_bundle() -> None:
    """Take Windows' internet mark off the shipped libraries, if it is there.

    Best effort by design. When the folder cannot be written to -- an install
    under Program Files -- the marks stay, the ``.exe.config`` beside the
    executable is what lets the runtime load them anyway, and ``doctor`` is
    what says so. None of that is worth failing to start over.
    """
    try:
        from wilcoatc.winzone import relevant, unblock

        if relevant():
            unblock()
    except Exception:
        pass


def main() -> int:
    multiprocessing.freeze_support()
    _replace_missing_streams()
    _unblock_bundle()
    from wilcoatc.cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
