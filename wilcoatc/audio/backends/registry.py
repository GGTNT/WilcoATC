"""Which engine speaks this transmission.

The choice is made per language, not once per session, because no engine
speaks all five well. It is made from three things and nothing else: what the
backend declares it can speak, whether it is actually installed, and what the
hardware allows.

**The fallback is not optional.** A transmission that cannot be synthesised is
an aeroplane nobody answers, which is worse than a transmission in the wrong
voice. So a preferred backend that turns out to be missing, or that throws
while a clearance is already composed, falls to the best remaining CPU backend
for the rest of the session -- and the pilot is told once, because the
controllers will have changed voice and that needs explaining.

What this deliberately does *not* do is choose per controller. Casting is
:mod:`wilcoatc.audio.voices`, it is a stable hash of facility and position,
and it has to stay that way: Kennedy Tower sounding like a different person
because a different engine loaded is the exact failure the casting exists to
prevent.
"""

from __future__ import annotations

import logging
import threading

from .base import Capabilities, TTSBackend, TTSUnavailable
from .hardware import gpu_available

log = logging.getLogger(__name__)


class BackendRegistry:
    """The installed engines, and which of them speaks a given language.

    ``prefer`` is the name of a backend from the config, or ``"auto"``.
    Naming one that cannot speak the language, or is not installed, is not an
    error: the preference is a preference, and the fallback below it still
    applies. Naming one that does not exist at all *is* an error, and is
    reported once at registration rather than on the first transmission.
    """

    def __init__(self, prefer: str = "auto", allow_gpu: bool = False):
        self.prefer = (prefer or "auto").strip().lower()
        #: Whether the pilot has opted into the GPU tier. Off by default, and
        #: still subject to there being a GPU.
        self.allow_gpu = bool(allow_gpu)
        self._backends: list[TTSBackend] = []
        self._chosen: dict[str, TTSBackend] = {}
        #: Backends that failed mid-session. Not retried: the first failure
        #: cost a transmission and the second would cost another.
        self._broken: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------

    def register(self, backend: TTSBackend) -> TTSBackend:
        """Add one engine. Registration order is the last tie-break."""
        with self._lock:
            self._backends.append(backend)
            self._chosen.clear()
        return backend

    @property
    def backends(self) -> list[TTSBackend]:
        return list(self._backends)

    def capabilities(self) -> list[Capabilities]:
        """What is registered, for the diagnostics report."""
        return [backend.capabilities for backend in self._backends]

    def installed(self) -> list[Capabilities]:
        """What is registered *and* usable right now."""
        return [backend.capabilities for backend in self._backends
                if self._usable(backend)]

    # ------------------------------------------------------------------

    def for_language(self, language: str) -> TTSBackend:
        """The engine that speaks this language, preference first.

        Raises :class:`~wilcoatc.audio.backends.base.TTSUnavailable` when
        nothing can, which the caller turns into something a pilot can act on
        rather than a stack trace.
        """
        code = (language or "en").lower()[:2]
        cached = self._chosen.get(code)
        if cached is not None and cached.capabilities.name not in self._broken:
            return cached

        candidates = self._ranked(code)
        if not candidates:
            raise TTSUnavailable(
                f"No speech engine is installed that can speak {code!r}."
            )
        chosen = candidates[0]
        with self._lock:
            self._chosen[code] = chosen
        return chosen

    def mark_broken(self, backend: TTSBackend) -> None:
        """This one threw while a controller was speaking. Do not use it again.

        Every cached choice is dropped, not just the one that failed: an
        engine that has fallen over for French has almost certainly fallen
        over for English too, and finding that out one language at a time
        costs one transmission each.
        """
        name = backend.capabilities.name
        with self._lock:
            self._broken.add(name)
            self._chosen.clear()
        log.warning("speech engine %s is out for this session", name)

    def invalidate(self) -> None:
        """Look again -- something was installed while the program was running.

        Deliberately does not clear ``_broken``: a download does not repair an
        engine that threw.
        """
        with self._lock:
            self._chosen.clear()

    # ------------------------------------------------------------------

    def _ranked(self, language: str) -> list[TTSBackend]:
        """Usable engines for one language, best first.

        The order is: the named preference, then CPU before GPU, then
        registration order. CPU first is not a quality judgement -- it is the
        constraint this program is built around, that the card belongs to the
        simulator.
        """
        usable = [backend for backend in self._backends
                  if backend.capabilities.speaks(language)
                  and self._usable(backend)]

        def rank(backend: TTSBackend) -> tuple[int, int, int]:
            caps = backend.capabilities
            preferred = 0 if caps.name.lower() == self.prefer else 1
            tier = 1 if caps.needs_gpu else 0
            return (preferred, tier, self._backends.index(backend))

        return sorted(usable, key=rank)

    def _usable(self, backend: TTSBackend) -> bool:
        caps = backend.capabilities
        if caps.name in self._broken:
            return False
        # A GPU engine is reached two ways: the pilot has said the card is
        # free and there is one, or the pilot has named the engine outright.
        # Naming it is taken as knowing what it costs -- on a CPU it will run,
        # slowly, and the backend says so in the log -- because the
        # alternative is a setting that is silently ignored on the machine
        # the pilot most wanted it on.
        named = caps.name.lower() == self.prefer
        if caps.needs_gpu and not named \
                and not (self.allow_gpu and gpu_available()):
            return False
        try:
            return bool(backend.available())
        except Exception:                            # pragma: no cover
            # available() is documented not to raise. One that does is broken
            # in a way that will not improve by being asked again.
            log.debug("%s.available() raised", caps.name, exc_info=True)
            return False


__all__ = ["BackendRegistry"]
