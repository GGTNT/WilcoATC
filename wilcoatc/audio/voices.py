"""Controller voice catalogue and assignment.

Two problems have to be solved together:

*Variety* -- a flight that talks to Delivery, Ground, Tower, Departure and
Center should hear five different people, not one voice five times.

*Consistency* -- Kennedy Tower must sound like the same controller on every
flight, forever. Nothing breaks the illusion faster than the same facility
answering in a different voice each time you tune it.

Both fall out of deriving the voice from a stable hash of the facility and
position, rather than picking at random. The hash also selects small pitch and
rate offsets, which multiplies a modest set of base voices into a much larger
set of distinguishable controllers.

There are four catalogues, because there are four synthesisers, and
:meth:`VoiceRegistry.available` layers them rather than choosing one.

Supertonic leads: ten speakers of each sex, every one of whom speaks all six
controller languages, so it covers nearly the whole map on its own. Kokoro
sits under it and still owns British English, because Supertonic's ten English
speakers are one accent and it is the American one. Piper sits under both, is
about six times cheaper per second of speech, and sounds like a synthesiser --
it is kept because it and Kokoro are the only engines here that take phonemes,
and phonemes are how a German controller gets a German accent in English.
``voice.engine`` in the config overrides all of it.

Above all three sits Chatterbox, which has no catalogue of its own: it clones
whoever is in a reference clip, so its voices are whatever clips are in the
prompt folder. It is opt-in -- it costs a great deal more per second than the
others -- and where it is in use a clone takes a position only when the clip's
accent is exactly the one the field asks for. A Heathrow controller is not
cast at Frankfurt because nothing closer was found.

Piper's quality is capped at its "medium" tier where it is still used. The
radio effect chain band-limits everything to roughly 300-3400 Hz, so the extra
detail a "high" model produces is filtered away before it reaches the speakers,
while still costing memory and synthesis time.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

import requests

from ..paths import command_hint, voice_dir

# Called with (what is being fetched, bytes so far, bytes expected). A total of
# zero means the server would not say how big it is.
Progress = Optional[Callable[[str, int, int], None]]

class VoicesMissing(RuntimeError):
    """Not one controller voice is on disk, so nobody can be cast.

    Its own class rather than a bare RuntimeError because the panel treats it
    differently from every other synthesis failure: this one is not a fault,
    it is a download that has not happened yet, and the answer to it is a
    button rather than a bug report.

    The message is written for a pilot who double-clicked an executable.
    Telling that person to run ``python -m wilcoatc.audio.voices --download``
    -- which is what this used to say -- is telling them to install Python in
    order to use a program that ships without it.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        super().__init__(
            "The controller voices have not been downloaded yet. "
            "Open Settings, then Components, and press Download."
        )

    def where(self) -> str:
        """The detail for the log and the diagnostics, not for the panel."""
        return (f"no voice models in {self.root} -- "
                f"download them in Settings > Components, "
                f"or run: {command_hint('setup')}")


HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
VOICE_DIR = voice_dir()

# Kokoro ships as one model plus one file holding every speaker in it. Both
# are release assets rather than a package, so they are fetched by name.
KOKORO_BASE = ("https://github.com/thewh1teagle/kokoro-onnx/releases/download"
               "/model-files-v1.1")
KOKORO_DIR = "kokoro"
KOKORO_MODEL = "kokoro-v1.0.onnx"
KOKORO_VOICES = "voices-v1.0.bin"

# Supertonic ships as a Hugging Face repository rather than as named release
# assets: four ONNX graphs, a unicode table and one small JSON per speaker.
# They are fetched as a set, at the revision the installed package pins, so
# the folder is either whole or absent -- which is what lets one file stand
# for the install below.
SUPERTONIC_DIR = "supertonic3"
SUPERTONIC_REPO = "Supertone/supertonic-3"
SUPERTONIC_MODEL = "supertonic-3"
#: The file whose presence means the download finished. Any of the four would
#: do: the package downloads to a temporary folder and moves it into place, so
#: a partial install is not a state this can be in.
SUPERTONIC_SENTINEL = "onnx/vocoder.onnx"

# Chatterbox: the weights live one folder per model under this, and the
# reference clips a controller is cloned from live in ``prompts`` beside them.
# See :mod:`wilcoatc.audio.backends.chatterbox`.
CHATTERBOX_DIR = "chatterbox"


@dataclass(frozen=True)
class VoiceSpec:
    """One synthesis voice, with the metadata needed to cast it as a controller.

    Two engines produce these. A Piper spec names a model file of its own and
    is downloaded on its own. A Kokoro spec names a speaker -- or a blend of
    two -- inside the single model file every Kokoro voice shares, so its
    ``path`` is that file and installing one installs all of them.
    """

    key: str            # "en_US-ryan-medium", or "kokoro-am_michael"
    locale: str         # "en_US"
    name: str           # "ryan"
    quality: str        # "medium"
    gender: str         # "m" / "f"
    accent: str         # "us" / "gb" / "fr" / "de" ...
    core: bool = False  # part of the minimal download set
    # Multi-speaker models hold several distinct voices in one file.
    speaker_id: int | None = None
    speaker_name: str = ""
    # Which synthesiser renders this voice: "piper", "kokoro", "supertonic"
    # or "chatterbox".
    engine: str = "piper"
    # Kokoro only: the speaker names to mix, and how much of the first.
    # One name and a weight of 1.0 is a plain Kokoro speaker.
    blend: tuple[str, ...] = ()
    blend_weight: float = 1.0
    # Chatterbox only: the reference clip, by file name, in the prompt folder.
    file: str = ""

    @property
    def language(self) -> str:
        """ISO code of the language this model actually speaks."""
        return self.locale.split("_")[0]

    @property
    def filename(self) -> str:
        if self.engine == "kokoro":
            return KOKORO_MODEL
        if self.engine == "supertonic":
            return SUPERTONIC_SENTINEL
        if self.engine == "chatterbox":
            return self.file
        return f"{self.locale}-{self.name}-{self.quality}.onnx"

    @property
    def url(self) -> str:
        if self.engine == "kokoro":
            return f"{KOKORO_BASE}/{KOKORO_MODEL}"
        if self.engine == "supertonic":
            # A repository, not a file. It is fetched by
            # :func:`download_supertonic`, which is why nothing here composes
            # a per-voice URL: ten speakers share one download.
            return SUPERTONIC_REPO
        if self.engine == "chatterbox":
            # Nothing to fetch: the voice is a clip somebody put in a folder.
            return ""
        lang = self.locale.split("_")[0]
        return f"{HF_BASE}/{lang}/{self.locale}/{self.name}/{self.quality}/{self.filename}"

    def path(self, root: Path = VOICE_DIR) -> Path:
        if self.engine == "kokoro":
            return root / KOKORO_DIR / KOKORO_MODEL
        if self.engine == "supertonic":
            return root / SUPERTONIC_DIR / SUPERTONIC_SENTINEL
        if self.engine == "chatterbox":
            return root / CHATTERBOX_DIR / "prompts" / self.file
        return root / self.filename


# The catalogue. "core" voices are enough to run every position with distinct
# voices; the rest widen the pool so a busy area does not repeat itself.
CATALOGUE: tuple[VoiceSpec, ...] = (
    # --- US male ---
    VoiceSpec("en_US-ryan-medium",      "en_US", "ryan",       "medium", "m", "us", core=True),
    VoiceSpec("en_US-joe-medium",       "en_US", "joe",        "medium", "m", "us", core=True),
    VoiceSpec("en_US-hfc_male-medium",  "en_US", "hfc_male",   "medium", "m", "us", core=True),
    VoiceSpec("en_US-bryce-medium",     "en_US", "bryce",      "medium", "m", "us"),
    VoiceSpec("en_US-norman-medium",    "en_US", "norman",     "medium", "m", "us"),
    VoiceSpec("en_US-kusal-medium",     "en_US", "kusal",      "medium", "m", "us"),
    VoiceSpec("en_US-john-medium",      "en_US", "john",       "medium", "m", "us"),
    # --- US female ---
    VoiceSpec("en_US-lessac-medium",    "en_US", "lessac",     "medium", "f", "us", core=True),
    VoiceSpec("en_US-amy-medium",       "en_US", "amy",        "medium", "f", "us", core=True),
    VoiceSpec("en_US-hfc_female-medium","en_US", "hfc_female", "medium", "f", "us", core=True),
    VoiceSpec("en_US-kristin-medium",   "en_US", "kristin",    "medium", "f", "us"),
    # --- UK male ---
    VoiceSpec("en_GB-alan-medium",      "en_GB", "alan",       "medium", "m", "gb", core=True),
    VoiceSpec("en_GB-northern_english_male-medium",
              "en_GB", "northern_english_male", "medium", "m", "gb"),
    # --- UK female ---
    VoiceSpec("en_GB-jenny_dioco-medium", "en_GB", "jenny_dioco", "medium", "f", "gb", core=True),
    VoiceSpec("en_GB-cori-medium",        "en_GB", "cori",        "medium", "f", "gb"),
    VoiceSpec("en_GB-alba-medium",        "en_GB", "alba",        "medium", "f", "gb"),

    # --- French. These serve twice over: they speak French, and they speak
    # English with a French accent, which is what a controller at Orly
    # actually sounds like when switching for a foreign crew.
    #
    # There is a fourth French voice in Piper's set, fr_FR-tom-medium, and it
    # is deliberately not here. Scored for naturalness it comes out 0.2 below
    # these in French and 0.2 below them in accented English, at every
    # synthesis setting tried, and its overall score is 0.4 down -- which is a
    # lot for a mean-opinion scale. A fourth voice is not worth a third of the
    # French positions sounding worse than the rest.
    VoiceSpec("fr_FR-siwis-medium", "fr_FR", "siwis", "medium", "f", "fr", core=True),
    VoiceSpec("fr_FR-upmc-medium",  "fr_FR", "upmc",  "medium", "f", "fr",
              core=True, speaker_id=0, speaker_name="jessica"),
    VoiceSpec("fr_FR-upmc-medium",  "fr_FR", "upmc",  "medium", "m", "fr",
              core=True, speaker_id=1, speaker_name="pierre"),

    # --- other European accents, for English spoken by a local controller ---
    VoiceSpec("de_DE-thorsten-medium", "de_DE", "thorsten", "medium", "m", "de"),
    VoiceSpec("de_DE-kerstin-low",     "de_DE", "kerstin",  "low",    "f", "de"),
    VoiceSpec("de_DE-ramona-low",      "de_DE", "ramona",   "low",    "f", "de"),
    VoiceSpec("es_ES-davefx-medium",   "es_ES", "davefx",   "medium", "m", "es"),
    VoiceSpec("es_ES-sharvard-medium", "es_ES", "sharvard", "medium", "f", "es"),
    VoiceSpec("it_IT-riccardo-x_low",  "it_IT", "riccardo", "x_low",  "m", "it"),
    VoiceSpec("it_IT-paola-medium",    "it_IT", "paola",    "medium", "f", "it"),
    VoiceSpec("nl_NL-ronnie-medium",   "nl_NL", "ronnie",   "medium", "m", "nl"),
    VoiceSpec("nl_NL-pim-medium",      "nl_NL", "pim",      "medium", "m", "nl"),
    VoiceSpec("pt_BR-faber-medium",    "pt_BR", "faber",    "medium", "m", "pt"),
)

# --------------------------------------------------------------------------
# the Kokoro catalogue
# --------------------------------------------------------------------------
#
# Kokoro is one 82M model holding every speaker it has, so this catalogue costs
# a single download rather than 60 MB per voice. What it buys is the thing the
# project was short of: speech that reads as a person rather than as a
# synthesiser. See :mod:`wilcoatc.audio.tts_kokoro`.
#
# Not every Kokoro speaker is here. The model card grades them by how much
# training data each one had, and the difference between the top of that list
# and the bottom is audible -- a D-grade speaker mumbles the ends of words,
# which on a radio is the difference between a readback and a "say again". So
# the graded-C-and-better speakers are used, and the pool is widened by
# *blending* two of them rather than by reaching further down the list.
#
# A blend is a weighted mean of two speakers' style vectors. Kokoro's style
# vector is the whole of the speaker's identity -- timbre, rate, how they place
# stress -- so the mean of two is a third person who sounds like neither, at
# the quality of the two they came from. That is what keeps five controllers at
# one airport sounding like five people without casting anyone weak.


def _kk(name: str, locale: str, gender: str, accent: str, *,
        core: bool = False, blend: tuple[str, ...] = (),
        weight: float = 1.0, label: str = "") -> VoiceSpec:
    """One Kokoro controller: a speaker, or a blend of two."""
    return VoiceSpec(
        key=f"kokoro-{name}", locale=locale, name=name, quality="kokoro",
        gender=gender, accent=accent, core=core, engine="kokoro",
        blend=blend or (name,), blend_weight=weight,
        speaker_name=label or name.replace("_", " ").title(),
    )


# The English pool keeps the shape the Piper one had -- seven American men to
# four women, four British men to three -- because that ratio was not an
# accident of what Piper published. Controlling is still a male-majority trade,
# and a tower that answers in a woman's voice half the time is a different kind
# of wrong from a robotic one.
KOKORO_CATALOGUE: tuple[VoiceSpec, ...] = (
    # --- US male ---
    _kk("am_michael", "en_US", "m", "us", label="Michael"),
    _kk("am_fenrir",  "en_US", "m", "us", label="Fenrir"),
    _kk("am_puck",    "en_US", "m", "us", label="Puck"),
    _kk("us_m_hale",  "en_US", "m", "us", blend=("am_michael", "am_fenrir"),
        weight=0.55, label="Hale"),
    _kk("us_m_reeve", "en_US", "m", "us", blend=("am_puck", "am_onyx"),
        weight=0.65, label="Reeve"),
    _kk("us_m_dwyer", "en_US", "m", "us", blend=("am_fenrir", "am_puck"),
        weight=0.45, label="Dwyer"),
    _kk("us_m_kane",  "en_US", "m", "us", blend=("am_michael", "am_eric"),
        weight=0.7, label="Kane"),
    # --- US female ---
    _kk("af_heart",   "en_US", "f", "us", label="Heart"),
    _kk("af_bella",   "en_US", "f", "us", label="Bella"),
    _kk("af_nicole",  "en_US", "f", "us", label="Nicole"),
    _kk("us_f_shea",  "en_US", "f", "us", blend=("af_bella", "af_kore"),
        weight=0.55, label="Shea"),
    # --- UK male ---
    _kk("bm_george",  "en_GB", "m", "gb", label="George"),
    _kk("bm_fable",   "en_GB", "m", "gb", label="Fable"),
    _kk("gb_m_arden", "en_GB", "m", "gb", blend=("bm_george", "bm_lewis"),
        weight=0.6, label="Arden"),
    _kk("gb_m_ovett", "en_GB", "m", "gb", blend=("bm_fable", "bm_daniel"),
        weight=0.65, label="Ovett"),
    # --- UK female ---
    _kk("bf_emma",     "en_GB", "f", "gb", label="Emma"),
    _kk("bf_isabella", "en_GB", "f", "gb", label="Isabella"),
    _kk("gb_f_wren",   "en_GB", "f", "gb", blend=("bf_emma", "bf_alice"),
        weight=0.65, label="Wren"),

    # --- the languages Kokoro speaks besides English. One speaker each,
    # which is the model's limit and not a choice: blending towards a voice of
    # the other gender moves the pitch by a few hertz and nothing else, so a
    # French male controller is not available here. Piper's French voices stay
    # in the pool for exactly that reason -- see VoiceRegistry.available.
    _kk("ff_siwis", "fr_FR", "f", "fr", label="Siwis"),
    _kk("if_sara",   "it_IT", "f", "it", label="Sara"),
    _kk("im_nicola", "it_IT", "m", "it", label="Nicola"),
    _kk("ef_dora",   "es_ES", "f", "es", label="Dora"),
    _kk("em_alex",   "es_ES", "m", "es", label="Alex"),
    _kk("pf_dora",   "pt_BR", "f", "pt", label="Dora"),
    _kk("pm_alex",   "pt_BR", "m", "pt", label="Alex"),
)

# Kokoro has no German or Dutch speaker. Supertonic has both, and takes those
# languages; a German or Dutch controller's accented *English* is rendered by
# a model of their own language through the phoneme layer, which Supertonic
# has no input for, so that stays on Piper. The registry layers the three
# catalogues rather than choosing between them.
KOKORO_LANGUAGES: frozenset[str] = frozenset(
    spec.language for spec in KOKORO_CATALOGUE
)


# --------------------------------------------------------------------------
# the Supertonic catalogue
# --------------------------------------------------------------------------
#
# Supertonic 3 is one model holding ten speakers, and every one of them speaks
# all thirty-one of its languages. That is not how either of the other two
# engines works and it changes what a catalogue entry means: a Piper voice is
# a file, a Kokoro voice is a speaker, and a Supertonic voice is a speaker
# *and* a language. So this catalogue is the cross product, and there is no
# per-language selection to defend -- the same ten people say everything.
#
# It also means the catalogue costs nothing to widen. Ten voices per language
# is a deeper pool than either other engine offers outside English, which is
# most of what fixes the thing German and Dutch fields have always had wrong:
# three voices covering seven positions.
#
# What it does not give is an accent. See
# :mod:`wilcoatc.audio.backends.supertonic` and :meth:`VoiceRegistry.pool`.

#: The ten built-in speakers, in the package's own naming. Five of each,
#: which is the whole set; there is no quality grading to select from the way
#: there is with Kokoro.
SUPERTONIC_SPEAKERS: tuple[tuple[str, str], ...] = (
    ("M1", "m"), ("M2", "m"), ("M3", "m"), ("M4", "m"), ("M5", "m"),
    ("F1", "f"), ("F2", "f"), ("F3", "f"), ("F4", "f"), ("F5", "f"),
)

#: The languages this program casts Supertonic for, and the locale and accent
#: pool each one belongs to. Deliberately the six a controller speaks plus
#: Dutch -- not the model's full thirty-one, which would put Hindi voices in a
#: catalogue nothing can ever reach.
#:
#: The English entries use the American pool because these speakers have no
#: regional variety to divide: one accent, ten people. A British field is
#: still cast from Kokoro, which does have both.
SUPERTONIC_LOCALES: tuple[tuple[str, str], ...] = (
    ("en_US", "us"), ("fr_FR", "fr"), ("de_DE", "de"), ("es_ES", "es"),
    ("it_IT", "it"), ("pt_BR", "pt"), ("nl_NL", "nl"),
)

#: Languages Supertonic does not speak, but reads.
#:
#: French only, and the mechanism is in
#: :mod:`wilcoatc.audio.backends.supertonic`: the words are respelled by
#: :mod:`wilcoatc.audio.translit` and handed to the model at ``lang="en"``,
#: because Supertonic's own French token has flat prosody. What comes back is
#: an English voice reading "ohtohreezay ah ahttehrreer", and it sounds like
#: one -- which is fine as the only way to reach a French *male*, and wrong as
#: the way to render French at every position in the country.
#:
#: So the preference does not apply here. Kokoro and Piper have real French
#: models; where they have a speaker, they win, and Supertonic keeps only the
#: genders they cannot fill. Exactly the same shape as the accent rule below,
#: and for the same reason: an engine does not get preferred for work it
#: cannot actually do.
SUPERTONIC_RESPELLED: frozenset[str] = frozenset({"fr"})


def _st(speaker: str, gender: str, locale: str, accent: str) -> VoiceSpec:
    """One Supertonic controller: a speaker, in one language."""
    return VoiceSpec(
        key=f"supertonic-{locale.split('_')[0]}-{speaker}",
        locale=locale, name=speaker, quality="supertonic",
        gender=gender, accent=accent, engine="supertonic",
        # The backend reads the speaker off here rather than off the key,
        # because several entries share one speaker and the key does not.
        speaker_name=speaker,
    )


SUPERTONIC_CATALOGUE: tuple[VoiceSpec, ...] = tuple(
    _st(speaker, gender, locale, accent)
    for locale, accent in SUPERTONIC_LOCALES
    for speaker, gender in SUPERTONIC_SPEAKERS
)

SUPERTONIC_LANGUAGES: frozenset[str] = frozenset(
    spec.language for spec in SUPERTONIC_CATALOGUE
)

# --------------------------------------------------------------------------
# the Chatterbox catalogue
# --------------------------------------------------------------------------
#
# There is none, in the sense the other three have one. Chatterbox clones the
# person in a reference clip, so its voices are whatever clips are in the
# prompt folder, and the catalogue is read off the folder each time the
# registry looks. That is deliberate: a pilot who has a recording of their
# home field's tower adds a controller by dropping a file in, and never edits
# a table.
#
# The file name carries what a catalogue entry would: ``<accent>_<sex>_<name>``
# -- ``gb_f_heathrow_director.wav`` is a British woman, and "heathrow
# director" is what the panel calls her. A file that does not follow the
# convention is still a voice, an American man, so that a stray clip is one
# odd controller rather than a folder the program refuses.

#: Where each accent's own language is spoken, for a model that can speak it.
_ACCENT_LOCALE: dict[str, str] = {
    "us": "en_US", "gb": "en_GB", "fr": "fr_FR", "de": "de_DE", "es": "es_ES",
    "it": "it_IT", "pt": "pt_BR", "nl": "nl_NL",
}

#: What a reference clip may be. Anything librosa reads, in practice; these
#: are the ones anybody would actually put in the folder.
PROMPT_SUFFIXES: frozenset[str] = frozenset({".wav", ".flac", ".mp3", ".ogg"})


def chatterbox_catalogue(root: Path = VOICE_DIR,
                         languages: frozenset[str] | None = None,
                         ) -> tuple[VoiceSpec, ...]:
    """The voices in the prompt folder, as catalogue entries.

    ``languages`` is what the loaded model speaks. A clip whose accent
    belongs to a language the model has -- a French clip under the
    multilingual model -- is a French speaker and is cast for French; the
    same clip under the English-only Turbo model is a French-accented speaker
    of English, which is also a thing a controller can be, and is cast for
    that. The prompt is the same file either way.
    """
    spoken = languages or frozenset({"en"})
    folder = Path(root) / CHATTERBOX_DIR / "prompts"
    if not folder.is_dir():
        return ()
    specs: list[VoiceSpec] = []
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() not in PROMPT_SUFFIXES or not path.is_file():
            continue
        stem = path.stem
        parts = stem.split("_", 2)
        accent, gender, label = "us", "m", stem
        if len(parts) == 3 and parts[0].lower() in _ACCENT_LOCALE \
                and parts[1].lower() in ("m", "f"):
            accent, gender, label = parts[0].lower(), parts[1].lower(), parts[2]
        locale = _ACCENT_LOCALE[accent]
        if locale.split("_")[0] not in spoken:
            # A speaker of English with that accent, under a model that has
            # no other language to give them.
            locale = f"en_{accent.upper()}"
        specs.append(VoiceSpec(
            key=f"chatterbox-{stem}", locale=locale, name=stem,
            quality="chatterbox", gender=gender, accent=accent,
            engine="chatterbox", file=path.name,
            speaker_name=label.replace("_", " ").replace("-", " ").title(),
        ))
    return tuple(specs)


BY_KEY = {(v.key, v.speaker_id): v
          for v in CATALOGUE + KOKORO_CATALOGUE + SUPERTONIC_CATALOGUE}

#: What language a voice named in the settings speaks. The catalogue is the
#: only place that knows, and the crew have to ask: a first officer pinned to
#: a German voice is a request for a German first officer, and a Supertonic
#: key carries the language nowhere else -- its ten speakers are the same ten
#: people in every language, so ``supertonic-de-M3`` and ``supertonic-en-M3``
#: differ in nothing but this.
_LANGUAGE_OF: dict[str, str] = {
    v.key: v.language
    for v in CATALOGUE + KOKORO_CATALOGUE + SUPERTONIC_CATALOGUE
}


def language_of(voice_key: str) -> str:
    """The language a named voice speaks, or "" for a name nothing has.

    A cloned voice is not in the tables -- it is a file in a folder -- so a
    ``chatterbox-`` key is answered off the file name's accent instead.
    """
    if voice_key.startswith("chatterbox-"):
        accent = voice_key[len("chatterbox-"):].split("_", 1)[0].lower()
        locale = _ACCENT_LOCALE.get(accent, "en_US")
        return locale.split("_")[0]
    return _LANGUAGE_OF.get((voice_key or "").strip(), "")


def identity(spec: VoiceSpec) -> str:
    """One person, where a key is not enough to name them.

    A multi-speaker Piper model is one key for several people --
    ``fr_FR-upmc-medium`` is a man and a woman -- so avoiding a voice by its
    key alone would avoid both of them.
    """
    if spec.speaker_id is None:
        return spec.key
    return f"{spec.key}#{spec.speaker_id}"


def in_language(voice_key: str, language: str) -> str:
    """The same speaker as a named voice, speaking another language.

    Only Supertonic has one: its ten speakers are the same ten people in
    every language, so ``supertonic-fr-F4`` asked for English is
    ``supertonic-en-F4``. Every other voice is one model of one language and
    has no such person, and the answer is "".
    """
    key = (voice_key or "").strip()
    if language_of(key) == language:
        return key
    if key.startswith("supertonic-"):
        speaker = key.rsplit("-", 1)[-1]
        other = f"supertonic-{language}-{speaker}"
        if _LANGUAGE_OF.get(other) == language:
            return other
    return ""


def gender_of(voice_key: str) -> str:
    """Whether a named voice is a man or a woman, or "" if nothing knows."""
    key = (voice_key or "").strip()
    for spec in CATALOGUE + KOKORO_CATALOGUE + SUPERTONIC_CATALOGUE:
        if spec.key == key:
            return spec.gender
    if key.startswith("chatterbox-"):
        parts = key[len("chatterbox-"):].split("_")
        if len(parts) > 1 and parts[1] in ("m", "f"):
            return parts[1]
    return ""


# --------------------------------------------------------------------------
# accent regions
# --------------------------------------------------------------------------

def accent_for(ident: str) -> str:
    """Pick the accent pool for a facility's ICAO identifier.

    The regions are defined once, in :mod:`wilcoatc.atc.language`, because the
    accent of a controller's English and the language they work in are two
    answers to the same question about where they are.
    """
    from ..atc.language import accent_for_ident

    return accent_for_ident(ident)


def _stable_hash(text: str) -> int:
    """A hash that does not change between runs, unlike Python's ``hash``."""
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


# --------------------------------------------------------------------------
# assignment
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class VoiceAssignment:
    """A specific controller's voice: which model, and how it is shaded."""

    spec: VoiceSpec
    length_scale: float   # >1 slower, <1 faster
    pitch_semitones: float
    noise_scale: float
    noise_w: float
    # The language this assignment is for, and how heavily the controller's
    # own accent shows when it is not the one their voice model speaks.
    language: str = "en"
    accent_strength: float = 0.0
    # The controller's own language, when their accent is being carried by an
    # English model rather than by a model of their own language. Empty for
    # every other case. See :func:`wilcoatc.audio.accent.colour_blocks`.
    accent_of: str = ""

    @property
    def key(self) -> str:
        return self.spec.key

    @property
    def speaker_id(self) -> int | None:
        """Which voice inside a multi-speaker model, or None for single-speaker."""
        return self.spec.speaker_id

    @property
    def model_language(self) -> str:
        """The language the model itself was trained on."""
        return self.spec.language

    @property
    def native(self) -> bool:
        """Whether this controller is speaking their own language."""
        if self.accent_of:
            return False
        return self.language == self.spec.language

    @property
    def coloured(self) -> bool:
        """Whether the accent comes from colouring rather than from the model.

        Two ways to give a controller a foreign accent. The old one puts the
        words through a model of *their* language, which is faithful and
        costs the nine phonemes English has and French does not -- enough that
        "without your takeoff clearance" stops surviving a readback. The new
        one keeps an English model, which owns the words and their rhythm, and
        moves only the sounds a French speaker actually misses.

        The second needs a model whose phoneme table is shared across
        languages, because the uvular r is not in any English model's. Kokoro
        has one; Piper does not, and falls back to the part of the colouring
        that fits inside English.
        """
        return bool(self.accent_of)


# The accents whose colouring exists, is tested, and has been listened to.
# Adding one is a table in :mod:`wilcoatc.audio.accent` plus an entry here;
# until both are done the controller keeps the older rendering, which is worse
# but is not a guess.
COLOURED_ACCENTS: frozenset[str] = frozenset({"fr"})


# Controllers do not all talk at the same speed. Ground and Delivery read long
# clearances briskly; Tower is clipped; Center is relaxed. These are multipliers
# on Piper's length_scale, where larger is slower.
_POSITION_RATE: dict[str, float] = {
    "DEL": 0.97,
    "GND": 0.95,
    "TWR": 0.90,
    # Radar controllers talk quickest of anyone: measured over an hour of
    # Heathrow Director, four to five words a second inside a transmission,
    # against three and a half from the engines at their natural rate. Kokoro
    # holds its naturalness score to about seventeen per cent faster than
    # natural and falls off past that, so this is set to reach the recording
    # at the quick end of the shading without leaving that band.
    "DEP": 0.91,
    "APP": 0.90,
    "CTR": 0.96,
    "ATIS": 1.02,
    "CTAF": 1.00,
    "INFO": 1.00,
    # Nobody in this aeroplane is fighting for a gap on a busy frequency, so
    # none of them talk at controller speed. A cabin announcement is the
    # slowest thing you will hear all flight.
    "FO": 1.00,
    "CAPTAIN": 1.05,
    "PURSER": 1.07,
    "CABIN": 1.07,
}


# Slot each position takes in a facility's shuffled voice pool. Positions the
# pilot hears in the same flight get consecutive slots, so they are always
# different people. Approach/Departure/Center start at 0 as well, but they are
# drawn from a different facility's permutation ("New York", not "Kennedy").
_POSITION_SLOT: dict[str, int] = {
    "DEL": 0, "GND": 1, "TWR": 2, "ATIS": 3,
    "APP": 0, "DEP": 1, "CTR": 2,
    "CTAF": 4, "INFO": 5, "AWOS": 6,
    # Every aeroplane draws slot 0 of its own permutation, which is keyed on
    # the callsign: the aeroplanes differ from each other because the
    # permutations differ, not because the slots do.
    "PILOT": 0,
    # The people in this aeroplane. Consecutive slots, because you hear all
    # four of them on one flight and two of them sounding alike is worse
    # here than anywhere else -- these are the voices closest to you.
    "FO": 0, "CAPTAIN": 1, "PURSER": 2, "CABIN": 3,
}

# Positions split into two tiers that are staffed by different people even when
# they share a name. At Boston the tower cab and the TRACON are both called
# "Boston", so without this discriminator Boston Delivery and Boston Approach
# would draw the same slot from the same permutation and sound identical.
_POSITION_TIER: dict[str, str] = {
    "DEL": "local", "GND": "local", "TWR": "local", "ATIS": "local",
    "CTAF": "local", "AWOS": "local",
    "APP": "radar", "DEP": "radar", "CTR": "radar", "INFO": "radar",
    # Not a controller at all: the crew of another aeroplane on the frequency.
    # Its own tier so an airline crew never draws the same permutation as the
    # controller they are talking to and answers itself in the same voice.
    "PILOT": "aircraft",
    # Your own crew, in a tier of their own for the same reason again: the
    # first officer must not turn out to be the tower controller.
    "FO": "crew", "CAPTAIN": "crew", "PURSER": "crew", "CABIN": "crew",
}


class _Shuffler:
    """A tiny reproducible shuffler.

    ``random.Random`` would do, but its algorithm is not contractually stable
    across Python versions and these assignments have to survive an upgrade.
    """

    def __init__(self, seed: int):
        self.state = seed & 0xFFFFFFFFFFFFFFFF or 0x9E3779B97F4A7C15

    def next(self) -> int:
        # xorshift64*
        x = self.state
        x ^= (x >> 12) & 0xFFFFFFFFFFFFFFFF
        x ^= (x << 25) & 0xFFFFFFFFFFFFFFFF
        x ^= (x >> 27) & 0xFFFFFFFFFFFFFFFF
        self.state = x & 0xFFFFFFFFFFFFFFFF
        return (self.state * 0x2545F4914F6CDD1D) & 0xFFFFFFFFFFFFFFFF

    def shuffled(self, items: list) -> list:
        out = list(items)
        for i in range(len(out) - 1, 0, -1):
            j = self.next() % (i + 1)
            out[i], out[j] = out[j], out[i]
        return out



class VoiceRegistry:
    """Assigns a stable voice to every controller position."""

    def __init__(self, root: Path = VOICE_DIR, seed: str = "wilcoatc",
                 engine: str = "auto", hq: bool = False,
                 hq_model: str = "standard"):
        self.root = Path(root)
        self.seed = seed
        # auto       -- Supertonic where it can speak, then Kokoro, then Piper
        # kokoro     -- Kokoro only, and nothing at all where it cannot speak
        # supertonic -- Supertonic only, likewise
        # piper      -- the old catalogue, whatever else is installed
        # chatterbox -- the cloned voices wherever a clip matches the field's
        #               accent, and the auto order everywhere else
        self.engine = (engine or "auto").strip().lower()
        #: Whether ``auto`` may reach the cloning tier. The synthesiser says
        #: yes when the pilot has said the card is free and there is one.
        self.hq = bool(hq)
        self.hq_model = hq_model
        self._cache: dict[str, VoiceAssignment] = {}
        self._available: list[VoiceSpec] | None = None

    def kokoro_installed(self) -> bool:
        """Whether Kokoro can actually be used: the two files, and the package.

        Both halves are checked, and the package half is checked without
        importing it, because the failure it prevents is the expensive one.
        Casting a controller from a catalogue whose engine cannot be loaded
        does not fail here -- it fails when they first key up, mid-flight,
        with a clearance already composed.
        """
        base = self.root / KOKORO_DIR
        if not ((base / KOKORO_MODEL).exists()
                and (base / KOKORO_VOICES).exists()):
            return False
        from importlib.util import find_spec

        try:
            return find_spec("kokoro_onnx") is not None
        except (ImportError, ValueError):        # pragma: no cover
            return False

    def supertonic_installed(self) -> bool:
        """Whether Supertonic can be used: the weights, and the package.

        The same two halves, and for the same reason, as
        :meth:`kokoro_installed`.
        """
        base = self.root / SUPERTONIC_DIR
        if not (base / SUPERTONIC_SENTINEL).exists():
            return False
        if not (base / "voice_styles").exists():
            return False
        from importlib.util import find_spec

        try:
            return find_spec("supertonic") is not None
        except (ImportError, ValueError):        # pragma: no cover
            return False

    def chatterbox_installed(self) -> bool:
        """Whether the cloning tier can be used: the weights, at least one
        reference clip, and the package. Nothing is loaded to find out."""
        from importlib.util import find_spec

        from .backends.chatterbox import installed

        if not installed(self.root, self.hq_model):
            return False
        if not chatterbox_catalogue(self.root):
            return False
        try:
            return find_spec("chatterbox") is not None
        except (ImportError, ValueError):        # pragma: no cover
            return False

    def cloning(self) -> bool:
        """Whether the cloned voices are in the cast at all."""
        return self.engine == "chatterbox" or (self.engine == "auto"
                                               and self.hq)

    def hq_missing(self) -> bool:
        """Asked for the cloning tier and cannot have it. Said once by the
        engine, because the controllers will not be the voices the pilot
        set up."""
        return self.engine == "chatterbox" and not self.chatterbox_installed()

    def available(self) -> list[VoiceSpec]:
        """Voices actually present on disk, best engine first.

        Supertonic leads. Ten speakers of each sex in every language a
        controller works in, against Kokoro's one speaker per language outside
        English, and it is the better voice at both ends of that comparison.
        So where it has a (language, gender, accent) the other two are not
        needed for it, and are dropped.

        Two things survive that on purpose, and both are capability rather
        than taste:

        *British English.* Supertonic's ten English speakers are one accent,
        and it is the American one -- see :data:`SUPERTONIC_LOCALES`. A UK
        field cast from them would answer every call in an American voice,
        which is a worse error than the one this preference is fixing. The
        accent is part of the key, so ``gb`` is a gap and Kokoro fills it.

        *Every foreign accent pool.* A German controller speaking English is
        rendered by substituting phonemes into the words, and Supertonic takes
        text. Kokoro and Piper both take phonemes, so both are kept for any
        accent that is not natively English, whatever Supertonic covers -- and
        :meth:`pool` is what decides that a controller speaking their own
        language still gets Supertonic.

        Under Supertonic the older order is unchanged: Kokoro, then Piper for
        the pairs Kokoro has no speaker for.
        """
        if self._available is not None:
            return self._available

        clones: list[VoiceSpec] = []
        if self.cloning() and self.chatterbox_installed():
            from .backends.chatterbox import VARIANTS, variant_of

            clones = list(chatterbox_catalogue(
                self.root, VARIANTS[variant_of(self.hq_model)]["languages"]))

        piper = [v for v in CATALOGUE if v.path(self.root).exists()]
        # Under the cloning tier the ordinary cast is the ``auto`` one: the
        # clones cover the accents they have clips for and everything else is
        # answered as it would be without them.
        ordinary = self.engine in ("auto", "chatterbox")
        kokoro: list[VoiceSpec] = []
        if (ordinary or self.engine == "kokoro") and self.kokoro_installed():
            kokoro = list(KOKORO_CATALOGUE)
        supertonic: list[VoiceSpec] = []
        if (ordinary or self.engine == "supertonic") \
                and self.supertonic_installed():
            supertonic = list(SUPERTONIC_CATALOGUE)

        if self.engine == "piper":
            found = piper
        elif self.engine == "kokoro":
            found = kokoro or piper
        elif self.engine == "supertonic":
            found = supertonic or piper
        else:
            # Under Supertonic the older order holds: Kokoro, then Piper for
            # the pairs Kokoro has no speaker for.
            #
            # Except in a language Supertonic only respells. There the whole
            # pool is whatever genuinely speaks it, and that is three voices
            # in French against Supertonic's ten -- so Piper is kept even
            # where Kokoro has the pair, because a country whose every
            # position is one of two people is its own kind of wrong. A Piper
            # Frenchwoman is a worse voice than a Kokoro one and is still a
            # Frenchwoman.
            by_kokoro = {(v.language, v.gender) for v in kokoro}
            beneath = kokoro + [
                v for v in piper
                if (v.language, v.gender) not in by_kokoro
                or v.language in SUPERTONIC_RESPELLED
            ]
            # A language Supertonic reads by respelling is a language
            # Supertonic does not speak, so it displaces nothing there. See
            # SUPERTONIC_RESPELLED.
            covered = {(v.language, v.gender, v.accent) for v in supertonic
                       if v.language not in SUPERTONIC_RESPELLED}
            found = supertonic + [
                v for v in beneath
                if (v.language, v.gender, v.accent) not in covered
                or v.accent not in self._ENGLISH_ACCENTS
            ]

        # The clones go on top of whichever order was chosen and displace
        # nothing here: a clone is only ever cast where its accent is exactly
        # the one asked for, and :meth:`pool` is where that is decided.
        self._available = clones + found
        return self._available

    def invalidate(self) -> None:
        """Forget what is installed, after a download."""
        self._available = None
        self._cache.clear()

    # When a region's own voices are not installed, fall back to the accent
    # that is closest rather than to a US voice: a Dutch controller sounds far
    # more like a British one than like an American one.
    _ACCENT_FALLBACK: dict[str, tuple[str, ...]] = {
        "fr": ("fr", "gb", "us"),
        "de": ("de", "nl", "gb", "us"),
        "nl": ("nl", "de", "gb", "us"),
        "es": ("es", "it", "gb", "us"),
        "it": ("it", "es", "gb", "us"),
        "pt": ("pt", "es", "gb", "us"),
        "gb": ("gb", "us"),
        "us": ("us", "gb"),
    }

    #: Accents an English model has natively. Everything else in the pool
    #: table is a foreign accent that has to be produced rather than spoken.
    _ENGLISH_ACCENTS: frozenset[str] = frozenset({"us", "gb"})

    def pool(self, accent: str, gender: str | None = None,
             language: str = "", clones: bool = True) -> list[VoiceSpec]:
        """Voices plausible for one accent, optionally able to speak a language.

        ``language`` constrains the model itself: a French transmission has to
        come out of a French model, because the phonemiser follows the model's
        language. English has no such constraint, which is exactly what lets a
        French model speak accented English.

        This is also where Supertonic is told apart from the two engines
        :meth:`available` deliberately keeps under it. Two rules, and they
        follow from one fact about the engine -- it reads text, and
        :mod:`wilcoatc.audio.accent` works on phonemes:

        *A controller speaking their own language* gets Supertonic wherever it
        has that language, because it is the better voice by a wide margin and
        has ten speakers where Kokoro has one.

        *A controller speaking English with a foreign accent* does not, and
        cannot: the accent is made by substituting phonemes into the words,
        there is no phoneme input to substitute into, and a Supertonic German
        would speak unaccented English. That transmission stays with Kokoro or
        Piper, which sound worse and are the only engines here still
        recognisably at Frankfurt.
        """
        available = self.available()
        if language and language != "en":
            available = [v for v in available if v.language == language]

        # A cloned voice is a particular person with a particular accent, and
        # is cast only where that accent is exactly the one asked for. The
        # fallback table below finds the nearest accent when the exact one has
        # no voices; a Heathrow controller is not the nearest thing to a
        # Frankfurt one, however the table reads, so the clones never go
        # through it. Where there is an exact match they take the whole pool,
        # the way Supertonic does below: the point of setting the tier up is
        # to hear it.
        cloned = [v for v in available
                  if v.engine == "chatterbox" and v.accent == accent]
        available = [v for v in available if v.engine != "chatterbox"]
        if clones and cloned:
            found = cloned
        else:
            for candidate in self._ACCENT_FALLBACK.get(accent, (accent, "gb", "us")):
                found = [v for v in available if v.accent == candidate]
                if found:
                    break
            else:
                found = available

        if not found:
            found = available or self.available()

        if (language or "en") == "en" and accent not in self._ENGLISH_ACCENTS:
            # Faking an accent. Supertonic cannot, so it stands down.
            found = [v for v in found if v.engine != "supertonic"] or found
        elif (language or "en") in SUPERTONIC_RESPELLED:
            # French. Supertonic does not speak it -- it reads a respelling of
            # it in an English voice -- so the engines with a real French
            # model win, and Supertonic keeps only the genders they have no
            # speaker for. In practice that is the French male, which is the
            # gap it was brought in to fill in the first place.
            real = {(v.language, v.gender)
                    for v in found if v.engine != "supertonic"}
            found = [v for v in found
                     if v.engine != "supertonic"
                     or (v.language, v.gender) not in real] or found
        else:
            # Not faking one, so the preference applies: Supertonic takes
            # every (language, gender) pair it has, and the other two keep
            # only what it does not cover. Both of them, not just Piper --
            # Kokoro's one Italian man is not a reason to cast an Italian man
            # from Kokoro when there are five of them here.
            better = {(v.language, v.gender)
                      for v in found if v.engine == "supertonic"}
            found = [v for v in found
                     if v.engine == "supertonic"
                     or (v.language, v.gender) not in better] or found

        if gender:
            found = [v for v in found if v.gender == gender] or found
        return found

    def _permutation(self, facility: str, accent: str, tier: str = "local",
                     language: str = "", clones: bool = True) -> list[VoiceSpec]:
        """A stable shuffle of the accent pool, unique to one facility.

        Positions are then taken from successive slots, which guarantees that
        every position at a facility gets a different voice. Hashing the
        position directly does not: it collides often enough that Ground and
        Tower at the same field regularly came out as the same person, which is
        the single most noticeable way this can sound wrong.
        """
        pool = self.pool(accent, language=language, clones=clones)
        seed = _stable_hash(f"{self.seed}|perm|{tier}|{facility}")
        rng = _Shuffler(seed)
        return rng.shuffled(pool)

    def pin(self, assignment: VoiceAssignment,
            voice_key: str) -> VoiceAssignment | None:
        """The same shading, on a voice the pilot picked by name.

        Casting is otherwise automatic and stable, which is right for a
        controller: nobody chooses who is working Kennedy Tower. Your own
        first officer is different -- you are going to be listening to them
        for four hours, and being able to say "not that one" is worth the one
        place where the casting can be overridden.

        Returns None when the named voice is not installed, so the caller
        keeps the automatic assignment rather than falling silent.
        """
        wanted = voice_key.strip()
        if not wanted or wanted in ("auto", assignment.key):
            return None
        for spec in self.available():
            if spec.key != wanted:
                continue
            if assignment.language != "en" and spec.language != assignment.language:
                # A voice that cannot speak the language being spoken is not
                # a choice, it is a different sentence.
                return None
            return spec_assignment(assignment, spec)
        return None

    def assign(self, facility: str, position: str, ident: str = "",
               language: str = "en", gender: str = "", *,
               accent: str = "",
               avoid: tuple[str, ...] = ()) -> VoiceAssignment:
        """The voice for a given controller, stable across runs.

        ``facility`` is the spoken name ("Kennedy", "New York"), ``position`` is
        DEL/GND/TWR/..., and ``ident`` is the owning airport, used only to pick
        the accent pool.

        ``accent`` names the pool outright, for a speaker who has no airport.
        The crew were cast with their callsign as the ``ident``, and a
        callsign read as an ICAO prefix is a place picked at random: SWR came
        out Portuguese and THY Spanish, and the crew's English was then put
        through a Spanish model with its phonemes substituted -- which is the
        foreign accent nobody on board could place.

        ``avoid`` is voices somebody else is already speaking in. The slots
        keep an automatic cast apart from itself, but not from a voice the
        pilot pinned: a first officer pinned to the voice slot 1 would have
        drawn made the captain the same person.

        The permutation is keyed on the facility rather than the airport
        because a TRACON serves several fields: New York Approach has to sound
        like the same controller whether you are departing Kennedy, LaGuardia
        or Newark.
        """
        language = (language or "en").lower()[:2]
        gender = gender if gender in ("m", "f") else ""
        avoid = tuple(sorted(k for k in avoid if k))
        cache_key = (f"{ident}|{facility}|{position}|{language}|{gender}"
                     f"|{accent}|{','.join(avoid)}")
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        available = self.available()
        if not available:
            raise VoicesMissing(self.root)

        # The hash deliberately excludes the language: the same controller
        # keeps the same slot whichever language they are speaking.
        #
        # That used to mean the same *voice* as well. It no longer does at a
        # facility whose accent is coloured: the French is rendered by a French
        # model and the English by an English one, so a controller who switches
        # languages mid-session changes timbre. It is a real regression and it
        # is deliberate, because the alternative -- one model for both -- is
        # what made the English unreadable. A backend with one model and one
        # phoneme table for every language would give back both at once; see
        # :mod:`wilcoatc.audio.tts_kokoro`.
        h = _stable_hash(f"{self.seed}|{ident}|{facility}|{position}")
        accent = accent or accent_for(ident or facility)

        # A controller whose English is rendered by colouring an English voice
        # rather than by putting the words through a model of their own
        # language. Only where the colouring has been written and measured;
        # everywhere else the old path stands.
        colour_of = ""
        if language == "en" and accent in COLOURED_ACCENTS:
            colour_of = accent
            accent = "gb"

        tier = _POSITION_TIER.get(position, "local")
        # A coloured controller is an English voice with the accent painted
        # on, and the pool it draws from is the British one. A clone in that
        # pool is a real British person, which is exactly who must not answer
        # at Orly -- so the clones stand down wherever the accent is being
        # produced rather than spoken.
        order = self._permutation(facility, accent, tier, language,
                                  clones=not colour_of)
        if gender:
            # Asked for a man or a woman: keep the permutation, drop everyone
            # who is not one. The order is preserved, so the same slot still
            # picks the same person as long as the pool has not changed.
            narrowed = [v for v in order if v.gender == gender]
            if narrowed:
                order = narrowed
        if avoid:
            order = [v for v in order
                     if v.key not in avoid and identity(v) not in avoid] or order
        slot = _POSITION_SLOT.get(position, 7)
        spec = order[slot % len(order)]

        # Shading: +-6% rate and +-1.5 semitones, quantised so that two
        # controllers rarely land on near-identical settings.
        rate = _POSITION_RATE.get(position, 1.0) * (1.0 + ((h >> 8) % 13 - 6) * 0.01)
        pitch = ((h >> 16) % 13 - 6) * 0.25
        if position == "ATIS":
            rate *= 1.02          # ATIS loops are read a touch slower
            pitch *= 0.4          # and flatter
        if spec.engine == "chatterbox":
            # A clone is a particular person, and the shading exists to make
            # several people out of one voice. Shifting the pitch would make
            # them somebody else, and the engine has no rate to shade.
            pitch = 0.0
        # Piper's own defaults are 0.667 and 0.8. Measured against the
        # recogniser, the accented voices come back cleaner a little below
        # both, and noticeably worse above them: more duration noise on a
        # phoneme sequence the model has not seen before is more chance for
        # it to come apart. The shading stays inside the band that measured
        # well rather than spanning the default.
        noise = 0.50 + ((h >> 24) % 7) * 0.02       # 0.50 - 0.62
        noise_w = 0.62 + ((h >> 32) % 7) * 0.026    # 0.62 - 0.78

        # Speaking a language that is not your own is slower, and how much of
        # your own accent shows through varies from person to person. Both are
        # derived from the same stable hash, so a given controller's English is
        # as good today as it was on your last flight.
        strength = 0.0
        if colour_of:
            # The colouring scale is not the substitution scale. It runs over
            # a narrower band because it is not choosing how much of the word
            # to throw away, only how much of the speaker shows through: at
            # 0.45 the r and the r-coloured vowels, by 0.66 the flattened
            # diphthongs and the accent on the last syllable.
            strength = round(0.45 + ((h >> 40) % 8) * 0.03, 3)
            rate *= 1.03 + strength * 0.03
        elif language != spec.language:
            strength = round(0.25 + ((h >> 40) % 8) * 0.09, 3)
            rate *= 1.04 + strength * 0.05

        assignment = VoiceAssignment(
            spec=spec,
            length_scale=round(rate, 4),
            pitch_semitones=round(pitch, 3),
            noise_scale=round(noise, 3),
            noise_w=round(noise_w, 3),
            language=language,
            accent_strength=strength,
            accent_of=colour_of,
        )
        self._cache[cache_key] = assignment
        return assignment


def spec_assignment(assignment: VoiceAssignment,
                    spec: VoiceSpec) -> VoiceAssignment:
    """The same assignment on a different model.

    The accent colouring has to be recomputed: it exists because the model
    does not speak the language the words are in, and a pinned model may well
    speak it. Everything else -- rate, pitch, the noise shaping -- is the
    person rather than the model, and is kept.

    A pinned model that does *not* speak it is given the accent that
    :meth:`VoiceRegistry.assign` would have given it. Without that, naming a
    German voice for a crew speaking English changed nothing audible at all:
    the substitution layer is driven by the strength, the automatic casting
    had no reason to set one, and the setting quietly did nothing.
    """
    language = assignment.language
    native = spec.language == language
    strength = assignment.accent_strength
    if not native and not strength:
        # The middle of the band assign() draws from. A pinned voice is one
        # the pilot chose rather than one a hash landed on, so there is no
        # per-controller variation to preserve here.
        strength = 0.55
    return replace(
        assignment,
        spec=spec,
        accent_of=assignment.accent_of if not native else "",
        accent_strength=0.0 if native else strength,
    )


# --------------------------------------------------------------------------
# download
# --------------------------------------------------------------------------


def download_kokoro(root: Path = VOICE_DIR, force: bool = False,
                    on_progress: Progress = None) -> list[Path]:
    """Fetch the one Kokoro model and the file holding all of its speakers.

    Around 350 MB, once, for every Kokoro controller in the catalogue.
    """
    base = root / KOKORO_DIR
    base.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for name in (KOKORO_MODEL, KOKORO_VOICES):
        target = base / name
        if target.exists() and not force and target.stat().st_size > 0:
            out.append(target)
            continue
        _download(f"{KOKORO_BASE}/{name}", target, label=name,
                  on_progress=on_progress)
        out.append(target)
    return out


def download_supertonic(root: Path = VOICE_DIR, force: bool = False,
                        on_progress: Progress = None) -> list[Path]:
    """Fetch the Supertonic model: four ONNX graphs and ten speaker files.

    Around 400 MB, once, for every Supertonic controller in the catalogue.

    Fetched file by file rather than through the package's own one-call
    downloader, which reports to a terminal nobody installing this has open.
    The panel needs a number per file to draw a bar with, and that is the only
    reason this is not three lines.
    """
    import shutil

    base = root / SUPERTONIC_DIR
    if (base / SUPERTONIC_SENTINEL).exists() and not force:
        return [base / SUPERTONIC_SENTINEL]

    try:
        from huggingface_hub import hf_hub_download, list_repo_files
    except ImportError as exc:                       # pragma: no cover
        # Not something a pilot can hit: both packages are in the shipped
        # build. It is what a checkout with an incomplete install gets, and a
        # sentence beats a traceback from three frames down.
        raise RuntimeError(
            "The Supertonic voices need the 'supertonic' package. "
            "Install the requirements and try again."
        ) from exc

    # The package pins the revision each of its releases was tested against,
    # which is worth honouring: a model that changed under a fixed package
    # would re-cast every controller it renders. Without the package there is
    # nothing to ask, and the head of the repository is the best guess left.
    try:
        from supertonic.config import get_model_revision

        revision = get_model_revision(SUPERTONIC_MODEL)
    except Exception:                                # pragma: no cover
        revision = "main"

    wanted = [name for name in list_repo_files(SUPERTONIC_REPO, revision=revision)
              if name.startswith(("onnx/", "voice_styles/"))]

    # Into a temporary folder and moved into place, so that a download
    # interrupted halfway does not leave a folder the engine believes in.
    # SUPERTONIC_SENTINEL only means "complete" because of this.
    staging = base.parent / f".{base.name}.part"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    for index, name in enumerate(wanted, start=1):
        hf_hub_download(SUPERTONIC_REPO, name, revision=revision,
                        local_dir=str(staging))
        if on_progress is not None:
            on_progress(name, index, len(wanted))
        else:
            print(f"\r  {name:46} {index}/{len(wanted)}", end="")

    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staging), str(base))
    if on_progress is None:
        print(f"\r  {SUPERTONIC_MODEL:46} done  {len(wanted)} files")
    return [base / name for name in wanted]


def download_chatterbox(root: Path = VOICE_DIR, variant: str = "standard",
                        force: bool = False,
                        on_progress: Progress = None) -> list[Path]:
    """Fetch one Chatterbox model: two to three gigabytes, once.

    The same shape as :func:`download_supertonic` and for the same reasons:
    file by file so the panel has a number to draw, into a staging folder so
    that an interrupted download is a missing folder rather than a folder
    the engine believes in. The reference clips are not downloaded -- they
    are the pilot's own, or the ones a checkout ships beside the program --
    so an install with the weights and no clips is complete and silent, and
    :meth:`VoiceRegistry.chatterbox_installed` says so.
    """
    import shutil

    from .backends.chatterbox import VARIANTS, model_dir, variant_of

    variant = variant_of(variant)
    spec = VARIANTS[variant]
    base = model_dir(root, variant)
    if (base / spec["sentinel"]).exists() and not force:
        return [base / spec["sentinel"]]

    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:                       # pragma: no cover
        raise RuntimeError(
            "The cloned voices need the 'chatterbox-tts' package. "
            "Install the optional requirements and try again."
        ) from exc

    staging = base.parent / f".{base.name}.part"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    wanted = list(spec["files"])
    for index, name in enumerate(wanted, start=1):
        hf_hub_download(spec["repo"], name, local_dir=str(staging))
        if on_progress is not None:
            on_progress(name, index, len(wanted))
        else:
            print(f"\r  {name:46} {index}/{len(wanted)}", end="")

    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staging), str(base))
    (root / CHATTERBOX_DIR / "prompts").mkdir(parents=True, exist_ok=True)
    if on_progress is None:
        print(f"\r  chatterbox {variant:35} done  {len(wanted)} files")
    return [base / name for name in wanted]


def download_voices(
    which: str = "core", root: Path = VOICE_DIR, force: bool = False,
    on_progress: Progress = None, hq_model: str = "standard",
) -> list[Path]:
    """Fetch voice models. ``which`` is ``core``, ``all``, or a comma-separated list.

    ``core`` now means Kokoro plus the Piper voices that fill its gaps -- the
    German ones, and the French male. That is the smallest install that can
    work every position, in every language, in the better voice wherever there
    is one.

    Supertonic is **not** in ``core``. It is a second 400 MB download on top of
    Kokoro's 350, it replaces voices that already work rather than adding a
    language that does not, and doubling the first-run download is not a thing
    to do to a pilot on the strength of a bench. It comes with ``all``, or on
    its own as ``supertonic``.

    Chatterbox is in neither. Two gigabytes and a tier that costs more per
    second than the card is likely to have spare, it comes only by name, as
    ``chatterbox``, with ``hq_model`` saying which of its models.
    """
    root.mkdir(parents=True, exist_ok=True)

    fetched: list[Path] = []
    if which == "supertonic":
        return download_supertonic(root, force=force, on_progress=on_progress)
    if which in ("chatterbox", "hq"):
        return download_chatterbox(root, variant=hq_model, force=force,
                                   on_progress=on_progress)
    if which in ("core", "all", "kokoro"):
        fetched = download_kokoro(root, force=force, on_progress=on_progress)
        if which == "kokoro":
            return fetched
    if which == "all":
        fetched += download_supertonic(root, force=force,
                                       on_progress=on_progress)

    if which == "core":
        # The Piper voices Kokoro cannot stand in for. Everything else in the
        # old core set is now cast from the Kokoro catalogue instead.
        covered = {(v.language, v.gender) for v in KOKORO_CATALOGUE}
        wanted = [v for v in CATALOGUE
                  if v.core and (v.language, v.gender) not in covered]
    elif which == "all":
        wanted = list(CATALOGUE)
    else:
        keys = {k.strip().lower() for k in which.split(",") if k.strip()}
        wanted = [
            v for v in CATALOGUE
            if v.key.lower() in keys or v.name.lower() in keys
            or v.language in keys or v.accent in keys
        ]
        if not wanted:
            raise SystemExit(
                f"No voices matched {which!r}. Try a language (fr, de, es, it, nl), "
                "an accent (us, gb), a voice name, or --all."
            )

    out: list[Path] = list(fetched)
    seen: set[str] = set()
    for spec in wanted:
        if spec.filename in seen:
            continue          # two speakers can share one model file
        seen.add(spec.filename)
        for suffix in ("", ".json"):
            url = spec.url + suffix
            target = root / (spec.filename + suffix)
            if target.exists() and not force and target.stat().st_size > 0:
                continue
            _download(url, target, label=target.name, on_progress=on_progress)
        out.append(spec.path(root))

    if on_progress is None:
        _report(root)
    return out


def _report(root: Path) -> None:
    """What is now installed, counted in controllers rather than in files.

    Kokoro is one file holding a whole catalogue, so counting files would say
    "one voice" for the twenty-five people it casts.
    """
    available = VoiceRegistry(root).available()
    counts: dict[str, int] = {}
    for spec in available:
        counts[spec.engine] = counts.get(spec.engine, 0) + 1
    files = (list(root.glob("*.onnx")) + list((root / KOKORO_DIR).glob("*"))
             + list((root / SUPERTONIC_DIR).rglob("*")))
    total = sum(f.stat().st_size for f in files if f.is_file()) / 1e6
    by_engine = ", ".join(f"{count} {name.title()}"
                          for name, count in sorted(counts.items()))
    print(f"\n{len(available)} controller voices on disk "
          f"({by_engine}), {total:.0f} MB total")


def _download(url: str, target: Path, label: str,
              on_progress: Progress = None) -> None:
    """Fetch one file, reporting as it goes.

    ``on_progress`` is how the panel draws a bar for a download that takes
    three minutes. Without one the only sign of life is a terminal, which is
    no use to somebody who installed this by double-clicking it. Given one,
    the printing stops: two progress reports for one download is one too many.
    """
    tmp = target.with_suffix(target.suffix + ".part")
    with requests.get(url, stream=True, timeout=180) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        written = 0
        with tmp.open("wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 18):
                fh.write(chunk)
                written += len(chunk)
                if on_progress is not None:
                    on_progress(label, written, total)
                elif total:
                    print(f"\r  {label:46} {100*written/total:5.1f}%", end="")
    tmp.replace(target)
    if on_progress is not None:
        on_progress(label, written, written)
    else:
        print(f"\r  {label:46} done  {written/1e6:6.1f} MB")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--list" in args:
        for v in KOKORO_CATALOGUE + SUPERTONIC_CATALOGUE + CATALOGUE:
            mark = "*" if v.core or v.engine == "kokoro" else " "
            here = "on disk" if v.path().exists() else ""
            print(f" {mark} {v.key:46} {v.gender} {v.accent} "
                  f"{v.engine:7} {here}")
        raise SystemExit

    which = "core"
    for i, a in enumerate(args):
        if a == "--all":
            which = "all"
        elif a == "--kokoro":
            which = "kokoro"
        elif a == "--supertonic":
            which = "supertonic"
        elif a == "--voices" and i + 1 < len(args):
            which = args[i + 1]
    download_voices(which, force="--force" in args)
