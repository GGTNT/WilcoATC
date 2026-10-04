# -*- coding: utf-8 -*-
"""The first officer's half of the radio work.

Two separate things, and it matters that they are separate.

*Confirming* is what the pilot monitoring does on the interphone: the
controller says a number, and the other seat says it back to you so that two
people have heard it. It is not on the air, it changes nothing, and it is on
by default because it is the single most useful thing a first officer does.

*Reading back* is the real thing, on the frequency, and it is what the
controller is waiting for. Having the aeroplane do it for you is off by
default: a pilot practising radio work does not want the readback made before
they have drawn breath. Where it is switched on, the first officer waits --
long enough that a pilot who was going to answer has answered -- and only then
speaks.

Both are built from the instruction the controller actually issued, which is
already held on the session with its values. Nothing here re-parses anything
or invents a number: a readback that says something the controller did not is
worse than no readback at all.
"""

from __future__ import annotations

import logging

from .phraseology import Aircraft
from .pilot import PilotSpeech
from .session import Instruction

log = logging.getLogger(__name__)


# How long the first officer gives you before reading a clearance back. Long
# enough to be sure you were not simply slower than the machine -- a pilot has
# to find the button, think, and speak, and the whole point of this being off
# by default is that the aeroplane must never take the transmission out of
# your mouth.
READBACK_WAIT_S = 12.0


def confirmation(instruction: Instruction, language: str = "en") -> str:
    """What the other seat says to you about an instruction, on the interphone.

    Short, and only the numbers. "Two four zero on the heading" is what a
    first officer says; the full readback belongs on the radio and would be
    an odd thing to say to the person sitting next to you.
    """
    values = instruction.values or {}
    book = _CONFIRMATIONS.get(language, _CONFIRMATIONS["en"])
    parts: list[str] = []

    if "altitude_ft" in values:
        parts.append(book["altitude"].format(
            value=_altitude_words(values["altitude_ft"], language,
                                  _transition_for(instruction))))
    if "heading" in values:
        parts.append(book["heading"].format(
            value=_digits(int(values["heading"]), 3)))
    if "speed_kt" in values:
        parts.append(book["speed"].format(value=int(values["speed_kt"])))
    if "squawk" in values:
        parts.append(book["squawk"].format(
            value=_digits(str(values["squawk"]))))
    if "frequency" in values:
        parts.append(book["frequency"].format(
            value=_frequency_words(values["frequency"])))
    if "runway" in values:
        parts.append(book["runway"].format(
            value=_digits(str(values["runway"]))))
    if "hold_short" in values and values["hold_short"]:
        target = values["hold_short"]
        parts.append(book["hold_short"].format(
            value=_digits(str(target)) if not isinstance(target, bool) else ""))

    if not parts:
        return ""
    return ", ".join(p for p in parts if p).strip() + "."


def readback(instruction: Instruction, aircraft: Aircraft,
             phraseology, language: str = "en",
             station_name: str = "") -> str:
    """The readback itself, in the words the controller would expect.

    Built through :class:`~wilcoatc.atc.pilot.PilotSpeech`, which is the same
    thing the other aeroplanes on the frequency read back with, so the numbers
    come out identically to the way they went in. A readback whose digits are
    rendered differently from the clearance is not a readback the controller
    can check.
    """
    speech = PilotSpeech(language, phraseology)
    values = instruction.values or {}
    kind = instruction.kind

    try:
        if kind == "handoff" and "frequency" in values:
            return speech.contact(aircraft,
                                  station_name or values.get("station", ""),
                                  float(values["frequency"]))
        if kind == "taxi":
            return speech.taxi(aircraft, str(values.get("runway", "")),
                               str(values.get("route", "")),
                               str(values.get("hold_short", "")))
        if kind == "line_up_and_wait":
            return speech.line_up(aircraft, str(values.get("runway", "")))
        if kind == "takeoff_clearance":
            return speech.takeoff(aircraft, str(values.get("runway", "")))
        if kind == "landing_clearance":
            return speech.land(aircraft, str(values.get("runway", "")))
        if kind == "go_around":
            return speech.going_around(
                aircraft, float(values.get("altitude_ft", 0) or 0))
        if kind == "approach_clearance":
            # Not the hold-short frame the runway-only fallback gives, which
            # read "hold short runway two two" back to an approach clearance
            # and set off a loop of corrections.
            return speech.approach(
                aircraft, _approach_named(instruction.text),
                str(values.get("runway", "")),
                float(values.get("altitude_ft", 0) or 0),
                float(values.get("altimeter", 0) or 0))
        if kind == "ifr_clearance":
            return speech.clearance(
                aircraft,
                str(values.get("destination", "")),
                str(values.get("squawk", "")),
                float(values.get("altitude_ft", 0) or 0),
            )
        if kind in ("altitude", "vfr_altitude") and "altitude_ft" in values:
            return speech.level(aircraft, float(values["altitude_ft"]),
                                climbing=_climbing(instruction))
        if "heading" in values:
            return speech.heading(aircraft, float(values["heading"]))
        if "speed_kt" in values:
            return speech.speed(aircraft, float(values["speed_kt"]))
        if "squawk" in values:
            return speech.squawk(aircraft, str(values["squawk"]))
        if "runway" in values:
            # With no route, the taxi frame is the hold-short one, which is
            # the right readback for any instruction whose only number is a
            # runway.
            return speech.taxi(aircraft, str(values["runway"]))
    except Exception:
        log.debug("could not build a readback for %s", kind, exc_info=True)
        return ""

    # Nothing with a number in it. An instruction that has none is one a
    # "wilco" answers, and that is what a first officer would say.
    return speech.wilco(aircraft)


# The verb of a descent, in each language a controller here speaks.
_DESCENT_WORDS = ("descend", "descendez", "descienda", "sinken", "scenda",
                  "desça", "desca")


def _climbing(instruction: Instruction) -> bool:
    """Whether a level instruction is a climb.

    Said by the instruction where it says (``values["climbing"]``), else read
    off its verb. Defaulting to a climb read every descent back as one.
    """
    values = instruction.values or {}
    if "climbing" in values:
        return bool(values["climbing"])
    text = (instruction.text or "").lower()
    return not any(word in text for word in _DESCENT_WORDS)


def _approach_named(text: str) -> str:
    """The approach a clearance named, as it was said; ILS if none."""
    import re

    found = re.search(r"\b(ILS|RNAV|RNP|GPS|VOR|NDB|LOC|localizer|visual|"
                      r"à vue)\b", text or "", re.IGNORECASE)
    return found.group(1) if found else "ILS"


# --------------------------------------------------------------------------
# the interphone wording
# --------------------------------------------------------------------------

_CONFIRMATIONS: dict[str, dict[str, str]] = {
    "en": {
        "altitude": "{value} set",
        "heading": "heading {value}",
        "speed": "{value} knots",
        "squawk": "squawk {value}",
        "frequency": "{value} in the standby",
        "runway": "runway {value}",
        "hold_short": "holding short {value}",
    },
    "fr": {
        "altitude": "{value} affiché",
        "heading": "cap {value}",
        "speed": "{value} nœuds",
        "squawk": "transpondeur {value}",
        "frequency": "{value} en veille",
        "runway": "piste {value}",
        "hold_short": "on maintient avant {value}",
    },
}


def _digits(value, width: int = 0) -> str:
    """A number said one digit at a time, which is how a cockpit says one."""
    text = str(value)
    if width and text.isdigit():
        text = text.rjust(width, "0")
    return " ".join(text)


def _transition_for(instruction: Instruction) -> int:
    """Where the controller who said this turns feet into flight levels.

    Read off the station the instruction came from, which carries the ident
    in front of the position. Nothing else has to be plumbed through: the
    first officer confirms an instruction, and the instruction already knows
    who issued it.
    """
    from ..navdata.db import transition_altitude

    key = getattr(instruction, "station_key", "") or ""
    return transition_altitude(key.split(":", 1)[0])


def _altitude_words(feet, language: str = "en",
                    transition: int = 18_000) -> str:
    """An altitude as the flight deck says it back to itself.

    ``transition`` is where the region turns feet into flight levels. It used
    to be eighteen thousand and nothing else, which is the American figure --
    so over London the controller issued a flight level and the first officer
    confirmed it in thousands of feet, which is the one thing the confirmation
    exists to stop.
    """
    try:
        value = int(round(float(feet) / 100.0) * 100)
    except (TypeError, ValueError):
        return str(feet)
    if value >= (transition or 18_000):
        # Digit by digit, which is how a level is said and is also the only
        # reading a synthesiser gives back: handed "flight level 180" it says
        # "one hundred eighty", which is not a level anybody has ever read
        # back. The heading beside this has always been spelled out for
        # exactly the same reason.
        level = _digits(int(value / 100), 3)
        return f"niveau {level}" if language == "fr" else f"flight level {level}"
    thousands, hundreds = divmod(value, 1000)
    if language == "fr":
        words = f"{thousands} mille" if thousands else ""
        if hundreds:
            words = f"{words} {hundreds}".strip()
        return f"{words or 'zéro'} pieds"
    words = f"{thousands} thousand" if thousands else ""
    if hundreds:
        words = f"{words} {hundreds}".strip()
    return f"{words or 'zero'} feet"


def _frequency_words(mhz) -> str:
    """A frequency, digit by digit, the way it is set on a radio."""
    try:
        text = f"{float(mhz):.3f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(mhz)
    whole, _, fraction = text.partition(".")
    spoken = " ".join(whole)
    if fraction:
        spoken = f"{spoken} decimal {' '.join(fraction)}"
    return spoken


__all__ = ["confirmation", "readback", "READBACK_WAIT_S"]
