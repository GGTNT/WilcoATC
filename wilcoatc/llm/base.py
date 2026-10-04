"""The AI layer: understanding what the pilot meant.

This is deliberately narrow. A language model is used for exactly one job --
turning a transmission the rule-based parser could not classify into a
structured intent -- and it is never allowed anywhere near what the controller
says back.

That boundary is the whole design:

    pilot speech -> recogniser -> rules ---------------> intent
                                    |                      |
                                    +-- (unparsed) -> AI --+
                                                           |
                                              phraseology templates -> ATC

The model chooses from a fixed list of intents and fills a fixed set of typed
fields. Its output is validated against that schema and discarded if it does not
fit. It cannot invent an instruction, cannot phrase a clearance, and cannot put
a word into a controller's mouth, because the controller's words come from
:mod:`wilcoatc.atc.phraseology` and nowhere else.

Why bother at all: the rules cover the phraseology a pilot is *supposed* to use.
Real pilots say "uh, we'd like to get going whenever you're ready" and "any
chance of a shortcut". The model turns those into REQUEST_TAKEOFF and
REQUEST_DIRECT, and the reply is still a template.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..atc.intents import Intent, ParsedIntent

log = logging.getLogger(__name__)

# The only intents the model is allowed to return. Anything else is rejected.
ALLOWED_INTENTS: tuple[str, ...] = tuple(i.value for i in Intent if i is not Intent.UNKNOWN)

# The only values it may fill in, and how each is validated. Keeping this
# explicit is what stops a hallucinated "altitude_ft": 999999 reaching the
# controller logic.
VALUE_SPEC: dict[str, tuple[type, Any, Any]] = {
    "altitude_ft": (int, 0, 60000),
    "heading": (int, 1, 360),
    "speed_kt": (int, 40, 500),
    "frequency": (float, 118.0, 136.999),
    "runway": (str, None, None),
    "squawk": (str, None, None),
    "fix": (str, None, None),
    "atis_letter": (str, None, None),
}


@dataclass
class LLMResult:
    """What the model returned, after validation."""

    intent: Intent = Intent.UNKNOWN
    values: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    raw: str = ""
    provider: str = ""
    latency_s: float = 0.0

    @property
    def usable(self) -> bool:
        return self.intent is not Intent.UNKNOWN


class Provider(Protocol):
    """Anything that can classify a transmission."""

    name: str

    def available(self) -> bool: ...
    def classify(self, prompt: str, system: str) -> str: ...


# --------------------------------------------------------------------------
# prompt
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You classify radio transmissions from a pilot to air traffic control.

You do NOT reply to the pilot. You do NOT write air traffic control \
phraseology. You only report what the pilot asked for, as structured data.

Return one JSON object with these fields:
  intent      one of the listed intent names, or "unknown"
  values      an object holding only the fields you are certain of
  confidence  0.0 to 1.0

Rules:
- Use "unknown" whenever you are not sure. A wrong guess is worse than admitting
  you did not understand, because the controller will simply ask the pilot to
  say again.
- Only include a value you actually heard. Never infer, complete or invent one.
- altitude_ft is in feet: "flight level three five zero" is 35000, "one two
  thousand" is 12000, "niveau de vol trois cinq zero" is 35000.
- heading, runway and squawk are read digit by digit; a squawk is four octal
  digits.
- frequency is megahertz, e.g. 121.9.
- The pilot may speak English or French.

Intents:
{intents}

Examples:
  "any chance of a shortcut direct CAMRN"
      {{"intent": "request_direct", "values": {{"fix": "CAMRN"}}, "confidence": 0.9}}
  "uh yeah we're all set down here whenever you're ready"
      {{"intent": "request_takeoff", "values": {{}}, "confidence": 0.7}}
  "we'd like to get down a bit earlier if you can"
      {{"intent": "request_descent", "values": {{}}, "confidence": 0.8}}
  "on est prets a rouler des que possible"
      {{"intent": "request_taxi", "values": {{}}, "confidence": 0.9}}
  "lovely weather today isn't it"
      {{"intent": "unknown", "values": {{}}, "confidence": 0.9}}
"""

# What each intent means, so the model is not guessing from the name alone.
INTENT_HELP: dict[str, str] = {
    "request_clearance": "asking for an IFR clearance or route clearance",
    "request_vfr_departure": "asking to leave VFR",
    "request_pushback": "asking to push back from the stand",
    "request_taxi": "asking to taxi for departure",
    "request_takeoff": "ready for departure, at or near the runway",
    "check_in": "first call on a new frequency, often stating a level",
    "position_report": "reporting a position or a circuit leg",
    "request_climb": "asking for a higher level",
    "request_descent": "asking for a lower level",
    "request_direct": "asking to route direct to a waypoint",
    "request_vectors": "asking for radar vectors",
    "request_approach": "asking for a particular approach",
    "request_landing": "inbound to land",
    "report_field_in_sight": "has the airport or runway visually",
    "report_traffic_in_sight": "has the other aircraft visually",
    "report_established": "established on the approach or localiser",
    "going_around": "discontinuing the landing",
    "missed_approach": "flying the published missed approach",
    "request_taxi_to_parking": "asking to taxi in after landing",
    "readback": "reading an instruction back to the controller",
    "acknowledge": "roger or wilco, nothing more",
    "affirmative": "yes",
    "negative": "no",
    "say_again": "asking the controller to repeat",
    "standby": "asking the controller to wait",
    "unable": "declining an instruction",
    "request_frequency_change": "asking to leave the frequency",
    "cancel_ifr": "cancelling the IFR flight plan",
    "radio_check": "testing the radio",
    "emergency": "mayday, a declared emergency",
    "urgency": "pan-pan, an urgent but not distress situation",
    "minimum_fuel": "declaring minimum fuel",
}


def build_system_prompt() -> str:
    lines = [
        f"  {name}: {INTENT_HELP.get(name, '')}".rstrip()
        for name in ALLOWED_INTENTS
    ]
    return SYSTEM_PROMPT.format(intents="\n".join(lines))


def build_prompt(text: str, context: dict[str, Any] | None = None) -> str:
    """The user turn: the transmission plus the situation it arrived in."""
    parts = []
    if context:
        described = ", ".join(
            f"{key} {value}" for key, value in context.items() if value
        )
        if described:
            parts.append(f"Situation: {described}.")
    parts.append(f'Pilot transmission: "{(text or "").strip()}"')
    parts.append("Classify it.")
    return "\n".join(parts)


# The JSON schema handed to providers that support structured output.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(ALLOWED_INTENTS) + ["unknown"]},
        "values": {
            "type": "object",
            "properties": {
                "altitude_ft": {"type": "integer"},
                "heading": {"type": "integer"},
                "speed_kt": {"type": "integer"},
                "frequency": {"type": "number"},
                "runway": {"type": "string"},
                "squawk": {"type": "string"},
                "fix": {"type": "string"},
                "atis_letter": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "confidence": {"type": "number"},
    },
    "required": ["intent", "values", "confidence"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def parse_response(raw: str) -> LLMResult:
    """Turn a model response into a validated result.

    Everything is checked: the intent must be one this system implements, and
    every value must be the right type and inside a physically sensible range.
    Anything that fails is dropped rather than corrected, so a malformed answer
    degrades to "say again" instead of to a wrong instruction.
    """
    result = LLMResult(raw=raw or "")
    payload = _extract_json(raw)
    if payload is None:
        return result

    name = str(payload.get("intent", "")).strip().lower()
    if name not in ALLOWED_INTENTS:
        return result

    try:
        result.intent = Intent(name)
    except ValueError:
        return result

    raw_values = payload.get("values")
    if isinstance(raw_values, dict):
        result.values = _validate_values(raw_values)

    try:
        result.confidence = max(0.0, min(1.0, float(payload.get("confidence", 0.0))))
    except (TypeError, ValueError):
        result.confidence = 0.0
    return result


def _validate_values(raw: dict) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        spec = VALUE_SPEC.get(key)
        if spec is None or value is None:
            continue
        kind, low, high = spec
        try:
            if kind is int:
                coerced = int(float(value))
            elif kind is float:
                coerced = float(value)
            else:
                coerced = str(value).strip().upper()
        except (TypeError, ValueError):
            continue

        if kind in (int, float) and low is not None and not (low <= coerced <= high):
            continue
        if key == "squawk" and not re.fullmatch(r"[0-7]{4}", str(coerced)):
            continue
        if key == "runway" and not re.fullmatch(r"0?\d{1,2}[LRC]?", str(coerced)):
            continue
        if key == "atis_letter" and not re.fullmatch(r"[A-Z]", str(coerced)):
            continue
        if key == "fix" and not re.fullmatch(r"[A-Z]{2,6}", str(coerced)):
            continue
        clean[key] = coerced
    return clean


def _extract_json(raw: str) -> dict | None:
    """Pull the JSON object out of a response that may have prose around it."""
    if not raw:
        return None
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


def to_parsed_intent(result: LLMResult, text: str, language: str) -> ParsedIntent:
    """Present an LLM result in the same shape the rule parser produces."""
    return ParsedIntent(
        intent=result.intent,
        text=text,
        normalized=(text or "").lower().strip(),
        language=language,
        callsign_matched=True,
        confidence=result.confidence,
        values=dict(result.values),
        readback_items=dict(result.values),
    )
