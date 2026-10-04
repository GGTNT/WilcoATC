"""French controller phraseology.

The French half of a bilingual frequency. Same contract as
:class:`~wilcoatc.atc.phraseology.Phraseology` -- same method names, same
signatures -- so the controller logic never knows which language it is issuing
instructions in.

As with the English side, nothing here is generated. Every phrase is a fixed
template taken from published French phraseology:

  * SERA (Standardised European Rules of the Air), section 14, which is the
    binding source for European radiotelephony
  * the French AIP, GEN 3.4 and the DGAC radiotelephony manual
  * ICAO Doc 4444 chapter 12, of which the French forms are the translation

Two things distinguish it from a translation of the English:

*Number conventions differ within a single phrase.* "vent deux sept zéro
degrés, dix noeuds" reads the wind direction digit by digit and the speed as a
whole number. See :mod:`wilcoatc.atc.speech_fr`.

*The instruction verb carries the mode.* French uses the imperative --
"montez", "descendez", "roulez", "rappelez" -- where English uses a verb phrase
("climb and maintain"). Word order follows the French pattern rather than the
English one, so it reads as French rather than as translated English.
"""

from __future__ import annotations

from typing import Sequence

from . import speech_fr as fr
from .phraseology import (Aircraft, Weather, _join, _pick, _sentence,
                          without_callsign)


# The conspicuity code a VFR flight squawks. Europe uses 7000 where the US
# uses 1200, and a controller says the digits rather than the number.
VFR_SQUAWK_EUROPE = "7000"


def fix_name_fr(ident: str, spoken: str = "") -> str:
    """A waypoint as a French controller says it."""
    ident = (ident or "").strip().upper()
    if spoken:
        return spoken
    if not ident:
        return ""
    if len(ident) <= 3 or not ident.isalpha():
        return fr.letters(ident)
    return ident.capitalize()


class FrenchPhraseology:
    """Builds controller transmissions in French."""

    language = "fr"
    dialect = "fr"
    strict_icao_digits = False

    # Fragments the decision layer passes back into a template.
    ROUTE_RADAR_VECTORS = "guidage radar, puis route prévue"
    ROUTE_AS_FILED = "route prévue"
    RUNWAY_HEADING = "dans l'axe de piste"
    PARKING = "le parking"
    UNKNOWN_DESTINATION = "votre destination"
    DEFAULT_APPROACH = "ILS"

    def approach_name(self, kind: str) -> str:
        return {"VISUAL": "à vue"}.get(kind, kind)

    # Les points de report qui tiennent un circuit sans radar.
    REPORT_MIDFIELD_DOWNWIND = "en milieu de vent arrière"
    REPORT_TURNING_BASE = "en base"
    REPORT_ENTERING_DOWNWIND = "en vent arrière"

    _LEGS = {"downwind": "vent arrière", "base": "base", "final": "finale",
             "crosswind": "vent traversier", "upwind": "montée initiale"}
    _SIDES = {"left": "gauche", "right": "droite"}
    # La France ne parle pas de classes sur la fréquence, elle nomme le
    # volume : la CTR autour d'un terrain, la TMA au-dessus.
    _AIRSPACE = {"B": "la région terminale", "C": "la région terminale",
                 "D": "la zone de contrôle"}

    def leg(self, name: str) -> str:
        return self._LEGS.get((name or "").lower(), self._LEGS["downwind"])

    def side(self, name: str) -> str:
        return self._SIDES.get((name or "").lower(), self._SIDES["left"])

    _BEARINGS = {
        "north": "nord", "northeast": "nord-est", "east": "est",
        "southeast": "sud-est", "south": "sud", "southwest": "sud-ouest",
        "west": "ouest", "northwest": "nord-ouest",
    }

    def bearing(self, name: str) -> str:
        return self._BEARINGS.get((name or "").lower(), "")

    def airspace(self, kind: str = "D") -> str:
        return self._AIRSPACE.get((kind or "D").upper(), self._AIRSPACE["D"])

    def clear_of(self, airspace: str) -> str:
        return f"sorti de {airspace}"

    def miles_final(self, nm: float) -> str:
        return f"en finale à {fr.cardinal(int(round(nm)))} milles"

    def __init__(self, strict_icao_digits: bool = False,
                 transition: int = 5000):
        # Accepted for interface compatibility; French has no equivalent of
        # the "tree/fower/fife" question.
        self.strict_icao_digits = strict_icao_digits
        # French airspace meets at four to five thousand feet, so that is the
        # default rather than the American eighteen. A French-speaking
        # controller somewhere else is still told where the region's is.
        self.transition = int(transition) or 5000
        self.digit_dialect = "fr"

    # ------------------------------------------------------------------
    # primitives
    # ------------------------------------------------------------------

    def d(self, value) -> str:
        return fr.digits(value)

    def alt(self, feet: float, transition: int = 0, level_from: int = 0) -> str:
        return fr.altitude(feet, transition or self.transition, level_from)

    def freq(self, mhz: float) -> str:
        return fr.frequency(mhz)

    def hdg(self, degrees: float) -> str:
        return fr.heading(degrees)

    def rwy(self, ident: str) -> str:
        return fr.runway(ident)

    def pressure(self, value: float) -> str:
        return fr.qnh(value)

    def wind(self, weather: Weather) -> str:
        from .phraseology import spoken_wind_direction

        return fr.wind(spoken_wind_direction(weather), weather.wind_kt,
                       weather.gust_kt or None)

    def addressed(self, aircraft: Aircraft, station: str = "",
                  abbreviated: bool = False) -> str:
        who = aircraft.spoken(abbreviated)
        return _join(who, station) if station else who

    # ------------------------------------------------------------------
    # clearance delivery
    # ------------------------------------------------------------------

    def ifr_clearance(
        self,
        aircraft: Aircraft,
        station: str,
        destination: str,
        *,
        departure_procedure: str = "",
        transition: str = "",
        route: str = "route prévue",
        initial_altitude: float = 5000,
        cruise_altitude: float = 0,
        expect_minutes: int = 10,
        departure_freq: float = 0.0,
        squawk: str = "1200",
        transition_altitude: int = 5000,
    ) -> str:
        """A full IFR clearance in French, in the published order."""
        if departure_procedure:
            via = f"par la {departure_procedure}"
            if transition:
                via += f", transition {transition}"
            via += f", puis {route}"
        else:
            via = f"par {route}"

        parts = [
            self.addressed(aircraft, station),
            f"autorisé {destination} {via}",
            f"montez initialement {self.alt(initial_altitude, transition_altitude)}",
        ]
        if cruise_altitude and cruise_altitude > initial_altitude:
            parts.append(
                f"prévoyez {self.alt(cruise_altitude, transition_altitude)} "
                f"{fr.cardinal(expect_minutes)} minutes après le décollage"
            )
        if departure_freq:
            parts.append(f"fréquence départ {self.freq(departure_freq)}")
        parts.append(f"affichez {fr.squawk(squawk)}")
        return _join(*parts) + "."

    def vfr_departure_clearance(
        self, aircraft: Aircraft, station: str, *, airspace: str = "",
        departure_freq: float = 0.0, squawk: str = "1200",
        altitude_restriction: float = 0.0, direction: str = "",
    ) -> str:
        parts = [self.addressed(aircraft, station)]
        if airspace:
            body = f"autorisé à quitter {airspace}"
            if direction:
                body += f", départ {direction} approuvé"
            parts.append(body)
        elif direction:
            parts.append(f"départ {direction} approuvé")
        else:
            parts.append("départ VFR approuvé")
        if altitude_restriction:
            parts.append(f"maintenez VFR à ou en dessous de "
                         f"{self.alt(altitude_restriction)}")
        if departure_freq:
            parts.append(f"fréquence départ {self.freq(departure_freq)}")
        parts.append(f"affichez {fr.squawk(squawk)}")
        return _join(*parts) + "."

    def readback_correct(self, aircraft: Aircraft, *, next_station: str = "",
                         next_freq: float = 0.0,
                         when: str = "quand prêt au roulage") -> str:
        parts = [aircraft.spoken(True), "collationnement correct"]
        if next_station and next_freq:
            parts.append(f"contactez {next_station} {self.freq(next_freq)} {when}")
        elif next_station:
            parts.append(f"contactez {next_station} {when}")
        return _join(*parts) + "."

    def readback_incorrect(self, aircraft: Aircraft, correction: str) -> str:
        return _join(aircraft.spoken(True), "négatif", correction) + "."

    # Le collationnement n'est pas facultatif : SERA.14050 en fait une
    # obligation, et le contrôleur nomme ce qu'il attend plutôt que de laisser
    # passer. "Collationnez" est le mot, pas "répétez".
    READBACK_ITEM_NAMES: dict[str, str] = {
        "hold_short": "les instructions de point d'attente",
        "runway": "la piste",
        "altitude_ft": "l'altitude",
        "heading": "le cap",
        "squawk": "le code transpondeur",
        "frequency": "la fréquence",
        "speed_kt": "la vitesse",
    }

    def read_back_instruction(self, aircraft: Aircraft,
                              items: Sequence[str] = ()) -> str:
        if "hold_short" in items:
            body = "collationnez les instructions de point d'attente"
        elif len(items) == 1:
            body = "collationnez " + self.READBACK_ITEM_NAMES.get(
                items[0], "le message")
        else:
            body = "collationnez le message"
        return _join(aircraft.spoken(True), body) + "."

    def say_again_instruction(self, aircraft: Aircraft, text: str) -> str:
        """L'annonce entre l'indicatif et le message.

        See :func:`wilcoatc.atc.phraseology.without_callsign`: the instruction
        opens by naming the aeroplane, and repeating it whole after a line that
        has just named the aeroplane says it twice.
        """
        return _sentence(_join(aircraft.spoken(True), "je répète",
                               without_callsign(text, aircraft)))

    # ------------------------------------------------------------------
    # ground
    # ------------------------------------------------------------------

    def taxi_to_runway(
        self, aircraft: Aircraft, station: str, runway: str,
        route: Sequence[str], *, hold_short: str = "", altimeter: float = 0.0,
        abbreviated: bool = True,
    ) -> str:
        """``roulez`` plus the route, then the holding point.

        French names the holding point rather than saying "hold short of":
        "point d'attente piste zéro six".
        """
        parts = [self.addressed(aircraft, station, abbreviated)]
        if route:
            parts.append(f"roulez par {fr.taxi_route(route)}")
        else:
            parts.append("roulez vers la piste")
        parts.append(f"point d'attente piste {self.rwy(runway)}")
        if hold_short:
            parts.append(f"maintenez avant la piste {self.rwy(hold_short)}")
        if altimeter:
            parts.append(self.pressure(altimeter))
        return _join(*parts) + "."

    def taxi_to_parking(
        self, aircraft: Aircraft, station: str, route: Sequence[str],
        destination: str = "le parking", *, hold_short: str = "",
        abbreviated: bool = True,
    ) -> str:
        parts = [self.addressed(aircraft, station, abbreviated)]
        if route:
            parts.append(f"roulez vers {destination} par {fr.taxi_route(route)}")
        else:
            parts.append(f"roulez vers {destination}")
        if hold_short:
            parts.append(f"maintenez avant la piste {self.rwy(hold_short)}")
        return _join(*parts) + "."

    def cross_runway(self, aircraft: Aircraft, runway: str, *, then: str = "",
                     abbreviated: bool = True) -> str:
        parts = [aircraft.spoken(abbreviated), f"traversez la piste {self.rwy(runway)}"]
        if then:
            parts.append(then)
        return _join(*parts) + "."

    def hold_short_instruction(self, aircraft: Aircraft, runway: str) -> str:
        return _join(aircraft.spoken(True),
                     f"maintenez avant la piste {self.rwy(runway)}") + "."

    def hold_position(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), "maintenez position") + "."

    def continue_taxi(self, aircraft: Aircraft, route: Sequence[str] = ()) -> str:
        if route:
            return _join(aircraft.spoken(True),
                         f"continuez par {fr.taxi_route(route)}") + "."
        return _join(aircraft.spoken(True), "continuez le roulage") + "."

    def give_way(self, aircraft: Aircraft, traffic: str) -> str:
        return _join(aircraft.spoken(True), f"cédez le passage à {traffic}",
                     "puis continuez") + "."

    def pushback_approved(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), "repoussage et mise en route approuvés",
                     "rappelez prêt au roulage") + "."

    # ------------------------------------------------------------------
    # tower
    # ------------------------------------------------------------------

    def line_up_and_wait(self, aircraft: Aircraft, runway: str, *,
                         reason: str = "", station: str = "",
                         abbreviated: bool = True) -> str:
        parts = [self.addressed(aircraft, station, abbreviated),
                 f"piste {self.rwy(runway)}", "alignez-vous et attendez"]
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
        parts.append(f"piste {self.rwy(runway)}")
        if after_departure:
            parts.append(after_departure)
        parts.append("autorisé décollage")
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
        parts.append(f"piste {self.rwy(runway)}")
        parts.append("autorisé atterrissage")
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
        parts += [f"piste {self.rwy(runway)}", "autorisé option"]
        return _join(*parts) + "."

    def closed_traffic_approved(
        self, aircraft: Aircraft, runway: str, direction: str = "gauche",
        weather: Weather | None = None, *, station: str = "",
        abbreviated: bool = True,
    ) -> str:
        parts = [self.addressed(aircraft, station, abbreviated)]
        if weather is not None:
            parts.append(self.wind(weather))
        parts += [f"piste {self.rwy(runway)}", "autorisé décollage",
                  f"tour de piste main {direction} approuvé"]
        return _join(*parts) + "."

    def make_short_approach(self, aircraft: Aircraft, reason: str = "") -> str:
        parts = [aircraft.spoken(True), "faites une approche courte"]
        if reason:
            parts.append(reason)
        return _join(*parts) + "."

    def turn_out_approved(self, aircraft: Aircraft, direction: str = "") -> str:
        if direction:
            return _join(aircraft.spoken(True),
                         f"virage à {direction} approuvé") + "."
        return _join(aircraft.spoken(True), "poursuite en route approuvée") + "."

    def go_around(self, aircraft: Aircraft, reason: str = "",
                  instruction: str = "") -> str:
        parts = [aircraft.spoken(True), "remettez les gaz"]
        if reason:
            parts.append(reason)
        if instruction:
            parts.append(instruction)
        return _join(*parts) + "."

    def go_around_standard(self, aircraft: Aircraft, altitude_ft: float = 3000,
                           transition_altitude: int = 5000) -> str:
        return self.go_around(
            aircraft, "",
            f"{self.RUNWAY_HEADING}, montez {self.alt(altitude_ft, transition_altitude)}",
        )

    def continue_approach(self, aircraft: Aircraft, reason: str = "") -> str:
        parts = [aircraft.spoken(True), "continuez l'approche"]
        if reason:
            parts.append(reason)
        return _join(*parts) + "."

    def sequence_number(self, position: int, traffic: str = "") -> str:
        text = f"numéro {fr.cardinal(position)}"
        if traffic:
            text += f", suivez {traffic}"
        return text

    def enter_pattern(self, aircraft: Aircraft, station: str, runway: str,
                      leg: str = "vent arrière", direction: str = "gauche",
                      *, report: str = "", weather: Weather | None = None,
                      sequence: str = "", straight_in: bool = False) -> str:
        """L'intégration d'un VFR au circuit.

        Le français nomme l'étape ("intégrez en vent arrière main gauche") là
        où l'anglais dit "make left traffic", et l'approche directe est une
        intégration comme une autre.
        """
        parts = [self.addressed(aircraft, station)]
        if sequence:
            parts.append(sequence)
        if straight_in:
            parts.append(f"en approche directe piste {self.rwy(runway)}")
        elif leg:
            parts.append(f"intégrez en {leg} main {direction} "
                         f"piste {self.rwy(runway)}")
        else:
            parts.append(f"intégrez le circuit main {direction} "
                         f"piste {self.rwy(runway)}")
        if report:
            parts.append(f"rappelez {report}")
        if weather is not None:
            parts.append(self.wind(weather))
            parts.append(self.pressure(weather.qnh_hpa))
        return _join(*parts) + "."

    def report_position(self, aircraft: Aircraft, point: str) -> str:
        return _join(aircraft.spoken(True), f"rappelez {point}") + "."

    def extend_downwind(self, aircraft: Aircraft, reason: str = "") -> str:
        parts = [aircraft.spoken(True), "prolongez vent arrière"]
        if reason:
            parts.append(reason)
        parts.append("je vous rappelle en base")
        return _join(*parts) + "."

    def cleared_low_approach(self, aircraft: Aircraft, runway: str) -> str:
        return _join(aircraft.spoken(True), f"piste {self.rwy(runway)}",
                     "autorisé passage bas") + "."

    def wind_check(self, weather: Weather, aircraft: Aircraft | None = None) -> str:
        parts = []
        if aircraft:
            parts.append(aircraft.spoken(True))
        parts.append(self.wind(weather))
        return _join(*parts) + "."

    # ------------------------------------------------------------------
    # radar
    # ------------------------------------------------------------------

    def radar_contact(self, aircraft: Aircraft, station: str, *,
                      position: str = "", instruction: str = "",
                      abbreviated: bool = False) -> str:
        parts = [self.addressed(aircraft, station, abbreviated), "identifié radar"]
        if position:
            parts.append(position)
        if instruction:
            parts.append(instruction)
        return _join(*parts) + "."

    def radar_contact_lost(self, aircraft: Aircraft, instruction: str = "") -> str:
        parts = [aircraft.spoken(True), "contact radar perdu"]
        if instruction:
            parts.append(instruction)
        return _join(*parts) + "."

    def climb_maintain(self, aircraft: Aircraft, feet: float, *,
                       station: str = "", transition_altitude: int = 5000,
                       restriction: str = "", abbreviated: bool = True) -> str:
        parts = [self.addressed(aircraft, station, abbreviated),
                 f"montez {self.alt(feet, transition_altitude)}"]
        if restriction:
            parts.append(restriction)
        return _join(*parts) + "."

    def descend_maintain(self, aircraft: Aircraft, feet: float, *,
                         station: str = "", transition_altitude: int = 5000,
                         restriction: str = "", pilots_discretion: bool = False,
                         abbreviated: bool = True, transition_level: int = 0,
                         pressure: float = 0.0) -> str:
        """See :meth:`Phraseology.descend_maintain`: QNH after an altitude."""
        from .speech import is_flight_level

        verb = "descendez à votre convenance" if pilots_discretion else "descendez"
        parts = [self.addressed(aircraft, station, abbreviated),
                 f"{verb} {self.alt(feet, transition_altitude, transition_level)}"]
        if pressure and not is_flight_level(
                feet, transition_altitude or self.transition, transition_level):
            parts.append(self.pressure(pressure))
        if restriction:
            parts.append(restriction)
        return _join(*parts) + "."

    def maintain_altitude(self, aircraft: Aircraft, feet: float,
                          transition_altitude: int = 5000) -> str:
        return _join(aircraft.spoken(True),
                     f"maintenez {self.alt(feet, transition_altitude)}") + "."

    def cross_at(self, fix: str, feet: float, *, at_or_above: bool = False,
                 at_or_below: bool = False, transition_altitude: int = 5000,
                 fix_spoken: str = "") -> str:
        qualifier = ""
        if at_or_above:
            qualifier = "à ou au-dessus de "
        elif at_or_below:
            qualifier = "à ou en dessous de "
        return (f"passez {fix_name_fr(fix, fix_spoken)} "
                f"{qualifier}{self.alt(feet, transition_altitude)}")

    def turn_heading(self, aircraft: Aircraft, degrees: float,
                     direction: str = "", *, station: str = "",
                     reason: str = "", abbreviated: bool = True) -> str:
        side = {"left": "gauche", "right": "droite"}.get(direction, "")
        if side:
            body = f"tournez à {side} cap {self.hdg(degrees)}"
        else:
            body = f"prenez le cap {self.hdg(degrees)}"
        parts = [self.addressed(aircraft, station, abbreviated), body]
        if reason:
            parts.append(reason)
        return _join(*parts) + "."

    def direct_to(self, aircraft: Aircraft, fix: str, *, station: str = "",
                  abbreviated: bool = True, fix_spoken: str = "") -> str:
        return _join(self.addressed(aircraft, station, abbreviated),
                     f"direct {fix_name_fr(fix, fix_spoken)}") + "."

    def resume_own_navigation(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), "reprenez votre navigation") + "."

    def speed_restriction(self, aircraft: Aircraft, knots: float, *,
                          maintain: bool = False, or_greater: bool = False,
                          or_less: bool = False) -> str:
        if maintain:
            body = f"maintenez {fr.speed(knots)}"
        else:
            body = f"réduisez à {fr.speed(knots)}"
        if or_greater:
            body += " ou plus"
        elif or_less:
            body += " ou moins"
        return _join(aircraft.spoken(True), body) + "."

    def resume_normal_speed(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), "reprenez vitesse normale") + "."

    def traffic_advisory(self, aircraft: Aircraft, oclock: int,
                         distance_nm: float, direction: str = "",
                         traffic_type: str = "", altitude_ft: float = 0.0,
                         *, transition_altitude: int = 5000) -> str:
        parts = [aircraft.spoken(True), "trafic",
                 f"à {fr.cardinal(oclock)} heures",
                 f"à {fr.distance(distance_nm)}"]
        if direction:
            parts.append(direction)
        if traffic_type:
            parts.append(traffic_type)
        if altitude_ft:
            parts.append(f"à {self.alt(altitude_ft, transition_altitude)}")
        else:
            parts.append("niveau inconnu")
        return _join(*parts) + "."

    def traffic_no_factor(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), "trafic précédent sans influence") + "."

    def maintain_visual_separation(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), "assurez votre propre espacement",
                     "suivez ce trafic") + "."

    # --- approach ---

    def expect_approach(self, aircraft: Aircraft, approach: str, runway: str,
                        airport: str = "") -> str:
        body = f"prévoyez l'approche {approach} piste {self.rwy(runway)}"
        if airport:
            body += f" à {airport}"
        return _join(aircraft.spoken(True), body) + "."

    def vector_to_final(self, aircraft: Aircraft, *, distance_nm: float,
                        fix: str, heading: float, direction: str,
                        altitude_ft: float, approach: str, runway: str,
                        transition_altitude: int = 5000,
                        fix_spoken: str = "") -> str:
        side = {"left": "gauche", "right": "droite"}.get(direction, "")
        turn = (f"tournez à {side} cap {self.hdg(heading)}" if side
                else f"prenez le cap {self.hdg(heading)}")
        parts = [
            aircraft.spoken(True),
            f"à {fr.distance(distance_nm)} de {fix_name_fr(fix, fix_spoken)}",
            turn,
            f"maintenez {self.alt(altitude_ft, transition_altitude)} "
            f"jusqu'à l'établissement sur le localizer",
            f"autorisé approche {approach} piste {self.rwy(runway)}",
        ]
        return _join(*parts) + "."

    def cleared_approach(self, aircraft: Aircraft, approach: str, runway: str,
                         *, straight_in: bool = False, maintain_ft: float = 0.0,
                         descend_ft: float = 0.0, transition_altitude: int = 0,
                         transition_level: int = 0, pressure: float = 0.0) -> str:
        """See :meth:`Phraseology.cleared_approach`. "Jusqu'à l'établissement"
        is the wording :meth:`vector_to_final` already used; it is not
        checked against a DGAC publication."""
        from .speech import is_flight_level

        prefix = "autorisé approche directe" if straight_in else "autorisé approche"
        parts = [aircraft.spoken(True)]
        if descend_ft:
            parts.append(f"descendez {self.alt(descend_ft, transition_altitude, transition_level)}")
            if pressure and not is_flight_level(
                    descend_ft, transition_altitude or self.transition,
                    transition_level):
                parts.append(self.pressure(pressure))
        parts.append(f"{prefix} {approach} piste {self.rwy(runway)}")
        if maintain_ft:
            parts.append(f"maintenez {self.alt(maintain_ft, transition_altitude, transition_level)} "
                         "jusqu'à l'établissement")
        return _join(*parts) + "."

    def cleared_visual_approach(self, aircraft: Aircraft, runway: str,
                                airport: str = "") -> str:
        body = f"autorisé approche à vue piste {self.rwy(runway)}"
        if airport:
            body = f"autorisé approche à vue {airport} piste {self.rwy(runway)}"
        return _join(aircraft.spoken(True), body) + "."

    def report_field_in_sight(self, aircraft: Aircraft, airport: str,
                              oclock: int = 0, distance_nm: float = 0) -> str:
        parts = [aircraft.spoken(True)]
        if oclock and distance_nm:
            parts.append(f"{airport} à {fr.cardinal(oclock)} heures, "
                         f"{fr.distance(distance_nm)}")
        parts.append("rappelez terrain en vue")
        return _join(*parts) + "."

    def descend_via(self, aircraft: Aircraft, arrival: str) -> str:
        return _join(aircraft.spoken(True), f"descendez selon la {arrival}") + "."

    def climb_via_sid(self, aircraft: Aircraft, departure: str,
                      top_altitude: float = 0.0,
                      transition_altitude: int = 5000) -> str:
        body = f"montez selon la {departure}"
        if top_altitude:
            body += f", sauf maintenez {self.alt(top_altitude, transition_altitude)}"
        return _join(aircraft.spoken(True), body) + "."

    def holding_instruction(self, aircraft: Aircraft, fix: str, radial: float,
                            direction: str, turns: str = "droite",
                            leg_minutes: int = 1, efc: str = "",
                            altitude_ft: float = 0.0,
                            transition_altitude: int = 5000,
                            fix_spoken: str = "") -> str:
        parts = [
            aircraft.spoken(True),
            f"attendez {direction} de {fix_name_fr(fix, fix_spoken)}",
            f"sur le radial {self.hdg(radial)}",
        ]
        if altitude_ft:
            parts.append(f"maintenez {self.alt(altitude_ft, transition_altitude)}")
        parts.append(f"virages à {turns}")
        parts.append(f"branches de {fr.cardinal(leg_minutes)} minute"
                     f"{'s' if leg_minutes > 1 else ''}")
        if efc:
            parts.append(f"autorisation ultérieure prévue {efc}")
        return _join(*parts) + "."

    # ------------------------------------------------------------------
    # VFR : espaces, information de vol et niveaux
    # ------------------------------------------------------------------
    #
    # Un VFR ne se travaille pas avec les mots de l'IFR. Il n'est jamais
    # "autorisé à destination de", on ne lui donne pas un niveau à tenir, et
    # son altitude le regarde tant que rien ne le surplombe. D'où des
    # approbations, des plafonds et des informations plutôt que des clairances.

    @property
    def vfr_squawk(self) -> str:
        return VFR_SQUAWK_EUROPE

    def squawk_vfr(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True),
                     f"affichez {fr.squawk(VFR_SQUAWK_EUROPE)}") + "."

    def maintain_vfr_clause(self, altitude_ft: float = 0.0, *,
                            at_or_below: bool = True,
                            transition_altitude: int = 5000) -> str:
        if not altitude_ft:
            return "maintenez VFR"
        limit = "à ou en dessous de" if at_or_below else "à ou au dessus de"
        return f"maintenez VFR {limit} {self.alt(altitude_ft, transition_altitude)}"

    def maintain_vfr(self, aircraft: Aircraft, altitude_ft: float = 0.0, *,
                     at_or_below: bool = True, transition_altitude: int = 5000,
                     abbreviated: bool = True) -> str:
        return _join(aircraft.spoken(abbreviated), self.maintain_vfr_clause(
            altitude_ft, at_or_below=at_or_below,
            transition_altitude=transition_altitude)) + "."

    def vfr_altitude_discretion(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), "maintenez VFR",
                     "altitude à votre convenance") + "."

    def cleared_through_airspace(
        self, aircraft: Aircraft, airspace: str, *, altitude_ft: float = 0.0,
        route: str = "", report: str = "", transition_altitude: int = 5000,
    ) -> str:
        parts = [aircraft.spoken(True), f"autorisé à traverser {airspace}"]
        if route:
            parts.append(route)
        if altitude_ft:
            parts.append("maintenez VFR à ou en dessous de "
                         f"{self.alt(altitude_ft, transition_altitude)}")
        else:
            parts.append("maintenez VFR")
        if report:
            parts.append(f"rappelez {report}")
        return _join(*parts) + "."

    def remain_clear_of_airspace(self, aircraft: Aircraft, airspace: str,
                                 station: str = "", *, reason: str = "") -> str:
        parts = [self.addressed(aircraft, station),
                 f"restez en dehors de {airspace}"]
        if reason:
            parts.append(reason)
        parts.append("attendez")
        return _join(*parts) + "."

    def flight_following_approved(self, aircraft: Aircraft, station: str,
                                  squawk: str) -> str:
        return _join(self.addressed(aircraft, station),
                     f"affichez {fr.squawk(squawk)}", "maintenez VFR") + "."

    def flight_following_unavailable(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True),
                     "service d'information impossible, charge de travail",
                     "restez en VFR") + "."

    # ------------------------------------------------------------------
    # handoffs
    # ------------------------------------------------------------------

    def handoff(self, aircraft: Aircraft, next_station: str, next_freq: float,
                *, when: str = "", farewell: bool = False, welcome: str = "",
                abbreviated: bool = True) -> str:
        parts = [self.addressed(aircraft, "", abbreviated)]
        if welcome:
            parts.append(f"bienvenue à {welcome}")
        body = f"contactez {next_station} {self.freq(next_freq)}"
        if when:
            body = f"{when}, {body}"
        parts.append(body)
        text = _join(*parts) + "."
        # A welcome is the farewell for an arrival.
        if farewell and not welcome:
            text += " " + _pick(
                ("Au revoir.", "Bonne journée.", "Bon vol."),
                aircraft.callsign.written + next_station,
            )
        return text

    def monitor(self, aircraft: Aircraft, next_station: str,
                next_freq: float) -> str:
        return _join(aircraft.spoken(True),
                     f"surveillez {next_station} {self.freq(next_freq)}") + "."

    def remain_this_frequency(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), "restez sur cette fréquence") + "."

    def frequency_change_approved(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), "changement de fréquence approuvé") + "."

    def radar_service_terminated(self, aircraft: Aircraft, *,
                                 squawk_vfr: bool = True,
                                 next_station: str = "",
                                 next_freq: float = 0.0) -> str:
        parts = [aircraft.spoken(True), "service radar terminé"]
        if squawk_vfr:
            parts.append(f"affichez {fr.squawk(VFR_SQUAWK_EUROPE)}")
        if next_station and next_freq:
            parts.append(f"contactez {next_station} {self.freq(next_freq)}")
        else:
            parts.append("changement de fréquence approuvé")
        return _join(*parts) + "."

    def ifr_cancelled(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), "annulation IFR reçue",
                     f"affichez {fr.squawk(VFR_SQUAWK_EUROPE)}",
                     "changement de fréquence approuvé") + "."

    # ------------------------------------------------------------------
    # transponder, general
    # ------------------------------------------------------------------

    def squawk(self, aircraft: Aircraft, code: str) -> str:
        return _join(aircraft.spoken(True), f"affichez {fr.squawk(code)}") + "."

    def ident(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), "ident") + "."

    def altimeter_setting(self, aircraft: Aircraft, value: float) -> str:
        return _join(aircraft.spoken(True), self.pressure(value)) + "."

    def say_again(self, aircraft: Aircraft | None = None, item: str = "") -> str:
        body = f"répétez votre {item}" if item else "répétez"
        if aircraft:
            return _join(aircraft.spoken(True), body) + "."
        return body.capitalize() + "."

    def expecting(self, aircraft: Aircraft, calls: Sequence[str] = ()) -> str:
        wanted = [call for call in calls if call]
        if not wanted:
            return self.say_again(aircraft)
        # The calls are the pilot's own words, so they are quoted after a
        # verb that takes a quotation: "dites par exemple : demande la
        # clairance IFR pour Lyon". After "j'attends" they were not French.
        return _join(aircraft.spoken(True),
                     "dites par exemple : " + ", ou ".join(wanted)) + "."

    def roger_runway(self, aircraft: Aircraft, runway: str) -> str:
        """Acknowledge a runway the pilot asked for, by naming it back."""
        return _join(aircraft.spoken(True), "reçu",
                     f"prévoyez la piste {self.rwy(runway)}") + "."

    def roger(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), "reçu") + "."

    def standby(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), "attendez") + "."

    def unable(self, aircraft: Aircraft, reason: str = "") -> str:
        parts = [aircraft.spoken(True), "impossible"]
        if reason:
            parts.append(reason)
        return _join(*parts) + "."

    def unable_lower(self, aircraft: Aircraft) -> str:
        return self.unable(aircraft, "maintenez votre niveau actuel")

    def unable_unknown_fix(self, aircraft: Aircraft) -> str:
        return self.unable(aircraft, "répétez le point")

    def unable_unknown_destination(self, aircraft: Aircraft) -> str:
        return self.unable(aircraft, "annoncez votre destination")

    def wilco_expected(self, aircraft: Aircraft, instruction: str) -> str:
        return _join(aircraft.spoken(True), instruction) + "."

    def verify(self, aircraft: Aircraft, item: str) -> str:
        return _join(aircraft.spoken(True), f"confirmez {item}") + "."

    def radio_check(self, aircraft: Aircraft, readability: int = 5) -> str:
        return _join(aircraft.spoken(True),
                     f"je vous reçois {fr.cardinal(readability)} sur "
                     f"{fr.cardinal(5)}") + "."

    def no_reply(self, aircraft: Aircraft, station: str) -> str:
        return _join(aircraft.spoken(False), station, "comment me recevez-vous") + " ?"

    # ------------------------------------------------------------------
    # quand l'avion et la demande ne concordent pas
    # ------------------------------------------------------------------

    def already_airborne(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), "vous êtes en vol",
                     "annoncez vos intentions") + "."

    def already_on_the_ground(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), "vous êtes au sol",
                     "annoncez vos intentions") + "."

    def already_cleared_for_takeoff(self, aircraft: Aircraft, runway: str) -> str:
        return _join(aircraft.spoken(True), "vous êtes déjà autorisé décollage",
                     f"piste {self.rwy(runway)}") + "."

    def already_cleared_to_land(self, aircraft: Aircraft, runway: str) -> str:
        return _join(aircraft.spoken(True), "vous êtes déjà autorisé atterrissage",
                     f"piste {self.rwy(runway)}") + "."

    # ------------------------------------------------------------------
    # ce que le contrôleur dit sans qu'on lui demande
    # ------------------------------------------------------------------
    #
    # La France ne connaît pas la formule américaine "possible pilot
    # deviation". Un écart se traite par un rappel immédiat sur la fréquence
    # et par un compte rendu, ce que disent ces messages.

    def hold_position_incursion(self, aircraft: Aircraft, runway: str) -> str:
        return _join(aircraft.spoken(True), "maintenez position",
                     f"vous êtes entré sur la piste {self.rwy(runway)} "
                     "sans autorisation") + "."

    def departed_without_clearance(self, aircraft: Aircraft, runway: str,
                                   station: str = "") -> str:
        where = f" de la piste {self.rwy(runway)}" if runway else ""
        parts = [aircraft.spoken(True),
                 f"vous avez décollé{where} sans autorisation",
                 "un compte rendu d'incident sera transmis"]
        if station:
            parts.append(f"rappelez {station} après votre vol")
        return _join(*parts) + "."

    def landed_without_clearance(self, aircraft: Aircraft, runway: str,
                                 station: str = "") -> str:
        where = f" sur la piste {self.rwy(runway)}" if runway else ""
        parts = [aircraft.spoken(True),
                 f"vous vous êtes posé{where} sans autorisation",
                 "un compte rendu d'incident sera transmis"]
        if station:
            parts.append(f"rappelez {station} après le stationnement")
        return _join(*parts) + "."

    def correction(self, aircraft: Aircraft, wrong: list[str], values: dict,
                   current_altitude_ft: float = 0.0,
                   transition_altitude: int = 5000) -> str:
        """Correct only the items read back wrongly."""
        parts: list[str] = []
        for key in wrong:
            value = values[key]
            if key == "altitude_ft":
                descending = current_altitude_ft and float(value) < current_altitude_ft
                verb = "descendez" if descending else "montez"
                parts.append(f"{verb} {self.alt(value, transition_altitude)}")
            elif key == "heading":
                parts.append(f"cap {self.hdg(value)}")
            elif key == "runway":
                parts.append(f"piste {self.rwy(value)}")
            elif key == "squawk":
                parts.append(f"affichez {self.d(value)}")
            elif key == "frequency":
                parts.append(f"contactez sur {self.freq(value)}")
            elif key == "speed_kt":
                parts.append(f"vitesse {fr.speed(value)}")
            elif key == "hold_short":
                # The instruction's own words. Correcting with "point
                # d'attente" names the holding point of the runway being
                # crossed, which is not what was said and is not something a
                # readback could ever satisfy.
                if isinstance(value, str) and value:
                    parts.append(f"maintenez avant la piste {self.rwy(value)}")
                else:
                    parts.append("maintenez avant la piste")
        return self.readback_incorrect(aircraft, ", ".join(parts) + ", collationnez")

    # ------------------------------------------------------------------
    # emergencies
    # ------------------------------------------------------------------

    def emergency_acknowledged(self, aircraft: Aircraft, station: str, *,
                               runway: str = "",
                               souls_and_fuel: bool = True) -> str:
        parts = [self.addressed(aircraft, station), "reçu",
                 "annoncez la nature de votre urgence"]
        if souls_and_fuel:
            parts.append("personnes à bord et carburant restant")
        return _join(*parts) + "."

    def emergency_priority(self, aircraft: Aircraft, runway: str,
                           heading: float = 0.0, equipment: bool = True) -> str:
        parts = [aircraft.spoken(True), "reçu", "vous avez la priorité"]
        if heading:
            parts.append(f"prenez le cap {self.hdg(heading)}")
        parts.append(f"prévoyez la piste {self.rwy(runway)}")
        if equipment:
            parts.append("les secours sont alertés")
        return _join(*parts) + "."

    def emergency_cleared_to_land(self, aircraft: Aircraft, runway: str,
                                  weather: Weather | None = None) -> str:
        parts = [aircraft.spoken(True)]
        if weather is not None:
            parts.append(self.wind(weather))
        parts += [f"piste {self.rwy(runway)}", "autorisé atterrissage",
                  "la piste est à vous"]
        return _join(*parts) + "."


__all__ = ["FrenchPhraseology", "fix_name_fr"]
