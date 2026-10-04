"""Put the toolbar panel where the simulator will find it.

The panel is an ordinary Community package: a folder of files plus a
``layout.json`` that lists every one of them. The simulator reads the package
*through* that list rather than by walking the folder, so a file that is on
disk and not in the list does not exist as far as the simulator is concerned --
and a list entry whose path or size does not match the file is worse, because
the package looks present and loads nothing.

That is what this exists to get right. It copies the package, then writes the
list from what it actually copied.

    python msfs/install_panel.py              # find the simulator and install
    python msfs/install_panel.py --check      # say what it would do
    python msfs/install_panel.py --to <path>  # a Community folder of your own

Both simulators keep the path to their package folder in ``UserCfg.opt``, so
neither the drive nor the edition has to be guessed at.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKAGE = HERE / "package"
NAME = "wilcoatc-ingamepanel"

# Where each simulator writes the path to its own package folder. The 2024
# entry is first because that is what this panel is built against; the 2020
# one loads it too.
USER_CFG = (
    Path.home() / "AppData/Roaming/Microsoft Flight Simulator 2024/UserCfg.opt",
    Path.home() / "AppData/Roaming/Microsoft Flight Simulator/UserCfg.opt",
    Path.home() / ("AppData/Local/Packages/Microsoft.FlightSimulator_8wekyb3d8bbwe"
                   "/LocalCache/UserCfg.opt"),
)

# Never listed, whatever else is in the folder.
SKIP = {"layout.json"}


def community_folders() -> list[Path]:
    """Every Community folder this machine has, most recent simulator first."""
    found: list[Path] = []
    for config in USER_CFG:
        if not config.exists():
            continue
        try:
            text = config.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip().startswith("InstalledPackagesPath"):
                continue
            path = line.split(None, 1)[1].strip().strip('"')
            community = Path(path) / "Community"
            if community.is_dir() and community not in found:
                found.append(community)
    return found


def files_in(root: Path) -> list[Path]:
    """Every file in the package, in a stable order."""
    out = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name not in SKIP:
            out.append(path)
    return out


def layout_for(root: Path) -> dict:
    """The list the simulator reads the package through.

    Paths use forward slashes and the case the files actually have. Both
    matter: the simulator opens each entry by the string given here, and the
    virtual file system it opens them through does not do the case-folding
    Windows would.
    """
    content = []
    for path in files_in(root):
        relative = path.relative_to(root).as_posix()
        if relative == "manifest.json":
            continue
        stat = path.stat()
        content.append({
            "path": relative,
            "size": stat.st_size,
            # The simulator's own epoch: hundred-nanosecond ticks since 1601.
            # It only ever compares these, so what matters is that they move
            # when a file does.
            "date": int(stat.st_mtime * 10_000_000) + 116_444_736_000_000_000,
        })
    return {"content": content}


def install(source: Path, into: Path) -> Path:
    """Copy the package in, replacing whatever was there before.

    Replacing rather than merging: a file left behind from an older version is
    a file the layout no longer lists, and chasing which of two panels the
    simulator loaded is not a thing anybody should have to do.
    """
    target = into / NAME
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    layout = layout_for(target)
    (target / "layout.json").write_text(
        json.dumps(layout, indent=2) + "\n", encoding="utf-8")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", help="a Community folder to install into")
    parser.add_argument("--check", action="store_true",
                        help="say what would happen and change nothing")
    args = parser.parse_args()

    if not PACKAGE.is_dir():
        print(f"no package to install: {PACKAGE} is not there")
        return 1

    if args.to:
        targets = [Path(args.to)]
    else:
        targets = community_folders()
        if not targets:
            print("Could not find a Community folder. Microsoft Flight "
                  "Simulator writes its package path into UserCfg.opt; if "
                  "yours is somewhere unusual, pass it:")
            print("  python msfs/install_panel.py --to <Community folder>")
            return 1

    listed = files_in(PACKAGE)
    print(f"{len(listed)} files:")
    for path in listed:
        print(f"  {path.relative_to(PACKAGE).as_posix()}")

    for community in targets:
        if args.check:
            print(f"\nwould install into {community / NAME}")
            continue
        if not community.is_dir():
            print(f"\n{community} is not a folder")
            return 1
        target = install(PACKAGE, community)
        print(f"\ninstalled into {target}")

    if not args.check:
        print("\nRestart the simulator. The button is in the toolbar, under "
              "the same menu as GSX.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
