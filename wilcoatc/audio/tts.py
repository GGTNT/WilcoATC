"""Speech synthesis for controller transmissions.

Applies the per-controller voice shading from :mod:`wilcoatc.audio.voices`,
hands the words to whichever synthesiser that controller's voice belongs to,
and runs the result through the radio effect chain.

The casting decides which engine speaks, not this module: ``spec.engine`` on
the cast voice names it and :mod:`wilcoatc.audio.backends` turns that name
into an object. Kokoro is where nearly every controller now comes from,
because it is the one that sounds like a person; Piper covers the languages
Kokoro has no speaker for. Both are given the same shading in the same units,
and neither knows this module exists.

What stays here is everything that must be the same whichever engine
rendered the words: the transmission cache, the pitch compensation the radio
chain's resampling needs, the radio chain itself, and the fallback that drops
a session to Piper rather than leaving an aeroplane unanswered.

Two layers sit either side of the engine and are applied here for the same
reason -- they have to happen once, whichever engine is speaking.
:mod:`wilcoatc.audio.prosody` decides where the sentences go and how fast the
transmission is read; :mod:`wilcoatc.audio.imperfections` supplies the
operator on top of the equipment. Both are off-switchable and neither can
change a word of phraseology.

Models are loaded lazily and kept, by the backend that owns them. Kokoro is
one model for the whole catalogue and is loaded at startup; Piper is one file
per voice, so those are loaded as they are first cast.

Synthesis of a normal clearance takes about a second on a modern CPU, no GPU.
Transmissions are cached by (text, voice, quality) since ATC repeats itself
constantly -- every "roger", "say again" and readback correction is a cache
hit.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import accent, imperfections, prosody, tone
from .radio_fx import (
    PROFILE_ATIS,
    PROFILE_CLEAN,
    RadioEffect,
    RadioProfile,
    signal_quality_for,
    station_character,
    station_keying,
)
from .backends import BackendRegistry, TTSUnavailable, default_registry
from .voices import (VoiceAssignment, VoiceRegistry,
                     VoicesMissing, VOICE_DIR)

log = logging.getLogger(__name__)

# Piper's English models all run at this rate; the radio chain and playback
# follow whatever the loaded model reports.
DEFAULT_SAMPLE_RATE = 22050


@dataclass(frozen=True)
class Transmission:
    """A rendered controller transmission, ready to play."""

    text: str
    audio: np.ndarray
    sample_rate: int
    voice_key: str
    speaker: str = ""
    duration_s: float = 0.0


class Synthesizer:
    """Renders controller speech with a stable voice per facility."""

    def __init__(
        self,
        voice_dir: Path = VOICE_DIR,
        registry: VoiceRegistry | None = None,
        cache_size: int = 512,
        seed: str = "wilcoatc",
        engine: str = "auto",
        backends: "BackendRegistry | None" = None,
        prosody_settings: "prosody.Prosody | None" = None,
        imperfection_settings: "imperfections.Imperfections | None" = None,
        tone_settings: "tone.Tone | None" = None,
        gpu: bool = False,
        hq_model: str = "standard",
        hq_takes: int = 2,
    ):
        # The cloning tier is reachable two ways, and the casting has to know
        # about both: named outright, or through ``auto`` when the pilot has
        # said the card is free and there is one. The backend registry
        # applies the same rule to the engines; the two must agree, or a
        # controller is cast from a clip nothing will render.
        from .backends.hardware import gpu_available

        hq = (engine or "auto").strip().lower() == "chatterbox" \
            or (bool(gpu) and gpu_available())
        self.registry = registry or VoiceRegistry(voice_dir, seed=seed,
                                                  engine=engine, hq=hq,
                                                  hq_model=hq_model)
        # How it is said, on top of what the phrasebook composed. Both default
        # to on; both are one flag away from the delivery this program had
        # before them, which is what makes the comparison a fair one.
        self.prosody = prosody_settings or prosody.DEFAULT
        self.imperfections = imperfection_settings or imperfections.DEFAULT
        # The receiver's balance, measured against a Heathrow recording. The
        # third layer, and the only one that is the equipment rather than the
        # operator; it goes through the same profile copy the operator's
        # numbers do, so the chain itself is never edited.
        self.tone = tone_settings or tone.DEFAULT
        # Which engine renders a given voice is now asked of the backend
        # registry rather than branched on here. The casting is unchanged:
        # ``spec.engine`` still says who the controller is, and this only
        # decides which piece of code turns that into a waveform.
        self.backends = backends or default_registry(
            self.registry, prefer=self.registry.engine, allow_gpu=bool(gpu),
            hq_model=hq_model, hq_takes=hq_takes)
        self._lock = threading.Lock()
        self._cache: dict[str, Transmission] = {}
        self._cache_order: list[str] = []
        self._cache_size = cache_size
        self._effects: dict[int, RadioEffect] = {}
        self._fell_back = False

    # ------------------------------------------------------------------

    def forget(self) -> None:
        """Throw away every rendering, keeping the casting and the models.

        For a change to *how* a transmission is said rather than to who says
        it: the delivery layers are read once and every rendering is cached,
        so turning the wind off would otherwise be audible only on phrases
        nobody had heard yet. Narrower than :meth:`refresh`, which also
        re-reads the voice folder and drops the loaded models.
        """
        with self._lock:
            self._cache.clear()
            self._cache_order.clear()

    def refresh(self) -> None:
        """Look at the voice folder again, after something was downloaded.

        The registry reads the folder once and keeps the answer, which is
        right -- it is asked on every transmission. It made a download during
        a session useless, though: the voices arrived, the engine went on
        using the empty list it had read at startup, and the panel could only
        tell the pilot to restart the program they had just set up. This is
        the one call that makes the new files count.

        Everything derived from the old casting goes with it. A cached
        transmission was rendered by a voice that may no longer be the one
        assigned to that position, and a loaded Piper model may have been
        superseded by a Kokoro voice that can now be reached.
        """
        with self._lock:
            self.registry.invalidate()
            self._cache.clear()
            self._cache_order.clear()
        self.backends.invalidate()
        for backend in self.backends.backends:
            forget = getattr(backend, "forget", None)
            if forget is not None:
                forget()

    def _assign(self, facility: str, position: str, ident: str,
                language: str, gender: str, voice_key: str,
                accent: str = "", avoid: tuple[str, ...] = ()) -> VoiceAssignment:
        try:
            assignment = self.registry.assign(facility, position, ident,
                                              language, gender=gender,
                                              accent=accent, avoid=avoid)
        except VoicesMissing:
            # Nothing was on disk the last time the folder was read. That may
            # simply be out of date -- the download runs while the engine is
            # up -- so look once more before telling the pilot there are no
            # voices. It costs one directory listing, and only on the path
            # that was about to fail anyway.
            self.registry.invalidate()
            assignment = self.registry.assign(facility, position, ident,
                                              language, gender=gender,
                                              accent=accent, avoid=avoid)
        if voice_key:
            pinned = self.registry.pin(assignment, voice_key)
            if pinned is not None:
                assignment = pinned
        return assignment

    def cast(self, facility: str, position: str, ident: str = "", *,
             language: str = "en", gender: str = "", voice_key: str = "",
             accent: str = "", avoid: tuple[str, ...] = ()) -> str:
        """Who :meth:`speak` would cast, without rendering anything.

        For the crew, who have to know who the others are before casting
        the next one: two seats narrowed to one sex draw from two different
        lists, and the slots that keep an automatic cast apart only do so
        inside one list.
        """
        from .voices import identity

        return identity(self._assign(facility, position, ident, language,
                                     gender, voice_key, accent, avoid).spec)

    def backend_for(self, spec):
        """The engine that renders one cast voice.

        The catalogue says which engine a voice belongs to and the backend
        registry says which object that is, so a new engine is a registration
        rather than another branch here.
        """
        for backend in self.backends.backends:
            if backend.capabilities.name == spec.engine:
                return backend
        # Not in the registry: fall to whatever can speak the language at all,
        # which is the mandatory CPU fallback rather than silence.
        return self.backends.for_language(spec.language)

    def speaks(self, language: str) -> bool:
        """Whether any usable engine here can read this language at all.

        Asked by the cabin before it puts a language in its order, so a
        Finnish announcement nothing can read is left out and said so, rather
        than reaching the cast and coming back as a second English one.
        """
        try:
            self.backends.for_language(language)
        except TTSUnavailable:
            return False
        return True

    def verify_with(self, recognizer) -> None:
        """Give the cloning tier the program's recogniser to read takes back.

        The tier samples, and a take can come back with a number mumbled;
        the recogniser is the one judge of whether the words survived that
        the program already has loaded. Wired here rather than in the
        backend so that the backend imports nothing about speech
        recognition, and so that a synthesiser without a recogniser -- the
        ``say`` command, the tests -- simply accepts the first take.
        """
        backend = self._backend_named("chatterbox")
        if backend is None or recognizer is None:
            return
        from .backends.chatterbox import word_error_rate

        def verify(audio, sample_rate, text) -> float:
            heard = recognizer.transcribe(audio, sample_rate)
            return word_error_rate(text, getattr(heard, "text", "") or "")

        backend.verify = verify

    def _backend_named(self, name: str):
        """One registered engine by name, or None if this build has no such
        thing. Used by the mid-session fallback, which has to name the engine
        that threw rather than the engine it used to be."""
        for backend in self.backends.backends:
            if backend.capabilities.name == name:
                return backend
        return None

    def _backend(self):
        """The Kokoro engine, for the diagnostics that reach past the casting."""
        found = self._backend_named("kokoro")
        if found is None:
            raise TTSUnavailable("Kokoro is not registered in this build.")
        return found

    # ------------------------------------------------------------------

    @staticmethod
    def _payloads(text: str, assignment: VoiceAssignment,
                  model_voice: str = "") -> list[str]:
        """What goes to Piper, for callers that reach past the backend.

        The work moved to :meth:`wilcoatc.audio.backends.piper.PiperTTS
        .payloads` when the engines were separated. This keeps the name --
        and the un-prepared text it has always accepted -- because it is the
        seam the accent tests read the phonemes out of.
        """
        from .backends.piper import PiperTTS

        return PiperTTS.payloads(prepare_text(text, assignment.language),
                                 assignment, model_voice)

    def _load(self, key: str):
        """Load and keep a Piper model. Kept as the name the tests and the
        diagnostics already use; the work is the Piper backend's."""
        for backend in self.backends.backends:
            if backend.capabilities.name == "piper":
                return backend.load(key)
        raise FileNotFoundError(f"Voice model {key} not found: no Piper engine")

    def _effect(self, sample_rate: int) -> RadioEffect:
        effect = self._effects.get(sample_rate)
        if effect is None:
            effect = RadioEffect(sample_rate)
            self._effects[sample_rate] = effect
        return effect

    def _operator(self, shaped: bool) -> "imperfections.Imperfections":
        """Who is speaking, as far as the imperfections layer is concerned.

        ``shaped`` says whether this is somebody on a frequency. It used to
        select between the full layer and nothing at all, which left the
        first officer and the cabin crew rendered as evenly as a file being
        played -- correct about the radio, wrong about the person. They get
        the half of the layer that is human: see
        :meth:`wilcoatc.audio.imperfections.Imperfections.indoors`.
        """
        return self.imperfections if shaped else self.imperfections.indoors()

    # ------------------------------------------------------------------

    def speak(
        self,
        text: str,
        facility: str,
        position: str,
        ident: str = "",
        *,
        language: str = "en",
        distance_nm: float = 0.0,
        altitude_ft: float = 0.0,
        signal_quality: float | None = None,
        profile: RadioProfile | None = None,
        radio: bool = True,
        gender: str = "",
        voice_key: str = "",
        shaped: bool = True,
        accent: str = "",
        avoid: tuple[str, ...] = (),
    ) -> Transmission:
        """Render one transmission from a named controller.

        ``language`` selects the model that speaks it. English at a French
        facility is rendered by a French model, which is what gives a French
        controller a French accent when they switch languages for a foreign
        crew rather than turning into an American.

        ``shaped`` is whether this is somebody on a frequency. The prosody
        layer is written for one thing -- a callsign and then an instruction
        -- and leans on it: it breaks after the first clause because the first
        clause is the aeroplane. The first officer calling "positive rate,
        gear up" has no callsign in it, and shaping it would put a full stop
        in the middle of a single callout, so the flight deck and the cabin
        pass ``shaped=False`` and their words come out as written.

        It is not silence on the imperfections layer, though. Half of that
        layer is the radio and half of it is the person, and the person is
        still there on an interphone: see :meth:`_operator`.
        """
        assignment = self._assign(facility, position, ident, language,
                                  gender, voice_key, accent, avoid)
        if signal_quality is None:
            signal_quality = signal_quality_for(distance_nm, position, altitude_ft)
        if profile is None:
            profile = PROFILE_ATIS if position == "ATIS" else PROFILE_CLEAN

        cache_key = self._cache_key(
            text, assignment, round(float(signal_quality), 2), radio,
            _profile_key(profile)
        )
        hit = self._cache.get(cache_key)
        if hit is not None:
            return hit

        # Everything that varies per transmission -- the delivery rate, the
        # hesitation, the lead-in, the carrier -- is drawn from this one
        # number, and it comes from the cache key. A transmission that is a
        # cache hit has to be the same transmission, and one rolled fresh on
        # each call would be frozen at its first roll the moment it was cached.
        seed = _stable_seed(cache_key)

        try:
            raw, sample_rate = self._synthesize(text, assignment, seed,
                                               shaped)
        except Exception:
            failed = assignment.spec.engine
            if failed == "piper" or self._fell_back:
                raise
            # The cloning tier failing is a different case from a CPU engine
            # failing: everything beneath it still works, so the session
            # drops to the ordinary cast rather than all the way to Piper,
            # and the cloning tier can fail once without costing Kokoro.
            if failed == "chatterbox":
                log.exception("chatterbox failed; falling back to the CPU "
                              "voices for this session")
                broken = self._backend_named(failed)
                if broken is not None:
                    self.backends.mark_broken(broken)
                self.registry = VoiceRegistry(self.registry.root,
                                              seed=self.registry.seed,
                                              engine="auto")
                self._cache.clear()
                self._cache_order.clear()
                assignment = self._assign(facility, position, ident, language,
                                          gender, voice_key, accent, avoid)
                cache_key = self._cache_key(
                    text, assignment, round(float(signal_quality), 2), radio,
                    _profile_key(profile)
                )
                seed = _stable_seed(cache_key)
                raw, sample_rate = self._synthesize(text, assignment, seed,
                                                   shaped)
                return self._finish(text, raw, sample_rate, assignment, seed,
                                    signal_quality, profile, radio, shaped,
                                    facility, position, ident, cache_key)
            # A better engine has failed at the worst possible moment: a
            # clearance is composed and the controller is due to say it. Drop
            # to Piper for the rest of the session rather than going silent on
            # the frequency, and say so once. Everyone is re-cast, so the
            # controllers change voice -- which is wrong, and is a great deal
            # less wrong than an aeroplane nobody answers.
            #
            # Which engine failed is read off the assignment rather than
            # assumed. This used to name Kokoro, which was true while Kokoro
            # was what nearly every controller was cast from; Supertonic
            # leads now, and an engine that can fail without being caught is
            # a frequency that goes quiet.
            log.exception("%s failed; falling back to Piper for this session",
                          failed)
            self._fell_back = True
            # Two things have to be told, because they are two registries: the
            # casting must stop choosing that engine's voices, and the backend
            # registry must stop offering the engine that just threw. Telling
            # only the first would re-cast onto Piper and then hand the words
            # back to the broken engine for any voice still shaped for it.
            broken = self._backend_named(failed)
            if broken is not None:
                self.backends.mark_broken(broken)
            self.registry = VoiceRegistry(self.registry.root,
                                          seed=self.registry.seed,
                                          engine="piper")
            self._cache.clear()
            self._cache_order.clear()
            assignment = self._assign(facility, position, ident, language,
                                      gender, voice_key, accent, avoid)
            cache_key = self._cache_key(
                text, assignment, round(float(signal_quality), 2), radio,
                _profile_key(profile)
            )
            seed = _stable_seed(cache_key)
            raw, sample_rate = self._synthesize(text, assignment, seed,
                                               shaped)
        return self._finish(text, raw, sample_rate, assignment, seed,
                            signal_quality, profile, radio, shaped,
                            facility, position, ident, cache_key)

    def _finish(self, text, raw, sample_rate, assignment, seed,
                signal_quality, profile, radio, shaped,
                facility, position, ident, cache_key) -> Transmission:
        """Everything after the engine: the operator, the chain, the cache.

        Split from :meth:`speak` so that the two fallback paths and the
        ordinary one end in the same place rather than in three copies of
        it.
        """
        if radio:
            # Each facility's transmitter has its own response and, for a
            # radar position, its own path flutter, so two stations never sound
            # like the same box.
            tilt, flutter = station_character(facility, position, ident)
            effect = self._effect(sample_rate)
            # The operator, on top of the equipment. This runs after the cache
            # key has been taken so that the key still names the profile it
            # was given rather than the tuned copy, and it changes nothing at
            # all when the layer is switched off.
            operator = self._operator(shaped)
            rng = imperfections.rng_for(seed, salt=2)
            # Order matters, and each step says why in its own docstring:
            # breathing reads the pauses the synthesiser left, so it has to
            # run before the lead-in adds a longer one; the wander is the
            # voice's own and stays off the silence in front of it; the wind
            # has to run after, so that the gap between the carrier and the
            # first word has weather in it rather than being the one silent
            # moment.
            raw = imperfections.breathe(raw, sample_rate, profile, operator,
                                        rng)
            raw = imperfections.wander(raw, sample_rate, operator, rng)
            raw = imperfections.lead_in(raw, sample_rate, operator, rng)
            raw = imperfections.wind(raw, sample_rate, profile, operator, rng)
            tuned = tone.apply(imperfections.tune(profile, operator),
                               self.tone)
            audio = effect.process(
                raw,
                signal_quality=signal_quality,
                pitch_semitones=assignment.pitch_semitones,
                seed=seed,
                profile=tuned,
                tilt_db=tilt + tuned.tilt_db,
                flutter=max(tuned.flutter, flutter),
                keying=imperfections.keying(
                    station_keying(facility, position, ident),
                    operator, rng),
            )
        else:
            audio = raw

        result = Transmission(
            text=text,
            audio=audio,
            sample_rate=sample_rate,
            voice_key=assignment.key,
            speaker=f"{facility} {position}".strip(),
            duration_s=len(audio) / float(sample_rate),
        )
        self._remember(cache_key, result)
        return result

    def _synthesize(
        self, text: str, assignment: VoiceAssignment, seed: int = 0,
        shaped: bool = True,
    ) -> tuple[np.ndarray, int]:
        # The radio chain shifts pitch by resampling, which also changes
        # duration; asking for a compensating length_scale cancels that out so
        # the delivery rate stays where the assignment intended. Every backend
        # is given the same compensated number, in Piper's units.
        pitch_ratio = 2.0 ** (assignment.pitch_semitones / 12.0)
        length_scale = float(assignment.length_scale * pitch_ratio)

        # One preparation, here, for every engine. It used to be done twice on
        # the Kokoro path and once on the Piper one, which was harmless and
        # was still two answers to one question.
        prepared = prepare_text(text, assignment.language)

        # Where the sentences go, and how fast they are read. The rate is a
        # multiplier on the compensated scale rather than a replacement for
        # it: the casting's own idea of how quickly this controller talks is
        # not something delivery shaping gets to overrule.
        settings = self.prosody if shaped else prosody.OFF
        prepared, rate = prosody.shape(prepared, assignment.language,
                                       settings, seed)
        length_scale *= rate

        # The filler goes on last, so that it is not mistaken for the address
        # the prosody layer breaks after.
        prepared = imperfections.hesitate(
            prepared, assignment.language, self._operator(shaped),
            imperfections.rng_for(seed, salt=1))

        backend = self.backend_for(assignment.spec)
        return backend.synthesize(prepared, assignment, assignment.language,
                                  length_scale=length_scale)

    # ------------------------------------------------------------------

    @staticmethod
    def _cache_key(text: str, assignment: VoiceAssignment, quality: float,
                   radio: bool, profile_id: str) -> str:
        parts = (
            text, assignment.key, str(assignment.speaker_id),
            assignment.language, f"{assignment.accent_strength:.3f}",
            f"{assignment.length_scale:.4f}",
            f"{assignment.pitch_semitones:.3f}", f"{assignment.noise_scale:.3f}",
            f"{assignment.noise_w:.3f}", f"{quality:.2f}", str(radio), str(profile_id),
        )
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()

    def _remember(self, key: str, value: Transmission) -> None:
        self._cache[key] = value
        self._cache_order.append(key)
        while len(self._cache_order) > self._cache_size:
            self._cache.pop(self._cache_order.pop(0), None)

    def warm(self, keys: list[str] | None = None, piper: bool = True) -> None:
        """Pre-load models so the first transmission is not delayed.

        The single-model engines are always worth warming and Piper is not,
        which is why they are separated. Kokoro and Supertonic are one model
        however many controllers are cast from them: a few hundred megabytes
        and about two seconds, once, covering every voice they own. Piper is
        one file per voice, and preloading all of them costs well over a
        gigabyte to save a second on each first use, which is why
        ``voice.preload`` defaults to off.
        """
        wanted = [spec for spec in self.registry.available()
                  if not keys or spec.key in keys]
        # One warm per engine, not per voice: these hold every voice they own
        # in one model, and asking twice would load it twice.
        shared = {spec.engine for spec in wanted} - {"piper"}
        # The cloning model is several seconds and a couple of gigabytes, and
        # it is only in the cast when the pilot asked for it -- so it is
        # warmed when it is there, like the others, and never otherwise.
        for backend in self.backends.backends:
            name = backend.capabilities.name
            if name not in shared:
                continue
            try:
                backend.warm()
            except Exception as exc:
                log.warning("could not preload %s: %s", name, exc)
        if not piper:
            return
        for spec in wanted:
            if spec.engine != "piper":
                continue
            try:
                self._load(spec.key)
            except Exception as exc:
                log.warning("could not preload %s: %s", spec.key, exc)


# --------------------------------------------------------------------------
# text preparation
# --------------------------------------------------------------------------

# Piper reads plain words, and the phraseology layer has already done the two
# things that matter: it emits spoken forms ("one two thousand", "two seven
# left"), and it separates every clause with a comma, because that is how its
# templates are assembled. Measured over the whole phrasebook the median clause
# is four words long. There is nothing left to break up.
#
# This used to add a pause in front of the instruction words as well, on the
# theory that a long clearance would otherwise run together. It did not do
# that. What it did was cut fixed phrases in half, because those words also
# occur inside them:
#
#     radar contact              -> radar, contact
#     hold short of runway 06    -> hold short of, runway 06
#     point d'attente piste 26   -> point d'attente, piste 26
#
# A controller says each of those as one unit, and a pause in the middle is the
# kind of thing that makes synthesised speech sound synthesised. So the commas
# now come only from the templates, which put them where a controller breathes.


def prepare_text(text: str, language: str = "en") -> str:
    """Tidy the text for the synthesiser. The pauses are already in it."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return ""
    text = re.sub(r",\s*,+", ", ", text)
    text = re.sub(r"^\s*,\s*", "", text)
    text = re.sub(r"\s+([,.])", r"\1", text)
    if not text.endswith((".", "?", "!")):
        text += "."
    return text


# The named profiles, so a cache key can say which one it was rather than
# where the object happened to be in memory. An address is not stable across
# runs, which is harmless for a cache that lives one session -- but it is also
# not stable across *equal* profiles, and two identical renderings that miss
# each other are a cache that does nothing.
_PROFILE_NAMES: dict[int, str] = {}


def _profile_key(profile) -> str:
    if profile is None:
        return "-"
    from . import radio_fx

    if not _PROFILE_NAMES:
        for name in dir(radio_fx):
            if name.startswith("PROFILE_"):
                _PROFILE_NAMES[id(getattr(radio_fx, name))] = name
    known = _PROFILE_NAMES.get(id(profile))
    if known:
        return known
    # A profile built on the fly is named by what it is.
    try:
        return hashlib.sha1(repr(profile).encode("utf-8")).hexdigest()[:12]
    except Exception:                                   # pragma: no cover
        return str(id(profile))


def _stable_seed(key: str) -> int:
    return int(key[:8], 16)
