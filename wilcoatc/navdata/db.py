"""Query layer over the nav database.

The single most important function here is :meth:`NavDB.resolve_station`: the
pilot dials a frequency into COM1 and something has to decide *who answers*.
Getting that right is what makes the radio feel real -- 121.90 is Ground at the
field you are parked at, not the Ground at an airport 400 miles away that
happens to share the frequency.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Sequence

from ..paths import command_hint
from .build import DB_PATH, bearing_deg, haversine_nm
from .channels import same_channel

# How far from the transmitter each kind of station is still plausible, in
# nautical miles. These are line-of-sight/coverage figures, not legal airspace
# limits: a tower will not answer you from 80 miles out, but you can hear a
# centre from a long way away at altitude.
STATION_RANGE_NM: dict[str, float] = {
    "DEL": 8.0,
    "GND": 8.0,
    "TWR": 20.0,
    "CTAF": 25.0,
    "ATIS": 60.0,
    "AWOS": 40.0,
    "APP": 70.0,
    "DEP": 70.0,
    "INFO": 120.0,
    "CTR": 300.0,
}

# The order a flight normally moves through positions, used for handoffs.
DEPARTURE_SEQUENCE = ["DEL", "GND", "TWR", "DEP", "CTR"]
ARRIVAL_SEQUENCE = ["CTR", "APP", "TWR", "GND"]

# What a position is called on the radio. "Delivery" and "Clearance Delivery"
# are both used; the longer form is the initial-callup form.
POSITION_SUFFIX: dict[str, str] = {
    "DEL": "Clearance Delivery",
    "GND": "Ground",
    "TWR": "Tower",
    "APP": "Approach",
    "DEP": "Departure",
    "CTR": "Center",
    "ATIS": "ATIS",
    "AWOS": "Automated Weather",
    "CTAF": "Traffic",
    "INFO": "Radio",
}

# Short form used once two-way contact is established.
POSITION_SUFFIX_SHORT: dict[str, str] = dict(POSITION_SUFFIX, DEL="Delivery")

# Outside US-administered airspace the enroute position is "Control", not
# "Center", and approach control is often "Radar" or "Director".
POSITION_SUFFIX_ICAO: dict[str, str] = dict(POSITION_SUFFIX, CTR="Control")

# What each position is called on a French frequency. "Prévol" is the French
# name for the pre-departure clearance position, and a pilot calling "Orly
# Ground" in French would be calling "Orly Sol".
POSITION_SUFFIX_FR: dict[str, str] = {
    "DEL": "Prévol",
    "GND": "Sol",
    "TWR": "Tour",
    "APP": "Approche",
    "DEP": "Départ",
    "CTR": "Contrôle",
    "ATIS": "ATIS",
    "AWOS": "Météo automatique",
    "CTAF": "Trafic",
    "INFO": "Information",
}

# The same again for the other languages worked on frequency. A controller at
# Fiumicino who has just answered in Italian does not then call themselves
# "Fiume Tower", so the suffix follows the language of the transmission rather
# than the language of the database.
POSITION_SUFFIX_ES: dict[str, str] = {
    "DEL": "Autorizaciones",
    "GND": "Rodadura",
    "TWR": "Torre",
    "APP": "Aproximación",
    "DEP": "Salidas",
    "CTR": "Control",
    "ATIS": "ATIS",
    "AWOS": "Meteorología automática",
    "CTAF": "Tráfico",
    "INFO": "Información",
}

POSITION_SUFFIX_DE: dict[str, str] = {
    "DEL": "Clearance",
    "GND": "Rollkontrolle",
    "TWR": "Turm",
    "APP": "Anflug",
    "DEP": "Abflug",
    "CTR": "Kontrolle",
    "ATIS": "ATIS",
    "AWOS": "Wetter",
    "CTAF": "Verkehr",
    "INFO": "Information",
}

POSITION_SUFFIX_IT: dict[str, str] = {
    "DEL": "Autorizzazioni",
    "GND": "Terra",
    "TWR": "Torre",
    "APP": "Avvicinamento",
    "DEP": "Partenze",
    "CTR": "Controllo",
    "ATIS": "ATIS",
    "AWOS": "Meteo automatico",
    "CTAF": "Traffico",
    "INFO": "Informazioni",
}

POSITION_SUFFIX_PT: dict[str, str] = {
    "DEL": "Autorizações",
    "GND": "Solo",
    "TWR": "Torre",
    "APP": "Aproximação",
    "DEP": "Partidas",
    "CTR": "Controlo",
    "ATIS": "ATIS",
    "AWOS": "Meteorologia automática",
    "CTAF": "Tráfego",
    "INFO": "Informação",
}

# Every language that renames the positions. English is absent on purpose: it
# is the dialect-dependent case below, not a simple table lookup.
POSITION_SUFFIX_BY_LANGUAGE: dict[str, dict[str, str]] = {
    "fr": POSITION_SUFFIX_FR,
    "es": POSITION_SUFFIX_ES,
    "de": POSITION_SUFFIX_DE,
    "it": POSITION_SUFFIX_IT,
    "pt": POSITION_SUFFIX_PT,
}

# Tie-break weight when several stations share a frequency at the same place.
# Lower is preferred, so a controller beats a broadcast and ATIS beats AWOS.
_POSITION_WEIGHT: dict[str, float] = {
    "DEL": 0.0, "GND": 0.0, "TWR": 0.0, "APP": 0.0, "DEP": 0.0, "CTR": 0.0,
    "ATIS": 0.30, "CTAF": 0.45, "AWOS": 0.60, "INFO": 0.70,
}

# ICAO regions that use "Centre"/"Control" and hectopascals rather than the US
# "Center"/inches. Keyed on the first letter(s) of the ICAO identifier.
_FAA_PREFIXES = ("K", "PA", "PH", "PG", "TJ")


def dialect_for(ident: str) -> str:
    """``faa`` inside US-administered airspace, ``icao`` everywhere else."""
    return "faa" if (ident or "").upper().startswith(_FAA_PREFIXES) else "icao"


# Where the enroute position is not the ICAO "Control": Melbourne Centre,
# Toronto Centre; Langen Radar, München Radar, Swiss Radar. Keyed like
# _FAA_PREFIXES.
_CENTRE_SUFFIX_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("Y", "Centre"), ("C", "Centre"),
    ("ED", "Radar"), ("ET", "Radar"), ("LS", "Radar"),
)

# Words that make a frequency row a centre whatever else it is called. The
# name of the centre is what comes before them: "MELBOURNE CENTER
# INFORMATION" is Melbourne.
_CENTRE_WORDS = re.compile(r"\b(?:CENTER|CENTRE|CNTR|ARTCC|ACC|UAC)\b", re.I)
# Words that do not: a country field files its own frequency as "Bairnsdale
# Control", and Munich files its centre as "Munchen Con". Either is a centre
# only when the name is the sector's.
_CONTROL_WORDS = re.compile(r"\b(?:CONTROL|CON|RADAR|RDR)\b", re.I)


def _restyled(name: str) -> str:
    name = " ".join(name.replace(",", " ").split()).strip(" -/()")
    return name.title() if name.isupper() else name


def _folded(name: str) -> str:
    """A name with its spelling differences taken out.

    The boundary data says Muenchen, the frequency rows say Munchen, the
    simulator says München, and all three are one centre.
    """
    import unicodedata
    text = unicodedata.normalize("NFKD", name.strip().lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    for digraph, vowel in (("ue", "u"), ("oe", "o"), ("ae", "a")):
        text = text.replace(digraph, vowel)
    return text


def _names_match(a: str, b: str) -> bool:
    a, b = _folded(a), _folded(b)
    return bool(a and b) and (a in b or b in a)


# The words a centre's own name can already end in.
_OWN_POSITION_WORD = re.compile(r"\b(?:Radar|Information)$", re.I)
# The words a sector's callsign is stripped of, so the language being spoken
# supplies its own: "Paris Control" is Paris Contrôle in French.
_GENERIC_CENTRE_WORD = re.compile(r"\s+(?:Control|Centre|Center)$", re.I)


def _described_centre(description: str) -> str:
    """The centre a description names, with or without saying "centre"."""
    found = _CENTRE_WORDS.search(description) or _CONTROL_WORDS.search(description)
    return _restyled(description[:found.start()] if found else description)


def _answering_centre(facility: str, description: str, field: str) -> str:
    """What a centre row is called when somebody answers on it.

    The simulator files a centre at a country field under the field's name
    -- Goulburn, for MELBOURNE CENTER INFORMATION; Aspen, for DENVER -- and
    the handoff that named the centre was then answered by the field. A row
    named after its own field takes the name its description gives; any
    other keeps its own, less the position word already in it, so "Munchen
    Con" answers as München Radar and not as "Munchen Con Radar". "Dutch"
    is already a centre's name and is left alone.
    """
    if _names_match(facility, field):
        return _described_centre(description) or facility
    found = _CONTROL_WORDS.search(facility) or _CENTRE_WORDS.search(facility)
    if found:
        return _restyled(facility[:found.start()]) or facility
    return facility


def centre_name(facility: str, description: str, sector: str = "") -> str:
    """The centre a frequency row belongs to, or "" if it is not a centre.

    Being filed as a centre is not enough. In Australia the rows filed that
    way include a dozen country fields under their own names and two entries
    for pilot-activated lighting, and the nearest of them was handing the
    cruise to Bairnsdale. A row names a centre by saying so -- "MELBOURNE
    CENTER INFORMATION" -- or by carrying the name of the sector the
    aeroplane is in: "DENVER" filed at Aspen, under Denver's boundary, is
    Denver Center, and "Bairnsdale Control" under Melbourne's is not.
    """
    for text in (description or "", facility or ""):
        found = _CENTRE_WORDS.search(text)
        if found:
            name = _restyled(text[:found.start()])
            if name:
                return name
    if not sector:
        return ""
    for text in (description or "", facility or ""):
        found = _CONTROL_WORDS.search(text)
        name = _restyled(text[:found.start()] if found else text)
        if _names_match(name, sector):
            return name
    return ""


# Where altitudes become flight levels, by region.
#
# Published per terminal area, and there is no open dataset of it, so these
# are the commonest value in each region rather than the exact figure for a
# given field. Being roughly right matters far more than being exactly right:
# the alternative in place of this table was eighteen thousand feet
# everywhere, which is correct in the Americas and nowhere else, and made
# every European aircraft in the program report "one eight thousand" for a
# level it should have called flight level one eight zero.
#
# Order matters: the first prefix that matches wins, so a country carved out
# of a region comes before the region.
_TRANSITION_BY_PREFIX: tuple[tuple[tuple[str, ...], int], ...] = (
    # Argentina: Ezeiza and Aeroparque publish 3000 ft. Not checked field by
    # field for the rest of the country.
    (("SA",), 3000),
    # Brazil: TMAs historically each had their own figure; DECEA's
    # harmonisation project sets 10,000 ft across SISCEAB (ICAO GREPECAS/23
    # working paper). Whether every TMA has changed over is NOT verified --
    # this is the stated national figure, not a per-field one.
    (("SB",), 10000),
    # The rest of South America is still the North American figure, which is
    # unverified for most of it.
    (("K", "PA", "PH", "PG", "TJ", "C", "M", "S"), 18000),  # the Americas
    (("LF",), 5000),        # France: 4,000-5,000 ft across the TMAs
    (("EG",), 6000),        # United Kingdom
    (("ED", "ET"), 5000),   # Germany
    (("LE", "LP"), 6000),   # Iberia
    (("LI",), 6000),        # Italy
    (("EB",), 4500),        # Belgium: skeyes eAIP ENR 2.1, 4500 ft AMSL
    (("EH",), 3000),        # Netherlands: EHAM 3000 ft
    (("LS",), 7000),        # Switzerland
    (("LO",), 5000),        # Austria
    (("EK", "ES", "EN", "EF"), 5000),   # Scandinavia
    (("EI",), 5000),        # Ireland
    (("EP", "LK", "LH", "LZ"), 5000),   # Central Europe
    (("LG", "LT"), 5000),   # Greece and Türkiye
    (("Y",), 10000),        # Australia
    (("NZ",), 13000),       # New Zealand
)

#: Outside every region named above. Feet and flight levels meet here in the
#: Americas, and this is the fallback for anywhere the table does not reach.
DEFAULT_TRANSITION_FT = 18000


def transition_altitude(ident: str) -> int:
    """Where altitudes become flight levels at this field, in feet.

    Read off the ICAO prefix, like the dialect and the registration prefix
    beside it, because the region is the only thing about a field that decides
    this and the only thing an ident reliably says.
    """
    code = (ident or "").upper()
    if not code:
        return DEFAULT_TRANSITION_FT
    for prefixes, altitude in _TRANSITION_BY_PREFIX:
        if code.startswith(prefixes):
            return altitude
    # Not a region this knows. Anywhere US-administered meets at eighteen
    # thousand; the rest of the world is far more often six.
    return DEFAULT_TRANSITION_FT if dialect_for(code) == "faa" else 6000


def transition_level(altitude_ft: int, qnh_hpa: float, ident: str = "") -> int:
    """The lowest flight level in use above a transition altitude, in feet.

    A descent is cleared in flight levels down to this and in altitudes
    below it; the layer in between is never assigned.

    North America: the lowest usable flight level from the altimeter
    setting, 14 CFR 91.121(b) -- FL180 at 29.92 or higher, FL185 down to
    29.42, FL190 down to 28.92, FL195 below that.

    Elsewhere the level is published per terminal area and moves with QNH,
    and there is no open dataset of it. This takes the first whole thousand
    whose flight level, on today's pressure, is at least a thousand feet
    above the transition altitude: FL70 over 6000 ft and FL60 over 5000 or
    4500 at standard pressure. It is an approximation of each state's own
    table, not a copy of it: not verified against any State's publication.
    """
    qnh = float(qnh_hpa or 1013.25)
    if altitude_ft >= DEFAULT_TRANSITION_FT:
        inhg = qnh / 33.8638866667
        if inhg >= 29.92 - 0.005:
            return 18000
        if inhg >= 29.42 - 0.005:
            return 18500
        if inhg >= 28.92 - 0.005:
            return 19000
        return 19500
    # About 27 ft per hectopascal near sea level: a flight level sits that
    # much lower over a low QNH.
    below_standard = (1013.25 - qnh) * 27.0
    wanted = altitude_ft + 1000.0 + max(0.0, below_standard)
    # Fifty feet of slack, so a QNH of 1013 against the 1013.25 standard
    # does not push the level up a whole thousand feet.
    return int(math.ceil((wanted - 50.0) / 1000.0) * 1000)


@dataclass(frozen=True)
class Airport:
    ident: str
    icao: str
    iata: str
    name: str
    spoken: str
    type: str
    lat: float
    lon: float
    elev_ft: float
    country: str
    region: str
    municipality: str
    scheduled: int
    size_rank: int

    @property
    def is_towered(self) -> bool:
        return self.size_rank >= 2

    @property
    def dialect(self) -> str:
        """``faa`` inside US-administered airspace, ``icao`` elsewhere."""
        return "faa" if self.ident.startswith(_FAA_PREFIXES) else "icao"


@dataclass(frozen=True)
class Station:
    """A controller position the pilot can tune and talk to."""

    ident: str            # owning airport, e.g. KJFK
    position: str         # DEL / GND / TWR / APP / DEP / CTR / ATIS / CTAF
    mhz: float
    facility: str         # spoken facility name, e.g. "Kennedy", "New York"
    description: str = ""
    priority: int = 0
    distance_nm: float = 0.0
    dialect: str = "faa"

    def _named(self, table: dict[str, str]) -> str:
        suffix = table.get(self.position, self.position)
        facility = self.facility.strip()
        if not facility:
            return suffix
        # A sector that is called "Maastricht Radar" is not a Maastricht
        # Radar Control, in any language.
        if self.position == "CTR" and _OWN_POSITION_WORD.search(facility):
            return facility
        # Some published names already carry the position ("Langen Radar"),
        # in which case appending "Approach" would be wrong.
        if facility.lower().endswith(suffix.lower()):
            return facility
        return f"{facility} {suffix}"

    def callsign_in(self, language: str = "en", short: bool = False) -> str:
        """How this position is addressed, in a given language."""
        local = POSITION_SUFFIX_BY_LANGUAGE.get((language or "en").lower()[:2])
        if local is not None:
            return self._named(local)
        if self.dialect == "icao":
            if self.position == "CTR":
                for prefix, suffix in _CENTRE_SUFFIX_BY_PREFIX:
                    if self.ident.upper().startswith(prefix):
                        return self._named(dict(POSITION_SUFFIX_ICAO, CTR=suffix))
            return self._named(POSITION_SUFFIX_ICAO)
        return self._named(POSITION_SUFFIX_SHORT if short else POSITION_SUFFIX)

    @property
    def callsign(self) -> str:
        """Full callup name, e.g. ``"Kennedy Tower"``, ``"London Control"``."""
        return self.callsign_in("en")

    @property
    def callsign_short(self) -> str:
        return self.callsign_in("en", short=True)

    @property
    def is_controller(self) -> bool:
        """False for broadcast-only and uncontrolled-field frequencies."""
        return self.position in {"DEL", "GND", "TWR", "APP", "DEP", "CTR"}


@dataclass(frozen=True)
class RunwayPreference:
    """A locally configured preferential runway configuration."""

    runways: list[str]
    max_tailwind_kt: float = 5.0
    # Both Brussels and Schiphol publish 20 kt for their preferential
    # systems, gusts included; used where a row does not give its own.
    max_crosswind_kt: float = 20.0


@dataclass(frozen=True)
class Runway:
    ident: str
    le_ident: str
    he_ident: str
    length_ft: float
    width_ft: float
    surface: str
    lighted: bool
    closed: bool
    le_lat: float | None = None
    le_lon: float | None = None
    le_heading: float | None = None
    he_lat: float | None = None
    he_lon: float | None = None
    he_heading: float | None = None
    # Each threshold's own elevation. The airport's published figure is its
    # highest point, and a sloping runway can be forty feet under it at one
    # end -- which is where an aeroplane aimed at the published figure flew
    # its whole landing roll, at taxi speed, in the air.
    le_elev_ft: float | None = None
    he_elev_ft: float | None = None

    def ends(self) -> list[tuple[str, float | None, float | None, float | None]]:
        """``[(ident, heading, lat, lon), ...]`` for both directions."""
        return [
            (self.le_ident, self.le_heading, self.le_lat, self.le_lon),
            (self.he_ident, self.he_heading, self.he_lat, self.he_lon),
        ]

    @property
    def is_hard(self) -> bool:
        s = (self.surface or "").upper()
        return any(k in s for k in ("ASP", "CON", "PEM", "BIT", "TAR", "PAVED"))


def normalize_runway(ident: str) -> str:
    """Canonical runway key: ``"01L"``, ``"1L"`` and ``"RW01L"`` all match.

    Source data, chart conventions and anything a user types by hand disagree
    about the leading zero and the ``RW`` prefix, so every comparison goes
    through here.
    """
    text = (ident or "").strip().upper().replace(" ", "")
    # "RWY25L" as well as "RW25L": stripping two letters off the first left
    # "Y25L", which matched no runway anywhere.
    for prefix in ("RWY", "RW"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    m = re.match(r"^0*(\d{1,2})([LRCGW]?)$", text)
    if not m:
        return text
    return f"{int(m.group(1)):02d}{m.group(2)}"


def _heading_from_ident(ident: str) -> float | None:
    """Fall back to the runway number when the survey heading is missing."""
    digits = "".join(ch for ch in (ident or "") if ch.isdigit())
    if not digits:
        return None
    try:
        return (int(digits) % 36) * 10.0
    except ValueError:
        return None


class NavDB:
    """Read-only accessor for the nav database."""

    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(
                f"Nav database missing at {self.path}. "
                f"Run: {command_hint('setup')}"
            )
        self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._boundaries: list[tuple[str, float, float, float, float, list]] | None = None
        self._preferences: dict[str, tuple[list[str], list[str], float]] | None = None
        # Per-field descent floors from ``minimum_altitudes.csv``.
        self._minimums: dict[str, int] | None = None

    # ------------------------------------------------------------------
    # airports
    # ------------------------------------------------------------------

    def airport(self, ident: str) -> Airport | None:
        ident = (ident or "").strip().upper()
        if not ident:
            return None
        row = self._conn.execute(
            "SELECT * FROM airports WHERE ident = ?", (ident,)
        ).fetchone()
        if row is None:
            row = self._conn.execute(
                "SELECT * FROM airports WHERE icao = ? OR iata = ? "
                "ORDER BY size_rank DESC LIMIT 1", (ident, ident)
            ).fetchone()
        return self._airport_from_row(row)

    @staticmethod
    def _airport_from_row(row: sqlite3.Row | None) -> Airport | None:
        if row is None:
            return None
        return Airport(
            ident=row["ident"], icao=row["icao"] or "", iata=row["iata"] or "",
            name=row["name"], spoken=row["spoken"], type=row["type"],
            lat=row["lat"], lon=row["lon"], elev_ft=row["elev_ft"] or 0.0,
            country=row["country"] or "", region=row["region"] or "",
            municipality=row["municipality"] or "",
            scheduled=row["scheduled"], size_rank=row["size_rank"],
        )

    def nearest_airports(
        self,
        lat: float,
        lon: float,
        limit: int = 5,
        max_nm: float = 60.0,
        min_rank: int = 0,
        require_runway: bool = False,
    ) -> list[tuple[Airport, float]]:
        """Airports near a position, nearest first, with distance in NM."""
        # Bounding box first so SQLite can use the (lat, lon) index; a degree of
        # latitude is 60 NM, and longitude shrinks by cos(lat).
        dlat = max_nm / 60.0
        coslat = max(0.02, math.cos(math.radians(lat)))
        dlon = min(180.0, max_nm / (60.0 * coslat))
        rows = self._conn.execute(
            """
            SELECT * FROM airports
            WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
              AND size_rank >= ?
            """,
            (lat - dlat, lat + dlat, lon - dlon, lon + dlon, min_rank),
        ).fetchall()

        out: list[tuple[Airport, float]] = []
        for row in rows:
            d = haversine_nm(lat, lon, row["lat"], row["lon"])
            if d <= max_nm:
                out.append((self._airport_from_row(row), d))
        out.sort(key=lambda t: t[1])
        if require_runway:
            out = [t for t in out if self.runways(t[0].ident)]
        return out[:limit]

    def airports_in_box(
        self,
        south: float,
        north: float,
        west: float,
        east: float,
        min_rank: int = 0,
        limit: int = 400,
    ) -> list[Airport]:
        """Every airport inside a rectangle, biggest first.

        For the map, which asks by what is on the screen rather than by what
        is near the aeroplane. Two things it has to get right that a radius
        query never meets:

        *The date line.* A view that spans it has a west edge east of its east
        edge, and a single BETWEEN over that returns nothing at all. It is
        split into two boxes and the halves are put back together.

        *Which airports to drop.* A continent-wide view holds forty thousand
        aerodromes, and drawing them would be a grey smear. The biggest are
        the ones a pilot is looking for at that scale, so the ordering is by
        size and the caller's limit does the rest.
        """
        west = ((float(west) + 180.0) % 360.0) - 180.0
        east = ((float(east) + 180.0) % 360.0) - 180.0
        south = max(-90.0, float(south))
        north = min(90.0, float(north))

        spans = ([(west, 180.0), (-180.0, east)] if west > east
                 else [(west, east)])
        found: list[Airport] = []
        for left, right in spans:
            rows = self._conn.execute(
                """
                SELECT * FROM airports
                WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
                  AND size_rank >= ?
                ORDER BY size_rank DESC, scheduled DESC
                LIMIT ?
                """,
                (south, north, left, right, min_rank, int(limit)),
            ).fetchall()
            found.extend(self._airport_from_row(row) for row in rows)
        found.sort(key=lambda a: (-a.size_rank, -int(a.scheduled), a.ident))
        return found[:int(limit)]

    def boundaries_in_box(
        self,
        south: float,
        north: float,
        west: float,
        east: float,
        limit: int = 60,
    ) -> list[dict]:
        """The flight information regions a rectangle touches.

        These are what a wide view is actually made of. A country outline
        says where the land is; an FIR says whose airspace it is, which is
        the thing that decides who you would be talking to -- and at the zoom
        where individual airports stop being drawable, it is the only honest
        way left to say what you are looking at.
        """
        west = ((float(west) + 180.0) % 360.0) - 180.0
        east = ((float(east) + 180.0) % 360.0) - 180.0
        wrapped = west > east

        # One row per boundary. Several FIRs can name the same polygon --
        # an upper and a lower region over the same ground -- and a plain
        # join hands back the same outline once for each of them, which the
        # map then draws on top of itself.
        rows = self._conn.execute(
            """
            SELECT b.id, b.oceanic, b.min_lat, b.max_lat, b.min_lon,
                   b.max_lon, b.polygon,
                   (SELECT f.name FROM firs f
                     WHERE f.boundary = b.id OR f.icao = b.id
                     LIMIT 1) AS name
            FROM boundaries b
            WHERE b.max_lat >= ? AND b.min_lat <= ?
            """,
            (south, north),
        ).fetchall()

        out: list[dict] = []
        for row in rows:
            if wrapped:
                overlaps = row["max_lon"] >= west or row["min_lon"] <= east
            else:
                overlaps = row["max_lon"] >= west and row["min_lon"] <= east
            if not overlaps:
                continue
            try:
                rings = json.loads(row["polygon"])
            except (TypeError, ValueError):
                continue
            out.append({
                "id": row["id"],
                "name": row["name"] or row["id"],
                "oceanic": bool(row["oceanic"]),
                "rings": rings,
                "lat": (row["min_lat"] + row["max_lat"]) / 2.0,
                "lon": (row["min_lon"] + row["max_lon"]) / 2.0,
            })
            if len(out) >= int(limit):
                break
        return out

    def nearest_airport(self, lat: float, lon: float, **kw) -> Airport | None:
        found = self.nearest_airports(lat, lon, limit=1, **kw)
        return found[0][0] if found else None

    def home_airport(self, lat: float, lon: float) -> Airport | None:
        """The field an aircraft on the ground is most plausibly parked at.

        Prefers a real airport with runways over a heliport 200 ft closer, and
        weights larger fields slightly so that parking on a big airport's ramp
        does not resolve to the private strip across the fence.
        """
        candidates = self.nearest_airports(lat, lon, limit=12, max_nm=12.0)
        if not candidates:
            return None
        scored = []
        for ap, d in candidates:
            if ap.type in ("heliport", "seaplane_base", "balloonport") and d > 0.5:
                continue
            # A larger field wins ties within about 1.5 NM.
            scored.append((d - 0.5 * ap.size_rank, ap))
        if not scored:
            return candidates[0][0]
        scored.sort(key=lambda t: t[0])
        return scored[0][1]

    # ------------------------------------------------------------------
    # frequencies and stations
    # ------------------------------------------------------------------

    def stations(self, ident: str, position: str | None = None) -> list[Station]:
        ident = (ident or "").strip().upper()
        sql = "SELECT * FROM frequencies WHERE ident = ?"
        args: list = [ident]
        if position:
            sql += " AND position = ?"
            args.append(position)
        sql += " ORDER BY priority ASC, mhz ASC"
        return [
            Station(
                ident=r["ident"], position=r["position"], mhz=r["mhz"],
                facility=r["facility"], description=r["description"] or "",
                priority=r["priority"], dialect=dialect_for(r["ident"]),
            )
            for r in self._conn.execute(sql, args).fetchall()
        ]

    # Where a position is missing at a field, this is the order in which a real
    # pilot would look for the next best frequency. Clearance falls to Ground,
    # Ground falls to Tower, an untowered field falls to CTAF, and weather
    # falls from ATIS to the automated station.
    _FALLBACK: dict[str, Sequence[str]] = {
        "DEL": ("DEL", "GND", "TWR", "CTAF"),
        "GND": ("GND", "TWR", "CTAF"),
        "TWR": ("TWR", "CTAF"),
        "DEP": ("DEP", "APP", "CTR"),
        "APP": ("APP", "DEP", "CTR"),
        "CTR": ("CTR", "APP"),
        "ATIS": ("ATIS", "AWOS"),
        "CTAF": ("CTAF", "TWR"),
    }

    def station_on(self, ident: str, mhz: float, position: str) -> Station | None:
        """A station at this field, on a frequency it does not publish.

        Built from what the field is called and how far away it is, so it
        sounds and behaves exactly like a published one. Used only when the
        aeroplane is at the field: it is filling a gap in the data, not
        inventing an airport.
        """
        airport = self.airport(ident)
        if airport is None:
            return None
        published = self.stations(ident)
        if not published:
            return None
        facility = published[0].facility
        return Station(
            ident=airport.ident,
            position=position,
            mhz=round(float(mhz), 3),
            facility=facility,
            description="not in the published data",
            priority=9,
            dialect=airport.dialect,
        )

    def station_for_runway(self, ident: str, position: str,
                           runway: str) -> Station | None:
        """The one of a split position whose description names this runway.

        Big fields publish a tower per runway pair -- Brussels "TWR (RWY
        07R/25L and 01)" and "TWR (RWY 07L/25R or 19)" -- and the handoff
        named whichever came first. Only the runways after "RWY" count, so a
        frequency figure in a description is never read as one. None where
        no description says.
        """
        key = normalize_runway(runway)
        if not key:
            return None
        for station in self.stations(ident, position):
            text = (station.description or "").upper()
            at = text.find("RWY")
            if at < 0:
                continue
            named = {normalize_runway(token) for token in
                     re.findall(r"\b(\d{1,2}[LRC]?)\b", text[at + 3:])}
            if key in named:
                return station
        return None

    def station_for(self, ident: str, position: str) -> Station | None:
        """Best station for a position, following the real-world fallback chain."""
        for candidate in self._FALLBACK.get(position, (position,)):
            found = self.stations(ident, candidate)
            if found:
                return found[0]
        return None

    def frequency_for(self, ident: str, position: str) -> float | None:
        station = self.station_for(ident, position)
        return station.mhz if station else None

    def resolve_station(
        self,
        mhz: float,
        lat: float,
        lon: float,
        altitude_ft: float = 0.0,
        tolerance: float = 0.004,
    ) -> Station | None:
        """Decide who is listening on a tuned frequency at a given position.

        Frequencies repeat constantly -- 121.90 is Ground at hundreds of fields
        -- so candidates are filtered by whether the aircraft is inside that
        kind of station's coverage, then ranked. Nearby ground positions beat a
        distant centre, and at altitude the reception radius grows because the
        limit is line of sight.
        """
        candidates = self.candidate_stations(mhz, lat, lon, altitude_ft, tolerance)
        return candidates[0] if candidates else None

    def candidate_stations(
        self,
        mhz: float,
        lat: float,
        lon: float,
        altitude_ft: float = 0.0,
        tolerance: float = 0.004,
    ) -> list[Station]:
        """All plausible stations on a frequency, best match first.

        The number the radio shows is a channel name, not always a frequency.
        Under 8.33 kHz spacing a 25 kHz carrier has two names -- Paris Ground
        is published as 121.600 and appears on the panel as 121.605 -- so the
        comparison is made on the carrier rather than on the digits. See
        :mod:`wilcoatc.navdata.channels`.
        """
        if not mhz or mhz < 118.0:
            return []

        # Line-of-sight range in NM for a ground station, from altitude AGL.
        los = 1.23 * math.sqrt(max(0.0, altitude_ft)) if altitude_ft > 0 else 0.0

        widest = max(STATION_RANGE_NM.values())
        reach = max(widest, min(los, 400.0))
        dlat = reach / 60.0
        coslat = max(0.02, math.cos(math.radians(lat)))
        dlon = min(180.0, reach / (60.0 * coslat))

        # The window is widened enough to catch the other name for this
        # carrier; which of the rows in it actually count is decided by the
        # carrier comparison below, so nothing adjacent slips through.
        window = max(tolerance, 0.008)
        rows = self._conn.execute(
            """
            SELECT f.*, a.lat AS alat, a.lon AS alon, a.size_rank AS rank,
                   a.spoken AS aspoken
            FROM frequencies f
            JOIN airports a ON a.ident = f.ident
            WHERE f.mhz BETWEEN ? AND ?
              AND a.lat BETWEEN ? AND ? AND a.lon BETWEEN ? AND ?
            """,
            (mhz - window, mhz + window,
             lat - dlat, lat + dlat, lon - dlon, lon + dlon),
        ).fetchall()

        scored: list[tuple[float, Station]] = []
        sector = None                       # looked up once, if a centre is here
        for r in rows:
            if not same_channel(mhz, r["mhz"], int(round(tolerance * 1000))):
                continue
            d = haversine_nm(lat, lon, r["alat"], r["alon"])
            base = STATION_RANGE_NM.get(r["position"], 40.0)
            # Airborne reception extends the usable range, but never for
            # ground-bound positions: you cannot raise Ground from 200 miles.
            limit = base if r["position"] in ("DEL", "GND") else max(base, min(los, base * 4))
            if d > limit:
                continue
            # Rank: closest wins, with a nudge for larger fields and for the
            # published primary frequency over an alternate. The position
            # weight breaks the common tie where one airport publishes the same
            # frequency as both ATIS and an automated weather station -- a
            # controller outranks a broadcast, and ATIS outranks AWOS.
            score = (
                d / max(1.0, base)
                + 0.15 * r["priority"]
                - 0.05 * r["rank"]
                + _POSITION_WEIGHT.get(r["position"], 0.5)
            )
            facility = r["facility"]
            if r["position"] == "CTR":
                facility = _answering_centre(facility, r["description"] or "",
                                             r["aspoken"] or "")
                if sector is None:
                    fir = self.fir_at(lat, lon)
                    sector = (fir or {}).get("spoken") or ""
                facility = self._centre_called(
                    facility, r["description"] or "", sector, d)
            scored.append(
                (
                    score,
                    Station(
                        ident=r["ident"], position=r["position"], mhz=r["mhz"],
                        facility=facility, description=r["description"] or "",
                        priority=r["priority"], distance_nm=d,
                        dialect=dialect_for(r["ident"]),
                    ),
                )
            )
        # Sector positions answer ahead of a field that happens to share the
        # number: the aeroplane is inside their airspace.
        for station in self._sector_candidates(
                mhz, lat, lon, int(round(tolerance * 1000))):
            scored.append((-1.0, station))
        scored.sort(key=lambda t: t[0])
        return [s for _, s in scored]

    def all_stations_near(
        self, lat: float, lon: float, max_nm: float = 40.0
    ) -> list[Station]:
        """Every tunable station within range, for the frequency browser."""
        out: list[Station] = []
        for ap, d in self.nearest_airports(lat, lon, limit=40, max_nm=max_nm):
            for st in self.stations(ap.ident):
                out.append(
                    Station(
                        ident=st.ident, position=st.position, mhz=st.mhz,
                        facility=st.facility, description=st.description,
                        priority=st.priority, distance_nm=d, dialect=st.dialect,
                    )
                )
        out.sort(key=lambda s: (s.distance_nm, s.position))
        return out

    # ------------------------------------------------------------------
    # runways
    # ------------------------------------------------------------------

    @lru_cache(maxsize=512)
    def runways(self, ident: str, include_closed: bool = False) -> tuple[Runway, ...]:
        ident = (ident or "").strip().upper()
        rows = self._conn.execute(
            "SELECT * FROM runways WHERE ident = ?", (ident,)
        ).fetchall()
        out = []
        for r in rows:
            if r["closed"] and not include_closed:
                continue
            if not r["le_ident"] or not r["he_ident"]:
                continue
            out.append(
                Runway(
                    ident=r["ident"], le_ident=r["le_ident"], he_ident=r["he_ident"],
                    length_ft=r["length_ft"] or 0.0, width_ft=r["width_ft"] or 0.0,
                    surface=r["surface"] or "", lighted=bool(r["lighted"]),
                    closed=bool(r["closed"]),
                    le_lat=r["le_lat"], le_lon=r["le_lon"],
                    le_heading=r["le_heading"] if r["le_heading"] is not None
                    else _heading_from_ident(r["le_ident"]),
                    he_lat=r["he_lat"], he_lon=r["he_lon"],
                    he_heading=r["he_heading"] if r["he_heading"] is not None
                    else _heading_from_ident(r["he_ident"]),
                    le_elev_ft=r["le_elev"], he_elev_ft=r["he_elev"],
                )
            )
        out.sort(key=lambda rw: -(rw.length_ft or 0))
        return tuple(out)

    def runway_at(self, ident: str, latitude: float, longitude: float,
                  heading: float | None = None,
                  margin_ft: float = 60.0) -> str:
        """Which runway a point on the surface is standing on, if any.

        The published thresholds and width are enough: a runway is a rectangle
        between its two ends, and the aircraft is on it when it is inside that
        rectangle. ``margin_ft`` widens it a little, because the threshold
        coordinates are the paved end rather than the hold line and an aircraft
        stopped short should not read as an incursion.

        Returns the designator facing the way the aircraft is pointing, so an
        aircraft lined up on 31L is told it is on 31L rather than on 13R.
        Empty when it is not on a runway, which is the normal answer.
        """
        best = ""
        best_offset = float("inf")
        for rw in self.runways(ident):
            if None in (rw.le_lat, rw.le_lon, rw.he_lat, rw.he_lon):
                continue
            along, offset = _segment_position(
                latitude, longitude,
                rw.le_lat, rw.le_lon, rw.he_lat, rw.he_lon,
            )
            half_width_ft = max(float(rw.width_ft or 0.0), 75.0) / 2.0 + margin_ft
            if not (0.0 <= along <= 1.0) or offset > half_width_ft:
                continue
            if offset >= best_offset:
                continue
            best_offset = offset
            best = rw.le_ident
            if heading is not None:
                # Two designators, 180 degrees apart. Take the one the
                # aircraft is facing; a difference over 90 means the other.
                le_heading = rw.le_heading
                if le_heading is None:
                    le_heading = _heading_from_ident(rw.le_ident) or 0.0
                apart = abs(((float(heading) - float(le_heading)) + 180.0) % 360.0 - 180.0)
                best = rw.he_ident if apart > 90.0 else rw.le_ident
        return best

    def runway_within_limits(self, ident: str, runway: str, wind_dir: float,
                             wind_kt: float, gust_kt: float = 0.0,
                             variable: bool = False,
                             max_tailwind_kt: float = 5.0,
                             max_crosswind_kt: float = 20.0) -> bool:
        """Whether the wind leaves a runway usable, gusts included.

        The same limits a preferential runway is held to: a planned runway
        is used while it is inside them and the wind's runway when it is not.
        """
        key = normalize_runway(runway)
        for end, heading, _rw in self.runway_ends(ident):
            if normalize_runway(end) != key:
                continue
            if variable:
                return True
            speed = max(float(wind_kt or 0.0), float(gust_kt or 0.0))
            angle = math.radians(((wind_dir - heading) + 540) % 360 - 180)
            return (-speed * math.cos(angle) <= max_tailwind_kt
                    and abs(speed * math.sin(angle)) <= max_crosswind_kt)
        return False

    def has_runway(self, ident: str, runway: str) -> bool:
        """Whether the field has this landing direction."""
        key = normalize_runway(runway)
        return bool(key) and any(normalize_runway(end) == key
                                 for end, _hdg, _rw in self.runway_ends(ident))

    def runway_ends(self, ident: str) -> list[tuple[str, float, Runway]]:
        """Every usable landing direction as ``(ident, heading, runway)``."""
        ends: list[tuple[str, float, Runway]] = []
        for rw in self.runways(ident):
            for end_ident, hdg, _lat, _lon in rw.ends():
                if hdg is None:
                    hdg = _heading_from_ident(end_ident)
                if hdg is None:
                    continue
                ends.append((end_ident, float(hdg), rw))
        return ends

    def best_runway(
        self,
        ident: str,
        wind_dir: float,
        wind_kt: float,
        prefer_hard: bool = True,
        min_length_ft: float = 0.0,
        departing: bool = False,
        variable: bool = False,
        gust_kt: float = 0.0,
    ) -> tuple[str, Runway] | None:
        """Pick the runway in use for the current wind.

        A local preference from ``preferred_runways.csv`` wins whenever the
        tailwind stays inside its limit, because real airports run a
        preferential configuration that no open dataset publishes. Failing
        that, the choice is the standard one: greatest headwind component,
        breaking ties toward the longest hard surface.

        A ``variable`` wind has no direction, so it is scored as calm: no
        end is favoured by it and the preference, or the length, decides.

        A preference is only ever a preference. It holds while both the
        tailwind and the crosswind on it, gusts included, are inside its
        limits; past either, the runway the wind needs is chosen exactly as
        if there were no preference at all. Only the tailwind used to be
        checked, so Schiphol kept departing 36L into twenty-five knots
        straight across it.
        """
        ends = self.runway_ends(ident)
        if not ends:
            return None
        if variable:
            wind_kt = gust_kt = 0.0
        # The limits are published gusts included.
        limit_kt = max(float(wind_kt or 0.0), float(gust_kt or 0.0))

        preferred = self._preference(ident, departing)
        if preferred:
            # The database and the override file disagree about leading zeros
            # ("1L" vs "01L"), so both are matched on a normalised key.
            by_ident = {normalize_runway(e[0]): (e[0], e[1], e[2]) for e in ends}
            for choice in preferred.runways:
                match = by_ident.get(normalize_runway(choice))
                if match is None:
                    continue
                choice, hdg, rw = match
                angle = math.radians(((wind_dir - hdg) + 540) % 360 - 180)
                tailwind = -limit_kt * math.cos(angle)
                crosswind = abs(limit_kt * math.sin(angle))
                if (tailwind <= preferred.max_tailwind_kt
                        and crosswind <= preferred.max_crosswind_kt):
                    return choice, rw

        best: tuple[float, str, Runway] | None = None
        for end_ident, hdg, rw in ends:
            if min_length_ft and (rw.length_ft or 0) < min_length_ft:
                continue
            if prefer_hard and not rw.is_hard and any(
                r.is_hard for _, _, r in ends
            ):
                continue
            angle = math.radians(((wind_dir - hdg) + 540) % 360 - 180)
            headwind = wind_kt * math.cos(angle)
            crosswind = abs(wind_kt * math.sin(angle))
            # Length is worth a knot of headwind per 1000 ft; enough to break
            # calm-wind ties toward the main runway without overriding real wind.
            score = headwind + (rw.length_ft or 0) / 1000.0 * 0.9 - crosswind * 0.15
            if best is None or score > best[0]:
                best = (score, end_ident, rw)
        if best is None:
            # Every runway was filtered out; fall back to the longest.
            end_ident, _hdg, rw = ends[0]
            return end_ident, rw
        return best[1], best[2]

    def _preference(self, ident: str, departing: bool) -> "RunwayPreference | None":
        self._load_preferences()
        entry = self._preferences.get((ident or "").upper())
        if entry is None:
            return None
        landing, depart, limit, crosswind = entry
        chosen = (depart or landing) if departing else landing
        if not chosen:
            return None
        return RunwayPreference(runways=chosen, max_tailwind_kt=limit,
                                max_crosswind_kt=crosswind)

    def _load_preferences(self) -> None:
        if self._preferences is not None:
            return
        self._preferences = {}
        path = self.path.parent / "preferred_runways.csv"
        if not path.exists():
            return
        import csv

        with path.open(encoding="utf-8", newline="") as fh:
            rows = [ln for ln in fh if not ln.lstrip().startswith("#")]
        for r in csv.DictReader(rows):
            ident = (r.get("ident") or "").strip().upper()
            if not ident:
                continue
            landing = (r.get("landing") or "").upper().split()
            departing = (r.get("departing") or "").upper().split()
            try:
                limit = float(r.get("max_tailwind_kt") or 5.0)
            except ValueError:
                limit = 5.0
            if limit <= 0:
                # Zero used to mean "always prefer", which let a preference
                # override the wind. It now means the usual five knots.
                limit = 5.0
            try:
                crosswind = float(r.get("max_crosswind_kt") or 20.0)
            except ValueError:
                crosswind = 20.0
            self._preferences[ident] = (landing, departing, limit, crosswind)

    def _minimums_path(self) -> Path:
        return self.path.parent / "minimum_altitudes.csv"

    def minimum_altitude(self, ident: str) -> int:
        """The lowest altitude an arrival at this field is descended to.

        Zero when the field has no entry, which leaves the generic floor of
        field elevation plus three thousand feet. There is no open dataset of
        minimum sector altitudes, so this is a hand-kept file beside the
        preferred runways: a pilot flying into Innsbruck writes the MSA off
        the chart once and it holds from then on.
        """
        if self._minimums is None:
            self._minimums = {}
            path = self._minimums_path()
            if path.exists():
                import csv

                with path.open(encoding="utf-8", newline="") as fh:
                    rows = [ln for ln in fh if not ln.lstrip().startswith("#")]
                for r in csv.DictReader(rows):
                    code = (r.get("ident") or "").strip().upper()
                    try:
                        floor = int(float(r.get("floor_ft") or 0))
                    except ValueError:
                        continue
                    if code and floor > 0:
                        self._minimums[code] = floor
        return self._minimums.get((ident or "").strip().upper(), 0)

    def parallel_runways(self, ident: str, end_ident: str) -> list[str]:
        """Other runway ends pointing the same way, e.g. 27L given 27R."""
        target = _heading_from_ident(end_ident)
        if target is None:
            return []
        out = []
        target_key = normalize_runway(end_ident)
        for other, hdg, _rw in self.runway_ends(ident):
            if normalize_runway(other) == target_key:
                continue
            if abs(((hdg - target) + 540) % 360 - 180) <= 12:
                out.append(other)
        return out

    # ------------------------------------------------------------------
    # navaids and fixes
    # ------------------------------------------------------------------

    def navaid(self, ident: str, near: tuple[float, float] | None = None):
        ident = (ident or "").strip().upper()
        rows = self._conn.execute(
            "SELECT * FROM navaids WHERE ident = ?", (ident,)
        ).fetchall()
        if not rows:
            return None
        if near and len(rows) > 1:
            rows = sorted(
                rows, key=lambda r: haversine_nm(near[0], near[1], r["lat"], r["lon"])
            )
        return dict(rows[0])

    def nearest_navaids(
        self, lat: float, lon: float, limit: int = 5, max_nm: float = 120.0,
        types: Iterable[str] = ("VOR", "VORTAC", "VOR-DME", "TACAN", "NDB"),
    ) -> list[tuple[dict, float]]:
        dlat = max_nm / 60.0
        coslat = max(0.02, math.cos(math.radians(lat)))
        dlon = min(180.0, max_nm / (60.0 * coslat))
        wanted = {t.upper() for t in types}
        rows = self._conn.execute(
            "SELECT * FROM navaids WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
            (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
        ).fetchall()
        out = []
        for r in rows:
            if wanted and (r["type"] or "").upper() not in wanted:
                continue
            d = haversine_nm(lat, lon, r["lat"], r["lon"])
            if d <= max_nm:
                out.append((dict(r), d))
        out.sort(key=lambda t: t[1])
        return out[:limit]

    # ------------------------------------------------------------------
    # enroute centres
    # ------------------------------------------------------------------

    def _load_boundaries(self) -> None:
        if self._boundaries is not None:
            return
        self._boundaries = []
        for r in self._conn.execute("SELECT * FROM boundaries"):
            self._boundaries.append(
                (r["id"], r["min_lat"], r["max_lat"], r["min_lon"], r["max_lon"],
                 json.loads(r["polygon"]))
            )

    def fir_at(self, lat: float, lon: float) -> dict | None:
        """The ARTCC / FIR whose boundary contains a position."""
        self._load_boundaries()
        for fid, min_lat, max_lat, min_lon, max_lon, rings in self._boundaries:
            if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
                continue
            if any(_point_in_ring(lon, lat, ring) for ring in rings):
                return self._fir_details(fid)
        return None

    def _fir_details(self, fid: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM firs WHERE icao = ? OR boundary = ? LIMIT 1", (fid, fid)
        ).fetchone()
        if row:
            return {
                "id": fid, "icao": row["icao"], "name": row["name"],
                "spoken": row["spoken"], "callsign": row["callsign"] or row["icao"],
            }
        return {"id": fid, "icao": fid, "name": fid, "spoken": fid, "callsign": fid}

    def center_station(
        self, lat: float, lon: float, altitude_ft: float = 0.0
    ) -> Station | None:
        """The enroute centre for a position, with a real frequency if one exists.

        Centre sector frequencies are not published in any open worldwide
        dataset, so the facility *name* comes from the boundary polygons (which
        is what the pilot hears and reads back) and the *frequency* is taken
        from the nearest airport that publishes a centre frequency. Where no
        such frequency is on file the station is returned with ``mhz`` of 0 and
        the caller keeps the pilot with approach instead of inventing a number.

        The nearest such airport has to be in the same country as the
        aeroplane. It used to be simply the nearest within two hundred and
        fifty miles, and the United Kingdom publishes exactly one centre
        frequency in this data -- an oceanic one at Prestwick -- so a
        departure out of Manchester was handed to "Manchester Control" on
        Dublin's frequency, tuned it, and was answered by Dublin Control from
        a hundred and forty miles away. Nobody then passed the flight on
        again, because nobody was working it. A frequency with no controller
        on it is worse than no frequency: the pilot has left the one station
        that could still hear them.
        """
        sector = self.sector_centre(lat, lon, altitude_ft)
        if sector is not None:
            return sector

        fir = self.fir_at(lat, lon)
        spoken = fir["spoken"] if fir else ""

        found = self._nearest_center_frequency(
            lat, lon, regions=self._regions_at(lat, lon, fir), sector=spoken)
        if found is not None and not self._same_centre(spoken, *found):
            found = None
        if found is not None:
            station, _d = found
            called = self._centre_called(station.facility,
                                         station.description, spoken, _d)
            if called != station.facility:
                station = Station(
                    ident=station.ident, position="CTR", mhz=station.mhz,
                    facility=called,
                    description=station.description, distance_nm=station.distance_nm,
                    dialect=station.dialect,
                )
            return station
        if not spoken:
            return None
        return Station(ident="", position="CTR", mhz=0.0, facility=spoken,
                       dialect=dialect_for((fir or {}).get("icao", "")))

    # How near a borrowed centre frequency has to be published for it to
    # count as this sector's, when the name on it is not the name of the
    # sector the aeroplane is in. A hundred miles is a large terminal area
    # and a small enroute sector; beyond that, a frequency with a different
    # centre's name on it is a different centre.
    CENTER_FREQUENCY_LOCAL_NM = 100.0

    # ------------------------------------------------------------------
    # sectors with levels (VatGlasses)
    # ------------------------------------------------------------------

    def _sector_rows(self, lat: float, lon: float, margin: float = 0.0,
                     owner: str = "") -> list:
        try:
            query = ("SELECT * FROM sectors WHERE min_lat <= ? AND max_lat >= ? "
                     "AND min_lon <= ? AND max_lon >= ?")
            args: list = [lat + margin, lat - margin, lon + margin, lon - margin]
            if owner:
                query += " AND owners LIKE ?"
                args.append(f'%"{owner}"%')
            return self._conn.execute(query, args).fetchall()
        except sqlite3.OperationalError:
            # A database built before sectors existed.
            return []

    def _sector_position(self, key: str):
        try:
            return self._conn.execute(
                "SELECT * FROM sector_positions WHERE key = ?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return None

    @staticmethod
    def _station_for_position(row) -> "Station":
        """One sector position, as the handoff names it and as it answers."""
        facility = _GENERIC_CENTRE_WORD.sub("", row["callsign"].strip())             or row["callsign"]
        prefix = row["prefix"] or ""
        return Station(ident=prefix, position="CTR", mhz=row["mhz"],
                       facility=facility, description=f"sector {row['key']}",
                       dialect=dialect_for(prefix))

    def sector_centre(self, lat: float, lon: float,
                      altitude_ft: float) -> "Station | None":
        """Who works this position at this level, from the sector data.

        The sectors containing the aeroplane at its level, and of each the
        first owner that is an area control position: a tower's own
        airspace lists the tower first and the centre after it, and the
        centre is what is wanted here. Where sectors overlap, the smallest
        is the most particular. None where there is no sector data.

        The owners are VATSIM's order of cover, and the first is usually the
        sector's own controller -- but not always: a Bordeaux sector lists
        Brest first, as the network's cover for it. A sector names its own
        group (Bordeaux), and an owner by that name is the one chosen.
        """
        level = max(0.0, altitude_ft) / 100.0
        best = None
        for row in self._sector_rows(lat, lon):
            if not (row["min_fl"] <= level < row["max_fl"]):
                continue
            rings = json.loads(row["polygon"])
            if not any(_point_in_ring(lon, lat, ring) for ring in rings):
                continue
            area = (row["max_lat"] - row["min_lat"]) * (row["max_lon"] - row["min_lon"])
            centres = [position for position in
                       (self._sector_position(key) for key in json.loads(row["owners"]))
                       if position is not None and position["type"] == "CTR"]
            if not centres:
                continue
            group = row["grp"] if "grp" in row.keys() else ""
            own = [position for position in centres
                   if group and _names_match(
                       _GENERIC_CENTRE_WORD.sub("", position["callsign"]), group)]
            chosen = (own or centres)[0]
            if best is None or area < best[0]:
                best = (area, chosen)
        return self._station_for_position(best[1]) if best else None

    # How far outside its sectors a sector position still answers, in
    # degrees: a pilot dials the next centre a few miles before the line.
    SECTOR_ANSWER_MARGIN_DEG = 0.75

    def _sector_candidates(self, mhz: float, lat: float, lon: float,
                           tolerance_khz: int) -> list["Station"]:
        """Sector positions on this frequency that cover, or nearly cover, here."""
        try:
            rows = self._conn.execute(
                "SELECT * FROM sector_positions WHERE mhz BETWEEN ? AND ?",
                (mhz - 0.008, mhz + 0.008)).fetchall()
        except sqlite3.OperationalError:
            return []
        out = []
        for row in rows:
            if not same_channel(mhz, row["mhz"], tolerance_khz):
                continue
            if self._sector_rows(lat, lon, self.SECTOR_ANSWER_MARGIN_DEG,
                                 owner=row["key"]):
                out.append(self._station_for_position(row))
        return out

    @classmethod
    def _centre_called(cls, name: str, description: str, sector: str,
                       distance: float) -> str:
        """What a centre frequency is called, by the handoff and on the air alike.

        The two used to be named apart: the handoff after the sector the
        aeroplane was in, the answer after the row the number is filed
        under. Told to call Marseille Control, a flight was answered by
        Rhone Control -- RHONE ACC, filed at Istres -- which is two
        controllers for one frequency. Both now come through here.

        A row that is already the sector's keeps its own spelling. One that
        says it is a centre and is near enough to count as this sector's
        (the rule :meth:`_same_centre` admits it by) takes the sector's name.
        Anything else is left as it is filed: a country field's own row
        still answers as the field.
        """
        if not sector or not name or _names_match(name, sector):
            return name
        says_centre = bool(_CENTRE_WORDS.search(description or "")
                           or _CENTRE_WORDS.search(name))
        if says_centre and distance <= cls.CENTER_FREQUENCY_LOCAL_NM:
            return sector
        return name

    @classmethod
    def _same_centre(cls, spoken: str, station: "Station", distance: float) -> bool:
        """Whether a borrowed frequency really belongs to the sector overhead.

        Two ways of being satisfied. The names agree, which is the ordinary
        case -- Denver Center's frequency published at a field inside the
        Denver sector. Or the field is near enough that the name is a local
        variant of the same organisation: Amsterdam's boundary is called
        Amsterdam and its frequency is filed under "Dutch", forty-five miles
        away, and those are one centre.

        What it rejects is the pair that is neither. The one UK centre
        frequency in this data is Shanwick Oceanic, filed at Prestwick; a
        Manchester departure was handed to "Manchester Control" on it, and
        what answered was the wrong ocean.
        """
        if not spoken:
            # No boundary here, so the frequency supplies the name as well as
            # the number. Nothing can disagree with itself.
            return True
        if distance <= cls.CENTER_FREQUENCY_LOCAL_NM:
            return True
        here = spoken.strip().lower()
        theirs = (station.facility or "").strip().lower()
        if not theirs:
            return True
        return here in theirs or theirs in here

    # ICAO regions whose identifiers are one letter rather than two. Every
    # airport in the contiguous United States is K, every one in Canada is C;
    # everywhere else the country is the first two letters. Comparing two
    # letters everywhere made KJFK and KJST different countries, which threw
    # away the centre frequency the American case depends on.
    _ONE_LETTER_REGIONS = frozenset("CKUYZ")

    @classmethod
    def _icao_region(cls, ident: str) -> str:
        """The country an identifier belongs to, as ICAO writes it."""
        ident = (ident or "").upper()
        if not ident:
            return ""
        if ident[0] in cls._ONE_LETTER_REGIONS:
            return ident[0]
        return cls._SAME_COUNTRY.get(ident[:2], ident[:2])

    # Prefixes that are one country. Germany's military fields are ET and
    # its civil ones and boundaries ED, and the Bremen and München centre
    # frequencies are filed at the military ones -- so comparing two letters
    # threw away every German centre there is.
    _SAME_COUNTRY = {"ET": "ED"}

    def _regions_at(self, lat: float, lon: float, fir: dict | None) -> set[str]:
        """Which ICAO regions count as "here", for borrowing a frequency.

        Two answers, and either will do. The field underneath is the direct
        one; the boundary the aeroplane is inside is the one that still works
        over water, and it is also what keeps a sector spanning countries --
        Maastricht over Belgium, say -- from refusing its own frequency.
        """
        regions = set()
        icao = self._icao_region((fir or {}).get("icao") or "")
        if icao:
            regions.add(icao)
        near = self.nearest_airport(lat, lon, max_nm=250.0)
        if near is not None:
            found = self._icao_region(near.ident)
            if found:
                regions.add(found)
        return regions

    def _nearest_center_frequency(
        self, lat: float, lon: float, max_nm: float = 250.0,
        regions: set[str] | None = None, sector: str = "",
    ) -> tuple[Station, float] | None:
        """The nearest row that is a centre, the sector's own first.

        Nearest alone handed the cruise over Victoria to Bairnsdale, filed as
        a centre seventeen miles away, rather than to Melbourne Centre filed
        two hundred miles away. A row has to name a centre (``centre_name``)
        to count, and one naming the sector overhead beats one that is
        merely closer.
        """
        dlat = max_nm / 60.0
        coslat = max(0.02, math.cos(math.radians(lat)))
        dlon = min(180.0, max_nm / (60.0 * coslat))
        rows = self._conn.execute(
            """
            SELECT f.*, a.lat AS alat, a.lon AS alon
            FROM frequencies f JOIN airports a ON a.ident = f.ident
            WHERE f.position = 'CTR'
              AND a.lat BETWEEN ? AND ? AND a.lon BETWEEN ? AND ?
            """,
            (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
        ).fetchall()
        best = None
        best_rank = None
        for r in rows:
            if regions and self._icao_region(r["ident"]) not in regions:
                continue
            name = centre_name(r["facility"], r["description"] or "", sector)
            if not name:
                continue
            d = haversine_nm(lat, lon, r["alat"], r["alon"])
            rank = (0 if _names_match(name, sector) else 1, d)
            if best_rank is None or rank < best_rank:
                best_rank = rank
                best = (
                    Station(
                        ident=r["ident"], position="CTR", mhz=r["mhz"],
                        facility=name, description=r["description"] or "",
                        distance_nm=d, dialect=dialect_for(r["ident"]),
                    ),
                    d,
                )
        return best

    # ------------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()


def _segment_position(
    lat: float, lon: float,
    lat1: float, lon1: float, lat2: float, lon2: float,
) -> tuple[float, float]:
    """Where a point sits relative to a line between two others.

    Returns ``(along, offset_ft)``: how far down the segment the point is as a
    fraction, and how far it is from the line sideways. A runway is under two
    nautical miles long, so a flat projection is exact to well under the width
    of the pavement and avoids a spherical solution nothing here needs.
    """
    import math

    mid = math.radians((lat1 + lat2) / 2.0)
    # Feet per degree at this latitude.
    fx = 364_000.0 * math.cos(mid)
    fy = 364_000.0

    ax, ay = lon1 * fx, lat1 * fy
    bx, by = lon2 * fx, lat2 * fy
    px, py = lon * fx, lat * fy

    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 0.0:
        return 0.0, math.hypot(px - ax, py - ay)

    along = ((px - ax) * dx + (py - ay) * dy) / length_sq
    # Perpendicular distance to the infinite line through the two ends.
    offset = abs((px - ax) * dy - (py - ay) * dx) / math.sqrt(length_sq)
    return along, offset


def _point_in_ring(x: float, y: float, ring: Sequence[Sequence[float]]) -> bool:
    """Ray-casting point-in-polygon. ``ring`` is a list of ``[lon, lat]``."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y):
            if x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
                inside = not inside
        j = i
    return inside


__all__ = [
    "NavDB", "Airport", "Station", "Runway", "RunwayPreference", "dialect_for", "normalize_runway",
    "STATION_RANGE_NM", "POSITION_SUFFIX", "POSITION_SUFFIX_FR",
    "POSITION_SUFFIX_ES", "POSITION_SUFFIX_DE", "POSITION_SUFFIX_IT",
    "POSITION_SUFFIX_PT", "POSITION_SUFFIX_BY_LANGUAGE",
    "DEPARTURE_SEQUENCE", "ARRIVAL_SEQUENCE",
    "haversine_nm", "bearing_deg",
]
