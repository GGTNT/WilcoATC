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

*Occasionally they lose the thread.* This one is not audio. An "uh" has to be
synthesised, so it is a word added to the text before the engine is called,
and it lives here rather than in :mod:`wilcoatc.audio.prosody` because it is
the same feature and the same switch as the rest. Two places it can go: at
the front, from somebody who keyed up before they had decided what to say,
and at a comma part way through, from somebody who has lost their place in a
long clearance. The second is the commoner of the two on a real frequency and
the less noticeable of the two here. Both stay rare: a controller who says
"uh" once every few transmissions is a person, and one who says it every
third is a bit.

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
    "it": "ehm",
    "pt": "eh",
    "nl": "eh",
}


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
                       hesitation_chance=0.0)


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
    a comma that is already in the text and nowhere else, for exactly the
    reason :mod:`wilcoatc.audio.prosody` promotes commas rather than
    inventing them: a filler dropped between any two words would eventually
    land inside "hold short of runway", and a fixed phrase cut in half is a
    clearance nobody can read back.

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
        if breaks and rng.random() < settings.mid_hesitation_chance:
            at = rng.choice(breaks)
            # "...via alpha, uh, hold short..." -- the filler takes the comma
            # with it and puts another one back, so the engine still has the
            # boundary it was given and the pause lands either side of the
            # word rather than only in front of it.
            body = f"{body[:at]}, {filler},{body[at + 1:]}"

    if settings.hesitation_chance > 0.0 \
            and rng.random() < settings.hesitation_chance:
        body = f"{filler}, {body}"
    # The text itself when nothing fired, not a stripped copy of it: this
    # runs on every transmission and most of them are not hesitant ones.
    return text if body == original else body


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
            rng: random.Random | None = None) -> np.ndarray:
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
    runs = _pauses(voice, sample_rate, settings.breath_pause_ms)
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


__all__ = ["DEFAULT", "FILLERS", "OFF", "Imperfections", "breathe",
           "from_config", "hesitate", "keying", "lead_in", "rng_for", "tune",
           "wander", "wind"]
