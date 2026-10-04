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

* **Taxi routes are synthetic.** There is no free worldwide taxiway graph, so
  the route is a deterministic, plausible sequence of taxiway identifiers
  rather than the real layout of the airport. The phraseology is correct and
  the same route is issued every time for a given airport and runway, but do
  not expect it to match the airport diagram.

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
                          transition_level)
from ..logs import decision
from .approaches import VISUAL, FieldApproaches, asked_for, choose
from .intents import Intent, ParsedIntent
from .phraseology import Phraseology, Weather, language_of
from .session import (ATTEMPTS_BEFORE_REISSUE, PROMPTS_BEFORE_REISSUE,
                      FlightSession, Instruction, Phase, station_key)

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


# Taxiway identifiers used to build synthetic routes. Real airports use these
# letters; the sequence is invented, the alphabet is not.
_TAXIWAYS = ("A", "B", "C", "D", "E", "F", "G", "H", "J", "K", "L", "M",
             "N", "P", "Q", "R", "S", "T", "U", "V", "W", "Y", "Z")


def _stable(*parts: str) -> int:
    return int.from_bytes(
        hashlib.sha256("|".join(parts).encode("utf-8")).digest()[:6], "big"
    )


def taxi_route(airport_ident: str, runway: str, inbound: bool = False) -> list[str]:
    """A deterministic, plausible taxi route.

    Synthetic -- see the module docstring. Determinism is what matters here:
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
        # The flight plan's runways and SID, by airport: {"KJFK":
        # {"departing": "31L", "sid": "SKORR5"}, "KBOS": {"landing": "04R"}}.
        # Used whenever the wind allows them, never against it.
        self.plan_runways: dict[str, dict[str, str]] = {}
        self.phrase = phraseology or Phraseology("faa")
        self.strict_icao_digits = (
            strict_icao_digits or getattr(self.phrase, "strict_icao_digits", False)
        )
        # Phraseology objects are cheap but not free; one per language and
        # facility is plenty and keeps them out of the hot path.
        self._phraseology: dict[tuple[str, str], object] = {}

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
        return (station is not None and station.is_controller
                and not self.closed(station))

    def for_station(self, station: Station, language: str = "en"):
        """Phraseology for a station, in the language being spoken.

        Two independent axes meet here. The *language* is whatever the pilot
        used; the *dialect* is where the facility is, so English at Orly is
        ICAO English rather than US English.
        """
        from .language import phraseology_for

        language = (language or "en").lower()[:2]
        ident = station.ident if station else ""
        cached = self._phraseology.get((language, ident))
        if cached is None:
            cached = phraseology_for(language, ident, self.strict_icao_digits)
            self._phraseology[(language, ident)] = cached
        return cached

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

        handler = {
            "DEL": self._delivery,
            "GND": self._ground,
            "TWR": self._tower,
            "DEP": self._radar,
            "APP": self._radar,
            "CTR": self._radar,
            "CTAF": self._ctaf,
        }.get(station.position)

        if handler is None:
            return Reply("", station, silent=True)

        # Two-way contact is only established *after* this transmission has
        # been handled: the reply to a first call uses the full callsign and,
        # for a radar position, opens with "radar contact".
        reply = handler(parsed, session, station, weather, p, state)
        session.establish(station)
        if reply.instruction is not None and not reply.instruction.from_intent:
            reply.instruction.from_intent = parsed.intent.value
        return reply

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
        return self._insist(session, station, p)

    def _check_readback(
        self, parsed: ParsedIntent, session: FlightSession,
        station: Station, p: Phraseology, state=None,
    ) -> Reply:
        pending = session.pending
        assert pending is not None
        wrong = pending.mismatches(parsed.readback_items)

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
            self._note_rules(session, parsed)
            if not session.ifr:
                return self._issue_vfr_release(session, station, p, parsed)
            return self._issue_clearance(session, station, weather, p)

        if parsed.intent is Intent.REQUEST_VFR_DEPARTURE:
            return self._issue_vfr_release(session, station, p, parsed)

        if parsed.intent is Intent.REQUEST_PATTERN_WORK:
            # Circuits are a tower matter, and the aircraft has to taxi first.
            session.ifr = False
            session.pattern_work = True
            ground = self.controller_at(station.ident, "GND")
            if ground and ground.mhz != station.mhz:
                return Reply(
                    p.handoff(aircraft,
                              self._station_name(ground, p, short=True),
                              ground.mhz),
                    station, handoff_to=ground,
                )
            return self._issue_taxi(session, station, weather, p)

        if parsed.intent is Intent.REQUEST_TAXI:
            ground = self.controller_at(station.ident, "GND")
            if ground and ground.mhz != station.mhz:
                return Reply(
                    p.handoff(aircraft, self._station_name(ground, p, short=True), ground.mhz),
                    station, handoff_to=ground,
                )
            return self._issue_taxi(session, station, weather, p)

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
        initial = session.initial_altitude_ft or 5000.0

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
        text = p.ifr_clearance(
            session.aircraft,
            self._station_name(station, p) if not session.established_with(station) else "",
            dest_name,
            departure_procedure=sid,
            route=p.ROUTE_AS_FILED if sid else p.ROUTE_RADAR_VECTORS,
            initial_altitude=initial,
            cruise_altitude=cruise,
            departure_freq=departure_freq,
            squawk=session.squawk,
            transition_altitude=self._transition_altitude(airport),
        )

        session.clearance_issued = True
        values: dict[str, Any] = {"altitude_ft": int(initial), "squawk": session.squawk}
        if departure_freq:
            values["frequency"] = departure_freq
        # A clearance readback that gets the altitude and the code right is
        # accepted. Pilots routinely leave the departure frequency out of the
        # readback and no controller challenges it.
        instruction = session.remember(
            Instruction("ifr_clearance", station_key(station), text, values,
                        required_items={"altitude_ft", "squawk"})
        )
        return Reply(text, station, instruction=instruction)

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

    @staticmethod
    def _note_rules(session, parsed) -> None:
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
        file.
        """
        if parsed.rules == "ifr":
            session.ifr = True
        elif parsed.rules == "vfr":
            session.ifr = False

    def _issue_vfr_release(self, session, station, p, parsed=None) -> Reply:
        """Release a VFR flight, in the direction it asked to go.

        Not a clearance. It is an approval to leave, a ceiling while it is
        under somebody else's traffic, and a code -- and the code is the
        conspicuity one unless a radar position is going to work the flight,
        because nobody hands a discrete code to an aircraft they will never
        see.
        """
        session.ifr = False
        airport = self.navdb.airport(station.ident)
        departure_station = (
            self.navdb.station_for(station.ident, "DEP")
            or self.navdb.station_for(station.ident, "APP")
        )
        freq = departure_station.mhz if departure_station else 0.0
        session.squawk = (
            squawk_for(session, station.ident) if freq else p.vfr_squawk
        )

        direction = p.bearing(self._direction_asked(parsed))
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
        )
        values: dict[str, Any] = {"squawk": session.squawk}
        if freq:
            values["frequency"] = freq
        instruction = session.remember(
            Instruction("vfr_release", station_key(station), text, values)
        )
        return Reply(text, station, instruction=instruction)

    # ------------------------------------------------------------------
    # VFR helpers
    # ------------------------------------------------------------------

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
        return ""

    def _vfr_ceiling(self, airport) -> float:
        """A level to keep a VFR departure at or below, in feet AMSL."""
        elevation = float(getattr(airport, "elev_ft", 0.0) or 0.0)
        return round((elevation + self.VFR_CEILING_AGL_FT) / 500.0) * 500.0

    @staticmethod
    def _airspace_class(airport, station) -> str:
        """Which class of airspace a facility owns.

        No open dataset publishes airspace boundaries, so this follows the
        size of the field and the position being worked, which gets the common
        cases right: a tower-only field is a Delta, a radar field is a Charlie,
        and a large scheduled airport is a Bravo. The name it turns into is the
        language's own -- a French controller says "la zone de contrôle", not
        "Class Delta".
        """
        rank = int(getattr(airport, "size_rank", 0) or 0)
        if station is not None and station.position in ("APP", "DEP", "CTR"):
            return "B" if rank >= 3 else "C"
        return "B" if rank >= 3 else "D"

    def _pattern_direction(self, session, parsed, p) -> str:
        """Which way round the circuit goes, as this language says it."""
        asked = (parsed.get("pattern_direction") if parsed else "") or ""
        if asked in ("left", "right"):
            session.pattern_direction = asked
        return p.side(session.pattern_direction)

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
            return self._issue_vfr_release(session, station, p, parsed)

        if parsed.intent is Intent.REQUEST_CLEARANCE:
            # A VFR flight has nothing to collect from delivery. Sending it
            # there to be read an airways clearance it did not file is the
            # wrong half of the phrasebook and a wasted frequency change.
            self._note_rules(session, parsed)
            if not session.ifr:
                return self._issue_vfr_release(session, station, p, parsed)
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
                return self._issue_taxi_in(session, station, p)
            return self._issue_taxi(session, station, weather, p)

        if parsed.intent is Intent.REQUEST_TAXI_TO_PARKING:
            return self._issue_taxi_in(session, station, p)

        if parsed.intent is Intent.REQUEST_PATTERN_WORK:
            # Circuits are the tower's to approve; ground's job is to get the
            # aircraft to the runway, which is what it asked for in effect.
            session.ifr = False
            session.pattern_work = True
            return self._issue_taxi(session, station, weather, p)

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

    def _issue_taxi(self, session, station, weather, p) -> Reply:
        airport = self.navdb.airport(station.ident)
        runway = session.departure_runway or self._departure_runway(airport, weather)
        session.departure_runway = runway

        route = taxi_route(station.ident, runway)
        session.taxi_route = route

        # Hold short of a runway that is not the assigned one, when the field
        # has more than one. Crossing clearances are issued separately.
        hold_short = self._hold_short_candidate(airport, runway)
        session.hold_short_of = hold_short

        altimeter = (
            weather.altimeter_inhg if p.dialect == "faa" else weather.qnh_hpa
        )
        text = p.taxi_to_runway(
            session.aircraft,
            self._station_name(station, p) if not session.established_with(station) else "",
            runway, route, hold_short=hold_short,
            altimeter=altimeter if not session.clearance_issued else 0.0,
            abbreviated=session.established_with(station),
        )
        session.phase = Phase.TAXI_OUT

        values: dict[str, Any] = {"runway": runway}
        if hold_short:
            values["hold_short"] = hold_short
        instruction = session.remember(
            Instruction("taxi", station_key(station), text, values)
        )
        return Reply(text, station, instruction=instruction)

    def _issue_taxi_in(self, session, station, p) -> Reply:
        route = taxi_route(station.ident, session.arrival_runway or "01", inbound=True)
        text = p.taxi_to_parking(
            session.aircraft,
            self._station_name(station, p) if not session.established_with(station) else "",
            route, p.PARKING,
            abbreviated=session.established_with(station),
        )
        session.phase = Phase.TAXI_IN
        instruction = session.remember(
            Instruction("taxi_in", station_key(station), text, {},
                        readback_required=False)
        )
        return Reply(text, station, instruction=instruction)

    def _hold_short_candidate(self, airport, runway: str) -> str:
        """A runway the taxi route would plausibly cross."""
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
            if not candidate[:2].isdigit():
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

        if parsed.intent is Intent.REQUEST_TAKEOFF:
            if airborne:
                return Reply(p.already_airborne(aircraft), station)
            return self._issue_takeoff(session, station, weather, p, parsed)

        if parsed.intent is Intent.REQUEST_PATTERN_WORK:
            session.ifr = False
            session.pattern_work = True
            if not airborne:
                return self._issue_takeoff(session, station, weather, p, parsed)
            return self._work_the_circuit(session, station, weather, p, parsed,
                                          state)

        if parsed.intent is Intent.REQUEST_TRANSITION:
            return self._issue_transition(session, station, weather, p, parsed,
                                          state)

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
            if not session.ifr:
                return self._work_the_circuit(session, station, weather, p,
                                              parsed, state)
            # Airborne, so the phase tracker's opinion is beside the point: an
            # aircraft in the circuit is outbound and inbound at once, and it
            # is asking to land.
            return self._issue_landing(session, station, weather, p, parsed, state)

        if parsed.intent is Intent.CHECK_IN:
            if not session.ifr and airborne:
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
            return self._issue_taxi_in(session, station, p)

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

    def landing_on_top(self):
        """The arrival too close for anybody else to be sent down the runway."""
        return next((f for f in self._traffic_ahead(True)
                     if f.phase == "final"
                     and f.distance_nm <= self.RUNWAY_CONFLICT_NM), None)

    def runway_available(self) -> bool:
        """Whether the runway is free enough to clear somebody for takeoff.

        Public because the tower has to answer this question twice: once when
        the pilot asks, and again on its own, for an aeroplane it has already
        put on the runway and told to wait. See
        :class:`~wilcoatc.atc.departure.DepartureWatch`.
        """
        return not self._runway_blocked() and self.landing_on_top() is None

    def _runway_blocked(self) -> bool:
        """Whether somebody else is on the runway right now."""
        if self.traffic is None:
            return False
        try:
            return self.traffic.runway_is_busy(time.monotonic())
        except Exception:
            return False

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

        # The runway is one aeroplane wide. If somebody else is rolling on it,
        # or is about to land on it, the user is lined up and held rather than
        # cleared -- which is the whole of what a tower does with a departure
        # queue, and it is why a takeoff clearance sometimes takes a minute.
        landing = self.landing_on_top()
        if self._runway_blocked() or landing is not None:
            if session.lined_up:
                # Already on the runway and still waiting. Asking again does
                # not make the aeroplane in front of you land any sooner.
                return Reply(p.hold_position(session.aircraft), station)
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
            # Staying in the circuit, so the clearance says which way round it
            # goes instead of sending the aircraft off on runway heading.
            text = p.closed_traffic_approved(
                session.aircraft, runway, p.side(session.pattern_direction),
                weather, station=station_name,
                abbreviated=session.established_with(station),
            )
        else:
            text = p.cleared_for_takeoff(
                session.aircraft, runway, weather,
                station=station_name,
                after_departure=p.RUNWAY_HEADING if session.ifr else "",
                abbreviated=session.established_with(station),
            )
        session.cleared_takeoff = True
        session.phase = Phase.TAKEOFF
        session.allow_runway(runway)
        instruction = session.remember(
            Instruction("takeoff_clearance", station_key(station), text,
                        {"runway": runway})
        )
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
    # A landing clearance is issued from base or final, or from inside this.
    PATTERN_FINAL_NM = 3.0

    def _work_the_circuit(self, session, station, weather, p, parsed,
                          state=None) -> Reply:
        """Answer a VFR aircraft in, or joining, the traffic pattern."""
        aircraft = session.aircraft
        airport = self.navdb.airport(station.ident)
        runway = (
            session.arrival_runway
            or parsed.get("runway")
            or self._arrival_runway(airport, weather)
        )
        session.arrival_runway = runway
        session.allow_runway("")

        direction = self._pattern_direction(session, parsed, p)
        leg = (parsed.get("pattern_leg") or "").lower()
        if leg:
            session.pattern_leg = leg
        distance = self._distance_to(state, airport)
        first_call = not session.established_with(station)
        station_name = self._station_name(station, p) if first_call else ""

        # Who is in front. A circuit is sequenced by eye, so the number and the
        # aeroplane to follow are the whole of the instruction.
        ahead = [f for f in self._traffic_ahead(True)
                 if not distance or f.distance_nm < distance]
        sequence = ""
        if ahead:
            sequence = p.sequence_number(
                len(ahead) + 1, ahead[-1].describe(language_of(p)))

        # --- on base or final: this is the landing clearance ---------------
        on_final = leg in ("base", "final") or (
            distance and distance <= self.PATTERN_FINAL_NM)
        if on_final and not session.cleared_landing:
            return self._clear_the_circuit(session, station, weather, p,
                                           runway, sequence, station_name)

        # --- downwind: sequence them and give them the next report ---------
        if leg in ("downwind", "crosswind", "upwind"):
            session.phase = Phase.APPROACH
            if ahead:
                text = p.extend_downwind(
                    aircraft, ahead[-1].describe(language_of(p)))
            else:
                text = p.report_position(aircraft, p.REPORT_TURNING_BASE)
            instruction = session.remember(
                Instruction("circuit_report", station_key(station), text, {},
                            readback_required=False))
            return Reply(text, station, instruction=instruction)

        # --- everything else is a join -------------------------------------
        session.phase = Phase.APPROACH
        straight_in = bool(distance) and distance > self.PATTERN_JOIN_NM * 2
        text = p.enter_pattern(
            aircraft, station_name, runway,
            leg=p.leg("downwind"), direction=direction,
            report=p.REPORT_MIDFIELD_DOWNWIND,
            weather=weather if first_call else None,
            sequence=sequence, straight_in=straight_in,
        )
        instruction = session.remember(
            Instruction("pattern_entry", station_key(station), text,
                        {"runway": runway,
                         "pattern_direction": session.pattern_direction}))
        return Reply(text, station, instruction=instruction)

    def _clear_the_circuit(self, session, station, weather, p, runway,
                           sequence, station_name) -> Reply:
        """The clearance at the end of a circuit.

        An aircraft doing circuits gets "cleared for the option", which covers
        the touch-and-go, the low approach and the full stop without the tower
        having to know which one the pilot has decided on. One that is coming
        in to land gets a landing clearance.
        """
        aircraft = session.aircraft
        abbreviated = bool(session.established_with(station))
        if session.pattern_work:
            text = p.cleared_touch_and_go(
                aircraft, runway, weather, sequence=sequence,
                station=station_name, abbreviated=abbreviated)
            kind = "option_clearance"
            session.cleared_option = True
        else:
            text = p.cleared_to_land(
                aircraft, runway, weather, station=station_name,
                sequence=sequence, abbreviated=abbreviated)
            kind = "landing_clearance"
            session.cleared_landing = True
        session.phase = Phase.LANDING
        session.allow_runway(runway)
        instruction = session.remember(
            Instruction(kind, station_key(station), text, {"runway": runway}))
        return Reply(text, station, instruction=instruction)

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
        session.ifr = False
        text = p.cleared_through_airspace(
            aircraft, airspace, altitude_ft=ceiling,
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

        text = p.cleared_to_land(
            session.aircraft, runway, weather,
            station=self._station_name(station, p) if not session.established_with(station) else "",
            sequence=sequence,
            abbreviated=session.established_with(station),
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

    def approach_for(self, session, airport, runway: str) -> str:
        """The approach to name for this runway: the one asked for if the
        runway has it, else the best it has, else a visual one -- never an
        ILS the simulator says is not there."""
        known = self.approach_data.get(getattr(airport, "ident", "") or "")
        available = known.types(runway) if known is not None else None
        requested = getattr(session, "requested_approach", "") if session else ""
        return choose(available, requested)

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
        value = known.altitudes(runway, kind)[which]
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
        session.cleared_landing = False
        session.going_around = True
        session.final_low_ft = 0.0
        session.climb_since = 0.0
        session.phase = Phase.APPROACH
        session.advisories.difference_update({
            "arrival:cleared_approach", "handoff:tower", "handoff:missed"})
        # The runway stays assigned: a go-around is not a runway change, and
        # the aircraft will be re-sequenced to the same one.
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

        if parsed.intent is Intent.REQUEST_TRANSITION:
            return self._issue_transition(session, station, weather, p, parsed,
                                          state)

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

    def _issue_flight_following(self, session, station, p, state) -> Reply:
        """VFR radar advisories, which are asked for and can be refused.

        FAA JO 7110.65 2-1-1: additional services are provided workload
        permitting. Busy means busy, so a sector already sequencing arrivals
        says so instead of pretending it has the capacity.
        """
        aircraft = session.aircraft
        session.ifr = False
        if len(self._traffic_ahead(True)) >= 3:
            return Reply(p.flight_following_unavailable(aircraft), station)

        session.flight_following = True
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

        # "Request RNAV two five left": the type, and the runway if one was
        # named and the field has it.
        asked = asked_for(parsed.normalized or parsed.text) if parsed else ""
        if asked:
            session.requested_approach = asked
        named = parsed.get("runway") if parsed else ""
        if named and self.navdb.has_runway(destination.ident, named):
            session.arrival_runway = named
        runway = session.arrival_runway or self._arrival_runway(destination, weather)
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

        text = p.expect_approach(
            aircraft, p.approach_name(self.approach_for(session, destination, runway)),
            runway, destination.spoken)
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
        best = self.navdb.best_runway(
            airport.ident, weather.wind_dir, weather.wind_kt, departing=True,
            variable=weather.variable, gust_kt=weather.gust_kt,
        )
        return best[0] if best else "01"

    def _arrival_runway(self, airport, weather) -> str:
        if airport is None:
            return "01"
        planned = self._planned(airport, weather, "landing")
        if planned:
            return planned
        best = self.navdb.best_runway(
            airport.ident, weather.wind_dir, weather.wind_kt,
            variable=weather.variable, gust_kt=weather.gust_kt,
        )
        return best[0] if best else "01"

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
        if session.aircraft.callsign.is_airline:
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
