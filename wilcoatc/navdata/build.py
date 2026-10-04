"""Build the queryable nav database from the downloaded CSVs.

Three things happen here that matter for realism:

1. **Frequencies are filtered to the VHF airband** (118.000-136.975 MHz). The
   raw data mixes in VOR-collocated ATIS and military UHF, and a frequency the
   pilot physically cannot dial into COM1 is worse than no frequency at all.

2. **Position types are normalised.** The source uses 30-odd tags (CLD, CLNC,
   DIR, RDR, A/D, CNTR ...) that collapse onto the handful of control positions
   an aircraft is actually handed between.

3. **Spoken facility names are derived.** A controller says "Kennedy Tower",
   never "John F Kennedy International Airport Tower". The frequency
   description in the source data usually carries the real facility name, so it
   is parsed first and the airport name is only a fallback.
"""

from __future__ import annotations

import csv
import json
import math
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

from . import channels
from ..paths import bundle_root, navdata_dir

ROOT = bundle_root()
DATA_DIR = navdata_dir()
DB_PATH = DATA_DIR / "nav.sqlite"

# Civil VHF air-band. Anything outside it cannot be tuned in a COM radio.
VHF_MIN, VHF_MAX = 118.0, 136.975

# Source tag -> control position. Positions not listed are dropped: airport
# operations, military PMSV, weather teletype and the like are never a
# controller an aircraft talks to.
TYPE_MAP: dict[str, str] = {
    # Clearance delivery. "PFLT"/"PRE-TAXI" is the pre-departure clearance
    # position, which is what Paris-Orly and much of Europe files it under.
    "CLD": "DEL", "CLNC": "DEL", "DEL": "DEL", "PDC": "DEL",
    "PFLT": "DEL", "PRE-TAXI": "DEL", "PRETAXI": "DEL", "DEL/GND": "DEL",
    # Ground, including combined ground/clearance and apron positions.
    "GND": "GND", "GROUND": "GND", "RMP": "GND", "APRON": "GND",
    "GCCD": "GND", "GTE": "GND", "GND/TWR": "GND",
    "TWR": "TWR", "TOWER": "TWR", "TWR/GND": "TWR",
    # Approach control, under all the names it is filed as.
    "APP": "APP", "ARR": "APP", "DIR": "APP", "RDR": "APP",
    "GCA": "APP", "PAR": "APP", "A/D": "APP", "APRC": "APP",
    "TMA": "APP", "TCA": "APP", "RADAR": "APP", "RAD/APP": "APP",
    "APP/DEP": "APP", "TML": "APP", "TRML": "APP",
    "DEP": "DEP", "DEPC": "DEP",
    "ATIS": "ATIS", "D-ATIS": "ATIS", "DATIS": "ATIS",
    "CNTR": "CTR", "CTR": "CTR", "ACC": "CTR", "CENTER": "CTR",
    # Not "PAL": in Australia that is pilot-activated lighting, and filing it
    # as a centre handed the cruise to "Pilot Activated Lighting Control".
    "ARTC": "CTR", "ARTCC": "CTR", "UAC": "CTR",
    # Uncontrolled-field common frequencies. "MF" is the Canadian mandatory
    # frequency, TIBA and SAFETYCOM the European equivalents.
    "UNIC": "CTAF", "UNICOM": "CTAF", "CTAF": "CTAF", "ATF": "CTAF",
    "MULTICOM": "CTAF", "MF": "CTAF", "TIBA": "CTAF", "A/A": "CTAF",
    "SAFETYCOM": "CTAF", "SAFETY COMM": "CTAF",
    "AWOS": "AWOS", "ASOS": "AWOS", "AWIB": "AWOS", "AWIS": "AWOS",
    "FSS": "INFO", "RCO": "INFO", "RDO": "INFO", "RADIO": "INFO",
    "A/G": "INFO", "INFO": "INFO", "AFIS": "INFO", "FIS": "INFO",
    "AAS": "INFO", "VDF": "INFO",
    # Deliberately not mapped, and dropped: MISC, OPS, PMSV, ACP and POST are
    # military or airport operations rather than ATC, EMR is the 121.5 distress
    # frequency, and VOLMET is a recorded weather broadcast. None of them is a
    # controller an aircraft can call.
}

# Words stripped when turning an airport or facility name into what a
# controller actually says on the radio.
_NAME_NOISE = re.compile(
    r"\b(international|intl|intercontinental|regional|rgnl|municipal|muni|"
    r"airport|airfield|aerodrome|airpark|air\s*park|field|air\s*force\s*base|"
    r"afb|air\s*base|army|naval|air\s*station|nas|county|co|memorial|"
    r"executive|metropolitan|metro|national|natl|general|aviation|"
    r"and\s+international)\b",
    re.IGNORECASE,
)

# Words stripped from a frequency description whatever position it belongs to:
# sector qualifiers, "initial contact", and the generic airport words.
_UNIVERSAL_NOISE = re.compile(
    r"\b("
    r"initial|contact|call|secondary|primary|main|alternate|alt|standby|"
    r"backup|emergency|emerg|frequency|freq|final|intermediate|"
    r"north|south|east|west|northeast|northwest|southeast|southwest|"
    r"nw|ne|sw|se|sectors?|low|high|upper|lower|above|below|and|"
    r"rwy|runway|ops|operations|military|mil|civil|"
    r"afb|nas|mcas|aaf|raf|naf|ang|arb|"
    r"airport|arpt|apt|aerodrome|international|intl|regional|rgnl|"
    r"municipal|muni|field"
    r")\b",
    re.IGNORECASE,
)

# Position names that are never part of a real facility name, so they can be
# removed wherever they appear. A description routinely names a position other
# than the one the row is filed under -- "APP/DEP" on an approach frequency,
# "BRISBANE CNTR" on a flight-information one -- so this cannot be scoped to
# the row's own position.
_POSITION_NOISE = re.compile(
    r"\b("
    r"twr|tower|turm|toren|gnd|ground|rollkontrolle|rollfeld|vorfeld|"
    r"clnc|clearance|delivery|cd|pdc|pre[\s-]?taxi|pre[\s-]?flight|pflt|"
    r"appr|approach|app|departures?|dep|depc|arrivals?|"
    r"cent(?:er|re)|cntr|ctr|acc|"
    r"d-?atis|atis|unicom|unic|ctaf|multicom|atf|"
    r"awos|asos|awib|awis|automated|observation|"
    r"rdo|rco|fss|afis|fis|"
    r"gca|aprc|tma|tca|rdr|director|precision|talkdown|terminal|trml"
    r")\b",
    re.IGNORECASE,
)

# Words that are position names in some contexts and ordinary parts of a place
# name in others: "DEL" is Clearance Delivery, but "Aeropuerto del Sol" and
# "Del Rio" are real names, and "Vina del Mar" is a real city. These are
# removed only when they trail the description, which is where an abbreviated
# position sits and where a Spanish or French article never does.
_TRAILING_NOISE = re.compile(
    r"[\s/-]*\b("
    r"del|dir|arr|par|info|information|radio|radar|control|traffic|"
    r"area|taxi|ramp|apron|tour|torre|service|advisory|weather|"
    r"flight|oceanic|zone"
    r")\b[\s/-]*$",
    re.IGNORECASE,
)

# Sector qualifiers begin at the first digit or degree sign: "ORLANDO APP/DEP
# 061-180 4500' AND BELOW" is Orlando Approach, and everything from "061" on is
# a sector definition rather than a name.
_SECTOR_TAIL = re.compile(r"[\d°].*$", re.DOTALL)


# Airport and facility names arrive with the local spelling -- "Zurich",
# "Malmo", "Keflavik". Accents are folded to ASCII rather than deleted, because
# deleting them turns "Zurich" into "Z rich" and then into nonsense.
_TRANSLIT = {
    "ß": "ss", "æ": "ae", "œ": "oe", "ø": "o",
    "đ": "d", "ħ": "h", "ł": "l", "ŋ": "n",
    "þ": "th", "ð": "d", "ı": "i",
}


def to_ascii(text: str) -> str:
    """Fold accented Latin text down to plain ASCII letters."""
    if not text:
        return ""
    lowered = "".join(_TRANSLIT.get(ch, _TRANSLIT.get(ch.lower(), ch)) for ch in text)
    decomposed = unicodedata.normalize("NFKD", lowered)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _clean_facility(description: str, fallback: str, position: str = "") -> str:
    """Pull the spoken facility name out of a frequency description.

    ``"KENNEDY TWR"`` -> ``"Kennedy"``; ``"NORCAL APP SECONDARY"`` -> ``"NorCal"``;
    ``"NEW YORK APPROACH (CAMRN)"`` -> ``"New York"``; and the dual-language
    ``"FRANKFURT TOWER / TURM (NORTH)"`` -> ``"Frankfurt"``. When nothing
    survives, the cleaned airport name is used instead.

    ``position`` is accepted for context but the stripping is global, because
    descriptions frequently name a different position than the row they are
    filed under.
    """
    text = to_ascii(description or "")
    text = re.sub(r"\([^)]*\)", " ", text)   # drop "(CAMRN)" etc
    text = _SECTOR_TAIL.sub(" ", text)       # drop sector limits

    # A slash separates either two positions ("APP/DEP") or the same name in
    # two languages ("Tower / Turm"). Clean each side and keep the longest
    # name that survives.
    best = ""
    for segment in re.split(r"[/|]", text):
        candidate = segment
        # Trailing abbreviations can stack: "BANGOR RDO CLNC DEL".
        for _ in range(3):
            trimmed = _TRAILING_NOISE.sub("", candidate).strip()
            if trimmed == candidate:
                break
            candidate = trimmed
        candidate = _POSITION_NOISE.sub(" ", candidate)
        candidate = _UNIVERSAL_NOISE.sub(" ", candidate)
        # Removing a position from the middle can expose a new trailing one.
        for _ in range(2):
            trimmed = _TRAILING_NOISE.sub("", candidate).strip()
            if trimmed == candidate:
                break
            candidate = trimmed
        candidate = re.sub(r"[^A-Za-z'\- ]+", " ", candidate)
        candidate = re.sub(r"\s+", " ", candidate).strip(" -'")
        if len(candidate) > len(best):
            best = candidate
    if len(best) < 3:
        return fallback
    if best.isupper() or best.islower():
        best = best.title()
    return _restyle(best)


_STYLINGS = {
    "Norcal": "NorCal", "Socal": "SoCal", "Potomac": "Potomac",
    "Jfk": "Kennedy", "Lax": "Los Angeles", "Ord": "O'Hare",
    "Mcguire": "McGuire", "Macdill": "MacDill", "Mcconnell": "McConnell",
    "Mcchord": "McChord", "Obrien": "O'Brien", "Oharemcgee": "O'Hare",
}


def _restyle(text: str) -> str:
    for wrong, right in _STYLINGS.items():
        text = re.sub(rf"\b{wrong}\b", right, text, flags=re.IGNORECASE)
    return text


def spoken_airport_name(name: str, municipality: str = "") -> str:
    """Turn a database airport name into what a controller says on frequency.

    ``"John F. Kennedy International Airport"`` -> ``"Kennedy"``,
    ``"Hartsfield Jackson Atlanta International Airport"`` -> ``"Atlanta"``.
    """
    text = _NAME_NOISE.sub(" ", to_ascii(name or ""))
    # Hyphens join two place names ("Aspen-Pitkin", "Dallas-Fort Worth") and
    # have to become word breaks, or the whole compound reads as one token and
    # survives the trim that should have reduced it to the city.
    text = text.replace(".", " ").replace("-", " ").replace("/", " ")
    text = re.sub(r"[^A-Za-z' ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    parts = text.split()
    # A dropped single-letter initial is a strong signal the field is named
    # after a person, and a person-named field goes by the surname alone.
    had_initial = any(len(p) == 1 and p.isalpha() for p in parts)
    parts = [p for p in parts if not (len(p) == 1 and p.isalpha())]

    if had_initial and len(parts) >= 2:
        return _restyle(parts[-1])
    if len(parts) > 2:
        # Prefer the city when the name already contains it.
        if municipality and municipality.lower() in text.lower():
            return _restyle(municipality)
        parts = parts[-2:]

    result = " ".join(parts).strip()
    if not result:
        result = to_ascii(municipality) or to_ascii(name) or "the field"
    return _restyle(result)


SCHEMA = """
PRAGMA journal_mode = OFF;
PRAGMA synchronous = OFF;

DROP TABLE IF EXISTS airports;
CREATE TABLE airports (
    ident        TEXT PRIMARY KEY,
    icao         TEXT,
    iata         TEXT,
    name         TEXT NOT NULL,
    spoken       TEXT NOT NULL,
    type         TEXT,
    lat          REAL NOT NULL,
    lon          REAL NOT NULL,
    elev_ft      REAL,
    country      TEXT,
    region       TEXT,
    municipality TEXT,
    scheduled    INTEGER DEFAULT 0,
    size_rank    INTEGER DEFAULT 0
);
CREATE INDEX idx_airports_pos     ON airports(lat, lon);
CREATE INDEX idx_airports_icao    ON airports(icao);
CREATE INDEX idx_airports_iata    ON airports(iata);
CREATE INDEX idx_airports_country ON airports(country);

DROP TABLE IF EXISTS frequencies;
CREATE TABLE frequencies (
    ident       TEXT NOT NULL,
    position    TEXT NOT NULL,
    mhz         REAL NOT NULL,
    facility    TEXT NOT NULL,
    description TEXT,
    priority    INTEGER DEFAULT 0
);
CREATE INDEX idx_freq_ident ON frequencies(ident, position);
CREATE INDEX idx_freq_mhz   ON frequencies(mhz);

DROP TABLE IF EXISTS runways;
CREATE TABLE runways (
    ident      TEXT NOT NULL,
    le_ident   TEXT,
    he_ident   TEXT,
    length_ft  REAL,
    width_ft   REAL,
    surface    TEXT,
    lighted    INTEGER,
    closed     INTEGER,
    le_lat     REAL, le_lon REAL, le_elev REAL, le_heading REAL, le_displaced REAL,
    he_lat     REAL, he_lon REAL, he_elev REAL, he_heading REAL, he_displaced REAL
);
CREATE INDEX idx_runways_ident ON runways(ident);

DROP TABLE IF EXISTS navaids;
CREATE TABLE navaids (
    ident     TEXT NOT NULL,
    name      TEXT,
    type      TEXT,
    freq_khz  REAL,
    lat       REAL, lon REAL, elev_ft REAL,
    country   TEXT,
    airport   TEXT
);
CREATE INDEX idx_navaids_ident ON navaids(ident);
CREATE INDEX idx_navaids_pos   ON navaids(lat, lon);

DROP TABLE IF EXISTS boundaries;
CREATE TABLE boundaries (
    id       TEXT PRIMARY KEY,
    oceanic  INTEGER,
    min_lat  REAL, max_lat REAL, min_lon REAL, max_lon REAL,
    polygon  TEXT NOT NULL
);
CREATE INDEX idx_bounds_box ON boundaries(min_lat, max_lat, min_lon, max_lon);

DROP TABLE IF EXISTS firs;
CREATE TABLE firs (
    icao      TEXT NOT NULL,
    name      TEXT NOT NULL,
    spoken    TEXT NOT NULL,
    callsign  TEXT,
    boundary  TEXT
);
CREATE INDEX idx_firs_icao ON firs(icao);

-- Enroute sectors with their levels, from the VatGlasses data
-- (CC BY-NC-SA 4.0, VatGlasses contributors). Levels are hundreds of feet;
-- owners is the JSON list of sector_positions keys, in order.
DROP TABLE IF EXISTS sectors;
CREATE TABLE sectors (
    id       TEXT NOT NULL,
    prefix   TEXT,
    grp      TEXT,
    min_fl   INTEGER, max_fl INTEGER,
    min_lat  REAL, max_lat REAL, min_lon REAL, max_lon REAL,
    polygon  TEXT NOT NULL,
    owners   TEXT NOT NULL
);
CREATE INDEX idx_sectors_box ON sectors(min_lat, max_lat, min_lon, max_lon);

DROP TABLE IF EXISTS sector_positions;
CREATE TABLE sector_positions (
    key      TEXT PRIMARY KEY,
    callsign TEXT NOT NULL,
    mhz      REAL NOT NULL,
    type     TEXT,
    prefix   TEXT
);
CREATE INDEX idx_sector_positions_mhz ON sector_positions(mhz);

DROP TABLE IF EXISTS meta;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Airport importance, used to pick which field a pilot most likely means and
# which facility owns the airspace around a position.
_SIZE_RANK = {"large_airport": 3, "medium_airport": 2, "small_airport": 1}


def _f(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rows(path: Path):
    with path.open(encoding="utf-8", newline="") as fh:
        yield from csv.DictReader(fh)


def build(data_dir: Path = DATA_DIR, db_path: Path = DB_PATH) -> Path:
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    # ---------------- airports ----------------
    airport_names: dict[str, tuple[str, str]] = {}
    batch = []
    for r in _rows(data_dir / "airports.csv"):
        if r["type"] in ("closed",):
            continue
        lat, lon = _f(r["latitude_deg"]), _f(r["longitude_deg"])
        if lat is None or lon is None:
            continue
        ident = r["ident"].strip().upper()
        name = r["name"].strip()
        muni = r["municipality"].strip()
        spoken = spoken_airport_name(name, muni)
        airport_names[ident] = (name, spoken)
        icao = (r.get("icao_code") or "").strip().upper() or (
            ident if len(ident) == 4 and ident.isalpha() else ""
        )
        batch.append(
            (
                ident, icao, (r.get("iata_code") or "").strip().upper(), name, spoken,
                r["type"], lat, lon, _f(r["elevation_ft"]),
                r["iso_country"].strip().upper(), r["iso_region"].strip().upper(),
                muni, 1 if r["scheduled_service"] == "yes" else 0,
                _SIZE_RANK.get(r["type"], 0),
            )
        )
    conn.executemany(
        "INSERT OR REPLACE INTO airports VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch
    )
    print(f"  airports      {len(batch):>7}")

    # ---------------- frequencies ----------------
    batch, dropped_band, dropped_type, snapped = [], 0, 0, 0
    for r in _rows(data_dir / "airport-frequencies.csv"):
        mhz = _f(r["frequency_mhz"])
        if mhz is None:
            continue
        raw_type = r["type"].strip().upper()
        position = TYPE_MAP.get(raw_type)
        if position is None:
            dropped_type += 1
            continue
        if not (VHF_MIN <= mhz <= VHF_MAX):
            # VOR-collocated ATIS and military UHF cannot be tuned in COM1.
            dropped_band += 1
            continue
        ident = r["airport_ident"].strip().upper()
        _, spoken = airport_names.get(ident, ("", ident))
        desc = (r["description"] or "").strip()
        facility = _clean_facility(desc, spoken, position)
        # An "initial contact"/primary frequency should be handed out before an
        # alternate or secondary one.
        low = desc.lower()
        priority = 0
        if re.search(r"\b(alt|alternate|secondary|backup|standby|2)\b", low):
            priority = 2
        elif re.search(r"\b(initial|primary|main)\b", low):
            priority = -1
        # A figure written to two decimals loses its last digit, so 123.075
        # arrives as 123.07 -- not a channel any radio can select, and so a row
        # that could never be matched. Put it back on the grid.
        tuned = channels.snap(mhz)
        if channels.to_khz(tuned) != channels.to_khz(mhz):
            snapped += 1
        batch.append((ident, position, round(tuned, 3), facility, desc, priority))
    conn.executemany("INSERT INTO frequencies VALUES (?,?,?,?,?,?)", batch)
    print(f"  frequencies   {len(batch):>7}   "
          f"(dropped {dropped_band} out-of-band, {dropped_type} non-ATC"
          + (f", {snapped} snapped to a selectable channel)" if snapped else ")"))

    _apply_overrides(conn, data_dir, airport_names)
    _backfill_spoken_names(conn)

    # ---------------- runways ----------------
    batch = []
    for r in _rows(data_dir / "runways.csv"):
        batch.append(
            (
                r["airport_ident"].strip().upper(),
                r["le_ident"].strip().upper(), r["he_ident"].strip().upper(),
                _f(r["length_ft"]), _f(r["width_ft"]), r["surface"].strip(),
                1 if r["lighted"] == "1" else 0, 1 if r["closed"] == "1" else 0,
                _f(r["le_latitude_deg"]), _f(r["le_longitude_deg"]),
                _f(r["le_elevation_ft"]), _f(r["le_heading_degT"]),
                _f(r["le_displaced_threshold_ft"]),
                _f(r["he_latitude_deg"]), _f(r["he_longitude_deg"]),
                _f(r["he_elevation_ft"]), _f(r["he_heading_degT"]),
                _f(r["he_displaced_threshold_ft"]),
            )
        )
    conn.executemany(
        "INSERT INTO runways VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch
    )
    print(f"  runways       {len(batch):>7}")

    # ---------------- navaids ----------------
    batch = []
    for r in _rows(data_dir / "navaids.csv"):
        lat, lon = _f(r["latitude_deg"]), _f(r["longitude_deg"])
        if lat is None or lon is None:
            continue
        batch.append(
            (
                r["ident"].strip().upper(), r["name"].strip(), r["type"].strip(),
                _f(r["frequency_khz"]), lat, lon, _f(r["elevation_ft"]),
                r["iso_country"].strip().upper(),
                (r.get("associated_airport") or "").strip().upper(),
            )
        )
    conn.executemany("INSERT INTO navaids VALUES (?,?,?,?,?,?,?,?,?)", batch)
    print(f"  navaids       {len(batch):>7}")

    # ---------------- FIR / ARTCC boundaries ----------------
    _load_boundaries(conn, data_dir)
    _load_sectors(conn, data_dir)

    conn.execute("INSERT INTO meta VALUES ('built_from', 'ourairports+vatspy')")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    size = db_path.stat().st_size / 1e6
    print(f"  -> {db_path}  ({size:.1f} MB)")
    return db_path


def _apply_overrides(
    conn: sqlite3.Connection, data_dir: Path, airport_names: dict[str, tuple[str, str]]
) -> None:
    """Apply local frequency corrections from ``overrides.csv``.

    The bundled file is empty. It exists because the open dataset has real
    gaps -- most often an airport whose only listed ATIS is the VOR-collocated
    one, which the airband filter correctly drops -- and a user with a current
    chart should be able to fix their home field without editing code.
    """
    path = data_dir / "overrides.csv"
    if not path.exists():
        return

    applied = 0
    with path.open(encoding="utf-8", newline="") as fh:
        rows = [ln for ln in fh if not ln.lstrip().startswith("#")]
    for r in csv.DictReader(rows):
        ident = (r.get("ident") or "").strip().upper()
        position = (r.get("position") or "").strip().upper()
        if not ident or position not in set(TYPE_MAP.values()):
            continue
        action = (r.get("action") or "add").strip().lower()

        if action in ("delete", "replace"):
            conn.execute(
                "DELETE FROM frequencies WHERE ident = ? AND position = ?",
                (ident, position),
            )
            applied += 1
            if action == "delete":
                continue

        mhz = _f(r.get("mhz", ""))
        if mhz is None:
            continue
        if not (VHF_MIN <= mhz <= VHF_MAX):
            print(f"  override {ident} {position} {mhz} rejected: outside airband")
            continue
        mhz = round(mhz, 3)
        conn.execute(
            "DELETE FROM frequencies WHERE ident = ? AND position = ? AND mhz = ?",
            (ident, position, mhz),
        )
        desc = (r.get("description") or "").strip()
        facility = (r.get("facility") or "").strip()
        if not facility:
            _, spoken = airport_names.get(ident, ("", ident))
            facility = _clean_facility(desc, spoken, position)
        conn.execute(
            "INSERT INTO frequencies VALUES (?,?,?,?,?,?)",
            (ident, position, mhz, facility, desc or "local override", -2),
        )
        applied += 1

    if applied:
        print(f"  overrides     {applied:>7}   (from overrides.csv)")


def _backfill_spoken_names(conn: sqlite3.Connection) -> None:
    """Let the published tower/ATIS name override the derived airport name.

    The frequency descriptions are transcribed from charts, so when one says
    "KENNEDY TWR" that is literally what is spoken on the radio -- a better
    source than any amount of cleverness applied to the airport's legal name.
    Only same-airport tower and ATIS entries are trusted, because approach and
    centre names belong to a wider facility ("NorCal", "New York") that is not
    the airport's own name.
    """
    rows = conn.execute(
        """
        SELECT f.ident, f.facility, COUNT(*) AS n
        FROM frequencies f
        WHERE f.position IN ('TWR', 'ATIS')
        GROUP BY f.ident, f.facility
        ORDER BY f.ident, n DESC, LENGTH(f.facility) ASC
        """
    ).fetchall()

    chosen: dict[str, str] = {}
    for ident, facility, _ in rows:
        if ident in chosen or not facility:
            continue
        chosen[ident] = facility

    updates = []
    for ident, facility in chosen.items():
        row = conn.execute(
            "SELECT spoken, name FROM airports WHERE ident = ?", (ident,)
        ).fetchone()
        if not row:
            continue
        current, legal = row
        if facility.lower() == current.lower():
            continue
        # Only accept the published name when it is a genuine alternative, not
        # a truncation artefact: it must appear in the legal name, or be a
        # strictly shorter form of what we derived.
        legal_l, facility_l = legal.lower(), facility.lower()
        if facility_l in legal_l or facility_l in current.lower():
            updates.append((facility, ident))

    conn.executemany("UPDATE airports SET spoken = ? WHERE ident = ?", updates)
    print(f"  spoken names  {len(updates):>7}   (from published tower/ATIS names)")


def _load_boundaries(conn: sqlite3.Connection, data_dir: Path) -> None:
    """Index ARTCC/FIR polygons so an enroute position resolves to a facility."""
    geo = data_dir / "Boundaries.geojson"
    dat = data_dir / "VATSpy.dat"
    if not geo.exists():
        print("  boundaries    skipped (no Boundaries.geojson)")
        return

    with geo.open(encoding="utf-8") as fh:
        collection = json.load(fh)

    batch = []
    for feature in collection.get("features", []):
        props = feature.get("properties", {})
        fid = str(props.get("id") or props.get("ID") or "").upper()
        if not fid:
            continue
        rings = _all_rings(feature.get("geometry") or {})
        if not rings:
            continue
        lats = [p[1] for ring in rings for p in ring]
        lons = [p[0] for ring in rings for p in ring]
        batch.append(
            (
                fid, int(props.get("oceanic", 0) or 0),
                min(lats), max(lats), min(lons), max(lons),
                json.dumps(rings, separators=(",", ":")),
            )
        )
    conn.executemany("INSERT OR REPLACE INTO boundaries VALUES (?,?,?,?,?,?,?)", batch)
    print(f"  boundaries    {len(batch):>7}")

    if not dat.exists():
        return
    firs = []
    section = ""
    for line in dat.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("["):
            section = line.strip("[]").upper()
            continue
        parts = line.split("|")
        if section == "FIRS" and len(parts) >= 4:
            icao, name, callsign, boundary = parts[0], parts[1], parts[2], parts[3]
            firs.append((icao.upper(), name, _spoken_fir(name), callsign, boundary.upper()))
        elif section == "UIRS" and len(parts) >= 2:
            firs.append((parts[0].upper(), parts[1], _spoken_fir(parts[1]), "", ""))
    conn.executemany("INSERT INTO firs VALUES (?,?,?,?,?)", firs)
    print(f"  firs          {len(firs):>7}")


# Positions that exist only on the VATSIM network: "EuroWest", "EuroSouth"
# and the like, top-down cover for half a continent at once, filed in fss.json
# and listed first in the owners of real sectors such as Maastricht's. No real
# sky has them, so a sector is owned by the first real position after them.
# The file decides, not the type: Australia files its real oceanic stations,
# Brisbane Radio among them, as FSS too.
_NETWORK_ONLY_FILES = ("fss",)


def parse_dms(text: str) -> float:
    """A VatGlasses coordinate: DDMMSS or DDDMMSS, "-" for west and south."""
    text = str(text).strip()
    sign = -1.0 if text.startswith("-") else 1.0
    text = text.lstrip("+-")
    whole, _, fraction = text.partition(".")
    seconds = float(whole[-2:] + ("." + fraction if fraction else ""))
    minutes = int(whole[-4:-2])
    degrees = int(whole[:-4] or 0)
    return sign * (degrees + minutes / 60 + seconds / 3600)


def _load_sectors(conn: sqlite3.Connection, data_dir: Path) -> None:
    """Index the VatGlasses sectors: polygon, floor, ceiling, owners.

    Each file holds a country (named by its ICAO prefixes, ``eb-el.json``)
    and may name owners in another file as ``file/KEY``. Every position is
    stored under ``file/KEY`` so the two meet. A position off the civil VHF
    band -- a supervisor on 199.998 -- is nobody a pilot can call.
    """
    folder = data_dir / "vatglasses"
    files = sorted(folder.glob("*.json")) if folder.is_dir() else []
    if not files:
        print("  sectors       skipped (no VatGlasses data)")
        return
    positions = []
    sectors = []
    for path in files:
        name = path.stem.lower()
        prefix = name.split("-")[0].upper()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"  sectors       {path.name}: {exc}")
            continue
        for key, position in (data.get("positions") or {}).items():
            try:
                mhz = float(position.get("frequency") or 0)
            except (TypeError, ValueError):
                continue
            kind = str(position.get("type") or "").upper()
            if not VHF_MIN <= mhz <= VHF_MAX or name in _NETWORK_ONLY_FILES:
                continue
            positions.append((f"{name}/{key}", str(position.get("callsign") or ""),
                              mhz, kind, prefix))
        groups = data.get("groups") or {}
        for airspace in data.get("airspace") or []:
            group = groups.get(str(airspace.get("group") or "")) or {}
            group_name = str(group.get("name") or "") if isinstance(group, dict) else ""
            owners = []
            for owner in airspace.get("owner") or []:
                owner = str(owner)
                qualified = owner if "/" in owner else f"{name}/{owner}"
                if qualified.split("/")[0] in _NETWORK_ONLY_FILES:
                    continue
                owners.append(qualified)
            if not owners:
                continue
            for sector in airspace.get("sectors") or []:
                try:
                    ring = [[parse_dms(lon), parse_dms(lat)]
                            for lat, lon in sector.get("points") or []]
                except (TypeError, ValueError):
                    continue
                if len(ring) < 3:
                    continue
                lats = [p[1] for p in ring]
                lons = [p[0] for p in ring]
                sectors.append((
                    str(airspace.get("id") or ""), prefix, group_name,
                    int(sector.get("min") or 0), int(sector.get("max") or 999),
                    min(lats), max(lats), min(lons), max(lons),
                    json.dumps([ring], separators=(",", ":")),
                    json.dumps(owners, separators=(",", ":")),
                ))
    conn.executemany("INSERT OR REPLACE INTO sector_positions VALUES (?,?,?,?,?)",
                     positions)
    conn.executemany("INSERT INTO sectors VALUES (?,?,?,?,?,?,?,?,?,?,?)", sectors)
    print(f"  sectors       {len(sectors):>7}  ({len(positions)} positions)")


def _spoken_fir(name: str) -> str:
    """Reduce a FIR/ARTCC name to what precedes "Center" on the radio.

    ``"Boston Oceanic FIR"`` -> ``"Boston"``, and the heavily qualified
    ``"London TMA (Up to FL195) - London Control"`` -> ``"London"``.
    """
    text = to_ascii(name or "")
    text = re.sub(r"\([^)]*\)", " ", text)          # "(Up to FL195)"
    text = re.split(r"\s+-\s+|,", text)[0]          # keep the leading segment
    text = re.sub(
        r"\b(fir|uir|ctr|acc|artcc|tma|cta|mtma|ta|"
        r"center|centre|control|oceanic|domestic|upper|lower|"
        r"flight\s+information\s+region|information|region|radar|"
        r"north|south|east|west|sector)\b",
        " ", text, flags=re.IGNORECASE,
    )
    text = re.sub(r"[^A-Za-z'\- ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -")
    if not text:
        text = re.sub(r"[^A-Za-z ]+", " ", to_ascii(name or "")).strip()
    return _restyle(text or name)


def _all_rings(geometry: dict) -> list[list[list[float]]]:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon":
        return [ring for ring in coords if len(ring) >= 3]
    if gtype == "MultiPolygon":
        return [ring for poly in coords for ring in poly if len(ring) >= 3]
    return []


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    r = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial true bearing from point 1 to point 2, in degrees."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def project(lat: float, lon: float, bearing: float, distance_nm: float
            ) -> tuple[float, float]:
    """The point that far from here, on that bearing.

    The inverse of :func:`haversine_nm` and :func:`bearing_deg`, which is what
    puts an aeroplane four miles out on the extended centreline rather than
    somewhere near the airport.
    """
    r = 3440.065
    angular = float(distance_nm) / r
    p1 = math.radians(lat)
    theta = math.radians(bearing)
    p2 = math.asin(math.sin(p1) * math.cos(angular)
                   + math.cos(p1) * math.sin(angular) * math.cos(theta))
    l2 = math.radians(lon) + math.atan2(
        math.sin(theta) * math.sin(angular) * math.cos(p1),
        math.cos(angular) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


if __name__ == "__main__":
    from wilcoatc.navdata.fetch import fetch_all

    if "--fetch" in sys.argv:
        fetch_all(force="--force" in sys.argv)
    print("Building nav database...")
    build()
