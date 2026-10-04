"""Frequencies the published data does not have.

The worldwide dataset is community-maintained and it is thin in places. Charles
de Gaulle carries nine frequencies in it and something like twenty in reality,
so a pilot sitting on 123.605 at CDG is tuned to a real frequency that no
database here has ever heard of.

There is no honest way to invent the missing ones. What there is instead is a
way to write down the one in front of you, once, so that it works from then on:

    python -m wilcoatc freq LFPG --add GND 123.605 --name "De Gaulle Ground"

That appends to ``data/navdata/overrides.csv`` -- which the database build
already reads, so the entry survives a rebuild -- and inserts it into the live
database, so it works on the next transmission rather than after a rebuild.

Writing them down one at a time is the fallback. When the simulator is running
it can simply be asked, and it knows all thirty:

    python -m wilcoatc freq LFPG --from-sim

Rows are tagged with where they came from, so the three sources can live in one
table without overwriting each other: the published dataset, the simulator, and
whatever the pilot wrote down. A pilot's own entry always wins, and a simulator
import replaces the published rows for that airport rather than adding to them
-- a frequency the simulator does not have is one nobody can talk on.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from .build import DB_PATH
from .channels import is_valid_channel, snap

OVERRIDES = Path(DB_PATH).parent / "overrides.csv"

# The positions an override may name. Anything else is a typo.
POSITIONS = ("DEL", "GND", "TWR", "APP", "DEP", "CTR", "ATIS", "AWOS",
             "CTAF", "INFO")


# Where a row came from. The column is added on demand, so a database built
# before this existed picks it up without a rebuild.
PUBLISHED = ""          # the worldwide dataset this ships with
FROM_SIM = "sim"        # read out of the running simulator
LOCAL = "local"         # written down by the pilot

# Sort order, and therefore which frequency a controller is reached on first.
# The pilot's own entry beats the simulator, which beats the published data.
PRIORITY = {LOCAL: -2, FROM_SIM: -1, PUBLISHED: 0}


class Rejected(Exception):
    """The entry would make the database worse."""


def _with_source(conn) -> None:
    """Make sure the table can say where a row came from."""
    columns = {r["name"] for r in conn.execute(
        "PRAGMA table_info(frequencies)").fetchall()}
    if "source" not in columns:
        conn.execute("ALTER TABLE frequencies ADD COLUMN source TEXT DEFAULT ''")


def add_frequency(ident: str, position: str, mhz: float, name: str = "",
                  description: str = "", db_path: Path | None = None) -> str:
    """Record a frequency, in the overrides file and in the live database.

    Returns a line describing what was written. Raises :class:`Rejected` if the
    entry could not be right -- an unknown airport, a position that is not a
    position, or a number no radio could be tuned to.
    """
    ident = (ident or "").strip().upper()
    position = (position or "").strip().upper()
    if position not in POSITIONS:
        raise Rejected(f"{position!r} is not a position. "
                       f"One of: {', '.join(POSITIONS)}")
    if not is_valid_channel(mhz):
        nearest = snap(mhz)
        raise Rejected(f"{mhz:.3f} is not a channel a radio can select. "
                       f"Did you mean {nearest:.3f}?")

    path = Path(db_path) if db_path else Path(DB_PATH)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        airport = conn.execute(
            "SELECT ident, name, spoken FROM airports WHERE ident = ?",
            (ident,)).fetchone()
        if airport is None:
            raise Rejected(f"no airport {ident} in the database")

        facility = name.strip() or (airport["spoken"] or airport["name"])
        note = description.strip() or "added locally"

        conn.execute(
            "DELETE FROM frequencies WHERE ident = ? AND position = ? AND mhz = ?",
            (ident, position, round(float(mhz), 3)))
        _with_source(conn)
        conn.execute(
            "INSERT INTO frequencies "
            "(ident, position, mhz, facility, description, priority, source) "
            "VALUES (?,?,?,?,?,?,?)",
            (ident, position, round(float(mhz), 3), facility, note,
             PRIORITY[LOCAL], LOCAL))
        conn.commit()
    finally:
        conn.close()

    _append_override(ident, position, mhz, facility, note)
    return (f"{ident} {position} {mhz:.3f} as \"{facility}\" — "
            f"live now, and kept in {OVERRIDES.name}")


def _spoken_names(conn, ident: str, fallback: str) -> dict[str, str]:
    """What each position at this field is already called on the radio.

    The facility name is spoken aloud -- it is the "New York" in "New York
    Approach" -- and the published dataset has these right, including the ones
    that differ from the airport's own name. The simulator does not: it shouts,
    it calls the ATIS at Charles de Gaulle "LFPG", and it calls one frequency
    there "COMMUNE INFO & MILITARY A INFORMATION". So the numbers come from the
    simulator and the names stay where they were.
    """
    names: dict[str, str] = {}
    counts: dict[str, int] = {}
    for row in conn.execute(
            "SELECT position, facility, COUNT(*) AS n FROM frequencies "
            "WHERE ident = ? AND facility != '' "
            "GROUP BY position, facility ORDER BY n DESC", (ident,)):
        names.setdefault(row["position"], row["facility"])
        counts[row["facility"]] = counts.get(row["facility"], 0) + row["n"]
    names[""] = max(counts, key=counts.get) if counts else fallback
    return names


def replace_airport(ident: str, frequencies, db_path=None) -> tuple[int, int]:
    """Put the simulator's frequency list in place of the published one.

    Returns how many rows went in and how many were replaced. Anything the
    pilot wrote down themselves is left where it is: they were sitting in the
    aeroplane when they wrote it.
    """
    ident = (ident or "").strip().upper()
    if not ident or not frequencies:
        return 0, 0

    path = Path(db_path) if db_path else Path(DB_PATH)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        _with_source(conn)
        airport = conn.execute(
            "SELECT ident, name, spoken FROM airports WHERE ident = ?",
            (ident,)).fetchone()
        if airport is None:
            return 0, 0

        # Read the spoken names before the rows carrying them are removed.
        names = _spoken_names(conn, ident,
                              airport["spoken"] or airport["name"] or ident)
        # And the runways the published data says each frequency works --
        # "TWR (RWY 07R/25L and 01)" -- which the simulator's list does not
        # carry. Without them a split tower could not be told apart, and the
        # handoff named whichever came first (see station_for_runway).
        runway_notes = {
            (row["position"], round(float(row["mhz"]), 3)): row["description"]
            for row in conn.execute(
                "SELECT position, mhz, description FROM frequencies "
                "WHERE ident = ? AND description LIKE '%RWY%'", (ident,))
        }

        gone = conn.execute(
            "DELETE FROM frequencies WHERE ident = ? "
            "AND COALESCE(source, '') != ?", (ident, LOCAL)).rowcount

        # Which name the airport itself goes by in the simulator's list. At de
        # Gaulle most frequencies are "DE GAULLE" and a handful are "DE GAULLE
        # APRON"; at Kennedy most are "KENNEDY" and two are "AMERICAN" and
        # "DELTA". The airport's own ones are the frequencies a pilot is
        # handed to, which is why the simulator's ATC menu offers 121.610 as
        # de Gaulle Ground and the apron frequencies unnamed after it.
        # The simulator carries what is at the airport, not what a civil radio
        # can select: de Gaulle's list includes military frequencies above the
        # airband. Anything a COM radio could not be tuned to is not something
        # a pilot can be answered on.
        usable = [f for f in frequencies if is_valid_channel(f.mhz)]
        tally: dict[str, int] = {}
        for entry in usable:
            key = " ".join((entry.name or "").split()).upper()
            if key and key != ident:
                tally[key] = tally.get(key, 0) + 1
        main = max(tally, key=tally.get) if tally else ""

        # The simulator's order is kept within each position, with the
        # airport's own frequencies ahead of the aprons and the airline desks,
        # so whichever tower it lists first is the one a pilot reaches first.
        ordered = sorted(
            enumerate(usable),
            key=lambda pair: (
                " ".join((pair[1].name or "").split()).upper() != main,
                pair[0]),
        )

        # Priority marks the primary frequency for a position and nothing
        # more. It is not a place in a queue: the station resolver weighs it
        # when deciding who answers a shared frequency, so a wide spread here
        # would outweigh how far away the airport is. The first entry for a
        # position leads; the rest sort by frequency, as the published data
        # does.
        seen: set[str] = set()
        rows = []
        for _, entry in ordered:
            position = entry.position
            primary = position not in seen
            seen.add(position)
            mhz = round(float(entry.mhz), 3)
            description = " ".join((entry.name or "").split()) or "from the simulator"
            note = runway_notes.get((position, mhz))
            if note:
                description = f"{description} ({note})"
            rows.append((
                ident, position, mhz,
                names.get(position) or names[""],
                description,
                PRIORITY[FROM_SIM] if primary else PRIORITY[PUBLISHED],
                FROM_SIM,
            ))
        conn.executemany(
            "INSERT INTO frequencies "
            "(ident, position, mhz, facility, description, priority, source) "
            "VALUES (?,?,?,?,?,?,?)", rows)
        conn.commit()
        return len(rows), max(0, gone)
    finally:
        conn.close()


def _append_override(ident: str, position: str, mhz: float,
                     facility: str, description: str) -> None:
    """Keep the entry, so a rebuild does not throw it away."""
    OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
    exists = OVERRIDES.exists()
    with OVERRIDES.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        if not exists:
            writer.writerow(["ident", "position", "mhz", "facility",
                             "description", "action"])
        writer.writerow([ident, position, f"{mhz:.3f}", facility,
                         description, "add"])


def snap_in_place(db_path: Path | None = None) -> int:
    """Put every unselectable channel in the database back on the grid.

    The build does this on import, so this is for a database built before it
    did. Returns how many rows moved.
    """
    path = Path(db_path) if db_path else Path(DB_PATH)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    moved = 0
    try:
        rows = conn.execute(
            "SELECT rowid, mhz FROM frequencies").fetchall()
        for r in rows:
            if is_valid_channel(r["mhz"]):
                continue
            fixed = round(snap(r["mhz"]), 3)
            if fixed != round(r["mhz"], 3):
                conn.execute("UPDATE frequencies SET mhz = ? WHERE rowid = ?",
                             (fixed, r["rowid"]))
                moved += 1
        conn.commit()
    finally:
        conn.close()
    return moved


__all__ = ["add_frequency", "snap_in_place", "Rejected", "OVERRIDES",
           "POSITIONS"]
