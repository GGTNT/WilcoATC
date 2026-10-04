"""Piper, behind the backend interface.

Moved out of :class:`wilcoatc.audio.tts.Synthesizer` unchanged: the model
cache keyed on the file rather than the voice, the raw-phoneme payloads, the
``SynthesisConfig`` fields and the int16-to-float conversion are all the code
that was there, in the same order, with the same numbers.

The one thing worth re-reading before touching any of it is
:meth:`PiperTTS.payloads`. Piper phonemises in the *voice's* language, so
English handed to a French model comes back as English words read with French
spelling rules -- "knots" as /kno/. Phonemising here instead is what keeps the
words in their own language while only their sounds become the controller's.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

from .. import accent
from ..voices import CATALOGUE, VoiceRegistry, VOICE_DIR
from .base import Capabilities

log = logging.getLogger(__name__)

#: Piper's English models all run at this rate. What is actually returned is
#: whatever the loaded model reports; this is the fallback for an empty result.
DEFAULT_SAMPLE_RATE = 22050

#: Every language the catalogue has a Piper voice for. Piper is one model per
#: voice, so this is a property of the catalogue rather than of the engine.
LANGUAGES = frozenset({"en", "fr", "de", "nl", "es", "it", "pt"})

#: Silence between one sentence and the next, in seconds.
#:
#: :meth:`PiperTTS.payloads` hands the model one phoneme block per sentence,
#: and the blocks used to be concatenated flush. That was invisible while
#: every transmission was a single sentence, and wrong the moment one was not:
#: a full stop is the strongest pause mark there is and it was coming back
#: shorter than the commas around it, because the pause a full stop earns is
#: produced *inside* a synthesis run and there is nothing inside a run that
#: ends at the full stop. Kokoro pads its own sentence batches for the same
#: reason and with the same number -- see ``tts_kokoro.SENTENCE_PAUSE`` -- and
#: the two engines have to agree, or the same clearance is paced differently
#: depending on which controller says it.
SENTENCE_PAUSE = 0.25


class PiperTTS:
    """The Piper engine as a registry backend.

    Resolves a model from the catalogue and the voice folder, deliberately
    *not* from ``VoiceRegistry.available()``. That list is filtered by which
    engine is casting -- with Kokoro installed it drops every Piper voice
    Kokoro can stand in for -- and asking it for a path is how the Kokoro
    fallback came to re-cast onto a Piper voice and then fail to find its
    file. What is on disk is what is on disk, whoever is doing the casting.
    """

    def __init__(self, registry: VoiceRegistry | None = None):
        self.registry = registry or VoiceRegistry(VOICE_DIR)
        self._voices: dict[str, object] = {}
        self._lock = threading.Lock()
        self.capabilities = Capabilities(
            name="piper",
            languages=LANGUAGES,
            needs_gpu=False,
            can_clone=False,
            sample_rate=DEFAULT_SAMPLE_RATE,
            tier="cpu",
        )

    # ------------------------------------------------------------------

    def available(self) -> bool:
        """Whether any Piper model is on disk and the package can be imported.

        Both halves, and the package half without importing it: casting a
        controller from an engine that cannot load does not fail here, it
        fails when they first key up with a clearance already composed.
        """
        try:
            if not any(spec.path(self.root).exists() for spec in CATALOGUE):
                return False
            from importlib.util import find_spec

            return find_spec("piper") is not None
        except Exception:                            # pragma: no cover
            return False

    @property
    def root(self):
        """The voice folder. Read through the registry so that a registry
        swapped in during the Kokoro fallback moves this with it."""
        return self.registry.root

    def warm(self, keys: list[str] | None = None) -> None:
        """Load the models. Costs well over a gigabyte for the whole catalogue,
        which is why ``voice.preload`` defaults to off."""
        for spec in CATALOGUE:
            if keys and spec.key not in keys:
                continue
            if not spec.path(self.root).exists():
                continue
            try:
                self.load(spec.key)
            except Exception as exc:
                log.warning("could not preload %s: %s", spec.key, exc)

    def forget(self) -> None:
        """Drop the loaded models, after the voice folder has changed."""
        with self._lock:
            self._voices.clear()

    # ------------------------------------------------------------------

    def load(self, key: str):
        """Load and keep a Piper model.

        Keyed on the model, not on the voice: a multi-speaker model holds
        several controllers and must only be loaded once.
        """
        with self._lock:
            voice = self._voices.get(key)
            if voice is not None:
                return voice
            from piper import PiperVoice

            path = None
            for spec in CATALOGUE:
                if spec.key == key and spec.engine == "piper":
                    path = spec.path(self.root)
                    break
            if path is None or not path.exists():
                raise FileNotFoundError(
                    f"Voice model {key} not found in {self.root}")
            voice = PiperVoice.load(str(path))
            self._voices[key] = voice
            return voice

    # ------------------------------------------------------------------

    def synthesize(self, text: str, voice, lang: str, *,
                   length_scale: float | None = None,
                   ) -> tuple[np.ndarray, int]:
        from piper import SynthesisConfig

        model = self.load(voice.key)
        scale = float(length_scale if length_scale is not None
                      else voice.length_scale)

        config = SynthesisConfig(
            length_scale=scale,
            noise_scale=float(voice.noise_scale),
            noise_w_scale=float(voice.noise_w),
            normalize_audio=True,
            speaker_id=voice.speaker_id,
        )

        chunks: list[np.ndarray] = []
        sample_rate = DEFAULT_SAMPLE_RATE
        model_voice = str(getattr(model.config, "espeak_voice", "") or "")
        payloads = self.payloads(text, voice, model_voice)
        for payload in payloads:
            if chunks:
                # The gap the full stop earns. See SENTENCE_PAUSE: each block
                # is its own synthesis run, so nothing but this puts one there.
                # Guarded on there being audio already, so a block that
                # rendered nothing cannot leave a transmission of pure silence.
                chunks.append(np.zeros(int(SENTENCE_PAUSE * sample_rate),
                                       dtype=np.float32))
            for chunk in model.synthesize(payload, syn_config=config):
                pcm = np.asarray(chunk.audio_int16_array, dtype=np.int16)
                chunks.append(pcm.astype(np.float32) / 32768.0)
                rate = getattr(chunk, "sample_rate", None)
                if rate:
                    sample_rate = int(rate)
        if not chunks:
            return np.zeros(0, dtype=np.float32), sample_rate
        sample_rate = int(getattr(model.config, "sample_rate", sample_rate))
        return np.concatenate(chunks).astype(np.float32), sample_rate

    @staticmethod
    def payloads(prepared: str, voice, model_voice: str = "") -> list[str]:
        """What actually goes to Piper: one raw-phoneme block per sentence.

        ``prepared`` has already been through ``prepare_text``; this does not
        repeat it, because doing so is where a double full stop would come
        from.
        """
        if not prepared:
            return []
        try:
            if voice.coloured:
                # An English model with a French colouring. Piper's phoneme
                # table is per model, so the French-only half of the colouring
                # -- the uvular r above all -- is filtered out here and the
                # accent is the weaker for it. Kokoro does not have that
                # limit; this is the fallback, not the intended path.
                return accent.colour_blocks(
                    prepared,
                    spoken=voice.language,
                    accent_of=voice.accent_of,
                    strength=voice.accent_strength,
                ) or [prepared]
            blocks = accent.phoneme_blocks(
                prepared,
                spoken=voice.language,
                target=voice.model_language,
                strength=voice.accent_strength,
                # A controller speaking their own language is read by the same
                # front end the model was trained with, down to the variety:
                # most of the British voices were built on RP rather than on
                # espeak's default English, and reading them with the default
                # would make them American.
                espeak_voice=model_voice if voice.native else "",
            )
        except Exception:
            log.warning(
                "could not build %s phonemes for a %s voice; falling back to text",
                voice.language, voice.model_language, exc_info=True,
            )
            return [prepared]
        return blocks or [prepared]


__all__ = ["PiperTTS", "DEFAULT_SAMPLE_RATE", "LANGUAGES"]
