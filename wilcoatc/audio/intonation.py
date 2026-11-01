"""A transmission is not said on one note, or at one speed.

Measured on a recorded IFR clearance -- a controller reading a full
clearance to a Cessna -- against the same clearance cloned by Chatterbox from
a real controller's clip:

====================  ==========  ===========  ==============
                      phrase       overall f0    rate, syl/s
                      pitch sd     p10-p90       mean (range)
====================  ==========  ===========  ==============
the controller          3.3 st       7.8 st       6.8 (4.9-8.9)
Chatterbox, cloned      0.4 st       2.4 st       5.3 (4.4-6.7)
Chatterbox-Flash        0.7 st       2.7 st       4.7 (4.0-6.1)
====================  ==========  ===========  ==============

Within a phrase the clone is about as lively as the man (2.1 against 2.3
semitones). What it never does is move *between* phrases: the controller
says the callsign 6.6 semitones above his own median and slowly, at 4.9
syllables a second, then drops to his median for "climb and maintain five
thousand" and says it at 8.9. The clone says every phrase on the same note
at the same speed, which is what a listener hears as reading.

So this moves whole phrases. A phrase is what the text says it is -- the
stretch between two of the commas and full stops the phrasebook wrote and
the prosody layer turned into the boundaries it wanted -- found in the
audio as the longest pause near where the text puts each break. The
first version took every pause over 80 ms, and Chatterbox leaves clean
silences that long between words and inside them: a clearance came apart
into 26 "phrases", each word on its own random note, which is a stammer
and not intonation. Each phrase is given its own pitch offset and its own
speed: the address lifted
and a touch slower, the instructions scattered about the median, with a
slight fall across the transmission, and a little quicker than the engine
reads them. Inside a phrase nothing changes, so the engine's own contour,
the part it does well, is left alone, and every change of pitch or speed
lands in a pause where there is nothing to hear it happen.

**How.** The cloned voices take no pitch or rate, and a resample moves pitch
and duration together and drags the formants with them: four semitones of
that is a smaller person, not a raised voice. This is TD-PSOLA instead --
the waveform cut into two-period grains at its glottal pulses and laid back
down at a new spacing (pitch) and from a new place in the source (speed) --
which moves the pitch and the pace separately and keeps the formants where
the speaker had them. Its weakness is a wrong pitch mark, which is why the
f0 is a YIN track with octave errors smoothed out, and why unvoiced speech
is cut into short fixed grains and never pitch-shifted at all. The other
weakness is the same rule turned round: voiced speech the tracker calls
unvoiced stays on its old note while the phrase around it moves, which
is a wobble and was one -- YIN's 0.15 alone missed a third of a breathy
voice, so a frame is voiced at its best lag up to ``VOICING_CEILING``.

It runs on the dry voice, before the operator's breath and before the chain:
the chain is the radio, and a radio does not change how a man speaks.
"""

from __future__ import annotations

import random

import numpy as np

from .radio_fx import lowpass

#: The f0 tracker's analysis frame and hop, and the range it looks in. A
#: controller's voice sits between 80 and 300 Hz; the margins are for the
#: odd shout and the odd growl.
FRAME_S = 0.04
HOP_S = 0.005
F0_MIN = 70.0
F0_MAX = 400.0
#: YIN's threshold on the cumulative-mean-normalised difference. Its paper's
#: 0.1-0.15; above it a frame is called unvoiced rather than guessed at.
YIN_THRESHOLD = 0.15
#: A frame whose difference never dips under ``YIN_THRESHOLD`` is still
#: voiced, at its lowest point, when that is under this. The synthesiser's
#: low, breathy voices sit at 0.2-0.5 for whole vowels: at 0.15 alone YIN
#: called 36-44% of LTFM's and KJFK's voiced frames unvoiced, PSOLA left
#: those at their own pitch while the rest of the phrase moved, and the
#: voice flipped between two notes inside a word -- the wobble. 0.4 is the
#: knee measured on those takes (error under a 3-semitone lift 100 -> 39
#: cents at LTFM, 59 -> 12 at KJFK); 0.5 bought a few cents more and twice
#: the fricatives called voiced.
VOICING_CEILING = 0.4
#: Frames quieter than this under the loudest are unvoiced whatever YIN says:
#: the floor of a synthesiser's pause is periodic noise, not a voice.
VOICED_FLOOR_DB = -35.0
#: The pitch marks are found below this many times the speaker's median f0.
MARK_BAND = 1.5
#: The grain for unvoiced speech, which carries no pitch to follow.
UNVOICED_STEP_S = 0.008
#: The shortest silence that can be a phrase boundary, and the shortest run
#: of speech worth calling a phrase. Under 60 ms is a closure inside a word.
PAUSE_MS = 60.0
PHRASE_MS = 150.0
#: With no text to count breaks in, a boundary is a pause at least this
#: long -- longer than the gaps the engines leave between words.
UNCOUNTED_PAUSE_MS = 180.0


def breaks(text: str) -> list[float]:
    """Where the text's phrase boundaries are, as fractions of its length.

    Punctuation followed by a space, so "126.7" is not split and the full
    stop at the end is not counted. A fraction of the characters is a
    rough fraction of the time -- the phrasebook spells its numbers out --
    which is all :func:`phrases` asks of it: which pause to look near.
    """
    import re

    body = (text or "").strip()
    if not body:
        return []
    return [m.end() / len(body)
            for m in re.finditer(r"[,.;:!?](?=\s)", body)]


def f0_track(x: np.ndarray, sample_rate: int) -> np.ndarray:
    """Fundamental frequency per ``HOP_S`` frame, in Hz; 0 where unvoiced.

    YIN, with the difference function computed from the autocorrelation so
    that the whole transmission is one FFT rather than a loop over lags.
    """
    x = np.asarray(x, dtype=np.float64)
    n = int(sample_rate * FRAME_S)
    hop = int(sample_rate * HOP_S)
    if x.size < n:
        return np.zeros(0)
    frames = 1 + (x.size - n) // hop
    index = np.arange(n)[None, :] + hop * np.arange(frames)[:, None]
    framed = x[index]
    framed = framed - framed.mean(axis=1, keepdims=True)

    lag_min = max(2, int(sample_rate / F0_MAX))
    lag_max = min(n - 1, int(sample_rate / F0_MIN))
    spectrum = np.fft.rfft(framed, 2 * n, axis=1)
    acf = np.fft.irfft(np.abs(spectrum) ** 2, axis=1)[:, :lag_max + 1]
    squares = np.concatenate([np.zeros((frames, 1)),
                              np.cumsum(framed ** 2, axis=1)], axis=1)
    lags = np.arange(lag_max + 1)
    # d(tau) = sum over the overlap of (x_j - x_{j+tau})^2.
    diff = squares[:, n - lags] + (squares[:, n:n + 1] - squares[:, lags]) \
        - 2.0 * acf
    diff[:, 0] = 0.0
    running = np.cumsum(diff[:, 1:], axis=1)
    cmnd = np.ones_like(diff)
    cmnd[:, 1:] = diff[:, 1:] * lags[1:] / np.maximum(running, 1e-12)

    rms = np.sqrt(np.mean(framed ** 2, axis=1))
    loud = rms > (rms.max() if rms.size else 0.0) \
        * 10.0 ** (VOICED_FLOOR_DB / 20.0)
    f0 = np.zeros(frames)
    window = cmnd[:, lag_min:lag_max + 1]
    below = window < YIN_THRESHOLD
    for i in np.flatnonzero(loud):
        if below[i].any():
            # Down to the bottom of the dip the threshold first crossed into.
            lag = int(np.argmax(below[i]))
            while (lag + 1 < window.shape[1]
                   and window[i, lag + 1] < window[i, lag]):
                lag += 1
        elif window[i].min() < VOICING_CEILING:
            lag = int(np.argmin(window[i]))
        else:
            continue
        f0[i] = sample_rate / (lag + lag_min)

    # Octave errors: a voiced frame more than a fifth away from the median
    # of its voiced neighbours takes the median.
    voiced = np.flatnonzero(f0 > 0)
    if voiced.size > 4:
        logs = np.log2(f0[voiced])
        smoothed = logs.copy()
        for k in range(voiced.size):
            near = logs[max(0, k - 3):k + 4]
            centre = np.median(near)
            if abs(logs[k] - centre) > 7.0 / 12.0:
                smoothed[k] = centre
        f0[voiced] = 2.0 ** smoothed
    return f0


def _marks(x: np.ndarray, sample_rate: int,
           f0: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Where each grain is centred, how long its period is, and whether it
    is voiced. Voiced marks sit on the waveform's peaks one period apart;
    unvoiced ones are evenly spaced."""
    hop = int(sample_rate * HOP_S)
    centre = int(sample_rate * FRAME_S) // 2
    # Peaks of the low band are the glottal pulses; the full band has
    # formant ringing that would move the mark about within a period. The
    # band is the speaker's own: a fixed 900 Hz let the first formant
    # through for every voice under 300 Hz, the mark hopped between its
    # peaks, and successive marks were 12-15% apart in length at the 90th
    # percentile against 4% at one and a half times the median pitch. The
    # re-laying puts the pulses out at those spacings, so a hop is a tremble.
    sung = f0[f0 > 0] if f0.size else f0
    corner = MARK_BAND * float(np.median(sung)) if sung.size else 900.0
    corner = min(max(corner, 2.0 * F0_MIN), 900.0)
    smooth = lowpass(np.asarray(x, dtype=np.float32), sample_rate, corner, 4)
    step_u = int(sample_rate * UNVOICED_STEP_S)
    positions, periods, voiced = [], [], []
    t = 0
    last_voiced = False
    while t < x.size:
        frame = min(max(0, (t - centre) // hop), f0.size - 1) \
            if f0.size else -1
        hz = f0[frame] if frame >= 0 else 0.0
        if hz > 0:
            period = sample_rate / hz
            if last_voiced:
                guess = int(t + period)
                reach = int(0.3 * period)
            else:
                guess = int(t + period / 2)
                reach = int(period / 2)
            lo = max(0, guess - reach)
            hi = min(x.size, guess + reach + 1)
            if hi <= lo:
                break
            at = lo + int(np.argmax(smooth[lo:hi]))
            if at <= t:
                at = t + max(1, int(period))
            positions.append(at)
            periods.append(period)
            voiced.append(True)
            t = at
            last_voiced = True
        else:
            if last_voiced:
                # The voiced mark this follows is already a grain on this
                # very sample. A second one there is a gap of nothing, which
                # the re-laying cannot window; it used to happen every time
                # a voice stopped.
                t += step_u
                last_voiced = False
                continue
            positions.append(t)
            periods.append(float(step_u))
            voiced.append(False)
            t += step_u
            last_voiced = False
    return (np.asarray(positions, dtype=np.int64),
            np.asarray(periods, dtype=np.float64),
            np.asarray(voiced, dtype=bool))


def psola(x: np.ndarray, sample_rate: int, pitch: np.ndarray,
          speed: np.ndarray, f0: np.ndarray | None = None) -> np.ndarray:
    """Re-lay the voice's grains at ``pitch`` x its pitch and ``speed`` x
    its pace, each given per input sample.

    Speed above one is quicker; pitch above one is higher. Unvoiced grains
    keep their pitch, which they do not have.

    Each grain reaches from the mark before it to the mark after it, and its
    window rises over the one gap and falls over the other, so that asked
    for nothing the grains add back up to the voice exactly. The first
    version sized every grain, and stepped between them, by the tracker's
    smoothed period rather than by where the marks actually fell. On a tone
    the two agree; on speech they do not, the grains no longer summed to
    one, and asked for nothing it returned the voice at -3 dB signal to
    error with a quarter of its voicing gone -- which is what the pilot
    heard as every controller's voice shaking.
    """
    x = np.asarray(x, dtype=np.float32)
    if x.size == 0:
        return x
    if f0 is None:
        f0 = f0_track(x, sample_rate)
    positions, periods, voiced = _marks(x, sample_rate, f0)
    if positions.size == 0:
        return x
    # How far each grain reaches either side: to its neighbouring marks.
    gaps = np.diff(positions).astype(np.float64)
    if gaps.size:
        # The first and last reach to the ends, so nothing is cut off.
        before = np.concatenate([[max(gaps[0], positions[0])], gaps])
        after = np.concatenate([gaps, [max(gaps[-1],
                                           x.size - positions[-1])]])
    else:
        before = after = periods.copy()

    # Output time against input time: every input sample lasts 1/speed.
    out_of_in = np.concatenate([[0.0], np.cumsum(1.0 / np.maximum(speed, 0.2))])
    total = int(out_of_in[-1])
    in_axis = np.arange(out_of_in.size, dtype=np.float64)

    pad = int(sample_rate * 0.06)
    y = np.zeros(total + 2 * pad, dtype=np.float64)
    weight = np.zeros_like(y)
    padded = np.pad(x.astype(np.float64), pad)

    def ramp(length: int, rising: bool) -> np.ndarray:
        n = np.arange(length, dtype=np.float64) / length
        return 0.5 - 0.5 * np.cos(np.pi * n) if rising \
            else 0.5 + 0.5 * np.cos(np.pi * n)

    # From the first mark, not from zero: a grain is laid where its mark
    # falls, and starting a mark's width early puts every one after it off
    # its place.
    ts = float(np.interp(positions[0], in_axis, out_of_in))
    while ts < total:
        ta = float(np.interp(ts, out_of_in, in_axis))
        k = int(np.searchsorted(positions, ta))
        if k > 0 and (k == positions.size
                      or ta - positions[k - 1] < positions[k] - ta):
            k -= 1
        k = min(k, positions.size - 1)
        mark = int(positions[k])
        ratio = float(pitch[min(mark, pitch.size - 1)]) if voiced[k] else 1.0
        ratio = max(ratio, 0.2)
        # Lowered, the grains are laid further apart than they were cut;
        # widened by as much, they still meet rather than leaving a gap.
        stretch = max(1.0, 1.0 / ratio)
        left = int(min(pad - 1, max(1, round(before[k] * stretch))))
        right = int(min(pad - 1, max(1, round(after[k] * stretch))))
        window = np.concatenate([ramp(left, True), ramp(right, False)])
        grain = padded[pad + mark - left:pad + mark + right]
        at = int(round(ts)) + pad
        if grain.size != window.size or at + right > y.size:
            break
        y[at - left:at + right] += grain * window
        weight[at - left:at + right] += window
        ts += max(1.0, after[k] / ratio)

    y = y[pad:pad + total]
    weight = weight[pad:pad + total]
    floor = 0.25 * float(np.median(weight[weight > 0])) if np.any(weight > 0) \
        else 1.0
    return (y / np.maximum(weight, floor)).astype(np.float32)


def phrases(voice: np.ndarray, sample_rate: int,
            where: list[float] | None = None) -> list[tuple[int, int]]:
    """The runs of speech between the phrase boundaries.

    ``where`` is the text's boundaries (:func:`breaks`). For each, the pause
    taken is the one that is longest for how far it is from where the text
    puts it -- within an eighth of the transmission either side, and never
    one already taken. The longest pauses alone are not enough: Chatterbox
    leaves its longest in the middle of a phrase about as often as at the
    end of one. Without ``where``, every pause of ``UNCOUNTED_PAUSE_MS`` or
    more is a boundary.
    """
    from .imperfections import _pauses, _speech_bounds

    start, end = _speech_bounds(voice, sample_rate)
    if end <= start:
        return []
    if where is None:
        cuts = _pauses(voice, sample_rate, UNCOUNTED_PAUSE_MS)
    else:
        found = _pauses(voice, sample_rate, PAUSE_MS)
        span = end - start
        reach = max(span / 8.0, 0.6 * sample_rate)
        taken: set[int] = set()
        for fraction in where:
            expected = start + fraction * span
            best, score = None, 0.0
            for n, (lo, hi) in enumerate(found):
                distance = abs((lo + hi) / 2.0 - expected)
                if n in taken or distance > reach:
                    continue
                # A second of distance costs as much as half a second of
                # pause: near and long beats far and longer.
                value = (hi - lo) - 0.5 * distance
                if best is None or value > score:
                    best, score = n, value
            if best is not None:
                taken.add(best)
        cuts = sorted(found[n] for n in taken)
    runs, cursor = [], start
    for lo, hi in cuts:
        if lo > cursor:
            runs.append((cursor, lo))
        cursor = max(cursor, hi)
    if end > cursor:
        runs.append((cursor, end))
    shortest = int(sample_rate * PHRASE_MS / 1000.0)
    return [(a, b) for a, b in runs if b - a >= shortest]


def plan(count: int, settings, rng: random.Random) -> list[tuple[float, float]]:
    """(semitones, speed) for each of ``count`` phrases.

    The first is the address when there is more than one: lifted, as the
    controller in the recording lifts his, and a touch slower, because the
    callsign is the one thing that must be heard. The rest scatter about
    the median and fall a little across the transmission -- declination,
    which every speaker of every language has -- at the instructions' pace.
    """
    out = []
    spread = max(0.0, float(settings.phrase_pitch_st))
    for i in range(count):
        if i == 0 and count > 1 and settings.address_lift_st > 0.0:
            semitones = settings.address_lift_st * rng.uniform(0.7, 1.0)
            speed = rng.uniform(0.93, 1.0)
        else:
            drawn = rng.gauss(0.0, spread) if spread > 0.0 else 0.0
            semitones = max(-2.5 * spread, min(2.5 * spread, drawn)) \
                - min(1.5, 0.25 * i)
            pace = rng.gauss(settings.instruction_pace, settings.phrase_rate) \
                if settings.phrase_rate > 0.0 else settings.instruction_pace
            speed = max(0.88, min(1.2, pace))
        out.append((semitones, speed))
    return out


def shape(voice: np.ndarray, sample_rate: int, settings,
          rng: random.Random | None = None,
          text: str | None = None) -> np.ndarray:
    """The voice with each phrase on its own note and at its own pace.

    ``text`` is what was said, for :func:`breaks` to count the phrases in.
    """
    if not getattr(settings, "enabled", False) or voice is None:
        return voice
    voice = np.asarray(voice, dtype=np.float32)
    if voice.size == 0:
        return voice
    quiet = (settings.phrase_pitch_st <= 0.0 and settings.address_lift_st <= 0.0
             and settings.phrase_rate <= 0.0
             and abs(settings.instruction_pace - 1.0) < 1e-6)
    if quiet:
        return voice
    runs = phrases(voice, sample_rate,
                   breaks(text) if text is not None else None)
    if not runs:
        return voice
    rng = rng or random.Random()
    pitch = np.ones(voice.size, dtype=np.float32)
    speed = np.ones(voice.size, dtype=np.float32)
    for (a, b), (semitones, pace) in zip(runs, plan(len(runs), settings, rng)):
        pitch[a:b] = 2.0 ** (semitones / 12.0)
        speed[a:b] = pace
    return psola(voice, sample_rate, pitch, speed)


__all__ = ["breaks", "f0_track", "phrases", "plan", "psola", "shape"]
