"""The other aeroplanes.

An empty frequency is the least realistic thing about a single-player ATC
simulation. Real radio is mostly other people: you hold short listening to two
departures and an arrival, and your own clearance arrives in a gap. This module
manufactures that, as complete two-sided exchanges -- a controller instruction
and the readback that follows it -- on the frequency the user is actually
tuned to.

Four things make it worth listening to rather than noise.

*The traffic belongs where it is.* The roster comes from
:mod:`wilcoatc.atc.operators`, which is weighted by where each operator is
based, so Orly is full of Air France and Frankfurt is full of Lufthansa.

*Each crew speaks the right language.* An operator's crew uses its own language
where the state below works it and English where it does not, by the same rule
that decides what the user is answered in. Air France speaks French at Orly and
English at Frankfurt, in the middle of the same flight.

*A callsign belongs to one aeroplane.* A flight number is drawn once, from the
operator's own range, and stays with that aircraft from clearance delivery to
the gate. It is never handed to a second aeroplane while the first is still on
frequency, and the aircraft type never changes underneath it.

*The runway is a resource.* Only one aeroplane is on it at a time, arrivals
have priority over departures, and a departure released in front of an arrival
gets the gap it needs. That is what makes a takeoff clearance arrive when it
plausibly would rather than on a timer.
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field

from ..navdata.build import project
from ..navdata.channels import same_channel
from ..navdata.db import (NavDB, Station, bearing_deg, dialect_for,
                          haversine_nm, normalize_runway)
from .callsigns import every_language
from . import fleet as fleets
from . import live as livemod
from ..logs import decision
from . import circuit as circuitry
from .circuit import (CIRCUIT_BASE_NM, CIRCUIT_CENTRELINE_NM,
                      CIRCUIT_UPWIND_NM, CIRCUIT_WIDTH_NM)
from . import ground as groundwork
from .flying import (AIRLINER, OWN_DECISIONS, Command, Performance, Target, glide_altitude,
                     offset_from_track, performance_for)
from .flying import step as fly_step
from .flying import FLARE_FPM, FLARE_FT, LIFTOFF_AFTER_S
from .flying import LIGHT as LIGHT_PERFORMANCE
from .language import phraseology_for, reply_language
from .operators import (LIGHT_FLEET, Operator, by_icao, registration_prefix,
                        roster_for)
from .phraseology import Aircraft, Weather, wake_suffix
from .pilot import PilotSpeech

log = logging.getLogger(__name__)

# How long a departure occupies the runway, and the gap behind a landing.
RUNWAY_ROLL_S = 45.0
LANDING_ROLL_S = 55.0

# The pause between an instruction and its readback. Long enough to hear as two
# transmissions rather than one, short enough to sound like a working pilot.
READBACK_GAP_S = 2.6

# Live flights per density unit, by airport size rank. A rank-5 field runs a
# lot of aeroplanes; a rank-2 grass strip runs one Cessna in the circuit.
CONCURRENT_BY_RANK: dict[int, int] = {0: 1, 1: 1, 2: 2, 3: 3, 4: 5, 5: 7}

# Below this rank the traffic is light aircraft rather than airliners, and --
# because that is what a field of that size actually works -- VFR rather than
# IFR. An airfield does not run an IFR departure queue.
AIRLINE_RANK = 3

# How many of the VFR aircraft at an airfield are doing circuits rather than
# going somewhere. Roughly what a fine afternoon at a training field looks
# like: most of the movements are the same four aeroplanes going round.
CIRCUIT_SHARE = 0.45


@dataclass
class Flight:
    """One AI aeroplane, from first call to last."""

    written: str                 # AFR1234 or F-GXYZ
    type_code: str
    aircraft: Aircraft
    language: str                # what this crew speaks at this field
    operator: Operator | None
    arriving: bool
    # Which rules this aeroplane is flying under. A Cessna in the circuit at a
    # grass strip does not file IFR to Nice, and having it ask for an airways
    # clearance was the single most obviously wrong thing on the frequency at
    # a small field.
    ifr: bool = True
    # Staying in the circuit rather than leaving it.
    pattern_work: bool = False
    pattern_direction: str = "left"
    runway: str = ""
    destination: str = ""
    origin: str = ""
    squawk: str = "2000"
    altitude_ft: float = 0.0
    distance_nm: float = 0.0     # arrivals: distance still to run
    phase: str = "clearance"
    step: int = 0
    next_at: float = 0.0
    sequence: int = 0            # position in the landing or departure queue
    done: bool = False
    # The positions this crew has already greeted, so they say good morning
    # once per frequency rather than on every transmission.
    greeted: set[str] = field(default_factory=set)

    # Where it actually is. Flown there by the model in :mod:`.flying` once
    # the aeroplane is on the runway or in the air, so the map, the strip and
    # the simulator all see the same thing -- and see it arrive at the rate an
    # aeroplane arrives at it.
    latitude: float = 0.0
    longitude: float = 0.0
    heading: float = 0.0
    ground_speed_kt: float = 0.0
    vertical_rate_fpm: float = 0.0
    on_ground: bool = True
    # Whether the flight model owns this aeroplane's position. It does from
    # the moment it lines up until it has slowed down after landing; before
    # and after that it is on a taxiway, and there is no taxiway data to fly
    # it along, so it is placed rather than flown.
    airborne_model: bool = False
    # When the nose came up on the takeoff roll (the model's clock), or None:
    # the wheels leave flying.LIFTOFF_AFTER_S later, and the injector's
    # follower pitches the drawn aeroplane from it.
    rotate_at: float | None = None

    # What it can do, and what it is currently trying to do.
    performance: Performance = AIRLINER
    target: Target = field(default_factory=Target)

    # What the controller has told it, and when each takes effect -- which is
    # when the readback finishes, not when the instruction goes out.
    commands: list[Command] = field(default_factory=list)
    # The last one to have taken effect, which is what the strip and the map
    # show: "cleared takeoff", "go around", "HDG 270".
    doing: Command | None = None

    # What it has been assigned and is still holding. These outrank whatever
    # the phase would otherwise fly, which is what makes an instruction an
    # instruction rather than a suggestion.
    assigned_heading: float | None = None
    assigned_altitude_ft: float | None = None
    assigned_speed_kt: float | None = None
    # A downwind being extended: the crew flies past the point they would
    # normally turn until they are told to turn base.
    extending: bool = False
    # How many times this aeroplane has been sent around. It is worth knowing
    # because a second one at the same field is a controller getting it wrong.
    go_arounds: int = 0
    # The circuit it has decided will be its last: it goes round once more and
    # lands off that one rather than asking for the option again.
    last_circuit: bool = False

    # Somebody else's aeroplane: FSLTL, AIG, the simulator's own live traffic.
    # It is read rather than flown and it cannot be instructed, so the flight
    # model never touches it and the tower never tells it to do anything --
    # see :meth:`TrafficInjector.set_observed`.
    observed: bool = False
    # Which object in the simulator it is, which is what it is recognised by
    # between polls. A callsign is not enough: FSLTL reuses one the moment a
    # flight number turns round.
    observed_id: int = 0
    # Where an observed one comes from: "simulator" (FSLTL, AIG, the
    # simulator's own) or "live" (the ADS-B feed, atc/live.py).
    source: str = ""
    # airline, cargo or ga (atc/live.py): what it counts against in the
    # field's density. Set when invented; worked out for an observed one.
    category: str = ""
    # An invented flight whose callsign a real one has turned up with: it
    # finishes what it is doing and goes at its next gate.
    retire: bool = False
    # A live one (atc/live.py): its address, how far its lead (where the
    # real one is now) has to run, and whether our tower is holding it.
    # A long taxi cut in two (atc.taxi.split_route): the taxiways still to
    # be given, and where (layout metres) it was told to hold short.
    taxi_rest: list = field(default_factory=list)
    taxi_split_xy: tuple | None = None
    hex: str = ""
    lead_distance_nm: float = 0.0
    lead_at: tuple | None = None   # (lat, lon) of the real one now
    live_held: str = ""          # "" | "runway" | "give way"
    cleared: bool = False        # delivery has given it its clearance

    # On the ground, at an airport whose taxiways the simulator has given us
    # (see wilcoatc.atc.ground). ``stand`` is the one it is on or going to;
    # ``taxi`` the route it is on now; ``line_up`` the way from the holding
    # point onto the runway, kept until the tower says to use it.
    on_layout: bool = False
    stand: int | None = None
    taxi: object | None = None
    line_up: object | None = None
    planned: object | None = None
    parked_at: float = 0.0
    # The schedule (traffic.lod.mode: schedule). An arrival's stand, held
    # from the moment it is invented so its model can be chosen for where
    # it will turn round; and a parked aeroplane booked as a departure:
    # when its boarding starts, and whether it has.
    planned_stand: int | None = None
    boarding_at: float = 0.0
    boarding: bool = False
    # Boarded with the test button (traffic.debug.triggers): its model is
    # changed in front of the pilot if need be, because seeing its doors is
    # what the button was pressed for.
    by_hand: bool = False
    # A departure that has been handed on and is flying away out of sight,
    # rather than vanishing a mile off the end of the runway.
    handed_off: bool = False
    # Since when it has been waiting for the runway, or 0 if it is not. How
    # the tower knows to leave a gap in the arrivals for it.
    held_since: float = 0.0
    # Parked at a stand and going nowhere yet: out of the window and on the
    # map, never on the radio. See TrafficInjector._fill_stands.
    static: bool = False
    # Whether the simulator has drawn it, for a static aeroplane, which does
    # not wait to be drawn before it exists the way a moving one does.
    drawn: bool = False
    # The watchdog's (TrafficInjector._watchdog): where it last moved from
    # and since when it has stood there, whether it has been given its one
    # recovery, and whether it is stuck -- out of the controller's picture
    # from then on, and taken away if the recovery does not move it.
    still_at: tuple | None = None
    still_since: float = 0.0
    recovered: bool = False
    stuck: bool = False
    # Asked to be drawn again from scratch, at the same spot, as the
    # recovery for one the simulator may have lost: left out of the next
    # positions() so the object is removed and then created again.
    redraw: bool = False

    @property
    def positioned(self) -> bool:
        return self.latitude != 0.0 or self.longitude != 0.0

    @property
    def order(self) -> dict:
        """What it was last told, as a kind and a number.

        Empty until somebody has said something to it, which for a flight
        still on the stand is most of the time.
        """
        return self.doing.as_dict() if self.doing else {"kind": "", "detail": ""}

    def snapshot(self) -> dict:
        """What this aeroplane looks like from outside.

        The shape the map, the traffic list and the simulator injector all
        read, so there is one answer to where an aeroplane is rather than
        three.
        """
        return {
            "callsign": self.written,
            "spoken": self.aircraft.for_language(self.language).spoken_short,
            "type": self.type_code,
            "ifr": self.ifr,
            "operator": (self.operator.icao if self.operator
                         else self.written[:3]
                         if self.source == "live"
                         and self.category in (livemod.AIRLINE, livemod.CARGO)
                         else ""),
            "language": self.language,
            "phase": self.phase,
            "arriving": self.arriving,
            "runway": self.runway,
            "sequence": self.sequence,
            "position": self.position,
            "lat": round(self.latitude, 6),
            "lon": round(self.longitude, 6),
            "altitude_ft": round(self.altitude_ft),
            "heading": round(self.heading),
            "ground_speed_kt": round(self.ground_speed_kt),
            "on_ground": self.on_ground,
            "observed": self.observed,
            "source": self.source or ("simulator" if self.observed
                                      else "invented"),
            "static": self.static,
            "vertical_rate_fpm": round(self.vertical_rate_fpm),
            "distance_nm": round(self.distance_nm, 1),
            # What it is doing and why, which is the whole point of being able
            # to see it: an aeroplane going around on the map is one that was
            # told to go around a moment ago, and the strip says so.
            "order": self.order,
            "intent": self.target.intent,
            "assigned_heading": (round(self.assigned_heading)
                                 if self.assigned_heading is not None else None),
            "assigned_altitude_ft": (round(self.assigned_altitude_ft)
                                     if self.assigned_altitude_ft is not None
                                     else None),
            "assigned_speed_kt": (round(self.assigned_speed_kt)
                                  if self.assigned_speed_kt is not None else None),
        }

    @property
    def position(self) -> str:
        """The controller position this flight is working right now."""
        return _POSITION_OF_PHASE.get(self.phase, "TWR")

    @property
    def is_heavy(self) -> bool:
        return bool(self.aircraft.wake)

    def describe(self, language: str = "en") -> str:
        """How the controller points this aeroplane out to somebody else.

        By make, which is what is actually said: "follow the Airbus on short
        final", never "follow the A320". The manufacturer names are proper
        nouns and survive translation; the article in front of one does not,
        so it comes from a table.
        """
        return article(make_of(self.type_code), language)


# What a controller calls each type on the radio. Grouped by make, because
# that is the level of detail a sequencing instruction actually carries: you
# are told to follow the Airbus, not the A320.
_MAKE: tuple[tuple[tuple[str, ...], str], ...] = (
    (("A319", "A320", "A321", "A20N", "A21N", "A306", "A333", "A359", "A388"),
     "Airbus"),
    (("B737", "B738", "B38M", "B37M", "B752", "B753", "B763", "B77W", "B77L",
      "B788", "B789", "B78X"), "Boeing"),
    (("E190", "E195", "E75L", "CRJ9"), "Embraer"),
    (("AT76", "DH8D"), "ATR"),
    (("C152", "C172", "C182", "C25A"), "Cessna"),
    (("PA28", "PA34", "P28A"), "Piper"),
    (("DA40", "DA42"), "Diamond"),
    (("SR22",), "Cirrus"),
    (("BE36",), "Beechcraft"),
    (("TBM9",), "TBM"),
    (("PC12",), "Pilatus"),
    (("R44",), "Robinson"),
    (("TB20", "AT3"), "Socata"),
)

# "the Airbus", in each language the controller may be speaking. French and
# Italian elide before a vowel and do not before a consonant, so each has two
# forms and the make decides which one: "l'Airbus", but "le Boeing".
_ARTICLE: dict[str, tuple[str, str]] = {
    "en": ("the {}", "the {}"),
    "fr": ("l'{}", "le {}"),
    "es": ("el {}", "el {}"),
    "de": ("die {}", "die {}"),
    "it": ("l'{}", "il {}"),
    "pt": ("o {}", "o {}"),
}

# "traffic on the runway", which is why you are going around.
_RUNWAY_TRAFFIC: dict[str, str] = {
    "en": "traffic on the runway",
    "fr": "trafic sur la piste",
    "es": "tráfico en la pista",
    "de": "Verkehr auf der Piste",
    "it": "traffico in pista",
    "pt": "tráfego na pista",
}

# "traffic landing", which is why you are being held on the runway.
_LANDING_TRAFFIC: dict[str, str] = {
    "en": "traffic landing, {}",
    "fr": "trafic à l'atterrissage, {}",
    "es": "tráfico aterrizando, {}",
    "de": "landender Verkehr, {}",
    "it": "traffico in atterraggio, {}",
    "pt": "tráfego a aterrar, {}",
}


def article(make: str, language: str = "en") -> str:
    """``"Airbus"`` -> ``"the Airbus"``, ``"l'Airbus"``, ``"il Boeing"``."""
    if not make:
        return ""
    vowel, consonant = _ARTICLE.get(language, _ARTICLE["en"])
    frame = vowel if make[0].upper() in "AEIOU" else consonant
    return frame.format(make)


def landing_traffic(make_phrase: str, language: str = "en") -> str:
    """Why an aircraft is being held on the runway rather than released."""
    if not make_phrase:
        return ""
    return _LANDING_TRAFFIC.get(language, _LANDING_TRAFFIC["en"]).format(
        make_phrase)


def runway_traffic(language: str = "en") -> str:
    """Why an aircraft on short final is being sent around.

    Not which aeroplane: a crew at two hundred feet is told to go around and
    told why in three words, and whether the thing on the runway is an Airbus
    is a conversation for afterwards.
    """
    return _RUNWAY_TRAFFIC.get(language, _RUNWAY_TRAFFIC["en"])


def make_of(type_code: str) -> str:
    """The manufacturer a type belongs to, for pointing it out on frequency."""
    code = (type_code or "").upper()
    for codes, make in _MAKE:
        if code in codes:
            return make
    return ""


# Which frequency a phase happens on. A pilot hears an exchange only if they
# are tuned to the same one, which is what keeps the ground frequency full of
# taxi instructions and the tower frequency full of takeoff clearances.
_POSITION_OF_PHASE: dict[str, str] = {
    "clearance": "DEL",
    "taxi": "GND",
    # At the holding point: asking for the runway, and waiting to hear.
    "holding": "TWR",
    "ready": "TWR",
    "waiting": "TWR",
    "joining": "TWR",
    "lineup": "TWR",
    "takeoff": "TWR",
    "departed": "TWR",
    "inbound": "APP",
    "final": "TWR",
    "landed": "TWR",
    "taxi_in": "GND",
    # The circuit, and the aeroplane that has just been sent round it again.
    # All of it belongs to the tower, which is the position that put it there.
    "upwind": "TWR",
    "crosswind": "TWR",
    "downwind": "TWR",
    "base": "TWR",
    "go_around": "TWR",
}

# The legs of a circuit, which are flown rather than timed.
_CIRCUIT_PHASES = frozenset({"upwind", "crosswind", "downwind", "base"})

# The shape of that circuit lives in :mod:`wilcoatc.atc.circuit`, where the
# tower reads the user's position against the same numbers these aeroplanes
# fly: how far out before the crosswind turn, how wide the downwind, how far
# past the threshold before base, how close to the centreline is final.

def _turn_radius_nm(speed_kt: float, bank_deg: float = 25.0) -> float:
    """How wide a rate-limited turn is at this speed, in nautical miles."""
    return (speed_kt * speed_kt
            / (11.26 * math.tan(math.radians(bank_deg)))) / 6076.0


# How close to the start of the downwind counts as having got there.
JOIN_REACHED_NM = 0.5

# How an aeroplane gets back onto the centreline: it aims at a point this far
# ahead of itself along it, and turns no more than this to do it. Aiming at a
# point rather than correcting by an angle is what stops it weaving -- and
# aiming at all is what was missing, so an aeroplane rolling out of a base
# turn a little wide flew a final parallel to the runway rather than to it.
INTERCEPT_LEAD_NM = 1.0
INTERCEPT_LIMIT_DEG = 30.0

# How far before the turning point the tower decides whether this aeroplane is
# turning base. A controller watching an aeroplane run down the downwind says
# "extend downwind" before it turns, not after: an instruction that arrives
# while the crew is already in the turn is an instruction that arrived late.
BASE_LOOKAHEAD_NM = 0.8

# Where a departure is heading before anybody has said otherwise: the initial
# climb in the clearance, and the level it settles at on the way out.
INITIAL_CLIMB_FT = 3000.0
# Climbing out, the speed is held at V2 plus a little to this height above the
# runway, and only then allowed to build to the climb speed.
ACCELERATE_AT_FT = 1000.0
CLIMB_TO_FT = 5000.0

# --------------------------------------------------------------------------
# aeroplanes somebody else is flying
# --------------------------------------------------------------------------
#
# How much of what the simulator reports is this airport's business. A busy
# region has hundreds of aircraft in it and almost none of them are working the
# field the user is at; putting one on the strip because it happened to be
# thirty thousand feet over the top is the fastest way to make the traffic list
# useless.

# The live ones on the scenery (TrafficInjector._live_pose). The centreline
# is the pilot's figure (30 Oct 2026); the rest UNVERIFIED, chosen: a
# heading this close to the runway's is lined up with it, the final is this
# long, a runway with no published length this long, and a taxiway this
# close is the one it is on.
LIVE_CENTRELINE_M = 40.0
LIVE_ALIGNED_DEG = 15.0
LIVE_FINAL_M = 3.0 * 1852.0
LIVE_RUNWAY_M = 3000.0
LIVE_TAXIWAY_M = 40.0
# Rolled onto a stand (after LocalTC): within 150 m, ahead, at taxi speed,
# at a walking pace, for 45 minutes at most.
LIVE_STAND_M = 150.0
LIVE_AHEAD_DEG = 70.0
LIVE_TAXI_IN_KT = 30.0
LIVE_ROLL_MS = 1.5
LIVE_PARKED_S = 45 * 60.0
M_PER_DEG = 111_320.0
# A hectopascal of QNH near the ground, in feet (ISA: 8.3 m).
FT_PER_HPA = 27.3
# The pilot's runway against the live ones (after LocalTC): the pilot on a
# final inside 4 nm counts as using it; a live arrival this close to the
# threshold with the pilot on the runway, or this little behind the pilot
# on the same final, is sent round; held departures go 90 s apart; a hold
# short is said once a minute at most; on the ground one taxiing gives way
# to the pilot this close in front. UNVERIFIED: the 2 and 2.5 nm and the
# 150 m / 45 degrees, chosen.
PILOT_FINAL_NM = 4.0
LIVE_GO_AROUND_NM = 2.0
LIVE_BEHIND_NM = 2.5
RELEASE_GAP_S = 90.0
HOLD_SHORT_EVERY_S = 60.0
GIVE_WAY_M = 150.0
GIVE_WAY_DEG = 45.0

#: How far from the field an observed aeroplane still counts as working it.
OBSERVED_RANGE_NM = 30.0
#: And how far above it. An airliner in the cruise over the top is not an
#: arrival, however close to the field it is.
OBSERVED_CEILING_FT = 12_000.0
#: Rolling rather than taxiing, and moving rather than parked.
ROLLING_KT = 40.0
TAXIING_KT = 3.0
# Pushed back: slow, and moving the other way from the nose (atc/live.py).
PUSHBACK_KT = 8.0
PUSHBACK_DEG = 120.0
# How far from the field's reference point somebody else's parked aeroplane
# can be and still be standing on one of its stands.
PARKED_RANGE_NM = 3.0
#: How far out an observed arrival is cleared to land. Far enough to be a
#: landing clearance rather than a commentary, near enough that it is
#: certainly landing off this approach.
LANDING_CLEARANCE_NM = 8.0
#: How many of the simulator's aeroplanes are worked at once. A big airport
#: under FSLTL has a hundred aircraft parked on it, and a hundred strips is
#: not a traffic list -- it is a wall. The ones that matter are the ones near
#: the runway, so that is what the limit keeps.
OBSERVED_LIMIT = 20

# How far above the field an aeroplane has to be before it counts as having
# left it. On the tick the wheels come off it is still at field elevation, and
# calling that "climbing out" is how a departure reaches the map at zero feet.
AIRBORNE_BY_FT = 60.0


# How fast an aeroplane of each kind is going, by what it is doing. Rounded
# numbers rather than a performance model: the point is that a landing aircraft
# is slower than a departing one and that neither of them is stationary.
_SPEED = {
    "clearance": 0.0, "taxi": 12.0, "holding": 0.0, "ready": 0.0,
    "waiting": 0.0, "taxi_in": 12.0,
}

# The phases an aeroplane is flown through rather than placed in. Anything in
# here is handed to the flight model the moment it enters it, wherever it came
# from, so a flight put together by hand behaves like one the field made.
_FLYING_PHASES = frozenset({
    "lineup", "takeoff", "departed", "inbound", "joining", "final", "landed",
    "go_around",
}) | _CIRCUIT_PHASES

# Every phase a flight can be in, which is what the panel needs a label for.
# "parked" is the apron: this program's aeroplanes at the gates and FSLTL's,
# which are on the map and never on a strip.
PHASES: frozenset[str] = frozenset(_SPEED) | _FLYING_PHASES | {"parked"}

# Three degrees, in feet per nautical mile.
GLIDE_FT_PER_NM = 318.0

# What an arrival is doing at the threshold and what it is doing before it
# starts slowing down, with the deceleration spread over the miles between.
APPROACH_SPEED_KT = 140.0
ARRIVAL_SPEED_KT = 250.0
SLOWING_FROM_NM = 15.0
STABLE_BY_NM = 4.0


def _squawk(code: str) -> str:
    """The real one's code where it is a code, the conspicuity one else."""
    code = (code or "").strip()
    if len(code) == 4 and all(ch in "01234567" for ch in code):
        return code
    return "2000"


def observed_type(raw: str) -> str:
    """The ICAO type designator out of whatever the simulator calls a model.

    Injectors differ. FSLTL writes the designator straight into ``ATC MODEL``
    and the answer is the string itself; the simulator's own aircraft write a
    localisation key -- ``TT:ATCCOM.AC_MODEL_B738.0.text`` -- with the
    designator buried in the middle of it. Anything that does not come out as
    a plausible designator comes out empty, and an aeroplane with no type is
    still an aeroplane on the runway.
    """
    text = (raw or "").strip().upper()
    if not text:
        return ""
    if "AC_MODEL" in text:
        text = text.split("AC_MODEL", 1)[1]
    text = text.strip("_. ").split(".", 1)[0].strip("_ ")
    # "A350-900" and "A320 neo" are a designator with something after it, and
    # the something is not part of it. A name with no designator in it at all
    # -- "Cherokee" -- comes back empty rather than truncated.
    head = text.replace("-", " ").split()
    text = "".join(c for c in (head[0] if head else "") if c.isalnum())
    # FSLTL's models say what they are the way a ramp agent would: "737",
    # "ATR76". Read off a running 2024 at Madeira, and each is one type.
    text = _MODEL_NAMES.get(text, text)
    if 3 <= len(text) <= 4 and text[0].isalpha() and any(
            c.isdigit() for c in text):
        return text
    return ""


#: What FSLTL writes in ATC MODEL for the types it does not give a designator
#: for, and the designator. A bare Boeing number is the variant FSLTL's base
#: model of it is.
_MODEL_NAMES: dict[str, str] = {
    "737": "B738", "738": "B738", "739": "B739", "747": "B744", "748": "B748",
    "757": "B752", "767": "B763", "777": "B77W", "787": "B789",
    "ATR72": "AT76", "ATR75": "AT75", "ATR76": "AT76", "ATR42": "AT46",
    "ATR45": "AT45", "ATR46": "AT46", "A320NEO": "A20N", "A321NEO": "A21N",
    "A319NEO": "A19N", "A330NEO": "A339",
}


def approach_speed(distance_nm: float,
                   performance: Performance | None = None) -> float:
    """How fast an arrival that far from the threshold is going.

    Threshold speed close in, arrival speed far out, and the deceleration
    spread over the miles between. With a performance it is that type's own
    two numbers, which is what keeps a Dash 8 off an airliner's profile and a
    Cessna off both.
    """
    stable = APPROACH_SPEED_KT if performance is None else performance.approach_kt
    arrival = ARRIVAL_SPEED_KT if performance is None else performance.cruise_kt
    if distance_nm <= STABLE_BY_NM:
        return stable
    if distance_nm >= SLOWING_FROM_NM:
        return arrival
    across = ((distance_nm - STABLE_BY_NM)
              / (SLOWING_FROM_NM - STABLE_BY_NM))
    return stable + across * (arrival - stable)


@dataclass
class Call:
    """One transmission, ready to be spoken."""

    text: str
    language: str
    from_pilot: bool
    position: str
    speaker: str
    flight: Flight
    at: float = 0.0
    # Something that cannot wait its turn behind two taxi instructions. A
    # go-around is the whole of this category.
    urgent: bool = False
    # Which exchange this is a half of, so that the two halves live or die
    # together. A call, its clearance and its readback are one thing to
    # listen to, and hearing the middle of it is worse than hearing none of
    # it: a controller answering an aeroplane that never called sounds like
    # the frequency is missing transmissions, which it is. Zero for a call
    # that is not part of an exchange at all.
    exchange: int = 0
    # The channel it is said on, where known: what decides who hears it.
    mhz: float = 0.0


#: How big a parked aeroplane is taken to be when it is standing nowhere the
#: layout calls a stand: the radius the simulator gives an airliner's gate,
#: and a light aircraft's tie-down.
AIRLINER_FOOTPRINT_M = 18.0
LIGHT_FOOTPRINT_M = 6.0


@dataclass
class ParkedObserved:
    """Somebody else's aeroplane standing on this airport.

    Not a :class:`Flight`: it is on no frequency and in no sequence. It is
    on the map, and the ground handling calls on it.
    """

    object_id: int
    callsign: str
    registration: str = ""
    type_code: str = ""
    # The simulator title, for the door table.
    title: str = ""
    since: float = 0.0
    # A live flight (atc/live.py): it holds its stand and counts against the
    # parked ones, but nothing in the simulator is it yet, so nothing is
    # sent to it -- no vehicles, no doors.
    live: bool = False
    category: str = ""
    # A live one's airline, for its model and livery; and when it was put
    # on the stand, for how long it stays when the feed no longer hears it.
    operator: str = ""
    parked_at: float = 0.0
    latitude: float = 0.0
    longitude: float = 0.0
    heading: float = 0.0
    altitude_ft: float = 0.0

    @property
    def key(self) -> str:
        """What the handling knows it by. FSLTL's parked aeroplanes carry
        made-up callsigns that can repeat; the object id cannot."""
        return f"{self.callsign or 'AI'}#{self.object_id}"

    def snapshot(self, field) -> dict:
        """The shape :meth:`Flight.snapshot` has, as far as the map reads it."""
        return {
            "callsign": self.callsign or self.registration or "AI",
            "spoken": "",
            "type": self.type_code,
            "ifr": True,
            "operator": self.operator,
            "language": "",
            "phase": "parked",
            "arriving": False,
            "runway": "",
            "sequence": 0,
            "position": "",
            "lat": round(self.latitude, 6),
            "lon": round(self.longitude, 6),
            "altitude_ft": round(self.altitude_ft),
            "heading": round(self.heading),
            "ground_speed_kt": 0,
            "on_ground": True,
            "observed": True,
            "source": "live" if self.live else "simulator",
            "static": True,
            "vertical_rate_fpm": 0,
            "distance_nm": round(haversine_nm(
                field.latitude, field.longitude,
                self.latitude, self.longitude), 1) if field else 0.0,
            "order": {"kind": "", "detail": ""},
            "intent": "parked",
            "assigned_heading": None,
            "assigned_altitude_ft": None,
            "assigned_speed_kt": None,
        }


@dataclass
class UserAircraft:
    """The aeroplane the person is flying, as the traffic needs to see it.

    The AI used to work a field with one aeroplane missing from it: the one
    that mattered. A departure was held for an arrival that was another AI and
    never for the user, and an AI landed on a runway the user was sitting on.

    This is the fix, and it is deliberately thin. The traffic does not need to
    know what the user was cleared for or what they read back -- only where
    they are, which way they are pointing and whether they are on the ground.
    Everything a controller would decide from looking out of the window, and
    nothing that would need the two halves of the program to agree about
    state.
    """

    callsign: str = ""
    type_code: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    altitude_ft: float = 0.0
    heading: float = 0.0
    ground_speed_kt: float = 0.0
    on_ground: bool = True

    @property
    def has_position(self) -> bool:
        return not (abs(self.latitude) < 1e-6 and abs(self.longitude) < 1e-6)

    @property
    def make(self) -> str:
        """What a controller would call it when pointing it out to somebody."""
        return make_of(self.type_code)


# How wide a runway is, and how long, in miles -- for deciding whether an
# aeroplane is on one. Generous on both counts: half a mile of centreline
# either side of the threshold catches an aeroplane lined up on a displaced
# one, and two miles catches the far end of the longest runway there is.
RUNWAY_WIDTH_NM = 0.05
RUNWAY_LENGTH_NM = 2.2

# How far off the runway heading an aeroplane on the pavement can be and still
# be lined up on it rather than crossing it. The same figure the surface watch
# uses on the user, and for the same reason.
LINED_UP_DEG = 35.0

#: Positions that belong to the airspace rather than to the aerodrome.
#:
#: The distinction only matters when one is standing in for another. A field
#: that publishes its own Approach has an approach controller; a field that
#: publishes the enroute Center frequency because that is who you call in the
#: overlying airspace does not, and reading the second as the first is what
#: made an uncontrolled strip look staffed.
_ENROUTE_POSITIONS = frozenset({"CTR"})


def _is_enroute_standin(station, position: str) -> bool:
    """Whether this station is the airspace above the field, not the field.

    True only when the lookup substituted one position for another *and* what
    came back is enroute. A Ground standing in for a Delivery is left alone:
    plenty of towered fields really do issue clearances on the ground
    frequency, and the traffic sounds right doing the same.
    """
    return (station.position != position
            and station.position in _ENROUTE_POSITIONS)


@dataclass
class _Field:
    """The airport the injector is working."""

    ident: str = ""
    name: str = ""
    size_rank: int = 3
    # Airline field or airfield, and why (wilcoatc.atc.fleet). Logged as a
    # "fleet" row when the field is set.
    kind: fleets.FieldKind | None = None
    departure_runway: str = ""
    arrival_runway: str = ""
    weather: Weather | None = None
    # The traffic frequency, where a field has one. Set when the field has no
    # controllers at all, which is what makes it an uncontrolled one.
    advisory: Station | None = None
    # The field's own aerodrome information service (an AFIS, or a UK air/
    # ground "Radio"), where it has one and no controller. Kortrijk has
    # nothing else, and with neither this nor a traffic frequency the
    # injector worked it as a towered field with no tower.
    afis: Station | None = None
    stations: dict[str, Station] = field(default_factory=dict)
    # Where the field is, and where its runway thresholds are, so an aeroplane
    # can be put on a centreline rather than merely near an airport.
    latitude: float = 0.0
    longitude: float = 0.0
    elevation_ft: float = 0.0
    thresholds: dict[str, tuple[float, float, float]] = field(
        default_factory=dict)          # runway -> (lat, lon, heading)
    # How the runway itself lies: runway -> (threshold elevation, elevation
    # at the far end, length in nm). Only where both ends are published;
    # everything else is taken to be at the field's elevation.
    surfaces: dict[str, tuple[float, float, float]] = field(
        default_factory=dict)


class TrafficInjector:
    """Runs the other aeroplanes on the frequency.

    The engine calls :meth:`poll` on the situation loop and speaks whatever
    comes back. Everything else -- who exists, what phase they are in, whether
    the runway is free -- is decided here.
    """

    def __init__(self, navdb: NavDB, seed: int | None = None,
                 density: float = 1.0, callsigns: str = "auto"):
        self.navdb = navdb
        self.density = max(0.0, float(density))
        # "auto" is whatever the field would carry; "registration" makes every
        # generated aeroplane a private one, spelled out.
        self.callsigns = (callsigns or "auto").strip().lower()
        # No seed means a different session every time, which is what the
        # setting promises. This used to fall back to a constant, and every
        # visit to a field replayed the same draw: the same two airlines at
        # Funchal, in the same order, on every flight.
        self.random = random.Random(seed)
        self.field = _Field()
        self.flights: list[Flight] = []
        self.pending: list[Call] = []
        # Numbers the exchanges, so the halves of one can be recognised as
        # halves of one after they have been queued separately.
        self._exchanges = 0
        self.runway_free_at = 0.0
        # Every callsign this field has issued, live or finished. A number is
        # never handed out twice: an aeroplane that has gone to the gate is
        # still the aeroplane the user heard, and hearing the same callsign
        # come back on a different type an hour later is the exact opposite of
        # what a consistent callsign means.
        self._used_callsigns: set[str] = set()
        self._spawn_at = 0.0
        # When the flights were last moved, so they travel at their own speed
        # rather than at the rate the caller happens to poll.
        self._flown_at = 0.0
        self._phraseology: dict[tuple[str, str], object] = {}
        # The pilot's profile: a pinned region is the controller's words to
        # the traffic as well as to the pilot, on the same frequency.
        from .profile import DEFAULT as DEFAULT_PROFILE

        self.profile = DEFAULT_PROFILE
        self._pilots: dict[str, PilotSpeech] = {}
        # The aeroplane the person is flying, when the engine has told us
        # about it. None means the traffic works the field as though it were
        # alone on it, which is what a session with no simulator gets.
        self.user: UserAircraft | None = None
        # The tower does not re-examine the whole field on every poll; once a
        # second is faster than anything it could usefully say.
        self._watched_at = 0.0
        # Whether anything is reporting the simulator's own traffic. Read only
        # to decide how much invented traffic to add on top of it.
        self.observed_count = 0
        # Aeroplanes the pilot asked for, waiting for the next tick to join
        # the roster -- see :meth:`spawn_on_runway`.
        self._asked_for: list[Flight] = []
        # The airport's taxiways, when the simulator has given them. Without
        # them the ground is placed, as it always was; with them it is driven.
        self.ground: groundwork.GroundLayout | None = None
        # Taxiway intersections and who has them, so two aeroplanes do not
        # arrive at one from different directions at once.
        self._reserved: dict[object, str] = {}

        # Whether an invented aeroplane has to be in the simulator before it
        # exists here. The engine turns this on: the rule is that nothing is
        # heard, drawn on the map or sequenced against unless the pilot can
        # also see it out of the window. Off, the injector works on its own,
        # which is what the tests of the traffic itself want.
        self.requires_sim = False
        # Whether the simulator is drawing aeroplanes at all right now. With
        # ``requires_sim`` and this off, nothing is invented.
        self.can_invent = True
        # Invented aeroplanes asked of the simulator and not yet confirmed by
        # it, with the moment each is given up on. Kept off ``flights`` so
        # that nothing -- the radio, the map, the runway, the sequence --
        # can see an aeroplane that may never appear.
        self._materialising: list[Flight] = []
        self._materialise_by: dict[str, float] = {}
        # How many the simulator will draw at once. More than that would
        # never appear, time out and be invented again, round and round.
        self.max_invented: int | None = None
        # Whether an airline has its own livery on a type, when something
        # can say -- FSLTL's table, through the engine. With it, an airline
        # is given the types it is painted for, so an Air France flight is
        # an Air France aeroplane out of the window rather than a white one.
        self.has_livery = None
        # Whether an aeroplane may push: its doors read shut. Set by the
        # engine from the door layer; None means nobody is reading doors
        # and nothing waits. A departure that is not ready does not ask for
        # taxi -- a real crew does not call for push with a door open -- and
        # tries again a few seconds later.
        self.can_push = None

        # The aeroplanes standing at the gates, going nowhere yet. Kept off
        # ``flights`` because nothing about them is the radio's business:
        # they are not sequenced, not spoken to and not in anybody's way on
        # a taxiway. ``parked`` is how many there should be, and 0 -- the
        # default, which the tests of the moving traffic want -- is none.
        self.statics: list[Flight] = []
        # The schedule: departures board on their stands before they call,
        # and arrivals hold their stand from the start. Off, a departure
        # is a parked aeroplane chosen the moment it is wanted, as always.
        self.schedule = False
        self.boarding_s = 240.0
        self.boarding_lead_s = 120.0
        self.parked = 0
        self._parked_at = 0.0
        self._stands_filled = False
        # Whether to wait, before setting the stands out, for the simulator
        # to have reported what is already standing on them. The first fill
        # used to go on the first tick, two seconds before FSLTL's parked
        # aeroplanes had been read, and put an aeroplane on every stand one
        # of theirs was already on.
        self.await_observed = False
        self._observed_read = False
        self._first_poll: float | None = None
        # Invent only these types (ICAO codes), for testing a model: empty
        # is the airlines' real fleets.
        self.force_types: tuple[str, ...] = ()
        # Where somebody else's parked aeroplanes are -- FSLTL, AIG, the
        # simulator's own -- which are not worked (see _works_here) but are
        # standing on stands nothing here may be put on.
        self._parked_seen: list[tuple[float, float]] = []
        # And what they are, by simulator object, for the map and the ground
        # handling: an FSLTL apron is forty aeroplanes going nowhere, and the
        # map is about the window. ``since`` is when one stopped, if it was
        # seen moving first, else 0 for "it was there when we looked".
        self._parked_observed: dict[int, ParkedObserved] = {}
        # The live ones (atc/live.py): rolled onto a stand and held there,
        # let go to move again, and seen in the air (so taxiing in).
        self._live_stands: dict[int, dict] = {}
        self._live_moving: set[int] = set()
        self._live_flew: set[str] = set()
        # The pilot's runway (_protect_the_pilot): how a live one is held and
        # let go (LiveRoster.hold, handed in by the engine), the ones sent
        # around and flown by us, and when a hold short was last said and a
        # held departure last let go.
        self.hold_live = None
        self._taken_over: set[int] = set()
        # The one we fly for each taken over, and since when it has been
        # parked, waiting for the feed to hear the real one again.
        self._flown_for: dict[int, Flight] = {}
        self._parked_since: dict[int, float] = {}
        self._hold_short_said = -math.inf
        self._last_release = -math.inf
        # What each live one was last put on (runway, taxiway, stand, or
        # nothing), for traffic --live --dry-run and the status line.
        self.live_snapped: dict[str, str] = {}

    # ------------------------------------------------------------------
    # the field
    # ------------------------------------------------------------------

    def set_field(self, ident: str, departure_runway: str = "",
                  arrival_runway: str = "", weather: Weather | None = None,
                  size_rank: int | None = None,
                  tuned: Station | None = None) -> None:
        """Point the injector at an airport, or move it to a new one.

        Moving fields clears the roster: the aeroplanes at the last airport are
        no longer on this frequency and are not carried along.
        """
        ident = (ident or "").upper()
        if ident != self.field.ident:
            self.flights.clear()
            self._asked_for.clear()
            self._reserved.clear()
            if self.ground is not None and self.ground.ident != ident:
                self.ground = None
            self._materialising.clear()
            self._materialise_by.clear()
            self.statics.clear()
            self._stands_filled = False
            self._parked_seen = []
            self._parked_observed.clear()
            self.pending.clear()
            self._used_callsigns.clear()
            self._phraseology.clear()
            self._pilots.clear()
            self.runway_free_at = 0.0
            self._watched_at = 0.0
            airport = self.navdb.airport(ident)
            self.field = _Field(
                ident=ident,
                name=airport.spoken or airport.name if airport else ident,
                size_rank=(size_rank if size_rank is not None
                           else (airport.size_rank if airport else 3)),
            )
            # The lookup falls back to the traffic frequency for every
            # position, which is right for a pilot deciding what to tune and
            # wrong here: it would give this field a clearance delivery that
            # does not exist. Only real controllers are kept, and a field with
            # none of them is worked the way it really is -- everybody
            # announcing themselves on one frequency, nobody answering.
            #
            # The fallback also runs *upwards*, and that is the case this has
            # to refuse. A grass strip with no tower and one published Center
            # frequency -- which is most of the United States -- answered
            # ``station_for("APP")`` with Albuquerque Center, because that is
            # genuinely the next thing above it. Kept, it made the field look
            # controlled: the traffic worked an approach controller a hundred
            # miles away, nothing went out on the traffic frequency, and a
            # pilot sitting on it heard an empty channel. So a position only
            # counts when the station really is that position. An enroute
            # centre overhead is not an aerodrome's controller.
            self.field.stations = {
                position: station
                for position in ("DEL", "GND", "TWR", "APP", "DEP")
                if (station := self.navdb.station_for(ident, position))
                and station.is_controller
                and not _is_enroute_standin(station, position)
            }
            self.field.advisory = self.navdb.station_for(ident, "CTAF")
            self.field.afis = self._aerodrome_information(ident)
            self.field.kind = fleets.classify(self.navdb, ident, size_rank)
            decision("fleet", airport=ident, **self.field.kind.log_fields(),
                     density=self.density,
                     in_play_scaled=self._in_play(self.field.kind),
                     movement_gap_scaled_s=round(
                         self._movement_gap_s(self.field.kind)),
                     parked=fleets.parked_for(self.field.kind, self.parked))
            if airport is not None:
                self.field.latitude = airport.lat
                self.field.longitude = airport.lon
                self.field.elevation_ft = airport.elev_ft
            self.field.thresholds = self._thresholds(ident)
            self.field.surfaces = self._surfaces(ident)
        # A field that publishes several frequencies for one position --
        # Brussels has four Grounds -- had its traffic on the one the lookup
        # ranked first while the pilot sat on another, and since a call is
        # heard by its channel (engine._on_this_channel) the pilot heard
        # nobody at all, for a day, at every such field. The traffic works
        # the position on the frequency the pilot is tuned to.
        if (tuned is not None and (tuned.ident or "").upper() == ident
                and tuned.position in self.field.stations
                and tuned.is_controller):
            self.field.stations[tuned.position] = tuned
        if weather is not None:
            self.field.weather = weather
        self.field.departure_runway = departure_runway or self.field.departure_runway
        self.field.arrival_runway = (arrival_runway or departure_runway
                                     or self.field.arrival_runway)

        # The user may not have been given a runway yet -- they have not asked
        # for anything -- but the traffic is already flying, and a takeoff
        # clearance without a runway in it is not a takeoff clearance. So the
        # field runs the configuration the wind implies until the controller
        # says otherwise, which is the same runway the user will be given.
        if not self.field.departure_runway:
            self.field.departure_runway = self._wind_runway()
            self.field.arrival_runway = (self.field.arrival_runway
                                         or self.field.departure_runway)

    def set_ground(self, layout: "groundwork.GroundLayout | None") -> None:
        """The taxiways of the field being worked, from the simulator.

        Only for this field, and only for aeroplanes made from now on: one
        already sitting somewhere it was placed is left to finish as it
        started.
        """
        if layout is None or not layout:
            return
        if layout.ident != self.field.ident:
            return
        if layout is not self.ground:
            # A static aeroplane's stand is a number in the old layout.
            self.statics.clear()
            self._stands_filled = False
        self.ground = layout

    # How long a runway is taken to be where nothing says otherwise. Only used
    # to bound where an entry or an exit may be found.
    RUNWAY_LENGTH_M = 3500.0

    def _runway_xy(self, runway: str):
        """A threshold in the ground layout's plane, with its heading."""
        end = self._threshold(runway)
        if end is None or self.ground is None:
            return None
        return self.ground.to_xy(end[0], end[1]), end[2]

    def _thresholds(self, ident: str) -> dict[str, tuple[float, float, float]]:
        """Every runway end, as (latitude, longitude, heading).

        Ends without published coordinates are left out rather than guessed:
        an aeroplane on a centreline that is not where the runway is would be
        worse than one drawn at the field.
        """
        found: dict[str, tuple[float, float, float]] = {}
        for runway in self.navdb.runways(ident):
            for end, heading, lat, lon in runway.ends():
                if not end or lat is None or lon is None:
                    continue
                if heading is None:
                    from ..navdata.db import _heading_from_ident

                    heading = _heading_from_ident(end)
                if heading is None:
                    continue
                found[normalize_runway(end)] = (float(lat), float(lon),
                                                float(heading))
        return found

    def _threshold(self, runway: str):
        """The end an aeroplane is using, or None if it is not published."""
        return self.field.thresholds.get(normalize_runway(runway))

    def _surfaces(self, ident: str) -> dict[str, tuple[float, float, float]]:
        """Every runway end's elevation, the far end's, and the length.

        The published airport elevation is the highest point of its
        runways. Funchal's is 192 ft and the threshold of runway 05 is at
        147: an arrival aimed at the published figure levelled off 45 ft
        above the runway, slowed to a walk up there, and taxied to the gate
        through the air.
        """
        found: dict[str, tuple[float, float, float]] = {}
        for runway in self.navdb.runways(ident):
            low, high = runway.le_elev_ft, runway.he_elev_ft
            if low is None or high is None:
                continue
            length_nm = max(0.1, (runway.length_ft or 0.0) / 6076.1)
            if runway.le_ident:
                found[normalize_runway(runway.le_ident)] = (
                    float(low), float(high), length_nm)
            if runway.he_ident:
                found[normalize_runway(runway.he_ident)] = (
                    float(high), float(low), length_nm)
        return found

    def _threshold_ft(self, runway: str) -> float:
        """How high the landing end of a runway is."""
        surface = self.field.surfaces.get(normalize_runway(runway))
        return surface[0] if surface else self.field.elevation_ft

    def _surface_ft(self, flight: Flight) -> float:
        """How high the ground is under an aeroplane on or near its runway.

        Short of the threshold it is the threshold; along the runway it
        follows the slope to the far end; past that, the far end.
        """
        surface = self.field.surfaces.get(normalize_runway(flight.runway))
        end = self._threshold(flight.runway)
        if surface is None or end is None:
            return self.field.elevation_ft
        near, far, length_nm = surface
        along, _across = self._runway_frame(flight, end[2])
        share = min(1.0, max(0.0, along / length_nm))
        return near + (far - near) * share

    def _place(self, flight: Flight) -> None:
        """Where an aeroplane is when the flight model is not flying it.

        Everything on a taxiway: the stand it is pushing back from, the queue
        at the holding point, the walk to the gate afterwards. No open dataset
        publishes taxiway geometry this could fly an aeroplane along, so those
        are placed from the phase rather than invented as a route.

        From lining up to slowing down after landing it is flown instead, by
        :mod:`wilcoatc.atc.flying`, and this leaves it alone.
        """
        field_lat, field_lon = self.field.latitude, self.field.longitude
        if not field_lat and not field_lon:
            return
        if flight.airborne_model:
            return

        elevation = self.field.elevation_ft
        end = self._threshold(flight.runway)
        phase = flight.phase
        flight.ground_speed_kt = _SPEED.get(phase, 0.0)
        flight.vertical_rate_fpm = 0.0
        flight.on_ground = True
        flight.altitude_ft = elevation

        if phase in ("holding", "ready", "waiting") and end is not None:
            lat, lon, heading = end
            # One aeroplane lines up; the rest wait behind it. Without this
            # they all sit on the threshold, which looks like one aeroplane on
            # a map and like a pile-up in the simulator.
            waiting = [f for f in self.flights
                       if f is not flight and not f.arriving
                       and f.phase in ("holding", "ready", "waiting")
                       and f.next_at <= flight.next_at]
            along, across = -0.03, 0.02 + 0.025 * len(waiting)
            flight.latitude, flight.longitude = project(lat, lon, heading, along)
            flight.latitude, flight.longitude = project(
                flight.latitude, flight.longitude,
                (heading + 90.0) % 360.0, across)
            flight.heading = heading
            return

        # Everything else is on the apron. Spread them around the field rather
        # than stacking them on the centre of it.
        bearing = (hash(flight.written) % 360)
        flight.latitude, flight.longitude = project(
            field_lat, field_lon, bearing, 0.35)
        flight.heading = (bearing + 90.0) % 360.0

    # ------------------------------------------------------------------
    # flying them
    # ------------------------------------------------------------------

    def _start_flying(self, flight: Flight) -> None:
        """Hand an aeroplane to the flight model.

        Everything after this point is integrated -- a heading swings round at
        three degrees a second, a runway takes half a minute to roll down --
        so this only has to say where it is the moment the model takes over.
        """
        end = self._threshold(flight.runway)
        if end is None:
            return
        lat, lon, heading = end
        elevation = self.field.elevation_ft
        threshold_ft = self._threshold_ft(flight.runway)
        flight.airborne_model = True
        flight.heading = heading

        if flight.arriving and not flight.ifr and flight.phase == "inbound":
            # A light aircraft arriving at an airfield is not on an instrument
            # approach and does not appear on the centreline eight miles out.
            # It comes in from wherever it has been, at circuit height, and
            # joins where the tower tells it to.
            side = -1.0 if flight.pattern_direction != "right" else 1.0
            from_where = (heading + 180.0 + 45.0 * side) % 360.0
            flight.latitude, flight.longitude = project(
                lat, lon, from_where, max(1.0, flight.distance_nm))
            flight.altitude_ft = (elevation
                                  + flight.performance.pattern_agl_ft + 500.0)
            flight.ground_speed_kt = flight.performance.cruise_kt
            flight.heading = (from_where + 180.0) % 360.0
            flight.on_ground = False
            return

        if flight.arriving:
            # On the extended centreline at the distance it is reporting, on a
            # three-degree path, at the speed that distance implies.
            behind = (heading + 180.0) % 360.0
            flight.latitude, flight.longitude = project(
                lat, lon, behind, max(0.2, flight.distance_nm))
            flight.altitude_ft = glide_altitude(threshold_ft,
                                                flight.distance_nm,
                                                GLIDE_FT_PER_NM)
            flight.ground_speed_kt = approach_speed(flight.distance_nm,
                                                    flight.performance)
            flight.on_ground = False
            return

        # A departure joins the model at the threshold, stopped -- or, having
        # taxied onto the runway itself, wherever it now is.
        if not flight.on_layout:
            flight.latitude, flight.longitude = project(lat, lon, heading,
                                                        0.04)
        flight.altitude_ft = self._surface_ft(flight)
        flight.ground_speed_kt = 0.0
        flight.on_ground = True

    def _stop_flying(self, flight: Flight) -> None:
        """Take an aeroplane off the model and back onto a taxiway."""
        flight.airborne_model = False
        flight.assigned_heading = None
        flight.assigned_altitude_ft = None
        flight.assigned_speed_kt = None
        flight.target.clear()

    def _aim(self, flight: Flight) -> None:
        """What this aeroplane is trying to do, right now.

        The phase says what it would be doing if nobody had said anything; an
        assigned heading, level or speed says what it was told, and outranks
        it. That order is the whole of what makes an instruction an
        instruction: the aeroplane goes back to flying the phase only when the
        assignment is taken away.
        """
        target = flight.target
        target.hold = False
        target.turn = ""
        target.accel_kt_s = None
        target.max_descent_fpm = None
        end = self._threshold(flight.runway)
        heading = end[2] if end is not None else flight.heading
        elevation = self.field.elevation_ft
        # Where the wheels are, which on a sloping runway is not the field's
        # published elevation; see _surfaces.
        surface = self._surface_ft(flight)
        performance = flight.performance
        phase = flight.phase

        if phase == "lineup":
            target.heading = heading
            target.altitude_ft = surface
            target.speed_kt = 0.0
            target.hold = True
            target.intent = "lined up"
        elif phase == "takeoff":
            target.heading = heading
            target.speed_kt = performance.climb_kt
            if flight.on_ground:
                target.altitude_ft = surface
                target.accel_kt_s = performance.takeoff_kt_s
                target.intent = "rolling"
            else:
                target.altitude_ft = elevation + INITIAL_CLIMB_FT
                target.intent = "climbing out"
                if flight.altitude_ft < surface + ACCELERATE_AT_FT:
                    # V2 plus a little to the acceleration height, nose high,
                    # rather than flattening out to gather speed off the
                    # runway.
                    target.speed_kt = min(performance.climb_kt,
                                          performance.rotate_kt + 20.0)
        elif phase == "departed":
            target.heading = heading
            target.speed_kt = performance.cruise_kt
            target.altitude_ft = elevation + CLIMB_TO_FT
            target.intent = "climbing out"
        elif phase in _CIRCUIT_PHASES:
            self._circuit(flight, heading, elevation)
        elif phase == "go_around":
            target.heading = heading
            target.speed_kt = performance.climb_kt
            target.altitude_ft = elevation + performance.pattern_agl_ft
            target.intent = "going around"
        elif phase == "joining":
            target.heading = self._bearing_to_join(flight, heading)
            target.speed_kt = performance.cruise_kt * 0.85
            target.altitude_ft = elevation + performance.pattern_agl_ft
            target.intent = "joining downwind"
        elif phase in ("inbound", "final"):
            target.heading = self._intercept(flight, heading)
            target.speed_kt = approach_speed(flight.distance_nm, performance)
            target.altitude_ft = glide_altitude(
                self._threshold_ft(flight.runway), flight.distance_nm,
                GLIDE_FT_PER_NM)
            target.intent = "on approach" if phase == "inbound" else "on final"
            height = flight.altitude_ft - surface
            if phase == "final" and height < FLARE_FT:
                # The flare: the descent eased off from the glide path's rate
                # to a gentle touchdown over the last forty feet.
                glide_fpm = flight.ground_speed_kt * GLIDE_FT_PER_NM / 60.0
                target.max_descent_fpm = max(
                    FLARE_FPM, glide_fpm * max(0.0, height) / FLARE_FT)
                # Down to the runway under it, not to the threshold's
                # height: the flare carries it past the threshold, and on a
                # runway that falls away (Madeira's 23 drops 45 ft) it held
                # the threshold's altitude above the slope for ever.
                target.altitude_ft = surface
        elif phase == "landed":
            target.heading = heading
            target.altitude_ft = surface
            target.speed_kt = 0.0
            target.intent = "landing roll"
        else:
            target.speed_kt = 0.0
            target.hold = True
            target.intent = phase

        # And what it was told, which beats all of that.
        if flight.assigned_heading is not None:
            target.heading = flight.assigned_heading
        if flight.assigned_altitude_ft is not None:
            target.altitude_ft = flight.assigned_altitude_ft
        if (flight.assigned_speed_kt is not None and not target.hold
                and not flight.on_ground):
            target.speed_kt = flight.assigned_speed_kt

    def _circuit(self, flight: Flight, runway_heading: float,
                 elevation: float) -> None:
        """Fly the circuit, one leg at a time.

        The legs are decided by where the aeroplane actually is rather than by
        a timer, in the runway's own coordinates: how far along the takeoff
        direction it is from the threshold, and how far off to the side. That
        is what makes "extend downwind" a real instruction -- the aeroplane
        goes past the point it would have turned, and keeps going until it is
        told to turn base.
        """
        target = flight.target
        performance = flight.performance
        pattern_ft = elevation + performance.pattern_agl_ft
        along, across = self._runway_frame(flight, runway_heading)
        side = -1.0 if flight.pattern_direction != "right" else 1.0
        # A circuit speed, not a cruise: an airliner going round at two
        # hundred and twenty knots flew a circuit four miles wide.
        target.speed_kt = min(performance.cruise_kt * 0.8,
                              performance.approach_kt * 1.45)
        target.altitude_ft = pattern_ft

        if flight.phase == "upwind":
            target.heading = runway_heading
            target.intent = "upwind"
            if along >= CIRCUIT_UPWIND_NM:
                flight.phase = "crosswind"
        elif flight.phase == "crosswind":
            target.heading = (runway_heading + 90.0 * side) % 360.0
            target.intent = "crosswind"
            if across * side >= CIRCUIT_WIDTH_NM:
                flight.phase = "downwind"
        elif flight.phase == "downwind":
            target.heading = (runway_heading + 180.0) % 360.0
            target.intent = ("extending downwind" if flight.extending
                             else "downwind")
            # An extension that has been transmitted but not yet read back is
            # still an extension. Turning base in the gap would be a crew that
            # heard the tower and did it anyway.
            holding = flight.extending or self._already_told(flight, "extend")
            if not holding and along <= -self._base_nm(flight):
                if self._room_on_final(
                        -along, ignore=flight,
                        within_s=self._rollout_s(flight, across)):
                    flight.phase = "base"
                else:
                    # No gap by the time it would be there. It carries on
                    # down the downwind rather than starting a turn it would
                    # have to abandon a mile from the centreline -- inside
                    # its own turning circle, which is across the approach.
                    flight.extending = True
        elif flight.phase == "base":
            target.heading = (runway_heading - 90.0 * side) % 360.0
            target.intent = "base"
            target.altitude_ft = elevation + performance.pattern_agl_ft * 0.6
            target.speed_kt = performance.approach_kt * 1.15
            # Rolling out on the centreline rather than through it: the turn
            # starts one turn radius short of it.
            if abs(across) <= max(CIRCUIT_CENTRELINE_NM,
                                  _turn_radius_nm(flight.ground_speed_kt)):
                # Committed: the gap was judged for this moment when the base
                # turn began. Turning away from here, inside its own turning
                # circle, is what took the last one across the approach; if
                # the spacing has closed anyway, the tower's watch on final
                # sends one of them round.
                flight.phase = "final"
                flight.step = 0
                flight.arriving = True
                flight.distance_nm = max(1.0, -along)

    def _join_point(self, flight: Flight,
                    runway_heading: float) -> tuple[float, float] | None:
        """Where the downwind starts, for an aeroplane joining the circuit.

        Abeam the upwind end, out on the circuit side. A VFR arrival told to
        join left downwind flies to this point and turns onto the leg, which
        is what it was told to do -- rather than appearing on it.
        """
        end = self._threshold(flight.runway)
        if end is None:
            return None
        return circuitry.join_point(end[0], end[1], runway_heading,
                                    flight.pattern_direction)

    def _bearing_to_join(self, flight: Flight, runway_heading: float) -> float:
        """Which way to point to get to the start of the downwind."""
        point = self._join_point(flight, runway_heading)
        if point is None:
            return flight.heading
        return bearing_deg(flight.latitude, flight.longitude, point[0], point[1])

    def _distance_to_join(self, flight: Flight) -> float:
        """How far it still has to fly before it is on the downwind."""
        end = self._threshold(flight.runway)
        point = self._join_point(flight, end[2]) if end else None
        if point is None:
            return 0.0
        return haversine_nm(flight.latitude, flight.longitude,
                            point[0], point[1])

    def _intercept(self, flight: Flight, runway_heading: float) -> float:
        """The heading that closes the centreline, rather than parallels it.

        An aeroplane that rolls out of a turn a few hundred yards wide and
        then flies the runway heading stays a few hundred yards wide all the
        way to the threshold. This aims it at a point on the centreline ahead
        of it, which brings it in and then keeps it there.
        """
        _along, across = self._runway_frame(flight, runway_heading)
        angle = math.degrees(math.atan2(-across, INTERCEPT_LEAD_NM))
        angle = max(-INTERCEPT_LIMIT_DEG, min(INTERCEPT_LIMIT_DEG, angle))
        return (runway_heading + angle) % 360.0

    def _runway_frame(self, flight: Flight,
                      runway_heading: float) -> tuple[float, float]:
        """Where an aeroplane is in the runway's own coordinates.

        ``(along, across)`` in nautical miles from the threshold: along is
        positive in the direction of takeoff, across is positive to the right
        of it. Every decision about the circuit, the approach and whether
        somebody is on the runway is made in those two numbers.
        """
        end = self._threshold(flight.runway)
        if end is None:
            return 0.0, 0.0
        return circuitry.runway_frame(flight.latitude, flight.longitude,
                                      end[0], end[1], runway_heading)

    def _after_flying(self, flight: Flight) -> None:
        """Read back off the aeroplane whatever the rest of the module asks it.

        The distance to run and whether it is on the ground are facts about
        where it now is, not numbers something else has to remember to keep in
        step with it.
        """
        end = self._threshold(flight.runway)
        if end is None:
            return
        along, _across = self._runway_frame(flight, end[2])
        if flight.arriving or flight.phase in _CIRCUIT_PHASES:
            flight.distance_nm = max(0.0, -along)
        else:
            flight.distance_nm = max(0.0, along)

        performance = flight.performance
        if flight.phase == "takeoff":
            if not flight.on_ground:
                pass                     # once off, it stays off
            elif flight.ground_speed_kt < performance.rotate_kt:
                flight.rotate_at = None
            else:
                now = self._flown_at
                if flight.rotate_at is None:
                    flight.rotate_at = now
                # Rotated, and off once the nose is high enough.
                if now - flight.rotate_at >= LIFTOFF_AFTER_S:
                    flight.on_ground = False
        elif flight.phase in ("lineup", "landed"):
            flight.on_ground = True
        else:
            flight.on_ground = (flight.altitude_ft
                                <= self._surface_ft(flight) + 3.0)

    def positions(self, invented_only: bool = False,
                  parked: bool = False) -> list[dict]:
        """Where every live aeroplane is, for the map and the simulator.

        ``invented_only`` leaves out the ones the simulator is already
        holding. The map wants all of them -- they are all out of the window
        -- and the injector in :mod:`wilcoatc.integrations.aitraffic` wants
        only the ones it is responsible for, or it would create a second copy
        of every FSLTL aeroplane a foot away from the first.

        ``parked`` adds the aeroplanes standing at the gates, this program's
        and everybody else's. The map wants those too; the strips do not,
        because a dozen aeroplanes that will say nothing would bury the
        three that will.
        """
        # The live ones (atc/live.py) are observed -- nobody instructs them --
        # but this program draws them, so they go to the injector with the
        # invented ones; FSLTL's and the simulator's never do.
        found = [f.snapshot() for f in self.flights
                 if not f.done and f.positioned
                 and not (invented_only and f.observed and f.source != "live")
                 and not (invented_only and f.redraw)]
        if invented_only:
            # A recovery's redraw (``_recover``): left out of the simulator's
            # list once, so its object is removed, and created again on the
            # next tick at the same spot.
            for flight in self.flights:
                flight.redraw = False
        if parked and not invented_only:
            # The apron as well: this program's own parked aeroplanes, and
            # the ones FSLTL and the simulator stood there. Neither is on
            # the radio, both are out of the window.
            found += [f.snapshot() for f in self.statics
                      if f.positioned and (f.drawn or not self.requires_sim)]
            found += [one.snapshot(self.field) for one in
                      self._parked_observed.values()]
        if not invented_only:
            # The map also wants where each one is going: the rest of its
            # route, as the aeroplane itself would fly it. The simulator does
            # not, and is asked for these a dozen times a second.
            by_callsign = {f.written: f for f in self.flights}
            for snapshot in found:
                flight = by_callsign.get(snapshot["callsign"])
                if flight is not None:
                    snapshot.update(self.route_ahead(flight))
        if invented_only:
            # What the simulator still has to make. It is asked for these and
            # nobody else hears of them until it has.
            found += [f.snapshot() for f in self._materialising
                      if f.positioned]
            # The parked ones last, so that where the simulator can only draw
            # so many it is the ones standing still that go without. They
            # are for the simulator only: the panel's list and its maps are
            # of the traffic on the frequency, and a dozen aeroplanes that
            # will say nothing would bury the three that will.
            found += [f.snapshot() for f in self.statics if f.positioned]
            # And the live ones standing at the gates, which this program
            # draws as well.
            found += [one.snapshot(self.field) for one in
                      self._parked_observed.values() if one.live]
            # And how each is moving, unrounded, with the way ahead on the
            # ground: the simulator draws them between ticks from this
            # (wilcoatc.atc.follow), rather than jumping once a tick.
            by_callsign = {f.written: f for f in self.flights}
            by_callsign.update({f.written: f for f in self._materialising})
            by_callsign.update({f.written: f for f in self.statics})
            for snapshot in found:
                flight = by_callsign.get(snapshot["callsign"])
                if flight is not None:
                    snapshot["motion"] = self.motion_of(flight)
            # The simulator draws the first traffic.in_simulator_limit of
            # these: the ones that matter to the pilot first (the pilot's
            # rule, 30 Oct 2026) -- the runway and the final, then the rest
            # of the air, then the taxiways, then the parked ones nearest
            # first. An aeroplane on final is never left out for one parked
            # across the airport.
            found.sort(key=self._relevance)
        return found

    def _relevance(self, snapshot: dict) -> tuple:
        phase = snapshot.get("phase", "")
        on_ground = bool(snapshot.get("on_ground"))
        if phase in ("final", "takeoff", "lineup", "landed", "go_around") \
                or (not on_ground and phase in ("departed", "circuit")):
            rank = 0
        elif not on_ground:
            rank = 1
        elif phase not in ("parked",) and (
                snapshot.get("ground_speed_kt", 0) >= TAXIING_KT
                or phase in ("taxi", "taxi_in", "pushback", "holding")):
            rank = 2
        else:
            rank = 3
        user = self.user
        if user is not None and user.has_position:
            here = (user.latitude, user.longitude)
        else:
            here = (self.field.latitude, self.field.longitude)
        far = haversine_nm(here[0], here[1], snapshot.get("lat", here[0]),
                           snapshot.get("lon", here[1]))
        return rank, far

    #: The most route points handed to the injector's follower: a few
    #: hundred metres of taxiway, all it can draw ahead within a tick.
    MOTION_POINTS = 12

    def motion_of(self, flight: Flight) -> dict:
        """How an aeroplane is moving, for drawing it between ticks."""
        motion = {
            "t": self._flown_at or 0.0,
            "lat": flight.latitude, "lon": flight.longitude,
            "alt": flight.altitude_ft, "heading": flight.heading,
            "gs": flight.ground_speed_kt, "vs": flight.vertical_rate_fpm,
            "on_ground": bool(flight.on_ground),
            "arriving": bool(flight.arriving),
            # A light aircraft rotates less and climbs flatter.
            "light": flight.performance.climb_fpm <= LIGHT_PERFORMANCE.climb_fpm,
            "rotate_at": flight.rotate_at,
            "surface_ft": (self._surface_ft(flight) if flight.runway
                           else self.field.elevation_ft),
            "route": [], "push": 0,
        }
        taxi = flight.taxi
        layout = self.ground
        if (taxi is not None and layout is not None and not taxi.done
                and not flight.airborne_model):
            ahead = taxi.points[taxi.index:taxi.index + self.MOTION_POINTS]
            motion["route"] = [layout.to_latlon(x, z) for x, z in ahead]
            # And the two points behind it, so a corner it is half way round
            # is the same curve from one tick to the next.
            motion["behind"] = [layout.to_latlon(x, z) for x, z in
                                taxi.points[max(0, taxi.index - 2):taxi.index]]
            motion["push"] = max(0, taxi.push - taxi.index)
            if taxi.face is not None and taxi.index + len(ahead) >= len(taxi.points):
                motion["face"] = taxi.face
        return motion

    def standing(self) -> list:
        """Every invented aeroplane on a stand, for the ground handling.

        The ones set out at the gates, an arrival that has just stopped on
        its stand, and a departure boarded and waiting for its push: each
        with its stand's geometry and which of those it is, in the shape
        :class:`wilcoatc.atc.handling.Standing` wants. Somebody else's
        aeroplanes are never in it -- the simulator handles its own -- and
        neither is anything that has started to move.
        """
        from .handling import (ARRIVED, BOARDING, DEPARTING, PARKED, Standing,
                               is_light)

        if self.ground is None:
            return []
        found: list = []

        def add(flight: Flight, status: str) -> None:
            if flight.stand is None or not flight.positioned:
                return
            if not 0 <= flight.stand < len(self.ground.stands):
                return
            stand = self.ground.stands[flight.stand]
            x, z = self.ground.to_xy(flight.latitude, flight.longitude)
            # Boarding, its doors close when the boarding does: the holds
            # first, and the bags must be on by then.
            closes = (flight.boarding_at + self.boarding_s
                      if status == BOARDING and flight.boarding_at else None)
            found.append(Standing(
                callsign=flight.written, x=x, z=z, heading=flight.heading,
                radius=stand.radius, status=status, since=flight.parked_at,
                light=is_light(flight.type_code, flight.operator is None),
                gate=stand.gate, type_code=flight.type_code,
                operator=flight.operator.icao if flight.operator else "",
                closes_at=closes))

        for flight in self.statics:
            if flight.drawn or not self.requires_sim:
                add(flight, BOARDING if flight.boarding else PARKED)
        for one in self._parked_observed.values():
            # On the stand it is nearest, if it is on one; otherwise on its
            # own footprint, which is all the geometry a vehicle needs.
            x, z = self.ground.to_xy(one.latitude, one.longitude)
            stand = self.ground.stand_at(x, z)
            light = is_light(one.type_code, not one.type_code)
            # A live one is drawn by this program, so it is handled as one of
            # ours: found by its callsign, not by somebody else's object.
            found.append(Standing(
                callsign=one.callsign if one.live else one.key,
                x=x, z=z, heading=one.heading,
                radius=(stand.radius if stand is not None
                        else (LIGHT_FOOTPRINT_M if light
                              else AIRLINER_FOOTPRINT_M)),
                status=ARRIVED if one.since else PARKED, since=one.since,
                light=light, gate=stand.gate if stand is not None else False,
                type_code=one.type_code, observed=not one.live,
                object_id=0 if one.live else one.object_id, title=one.title))
        for flight in self.flights:
            if flight.observed or flight.done or flight.static:
                continue
            if not flight.on_ground or flight.ground_speed_kt > 0.5:
                continue
            if flight.taxi is not None:
                continue
            if flight.arriving:
                if flight.parked_at:
                    add(flight, ARRIVED)
            else:
                add(flight, DEPARTING)
        return found

    def flight_named(self, callsign: str) -> Flight | None:
        """One of this program's aeroplanes by callsign, wherever it is."""
        for flight in (self.statics + self.flights + self._materialising
                       + self._asked_for):
            if flight.written == callsign and not flight.done:
                return flight
        return None

    def apron_state(self, callsign: str
                    ) -> tuple[str, tuple[float, float] | None]:
        """Where one of this program's aeroplanes is in its day, for the
        schedule's choice of model, and the stand it turns round on.

        One of "static" (standing on its stand), "booked" (to depart,
        boarding not started), "boarding", "departing" (boarded, on its
        stand, not yet moving), "arrived" (blocked in, still on the
        radio), "arriving" (on its way to a stand it holds) or "moving".
        The stand is in the layout's metres, or None.
        """
        def stand_xy(index: int | None) -> tuple[float, float] | None:
            if (index is None or self.ground is None
                    or not 0 <= index < len(self.ground.stands)):
                return None
            stand = self.ground.stands[index]
            return stand.x, stand.z

        for flight in self.statics:
            if flight.written == callsign:
                kind = ("boarding" if flight.boarding
                        else "booked" if flight.boarding_at else "static")
                return kind, stand_xy(flight.stand)
        for flight in self.flights + self._materialising + self._asked_for:
            if flight.written != callsign or flight.done:
                continue
            still = flight.on_ground and flight.ground_speed_kt <= 0.5
            if flight.arriving:
                if flight.parked_at and still:
                    return "arrived", stand_xy(flight.stand)
                return "arriving", stand_xy(flight.planned_stand
                                            if flight.planned_stand is not None
                                            else flight.stand)
            if still and flight.taxi is None and flight.stand is not None:
                return "departing", stand_xy(flight.stand)
            return "moving", None
        return "moving", None

    # How long the simulator has to produce an invented aeroplane before it
    # is dropped. Long enough for a refused title and a second one to be
    # tried; short enough that a flight nobody will ever see does not sit in
    # the queue for the next one.
    MATERIALISE_S = 30.0

    def _admit(self, flight: Flight, now: float) -> None:
        """Put a new invented aeroplane where it belongs: on the roster, or
        waiting for the simulator to draw it first."""
        if self.requires_sim:
            self._materialising.append(flight)
            self._materialise_by[flight.written] = now + self.MATERIALISE_S
        else:
            self.flights.append(flight)

    def confirm(self, drawn, failed, now: float
                ) -> tuple[list[Flight], list[Flight]]:
        """Admit what the simulator has drawn and drop what it has not.

        ``drawn`` and ``failed`` answer for a callsign: whether the
        simulator has the aeroplane, and whether it has given up on it.
        Returns the flights that have just become real and the ones that
        were dropped, so the caller can say which.
        """
        confirmed: list[Flight] = []
        dropped: list[Flight] = []
        waiting: list[Flight] = []
        for flight in self._materialising:
            if drawn(flight.written):
                # It has been sitting still while the simulator made it; it
                # does not then say two things in the same second.
                flight.next_at = max(flight.next_at, now + 1.0)
                self.flights.append(flight)
                confirmed.append(flight)
            elif (failed(flight.written)
                  or now > self._materialise_by.get(flight.written, now)):
                flight.done = True
                dropped.append(flight)
            else:
                waiting.append(flight)
        self._materialising = waiting
        for flight in confirmed + dropped:
            self._materialise_by.pop(flight.written, None)
        # A parked one the simulator cannot draw is simply not there. It is
        # not reported: nobody was told about it.
        for flight in self.statics:
            flight.drawn = bool(drawn(flight.written))
        self.statics = [f for f in self.statics
                        if f.drawn or not failed(f.written)]
        return confirmed, dropped

    def _drop_invented(self) -> None:
        """Take every invented aeroplane away: the simulator is not drawing
        them, so they are not there. What somebody else put in the
        simulator stays -- it is out of the window whatever this does."""
        self._asked_for.clear()
        self._materialising.clear()
        self._materialise_by.clear()
        self.statics.clear()
        self._stands_filled = False
        gone = {id(f) for f in self.flights if not f.observed}
        if gone:
            self.flights = [f for f in self.flights if id(f) not in gone]
            self.pending = [c for c in self.pending
                            if id(c.flight) not in gone]

    def _wind_runway(self) -> str:
        """The runway in use for the wind, or the longest one if there is none."""
        weather = self.field.weather
        found = self.navdb.best_runway(
            self.field.ident,
            weather.wind_dir if weather else 0.0,
            weather.wind_kt if weather else 0.0,
            variable=bool(weather and weather.variable),
            gust_kt=weather.gust_kt if weather else 0.0,
        )
        return found[0] if found else ""

    # ------------------------------------------------------------------
    # what the pilot hears
    # ------------------------------------------------------------------

    @property
    def quiet(self) -> bool:
        """Whether there is nothing here to run at all.

        The density says how much to *invent*, not whether the frequency is
        worked. A pilot running FSLTL with the invented traffic turned right
        down has a full airport and a silent radio otherwise -- which was the
        first thing that went wrong when the two sources were put together.
        """
        if not self.field.ident:
            return True
        # One the pilot put there by hand is worked whatever the density:
        # turning the invented traffic off is not asking for that one to
        # stand frozen on the runway.
        return (self.density <= 0 and not self.observed_count
                and not self.flights and not self._asked_for
                and not self._materialising)

    def advance(self, now: float) -> None:
        """Move everybody along without collecting what they said.

        For when there is nothing to hear it on: the aeroplanes are still
        there, still taxiing and still landing, and whatever they transmit
        waits in the queue until somebody tunes in or it goes stale.
        """
        if self.quiet:
            return
        self._spawn(now)
        self._advance(now)
        self.pending = [c for c in self.pending if c.at > now]
        self.flights = [f for f in self.flights if not f.done]

    def poll(self, now: float, listening_on: str = "",
             mhz: float = 0.0) -> list[Call]:
        """Advance every flight and return what is due on this frequency.

        ``listening_on`` is the position the user is tuned to. Transmissions on
        any other frequency still happen -- the flights keep moving -- but the
        user does not hear them, exactly as on a real radio.

        ``mhz`` is the channel tuned, when known, and then decides instead:
        a call is heard when it is said on the same channel. Ground and
        Tower sharing one are both heard; a field's second Approach
        frequency does not hear the first's.
        """
        if self.quiet:
            return []

        if self._first_poll is None:
            self._first_poll = now
        self._spawn(now)
        self._advance(now)

        due: list[Call] = []
        keep: list[Call] = []
        for call in self.pending:
            if call.at > now:
                keep.append(call)
            elif self._heard_on(call, listening_on, mhz):
                due.append(call)
            # A call on another frequency is simply dropped: it happened, the
            # user was not listening to it.
        self.pending = keep
        self.flights = [f for f in self.flights if not f.done]
        return due

    def _heard_on(self, call: Call, listening_on: str, mhz: float) -> bool:
        if not listening_on and not mhz:
            return True
        if not call.mhz:
            call.mhz = self._freq(call.position)
        if mhz and call.mhz:
            return same_channel(call.mhz, mhz, 0)
        return not listening_on or call.position == listening_on

    def traffic_ahead(self, arriving: bool = True) -> list[Flight]:
        """The flights in front of the user, nearest first.

        Used by the controller to sequence the user into the stream: "number
        two, follow the Airbus on a four mile final" is only honest if there is
        an Airbus on a four mile final.
        """
        if arriving:
            found = [f for f in self.flights
                     if f.arriving and not f.stuck
                     and f.phase in ("inbound", "final")]
            found.sort(key=lambda f: f.distance_nm)
        else:
            found = [f for f in self.flights
                     if not f.arriving and not f.stuck
                     and f.phase in ("holding", "ready", "waiting", "lineup")]
            found.sort(key=lambda f: f.sequence)
        return found

    def runway_is_busy(self, now: float) -> bool:
        """Whether somebody is on the runway right now."""
        return now < self.runway_free_at

    # ------------------------------------------------------------------
    # making aeroplanes
    # ------------------------------------------------------------------

    def _wanted(self) -> int:
        """How many invented aeroplanes this field should have.

        Less whatever somebody else has already put there -- FSLTL's, or the
        real ones from the live feed -- counted in its own category only
        (:meth:`_room`). A pilot running FSLTL has a full airport and does
        not want a second one invented on top of it; but a field with two
        real aeroplanes on it is still a quiet field, so the invented traffic
        fills the rest rather than standing down the moment anything is seen.
        """
        wanted = sum(self._room().values())
        if self.max_invented is not None:
            wanted = min(wanted, self.max_invented)
        return wanted

    def _density_by_category(self) -> dict[str, int]:
        """The field's in-play figure, split into the categories its invented
        traffic is made of (airline and cargo, or light aircraft)."""
        kind = self.field.kind
        roster = ()
        if kind is None or kind.airline:
            key = (self.field.ident, self.field.size_rank)
            if getattr(self, "_roster_for", None) != key:
                self._roster_for = key
                self._roster = roster_for(self.field.ident,
                                          self.field.size_rank)
            roster = self._roster
        return livemod.wanted_by_category(kind, self._in_play(kind), roster)

    def _observed_by_category(self) -> dict[str, int]:
        """The aeroplanes somebody else flies that work this field, by
        category. What is overflying never had a strip (_works_here)."""
        counted = {c: 0 for c in livemod.CATEGORIES}
        for f in self.flights:
            if f.observed and not f.done and not f.stuck:
                counted[livemod.flight_category(f)] += 1
        return counted

    def _room(self) -> dict[str, int]:
        """How many invented aeroplanes each category still has room for:
        the field's density in that category less the real ones in it. A
        real Cessna crossing a big airport's zone takes nothing from its
        airliners, and a real airliner nothing from an airfield's Cessnas."""
        return livemod.room(self._density_by_category(),
                            self._observed_by_category())

    def _invented_by_category(self) -> dict[str, int]:
        counted = {c: 0 for c in livemod.CATEGORIES}
        for f in (self.flights + self._materialising
                  + [s for s in self.statics if s.boarding_at]):
            if not f.observed and not f.done:
                counted[livemod.flight_category(f)] += 1
        return counted

    def _steer(self) -> str | None:
        """The category the next invented aeroplane should be, when real ones
        have filled the others; None leaves the draw as it always was."""
        if not any(self._observed_by_category().values()):
            return None
        room = self._room()
        invented = self._invented_by_category()
        short = [c for c in livemod.CATEGORIES if room[c] - invented[c] > 0]
        return short[0] if len(short) == 1 else None

    def _in_play(self, kind: "fleets.FieldKind | None") -> int:
        """Aeroplanes in play at once at this field, scaled by the density:
        the field's kind for a general-aviation field (fleet.classify), the
        size rank's for an airport, as it always was."""
        base = (kind.in_play if kind is not None and kind.in_play
                else CONCURRENT_BY_RANK.get(self.field.size_rank, 3))
        return int(round(base * self.density))

    def _movement_gap_s(self, kind: "fleets.FieldKind | None") -> float:
        """Seconds between one aeroplane starting and the next."""
        base = (kind.movement_gap_s if kind is not None and kind.movement_gap_s
                else 55.0)
        return base / max(0.25, self.density)

    def _booked(self) -> int:
        """Departures booked off the apron, not yet moving. They are in play
        already: counted only once they moved, two booked on top of two in
        the air made four at Kortrijk."""
        return sum(1 for f in self.statics if f.boarding_at)

    def _spawn(self, now: float) -> None:
        if self.requires_sim and not self.can_invent:
            self._drop_invented()
            return
        if self._asked_for:
            asked, self._asked_for = self._asked_for, []
            for flight in asked:
                self._admit(flight, now)
        if self.schedule or any(f.boarding_at for f in self.statics):
            # Without the schedule only the board button ever books one.
            self._work_the_boarding(now)
        self._fill_stands(now)
        self._retire_at_gate()
        self._say_the_blend()
        invented = (sum(1 for f in self.flights if not f.observed)
                    + len(self._materialising) + self._booked())
        if invented >= self._wanted() or now < self._spawn_at:
            return
        flight = self._new_flight(now, category=self._steer())
        if flight is not None:
            self._admit(flight, now)
        # Spacing between one starting and the next: the field's own, scaled
        # by how busy the pilot asked for it to be.
        gap = self._movement_gap_s(self.field.kind)
        self._spawn_at = now + self.random.uniform(gap * 0.6, gap * 1.4)

    # ------------------------------------------------------------------
    # the ones standing at the gates
    # ------------------------------------------------------------------

    # How often an emptied stand is filled again once the apron has been
    # set out, and how far from the pilot it has to be: an aeroplane that
    # appears on the next stand is a conjuring trick, one that appears
    # across the apron is one that was towed there while nobody looked.
    RESTOCK_S = 90.0
    # How long the first fill waits for the simulator's own aeroplanes to
    # have been read, when a reader is attached. Two polls and a margin.
    OBSERVED_GRACE_S = 8.0
    RESTOCK_CLEAR_M = 150.0

    def _parked_wanted(self) -> int:
        """How many aeroplanes should be standing at the gates.

        As many as were asked for, but never so many that the moving traffic
        has nowhere to go: an arrival with no stand free rolls off the
        runway and is taken away, so enough are left empty for everything
        that could be on the move at once, and a couple over.
        """
        if self.ground is None or self.parked <= 0:
            return 0
        airline_field = self._airline_field
        stands = len(self.ground.stands_for(airliner=airline_field))
        spare = CONCURRENT_BY_RANK.get(self.field.size_rank, 3) + 2
        # The field's share of ``traffic.parked``, which is an airport's:
        # twelve Cessnas stood at Kortrijk.
        wanted = fleets.parked_for(self.field.kind, self.parked)
        # Less the real ones standing here in the categories this field
        # parks: a live airliner at the gate is one fewer to invent, a light
        # aeroplane on the grass at a big airport is none fewer.
        mine = livemod.field_categories(self.field.kind)
        wanted -= sum(1 for one in self._parked_observed.values()
                      if (one.category or livemod.category_of(
                          one.callsign, one.type_code)) in mine)
        return max(0, min(wanted, stands - spare))

    def _fill_stands(self, now: float) -> None:
        """Set the apron out, then keep it about as full as it was.

        All at once the first time -- the pilot is loading in and has not
        looked yet -- and one at a time after that, away from the pilot.
        Most of the restocking is done by the traffic itself: an arrival
        that has parked stays, as one of these, and a departure boards one.
        """
        if now < self._parked_at:
            return
        if (self.await_observed and not self._observed_read
                and self._first_poll is not None
                and now - self._first_poll < self.OBSERVED_GRACE_S):
            return                     # the simulator has not said yet
        wanted = self._parked_wanted()
        first = not self._stands_filled
        while len(self.statics) < wanted:
            flight = self._new_static(now, clear_of_user=not first)
            if flight is None:
                break
            self.statics.append(flight)
            if not first:
                break
        self._stands_filled = True
        self._parked_at = now + self.RESTOCK_S

    def _new_static(self, now: float,
                    clear_of_user: bool = False) -> Flight | None:
        """An aeroplane on a free stand, which will one day be a departure."""
        if self.ground is None:
            return None
        away = []
        user = self.user
        if clear_of_user and user is not None and user.has_position:
            away = [(*self.ground.to_xy(user.latitude, user.longitude),
                     self.RESTOCK_CLEAR_M)]
        flight = self._new_flight(now, arriving=False, parked=False)
        if flight is None:
            return None
        stand = self._free_stand(airliner=flight.operator is not None
                                 or flight.ifr, also=away)
        if stand is None:
            self._used_callsigns.discard(flight.written)
            return None
        self._park(flight, stand)
        self._make_static(flight)
        return flight

    def _make_static(self, flight: Flight) -> None:
        flight.static = True
        flight.phase = "parked"
        flight.step = 0
        flight.sequence = 0
        flight.next_at = float("inf")
        flight.ground_speed_kt = 0.0
        flight.target.clear()
        flight.target.intent = "parked"
        flight.doing = None
        flight.commands.clear()

    # How many parked aeroplanes may be booked or boarding at once, under
    # the schedule. A departure asked for while that many are in hand is
    # one already on its way.
    MAX_BOARDING = 2

    def _book_boarding(self, now: float) -> bool:
        """The schedule's departure: book a parked aeroplane to board.

        It boards after ``boarding_lead_s`` -- the minutes in which its model
        can be changed with every door shut -- for ``boarding_s``, and only
        then calls for its clearance (:meth:`_work_the_boarding`). True when
        the departure is in hand, booked now or already; False when there is
        nobody parked to book, and a departure has to be found another way.
        """
        booked = [f for f in self.statics if f.boarding_at]
        if len(booked) >= self.MAX_BOARDING:
            return True
        ready = [f for f in self.statics
                 if (f.drawn or not self.requires_sim) and not f.boarding_at]
        if not ready:
            return bool(booked)
        flight = self.random.choice(ready)
        flight.boarding_at = now + self.boarding_lead_s
        return True

    def _work_the_boarding(self, now: float) -> None:
        """Start the boarding that is due, and send the boarded off."""
        for flight in list(self.statics):
            if not flight.boarding_at:
                continue
            if not flight.boarding and now >= flight.boarding_at:
                flight.boarding = True
            if flight.boarding and now >= flight.boarding_at + self.boarding_s:
                boarded = self._board_static(now, flight)
                if boarded is not None:
                    self._admit(boarded, now)

    def _board_static(self, now: float,
                      flight: Flight | None = None) -> Flight | None:
        """A departure from an aeroplane already standing at a gate.

        The same aeroplane, in the same simulator object, so nothing
        appears: the one the pilot has been parked beside is the one that
        calls for its clearance and pushes back. ``flight`` is the one the
        schedule booked; without it, one is chosen now.
        """
        if flight is None:
            ready = [f for f in self.statics
                     if f.drawn or not self.requires_sim]
            if not ready:
                return None
            flight = self.random.choice(ready)
        self.statics.remove(flight)
        flight.boarding_at = 0.0
        flight.boarding = False
        flight.static = False
        flight.arriving = False
        flight.runway = self.field.departure_runway
        flight.destination = self._elsewhere()
        flight.parked_at = 0.0
        flight.next_at = now + self.random.uniform(2.0, 12.0)
        flight.sequence = len(self.traffic_ahead(False)) + 1
        flight.target.intent = ""
        flight.phase = ("clearance" if flight.ifr
                        and "DEL" in self.field.stations else "taxi")
        return flight

    # How many aeroplanes may be committed to the runway at once. One a
    # minute is what a single runway does, and the approach is that long: any
    # more than this and the tower is manufacturing a queue it will have to
    # send half of round again. Departures are not limited the same way -- an
    # aeroplane at a holding point costs nothing.
    MAX_ON_APPROACH = 4

    # ------------------------------------------------------------------
    # an aeroplane asked for
    # ------------------------------------------------------------------

    def spawn_on_runway(self, now: float) -> tuple[Flight | None, str]:
        """Put a departure at the runway in use, for the panel's button.

        An ordinary flight from the field's own roster that starts at the
        last step before the roll, so from here the tower works it like any
        other: cleared for takeoff when the runway and the approach allow it,
        handed to departure once it is climbing.

        Lined up when the runway really is free, and at the holding point
        when it is not. The first version refused instead, and a refusal
        reads as the program being wrong: the pilot looks at an empty
        runway -- the aeroplane in the way may be one the simulator never
        drew -- and is told it is occupied. A busy runway is a queue at a
        real field, not a reason to have no aeroplane. And lining one up
        with an arrival inside :attr:`LINEUP_BLOCK_NM` is the mistake the
        tower itself is written not to make: it sent the arrival around.

        Returns the flight and an empty reason, or None and the reason, as a
        key the panel can translate. Whether it was lined up is its phase.

        The flight waits in ``_asked_for`` until the next tick moves it onto
        the roster. The panel calls this from a different thread from the
        one running the traffic, and the tick rebuilds ``flights`` whole, so
        an aeroplane appended to it from here could vanish in between. Where
        the simulator has to draw it first (``requires_sim``), it goes from
        there to waiting for that, like any other invented aeroplane.
        """
        if not self.field.ident:
            return None, "no_field"
        if self.requires_sim and not self.can_invent:
            # Nothing to put it in. An aeroplane on the radio and not on the
            # runway is exactly what the pilot asked this never to do.
            return None, "not_in_sim"
        runway = self.field.departure_runway
        if not runway or self._threshold(runway) is None:
            return None, "no_runway"
        free = not (any(f.phase == "lineup"
                        for f in self._asked_for + self._materialising)
                    or self._somebody_lined_up()
                    or self.runway_occupied(now)
                    or self._traffic_on_final(self.LINEUP_BLOCK_NM)
                    is not None)
        flight = self._new_flight(now, arriving=False, parked=False)
        if flight is None:
            return None, "no_callsign"
        flight.sequence = 0
        legs = self._legs_from_the_ramp(flight)
        if not free:
            # Asked for the runway already; the tower answers it the way it
            # answers every other departure at the holding point.
            flight.phase = "waiting"
            flight.step = 0
            flight.next_at = now + self.random.uniform(3.0, 6.0)
            if legs is not None:
                # At the airport's own holding point, facing the runway.
                out, onto = legs
                self._put(flight, out.points[-1],
                          groundwork._heading(onto.points[1][0]
                                              - out.points[-1][0],
                                              onto.points[1][1]
                                              - out.points[-1][1]))
                flight.line_up = onto
                onto.index = 1
            else:
                self._place(flight)
            self._asked_for.append(flight)
            return flight, ""
        if legs is not None:
            # On the runway where a real line-up ends, not at a point worked
            # out from the threshold.
            _out, onto = legs
            self._put(flight, onto.points[-1], onto.face)
        flight.phase = "lineup"
        flight.step = 2
        # Recorded as already said and done, so the strip reads "lined up"
        # and the tower does not tell it to line up a second time.
        lined_up = self._tell(flight, "line_up", now, runway=runway)
        lined_up.applied = True
        flight.doing = lined_up
        self._start_flying(flight)
        # Long enough for the pilot to find it out of the window before it
        # asks to go.
        flight.next_at = now + self.random.uniform(8.0, 15.0)
        self._asked_for.append(flight)
        return flight, ""

    # -- test tools (traffic.debug.triggers) ------------------------------

    # How long a departure boarded by hand stands booked before its boarding
    # starts: long enough for its model to be swapped with the doors shut.
    BOARD_NOW_LEAD_S = 10.0
    # Where an arrival asked for by hand is put: on final, this far out.
    SHORT_FINAL_NM = 4.0

    def _nearest_to_user(self, options, xy_of):
        user = self.user
        if user is None or not user.has_position or self.ground is None:
            return None
        ux, uz = self.ground.to_xy(user.latitude, user.longitude)
        return min(options, key=lambda one: math.hypot(xy_of(one)[0] - ux,
                                                       xy_of(one)[1] - uz))

    def board_nearest(self, now: float) -> tuple[Flight | None, str]:
        """Board the parked aeroplane nearest the pilot, now.

        With the schedule it is booked to start boarding in
        :attr:`BOARD_NOW_LEAD_S` -- that much pre-boarding so its model can
        change with every door shut -- and departs once it has boarded, as
        any booked departure does. Without it, it is a departure at once.
        Returns the flight and "", or None and a reason key.
        """
        if self.ground is None:
            return None, "no_stands"
        user = self.user
        if user is None or not user.has_position:
            return None, "no_position"
        ready = [f for f in self.statics
                 if (f.drawn or not self.requires_sim) and not f.boarding]
        if not ready:
            return None, "nobody_parked"
        flight = self._nearest_to_user(
            ready, lambda f: self.ground.to_xy(f.latitude, f.longitude))
        flight.by_hand = True
        if self.schedule:
            flight.boarding_at = now + min(self.boarding_lead_s,
                                           self.BOARD_NOW_LEAD_S)
            log.info("traffic.debug: %s booked by hand, boarding at +%.0f s",
                     flight.written, flight.boarding_at - now)
            return flight, ""
        # Due already: the traffic's own tick sends it, on its own thread,
        # rather than this one taking it off the apron under the tick.
        flight.boarding = True
        flight.boarding_at = now - self.boarding_s - 1.0
        log.info("traffic.debug: %s boarded by hand", flight.written)
        return flight, ""

    def arrival_to_nearest_stand(self, now: float) -> tuple[Flight | None, str]:
        """An arrival on short final, holding the free stand nearest the
        pilot. Returns the flight and "", or None and a reason key."""
        if not self.field.ident:
            return None, "no_field"
        if self.requires_sim and not self.can_invent:
            return None, "not_in_sim"
        if self.ground is None:
            return None, "no_stands"
        user = self.user
        if user is None or not user.has_position:
            return None, "no_position"
        if self._threshold(self.field.arrival_runway) is None:
            return None, "no_runway"
        occupied = self._on_ground_obstacles() + [
            (*self.ground.to_xy(lat, lon), 20.0)
            for lat, lon in self._parked_seen]
        stand = self.ground.free_stand(
            True, occupied, self._stands_taken(),
            choose=lambda options: self._nearest_to_user(
                options, lambda s: (s.x, s.z)))
        if stand is None:
            return None, "no_free_stand"
        flight = self._new_flight(now, arriving=True)
        if flight is None:
            return None, "no_callsign"
        flight.planned_stand = stand.index
        if flight.ifr:
            ahead = [f.distance_nm for f in self.traffic_ahead(True)]
            flight.distance_nm = max([self.SHORT_FINAL_NM]
                                     + [d + 3.0 for d in ahead
                                        if d < self.SHORT_FINAL_NM + 3.0])
            flight.altitude_ft = (self.field.elevation_ft
                                  + flight.distance_nm * GLIDE_FT_PER_NM)
            flight.sequence = len(ahead) + 1
            self._start_flying(flight)
        self._asked_for.append(flight)
        log.info("traffic.debug: %s inbound by hand, %.1f nm out, for stand %s",
                 flight.written, flight.distance_nm, stand.index)
        return flight, ""

    def _legs_from_the_ramp(self, flight: Flight):
        """The way from the ramp to this runway, for an aeroplane put on it
        by hand: somewhere to be held and somewhere to line up."""
        if self.ground is None:
            return None
        runway = self._runway_xy(flight.runway)
        if runway is None:
            return None
        threshold, heading = runway
        for stand in self.ground.stands:
            legs = self.ground.departure(stand, (stand.x, stand.z), threshold,
                                         heading, self.RUNWAY_LENGTH_M)
            if legs is not None:
                return legs
        return None

    def _put(self, flight: Flight, xy: tuple[float, float],
             heading: float | None) -> None:
        flight.on_layout = True
        flight.stand = None
        flight.latitude, flight.longitude = self.ground.to_latlon(*xy)
        if heading is not None:
            flight.heading = heading
        flight.altitude_ft = self.field.elevation_ft
        flight.ground_speed_kt = 0.0
        flight.on_ground = True

    # How long a departure waits for the runway before the tower stops
    # taking more arrivals to make a gap for it.
    DEPARTURE_PATIENCE_S = 120.0

    def _departures_kept_waiting(self, now: float) -> bool:
        return any(not f.done and not f.arriving and f.held_since
                   and now - f.held_since > self.DEPARTURE_PATIENCE_S
                   for f in self.flights)

    def _rest_of_taxi(self, flight: Flight, now: float, *,
                      inbound: bool) -> None:
        """The second half of a taxi cut in two, said once the aeroplane is
        at the taxiway it was told to hold short of: to the runway, or to
        the stand. Read back short, as the first half was."""
        if not flight.taxi_rest or flight.taxi_split_xy is None \
                or self.ground is None:
            return
        x, z = self.ground.to_xy(flight.latitude, flight.longitude)
        sx, sz = flight.taxi_split_xy
        if math.hypot(x - sx, z - sz) > REST_OF_TAXI_M:
            return
        rest, flight.taxi_rest, flight.taxi_split_xy = \
            flight.taxi_rest, [], None
        position = "GND" if "GND" in self.field.stations else "TWR"
        if inbound:
            self._exchange(
                flight, now, position,
                controller=lambda c, a, s: c.taxi_to_parking(a, s, rest),
                pilot_back=lambda p, a, s: p.leaving(a))
            return
        runway = self.field.departure_runway or flight.runway
        self._exchange(
            flight, now, position,
            controller=lambda c, a, s: c.taxi_to_runway(a, s, runway, rest),
            pilot_back=lambda p, a, s: p.taxi(a, runway,
                                              route=_spell_route(rest)))

    def _retire_at_gate(self) -> None:
        """Invented flights whose callsign a real one has turned up with go
        once they are standing at a gate."""
        if any(s.retire for s in self.statics):
            for s in self.statics:
                if s.retire:
                    self._used_callsigns.discard(s.written)
            self.statics = [s for s in self.statics if not s.retire]

    def _say_the_blend(self) -> None:
        """A row whenever the mix changes: the density by category, the real
        ones in it (live and simulator), the invented ones, and why."""
        live = {c: 0 for c in livemod.CATEGORIES}
        sim = {c: 0 for c in livemod.CATEGORIES}
        for f in self.flights:
            if f.observed and not f.done:
                (live if f.source == "live" else sim)[
                    livemod.flight_category(f)] += 1
        wanted = self._density_by_category()
        invented = self._invented_by_category()
        summary = (tuple(wanted.values()), tuple(live.values()),
                   tuple(sim.values()), tuple(invented.values()))
        if summary == getattr(self, "_blend_said", None):
            return
        self._blend_said = summary
        room = livemod.room(wanted, {c: live[c] + sim[c] for c in live})
        why = ("no real traffic here: all invented" if not any(live.values())
               and not any(sim.values())
               else "real traffic fills the density: none invented"
               if not any(room.values())
               else "invented fill the difference, by category")
        decision("blend", airport=self.field.ident, wanted=wanted,
                 live=live, simulator=sim, invented=invented, room=room,
                 why=why)

    def _new_flight(self, now: float, arriving: bool | None = None,
                    parked: bool = True,
                    category: str | None = None) -> Flight | None:
        if arriving is None:
            arriving = (self.random.random() < 0.5
                        and len(self._approaching()) < self.MAX_ON_APPROACH)
            if arriving and self._departures_kept_waiting(now):
                # A departure has waited long enough. The stream of arrivals
                # is broken for it, the way a tower leaves a gap; at a
                # runway that has to be backtracked there is otherwise never
                # one long enough.
                arriving = False
        if not arriving and parked and self.ground is not None:
            if self.schedule:
                # Booked to board; it calls once it has. Nothing to admit
                # yet -- unless there is nobody parked to book.
                if self._book_boarding(now):
                    return None
            else:
                boarded = self._board_static(now)
                if boarded is not None:
                    return boarded
        # Which kind of aeroplane this is. Normally the field decides -- an
        # airline field carries airlines -- but a pilot can ask for a
        # frequency of registrations instead, which is a great deal easier to
        # follow than a roomful of telephony names you have to know by heart.
        airline_field = self._airline_field
        if self.callsigns == "registration":
            # A registration, but still the aeroplane the field would have.
            # Giving Heathrow a frequency full of Cessnas to get its
            # frequency full of registrations would be trading one wrong
            # thing for a louder one; a jet on a private registration is a
            # corporate aeroplane, which every large field really does have.
            made = self._light_flight(airline_fleet=airline_field)
        elif airline_field:
            made = self._airline_flight(cargo=(
                None if category is None else category == livemod.CARGO))
        else:
            made = self._light_flight()
        if made is None:
            return None
        written, type_code, operator = made
        if self.force_types:
            type_code = self.random.choice(self.force_types)

        language = (operator.speaks_at(self.field.ident) if operator
                    else _local_or_english(self.field.ident))
        # Read the way this field reads it: digit by digit under ICAO, in
        # pairs only in US airspace. Without the dialect every invented flight
        # was grouped the American way -- "Air Portugal two fourteen" for
        # TAP0214 at Madeira, which no Portuguese controller would say and
        # which the user's own callsign, read correctly, sat next to.
        forms = every_language(written, type_code,
                               dialect=self.profile.dialect_for(self.field.ident))
        aircraft = Aircraft(forms["en"], type_code, wake_suffix(type_code),
                            callsigns=forms, language=language)

        runway = (self.field.arrival_runway if arriving
                  else self.field.departure_runway)
        flight = Flight(
            written=written,
            type_code=type_code,
            aircraft=aircraft,
            language=language,
            operator=operator,
            arriving=arriving,
            runway=runway,
            destination=self._elsewhere(),
            origin=self._elsewhere(),
            squawk=f"{self.random.randint(1000, 7777):04d}".replace("8", "5")
                                                            .replace("9", "6"),
            phase="inbound" if arriving else "clearance",
            next_at=now + self.random.uniform(2.0, 12.0),
            ifr=airline_field,
            performance=performance_for(type_code, light=operator is None),
            category=(livemod.CARGO if operator is not None
                      and operator.fleet == livemod.FREIGHT
                      else livemod.AIRLINE if airline_field else livemod.GA),
        )
        if not flight.ifr:
            # A VFR flight squawks the conspicuity code, which is regional, and
            # is given one by nobody: it sets it before it starts the engine.
            flight.squawk = self._phrase(language).vfr_squawk
            # A business jet comes and goes; the circuit share is the
            # field's (wilcoatc.atc.fleet).
            kind = self.field.kind
            share = kind.circuit_share if kind is not None else CIRCUIT_SHARE
            flight.pattern_work = (type_code not in fleets.BUSINESS
                                   and self.random.random() < share)
            # And it comes from, or goes to, a field near this one, not Nice.
            flight.origin = self._nearby_field() or flight.origin
            flight.destination = self._nearby_field() or flight.destination
            flight.pattern_direction = (
                "right" if self.random.random() < 0.2 else "left")
        if arriving and not flight.ifr:
            # Far enough out to be arriving rather than already in the
            # circuit, close enough that it joins within a few minutes.
            flight.distance_nm = self.random.uniform(4.0, 7.0)
            flight.sequence = len(self.traffic_ahead(True)) + 1
        elif arriving:
            ahead = self.traffic_ahead(True)
            # Far enough behind whoever is already on the approach to be a
            # separate aeroplane rather than a formation.
            flight.distance_nm = max(
                [f.distance_nm for f in ahead] + [10.0]) + self.random.uniform(
                    5.0, 11.0)
            flight.altitude_ft = (self.field.elevation_ft
                                  + flight.distance_nm * GLIDE_FT_PER_NM)
            flight.sequence = len(ahead) + 1
        else:
            flight.sequence = len(self.traffic_ahead(False)) + 1
            # A VFR departure has no clearance to collect, and a light field
            # has no delivery position to collect one from. Either way it
            # starts on the ground frequency.
            if not flight.ifr or "DEL" not in self.field.stations:
                flight.phase = "taxi"
        self._used_callsigns.add(written)
        if (arriving and self.schedule and self.ground is not None
                and (flight.operator is not None or flight.ifr)):
            # Its stand, held from now: where it will turn round decides
            # which model it is drawn with, so it is drawn right from the
            # first frame and never swapped at the gate.
            stand = self._free_stand(airliner=True)
            if stand is not None:
                flight.planned_stand = stand.index
        if flight.phase in _FLYING_PHASES:
            self._start_flying(flight)
        elif self.ground is not None and parked:
            # On a real stand, nobody else's, and not the pilot's. No stand
            # free is no departure: a full apron is a quiet frequency, not an
            # aeroplane parked on top of another one.
            stand = self._free_stand(airliner=flight.operator is not None
                                     or flight.ifr)
            if stand is None:
                return None
            self._park(flight, stand)
        else:
            self._place(flight)
        return flight

    # -- the ground, driven ----------------------------------------------

    def _on_ground_obstacles(self, ignore: Flight | None = None,
                             with_user: bool = True
                             ) -> list[tuple[float, float, float]]:
        """Everything on the ground, as (x, z, radius) in the layout plane:
        the pilot and every other aeroplane."""
        found: list[tuple[float, float, float]] = []
        layout = self.ground
        if layout is None:
            return found
        user = self.user
        if with_user and user is not None and user.has_position                 and user.on_ground:
            found.append((*layout.to_xy(user.latitude, user.longitude), 25.0))
        for other in self.flights + self._materialising + self._asked_for:
            if other is ignore or other.done or not other.positioned:
                continue
            if not other.on_ground:
                continue
            found.append((*layout.to_xy(other.latitude, other.longitude),
                          20.0))
        return found

    def _stands_taken(self) -> set[int]:
        everyone = (self.flights + self._materialising + self._asked_for
                    + self.statics)
        return ({f.stand for f in everyone
                 if f.stand is not None and not f.done}
                | {f.planned_stand for f in everyone
                   if f.planned_stand is not None and not f.done})

    def _free_stand(self, airliner: bool, also=(), prefer: int | None = None):
        """A free stand, chosen at random -- or ``prefer``, the one an
        arrival has held since it was invented, while it is still free."""
        occupied = self._on_ground_obstacles() + list(also) + [
            (*self.ground.to_xy(lat, lon), 20.0)
            for lat, lon in self._parked_seen]
        taken = self._stands_taken()
        choose = self.random.choice
        if prefer is not None:
            taken.discard(prefer)

            def choose(options):
                for option in options:
                    if option.index == prefer:
                        return option
                return self.random.choice(options)
        return self.ground.free_stand(airliner, occupied, taken, choose=choose)

    def ground_occupancy(self, layout: "groundwork.GroundLayout"
                         ) -> tuple[list[tuple[float, float, float]], set[int]]:
        """Where every other aeroplane is on ``layout``'s ground, for the
        pilot's own taxi (:mod:`wilcoatc.atc.taxi`).

        (x, z, radius) for each aeroplane on the ground -- the ones driven
        here and the ones somebody else put in the simulator -- and the stands
        handed out to the traffic, which a stand number only means anything
        on the layout the traffic is using. The pilot is not in it: they are
        the one being taxied.
        """
        if layout is None or self.ground is not layout:
            return [], set()
        occupied = self._on_ground_obstacles(with_user=False) + [
            (*layout.to_xy(lat, lon), 20.0) for lat, lon in self._parked_seen]
        return occupied, self._stands_taken()

    def _park(self, flight: Flight, stand) -> None:
        flight.on_layout = True
        flight.stand = stand.index
        flight.latitude, flight.longitude = self.ground.to_latlon(stand.x,
                                                                  stand.z)
        flight.heading = stand.heading
        flight.altitude_ft = self.field.elevation_ft
        flight.ground_speed_kt = 0.0
        flight.on_ground = True

    def _airline_flight(self, cargo: bool | None = None
                        ) -> tuple[str, str, Operator] | None:
        """An airline's flight. ``cargo`` True or False keeps to freighters or
        to the others, where real traffic has filled the other category;
        a field with none of the kind asked for draws from all of them."""
        candidates = roster_for(self.field.ident, self.field.size_rank)
        if cargo is not None:
            kept = [c for c in candidates
                    if (c.operator.fleet == livemod.FREIGHT) == cargo]
            candidates = kept or candidates
        if not candidates:
            return None
        operator = self.random.choices(
            [c.operator for c in candidates],
            weights=[c.weight for c in candidates],
        )[0]
        low, high = operator.number_range
        fleet = operator.fleet
        if self.has_livery is not None:
            # Only where there is a painted one to choose: an airline with
            # none keeps its whole fleet rather than having no aeroplanes.
            fleet = tuple(t for t in fleet
                          if self.has_livery(operator.icao, t)) or fleet
        for _ in range(20):
            written = f"{operator.icao}{self.random.randint(low, high)}"
            if written not in self._used_callsigns:
                return written, self.random.choice(fleet), operator
        return None

    def _light_flight(self, airline_fleet: bool = False
                      ) -> tuple[str, str, None] | None:
        """A registration of the shape used where this airport is.

        Every national scheme here is five characters once the dash is taken
        out -- F-GXYZ, G-ABCD, D-EFGH, HB-ABC -- so the prefix decides how many
        letters are still to be drawn. The US is the exception and is numeric.

        ``airline_fleet`` gives it an airliner instead of a light single,
        which is what a large field gets when the pilot has asked for
        registrations rather than telephony names. The registration is the
        thing being chosen here; the aeroplane under it should still be the
        one the airport would have.
        """
        prefix = registration_prefix(self.field.ident)
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        needed = 5 - len(prefix.replace("-", ""))
        kind = self.field.kind or fleets.classify(self.navdb, self.field.ident)
        airliners = self._airline_fleet() if airline_fleet else ()
        for _ in range(20):
            if prefix == "N":
                tail = (f"N{self.random.randint(100, 999)}"
                        f"{self.random.choice(letters)}"
                        f"{self.random.choice(letters)}")
            else:
                tail = prefix + "".join(
                    self.random.choice(letters) for _ in range(needed))
            if tail not in self._used_callsigns:
                type_code = (self.random.choice(airliners) if airliners
                             else fleets.light_type(kind, self.random))
                return tail, type_code, None
        return None

    @property
    def _airline_field(self) -> bool:
        kind = self.field.kind
        if kind is None:
            return self.field.size_rank >= AIRLINE_RANK
        return kind.airline

    def _nearby_field(self) -> str:
        """A field a light aeroplane might have come from or be going to:
        one with a runway, ten to eighty miles away. Empty with none."""
        if not (self.field.latitude or self.field.longitude):
            return ""
        cached = getattr(self, "_nearby_cache", None)
        if cached is None or cached[0] != self.field.ident:
            try:
                found = self.navdb.nearest_airports(
                    self.field.latitude, self.field.longitude, limit=40,
                    max_nm=80.0, min_rank=1, require_runway=True)
            except Exception:
                found = []
            names = [a.spoken or a.name for a, d in found
                     if a.ident != self.field.ident and d >= 10.0]
            cached = (self.field.ident, names)
            self._nearby_cache = cached
        return self.random.choice(cached[1]) if cached[1] else ""

    def _airline_fleet(self) -> tuple[str, ...]:
        """The types the operators at this field actually fly.

        Taken from the roster rather than written down again, so a field that
        only sees regional jets does not suddenly produce a 777 because the
        pilot asked for registrations.
        """
        seen: list[str] = []
        for candidate in roster_for(self.field.ident, self.field.size_rank):
            for type_code in candidate.operator.fleet:
                if type_code not in seen:
                    seen.append(type_code)
        return tuple(seen) or LIGHT_FLEET

    def _elsewhere(self) -> str:
        """Somewhere plausible to be going to or coming from."""
        return self.random.choice(_DESTINATIONS)

    # ------------------------------------------------------------------
    # running them
    # ------------------------------------------------------------------

    # A poll that arrives after a long gap -- the window was minimised, the
    # sim was paused -- must not teleport everybody. An aeroplane moves by at
    # most this many seconds' worth in one step.
    MAX_STEP_S = 5.0

    # How far ahead of a taxiing aeroplane anything stops it. Ninety metres
    # is the braking distance from twenty knots (groundwork.BRAKE_HARD_MS2,
    # about thirty-five metres) plus an airliner's length and a half: room to
    # stop behind one without its tail filling the windscreen. It was seventy
    # when an aeroplane stopped dead the instant it saw something.
    TAXI_LOOK_M = 90.0
    # An intersection is claimed this far before it is reached, for the same
    # reason: what holds an aeroplane short of it has to leave room to brake.
    CLAIM_M = 95.0

    def _taxi(self, flight: Flight, elapsed: float, now: float) -> None:
        """One step along the route, unless something is in the way."""
        layout = self.ground
        taxi = flight.taxi
        if layout is None or taxi is None:
            flight.taxi = None
            return
        x, z = layout.to_xy(flight.latitude, flight.longitude)
        if taxi.done:
            self._arrived(flight, now)
            return
        tx, tz = taxi.points[taxi.index]
        pushing = taxi.index < taxi.push
        moving = groundwork._heading(tx - x, tz - z)
        why = self._taxi_blocked(flight, x, z, moving, now)
        if why == "holding short" and not flight.arriving:
            flight.held_since = flight.held_since or now
        elif not why:
            flight.held_since = 0.0
        if taxi.speed < 0.0:
            # Starting the route at the speed it already has: an arrival
            # turning off the runway rolls on to its taxi.
            taxi.speed = flight.ground_speed_kt / 1.943844
        if why:
            taxi.blocked_since = taxi.blocked_since or now
            if taxi.speed > 0.0:
                # Braking, not stopped dead: something is seen in the look
                # ahead (TAXI_LOOK_M, CLAIM_M), further than an airliner
                # takes to stop from taxi speed, and stopping in an instant
                # was a jolt in the simulator.
                x, z, heading, speed = groundwork.advance(
                    taxi, x, z, flight.heading, elapsed, stop=True)
                flight.latitude, flight.longitude = layout.to_latlon(x, z)
                flight.heading = heading
            flight.ground_speed_kt = taxi.speed * 1.943844
            flight.target.intent = why
            self._break_deadlock(flight, now)
            return
        if (taxi.hold_at is not None and not taxi.cleared
                and taxi.index > taxi.hold_at):
            # Off the runway. On to the stand only if nobody is coming the
            # other way along the same taxiway; otherwise it waits here,
            # clear of the runway, which is where a real one would be told to.
            if self._crosses(taxi, flight):
                taxi.blocked_since = taxi.blocked_since or now
                flight.ground_speed_kt = 0.0
                flight.target.intent = "holding position"
                return
            taxi.cleared = True
        taxi.blocked_since = None
        x, z, heading, speed = groundwork.advance(taxi, x, z, flight.heading,
                                                  elapsed)
        flight.latitude, flight.longitude = layout.to_latlon(x, z)
        flight.heading = heading
        flight.ground_speed_kt = speed * 1.943844
        flight.on_ground = True
        flight.altitude_ft = self.field.elevation_ft
        flight.target.intent = "pushing back" if pushing else "taxiing"
        self._release_behind(flight)
        if taxi.done:
            self._arrived(flight, now)

    def _in_the_way(self, flight: Flight, x: float, z: float
                    ) -> Flight | str | None:
        """Whatever is on the stretch of route this aeroplane is about to use.

        A corridor along the route itself rather than a cone off the nose: a
        cone saw the pilot parked beside a taxilane and stopped for them for
        ever, and missed an aeroplane round the next corner.
        """
        layout = self.ground
        corridor = groundwork.ahead(flight.taxi, x, z, self.TAXI_LOOK_M)
        others: list[tuple[object, float, float]] = []
        user = self.user
        if user is not None and user.has_position and user.on_ground:
            others.append(("you", *layout.to_xy(user.latitude,
                                                user.longitude)))
        for other in self.flights:
            if other is flight or other.done or not other.positioned:
                continue
            if not other.on_ground:
                continue
            others.append((other, *layout.to_xy(other.latitude,
                                                 other.longitude)))
        for who, ox, oz in others:
            if math.hypot(ox - x, oz - z) < 1.0:
                continue
            # The pilot gets more room than the traffic gives itself: their
            # aeroplane may be anything, parked a little off the stand mark.
            room = (self.USER_CLEARANCE_M if who == "you"
                    else self.TAXI_CORRIDOR_M)
            # Behind it does not count; that one is the other's to watch.
            for px, pz in corridor[1:]:
                if math.hypot(ox - px, oz - pz) < room:
                    return who
        return None

    USER_CLEARANCE_M = 35.0

    def _avoid_standing(self, flight: Flight) -> set[object]:
        """Taxi nodes beside an aeroplane that is not moving."""
        layout = self.ground
        found: set[object] = set()
        for other in self.flights:
            if other is flight or other.done or not other.positioned:
                continue
            if not other.on_ground or other.ground_speed_kt > 1.0:
                continue
            ox, oz = layout.to_xy(other.latitude, other.longitude)
            found |= {node for node, (x, z) in layout.xy.items()
                      if math.hypot(x - ox, z - oz) < self.TAXI_CORRIDOR_M
                      + 5.0}
        return found

    def _avoid_user(self) -> set[object]:
        """The taxi nodes too close to the pilot to route through, if there
        is any other way round."""
        layout = self.ground
        user = self.user
        if layout is None or user is None or not user.has_position \
                or not user.on_ground:
            return set()
        ux, uz = layout.to_xy(user.latitude, user.longitude)
        return {node for node, (x, z) in layout.xy.items()
                if math.hypot(x - ux, z - uz) < self.USER_CLEARANCE_M + 10.0}

    # Half an airliner's span and a margin: anything with its middle closer
    # than this to the route ahead is in the way.
    TAXI_CORRIDOR_M = 28.0
    # Two aeroplanes each waiting for the other this long are not going to
    # sort it out between themselves.
    DEADLOCK_S = 60.0
    STUCK_ON_RUNWAY_S = 30.0

    def _break_deadlock(self, flight: Flight, now: float) -> None:
        """The last resort when two taxiing aeroplanes face each other.

        The route check stops a ground controller sending them at each
        other, but an aeroplane that has just landed and one pushing back can
        still meet. After a minute nose to nose the one that matters less --
        a departure gives way to an arrival that has to get off the runway --
        is taken away. Not what a real airport would do; what a real airport
        would do is tow it, and there is no tug to call.
        """
        taxi = flight.taxi
        if taxi is None or taxi.blocked_since is None:
            return
        layout = self.ground
        x, z = layout.to_xy(flight.latitude, flight.longitude)
        if (now - taxi.blocked_since >= self.STUCK_ON_RUNWAY_S
                and self._on_runway_itself(flight)):
            # Stuck on the runway behind somebody standing still. The runway
            # is the one place nobody may wait, so whatever of ours is in the
            # way goes -- never the pilot, never somebody else's aeroplane.
            blocker = self._in_the_way(flight, x, z)
            if isinstance(blocker, Flight) and not blocker.observed \
                    and blocker.ground_speed_kt <= 1.0:
                log.info("%s is stuck on the runway behind %s; %s is taken "
                         "away", flight.written, blocker.written,
                         blocker.written)
                blocker.done = True
                self._release_all(blocker)
                return
        if now - taxi.blocked_since < self.DEADLOCK_S:
            return
        other = self._in_the_way(flight, x, z)
        if not isinstance(other, Flight) or other.taxi is None \
                or other.taxi.blocked_since is None:
            return
        ox, oz = layout.to_xy(other.latitude, other.longitude)
        if self._in_the_way(other, ox, oz) is not flight:
            return
        loser = other if (flight.arriving and not other.arriving) else flight
        if loser.observed:
            loser = flight
        log.info("%s and %s are nose to nose; %s is taken away",
                 flight.written, other.written, loser.written)
        loser.done = True
        self._release_all(loser)

    def _on_runway_itself(self, flight: Flight) -> bool:
        end = self._threshold(flight.runway or self.field.arrival_runway)
        if end is None:
            return False
        along, across = offset_from_track(flight.latitude, flight.longitude,
                                          end[0], end[1], end[2])
        # The runway surface, not the ninety-metre strip RUNWAY_WIDTH_NM
        # describes: with that, an aeroplane eighty metres off the
        # centreline counted as already on the runway, was let "off" it, and
        # drove across in front of a departure on its takeoff roll.
        return abs(across) <= self.ON_RUNWAY_NM and \
            -0.15 <= along <= RUNWAY_LENGTH_NM

    def _taxi_blocked(self, flight: Flight, x: float, z: float,
                      moving: float, now: float) -> str:
        """Why this aeroplane may not move just now, or "" if it may."""
        layout = self.ground
        # Anybody ahead: the pilot, another of ours, somebody else's.
        if self._in_the_way(flight, x, z) is not None:
            return "holding for traffic"
        # The next intersection, which is somebody else's until they are
        # through it.
        taxi = flight.taxi
        for position in range(taxi.index, len(taxi.nodes)):
            node = taxi.nodes[position]
            if node is None:
                continue
            nx, nz = layout.xy[node]
            if math.hypot(nx - x, nz - z) > self.CLAIM_M:
                break
            if layout.degree(node) < 3 and node not in layout.runway_nodes:
                continue
            holder = self._reserved.get(node)
            if holder and holder != flight.written:
                return "holding for traffic"
            # The line-up itself was cleared by the tower; every other way
            # onto the runway -- the taxi out included, which carries the
            # line-up leg from the stand onwards -- asks here first.
            if node in layout.runway_nodes and taxi is not flight.line_up \
                    and not self._on_runway_itself(flight) \
                    and self._reserved.get(node) != flight.written:
                # Onto the runway to cross it or to backtrack along it --
                # which at a field like Madeira is the only way to the end.
                # Asked once, for all of it: the stretch it will be on, and
                # how long that takes. An aeroplane that has just landed is
                # always let off the runway it is on.
                if not self._may_cross_runway(flight, position, now):
                    return "holding short"
                self._claim_runway_stretch(flight, position)
            self._reserved[node] = flight.written
        return ""

    # A runway is crossed only with this long before anybody lands on it.
    CROSS_CLEAR_S = 90.0

    def _runway_stretch(self, flight: Flight, position: int) -> list[object]:
        """The runway nodes a route uses from here, in one go."""
        layout = self.ground
        nodes = flight.taxi.nodes
        stretch = []
        for node in nodes[position:]:
            if node is None or node not in layout.runway_nodes:
                break
            stretch.append(node)
        return stretch

    def _may_cross_runway(self, flight: Flight, position: int,
                          now: float) -> bool:
        """Whether it may go onto the runway for the stretch ahead, and if it
        may, the runway is taken for as long as that stretch takes."""
        layout = self.ground
        stretch = self._runway_stretch(flight, position)
        metres = sum(math.dist(layout.xy[a], layout.xy[b])
                     for a, b in zip(stretch, stretch[1:]))
        # Off the far side as well: the tail has to clear it, not the nose.
        takes = metres / groundwork.TURN_MS + 30.0
        if (self.runway_occupied(now, ignore=flight)
                or self._somebody_lined_up(flight)
                or self._nearest_arrival_s(flight) < takes
                + self.CROSS_CLEAR_S):
            return False
        self.runway_free_at = max(self.runway_free_at, now + takes)
        return True

    def _claim_runway_stretch(self, flight: Flight, position: int) -> None:
        for node in self._runway_stretch(flight, position):
            self._reserved[node] = flight.written

    def _release_behind(self, flight: Flight) -> None:
        """Give back the intersections this aeroplane is through."""
        taxi = flight.taxi
        layout = self.ground
        x, z = layout.to_xy(flight.latitude, flight.longitude)
        for node, holder in list(self._reserved.items()):
            if holder != flight.written:
                continue
            ahead = taxi is not None and node in taxi.nodes[taxi.index:]
            nx, nz = layout.xy[node]
            if not ahead and math.hypot(nx - x, nz - z) > 30.0:
                del self._reserved[node]

    def _release_all(self, flight: Flight) -> None:
        for node, holder in list(self._reserved.items()):
            if holder == flight.written:
                del self._reserved[node]

    def _prune_reservations(self) -> None:
        """Drop every reservation whose holder is no longer on the ground.

        A reservation was released by driving past the junction, by parking,
        or by the deadlock breaker -- never by leaving. A departure lined up
        and took off still holding its last junction, and the next aeroplane
        to use it waited for one that no longer existed: Kortrijk's "takeoff,
        0.0 nm to run" for twenty-eight minutes, and the queue behind it.
        Reproduced on Kortrijk's own taxi network (OO-BPH waiting on a
        junction held by OO-XLZ, gone).
        """
        if not self._reserved:
            return
        on_the_ground = {f.written for f in
                         self.flights + self._materialising + self._asked_for
                         if not f.done and f.on_ground}
        for node, holder in list(self._reserved.items()):
            if holder not in on_the_ground:
                del self._reserved[node]

    # -- the watchdog ---------------------------------------------------

    # How long an aeroplane may stand still where it should be moving before
    # it is stuck: on a runway, on a taxiway, in the air. The figures asked
    # for after Kortrijk -- longer than any stop this program's own traffic
    # makes there on purpose, which is all they have to be.
    STUCK_AFTER_S = {"runway": 20.0, "taxiway": 60.0, "air": 30.0}
    # Standing still on purpose: told to, or waiting its turn.
    _WAITING_ON_PURPOSE = frozenset({"holding short", "lined up",
                                     "holding position", "parked"})
    _AIRBORNE_PHASES = frozenset({"takeoff", "departed", "go_around",
                                  "inbound", "joining", "final"}) \
        | _CIRCUIT_PHASES
    # Moved: a metre on the ground, ten feet up or down.
    STILL_M = 1.0
    STILL_FT = 10.0

    def _where_moving(self, flight: Flight) -> str:
        """"runway", "taxiway" or "air" for an aeroplane in a phase in which
        it should be moving, or "" for one that may stand still."""
        if flight.done or flight.observed or flight.static:
            return ""
        if not flight.on_ground:
            return "air" if flight.phase in self._AIRBORNE_PHASES else ""
        if flight.taxi is not None and flight.phase in (
                "taxi", "ready", "taxi_in", "takeoff", "lineup", "landed"):
            on_runway = (self.ground is not None
                         and self._on_runway_itself(flight))
            return "runway" if on_runway else "taxiway"
        if flight.phase in ("takeoff", "landed"):
            return "runway"
        return ""

    def _queued(self, flight: Flight, now: float) -> bool:
        """Waiting for somebody who is themselves getting on with it -- the
        aeroplane in front, or the one holding the junction ahead -- which
        is a queue and not a fault. Two that have both stood still for the
        whole limit are waiting on each other, and that is.

        Measured at Madeira: an arrival taxiing in held for a junction
        another of ours had reserved was taken away as stuck while the
        holder was simply waiting its own turn.
        """
        if flight.taxi is None or self.ground is None:
            return False
        x, z = self.ground.to_xy(flight.latitude, flight.longitude)
        ahead = self._in_the_way(flight, x, z)
        if ahead is not None and not isinstance(ahead, Flight):
            return True                     # the pilot, or the simulator's
        waiting_on = [ahead] if isinstance(ahead, Flight) else []
        taxi = flight.taxi
        holders = {holder for node, holder in self._reserved.items()
                   if holder != flight.written and node in taxi.nodes[taxi.index:]}
        waiting_on += [f for f in self.flights if f.written in holders]
        limit = min(self.STUCK_AFTER_S.values())
        return any(not other.stuck and not other.done
                   and now - (other.still_since or now) < limit
                   for other in waiting_on)

    def _watchdog(self, now: float) -> None:
        """Find what has stopped where it should be moving, and deal with it.

        The safety net, not the fix: whatever froze an aeroplane is a bug to
        be found, and this is what keeps the frequency working until it is.
        Stuck for ``STUCK_AFTER_S`` it is flagged -- out of the controller's
        picture from then on -- and given one recovery; still stuck after
        another as long, it is taken out of the traffic, and so out of the
        simulator, with a ``traffic_stuck`` row saying which, where, in
        what phase and why.
        """
        for flight in self.flights:
            where = self._where_moving(flight)
            here = (flight.latitude, flight.longitude, flight.altitude_ft)
            if (not where or flight.target.intent in self._WAITING_ON_PURPOSE
                    or self._queued(flight, now)):
                flight.still_at, flight.still_since = here, now
                flight.stuck = flight.recovered = False
                continue
            if flight.still_at is None or self._moved(flight.still_at, here):
                flight.still_at, flight.still_since = here, now
                if flight.stuck:
                    log.info("traffic: %s is moving again", flight.written)
                flight.stuck = flight.recovered = False
                continue
            still = now - flight.still_since
            if still < self.STUCK_AFTER_S[where]:
                continue
            if not flight.recovered:
                flight.stuck = True
                flight.recovered = True
                self._say_stuck(flight, where, still, "retry")
                self._recover(flight)
                flight.still_since = now
                continue
            self._say_stuck(flight, where, still, "removed")
            flight.done = True
            self._release_all(flight)
            if where == "runway":
                # Whatever it had booked the runway for is not happening.
                self.runway_free_at = min(self.runway_free_at, now)

    def _moved(self, was: tuple, here: tuple) -> bool:
        north_m = (here[0] - was[0]) * 111_320.0
        east_m = ((here[1] - was[1]) * 111_320.0
                  * math.cos(math.radians(here[0])))
        return (math.hypot(north_m, east_m) > self.STILL_M
                or abs(here[2] - was[2]) > self.STILL_FT)

    def _recover(self, flight: Flight) -> None:
        """One attempt to get it going: nothing reserved in its name, the
        route's wait forgotten, back in the flight model if it is flying,
        and drawn again from scratch in case the simulator lost it."""
        self._release_all(flight)
        if flight.taxi is not None:
            flight.taxi.blocked_since = None
        flight.target.hold = False
        if (flight.phase in _FLYING_PHASES and flight.taxi is None
                and not flight.airborne_model):
            self._start_flying(flight)
        flight.redraw = True

    def _say_stuck(self, flight: Flight, where: str, still: float,
                   action: str) -> None:
        if where == "runway":
            place = f"runway {flight.runway}".strip()
        elif where == "air":
            place = f"in the air, {flight.altitude_ft:.0f} ft"
        else:
            place = "taxiway"
        why = flight.target.intent or "not moving"
        holders = sorted({holder for node, holder in self._reserved.items()
                          if holder != flight.written and flight.taxi is not None
                          and node in flight.taxi.nodes[flight.taxi.index:]})
        if holders:
            why += f" (route held by {', '.join(holders)})"
        decision("traffic_stuck", callsign=flight.written, phase=flight.phase,
                 where=place, latitude=round(flight.latitude, 5),
                 longitude=round(flight.longitude, 5),
                 altitude_ft=round(flight.altitude_ft), still_s=round(still),
                 why=why, action=action)
        log.info("traffic: %s stuck %s s %s in phase %s (%s): %s",
                 flight.written, round(still), place, flight.phase, why,
                 "trying once more" if action == "retry" else "taken away")

    def _arrived(self, flight: Flight, now: float) -> None:
        """At the end of a route: the holding point, the runway, a stand."""
        taxi = flight.taxi
        flight.taxi = None
        flight.ground_speed_kt = 0.0
        if taxi is not None and taxi.face is not None:
            # On the runway, pointing down it. The flight model takes it from
            # here, from where it actually is.
            flight.line_up = None
            flight.heading = taxi.face
            return
        if flight.arriving and flight.stand is not None:
            flight.parked_at = now
            flight.target.intent = "parked"
            stand = self.ground.stands[flight.stand]
            flight.heading = stand.heading
            self._release_all(flight)
            return
        flight.target.intent = "holding short"

    def _crosses(self, route, flight: Flight) -> bool:
        """Whether this route meets somebody else's head on.

        A ground controller does not send two aeroplanes at each other down
        one taxiway and hope; one of them waits. So does this.
        """
        if route is None:
            return False
        mine = route.remaining_edges()
        reversed_mine = {(b, a) for a, b in mine}
        for other in self.flights:
            if other is flight or other.done or other.taxi is None:
                continue
            if other.taxi.remaining_edges() & reversed_mine:
                return True
        return False

    def _fly(self, now: float) -> None:
        """Move every aeroplane along, at its own rate."""
        elapsed = now - self._flown_at if self._flown_at else 0.0
        self._flown_at = now
        elapsed = max(0.0, min(elapsed, self.MAX_STEP_S))
        if elapsed <= 0.0:
            return
        for flight in self.flights:
            if flight.done or flight.observed:
                # Somebody else owns this one's position. Flying it here would
                # be two hands on the same aeroplane.
                continue
            if not flight.airborne_model:
                if flight.taxi is not None:
                    # On the ground and on its way somewhere: driven, not
                    # placed, and not flown until it gets there.
                    self._taxi(flight, elapsed, now)
                    if flight.taxi is not None:
                        continue
                if flight.phase in _FLYING_PHASES and not (
                        flight.on_layout and flight.line_up is not None):
                    # Anything in an airborne phase belongs to the model,
                    # however it got there.
                    self._start_flying(flight)
                if not flight.airborne_model:
                    if not flight.on_layout:
                        self._place(flight)
                    continue
            self._aim(flight)
            fly_step(flight, flight.target, flight.performance, elapsed,
                     self._surface_ft(flight))
            self._after_flying(flight)

    def _advance(self, now: float) -> None:
        # What was said a moment ago takes effect first: an aeroplane that has
        # finished reading back a go-around is going around before it is moved
        # anywhere, not a tick later.
        self._obey(now)
        self._fly(now)
        self._prune_reservations()
        self._watchdog(now)
        self._protect_the_pilot(now)
        for flight in self.flights:
            if flight.done:
                continue
            if now >= flight.next_at:
                if flight.observed:
                    self._advance_observed(flight, now)
                elif flight.arriving:
                    self._advance_arrival(flight, now)
                else:
                    self._advance_departure(flight, now)
        # And then the tower looks at what has developed and says something
        # about it, which is the half of this that is not on a timetable.
        self._watch(now)
        # Whoever has just parked for good leaves the frequency for the apron.
        parked = [f for f in self.flights if f.static]
        if parked:
            self.flights = [f for f in self.flights if not f.static]
            for flight in parked:
                # Already in the simulator; it has been all along.
                flight.drawn = True
            self.statics.extend(parked)
            self.pending = [c for c in self.pending
                            if not c.flight.static]

    # ------------------------------------------------------------------
    # instructions, and being obeyed
    # ------------------------------------------------------------------

    def _tell(self, flight: Flight, kind: str, at: float, **values) -> Command:
        """Record an instruction, to take effect when the readback ends.

        Nothing here says it -- the words are the phraseology's job and are
        queued alongside. This is the other half: what the aeroplane will
        actually do about it, and when.
        """
        command = Command(kind=kind, at=at, values=values,
                          shown=(OWN_DECISIONS.get(kind, "")
                                 if self.uncontrolled else ""))
        flight.commands.append(command)
        # Only the last few are kept. A strip shows what an aeroplane is
        # doing, not everything it has ever been told.
        if len(flight.commands) > 8:
            del flight.commands[:-8]
        return command

    def _already_told(self, flight: Flight, kind: str) -> bool:
        """Whether this aeroplane is already dealing with an instruction.

        An instruction takes a few seconds to be read back, and the tower
        looks at the field every second. Without this, an aeroplane on short
        final to a blocked runway is told to go around six times before the
        first one takes effect -- and goes around six times.
        """
        if flight.doing is not None and flight.doing.kind == kind:
            return True
        return any(command.kind == kind and not command.applied
                   for command in flight.commands)

    def _obey(self, now: float) -> None:
        """Do what has been read back and is now due."""
        for flight in self.flights:
            if flight.done:
                continue
            for command in flight.commands:
                if command.applied or command.at > now:
                    continue
                command.applied = True
                flight.doing = command
                self._carry_out(flight, command, now)

    def _carry_out(self, flight: Flight, command: Command, now: float) -> None:
        """One instruction, turned into something the aeroplane is doing."""
        kind = command.kind
        values = command.values

        if kind in ("line_up", "takeoff", "touch_and_go"):
            # Released: no longer waiting, and no longer a reason for the
            # tower to hold the arrivals off.
            flight.held_since = 0.0
        if kind == "touch_and_go" and flight.arriving:
            # Cleared for the option on base or final: the clearance is used
            # on the runway, by the touchdown in _work_the_arrival, which is
            # where a circuit becomes a takeoff. Taken as a takeoff here, it
            # put an arrival two miles out into a phase nothing on the
            # arrival side moves on from, and it flew the runway heading for
            # the rest of the session (Centennial, 26 October: five at once).
            return
        if kind == "taxi" and flight.on_layout and flight.planned is not None:
            # The taxi clearance has been read back: off the stand and on
            # the way to the holding point.
            flight.taxi, flight.line_up = flight.planned
            flight.planned = None
            flight.stand = None
        elif kind in ("line_up", "takeoff", "touch_and_go") \
                and flight.on_layout and flight.line_up is not None:
            # Driven onto the runway rather than put there; the roll starts
            # when it is lined up (see _fly).
            if flight.taxi is None:
                flight.taxi = flight.line_up
            flight.phase = "lineup" if kind == "line_up" else "takeoff"
            if kind != "line_up":
                flight.assigned_heading = None
                flight.assigned_altitude_ft = None
                flight.assigned_speed_kt = None
                self.runway_free_at = max(self.runway_free_at,
                                          now + RUNWAY_ROLL_S + 30.0)
        elif kind == "line_up":
            if not flight.airborne_model:
                self._start_flying(flight)
            flight.phase = "lineup"
        elif kind in ("takeoff", "touch_and_go"):
            if not flight.airborne_model:
                self._start_flying(flight)
            flight.phase = "takeoff"
            flight.assigned_heading = None
            flight.assigned_altitude_ft = None
            flight.assigned_speed_kt = None
            self.runway_free_at = max(self.runway_free_at, now + RUNWAY_ROLL_S)
        elif kind == "go_around":
            # The one instruction that has to work the instant it is read
            # back: the aeroplane stops descending, climbs away on the runway
            # heading, and rejoins the circuit for another go.
            flight.phase = "go_around"
            flight.arriving = False
            flight.extending = False
            flight.go_arounds += 1
            flight.step = 0
            flight.assigned_heading = None
            flight.assigned_altitude_ft = None
            flight.assigned_speed_kt = None
            flight.next_at = now + self.random.uniform(20.0, 35.0)
        elif kind == "extend":
            flight.extending = True
        elif kind == "continue":
            flight.extending = False
        elif kind == "heading":
            flight.assigned_heading = float(values.get("heading", flight.heading))
            flight.target.turn = values.get("turn", "")
        elif kind == "altitude":
            flight.assigned_altitude_ft = float(values.get("altitude_ft", 0.0))
        elif kind == "speed":
            flight.assigned_speed_kt = float(values.get("speed_kt", 0.0))
        elif kind == "resume":
            flight.assigned_heading = None
            flight.assigned_speed_kt = None
        elif kind == "hold_short":
            flight.target.hold = True

    # ------------------------------------------------------------------
    # the aeroplane the person is flying
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # aeroplanes somebody else is flying
    # ------------------------------------------------------------------

    def set_observed(self, seen, now: float) -> None:
        """Take the aeroplanes the simulator is holding onto the frequency.

        ``seen`` is what :class:`wilcoatc.integrations.simaircraft.SimAircraft`
        reports: everything the simulator has, most of which is not this
        airport's business. What survives :meth:`_works_here` becomes a
        :class:`Flight` like any other, and from there the whole of the rest of
        this module treats it as traffic -- it holds the runway, it takes its
        place in the landing sequence, and it is the aeroplane the user is told
        to follow.

        What it never becomes is a subordinate. It is flown by whoever injected
        it, so the flight model does not touch it and the tower does not
        instruct it. The only thing said to it is a description of what it is
        already doing; see :meth:`_advance_observed`.
        """
        if not self.field.ident:
            return
        # The parked aeroplanes are taken up only once the pilot's own
        # position is known: the pilot is in the simulator's list too, and
        # until they can be recognised by where they are, their own
        # aeroplane is a parked airliner on a stand -- which, measured, got
        # its doors opened by this program for the ten seconds it took.
        user_known = self.user is not None and self.user.has_position
        self._observed_read = seen is not None and user_known
        known = {f.observed_id: f for f in self.flights if f.observed}
        here: set[int] = set()
        # The ones this program put there itself. They are in the simulator
        # now, so the simulator reports them, under the callsign they were
        # created with -- and read back as somebody else's they would be
        # worked twice, once flown and once watched.
        ours = {f.written.upper()
                for f in self.flights + self._materialising + self._asked_for
                + self.statics
                if not f.observed}
        # The live ones this program draws: the simulator reports each of
        # those back too, under the same callsign, and that copy is ours.
        live_ours = ({f.written.upper() for f in self.flights
                      if f.observed and f.source == "live"}
                     | {one.callsign for one in self._parked_observed.values()
                        if one.live})
        seen = [one for one in self._rejoin_the_feed(seen or (), now)
                if getattr(one, "source", "") == "live"
                or (one.callsign or "").strip().upper() not in live_ours]
        # A copy of ours just given back to the feed is no longer ours.
        ours -= {f.written.upper() for f in self.flights
                 if f.done and not f.observed}
        seen = [self._live_pose(one, now)
                if getattr(one, "source", "") == "live" else one
                for one in seen]
        # The live ones the feed hears now: a parked one is kept on its stand
        # only while it does not (_note_parked).
        self._live_heard = {one.object_id for one in seen
                            if getattr(one, "source", "") == "live"}
        # A real flight with a callsign an invented one already has (same
        # airline, same number): the invented one finishes what it is doing
        # and goes at its next gate, and the real one joins once it has --
        # never two aeroplanes with one callsign, and nobody taken off the
        # frequency mid-exchange.
        for one in seen or ():
            if getattr(one, "source", "") != "live":
                continue
            written = (one.callsign or "").strip().upper()
            if written in ours:
                for f in (self.flights + self._materialising + self.statics):
                    if not f.observed and f.written.upper() == written \
                            and not f.retire:
                        f.retire = True
                        decision("blend", event="retire", callsign=written,
                                 why="the real flight has this callsign")
        # Somebody else's aeroplanes standing on this airport. Not worked,
        # but in the way of anything this puts on a stand -- and on the map,
        # and handled, because the simulator does nothing for them.
        standing = [
            one for one in (seen if user_known else ())
            if getattr(one, "positioned", False) and one.on_ground
            and one.ground_speed_kt < TAXIING_KT
            and (one.callsign or "").strip().upper() not in ours
            and not self._is_user(one)
            and haversine_nm(self.field.latitude, self.field.longitude,
                             one.latitude, one.longitude) < PARKED_RANGE_NM]
        self._parked_seen = [(one.latitude, one.longitude) for one in standing]
        self._note_parked(standing, known, now)

        # Nearest first, so a field with more aeroplanes on it than can be
        # worked keeps the ones that are about to use the runway.
        wanted = [one for one in (seen or ())
                  if (one.callsign or "").strip().upper() not in ours
                  and not self._is_user(one)
                  and self._works_here(one, one.object_id in known)]
        wanted.sort(key=lambda one: haversine_nm(
            self.field.latitude, self.field.longitude,
            one.latitude, one.longitude))

        for one in wanted[:OBSERVED_LIMIT]:
            here.add(one.object_id)
            flight = known.get(one.object_id)
            if flight is None:
                flight = self._observed_flight(one)
                if flight is None:
                    continue
                self.flights.append(flight)
                if flight.source == "live":
                    # Its first appearance, for logs radio: which real
                    # aeroplane, from which source, going where.
                    decision("live_traffic", event="appeared",
                             callsign=flight.written, type=flight.type_code,
                             feed=getattr(one, "feed", "") or "",
                             category=flight.category,
                             origin=flight.origin,
                             destination=flight.destination,
                             on_ground=bool(one.on_ground))
            self._read_observed(flight, one, now)

        # Anything that has gone is gone. It landed and taxied out of range,
        # it was despawned, or the pilot flew away from the field -- and in
        # none of those cases is it still on this frequency.
        for object_id, flight in known.items():
            if object_id not in here:
                flight.done = True
        self.flights = [f for f in self.flights if not f.done]
        self.observed_count = sum(1 for f in self.flights if f.observed)

    # -- the live ones on the scenery (atc/live.py) ------------------------

    def _live_pose(self, one, now: float):
        """A live position, put where the simulator's scenery has it.

        The reports are a receiver's, metres off: a departure on its roll
        drawn beside the centreline, a taxiing one on the grass, a parked one
        across two stands, and every one of them at the height of a pressure
        altitude rather than of the simulator's ground. So:

        - on the runway or its final, aligned with it and within 40 m of the
          centreline, it is put on the centreline (the pilot's figure,
          30 Oct 2026);
        - taxiing, on the nearest taxiway, within 40 m;
        - parked, on the stand it is on;
        - on the ground, at the ground's height; in the air, its pressure
          altitude corrected to the field's QNH, never below the field.

        And an arrival taxiing in near a free stand is rolled onto it
        (:meth:`_roll_in`), where it then stays.
        """
        from dataclasses import replace

        if self.ground is not None:
            rolled = self._roll_in(one, now)
            if rolled is not None:
                return rolled
        lat, lon, heading = one.latitude, one.longitude, one.heading
        snapped = ""
        runway = self._on_a_centreline(lat, lon, heading)
        if runway is not None:
            lat, lon, course = runway
            if one.on_ground:
                heading = course
            snapped = "runway"
        elif one.on_ground and self.ground is not None:
            if one.ground_speed_kt < livemod.MOVING_KT:
                x, z = self.ground.to_xy(lat, lon)
                stand = self.ground.stand_at(x, z)
                if stand is not None:
                    lat, lon = self.ground.to_latlon(stand.x, stand.z)
                    heading, snapped = stand.heading, "stand"
            else:
                taxiway = self._on_a_taxiway(lat, lon)
                if taxiway is not None:
                    lat, lon = taxiway
                    snapped = "taxiway"
        elevation = self.field.elevation_ft
        if one.on_ground:
            altitude = elevation
        else:
            altitude = max(elevation, self._qnh_altitude(one.altitude_ft))
        if not one.on_ground and one.hex:
            self._live_flew.add(one.hex)
        self.live_snapped[(one.callsign or "").upper()] = snapped
        return replace(one, latitude=lat, longitude=lon, heading=heading,
                       altitude_ft=altitude)

    def _qnh_altitude(self, pressure_ft: float) -> float:
        """A pressure altitude (1013.25) as the altitude above the sea the
        simulator draws at: about 27 ft a hectopascal near the ground (ISA).
        Above the transition the difference is nobody's business, and it is
        not applied above FL180."""
        weather = self.field.weather
        qnh = getattr(weather, "qnh_hpa", None) if weather is not None else None
        if not qnh or pressure_ft > 18000.0:
            return pressure_ft
        return pressure_ft + (float(qnh) - 1013.25) * FT_PER_HPA

    def _on_a_centreline(self, lat: float, lon: float, heading: float):
        """(lat, lon, course) on the centreline of the runway it is lined up
        with -- on it, or on its final -- when it is within
        LIVE_CENTRELINE_M of it; else None."""
        for runway, (lat0, lon0, course) in self.field.thresholds.items():
            if abs(livemod._wrap(heading - course)) > LIVE_ALIGNED_DEG:
                continue
            north = (lat - lat0) * M_PER_DEG
            east = (lon - lon0) * M_PER_DEG * math.cos(math.radians(lat0))
            rad = math.radians(course)
            along = north * math.cos(rad) + east * math.sin(rad)
            across = -north * math.sin(rad) + east * math.cos(rad)
            surface = self.field.surfaces.get(runway)
            length_m = (surface[2] * 1852.0 if surface else LIVE_RUNWAY_M)
            if not (-LIVE_FINAL_M <= along <= length_m + 300.0):
                continue
            if abs(across) > LIVE_CENTRELINE_M:
                continue
            north_c, east_c = along * math.cos(rad), along * math.sin(rad)
            return (lat0 + north_c / M_PER_DEG,
                    lon0 + east_c / (M_PER_DEG * math.cos(math.radians(lat0))),
                    course)
        return None

    def _on_a_taxiway(self, lat: float, lon: float):
        """The nearest point of the nearest taxiway, within
        LIVE_TAXIWAY_M; None away from all of them."""
        layout = self.ground
        x, z = layout.to_xy(lat, lon)
        best, found = LIVE_TAXIWAY_M, None
        for a, others in layout.edges.items():
            ax, az = layout.xy[a]
            for b, _length, runway, _label in others:
                if runway:
                    continue
                bx, bz = layout.xy[b]
                dx, dz = bx - ax, bz - az
                span = dx * dx + dz * dz
                u = 0.0 if span <= 0 else max(0.0, min(
                    1.0, ((x - ax) * dx + (z - az) * dz) / span))
                px, pz = ax + u * dx, az + u * dz
                distance = math.hypot(x - px, z - pz)
                if distance < best:
                    best, found = distance, (px, pz)
        return layout.to_latlon(*found) if found is not None else None

    def _roll_in(self, one, now: float):
        """An arrival taxiing in at taxi speed within 150 m of a free stand
        ahead of it is rolled onto the stand at a walking pace and stays
        there (after LocalTC): the real one goes to a gate the scenery may
        not have where it is, and drawn on the grass beside the stand it
        would not be parked anywhere. Let go once the feed has heard it stop
        and then move again -- its next flight -- or after 45 minutes."""
        from dataclasses import replace

        held = self._live_stands.get(one.object_id)
        if held is None:
            if not (one.on_ground and one.hex in self._live_flew
                    and livemod.MOVING_KT <= one.ground_speed_kt
                    <= LIVE_TAXI_IN_KT):
                return None
            stand = self._stand_ahead(one)
            if stand is None:
                return None
            self.live_snapped[(one.callsign or "").upper()] = "rolled in"
            held = self._live_stands[one.object_id] = {
                "stand": stand, "since": now, "at": (one.latitude,
                                                     one.longitude),
                "heading": one.heading, "stopped": False}
            self._live_moving.discard(one.object_id)
            decision("live_traffic", event="rolled onto a stand",
                     callsign=one.callsign, stand=stand.index)
        stand = held["stand"]
        # Heard stopping, then moving again: its next flight.
        if one.ground_speed_kt < livemod.MOVING_KT:
            held["stopped"] = True
        elif held["stopped"] or now - held["since"] > LIVE_PARKED_S:
            del self._live_stands[one.object_id]
            self._live_moving.add(one.object_id)
            return None
        layout = self.ground
        target = layout.to_latlon(stand.x, stand.z)
        lat, lon = held["at"]
        x, z = layout.to_xy(lat, lon)
        tx, tz = stand.x, stand.z
        left = math.hypot(tx - x, tz - z)
        step = LIVE_ROLL_MS * max(0.0, now - held.get("moved", now))
        held["moved"] = now
        if left <= step or left < 0.5:
            held["at"] = target
            return replace(one, latitude=target[0], longitude=target[1],
                           heading=stand.heading, ground_speed_kt=0.0,
                           altitude_ft=self.field.elevation_ft)
        x += (tx - x) / left * step
        z += (tz - z) / left * step
        held["at"] = layout.to_latlon(x, z)
        heading = math.degrees(math.atan2(tx - x, tz - z)) % 360.0
        return replace(one, latitude=held["at"][0], longitude=held["at"][1],
                       heading=heading,
                       ground_speed_kt=LIVE_ROLL_MS / 0.514444,
                       altitude_ft=self.field.elevation_ft)

    def _stand_ahead(self, one):
        """A free stand within LIVE_STAND_M, ahead of the aeroplane."""
        layout = self.ground
        x, z = layout.to_xy(one.latitude, one.longitude)
        airliner = livemod.category_of(one.callsign or "",
                                       one.type_code or "") != livemod.GA
        taken = {held["stand"].index for held in self._live_stands.values()}
        taken |= {f.stand for f in self.statics if f.stand is not None}
        occupied = [layout.to_xy(lat, lon) for lat, lon in self._parked_seen]
        best, found = LIVE_STAND_M, None
        for stand in layout.stands_for(airliner=airliner):
            if stand.index in taken:
                continue
            distance = math.hypot(stand.x - x, stand.z - z)
            if distance > best:
                continue
            bearing = math.degrees(math.atan2(stand.x - x, stand.z - z))
            if abs(livemod._wrap(bearing - one.heading)) > LIVE_AHEAD_DEG:
                continue
            if any(math.hypot(stand.x - ox, stand.z - oz) < stand.radius
                   for ox, oz in occupied):
                continue
            best, found = distance, stand
        return found

    # How close an observed aeroplane has to be to the pilot's own position
    # to be the pilot. Twenty-five metres: the next stand is forty away.
    USER_IS_HERE_M = 25.0

    def _is_user(self, one) -> bool:
        """Whether an observed aeroplane is the pilot's own.

        The simulator reports the user alongside the AI, and the name it
        reports them under is not always the one the session knows: the
        reader rebuilds a callsign from the airline and flight number, and
        a user aircraft with neither answers with its registration. So the
        pilot is also recognised by where they are, which no name can get
        wrong -- a parked aeroplane twenty-five metres from the pilot's
        reference point, on the ground as they are, is the pilot.
        """
        user = self.user
        if user is None or not user.has_position:
            return False
        if bool(one.on_ground) != bool(user.on_ground):
            return False
        apart_m = haversine_nm(user.latitude, user.longitude,
                               one.latitude, one.longitude) * 1852.0
        return apart_m <= self.USER_IS_HERE_M

    def _note_parked(self, standing, known: dict, now: float) -> None:
        """Keep the parked observed aeroplanes, and when each one stopped.

        One that had a strip a moment ago -- it taxied in and has just
        stopped -- is an arrival and its turnaround starts now; one found
        standing there has been there for some unknown while. An aeroplane
        on a runway is not parked whatever its speed.
        """
        here: dict[int, ParkedObserved] = {}
        for one in standing:
            if self._on_runway(one):
                continue
            kept = self._parked_observed.get(one.object_id)
            if kept is None:
                flight = known.get(one.object_id)
                if flight is not None:
                    # It had a strip. Stopped on a stand it has parked and
                    # the strip is finished; stopped anywhere else it is
                    # holding short, which is the aeroplane the tower is
                    # about to talk to, and it keeps its strip.
                    if not self._parked_for_good(flight, one):
                        continue
                    flight.done = True
                kept = ParkedObserved(
                    object_id=one.object_id,
                    callsign=(one.callsign or "").strip().upper(),
                    registration=getattr(one, "registration", "") or "",
                    type_code=observed_type(one.type_code),
                    title=getattr(one, "title", "") or "",
                    since=now if flight is not None else 0.0,
                    live=getattr(one, "source", "") == "live",
                    operator=(one.callsign[:3].upper() if livemod.category_of(
                        one.callsign or "", one.type_code or "") in (
                            livemod.AIRLINE, livemod.CARGO) else ""),
                    parked_at=now,
                    category=(getattr(one, "category", "") or
                              livemod.category_of(one.callsign or "",
                                                  one.type_code or "")))
            kept.latitude = one.latitude
            kept.longitude = one.longitude
            kept.heading = one.heading
            kept.altitude_ft = one.altitude_ft
            here[one.object_id] = kept
        # A live one the feed no longer hears is still at its gate: a parked
        # aeroplane's transponder goes off. It stays its 45 minutes, unless
        # the feed hears it moving again (_live_pose lets it go).
        heard = getattr(self, "_live_heard", set())
        for object_id, kept in self._parked_observed.items():
            if (kept.live and object_id not in here
                    and object_id not in heard
                    and object_id not in self._live_moving
                    and now - kept.parked_at < LIVE_PARKED_S):
                here[object_id] = kept
        self._parked_observed = here

    def _parked_for_good(self, flight: Flight, one) -> bool:
        """Whether a worked aeroplane that has stopped has parked.

        With the airport's stands to hand, standing on one is the answer.
        Without them, an arrival that has taxied in and stopped has parked
        and a departure that has stopped is holding somewhere.
        """
        if self.ground is not None:
            x, z = self.ground.to_xy(one.latitude, one.longitude)
            return self.ground.stand_at(x, z) is not None
        return flight.phase in ("taxi_in", "landed")

    @property
    def parked_observed(self) -> list["ParkedObserved"]:
        """Somebody else's aeroplanes standing on this airport."""
        return list(self._parked_observed.values())

    def _works_here(self, one, known: bool = False) -> bool:
        """Whether an observed aeroplane is this airport's business.

        Three ways of not being. A busy region has hundreds of aircraft in it
        and almost all of them are somewhere else. The few that are overhead
        are at thirty thousand feet on their way past. And a big field under
        FSLTL has a hundred aeroplanes parked on it, which are not on anybody's
        frequency -- putting those on strips would sequence the user behind
        sixty aircraft that are not going anywhere.

        ``known`` is whether it already has a strip. One that has been worked
        keeps its strip when it stops, because an aeroplane holding short with
        the brakes on is exactly the aeroplane the tower is about to talk to.
        """
        if not getattr(one, "positioned", False):
            return False
        if not self.field.latitude and not self.field.longitude:
            return False
        distance = haversine_nm(self.field.latitude, self.field.longitude,
                                one.latitude, one.longitude)
        if distance > OBSERVED_RANGE_NM:
            return False
        if not one.on_ground:
            return (one.altitude_ft - self.field.elevation_ft
                    ) <= OBSERVED_CEILING_FT
        return (known or one.ground_speed_kt >= TAXIING_KT
                or self._on_runway(one))

    def _observed_flight(self, one) -> Flight | None:
        """A strip for an aeroplane that has just come into view.

        The callsign is whatever the simulator calls it, which for FSLTL is
        the real flight's: that is the whole point, and it is why the operator
        and the language fall out of the same tables the invented traffic uses.
        An aeroplane with no callsign at all is skipped rather than given an
        invented one -- there is nothing to call it, and "unknown traffic" on
        a strip is worse than an aeroplane that is only on the map.
        """
        written = (one.callsign or "").strip().upper()
        if not written:
            return None
        type_code = observed_type(one.type_code)
        operator = by_icao(written[:3]) if (
            len(written) > 3 and written[:3].isalpha()
            and written[3].isdigit()) else None
        language = (operator.speaks_at(self.field.ident) if operator
                    else reply_language(self.field.ident, "en"))
        forms = every_language(written, type_code,
                               dialect=self.profile.dialect_for(self.field.ident))
        aircraft = Aircraft(forms["en"], type_code, wake_suffix(type_code),
                            callsigns=forms)
        # Reserved, so the invented traffic never hands the same number to a
        # second aeroplane while this one is on the field.
        self._used_callsigns.add(written)
        source = getattr(one, "source", "") or "simulator"
        category = (getattr(one, "category", "")
                    or livemod.category_of(written, type_code,
                                           operator=operator))
        return Flight(
            written=written, type_code=type_code, aircraft=aircraft,
            language=language, operator=operator, arriving=not one.on_ground,
            ifr=True, observed=True, observed_id=one.object_id,
            runway=self.field.arrival_runway or self.field.departure_runway,
            phase="taxi", step=0, source=source, category=category,
            origin=getattr(one, "origin", "") or "",
            destination=getattr(one, "destination", "") or "",
            hex=getattr(one, "hex", "") or "",
            squawk=_squawk(getattr(one, "squawk", "")),
        )

    def _read_observed(self, flight: Flight, one, now: float) -> None:
        """Put what the simulator says onto the strip, and work out the phase."""
        flight.latitude = one.latitude
        flight.longitude = one.longitude
        flight.altitude_ft = one.altitude_ft
        flight.heading = one.heading
        flight.ground_speed_kt = one.ground_speed_kt
        flight.vertical_rate_fpm = one.vertical_speed_fpm
        flight.on_ground = one.on_ground

        to_run = self._final_distance(one)
        flight.distance_nm = to_run if to_run is not None else haversine_nm(
            self.field.latitude, self.field.longitude,
            one.latitude, one.longitude)

        # A live one is drawn 30 s behind the feed; what it is doing is read
        # off where the real one is now (its lead), so each thing it does is
        # said before it is seen to do it -- a takeoff clearance half a
        # minute before the roll. Held by our tower, it is doing nothing.
        lead = getattr(one, "lead", None) or one
        flight.lead_at = (lead.latitude, lead.longitude)
        lead_to_run = to_run if lead is one else self._final_distance(lead)
        flight.lead_distance_nm = (lead_to_run if lead_to_run is not None
                                   else flight.distance_nm)
        was = flight.phase
        if flight.live_held:
            return
        flight.phase = self._observed_phase(flight, lead, lead_to_run)
        flight.arriving = flight.phase in ("inbound", "final")
        if flight.phase != was:
            # A phase change is the only thing worth saying anything about, and
            # it is checked on the next pass rather than here so that the words
            # are composed on the situation loop like everybody else's.
            flight.step = 0
            flight.next_at = now
        if self._on_runway(one) and (
                flight.phase in ("takeoff", "final", "landed")
                or self._lined_up(one)):
            self.runway_free_at = max(self.runway_free_at, now + 8.0)

    def _observed_phase(self, flight: Flight, one, to_run) -> str:
        """What an observed aeroplane is doing, from what it is doing.

        There is no plan to read and no instruction it was given, so this is
        entirely geometry and speed. It is deliberately coarse: the phase is
        used for the strip, for the frequency an aeroplane is worked on, and
        for the landing sequence, and none of those needs to know the
        difference between a base leg and a long final.
        """
        if one.on_ground:
            # An arrival first: it rolls down the runway at the same speed
            # a departure does, and the one thing that tells them apart is
            # that it was on final a moment ago.
            if flight.phase in ("final", "landed", "taxi_in"):
                return "landed" if one.ground_speed_kt >= ROLLING_KT else "taxi_in"
            if one.ground_speed_kt >= ROLLING_KT and self._on_runway(one):
                return "takeoff"
            if self._lined_up(one) and flight.phase in (
                    "taxi", "holding", "lineup", "pushback"):
                return "lineup"
            track = getattr(one, "track", None)
            if (track is not None and flight.phase in ("holding", "pushback")
                    and TAXIING_KT <= one.ground_speed_kt < PUSHBACK_KT
                    and abs((track - one.heading + 540.0) % 360.0 - 180.0)
                    > PUSHBACK_DEG):
                # Moving tail first off a stand: being pushed back.
                return "pushback"
            if one.ground_speed_kt >= TAXIING_KT:
                return "taxi"
            return "holding"
        if to_run is not None and to_run <= 12.0:
            return "final"
        climbing = one.vertical_speed_fpm > 300.0
        if climbing and flight.phase in ("takeoff", "departed", "taxi",
                                         "holding", "lineup"):
            return "departed"
        return "inbound"

    def _final_distance(self, one) -> float | None:
        """How far an observed aeroplane has to run, if it is on final.

        The same test the user gets: lined up with the landing runway to
        within thirty degrees, inside the centreline, and closing. Anything
        else is an aeroplane near the airport rather than one landing at it.
        """
        if one.on_ground:
            return None
        end = self._threshold(self.field.arrival_runway
                              or self.field.departure_runway)
        if end is None:
            return None
        along, across = offset_from_track(one.latitude, one.longitude,
                                          end[0], end[1], end[2])
        to_run = -along
        if not 0.0 < to_run <= 14.0 or abs(across) > 1.2:
            return None
        if abs((one.heading - end[2] + 540.0) % 360.0 - 180.0) > 30.0:
            return None
        return to_run

    def _lined_up(self, one) -> bool:
        """Whether an observed aeroplane is sitting on the runway, ready to go.

        The occupancy test used to read the phase, and the phase of a stopped
        aeroplane is "holding" whether it is holding at the point or holding
        on the pavement. So an airliner lined up and waiting was not counted,
        and the user was cleared for takeoff onto a runway with somebody
        already on it -- which is what "it ignores the traffic" looks like
        from the flight deck.

        Alignment is the discriminator, the same one the surface watch uses on
        the user: an aeroplane holding short is pointing across the runway and
        one lined up is pointing along it.
        """
        if not getattr(one, "on_ground", False):
            return False
        heading = getattr(one, "heading", None)
        if heading is None:
            return False
        for name in (self.field.departure_runway, self.field.arrival_runway):
            end = self._threshold(name)
            if end is None:
                continue
            along, across = offset_from_track(one.latitude, one.longitude,
                                              end[0], end[1], end[2])
            if not (abs(across) <= RUNWAY_WIDTH_NM
                    and -0.15 <= along <= RUNWAY_LENGTH_NM):
                continue
            apart = abs(((float(heading) - end[2]) + 180.0) % 360.0 - 180.0)
            if apart <= LINED_UP_DEG:
                return True
        return False

    def _on_runway(self, one) -> bool:
        """Whether an observed aeroplane is on the runway in use."""
        for name in (self.field.departure_runway, self.field.arrival_runway):
            end = self._threshold(name)
            if end is None:
                continue
            along, across = offset_from_track(one.latitude, one.longitude,
                                              end[0], end[1], end[2])
            if (abs(across) <= RUNWAY_WIDTH_NM
                    and -0.15 <= along <= RUNWAY_LENGTH_NM):
                return True
        return False

    def _advance_observed(self, flight: Flight, now: float) -> None:
        """What the controller says to an aeroplane it cannot instruct.

        Four moments, and every one of them is a description rather than an
        order: the aeroplane is going to do this whatever is said, so the only
        way to be wrong is to say something it is not about to do. Each is said
        once per phase -- ``step`` is set by whichever branch actually speaks,
        so a phase that is not ready to be talked about yet is looked at again
        on the next pass rather than being used up.

        The landing clearance is the one with a condition on it. A controller
        with somebody on the runway would send an arrival around, and this one
        will not go around -- so nothing is said at all rather than a clearance
        that is wrong. Silence on a busy frequency is normal; a landing
        clearance over an occupied runway is not.
        """
        flight.next_at = now + 3.0
        if flight.live_held:
            return
        # A live departure's cut taxi: the rest when it is drawn there.
        self._rest_of_taxi(flight, now, inbound=False)
        if flight.step:
            return
        phase = flight.phase
        runway_out = self.field.departure_runway or flight.runway

        # The departure, as it happens -- for a live one half a minute
        # before it is seen to happen: its clearance and its push, its taxi
        # on the airport's own taxiways, its line-up and its takeoff. Nothing
        # it does is unannounced, and nothing announced is not then done,
        # because each is read off what the real one has just done.
        if phase == "pushback":
            flight.step = 1
            if ("DEL" in self.field.stations and flight.destination
                    and not flight.cleared):
                flight.cleared = True
                self._exchange(
                    flight, now, "DEL",
                    pilot_first=lambda p, a, s: p.request_clearance(
                        a, s, flight.destination),
                    controller=lambda c, a, s: c.ifr_clearance(
                        a, s, flight.destination,
                        initial_altitude=INITIAL_CLIMB_FT,
                        squawk=flight.squawk,
                        departure_freq=self._freq("DEP")),
                    pilot_back=lambda p, a, s: p.clearance(
                        a, flight.destination, flight.squawk,
                        INITIAL_CLIMB_FT),
                )
            self._exchange(
                flight, now, "GND" if "GND" in self.field.stations else "TWR",
                controller=lambda c, a, s: c.pushback_approved(a),
                pilot_back=lambda p, a, s: p.wilco(a),
            )
            return

        if phase == "taxi" and not flight.arriving:
            flight.step = 1
            route: list[str] = []
            hold_at = ""
            if self.ground is not None:
                planned = self._plan_departure(flight)
                if planned is not None:
                    route, hold_at, flight.taxi_rest, flight.taxi_split_xy = \
                        _said_route(self.ground, planned[0], self.field.ident,
                                    inbound=False)
            self._exchange(
                flight, now, "GND" if "GND" in self.field.stations else "TWR",
                controller=lambda c, a, s: c.taxi_to_runway(
                    a, s, runway_out, route, hold_short_taxiway=hold_at),
                pilot_back=lambda p, a, s: p.taxi(
                    a, runway_out, route=_spell_route(route),
                    hold_short_taxiway=_spell_route([hold_at]) if hold_at
                    else ""),
            )
            return

        if phase == "lineup":
            flight.step = 1
            self._exchange(
                flight, now, "TWR",
                controller=lambda c, a, s: c.line_up_and_wait(
                    a, runway_out, station=s),
                pilot_back=lambda p, a, s: p.line_up(a, runway_out),
            )
            return

        if phase == "takeoff":
            flight.step = 1
            self._exchange(
                flight, now, "TWR",
                controller=lambda c, a, s: c.cleared_for_takeoff(
                    a, runway_out, self.field.weather, station=s),
                pilot_back=lambda p, a, s: p.takeoff(a, runway_out),
            )
            return

        if phase == "inbound":
            flight.step = 1
            self._exchange(
                flight, now, "APP",
                pilot_first=lambda p, a, s: p.checking_in(
                    a, s, flight.altitude_ft, climbing=False),
                controller=lambda c, a, s: c.radar_contact(a, s),
            )
            return

        if phase == "final":
            if flight.lead_distance_nm > LANDING_CLEARANCE_NM:
                return                     # not yet close enough to clear
            if self.runway_occupied(now):
                return                     # and not while somebody is on it
            flight.step = 1
            runway = self.field.arrival_runway or flight.runway
            self._exchange(
                flight, now, "TWR",
                controller=lambda c, a, s: c.cleared_to_land(
                    a, runway, self.field.weather),
                pilot_back=lambda p, a, s: p.land(a, runway),
            )
            return

        if phase == "departed":
            station, mhz = self._next_station("DEP", flight.language)
            if not station:
                return
            flight.step = 1
            self._exchange(
                flight, now, "TWR",
                controller=lambda c, a, s: c.handoff(a, station, mhz),
                pilot_back=lambda p, a, s: p.contact(a, station, mhz),
            )
            return

        if phase == "taxi_in":
            station, mhz = self._next_station("GND", flight.language)
            if not station:
                return
            flight.step = 1
            self._exchange(
                flight, now, "TWR",
                controller=lambda c, a, s: c.handoff(a, station, mhz),
                pilot_back=lambda p, a, s: p.contact(a, station, mhz),
            )
            if flight.source == "live":
                # And where it goes, which it then does: to the ramp, the
                # stand being the one it rolls onto.
                self._exchange(
                    flight, now, "GND",
                    controller=lambda c, a, s: c.taxi_to_parking(a, s, []),
                    pilot_back=lambda p, a, s: p.leaving(a),
                )
            return

    def _protect_the_pilot(self, now: float) -> None:
        """The pilot's runway, against the real aeroplanes (occupancy):

        - with the pilot on the runway, a live arrival on short final for it
          is sent around -- taken over and flown round by us, the real one's
          positions ignored from then on;
        - with the pilot on the runway or on a final inside 4 nm, a live
          departure about to go onto it is held short, and let go once the
          pilot is clear, one at a time, 90 s apart;
        - on the ground, a live aeroplane taxiing at the pilot stops and
          gives way.

        Said on the tower's frequency, where the pilot hears it only when
        tuned there, and a hold short at most once a minute. After LocalTC's
        traffic manager, which does the same three things.
        """
        hold = self.hold_live
        user = self.user
        on_runway = self._user_on_runway()
        to_run = self._user_on_final()
        busy = on_runway or (to_run is not None and to_run <= PILOT_FINAL_NM)
        for flight in list(self.flights):
            if flight.done or not flight.observed or flight.source != "live":
                continue
            # Arrivals: one about to land on top of the pilot goes around.
            if (flight.phase == "final" and not flight.on_ground
                    and flight.distance_nm <= LIVE_GO_AROUND_NM
                    and (on_runway or (to_run is not None
                                       and flight.distance_nm - to_run
                                       < LIVE_BEHIND_NM
                                       and flight.distance_nm > to_run))):
                self._take_over_for_a_go_around(flight, now)
                continue
            if hold is None:
                continue
            # Departures: kept off the pilot's runway.
            if (flight.live_held != "give way"
                    and flight.phase in ("lineup", "takeoff")
                    and not flight.live_held and busy
                    and not self._on_runway(flight)):
                flight.live_held = "runway"
                hold(flight.hex, True)
                decision("live_traffic", event="held short",
                         callsign=flight.written,
                         why="the pilot is on the runway or its final")
                if now - self._hold_short_said >= HOLD_SHORT_EVERY_S:
                    self._hold_short_said = now
                    self._exchange(
                        flight, now, "TWR",
                        controller=lambda c, a, s, f=flight:
                        c.hold_short_instruction(
                            a, self.field.departure_runway or f.runway),
                        pilot_back=lambda p, a, s: p.wilco(a),
                    )
            # On the ground: give way to the pilot taxiing.
            ahead = self._pilot_ahead(flight)
            if ahead and not flight.live_held:
                flight.live_held = "give way"
                hold(flight.hex, True)
            elif flight.live_held == "give way" and not ahead:
                flight.live_held = ""
                hold(flight.hex, False)
        # Let the held departures go once the pilot is clear: one at a time.
        if hold is not None and not busy \
                and now - self._last_release >= RELEASE_GAP_S:
            waiting = [f for f in self.flights
                       if f.live_held == "runway" and not f.done]
            if waiting:
                first = waiting[0]
                first.live_held = ""
                first.step = 0                 # its line-up is said now
                hold(first.hex, False)
                self._last_release = now
                decision("live_traffic", event="released",
                         callsign=first.written, why="the pilot is clear")

    def _rejoin_the_feed(self, seen, now: float) -> list:
        """The live arrivals we sent around and flew, given back to the feed
        once ours is parked (the pilot's rule, 30 Oct 2026): not before --
        the real one landed half a circuit ahead of ours -- and not never.
        Ours goes the moment the feed hears the real one; a real one the
        feed does not hear (a transponder off at the gate) leaves ours on
        the stand, as one of the parked, for LIVE_PARKED_S. Returns ``seen``
        without the feed's copies still being ignored."""
        for object_id, ours in list(self._flown_for.items()):
            if object_id in self._parked_since:
                continue
            if ours.done or ours.static or ours.parked_at:
                self._parked_since[object_id] = now
        heard = {one.object_id for one in seen
                 if getattr(one, "source", "") == "live"}
        for object_id, since in list(self._parked_since.items()):
            ours = self._flown_for.get(object_id)
            if object_id in heard:
                if ours is not None:
                    ours.done = True
                    self._used_callsigns.discard(ours.written)
                    if ours in self.statics:
                        self.statics.remove(ours)
                    decision("live_traffic", event="rejoined the feed",
                             callsign=ours.written,
                             why="ours is parked and the real one is heard")
                self._taken_over.discard(object_id)
            elif now - since > LIVE_PARKED_S:
                # Never heard again: ours stays, an ordinary parked one.
                self._taken_over.discard(object_id)
            else:
                continue
            self._flown_for.pop(object_id, None)
            self._parked_since.pop(object_id, None)
        return [one for one in seen if one.object_id not in self._taken_over]

    def _pilot_ahead(self, flight: Flight) -> bool:
        """The pilot on the ground, close in front of a live one taxiing."""
        user = self.user
        if (user is None or not user.has_position or not user.on_ground
                or not flight.on_ground
                or flight.ground_speed_kt < TAXIING_KT
                and flight.live_held != "give way"):
            return False
        distance_m = haversine_nm(flight.latitude, flight.longitude,
                                  user.latitude, user.longitude) * 1852.0
        if distance_m > GIVE_WAY_M:
            return False
        bearing = bearing_deg(flight.latitude, flight.longitude,
                              user.latitude, user.longitude)
        return abs((bearing - flight.heading + 540.0) % 360.0
                   - 180.0) <= GIVE_WAY_DEG

    def _take_over_for_a_go_around(self, flight: Flight, now: float) -> None:
        """A live arrival sent around: the real one will land, so from here
        it is ours, flown round by the flight model like any invented one,
        and the feed's copy of it is ignored -- until ours has landed and
        is parked, where the real one already is (_rejoin_the_feed)."""
        self._taken_over.add(flight.observed_id)
        self._flown_for[flight.observed_id] = flight
        flight.observed = False
        flight.source = "taken over"
        flight.arriving = True
        flight.runway = self.field.arrival_runway or flight.runway
        flight.performance = performance_for(
            flight.type_code, light=livemod.flight_category(flight)
            == livemod.GA)
        flight.airborne_model = True
        decision("live_traffic", event="sent around", callsign=flight.written,
                 why="the pilot is on the runway", to_run_nm=round(
                     flight.distance_nm, 1))
        reason = runway_traffic(flight.language)
        self._exchange(
            flight, now, "TWR",
            controller=lambda c, a, s: c.go_around(a, reason=reason),
            pilot_back=lambda p, a, s: p.going_around(a),
            command=("go_around", {}),
            urgent=True,
        )

    def set_user(self, user: "UserAircraft | None") -> None:
        """Tell the traffic where the person flying actually is.

        Without this the AI works a field with one aeroplane missing from it:
        the one that matters. With it, the runway the user is sitting on is an
        occupied runway, and an aeroplane on short final behind them is sent
        around by a controller that can see both.
        """
        self.user = user

    def _user_on_runway(self) -> bool:
        """Whether the person flying is on the runway in use."""
        user = self.user
        if user is None or not user.on_ground or not user.has_position:
            return False
        end = self._threshold(self.field.departure_runway
                              or self.field.arrival_runway)
        if end is None:
            return False
        along, across = offset_from_track(user.latitude, user.longitude,
                                          end[0], end[1], end[2])
        return (abs(across) <= RUNWAY_WIDTH_NM
                and -0.15 <= along <= RUNWAY_LENGTH_NM)

    def _user_on_final(self) -> float | None:
        """How far the person flying has to run, if they are on final.

        Airborne, lined up with the runway to within thirty degrees, inside
        the centreline and closing. Anything else is an aeroplane in the
        circuit or somewhere else entirely, and not a reason to hold anybody.
        """
        user = self.user
        if user is None or user.on_ground or not user.has_position:
            return None
        end = self._threshold(self.field.arrival_runway
                              or self.field.departure_runway)
        if end is None:
            return None
        along, across = offset_from_track(user.latitude, user.longitude,
                                          end[0], end[1], end[2])
        to_run = -along
        if not 0.0 < to_run <= 12.0 or abs(across) > 1.0:
            return None
        if abs((user.heading - end[2] + 540.0) % 360.0 - 180.0) > 30.0:
            return None
        return to_run

    def runway_occupied(self, now: float, ignore: Flight | None = None) -> bool:
        """Whether anybody at all is on the runway -- the user included.

        :meth:`runway_is_busy` deliberately does not count the user, because
        the controller asks it whether the user may be cleared. This one is
        what the traffic asks before it rolls.

        On an airport whose taxiways are driven, an aeroplane is on the
        runway because it is on it -- still rolling out, crossing, turning
        off -- and not only while a timer says so.
        """
        return (self.runway_is_busy(now) or self._user_on_runway()
                or self._somebody_on_runway(ignore))

    def _somebody_on_runway(self, ignore: Flight | None = None) -> bool:
        end = self._threshold(self.field.departure_runway
                              or self.field.arrival_runway)
        if end is None:
            return False
        for other in self.flights:
            if other is ignore or other.done or not other.positioned:
                continue
            if not other.on_ground or not other.on_layout:
                continue
            if other.phase in ("lineup",) and other.taxi is None:
                continue          # counted as lined up, not as in the way
            along, across = offset_from_track(other.latitude, other.longitude,
                                              end[0], end[1], end[2])
            # The runway itself, not the strip around it: an aeroplane at the
            # holding point is forty-five metres off the centreline and
            # counting it sent every arrival round.
            if abs(across) <= self.ON_RUNWAY_NM and \
                    -0.15 <= along <= RUNWAY_LENGTH_NM:
                return True
        return False

    # Thirty metres: half a wide runway and a little more.
    ON_RUNWAY_NM = 30.0 / 1852.0

    # ------------------------------------------------------------------
    # the tower, watching
    # ------------------------------------------------------------------

    # How close an arrival gets to a blocked runway before it is sent around,
    # and how much room it needs behind the one in front.
    #
    # The spacing is what it is because of the runway rather than because of
    # wake: an aeroplane is on it for the best part of a minute after it
    # lands, so a stream built to anything tighter than that is a stream that
    # has to send every other aeroplane round again.
    GO_AROUND_NM = 1.3
    SPACING_NM = 3.5

    def _watch(self, now: float) -> None:
        """Look at what is actually happening and say something about it.

        Everything else in this module is a flight following its own script.
        This is the part that is not: a controller looking at a runway with
        somebody on it and an aeroplane a mile from it, and doing what a
        controller does about that.
        """
        if now < self._watched_at:
            return
        self._watched_at = now + 1.0
        for flight in list(self.flights):
            if flight.done or flight.observed or not flight.airborne_model:
                # An observed aeroplane is flown by whoever injected it and
                # will not go around, slow down or extend for anybody. Telling
                # it to would be a controller talking to itself.
                continue
            if flight.arriving and flight.phase in ("inbound", "final"):
                self._watch_final(flight, now)
                self._watch_spacing(flight, now)
            elif flight.phase == "downwind":
                self._watch_downwind(flight, now)

    # A stable approach, the crew's own gate: inside this distance it is on
    # the centreline to within this, and pointing down the runway to within
    # this, or it is not landing.
    STABLE_BY_NM = 1.5
    STABLE_BY_NM_LIGHT = 0.5
    STABLE_ACROSS_M = 60.0
    STABLE_HEADING_DEG = 20.0

    def _stable_by_nm(self, flight: Flight) -> float:
        """Where this crew's own stable-approach gate is: an airliner's a mile
        and a half out, a light aircraft's much closer in."""
        return (self.STABLE_BY_NM if flight.performance.approach_kt >= 110
                else self.STABLE_BY_NM_LIGHT)

    def _base_nm(self, flight: Flight) -> float:
        """How far out the base turn is, so final is long enough to be
        stable by the gate. A fixed mile and a half suited a Cessna and put
        an airliner on a final shorter than its own gate, so it went round
        every time."""
        radius = _turn_radius_nm(flight.performance.approach_kt * 1.15)
        return max(CIRCUIT_BASE_NM, self._stable_by_nm(flight) + 1.0 + radius)

    def _unstable(self, flight: Flight) -> bool:
        end = self._threshold(flight.runway)
        if end is None or flight.distance_nm > self._stable_by_nm(flight):
            return False
        _along, across = self._runway_frame(flight, end[2])
        off = abs((flight.heading - end[2] + 540.0) % 360.0 - 180.0)
        return (abs(across) * 1852.0 > self.STABLE_ACROSS_M
                or off > self.STABLE_HEADING_DEG)

    def _watch_final(self, flight: Flight, now: float) -> None:
        """An aeroplane on short final to a runway that may not be free."""
        if flight.phase != "final" or flight.on_ground:
            return
        if (flight.distance_nm > self.GO_AROUND_NM
                and not self._unstable(flight)):
            return
        if self._already_told(flight, "go_around"):
            return
        if not flight.observed and self._unstable(flight):
            # Not lined up with the runway by now, and a crew does not land
            # from there: they go around and say so. Without this an
            # aeroplane that turned final late touched down two hundred
            # metres to the side of the runway, on the apron, beside the
            # pilot's own stand.
            self._exchange(
                flight, now, "TWR",
                pilot_first=lambda p, a, s: p.going_around(a),
                controller=lambda c, a, s: c.roger(a),
                command=("go_around", {}),
                urgent=True,
            )
            return
        if not self._runway_blocked_for(flight, now):
            return
        reason = runway_traffic(flight.language)
        self._exchange(
            flight, now, "TWR",
            controller=lambda c, a, s: c.go_around(a, reason=reason),
            pilot_back=lambda p, a, s: p.going_around(a),
            command=("go_around", {}),
            urgent=True,
        )

    # How far past the normal turning point an aeroplane is asked to go before
    # it is brought back in regardless. An extension is a delay, not a
    # dismissal, and a crew told to extend and never told anything else would
    # fly to the coast.
    MAX_EXTEND_NM = 6.0
    # And past this it is simply gone: out of sight, and somebody else's.
    GIVE_UP_EXTENDING_NM = 20.0

    def _watch_downwind(self, flight: Flight, now: float) -> None:
        """An aeroplane at the point it would turn base, with somebody on final.

        Two decisions, and the second is the one that was missing: an aircraft
        told to extend has to be told when to stop. Without that it flies
        downwind until the map runs out, which is a controller who has
        forgotten about it.
        """
        # Where it would roll out if it turned base now, and whether that
        # leaves anybody crowded -- by then, not now.
        _along, across = self._runway_frame(
            flight, self._threshold(flight.runway)[2]) \
            if self._threshold(flight.runway) else (0.0, CIRCUIT_WIDTH_NM)
        room = self._room_on_final(flight.distance_nm, ignore=flight,
                                   within_s=self._rollout_s(flight, across))

        if flight.extending:
            if not room:
                # Still no gap. It keeps going until there is one -- the
                # arrivals are spaced five miles and more apart, so one comes
                # within a minute of the one abeam passing. Turning it in at
                # a fixed distance regardless put it on base beside an
                # arrival and across its approach.
                if flight.distance_nm > self.GIVE_UP_EXTENDING_NM:
                    flight.done = True
                return
            if self._already_told(flight, "continue"):
                return
            self._exchange(
                flight, now, "TWR",
                controller=lambda c, a, s: c.continue_approach(a),
                pilot_back=lambda p, a, s: p.wilco(a),
                command=("continue", {}),
            )
            return

        if (flight.distance_nm < self._base_nm(flight) - BASE_LOOKAHEAD_NM
                or room or self._already_told(flight, "extend")):
            return
        ahead = self._ahead_of(flight) or self._traffic_on_final(
            self.SPACING_NM * 3.0, ignore=flight)
        self._exchange(
            flight, now, "TWR",
            controller=lambda c, a, s: c.extend_downwind(
                a, reason=self._describe_arrival(ahead, flight.language)),
            pilot_back=lambda p, a, s: p.wilco(a),
            command=("extend", {}),
        )

    def _runway_blocked_for(self, flight: Flight, now: float) -> bool:
        """Whether this arrival will have something in its way when it gets there.

        The question a controller asks about an aeroplane a mile out is not
        whether the runway is clear now -- it rarely is, a mile out -- but
        whether it will be clear in the half minute the aeroplane still has to
        run. Asking the first question sent an arrival around every time the
        one in front of it was still rolling out, which on a single runway is
        every time.
        """
        if self._user_on_runway():
            return True
        if self._somebody_on_runway(ignore=flight):
            # Crossing it, backtracking along it, still turning off it:
            # whatever the timetable says, the runway is not clear.
            return True
        arrives_in = self._seconds_to_threshold(flight)
        if self.runway_free_at > now + arrives_in:
            return True
        for other in self.flights:
            if other is flight or other.done:
                continue
            # An aeroplane sitting on the runway waiting for a clearance is
            # not going anywhere on its own. Everything else that is on it --
            # rolling, landing -- is already counted by the time above.
            if other.phase == "lineup":
                return True
            if (other.arriving and other.phase == "final"
                    and other.distance_nm < flight.distance_nm
                    and flight.distance_nm - other.distance_nm < 0.8):
                return True
        return False

    def _approaching(self, ignore: Flight | None = None) -> list[Flight]:
        """Everybody committed to this runway, in miles still to run.

        The person flying is in the list on the same terms as anybody else,
        because to everything that reads it they are another aeroplane on
        final.
        """
        found = [f for f in self.flights
                 if f is not ignore and not f.done and not f.stuck
                 and f.arriving and f.phase in ("inbound", "final", "base")]
        # Only when they are on final, or in the circuit close to turning on
        # to it. The stand-in was added whatever the user was doing, at a
        # distance of nought when they were not on final -- so a pilot parked
        # at the gate was an arrival at the threshold, every departure was
        # held for them for ever, and nothing at the field ever took off.
        # On base or late downwind they are at the distance they still have
        # to fly, which is what makes the AI behind them extend rather than
        # turn in front of them (atc.circuit).
        to_run = self._user_on_final()
        if to_run is None:
            to_run = self._user_in_circuit()
        if to_run is not None:
            user = self._user_flight(to_run)
            if user is not None:
                found.append(user)
        found.sort(key=lambda f: f.distance_nm)
        return found

    def _room_on_final(self, at_nm: float,
                       ignore: Flight | None = None,
                       within_s: float = 0.0) -> bool:
        """Whether an aeroplane could roll out there without crowding anybody.

        This is what the base turn actually waits for. Turning when the
        aeroplane ahead merely happens to be outside some radius is how two
        arrivals end up a fifth of a mile apart on a four mile final -- which
        is what this used to do.
        """
        # Where everybody will be by the time it rolls out, not where they
        # are now. An arrival at a hundred and eighty knots closes three
        # miles in the minute a base leg takes, and judging the gap as it
        # stood turned an aeroplane in front of one that then ran into it.
        return all(abs(max(0.0, other.distance_nm
                           - other.ground_speed_kt * within_s / 3600.0)
                       - at_nm) >= self.SPACING_NM
                   for other in self._approaching(ignore))

    def _rollout_s(self, flight: Flight, across_nm: float) -> float:
        """How long from here to rolling out on final: the base leg and the
        turn at the end of it."""
        speed = max(60.0, flight.performance.approach_kt * 1.15)
        return (abs(across_nm) + 1.6 * _turn_radius_nm(speed)) \
            / speed * 3600.0

    def _ahead_of(self, flight: Flight) -> Flight | None:
        """The aeroplane immediately in front of this one on the approach."""
        nearer = [f for f in self._approaching(flight)
                  if f.distance_nm < flight.distance_nm]
        return nearer[-1] if nearer else None

    def _watch_spacing(self, flight: Flight, now: float) -> None:
        """Keep an arrival off the back of the one in front of it.

        A speed is what a controller has for this, and it is the instruction
        with the least drama in it: nobody is sent around, the aeroplane just
        slows down and the gap opens. It only works while there is still room
        to lose speed, so it is not offered to anything already committed.
        """
        # Below this there is nothing left to give: an aeroplane at two miles
        # is at its threshold speed and slowing it further is not a spacing
        # instruction, it is a stall warning.
        if flight.distance_nm < STABLE_BY_NM:
            return
        ahead = self._ahead_of(flight)
        gap = (flight.distance_nm - ahead.distance_nm) if ahead else 99.0

        if flight.assigned_speed_kt is not None:
            if gap >= self.SPACING_NM * 1.6 or ahead is None:
                if self._already_told(flight, "resume"):
                    return
                self._exchange(
                    flight, now, flight.position,
                    controller=lambda c, a, s: c.resume_normal_speed(a),
                    pilot_back=lambda p, a, s: p.wilco(a),
                    command=("resume", {}),
                )
            return

        if gap >= self.SPACING_NM or self._already_told(flight, "speed"):
            return
        # Slower than the one in front, or the gap merely stops shrinking
        # instead of opening.
        wanted = min(approach_speed(ahead.distance_nm, flight.performance) - 15.0,
                     flight.performance.approach_kt)
        slower = max(round(wanted / 10.0) * 10,
                     round(flight.performance.approach_kt * 0.85 / 10.0) * 10)
        self._exchange(
            flight, now, flight.position,
            controller=lambda c, a, s: c.speed_restriction(a, slower),
            pilot_back=lambda p, a, s: p.speed(a, slower),
            command=("speed", {"speed_kt": slower}),
        )

    def _traffic_on_final(self, miles: float,
                          ignore: Flight | None = None) -> Flight | None:
        """Anybody on final inside that distance, the user counted.

        The user does not have a :class:`Flight`, so a stand-in is made for
        them. It is never added to the roster: it exists for the length of
        this answer and says only what has to be known about it.
        """
        for other in self.flights:
            if other is ignore or other.done:
                continue
            if (other.arriving and other.phase in ("final", "base")
                    and other.distance_nm <= miles):
                return other
        to_run = self._user_on_final()
        if to_run is not None and to_run <= miles:
            return self._user_flight()
        return None

    # How far round the circuit the person flying can be and still count as
    # the next to land: base, or late on the downwind.
    USER_IN_CIRCUIT_NM = 4.0

    def _user_in_circuit(self) -> float | None:
        """What the person flying still has to fly to the threshold, when
        they are on base or late downwind; None anywhere else."""
        user = self.user
        if user is None or user.on_ground or not user.has_position:
            return None
        runway = self.field.arrival_runway or self.field.departure_runway
        if not runway or not self.field.ident:
            return None
        from .circuit import circuit_for

        published = self.navdb.circuit_data(self.field.ident, runway)
        side = published.side if published is not None and published.side else "left"
        circuit = circuit_for(self.navdb, self.field.ident, runway, side=side)
        if circuit is None:
            return None
        here = circuit.locate_at(user.latitude, user.longitude,
                                 user.altitude_ft, user.heading)
        if here.leg not in ("base", "downwind", "final"):
            return None
        if here.to_go_nm > self.USER_IN_CIRCUIT_NM:
            return None
        return here.to_go_nm

    def _user_ahead_of(self, flight: Flight) -> str:
        """The person flying, said as traffic, when they are ahead of this
        aeroplane in the circuit at a field nobody works -- the "number two,
        following the Cessna" an AI pilot announces. "" otherwise."""
        if self.field.stations.get("TWR") is not None or self.user is None:
            return ""
        to_run = self._user_on_final()
        if to_run is None:
            to_run = self._user_in_circuit()
        if to_run is None or to_run >= flight.distance_nm:
            return ""
        return article(make_of(self.user.type_code), flight.language)

    def _user_flight(self, distance_nm: float | None = None) -> Flight | None:
        """The person flying, in the shape the rest of this module reads."""
        user = self.user
        if user is None:
            return None
        forms = every_language(user.callsign or "UNKNOWN", user.type_code,
                               dialect=self.profile.dialect_for(self.field.ident))
        aircraft = Aircraft(forms["en"], user.type_code,
                            wake_suffix(user.type_code), callsigns=forms)
        return Flight(
            written=user.callsign, type_code=user.type_code,
            aircraft=aircraft, language="en", operator=None,
            arriving=True, runway=self.field.arrival_runway,
            phase="final", latitude=user.latitude, longitude=user.longitude,
            altitude_ft=user.altitude_ft, heading=user.heading,
            distance_nm=(distance_nm if distance_nm is not None
                         else self._user_on_final() or 0.0),
        )

    # -- departures ----------------------------------------------------

    # How long a departure with a door still open waits before looking again.
    PUSH_RETRY_S = 10.0

    def _may_push(self, flight: Flight) -> bool:
        """Whether this departure's doors are shut, as far as anybody knows."""
        if self.can_push is None:
            return True
        try:
            return bool(self.can_push(flight.written))
        except Exception:
            return True

    def _advance_departure(self, flight: Flight, now: float) -> None:
        phase = flight.phase

        if phase == "clearance":
            self._exchange(
                flight, now, "DEL",
                pilot_first=lambda p, a, s: p.request_clearance(
                    a, s, flight.destination),
                controller=lambda c, a, s: c.ifr_clearance(
                    a, s, flight.destination,
                    initial_altitude=INITIAL_CLIMB_FT, squawk=flight.squawk,
                    departure_freq=self._freq("DEP")),
                pilot_back=lambda p, a, s: p.clearance(
                    a, flight.destination, flight.squawk, INITIAL_CLIMB_FT),
                command=("clearance", {"altitude_ft": INITIAL_CLIMB_FT}),
            )
            flight.phase = "taxi"
            flight.next_at = now + self.random.uniform(25.0, 70.0)
            return

        if phase == "taxi" and not self._may_push(flight):
            flight.next_at = now + self.PUSH_RETRY_S
            return

        if phase == "taxi" and flight.on_layout:
            planned = self._plan_departure(flight)
            if planned is None:
                # Nowhere to go from here. It stays on its stand and is
                # dropped, rather than being put somewhere it cannot reach.
                flight.done = True
                return
            if self._crosses(planned[0], flight):
                # Somebody is coming the other way along it. It waits on
                # the stand until they are past.
                flight.next_at = now + 10.0
                return
            flight.planned = planned
            route, hold_at, rest, where = _said_route(
                self.ground, planned[0], self.field.ident, inbound=False)
            route = route or self._taxi_route()
            flight.taxi_rest, flight.taxi_split_xy = rest, where
            if flight.ifr:
                first = lambda p, a, s: p.request_taxi(a, s)
            elif flight.pattern_work:
                first = lambda p, a, s: p.call(
                    a, s, p.book.request_pattern_work, greeting=True)
            else:
                first = lambda p, a, s: p.request_vfr_departure(a, s)
            self._exchange(
                flight, now, "GND",
                pilot_first=first,
                controller=lambda c, a, s: c.taxi_to_runway(
                    a, s, flight.runway, route, hold_short_taxiway=hold_at),
                # The readback is the taxiways, where it holds and the
                # runway: never the whole clearance said back.
                pilot_back=lambda p, a, s: p.taxi(
                    a, flight.runway, route=_spell_route(route),
                    hold_short_taxiway=_spell_route([hold_at]) if hold_at
                    else ""),
                command=("taxi", {"runway": flight.runway}),
            )
            flight.phase = "ready"
            flight.next_at = now + 5.0
            return

        if phase == "ready" and flight.on_layout and (
                flight.taxi is not None or flight.planned is not None):
            # Still taxiing. It asks for the runway when it is at the
            # holding point, not when a timer says it should be -- and is
            # given the rest of a cut taxi at the taxiway it held short of.
            self._rest_of_taxi(flight, now, inbound=False)
            flight.next_at = now + 2.0
            return

        if phase == "taxi":
            route = self._taxi_route()
            if flight.ifr:
                first = lambda p, a, s: p.request_taxi(a, s)
            elif flight.pattern_work:
                # No clearance to collect: a VFR departure asks to taxi and,
                # if it is staying in the circuit rather than going somewhere,
                # says so.
                first = lambda p, a, s: p.call(
                    a, s, p.book.request_pattern_work, greeting=True)
            else:
                first = lambda p, a, s: p.request_vfr_departure(a, s)
            self._exchange(
                flight, now, "GND",
                pilot_first=first,
                controller=lambda c, a, s: c.taxi_to_runway(
                    a, s, flight.runway, route),
                pilot_back=lambda p, a, s: p.taxi(
                    a, flight.runway, route=_spell_route(route)),
                command=("taxi", {"runway": flight.runway}),
            )
            flight.phase = "ready"
            flight.next_at = now + self.random.uniform(50.0, 140.0)
            return

        if phase == "ready":
            # At the holding point and asking for the runway. What happens
            # next is not this flight's decision. At an AFIS the call opens
            # the exchange that gives the runway (``_afis_exchange``), so it
            # is not made twice.
            if not (self.uncontrolled and self.field.afis is not None):
                self._say(flight, now, "TWR", from_pilot=True,
                          make=lambda p, a, s: p.ready_for_departure(
                              a, flight.runway, s))
            flight.phase = "waiting"
            flight.next_at = now + self.random.uniform(4.0, 12.0)
            return

        if phase in ("waiting", "lineup"):
            self._work_the_departure(flight, now)
            return

        if phase == "takeoff":
            # Rolling, or climbing away. The runway is left behind when the
            # aeroplane has actually left it.
            climbing = (flight.altitude_ft
                        > self.field.elevation_ft + AIRBORNE_BY_FT)
            if flight.on_ground or not climbing:
                flight.next_at = now + 2.0
                return
            if flight.pattern_work or flight.last_circuit:
                self._exchange(
                    flight, now, "TWR",
                    controller=lambda c, a, s: c.report_position(
                        a, c.REPORT_MIDFIELD_DOWNWIND),
                    pilot_back=lambda p, a, s: p.wilco(a),
                )
                flight.phase = "upwind"
                flight.step = 0
                flight.next_at = now + 20.0
                return
            flight.phase = "departed"
            flight.next_at = now + self.random.uniform(15.0, 40.0)
            return

        if phase == "departed" and flight.handed_off:
            # Flying away. It goes when it is out of sight, not the moment
            # it has changed frequency.
            if (flight.distance_nm > self.OUT_OF_SIGHT_NM
                    or flight.altitude_ft > self.field.elevation_ft
                    + self.OUT_OF_SIGHT_FT):
                flight.done = True
            flight.next_at = now + 5.0
            return

        if phase == "departed":
            if not flight.ifr:
                # Nobody to hand a VFR departure to at an airfield. It is
                # turned on course and then it is on its own.
                self._exchange(
                    flight, now, "TWR",
                    controller=lambda c, a, s: c.turn_out_approved(a),
                    pilot_back=lambda p, a, s: p.leaving(a),
                    command=("turn_out", {}),
                )
                flight.handed_off = True
                flight.next_at = now + 5.0
                return
            station, mhz = self._next_station("DEP", flight.language)
            if station:
                self._exchange(
                    flight, now, "TWR",
                    controller=lambda c, a, s: c.handoff(a, station, mhz),
                    pilot_back=lambda p, a, s: p.contact(a, station, mhz),
                    command=("handoff", {}),
                )
            flight.handed_off = True
            flight.next_at = now + 5.0
            return

        if phase in _CIRCUIT_PHASES:
            # Round the circuit after a departure: the legs fly themselves and
            # the aeroplane becomes an arrival when it turns final.
            flight.next_at = now + 4.0
            return

        if phase == "go_around":
            self._after_the_go_around(flight, now)

    # Where a departure that has been handed on stops being drawn: far enough
    # out, or high enough, that nobody on the field could still see it.
    OUT_OF_SIGHT_NM = 12.0
    OUT_OF_SIGHT_FT = 9000.0

    # -- where it is going, for the map ------------------------------------

    # How far ahead a path on the map runs once an aeroplane is in the air
    # and has nothing nearer to aim at.
    PATH_OUT_NM = 25.0

    def route_ahead(self, flight: Flight) -> dict:
        """The rest of this aeroplane's way, as the map draws it.

        ``path`` is points of [latitude, longitude, altitude in feet] from
        where it is to where it is going: the taxi route it is on, the
        runway, the climb out towards its destination; or the approach, the
        runway and the stand. ``waypoints`` names the ones worth naming, as
        a kind and a value the panel puts into words. Nothing is invented
        for an aeroplane somebody else is flying: the simulator does not say
        where FSLTL's are going, and a line to nowhere would be a guess.
        """
        extra = {
            "origin": flight.origin, "destination": flight.destination,
            "origin_icao": DESTINATION_AIRPORTS.get(flight.origin, ""),
            "destination_icao": DESTINATION_AIRPORTS.get(flight.destination,
                                                         ""),
            "squawk": flight.squawk,
            "make": make_of(flight.type_code),
            "airline": (flight.operator.icao if flight.operator else ""),
            "telephony": flight.aircraft.for_language("en").spoken_full,
            "stand": (self.ground.stands[flight.stand].number
                      if self.ground is not None and flight.stand is not None
                      and flight.stand < len(self.ground.stands) else None),
            "path": [], "waypoints": [],
        }
        if flight.observed:
            return extra
        try:
            path, waypoints = self._path(flight)
        except Exception:
            log.debug("could not work out %s's path", flight.written,
                      exc_info=True)
            return extra
        extra["path"] = [[round(lat, 6), round(lon, 6), round(alt)]
                         for lat, lon, alt in path]
        extra["waypoints"] = waypoints
        return extra

    def _path(self, flight: Flight):
        elevation = self.field.elevation_ft
        here = (flight.latitude, flight.longitude, flight.altitude_ft)
        path = [here]
        waypoints: list[dict] = []
        end = self._threshold(flight.runway)

        def mark(kind: str, value: str, lat: float, lon: float) -> None:
            waypoints.append({"kind": kind, "value": value,
                              "lat": round(lat, 6), "lon": round(lon, 6)})

        def along(taxi) -> None:
            if taxi is None or self.ground is None:
                return
            for x, z in taxi.points[max(1, taxi.index):]:
                lat, lon = self.ground.to_latlon(x, z)
                path.append((lat, lon, elevation))

        def out_of_the_circuit(lat: float, lon: float, alt: float,
                               heading: float) -> None:
            """Along the runway heading and then on course, climbing."""
            lat2, lon2 = project(lat, lon, heading, 6.0)
            path.append((lat2, lon2, max(alt, elevation + INITIAL_CLIMB_FT)))
            course = heading
            target = DESTINATION_AIRPORTS.get(flight.destination)
            airport = self.navdb.airport(target) if target else None
            if airport is not None:
                course = bearing_deg(lat2, lon2, airport.lat, airport.lon)
            lat3, lon3 = project(lat2, lon2, course, self.PATH_OUT_NM)
            path.append((lat3, lon3, elevation + CLIMB_TO_FT))
            mark("destination", target or flight.destination, lat3, lon3)

        departing = not flight.arriving and flight.phase not in _CIRCUIT_PHASES
        if departing and flight.phase in ("clearance", "taxi", "ready",
                                          "waiting", "holding", "lineup",
                                          "takeoff") and not (
                flight.phase == "takeoff" and not flight.on_ground):
            planned = flight.planned
            to_hold, onto = ((planned[0], planned[1]) if planned is not None
                             else (flight.taxi, flight.line_up))
            if to_hold is not onto:
                along(to_hold)
                if self.ground is not None and len(path) > 1 \
                        and flight.phase in ("clearance", "taxi", "ready"):
                    # The holding point: the end of the taxi out, before the
                    # line-up leg onto the runway.
                    mark("hold", flight.runway, path[-1][0], path[-1][1])
            along(onto)
            if end is not None:
                lat, lon, heading = end
                roll_from = path[-1]
                lift = project(roll_from[0], roll_from[1], heading, 1.2)
                path.append((lift[0], lift[1], elevation + 400.0))
                mark("runway", flight.runway, roll_from[0], roll_from[1])
                out_of_the_circuit(lift[0], lift[1], elevation + 400.0,
                                   heading)
            return path, waypoints

        if flight.phase in ("takeoff", "departed", "go_around") \
                and not flight.arriving and end is not None:
            out_of_the_circuit(flight.latitude, flight.longitude,
                               flight.altitude_ft, end[2])
            return path, waypoints

        if flight.phase in _CIRCUIT_PHASES and end is not None:
            lat, lon, heading = end
            side = -1.0 if flight.pattern_direction != "right" else 1.0
            base = self._base_nm(flight)
            wide = (heading + 90.0 * side) % 360.0
            legs = {
                "upwind": project(lat, lon, heading, CIRCUIT_UPWIND_NM),
                "crosswind": project(*project(lat, lon, heading,
                                              CIRCUIT_UPWIND_NM), wide,
                                     CIRCUIT_WIDTH_NM),
                "downwind": project(*project(lat, lon, heading, -base), wide,
                                    CIRCUIT_WIDTH_NM),
                "base": project(lat, lon, heading, -base),
            }
            order = ["upwind", "crosswind", "downwind", "base"]
            pattern = elevation + flight.performance.pattern_agl_ft
            for leg in order[order.index(flight.phase):]:
                point = legs[leg]
                path.append((point[0], point[1], pattern))
                mark("leg", leg, point[0], point[1])
            path.append((lat, lon, elevation))
            mark("threshold", flight.runway, lat, lon)
            return path, waypoints

        if flight.arriving and end is not None and flight.phase in (
                "inbound", "joining", "final", "landed"):
            lat, lon, heading = end
            if flight.phase != "landed":
                behind = project(lat, lon, (heading + 180.0) % 360.0,
                                 min(6.0, max(0.0, flight.distance_nm)))
                path.append((behind[0], behind[1],
                             glide_altitude(elevation, min(
                                 6.0, max(0.0, flight.distance_nm)),
                                 GLIDE_FT_PER_NM)))
                path.append((lat, lon, elevation + 50.0))
                mark("threshold", flight.runway, lat, lon)
            rollout = project(lat, lon, heading, 0.8)
            path.append((rollout[0], rollout[1], elevation))
            along(flight.taxi)
            return path, waypoints

        if flight.phase == "taxi_in":
            along(flight.taxi)
        if flight.stand is not None and self.ground is not None \
                and flight.stand < len(self.ground.stands):
            stand = self.ground.stands[flight.stand]
            lat, lon = self.ground.to_latlon(stand.x, stand.z)
            mark("stand", str(stand.number), lat, lon)
        return path, waypoints

    def _plan_departure(self, flight: Flight):
        """The way from where it is to the holding point, and onto the
        runway: two legs, because they are two clearances."""
        runway = self._runway_xy(flight.runway)
        if runway is None:
            return None
        threshold, heading = runway
        layout = self.ground
        stand = (layout.stands[flight.stand] if flight.stand is not None
                 else None)
        here = layout.to_xy(flight.latitude, flight.longitude)
        return (layout.departure(stand, here, threshold, heading,
                                 self.RUNWAY_LENGTH_M,
                                 avoid=self._avoid_user())
                or layout.departure(stand, here, threshold, heading,
                                    self.RUNWAY_LENGTH_M))

    # An arrival inside this is close enough that a departure is kept at the
    # holding point rather than put on the runway in front of it. Lining one
    # up with somebody on a two-mile final is how a tower ends up sending the
    # arrival around, which is the mistake this exists to not make.
    LINEUP_BLOCK_NM = 4.0
    # And the slack on top of the roll itself. A departure released with
    # exactly enough time is a departure that goes wrong.
    DEPARTURE_MARGIN_S = 25.0

    def _runway_taken_by_departure(self, flight: Flight) -> bool:
        """Whether another departure holds the runway: told to line up or
        to take off, and still on the ground."""
        for other in self.flights:
            if other is flight or other.done or other.arriving:
                continue
            if not other.on_ground:
                continue
            if other.phase in ("lineup", "takeoff") or any(
                    command.kind in ("line_up", "takeoff")
                    and not command.applied for command in other.commands):
                return True
        return False

    def _somebody_lined_up(self, ignore: Flight | None = None) -> bool:
        """Whether the runway already has an aeroplane sitting on it."""
        return any(
            other is not ignore and not other.done
            and (other.phase == "lineup"
                 or self._already_told(other, "line_up"))
            for other in self.flights)

    def _seconds_to_threshold(self, flight: Flight) -> float:
        """How long this arrival has left to run, at the speed it is doing."""
        return flight.distance_nm / max(30.0, flight.ground_speed_kt) * 3600.0

    def _nearest_arrival_s(self, flight: Flight) -> float:
        """How long before the closest thing on the approach is on the runway."""
        approaching = self._approaching(flight)
        if not approaching:
            return 1e6
        return min(self._seconds_to_threshold(other) for other in approaching)

    def _room_to_depart(self, flight: Flight, now: float) -> bool:
        """Whether a departure has time to be gone before anybody lands.

        A tower does not decide this in miles, it decides it in seconds: the
        runway has to be free, the roll takes most of a minute, and whatever
        is on final has to still be on final at the end of it. Working in
        miles is what let a departure be lined up in front of an arrival that
        then had to be sent around.
        """
        needed = (max(0.0, self.runway_free_at - now)
                  + RUNWAY_ROLL_S + self.DEPARTURE_MARGIN_S)
        for other in self._approaching(flight):
            if self._seconds_to_threshold(other) < needed:
                return False
        return True

    def _work_the_departure(self, flight: Flight, now: float) -> None:
        """Decide what to do with an aeroplane that wants the runway.

        The runway is one aeroplane wide, and it is not only AI aeroplanes on
        it: the person flying counts, and so does one of them on short final.
        Nothing is released while either is true, and what the departure is
        told depends on which of the two it is.
        """
        # An aeroplane already sitting on the runway has priority over one
        # still at the holding point: it cannot get out of the way, so the
        # moment the runway is free it goes, as long as there is time for the
        # roll. Making it wait for the full margin instead was what left a
        # departure stuck on the runway with an arrival closing on it -- and
        # the arrival was then the one that had to go round.
        pressing = (flight.phase == "lineup"
                    and not self.runway_occupied(now, ignore=flight)
                    and self._nearest_arrival_s(flight) >= RUNWAY_ROLL_S)

        flight.held_since = flight.held_since or now
        if flight.phase != "lineup" and self._runway_taken_by_departure(flight):
            # Somebody else already has the runway -- lined up, entering it,
            # or rolling. One departure at a time: the clearance that used to
            # go out here put a second aeroplane on the runway behind a
            # lined-up one, and the two rolled together.
            flight.next_at = now + self.random.uniform(4.0, 8.0)
            return

        if not pressing and not self._room_to_depart(flight, now):
            blocking = self._traffic_on_final(self.LINEUP_BLOCK_NM,
                                              ignore=flight)
            # Somebody is landing. The departure stays where it is: held at
            # the holding point if it is still there, and held on the runway
            # if it was already lined up before the arrival got this close.
            if (flight.phase != "lineup" and flight.step == 0
                    and blocking is not None):
                flight.step = 1
                self._exchange(
                    flight, now, "TWR",
                    controller=lambda c, a, s: c.hold_short_instruction(
                        a, flight.runway),
                    pilot_back=lambda p, a, s: p.wilco(a),
                    command=("hold_short", {"runway": flight.runway}),
                )
            flight.next_at = now + self.random.uniform(6.0, 14.0)
            return

        if self.runway_occupied(now, ignore=flight):
            # Somebody is rolling, and will be gone in half a minute. That is
            # what lining up and waiting is for -- for one aeroplane. A runway
            # with three aeroplanes lined up on it is not a runway, and it was
            # what sent the next arrival round.
            if (flight.phase != "lineup" and flight.step < 2
                    and not self._somebody_lined_up(flight)):
                flight.step = 2
                self._exchange(
                    flight, now, "TWR",
                    controller=lambda c, a, s: c.line_up_and_wait(
                        a, flight.runway, station=s),
                    pilot_back=lambda p, a, s: p.line_up(a, flight.runway),
                    command=("line_up", {"runway": flight.runway}),
                )
            flight.next_at = now + self.random.uniform(6.0, 14.0)
            return

        taking_the_runway = lambda p, a, s: p.line_up(a, flight.runway)
        if flight.pattern_work:
            self._exchange(
                flight, now, "TWR",
                controller=lambda c, a, s: c.closed_traffic_approved(
                    a, flight.runway, c.side(flight.pattern_direction),
                    self.field.weather, station=s),
                pilot_back=lambda p, a, s: p.takeoff(a, flight.runway),
                announce=taking_the_runway,
                command=("takeoff", {"runway": flight.runway}),
            )
        else:
            self._exchange(
                flight, now, "TWR",
                controller=lambda c, a, s: c.cleared_for_takeoff(
                    a, flight.runway, self.field.weather, station=s),
                pilot_back=lambda p, a, s: p.takeoff(a, flight.runway),
                announce=taking_the_runway,
                command=("takeoff", {"runway": flight.runway}),
            )
        self.runway_free_at = now + RUNWAY_ROLL_S
        flight.next_at = now + RUNWAY_ROLL_S * 0.5

    def _after_the_go_around(self, flight: Flight, now: float) -> None:
        """What happens to an aeroplane that has just been sent around.

        It is climbing away on the runway heading. Once it is high enough and
        far enough out it is put back into the circuit, which it then flies --
        so a go-around costs it a full circuit, exactly as one does.
        """
        pattern_ft = (self.field.elevation_ft
                      + flight.performance.pattern_agl_ft)
        if (flight.altitude_ft < pattern_ft - 200.0
                and flight.distance_nm > -CIRCUIT_UPWIND_NM):
            flight.next_at = now + 3.0
            return
        flight.phase = "upwind"
        flight.arriving = False
        flight.step = 0
        flight.next_at = now + 5.0

    # -- arrivals ------------------------------------------------------

    # Where on the approach each thing happens, in miles to run.
    HANDOFF_TO_TOWER_NM = 12.0
    LANDING_CLEARANCE_NM = 8.0
    TOUCHDOWN_NM = 0.15

    def _advance_arrival(self, flight: Flight, now: float) -> None:
        phase = flight.phase

        if phase == "inbound" and not flight.ifr:
            # A VFR arrival calls the tower up itself from outside the circuit
            # and is given a join, not a handoff and an instrument approach.
            if flight.step == 0:
                direction = flight.pattern_direction

                def joined(c, a, s):
                    return c.enter_pattern(
                        a, s, flight.runway, leg=c.leg("downwind"),
                        direction=c.side(direction),
                        report=c.REPORT_MIDFIELD_DOWNWIND)

                def read_back(p, a, s):
                    phrase = self._phrase(flight.language)
                    return p.pattern_entry(
                        a, flight.runway, phrase.leg("downwind"),
                        phrase.side(direction))

                self._exchange(
                    flight, now, "TWR",
                    pilot_first=lambda p, a, s: p.inbound_for_landing(a, s),
                    controller=joined,
                    pilot_back=read_back,
                    command=("pattern", {"leg": "downwind"}),
                )
                flight.step = 1
                flight.phase = "joining"
                flight.next_at = now + 4.0
                return
            flight.next_at = now + 4.0
            return

        if phase == "joining":
            # Flying to the point the downwind starts from. It joins when it
            # gets there, not when a timer says so.
            if self._distance_to_join(flight) > JOIN_REACHED_NM:
                flight.next_at = now + 3.0
                return
            flight.phase = "downwind"
            flight.arriving = False
            flight.step = 0
            flight.next_at = now + 3.0
            return

        if phase in _CIRCUIT_PHASES:
            flight.next_at = now + 3.0
            return

        if phase == "inbound":
            # One check-in with approach, then it flies the arrival until the
            # tower takes it. The flying is done by the model, at its own
            # speed.
            #
            # Approach answers it. This used to be the one transmission in
            # the whole roster that nobody replied to, and a pilot sitting on
            # the approach frequency heard an aeroplane check in and then
            # minutes of silence -- which does not read as a quiet frequency,
            # it reads as a radio that is dropping transmissions.
            if flight.step == 0:
                flight.step = 1
                self._exchange(
                    flight, now, "APP",
                    pilot_first=lambda p, a, s: p.checking_in(
                        a, s, flight.altitude_ft, climbing=False),
                    controller=lambda c, a, s: c.radar_contact(a, s),
                )
            if flight.distance_nm > self.HANDOFF_TO_TOWER_NM:
                flight.next_at = now + 4.0
                return
            station, mhz = self._next_station("TWR", flight.language)
            if station:
                self._exchange(
                    flight, now, "APP",
                    controller=lambda c, a, s: c.handoff(a, station, mhz),
                    pilot_back=lambda p, a, s: p.contact(a, station, mhz),
                    command=("handoff", {}),
                )
            flight.phase = "final"
            flight.step = 0
            flight.next_at = now + 3.0
            return

        if phase == "final":
            self._work_the_arrival(flight, now)
            return

        if phase == "landed":
            if flight.ground_speed_kt > 30.0:
                flight.next_at = now + 2.0
                return
            if self.ground is not None and not flight.on_layout:
                self._plan_arrival(flight)
            self._stop_flying(flight)
            station, mhz = self._next_station("GND", flight.language)
            if station:
                self._exchange(
                    flight, now, "TWR",
                    controller=lambda c, a, s: c.handoff(a, station, mhz),
                    pilot_back=lambda p, a, s: p.contact(a, station, mhz),
                    command=("handoff", {}),
                )
            flight.phase = "taxi_in"
            flight.next_at = now + self.random.uniform(20.0, 50.0)
            return

        if phase == "taxi_in" and flight.on_layout:
            if flight.step < 10:
                route, hold_at, rest, where = _said_route(
                    self.ground, flight.taxi, self.field.ident, inbound=True)
                route = route or self._taxi_route()
                flight.taxi_rest, flight.taxi_split_xy = rest, where
                self._exchange(
                    flight, now,
                    "GND" if "GND" in self.field.stations else "TWR",
                    controller=lambda c, a, s: c.taxi_to_parking(
                        a, s, route, hold_short_taxiway=hold_at),
                    pilot_back=lambda p, a, s: p.leaving(a),
                    command=("taxi_in", {}),
                )
                flight.step = 10
            self._rest_of_taxi(flight, now, inbound=True)
            # Parked a while, and then off the radio -- the passengers are off
            # and it is nobody's business any more. It stays on its stand as
            # one of the parked aeroplanes if the apron has room for it, and
            # is its next departure; it used to vanish, in front of a pilot
            # who had just watched it taxi in.
            if flight.parked_at and now - flight.parked_at > self.PARKED_S:
                self._release_all(flight)
                if len(self.statics) < self._parked_wanted():
                    self._make_static(flight)
                    return
                flight.done = True
            flight.next_at = now + 5.0
            return

        if phase == "taxi_in":
            route = self._taxi_route()
            self._exchange(
                flight, now, "GND" if "GND" in self.field.stations else "TWR",
                controller=lambda c, a, s: c.taxi_to_parking(a, s, route),
                pilot_back=lambda p, a, s: p.leaving(a),
                command=("taxi_in", {}),
            )
            flight.done = True
            return

        if phase == "go_around":
            self._after_the_go_around(flight, now)

    # How long an arrival stays on its stand before it is taken away.
    PARKED_S = 180.0

    def _plan_arrival(self, flight: Flight) -> None:
        """Off the runway at the first exit and on to a free stand.

        With no stand free it rolls off the runway and is taken away there,
        which is better than being parked on top of somebody.
        """
        runway = self._runway_xy(flight.runway)
        layout = self.ground
        if runway is None or layout is None:
            return
        threshold, heading = runway
        stand = self._free_stand(airliner=flight.operator is not None
                                 or flight.ifr, prefer=flight.planned_stand)
        if stand is None:
            return
        x, z = layout.to_xy(flight.latitude, flight.longitude)
        # Round whoever is standing still -- the pilot, a departure at its
        # holding point -- where there is a way round. An exit planned
        # through a holding point left the arrival on the runway waiting
        # for the departure, and the departure waiting for the runway.
        standing = self._avoid_user() | self._avoid_standing(flight)
        taxi = None
        for avoid in (standing, self._avoid_user(), set()):
            taxi = layout.vacate(x, z, flight.heading, threshold, heading,
                                 self.RUNWAY_LENGTH_M, stand, avoid=avoid)
            if taxi is not None:
                break
        if taxi is None:
            return
        flight.on_layout = True
        flight.stand = stand.index
        flight.planned_stand = None
        flight.taxi = taxi

    def _work_the_arrival(self, flight: Flight, now: float) -> None:
        """An aeroplane on final: clear it, then watch it land."""
        if flight.step == 0:
            if flight.distance_nm > self.LANDING_CLEARANCE_NM:
                flight.next_at = now + 3.0
                return
            flight.step = 1
            ahead = [f for f in self.traffic_ahead(True)
                     if f is not flight and f.distance_nm < flight.distance_nm]
            flight.sequence = len(ahead) + 1
            sequence = ""
            if flight.sequence > 1 and ahead:
                sequence = self._sequence_text(flight, ahead[-1])
            following = self._user_ahead_of(flight)
            if flight.pattern_work:
                # Round again: the option covers the touch-and-go without the
                # tower having to know which one the pilot has chosen.
                self._exchange(
                    flight, now, "TWR",
                    pilot_first=lambda p, a, s: p.on_leg(
                        a, self._phrase(flight.language).leg("base"),
                        following=following),
                    controller=lambda c, a, s: c.cleared_touch_and_go(
                        a, flight.runway, self.field.weather,
                        sequence=sequence, station=s),
                    pilot_back=lambda p, a, s: p.option(a, flight.runway),
                    command=("touch_and_go", {"runway": flight.runway}),
                )
            elif not flight.ifr:
                self._exchange(
                    flight, now, "TWR",
                    pilot_first=lambda p, a, s: p.on_leg(
                        a, self._phrase(flight.language).leg("final"),
                        following=following),
                    controller=lambda c, a, s: c.cleared_to_land(
                        a, flight.runway, self.field.weather, station=s,
                        sequence=sequence),
                    pilot_back=lambda p, a, s: p.land(a, flight.runway),
                    command=("land", {"runway": flight.runway}),
                )
            else:
                self._exchange(
                    flight, now, "TWR",
                    pilot_first=lambda p, a, s: p.established(a, flight.runway),
                    controller=lambda c, a, s: c.cleared_to_land(
                        a, flight.runway, self.field.weather, station=s,
                        sequence=sequence),
                    pilot_back=lambda p, a, s: p.land(a, flight.runway),
                    command=("land", {"runway": flight.runway}),
                )
            flight.next_at = now + 3.0
            return

        if flight.distance_nm > self.TOUCHDOWN_NM or not flight.on_ground:
            flight.next_at = now + 2.0
            return

        self.runway_free_at = max(self.runway_free_at, now + LANDING_ROLL_S)
        # Down. Everything it was given for the approach is finished with.
        flight.assigned_speed_kt = None
        flight.assigned_heading = None
        flight.assigned_altitude_ft = None
        if flight.pattern_work:
            # A touch-and-go is on the runway for a moment and then round
            # again, which is what makes a circuit sound like a circuit.
            flight.phase = "takeoff"
            flight.arriving = False
            flight.step = 0
            # Whether to ask for the option again. When the answer is no it
            # still flies one more circuit -- and lands off that one, which is
            # what "last one" means to a pilot in the circuit.
            flight.pattern_work = self.random.random() < 0.7
            flight.last_circuit = not flight.pattern_work
            flight.next_at = now + 4.0
            return
        flight.phase = "landed"
        flight.next_at = now + 4.0

    # ------------------------------------------------------------------
    # putting words together
    # ------------------------------------------------------------------

    @property
    def uncontrolled(self) -> bool:
        """Whether this field has no controller: worked by nobody, on its
        traffic frequency, or by an information service, which gives
        information and clears nobody."""
        return not self.field.stations and (
            self.field.advisory is not None or self.field.afis is not None)

    @property
    def _announcing_on(self) -> str:
        """The position an uncontrolled field's traffic is heard on."""
        return "INFO" if self.field.afis is not None else "CTAF"

    def _aerodrome_information(self, ident: str) -> Station | None:
        from . import afis

        for station in self.navdb.stations(ident):
            if (station.position == "INFO" and station.mhz
                    and afis.kind_of(self.navdb, station) == afis.AERODROME):
                return station
        return None


    def _exchange(self, flight: Flight, now: float, position: str, *,
                  pilot_first=None, controller=None, pilot_back=None,
                  announce=None, command=None, urgent: bool = False) -> None:
        """Queue a complete exchange, spaced the way a real one sounds.

        At an uncontrolled field there is no exchange. There is one aeroplane
        saying what it is about to do, and everybody else listening -- so the
        controller's half is dropped, and so is the readback of it, which is a
        readback of something nobody said.

        ``announce`` is what to say instead, for the exchanges where neither
        half of the real one works on its own: reading back a takeoff
        clearance is not an announcement a pilot could make at a field where
        nobody issued one.

        ``command`` is ``(kind, values)``: what the aeroplane does about it,
        timed to the end of the readback rather than to the start of the
        instruction. That gap is what makes the traffic look like it is
        listening -- you hear "go around", you hear it acknowledged, and then
        the nose comes up.

        ``urgent`` is for the instructions that cannot wait behind a queue of
        pleasantries. A go-around is one of them: it goes to the front, and
        the aeroplane acts on it whether or not the frequency was clear enough
        for the words to be heard.
        """
        if self.uncontrolled and self.field.afis is not None:
            self._afis_exchange(flight, now, pilot_first=pilot_first,
                                announce=announce, command=command)
            return
        if self.uncontrolled:
            say = announce or pilot_first or pilot_back
            if say is not None:
                self._queue(flight, now, "CTAF", True, say)
            if command is not None:
                # Nobody said it, but the aeroplane is still flying: at a field
                # with no tower a pilot who has to go around goes around.
                self._tell(flight, command[0], now + 1.0, **command[1])
            return

        self._exchanges += 1
        mark = self._exchanges
        at = now
        halves: list[Call | None] = []
        if pilot_first is not None:
            halves.append(self._queue(flight, at, position, True, pilot_first,
                                      exchange=mark))
            at += READBACK_GAP_S + self.random.uniform(0.4, 2.0)
        if controller is not None:
            halves.append(self._queue(flight, at, position, False, controller,
                                      urgent=urgent, exchange=mark))
            at += READBACK_GAP_S + self.random.uniform(0.2, 1.2)
        if pilot_back is not None:
            halves.append(self._queue(flight, at, position, True, pilot_back,
                                      urgent=urgent, exchange=mark))

        # All of it or none of it. A half that would not compose -- a
        # phrasebook with no wording for this in this language, or a value the
        # sentence needed and did not have -- used to be dropped on its own,
        # which left an aeroplane calling a controller who never answered, or
        # a controller answering nobody. The aeroplane still does what it was
        # told: it is the words that failed, not the clearance.
        if any(half is None for half in halves):
            self.pending = [c for c in self.pending if c.exchange != mark]

        if command is not None:
            self._tell(flight, command[0], at, **command[1])

    def _afis_exchange(self, flight: Flight, now: float, *, pilot_first=None,
                       announce=None, command=None) -> None:
        """An exchange with an AFIS: the pilot calls up and is given the
        field (runway in use, wind, QNH, traffic), or calls ready or on final
        and is told the runway is free, the wind, and "at your discretion";
        the pilot acknowledges. Nothing the pilot would only have said back
        to a controller -- a readback of a takeoff clearance, "line up and
        wait" -- is said at all. UNVERIFIED: the acknowledgement as a plain
        "roger".
        """
        kind = command[0] if command is not None else ""
        runway = (command[1].get("runway") if command is not None else "") \
            or flight.runway
        weather = self.field.weather or Weather()
        if kind == "takeoff":
            opening = lambda p, a, s: p.ready_for_departure(a, runway, station=s)
            answer = lambda c, a, s: c.afis_takeoff(a, weather)
        elif kind in ("land", "touch_and_go"):
            opening = pilot_first or announce
            answer = lambda c, a, s: c.afis_land(a, weather)
        elif kind in ("taxi", "pattern") and pilot_first is not None:
            opening = pilot_first
            answer = lambda c, a, s: c.afis_field(a, s, runway, weather)
        elif kind == "line_up":
            opening = answer = None
        else:
            opening, answer = announce or pilot_first, None
        at = now
        if opening is not None and answer is not None:
            self._exchanges += 1
            mark = self._exchanges
            halves = [self._queue(flight, at, "INFO", True, opening,
                                  exchange=mark)]
            at += READBACK_GAP_S + self.random.uniform(0.4, 2.0)
            halves.append(self._queue(flight, at, "INFO", False, answer,
                                      exchange=mark))
            at += READBACK_GAP_S + self.random.uniform(0.2, 1.2)
            halves.append(self._queue(flight, at, "INFO", True,
                                      lambda p, a, s: p.roger(a),
                                      exchange=mark))
            if any(half is None for half in halves):
                self.pending = [c for c in self.pending if c.exchange != mark]
        elif opening is not None:
            self._queue(flight, now, "INFO", True, opening)
        if command is not None:
            self._tell(flight, command[0], max(at, now + 1.0), **command[1])

    def _say(self, flight: Flight, now: float, position: str, *,
             from_pilot: bool, make) -> None:
        if self.uncontrolled:
            if not from_pilot:
                return
            position = self._announcing_on
        self._queue(flight, now, position, from_pilot, make)

    def _queue(self, flight: Flight, at: float, position: str,
               from_pilot: bool, make, urgent: bool = False,
               exchange: int = 0) -> Call | None:
        """Compose one transmission and queue it. None if there were no words.

        The return value is what lets :meth:`_exchange` refuse to say half of
        something: a sentence that will not compose is a silence, and a
        silence in the middle of an exchange is heard as a lost transmission.
        """
        station = self.field.stations.get(position) or (
            self.field.advisory if position == "CTAF"
            else self.field.afis if position == "INFO" else None)
        station_name = station.callsign_in(flight.language) if station else ""
        speaker = (flight.aircraft.for_language(flight.language).spoken_short
                   if from_pilot else station_name)
        try:
            if from_pilot:
                speaker_book = self._pilot(flight.language)
                speaker_book.greet = position not in flight.greeted
                text = make(speaker_book, flight.aircraft, station_name)
                flight.greeted.add(position)
            else:
                text = make(self._phrase(flight.language),
                            flight.aircraft, station_name)
        except Exception:
            log.debug("could not compose a %s transmission for %s",
                      position, flight.aircraft.callsign.written,
                      exc_info=True)
            return None
        if not text:
            return None
        call = Call(
            text=text, language=flight.language, from_pilot=from_pilot,
            position=position, speaker=speaker or station_name,
            flight=flight, at=at, urgent=urgent, exchange=exchange,
        )
        self.pending.append(call)
        return call

    def _phrase(self, language: str):
        key = (language, self.profile.region)
        found = self._phraseology.get(key)
        if found is None:
            found = phraseology_for(language, self.field.ident,
                                    region=self.profile.region)
            self._phraseology[key] = found
        return found

    def _pilot(self, language: str) -> PilotSpeech:
        found = self._pilots.get(language)
        if found is None:
            found = PilotSpeech(language, self._phrase(language))
            self._pilots[language] = found
        return found

    # ------------------------------------------------------------------
    # small facts about the field
    # ------------------------------------------------------------------

    def _freq(self, position: str) -> float:
        station = self.field.stations.get(position)
        return station.mhz if station else 0.0

    def _next_station(self, position: str,
                      language: str = "en") -> tuple[str, float]:
        """The next frequency, named the way the current exchange is being run.

        A French handoff hands you to "Orly Sol", not to "Orly Ground": the
        station's name is part of the transmission and follows its language.
        Empty where nobody works that position, which is what stops an
        aeroplane being sent to a frequency that cannot answer it.
        """
        station = self.field.stations.get(position)
        if station is None:
            return "", 0.0
        return station.callsign_in(language), station.mhz

    def _taxi_route(self) -> list[str]:
        """A short taxi route, as letters.

        The phraseology spells them, so they leave here as "A", "B" -- handing
        it "Alpha" would get it spelled a second time, letter by letter.
        """
        letters = list("ABCDEFGHJKLMNPQRSTVWY")
        count = self.random.choice((1, 2, 2, 3))
        return self.random.sample(letters, count)

    def _arrival_inside(self, miles: float) -> Flight | None:
        for flight in self.traffic_ahead(True):
            if flight.phase == "final" and flight.distance_nm <= miles:
                return flight
        return None

    def _describe_arrival(self, flight: Flight | None, language: str) -> str:
        if flight is None:
            return ""
        return landing_traffic(flight.describe(language), language)

    def _sequence_text(self, flight: Flight, ahead: Flight) -> str:
        phrase = self._phrase(flight.language)
        try:
            return phrase.sequence_number(flight.sequence,
                                          ahead.describe(flight.language))
        except Exception:
            return ""


def _said_route(layout, taxi, field: str, *, inbound: bool):
    """A route as a controller gives it (atc.taxi): ``(said now, held short
    of, the rest, where the rest is given)`` -- each taxiway once, its
    number only at the holding point or the exit, five at most before the
    cut, field words said as words -- from a ground Taxiing."""
    from . import taxi as taxiplan

    if layout is None or taxi is None:
        return [], "", [], None
    nodes = [node for node in getattr(taxi, "nodes", []) if node is not None]
    said = taxiplan.spoken_route(layout, nodes, inbound=inbound)
    names = [name for name, _ in said]
    first, hold_at, rest = taxiplan.split_route(names)
    where = None
    if rest:
        where = layout.xy[nodes[said[len(first)][1]]]
    return (taxiplan.say_words(field, first),
            taxiplan.say_words(field, [hold_at])[0] if hold_at else "",
            taxiplan.say_words(field, rest), where)


# How close one of ours gets to where it was told to hold short before the
# rest of a cut taxi is given. Chosen: a junction's width.
REST_OF_TAXI_M = 60.0


def _spell_route(route: list[str]) -> str:
    """``["A", "B"]`` -> ``"Alpha Bravo"``, for a pilot reading it back."""
    from .speech import taxiway

    return " ".join(taxiway(letter) for letter in route)


def _local_or_english(ident: str) -> str:
    """What a local light aircraft speaks: the field's own working language
    if it has one -- not one it merely accepts (Kortrijk's French)."""
    from .language import traffic_language

    return traffic_language(ident) or "en"


# Somewhere to be going. Spoken names, because they are read out on the radio.
_DESTINATIONS: tuple[str, ...] = (
    "Nice", "Lyon", "Toulouse", "Marseille", "Barcelona", "Madrid", "Lisbon",
    "Rome", "Milan", "Naples", "Munich", "Berlin", "Hamburg", "Vienna",
    "Zurich", "Geneva", "Brussels", "Amsterdam", "London Heathrow",
    "Manchester", "Dublin", "Copenhagen", "Stockholm", "Oslo", "Helsinki",
    "Warsaw", "Prague", "Budapest", "Athens", "Istanbul", "Casablanca",
    "Algiers", "Tunis", "New York", "Chicago", "Atlanta", "Dallas", "Denver",
    "Los Angeles", "Miami", "Toronto", "Montreal", "Dubai", "Doha",
    "Singapore", "Tokyo", "São Paulo", "Buenos Aires", "Mexico City",
)


# Where each of those is, so a departure's path on the map points the right
# way out of the circuit. The airport a city means to an airline.
DESTINATION_AIRPORTS: dict[str, str] = {
    "Nice": "LFMN", "Lyon": "LFLL", "Toulouse": "LFBO", "Marseille": "LFML",
    "Barcelona": "LEBL", "Madrid": "LEMD", "Lisbon": "LPPT", "Rome": "LIRF",
    "Milan": "LIMC", "Naples": "LIRN", "Munich": "EDDM", "Berlin": "EDDB",
    "Hamburg": "EDDH", "Vienna": "LOWW", "Zurich": "LSZH", "Geneva": "LSGG",
    "Brussels": "EBBR", "Amsterdam": "EHAM", "London Heathrow": "EGLL",
    "Manchester": "EGCC", "Dublin": "EIDW", "Copenhagen": "EKCH",
    "Stockholm": "ESSA", "Oslo": "ENGM", "Helsinki": "EFHK",
    "Warsaw": "EPWA", "Prague": "LKPR", "Budapest": "LHBP", "Athens": "LGAV",
    "Istanbul": "LTFM", "Casablanca": "GMMN", "Algiers": "DAAG",
    "Tunis": "DTTA", "New York": "KJFK", "Chicago": "KORD", "Atlanta": "KATL",
    "Dallas": "KDFW", "Denver": "KDEN", "Los Angeles": "KLAX",
    "Miami": "KMIA", "Toronto": "CYYZ", "Montreal": "CYUL", "Dubai": "OMDB",
    "Doha": "OTHH", "Singapore": "WSSS", "Tokyo": "RJTT",
    "São Paulo": "SBGR", "Buenos Aires": "SAEZ", "Mexico City": "MMMX",
}


__all__ = ["TrafficInjector", "Flight", "Call", "UserAircraft", "make_of",
           "article", "GLIDE_FT_PER_NM", "approach_speed",
           "landing_traffic", "runway_traffic", "CONCURRENT_BY_RANK"]
