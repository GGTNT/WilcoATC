# -*- coding: utf-8 -*-
"""An instruction said again, in the language it is being said again in.

A controller repeating itself repeats the instruction, and a pilot who asked
for it in French wants it in French. What was stored was the sentence as it
was first said, so a frequency that changed language between the instruction
and the request heard the French announcement and then the English
instruction inside it: "je répète, descend to flight level two five zero".

So an instruction is rebuilt from what it carries -- its kind and its values
-- with the phrasebook of the language being spoken now. Only the kinds that
can be rebuilt exactly are; anything else returns nothing, and the caller
repeats the original whole in the language it was issued in, which is one
language for the whole transmission even if not the latest one.
"""

from __future__ import annotations

import re

from .phraseology import Aircraft

# The verbs of a descent, in each language an instruction can be stored in.
_DESCENT = re.compile(r"\b(descend|descendez|descienda|sinken|scenda|desça|desca)",
                      re.IGNORECASE)


def _climbing(instruction) -> bool:
    values = instruction.values or {}
    if "climbing" in values:
        return bool(values["climbing"])
    return not _DESCENT.search(instruction.text or "")


def restate(instruction, p, aircraft: Aircraft) -> str:
    """The instruction as ``p`` would say it, or "" if it cannot be rebuilt."""
    values = instruction.values or {}
    kind = instruction.kind
    try:
        if kind in ("altitude", "vfr_altitude") and "altitude_ft" in values:
            feet = float(values["altitude_ft"])
            if _climbing(instruction):
                return p.climb_maintain(aircraft, feet)
            return p.descend_maintain(aircraft, feet)
        if kind == "go_around" and "altitude_ft" in values:
            return p.go_around_standard(aircraft, float(values["altitude_ft"]))
        if kind in ("handoff", "frequency_change") and "frequency" in values:
            return p.handoff(aircraft, str(values.get("station", "")),
                             float(values["frequency"]))
        if kind == "takeoff_clearance" and "runway" in values:
            return p.cleared_for_takeoff(aircraft, str(values["runway"]))
        if kind == "landing_clearance" and "runway" in values:
            return p.cleared_to_land(aircraft, str(values["runway"]))
        if kind == "line_up_and_wait" and "runway" in values:
            return p.line_up_and_wait(aircraft, str(values["runway"]))
        if kind == "approach_clearance" and "runway" in values:
            return p.cleared_approach(
                aircraft, p.approach_name(values.get("approach", "ILS")),
                str(values["runway"]),
                maintain_ft=float(values.get("altitude_ft", 0) or 0))
        if "heading" in values and set(values) <= {"heading", "direction"}:
            return p.turn_heading(aircraft, float(values["heading"]),
                                  str(values.get("direction", "")))
        if "squawk" in values and set(values) <= {"squawk"}:
            return p.squawk(aircraft, str(values["squawk"]))
    except Exception:
        return ""
    return ""


__all__ = ["restate"]
