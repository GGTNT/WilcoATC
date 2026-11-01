"""The airline liveries the simulator's own AI models are painted in.

The detailed model of an invented aeroplane is the simulator's own
(``Asobo PassiveAircraft A320 CEO``), and it was drawn in whatever livery
that title defaults to, because the call that created it took a title and
nothing else. 2024 has a second call, ``AICreateNonATCAircraft_EX1``, that
takes a livery as well, and those models come with real ones -- but only a
few. Listed off a running 2024 (EnumerateSimObjectsAndLiveries, type
AIRCRAFT): every PassiveAircraft has ``<TYPE>_0`` and ``<TYPE>_OFFICIAL``,
and the airliners add a couple of dozen airlines between them -- Qatar,
KLM, Korean, United, Alaska, Iberia, easyJet, Condor, AirAsia, Air India,
Air New Zealand, Aeromexico, Malaysia, S7, Jet2, SunExpress, Tarom,
Brussels, Allegiant, Spirit, Firefly, Discover, Wizz, Republic. Not TAP,
not Ryanair, not British Airways, Air France, Lufthansa, Delta, American,
Southwest or Emirates. Measured on that installation, too: an A320 CEO
created with ``A320_CEO_EASY_JET`` is orange.

So this is a table, and a small one: which listed livery is which airline.
The listing is read live rather than written down, because the names are
not consistent from one type to the next (``A320_CEO_EASY_JET`` and
``A320_NEO_EASYJET``; ``ALASKAN_AIRLINES``, ``ALASKAAIRLINES`` and
``ALASKA_AIRLINES``) and a new simulator update may add some. What is
written down is the airlines: ICAO code to the start of the name, compared
with every non-letter taken out.

Where an airline has no livery on a type this answers "", and the model is
drawn the way it always was. Whether that is acceptable next to FSLTL's
painted model of the same aeroplane is :mod:`wilcoatc.atc.lod`'s question
(``traffic.lod.livery``).
"""

from __future__ import annotations

import re
import zlib

# ICAO code -> how its liveries' names begin, with every non-letter and
# non-digit taken out. Several codes share one paint (the AirAsia group,
# easyJet's three operating companies, Wizz's four).
AIRLINES: dict[str, tuple[str, ...]] = {
    "QTR": ("QATAR",),
    "KLM": ("KLM",),
    "KAL": ("KOREAN",),
    "UAL": ("UNITEDAIRLINES",),
    "ASA": ("ALASKA",),
    "QXE": ("ALASKA",),
    "IBE": ("IBERIA",),
    "EZY": ("EASYJET",),
    "EJU": ("EASYJET",),
    "EZS": ("EASYJET",),
    "CFG": ("CONDOR",),
    "AXM": ("AIRASIA",),
    "AIQ": ("AIRASIA",),
    "AWQ": ("AIRASIA",),
    "APG": ("AIRASIA",),
    "IAD": ("AIRASIA",),
    "XAX": ("AIRASIA",),
    "AIC": ("AIRINDIA",),
    "ANZ": ("AIRNEWZEALAND",),
    "AMX": ("AEROMEXICO",),
    "MAS": ("MALAYSIA",),
    "SBI": ("S7",),
    "EXS": ("JET2",),
    "SXS": ("SUNEXPRESS",),
    "ROT": ("TAROM",),
    "BEL": ("BRUSSELS",),
    "AAY": ("ALLEGIANT",),
    "NKS": ("SPIRIT",),
    "FFM": ("FIREFLY",),
    "OCN": ("DISCOVER",),
    "WZZ": ("WIZZ",),
    "WMT": ("WIZZ",),
    "WUK": ("WIZZ",),
    "WAZ": ("WIZZ",),
    "RPA": ("REPUBLIC",),
}

_NOT_A_LETTER = re.compile(r"[^A-Z0-9]")
# The house paints every model has, which belong to no airline.
_HOUSE = re.compile(r"(?:^|_)(?:0|OFFICIAL(?:_\d+)?)$")


def _plain(text: str) -> str:
    return _NOT_A_LETTER.sub("", text.upper())


class AiLiveries:
    """The simulator's AI liveries, by title and airline."""

    def __init__(self, by_title: dict[str, dict[str, tuple[str, ...]]]
                 | None = None):
        # title -> airline ICAO -> liveries, in the order listed.
        self._by_title = by_title or {}

    @classmethod
    def from_pairs(cls, pairs) -> "AiLiveries":
        """From what the simulator listed: (title, livery) for every one."""
        listed: dict[str, list[str]] = {}
        for title, livery in pairs or ():
            if "PassiveAircraft" in title and livery:
                listed.setdefault(title, []).append(livery)
        by_title: dict[str, dict[str, tuple[str, ...]]] = {}
        for title, liveries in listed.items():
            # Each model's own prefix, read off its blank livery: A320_CEO
            # from A320_CEO_0. Some titles list a sister type's as well.
            prefixes = sorted({l[:-2] for l in liveries if l.endswith("_0")},
                              key=len, reverse=True)
            airlines: dict[str, list[str]] = {}
            for livery in liveries:
                if _HOUSE.search(livery.upper()):
                    continue
                rest = livery
                for prefix in prefixes:
                    if livery.startswith(prefix + "_"):
                        rest = livery[len(prefix) + 1:]
                        break
                name = _plain(rest)
                for icao, starts in AIRLINES.items():
                    if any(name.startswith(start) for start in starts):
                        airlines.setdefault(icao, []).append(livery)
            if airlines:
                by_title[title] = {icao: tuple(found)
                                   for icao, found in airlines.items()}
        return cls(by_title)

    def __len__(self) -> int:
        return len(self._by_title)

    def __bool__(self) -> bool:
        return bool(self._by_title)

    def livery(self, title: str, operator: str, seed: str = "") -> str:
        """The livery to draw this airline's aeroplane in with this title,
        or "" for none. Where it has several (Condor's five colours), one,
        the same one for the same ``seed`` every time."""
        found = self._by_title.get(title, {}).get((operator or "").upper())
        if not found:
            return ""
        if len(found) == 1:
            return found[0]
        return found[zlib.crc32(seed.encode("utf-8")) % len(found)]

    def has(self, title: str, operator: str) -> bool:
        return bool(self.livery(title, operator))


__all__ = ["AiLiveries", "AIRLINES"]
