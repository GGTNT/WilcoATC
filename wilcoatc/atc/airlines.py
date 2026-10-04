"""Airline telephony names, read from ``data/navdata/icao_callsigns.json``.

The hand-written tables in :mod:`speech` hold the eighty-odd operators that
turned up first, and a designator that was in none of them was spelled out:
an airliner filed as TOM2AB called itself "Tango Oscar Mike two Alpha Bravo",
which no airliner does. The file lists every operator a simulator livery is
likely to carry, cargo included, with the telephony designator the controller
actually says, and it sits with the navigation data so a pilot can add a line
to it without rebuilding anything.

The file is the authority for the name. Where it and the built-in tables
disagree, the file wins; what stays above it are the renderings a controller
speaking another language uses ("Air France" at Orly), and whatever the pilot
put in ``airline_names``.

A missing or unreadable file is not an error. The built-in tables still work,
and a broken line in a hand-edited file should cost that line, not the radio.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

FILE_NAME = "icao_callsigns.json"

# Telephony designators that are said as letters. Everything else in the file
# is a word, and is written in capitals only because ICAO Doc 8585 writes it
# that way -- a synthesiser given "SPEEDBIRD" may spell it, so it is handed
# "Speedbird".
_INITIALISMS = frozenset({"KLM", "UPS", "TWA", "CSA", "DHL", "ABX"})


def _candidates() -> list[Path]:
    """Where the file may be: beside the navigation data, then in the bundle.

    The navigation folder is the writable one, so a pilot's edited copy there
    is found before the one that shipped.
    """
    from .. import paths

    return [
        paths.data_root() / "data" / "navdata" / FILE_NAME,
        paths.bundle_root() / "data" / "navdata" / FILE_NAME,
    ]


def spoken_name(written: str) -> str:
    """A telephony designator as a synthesiser should be given it.

    "SPEEDBIRD" -> "Speedbird", "CSA-LINES" -> "CSA Lines", "KLM" -> "KLM".
    The words are the file's own; only the case and the hyphen change, because
    a hyphen is read as a pause and capitals are read as letters.
    """
    words = str(written or "").replace("-", " ").split()
    out = []
    for word in words:
        upper = word.upper()
        if upper in _INITIALISMS or not any(c in "AEIOUY" for c in upper):
            out.append(upper)
        else:
            out.append(word[:1].upper() + word[1:].lower())
    return " ".join(out)


def _load(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return []
    rows = data.get("airlines") if isinstance(data, dict) else data
    return [r for r in (rows or []) if isinstance(r, dict)]


@lru_cache(maxsize=1)
def _table() -> tuple[dict[str, str], dict[str, str]]:
    """(designator -> spoken telephony, lower-case airline name -> designator)."""
    telephony: dict[str, str] = {}
    names: dict[str, str] = {}
    for path in _candidates():
        if not path.is_file():
            continue
        for row in _load(path):
            icao = str(row.get("icao") or "").strip().upper()
            said = spoken_name(row.get("callsign") or "")
            if len(icao) != 3 or not icao.isalpha() or not said:
                continue
            telephony.setdefault(icao, said)
            name = str(row.get("name") or "").strip().lower()
            if name:
                names.setdefault(name, icao)
        break
    return telephony, names


def telephony() -> dict[str, str]:
    """Every designator in the file, with the name a controller says."""
    return dict(_table()[0])


def icao_for_name(name: str) -> str:
    """The designator for an airline the file names ("British Airways"), or ""."""
    return _table()[1].get((name or "").strip().lower(), "")


@lru_cache(maxsize=1)
def _countries() -> dict[str, str]:
    """designator -> the country the file registers the airline in."""
    out: dict[str, str] = {}
    for path in _candidates():
        if not path.is_file():
            continue
        for row in _load(path):
            icao = str(row.get("icao") or "").strip().upper()
            country = str(row.get("country") or "").strip()
            if len(icao) == 3 and icao.isalpha() and country:
                out.setdefault(icao, country)
        break
    return out


def country_of(icao: str) -> str:
    """Where an airline is registered, as the file writes it ("Australie"), or ""."""
    return _countries().get((icao or "").strip().upper(), "")


def reload() -> None:
    """Forget the cached file, so an edit to it counts without a restart."""
    _table.cache_clear()
    _countries.cache_clear()


__all__ = ["telephony", "icao_for_name", "spoken_name", "reload", "FILE_NAME"]
