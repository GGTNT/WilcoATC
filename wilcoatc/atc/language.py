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
    # The local language is one a pilot may choose, not the one the field's
    # own traffic works in: Kortrijk accepts French and is worked in
    # English, and its traffic spoke French because of it.
    secondary: bool = False

    @property
    def bilingual(self) -> bool:
        return bool(self.local) and self.local != "en"

    def languages(self) -> list[str]:
        """Every language a pilot may legitimately use here, English first."""
        return ["en", self.local] if self.bilingual else ["en"]


def _p(local: str, accent: str, name: str,
       secondary: bool = False) -> LanguageProfile:
    return LanguageProfile(local, accent, name, secondary)


# Keyed by ICAO location-indicator prefix. Longer prefixes win, so a specific
# airport beats its country and a country beats its region.
#
# `local` is an empty string wherever English is what is actually worked, or
# where the local language is worked but is one this system cannot speak. The
# accent is still regional in both cases, because a Warsaw controller speaking
# English does not sound American.
_PROFILES: tuple[tuple[tuple[str, ...], LanguageProfile], ...] = (

    # ---------------------------------------------------------- English --
    # Measured through the whole chain (scripts/accent_calibrate.py), these
    # languages' Piper voices lost more than a quarter of their English at
    # every accent strength, the best of each shown; their countries are
    # British-voiced like those Piper has no voice for at all. Their own
    # language, where it is one a controller speaks, is still theirs.
    #   Albanian 28%
    #   Arabic 31%
    #   Armenian 30%
    #   Bengali 39%
    #   Danish 34%
    #   Estonian 27%
    #   European Portuguese 55%
    #   Finnish 33%
    #   Georgian 31%
    #   Greek 60%
    #   Hungarian 36%
    #   Kazakh 71%
    #   Korean 91%
    #   Luxembourgish 44%
    #   Nepali 59%
    #   Norwegian 42%
    #   Persian 41%
    #   Slovak 54%
    #   Slovene 26%
    #   Swedish 37%
    #   Turkish 28%
    #   Ukrainian 30%
    #   Vietnamese 68%
    # The accent is the country's own: a Piper voice of its language reading
    # English (wilcoatc.audio.voices, wilcoatc.audio.accent), and "gb", the
    # neutral British pool, where Piper has no voice of that language espeak
    # can drive -- never a neighbour's. The US and Canada are Supertonic's
    # American English; the UK and Ireland Piper's British voices.
    (("K", "PA", "PH", "PG", "PJ", "PK", "PL", "PM", "PT", "PW", "TJ", "TI"),
     _p("", "us", "United States")),
    (("C",), _p("", "us", "Canada")),
    (("EG",), _p("", "gb", "United Kingdom")),
    (("EI",), _p("", "gb", "Ireland")),
    (("LX",), _p("", "gb", "Gibraltar")),
    # UNVERIFIED: Australia, New Zealand and South Africa in the British
    # voices. Piper has no voice with any of their accents.
    (("Y",), _p("", "gb", "Australia")),
    (("NZ",), _p("", "gb", "New Zealand")),
    (("FA",), _p("", "gb", "South Africa")),

    # --- Northern Europe ---
    (("EK",), _p("", "gb", "Denmark and the Faroes")),
    (("BG",), _p("", "gb", "Greenland")),
    (("ES",), _p("", "gb", "Sweden")),
    (("EN",), _p("", "gb", "Norway")),
    (("EF",), _p("", "gb", "Finland")),
    (("BI",), _p("", "is", "Iceland")),
    (("EE",), _p("", "gb", "Estonia")),
    (("EV",), _p("", "lv", "Latvia")),
    # Piper's Lithuanian models take their own phoneme set, not espeak's.
    (("EY",), _p("", "gb", "Lithuania")),
    (("EH",), _p("", "nl", "Netherlands")),
    # Belgium is not the Netherlands: "be" is Flemish, the neutral British
    # voices -- a Dutch voice reading English was heard as German at
    # Kortrijk. Wallonia and Brussels below.
    (("EB",), _p("", "be", "Flanders")),
    (("EL",), _p("", "gb", "Luxembourg")),

    # --- Central and Eastern Europe ---
    (("EP",), _p("", "pl", "Poland")),
    (("LK",), _p("", "cs", "Czechia")),
    (("LZ",), _p("", "gb", "Slovakia")),
    (("LH",), _p("", "gb", "Hungary")),
    (("LR",), _p("", "ro", "Romania")),
    (("LU",), _p("", "ro", "Moldova")),
    (("LB",), _p("", "bg", "Bulgaria")),
    (("LJ",), _p("", "gb", "Slovenia")),
    # Piper has no Croatian, Bosnian or Macedonian, and its "Serbian" model
    # is Sorbian (serbski institut): neutral British, not a neighbour.
    (("LD",), _p("", "gb", "Croatia")),
    (("LQ",), _p("", "gb", "Bosnia and Herzegovina")),
    (("LW",), _p("", "gb", "North Macedonia")),
    (("LY",), _p("", "gb", "Serbia and Montenegro")),
    (("LA", "BK"), _p("", "gb", "Albania and Kosovo")),
    (("LG",), _p("", "gb", "Greece")),
    (("LC",), _p("", "gb", "Cyprus")),
    (("LT",), _p("", "gb", "Turkey")),
    (("LM",), _p("", "gb", "Malta")),
    # Piper's Hebrew model takes its own phoneme set.
    (("LL",), _p("", "gb", "Israel")),
    (("LV",), _p("", "gb", "Palestine")),

    # --- the former Soviet Union ---
    (("U",), _p("", "ru", "Russia")),
    (("UM",), _p("", "ru", "Belarus")),            # Russian is official
    (("UK",), _p("", "gb", "Ukraine")),
    (("UA",), _p("", "gb", "Kazakhstan")),
    (("UC",), _p("", "ru", "Kyrgyzstan")),         # Russian is official
    (("UTD",), _p("", "ru", "Tajikistan")),        # Russian by the constitution
    (("UT",), _p("", "gb", "Uzbekistan and Turkmenistan")),
    (("UB",), _p("", "gb", "Azerbaijan")),
    (("UG",), _p("", "gb", "Georgia")),
    (("UD",), _p("", "gb", "Armenia")),

    # --- the Middle East and North Africa ---
    (("OM", "OT", "OB", "OK", "OO", "OE", "OY", "OR", "OS", "OJ", "OL"),
     _p("", "gb", "the Gulf and the Levant")),
    (("HE", "HL", "HS"), _p("", "gb", "Egypt, Libya and Sudan")),
    (("OI",), _p("", "gb", "Iran")),
    (("OA",), _p("", "gb", "Afghanistan")),        # Dari is Persian
    (("OP",), _p("", "ur", "Pakistan")),

    # --- Asia ---
    (("VA", "VE", "VI", "VO"), _p("", "hi", "India")),
    (("VG",), _p("", "gb", "Bangladesh")),
    (("VN",), _p("", "gb", "Nepal")),
    (("VV",), _p("", "gb", "Vietnam")),
    (("ZK", "RK"), _p("", "gb", "Korea")),
    (("WA", "WI", "WR"), _p("", "id", "Indonesia")),
    # Piper's Japanese and Thai models take their own phoneme sets, its one
    # Mandarin voice espeak drives is unintelligible in English (95% of the
    # words lost, 70% with tones; wilcoatc.audio.voices); Mongolian,
    # Cantonese, Malay, Tagalog, Sinhala, Burmese, Khmer and Lao it does not
    # have.
    (("Z", "RC", "ZM", "RJ", "RO", "VT", "VH", "VM", "WM", "WB", "WS", "RP",
      "VC", "VY", "VD", "VL", "VQ", "VR"),
     _p("", "gb", "the rest of Asia")),
    (("R", "V", "W", "O", "A", "N"), _p("", "gb", "Asia and the Pacific")),
    (("F", "H", "D", "G"), _p("", "gb", "Africa")),
    (("M", "T", "S"), _p("", "es-419", "the Americas")),

    # ------------------------------------------------------------ French --
    (("LF", "LN"), _p("fr", "fr", "France")),
    (("TFF", "TFM"), _p("fr", "fr", "the French Antilles")),
    (("SOCA", "SOOO", "SOOG", "SOOM"), _p("fr", "fr", "French Guiana")),
    (("NTAA", "NTG", "NTT", "NTM"), _p("fr", "fr", "French Polynesia")),
    (("NWW", "NLWW"), _p("fr", "fr", "New Caledonia and Wallis")),
    (("FM",), _p("fr", "fr", "Madagascar and the Mascarenes")),
    # The Maghreb works French on the radio and Arabic is its language: its
    # English has the Arabic accent.
    (("DA",), _p("fr", "gb", "Algeria")),
    (("DT",), _p("fr", "gb", "Tunisia")),
    (("GM",), _p("fr", "gb", "Morocco")),
    (("GQ",), _p("fr", "gb", "Mauritania")),
    (("GO", "GU", "GA"), _p("fr", "fr", "West Africa")),
    (("DB", "DF", "DI", "DR", "DX"), _p("fr", "fr", "francophone West Africa")),
    (("FC", "FE", "FK", "FO", "FT", "FZ"),
     _p("fr", "fr", "francophone Central Africa")),
    (("HB", "HR", "HD"), _p("fr", "fr", "francophone East Africa")),
    (("MTPP", "MTCH"), _p("fr", "fr", "Haiti")),
    (("CYUL", "CYQB", "CYHU", "CYMX", "CYBG", "CYRQ", "CYVO", "CYYY"),
     _p("fr", "fr", "Quebec")),
    (("LSGG", "LSGS", "LSGL", "LSGN"),
     _p("fr", "fr", "French-speaking Switzerland")),
    # Belgium works English, but Namur is in Wallonia and its radio is worked
    # in French as well. Kortrijk, in Flanders, may be called in French too;
    # its controllers keep the Flemish voices the rest of Belgium has, so
    # adding the language re-casts nobody there.
    (("EBNM",), _p("fr", "fr", "Wallonia")),
    (("EBKT",), _p("fr", "be", "Kortrijk", secondary=True)),
    # The rest of Wallonia works English with a French accent, and Brussels,
    # bilingual, with a neutral one. Fields in the German-speaking east
    # (Büllingen) are left Flemish-default: nothing better is known.
    (("EBAR", "EBBX", "EBSU", "EBSH", "EBMG", "EBCF", "EBFS", "EBML", "EBSG",
      "EBCI", "EBSP", "EBTY", "EBBZ", "EBTX", "EBBY", "EBCV", "EBLG", "EBIS",
      "EBAV", "EBAM", "EBBE"),
     _p("", "fr", "Wallonia")),
    (("EBBR", "EBMB"), _p("", "gb", "Brussels")),

    # ----------------------------------------------------------- Spanish --
    (("LE", "GC", "GE"), _p("es", "es", "Spain")),
    (("MM",), _p("es", "es-419", "Mexico")),
    (("MG", "MH", "MN", "MP", "MR", "MS", "MU", "MD"),
     _p("es", "es-419", "Central America and the Spanish Caribbean")),
    (("SA",), _p("es", "es-419", "Argentina")),
    (("SC",), _p("es", "es-419", "Chile")),
    (("SE",), _p("es", "es-419", "Ecuador")),
    (("SG",), _p("es", "es-419", "Paraguay")),
    (("SK",), _p("es", "es-419", "Colombia")),
    (("SL",), _p("es", "es-419", "Bolivia")),
    (("SP",), _p("es", "es-419", "Peru")),
    (("SU",), _p("es", "es-419", "Uruguay")),
    (("SV",), _p("es", "es-419", "Venezuela")),
    (("GS",), _p("", "gb", "Western Sahara")),
    (("FG",), _p("es", "es", "Equatorial Guinea")),

    # ------------------------------------------------------------ German --
    (("ED", "ET"), _p("de", "de", "Germany")),
    (("LO",), _p("de", "de", "Austria")),
    (("LS",), _p("de", "de", "Switzerland")),

    # ----------------------------------------------------------- Italian --
    (("LI",), _p("it", "it", "Italy")),
    # Lugano is in Ticino: Italian, not the German of the rest of Switzerland.
    (("LSZA",), _p("it", "it", "Italian-speaking Switzerland")),

    # -------------------------------------------------------- Portuguese --
    (("LP",), _p("pt", "gb", "Portugal")),
    (("SB", "SD", "SI", "SJ", "SN", "SS", "SW"), _p("pt", "pt-br", "Brazil")),
    (("FN",), _p("pt", "gb", "Angola")),
    (("FQ",), _p("pt", "gb", "Mozambique")),
    (("GV",), _p("pt", "gb", "Cape Verde")),
    (("GG",), _p("pt", "gb", "Guinea-Bissau")),
    (("FP",), _p("pt", "gb", "São Tomé and Príncipe")),

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


def traffic_language(ident: str) -> str:
    """What the field's own light traffic speaks: the local language where
    the field is worked in it, "" (English) where it is only accepted."""
    profile = profile_for(ident)
    return "" if profile.secondary else profile.local


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


def phraseology_for(language: str, ident: str = "", strict_icao_digits: bool = False,
                    region: str = "auto"):
    """Build the phraseology object for a language and region.

    English gets FAA or ICAO wording according to where the facility is, which
    is a separate axis from the language: English at Orly is ICAO English, not
    US English. French has its own hand-written class; the rest are built from
    phrasebooks.

    Where the field is also decides where altitudes become flight levels, and
    that is carried on the object rather than passed at each call. Anything
    built without an ident keeps its own regional default, which is what a
    test with no airport in it gets.

    ``region`` is the pilot's pinned one (``flight.region``), which is the
    VFR words' and the numbers' everywhere -- except in French, whose own
    forms are the DGAC's wherever it is spoken. "auto" is the field's.
    """
    language = (language or "en").lower()[:2]
    p = _phraseology(language, ident, strict_icao_digits,
                     "" if region == "auto" else region)
    # In 8.33 airspace a frequency is said by its channel name, the number
    # the AIP publishes and the radio shows (channels.spoken_channel).
    p.channels = ident or ""
    if region != "auto":
        if language != "fr":
            p.region = region
        return p
    if ident:
        # The VFR words follow the field's region, whatever the language:
        # English at Shoreham is CAP 413, English at Kortrijk plain ICAO.
        # French keeps the DGAC's forms everywhere it is spoken.
        from ..navdata.db import vfr_region

        region = vfr_region(ident)
        if language != "fr":
            p.region = region
    return p


def _phraseology(language: str, ident: str, strict_icao_digits: bool,
                 pinned: str = ""):
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

    dialect = (("faa" if pinned == "faa" else "icao") if pinned
               else dialect_for(ident))
    return Phraseology(dialect,
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
    "traffic_language",
    "local_language", "reply_language", "phraseology_for", "LANGUAGE_NAMES",
    "SUPPORTED_LANGUAGES", "SPOKEN_HERE", "DEFAULT_PROFILE",
]
