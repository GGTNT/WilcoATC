"""French aviation speech forms.

France runs genuinely bilingual ATC: a controller at Orly speaks French to Air
France and English to Speedbird, on the same frequency, switching by whichever
language the pilot used. This module is the French half of that.

French radiotelephony is not simply the English forms translated. The rule that
matters is *which numbers are read as digits and which as whole numbers*:

  * digit by digit -- runway, heading, transponder code, wind direction,
    flight levels ("piste zero six", "cap zero quatre zero")
  * as a whole number -- QNH, the megahertz part of a frequency, altitudes in
    feet, and air-carrier flight numbers ("QNH mille treize", "cent vingt et un
    decimale sept", "Air France mille deux cent trente-quatre")

Getting that split wrong is the single most obvious tell to a French-speaking
pilot, so each function below states which convention it follows.

Sources: SERA.14000 and following, the French AIP GEN 3.4, and DGAC guidance on
radiotelephony phraseology.
"""

from __future__ import annotations

import re
from .speech import Callsign, _ALPHABET

# --------------------------------------------------------------------------
# digits
# --------------------------------------------------------------------------

DIGITS_FR = {
    "0": "zéro",
    "1": "un",
    "2": "deux",
    "3": "trois",
    "4": "quatre",
    "5": "cinq",
    "6": "six",
    "7": "sept",
    "8": "huit",
    "9": "neuf",
}

# The phonetic alphabet is the ICAO one; only the pronunciation differs, and
# that comes from using a French voice rather than from different spellings.
ALPHABET_FR = dict(_ALPHABET)

_WORD_TO_DIGIT_FR = {
    "zéro": "0", "zero": "0",
    "un": "1", "une": "1",
    "deux": "2", "trois": "3", "quatre": "4", "cinq": "5",
    "six": "6", "sept": "7", "huit": "8", "neuf": "9",
}


def digits(value) -> str:
    """Speak each character individually: ``129`` -> ``un deux neuf``."""
    out: list[str] = []
    for ch in str(value):
        if ch in DIGITS_FR:
            out.append(DIGITS_FR[ch])
        elif ch.upper() in ALPHABET_FR:
            out.append(ALPHABET_FR[ch.upper()])
        elif ch == ".":
            out.append("décimale")
    return " ".join(out)


def letters(value: str) -> str:
    """Spell with the phonetic alphabet, digits stay digits."""
    out: list[str] = []
    for ch in (value or ""):
        if ch.upper() in ALPHABET_FR:
            out.append(ALPHABET_FR[ch.upper()])
        elif ch.isdigit():
            out.append(DIGITS_FR[ch])
    return " ".join(out)


# --------------------------------------------------------------------------
# cardinal numbers
# --------------------------------------------------------------------------

_UNITS = [
    "zéro", "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit",
    "neuf", "dix", "onze", "douze", "treize", "quatorze", "quinze", "seize",
    "dix-sept", "dix-huit", "dix-neuf",
]
_TENS = {
    20: "vingt", 30: "trente", 40: "quarante", 50: "cinquante", 60: "soixante",
}


def cardinal(number: int) -> str:
    """Write a whole number in French words.

    Handles the awkward parts of French counting that a naive table gets wrong:
    seventy is "soixante-dix", eighty is "quatre-vingts", ninety-one is
    "quatre-vingt-onze", and twenty-one takes the "et" ("vingt et un").
    """
    n = int(number)
    if n < 0:
        return f"moins {cardinal(-n)}"
    if n < 20:
        return _UNITS[n]

    if n < 70:
        tens, unit = divmod(n, 10)
        base = _TENS[tens * 10]
        if unit == 0:
            return base
        if unit == 1:
            return f"{base} et un"
        return f"{base}-{_UNITS[unit]}"

    if n < 80:
        # 70-79 counts on from sixty: soixante-dix, soixante et onze ...
        rest = n - 60
        if rest == 11:
            return "soixante et onze"
        return f"soixante-{_UNITS[rest]}"

    if n < 100:
        rest = n - 80
        if rest == 0:
            return "quatre-vingts"
        return f"quatre-vingt-{_UNITS[rest]}"

    if n < 1000:
        hundreds, rest = divmod(n, 100)
        head = "cent" if hundreds == 1 else f"{_UNITS[hundreds]} cent"
        if rest == 0:
            # "deux cents" takes an s only with nothing after it.
            return head if hundreds == 1 else f"{_UNITS[hundreds]} cents"
        return f"{head} {cardinal(rest)}"

    if n < 1_000_000:
        thousands, rest = divmod(n, 1000)
        head = "mille" if thousands == 1 else f"{cardinal(thousands)} mille"
        return head if rest == 0 else f"{head} {cardinal(rest)}"

    millions, rest = divmod(n, 1_000_000)
    head = "un million" if millions == 1 else f"{cardinal(millions)} millions"
    return head if rest == 0 else f"{head} {cardinal(rest)}"


# --------------------------------------------------------------------------
# altitudes and levels
# --------------------------------------------------------------------------


def flight_level(level: int) -> str:
    """Flight levels are read digit by digit: ``niveau de vol trois cinq zéro``."""
    return f"niveau de vol {digits(f'{int(level):03d}')}"


def altitude(feet: float, transition: int = 5000, level_from: int = 0) -> str:
    """Altitude in feet as a whole number, or a flight level above transition.

    French transition altitudes are published per terminal area and are much
    lower than the US 18,000 ft; 5,000 ft is the commonest value in France and
    is used as the default. At the transition altitude itself it is still an
    altitude (see :func:`wilcoatc.atc.speech.is_flight_level`).
    """
    from .speech import is_flight_level

    ft = int(round(float(feet) / 100.0) * 100)
    if is_flight_level(ft, transition, level_from):
        return flight_level(ft // 100)
    if ft <= 0:
        return "au niveau de la mer"
    return f"{cardinal(ft)} pieds"


def height(feet: float) -> str:
    """A height rather than an altitude, e.g. a circuit altitude."""
    return f"{cardinal(int(round(float(feet) / 100.0) * 100))} pieds"


# --------------------------------------------------------------------------
# headings, speeds, runways
# --------------------------------------------------------------------------


def heading(degrees: float) -> str:
    """``cap`` plus three digits, spoken individually."""
    deg = int(round(float(degrees))) % 360
    if deg == 0:
        deg = 360
    return digits(f"{deg:03d}")


def speed(knots: float) -> str:
    """Speeds are read as whole numbers: ``deux cent dix noeuds``."""
    return f"{cardinal(int(round(float(knots))))} noeuds"


_RUNWAY_SUFFIX_FR = {"L": "gauche", "R": "droite", "C": "centre",
                     "G": "gauche", "W": "eau"}


def runway(ident: str) -> str:
    """``27L`` -> ``deux sept gauche``. Always two digits in France."""
    ident = (ident or "").strip().upper().replace("RW", "")
    m = re.match(r"^0*(\d{1,2})\s*([LRCGW]?)$", ident)
    if not m:
        return letters(ident)
    number, suffix = int(m.group(1)), m.group(2)
    spoken = digits(f"{number:02d}")
    if suffix:
        spoken += " " + _RUNWAY_SUFFIX_FR[suffix]
    return spoken


def taxiway(ident: str) -> str:
    return letters((ident or "").strip().upper())


def taxi_route(route) -> str:
    return ", ".join(taxiway(t) for t in route)


# --------------------------------------------------------------------------
# frequencies, pressure, transponder
# --------------------------------------------------------------------------


def frequency(mhz: float | str) -> str:
    """``121.700`` -> ``cent vingt et un décimale sept``.

    The megahertz part is a whole number and the decimals are digits, which is
    what French controllers say and what a French pilot expects to hear.
    """
    text = f"{float(mhz):.3f}" if not isinstance(mhz, str) else mhz
    whole, _, frac = text.partition(".")
    frac = (frac + "000")[:3]
    if frac[2] != "0":
        spoken_frac = digits(frac)          # true 8.33 kHz channel
    else:
        spoken_frac = digits(frac[:2].rstrip("0") or "0")
    return f"{cardinal(int(whole))} décimale {spoken_frac}"


def qnh(hectopascals: float) -> str:
    """``QNH mille treize`` -- read as a whole number, not as digits."""
    return f"QNH {cardinal(int(round(float(hectopascals))))}"


def squawk(code) -> str:
    """Transponder codes are digits: ``quatre cinq deux un``."""
    return digits(f"{int(code):04d}")


def wind(direction, knots: float, gust: float | None = None) -> str:
    """``vent deux sept zéro degrés, dix noeuds``.

    The direction is digits and the speed is a whole number, which is the
    opposite convention within a single phrase and a common mistake.
    """
    if int(round(float(knots))) == 0:
        return "vent calme"
    if isinstance(direction, str) and direction.upper().startswith("VRB"):
        base = "variable"
    else:
        # North is trois six zéro, never zéro zéro zéro.
        base = f"{digits(f'{int(round(float(direction))) % 360 or 360:03d}')} degrés"
    text = f"vent {base}, {cardinal(int(round(knots)))} noeuds"
    if gust and gust > knots:
        text += f", rafales {cardinal(int(round(gust)))} noeuds"
    return text


def visibility(statute_miles: float) -> str:
    """French reports visibility in metres or kilometres, not statute miles."""
    metres = statute_miles * 1609.34
    if metres >= 9999:
        return "visibilité dix kilomètres ou plus"
    # Below 5 km, visibility is reported in metres rounded to the nearest
    # hundred; above that, in whole kilometres. Reporting 1,930 m as "two
    # kilometres" would overstate it by more than the reportable step.
    if metres >= 5000:
        return f"visibilité {cardinal(int(round(metres / 1000.0)))} kilomètres"
    if metres >= 800:
        return f"visibilité {cardinal(int(round(metres / 100.0) * 100))} mètres"
    return f"visibilité {cardinal(int(round(metres / 50.0) * 50))} mètres"


def temperature(celsius: float) -> str:
    value = int(round(celsius))
    return f"moins {cardinal(abs(value))}" if value < 0 else cardinal(value)


def time_utc(hour: int, minute: int) -> str:
    return f"{digits(f'{hour % 24:02d}{minute % 60:02d}')} UTC"


def distance(nm: float) -> str:
    n = int(round(nm))
    return f"{cardinal(n)} mille{'s' if n != 1 else ''} nautique{'s' if n != 1 else ''}"


# --------------------------------------------------------------------------
# callsigns
# --------------------------------------------------------------------------

# Radiotelephony designators as used on French frequencies. Air France is
# "Air France" in France, not the international "Airfrans".
TELEPHONY_FR: dict[str, str] = {
    "AFR": "Air France",
    "BAW": "Speedbird",
    "DLH": "Lufthansa",
    "KLM": "KLM",
    "IBE": "Iberia",
    "AZA": "Alitalia",
    "SWR": "Swiss",
    "TAP": "Air Portugal",
    "EZY": "Easy",
    "RYR": "Ryanair",
    "VLG": "Vueling",
    "AAL": "American",
    "DAL": "Delta",
    "UAL": "United",
    "ACA": "Air Canada",
    "UAE": "Emirates",
    "QTR": "Qatari",
    "THY": "Turkish",
    "SAS": "Scandinavian",
    "FIN": "Finnair",
    "AUA": "Austrian",
    "BEL": "Beeline",
    "CSA": "CSA Lines",
    "LOT": "Lot",
    "MSR": "Egyptair",
    "RAM": "Royal Air Maroc",
    "TAR": "Tunair",
    "DAH": "Air Algérie",
    "CRL": "Corsair",
    "FPO": "French Post",
    "SEU": "Star Europe",
    "AEE": "Aegean",
}

from .speech import _AIRLINE_RE, _NUMERIC_FLIGHT_RE, flies_as_airline  # noqa: E402


def parse_callsign(raw: str, aircraft_type: str = "",
                   airline_names: dict[str, str] | None = None) -> Callsign:
    """French spoken form of a callsign.

    Air-carrier flight numbers are read as whole French numbers, so AFR1234 is
    "Air France mille deux cent trente-quatre" -- not the pairwise grouping the
    FAA uses, and not digit by digit.
    """
    raw = (raw or "").strip().upper().replace("_", "")
    if not raw:
        return Callsign("INCONNU", "aéronef inconnu", "aéronef inconnu")

    # The file is the base; a name a French controller says differently is
    # above it, and the pilot's own names above that.
    from .airlines import telephony as file_telephony

    table = file_telephony()
    table.update(TELEPHONY_FR)
    if airline_names:
        table.update({k.upper(): v for k, v in airline_names.items()})

    m = _AIRLINE_RE.match(raw)
    if m and m.group(1) in table and flies_as_airline(aircraft_type):
        icao, flight = m.group(1), m.group(2)
        name = table[icao]
        if not _NUMERIC_FLIGHT_RE.match(flight):
            # BAW7A8 has no whole number in it to read: "sept Alpha huit".
            said = " ".join(ALPHABET_FR[c] if c.isalpha() else digits(c)
                            for c in flight)
            spoken = f"{name} {said}"
            return Callsign(raw, spoken, spoken, telephony=name, is_airline=True)
        suffix = ""
        if flight[-1].isalpha():
            suffix = f" {ALPHABET_FR[flight[-1]]}"
            flight = flight[:-1]
        spoken = f"{name} {cardinal(int(flight))}{suffix}"
        return Callsign(raw, spoken, spoken, telephony=name, is_airline=True)

    # Registrations are spelled out. A French registration is F-Gxxx, and once
    # two-way contact exists the last three characters are enough.
    body = raw.replace("-", "")
    full = letters(body)
    short = letters(body[-3:])
    return Callsign(raw, full, short)


# --------------------------------------------------------------------------
# reverse: understanding what a French-speaking pilot said
# --------------------------------------------------------------------------

_CARDINAL_WORDS = {
    "zéro": 0, "zero": 0, "un": 1, "une": 1, "deux": 2, "trois": 3,
    "quatre": 4, "cinq": 5, "six": 6, "sept": 7, "huit": 8, "neuf": 9,
    "dix": 10, "onze": 11, "douze": 12, "treize": 13, "quatorze": 14,
    "quinze": 15, "seize": 16, "vingt": 20, "trente": 30, "quarante": 40,
    "cinquante": 50, "soixante": 60, "cent": 100, "cents": 100,
    "mille": 1000,
}


# French builds 70-99 by addition rather than with its own words, so
# "quatre-vingt-dix" is literally "four-twenty-ten". Summing the parts gives 34.
# These compounds are collapsed to a single value before anything else is read.
_COMPOUNDS: tuple[tuple[str, int], ...] = (
    ("quatre[\\s-]+vingt[s]?[\\s-]+dix[\\s-]+neuf", 99),
    ("quatre[\\s-]+vingt[s]?[\\s-]+dix[\\s-]+huit", 98),
    ("quatre[\\s-]+vingt[s]?[\\s-]+dix[\\s-]+sept", 97),
    ("quatre[\\s-]+vingt[s]?[\\s-]+seize", 96),
    ("quatre[\\s-]+vingt[s]?[\\s-]+quinze", 95),
    ("quatre[\\s-]+vingt[s]?[\\s-]+quatorze", 94),
    ("quatre[\\s-]+vingt[s]?[\\s-]+treize", 93),
    ("quatre[\\s-]+vingt[s]?[\\s-]+douze", 92),
    ("quatre[\\s-]+vingt[s]?[\\s-]+onze", 91),
    ("quatre[\\s-]+vingt[s]?[\\s-]+dix", 90),
    ("quatre[\\s-]+vingt[s]?", 80),
    ("soixante[\\s-]+et[\\s-]+onze", 71),
    ("soixante[\\s-]+dix[\\s-]+neuf", 79),
    ("soixante[\\s-]+dix[\\s-]+huit", 78),
    ("soixante[\\s-]+dix[\\s-]+sept", 77),
    ("soixante[\\s-]+seize", 76),
    ("soixante[\\s-]+quinze", 75),
    ("soixante[\\s-]+quatorze", 74),
    ("soixante[\\s-]+treize", 73),
    ("soixante[\\s-]+douze", 72),
    ("soixante[\\s-]+dix", 70),
)

_COMPOUND_RES = tuple(
    (re.compile(pattern), value) for pattern, value in _COMPOUNDS
)


def words_to_number(text: str) -> int | None:
    """Parse a French number written in words back into an integer.

    Handles the compound forms ("quatre-vingt-dix", "mille treize",
    "deux cent dix") that a simple lookup table cannot.
    """
    lowered = (text or "").lower().strip()
    # Longest compounds first, so "quatre-vingt-dix-neuf" is not read as
    # "quatre-vingt-dix" followed by a stray "neuf".
    for pattern, value in _COMPOUND_RES:
        lowered = pattern.sub(f" {value} ", lowered)

    tokens = [
        t for t in re.split(r"[\s-]+", lowered)
        if t and t != "et"
    ]
    if not tokens:
        return None

    total = 0
    current = 0
    seen = False
    for token in tokens:
        if token in ("dix-sept", "dix-huit", "dix-neuf"):
            current += {"dix-sept": 17, "dix-huit": 18, "dix-neuf": 19}[token]
            seen = True
            continue
        value = _CARDINAL_WORDS.get(token)
        if value is None:
            if token.isdigit():
                current += int(token)
                seen = True
                continue
            return total + current if seen else None
        seen = True
        if value == 1000:
            total += (current or 1) * 1000
            current = 0
        elif value == 100:
            current = (current or 1) * 100
        else:
            current += value
    return total + current if seen else None


def digit_words_to_string(text: str) -> str:
    """Collapse spoken French digits into a numeric string."""
    out: list[str] = []
    for token in re.split(r"[\s-]+", (text or "").lower()):
        token = token.strip(".,")
        if token in _WORD_TO_DIGIT_FR:
            out.append(_WORD_TO_DIGIT_FR[token])
        elif token.isdigit():
            out.append(token)
    return "".join(out)


__all__ = [
    "digits", "letters", "cardinal", "altitude", "flight_level", "heading",
    "speed", "runway", "taxiway", "taxi_route", "frequency", "qnh", "squawk",
    "wind", "visibility", "temperature", "time_utc", "distance",
    "parse_callsign", "words_to_number", "digit_words_to_string",
    "TELEPHONY_FR", "DIGITS_FR",
]
