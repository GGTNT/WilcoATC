"""Controller logic: what each position says in reply to the pilot.

This is the decision layer. It never composes words -- every reply comes from
:mod:`wilcoatc.atc.phraseology` -- it only decides *which* instruction is
appropriate given the position being talked to, the phase of flight, and what
the pilot just said.

Readbacks are checked item by item. A pilot who reads twelve thousand back as
two thousand gets corrected, which is both realistic and the main safety value
of a readback in the first place.

Two limits are worth stating plainly, because both come from data that no open
dataset publishes:

* **Taxi routes come from the simulator where it is there.** There is no free
  worldwide taxiway graph, but the simulator describes every airport it draws,
  and :mod:`wilcoatc.atc.taxi` plans the pilot's taxi on that graph -- the
  same one the AI traffic drives on -- with the hold-short taken from the
  runways the route actually crosses. Without it, or where the graph has no
  path, the route is :func:`taxi_route`: a deterministic, plausible sequence
  of taxiway identifiers that does not match the airport diagram, with a
  hold-short picked from the other runways. The phraseology is correct
  either way, and ``logs radio`` says which one was given and why.

* **SIDs and STARs are not used.** Departure and arrival procedure names are
  likewise unpublished in open data, so clearances are issued with radar
  vectors instead. That is real phraseology and is what smaller fields use
  anyway; it simply is not what you would get at a major hub.
"""

from __future__ import annotations

import time

import hashlib
import logging
import math
import re
from dataclasses import dataclass
from typing import Any

from ..navdata import terrain
from ..navdata.db import (DEFAULT_TRANSITION_FT, NavDB, Station,
                          normalize_runway, transition_altitude,
                          bearing_deg, haversine_nm, transition_level,
                          vfr_region)
from ..logs import decision
from ..navdata.channels import same_channel
from . import speech as sp
from .taxi import say_words
from .approaches import (VISUAL, Approach, Choice, FieldApproaches, asked_for,
                         choose, legacy_kind, plan as plan_approach)
from .enroute import is_oceanic
from .circuit import agrees, choose_join, circuit_for, runway_length_nm
from .climb import (CLIMBING_FPM, DEPARTURE_REACHING_FT, LAST_STEP_FT,
                    centre_step, departure_step, departure_top,
                    initial_altitude)
from .procedures import heard as procedure_heard
from .procedures import near as procedure_near
from .procedures import on_course_fix, sid_first_fix
from .procedures import written as written_procedure
from .final import final_for
from .intents import _REPORT_WORD, Intent, ParsedIntent
from .phraseology import Phraseology, Weather, _join, language_of
from .session import (ATTEMPTS_BEFORE_REISSUE, PROMPTS_BEFORE_REISSUE,
                      FlightSession, Instruction, Phase, nearly_matches,
                      station_key)

log = logging.getLogger(__name__)


@dataclass
class Reply:
    """One controller transmission to be spoken."""

    text: str
    station: Station
    instruction: Instruction | None = None
    handoff_to: Station | None = None
    silent: bool = False           # nothing to say; do not key the transmitter
    # A ground-handling action this reply authorises: "pushback", or nothing.
    # Named here rather than inferred by the caller from the words, because
    # the words are phraseology in six languages and the tug is a switch.
    ground: str = ""

    @property
    def speak(self) -> bool:
        return bool(self.text) and not self.silent


# A transmission asking for a runway rather than reading one back. Only
# consulted where nothing is pending, so it cannot mistake a readback for a
# request; what it separates is "request runway three zero" from a pilot
# repeating a runway nobody gave them.
_ASKS_FOR_RUNWAY = re.compile(
    r"\b(request(ing)?|like|want|prefer|need|ready for)\b[^.]{0,24}\brunway\b")


# The side and the size of a weather deviation, in every pilot language.
_SIDES = (("left", r"\b(left|gauche|izquierda|links|sinistra|esquerda)\b"),
          ("right", r"\b(right|droite|derecha|rechts|destra|direita)\b"))
_DEGREE_WORDS = r"(degrees?|degres?|grados?|grad|gradi|graus?)"
# English and French tens, which numbers.parse_number does not read.
_TENS = {"five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30,
         "forty": 40, "fifty": 50, "sixty": 60, "cinq": 5, "dix": 10,
         "quinze": 15, "vingt": 20, "trente": 30, "quarante": 40,
         "cinquante": 50, "soixante": 60}


def _side_said(text: str) -> str:
    for side, pattern in _SIDES:
        if re.search(pattern, text):
            return side
    return ""


def _degrees_said(text: str, language: str = "en") -> int:
    """The number in front of "degrees", in figures or words, or 0."""
    from .numbers import parse_number

    found = re.search(r"\b([\w-]+(?: [\w-]+)?) " + _DEGREE_WORDS + r"\b", text)
    if not found:
        return 0
    words = found.group(1).split()
    for candidate in (words[-1:], words):
        phrase = " ".join(candidate)
        if phrase.isdigit():
            return int(phrase)
        if phrase in _TENS:
            return _TENS[phrase]
        value = parse_number(phrase, language)
        if value:
            return int(value)
    return 0


# Taxiway identifiers used to build the invented route, for a field the
# simulator has not described. Real airports use these letters; the sequence
# is invented, the alphabet is not.
_TAXIWAYS = ("A", "B", "C", "D", "E", "F", "G", "H", "J", "K", "L", "M",
             "N", "P", "Q", "R", "S", "T", "U", "V", "W", "Y", "Z")


def _stable(*parts: str) -> int:
    return int.from_bytes(
        hashlib.sha256("|".join(parts).encode("utf-8")).digest()[:6], "big"
    )


def taxi_route(airport_ident: str, runway: str, inbound: bool = False) -> list[str]:
    """A deterministic, plausible taxi route, for where there is no real one.

    Invented -- see the module docstring. It is what the pilot is given when
    the simulator has not described the airport, or its taxiways have no path
    from where the aeroplane is (:mod:`wilcoatc.atc.taxi` plans the real one).
    Determinism is what matters here:
    the same airport and runway always produce the same route, so a pilot who
    flies from one field repeatedly learns "their" taxi route and a readback
    can be checked against it.
    """
    seed = _stable(airport_ident, normalize_runway(runway), "in" if inbound else "out")
    count = 2 + (seed % 2)
    route: list[str] = []
    for i in range(count):
        letter = _TAXIWAYS[(seed >> (8 * (i + 1))) % len(_TAXIWAYS)]
        if letter not in route:
            route.append(letter)
    return route or ["A"]


# The words a pilot reads a way out of the zone back with, in the
# languages the controller works.
_EXIT_WORDS = r"(?:via|by|par|uber|über|por|tramite|pelo|pela)"


def squawk_for(session: FlightSession, airport_ident: str) -> str:
    """Assign a discrete beacon code.

    Real codes come from a block allocated to the facility. This picks a stable
    pseudo-random code in the ordinary discrete range, avoiding the reserved
    ones: 1200 (US VFR), 7000 (European VFR), 7500 hijack, 7600 radio failure,
    7700 emergency, and anything ending in 00 which is normally a block header.
    """
    seed = _stable(session.aircraft.callsign.written, airport_ident)
    reserved = {"1200", "7000", "7500", "7600", "7700", "2000", "0000"}
    for attempt in range(64):
        digits = []
        value = seed + attempt * 7919
        for _ in range(4):
            digits.append(str(value % 8))
            value //= 8
        code = "".join(reversed(digits))
        if code in reserved or code.endswith("00") or code.startswith("0"):
            continue
        return code
    return "4321"


class ControllerBrain:
    """Chooses the correct instruction for a situation."""

    def __init__(self, navdb: NavDB, phraseology: Phraseology | None = None,
                 strict_icao_digits: bool = False,
                 ground_control: bool = True):
        self.navdb = navdb
        # ``atc.ground_control``. The brain is where it is decided because
        # every handoff, every unprompted call and every answer already asks
        # the brain who is working a position.
        self.ground_control = ground_control
        # What the simulator says each airport publishes, by ident, filled in
        # by the engine as the answers arrive. Empty means nothing is known,
        # and nothing known is the ILS (see :mod:`wilcoatc.atc.approaches`).
        self.approach_data: dict[str, FieldApproaches] = {}
        # Each runway end's pattern height above the field, as the
        # simulator's facility data gives it (integrations.runways), by
        # field. Behind the circuit files, ahead of the default.
        self.sim_patterns: dict[str, dict[str, int]] = {}
        # The flight plan's runways and SID, by airport: {"KJFK":
        # {"departing": "31L", "sid": "SKORR5"}, "KBOS": {"landing": "04R"}}.
        # Used whenever the wind allows them, never against it.
        self.plan_runways: dict[str, dict[str, str]] = {}
        # The flight plan's fixes in order (integrations.simbrief.PlanFix):
        # what a centre grants a direct to. Empty without a plan.
        self.plan_fixes: list = []
        # The filed route (atc/route.py), when a plan has one: the minutes
        # to the cruise, the SID's fixes, where a departure is sent direct.
        self.route = None
        # The arrival watch (arrival.py), set by the engine: a requested hold
        # or descent is answered with what it would have said unprompted.
        self.arrivals = None
        # Where the aeroplane is along it (route.Progress), kept by the
        # engine: what the descent is measured on, the miles still to fly.
        self.route_progress = None
        self.phrase = phraseology or Phraseology("faa")
        self.strict_icao_digits = (
            strict_icao_digits or getattr(self.phrase, "strict_icao_digits", False)
        )
        # Phraseology objects are cheap but not free; one per language,
        # facility and region is plenty and keeps them out of the hot path.
        self._phraseology: dict[tuple[str, str, str], object] = {}
        # The pilot's profile (atc.profile): its pinned region is every
        # controller's. The engine hands it in with each session.
        from .profile import DEFAULT as DEFAULT_PROFILE

        self.profile = DEFAULT_PROFILE
        # ``immersion.controller``: which of the realism behaviours are on.
        # The engine hands in the configured one; on its own, all of them.
        from ..config import ControllerRealismConfig, SpeechConfig

        self.realism = ControllerRealismConfig()
        # The recogniser's settings (config ``speech``), handed in by the
        # engine: ``confirm_below`` is where a readback heard nearly is
        # asked about rather than refused.
        self.hearing = SpeechConfig()
        # The runway in use at each field, landing and departing, kept until
        # the wind takes it away (``_wind_runway``); and the last change at
        # each, ``(was, now)``, for the tower to announce (watch.RunwayWatch).
        self.runway_in_use: dict[tuple[str, bool], str] = {}
        self.runway_changes: dict[str, tuple[str, str]] = {}

    def controller_at(self, ident: str, position: str,
                      runway: str = "") -> Station | None:
        """The controller working a position, or ``None`` if nobody is.

        Not the same question as :meth:`NavDB.station_for`, which falls back
        down the chain to the traffic frequency so that a pilot tuning it at an
        uncontrolled field is talking to the right place. That fallback must
        not reach a handoff: an aerodrome traffic frequency has nobody on it,
        and telling a pilot to contact one is telling them to talk to an empty
        room. Where this returns nothing, the position that was asked keeps the
        aircraft and deals with the request itself.

        ``runway`` picks between the frequencies of a split position, where
        their descriptions say which runways each works.
        """
        found = (self.navdb.station_for_runway(ident, position, runway)
                 if runway else None) or self.navdb.station_for(ident, position)
        if not self.staffed(found) or not found.mhz:
            return None
        return found

    def closed(self, station: Station | None) -> bool:
        """A published controller position this program has been told not to work."""
        return (station is not None and station.position == "GND"
                and not self.ground_control)

    def staffed(self, station: Station | None) -> bool:
        """Whether anybody answers on this station.

        Not :attr:`Station.is_controller` on its own: that says what the
        frequency is, and a Ground frequency is still a controller's frequency
        when Ground is switched off. Everything that speaks unprompted asks
        this instead, so a closed position is as quiet as a traffic frequency.
        """
        if station is None:
            return False
        # Inside its hours (afis.is_open, from service_hours.csv; with none
        # known, always). An information service is somebody on the
        # frequency too, and a field's own one only while its tower is shut.
        from . import afis

        if station.position == "INFO":
            return afis.is_open(self.navdb, station)
        return (station.is_controller and not self.closed(station)
                and afis.is_open(self.navdb, station))

    def issues_clearances(self, station: Station | None) -> bool:
        """Whether this station clears anybody to do anything: staffed, and
        not an information service. An AFIS gives the runway "at your
        discretion" and a FIS gives information; neither owes a clearance, so
        nothing that watches for a missing one may run on either."""
        return (self.staffed(station) and station is not None
                and station.position != "INFO")

    def for_station(self, station: Station, language: str = "en"):
        """Phraseology for a station, in the language being spoken.

        Two independent axes meet here. The *language* is whatever the pilot
        used; the *dialect* is where the facility is, so English at Orly is
        ICAO English rather than US English -- unless the pilot pinned a
        region, which is then every facility's.
        """
        from .language import phraseology_for

        language = (language or "en").lower()[:2]
        ident = station.ident if station else ""
        region = self.profile.region
        cached = self._phraseology.get((language, ident, region))
        if cached is None:
            cached = phraseology_for(language, ident, self.strict_icao_digits,
                                     region=region)
            self._phraseology[(language, ident, region)] = cached
        return cached

    def region_for(self, ident: str) -> str:
        """The VFR wording's region at a field, pinned or its own."""
        return self.profile.region_for(ident)

    # ------------------------------------------------------------------
    # entry point
    # ------------------------------------------------------------------

    def respond(
        self,
        parsed: ParsedIntent,
        session: FlightSession,
        station: Station,
        weather: Weather,
        state=None,
    ) -> Reply:
        """Produce the controller's reply to one pilot transmission."""
        language = getattr(parsed, "language", "en")
        p = self.for_station(station, language)
        aircraft = session.aircraft
        # The callsign is spoken differently in each language, so the aircraft
        # is told which one this exchange is in before any template runs.
        aircraft.language = language_of(p)
        self._hear_procedure(parsed, session)
        self._current_runway_heard(parsed, session, station)

        # An aerodrome traffic frequency has nobody on it, so nothing is said
        # on it -- including "say again". Answering only the transmissions it
        # failed to parse was the worst of both: silence when the pilot got it
        # right, and a voice when they did not.
        if not self.staffed(station):
            return Reply("", station, silent=True)

        # With Ground switched off nobody approves a pushback, and nobody
        # redirects it either: "contact Tower" for a pushback is the tower
        # doing Ground's job by another name. The request goes unanswered on
        # every frequency.
        if not self.ground_control and parsed.intent is Intent.REQUEST_PUSHBACK:
            return Reply("", station, silent=True)

        # A distress call is answered by whoever hears it, before anything
        # else is considered -- including an outstanding readback, and
        # including whether the request makes sense for the position. It used
        # to fall through every handler to the not-understood reply.
        if parsed.intent in (Intent.EMERGENCY, Intent.URGENCY,
                             Intent.MINIMUM_FUEL):
            session.establish(station)
            return self._emergency(parsed, session, station, weather, p, state)

        # The answer to the controller's questions after a MAYDAY or PAN.
        # Kept, and answered with the priority and the runway -- it used to
        # fall through to the list of calls the controller was "expecting".
        if parsed.intent is Intent.EMERGENCY_DETAILS:
            session.establish(station)
            session.emergency_details.update({
                key: parsed.values[key] for key in ("souls", "fuel_minutes")
                if key in parsed.values})
            if not session.emergency:
                return Reply(p.roger(aircraft), station)
            runway = self._emergency_runway(session, station, weather, state)
            return Reply(p.emergency_priority(aircraft, runway), station)

        # The reporting point an arrival was asked to report, named on its own
        # ("Whiskey"), is that report; no rule table knows a field's points.
        if (not parsed.understood and session.expected_vrp
                and self._mentions(parsed, session.expected_vrp, p)):
            parsed.intent = Intent.POSITION_REPORT
        if (not parsed.understood and session.expected_exit
                and self._mentions(parsed, session.expected_exit, p)):
            parsed.intent = Intent.POSITION_REPORT

        # The rules the pilot says they are flying under, to any station.
        self._note_rules(session, parsed)

        # Another of this field's positions, called on this one's frequency.
        elsewhere = self._called_elsewhere(session, station, parsed, p)
        if elsewhere is not None:
            return elsewhere

        if self._checking_in_after_handoff(session, station, parsed):
            parsed.intent = Intent.CHECK_IN

        answered = self._altitude_answered(session, station, parsed, p, state)
        if answered is not None:
            return answered

        # A transmission we could not understand gets a "say again", exactly as
        # it would on a real frequency. Guessing would be worse.
        if not parsed.understood and parsed.intent is not Intent.READBACK:
            if not parsed.normalized.strip():
                return Reply("", station, silent=True)
            # A readback owed here, garbled: ask for it again by name. The
            # list of calls the controller was "expecting" is the answer to a
            # pilot who has said nothing useful, not to one reading back.
            pending = session.pending
            if (pending is not None and pending.readback_required
                    and pending.station_key == station_key(station)):
                session.establish(station)
                # Unrecognised, but carrying exactly what is owed: that is
                # the readback, whatever words came with it.
                heard = parsed.readback_items
                if heard and pending.owed_items and not pending.mismatches(heard):
                    return self._check_readback(parsed, session, station, p, state)
                return self._insist(session, station, p)
            # The callsign was heard, so somebody is on frequency and has
            # called: that is two-way contact, whatever the rest was. Without
            # it an unreadable first call ("descending flight level eight
            # zero" before it was parsed) never counted, and every arrival
            # call that waits for a check-in waited for ever.
            if parsed.callsign_matched:
                session.establish(station)
                return Reply(p.say_again(aircraft), station)
            return self._not_understood(session, station, p, state)

        # "Final, runway two four" carries a runway and no request verb, so it
        # parses as a readback -- and with nothing waiting to be read back it
        # was answered "roger" on every frequency, the tower's included.
        # With nothing pending, a call that names a leg is where the
        # aeroplane is.
        if (parsed.intent is Intent.READBACK and session.pending is None
                and parsed.get("pattern_leg")
                and not _REPORT_WORD.search(parsed.normalized)):
            parsed.intent = Intent.POSITION_REPORT

        # A readback is checked before anything else, because an incorrect one
        # has to be corrected before the flight moves on.
        if parsed.intent is Intent.READBACK:
            if session.pending is not None:
                return self._check_readback(parsed, session, station, p, state)
            # Reading back something we never issued: acknowledge rather than
            # asking them to say again, which would be a pointless loop.
            #
            # Except that some of those are not readbacks at all. "Request
            # runway three zero" carries a runway and no request verb the
            # rules know, so it lands here -- and a bare "roger" to it is
            # indistinguishable from not having heard it. Nothing is pending,
            # so nothing here can be a readback of anything; a transmission
            # that asks for a runway in as many words is asking for one.
            session.establish(station)
            if _ASKS_FOR_RUNWAY.search(parsed.normalized):
                self._note_runway(parsed, session, station, asked=True)
                runway = (session.departure_runway if session.phase.outbound
                          else session.arrival_runway)
                if runway:
                    return Reply(p.roger_runway(aircraft, runway), station)
            return Reply(p.roger(aircraft), station)

        # An outstanding readback is settled before any new request is worked.
        # A pilot who has been told to hold short of a runway and answers with
        # "request taxi" has not been told to hold short in any sense that
        # matters, and a controller does not move on until they have said it.
        #
        # This comes before "say again" on purpose. German uses "wiederholen
        # Sie" for both "read back" and "say again", so a pilot echoing a
        # correction was heard as asking the controller to repeat itself and
        # the exchange could not be ended. A transmission carrying the values
        # a readback is waiting for is a readback, whatever else those words
        # could have meant.
        insisted = self._settle_pending(parsed, session, station, p, state)
        if insisted is not None:
            session.establish(station)
            return insisted

        # "Say again" from the pilot asks this controller to repeat itself,
        # which it can do exactly, because it wrote down what it said. What it
        # cannot do is repeat somebody else's transmission: the last
        # instruction of the flight may have come from a frequency the
        # aeroplane has left, and reading it out here would be saying again
        # something that was never said on this one.
        if parsed.intent is Intent.SAY_AGAIN:
            here = station_key(station)
            last = session.pending or session.last_instruction
            if last is not None and last.text and last.station_key == here:
                return Reply(self._repeat(last, station, p, aircraft),
                             station)
            return self._not_understood(session, station, p, state)

        # A runway the pilot asked for, before any handler decides which one
        # they are getting. After the readback handling above, so reading back
        # the runway you were just given is never mistaken for asking for a
        # different one.
        self._note_runway(parsed, session, station)

        # The answer to a traffic call. In sight is the end of it -- and for a
        # VFR flight that is all it is: see-and-avoid is its own business, so
        # it is not told to maintain visual separation, which is what an IFR
        # flight is given (FAA JO 7110.65 7-2-1). Looking, or not seeing it,
        # is acknowledged, and the watch says when it is no factor.
        if parsed.intent in (Intent.REPORT_TRAFFIC_IN_SIGHT,
                             Intent.REPORT_TRAFFIC_LOOKING,
                             Intent.REPORT_TRAFFIC_NOT_IN_SIGHT):
            state_word = {Intent.REPORT_TRAFFIC_IN_SIGHT: "seen",
                          Intent.REPORT_TRAFFIC_LOOKING: "looking",
                          Intent.REPORT_TRAFFIC_NOT_IN_SIGHT: "negative"}[parsed.intent]
            for name, said in list(session.traffic_called.items()):
                if said in ("called", "looking", "negative"):
                    session.traffic_called[name] = state_word
            if parsed.intent is Intent.REPORT_TRAFFIC_IN_SIGHT and session.ifr:
                session.establish(station)
                return Reply(p.maintain_visual_separation(aircraft), station)
            if session.traffic_called or not session.ifr:
                session.establish(station)
                return Reply(p.roger(aircraft), station)

        # A VFR flight plan is flight service's to close. A controller asked
        # to close one passes it on; an information service closes it itself
        # (afis), so it is not answered here.
        if (parsed.intent is Intent.CLOSE_FLIGHT_PLAN
                and station.position not in ("INFO", "CTAF")):
            session.establish(station)
            if not session.flight_plan_open:
                return Reply(p.roger(aircraft), station)
            session.flight_plan_open = False
            decision("flight_plan", station=station.callsign, said="relayed_close")
            return Reply(p.flight_plan_relayed(aircraft), station)

        # The departure over the point it was released by, or saying it is
        # leaving: the last thing the tower wanted from it, so it is let go.
        if (session.expected_exit and station.position == "TWR"
                and self.realism.reporting_points and _airborne(state)
                and (parsed.intent is Intent.REPORT_LEAVING
                     or self._mentions(parsed, session.expected_exit, p))):
            decision("vrp", airport=station.ident, use="exit_reported",
                     point=session.expected_exit)
            session.expected_exit = ""
            session.establish(station)
            return Reply(p.frequency_change_approved(aircraft), station)

        # A full stop ends the circuits. Asking for them set the flag and
        # nothing ever cleared it, so "downwind, full stop" was answered on
        # final with "cleared for the option" and the tower never cleared the
        # aeroplane to land of its own accord.
        if session.pattern_work and parsed.get("full_stop"):
            session.pattern_work = False
            decision("circuits_ended", airport=station.ident, why="pilot",
                     said=parsed.normalized)

        handler = {
            "DEL": self._delivery,
            "GND": self._ground,
            "TWR": self._tower,
            "DEP": self._radar,
            "APP": self._radar,
            "CTR": self._radar,
            "CTAF": self._ctaf,
            "INFO": self._info,
        }.get(station.position)

        if handler is None:
            return Reply("", station, silent=True)
        if parsed.intent is Intent.REPORT_RUNWAY_VACATED:
            session.occupying_runway = ""
            destination = session.destination
            if (self.realism.tower_continue and station.position == "GND"
                    and destination is not None
                    and station.ident == destination.ident
                    and session.phase in (Phase.LANDING, Phase.LANDED,
                                          Phase.TAXI_IN)):
                # An arrival off the runway on Ground's frequency wants its
                # stand: given the taxi in, where "roger" left it sitting
                # on the taxiway with nothing to do (the IFR plan, 5 (e)).
                decision("ground", event="taxi in on vacating",
                         station=station.callsign)
                parsed.intent = Intent.REQUEST_TAXI_TO_PARKING
            else:
                # Acknowledged by whoever is listening: a report, not a
                # request.
                session.establish(station)
                return Reply(p.roger(session.aircraft), station)
        # Special VFR is the zone's: whoever releases or joins a VFR flight
        # there gives it.
        if (parsed.intent is Intent.REQUEST_SPECIAL_VFR
                and station.position in ("DEL", "GND", "TWR")):
            handler = self._special_vfr

        # Two-way contact is only established *after* this transmission has
        # been handled: the reply to a first call uses the full callsign and,
        # for a radar position, opens with "radar contact".
        self._circuit_logged = False
        reply = handler(parsed, session, station, weather, p, state)
        if reply.speak and not self._circuit_logged:
            self.note_circuit(session, station, state,
                              reply.instruction.kind if reply.instruction
                              else parsed.intent.value)
        if reply.speak:
            told = self._old_information(parsed, session, station, p)
            if told:
                reply.text = f"{reply.text} {told[0].upper()}{told[1:]}"
        session.establish(station)
        if reply.instruction is not None and not reply.instruction.from_intent:
            reply.instruction.from_intent = parsed.intent.value
        return reply

    # Set by the engine: ``(ident, language) -> AtisReport | None``, the
    # broadcast the field is putting out now. Unset, an ATIS letter in a call
    # is not checked against anything, which is what tests and a field
    # without an engine get.
    atis = None

    def _old_information(self, parsed, session, station, p) -> str:
        """The current ATIS letter, when the pilot reported another.

        The letter in a first call was parsed, kept, and never compared with
        anything, so a pilot holding a broadcast an hour old was worked on its
        runway and its pressure setting without a word.
        """
        heard = str(parsed.get("atis_letter") or "").strip().upper()
        if not heard or self.atis is None:
            return ""
        if station.position in ("APP", "DEP", "CTR"):
            field = session.destination
        else:
            field = self.navdb.airport(station.ident)
        # Only where there is a broadcast to have heard. A letter reported at
        # a field without one is not wrong; it is the pilot's own business.
        if field is None or self.navdb.station_for(field.ident, "ATIS") is None:
            return ""
        try:
            report = self.atis(field.ident, language_of(p))
        except Exception:
            log.debug("could not read the ATIS for %s", field.ident,
                      exc_info=True)
            return ""
        if report is None or not report.letter or report.letter.upper() == heard:
            return ""
        weather = report.weather
        pressure = (weather.altimeter_inhg if p.dialect == "faa"
                    else weather.qnh_hpa)
        decision("atis_letter_old", airport=field.ident, heard=heard,
                 current=report.letter)
        return p.information_current(report.letter, pressure)

    # ------------------------------------------------------------------
    # emergencies
    # ------------------------------------------------------------------

    # How close an aircraft in trouble has to be before the tower stops
    # talking and clears it to land.
    EMERGENCY_LANDING_NM = 25.0

    def _emergency(self, parsed, session, station, weather, p, state) -> Reply:
        """Answer a distress or urgency call, from any position.

        The first one is acknowledged and the two questions asked -- what is
        wrong, and how many people and how much fuel are on board -- because
        those are what everything after depends on. After that the answer is
        priority and a runway, and at the tower, once the aeroplane is close
        enough, it is a landing clearance.
        """
        aircraft = session.aircraft
        kind = parsed.intent.value
        first = not session.emergency
        session.emergency = kind

        # Minimum fuel is an advisory, not a declaration: nobody is asked for
        # souls on board and no equipment is turned out for it. It is
        # acknowledged and the flight is given priority, which is what it is
        # for.
        if parsed.intent is Intent.MINIMUM_FUEL:
            runway = self._emergency_runway(session, station, weather, state)
            return Reply(
                p.emergency_priority(aircraft, runway, equipment=False),
                station,
            )

        if first:
            return Reply(
                p.emergency_acknowledged(
                    aircraft,
                    self._station_name(station, p)
                    if not session.established_with(station) else "",
                ),
                station,
            )

        runway = self._emergency_runway(session, station, weather, state)
        airport = self.navdb.airport(station.ident)
        close_in = self._distance_to(state, airport) <= self.EMERGENCY_LANDING_NM

        if station.position == "TWR" and _airborne(state) and close_in:
            session.cleared_landing = True
            session.arrival_runway = runway
            session.phase = Phase.LANDING
            session.allow_runway(runway)
            text = p.emergency_cleared_to_land(aircraft, runway, weather)
            instruction = session.remember(
                Instruction("landing_clearance", station_key(station), text,
                            {"runway": runway}))
            return Reply(text, station, instruction=instruction)

        return Reply(p.emergency_priority(aircraft, runway), station)

    def _emergency_runway(self, session, station, weather, state) -> str:
        """The runway to send somebody in trouble to: the nearest one there is."""
        airport = (self.navdb.airport(station.ident)
                   or self._nearest_airport(state) or session.destination)
        if airport is None:
            return session.arrival_runway or session.departure_runway or "01"
        return (session.arrival_runway
                or self._arrival_runway(airport, weather))

    # ------------------------------------------------------------------
    # what the controller is waiting to hear
    # ------------------------------------------------------------------
    #
    # "Say again" tells a pilot that they were not understood and nothing
    # else. On a frequency they have never worked, in a language they are
    # learning, that is the least useful thing a controller could say -- and
    # the controller knows perfectly well what call would make sense, because
    # it knows the position it is working and where the flight has got to.
    # So it says that instead.

    def expected_calls(self, session: FlightSession, station: Station,
                       state=None, language: str = "en") -> list[str]:
        """The calls that would make sense on this frequency, right now."""
        from .pilot import BOOKS, ENGLISH

        book = BOOKS.get((language or "en")[:2], ENGLISH)
        position = station.position if station else ""
        phase = session.phase
        airborne = _airborne(state)
        inbound = phase in (Phase.LANDED, Phase.TAXI_IN, Phase.PARKED)

        wanted: list[str] = []
        if position == "DEL":
            if session.clearance_issued:
                # It has been given. Naming it again is the one call that
                # cannot help, and it was the call this kept naming.
                wanted = [book.request_pushback, book.request_taxi]
            elif not session.ifr:
                wanted = [book.request_vfr_departure, book.request_pushback]
            elif session.destination and session.destination.spoken:
                wanted = [book.request_clearance.format(
                    dest=session.destination.spoken).strip(),
                    book.request_pushback]
            else:
                # No destination filed. The call still has to be nameable, and
                # "request IFR clearance to" with nothing after it is not a
                # call anybody can make.
                wanted = [book.request_clearance_plain
                          or book.request_clearance.format(dest="").strip(),
                          book.request_pushback]
        elif position == "GND":
            wanted = ([book.request_taxi_to_parking] if inbound
                      else [book.request_taxi, book.request_pushback])
        elif position == "TWR":
            if airborne:
                wanted = [book.request_landing, book.field_in_sight]
                if not session.ifr:
                    wanted.insert(0, book.request_pattern_work)
            elif inbound:
                wanted = [book.request_taxi_to_parking]
            else:
                wanted = [book.ready_for_departure_plain]
                if not session.ifr:
                    wanted.append(book.request_pattern_work)
        elif position in ("DEP", "APP", "CTR"):
            wanted = [book.check_in]
            if session.ifr:
                if phase in (Phase.DESCENT, Phase.APPROACH):
                    wanted.append(book.request_landing)
            else:
                wanted += [book.request_flight_following,
                           book.request_transition]
        if not self.ground_control:
            # Suggesting a call nobody will answer is worse than not
            # suggesting it.
            wanted = [call for call in wanted if call != book.request_pushback]
        return [call for call in wanted if call][:3]

    def _repeat(self, instruction: Instruction, station: Station, p,
                aircraft) -> str:
        """An instruction said again, all in one language.

        In the language being spoken now when it can be rebuilt from its
        values; otherwise whole, in the language it was issued in. The stored
        sentence used to go inside the current language's announcement --
        "je répète, descend to flight level two five zero".
        """
        from .restate import restate

        spoken = language_of(p)
        if not instruction.language or instruction.language == spoken:
            return p.say_again_instruction(aircraft, instruction.text)
        rebuilt = restate(instruction, p, aircraft)
        if rebuilt:
            return p.say_again_instruction(aircraft, rebuilt)
        original = self.for_station(station, instruction.language)
        aircraft.language = instruction.language
        return original.say_again_instruction(aircraft, instruction.text)

    # What the first call after a handoff is read as when it is really the
    # check-in. On a radar frequency the readings that come of the words a
    # check-in carries: the altitudes ("climbing seven thousand five
    # hundred", read as a climb request or a readback), and "VFR to the
    # north", read as asking for a VFR departure. A tower or ground keeps
    # the requests it works ("ready for departure", "five miles final"), so
    # only what it could not read at all is taken as the check-in there.
    _CHECK_IN_READINGS = frozenset({Intent.UNKNOWN, Intent.READBACK,
                                    Intent.ACKNOWLEDGE})
    _RADAR_CHECK_IN_READINGS = _CHECK_IN_READINGS | {
        Intent.REQUEST_VFR_DEPARTURE, Intent.REPORT_LEAVING,
        Intent.REQUEST_CLIMB, Intent.REQUEST_DESCENT}
    _ASKING = re.compile(r"\b(?:request|requesting|like)\b")

    def _checking_in_after_handoff(self, session: FlightSession,
                                   station: Station,
                                   parsed: ParsedIntent) -> bool:
        """Whether this is the first call from an aeroplane sent here.

        After 26 October's departure from Centennial: the tower's handoff
        was read back, and "Denver Departure, Diamond one seven two Sierra
        Papa, six thousand five hundred climbing seven thousand five
        hundred, VFR to the north" was answered "expecting with you, or
        request flight following, or request transition" -- the controller
        listing the check-in to a pilot who had just made it. A controller
        handed an aeroplane is waiting for its call, and the first one with
        the callsign in it is the check-in.
        """
        handed = session.handoff_pending
        if (handed is None or not parsed.callsign_matched
                or not same_channel(handed.mhz, station.mhz, 0)
                or session.established_with(station)):
            return False
        pending = session.pending
        if (pending is not None and pending.readback_required
                and not pending.read_back
                and pending.station_key == station_key(station)):
            return False
        radar = station.position in ("DEP", "APP", "CTR")
        readings = (self._RADAR_CHECK_IN_READINGS if radar
                    else self._CHECK_IN_READINGS)
        if parsed.intent not in readings:
            return False
        if (parsed.intent in (Intent.REQUEST_CLIMB, Intent.REQUEST_DESCENT)
                and self._ASKING.search(parsed.normalized)):
            return False
        return True

    # The pilot's altitude and the Mode C disagree by this much or more:
    # the controller says what it sees before telling it to descend.
    MODE_C_DISAGREES_FT = 300.0

    def _altitude_answered(self, session: FlightSession, station: Station,
                           parsed: ParsedIntent, p, state) -> Reply | None:
        """The answer to "say altitude", asked of a VFR flight above the
        altitude this radar holds it under (watch.RestrictionWatch).

        It was "roger", whatever the pilot said: an aeroplane 700 ft over
        its restriction reported itself under it and was left there. The
        answer is read as an altitude and set against the Mode C -- the
        same, and it is told to descend; 300 ft or more apart, it is told
        what the controller sees first, because one of the two is wrong and
        the pilot is the one who can tell which.
        """
        from .watch import RestrictionWatch

        key = station_key(station)
        if (not session.restriction_asked_at or session.radar_service != key
                or session.ifr or not session.vfr_ceiling_ft):
            return None
        heard = parsed.get("altitude_ft")
        shown = getattr(state, "altitude_ft", None) if state is not None else None
        if not heard or shown is None:
            return None
        ceiling = float(session.vfr_ceiling_ft)
        if shown < ceiling + RestrictionWatch.ABOVE_FT:
            return None
        if not session.advise_once(f"restriction:descend:{key}"):
            return None
        session.establish(station)
        disagree = abs(float(heard) - float(shown)) >= self.MODE_C_DISAGREES_FT
        text = p.descend_at_or_below(
            session.aircraft, ceiling,
            transition_altitude=self._transition_altitude(
                self.navdb.airport(station.ident)),
            shown_ft=round(float(shown) / 100.0) * 100.0 if disagree else 0.0)
        decision("vfr_restriction", station=station.callsign, said="descend",
                 heard_ft=heard, mode_c_ft=round(float(shown)),
                 ceiling_ft=round(ceiling), disagree=disagree)
        instruction = session.remember(Instruction(
            "vfr_ceiling", key, text, {"vfr_ceiling_ft": ceiling}))
        return Reply(text, station, instruction=instruction)

    def _not_understood(self, session: FlightSession, station: Station,
                        p: Phraseology, state=None) -> Reply:
        """The answer to a transmission the controller could not act on."""
        return Reply(
            p.expecting(session.aircraft,
                        self.expected_calls(session, station, state,
                                            language_of(p))),
            station,
        )

    # ------------------------------------------------------------------
    # readback checking
    # ------------------------------------------------------------------

    # What gets through an outstanding readback. An emergency does not wait to
    # be collationed, a go-around is flown before it is discussed, and a pilot
    # who says "say again" or "unable" is answering the instruction, not
    # ignoring it. Everything else waits.
    _READBACK_EXEMPT = frozenset({
        Intent.EMERGENCY, Intent.URGENCY, Intent.MINIMUM_FUEL,
        Intent.EMERGENCY_DETAILS,
        Intent.GOING_AROUND, Intent.MISSED_APPROACH,
        Intent.SAY_AGAIN, Intent.UNABLE, Intent.NEGATIVE, Intent.STANDBY,
        Intent.RADIO_CHECK,
    })

    def _settle_pending(
        self, parsed: ParsedIntent, session: FlightSession,
        station: Station, p: Phraseology, state=None,
    ) -> Reply | None:
        """Deal with an instruction that has not been read back yet.

        Returns the controller's reply when the readback has to be insisted on
        (or when the pilot in fact gave one), and ``None`` when the flight may
        get on with whatever it asked for.
        """
        pending = session.pending
        if pending is None or not pending.readback_required:
            return None

        # They are talking to somebody else now. The controller who issued it
        # cannot chase a readback on a frequency the aircraft has left, so the
        # instruction stops being outstanding rather than blocking the new
        # position for the rest of the flight.
        if station_key(station) != pending.station_key:
            session.clear_pending()
            return None

        # "Affirm" to a "confirm the ... departure" settles what was asked.
        if pending.confirming and parsed.intent is Intent.AFFIRMATIVE:
            for key in pending.confirming:
                parsed.readback_items.setdefault(key, pending.values[key])
            return self._check_readback(parsed, session, station, p, state)

        heard = parsed.readback_items
        owed = pending.owed_items

        # A readback by any other name. "Runway two seven, hold short of
        # runway three three" reads as a taxi request to a rule table and as a
        # readback to a controller, and the controller is right.
        if owed and heard and not pending.mismatches(heard):
            return self._check_readback(parsed, session, station, p, state)

        # An instruction with nothing to check back -- a taxi to the ramp, a
        # clearance direct to a fix -- is satisfied by being acknowledged.
        # That is what "roger" and "wilco" are for, and demanding a readback of
        # something with no numbers in it is pedantry rather than safety.
        if not owed and parsed.intent in (Intent.ACKNOWLEDGE,
                                          Intent.AFFIRMATIVE):
            pending.read_back = True
            session.apply(pending.values)
            session.clear_pending()
            return None

        if parsed.intent in self._READBACK_EXEMPT:
            return None

        # "Cleared to land, Delta twelve thirty four": the clearance restated
        # with nothing contradicting it. A runway that is said has to be the
        # right one (that is a readback, checked above); one that is not said
        # is accepted, as most pilots read a landing clearance back.
        if (pending.kind == "landing_clearance"
                and parsed.intent is Intent.ACKNOWLEDGE
                and "cleared to land" in parsed.normalized
                and "runway" not in heard):
            pending.read_back = True
            session.apply(pending.values)
            session.clear_pending()
            return None

        # Asking again for the thing that produced the instruction is not
        # ignoring it, it is not having heard it. That gets the answer -- which
        # may well be "you are already cleared for takeoff" or "hold position"
        # -- rather than a demand for a readback of something unheard. The
        # readback stays owed, and the situation loop goes on chasing it.
        if pending.from_intent and parsed.intent.value == pending.from_intent:
            return None

        return self._insist(session, station, p)

    def _insist(self, session: FlightSession, station: Station,
                p: Phraseology) -> Reply:
        """Ask again for a readback, or re-read the instruction.

        Twice the controller names what it is waiting for. After that it reads
        the whole instruction again, because a pilot who has not read back
        twice has usually not heard it, and asking a third time for a readback
        of something they never received helps nobody.
        """
        pending = session.pending
        assert pending is not None
        pending.prompted()
        if pending.prompts > PROMPTS_BEFORE_REISSUE:
            pending.prompts = 0
            return Reply(
                self._repeat(pending, station, p, session.aircraft),
                station, instruction=pending,
            )
        return Reply(
            p.read_back_instruction(session.aircraft, pending.owed_items),
            station, instruction=pending,
        )

    def chase_readback(self, session: FlightSession, station: Station | None,
                       p: Phraseology) -> Reply | None:
        """What the controller says when the pilot simply goes quiet.

        Called by the situation loop rather than by a transmission. A readback
        that is never given is the case the old code could not see at all: the
        instruction sat pending forever and nothing was ever said about it.
        """
        pending = session.pending
        if pending is None or not pending.readback_required:
            return None
        if station is None or station_key(station) != pending.station_key:
            return None
        if not pending.overdue:
            return None
        if pending.kind == "handoff" and pending.prompts >= 1:
            # Asked for once. What follows, if the flight has not switched,
            # is the handoff said again (Engine._reoffer_handoff), not the
            # same request on repeat.
            return None
        return self._insist(session, station, p)

    def _check_readback(
        self, parsed: ParsedIntent, session: FlightSession,
        station: Station, p: Phraseology, state=None,
    ) -> Reply:
        pending = session.pending
        assert pending is not None
        wrong = pending.mismatches(parsed.readback_items)
        wrong += [key for key in self._vfr_items_wrong(parsed, session, pending, station, p)
                  if key not in wrong]

        # Only what the recogniser nearly caught, and nothing else wrong --
        # a SID's name heard close, a figure one off: asked about, the way a
        # controller who did not quite hear it asks, rather than "negative"
        # to a pilot who may well have said it right.
        nearly = set(getattr(parsed, "nearly", set()))
        if self._unsure(parsed):
            heard = parsed.readback_items or {}
            nearly |= {key for key in wrong
                       if nearly_matches(key, pending.values.get(key),
                                         heard.get(key))}
        if wrong and set(wrong) <= nearly:
            pending.outstanding = set(wrong)
            pending.confirming = set(wrong)
            decision("readback", station=station.callsign, instruction=pending.kind,
                     said="confirm", items=sorted(wrong),
                     heard_confidence=round(parsed.heard_confidence, 2),
                     confirm_below=self._confirm_below(), heard=parsed.text)
            return Reply(
                p.correction(
                    session.aircraft, wrong, pending.values,
                    current_altitude_ft=getattr(state, "altitude_ft", 0.0) or 0.0,
                    transition_altitude=self._transition_altitude(
                        self.navdb.airport(station.ident)),
                    confirm=True),
                station, instruction=pending)

        if not wrong:
            pending.read_back = True
            session.apply(pending.values)
            session.clear_pending()

            # Delivery hands off to Ground once the clearance is read back;
            # every other position simply accepts it.
            if pending.kind == "ifr_clearance":
                session.clearance_read_back = True
                session.phase = Phase.CLEARED
                ground = self.controller_at(station.ident, "GND")
                if ground:
                    return Reply(
                        p.readback_correct(
                            session.aircraft,
                            next_station=self._station_name(ground, p, short=True),
                            next_freq=ground.mhz,
                        ),
                        station,
                        handoff_to=ground,
                    )
                return Reply(p.readback_correct(session.aircraft), station)

            if pending.kind == "handoff" and pending.values.get("frequency"):
                return Reply("", station, silent=True)  # they are leaving; say nothing more

            if pending.kind in ("takeoff_clearance", "landing_clearance"):
                return Reply("", station, silent=True)

            return Reply(p.readback_correct(session.aircraft), station)

        # Something was wrong or missing: correct just that item, and from
        # here on judge the answer on that item alone. The pilot is replying
        # to the question they were asked, not reciting the clearance again.
        pending.outstanding = set(wrong)
        pending.confirming = None
        pending.attempts += 1
        if pending.attempts >= ATTEMPTS_BEFORE_REISSUE:
            # Correcting the same item a fourth time is not working, so the
            # whole instruction is read again. It is still not accepted: an
            # instruction that has not been read back correctly is not one the
            # aircraft is operating under, and pretending otherwise is the
            # thing a readback exists to prevent.
            pending.attempts = 0
            return Reply(
                self._repeat(pending, station, p, session.aircraft),
                station, instruction=pending,
            )

        return Reply(
            p.correction(
                session.aircraft, wrong, pending.values,
                current_altitude_ft=getattr(state, "altitude_ft", 0.0) or 0.0,
                transition_altitude=self._transition_altitude(
                    self.navdb.airport(station.ident)),
            ),
            station, instruction=pending,
        )

    def _vfr_items_wrong(self, parsed, session, pending, station,
                         p) -> list[str]:
        """The VFR items of a release or a transit read back as something
        else: the ceiling ("not above one thousand five hundred") and the
        point to leave by.

        Only a figure or a point that *was* said and is wrong; one the
        recogniser did not catch is not chased, because neither is on the
        list of items a pilot must read back word for word (FAA AIM 4-4-7,
        ICAO Doc 4444 4.5.7.5) and insisting on them would be insisting on
        the recogniser. Logged, either way, so the arrival log shows what
        was heard against what was given.
        """
        if not self.realism.readback_items:
            return []
        values = pending.values
        if not ("vfr_ceiling_ft" in values or "exit_point" in values):
            return []
        heard = parsed.readback_items or {}
        # Answering a correction is answering that item alone.
        asked = pending.outstanding
        wrong = []
        altitude = heard.get("altitude_ft")
        # A code read as a height ("squawk one two zero zero" is also twelve
        # hundred feet) is not a ceiling.
        if altitude and str(int(float(altitude))) == str(values.get("squawk", "")).lstrip("0"):
            altitude = None
        if ("vfr_ceiling_ft" in values and altitude
                and (asked is None or "vfr_ceiling_ft" in asked)):
            if int(float(altitude)) != int(float(values["vfr_ceiling_ft"])):
                wrong.append("vfr_ceiling_ft")
        said_point = None
        if "exit_point" in values and (asked is None or "exit_point" in asked):
            if self._mentions(parsed, values["exit_point"], p):
                said_point = values["exit_point"]
            else:
                # Only a point said as the way out ("via Sierra Alpha"): a
                # point's name on its own is as likely to be the callsign --
                # "November one seven two Sierra Papa" is not November.
                text = f" {parsed.normalized} "
                said_point = next(
                    (v.name for v in self.navdb.vrps(station.ident)
                     if v.spoken and v.name != values["exit_point"]
                     and re.search(rf" {_EXIT_WORDS} ({re.escape(p.vrp(v.name).lower())}"
                                   rf"|{re.escape(v.name.lower())}) ", text)),
                    None)
                if said_point is not None:
                    wrong.append("exit_point")
        decision("readback_vfr", station=station.callsign, instruction=pending.kind,
                 ceiling_given=values.get("vfr_ceiling_ft"),
                 ceiling_heard=heard.get("altitude_ft"),
                 point_given=values.get("exit_point"), point_heard=said_point,
                 wrong=wrong or None)
        return wrong

    # ------------------------------------------------------------------
    # clearance delivery
    # ------------------------------------------------------------------

    def _send_on(self, session, station, p, *positions) -> Reply | None:
        """Hand the aircraft to the first of these positions that is staffed.

        ``None`` when none of them is, which means the position holding the
        aircraft has to deal with the request itself rather than pass it to
        somebody who is not there.
        """
        for position in positions:
            found = self.controller_at(station.ident, position)
            if found is None or station_key(found) == station_key(station):
                continue
            return Reply(
                p.handoff(session.aircraft,
                          self._station_name(found, p, short=True), found.mhz),
                station, handoff_to=found,
            )
        return None

    def _delivery(self, parsed, session, station, weather, p, state) -> Reply:
        aircraft = session.aircraft

        # Delivery works aircraft at the gate. One that is already flying wants
        # a controller who can see it on radar, so it is sent to one rather
        # than issued a departure clearance it has no use for.
        if _airborne(state) and parsed.intent is not Intent.RADIO_CHECK:
            return self._hand_off_departure(session, station, p)

        if parsed.intent in (Intent.REQUEST_CLEARANCE, Intent.CHECK_IN,
                             Intent.POSITION_REPORT):
            # A VFR flight has no IFR clearance to be given. Reading one to it
            # was the clearest case of the controller using the wrong half of
            # the phrasebook: it is released, not cleared to a destination.
            if not session.ifr:
                return self._issue_vfr_release(session, station, p, parsed, weather)
            return self._issue_clearance(session, station, weather, p)

        if parsed.intent is Intent.REQUEST_VFR_DEPARTURE:
            return self._issue_vfr_release(session, station, p, parsed, weather)

        if parsed.intent is Intent.REQUEST_PATTERN_WORK:
            # Circuits are a tower matter, and the aircraft has to taxi first.
            # They are flown visually (FlightSession.visual) without changing
            # the rules the flight was filed under.
            session.pattern_work = True
            ground = self.controller_at(station.ident, "GND")
            if ground and not same_channel(ground.mhz, station.mhz, 0):
                return Reply(
                    p.handoff(aircraft,
                              self._station_name(ground, p, short=True),
                              ground.mhz),
                    station, handoff_to=ground,
                )
            return self._issue_taxi(session, station, weather, p, state)

        if parsed.intent is Intent.REQUEST_TAXI:
            ground = self.controller_at(station.ident, "GND")
            if ground and not same_channel(ground.mhz, station.mhz, 0):
                return Reply(
                    p.handoff(aircraft, self._station_name(ground, p, short=True), ground.mhz),
                    station, handoff_to=ground,
                )
            return self._issue_taxi(session, station, weather, p, state)

        # Delivery pushes nobody back and launches nobody, but it is the
        # frequency the aircraft is on, so it answers -- by naming whoever
        # does that job, or by doing it itself at a field with nobody else.
        # Handing the request back as a list of other calls was the one reply
        # that left the pilot with nowhere to go.
        if parsed.intent is Intent.REQUEST_PUSHBACK:
            sent = self._send_on(session, station, p, "GND")
            if sent is not None:
                return sent
            reason = self._stuck_on_the_stand()
            if reason:
                return Reply(p.hold_position(aircraft) + f" You are {reason}.",
                             station)
            return Reply(p.pushback_approved(aircraft), station,
                         ground="pushback")

        if parsed.intent is Intent.REQUEST_TAKEOFF:
            if _airborne(state):
                return Reply(p.already_airborne(aircraft), station)
            sent = self._send_on(session, station, p, "TWR", "GND")
            if sent is not None:
                return sent
            return self._issue_takeoff(session, station, weather, p, parsed)

        if parsed.intent is Intent.RADIO_CHECK:
            return Reply(p.radio_check(aircraft), station)

        if parsed.intent in (Intent.ACKNOWLEDGE, Intent.AFFIRMATIVE):
            return Reply("", station, silent=True)

        return self._not_understood(session, station, p, state)

    def _issue_clearance(self, session, station, weather, p) -> Reply:
        airport = self.navdb.airport(station.ident)
        destination = session.destination
        dest_name = (
            destination.spoken if destination
            else (session.destination_input or p.UNKNOWN_DESTINATION)
        )

        session.squawk = squawk_for(session, station.ident)
        cruise = session.cruise_altitude_ft or self._default_cruise(session)

        departure_station = (
            self.navdb.station_for(station.ident, "DEP")
            or self.navdb.station_for(station.ident, "APP")
        )
        departure_freq = departure_station.mhz if departure_station else 0.0

        # No open dataset publishes SIDs, so the clearance is issued with
        # radar vectors, which is correct phraseology in its own right --
        # unless the flight plan names one for the runway in use, which is
        # what a controller would clear.
        sid = self._planned_sid(airport, weather)
        runway = self._departure_runway(airport, weather) if airport else ""
        worked = self.realism.ifr_departure
        if worked:
            initial, expect_minutes = self._departure_altitudes(
                session, airport, sid, runway, cruise)
            sid = self._sid_for_runway(airport, sid, runway, weather)
        else:
            initial, expect_minutes = session.initial_altitude_ft or 5000.0, 10
        session.initial_altitude_ft = float(initial)
        # As the chart prints it, ROUSY 1L, which is what is said and what a
        # readback is held to; the plan's six-letter ROUS1L is neither.
        sid_name = written_procedure(sid, self._plan_idents()) if sid else ""
        session.sid = sid_name
        text = p.ifr_clearance(
            session.aircraft,
            self._station_name(station, p) if not session.established_with(station) else "",
            dest_name,
            departure_procedure=sid_name,
            route=p.ROUTE_AS_FILED if sid else p.ROUTE_RADAR_VECTORS,
            initial_altitude=initial,
            cruise_altitude=cruise,
            expect_minutes=expect_minutes,
            departure_freq=departure_freq,
            squawk=session.squawk,
            transition_altitude=self._transition_altitude(airport),
            runway=runway if worked else "",
        )

        session.clearance_issued = True
        values: dict[str, Any] = {"altitude_ft": int(initial), "squawk": session.squawk}
        if departure_freq:
            values["frequency"] = departure_freq
        # A clearance readback that gets the altitude and the code right is
        # accepted. Pilots routinely leave the departure frequency out of the
        # readback and no controller challenges it. The SID is not left out:
        # it is the route the aeroplane is about to fly (ICAO Doc 4444
        # 4.5.7.5.1 a), the route clearance, is a readback item).
        required = {"altitude_ft", "squawk"}
        if sid_name and worked:
            values["sid"] = sid_name
            required.add("sid")
        instruction = session.remember(
            Instruction("ifr_clearance", station_key(station), text, values,
                        required_items=required)
        )
        return Reply(text, station, instruction=instruction)

    def _hear_procedure(self, parsed, session) -> None:
        """A SID or a STAR owed in a readback, found in what was said.

        The parser has no slot for a procedure's name -- it would have to
        know every SID there is -- so the one that is owed is looked for in
        the words (atc/procedures.py ``heard``) and put with the rest of the
        readback's items.
        """
        pending = session.pending
        if pending is None:
            return
        text = getattr(parsed, "text", "") or ""
        for key in ("sid", "star"):
            if key not in (pending.values or {}):
                continue
            expected = str(pending.values[key])
            if procedure_heard(text, expected, self._plan_idents()):
                parsed.readback_items[key] = expected
            elif (key == "sid" and self._unsure(parsed)
                  and procedure_near(text, expected, self._plan_idents())):
                parsed.nearly.add(key)

    def _confirm_below(self) -> float:
        return float(getattr(self.hearing, "confirm_below", 0.0) or 0.0)

    def _unsure(self, parsed) -> bool:
        """The recogniser was less sure of these words than
        ``speech.confirm_below``; typed text is always sure."""
        return getattr(parsed, "heard_confidence", 1.0) < self._confirm_below()

    def _plan_idents(self) -> list[str]:
        idents = [f.ident for f in getattr(self.route, "fixes", ()) or ()]
        return idents or [getattr(f, "ident", "") for f in self.plan_fixes]

    def _departure_altitudes(self, session, airport, sid: str, runway: str,
                             cruise: float) -> tuple[int, int]:
        """The clearance's initial altitude and the minutes to the cruise.

        The altitude is the field's own (atc/climb.py), lifted over the
        terrain round it; the minutes are the plan's time to its top of
        climb, else the climb worked out from the cruise (atc/route.py).
        """
        from .route import Route

        elevation = float(getattr(airport, "elev_ft", 0.0) or 0.0)
        floor = 0.0
        if airport is not None:
            floor = terrain.floor_ft(airport.lat, airport.lon, elevation)
        initial = initial_altitude(
            elevation, getattr(airport, "ident", ""), sid, runway,
            session.aircraft.callsign.written, floor_ft=floor)
        route = self.route if self.route else Route(())
        minutes = route.minutes_to_cruise(cruise)
        decision("clearance", airport=getattr(airport, "ident", ""),
                 initial_ft=initial, terrain_floor_ft=round(floor),
                 field_ft=round(elevation), sid=sid, runway=runway,
                 expect_minutes=minutes,
                 minutes_from=("plan" if route and route.top_of_climb
                               and route.top_of_climb.time_s else
                               "route" if route else "rate"))
        return initial, minutes

    def _sid_for_runway(self, airport, sid: str, runway: str, weather) -> str:
        """The plan's SID while its runway is the one in use; said why not.

        A SimBrief plan names one SID, for one runway: another runway in use
        is radar vectors, and the log says which runway the SID was for.
        """
        planned = self.plan_runways.get(getattr(airport, "ident", ""), {})
        filed = planned.get("sid", "")
        if filed and not sid:
            decision("sid", airport=getattr(airport, "ident", ""), filed=filed,
                     planned_runway=planned.get("departing", ""),
                     runway=runway, cleared="radar vectors",
                     why="the plan's SID is for another runway")
        elif sid:
            decision("sid", airport=getattr(airport, "ident", ""), filed=sid,
                     runway=runway, cleared=sid)
        return sid

    # How high a VFR departure is kept until it is clear of the circuit. A
    # real one is published per field; this is the value that fits almost all
    # of them and is what keeps the aircraft under the arriving traffic.
    VFR_CEILING_AGL_FT = 2500.0

    # The requests a pilot names a runway in. Deliberately not every intent
    # that can carry one: a position report saying "runway two four in sight"
    # is not a request to change the runway in use, and a readback of the
    # runway just issued is the opposite of asking for another.
    _RUNWAY_REQUESTS = frozenset({
        Intent.REQUEST_CLEARANCE, Intent.REQUEST_TAXI, Intent.REQUEST_TAKEOFF,
        Intent.REQUEST_PUSHBACK, Intent.REQUEST_VFR_DEPARTURE,
        Intent.REQUEST_APPROACH, Intent.REQUEST_LANDING,
        Intent.REQUEST_PATTERN_WORK,
    })

    def _note_runway(self, parsed, session, station,
                     asked: bool = False) -> None:
        """Use the runway the pilot asked for, where the field has one.

        The runway was decided from the wind and nothing else, so a pilot at
        Prague asking for three zero was cleared for zero six every time --
        and asking again changed nothing, because there was nowhere for the
        request to be written down. A real controller is asked for a runway
        all the time and normally approves it: it is the pilot who knows how
        long their aeroplane needs and which way they are going.

        Only a runway the field actually publishes. One it has not got is left
        alone rather than acted on, so a misheard number gets the runway in
        use rather than a clearance to pavement that is not there.
        """
        if not asked and parsed.intent not in self._RUNWAY_REQUESTS:
            return
        wanted = parsed.get("runway")
        if wanted is None:
            return
        wanted = normalize_runway(str(wanted))
        if not wanted:
            return
        airport = self.navdb.airport(station.ident)
        if airport is None:
            return
        published = {name for name, _heading, _runway
                     in self.navdb.runway_ends(airport.ident)}
        if wanted not in published:
            return
        if session.phase.outbound:
            session.departure_runway = wanted
        else:
            session.arrival_runway = wanted

    # The position a callup names, by its last word ("Centennial Ground").
    # The field's own positions only: Approach and Departure are one radar
    # room at most fields, and "Boston Departure" said on the Approach
    # frequency is answered there, not sent away.
    _CALLED = {"ground": "GND", "tower": "TWR", "clearance": "DEL",
               "delivery": "DEL"}
    # How far into a transmission the callup is, in words, when the pilot
    # did not pause after it: the field's name and the position.
    _CALLUP_WORDS = 4

    def _called_elsewhere(self, session, station, parsed, p) -> Reply | None:
        """The pilot named another of this field's positions on this one's
        frequency: say where that one is, and do nothing else.

        At Centennial a call to "Centennial Ground" made on the Delivery
        frequency was answered by Delivery with a VFR release, as if it had
        been addressed -- which tells the pilot nothing about having dialled
        the wrong number, and has Delivery doing Ground's job. The one
        exception is what this position does at this field anyway: in the
        US a VFR departure from inside a Class B or C collects its squawk
        from Delivery, whoever the pilot happened to name.
        """
        if station is None or not parsed.understood \
                or parsed.intent in self._NOT_A_REQUEST:
            return None
        head = re.split(r"[,.;]", parsed.text or "", maxsplit=1)[0].lower()
        words = re.findall(r"[a-z]+", head)[:self._CALLUP_WORDS]
        called = next((self._CALLED[w] for w in words if w in self._CALLED), "")
        if not called or called == station.position                 or station.position not in ("DEL", "GND", "TWR"):
            return None
        other = self.controller_at(station.ident, called)
        if other is None or not self.staffed(other) \
                or same_channel(other.mhz, station.mhz, 4):
            return None
        airport = self.navdb.airport(station.ident)
        if (station.position == "DEL"
                and parsed.intent is Intent.REQUEST_VFR_DEPARTURE
                and p.region == "faa"
                and self._airspace_class(airport, station) in ("B", "C")):
            return None
        decision("called_elsewhere", station=station.callsign,
                 called=other.callsign, mhz=other.mhz)
        return Reply(p.called_elsewhere(
            session.aircraft, self._station_name(station, p),
            self._station_name(other, p), other.mhz), station)

    # What the pilot says back is the controller's words, not a request:
    # "squawk VFR" read back is not a VFR flight.
    _NOT_A_REQUEST = frozenset({Intent.READBACK, Intent.ACKNOWLEDGE})

    @classmethod
    def _note_rules(cls, session, parsed) -> None:
        """Believe the pilot about which rules they are flying under.

        The rules live on the session, and the session gets them from the
        flight it was set up with -- which is a setting, and one a pilot may
        never have looked at. So a flight left at the default called up, asked
        for an IFR clearance in as many words, and was answered with a VFR
        release; and because the release itself writes VFR onto the session,
        every later request was answered the same way. The one transmission
        that said what was actually filed was the one thing being thrown away.

        Only when it was said. A bare "request clearance" leaves the session
        where it is, which is what keeps a VFR flight collecting its release
        from delivery rather than being read an airways clearance it did not
        file. And to any station: it was Delivery and Ground only, so "IFR
        to Boston" said to a tower that issues its own clearances was not
        heard. A cancellation says "IFR" too, and is the radar's to take.
        """
        if (parsed.intent in cls._NOT_A_REQUEST
                or parsed.intent is Intent.CANCEL_IFR):
            return
        if parsed.rules == "ifr":
            session.ifr = True
        elif parsed.rules == "vfr":
            session.ifr = False

    def _issue_vfr_release(self, session, station, p, parsed=None,
                           weather=None) -> Reply:
        """Release a VFR flight, in the direction it asked to go.

        Not a clearance. It is an approval to leave, a ceiling while it is
        under somebody else's traffic, and a code -- and the code is the
        conspicuity one unless a radar position is going to work the flight,
        because nobody hands a discrete code to an aircraft they will never
        see.
        """
        session.ifr = False
        airport = self.navdb.airport(station.ident)
        refused = self._below_vmc(session, station, weather, p, airport,
                                  asked="vfr_release")
        if refused is not None:
            return refused
        departure_station = (
            self.navdb.station_for(station.ident, "DEP")
            or self.navdb.station_for(station.ident, "APP")
        )
        freq = departure_station.mhz if departure_station else 0.0
        session.squawk = (
            squawk_for(session, station.ident) if freq else p.vfr_squawk
        )

        asked = self._direction_asked(parsed)
        direction = p.bearing(asked)
        # The published way out in that direction, where the field has one
        # a controller can say ("leave the zone via Sierra Echo").
        exit_point = ""
        if asked in self._COMPASS_DEG and airport is not None:
            point = self.vrp_toward(airport, self._COMPASS_DEG[asked],
                                    departing=True)
            if point is not None:
                exit_point = p.vrp(point.name)
                session.expected_exit = point.name
                decision("vrp", airport=airport.ident, use="exit",
                         point=point.name, source=point.source)
        ceiling = self._vfr_ceiling(airport)
        session.vfr_ceiling_ft = ceiling

        text = p.vfr_departure_clearance(
            session.aircraft,
            self._station_name(station, p) if not session.established_with(station) else "",
            airspace=p.airspace(self._airspace_class(airport, station)),
            departure_freq=freq,
            squawk=session.squawk,
            altitude_restriction=ceiling,
            direction=direction,
            exit_point=exit_point,
            # The pressure is in every release: a VFR departure on the wrong
            # setting is the one a release exists to prevent. The way out of
            # the zone is the direction until there are reporting points to
            # name (Phase 4 of the VFR plan).
            pressure=(0.0 if weather is None else
                      weather.altimeter_inhg if p.region == "faa"
                      else weather.qnh_hpa),
        )
        values: dict[str, Any] = {"squawk": session.squawk}
        if freq:
            values["frequency"] = freq
        if ceiling:
            values["vfr_ceiling_ft"] = float(ceiling)
        if session.expected_exit and exit_point:
            values["exit_point"] = session.expected_exit
        instruction = session.remember(
            Instruction("vfr_release", station_key(station), text, values)
        )
        return Reply(text, station, instruction=instruction)

    # ------------------------------------------------------------------
    # VFR helpers
    # ------------------------------------------------------------------

    def zone_weather(self, airport, weather):
        """The field's weather against its region's VFR rules
        (:mod:`wilcoatc.atc.svfr`), or None where they are not applied:
        switched off (``immersion.controller.special_vfr``), no field, or no
        weather."""
        if not self.realism.special_vfr or airport is None or weather is None:
            return None
        from .svfr import conditions

        return conditions(weather, self.region_for(airport.ident),
                          airport.lat, airport.lon)

    def _below_vmc(self, session, station, weather, p, airport, *,
                   asked: str, outside: bool = False,
                   offer: bool = True) -> Reply | None:
        """The answer to a plain VFR request into weather below the zone's
        VFR minima, or None when the weather allows it.

        Special VFR is offered where it may be given (``offer``, and only
        when the minima for it are met); an aeroplane outside the zone is
        kept out of it (``outside``).
        """
        if session.ifr or session.special_vfr:
            return None
        rules = self.zone_weather(airport, weather)
        if rules is None or rules.vmc:
            return None
        decision("vmc", airport=airport.ident, asked=asked,
                 said="below_minima", **rules.as_log())
        text = p.vfr_below_minima(
            session.aircraft,
            self._station_name(station, p)
            if not session.established_with(station) else "",
            offer=offer and rules.svfr,
            airspace=(p.airspace(self._airspace_class(airport, station))
                      if outside else ""))
        instruction = session.remember(
            Instruction("below_minima", station_key(station), text, {},
                        readback_required=False))
        return Reply(text, station, instruction=instruction)

    def _special_vfr(self, parsed, session, station, weather, p, state) -> Reply:
        """Special VFR, asked for: out of the zone from the ground, into it
        from the air.

        Refused below the Special VFR minima, and at night only where a
        source says the region refuses it (:mod:`wilcoatc.atc.svfr`). Above VFR minima nobody
        needs it, and the ordinary release or join is given instead. The
        route is by the field's Special VFR points (kind "svfr" in
        ``vrps.csv``) where it has them.
        """
        from .svfr import svfr_altitude

        aircraft = session.aircraft
        airport = self.navdb.airport(station.ident)
        airborne = _airborne(state)
        session.ifr = False
        rules = self.zone_weather(airport, weather)
        if rules is not None:
            decision("special_vfr", airport=station.ident, airborne=airborne,
                     **rules.as_log())
        if rules is None or rules.vmc:
            if airborne:
                return self._work_the_circuit(session, station, weather, p,
                                              parsed, state)
            return self._issue_vfr_release(session, station, p, parsed, weather)
        station_name = (self._station_name(station, p)
                        if not session.established_with(station) else "")
        if not rules.svfr:
            text = p.special_vfr_unable(aircraft, rules.refused_for, station_name)
            instruction = session.remember(
                Instruction("svfr_unable", station_key(station), text, {},
                            readback_required=False))
            return Reply(text, station, instruction=instruction)

        elevation = float(getattr(airport, "elev_ft", 0.0) or 0.0)
        altitude = svfr_altitude(elevation, rules.ceiling_ft,
                                 self._vfr_ceiling(airport))
        session.vfr_ceiling_ft = altitude
        session.special_vfr = True
        airspace = p.airspace(self._airspace_class(airport, station))
        zone = getattr(airport, "spoken", "") or ""
        pressure = (weather.altimeter_inhg if p.region == "faa"
                    else weather.qnh_hpa)

        if airborne:
            point = None
            if getattr(state, "has_position", False):
                point = self.vrp_toward(
                    airport, bearing_deg(airport.lat, airport.lon,
                                         state.latitude, state.longitude),
                    departing=False, svfr=True)
            said = p.vrp(point.name) if point is not None else ""
            if point is not None:
                session.expected_vrp = point.name
                decision("vrp", airport=airport.ident, use="svfr_entry",
                         point=point.name, source=point.source)
            session.phase = Phase.APPROACH
            text = p.special_vfr_enter(
                aircraft, station_name, zone=zone, airspace=airspace,
                entry_point=said, altitude_ft=altitude, pressure=pressure,
                report=said)
            decision("special_vfr", airport=station.ident, said="enter",
                     altitude_ft=altitude, point=point.name if point else None)
            instruction = session.remember(
                Instruction("svfr_entry", station_key(station), text,
                            {"vfr_ceiling_ft": altitude}))
            return Reply(text, station, instruction=instruction)

        departure_station = (self.navdb.station_for(station.ident, "DEP")
                             or self.navdb.station_for(station.ident, "APP"))
        freq = departure_station.mhz if departure_station else 0.0
        session.squawk = (squawk_for(session, station.ident) if freq
                          else p.vfr_squawk)
        asked = self._direction_asked(parsed)
        point = (self.vrp_toward(airport, self._COMPASS_DEG[asked],
                                 departing=True, svfr=True)
                 if asked in self._COMPASS_DEG else None)
        if point is not None:
            session.expected_exit = point.name
            decision("vrp", airport=airport.ident, use="svfr_exit",
                     point=point.name, source=point.source)
        text = p.special_vfr_leave(
            aircraft, station_name, zone=zone, airspace=airspace,
            exit_point=p.vrp(point.name) if point is not None else "",
            direction=p.bearing(asked) if asked else "",
            altitude_ft=altitude, pressure=pressure, squawk=session.squawk,
            departure_freq=freq)
        decision("special_vfr", airport=station.ident, said="leave",
                 altitude_ft=altitude, point=point.name if point else None)
        values: dict[str, Any] = {"squawk": session.squawk,
                                  "vfr_ceiling_ft": float(altitude)}
        if freq:
            values["frequency"] = freq
        if point is not None:
            values["exit_point"] = point.name
        instruction = session.remember(
            Instruction("vfr_release", station_key(station), text, values))
        return Reply(text, station, instruction=instruction)

    # The parser hands back English compass names whatever was spoken, so the
    # decision layer works in those and the phraseology turns one into the
    # local word. Compounds first: "northwest" contains "north".
    _COMPASS = ("northeast", "northwest", "southeast", "southwest",
                "north", "south", "east", "west")

    @classmethod
    def _direction_asked(cls, parsed) -> str:
        """Which way the pilot said they were going, if they said."""
        if parsed is None:
            return ""
        asked = parsed.get("direction") or ""
        if asked:
            return str(asked).lower()
        text = getattr(parsed, "normalized", "") or ""
        for word in cls._COMPASS:
            if word in text:
                return word
        # French: "vers le sud-est", "au nord", "vers l'est". Compound
        # points first, so "sud est" is not read as "sud".
        for french, english in cls._COMPASS_FR:
            if re.search(rf"\b(vers (le |l )?|au |direction ){french}\b", text):
                return english
        return ""

    _COMPASS_FR = (("nord est", "northeast"), ("nord ouest", "northwest"),
                   ("sud est", "southeast"), ("sud ouest", "southwest"),
                   ("nord", "north"), ("sud", "south"), ("est", "east"),
                   ("ouest", "west"))

    # A point this far either side of the way the flight is going is on its
    # way; further round it is not.
    VRP_SECTOR_DEG = 60.0

    def _points(self, airport, *, departing: bool, svfr: bool = False) -> list:
        """The field's reporting points a controller can say, for a
        departure or for an arrival.

        Special VFR points (kind "svfr") for a Special VFR flight; for an
        ordinary one only where the field has no other point -- Lille
        publishes coordinates for its Special VFR points and none for the
        ordinary ones, which are drawn on the chart.
        """
        if airport is None:
            return []
        spoken = [v for v in self.navdb.vrps(airport.ident) if v.spoken]
        special = [v for v in spoken if v.for_svfr]
        if svfr:
            return special
        ordinary = [v for v in spoken
                    if (v.for_departures if departing else v.for_arrivals)]
        return ordinary or special

    def atis_briefing(self, airport, runway: str, p) -> tuple[str, list[str]]:
        """The VFR part of a field's ATIS: the published circuit side for
        ``runway`` ("" where none is published) and the ordinary reporting
        points, said as a controller says them. Nothing with
        ``immersion.controller.atis_briefing`` off."""
        if not self.realism.atis_briefing or airport is None:
            return "", []
        published = self.navdb.circuit_data(airport.ident, runway)
        side = (published.side or "") if published is not None else ""
        points = [p.vrp(v.name) for v in self.navdb.vrps(airport.ident)
                  if v.spoken and not v.for_svfr
                  and (v.for_departures or v.for_arrivals)]
        return side, points

    def vrp_toward(self, airport, bearing: float, *, departing: bool,
                   svfr: bool = False):
        """The point nearest the direction a flight is going, or None."""
        best = None
        for point in self._points(airport, departing=departing, svfr=svfr):
            towards = bearing_deg(airport.lat, airport.lon, point.lat, point.lon)
            off = abs(((towards - bearing) + 540.0) % 360.0 - 180.0)
            if off <= self.VRP_SECTOR_DEG and (best is None or off < best[0]):
                best = (off, point)
        return best[1] if best else None

    def vrp_ahead(self, airport, state):
        """The arrival point between an aeroplane and the field: on its side
        of the field, and nearer the field than it is. None once it is
        inside them all."""
        if airport is None or state is None or not getattr(state, "has_position", False):
            return None
        from_field = bearing_deg(airport.lat, airport.lon,
                                 state.latitude, state.longitude)
        point = self.vrp_toward(airport, from_field, departing=False)
        if point is None:
            return None
        out = state.distance_to(airport.lat, airport.lon)
        there = state.distance_to(point.lat, point.lon)
        point_out = haversine_nm(airport.lat, airport.lon, point.lat, point.lon)
        return point if out > point_out and there < out else None

    _COMPASS_DEG = {"north": 0.0, "northeast": 45.0, "east": 90.0,
                    "southeast": 135.0, "south": 180.0, "southwest": 225.0,
                    "west": 270.0, "northwest": 315.0}

    # How far under the floor of the airspace above a VFR ceiling is set.
    UNDER_THE_SHELF_FT = 500.0
    # The top of the airspace a VFR flight may use where Class A begins
    # (FAA: 18,000 ft MSL).
    BELOW_CLASS_A_FT = 17500.0

    def _vfr_ceiling(self, airport) -> float:
        """A level to keep a VFR departure or transit at or below, in feet
        AMSL: under the floor of the airspace above the field -- a Class B
        or C shelf, a TMA base -- where one is published
        (``NavDB.airspace_over``), else field elevation plus
        VFR_CEILING_AGL_FT. That figure put Centennial's departures at 8,500
        ft, inside Denver's Class B, which starts at 8,000 above the field.
        """
        elevation = float(getattr(airport, "elev_ft", 0.0) or 0.0)
        floors = []
        if airport is not None:
            for volume in self.navdb.airspace_over(airport.lat, airport.lon):
                floor = volume.floor_msl(elevation)
                if volume.cls in "ABC" and volume.airport != airport.ident \
                        and floor > elevation + 300.0:
                    floors.append(floor)
        if floors:
            ceiling = math.floor((min(floors) - self.UNDER_THE_SHELF_FT) / 500.0) * 500.0
            decision("vfr_ceiling", airport=getattr(airport, "ident", ""),
                     ceiling_ft=ceiling, under_ft=min(floors))
            return min(ceiling, self.BELOW_CLASS_A_FT)
        return round((elevation + self.VFR_CEILING_AGL_FT) / 500.0) * 500.0

    def _airspace_class(self, airport, station) -> str:
        """Which class of airspace a facility owns.

        The published volume where there is one (``NavDB.airspace_over``):
        the field's own surface volume for a tower, the field's B or C for
        its radar positions. Where nothing is published, the size of the
        field and the position being worked, which gets the common cases
        right: a tower-only field is a Delta, a radar field is a Charlie, and
        a large scheduled airport is a Bravo. The name it turns into is the
        region's own (``airspace()``).
        """
        if airport is not None:
            own = [v for v in self.navdb.airspace_over(airport.lat, airport.lon)
                   if v.airport == airport.ident]
            radar = station is not None and station.position in ("APP", "DEP", "CTR")
            wanted = [v for v in own if v.cls in ("BC" if radar else "BCD")]
            if wanted:
                return min(v.cls for v in wanted)
        rank = int(getattr(airport, "size_rank", 0) or 0)
        if station is not None and station.position in ("APP", "DEP", "CTR"):
            return "B" if rank >= 3 else "C"
        return "B" if rank >= 3 else "D"

    def _pattern_direction(self, session, parsed, p, airport=None,
                           runway: str = "") -> str:
        """Which way round the circuit goes, as this language says it.

        The pilot's own word first; then the side published for the runway
        end (``NavDB.circuit_data``: the FAA's, or a hand-kept row); then
        whatever the session had, which starts left-hand. Every circuit was
        left-hand, so Centennial's 35R -- right-hand -- was joined on the
        wrong side of the runway.
        """
        asked = (parsed.get("pattern_direction") if parsed else "") or ""
        if asked in ("left", "right"):
            session.pattern_direction = asked
            session.pattern_direction_said = True
        elif not session.pattern_direction_said and airport is not None and runway:
            published = self.navdb.circuit_data(airport.ident, runway)
            if published is not None and published.side:
                session.pattern_direction = published.side
        return p.side(session.pattern_direction)

    def circuit_height(self, airport, runway: str) -> tuple[float, str]:
        """The circuit height above the field, and where it came from: the
        circuit files, the simulator's runway data, or the default."""
        from .circuit import DEFAULT_HEIGHT_AGL_FT

        if airport is None:
            return DEFAULT_HEIGHT_AGL_FT, "default"
        published = self.navdb.circuit_data(airport.ident, runway)
        if published is not None and published.height_ft_agl:
            return float(published.height_ft_agl), published.source or "file"
        sim = self.sim_patterns.get(airport.ident, {}).get(normalize_runway(runway))
        if sim:
            return float(sim), "simulator"
        return DEFAULT_HEIGHT_AGL_FT, "default"

    # ------------------------------------------------------------------
    # ground
    # ------------------------------------------------------------------

    # Requests that only make sense with the wheels on the ground. Answering
    # any of them from the air used to produce a taxi route for an aircraft at
    # cruise, which is the ground half of the same mistake the tower made.
    #
    # Checking in and asking for circuits are on the list because both reach
    # the taxi code by another route: a check-in on the ground frequency is a
    # request to taxi, and it is still one when the aeroplane is at six
    # thousand feet -- at which point it is not.
    _GROUND_ONLY = (
        Intent.REQUEST_PUSHBACK, Intent.REQUEST_TAXI,
        Intent.REQUEST_TAXI_TO_PARKING, Intent.REQUEST_CLEARANCE,
        Intent.CHECK_IN, Intent.REQUEST_PATTERN_WORK,
    )

    def _ground(self, parsed, session, station, weather, p, state) -> Reply:
        aircraft = session.aircraft

        if parsed.intent in self._GROUND_ONLY and _airborne(state):
            # Being told "you are airborne" and nothing else leaves the pilot
            # on a frequency that cannot help them, so whoever can takes the
            # call -- and that is radar, not the tower. An aeroplane at cruise
            # asking to taxi is not in the circuit, and the tower could do no
            # more for it than ground can.
            sent = self._send_on(session, station, p, "DEP", "APP")
            if sent is not None:
                return sent
            return Reply(p.already_airborne(aircraft), station)

        if parsed.intent is Intent.REQUEST_PUSHBACK:
            return Reply(p.pushback_approved(aircraft), station,
                         ground="pushback")

        if parsed.intent is Intent.REQUEST_TAXI:
            # The ground handling has the aeroplane. Telling it to taxi now
            # would be an instruction it cannot follow.
            reason = self._stuck_on_the_stand()
            if reason:
                return Reply(p.hold_position(aircraft) + f" You are {reason}.",
                             station)

        if parsed.intent is Intent.REQUEST_VFR_DEPARTURE:
            return self._issue_vfr_release(session, station, p, parsed, weather)

        if parsed.intent is Intent.REQUEST_CLEARANCE:
            # A VFR flight has nothing to collect from delivery. Sending it
            # there to be read an airways clearance it did not file is the
            # wrong half of the phrasebook and a wasted frequency change.
            if not session.ifr:
                return self._issue_vfr_release(session, station, p, parsed, weather)
            # A clearance is not a taxi instruction, and answering one with the
            # other is the ground half of the mistake the tower used to make.
            # Where the field has a delivery position, that is whose job it is.
            delivery = self.controller_at(station.ident, "DEL")
            if delivery and station_key(delivery) != station_key(station):
                return Reply(
                    p.handoff(aircraft,
                              self._station_name(delivery, p, short=True),
                              delivery.mhz),
                    station, handoff_to=delivery,
                )
            # No delivery here, so ground issues it, which is what a small
            # field actually does.
            return self._issue_clearance(session, station, weather, p)

        if parsed.intent in (Intent.REQUEST_TAXI, Intent.CHECK_IN):
            if session.phase in (Phase.LANDED, Phase.TAXI_IN):
                return self._issue_taxi_in(session, station, p, state)
            return self._issue_taxi(session, station, weather, p, state)

        if parsed.intent is Intent.REQUEST_TAXI_TO_PARKING:
            return self._issue_taxi_in(session, station, p, state)

        if parsed.intent is Intent.REQUEST_PATTERN_WORK:
            # Circuits are the tower's to approve; ground's job is to get the
            # aircraft to the runway, which is what it asked for in effect.
            session.pattern_work = True
            return self._issue_taxi(session, station, weather, p, state)

        if parsed.intent is Intent.REQUEST_TAKEOFF:
            tower = self.controller_at(station.ident, "TWR",
                                       runway=session.departure_runway)
            if tower:
                return Reply(
                    p.handoff(aircraft, self._station_name(tower, p, short=True), tower.mhz),
                    station, handoff_to=tower,
                )
            return Reply(p.roger(aircraft), station)

        if parsed.intent is Intent.RADIO_CHECK:
            return Reply(p.radio_check(aircraft), station)

        if parsed.intent in (Intent.ACKNOWLEDGE, Intent.AFFIRMATIVE):
            return Reply("", station, silent=True)

        return self._not_understood(session, station, p, state)

    # Set by the engine: ``ident -> GroundLayout | None``, the simulator's
    # stands and taxiways for a field. It never blocks -- a layout still being
    # read is None, and asking is what starts the read -- and without it, or
    # without an answer, the taxi is the invented one it always was.
    ground_layout = None

    def _layout_for(self, ident: str):
        if self.ground_layout is None:
            return None
        try:
            return self.ground_layout(ident) or None
        except Exception:
            log.debug("the ground layout for %s raised", ident, exc_info=True)
            return None

    def _ground_traffic(self, layout):
        """Every other aeroplane on the ground, as (x, z, radius) in the
        layout's plane, and the stands the traffic has handed out."""
        if self.traffic is None:
            return [], set()
        try:
            return self.traffic.ground_occupancy(layout)
        except Exception:
            log.debug("could not ask the traffic where it is", exc_info=True)
            return [], set()

    def _plan_taxi(self, session, station, state, *, inbound: bool):
        """The taxi on the simulator's own taxiways, or None and the reason
        it is the invented one instead (see :mod:`wilcoatc.atc.taxi`)."""
        from . import taxi as taxiplan

        layout = self._layout_for(station.ident)
        if layout is None:
            return None, "the simulator has not described this airport"
        if state is None or not getattr(state, "has_position", False):
            return None, "no aircraft position"
        lines = taxiplan.runway_lines(layout, self.navdb.runways(station.ident))
        occupied, taken = self._ground_traffic(layout)
        if inbound:
            aircraft = session.aircraft
            # An airline stand for an airline flight, as the pilot said.
            airliner = session.profile.airliner
            return taxiplan.plan_arrival(
                layout, state.latitude, state.longitude, lines,
                airliner=airliner, key=aircraft.callsign.written,
                occupied=occupied, taken=taken,
                runway=session.arrival_runway or session.touchdown_runway)
        return taxiplan.plan_departure(
            layout, state.latitude, state.longitude, session.departure_runway,
            lines, avoid=taxiplan.avoid_near(layout, occupied))

    def _safely_plan(self, session, station, state, *, inbound: bool):
        # Scenery is data somebody else wrote, and a layout nobody tested
        # against is the likeliest place for a surprise. A pilot who asked
        # to taxi gets the invented route rather than silence.
        try:
            return self._plan_taxi(session, station, state, inbound=inbound)
        except Exception as error:
            log.info("planning the taxi at %s raised", station.ident,
                     exc_info=True)
            return None, f"the taxi planner raised {type(error).__name__}"

    @staticmethod
    def _keep_the_rest(session, plan, *, inbound: bool) -> None:
        """A cut taxi's second half, for when the aeroplane gets there."""
        session.taxi_rest = list(plan.rest)
        session.taxi_split_at = plan.split_at if plan.rest else None
        session.taxi_rest_inbound = inbound

    # How close the aeroplane has to be to where it was told to hold short
    # for the rest of a cut taxi to be given. Chosen: a junction's width.
    REST_OF_TAXI_M = 60.0

    def rest_of_taxi(self, session, station, p, state) -> Reply | None:
        """The second half of a taxi cut in two, once the aeroplane is at
        the taxiway it was told to hold short of: the rest of the route to
        the runway, or to the stand."""
        if not session.taxi_rest or session.taxi_split_at is None:
            return None
        if state is None or not state.on_ground or not state.has_position:
            return None
        from ..navdata.build import haversine_nm

        lat, lon = session.taxi_split_at
        apart_m = haversine_nm(state.latitude, state.longitude, lat, lon) * 1852.0
        if apart_m > self.REST_OF_TAXI_M:
            return None
        rest = list(session.taxi_rest)
        session.taxi_rest, session.taxi_split_at = [], None
        session.taxi_route = list(session.taxi_route) + rest
        said = say_words(station.ident, rest)
        decision("taxi_route", airport=station.ident, event="the rest",
                 route=rest, inbound=session.taxi_rest_inbound)
        if session.taxi_rest_inbound:
            text = p.taxi_to_parking(session.aircraft, "", said, p.PARKING,
                                     abbreviated=True)
            return Reply(text, station, instruction=session.remember(
                Instruction("taxi_in", station_key(station), text, {},
                            readback_required=False)))
        runway = session.departure_runway
        text = p.taxi_to_runway(session.aircraft, "", runway, said,
                                hold_short=session.hold_short_of or "",
                                abbreviated=True)
        values: dict[str, Any] = {"runway": runway, "route": rest}
        if session.hold_short_of:
            values["hold_short"] = session.hold_short_of
        return Reply(text, station, instruction=session.remember(
            Instruction("taxi", station_key(station), text, values)))

    @staticmethod
    def _keep_taxi(session, plan) -> None:
        """The route on the session, so the map, the readback and whatever is
        said next use the one that was given."""
        session.taxi_nodes = list(plan.nodes) if plan else []
        session.taxi_path = list(plan.points) if plan else []
        session.taxi_crossings = list(plan.crossings) if plan else []
        session.taxi_stand = (plan.stand.number
                              if plan and plan.stand is not None else None)

    def _issue_taxi(self, session, station, weather, p, state=None) -> Reply:
        airport = self.navdb.airport(station.ident)
        runway = session.departure_runway or self._departure_runway(airport, weather)
        session.departure_runway = runway

        plan, why = self._safely_plan(session, station, state, inbound=False)
        self._keep_taxi(session, plan)
        hold_at = ""
        if plan is not None:
            # Five taxiways at most; a longer route is cut, held short of
            # the sixth, and the rest is given there (SurfaceWatch).
            route = plan.first
            hold_at = plan.hold_at
            self._keep_the_rest(session, plan, inbound=False)
            # Every runway the route crosses has to be held short of, and the
            # first is the one the pilot meets first. The rest are for after
            # it has been crossed.
            hold_short = plan.hold_short
            decision("taxi_route", airport=station.ident, runway=runway,
                     source="simulator", route=route, crossings=plan.crossings)
        else:
            route = taxi_route(station.ident, runway)
            # Hold short of a runway that is not the assigned one, when the
            # field has more than one. Crossing clearances are issued
            # separately.
            hold_short = self._hold_short_candidate(airport, runway)
            decision("taxi_route", airport=station.ident, runway=runway,
                     source="invented", route=route, why=why)
        session.taxi_route = route
        session.hold_short_of = hold_short

        altimeter = (
            weather.altimeter_inhg if p.dialect == "faa" else weather.qnh_hpa
        )
        said = say_words(station.ident, route)
        text = p.taxi_to_runway(
            session.aircraft,
            self._station_name(station, p) if not session.established_with(station) else "",
            runway, said, hold_short=hold_short,
            altimeter=altimeter if not session.clearance_issued else 0.0,
            abbreviated=session.established_with(station),
            hold_short_taxiway=(say_words(station.ident, [hold_at])[0]
                                if hold_at else ""),
        )
        session.phase = Phase.TAXI_OUT

        values: dict[str, Any] = {"runway": runway}
        if hold_short:
            values["hold_short"] = hold_short
        if route:
            # Not a readback item, but the first officer's readback is built
            # from these values, and without the route it read the runway
            # back in the hold-short frame -- "hold short runway one nine,
            # hold short runway zero one" -- which the controller refused,
            # four times, on the first IFR reference flight.
            values["route"] = list(route)
        instruction = session.remember(
            Instruction("taxi", station_key(station), text, values)
        )
        return Reply(text, station, instruction=instruction)

    def _issue_taxi_in(self, session, station, p, state=None) -> Reply:
        plan, why = self._safely_plan(session, station, state, inbound=True)
        self._keep_taxi(session, plan)
        hold_short = ""
        hold_at = ""
        destination = p.PARKING
        if plan is not None:
            route = plan.first
            hold_at = plan.hold_at
            self._keep_the_rest(session, plan, inbound=True)
            hold_short = plan.hold_short
            if plan.stand.number:
                destination = p.stand(plan.stand.number, plan.stand.gate)
            decision("taxi_route", airport=station.ident, inbound=True,
                     source="simulator", route=route, stand=plan.stand.number,
                     crossings=plan.crossings)
        else:
            route = taxi_route(station.ident, session.arrival_runway or "01",
                               inbound=True)
            decision("taxi_route", airport=station.ident, inbound=True,
                     source="invented", route=route, why=why)
        session.taxi_route = route
        session.hold_short_of = hold_short
        text = p.taxi_to_parking(
            session.aircraft,
            self._station_name(station, p) if not session.established_with(station) else "",
            say_words(station.ident, route), destination,
            hold_short=hold_short,
            abbreviated=session.established_with(station),
            hold_short_taxiway=(say_words(station.ident, [hold_at])[0]
                                if hold_at else ""),
        )
        session.phase = Phase.TAXI_IN
        # A taxi to the stand is not read back, but a hold-short in it is:
        # FAA AIM 4-4-7 and ICAO Doc 4444 4.5.7.5.1 both name it, and a pilot
        # who has not said it back has not agreed to stop.
        values = {"hold_short": hold_short} if hold_short else {}
        instruction = session.remember(
            Instruction("taxi_in", station_key(station), text, values,
                        readback_required=bool(hold_short))
        )
        return Reply(text, station, instruction=instruction)

    def _hold_short_candidate(self, airport, runway: str) -> str:
        """A runway the invented taxi route would plausibly cross.

        Only for the invented route. Where the route comes from the
        simulator's taxiways the hold-short is the first runway it really
        crosses, and a runway it does not cross is never named."""
        if airport is None:
            return ""
        ends = self.navdb.runway_ends(airport.ident)
        target = normalize_runway(runway)
        others = sorted(
            {normalize_runway(e[0]) for e in ends} - {target}
        )
        if not others:
            return ""
        # Only pick a runway that is not the reciprocal of the assigned one.
        target_number = int(target[:2]) if target[:2].isdigit() else 0
        for candidate in others:
            # Not a helicopter strip: an "H" end (de Gaulle's 08H/26H, grass,
            # 440 m) is not a runway a jet taxis across, and the readback
            # "hold short runway two six Hotel" was refused as unreadable on
            # the LFPG-EGLL reference flight.
            if not candidate[:2].isdigit() or candidate.endswith("H"):
                continue
            number = int(candidate[:2])
            if abs(((number - target_number) * 10 + 540) % 360 - 180) > 30:
                return candidate
        return ""

    # ------------------------------------------------------------------
    # tower
    # ------------------------------------------------------------------

    def _tower(self, parsed, session, station, weather, p, state) -> Reply:
        aircraft = session.aircraft
        airport = self.navdb.airport(station.ident)

        # Everything a tower says depends on whether the aircraft is on the
        # ground, so that is settled before the intent is looked at. Without
        # it a departure that called back after rotating was answered with a
        # takeoff clearance, which is the one instruction that cannot possibly
        # apply to an aircraft already in the air.
        airborne = _airborne(state)

        # An IFR arrival checking in on the final (the IFR plan, Phase 5
        # (e)): "established on the localizer" was not understood here and
        # was answered "expecting inbound for landing, or field in sight".
        if (self.realism.tower_continue and session.ifr and airborne
                and not session.phase.outbound
                and parsed.intent in (Intent.REPORT_ESTABLISHED,
                                      Intent.CHECK_IN)):
            return self._tower_check_in_on_final(parsed, session, station,
                                                 weather, p, state)

        if parsed.intent is Intent.REQUEST_TAKEOFF:
            if airborne:
                return Reply(p.already_airborne(aircraft), station)
            return self._issue_takeoff(session, station, weather, p, parsed)

        if parsed.intent is Intent.REQUEST_PATTERN_WORK:
            session.pattern_work = True
            if not airborne:
                return self._issue_takeoff(session, station, weather, p, parsed)
            return self._work_the_circuit(session, station, weather, p, parsed,
                                          state)

        if parsed.intent is Intent.REQUEST_TRANSITION:
            return self._issue_transition(session, station, weather, p, parsed,
                                          state)

        # A field whose tower is its only position releases its own VFR
        # departures; the request fell through to the list of calls the
        # tower was "expecting".
        if parsed.intent is Intent.REQUEST_VFR_DEPARTURE and not airborne:
            return self._issue_vfr_release(session, station, p, parsed, weather)

        if parsed.intent is Intent.CHECK_IN and session.phase.outbound:
            if not airborne:
                return self._issue_takeoff(session, station, weather, p, parsed)
            # Already off the ground and still on the tower frequency: what
            # they need is the next controller, not a clearance to do the
            # thing they have already done.
            return self._hand_off_departure(session, station, p)

        if parsed.intent in (Intent.REQUEST_LANDING, Intent.POSITION_REPORT,
                             Intent.REPORT_FIELD_IN_SIGHT):
            if (not airborne and session.phase.outbound
                    and session.phase is not Phase.PREFLIGHT):
                return Reply(p.roger(aircraft), station)
            # A VFR arrival is sequenced round the circuit, not vectored onto
            # final. "Continue approach" to an aeroplane at eight miles VFR is
            # IFR phraseology used on a flight that is not flying one.
            if session.visual:
                return self._work_the_circuit(session, station, weather, p,
                                              parsed, state)
            # Airborne, so the phase tracker's opinion is beside the point: an
            # aircraft in the circuit is outbound and inbound at once, and it
            # is asking to land.
            return self._issue_landing(session, station, weather, p, parsed, state)

        if parsed.intent is Intent.CHECK_IN:
            if session.visual and airborne:
                return self._work_the_circuit(session, station, weather, p,
                                              parsed, state)
            return self._issue_landing(session, station, weather, p, parsed, state)

        if parsed.intent in (Intent.GOING_AROUND, Intent.MISSED_APPROACH):
            if not airborne:
                return Reply(p.already_on_the_ground(aircraft), station)
            return self.go_around(session, station, weather, p, airport)

        # A clearance asked of the tower: whoever delivers them here, or the
        # tower itself where nobody else does. It had no case at all, and the
        # pilot was told the tower was "expecting ready for departure".
        if parsed.intent is Intent.REQUEST_CLEARANCE:
            if airborne:
                return Reply(p.already_airborne(aircraft), station)
            sent = self._send_on(session, station, p, "DEL", "GND")
            if sent is not None:
                return sent
            return self._delivery(parsed, session, station, weather, p, state)

        if parsed.intent is Intent.REQUEST_TAXI_TO_PARKING:
            if airborne:
                # An aeroplane in the air does not taxi anywhere, and telling
                # it to call Ground is telling it to call somebody who cannot
                # do anything for it until it has landed.
                return Reply(p.already_airborne(aircraft), station)
            ground = self.controller_at(station.ident, "GND")
            if ground:
                return Reply(
                    p.handoff(aircraft, self._station_name(ground, p, short=True), ground.mhz),
                    station, handoff_to=ground,
                )
            # A field whose tower also works the ground taxis them itself,
            # rather than asking them to say again what they clearly said.
            return self._issue_taxi_in(session, station, p, state)

        if parsed.intent is Intent.RADIO_CHECK:
            return Reply(p.radio_check(aircraft), station)

        if parsed.intent in (Intent.ACKNOWLEDGE, Intent.AFFIRMATIVE):
            return Reply("", station, silent=True)

        return self._not_understood(session, station, p, state)

    def _hand_off_departure(self, session, station, p) -> Reply:
        """Send an airborne aircraft on to whoever works departures."""
        nxt = (self.controller_at(station.ident, "DEP")
               or self.controller_at(station.ident, "APP"))
        if nxt is None:
            return Reply(p.roger(session.aircraft), station)
        text = p.handoff(session.aircraft,
                         self._station_name(nxt, p, short=True), nxt.mhz)
        instruction = session.remember(
            Instruction("handoff", station_key(station), text,
                        {"frequency": nxt.mhz})
        )
        return Reply(text, station, instruction=instruction, handoff_to=nxt)

    # ------------------------------------------------------------------
    # working the user into the traffic
    # ------------------------------------------------------------------

    # Set by the engine when something is reporting the ground handling. A
    # controller that can see the tug does not clear the aeroplane to taxi
    # while it is still attached to it.
    ground = None

    def _stuck_on_the_stand(self) -> str:
        """Why the aeroplane cannot move, or an empty string if it can."""
        if self.ground is None:
            return ""
        try:
            state = self.ground.state
            return "" if state.can_taxi else state.why_not()
        except Exception:
            return ""

    # Set by the engine when the traffic injector is running. Without it every
    # method below falls back to the way it worked when the frequency was
    # empty, which is what the tests and a traffic-free session get.
    traffic = None

    def _traffic_ahead(self, arriving: bool):
        """The AI flights in front of the user, nearest first."""
        if self.traffic is None:
            return []
        try:
            return self.traffic.traffic_ahead(arriving)
        except Exception:
            return []

    # How close an arrival has to be before nobody else may be cleared to
    # go. Four miles is about ninety seconds, which is not enough to line up,
    # roll and be airborne with anything to spare.
    RUNWAY_CONFLICT_NM = 4.0

    # A departure with an arrival between RUNWAY_CONFLICT_NM and this on the
    # final goes now or not at all: "cleared for immediate takeoff".
    IMMEDIATE_NM = 6.0
    # An arrival still told to continue, or cleared behind a departure that
    # has not gone, is sent round inside this of the threshold.
    GO_AROUND_NM = 0.6

    def landing_on_top(self, airport=None, runway: str = ""):
        """The arrival too close for anybody else to be sent down the runway:
        the invented traffic's, or -- given the field and the runway --
        anything in the picture on that final (atc.occupancy)."""
        mine = next((f for f in self._traffic_ahead(True)
                     if f.phase == "final"
                     and f.distance_nm <= self.RUNWAY_CONFLICT_NM), None)
        if mine is not None or airport is None or not runway \
                or not self.realism.conditional_clearances:
            return mine
        seen = self.arrivals_on_final(airport, runway, self.RUNWAY_CONFLICT_NM)
        return seen[0] if seen else None

    def runway_available(self, airport=None, runway: str = "") -> bool:
        """Whether the runway is free enough to clear somebody for takeoff.

        Public because the tower has to answer this question twice: once when
        the pilot asks, and again on its own, for an aeroplane it has already
        put on the runway and told to wait. See
        :class:`~wilcoatc.atc.departure.DepartureWatch`.
        """
        return (not self._runway_blocked(airport, runway)
                and self.landing_on_top(airport, runway) is None)

    def _runway_blocked(self, airport=None, runway: str = "") -> bool:
        """Whether somebody else is on the runway right now: the invented
        traffic's reservation, or anything in the picture on the strip."""
        if self.traffic is not None:
            try:
                if self.traffic.runway_is_busy(time.monotonic()):
                    return True
            except Exception:
                pass
        return bool(self.runway_occupants(airport, runway))

    def arrivals_ahead(self, airport, runway: str, state) -> list:
        """Arrivals on the same final nearer its threshold than this
        flight: the landing clearance waits for them (Phase 5 (e))."""
        if not self.realism.tower_continue or state is None \
                or not getattr(state, "has_position", False):
            return []
        final = final_for(self.navdb, getattr(airport, "ident", ""), runway)
        if final is None:
            return []
        mine = final.distance(state)
        return [one for one in self.arrivals_on_final(
                    airport, runway, self.LANDING_CLEARANCE_NM + 5.0)
                if one.distance_nm < mine - 0.3]

    def runway_occupants(self, airport, runway: str) -> list:
        """Everybody on ``runway``'s strip (atc.occupancy.on_the_runway)."""
        if airport is None or not runway or not self.realism.conditional_clearances:
            return []
        from .occupancy import on_the_runway

        return on_the_runway(self.navdb, airport, runway, self.contacts())

    def arrivals_on_final(self, airport, runway: str, within_nm: float) -> list:
        """The arrivals on ``runway``'s final inside ``within_nm``."""
        if airport is None or not runway:
            return []
        from .occupancy import on_final

        return on_final(self.navdb, airport, runway, self.contacts(), within_nm)

    def landing_or_withheld(self, session, station, weather, p, airport,
                            runway: str, *, station_name: str = "",
                            sequence: str = "", caution: str = "",
                            state=None) -> Reply | None:
        """What the tower says instead of a plain landing clearance while
        somebody is on the runway, or None when it is clear.

        In daylight in the UK and the ICAO and German regions, "land after"
        the aeroplane on it (CAP 413). In the US, a departure still on the
        runway gets the anticipated-separation clearance. Otherwise, and for
        circuits, "continue approach": the clearance comes when the runway is
        free, and a go-around if it is not free in time
        (``ArrivalWatch``). Every one of them is logged with what was on the
        runway and where.
        """
        if not self.realism.conditional_clearances:
            return None
        from .svfr import is_night
        from .traffic import runway_traffic

        occupants = self.runway_occupants(airport, runway)
        if not occupants:
            session.landing_withheld = False
            return None
        first = occupants[0]
        language = language_of(p)
        aircraft = session.aircraft
        abbreviated = bool(session.established_with(station))
        region = getattr(p, "region", "icao")
        final = final_for(self.navdb, airport.ident, runway)
        departing = bool(final is not None and abs(((first.contact.heading
                         - final.course) + 540.0) % 360.0 - 180.0) <= 30.0
                         and first.along_nm < 0.5 * max(0.3, runway_length_nm(
                             self.navdb, airport.ident, runway)))
        log_fields = dict(airport=airport.ident, runway=runway,
                          occupant=first.written or None,
                          occupant_type=first.contact.type_code or None,
                          along_nm=round(first.along_nm, 2), moving=first.moving,
                          observed=first.contact.observed,
                          threshold_nm=(round(final.distance(state), 2)
                                        if final is not None and state is not None
                                        and getattr(state, "has_position", False)
                                        else None))
        if (region in ("uk", "icao", "de") and not session.pattern_work
                and not is_night(airport.lat, airport.lon, region)):
            text = p.land_after(aircraft, runway, weather,
                                traffic=first.describe(language),
                                station=station_name, abbreviated=abbreviated,
                                caution=caution)
            decision("landing_clearance", said="land_after", **log_fields)
            return self._landing_given(session, station, runway, text)
        if region == "faa" and departing and not session.pattern_work:
            text = p.cleared_to_land(
                aircraft, runway, weather, station=station_name,
                sequence=sequence, abbreviated=abbreviated,
                caution=", ".join(c for c in (caution, p.departing_prior()) if c))
            session.landing_anticipated = first.written
            decision("landing_clearance", said="anticipated", **log_fields)
            return self._landing_given(session, station, runway, text)
        session.landing_withheld = True
        session.phase = Phase.LANDING
        text = p.continue_approach(aircraft, runway_traffic(language))
        decision("landing_clearance", said="continue", **log_fields)
        instruction = session.remember(
            Instruction("continue_approach", station_key(station), text, {},
                        readback_required=False))
        return Reply(text, station, instruction=instruction)

    def _landing_given(self, session, station, runway, text) -> Reply:
        session.cleared_landing = True
        session.landing_withheld = False
        session.phase = Phase.LANDING
        session.allow_runway(runway)
        instruction = session.remember(
            Instruction("landing_clearance", station_key(station), text,
                        {"runway": runway}))
        return Reply(text, station, instruction=instruction)

    def order_go_around(self, session, station, weather, p, airport,
                        why: str) -> Reply:
        """The tower sending an arrival round because the runway is not
        clear: "go around, traffic on the runway", then what to fly (the
        circuit again, or the missed approach)."""
        from .traffic import runway_traffic

        occupants = self.runway_occupants(airport, session.arrival_runway)
        decision("go_around_ordered", airport=getattr(airport, "ident", ""),
                 runway=session.arrival_runway, why=why,
                 occupant=occupants[0].written if occupants else None)
        session.landing_withheld = False
        session.landing_anticipated = ""
        then = self.go_around(session, station, weather, p, airport, told=True)
        order = p.go_around(session.aircraft,
                            reason=runway_traffic(language_of(p)))
        then.text = f"{order} {then.text}"
        return then

    def _issue_takeoff(self, session, station, weather, p, parsed) -> Reply:
        airport = self.navdb.airport(station.ident)
        runway = (
            session.departure_runway
            or parsed.get("runway")
            or self._departure_runway(airport, weather)
        )
        session.departure_runway = runway

        # A clearance that is still standing is not reissued. Asking twice is
        # usually a pilot who did not hear it, so the runway is repeated, but
        # a second "cleared for takeoff" for a clearance they already hold
        # reads as a new one and is exactly how a runway gets two aircraft.
        if session.cleared_takeoff:
            return Reply(
                p.already_cleared_for_takeoff(session.aircraft, runway), station
            )
        # Below VFR minima nothing VFR takes off without Special VFR, and
        # circuits are not flown in it at all.
        refused = self._below_vmc(session, station, weather, p, airport,
                                  asked="takeoff",
                                  offer=not session.pattern_work)
        if refused is not None:
            return refused

        # The runway is one aeroplane wide. If somebody else is rolling on it,
        # or is about to land on it, the user is lined up and held rather than
        # cleared -- which is the whole of what a tower does with a departure
        # queue, and it is why a takeoff clearance sometimes takes a minute.
        landing = self.landing_on_top(airport, runway)
        blocked = self._runway_blocked(airport, runway)
        if blocked or landing is not None:
            if session.lined_up or session.held_for_landing:
                # Already on the runway and still waiting. Asking again does
                # not make the aeroplane in front of you land any sooner.
                return Reply(p.hold_position(session.aircraft), station)
            if not blocked and self.realism.conditional_clearances:
                return self._held_for_landing(session, station, p, runway,
                                              landing)
            reason = ""
            if landing is not None:
                from .traffic import landing_traffic

                reason = landing_traffic(landing.describe(language_of(p)),
                                         language_of(p))
            text = p.line_up_and_wait(
                session.aircraft, runway, reason=reason,
                station=self._station_name(station, p)
                if not session.established_with(station) else "",
                abbreviated=session.established_with(station),
            )
            session.lined_up = True
            session.allow_runway(runway)
            instruction = session.remember(
                Instruction("line_up_and_wait", station_key(station), text,
                            {"runway": runway})
            )
            return Reply(text, station, instruction=instruction)

        station_name = (
            self._station_name(station, p)
            if not session.established_with(station) else ""
        )
        if session.pattern_work:
            # Staying in the circuit, and coming back to the runway it left
            # from. The circuit's landings were taken from the wind, so
            # circuits asked for on two eight were flown back to three five.
            session.arrival_runway = runway
            # Staying in the circuit, so the clearance says which way round it
            # goes instead of sending the aircraft off on runway heading.
            text = p.closed_traffic_approved(
                session.aircraft, runway,
                self._pattern_direction(session, parsed, p, airport, runway),
                weather, station=station_name,
                abbreviated=session.established_with(station),
            )
        else:
            # An arrival on the final with room for one departure in front of
            # it, and no more: the departure goes now (ICAO Doc 4444 chapter
            # 12, "cleared for immediate take-off"), told why.
            soon = (self.arrivals_on_final(airport, runway, self.IMMEDIATE_NM)
                    if self.realism.conditional_clearances else [])
            traffic = ""
            if soon:
                described = (soon[0].indefinite(language_of(p))
                             or soon[0].describe(language_of(p)))
                traffic = p.traffic_on_final(described, soon[0].distance_nm)
                decision("takeoff", airport=getattr(airport, "ident", ""),
                         runway=runway, said="immediate",
                         arrival=soon[0].written or None,
                         arrival_nm=round(soon[0].distance_nm, 1),
                         observed=soon[0].contact.observed)
            text = p.cleared_for_takeoff(
                session.aircraft, runway, weather,
                caution=self._caution(session, weather, p,
                                      self._departing_airliner(airport)),
                station=station_name,
                after_departure=self._after_takeoff(session, airport, runway, p),
                abbreviated=session.established_with(station),
                traffic=traffic, immediate=bool(soon),
            )
        session.held_for_landing = ""
        session.cleared_takeoff = True
        session.cleared_takeoff_at = time.monotonic()
        session.phase = Phase.TAKEOFF
        session.allow_runway(runway)
        instruction = session.remember(
            Instruction("takeoff_clearance", station_key(station), text,
                        {"runway": runway})
        )
        return Reply(text, station, instruction=instruction)

    def _after_takeoff(self, session, airport, runway: str, p) -> str:
        """What the takeoff clearance says the flight does next.

        A SID cleared is the route, and an ICAO tower says nothing more. A
        US one names an RNAV SID's first fix ("RNAV to MERIT", 7110.65
        4-3-2) when it is a path from the runway, within 15 nm. No SID is
        runway heading, which departure turns on course with a direct.
        """
        if not session.ifr:
            return ""
        if not self.realism.ifr_departure:
            return p.RUNWAY_HEADING
        session.runway_heading = False
        if session.sid:
            first = None
            if airport is not None:
                first = sid_first_fix(self.route, airport.lat, airport.lon)
            said = p.rnav_to(first.ident) if first is not None else ""
            decision("takeoff", airport=getattr(airport, "ident", ""),
                     runway=runway, said="rnav_to" if said else "sid",
                     sid=session.sid,
                     first_fix=first.ident if first is not None else None)
            return said
        session.runway_heading = True
        decision("takeoff", airport=getattr(airport, "ident", ""),
                 runway=runway, said="runway_heading",
                 why="no SID cleared")
        return p.RUNWAY_HEADING

    def _held_for_landing(self, session, station, p, runway, landing) -> Reply:
        """A departure with an arrival too close in front of it.

        Outside the US, lined up behind it: "behind the landing Cessna, line
        up runway two four, behind" (ICAO Doc 4444 chapter 12, conditional
        clearances) -- not "line up and wait" in front of it, which put the
        departure on the runway with an aeroplane landing on it. In the US,
        which gives no conditional clearances, held short. Either way the
        takeoff clearance is the tower's to give once it has landed
        (departure.DepartureWatch).
        """
        language = language_of(p)
        aircraft = session.aircraft
        abbreviated = session.established_with(station)
        name = self._station_name(station, p) if not abbreviated else ""
        traffic = landing.describe(language)
        miles = float(getattr(landing, "distance_nm", 0.0) or 0.0)
        session.held_for_landing = getattr(landing, "written", "") or "traffic"
        if getattr(p, "region", "") == "faa":
            from .traffic import landing_traffic

            held = p.hold_short_instruction(aircraft, runway)
            text = f"{held[:-1]}, {landing_traffic(traffic, language)}."
            kind, values = "hold_short", {"hold_short": runway}
            said = "hold_short"
        else:
            text = p.line_up_behind(aircraft, runway,
                                    p.landing_traffic_named(traffic),
                                    station=name, abbreviated=abbreviated)
            session.lined_up = True
            session.allow_runway(runway)
            kind, values = "line_up_and_wait", {"runway": runway}
            said = "line_up_behind"
        decision("takeoff", airport=station.ident, runway=runway, said=said,
                 arrival=session.held_for_landing, arrival_nm=round(miles, 1))
        instruction = session.remember(
            Instruction(kind, station_key(station), text, values))
        return Reply(text, station, instruction=instruction)

    # ------------------------------------------------------------------
    # the circuit
    # ------------------------------------------------------------------
    #
    # A tower without radar keeps VFR aircraft apart with reporting points and
    # a sequence, not with vectors and an approach clearance. Everything below
    # is that: where to join, what to report next, who to follow, and the
    # clearance at the end of it.

    # Further out than this an aircraft is joining the circuit; inside it, it
    # is already in one and is sequenced rather than given a join.
    PATTERN_JOIN_NM = 6.0
    # Without a leg reported, the aeroplane is on final -- and is cleared to
    # land -- only inside this of the threshold, in the final-approach cone
    # (:mod:`wilcoatc.atc.final`), below PATTERN_FINAL_BELOW_AGL_FT and
    # descending. Three miles on its own cleared an aeroplane on the
    # downwind, one at four thousand feet over the field and one climbing
    # away. 1,500 ft is above any circuit height in use (1,000 ft is the
    # usual light-aircraft figure in the FAA AIM 4-3-3 and the UK and French
    # AIPs) with room for a pilot high on a long final.
    PATTERN_FINAL_NM = 3.0
    PATTERN_FINAL_BELOW_AGL_FT = 1500.0

    def _on_final(self, state, airport, runway: str) -> bool:
        """Whether the aeroplane is on final for ``runway``, by where it is."""
        if state is None or airport is None or not getattr(state, "has_position", False):
            return False
        if getattr(state, "on_ground", False) or not state.descending:
            return False
        elevation = float(getattr(airport, "elev_ft", 0.0) or 0.0)
        above = (getattr(state, "altitude_agl_ft", 0.0)
                 or float(state.altitude_ft or 0.0) - elevation)
        if above > self.PATTERN_FINAL_BELOW_AGL_FT:
            return False
        final = final_for(self.navdb, airport.ident, runway)
        if final is None:
            # No threshold to measure from: the old radius, still with the
            # height and the descent.
            distance = self._distance_to(state, airport)
            return bool(distance) and distance <= self.PATTERN_FINAL_NM
        return final.in_cone(state, self.PATTERN_FINAL_NM)

    # Whether the transmission being answered has written its circuit row,
    # so that :meth:`respond` writes one only for those that have not.
    _circuit_logged = False

    # How far round the circuit an aeroplane has to be behind the one it
    # follows: about a minute at a light aircraft's circuit speed. Closer
    # than that on the downwind, it is extended and its base called later.
    CIRCUIT_SPACING_NM = 1.5
    # On base or final, where there is nothing left to extend: closer than
    # this behind the one ahead, about thirty seconds, it is given an orbit.
    # UNVERIFIED: the figure.
    ORBIT_SPACING_NM = 0.8
    # As many aeroplanes in the circuit as this and an arrival is held at a
    # reporting point until one has landed. UNVERIFIED: the figure; ATZ
    # circuits are commonly run with four to six.
    CIRCUIT_FULL = 4

    def _circuit_at(self, session, airport, runway: str):
        """The circuit to ``runway``, flown the way this session flies it."""
        if airport is None or not runway:
            return None
        if not session.pattern_direction_said:
            published = self.navdb.circuit_data(airport.ident, runway)
            if published is not None and published.side:
                session.pattern_direction = published.side
        height, _source = self.circuit_height(airport, runway)
        return circuit_for(self.navdb, airport.ident, runway,
                           side=session.pattern_direction, height_agl_ft=height)

    @staticmethod
    def _where_in(circuit, state):
        """Where the user is in ``circuit``, or None if that cannot be seen."""
        if circuit is None or state is None:
            return None
        if not getattr(state, "has_position", False) or getattr(state, "on_ground", False):
            return None
        return circuit.locate(state)

    # Set by the engine: ``() -> list[Seen]``, every aeroplane the simulator
    # reports (FSLTL, its own traffic, other AI), the pilot's own excluded.
    sim_traffic = None

    def contacts(self) -> list:
        """Every aeroplane in the controller's picture
        (:class:`~wilcoatc.atc.trafficinfo.Contact`): the invented traffic
        and everything the simulator reports, each once.

        The sequencing and the traffic information read this. They read the
        invented traffic alone, so a pilot flying with FSLTL -- or with the
        invented traffic switched off -- had a controller blind to every
        aeroplane out of the window.
        """
        from .trafficinfo import from_flight, from_seen

        found, ours = [], set()
        if self.traffic is not None:
            for flight in list(getattr(self.traffic, "flights", []) or []):
                if getattr(flight, "done", False):
                    continue
                if getattr(flight, "stuck", False):
                    # Frozen and about to be taken away (TrafficInjector
                    # ._watchdog): nothing may wait on it moving -- no "land
                    # after", no "line up behind" -- and the simulator's copy
                    # of it, read below, is left out with it.
                    ours.add(flight.written)
                    continue
                if (getattr(flight, "observed", False)
                        and getattr(flight, "source", "") != "live"
                        and self.sim_traffic is not None):
                    continue        # read below, from the simulator itself
                # A live one (atc/live.py) is drawn by this program, so the
                # simulator never reports it as somebody else's: it is taken
                # from its strip, and its callsign keeps the simulator's copy
                # of it out below.
                contact = from_flight(flight)
                found.append(contact)
                ours.add(contact.written)
        if self.sim_traffic is not None:
            try:
                seen = self.sim_traffic() or []
            except Exception:
                log.debug("could not read the simulator's traffic", exc_info=True)
                seen = []
            for one in seen:
                if not getattr(one, "positioned", True):
                    continue
                contact = from_seen(one)
                if contact.written and contact.written not in ours:
                    found.append(contact)
                    ours.add(contact.written)
        return found

    def circuit_traffic(self, circuit) -> list:
        """The traffic in ``circuit``, as ``(position, contact)``, the one
        with the least still to fly first -- invented or the simulator's,
        placed by where it is rather than by what it was told."""
        if circuit is None:
            return []
        placed = []
        for contact in self.contacts():
            if contact.on_ground:
                continue
            here = circuit.locate_at(contact.latitude, contact.longitude,
                                     contact.altitude_ft, contact.heading,
                                     contact.vertical_rate_fpm)
            if here.in_circuit:
                placed.append((here, contact))
        placed.sort(key=lambda pair: pair[0].to_go_nm)
        return placed

    def _caution(self, session, weather, p, ahead_type: str = "") -> str:
        """Wake turbulence behind an airliner, a wet runway in rain."""
        from .trafficinfo import caution, needs_wake_caution, runway_wet

        wake = needs_wake_caution(ahead_type, session.aircraft.type_code,
                                  light=not session.profile.airliner)
        wet = runway_wet(weather)
        if wake or wet:
            decision("caution", wake=wake, wet=wet, ahead=ahead_type or None)
        return caution(p, wet=wet, wake=wake)

    def _departing_airliner(self, airport) -> str:
        """An airliner just off this field, climbing out: what a departure
        behind it is cautioned about."""
        from . import speech as sp

        if airport is None:
            return ""
        for contact in self.contacts():
            if contact.on_ground or contact.vertical_rate_fpm < 300.0:
                continue
            if haversine_nm(airport.lat, airport.lon, contact.latitude,
                            contact.longitude) <= 3.0 and sp.is_airliner(contact.type_code):
                return contact.type_code
        return ""

    def circuit_sequence(self, circuit, here, p) -> tuple[str, Any, Any]:
        """``("number two, follow the Cessna on base", position, flight)``
        for the aeroplane immediately ahead, or ``("", None, None)``."""
        if here is None:
            return "", None, None
        ahead = [pair for pair in self.circuit_traffic(circuit)
                 if pair[0].to_go_nm < here.to_go_nm]
        if not ahead:
            return "", None, None
        there, flight = ahead[-1]
        who = p.traffic_on_leg(flight.describe(language_of(p)), p.leg(there.leg))
        return p.sequence_number(len(ahead) + 1, who), there, flight

    def note_circuit(self, session, station, state, said: str) -> None:
        """The arrival-log row for a tower transmission that is not one of
        the circuit's own -- a takeoff clearance, a landing clearance seen
        rather than asked for, a rejoin after a go-around -- so that every
        one the tower makes to a VFR flight carries the leg it saw."""
        if station is None or station.position != "TWR" or not session.visual:
            return
        if state is None or getattr(state, "on_ground", True):
            return
        airport = self.navdb.airport(station.ident)
        here = self._where_in(self._circuit_at(
            session, airport, session.arrival_runway), state)
        self.log_circuit(session, station, here, said=said)

    def log_circuit(self, session, station, here, **fields) -> None:
        """One row of the arrival log for a tower transmission to a VFR
        flight: where the tower saw it, and what it made of that."""
        self._circuit_logged = True
        decision("circuit", airport=station.ident,
                 runway=session.arrival_runway,
                 leg=here.describe() if here is not None else "unseen",
                 to_go_nm=round(here.to_go_nm, 2) if here is not None else None,
                 along_nm=round(here.along_nm, 2) if here is not None else None,
                 out_nm=round(here.out_nm, 2) if here is not None else None,
                 height_agl_ft=(round(here.height_agl_ft)
                                if here is not None else None),
                 inside=here.inside if here is not None else None,
                 **fields)

    def _work_the_circuit(self, session, station, weather, p, parsed,
                          state=None) -> Reply:
        """Answer a VFR aircraft in, or joining, the traffic pattern.

        What the pilot says is checked against where the aeroplane is
        (:mod:`wilcoatc.atc.circuit`) rather than believed: the join is
        chosen from the position, a reported leg two legs away from the one
        the tower can see is queried, and the sequence is the traffic
        actually in the circuit.
        """
        aircraft = session.aircraft
        airport = self.navdb.airport(station.ident)
        runway = (
            session.arrival_runway
            or parsed.get("runway")
            or self._arrival_runway(airport, weather)
        )
        session.arrival_runway = runway
        session.allow_runway("")
        # Joining, so no longer only passing through: the arrival watch
        # leaves a transit alone, and this one has asked to land.
        session.transition_approved = False

        direction = self._pattern_direction(session, parsed, p, airport, runway)
        reported = (parsed.get("pattern_leg") or "").lower()
        first_call = not session.established_with(station)
        station_name = self._station_name(station, p) if first_call else ""
        circuit = self._circuit_at(session, airport, runway)
        here = self._where_in(circuit, state)
        note = {"reported": reported or None}

        # Joining from outside into weather below VFR minima: kept out of
        # the zone, and told Special VFR can be asked for.
        if not reported and not session.cleared_landing and (
                here is None or not here.in_circuit):
            refused = self._below_vmc(session, station, weather, p, airport,
                                      asked="join", outside=True)
            if refused is not None:
                return refused

        # --- a leg the tower can see is not where the aeroplane is ---------
        if reported and here is not None and not agrees(reported, here.leg):
            text = p.confirm_leg(aircraft, p.leg(here.leg))
            self.log_circuit(session, station, here, said="confirm_leg", **note)
            instruction = session.remember(
                Instruction("confirm_position", station_key(station), text, {},
                            readback_required=False))
            return Reply(text, station, instruction=instruction)

        if reported:
            session.pattern_leg = reported
            # The report the tower was waiting for has come, or a later one.
            session.expected_report = ""
            session.expected_report_chased = False
        if session.orbiting and not reported:
            # "Orbit complete": the report asked for after it.
            return self.orbit_complete(session, station, p, circuit, here,
                                       said_by="pilot")
        if session.expected_vrp and self._mentions(parsed, session.expected_vrp, p):
            # Over the point it was asked to report: now it is given the join.
            decision("vrp", airport=station.ident, use="reported",
                     point=session.expected_vrp)
            session.vrp_reported = session.expected_vrp
            session.expected_vrp = ""

        # Who is in front, by where everybody is in the circuit. Without a
        # picture of the user, the arrival stream as it was before.
        if here is not None:
            sequence, there, followed = self.circuit_sequence(circuit, here, p)
        else:
            there = followed = None
            distance = self._distance_to(state, airport)
            ahead = [f for f in self._traffic_ahead(True)
                     if not distance or f.distance_nm < distance]
            sequence = (p.sequence_number(len(ahead) + 1,
                                          ahead[-1].describe(language_of(p)))
                        if ahead else "")
        note["sequence"] = sequence or None

        # --- on base or final: this is the landing clearance ---------------
        on_final = (reported in ("base", "final")
                    or self._on_final(state, airport, runway))
        # On base or final and too close behind the one ahead to land after
        # it: an orbit, ended by the geometry (ArrivalWatch) or the report.
        if (on_final and not session.cleared_landing and self.realism.delays
                and not session.orbiting and followed is not None
                and here is not None and there is not None
                and here.to_go_nm - there.to_go_nm < self.ORBIT_SPACING_NM):
            side = "right" if session.pattern_direction == "left" else "left"
            session.orbiting = side
            session.orbit_turned = 0.0
            session.orbit_track = -1.0
            text = p.orbit(aircraft, p.side(side), sequence)
            self.log_circuit(session, station, here, said="orbit", side=side,
                             behind=followed.written,
                             gap_nm=round(here.to_go_nm - there.to_go_nm, 2),
                             **note)
            instruction = session.remember(
                Instruction("circuit_report", station_key(station), text, {},
                            readback_required=False))
            return Reply(text, station, instruction=instruction)
        if on_final and not session.cleared_landing:
            self.log_circuit(session, station, here, said="clearance", **note)
            return self._clear_the_circuit(
                session, station, weather, p, runway, sequence, station_name,
                caution=self._caution(session, weather, p,
                                      followed.type_code if followed is not None else ""))

        # --- in the circuit: sequence them and give them the next report ---
        if reported in ("overhead", "deadside"):
            # Letting down on the dead side after an overhead join: the
            # downwind is next.
            session.phase = Phase.APPROACH
            text = p.report_position(aircraft, p.join_report())
            session.expected_report = "downwind"
            session.expected_report_chased = False
            self.log_circuit(session, station, here, said="circuit_report",
                             **note)
            instruction = session.remember(
                Instruction("circuit_report", station_key(station), text, {},
                            readback_required=False))
            return Reply(text, station, instruction=instruction)

        if reported in ("downwind", "crosswind", "upwind"):
            session.phase = Phase.APPROACH
            after_downwind, expect_after = p.report_after_downwind()
            if reported != "downwind":
                text = p.report_position(aircraft, p.join_report())
                session.expected_report = "downwind"
            elif followed is not None and here is not None and (
                    here.to_go_nm - there.to_go_nm < self.CIRCUIT_SPACING_NM):
                # Too close behind to turn base where it would: extended, and
                # the base is the tower's to call (ArrivalWatch.circuit).
                text = p.extend_downwind(aircraft, sequence)
                session.base_owed_behind = followed.written
                session.expected_report = ""
            else:
                # The FAA asks for the base, CAP 413 and ICAO for the final,
                # the DGAC for the "dernier virage" (report_after_downwind).
                text = p.report_position(aircraft, after_downwind,
                                         sequence=sequence)
                session.expected_report = expect_after
            session.expected_report_chased = False
            self.log_circuit(session, station, here, said="circuit_report",
                             **note)
            instruction = session.remember(
                Instruction("circuit_report", station_key(station), text, {},
                            readback_required=False))
            return Reply(text, station, instruction=instruction)

        # --- everything else is a join, from where the aeroplane is --------
        session.phase = Phase.APPROACH
        if here is not None:
            join = choose_join(here, circuit, self.region_for(station.ident))
        else:
            distance = self._distance_to(state, airport)
            join = ("straight_in" if distance and distance > self.PATTERN_JOIN_NM * 2
                    else "downwind")
        if join == "overhead":
            # Over the field two thousand feet above it, on the QNH, then
            # down on the dead side and into the circuit (the UK's Standard
            # Overhead Join; France's verticale terrain).
            elevation = float(getattr(airport, "elev_ft", 0.0) or 0.0)
            overhead_ft = math.ceil((elevation + 2000.0) / 100.0) * 100.0
            session.expected_report = "downwind"
            session.expected_report_chased = False
            text = p.join_overhead(aircraft, station_name, runway, overhead_ft,
                                   weather.qnh_hpa, direction)
            self.log_circuit(session, station, here, said="join", join=join,
                             spoken_join=join, overhead_ft=overhead_ft, **note)
            instruction = session.remember(
                Instruction("pattern_entry", station_key(station), text,
                            {"runway": runway,
                             "pattern_direction": session.pattern_direction}))
            return Reply(text, station, instruction=instruction)
        spoken = join
        leg, report, expect = {
            "straight_in": ("", p.REPORT_FINAL, "final"),
            "base": (p.leg("base"), p.REPORT_BASE, "base"),
            "crosswind": (p.leg("crosswind"), p.join_report(), "downwind"),
            "downwind": (p.leg("downwind"), p.join_report(), "downwind"),
        }[spoken]
        session.expected_report = expect
        session.expected_report_chased = False
        # Outside the published points, the first report is the point on the
        # aeroplane's side of the field (the plan's "report November"),
        # wherever the field has points a controller can say. Not in the US,
        # whose charted VFR waypoints are codes nobody says on the radio.
        point = (self.vrp_ahead(airport, state)
                 if p.region != "faa" and here is not None and not here.in_circuit
                 and not session.vrp_reported else None)
        if point is not None and self.realism.delays and circuit is not None:
            busy = len(self.circuit_traffic(circuit))
            if busy >= self.CIRCUIT_FULL:
                # Full: held at the point, and called in from it when there
                # is room (ArrivalWatch).
                session.holding_at = point.name
                session.expected_report = ""
                text = p.hold_at(aircraft, station_name, p.vrp(point.name), runway)
                self.log_circuit(session, station, here, said="hold",
                                 point=point.name, in_circuit=busy, **note)
                instruction = session.remember(
                    Instruction("hold_at", station_key(station), text, {}))
                return Reply(text, station, instruction=instruction)
        if point is not None:
            report = p.vrp(point.name, point.altitude_ft)
            session.expected_vrp = point.name
            session.expected_report = ""
            decision("vrp", airport=airport.ident, use="entry", point=point.name,
                     source=point.source)
        text = p.enter_pattern(
            aircraft, station_name, runway,
            leg=leg, direction=direction, report=report,
            weather=weather if first_call else None,
            sequence=sequence, straight_in=spoken == "straight_in",
        )
        self.log_circuit(session, station, here, said="join", join=join,
                         spoken_join=spoken, **note)
        instruction = session.remember(
            Instruction("pattern_entry", station_key(station), text,
                        {"runway": runway,
                         "pattern_direction": session.pattern_direction}))
        return Reply(text, station, instruction=instruction)

    def orbit_complete(self, session, station, p, circuit, here,
                       said_by: str) -> Reply:
        """Round the orbit: back into the sequence, and the final asked for."""
        session.orbiting = ""
        session.expected_report = "final"
        session.expected_report_chased = False
        sequence = self.circuit_sequence(circuit, here, p)[0] if here is not None else ""
        text = p.report_position(session.aircraft, p.REPORT_FINAL,
                                 sequence=sequence)
        self.log_circuit(session, station, here, said="orbit_complete",
                         by=said_by, turned_deg=round(session.orbit_turned))
        instruction = session.remember(
            Instruction("circuit_report", station_key(station), text, {},
                        readback_required=False))
        return Reply(text, station, instruction=instruction)

    def _clear_the_circuit(self, session, station, weather, p, runway,
                           sequence, station_name, caution: str = "") -> Reply:
        """The clearance at the end of a circuit.

        An aircraft doing circuits gets "cleared for the option", which covers
        the touch-and-go, the low approach and the full stop without the tower
        having to know which one the pilot has decided on. One that is coming
        in to land gets a landing clearance.
        """
        aircraft = session.aircraft
        abbreviated = bool(session.established_with(station))
        withheld = self.landing_or_withheld(
            session, station, weather, p, self.navdb.airport(station.ident),
            runway, station_name=station_name, sequence=sequence,
            caution=caution)
        if withheld is not None:
            return withheld
        if session.pattern_work:
            text = p.cleared_touch_and_go(
                aircraft, runway, weather, sequence=sequence,
                station=station_name, abbreviated=abbreviated, caution=caution)
            kind = "option_clearance"
            session.cleared_option = True
        else:
            text = p.cleared_to_land(
                aircraft, runway, weather, station=station_name,
                sequence=sequence, abbreviated=abbreviated, caution=caution)
            kind = "landing_clearance"
            session.cleared_landing = True
        session.phase = Phase.LANDING
        session.allow_runway(runway)
        instruction = session.remember(
            Instruction(kind, station_key(station), text, {"runway": runway}))
        return Reply(text, station, instruction=instruction)

    @staticmethod
    def _mentions(parsed, name: str, p) -> bool:
        """Whether a transmission names a reporting point, said the way the
        controller said it ("Whiskey") or written ("W")."""
        text = f" {parsed.normalized} "
        said = p.vrp(name).lower()
        return f" {said} " in text or f" {name.lower()} " in text

    def _issue_transition(self, session, station, weather, p, parsed,
                          state=None) -> Reply:
        """A VFR flight asking to cross the airspace on its way past.

        Approved with a ceiling and a reporting point, which is the shape of
        the instruction in every state. Refused while the runway is occupied,
        because a tower with an aeroplane on the pavement has nothing to spare
        for somebody crossing overhead, and a controller that can only ever
        say yes is not a controller.
        """
        aircraft = session.aircraft
        airport = self.navdb.airport(station.ident)
        airspace = p.airspace(self._airspace_class(airport, station))
        first_call = not session.established_with(station)

        refused = self._below_vmc(session, station, weather, p, airport,
                                  asked="transit", outside=True, offer=False)
        if refused is not None:
            return refused

        if station.position == "TWR" and self._runway_blocked():
            text = p.remain_clear_of_airspace(
                aircraft, airspace,
                self._station_name(station, p) if first_call else "")
            instruction = session.remember(
                Instruction("remain_clear", station_key(station), text, {},
                            readback_required=False))
            return Reply(text, station, instruction=instruction)

        ceiling = parsed.get("altitude_ft") or self._vfr_ceiling(airport)
        session.vfr_ceiling_ft = float(ceiling)
        session.transition_approved = True
        if self._airspace_class(airport, station) == "B":
            # Cleared through a Bravo is cleared into it (FAA JO 7110.65
            # chapter 7): the airspace watch lets the aeroplane in.
            session.cleared_bravo = airport.ident
        route = ""
        if state is not None and getattr(state, "has_position", False):
            point = self.vrp_toward(
                airport, bearing_deg(airport.lat, airport.lon,
                                     state.latitude, state.longitude),
                departing=False) if airport is not None else None
            if point is not None:
                # French says "transit autorisé par Sierra Echo"; the
                # preposition is the phrase's own.
                route = (p.vrp(point.name) if language_of(p) == "fr"
                         else f"via {p.vrp(point.name)}")
        text = p.cleared_through_airspace(
            aircraft, airspace, altitude_ft=ceiling, route=route,
            report=p.clear_of(airspace),
            transition_altitude=self._transition_altitude(airport),
        )
        instruction = session.remember(
            Instruction("transition", station_key(station), text,
                        {"vfr_ceiling_ft": float(ceiling)}))
        return Reply(text, station, instruction=instruction)

    # How close an arrival has to be before a landing clearance is issued.
    # Further out than this a tower says "continue approach" and clears the
    # aircraft later, because a clearance given at forty miles has to be
    # cancelled the moment anything changes.
    LANDING_CLEARANCE_NM = 12.0

    def _tower_check_in_on_final(self, parsed, session, station, weather, p,
                                 state) -> Reply:
        """The landing clearance if it can be given now; else "runway 27
        center, continue, I'll call your clearance", and the arrival watch
        clears it once the runway is seen empty and nobody is ahead on the
        final (occupancy.py, through landing_or_withheld)."""
        airport = self.navdb.airport(station.ident)
        runway = session.arrival_runway or self._arrival_runway(airport, weather)
        session.arrival_runway = runway
        ahead = self.arrivals_ahead(airport, runway, state)
        if ahead and not session.cleared_landing:
            # Somebody ahead on the same final: the clearance waits for them
            # (arrival.py _runway_owed gives it once nobody is).
            session.landing_withheld = True
            session.phase = Phase.LANDING
        else:
            reply = self._issue_landing(session, station, weather, p, parsed,
                                        state)
            if reply.instruction is None \
                    or reply.instruction.kind != "continue_approach":
                return reply
        text = p.tower_continue(
            session.aircraft, runway,
            station=(self._station_name(station, p)
                     if not session.established_with(station) else ""),
            # Its place behind whoever is ahead on the same final: "number
            # two" behind one (live traffic, Phase 5).
            sequence=p.sequence_number(len(ahead) + 1) if ahead else "")
        decision("tower", event="continue", runway=runway,
                 station=station.callsign,
                 ahead=ahead[0].contact.written if ahead else None)
        instruction = session.remember(Instruction(
            "continue_approach", station_key(station), text, {},
            readback_required=False))
        return Reply(text, station, instruction=instruction)

    def _issue_landing(self, session, station, weather, p, parsed,
                       state=None) -> Reply:
        airport = self.navdb.airport(station.ident)
        runway = (
            session.arrival_runway
            or parsed.get("runway")
            or self._arrival_runway(airport, weather)
        )
        session.arrival_runway = runway

        if state is not None and getattr(state, "on_ground", False):
            # Nothing on the ground can be cleared to land, whatever it asked
            # for. Saying so is more use than a clearance it cannot fly.
            return Reply(p.already_on_the_ground(session.aircraft), station)

        if session.cleared_landing:
            return Reply(
                p.already_cleared_to_land(session.aircraft, runway), station
            )

        distance = self._distance_to(state, airport)
        if distance > self.LANDING_CLEARANCE_NM or (
                state is not None and not self.re_established(session, airport, state)):
            text = p.continue_approach(session.aircraft)
            session.phase = Phase.APPROACH
            instruction = session.remember(
                Instruction("continue_approach", station_key(station), text, {},
                            readback_required=False)
            )
            return Reply(text, station, instruction=instruction)

        # Where the user comes in the arrival stream. "Number two, follow the
        # Airbus" is only worth saying when there is an Airbus to follow, so
        # the sequence is built from the traffic that actually exists.
        sequence = ""
        ahead = [f for f in self._traffic_ahead(True) if f.distance_nm < distance]
        if ahead:
            sequence = p.sequence_number(len(ahead) + 1,
                                         ahead[-1].describe(language_of(p)))
        withheld = self.landing_or_withheld(
            session, station, weather, p, airport, runway,
            station_name=(self._station_name(station, p)
                          if not session.established_with(station) else ""),
            sequence=sequence,
            caution=self._caution(session, weather, p,
                                  ahead[-1].type_code if ahead else ""),
            state=state)
        if withheld is not None:
            return withheld

        text = p.cleared_to_land(
            session.aircraft, runway, weather,
            station=self._station_name(station, p) if not session.established_with(station) else "",
            sequence=sequence,
            abbreviated=session.established_with(station),
            caution=self._caution(session, weather, p,
                                  ahead[-1].type_code if ahead else ""),
        )
        session.cleared_landing = True
        session.phase = Phase.LANDING
        session.allow_runway(runway)
        instruction = session.remember(
            Instruction("landing_clearance", station_key(station), text,
                        {"runway": runway})
        )
        return Reply(text, station, instruction=instruction)

    # ------------------------------------------------------------------
    # what the arrival watch composes with
    # ------------------------------------------------------------------
    #
    # :mod:`wilcoatc.atc.arrival` says the things a controller says about an
    # arrival that nobody asked about -- the descent, the approach, the
    # clearance to land. It composes them from the same pieces this class
    # does, and those pieces were all private to it, so this is the seam
    # rather than four reaching-in calls. Nothing new is decided here; each
    # one is the answer this class already had.

    def release_for_takeoff(self, session, station, weather, p,
                            state=None) -> Reply:
        """The takeoff clearance for an aeroplane already sitting on the runway.

        The same clearance a pilot who asks gets. What it is for is the
        aeroplane that asked once, was told to line up and wait because
        somebody was landing, and then heard nothing more for the rest of its
        life -- there was no path in this class that ever went back to a
        departure it had held.
        """
        return self._issue_takeoff(session, station, weather, p,
                                   ParsedIntent())

    def clear_to_land(self, session, station, weather, p, state=None) -> Reply:
        """The landing clearance for an arrival that has not asked for one.

        Identical to what a pilot who calls for it gets: the runway, the
        sequence, the wind, and the phase and the runway allowance that go
        with it. A tower that waits to be asked is the one thing a tower
        never does.
        """
        return self._issue_landing(session, station, weather, p,
                                   ParsedIntent(), state)

    def approach_for(self, session, airport, runway: str,
                     weather=None) -> str:
        """The approach to name for this runway, as charted ("ILS Z",
        "RNAV", "VISUAL"): :meth:`approach_choice`'s."""
        return self.approach_choice(session, airport, runway,
                                    weather).approach.name

    def _current_runway_heard(self, parsed, session, station) -> None:
        """A runway the pilot names by the number the database had and the
        simulator's scenery no longer has ("25" where the signs now say
        "26"): understood, and answered with the current one, which is the
        one every reply, readback check and clearance then uses."""
        values = getattr(parsed, "values", None)
        named = values.get("runway") if isinstance(values, dict) else None
        if not named:
            return
        fields = [getattr(station, "ident", "")]
        for field in (getattr(session, "destination", None),
                      getattr(session, "departure", None)):
            if field is not None and field.ident not in fields:
                fields.append(field.ident)
        for ident in fields:
            if not ident or self.navdb.has_runway(ident, named):
                return
            current = self.navdb.current_runway(ident, named)
            if current and current != named:
                values["runway"] = current
                decision("runway_renamed", airport=ident, heard=named,
                         current=current,
                         why="renumbered in the simulator's scenery")
                return

    def _sim_ils(self, ident: str, runway: str) -> bool | None:
        """Whether the simulator's runway end has an ILS, when the runways
        are the simulator's: True for a navaid its approaches show is a
        localizer, False for none or a VOR, None while that is not known
        (a navaid whose kind the approaches have not told yet) or when the
        runways are the database's, which does not say."""
        if not ident or self.navdb.runway_source(ident) == "db":
            return None
        from ..navdata.db import normalize_runway

        key = normalize_runway(runway)
        for rw in self.navdb.runways(ident):
            for end, navaid, kind in ((rw.le_ident, rw.le_ils, rw.le_ils_kind),
                                      (rw.he_ident, rw.he_ils, rw.he_ils_kind)):
                if normalize_runway(end) != key:
                    continue
                if not navaid:
                    return False
                return {"ILS": True, "VOR": False}.get(kind)
        return None

    def approach_choice(self, session, airport, runway: str,
                        weather=None) -> Choice:
        """The approach a controller names for landing on ``runway``
        (atc/approaches.py ``plan``), chosen once and kept: it changes with
        the runway or a request, never because the weather got known on the
        way in. Never an ILS the simulator says is not there."""
        ident = getattr(airport, "ident", "") or ""
        requested = getattr(session, "requested_approach", "") if session else ""
        key = (ident, runway, requested)
        kept = getattr(session, "approach_choice", None) if session else None
        if kept is not None and getattr(session, "approach_key", None) == key:
            return kept
        known = self.approach_data.get(ident)
        if not self.realism.approach_catalogue:
            available = known.types(runway) if known is not None else None
            return Choice(Approach(choose(available, requested), runway),
                          runway, why="the catalogue is switched off")
        headings = {}
        for end, heading, _rw in self.navdb.runway_ends(ident) if ident else ():
            headings[end] = heading
        choice = plan_approach(
            self.approach_data.get(ident), runway, weather,
            requested=requested,
            airliner=bool(getattr(getattr(session, "profile", None),
                                  "airliner", True)),
            # The visual "in the US and Canada": Canada's fields are C...
            region=("faa" if ident.upper().startswith("C")
                    else vfr_region(ident)),
            runway_headings=headings,
            ils=self._sim_ils(ident, runway))
        if session is not None:
            session.approach_choice = choice
            session.approach_key = key
            decision("approach", airport=ident, runway=runway,
                     approach=choice.approach.name, line=choice.line or None,
                     circle_to=choice.circle_to or None,
                     circle_side=choice.circle_side or None, why=choice.why,
                     requested=requested or None)
        return choice

    def approach_said(self, p, choice: Choice) -> str:
        """The approach as this phrasebook says it, letter and all."""
        return p.approach_name(choice.approach.name)

    def circle_said(self, p, choice: Choice) -> str:
        """", circle to runway 07, north of the airport", or ""."""
        if not choice.circle_to:
            return ""
        return p.circle_clause(choice.circle_to, choice.circle_side)

    # Inside this of the field the procedures take over from the sector
    # altitude: the radar floor is the intercept altitude, not the MSA.
    MSA_RADIUS_NM = 30.0

    @staticmethod
    def _procedure_default(airport) -> float:
        """Field elevation plus 2500 ft, up to the next thousand: 3000 ft at
        a field near sea level, 5000 at Innsbruck and Las Vegas. Where no
        procedure is known, the intercept and the missed approach both."""
        elevation = float(getattr(airport, "elev_ft", 0.0) or 0.0)
        return float(math.ceil((elevation + 2500.0) / 1000.0) * 1000.0)

    def _published(self, airport, runway: str, kind: str, which: int) -> float:
        """A procedure altitude from the simulator, if it is believable:
        between 1000 and 12,000 ft above the field. Outside that it is a unit
        the documentation got wrong, and the default is safer than it."""
        known = self.approach_data.get(getattr(airport, "ident", "") or "")
        if known is None or not runway:
            return 0.0
        value = known.altitudes(runway, legacy_kind(kind))[which]
        elevation = float(getattr(airport, "elev_ft", 0.0) or 0.0)
        believed = elevation + 1000.0 <= value <= elevation + 12000.0
        if value:
            decision("procedure_altitude", airport=airport.ident, runway=runway,
                     which=("faf", "missed")[which], value_ft=round(value),
                     field_ft=round(elevation), accepted=believed)
        if believed:
            return float(round(value / 100.0) * 100.0)
        return 0.0

    def altitude_source(self, airport, runway: str = "", kind: str = "",
                        which: int = 0) -> str:
        """Where an intercept (0) or missed-approach (1) altitude came from."""
        return ("published" if self._published(airport, runway, kind, which)
                else "field+2500")

    def intercept_altitude(self, airport, runway: str = "", kind: str = "") -> float:
        """The altitude to hold until established on the approach.

        The final approach fix altitude where the simulator publishes one,
        else field elevation plus about 2500 ft. Never the sector altitude:
        procedures descend below an MSA, and using it told Innsbruck to
        "maintain flight level one five zero until established" from seven
        thousand feet on a sixteen-mile final.
        """
        return (self._published(airport, runway, kind, 0)
                or self._procedure_default(airport))

    def go_around_altitude(self, airport, runway: str = "") -> float:
        """Where a go-around climbs to: the published missed-approach
        altitude, else field elevation plus about 2500 ft -- never the
        sector altitude, which sent Innsbruck round to fifteen thousand."""
        return (self._published(airport, runway, "", 1)
                or self._procedure_default(airport))

    def descent_floor(self, airport, state=None, runway: str = "",
                      cleared: bool = False) -> float:
        """The lowest a radar position descends an arrival to.

        The sector altitude (:meth:`minimum_for`) while the aeroplane is
        beyond MSA_RADIUS_NM and not cleared for an approach; inside it, or
        once cleared, the intercept altitude, because the approach is the
        terrain protection from there.
        """
        intercept = self.intercept_altitude(airport, runway)
        if cleared or airport is None or state is None \
                or not getattr(state, "has_position", False):
            return intercept if cleared or state is None else max(
                intercept, self.minimum_for(airport, state))
        if state.distance_to(airport.lat, airport.lon) <= self.MSA_RADIUS_NM:
            return intercept
        return max(intercept, self.minimum_for(airport, state))

    def go_around(self, session, station, weather, p, airport,
                  told: bool = False) -> Reply:
        """An arrival that has gone round, announced or seen.

        The landing clearance is spent and is not given back until the
        aeroplane is on the final again and descending (``going_around``).
        The approach clearance and both tower handoffs are armed again, so
        the flight is worked round a second time by the same sequence as the
        first. A tower gives the missed-approach instruction; a radar
        position, which the pilot has told, gives the climb. ``told`` is the
        tower seeing it climb away without a word, which it tells to go
        around exactly as if it had asked.
        """
        aircraft = session.aircraft
        if session.visual:
            return self._back_into_the_circuit(session, station, weather, p,
                                               airport, told)
        session.cleared_landing = False
        session.going_around = True
        session.final_low_ft = 0.0
        session.climb_since = 0.0
        session.phase = Phase.APPROACH
        session.advisories.difference_update({
            "arrival:cleared_approach", "handoff:tower", "handoff:missed"})
        if self.realism.go_around_vectors:
            # Re-vectored into the pattern from where it is (the IFR plan,
            # Phase 5 (d)): the pattern it flew is over, and its speed with
            # it. Left as it was, the next vector was the old intercept.
            session.vector_leg, session.vector_side = "", 0.0
            session.vector_at = 0.0
            session.speed_given, session.speed_lifted = "", False
            session.joined_final = False
        session.arrival_runway = (
            session.arrival_runway or self._arrival_runway(airport, weather))
        altitude = self.go_around_altitude(airport, session.arrival_runway)
        session.assigned_altitude_ft = altitude
        decision("go_around", airport=getattr(airport, "ident", ""),
                 runway=session.arrival_runway, seen=told,
                 announced_on=station.position, altitude_ft=altitude,
                 source=self.altitude_source(airport, session.arrival_runway,
                                             which=1))
        transition = self._transition_altitude(airport)
        if station.position == "TWR" or told:
            text = p.go_around_standard(aircraft, altitude, transition)
        else:
            text = p.climb_maintain(aircraft, altitude,
                                    transition_altitude=transition)
        instruction = session.remember(
            Instruction("go_around", station_key(station), text,
                        {"altitude_ft": int(altitude)}))
        return Reply(text, station, instruction=instruction)

    def _back_into_the_circuit(self, session, station, weather, p, airport,
                               told: bool) -> Reply:
        """A VFR aeroplane that has gone round, announced or seen.

        It was given the instrument missed approach -- an altitude to climb
        to and, from anywhere but the tower, approach control -- which is
        how a Cessna that bounced at Centennial was sent to Denver Approach.
        A VFR go-around climbs out on the upwind and flies the circuit
        again: the tower says which way round and asks for the downwind.
        The words are the join with no particular leg the tower already
        uses -- the FAA's "make left/right traffic" (JO 7110.65 chapter 3,
        section 10; paragraph not checked) and "intégrez le circuit" in
        French -- until the regional phrasebook gives each region its own
        (Phase 3 of the VFR plan).
        """
        session.cleared_landing = False
        session.cleared_option = False
        # Not the instrument go-around state: that holds the landing
        # clearance back until the aeroplane is re-established on an approach
        # and hands it back to approach control on the way.
        session.going_around = False
        session.phase = Phase.APPROACH
        session.pattern_leg = "upwind"
        session.expected_report = "downwind"
        session.expected_report_chased = False
        session.base_owed_behind = ""
        session.arrival_runway = (
            session.arrival_runway or self._arrival_runway(airport, weather))
        decision("go_around", airport=getattr(airport, "ident", ""),
                 runway=session.arrival_runway, seen=told, rules="vfr",
                 announced_on=station.position)
        text = p.enter_pattern(
            session.aircraft, "", session.arrival_runway, leg="",
            direction=self._pattern_direction(session, None, p, airport,
                                              session.arrival_runway),
            report=p.join_report())
        instruction = session.remember(
            Instruction("circuit_report", station_key(station), text, {},
                        readback_required=False))
        return Reply(text, station, instruction=instruction)

    def re_established(self, session, airport, state) -> bool:
        """Back on the final after a go-around: in the cone and descending.

        Clears ``going_around`` when it is so. Without the final in the data
        a descent inside the landing-clearance range is taken instead.
        """
        if not session.going_around:
            return True
        if state is None or not getattr(state, "has_position", False):
            return False
        if float(getattr(state, "vertical_speed_fpm", 0.0) or 0.0) > -300.0:
            return False
        from .final import final_for

        final = final_for(self.navdb, getattr(airport, "ident", ""),
                          session.arrival_runway)
        if final is not None:
            back = final.in_cone(state, self.LANDING_CLEARANCE_NM)
        else:
            back = self._distance_to(state, airport) <= self.LANDING_CLEARANCE_NM
        if back:
            session.going_around = False
        return back

    def departure_runway(self, airport, weather) -> str:
        """The runway in use for departures: the plan's if the wind allows,
        else the wind's."""
        return self._departure_runway(airport, weather)

    def arrival_runway(self, airport, weather) -> str:
        """The runway in use for arrivals, from the wind."""
        return self._arrival_runway(airport, weather)

    def transition_altitude(self, airport) -> int:
        """Where altitudes become flight levels at this field."""
        return self._transition_altitude(airport)

    def descent_terms(self, airport, weather: Weather | None, p,
                      from_ft: float, target_ft: float) -> dict[str, Any]:
        """How a descent to ``target_ft`` is worded, as keyword arguments.

        A descent is read against the transition *level*: flight levels down
        to it, altitudes below it. The first clearance that leaves the levels
        carries the pressure setting -- QNH in hectopascals, or the altimeter
        in inches where the phrasebook is the FAA's -- and none after it.
        ``from_ft`` is where the aeroplane was cleared or is, whichever the
        caller knows.
        """
        ta = self._transition_altitude(airport)
        wx = weather or Weather()
        tl = transition_level(ta, wx.qnh_hpa, getattr(airport, "ident", ""))
        terms: dict[str, Any] = {"transition_altitude": ta,
                                 "transition_level": tl}
        if target_ft < tl <= from_ft + 50:
            terms["pressure"] = (wx.altimeter_inhg
                                 if getattr(p, "dialect", "") == "faa"
                                 else wx.qnh_hpa)
        return terms

    def minimum_for(self, airport, state=None) -> float:
        """The lowest altitude this controller will clear an arrival to.

        Field elevation plus three thousand feet, raised to two MSA-style
        floors read off the terrain grid (:mod:`wilcoatc.navdata.terrain`):
        the field's -- the highest terrain within 25 NM of it plus 1000 ft,
        or 2000 ft where that is mountainous -- and the same circle round the
        aeroplane, for the terrain on the way in that the field's circle
        does not reach. An area, not a point: the ground directly under the
        aircraft used to be the floor, which is the valley at Innsbruck until
        the moment it is a ridge.

        ``minimum_altitudes.csv`` overrides the field's figure, because a
        published MSA is the authority and is often lower than a grid allows;
        inside its 25 NM it overrides the aircraft's circle too, which would
        otherwise put the grid's figure straight back.
        """
        elevation = float(getattr(airport, "elev_ft", 0.0) or 0.0)
        floor = round((elevation + 3000.0) / 1000.0) * 1000.0
        if airport is None:
            return float(floor)
        published = float(self.navdb.minimum_altitude(airport.ident))
        if published:
            floor = max(floor, published)
        else:
            floor = max(floor, terrain.floor_ft(airport.lat, airport.lon, elevation))
        if state is not None and getattr(state, "has_position", False):
            inside = (state.distance_to(airport.lat, airport.lon)
                      <= terrain.RADIUS_NM)
            if not (published and inside):
                floor = max(floor, terrain.floor_ft(
                    state.latitude, state.longitude, elevation))
        return float(floor)

    def out_of_the_layer(self, airport, weather: Weather | None,
                         target_ft: float) -> float:
        """A descent target moved out of the transition layer.

        Between the transition altitude and level nothing is assigned: the
        level would be a flight level nobody may use and the altitude one
        above the altitude everybody changes over at. The target goes down to
        the last whole thousand at or under the transition altitude.
        """
        ta = self._transition_altitude(airport)
        tl = transition_level(ta, (weather or Weather()).qnh_hpa,
                              getattr(airport, "ident", ""))
        if ta < target_ft < tl:
            return float(int(ta / 1000.0) * 1000)
        return float(target_ft)

    def bearing_to(self, state, airport) -> tuple[int, float]:
        """Where a field is from the aircraft: a clock position and a range."""
        return self._bearing_to(state, airport)

    def station_name(self, station: Station, p, short: bool = False) -> str:
        """What a station calls itself, in the language being worked in."""
        return self._station_name(station, p, short)

    # ------------------------------------------------------------------
    # radar positions: departure, approach, centre
    # ------------------------------------------------------------------

    # Nothing a radar controller can do anything about. Asked for any of them
    # it names whoever can, rather than listing the calls it would rather have
    # heard -- which is what a pilot on the wrong frequency got.
    _SURFACE_ONLY = (
        Intent.REQUEST_PUSHBACK, Intent.REQUEST_TAXI,
        Intent.REQUEST_TAXI_TO_PARKING, Intent.REQUEST_TAKEOFF,
        Intent.REQUEST_CLEARANCE, Intent.REQUEST_PATTERN_WORK,
    )

    def _radar(self, parsed, session, station, weather, p, state) -> Reply:
        aircraft = session.aircraft
        first_contact = not session.established_with(station)

        if parsed.intent in self._SURFACE_ONLY:
            if _airborne(state):
                return Reply(p.already_airborne(aircraft), station)
            sent = self._send_on(session, station, p, "TWR", "GND", "DEL")
            if sent is not None:
                return sent
            return Reply(p.already_on_the_ground(aircraft), station)

        # Radar works aeroplanes that are flying. One still on the ground is
        # not identified, not vectored and certainly not cleared for a visual
        # approach -- all of which it used to be told from the parking stand.
        if not _airborne(state):
            sent = self._send_on(session, station, p, "TWR", "GND", "DEL")
            if sent is not None:
                return sent
            return Reply(p.already_on_the_ground(aircraft), station)

        if is_oceanic(station) and (
                parsed.intent in (Intent.CHECK_IN, Intent.POSITION_REPORT,
                                  Intent.UNKNOWN)
                or "position" in parsed.normalized):
            # An HF station over the ocean has no radar: a position report
            # is taken and acknowledged, never "radar contact", never
            # "identified".
            session.establish(station)
            return Reply(p.roger(aircraft), station)

        if parsed.intent in (Intent.CHECK_IN, Intent.POSITION_REPORT):
            return self._radar_check_in(parsed, session, station, p, state, first_contact)

        if parsed.intent in (Intent.GOING_AROUND, Intent.MISSED_APPROACH):
            # On approach as well as on the tower: a missed approach is
            # often reported to whoever the pilot is still talking to.
            return self.go_around(session, station, weather, p,
                                  session.destination
                                  or self.navdb.airport(station.ident))

        if parsed.intent is Intent.REQUEST_FLIGHT_FOLLOWING:
            return self._issue_flight_following(session, station, p, state)

        if parsed.intent is Intent.REQUEST_DEVIATION \
                and self.realism.deviations and session.ifr:
            return self._approve_deviation(parsed, session, station, p, state)

        # The pilot's other requests (the IFR plan, Phase 6): each answered
        # with a clearance, or "unable" and the reason.
        if self.realism.pilot_requests and session.ifr:
            if parsed.intent is Intent.REQUEST_SPEED:
                return self._answer_speed(parsed, session, station, p, state)
            if parsed.intent is Intent.REQUEST_HOLD and self.realism.holds:
                return self._answer_hold(session, station, p, state)
            if parsed.intent is Intent.REQUEST_RETURN:
                return self._divert(parsed, session, station, p, state,
                                    back=True)
            if parsed.intent is Intent.REQUEST_DIVERSION:
                return self._divert(parsed, session, station, p, state,
                                    back=False)
            if parsed.intent is Intent.REQUEST_DESCENT:
                early = self._descent_asked_early(session, station, p, state)
                if early is not None:
                    return early

        if parsed.intent is Intent.REQUEST_TRANSITION:
            return self._issue_transition(session, station, weather, p, parsed,
                                          state)

        # "Request flight level three three zero" names a level and not a
        # direction, and the parser, which cannot see the altimeter, files
        # it as a climb. Below the aeroplane it is a descent.
        if parsed.intent is Intent.REQUEST_CLIMB:
            asked = parsed.get("altitude_ft") or 0
            current = getattr(state, "altitude_ft", 0.0) or 0.0
            if asked and current and asked < current - 200:
                parsed.intent = Intent.REQUEST_DESCENT

        # A VFR flight is not assigned a level. Its altitude is its own affair
        # unless something is above it, so a request for one gets the approval
        # a controller actually gives rather than an IFR level clearance.
        if (not session.ifr
                and parsed.intent in (Intent.REQUEST_CLIMB,
                                      Intent.REQUEST_DESCENT)):
            asked = parsed.get("altitude_ft") or 0
            if session.vfr_ceiling_ft and asked > session.vfr_ceiling_ft:
                return Reply(
                    p.maintain_vfr(aircraft, session.vfr_ceiling_ft), station)
            text = p.vfr_altitude_discretion(aircraft)
            instruction = session.remember(
                Instruction("vfr_altitude", station_key(station), text, {},
                            readback_required=False))
            return Reply(text, station, instruction=instruction)

        if parsed.intent is Intent.REQUEST_CLIMB:
            target = parsed.get("altitude_ft") or self._next_level_up(session, state)
            # A centre's step climb. Departure and approach climb their
            # flights on a plan of their own and are left as they were.
            blocking = None
            if station.position == "CTR":
                blocking = self._climb_blocked_by(state, target)
                decision("enroute_step_climb", station=station.callsign,
                         event="step climb granted" if blocking is None
                         else "step climb refused",
                         from_ft=round(getattr(state, "altitude_ft", 0.0) or 0.0),
                         to_ft=int(target), granted=blocking is None,
                         traffic=getattr(blocking, "written", None),
                         reason=(f"to {int(target)} ft, nothing in the way"
                                 if blocking is None else
                                 f"to {int(target)} ft, {blocking.written} at "
                                 f"{int(blocking.altitude_ft)} ft inside "
                                 f"{self.STEP_CLIMB_TRAFFIC_NM:g} nm"))
            if blocking is not None:
                current = float(getattr(state, "altitude_ft", 0.0) or 0.0)
                return Reply(p.unable_climb_traffic(
                    aircraft, target, round(current / 100.0) * 100.0,
                    transition_altitude=self._transition_altitude(
                        self.navdb.airport(station.ident))), station)
            text = p.climb_maintain(aircraft, target,
                                    transition_altitude=self._transition_altitude(
                                        self.navdb.airport(station.ident)))
            instruction = session.remember(
                Instruction("altitude", station_key(station), text,
                            {"altitude_ft": int(round(target / 100.0) * 100)})
            )
            return Reply(text, station, instruction=instruction)

        if parsed.intent is Intent.REQUEST_DESCENT:
            target = parsed.get("altitude_ft") or self._next_level_down(session, state)
            current = getattr(state, "altitude_ft", 0.0) or 0.0
            if current and target >= current - 200:
                # Already at or below what was asked for. Refusing is the
                # correct answer; issuing a "descent" to a higher level is not.
                return Reply(p.unable_lower(aircraft), station)
            airport = self.navdb.airport(station.ident)
            if not parsed.get("altitude_ft"):
                target = self.out_of_the_layer(airport, weather, target)
            # Never below the floor, whoever asked for it, and issued as a
            # whole thousand.
            target = max(target, math.ceil(self.descent_floor(
                session.destination or airport, state,
                session.arrival_runway) / 1000.0) * 1000.0)
            if current and target >= current - 200:
                return Reply(p.unable_lower(aircraft), station)
            text = p.descend_maintain(
                aircraft, target,
                **self.descent_terms(
                    airport, weather, p,
                    max(current, float(session.assigned_altitude_ft or 0.0)),
                    target))
            instruction = session.remember(
                Instruction("altitude", station_key(station), text,
                            {"altitude_ft": int(round(target / 100.0) * 100)})
            )
            return Reply(text, station, instruction=instruction)

        if parsed.intent is Intent.REQUEST_DIRECT and self.plan_fixes \
                and station.position == "CTR":
            return self._direct_on_the_plan(parsed, session, station, p, state)

        if parsed.intent is Intent.REQUEST_DIRECT:
            fix = parsed.get("fix", "")
            resolved, spoken = self._resolve_fix(fix, state)
            if not resolved:
                return Reply(p.unable_unknown_fix(aircraft), station)
            text = p.direct_to(aircraft, resolved, fix_spoken=spoken)
            instruction = session.remember(
                Instruction("direct", station_key(station), text, {},
                            readback_required=False)
            )
            return Reply(text, station, instruction=instruction)

        if parsed.intent in (Intent.REQUEST_APPROACH, Intent.REQUEST_VECTORS,
                             Intent.REQUEST_LANDING):
            return self._issue_approach(session, station, weather, p, state,
                                        parsed)

        if parsed.intent is Intent.REPORT_FIELD_IN_SIGHT:
            if session.vector_leg and session.ifr and self.realism.vectors:
                # Being vectored: noted, and the visual comes with the last
                # vector onto the final (arrival.py _intercept), not here
                # in the middle of a downwind.
                session.field_in_sight = True
                session.establish(station)
                decision("vectors", event="field in sight", leg=session.vector_leg)
                return Reply(p.roger(aircraft), station)
            return self._issue_visual_approach(session, station, weather, p)

        if parsed.intent is Intent.REPORT_TRAFFIC_IN_SIGHT:
            return Reply(p.maintain_visual_separation(aircraft), station)

        if parsed.intent is Intent.REPORT_ESTABLISHED:
            tower = self._destination_tower(session, station)
            if tower and tower.mhz:
                text = p.handoff(aircraft, self._station_name(tower, p, short=True), tower.mhz)
                instruction = session.remember(
                    Instruction("handoff", station_key(station), text,
                                {"frequency": tower.mhz})
                )
                return Reply(text, station, instruction=instruction, handoff_to=tower)
            return Reply(p.roger(aircraft), station)

        if parsed.intent is Intent.REQUEST_FREQUENCY_CHANGE:
            if not session.ifr and not session.flight_following \
                    and not session.radar_service \
                    and session.squawk != p.vfr_squawk:
                # A VFR flight that was given a code and no advisories: the
                # code comes back with the frequency change.
                session.squawk = p.vfr_squawk
                return Reply(p.squawk_vfr(aircraft, frequency_change=True),
                             station)
            # The end of radar advisories, and of the code that went with
            # them; left set, the traffic watch carried on calling traffic
            # to a flight that had left the frequency.
            session.flight_following = False
            session.radar_service = ""
            if not session.ifr:
                session.squawk = p.vfr_squawk
            return Reply(p.radar_service_terminated(aircraft), station)

        if parsed.intent is Intent.CANCEL_IFR:
            session.ifr = False
            return Reply(p.ifr_cancelled(aircraft), station)

        if parsed.intent is Intent.RADIO_CHECK:
            return Reply(p.radio_check(aircraft), station)

        if parsed.intent in (Intent.ACKNOWLEDGE, Intent.AFFIRMATIVE):
            return Reply("", station, silent=True)

        return self._not_understood(session, station, p, state)

    def _radar_check_in(self, parsed, session, station, p, state, first_contact) -> Reply:
        aircraft = session.aircraft
        transition = self._transition_altitude(self.navdb.airport(station.ident))

        # A check-in on departure gets radar contact and the climb the
        # clearance already promised.
        target = parsed.get("target_altitude_ft") or 0
        current = getattr(state, "altitude_ft", 0.0) or 0.0
        airborne = state is not None and not getattr(state, "on_ground", True)
        cruise = session.cruise_altitude_ft or self._default_cruise(session)

        climb_to = 0.0
        # Never issue a climb to a level the aircraft is already at: an
        # aircraft checking in level at cruise gets "radar contact" and nothing
        # more, which is what a real controller says.
        below_cruise = current <= 0 or current < cruise - 500
        if session.phase in (Phase.TAKEOFF, Phase.DEPARTURE) and below_cruise:
            session.phase = Phase.DEPARTURE
            climb_to = cruise
        elif (airborne and station.position == "DEP" and current < cruise - 1000
              and self._departing(session, state)):
            # Checking in with departure on the way up, whatever the phase
            # tracker thinks: the climb is what the aircraft is waiting for.
            # Only on the way up. Ten fields publish a Departure and no
            # Approach, the centre hands arrivals to it, and an arrival
            # checking in was climbed to cruise and lost as a departure.
            session.phase = Phase.DEPARTURE
            climb_to = cruise
        elif target and target > max(current, session.assigned_altitude_ft):
            climb_to = target

        # A VFR flight checking in is identified and told to stay VFR. It is
        # not climbed to a cruising level it never filed.
        if not session.ifr:
            # Identified: from here this room is watching the flight, and it
            # is this room that ends that (watch.RadarServiceWatch).
            session.radar_service = station_key(station)
            instruction_text = p.maintain_vfr_clause(
                session.vfr_ceiling_ft, transition_altitude=transition)
            if first_contact:
                text = p.radar_contact(
                    aircraft, self._station_name(station, p),
                    instruction=instruction_text, abbreviated=False)
            else:
                text = p.maintain_vfr(aircraft, session.vfr_ceiling_ft)
            instruction = session.remember(
                Instruction("radar_contact", station_key(station), text, {},
                            readback_required=False))
            return Reply(text, station, instruction=instruction)

        if self.realism.climb_steps and self._climbing_out(session, state,
                                                           cruise):
            return self._climbing_check_in(session, station, p, state,
                                           first_contact, transition, cruise)

        if station.position == "CTR" and first_contact and not climb_to:
            return self._centre_check_in(parsed, session, station, p, state,
                                         transition)

        instruction_text = ""
        values: dict[str, Any] = {}
        if climb_to:
            verb = "climb and maintain" if p.dialect == "faa" else "climb to"
            instruction_text = f"{verb} {p.alt(climb_to, transition)}"
            values["altitude_ft"] = int(climb_to)

        if first_contact:
            text = p.radar_contact(
                aircraft, self._station_name(station, p), instruction=instruction_text,
                abbreviated=False,
            )
        elif instruction_text:
            text = p.climb_maintain(aircraft, climb_to, transition_altitude=transition)
        else:
            text = p.roger(aircraft)

        instruction = None
        if values:
            instruction = session.remember(
                Instruction("altitude", station_key(station), text, values)
            )
        return Reply(text, station, instruction=instruction)

    def _climbing_out(self, session, state, cruise: float) -> bool:
        """An IFR flight on its way up to the cruise, wherever it is."""
        if state is None or getattr(state, "on_ground", True):
            return False
        if session.phase in (Phase.DESCENT, Phase.APPROACH, Phase.LANDING):
            return False
        here = float(getattr(state, "altitude_ft", 0.0) or 0.0)
        if not cruise or here >= cruise - 500:
            return False
        climbing = float(getattr(state, "vertical_speed_fpm", 0.0) or 0.0)             >= CLIMBING_FPM
        return session.phase in (Phase.TAKEOFF, Phase.DEPARTURE) or climbing             or self._departing(session, state)

    def _climbing_check_in(self, session, station, p, state,
                           first_contact: bool, transition: int,
                           cruise: float) -> Reply:
        """The answer to a flight checking in on its way up.

        Never "maintain" a level it is already through, and never the whole
        climb at once (atc/climb.py). Departure: radar contact, and the next
        step when the flight is nearly at what the clearance gave -- a flight
        still well below it gets its next step as it nears it, from the
        climb watch -- with "proceed direct" the first fix ahead for a flight
        the tower sent off on runway heading. A centre: its own step, or the
        cruise, once the flight is nearly up to what departure gave; else
        "roger" (FAA) or "identified" (ICAO) and the climb carries on.
        """
        aircraft = session.aircraft
        here = float(getattr(state, "altitude_ft", 0.0) or 0.0)
        assigned = float(session.assigned_altitude_ft
                         or session.initial_altitude_ft or 0.0)
        callsign = aircraft.callsign.written
        step = 0
        direct = None
        why = ""
        if station.position in ("DEP", "APP"):
            session.phase = Phase.DEPARTURE
            short = here < assigned - DEPARTURE_REACHING_FT
            if short and departure_top(cruise) - assigned > LAST_STEP_FT:
                why = "still climbing to the clearance's altitude"
            else:
                step = departure_step(station.callsign, callsign, assigned,
                                      here, cruise)
                why = "the next step"
            if session.runway_heading and self.route:
                direct = on_course_fix(self.route, state.latitude,
                                       state.longitude)
        else:
            if here >= assigned - DEPARTURE_REACHING_FT:
                step = centre_step(station.callsign, here, assigned, cruise)
                why = "the centre's step" if step < cruise else "the cruise"
            else:
                why = "still climbing to departure's altitude"
        if step and step <= max(assigned, here) + 500:
            step = 0
        clauses = []
        values: dict[str, Any] = {}
        if step:
            clauses.append(p.climb_clause(step, transition))
            values["altitude_ft"] = int(step)
        if direct is not None:
            clauses.append(p.direct_clause(direct.ident))
            values["fix"] = direct.ident
            session.runway_heading = False
        decision("climb", event="check_in", station=station.callsign,
                 here_ft=round(here), assigned_ft=round(assigned),
                 step_ft=step or None, direct=direct.ident if direct else None,
                 why=why)
        session.establish(station)
        instruction_text = ", ".join(clauses)
        if first_contact and station.position == "CTR" and not instruction_text:
            return Reply(p.centre_check_in(aircraft,
                                           self._station_name(station, p)),
                         station)
        if first_contact:
            text = p.radar_contact(aircraft, self._station_name(station, p),
                                   instruction=instruction_text,
                                   abbreviated=False)
        elif instruction_text:
            text = _join(aircraft.spoken(True), instruction_text) + "."
        else:
            text = p.roger(aircraft)
        instruction = None
        if "altitude_ft" in values:
            instruction = session.remember(
                Instruction("altitude", station_key(station), text, values))
        elif values:
            instruction = session.remember(
                Instruction("direct", station_key(station), text, values,
                            readback_required=False))
        return Reply(text, station, instruction=instruction)

    def _centre_check_in(self, parsed, session, station, p, state,
                         transition: int) -> Reply:
        """A flight checking in with a centre, in the cruise.

        In the region's words: an FAA centre already has the flight on
        radar and says "Boston Center, roger"; an ICAO one says "identified"
        and the level to keep. A level reported that the Mode C does not
        show is asked for -- the check this program already makes of a VFR
        flight under a Bravo -- because one of the two is wrong and only
        the pilot can say which.
        """
        aircraft = session.aircraft
        session.establish(station)
        heard = parsed.get("altitude_ft") or 0
        shown = getattr(state, "altitude_ft", None) if state is not None else None
        if heard and shown is not None \
                and abs(float(heard) - float(shown)) >= self.MODE_C_DISAGREES_FT:
            decision("enroute", event="check_in", station=station.callsign,
                     said="say_altitude", heard_ft=heard,
                     mode_c_ft=round(float(shown)))
            return Reply(p.say_altitude(aircraft), station)
        level = (session.assigned_altitude_ft or heard
                 or (round(float(shown) / 100.0) * 100.0 if shown else 0.0))
        text = p.centre_check_in(aircraft, self._station_name(station, p),
                                 level_ft=float(level or 0.0),
                                 transition=transition)
        decision("enroute", event="check_in", station=station.callsign,
                 said="roger" if p.dialect == "faa" else "identified",
                 heard_ft=heard,
                 mode_c_ft=round(float(shown)) if shown is not None else None)
        return Reply(text, station)

    def _issue_flight_following(self, session, station, p, state) -> Reply:
        """VFR radar advisories, which are asked for and can be refused.

        FAA JO 7110.65 2-1-1: additional services are provided workload
        permitting. Busy means busy, so a sector already sequencing arrivals
        says so instead of pretending it has the capacity.
        """
        aircraft = session.aircraft
        if len(self._traffic_ahead(True)) >= 3:
            return Reply(p.flight_following_unavailable(aircraft), station)

        session.flight_following = True
        session.radar_service = station_key(station)
        session.squawk = squawk_for(session, station.ident)
        text = p.flight_following_approved(
            aircraft,
            self._station_name(station, p) if not session.established_with(station) else "",
            session.squawk,
        )
        instruction = session.remember(
            Instruction("flight_following", station_key(station), text,
                        {"squawk": session.squawk}))
        return Reply(text, station, instruction=instruction)

    def _issue_approach(self, session, station, weather, p, state,
                        parsed: ParsedIntent | None = None) -> Reply:
        aircraft = session.aircraft
        destination = session.destination or self._nearest_airport(state)
        if destination is None:
            return Reply(p.unable_unknown_destination(aircraft), station)

        if session.hold is not None and session.hold_requested:
            # The hold the pilot asked for is over when they ask for the
            # approach: the arrival watch lets the flight go.
            session.hold_requested = False
        # "Request RNAV two five left": the type, and the runway if one was
        # named and the field has it.
        asked = asked_for(parsed.normalized or parsed.text) if parsed else ""
        named = parsed.get("runway") if parsed else ""
        if named and not self.navdb.has_runway(destination.ident, named) \
                and self.realism.pilot_requests and session.ifr:
            # A runway the field does not have: said, not quietly swapped.
            decision("request", asked="runway", answer="unable",
                     runway=named, why="no such runway")
            return Reply(p.unable_because(aircraft, "no_runway", runway=named),
                         station)
        if named and self.navdb.has_runway(destination.ident, named):
            session.arrival_runway = named
        runway = session.arrival_runway or self._arrival_runway(destination, weather)
        known = self.approach_data.get(destination.ident)
        if (asked and asked != VISUAL and known is not None and session.ifr
                and self.realism.pilot_requests
                and asked not in known.types(runway)):
            # An approach the runway does not have: unable, and the one to
            # expect instead (the choice is not changed).
            other = self.approach_choice(session, destination, runway, weather)
            decision("request", asked="approach", answer="unable",
                     approach=asked, runway=runway,
                     instead=other.approach.name)
            return Reply(p.unable_because(
                aircraft, "no_approach", approach=p.approach_name(asked),
                runway=runway, other=self.approach_said(p, other)), station)
        if asked:
            session.requested_approach = asked
        session.arrival_runway = runway
        session.phase = Phase.APPROACH

        # A VFR flight is not given an instrument approach. It is told where
        # the field is and asked to report it in sight, which is how a radar
        # position hands one to a tower.
        if not session.ifr:
            oclock, distance = self._bearing_to(state, destination)
            text = p.report_field_in_sight(
                aircraft, destination.spoken, oclock, distance)
            instruction = session.remember(
                Instruction("report_field", station_key(station), text, {},
                            readback_required=False))
            return Reply(text, station, instruction=instruction)

        choice = self.approach_choice(session, destination, runway, weather)
        text = p.expect_approach(
            aircraft, self.approach_said(p, choice),
            choice.approach.runway or runway, destination.spoken,
            circle=self.circle_said(p, choice))
        instruction = session.remember(
            Instruction("expect_approach", station_key(station), text, {},
                        readback_required=False)
        )
        return Reply(text, station, instruction=instruction)

    def _issue_visual_approach(self, session, station, weather, p) -> Reply:
        aircraft = session.aircraft
        destination = session.destination
        runway = session.arrival_runway or (
            self._arrival_runway(destination, weather) if destination else ""
        )
        if not runway:
            return Reply(p.roger(aircraft), station)

        session.arrival_runway = runway
        session.phase = Phase.APPROACH
        text = p.cleared_visual_approach(
            aircraft, runway, destination.spoken if destination else ""
        )
        tower = self._destination_tower(session, station)
        instruction = session.remember(
            Instruction("visual_approach", station_key(station), text,
                        {"runway": runway})
        )
        return Reply(text, station, instruction=instruction, handoff_to=tower)

    # ------------------------------------------------------------------
    # uncontrolled fields
    # ------------------------------------------------------------------

    # Set by the engine: ``ident -> Weather``, the observation at a field.
    # Flight service reads the weather where the pilot is going, which is
    # not the field the station is filed at.
    weather_for = None

    def _info(self, parsed, session, station, weather, p, state) -> Reply:
        """An information frequency: AFIS, FIS or flight service
        (:mod:`wilcoatc.atc.afis`). Information, never a clearance."""
        from . import afis

        return afis.answer(self, parsed, session, station, weather, p, state)

    def _ctaf(self, parsed, session, station, weather, p, state) -> Reply:
        """An uncontrolled field has nobody to answer.

        Staying silent is the correct behaviour and the realistic one: on CTAF
        the pilot self-announces and no controller replies. Reached only if a
        broadcast position slips past the check at the top of :meth:`respond`,
        which is why that check is the one that matters.
        """
        return Reply("", station, silent=True)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _station_name(station: Station, phraseology, short: bool = False) -> str:
        """A station's callup name in the language currently being spoken."""
        if station is None:
            return ""
        return station.callsign_in(language_of(phraseology), short=short)

    def _planned_sid(self, airport, weather) -> str:
        """The plan's SID, while the runway it was planned for is in use."""
        planned = self.plan_runways.get(getattr(airport, "ident", ""), {})
        sid = planned.get("sid", "")
        if not sid or weather is None:
            return ""
        runway = planned.get("departing", "")
        if runway and self._departure_runway(airport, weather) != runway:
            return ""
        return sid

    def _planned(self, airport, weather, which: str) -> str:
        """The plan's runway at this field, if the wind leaves it usable."""
        planned = self.plan_runways.get(getattr(airport, "ident", ""), {})
        runway = planned.get(which, "")
        if not runway or weather is None:
            return ""
        if not self.navdb.runway_within_limits(
                airport.ident, runway, weather.wind_dir, weather.wind_kt,
                gust_kt=weather.gust_kt, variable=weather.variable):
            return ""
        return runway

    def _departure_runway(self, airport, weather) -> str:
        if airport is None:
            return "01"
        planned = self._planned(airport, weather, "departing")
        if planned:
            return planned
        return self._wind_runway(airport, weather, departing=True)

    def _arrival_runway(self, airport, weather) -> str:
        if airport is None:
            return "01"
        planned = self._planned(airport, weather, "landing")
        if planned:
            return planned
        return self._wind_runway(airport, weather, departing=False)

    def _wind_runway(self, airport, weather, *, departing: bool) -> str:
        """The runway the wind gives, kept until the wind takes it away.

        It was worked out afresh on every call, so a wind veering through
        the crossover flipped the runway on the next transmission and back
        on the one after, and nobody was ever told. A tower keeps the runway
        in use while the wind leaves it usable -- a tailwind of 5 kt or less,
        gusts included, which is the figure both ICAO PANS-ATM (Doc 4444,
        aerodrome control, runway in use) and the FAA's calm-wind runway use
        work to; UNVERIFIED: the paragraph numbers -- and changes it only
        when it does not (``NavDB.runway_within_limits``).
        """
        best = self.navdb.best_runway(
            airport.ident, weather.wind_dir, weather.wind_kt, departing=departing,
            variable=weather.variable, gust_kt=weather.gust_kt,
        )
        choice = best[0] if best else "01"
        if not best or not self.realism.runway_change:
            return choice
        key = (airport.ident, departing)
        current = self.runway_in_use.get(key)
        if current and current != choice:
            if self.navdb.runway_within_limits(
                    airport.ident, current, weather.wind_dir, weather.wind_kt,
                    gust_kt=weather.gust_kt, variable=weather.variable):
                return current
            decision("runway_change", airport=airport.ident,
                     departing=departing, was=current, now=choice,
                     wind_dir=round(float(weather.wind_dir or 0.0)),
                     wind_kt=round(float(weather.wind_kt or 0.0)),
                     gust_kt=round(float(weather.gust_kt or 0.0)) or None,
                     variable=bool(weather.variable),
                     weather=getattr(weather, "source", "") or None)
            # The landing direction is the one announced; a departure-only
            # change (a preferential system) is announced when it is the
            # only one.
            if not departing or airport.ident not in self.runway_changes \
                    or self.runway_changes[airport.ident][1] != choice:
                self.runway_changes[airport.ident] = (current, choice)
        self.runway_in_use[key] = choice
        return choice

    @classmethod
    def _departing(cls, session, state) -> bool:
        """Whether a flight is outbound from where it took off.

        The phase says so, or the aeroplane is still near the field it
        left and is not an arrival back at it.
        """
        if session.phase.outbound:
            return True
        if session.phase in (Phase.DESCENT, Phase.APPROACH, Phase.LANDING):
            return False
        origin = session.departure
        if origin is None or state is None or not getattr(state, "has_position", False):
            return False
        return state.distance_to(origin.lat, origin.lon) <= DEPARTING_WITHIN_NM

    @classmethod
    def _transition_altitude(cls, airport) -> int:
        """Where altitudes become flight levels.

        The table moved to :mod:`wilcoatc.navdata.db`, beside the dialect and
        the registration prefix, because the controller was not the only thing
        that needed it. The other aeroplanes on the frequency say their own
        levels, and they were reading every one of them against eighteen
        thousand feet -- so a controller over London issued a flight level and
        the aircraft read it back in thousands of feet.
        """
        if airport is None:
            return DEFAULT_TRANSITION_FT
        return transition_altitude(airport.ident)

    @staticmethod
    def _default_cruise(session: FlightSession) -> float:
        """A plausible cruise level when the pilot never filed one."""
        if session.cruise_altitude_ft:
            return session.cruise_altitude_ft
        wake = session.aircraft.wake
        if wake in ("heavy", "super"):
            return 35000.0
        if session.profile.airliner:
            return 33000.0
        return 9000.0

    def _valid_level(self, feet: float, state, airport, upward: bool) -> float:
        """The nearest level an aeroplane can be cleared to, above or below.

        Whole thousands; and above the transition, the semicircular rule on
        the magnetic track (ICAO Annex 2 Appendix 3, 14 CFR 91.179): 000-179
        odd, 180-359 even, and above FL410 the RVSM steps of four thousand
        (odd 410, 450...; even 430, 470...). "Request climb" at 17,143 ft
        used to give flight level two one one.
        """
        transition = self._transition_altitude(airport)
        if feet <= transition:
            step = 1000.0
            base = 0.0
        else:
            from ..navdata.magvar import to_magnetic, variation
            from .final import course_over_ground

            track = course_over_ground(state) if state is not None else None
            if track is not None and getattr(state, "has_position", False):
                track = to_magnetic(track, variation(state.latitude, state.longitude))
            east = track is None or track < 180.0
            if feet > 41000.0:
                step, base = 4000.0, (41000.0 if east else 43000.0)
            else:
                step, base = 2000.0, (1000.0 if east else 0.0)
        if upward:
            level = math.ceil((feet - base) / step) * step + base
        else:
            level = math.floor((feet - base) / step) * step + base
        return float(level)

    def _next_level_up(self, session, state) -> float:
        """The next level up, valid for the direction, never below where the
        aeroplane is, and the filed cruise level at most."""
        current = getattr(state, "altitude_ft", 0.0) or session.assigned_altitude_ft
        target = session.cruise_altitude_ft or self._default_cruise(session)
        step = 4000 if current < 18000 else 10000
        airport = self.navdb.airport(getattr(session.departure, "ident", "") or "") \
            if session.departure else None
        wanted = max(current + step, 3000.0)
        level = self._valid_level(wanted, state, airport, upward=True)
        # Inside the band near the top the filed level is the answer, valid
        # for the direction or not: it is what was filed and approved.
        if level >= target:
            return float(target)
        return level

    def _next_level_down(self, session, state) -> float:
        """The next level down, rounded to a usable altitude.

        Descents are issued in the steps a controller actually uses -- large
        ones in the flight levels, smaller ones below -- and never to a level
        the aircraft is already at or beneath.
        """
        current = getattr(state, "altitude_ft", 0.0) or session.assigned_altitude_ft
        if current <= 0:
            return 10000.0
        step = 10000 if current > 24000 else 4000
        target = current - step
        # A level, not a number: whole, and valid for the direction above
        # the transition.
        airport = session.destination or session.departure
        level = self._valid_level(min(target, current - 1000), state, airport,
                                  upward=False)
        return float(max(3000, level))

    # A step climb is refused for an aeroplane this close laterally that sits
    # between the level the flight is leaving and the one it asked for, or
    # less than the RVSM thousand feet above that one. UNVERIFIED as a
    # figure: real controllers judge it off the closure, and ten miles is
    # what two airliners closing head-on cover in under a minute.
    STEP_CLIMB_TRAFFIC_NM = 10.0

    def _climb_blocked_by(self, state, target_ft: float):
        """The contact a climb to ``target_ft`` would go through, or None."""
        if state is None or not getattr(state, "has_position", False):
            return None
        current = float(getattr(state, "altitude_ft", 0.0) or 0.0)
        nearest = None
        for contact in self.contacts():
            if contact.on_ground:
                continue
            if not current + 500.0 < contact.altitude_ft < target_ft + 1000.0:
                continue
            miles = state.distance_to(contact.latitude, contact.longitude)
            if miles > self.STEP_CLIMB_TRAFFIC_NM:
                continue
            if nearest is None or miles < nearest[0]:
                nearest = (miles, contact)
        return nearest[1] if nearest else None

    # A request for descent this long or more before it is due gets "expect
    # lower"; nearer, the descent itself (after LocalTC: 15 minutes).
    DESCENT_ASK_MIN = 15.0
    # The speed limit below 10,000 ft (14 CFR 91.117(a), SERA.6001).
    SPEED_LIMIT_KT = 250
    SPEED_LIMIT_BELOW_FT = 10000.0

    def _answer_speed(self, parsed, session, station, p, state) -> Reply:
        """A speed, or no restriction: unable below 10,000 ft above 250 kt,
        and unable while there is somebody to follow."""
        asked = int(parsed.get("speed_kt") or 0)
        altitude = float(getattr(state, "altitude_ft", 0.0) or 0.0)
        aircraft = session.aircraft
        if asked > self.SPEED_LIMIT_KT and altitude < self.SPEED_LIMIT_BELOW_FT:
            decision("request", asked="speed", answer="unable",
                     why="speed limit", knots=asked)
            return Reply(p.unable_because(aircraft, "speed_limit"), station)
        destination = session.destination
        if (session.speed_given and not session.speed_lifted
                and self.arrivals is not None and destination is not None
                and state is not None and getattr(state, "has_position", False)
                and self.arrivals._traffic_ahead(
                    state, destination,
                    state.distance_to(destination.lat, destination.lon))):
            decision("request", asked="speed", answer="unable",
                     why="traffic ahead")
            return Reply(p.unable_because(aircraft, "traffic_ahead"), station)
        session.speed_lifted = bool(session.speed_given)
        decision("request", asked="speed", answer="no restriction",
                 knots=asked or None)
        return Reply(p.no_speed_restrictions(aircraft), station)

    def _answer_hold(self, session, station, p, state) -> Reply:
        """The hold itself, over the arrival's last fix, wherever the
        flight is; unable without a fix to hold over."""
        session.hold_requested = True
        destination = session.destination
        reply = None
        if self.arrivals is not None and destination is not None \
                and state is not None and getattr(state, "has_position", False):
            reply = self.arrivals._give_hold(
                session, station, state, destination, "requested",
                self.arrivals._arrivals_queued(state, destination), p,
                time.time())
        if reply is None:
            session.hold_requested = False
            decision("request", asked="hold", answer="unable",
                     why="no holding fix")
            return Reply(p.unable_because(session.aircraft, "no_hold_fix"),
                         station)
        decision("request", asked="hold", answer="hold")
        return reply

    def _descent_asked_early(self, session, station, p, state) -> Reply | None:
        """"Request descent" before the descent is due: "expect lower in N
        minutes" well ahead of it, the descent itself nearer; None leaves
        the old answer (the descent is already under way, or no route)."""
        from .arrival import FIRST_DESCENT_KEY

        destination = session.destination
        if (not self.realism.ifr_descent or self.arrivals is None
                or destination is None or FIRST_DESCENT_KEY in session.advisories
                or state is None or not getattr(state, "has_position", False)):
            return None
        speed = float(getattr(state, "ground_speed_kt", 0.0) or 0.0)
        if speed < 100.0:
            return None
        level = max(float(state.altitude_ft or 0.0),
                    float(session.assigned_altitude_ft or 0.0))
        to_run = state.distance_to(destination.lat, destination.lon)
        to_go, start, lead = self.arrivals._descent_point(
            destination, state, level, to_run, session)
        minutes = (to_go - start - lead) / speed * 60.0
        if minutes > self.DESCENT_ASK_MIN:
            decision("request", asked="descent", answer="expect lower",
                     minutes=round(minutes))
            text = p.expect_lower(session.aircraft, int(round(minutes)))
            return Reply(text, station, instruction=session.remember(
                Instruction("expect_lower", station_key(station), text, {},
                            readback_required=False)))
        decision("request", asked="descent", answer="the descent now",
                 minutes=round(minutes))
        return self.arrivals._first_descent(
            session, station, state, destination, to_run,
            getattr(self, "weather_for", None), p, force=True)

    def _airport_named(self, parsed):
        """An airport said by its ICAO code, written or in the alphabet."""
        from .speech import _ALPHABET_REVERSE

        text = getattr(parsed, "text", "") or ""
        # Spelled first: "Lima India Romeo Alfa" is LIRA, and "Lima" on its
        # own is a word of the alphabet, not the code LIMA.
        words = re.findall(r"[a-z]+", text.lower())
        letters = [_ALPHABET_REVERSE.get(w, "") for w in words]
        for i in range(len(letters) - 3):
            group = letters[i:i + 4]
            if all(len(g) == 1 for g in group):
                found = self.navdb.airport("".join(group).upper())
                if found is not None:
                    return found
        # Then a code typed in capitals.
        for token in re.findall(r"\b[A-Z]{4}\b", text):
            found = self.navdb.airport(token)
            if found is not None:
                return found
        return None

    def _divert(self, parsed, session, station, p, state, back: bool) -> Reply:
        """Back to the field left, or on to the alternate (or the airport
        named): "cleared to (airport) via direct, maintain ...", and the
        arrival worked from here to there."""
        aircraft = session.aircraft
        if back:
            target = session.departure
        else:
            target = self._airport_named(parsed)
            alternate = getattr(self.route, "alternate", "") if self.route else ""
            if target is None and alternate:
                target = self.navdb.airport(alternate)
        if target is None:
            decision("request", asked="return" if back else "diversion",
                     answer="unable", why="no airport")
            return Reply(p.unable_because(aircraft, "no_alternate"), station)
        self._new_destination(session, target)
        altitude = float(session.assigned_altitude_ft
                         or round(float(getattr(state, "altitude_ft", 0.0) or 0.0), -2))
        session.assigned_altitude_ft = altitude
        decision("request", asked="return" if back else "diversion",
                 answer="cleared", to=target.ident, altitude_ft=int(altitude))
        text = p.cleared_to_airport(
            aircraft, target.spoken, altitude,
            transition_altitude=self._transition_altitude(target))
        instruction = session.remember(Instruction(
            "diversion", station_key(station), text,
            {"altitude_ft": int(altitude), "airport": target.spoken},
            required_items={"altitude_ft"}))
        return Reply(text, station, instruction=instruction)

    @staticmethod
    def _new_destination(session, airport) -> None:
        """Everything the arrival kept about the old destination, dropped."""
        session.destination = airport
        if not session.phase.on_ground:
            # An arrival from here on, not a climb-out: departure must not
            # hand it to the centre as one.
            session.phase = Phase.DESCENT
        session.arrival_runway = ""
        session.approach_choice, session.approach_key = None, ()
        session.vector_leg, session.vector_side, session.vector_at = "", 0.0, 0.0
        session.hold, session.hold_requested = None, False
        session.star_join, session.descend_via = "", ""
        session.speed_given, session.speed_lifted = "", False
        session.field_in_sight = False
        session.advisories = {key for key in session.advisories
                              if not key.startswith(("arrival:", "hold:"))
                              and key not in ("handoff:approach",
                                              "handoff:tower")}

    def _approve_deviation(self, parsed, session, station, p, state) -> Reply:
        """"Request deviation twenty degrees left for weather": approved,
        with the first route fix forty miles on to rejoin at (after
        LocalTC). The weather is the pilot's to see, so it is not refused;
        the off-route watch stands down until the flight is back on its
        route (atc/routing.py)."""
        from .routing import rejoin_fix

        text_heard = parsed.normalized or ""
        side = _side_said(text_heard)
        degrees = _degrees_said(text_heard, getattr(parsed, "language", "en"))
        fix = None
        if state is not None and getattr(state, "has_position", False):
            fix = rejoin_fix(self.route, self.route_progress, state)
        session.deviating = "approved"
        session.establish(station)
        decision("route", event="deviation", station=station.callsign,
                 side=side or None, degrees=degrees or None,
                 rejoin=getattr(fix, "ident", None))
        text = p.deviation_approved(session.aircraft, degrees, side,
                                    getattr(fix, "ident", ""))
        values = {"fix": fix.ident} if fix is not None else {}
        instruction = session.remember(Instruction(
            "deviation", station_key(station), text, values,
            readback_required=False))
        return Reply(text, station, instruction=instruction)

    def _direct_on_the_plan(self, parsed, session, station, p, state) -> Reply:
        """A direct asked of a centre, with a flight plan loaded.

        Granted to a fix still ahead on the route; anything else is unable.
        Without the plan the controller matched the word against every
        navaid within 250 miles and cleared the flight to whichever sounded
        nearest, which over Vietnam was a beacon in China.
        """
        from .final import course_over_ground

        aircraft = session.aircraft
        heard = (parsed.get("fix", "") or "").upper()
        best, score = None, 0.0
        for fix in self.plan_fixes:
            alike = max(_similar(heard, fix.ident),
                        _similar(heard, (fix.name or "").upper()))
            if alike > score:
                best, score = fix, alike
        if not heard or best is None or score < 0.6:
            decision("enroute_direct", station=station.callsign, fix=heard,
                     event="direct refused", granted=False,
                     reason=f"{heard} is not on the flight plan")
            if not heard:
                return Reply(p.unable_unknown_fix(aircraft), station)
            return Reply(p.unable_direct(aircraft, heard), station)
        track = course_over_ground(state) if state is not None else None
        if track is None:
            track = float(getattr(state, "heading_true", 0.0) or 0.0)
        bearing = bearing_deg(state.latitude, state.longitude, best.lat, best.lon)
        off = abs(((bearing - track) + 540.0) % 360.0 - 180.0)
        if off > 90.0:
            decision("enroute_direct", station=station.callsign,
                     fix=best.ident, event="direct refused", granted=False,
                     reason=f"{best.ident} is already behind the flight")
            return Reply(p.unable_direct(aircraft, best.ident), station)
        decision("enroute_direct", station=station.callsign, fix=best.ident,
                 event="direct granted", granted=True,
                 reason=f"{best.ident} is ahead on the flight plan")
        spoken = best.name if best.name and best.name.upper() != best.ident else ""
        text = p.direct_to(aircraft, best.ident, fix_spoken=spoken)
        instruction = session.remember(
            Instruction("direct", station_key(station), text, {},
                        readback_required=False))
        return Reply(text, station, instruction=instruction)

    def _resolve_fix(self, heard: str, state) -> tuple[str, str]:
        """Match a mis-heard fix name against real navaids nearby.

        Recognition regularly turns CAMRN into "cammen". Rather than refusing,
        the heard word is matched against navaids in the area, which is what a
        controller does when a readback is close but not exact.
        """
        heard = (heard or "").upper()
        if not heard or state is None or not getattr(state, "has_position", False):
            return heard, ""

        candidates = self.navdb.nearest_navaids(
            state.latitude, state.longitude, limit=40, max_nm=250.0
        )
        best_ident, best_name, best_score = heard, "", 0.0
        for navaid, _d in candidates:
            ident = (navaid.get("ident") or "").upper()
            name = (navaid.get("name") or "")
            score = max(_similar(heard, ident), _similar(heard, name.upper()))
            if score > best_score:
                best_ident, best_name, best_score = ident, name, score
        if best_score >= 0.6:
            return best_ident, best_name
        return heard, ""

    @staticmethod
    def _bearing_to(state, airport) -> tuple[int, float]:
        """Where a field is from the aircraft, as a clock position and a range.

        Zero and zero when the position or the heading is unknown, which reads
        as "no position given" and leaves the phrase without the clause.
        """
        if state is None or airport is None or not getattr(state, "has_position", False):
            return 0, 0.0
        # An explicit None: a heading of north is 0, and "or" took it for no
        # heading and read the magnetic one instead.
        heading = getattr(state, "heading_true", None)
        if heading is None:
            heading = getattr(state, "heading_magnetic", None)
        distance = state.distance_to(airport.lat, airport.lon)
        if heading is None:
            return 0, distance
        import math

        lat1 = math.radians(state.latitude)
        lat2 = math.radians(airport.lat)
        dlon = math.radians(airport.lon - state.longitude)
        y = math.sin(dlon) * math.cos(lat2)
        x = (math.cos(lat1) * math.sin(lat2)
             - math.sin(lat1) * math.cos(lat2) * math.cos(dlon))
        bearing = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
        relative = (bearing - float(heading) + 360.0) % 360.0
        oclock = int(round(relative / 30.0)) or 12
        return (oclock if oclock <= 12 else oclock - 12), distance

    @staticmethod
    def _distance_to(state, airport) -> float:
        """How far the aircraft is from a field, in nautical miles.

        Zero when either is unknown, which reads as "at the field" and keeps
        the no-simulator case working the way it always has.
        """
        if state is None or airport is None or not getattr(state, "has_position", False):
            return 0.0
        return state.distance_to(airport.lat, airport.lon)

    def _nearest_airport(self, state):
        if state is None or not getattr(state, "has_position", False):
            return None
        return self.navdb.nearest_airport(
            state.latitude, state.longitude, max_nm=60.0, min_rank=1
        )

    def _destination_tower(self, session, station) -> Station | None:
        target = session.destination
        if target is None:
            return None
        return self.controller_at(target.ident, "TWR",
                                  runway=session.arrival_runway)


# How far from the field it left a flight can still be a departure whatever
# the phase tracker says: the tracker calls it en route at ten thousand feet.
DEPARTING_WITHIN_NM = 40.0


def _airborne(state) -> bool:
    """Whether the aircraft is flying.

    ``None`` means no simulator, and an aircraft nobody can see is treated as
    being on the ground: that is where a flight starts, and refusing a taxi
    clearance to somebody testing the radio without a sim would be worse than
    the mistake this exists to prevent.
    """
    return state is not None and not getattr(state, "on_ground", True)


def _similar(a: str, b: str) -> float:
    """Cheap similarity for matching a mis-heard identifier."""
    if not a or not b:
        return 0.0
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio()
