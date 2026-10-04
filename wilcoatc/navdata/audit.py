"""Check the frequency table against what a radio can actually do.

The published data is very good and not perfect. This finds the places where it
is wrong in a way a pilot would notice: a channel no radio can select, a
position listed twice on the same carrier, a field with a tower and nothing to
taxi on, an airport of consequence with no frequencies at all.

It is deliberately about *the data*, not about coverage. A grass strip with no
frequencies is correct; Charles de Gaulle with nine is not.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from .channels import (AIRBAND_HIGH_KHZ, AIRBAND_LOW_KHZ, carrier_khz,
                       channel_name, is_eight_33, is_valid_channel, to_khz)

# What a field of each size ought to have. Below rank 3 an airport may well be
# uncontrolled, so nothing is expected of it.
EXPECTED: dict[int, tuple[str, ...]] = {
    3: ("TWR",),
    4: ("TWR", "GND"),
    5: ("TWR", "GND", "ATIS"),
}

# How many frequencies a field of each size usually carries. Well under this is
# not an error, but it is worth knowing about: it is where the gaps are.
THIN_BELOW: dict[int, int] = {4: 6, 5: 10}


@dataclass
class Finding:
    """One thing worth looking at."""

    kind: str
    ident: str
    detail: str
    severity: str = "warn"      # "error" or "warn" or "note"


@dataclass
class Report:
    counted: int = 0
    airports: int = 0
    eight_33: int = 0
    second_names: int = 0
    findings: list[Finding] = field(default_factory=list)

    def by_kind(self) -> dict[str, list[Finding]]:
        out: dict[str, list[Finding]] = defaultdict(list)
        for finding in self.findings:
            out[finding.kind].append(finding)
        return dict(out)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]


def audit(conn: sqlite3.Connection, deep: bool = False) -> Report:
    """Walk the whole frequency table and report what is wrong with it."""
    report = Report()
    rows = conn.execute(
        "SELECT f.ident, f.position, f.mhz, f.facility, f.description,"
        "       a.size_rank AS rank, a.name AS airport, a.scheduled"
        "  FROM frequencies f JOIN airports a ON a.ident = f.ident"
    ).fetchall()
    report.counted = len(rows)

    by_airport: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_airport[r["ident"]].append(r)
    report.airports = len(by_airport)

    seen_carrier: dict[tuple[str, int], list[sqlite3.Row]] = defaultdict(list)

    for r in rows:
        mhz = r["mhz"]
        khz = to_khz(mhz)

        if not AIRBAND_LOW_KHZ <= khz <= AIRBAND_HIGH_KHZ:
            report.findings.append(Finding(
                "outside the airband", r["ident"],
                f"{r['position']} {mhz:.3f} cannot be tuned in COM1",
                "error"))
            continue

        if not is_valid_channel(mhz):
            report.findings.append(Finding(
                "not a selectable channel", r["ident"],
                f"{r['position']} {mhz:.3f} is not one of the sixteen names a "
                f"block has; nearest is {channel_name(carrier_khz(mhz) / 1000):.3f}",
                "error"))

        if is_eight_33(mhz):
            report.eight_33 += 1
        if khz % 100 in (5, 30, 55, 80):
            report.second_names += 1

        seen_carrier[(r["ident"], carrier_khz(mhz))].append(r)

    # Two stations on one carrier at one field is either a duplicate row or a
    # genuine clash; either way the resolver has to pick and may pick wrong.
    for (ident, khz), group in seen_carrier.items():
        if len(group) < 2:
            continue
        positions = sorted({g["position"] for g in group})
        names = sorted({f"{g['mhz']:.3f}" for g in group})
        if len(positions) == 1 and len(names) == 1:
            report.findings.append(Finding(
                "the same row twice", ident,
                f"{positions[0]} {names[0]} appears {len(group)} times", "warn"))
        elif len(names) > 1:
            report.findings.append(Finding(
                "two names for one carrier", ident,
                f"{' and '.join(names)} are the same transmitter "
                f"({'/'.join(positions)})", "warn"))
        else:
            report.findings.append(Finding(
                "one carrier, several positions", ident,
                f"{names[0]} is {' and '.join(positions)}", "note"))

    for ident, group in by_airport.items():
        rank = group[0]["rank"]
        have = {g["position"] for g in group}
        for expected in EXPECTED.get(rank, ()):
            if expected not in have:
                report.findings.append(Finding(
                    "a position is missing", ident,
                    f"size {rank} field with no {expected}", "warn"))
        thin = THIN_BELOW.get(rank)
        if thin and len(group) < thin:
            report.findings.append(Finding(
                "thin for its size", ident,
                f"{len(group)} frequencies at a size {rank} field "
                f"({group[0]['airport']})", "note"))

    if deep:
        _large_fields_with_nothing(conn, report)
    return report


def _large_fields_with_nothing(conn: sqlite3.Connection, report: Report) -> None:
    """Airports big enough to be controlled that carry no frequency at all."""
    rows = conn.execute(
        "SELECT ident, name, size_rank FROM airports"
        " WHERE size_rank >= 4"
        "   AND ident NOT IN (SELECT DISTINCT ident FROM frequencies)"
    ).fetchall()
    for r in rows:
        report.findings.append(Finding(
            "no frequencies at all", r["ident"],
            f"size {r['size_rank']} field ({r['name']})", "warn"))


def summarise(report: Report) -> list[str]:
    """The report as lines, worst first."""
    lines = [
        f"{report.counted} frequencies at {report.airports} airports",
        f"{report.eight_33} are 8.33 kHz channels; {report.second_names} of "
        f"those are a second name for a 25 kHz carrier",
    ]
    order = {"error": 0, "warn": 1, "note": 2}
    grouped = report.by_kind()
    for kind in sorted(grouped, key=lambda k: (order[grouped[k][0].severity], k)):
        found = grouped[kind]
        lines.append(f"\n{kind}: {len(found)}")
        for finding in found[:12]:
            lines.append(f"    {finding.ident:8s} {finding.detail}")
        if len(found) > 12:
            lines.append(f"    ... and {len(found) - 12} more")
    return lines


__all__ = ["Finding", "Report", "audit", "summarise", "EXPECTED"]
