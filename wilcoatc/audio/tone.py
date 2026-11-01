"""The balance of the receiver, measured against a Heathrow recording.

The chain in :mod:`wilcoatc.audio.radio_fx` was measured for what a radio
*does* -- the AGC breathing, the keying, the noise floor -- and it is not to be
edited. Its tonal balance, though, is a handful of numbers it already takes
from the profile, and those decide whether the controller sounds like an
air-band receiver or like a telephone. The profile shipped with the chain
leans well toward the receiver.

Measured against an hour of Heathrow Director, it leans too far. Real
controller audio puts 94% of its energy between 250 Hz and 1 kHz, with a
spectral centroid at 620 Hz. The chain's own profile puts 67% there and the
centroid at 890, which is the difference between a voice in a headset and a
voice through a hole in a wall -- audible on every transmission.

`tilt_db` and `presence_db` were the first answer to that, and they got the
centroid right while leaving the *shape* wrong. Re-measured against two
recordings of one Heathrow Director exchange -- the same script read by a
woman and by a man, so that what both have in common is the channel and not
the speaker -- the two curves agree closely: a broad peak at 630 Hz, within
3 dB of it from 400 to 800, then a steady fall to -20 dB at 2.5 kHz and a
cliff past 3 kHz. Read in third-octave bands, the chain sat about 10 dB over
that at 315 Hz, 5 dB under it at 800, and 4 to 5 dB over it above 3 kHz: too
much bass, the peak in the wrong place, and a top end the recordings do not
have. A tilt cannot fix that, because a tilt pivots about a peak and does not
move one.

Three changes do, and none of them is a new effect:

* the cockpit speaker's resonance down from 1650 Hz to 900, which puts the
  peak where the recordings put it, and brings the chain's own notch an
  octave above it down from 3.5 kHz to 1.9 kHz, where it takes out most of
  what was left above the peak;
* its height up from 2 dB to 4, because at 900 Hz it is doing the work the
  tilt was being asked to do;
* the passband narrowed from 300-3400 Hz to 330-2700, which is the Annex 10
  channel the recordings actually are.

Together those take the third-octave distance from the recordings from
6.3 dB to 3.8 dB. For scale: the two recordings differ from *each other* by
2.4 dB, and one recording differs from its own second half by 1.1 dB. So what
is left is close to the width of the target, and most of it is now in the
bands where the man and the woman disagree -- their own fundamentals -- rather
than in the bands where they agree.

None of it costs words, which was not the expectation. Every candidate was
put back through the recogniser the program ships, over 26 transmissions,
with the numbers spelled the same way on both sides. Balance and floor
together take the British Kokoro voices from 15.6% to 13.6%, and Supertonic
-- which casts nearly every other controller and is not what any of this was
measured on -- from 14.5% to 14.2% across a tower and a ground. The band was
narrowed to where the recordings still have energy, so nothing was taken away
that the words were using. A twelve-line bench said otherwise and was too
small to say anything; the full corpus is the number.

Going further does cost words, and that is what settled where to stop. A band
order of 8 and a 2500 Hz top each bought about two tenths of a decibel and
lost words in every bench they were tried in, and using ``presence_db`` as a
low cut made the match *worse* rather than better. None of those is in here.

For what "too dark to read" would actually look like: the recordings
themselves, through this same recogniser and scorer, lose 37% of their words
(the male clip, whose script is known independently) and 21% (the female,
whose transcript came off this recogniser and so is flattered). The program
loses 17% and 12%. A real frequency sits well below this program on the
measure that guards it, which is worth knowing before anyone spends the
difference.

Everything goes through :func:`wilcoatc.audio.imperfections.tune`, which
already returns a copy of the profile with the operator's numbers in it. A
profile that is not a controller's radio -- the intercom, the cabin address --
is left alone: those are not heard through an air-band receiver and were not
measured against one. ``enabled: false`` gives the chain's own balance back
exactly.

**Then it was re-fitted, to a different target.** Everything above is kept
as :data:`HEATHROW`. The default is now matched to the controllers in a
reference flight video (MSFS 2024, an A350 from Melbourne to Toulouse), cut
out of the soundtrack where nothing else was playing: four clean
transmissions for the shape, the floor and the keying, two more under engine
noise for the pace. Those controllers are brighter than Heathrow Director --
-6 to -8 dB held from 1 to 2.5 kHz where Director falls to -20, a centroid
between 800 and 1075 Hz against 620 -- with a steep cut under 400 Hz, a
cliff past 2.5 kHz, a floor 20 dB under the voice, no key-up click to speak
of and the floor running on for about 0.3 s after the last word.

A coordinate search over these fields, scored against the clean cuts, put
the speaker resonance at 1.9 kHz and 6 dB, the bottom of the band at 400 Hz
and the floor offset at nothing. That takes the third-octave distance from
6.8 dB to 4.7 -- under the 5 to 6.5 dB the four reference transmissions
differ from *each other* by, which is as close as six words a clip can
measure -- and the floor from -27.5 to -21.8 against -20.6. The search's own
optimum had the bottom at 500 Hz and the floor 3 dB louder; that cost 1.6
points of word error over 156 transmissions (26 lines, six casts) and 430 Hz
still cost 0.6, where 400 costs 0.1, so 400 is where it stops. The keying
fields cost nothing measurable (12.1% against 11.9%).

The pace was measured too and left alone: the video articulates about 5.3
syllables a second against 4.5 here, and the two cheap ways to close it --
flattening ``measured``, or 0.88/0.96 for ``quick``/``measured`` -- moved the
measured rate by three per cent for 0.4 and 3.6 points of word error, almost
all of the latter on Kokoro. That is the same answer the Heathrow recordings
gave; see the README. Supertonic alone was tried as well, through its own
base speed: it already reads at 4.8 to 5.7 syllables a second against the
video's 5.4 (both through the shipped recogniser), and 15% quicker costs its
casts 2 to 7 points. The gap that is left is Kokoro's, and Kokoro is the
engine that falls apart first.

Two other things were measured and are not here. The +5 to +7 dB still
showing at 315-400 Hz can be taken down with the transmitter's presence
filter turned into a dip at 330 Hz, and that buys 0.18 dB of distance over
the whole corpus -- far inside the 5 dB the reference clips disagree by --
while pushing 3-4 kHz past the reference. And the remaining difference that
anyone will hear is not the channel at all but the voice: the video's
controllers come off a far larger synthesiser than anything that runs on a
CPU beside the simulator. The cloning tier is what closes that.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace

#: The profiles this applies to are the ones with a squelch, because a squelch
#: is what says "heard through a receiver". The intercom and the cabin address
#: have none and keep the balance they were written with.
_RECEIVED = "squelch_open_ms"


@dataclass(frozen=True)
class Tone:
    """Where the receiver puts its energy.

    Six of the chain's own profile fields, restated here so that they can be
    matched to a recording without the chain being edited. The defaults are
    the measured ones; see the module docstring for the measurement, and for
    the candidates that matched better and cost words.
    """

    #: The switch. Off means the profile is handed back untouched.
    enabled: bool = True
    #: Across the passband, in dB. Negative is darker. The chain's own
    #: ``tilt_db`` is per station and small; this is added to it, so the
    #: stations still differ from one another by the same amount.
    #:
    #: Re-swept after the resonance moved, on the theory that -6 was chosen
    #: against a chain whose peak was somewhere else. It was not worth
    #: moving. -7.5 and -9 take the third-octave distance from 3.56 to 3.48
    #: and 3.42 and then it flattens -- 0.14 dB, an eighth of what this
    #: metric's own halves disagree by -- and they buy it by pushing 2.5 to
    #: 4 kHz from a +2 dB excess into a -1 dB deficit, which is trading one
    #: error for another. The centroid moves the wrong way while they do it,
    #: 570 Hz to 561 against the recordings' 635, and the recogniser shows no
    #: trend either way. Note what the tilt does *not* reach: the +5/+9 dB at
    #: 315 Hz is identical at -5 and at -11.
    tilt_db: float = -6.0
    #: The transmitter's pre-emphasis at 2 kHz, replacing the profile's own.
    presence_db: float = 0.0
    #: The cockpit speaker's resonance, replacing the profile's own -- how
    #: much, and where. Moving it down to 900 Hz is the single change that
    #: fixed the shape rather than the slope: the recordings peak at 630 Hz
    #: and the chain peaked below 500, and no amount of ``tilt_db`` can move
    #: a peak, because a tilt pivots about one and does not relocate it. Note
    #: what comes with it: the chain hangs a fixed -2.5 dB notch an octave
    #: above the resonance, which at 1650 Hz sat at 3.5 kHz where nothing
    #: could hear it and at 900 Hz sits at 1.9 kHz, where it takes out most
    #: of the remaining excess above the peak. Both halves were measured.
    speaker_db: float = 6.0
    speaker_hz: float = 1900.0
    #: The passband. The chain band-limits to 300-3400 Hz, the usual design
    #: figure; the recordings are an ICAO Annex 10 channel and have nothing
    #: above 3.4 kHz at all -- 0.0% of their energy, already 26 dB down at
    #: 3.15 kHz. Narrowed rather than replaced, so a profile that is already
    #: duller than this stays duller: the receiver cannot pass what it cannot
    #: pass, and no station is heard outside it.
    #:
    #: These are as far as the band can usefully be closed. Lifting the
    #: bottom past 330 Hz or steepening the skirts past the chain's order of
    #: 6 both go on improving the match and start taking the second harmonic
    #: off a male controller: order 8 cost 4 points of word error for two
    #: tenths of a decibel, and 2500 Hz at the top cost nearly 2 points for
    #: the same. Neither is in here for that reason.
    band_low: float = 400.0
    band_high: float = 2700.0
    #: How far down the floor between the words sits, in dB, *added* to the
    #: profile's own the way ``tilt_db`` is. Added rather than replacing,
    #: because the floor is most of what tells one station from another:
    #: ``PROFILE_DISTANT`` is a weak station and is written noisy on purpose,
    #: and a setting that replaced its figure would make a station two
    #: hundred miles away sound like one across the field.
    #:
    #: Measured inside a transmission -- the quietest frames while the
    #: carrier is up, against the speech -- the recordings sit at -25 dB and
    #: the chain sat at -14, which is louder than the recordings and louder
    #: than ``radio_fx``'s own docstring aims for ("18-25 dB under the
    #: voice"). No one source owns that: the receiver's hiss, the ops room
    #: and the imperfections layer's weather bed are each worth one to four
    #: decibels of it and the makeup gain lifts all three together. So both
    #: come down, and every effect stays switched on.
    #:
    #: -8 rather than more, and the reason is not caution. Read the floor at
    #: the quietest 2% of frames -- the ones that are genuinely nothing --
    #: and this lands at -30.1 against the recordings' -30.8, which is the
    #: match. Taking it to -12 or -16 moves that past the recordings and does
    #: not move the 15% figure at all, because the 15% figure is saturated by
    #: something this layer cannot reach: the recordings spend 23.6% of a
    #: transmission silent and this program spends 16.5%, so that percentile
    #: sits in a gap for one and on the decay of a word for the other. The
    #: remaining four decibels are the speaking rate, not the hiss.
    noise_db: float = 0.0
    room_db: float = -10.0
    #: The two ends of a transmission, as offsets on the profile's own
    #: figures, added like ``noise_db`` so a station keeps its own keying.
    #: ``click_db`` is the transmit relay and the carrier detect, ``rush_db``
    #: the open-squelch hiss before the carrier captures the receiver,
    #: ``tail_burst_db`` the AGC's parting blast, and ``tail_ms`` how much
    #: longer the carrier's hiss runs on after the last word before the
    #: squelch shuts.
    #:
    #: Measured against the reference video: its key-up peaks 15 to 20 dB
    #: under the voice where the chain's was 8 to 12, the hiss before the
    #: first word sits at the floor rather than 7 to 16 dB over it, and
    #: after the last word the floor simply runs on for about 0.3 s, with
    #: no burst, before the squelch shuts. None of the four cost a word.
    click_db: float = -7.0
    rush_db: float = -5.0
    tail_burst_db: float = -11.0
    tail_ms: float = 0.0


#: The settings in force when nobody says otherwise.
DEFAULT = Tone()

#: The first fit, against Heathrow Director, before the reference video.
#: Darker -- the speaker peak at 900 Hz -- with a quieter floor and the
#: chain's own keying. Kept so the two can be rendered side by side.
HEATHROW = Tone(speaker_db=4.0, speaker_hz=900.0, band_low=330.0,
                band_high=2700.0, noise_db=-8.0, room_db=-10.0,
                click_db=0.0, rush_db=0.0, tail_burst_db=0.0, tail_ms=0.0)

#: The chain's own balance, for the comparison.
OFF = Tone(enabled=False)


def from_config(section) -> Tone:
    """The settings, read off ``config.delivery.tone``.

    By field name and tolerant of a section that is missing one, for the same
    reason the other two delivery layers do it that way: the two dataclasses
    are extended one at a time.
    """
    if section is None:
        return DEFAULT
    values = {field.name: getattr(section, field.name)
              for field in fields(Tone) if hasattr(section, field.name)}
    return Tone(**values)


def apply(profile, settings: Tone = DEFAULT):
    """A copy of the profile with the receiver's balance in it.

    Returns the profile itself when nothing changes, so the cache key that
    names the profile goes on naming it.
    """
    if not settings.enabled or profile is None:
        return profile
    if getattr(profile, _RECEIVED, 0.0) <= 0.0:
        return profile
    changes = {}
    if abs(settings.tilt_db) > 0.0:
        changes["tilt_db"] = float(getattr(profile, "tilt_db", 0.0)
                                   + settings.tilt_db)
    if getattr(profile, "presence_db", None) != settings.presence_db:
        changes["presence_db"] = float(settings.presence_db)
    if getattr(profile, "speaker_peak_db", None) != settings.speaker_db:
        changes["speaker_peak_db"] = float(settings.speaker_db)
    if getattr(profile, "speaker_peak_hz", None) != settings.speaker_hz:
        changes["speaker_peak_hz"] = float(settings.speaker_hz)
    for field_name, offset in (("noise_db", settings.noise_db),
                               ("room_db", settings.room_db),
                               ("ptt_click_db", settings.click_db),
                               ("key_thump_db", settings.click_db),
                               ("open_noise_db", settings.rush_db),
                               ("tail_burst_db", settings.tail_burst_db)):
        if offset:
            changes[field_name] = float(getattr(profile, field_name, 0.0)
                                        + offset)
    if settings.tail_ms:
        # The run-on is the burst segment held at the floor; the squelch
        # tail is lengthened by the same amount so the gate still closes
        # inside it.
        for field_name in ("tail_burst_ms", "squelch_tail_ms"):
            changes[field_name] = max(
                0.0, float(getattr(profile, field_name, 0.0)) + settings.tail_ms)
    # Narrowed, never widened: a profile already duller than the receiver
    # stays exactly as dull as it was written.
    low = max(float(getattr(profile, "band_low", 0.0)), float(settings.band_low))
    high = min(float(getattr(profile, "band_high", 1e9)), float(settings.band_high))
    if low != getattr(profile, "band_low", None):
        changes["band_low"] = low
    if high != getattr(profile, "band_high", None):
        changes["band_high"] = high
    if not changes:
        return profile
    return replace(profile, **changes)


__all__ = ["DEFAULT", "HEATHROW", "OFF", "Tone", "apply", "from_config"]
