"""The toolbar panel, put where the simulator will find it.

The panel (``msfs/package`` in the tree, ``msfs-panel`` beside the program in
a build) is an ordinary Community package: a folder of files plus a
``layout.json`` that lists every one of them. The simulator reads the package
*through* that list rather than by walking the folder, so a file on disk and
not in the list does not exist as far as it is concerned -- and an entry whose
path or size does not match its file is worse, because the package looks
present and loads nothing. So the package is copied, then the list is written
from what was actually copied.

Each simulator keeps the path to its package folder in ``UserCfg.opt``, so
neither the drive nor the edition is guessed. That file is read as bytes and
decoded as UTF-8 *or* the ANSI code page: the simulator has been seen writing
either, and reading ANSI as UTF-8 with errors ignored -- what the first
version did -- quietly drops the "é" of ``D:\\Jeux\\Été`` and finds no folder.

Used by the installer (``wilcoatc/installer``), by ``msfs/install_panel.py``
in a checkout, and by the tests.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

NAME = "wilcoatc-ingamepanel"

#: Where each simulator writes the path to its package folder, newest first.
#: (environment variable, path under it)
USER_CFG = (
    ("APPDATA", "Microsoft Flight Simulator 2024/UserCfg.opt"),
    ("LOCALAPPDATA", "Packages/Microsoft.Limitless_8wekyb3d8bbwe/LocalCache/UserCfg.opt"),
    ("APPDATA", "Microsoft Flight Simulator/UserCfg.opt"),
    ("LOCALAPPDATA", "Packages/Microsoft.FlightSimulator_8wekyb3d8bbwe/LocalCache/UserCfg.opt"),
)

# Never listed, whatever else is in the folder.
SKIP = {"layout.json"}


def package_source() -> Path | None:
    """The panel this program carries: beside the executable, or the tree's."""
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "msfs-panel")
    root = Path(__file__).resolve().parents[1]
    candidates += [root / "msfs-panel", root / "msfs" / "package"]
    for candidate in candidates:
        if (candidate / "manifest.json").is_file():
            return candidate
    return None


def decode_cfg(raw: bytes) -> str:
    """UserCfg.opt's text, in whichever encoding the simulator wrote it."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("mbcs" if sys.platform == "win32" else "cp1252",
                          errors="replace")


def packages_path(text: str) -> str:
    for line in text.splitlines():
        if line.strip().startswith("InstalledPackagesPath"):
            parts = line.split(None, 1)
            if len(parts) == 2:
                return parts[1].strip().strip('"')
    return ""


def community_folders() -> list[Path]:
    """Every Community folder this machine has, most recent simulator first."""
    found: list[Path] = []
    for variable, rel in USER_CFG:
        base = os.environ.get(variable)
        if not base:
            continue
        config = Path(base) / rel
        try:
            text = decode_cfg(config.read_bytes())
        except OSError:
            continue
        path = packages_path(text)
        if not path:
            continue
        community = Path(path) / "Community"
        if community.is_dir() and all(
                str(community).casefold() != str(f).casefold() for f in found):
            found.append(community)
    return found


def files_in(root: Path) -> list[Path]:
    """Every file in the package, in a stable order."""
    return [p for p in sorted(root.rglob("*")) if p.is_file() and p.name not in SKIP]


def layout_for(root: Path) -> dict:
    """The list the simulator reads the package through.

    Paths use forward slashes and the case the files actually have. Both
    matter: the simulator opens each entry by the string given here, and the
    virtual file system it opens them through does not case-fold.
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
    """Copy the package into a Community folder, replacing an older one.

    Replacing rather than merging: a file left behind from an older version
    is a file the layout no longer lists.
    """
    target = into / NAME
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    (target / "layout.json").write_text(
        json.dumps(layout_for(target), indent=2) + "\n", encoding="utf-8")
    return target


def uninstall(into: Path) -> bool:
    target = into / NAME
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
        return True
    return False
