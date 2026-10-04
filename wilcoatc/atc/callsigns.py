"""A callsign, in whichever language it is being said in.

The written callsign never changes. How it is said does, in two ways.

*The telephony name.* AFR is "Airfrans" to an English-speaking controller and
"Air France" to a French one, because the second is reading a French name and
the first is approximating it. Most names are the same in every language --
they are international designators -- so only the ones that genuinely differ
are listed.

*The flight number.* English groups it into pairs *in the United States*, so
AFR1234 is "twelve thirty four" to a controller at Kennedy and "one two three
four" to one at Prague speaking English. FAA JO 7110.65 2-4-20 has the group
form; ICAO Doc 9432 has digit by digit, and that is what the rest of the world
does whichever language it is speaking. France reads the number as one whole
number, "mille deux cent trente-quatre". Spain, Germany, Italy and Portugal
read it digit by digit.

The region is why this takes a dialect. Without one, English was grouped
everywhere -- so an English-speaking controller in Czech airspace said "Delta
twelve thirty four", which is neither what ICAO prescribes nor a number said
in numerals, and it was the one place in the program that spoke a quantity as
a word.

Registrations are spelled with the ICAO alphabet, which is the same set of
words everywhere; only the pronunciation differs, and that comes from the voice
rather than from here.
"""

from __future__ import annotations

from . import numbers as num
from .speech import (_AIRLINE_RE, _ALPHABET, _NUMERIC_FLIGHT_RE, Callsign,
                     airline_table, flies_as_airline)

# Names English approximates and everybody else says properly. "Airfrans" and
# "Itarrow" exist so an English speaker can say them on a noisy channel; a
# controller who speaks the language says the name.
#
# Only a different *spelling* of the same name belongs here or in the table
# below. A different name -- "Transavia" for France Soleil, "Italia" for
# Itarrow -- is a disagreement with icao_callsigns.json, and the file wins.
INTERNATIONAL_TELEPHONY: dict[str, str] = {
    "AFR": "Air France", "AZA": "Alitalia",
    "TAP": "Air Portugal", "DLH": "Lufthansa",
    "IBE": "Iberia", "SWR": "Swiss", "AUA": "Austrian",
}

# Telephony names that are said differently in a language than in English.
# Anything not listed here is the same word in both, which is most of them.
LOCAL_TELEPHONY: dict[str, dict[str, str]] = {
    "fr": {"AFR": "Air France", "TAP": "Air Portugal", "AZA": "Alitalia"},
    "es": {"IBE": "Iberia", "VLG": "Vueling", "AEA": "Europa",
           "AVA": "Avianca", "ARG": "Argentina",
           "AMX": "Aeroméxico", "CMP": "Copa"},
    "de": {"DLH": "Lufthansa", "AUA": "Austrian", "SWR": "Swiss",
           "EWG": "Eurowings", "CFG": "Condor", "BER": "Air Berlin"},
    "it": {"AZA": "Alitalia", "AEZ": "Aeroitalia"},
    "pt": {"TAP": "Air Portugal", "AZU": "Azul", "TAM": "TAM"},
}

# How the flight number is read.
_WHOLE_NUMBER = frozenset({"fr"})


def telephony_for(icao: str, language: str,
                  overrides: dict[str, str] | None = None) -> str:
    """The radiotelephony name of an operator, in one language."""
    code = (icao or "").upper()
    # The built-in table with icao_callsigns.json over it; then what another
    # language says differently, then the pilot's own names.
    table = airline_table()
    if language != "en":
        table.update(INTERNATIONAL_TELEPHONY)
    table.update(LOCAL_TELEPHONY.get(language, {}))
    if overrides:
        table.update({k.upper(): v for k, v in overrides.items()})
    name = table.get(code, "")
    if name:
        return name
    # An operator the general table has never heard of, which is most of the
    # smaller ones. Falling through to spelling the callsign would turn
    # "Moonflower eight six niner" into "eight six niner".
    from .operators import TELEPHONY as OPERATOR_TELEPHONY

    return OPERATOR_TELEPHONY.get(code, "")


def parse_callsign(raw: str, aircraft_type: str = "", language: str = "en",
                   airline_names: dict[str, str] | None = None,
                   dialect: str = "faa") -> Callsign:
    """Build the spoken form of a callsign in a given language.

    English and French have their own readings and their own modules; every
    other language reads the flight number digit by digit.

    ``dialect`` is where the aeroplane is rather than what it speaks, and only
    English uses it: the group form is a US rule, so English over Europe reads
    the number digit by digit like everybody else there.
    """
    language = (language or "en").lower()[:2]

    if language == "en":
        from .speech import parse_callsign as parse_en

        # Its third argument is the dialect, not the overrides: the FAA groups
        # a flight number into pairs and ICAO reads it digit by digit.
        return parse_en(raw, aircraft_type, dialect,
                        airline_names=airline_names)
    if language == "fr":
        from .speech_fr import parse_callsign as parse_fr

        return parse_fr(raw, aircraft_type, airline_names)

    raw = (raw or "").strip().upper().replace("_", "")
    if not raw:
        return Callsign("UNKNOWN", "unknown aircraft", "unknown aircraft")

    match = _AIRLINE_RE.match(raw)
    if match and flies_as_airline(aircraft_type):
        name = telephony_for(match.group(1), language, airline_names)
        if name:
            flight = match.group(2)
            if not _NUMERIC_FLIGHT_RE.match(flight):
                spoken = f"{name} {_spell(flight, language)}"
                return Callsign(raw, spoken, spoken, telephony=name,
                                is_airline=True)
            suffix = ""
            if flight[-1].isalpha():
                suffix = f" {_ALPHABET[flight[-1]]}"
                flight = flight[:-1]
            spoken_flight = (num.cardinal(int(flight), language)
                             if language in _WHOLE_NUMBER
                             else num.digits(flight, language))
            spoken = f"{name} {spoken_flight}{suffix}"
            return Callsign(raw, spoken, spoken, telephony=name, is_airline=True)

    body = raw.replace("-", "")
    return Callsign(raw, _spell(body, language), _spell(body[-3:], language))


def _spell(text: str, language: str) -> str:
    """A registration, letter by letter and digit by digit.

    The alphabet is the ICAO one everywhere -- only the pronunciation differs,
    and that comes from the voice. The digits are the local words.
    """
    out: list[str] = []
    for ch in (text or ""):
        if ch.upper() in _ALPHABET:
            out.append(_ALPHABET[ch.upper()])
        elif ch.isdigit():
            out.append(num.digits(ch, language))
    return " ".join(out)


def every_language(raw: str, aircraft_type: str = "",
                   airline_names: dict[str, str] | None = None,
                   dialect: str = "faa") -> dict[str, Callsign]:
    """One spoken form per language this system can work in.

    Built once when a flight starts, so a controller anywhere can address the
    aircraft correctly without the callsign having to be re-parsed on every
    transmission. ``dialect`` is the region it is starting in, which is what
    decides whether English groups the flight number or reads it out.
    """
    from .language import SUPPORTED_LANGUAGES

    return {
        code: parse_callsign(raw, aircraft_type, code, airline_names, dialect)
        for code in SUPPORTED_LANGUAGES
    }


__all__ = ["parse_callsign", "every_language", "telephony_for", "LOCAL_TELEPHONY"]
