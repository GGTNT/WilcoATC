"""Is everything here, and does it work?

One set of checks, two places to read them: ``wilcoatc doctor`` prints them as
a table in the terminal, and the panel's Diagnostics button shows the same
rows in the window. They used to exist only in the terminal, written straight
into a ``rich`` table, which meant the pilot most likely to need them -- the
one who installed the program by double-clicking it and has never opened a
command prompt -- was the one who could not get at them.

Every check answers with a status and a line of detail, and nothing here
raises: a check that cannot run reports that it could not run, because
"unknown" is a useful answer and a traceback in the middle of the list is not.

The statuses are four, and the distinction between the middle two is the
whole point of having more than two:

``ok``      working.
``warn``    working, but not as well as it could be, or not switched on.
``error``   not working. The program will not do its job until this is fixed.
``off``     deliberately not in use. Nothing to fix.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

from .config import Config
from .paths import command_hint, model_dir, resolve

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Check:
    """One line of the report."""

    key: str            # stable, for the panel to attach an icon to
    name: str           # "Controller voices"
    status: str         # ok | warn | error | off
    detail: str         # "25 Kokoro, 6 Piper"
    # What to do about it, when there is something to do. The panel turns this
    # into a button; the terminal prints it.
    fix: str = ""

    @property
    def blocking(self) -> bool:
        return self.status == "error"

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "name": self.name, "status": self.status,
                "detail": self.detail, "fix": self.fix}


def _check(fn: Callable[[Config], Check | None], config: Config,
           key: str, name: str) -> Check | None:
    """Run one check, and turn a failure of the check itself into a row."""
    try:
        return fn(config)
    except Exception as exc:                        # pragma: no cover - defensive
        log.debug("check %s failed", key, exc_info=True)
        return Check(key, name, "error", str(exc)[:80])


# --------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------


def _navdata(config: Config) -> Check:
    from .navdata.db import NavDB

    try:
        db = NavDB()
        frequencies = db._conn.execute(
            "SELECT COUNT(*) FROM frequencies").fetchone()[0]
        airports = db._conn.execute(
            "SELECT COUNT(*) FROM airports").fetchone()[0]
    except Exception as exc:
        return Check("navdata", "Nav database", "error", str(exc)[:80],
                     fix="navdata")
    return Check("navdata", "Nav database", "ok",
                 f"{airports:,} airports, {frequencies:,} frequencies")


def _voices(config: Config) -> Check:
    from .audio.voices import VoiceRegistry

    registry = VoiceRegistry(resolve(config.voice.voice_dir),
                             engine=config.voice.engine,
                             hq_model=config.voice.hq_model)
    available = registry.available()
    if not available:
        return Check("voices", "Controller voices", "error",
                     "none installed", fix="voices")
    if registry.hq_missing():
        # The pilot asked for the cloned voices and will not hear them. The
        # cast still works, which is why this is a warning and not an error.
        return Check("voices", "Controller voices", "warn",
                     f"{len(available)} voices, but voice.engine is "
                     "chatterbox and the cloning model or its prompts are "
                     "not installed", fix="voices")
    # Which engine they come from is the thing worth reporting: a cast made
    # entirely of Piper voices is a working system that sounds ten years old,
    # and it looks identical to a good one from a count alone.
    counts: dict[str, int] = {}
    for spec in available:
        counts[spec.engine] = counts.get(spec.engine, 0) + 1
    detail = ", ".join(f"{count} {name.title()}"
                       for name, count in sorted(counts.items()))
    if counts.get("kokoro"):
        return Check("voices", "Controller voices", "ok", detail)
    return Check("voices", "Controller voices", "warn",
                 f"{detail}, Kokoro not installed", fix="voices")


def _speech(config: Config) -> Check:
    try:
        import faster_whisper as _fw  # noqa: F401  (presence check only)
    except ImportError:
        return Check("speech", "Speech recognition", "error",
                     "faster-whisper is not installed")

    from .audio.stt import model_for

    # Report the model that will actually be loaded: a ".en" model cannot hear
    # anything but English, so a bilingual setup silently uses the multilingual
    # one and the status line has to say so.
    resolved = model_for(config.speech.languages, config.speech.model)
    folder = model_dir()
    cached = (
        list(folder.glob(f"**/*{resolved}/**/model.bin"))
        or list(folder.glob("**/model.bin"))
    ) if folder.exists() else []
    languages = ", ".join(config.speech.languages) or "en"
    detail = f"{resolved} ({config.speech.compute_type}), {languages}"
    if cached:
        return Check("speech", "Speech recognition", "ok", detail)
    return Check("speech", "Speech recognition", "warn",
                 f"{detail} -- not downloaded", fix="speech")


def _audio_out(config: Config) -> Check:
    import sounddevice as sd

    name = sd.query_devices(device=config.audio.output_device,
                            kind="output")["name"]
    return Check("audio_out", "Audio output", "ok", name)


def _audio_in(config: Config) -> Check:
    import sounddevice as sd

    name = sd.query_devices(device=config.audio.input_device,
                            kind="input")["name"]
    return Check("audio_in", "Audio input", "ok", name)


def _cabin_out(config: Config) -> Check | None:
    """Whether the cabin's own output stream will open.

    The cabin and the first officer do not share the radio's player: they open
    a second stream on the same device so that a controller never queues
    behind a safety demonstration. That second stream is a separate thing for
    an audio stack to refuse, and when it refuses the symptom is a crew who
    simply never speak -- there is no error on the radio, because the radio
    is fine.

    So it is opened here, briefly, with nothing written to it. Only when the
    crew are switched on: a pilot who has never asked for a first officer
    does not need a row about one.
    """
    if not config.immersion.enabled:
        return None

    import sounddevice as sd

    from .audio.boarding import SAMPLE_RATE

    try:
        stream = sd.OutputStream(samplerate=SAMPLE_RATE, channels=1,
                                 device=config.audio.output_device,
                                 dtype="float32", blocksize=1024)
        stream.close()
    except Exception as exc:
        return Check("cabin_out", "Cabin audio", "warn",
                     f"the crew cannot open their own output: {exc}")
    return Check("cabin_out", "Cabin audio", "ok",
                 "the crew have an output of their own")


def _audio_devices(config: Config) -> Check:
    from .audio.io import list_audio_devices

    inputs, outputs = list_audio_devices()
    return Check("audio_devices", "Audio devices", "ok",
                 f"{len(inputs)} input, {len(outputs)} output available")


def _simulator(config: Config) -> Check:
    from .navdata.db import NavDB
    from .sim.source import SimConnectSource

    source = SimConnectSource()
    if not source.connect():
        return Check("simulator", "Simulator", "warn",
                     "not running -- start MSFS, or use text mode")
    try:
        state = source.read()
        where = f"{state.latitude:.4f}, {state.longitude:.4f}"
        if state.has_position:
            db = NavDB()
            near = db.home_airport(state.latitude, state.longitude)
            if near is None:
                near = db.nearest_airport(state.latitude, state.longitude,
                                          max_nm=60, min_rank=1)
            where += f"  ({near.ident} {near.name})" if near else "  (no airport near)"
        else:
            where += "  (no position reported)"
        return Check("simulator", "Simulator", "ok",
                     f"{state.callsign or 'unknown'} at {where}")
    finally:
        source.close()


def _ground(config: Config) -> Check | None:
    """Whether the ground handling can be read, and asked for anything.

    This is the row that answers "GSX does nothing". There are four ways for
    it to do nothing and they are not the same problem: the switch is off,
    nothing is installed that can read a local variable, the variables can be
    read and GSX is not running, or all of that works and the menu cannot be
    written to -- which is the difference between a controller that can see
    the ramp and one that can call the tug.
    """
    if not config.gsx.enabled:
        return Check("ground", "Ground handling", "off", "switched off")

    from .integrations.gsx import LVARS, menu_file
    from .integrations.lvars import open_bridge

    bridge = open_bridge(config.gsx.bridge, config.gsx.lvar_file,
                         client=config.gsx.client)
    try:
        if not bridge.available:
            why = bridge.trouble or ("nothing is installed that can read "
                                     "them")
            return Check("ground", "Ground handling", "warn",
                         f"the local variables cannot be read: {why}")
        # The values arrive as messages rather than as a return value, so
        # there is a moment between asking and knowing. Two seconds is many
        # times what it takes and this is a diagnostic, not a flight.
        bridge.read(LVARS)
        deadline = time.monotonic() + 2.0
        values = {}
        while time.monotonic() < deadline and not values:
            time.sleep(0.1)
            values = bridge.read(LVARS)
        if not values:
            return Check("ground", "Ground handling", "warn",
                         f"{bridge.name} is connected and nothing is "
                         f"publishing the GSX variables")
        if not values.get("FSDT_GSX_COUATL_STARTED"):
            return Check("ground", "Ground handling", "warn",
                         f"read through {bridge.name}, and GSX itself is not "
                         f"running")
        detail = f"read through {bridge.name}"
        if config.gsx.control and bridge.can_write and menu_file() is not None:
            detail += ", and its menu can be worked"
        else:
            detail += ", and nothing can be asked for"
        return Check("ground", "Ground handling", "ok", detail)
    finally:
        bridge.close()


def _com1(config: Config) -> Check | None:
    from .sim.source import SimConnectSource

    source = SimConnectSource()
    if not source.connect():
        return None
    try:
        state = source.read()
        if not state.com1_active:
            return None
        return Check("com1", "COM1", "ok", f"{state.com1_active:.3f}")
    finally:
        source.close()


def _frequencies(config: Config) -> Check:
    """Where the frequency list comes from.

    The published dataset is thin: de Gaulle carries nine frequencies in it and
    the simulator has thirty. Which one is in use is the first thing to know
    when the panel and the simulator's ATC menu disagree.
    """
    from .integrations.facilities import find_simconnect

    if not config.sim.frequencies_from_sim:
        return Check("frequencies", "Frequencies", "warn",
                     "published only -- sim.frequencies_from_sim is off")
    library = find_simconnect(config.sim.simconnect_dll)
    if library is None:
        return Check("frequencies", "Frequencies", "warn",
                     "published only -- no SimConnect with the facility API; "
                     "install the MSFS SDK or set sim.simconnect_dll")
    return Check("frequencies", "Frequencies", "ok",
                 f"from the simulator ({library})")


def _ai(config: Config) -> Check:
    from .llm.providers import build_provider

    provider = build_provider(config.ai.provider, model=config.ai.model,
                              host=config.ai.host, api_key=config.ai.api_key)
    if provider is None:
        return Check("ai", "AI understanding", "off",
                     "rules only; set ai.provider to ollama or anthropic")
    if provider.available():
        return Check("ai", "AI understanding", "ok",
                     f"{provider.name} ({getattr(provider, 'model', '?')})")
    return Check("ai", "AI understanding", "warn",
                 f"{provider.name} configured but not reachable")


def _windows_block(config: Config) -> Check | None:
    """The internet mark Windows leaves on an extracted zip.

    Only reported when there is something wrong: startup clears the marks by
    itself, so a green row here would be a line of noise on every machine in
    order to describe a problem almost nobody has. A red one means the
    clearing failed, which is the case worth a pilot's attention.
    """
    if os.name != "nt":
        return None
    from .winzone import marked

    blocked = marked()
    if not blocked:
        return None
    return Check(
        "windows_block", "Windows block", "error",
        f"{len(blocked)} files marked as from the internet; run in this "
        f"folder: Get-ChildItem -Recurse | Unblock-File",
    )


def _weather(config: Config) -> Check:
    from .weather.metar import WeatherProvider

    sample = WeatherProvider(online=config.weather.online).metar("KJFK")
    if sample.weather.raw:
        return Check("weather", "Weather", "ok", sample.weather.raw[:70])
    return Check("weather", "Weather", "warn", "no observation")


# The order they are reported in, which is roughly the order they matter:
# what the program needs to run at all, then what it needs to hear and speak,
# then what it is connected to.
CHECKS: tuple[tuple[str, str, Callable[[Config], Check | None]], ...] = (
    ("navdata", "Nav database", _navdata),
    ("voices", "Controller voices", _voices),
    ("speech", "Speech recognition", _speech),
    ("audio_out", "Audio output", _audio_out),
    ("audio_in", "Audio input", _audio_in),
    ("cabin_out", "Cabin audio", _cabin_out),
    ("audio_devices", "Audio devices", _audio_devices),
    ("simulator", "Simulator", _simulator),
    ("ground", "Ground handling", _ground),
    ("com1", "COM1", _com1),
    ("frequencies", "Frequencies", _frequencies),
    ("ai", "AI understanding", _ai),
    ("windows_block", "Windows block", _windows_block),
    ("weather", "Weather", _weather),
)


def check_all(config: Config | None = None,
              only: set[str] | None = None) -> list[Check]:
    """Every check, in order. Rows that do not apply are left out."""
    config = config or Config()
    rows: list[Check] = []
    for key, name, fn in CHECKS:
        if only and key not in only:
            continue
        row = _check(fn, config, key, name)
        if row is not None:
            rows.append(row)
    return rows


def report(config: Config | None = None) -> dict[str, Any]:
    """The whole report, shaped for the panel.

    ``fixable`` is what the pilot can do something about from a button, which
    is the reason this returns more than the rows: the panel offers a download
    for a missing component and says nothing about a simulator that is not
    running, because one of those is a button and the other is a decision.
    """
    rows = check_all(config)
    return {
        "ok": not any(row.blocking for row in rows),
        "checks": [row.as_dict() for row in rows],
        "fixable": sorted({row.fix for row in rows if row.fix}),
        "hint": command_hint("doctor"),
    }


__all__ = ["Check", "CHECKS", "check_all", "report"]
