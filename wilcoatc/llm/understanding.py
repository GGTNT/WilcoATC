"""The rules-first understanding pipeline.

One entry point, :class:`Understanding`, which the engine calls in place of the
bare rule parser. It applies them in a fixed order:

1. **Rules.** Deterministic, instant, and covering the phraseology a pilot is
   supposed to use. If they classify the transmission, that is the answer and
   no model is consulted.
2. **The model**, only for what the rules could not place, and only when one is
   configured. Its answer is validated against a fixed schema and accepted only
   above a confidence floor.
3. **"Say again."** If neither understood, the controller says so, which is
   both the honest answer and the realistic one.

The model never sees a transmission the rules already understood, so the common
case costs nothing and stays deterministic. It also never writes a reply: it
returns a label and some numbers, and the controller's words come from the
phraseology templates either way.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from ..atc.intents import IntentParser, ParsedIntent
from .base import (
    LLMResult, build_prompt, build_system_prompt, parse_response, to_parsed_intent,
)

log = logging.getLogger(__name__)


@dataclass
class UnderstandingStats:
    """How the two layers are performing, for the console and the log."""

    total: int = 0
    by_rules: int = 0
    by_model: int = 0
    unresolved: int = 0
    model_calls: int = 0
    model_failures: int = 0
    model_time_s: float = 0.0

    @property
    def rule_share(self) -> float:
        return self.by_rules / self.total if self.total else 0.0

    def summary(self) -> str:
        if not self.total:
            return "no transmissions yet"
        parts = [
            f"{self.total} transmissions",
            f"{self.by_rules} by rules",
        ]
        if self.model_calls:
            average = self.model_time_s / max(1, self.model_calls)
            parts.append(f"{self.by_model} by AI ({average:.1f}s average)")
        if self.unresolved:
            parts.append(f"{self.unresolved} not understood")
        return ", ".join(parts)


class Understanding:
    """Rules first, model second, "say again" last."""

    def __init__(
        self,
        parser: IntentParser | None = None,
        provider=None,
        *,
        min_confidence: float = 0.55,
        enabled: bool = True,
    ):
        self.parser = parser or IntentParser()
        self.provider = provider
        self.min_confidence = min_confidence
        self.enabled = enabled
        self.stats = UnderstandingStats()
        self._system = build_system_prompt()
        self._last: LLMResult | None = None

    # ------------------------------------------------------------------

    @property
    def model_available(self) -> bool:
        if not (self.enabled and self.provider is not None):
            return False
        try:
            return bool(self.provider.available())
        except Exception as exc:
            log.debug("provider availability check failed: %s", exc)
            return False

    def describe(self) -> str:
        """One line for the console about what is doing the understanding."""
        if not self.model_available:
            return "Understanding: rules only."
        return (
            f"Understanding: rules, with {self.provider.name} "
            f"({getattr(self.provider, 'model', '?')}) for anything they miss."
        )

    def set_callsign(self, variants: list[str]) -> None:
        self.parser.set_callsign(variants)

    # ------------------------------------------------------------------

    def parse(
        self,
        text: str,
        language: str = "en",
        context: dict[str, Any] | None = None,
    ) -> ParsedIntent:
        """Understand one transmission."""
        self.stats.total += 1

        parsed = self.parser.parse(text, language)
        if parsed.understood:
            self.stats.by_rules += 1
            return parsed

        if not (text or "").strip():
            self.stats.unresolved += 1
            return parsed

        result = self._ask_model(text, context)
        if result is not None and result.usable:
            if result.confidence >= self.min_confidence:
                self.stats.by_model += 1
                log.info(
                    "AI understood %r as %s (confidence %.2f)",
                    text, result.intent.value, result.confidence,
                )
                enriched = to_parsed_intent(result, text, language)
                # Values the rules did find are more trustworthy than the
                # model's, so they win where both produced one.
                merged = dict(enriched.values)
                merged.update(parsed.values)
                enriched.values = merged
                enriched.readback_items = dict(merged)
                enriched.callsign_matched = (
                    parsed.callsign_matched or enriched.callsign_matched
                )
                enriched.station_addressed = parsed.station_addressed
                return enriched
            log.info(
                "AI classified %r as %s but only at %.2f confidence; asking the "
                "pilot to say again instead",
                text, result.intent.value, result.confidence,
            )

        self.stats.unresolved += 1
        return parsed

    def _ask_model(self, text: str, context: dict[str, Any] | None) -> LLMResult | None:
        if not self.model_available:
            return None
        prompt = build_prompt(text, context)
        started = time.perf_counter()
        try:
            raw = self.provider.classify(prompt, self._system)
        except Exception as exc:
            self.stats.model_failures += 1
            log.warning("AI classification failed: %s", exc)
            return None
        finally:
            self.stats.model_calls += 1
            self.stats.model_time_s += time.perf_counter() - started

        result = parse_response(raw)
        result.provider = getattr(self.provider, "name", "")
        result.latency_s = time.perf_counter() - started
        self._last = result
        if not result.usable and raw:
            log.debug("AI returned nothing usable: %r", raw[:200])
        return result

    @property
    def last_result(self) -> LLMResult | None:
        """The most recent model answer, for the console and for debugging."""
        return self._last


__all__ = ["Understanding", "UnderstandingStats"]
