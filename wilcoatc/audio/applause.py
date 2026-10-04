# -*- coding: utf-8 -*-
"""A cabin clapping, for a landing gentle enough to deserve it.

Synthesised rather than shipped, for the reasons :mod:`wilcoatc.audio.chime`
gives: a recording would have to be licensed, would be one cabin rather than
a cabin, and would be a binary blob in a tree where everything else can be
read. Applause is also one of the few sounds that is easier to build honestly
than to fake -- it is nothing but a crowd of short noise bursts.

What it is
----------

A clap is a burst of noise a few tens of milliseconds long, coloured by the
cupped hands into a broad resonance somewhere between about 700 Hz and
2.5 kHz. Each passenger has their own hands, so their own colour and their
own tempo -- roughly four claps a second, never quite even -- and they join
in over the first second or so and stop one by one over the last few. That
spread of starts and stops is the whole envelope; nothing is faded by hand.

Heard from the flight deck, through the door, so the top is rolled off and it
sits well under a voice. There is no cheering: one "whoo" synthesised badly
would be the only thing anybody remembered about the landing.

Seeded, so the same flight hears the same cabin.
"""

from __future__ import annotations

import numpy as np

SAMPLE_RATE = 22050

# How many people clap. Not everybody on a narrowbody does, and past about
# forty individual hands stop being audible as hands and become rain.
CLAPPERS = 34

# How long the whole thing lasts, in seconds, including the tail.
LENGTH_S = 7.0

# A clap's own length and how quickly it dies.
_CLAP_S = 0.045
_DECAY_S = (0.006, 0.014)

# The colour each pair of hands can have, in Hz.
_HANDS_HZ = (700.0, 2500.0)

# Claps per second, per person.
_TEMPO_HZ = (3.2, 4.8)

# Where the door takes the top off.
_DOOR_HZ = 3200.0

# The loudest it gets. Under the crew, because it is behind a door and they
# are on a speaker.
PEAK = 0.34


def _hands(rng: np.random.Generator, sample_rate: int) -> np.ndarray:
    """One person's clap: a decaying noise burst through a broad resonance."""
    n = int(_CLAP_S * sample_rate)
    t = np.arange(n, dtype=np.float32) / float(sample_rate)
    burst = rng.standard_normal(n).astype(np.float32)
    burst *= np.exp(-t / rng.uniform(*_DECAY_S)).astype(np.float32)
    # A band-pass done in the frequency domain, which on a burst this short
    # is exact enough and needs nothing beyond numpy.
    spectrum = np.fft.rfft(burst, n=2 * n)
    freqs = np.fft.rfftfreq(2 * n, 1.0 / sample_rate)
    centre = rng.uniform(*_HANDS_HZ)
    width = centre * rng.uniform(0.6, 1.1)
    spectrum *= np.exp(-0.5 * ((freqs - centre) / width) ** 2)
    shaped = np.fft.irfft(spectrum)[:n].astype(np.float32)
    peak = float(np.max(np.abs(shaped))) or 1.0
    return shaped / peak


def applause(seed: int = 0, sample_rate: int = SAMPLE_RATE,
             seconds: float = LENGTH_S) -> np.ndarray:
    """A few seconds of a cabin clapping, as float32 mono."""
    rng = np.random.default_rng(seed & 0xFFFFFFFF)
    total = int(seconds * sample_rate)
    out = np.zeros(total + int(_CLAP_S * sample_rate) + 1, dtype=np.float32)

    for _ in range(CLAPPERS):
        hands = _hands(rng, sample_rate)
        # Somebody starts it and the rest join; a few never quite commit.
        start = rng.gamma(2.0, 0.28)
        stop = rng.uniform(0.5, 0.94) * seconds
        loudness = rng.uniform(0.35, 1.0)
        period = 1.0 / rng.uniform(*_TEMPO_HZ)
        at = start
        while at < stop:
            # Softer for the first couple of claps and as they tail off.
            swell = min(1.0, (at - start) / 0.6 + 0.4)
            fade = min(1.0, (stop - at) / 0.8)
            gain = loudness * swell * fade * rng.uniform(0.75, 1.0)
            index = int(at * sample_rate)
            out[index:index + len(hands)] += gain * hands
            at += period * rng.uniform(0.88, 1.12)

    out = out[:total]
    # Through the flight deck door.
    spectrum = np.fft.rfft(out)
    freqs = np.fft.rfftfreq(len(out), 1.0 / sample_rate)
    spectrum /= np.sqrt(1.0 + (freqs / _DOOR_HZ) ** 4)
    out = np.fft.irfft(spectrum, n=len(out)).astype(np.float32)

    # The last claps stop dead otherwise, and a cabin does not.
    tail = int(0.4 * sample_rate)
    out[-tail:] *= np.linspace(1.0, 0.0, tail, dtype=np.float32)
    peak = float(np.max(np.abs(out))) or 1.0
    return (out * (PEAK / peak)).astype(np.float32)


__all__ = ["SAMPLE_RATE", "applause"]
