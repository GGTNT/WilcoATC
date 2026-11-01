"""Which language a written transmission is in.

Speech arrives with a language already attached: the recogniser decides it from
the sound. Typed text arrives with nothing, and the panel says as much -- "which
language a transmission is in is detected from the words" -- so something has to
do the detecting. Without it, typing "essai radio" at Orly was answered with
"say again" in English, because the text was handed to the English grammar and
the English grammar has never heard of it.

The signal used is the one that matters here: whether the phrase is phraseology
in that language. A pilot typing on an aviation frequency is not writing prose,
and "which grammar understands this" is a far better question than "which
language do these letters look like". Only the languages actually worked at the
facility are considered, so a French phrase at Frankfurt is not answered in
French.

Where the grammars disagree or both fall silent, the words decide: each
language's vocabulary is drawn from its own rules, so a word only counts for a
language if that language's controller could have said it. English is the
default and has to be beaten rather than merely tied, because English is
available on every frequency in the world and the local language is not.
"""

from __future__ import annotations

import re
from functools import lru_cache

from .intents import Intent, IntentParser

# Words too common, or too shared between languages, to say anything about
# which one is being spoken. "Roger" and "radio" are the same in most of them,
# and a callsign or a number is no evidence at all.
_SHARED = frozenset({
    "radio", "roger", "atis", "qnh", "squawk", "ils", "vfr", "ifr", "vor",
    "taxi", "info", "papa", "victor", "sierra", "hotel", "mike", "alpha",
    "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "india", "juliett",
    "kilo", "lima", "november", "oscar", "quebec", "romeo", "tango",
    "uniform", "whiskey", "xray", "yankee", "zulu",
})

# English words the other grammars accept too, because their pilots say them:
# "Stuttgart Tower" as often as "Stuttgart Turm", "Radiocheck" as often as
# "Funkprobe". As evidence they are English: at Heubach "Heubach Tower, ...,
# request radio check" scored one German word ("tower") against one English.
_BORROWED_ENGLISH = frozenset({
    "tower", "ground", "approach", "delivery", "departure", "apron", "check",
})

_WORD = re.compile(r"[^\W\d_]{3,}", re.UNICODE)


@lru_cache(maxsize=1)
def _parser() -> IntentParser:
    return IntentParser()


@lru_cache(maxsize=8)
def _words(language: str) -> frozenset[str]:
    """The words that belong to one language's phraseology.

    Taken from the grammar rules themselves, so this cannot drift away from
    what the parser actually understands.
    """
    from .vocabulary import VOCABULARY, _rule_words

    found = set(_rule_words(language)) | set(VOCABULARY.get(language, ()))
    if language == "en":
        # The English rules are written separately from the rest.
        from .intents import _RULES

        for _, pattern in _RULES:
            clean = re.sub(r"[()\[\]{}|?*+^$.\\]", " ", pattern)
            found |= {w.lower() for w in _WORD.findall(clean)}
        found |= _BORROWED_ENGLISH
    else:
        found -= _BORROWED_ENGLISH
    return frozenset(found - _SHARED)


def _understands(text: str, language: str) -> bool:
    """Whether this language's grammar makes sense of the phrase."""
    try:
        parsed = _parser().parse(text, language=language)
    except Exception:                              # pragma: no cover
        return False
    return parsed is not None and parsed.intent is not Intent.UNKNOWN


def _score(text: str, language: str) -> int:
    """How many of the phrase's words belong to this language."""
    vocabulary = _words(language)
    return sum(1 for word in _WORD.findall(text.lower())
               if word in vocabulary)


def language_of_text(text: str, candidates) -> str:
    """The language a typed transmission is in.

    ``candidates`` is what may be spoken on this frequency, English first.
    Returns one of them, always: an undecidable phrase is English, because
    English is worked everywhere and guessing the local language wrongly is the
    more surprising of the two mistakes.
    """
    options = [str(c).lower()[:2] for c in candidates if c]
    if not options:
        return "en"
    if len(options) == 1 or not (text or "").strip():
        return options[0]

    # Which grammars make sense of it. One is an answer.
    understood = [lang for lang in options if _understands(text, lang)]
    if len(understood) == 1:
        return understood[0]

    # More than one, or none: let the words decide. A language has to be
    # strictly ahead of English to displace it.
    scores = {lang: _score(text, lang) for lang in options}
    ranked = sorted(options, key=lambda lang: -scores[lang])
    best = ranked[0]
    default = "en" if "en" in options else options[0]
    if scores[best] > scores.get(default, 0):
        return best

    # Still tied. If the grammars agreed on a shortlist, prefer the default
    # inside it; otherwise the default.
    if understood and default not in understood:
        return understood[0]
    return default


def by_words(text: str, candidates) -> str | None:
    """The one language whose phraseology is in the transmission, or None
    when the words do not say: none of them, or as many of one as another.

    For a first call, where nothing else is known yet, this beats the
    recogniser's tag. At Heubach "Heubach Tower, DEHUH, request radio check"
    was tagged German -- the field's German name pulls the guess -- and three
    of its words are English phraseology and none German.
    """
    options = list(dict.fromkeys(str(c).lower()[:2] for c in candidates if c))
    scores = sorted(((_score(text, lang), lang) for lang in options),
                    reverse=True)
    if not scores or scores[0][0] == 0:
        return None
    tied = [lang for score, lang in scores if score == scores[0][0]]
    if len(tied) == 1:
        return tied[0]
    # A tie the grammars settle: told the station's name, the recogniser
    # writes a spoken "Heubach Turm" as "Heubach Tower", and "Tower" then
    # weighs as much as the "Funkprobe" that only German understands.
    understood = [lang for lang in tied if _understands(text, lang)]
    return understood[0] if len(understood) == 1 else None


def clearly_in(text: str, language: str, than: str,
               words: int = 2) -> bool:
    """Whether a transmission is in ``language`` over several words, rather
    than one word of it on a frequency worked in ``than``: its grammar makes
    sense of it, ``than``'s does not, and more of its phraseology is in it.
    Callsigns and numbers count for neither, which is why two words are
    several ("Delta douze trente-quatre, demande la descente").

    The recogniser's language tag is a guess from the sound and is wrong on
    short English calls with a local accent: at Kortrijk an English taxi call
    was tagged French. A frequency changes language on the words, never on
    the tag.
    """
    if language == than:
        return True
    ours, theirs = _score(text, language), _score(text, than)
    # ``than`` understanding it counts only on words of its own: German
    # takes "radio check" as German pilots say it, and at Heubach that kept
    # an English radio check on a German frequency in German.
    return (ours >= words and ours > theirs and _understands(text, language)
            and (theirs == 0 or not _understands(text, than)))


__all__ = ["by_words", "clearly_in", "language_of_text"]
