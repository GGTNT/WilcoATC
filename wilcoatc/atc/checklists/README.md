# The first officer's checklists

The first officer runs challenge-and-response checklists with you: they read
an item, you answer ("check", "set", "on", "three green", "seven thousand"),
and where the simulator can tell, they check your answer against the
aeroplane ("landing light... I show it off").

## Which file is used

One file per class of aeroplane ships here:

| File | Aeroplanes |
| --- | --- |
| `piston_single.json` | C172, DA40, SR22, PA28, Bonanza... |
| `piston_twin.json` | DA62, Baron, Seneca... |
| `turboprop_single.json` | TBM, PC-12, Caravan... |
| `turboprop_twin.json` | King Air, ATR, Dash 8... |
| `business_jet.json` | Citation, Phenom, Learjet, Vision Jet... |
| `airliner.json` | A320, 737, 777, E-Jets, CRJ... |

Helicopters get callouts only, no checklists, for now.

The class comes from the simulator first (its engine type and number of
engines), then from the type's ICAO designator; `logs crew` says which.

**Your own:** put a file in the `checklists` folder beside `config.yaml`:

- `C172.json` (the ICAO type designator) for one aeroplane, or
- `piston_single.json` for every aeroplane of that class.

What wins, from the bottom up: the file here, your class file, your type
file. A file replaces each checklist it names **as a whole** and leaves the
others alone, so your `C172.json` can hold just `before_takeoff` and the
other checklists still come from the class. Set a checklist to `null` to
remove it.

A file is read again as soon as you save it: no restart. A mistake in it is
reported in `logs crew` (a `checklist_file` line) and never stops the
program. An item with a bad check keeps its words and loses only the check.

## The format

```json
{
  "_comment": "Anything starting with an underscore is ignored.",
  "source": "Where the flows come from: the POH, the FAA Airplane Flying Handbook...",
  "checklists": {
    "before_takeoff": {
      "title": {"en": "before takeoff", "fr": "avant décollage"},
      "rules": "any",
      "items": [
        {
          "challenge": {"en": "Flaps", "fr": "Volets"},
          "response": {"en": "set for takeoff", "fr": "position décollage"},
          "accept": ["set", "check", "value"],
          "check": {"var": "flaps_index", "op": "eq", "value": 1}
        },
        {
          "challenge": "Transponder",
          "response": "set",
          "accept": ["set", "on", "value"],
          "rules": "vfr",
          "check": {"var": "transponder_code", "op": "eq", "value": "$squawk"}
        },
        {
          "challenge": "Parking brake",
          "response": "set",
          "accept": ["set", "on"],
          "check": {"var": "parking_brake", "op": "eq", "value": true},
          "note": "UNVERIFIED: whether it belongs in this checklist on this type"
        }
      ]
    },
    "cruise": null
  }
}
```

### A checklist

| Key | | |
| --- | --- | --- |
| `title` | words | What the first officer calls it: "before takeoff checklist". |
| `rules` | `any`, `vfr`, `ifr` | Which flights it is for. Default `any`. |
| `items` | list | In the order they are read. |

The checklist names the first officer knows: `before_start`, `after_start`,
`taxi`, `before_takeoff` (the run-up, for a piston), `after_takeoff`,
`cruise`, `descent`, `approach`, `before_landing`, `after_landing`,
`shutdown`. You may add others; you start them by name.

### An item

| Key | | |
| --- | --- | --- |
| `challenge` | words | What the first officer reads. Required. |
| `response` | words | The answer expected, as you would say it. Default "check". |
| `accept` | list | What kinds of answer count: `check`, `set`, `on`, `off`, `up`, `down`, `green`, `complete`, `value` (a figure: "seven thousand", "one zero one three"). Default `["check"]`. |
| `rules` | `any`, `vfr`, `ifr` | Read only on that kind of flight. Default `any`. |
| `check` | object | How to verify your answer in the simulator. Optional. |
| `note` | text | Why the item is there; `UNVERIFIED: ...` where it is not sure it belongs. |

*Words* are either one string (English) or one per language,
`{"en": "Flaps", "fr": "Volets", "de": "Klappen"}`. A language with no
entry is read in English.

### A check

| Key | | |
| --- | --- | --- |
| `var` | name | A variable the program reads off the simulator (below). |
| `op` | `eq`, `ne`, `in`, `min`, `max`, `near` | Equal, not equal, one of a list, at least, at most, within `tolerance`. Default `eq`. |
| `value` | number, text, `true`/`false`, a list, or a reference | What the variable should be. Default `true`. |
| `tolerance` | number | For `near`. |

A `value` can name what the controller gave you, or what you just said:
`$qnh`, `$squawk`, `$altitude`, `$heading`, `$frequency`, `$runway`, `$said`.

The variables are the fields of the program's aircraft state
(`wilcoatc/sim/state.py`). The ones read off the simulator for a check:

| Variable | What it is |
| --- | --- |
| `parking_brake`, `gear_down`, `flaps_index` | the brake, the gear handle, the flaps handle's notch (0 up) |
| `landing_light`, `taxi_light`, `nav_light`, `strobe_light`, `beacon_light` | the lights' switches |
| `transponder_code`, `transponder_state` | the code; the mode, 0 off, 1 standby, 2 test, 3 on, 4 ALT |
| `kohlsman_hpa` | the altimeter setting, hectopascals (inches are converted) |
| `fuel_selector` | the simulator's tank number, 0 off |
| `mixture_pct` | engine 1's mixture lever, per cent |
| `autopilot`, `gps_drives_nav1`, `pitot_heat`, `spoilers_armed` | on or off |
| `avionics_master`, `battery_master`, `seatbelt_sign`, `engines_running` | on or off |
| `com1_active`, `com1_standby`, `com2_active`, `com2_standby` | megahertz |

A variable that is not in the state is reported as a problem and the item
is read without a check.

## Study-level add-ons: `types/`

Many add-ons (Fenix, PMDG, the Aerosoft CRJ, FlyByWire...) drive their own
switches rather than the simulator's, so the cockpit can show the landing
light on while the simulator's variable says off. Their files are in
`types/` here, one per add-on:

```json
{
  "match": ["fenix"],
  "no_check": ["landing_light", "strobe_light", "parking_brake"]
}
```

`match` is fragments of the simulator's title for the model (any one of
them, upper or lower case); `no_check` the variables whose checks are taken
off the shipped items for it. The item is still read and answered; only
the comparison with the simulator goes. A file may carry `checklists` too,
in the format above. Your own class or type file comes after it and may
check those variables again.

What these add-ons publish is not measured, so their files leave
everything but the gear and flap handles unchecked. Every disagreement the
first officer sees -- objected to or not -- is a `mismatch` line in
`logs crew` with the variable, the value read, your answer and the
aircraft's title. Those lines are how the list grows.

## How the first officer runs them

- **Start one** by saying its name: "before takeoff checklist". Or address
  the first officer -- "first officer", "copilot", or the name they have --
  first: "copilot, after landing checklist".
- **Answer** each item the way it is written: "set", "on", "three green",
  "one zero one three". "Check" is always accepted.
- "**Say again**", "**skip**", "**hold**", "**continue**", "**from the
  top**" do what they say.
- A bare answer reaches the first officer only while an item is waiting,
  no readback is owed to the controller, and the first officer spoke after
  the controller did. Anything else goes to the radio as before.
- Where the simulator disagrees with your answer, the first officer says
  so once ("landing light... I show it off"). Put it right, answer again,
  or say "continue": the item is left open and the checklist ends with how
  many are.
- No answer: the item is read once more, then the checklist is held until
  you say "continue". Nothing is said while the radio is live; an item the
  radio talked over is read again.
- The first officer offers the checklist that belongs to the moment, once
  a flight, never twice in two minutes, never below 500 ft or on short
  final, and never over the radio or the cabin.

Everything the first officer says or does not say is in `logs crew`, as
`checklist` lines.
