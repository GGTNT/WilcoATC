"""Controller phraseology in a language, from a phrasebook.

The English and French classes are written out by hand, because they carry the
two dialects the rest of the system is built around and both are large enough
to deserve their own file. Doing that a fifth and sixth time would be a mistake
of a different kind: six copies of the same eighty-six methods, differing only
in the words, is six places for the assembly to drift apart.

So the assembly lives here once, and each language supplies a :class:`Phrasebook`
-- the fragments, the units, and the handful of places where word order really
does differ. Nothing is generated: every fragment is still a fixed string
written by a person, which is the whole point of this package. It is just
stored as data rather than as code.

What a phrasebook has to get right beyond translation:

*Which numbers are read as digits and which as whole numbers.* Runways,
headings, squawks and flight levels are digits everywhere. Altitudes in feet,
the pressure setting, speeds and the megahertz of a frequency are whole
numbers. See :mod:`wilcoatc.atc.numbers`.

*The pressure setting.* Every language here uses QNH in hectopascals, because
every country that speaks them does. The inches-of-mercury altimeter setting is
a US thing and belongs to the English class.

*The leading zero on a runway.* "Runway 4 right" is US; everywhere else it is
"runway zero four right".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from . import numbers as num
from . import speech as sp
from .phraseology import (Aircraft, Weather, _join, _pick, _sentence,
                          without_callsign)


@dataclass(frozen=True)
class Phrasebook:
    """Everything one language needs to work a frequency."""

    language: str
    name: str

    # What each thing is called. Keys are listed in PHRASE_KEYS below, and a
    # test asserts every book has every one of them.
    say: dict[str, str]

    # Runway suffixes, and the words for the units.
    runway_suffix: dict[str, str]
    unit_feet: str
    unit_knots: str
    unit_miles: str
    unit_degrees: str
    unit_minutes: str
    unit_minute: str
    flight_level: str
    sea_level: str

    # What a circuit leg is called, keyed on the English name the decision
    # layer uses, and what the airspace round a facility is called, keyed on
    # its ICAO class. Every language here names the volume -- "la CTR", "die
    # Kontrollzone" -- rather than the class, which is a US convention.
    legs: dict[str, str] = field(default_factory=dict)
    airspaces: dict[str, str] = field(default_factory=dict)
    # The compass, for "north departure approved".
    bearings: dict[str, str] = field(default_factory=dict)

    # The conspicuity code a VFR flight squawks. Europe uses 7000; the
    # Americas use 1200.
    vfr_squawk: str = "7000"
    # Where altitudes become flight levels, in feet.
    transition_altitude: int = 5000
    # Farewells, chosen deterministically so one controller is consistent.
    farewells: tuple[str, ...] = ()
    left: str = "left"
    right: str = "right"
    # The singular of a mile, where the language has one worth saying.
    unit_mile: str = ""

    # A count agrees with the noun it counts in every language here except
    # English. "Dos millas" but "una milla"; "dois pés" but "duas milhas"; "due
    # miglia" but "un miglio". The spellers produce the citation form, so a
    # language that needs another one says which words change, and whether a
    # mile is one of the nouns that asks for it.
    agreeing_forms: tuple[tuple[str, str], ...] = ()
    miles_take_agreement: bool = False

    def word(self, key: str) -> str:
        return self.say[key]


# Every fragment a book must supply. Listed rather than inferred so a missing
# one is a failing test rather than a KeyError in the middle of a clearance.
PHRASE_KEYS: tuple[str, ...] = (
    # the weather
    "wind", "gusting",
    # clearance delivery
    "cleared_to", "via_procedure", "via_transition", "via_then", "via_route",
    "climb_initially", "expect_after", "departure_frequency", "squawk",
    "vfr_leave_airspace", "vfr_direction_approved", "vfr_approved",
    "maintain_vfr_at_or_below", "readback_correct", "negative",
    "contact_when", "contact_station_when", "when_ready_to_taxi",
    # ground
    "taxi_via", "taxi_to_the_runway", "holding_point", "hold_short",
    "taxi_to_parking_via", "taxi_to_parking", "cross_runway", "hold_position",
    "continue_via", "continue_taxi", "give_way", "then_continue",
    "pushback_approved", "report_ready_to_taxi",
    # tower
    "runway_is", "line_up_and_wait", "cleared_takeoff", "cleared_to_land",
    "cleared_option", "go_around", "continue_approach", "number", "follow",
    "enter_pattern", "report", "extend_downwind", "i_will_call_your_base",
    "cleared_low_approach",
    # radar
    "radar_contact", "radar_contact_lost", "climb_to", "descend_to",
    "descend_discretion", "maintain_altitude", "cross_at", "at_or_above",
    "at_or_below", "turn_heading", "fly_heading", "direct_to",
    "resume_own_navigation", "maintain_speed", "reduce_speed", "or_greater",
    "or_less", "resume_normal_speed", "traffic", "oclock", "at_distance",
    "altitude_unknown", "traffic_no_factor", "maintain_visual_separation",
    "follow_that_traffic",
    # approach
    "expect_approach", "at_airport", "distance_from", "maintain_until_established",
    "cleared_approach", "cleared_straight_in_approach", "cleared_visual",
    "cleared_visual_at", "report_field_in_sight", "field_at",
    "descend_via", "climb_via", "except_maintain",
    "hold_at", "on_radial", "turns_to", "legs_of", "expect_further_clearance",
    # handoffs
    "contact", "monitor", "remain_this_frequency", "frequency_change_approved",
    "welcome_to",
    "radar_service_terminated", "ifr_cancellation_received",
    # naming the call the controller is waiting for
    "expecting", "or_else",
    # insisting on a readback
    "read_back_all", "read_back_item", "read_back_hold_short", "i_say_again",
    "item_hold_short", "item_runway", "item_altitude", "item_heading",
    "item_squawk", "item_frequency", "item_speed",
    # VFR: the circuit, the airspace, the levels
    "enter_leg", "straight_in", "closed_traffic_approved",
    "make_short_approach", "turn_out_approved", "on_course_approved",
    "maintain_vfr", "maintain_vfr_at_or_above", "altitude_your_discretion",
    "cleared_through", "remain_clear_of", "clear_of", "miles_final",
    "flight_following_unavailable", "midfield_downwind", "turning_base",
    "entering_downwind",
    # general
    "ident", "say_again", "say_again_item", "roger", "standby", "unable",
    "verify", "read_you", "how_do_you_hear", "read_back",
    "maintain_present_altitude", "say_again_the_fix", "say_your_destination",
    # the aircraft and the request do not agree
    "you_are_airborne", "you_are_on_the_ground", "say_intentions",
    "already_cleared_takeoff", "already_cleared_to_land",
    # what the controller says unprompted
    "hold_position_now", "entered_runway_without_clearance",
    "departed_without_clearance", "departed_without_clearance_no_runway",
    "landed_without_clearance", "landed_without_clearance_no_runway",
    "report_will_be_filed", "call_after_flight", "call_after_parking",
    # emergencies
    "state_nature_of_emergency", "souls_and_fuel", "you_have_priority",
    "expect_runway", "equipment_standing_by", "the_airport_is_yours",
    # fragments the decision layer passes back in
    "route_radar_vectors", "runway_heading", "parking", "unknown_destination",
    "default_approach",
)


class LocalisedPhraseology:
    """Builds controller transmissions from a phrasebook.

    A drop-in for :class:`wilcoatc.atc.phraseology.Phraseology`: same methods,
    same signatures, checked by the same parity test the French class passes.
    """

    def __init__(self, book: Phrasebook, strict_icao_digits: bool = False,
                 transition: int = 0):
        self.book = book
        self.language = book.language
        self.dialect = book.language
        # Accepted for interface compatibility. The "tree/fower/fife" question
        # is an English one; these languages have no equivalent.
        self.strict_icao_digits = strict_icao_digits
        # Where the region says altitudes become flight levels, falling back
        # to the phrasebook's own figure. The book's is a fact about where
        # the language is spoken; this is a fact about where the aeroplane
        # is, and the second one wins when it is known.
        self.transition = int(transition) or book.transition_altitude
        self.digit_dialect = book.language

    # ------------------------------------------------------------------
    # primitives
    # ------------------------------------------------------------------

    def _(self, key: str) -> str:
        return self.book.say[key]

    def d(self, value) -> str:
        return num.digits(value, self.book.language)

    def n(self, value) -> str:
        return num.cardinal(int(value), self.book.language)

    def alt(self, feet: float, transition: int = 0, level_from: int = 0) -> str:
        """Feet as a whole number, or a flight level above the transition."""
        limit = transition or self.transition
        ft = int(round(float(feet) / 100.0) * 100)
        if sp.is_flight_level(ft, limit, level_from):
            return f"{self.book.flight_level} {self.d(f'{ft // 100:03d}')}"
        if ft <= 0:
            return self.book.sea_level
        return f"{self.n(ft)} {self.book.unit_feet}"

    def freq(self, mhz: float) -> str:
        """The megahertz as a whole number, the decimals as digits."""
        text = f"{float(mhz):.3f}" if not isinstance(mhz, str) else mhz
        whole, _, frac = text.partition(".")
        frac = (frac + "000")[:3]
        spoken = self.d(frac if frac[2] != "0" else (frac[:2].rstrip("0") or "0"))
        return f"{self.n(int(whole))} {num.DECIMAL_WORD[self.book.language]} {spoken}"

    def hdg(self, degrees: float) -> str:
        deg = int(round(float(degrees))) % 360
        return self.d(f"{deg or 360:03d}")

    def rwy(self, ident: str) -> str:
        """Two digits and a side, with the leading zero every ICAO state uses."""
        from ..navdata.db import normalize_runway

        text = normalize_runway(ident)
        number, suffix = text[:2], text[2:]
        spoken = self.d(number)
        side = self.book.runway_suffix.get(suffix.upper(), "")
        return f"{spoken} {side}".strip()

    def pressure(self, value: float) -> str:
        return f"QNH {self.n(round(float(value)))}"

    def speed(self, knots: float) -> str:
        return f"{self.n(round(float(knots)))} {self.book.unit_knots}"

    def _count(self, value: int, agreeing: bool = False) -> str:
        """A number in words, agreeing with the noun it is about to count."""
        text = self.n(value)
        if not agreeing:
            return text
        for citation, other in self.book.agreeing_forms:
            if text.endswith(citation):
                return text[: len(text) - len(citation)] + other
        return text

    def distance(self, nm: float) -> str:
        count = int(round(float(nm)))
        unit = self.book.unit_miles
        if count == 1 and self.book.unit_mile:
            unit = self.book.unit_mile
        return f"{self._count(count, self.book.miles_take_agreement)} {unit}"

    def wind(self, weather: Weather) -> str:
        """Direction digit by digit, speed as a whole number.

        Magnetic, like every spoken wind. A variable wind and a calm one
        have words of their own, which were "zero zero zero degrees" -- a
        wind from the north.
        """
        from .phraseology import spoken_wind_direction

        if int(round(weather.wind_kt)) == 0:
            return self._("wind_calm")
        heading = spoken_wind_direction(weather)
        if heading == "VRB":
            body = f"{self._('wind_variable')}, {self.speed(weather.wind_kt)}"
        else:
            # North is 360, never 000.
            direction = self.d(f"{int(round(heading)) % 360 or 360:03d}")
            body = (f"{self._('wind')} {direction} {self.book.unit_degrees}, "
                    f"{self.speed(weather.wind_kt)}")
        if weather.gust_kt:
            body += f", {self._('gusting')} {self.speed(weather.gust_kt)}"
        return body

    def addressed(self, aircraft: Aircraft, station: str = "",
                  abbreviated: bool = False) -> str:
        who = aircraft.spoken(abbreviated)
        return _join(who, station) if station else who

    def _fix(self, fix: str, spoken: str = "") -> str:
        if spoken:
            return spoken
        ident = (fix or "").strip().upper()
        if not ident:
            return ""
        if len(ident) <= 3 or not ident.isalpha():
            return sp.letters(ident)
        return ident.capitalize()

    # ------------------------------------------------------------------
    # clearance delivery
    # ------------------------------------------------------------------

    def ifr_clearance(
        self, aircraft: Aircraft, station: str, destination: str, *,
        departure_procedure: str = "", transition: str = "",
        route: str = "", initial_altitude: float = 5000,
        cruise_altitude: float = 0, expect_minutes: int = 10,
        departure_freq: float = 0.0, squawk: str = "1200",
        transition_altitude: int = 0,
    ) -> str:
        route = route or self._("route_radar_vectors")
        if departure_procedure:
            via = self._("via_procedure").format(procedure=departure_procedure)
            if transition:
                via += self._("via_transition").format(transition=transition)
            via += self._("via_then").format(route=route)
        else:
            via = self._("via_route").format(route=route)

        parts = [
            self.addressed(aircraft, station),
            self._("cleared_to").format(destination=destination, via=via),
            self._("climb_initially").format(
                altitude=self.alt(initial_altitude, transition_altitude)),
        ]
        if cruise_altitude and cruise_altitude > initial_altitude:
            parts.append(self._("expect_after").format(
                altitude=self.alt(cruise_altitude, transition_altitude),
                minutes=self.n(expect_minutes)))
        if departure_freq:
            parts.append(self._("departure_frequency").format(
                frequency=self.freq(departure_freq)))
        parts.append(self._("squawk").format(code=self.d(f"{int(squawk):04d}")))
        return _join(*parts) + "."

    def vfr_departure_clearance(
        self, aircraft: Aircraft, station: str, *, airspace: str = "",
        departure_freq: float = 0.0, squawk: str = "1200",
        altitude_restriction: float = 0.0, direction: str = "",
    ) -> str:
        parts = [self.addressed(aircraft, station)]
        if airspace:
            body = self._("vfr_leave_airspace").format(airspace=airspace)
            if direction:
                body += ", " + self._("vfr_direction_approved").format(
                    direction=direction)
            parts.append(body)
        elif direction:
            parts.append(self._("vfr_direction_approved").format(direction=direction))
        else:
            parts.append(self._("vfr_approved"))
        if altitude_restriction:
            parts.append(self._("maintain_vfr_at_or_below").format(
                altitude=self.alt(altitude_restriction)))
        if departure_freq:
            parts.append(self._("departure_frequency").format(
                frequency=self.freq(departure_freq)))
        parts.append(self._("squawk").format(code=self.d(f"{int(squawk):04d}")))
        return _join(*parts) + "."

    def readback_correct(self, aircraft: Aircraft, *, next_station: str = "",
                         next_freq: float = 0.0, when: str = "") -> str:
        when = when or self._("when_ready_to_taxi")
        parts = [aircraft.spoken(True), self._("readback_correct")]
        if next_station and next_freq:
            parts.append(self._("contact_when").format(
                station=next_station, frequency=self.freq(next_freq), when=when))
        elif next_station:
            parts.append(self._("contact_station_when").format(
                station=next_station, when=when))
        return _join(*parts) + "."

    def readback_incorrect(self, aircraft: Aircraft, correction: str) -> str:
        return _join(aircraft.spoken(True), self._("negative"), correction) + "."

    # A readback is not a courtesy. ICAO Annex 10 II 5.2.1.9.3 makes it
    # mandatory for the safety-related part of a clearance, so the controller
    # names what it is still owed rather than moving on without it.
    _READBACK_ITEM_KEYS = {
        "hold_short": "item_hold_short", "runway": "item_runway",
        "altitude_ft": "item_altitude", "heading": "item_heading",
        "squawk": "item_squawk", "frequency": "item_frequency",
        "speed_kt": "item_speed",
    }

    @property
    def READBACK_ITEM_NAMES(self) -> dict[str, str]:
        return {key: self._(name) for key, name in self._READBACK_ITEM_KEYS.items()}

    def read_back_instruction(self, aircraft: Aircraft,
                              items: Sequence[str] = ()) -> str:
        if "hold_short" in items:
            body = self._("read_back_hold_short")
        elif len(items) == 1 and items[0] in self._READBACK_ITEM_KEYS:
            body = self._("read_back_item").format(
                item=self._(self._READBACK_ITEM_KEYS[items[0]]))
        else:
            body = self._("read_back_all")
        return _join(aircraft.spoken(True), body) + "."

    def say_again_instruction(self, aircraft: Aircraft, text: str) -> str:
        """The announcement between the callsign and the instruction.

        See :func:`wilcoatc.atc.phraseology.without_callsign`: the instruction
        opens by naming the aeroplane, and repeating it whole after a line
        that has just named the aeroplane says it twice.
        """
        return _sentence(_join(aircraft.spoken(True), self._("i_say_again"),
                               without_callsign(text, aircraft)))

    # ------------------------------------------------------------------
    # ground
    # ------------------------------------------------------------------

    def _route(self, route: Sequence[str]) -> str:
        return " ".join(sp.letters(leg) for leg in route)

    def taxi_to_runway(
        self, aircraft: Aircraft, station: str, runway: str,
        route: Sequence[str], *, hold_short: str = "", altimeter: float = 0.0,
        abbreviated: bool = True,
    ) -> str:
        parts = [self.addressed(aircraft, station, abbreviated)]
        parts.append(self._("taxi_via").format(route=self._route(route))
                     if route else self._("taxi_to_the_runway"))
        parts.append(self._("holding_point").format(runway=self.rwy(runway)))
        if hold_short:
            parts.append(self._("hold_short").format(runway=self.rwy(hold_short)))
        if altimeter:
            parts.append(self.pressure(altimeter))
        return _join(*parts) + "."

    def taxi_to_parking(
        self, aircraft: Aircraft, station: str, route: Sequence[str],
        destination: str = "", *, hold_short: str = "", abbreviated: bool = True,
    ) -> str:
        destination = destination or self._("parking")
        parts = [self.addressed(aircraft, station, abbreviated)]
        parts.append(
            self._("taxi_to_parking_via").format(
                destination=destination, route=self._route(route))
            if route else
            self._("taxi_to_parking").format(destination=destination))
        if hold_short:
            parts.append(self._("hold_short").format(runway=self.rwy(hold_short)))
        return _join(*parts) + "."

    def cross_runway(self, aircraft: Aircraft, runway: str, *, then: str = "",
                     abbreviated: bool = True) -> str:
        parts = [aircraft.spoken(abbreviated),
                 self._("cross_runway").format(runway=self.rwy(runway))]
        if then:
            parts.append(then)
        return _join(*parts) + "."

    def hold_short_instruction(self, aircraft: Aircraft, runway: str) -> str:
        return _join(aircraft.spoken(True),
                     self._("hold_short").format(runway=self.rwy(runway))) + "."

    def hold_position(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), self._("hold_position")) + "."

    def continue_taxi(self, aircraft: Aircraft, route: Sequence[str] = ()) -> str:
        body = (self._("continue_via").format(route=self._route(route))
                if route else self._("continue_taxi"))
        return _join(aircraft.spoken(True), body) + "."

    def give_way(self, aircraft: Aircraft, traffic: str) -> str:
        return _join(aircraft.spoken(True),
                     self._("give_way").format(traffic=traffic),
                     self._("then_continue")) + "."

    def pushback_approved(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), self._("pushback_approved"),
                     self._("report_ready_to_taxi")) + "."

    # ------------------------------------------------------------------
    # tower
    # ------------------------------------------------------------------

    def line_up_and_wait(self, aircraft: Aircraft, runway: str, *,
                         reason: str = "", station: str = "",
                         abbreviated: bool = True) -> str:
        parts = [self.addressed(aircraft, station, abbreviated),
                 self._("runway_is").format(runway=self.rwy(runway)),
                 self._("line_up_and_wait")]
        if reason:
            parts.append(reason)
        return _join(*parts) + "."

    def cleared_for_takeoff(
        self, aircraft: Aircraft, runway: str, weather: Weather | None = None,
        *, station: str = "", after_departure: str = "",
        abbreviated: bool = True, traffic: str = "",
    ) -> str:
        parts = [self.addressed(aircraft, station, abbreviated)]
        if traffic:
            parts.append(traffic)
        if weather is not None:
            parts.append(self.wind(weather))
        parts.append(self._("runway_is").format(runway=self.rwy(runway)))
        if after_departure:
            parts.append(after_departure)
        parts.append(self._("cleared_takeoff"))
        return _join(*parts) + "."

    def cleared_to_land(
        self, aircraft: Aircraft, runway: str, weather: Weather | None = None,
        *, station: str = "", sequence: str = "", abbreviated: bool = True,
    ) -> str:
        parts = [self.addressed(aircraft, station, abbreviated)]
        if sequence:
            parts.append(sequence)
        if weather is not None:
            parts.append(self.wind(weather))
        parts.append(self._("runway_is").format(runway=self.rwy(runway)))
        parts.append(self._("cleared_to_land"))
        return _join(*parts) + "."

    def cleared_touch_and_go(self, aircraft: Aircraft, runway: str,
                             weather: Weather | None = None, *,
                             sequence: str = "", station: str = "",
                             abbreviated: bool = True) -> str:
        parts = [self.addressed(aircraft, station, abbreviated)]
        if sequence:
            parts.append(sequence)
        if weather is not None:
            parts.append(self.wind(weather))
        parts += [self._("runway_is").format(runway=self.rwy(runway)),
                  self._("cleared_option")]
        return _join(*parts) + "."

    def closed_traffic_approved(
        self, aircraft: Aircraft, runway: str, direction: str = "",
        weather: Weather | None = None, *, station: str = "",
        abbreviated: bool = True,
    ) -> str:
        direction = direction or self.book.left
        parts = [self.addressed(aircraft, station, abbreviated)]
        if weather is not None:
            parts.append(self.wind(weather))
        parts += [self._("runway_is").format(runway=self.rwy(runway)),
                  self._("cleared_takeoff"),
                  self._("closed_traffic_approved").format(direction=direction)]
        return _join(*parts) + "."

    def make_short_approach(self, aircraft: Aircraft, reason: str = "") -> str:
        parts = [aircraft.spoken(True), self._("make_short_approach")]
        if reason:
            parts.append(reason)
        return _join(*parts) + "."

    def turn_out_approved(self, aircraft: Aircraft, direction: str = "") -> str:
        if direction:
            return _join(aircraft.spoken(True),
                         self._("turn_out_approved").format(direction=direction)) + "."
        return _join(aircraft.spoken(True), self._("on_course_approved")) + "."

    def go_around(self, aircraft: Aircraft, reason: str = "",
                  instruction: str = "") -> str:
        parts = [aircraft.spoken(True), self._("go_around")]
        if reason:
            parts.append(reason)
        if instruction:
            parts.append(instruction)
        return _join(*parts) + "."

    def go_around_standard(self, aircraft: Aircraft, altitude_ft: float = 3000,
                           transition_altitude: int = 0) -> str:
        return self.go_around(aircraft, "", _join(
            self._("runway_heading"),
            self._("climb_to").format(
                altitude=self.alt(altitude_ft, transition_altitude))))

    def continue_approach(self, aircraft: Aircraft, reason: str = "") -> str:
        parts = [aircraft.spoken(True), self._("continue_approach")]
        if reason:
            parts.append(reason)
        return _join(*parts) + "."

    def sequence_number(self, position: int, traffic: str = "") -> str:
        text = self._("number").format(number=self.n(position))
        if traffic:
            text += ", " + self._("follow").format(traffic=traffic)
        return text

    def enter_pattern(self, aircraft: Aircraft, station: str, runway: str,
                      leg: str = "", direction: str = "",
                      *, report: str = "", weather: Weather | None = None,
                      sequence: str = "", straight_in: bool = False) -> str:
        direction = direction or self.book.left
        parts = [self.addressed(aircraft, station)]
        if sequence:
            parts.append(sequence)
        if straight_in:
            parts.append(self._("straight_in").format(runway=self.rwy(runway)))
        elif leg:
            parts.append(self._("enter_leg").format(
                leg=leg, direction=direction, runway=self.rwy(runway)))
        else:
            parts.append(self._("enter_pattern").format(
                direction=direction, runway=self.rwy(runway)))
        if report:
            parts.append(self._("report").format(what=report))
        if weather is not None:
            parts.append(self.wind(weather))
            parts.append(self.pressure(weather.qnh_hpa))
        return _join(*parts) + "."

    def report_position(self, aircraft: Aircraft, point: str) -> str:
        return _join(aircraft.spoken(True),
                     self._("report").format(what=point)) + "."

    def extend_downwind(self, aircraft: Aircraft, reason: str = "") -> str:
        parts = [aircraft.spoken(True), self._("extend_downwind")]
        if reason:
            parts.append(reason)
        parts.append(self._("i_will_call_your_base"))
        return _join(*parts) + "."

    def cleared_low_approach(self, aircraft: Aircraft, runway: str) -> str:
        return _join(aircraft.spoken(True),
                     self._("runway_is").format(runway=self.rwy(runway)),
                     self._("cleared_low_approach")) + "."

    def wind_check(self, weather: Weather, aircraft: Aircraft | None = None) -> str:
        parts = [aircraft.spoken(True)] if aircraft else []
        parts.append(self.wind(weather))
        return _join(*parts) + "."

    # ------------------------------------------------------------------
    # radar
    # ------------------------------------------------------------------

    def radar_contact(self, aircraft: Aircraft, station: str, *,
                      position: str = "", instruction: str = "",
                      abbreviated: bool = False) -> str:
        parts = [self.addressed(aircraft, station, abbreviated),
                 self._("radar_contact")]
        if position:
            parts.append(position)
        if instruction:
            parts.append(instruction)
        return _join(*parts) + "."

    def radar_contact_lost(self, aircraft: Aircraft, instruction: str = "") -> str:
        parts = [aircraft.spoken(True), self._("radar_contact_lost")]
        if instruction:
            parts.append(instruction)
        return _join(*parts) + "."

    def climb_maintain(self, aircraft: Aircraft, feet: float, *,
                       station: str = "", transition_altitude: int = 0,
                       restriction: str = "", abbreviated: bool = True) -> str:
        parts = [self.addressed(aircraft, station, abbreviated),
                 self._("climb_to").format(
                     altitude=self.alt(feet, transition_altitude))]
        if restriction:
            parts.append(restriction)
        return _join(*parts) + "."

    def descend_maintain(self, aircraft: Aircraft, feet: float, *,
                         station: str = "", transition_altitude: int = 0,
                         restriction: str = "", pilots_discretion: bool = False,
                         abbreviated: bool = True, transition_level: int = 0,
                         pressure: float = 0.0) -> str:
        """See :meth:`Phraseology.descend_maintain`: QNH after an altitude."""
        key = "descend_discretion" if pilots_discretion else "descend_to"
        parts = [self.addressed(aircraft, station, abbreviated),
                 self._(key).format(altitude=self.alt(
                     feet, transition_altitude, transition_level))]
        if pressure and not sp.is_flight_level(
                feet, transition_altitude or self.transition, transition_level):
            parts.append(self.pressure(pressure))
        if restriction:
            parts.append(restriction)
        return _join(*parts) + "."

    def maintain_altitude(self, aircraft: Aircraft, feet: float,
                          transition_altitude: int = 0) -> str:
        return _join(aircraft.spoken(True), self._("maintain_altitude").format(
            altitude=self.alt(feet, transition_altitude))) + "."

    def cross_at(self, fix: str, feet: float, *, at_or_above: bool = False,
                 at_or_below: bool = False, transition_altitude: int = 0,
                 fix_spoken: str = "") -> str:
        qualifier = ""
        if at_or_above:
            qualifier = self._("at_or_above") + " "
        elif at_or_below:
            qualifier = self._("at_or_below") + " "
        return self._("cross_at").format(
            fix=self._fix(fix, fix_spoken),
            altitude=qualifier + self.alt(feet, transition_altitude))

    def turn_heading(self, aircraft: Aircraft, degrees: float,
                     direction: str = "", *, station: str = "",
                     reason: str = "", abbreviated: bool = True) -> str:
        side = {"left": self.book.left, "right": self.book.right}.get(direction, "")
        body = (self._("turn_heading").format(side=side, heading=self.hdg(degrees))
                if side else
                self._("fly_heading").format(heading=self.hdg(degrees)))
        parts = [self.addressed(aircraft, station, abbreviated), body]
        if reason:
            parts.append(reason)
        return _join(*parts) + "."

    def direct_to(self, aircraft: Aircraft, fix: str, *, station: str = "",
                  abbreviated: bool = True, fix_spoken: str = "") -> str:
        return _join(self.addressed(aircraft, station, abbreviated),
                     self._("direct_to").format(
                         fix=self._fix(fix, fix_spoken))) + "."

    def resume_own_navigation(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), self._("resume_own_navigation")) + "."

    def speed_restriction(self, aircraft: Aircraft, knots: float, *,
                          maintain: bool = False, or_greater: bool = False,
                          or_less: bool = False) -> str:
        key = "maintain_speed" if maintain else "reduce_speed"
        body = self._(key).format(speed=self.speed(knots))
        if or_greater:
            body += " " + self._("or_greater")
        elif or_less:
            body += " " + self._("or_less")
        return _join(aircraft.spoken(True), body) + "."

    def resume_normal_speed(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), self._("resume_normal_speed")) + "."

    def traffic_advisory(self, aircraft: Aircraft, oclock: int,
                         distance_nm: float, direction: str = "",
                         traffic_type: str = "", altitude_ft: float = 0.0,
                         *, transition_altitude: int = 0) -> str:
        parts = [aircraft.spoken(True), self._("traffic"),
                 self._("oclock").format(hour=self.n(oclock)),
                 self._("at_distance").format(distance=self.distance(distance_nm))]
        if direction:
            parts.append(direction)
        if traffic_type:
            parts.append(traffic_type)
        parts.append(
            self._("at_distance").format(
                distance=self.alt(altitude_ft, transition_altitude))
            if altitude_ft else self._("altitude_unknown"))
        return _join(*parts) + "."

    def traffic_no_factor(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), self._("traffic_no_factor")) + "."

    def maintain_visual_separation(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), self._("maintain_visual_separation"),
                     self._("follow_that_traffic")) + "."

    # --- approach ---

    def expect_approach(self, aircraft: Aircraft, approach: str, runway: str,
                        airport: str = "") -> str:
        body = self._("expect_approach").format(approach=approach,
                                                runway=self.rwy(runway))
        if airport:
            body += " " + self._("at_airport").format(airport=airport)
        return _join(aircraft.spoken(True), body) + "."

    def vector_to_final(self, aircraft: Aircraft, *, distance_nm: float,
                        fix: str, heading: float, direction: str,
                        altitude_ft: float, approach: str, runway: str,
                        transition_altitude: int = 0,
                        fix_spoken: str = "") -> str:
        side = {"left": self.book.left, "right": self.book.right}.get(direction, "")
        turn = (self._("turn_heading").format(side=side, heading=self.hdg(heading))
                if side else self._("fly_heading").format(heading=self.hdg(heading)))
        parts = [
            aircraft.spoken(True),
            self._("distance_from").format(distance=self.distance(distance_nm),
                                           fix=self._fix(fix, fix_spoken)),
            turn,
            self._("maintain_until_established").format(
                altitude=self.alt(altitude_ft, transition_altitude)),
            self._("cleared_approach").format(approach=approach,
                                              runway=self.rwy(runway)),
        ]
        return _join(*parts) + "."

    def approach_name(self, kind: str) -> str:
        if kind == "VISUAL":
            return self._("visual_approach_word")
        return kind

    def cleared_approach(self, aircraft: Aircraft, approach: str, runway: str,
                         *, straight_in: bool = False, maintain_ft: float = 0.0,
                         descend_ft: float = 0.0, transition_altitude: int = 0,
                         transition_level: int = 0, pressure: float = 0.0) -> str:
        """See :meth:`Phraseology.cleared_approach`."""
        key = "cleared_straight_in_approach" if straight_in else "cleared_approach"
        parts = [aircraft.spoken(True)]
        if descend_ft:
            parts.append(self._("descend_to").format(altitude=self.alt(
                descend_ft, transition_altitude, transition_level)))
            if pressure and not sp.is_flight_level(
                    descend_ft, transition_altitude or self.transition,
                    transition_level):
                parts.append(self.pressure(pressure))
        parts.append(self._(key).format(approach=approach, runway=self.rwy(runway)))
        if maintain_ft:
            parts.append(self._("maintain_until_established").format(
                altitude=self.alt(maintain_ft, transition_altitude, transition_level)))
        return _join(*parts) + "."

    def cleared_visual_approach(self, aircraft: Aircraft, runway: str,
                                airport: str = "") -> str:
        body = (self._("cleared_visual_at").format(airport=airport,
                                                   runway=self.rwy(runway))
                if airport else
                self._("cleared_visual").format(runway=self.rwy(runway)))
        return _join(aircraft.spoken(True), body) + "."

    def report_field_in_sight(self, aircraft: Aircraft, airport: str,
                              oclock: int = 0, distance_nm: float = 0) -> str:
        parts = [aircraft.spoken(True)]
        if oclock and distance_nm:
            parts.append(self._("field_at").format(
                airport=airport, hour=self.n(oclock),
                distance=self.distance(distance_nm)))
        parts.append(self._("report_field_in_sight"))
        return _join(*parts) + "."

    def descend_via(self, aircraft: Aircraft, arrival: str) -> str:
        return _join(aircraft.spoken(True),
                     self._("descend_via").format(arrival=arrival)) + "."

    def climb_via_sid(self, aircraft: Aircraft, departure: str,
                      top_altitude: float = 0.0,
                      transition_altitude: int = 0) -> str:
        body = self._("climb_via").format(departure=departure)
        if top_altitude:
            body += ", " + self._("except_maintain").format(
                altitude=self.alt(top_altitude, transition_altitude))
        return _join(aircraft.spoken(True), body) + "."

    def holding_instruction(self, aircraft: Aircraft, fix: str, radial: float,
                            direction: str, turns: str = "",
                            leg_minutes: int = 1, efc: str = "",
                            altitude_ft: float = 0.0,
                            transition_altitude: int = 0,
                            fix_spoken: str = "") -> str:
        turns = turns or self.book.right
        parts = [
            aircraft.spoken(True),
            self._("hold_at").format(direction=direction,
                                     fix=self._fix(fix, fix_spoken)),
            self._("on_radial").format(radial=self.hdg(radial)),
        ]
        if altitude_ft:
            parts.append(self._("maintain_altitude").format(
                altitude=self.alt(altitude_ft, transition_altitude)))
        parts.append(self._("turns_to").format(side=turns))
        unit = self.book.unit_minutes if leg_minutes > 1 else self.book.unit_minute
        parts.append(self._("legs_of").format(minutes=self.n(leg_minutes),
                                              unit=unit))
        if efc:
            parts.append(self._("expect_further_clearance").format(time=efc))
        return _join(*parts) + "."

    # ------------------------------------------------------------------
    # VFR: airspace, advisories and levels
    # ------------------------------------------------------------------

    @property
    def vfr_squawk(self) -> str:
        return self.book.vfr_squawk

    def squawk_vfr(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), self._("squawk").format(
            code=self.d(f"{int(self.book.vfr_squawk):04d}"))) + "."

    def maintain_vfr_clause(self, altitude_ft: float = 0.0, *,
                            at_or_below: bool = True,
                            transition_altitude: int = 0) -> str:
        if not altitude_ft:
            return self._("maintain_vfr")
        key = "maintain_vfr_at_or_below" if at_or_below else "maintain_vfr_at_or_above"
        return self._(key).format(
            altitude=self.alt(altitude_ft, transition_altitude))

    def maintain_vfr(self, aircraft: Aircraft, altitude_ft: float = 0.0, *,
                     at_or_below: bool = True, transition_altitude: int = 0,
                     abbreviated: bool = True) -> str:
        return _join(aircraft.spoken(abbreviated), self.maintain_vfr_clause(
            altitude_ft, at_or_below=at_or_below,
            transition_altitude=transition_altitude)) + "."

    def vfr_altitude_discretion(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), self._("maintain_vfr"),
                     self._("altitude_your_discretion")) + "."

    def cleared_through_airspace(
        self, aircraft: Aircraft, airspace: str, *, altitude_ft: float = 0.0,
        route: str = "", report: str = "", transition_altitude: int = 0,
    ) -> str:
        parts = [aircraft.spoken(True),
                 self._("cleared_through").format(airspace=airspace)]
        if route:
            parts.append(route)
        if altitude_ft:
            parts.append(self._("maintain_vfr_at_or_below").format(
                altitude=self.alt(altitude_ft, transition_altitude)))
        else:
            parts.append(self._("maintain_vfr"))
        if report:
            parts.append(self._("report").format(what=report))
        return _join(*parts) + "."

    def remain_clear_of_airspace(self, aircraft: Aircraft, airspace: str,
                                 station: str = "", *, reason: str = "") -> str:
        parts = [self.addressed(aircraft, station),
                 self._("remain_clear_of").format(airspace=airspace)]
        if reason:
            parts.append(reason)
        parts.append(self._("standby"))
        return _join(*parts) + "."

    def flight_following_approved(self, aircraft: Aircraft, station: str,
                                  squawk: str) -> str:
        return _join(self.addressed(aircraft, station),
                     self._("squawk").format(code=self.d(f"{int(squawk):04d}")),
                     self._("maintain_vfr")) + "."

    def flight_following_unavailable(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True),
                     self._("flight_following_unavailable"),
                     self._("maintain_vfr")) + "."

    # ------------------------------------------------------------------
    # handoffs
    # ------------------------------------------------------------------

    def handoff(self, aircraft: Aircraft, next_station: str, next_freq: float,
                *, when: str = "", farewell: bool = False,
                welcome: str = "", abbreviated: bool = True) -> str:
        parts = [self.addressed(aircraft, "", abbreviated)]
        if welcome:
            parts.append(self._("welcome_to").format(place=welcome))
        body = self._("contact").format(station=next_station,
                                        frequency=self.freq(next_freq))
        if when:
            body = f"{when}, {body}"
        parts.append(body)
        text = _join(*parts) + "."
        # A welcome is the farewell for an arrival. Saying both would be
        # greeting somebody and taking leave of them in one breath.
        if farewell and not welcome and self.book.farewells:
            text += " " + _pick(self.book.farewells,
                                aircraft.callsign.written + next_station)
        return text

    def monitor(self, aircraft: Aircraft, next_station: str,
                next_freq: float) -> str:
        return _join(aircraft.spoken(True), self._("monitor").format(
            station=next_station, frequency=self.freq(next_freq))) + "."

    def remain_this_frequency(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), self._("remain_this_frequency")) + "."

    def frequency_change_approved(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True),
                     self._("frequency_change_approved")) + "."

    def radar_service_terminated(self, aircraft: Aircraft, *,
                                 squawk_vfr: bool = True,
                                 next_station: str = "",
                                 next_freq: float = 0.0) -> str:
        parts = [aircraft.spoken(True), self._("radar_service_terminated")]
        if squawk_vfr:
            parts.append(self._("squawk").format(
                code=self.d(self.book.vfr_squawk)))
        if next_station and next_freq:
            parts.append(self._("contact").format(
                station=next_station, frequency=self.freq(next_freq)))
        else:
            parts.append(self._("frequency_change_approved"))
        return _join(*parts) + "."

    def ifr_cancelled(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), self._("ifr_cancellation_received"),
                     self._("squawk").format(code=self.d(self.book.vfr_squawk)),
                     self._("frequency_change_approved")) + "."

    # ------------------------------------------------------------------
    # transponder, general
    # ------------------------------------------------------------------

    def squawk(self, aircraft: Aircraft, code: str) -> str:
        return _join(aircraft.spoken(True), self._("squawk").format(
            code=self.d(f"{int(code):04d}"))) + "."

    def ident(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), self._("ident")) + "."

    def altimeter_setting(self, aircraft: Aircraft, value: float) -> str:
        return _join(aircraft.spoken(True), self.pressure(value)) + "."

    def say_again(self, aircraft: Aircraft | None = None, item: str = "") -> str:
        body = (self._("say_again_item").format(item=item) if item
                else self._("say_again"))
        if aircraft:
            return _join(aircraft.spoken(True), body) + "."
        return body[0].upper() + body[1:] + "."

    def expecting(self, aircraft: Aircraft, calls: Sequence[str] = ()) -> str:
        wanted = [call for call in calls if call]
        if not wanted:
            return self.say_again(aircraft)
        joined = (", " + self._("or_else") + " ").join(wanted)
        return _join(aircraft.spoken(True),
                     self._("expecting").format(calls=joined)) + "."

    def roger(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), self._("roger")) + "."

    def standby(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), self._("standby")) + "."

    def roger_runway(self, aircraft: Aircraft, runway: str) -> str:
        """Acknowledge a runway the pilot asked for, by naming it back.

        Both halves are already in every book -- the acknowledgement and the
        "expect runway" an emergency is given -- so this is a sentence rather
        than a new phrase to translate.
        """
        return _join(aircraft.spoken(True), self._("roger"),
                     self._("expect_runway").format(
                         runway=self.rwy(runway))) + "."

    def unable(self, aircraft: Aircraft, reason: str = "") -> str:
        parts = [aircraft.spoken(True), self._("unable")]
        if reason:
            parts.append(reason)
        return _join(*parts) + "."

    def unable_lower(self, aircraft: Aircraft) -> str:
        return self.unable(aircraft, self._("maintain_present_altitude"))

    def unable_unknown_fix(self, aircraft: Aircraft) -> str:
        return self.unable(aircraft, self._("say_again_the_fix"))

    def unable_unknown_destination(self, aircraft: Aircraft) -> str:
        return self.unable(aircraft, self._("say_your_destination"))

    def wilco_expected(self, aircraft: Aircraft, instruction: str) -> str:
        return _join(aircraft.spoken(True), instruction) + "."

    def verify(self, aircraft: Aircraft, item: str) -> str:
        return _join(aircraft.spoken(True),
                     self._("verify").format(item=item)) + "."

    def radio_check(self, aircraft: Aircraft, readability: int = 5) -> str:
        return _join(aircraft.spoken(True), self._("read_you").format(
            readability=self.n(readability), scale=self.n(5))) + "."

    def no_reply(self, aircraft: Aircraft, station: str) -> str:
        return _join(aircraft.spoken(False), station,
                     self._("how_do_you_hear")) + " ?"

    # ------------------------------------------------------------------
    # when the aircraft and the request do not agree
    # ------------------------------------------------------------------

    def already_airborne(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), self._("you_are_airborne"),
                     self._("say_intentions")) + "."

    def already_on_the_ground(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), self._("you_are_on_the_ground"),
                     self._("say_intentions")) + "."

    def already_cleared_for_takeoff(self, aircraft: Aircraft, runway: str) -> str:
        return _join(aircraft.spoken(True), self._("already_cleared_takeoff"),
                     self._("runway_is").format(runway=self.rwy(runway))) + "."

    def already_cleared_to_land(self, aircraft: Aircraft, runway: str) -> str:
        return _join(aircraft.spoken(True), self._("already_cleared_to_land"),
                     self._("runway_is").format(runway=self.rwy(runway))) + "."

    # ------------------------------------------------------------------
    # what the controller says without being asked
    # ------------------------------------------------------------------

    def hold_position_incursion(self, aircraft: Aircraft, runway: str) -> str:
        return _join(aircraft.spoken(True), self._("hold_position_now"),
                     self._("entered_runway_without_clearance").format(
                         runway=self.rwy(runway))) + "."

    def departed_without_clearance(self, aircraft: Aircraft, runway: str,
                                   station: str = "") -> str:
        body = (self._("departed_without_clearance").format(runway=self.rwy(runway))
                if runway else self._("departed_without_clearance_no_runway"))
        parts = [aircraft.spoken(True), body, self._("report_will_be_filed")]
        if station:
            parts.append(self._("call_after_flight").format(station=station))
        return _join(*parts) + "."

    def landed_without_clearance(self, aircraft: Aircraft, runway: str,
                                 station: str = "") -> str:
        body = (self._("landed_without_clearance").format(runway=self.rwy(runway))
                if runway else self._("landed_without_clearance_no_runway"))
        parts = [aircraft.spoken(True), body, self._("report_will_be_filed")]
        if station:
            parts.append(self._("call_after_parking").format(station=station))
        return _join(*parts) + "."

    # ------------------------------------------------------------------

    def correction(self, aircraft: Aircraft, wrong: list[str], values: dict,
                   current_altitude_ft: float = 0.0,
                   transition_altitude: int = 0) -> str:
        parts: list[str] = []
        for key in wrong:
            value = values[key]
            if key == "altitude_ft":
                descending = (current_altitude_ft
                              and float(value) < current_altitude_ft)
                verb = "descend_to" if descending else "climb_to"
                parts.append(self._(verb).format(
                    altitude=self.alt(value, transition_altitude)))
            elif key == "heading":
                parts.append(self._("fly_heading").format(heading=self.hdg(value)))
            elif key == "runway":
                parts.append(self._("runway_is").format(runway=self.rwy(value)))
            elif key == "squawk":
                parts.append(self._("squawk").format(
                    code=self.d(f"{int(value):04d}")))
            elif key == "frequency":
                parts.append(self._("contact").format(
                    station="", frequency=self.freq(value)).strip())
            elif key == "speed_kt":
                parts.append(self._("maintain_speed").format(
                    speed=self.speed(value)))
            elif key == "hold_short":
                if isinstance(value, str) and value:
                    parts.append(self._("hold_short").format(
                        runway=self.rwy(value)))
                else:
                    parts.append(self._("hold_position"))
        return self.readback_incorrect(
            aircraft, ", ".join(parts) + ", " + self._("read_back"))

    # ------------------------------------------------------------------
    # emergencies
    # ------------------------------------------------------------------

    def emergency_acknowledged(self, aircraft: Aircraft, station: str, *,
                               runway: str = "",
                               souls_and_fuel: bool = True) -> str:
        parts = [self.addressed(aircraft, station), self._("roger"),
                 self._("state_nature_of_emergency")]
        if souls_and_fuel:
            parts.append(self._("souls_and_fuel"))
        return _join(*parts) + "."

    def emergency_priority(self, aircraft: Aircraft, runway: str,
                           heading: float = 0.0, equipment: bool = True) -> str:
        parts = [aircraft.spoken(True), self._("roger"),
                 self._("you_have_priority")]
        if heading:
            parts.append(self._("fly_heading").format(heading=self.hdg(heading)))
        parts.append(self._("expect_runway").format(runway=self.rwy(runway)))
        if equipment:
            parts.append(self._("equipment_standing_by"))
        return _join(*parts) + "."

    def emergency_cleared_to_land(self, aircraft: Aircraft, runway: str,
                                  weather: Weather | None = None) -> str:
        parts = [aircraft.spoken(True)]
        if weather is not None:
            parts.append(self.wind(weather))
        parts += [self._("runway_is").format(runway=self.rwy(runway)),
                  self._("cleared_to_land"), self._("the_airport_is_yours")]
        return _join(*parts) + "."

    # ------------------------------------------------------------------
    # fragments the decision layer passes back into a template
    # ------------------------------------------------------------------

    @property
    def ROUTE_RADAR_VECTORS(self) -> str:
        return self._("route_radar_vectors")

    @property
    def ROUTE_AS_FILED(self) -> str:
        # No book has its own words for the route after a SID; the vectors
        # line ends "then as filed" in each of them, which is what it is.
        return self.book.say.get("route_as_filed") or self.ROUTE_RADAR_VECTORS

    @property
    def RUNWAY_HEADING(self) -> str:
        return self._("runway_heading")

    @property
    def PARKING(self) -> str:
        return self._("parking")

    @property
    def UNKNOWN_DESTINATION(self) -> str:
        return self._("unknown_destination")

    @property
    def DEFAULT_APPROACH(self) -> str:
        return self._("default_approach")

    @property
    def REPORT_MIDFIELD_DOWNWIND(self) -> str:
        return self._("midfield_downwind")

    @property
    def REPORT_TURNING_BASE(self) -> str:
        return self._("turning_base")

    @property
    def REPORT_ENTERING_DOWNWIND(self) -> str:
        return self._("entering_downwind")

    # --- how the decision layer names things a language spells differently ---

    def leg(self, name: str) -> str:
        legs = self.book.legs
        return legs.get((name or "").lower(), legs["downwind"])

    def side(self, name: str) -> str:
        return self.book.right if (name or "").lower() == "right" else self.book.left

    def bearing(self, name: str) -> str:
        return self.book.bearings.get((name or "").lower(), "")

    def airspace(self, kind: str = "D") -> str:
        spaces = self.book.airspaces
        return spaces.get((kind or "D").upper(), spaces["D"])

    def clear_of(self, airspace: str) -> str:
        return self._("clear_of").format(airspace=airspace)

    def miles_final(self, nm: float) -> str:
        return self._("miles_final").format(distance=self.distance(nm))


__all__ = ["Phrasebook", "LocalisedPhraseology", "PHRASE_KEYS"]
