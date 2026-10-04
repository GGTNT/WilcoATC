"""How a transmission is written down, which is not how it is said.

A controller says "one two seven decimal seven five" and a pilot writes 127.75.
Both are correct and they are not the same string. Everything else in this
package produces the spoken form, because that is what has to be synthesised
and what a pilot reads back; this turns it back into figures for the
transcript, which is what a pilot writes on a strip.

Every number, not only some
---------------------------

This began as a frequency and a squawk code, on the reasoning that an altitude
rendered as 12000 could not be checked against a readback that said "one two
thousand". That reasoning does not survive contact with the screen: both sides
of the exchange go through here, so both are written the same way and still
match each other -- and a strip full of "two four zero" where every real one
says 240 reads like a transcript of a conversation rather than a record of a
flight. So all of it is converted, and ``figures=False`` puts the words back
for anybody who wants them.

The conversion is safe because it is the inverse of a function in this same
package: everything :mod:`wilcoatc.atc.speech` produces, it produces
digit-by-digit through one function, so the shapes to recognise are known
rather than guessed. Nothing is converted unless it matches one of them, and
anything unrecognised is left alone.

What is deliberately left alone is a number said as a *word* rather than as
digits. "Delta twelve thirty four" is a callsign and not the number 1234, and
the two are told apart by how they are said: a controller says a quantity one
digit at a time and says a callsign in groups. That is the whole of the rule.
"""

from __future__ import annotations

import re

#: What a quantity is announced by, and how many digits it can have. Every
#: one of these is a shape :mod:`wilcoatc.atc.speech` produces, and the length
#: is what the function that produces it emits: a heading is three digits, a
#: runway is two, a squawk is four.
#:
#: The lengths are the point. Without them a run of digits swallows the next
#: one along, and "flight level three five zero, one zero minutes after
#: departure" comes out as 35010.
_ANNOUNCED_BY: dict[str, int] = {
    "runway": 2, "piste": 2, "rwy": 2,
    "heading": 3, "cap": 3,
    "level": 3, "niveau": 3, "vol": 3, "fl": 3,
    "speed": 3, "vitesse": 3,
    "wind": 3, "vent": 3,
    "squawk": 4, "transpondeur": 4,
    "qnh": 4, "altimeter": 4, "qfe": 4,
    "at": 2, "gusting": 2, "rafales": 2,
    "number": 1, "numéro": 1,
}

#: What a quantity is followed by. A number with one of these after it is a
#: number however it got there, which is what catches the second half of
#: "flight level three five zero, one zero minutes".
_MEASURED_IN: frozenset[str] = frozenset({
    "knots", "knot", "feet", "foot", "miles", "mile", "degrees", "degree",
    "minutes", "minute", "thousand", "hundred",
    "left", "right", "center", "centre",
    "nœuds", "noeuds", "pieds", "milles", "minutes", "degrés",
    "gauche", "droite",
})

#: The words that multiply a figure. English only, and deliberately: French
#: says its quantities as cardinals -- "mille six cent soixante-six" is a
#: callsign, and reading its "six cent" as six hundred is how a flight number
#: becomes an altitude.
_THOUSAND: frozenset[str] = frozenset({"thousand"})
_HUNDRED: frozenset[str] = frozenset({"hundred"})

# What each spoken digit can look like. The ICAO forms ("tree", "fower",
# "fife") appear when strict_icao_digits is on, and a recogniser hearing a
# pilot will produce the ordinary ones, so both are read here.
_SPOKEN_DIGIT: dict[str, str] = {
    "zero": "0", "oh": "0", "o": "0",
    "one": "1",
    "two": "2",
    "three": "3", "tree": "3",
    "four": "4", "fower": "4",
    "five": "5", "fife": "5",
    "six": "6",
    "seven": "7",
    "eight": "8", "ait": "8",
    "nine": "9", "niner": "9",
    # French
    "zéro": "0", "un": "1", "une": "1", "deux": "2", "trois": "3",
    "quatre": "4", "cinq": "5", "sept": "7", "huit": "8", "neuf": "9",
}

# The word between the megahertz and the decimals. US practice says "point",
# ICAO Annex 10 says "decimal", France says "décimale".
_DECIMAL_WORD = {"point", "decimal", "décimale", "decimale"}

# What introduces a transponder code.
_SQUAWK_WORD = {"squawk", "squawking", "affichez", "affiche"}

# A word that is part of a French cardinal, for the megahertz half of a French
# frequency: "cent vingt et un décimale sept".
_CARDINAL_WORD = {
    "zéro", "un", "une", "deux", "trois", "quatre", "cinq", "six", "sept",
    "huit", "neuf", "dix", "onze", "douze", "treize", "quatorze", "quinze",
    "seize", "vingt", "vingts", "trente", "quarante", "cinquante", "soixante",
    "cent", "cents", "mille", "et",
}

_TOKEN = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*|\d+|\s+|.", re.UNICODE)


def _split(text: str) -> list[str]:
    return _TOKEN.findall(text or "")


def _is_word(token: str) -> bool:
    return bool(token.strip()) and not token.isspace() and token[0].isalnum()


def _digit(token: str) -> str | None:
    return _SPOKEN_DIGIT.get(token.lower())


def _digit_run(tokens: list[str], start: int) -> tuple[str, int]:
    """Read as many consecutive spoken digits as there are from ``start``."""
    figures, index = [], start
    while index < len(tokens):
        token = tokens[index]
        if token.isspace():
            index += 1
            continue
        value = _digit(token)
        if value is None:
            break
        figures.append(value)
        index += 1
    # Give back any trailing whitespace that was consumed but not used.
    while index > start and tokens[index - 1].isspace():
        index -= 1
    return "".join(figures), index


def _is_cardinal(token: str) -> bool:
    """Whether a token is part of a French number.

    French writes its compounds with hyphens -- "trente-quatre",
    "quatre-vingt-dix" -- so a token can be several number words at once.
    """
    parts = token.lower().replace("’", "'").split("-")
    return bool(parts) and all(part in _CARDINAL_WORD for part in parts)


def _cardinal_run(tokens: list[str], start: int) -> tuple[int | None, int]:
    """Read a French cardinal, which is how the megahertz half is said."""
    from .speech_fr import words_to_number

    words, index = [], start
    while index < len(tokens):
        token = tokens[index]
        if token.isspace():
            index += 1
            continue
        if not _is_cardinal(token):
            break
        words.append(token)
        index += 1
    while index > start and tokens[index - 1].isspace():
        index -= 1
    if not words:
        return None, start
    return words_to_number(" ".join(words)), index


def _skip_space(tokens: list[str], index: int) -> int:
    while index < len(tokens) and tokens[index].isspace():
        index += 1
    return index


def _frequency(whole: str, frac: str) -> str:
    """Put a spoken frequency back into figures, three decimals as published."""
    decimals = (frac + "000")[:3]
    return f"{whole}.{decimals}"


def _digit_positions(tokens: list[str], start: int) -> list[tuple[int, str]]:
    """Every spoken digit from ``start``, with the token each one came from.

    The positions are what let a run be split: a heading takes the first three
    of them and whatever follows is a different number.
    """
    found: list[tuple[int, str]] = []
    index = start
    while index < len(tokens):
        token = tokens[index]
        if token.isspace():
            index += 1
            continue
        value = _digit(token)
        if value is None:
            break
        found.append((index, value))
        index += 1
    return found


def _announcer(tokens: list[str], before: int) -> int | None:
    """How many digits the word in front of a run says are coming, if any."""
    index = before - 1
    while index >= 0 and (tokens[index].isspace() or not _is_word(tokens[index])):
        index -= 1
    if index < 0:
        return None
    return _ANNOUNCED_BY.get(tokens[index].lower())


def _followed_by_unit(tokens: list[str], end: int) -> bool:
    """Whether what comes next says a quantity has just been said."""
    after = _skip_space(tokens, end)
    return (after < len(tokens) and _is_word(tokens[after])
            and tokens[after].lower() in _MEASURED_IN)


def _announced_cardinal(tokens: list[str], index: int) -> tuple[str, int] | None:
    """A quantity French says as a word rather than as digits.

    "Piste vingt-six", "QNH mille treize": the same numbers the FAA says one
    digit at a time. Only ever read where something announced a number is
    coming, because the same words build a callsign -- "Air France mille deux
    cent trente-quatre" -- and nothing announced that.
    """
    value, end = _cardinal_run(tokens, index)
    if value is None or not 0 <= value <= 9999:
        return None
    return str(value), end


def _numerals(tokens: list[str]) -> list[str]:
    """Turn the numbers a controller announced into figures.

    Only where one was announced. A digit word is read as a digit when
    something says a number is coming -- "runway", "heading", "squawk" -- or
    when something after it says one has just been said: "four mile final",
    "one zero minutes". Everywhere else it is left as a word, and that is not
    a limitation but the whole distinction: "Delta twelve thirty four" ends in
    the word four and is not a number, "Iberia fifteen oh six" is a flight
    number said in groups, and "one o'clock" is a direction.
    """
    out: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not _is_word(token):
            out.append(token)
            index += 1
            continue

        # A quantity said as a word rather than as digits, which is what
        # French does for some of them. Read where a number was announced, or
        # where a unit follows -- "douze noeuds" is twelve knots wherever it
        # appears, and "mille deux cent trente-quatre" after nothing at all is
        # a flight number.
        if _is_cardinal(token):
            found = _announced_cardinal(tokens, index)
            if found is not None and (_announcer(tokens, index)
                                      or _followed_by_unit(tokens, found[1])):
                # Unless it is really digits: "trois cinq zéro" is a level
                # said one digit at a time and every word of it is also a
                # cardinal. Whichever reading covers more words is the one
                # that was meant.
                digits_here = _digit_positions(tokens, index)
                if len(digits_here) < 2 or found[1] > digits_here[-1][0] + 1:
                    out.append(found[0])
                    index = found[1]
                    continue

        if _digit(token) is None:
            out.append(token)
            index += 1
            continue

        run = _digit_positions(tokens, index)
        end = run[-1][0] + 1
        after = _skip_space(tokens, end)
        next_word = (tokens[after].lower()
                     if after < len(tokens) and _is_word(tokens[after]) else "")

        # "one two thousand", and then "five hundred" after it.
        if next_word in _THOUSAND:
            value = int("".join(d for _i, d in run)) * 1000
            index = after + 1
            more = _digit_positions(tokens, _skip_space(tokens, index))
            if more:
                past = _skip_space(tokens, more[-1][0] + 1)
                if (past < len(tokens) and _is_word(tokens[past])
                        and tokens[past].lower() in _HUNDRED):
                    value += int("".join(d for _i, d in more)) * 100
                    index = past + 1
            out.append(str(value))
            continue

        if next_word in _HUNDRED:
            out.append(str(int("".join(d for _i, d in run)) * 100))
            index = after + 1
            continue

        # Announced by the word in front of it: take as many digits as that
        # word says are coming, and leave the rest to be read on their own.
        length = _announcer(tokens, index)
        if length:
            taken = run[:length]
            out.append("".join(d for _i, d in taken))
            index = taken[-1][0] + 1
            continue

        # Or measured by the word after it, which makes the whole run one
        # number however it was introduced.
        if next_word in _MEASURED_IN:
            out.append("".join(d for _i, d in run))
            index = end
            continue

        # Nobody said a number was coming. It is a word.
        out.append(token)
        index += 1
    return out


def as_written(text: str, language: str = "en", figures: bool = True) -> str:
    """Rewrite the numbers in a transmission the way a pilot writes them.

    ``figures=False`` leaves everything as it was said, for anybody who would
    rather read the transcript in the words that were spoken.
    """
    if not text:
        return text

    tokens = _split(text)
    out: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]

        # --- a squawk code: the word, then four digits -------------------
        if _is_word(token) and token.lower() in _SQUAWK_WORD:
            after = _skip_space(tokens, index + 1)
            figures, end = _digit_run(tokens, after)
            if len(figures) == 4:
                out.append(token)
                out.extend(tokens[index + 1:after])
                out.append(figures)
                index = end
                continue

        # --- a frequency: digits, the decimal word, digits ---------------
        if _is_word(token) and _digit(token) is not None:
            whole, end = _digit_run(tokens, index)
            after = _skip_space(tokens, end)
            if (after < len(tokens) and _is_word(tokens[after])
                    and tokens[after].lower() in _DECIMAL_WORD):
                frac, frac_end = _digit_run(tokens, _skip_space(tokens, after + 1))
                if whole and frac:
                    out.append(_frequency(whole, frac))
                    index = frac_end
                    continue

        # --- a French frequency: a cardinal, then the decimal word -------
        if _is_word(token) and _is_cardinal(token):
            value, end = _cardinal_run(tokens, index)
            after = _skip_space(tokens, end)
            if (value is not None and 100 <= value <= 999
                    and after < len(tokens) and _is_word(tokens[after])
                    and tokens[after].lower() in _DECIMAL_WORD):
                frac, frac_end = _digit_run(tokens, _skip_space(tokens, after + 1))
                if frac:
                    out.append(_frequency(str(value), frac))
                    index = frac_end
                    continue

        out.append(token)
        index += 1

    if figures:
        out = _numerals(out)
    return "".join(out)


__all__ = ["as_written"]
