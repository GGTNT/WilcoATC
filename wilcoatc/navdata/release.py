# -*- coding: utf-8 -*-
"""The navigation data as it ships in a build: everything but VatGlasses.

The VatGlasses sectors are under CC BY-NC-SA. WilcoATC downloads them only
when the pilot asks, after showing that licence, and never bundles them
(data/navdata/SOURCES.md). build.ps1 used to copy the whole of data/navdata
into the release, which on a machine where they had been downloaded shipped
their folder and a nav.sqlite holding eleven thousand of their sectors. This
is the copy the build makes instead: the folder is left out, and the copied
database has its sector tables emptied. The pilot's own copy is not touched.

    python -m wilcoatc.navdata.release <data\\navdata> <dist\\...\\data\\navdata>
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

# Never in a release: the opt-in sectors, and a rebuild still waiting to be
# swapped in (navdata.build.put_in_place).
NOT_SHIPPED = ("vatglasses",)
NOT_SHIPPED_SUFFIXES = (".new",)

# The tables only the VatGlasses data fills.
NETWORK_TABLES = ("sectors", "sector_positions")


def release_copy(source: Path, destination: Path) -> list[str]:
    """Copy ``source`` to ``destination`` as it may ship. Returns what was
    left out or emptied, for the build to print."""
    source, destination = Path(source), Path(destination)
    left_out: list[str] = []

    def ignore(folder: str, names: list[str]) -> set[str]:
        skipped = {n for n in names if n in NOT_SHIPPED
                   or n.endswith(NOT_SHIPPED_SUFFIXES)}
        left_out.extend(sorted(skipped))
        return skipped

    shutil.copytree(source, destination, ignore=ignore, dirs_exist_ok=True)
    database = destination / "nav.sqlite"
    if database.exists():
        removed = strip_network_data(database)
        if removed:
            left_out.append(f"{removed} VatGlasses rows from nav.sqlite")
    return left_out


def strip_network_data(database: Path) -> int:
    """Empty the VatGlasses tables of a database. Returns how many rows went."""
    conn = sqlite3.connect(database)
    try:
        removed = 0
        for table in NETWORK_TABLES:
            try:
                removed += conn.execute(f"DELETE FROM {table}").rowcount
            except sqlite3.OperationalError:
                continue          # a database built before the table existed
        conn.commit()
        if removed:
            conn.execute("VACUUM")
        return removed
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    for item in release_copy(Path(argv[0]), Path(argv[1])):
        print(f"  not shipped: {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
