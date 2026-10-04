"""Command line interface.

``python -m wilcoatc`` with no arguments starts the radio.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import logs
from . import __version__
from .config import Config, CONFIG_PATH
from .paths import (command_hint, log_dir, model_dir as _model_dir,
                    resolve as _resolve)
from .navdata.channels import channel_name
from .navdata.db import NavDB, POSITION_SUFFIX

console = Console()

STYLES = {
    "pilot": "bold cyan",
    "atc": "bold green",
    "atis": "green",
    "info": "dim",
    "error": "bold red",
}


def _print_event(event) -> None:
    style = STYLES.get(event.kind, "")
    stamp = time.strftime("%H:%M:%S", time.localtime(event.at))
    if event.kind == "pilot":
        console.print(f"[dim]{stamp}[/dim] [{style}]YOU[/{style}]  {event.text}")
    elif event.kind in ("atc", "atis"):
        who = event.speaker or "ATC"
        console.print(f"[dim]{stamp}[/dim] [{style}]{who}[/{style}]")
        console.print(f"         {event.text}")
    elif event.kind == "error":
        console.print(f"[dim]{stamp}[/dim] [{style}]!![/{style}]   {event.text}")
    else:
        console.print(f"[dim]{stamp}  {event.text}[/dim]")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_setup(args) -> int:
    """Download everything the system needs."""
    from .audio.voices import download_voices
    from .navdata.build import build
    from .navdata.fetch import fetch_all

    console.rule("[bold]Navigation data")
    fetch_all(force=args.force)
    # Only build when there is nothing to build on, or when asked outright.
    #
    # Rebuilding starts by deleting the database, and the database is not only
    # the published data: frequencies read out of the simulator with
    # `freq --from-sim` are written straight into it and nowhere else, so a
    # rebuild silently throws them away. Running setup a second time -- which
    # is a reasonable thing to do, and is what somebody does when they think
    # something is missing -- used to cost a pilot every frequency they had
    # imported.
    from .navdata.build import DB_PATH

    if args.force or not DB_PATH.exists():
        console.print("Building the nav database...")
        build()
    else:
        console.print(f"[dim]The nav database is already built: {DB_PATH}\n"
                      "Pass --force to build it again. Note that rebuilding "
                      "discards frequencies imported from the simulator.[/dim]")

    console.rule("[bold]Controller voices")
    download_voices("all" if args.all_voices else "core", force=args.force)
    if args.hq:
        config = Config.load(args.config)
        console.rule("[bold]Cloned voices")
        download_voices("chatterbox", force=args.force,
                        hq_model=config.voice.hq_model)

    console.rule("[bold]Speech recognition")
    from .audio.stt import Recognizer

    config = Config.load(args.config)
    recognizer = Recognizer(model_size=config.speech.model)
    console.print(f"Loading Whisper {config.speech.model} (first run downloads it)...")
    recognizer.warm()

    console.rule("[bold]Ready")
    console.print(f"Run [bold]{command_hint()}[/bold] to start the radio.")
    return 0


def cmd_doctor(args) -> int:
    """Check that every component is present and working.

    The checks themselves live in :mod:`wilcoatc.health`, because the panel
    shows the same report in its own window and a pilot who installed this by
    double-clicking it should not have to open a command prompt to read it.
    This is the terminal's rendering of them and nothing more.
    """
    from .health import check_all

    config = Config.load(args.config)
    table = Table(title="WilcoATC status", show_header=True, header_style="bold")
    table.add_column("Component")
    table.add_column("Status")
    table.add_column("Detail")

    colours = {"ok": "green", "warn": "yellow", "error": "red", "off": "dim"}
    words = {"ok": "ok", "warn": "check", "error": "missing", "off": "off"}

    rows = check_all(config)
    for row in rows:
        colour = colours.get(row.status, "yellow")
        detail = row.detail
        if row.fix:
            detail += f"  ->  run: {command_hint('setup')}"
        table.add_row(row.name, f"[{colour}]{words.get(row.status, row.status)}[/{colour}]",
                      detail)

    console.print(table)
    return 0 if not any(row.blocking for row in rows) else 1


def cmd_devices(args) -> int:
    from .audio.io import JoystickPushToTalk, list_audio_devices

    inputs, outputs = list_audio_devices()
    table = Table(title="Audio devices")
    table.add_column("Kind")
    table.add_column("Index", justify="right")
    table.add_column("Name")
    for index, name in inputs:
        table.add_row("input", str(index), name)
    for index, name in outputs:
        table.add_row("output", str(index), name)
    console.print(table)

    sticks, reason = JoystickPushToTalk.scan()
    if sticks:
        # Listing a device proves only that SDL knows it exists. Reading a
        # button needs the event queue moving, which is a separate thing and
        # was for a while the reason the button did nothing at all: the list
        # looked right and no press ever arrived.
        try:
            live = JoystickPushToTalk._pump(JoystickPushToTalk._import())
        except Exception:
            live = False
        table = Table(title="Controllers (for joystick push-to-talk)")
        table.add_column("Index", justify="right")
        table.add_column("Name")
        table.add_column("Buttons", justify="right")
        for index, name, buttons in sticks:
            table.add_row(str(index), name, str(buttons))
        console.print(table)
        if not live:
            console.print("[yellow]Button presses cannot be read on this "
                          "computer: SDL will not run its event queue. Use a "
                          "keyboard key for push-to-talk.[/yellow]")
    elif reason == JoystickPushToTalk.MISSING:
        from .paths import FROZEN

        console.print("[dim]Joystick push-to-talk needs pygame-ce.[/dim]")
        if FROZEN:
            # Installing it into some other Python does nothing for a packaged
            # build, which carries its own. Saying "pip install" here would
            # send somebody round a loop they cannot get out of.
            console.print("[dim]This is a packaged build, which should ship "
                          "with it. Please report this.[/dim]")
        else:
            console.print("[dim]pip install pygame-ce[/dim]")
    elif reason and reason != JoystickPushToTalk.NONE:
        console.print(f"[yellow]Controllers could not be read: {reason}[/yellow]")
    else:
        console.print("[dim]No game controllers detected. Plug one in and "
                      "run this again.[/dim]")

    here = {name for _index, name, _buttons in sticks}
    known = [name for name in JoystickPushToTalk.remembered()
             if name not in here]
    if known:
        # A yoke that is switched off, or plugged into a hub that is, looks
        # exactly like a yoke that is not supported. It is not, and this says
        # so by name.
        console.print("[dim]Seen on this computer before, but not connected "
                      "now: " + ", ".join(known) + ".[/dim]")
    return 0


def _frequencies_from_sim(ident: str) -> int:
    """Replace an airport's published frequencies with the simulator's.

    Returns 0 when the table that follows should be printed, and a failure code
    when there is nothing to print because nothing was read.
    """
    from .integrations.facilities import SimFacilities, find_simconnect
    from .navdata.localdata import replace_airport

    library = find_simconnect()
    if library is None:
        console.print(
            "[red]No SimConnect library that can answer facility "
            "questions.[/red]\n"
            "It comes with the MSFS SDK. Install that, or name yours in "
            "config.yaml as [bold]sim.simconnect_dll[/bold]."
        )
        return 1

    facilities = SimFacilities()
    if not facilities.open():
        console.print("[red]Could not reach the simulator.[/red] "
                      "Is it running?")
        return 1
    try:
        found = facilities.frequencies(ident)
        if not found:
            console.print(f"[yellow]The simulator has no frequencies for "
                          f"{ident.upper()}.[/yellow] Keeping what is "
                          f"published.")
            return 1
        added, gone = replace_airport(ident, found)
    finally:
        facilities.close()

    if not added:
        console.print(f"[red]No airport {ident.upper()} in the database.[/red]")
        return 1
    console.print(f"[green]{ident.upper()}: {added} frequencies from the "
                  f"simulator, replacing {gone}.[/green]")
    return 0


def cmd_freq(args) -> int:
    if getattr(args, "audit", False):
        return _audit_frequencies(fix=getattr(args, "fix", False))
    if not args.airport:
        console.print("[red]Name an airport, or pass --audit.[/red]")
        return 1
    if getattr(args, "add", None):
        return _add_frequency(args)
    if getattr(args, "from_sim", False):
        code = _frequencies_from_sim(args.airport)
        if code:
            return code

    db = NavDB()
    airport = db.airport(args.airport)
    if airport is None:
        console.print(f"[red]No airport matching {args.airport!r}.[/red]")
        return 1

    console.print(Panel(
        f"[bold]{airport.name}[/bold]\n"
        f"{airport.ident}  spoken as \"{airport.spoken}\"  "
        f"elevation {airport.elev_ft:.0f} ft  "
        f"{'FAA' if airport.dialect == 'faa' else 'ICAO'} phraseology",
        title=airport.ident,
    ))

    table = Table(show_header=True, header_style="bold")
    table.add_column("Position")
    table.add_column("Frequency", justify="right")
    table.add_column("On an 8.33 radio", justify="right", style="dim")
    table.add_column("Called")
    table.add_column("Published as", style="dim")
    order = ["ATIS", "AWOS", "DEL", "GND", "TWR", "DEP", "APP", "CTR", "CTAF", "INFO"]
    stations = sorted(
        db.stations(airport.ident),
        key=lambda s: (order.index(s.position) if s.position in order else 99,
                       s.priority, s.mhz),
    )
    for station in stations:
        shown = channel_name(station.mhz)
        table.add_row(
            POSITION_SUFFIX.get(station.position, station.position),
            f"{station.mhz:.3f}",
            f"{shown:.3f}" if abs(shown - station.mhz) > 1e-6 else "",
            station.callsign,
            station.description,
        )
    console.print(table)

    runways = db.runways(airport.ident)
    if runways:
        table = Table(title="Runways", show_header=True, header_style="bold")
        table.add_column("Runway")
        table.add_column("Length", justify="right")
        table.add_column("Surface")
        for rw in runways:
            table.add_row(f"{rw.le_ident}/{rw.he_ident}",
                          f"{rw.length_ft:,.0f} ft", rw.surface or "?")
        console.print(table)
    return 0


def _add_frequency(args) -> int:
    """Write down a frequency the published data does not have."""
    from .navdata.localdata import Rejected, add_frequency

    position, raw = args.add[0], args.add[1]
    try:
        mhz = float(raw)
    except ValueError:
        console.print(f"[red]{raw!r} is not a frequency.[/red]")
        return 1
    try:
        line = add_frequency(args.airport, position, mhz,
                             name=args.name or "", description=args.note or "")
    except Rejected as why:
        console.print(f"[red]{why}[/red]")
        return 1
    console.print(f"[green]Added[/green] {line}")
    return 0


def _audit_frequencies(fix: bool = False) -> int:
    """Check the whole frequency table against what a radio can do."""
    import sqlite3

    from .navdata.audit import audit, summarise
    from .navdata.build import DB_PATH
    from .navdata.localdata import snap_in_place

    if fix:
        moved = snap_in_place()
        console.print(f"[green]{moved}[/green] frequencies moved onto a "
                      f"selectable channel.")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        report = audit(conn, deep=True)
    finally:
        conn.close()

    for line in summarise(report):
        console.print(line)
    if report.errors:
        console.print(f"\n[yellow]{len(report.errors)} of these cannot be "
                      f"tuned at all. Run with --fix to put them back on the "
                      f"grid.[/yellow]")
    return 0


def cmd_simbrief(args) -> int:
    """Show the latest SimBrief plan, and optionally keep it."""
    from .integrations.simbrief import SimBriefError, fetch

    config = Config.load(args.config)
    user = args.user or config.simbrief.username
    if not user:
        console.print("[red]Give a SimBrief username, or set "
                      "simbrief.username in config.yaml.[/red]")
        return 1
    try:
        plan = fetch(user)
    except SimBriefError as why:
        console.print(f"[red]{why}[/red]")
        return 1

    table = Table(show_header=False, box=None)
    for label, value in (
        ("Callsign", plan.callsign),
        ("Aircraft", f"{plan.aircraft_type} {plan.registration}".strip()),
        ("From", plan.origin),
        ("To", plan.destination),
        ("Alternate", plan.alternate),
        ("Cruise", f"{plan.cruise_altitude_ft:,} ft"
                   if plan.cruise_altitude_ft else ""),
        ("Planned runway", plan.planned_runway),
        ("Route", plan.route),
    ):
        if value:
            table.add_row(f"[dim]{label}[/dim]", str(value))
    console.print(Panel(table, title=f"SimBrief · {user}"))

    if args.save:
        config.simbrief.username = user
        config.flight.callsign = plan.callsign or config.flight.callsign
        config.flight.aircraft_type = (plan.aircraft_type
                                       or config.flight.aircraft_type)
        config.flight.departure = plan.origin
        config.flight.destination = plan.destination
        if plan.cruise_altitude_ft:
            config.flight.cruise_altitude_ft = plan.cruise_altitude_ft
        config.save(args.config)
        console.print(f"[green]Saved to {args.config}.[/green]")
    return 0


def cmd_gsx(args) -> int:
    """Say what the ground handling is doing, and how it is being read."""
    from .integrations.gsx import LVARS, interpret
    from .integrations.lvars import open_bridge

    config = Config.load(args.config)
    if not config.gsx.enabled:
        console.print("[yellow]Ground handling is switched off in the "
                      "config (gsx.enabled).[/yellow]")
        return 0

    bridge = open_bridge(config.gsx.bridge, config.gsx.lvar_file, None,
                         client=config.gsx.client)
    if not bridge.available:
        console.print(Panel(
            f"No bridge to the simulator's local variables"
            f"{': ' + bridge.trouble if bridge.trouble else ''}.\n\n"
            "GSX publishes what it is doing in [bold]L:[/bold] variables, "
            "which plain SimConnect cannot read. Either install the "
            "MobiFlight WASM module, or point [bold]gsx.lvar_file[/bold] at a "
            "JSON file of variable names to numbers that something else "
            "writes.\n\n"
            "Without one, the controller still notices a pushback from the "
            "aeroplane's own movement, which works with any ground handling "
            "add-on, and an approved push is started through the simulator's "
            "own pushback instead of through GSX's menu.",
            title="Ground handling"))
        _report_tug(config, None)
        return 0

    # Asking is not the same as knowing. A variable read through the
    # MobiFlight channel arrives as a message some milliseconds later, so a
    # read taken the instant after the subscription returns nothing at all --
    # which looks exactly like a stand where nothing is happening.
    values = bridge.read(LVARS)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not values:
        time.sleep(0.1)
        values = bridge.read(LVARS)

    if not values:
        console.print(Panel(
            f"{bridge.name} is connected and nothing is publishing the GSX "
            f"variables.\n\nEither GSX is not running, or the module cannot "
            f"see it. The controller still notices a pushback from the "
            f"aeroplane's own movement.",
            title="Ground handling"))
        _report_tug(config, bridge)
        return 0

    state = interpret(values, source=bridge.name)
    console.print(Panel(state.describe(), title="Ground handling"))
    if not state.running:
        console.print("[yellow]GSX itself is not running: its variables read "
                      "back, and its scripts have not started.[/yellow]")
    if not state.can_taxi:
        console.print(f"[yellow]Cannot taxi: {state.why_not()}.[/yellow]")
    _report_tug(config, bridge)
    bridge.close()
    return 0


def _report_tug(config, bridge) -> None:
    """How an approved push would actually be started, and whether it could.

    Worth printing whichever way the read went. "GSX does nothing when I ask
    for a push" has four possible causes -- the setting, a bridge that can
    only read, a GSX that is not running, and a stand with no push on its
    menu -- and only one of them is visible from the aeroplane.
    """
    from .integrations.gsx import (SERVICES, menu_file, read_menu,
                                   pushback_choice)

    if not config.gsx.control:
        console.print("[yellow]Calling the tug is switched off "
                      "(gsx.control).[/yellow]")
        return
    if bridge is None or not getattr(bridge, "can_write", False):
        console.print("Push-back: the simulator's own pushback. GSX starts "
                      "with it where it is set to take the default pushback "
                      "over.")
        return
    path = menu_file()
    if path is None:
        console.print("Push-back: the simulator's own pushback. GSX has not "
                      "written a menu file, so it is either not installed or "
                      "has not run yet.")
        return
    entries = read_menu(path)
    choice = pushback_choice(entries)
    if choice < 0:
        console.print(f"Push-back: through the GSX menu ({path}). Its last "
                      "menu had no pushback entry on it, which is normal "
                      "away from a stand.")
        return
    console.print(f"Push-back: through the GSX menu ({path}), entry "
                  f"{choice} -- {entries[choice]!r}.")
    if config.gsx.voice:
        console.print("Out loud: " + ", ".join(
            f'"ground crew, request {name.replace("_", " ")}"'
            for name in ("pushback", "boarding", "refuel")) + ", and the rest "
            f"of {len(SERVICES)} services.")


def cmd_atis(args) -> int:
    from .atc.atis import AtisGenerator
    from .atc.phraseology import Phraseology
    from .weather.metar import WeatherProvider, flight_category

    db = NavDB()
    airport = db.airport(args.airport)
    if airport is None:
        console.print(f"[red]No airport matching {args.airport!r}.[/red]")
        return 1

    provider = WeatherProvider()
    sample = provider.metar(airport.icao or airport.ident)
    generator = AtisGenerator(db, airport, Phraseology(airport.dialect))
    report = generator.issue(sample.weather)

    from .atc.written import as_written

    console.print(Panel(as_written(report.text),
                        title=f"{airport.ident} information {report.letter}"))
    console.print(f"[dim]source: {sample.source}   "
                  f"{sample.weather.raw or 'no raw observation'}[/dim]")
    console.print(f"[dim]category: {flight_category(sample.weather)}   "
                  f"runways in use: {', '.join(report.landing_runways) or 'n/a'}[/dim]")

    if args.play:
        _speak_once(report.text, airport, "ATIS", db)
    return 0


def cmd_say(args) -> int:
    """Render one transmission, to hear a controller's voice."""
    db = NavDB()
    airport = db.airport(args.airport) if args.airport else None
    if airport is None:
        console.print("[red]Specify an airport with --airport.[/red]")
        return 1
    _speak_once(args.text, airport, args.position, db, save=args.save)
    return 0


def _speak_once(text: str, airport, position: str, db: NavDB,
                save: str | None = None) -> None:
    from .audio.io import RadioPlayer
    from .audio.tts import Synthesizer

    station = db.station_for(airport.ident, position)
    facility = station.facility if station else airport.spoken
    synth = Synthesizer()
    console.print(f"[dim]synthesising as {facility} {position}...[/dim]")
    transmission = synth.speak(text, facility, position, airport.ident)
    console.print(f"[dim]voice: {transmission.voice_key}  "
                  f"{transmission.duration_s:.1f}s[/dim]")

    if save:
        import soundfile as sf

        sf.write(save, transmission.audio, transmission.sample_rate)
        console.print(f"Written to {save}")
        return

    player = RadioPlayer()
    player.play(transmission.audio, transmission.sample_rate)
    player.wait_until_idle(timeout=transmission.duration_s + 5)
    player.stop()


def cmd_sound_test(args) -> int:
    """Play a real controller transmission, to prove the audio path works."""
    import numpy as np
    import sounddevice as sd

    from .audio.io import RadioPlayer

    config = Config.load(args.config)
    device = config.audio.output_device

    try:
        info = sd.query_devices(device=device, kind="output")
        console.print(
            f"Output device: [bold]{info['name']}[/bold] "
            f"({info['max_output_channels']} channels, "
            f"{info['default_samplerate']:.0f} Hz default)"
        )
    except Exception as exc:
        console.print(f"[red]Cannot open an output device: {exc}[/red]")
        console.print(f"Run [bold]{command_hint('devices')}[/bold] and set "
                      "audio.output_device in config.yaml.")
        return 1

    console.print("[dim]Check this is the device you are actually listening to.[/dim]\n")

    player = RadioPlayer(device=device, volume=config.audio.volume)

    console.print("1/3  a one-second tone...")
    rate = 22050
    t = np.arange(rate) / rate
    tone = (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    player.play(tone, rate)
    player.wait_until_idle(timeout=8)

    console.print("2/3  a controller transmission...")
    try:
        db = NavDB()
        airport = db.airport(args.airport)
        if airport is None:
            airport = db.airport("KJFK")
        station = db.station_for(airport.ident, "TWR")
        from .atc.phraseology import Aircraft, Phraseology, Weather
        from .atc.speech import parse_callsign
        from .audio.imperfections import from_config as imperfections_for
        from .audio.prosody import from_config as prosody_for
        from .audio.tone import from_config as tone_for
        from .audio.tts import Synthesizer

        phrase = Phraseology(airport.dialect)
        aircraft = Aircraft(parse_callsign("DAL1234"))
        runway = db.best_runway(airport.ident, 0, 0)
        text = phrase.cleared_for_takeoff(
            aircraft, runway[0] if runway else "27L",
            Weather(wind_dir=270, wind_kt=10),
            station=station.callsign if station else f"{airport.spoken} Tower",
            abbreviated=False,
        )
        console.print(f"[dim]{text}[/dim]")
        synth = Synthesizer(
            voice_dir=_resolve(config.voice.voice_dir),
            engine=config.voice.engine,
            # ``say`` is what a pilot auditions a voice with, so it has to be
            # the delivery they will actually hear rather than the bare engine.
            prosody_settings=prosody_for(config.delivery.prosody),
            imperfection_settings=imperfections_for(
                config.delivery.imperfections),
            tone_settings=tone_for(config.delivery.tone),
            gpu=config.voice.gpu,
            hq_model=config.voice.hq_model,
            hq_takes=config.voice.hq_takes,
        )
        facility = station.facility if station else airport.spoken
        transmission = synth.speak(text, facility, "TWR", airport.ident,
                                   radio=config.audio.radio_effects)
        player.play(transmission.audio, transmission.sample_rate)
        player.wait_until_idle(timeout=transmission.duration_s + 10)
    except Exception as exc:
        console.print(f"[red]Synthesis failed: {exc}[/red]")
        player.stop()
        return 1

    player.stop()

    # And the other path. The first officer and the cabin do not come out of
    # the player above: they have a second output stream of their own, so that
    # a controller never has to queue behind a safety demonstration. That
    # second stream is a separate thing for an audio stack to refuse, and
    # when it refuses, the radio is perfect and the crew are silent -- which
    # is unfalsifiable from the flight deck and is why this step exists.
    console.print("3/3  the first officer, on the cabin path...")
    if not _cabin_test(config):
        return 1

    console.print("\nIf you heard all three, audio is working.")
    console.print(f"If not, pick a different device: [bold]{command_hint('devices')}[/bold], "
                  "then set [bold]audio.output_device[/bold] in config.yaml.")
    return 0


def _cabin_test(config) -> bool:
    """Play one crew line through the crew's own output. True if it played."""
    import time

    from .audio.imperfections import from_config as imperfections_for
    from .audio.io import CabinPlayer
    from .audio.prosody import from_config as prosody_for
    from .audio.radio_fx import PROFILE_INTERCOM
    from .audio.tone import from_config as tone_for
    from .audio.tts import Synthesizer

    trouble: list[str] = []
    cabin = CabinPlayer(device=config.audio.output_device,
                        volume=config.immersion.volume,
                        on_trouble=trouble.append)
    try:
        synth = Synthesizer(
            voice_dir=_resolve(config.voice.voice_dir),
            engine=config.voice.engine,
            prosody_settings=prosody_for(config.delivery.prosody),
            imperfection_settings=imperfections_for(
                config.delivery.imperfections),
            tone_settings=tone_for(config.delivery.tone),
            gpu=config.voice.gpu,
            hq_model=config.voice.hq_model,
            hq_takes=config.voice.hq_takes,
        )
        spoken = synth.speak(
            "Positive rate, gear up. After takeoff checklist complete.",
            facility="crew", position="FO", ident="crew",
            profile=PROFILE_INTERCOM, radio=True, signal_quality=1.0,
            gender=(config.immersion.copilot_gender
                    if config.immersion.copilot_gender in ("m", "f") else ""),
            voice_key=config.immersion.copilot_voice or "",
            shaped=False,
        )
    except Exception as exc:
        console.print(f"[red]The crew could not be synthesised: {exc}[/red]")
        return False

    console.print(f"[dim]voice: {spoken.voice_key}, at "
                  f"{config.immersion.volume:g} of the radio's level[/dim]")
    cabin.play(spoken.audio, spoken.sample_rate, label="FO")
    deadline = time.monotonic() + spoken.duration_s + 15
    while cabin.busy and time.monotonic() < deadline:
        time.sleep(0.1)
    wrote = cabin._wrote
    cabin.stop()

    if trouble:
        console.print(f"[red]The cabin cannot reach the speaker: "
                      f"{trouble[0]}[/red]")
        console.print("The radio works and the crew do not, which is one "
                      "device refusing a second stream.")
        return False
    if wrote < len(spoken.audio):
        console.print(f"[yellow]Only {wrote:,} of {len(spoken.audio):,} "
                      f"samples reached the device.[/yellow]")
        return False
    return True


def cmd_online(args) -> int:
    """Who is on VATSIM or IVAO right now, and which of them you would hear.

    Worth running before a flight: it is the list of frequencies this program
    will stay off, and the reason it will.
    """
    from .integrations.network import PROVIDERS, OnlineNetwork

    config = Config.load(args.config)
    provider = (args.provider or config.network.provider or "none").lower()
    if provider not in PROVIDERS:
        console.print(
            f"[yellow]No network selected.[/yellow] Set [bold]network.provider"
            f"[/bold] to one of: {', '.join(sorted(PROVIDERS))}.")
        return 1

    online = OnlineNetwork(provider, config.network.refresh_s)
    console.print(f"Reading {provider}...")
    controllers = online.refresh()
    if online.error:
        console.print(f"[red]{online.error}[/red]")
        return 1
    if not controllers:
        console.print("[yellow]Nobody is online.[/yellow]")
        return 0

    wanted = (args.airport or "").upper()
    if wanted:
        controllers = [c for c in controllers if c.ident == wanted]
        if not controllers:
            console.print(f"[yellow]Nobody online at {wanted}.[/yellow]")
            return 0

    table = Table(box=None, pad_edge=False)
    for column in ("callsign", "position", "frequency", "who", "ATIS"):
        table.add_column(column, overflow="fold")
    for controller in sorted(controllers,
                             key=lambda c: (c.ident, c.position, c.mhz)):
        table.add_row(
            controller.callsign, controller.position,
            f"{controller.mhz:.3f}", controller.name or "-",
            (controller.atis_letter or "yes") if controller.atis else "",
        )
    console.print(table)
    console.print(f"[dim]{len(controllers)} online on {provider}.[/dim]")
    return 0


def cmd_logs(args) -> int:
    """Read the logs back: the application log, or the radio transcript.

    The transcript is the one worth having. It holds every transmission with
    the reading the parser gave it, so "it heard me wrong" becomes a question
    with an answer -- which intent it chose, how sure it was, and which
    numbers it took out of the sentence.

    What is stored is the spoken form, because that is what was said and what
    the parser was given; what is printed is the written one, for the same
    reason the panel shows it that way. The record keeps the evidence and the
    reader gets the strip.
    """
    import json

    from .atc.written import as_written

    config = Config.load(args.config)
    directory = Path(config.logging.directory) if config.logging.directory \
        else log_dir()

    if args.what == "radio":
        path = directory / "radio.jsonl"
        if not path.exists():
            console.print(f"[yellow]No radio transcript at {path}.[/yellow]")
            return 1
        rows = _tail_json(path, args.lines)
        table = Table(box=None, pad_edge=False)
        for column in ("time", "kind", "who", "said", "read as"):
            table.add_column(column, overflow="fold")
        for row in rows:
            if args.language and row.get("language") != args.language:
                continue
            # The reasoning rows have their own view, ``logs arrival``.
            if row.get("kind") == "decision":
                continue
            # What the machine made of it. For a transmission the pilot made
            # that is the intent and how sure the parser was; for one the
            # controller made it is the instruction it was, which is what the
            # readback will be checked against.
            reading = row.get("intent", "")
            if row.get("confidence") is not None and reading:
                reading = f"{reading} {row['confidence']}"
            if not reading:
                reading = row.get("instruction", "") or ""
            owed = row.get("owed") or []
            if owed:
                reading = f"{reading} owes {', '.join(owed)}".strip()
            if row.get("unprompted"):
                reading = f"{reading} (unprompted: {row.get('why', '')})".strip()
            table.add_row(
                str(row.get("time", ""))[-8:],
                str(row.get("kind", "")),
                str(row.get("station") or row.get("speaker") or ""),
                as_written(str(row.get("text", "")),
                           figures=config.ui.numerals),
                reading,
            )
        console.print(table)
        return 0

    if args.what == "arrival":
        path = directory / "radio.jsonl"
        if not path.exists():
            console.print(f"[yellow]No radio transcript at {path}.[/yellow]")
            return 1
        table = Table(box=None, pad_edge=False)
        for column in ("time", "what", "detail"):
            table.add_column(column, overflow="fold")
        for row in _tail_json(path, max(args.lines, 200)):
            if row.get("kind") == "decision":
                detail = ", ".join(
                    f"{key}={value}" for key, value in row.items()
                    if key not in ("at", "time", "session", "kind", "what")
                    and value not in (None, "", []))
                table.add_row(str(row.get("time", ""))[-8:],
                              str(row.get("what", "")), detail)
            elif row.get("kind") == "atc" and row.get("unprompted"):
                table.add_row(str(row.get("time", ""))[-8:],
                              f"said ({row.get('why', '')})",
                              as_written(str(row.get("text", "")),
                                         figures=config.ui.numerals))
        console.print(table)
        return 0

    if args.what == "crew":
        path = directory / "crew.jsonl"
        if not path.exists():
            console.print(f"[yellow]Nothing from the cabin at {path}.[/yellow]")
            return 1
        table = Table(box=None, pad_edge=False)
        for column in ("time", "what", "cue", "said", "why"):
            table.add_column(column, overflow="fold")
        for row in _tail_json(path, args.lines):
            # The reason is the column worth having, and it is the one that
            # exists whether or not anything was said: a cue that was refused
            # names the switch, a line that was dropped names the flight, and
            # a line that was played names the voice and how loud it was.
            why = str(row.get("why", ""))
            if not why and row.get("said"):
                why = (f"{row.get('seconds', '?')}s, peak "
                       f"{row.get('peak', '?')}, {row.get('voice', '')}").strip(", ")
            if row.get("kind") == "player":
                why = (f"wrote {row.get('wrote', 0):,} of "
                       f"{row.get('expected', 0):,} to {row.get('device')}"
                       + (f" -- {row['error']}" if row.get("error") else ""))
            if row.get("kind") == "music":
                why = why or f"{row.get('rate', '')} Hz"
            if row.get("kind") == "applause" and "fpm" in row:
                why = f"{row['fpm']} fpm: {why}"
            if row.get("kind") == "flight":
                # The header row of a flight: which voices were cast and
                # which switches were on. It is the first thing worth knowing
                # about any of the rows underneath it.
                settings = row.get("settings") or {}
                off = [name for name, on in settings.items() if on is False]
                why = (f"{row.get('language', '')}, "
                       f"{row.get('copilot') or 'cast from the aeroplane'} / "
                       f"{row.get('cabin') or 'cast from the aeroplane'}"
                       + (f"; off: {', '.join(off)}" if off else
                          "; everything on"))
            said = row.get("said")
            if said is None:
                said = row.get("playing") if row.get("kind") in (
                    "music", "applause") else \
                    row.get("fired")
            table.add_row(
                str(row.get("time", ""))[-8:],
                str(row.get("kind", "")),
                str(row.get("cue") or row.get("who") or row.get("label")
                    or row.get("callsign") or ""),
                "" if said is None else ("yes" if said else "no"),
                why,
            )
        console.print(table)
        return 0

    if args.what == "where":
        console.print(f"[bold]{directory}[/bold]")
        for path in sorted(directory.glob("*")):
            if path.is_file():
                console.print(f"  {path.name}  "
                              f"[dim]{path.stat().st_size:,} bytes[/dim]")
        return 0

    path = directory / ("wilcoatc.jsonl" if args.what == "json"
                        else "wilcoatc.log")
    if not path.exists():
        console.print(f"[yellow]No log at {path}.[/yellow]")
        return 1
    if args.what == "json":
        for row in _tail_json(path, args.lines):
            console.print(json.dumps(row, ensure_ascii=False))
        return 0
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in _tail_lines(handle, args.lines):
            console.print(line.rstrip(), highlight=False)
    return 0


def _tail_lines(handle, count: int) -> list[str]:
    """The last ``count`` lines, without holding the whole file in memory."""
    from collections import deque

    return list(deque(handle, maxlen=max(1, count)))


def _tail_json(path, count: int) -> list[dict]:
    import json
    from collections import deque

    rows: list[dict] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in deque(handle, maxlen=max(1, count)):
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def cmd_app(args) -> int:
    """Start the radio in its own window.

    The default way in. Everything runs in this one process and nothing is
    reachable from the network; ``gui`` is the version that serves the same
    panel to a browser or a tablet instead.
    """
    from . import desktop

    config = Config.load(args.config)
    _apply_overrides(config, args)
    logs.configure(config)

    ok, why = desktop.available()
    if not ok:
        console.print(f"[red]{why}[/red]")
        return 1

    def ready(engine, api) -> None:
        if engine is None:
            # Nothing is downloaded yet, so the window has opened on the
            # setup panel. Which is the whole point: the person who needs
            # this message is looking at a window, not at this terminal.
            console.print(Panel(
                "[bold]WilcoATC[/bold] -- first run\n"
                "Nothing has been downloaded yet.\n"
                "Press [bold]Download[/bold] in the window, then start "
                "WilcoATC again.\n"
                f"[dim]or run {command_hint('setup')} here instead[/dim]",
                border_style="yellow",
            ))
            return
        console.print(Panel(
            "[bold]WilcoATC[/bold] -- local ATC for Microsoft Flight Simulator\n"
            "the window is open; close it to stop\n"
            f"push to talk: hold [bold]{config.ptt.key}[/bold] anywhere, "
            "or [bold]space[/bold] in the window",
            border_style="green",
        ))

    try:
        return desktop.run(config, connect_sim=not args.no_sim, on_ready=ready)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        console.print(f"Run [bold]{command_hint('setup')}[/bold] first.")
        return 1
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1


def cmd_gui(args) -> int:
    """Start the engine and serve the panel in a browser."""
    import webbrowser

    from .engine import Engine
    from .web.server import Broadcaster, event_sink, serve, serve_setup

    config = Config.load(args.config)
    _apply_overrides(config, args)
    logs.configure(config)

    broadcaster = Broadcaster()
    url = f"http://{args.host if args.host != '0.0.0.0' else 'localhost'}:{args.port}"
    try:
        engine = Engine(config, NavDB(), event_sink(broadcaster))
    except FileNotFoundError:
        # Nothing is downloaded yet. Serving the setup panel is a better
        # answer than printing "run wilcoatc setup" to a terminal the pilot
        # is not looking at -- they opened a window, so the window is where
        # the answer belongs.
        console.print(Panel(
            "[bold]WilcoATC[/bold] -- first run\n"
            "Nothing has been downloaded yet.\n"
            f"open [bold]{url}[/bold] and press Download\n"
            f"[dim]or run {command_hint('setup')} here instead[/dim]",
            border_style="yellow",
        ))
        if not args.no_browser:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        try:
            serve_setup(config, broadcaster, args.host, args.port)
        except KeyboardInterrupt:
            pass
        return 1

    console.print(Panel(
        "[bold]WilcoATC[/bold] -- local ATC for Microsoft Flight Simulator\n"
        f"panel: [bold]{url}[/bold]\n"
        f"push to talk: hold [bold]{config.ptt.key}[/bold] anywhere, "
        "or [bold]space[/bold] on the page\n"
        "[dim]ctrl-c here to stop[/dim]",
        border_style="green",
    ))

    engine.warm()
    engine.start(connect_sim=not args.no_sim)

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        serve(engine, broadcaster, args.host, args.port)
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
    return 0


def cmd_run(args) -> int:
    """Start the radio."""
    from .engine import Engine

    config = Config.load(args.config)
    _apply_overrides(config, args)
    logs.configure(config)

    try:
        engine = Engine(config, NavDB(), _print_event)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        console.print(f"Run [bold]{command_hint('setup')}[/bold] first.")
        return 1

    console.print(Panel(
        "[bold]WilcoATC[/bold] -- local ATC for Microsoft Flight Simulator\n"
        f"push to talk: [bold]{config.ptt.key}[/bold]   "
        "type [bold]/help[/bold] for commands, [bold]/quit[/bold] to exit",
        border_style="green",
    ))

    engine.warm()
    engine.start(connect_sim=not args.no_sim)

    try:
        _console_loop(engine)
    except (KeyboardInterrupt, EOFError):
        console.print()
    finally:
        engine.stop()
    return 0


HELP = """
[bold]Commands[/bold]
  /say <text>        Speak to the current controller by typing instead of talking
  /tune <mhz>        Check who is on a frequency; sets COM1 only when no
                     simulator is connected (otherwise tune it in the aircraft)
  /atis [ICAO] [en|fr]  Play the ATIS, optionally in a given language
  /freq [ICAO]       Show frequencies near you or at an airport
  /who               Who is listening on the current frequency
  /where             Aircraft position and phase
  /state k=v ...     Set aircraft state by hand (no simulator), e.g.
                     /state altitude_ft=12000 on_ground=False
  /flight k=v ...    Set the flight, e.g. /flight callsign=DAL1234 destination=KBOS
  /stats             How transmissions are being understood
  /help  /quit
"""


def _console_loop(engine) -> None:
    while True:
        try:
            line = console.input("[bold]> [/bold]").strip()
        except (KeyboardInterrupt, EOFError):
            break
        if not line:
            continue
        if not line.startswith("/"):
            engine.emit("pilot", line)
            engine.handle_text(line)
            continue

        command, _, rest = line[1:].partition(" ")
        command = command.lower()
        rest = rest.strip()

        if command in ("quit", "exit", "q"):
            break
        elif command == "help":
            console.print(HELP)
        elif command == "say":
            if rest:
                engine.emit("pilot", rest)
                engine.handle_text(rest)
        elif command == "tune":
            try:
                mhz = float(rest)
            except ValueError:
                console.print("[red]Usage: /tune 121.90[/red]")
                continue
            if hasattr(engine.source, "update"):
                engine.source.update(com1_active=mhz)
                engine.refresh_state()
                engine.tune(mhz)
            else:
                # The simulator owns COM1 and the situation loop follows it, so
                # setting it here would be undone within a quarter second.
                # Say so rather than appearing to work and then reverting.
                console.print(
                    "[yellow]COM1 is driven by the simulator.[/yellow] "
                    f"Tune {mhz:.3f} in the aircraft radio and it will be "
                    "picked up automatically."
                )
                _who_is_on(engine, mhz)
        elif command == "atis":
            # "/atis LFPO fr" or "/atis fr" for the current field.
            words = rest.split()
            language = None
            if words and words[-1].lower() in ("en", "fr"):
                language = words.pop().lower()
            engine.play_atis(words[0].upper() if words else None, language)
        elif command == "freq":
            _show_frequencies(engine, rest.upper())
        elif command == "who":
            station = engine.station
            if station is None:
                console.print("[dim]Nobody is listening on this frequency.[/dim]")
            else:
                console.print(
                    f"{station.callsign}  {station.mhz:.3f}  "
                    f"{station.ident}  {station.distance_nm:.1f} NM"
                )
        elif command == "where":
            _show_position(engine)
        elif command == "stats":
            console.print(engine.understanding.describe())
            console.print(engine.understanding.stats.summary())
        elif command == "state":
            _set_fields(engine.source, rest, engine)
        elif command == "flight":
            _set_flight(engine, rest)
        else:
            console.print(f"[red]Unknown command /{command}. Try /help.[/red]")


def _who_is_on(engine, mhz: float) -> None:
    """Report who would answer on a frequency, without tuning it."""
    state = engine.state
    if not state.has_position:
        console.print("[dim]No aircraft position, so nobody can be resolved.[/dim]")
        return
    station = engine.navdb.resolve_station(
        mhz, state.latitude, state.longitude, state.altitude_ft
    )
    if station is None:
        console.print(f"[dim]Nobody is on {mhz:.3f} here.[/dim]")
    else:
        console.print(
            f"[dim]{mhz:.3f} would be {station.callsign} "
            f"({station.ident}, {station.distance_nm:.0f} NM).[/dim]"
        )


def _show_frequencies(engine, ident: str) -> None:
    db = engine.navdb
    if ident:
        airport = db.airport(ident)
        if airport is None:
            console.print(f"[red]No airport matching {ident!r}.[/red]")
            return
        stations = db.stations(airport.ident)
    else:
        state = engine.state
        if not state.has_position:
            console.print("[dim]No aircraft position available.[/dim]")
            return
        stations = db.all_stations_near(state.latitude, state.longitude, max_nm=40)

    table = Table(show_header=True, header_style="bold")
    table.add_column("Freq", justify="right")
    table.add_column("Station")
    table.add_column("Airport")
    table.add_column("NM", justify="right")
    for station in stations[:40]:
        table.add_row(f"{station.mhz:.3f}", station.callsign, station.ident,
                      f"{station.distance_nm:.0f}" if station.distance_nm else "")
    console.print(table)


def _show_position(engine) -> None:
    state = engine.state
    session = engine.session
    db = engine.navdb
    nearest = (
        db.nearest_airport(state.latitude, state.longitude, max_nm=100)
        if state.has_position else None
    )
    lines = [
        f"position   {state.latitude:.4f}, {state.longitude:.4f}",
        f"altitude   {state.altitude_ft:,.0f} ft"
        + (" (on the ground)" if state.on_ground else ""),
        f"heading    {state.heading_true:.0f}   "
        f"ground speed {state.ground_speed_kt:.0f} kt",
        f"COM1       {state.com1_active:.3f}",
        f"squawk     {state.transponder_code}",
    ]
    if nearest:
        lines.append(
            f"nearest    {nearest.ident} {nearest.name} "
            f"({state.distance_to(nearest.lat, nearest.lon):.1f} NM)"
        )
    if session:
        lines.append(f"phase      {session.phase.value}")
        if session.pending:
            lines.append(f"awaiting readback of {session.pending.kind}: "
                         f"{session.pending.values}")
    console.print(Panel("\n".join(lines), title="Aircraft"))


def _set_fields(source, rest: str, engine) -> None:
    if not hasattr(source, "update"):
        console.print("[yellow]The simulator is driving the aircraft state.[/yellow]")
        return
    fields = {}
    for token in rest.split():
        key, _, value = token.partition("=")
        if not value:
            continue
        fields[key] = _coerce(value)
    if not fields:
        console.print("[red]Usage: /state altitude_ft=12000 on_ground=False[/red]")
        return
    try:
        source.update(**fields)
    except AttributeError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    engine.refresh_state()
    console.print(f"[dim]set {', '.join(f'{k}={v}' for k, v in fields.items())}[/dim]")


def _set_flight(engine, rest: str) -> None:
    fields = {}
    for token in rest.split():
        key, _, value = token.partition("=")
        if value:
            fields[key] = _coerce(value)
    for key, value in fields.items():
        if hasattr(engine.config.flight, key):
            setattr(engine.config.flight, key, value)
        else:
            console.print(f"[red]Unknown flight field {key!r}.[/red]")
            return
    engine.begin_session()


def _coerce(value: str):
    low = value.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _apply_overrides(config: Config, args) -> None:
    if args.callsign:
        config.flight.callsign = args.callsign
    if args.aircraft:
        config.flight.aircraft_type = args.aircraft
    if args.departure:
        config.flight.departure = args.departure.upper()
    if args.destination:
        config.flight.destination = args.destination.upper()
    if args.ptt:
        config.ptt.key = args.ptt
    if args.no_sim:
        config.sim.enabled = False
    if args.no_radio_effects:
        config.audio.radio_effects = False


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wilcoatc",
        description="Local air traffic control for Microsoft Flight Simulator.",
    )
    parser.add_argument("--config", default=str(CONFIG_PATH),
                        help="path to config.yaml")
    # The same number the executable carries in its version resource, so a
    # report that says "Defender ate 1.0.0" names a build that can be found.
    parser.add_argument("--version", action="version",
                        version=f"WilcoATC {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser(
        "console", aliases=["run"],
        help="start the radio in the terminal, with no window",
    )
    for target in (run, parser):
        target.add_argument("--callsign", help="e.g. DAL1234 or N172SP")
        target.add_argument("--aircraft", help="ICAO type, e.g. B739")
        target.add_argument("--departure", help="departure ICAO")
        target.add_argument("--destination", help="destination ICAO")
        target.add_argument("--ptt", help="push-to-talk key")
        target.add_argument("--no-sim", action="store_true",
                            help="do not connect to the simulator")
        target.add_argument("--no-radio-effects", action="store_true",
                            help="play voices without the radio effect chain")
    run.set_defaults(func=cmd_run)

    setup = sub.add_parser("setup", help="download nav data, voices and models")
    setup.add_argument("--force", action="store_true", help="re-download everything")
    setup.add_argument("--hq", action="store_true",
                       help="also fetch the voice-cloning model (2 GB; needs "
                            "a card, or patience)")
    setup.add_argument("--all-voices", action="store_true",
                       help="download the full voice set rather than the core set")
    setup.set_defaults(func=cmd_setup)

    app = sub.add_parser("app", help="start the radio in its own window (default)")
    app.set_defaults(func=cmd_app)

    gui = sub.add_parser(
        "gui",
        help="serve the same panel to a browser, for a second screen or a tablet",
    )
    gui.add_argument("--host", default="127.0.0.1",
                     help="0.0.0.0 to reach it from a tablet on the same network")
    gui.add_argument("--port", type=int, default=8787)
    gui.add_argument("--no-browser", action="store_true",
                     help="do not open a browser window")
    for target in (app, gui):
        target.add_argument("--callsign")
        target.add_argument("--aircraft")
        target.add_argument("--departure")
        target.add_argument("--destination")
        target.add_argument("--ptt")
        target.add_argument("--no-sim", action="store_true")
        target.add_argument("--no-radio-effects", action="store_true")
    gui.set_defaults(func=cmd_gui)

    doctor = sub.add_parser("doctor", help="check the installation")
    doctor.set_defaults(func=cmd_doctor)

    devices = sub.add_parser("devices", help="list audio and controller devices")
    devices.set_defaults(func=cmd_devices)

    sound = sub.add_parser("sound-test",
                           help="play a tone and a controller transmission")
    sound.add_argument("--airport", default="KJFK")
    sound.set_defaults(func=cmd_sound_test)

    freq = sub.add_parser("freq", help="show, check or correct frequencies")
    freq.add_argument("airport", nargs="?",
                      help="the airport to show; omit with --audit")
    freq.add_argument("--add", nargs=2, metavar=("POSITION", "MHZ"),
                      help="record a frequency the published data lacks, "
                           "e.g. --add GND 123.605")
    freq.add_argument("--name", help="what the controller is called on it")
    freq.add_argument("--note", help="where you got it from")
    freq.add_argument("--from-sim", action="store_true",
                      help="take the airport's frequencies from the running "
                           "simulator, replacing the published ones")
    freq.add_argument("--audit", action="store_true",
                      help="check every frequency in the database")
    freq.add_argument("--fix", action="store_true",
                      help="with --audit, put unselectable channels back on "
                           "the grid")
    freq.set_defaults(func=cmd_freq)

    brief = sub.add_parser("simbrief", help="read the latest SimBrief plan")
    brief.add_argument("user", nargs="?",
                       help="SimBrief username or pilot ID; "
                            "defaults to simbrief.username")
    brief.add_argument("--save", action="store_true",
                       help="write the flight into config.yaml")
    brief.set_defaults(func=cmd_simbrief)

    gsx = sub.add_parser("gsx", help="what the ground handling is doing")
    gsx.set_defaults(func=cmd_gsx)

    atis = sub.add_parser("atis", help="generate an airport's ATIS")
    atis.add_argument("airport")
    atis.add_argument("--play", action="store_true", help="also speak it")
    atis.set_defaults(func=cmd_atis)

    online = sub.add_parser(
        "online", help="who is on VATSIM or IVAO right now")
    online.add_argument("airport", nargs="?", default="",
                        help="only this field, by ICAO code")
    online.add_argument("--provider", default="",
                        help="vatsim or ivao, overriding the config")
    online.set_defaults(func=cmd_online)

    logs_cmd = sub.add_parser(
        "logs", help="read the log, or the radio transcript, back")
    logs_cmd.add_argument(
        "what", nargs="?", default="radio",
        choices=("radio", "arrival", "crew", "app", "json", "where"),
        help="radio: the transcript with what the parser made of each call; "
             "arrival: why the controllers did what they did unprompted -- "
             "approach data, descents, clearances, go-arounds, weather "
             "source -- beside what they said; "
             "crew: why the cabin said what it said, and why it said nothing; "
             "app: the application log; json: the same as JSON lines; "
             "where: which files exist and how big they are")
    logs_cmd.add_argument("-n", "--lines", type=int, default=40,
                          help="how many of the most recent lines to show")
    logs_cmd.add_argument("--language", default="",
                          help="only transmissions in this language")
    logs_cmd.set_defaults(func=cmd_logs)

    say = sub.add_parser("say", help="speak one transmission, to audition a voice")
    say.add_argument("text")
    say.add_argument("--airport", required=True)
    say.add_argument("--position", default="TWR",
                     choices=["DEL", "GND", "TWR", "APP", "DEP", "CTR", "ATIS"])
    say.add_argument("--save", help="write a WAV file instead of playing")
    say.set_defaults(func=cmd_say)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        args.command = "app"
        args.func = cmd_app
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
