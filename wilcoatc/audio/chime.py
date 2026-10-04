"""The cabin chime.

One sound, synthesised rather than shipped as a file, for the same reason the
controller voices are: a sample would have to be licensed, would be one
airline's chime rather than a chime, and would be a binary blob in a source
tree where everything else can be read.

What it is physically is a pair of struck bars, which is a small number of
partials with a fast attack and a long, slightly detuned decay. Two notes a
fourth apart going up means "the crew would like your attention"; going down
means the seatbelt sign has changed. Both are the same generator with the
notes swapped, because they are the same instrument.
"""

from __future__ import annotations

import numpy as np

SAMPLE_RATE = 22050

# The two notes, in Hz. A fourth apart, in the register a cabin chime actually
# sits in -- high enough to carry over the air conditioning, low enough not to
# be a smoke alarm.
HIGH = 880.0
LOW = 659.25

# The partials of a struck bar, as multiples of the fundamental with their own
# amplitudes and decay rates. A bar is not a string: its overtones are not
# harmonic, and the inharmonicity is most of why a chime sounds like metal.
_PARTIALS: tuple[tuple[float, float, float], ...] = (
    (1.000, 1.00, 1.00),
    (2.756, 0.34, 1.90),
    (5.404, 0.13, 3.20),
    (8.933, 0.05, 5.10),
)


def _bar(frequency: float, seconds: float, sample_rate: int,
         decay_s: float = 0.75) -> np.ndarray:
    """One struck bar."""
    n = int(seconds * sample_rate)
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    t = np.arange(n, dtype=np.float32) / float(sample_rate)
    out = np.zeros(n, dtype=np.float32)
    for ratio, amplitude, decay in _PARTIALS:
        # Two voices a few cents apart, which is what gives the tail its slow
        # beat rather than a dead sine.
        for detune in (1.0, 1.0015):
            out += (amplitude * 0.5
                    * np.sin(2.0 * np.pi * frequency * ratio * detune * t)
                    * np.exp(-t * decay / decay_s))
    # The strike itself: a couple of milliseconds of rise, so it is a hit
    # rather than a tone that was always there.
    rise = max(1, int(0.0025 * sample_rate))
    out[:rise] *= np.linspace(0.0, 1.0, rise, dtype=np.float32)
    return out


def chime(up: bool = True, sample_rate: int = SAMPLE_RATE,
          gap_s: float = 0.30, tail_s: float = 1.25) -> np.ndarray:
    """The two-note cabin chime.

    ``up`` is the attention chime the crew ring before an announcement; down
    is the one the seatbelt sign makes.
    """
    first, second = (LOW, HIGH) if up else (HIGH, LOW)
    length = gap_s + tail_s
    audio = _bar(first, length, sample_rate)
    offset = int(gap_s * sample_rate)
    second_bar = _bar(second, tail_s, sample_rate)
    audio[offset:offset + len(second_bar)] += second_bar[:len(audio) - offset]

    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 0:
        audio = audio / peak * 0.5
    return audio.astype(np.float32)


def silence(seconds: float, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """A gap, for putting between a chime and the voice that follows it."""
    return np.zeros(max(0, int(seconds * sample_rate)), dtype=np.float32)


__all__ = ["chime", "silence", "SAMPLE_RATE"]
