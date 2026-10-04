# -*- coding: utf-8 -*-
"""The music that plays in the cabin while people are getting on.

Synthesised rather than shipped, for the reasons :mod:`wilcoatc.audio.chime`
is: a recording would have to be licensed, would be one airline's playlist
rather than boarding music, and would be a binary blob in a tree where
everything else can be read.

This file used to say that inventing boarding music would be silly, and for
a while that was right -- the moment existed only for pilots who had dropped
a Fenix sound pack in, and a synthesised substitute for a pop song would have
been embarrassing. What changed is the ambition. Nobody is trying to
reproduce a playlist here. The target is the *bed*: the soft, slow, harmonically
static pad that sits under a boarding cabin at a level where you stop
noticing it, and whose only real job is to make the silence before pushback
sound like an aeroplane rather than like a program that has not started yet.
That is a thing four chords and a sine bank can do honestly.

What it is
----------

Four chords, each about eight seconds, in a low warm register, played on a
bank of detuned sine partials with a long attack and a longer release. The
chords overlap, so the pad never gates; the whole thing is written into a
circular buffer, so the last sample runs into the first and the loop can be
repeated for as long as boarding takes without a seam.

There is no percussion and no melody on purpose. Both draw attention, and
anything that draws attention here is competing with the purser.
"""

from __future__ import annotations

import numpy as np

SAMPLE_RATE = 22050

# How long the loop is before it repeats. Long enough that a pilot sitting
# through a full boarding does not hear it come round as a loop, short enough
# that rendering it is a few hundred milliseconds rather than a wait.
LOOP_S = 32.0

# The progression, as note names in a low register. A ii-IV-V-I in F that
# never resolves hard, which is what keeps it from sounding like it is going
# anywhere -- boarding music that arrives somewhere makes people look up.
#
# Written as semitone offsets from A2 (110 Hz) so the file needs no note
# table: 0 is A2, 12 is A3.
_CHORDS: tuple[tuple[int, ...], ...] = (
    (8, 12, 15, 19, 22),      # F major 9
    (5, 12, 17, 20, 24),      # D minor 9
    (1, 8, 13, 17, 20),       # B flat major 9
    (3, 7, 10, 15, 19),       # C 9
)

# The base of the register, in Hz. A2. High enough to survive a laptop
# speaker, low enough that it sits under a voice instead of in front of it.
_ROOT_HZ = 110.0

# Each note is a small bank of partials: the fundamental, its octave, and its
# twelfth, each quieter than the last. More than three and it stops being a
# pad and starts being an organ.
_PARTIALS: tuple[tuple[float, float], ...] = (
    (1.0, 1.00),
    (2.0, 0.30),
    (3.0, 0.11),
)

# How far apart the two voices of each partial are, as a ratio. A couple of
# cents, which is the whole of why the pad moves at all: two exact sines are
# a test tone.
_DETUNE = 1.0022

# How long a chord takes to arrive and to go, in seconds. Both are long, and
# together they are longer than a chord lasts -- which is the point, because
# it means the next chord is already coming up as this one goes and there is
# never a gap between them.
_ATTACK_S = 2.6
_RELEASE_S = 4.2

# The cache. Rendering is a second or so of arithmetic and the answer never
# changes, so it is done once per sample rate and kept.
_CACHE: dict[int, np.ndarray] = {}


def _envelope(n: int, sample_rate: int) -> np.ndarray:
    """One chord's shape: up slowly, hold, down more slowly."""
    attack = min(n, int(_ATTACK_S * sample_rate))
    release = min(n - attack, int(_RELEASE_S * sample_rate))
    env = np.ones(n, dtype=np.float32)
    if attack:
        # Raised cosine rather than a straight line. A linear fade into a
        # sustained pad has an audible corner where it stops fading.
        env[:attack] = (1.0 - np.cos(
            np.linspace(0.0, np.pi, attack, dtype=np.float32))) * 0.5
    if release:
        env[n - release:] = (1.0 + np.cos(
            np.linspace(0.0, np.pi, release, dtype=np.float32))) * 0.5
    return env


def _chord(semitones: tuple[int, ...], n: int, sample_rate: int,
           phase: float) -> np.ndarray:
    """One chord, enveloped, ready to be laid into the loop.

    ``phase`` is where in the loop this chord starts, in seconds. The partials
    are generated against absolute time rather than against the start of the
    chord, so that a chord written across the seam of the circular buffer
    meets itself in phase instead of clicking.
    """
    t = (np.arange(n, dtype=np.float32) / float(sample_rate)) + phase
    out = np.zeros(n, dtype=np.float32)
    for step in semitones:
        frequency = _ROOT_HZ * (2.0 ** (step / 12.0))
        for ratio, amplitude in _PARTIALS:
            for detune in (1.0, _DETUNE):
                out += (amplitude * 0.5
                        * np.sin(2.0 * np.pi * frequency * ratio * detune * t))
    return out * _envelope(n, sample_rate)


def bed(sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """The boarding bed: one seamless loop, mono, peaking at about a half.

    The level is deliberately left where a voice is left. What makes this
    music rather than an announcement is the gain the player puts it behind,
    not a quiet waveform -- a bed rendered quiet and then turned up again is
    a bed rendered with less of it.
    """
    rate = int(sample_rate) or SAMPLE_RATE
    cached = _CACHE.get(rate)
    if cached is not None:
        return cached

    total = int(LOOP_S * rate)
    loop = np.zeros(total, dtype=np.float32)
    span = total // len(_CHORDS)
    # Each chord runs past its own slot by the length of its release, and the
    # overrun wraps round the end of the buffer. That wrap is what makes the
    # loop seamless: the tail of the last chord is already sounding under the
    # first one when the loop comes round again.
    length = span + int(_RELEASE_S * rate)
    for index, semitones in enumerate(_CHORDS):
        start = index * span
        voice = _chord(semitones, length, rate, start / float(rate))
        end = start + length
        if end <= total:
            loop[start:end] += voice
        else:
            split = total - start
            loop[start:] += voice[:split]
            loop[:end - total] += voice[split:]

    peak = float(np.max(np.abs(loop))) if loop.size else 0.0
    if peak > 0:
        loop = loop / peak * 0.5
    loop = loop.astype(np.float32)
    _CACHE[rate] = loop
    return loop


__all__ = ["bed", "SAMPLE_RATE", "LOOP_S"]
