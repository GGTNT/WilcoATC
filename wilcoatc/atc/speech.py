"""Aviation speech formatting.

Everything a controller says is built from these primitives, so the rules here
decide whether the sim sounds like ATC or like a text-to-speech engine reading a
spreadsheet. References:

  * FAA JO 7110.65   -- Air Traffic Control, ch. 2 sec. 4 (radio phraseology)
  * FAA AIM 4-2      -- Radio communications phraseology and technique
  * ICAO Annex 10 II -- Aeronautical telecommunications, ch. 5
  * ICAO Doc 4444    -- PANS-ATM, ch. 12

Two dialects are supported. ``faa`` is the US domestic convention; ``icao`` is
the international one. They differ in digit pronunciation ("three" vs "tree"),
altitude wording (flight levels vs metric-free thousands), pressure setting
(altimeter/inHg vs QNH/hPa) and how air-carrier flight numbers are grouped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal

Dialect = Literal["faa", "icao"]

# --------------------------------------------------------------------------
# digits
# --------------------------------------------------------------------------

# ICAO Annex 10 spells 3/4/5/9 differently so they survive a noisy VHF channel.
# US controllers keep "three" and "five" but universally say "niner".
_DIGITS_FAA = {
    "0": "zero",
    "1": "one",
    "2": "two",
    "3": "three",
    "4": "four",
    "5": "five",
    "6": "six",
    "7": "seven",
    "8": "eight",
    "9": "niner",
}

_DIGITS_ICAO = dict(_DIGITS_FAA, **{"3": "tree", "4": "fower", "5": "fife"})

_ALPHABET = {
    "A": "Alpha",
    "B": "Bravo",
    "C": "Charlie",
    "D": "Delta",
    "E": "Echo",
    "F": "Foxtrot",
    "G": "Golf",
    "H": "Hotel",
    "I": "India",
    "J": "Juliet",
    "K": "Kilo",
    "L": "Lima",
    "M": "Mike",
    "N": "November",
    "O": "Oscar",
    "P": "Papa",
    "Q": "Quebec",
    "R": "Romeo",
    "S": "Sierra",
    "T": "Tango",
    "U": "Uniform",
    "V": "Victor",
    "W": "Whiskey",
    "X": "Xray",
    "Y": "Yankee",
    "Z": "Zulu",
}

# Reverse map, used when parsing what the pilot said back to us.
_ALPHABET_REVERSE = {v.lower(): k for k, v in _ALPHABET.items()}
_ALPHABET_REVERSE.update(
    {
        "alfa": "A",
        "juliett": "J",
        "juliette": "J",
        "x-ray": "X",
        "xray": "X",
    }
)

_WORD_TO_DIGIT = {
    "zero": "0",
    "oh": "0",
    "o": "0",
    "one": "1",
    "won": "1",
    "two": "2",
    "to": "2",
    "too": "2",
    "three": "3",
    "tree": "3",
    "four": "4",
    "fower": "4",
    "for": "4",
    "five": "5",
    "fife": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "ate": "8",
    "nine": "9",
    "niner": "9",
}


def digits(value: str | int, dialect: Dialect = "faa") -> str:
    """Speak each character individually: ``129`` -> ``one two niner``."""
    table = _DIGITS_ICAO if dialect == "icao" else _DIGITS_FAA
    out: list[str] = []
    for ch in str(value):
        if ch in table:
            out.append(table[ch])
        elif ch.upper() in _ALPHABET:
            out.append(_ALPHABET[ch.upper()])
        elif ch == ".":
            out.append("point")
        elif ch == "-":
            continue
        elif ch == " ":
            continue
        else:
            out.append(ch)
    return " ".join(out)


def letters(value: str) -> str:
    """Spell a string with the phonetic alphabet, leaving digits as digits."""
    out: list[str] = []
    for ch in value:
        if ch.upper() in _ALPHABET:
            out.append(_ALPHABET[ch.upper()])
        elif ch.isdigit():
            out.append(_DIGITS_FAA[ch])
    return " ".join(out)


# --------------------------------------------------------------------------
# group form (air-carrier flight numbers)
# --------------------------------------------------------------------------

_ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = [
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
]


def _two_digit_group(n: int) -> str:
    """0-99 as a spoken pair. 5 -> 'oh five', 40 -> 'forty', 34 -> 'thirty four'."""
    if n < 10:
        return f"oh {_ONES[n]}"
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] if ones == 0 else f"{_TENS[tens]} {_ONES[ones]}"


def group_form(number: str | int) -> str:
    """Speak an air-carrier flight number the way US controllers do.

    FAA JO 7110.65 2-4-20 has air-carrier flight numbers in *group form*, which
    is why ``AAL1234`` is "American twelve thirty four" and not "American one
    two three four". The grouping follows everyday spoken English:

        5     -> five
        45    -> forty five
        305   -> three oh five
        320   -> three twenty
        1234  -> twelve thirty four
        1005  -> ten oh five
        1100  -> eleven hundred
        2000  -> two thousand
    """
    text = str(number).lstrip("0") or "0"
    if not text.isdigit():
        return digits(number)
    n = int(text)

    if n < 100:
        # Leading "oh" is only used inside a group, never on its own.
        return _ONES[n] if n < 20 else _two_digit_group(n)

    if n < 1000:
        hundreds, rest = divmod(n, 100)
        if rest == 0:
            return f"{_ONES[hundreds]} hundred"
        return f"{_ONES[hundreds]} {_two_digit_group(rest)}"

    if n < 10000:
        if n % 1000 == 0:
            return f"{_ONES[n // 1000]} thousand"
        if n % 100 == 0:
            hundreds = n // 100
            if hundreds < 20:
                return f"{_ONES[hundreds]} hundred"
            return f"{_two_digit_group(hundreds)} hundred"
        high, low = divmod(n, 100)
        return f"{_two_digit_group(high)} {_two_digit_group(low)}"

    return digits(number)


# --------------------------------------------------------------------------
# callsigns
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Callsign:
    """A callsign in both its written and spoken forms.

    ``spoken_full`` is what a controller uses on first contact; ``spoken_short``
    is the abbreviated form permitted once two-way communication is established
    (AIM 4-2-4), which for a GA tail number drops to the last three characters.
    """

    written: str
    spoken_full: str
    spoken_short: str
    telephony: str = ""
    is_airline: bool = False

    def spoken(self, abbreviated: bool = False) -> str:
        return self.spoken_short if abbreviated else self.spoken_full


# ICAO three-letter designator -> radiotelephony designator. This is the subset
# that actually shows up in flight-sim traffic; airlines/airlines.csv extends it
# at runtime, so anything missing there falls back to spelling the code.
TELEPHONY: dict[str, str] = {
    "AAL": "American",
    "ACA": "Air Canada",
    "AAR": "Asiana",
    "AFL": "Aeroflot",
    "AFR": "Airfrans",
    "AIC": "Air India",
    "ANA": "All Nippon",
    "ANZ": "New Zealand",
    "ASA": "Alaska",
    "AUA": "Austrian",
    "AVA": "Avianca",
    "AWI": "Wisconsin",
    "AZA": "Itarrow",
    "BAW": "Speedbird",
    "BCS": "Eurotrans",
    "BEL": "Beeline",
    "CAL": "Dynasty",
    "CCA": "Air China",
    "CES": "China Eastern",
    "CFG": "Condor",
    "CKS": "Connie",
    "CPA": "Cathay",
    "CSN": "China Southern",
    "DAL": "Delta",
    "DLH": "Lufthansa",
    "EDV": "Endeavor",
    "EIN": "Shamrock",
    "ELY": "El Al",
    "ETD": "Etihad",
    "ETH": "Ethiopian",
    "EVA": "Eva",
    "EZY": "Easy",
    "FDX": "FedEx",
    "FFT": "Frontier Flight",
    "FIN": "Finnair",
    "GLO": "Gol Transporte",
    "GTI": "Giant",
    "HAL": "Hawaiian",
    "IBE": "Iberia",
    "ICE": "Iceair",
    "JAL": "Japan Air",
    "JBU": "JetBlue",
    "JIA": "Blue Streak",
    "KAL": "Koreanair",
    "KLM": "KLM",
    "LAN": "Lan Chile",
    "LOT": "Lot",
    "MSR": "Egyptair",
    "NKS": "Spirit Wings",
    "NWA": "Northwest",
    "PAL": "Philippine",
    "QFA": "Qantas",
    "QTR": "Qatari",
    "RJA": "Jordanian",
    "ROU": "Rouge",
    "RYR": "Ryanair",
    "SAS": "Scandinavian",
    "SIA": "Singapore",
    "SKW": "SkyWest",
    "SVA": "Saudia",
    "SWA": "Southwest",
    "SWR": "Swiss",
    "TAM": "Tam",
    "TAP": "Air Portugal",
    "THA": "Thai",
    "THY": "Turkish",
    "TSC": "Air Transat",
    "TVF": "Transavia France",
    "UAE": "Emirates",
    "UAL": "United",
    "UPS": "UPS",
    "VIR": "Virgin",
    "VLG": "Vueling",
    "VOI": "Volaris",
    "WJA": "Westjet",
    "WZZ": "Wizz Air",
}

# Airline names as a simulator reports them, mapped to ICAO designators.
#
# A simulator gives a marketing name -- "American Airlines", "Delta Air
# Lines" -- and the radio needs "AAL". Most of them are the telephony name
# already and are found by reversing :data:`TELEPHONY`; this table is the ones
# that are not, either because the marketing name and the callsign are
# different words ("British Airways" is Speedbird) or because the sim writes
# the name a different way.
SIM_AIRLINE_NAMES: dict[str, str] = {
    "american": "AAL", "american airlines": "AAL",
    "delta air lines": "DAL", "united airlines": "UAL",
    "southwest": "SWA", "southwest airlines": "SWA",
    "jetblue": "JBU", "jetblue airways": "JBU",
    "alaska airlines": "ASA", "spirit": "NKS", "allegiant": "AAY",
    "hawaiian": "HAL", "skywest": "SKW",
    "british airways": "BAW", "air france": "AFR",
    "iberia": "IBE", "swiss": "SWR", "austrian": "AUA",
    "easyjet": "EZY", "vueling": "VLG", "wizz air": "WZZ",
    "virgin atlantic": "VIR", "aer lingus": "EIN", "tap": "TAP",
    "tap air portugal": "TAP", "sas": "SAS", "scandinavian": "SAS",
    "turkish": "THY", "turkish airlines": "THY",
    "qatar": "QTR", "qatar airways": "QTR",
    "singapore": "SIA", "singapore airlines": "SIA",
    "cathay": "CPA", "cathay pacific": "CPA",
    "japan airlines": "JAL", "all nippon": "ANA", "ana": "ANA",
    "korean air": "KAL", "asiana": "AAR", "china eastern": "CES",
    "china southern": "CSN", "air china": "CCA", "eva air": "EVA",
    "air canada": "ACA", "westjet": "WJA", "aeromexico": "AMX",
    "latam": "LAN", "gol": "GLO", "azul": "AZU", "avianca": "AVA",
    "fedex": "FDX", "ups": "UPS", "dhl": "BCS", "atlas air": "GTI",
    "brussels airlines": "BEL", "el al": "ELY", "lot": "LOT",
    "aegean": "AEE", "norwegian": "NAX", "transavia": "TRA",
    "air europa": "AEA", "condor": "CFG", "eurowings": "EWG",
    "royal air maroc": "RAM", "tunisair": "TAR", "air algerie": "DAH",
    "south african": "SAA", "ethiopian": "ETH", "kenya airways": "KQA",
}


def airline_icao(name: str) -> str:
    """The ICAO designator for an airline a simulator has named, or "".

    Three readings of the name, widest last. The explicit table above, then
    the telephony names this module already knows read backwards, then a bare
    three-letter word, which is a designator somebody typed in the airline
    field. Anything else is not an airline and the aeroplane keeps its
    registration.
    """
    key = (name or "").strip().lower()
    if not key:
        return ""
    if key in SIM_AIRLINE_NAMES:
        return SIM_AIRLINE_NAMES[key]
    reversed_telephony = _telephony_by_name()
    if key in reversed_telephony:
        return reversed_telephony[key]
    from .airlines import icao_for_name

    if icao_for_name(key):
        return icao_for_name(key)
    for candidate, icao in SIM_AIRLINE_NAMES.items():
        if candidate in key:
            return icao
    if len(key) == 3 and key.isalpha():
        return key.upper()
    return ""


_BY_NAME: dict[str, str] = {}


def _telephony_by_name() -> dict[str, str]:
    """:data:`TELEPHONY` the other way round, built once.

    Several designators share a telephony name -- Air France and its regional
    subsidiary are both "Air France" on the radio -- so the first one wins and
    the order of the table decides it. That is the right answer for this: what
    is being recovered is who is speaking, not which subsidiary owns the
    aeroplane.
    """
    if not _BY_NAME:
        for icao, name in TELEPHONY.items():
            _BY_NAME.setdefault(name.strip().lower(), icao)
    return _BY_NAME


def radio_callsign(registration: str, airline: str = "",
                   flight_number: str = "", aircraft_type: str = "") -> str:
    """What an aeroplane calls itself, from what a simulator reports.

    A tail number is not a callsign. An airliner in a simulator carries all
    three of these, and reading only the registration made every one of them
    spell its tail on the radio -- an El Al 787 into Heathrow announcing
    itself as "four Xray Echo Delta Charlie", which is a thing no airliner has
    ever said.

    The registration is kept whenever the other two do not make a callsign,
    which is the honest answer for a private aeroplane and for a simulator
    that fills the airline field with something that is not an airline.
    """
    written = "".join(ch for ch in (flight_number or "").upper() if ch.isalnum())
    number = "".join(ch for ch in written if ch.isdigit())
    if not number:
        return registration
    icao = airline_icao(airline)
    if not icao:
        return registration
    # A light aeroplane in an airline's paint is still a light aeroplane, and
    # calls itself by its registration.
    if not flies_as_airline(aircraft_type):
        return registration
    # 7A8 is a flight identification in its own right; keeping only its
    # digits would turn Speedbird seven Alpha eight into Speedbird seventy
    # eight, which is somebody else.
    if written != number and _AIRLINE_RE.match(f"{icao}{written.lstrip('0')}"):
        return f"{icao}{written.lstrip('0')}"
    return f"{icao}{int(number)}"


# Manufacturer names controllers actually use for light aircraft, keyed by the
# ICAO type designator reported by the sim.
GA_TYPE_TELEPHONY: dict[str, str] = {
    "C172": "Cessna",
    "C152": "Cessna",
    "C182": "Cessna",
    "C208": "Caravan",
    "C25A": "Citation",
    "C25C": "Citation",
    "C700": "Citation",
    "P28A": "Cherokee",
    "P28R": "Arrow",
    "PA18": "Cub",
    "PA24": "Comanche",
    "PA34": "Seneca",
    "PA44": "Seminole",
    "PA46": "Malibu",
    "SR20": "Cirrus",
    "SR22": "Cirrus",
    "BE36": "Bonanza",
    "BE58": "Baron",
    "BE9L": "King Air",
    "B350": "King Air",
    "DA40": "Diamond Star",
    "DA42": "Twin Star",
    "DA62": "Diamond",
    "DV20": "Katana",
    "TBM9": "Tee Bee Em",
    "EPIC": "Epic",
    "PC12": "Pilatus",
    "M20P": "Mooney",
    "AC11": "Commander",
    "RV10": "Are Vee",
    "VL3": "Vee Ell",
}

# The types that fly under an airline's telephony name: airliners, regional
# airliners and freighters, by ICAO designator family. Anything else is a
# light or business aeroplane and is called by its registration, whatever its
# callsign field says -- otherwise a Baron filed as BEL123 answers to
# "Beeline", which is a thing only an Airbus of Brussels Airlines does.
_AIRLINER_RE = re.compile(
    r"^(?:"
    r"A3\d[\dA-Z]|A[12][019]N|A124|A225|A400|BCS[13]"      # Airbus, Antonov
    r"|B7\d[\dA-Z]|B3[789X]M|B46[123]|RJ(?:70|85|1H)"      # Boeing, BAe 146
    r"|MD[89]\d|MD11|DC(?:8\d|9\d?|10)"                    # McDonnell Douglas
    r"|E1[3479]\d|E75[LS]|E29\d|E45X|CRJ[\dX]"             # Embraer, CRJ
    r"|AT[47]\d|DH8[A-D]|SF34|SB20|JS41|D328|J328"         # regional turboprops
    r"|F(?:27|28|50|70|100)|SU95|C919|AJ27|BA11"
    r"|IL(?:62|76|86|96)|T(?:134|154|204)|AN(?:12|26|72)|A[12]48|A158|YK42"
    r"|C130|C30J|C17|C5M?|K35R"                             # military transport
    r")$"
)


def is_airliner(aircraft_type: str) -> bool:
    """Whether a type is an airliner or a freighter, rather than a light or
    business aeroplane."""
    return bool(_AIRLINER_RE.match((aircraft_type or "").strip().upper()))


def flies_as_airline(aircraft_type: str) -> bool:
    """Whether an airline callsign is said as one on this type.

    No type is no evidence either way -- a flight typed in with no simulator,
    a model the injector does not name -- and the callsign was filed as an
    airline's, so it is taken at its word. A type that is known and is not an
    airliner is what stops a light aeroplane borrowing an airline's name.
    """
    return not (aircraft_type or "").strip() or is_airliner(aircraft_type)


# A designator and a flight identification. The identification is a number,
# perhaps with one trailing letter, or -- the way the UK and much of Europe
# file them now to keep similar callsigns apart -- a digit followed by up to
# three letters and digits: BAW7A8, EZY83BG. Leading with a digit is what
# keeps an all-letter registration (GABCD) out of it.
_AIRLINE_RE = re.compile(r"^([A-Z]{3})(\d{1,4}[A-Z]?|\d[A-Z0-9]{1,3})$")
_NUMERIC_FLIGHT_RE = re.compile(r"^\d{1,4}[A-Z]?$")


def airline_table(airline_names: dict[str, str] | None = None) -> dict[str, str]:
    """Designator -> telephony: the built-in table, the file over it, the
    pilot's own names over both."""
    from .airlines import telephony as file_telephony

    table = dict(TELEPHONY)
    table.update(file_telephony())
    if airline_names:
        table.update({k.upper(): v for k, v in airline_names.items()})
    return table
_TAIL_RE = re.compile(r"^([A-Z]{1,2})[- ]?([A-Z0-9]{1,5})$")


def parse_callsign(
    raw: str,
    aircraft_type: str = "",
    dialect: Dialect = "faa",
    airline_names: dict[str, str] | None = None,
    strict_icao_digits: bool = False,
) -> Callsign:
    """Turn a written callsign into its spoken forms.

    ``raw`` is whatever the sim reports in ATC ID -- ``DAL1234``, ``N172SP``,
    ``G-ABCD``. ``aircraft_type`` lets a bare tail number pick up the
    manufacturer prefix a controller would use ("Cessna Seven Two Sierra Papa").

    ``dialect`` decides how the number is *grouped* -- the FAA says "twelve
    thirty four" and everybody else says "one two three four". Whether those
    digits are then pronounced "three" or "tree" is a different question and
    is ``strict_icao_digits``, exactly as it is for the rest of the
    phraseology; an ICAO field does not automatically mean an ICAO alphabet.
    """
    figures: Dialect = ("icao" if (dialect == "icao" and strict_icao_digits)
                        else "faa")
    raw = (raw or "").strip().upper().replace("_", "")
    if not raw:
        return Callsign("UNKNOWN", "unknown aircraft", "unknown aircraft")

    table = airline_table(airline_names)

    # --- air carrier: three-letter designator plus flight number -----------
    m = _AIRLINE_RE.match(raw)
    if m and m.group(1) in table and flies_as_airline(aircraft_type):
        icao, flight = m.group(1), m.group(2)
        name = table[icao]
        if not _NUMERIC_FLIGHT_RE.match(flight):
            # An alphanumeric identification has no number to group, so it
            # is read a character at a time everywhere: "seven Alpha eight".
            spoken = f"{name} {digits_and_letters(flight, figures)}"
            return Callsign(raw, spoken, spoken, telephony=name, is_airline=True)
        suffix = ""
        if flight[-1].isalpha():  # e.g. DAL123A "heavy"-style trip suffix
            suffix = f" {_ALPHABET[flight[-1]]}"
            flight = flight[:-1]
        # ICAO practice is digit-by-digit; the FAA groups them.
        number = (group_form(flight) if dialect == "faa"
                  else digits(flight, figures))
        spoken = f"{name} {number}{suffix}"
        return Callsign(raw, spoken, spoken, telephony=name, is_airline=True)

    # --- US registration --------------------------------------------------
    if raw.startswith("N") and len(raw) >= 4 and raw[1].isdigit():
        body = raw[1:]
        prefix = GA_TYPE_TELEPHONY.get(aircraft_type.upper(), "")
        full_body = digits_and_letters(body, figures)
        full = f"{prefix} {full_body}".strip() if prefix else f"November {full_body}"
        # AIM 4-2-4: abbreviate to the last three characters once established.
        short_body = digits_and_letters(body[-3:], figures)
        short = f"{prefix} {short_body}".strip() if prefix else f"November {short_body}"
        return Callsign(raw, full, short)

    # --- other registrations (G-ABCD, D-EFGH, VH-ABC ...) -----------------
    m = _TAIL_RE.match(raw)
    if m:
        full = letters(raw.replace("-", ""))
        short = letters(raw.replace("-", "")[-3:])
        return Callsign(raw, full, short)

    spoken = digits_and_letters(raw, figures)
    return Callsign(raw, spoken, spoken)


def digits_and_letters(value: str, dialect: Dialect = "faa") -> str:
    """Speak a mixed alphanumeric string character by character."""
    table = _DIGITS_ICAO if dialect == "icao" else _DIGITS_FAA
    out: list[str] = []
    for ch in value.upper():
        if ch.isdigit():
            out.append(table[ch])
        elif ch in _ALPHABET:
            out.append(_ALPHABET[ch])
    return " ".join(out)


# --------------------------------------------------------------------------
# altitudes, levels, headings, speeds
# --------------------------------------------------------------------------


def is_flight_level(feet: float, transition: int, level_from: int = 0) -> bool:
    """Whether a vertical position is spoken as a flight level.

    ``transition`` is the transition altitude, and at it the position is
    still an altitude (ICAO Doc 8168 Vol I): "three thousand" at Schiphol,
    not "flight level zero three zero". The exception is the North American
    eighteen thousand, which is the floor of Class A and is itself flight
    level one eight zero (14 CFR 91.121, AIM 7-2-2) -- no other region in
    the table publishes a figure that high.

    ``level_from`` is a transition *level*, for a descent: in the layer
    between the two an aeroplane descending is given altitudes, so the
    boundary is the level rather than the altitude.
    """
    ft = int(round(float(feet) / 100.0) * 100)
    if level_from:
        return ft >= level_from
    if transition >= 18000:
        return ft >= transition
    return ft > transition


def altitude(feet: float, dialect: Dialect = "faa", transition: int = 18000,
             level_from: int = 0) -> str:
    """Speak an altitude or flight level.

    Below the transition altitude the FAA states thousands digit-by-digit and
    appends hundreds: 12,500 becomes "one two thousand five hundred". Above
    it (see :func:`is_flight_level`), the same number is a flight level.
    """
    ft = int(round(float(feet) / 100.0) * 100)
    if is_flight_level(ft, transition, level_from):
        return flight_level(ft // 100, dialect)
    if ft <= 0:
        return "sea level"

    thousands, remainder = divmod(ft, 1000)
    hundreds = remainder // 100
    parts: list[str] = []
    if thousands:
        parts.append(f"{digits(thousands, dialect)} thousand")
    if hundreds:
        parts.append(f"{_DIGITS_FAA[str(hundreds)] if dialect == 'faa' else _DIGITS_ICAO[str(hundreds)]} hundred")
    return " ".join(parts)


def flight_level(level: int, dialect: Dialect = "faa") -> str:
    """``350`` -> ``flight level three five zero``."""
    return f"flight level {digits(f'{int(level):03d}', dialect)}"


def heading(degrees: float, dialect: Dialect = "faa") -> str:
    """Headings are always three digits; 360 is spoken, never 000."""
    deg = int(round(float(degrees))) % 360
    if deg == 0:
        deg = 360
    return digits(f"{deg:03d}", dialect)


def speed(knots: float, dialect: Dialect = "faa") -> str:
    return digits(int(round(float(knots))), dialect)


def vertical_speed(fpm: float, dialect: Dialect = "faa") -> str:
    return digits(int(abs(round(float(fpm) / 100.0)) * 100), dialect)


# --------------------------------------------------------------------------
# frequencies
# --------------------------------------------------------------------------


def frequency(mhz: float | str, dialect: Dialect = "faa") -> str:
    """Speak a VHF frequency.

    US practice drops trailing zeros, so 121.900 is "one two one point niner"
    and 118.000 is "one one eight point zero". An 8.33 kHz channel keeps its
    third decimal, because 118.005 and 118.000 are different channels.
    """
    text = f"{float(mhz):.3f}" if not isinstance(mhz, str) else mhz
    whole, _, frac = text.partition(".")
    frac = (frac + "000")[:3]
    if frac[2] != "0":
        spoken_frac = digits(frac, dialect)  # true 8.33 kHz channel
    else:
        trimmed = frac[:2].rstrip("0") or "0"
        spoken_frac = digits(trimmed, dialect)
    # ICAO Annex 10 5.2.1.4.3 uses "decimal"; US practice says "point".
    separator = "decimal" if dialect == "icao" else "point"
    return f"{digits(whole, dialect)} {separator} {spoken_frac}"


def squawk(code: str | int, dialect: Dialect = "faa") -> str:
    return digits(f"{int(code):04d}", dialect)


# --------------------------------------------------------------------------
# runways, taxiways, pressure, wind
# --------------------------------------------------------------------------

_RUNWAY_SUFFIX = {"L": "left", "R": "right", "C": "center", "G": "grass", "W": "water"}


def runway(ident: str, dialect: Dialect = "faa",
           digit_dialect: Dialect | None = None) -> str:
    """``27L`` -> ``two seven left``.

    US runway designators carry no leading zero, so runway 4R is "runway four
    right". ICAO designators are always two digits and the zero is spoken:
    "runway zero four". Source data is inconsistent about which form it stores,
    so the identifier is normalised here rather than trusted.

    ``dialect`` decides the leading zero, which is a regional convention;
    ``digit_dialect`` decides the digit words, which is a separate style
    choice. They differ whenever ICAO phrasing is used with plain digits.
    """
    digit_dialect = digit_dialect or dialect
    ident = (ident or "").strip().upper().replace("RW", "")
    m = re.match(r"^0*(\d{1,2})\s*([LRCGW]?)$", ident)
    if not m:
        return digits_and_letters(ident, digit_dialect)
    number, suffix = int(m.group(1)), m.group(2)
    text = str(number) if dialect == "faa" else f"{number:02d}"
    spoken = digits(text, digit_dialect)
    if suffix:
        spoken += " " + _RUNWAY_SUFFIX[suffix]
    return spoken


def taxiway(ident: str) -> str:
    """``A``->``Alpha``, ``B4``->``Bravo four``, ``KILO``->``Kilo``."""
    ident = (ident or "").strip().upper()
    if len(ident) > 2 and ident.isalpha() and ident.capitalize() in _ALPHABET.values():
        return ident.capitalize()
    return digits_and_letters(ident)


def taxi_route(route: Iterable[str]) -> str:
    """``['A','B','C']`` -> ``Alpha, Bravo, Charlie``."""
    return ", ".join(taxiway(t) for t in route)


def altimeter(value: float, dialect: Dialect = "faa") -> str:
    """Pressure setting, in the units the region actually uses.

    The FAA reads inches of mercury without the decimal point -- 29.92 is
    "two niner niner two". ICAO regions read a hectopascal QNH.
    """
    if dialect == "faa":
        return digits(f"{float(value):.2f}".replace(".", ""), dialect)
    return digits(f"{int(round(float(value))):04d}", dialect)


def wind(direction: float | str, knots: float, gust: float | None = None,
         dialect: Dialect = "faa") -> str:
    if int(round(float(knots))) == 0:
        # Calm is calm from any direction, and after variation is applied
        # a calm wind is no longer from zero.
        return "wind calm"
    if isinstance(direction, str) and direction.upper().startswith("VRB"):
        base = "variable"
    else:
        # North is three six zero; "% 360" alone made it zero zero zero.
        base = digits(f"{int(round(float(direction))) % 360 or 360:03d}", dialect)
    text = f"wind {base} at {digits(int(round(knots)), dialect)}"
    if gust and gust > knots:
        text += f" gusting {digits(int(round(gust)), dialect)}"
    return text


def visibility(statute_miles: float, dialect: Dialect = "faa") -> str:
    if statute_miles >= 10:
        return "visibility one zero"
    if statute_miles >= 1:
        whole = int(statute_miles)
        frac = statute_miles - whole
        if frac < 0.2:
            return f"visibility {digits(whole, dialect)}"
        if frac < 0.4:
            return f"visibility {digits(whole, dialect)} and one quarter"
        if frac < 0.6:
            return f"visibility {digits(whole, dialect)} and one half"
        return f"visibility {digits(whole, dialect)} and three quarters"
    quarters = max(1, int(round(statute_miles * 4)))
    return {1: "visibility one quarter", 2: "visibility one half",
            3: "visibility three quarters"}.get(quarters, "visibility one quarter")


def time_utc(hour: int, minute: int, dialect: Dialect = "faa") -> str:
    """Zulu time, spoken as four digits."""
    return f"{digits(f'{hour % 24:02d}{minute % 60:02d}', dialect)} zulu"


def distance(nm: float, dialect: Dialect = "faa") -> str:
    n = int(round(nm))
    return f"{digits(n, dialect)} mile{'s' if n != 1 else ''}"


# --------------------------------------------------------------------------
# reverse direction: what the pilot said -> machine values
# --------------------------------------------------------------------------


def words_to_digits(text: str) -> str:
    """Collapse spoken digit words in a transcript into a numeric string.

    ``"climb one two thousand"`` keeps its words but ``"one two thousand"``
    embedded in a heading or squawk read-back becomes ``12``. Used by the intent
    parser to recover numbers Whisper transcribed as words.
    """
    out: list[str] = []
    for token in re.split(r"[\s,]+", text.lower()):
        token = token.strip(".")
        if token in _WORD_TO_DIGIT:
            out.append(_WORD_TO_DIGIT[token])
        elif token.isdigit():
            out.append(token)
        else:
            out.append(" " + token + " ")
    return re.sub(r"\s+", " ", "".join(out)).strip()


def phonetic_to_letters(text: str) -> str:
    """``"alpha bravo"`` -> ``"AB"``; leaves unrecognised words untouched."""
    out: list[str] = []
    for token in re.split(r"[\s,]+", text.lower()):
        token = token.strip(".")
        if token in _ALPHABET_REVERSE:
            out.append(_ALPHABET_REVERSE[token])
        elif token in _WORD_TO_DIGIT:
            out.append(_WORD_TO_DIGIT[token])
    return "".join(out)
