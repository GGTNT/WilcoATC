"""Kokoro, behind the backend interface.

A thin adapter and nothing else. :class:`wilcoatc.audio.tts_kokoro.KokoroBackend`
already had the shape this interface asks for -- it was written to sit exactly
where ``Synthesizer._synthesize`` calls it -- so the port is a declaration of
capabilities plus the one call the old code made, with the same argument, in
the same order. Nothing about how Kokoro is loaded, threaded, phonemised or
paused is touched here; all of that stays in ``tts_kokoro`` where it was
measured.

The language set is the five Kokoro actually has a speaker for. It is
deliberately not ``ANY_LANGUAGE``: Kokoro has no German and no Dutch, and a
registry that believed otherwise would hand it a German clearance and get
English phonemes read back.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ..tts_kokoro import MODEL_DIR, KokoroBackend as _Kokoro
from .base import Capabilities

#: The languages the shipped Kokoro model has a speaker for. Mirrors
#: :data:`wilcoatc.audio.voices.KOKORO_CATALOGUE`; widening one without the
#: other is how a controller ends up mute.
LANGUAGES = frozenset({"en", "fr", "it", "pt", "es"})


class KokoroTTS:
    """The Kokoro engine as a registry backend."""

    def __init__(self, model_dir: Path = MODEL_DIR):
        self.inner = _Kokoro(model_dir)
        self.capabilities = Capabilities(
            name="kokoro",
            languages=LANGUAGES,
            needs_gpu=False,
            can_clone=False,
            # Kokoro's own rate. What actually comes back is whatever
            # ``create`` reports, which is why this is only the fallback.
            sample_rate=24000,
            tier="cpu",
        )

    def available(self) -> bool:
        return self.inner.available()

    def warm(self) -> None:
        self.inner.warm()

    def synthesize(self, text: str, voice, lang: str, *,
                   length_scale: float | None = None,
                   ) -> tuple[np.ndarray, int]:
        """Exactly the call ``Synthesizer._synthesize`` used to make.

        ``text`` arrives already through ``prepare_text`` and already cast;
        the phonemising is ``tts_kokoro``'s, because the accent layer needs
        Kokoro's shared phoneme table and cannot be lifted out of it.
        """
        return self.inner.synthesize(text, voice, length_scale=length_scale)

    # Kokoro's style vectors are per catalogue voice and are wanted by the
    # diagnostics, which used to reach through ``Synthesizer._backend()``.
    def style(self, spec):
        return self.inner.style(spec)


__all__ = ["KokoroTTS", "LANGUAGES"]
