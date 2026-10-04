"""What a radio shows, and what it is actually tuned to.

Europe moved the airband to 8.33 kHz channel spacing, and the number on the
radio stopped being a frequency. It is a *channel name*: a label from a fixed
table, chosen so that every channel has five digits and no two look alike.
Three of every four channel names are within 5 kHz of the carrier they
describe, and one of them is not a carrier at all.

The part that breaks a naive lookup is that a 25 kHz frequency has two names.
Paris Ground on 121.600 appears on an 8.33-capable radio as **121.605**, and
both refer to the same carrier. A pilot reading the number off the panel and a
database built from published 25 kHz frequencies will therefore disagree about
every such frequency in Europe -- which is exactly what happens at Charles de
Gaulle, and at several thousand other fields.

The rule is short. Within each 25 kHz block the channel names run
``.x00 .x05 .x10 .x15``, and the first two share the block's carrier:

    channel   .x05 -> carrier .x00      channel  .x30 -> carrier .x25
    channel   .x55 -> carrier .x50      channel  .x80 -> carrier .x75

Every other name is a genuine 8.33 kHz channel of its own and must not be
folded into anything. 118.010 and 118.015 are real, separate frequencies, and
answering one on the other would be worse than not answering at all.
"""

from __future__ import annotations

# The channel names that are a second label for a 25 kHz carrier. Keyed by the
# last two digits of the channel in kHz; the value is what to subtract.
_SECOND_NAME = frozenset({5, 30, 55, 80})

# Where the airband starts and stops. Anything outside it is not a COM channel
# and is left alone rather than being "corrected" into one.
AIRBAND_LOW_KHZ = 118_000
AIRBAND_HIGH_KHZ = 136_990


def to_khz(mhz: float) -> int:
    """Megahertz as a whole number of kilohertz.

    Frequencies arrive as floats from three different places -- a CSV, a
    simulator variable, and a person typing -- and 121.605 is not exactly
    representable in any of them. Rounding to kilohertz once, here, is what
    stops the comparison being a game of chance.
    """
    return int(round(float(mhz) * 1000.0))


def carrier_khz(mhz: float) -> int:
    """The carrier a tuned channel actually sits on, in kilohertz.

    ``121.605`` and ``121.600`` both come back as 121600, because they are the
    same transmitter. ``118.010`` comes back as 118010, because it is not.
    """
    khz = snap_khz(mhz)
    if not AIRBAND_LOW_KHZ <= khz <= AIRBAND_HIGH_KHZ:
        return khz
    return khz - 5 if khz % 100 in _SECOND_NAME else khz


def carrier(mhz: float) -> float:
    """The same thing in megahertz, for display."""
    return carrier_khz(mhz) / 1000.0


def same_channel(a: float, b: float, tolerance_khz: int = 4) -> bool:
    """Whether two numbers name the same thing on the air.

    True when they share a carrier -- which is what makes 121.605 find
    121.600 -- and also when they are within a few kilohertz of each other,
    which forgives a published figure that was rounded to two decimals.
    """
    if carrier_khz(a) == carrier_khz(b):
        return True
    return abs(to_khz(a) - to_khz(b)) <= tolerance_khz


def is_eight_33(mhz: float) -> bool:
    """Whether this is a channel only an 8.33 kHz radio can select."""
    khz = to_khz(mhz)
    if not AIRBAND_LOW_KHZ <= khz <= AIRBAND_HIGH_KHZ:
        return False
    return khz % 25 != 0


def channel_name(mhz: float) -> float:
    """The 8.33 channel name a modern radio shows for a carrier.

    The inverse of :func:`carrier`, for the three-quarters of carriers that
    have a second name. A channel that is already a name comes back unchanged.
    """
    khz = to_khz(mhz)
    if not AIRBAND_LOW_KHZ <= khz <= AIRBAND_HIGH_KHZ:
        return khz / 1000.0
    return (khz + 5) / 1000.0 if khz % 25 == 0 else khz / 1000.0


# The sixteen names each 100 kHz block has, as offsets in kHz.
_NAMES = (0, 5, 10, 15, 25, 30, 35, 40, 50, 55, 60, 65, 75, 80, 85, 90)


def snap_khz(mhz: float) -> int:
    """The selectable channel a published figure was meant to be.

    Community data carries a systematic slip: a frequency written to two
    decimals loses its last digit, so 123.075 is recorded as 123.07 and 128.225
    as 128.22. Neither is a channel any radio can select, so the row can never
    match anything and is dead weight.

    Snapping to the nearest name recovers both, and a tie is broken upwards
    because truncation only ever loses value -- .x70 came from .x75, not from
    .x65.
    """
    khz = to_khz(mhz)
    if not AIRBAND_LOW_KHZ <= khz <= AIRBAND_HIGH_KHZ:
        return khz
    block, offset = divmod(khz, 100)
    if offset in _NAMES:
        return khz
    best = min(_NAMES + (100,), key=lambda name: (abs(name - offset), -name))
    return block * 100 + best


def snap(mhz: float) -> float:
    """The same, in megahertz."""
    return snap_khz(mhz) / 1000.0


def is_valid_channel(mhz: float) -> bool:
    """Whether this is a channel a radio could actually be tuned to.

    Inside the airband, and landing on one of the sixteen names each 100 kHz
    block has. A published figure that fails this is a data error.
    """
    khz = to_khz(mhz)
    if not AIRBAND_LOW_KHZ <= khz <= AIRBAND_HIGH_KHZ:
        return False
    return khz % 100 in _NAMES


__all__ = [
    "to_khz", "carrier_khz", "carrier", "same_channel", "is_eight_33",
    "snap", "snap_khz",
    "channel_name", "is_valid_channel", "AIRBAND_LOW_KHZ", "AIRBAND_HIGH_KHZ",
]
