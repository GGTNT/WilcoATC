"""What a speech synthesiser has to be, for this program to use it.

There are two synthesisers in the tree today and the plan calls for more: a
CPU tier that every pilot gets, and a GPU tier for the few whose card is not
already fully occupied drawing an aeroplane. Adding one used to mean editing
:mod:`wilcoatc.audio.tts` -- a branch in ``_synthesize``, a branch in
``warm``, a branch in the fallback -- which is three places to get wrong per
engine and a growing pile of ``if engine == ...``.

So an engine is now an object with two questions asked of it: *what can you
speak*, and *speak this*. Everything else -- which controller gets which
voice, the transmission cache, the radio chain -- stays where it is and does
not know how many engines exist.

**What a backend is not allowed to do.** It returns a waveform and a rate. It
does not band-limit, it does not add noise, it does not shift pitch: all of
that is the radio chain in :mod:`wilcoatc.audio.radio_fx`, which is measured
and shared, and an engine that did its own would sound like a different
transmitter. It also does not phonemise on its own initiative where the
pipeline has already done it -- see :mod:`wilcoatc.audio.accent`, which is
what keeps an English word read by a French model from coming back as a
French word.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np

#: A backend that declares this instead of a language set will be considered
#: for any language. Only honest for an engine that phonemises what it is
#: given rather than reading spelling.
ANY_LANGUAGE = "*"


class TTSUnavailable(RuntimeError):
    """No backend can speak this, and there is nothing to fall back to.

    Distinct from a backend that merely failed once: that is retried on the
    next transmission, and this is not.
    """


@dataclass(frozen=True)
class Capabilities:
    """What an engine can do, declared rather than discovered.

    Declared, because the registry has to choose between engines *before*
    loading any of them. Loading a 300 MB model in order to find out it does
    not speak German is exactly the cost this avoids.
    """

    #: Short stable identifier. Goes in the config, the logs and the
    #: diagnostics, so it does not change once it has shipped.
    name: str

    #: ISO-639-1 codes this engine speaks, or ``{ANY_LANGUAGE}``.
    languages: frozenset[str] = field(default_factory=frozenset)

    #: True for an engine that is not worth running without a GPU. The
    #: registry will not choose one of these unless the hardware is there and
    #: the pilot has opted in, because on this program's target machine the
    #: card is already busy with the simulator.
    needs_gpu: bool = False

    #: Whether the engine can be given a reference clip and imitate it. No
    #: casting uses this yet; it is declared so that a backend which has it
    #: can be told apart from one which does not.
    can_clone: bool = False

    #: What the engine returns when it has nothing to say. The real rate comes
    #: back from :meth:`TTSBackend.synthesize` with the audio, because a
    #: multi-model engine does not have one rate.
    sample_rate: int = 24000

    #: ``cpu`` or ``hq``. Ordering between tiers is the registry's, not the
    #: backend's: an engine does not get to declare itself the best one.
    tier: str = "cpu"

    def speaks(self, language: str) -> bool:
        if ANY_LANGUAGE in self.languages:
            return True
        return (language or "en").lower()[:2] in self.languages


@runtime_checkable
class TTSBackend(Protocol):
    """One speech synthesiser.

    ``voice`` is a :class:`~wilcoatc.audio.voices.VoiceAssignment`: the
    casting has already chosen who is speaking and how they are shaded, and
    the backend's job is to render that, not to decide it. ``lang`` is the
    language the *words* are in, which is not always the language the model
    speaks -- a French controller reading an English clearance is the whole
    reason the two are separate arguments.
    """

    capabilities: Capabilities

    def available(self) -> bool:
        """Whether this could run right now: weights on disk, package importable.

        Must be cheap and must not raise. It is asked on every selection, and
        a selection happens while a controller is waiting to speak.
        """
        ...

    def synthesize(self, text: str, voice, lang: str, *,
                   length_scale: float | None = None,
                   ) -> tuple[np.ndarray, int]:
        """One transmission, as float32 mono in [-1, 1], and its sample rate.

        ``length_scale`` is in Piper's units -- larger is slower -- because
        that is what the casting is expressed in and what every existing
        assignment was tuned against. An engine whose own parameter runs the
        other way converts; it does not ask the caller to.
        """
        ...

    def warm(self) -> None:
        """Load what a first transmission would otherwise wait for.

        Optional in effect but not in signature: an engine with nothing to
        pre-load implements it as a no-op rather than making every caller
        check.
        """
        ...


__all__ = ["ANY_LANGUAGE", "Capabilities", "TTSBackend", "TTSUnavailable"]
