# The toolbar panel

A button in the simulator's own toolbar, next to GSX and the camera controls,
that opens WilcoATC without leaving the cockpit view.

```
python msfs/install_panel.py
```

Then restart the simulator. The button is a control tower, in the toolbar.

## What it shows

A dark card in the register of the simulator's own 2024 interface: neutral
surfaces, hairline edges, one typeface, and colour only where it means
something. Green is the microphone open; amber is the controller waiting on
you. Top to bottom:

- **The radio.** COM1 in large figures, with the station that answers on it
  and how far away it is. Standby beside it, and the swap between them, which
  swaps the radio in the aircraft too.
- **The field.** ATIS letter (tap it to hear the broadcast), QNH, wind as the
  METAR writes it (`050° 12G22`), and your squawk.
- **The clearance.** Phase of flight, the last thing you were cleared to do,
  and your assigned altitude. It turns amber, and says *Read back*, when an
  instruction is waiting for your readback.
- **The conversation.** The last four transmissions: the controller on the
  left, you on the right, other traffic and the crew small and grey between.
- **Quick replies.** The phrases the phase of flight calls for, the same ones
  the desktop window offers. Tap one and it is transmitted as if spoken.
- **Text or voice, side by side.** A chat box at the foot: type a
  transmission and press Enter (or the send key), or hold the microphone
  beside it and speak. Typed and spoken go through the same parser and are
  answered the same way, so you can switch mid-exchange. The microphone is
  held rather than toggled, the way the key on the yoke is; it goes green
  while open and grey while the controller is speaking. Escape gives the
  keyboard back to the aircraft.

While the box has focus, the simulator is told an input field has the
keyboard (`FOCUS_INPUT_FIELD`, then `UNFOCUS_INPUT_FIELD` on blur, as
FlyByWire's EFB does). Without that, the letters you type would also reach the
simulator's key bindings.

After a landing, the touchdown card (rate, grade, bounces, stability, how early
or late) comes up over it for a minute, or until tapped.

It replaced a panel built as a copy of the weather panel beside it -- a blue
ground, a white dial, two lozenges -- which borrowed the simulator's look so
faithfully that the one thing you open it for, the frequency, was a small label
on the edge of a compass.

The browser inside the simulator is Coherent GT, and its version moves between
builds. So the page keeps to what every build has: ES5, XMLHttpRequest,
flexbox without `gap`, no grid, no web fonts. `tests/test_toolbar.py` holds it
to that.

## How it talks to the application

It does not contain any air traffic control. The phraseology, the voices, the
speech recognition and the transcript all live in the application; the panel
polls two endpoints twice a second and sends a handful of commands back. Anything
duplicated here would one day disagree with what the radio was actually doing.

    GET  /api/state              what is tuned, the weather, the clearance, the mic
    GET  /api/transcript         the last few transmissions
    POST /api/ptt/start, /stop   the microphone
    POST /api/swap               swap active and standby, in the aircraft too
    POST /api/say                a quick reply, as though it had been spoken
    POST /api/atis               play the ATIS

The application serves its own window on a port the operating system picks,
because nothing else has to find it. The panel does have to find it, and it
runs inside the simulator where it cannot be told a number that changes every
launch — so it gets a fixed one:

```yaml
toolbar:
  enabled: true
  port: 8787
```

Change the port there and change `PORT` at the top of `WilcoATC.js` to match.
Loopback only, like the rest of the application.

## What is in here

```
package/                     the Community package, as installed
  manifest.json              what the simulator reads first
  InGamePanels/
    wilcoatc.spb             the compiled descriptor: what puts the button there
    wilcoatc.xml             the same thing uncompiled, for builds that read it
  html_ui/
    InGamePanels/WilcoATC/   the panel itself
    icons/toolbar/           the button's icon, and again under Textures/Menu
source/wilcoatc.xml          the descriptor's source
install_panel.py             copies the package in and writes its layout.json
```

`layout.json` is not written by hand and is not kept here. The simulator reads
a package *through* that list rather than by walking the folder, so an entry
whose path or size does not match the file on disk is a package that appears to
be present and loads nothing — which is exactly the state this one was found
in, with every path lowercased and every file mixed case. The installer writes
it from what it actually copied, so it cannot drift.

## If the button does not appear

1. The package has to be in the folder the simulator is actually reading.
   `install_panel.py` finds that from `UserCfg.opt` rather than guessing;
   `--check` prints where it would go without touching anything.
2. The simulator reads packages at startup. A restart is required, not a
   flight reload.
3. `wilcoatc.spb` is what registers the button, and it is compiled. If it ever
   needs rebuilding, `source/wilcoatc.xml` is the input and the SDK's
   `fspackagetool` is what compiles it. The `Id` in it must match the
   `panel-id` attribute in `WilcoATC.html`: the toolbar knows the button by
   that id and the page answers to it.
