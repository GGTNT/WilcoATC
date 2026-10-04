"""French written so that an English front end reads it as French.

This exists for one engine and one failure. Supertonic-3 speaks French -- it
has a ``<fr>`` language token and it uses it -- but what comes back at
``lang="fr"`` is not French delivery. The vowels are right and the sentence is
flat: every clause lands on the same pitch, the phrase-final rise a French
speaker puts on "autorise a atterrir" is not there, and after two
transmissions it reads as a machine rather than as a controller. The same
model given the same words at ``lang="en"`` is lively -- and reads them as
English words, which is worse than flat.

So the words are respelled. The French is rewritten into the English spelling
that produces the nearest French sounds, and handed over as English. "trois
cent dix degres" goes in as "trwah sahn deess daygray", the English front end
reads exactly that, and the prosody stays the one the English path has.

**This is not phonemisation and it is not the accent layer.**
:mod:`wilcoatc.audio.accent` operates on phonemes, is far more faithful, and
cannot be used here: Supertonic takes text and has no raw-phoneme input at
all. What is below is the crude version that fits through a text interface,
and it is only ever applied to a language the model would otherwise read
badly. Nothing else in the program calls it.

Two layers, in the order the accent layer uses for the same reason:

*A lexicon first*, for the words a controller says ten thousand times. Rules
do not describe how somebody says a number -- "six" and "dix" sound their
final s, "huit" does not sound its h, "cinq" sounds its q -- and getting a
digit wrong is not an accent, it is a different clearance.

*Rules underneath*, because the rest is open: airport names, airline names,
fixes, and whatever the phrasebook composes. French spelling is regular in
the reading direction, which is the direction needed here, so a scanner over
an ordered table covers it.

The scanner writes into a separate buffer and never rescans what it has
written. Substituting in place instead -- replace "ou" with "oo", then later
replace "o" -- is how "oo" turns into "ohoh", and it is the reason this is a
single left-to-right pass rather than a list of ``str.replace`` calls.
"""

from __future__ import annotations

import re

#: Vowel letters, accents included. Used by the rules that need to know
#: whether the next letter is a vowel -- which in French is most of them,
#: because a nasal is only nasal when nothing follows it.
_VOWELS = "aeiouyàâäéèêëîïôöûüùœæ"

#: A nasal vowel is one that is *not* followed by another vowel or by a second
#: n/m. "vent" is nasal; "venez" is not; "comme" is not.
_NASAL = f"(?![{_VOWELS}nm])"

#: Consonants that stay silent at the end of a French word. The ones that do
#: sound are c, f, l, r, k, q and b -- the "careful" set every French course
#: teaches -- and they are simply absent from here.
_SILENT_FINAL = "stdxzpg"


# --------------------------------------------------------------------------
# the lexicon
# --------------------------------------------------------------------------

#: Words whose respelling is not worth deriving. Numbers make up most of it
#: because numbers make up most of the radio: an altitude, a heading, a
#: frequency, a squawk and a runway are all numbers, and the rules get four of
#: the first ten wrong. The rest are function words that are irregular in
#: French and common in every transmission.
#:
#: Keyed on the lowercased word with its accents intact.
LEXICON: dict[str, str] = {
    # --- digits and the teens ---
    "zéro": "zayro", "zero": "zayro",
    "un": "uhn", "une": "ewn",
    "deux": "duh",
    "trois": "trwah",
    "quatre": "katr",
    "cinq": "sank",          # the q sounds, which no rule here would guess
    "six": "seess",          # as does the x, when the word stands alone
    "sept": "set",           # the p does not
    "huit": "weet",          # nor does the h, and the ui is /ɥi/
    "neuf": "nuhf",
    "dix": "deess",
    "onze": "ohnz",
    "douze": "dooz",
    "treize": "trez",
    "quatorze": "katorz",
    "quinze": "kanz",
    "seize": "sez",
    # --- tens, hundreds, thousands ---
    "vingt": "van", "vingts": "van",
    "trente": "trahnt",
    "quarante": "karahnt",
    "cinquante": "sankahnt",
    "soixante": "swasahnt",
    "cent": "sahn", "cents": "sahn",
    "mille": "meel",
    "million": "meelyohn", "millions": "meelyohn",
    # --- function words the rules would read letter by letter ---
    "et": "ay",
    "est": "eh",
    "les": "lay", "des": "day", "mes": "may", "ces": "say", "ses": "say",
    "le": "luh", "la": "lah", "de": "duh", "du": "dew",
    "au": "oh", "aux": "oh",
    "à": "ah", "a": "ah",
    "en": "ahn", "on": "ohn",
    "vous": "voo", "nous": "noo",
    "monsieur": "muhsyuh", "madame": "madam",
    "oui": "wee",
    "plus": "plew",
    # --- words this radio says on every flight ---
    "vent": "vahn",          # a noun, so the -ent is nasal, not silent
    "piste": "peest",
    "noeuds": "nuh", "nœuds": "nuh",
    "pieds": "pyay",
    "degrés": "daygray", "degres": "daygray",
    "niveau": "neevoh",
    "atis": "atees",         # the final s is sounded on the radio
    "qnh": "kew enn ash",    # read as letters, and as French letter names
    "qfe": "kew eff uh",
    "ils": "eel", "il": "eel",
    "cap": "kahp",           # the p sounds here, unlike in "sept"
    # "est" is deliberately absent: as a compass point it is /ɛst/ and as the
    # verb it is /ɛ/, the verb is far the commoner, and it is already above.
    "nord": "nor", "sud": "sewd", "ouest": "west",
    "gaulle": "gohl",        # the rules would fold the ll into a y
    "orly": "orlee",
    "ville": "veel",         # one of the three words where "ille" is not /j/
}


# --------------------------------------------------------------------------
# the rules
# --------------------------------------------------------------------------

# Ordered, longest and most specific first, and read strictly left to right.
# Each entry is a pattern anchored at the current position and what to write
# instead. The first that matches wins and the scanner steps over what it
# consumed, so nothing produced here is ever looked at again.
#
# The English spellings are chosen for how an English front end reads them,
# not for how they look:
#
#   nasals    "ahn" /ɑ̃/, "an" /ɛ̃/, "ohn" /ɔ̃/, "uhn" /œ̃/
#   u         "ew", the closest English has to /y/
#   eu        "uh", for /ø/ and /œ/
#   ou        "oo"
#   oi        "wah"
#   é         "ay", è/ê "eh"
#
# The r is the one thing no respelling can carry: English has no letter that
# produces a uvular /ʁ/. It goes through as "r" and comes back American, which
# is the single largest thing separating this from the phoneme path.
_RULES: list[tuple[str, str]] = [
    # --- endings, before anything general can eat them ---
    (r"tion\b", "syohn"),
    (r"ssion\b", "syohn"),
    (r"eaux?\b", "oh"),
    (r"ier\b", "yay"),
    (r"iers\b", "yay"),
    (r"aient\b", "eh"),
    (r"ais\b", "eh"), (r"ait\b", "eh"), (r"ai\b", "ay"),
    (r"ez\b", "ay"), (r"er\b", "ay"), (r"ers\b", "ay"),
    (r"ée?s?\b", "ay"),

    # --- nasal vowels. Order matters: the three-letter ones first, or "oin"
    # --- would be read as "o" plus a nasal "in".
    (rf"oin{_NASAL}", "wan"),
    (rf"ien{_NASAL}", "yan"),
    (rf"éen{_NASAL}", "ayan"),
    (rf"(?:ain|aim|ein|eim){_NASAL}", "an"),
    (rf"(?:in|im|yn|ym|ïn){_NASAL}", "an"),
    (rf"(?:un|um){_NASAL}", "uhn"),
    (rf"(?:ean|an|am|en|em){_NASAL}", "ahn"),
    (rf"(?:on|om){_NASAL}", "ohn"),

    # --- the l that is not an l. French spells /j/ with "ill", and after
    # --- a, e or eu the vowel in front of it changes too, so these come as
    # --- whole groups before anything can take the vowel on its own.
    (r"(?:euill|euil\b)", "uhy"),
    (r"(?:aill|ail\b)", "ahy"),
    (r"(?:eill|eil\b)", "ey"),

    # --- vowel groups ---
    (r"eau", "oh"),
    (r"au", "oh"),
    (r"ou[îï]", "wee"),
    (r"o[uû]", "oo"),
    (r"o[iî]", "wah"),
    (r"oy", "wahy"),
    (r"(?:œu|oeu|eu|eû)", "uh"),
    (r"a[iî]", "eh"),
    (r"ei", "eh"),
    (r"ui", "wee"),

    # --- consonants and digraphs ---
    (r"ch", "sh"),
    (r"gn", "ny"),
    (r"ph", "f"),
    (r"th", "t"),
    (r"qu", "k"),
    (r"q", "k"),
    (r"ç", "s"),
    (rf"c(?=[eiyéèê])", "s"),
    (r"cc(?=[eiy])", "ks"),
    (r"c", "k"),
    (rf"g(?=[eiyéèê])", "zh"),
    (r"g", "g"),
    (r"j", "zh"),
    (r"h", ""),
    # "ill" is /j/ after a vowel ("travailler") and /il/ at the start of a
    # word ("ville" being the famous exception nobody says on a radio).
    (rf"(?<=[{_VOWELS}])ill", "y"),
    (r"ill", "eey"),
    (rf"(?<=[{_VOWELS}])il\b", "y"),
    (r"ss", "s"),
    (rf"(?<=[{_VOWELS}])s(?=[{_VOWELS}])", "z"),
    (r"x", "ks"),
    (r"w", "v"),
    (r"y(?=[aeiou])", "y"),
    (r"y", "ee"),

    # --- single vowels ---
    (r"é", "ay"),
    (r"[èêë]", "eh"),
    (r"[àâä]", "ah"),
    (r"a", "ah"),
    (r"[îï]", "ee"),
    (r"i", "ee"),
    (r"[ôö]", "oh"),
    (r"o", "oh"),
    (r"[ûùü]", "ew"),
    (r"u", "ew"),
    # A word-final e is mute, and so is a final "es". Everything else is the
    # schwa, which English spells with a bare u often enough to be read right.
    # A word-final e is mute; so is the plural "es" it takes, which is
    # why "pistes" is one syllable shorter than it looks.
    (r"es\b", ""),
    (r"e\b", ""),
    # An e in front of a doubled consonant is open -- "atterrir" is /atɛʁiʁ/,
    # not /atəʁiʁ/ -- and this is the commonest word on a French approach
    # frequency, so it is worth the one rule.
    (r"e(?=[bcdfglmnprstz]{2})", "eh"),
    (r"e", "uh"),
]

_COMPILED: list[tuple[re.Pattern[str], str]] = [
    (re.compile(pattern), replacement) for pattern, replacement in _RULES
]

#: A run of letters, accents included. Everything between runs -- spaces,
#: commas, full stops -- is copied through untouched, because the commas are
#: what the phrasebook uses to place the pauses and dropping them would run a
#: whole clearance together.
#:
#: The hyphen and the apostrophe are separators rather than letters, which is
#: what lets the lexicon see "quatre" inside "trente-quatre" and "attente"
#: inside "d'attente". Both are how French writes two words, not one.
_WORD = re.compile(rf"[a-zA-Z{_VOWELS}çÇ]+")

#: An acronym the phrasebook emits as letters rather than as a word. Read as
#: English letter names, which is wrong, but is closer than the rules -- which
#: would turn "VOR" into "vohr".
_ACRONYM = re.compile(r"^[A-ZÀ-Þ]{2,}$")


def _scan(word: str) -> str:
    """One word, through the rules, left to right."""
    out: list[str] = []
    at = 0
    while at < len(word):
        for pattern, replacement in _COMPILED:
            found = pattern.match(word, at)
            if found is not None and found.end() > at:
                out.append(replacement)
                at = found.end()
                break
        else:
            # A letter no rule claims -- k, b, f, l, m, n, r, s, t, v, and the
            # apostrophe. It is already the letter an English reader would
            # sound, so it goes through as itself.
            out.append(word[at])
            at += 1

    spelled = "".join(out)

    # The final consonant, once the rules have had the word. Done here rather
    # than as a rule because it depends on the whole word: dropping the t of
    # "vingt" is right, and dropping the t of "et" would leave nothing at all.
    #
    # A mute e is what makes the consonant in front of it sound, and the rules
    # have already deleted it by this point -- so the source word is asked
    # rather than the spelling. Without this, "France" comes out as "frahn"
    # and "droite" as "drwah", which are two other French words.
    voiced_by_mute_e = word.endswith(("e", "es"))
    if not voiced_by_mute_e and len(spelled) > 2 and spelled[-1] in _SILENT_FINAL:
        spelled = spelled[:-1]
    return spelled or word


def word(text: str) -> str:
    """One French word as English spelling. The lexicon first, then the rules."""
    lowered = text.lower()
    known = LEXICON.get(lowered)
    if known is not None:
        return known
    return _scan(lowered)


def to_english(text: str, language: str = "fr") -> str:
    """Respell ``text`` so an English front end reads it in ``language``.

    French only, for now. Any other code returns the text unchanged rather
    than raising: a caller that asks for a language with no table wants the
    words as they were, not an exception mid-transmission.
    """
    if (language or "")[:2] != "fr" or not text:
        return text

    def one(match: re.Match[str]) -> str:
        found = match.group(0)
        if _ACRONYM.match(found) and found.lower() not in LEXICON:
            # Left alone deliberately. See _ACRONYM.
            return found
        return word(found)

    return _WORD.sub(one, text)


__all__ = ["to_english", "word", "LEXICON"]
