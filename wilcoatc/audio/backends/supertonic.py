"""Supertonic 3, behind the backend interface.

One 400 MB ONNX model, ten speakers, thirty-one languages, and no GPU. That
combination is why it is here: Kokoro speaks five of this program's six
controller languages and has one speaker outside English, so German, Dutch and
the French male have been coming out of Piper -- which is fast, cheap and
audibly a synthesiser from 2021. Supertonic gives those positions a voice that
sounds like a person on the same CPU budget.

**Thirty-two denoising steps, not the eight it defaults to.** The default is
tuned for demos on a phone. Below about sixteen the model buzzes on sibilants
and the buzz survives the radio chain, which band-limits to 300-3400 Hz and
therefore keeps everything a hiss lives in. Thirty-two is where that stops
being audible; going higher costs time and changes nothing that reaches a
speaker.

**French is spoken as English.** Not a typo: the words are respelled by
:mod:`wilcoatc.audio.translit` and handed over at ``lang="en"``. Supertonic
has a French token and it works, in the sense that the vowels are French --
but the delivery is flat, every clause lands on the same pitch, and two
transmissions in it sound like an announcement board. The English path keeps
the prosody and the respelling supplies the sounds. See that module for what
the trade actually costs.

**No accent, and no way to add one.** :mod:`wilcoatc.audio.accent` gives Piper
and Kokoro a foreign accent by substituting phonemes, and Supertonic has no
raw-phoneme input to substitute into -- it takes text. So a Supertonic voice
speaks its own language well and every other one without a trace of where it
is from. :meth:`wilcoatc.audio.voices.VoiceRegistry.pool` is where that is
accounted for: accented English stays with the engines that can do it.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

from ..voices import SUPERTONIC_DIR, SUPERTONIC_RESPELLED, VOICE_DIR
from .base import Capabilities

log = logging.getLogger(__name__)

#: Supertonic 3 renders at 44.1 kHz, well above every other engine here. The
#: radio chain resamples whatever it is given, so this costs a little time and
#: buys nothing above 3400 Hz -- but it is what the model produces and
#: downsampling before the chain would be a second resample for no reason.
SAMPLE_RATE = 44100

#: Measured, not chosen from the documentation. See the module docstring: the
#: packaged default of 8 buzzes on sibilants at radio bandwidth.
TOTAL_STEPS = 32

#: The model's own idea of a natural rate, and the base this converts
#: ``length_scale`` against. Supertonic's ``speed`` runs the opposite way from
#: Piper's ``length_scale`` -- larger is faster, not slower -- so the casting's
#: number is inverted here rather than at the call site.
BASE_SPEED = 1.05

#: What the package will accept. A speed outside this raises rather than
#: clamping, and the casting can hand over a scale near the edge once the
#: radio chain's pitch compensation has been folded in.
MIN_SPEED, MAX_SPEED = 0.7, 2.0

#: The model name in the ``supertonic`` package. Pinned rather than left to
#: the package default so that an upgrade of the package cannot silently move
#: every controller onto a different model.
MODEL = "supertonic-3"

#: The subset of the model's thirty-one languages this program has any use
#: for: the six a controller works in, plus Dutch, which is an accent pool
#: rather than a spoken language here. Declared narrowly on purpose -- the
#: registry chooses from what a backend claims, and claiming Hindi would mean
#: fielding a Hindi transmission this program never composes.
LANGUAGES = frozenset({"en", "fr", "de", "es", "it", "pt", "nl"})

#: Languages handed over as respelled English instead of as themselves.
#: French only, and only because the French token's prosody is worse than the
#: respelling. Every other language reads well under its own token.
#:
#: Defined in :mod:`wilcoatc.audio.voices` rather than here, because the
#: casting has to know it too and the two must not drift. What it costs is not
#: only prosody: this path is an English voice reading French, and it sounds
#: like one, so the casting refuses to prefer Supertonic for a language in
#: this set. See :data:`wilcoatc.audio.voices.SUPERTONIC_RESPELLED`.
RESPELLED = SUPERTONIC_RESPELLED


class SupertonicTTS:
    """The Supertonic engine as a registry backend.

    One model for every voice and every language, so it is loaded once and
    kept -- the same shape as Kokoro and the opposite of Piper. The ten
    speakers are style vectors read from small JSON files beside the model;
    those are cached too, because a cast controller asks for the same one on
    every transmission.
    """

    def __init__(self, model_dir=None):
        #: Where the ONNX files and the voice styles live. Under the voice
        #: folder rather than the package's own ``~/.cache/supertonic3``, so
        #: that an installation stays portable and ``doctor`` can find it.
        self.model_dir = model_dir if model_dir is not None else (
            VOICE_DIR / SUPERTONIC_DIR)
        self._tts = None
        self._styles: dict[str, object] = {}
        self._lock = threading.Lock()
        self.capabilities = Capabilities(
            name="supertonic",
            languages=LANGUAGES,
            needs_gpu=False,
            can_clone=False,
            sample_rate=SAMPLE_RATE,
            tier="cpu",
        )

    # ------------------------------------------------------------------

    def available(self) -> bool:
        """Whether the weights are on disk and the package can be imported.

        Both halves, and neither of them by loading anything: this is asked
        on every selection, with a controller waiting.
        """
        try:
            from importlib.util import find_spec

            onnx = self.model_dir / "onnx"
            if not (onnx / "vocoder.onnx").exists():
                return False
            if not (self.model_dir / "voice_styles").exists():
                return False
            return find_spec("supertonic") is not None
        except Exception:                            # pragma: no cover
            return False

    def warm(self) -> None:
        """Load the model, which the first transmission would otherwise wait for.

        Around two seconds and 400 MB, once, for every Supertonic controller
        in the catalogue.
        """
        self._engine()

    def forget(self) -> None:
        """Drop the model, after the voice folder has changed underneath it."""
        with self._lock:
            self._tts = None
            self._styles.clear()

    # ------------------------------------------------------------------

    def _engine(self):
        """The loaded ``supertonic.TTS``, loaded on first use.

        ``auto_download`` is off. The download is 400 MB and belongs to
        Settings > Components, where there is a progress bar and a pilot who
        is not currently on final -- not to the first time a controller keys
        up. :meth:`available` is what keeps that from being reached.
        """
        with self._lock:
            if self._tts is None:
                from supertonic import TTS

                self._tts = TTS(model=MODEL, model_dir=self.model_dir,
                                auto_download=False)
            return self._tts

    def style(self, name: str):
        """One speaker's style vectors, by catalogue name (``M1`` .. ``F5``)."""
        with self._lock:
            found = self._styles.get(name)
        if found is None:
            found = self._engine().get_voice_style(voice_name=name)
            with self._lock:
                self._styles[name] = found
        return found

    # ------------------------------------------------------------------

    def synthesize(self, text: str, voice, lang: str, *,
                   length_scale: float | None = None,
                   ) -> tuple[np.ndarray, int]:
        """One transmission, as float32 mono, at the model's own rate."""
        if not (text or "").strip():
            return np.zeros(0, dtype=np.float32), SAMPLE_RATE

        engine = self._engine()
        spoken = (lang or voice.language or "en").lower()[:2]

        # French goes in as English spelling. See the module docstring; the
        # respelling itself is translit's, and is a no-op for every other
        # language, so this is one branch rather than a table.
        if spoken in RESPELLED:
            from ..translit import to_english

            text = to_english(text, spoken)
            spoken = "en"

        text = _speakable(engine, text)

        scale = float(length_scale if length_scale is not None
                      else voice.length_scale)
        speed = BASE_SPEED / max(scale, 0.01)
        speed = min(max(speed, MIN_SPEED), MAX_SPEED)

        wav, _duration = engine.synthesize(
            text,
            voice_style=self.style(_speaker(voice)),
            total_steps=TOTAL_STEPS,
            speed=speed,
            lang=spoken if spoken in LANGUAGES else "na",
        )

        # The package returns (1, samples). Everything downstream -- the radio
        # chain, the cache, playback -- is mono and one-dimensional.
        audio = np.asarray(wav, dtype=np.float32).reshape(-1)
        rate = int(getattr(engine, "sample_rate", SAMPLE_RATE) or SAMPLE_RATE)
        return audio, rate


def _speakable(engine, text: str) -> str:
    """Drop the characters the model would refuse the whole sentence over.

    Supertonic validates its input and raises on a character its table does
    not have. Every other engine here reads what it is given, and the caller
    only catches a failed synthesis for Kokoro -- so one stray degree sign in
    a wind check would be a transmission nobody hears rather than a
    transmission with a word missing. Ask first, and drop what it named.

    The check is over the distinct characters of one sentence, which is a few
    dozen at most against thirty-two denoising steps.
    """
    try:
        ok, unsupported = engine.model.text_processor.validate_text(text)
        if ok:
            return text
        log.warning("supertonic cannot say %r; dropping it", "".join(unsupported))
        return "".join(c for c in text if c not in set(unsupported)) or text
    except Exception:                                # pragma: no cover
        # A package that no longer exposes the processor. Better to hand the
        # text over and let it decide than to refuse it here.
        return text


#: The ten built-in speakers. Asked here rather than trusted from the spec,
#: because the fallback path in ``Synthesizer.backend_for`` can route a voice
#: cast for another engine to this one, and a Piper speaker name reaching
#: ``get_voice_style`` is a FileNotFoundError with a clearance already
#: composed.
SPEAKERS = frozenset(f"{sex}{n}" for sex in "MF" for n in range(1, 6))


def _speaker(voice) -> str:
    """Which of the ten built-in speakers renders this cast voice.

    The catalogue carries the name in ``speaker_name`` because a Supertonic
    voice is a speaker plus a language, and several catalogue entries share
    one speaker. Falling back on the sex rather than on a fixed default keeps
    a spec that never had a Supertonic name from casting a woman as a man.
    """
    name = str(getattr(voice.spec, "speaker_name", "") or "").strip()
    if name in SPEAKERS:
        return name
    return "F1" if getattr(voice.spec, "gender", "m") == "f" else "M1"


__all__ = ["SupertonicTTS", "LANGUAGES", "SAMPLE_RATE", "TOTAL_STEPS", "MODEL"]
