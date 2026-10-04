<p align="center">
  <img src="logo.png" alt="WilcoATC" width="160">
</p>

<h1 align="center">WilcoATC</h1>

<p align="center">
  <b>Local, voice-driven air traffic control for Microsoft Flight Simulator 2024 (and 2020).</b><br>
  Free, open source, CPU-only, no account, no subscription.
</p>

---

You hold a push-to-talk key, speak to whichever frequency is tuned in COM1, and
a controller answers you in a consistent voice with correct phraseology.
Everything runs on your own computer. Apart from an optional weather fetch,
nothing goes over the network during a flight.

```
YOU   Kennedy Ground, Delta twelve thirty four, ready to taxi.
ATC   Delta twelve thirty four, Kennedy Ground, runway three one left,
      taxi via Uniform, Zulu, Delta, hold short of runway four left.
YOU   Runway three one left, hold short of four left, Delta twelve thirty four.
ATC   Delta twelve thirty four, readback correct.
```

> **Status: alpha.** It works end to end, but you will find bugs, and
> sometimes the controller will misunderstand you or say the wrong thing. This
> is a simulator add-on, so don't use it to prepare for real flights.

This README is the overview. **The full user manual, covering every feature
in detail, is in [`docs/MANUAL.md`](docs/MANUAL.md).** Developer notes and
project conventions are in [`CLAUDE.md`](CLAUDE.md).

---

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Quick start (from source)](#quick-start-from-source)
- [Using it](#using-it)
- [Command reference](#command-reference)
- [Configuration](#configuration)
- [How it works](#how-it-works)
- [Project layout](#project-layout)
- [Building the Windows executable](#building-the-windows-executable)
- [Windows Defender says it is a virus](#windows-defender-says-it-is-a-virus)
- [Tests](#tests)
- [Contributing](#contributing)
- [Licence and data](#licence-and-data)

---

## Features

**Real frequencies that match your radio.** About 25,000 published ATC
frequencies at more than 72,000 airports, limited to the VHF airband so it
never offers a frequency you can't tune in COM1. Tune 121.90 at Kennedy and
Kennedy Ground answers. Tune the same frequency at Heathrow and Heathrow Ground
answers. Frequencies are matched as *channels*, so the 121.605 an 8.33 kHz
radio displays finds the published 121.600. When the simulator is connected,
the frequencies come from its own facility data, so they match its scenery.

**Phraseology comes from templates, not from a language model.** Every word a
controller says comes from a fixed template based on FAA JO 7110.65, the AIM,
ICAO Doc 4444 and SERA/national AIPs. Controller speech can't drift or invent
instructions. US and ICAO dialects differ where the real ones do ("climb and
maintain … altimeter" against "climb to … QNH").

**Consistent voices.** Each controller's voice comes from a stable hash of
facility and position. Kennedy Tower sounds like the same person on every
flight, while one flight still hears different people at Delivery, Ground,
Tower, Departure and Center. Accents follow the region: Frankfurt Ground has a
German accent and Orly Tower a French one.

**A realistic VHF radio.** The audio passes through a measured radio chain:
band limiting, AGC, transmitter overdrive, cockpit-speaker resonance, squelch,
key clicks, and a noise floor that grows with distance. It was tuned against
real recordings of Heathrow Director.

**Several languages.** The controller works in English, French, German,
Spanish, Italian and more, and follows the airspace the aircraft is in. At a
bilingual field such as Orly you can speak French or English, and the
controller replies in the language you used. A foreign controller's English
carries their accent, which is built phoneme by phoneme.

**Other traffic.** Airlines that actually operate at your field, each crew
speaking the language they would really use there, share your runway and
frequency. They are drawn on the map and spawned in the simulator, and they
taxi along the airport's real stands and taxiways.

**A full flight.** Clearance delivery, taxi, takeoff, departure, en-route
centre sectors, arrival, the approach your runway actually has (ILS, RNAV,
non-precision or visual), landing, and taxi in. There is also VFR support,
handoffs, readback checking, go-arounds, ATIS, uncontrolled fields, and
VATSIM/IVAO awareness (it stands down where a real controller is online).

**An optional crew.** A first officer on the interphone with callouts and
checklists, and a cabin crew with announcements on their own audio path. The
cabin always waits for the radio, and the radio never waits for the cabin. You
can also use your own recorded announcement packs (the Fenix A320 layout).

**Integrations.** SimBrief flight plans, GSX ground services, the simulator's
own AI traffic and FSLTL, real-world METARs from NOAA, and a toolbar panel
inside MSFS ([`msfs/`](msfs/README.md)).

**An interface in 41 languages.** A desktop window with Comms, Airspace (a 2D
chart that becomes a 3D airport model as you zoom in), Frequencies and
Settings. You can also serve the same panel to a browser or tablet, or run
everything in a terminal.

**Optional AI understanding.** If you phrase something in a non-standard way,
a local model (Ollama) or the Claude API can map it to an intent. It
classifies only and never writes controller speech. It is off by default.

---

## Requirements

| | |
|---|---|
| OS | Windows 10/11 for the simulator link. Everything except the sim link also runs without a simulator. |
| Simulator | Microsoft Flight Simulator 2024 or 2020 (optional for testing) |
| Python | **3.11 or 3.12** (not 3.13+ yet, because the speech-recognition wheels lag behind) |
| Disk | About 1.5 GB for models and data, 2.5 GB with every accent and the multilingual recogniser |
| GPU | **Not needed.** All inference runs on the CPU, because the simulator is using the GPU. |
| Other | A microphone. The window uses Edge WebView2, which Windows 10/11 already include. |

Pilots using a release build need none of this: they unzip the folder and run
`WilcoATC.exe`. Python is only needed to run from source or to build.

---

## Quick start (from source)

```powershell
git clone https://github.com/<you>/wilcoatc.git
cd wilcoatc

python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# Download navigation data, controller voices and the speech recogniser
.venv\Scripts\python -m wilcoatc setup

# Check that everything works
.venv\Scripts\python -m wilcoatc doctor
.venv\Scripts\python -m wilcoatc sound-test

# Fly
.venv\Scripts\python -m wilcoatc
```

`setup` is optional. On a first run with nothing downloaded, the window opens
on a setup panel with one **Download** button and a progress bar for each
component. The same controls are in **Settings → Components** later.

The downloads are model weights and data only. No code is ever fetched at
runtime. Optional extras:

```powershell
.venv\Scripts\python -m wilcoatc setup --all-voices   # extra controller voices (Supertonic, Piper accents)
.venv\Scripts\python -m wilcoatc setup --hq           # voice-cloning tier, ~2 GB, needs requirements-optional.txt
```

---

## Using it

1. Start the simulator and load your aircraft.
2. Start WilcoATC. It reads your position, callsign, aircraft type and radios
   from SimConnect.
3. Tune a frequency in COM1. The **Frequencies** screen lists every station
   near you.
4. Hold the push-to-talk key (backtick `` ` `` by default, or a joystick
   button), say your transmission, and release.
5. Read back what you are told. The controller checks your readback.

The simulator can't report your destination or filed cruise altitude. Load a
SimBrief plan, set them in `config.yaml`, or pass them on the command line
(`--destination KBOS`).

You can type instead of talking. This is useful for testing and needs no
microphone:

```
> /say Kennedy Ground, Delta twelve thirty four, ready to taxi
> /atis KJFK
> /freq          stations within 40 NM
> /who           who is listening on this frequency
> /where         position, phase, outstanding readbacks
> /help
```

---

## Command reference

| Command | What it does |
|---|---|
| `python -m wilcoatc` | The desktop window (same as `app`) |
| `python -m wilcoatc console` | The radio in the terminal, no window |
| `python -m wilcoatc gui [--host 0.0.0.0]` | Serve the panel to a browser or tablet at `http://<pc>:8787`. No password, so only use it on a network you trust. |
| `python -m wilcoatc setup [--all-voices] [--hq] [--force]` | Download nav data, voices and recogniser |
| `python -m wilcoatc doctor` | Check audio, recogniser, voices and the simulator link |
| `python -m wilcoatc sound-test` | Play a tone, a controller and the crew |
| `python -m wilcoatc say "..." --airport KJFK --position TWR` | Audition a controller's voice |
| `python -m wilcoatc freq LFPG` | Every published frequency at a field |
| `python -m wilcoatc logs radio` | The transcript, with what the parser made of each line |
| `python -m wilcoatc logs arrival` | Why the arrival was worked as it was |
| `python -m wilcoatc logs crew` | Why the crew said (or didn't say) something |

In a release build, replace `python -m wilcoatc` with `WilcoATC-console.exe`.

---

## Configuration

Settings are in [`config.yaml`](config.yaml). It is documented inline, every
value is optional, and most settings can also be changed from the
**Settings** screen. The most useful ones:

| Setting | Default | Purpose |
|---|---|---|
| `ptt.key` | `grave` | Push-to-talk key |
| `speech.languages` | `[]` | Empty follows the aircraft; `[en]` pins English and uses the faster English-only recogniser |
| `speech.model` | `small.en` | `medium` if your CPU has headroom |
| `atc.dialect` | `auto` | Force `faa` or `icao` |
| `simbrief.username` | `''` | Your SimBrief username or pilot ID |
| `flight.callsign`, `flight.destination`, `flight.cruise_altitude_ft` | | Flight details the simulator can't provide |
| `traffic.enabled` / `traffic.density` | `true` / `1.0` | Other aircraft on frequency |
| `immersion.enabled` | | First officer and cabin crew |
| `network.provider` | `none` | `vatsim` or `ivao`, to stand down where a real controller is online |
| `ai.provider` | `none` | `ollama` or `anthropic` for non-standard phrasing |
| `voice.engine` | `auto` | `kokoro`, `supertonic`, `piper`, or `chatterbox` (opt-in cloning tier) |
| `voice.seed` | `wilcoatc` | Change it to reshuffle every controller's voice |
| `audio.radio_effects` | `true` | `false` for the raw synthesised voice |

The full table is in [the manual](docs/MANUAL.md#configuration).

Paths in `config.yaml` are relative to the **data root**: the folder next to
the executable, or `%LOCALAPPDATA%\WilcoATC` when that folder is read-only.

---

## How it works

```
push to talk ─> microphone ─> faster-whisper (CPU) ─> intent parser (rules, 6 languages)
                                                            │   └─ optional AI fallback
                                              nav database: who is on this frequency, here
                                                            │
                                    controller logic  <──  other traffic, weather, sim state
                                                            │
                                          phraseology templates (FAA / ICAO / SERA)
                                                            │
                               voice casting (stable per facility) ─> prosody ─> TTS
                                                            │
                                 imperfections ─> VHF radio chain ─> speakers
```

| Stage | Technology |
|---|---|
| Speech recognition | [faster-whisper](https://github.com/SYSTRAN/faster-whisper), int8 on CPU, primed with aviation vocabulary for each language |
| Understanding | A rule-based grammar. Anything unrecognised gets "say again" rather than a guess. There is an optional, schema-validated LLM fallback. |
| Speech synthesis | Supertonic 3, Kokoro-82M and Piper, in that order of preference (all ONNX, CPU). Chatterbox voice cloning is an opt-in GPU tier. |
| Accents | Phoneme substitution into the target voice's own inventory |
| Radio | A custom DSP chain (`wilcoatc/audio/radio_fx.py`) measured against real air-band recordings |
| Sim link | SimConnect |
| Interface | FastAPI + pywebview (Edge WebView2), three.js for the 3D airport view, bundled for offline use |
| Navigation data | OurAirports, VATSpy, VatGlasses sectors, the simulator's facility data |

On a modern desktop CPU, recognition runs at about 0.27× real time and
synthesis at 0.02–0.18×. A typical exchange gets an answer in about a second
and a half.

---

## Project layout

| Path | Contents |
|---|---|
| `wilcoatc/engine.py` | The object that owns a flight: state, controllers, transmissions in and out |
| `wilcoatc/cli.py` | Every subcommand |
| `wilcoatc/config.py` | `config.yaml` as dataclasses |
| `wilcoatc/paths.py` | Bundle root vs. data root |
| `wilcoatc/install.py`, `health.py` | First-run downloads, `doctor` checks |
| `wilcoatc/atc/` | Phraseology, intent parsing, controllers, traffic, readbacks, the crew |
| `wilcoatc/audio/` | Recogniser, synthesiser backends, voice casting, prosody, radio effects, playback |
| `wilcoatc/navdata/` | Frequency/airport database, magnetic variation model, terrain grid |
| `wilcoatc/sim/` | SimConnect bridge |
| `wilcoatc/web/` | FastAPI server and the panel, with 41 translations in `static/i18n/` |
| `wilcoatc/llm/` | Optional AI understanding layer |
| `wilcoatc/integrations/` | SimBrief, GSX, VATSIM/IVAO, AI traffic, simulator facility data |
| `msfs/` | The in-sim toolbar panel and its installer |
| `scripts/` | Demos, benchmarks and data/asset builders (world map, terrain grid, icons, printed phrasebook) |
| `tests/` | The test suite |
| `data/navdata/` | Hand-maintained corrections: overrides, preferred runways, minimum altitudes, ICAO callsigns |
| `docs/MANUAL.md` | The full user manual |
| `build.ps1`, `wilcoatc.spec` | The PyInstaller build |

Downloaded assets (`data/voices/`, `data/models/`, the nav database) and
runtime output (`logs/`, `out/`) are excluded by `.gitignore`. `setup`
recreates them.

---

## Building the Windows executable

Windows only, because PyInstaller can't cross-compile.

```powershell
.venv\Scripts\python -m pip install -r requirements-build.txt
.\build.ps1          # -> dist\WilcoATC\  (WilcoATC.exe, WilcoATC-console.exe, _internal\)
.\build.ps1 -Zip     # also produces the release archive
```

The build doesn't include voices or recogniser weights. The program downloads
them on first run. Other switches: `-RebuildBootloader` (compiles
PyInstaller's bootloader locally, which needs MSVC) and `-Sign <thumbprint-or-pfx>`
(Authenticode signing).

---

## Windows Defender says it is a virus

It is a false positive. The verdicts people report (`Trojan:Win32/Wacatac`,
`Wacapew`, `Sabsik`) come from machine-learning classifiers, not signatures. An
unsigned PyInstaller program that installs a global keyboard hook (for
push-to-talk), reads the microphone, and downloads hundreds of megabytes on
first run looks like a keylogger to a classifier, even though it is a radio.

The build does what it can: no UPX, a full version resource, `asInvoker`, an
optional rebuilt bootloader, and nothing executable is ever downloaded. Only a
code-signing certificate (`.\build.ps1 -Sign …`) really ends the problem.

If it happens to you:

1. **Report it** at
   [microsoft.com/wdsi/filesubmission](https://www.microsoft.com/wdsi/filesubmission)
   as "Incorrectly detected as malware". The correction reaches everyone.
2. **Restore it** from Windows Security → Protection history.
3. **Check it first** at [virustotal.com](https://www.virustotal.com). A few
   generic `PyInstaller`/`heuristic` hits are normal. Thirty engines naming the
   same trojan are not, and mean you didn't get the file from this project's
   releases.
4. Only then **exclude the folder**, and never a whole drive.

The full explanation is in [the manual](docs/MANUAL.md#windows-defender-says-it-is-a-virus).

---

## Tests

```powershell
.venv\Scripts\python -m pytest tests -q
```

About 2,300 tests, and they run fast. Run all of them before calling a change
done. Among other things, they check:

- published frequencies at real airports, and channel matching for 8.33 kHz radios
- that every value a controller can say round-trips through the parser in five
  languages (13,400 values)
- that voices are stable per facility and distinct per position
- a full **consistency sweep** across positions × phases × intents × languages
  (for example, no taxi instructions at cruise, no takeoff clearance from the
  wrong frequency, and a mayday is always answered)
- that the radio chain hits its measured targets
- that all 41 interface translations are complete and not left in English

Some tests need the downloaded navigation data or voices. Run `setup` first.

---

## Contributing

Issues and pull requests are welcome. Please read [`CLAUDE.md`](CLAUDE.md)
first. It contains the project's rules, the most important of which are:

- **CPU first.** No mandatory GPU dependency, because the simulator is using the GPU.
- **Pilots never type commands.** No user-facing message may tell a pilot to
  run a Python command. Use `wilcoatc.paths.command_hint(...)` or name a button.
- **Only models and data are downloaded at runtime**, never code.
- **Every user-facing string is translated**: a key in
  `wilcoatc/web/static/i18n/en.json` and in all 40 other catalogues
  (enforced by `tests/test_interface.py`).
- **Don't modify the radio chain** (`audio/radio_fx.py`) or **voice casting**
  without a measured reason. Changing the casting re-casts every controller
  everywhere.
- **Comments say why, not what**, and test names are sentences.

---

## Licence and data

The WilcoATC code is free to use, modify and redistribute. Third-party data
and models keep their own terms:

| Component | Licence |
|---|---|
| OurAirports | Public domain |
| VATSpy boundary data | CC BY-SA 4.0 |
| [VatGlasses](https://github.com/lennycolton/vatglasses-data) enroute sectors | CC BY-NC-SA 4.0. Downloaded at setup into its own folder and never bundled. |
| Natural Earth (map outlines) | Public domain |
| NOAA weather | US government work |
| Kokoro-82M | Apache-2.0 |
| Piper voices, Supertonic | See their model cards on Hugging Face |
| three.js | MIT |
| WMM2025 coefficients | NOAA/NCEI, public domain |

No recorded cabin announcements are included. Your sound packs stay yours. The
folder layout follows
[Fenix Simulations' documented format](https://kb.fenixsim.com/cabin-announcements).

*This is a simulator toy. Don't use it to prepare for a real flight, and don't
treat its phraseology as current published procedure.*
