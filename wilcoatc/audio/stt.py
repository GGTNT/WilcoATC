"""Speech recognition for pilot transmissions.

Uses faster-whisper, which runs Whisper through CTranslate2 on the CPU fast
enough for this to be interactive: a five-second transmission transcribes in
well under a second on a modern desktop, with no GPU.

Two things are done beyond calling the model:

*The vocabulary is biased toward aviation.* Whisper's language model has never
heard a clearance readback, and left alone it produces "Delta 1234 climbing to
one two thousand" as "Delta 1234 climbing to 112,000", or turns "squawk" into
"squat". An initial prompt full of real phraseology pulls it back.

*Numbers are kept as digits where possible.* The intent parser handles both
forms, but digit output is far less ambiguous, so the decoding options are set
to discourage the model from spelling numbers out inconsistently.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..paths import model_dir

log = logging.getLogger(__name__)

MODEL_DIR = model_dir()

# Whisper works at 16 kHz mono; anything else has to be resampled first.
SAMPLE_RATE = 16000

# Seeding the decoder with real phraseology is the single most effective way to
# stop aviation words being transcribed as ordinary English. Every term here is
# one that Whisper gets wrong without help.
AVIATION_PROMPT_EN = (
    "Air traffic control radio, the pilot's side. "
    "Request clearance, ready to copy, request pushback, request taxi, "
    "ready to taxi, ready for departure, holding short runway two seven left, "
    "request takeoff, going around, request the ILS approach, "
    "established on the localizer, field in sight, traffic in sight, "
    "request landing, full stop, request taxi to the gate, "
    "request higher, request lower, request descent, request climb, "
    "request direct, request vectors, request frequency change, cancel IFR, "
    "with you passing five thousand for one two thousand, "
    "roger, wilco, affirmative, negative, unable, standby, say again, "
    "cleared for takeoff runway two seven left, cleared to land, "
    "taxi via alpha bravo, hold short of runway three one right, "
    "climb flight level three five zero, descend five thousand, "
    "turn right heading zero four zero, squawk four five two one, "
    "QNH one zero one three, altimeter two niner niner two, "
    "contact departure one three five point niner, information bravo, "
    "November one seven two sierra papa, Delta twelve thirty four, "
    "Speedbird one seventeen heavy, mayday, pan-pan, minimum fuel."
)

# The same idea in French. Whisper knows French, but not French radiotelephony:
# without this it writes "autorisé décollage" as "autorisez de collage" and
# "point d'attente" as "pointe d'attente".
AVIATION_PROMPT_FR = (
    "Radiotéléphonie aéronautique, côté pilote. "
    "Demande la clairance, prêt à copier, demande la mise en route, "
    "demande le repoussage, prêt au repoussage, demande le roulage, "
    "prêt au roulage, prêt au départ, prêt au décollage, "
    "au point d'attente piste zéro huit gauche, "
    "demande la montée, demande la descente, demande un niveau supérieur, "
    "demande un niveau inférieur, demande un guidage radar, demande direct, "
    "demande l'approche ILS, établi sur le localizer, terrain en vue, "
    "trafic en vue, en finale, pour l'atterrissage, remise de gaz, "
    "approche interrompue, roulage vers le parking, "
    "demande le changement de fréquence, essai radio, "
    "bien reçu, compris, affirme, négatif, impossible, attendez, répétez, "
    "autorisé décollage piste zéro huit gauche, autorisé atterrissage, "
    "roulez par Alpha Bravo, montez niveau de vol trois cinq zéro, "
    "descendez cinq mille pieds, cap zéro quatre zéro, "
    "affiche quatre cinq deux un, QNH mille treize, "
    "contactez Paris contrôle cent vingt-sept décimale sept cinq, "
    "Air France mille deux cent trente-quatre, Foxtrot Golf Xray Yankee, "
    "information Bravo, carburant minimum, mayday, pan-pan."
)

# The same idea again for the other languages worked on frequency. Each is real
# phraseology from that state's own manual, because the point of the prompt is
# to pull the decoder toward words it would otherwise never produce.
AVIATION_PROMPT_ES = (
    "Radiotelefonía aeronáutica, lado del piloto. "
    "Solicito la autorización, listo para copiar, solicito el retroceso, "
    "solicito el rodaje, listo para rodar, listo para despegue, "
    "en el punto de espera pista uno ocho derecha, "
    "solicito ascenso, solicito descenso, solicito nivel superior, "
    "solicito nivel inferior, solicito vectores, solicito directo, "
    "solicito la aproximación ILS, establecidos en el localizador, "
    "campo a la vista, tráfico a la vista, en final, solicito aterrizar, "
    "motor y al aire, aproximación frustrada, rodaje a la plataforma, "
    "solicito cambio de frecuencia, prueba de radio, "
    "recibido, entendido, afirmo, negativo, imposible, espere, repita, "
    "autorizado a despegar pista uno ocho derecha, autorizado a aterrizar, "
    "ruede por Alpha Bravo, ascienda nivel de vuelo tres cinco cero, "
    "descienda cinco mil pies, rumbo cero cuatro cero, "
    "transpondedor cuatro cinco dos uno, QNH mil trece, "
    "contacte Madrid Torre ciento veintiuno decimal siete, "
    "Iberia tres cuatro cinco dos, información Bravo, "
    "combustible mínimo, mayday, pan-pan."
)

AVIATION_PROMPT_DE = (
    "Sprechfunkverkehr, Seite des Piloten. "
    "Erbitte Freigabe, bereit zum Mitschreiben, erbitte Push, "
    "erbitte Rollen, rollbereit, startbereit, am Rollhalt Piste zwo fünf rechts, "
    "erbitte Steigflug, erbitte Sinkflug, erbitte höhere Flugfläche, "
    "erbitte tiefere Flugfläche, erbitte Radarführung, erbitte direkt, "
    "erbitte Anflug, ILS Anflug, etabliert auf dem Localizer, "
    "Platz in Sicht, Verkehr in Sicht, im Endanflug, zur Landung, "
    "durchstarten, Fehlanflug, zur Parkposition, erbitte Frequenzwechsel, "
    "Funkprobe, verstanden, wilco, affirm, negativ, nicht möglich, "
    "warten Sie, wiederholen Sie, "
    "Start frei Piste zwo fünf rechts, Landung frei, "
    "rollen über Alpha Bravo, steigen Sie auf Flugfläche drei fünf null, "
    "sinken Sie auf fünftausend Fuß, Kurs null vier null, "
    "Squawk vier fünf zwo eins, QNH eintausenddreizehn, "
    "rufen Sie Langen Radar einhundertsiebenundzwanzig Komma sieben fünf, "
    "Lufthansa vier fünf sechs, Information Bravo, "
    "minimum fuel, mayday, pan-pan."
)

AVIATION_PROMPT_IT = (
    "Radiotelefonia aeronautica, lato pilota. "
    "Chiedo l'autorizzazione, pronto a copiare, chiedo il push, "
    "chiedo il rullaggio, pronto al rullaggio, pronto al decollo, "
    "al punto attesa pista uno sei destra, "
    "chiedo la salita, chiedo la discesa, chiedo livello superiore, "
    "chiedo livello inferiore, chiedo vettori, chiedo diretto, "
    "chiedo l'avvicinamento ILS, stabilizzati sul localizzatore, "
    "campo in vista, traffico in vista, in finale, chiedo l'atterraggio, "
    "riattacchiamo, mancato avvicinamento, rullaggio al parcheggio, "
    "chiedo cambio frequenza, prova radio, "
    "ricevuto, capito, affermo, negativo, impossibile, attenda, ripeta, "
    "autorizzato al decollo pista uno sei destra, autorizzato all'atterraggio, "
    "rulli via Alpha Bravo, salga a livello di volo tre cinque zero, "
    "scenda a cinquemila piedi, prua zero quattro zero, "
    "transponder quattro cinque due uno, QNH milletredici, "
    "contatti Roma Avvicinamento centoventuno decimale sette, "
    "Alitalia sei uno due, informazione Bravo, "
    "carburante minimo, mayday, pan-pan."
)

AVIATION_PROMPT_PT = (
    "Radiotelefonia aeronáutica, lado do piloto. "
    "Peço a autorização, pronto para copiar, peço push, "
    "peço o táxi, pronto para taxiar, pronto para descolar, "
    "no ponto de espera pista zero três esquerda, "
    "peço a subida, peço a descida, peço nível superior, "
    "peço nível inferior, peço vetores, peço directo, "
    "peço a aproximação ILS, estabelecidos no localizador, "
    "campo à vista, tráfego à vista, em final, peço a aterragem, "
    "arremetemos, aproximação falhada, para o estacionamento, "
    "peço mudança de frequência, teste de rádio, "
    "recebido, entendido, afirmativo, negativo, impossível, aguarde, repita, "
    "autorizado a descolar pista zero três esquerda, autorizado a aterrar, "
    "taxie por Alpha Bravo, suba para nível de voo três cinco zero, "
    "desça para cinco mil pés, proa zero quatro zero, "
    "transponder quatro cinco dois um, QNH mil e treze, "
    "contacte Lisboa Torre cento e vinte e um decimal sete, "
    "Air Portugal um dois três, informação Bravo, "
    "combustível mínimo, mayday, pan-pan."
)

PROMPTS: dict[str, str] = {
    "en": AVIATION_PROMPT_EN,
    "fr": AVIATION_PROMPT_FR,
    "es": AVIATION_PROMPT_ES,
    "de": AVIATION_PROMPT_DE,
    "it": AVIATION_PROMPT_IT,
    "pt": AVIATION_PROMPT_PT,
}

# Kept for callers that imported the old name.
AVIATION_PROMPT = AVIATION_PROMPT_EN


@dataclass
class Transcript:
    """One recognised transmission."""

    text: str
    confidence: float = 0.0
    duration_s: float = 0.0
    decode_s: float = 0.0
    language: str = "en"
    language_confidence: float = 1.0
    segments: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.text.strip()

    def replace_meta(self, duration_s: float, decode_s: float,
                     language_confidence: float) -> "Transcript":
        """Stamp timing onto a transcript produced by the arbitration path."""
        self.duration_s = duration_s
        self.decode_s = decode_s
        self.language_confidence = language_confidence
        return self


class Recognizer:
    """Wraps faster-whisper with settings tuned for radio phraseology."""

    def __init__(
        self,
        model_size: str = "small.en",
        device: str = "cpu",
        compute_type: str = "int8",
        download_root: Path = MODEL_DIR,
        beam_size: int = 5,
        prompt: str | None = None,
        language: str | None = "en",
        languages: list[str] | None = None,
        detection_threshold: float = 0.85,
    ):
        """``languages`` is the set the pilot may speak.

        One entry pins the decoder to that language, which is both faster and
        more accurate. Two or more turns on detection per transmission, and the
        result is constrained to the set: a mumbled English readback is far
        more likely to be misdetected as Welsh than actually to be Welsh.
        """
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.download_root = Path(download_root)
        self.beam_size = beam_size
        self.prompt = prompt
        self.languages = [l.lower()[:2] for l in (languages or ([language] if language else ["en"]))]
        self.language = self.languages[0] if len(self.languages) == 1 else None
        self.detection_threshold = detection_threshold
        self._model = None
        self._lock = threading.Lock()

    @property
    def multilingual(self) -> bool:
        return len(self.languages) > 1

    def set_languages(self, languages: list[str]) -> bool:
        """Change what the decoder is listening for, mid-flight.

        The model itself does not change -- it is the multilingual one and
        already knows all of these -- so this is only the candidate set that
        detection is constrained to. Fly from France into Italy and Italian
        replaces French in it, which is what the aeroplane experiences.

        Returns whether anything actually changed.
        """
        wanted = [l.lower()[:2] for l in languages if l] or ["en"]
        if wanted == self.languages:
            return False
        self.languages = wanted
        self.language = wanted[0] if len(wanted) == 1 else None
        return True

    def prompt_for(self, language: str) -> str:
        if self.prompt is not None:
            return self.prompt
        return PROMPTS.get((language or "en")[:2], AVIATION_PROMPT_EN)

    def detect(self, samples: np.ndarray) -> tuple[str, float]:
        """Pick the language, constrained to the configured set."""
        if not self.multilingual:
            return self.languages[0], 1.0
        model = self.load()
        try:
            language, probability, candidates = model.detect_language(
                audio=samples, vad_filter=False, language_detection_segments=1
            )
        except Exception as exc:
            log.debug("language detection failed: %s", exc)
            return self.languages[0], 0.0

        allowed = set(self.languages)
        if language in allowed:
            return language, float(probability)
        # Whisper is confident about a language we do not work in. Fall back to
        # the best-scoring one we do, rather than trusting a language nobody on
        # this frequency speaks.
        for candidate, score in candidates or []:
            if candidate in allowed:
                return candidate, float(score)
        return self.languages[0], 0.0

    # ------------------------------------------------------------------

    def load(self):
        """Load the model, downloading it on first use."""
        with self._lock:
            if self._model is not None:
                return self._model
            from faster_whisper import WhisperModel

            self.download_root.mkdir(parents=True, exist_ok=True)
            t0 = time.perf_counter()
            self._model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
                download_root=str(self.download_root),
            )
            log.info(
                "loaded whisper %s (%s/%s) in %.1fs",
                self.model_size, self.device, self.compute_type,
                time.perf_counter() - t0,
            )
            return self._model

    @property
    def loaded(self) -> bool:
        return self._model is not None

    # ------------------------------------------------------------------

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = SAMPLE_RATE,
        *,
        prompt: str | None = None,
        language: str | None = None,
    ) -> Transcript:
        """Transcribe a block of mono float32 audio.

        ``language`` forces one for this transmission; leaving it out detects
        the language when more than one is configured. Detection runs first so
        that the aviation prompt fed to the decoder is in the right language --
        an English prompt actively harms a French transcription.
        """
        samples = np.asarray(audio, dtype=np.float32).flatten()
        duration = len(samples) / float(sample_rate or SAMPLE_RATE)
        if duration < 0.25:
            return Transcript(text="", duration_s=duration)

        if sample_rate != SAMPLE_RATE:
            samples = resample_to(samples, sample_rate, SAMPLE_RATE)

        model = self.load()
        t0 = time.perf_counter()

        chosen = (language or "").lower()[:2] or None
        language_confidence = 1.0
        if chosen is None:
            chosen, language_confidence = self.detect(samples)
            if language_confidence < self.detection_threshold:
                # Detection is unsure. Strongly accented English is regularly
                # heard as the speaker's mother tongue, so rather than trust a
                # coin-flip, decode in every candidate language and keep
                # whichever the decoder itself was most confident about.
                best = self._decode_best(model, samples, prompt, chosen)
                if best is not None:
                    return best.replace_meta(duration, time.perf_counter() - t0,
                                             language_confidence)

        segments, info = model.transcribe(
            samples,
            language=chosen,
            beam_size=self.beam_size,
            initial_prompt=prompt if prompt is not None else self.prompt_for(chosen),
            # Radio transmissions are short and complete; suppressing the
            # previous-text context stops one readback contaminating the next.
            condition_on_previous_text=False,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
            # A clipped or noisy transmission should come back empty rather
            # than as a hallucinated sentence.
            no_speech_threshold=0.55,
            log_prob_threshold=-1.0,
            temperature=[0.0, 0.2, 0.4],
        )

        texts: list[str] = []
        logprobs: list[float] = []
        for segment in segments:
            piece = (segment.text or "").strip()
            if piece:
                texts.append(piece)
                logprobs.append(getattr(segment, "avg_logprob", -1.0))

        text = " ".join(texts).strip()
        confidence = float(np.exp(np.mean(logprobs))) if logprobs else 0.0
        return Transcript(
            text=text,
            confidence=confidence,
            duration_s=duration,
            decode_s=time.perf_counter() - t0,
            language=getattr(info, "language", chosen) or chosen,
            language_confidence=language_confidence,
            segments=texts,
        )

    # How much a decode in the language detection actually picked has to be
    # beaten by. Detection being below the threshold makes it unreliable, not
    # worthless: at 0.83 it is still the best single piece of evidence there
    # is, so a rival language has to be clearly better rather than a hair
    # better to take the transmission away from it.
    DETECTED_MARGIN = 1.15

    def _decode_best(self, model, samples: np.ndarray, prompt: str | None,
                     detected: str = ""):
        """Decode in each configured language, keep the most convincing result.

        "Most convincing" is not simply the highest confidence. A decoder asked
        to hear one language in audio that is really another falls into a loop
        -- "um prédio, um prédio, um prédio" -- and a loop scores *well*,
        because each repeated token is exactly what the previous one predicts.
        A degenerate result is therefore thrown out before the comparison
        rather than being allowed to win it.
        """
        best = None
        best_score = 0.0
        for candidate in self.languages:
            try:
                result = self._decode(model, samples, candidate, prompt)
            except Exception as exc:
                log.debug("decode in %s failed: %s", candidate, exc)
                continue
            if result.empty or _looping(result.text):
                continue
            score = result.confidence
            if candidate == detected:
                score *= self.DETECTED_MARGIN
            if best is None or score > best_score:
                best, best_score = result, score
        return best

    def _decode(self, model, samples: np.ndarray, language: str,
                prompt: str | None) -> "Transcript":
        segments, info = model.transcribe(
            samples,
            language=language,
            beam_size=self.beam_size,
            initial_prompt=prompt if prompt is not None else self.prompt_for(language),
            condition_on_previous_text=False,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
            no_speech_threshold=0.55,
            log_prob_threshold=-1.0,
            temperature=[0.0, 0.2, 0.4],
        )
        texts: list[str] = []
        logprobs: list[float] = []
        for segment in segments:
            piece = (segment.text or "").strip()
            if piece:
                texts.append(piece)
                logprobs.append(getattr(segment, "avg_logprob", -1.0))
        return Transcript(
            text=" ".join(texts).strip(),
            confidence=float(np.exp(np.mean(logprobs))) if logprobs else 0.0,
            language=language,
            segments=texts,
        )

    def warm(self) -> None:
        """Load the model and run one tiny inference so the first call is fast."""
        self.load()
        self.transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32), SAMPLE_RATE,
                        language=self.languages[0])


# Below this many words a transcript is too short to tell a loop from a
# genuinely repetitive readback ("squawk seven seven zero zero").
_LOOP_MIN_WORDS = 10


def _looping(text: str) -> bool:
    """Whether a decode has fallen into a repetition loop.

    Real phraseology repeats words -- "zero six zero at one zero, runway zero
    seven" says "zero" three times -- so a single-word count proves nothing.
    What separates a loop is that the *pairs* stop varying too, which no
    readback does.
    """
    words = (text or "").lower().replace(",", " ").split()
    if len(words) < _LOOP_MIN_WORDS:
        return False
    if len(set(words)) / len(words) < 0.30:
        return True
    pairs = list(zip(words, words[1:]))
    return len(set(pairs)) / len(pairs) < 0.35


def resample_to(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Rational resampling between two rates."""
    if source_rate == target_rate:
        return audio.astype(np.float32)
    from fractions import Fraction

    from scipy import signal as sps

    ratio = Fraction(target_rate, source_rate).limit_denominator(1000)
    return sps.resample_poly(audio, ratio.numerator, ratio.denominator).astype(np.float32)


# Model sizes worth using, with rough CPU cost. "small.en" is the sweet spot:
# it reliably gets callsigns and numbers right, and still runs several times
# faster than real time on a modern desktop core.
MODEL_CHOICES = {
    "tiny.en":   "fastest, misses callsigns and digits too often",
    "base.en":   "usable on an old machine, still weak on numbers",
    "small.en":  "recommended for English only -- accurate and real-time on CPU",
    "small":     "recommended when flying in more than one language",
    "medium":    "better still, roughly 3x the cost of small",
    "large-v3":  "best accuracy, needs a GPU to stay interactive",
}


def model_for(languages: list[str], preferred: str = "small.en") -> str:
    """Pick a model that can actually hear the languages that may come up.

    The ``.en`` models are English-only and silently ignore a request for any
    other language. Which language will come up is not known at startup any
    more -- it depends on where the aeroplane goes -- so unless the caller has
    pinned the set to English, the multilingual model is the one to load.
    """
    codes = {l.lower()[:2] for l in languages or ["en"]}
    if codes <= {"en"}:
        return preferred
    return preferred[:-3] if preferred.endswith(".en") else preferred
