"""Chatterbox, behind the backend interface: the tier that clones a voice.

Every other engine here has a catalogue of speakers it was trained with, and a
controller is one of them, shaded. Chatterbox has none. It is given ten
seconds of somebody and speaks as them -- which is what turns an hour of
recorded Heathrow Director into a Heathrow Director who will answer your
call, and is the only way a synthesiser gets *that* voice rather than a voice.

It is the tier above the CPU one, and it is opt-in, for one reason: it costs
what a voice clone costs. Measured on a sixteen-core desktop with eight
threads, the standard model renders a clearance in a little over twice the
time it takes to say it, and the Turbo model in about the time it takes to
say it; Kokoro does the same in a tenth. On a card it is a tenth or better.
So it is never chosen by ``auto`` unless the pilot has said the card is free
(``voice.gpu``), and naming it outright (``voice.engine: chatterbox``) is
taken as knowing what it costs.

**Three models, one interface.** ``standard`` is the default. It is the
original, with classifier-free guidance that keeps it on the text, and it is
the model the reference clips this tier was matched against were made with.
``turbo`` is one pass and no guidance, twice as quick, and half as faithful:
measured over the same eight Heathrow transmissions through the radio chain,
one take of the standard model loses 14% of its words to the recogniser, one
Turbo take 32%, and Turbo at a lower sampling temperature 18% -- while
dragging every line out half as long again, which is not a Heathrow
Director. ``multilingual`` speaks twenty-three languages and is the only one
of the three that can render a French clearance -- so the catalogue admits a
prompt for a language only when the loaded model speaks it, and a French
prompt under an English-only model is a French-accented speaker of English,
which is also a thing a controller can be.

**The prompt is the voice.** ``data/voices/chatterbox/prompts/`` holds one
clip per controller, named ``<accent>_<sex>_<name>.wav``; see
:func:`wilcoatc.audio.voices.chatterbox_catalogue` for what the name means.
The clip has to be more than five seconds -- the model refuses less -- and
ten to fifteen is the useful length. What is in it is what comes out: a clip
taken off an air-band receiver gives a voice with the receiver's bandwidth
already in it, which the radio chain then band-limits again, harmlessly.

**No rate control.** The model has no speed parameter and the whole point of
a clone is that it talks the way the person did, so ``length_scale`` is
accepted and ignored. The casting knows: see
:meth:`wilcoatc.audio.voices.VoiceRegistry.assign`, which shades a clone
neither in rate nor in pitch.

**Determinism.** The model samples, and the interface carries no seed, so one
is derived from the text and the prompt. The same clearance from the same
controller is the same waveform in every session, which is what the
transmission cache and every other layer here already assume.

**The words have to survive.** The model samples, and on a prompt taken off
an air-band receiver -- which is what a clip of a real controller is -- one
take in three comes back with the callsign or a number mumbled. Measured over
the same eight Heathrow transmissions, a single Turbo take loses a third of
its words through the radio chain where Kokoro loses a fourteenth, and a
controller who sounds like a person and says "cleared aisle at the port" is
worse than one who sounds like a synthesiser. So the engine can be handed a
*verifier* -- the program's own recogniser, reading the take back -- and told
how many takes it may try. A take whose words came back is accepted; one that
did not is re-rolled with the next seed, and the best of the lot is what the
pilot hears. This is how the reference clips this tier was matched against
were made, and it is the only way a sampled model is safe on a frequency.

A backend returns a waveform and a rate and does nothing else to it. The
one thing this does beyond the model's own output is trim the silence the
model pads either end with: a keying that fires half a second before the
first word is the operator's to decide, not the engine's.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path

import numpy as np

from ..voices import CHATTERBOX_DIR, VOICE_DIR
from .base import Capabilities

log = logging.getLogger(__name__)

#: Every Chatterbox model renders at this rate.
SAMPLE_RATE = 24000

#: The three models, and what each one needs on disk. ``sentinel`` is the
#: file whose presence means the download finished: the weights are fetched
#: into a staging folder and moved into place as one, so a partial folder is
#: not a state this can be in. ``languages`` is what the casting is allowed
#: to give the model, narrowed to the ones a controller works in here.
VARIANTS: dict[str, dict] = {
    "turbo": dict(
        repo="ResembleAI/chatterbox-turbo",
        files=("ve.safetensors", "t3_turbo_v1.safetensors",
               "t3_turbo_v1.yaml", "s3gen_meanflow.safetensors", "conds.pt",
               "vocab.json", "merges.txt", "tokenizer_config.json",
               "special_tokens_map.json", "added_tokens.json"),
        sentinel="t3_turbo_v1.safetensors",
        languages=frozenset({"en"}),
        size_mb=2100,
    ),
    "standard": dict(
        repo="ResembleAI/chatterbox",
        files=("ve.safetensors", "t3_cfg.safetensors", "s3gen.safetensors",
               "tokenizer.json", "conds.pt"),
        sentinel="t3_cfg.safetensors",
        languages=frozenset({"en"}),
        size_mb=2200,
    ),
    "multilingual": dict(
        repo="ResembleAI/chatterbox",
        files=("ve.pt", "t3_mtl23ls_v2.safetensors", "s3gen.pt",
               "grapheme_mtl_merged_expanded_v1.json", "conds.pt",
               "Cangjie5_TC.json"),
        sentinel="t3_mtl23ls_v2.safetensors",
        languages=frozenset({"en", "fr", "de", "es", "it", "pt", "nl"}),
        size_mb=3300,
    ),
}

#: The model when the config does not say. See the module docstring for
#: the measurement behind the choice.
DEFAULT_VARIANT = "standard"

#: What the standard model is asked for. ``exaggeration`` is its emotion
#: control, where the card's 0.5 is a narrator and 0.4 is somebody reading
#: headings off a strip; ``cfg_weight`` at 0.4 keeps the clone close to the
#: prompt's own pacing. Both listened for on ATC text, not tuned by number.
EXAGGERATION = 0.4
CFG_WEIGHT = 0.4
TEMPERATURE = 0.8

#: The Turbo model's sampling temperature. The card's 0.8; lower is more
#: faithful to the text and measurably slower in delivery, and at 0.5 the
#: lines come out half as long again, which reads as a drawl.
TURBO_TEMPERATURE = 0.8

#: Below this, under the clip's own peak, is the padding the model adds.
TRIM_DB = -45.0
TRIM_PAD_MS = 40.0

#: A take whose read-back loses no more than this fraction of its words is
#: accepted without another roll. On a twenty-word clearance that is three
#: words, which is a mumbled "the" and a filler the recogniser did not write
#: down; a callsign digit gone wrong is one word and is caught by the best-of
#: rule anyway, because a take with one error beats a take with two.
ACCEPT_WER = 0.15

#: How many takes the engine may try when it has a verifier. Two, because on
#: a CPU each take costs about the length of the transmission and the second
#: is only rendered when the first failed; on a card the cost is nothing.
DEFAULT_TAKES = 2


def variant_of(name: str) -> str:
    """The model name from the config, or the default for one it has not
    heard of. Never raises: a typo in a config file is a warning, not a
    frequency nobody answers."""
    key = (name or "").strip().lower()
    if key in VARIANTS:
        return key
    if key:
        log.warning("no Chatterbox model %r; using %s", name, DEFAULT_VARIANT)
    return DEFAULT_VARIANT


def model_dir(root: Path = VOICE_DIR, variant: str = DEFAULT_VARIANT) -> Path:
    """Where one model's weights live: ``data/voices/chatterbox/<model>``."""
    return Path(root) / CHATTERBOX_DIR / variant_of(variant)


def prompt_dir(root: Path = VOICE_DIR) -> Path:
    """Where the reference clips live. One folder for every model, because a
    prompt is a person and a person is not model-specific."""
    return Path(root) / CHATTERBOX_DIR / "prompts"


def installed(root: Path = VOICE_DIR, variant: str = DEFAULT_VARIANT) -> bool:
    """Whether one model's weights are on disk, complete."""
    spec = VARIANTS[variant_of(variant)]
    return (model_dir(root, variant) / spec["sentinel"]).exists()


def _threads() -> int:
    """How many cores to give the model on a CPU.

    Half the machine, at most eight: the CPU tier keeps to four so the
    simulator has the rest, and this tier is opt-in by somebody who has
    accepted that it costs more. ``WILCOATC_TTS_THREADS`` overrides, as it
    does for Kokoro.
    """
    override = os.environ.get("WILCOATC_TTS_THREADS", "").strip()
    if override.isdigit() and int(override) > 0:
        return int(override)
    return max(1, min(8, (os.cpu_count() or 4) // 2))


def _device() -> str:
    """Where the model runs. The card if torch can see one, else the CPU."""
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            return "mps"
    except Exception:                                # pragma: no cover
        pass
    return "cpu"


class ChatterboxTTS:
    """The Chatterbox engine as a registry backend.

    One model, loaded once and kept, plus one set of conditionals per prompt
    the casting has asked for. The conditionals are the expensive half of a
    clone -- the speaker embedding and the prompt's speech tokens -- and a
    cast controller asks for the same ones on every transmission.
    """

    def __init__(self, root: Path | None = None,
                 variant: str = DEFAULT_VARIANT, takes: int = DEFAULT_TAKES):
        self.root = Path(root) if root is not None else VOICE_DIR
        self.variant = variant_of(variant)
        #: How many takes to try, at most, when a verifier is set.
        self.takes = max(1, int(takes))
        #: ``(audio, sample_rate, text) -> word error rate`` of a take read
        #: back, or None to accept the first take unread. Set by the
        #: synthesiser once the program's recogniser exists; the engine does
        #: not import the recogniser, because it is a backend.
        self.verify = None
        self._model = None
        self._device = ""
        self._conds: dict[str, object] = {}
        # Re-entrant, because a transmission holds it for the whole of the
        # model call -- the model keeps its prompt as state, and two
        # controllers rendering at once would swap each other's voices -- and
        # asks for the prompt's conditionals from inside that.
        self._lock = threading.RLock()
        self.capabilities = Capabilities(
            name="chatterbox",
            languages=VARIANTS[self.variant]["languages"],
            needs_gpu=True,
            can_clone=True,
            sample_rate=SAMPLE_RATE,
            tier="hq",
        )

    # ------------------------------------------------------------------

    @property
    def model_dir(self) -> Path:
        return model_dir(self.root, self.variant)

    @property
    def prompt_dir(self) -> Path:
        return prompt_dir(self.root)

    def available(self) -> bool:
        """Weights on disk, at least one prompt, and the package importable.

        None of it by loading anything: this is asked on every selection.
        """
        try:
            from importlib.util import find_spec

            if not installed(self.root, self.variant):
                return False
            if not any(self.prompt_dir.glob("*")):
                return False
            return find_spec("chatterbox") is not None
        except Exception:                            # pragma: no cover
            return False

    def warm(self) -> None:
        """Load the model. Several seconds and a couple of gigabytes, once."""
        self._engine()

    def forget(self) -> None:
        """Drop the model and every prompt, after the folder changed."""
        with self._lock:
            self._model = None
            self._conds.clear()

    # ------------------------------------------------------------------

    def _engine(self):
        with self._lock:
            if self._model is None:
                import torch

                device = _device()
                if device == "cpu":
                    torch.set_num_threads(_threads())
                    log.warning(
                        "Chatterbox (%s) is running on the CPU with %d "
                        "threads; a clearance takes %s to render. Set "
                        "voice.engine to auto for the quick tier.",
                        self.variant, torch.get_num_threads(),
                        "about as long as it takes to say"
                        if self.variant == "turbo"
                        else "a little over twice as long as it takes to say")
                cls = self._class()
                self._model = cls.from_local(str(self.model_dir), device)
                self._device = device
                log.info("chatterbox %s loaded on %s", self.variant, device)
            return self._model

    def _class(self):
        if self.variant == "turbo":
            from chatterbox.tts_turbo import ChatterboxTurboTTS

            return ChatterboxTurboTTS
        if self.variant == "multilingual":
            from chatterbox.mtl_tts import ChatterboxMultilingualTTS

            return ChatterboxMultilingualTTS
        from chatterbox.tts import ChatterboxTTS as Standard

        return Standard

    def _conditionals(self, model, prompt: Path):
        """The clone of one prompt, computed once per session."""
        key = str(prompt)
        with self._lock:
            found = self._conds.get(key)
        if found is None:
            if self.variant == "turbo":
                # The Turbo model has no emotion control to set.
                model.prepare_conditionals(key)
            else:
                model.prepare_conditionals(key, exaggeration=EXAGGERATION)
            found = model.conds
            with self._lock:
                self._conds[key] = found
        return found

    # ------------------------------------------------------------------

    def synthesize(self, text: str, voice, lang: str, *,
                   length_scale: float | None = None,
                   ) -> tuple[np.ndarray, int]:
        """One transmission, as float32 mono at 24 kHz.

        ``length_scale`` is ignored; see the module docstring.
        """
        if not (text or "").strip():
            return np.zeros(0, dtype=np.float32), SAMPLE_RATE

        prompt = self.prompt_dir / str(getattr(voice.spec, "file", "") or "")
        if not prompt.is_file():
            raise FileNotFoundError(f"no voice prompt {prompt.name} in "
                                    f"{self.prompt_dir}")

        import torch

        model = self._engine()
        spoken = (lang or voice.language or "en").lower()[:2]
        if spoken not in self.capabilities.languages:
            spoken = "en"

        # The model samples. Seeded from what is being said and by whom, so
        # the same clearance from the same controller is the same waveform
        # in every session, which the transmission cache assumes. A re-roll
        # is the next seed along, so it is just as repeatable.
        seed = int(hashlib.sha1(f"{prompt.name}|{text}".encode("utf-8"))
                   .hexdigest()[:8], 16)
        rate = int(getattr(model, "sr", SAMPLE_RATE) or SAMPLE_RATE)

        takes = self.takes if self.verify is not None else 1
        best: tuple[float, np.ndarray] | None = None
        for take in range(takes):
            with self._lock:
                conds = self._conditionals(model, prompt)
                model.conds = conds
                torch.manual_seed(seed + take)
                wav = self._generate(model, text, spoken)
            audio = _trim(np.asarray(
                wav.detach().cpu().numpy() if hasattr(wav, "detach") else wav,
                dtype=np.float32).reshape(-1), rate)
            if takes == 1:
                return audio, rate
            try:
                lost = float(self.verify(audio, rate, text))
            except Exception:
                # A verifier that fails is not a reason to fail the
                # transmission: the take is what it is, and it is played.
                log.debug("could not read the take back", exc_info=True)
                return audio, rate
            if best is None or lost < best[0]:
                best = (lost, audio)
            if lost <= ACCEPT_WER:
                break
            log.info("chatterbox take %d lost %.0f%% of its words; %s",
                     take + 1, lost * 100.0,
                     "re-rolling" if take + 1 < takes else "keeping the best")
        return best[1], rate

    def _generate(self, model, text: str, spoken: str):
        if self.variant == "turbo":
            return model.generate(text, temperature=TURBO_TEMPERATURE)
        if self.variant == "multilingual":
            return model.generate(text, language_id=spoken,
                                  exaggeration=EXAGGERATION,
                                  cfg_weight=CFG_WEIGHT,
                                  temperature=TEMPERATURE)
        return model.generate(text, exaggeration=EXAGGERATION,
                              cfg_weight=CFG_WEIGHT, temperature=TEMPERATURE)


def word_error_rate(reference: str, hypothesis: str) -> float:
    """How many words of ``reference`` did not come back, as a fraction.

    Word-level edit distance, on lower-cased words with the punctuation
    taken off and every numeral spelled out a digit at a time. The
    phrasebook says "one two zero" and the recogniser writes "120" for it,
    and a verifier that counted that as three wrong words would re-roll
    every clearance with a number in it, which is every clearance -- the
    first version of this did exactly that, and best-of-two by a wrong
    measure is no better than one take. "niner" is "nine", and a filler is
    not a word: the operator layer puts "uh" in and the recogniser is right
    not to write it down.
    """
    want = _words(reference)
    got = _words(hypothesis)
    if not want:
        return 0.0
    # Levenshtein over words, one row at a time.
    previous = list(range(len(got) + 1))
    for i, w in enumerate(want, start=1):
        current = [i]
        for j, g in enumerate(got, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (0 if w == g else 1)))
        previous = current
    return min(1.0, previous[-1] / float(len(want)))


_DIGIT_WORDS = {"0": "zero", "1": "one", "2": "two", "3": "three",
                "4": "four", "5": "five", "6": "six", "7": "seven",
                "8": "eight", "9": "nine"}
_SAME_WORD = {"niner": "nine", "tree": "three", "fife": "five",
              "fower": "four", "decimal": "point"}
_FILLERS = {"uh", "um", "er", "erm", "euh", "äh", "eh", "ehm"}
_RUNWAY_SIDE = {"l": "left", "r": "right", "c": "centre"}


def _words(text: str) -> list[str]:
    """The comparable words of a transmission: lower case, numerals spelled
    a digit at a time, decimal points kept as a word, fillers dropped."""
    import re

    out: list[str] = []
    for token in re.sub(r"[^\w\s.]", " ", (text or "").lower()).split():
        # "118.5" is "one one eight point five"; a full stop on the end of a
        # word is punctuation and goes.
        token = token.strip(".")
        if not token:
            continue
        pieces = re.findall(r"\d|\.|[a-zß-ÿ]+", token)
        for n, piece in enumerate(pieces):
            if piece == ".":
                out.append("point")
            elif piece.isdigit():
                out.append(_DIGIT_WORDS[piece])
            elif piece in _FILLERS:
                continue
            elif piece in _RUNWAY_SIDE and n and pieces[n - 1].isdigit():
                # "27L" is how the recogniser writes "two seven left".
                out.append(_RUNWAY_SIDE[piece])
            else:
                out.append(_SAME_WORD.get(piece, piece))
    return out


def _trim(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    """Take the model's padding off either end, leaving a little.

    The model puts a few hundred milliseconds of near-silence before the
    first word and after the last. The operator layer decides how long the
    gap after keying up is, and a fixed half second underneath its jitter
    would make every transmission start on the same late beat.
    """
    if audio.size == 0:
        return audio
    peak = float(np.max(np.abs(audio)))
    if peak <= 0.0:
        return audio
    window = max(1, int(sample_rate * 0.01))
    envelope = np.convolve(np.abs(audio), np.ones(window, dtype=np.float32)
                           / window, mode="same")
    loud = np.flatnonzero(envelope > peak * (10.0 ** (TRIM_DB / 20.0)))
    if loud.size == 0:
        return audio
    pad = int(sample_rate * TRIM_PAD_MS / 1000.0)
    start = max(0, int(loud[0]) - pad)
    end = min(audio.size, int(loud[-1]) + pad)
    return np.ascontiguousarray(audio[start:end], dtype=np.float32)


__all__ = ["ACCEPT_WER", "ChatterboxTTS", "DEFAULT_TAKES", "DEFAULT_VARIANT",
           "SAMPLE_RATE", "VARIANTS", "installed", "model_dir", "prompt_dir",
           "variant_of", "word_error_rate"]
