"""Which language a frequency is worked in, anywhere in the world.

Real ATC outside the anglophone world is bilingual. At Orly the same controller
says "Air France mille deux cent trente-quatre, autorisé décollage" and then,
seconds later on the same frequency, "Speedbird one seventeen, cleared for
takeoff". Two rules do all the work:

    English is always available, because ICAO Annex 10 requires it to be.

    the controller answers in the language the pilot used,
    provided that language is worked at that facility.

Nobody chooses any of this. The language follows the aeroplane: fly into Italy
and Italian joins the set the recogniser is listening for, and leaves it again
when you fly out. That is also what a real pilot experiences, and it is the
reason there is no language setting in the interface.

*Local* is only filled in where two things are both true: the state genuinely
works that language on frequency, and this system can actually speak it. Where
the first holds and the second does not -- Russia, China, Japan, Greece, Poland
-- English is what you get, which is what an international crew gets there too.

The same table decides the *accent* of English, because a controller at Orly
who switches to English is still a French speaker. That is why the voice is
chosen per facility and the language per transmission, rather than the other
way round.
"""

from __future__ import annotations

from dataclasses import dataclass

# Languages the controller can actually speak: there is a phrasebook, a voice
# and a recogniser prompt for each.
SPOKEN_HERE: frozenset[str] = frozenset({"en", "fr", "es", "de", "it", "pt"})


@dataclass(frozen=True)
class LanguageProfile:
    """The languages a region's controllers work in."""

    local: str            # ISO code of the local language, "" if English-only
    accent: str           # voice accent pool to draw from
    name: str = ""        # human-readable, for the console

    @property
    def bilingual(self) -> bool:
        return bool(self.local) and self.local != "en"

    def languages(self) -> list[str]:
        """Every language a pilot may legitimately use here, English first."""
        return ["en", self.local] if self.bilingual else ["en"]


def _p(local: str, accent: str, name: str) -> LanguageProfile:
    return LanguageProfile(local, accent, name)


# Keyed by ICAO location-indicator prefix. Longer prefixes win, so a specific
# airport beats its country and a country beats its region.
#
# `local` is an empty string wherever English is what is actually worked, or
# where the local language is worked but is one this system cannot speak. The
# accent is still regional in both cases, because a Warsaw controller speaking
# English does not sound American.
_PROFILES: tuple[tuple[tuple[str, ...], LanguageProfile], ...] = (

    # ---------------------------------------------------------- English --
    (("K", "PA", "PH", "PG", "PJ", "PK", "PL", "PM", "PT", "PW", "TJ", "TI"),
     _p("", "us", "United States")),
    (("C",), _p("", "us", "Canada")),
    (("EG",), _p("", "gb", "United Kingdom")),
    (("EI",), _p("", "gb", "Ireland")),
    (("LX",), _p("", "gb", "Gibraltar")),
    (("Y",), _p("", "gb", "Australia")),
    (("NZ",), _p("", "gb", "New Zealand")),
    (("BI", "BG"), _p("", "gb", "Iceland and Greenland")),
    (("EK", "ES", "EN", "EF", "EE", "EV", "EY"),
     _p("", "gb", "Northern Europe")),
    (("EH",), _p("", "nl", "Netherlands")),
    (("EB", "EL"), _p("", "nl", "Belgium and Luxembourg")),
    (("EP",), _p("", "de", "Poland")),
    (("LK", "LZ", "LH"), _p("", "de", "Central Europe")),
    (("LD", "LJ", "LQ", "LY", "LW", "LA", "LB", "LR", "LU"),
     _p("", "it", "South-eastern Europe")),
    (("LG", "LC", "LM", "LT", "LL", "LV"),
     _p("", "gb", "Eastern Mediterranean")),
    (("U",), _p("", "gb", "Russia and Central Asia")),
    (("Z", "R", "V", "W"), _p("", "gb", "Asia")),
    (("O",), _p("", "gb", "Middle East")),
    (("A", "N"), _p("", "gb", "Pacific")),
    (("F", "H", "D", "G"), _p("", "gb", "Africa")),
    (("M", "T", "S"), _p("", "es", "the Americas")),

    # ------------------------------------------------------------ French --
    (("LF", "LN"), _p("fr", "fr", "France")),
    (("TFF", "TFM"), _p("fr", "fr", "the French Antilles")),
    (("SOCA", "SOOO", "SOOG", "SOOM"), _p("fr", "fr", "French Guiana")),
    (("NTAA", "NTG", "NTT", "NTM"), _p("fr", "fr", "French Polynesia")),
    (("NWW", "NLWW"), _p("fr", "fr", "New Caledonia and Wallis")),
    (("FM",), _p("fr", "fr", "Madagascar and the Mascarenes")),
    (("DA",), _p("fr", "fr", "Algeria")),
    (("DT",), _p("fr", "fr", "Tunisia")),
    (("GM",), _p("fr", "fr", "Morocco")),
    (("GO", "GQ", "GU", "GA"), _p("fr", "fr", "West Africa")),
    (("DB", "DF", "DI", "DR", "DX"), _p("fr", "fr", "francophone West Africa")),
    (("FC", "FE", "FK", "FO", "FT", "FZ"),
     _p("fr", "fr", "francophone Central Africa")),
    (("HB", "HR", "HD"), _p("fr", "fr", "francophone East Africa")),
    (("MTPP", "MTCH"), _p("fr", "fr", "Haiti")),
    (("CYUL", "CYQB", "CYHU", "CYMX", "CYBG", "CYRQ", "CYVO", "CYYY"),
     _p("fr", "fr", "Quebec")),
    (("LSGG", "LSGS", "LSGL", "LSGN"),
     _p("fr", "fr", "French-speaking Switzerland")),

    # ----------------------------------------------------------- Spanish --
    (("LE", "GC", "GE"), _p("es", "es", "Spain")),
    (("MM",), _p("es", "es", "Mexico")),
    (("MG", "MH", "MN", "MP", "MR", "MS", "MU", "MD"),
     _p("es", "es", "Central America and the Spanish Caribbean")),
    (("SA",), _p("es", "es", "Argentina")),
    (("SC",), _p("es", "es", "Chile")),
    (("SE",), _p("es", "es", "Ecuador")),
    (("SG",), _p("es", "es", "Paraguay")),
    (("SK",), _p("es", "es", "Colombia")),
    (("SL",), _p("es", "es", "Bolivia")),
    (("SP",), _p("es", "es", "Peru")),
    (("SU",), _p("es", "es", "Uruguay")),
    (("SV",), _p("es", "es", "Venezuela")),
    (("GS",), _p("es", "es", "Western Sahara")),
    (("FG",), _p("es", "es", "Equatorial Guinea")),

    # ------------------------------------------------------------ German --
    (("ED", "ET"), _p("de", "de", "Germany")),
    (("LO",), _p("de", "de", "Austria")),
    (("LS",), _p("de", "de", "Switzerland")),

    # ----------------------------------------------------------- Italian --
    (("LI",), _p("it", "it", "Italy")),

    # -------------------------------------------------------- Portuguese --
    (("LP",), _p("pt", "pt", "Portugal")),
    (("SB", "SD", "SI", "SJ", "SN", "SS", "SW"), _p("pt", "pt", "Brazil")),
    (("FN",), _p("pt", "pt", "Angola")),
    (("FQ",), _p("pt", "pt", "Mozambique")),
    (("GV",), _p("pt", "pt", "Cape Verde")),
    (("GG",), _p("pt", "pt", "Guinea-Bissau")),
    (("FP",), _p("pt", "pt", "São Tomé and Príncipe")),

    # --- English-speaking exceptions inside otherwise Spanish blocks -----
    (("MK", "MW", "MY", "MZ", "MB", "MMUN"), _p("", "gb", "the Caribbean")),
    (("SM",), _p("", "nl", "Suriname")),
    (("SY",), _p("", "gb", "Guyana")),
    (("TA", "TB", "TD", "TG", "TK", "TL", "TN", "TQ", "TT", "TU", "TV", "TX"),
     _p("", "gb", "the Lesser Antilles")),
)

# Everything not matched above.
DEFAULT_PROFILE = LanguageProfile("", "gb", "international")


def profile_for(ident: str) -> LanguageProfile:
    """The language profile of a facility, by ICAO identifier.

    Longer prefixes are tried first so that a specific airport ("LSGG",
    Geneva) beats its country ("LS", Switzerland), and a country beats the
    region it sits in.
    """
    code = (ident or "").upper()
    if not code:
        return DEFAULT_PROFILE
    best: tuple[int, LanguageProfile] | None = None
    for prefixes, profile in _PROFILES:
        for prefix in prefixes:
            if code.startswith(prefix):
                if best is None or len(prefix) > best[0]:
                    best = (len(prefix), profile)
    return best[1] if best else DEFAULT_PROFILE


def languages_for(ident: str) -> list[str]:
    """Languages a pilot may use at a facility, English first."""
    return profile_for(ident).languages()


def accent_for_ident(ident: str) -> str:
    """Which voice pool a facility's controllers are drawn from."""
    return profile_for(ident).accent


def local_language(ident: str) -> str:
    """The local language worked at a facility, or "" for English only."""
    return profile_for(ident).local


def reply_language(ident: str, heard_language: str,
                   allowed: list[str] | None = None) -> str:
    """Pick the language to answer in.

    Answers in whatever the pilot used, if that language is worked at this
    facility. Otherwise falls back to English, which is always available.

    ``allowed`` narrows the set further, which is only used when something
    outside has a reason to restrict it; nothing in normal operation does.
    """
    heard = (heard_language or "en").lower()[:2]
    spoken_here = languages_for(ident)
    if allowed is not None:
        spoken_here = [lang for lang in spoken_here if lang in allowed] or ["en"]
    return heard if heard in spoken_here else "en"


# --------------------------------------------------------------------------
# phraseology selection
# --------------------------------------------------------------------------


def phraseology_for(language: str, ident: str = "", strict_icao_digits: bool = False):
    """Build the phraseology object for a language and region.

    English gets FAA or ICAO wording according to where the facility is, which
    is a separate axis from the language: English at Orly is ICAO English, not
    US English. French has its own hand-written class; the rest are built from
    phrasebooks.

    Where the field is also decides where altitudes become flight levels, and
    that is carried on the object rather than passed at each call. Anything
    built without an ident keeps its own regional default, which is what a
    test with no airport in it gets.
    """
    language = (language or "en").lower()[:2]
    from ..navdata.db import transition_altitude

    transition = transition_altitude(ident) if ident else 0

    if language == "fr":
        from .phraseology_fr import FrenchPhraseology

        return FrenchPhraseology(strict_icao_digits=strict_icao_digits,
                                 transition=transition)

    if language != "en":
        from .phrasebook import LocalisedPhraseology
        from .phrasebooks import BOOKS

        book = BOOKS.get(language)
        if book is not None:
            return LocalisedPhraseology(book,
                                        strict_icao_digits=strict_icao_digits,
                                        transition=transition)

    from ..navdata.db import dialect_for
    from .phraseology import Phraseology

    return Phraseology(dialect_for(ident),
                       strict_icao_digits=strict_icao_digits,
                       transition=transition or 18000)


# Human-readable names, for the console and the transcript.
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
}

SUPPORTED_LANGUAGES = tuple(LANGUAGE_NAMES)


__all__ = [
    "LanguageProfile", "profile_for", "languages_for", "accent_for_ident",
    "local_language", "reply_language", "phraseology_for", "LANGUAGE_NAMES",
    "SUPPORTED_LANGUAGES", "SPOKEN_HERE", "DEFAULT_PROFILE",
]
