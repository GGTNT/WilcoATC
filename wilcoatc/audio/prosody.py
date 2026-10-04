"""Where a controller breathes, and how fast they are talking.

A synthesiser has two levers and no more: punctuation and rate. Everything a
listener hears as "delivery" -- the drop at the end of a clearance, the beat
after the callsign, the fact that "roger" comes out quicker than a full route
clearance -- has to be spelled with those two. This module is the layer that
spells it.

The problem it solves is that every transmission this program composes is one
sentence. :func:`wilcoatc.atc.phraseology._join` strings the clauses together
with commas and puts a single full stop on the end, so a fourteen-word taxi
instruction and a three-word acknowledgement arrive at the engine in the same
shape and at the same rate. What comes back is even, level and slightly
hurried, and two transmissions in a row sound like the same block of text with
different words in it.

**This layer never invents a boundary.** It only promotes ones the phrasebook
already put there. That distinction is the whole design, and it is not
aesthetic: ``tts.prepare_text`` used to insert commas ahead of instruction
words and had to be reverted, because the instruction words also occur inside
fixed phrases and it was cutting them in half -- "radar, contact", "hold short
of, runway 06". Promoting an existing comma to a full stop cannot do that,
because the phrasebook does not put a comma inside a fixed phrase.

Three promotions, in the order they matter:

*The address gets its own sentence.* "Delta four fifty six, Kennedy Ground,
runway two two right, ..." is read as one long list. A controller says the
callsign, lets go of it, and then starts the instruction. A full stop is the
only way to say that to an engine: it buys the pause and the falling pitch
together, and the falling pitch is the half that matters, because a comma
leaves the callsign hanging on a rise as though more of it were coming.

*The operative clause gets its own sentence.* "Cleared for takeoff", "line up
and wait", "hold short of runway three one left" -- the thing the transmission
exists to say lands last and lands down. Behind a comma it is the tail of a
list. Behind a full stop it is an instruction.

*The rate follows the length.* Short transmissions are quicker in real life,
because there is nothing in them to get wrong; long ones are more measured,
because the controller knows the pilot is writing. A little jitter on top
stops every transmission of the same length arriving at exactly the same
tempo, which is audible over a session even when a single transmission is
fine.

The rate is returned rather than applied: it is a multiplier on the
``length_scale`` the casting already computed, in Piper's units, where larger
is slower. Everything here is deterministic given the text and a seed, because
:class:`wilcoatc.audio.tts.Synthesizer` caches rendered transmissions and a
cache that returns a different reading each time is not a cache.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, fields

#: Clause boundaries as the phrasebook writes them. A comma, and nothing else:
#: see the module docstring for why this does not look inside a clause.
_CLAUSE = re.compile(r"\s*,\s*")

#: What a transmission may already end with.
_TERMINAL = ".?!"


@dataclass(frozen=True)
class Prosody:
    """How much shaping to do. Every field is off-switchable; so is the lot.

    The thresholds are in clauses and words rather than in seconds because
    that is what this layer can see. A transmission's duration depends on the
    voice, the rate and the engine, none of which are known here.
    """

    #: The global switch. Off means :func:`punctuate` returns what it was
    #: given and :func:`pace` returns 1.0, which is the behaviour this
    #: program had before the layer existed -- so a comparison is one setting.
    enabled: bool = True

    # --- where the sentences go ---
    #: Give the address -- the callsign, and the station callup when the
    #: transmission opens with one -- its own falling sentence.
    address: bool = True
    #: Give the last clause its own falling sentence.
    closing: bool = True
    #: Under this many clauses there is no boundary to promote. Two is the
    #: floor rather than three because the two-clause transmission -- a
    #: callsign and one instruction -- is the commonest thing on the
    #: frequency, and it is exactly the case the address break is for.
    min_clauses: int = 2
    #: How much has to be left between the address and the closing clause
    #: before the closing break is worth taking. One clause in the middle
    #: gives three sentences out of twelve words, which is not a controller
    #: landing an instruction, it is a controller reading a list.
    min_body_clauses: int = 2
    #: Never strand a one-word sentence on the end. "Roger." on its own after
    #: a full stop reads as an afterthought rather than as a close.
    min_closing_words: int = 2

    # --- how fast ---
    #: Vary the rate with the length of the transmission.
    pace: bool = True
    #: At or below this many words the transmission is read at ``quick``.
    short_words: int = 7
    #: At or above this many words it is read at ``measured``.
    long_words: int = 28
    #: Rate multipliers, in Piper's ``length_scale`` units: larger is slower.
    #: The spread is deliberately small. Past about eight per cent either way
    #: the difference stops reading as a controller in a hurry and starts
    #: reading as a tape at the wrong speed.
    quick: float = 0.94
    measured: float = 1.07
    #: Per-transmission wobble, as a fraction. Enough that two clearances of
    #: the same length are not metronomic, small enough that a readback of the
    #: same clearance is still recognisably the same delivery.
    jitter: float = 0.025


#: The settings in force when nobody says otherwise.
DEFAULT = Prosody()

#: The shaping turned off, for the A/B the module docstring describes.
OFF = Prosody(enabled=False)


def from_config(section) -> Prosody:
    """The settings, read off ``config.delivery.prosody``.

    By field name rather than positionally, and skipping anything the config
    section does not have, so that the two dataclasses can be extended one at
    a time. Takes a duck-typed object rather than importing the config, which
    would make the audio package depend on it for one attribute lookup.
    """
    if section is None:
        return DEFAULT
    values = {field.name: getattr(section, field.name)
              for field in fields(Prosody) if hasattr(section, field.name)}
    return Prosody(**values)


# --------------------------------------------------------------------------
# the address
# --------------------------------------------------------------------------


def _station_words() -> frozenset[str]:
    """The last word of every position callup, in every language spoken here.

    Read off the navigation database rather than written out again, because
    that is where the callups are composed and a second copy of them would
    drift. Only the final word is kept: "Kennedy Clearance Delivery" and
    "Orly Prévol" both end in the word that says what the position is, and
    the field name in front of it is whatever the airport is called.

    Imported inside the function, the way ``radio_fx`` reaches for the same
    module: this one is on the synthesis path and the navigation database is
    a large import to pay for at module load.
    """
    global _STATION_CACHE
    if _STATION_CACHE is None:
        from ..navdata.db import (POSITION_SUFFIX, POSITION_SUFFIX_BY_LANGUAGE,
                                  POSITION_SUFFIX_ICAO, POSITION_SUFFIX_SHORT)

        tables = [POSITION_SUFFIX, POSITION_SUFFIX_SHORT, POSITION_SUFFIX_ICAO,
                  *POSITION_SUFFIX_BY_LANGUAGE.values()]
        _STATION_CACHE = frozenset(
            name.split()[-1].lower()
            for table in tables for name in table.values() if name.split()
        )
    return _STATION_CACHE


_STATION_CACHE: frozenset[str] | None = None


def _is_station(clause: str) -> bool:
    """Whether this clause is the controller naming themselves.

    Asked of the second clause only. The first is the aircraft -- every
    template in the phrasebook opens with :meth:`Phraseology.addressed` -- and
    the second is either the station callup or the first instruction. That
    narrowness is what makes a word test safe: "contact Kennedy Tower" also
    ends in a position word, and it is never the second clause of a
    transmission that opens with a callsign.
    """
    words = clause.split()
    return bool(words) and words[-1].strip(".,").lower() in _station_words()


# --------------------------------------------------------------------------
# the layer
# --------------------------------------------------------------------------


def punctuate(text: str, language: str = "en",
              settings: Prosody = DEFAULT) -> str:
    """Raw transmission text in, punctuated transmission text out.

    Pure, and deliberately so: this is the whole of the shaping, it takes no
    voice, no engine and no clock, and a test can hold one sentence against
    another. ``language`` is accepted because the station callups differ by
    it, though the lookup is over every language at once -- a French
    controller answering in English says "Orly Tower", not "Orly Tour", and
    both spellings have to be recognised whichever language is nominally
    being spoken.
    """
    if not settings.enabled or not (settings.address or settings.closing):
        return text

    body = (text or "").strip()
    if not body:
        return text

    end = ""
    if body[-1] in _TERMINAL:
        end, body = body[-1], body[:-1].rstrip()

    # Anything that already has sentences in it has been shaped by whoever
    # wrote it -- an ATIS broadcast, a cabin announcement, a pilot's own words
    # played back. Two layers of shaping is one layer too many.
    if any(mark in body for mark in _TERMINAL):
        return text

    clauses = [clause for clause in _CLAUSE.split(body) if clause.strip()]
    if len(clauses) < settings.min_clauses:
        return text

    # --- the address -----------------------------------------------------
    head = 0
    if settings.address:
        head = 1
        if len(clauses) > 2 and _is_station(clauses[1]):
            head = 2
        # An address with nothing after it is not an address, it is the whole
        # transmission -- a callup, or a station identifying itself.
        if len(clauses) - head < 1:
            head = 0

    # --- the operative clause --------------------------------------------
    tail = 0
    if settings.closing:
        last = clauses[-1]
        if (len(clauses) - head - 1 >= settings.min_body_clauses
                and len(last.split()) >= settings.min_closing_words):
            tail = 1

    if not head and not tail:
        return text

    groups = []
    if head:
        groups.append(clauses[:head])
    middle = clauses[head:len(clauses) - tail]
    if middle:
        groups.append(middle)
    if tail:
        groups.append(clauses[len(clauses) - tail:])

    sentences = [", ".join(group) for group in groups]
    # The transmission keeps the terminator it arrived with; the breaks this
    # layer adds are full stops, because a full stop is what an engine reads
    # as "pause, and take the pitch down".
    return ". ".join(sentences) + (end or ".")


def pace(text: str, settings: Prosody = DEFAULT, seed: int = 0) -> float:
    """A multiplier on ``length_scale``: below 1.0 is quicker, above is slower.

    Deterministic given ``text`` and ``seed``. The caller passes the same seed
    it gives the radio chain, which is derived from the transmission's cache
    key -- so a transmission that is a cache hit is the same reading, and two
    different clearances of the same length are not.
    """
    if not settings.enabled or not settings.pace:
        return 1.0

    words = len(text.split())
    span = max(1, settings.long_words - settings.short_words)
    across = min(1.0, max(0.0, (words - settings.short_words) / span))
    scale = settings.quick + (settings.measured - settings.quick) * across

    if settings.jitter > 0.0:
        wobble = random.Random(seed).uniform(-settings.jitter, settings.jitter)
        scale *= 1.0 + wobble
    return scale


def shape(text: str, language: str = "en", settings: Prosody = DEFAULT,
          seed: int = 0) -> tuple[str, float]:
    """Both halves at once: the punctuated text and the rate it is read at.

    The rate is measured on the punctuated text rather than on the raw text,
    which is the same word count -- this layer adds no words -- and is the
    version that will actually be spoken.
    """
    spoken = punctuate(text, language, settings)
    return spoken, pace(spoken, settings, seed)


__all__ = ["DEFAULT", "OFF", "Prosody", "from_config", "pace",
           "punctuate", "shape"]
