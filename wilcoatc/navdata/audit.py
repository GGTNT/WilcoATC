"""Check the frequency table against what a radio can actually do.

The published data is very good and not perfect. This finds the places where it
is wrong in a way a pilot would notice: a channel no radio can select, a
position listed twice on the same carrier, a field with a tower and nothing to
taxi on, an airport of consequence with no frequencies at all.

It is deliberately about *the data*, not about coverage. A grass strip with no
frequencies is correct; Charles de Gaulle with nine is not.

The en-route half (:func:`fir_coverage`, :func:`summarise_firs`) is the
exception, because there the gap is hidden on purpose. The en-route
controller (:mod:`wilcoatc.atc.enroute`) never leaves a FIR silent: one with
no row in ``fir_frequencies.csv`` borrows a centre row filed at a field that
names it, and failing that is given a frequency invented from its id. That
keeps the flight talking, and the only way to learn which FIRs were worked
on a made-up number was to fly through them and read the log. So this lists
where every FIR's frequency comes from, the outlines VATSpy.dat does not
name, and what the three routes the suite flies cross, so the next rows
typed into the file are the ones that matter.
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


@dataclass
class Coverage:
    """How much of the VFR data there is: fields with a published circuit
    side or height, fields with reporting points (and how many of those can
    be said on the radio), fields an airspace volume belongs to."""

    circuit_fields: int = 0
    circuit_sides: int = 0
    circuit_heights: int = 0
    vrp_fields: int = 0
    spoken_vrp_fields: int = 0
    airspace_volumes: int = 0
    airspace_fields: int = 0
    by_class: dict = field(default_factory=dict)


def vfr_coverage(navdb) -> Coverage:
    """The coverage of circuits.csv, vrps.csv and airspace.csv and their
    generated counterparts (navdata.vfrdata)."""
    from .vfrdata import load_circuits, load_vrps

    report = Coverage()
    circuits = load_circuits(navdb._vfr_files("circuits"))
    report.circuit_fields = len({ident for ident, _ in circuits})
    report.circuit_sides = sum(1 for c in circuits.values() if c.side)
    report.circuit_heights = sum(1 for c in circuits.values() if c.height_ft_agl)
    points = load_vrps(navdb._vfr_files("vrps"))
    report.vrp_fields = len(points)
    report.spoken_vrp_fields = sum(1 for found in points.values()
                                   if any(p.spoken for p in found))
    volumes = navdb.airspace_volumes()
    report.airspace_volumes = len(volumes)
    report.airspace_fields = len({v.airport for v in volumes if v.airport})
    counts: dict = defaultdict(int)
    for v in volumes:
        counts[v.cls] += 1
    report.by_class = dict(sorted(counts.items()))
    return report


def summarise_coverage(report: Coverage) -> list[str]:
    return [
        f"circuits: {report.circuit_fields} fields "
        f"({report.circuit_sides} runway ends with a side, "
        f"{report.circuit_heights} with a height)",
        f"reporting points: {report.vrp_fields} fields "
        f"({report.spoken_vrp_fields} with points a controller can say)",
        f"airspace: {report.airspace_volumes} volumes belonging to "
        f"{report.airspace_fields} fields, by class "
        + ", ".join(f"{k} {v}" for k, v in report.by_class.items()),
    ]


# ----------------------------------------------------------------------
# the approach areas: which fields with an approach have none
# ----------------------------------------------------------------------

# Large airports (airports.size_rank 3, the top of its scale) are listed by
# name when they have an approach control and no area; the rest are only
# counted.
APPROACH_LIST_RANK = 3


@dataclass
class ApproachCoverage:
    version: str = ""
    areas: int = 0
    fields: int = 0             # fields with an approach or departure frequency
    with_area: int = 0
    missing: list = field(default_factory=list)   # (ident, name, rank), largest first


def approach_coverage(navdb) -> ApproachCoverage:
    """Which fields with an APP or DEP frequency fall in no approach area
    (navdata.approach_areas): their arrivals are handed over at sixty miles
    and their departures at 30 nm or 17,000 ft, as before the areas."""
    loaded = navdb.approach_areas()
    report = ApproachCoverage(version=loaded.version, areas=len(loaded.areas))
    rows = navdb._conn.execute(
        "SELECT DISTINCT a.ident, a.name, a.lat, a.lon, a.size_rank "
        "FROM airports a JOIN frequencies f ON f.ident = a.ident "
        "WHERE f.position IN ('APP', 'DEP')").fetchall()
    for ident, name, lat, lon, rank in rows:
        report.fields += 1
        if loaded.for_field(ident, lat, lon) is not None:
            report.with_area += 1
        else:
            report.missing.append((ident, name, int(rank or 0)))
    report.missing.sort(key=lambda m: (-m[2], m[0]))
    return report


def summarise_approaches(report: ApproachCoverage) -> list[str]:
    big = [m for m in report.missing if m[2] >= APPROACH_LIST_RANK]
    lines = [
        f"approach areas: {report.areas} areas"
        + (f" (SimAware {report.version})" if report.version else "")
        + f", {report.with_area} of {report.fields} fields with an approach "
        f"control inside one; {len(report.missing)} handed over by distance",
    ]
    if big:
        lines.append(f"  large fields with no area ({len(big)}): "
                     + ", ".join(ident for ident, _, _ in big))
    return lines


# ----------------------------------------------------------------------
# en route: where each FIR's frequency comes from
# ----------------------------------------------------------------------

# The routes the suite flies, as fields and waypoints: the same tracks as
# tests/test_enroute_wsss_vhhh.py, which holds the hand-offs along them.
ROUTES: dict[str, tuple] = {
    "WSSS-VHHH": ("WSSS", (2.6, 105.0), (5.0, 106.6), (11.0, 109.6),
                  (17.5, 112.6), "VHHH"),
    "EBBR-LIRF": ("EBBR", (49.6, 4.6), (47.0, 6.6), (45.5, 8.0),
                  (43.5, 10.3), "LIRF"),
    "KJFK-KORD": ("KJFK", (41.1, -74.6), (41.5, -78.0), (41.8, -82.0),
                  (41.9, -85.5), "KORD"),
}

# How finely a route is walked for the FIRs it crosses. A FIR narrower than
# this along the track can be missed; none on the test routes is.
ROUTE_STEP_NM = 10.0

FROM_FILE = "fir_frequencies.csv"
BORROWED = "OurAirports centre row"


@dataclass(frozen=True)
class FirCoverage:
    id: str
    name: str
    named: bool                 # the FIR table names it (VATSpy.dat)
    oceanic: bool
    source: str                 # FAA NASR, FROM_FILE, BORROWED, HF, invented, none
    frequencies: tuple = ()


def _fir_source(sectors) -> str:
    if any(s.source == "faa" for s in sectors):
        return "FAA NASR"
    fir = [s for s in sectors if s.source == "fir"]
    if any(s.frequencies and s.frequency_source not in (BORROWED, "invented")
           for s in fir):
        return FROM_FILE
    if any(s.hf_khz for s in fir):
        return "HF"
    for s in fir:
        if s.invented:
            return "invented"
        if s.frequency_source == BORROWED:
            return BORROWED
    return "none"


def fir_coverage(navdb) -> list[FirCoverage]:
    """Every FIR outline, with where the frequency it is worked on is from."""
    out = []
    for fid in navdb.fir_ids():
        fir = navdb.fir(fid)
        sectors = navdb.enroute.sectors_of(fid)
        freqs = tuple(dict.fromkeys(f for s in sectors for f in s.frequencies))
        out.append(FirCoverage(fid, fir.get("name") or fid,
                               bool(fir.get("named")), bool(fir.get("oceanic")),
                               _fir_source(sectors), freqs))
    return out


def firs_crossed(navdb, route) -> list[str]:
    """The FIRs a route of fields and (lat, lon) points crosses, in order."""
    from .build import bearing_deg, haversine_nm, project

    points = []
    for item in route:
        if isinstance(item, str):
            airport = navdb.airport(item)
            if airport is not None:
                points.append((airport.lat, airport.lon))
        else:
            points.append((float(item[0]), float(item[1])))
    seen: list[str] = []
    for (lat, lon), (lat2, lon2) in zip(points, points[1:]):
        steps = max(1, int(haversine_nm(lat, lon, lat2, lon2) // ROUTE_STEP_NM))
        for i in range(steps + 1):
            if i:
                left = haversine_nm(lat, lon, lat2, lon2)
                lat, lon = project(lat, lon, bearing_deg(lat, lon, lat2, lon2),
                                   min(ROUTE_STEP_NM, left))
            fir = navdb.fir_at(lat, lon)
            if fir is not None and (not seen or seen[-1] != fir["id"]):
                seen.append(fir["id"])
    return seen


def summarise_firs(navdb, routes: dict | None = None) -> list[str]:
    routes = ROUTES if routes is None else routes
    rows = fir_coverage(navdb)
    by_id = {r.id: r for r in rows}

    def counted(found) -> str:
        counts = defaultdict(int)
        for r in found:
            counts[r.source] += 1
        return ", ".join(f"{n} {source}" for source, n in
                         sorted(counts.items(), key=lambda kv: -kv[1]))

    unnamed = [r.id for r in rows if not r.named]
    invented = [r.id for r in rows if r.source == "invented"]
    silent = [r.id for r in rows if r.source == "none"]
    lines = [
        f"FIRs: {len(rows)} ({counted(rows)})",
        f"  no callsign (an outline VATSpy.dat does not name): {len(unnamed)}"
        f" {', '.join(unnamed)}".rstrip(),
        f"  invented frequency: {len(invented)} {', '.join(invented)}".rstrip(),
        f"  oceanic with no HF row: {len(silent)} {', '.join(silent)}".rstrip(),
    ]
    for name, route in routes.items():
        ids = firs_crossed(navdb, route)
        lines.append(f"{name}: {len(ids)} FIRs "
                     f"({counted(by_id[i] for i in ids if i in by_id)})")
        for i in ids:
            row = by_id.get(i)
            freqs = " ".join(f"{f:.3f}" for f in row.frequencies) if row else ""
            lines.append(f"  {i:<10} {row.source if row else '?':<24} {freqs}")
    return lines


__all__ = ["Coverage", "FirCoverage", "Finding", "Report", "ROUTES", "audit",
           "fir_coverage", "firs_crossed", "summarise", "summarise_coverage",
           "summarise_firs", "vfr_coverage", "EXPECTED"]
