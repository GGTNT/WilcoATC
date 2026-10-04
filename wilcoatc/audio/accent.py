"""Foreign-accented speech, done by phonemes rather than by spelling.

A controller at Orly speaks French to Air France and English to Speedbird, and
their English carries their French. Getting that second half right is the whole
point of this module, and the obvious way to do it is wrong.

The obvious way is to hand English text to a French voice and let its own
letter-to-sound rules deal with it. That is what this project did, and the
result was not a French accent -- it was a French *reader*. espeak-ng's French
front end recognises some English words and guesses at the rest, so a single
transmission came out half English and half French:

    knots     -> /kno/      the t silenced, as in the French "knout"
    descend   -> /dɛsɑ̃/     read as the French verb
    maintain  -> /mɛ̃tɛ̃/     read as "maintien"
    Paris     -> /paʁi/     the s silenced
    November  -> /novɑ̃be/   read as "novembre"

Those are not accented English. They are different words, and a pilot cannot
recover the instruction from them. The inconsistency is the giveaway: "runway"
came out in clean English in the same breath as "knots" came out as "kno".

What a French speaker actually does is pronounce the *English* word with the
sounds their own language gives them. So that is what happens here:

    1. the English text is phonemised by espeak-ng's **English** front end, so
       the words keep their English shape;
    2. the words a controller says most often are looked up in a lexicon,
       because rules do not describe how somebody says a word they have said
       ten thousand times, and because "Bravo" and "Papa" are French words a
       French mouth already owns;
    3. every phoneme English has and French does not is replaced by the nearest
       French one -- /θ/ becomes /s/, /h/ disappears, /ɹ/ becomes uvular /ʁ/,
       /ɪ/ merges into /i/, /eɪ/ flattens to /e/;
    4. the stress a word already carries is moved to its last syllable, because
       that is where French puts it. It is moved, never added: espeak's own
       French leaves half its words unstressed, and marking every one of them
       comes out as a chant;
    5. the result is handed to the French voice as raw phonemes.

Step 3 needs no accent-specific cleverness. Give it the list of phonemes the
target voice was actually trained on and the substitutions fall out on their
own: a French model never saw /θ/, so /θ/ has to go somewhere, and the table
below only has to say where. That is also why this works in every direction --
French text through an English voice folds the other way with the same code.

The inventories are what espeak-ng emits for that language, which is by
construction what each Piper model saw in training. Feeding a model a phoneme
outside its inventory does not produce an accent; it produces a glitch.

Handing Piper phonemes rather than text is free: measured against the
recogniser, the same French sentence scores the same either way. What is not
free is asking a model for sequences it has never seen, which is what the
lexicon exists to avoid.
"""

from __future__ import annotations

import logging
import unicodedata

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# phoneme inventories
# --------------------------------------------------------------------------
#
# The phonemes espeak-ng produces for each language, and therefore the ones the
# Piper model for that language was trained on. Anything outside this set is a
# sound the voice has never made and cannot be asked to make.
#
# "en" covers both of espeak's English front ends, because both are used: the
# American one for a foreign speaker's English and the British one for a
# British controller's own. The only sound the second has that the first does
# not is the LOT vowel, and it is not optional -- without it declared, every
# British transmission lost the vowel out of "one", "contact" and "takeoff",
# which came out as /wˈn/, /kˈntakt/ and /tˈeɪkf/.

INVENTORY: dict[str, frozenset[str]] = {
    "en": frozenset("abdefhijklmnoprstuvwzæðŋɐɑɒɔəɚɛɜɡɪɫɹɾʃʊʌʒʔθᵻ"),
    "fr": frozenset("abdefijklmnopstuvwyzøœɑɔəɛɡɲʁʃʒ"),
    # These are the characters espeak emits, not an alphabet of phonemes, and
    # the two are not quite the same thing. German's ich-Laut comes back as a
    # bare "c" with a combining cedilla after it rather than as the single
    # character ç, so both have to be declared -- with only ç here, "München"
    # and "nicht" lost the consonant out of the middle on every German
    # transmission.
    "de": frozenset("abcdefhijklmnoprstuvxyzøŋœɐɑɔəɛɜɡɪɾʃʊʏçʒ̧"),
    # ʝ is the y of "ya" and the ll of "llegada", which is most of the
    # Spanish that a controller says.
    "es": frozenset("abdefijklmnoprstuwxðɛɡɣɲɾʃʎʝʲβθ"),
    "it": frozenset("abdefijklmnoprstuvwzɔɛɡɲɾʃʎʒ"),
    "nl": frozenset("abdefhijklmnoprstuvwxyzøŋœɑɔəɛɣɪɲɵʃʋʒ"),
    "pt": frozenset("abdefijklmnopstuvwxzɐɔɛɡɪɲɾʃʊʎʒ"),
}

# Vowels take a length mark in the languages that contrast length. French,
# Spanish, Italian and Portuguese do not, so the mark is dropped there and the
# vowel comes out short, which is itself an audible part of the accent.
KEEPS_LENGTH: frozenset[str] = frozenset({"en", "de", "nl"})


# --------------------------------------------------------------------------
# tokenising espeak output
# --------------------------------------------------------------------------

# espeak returns single characters. Diphthongs and affricates have to be put
# back together before they can be substituted, because a French speaker
# replaces /eɪ/ with a single /e/ -- not with /e/ followed by something.
_DIGRAPHS: frozenset[str] = frozenset({
    "eɪ", "aɪ", "aʊ", "ɔɪ", "oʊ", "əʊ", "ɪə", "eə", "ɛə", "ʊə", "ʊɹ", "ɔə",
    "tʃ", "dʒ", "ts", "dz", "pf", "aɛ", "ɔø",
})

# A vowel with an r after it, which American English spells as one sound and
# these languages do not. Two of them have to be held together for a different
# reason each. "departure" is written with an r-coloured vowel *and* an r, and
# taken apart both halves turn into an r, so the word ends up with two. "radar"
# and "knots" share a vowel in American English and not in any language here:
# the r is what tells them apart, so the vowel cannot be substituted until the
# r has been looked at.
_R_COLOURED: frozenset[str] = frozenset({"ɑɹ", "ɚɹ", "ɝɹ"})

_STRESS: frozenset[str] = frozenset({"ˈ", "ˌ"})
_LENGTH = "ː"
# Marks that belong to the phoneme in front of them.
_TRAILING: frozenset[str] = frozenset({"ː", "ˑ", "ʲ", "ʰ", "ʷ", "˞", "̩", "̯", "̃",
                                       "̪", "̺", "̻", "̧", "̝", "̊", "↑", "↓"})


class Token:
    """One symbol out of espeak: a phoneme, a stress mark, or punctuation."""

    __slots__ = ("text", "kind", "fixed")

    def __init__(self, text: str, kind: str, fixed: bool = False):
        self.text = text
        self.kind = kind      # "phoneme" | "stress" | "space" | "punct"
        # Set on phonemes that came from the lexicon. They are already the
        # realisation the controller uses, so the substitution rules leave
        # them alone rather than applying a second time to their own output.
        self.fixed = fixed

    def __repr__(self) -> str:          # pragma: no cover - debugging aid
        return f"Token({self.text!r}, {self.kind})"


def tokenize(phonemes: list[str] | str) -> list[Token]:
    """Group espeak's character stream into phonemes, stresses and breaks."""
    chars = list(phonemes)
    out: list[Token] = []
    i = 0
    while i < len(chars):
        ch = chars[i]
        if ch in _STRESS:
            out.append(Token(ch, "stress"))
            i += 1
            continue
        if ch.isspace():
            out.append(Token(" ", "space"))
            i += 1
            continue
        if ch in ".,;:!?()\"'-":
            out.append(Token(ch, "punct"))
            i += 1
            continue

        unit = ch
        i += 1
        if i < len(chars) and (unit + chars[i]) in _DIGRAPHS:
            unit += chars[i]
            i += 1
        while i < len(chars) and chars[i] in _TRAILING:
            unit += chars[i]
            i += 1
        # The r comes after the length mark, so this check has to wait until
        # the marks have been taken: the unit is "ɑːɹ", not "ɑɹː".
        if i < len(chars) and (_base(unit) + chars[i]) in _R_COLOURED:
            unit += chars[i]
            i += 1
        out.append(Token(unit, "phoneme"))
    return out


def _base(unit: str) -> str:
    """The phoneme without its length mark or diacritics."""
    stripped = "".join(
        c for c in unit if c not in _TRAILING and not unicodedata.combining(c)
    )
    return stripped or unit


# --------------------------------------------------------------------------
# substitution tables
# --------------------------------------------------------------------------
#
# One table per *target* language: what that language's speakers do with a
# sound their own inventory does not contain. The table is keyed on the foreign
# phoneme and does not care which language it arrived from, so the same entry
# serves English through a French voice and Dutch through one.
#
# Only sounds the target lacks need an entry. Everything already in the
# inventory passes through untouched, which is why these tables are short and
# why they are readable as a description of the accent.

_INTO: dict[str, dict[str, str]] = {

    # ---- French -------------------------------------------------------
    # The textbook French accent in English: no /h/, no dental fricatives,
    # a uvular /ʁ/, the tense-lax vowel pairs collapsed, and the English
    # diphthongs flattened into pure vowels.
    "fr": {
        # dental fricatives -- the single most recognisable substitution
        "θ": "s", "ð": "z",
        # h-dropping, all the way
        "h": "", "ʔ": "",
        # the r
        "ɹ": "ʁ", "ɻ": "ʁ", "r": "ʁ", "ɾ": "t", "ɽ": "ʁ", "ʀ": "ʁ", "x": "ʁ",
        # the velar nasal French only has before a stop, hence the audible g
        "ŋ": "nɡ",
        # tense/lax vowel pairs merge to the tense one
        "ɪ": "i", "ᵻ": "i", "ʊ": "u",
        # the English low and central vowels
        "æ": "a", "ɐ": "a", "ʌ": "a", "ɜ": "œʁ", "ɒ": "ɔ", "ɑ": "ɔ", "ɑɹ": "aʁ",
        "ɚɹ": "œʁ", "ɝɹ": "œʁ", "ɚ": "œʁ", "ɝ": "œʁ", "ɵ": "ø", "ʉ": "y", "ɨ": "i",
        # diphthongs flatten; the closing ones keep a glide
        "eɪ": "e", "oʊ": "o", "əʊ": "o",
        "aɪ": "aj", "aʊ": "aw", "ɔɪ": "ɔj",
        "ɪə": "iʁ", "eə": "ɛʁ", "ɛə": "ɛʁ", "ʊə": "uʁ", "ʊɹ": "uʁ", "ɔə": "ɔʁ",
        # consonants French has, spelled differently by espeak
        "ɫ": "l", "ʍ": "w", "ç": "ʃ", "ɥ": "y", "ʎ": "j", "ɟ": "ɡj",
        "β": "b", "ɣ": "ɡ", "ʝ": "j",
    },

    # ---- German -------------------------------------------------------
    # German keeps the lax vowels, so the vowel system survives largely
    # intact. What gives it away is /w/ becoming /v/, the dental fricatives,
    # and final obstruent devoicing.
    "de": {
        "θ": "s", "ð": "d",
        "w": "v", "ʍ": "v",
        "ɹ": "r", "ɻ": "r", "ʁ": "r",
        "æ": "ɛ", "ʌ": "a", "ɒ": "ɔ", "ɑ": "ɔ", "ɑɹ": "aːr", "ᵻ": "ɪ", "ɾ": "t",
        "ɚɹ": "ɐ", "ɝɹ": "ɐ", "ɚ": "ɐ", "ɝ": "ɛr",
        "eɪ": "eː", "oʊ": "oː", "əʊ": "oː",
        "aɪ": "aɪ", "aʊ": "aʊ", "ɔɪ": "ɔʏ",
        "ɪə": "iːɐ", "eə": "ɛːɐ", "ɛə": "ɛːɐ", "ʊə": "uːɐ", "ʊɹ": "uːɐ",
        "ɔə": "ɔːɐ",
        "dʒ": "tʃ", "ʒ": "ʃ", "ɫ": "l", "ʔ": "",
        "ɥ": "yv", "ʎ": "lj", "ɲ": "nj", "β": "b", "ɣ": "ɡ", "ʝ": "j",
        "ɔ̃": "ɔŋ", "ɑ̃": "aŋ", "ɛ̃": "ɛŋ", "œ̃": "œŋ",
    },

    # ---- Spanish ------------------------------------------------------
    # Five vowels and no schwa, so every reduced English vowel comes back as
    # a full one. /v/ merges into /b/ and a word cannot begin with s plus a
    # consonant, which is where the famous "espeak" vowel comes from.
    "es": {
        "h": "x", "v": "b", "ʍ": "w", "z": "s", "ʒ": "ʃ", "dʒ": "ʃ",
        "ɹ": "ɾ", "ɻ": "ɾ", "r": "ɾ", "ŋ": "n", "ʔ": "", "ɾ": "t",
        "ɪ": "i", "ᵻ": "i", "ʊ": "u", "æ": "a", "ʌ": "a", "ɑ": "o", "ɑɹ": "aɾ",
        "ɒ": "o", "ɔ": "o", "ə": "a", "ɜ": "eɾ", "ɚɹ": "eɾ", "ɝɹ": "eɾ", "ɚ": "eɾ", "ɝ": "eɾ",
        "eɪ": "ei", "oʊ": "ou", "əʊ": "ou",
        "aɪ": "ai", "aʊ": "au", "ɔɪ": "oi",
        "ɪə": "ia", "eə": "ea", "ɛə": "ea", "ʊə": "ua", "ʊɹ": "uɾ",
        "ɛ": "e", "ɫ": "l", "ø": "e", "y": "i", "œ": "e",
        "ʁ": "ɾ", "ɥ": "w", "ɐ": "a",
    },

    # ---- Italian ------------------------------------------------------
    # Seven vowels, no schwa, no /h/. Italian keeps every consonant it is
    # given, which is why an Italian speaker's English sounds fully
    # articulated rather than reduced.
    "it": {
        "θ": "t", "ð": "d", "h": "", "ʔ": "",
        "ɹ": "r", "ɻ": "r", "ɾ": "t", "ŋ": "n", "ʍ": "v",
        "ɪ": "i", "ᵻ": "i", "ʊ": "u", "æ": "ɛ", "ʌ": "a", "ɑ": "ɔ", "ɑɹ": "ar",
        "ɒ": "ɔ", "ə": "e", "ɜ": "er", "ɚɹ": "er", "ɝɹ": "er", "ɚ": "er", "ɝ": "er",
        "eɪ": "e", "oʊ": "o", "əʊ": "o",
        "aɪ": "ai", "aʊ": "au", "ɔɪ": "ɔi",
        "ɪə": "ia", "eə": "ɛa", "ɛə": "ɛa", "ʊə": "ua", "ʊɹ": "ur",
        "ɫ": "l", "ø": "e", "y": "i", "œ": "ɛ", "ʁ": "r", "ɐ": "a",
        "ç": "ʃ", "x": "k", "ɣ": "ɡ", "β": "b", "ɥ": "w",
    },

    # ---- Dutch --------------------------------------------------------
    # The closest of these to English, so the accent is carried by a handful
    # of consonants: a fricative /ɣ/ for /ɡ/ in some positions, /w/ as /ʋ/,
    # and the dental fricatives going to /s/ and /d/.
    "nl": {
        "θ": "s", "ð": "d",
        "w": "ʋ", "ʍ": "ʋ",
        "ɹ": "r", "ɻ": "r", "ʁ": "r", "ɾ": "t",
        "æ": "ɛ", "ɒ": "ɔ", "ᵻ": "ɪ", "ɐ": "ə", "ʌ": "ɵ", "ʊ": "u",
        "ɑ": "ɔ", "ɑɹ": "aːr",
        "ɚɹ": "ər", "ɝɹ": "ər", "ɚ": "ər", "ɝ": "ɵr", "ɜ": "ɵ",
        "eɪ": "eː", "oʊ": "oː", "əʊ": "oː",
        "aɪ": "ɑi", "aʊ": "ɑu", "ɔɪ": "ɔi",
        "ɪə": "iːə", "eə": "ɛː", "ɛə": "ɛː", "ʊə": "uːə", "ʊɹ": "uːr",
        "ɔə": "ɔːr", "θ̠": "s",
        "dʒ": "ʒ", "ɫ": "l", "ʔ": "", "ɡ": "ɣ",
        "ʎ": "lj", "β": "b", "ʝ": "j", "ɥ": "ʋ",
    },

    # ---- Portuguese ---------------------------------------------------
    # Brazilian Portuguese palatalises /t/ and /d/ before a high front vowel
    # and adds a vowel after a final consonant, both of which survive into
    # English and are what make the accent recognisable.
    "pt": {
        "θ": "t", "ð": "d", "h": "", "ʔ": "",
        "ɹ": "ɾ", "ɻ": "ɾ", "r": "x", "ʁ": "x", "ŋ": "̃", "ɾ": "t",
        "ɪ": "i", "ᵻ": "i", "ʊ": "u", "æ": "ɛ", "ʌ": "a", "ɑ": "ɔ", "ɑɹ": "aɾ",
        "ɒ": "ɔ", "ə": "ɐ", "ɜ": "ɛɾ", "ɚɹ": "ɛɾ", "ɝɹ": "ɛɾ", "ɚ": "ɛɾ", "ɝ": "ɛɾ",
        "eɪ": "ei", "oʊ": "ou", "əʊ": "ou",
        "aɪ": "ai", "aʊ": "au", "ɔɪ": "ɔi",
        "ɪə": "iɐ", "eə": "ɛɐ", "ɛə": "ɛɐ", "ʊə": "uɐ", "ʊɹ": "uɾ",
        "ɫ": "w", "ʍ": "v", "y": "i", "ø": "e", "œ": "ɛ",
        "ç": "ʃ", "ɣ": "ɡ", "β": "b", "ɥ": "w",
    },

    # ---- English ------------------------------------------------------
    # The fallback direction: French, German or Spanish spoken by an English
    # voice, which happens only when the region's own voices are not
    # installed. It is not what a French controller sounds like, but it is
    # intelligible, which the alternative is not.
    "en": {
        "y": "uː", "ø": "ɜː", "œ": "ɜː", "ɥ": "w", "ʁ": "ɹ", "ʀ": "ɹ",
        "ɲ": "nj", "ʎ": "lj", "ç": "ʃ", "x": "k", "ɣ": "ɡ", "β": "b",
        "ʝ": "j", "ʋ": "v", "ɐ": "ʌ", "ʏ": "ɪ", "ɵ": "ʊ", "ɔʏ": "ɔɪ",
        "ɑ̃": "ɑːn", "ɛ̃": "ɛn", "ɔ̃": "ɔːn", "œ̃": "ɜːn",
        "ɕ": "ʃ", "ʑ": "ʒ", "ɖ": "d", "ʈ": "t",

    },
}

# Substitutions that are a *choice* rather than a necessity, with the accent
# strength at which each one appears.
#
# Everything in the table above is forced: the phoneme is not in the target
# inventory, the model has never made it, and it has to go somewhere whatever
# the speaker's fluency. What is left over is the part fluency actually
# governs -- the sounds the target language *has* and its speakers still use
# differently. Those live here, keyed on the strength at which a speaker stops
# correcting for them, so the dial moves continuously instead of flipping
# between two settings.
#
# The schwa is the important one. French has no reduced vowel in an unstressed
# syllable: every syllable gets its full value, which is what makes the accent
# sound evenly weighted rather than English's stress-and-mumble. A near-fluent
# speaker reduces; a heavily accented one does not.
_OPTIONAL: dict[str, dict[str, tuple[str, float]]] = {
    "fr": {},
    "es": {},
    "it": {},
    "nl": {},
    "de": {},
    "pt": {},
    "en": {},
}

# The strength at which each graded behaviour switches on.
# They are spread across the range the registry actually produces (0.25 to
# 0.88) and ordered the way a speaker sheds them: the vowels stay unreduced
# long after the affricates have come back, so vowel quality switches on low
# and lenition high. Between them the dial has five settings rather than two.
FULL_FINAL_AT = 0.30    # a word-final schwa gets its full value
SCHWA_AT = 0.45         # no vowel is reduced anywhere -- see _OPTIONAL
FINAL_STRESS_AT = 0.60  # the last vowel of each phrase is held
LENITION_AT = 0.75      # the affricates in _STRONG below
PROSTHESIS_AT = 0.50    # the vowel in front of "speedbird"


# Substitutions a heavily accented speaker makes and a lightly accented one
# does not. Every controller gets the table above; the strong ones also get
# this, which is what stops a whole country sounding like one person.
_STRONG: dict[str, dict[str, str]] = {
    # French has neither affricate, so a heavy speaker loses the stop in front
    # of them: "charlie" towards "sharlie", "juliett" towards "zhuliett".
    "fr": {"tʃ": "ʃ", "dʒ": "ʒ"},
    "de": {"v": "f", "dʒ": "tʃ"},
    "es": {"ʃ": "tʃ", "ʒ": "tʃ"},
    "it": {"ə": "a"},
    "nl": {"θ": "t"},
    "pt": {"l": "w"},
    "en": {},
}


# Neither Spanish nor Portuguese allows a word to begin with /s/ and another
# consonant, so a vowel appears in front of one: "Speedbird" comes out as
# "espeedbird". It is the most recognisable single feature of both accents, and
# the first one a fluent speaker learns to drop, so it follows the strength.
_PROSTHESIS: dict[str, str] = {"es": "e", "pt": "i"}


# Languages where a doubled consonant is a different word from a single one.
# Everywhere else the two run together, so a pair that only exists because two
# neighbouring phonemes were substituted into the same sound is collapsed
# rather than left to be read as a geminate.
_GEMINATES: frozenset[str] = frozenset({"it"})


# --------------------------------------------------------------------------
# the words a controller actually says
# --------------------------------------------------------------------------
#
# Rules get a foreign accent most of the way. They do not get it all the way,
# because a controller is not sounding words out: they have said "cleared for
# takeoff" ten thousand times and it comes out as one fluent, memorised chunk.
# The rules also mangle the handful of words a French speaker knows *better*
# than the rule does -- the ICAO alphabet is international, and "Bravo",
# "Papa" and "Delta" are French words a French mouth already owns.
#
# What a controller can say is a closed set. Every word of it comes from a
# template in :mod:`wilcoatc.atc.phraseology` and there are about two hundred
# of them, so the ones that carry the traffic are written out here and the
# rules are left to cover names, callsigns and anything new.
#
# Two lines are held throughout. The entries keep the *English* word and give
# it French sounds -- "degrees" is /deɡʁiz/, not the French word
# /deɡʁe/, because a pilot has to be able to read it back. And every
# symbol used is one the voice has made before, which the tests check against
# the inventory above.
#
# Keys are what espeak-ng's English front end produces, with stress and length
# stripped, so nothing has to align phonemes back to the words they came from.

LEXICON: dict[str, dict[str, str]] = {
    "fr": {
        "ɐdvaɪz": "advajz",  # advise
        "ɐfɜm": "afɛʁm",  # affirm
        "ɐɡɛn": "aɡɛn",  # again
        "ɛɹbɔɹn": "ɛʁbɔʁn",  # airborne
        "ɛɹpɔɹt": "ɛʁpɔʁt",  # airport
        "ælfə": "alfa",  # alpha
        "ɔlɹɛdi": "ɔlʁɛdi",  # already
        "æltɪtud": "altityd",  # altitude
        "ɐpɹoʊtʃ": "apʁotʃ",  # approach
        "ɐpɹuvd": "apʁuvd",  # approved
        "ɚɹaʊnd": "aʁawnd",  # around
        "ɚɹaɪvəl": "aʁajval",  # arrival
        "beɪs": "bes",  # base
        "bɔɹd": "bɔʁd",  # board
        "bɹɑvoʊ": "bʁavo",  # bravo
        "kɔl": "kɔl",  # call
        "kænsɪleɪʃən": "kansɛlaʃɛn",  # cancellation
        "tʃeɪndʒ": "ʃɛndʒ",  # change
        "tʃɑɹli": "ʃaʁli",  # charlie
        "klɪɹəns": "kliʁans",  # clearance
        "klɪɹd": "kliʁd",  # cleared
        "klaɪm": "klajm",  # climb
        "kɑntækt": "kɔntakt",  # contact
        "kəntɪnju": "kɔntinju",  # continue
        "kɚɹɛkt": "kɔʁɛkt",  # correct
        "kɹɔs": "kʁɔs",  # cross
        "dɛsɪməl": "desimal",  # decimal
        "dᵻɡɹiz": "deɡʁiz",  # degrees
        "dɛltə": "dɛlta",  # delta
        "dᵻpɑɹɾᵻd": "dipaʁtɛd",  # departed
        "dᵻpɑɹtʃɚ": "depaʁtʃœʁ",  # departure
        "dᵻsɛnd": "desɛnd",  # descend
        "dɛstɪneɪʃən": "dɛstinaʃɛn",  # destination
        "divɪeɪʃən": "deviaʃɛn",  # deviation
        "dᵻɹɛkt": "diʁɛkt",  # direct
        "daʊnwɪnd": "dawnwind",  # downwind
        "ɛkoʊ": "eko",  # echo
        "eɪt": "ejt",  # eight
        "ᵻlɛvən": "ilɛvɛn",  # eleven
        "ɪmɜdʒənsi": "imœʁdʒɛnsi",  # emergency
        "ɛntɚd": "ɛntœʁd",  # entered
        "ɪkwɪpmənt": "ikwipmɛnt",  # equipment
        "ɛkspɛkt": "ɛkspɛkt",  # expect
        "ɛkstɛnd": "ɛkstɛnd",  # extend
        "fæktɚ": "faktœʁ",  # factor
        "fild": "fild",  # field
        "faɪf": "fajf",  # fife
        "faɪld": "fajld",  # filed
        "faɪnəl": "fajnal",  # final
        "faɪv": "fajv",  # five
        "fɪks": "fiks",  # fix
        "flaɪt": "flajt",  # flight
        "flaɪ": "flaj",  # fly
        "fɑloʊ": "fɔlo",  # follow
        "fɔɹ": "fɔʁ",  # four
        "faʊɚ": "fawœʁ",  # fower
        "fɑkstɹɑt": "fɔkstʁɔt",  # foxtrot
        "fɹikwənsi": "fʁikwɛnsi",  # frequency
        "fjuəl": "fjuɛl",  # fuel
        "ɡɪv": "ɡiv",  # give
        "ɡɑlf": "ɡɔlf",  # golf
        "ɡɹaʊnd": "ɡʁawnd",  # ground
        "ɡʌstɪŋ": "ɡastinɡ",  # gusting
        "hɛdɪŋ": "ɛdinɡ",  # heading
        "hɪɹ": "iʁ",  # hear
        "hɛvi": "ɛvi",  # heavy
        "hoʊld": "old",  # hold
        "hoʊtɛl": "otɛl",  # hotel
        "haʊ": "aw",  # how
        "hʌndɹɪd": "andʁɛd",  # hundred
        "aɪdɛntɪfɪkeɪʃən": "ajdɛntifikeʃɛn",  # identification
        "ɪfɚ": "i ɛf aʁ",  # ifr
        "ɪlz": "i ɛl ɛs",  # ils
        "ɪndiə": "ɛ̃dja",  # india
        "ɪntɛnʃənz": "intɛnʃɛnz",  # intentions
        "dʒulɪɛt": "ʒyljɛt",  # juliet
        "kɛnədi": "kɛnedi",  # kennedy
        "kiloʊ": "kilo",  # kilo
        "nɑts": "nɔts",  # knots
        "lænd": "land",  # land
        "lændᵻd": "landɛd",  # landed
        "lændɪŋ": "landinɡ",  # landing
        "lɛft": "lɛft",  # left
        "lɛvəl": "levɛl",  # level
        "limɐ": "lima",  # lima
        "laɪn": "lajn",  # line
        "lɔst": "lɔst",  # lost
        "loʊ": "lo",  # low
        "meɪnteɪn": "mɛnten",  # maintain
        "meɪk": "mek",  # make
        "maɪk": "majk",  # mike
        "maɪlz": "majlz",  # miles
        "mɑnɪɾɚ": "monitœʁ",  # monitor
        "neɪtʃɚ": "netʃœʁ",  # nature
        "nævɪɡeɪʃən": "naviɡaʃɛn",  # navigation
        "nɛɡətɪv": "neɡativ",  # negative
        "naɪn": "najn",  # nine
        "naɪnɚ": "najnœʁ",  # niner
        "nɔɹməl": "nɔʁmal",  # normal
        "noʊvɛmbɚ": "novɑ̃bɛʁ",  # november
        "nʌmbɚ": "nambœʁ",  # number
        "wʌn": "wan",  # one
        "ɑpʃən": "ɔpʃɛn",  # option
        "ɑskɚ": "ɔskaʁ",  # oscar
        "oʊn": "on",  # own
        "pɑpə": "papa",  # papa
        "paɪlət": "pajlɔt",  # pilot
        "pɔɪnt": "pwɛnt",  # point
        "pəzɪʃən": "poziʃɛn",  # position
        "pɹɛzənt": "pʁezɛnt",  # present
        "pɹiviəsli": "pʁiviusli",  # previously
        "pɹaɪɔɹᵻɾi": "pʁajɔʁiti",  # priority
        "pɹəsid": "pʁosid",  # proceed
        "pʊʃ": "puʃ",  # push
        "kjuɛneɪtʃ": "ky ɛn aʃ",  # qnh
        "kwᵻbɛk": "kebɛk",  # quebec
        "ɹeɪdɑɹ": "ʁadaʁ",  # radar
        "ɹæmp": "ʁamp",  # ramp
        "ɹid": "ʁid",  # read
        "ɹidbæk": "ʁidbak",  # readback
        "ɹɛdi": "ʁɛdi",  # ready
        "ɹᵻsivd": "ʁisivd",  # received
        "ɹᵻdus": "ʁidus",  # reduce
        "ɹᵻmeɪn": "ʁimen",  # remain
        "ɹᵻmeɪnɪŋ": "ʁimeninɡ",  # remaining
        "ɹᵻpɔɹt": "ʁipɔʁt",  # report
        "ɹᵻpɔɹɾᵻd": "ʁipɔʁtɛd",  # reported
        "ɹᵻkwɛst": "ʁikwɛst",  # request
        "ɹᵻzum": "ʁizum",  # resume
        "ɹaɪt": "ʁajt",  # right
        "ɹɑdʒɚ": "ʁɔdʒœʁ",  # roger
        "ɹoʊmɪoʊ": "ʁomeo",  # romeo
        "ɹʌnweɪ": "ʁanwɛ",  # runway
        "seɪ": "se",  # say
        "sɛpɚɹeɪʃən": "sepaʁaʃɛn",  # separation
        "sɜvɪs": "sœʁvis",  # service
        "sɛvən": "sɛvɛn",  # seven
        "sɛvəntin": "sɛvɛntin",  # seventeen
        "ʃɔɹt": "ʃɔʁt",  # short
        "siɛɹə": "sjɛʁa",  # sierra
        "saɪt": "sajt",  # sight
        "sɪks": "siks",  # six
        "soʊlz": "solz",  # souls
        "spid": "spid",  # speed
        "spidbɜd": "spidbœʁd",  # speedbird
        "skwɔk": "skwɔk",  # squawk
        "stændbaɪ": "standbaj",  # standby
        "stændɪŋ": "standinɡ",  # standing
        "stɑɹt": "staʁt",  # start
        "steɪt": "stet",  # state
        "teɪkɔf": "tekɔf",  # takeoff
        "tæŋɡoʊ": "tɑ̃ɡo",  # tango
        "tæksi": "taksi",  # taxi
        "tɛn": "tɛn",  # ten
        "tɜmᵻneɪɾᵻd": "tɛʁminetɛd",  # terminated
        "θɜɾi": "sœʁti",  # thirty
        "θaʊzənd": "sawzand",  # thousand
        "θɹi": "sʁi",  # three
        "taʊɚ": "tawœʁ",  # tower
        "tɹæfɪk": "tʁafik",  # traffic
        "tɹi": "tʁi",  # tree
        "tɜn": "tœʁn",  # turn
        "tɜnz": "tœʁnz",  # turns
        "twɛlv": "twɛlv",  # twelve
        "tu": "tu",  # two
        "junɪfɔɹm": "ynifɔʁm",  # uniform
        "ʌnnoʊn": "anonn",  # unknown
        "vɛktɚz": "vɛktœʁz",  # vectors
        "vɛɹᵻfaɪ": "veʁifaj",  # verify
        "viɛfɑɹ": "ve ɛf aʁ",  # vfr
        "vaɪə": "vja",  # via
        "vaɪəɹ": "vja",  # via, with the linking r espeak adds
        "vɪktɚ": "viktɔʁ",  # victor
        "vɪʒuəl": "vizyal",  # visual
        "weɪt": "wɛt",  # wait
        "weɪ": "wɛ",  # way
        "wɪski": "wiski",  # whiskey
        "wɪlkoʊ": "wilko",  # wilco
        "wɪnd": "wind",  # wind
        "wɪðaʊt": "wizawt",  # without
        "ɛksɹeɪ": "iksʁɛ",  # xray
        "jæŋki": "jɑ̃ki",  # yankee
        "jɔɹz": "jɔʁz",  # yours
        "ziəɹoʊ": "zeʁo",  # zero
        "zulu": "zulu",  # zulu

        # --- callsign telephony, said on every transmission ------
        "ɛɹoʊflɑt": "aeʁoflɔt",  # aeroflot
        "ɛɹfɹənz": "ɛʁfʁɑ̃s",  # airfrans
        "ɐlæskə": "alaska",  # alaska
        "ɐsiɑnə": "azjana",  # asiana
        "ɔstɹiən": "ɔstʁijɛn",  # austrian
        "eɪvɪæŋkə": "avjanka",  # avianca
        "bæɹən": "baʁɔn",  # baron
        "bənænzə": "bonanza",  # bonanza
        "kænədə": "kanada",  # canada
        "kæɹɐvæn": "kaʁavan",  # caravan
        "sɛsnə": "sɛsna",  # cessna
        "tʃɛɹoʊki": "ʃeʁoki",  # cherokee
        "tʃaɪnə": "ʃina",  # china
        "sɪɹəs": "siʁys",  # cirrus
        "saɪteɪʃən": "sitasjɔn",  # citation
        "kəmæntʃi": "komanʃ",  # comanche
        "kɑndɔɹ": "kɔndɔʁ",  # condor
        "daɪəmənd": "dajamɔnd",  # diamond
        "daɪnɐsti": "dinasti",  # dynasty
        "idʒɪptɪɚ": "eʒiptɛʁ",  # egyptair
        "ɛmᵻɹeɪts": "emiʁɛts",  # emirates
        "iθɪoʊpiən": "esjopjɛn",  # ethiopian
        "jʊɹɹoʊtɹænz": "øʁotʁɑ̃s",  # eurotrans
        "fɹʌntɪɹ": "fʁɔntje",  # frontier
        "həwaɪən": "awajɛn",  # hawaiian
        "aɪbɪɹiə": "ibeʁja",  # iberia
        "aɪdənt": "idɛnt",  # ident
        "dʒɔɹdeɪniən": "ʒɔʁdanjɛn",  # jordanian
        "kætɑnə": "katana",  # katana
        "lʌfθænsə": "luftɑ̃za",  # lufthansa
        "mælɪbu": "malibu",  # malibu
        "muni": "muni",  # mooney
        "nu": "nju",  # new
        "nɪpən": "nipɔn",  # nippon
        "əklɑk": "oklɔk",  # o'clock
        "ʌv": "ɔv",  # of
        "fɪlᵻpin": "filipin",  # philippine
        "pɪlæɾəs": "pilatys",  # pilatus
        "pɔɹtʃəɡəl": "pɔʁtyɡal",  # portugal
        "kæntəz": "kwɑ̃tas",  # qantas
        "ɹeɪdɪəl": "ʁadjal",  # radial
        "skændɪneɪviən": "skandinavjɛn",  # scandinavian
        "sɛmɪnoʊl": "seminɔl",  # seminole
        "sɛnɛkə": "seneka",  # seneca
        "ʃæmɹɑk": "ʃamʁɔk",  # shamrock
        "sɪŋɡɐpɔɹ": "sinɡapuʁ",  # singapore
        "tɹænseɪviə": "tʁɑ̃zavja",  # transavia
        "tɜkɪʃ": "tœʁkiʃ",  # turkish
        "junaɪɾᵻd": "junajtɛd",  # united
        "ʌp": "ap",  # up
        "vɜdʒɪn": "vœʁʒin",  # virgin
        "zilənd": "zilɛnd",  # zealand
        "pɑsᵻbəl": "pɔsibl",  # possible
        "ʌneɪbəl": "anebl",  # unable
    },
}


# --------------------------------------------------------------------------
# prosody
# --------------------------------------------------------------------------
#
# Where a language puts its prominence is as much of a tell as which consonants
# it can make. English stresses one syllable per word and reduces the rest;
# French stresses the last syllable of each word and reduces nothing. Carrying
# English stress placement into a French voice sounds like an English speaker
# doing an impression, so it is re-laid.

# Languages whose stress lands on the last syllable of the word.
_FINAL_STRESS: frozenset[str] = frozenset({"fr"})

_VOWELS = frozenset("aeiouyæøœɐɑɒɔəɚɛɜɤɨɪʉʊʌʏɘɵ")


def _is_vowel(unit: str) -> bool:
    return bool(unit) and _base(unit)[:1] in _VOWELS


def relay_stress(tokens: list[Token], target: str,
                 strength: float = 1.0) -> list[Token]:
    """Give the line the rhythm of the target language.

    Only French has one to give, and only for a speaker whose accent is heavy.
    Rhythm is the last thing a learner acquires and the first thing a fluent
    speaker gets right, so a light French accent keeps the English pattern and
    a heavy one falls back to the French rule.

    The other accents never change it: a German or Dutch speaker of English
    gets the rhythm right and the vowels wrong, and reversing that would be a
    caricature rather than an accent.

    **The French rule is about phrases, not words, and it is about length.**
    French has no word stress at all. What it has is one accent at the end of
    each *groupe rythmique* -- the run of words said as a unit -- and that
    accent is mostly duration: the last vowel of the group is held. This used
    to move the stress mark inside every word instead, which is a third
    language, neither English nor French::

        said       runway zero three
        English    ɹˈʌnweɪ zˈiəɹoʊ θɹˈiː
        per word   ʁanwˈe  ziəʁˈo  θʁˈiː      <- what this used to do
        French     ʁˈanwe  zˈiəʁo  θʁˈiːː     <- one accent, held, at the end

    Marking every word hammers each one in turn, which is how a machine
    imitating an accent sounds rather than how a person with one sounds -- and
    it moves the shape of every word at once, which is what the shapes are for.
    All three were put through the recogniser, on five transmissions, at the
    heaviest accent the casting produces::

        stress mark moved, every word            0.69 of the words back
        stress mark moved, last word of phrase   0.78
        final vowel held, last word of phrase    0.87
        nothing at all                           0.88

    Holding the vowel keeps almost all of the words and is the realisation a
    phonetician would name first, which is a pleasant way for a measurement to
    come out. The clause is the unit, because the phraseology templates put a
    comma wherever a controller draws breath, so the two coincide about as
    closely as they can without a parser.
    """
    if target not in _FINAL_STRESS or strength < FINAL_STRESS_AT:
        return tokens

    # No stress mark is ever added, moved or removed. English and French agree
    # about which words are prominent -- the content ones, which espeak has
    # already marked -- so espeak's marks are read to find the end of the
    # group and are then left exactly where they are.
    out: list[Token] = []
    # The clause as it is read, in order: each entry is either a word or the
    # separator that followed one. Words are buffered rather than emitted
    # because which of them takes the accent is not known until the clause
    # ends.
    clause: list[list[Token] | Token] = []
    word: list[Token] = []

    def end_word() -> None:
        if word:
            clause.append(list(word))
            word.clear()

    def end_clause() -> None:
        """Emit the clause, with its one accent at the end of it."""
        end_word()
        if not clause:
            return
        # The last word English thought was prominent carries the clause's
        # accent. The ones before it keep their own stress, because taking
        # those away as well flattens the line into a monotone, which is the
        # fault this whole module exists to avoid.
        chosen = -1
        for index, item in enumerate(clause):
            if isinstance(item, list) and any(
                    t.kind == "stress" and t.text == "ˈ" for t in item):
                chosen = index
        for index, item in enumerate(clause):
            if not isinstance(item, list):
                out.append(item)
            elif index == chosen:
                out.extend(_hold_final_vowel(item))
            else:
                out.extend(item)
        clause.clear()

    for token in tokens:
        if token.kind == "punct":
            end_clause()
            out.append(token)
        elif token.kind == "space":
            end_word()
            clause.append(token)
        else:
            word.append(token)
    end_clause()
    return out


def _hold_final_vowel(word: list[Token]) -> list[Token]:
    """The same word with its last vowel held, as a phrase-final one is.

    The word keeps the stress English gave it. Only the length changes, which
    is what the French accent is made of and is the part a listener can afford
    to have moved: a vowel that runs on is still the same vowel.
    """
    out = list(word)
    last = -1
    for index, token in enumerate(out):
        if token.kind == "phoneme" and _is_vowel(token.text):
            last = index
    if last < 0 or "ː" in out[last].text:
        return word
    out[last] = Token(out[last].text + "ː", "phoneme")
    return out


# --------------------------------------------------------------------------
# the transformation
# --------------------------------------------------------------------------


def substitute(tokens: list[Token], target: str, strength: float = 0.5) -> list[Token]:
    """Replace every phoneme the target voice cannot make.

    ``strength`` selects how heavy the accent is, between 0 and 1. Above the
    halfway mark the speaker also makes the substitutions in :data:`_STRONG`,
    which are the ones a fluent speaker of the language has learned not to.
    """
    inventory = INVENTORY.get(target, INVENTORY["en"])
    table = dict(_INTO.get(target, {}))
    for phoneme, (replacement, at) in _OPTIONAL.get(target, {}).items():
        if strength >= at:
            table[phoneme] = replacement
    if strength >= LENITION_AT:
        table.update(_STRONG.get(target, {}))
    keep_length = target in KEEPS_LENGTH

    out: list[Token] = []
    for token in tokens:
        if token.kind != "phoneme" or token.fixed:
            out.append(token)
            continue

        unit = token.text
        if not keep_length:
            unit = unit.replace(_LENGTH, "").replace("ˑ", "")

        replacement = table.get(unit)
        if replacement is None:
            stripped = unit.replace(_LENGTH, "")
            replacement = table.get(stripped)
        if replacement is None:
            base = _base(unit)
            if all(c in inventory or c in _TRAILING or unicodedata.combining(c)
                   for c in unit):
                replacement = unit
            elif base in inventory:
                replacement = base
            else:
                replacement = table.get(base)
        if replacement is None:
            log.debug("no %s realisation for %r; dropping it", target, unit)
            continue
        if not replacement:
            continue

        for produced in tokenize(replacement):
            out.append(produced)

    out = _prosthesis(out, target, strength)
    return out if target in _GEMINATES else _degeminate(out)


def _prosthesis(tokens: list[Token], target: str, strength: float) -> list[Token]:
    """Put a vowel in front of a word that starts /s/ plus a consonant."""
    vowel = _PROSTHESIS.get(target) if strength >= PROSTHESIS_AT else None
    if not vowel:
        return tokens

    out: list[Token] = []
    at_word_start = True
    for index, token in enumerate(tokens):
        if token.kind in ("space", "punct"):
            at_word_start = True
            out.append(token)
            continue
        if at_word_start and token.kind == "phoneme" and _base(token.text) == "s":
            following = next(
                (t for t in tokens[index + 1:] if t.kind == "phoneme"), None
            )
            if following is not None and not _is_vowel(following.text):
                out.append(Token(vowel, "phoneme"))
        if token.kind == "phoneme":
            at_word_start = False
        out.append(token)
    return out


def _degeminate(tokens: list[Token]) -> list[Token]:
    """Collapse a consonant that ended up doubled by substitution."""
    out: list[Token] = []
    for token in tokens:
        if (token.kind == "phoneme" and out and out[-1].kind == "phoneme"
                and out[-1].text == token.text and not _is_vowel(token.text)):
            continue
        out.append(token)
    return out


# Languages that never leave a vowel reduced at the end of a word. English does
# it constantly -- "Alaska", "Cessna", "Canada" all end in a schwa -- and a
# French, Spanish or Italian speaker gives that final vowel its full value
# instead. It is one of the most audible things about all three accents, and
# it is a rule rather than a dozen lexicon entries.
#
# Monosyllables are left alone: "the" and "a" really are said with a schwa.
_FULL_FINAL_VOWEL: dict[str, str] = {"fr": "a", "es": "a", "it": "a", "pt": "ɐ"}

# The same thing one syllable earlier. English reduces every unstressed vowel
# in the word, not just the last one, and a French speaker reduces none of
# them: that even weighting is most of what the accent *is*, and it survives
# long after the consonants have been corrected. Monosyllables are left alone
# for the same reason as above.
_UNREDUCED: dict[str, str] = {"fr": "ɛ", "es": "a", "it": "e", "pt": "ɐ"}


def _unreduce(tokens: list[Token], target: str, strength: float) -> list[Token]:
    """Give every unstressed vowel inside a word its full value."""
    full = _UNREDUCED.get(target) if strength >= SCHWA_AT else None
    if not full:
        return tokens
    for word in _words(tokens):
        phonemes = [t for t in word if t.kind == "phoneme"]
        if sum(1 for t in phonemes if _is_vowel(t.text)) < 2:
            continue
        previous = ""
        for token in phonemes:
            if (not token.fixed and _base(token.text) == "ə"
                    and not _is_vowel(previous)):
                # A schwa straight after another vowel is the second half of a
                # diphthong espeak wrote as two symbols -- the "iə" of "zero",
                # the "ʊə" of "tour". Giving it a full value adds a syllable
                # the word does not have: "zero" comes out as "zi-e-ro", which
                # a recogniser hears as "Zierho" and no French speaker says.
                token.text = full
            previous = token.text
    return tokens


def _words(tokens: list[Token]) -> list[list[Token]]:
    """Split a token stream on the spaces and punctuation between words."""
    words: list[list[Token]] = [[]]
    for token in tokens:
        if token.kind in ("space", "punct"):
            words.append([])
        else:
            words[-1].append(token)
    return [w for w in words if w]


def _open_final_vowels(tokens: list[Token], target: str,
                       strength: float = 1.0) -> list[Token]:
    """Give a word-final schwa its full value."""
    full = _FULL_FINAL_VOWEL.get(target) if strength >= FULL_FINAL_AT else None
    if not full:
        return tokens

    words: list[list[Token]] = [[]]
    for token in tokens:
        if token.kind in ("space", "punct"):
            words.append([token])
            words.append([])
        else:
            words[-1].append(token)

    for word in words:
        phonemes = [t for t in word if t.kind == "phoneme"]
        if len(phonemes) < 2:
            continue
        vowels = [t for t in phonemes if _is_vowel(t.text)]
        if len(vowels) < 2 or _base(phonemes[-1].text) != "ə":
            continue
        if phonemes[-1].fixed:
            continue
        phonemes[-1].text = full

    return [t for word in words for t in word]


def phoneme_blocks(text: str, spoken: str, target: str, strength: float = 0.0,
                   espeak_voice: str = "") -> list[str]:
    """Phonemes for ``text``, said in ``spoken``, by a voice trained on ``target``.

    Returns one Piper raw-phoneme block per sentence, each ready to hand to
    ``PiperVoice.synthesize`` in place of the text. Punctuation survives into
    the block, so the pauses the phraseology layer put in are still there, and
    keeping the sentences apart keeps Piper's own per-sentence chunking.

    This runs even when the voice already speaks the language, because espeak's
    front end reaches for English whenever a word looks English to it. Left
    alone, a French voice reads "Speedbird" as /spiːdbɜːd/ and "Saint Exupery"
    as /ɛɡzjuːpəɹi/ -- phonemes a French model has never produced, which come
    out as a glitch rather than as an accent. Folding everything through the
    inventory turns them into /spidbœʁd/ and /ɛɡzjupeʁi/, which is what the
    controller would actually say.
    """
    voice = resolve_voice(espeak_voice or source_voice(spoken, target))
    sentences = _phonemizer().phonemize(voice, respell(text, spoken))
    blocks: list[str] = []
    for sentence in sentences:
        tokens = apply_lexicon(tokenize(sentence), spoken, target)
        tokens = substitute(tokens, target, strength)
        tokens = _open_final_vowels(tokens, target, strength)
        tokens = _unreduce(tokens, target, strength)
        tokens = relay_stress(tokens, target, strength)
        body = _render(tokens)
        if body:
            blocks.append(f"[[{body}]]")
    return blocks


def word_key(word: list[Token]) -> str:
    """A word's phonemes, bare, for looking it up.

    Stress and length come off, because they vary with where the word sits in
    the sentence and the lexicon is about the word itself. Homophones collapse
    onto one entry, which is what should happen: "for" and "four" are one
    sound to espeak and one sound to the controller.
    """
    out = []
    for token in word:
        if token.kind != "phoneme":
            continue
        for ch in token.text:
            if ch in _STRESS or ch in (_LENGTH, "ˑ", "ʲ") or unicodedata.combining(ch):
                continue
            out.append(ch)
    return "".join(out)


def _stressed_syllable(word: list[Token]) -> int | None:
    """Which syllable of the word carries the primary accent, counting vowels.

    Returns ``None`` when espeak left the word unstressed, which it does for
    function words in every language it reads.
    """
    seen = 0
    pending = False
    for token in word:
        if token.kind == "stress":
            pending = token.text == "ˈ"
            continue
        if token.kind == "phoneme" and _is_vowel(token.text):
            if pending:
                return seen
            seen += 1
    return None


def apply_lexicon(tokens: list[Token], spoken: str, target: str) -> list[Token]:
    """Replace the words that are written out by hand.

    Only when the controller is speaking a foreign language: the lexicon says
    how a French mouth handles an English word, which is not a question that
    arises when they are speaking French.
    """
    table = LEXICON.get(target, {}) if spoken != target else {}
    if not table:
        return tokens

    out: list[Token] = []
    word: list[Token] = []

    def flush() -> None:
        if not word:
            return
        said = table.get(word_key(word))
        if said is None:
            out.extend(word)
        else:
            # The lexicon says which *sounds* the word is made of. It does not
            # say which syllable is leaned on: that is prosody, it belongs to
            # relay_stress, and it follows the speaker's fluency. Writing the
            # accent onto the last syllable here instead put every word of
            # every transmission -- the phrasebook is almost entirely in this
            # table -- on final stress no matter how good the controller's
            # English was, which is the chant the module was written to avoid.
            #
            # So the stress keeps the syllable it had. "SPEEDbird" stays on its
            # first syllable, and a heavy accent moves it later, once.
            spelled = [Token(t.text, t.kind, fixed=True) for t in tokenize(said)]
            at = _stressed_syllable(word)
            if at is not None:
                vowels = [i for i, t in enumerate(spelled)
                          if t.kind == "phoneme" and _is_vowel(t.text)]
                if vowels:
                    spelled.insert(vowels[min(at, len(vowels) - 1)],
                                   Token("ˈ", "stress"))
            out.extend(spelled)
        word.clear()

    for token in tokens:
        if token.kind in ("space", "punct"):
            flush()
            out.append(token)
        else:
            word.append(token)
    flush()
    return out


def _render(tokens: list[Token]) -> str:
    """Flatten tokens back to a string, without doubled or dangling spaces."""
    out: list[str] = []
    for token in tokens:
        if token.kind == "space":
            if out and out[-1] != " ":
                out.append(" ")
            continue
        if token.kind == "punct" and out and out[-1] == " ":
            out[-1] = token.text
            continue
        out.append(token.text)
    return "".join(out).strip()


# --------------------------------------------------------------------------
# respelling
# --------------------------------------------------------------------------
#
# A handful of names espeak-ng's own dictionary gets wrong, respelled so that
# it gets them right. Folding phonemes through the inventory fixes a word read
# in the wrong language; it cannot fix a word read in the right language and
# still mispronounced, which is what these are. Keep the list short and keep it
# to names a controller says on every flight.

RESPELL: dict[str, dict[str, str]] = {
    "fr": {
        "orly": "Orli",              # otherwise read as English: /ɔːli/, no r
        "montreal": "Montréal",      # the unaccented spelling is read as English
        "exupery": "Exupéri",
        "quebec": "Québec",
        "atis": "atisse",            # the final s is sounded on the radio
    },
    "en": {
        "atis": "AY-tiss",
    },
}

_RESPELL_PATTERNS: dict[str, object] = {}


def respell(text: str, language: str) -> str:
    """Rewrite the words espeak reads wrongly in this language."""
    table = RESPELL.get((language or "")[:2])
    if not table:
        return text

    import re

    pattern = _RESPELL_PATTERNS.get(language)
    if pattern is None:
        pattern = re.compile(
            r"\b(" + "|".join(sorted(table, key=len, reverse=True)) + r")\b",
            re.IGNORECASE,
        )
        _RESPELL_PATTERNS[language] = pattern
    return pattern.sub(lambda m: table[m.group(0).lower()], text)


# espeak's own name for each language, where it differs from the ISO code.
_ESPEAK_VOICE: dict[str, str] = {
    "en": "en-us",
    "pt": "pt-br",
}

def source_voice(spoken: str, target: str) -> str:
    """Which espeak front end reads the words, before the accent is applied.

    This decides the *words*, not the sounds: which English the speaker
    learned, not how they say it. Their own language decides that.

    American English is the base, and the reason is the letter r. French,
    German, Spanish, Italian and Portuguese all pronounce every r they see,
    and so do their speakers in English; British English does not, and using
    it dropped the r out of "for", "four" and "Orly" -- which no French
    speaker does. The two American habits that would leak through are handled
    in the tables instead: the tapped t of "thirty" becomes a real t, and the
    vowel of "knots" is separated from the vowel of "radar" by whether an r
    follows it.
    """
    return _ESPEAK_VOICE.get(spoken, spoken)


# espeak-ng names its language files after the language and not after the
# locale, and its plain "en" is the British one -- American English is the
# variant, which is the opposite of how the rest of this project spells it.
# "en-gb" and "fr-fr" are aliases the full espeak voice list resolves and the
# phonemiser bridge does not; asking it for one raises, and what it raised into
# was a fallback that handed Piper the text and lost the accent. A model that
# names its own voice in a Piper json ("en-gb", for every British voice there
# is) went down that path on every transmission.
_VOICE_ALIASES: dict[str, str] = {
    "en-gb": "en", "en_gb": "en", "en-uk": "en", "gb": "en",
    "fr-fr": "fr", "fr_fr": "fr",
    "de-de": "de", "de_de": "de",
    "es-es": "es", "es_es": "es",
    "it-it": "it", "it_it": "it",
    "nl-nl": "nl", "nl_nl": "nl",
    "pt-pt": "pt",
}


def resolve_voice(name: str) -> str:
    """The espeak-ng voice that actually exists for a locale-shaped name."""
    cleaned = (name or "").strip()
    return _VOICE_ALIASES.get(cleaned.lower(), cleaned)


# Which English a native speaker of it reads. The difference is rhoticity and
# the vowel of "contact", and it is the whole of what separates a British
# controller from an American one once the voice is already British.
_NATIVE_ENGLISH: dict[str, str] = {"gb": "en", "us": "en-us"}


def native_voice(language: str, accent: str) -> str:
    """The espeak front end that reads a speaker's own language.

    ``accent`` only means anything for English, where it chooses between the
    British reading and the American one -- the difference is rhoticity, and it
    is the whole of what separates the two once the voice is already British.
    Everywhere else the language decides on its own and the accent is ignored.

    That is not a detail. This took the accent as if it were always a variety
    of English for a while, so a French controller speaking French was
    phonemised by the *American* front end: "trois deux un" came out as
    /twˈɔ djˈu ˈan/ and "zero" as the English word, with the nasal vowels gone
    altogether. A French voice reading French by English spelling rules does
    not sound French, and the nearest name for what it does sound like is
    Spanish.
    """
    language = (language or "en").lower()[:2]
    if language != "en":
        return source_voice(language, language)
    return _NATIVE_ENGLISH.get((accent or "").lower(), "en-us")

_PHONEMIZER = None


def _phonemizer():
    """One espeak phonemiser, shared. Creating one is not free."""
    global _PHONEMIZER
    if _PHONEMIZER is None:
        from piper.phonemize_espeak import EspeakPhonemizer

        _PHONEMIZER = EspeakPhonemizer()
    return _PHONEMIZER


def describe(spoken: str, target: str) -> str:
    """A one-line description of what this pairing sounds like."""
    if spoken == target:
        return f"{target} spoken natively"
    return f"{spoken} spoken with a {target} accent"


# --------------------------------------------------------------------------
# the other direction: a French mouth on an English voice
# --------------------------------------------------------------------------
#
# Everything above renders a French controller's English on a *French* model.
# That is the faithful thing to do and it has a ceiling nothing in this file
# can lift: French has 33 phonemes, English has 44, and folding one into the
# other throws away the nine that carry a good deal of what makes a word
# recognisable. Push it far enough for the accent to be convincing and the
# transmission stops being readable back -- "without your takeoff clearance"
# arrives as "with you take of clearance" -- and a controller a pilot cannot
# read back is not realistic, whatever the accent sounds like.
#
# It is also why the result sounds mechanical. A French model handed English
# word shapes is being asked for sequences it never saw in training, and a
# duration predictor outside its training distribution is exactly what a
# listener hears as robotic.
#
# So this is the same accent built the other way round: an *English* model,
# which owns the words and their rhythm, given the handful of substitutions a
# French speaker actually keeps. Everything here stays inside the English
# inventory, so the model is never asked for anything strange. What is lost is
# the uvular r, the single most recognisable feature and the one English
# cannot spell. What is gained is a voice that reads as a person, and a
# clearance that survives the trip.
#
# Which of the two is right depends on the transmission: the French model for
# a controller heard in passing on another frequency, this one for the
# controller the pilot is working.

_COLOUR: dict[str, dict[str, tuple[str, float]]] = {
    # keyed on the *speaker's* language; the value is what they do to English
    # and the strength at which they stop doing it.
    "fr": {
        # the two that survive any amount of fluency
        "θ": ("s", 0.0), "ð": ("z", 0.0),
        # American English taps the t of "departed" and "thirty"; no speaker
        # of a language that pronounces every t it writes does that, and it is
        # half of why "departed" came back as "departid".
        "ɾ": ("t", 0.0),
        # h-dropping: iconic, and it costs nothing to understand
        "h": ("", 0.15),
        # no tense/lax contrast, so the pairs merge to the tense member
        "ɪ": ("i", 0.30), "ᵻ": ("i", 0.30), "ʊ": ("u", 0.45),
        # the vowels English has and French does not
        "æ": ("a", 0.30), "ʌ": ("a", 0.30), "ɐ": ("a", 0.30),
        # ("the" keeps its schwa: the vowel rule is _unreduce, below, which
        #  knows how long the word is.)
        # the diphthongs flatten into the pure vowels French owns
        "eɪ": ("e", 0.55), "oʊ": ("o", 0.55), "əʊ": ("o", 0.55),
        # and last, the affricates a fluent speaker has learned to keep
        "tʃ": ("ʃ", 0.80), "dʒ": ("ʒ", 0.80),

        # --- sounds English does not have ------------------------------
        # Everything above stays inside the English inventory, which is what
        # lets any English voice say it. These do not, so they are only
        # reachable on a model whose phoneme table is shared across languages
        # -- Kokoro's is, Piper's is one table per model and is not. The
        # caller says which by passing ``can_say``.
        #
        # This tier is the accent itself. Without the uvular r a French
        # speaker's English is recognisably foreign and not recognisably
        # French, which is the one thing the version before it got right.
        "ɹ": ("ʁ", 0.40), "ɻ": ("ʁ", 0.40),
        "ɜ": ("œʁ", 0.45), "ɝ": ("œʁ", 0.45), "ɚ": ("œʁ", 0.45),
        "ɑɹ": ("aʁ", 0.40), "ɚɹ": ("œʁ", 0.40), "ɝɹ": ("œʁ", 0.40),
        "ɪə": ("iʁ", 0.60), "eə": ("ɛʁ", 0.60), "ɛə": ("ɛʁ", 0.60),
        "ʊə": ("uʁ", 0.60), "ʊɹ": ("uʁ", 0.60), "ɔə": ("ɔʁ", 0.60),
        # the velar nasal French only has in front of a stop, hence the g
        "ŋ": ("nɡ", 0.65),
    },
}


# What a voice with a phoneme table shared across languages can pronounce.
# Kokoro has one; hand it to :func:`colour_blocks` as ``can_say`` and the
# French-only tier of the table above comes into play.
SHARED_INVENTORY: frozenset[str] = INVENTORY["en"] | INVENTORY["fr"]


def colour_blocks(text: str, spoken: str, accent_of: str,
                  strength: float = 0.5,
                  can_say: frozenset[str] | None = None) -> list[str]:
    """Phonemes for ``text`` in ``spoken``, coloured by an ``accent_of`` mouth.

    The counterpart to :func:`phoneme_blocks`. That one changes the voice and
    keeps the words; this one keeps the voice and colours the words, so the
    model stays inside the language it was trained on and only the sounds a
    foreign speaker would miss are moved.
    """
    inventory = can_say or INVENTORY.get(spoken, INVENTORY["en"])
    # A substitution the voice cannot pronounce is worse than no substitution:
    # the phoneme is dropped on the floor and the word loses a sound. So the
    # table is filtered by what this voice can say before it is applied, and a
    # Piper English model simply never sees the French-only tier.
    table = {
        k: v for k, (v, at) in _COLOUR.get(accent_of, {}).items()
        if strength >= at and all(
            c in inventory or c in _TRAILING or unicodedata.combining(c)
            for c in v)
    }
    voice = source_voice(spoken, spoken)
    blocks: list[str] = []
    for sentence in _phonemizer().phonemize(voice, respell(text, spoken)):
        out: list[Token] = []
        for token in tokenize(sentence):
            if token.kind != "phoneme":
                out.append(token)
                continue
            unit = token.text
            replacement = table.get(unit, table.get(_base(unit)))
            if replacement is None:
                out.append(token)
                continue
            if not replacement:
                continue
            for produced in tokenize(replacement):
                if all(c in inventory or c in _TRAILING
                       or unicodedata.combining(c) for c in produced.text):
                    out.append(produced)
        out = _open_final_vowels(out, accent_of, strength)
        out = _unreduce(out, accent_of, strength)
        out = relay_stress(out, accent_of, strength)
        body = _render(out)
        if body:
            blocks.append(f"[[{body}]]")
    return blocks


__all__ = ["phoneme_blocks", "colour_blocks", "tokenize", "substitute", "relay_stress",
           "apply_lexicon", "word_key", "respell", "source_voice", "describe",
           "INVENTORY", "LEXICON", "RESPELL", "Token"]
