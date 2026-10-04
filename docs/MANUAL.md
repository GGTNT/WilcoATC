# WilcoATC

Local air traffic control for Microsoft Flight Simulator 2024. A desktop
application: you hold a push-to-talk key, speak to the frequency you have tuned
in COM1, and a controller answers in a consistent voice with correct
phraseology.

Everything runs on your machine. There is no account, no subscription, and no
network dependency in flight beyond an optional weather fetch.

```
YOU   Kennedy Ground, Delta twelve thirty four, ready to taxi.
ATC   Delta twelve thirty four, Kennedy Ground, runway three one left,
      taxi via Uniform, Zulu, Delta, hold short of runway four left.
YOU   Runway three one left, hold short of four left, Delta twelve thirty four.
ATC   Delta twelve thirty four, readback correct.
```

## What it gets right

**Frequencies are real, and they match your radio.** 24,977 published ATC
frequencies at 72,524 airports,
filtered to the VHF airband so nothing is offered that you cannot dial into
COM1. Kennedy Tower is 119.10, Heathrow Ground is 121.70, O'Hare Delivery is
121.60, because that is what the charts say. Tune 121.90 at Kennedy and Kennedy
Ground answers; tune the same frequency at Heathrow and Heathrow Ground does.
The number on your panel is matched as a *channel*, not as a number, so the
121.605 an 8.33 kHz radio shows finds the 121.600 the charts publish. See
[The number on the radio](#the-number-on-the-radio).

**Phraseology is written, not generated.** Every word a controller says comes
from a fixed template built on FAA JO 7110.65, the AIM, and ICAO Doc 4444. No
language model composes controller speech, so it cannot drift, invent
instructions, or phrase the same clearance differently twice. US and ICAO
dialects differ where they should: "climb and maintain one two thousand,
altimeter two niner niner two" against "climb to one two thousand, QNH one zero
one three". Runway 4R is "runway four right" in the US and "runway zero four
right" everywhere else.

**Voices are consistent, and local.** Each controller position gets a voice
derived from a stable hash of the facility, so Kennedy Tower sounds like the
same person on every flight you ever make. Positions at one field are
guaranteed to be different people, and a TRACON sounds the same whether you are
departing Kennedy, LaGuardia or Newark. The accent follows the region: Frankfurt
Ground is a German speaker, Madrid Tower a Spanish one, Orly Tower a French one.
Speech is band-limited to the 300-3400 Hz AM voice channel and run through
compression, transmitter clipping, cockpit-speaker resonance and a noise floor
that grows with distance, so a centre 250 miles away sounds like a centre 250
miles away.

**The frequency is bilingual where the real one is.** At Orly the controller
says "Air France mille deux cent trente-quatre, autorisé décollage" and then, on
the same frequency seconds later, "Speedbird one seventeen, cleared for
takeoff". Speak either language and you are answered in the one you used -- once
you have said something of substance in it: a lone "répétez" or "roger" in the
other language is understood, and answered in the language the frequency was
already being worked in, and an instruction said again is said again entirely
in one language. It is the same controller either way, so their English carries their French accent --
built sound by sound out of the phonemes a French speaker has, not by pointing a
French voice at English spelling. See [Accents](#accents).

**There are other aeroplanes on it, and out of the window.** Airlines that
belong at the field you are at, each crew speaking the language it would really
speak there, working the same runway you are -- drawn on the map and created in
the simulator, so the one you hear on final is the one on final. See
[Other traffic](#other-traffic).

## The language follows the aeroplane

There is no language setting. English is available everywhere, because ICAO
Annex 10 requires it to be, and the local language joins it over a state that
works one -- and leaves again when you fly out. Fly Paris to Rome and the
recogniser is listening for French and English on departure and for Italian and
English on arrival, without anything being switched.

| Where you are | What is worked |
|---|---|
| France, Quebec, Geneva, francophone Africa | French and English |
| Spain, Mexico, most of South America | Spanish and English |
| Germany, Austria, Zurich | German and English |
| Italy | Italian and English |
| Portugal, Brazil, Angola | Portuguese and English |
| Everywhere else | English |

"Everywhere else" includes states that really do work their own language --
Russia, China, Japan, Greece, Poland, the Netherlands -- but that this system
cannot speak. There you get English, which is what an international crew gets
there too. The accent still follows the region, so Warsaw Tower is a Polish
speaker speaking English.

Six languages are worked in full. Each has real radiotelephony rather than the
English translated: Italian says "rulli via Alpha Bravo, punto attesa pista zero
sei", not a word-for-word rendering of "taxi via Alpha Bravo, hold short of
runway six". Each has its own number conventions, followed inside a single
phrase -- "vent deux sept zéro degrés, dix noeuds" reads the wind direction
digit by digit and the speed as a whole number, and "QNH mille treize" is a
whole number where "niveau de vol trois cinq zéro" is digits. German reads
"eintausenddreizehn" as one word and Portuguese as "mil e treze", and both are
understood when you say them back.

Heavily accented English used to be misheard as the speaker's mother tongue.
Below a confidence threshold the audio is decoded in every candidate language
and the decoder's own confidence decides, which fixed it.

**A mis-heard word no longer costs you the whole transmission.** Recognition is
good at ordinary French and worst at exactly the words that decide what a
transmission means: "clairance" comes back as "clérance", "prévol" as
"prévole", "prêt" as "pré". One wrong letter used to defeat the rule and earn a
"say again" for a phrase the pilot had said correctly. Now every token is
compared against the words that actually mean something on a frequency, and one
within a letter or two of one of them is put back.

The care is all in the other direction. "possible" is two edits from
"impossible", "ciento" from "viento", "abflug" from "anflug" -- and each of
those would change what the pilot said rather than repair it. So a word has to
be long enough for an edit to be evidence, a prefix is never added, no number
word is ever moved, and every word the grammars themselves match on is
protected. That last set is read out of the grammars rather than listed by
hand, so a new rule protects its own vocabulary, and a test walks all 6,171 of
them to prove nothing moves.

The recogniser is also prompted with the pilot's half of the radio rather than
the controller's. It only ever hears a pilot, and steering it toward words it
will not meet is worse than not steering it at all.

The one reason to pin the set is speed. A machine that will never leave
English-speaking airspace can keep the smaller English-only recogniser:

```yaml
speech:
  languages: [en]
```

Leave it empty -- the default -- and the language follows the flight.

The ATIS plays in any language the field works:

```
> /atis LFPO fr
> /atis LIRF it
> /atis LFPO en
```

## The number on the radio

Europe moved the airband to 8.33 kHz spacing and the number on the panel
stopped being a frequency. It is a channel *name*, and three of every four
25 kHz carriers picked up a second one five kilohertz above: Paris Ground is
published as 121.600 and displayed as **121.605**. Both are the same
transmitter.

Matching on the digits therefore fails at essentially every European field. Of
the 24,977 frequencies in the database, 23,853 are shown differently on an
8.33 radio than they are published, so 95% of them would not answer the number
you actually dialled. They do now: the comparison is made on the carrier.

```bash
python -m wilcoatc freq LFPG          # both numbers, side by side
python -m wilcoatc freq --audit       # check the whole table
python -m wilcoatc freq --audit --fix # and correct what can be corrected
```

The audit found something else. A frequency written to two decimals loses its
last digit, so 123.075 was recorded as 123.07 and 128.225 as 128.22 -- neither
of which is a channel any radio can select, so those rows could never have
matched anything. 104 of them are now back on the grid.

## The same frequencies as the simulator

The open dataset is community-maintained and it is thin. Charles de Gaulle
carries nine frequencies in it; the simulator has thirty. Four of the nine are
frequencies the simulator has never heard of, and the one a pilot is most
likely to be sitting on is in neither. Comparing the panel against the
simulator's own ATC menu showed two different airports, and the simulator is
the one you have to talk to.

So when it is running, it is the authority. Wherever the aeroplane is, that
airport's list is read out of the simulator and replaces what was published:

```
LFPG   published        from the simulator
GND    121.600          121.610  121.580  121.640  121.680  121.780 ...
TWR    119.250 120.650  118.655  119.255  120.905  123.605
ATIS   127.125          127.130  128.230
```

It happens by itself, once per airport, on its own thread. Ask for one
deliberately -- a destination, before you get there -- with:

```bash
python -m wilcoatc freq LFPG --from-sim
```

Four rules keep that an improvement rather than a different kind of wrong.
Only channels a COM radio can select go in, because the simulator also carries
the military frequencies at the field. The spoken names stay as published: the
simulator shouts, and it calls de Gaulle's ATIS "LFPG", but Kennedy's approach
really is *New York* and the published data knows that. Anything you wrote down
yourself is left alone. And the airport's own frequencies lead each position,
so Ground is 121.610 at de Gaulle exactly as the simulator's menu offers it.

Reading them needs a SimConnect library new enough to answer facility
questions; the one bundled with the Python wrapper is not. The MSFS SDK ships
one and the usual places are searched, so this normally needs no setup.
`python -m wilcoatc doctor` says which list is in use. Turn it off with
`sim.frequencies_from_sim`, or name a library with `sim.simconnect_dll`.

**When there is no simulator.** Without one the published data is used, and a
frequency it lacks can be written down once:

```bash
python -m wilcoatc freq LFPG --add GND 123.605 --name "De Gaulle Ground"
```

That takes effect immediately and is written to `overrides.csv`, so it survives
a rebuild of the database and a simulator import both. The controller for where
you are will also answer on an unpublished frequency and say that it assumed;
turn that off with `atc.answer_unpublished` if you would rather have silence.

## The flight plan and the ramp

Two things a flight already involves, read rather than retyped. Both optional.

**SimBrief.** Set your username and the flight fills itself in -- callsign,
aircraft, origin, destination, cruise level -- from the plan you already built.
The plan's departure and arrival runways are used while the wind leaves them
inside 5 kt of tailwind and 20 kt of crosswind, and its SID goes into your IFR
clearance when you depart from the runway it was planned for. The STAR is read
but not spoken; the plan's approach is not read, because no field for it was
confirmed:

```bash
python -m wilcoatc simbrief yourname --save
```

or press *Load latest flight* under Settings.

**Ground handling.** A controller that can see the ramp does not clear an
aeroplane to taxi while it is still attached to a tug, a jetway or a fuel
truck. It reads GSX's own `FSDT_GSX_*` variables when it can:

```
> request taxi
Airfrans twelve thirty four, hold position. You are still on the tug.
```

Local variables are the one thing plain SimConnect cannot read, so getting at
them needs a bridge. The MobiFlight WASM module is used if it is installed, and
`gsx.lvar_file` will read a JSON file of names to numbers that anything else
can write. With neither, a pushback is still noticed from the aeroplane's own
movement -- rolling on the ground with the engines off and the brake released
is being pushed by somebody -- which works with GSX, the built-in pushback, and
anything else.

It also calls the tug. "Push-back approved" is not only a sentence: the
approval starts the push, through GSX's own menu where a bridge that can write
can reach it, and otherwise through the simulator's own pushback -- which is
the built-in tug, and is what GSX drives when it is set to take the default
pushback over. Which entry of the GSX menu is chosen is read out of the menu
GSX is showing rather than guessed at a fixed position, so a stand that offers
no push gets nothing rather than a catering truck. Set `gsx.control` to `false`
to have the controller watch the ramp without reaching into it.

A push is not one choice, though, and neither is anything else GSX does.
Asking for one opens a second menu about which way to leave the stand, so a
request is walked rather than clicked: a list of steps, each a set of words to
look for in whatever menu is on screen when it gets there. A step that finds
nothing ends the walk instead of guessing, and the submenu that never appears
is simply a submenu that stand has not got.

**You can ask the ramp for things yourself.** Address the ground crew and they
answer on the interphone, and what you asked for happens:

```
> ground crew, request pushback
Ground crew: Roger, calling the tug. Release the parking brake when you are ready.

> ground crew, start boarding
Ground crew: Roger, we will start boarding.
```

Pushback, stopping a pushback, confirming one, boarding, deboarding, fuel,
catering, the jetway, the stairs, de-icing and ground power. None of it goes
out over the radio and the controller never hears it. Where nothing can
actually be asked for -- no bridge that can write, or GSX not running -- the
ground crew say so rather than acknowledging a truck that is not coming. Set
`gsx.voice` to `false` to keep the controller's approval reaching the tug
without your own voice doing the same.

```bash
python -m wilcoatc gsx        # what is connected, and how it is being read
```

## The printed phrasebook

There is a manual, and it is generated rather than written:

```bash
pip install fpdf2
python scripts/make_manual.py        # out/wilcoatc-phraseology.pdf
```

Forty-seven pages: how to work the radio, which language is worked where, a
flight from the gate to the gate, and then a chapter per language with what you
can say, what the controller says back, how the numbers are read and how your
callsign sounds.

The interface's icons are generated too, from the two marks at the project
root. `logo.png` is the square one the sidebar and the browser tab use;
`logo_icon.png` is the round one the window and the taskbar use, because the
operating system draws it on its own background at sizes down to sixteen
pixels. Change either and rebuild:

```bash
python scripts/make_icons.py         # the page's mark and the window's icon
```

Nothing in it is written down twice. The controller's lines are produced by
calling the phraseology the controller answers with, the pilot's readbacks by
the composer the AI traffic reads back with, and every phrase in the "what you
can say" section is put through the parser before it is printed. A phrase that
appears in the book is a phrase that demonstrably works, and the test suite
fails if a grammar change breaks one.

That is not a hypothetical safeguard. Writing the book found twenty-one real
gaps: French rules that could never match because normalisation had already
removed the apostrophe they were written around, "niveau supérieur" swallowed
by the flight-level token before any rule saw it, and a position report that
only recognised a distance in figures when a pilot says "ten miles north".

## The window, in your language

The interface is translated into 41 languages, and by default it follows your
operating system. It is a separate question from what is spoken on the radio:
setting the window to Japanese does not make the controller speak Japanese,
because the controller speaks whatever is worked where the aeroplane is.

```yaml
ui:
  language: auto        # or "fr", "de", "ja", "pt", "zh-Hans"...
```

Arabic, Hebrew and Persian lay the whole window out right to left. Every
catalogue is complete or absent: a language with a missing entry falls back to
the English the markup already carries, so nothing ever shows a raw key.

The engine's own notices are translated too, which is most of what you read at
startup, and they carry their values separately from their words -- so
"{airport}" is filled in after the sentence has been chosen rather than before.

## The map

The Airspace screen is a map of the world that happens to have your aeroplane
on it, rather than a radar picture that happens to have some airports around
it. You can drag it, throw it across a continent, and zoom from a single runway
to the whole planet. Nothing on it is invented: the aerodromes and runways are
surveyed, the coastlines are Natural Earth's, the airspace regions are the same
boundaries that decide which centre you would be talking to, and every
aeroplane on it is one you could call.

**It moves.** Drag to pan, wheel to zoom, pinch on a touchscreen, two-finger
scroll on a trackpad, arrow keys and plus and minus if you would rather.
Zooming is anchored on the pointer, so reaching an airport is put the cursor on
it and turn the wheel rather than pan, zoom, pan, zoom. Dragging turns off the
follow-the-aircraft mode, because a map that snaps back to you three times a
second cannot be read; the key in the corner turns it on again, and lights
while it is on. Double-click an airport to go to it.

A trackpad's two-finger scroll arrives as the same event a mouse wheel does,
and treating both as a zoom meant that trying to move the map around zoomed it
in and out instead. They are told apart the way mapping libraries tell them
apart: a pinch always carries a modifier flag whatever the hardware, anything
with sideways movement in it is a trackpad, and what is left is judged on the
shape of the number -- a mouse notch is a big round delta and a trackpad's are
small and fractional. Getting it wrong in the safe direction matters more than
getting it right every time, so a mouse read as a trackpad pans a little
instead of zooming, and the zoom keys are right there.

The projection is Web Mercator, chosen rather than tolerated: it is conformal,
so a circle of coverage is a circle on the paper, a runway's bearing is its
real bearing, and a heading measured off the screen is the heading you would
fly. An equal-area projection would draw truer sizes and lie about every angle,
which on a chart for flying is the wrong trade.

The zoom is held as a drawing scale rather than as a width in miles, and that
is not a detail. A degree of longitude is sixty miles at the equator and sixty
times the cosine of the latitude anywhere else, so holding the miles fixed
means the scale has to change every time you pan north or south -- and it did,
visibly. Crossing Europe from forty to sixty degrees north rescaled everything
by a third while the hand was still moving. Holding the drawing scale fixed
instead makes panning pure translation; the width in miles, which really does
change with latitude on a Mercator, is derived for the scale bar rather than
driving the projection.

**What is drawn depends on how far out you are.** Wide: coastlines, national
borders, the flight information regions with their names, and the airports
somebody would actually be looking for at that scale. Closer: every aerodrome,
who is on frequency at each, and how far each of them reaches. Closest: the
runways at their surveyed bearings, so an aeroplane on final is visibly lined
up with the strip it is landing on.

Filling a continental view with all forty thousand aerodromes would be four
megabytes of JSON and a grey smear, so it is not done. The coastlines come in
three resolutions and only the one being drawn is fetched -- the whole-world
outline is ninety kilobytes, and nobody should wait for five megabytes of
coastline to see the airport they are parked at.

**What is beside it follows the aeroplane, not the map.** Panning away to look
at where you are going must not empty the list of the people you are talking
to, so the stations in range and the traffic list are computed from the
aircraft's own position whatever the window is showing.

Four more things make it readable rather than merely accurate.

**The fields are drawn as they are.** Every aerodrome used to be the same
schematic cross, which said one was there and nothing else. Both ends of a
runway are surveyed in the nav database, so each strip is drawn at its real
bearing and to scale, with the one in use in a heavier line -- and an aeroplane
on final is then visibly lined up with the runway it is landing on rather than
merely near the airport.

**Nothing is written on top of anything.** Labels used to be dropped when they
collided, so the busiest part of the map -- the circuit -- was the part with no
names on it. Each aeroplane now carries a data block on a hairline leader,
placed in the first free position of eight around it, which is what a radar
display does and for the same reason.

**Every aeroplane says where it is going.** A trend vector one minute long, so
a heading and a speed are one glance rather than two readings, and a level with
an arrow on it when it is climbing or descending.

**And what it was last told.** An aeroplane going around on the map is one that
was told to go around a moment ago, and the block under it says so. Click one
and it is selected, on the map and in the list beside it. The instruction is
written in the language the window is being read in, like everything else.

The coverage rings are hairlines now. Six of them filled in over each other
made a grey wash that everything else had to be found inside, and the one that
matters -- the frequency you are actually tuned to -- was lost among them. That
one is still drawn boldly, and it is the only one named.

## The panel in the simulator

A button in the simulator's own toolbar, beside GSX and the camera controls,
that opens WilcoATC without leaving the cockpit view.

```
python msfs/install_panel.py
```

Then restart the simulator.

It is built in the simulator's visual language rather than the application's --
a deep blue ground, one white circular face, two lozenges on it for the
numbers, a row of round keys and a block of monospaced text at the foot. The
window on your desk is a white card on near-black, which is right for a second
screen; pasted over the cockpit it would read as somebody else's program.

The lozenge above the ring is the frequency in COM1. The ring is the world,
north up, with you in the middle turned to your heading and a needle pointing
at the field you are working. The lozenge below is who answers on that
frequency and how far away they are. The keys are swap to standby, play the
ATIS, push to talk, what other traffic is on frequency, and where the full
window is. The foot is the last few transmissions.

The panel contains no air traffic control at all. It polls the application and
sends three commands back, because a rule duplicated there is a rule that would
one day disagree with what the radio was actually doing. The application serves
its own window on a port the operating system picks; the panel cannot be told a
number that changes every launch, so it gets a fixed one:

```yaml
toolbar:
  enabled: true
  port: 8787
```

Loopback only, like everything else here. See [msfs/README.md](../msfs/README.md).

## Other traffic

An empty frequency is the least realistic thing about a single-player ATC
simulation. Real radio is mostly other people: you hold short listening to two
departures and an arrival, and your own clearance arrives in a gap.

```yaml
traffic:
  enabled: true
  from_simulator: true   # work the aeroplanes FSLTL and friends put there
  density: 1.0           # how much to invent on top: 0 is silence, 2 is busy
```

What you hear is Orly tower for four minutes:

```
[fr] PILOT établi sur le localizer piste zéro six, Air France mille six cent soixante-six.
[fr]  ATC  Air France mille six cent soixante-six, Paris Orly Tour, vent deux cinq
           zéro degrés, huit noeuds, piste zéro six, autorisé atterrissage.
[fr] PILOT autorisé atterrissage piste zéro six, Air France mille six cent soixante-six.
[en] PILOT established on the localiser runway zero six, Iberia fifteen oh six.
[en]  ATC  Iberia fifteen oh six, Paris Orly Tower, wind two five zero at eight,
           runway zero six, cleared to land.
[en] PILOT cleared to land runway zero six, Iberia fifteen oh six.
[fr]  ATC  Air France mille six cent soixante-six, contactez Paris Orly Sol
           cent vingt et un décimale sept.
```

Five things make that worth listening to rather than being noise.

**The aeroplanes already out there are worked too.** If you run FSLTL, AIG or
the simulator's own live traffic, those aeroplanes are read out of the
simulator and put on the strips, in the landing sequence and on the frequency.
It is the difference between a controller that can see your airport and one
that cannot: before this it would clear you to land over the top of a 737
sitting on the threshold, and sequence you number one behind six aeroplanes,
because the frequency and the window were two different airports.

They are seen and never instructed. FSLTL flies its aeroplanes from a real
schedule and nothing here can tell one to go around, so an observed aeroplane
is treated as weather rather than as a subordinate: it holds the runway, it
takes its place in the sequence, it is the "Airbus on a four mile final" you
are told to follow, and what the tower says to it is a description of what it
is already doing. It is cleared to land because it is landing; it is handed to
departure because it has just gone. Nothing is said over an occupied runway,
because the clearance a controller would actually give there is a go-around
and this aeroplane will not fly one.

The invented traffic fills in around them rather than on top of them, so a
field with four real aeroplanes on it gets four fewer made up, and a real one
is never drawn into the simulator a second time. `traffic.from_simulator` turns
the whole of it off.

**The traffic belongs where it is.** Operators are weighted by where they are
based, so Orly is Air France, Transavia and HOP with an easyJet and a Vueling;
Frankfurt is Lufthansa, Eurowings and Condor; Kennedy is the US majors. A
long-haul widebody never appears at a field too small for one, and a rank-2
grass strip runs light aircraft with the right national registration prefix --
F-GXYZ in France, G-ABCD in Britain, N123AB in the States.

**Each crew speaks the language it would really speak.** An operator uses its
own language where the state below works it and English where it does not, by
exactly the rule that decides what you are answered in. Air France speaks French
at Orly, French at Geneva and English at Frankfurt. Lufthansa speaks German at
Frankfurt, German at Vienna and English at Heathrow. Ryanair speaks English
everywhere.

**A callsign belongs to one aeroplane.** A flight number is drawn once from the
operator's own range -- Air France short-haul is in the 1000s, British Airways
long-haul under 300 -- and stays with that aircraft from clearance delivery to
the gate. It is never issued twice, and the type never changes underneath it.

**The runway is a resource.** One aeroplane on it at a time, arrivals ahead of
departures, and you are in the same queue rather than beside it. Ask for a
takeoff clearance with an Airbus on a two mile final and you are lined up and
held, not cleared:

```
> ready for departure
Airfrans twelve thirty four, Paris Orly Tower, runway zero six, line up and
wait, traffic landing, the Airbus.
```

and on final behind two others you are told where you come:

```
Airfrans twelve thirty four, number three, follow the Boeing, wind calm,
runway zero six, cleared to land.
```

**Both sides of an exchange or neither.** A call, the clearance that answers it
and the readback of that are three transmissions and one thing to listen to, so
they are queued and dropped together. A frequency too busy to fit all three
drops the whole exchange rather than saying the readback to a clearance you
never heard, and one it has started is finished however long the frequency
stays busy. A sentence that will not compose takes its other halves with it for
the same reason: an aeroplane calling a controller who never answers sounds
like a radio losing transmissions, because that is what it is.

**They do what they are told.** This is the part that used to be missing. The
traffic was a timetable that also produced words: a flight sat in a phase until
a timer fired, and where it was drawn came from which phase that was, so an
aeroplane "cleared for takeoff" appeared a quarter of a mile down the runway
because the phase had changed rather than because it had rolled.

Now the instruction is the cause and the position is the effect. The tower
transmits, the crew reads it back, and from that moment the aeroplane is flying
it -- accelerating, rotating, turning at three degrees a second, climbing at
what its type can climb at. A Cessna and an A320 given the same instruction do
not do the same thing, and both take a plausible time about it.

```
 ATC  Iberia fifteen oh six, go around, traffic on the runway.
PILOT going around, Iberia fifteen oh six.
```

and on the map the aeroplane stops descending, climbs away on the runway
heading, and flies a full circuit before it is back on final -- because that is
what a go-around costs.

Which instructions, and what each one does:

| what is said | what the aeroplane does |
| --- | --- |
| line up and wait | taxis onto the threshold and stops |
| cleared for takeoff | rolls, rotates at its own speed, climbs out |
| cleared to land | flies the approach down, touches down, brakes |
| go around | stops descending, climbs away, rejoins the circuit |
| extend downwind | flies past the point it would have turned |
| continue approach | turns base and comes in |
| reduce speed | slows to what it was given |
| hold short | stays at the holding point |

**The tower is looking out of the window.** None of that is on a schedule
either. Once a second the controller looks at what has developed and says
something about it: an aeroplane a mile from a runway that will not be clear
when it gets there is sent around, one about to turn base in front of an
arrival is told to extend, one closing on the aeroplane in front is given a
speed. The judgement is made in seconds rather than in miles, which is how a
tower makes it -- a departure is released when there is time for the roll
before the arrival needs the runway, not when the arrival is some number of
miles away.

**You are on the field too.** The traffic used to work an airport with one
aeroplane missing from it: yours. A departure was held for an arrival that was
another AI aircraft and never for you, and an AI aircraft landed on a runway
you were sitting on. Now the runway you are holding on is an occupied runway,
an aeroplane on short final behind you is sent around, and a departure waits at
the holding point while you are on final. Nothing about this needs the two
halves of the program to agree about state -- the traffic sees where you are,
which way you are pointing and whether you are on the ground, which is what a
controller sees out of the window.

**They are somewhere, not just saying something.** Every flight has a position:
an arrival is on the extended centreline at the distance it is reporting, on a
three-degree path, slowing from cruise speed to threshold speed as it comes in;
a departure climbs out on the runway heading; an aeroplane in the circuit flies
the circuit, leg by leg, deciding where it is from where it actually is rather
than from a timer. They fly at their own speed, and nothing ever jumps.

That is what makes them visible. They are drawn on the Airspace map, listed in
the bay of strips under the transcript, and created in the simulator as AI
aircraft, so the aeroplane you hear cleared to land is the one on the map and
the one on short final out of the window.

```yaml
traffic:
  in_simulator: true        # create them as aircraft you can see
  in_simulator_limit: 12    # how many at once
  parked: 12                # and how many stand at the gates on top of that
```

At a field whose stands the simulator has given it, some of the gates are
taken by aeroplanes going nowhere yet. They are never on the radio or in the
list beside the map, and they are drawn after the moving traffic, so they
never cost it a place. They are not scenery either: a departure boards one of
them and pushes back from its stand, and an arrival that has parked stays
there as one, rather than vanishing in front of you three minutes after you
watched it taxi in. A few stands are always left for the traffic that is
moving, and none is put on a stand the pilot, FSLTL or the simulator already
has an aeroplane on.

Injection needs a live SimConnect session and fails closed: a simulator that
refuses anything leaves the traffic on the radio rather than raising. Nothing
but what it created is ever written to, and everything it created is removed
when the flight finishes and again on shutdown.

What reaches your speakers is still only what is said on the frequency in COM1,
so the ground frequency is full of taxi instructions and the tower frequency is
full of takeoff clearances. A transmission that falls due while somebody is
talking waits its turn instead of being lost, which is what makes an exchange
arrive as an exchange rather than as one half of one.

## The rest of the aeroplane

Everything on the radio is a controller. This is everybody else: a first
officer in the other seat, the captain on the address system, and the cabin
crew behind you.

```yaml
immersion:
  enabled: false
```

Off until you ask. A pilot who did not ask for a first officer should not have
one start talking to them on a flight they set up yesterday.

### What they do

The **first officer** works the way the pilot monitoring works. The standard
callouts on the roll and on the way down -- eighty knots, V one, rotate,
positive rate, one thousand, five hundred, minimums, and the reverse and speed
calls on the rollout. The checklists, called and answered at the points a
flight actually runs them. And the numbers: when the controller gives you a
level, a heading, a squawk or a frequency, the other seat says it back to you
on the interphone, so two people in the aeroplane heard it. That last one is
the single most useful thing a first officer does and it is on by default.

They will also work the radio for you, reading a clearance back if you have not
after a dozen seconds. That is off by default and deliberately slow when it is
on: the point of the radio is usually to work it yourself, and the aeroplane
must never take the transmission out of your mouth. What it sends goes through
the ordinary text path, so the controller checks it exactly as it checks
yours -- including finding it wrong.

The **cabin** does the rest of a flight. Boarding, doors to automatic and cross
check, the captain's welcome while the tug is pushing, and then -- once the
push is over and the aeroplane is standing waiting to taxi -- the cabin manager
introducing themselves by name:

> Ladies and gentlemen, on behalf of Captain Beaumont and the whole crew,
> welcome aboard Delta flight twelve thirty four to Boston. My name is Joanna,
> I am your cabin manager on this flight, and looking after you today I have
> Nathan and Megan... On behalf of the crew, thank you for choosing Delta.

The names come from the aircraft registration, so the same aeroplane keeps the
same crew, and they are in the language the cabin speaks. The captain is yours
if you want him to be: put a name in `immersion.captain_name`, or in the
settings, and that is who welcomes you aboard.

Then the safety demonstration during the taxi out, cabin crew take your seats
for departure, and the captain again once you have levelled off, with the
cruising level and the flight time. The drinks service after the climb, and a
call to the flight deck before it. The descent announcement with the local time
and temperature at the far end, the cabin secured for landing, welcome to
wherever you have arrived, and doors to manual on the stand.

**They answer when you talk to them.** Address the cabin -- "cabin crew", the
purser, or the manager by the name they gave you -- and the answer comes back
on the interphone instead of going out over the air:

```
> cabin crew, could I get a coffee when you have a moment
Cabin manager: Of course. I will bring it up in a moment.
    ...a minute later...
Cabin manager: Here you are. Mind the cup, it is hot.
```

Drinks, crew meals, how the cabin is doing, securing the cabin for landing, and
the seatbelt announcement for turbulence -- the last two answer you on the
interphone and then say it to everybody, which is what actually happens. An
address with nothing recognisable after it still gets an answer, because a
cabin that does not react at all reads as a broken program.

The address has to be at the front, and it has to be an address. "Ready for
pushback" is a thing you say to a controller on every flight, and nothing is
taken off the frequency unless you named somebody in the aeroplane first.
`immersion.interphone` turns the whole of it off.

Each of those is a switch of its own, so the callouts can stay without the
cabin, or the cabin without the checklists. `verbosity` sets how much of it
there is: `quiet` is the callouts and nothing else, `chatty` adds the small
talk a long cruise actually has in it.

**They watch the aeroplane, not the clock.** Everything the crew say about a
switch is checked against the switch when the words come out rather than when
the cue fired, because the seconds in between are where these go wrong. "V one,
rotate" is dropped at an aeroplane that has already gone. The first officer
reports the beacon on when it is on and says nothing about it when it is not.
And the cabin announces the seatbelt sign when you actually turn it off, which
may be a minute after ten thousand feet and is not a script's business to
decide. An aeroplane that does not model a sign at all is not silenced by this:
a switch is only believed once it has been seen used.

**The crew speak English, French or German.** The operator decides, the way it
already decides what the other aeroplanes on the frequency speak: a Lufthansa
crew is German wherever it is, and a private registration takes the language of
the field it left from. Naming a voice overrides both -- pick a German first
officer and the flight deck and the cabin are German, numbers, clock and
cruising level included. A voice in a language the crew have no lines for keeps
the crew's language and speaks it in that voice.

They do not change at a border, and that is deliberate. A crew belong to the
airline and not to the ground underneath them, so a Swiss flight from Zurich to
Prague is worked in German from the gate to the arrival announcement. The
*controller* is the opposite and already does the opposite: cross into Czech
airspace and Prague answers you in English, because Czech is not one of the
languages this program speaks.

### Your own recordings

A synthesised purser is a fallback. What people actually want in the cabin is a
recording -- their airline's real one, or one of the many sound packs made for
the Fenix A320 -- and there are a great many of those packs about already. So
this reads them, in the layout and with the naming rules Fenix defined, and the
crew play a file instead of saying a sentence.

Drop them in the `announcements` folder beside the program. It is created on
first run with a note in it explaining all of this, because a folder with
nothing in it and no explanation is a folder nobody uses.

```
announcements/
  Announcements/
    BoardingWelcome.ogg          played for any flight
    SafetyBriefing.ogg
    BAW/                         played only when the callsign is BAW
      BoardingWelcome.ogg
    EJU/
      BoardingWelcome.ogg
```

If you already have a pack somewhere else, do not copy it: put its path into
Settings. Pointing at the pack, at the folder above it, or at a whole community
folder full of packs all work, because all three are things people will do and
none of them is wrong.

The seventeen moments, and the file name for each:

| File | When |
|---|---|
| `BoardingWelcome` | boarding has begun |
| `BoardingMusic` | under the boarding, after the welcome |
| `BoardingComplete` | everybody is aboard |
| `ArmDoors` | doors to automatic and cross check |
| `PreSafetyBriefing` | the call before the demonstration |
| `SafetyBriefing` | the safety demonstration |
| `CabinDimTakeoff` | lights down for a night departure |
| `CrewSeatsTakeoff` | cabin crew, take your seats for departure |
| `CallCabinSecureTakeoff` | and the answer back |
| `AfterTakeoff` | the climb, seatbelt sign on |
| `FastenSeatbelt` | turbulence |
| `DescentSeatbelts` | the descent has begun |
| `CrewSeatsLanding` | cabin crew, take your seats for landing |
| `CallCabinSecureLanding` | and the answer back |
| `AfterLanding` | welcome to wherever you have arrived |
| `DisarmDoors` | doors to manual and cross check |
| `DisembarkStarted` | goodbye |

Where a recording exists it replaces the written lines outright -- one file is
one announcement, and half a recording followed by half a synthesised one would
be worse than either. Where none exists the synthesised crew carry on, so a
partial pack is fine. Seven of those moments have no written equivalent at all
and happen only when there is a file: inventing a synthesised "boarding music"
would be silly.

Tags in square brackets narrow when a file is used. A file is only a candidate
if *every* tag on it applies, the most specific candidate wins, and a tie is
settled by a number fixed for the flight -- so a numbered variant is chosen
once and then kept. Hearing a different voice for each half of the same
announcement is worse than hearing the same one twice.

```
SafetyBriefing[1].ogg              one of several, picked once per flight
BoardingWelcome[Morning].ogg       06:00-12:00; also Night, Afternoon, Evening
BoardingWelcome[EJU].ogg           that operator
AfterTakeoff[A319].ogg             that aircraft type
BoardingWelcome[EJU][Evening].ogg  tags combine
```

One thing is deliberately different from Fenix. They pick the airline folder
from `icao_airline` in aircraft.cfg, which only exists on a liveried airliner.
Here it comes from the callsign, and a flight without one falls through to the
files that apply to anything -- so a pack made for an A320 plays in a Cessna,
which is the whole point of not building this into one aeroplane.

Ogg Vorbis is what the packs are and what Fenix requires. WAV, FLAC, Opus and
MP3 are read too, because there is no reason to make somebody convert a file
they already have. A recording is played exactly as it was recorded: no radio
chain, no cabin-speaker colouring, no chime bolted on the front. Somebody who
supplied a real announcement has already decided how it should sound.

### The two rules

**None of it is on the radio.** The crew are not on the frequency and nothing
they say goes out over the air. They read the same aircraft state the
controller reads, and that is the whole of the coupling between them: nothing
here can change a clearance, and the controller does not know they exist.

**None of it queues behind the radio.** A controller calling you in the middle
of the safety demonstration has to arrive when they called, not forty seconds
later -- and in the aeroplane those really are separate paths, the radio in the
headset and the cabin address through a speaker. So the crew have their own
audio path and their own effect profiles: a dry, wide interphone with no
squelch and no carrier for the flight deck, and a narrow boxy one with the
cabin audible around it for the address system. A first officer who arrived
with a squelch burst would be filed by the ear as a second controller.

The chime is synthesised rather than shipped as a sample, for the same reason
the voices are: a sample would be one airline's chime rather than a chime. It
is a pair of struck bars -- inharmonic partials, a fast attack and a long
slightly detuned decay -- going up for an announcement and down for the
seatbelt sign.

### Casting

The crew are cast from the aircraft registration, like every other voice here,
so the same aeroplane keeps the same first officer and the same captain across
sessions. This is the one place the casting can be overridden: you are going to
be listening to these two for four hours, and being able to say "not that one"
is worth having. Pick a man, a woman, or a specific installed voice.

```yaml
immersion:
  enabled: true
  copilot: true
  copilot_voice: auto        # auto | m | f | a voice key
  callouts: true
  confirmations: true        # the numbers said back to you on the interphone
  checklists: true
  radio_readbacks: false     # they work the radio for you
  cabin: true
  cabin_voice: auto
  briefings: true            # the safety demonstration
  announcements: true        # welcome, cruise, descent, arrival
  service: true              # the trolley
  chimes: true
  recordings: true           # play your own files where you have them
  recordings_dir: ''         # empty is the folder beside WilcoATC
  volume: 0.8                # relative to the radio, which stays where it is
  airline: ''                # empty derives it from the callsign
  verbosity: normal          # quiet | normal | chatty
```

The crew appear in the transcript as themselves, on the controller's side but
never dressed as a transmission. A first officer's callout read as a clearance
would be the worst thing this feature could do.

## Accents

Voices are picked by region, so the controller sounds like they are where they
are. Installed with `--all-voices`:

| Region | Voices | Language on frequency |
|---|---|---|
| United States, Canada | 11 US English | English |
| United Kingdom, Ireland, Australia | 5 British English | English |
| France, Quebec, Geneva | 3 French | French and English |
| Germany, Austria, Switzerland | 3 German | German and English |
| Spain, Latin America | 2 Spanish | Spanish and English |
| Italy | 2 Italian | Italian and English |
| Netherlands, Belgium | 2 Dutch | English |
| Portugal, Brazil | 1 Portuguese | Portuguese and English |

The Netherlands works Dutch on frequency in real life and this does not, so
Schiphol is English -- said by a Dutch voice, because a Schiphol controller
speaking English is still a Dutch speaker. The same holds everywhere the local
language is one this system cannot speak: Warsaw Tower is a Polish speaker
speaking English rather than an American.

**The accent is made of phonemes, not spelling.** The obvious way to get a
French controller speaking English is to hand English text to a French voice.
That does not work, and it is worth saying why, because the result is
convincing enough at a glance to ship by mistake. A French text-to-speech front
end recognises some English words and falls back to French letter-to-sound
rules for the rest, so one transmission comes out half in each:

| word | read by a French front end | what a French speaker says |
|---|---|---|
| knots | /kno/, the t silent | /nɔts/ |
| descend | /dɛsɑ̃/, the French verb | /disɛnd/ |
| maintain | /mɛ̃tɛ̃/, as "maintien" | /mentˈen/ |
| Paris | /paʁi/, the s silent | /paʁis/ |

Those are not accented English, they are different words, and no readback
recovers the instruction. So the English is phonemised by the *English* front
end and every sound English has that French does not is replaced by the nearest
French one before it reaches the voice: /θ/ becomes /s/, /h/ disappears, /ɹ/
becomes uvular /ʁ/, "ship" and "sheep" merge, and the diphthong in "takeoff"
flattens. A heavy accent also changes the rhythm, because French has no word
stress at all -- it puts one accent at the end of each phrase, and that accent
is mostly duration: the last vowel is held.

That is worth being exact about, because getting it wrong is expensive. This
used to move the stress mark inside *every* word, which is a third language,
neither English nor French:

```
said       runway zero three
English    ɹˈʌnweɪ zˈiəɹoʊ θɹˈiː
per word   ʁanwˈe  ziəʁˈo  θʁˈiː      <- what it used to do
French     ʁˈanwe  zˈiəʁo  θʁˈiːː     <- one accent, held, at the end
```

Marking each word in turn is how a machine imitating an accent sounds rather
than how a person with one sounds, and it moves the shape of every word at
once, which is what a listener is using to tell them apart. All three were
measured through the recogniser, on five transmissions, at the heaviest accent
the casting produces:

| | words recovered |
|---|---|
| stress mark moved, every word | 0.69 |
| stress mark moved, last word of the phrase | 0.78 |
| final vowel held, last word of the phrase | 0.87 |
| nothing at all | 0.88 |

Holding the vowel keeps almost all of the words and is the realisation a
phonetician would name first, which is a pleasant way for a measurement to come
out.

The substitutions are not hand-written per accent. Each voice is given the list
of sounds it was trained on, and anything outside that list has to go
somewhere; the tables only say where. That is checked rather than assumed: a
model asked for a sound it has never made produces a glitch, not an accent, and
it fails silently, so the tests run every transmission in the phrasebook
through every voice and assert that nothing outside its inventory ever reaches
it.

The other half of that check was missing for a long time, and it is the half
that bites. A sound a voice *should* be able to make, but which the list forgot
to declare, is not passed through unchanged -- it is dropped, and the word
comes out a sound short with nothing on screen to say so. The test that was
supposed to catch it read the pipeline's output, by which point the phoneme was
already gone. Reading the drop instead found three, all of them in a
controller's own language:

| | lost | heard as |
|---|---|---|
| British English | the LOT vowel | "one" as /wˈn/, "takeoff" as /tˈeɪkf/ |
| German | the ich-Laut | "München" and "nicht" hollowed out |
| Spanish | /ʝ/ | the y of "ya" and the ll of "llegada" |

**Rules get most of the way, and then stop.** A controller is not sounding
words out. They have said "cleared for takeoff" ten thousand times and it comes
out as one memorised chunk, and there are words a French speaker knows *better*
than any rule does: the ICAO alphabet is international, and "Bravo", "Papa" and
"Delta" are French words a French mouth already owns. A rule that treats them
as English produces "brah-vuh" and "pah-puh", which is nobody.

What a controller can say is a closed set -- every word of it comes out of a
template -- so the words that carry the traffic are written down. There are 238
entries, covering the alphabet, the numbers, the instruction verbs and the
airline telephony names:

| said | by rule alone | written down |
|---|---|---|
| zero | /ziəʁo/ | /zeʁo/ |
| bravo | /bʁɔvo/ | /bʁavo/ |
| runway | /ʁœnwe/ | /ʁanwɛ/ |
| radar | /ʁedaʁ/ | /ʁadaʁ/ |
| Airfrans | /ɛʁfʁənz/ | /ɛʁfʁɑ̃s/ |
| IFR | /ifœʁ/, read as a word | /i ɛf aʁ/ |

The entries hold a line: the English word with French sounds, never the French
word. "degrees" is /deɡʁiz/ and not /deɡʁe/, because a pilot has to read it
back. Every entry is checked against the voice's inventory by the same test as
the rules, so a hand-written mistake cannot reach a model.

**Stress is moved, never added.** English and French agree on which words are
prominent -- the content ones -- and differ only on which syllable inside the
word carries it, so the accent moves the mark and leaves the function words
alone. Marking every word instead, "on" and "for" and "and" included, is not
how French sounds and is not something a French voice has ever been asked to
do: espeak's own French leaves half its words unstressed. Hammering all of them
came out as a chant, which is most of what made it sound synthetic.

One more thing falls out as a rule rather than as entries: French never leaves a
vowel reduced at the end of a word, so "Alaska", "Cessna" and "Canada" end in a
full /a/ rather than in a schwa.

**And the pauses come from the templates, not from a word list.** There used to
be a second pass that put a pause in front of every instruction word, on the
theory that a long clearance would otherwise run together. It did not do that.
The templates already separate every clause with a comma, because that is how
they are assembled -- the median clause across the whole phrasebook is four
words. What the extra pass did was cut fixed phrases in half, because those
words also occur inside them:

```
radar contact              ->  radar, contact
hold short of runway 06    ->  hold short of, runway 06
point d'attente piste 26   ->  point d'attente, piste 26
```

A controller says each of those as one unit, and a pause in the middle is
exactly the kind of thing that makes synthesised speech sound synthesised. It
was doing this in both languages, at every airport.

How strong the accent is comes from the same stable hash as the voice, so Orly
Ground and Orly Tower are two French people with different English, and each of
them sounds the same on every flight. A strong accent also takes the French
rhythm, holding the last vowel of each phrase, because rhythm is the last thing
a learner acquires and the first thing a fluent speaker gets right.

The English underneath *an accent* is the rhotic kind, for the sake of one
letter. French, German, Spanish, Italian and Portuguese speakers pronounce
every r they see and so does their English; starting from a non-rhotic reading
dropped the r out of "for", "four" and "Orly", which no French speaker does.
The two American habits that would otherwise leak through are handled instead:
the tapped t of "thirty" becomes a real t, and "knots" is kept apart from
"radar" by whether an r follows the vowel.

A controller speaking their *own* English is read the other way, in the variety
their voice was trained on -- so a British one is non-rhotic, and "four" and
"radar" end where an English controller ends them. That had been failing
silently. Every British model names its front end "en-gb", espeak-ng's own data
has no file by that name -- its plain "en" is the British one, and American is
the variant -- so the request raised, was caught, and fell back to handing over
the text, which threw the accent work away on every British transmission. The
alias is resolved now.

**The voices are chosen by measurement, not by taste.** Piper ships four French
voices and only three are used. Scored with DNSMOS, which is a network trained
on human quality ratings rather than a guess, three of them land together and
the fourth sits 0.2 below on signal quality and 0.4 below overall -- in French
as well as in accented English, at every synthesis setting tried. A fourth
voice is not worth a third of the French positions sounding worse than the
rest. The synthesiser's noise settings are picked the same way: a little under
Piper's own defaults measured better, and above them the accented voices come
apart, because more duration noise on a phoneme sequence the model has not seen
before is more chance for it to go wrong.

**The voice underneath is a bigger model now, and that is the change that
mattered.** Everything above is about making a small synthesiser say the right
sounds. It could not make it stop sounding like a synthesiser, because that is
not a property of the sounds -- it is in the vowels, in the joins between
words, and in the fall at the end of a phrase, and a 20 MB model per voice does
not have them. So the controllers are cast from Kokoro-82M instead: 82 million
parameters in one 310 MB file that carries every voice it has, Apache-2.0, and
local like everything else here.

This was tried once before and rejected on two counts, and both of them were
wrong. It was measured with DNSMOS, which is a network trained to grade *noise
suppressors* -- it scores how clean a signal is, not how human it sounds, and
it cannot see the thing being asked about. And it was timed on an untuned
single-threaded session, which put it at 22 times Piper's cost. Given four
threads and a fully optimised graph it renders at 0.16 of real time, against
Piper's 0.03:

| | render time | a 7-second clearance |
|---|---|---|
| Piper, warm | 0.03 x real time | 0.2 s |
| Kokoro, warm | 0.16 x real time | 1.2 s |
| Piper, cold voice | -- | 1.9 s |

Six times the cost, and affordable, because of where the cost lands. Every
transmission is cached by text and voice and ATC repeats itself relentlessly;
a clearance is composed before the controller is due to answer, not when the
audio is wanted; and 1.2 seconds is inside the pause a real controller leaves
before keying up. In a flight that works five positions Kokoro is *faster* in
wall-clock than Piper was, because Piper pays a model load per voice and Kokoro
loads one model for the whole cast: over six transmissions from five
facilities, 0.21 of real time against 0.32.

It is loaded at startup whatever `voice.preload` says, because it is one model
for the whole cast rather than one per voice -- around 550 MB of working set
with everything else the program holds, and two seconds paid once instead of
on the taxi clearance. `preload` still decides whether the Piper voices behind
it are loaded too, because that is the one that costs a gigabyte.

It runs on four threads. That is the knee of the curve rather than a cap: eight
buys another quarter and leaves the simulator with fewer cores, which is not a
trade this program gets to make on somebody's behalf. `WILCOATC_TTS_THREADS`
overrides it for a machine that is not average.

**Twenty-five controllers out of eighteen speakers.** Kokoro publishes its
voices with a grade for how much training data each one had, and the bottom of
that list is audible -- a weak speaker swallows the ends of words, which on a
radio is the difference between a readback and a "say again". So only the
graded-C-and-better speakers are cast, and the pool is widened by *blending*
two of them rather than by reaching further down the list. A blend is the
weighted mean of two style vectors, and a Kokoro style vector is the whole of a
speaker: timbre, rate, where they lean. The mean of two is a third person who
sounds like neither, at the quality of the two they came from.

The English pool keeps the shape the Piper one had, seven American men to four
women and four British men to three, because that ratio was not an accident of
what Piper published. Controlling is still a male-majority trade, and a tower
that answers in a woman's voice half the time is a different kind of wrong from
a robotic one.

**Nothing was traded away to get it.** The check that matters for a radio is
whether the instruction survives, so both catalogues were put through the
recogniser: every English voice in each, twelve transmissions apiece, and the
words compared with what the controller meant to say.

| | voices | words recovered |
|---|---|---|
| Piper | 16 | 0.955 |
| Kokoro | 18 | 0.958 |

The same, within the noise. Underneath it the American voices come back better
than Piper's (0.976 against 0.947) and the British ones slightly worse (0.927
against 0.953), and that gap is the recogniser rather than the voice: the
British controllers are now read with British phonemes, which is the point of
them, and `small.en` was trained mostly on Americans.

Two things are worse. Kokoro has one speaker per language outside English and
none at all for German or Dutch, so the casting keeps the Piper voices that
fill those gaps -- Germany, the Netherlands, and the French male -- rather than
choosing one engine for everything. And there is no French man in Kokoro to be
had: blending its French speaker towards a male English one moves the pitch by
a few hertz and nothing else.

```yaml
voice:
  engine: auto     # kokoro where it can speak, piper for the rest
                   # kokoro -- kokoro only.  piper -- the old, fast, robotic one
                   # chatterbox -- a controller cloned from a recording; see below
```

### A controller cloned from a recording

Every engine above has a catalogue of speakers, and a controller is one of
them, shaded. None of them is *the* Heathrow Director. Chatterbox is the tier
above them and has no catalogue at all: give it ten seconds of somebody and it
speaks as them. That is what turns an hour of a real approach frequency into a
controller who will answer your call in that voice, and it is the only way to
get that voice rather than a voice.

It is opt-in, because it costs what a clone costs. On a sixteen-core desktop
with eight threads the standard model renders a clearance in a little over
twice the time it takes to say it, and the Turbo model in about the time it
takes to say it, where Kokoro does it in a tenth. On a card it is a tenth or better -- but the card is
drawing the simulator, which is the whole reason the CPU tier exists.

```bash
pip install -r requirements-optional.txt   # chatterbox-tts, and torch with it
python -m wilcoatc setup --hq              # the standard model, about 2 GB
```

Then put a clip of the controller in `data/voices/chatterbox/prompts/`, named
for who they are -- `gb_f_heathrow_director.wav` is a British woman the panel
will call "Heathrow Director" -- and say so in the config:

```yaml
voice:
  engine: chatterbox   # the clones wherever a clip matches the field's accent,
                       # the ordinary cast everywhere else
  hq_model: standard   # or turbo (twice as quick, twice the word loss), or
                       # multilingual (the only one that speaks French, German
                       # and the rest)
  hq_takes: 2          # takes to try before settling for the best one
  gpu: false           # true lets `auto` reach the tier when there is a card
```

The model samples, and a take can come back with a number mumbled. Measured
over eight Heathrow transmissions, a single take loses a third of its words
through the radio chain where Kokoro loses a fourteenth, and a controller who
sounds like a person and says "cleared aisle at the port" is worse than one
who sounds like a synthesiser. So every take is read back by the program's
own recogniser, and one whose words did not survive is re-rolled, up to
`hq_takes`, with the best of them kept. The second take is only paid for
when the first one failed.

The clip has to be longer than five seconds, and ten to fifteen is the useful
length: a few clean transmissions with a beat of silence between them. What is
in it is what comes out. A clip taken off an air-band receiver gives a voice
with the receiver's bandwidth already in it, which the radio chain then
band-limits again, harmlessly; a clip with another voice talking over it gives
a controller who sounds like two people.

Two rules keep a clone where it belongs. It takes a position only where its
accent is exactly the one the field asks for -- a Heathrow controller is never
the nearest thing to a Frankfurt one, however the fallback table reads -- and
it is never shaded, because it is a particular person and shifting their pitch
would make them somebody else. Everything else about the cast is unchanged:
the same controller answers at the same position on every flight, and the
positions a flight works in a row are different people as long as there are
clips enough.

### The receiver, measured

The chain that turns a voice into a radio was measured for what a radio
*does* -- the gain control breathing, the keying, the noise floor. Its tonal
balance was then measured against an hour of Heathrow Director and found to
lean well toward the receiver: real controller audio puts 94% of its energy
between 250 Hz and 1 kHz with a spectral centroid at 620 Hz, and the chain's
own profile put 67% there with the centroid near 890. That is the difference
between a voice in a headset and a voice through a hole in a wall, and it was
audible on every transmission.

Three of the profile's own numbers closed it -- the transmitter's presence
lift off, the cockpit speaker's peak brought down, a tilt across the passband
-- and they landed the centroid within a few hertz of the recording. They are
`delivery.tone` in the config, and `enabled: false` gives the brighter
balance back exactly. Beside them, `delivery.imperfections` grew a
`wander_*` trio: the voice's own pitch and level drifting a per cent or so
over a clause, which a synthesiser never does and a person always does.

Getting the centroid right left the *shape* wrong. Re-measured against two
recordings of one Heathrow Director exchange -- the same script read by a
woman and by a man, so that what the two have in common is the channel and
not the speaker -- the curves agree closely: a broad peak at 630 Hz, within
3 dB of it from 400 to 800, a steady fall to -20 dB at 2.5 kHz, and a cliff
past 3 kHz. Read in third-octave bands the chain sat about 10 dB over that at
315 Hz, 5 dB under it at 800, and 4 to 5 dB over it above 3 kHz. A tilt
cannot fix that, because a tilt pivots about a peak and does not move one.

What moves one is the cockpit speaker's resonance, which is a profile field
like the others: down from 1650 Hz to 900, its height up from 2 dB to 4, and
the passband narrowed from 300-3400 Hz to 330-2700, which is the Annex 10
channel the recordings actually are. Moving the resonance also brings the
chain's own notch an octave above it down from 3.5 kHz to 1.9 kHz, where it
takes out most of what was left above the peak.

Putting all of it together, against the recordings, over the same six
transmissions and the same 26-transmission corpus:

| | spectrum | floor | words lost |
|---|---|---|---|
| the chain's own balance | 9.2 dB | -14.5 / -12.2 | 13.7% |
| before | 6.3 dB | -15.1 / -13.3 | 15.6% |
| **now** | **3.6 dB** | **-20.7 / -18.8** | **13.6%** |
| *the recordings* | *(2.4 dB apart)* | *-25.5 / -24.5* | *21% / 37%* |

| | third-octave distance from the recordings |
|---|---|
| the chain's own balance | 9.2 dB |
| presence, tilt and peak (before) | 6.3 dB |
| the resonance moved, the band narrowed, the floor down | 3.6 dB |
| *the two recordings, from each other* | *2.4 dB* |
| *one recording, from its own second half* | *1.1 dB* |

So what is left is close to the width of the target, and most of it now sits
in the bands where the man and the woman disagree -- their own fundamentals --
rather than where they agree.

None of it costs words, which was not the expectation. Every candidate went
back through the recogniser the program ships, over 26 transmissions, with
the numbers spelled the same way on both sides. Taken together with the
floor below, the British Kokoro voices go from 15.6% to 13.6%, and Supertonic
-- which casts nearly every other controller and was not what any of this was
measured on -- averages 14.5% against 14.2% across a tower and a ground, one
field a little worse and the other a little better. The band was narrowed to
where the recordings still have energy, so nothing was taken away that the
words were using. (A twelve-line bench said it cost three points, and twelve
lines is six words, and six words is noise. Use the corpus.)

Going *further* does cost words, and that is what settled where to stop: a
band order of 8 and a 2500 Hz top each bought about two tenths of a decibel
and lost words in every bench they were tried in, and using `presence_db` as
a low cut made the match worse rather than better. None of those is in.

### The floor between the words

The same two recordings settle a second number the balance had nothing to do
with. Measured inside a transmission -- the quietest frames while the carrier
is up, against the speech -- they sit 25 dB under the voice. The chain sat at
14, which is not only louder than the recordings but louder than
`radio_fx`'s own docstring is aiming for: "real ATC hiss is 18-25 dB under
the voice".

No one source owns that gap. Switched off one at a time, the receiver's own
hiss is worth about a decibel of it, the ops room behind the controller two,
and the weather bed the imperfections layer lays under the transmission
another two to three -- and the rest is the transmitter's makeup gain lifting
all three together, because all three arrive *before* the compressor. So they
come down together and every effect stays switched on: `noise_db` and
`room_db` in `delivery.tone` are offsets of -8 and -10 dB, added to whatever
the profile already says rather than replacing it.

Added, not replaced, because the floor is most of what tells one station from
another. `PROFILE_DISTANT` is a weak station two hundred miles away and is
written noisy on purpose; a setting that replaced its figure would make it
sound like one across the field. An offset moves every station down and
leaves the gaps between them exactly as they were.

It is the cheapest change in this section. The floor drops five to six
decibels, to -21 and -19 against the recordings' -25 and -24; the spectral
distance improves as well, because there was hiss in the measurement; the
dynamic range lands on the recordings almost exactly (19.7 and 19.9 against
20.0 and 19.4, from 18.9 and 16.3); and the recogniser gets *more* words
back, not fewer.

The four decibels still showing are not hiss, which is worth knowing before
anyone spends effort on them. Read the floor at a stricter percentile and the
two converge:

| quietest N% of frames | 15% | 10% | 5% | 2% |
|---|---|---|---|---|
| the recordings | -25.5 / -24.5 | -26.9 / -25.7 | -28.5 / -27.5 | **-30.8 / -29.4** |
| this program | -20.7 / -18.9 | -21.7 / -20.2 | -23.9 / -23.2 | **-30.1 / -28.8** |

Deep in the silences -- the 2% of frames that are genuinely nothing -- the
match is within 0.7 dB. What the 15% column measures is partly *how much
silence there is*: the recordings spend 23.6% of a transmission not talking
and this program spends 16.5%, so that percentile sits inside a gap for one
and on the decay of a word for the other. It is the speaking rate below,
wearing a different hat, and it is not fixable by making the radio quieter.

Which is also why the offset is -8 and not more. Taken to -12 or -16 the
floor at 15% does not move at all -- it is saturated by that same effect --
while the floor at 2% goes *past* the recordings, further from them rather
than closer. -8 is where the deep floor lands on the target.

For what "too dark to read" would actually look like, ask the recogniser to
read the *recordings*, on the same six transmissions and the same scorer:

| | words the recogniser loses |
|---|---|
| the male recording (same script as the female, so its truth is known) | 37% |
| the female recording (its transcript came off this recogniser, so this flatters it) | 21% |
| this program, the balance now, male voice | 17% |
| this program, the balance now, female voice | 12% |

Real Heathrow Director, through the recogniser this program ships, loses two
to three times as many words as the program does. So at these levels the
score is not a floor under usability -- a real frequency sits well below it --
and a couple of points either way is not the thing to be frightened of. What
the score is good for is what it was used for here: catching the settings
that cost words and returned nothing. The band order of 8 and the 2500 Hz top
did exactly that, and are out.

The same two recordings say something the chain cannot fix, and it is worth
writing down so it is not re-measured a third time. They articulate 1.4 times
quicker than this program does. They do *not* pause differently: 15 pauses
against 17 over the same six transmissions, 0.44 s mean in both, so the
rhythm is already right and only the speed is not.

Nothing cheap closes it. Scaling `quick` and `measured` together reaches the
recording's pace at 0.85 and costs eight points of word error; 0.92 costs
six. Flattening `measured` alone -- on the theory that the recording does not
slow down for a long clearance, which it does not -- moves the pace by four
per cent and still costs two points. Promoting every comma to its own
sentence, which would buy more pauses, makes both worse: the pace goes to
1.51 and the pause share *falls*, because Kokoro reads a short fragment more
deliberately than a long one.

All of which is the same answer: Kokoro holds its naturalness to about
seventeen per cent over its natural rate and `_POSITION_RATE` already spends
most of that. On a frequency where the point is copying a clearance, trading
words for tempo is the wrong way round, so the pace stays where it is. See
`delivery.prosody` to move it anyway.

The radio chain, meanwhile, is not the culprit and never was: it costs 0.17 of
a point on the old scale, and every gentler variant of it recovers at most 0.03
of that.

An accent is meant to colour the instruction, not hide it, and the ear is a bad
judge of one's own work. So the check is whether the words survive: the
transmission is synthesised, put back through the speech recogniser, and
compared with what the controller meant to say. Over 60 renderings of ten
transmissions through the French voice:

| rendering | words recovered |
|---|---|
| text handed to a French voice | 0.93 |
| English words, French sounds | 0.97 |

Paired on the same transmission, the second came back better 36 times, the same
15 and worse 9.

The other half of the check is how *clean* it sounds, for which the recogniser's
own confidence is a fair stand-in. Through the whole chain with the radio
effects on, a French controller speaking English scores 0.79 to 0.82, against
0.84 for the same voice speaking French. That gap is the accent, and it is
meant to be there: a real French controller does not score like a native one
either. What is not there any more is the gap that came from asking a French
voice for sounds it had never made.

Hear it, or measure it yourself:

```bash
python scripts/voice_compare.py                      # both engines, same six transmissions
python scripts/voice_compare.py --radio              # and as they actually arrive
python scripts/voice_compare.py --time               # what each one costs
python scripts/accent_demo.py --out out/accent.wav   # every accent, and the old French one
python scripts/accent_demo.py --transcribe           # what the recogniser gets back
python scripts/receiver_compare.py                   # the three receiver balances, one tape
python scripts/receiver_compare.py --reference CLIP --numbers
```

`receiver_compare` is the guard on the section above it. A number that closes
a spectral distance and makes the radio sound worse would pass every test in
the suite, so it renders the same six transmissions under the chain's own
balance, the balance before the recordings were re-measured and the balance
now, and puts a cut of the recording next to them if you have one.

`voice_compare` is the one that settles the argument this section is about.
Nothing that runs offline scores naturalness honestly -- the automatic measures
put the two engines within a hair of each other and they do not sound remotely
alike -- so it renders the same controllers on both, labels each one out loud,
and leaves it to your ears.

Add just the ones you want:

```bash
python -m wilcoatc.audio.voices --download         # Kokoro, plus the Piper
                                                   # voices that fill its gaps
python -m wilcoatc.audio.voices --voices fr,de     # extra Piper accents
python -m wilcoatc.audio.voices --all              # everything, about 2.2 GB
```

## The AI layer

Off by default, and deliberately narrow. A language model is used for exactly
one job: understanding a transmission the phraseology rules could not place. It
never writes a word the controller says.

```
pilot speech -> recogniser -> rules ---------------> intent
                                |                      |
                                +-- (unparsed) -> AI --+
                                                       |
                                          phraseology templates -> ATC
```

The rules cover the phraseology you are *supposed* to use, and handle it
instantly and for free. Real pilots also say "uh, we're all set down here
whenever you can fit us in". The model turns that into a takeoff request; the
reply is still a template.

It picks one label from a fixed list and fills a fixed set of typed fields. The
answer is validated against that schema and thrown away if it does not fit, so a
hallucinated altitude of 999,999 feet or a squawk of 4891 never reaches the
controller logic. Below a confidence floor the answer is discarded and you are
asked to say again, because a wrong guess is worse than an admitted
misunderstanding.

Two ways to run it:

```yaml
ai:
  provider: ollama        # local, free, nothing leaves the machine
```

```bash
# install from ollama.com, then
ollama pull llama3.2:3b
```

```yaml
ai:
  provider: anthropic     # the Claude API: better on awkward phrasing
```

```bash
pip install anthropic
set ANTHROPIC_API_KEY=...
```

`/stats` in the console shows how the split is going -- how many transmissions
the rules handled, how many needed the model, and how many nobody understood.

## Requirements

- Windows, with Microsoft Flight Simulator 2020 or 2024 for the sim link.
  Everything except the sim link also runs without a simulator. The window uses
  the Edge WebView2 runtime, which Windows 10 and 11 already have.
- Python 3.11 or 3.12. Not 3.13+ yet: the speech-recognition wheels lag.
- About 1.5 GB of disk, or 2.5 GB with every accent and the multilingual
  recogniser.
- No GPU. Everything is CPU inference and runs several times faster than real
  time on a modern desktop core.

## Install

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m wilcoatc setup
```

`setup` downloads the navigation data, builds the database if there is not one
already, fetches the controller voices and pulls the speech-recognition model.
It is safe to run again: the database is only rebuilt when you pass `--force`,
because rebuilding starts by deleting it and frequencies read out of the
simulator with `freq --from-sim` live in the database and nowhere else.

It takes a few minutes and about 1.2 GB, of which 350 MB is the one Kokoro
model that nearly every controller is cast from. Add `--all-voices` for the
Piper catalogue as well, which is another 1.8 GB and only widens the accents
Kokoro has no speaker for.

**Or skip all of that and just start it.** Nothing above is required, because
the program now downloads its own parts. Start WilcoATC on a machine where
nothing has been fetched and it opens on the setup panel: three lines, three
bars, one Download button. This is the path that matters, because the pilot
most likely to be missing a component is the one who installed this by
double-clicking an executable and has no terminal to be told to run `setup`
in. Before, they got a window that never appeared and a message printed
somewhere they were not looking.

It is in Settings afterwards too, under **Components**, which says what is
installed and what it costs, and fetches whatever is not. Asking again is
always safe: a download that stopped halfway keeps what arrived, a finished
piece is not fetched twice, and the nav database is not rebuilt for the sake
of rebuilding it.

Check it worked:

```bash
.venv\Scripts\python -m wilcoatc doctor        # everything, including the sim link
.venv\Scripts\python -m wilcoatc sound-test    # plays a tone and a controller
```

`doctor` names the audio device you will actually hear and, when the simulator
is running, the airport it thinks you are parked at. If that airport is wrong,
nothing else will work, because every frequency is resolved from your
position.

The same report is in the window, under **Settings -> Diagnostics**, with a
Copy button because most of what it is for is being pasted into a bug report.
It is not a second implementation: both render one set of checks from
`wilcoatc/health.py`, so the window and the terminal cannot start disagreeing
about a machine and leave nobody able to say which is right.

## Windows Defender says it is a virus

It is wrong, and it is wrong for reasons that are worth writing down, because
the same reasons apply to every program built the way this one is.

Nothing here is detected by name. The verdicts people report are
`Trojan:Win32/Wacatac`, `Program:Win32/Wacapew`, `Trojan:Win32/Sabsik` and
their relatives, and all of those are machine-learning verdicts: no analyst
ever looked at this file, and no signature matches anything inside it. A
classifier looked at the shape of the program and found it indistinguishable
from a shape it was trained to distrust. Look at what it sees:

| What Defender sees | Why it is here |
| --- | --- |
| A binary nobody signed, whose hash the world has never seen before | Every build is a new file, and there is no certificate |
| A compressed archive inside the executable, unpacked into a temporary folder at startup | That is how PyInstaller ships a Python program |
| A bootloader byte-identical to thousands of other programs' | The same PyInstaller wheel builds all of them, malware included |
| A global low-level keyboard hook, installed before any window is shown | Push-to-talk has to see the key while the simulator has focus |
| The microphone opened and read continuously | You are talking to a controller |
| Hundreds of megabytes fetched on first run and written next to the executable | The voices and the recogniser, which are too big to ship |

Read that list without knowing what the program is, and it describes a
keylogger with a dropper. Read it knowing, and it describes a radio. The
classifier does not know, and it is not going to be argued with.

### What the build does about it

None of these are fixes on their own. Together they move the file out of the
bucket it does not belong in.

- **No UPX, ever.** Compressing the executable is the single strongest
  trigger, and it buys a few megabytes of a package that is measured in
  hundreds. `upx=False` in `wilcoatc.spec`, in all three places.
- **A version resource.** Product name, company, version, description and
  copyright, stamped into both executables from `wilcoatc/__init__.py`. A file
  that names itself is treated differently from a file that does not, and it
  gives you something to read in Properties when you have just been warned
  about it.
- **`asInvoker`, never elevated.** The program never asks for administrator,
  so a scanner never sees it try.
- **Nothing downloaded is executable.** The first-run fetch is model weights
  and a database. No code arrives over the network, ever, which is the
  behaviour that separates this from a dropper if anything actually inspects
  it.
- **`.\build.ps1 -RebuildBootloader`** compiles PyInstaller's bootloader here
  instead of using the one from the wheel. Identical behaviour, different
  bytes, so the byte-match stops matching. It needs the MSVC build tools,
  which is why it is a switch rather than the default.

### The actual fix, which costs money

**`.\build.ps1 -Sign <thumbprint-or-pfx>`**. An Authenticode signature is the
only thing on this page that ends the argument instead of improving the odds.
Defender's classifiers weigh publisher identity heavily enough that a signed
build with a plausible publisher clears almost all of them, and SmartScreen
accumulates reputation against the *certificate* rather than against each
build's hash, so the second release inherits what the first one earned. An
unsigned project starts from zero on every version, forever.

An OV certificate is a few hundred a year and still starts with no reputation.
An EV certificate is more, and is trusted by SmartScreen immediately. Neither
is something a free project necessarily wants to pay for, which is why the
build works without one and says so.

### If it happens to you

1. **Report it to Microsoft.** Upload the file at
   [microsoft.com/wdsi/filesubmission](https://www.microsoft.com/wdsi/filesubmission),
   choose "Incorrectly detected as malware", and say it is a PyInstaller build
   of an open-source flight simulator add-on. These are normally corrected
   within a day or two, and the correction reaches everyone, not just you.
2. **Get the file back.** Windows Security -> Virus & threat protection ->
   Protection history, find the entry, Restore. Defender deletes quarantined
   files after a while, so do this before re-downloading.
3. **Exclude the folder**, under Manage settings -> Exclusions, so the next
   version is not eaten too.
4. **Check it first, though.** Upload to
   [virustotal.com](https://www.virustotal.com) before you exclude anything. A
   handful of engines saying `packer`, `PyInstaller` or `heuristic` is what a
   frozen Python program looks like everywhere. Thirty engines naming the same
   specific trojan is not a false positive, and means you did not get this
   from the project's own releases page.

Step 3 is a real reduction in your own protection, and it is only defensible
for a file you can account for. Do not exclude a whole drive, and do not
exclude anything you downloaded from somewhere else.

## The program starts and says a DLL is missing

Almost always a half-written build rather than anything wrong with the
machine, and it is worth knowing why it looks the other way round.

PyInstaller writes the two executables first and the `_internal` folder after
them. A freeze that dies partway therefore leaves something that looks
finished -- both .exe files are there, with icons -- and fails at the moment
Windows tries to load the interpreter:

```
[PYI-9232:ERROR] Failed to load Python DLL '...\_internal\python312.dll'.
LoadLibrary: The specified module could not be found.
```

From `WilcoATC.exe`, which has no console, that arrives as a message box
about a missing DLL and nothing else. The natural reading is that the build
is fine and the computer is short of a runtime, so people go looking for
Visual C++ redistributables they already have.

The freeze could die silently for a reason worth naming, because it is not
obvious and it was in `build.ps1` for a while. PyInstaller writes its entire
INFO log to **stderr**, not stdout. PowerShell turns a native command's
stderr into error records whenever that stream is *captured* -- `Tee-Object`,
`*>&1`, a CI step, an IDE task runner, anything that wants a build log -- and
the script runs with `$ErrorActionPreference = "Stop"`. So the build aborted
on PyInstaller's **first log line**, having already deleted `dist\`. Run by
hand in a console it worked; run through anything that captured its output it
did not, which is a difficult thing to notice.

Both halves are fixed. The freeze now asks `$LASTEXITCODE` whether it worked
instead of trusting the stderr stream, and it builds into `dist-staging\` and
checks the bundle has its executables, its `base_library.zip`, the VC runtime
and an interpreter DLL before it swaps the folder into place. A failed build
now leaves the last working `dist\` exactly where it was and says so.

If you are holding a broken folder from an older build, there is nothing to
repair: delete `dist\` and `build\` and run `.uild.ps1` again.

## Fly

Start the simulator, get to the aircraft, then:

```bash
.venv\Scripts\python -m wilcoatc
```

That opens the application. One window, four screens, nothing served to the
network: the engine, the interface and the audio all live in the one process,
and closing the window stops all of it.

It opens on a notice saying this is alpha software, that you will run into
plenty of bugs, and that the controller can say the wrong thing or fail to
understand you. You tick the box before you can carry on. It is shown every
launch rather than remembered, because what it is warning about keeps changing
and an acknowledgement given a fortnight ago was for different software.

**Comms** is where you fly. The frequency you are on and who is listening, the
whole conversation as it happens, a bay of strips under it for everybody else
the controller is working, your own flight strip, and the phrases you are most
likely to need next for the phase you are in. Push-to-talk is the big
button, the spacebar, or the same global key that works everywhere else -- all
three key the same microphone.

**Airspace** draws what is around you: the fields within range, who is on
frequency at each, how far each position's coverage reaches -- which is what
decides who can hear you -- and the other aeroplanes. North is up and you are
in the middle. Nothing on the map is invented for it: an aircraft drawn there
is one that is talking on the radio, and a filled chevron is one that is in the
simulator too.

**Frequencies** is every published station near you or at any field you name,
with the ATIS in full and a button to tune. With the simulator connected COM1
belongs to the aeroplane, so tuning from here tells you to reach for the radio
instead.

**Settings** is `config.yaml` with the lid off: audio devices, push-to-talk,
the languages you will speak, phraseology, weather. Changes are written to the
file, and the few that need a restart say so rather than pretending.

Two other ways in:

```bash
.venv\Scripts\python -m wilcoatc console   # the terminal, no window
.venv\Scripts\python -m wilcoatc gui       # the same panel, in a browser
```

`gui` is for a second monitor or a tablet next to the yoke:

```bash
.venv\Scripts\python -m wilcoatc gui --host 0.0.0.0
```

then open `http://<your-pc>:8787` on the tablet. Only do that on a network you
trust: there is no password, and anything that can reach the port can key your
microphone. The window does not do this -- it binds a loopback port the
operating system picks and nothing else can reach.

Hold the backtick key, say your transmission, release. The frequency in COM1
decides who hears you. Change frequency in the aircraft and the next controller
is picked up automatically.

The simulator supplies your position, altitude, callsign, aircraft type and
radios. Two things it cannot tell us are your destination and your filed cruise
altitude, so give them either in `config.yaml` or on the command line:

```bash
.venv\Scripts\python -m wilcoatc --destination KBOS
```

You can also type instead of talking, which is useful for testing and works
with no microphone at all:

```
> /say Kennedy Ground, Delta twelve thirty four, ready to taxi
> /atis KJFK
> /freq                 show every station within 40 NM
> /who                  who is listening on this frequency
> /where                position, phase, outstanding readbacks
> /help
```

## Said and written are not the same string

A controller says "one two seven decimal seven five" and a pilot writes 127.75.
Both are correct, and the transcript shows the second while the synthesiser is
given the first:

```
ATC   Delta twelve thirty four, cleared to the Boston airport via radar
      vectors, then as filed, maintain five thousand, departure frequency
      135.900, squawk 5657.
```

which is spoken as "departure frequency one three five point niner, squawk five
six five seven".

Only the two things a pilot actually copies onto a strip are converted: the
frequency and the squawk code. Altitudes, headings and runways stay in words on
the screen, because that is how they were said and how they have to be read
back, and a transcript that quietly rendered "one two thousand" as 12000 would
be one you could no longer check a readback against.

The conversion is the inverse of a function in the same package, so the shapes
it recognises are known rather than guessed: nothing is rewritten unless it
matches what `speech.frequency` or `speech_fr.frequency` produced, and it works
in both languages, where French says the megahertz as a whole number -- "cent
trente-quatre décimale deux sept cinq" becomes 134.275.

## The radio

The channel is what makes ATC audio recognisable, more than the voice is. The
chain models the signal path rather than applying effects to the end of it:

    at the controller     room ambience + voice -> microphone
    in the transmitter    pre-emphasis, AGC, overdrive, band limiting
    over the air          path loss, flutter, fading
    in the receiver       squelch, AGC-driven noise floor, audio stage
    in the cockpit        small speaker resonance

Four things carry most of the realism, and all four are measured by the tests
rather than tuned by ear alone:

**The noise floor is audible.** Real ATC hiss sits 18-25 dB under the voice.
Push it to 40 dB down and the result reads as a clean recording with a filter
on it. Measured: 22 dB.

**The noise breathes.** An AM receiver runs automatic gain control, so when the
controller pauses the hiss swells and then ducks under the next word. That
"shhh - words - shhh" pattern is the single most recognisable feature of the
medium, and a fixed noise bed does not have it.

**Somebody keys a microphone, and then lets go of it.** A transmission does
not begin with speech and does not end with it, and the three hundred
milliseconds at either end are the part that says a person is at the other end
rather than a file being played.

This used to be a noise burst faded in and out at both ends, which is what a
squelch does at the tail and the *opposite* of what it does at the head. What
actually happens, in order:

```
key-up     click        the transmitter switching, ringing the cockpit speaker
           thump        the detector's DC step as the carrier arrives
           hiss down    the carrier captures the receiver and the channel
                        goes quiet -- 12 dB in 55 ms, and this is the cue
           (a relay ticks a second time on about four radios in ten)
           the room     the controller's ops room, now on the air
words
key-down   the room     the key held a beat after the last word
           thump        the carrier gone
           kssht        the gain control still wound up for a signal that is
                        not there: 11 dB above the floor, decaying over 60 ms
           gate shut    25 ms, into a muted receiver rather than into silence
```

The drop at the head is the whole thing. A channel going quiet is what a
listener reads as "somebody has keyed up", and a burst of noise there reads as
a sound effect. The blast at the tail is the other half: it is louder than
anything during the transmission, because for a few tens of milliseconds the
receiver is running at full gain into an empty channel.

Two smaller things. About one transmission in six is clipped by a controller
letting go on the last syllable, which is real, common, and kept shallow at
45 ms because a clearance still has to survive it. And the keying is
per-station, from the same kind of stable hash as the voice: once the ear has
learned that Kennedy Ground clicks *like that*, hearing it is recognising
somebody.

None of it happens on a path with no carrier. The first officer on the
interphone does not key, and a squelch burst in front of their voice files them
with the radio instead of with the cockpit.

**The ops room comes with the controller.** Low rumble and air handling are
mixed in *before* the transmitter, so they are band-limited and compressed along
with the speech. Laid on top afterwards they sound like a separate recording;
mixed in early they sit behind the controller.

Every facility also gets its own transmitter response and, for a radar position,
its own path flutter, so no two stations sound like the same box. Dynamic range
lands at 26 dB, where real ATC sits.

Hear it for yourself:

```bash
python scripts/radio_demo.py --out out/radio.wav
python scripts/radio_demo.py --keying --out out/keying.wav
```

The first renders the same transmission with no processing, then strong, then
at thirty miles, then a fluttering centre at two hundred, then marginal and
breaking up, then the ATIS loop, then your own sidetone.

The second is a different question and gets its own tape: five short
transmissions from five stations with real silence between them, so the only
thing left to listen to is the carrier arriving and leaving.

## Without a simulator

Everything except the sim link works standalone, which is the easiest way to
audition voices for the airports you fly from.

```bash
python -m wilcoatc freq EGLL              # every frequency, and how it is spoken
python -m wilcoatc atis KSFO --play       # live-weather ATIS, spoken
python -m wilcoatc say "Delta twelve thirty four, runway three one left, cleared for takeoff." \
    --airport KJFK --position TWR
python scripts/demo.py --out out/demo.wav            # a whole departure
python scripts/demo_bilingual.py --out out/demo_fr.wav  # the same, in French and English
python scripts/radio_demo.py --out out/radio.wav        # the radio chain, seven ways
python scripts/radio_demo.py --keying                   # keying up and unkeying
python scripts/accent_demo.py --out out/accent.wav      # one line, every accent
```

The application works without a simulator too. Give it a departure airport and
it parks the aircraft there, so the frequencies resolve and you can fly the
radio on its own:

```bash
python -m wilcoatc --no-sim --departure KJFK --destination KBOS
```

Tuning works from the Frequencies screen either way. See below.

In the console, `/tune`, `/state` and `/flight` drive a simulated aircraft:

```
> /flight callsign=BAW117 aircraft_type=B77W departure=EGLL destination=KJFK
> /state latitude=51.47 longitude=-0.4543 on_ground=True
> /tune 121.975
> /say Heathrow Delivery, Speedbird one seventeen, request clearance to Kennedy
```

## The simulator link

The engine looks for Microsoft Flight Simulator, and keeps looking.

That sounds like nothing and it is the whole of a bug that made the program
useless. The old code connected once, during startup, and never again -- so a
pilot who opened the panel before the simulator had finished loading, which is
nearly everybody, got the stand-in aircraft state for the entire flight: parked
at whatever airport the config file named, with a position and a frequency that
never moved. From the pilot's side that is indistinguishable from the program
being broken, because it was.

Now:

* it retries every few seconds until the simulator appears, and picks it up
  mid-flight when it does;
* it notices when one goes away, says so, and keeps the last known position
  rather than freezing on it or zeroing it;
* the flight follows the aeroplane. A departure airport typed last week is a
  fallback, not a fact: if the simulator has the aircraft on the ground
  somewhere else, the field under it wins and the setting is corrected, so the
  panel stops insisting on an airport half a continent away and resolving every
  frequency against it. Airborne is always plausible -- a flight can be an hour
  out of the field it departed -- and so is a long taxi.
* the aircraft type follows the simulator outright. That is not a preference
  the way a callsign is: the type decides the wake-turbulence suffix and how
  the controller sequences you, and there is no version of "I am a Cessna 172"
  that is true while the simulator is holding an A321.

```yaml
sim:
  enabled: true
  poll_interval_s: 0.25
  reconnect_interval_s: 10.0
```

### Nothing is happening in the aeroplane

While the simulator is paused or you are in its menus, the whole situation
loop stops.

Every watcher in it is watching for a *change* -- a takeoff, a touchdown, a
runway entered, a thousand feet passed -- and while the sim is paused none of
the changes that arrive are things the aeroplane did. A menu that moves you to
another airport is not a flight, and a first officer calling the landing
checklist at it is the program talking over somebody who is not even flying.
So the crew go quiet, the controller stops, and the phase machine holds.

The position is still read, so the panel keeps telling the truth about where
the aeroplane is -- and that is also what makes the first pass after the menu
compare two readings from *after* the jump rather than straddling it. What the
crew still had to say is kept rather than dropped, and its clock is pushed
along by however long the pause lasted, so a queue held for ten minutes does
not come back due all at once.

It is read from the two system events SimConnect actually sends, Paused and
SimStop, the latter documented as firing when the user is navigating the
interface. Not from a camera-state number: those are renumbered between
simulator versions, and a magic number that means "menu" in one release means
a cockpit view in the next. The test is deliberately one-sided and answers
"not paused" unless there is positive evidence, because a false yes silences
the radio and freezes the flight, which is far worse than the thing it fixes.

### Reading the aeroplane four times a second

The SimConnect wrapper does one blocking round trip per variable: it clears the
cached value, sends a request, and sleeps until the answer arrives. Asking for
all thirty-three costs about half a second, which is why a loop that wants to
run four times a second ran twice a second with two seconds of lag behind it.

Only the things that move are asked for every pass -- position, motion, and the
frequency you are on. An aircraft title does not change four times a second,
and neither does the parking brake, the ambient temperature or the flight
number; those rotate a few per pass, which keeps every one of them within a
second or so of the truth at a fraction of the cost. The first read after
connecting takes the lot, because the flight is built from it.

Mean time per pass, on a live MSFS 2024 session: 233 ms, from 2,000.

## Tuning from the panel

The panel is a radio you are allowed to use. Picking a station on the
Frequencies screen sets COM1 in the aircraft, through the same SimConnect event
the knob in the cockpit sends -- so the number in the cockpit, the number on
this screen and the controller who answers are the same three things. The swap
key does what the swap button on a real radio does, and exchanges active and
standby in the aeroplane.

It used to refuse whenever a simulator was connected, on the grounds that COM1
belongs to the aircraft. It does. It is also a thing you set.

There are two events for setting a frequency and no way to ask which one a
simulator has: sending one it does not recognise is not an error, the call
succeeds, the exception arrives on a channel nothing is reading, and the radio
simply does not move. So the first write to each radio is checked -- sent, then
read back -- and the encoding that actually moved the needle is remembered.

Per radio and per active-versus-standby, because they genuinely differ. MSFS
2024 takes `COM_RADIO_SET_HZ` for the active frequency and does not know
`COM_STBY_RADIO_SET_HZ` at all, so one flag learned from the active radio is
exactly how the standby quietly stopped working.

## Push to talk

The default is the backtick key, which most people do not have bound in the
sim. To use a yoke or joystick button instead:

Choose **Joystick** under push-to-talk in Settings, then press **Press a
button…** and press the one you want. That is the whole of it -- the same as
binding the keyboard key, and for the same reason: nobody publishes which
number a yoke gives its buttons, so asking you to type one was asking you to
find out first.

The button is read here rather than in the browser, so the window does not
need focus and the yoke does not have to be on the machine showing the panel.
Anything already held down when you start is ignored, or a latching switch
would answer instantly.

From the command line:

```bash
python -m wilcoatc devices     # lists controllers and their button counts
```

then set `ptt.mode: joystick` with the index and button in `config.yaml`. This
needs `pip install pygame-ce`.

If the settings screen says there is no controller, it now says which of three
things is actually wrong, because they need three different answers:

| It says | What to do |
|---|---|
| no controller found — pip install pygame-ce | install it; the packaged build already ships with it |
| no controller connected — plug one in and reopen this screen | check Windows sees the device |
| no controller connected (some error) | that is SDL's own complaint, and worth reporting |

The device list is rebuilt every time you open that screen. SDL reads the
controllers when it starts and does not look again, so a yoke plugged in after
WilcoATC was running used to be invisible for the rest of the session --
which is most yokes, because people start the simulator first.

## Readbacks

The controller checks your readback item by item against what it actually
issued, for the things a pilot is required to read back: altitudes, headings,
runway assignments, hold-short instructions and squawk codes. Get one wrong and
you are corrected on that item alone.

```
ATC   Delta twelve thirty four, descend and maintain one two thousand.
YOU   Descend and maintain two thousand, Delta twelve thirty four.
ATC   Delta twelve thirty four, negative, descend and maintain one two thousand,
      read back.
```

**A readback is mandatory, and the controller behaves as though it is.** That
is what ICAO Annex 10 II 5.2.1.9.3 and FAA AIM 4-4-7 say it is, and until
recently this program only expected one. An instruction went out, you said
"roger", and the flight carried on as though the restriction had been read
back -- which is exactly the state a readback exists to prevent.

Three things follow from taking it seriously.

**An outstanding readback blocks the next request.** Ask for something else
while one is owed and you are asked for the readback instead, by name:

```
ATC   Delta twelve thirty four, runway three one left, taxi via Uniform Zulu
      Delta, hold short of runway four left.
YOU   Delta twelve thirty four, request pushback.
ATC   Delta twelve thirty four, read back hold short instructions.
YOU   Delta twelve thirty four, roger.
ATC   Delta twelve thirty four, read back hold short instructions.
```

An emergency, a go-around, "say again" and "unable" always get through, and so
does asking again for the thing that produced the instruction -- a pilot
repeating themselves has not heard the answer, and gets the answer rather than
a scolding.

**A hold-short readback has to name the right runway.** Reading back "hold
short of runway one seven left" against an instruction to hold short of runway
one zero used to be accepted, because the item was checked as a yes-or-no. It
is the one readback error that has actually killed people, so it is the one the
checker now sees.

**Going quiet does not make it go away.** An instruction nobody reads back is
chased after about twelve seconds, twice by name, and then read out again in
full. A wrong readback is never quietly accepted: after three corrections the
whole instruction is re-read, and it stays outstanding until it is right.

## What is written down

A controller says "one two seven decimal seven five" and a pilot writes 127.75.
Both are correct and they are not the same string, so the transcript shows the
second while the speaker gets the first. Every number the controller announced
is written the way it would be written on a strip:

```
 said  runway three one left, wind three one zero at one two, QNH one zero one
       three, climb and maintain one two thousand, squawk five seven two two
shown  runway 31 left, wind 310 at 12, QNH 1013, climb and maintain 12000,
       squawk 5722
```

What stays in words is what was never a quantity. A number said in groups is a
callsign -- "Delta twelve thirty four", "Iberia fifteen oh six", "Air France
mille six cent soixante-six" -- and a number said one digit at a time is a
quantity. That is the whole of the rule, and it is why nothing has to guess.
It works in French too, where a runway and a pressure are said as words rather
than as digits.

Nothing here changes a syllable of what is spoken. `ui.numerals: false` puts
the words back on the screen for anybody who would rather read what was said.

**On the air, a number is spoken one numeral at a time.** That is what the
radio does: 127.75 is "one two seven decimal seven five", not "a hundred and
twenty seven point seven five". The two exceptions are both rules rather than
choices. An altitude keeps its thousands -- "climb to five thousand", never
"five zero zero zero" -- and an air-carrier flight number is grouped into
everyday English *in the United States*, where FAA JO 7110.65 2-4-20 says it
is. Everywhere else it is read out: "Speedbird one one seven" at Heathrow,
"Speedbird one seventeen" at Kennedy, and the same aeroplane either way.

**An airliner is called by the name in `data/navdata/icao_callsigns.json`.**
Airliners and freighters alike: the three-letter designator at the front of the
callsign is looked up there, and the controller says the telephony name exactly
as the file writes it -- BAW is "Speedbird", TVF is "France Soleil", CLX is
"Cargolux". A flight identification with letters in it, which is how the UK and
much of Europe file callsigns now to keep similar ones apart, is read a
character at a time: BAW7A8 is "Speedbird seven Alpha eight", not "Bravo Alpha
Whiskey". A designator the file does not list, and any registration, is still
spelled. **So is any aeroplane that is not an airliner or a freighter:** a Baron
whose callsign field says BEL123 is "Bravo Echo Lima one two three", never
"Beeline", and the same goes for a Caravan, a King Air or a Citation. The
simulator's type decides; with no type at all, the filed callsign is believed. The file is plain JSON beside the navigation data; add a line to it and
the next flight uses it. Only a different spelling of the same name is said
another way in another language -- "Air France" rather than "Airfrans" at Orly
-- and nothing else overrides the file.

`atc.spoken_numerals: true` hands the voice the figures instead, so the
controller is given "runway 30, QNH 1013, departure frequency 118.310" to
read. What comes out is then a natural reading of a number rather than a radio
one -- "thirty" for the runway, "one hundred eighteen point three one zero"
for the frequency -- which is why it is off by default. It is the same
converter the transcript uses, so the two can never disagree about what counts
as a number, and a callsign stays a name in both.

## VFR

A VFR flight is worked on a different set of words from an IFR one, and using
the IFR set is the commonest way a simulated controller gives itself away. Set
`flight.ifr: false` and the shape of every exchange changes.

**The runway you ask for is the runway you get.** It was chosen from the wind
and nothing else, so asking for another changed nothing and asking again
changed nothing twice. Name one in a request -- with the taxi call, with the
clearance, or on its own -- and it is used from the taxi instruction through to
the takeoff clearance, as long as the field publishes it. One it has not got is
left alone rather than acted on, so a misheard number gets the runway in use
rather than a clearance to pavement that is not there.

```
YOU   Ruzyne Ground, Swiss four whiskey foxtrot, request taxi, runway three
      zero.
ATC   Swiss four whiskey foxtrot, Ruzyne Ground, runway 30, taxi via Mike,
      Foxtrot, hold short of runway 06, QNH 1013.
```

**Saying which you are flying settles it.** The setting is where a flight
starts, not where it is stuck: ask for an *IFR* clearance in as many words and
you get one, whatever the setting said, and the flight is IFR from then on.
Ask for a clearance VFR and the reverse. A bare "request clearance" leaves the
flight where it was, which is what keeps a VFR flight collecting its release
from delivery rather than being read an airways clearance it never filed.
Loading a SimBrief plan settles it too, because a filed plan says what it was
filed under.

You are never cleared anywhere. You are released in a direction, held at or
below a level where something is above you, and given a code:

```
YOU   Centennial Ground, November one seven two Sierra Papa, request VFR
      departure to the north.
ATC   November one seven two Sierra Papa, Centennial Ground, cleared out of the
      Class Delta, north departure approved, maintain VFR at or below seven
      thousand five hundred, squawk one two zero zero.
```

You are not vectored onto an approach. You are sequenced round the circuit with
reporting points, which is the whole of how a tower without radar keeps VFR
aircraft apart:

```
YOU   Centennial Tower, November one seven two Sierra Papa, inbound for landing.
ATC   November one seven two Sierra Papa, Centennial Tower, enter left downwind
      runway three five right, report midfield downwind.
YOU   November one seven two Sierra Papa, midfield downwind.
ATC   November one seven two Sierra Papa, report turning base.
YOU   November one seven two Sierra Papa, turning base.
ATC   November one seven two Sierra Papa, wind three five zero at eight,
      runway three five right, cleared to land.
```

Ask for circuits and the takeoff clearance says which way round they go, and
each time round you are cleared for the option rather than to land:

```
YOU   Centennial Tower, November one seven two Sierra Papa, ready for the option.
ATC   November one seven two Sierra Papa, wind three five zero at eight, runway
      three five right, cleared for takeoff, left closed traffic approved.
```

Three requests exist that an IFR flight never makes, in all six languages:

| You say | You get |
|---|---|
| request closed traffic, ready for the option, touch and go | circuits, and the option each time round |
| request transition through your class delta | cleared through, a ceiling, and a point to report clear |
| request flight following, request VFR advisories | a discrete code and radar advisories, or a refusal when the sector is busy |

Asking a radar position for a level gets the answer a VFR pilot actually gets
-- "maintain VFR, altitude your discretion" -- rather than a level to hold. The
airspace is named the way the state names it: "Class Delta" in the US, "la zone
de contrôle" in France.

The traffic changes with the field. At an airfield -- anything below the size
that runs scheduled airlines -- every AI aeroplane is VFR, roughly half of them
are doing circuits, and none of them files IFR to anywhere. They say hello when
they call up and good day when they leave:

```
PILOT Centennial Tower, Cherokee eight seven six Hotel Quebec, good day,
      inbound for landing.
ATC   Cherokee eight seven six Hotel Quebec, Centennial Tower, enter left
      downwind runway three five right, report midfield downwind.
PILOT left downwind runway three five right, Cherokee six Hotel Quebec.
...
ATC   Pilatus three Yankee Oscar, contact Centennial Ground one two one point eight.
PILOT contact Centennial Ground one two one point eight, good day,
      Pilatus three Yankee Oscar.
```

## When it did not understand you

It does not say "say again" and leave you there. It knows which position it is
working and where the flight has got to, so it knows what call would make sense,
and it says that instead:

```
YOU   (something the parser could not read)
ATC   Delta twelve thirty four, expecting request taxi, or request pushback.
```

Ask *it* to say again and it repeats what it actually said, word for word,
rather than asking you to repeat yourself:

```
YOU   Delta twelve thirty four, say again.
ATC   Delta twelve thirty four, I say again. Delta twelve thirty four, runway
      three one left, taxi via Uniform Zulu Delta, hold short of runway four left.
```

## The controller is looking out of the window

Answering the pilot is only half of what a controller does. The other half is
watching the aeroplane, and leaving it out is why a flight used to be able to
take a runway, depart, fly a circuit and land again without a single clearance
and never hear a word about it.

Three things get called, because they are the three where being somewhere
without a clearance is an event rather than a matter of taste:

```
ATC   Delta twelve thirty four, hold position, you have entered runway
      three one left without a clearance.
ATC   Delta twelve thirty four, you departed runway three one left without a
      takeoff clearance, possible pilot deviation, advise you contact Kennedy
      Tower after landing.
ATC   Delta twelve thirty four, you landed runway two two left without a
      landing clearance, possible pilot deviation, advise you contact Kennedy
      Tower after you park.
```

Runway occupancy comes from the published thresholds and width, so it is the
real pavement rather than a radius around the field. Crossing a runway is
deliberately *not* called: real ATC issues a crossing clearance and this cannot,
because the taxi routes are synthetic, so challenging every crossing would mean
crying wolf on every taxi at a large field. An aircraft lined up along a runway
is doing something different from one crossing it, and the heading tells them
apart.

Each is said once, not four times a second because that is how often the
simulator is polled, and nothing is said at an uncontrolled field, because
there is nobody in the cab.

### Clearances have to fit the aeroplane

The same blindness worked the other way round. The decision layer had exactly
one input -- what you said -- so it could be talked into anything, and the most
obvious case was being cleared for takeoff while already in the air. Every
clearance now looks at the aircraft first:

| you ask for | while | you get |
|---|---|---|
| takeoff | airborne | "you are airborne, say intentions" |
| takeoff | already cleared | the clearance you hold, not a second one |
| landing | on the ground | "you are on the ground, say intentions" |
| landing | forty miles out | "continue approach" |
| taxi or pushback | airborne | "you are airborne, say intentions" |
| an IFR clearance | airborne | the departure controller's frequency |

Checking in with the tower after rotating gets you the departure frequency,
which is what you actually want, rather than a takeoff clearance for a takeoff
you have finished. A takeoff clearance is spent the moment the wheels leave the
ground, so a circuit needs a new one each time round.

## Handoffs

You are passed from one controller to the next at the point the flight calls
for it, not when you fly out of somebody's radio range:

| On | When | To |
|---|---|---|
| Ground | taxied up to the runway you are departing from | Tower |
| Tower | climbing through 700 ft above the field | Departure, or Approach |
| Tower | slowed below 35 kt after landing and off the runway | Ground, with a welcome |
| Tower | climbing away from a go-around | Approach, to be sequenced again |
| Departure | through 10,000 ft, or 30 nm from the field | the Centre for where you are |
| Centre | 60 nm from the destination | the destination's Approach |
| Approach | in the final-approach cone, 15 nm from the threshold (or 6 nm from the field whichever way you point) | the destination's Tower |

Nobody is handed on, or spoken to first, before you have called them: a
frequency you have only dialled stays quiet until you check in. Where a big
field publishes a tower per runway (Brussels, Schiphol), the handoff names the
one whose published description lists your runway.

```
ATC   Delta twelve thirty four, contact New York Departure one two five
      point two five. Have a good flight.
```

The one after landing names where you have arrived:

```
ATC   Lufthansa four four one, welcome to Prague, contact Ruzyne Ground 121.910.
```

"Have a good flight" is what a controller says to somebody leaving, and it was
being said to every arrival as well -- to an aeroplane that had just stopped on
the airport it was being wished a good flight to. The welcome is the farewell
for an arrival and replaces it rather than joining it. It is only for a landing
that was cleared: a tower that has just had to tell somebody they landed without
a clearance does not then welcome them to the city, so that one gets the plain
handoff.

The first of the handoffs above never happened at all. The phase it hangs on was named,
and was in the table that decides who answers an unlisted frequency, and was
never once set -- so a flight went from taxiing to airborne with nothing in
between, and a pilot who taxied up to the hold was left on the ground
frequency to work out for themselves whose clearance a takeoff is. It is
measured to the end of the runway you are leaving from rather than to the
runway itself, because a taxiway running alongside one is a few hundred feet
from it for its whole length.

None of that is behind `atc.proactive_handoff`. That setting governs the
*reminder* you get when you have flown past the controller you are on and have
not called anybody; being passed on after departure is not a reminder, it is
the next thing the controller does, and it used to wait until the aeroplane
was eighteen miles out. For a centre, whose range is three hundred miles, it
never happened at all.

Where there is nobody to pass you to -- a tower with no radar behind it -- the
frequency is released instead, because silence is what looked like a fault:

```
ATC   November one seven two Sierra Papa, frequency change approved.
```

## VATSIM and IVAO

This program exists to put a controller on frequencies that have nobody on
them. On the networks, some of them have somebody -- and talking over a real
person is the one failure that matters more than any other. So set a network
and it asks first:

```yaml
network:
  provider: vatsim      # none | vatsim | ivao
  refresh_s: 120.0
  use_online_atis: true
```

If a human is transmitting on the frequency in COM1, within range of the
aeroplane, WilcoATC says nothing on it at all. Not an answer, not an advisory,
not a handoff, not a readback chased, and not the invented traffic:

```
121.705 - EGLL_2_GND (Dan Nicks) on VATSIM is working this frequency.
          WilcoATC will stay quiet on it.
```

Tune away, or wait for them to log off, and it has the frequency back and says
so. Nothing changes about how it works otherwise -- every position nobody is
staffing is still worked exactly as before, which is the point: you get a
controller on the ninety per cent of frequencies that are empty, and the real
one on the rest.

**It is the frequency that decides, not the callsign.** Matching "EGLL_TWR" to
Heathrow Tower by reading the callsign works until somebody logs on as
"EGLL_1_TWR", or as "LON_S_CTR" for a sector covering half of England. What is
never ambiguous is that a controller is transmitting on 118.500 and is within
range, which is the same test a radio makes.

**What they publish is used, not just obeyed.** A controller broadcasting an
ATIS is the authority on their own field -- it names the runway they are
actually using and the letter they are expecting to hear back -- so theirs is
read rather than a generated one. Set `use_online_atis: false` to keep the
generated one anyway.

See who is on before you fly:

```bash
python -m wilcoatc online              # everybody
python -m wilcoatc online EGLL         # just that field
python -m wilcoatc online --provider ivao
```

```
callsign     position  frequency  who        ATIS
EGLL_D_ATIS  ATIS      121.935    Dan Nicks  S
EGLL_A_ATIS  ATIS      128.080    Dan Nicks  G
EGLL_2_GND   GND       121.705    Dan Nicks  yes
```

Both feeds are public and anonymous, and nothing of yours is sent to them: the
poll is a plain GET of the whole online list, on a two-minute interval, on its
own thread. A network that goes down does not take the radio with it -- the
last picture is kept and the flight carries on. With `provider: none`, which
is the default, nothing here runs and nothing is fetched.

One honest limit: the picture is as fresh as the last poll, so a controller who
logged on ninety seconds ago may not be known about yet. Shorten `refresh_s`
if that matters more to you than the traffic to their servers.

## Weather and ATIS

The ATIS is generated from live weather and states the runway in use, the
pressure setting and an information letter that rolls only when conditions
actually change, not on a timer.

Weather comes from the simulator first and the real-world observation second.
That order matters: if you set custom weather in MSFS, the ATIS has to match
what you are flying in, not what is happening at the real airport. Real
observations come from the NOAA Aviation Weather Center and are cached, so a
dropped connection does not stop the ATIS.

## What is not included

Three things are missing because no open dataset publishes them, and inventing
them would have meant inventing something a pilot would recognise as wrong.

**Taxi routes are synthetic.** There is no free worldwide taxiway graph. The
phraseology is correct and the route for a given airport and runway is always
the same, but it will not match the airport diagram. Fly the routing you can
see; treat the instruction as flavour.

**SIDs only from your flight plan.** Procedure names are not in open data, so
without a SimBrief plan clearances are issued with radar vectors. That is real
phraseology in its own right and is what smaller fields use, but at a major hub
you would really be given a departure procedure by name.

**Approaches come from the simulator.** With MSFS connected, each airport's
published approaches are read from its facility data, and you are cleared for
the one your runway has: the ILS, else an RNAV, else a non-precision approach,
else a visual one -- never an ILS that is not there. Ask for one ("request RNAV
runway two five left") and you get it if it exists. The ATIS names the same
approach. Without the simulator nothing is known and it is the ILS.

**The arrival is flown in the cone.** The approach clearance, the handoff to
the tower and the landing clearance are given on the approach side of the
threshold, within 30 to 45 degrees of the final course, measured from the
threshold -- not on a radius round the field. The descent follows a three-to-one
profile to the intercept altitude, the approach clearance says what to maintain
until established -- never more than you are already at -- ("descend to
altitude 3000 ft, cleared ILS approach runway 25L" if you are still high), and
the readback owes the runway, the altitude and any QNH given with it. A
go-around -- called, or seen climbing away low in the cone for several seconds
and two hundred feet -- is sent back to Approach, sequenced and cleared for the
approach again before the tower has it back, and cleared to land only once it
is on the final and descending.

**Preferential runways are known for a handful of fields only.** Runway
selection uses the standard rule: greatest headwind, longest hard surface. A
variable wind (`VRB` in the METAR) counts as calm rather than as a wind from
the north. Real airports have preferences driven by noise and airspace flow
that no dataset publishes, so in light or calm wind the choice can disagree
with what your home field really does. `data/navdata/preferred_runways.csv`
ships with Brussels, Heathrow, Schiphol, Charles de Gaulle and San Francisco,
each with its source and how sure it is in a comment; add the fields you care
about:

```csv
ident,landing,departing,max_tailwind_kt
KSFO,28R 28L,01L 01R,5
```

Where a field lands on one runway and departs from another, the ATIS names
both ("landing runway two five left, departure runway two five right").

**Winds are spoken magnetic.** Observations are true; the ATIS and the tower
read the wind from magnetic north, as real ones do. The variation comes from
the World Magnetic Model (NOAA's WMM2025 coefficients, bundled, valid to 2030),
so it works offline anywhere.

**The simulator's weather counts only where you are.** Its wind and
temperature are what is around the aeroplane, so they stand for a field only
when you are on it or within about five miles and 2500 ft of it -- and the
wind only on the ground or below 500 ft, because at 1500 ft on final it is
the wind at 1500 ft. The ATIS letter does not change while you are on final. Elsewhere
the field's METAR is used, then the nearest field's, then calm and standard.

**Descents stop at a floor.** Beyond 30 NM of the field and before the
approach clearance, an MSA-style figure read off a bundled terrain grid: the
highest terrain within 25 NM of the field, and within 25 NM of you, plus
1000 ft -- or 2000 ft where that terrain is more than 3000 ft above the field.
Inside 30 NM, and once cleared for the approach, the floor is the intercept
altitude instead, because the procedure is the terrain protection from there:
the final approach fix altitude when the simulator publishes one, else field
elevation plus about 2500 ft (3000 ft at a field near sea level). A go-around
climbs to the published missed-approach altitude, or the same figure -- never
to the sector altitude. The grid is the highest
point in every eighth of a degree (USGS GMTED2010, public domain; rebuilt with
`scripts/build_terrain_grid.py`), so it errs high by up to a cell: Zurich
comes out at 11,500 ft because of a summit 33 NM away. Where you have the
published minimum sector altitude, write it into
`data/navdata/minimum_altitudes.csv` and it replaces the grid's figure for
that field. Obstacles are not in a terrain model, and the grid stops at 56S.

The other traffic is generated here rather than by the simulator, so it is only
as good as the model behind it: the aeroplanes fly a straight approach and a
straight departure, and nothing holds, diverts or goes around. With
`traffic.in_simulator` off, or with no simulator connected, they stay on the
radio and the map and do not appear out of the window at all.

## When a frequency finds nobody

Tuning a frequency with no controller on it is normal and the system says so,
along with what *is* available where you are:

```
127.850 - nobody is listening on this frequency here. Did you mean 127.750 for
Paris Orly Departure? At LFPO (Paris Orly): ATIS 131.350  DEL 121.050
GND 121.700  TWR 118.700  DEP 127.750  APP 123.875.
```

Two things cause this. Either the radio is mistuned, in which case the
"did you mean" line names the near miss. Or the simulator knows a frequency
that the open data does not, which does happen: add it to
`data/navdata/overrides.csv` using the line the message gives you, then rebuild.

If it says *no aircraft position available*, the simulator link is the problem,
not the frequency. Check `doctor`.

## Uncontrolled fields

Most airfields have no controller at all, just an aerodrome traffic frequency
where everybody announces themselves and nobody answers. That silence is
correct, and it used to be indistinguishable from the program having died, so
three things are true of it now.

**Nobody is ever sent there as though it were a controller.** The station
lookup falls back down the chain to the traffic frequency, which is right for
deciding what to tune and wrong for deciding where to send somebody: it was
producing "contact Laurent Medoc Traffic one two three decimal five" at a field
where that frequency has nobody on it. Where no controller works a position,
the position you are already talking to keeps you and answers itself.

**You are told what the frequency is** the moment you tune it:

```
123.500 - Laurent Medoc Traffic: an advisory frequency, so nobody answers.
Announce your position and listen out.
```

**Nothing is ever said on it** — not a clearance, not a correction, and not
"say again". Answering only the transmissions it failed to parse was the worst
of both: silence when you got it right and a voice when you did not.

**But it is not empty.** The other aeroplanes are on it, announcing themselves,
which is what an uncontrolled field sounds like:

```
[fr] Laurent Medoc Trafic, Foxtrot Golf Romeo Yankee Oscar, bonjour,
     pour l'atterrissage.
[fr] Laurent Medoc Trafic, Foxtrot Golf Delta Zulu Hotel, bonjour,
     prêt au départ piste zéro six.
[fr] alignement piste zéro six, Delta Zulu Hotel.
[fr] en base, Romeo Yankee Oscar.
[fr] en finale, Romeo Yankee Oscar.
```

Nobody reads back a clearance nobody gave, and each crew says good morning
once per frequency rather than on every call.

## Correcting the data

Two files let you fix anything wrong near you without touching code. Both are
applied when the database is rebuilt, so run `python -m wilcoatc.navdata.build`
afterwards.

- `data/navdata/overrides.csv` corrects or adds a frequency. Its main use is
  airports whose only published ATIS is the VOR-collocated one below 118 MHz,
  which is correctly dropped, leaving no VHF ATIS.
- `data/navdata/preferred_runways.csv` sets the preferential runway
  configuration described above.
- `data/navdata/minimum_altitudes.csv` sets the lowest altitude an arrival is
  descended to at a field -- the MSA off your chart. It is read when the
  program starts, without a rebuild.

Verify anything you add against a current chart. A confidently wrong frequency
is worse than a missing one.

## The logs

A flight simulator's ATC is hard to debug from a screenshot. The interesting
failures are not crashes -- those leave a traceback -- but transmissions
understood as something else, readbacks rejected for a reason you could not
see, and pauses that came from a slow model rather than from the controller
thinking. Three streams are written side by side, each answering a different
question.

| File | What it answers |
|---|---|
| `wilcoatc.log` | what happened, with levels, module names and tracebacks |
| `wilcoatc.jsonl` | the same records as JSON lines, with whatever fields the call site attached |
| `radio.jsonl` | every transmission, with the intent, the confidence and the numbers the parser took out of it |

The third one is the reason this exists. "It heard me wrong" becomes a question
with an answer:

```bash
python -m wilcoatc logs                # the transcript, with the reading
python -m wilcoatc logs app -n 200     # the application log
python -m wilcoatc logs json           # the same, as JSON lines
python -m wilcoatc logs where          # which files exist, and how big
```

```
21:04:11  pilot  Kennedy Ground  request taxi                  request_taxi 0.85
21:04:12  atc    Kennedy Ground  runway 31L, taxi via UZD...
21:04:31  pilot  Kennedy Ground  request pushback              request_pushback 0.85
21:04:32  atc    Kennedy Ground  read back hold short instr...
```

Everything rotates, so a long session cannot fill a disk, and every line carries
the id of the run that wrote it, so one flight can be pulled out of a file
holding a fortnight of them. Anything older than `keep_days` is deleted at
startup.

Two more things are kept in memory rather than on disk: the last few hundred
records, which the panel reads over `/api/logs` without being given the disk,
and timings for the three things that can make the radio feel slow --
recognition, synthesis and the situation loop -- at `/api/diagnostics`.

Nothing here writes a credential to a file. The one this program holds is the
LLM API key, and anything shaped like a key is replaced with `<redacted>`
before a record is written, so a log pasted into a bug report is safe to paste.

```yaml
logging:
  level: INFO           # what reaches the files
  console_level: WARNING  # the terminal stays quiet
  directory: ''         # empty is beside the program
  max_bytes: 4000000
  backups: 5
  structured: true      # the JSON-lines copy
  radio: true           # the transcript
  keep_days: 14         # 0 keeps everything
```


After a flight, `logs arrival` (`WilcoATC-console.exe logs arrival`, or
`python -m wilcoatc logs arrival` in a checkout) prints why the arrival was
worked the way it was, beside what was said unprompted: the simulator's raw
approach data and whether it was believed, each descent with the source of its
floor (the sector altitude or the intercept), each approach and landing
clearance with the altitude source and the weather it was read off, and any
go-around with what made it one. `logs radio` now includes the transmissions
nobody asked for -- approach clearances, handoffs, readback chases -- marked
"unprompted".

## Configuration

`config.yaml` is documented inline and every value is optional. The ones worth
knowing about:

| Setting | Default | Why you would change it |
|---|---|---|
| `speech.languages` | `[]` | empty follows the aeroplane; `[en]` pins English and keeps the faster English-only recogniser |
| `ui.language` | `auto` | name a language to override what the operating system says |
| `simbrief.username` | `''` | your SimBrief username or pilot ID |
| `gsx.enabled` | `true` | `false` to stop watching the ramp |
| `gsx.control` | `true` | `false` to watch the ramp without calling the tug |
| `gsx.lvar_file` | `''` | a JSON file of local variables, if something writes one |
| `atc.answer_unpublished` | `true` | `false` to stay silent on a frequency the data lacks |
| `traffic.enabled` | `true` | `false` for an empty frequency |
| `traffic.from_simulator` | `true` | `false` to ignore FSLTL and the simulator's own traffic |
| `traffic.density` | `1.0` | `0.5` for a quiet field, `2.0` for a busy morning |
| `sim.enabled` | `true` | `false` to fly the radio with no simulator at all |
| `sim.reconnect_interval_s` | `10.0` | how often it looks for a simulator that is not there yet |
| `sim.frequencies_from_sim` | `true` | `false` to use the published list instead |
| `sim.simconnect_dll` | *(searched)* | a SimConnect library that can answer facility questions |
| `traffic.in_simulator` | `true` | `false` to keep them on the radio only |
| `traffic.in_simulator_limit` | `12` | fewer on a machine that is working hard |
| `traffic.parked` | `12` | `0` for an empty apron; fewer on a machine that is working hard |
| `toolbar.enabled` | `true` | `false` to stop serving the panel inside the simulator |
| `toolbar.port` | `8787` | the fixed loopback port the panel finds the application on |
| `ai.provider` | `none` | `ollama` or `anthropic` to understand non-standard phrasing |
| `speech.model` | `small.en` | `medium` if your CPU has headroom; the `.en` suffix is dropped automatically unless the languages are pinned to English |
| `ptt.key` | `grave` | any key you do not use in the sim |
| `atc.dialect` | `auto` | force `faa` or `icao` instead of following the region |
| `atc.strict_icao_digits` | `false` | `true` for by-the-book "tree", "fower", "fife" |
| `voice.seed` | `wilcoatc` | change it to reshuffle the entire cast of controllers |
| `audio.radio_effects` | `true` | `false` to hear the raw synthesised voice |
| `weather.prefer_sim` | `true` | `false` to always use the real-world observation |
| `network.provider` | `none` | `vatsim` or `ivao` to stand down where a person is on frequency |
| `network.use_online_atis` | `true` | `false` to keep the generated ATIS even when a controller broadcasts one |
| `flight.ifr` | `true` | `false` to start VFR; asking for an IFR clearance overrides it |
| `ui.numerals` | `true` | `false` to read the transcript in the words that were spoken |
| `atc.spoken_numerals` | `false` | `true` to have the voice read figures rather than say the numerals |
| `immersion.enabled` | `false` | `true` for a first officer and a cabin crew |
| `immersion.verbosity` | `normal` | `quiet` for the callouts alone, `chatty` for a talkative cruise |
| `immersion.radio_readbacks` | `false` | `true` to have the other seat work the radio for you |
| `immersion.recordings` | `true` | `false` to keep the synthesised cabin even where you have files |
| `immersion.recordings_dir` | `''` | a sound pack you already have, wherever it lives |
| `immersion.volume` | `0.8` | the cabin is behind you and should not be louder than the controller |
| `logging.level` | `INFO` | `DEBUG` when something needs catching |
| `logging.radio` | `true` | `false` to stop writing the transcript |
| `logging.keep_days` | `14` | `0` to keep every log for ever |

## How it fits together

```
push to talk ─> microphone ─> faster-whisper ─> intent parser
                                                     │
                                          nav database (who is on this
                                          frequency, here, right now)
                                                     │
                                             controller logic  <─  other
                                                     │            traffic
                                          phraseology templates
                                                     │
                                  controller voice (stable per facility)
                                                     │
                                     radio effect chain ─> speakers
```

| Component | What it is |
|---|---|
| Speech recognition | faster-whisper, int8 on CPU, seeded with an aviation vocabulary per language |
| Language | follows the aeroplane: English everywhere plus the local language over a state that works one, detected per transmission, with multi-language decoding when detection is unsure |
| Intent parsing | rule-based grammar in six languages; unrecognised transmissions get "say again" rather than a guess |
| AI understanding | optional, local Ollama or the Claude API, classification only, schema-validated |
| Phraseology | fixed templates from FAA JO 7110.65, ICAO Doc 4444, and SERA / the national AIPs, in six languages |
| Transcript | frequencies and squawk codes written in figures; everything else in the words it was said in |
| Speech synthesis | Kokoro-82M, one 310 MB model holding 25 controllers across 6 languages, blended for more; Piper behind it for German, Dutch and the French male Kokoro has no speaker for; Chatterbox above both, opt-in, cloning a controller from a clip |
| Accent | phoneme substitution into the target voice's own inventory, with the stress pattern of the accent |
| Surface watch | runway occupancy from published thresholds; uncleared entry, departure and landing are called |
| Other traffic | airlines weighted by where they are based, each crew in the language it would speak there, sequenced onto one runway alongside you, drawn on the map and created in the simulator |
| Radio effects | full signal path: room ambience, AGC with audible breathing, transmitter overdrive, band limiting, path flutter, speaker resonance, and per-station keying -- the carrier quieting the receiver on the way in and the gain control's blast on the way out |
| The crew | optional: a first officer on the interphone and a cabin on the address system, following the flight the controller is working, on their own audio path so neither queues behind the other |
| Announcements | your own recordings, read in the layout and naming the Fenix A320 packs use, and played in any aircraft rather than only that one |
| Map | Web Mercator, pan and zoom from one runway to the whole world, over Natural Earth coastlines and the flight information regions from the nav database |
| Interface | a desktop window, four screens, translated into 41 languages; the same panel over loopback for a tablet, plus a terminal console |
| Setup | the window downloads its own parts on a first run, with a bar per component, and reports what works from the same checks the terminal prints |
| Navigation data | OurAirports, public domain, plus VATSpy ARTCC/FIR boundaries and VatGlasses enroute sectors with their levels; channels matched by carrier, so 8.33 kHz names resolve |
| Flight plan | SimBrief, read-only, on request |
| Ground handling | GSX through a local-variable bridge, or a pushback inferred from the aeroplane; the tug called on approval |
| Weather | NOAA Aviation Weather Center, with the simulator taking precedence |
| Sim link | SimConnect |

Medium-quality voices are a deliberate choice, not a compromise: the radio
chain band-limits everything to the AM voice channel, so the extra detail a
high-quality model produces is filtered away before it reaches your speakers.

## Performance

Measured on a Ryzen 7 9800X3D, CPU only, no GPU:

| Stage | Time |
|---|---|
| Recognition | 0.27x real time |
| Synthesis | 0.02-0.18x real time |
| Repeated transmission | cached, instant |
| Nav database | 21 MB, queries under a millisecond |

A normal exchange comes back in about a second and a half.

## The consistency sweep

The bugs that reach a pilot are not on the path everybody flies. They are the
combinations nobody thinks to try: asking ground to taxi you while you are at
cruise, asking approach for a takeoff clearance, asking one controller to say
again what a different controller said on a frequency you have left. Every one
of those was real.

So the cross-product is walked exhaustively rather than sampled: six
positions, sixteen phases, on the ground and in the air, IFR and VFR, every
intent, at four shapes of field, in six languages. Every reply is held against
the things that are true of a controller whatever it has been asked.

| The rule | What it caught |
|---|---|
| Nothing about the surface is said to an aeroplane that is flying | ground issuing taxi routes at cruise; the tower sending an airborne aircraft to Ground |
| Nothing about the air is said to one on the ground | radar identifying aeroplanes still on the stand, and clearing them for a visual approach |
| A clearance comes only from the position that owns it | takeoff clearances from the wrong frequency |
| A handoff names somebody who exists and can be heard | being sent to an aerodrome traffic frequency with nobody on it |
| Nothing is said again that was not said here first | approach reading out ground's taxi clearance word for word |
| A readback is only demanded for something actually issued | being asked to repeat what was never transmitted |
| A distress call is always answered | "mayday" answered with "expecting inbound for landing" |
| The controller's own words are a correct readback | a French taxi clearance that could not be read back in any form of words, in five languages |
| Every correction can be answered by saying it back | the correction loop with no way out |

A failure names the field, position, phase, aircraft state and intent that
produced it, so it can be reproduced directly. It is not a proof of
correctness -- it is a floor, and the floor is raised every time something
gets through.

## Tests

```bash
python -m pytest tests/ -q
```

2,245 tests. The ones that matter check published frequencies at real airports,
spoken-form rules against their source documents, that voices stay consistent
per facility and distinct per position and across a language switch, that no
English leaks into a French transmission, that all six phraseology classes are
interchangeable down to their signatures, that the AI layer is never consulted
when the rules already understood, and that the radio chain hits its measured
targets: dynamic range in the 25-35 dB band real ATC occupies, an audible noise
floor, hiss that breathes rather than sitting flat, and keying that goes the
right way round -- the channel quieting as the carrier arrives, the gain
control's blast as it leaves, and neither of them on a path that has no
carrier to key.

The number tables are checked by round trip rather than by example. Every value
the controller can say -- every number to 2,100 and every hundred to 60,000, in
five languages -- is spelled out and then read back through the parser, 13,400
values in all. That is the only way to be sure the two halves of a bilingual
conversation agree about what "eintausenddreizehn" means.

The interface catalogues are checked the same way: every one of the 41 has to
carry every key, none blank, none still English, and every brace that gets
filled in at runtime has to survive translation.

Two groups exist because of failures that are invisible until you listen or
until you fly. The accent tests run the whole phrasebook, in both languages,
through every voice and assert that no sound outside a voice's own inventory
ever reaches it, that nothing is silently dropped, and that English words keep
their English pronunciation rather than being read as French ones. The
consistency tests fly an aeroplane rather than typing at one: they take a
runway, depart and land without a clearance and check the controller says so,
and they ask for a takeoff clearance from four thousand feet and check that it
does not arrive. The traffic tests fly an hour of Orly and assert that no two
departures are released inside one runway occupancy, that no callsign is ever
issued twice, and that Air France speaks French there and English at Frankfurt.

The channel tests tune four thousand published frequencies the way an 8.33 kHz
radio displays them and check that every one comes back to its own airport,
while making sure that two genuinely different 8.33 channels are never folded
into one.

The announcement tests are mostly fidelity to somebody else's specification:
the folder layout, the file names, the tag rules, and the way a numbered
variant is chosen once and then kept. The rest is the one deliberate departure
-- that a pack has to work in an aeroplane that is not an A320, and in one with
no airline at all -- plus a check that no two settings ever share a name again,
because two settings with one name are one setting and the switch for the
trolley was briefly the switch for the sound packs.

Two more groups exist because of bugs that looked like the program simply not
working. The simulator-link tests take a simulator away mid-flight and give it
back, and assert that the aeroplane is picked up when it appears, that it is
not left frozen where it last was when it goes, and that a departure airport
typed last week does not survive contact with an aircraft parked somewhere
else. The crew tests fly a whole flight past the first officer and the cabin
and check mostly what they must *not* do: give the safety briefing twice, call
the landing checklist thirty seconds after takeoff, tell a cruise at
thirty-five thousand feet to take its seats for landing, or say anything at all
when nobody asked for them -- or while the simulator is paused and nothing is
happening in the aeroplane at all.

## Licence and data

The code is yours to do as you like with. The data has its own terms:
OurAirports is public domain, the VATSpy boundary data is CC BY-SA 4.0, the
enroute sectors -- who works the flight at which level, London Control,
Paris Control, Maastricht above Brussels -- are the
[VatGlasses data](https://github.com/lennycolton/vatglasses-data) by the
VatGlasses contributors under CC BY-NC-SA 4.0 (attribution, non-commercial,
share-alike: it is downloaded with the navigation data into its own folder,
`vatglasses/`, and never bundled with the program, and its frequencies are the
ones maintained for VATSIM, which mirror the real ones but are not an AIP), the
map's coastlines and borders are Natural Earth, which is public domain, NOAA
weather is US government work, Kokoro-82M is Apache-2.0, and the Piper voices
carry the licences listed in their model cards on Hugging Face.

Recorded announcements are yours, and stay yours: none ship with this and
nothing is downloaded. The folder layout and file names are the ones
[Fenix Simulations document for their A320 cabin
announcements](https://kb.fenixsim.com/cabin-announcements), followed so that
packs made for that aeroplane work here without being rewritten.

The world outline is built and committed rather than fetched at runtime, for
the same reason the nav database is: this has to work on a machine with no
internet, next to a simulator that also has none.

```bash
python scripts/make_world.py
```

This is a simulator toy. Do not use it to prepare for a real flight, and do not
confuse its phraseology with current published procedure.
