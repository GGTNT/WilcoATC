"""The things a person does to a radio that a file being played does not.

:mod:`wilcoatc.audio.radio_fx` is the channel: the transmitter, the path and
the receiver, measured against a recording and not to be disturbed. This is
the layer above it -- the *operator* rather than the equipment. It changes
nothing in that module. It supplies the numbers the channel already takes as
arguments, mixes into the voice before the channel sees it, and puts one word
in the text before the engine sees it. Turn it off and the chain runs exactly
as it ran before this file existed.

Six effects, and the reason each is here rather than in the chain:

*The controller presses the bar, then speaks.* The carrier is already up
before the first word -- ``squelch_open_ms`` sees to that -- but it is up for
the same 150 ms every time, so every transmission starts on the same beat.
People do not. This adds a jittered gap between the carrier and the voice, as
plain silence at the head of the waveform, so the room tone and the noise
floor fill it the way they fill any other pause.

*Sometimes they let go on the last syllable.* Already in the chain, already
measured -- see ``_key_held`` -- and previously not adjustable without editing
it. This sets how often and how deep, and a profile that has the effect turned
off stays turned off: ATIS is a recording and a recording does not fumble a
transmit bar.

*Occasionally they lose the thread.* An "uh" has to be synthesised, so it is
a word added to the text before the engine is called, and it lives here
rather than in :mod:`wilcoatc.audio.prosody` because it is the same feature
and the same switch as the rest. Two places it can go: at the front, from
somebody who keyed up before they had decided what to say, and part way
through, from somebody who has lost their place in a long clearance. The
second is the commoner of the two on a real frequency and the less
noticeable of the two here. Both stay rare: a controller who says "uh" once
every few transmissions is a person, and one who says it every third is a
bit.

The part-way one used to be ``, uh,`` read inside the sentence, and it did
not sound like anybody. Measured on a recorded IFR clearance exchange
(``clearance to, uh, Salt Lake City``), a real stall has a shape the
synthesiser will not produce from punctuation: the word in front is drawn
out, the "uh" itself lasts about 0.3 s, and then the voice stops -- 0.65 s of
nothing there -- before the name comes. An ellipsis or a dash buys 0.1-0.4 s
of pause from Supertonic, and Kokoro swallows the "uh" behind an ellipsis
outright. So the text is marked with :data:`STALL` after the filler and the
synthesiser renders the two halves separately: the first half ends on the
"uh", which gets the phrase-final lengthening of the word before it for
free, and :func:`join` puts real silence between the halves. The same
recording says *where*: right before the thing being remembered -- a name,
a number -- as well as at a comma. "Um" and "er" are gone: nobody in the
recording says them, and read by a synthesiser they are the words, not the
sound.

*They breathe, and not only before the first word.* The chain has a breath --
``_breath``, at ``breath_db``, roughly one transmission in three -- and it is
at the head, ahead of the callsign, because that is where the chain can put
one without knowing anything about the speech. A person breathes wherever the
sentence lets them, which on a route clearance is two or three times. This
finds the pauses the synthesiser actually left, and puts an inhale in one or
two of them. It *mixes* rather than inserts: a breath that lengthened the
waveform would push the words apart, and the pauses are the prosody layer's
work, not ours to widen.

*The window behind them is open.* ``room_db`` is the ops room, and it is
static -- a fixed bed under the whole transmission. What a tower cab or a
ramp actually has over it is weather, which arrives in gusts a second or two
long. This lays a slow, low-passed bed under the voice before the chain, so
the band limiting, the compressor and the AGC treat it as part of what the
microphone heard rather than as something painted on afterwards. It is the
reason the level dips under a gust instead of the gust simply being louder.

*No two transmissions arrive at the same level.* Level jitter is in the chain
already. What is not is variation in the *carrier* between transmissions from
the same station -- how hard the key clicks, how long the tail runs. The chain
takes that as its ``keying`` argument and
:func:`wilcoatc.audio.radio_fx.station_keying` supplies a value that is stable
per station, which is right, and which means one station's key-up is
identical every single time. This wobbles it a little without moving the
station off its own character.

*And the voice is never quite steady.* A synthesiser holds its pitch register
and its level for the whole transmission; a person drifts on both, a little,
over a second or two, and the ear reads the absence of that drift as "file"
long before it can say why. Measured on an hour of Heathrow Director, the
fundamental wanders by one to two per cent within a clause and the level by a
decibel or so, at well under one hertz. This lays that wander over the voice
before the chain: a slow contour on the playback rate, which moves pitch and
timing together the way a person's do, and another on the gain. Both are far
below what reads as tempo or as fading. Kokoro's own micro-variation stays
underneath it; what this adds is the slower layer that a model reading one
sentence at a time has no reason to produce.

Not all seven belong to everybody. Three of them are the radio -- the gap
after keying up, the fumbled release, the weather outside a tower window --
and the first officer and the cabin crew have none of those, because they are
people in an aeroplane rather than voices on a frequency. They do breathe,
they do lose their place, and their voices wander like anyone's.
:meth:`Imperfections.indoors` is that distinction, and it is the reason the
flight deck no longer arrives as a perfectly even file being played.

Everything is deterministic given the seed the caller passes, which is derived
from the transmission's cache key. That is not incidental: rendered
transmissions are cached, and an effect rolled fresh on every call would be
frozen at whatever it rolled the first time and then contradicted by the
setting for the rest of the session.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, fields, replace

import numpy as np

from .radio_fx import bandpass, lowpass

#: What a controller says when they have keyed up before they were ready, in
#: each language a controller works in here. Written out rather than derived,
#: because a filler is not a translation of "uh" -- it is whatever that
#: language's speakers actually make when they stall.
FILLERS: dict[str, str] = {
    "en": "uh",
    "fr": "euh",
    "de": "äh",
    "es": "eh",
    "it": "eh",
    "pt": "eh",
    "nl": "eh",
}

#: Where the voice stops after a filler. Never reaches an engine: the
#: synthesiser splits the text on it and :func:`join` puts the silence back.
#: A private-use character because anything printable could be in a script,
#: and the ASCII separators count as whitespace and are collapsed by
#: ``prepare_text``.
STALL = ""

#: The words a value can start with -- a number, read the way the phrasebook
#: spells it -- per language. This is where somebody stalls who is reading it
#: off a strip: "squawk, uh, four seven three one". A missing word only means
#: one place fewer to stall, so the tables are the common words and not the
#: whole of each language's counting.
VALUE_WORDS: dict[str, frozenset[str]] = {
    "en": frozenset(
        "zero one two three four five six seven eight nine niner tree fife "
        "ten eleven twelve thirteen fourteen fifteen sixteen seventeen "
        "eighteen nineteen twenty thirty forty fifty sixty seventy eighty "
        "ninety hundred thousand".split()),
    "fr": frozenset(
        "zéro un une deux trois quatre cinq six sept huit neuf dix onze "
        "douze treize quatorze quinze seize vingt trente quarante cinquante "
        "soixante cent mille".split()),
    "de": frozenset(
        "null eins ein zwei zwo drei vier fünf sechs sieben acht neun zehn "
        "elf zwölf zwanzig dreißig vierzig fünfzig hundert tausend".split()),
    "es": frozenset(
        "cero uno una dos tres cuatro cinco seis siete ocho nueve diez "
        "veinte treinta cuarenta cincuenta cien mil".split()),
    "it": frozenset(
        "zero uno una due tre quattro cinque sei sette otto nove dieci venti "
        "trenta quaranta cinquanta cento mille".split()),
    "pt": frozenset(
        "zero um uma dois duas três quatro cinco seis sete oito nove dez "
        "vinte trinta quarenta cinquenta cem mil".split()),
    "nl": frozenset(
        "nul een twee drie vier vijf zes zeven acht negen tien twintig "
        "dertig veertig vijftig honderd duizend".split()),
}

#: The words *inside* a number, after which a stall would cut it in half:
#: "one two six decimal, uh, seven" is not somebody remembering a frequency.
_INSIDE_VALUE = frozenset(
    "decimal point komma virgule décimal decimale decimaal".split())


def _bare(word: str) -> str:
    return word.strip(".,!?;:").lower()


def _is_value(word: str, language: str) -> bool:
    bare = _bare(word)
    if not bare:
        return False
    if bare[0].isdigit():
        return True
    table = VALUE_WORDS.get((language or "en").lower()[:2], VALUE_WORDS["en"])
    # French and Spanish hyphenate inside a number: "vingt-six", "dix-sept".
    return bare in table or bare.split("-")[0] in table


def value_starts(text: str, language: str = "en", after: int = -1) -> list[int]:
    """Where a number starts in the text, past ``after``, as indices.

    Only the first word of a number, and only after a word that is not
    already part of one: a stall goes in front of the value, never inside
    it, and never straight after a comma, which is a place of its own.
    """
    starts: list[int] = []
    previous = ""
    for match in re.finditer(r"\S+", text):
        word = match.group(0)
        if match.start() > after and previous \
                and not previous.endswith((",", ".", "!", "?", ";", ":")) \
                and _is_value(word, language) \
                and not _is_value(previous, language) \
                and _bare(previous) not in _INSIDE_VALUE:
            starts.append(match.start())
        previous = word
    return starts


@dataclass(frozen=True)
class Imperfections:
    """How much of the operator to put on the air.

    The settings that name a probability are probabilities, in the same units
    the radio profile uses. ``level`` is the exception and is a multiplier,
    because the profiles disagree about level on purpose -- a cockpit
    intercom and an ATIS tape are not both 3.5 dB of transmitter-to-
    transmitter spread -- and a single number here would flatten them into one
    another. The two that name a dB are absolute, under the voice's own peak,
    because a breath and a gust are the same loudness whoever is transmitting.
    """

    #: The global switch. Off means the chain is called with exactly the
    #: arguments it was called with before this module existed.
    enabled: bool = True

    # --- the controller presses the bar, then speaks ---
    #: Silence between the carrier coming up and the first word, on top of
    #: whatever the profile's ``squelch_open_ms`` already puts there.
    lead_in_ms: float = 60.0
    #: How much that varies per transmission, either side of the above. The
    #: total gap is what a listener hears as somebody gathering themselves,
    #: and it being different every time is the whole point of it.
    lead_in_jitter_ms: float = 55.0

    # --- and sometimes let go too early ---
    #: How often the key is released on the last syllable. The chain's own
    #: default, restated here so that it can be lowered, raised or zeroed
    #: without editing a module that was measured. A profile that sets its own
    #: chance to zero is left at zero: see :func:`tune`.
    clip_chance: float = 0.18
    #: How much of the tail that costs. Measured at 45 ms, which lands inside
    #: the silence after the last word rather than on the vowel; raising it
    #: starts costing the recogniser words.
    clip_ms: float = 45.0

    # --- occasionally they lose the thread ---
    #: How often a transmission opens with "uh": they keyed up before they had
    #: worked out what they were going to say. Kept well below every other
    #: probability here, because it is the most noticeable thing in the file.
    hesitation_chance: float = 0.05
    #: How often they stall part way through instead, at a comma the
    #: phrasebook already put there. Separate from the one above because it is
    #: a different thing happening -- not "I was not ready" but "I have lost
    #: my place" -- and because it is the one that reads as somebody thinking
    #: rather than as somebody being funny.
    mid_hesitation_chance: float = 0.06
    #: The longest the voice stops after that "uh", in ms; each stall draws
    #: between a seventh of this and all of it. 650 is the longer of the two
    #: in the recorded clearance exchange -- the other ran straight on, which
    #: is the short end. Zero runs every stall straight on.
    stall_ms: float = 650.0

    # --- and the phrase is not one note ---
    #: See :mod:`wilcoatc.audio.intonation`. How far each phrase's pitch is
    #: moved from the engine's, as a standard deviation in semitones. The
    #: controller in the recorded clearance moves 3.3 between phrases and the
    #: cloned voices 0.4-0.7; the engine's own movement adds to this.
    phrase_pitch_st: float = 1.3
    #: How far the address -- the first phrase, the callsign -- is lifted, in
    #: semitones, at most; each transmission draws 70-100% of it. The
    #: controller in the recording says the callsign 6.6 over his median.
    address_lift_st: float = 3.5
    #: How much each instruction phrase's pace varies, as a standard
    #: deviation on a speed of 1. His run from 4.9 to 8.9 syllables a second.
    phrase_rate: float = 0.07
    #: The instructions' pace against the engine's. A little quick: a
    #: controller says "climb and maintain" as one word. Measured cost below
    #: in the README; quicker than this starts costing the recogniser words.
    instruction_pace: float = 1.04

    # --- and they stop for air ---
    #: After about this much speech without a pause long enough to breathe
    #: in, the next pause is lengthened and an inhale goes into it: the
    #: pilot in the recorded clearance stops for 0.62 s after six seconds of
    #: reading back and takes a 0.35 s breath in it. Each run draws 80-125%
    #: of this. Zero turns it off.
    breath_group_s: float = 5.5
    #: That inhale's level against the speech's own (the loud frames, not the
    #: peak sample): -23 dB, as measured in the recording.
    catch_breath_db: float = -23.0

    # --- and they breathe between the clauses ---
    #: How often an eligible pause gets an audible inhale. Eligible means a
    #: pause the synthesiser already left, of at least ``breath_pause_ms``,
    #: with speech on both sides of it. A short acknowledgement has none, so
    #: this is the chance per *opportunity*, not per transmission.
    breath_chance: float = 0.5
    #: Level under the voice's own peak. Measured by ear against the chain's
    #: head breath, which is -30 dBFS against a normalised voice: a breath
    #: mid-sentence is nearer the microphone than that one and the compressor
    #: has already wound up for speech, so it needs less.
    breath_db: float = -26.0
    #: At most this many in one transmission. Two is a long clearance read by
    #: somebody who needed air twice; three is somebody who should sit down.
    breath_max: int = 2
    #: The shortest pause worth breathing into. Below this the breath is
    #: audible *over* the words either side rather than between them.
    breath_pause_ms: float = 170.0

    # --- and the weather is behind them ---
    #: Level of the wind bed under the voice's own peak, before the chain.
    #: Under the profile's own ``room_db`` on purpose -- this is weather
    #: through a window, not a headset in a gale. -90 turns it off.
    #:
    #: It was -42, which stopped being under the room when the room came
    #: down. :mod:`wilcoatc.audio.tone` now takes ``room_db`` to -44 to match
    #: what the Heathrow recordings put between the words, and at -42 the
    #: weather was the loudest thing in the gaps -- louder than the radio,
    #: which is the one place it was never supposed to be. Two decibels under
    #: the room again puts the measured floor at -20-ish, inside the 18-25 dB
    #: band ``radio_fx`` aims for and where the recordings sit. Taking it to
    #: -50 was measured too: it buys another decibel and a half of floor and
    #: costs half the weather, which is not a trade worth making for a number
    #: already inside the band.
    wind_db: float = -46.0
    #: How much the bed gusts, 0 for a flat bed and 1 for the level halving
    #: and doubling every second or two. The gusting is the part the ear
    #: identifies as weather; a steady bed just reads as a worse radio.
    wind_gust: float = 0.6

    # --- and the voice is never quite steady ---
    #: How far the pitch drifts either side of where the engine put it, as a
    #: percentage. One and a half per cent is a quarter of a semitone at the
    #: extremes and the drift is slow, so it never reads as vibrato or as a
    #: tape; at four it starts to. Zero turns the drift off.
    wander_pitch: float = 1.5
    #: How far the level drifts either side of the engine's, in dB. Zero
    #: turns it off.
    wander_level_db: float = 1.2
    #: How quickly both wander, as a corner frequency in hertz. Under one:
    #: this is a person's breath support shifting over a clause, not a
    #: tremolo.
    wander_hz: float = 0.7

    # --- and no two arrive quite the same ---
    #: Multiplier on the profile's own transmission-to-transmission level
    #: spread. 1.0 is the chain's measured behaviour.
    level: float = 1.0
    #: How far the station's key-up character wanders per transmission, in the
    #: chain's own ``keying`` units, where the whole scale is -1 to 1. Small:
    #: this is the same transmitter warming differently, not a different one.
    carrier: float = 0.25

    def indoors(self) -> "Imperfections":
        """The same person, with no transmit bar in their hand.

        The first officer and the cabin crew are people and are not on a
        frequency. Half of this layer is about the radio -- the gap after
        keying up, the fumbled release, the carrier wobble, the weather
        outside the window of a tower cab -- and none of that happens on an
        interphone or a cabin address system.

        The other half is about the person, and that half stays: they breathe
        between the clauses of a long announcement, and they lose their place
        in one. The opening filler goes with the radio half rather than the
        human one, because it exists to cover the moment between keying up
        and knowing what to say -- and "uh, positive rate" is not a callout,
        it is a joke.

        ``enabled`` survives untouched, so switching the layer off switches
        this off with it.
        """
        return replace(self, lead_in_ms=0.0, lead_in_jitter_ms=0.0,
                       clip_chance=0.0, carrier=0.0, wind_db=-90.0,
                       hesitation_chance=0.0, address_lift_st=0.0)


#: The settings in force when nobody says otherwise.
DEFAULT = Imperfections()

#: Everything off, for the comparison.
OFF = Imperfections(enabled=False)


def from_config(section) -> Imperfections:
    """The settings, read off ``config.delivery.imperfections``.

    Same shape and same reasoning as
    :func:`wilcoatc.audio.prosody.from_config`: by field name, tolerant of a
    config section that is missing one, and not importing the config module.
    """
    if section is None:
        return DEFAULT
    values = {field.name: getattr(section, field.name)
              for field in fields(Imperfections) if hasattr(section, field.name)}
    return Imperfections(**values)


def rng_for(seed: int, salt: int = 0) -> random.Random:
    """The generator for one transmission's imperfections.

    Separated from the chain's own generator by a salt so that the two do not
    walk the same sequence -- the chain draws for its noise, its breath and
    its relay tick from the same seed, and an effect here that happened to
    line up with one of those would correlate with it forever.
    """
    return random.Random((int(seed) ^ (0x9E3779B9 + salt)) & 0xFFFFFFFF)


# --------------------------------------------------------------------------
# text, before the engine
# --------------------------------------------------------------------------


def hesitate(text: str, language: str = "en",
             settings: Imperfections = DEFAULT,
             rng: random.Random | None = None) -> str:
    """Occasionally put a filler in, at the front or part way through.

    At the front is the controller who keyed up before they had decided what
    to say. It goes ahead of the callsign, because that is where it happens;
    behind the callsign it would read as hesitating over the aeroplane's
    name, which is a different and much less flattering thing.

    Part way through is the controller who has lost their place in a long
    one, and it is the more common of the two on a real frequency. It goes at
    a comma that is already in the text, or right in front of a value --
    "squawk, uh, four seven three one", which is where the recorded
    clearance stalls -- and nowhere else: a filler dropped between any two
    words would eventually land inside "hold short of runway", and a fixed
    phrase cut in half is a clearance nobody can read back. It is followed
    by :data:`STALL`, so the voice stops after it (see :func:`join`).

    Both are applied *after* :func:`wilcoatc.audio.prosody.punctuate`, so the
    filler cannot become the address the prosody layer breaks after -- and,
    because that layer has already turned the address comma into a full stop,
    the commas left to choose from are inside the instruction, which is where
    somebody actually stalls.
    """
    if not settings.enabled:
        return text
    body = (text or "").strip()
    if not body:
        return text
    rng = rng or random.Random()
    filler = FILLERS.get((language or "en").lower()[:2], FILLERS["en"])
    original = body

    if settings.mid_hesitation_chance > 0.0:
        # Drawn before the head filler so that the two do not share a draw:
        # one transmission may have both, rarely, and that is a person having
        # a bad morning rather than a bug.
        # Never inside the address. The prosody layer has normally already
        # made the address its own sentence, so everything past the first
        # sentence break is the instruction; with that layer switched off
        # there is no break and the first comma is the one after the
        # callsign, which is the one place a filler must not go -- stalling
        # over the aeroplane's name is a different and much less flattering
        # thing than stalling over the clearance.
        after = body.find(". ")
        if after < 0:
            after = body.find(",")
        breaks = [n for n, mark in enumerate(body) if mark == "," and n > after]
        # With no boundary at all there is no telling where the callsign
        # ends, and "Delta uh, four fifty six" is stalling over its name.
        values = value_starts(body, language, after) if after >= 0 else []
        if (breaks or values) \
                and rng.random() < settings.mid_hesitation_chance:
            at = rng.choice(breaks + values)
            if at in values:
                # "...squawk uh, four seven..." -- in front of the value,
                # which is the word being looked for. No comma ahead of the
                # filler: the word runs into it, as "to, uh" does in the
                # recording, where a comma has Kokoro stop for a quarter
                # of a second first.
                body = f"{body[:at - 1]} {filler},{STALL} {body[at:]}"
            else:
                # "...via alpha, uh, hold short..." -- the filler takes the
                # comma with it and puts another one back, so the engine
                # still has the boundary it was given.
                body = f"{body[:at]}, {filler},{STALL}{body[at + 1:]}"

    if settings.hesitation_chance > 0.0 \
            and rng.random() < settings.hesitation_chance:
        body = f"{filler}, {body}"
    # The text itself when nothing fired, not a stripped copy of it: this
    # runs on every transmission and most of them are not hesitant ones.
    return text if body == original else body


def pieces(text: str) -> list[str]:
    """The text cut at every :data:`STALL`, each piece for the engine."""
    if STALL not in (text or ""):
        return [text]
    return [piece.strip() for piece in text.split(STALL) if piece.strip()]


def _speech_bounds(voice: np.ndarray, sample_rate: int) -> tuple[int, int]:
    """First and last sample above the floor :func:`_pauses` uses."""
    if voice.size == 0:
        return 0, 0
    peak = float(np.max(np.abs(voice)))
    if peak <= 0.0:
        return 0, voice.size
    window = max(1, int(sample_rate * 0.02))
    envelope = np.convolve(np.abs(voice), np.ones(window, dtype=np.float32)
                           / window, mode="same")
    loud = np.flatnonzero(envelope >= peak * 0.02)
    if loud.size == 0:
        return 0, voice.size
    return int(loud[0]), int(loud[-1]) + 1


def join(parts: list[np.ndarray], sample_rate: int,
         settings: Imperfections = DEFAULT,
         rng: random.Random | None = None) -> np.ndarray:
    """The pieces of a stalled transmission, with the voice stopped between.

    Each piece the engine rendered comes with its own silence either end;
    that is trimmed to a few tens of milliseconds so the gap is the one drawn
    here and not whatever two engine margins add up to. Plain zeros, like
    :func:`lead_in`: the chain's noise floor and room fill it the way they
    fill every other pause, and :func:`breathe` may find it and breathe in
    it, which is what somebody who has just found their place does.
    """
    parts = [np.asarray(p, dtype=np.float32) for p in parts if np.size(p)]
    if not parts:
        return np.zeros(0, dtype=np.float32)
    if len(parts) == 1:
        return parts[0]
    rng = rng or random.Random()
    margin = int(sample_rate * 0.03)
    out: list[np.ndarray] = []
    for n, part in enumerate(parts):
        start, end = _speech_bounds(part, sample_rate)
        start = 0 if n == 0 else max(0, start - margin)
        end = part.size if n == len(parts) - 1 else min(part.size, end + margin)
        if n:
            gap_ms = rng.uniform(settings.stall_ms / 7.0, settings.stall_ms) \
                if settings.enabled and settings.stall_ms > 0.0 else 0.0
            out.append(np.zeros(int(sample_rate * gap_ms / 1000.0),
                                dtype=np.float32))
        out.append(part[start:end])
    return np.concatenate(out)


# --------------------------------------------------------------------------
# the waveform, before the chain
# --------------------------------------------------------------------------


def lead_in(audio: np.ndarray, sample_rate: int,
            settings: Imperfections = DEFAULT,
            rng: random.Random | None = None) -> np.ndarray:
    """Silence at the head of the voice, before the chain is run over it.

    Before, not after: the chain mixes the controller's room in early and its
    noise bed rides the speech envelope, so silence added here comes back as
    an open microphone in an ops room. Silence added afterwards would be a
    hole in the transmission.
    """
    if not settings.enabled or audio is None:
        return audio
    voice = np.asarray(audio, dtype=np.float32)
    if voice.size == 0:
        return voice

    rng = rng or random.Random()
    spread = max(0.0, settings.lead_in_jitter_ms)
    millis = settings.lead_in_ms + rng.uniform(-spread, spread)
    samples = int(sample_rate * max(0.0, millis) / 1000.0)
    if samples <= 0:
        return voice
    return np.concatenate([np.zeros(samples, dtype=np.float32), voice])


def _noise(rng: random.Random, length: int) -> np.ndarray:
    """White noise, deterministic from the caller's generator.

    NumPy's generator is drawn from the :class:`random.Random` the caller
    already seeded rather than being seeded separately, so a transmission's
    breath and its wind stay tied to the same cache key as everything else
    here. Sixty-four bits is plenty and is what ``default_rng`` wants.
    """
    return np.random.default_rng(rng.getrandbits(64)).standard_normal(
        length).astype(np.float32)


def _alive(profile) -> bool:
    """Whether a person is speaking into this path at all.

    The chain already answers this, in ``breath_db``: the profiles that are
    recordings rather than people -- ATIS at -90, the sidetone at -90 -- put
    it below audibility precisely because nobody is breathing into them,
    while the interphone at -40 and the cabin address at -34 are two people
    in an aeroplane. Reusing that answer keeps a breath off an ATIS loop
    without this module having to know the list of profiles.
    """
    return getattr(profile, "breath_db", -90.0) > -80.0


def _keyed(profile) -> bool:
    """Whether that person has a transmit bar in their hand.

    ``unkey_early_chance`` is the chain's own answer to this, and it is a
    narrower question than :func:`_alive`: the first officer is a person, and
    is not on a frequency and is not standing in the weather.
    """
    return getattr(profile, "unkey_early_chance", 0.0) > 0.0


def _pauses(voice: np.ndarray, sample_rate: int,
            min_ms: float) -> list[tuple[int, int]]:
    """Where the synthesiser left a gap, as (start, end) sample pairs.

    A smoothed absolute envelope against a fraction of the peak, which is
    crude and is enough: the question is not where the phonemes are, it is
    which stretches are quiet enough that a breath would land between words
    rather than on one. The threshold is 2% of peak -- about -34 dB -- which
    is below the quietest fricative and above the synthesiser's own floor.

    The gap before the first word and the one after the last are dropped. The
    chain owns both ends: ``_breath`` fills the head and ``_key_held`` the
    tail, and a second breath in either would be two people.
    """
    if voice.size == 0:
        return []
    peak = float(np.max(np.abs(voice)))
    if peak <= 0.0:
        return []

    window = max(1, int(sample_rate * 0.02))
    envelope = np.convolve(np.abs(voice), np.ones(window, dtype=np.float32)
                           / window, mode="same")
    quiet = envelope < peak * 0.02
    # Run-length encode the quiet mask by looking at where it changes.
    edges = np.flatnonzero(np.diff(quiet.astype(np.int8)))
    bounds = np.concatenate([[0], edges + 1, [quiet.size]])

    least = int(sample_rate * max(0.0, min_ms) / 1000.0)
    runs: list[tuple[int, int]] = []
    for start, end in zip(bounds[:-1], bounds[1:]):
        if not quiet[start]:
            continue
        if start == 0 or end >= quiet.size:
            continue
        if end - start >= least:
            runs.append((int(start), int(end)))
    return runs


def breathe(audio: np.ndarray, sample_rate: int, profile,
            settings: Imperfections = DEFAULT,
            rng: random.Random | None = None,
            avoid: tuple | list = ()) -> np.ndarray:
    """Mix an inhale into one or two of the pauses in the transmission.

    Mixed, not inserted: the pauses are where the prosody layer decided the
    sentences end, and lengthening one to make room would undo that decision
    and push every later word back. A breath that is louder than the pause it
    sits in is audible without the pause changing at all, which is also what
    happens on a real frequency -- the breath is in the gap, the gap is not
    made for the breath.

    Run before :func:`lead_in`. Afterwards the silence that function adds is
    itself the longest quiet run in the waveform, and the loudest breath in
    the transmission would end up in front of the callsign, where the chain
    has already put one.

    ``avoid`` is the pauses :func:`catch_breath` has already breathed in:
    two breaths in one pause is a man hyperventilating.
    """
    if not settings.enabled or audio is None:
        return audio
    voice = np.asarray(audio, dtype=np.float32)
    if voice.size == 0 or not _alive(profile):
        return voice
    if settings.breath_chance <= 0.0 or settings.breath_max <= 0:
        return voice
    if settings.breath_db < -80.0:
        return voice

    rng = rng or random.Random()
    runs = [(start, end) for start, end
            in _pauses(voice, sample_rate, settings.breath_pause_ms)
            if not any(start < b and end > a for a, b in avoid)]
    if not runs:
        return voice

    # The longest pauses first: those are the clause boundaries the prosody
    # layer made, and they are where somebody reading a clearance aloud
    # actually stops for air.
    runs.sort(key=lambda run: run[1] - run[0], reverse=True)
    peak = float(np.max(np.abs(voice)))
    out = voice.copy()
    for start, end in runs[:int(settings.breath_max)]:
        if rng.random() >= settings.breath_chance:
            continue
        room = end - start
        length = min(room, int(sample_rate * rng.uniform(0.13, 0.24)))
        if length < int(sample_rate * 0.05):
            continue
        # Band-limited the way the chain's own breath is: an inhale through
        # a carbon microphone has no top and no bottom, and noise that still
        # had either would arrive as a click once the compressor found it.
        breath = bandpass(_noise(rng, length), sample_rate, 350.0, 1800.0, 2)
        rise = max(1, int(length * 0.35))
        shape = np.concatenate([
            np.linspace(0.0, 1.0, rise, dtype=np.float32) ** 1.5,
            np.linspace(1.0, 0.0, length - rise, dtype=np.float32) ** 2.0,
        ])[:length]
        level = peak * (10.0 ** (settings.breath_db / 20.0))
        # Late in the pause rather than early: the breath is taken to say the
        # next thing, not left over from the last one.
        at = start + max(0, int((room - length) * rng.uniform(0.35, 0.9)))
        out[at:at + length] += breath * shape * level
    return out


def _inhale(rng: random.Random, length: int, sample_rate: int) -> np.ndarray:
    """An inhale through an open mouth, at unit RMS across its middle.

    Brighter than the light breath :func:`breathe` mixes in: the one in the
    recorded clearance is centred on 1.5 kHz, which is air through the
    mouth rather than the nose. It swells for most of its length and stops
    short, because the speaker stops it to talk.
    """
    noise = bandpass(_noise(rng, length), sample_rate, 500.0, 3500.0, 2)
    rise = max(1, int(length * 0.65))
    shape = np.concatenate([
        np.linspace(0.0, 1.0, rise, dtype=np.float32) ** 1.3,
        np.linspace(1.0, 0.0, length - rise, dtype=np.float32) ** 1.8,
    ])[:length]
    breath = noise * shape
    middle = breath[shape > 0.5]
    rms = float(np.sqrt(np.mean(middle ** 2))) if middle.size else 1.0
    return breath / max(rms, 1e-9)


def catch_breath(audio: np.ndarray, sample_rate: int, profile,
                 settings: Imperfections = DEFAULT,
                 rng: random.Random | None = None,
                 ) -> tuple[np.ndarray, list[tuple[int, int]]]:
    """Stop for air in a long transmission: a longer pause with an inhale.

    :func:`breathe` puts a light breath into pauses the synthesiser left,
    and never lengthens one, which is right for most of them and is why a
    long clearance never sounded like somebody running out of air. A person
    reading one does: in the recorded clearance the pilot reads for six
    seconds, stops for 0.62, and takes a 0.35-second breath you can hear.

    So after about ``breath_group_s`` of speech, the next pause the
    synthesiser left is opened to 0.45-0.7 s -- silence inserted in its
    middle, so the words either side keep their own ends -- and an inhale
    goes in late in it, taken to say the next thing. Never before the last
    second of the transmission: nobody breathes to say "out".

    Returns the voice and where each lengthened pause now is, for
    :func:`breathe` to leave alone.
    """
    if not settings.enabled or audio is None:
        return audio, []
    voice = np.asarray(audio, dtype=np.float32)
    if voice.size == 0 or not _alive(profile) or settings.breath_group_s <= 0.0:
        return voice, []
    if settings.catch_breath_db < -80.0:
        return voice, []
    first, last = _speech_bounds(voice, sample_rate)
    runs = _pauses(voice, sample_rate, 100.0)
    if not runs or last <= first:
        return voice, []

    rng = rng or random.Random()
    frame = max(1, int(sample_rate * 0.02))
    count = voice.size // frame
    loudness = np.sqrt(np.mean(
        voice[:count * frame].reshape(count, frame) ** 2, axis=1)) \
        if count else np.zeros(1)
    level = float(np.percentile(loudness, 95)) \
        * 10.0 ** (settings.catch_breath_db / 20.0)

    group = settings.breath_group_s * rng.uniform(0.8, 1.25)
    since, previous = 0.0, first
    parts, spans, cursor = [], [], 0
    for start, end in runs:
        since += (start - previous) / sample_rate
        previous = end
        if since < group or (last - end) < sample_rate:
            continue
        target = int(sample_rate * rng.uniform(0.45, 0.7))
        extra = max(0, target - (end - start))
        middle = (start + end) // 2
        pause = np.concatenate([voice[start:middle],
                                np.zeros(extra, dtype=np.float32),
                                voice[middle:end]])
        length = min(int(sample_rate * rng.uniform(0.28, 0.42)),
                     pause.size - int(sample_rate * 0.1))
        if length > int(sample_rate * 0.1):
            # Ending just before the next word: taken to say it.
            tail = int(sample_rate * rng.uniform(0.04, 0.09))
            at = max(0, pause.size - tail - length)
            pause[at:at + length] += _inhale(rng, length, sample_rate) * level
        parts.append(voice[cursor:start])
        offset = sum(p.size for p in parts)
        parts.append(pause)
        spans.append((offset, offset + pause.size))
        cursor = end
        since = 0.0
        group = settings.breath_group_s * rng.uniform(0.8, 1.25)
    if not spans:
        return voice, []
    parts.append(voice[cursor:])
    return np.concatenate(parts).astype(np.float32), spans


def wind(audio: np.ndarray, sample_rate: int, profile,
         settings: Imperfections = DEFAULT,
         rng: random.Random | None = None) -> np.ndarray:
    """A gusting bed under the whole transmission, before the chain.

    Before the chain, so the compressor and the AGC hear it: the level of the
    voice dipping as a gust arrives is what says the two were in the same
    room, and a bed added after the chain would ride serenely over the top of
    a transmission it was supposed to be behind.

    Run after :func:`lead_in`, so the gap between the carrier and the first
    word has weather in it. That gap is the clearest place in the whole
    transmission to hear one, because nobody is talking over it.
    """
    if not settings.enabled or audio is None:
        return audio
    voice = np.asarray(audio, dtype=np.float32)
    if voice.size == 0 or not _keyed(profile):
        return voice
    if settings.wind_db < -80.0:
        return voice
    peak = float(np.max(np.abs(voice)))
    if peak <= 0.0:
        return voice

    rng = rng or random.Random()
    n = voice.size
    source = _noise(rng, n)
    # Two parts, because wind is not one sound: a roar with no pitch, and the
    # hiss of it moving past an edge. All of it well inside the passband the
    # chain will impose anyway -- below 300 Hz the chain removes it and the
    # only thing left of the effort is a compressor pumping at nothing.
    bed = lowpass(source, sample_rate, 420.0, order=2)
    bed += bandpass(source, sample_rate, 600.0, 2400.0, 2) * 0.35

    gust = max(0.0, min(1.0, settings.wind_gust))
    if gust > 0.0:
        # A gust is a second or two long, so the envelope is white noise with
        # everything above about 0.6 Hz taken off it. Normalised rather than
        # scaled: low-passing that hard leaves an amplitude that depends on
        # the length of the clip, and a four-second transmission would
        # otherwise gust harder than a twelve-second one.
        slow = lowpass(_noise(rng, n), sample_rate, 0.6, order=2)
        span = float(np.max(np.abs(slow))) or 1.0
        bed *= 1.0 + gust * (slow / span)

    level = peak * (10.0 ** (settings.wind_db / 20.0))
    scale = float(np.sqrt(np.mean(bed * bed))) or 1.0
    return (voice + bed * (level / scale)).astype(np.float32)


def _contour(rng: random.Random, length: int, sample_rate: int,
             corner_hz: float) -> np.ndarray:
    """A slow, smooth curve in [-1, 1], as long as the voice.

    Drawn at a low control rate and interpolated up, rather than low-passing
    white noise at the audio rate: a Butterworth at 0.7 Hz against 44.1 kHz
    is numerically miserable and there is nothing above a few hertz worth
    keeping. Normalised to its own peak so that a short acknowledgement and a
    long clearance drift by the same amount -- the filter's gain would
    otherwise depend on the length of the clip.
    """
    control_rate = 100.0
    points = max(8, int(length / sample_rate * control_rate) + 8)
    source = _noise(rng, points)
    slow = lowpass(source, int(control_rate), max(0.05, corner_hz), order=2)
    span = float(np.max(np.abs(slow))) or 1.0
    slow = slow / span
    where = np.linspace(0.0, points - 1, length, dtype=np.float64)
    return np.interp(where, np.arange(points, dtype=np.float64),
                     slow.astype(np.float64)).astype(np.float32)


def wander(audio: np.ndarray, sample_rate: int,
           settings: Imperfections = DEFAULT,
           rng: random.Random | None = None) -> np.ndarray:
    """Let the pitch and the level drift the way a person's do.

    The pitch drift is a slow variable-speed read of the waveform: the
    playback rate follows a contour a fraction of a per cent either side of
    one, so the pitch and the timing move together, which is what a voice
    does and a tape does not. Cubic interpolation would be no better than
    linear here -- the rate never leaves one by more than a few per cent, so
    the samples are read almost exactly where they are.

    The level drift is a gain contour on top. Small, and slower than a
    syllable, so it is never confused with the compressor's work or with
    fading on the path, both of which the chain does for itself afterwards.

    Run before :func:`lead_in`: the drift belongs to the voice, and the
    silence in front of it should stay silent. Run after :func:`breathe`,
    which found the pauses on the un-warped waveform; a warp of a per cent
    moves them by a few milliseconds and nothing else.
    """
    if not settings.enabled or audio is None:
        return audio
    voice = np.asarray(audio, dtype=np.float32)
    if voice.size < 2:
        return voice
    pitch = max(0.0, settings.wander_pitch)
    level = max(0.0, settings.wander_level_db)
    if pitch <= 0.0 and level <= 0.0:
        return voice

    rng = rng or random.Random()
    out = voice
    if pitch > 0.0:
        rate = 1.0 + (pitch / 100.0) * _contour(rng, voice.size, sample_rate,
                                                settings.wander_hz)
        # Where each output sample reads from: the running sum of the rate.
        # The read never runs past the end, so the output is at most a few
        # per cent shorter or longer than the input, never padded.
        position = np.cumsum(rate.astype(np.float64)) - rate[0]
        position = position[position <= voice.size - 1]
        out = np.interp(position, np.arange(voice.size, dtype=np.float64),
                        voice.astype(np.float64)).astype(np.float32)
    if level > 0.0 and out.size:
        gain_db = level * _contour(rng, out.size, sample_rate,
                                   settings.wander_hz)
        out = (out * (10.0 ** (gain_db / 20.0))).astype(np.float32)
    return out


# --------------------------------------------------------------------------
# the arguments the chain already takes
# --------------------------------------------------------------------------


def tune(profile, settings: Imperfections = DEFAULT):
    """A copy of the radio profile with the operator's numbers in it.

    Only the fields this layer owns are touched, and only where the profile
    has the effect switched on at all. That condition is the important half:
    ``PROFILE_ATIS``, ``PROFILE_SIDETONE``, ``PROFILE_INTERCOM`` and
    ``PROFILE_CABIN`` all set ``unkey_early_chance`` to zero because none of
    them is a person holding a transmit bar, and a setting that overrode that
    would put a fumbled release on a recorded broadcast.

    Returns the profile itself when there is nothing to change, so the cache
    key that names the profile keeps naming it.
    """
    if not settings.enabled:
        return profile

    changes: dict[str, float] = {}
    if getattr(profile, "unkey_early_chance", 0.0) > 0.0:
        changes["unkey_early_chance"] = max(0.0, min(1.0, settings.clip_chance))
        changes["unkey_early_ms"] = max(0.0, settings.clip_ms)
    if getattr(profile, "level_jitter_db", 0.0) > 0.0 and settings.level != 1.0:
        changes["level_jitter_db"] = max(
            0.0, profile.level_jitter_db * settings.level)

    if not changes:
        return profile
    return replace(profile, **changes)


def keying(base: float, settings: Imperfections = DEFAULT,
           rng: random.Random | None = None) -> float:
    """The station's key-up character, wobbled for this one transmission.

    ``base`` is :func:`wilcoatc.audio.radio_fx.station_keying`, which is
    stable per station and must stay recognisable: the wobble is added to it,
    not substituted for it, so Kennedy Tower still keys like Kennedy Tower.
    """
    if not settings.enabled or settings.carrier <= 0.0:
        return base
    rng = rng or random.Random()
    spread = settings.carrier
    return float(max(-1.0, min(1.0, base + rng.uniform(-spread, spread))))


__all__ = ["DEFAULT", "FILLERS", "OFF", "STALL", "VALUE_WORDS", "Imperfections",
           "breathe", "catch_breath", "from_config", "hesitate", "join",
           "keying", "lead_in",
           "pieces", "rng_for", "tune", "value_starts", "wander", "wind"]
