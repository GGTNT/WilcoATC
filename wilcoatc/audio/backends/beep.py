"""A backend that says nothing, at 440 Hz.

For the tests and for ``doctor``. It has every property a real engine has --
it declares capabilities, it is always available, it returns float32 mono at a
stated rate, it respects ``length_scale`` -- and no model, so the registry,
the cache, the radio chain and the playback path can all be exercised on a
machine with nothing downloaded.

It is **not** registered by default. A frequency answering every clearance
with a tone is not a graceful degradation, it is a fault that sounds like a
feature, and a pilot would file it as one.
"""

from __future__ import annotations

import numpy as np

from .base import ANY_LANGUAGE, Capabilities

#: Concert A. Chosen because it is unmistakably not speech.
TONE_HZ = 440.0

#: Roughly what a transmission of the same length would take, so anything
#: timing-sensitive downstream sees a plausible duration rather than a click.
SECONDS_PER_CHARACTER = 0.055

#: Well under a real transmission, because this is played on purpose and a
#: sine at full scale is unpleasant.
AMPLITUDE = 0.25


class BeepBackend:
    """A 440 Hz tone, shaped like a transmission."""

    capabilities = Capabilities(
        name="beep",
        languages=frozenset({ANY_LANGUAGE}),
        needs_gpu=False,
        can_clone=False,
        sample_rate=24000,
        tier="cpu",
    )

    def __init__(self, sample_rate: int = 24000):
        self.sample_rate = int(sample_rate)
        #: What it has been asked to say, in order. The tests read this to
        #: check the pipeline reached the engine with the right words.
        self.spoken: list[tuple[str, str]] = []

    def available(self) -> bool:
        return True

    def warm(self) -> None:
        return None

    def synthesize(self, text: str, voice, lang: str, *,
                   length_scale: float | None = None,
                   ) -> tuple[np.ndarray, int]:
        self.spoken.append((text, lang))
        if not text.strip():
            return np.zeros(0, dtype=np.float32), self.sample_rate

        scale = float(length_scale if length_scale is not None else 1.0)
        seconds = len(text) * SECONDS_PER_CHARACTER * max(scale, 0.1)
        count = max(1, int(seconds * self.sample_rate))
        t = np.arange(count, dtype=np.float32) / float(self.sample_rate)
        tone = np.sin(2.0 * np.pi * TONE_HZ * t, dtype=np.float32) * AMPLITUDE

        # A five-millisecond fade at each end. Without it the tone starts and
        # stops on a discontinuity, which the radio chain turns into a click
        # that is louder than the tone.
        edge = min(count // 2, int(0.005 * self.sample_rate))
        if edge > 1:
            ramp = np.linspace(0.0, 1.0, edge, dtype=np.float32)
            tone[:edge] *= ramp
            tone[-edge:] *= ramp[::-1]
        return tone, self.sample_rate


__all__ = ["BeepBackend", "TONE_HZ"]
