"""Download the source data the nav database is built from.

Everything here is public-domain or openly licensed and small enough to keep on
a normal machine (about 40 MB of CSV, 25 MB once indexed):

  * OurAirports  -- airports, runways, radio frequencies, navaids. Public
    domain, refreshed nightly, and the frequencies are crowd-sourced from
    official AIP/Chart Supplement data.
  * VATSpy data  -- ARTCC / FIR boundary polygons, so enroute centre calls can
    name the right facility instead of guessing.
  * VatGlasses   -- enroute sectors with their floors and ceilings and the
    positions that work them, which is where London Control, Paris Control
    and Maastricht above Brussels come from: the published dataset has no
    area control frequency for most of Europe. CC BY-NC-SA 4.0, VatGlasses
    contributors. About 26 MB, one file per country, kept apart from the
    program under ``vatglasses/``.

Run ``python -m wilcoatc.navdata.fetch`` to refresh.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Optional

import requests

from ..paths import navdata_dir

# Called with (what is being fetched, bytes so far, bytes expected). A total of
# zero means the server would not say how big it is. Given one of these the
# printing stops: the panel is drawing the bar and two of them is one too many.
Progress = Optional[Callable[[str, int, int], None]]

OURAIRPORTS = "https://davidmegginson.github.io/ourairports-data"
VATSPY = "https://raw.githubusercontent.com/vatsimnetwork/vatspy-data-project/master"

FILES: dict[str, str] = {
    "airports.csv": f"{OURAIRPORTS}/airports.csv",
    "airport-frequencies.csv": f"{OURAIRPORTS}/airport-frequencies.csv",
    "runways.csv": f"{OURAIRPORTS}/runways.csv",
    "navaids.csv": f"{OURAIRPORTS}/navaids.csv",
    "countries.csv": f"{OURAIRPORTS}/countries.csv",
    "regions.csv": f"{OURAIRPORTS}/regions.csv",
    "Boundaries.geojson": f"{VATSPY}/Boundaries.geojson",
    "VATSpy.dat": f"{VATSPY}/VATSpy.dat",
}

VATGLASSES = "https://raw.githubusercontent.com/lennycolton/vatglasses-data/main/data"

# One file per country, named by its ICAO prefixes. Not fss.json: that is
# VATSIM's own top-down cover, which no real sky has.
VATGLASSES_FILES: tuple[str, ...] = (
    "ay", "bi-bg", "bk-la-ld-lj-lq-lw-ly", "da", "db-dg-dx", "df-dr",
    "di-ga-gb-gf-gg-gl-go-gq-gu", "dn", "dt", "eb-el", "ed", "ee", "ef",
    "eg", "eh", "ek", "en", "ep", "es", "ev", "ey", "fa-fd-fx", "fb",
    "fc-fe-fg-fk-fo-fp", "fi-fj", "fl", "fm", "fn", "fq", "fs", "ft",
    "fv", "fw", "fy", "fz", "gc", "gm", "gv", "ha-hd", "hb-hr-ht", "hc",
    "he", "hh", "hj-hs", "hk", "hl", "hu", "lb", "lc", "le", "lf", "lg",
    "lh", "li", "lk", "lm", "lo", "lppc", "lppo", "lr", "ls", "lt", "lu",
    "lx", "lz", "md", "mg-mh-mn-mr-ms-mz", "mk", "mmfo", "mp", "mu",
    "nat", "nc", "nf", "ns", "nt", "nv", "nw", "nz-ni", "ob", "oe", "oi",
    "ok", "om", "oo", "or", "ot", "rc", "rj", "sa", "sbao", "sbaz",
    "sbbs", "sbcw", "sbre", "sc", "se", "sf", "sg", "sip", "sm", "so",
    "su", "sy", "ta-tb-td-tf-tg-tk-tl-tr-tt-tv", "tn", "ub", "uc", "ud",
    "ug", "uhmm", "uiii", "uk", "ulll", "unkl", "unnt", "urrv", "uta",
    "utd", "uz", "v", "vc-vr", "vd-vl-vv", "vh-vm", "vt", "vy", "wb",
    "wi", "wm", "ws", "y-ag-an", "z", "zeg", "zm", "zse", "zsu", "zua",
    "zvr", "zwg"
)

for _name in VATGLASSES_FILES:
    FILES[f"vatglasses/{_name}.json"] = f"{VATGLASSES}/{_name}.json"

DATA_DIR = navdata_dir()


def download(name: str, url: str, dest: Path, timeout: int = 120,
             on_progress: Progress = None) -> Path:
    target = dest / name
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        written = 0
        with tmp.open("wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 16):
                fh.write(chunk)
                written += len(chunk)
                if on_progress is not None:
                    on_progress(name, written, total)
                elif total:
                    pct = 100 * written / total
                    print(f"\r  {name:28} {pct:5.1f}%  {written/1e6:6.1f} MB", end="")
    tmp.replace(target)
    if on_progress is not None:
        on_progress(name, written, written)
    else:
        print(f"\r  {name:28} done   {written/1e6:6.1f} MB")
    return target


# The three the database cannot be built without. The rest are optional: a
# missing boundary file costs the enroute centre its name and nothing else.
REQUIRED: frozenset[str] = frozenset({
    "airports.csv", "airport-frequencies.csv", "runways.csv",
})


def fetch_all(dest: Path = DATA_DIR, force: bool = False,
              on_progress: Progress = None) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if on_progress is None:
        print(f"Downloading navigation data into {dest}")
    for name, url in FILES.items():
        if (dest / name).exists() and not force:
            if on_progress is None:
                size = (dest / name).stat().st_size / 1e6
                print(f"  {name:28} cached {size:6.1f} MB")
            continue
        try:
            download(name, url, dest, on_progress=on_progress)
        except Exception as exc:  # a missing optional file must not be fatal
            if on_progress is None:
                print(f"  {name:28} FAILED: {exc}")
            if name in REQUIRED:
                raise


if __name__ == "__main__":
    fetch_all(force="--force" in sys.argv)
