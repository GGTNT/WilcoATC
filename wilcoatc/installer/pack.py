"""The payload the installer carries: the built program folder, as a zip.

    python -m wilcoatc.installer.pack dist\\WilcoATC build\\setup-payload\\WilcoATC.zip

Made here rather than with ``Compress-Archive`` because what goes in is
chosen: only what a release ships. A ``dist`` folder that has been run
has grown logs, a config and maybe voices, and those are one developer's,
not every pilot's. Names are written with forward slashes, as the format
says, whatever the machine.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

#: What a run of the program adds beside it, never shipped.
LEFT_OUT = {"logs", "updates", "airports", "announcements", "checklists",
            "config.yaml", "config.local.yaml", ".write-test", "uninstall.ps1"}


def wanted(rel: Path) -> bool:
    parts = rel.parts
    if parts[0] in LEFT_OUT:
        return False
    # Under data/ only the navigation data ships; voices and models are
    # downloaded on first run.
    if parts[0] == "data" and (len(parts) < 2 or parts[1] != "navdata"):
        return False
    return True


def pack(folder: Path, out: Path) -> int:
    folder = Path(folder)
    if not (folder / "WilcoATC.exe").is_file():
        raise SystemExit(f"{folder} has no WilcoATC.exe: build it first")
    out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(folder.rglob("*")):
            rel = path.relative_to(folder)
            if not path.is_file() or not wanted(rel):
                continue
            zf.write(path, f"{folder.name}/{rel.as_posix()}")
            count += 1
    return count


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    n = pack(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"{n} files in {sys.argv[2]}")
