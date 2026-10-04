"""Where the AI layer's model runs.

Two options, in the order most people will want them:

*Ollama*, on this machine. Free, private, and needs no account. A small
instruction-tuned model is entirely adequate for picking one label out of thirty
and reading a couple of numbers out of a sentence.

*The Claude API*, in the cloud. More accurate on the awkward transmissions,
costs a fraction of a cent each, and needs a key.

Both are optional. With neither configured the system runs exactly as it did
before -- the rule parser handles the phraseology, and anything it cannot place
gets "say again", which is what a real controller would say anyway.
"""

from __future__ import annotations

import logging
import os

import requests

from .base import RESPONSE_SCHEMA

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# local: Ollama
# --------------------------------------------------------------------------


class OllamaProvider:
    """Talks to a local Ollama server over HTTP.

    Nothing leaves the machine. Install from ollama.com, then::

        ollama pull llama3.2:3b
    """

    name = "ollama"

    def __init__(
        self,
        model: str = "llama3.2:3b",
        host: str = "http://127.0.0.1:11434",
        timeout_s: float = 12.0,
    ):
        self.model = model
        self.host = host.rstrip("/")
        self.timeout_s = timeout_s
        self._checked: bool | None = None

    def available(self) -> bool:
        """Whether the server is up and has the model."""
        if self._checked is not None:
            return self._checked
        try:
            response = requests.get(f"{self.host}/api/tags", timeout=2.0)
            response.raise_for_status()
            names = {m.get("name", "") for m in response.json().get("models", [])}
        except Exception as exc:
            log.info("Ollama not reachable at %s: %s", self.host, exc)
            self._checked = False
            return False

        # Ollama reports "llama3.2:3b"; a bare "llama3.2" should still match.
        wanted = self.model.split(":")[0]
        self._checked = any(name.split(":")[0] == wanted for name in names)
        if not self._checked:
            log.warning(
                "Ollama is running but %s is not installed. Run: ollama pull %s",
                self.model, self.model,
            )
        return self._checked

    def classify(self, prompt: str, system: str) -> str:
        response = requests.post(
            f"{self.host}/api/chat",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                # Ollama's structured-output mode: the response is constrained
                # to the schema, so there is no prose to strip.
                "format": RESPONSE_SCHEMA,
                "options": {"temperature": 0.0, "num_predict": 256},
            },
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        return response.json().get("message", {}).get("content", "")


# --------------------------------------------------------------------------
# cloud: Claude
# --------------------------------------------------------------------------


class AnthropicProvider:
    """Classifies through the Claude API.

    Uses structured outputs so the reply is schema-valid JSON, and low effort
    because picking one label from a list is not a reasoning problem and the
    round trip sits in the middle of a radio exchange.
    """

    name = "anthropic"

    def __init__(
        self,
        model: str = "claude-opus-5",
        api_key: str | None = None,
        timeout_s: float = 12.0,
        max_tokens: int = 512,
    ):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self._client = None
        self._checked: bool | None = None

    def available(self) -> bool:
        if self._checked is not None:
            return self._checked
        try:
            import anthropic as _sdk  # noqa: F401  (presence check only)
        except ImportError:
            log.info("anthropic package not installed; run: pip install anthropic")
            self._checked = False
            return False
        # A key in the environment is the common case, but the SDK also
        # resolves an `ant auth login` profile, so an unset key is not proof
        # that there are no credentials.
        self._checked = True
        return True

    def _get_client(self):
        if self._client is None:
            import anthropic

            self._client = (
                anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout_s)
                if self.api_key
                else anthropic.Anthropic(timeout=self.timeout_s)
            )
        return self._client

    def classify(self, prompt: str, system: str) -> str:
        import anthropic

        client = self._get_client()
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={
                    "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
                    # Classification is not a reasoning problem, and this call
                    # sits inside a radio exchange where latency is felt.
                    "effort": "low",
                },
            )
        except anthropic.NotFoundError:
            log.error("Model %s not available to this account", self.model)
            raise
        except anthropic.RateLimitError as exc:
            log.warning("Claude API rate limited: %s", exc)
            raise
        except anthropic.APIStatusError as exc:
            log.warning("Claude API error %s: %s", exc.status_code, exc)
            raise
        except anthropic.APIConnectionError as exc:
            log.warning("Claude API unreachable: %s", exc)
            raise

        if response.stop_reason == "refusal":
            log.info("Claude declined to classify the transmission")
            return ""
        return "".join(
            block.text for block in response.content if block.type == "text"
        )


# --------------------------------------------------------------------------


def build_provider(kind: str, **options):
    """Construct a provider by name, or ``None`` when the AI layer is off."""
    kind = (kind or "none").strip().lower()
    if kind in ("", "none", "off", "disabled"):
        return None
    if kind == "ollama":
        return OllamaProvider(
            model=options.get("model") or "llama3.2:3b",
            host=options.get("host") or "http://127.0.0.1:11434",
            timeout_s=float(options.get("timeout_s", 12.0)),
        )
    if kind in ("anthropic", "claude"):
        return AnthropicProvider(
            model=options.get("model") or "claude-opus-5",
            api_key=options.get("api_key") or None,
            timeout_s=float(options.get("timeout_s", 12.0)),
        )
    raise ValueError(f"Unknown AI provider {kind!r}. Use none, ollama or anthropic.")


__all__ = ["OllamaProvider", "AnthropicProvider", "build_provider"]
