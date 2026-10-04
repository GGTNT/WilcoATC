"""Turn clean speech into VHF aviation radio.

A neutral text-to-speech voice played straight sounds like a phone assistant.
What makes ATC audio instantly recognisable is the *channel*, not the speaker,
and the details that carry the illusion are mostly things that happen around
the words rather than to them.

Measured against a real recording, a naive band-pass-and-hiss chain gets four
things wrong, and each is fixed below:

*The noise floor sits too far down.* Real ATC hiss is 18-25 dB under the voice
and plainly audible; 40 dB down reads as a clean studio recording with a filter
on it.

*The noise does not breathe.* An AM receiver runs automatic gain control, so
when the controller pauses the AGC pulls gain up and the hiss audibly swells,
then ducks again as the next word starts. That "shhh - words - shhh" pattern is
the single most recognisable feature of the medium, and a fixed noise bed does
not have it.

*Nobody keys a microphone.* A transmission does not begin with speech and does
not end with it. The carrier appears, the receiver quiets under it, the voice
follows, the key is held a beat after the last word, and then the gain control
puts out a blast of noise into an empty channel before the squelch shuts. Most
of that is not speech and all of it is what says a person is at the other end.
Note the direction at the head: the channel goes *quiet*. A burst of noise
there -- which is what this did for a long time -- is a sound effect, not a
radio.

*Nothing is behind the controller.* A real transmission carries the ops room
with it -- low rumble, air handling, the room the microphone is standing in.
That ambience goes through the transmitter with the voice, so it is band-limited
and compressed along with it, not laid on top afterwards.

The chain, in the order the signal really travels:

    at the controller     a hand on the bar, room ambience + voice -> microphone
    in the transmitter    pre-emphasis, AGC, overdrive, band limiting
    over the air          path loss, flutter, fading
    in the receiver       carrier capture, AGC-driven noise floor, audio stage,
                          squelch
    in the cockpit        small speaker resonance

Everything is numpy and scipy on float32 and runs hundreds of times faster than
real time, so it costs nothing next to synthesis.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import signal as sps

# The AM aviation voice channel. ICAO Annex 10 guarantees 300-2500 Hz; real
# equipment passes a little more, and 3400 Hz is the usual design figure.
BAND_LOW_HZ = 300.0
BAND_HIGH_HZ = 3400.0


@dataclass(frozen=True)
class RadioProfile:
    """The character of one radio path, from the microphone to the speaker."""

    # --- channel ---
    band_low: float = BAND_LOW_HZ
    band_high: float = BAND_HIGH_HZ
    band_order: int = 6
    # Per-unit response tilt in dB across the passband. Positive is brighter.
    # Varied per station so two facilities do not sound like the same radio.
    tilt_db: float = 0.0

    # --- the controller's microphone and room ---
    # Ambience level relative to the voice, before the transmitter. This is
    # mixed in *early* so it is band-limited and compressed with the speech,
    # which is what makes it sit behind the voice rather than on top of it.
    room_db: float = -34.0
    room_rumble_hz: float = 120.0
    presence_db: float = 3.5          # transmitter pre-emphasis
    presence_hz: float = 2000.0
    de_ess_db: float = -3.0

    # --- transmitter ---
    # Aviation radios squash hard, so a weak station stays readable next to a
    # strong one. The target is 25-35 dB of dynamic range in the result.
    comp_threshold_db: float = -28.0
    comp_ratio: float = 7.0
    comp_attack_ms: float = 2.0
    comp_release_ms: float = 120.0
    makeup_db: float = 10.0
    drive: float = 2.1
    clip_softness: float = 0.82

    # --- the cockpit speaker ---
    speaker_peak_hz: float = 1650.0
    speaker_peak_db: float = 5.0
    speaker_q: float = 1.1

    # --- the receiver ---
    # Noise sits this far under the voice. Real ATC is 18-25 dB; further down
    # and it stops sounding like radio.
    noise_db: float = -30.0
    # How much louder the hiss gets when the controller stops talking. This is
    # the AGC breathing, and it is what the ear recognises as "a radio".
    breathing_db: float = 7.0
    hum_db: float = -62.0
    # --- keying ----------------------------------------------------------
    #
    # What a listener actually hears as somebody picking up a microphone, in
    # the order it happens. This is the part of the chain that says a *person*
    # is at the other end rather than a file being played, and none of it is
    # speech.
    #
    # Key-up. The controller presses the bar. The transmitter's carrier
    # appears at the receiver, which sees it as a step: a low thump through the
    # audio stage, and a switching transient that rings the cockpit speaker.
    # For an instant the receiver is still running at open-squelch gain, so the
    # hiss is *louder* than it will be during the transmission -- and then the
    # carrier quiets it, and the floor drops. That drop is the cue. It is the
    # opposite of a noise burst, which is what this used to do at both ends.
    #
    # Key-down. The carrier vanishes instantly. The gain control is still
    # wound up for a signal that is no longer there, so full-scale noise comes
    # through for a few tens of milliseconds -- the "kssht" -- until the
    # squelch notices and shuts. Then nothing, hard.
    #
    # Total time from carrier to first word, and from last word to silence.
    # Zero at either end turns the whole thing off, which is what the profiles
    # that are not radios do.
    squelch_open_ms: float = 150.0
    squelch_tail_ms: float = 150.0
    # The switching transient, and the cone ringing it leaves behind. This is
    # one of the loudest things on the channel -- a step into a compressed,
    # band-limited path -- so it sits closer to the speech than instinct
    # suggests. At 20 dB down it reads as a tick on a recording; at 13 it
    # reads as somebody's thumb.
    ptt_click_db: float = -13.0
    click_ring_ms: float = 22.0
    # The detector's DC step as the carrier arrives and leaves.
    key_thump_db: float = -24.0
    key_thump_hz: float = 85.0
    # How far above the in-transmission floor the hiss sits before the carrier
    # captures the receiver, and how long the capture takes.
    open_noise_db: float = 7.0
    quieting_ms: float = 55.0
    # Some radios tick twice on key-up: the relay, then the carrier detect.
    relay_chance: float = 0.4
    # The key is held a beat after the last word, and the controller's room is
    # on the air for it. Not always -- see ``unkey_early_chance``.
    hold_ms: float = 150.0
    # ...and sometimes they let go on the last syllable instead, which clips
    # it. Real, common, and kept rare and shallow because a clearance still has
    # to survive -- 45 ms is inside the silence the synthesiser leaves after
    # the last word, and through the recogniser it costs nothing: 1.00 of the
    # words back over 48 renderings with it and 1.00 over 48 without.
    unkey_early_chance: float = 0.18
    unkey_early_ms: float = 45.0
    # The AGC's parting blast, above the in-transmission floor, and how fast
    # the squelch then shuts.
    tail_burst_db: float = 11.0
    tail_burst_ms: float = 60.0
    gate_close_ms: float = 25.0
    # What a muted receiver actually puts out. Not digital silence: the
    # channel is gone but the audio panel is still running, and a clip that
    # ends in true zero reads as an edit rather than as a squelch. Deep enough
    # to be obviously shut, shallow enough that a two-word transmission is not
    # mostly dead air.
    muted_db: float = -54.0
    # Slow amplitude flutter, as a fraction. Airborne paths wobble; a ground
    # station two miles away does not.
    flutter: float = 0.0
    flutter_hz: float = 3.5

    # --- the AM link itself ---
    # A diode envelope detector is not symmetrical: the negative half of an
    # over-modulated carrier flattens against zero while the positive half is
    # merely compressed. Aviation transmitters run close to full modulation,
    # so this asymmetry is on nearly every loud syllable, and it is most of
    # what separates "band-limited speech" from "a radio".
    detector_asymmetry: float = 0.45
    # The receiver's AGC needs a moment to settle after the squelch opens, so
    # the first syllable of a transmission arrives hot and is pulled down.
    agc_overshoot_db: float = 3.0
    agc_settle_ms: float = 260.0
    # The controller keys the microphone, then speaks. What is on the air in
    # between is them.
    breath_db: float = -30.0
    # A small speaker is not linear either; a little second harmonic is what
    # makes it sound like a speaker rather than like headphones.
    speaker_harmonic: float = 0.05

    output_peak: float = 0.86
    # Real transmissions are not all equally loud: different transmitters,
    # different distances, different people leaning into the microphone. Every
    # transmission arriving at exactly the same peak is a strong tell, so the
    # level is jittered per transmission by up to this much.
    level_jitter_db: float = 3.5


# A controller heard through a good ground station close by.
PROFILE_CLEAN = RadioProfile()

# ATIS and other recorded broadcasts. Recorded once through a headset in a
# quiet room, then replayed through the same transmitter for hours: duller,
# more compressed, no room ambience, and a steadier noise floor because the
# level never changes.
PROFILE_ATIS = RadioProfile(
    # A recording, replayed. Nobody breathes into it and the level never moves.
    breath_db=-90.0,
    agc_overshoot_db=1.0,
    level_jitter_db=0.0,
    band_high=3000.0,
    tilt_db=-2.0,
    room_db=-70.0,
    presence_db=2.0,
    comp_threshold_db=-34.0,
    comp_ratio=12.0,
    makeup_db=12.0,
    drive=1.6,
    speaker_peak_db=4.0,
    noise_db=-30.0,
    breathing_db=3.0,
    hum_db=-52.0,
    # A transmitter nobody keys: it has been up for hours and will be up for
    # hours more. You are tuning into the middle of it, so there is a gate
    # opening and closing at your receiver and nothing at the far end -- no
    # relay, no hand on a bar, no room going quiet afterwards.
    squelch_open_ms=70.0,
    squelch_tail_ms=80.0,
    relay_chance=0.0,
    hold_ms=0.0,
    unkey_early_chance=0.0,
    tail_burst_db=5.0,
    tail_burst_ms=40.0,
    key_thump_db=-34.0,
)

# The pilot's own transmission heard as sidetone: their own headset, so no
# channel noise and no squelch, but the same band limiting and clipping.
PROFILE_SIDETONE = RadioProfile(
    breath_db=-90.0,
    agc_overshoot_db=0.0,
    level_jitter_db=1.0,
    room_db=-70.0,
    noise_db=-52.0,
    breathing_db=0.0,
    hum_db=-90.0,
    # You are the one keying, and you can feel the switch. What you hear of
    # it is the transmitter starting and stopping, not a receiver gating.
    squelch_open_ms=0.0,
    squelch_tail_ms=0.0,
    ptt_click_db=-30.0,
    hold_ms=0.0,
    unkey_early_chance=0.0,
    makeup_db=6.0,
    drive=1.5,
)

# The other seat, over the flight interphone.
#
# Not a radio at all, and the point of the profile is everything it turns off.
# There is no channel between the two pilots -- no squelch, no carrier, no AM
# detector, no path noise -- and the last of those matters most: a first
# officer who arrives with a squelch burst is a second controller, and the ear
# files them with the radio instead of with the cockpit. What is left is a
# headset: wide, close, a little compressed by the intercom amplifier, with
# the faint hiss of a live microphone two feet away.
PROFILE_INTERCOM = RadioProfile(
    band_low=180.0,
    band_high=7000.0,
    band_order=2,
    tilt_db=1.0,
    # A quiet flight deck, not a control room.
    room_db=-44.0,
    room_rumble_hz=90.0,
    presence_db=2.5,
    presence_hz=2600.0,
    comp_threshold_db=-26.0,
    comp_ratio=3.0,
    makeup_db=5.0,
    drive=1.2,
    clip_softness=0.95,
    speaker_peak_hz=2400.0,
    speaker_peak_db=1.5,
    speaker_q=0.8,
    noise_db=-46.0,
    breathing_db=1.0,
    hum_db=-72.0,
    # Nothing keys, so nothing clicks, nothing opens, nobody holds a bar down
    # afterwards and nobody clips their own last syllable letting go of one.
    squelch_open_ms=0.0,
    squelch_tail_ms=0.0,
    ptt_click_db=-90.0,
    hold_ms=0.0,
    unkey_early_chance=0.0,
    flutter=0.0,
    detector_asymmetry=0.0,
    agc_overshoot_db=0.0,
    breath_db=-40.0,
    speaker_harmonic=0.02,
    level_jitter_db=1.0,
)

# The cabin address system, heard from the flight deck.
#
# A different problem from the intercom: this one *is* a channel, just not a
# radio one. A small ceiling speaker at the front of a pressurised tube --
# narrow, boxy, hard-limited, and with the cabin itself audible around it.
PROFILE_CABIN = RadioProfile(
    band_low=260.0,
    band_high=4200.0,
    band_order=4,
    tilt_db=-1.5,
    # The cabin: air, engines, and a hundred people.
    room_db=-26.0,
    room_rumble_hz=70.0,
    presence_db=3.0,
    presence_hz=2200.0,
    comp_threshold_db=-30.0,
    comp_ratio=9.0,
    makeup_db=9.0,
    drive=1.7,
    clip_softness=0.8,
    # The boxy peak of a small moulded speaker.
    speaker_peak_hz=1250.0,
    speaker_peak_db=6.0,
    speaker_q=1.6,
    noise_db=-34.0,
    breathing_db=2.0,
    hum_db=-58.0,
    squelch_open_ms=0.0,
    squelch_tail_ms=0.0,
    ptt_click_db=-34.0,
    # No carrier to key, but there is a handset, and a purser holds it for a
    # moment after the last word before putting it back in its cradle.
    hold_ms=220.0,
    unkey_early_chance=0.0,
    flutter=0.0,
    detector_asymmetry=0.12,
    agc_overshoot_db=1.0,
    breath_db=-34.0,
    speaker_harmonic=0.09,
    level_jitter_db=1.5,
)

# An enroute centre a long way off: weaker, flutterier, duller.
PROFILE_DISTANT = RadioProfile(
    tilt_db=-3.0,
    noise_db=-18.0,
    breathing_db=10.0,
    flutter=0.10,
    squelch_open_ms=160.0,
)


# --------------------------------------------------------------------------
# building blocks
# --------------------------------------------------------------------------


def _db_to_lin(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def pitch_shift(audio: np.ndarray, semitones: float, sample_rate: int) -> np.ndarray:
    """Shift pitch by resampling.

    Resampling changes duration as well as pitch. That is intentional: the
    synthesiser is asked for a compensating ``length_scale`` so the two cancel,
    which avoids needing a phase vocoder for a couple of semitones of shading.
    """
    if abs(semitones) < 0.01:
        return audio
    ratio = 2.0 ** (semitones / 12.0)
    up, down = _ratio_to_fraction(1.0 / ratio)
    return sps.resample_poly(audio, up, down).astype(np.float32)


def _ratio_to_fraction(ratio: float, max_den: int = 400) -> tuple[int, int]:
    from fractions import Fraction

    frac = Fraction(ratio).limit_denominator(max_den)
    return max(1, frac.numerator), max(1, frac.denominator)


def bandpass(
    audio: np.ndarray, sample_rate: int, low: float, high: float, order: int = 6
) -> np.ndarray:
    """Band-limit to the AM voice channel using a stable SOS cascade."""
    nyq = sample_rate / 2.0
    low_n = max(1e-4, min(0.99, low / nyq))
    high_n = max(low_n + 1e-4, min(0.99, high / nyq))
    sos = sps.butter(order, [low_n, high_n], btype="bandpass", output="sos")
    return sps.sosfilt(sos, audio).astype(np.float32)


def highpass(audio: np.ndarray, sample_rate: int, cutoff: float,
             order: int = 2) -> np.ndarray:
    nyq = sample_rate / 2.0
    sos = sps.butter(order, max(1e-4, min(0.99, cutoff / nyq)),
                     btype="highpass", output="sos")
    return sps.sosfilt(sos, audio).astype(np.float32)


def lowpass(audio: np.ndarray, sample_rate: int, cutoff: float,
            order: int = 2) -> np.ndarray:
    nyq = sample_rate / 2.0
    sos = sps.butter(order, max(1e-4, min(0.99, cutoff / nyq)),
                     btype="lowpass", output="sos")
    return sps.sosfilt(sos, audio).astype(np.float32)


def peaking_eq(
    audio: np.ndarray, sample_rate: int, freq: float, gain_db: float, q: float
) -> np.ndarray:
    """Single peaking biquad."""
    if abs(gain_db) < 0.05:
        return audio
    a = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * min(freq, sample_rate * 0.45) / sample_rate
    alpha = math.sin(w0) / (2.0 * max(0.1, q))
    cos_w0 = math.cos(w0)

    b = [1 + alpha * a, -2 * cos_w0, 1 - alpha * a]
    a_c = [1 + alpha / a, -2 * cos_w0, 1 - alpha / a]
    b = [x / a_c[0] for x in b]
    a_c = [x / a_c[0] for x in a_c]
    return sps.lfilter(b, a_c, audio).astype(np.float32)


def tilt_eq(audio: np.ndarray, sample_rate: int, tilt_db: float) -> np.ndarray:
    """Tilt the response across the passband.

    A gentle shelf pair: positive brightens, negative darkens. Different
    facilities get different tilts so their radios do not sound identical.
    """
    if abs(tilt_db) < 0.05:
        return audio
    out = peaking_eq(audio, sample_rate, 700.0, -tilt_db / 2.0, 0.7)
    return peaking_eq(out, sample_rate, 2400.0, tilt_db / 2.0, 0.7)


def envelope(audio: np.ndarray, sample_rate: int, attack_ms: float,
             release_ms: float) -> np.ndarray:
    """Amplitude envelope with separate attack and release times."""
    rectified = np.abs(audio).astype(np.float32)

    def onepole(x: np.ndarray, tau_ms: float) -> np.ndarray:
        a = math.exp(-1.0 / max(1.0, sample_rate * tau_ms / 1000.0))
        return sps.lfilter([1.0 - a], [1.0, -a], x).astype(np.float32)

    return np.maximum(onepole(rectified, attack_ms), onepole(rectified, release_ms))


def compress(
    audio: np.ndarray,
    sample_rate: int,
    threshold_db: float,
    ratio: float,
    attack_ms: float,
    release_ms: float,
) -> np.ndarray:
    """Feed-forward compressor.

    This is what flattens a transmission into the narrow, always-present level
    that makes distant and close stations equally readable.
    """
    if ratio <= 1.0:
        return audio
    smoothed = envelope(audio, sample_rate, attack_ms, release_ms)
    level_db = 20.0 * np.log10(smoothed + 1e-9)
    over = np.maximum(0.0, level_db - threshold_db)
    gain_db = -over * (1.0 - 1.0 / ratio)
    return (audio * (10.0 ** (gain_db / 20.0))).astype(np.float32)


# Kept under its old name for callers that imported it.
_compress_fast = compress


def soft_clip(audio: np.ndarray, drive: float, softness: float = 0.85) -> np.ndarray:
    """Transmitter overdrive.

    ``tanh`` gives the rounded saturation of an AM transmitter rather than the
    harsh edge of hard digital clipping; ``softness`` blends toward hard
    limiting for a more strained sound.
    """
    driven = audio * max(0.1, drive)
    soft = np.tanh(driven)
    hard = np.clip(driven, -1.0, 1.0)
    return (softness * soft + (1.0 - softness) * hard).astype(np.float32)


def am_detector(audio: np.ndarray, asymmetry: float) -> np.ndarray:
    """The distortion a diode envelope detector adds to over-modulated AM.

    A symmetrical clipper adds odd harmonics and sounds like a fuzz pedal. An
    AM detector is not symmetrical: the carrier envelope cannot go below zero,
    so the negative half of an over-modulated signal flattens while the
    positive half is only compressed. That produces *even* harmonics, which is
    a different and much more familiar sound -- it is the crunch on a loud
    syllable that says "radio" before any of the hiss does.

    Aviation transmitters are run hot deliberately, so this happens on nearly
    every stressed vowel of every transmission.
    """
    if asymmetry <= 0.001:
        return audio
    # Only the trough saturates. Rounding both halves -- which is what a
    # symmetrical tanh does -- puts the third harmonic back level with the
    # second and undoes the point of the exercise: measured on a 1 kHz tone,
    # tanh on both sides gives H2 -18.8 dB and H3 -20.8 dB, two dB apart,
    # while leaving the positive half alone gives -15.9 and -24.7, which is
    # the nine-dB even-order lead a detector actually has.
    k = 1.0 + 3.0 * float(asymmetry)
    x = np.asarray(audio, dtype=np.float32)
    out = np.where(x >= 0.0, x, np.tanh(x * k) / k).astype(np.float32)
    # The asymmetry leaves a DC component; the audio stage would not pass it.
    return (out - float(np.mean(out))).astype(np.float32)


def agc_settle(audio: np.ndarray, sample_rate: int, overshoot_db: float,
               settle_ms: float) -> np.ndarray:
    """Let the first syllable through hot, then pull it down.

    The receiver's gain control has been sitting on noise; when the carrier
    appears it takes a couple of hundred milliseconds to find the new level.
    Until it does, the transmission is a few dB loud. Every real recording
    starts this way and no synthesised one does.
    """
    if overshoot_db <= 0.01 or audio.size == 0:
        return audio
    # Written as a gain that *falls* to its settled value rather than one that
    # starts above unity. The audible result is the same -- the opening is a
    # few dB hotter than the rest -- but the peak of the transmission does not
    # move, so the headroom this costs does not come out of the noise floor
    # when the caller normalises. An AGC reduces gain on a strong signal; it
    # has none to add.
    tau = max(1.0, settle_ms) / 1000.0 * sample_rate
    t = np.arange(audio.shape[0], dtype=np.float32)
    settled = _db_to_lin(-abs(overshoot_db))
    gain = settled + (1.0 - settled) * np.exp(-t / tau)
    return (audio * gain).astype(np.float32)


def _breath(sample_rate: int, level_db: float,
            rng: np.random.Generator) -> np.ndarray:
    """The controller keying the microphone and taking a breath before speaking.

    Not on every transmission -- roughly one in three, which is about how often
    it is audible on a real ground frequency. It goes in ahead of the voice and
    inside the transmission, so it is band-limited and compressed like the
    speech rather than laid over the top.
    """
    if level_db < -80.0 or rng.random() > 0.35:
        return np.zeros(0, dtype=np.float32)
    n = int(sample_rate * rng.uniform(0.16, 0.28))
    noise = rng.standard_normal(n).astype(np.float32)
    noise = bandpass(noise, sample_rate, 350.0, 1800.0, 2)
    rise = int(n * 0.35)
    shape = np.concatenate([
        np.linspace(0.0, 1.0, rise, dtype=np.float32) ** 1.5,
        np.linspace(1.0, 0.0, n - rise, dtype=np.float32) ** 2.0,
    ])[:n]
    return (noise * shape * _db_to_lin(level_db)).astype(np.float32)


def _key_held(audio: np.ndarray, sample_rate: int, p: RadioProfile,
              rng: np.random.Generator) -> np.ndarray:
    """How the controller lets go of the bar, at the end of the last word.

    Two things people do, and the difference between them is audible on every
    frequency. Mostly they hold the key a beat after they finish, and what
    goes out in that beat is their room -- appended here as silence, before the
    room tone is mixed, so the room fills it and is band-limited and compressed
    like the rest of the transmission. Occasionally they let go on the last
    syllable and clip it.

    The clip is kept rare and shallow. It is one of the most recognisable
    things a real frequency does, and it is also the only effect in this module
    that could cost a pilot a word, so it was measured rather than assumed: 45
    ms off the tail, sometimes, recovers 1.00 of the words through the
    recogniser over 48 renderings, which is what it recovers without it. That
    is because 45 ms is inside the silence the synthesiser already leaves after
    the last word -- the clip lands on the pause, not on the vowel.
    """
    if audio.size == 0:
        return audio
    if p.unkey_early_ms > 0.0 and rng.random() < p.unkey_early_chance:
        cut = int(sample_rate * p.unkey_early_ms / 1000.0)
        if 0 < cut < audio.shape[0] // 4:
            clipped = audio[:-cut].copy()
            # Cut the carrier, do not fade the voice: the transmitter stops,
            # so the word stops, mid-vowel. A ramp would sound like an editor.
            edge = max(2, int(sample_rate * 0.002))
            clipped[-edge:] *= np.linspace(1.0, 0.0, edge, dtype=np.float32)
            return clipped
    hold = int(sample_rate * max(0.0, p.hold_ms) / 1000.0)
    if hold <= 0:
        return audio
    return np.concatenate([audio, np.zeros(hold, dtype=np.float32)])


def speaker_harmonic(audio: np.ndarray, amount: float) -> np.ndarray:
    """A little second harmonic, the way a small cone driven hard behaves."""
    if amount <= 0.001:
        return audio
    x = np.asarray(audio, dtype=np.float32)
    second = x * np.abs(x)
    out = x + float(amount) * second
    return (out - float(np.mean(out))).astype(np.float32)



def de_ess(audio: np.ndarray, sample_rate: int, gain_db: float) -> np.ndarray:
    """Tame sibilance, which a narrow channel exaggerates unpleasantly."""
    return peaking_eq(audio, sample_rate, 3000.0, gain_db, 1.6)


# --------------------------------------------------------------------------
# noise sources
# --------------------------------------------------------------------------


def _channel_noise(
    length: int, sample_rate: int, rng: np.random.Generator
) -> np.ndarray:
    """Unit-level band-limited hiss, shaped like an AM receiver's noise floor."""
    if length <= 0:
        return np.zeros(0, dtype=np.float32)
    noise = rng.standard_normal(length).astype(np.float32)
    noise = bandpass(noise, sample_rate, BAND_LOW_HZ, BAND_HIGH_HZ, order=4)
    # A slight low-frequency tilt: real channel noise is not flat.
    tilted = sps.lfilter([1.0], [1.0, -0.25], noise).astype(np.float32)
    return _normalise(tilted)


def _room_tone(
    length: int, sample_rate: int, rumble_hz: float, rng: np.random.Generator
) -> np.ndarray:
    """The controller's ops room, at unit level.

    Low rumble from air handling and the building, plus a thin broadband layer
    for the room itself. Mixed in before the transmitter so it is band-limited
    and compressed with the voice, which is what puts it behind the controller
    rather than over them.
    """
    if length <= 0:
        return np.zeros(0, dtype=np.float32)
    noise = rng.standard_normal(length).astype(np.float32)
    rumble = lowpass(noise, sample_rate, rumble_hz, order=2)
    body = bandpass(noise, sample_rate, 200.0, 1200.0, order=2)
    # Slow level drift, so it is not a static wash.
    t = np.arange(length, dtype=np.float32) / sample_rate
    drift = 1.0 + 0.25 * np.sin(2 * math.pi * 0.11 * t + rng.random() * 6.28)
    return _normalise((rumble * 1.0 + body * 0.35) * drift)


def _mains_hum(
    length: int, sample_rate: int, level_db: float, rng: np.random.Generator
) -> np.ndarray:
    """A trace of mains hum, mostly audible in recorded broadcasts."""
    if level_db < -80 or length <= 0:
        return np.zeros(length, dtype=np.float32)
    t = np.arange(length, dtype=np.float32) / sample_rate
    base = 60.0 if rng.random() < 0.5 else 50.0
    hum = np.sin(2 * math.pi * base * t) + 0.4 * np.sin(2 * math.pi * base * 2 * t)
    return (hum / 1.4 * _db_to_lin(level_db)).astype(np.float32)


def _ptt_click(
    sample_rate: int, level_db: float, rng: np.random.Generator,
    length_ms: float = 9.0,
) -> np.ndarray:
    """The transient of a carrier appearing or disappearing."""
    n = max(4, int(sample_rate * length_ms / 1000.0))
    click = rng.standard_normal(n).astype(np.float32)
    click *= np.exp(-np.linspace(0.0, 7.0, n)).astype(np.float32)
    click = bandpass(click, sample_rate, 400.0, 3000.0, order=3)
    return (_normalise(click) * _db_to_lin(level_db)).astype(np.float32)


def _carrier_click(
    sample_rate: int, p: RadioProfile, level_db: float,
    rng: np.random.Generator, *, down: bool = False,
) -> np.ndarray:
    """The whole transient of a carrier switching on or off.

    Three things at once, which is why a bare noise burst never sounded right:

    * the switching transient itself, a few milliseconds of broadband;
    * the cockpit speaker ringing at its resonance afterwards, because that is
      what a small cone does when it is hit with a step -- the ring is most of
      what makes a click sound like it came out of a speaker rather than out
      of a file;
    * the detector's DC step, a low thump, downward when the carrier leaves.

    The three are summed rather than layered end to end, because they are one
    event.
    """
    ring_n = max(4, int(sample_rate * p.click_ring_ms / 1000.0))
    thump_n = max(4, int(sample_rate * 40.0 / 1000.0))
    n = max(ring_n, thump_n)
    out = np.zeros(n, dtype=np.float32)

    edge = _ptt_click(sample_rate, 0.0, rng, length_ms=5.0)
    out[: edge.shape[0]] += edge[: n]

    # The speaker's own impulse response: a decaying sinusoid at its peak.
    t = np.arange(ring_n, dtype=np.float32) / sample_rate
    decay = np.exp(-t * (6.0 / max(p.click_ring_ms / 1000.0, 1e-3)))
    ring = np.sin(2 * math.pi * p.speaker_peak_hz * t + rng.random() * 6.28)
    out[:ring_n] += (ring * decay * 0.55).astype(np.float32)
    out = _normalise(out) * _db_to_lin(level_db)

    # The thump is generated at its own level rather than scaled with the
    # click, because it comes from the detector and not from the switch.
    if p.key_thump_db > -80.0:
        tt = np.arange(thump_n, dtype=np.float32) / sample_rate
        shape = np.exp(-tt * 45.0).astype(np.float32)
        thump = np.sin(2 * math.pi * p.key_thump_hz * tt) * shape
        if down:
            thump = -thump
        out[:thump_n] += (thump * _db_to_lin(p.key_thump_db)).astype(np.float32)
    return out.astype(np.float32)


def _normalise(x: np.ndarray) -> np.ndarray:
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    return (x / peak).astype(np.float32) if peak > 0 else x.astype(np.float32)


# --------------------------------------------------------------------------
# the chain
# --------------------------------------------------------------------------


class RadioEffect:
    """Applies the full transmit and receive chain to a block of speech."""

    def __init__(self, sample_rate: int = 22050, profile: RadioProfile = PROFILE_CLEAN):
        self.sample_rate = sample_rate
        self.profile = profile

    def process(
        self,
        audio: np.ndarray,
        *,
        signal_quality: float = 1.0,
        pitch_semitones: float = 0.0,
        seed: int | None = None,
        profile: RadioProfile | None = None,
        tilt_db: float | None = None,
        flutter: float | None = None,
        keying: float = 0.0,
    ) -> np.ndarray:
        """Run the chain.

        ``signal_quality`` is 1.0 for a strong nearby station and falls toward
        0.0 with distance, raising the noise floor, softening the top end and
        eventually breaking the signal up. ``seed`` makes the noise
        reproducible. ``tilt_db``, ``flutter`` and ``keying`` let the caller
        vary the equipment, the path and the transmitter's switching per
        station.
        """
        p = profile or self.profile
        sr = self.sample_rate
        rng = np.random.default_rng(seed)

        voice = np.asarray(audio, dtype=np.float32).flatten()
        if voice.size == 0:
            return voice

        q = float(np.clip(signal_quality, 0.05, 1.0))

        # --- at the controller -------------------------------------------
        voice = pitch_shift(voice, pitch_semitones, sr)
        voice = highpass(voice, sr, 180.0)          # remove what a mic cannot pass
        voice = de_ess(voice, sr, p.de_ess_db)
        voice = peaking_eq(voice, sr, p.presence_hz, p.presence_db, 0.9)
        voice = _normalise(voice) * 0.9

        # The room the microphone stands in, mixed before the transmitter so
        # it is band-limited and compressed along with the speech.
        breath = _breath(sr, p.breath_db, rng)
        if breath.size:
            voice = np.concatenate([breath, np.zeros(int(sr * 0.06), np.float32),
                                    voice])
        voice = _key_held(voice, sr, p, rng)
        room = _room_tone(voice.shape[0], sr, p.room_rumble_hz, rng)
        source = voice + room * _db_to_lin(p.room_db)

        # --- in the transmitter -------------------------------------------
        high = p.band_high * (0.72 + 0.28 * q)      # a weak signal loses the top
        source = bandpass(source, sr, p.band_low, high, p.band_order)
        source = tilt_eq(source, sr, p.tilt_db if tilt_db is None else tilt_db)
        source = compress(source, sr, p.comp_threshold_db, p.comp_ratio,
                          p.comp_attack_ms, p.comp_release_ms)
        source *= _db_to_lin(p.makeup_db)
        source = soft_clip(source, p.drive, p.clip_softness)

        # --- over the air --------------------------------------------------
        depth = p.flutter if flutter is None else flutter
        if depth > 0.001:
            source = self._flutter(source, sr, depth, p.flutter_hz, rng)

        # --- in the receiver ------------------------------------------------
        # The detector comes before the audio stage, so its harmonics are
        # shaped by the speaker rather than the other way round.
        source = am_detector(source, p.detector_asymmetry)
        source = self._add_noise_bed(source, sr, p, q, rng)
        # The gain control sits after the detector and rides everything the
        # receiver produces, hiss included. Applying it to the voice alone
        # buys the first syllable a few dB at the expense of the noise floor,
        # which pushed the hiss under the 30 dB the audio tests hold it to --
        # the opposite of the point. Boosting both keeps the ratio and gives
        # the swell of noise that decays as the transmission settles.
        source = agc_settle(source, sr, p.agc_overshoot_db, p.agc_settle_ms)

        # --- in the cockpit --------------------------------------------------
        source = peaking_eq(source, sr, p.speaker_peak_hz, p.speaker_peak_db,
                            p.speaker_q)
        # A shallow dip above the peak stops it sounding like a telephone.
        source = peaking_eq(source, sr, p.speaker_peak_hz * 2.1, -2.5, 1.4)
        source = speaker_harmonic(source, p.speaker_harmonic)
        source = source + _mains_hum(source.shape[0], sr, p.hum_db, rng)

        # --- keying ----------------------------------------------------------
        source = self._add_squelch(source, sr, p, q, rng, keying)

        if q < 0.55:
            source = self._dropouts(source, sr, q, rng)

        peak = float(np.max(np.abs(source))) or 1.0
        # Not every transmission at the same level: different transmitters,
        # different distances, different people leaning into the microphone.
        # Identical peaks across a session is one of the clearest tells that
        # the audio was generated rather than received.
        jitter = _db_to_lin(float(rng.uniform(-p.level_jitter_db, 0.0)))
        return (source / peak * p.output_peak * jitter).astype(np.float32)

    # ------------------------------------------------------------------

    def _add_noise_bed(
        self, voice: np.ndarray, sr: int, p: RadioProfile, q: float,
        rng: np.random.Generator,
    ) -> np.ndarray:
        """Add hiss that breathes with the AGC.

        The receiver's gain control rides the signal, so the noise floor rises
        in the gaps between words and ducks under the speech. Reproducing that
        is what makes the result sound like a radio rather than like speech
        with a filter over it.
        """
        length = voice.shape[0]
        noise = _channel_noise(length, sr, rng)

        # Where is there speech? A slow envelope, normalised to 0-1.
        speech = envelope(voice, sr, 8.0, 140.0)
        peak = float(np.max(speech)) or 1.0
        speech = np.clip(speech / peak, 0.0, 1.0)
        # Smooth it further so the noise swells rather than flickering.
        speech = lowpass(speech, sr, 12.0, order=1)
        speech = np.clip(speech, 0.0, 1.0)

        # Base level, plus the breathing swing when speech is absent.
        base_db = p.noise_db + (1.0 - q) * 14.0
        gain = _db_to_lin(base_db) * (
            1.0 + (_db_to_lin(p.breathing_db) - 1.0) * (1.0 - speech)
        )
        return (voice + noise * gain).astype(np.float32)

    def _add_squelch(
        self, body: np.ndarray, sr: int, p: RadioProfile, q: float,
        rng: np.random.Generator, keying: float = 0.0,
    ) -> np.ndarray:
        """Key the transmitter up before the voice and down after it.

        A transmission does not begin with a word and does not end with one.
        See the keying block in :class:`RadioProfile` for what happens in
        between; this puts the two ends either side of the body.

        ``keying`` is the station's own radio, from -1 to 1: a stable offset
        that makes one facility's key-up brighter or duller, and its tail
        longer or shorter, than the next one's. The same idea as ``tilt_db``,
        applied to the part of the sound that is not the voice.
        """
        if p.squelch_open_ms <= 0.0 and p.squelch_tail_ms <= 0.0:
            return body                # nothing keys here: not a radio

        # The in-transmission floor, which is what the receiver settles to
        # once the carrier has captured it.
        floor_db = p.noise_db + (1.0 - q) * 14.0 + p.breathing_db
        keying = float(np.clip(keying, -1.0, 1.0))

        lead = self._key_up(sr, p, floor_db, keying, rng)
        tail = self._key_down(sr, p, floor_db, keying, rng)
        return np.concatenate([lead, body, tail])

    def _key_up(
        self, sr: int, p: RadioProfile, floor_db: float, keying: float,
        rng: np.random.Generator,
    ) -> np.ndarray:
        """Carrier on: a click, a thump, and the hiss dropping to the floor."""
        n = int(sr * max(0.0, p.squelch_open_ms) / 1000.0)
        if n <= 0:
            return np.zeros(0, dtype=np.float32)

        # Open-squelch hiss falling to the in-transmission floor as the
        # carrier captures the receiver. Exponential, because quieting is.
        quiet_n = max(1, int(sr * p.quieting_ms / 1000.0))
        curve = np.zeros(n, dtype=np.float32)
        fall = np.exp(-np.linspace(0.0, 4.0, min(quiet_n, n), dtype=np.float32))
        curve[: fall.shape[0]] = fall
        gain_db = floor_db + p.open_noise_db * curve
        out = _channel_noise(n, sr, rng) * (10.0 ** (gain_db / 20.0))

        # The gate itself opens in about a millisecond, not instantly: a step
        # into a band-limited channel is a click of its own, and there is
        # already one of those.
        gate = min(n, max(2, int(sr * 0.0012)))
        out[:gate] *= np.linspace(0.0, 1.0, gate, dtype=np.float32)

        click_db = p.ptt_click_db + keying * 3.0
        click = _carrier_click(sr, p, click_db, rng)
        end = min(n, click.shape[0])
        out[:end] += click[:end]

        # Some radios tick twice: the relay closing, then the carrier detect
        # a few milliseconds behind it.
        if rng.random() < p.relay_chance:
            offset = int(sr * rng.uniform(0.010, 0.026))
            second = _ptt_click(sr, click_db - 7.0, rng, length_ms=4.0)
            end = min(n, offset + second.shape[0])
            if end > offset:
                out[offset:end] += second[: end - offset]
        return out.astype(np.float32)

    def _key_down(
        self, sr: int, p: RadioProfile, floor_db: float, keying: float,
        rng: np.random.Generator,
    ) -> np.ndarray:
        """Carrier off: the AGC's parting blast, then the gate shutting."""
        n = int(sr * max(0.0, p.squelch_tail_ms) / 1000.0)
        if n <= 0:
            return np.zeros(0, dtype=np.float32)

        burst_n = min(n, max(1, int(sr * p.tail_burst_ms
                                    * (1.0 + keying * 0.3) / 1000.0)))
        close_n = min(n - burst_n, max(1, int(sr * p.gate_close_ms / 1000.0)))
        muted_n = max(0, n - burst_n - close_n)

        # Loud while the gain control is still wound up for a carrier that has
        # gone, then shut, then the floor a muted receiver actually sits on.
        levels = np.concatenate([
            np.linspace(floor_db + p.tail_burst_db, floor_db + 1.0,
                        burst_n, dtype=np.float32),
            np.linspace(floor_db + 1.0, p.muted_db, close_n, dtype=np.float32),
            np.full(muted_n, p.muted_db, dtype=np.float32),
        ])[:n]
        if levels.shape[0] < n:
            levels = np.pad(levels, (0, n - levels.shape[0]),
                            constant_values=p.muted_db)
        out = _channel_noise(n, sr, rng) * (10.0 ** (levels / 20.0))

        click = _carrier_click(sr, p, p.ptt_click_db + keying * 3.0, rng,
                               down=True)
        end = min(n, click.shape[0])
        out[:end] += click[:end]
        return out.astype(np.float32)

    @staticmethod
    def _flutter(
        audio: np.ndarray, sr: int, depth: float, rate_hz: float,
        rng: np.random.Generator,
    ) -> np.ndarray:
        """Slow amplitude wobble from a moving, multipath airborne path."""
        t = np.arange(audio.shape[0], dtype=np.float32) / sr
        phase = rng.random() * 6.28
        slow = np.sin(2 * math.pi * rate_hz * t + phase)
        slower = np.sin(2 * math.pi * (rate_hz * 0.37) * t + phase * 1.7)
        modulation = 1.0 + depth * (0.6 * slow + 0.4 * slower)
        return (audio * modulation).astype(np.float32)

    def _dropouts(
        self, x: np.ndarray, sr: int, q: float, rng: np.random.Generator
    ) -> np.ndarray:
        """Brief squelch-closure gaps on a marginal signal."""
        count = int((0.55 - q) * 10)
        for _ in range(count):
            if rng.random() > 0.45:
                continue
            n = int(sr * rng.uniform(0.02, 0.07))
            if n >= x.shape[0]:
                continue
            start = int(rng.integers(0, x.shape[0] - n))
            ramp = np.linspace(1.0, 0.08, n // 4, dtype=np.float32)
            x[start:start + ramp.shape[0]] *= ramp
            x[start + ramp.shape[0]:start + n] *= 0.08
        return x


# --------------------------------------------------------------------------
# per-station variation
# --------------------------------------------------------------------------


def station_character(facility: str, position: str, ident: str = "") -> tuple[float, float]:
    """A stable equipment tilt and flutter depth for one station.

    Two facilities should not sound like the same radio. Deriving the variation
    from a hash keeps it consistent: Kennedy Tower's transmitter sounds the
    same on every flight, just as its voice does.
    """
    import hashlib

    digest = hashlib.sha256(
        f"{ident}|{facility}|{position}".encode("utf-8")
    ).digest()
    tilt = ((digest[0] % 9) - 4) * 0.6          # -2.4 to +2.4 dB
    flutter = 0.0
    if position in ("CTR", "APP", "DEP"):
        flutter = (digest[1] % 5) * 0.015       # 0 to 0.06
    return round(tilt, 2), round(flutter, 3)


def station_keying(facility: str, position: str, ident: str = "") -> float:
    """How this station's transmitter switches, from -1 to 1.

    The same idea as :func:`station_character` and drawn from the same hash,
    for the part of the sound that is not the voice. A tower two miles away
    with a modern solid-state transmitter keys up almost politely; an old
    centre remote site announces itself. Once the ear has learned that
    Kennedy Ground clicks *like that*, hearing it is recognising somebody --
    which is the whole reason to make the keying per-station rather than
    per-transmission.
    """
    import hashlib

    digest = hashlib.sha256(
        f"{ident}|{facility}|{position}|keying".encode("utf-8")
    ).digest()
    return round(((digest[0] % 11) - 5) / 5.0, 2)


def signal_quality_for(distance_nm: float, position: str, altitude_ft: float = 0.0) -> float:
    """How strong a station should sound, given how far away it is.

    Ground positions stay strong because you are on their airport. Approach and
    centre fade with range, but altitude restores them, because VHF is
    line-of-sight and height is what buys range.
    """
    from ..navdata.db import STATION_RANGE_NM

    reach = STATION_RANGE_NM.get(position, 60.0)
    if position in ("DEL", "GND", "TWR"):
        return float(np.clip(1.0 - 0.25 * (distance_nm / max(1.0, reach)), 0.7, 1.0))

    los = 1.23 * math.sqrt(max(0.0, altitude_ft)) if altitude_ft > 0 else 0.0
    effective = max(reach, min(los, reach * 4))
    ratio = distance_nm / max(1.0, effective)
    return float(np.clip(1.05 - 0.75 * ratio, 0.25, 1.0))


__all__ = [
    "RadioEffect", "RadioProfile",
    "PROFILE_CLEAN", "PROFILE_ATIS", "PROFILE_SIDETONE", "PROFILE_DISTANT",
    "BAND_LOW_HZ", "BAND_HIGH_HZ",
    "bandpass", "highpass", "lowpass", "peaking_eq", "tilt_eq",
    "compress", "soft_clip", "de_ess", "envelope",
    "signal_quality_for", "station_character",
]
