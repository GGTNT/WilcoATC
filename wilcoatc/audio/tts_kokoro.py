"""The synthesis backend that does not sound like a synthesiser.

Piper is a 20 MB VITS model per voice. It renders a clearance in a thirtieth of
the time it takes to say it, which is why the project started on it, and it
sounds like a 20 MB VITS model, which is why this exists. Kokoro is 82M
parameters in one 310 MB ONNX file carrying every voice it has, and the
difference is in exactly the places a listener calls robotic: the vowels, the
joins between words, and the fall at the end of a phrase.

Nothing about the accent work changes. Kokoro takes IPA on the same terms Piper
does and its phoneme table covers every symbol :mod:`wilcoatc.audio.accent`
emits, so the pipeline is untouched -- the English is phonemised as English,
folded through the speaker's inventory, and handed over. This module only swaps
what turns those phonemes into a waveform.

**Speed.** Measured on this machine, four threads, a 17-second sample: 0.18 of
real time against Piper's 0.03. A seven-second clearance takes 1.2 seconds to
render rather than 0.2. That is six times the cost and it is affordable, which
is the whole argument for the switch:

* Every transmission is cached by text and voice, and ATC repeats itself
  relentlessly, so the second "roger" from a given controller is free.
* A clearance is composed before the controller is due to answer, not at the
  moment the audio is needed.
* Even uncached and unprepared, 1.2 seconds is inside the pause a real
  controller leaves before keying up.

An older note in the README put this at 22 times slower and no better on
DNSMOS. The speed was measured single-threaded with an untuned session; the
session built here is four times faster than that. And DNSMOS scores how clean
a signal is, not how human it sounds -- it was built to grade noise
suppressors. It is the wrong instrument for this question and it answered a
different one.

**What is lost.** Kokoro has one speaker per language outside English, and none
at all for German or Dutch. :class:`wilcoatc.audio.voices.VoiceRegistry` keeps
the Piper voices that fill those gaps rather than choosing one engine for
everything, so a German controller and a French male controller both survive.

The model and voice files are release assets on
github.com/thewh1teagle/kokoro-onnx and go in ``data/voices/kokoro``:
``python -m wilcoatc.audio.voices --download``.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import numpy as np

from . import accent
from ..paths import voice_dir

log = logging.getLogger(__name__)

MODEL_DIR = voice_dir() / "kokoro"
MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"

# Kokoro's own sample rate. Everything downstream follows what synthesise
# returns, so this is only the fallback for an empty result.
SAMPLE_RATE = 24000

# Kokoro takes espeak's name for the language. It only uses it to phonemise,
# which this pipeline has already done, so it is passed for the sake of being
# right rather than because anything downstream reads it.
_ESPEAK_LANG: dict[str, str] = {
    "fr": "fr-fr", "en": "en-us", "it": "it", "pt": "pt-br", "es": "es",
}


def _lang_tag(spec) -> str:
    if spec.language == "en":
        return "en-gb" if spec.accent == "gb" else "en-us"
    return _ESPEAK_LANG.get(spec.language, "en-us")

# How long to wait on a full stop and on a comma, in seconds -- but only where
# Kokoro had to split the phonemes into more than one batch, which for a
# transmission this short it almost never does. The pauses inside a clearance
# are the model's own reading of the commas, and they are the right length
# already. Shortening these was tried: over six transmissions it moved the mean
# duration by a sixth of a second and cost 0.02 of the words the recogniser got
# back, so they are left where the library puts them.
SENTENCE_PAUSE = 0.25
CLAUSE_PAUSE = 0.10

# Kokoro's speed parameter is rejected outside this band.
_MIN_SPEED, _MAX_SPEED = 0.5, 2.0


def _threads() -> int:
    """How many cores to give the model.

    Four is the knee of the curve: it is where an average consumer machine
    still has cores left for the simulator, and going to eight buys only
    another quarter. Overridable for a machine that is not average.
    """
    override = os.environ.get("WILCOATC_TTS_THREADS", "").strip()
    if override.isdigit() and int(override) > 0:
        return int(override)
    return max(1, min(4, (os.cpu_count() or 4)))


class KokoroBackend:
    """Renders a :class:`~wilcoatc.audio.voices.VoiceAssignment` with Kokoro.

    Shaped to sit where :meth:`wilcoatc.audio.tts.Synthesizer._synthesize`
    sits, so the caller keeps its cache, its radio chain and its casting, and
    only the waveform underneath changes.
    """

    def __init__(self, model_dir: Path = MODEL_DIR):
        self.model_dir = Path(model_dir)
        self._kokoro = None
        self._styles: dict[str, np.ndarray] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------

    def available(self) -> bool:
        return ((self.model_dir / MODEL_FILE).exists()
                and (self.model_dir / VOICES_FILE).exists())

    def _load(self):
        with self._lock:
            if self._kokoro is None:
                import onnxruntime as ort
                from kokoro_onnx import Kokoro

                # The defaults spawn a thread per core and leave the graph
                # half-optimised, which on a machine also running a flight
                # simulator is slower than asking for less. This is the
                # session the timings in the module docstring were taken on.
                options = ort.SessionOptions()
                options.intra_op_num_threads = _threads()
                options.inter_op_num_threads = 1
                options.graph_optimization_level = (
                    ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                )
                session = ort.InferenceSession(
                    str(self.model_dir / MODEL_FILE),
                    sess_options=options,
                    providers=["CPUExecutionProvider"],
                )
                self._kokoro = Kokoro.from_session(
                    session, str(self.model_dir / VOICES_FILE)
                )
                log.debug("kokoro loaded on %d threads", options.intra_op_num_threads)
            return self._kokoro

    # ------------------------------------------------------------------

    def style(self, spec) -> np.ndarray:
        """The style vector for one catalogue voice.

        A Kokoro style vector is the whole of a speaker: timbre, rate, and
        where they put the stress. Half of one plus half of another is a third
        person, which is how the catalogue gets six American men out of a model
        that ships three good ones. See
        :data:`wilcoatc.audio.voices.KOKORO_CATALOGUE`.
        """
        cached = self._styles.get(spec.key)
        if cached is not None:
            return cached

        kokoro = self._load()
        names = tuple(spec.blend) or (spec.name,)
        try:
            vectors = [np.asarray(kokoro.get_voice_style(n), dtype=np.float32)
                       for n in names]
        except KeyError as exc:
            raise ValueError(f"Kokoro has no speaker {exc}") from exc

        if len(vectors) == 1:
            blended = vectors[0]
        else:
            weight = float(np.clip(spec.blend_weight, 0.0, 1.0))
            rest = (1.0 - weight) / max(1, len(vectors) - 1)
            blended = vectors[0] * weight
            for vector in vectors[1:]:
                blended = blended + vector * rest
        blended = np.ascontiguousarray(blended, dtype=np.float32)
        self._styles[spec.key] = blended
        return blended

    # ------------------------------------------------------------------

    def synthesize(self, text: str, assignment,
                   length_scale: float | None = None) -> tuple[np.ndarray, int]:
        """One transmission, as float32 mono at Kokoro's rate.

        ``length_scale`` is Piper's units, where larger is slower; it carries
        the caller's compensation for the pitch shift the radio chain does by
        resampling. Kokoro's ``speed`` is its reciprocal.
        """
        blocks = self.phonemes(text, assignment)
        if not blocks:
            return np.zeros(0, dtype=np.float32), SAMPLE_RATE

        kokoro = self._load()
        style = self.style(assignment.spec)
        scale = float(length_scale if length_scale is not None
                      else assignment.length_scale or 1.0)
        speed = float(np.clip(1.0 / max(scale, 1e-3), _MIN_SPEED, _MAX_SPEED))
        lang = _lang_tag(assignment.spec)

        chunks: list[np.ndarray] = []
        rate = SAMPLE_RATE
        for phonemes in blocks:
            # One call per sentence, not one per transmission: Kokoro inserts
            # its own pause between the batches it splits, and letting it split
            # a sentence it has not been given the end of puts a breath in the
            # middle of a clearance.
            audio, rate = kokoro.create(
                phonemes, voice=style, speed=speed, lang=lang,
                is_phonemes=True, sentence_pause=SENTENCE_PAUSE,
                clause_pause=CLAUSE_PAUSE,
            )
            chunks.append(np.asarray(audio, dtype=np.float32))
            chunks.append(np.zeros(int(SENTENCE_PAUSE * rate), dtype=np.float32))
        if chunks:
            chunks.pop()            # no trailing pause on the last sentence
        if not chunks:
            return np.zeros(0, dtype=np.float32), int(rate)
        return np.concatenate(chunks).astype(np.float32), int(rate)

    # ------------------------------------------------------------------

    @staticmethod
    def phonemes(text: str, assignment) -> list[str]:
        """The IPA handed to the model, one block per sentence.

        Two ways a controller gets a foreign accent, and Kokoro can do both.
        Colouring keeps an English voice -- which owns the words and their
        rhythm -- and moves only the sounds a French speaker actually misses,
        including the uvular r, which is reachable here because Kokoro's
        phoneme table is one table for every language it speaks. Substitution
        puts the words through a model of the speaker's own language instead,
        and is what a controller heard in passing on another frequency gets.
        """
        if assignment.coloured:
            blocks = accent.colour_blocks(
                text,
                spoken=assignment.language,
                accent_of=assignment.accent_of,
                strength=assignment.accent_strength,
                can_say=accent.SHARED_INVENTORY,
            )
        else:
            blocks = accent.phoneme_blocks(
                text,
                spoken=assignment.language,
                target=assignment.model_language,
                strength=assignment.accent_strength,
                # A controller speaking their own language is read by the
                # front end their voice was trained with, down to the variety.
                # Kokoro's British speakers were trained on British phonemes,
                # and handing them the American ones puts an r on the end of
                # "four" and "radar" -- which turns an RP controller into
                # something from the west of England.
                espeak_voice=(accent.native_voice(assignment.language,
                                                  assignment.spec.accent)
                              if assignment.native else ""),
            )
        # Piper wants its raw phonemes wrapped in [[ ]] and Kokoro does not.
        out: list[str] = []
        for block in blocks:
            body = block[2:-2] if block.startswith("[[") else block
            body = body.strip()
            if body:
                out.append(body)
        return out

    def warm(self) -> None:
        """Load the model and run one inference, so the first clearance is not
        the one that pays for it."""
        kokoro = self._load()
        kokoro.create("hˈɛloʊ.", voice=self.style(_WARM_SPEC), speed=1.0,
                      lang="en-us", is_phonemes=True)


class _Warm:
    """The cheapest possible stand-in for a spec, for :meth:`KokoroBackend.warm`."""

    key = "kokoro-warm"
    name = "am_michael"
    blend = ("am_michael",)
    blend_weight = 1.0


_WARM_SPEC = _Warm()

# Kept for callers that predate the catalogue: the one Kokoro speaker per
# language, by ISO code.
SPEAKERS: dict[str, str] = {
    "fr": "ff_siwis", "en": "bm_george", "it": "if_sara",
    "pt": "pf_dora", "es": "ef_dora",
}

__all__ = ["KokoroBackend", "SPEAKERS", "MODEL_DIR", "SAMPLE_RATE"]
