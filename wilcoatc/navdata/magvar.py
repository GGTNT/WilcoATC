"""Magnetic variation, anywhere, offline.

Observations and the navigation data are true; what a controller says is
magnetic. A METAR reports the wind from true north, and an ATIS and a tower
read it from magnetic north (ICAO Annex 11 4.3.6 and Doc 4444 6.6; FAA JO
7110.65 2-6-4 and 2-9-3), and a runway's number is its magnetic heading. The
program read the true figure aloud and compared true headings with runway
numbers, which is ten degrees out at Kennedy and twenty in Iceland.

Where the aeroplane is, the simulator answers directly: the true and magnetic
headings it reports differ by the variation. A field has no such reading, and
the navigation database dropped the per-navaid variation on the way in, so
this evaluates the World Magnetic Model. The coefficients are NOAA's WMM2025
(``WMM_2025.COF``, NOAA/NCEI and the British Geological Survey, a US
government work in the public domain), valid 2025.0-2030.0; the evaluator is
the standard spherical-harmonic sum to degree 12 that NOAA's own reference C
code performs, rewritten here so it costs no dependency. Checked against an
independent implementation to well under a tenth of a degree.

Outside the model's five years the coefficients still give a usable figure
for a couple of years, drifting by a few tenths of a degree a year; the
answer is a spoken whole number of tens, so that is acceptable until the
next model is dropped in beside this one.
"""

from __future__ import annotations

import math
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

COEFFICIENTS = Path(__file__).with_name("WMM_2025.COF")
MAX_ORDER = 12

# WGS-84, and the model's reference radius, in kilometres.
_A = 6378.137
_B = 6356.7523142
_RE = 6371.2

_lock = threading.Lock()
_model: "_Model | None" = None


class _Model:
    """The coefficients, unnormalised once, ready to be summed."""

    def __init__(self, path: Path):
        size = MAX_ORDER + 1
        self.c = [[0.0] * size for _ in range(size)]
        self.cd = [[0.0] * size for _ in range(size)]
        self.k = [[0.0] * size for _ in range(size)]
        snorm = [[0.0] * size for _ in range(size)]
        self.fn = [0.0] * size
        self.fm = [0.0] * size

        lines = path.read_text(encoding="ascii").splitlines()
        self.epoch = float(lines[0].split()[0])
        for line in lines[1:]:
            parts = line.split()
            if len(parts) < 6 or parts[0].startswith("9999"):
                break
            n, m = int(parts[0]), int(parts[1])
            g, h, dg, dh = (float(x) for x in parts[2:6])
            if m <= n:
                self.c[m][n] = g
                self.cd[m][n] = dg
                if m != 0:
                    self.c[n][m - 1] = h
                    self.cd[n][m - 1] = dh

        # Schmidt semi-normalised to unnormalised, and the recursion terms.
        snorm[0][0] = 1.0
        self.fn[0] = 1.0
        for n in range(1, size):
            snorm[0][n] = snorm[0][n - 1] * (2 * n - 1) / n
            j = 2.0
            for m in range(0, n + 1):
                self.k[m][n] = (((n - 1) ** 2) - m ** 2) / ((2 * n - 1) * (2 * n - 3))
                if m > 0:
                    flnmj = ((n - m + 1) * j) / (n + m)
                    snorm[m][n] = snorm[m - 1][n] * math.sqrt(flnmj)
                    j = 1.0
                    self.c[n][m - 1] *= snorm[m][n]
                    self.cd[n][m - 1] *= snorm[m][n]
                self.c[m][n] *= snorm[m][n]
                self.cd[m][n] *= snorm[m][n]
            self.fn[n] = float(n + 1)
            self.fm[n] = float(n)
        self.k[1][1] = 0.0

    def declination(self, lat: float, lon: float, alt_km: float,
                    year: float) -> float:
        size = MAX_ORDER + 1
        dt = year - self.epoch
        rlat, rlon = math.radians(lat), math.radians(lon)
        srlat, crlat = math.sin(rlat), math.cos(rlat)
        srlon, crlon = math.sin(rlon), math.cos(rlon)
        srlat2, crlat2 = srlat * srlat, crlat * crlat

        # Geodetic to geocentric.
        a2, b2 = _A * _A, _B * _B
        c2 = a2 - b2
        a4, b4 = a2 * a2, b2 * b2
        c4 = a4 - b4
        q = math.sqrt(a2 - c2 * srlat2)
        q1 = alt_km * q
        q2 = ((q1 + a2) / (q1 + b2)) ** 2
        ct = srlat / math.sqrt(q2 * crlat2 + srlat2)
        st = math.sqrt(max(0.0, 1.0 - ct * ct))
        r2 = alt_km * alt_km + 2.0 * q1 + (a4 - c4 * srlat2) / (q * q)
        r = math.sqrt(r2)
        d = math.sqrt(a2 * crlat2 + b2 * srlat2)
        ca = (alt_km + d) / r
        sa = c2 * crlat * srlat / (r * d)

        sp = [0.0] * size
        cp = [1.0] * size
        sp[1], cp[1] = srlon, crlon
        for m in range(2, size):
            sp[m] = sp[1] * cp[m - 1] + cp[1] * sp[m - 1]
            cp[m] = cp[1] * cp[m - 1] - sp[1] * sp[m - 1]

        p = [[0.0] * size for _ in range(size)]
        dp = [[0.0] * size for _ in range(size)]
        pp = [0.0] * size
        p[0][0] = 1.0
        pp[0] = 1.0
        aor = _RE / r
        ar = aor * aor
        br = bt = bp = bpp = 0.0
        for n in range(1, size):
            ar *= aor
            for m in range(0, n + 1):
                if n == m:
                    p[m][n] = st * p[m - 1][n - 1]
                    dp[m][n] = st * dp[m - 1][n - 1] + ct * p[m - 1][n - 1]
                elif n == 1 and m == 0:
                    p[m][n] = ct * p[m][n - 1]
                    dp[m][n] = ct * dp[m][n - 1] - st * p[m][n - 1]
                elif n > 1:
                    if m > n - 2:
                        p[m][n - 2] = 0.0
                        dp[m][n - 2] = 0.0
                    p[m][n] = ct * p[m][n - 1] - self.k[m][n] * p[m][n - 2]
                    dp[m][n] = (ct * dp[m][n - 1] - st * p[m][n - 1]
                                - self.k[m][n] * dp[m][n - 2])
                g = self.c[m][n] + dt * self.cd[m][n]
                h = (self.c[n][m - 1] + dt * self.cd[n][m - 1]) if m else 0.0
                par = ar * p[m][n]
                temp1 = g * cp[m] + h * sp[m]
                temp2 = g * sp[m] - h * cp[m]
                bt -= ar * temp1 * dp[m][n]
                bp += self.fm[m] * temp2 * par
                br += self.fn[n] * temp1 * par
                if st == 0.0 and m == 1:
                    pp[n] = pp[n - 1] if n == 1 else (
                        ct * pp[n - 1] - self.k[m][n] * pp[n - 2])
                    bpp += self.fm[m] * temp2 * ar * pp[n]
        bp = bpp if st == 0.0 else bp / st
        bx = -bt * ca - br * sa
        by = bp
        return math.degrees(math.atan2(by, bx))


def _loaded() -> "_Model | None":
    global _model
    with _lock:
        if _model is None and COEFFICIENTS.exists():
            _model = _Model(COEFFICIENTS)
        return _model


def _decimal_year(when: datetime | None = None) -> float:
    when = when or datetime.now(timezone.utc)
    start = datetime(when.year, 1, 1, tzinfo=timezone.utc)
    end = datetime(when.year + 1, 1, 1, tzinfo=timezone.utc)
    return when.year + (when - start).total_seconds() / (end - start).total_seconds()


@lru_cache(maxsize=4096)
def _cached(lat_key: int, lon_key: int, year_key: int) -> float:
    model = _loaded()
    if model is None:
        return 0.0
    return model.declination(lat_key / 100.0, lon_key / 100.0, 0.0,
                             year_key / 10.0)


def variation(lat: float, lon: float, when: datetime | None = None) -> float:
    """Magnetic variation in degrees, east positive, at sea level.

    Zero when the coefficients are missing, which is the old behaviour rather
    than a wrong one. Keyed to a hundredth of a degree and a tenth of a year,
    because every field is asked about over and over and the answer does not
    move inside either.
    """
    return _cached(int(round(lat * 100)), int(round(lon * 100)),
                   int(round(_decimal_year(when) * 10)))


def to_magnetic(true_deg: float, variation_deg: float) -> float:
    """A true bearing as a magnetic one: east variation is subtracted."""
    return (float(true_deg) - float(variation_deg)) % 360.0


def to_true(magnetic_deg: float, variation_deg: float) -> float:
    return (float(magnetic_deg) + float(variation_deg)) % 360.0


__all__ = ["variation", "to_magnetic", "to_true", "COEFFICIENTS"]
