"""Numbers, in the languages the controllers work in.

Radiotelephony splits every number into one of two readings, and the split is
not the same in every language:

*digit by digit* -- runways, headings, transponder codes, flight levels, the
decimals of a frequency. "two seven left", "cap zero quatre zero".

*as a whole number* -- altitudes in feet, the pressure setting, speeds, the
megahertz part of a frequency. "one two thousand" in English is an exception
that proves the rule: the FAA reads thousands as digits plus the word, where
France reads the same altitude as "trois mille pieds".

Getting the split wrong is the single most obvious tell to a native speaker, so
each language's table says which convention it follows and the spelling is
written out rather than assembled from a lookup, because every one of these
languages has an irregular stretch: Spanish glues its twenties together
("veintiuno"), German inverts its tens and units ("einundzwanzig"), Italian
elides vowels ("ventuno"), Portuguese needs its "e" in the right places, and
Dutch inverts like German but spells it as one word.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------
# digits
# --------------------------------------------------------------------------

# Aviation uses a couple of digits that differ from the ordinary word, to keep
# them apart on a noisy channel. German says "zwo" for two so it cannot be
# heard as "drei", exactly as English says "niner" so it cannot be heard as
# "five".
DIGITS: dict[str, dict[str, str]] = {
    "es": {"0": "cero", "1": "uno", "2": "dos", "3": "tres", "4": "cuatro",
           "5": "cinco", "6": "seis", "7": "siete", "8": "ocho", "9": "nueve"},
    "de": {"0": "null", "1": "eins", "2": "zwo", "3": "drei", "4": "vier",
           "5": "fünf", "6": "sechs", "7": "sieben", "8": "acht", "9": "neun"},
    "it": {"0": "zero", "1": "uno", "2": "due", "3": "tre", "4": "quattro",
           "5": "cinque", "6": "sei", "7": "sette", "8": "otto", "9": "nove"},
    "pt": {"0": "zero", "1": "um", "2": "dois", "3": "três", "4": "quatro",
           "5": "cinco", "6": "seis", "7": "sete", "8": "oito", "9": "nove"},
    "nl": {"0": "nul", "1": "een", "2": "twee", "3": "drie", "4": "vier",
           "5": "vijf", "6": "zes", "7": "zeven", "8": "acht", "9": "negen"},
}


def digits(value, language: str) -> str:
    """Speak each character on its own: ``129`` -> ``uno dos nueve``."""
    from .speech import _ALPHABET

    table = DIGITS[language]
    out: list[str] = []
    for ch in str(value):
        if ch in table:
            out.append(table[ch])
        elif ch.upper() in _ALPHABET:
            out.append(_ALPHABET[ch.upper()])
        elif ch == ".":
            out.append(DECIMAL_WORD[language])
    return " ".join(out)


# The word between the megahertz and the decimals of a frequency.
DECIMAL_WORD: dict[str, str] = {
    "es": "decimal", "de": "Komma", "it": "decimale", "pt": "decimal",
    "nl": "decimaal",
}


# --------------------------------------------------------------------------
# whole numbers
# --------------------------------------------------------------------------

_ES_UNITS = ["cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete",
             "ocho", "nueve", "diez", "once", "doce", "trece", "catorce",
             "quince", "dieciséis", "diecisiete", "dieciocho", "diecinueve",
             "veinte", "veintiuno", "veintidós", "veintitrés", "veinticuatro",
             "veinticinco", "veintiséis", "veintisiete", "veintiocho",
             "veintinueve"]
_ES_TENS = {30: "treinta", 40: "cuarenta", 50: "cincuenta", 60: "sesenta",
            70: "setenta", 80: "ochenta", 90: "noventa"}
_ES_HUNDREDS = {1: "ciento", 2: "doscientos", 3: "trescientos",
                4: "cuatrocientos", 5: "quinientos", 6: "seiscientos",
                7: "setecientos", 8: "ochocientos", 9: "novecientos"}


def _spanish(n: int) -> str:
    if n < 30:
        return _ES_UNITS[n]
    if n < 100:
        tens, unit = divmod(n, 10)
        base = _ES_TENS[tens * 10]
        return base if unit == 0 else f"{base} y {_ES_UNITS[unit]}"
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        if hundreds == 1 and rest == 0:
            return "cien"
        head = _ES_HUNDREDS[hundreds]
        return head if rest == 0 else f"{head} {_spanish(rest)}"
    thousands, rest = divmod(n, 1000)
    head = "mil" if thousands == 1 else f"{_spanish(thousands)} mil"
    return head if rest == 0 else f"{head} {_spanish(rest)}"


_DE_UNITS = ["null", "eins", "zwei", "drei", "vier", "fünf", "sechs", "sieben",
             "acht", "neun", "zehn", "elf", "zwölf", "dreizehn", "vierzehn",
             "fünfzehn", "sechzehn", "siebzehn", "achtzehn", "neunzehn"]
_DE_TENS = {20: "zwanzig", 30: "dreißig", 40: "vierzig", 50: "fünfzig",
            60: "sechzig", 70: "siebzig", 80: "achtzig", 90: "neunzig"}


def _german(n: int) -> str:
    """German inverts its tens and units and writes the whole thing as a word."""
    if n < 20:
        return _DE_UNITS[n]
    if n < 100:
        tens, unit = divmod(n, 10)
        base = _DE_TENS[tens * 10]
        if unit == 0:
            return base
        # "einundzwanzig", not "einsundzwanzig".
        head = "ein" if unit == 1 else _DE_UNITS[unit]
        return f"{head}und{base}"
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        head = "einhundert" if hundreds == 1 else f"{_DE_UNITS[hundreds]}hundert"
        return head if rest == 0 else f"{head}{_german(rest)}"
    thousands, rest = divmod(n, 1000)
    head = "eintausend" if thousands == 1 else f"{_german(thousands)}tausend"
    return head if rest == 0 else f"{head}{_german(rest)}"


_IT_UNITS = ["zero", "uno", "due", "tre", "quattro", "cinque", "sei", "sette",
             "otto", "nove", "dieci", "undici", "dodici", "tredici",
             "quattordici", "quindici", "sedici", "diciassette", "diciotto",
             "diciannove"]
_IT_TENS = {20: "venti", 30: "trenta", 40: "quaranta", 50: "cinquanta",
            60: "sessanta", 70: "settanta", 80: "ottanta", 90: "novanta"}


def _italian(n: int) -> str:
    """Italian drops the final vowel of a ten before "uno" and "otto"."""
    if n < 20:
        return _IT_UNITS[n]
    if n < 100:
        tens, unit = divmod(n, 10)
        base = _IT_TENS[tens * 10]
        if unit == 0:
            return base
        if unit in (1, 8):
            base = base[:-1]
        return f"{base}{_IT_UNITS[unit]}"
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        head = "cento" if hundreds == 1 else f"{_IT_UNITS[hundreds]}cento"
        return head if rest == 0 else f"{head}{_italian(rest)}"
    thousands, rest = divmod(n, 1000)
    head = "mille" if thousands == 1 else f"{_italian(thousands)}mila"
    return head if rest == 0 else f"{head}{_italian(rest)}"


_PT_UNITS = ["zero", "um", "dois", "três", "quatro", "cinco", "seis", "sete",
             "oito", "nove", "dez", "onze", "doze", "treze", "catorze",
             "quinze", "dezasseis", "dezassete", "dezoito", "dezanove"]
_PT_TENS = {20: "vinte", 30: "trinta", 40: "quarenta", 50: "cinquenta",
            60: "sessenta", 70: "setenta", 80: "oitenta", 90: "noventa"}
_PT_HUNDREDS = {1: "cento", 2: "duzentos", 3: "trezentos", 4: "quatrocentos",
                5: "quinhentos", 6: "seiscentos", 7: "setecentos",
                8: "oitocentos", 9: "novecentos"}


def _portuguese(n: int) -> str:
    """Portuguese joins with "e" between every group below a hundred."""
    if n < 20:
        return _PT_UNITS[n]
    if n < 100:
        tens, unit = divmod(n, 10)
        base = _PT_TENS[tens * 10]
        return base if unit == 0 else f"{base} e {_PT_UNITS[unit]}"
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        if hundreds == 1 and rest == 0:
            return "cem"
        head = _PT_HUNDREDS[hundreds]
        return head if rest == 0 else f"{head} e {_portuguese(rest)}"
    thousands, rest = divmod(n, 1000)
    head = "mil" if thousands == 1 else f"{_portuguese(thousands)} mil"
    return head if rest == 0 else f"{head} e {_portuguese(rest)}"


_NL_UNITS = ["nul", "een", "twee", "drie", "vier", "vijf", "zes", "zeven",
             "acht", "negen", "tien", "elf", "twaalf", "dertien", "veertien",
             "vijftien", "zestien", "zeventien", "achttien", "negentien"]
_NL_TENS = {20: "twintig", 30: "dertig", 40: "veertig", 50: "vijftig",
            60: "zestig", 70: "zeventig", 80: "tachtig", 90: "negentig"}


def _dutch(n: int) -> str:
    """Dutch inverts like German, and takes an "-en-" between the two."""
    if n < 20:
        return _NL_UNITS[n]
    if n < 100:
        tens, unit = divmod(n, 10)
        base = _NL_TENS[tens * 10]
        if unit == 0:
            return base
        head = _NL_UNITS[unit]
        # A unit ending in a vowel takes an extra s: "tweeëntwintig".
        joiner = "ën" if head.endswith(("e", "a", "u")) else "en"
        return f"{head}{joiner}{base}"
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        head = "honderd" if hundreds == 1 else f"{_NL_UNITS[hundreds]}honderd"
        return head if rest == 0 else f"{head} {_dutch(rest)}"
    thousands, rest = divmod(n, 1000)
    head = "duizend" if thousands == 1 else f"{_dutch(thousands)} duizend"
    return head if rest == 0 else f"{head} {_dutch(rest)}"


_SPELLERS = {"es": _spanish, "de": _german, "it": _italian,
             "pt": _portuguese, "nl": _dutch}


def cardinal(number: int, language: str) -> str:
    """Write a whole number in words."""
    n = int(number)
    if n < 0:
        return f"-{cardinal(-n, language)}"
    return _SPELLERS[language](n)


# --------------------------------------------------------------------------
# reading whole numbers back
# --------------------------------------------------------------------------
#
# The pilot says numbers too, so the same knowledge is needed in reverse. It is
# built by inverting the spellers above rather than written a second time,
# which means the parser can read back exactly what the controller can say and
# the two can never drift apart.
#
# Inverting also solves the compounding problem for free. German and Italian
# write a whole number as a single word -- "einhundertsiebenundzwanzig",
# "milletredici" -- which no token-by-token walk can take apart, but which a
# table built from the speller contains verbatim.

# Values worth having a word for: every number up to 2100 (headings, speeds,
# QNH, the megahertz part of a frequency, small altitudes) and then every
# hundred and every thousand up to 60,000 (levels and altitudes).
def _worth_spelling() -> list[int]:
    values = list(range(0, 2101))
    values += [n for n in range(2200, 60001, 100)]
    return values


# Tokens that multiply or scale rather than add, per language. These are the
# joins a compound is built on, so they never appear alone in the table above.
_SCALE: dict[str, dict[str, int]] = {
    "es": {"cien": 100, "ciento": 100, "mil": 1000},
    "de": {"hundert": 100, "einhundert": 100, "tausend": 1000,
           "eintausend": 1000},
    "it": {"cento": 100, "mille": 1000, "mila": 1000},
    "pt": {"cem": 100, "cento": 100, "mil": 1000},
    "nl": {"honderd": 100, "eenhonderd": 100, "duizend": 1000,
           "eenduizend": 1000},
    "fr": {"cent": 100, "cents": 100, "mille": 1000},
}

# Words that join parts of a number without carrying a value of their own.
_GLUE: dict[str, frozenset[str]] = {
    "es": frozenset({"y"}),
    "de": frozenset({"und"}),
    "it": frozenset(),
    "pt": frozenset({"e"}),
    "nl": frozenset({"en"}),
    "fr": frozenset({"et"}),
}

_ACCENTS = str.maketrans(
    "àâäáãéèêëíìîïóòôöõúùûüçñÀÂÄÁÃÉÈÊËÍÌÎÏÓÒÔÖÕÚÙÛÜÇÑ",
    "aaaaaeeeeiiiiooooouuuucnAAAAAEEEEIIIIOOOOOUUUUCN",
)


def fold(text: str) -> str:
    """Lower-case and strip the accents a transcript may or may not carry."""
    return (text or "").lower().translate(_ACCENTS).replace("ß", "ss")


_WORD_VALUES: dict[str, dict[str, int]] = {}


def word_values(language: str) -> dict[str, int]:
    """Every number word in a language, mapped to what it means.

    Built once per language, by spelling out every value worth having a word
    for and reading the table backwards. Where two values share a spelling the
    smaller one wins, which is what makes "mil" a scale word rather than 1000
    in "mil trece".
    """
    table = _WORD_VALUES.get(language)
    if table is not None:
        return table

    table = {}
    speller = _SPELLERS.get(language)
    if speller is not None:
        for value in _worth_spelling():
            word = fold(speller(value))
            if " " not in word and word not in table:
                table[word] = value
    for word, value in _SCALE.get(language, {}).items():
        table.setdefault(fold(word), value)
    _WORD_VALUES[language] = table
    return table


def parse_number(text: str, language: str) -> int | None:
    """Read a whole number written in words, in any supported language.

    Handles a phrase ("mil trece", "cento e vinte e um"), a single compound
    word ("einhundertsiebenundzwanzig"), digits already transcribed as figures,
    and any mixture of the three.
    """
    values = word_values(language)
    # A scale word multiplies what came before it; a compound word that merely
    # happens to be large ("milletredici") carries its whole value. Telling
    # them apart by table membership rather than by size is what keeps
    # "eintausenddreizehn" from being read as one thousand.
    scale = {fold(w): v for w, v in _SCALE.get(language, {}).items()}
    glue = _GLUE.get(language, frozenset())
    tokens = [t for t in re.split(r"[\s-]+", fold(text).strip()) if t]
    tokens = [t for t in tokens if t not in glue]
    if not tokens:
        return None

    total = 0
    current = 0
    seen = False
    for token in tokens:
        if token.isdigit():
            current += int(token)
            seen = True
            continue
        if token in scale:
            seen = True
            if scale[token] == 100:
                current = (current or 1) * 100
            else:
                total += (current or 1) * 1000
                current = 0
            continue
        value = values.get(token)
        if value is None:
            # An unknown word ends the number rather than being skipped over,
            # so a facility name after a value cannot contaminate it.
            break
        seen = True
        if value >= 1000:
            total += value
            current = 0
        else:
            current += value
    return (total + current) if seen else None


__all__ = ["digits", "cardinal", "parse_number", "word_values", "fold",
           "DIGITS", "DECIMAL_WORD"]
