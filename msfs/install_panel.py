"""Put the toolbar panel where the simulator will find it, from a checkout.

A pilot does not run this: WilcoATC-Setup.exe installs the panel into every
Community folder it finds (the "Toolbar panel" switch on its Flight Simulator
screen). This is the same thing for a developer working in the tree; the work
itself is ``wilcoatc/msfs_panel.py``.

    python msfs/install_panel.py              # find the simulator and install
    python msfs/install_panel.py --check      # say what it would do
    python msfs/install_panel.py --to <path>  # a Community folder of your own
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from wilcoatc.msfs_panel import (NAME, community_folders, files_in,  # noqa: E402
                                 install, layout_for)

PACKAGE = HERE / "package"
__all__ = ["NAME", "PACKAGE", "community_folders", "files_in", "install", "layout_for"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", help="a Community folder to install into")
    parser.add_argument("--check", action="store_true",
                        help="say what would happen and change nothing")
    args = parser.parse_args()

    if not PACKAGE.is_dir():
        print(f"no package to install: {PACKAGE} is not there")
        return 1
    targets = [Path(args.to)] if args.to else community_folders()
    if not targets:
        print("Could not find a Community folder; pass one with --to.")
        return 1
    for community in targets:
        if args.check:
            print(f"would install into {community / NAME}")
            continue
        if not community.is_dir():
            print(f"{community} is not a folder")
            return 1
        print(f"installed into {install(PACKAGE, community)}")
    if not args.check:
        print("Restart the simulator. The button is in the toolbar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
