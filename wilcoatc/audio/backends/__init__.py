"""Pluggable speech synthesisers.

One module per engine, one interface between them, and a registry that picks
between them per language and per machine. See :mod:`.base` for what an engine
has to be and :mod:`.registry` for how one is chosen.

The engines are imported lazily by :func:`default_registry` rather than at
package import, because importing a backend must not import its model library:
a machine without ``kokoro_onnx`` installed still has to be able to run
``doctor`` and be told so.
"""

from __future__ import annotations

from .base import ANY_LANGUAGE, Capabilities, TTSBackend, TTSUnavailable
from .beep import BeepBackend
from .hardware import gpu_available
from .registry import BackendRegistry


def default_registry(voice_registry=None, prefer: str = "auto",
                     allow_gpu: bool = False,
                     hq_model: str = "standard",
                     hq_takes: int = 2) -> BackendRegistry:
    """The engines this build ships with, in the order they are preferred.

    Supertonic first. Ten speakers of each sex in every language a controller
    works in, on the same CPU budget as the others, against Kokoro's single
    speaker per language outside English -- so wherever it can speak, it is
    both the better voice and the deeper pool. Kokoro next, which still owns
    British English, because Supertonic's ten English speakers are one accent
    and it is the American one. Piper last, and not merely as a fallback: it
    is the widest phoneme-driven catalogue, and phonemes are what
    :mod:`wilcoatc.audio.accent` needs to put a German accent on English.

    Registration order is the registry's last tie-break, so this order *is*
    the preference. It is the order the casting in
    :mod:`wilcoatc.audio.voices` already assumes, and the two must not
    disagree.

    Chatterbox is registered last and above all of them: it is the tier that
    clones a voice, it needs a card or a pilot who has accepted what it costs
    without one, and the registry's rules keep it out of reach until one of
    those is true. Last in registration order because order is the tie-break
    among CPU engines and this is not one.

    The beep is not registered. It is a test instrument, and a frequency that
    answers with a tone is a fault that sounds like a feature.
    """
    from ..voices import SUPERTONIC_DIR
    from .chatterbox import ChatterboxTTS
    from .kokoro import KokoroTTS
    from .piper import PiperTTS
    from .supertonic import SupertonicTTS

    registry = BackendRegistry(prefer=prefer, allow_gpu=allow_gpu)
    root = getattr(voice_registry, "root", None)
    registry.register(SupertonicTTS(root / SUPERTONIC_DIR) if root is not None
                      else SupertonicTTS())
    registry.register(KokoroTTS(root / "kokoro") if root is not None
                      else KokoroTTS())
    registry.register(PiperTTS(voice_registry))
    registry.register(ChatterboxTTS(root, variant=hq_model, takes=hq_takes))
    return registry


__all__ = [
    "ANY_LANGUAGE",
    "BackendRegistry",
    "BeepBackend",
    "Capabilities",
    "TTSBackend",
    "TTSUnavailable",
    "default_registry",
    "gpu_available",
]
