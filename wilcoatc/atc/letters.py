# -*- coding: utf-8 -*-
"""A word heard where a spelled letter belongs is read as that letter.

The recogniser writes what a letter sounds like when it does not expect a
letter: at Kortrijk "Oscar, Oscar, Alpha, Bravo, Shirley" lost the pilot's
callsign, and a French pilot's "Juliett India Mike" came back as "Julia
India Mike", "Juliette, Indian Mike", "Jugaid India Mike". The parser reads
the alphabet only as it is spelled, so one letter heard as a name loses the
whole callsign.

Matching every word against the alphabet by similarity was tried on the
transcripts in logs/radio.jsonl and is wrong more often than right:
"taxiing" is as close to Tango as "Julia" is to Juliett, and "number" beside
an X-ray is nearer November than most mishearings. So a word is read as a
letter only when it is one the recogniser is known to write for that letter
(``HEARD_AS``, from the logs and from accented speech run through the
recogniser), and only where a letter is expected: next to another letter
(a callsign, a taxiway's name, a reporting point spelled out), after "via",
"taxiway" or "holding point", or in front of a figure ("Motel two").
"""

from __future__ import annotations

import re

from .speech import _ALPHABET, _ALPHABET_REVERSE

#: What the recogniser writes for a letter it did not expect. Each entry is
#: a mishearing seen in logs/radio.jsonl or in the vocabulary bench
#: (scripts/stt_vocabulary_bench.py), or one of the pilot's own list.
HEARD_AS: dict[str, str] = {
    **{w: "C" for w in ("shirley", "charley", "charly", "chari", "charry",
                        "charny", "sharley", "sharlie", "charli")},
    **{w: "D" for w in ("delter", "delda", "deltar")},
    **{w: "O" for w in ("oskar", "oskur")},
    **{w: "H" for w in ("motel", "hotell", "otel")},
    **{w: "P" for w in ("pepper", "poppa", "pappa", "papah")},
    **{w: "I" for w in ("indian", "indy", "indie", "indya")},
    **{w: "J" for w in ("julia", "julie", "julien", "juliete", "jugaid")},
    **{w: "F" for w in ("fox", "foxtrott", "foxtrod")},
    **{w: "X" for w in ("x-ary", "ex-ray", "exray", "x-rays")},
    **{w: "M" for w in ("mic", "mick", "maik")},
    **{w: "W" for w in ("whisky",)},
    **{w: "V" for w in ("viktor",)},
    **{w: "G" for w in ("gulf",)},
    **{w: "E" for w in ("eko", "ecco")},
    **{w: "K" for w in ("keelo", "kilos")},
    **{w: "L" for w in ("leema",)},
    **{w: "Q" for w in ("kebec", "kebeck")},
    **{w: "R" for w in ("romio",)},
    **{w: "S" for w in ("siera", "ciara")},
    **{w: "Y" for w in ("yanky", "yankie")},
    **{w: "Z" for w in ("zoulou", "zoulu")},
}

#: After these a letter is expected even with no other letter beside it.
_BEFORE_A_LETTER = frozenset({"via", "taxiway", "taxiways", "intersection",
                              "point"})

_FIGURES = frozenset({"zero", "one", "two", "three", "tree", "four", "fower",
                      "five", "fife", "six", "seven", "eight", "nine",
                      "niner"})

_TOKEN = re.compile(r"[A-Za-z][A-Za-z'-]*|\d+")


def _is_letter(word: str) -> bool:
    return word.lower() in _ALPHABET_REVERSE


def _spelled(word: str, letter: str) -> str:
    out = _ALPHABET[letter]
    return out.lower() if word.islower() else out


def snap(text: str) -> tuple[str, list[tuple[str, str]]]:
    """``text`` with every misheard letter in a letter's place read as the
    letter, and the (heard, read) pairs that were changed."""
    found = [(m.start(), m.end(), m.group(0)) for m in _TOKEN.finditer(text)]
    words = [w for _, _, w in found]
    letters = [_is_letter(w) for w in words]
    changes: dict[int, str] = {}
    # Twice: a letter read on the first pass ("Julia") makes its neighbour
    # ("Indian") a word beside a letter on the second.
    for _ in range(2):
        for i, word in enumerate(words):
            if letters[i]:
                continue
            letter = HEARD_AS.get(word.lower())
            if letter is None:
                continue
            before = words[i - 1].lower() if i else ""
            after = words[i + 1].lower() if i + 1 < len(words) else ""
            if ((i and letters[i - 1]) or (i + 1 < len(words) and letters[i + 1])
                    or before in _BEFORE_A_LETTER
                    or after.isdigit() or after in _FIGURES):
                changes[i] = _spelled(word, letter)
                letters[i] = True
    if not changes:
        return text, []
    out, last = [], 0
    for i, (start, end, word) in enumerate(found):
        if i in changes:
            out += [text[last:start], changes[i]]
            last = end
    out.append(text[last:])
    return "".join(out), [(words[i], changes[i]) for i in sorted(changes)]


__all__ = ["HEARD_AS", "snap"]
