"""Flight session state.

What the controller remembers about this aircraft: who it is, what it has been
cleared to do, what it still owes a readback for, and where it is in the flight.
Keeping this explicit is what lets a handoff work -- the next controller knows
the aircraft was cleared to twelve thousand because the last one wrote it down,
not because a model happened to recall it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..navdata.db import Airport, Station
from .phraseology import Aircraft
from .profile import DEFAULT as DEFAULT_PROFILE
from .profile import FlightProfile


class Phase(str, Enum):
    """Where the flight has got to."""

    PREFLIGHT = "preflight"
    CLEARED = "cleared"               # IFR clearance received, still at the gate
    PUSHBACK = "pushback"
    TAXI_OUT = "taxi_out"
    HOLDING_SHORT = "holding_short"
    LINED_UP = "lined_up"
    TAKEOFF = "takeoff"
    DEPARTURE = "departure"           # with departure control
    ENROUTE = "enroute"
    DESCENT = "descent"
    APPROACH = "approach"
    LANDING = "landing"
    LANDED = "landed"
    TAXI_IN = "taxi_in"
    PARKED = "parked"

    @property
    def on_ground(self) -> bool:
        return self in (
            Phase.PREFLIGHT, Phase.CLEARED, Phase.PUSHBACK, Phase.TAXI_OUT,
            Phase.HOLDING_SHORT, Phase.LINED_UP, Phase.LANDED, Phase.TAXI_IN,
            Phase.PARKED,
        )

    @property
    def outbound(self) -> bool:
        return self in (
            Phase.PREFLIGHT, Phase.CLEARED, Phase.PUSHBACK, Phase.TAXI_OUT,
            Phase.HOLDING_SHORT, Phase.LINED_UP, Phase.TAKEOFF, Phase.DEPARTURE,
        )


# How long the controller waits for a readback before asking for one, and how
# long it leaves between repeats. A readback is not optional, so an instruction
# that is never read back is chased rather than quietly forgotten.
#
# The clock does not start when the instruction is composed. It starts when the
# transmission has finished playing, because until then the pilot has not heard
# it -- and then it has to allow for a person finding the transmit button,
# thinking, speaking a whole clearance back, and the recogniser working through
# it. Twelve seconds covered none of that and produced a controller that
# interrupted the readback it was asking for.
READBACK_GRACE_S = 35.0
READBACK_REPEAT_S = 40.0

# After this many unanswered prompts the whole instruction is read again. A
# pilot who has not read back twice has usually not heard it, and asking a
# third time for a readback of something they never received helps nobody.
PROMPTS_BEFORE_REISSUE = 2

# How many wrong readbacks are corrected item by item before the controller
# gives up on the item and re-reads the instruction in full. It never gives up
# on the readback itself: an instruction that has not been read back correctly
# is not an instruction the aircraft is operating under.
ATTEMPTS_BEFORE_REISSUE = 3


@dataclass
class Instruction:
    """Something the controller told the aircraft to do.

    Held until the pilot reads it back, so the readback can be checked item by
    item rather than merely acknowledged.
    """

    kind: str
    station_key: str
    text: str = ""
    values: dict[str, Any] = field(default_factory=dict)
    # Through a lambda, so a test that moves the clock moves this with it:
    # the bare function is bound when the class is made.
    issued_at: float = field(default_factory=lambda: time.monotonic())
    readback_required: bool = True
    read_back: bool = False
    # The language it was said in, so a repeat in another one is rebuilt
    # rather than wrapped round it (see atc.restate). Stamped by remember().
    language: str = ""
    attempts: int = 0
    # How many times the controller has had to ask for the readback, and when
    # it last did.
    prompts: int = 0
    prompted_at: float = 0.0
    # What the pilot asked for that produced this. A pilot who asks the same
    # thing again has not heard the answer, and gets the answer rather than a
    # demand for a readback of it.
    from_intent: str = ""
    # What is still wrong, once the controller has challenged something. A
    # pilot answering "hold short of runway zero four left" has answered the
    # question they were asked, and judging that reply as though it were a
    # readback of the whole clearance again is how an exchange becomes a loop
    # with no way out of it.
    outstanding: set[str] | None = None
    # Which of ``values`` the pilot must read back. ``None`` means the standard
    # set; an instruction can narrow it where the standard set would be
    # pedantic, such as a full clearance where omitting the departure frequency
    # is normal and a controller would not challenge it.
    required_items: set[str] | None = None
    # Items the controller has asked the pilot to confirm, rather than
    # corrected: "affirm" settles them.
    confirming: set[str] | None = None

    @property
    def age_s(self) -> float:
        return time.monotonic() - self.issued_at

    @property
    def since_prompt_s(self) -> float:
        """How long since the controller last asked for this readback."""
        return time.monotonic() - (self.prompted_at or self.issued_at)

    @property
    def overdue(self) -> bool:
        """Whether the controller should chase this readback now."""
        if not self.readback_required or self.read_back:
            return False
        wait = READBACK_GRACE_S if not self.prompts else READBACK_REPEAT_S
        return self.since_prompt_s >= wait

    def prompted(self) -> None:
        self.prompts += 1
        self.prompted_at = time.monotonic()

    def hold_off(self) -> None:
        """Restart the clock, because the pilot could not have answered yet.

        Called while the frequency is busy or the pilot has the button down.
        Without it the wait ran through the controller's own transmission and
        through the pilot's reply, and the first prompt landed on top of the
        readback it was asking for.
        """
        now = time.monotonic()
        if self.prompted_at:
            self.prompted_at = now
        else:
            self.issued_at = now

    @property
    def owed_items(self) -> list[str]:
        """The items the pilot still has to say back, in the issued order."""
        required = self.required_items or REQUIRED_READBACK
        return [key for key in self.values if key in required]

    def mismatches(self, heard: dict[str, Any]) -> list[str]:
        """Which required items the pilot got wrong or left out.

        Only items the controller actually issued are checked, and only those
        a pilot is required to read back: altitude, heading, runway assignment,
        hold-short instructions, squawk and frequency. Reading back a wind
        check is not required and its absence is not an error.
        """
        required = self.required_items or REQUIRED_READBACK
        if self.outstanding is not None:
            required = required & self.outstanding
        wrong: list[str] = []
        for key, expected in self.values.items():
            if key not in required:
                continue
            got = heard.get(key)
            if got is None:
                wrong.append(key)
            elif not _matches(key, expected, got):
                wrong.append(key)
        return wrong


# Items FAA AIM 4-4-7 and ICAO Doc 4444 4.5.7.5 require the pilot to read back.
REQUIRED_READBACK = {
    "altitude_ft", "heading", "runway", "squawk", "frequency",
    "hold_short", "speed_kt",
}


# The readback items whose value a recogniser can get one figure wrong:
# heard that close with the recogniser unsure, they are asked about, not
# refused (:func:`nearly_matches`).
NEARLY_HEARD = ("frequency", "squawk", "altitude_ft", "runway")


def _figures(key: str, value: Any) -> str:
    """A value as the figures that were said: 135.900 is "135900", FL050
    and 5,000 ft are "50", runway 27L is "27L"."""
    if key == "frequency":
        return f"{float(value):.3f}".replace(".", "")
    if key == "squawk":
        return str(value).strip().zfill(4)
    if key == "altitude_ft":
        return str(int(round(float(value) / 100.0)))
    from ..navdata.db import normalize_runway

    return normalize_runway(str(value))


def _edits(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def nearly_matches(key: str, expected: Any, got: Any) -> bool:
    """A value read back one figure off what was given -- one changed,
    dropped or added: "five" heard for "nine", 27R for 27L, 135.95 for
    135.90. What a recogniser does to a figure it was unsure of, and what a
    pilot who misheard does too; only the recogniser's confidence tells
    which."""
    if key not in NEARLY_HEARD or got is None:
        return False
    try:
        return _edits(_figures(key, expected), _figures(key, got)) == 1
    except (TypeError, ValueError):
        return False


def _matches(key: str, expected: Any, got: Any) -> bool:
    if key == "frequency":
        return abs(float(expected) - float(got)) < 0.006
    if key == "runway":
        from ..navdata.db import normalize_runway

        return normalize_runway(str(expected)) == normalize_runway(str(got))
    if key == "hold_short":
        # A hold-short readback that names a runway has to name the right one.
        # One that names none is a pilot the recogniser dropped a word from
        # rather than a pilot who is about to cross the wrong runway, and is
        # accepted -- the restriction itself was read back.
        from ..navdata.db import normalize_runway

        if isinstance(expected, str) and isinstance(got, str):
            return normalize_runway(expected) == normalize_runway(got)
        return bool(expected) == bool(got)
    if key in ("altitude_ft", "heading", "speed_kt"):
        return int(expected) == int(got)
    if key == "altimeter":
        # Hectopascals to the unit, inches to the hundredth.
        tolerance = 0.006 if float(expected) < 100.0 else 0.5
        return abs(float(expected) - float(got)) <= tolerance
    return str(expected).strip().upper() == str(got).strip().upper()


@dataclass
class FlightSession:
    """Everything the controllers know about this flight."""

    aircraft: Aircraft
    departure: Airport | None = None
    destination: Airport | None = None
    destination_input: str = ""

    # --- clearance ---
    cruise_altitude_ft: float = 0.0
    initial_altitude_ft: float = 5000.0
    # The SID the clearance named, as the chart prints it ("ROUSY 1L"), or
    # "" for radar vectors; and whether the tower sent the flight off on
    # runway heading, which departure then turns on course with a direct.
    sid: str = ""
    runway_heading: bool = False
    # When a centre first saw the flight near the level it gave, for the
    # next step of the climb (atc/climb.py), and which station that was.
    level_reached_at: float = 0.0
    level_reached_with: str = ""
    # The en route (atc/routing.py): a fix the flight was sent direct to,
    # until it is passed, so the legs it cuts are not "off route"; a weather
    # deviation, "approved" and then "away" once the flight has left the
    # route, until it is back on it; since when the flight has been off its
    # route, and whether it was asked about it; when the last shortcut was
    # given.
    route_direct: str = ""
    # The approach to expect, chosen once (ControllerBrain.approach_choice)
    # and the (field, runway, request) it was chosen for.
    approach_choice: object = None
    approach_key: tuple = ()
    # The STAR the flight was told to "descend via" (FAA), and the fix where
    # its STAR joins the final when it was cleared along it (Phase 5).
    descend_via: str = ""
    star_join: str = ""
    # Radar vectors (atc/vectors.py): the leg last given and its side, when,
    # whether the pilot reported the field in sight (a visual is cleared
    # only then), and whether the flight joined the final by itself.
    vector_leg: str = ""
    vector_side: float = 0.0
    vector_at: float = 0.0
    field_in_sight: bool = False
    joined_final: bool = False
    # Speed control (Phase 5 (c)): "pattern" or "final" when one was given,
    # and whether it has been lifted since.
    speed_given: str = ""
    speed_lifted: bool = False
    # The hold the flight is in (atc/holds.py Hold), and whether the pilot
    # asked for one (Phase 6).
    hold: object = None
    hold_requested: bool = False
    deviating: str = ""
    off_route_since: float = 0.0
    off_route_asked: bool = False
    shortcut_at: float = 0.0
    assigned_altitude_ft: float = 0.0
    assigned_heading: float | None = None
    assigned_speed_kt: float | None = None
    squawk: str = "1200"
    # The last altimeter setting a controller gave, as given (hectopascals,
    # or inches of mercury); 0 for none. The first officer checks the
    # altimeter against it.
    altimeter: float = 0.0
    # The last clearance as the controller said it, for "read me the
    # clearance" (the first officer).
    last_clearance: str = ""
    # The rules this flight is flying under. They start as ``flight.ifr``
    # and change only for an explicit request on the radio (a VFR release,
    # "IFR" said to any station, an IFR cancellation) -- never as a side
    # effect of asking for circuits or a service.
    ifr: bool = True
    # ``flight.ifr`` as it was when the session was built, so a session
    # rebuilt for a new callsign keeps what the radio changed, and one
    # rebuilt because the pilot changed the rules on the screen does not.
    ifr_setting: bool = True
    # What kind of flying this is and which region's words, as the pilot
    # said it (atc.profile). Everything that differs between an airline and
    # a light aeroplane reads this rather than guessing from the type.
    profile: FlightProfile = DEFAULT_PROFILE
    clearance_issued: bool = False
    clearance_read_back: bool = False

    # --- VFR ---
    # A VFR flight is worked differently from the first call to the last: it is
    # not cleared to a destination, it is not given a level to maintain, and at
    # a tower it is sequenced into the circuit rather than vectored onto an
    # instrument approach. What that needs recording is which way round the
    # circuit goes, where the aircraft is in it, and whether a radar position
    # has taken the flight on for advisories.
    pattern_direction: str = "left"
    # Whether the pilot named the side themselves ("left traffic"), which
    # wins over the published side for the rest of the circuit.
    pattern_direction_said: bool = False
    pattern_leg: str = ""
    # Staying in the circuit: touch-and-goes, the option, closed traffic.
    pattern_work: bool = False
    # Radar advisories, which a VFR flight has to ask for and can be refused.
    flight_following: bool = False
    # A level a VFR flight has been held at or below, e.g. under a shelf.
    vfr_ceiling_ft: float = 0.0
    # The radar position that has said "radar contact" to this VFR flight
    # (its station key), until that service is ended. Flight following or
    # not: a departure handed over by the tower is being watched too.
    radar_service: str = ""
    # When the radar controller asked this flight its altitude for being
    # above vfr_ceiling_ft, while it still is (atc.watch.RestrictionWatch).
    restriction_asked_at: float = 0.0
    # Cleared through somebody's airspace on the way past.
    transition_approved: bool = False
    # The circuit report the tower asked for ("downwind", "base", "final"),
    # and whether it has already been chased once for not coming.
    expected_report: str = ""
    expected_report_chased: bool = False
    # "Extend downwind, I'll call your base": the base is owed, behind the
    # aeroplane written here.
    base_owed_behind: str = ""
    # The reporting point an arrival was asked to report, by name, while it
    # has not reported it.
    expected_vrp: str = ""
    # The field whose Class B the flight has been cleared into, which two-way
    # contact alone does not give (navdata airspace rules).
    cleared_bravo: str = ""
    # And the one it has reported, so it is not asked for it twice.
    vrp_reported: str = ""
    # The traffic this flight has been told about, by callsign: "called",
    # "seen", "looking", "negative", or "done" once it was said to be no
    # factor (atc.trafficinfo).
    traffic_called: dict = field(default_factory=dict)
    # A VFR flight plan opened with flight service and not yet closed.
    flight_plan_open: bool = False
    # Given Special VFR into or out of the zone (atc.svfr), so the
    # below-minima refusal does not apply to it again.
    special_vfr: bool = False
    # The runway change this flight has been told about, "LFQQ:26", so the
    # tower says it once (watch.RunwayWatch).
    runway_change_heard: str = ""
    # The runway was occupied when the landing clearance was due: "continue
    # approach", and the clearance -- or the go-around -- is the tower's to
    # give when it can see which (arrival.ArrivalWatch).
    landing_withheld: bool = False
    # Cleared to land with a departure still on the runway (the FAA's
    # anticipated separation), by the departure's callsign.
    landing_anticipated: str = ""
    # Held for an arrival on final: lined up behind it, or (in the US) held
    # short of the runway until it has landed, by its callsign.
    held_for_landing: str = ""
    # An orbit for spacing in progress: which way, how far round the track
    # has turned so far, and the last track seen (-1 before the first).
    orbiting: str = ""
    orbit_turned: float = 0.0
    orbit_track: float = -1.0
    # The reporting point an arrival is holding at while the circuit is full.
    holding_at: str = ""
    # When the takeoff clearance was given (time.monotonic), for "expedite".
    cleared_takeoff_at: float = 0.0
    # The point a VFR departure was released by and has not yet reported.
    expected_exit: str = ""
    # The station whose busy frequency let this flight's first call go
    # unanswered; the next call there is answered.
    first_call_missed: str = ""
    # The information services that have given this flight the field --
    # runway, wind, QNH, traffic -- once, keyed by station. After that they
    # say what is new, until the field is asked for again.
    field_given_by: set = field(default_factory=set)

    # --- airport operations ---
    departure_runway: str = ""
    arrival_runway: str = ""
    taxi_route: list[str] = field(default_factory=list)
    hold_short_of: str = ""
    # The same route on the simulator's own taxiways, where it came from
    # there (:mod:`wilcoatc.atc.taxi`): the graph nodes, the line the map
    # draws as (latitude, longitude), every runway it crosses in order, and
    # the stand number of a taxi in. All empty for an invented route.
    taxi_nodes: list = field(default_factory=list)
    taxi_path: list[tuple[float, float]] = field(default_factory=list)
    taxi_crossings: list[str] = field(default_factory=list)
    # A long taxi cut in two (atc.taxi.split_route): the taxiways still to
    # be given, where (lat, lon) the aeroplane was told to hold short for
    # them, and whether it is a taxi in.
    taxi_rest: list[str] = field(default_factory=list)
    taxi_split_at: tuple | None = None
    taxi_rest_inbound: bool = False
    taxi_stand: int | None = None
    cleared_takeoff: bool = False
    cleared_landing: bool = False
    # Sent round, and not yet back on the final. No landing clearance is
    # issued while this is set: it is cleared when the aeroplane is in the
    # cone again and descending, or when it lands.
    going_around: bool = False
    # Cleared for the option: the touch-and-go, the low approach or the full
    # stop. Not a landing clearance, and not a takeoff clearance either, but
    # it covers both halves of a touch-and-go.
    cleared_option: bool = False
    # The last touchdown, so a liftoff straight after it can be told from a
    # takeoff: when, on which runway, whether it was cleared, and the slowest
    # the rollout got. ``rollout_liftoff_at`` is set while an aeroplane that
    # got airborne again off that rollout has not yet been classified.
    touchdown_at: float = 0.0
    touchdown_runway: str = ""
    touchdown_cleared: bool = False
    rollout_min_kt: float = 0.0
    rollout_liftoff_at: float = 0.0
    # The touchdown already judged a full stop, so it is judged once.
    full_stop_judged_at: float = 0.0
    # What the tower watches to tell a go-around from a bump: the lowest
    # altitude seen on this final, and when the current climb began.
    final_low_ft: float = 0.0
    climb_since: float = 0.0
    # The approach the pilot asked for ("request RNAV 25L"), given whenever
    # the runway has it.
    requested_approach: str = ""
    # Whether the landing that has just happened was a cleared one. The
    # clearance itself is spent on touchdown -- it covers one landing -- so
    # the fact of it has to be kept separately, and it is what decides
    # whether the tower welcomes the aeroplane or has already had words with
    # it about arriving uninvited.
    landed_cleared: bool = False
    atis_letter: str = ""
    atis_acknowledged: str = ""
    # The runway the aircraft is currently allowed to be on, and the one it
    # actually is on. A runway is the one piece of an airport where being
    # somewhere without a clearance is an event in its own right, so the two
    # are tracked separately and compared rather than assumed to agree.
    # Held on the runway waiting for it to clear. Separate from being cleared
    # for takeoff, because an aircraft lined up has not been cleared to go.
    lined_up: bool = False
    cleared_runway: str = ""
    occupying_runway: str = ""
    # Advisories already given. A controller says "you departed without a
    # clearance" once, not once per poll of the simulator.
    advisories: set[str] = field(default_factory=set)

    # An emergency or an urgency the pilot has declared, which every position
    # answers and which outranks whatever else was being discussed.
    emergency: str = ""
    # What the pilot said when asked: souls on board, fuel endurance.
    emergency_details: dict[str, Any] = field(default_factory=dict)

    # --- conversation ---
    phase: Phase = Phase.PREFLIGHT
    current_station: Station | None = None
    pending: Instruction | None = None
    # The last instruction issued, readback or no. Kept after the readback
    # clears it so "say again" can be answered with what was actually said
    # rather than with a request to say again.
    last_instruction: Instruction | None = None
    contacted: set[str] = field(default_factory=set)
    last_transmission_at: float = 0.0
    handoff_pending: Station | None = None
    handoff_offered_at: float = 0.0
    # The HF station a flight was sent to over the ocean. It cannot be tuned
    # on COM1, so it is held here, and it is who answers until the next
    # domestic sector takes the flight back (atc/enroute.py).
    oceanic: Station | None = None
    # When the flight was handed away from each en-route station, by
    # station_key: a centre never hands a flight back to the one it has
    # just left (atc/enroute.py, Engine._handing_back).
    left_at: dict[str, float] = field(default_factory=dict)
    # When the flight came onto the en-route frequency it is on now by a
    # handoff, so that controller keeps it a few minutes (0: not handed).
    with_since: float = 0.0

    # --- history, for the transcript pane ---
    log: list[tuple[float, str, str]] = field(default_factory=list)

    # ------------------------------------------------------------------

    def established_with(self, station: Station | None) -> bool:
        """Whether two-way contact exists, which allows the abbreviated callsign."""
        return bool(station) and station_key(station) in self.contacted

    @property
    def visual(self) -> bool:
        """Flown by sight in the circuit: a VFR flight, or circuits asked
        for under any rules.

        Asking for touch-and-goes used to make the flight VFR, which is a
        change of rules nobody asked for and which stayed after the last
        circuit. The circuit is worked the same either way, so it is this
        that the circuit's gates ask, and the rules are left alone.
        """
        return not self.ifr or self.pattern_work

    def establish(self, station: Station | None) -> None:
        if station:
            self.contacted.add(station_key(station))

    def remember(self, instruction: Instruction) -> Instruction:
        """Hold an instruction, and owe a readback for it if one is due.

        Only an instruction that actually requires a readback becomes pending.
        An advisory -- "continue approach", "expect the ILS" -- is information
        rather than a clearance, so it never becomes one, and it never clears a
        readback that is still owed either: the pilot who has not read back a
        hold-short instruction still has not read it back.
        """
        if not instruction.language:
            instruction.language = getattr(self.aircraft, "language", "") or "en"
        self.last_instruction = instruction
        if instruction.kind in ("ifr_clearance", "vfr_release") \
                and instruction.text:
            self.last_clearance = instruction.text
        if instruction.readback_required:
            self.pending = instruction
        return instruction

    def clear_pending(self) -> None:
        self.pending = None

    def note(self, who: str, text: str) -> None:
        self.log.append((time.time(), who, text))
        if len(self.log) > 500:
            del self.log[:100]

    def apply(self, values: dict[str, Any]) -> None:
        """Record what an accepted instruction changed."""
        if "altitude_ft" in values:
            self.assigned_altitude_ft = float(values["altitude_ft"])
        if "vfr_ceiling_ft" in values:
            self.vfr_ceiling_ft = float(values["vfr_ceiling_ft"])
        if "pattern_direction" in values:
            self.pattern_direction = str(values["pattern_direction"])
        if "heading" in values:
            self.assigned_heading = float(values["heading"])
        if "speed_kt" in values:
            self.assigned_speed_kt = float(values["speed_kt"])
        if "squawk" in values:
            self.squawk = str(values["squawk"])
        if values.get("altimeter"):
            self.altimeter = float(values["altimeter"])
        if "runway" in values:
            if self.phase.outbound:
                self.departure_runway = str(values["runway"])
            else:
                self.arrival_runway = str(values["runway"])

    def allow_runway(self, runway: str) -> None:
        """Record that the aircraft may now occupy a runway.

        Set by every clearance that puts an aircraft on the pavement -- take
        off, land, line up, cross -- and read by the surface watch, which
        otherwise reads a runway entry as an incursion.
        """
        self.cleared_runway = str(runway or "")

    def advise_once(self, key: str) -> bool:
        """True the first time an advisory is due, False every time after."""
        if key in self.advisories:
            return False
        self.advisories.add(key)
        return True


def station_key(station: Station | None) -> str:
    """Stable identity for a controller position."""
    if station is None:
        return ""
    return f"{station.ident}:{station.position}:{station.mhz:.3f}"
