# -*- coding: utf-8 -*-
"""How the people in the aeroplane sound: less like a file, more like a crew.

The flight deck and the cabin were rendered almost clean. The interphone
profile is 180 Hz to 7 kHz with barely any drive, which is a studio headset
rather than an aircraft's audio panel, and the imperfections layer gives the
crew only its human half (:meth:`~wilcoatc.audio.imperfections.Imperfections
.indoors`) -- whose one filler waits for a comma, which a callout does not
have. So the crew came out smooth: every line even, every word where the
script put it.

Two things here, both switchable with ``immersion.realism``.

The equipment
-------------

Nobody in an aeroplane hears anybody else without an amplifier in between.
The first officer arrives through the audio panel and a headset: narrower
than a hi-fi, a little driven, with the hiss of a live boom microphone under
it. The cabin address is a small ceiling speaker heard through the flight
deck door. Both are *copies* of the profiles in
:mod:`wilcoatc.audio.radio_fx`, made the way :mod:`wilcoatc.audio.tone` makes
its copies -- nothing in that module is edited -- and both stay well short
of the VHF channel: there is no squelch, no carrier and no key click,
because there is no transmitter. A first officer who arrives with a squelch
burst gets filed by the ear as a second controller.

The person
----------

"Uh", "um" and "er", where people actually say them. The rule is the one the
prosody and imperfections layers already keep: *a filler never invents a
boundary*. It goes at the start of the line, after a full stop, or at a
comma the script already has, and nowhere else -- a filler dropped between
two words would eventually land inside "flaps one" and turn a callout into a
stammer.

How often depends on who is talking, because they are not doing the same
thing:

* A captain on the address system is the one everybody has heard "uhh" from.
  Thinking aloud, unscripted, and in no hurry.
* A purser reads a script and has read it a thousand times; the odd stumble
  between paragraphs and not much else.
* A first officer's *callouts* are never hesitant. "Positive rate" with an
  "um" in front of it is not a person, it is a joke. Their conversational
  lines are allowed one.
* Anybody speaking their second language -- the English repeat of a French
  announcement -- stumbles a bit more than they do in their own.

Deterministic given the seed, like everything else in the delivery layers:
the crew's renders are cached, and a filler rolled fresh on each call would
be frozen at its first roll.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace

from .radio_fx import PROFILE_CABIN, PROFILE_INTERCOM

# The audio panel and a headset. Narrowed rather than squashed: the top comes
# down from 7 kHz to about 4.5, which is where an aviation headset's own
# response falls away, and the bottom comes up so the voice loses its chest.
# A little more drive and a louder hiss is the live boom microphone and the
# amplifier behind it.
CREW_INTERCOM = replace(
    PROFILE_INTERCOM,
    band_low=300.0,
    band_high=4600.0,
    band_order=4,
    drive=1.6,
    clip_softness=0.9,
    comp_ratio=4.5,
    makeup_db=6.0,
    speaker_peak_hz=2100.0,
    speaker_peak_db=3.5,
    speaker_q=1.0,
    noise_db=-40.0,
    room_db=-40.0,
)

# The cabin speaker, heard from behind the flight deck door: the door takes a
# little more of the top off and the cabin is a little louder around it.
CREW_CABIN = replace(
    PROFILE_CABIN,
    band_low=300.0,
    band_high=3800.0,
    drive=1.95,
    room_db=-24.0,
    speaker_peak_db=7.0,
)

#: What people actually say when they stall, per language. The first entry is
#: the commonest and is weighted accordingly.
FILLER_WORDS: dict[str, tuple[str, ...]] = {
    "en": ("uh", "um", "uh", "er"),
    "fr": ("euh", "euh", "hum"),
    "de": ("äh", "ähm", "äh"),
    "es": ("eh", "este"),
    "it": ("ehm", "eh"),
    "pt": ("é", "hum"),
    "nl": ("eh", "uhm"),
}


@dataclass(frozen=True)
class Manner:
    """How readily one person stalls.

    Each chance is per opportunity: the start of the line, each sentence
    break, each comma. ``most`` is the ceiling for one line, because three
    fillers in one sentence is somebody who should not be on the address
    system.
    """

    head: float = 0.0
    sentence: float = 0.0
    comma: float = 0.0
    most: int = 1
    #: Lines shorter than this many words are callouts and are left alone.
    shortest: int = 6

    def scaled(self, factor: float) -> "Manner":
        return replace(self, head=min(1.0, self.head * factor),
                       sentence=min(1.0, self.sentence * factor),
                       comma=min(1.0, self.comma * factor))


#: Nobody hesitates.
STEADY = Manner()

#: The captain, thinking aloud to a hundred and fifty people.
CAPTAIN_PA = Manner(head=0.30, sentence=0.20, comma=0.08, most=2, shortest=5)

#: The purser, who has read this a thousand times.
PURSER_PA = Manner(head=0.06, sentence=0.14, comma=0.05, most=2, shortest=8)

#: The first officer, in conversation rather than calling something out.
FLIGHT_DECK = Manner(head=0.14, sentence=0.12, comma=0.12, most=1, shortest=6)

#: Somebody on the ramp with a headset plugged into the nose.
RAMP = Manner(head=0.22, sentence=0.10, comma=0.12, most=1, shortest=5)

#: How much more often anybody stalls in their second language.
SECOND_LANGUAGE = 1.3


def filler(language: str, rng: random.Random) -> str:
    words = FILLER_WORDS.get((language or "en").lower()[:2], FILLER_WORDS["en"])
    return rng.choice(words)


def falter(text: str, language: str, manner: Manner,
           rng: random.Random) -> str:
    """The line with the odd "uh" in it, at boundaries it already had.

    Returns the text unchanged when nothing fires, which is most of the time
    for most people.
    """
    body = (text or "").strip()
    if not body or manner is STEADY or len(body.split()) < manner.shortest:
        return text

    # Every place a filler may go, as (index, kind). Found before anything
    # is inserted, so an insertion cannot create an opportunity for another.
    spots: list[tuple[int, str]] = []
    for n, mark in enumerate(body):
        if n + 2 >= len(body):
            break
        if mark in ".!?" and body[n + 1] == " ":
            spots.append((n + 2, "sentence"))
        elif mark == "," and body[n + 1] == " ":
            spots.append((n + 2, "comma"))

    chosen: list[int] = []
    head = rng.random() < manner.head
    for at, kind in spots:
        chance = manner.sentence if kind == "sentence" else manner.comma
        if rng.random() < chance:
            chosen.append(at)
    budget = manner.most - (1 if head else 0)
    if budget < len(chosen):
        chosen = sorted(rng.sample(chosen, max(0, budget)))
    if not head and not chosen:
        return text

    # Only the synthesiser reads this; the transcript keeps the line as
    # written. So the case of the word after a filler is left alone -- the
    # engine does not read capitals -- and the filler only takes one itself
    # where it starts a sentence.
    out = body
    # From the back, so the earlier indices still point where they did.
    for at in reversed(chosen):
        word = filler(language, rng)
        if out[at - 2:at - 1] in ".!?":
            word = word[:1].upper() + word[1:]
        out = f"{out[:at]}{word}, {out[at:]}"
    if head:
        word = filler(language, rng)
        out = f"{word[:1].upper()}{word[1:]}, {out}"
    return out


__all__ = ["CAPTAIN_PA", "CREW_CABIN", "CREW_INTERCOM", "FILLER_WORDS",
           "FLIGHT_DECK", "Manner", "PURSER_PA", "RAMP", "SECOND_LANGUAGE",
           "STEADY", "falter", "filler"]
