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
from ..navdata.channels import spoken_channel

# The circle's side, as the French clause says it.
_FR_COMPASS = {"north": "nord", "east": "est", "south": "sud", "west": "ouest"}
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

    def stand(self, number: int, gate: bool = False) -> str:
        """A stand by its number, for a taxi in.

        "Poste" is the word French aerodromes use for a stand ("poste de
        stationnement"); whether the DGAC manual of phraseology has the
        controller say "roulez vers le poste" or "roulez au poste" has not
        been checked, and the first is used because it is the form this
        phrasebook's taxi already has.
        """
        return f"le poste {fr.cardinal(int(number))}"

    def approach_name(self, kind: str) -> str:
        """See :meth:`Phraseology.approach_name`. UNVERIFIED: "LOC arrière"
        for a back course."""
        from .approaches import split_name
        from .speech import _ALPHABET

        base, letter = split_name(kind)
        word = {"VISUAL": "à vue", "LOC BC": "LOC arrière",
                "VOR/DME": "VOR DME", "NDB/DME": "NDB DME"}.get(base, base)
        return f"{word} {_ALPHABET.get(letter, letter)}" if letter else word

    def circle_clause(self, runway: str, side: str = "") -> str:
        """A circle to land: "suivie d'une MVL piste 07". UNVERIFIED: the
        DGAC manual was not consulted for a circling clearance."""
        body = f"suivie d'une MVL piste {self.rwy(runway)}"
        if side:
            body += f", au {_FR_COMPASS.get(side, side)} de l'aérodrome"
        return body

    # Les points de report qui tiennent un circuit sans radar.
    REPORT_MIDFIELD_DOWNWIND = "en milieu de vent arrière"
    REPORT_TURNING_BASE = "en base"
    REPORT_ENTERING_DOWNWIND = "en vent arrière"
    REPORT_FINAL = "en finale"
    REPORT_BASE = "en base"
    REPORT_LINED_UP = "aligné"
    REPORT_LEAVING_ZONE = "quittant la zone"

    _LEGS = {"downwind": "vent arrière", "base": "base", "final": "finale",
             "crosswind": "vent traversier", "upwind": "montée initiale"}
    _SIDES = {"left": "gauche", "right": "droite"}
    # La France ne parle pas de classes sur la fréquence, elle nomme le
    # volume : la CTR autour d'un terrain, la TMA au-dessus.
    _AIRSPACE = {"B": "la région terminale", "C": "la région terminale",
                 "D": "la CTR"}

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
        # The French VFR practice (the DGAC's), wherever French is spoken.
        self.region = "fr"

    # ------------------------------------------------------------------
    # primitives
    # ------------------------------------------------------------------

    def d(self, value) -> str:
        return fr.digits(value)

    def alt(self, feet: float, transition: int = 0, level_from: int = 0) -> str:
        return fr.altitude(feet, transition or self.transition, level_from)

    def vfr_alt(self, feet: float, transition: int = 0) -> str:
        return self.alt(feet, transition)

    channels: str = ""      # as Phraseology.channels

    def freq(self, mhz: float) -> str:
        mhz, named = spoken_channel(mhz, self.channels)
        return fr.frequency(mhz, channel=named)

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
        runway: str = "",
    ) -> str:
        """A full IFR clearance in French, in the published order."""
        if departure_procedure:
            via = f"par la {self.procedure(departure_procedure)}"
            if transition:
                via += f", transition {transition}"
            via += f", puis {route}"
        else:
            via = f"par {route}"

        parts = [
            self.addressed(aircraft, station),
            f"autorisé {destination} {via}",
        ]
        if runway:
            # UNVERIFIED: the DGAC form of the runway in a clearance.
            parts.append(f"piste {self.rwy(runway)}")
        parts.append(
            f"montez initialement {self.alt(initial_altitude, transition_altitude)}")
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
        pressure: float = 0.0, exit_point: str = "",
    ) -> str:
        """La sortie d'un VFR : "quittez la CTR par Sierra, altitude maximum
        1500 pieds, QNH 1013, transpondeur 7000". Jamais "autorisé à quitter".
        UNVERIFIED: the plan's DGAC form, not checked against the manual."""
        where = (f"par {exit_point}" if exit_point
                 else f"par le {direction}" if direction else "")
        parts = [self.addressed(aircraft, station),
                 " ".join(w for w in ("quittez", airspace or "la CTR", where) if w)]
        if altitude_restriction:
            parts.append(f"altitude maximum {self.alt(altitude_restriction)}")
        if pressure:
            parts.append(self.pressure(pressure))
        if departure_freq:
            parts.append(f"fréquence départ {self.freq(departure_freq)}")
        parts.append(f"transpondeur {fr.squawk(squawk)}")
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
        abbreviated: bool = True, hold_short_taxiway: str = "",
    ) -> str:
        """``roulez`` plus the route, then the holding point.

        French names the holding point rather than saying "hold short of":
        "point d'attente piste zéro six". A long taxi cut in two holds
        before the next taxiway (UNVERIFIED: the DGAC wording).
        """
        parts = [self.addressed(aircraft, station, abbreviated)]
        if route:
            parts.append(f"roulez par {fr.taxi_route(route)}")
        else:
            parts.append("roulez vers la piste")
        if hold_short_taxiway:
            parts.append(f"maintenez avant {fr.taxiway(hold_short_taxiway)}")
        parts.append(f"point d'attente piste {self.rwy(runway)}")
        if hold_short:
            parts.append(f"maintenez avant la piste {self.rwy(hold_short)}")
        if altimeter:
            parts.append(self.pressure(altimeter))
        return _join(*parts) + "."

    def taxi_to_parking(
        self, aircraft: Aircraft, station: str, route: Sequence[str],
        destination: str = "le parking", *, hold_short: str = "",
        abbreviated: bool = True, hold_short_taxiway: str = "",
    ) -> str:
        parts = [self.addressed(aircraft, station, abbreviated)]
        if route:
            parts.append(f"roulez vers {destination} par {fr.taxi_route(route)}")
        else:
            parts.append(f"roulez vers {destination}")
        if hold_short_taxiway:
            parts.append(f"maintenez avant {fr.taxiway(hold_short_taxiway)}")
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
        caution: str = "",
        immediate: bool = False,
    ) -> str:
        parts = [self.addressed(aircraft, station, abbreviated)]
        if traffic:
            parts.append(traffic)
        if weather is not None:
            parts.append(self.wind(weather))
        if caution:
            parts.append(caution)
        parts.append(f"piste {self.rwy(runway)}")
        if after_departure:
            parts.append(after_departure)
        # UNVERIFIED: "décollage immédiat".
        parts.append("autorisé décollage immédiat" if immediate
                     else "autorisé décollage")
        return _join(*parts) + "."

    def cleared_to_land(
        self, aircraft: Aircraft, runway: str, weather: Weather | None = None,
        *, station: str = "", sequence: str = "", abbreviated: bool = True,
        caution: str = "",
    ) -> str:
        parts = [self.addressed(aircraft, station, abbreviated)]
        if sequence:
            parts.append(sequence)
        if weather is not None:
            parts.append(self.wind(weather))
        if caution:
            parts.append(caution)
        parts.append(f"piste {self.rwy(runway)}")
        parts.append("autorisé atterrissage")
        return _join(*parts) + "."

    def cleared_touch_and_go(self, aircraft: Aircraft, runway: str,
                             weather: Weather | None = None, *,
                             sequence: str = "", station: str = "",
                             abbreviated: bool = True,
                             caution: str = "") -> str:
        parts = [self.addressed(aircraft, station, abbreviated)]
        if sequence:
            parts.append(sequence)
        if weather is not None:
            parts.append(self.wind(weather))
        if caution:
            parts.append(caution)
        # UNVERIFIED: "posé-décollé" is the plan's DGAC form.
        parts += [f"piste {self.rwy(runway)}", "autorisé posé-décollé"]
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
                  f"tour de piste main {direction}"]
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

    # --- la piste occupée --------------------------------------------------
    # UNVERIFIED: every form below.

    def traffic_on_final(self, traffic: str, miles: float) -> str:
        n = max(1, int(round(miles)))
        return f"trafic {traffic} en finale à {fr.distance(n)}"

    def land_after(self, aircraft: Aircraft, runway: str,
                   weather: Weather | None = None, *, traffic: str,
                   station: str = "", abbreviated: bool = True,
                   caution: str = "") -> str:
        parts = [self.addressed(aircraft, station, abbreviated),
                 f"piste {self.rwy(runway)}", f"atterrissez après {traffic}"]
        if weather is not None:
            parts.append(self.wind(weather))
        if caution:
            parts.append(caution)
        return _join(*parts) + "."

    def departing_prior(self) -> str:
        return "un trafic décollera avant votre arrivée"

    def line_up_behind(self, aircraft: Aircraft, runway: str, traffic: str, *,
                       station: str = "", abbreviated: bool = True) -> str:
        return _join(self.addressed(aircraft, station, abbreviated),
                     f"derrière {traffic}",
                     f"alignez-vous piste {self.rwy(runway)}", "derrière") + "."

    def landing_traffic_named(self, traffic: str) -> str:
        return f"{traffic} à l'atterrissage"

    # --- les attentes -------------------------------------------------------
    # UNVERIFIED: every form below.

    def orbit(self, aircraft: Aircraft, side: str, sequence: str = "") -> str:
        return _join(aircraft.spoken(True), sequence,
                     f"faites un tour complet par la {side} pour espacement",
                     "rappelez terminé") + "."

    def hold_at(self, aircraft: Aircraft, station: str, point: str,
                runway: str) -> str:
        return _join(self.addressed(aircraft, station), f"attendez à {point}",
                     "circuit chargé", f"piste {self.rwy(runway)}",
                     "je vous rappelle") + "."

    def leave_hold(self, aircraft: Aircraft, point: str) -> str:
        return f"quittez {point}"

    def expedite(self, aircraft: Aircraft, reason: str = "") -> str:
        return _join(aircraft.spoken(True), "accélérez", reason) + "."

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
        # The leg is the report: "rappelez vent arrière main gauche piste 27"
        # is the whole instruction (UNVERIFIED: the plan's DGAC form).
        if straight_in:
            parts.append(f"en approche directe piste {self.rwy(runway)}")
            if report:
                parts.append(f"rappelez {report}")
        elif leg:
            parts.append(f"rappelez {leg} main {direction} "
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

    def vrp(self, name: str, altitude_ft: float = 0.0) -> str:
        from .phraseology import _is_letters

        said = fr.letters(name) if _is_letters(name) else name
        if altitude_ft:
            said = f"{said}, {self.alt(altitude_ft)}"
        return said

    def infringement(self, aircraft: Aircraft, station: str, airspace: str,
                     turn: str, heading: float) -> str:
        # UNVERIFIED: no DGAC source for the wording.
        return _join(self.addressed(aircraft, station),
                     f"vous pénétrez dans {airspace} sans clairance",
                     f"virez à {turn} cap {self.hdg(heading)}",
                     "restez en dehors de l'espace contrôlé") + "."

    def enter_zone(self, aircraft: Aircraft, airspace: str, via: str = "") -> str:
        # UNVERIFIED, as above.
        return _join(aircraft.spoken(True),
                     f"pénétrez dans {airspace}" + (f" par {via}" if via else "")) + "."

    def join_report(self) -> str:
        return self.leg("downwind")

    def report_after_downwind(self) -> tuple[str, str]:
        # "Rappelez dernier virage": the turn on to final, at the end of the
        # base, so it is chased as the base report.
        return "dernier virage", "base"

    def join_overhead(self, aircraft: Aircraft, station: str, runway: str,
                      altitude_ft: float, pressure: float,
                      direction: str = "gauche") -> str:
        # UNVERIFIED: the verticale terrain join; the order of the items.
        return _join(self.addressed(aircraft, station),
                     f"rappelez verticale terrain à {self.alt(altitude_ft)}",
                     f"piste {self.rwy(runway)}",
                     f"tour de piste main {direction}",
                     self.pressure(pressure)) + "."

    def report_position(self, aircraft: Aircraft, point: str,
                        sequence: str = "") -> str:
        return _join(aircraft.spoken(True), sequence, f"rappelez {point}") + "."

    def extend_downwind(self, aircraft: Aircraft, reason: str = "") -> str:
        parts = [aircraft.spoken(True), "prolongez vent arrière"]
        if reason:
            parts.append(reason)
        parts.append("je vous rappelle en base")
        return _join(*parts) + "."

    def traffic_on_leg(self, traffic: str, leg: str) -> str:
        """``"le Cessna en base"``."""
        return f"{traffic} en {leg}" if traffic and leg else traffic

    def turn_base(self, aircraft: Aircraft, sequence: str = "") -> str:
        # UNVERIFIED: the DGAC manual was not available to check the words.
        parts = [aircraft.spoken(True), "virez en base"]
        if sequence:
            parts.append(sequence)
        return _join(*parts) + "."

    def say_position(self, aircraft: Aircraft) -> str:
        # UNVERIFIED, as above.
        return _join(aircraft.spoken(True), "rappelez votre position") + "."

    def confirm_leg(self, aircraft: Aircraft, seen: str) -> str:
        # UNVERIFIED, as above.
        return _join(aircraft.spoken(True),
                     f"confirmez votre position, je vous ai en {seen}") + "."

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

    def descend_crossing(self, aircraft: Aircraft, fix: str, feet: float, *,
                         station: str = "", abbreviated: bool = True,
                         transition_altitude: int = 5000,
                         transition_level: int = 0, pressure: float = 0.0,
                         fix_spoken: str = "") -> str:
        """See :meth:`Phraseology.descend_crossing`. UNVERIFIED: the DGAC
        manual was not consulted for a level to reach by a point."""
        from .speech import is_flight_level

        level = self.alt(feet, transition_altitude, transition_level)
        parts = [self.addressed(aircraft, station, abbreviated),
                 f"descendez pour être {level} à "
                 f"{fix_name_fr(fix, fix_spoken)}"]
        if pressure and not is_flight_level(
                feet, transition_altitude or self.transition, transition_level):
            parts.append(self.pressure(pressure))
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

    def procedure(self, name: str) -> str:
        """Une SID ou une STAR telle qu'on la dit : "OPALE 7K" est "Opale
        sept Kilo"."""
        from .procedures import spoken

        return spoken(name, self.d, fix_name_fr, fr.ALPHABET_FR)

    def rnav_to(self, fix: str) -> str:
        # The US tower's form only; in France the SID is the route.
        return ""

    def climb_clause(self, feet: float, transition_altitude: int = 5000) -> str:
        return f"montez {self.alt(feet, transition_altitude)}"

    def direct_clause(self, fix: str, fix_spoken: str = "") -> str:
        return f"direct {fix_name_fr(fix, fix_spoken)}"

    def verify_on_departure(self, aircraft: Aircraft, procedure: str) -> str:
        # UNVERIFIED: the IFR plan's line, in French.
        return _join(aircraft.spoken(True),
                     f"vérifiez que vous êtes sur le départ "
                     f"{self.procedure(procedure)}") + "."

    def confirm_departure(self, aircraft: Aircraft, procedure: str) -> str:
        # UNVERIFIED: "confirmez" is the DGAC's word for a doubt; the line
        # for a SID is the IFR plan's.
        return _join(aircraft.spoken(True),
                     f"confirmez le départ {self.procedure(procedure)}") + "."

    def verify_direct(self, aircraft: Aircraft, fix: str,
                      fix_spoken: str = "") -> str:
        # UNVERIFIED: the IFR plan's line, in French.
        return _join(aircraft.spoken(True),
                     f"confirmez que vous êtes direct "
                     f"{fix_name_fr(fix, fix_spoken)}") + "."

    def deviation_approved(self, aircraft: Aircraft, degrees: int = 0,
                           side: str = "", fix: str = "",
                           fix_spoken: str = "") -> str:
        # UNVERIFIED: the DGAC manual was not consulted for a deviation.
        where = {"left": "à gauche", "right": "à droite"}.get(side, "")
        if degrees and where:
            what = f"déviation de {int(degrees)} degrés {where} approuvée"
        elif where:
            what = f"déviation {where} de la route approuvée"
        else:
            what = "déviation approuvée"
        parts = [aircraft.spoken(True), what]
        if fix:
            parts.append(f"dès que possible, direct {fix_name_fr(fix, fix_spoken)}")
        return _join(*parts) + "."

    def vectors_for(self, approach: str, runway: str) -> str:
        # UNVERIFIED: the DGAC manual was not consulted for the reason.
        return f"guidage pour l'approche {approach} piste {self.rwy(runway)}"

    def fly_present_heading(self, aircraft: Aircraft, reason: str = "") -> str:
        return _join(aircraft.spoken(True), "maintenez le cap actuel", reason) + "."

    def intercept_clearance(self, aircraft: Aircraft, heading: float,
                            direction: str, approach: str, runway: str, *,
                            maintain_ft: float = 0.0, localizer: bool = True,
                            visual: bool = False, transition_altitude: int = 5000,
                            transition_level: int = 0) -> str:
        """See :meth:`Phraseology.intercept_clearance`. UNVERIFIED: the
        DGAC wording for the intercept and "rappelez établi"."""
        side = {"left": "gauche", "right": "droite"}.get(direction, "")
        turn = (f"tournez à {side} cap {self.hdg(heading)}" if side
                else f"prenez le cap {self.hdg(heading)}")
        if visual:
            return _join(aircraft.spoken(True), turn,
                         f"autorisé approche à vue piste {self.rwy(runway)}") + "."
        return _join(aircraft.spoken(True), turn,
                     f"autorisé approche {approach} piste {self.rwy(runway)}",
                     "rappelez établi") + "."

    def tower_continue(self, aircraft: Aircraft, runway: str,
                       station: str = "", sequence: str = "") -> str:
        # UNVERIFIED: the DGAC wording for the check-in on the final.
        return _join(self.addressed(aircraft, station, not station), sequence,
                     f"continuez l'approche, piste {self.rwy(runway)}") + "."

    def hold_clearance(self, aircraft: Aircraft, fix: str, side: str,
                       inbound: float, altitude_ft: float, leg_minutes: float,
                       efc_hhmm: str, *, station: str = "",
                       abbreviated: bool = True, transition_altitude: int = 5000,
                       transition_level: int = 0, fix_spoken: str = "") -> str:
        """See :meth:`Phraseology.hold_clearance`. UNVERIFIED: the DGAC
        wording of a holding clearance."""
        legs = "une minute et demie" if leg_minutes == 1.5 else "une minute"
        return _join(self.addressed(aircraft, station, abbreviated),
                     f"attendez à {fix_name_fr(fix, fix_spoken)}",
                     f"maintenez {self.alt(altitude_ft, transition_altitude, transition_level)}",
                     f"rapprochement {self.hdg(inbound)}",
                     "virages à droite", f"éloignement {legs}",
                     f"heure d'approche prévue {self.d(efc_hhmm)}") + "."

    def hold_sequence(self, aircraft: Aircraft, number: int, efc_hhmm: str) -> str:
        return _join(aircraft.spoken(True), f"numéro {fr.cardinal(int(number))}",
                     f"heure d'approche prévue {self.d(efc_hhmm)}") + "."

    def leave_the_hold(self, aircraft: Aircraft, fix: str,
                       fix_spoken: str = "") -> str:
        return _join(aircraft.spoken(True), "quittez l'attente",
                     f"direct {fix_name_fr(fix, fix_spoken)}") + "."

    def verify_holding(self, aircraft: Aircraft, fix: str,
                       fix_spoken: str = "") -> str:
        return _join(aircraft.spoken(True),
                     f"confirmez en attente à {fix_name_fr(fix, fix_spoken)}") + "."

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

    def speed_until(self, aircraft: Aircraft, knots: float, miles: int) -> str:
        # UNVERIFIED: the DGAC wording of a speed held to a DME distance.
        return _join(aircraft.spoken(True),
                     f"maintenez {fr.speed(knots)} jusqu'à {fr.cardinal(int(miles))} DME") + "."

    def no_speed_restrictions(self, aircraft: Aircraft) -> str:
        # UNVERIFIED: "pas de restriction de vitesse".
        return _join(aircraft.spoken(True), "pas de restriction de vitesse") + "."

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

    def traffic_call(self, aircraft: Aircraft, *, oclock: int,
                     distance_nm: float, motion: str, heading: float,
                     traffic: str = "", relative_ft: int = 0) -> str:
        # UNVERIFIED: the DGAC wording for a traffic call.
        hour = ("midi" if oclock == 12 else "une heure" if oclock == 1
                else f"{fr.cardinal(oclock)} heures")
        parts = [aircraft.spoken(True), f"trafic à {hour}",
                 f"à {fr.distance(distance_nm)}",
                 {"same": "même sens", "opposite": "sens opposé",
                  "left_to_right": "de gauche à droite",
                  "right_to_left": "de droite à gauche"}[motion]]
        if traffic:
            parts.append(traffic)
        if abs(relative_ft) < 100:
            parts.append("même niveau")
        else:
            parts.append(f"{self.alt(abs(relative_ft))} "
                         f"{'plus haut' if relative_ft > 0 else 'plus bas'}")
        return _join(*parts) + "."

    def caution_wake(self) -> str:
        return "attention turbulence de sillage"

    def runway_wet(self) -> str:
        # UNVERIFIED.
        return "piste mouillée"

    # --- VFR spécial ------------------------------------------------------
    # UNVERIFIED: every form below; the DGAC manual was not read for them.

    def vfr_below_minima(self, aircraft: Aircraft, station: str = "", *,
                         offer: bool = True, airspace: str = "") -> str:
        parts = [self.addressed(aircraft, station),
                 "conditions inférieures aux minima VFR"]
        if airspace:
            parts.append(f"restez en dehors de {airspace}")
        parts.append("VFR spécial possible sur demande" if offer
                     else "rappelez vos intentions")
        return _join(*parts) + "."

    def _svfr_terms(self, altitude_ft: float, pressure: float) -> list[str]:
        parts = []
        if altitude_ft:
            parts.append(f"altitude maximum {self.alt(altitude_ft)}")
        if pressure:
            parts.append(self.pressure(pressure))
        return parts

    def special_vfr_leave(self, aircraft: Aircraft, station: str = "", *,
                          zone: str = "", airspace: str = "", exit_point: str = "",
                          direction: str = "", altitude_ft: float = 0.0,
                          pressure: float = 0.0, squawk: str = "",
                          departure_freq: float = 0.0) -> str:
        where = (f"par {exit_point}" if exit_point
                 else f"par le {direction}" if direction else "")
        head = " ".join(w for w in ("autorisé à quitter", airspace or "la CTR",
                                    "en VFR spécial", where) if w)
        parts = [self.addressed(aircraft, station), head,
                 *self._svfr_terms(altitude_ft, pressure)]
        if departure_freq:
            parts.append(f"fréquence départ {self.freq(departure_freq)}")
        if squawk:
            parts.append(f"transpondeur {fr.squawk(squawk)}")
        return _join(*parts) + "."

    def special_vfr_enter(self, aircraft: Aircraft, station: str = "", *,
                          zone: str = "", airspace: str = "", entry_point: str = "",
                          altitude_ft: float = 0.0, pressure: float = 0.0,
                          report: str = "") -> str:
        where = f"par {entry_point}" if entry_point else ""
        head = " ".join(w for w in ("autorisé à pénétrer dans", airspace or "la CTR",
                                    "en VFR spécial", where) if w)
        parts = [self.addressed(aircraft, station), head,
                 *self._svfr_terms(altitude_ft, pressure)]
        if report:
            parts.append(f"rappelez {report}")
        return _join(*parts) + "."

    def special_vfr_unable(self, aircraft: Aircraft, reason: str,
                           station: str = "") -> str:
        why = {"visibility": "visibilité inférieure aux minima",
               "ceiling": "plafond inférieur aux minima",
               "night": "pas de VFR spécial de nuit"}.get(reason, "")
        return _join(self.addressed(aircraft, station), "VFR spécial impossible",
                     why, "rappelez vos intentions") + "."

    def traffic_no_factor(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), "trafic précédent sans influence") + "."

    def maintain_visual_separation(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), "assurez votre propre espacement",
                     "suivez ce trafic") + "."

    # --- approach ---

    def expect_approach(self, aircraft: Aircraft, approach: str, runway: str,
                        airport: str = "", circle: str = "") -> str:
        body = f"prévoyez l'approche {approach} piste {self.rwy(runway)}"
        if airport:
            body += f" à {airport}"
        return _join(aircraft.spoken(True), body, circle) + "."

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
                         transition_level: int = 0, pressure: float = 0.0,
                         circle: str = "") -> str:
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
        if circle:
            parts.append(circle)
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

    def descend_via(self, aircraft: Aircraft, arrival: str, *,
                    station: str = "", abbreviated: bool = True,
                    level_ft: float = 0.0, transition_altitude: int = 5000,
                    transition_level: int = 0, pressure: float = 0.0) -> str:
        """See :meth:`Phraseology.descend_via`. UNVERIFIED: the DGAC
        manual was not consulted for a STAR named with a descent."""
        from .speech import is_flight_level

        parts = [self.addressed(aircraft, station, abbreviated),
                 f"arrivée {self.procedure(arrival)}",
                 f"descendez {self.alt(level_ft, transition_altitude, transition_level)}"]
        if pressure and not is_flight_level(
                level_ft, transition_altitude or self.transition, transition_level):
            parts.append(self.pressure(pressure))
        return _join(*parts) + "."

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

    def squawk_vfr(self, aircraft: Aircraft, frequency_change: bool = False) -> str:
        parts = [aircraft.spoken(True), f"transpondeur {fr.squawk(VFR_SQUAWK_EUROPE)}"]
        if frequency_change:
            parts.append("changement de fréquence approuvé")
        return _join(*parts) + "."

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

    def say_altitude(self, aircraft: Aircraft) -> str:
        # UNVERIFIED: DGAC wording.
        return _join(aircraft.spoken(True), "annoncez votre altitude") + "."

    def descend_at_or_below(self, aircraft: Aircraft, altitude_ft: float, *,
                            transition_altitude: int = 0,
                            shown_ft: float = 0.0) -> str:
        # UNVERIFIED: DGAC wording.
        shown = (f"je vous ai à {self.vfr_alt(shown_ft, transition_altitude)}"
                 if shown_ft else "")
        return _join(aircraft.spoken(True), shown,
                     f"descendez {self.vfr_alt(altitude_ft, transition_altitude)}"
                     " ou en dessous") + "."

    def vfr_altitude_discretion(self, aircraft: Aircraft) -> str:
        return _join(aircraft.spoken(True), "maintenez VFR",
                     "altitude à votre convenance") + "."

    def cleared_through_airspace(
        self, aircraft: Aircraft, airspace: str, *, altitude_ft: float = 0.0,
        route: str = "", report: str = "", transition_altitude: int = 5000,
    ) -> str:
        # UNVERIFIED: "transit autorisé par ..., altitude maximum ...,
        # rappelez ..." is the plan's DGAC form.
        parts = [aircraft.spoken(True),
                 f"transit autorisé par {route}" if route else "transit autorisé"]
        if altitude_ft:
            parts.append("altitude maximum "
                         f"{self.alt(altitude_ft, transition_altitude)}")
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

    def called_elsewhere(self, aircraft: Aircraft, station: str,
                         called: str, called_freq: float) -> str:
        """Un autre poste appelé sur la fréquence de celui-ci."""
        return _join(self.addressed(aircraft, station, True),
                     f"{called} est sur {self.freq(called_freq)}") + "."

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

    def frequency_change_approved(self, aircraft: Aircraft,
                                  squawk: str = "") -> str:
        parts = [aircraft.spoken(True)]
        if squawk:
            parts.append(f"transpondeur {fr.squawk(squawk)}")
        parts.append("changement de fréquence approuvé")
        return _join(*parts) + "."

    # ------------------------------------------------------------------
    # les services d'information : AFIS, information de vol
    # ------------------------------------------------------------------
    #
    # UNVERIFIED: the DGAC Manuel de phraséologie was not available. "Piste
    # libre", "à votre convenance" and "rappelez vent arrière" are the forms
    # the plan names and that French AFIS agents are heard to use.

    def afis_field(self, aircraft: Aircraft, station: str, runway: str,
                   weather: Weather, *, direction: str = "", traffic: str = "",
                   report: str = "") -> str:
        parts = [self.addressed(aircraft, station),
                 f"piste {self.rwy(runway)} en service"]
        if direction:
            parts.append(f"tour de piste main {direction}")
        parts += [self.wind(weather), self.pressure(weather.qnh_hpa),
                  f"trafic {traffic}" if traffic else "pas de trafic signalé"]
        if report:
            parts.append(f"rappelez {report}")
        return _join(*parts) + "."

    def afis_traffic_report(self, aircraft: Aircraft, traffic: str,
                            report: str) -> str:
        # UNVERIFIED: the words. "Reçu" where there is no traffic: the next
        # report is asked for either way.
        return _join(aircraft.spoken(True),
                     f"trafic {traffic}" if traffic else "reçu",
                     f"rappelez {report}") + "."

    def afis_takeoff(self, aircraft: Aircraft, weather: Weather) -> str:
        return _join(aircraft.spoken(True), "piste libre", self.wind(weather),
                     "décollage à votre convenance") + "."

    def afis_land(self, aircraft: Aircraft, weather: Weather) -> str:
        return _join(aircraft.spoken(True), "piste libre", self.wind(weather),
                     "atterrissage à votre convenance") + "."

    def afis_runway_occupied(self, aircraft: Aircraft, traffic: str = "") -> str:
        parts = [aircraft.spoken(True), "piste occupée"]
        if traffic:
            parts.append(f"trafic {traffic}")
        return _join(*parts) + "."

    def fis_service(self, aircraft: Aircraft, station: str, pressure: float,
                    region: str = "icao") -> str:
        return _join(self.addressed(aircraft, station), "information de vol",
                     self.pressure(pressure)) + "."

    def position_fix(self, aircraft: Aircraft, miles: int, direction: str,
                     field: str) -> str:
        # "à l'est", "au nord-ouest".
        where = f"à l'{direction}" if direction[:1] in "aeiou" else f"au {direction}"
        return _join(aircraft.spoken(True),
                     f"vous êtes à {fr.cardinal(int(miles))} nautiques "
                     f"{where} de {field}") + "."

    def bearing_fix(self, aircraft: Aircraft, kind: str, bearing: int) -> str:
        return _join(aircraft.spoken(True),
                     f"{kind} {fr.digits(f'{int(bearing) % 360 or 360:03d}')}") + "."

    def flight_plan_activated(self, aircraft: Aircraft, station: str = "") -> str:
        return _join(self.addressed(aircraft, station),
                     "plan de vol VFR activé") + "."

    def flight_plan_relayed(self, aircraft: Aircraft) -> str:
        # UNVERIFIED.
        return _join(aircraft.spoken(True), "bien reçu",
                     "je clôture votre plan de vol") + "."

    def flight_plan_closed(self, aircraft: Aircraft, station: str = "") -> str:
        return _join(self.addressed(aircraft, station),
                     "plan de vol clôturé") + "."

    def weather_report(self, aircraft: Aircraft, field: str,
                       weather: Weather) -> str:
        return _join(aircraft.spoken(True), f"météo {field}",
                     self.wind(weather), self.pressure(weather.qnh_hpa)) + "."

    def radar_service_terminated(self, aircraft: Aircraft, *,
                                 squawk_vfr: bool = True,
                                 next_station: str = "",
                                 next_freq: float = 0.0) -> str:
        # UNVERIFIED: the plan's DGAC form.
        parts = [aircraft.spoken(True), "fin du service radar"]
        if squawk_vfr:
            parts.append(f"transpondeur {fr.squawk(VFR_SQUAWK_EUROPE)}")
        if next_station and next_freq:
            parts.append(f"contactez {next_station} {self.freq(next_freq)}")
        else:
            parts.append("changement de fréquence approuvé")
        return _join(*parts) + "."

    def handoff_reoffer(self, aircraft: Aircraft, next_station: str,
                        next_freq: float) -> str:
        # UNVERIFIED: no recording of a handoff said again was checked.
        return (f"{aircraft.spoken(True)}, avez-vous la fréquence ? "
                f"Contactez {next_station} {self.freq(next_freq)}.")

    def centre_check_in(self, aircraft: Aircraft, station: str,
                        level_ft: float = 0.0, transition: int = 0) -> str:
        parts = [self.addressed(aircraft, station, False), "identifié"]
        if level_ft:
            parts.append(f"maintenez {self.alt(level_ft, transition)}")
        return _join(*parts) + "."

    def oceanic_contact(self, aircraft: Aircraft, next_station: str,
                        hf_khz: Sequence[int] = ()) -> str:
        # UNVERIFIED: no French recording of a transfer to HF was checked.
        parts = [aircraft.spoken(True), "fin du service radar"]
        body = f"contactez {next_station}"
        if hf_khz:
            body += f" en HF, principale {fr.digits(hf_khz[0])}"
            if len(hf_khz) > 1:
                body += f", secondaire {fr.digits(hf_khz[1])}"
        parts.append(body)
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

    # UNVERIFIED: the three below; no French recording or manual checked.
    def unable_climb_traffic(self, aircraft: Aircraft, asked_ft: float,
                             level_ft: float, *,
                             transition_altitude: int = 0) -> str:
        return _join(aircraft.spoken(True),
                     f"impossible {self.alt(asked_ft, transition_altitude)} "
                     "en raison du trafic",
                     f"maintenez {self.alt(level_ft, transition_altitude)}") + "."

    def unable_direct(self, aircraft: Aircraft, fix: str) -> str:
        return _join(aircraft.spoken(True),
                     f"impossible direct {fix_name_fr(fix)}",
                     "maintenez la route actuelle") + "."

    def expect_lower(self, aircraft: Aircraft, minutes: int) -> str:
        return _join(aircraft.spoken(True),
                     f"prévoyez la descente dans {fr.cardinal(minutes)} "
                     "minutes") + "."

    UNABLE_REASONS = {
        "speed_limit": "vitesse limitée à 250 nœuds sous le niveau 100",
        "traffic_ahead": "trafic devant vous",
        "no_hold_fix": "pas de point d'attente sur votre route, précisez le point",
        "no_alternate": "pas de dégagement déposé, précisez le terrain",
        "no_runway": "piste {runway} non disponible",
        "no_approach": "approche {approach} piste {runway} non disponible, prévoyez {other}",
    }

    def unable_because(self, aircraft: Aircraft, reason: str, **params) -> str:
        # UNVERIFIED: the DGAC wording of each reason.
        if "runway" in params:
            params["runway"] = self.rwy(params["runway"])
        return self.unable(aircraft, self.UNABLE_REASONS[reason].format(**params))

    def cleared_to_airport(self, aircraft: Aircraft, airport: str,
                           altitude_ft: float, *, transition_altitude: int = 5000,
                           transition_level: int = 0) -> str:
        # UNVERIFIED: the DGAC wording of a diversion.
        return _join(aircraft.spoken(True), f"autorisé vers {airport} direct",
                     f"maintenez {self.alt(altitude_ft, transition_altitude, transition_level)}") + "."

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

    def runway_change(self, station: str, runway: str) -> str:
        # UNVERIFIED: the DGAC wording.
        return _sentence(f"à toutes les stations, {station}, changement de "
                         f"piste, piste en service {self.rwy(runway)}")

    def information_current(self, letter: str, pressure: float) -> str:
        """L'information ATIS en vigueur, pour un pilote qui en annonce une
        ancienne."""
        # UNVERIFIED: "information ... en vigueur" is the usual French form;
        # the DGAC Manuel de phraséologie was not available to check it.
        return _sentence(f"information {fr.letters(letter)} en vigueur, "
                         f"{self.pressure(pressure)}")

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
                   transition_altitude: int = 5000,
                   confirm: bool = False) -> str:
        """Correct only the items read back wrongly; ``confirm`` asks
        instead (see :meth:`Phraseology.correction`)."""
        if confirm and list(wrong) == ["sid"]:
            return self.confirm_departure(aircraft, values["sid"])
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
            elif key == "vfr_ceiling_ft":
                parts.append(f"altitude maximum {self.alt(value)}")
            elif key == "exit_point":
                parts.append(f"par {self.vrp(value)}")
            elif key == "sid":
                parts.append(f"par la {self.procedure(value)}")
            elif key == "star":
                parts.append(f"arrivée {self.procedure(value)}")
            elif key == "hold_short":
                # The instruction's own words. Correcting with "point
                # d'attente" names the holding point of the runway being
                # crossed, which is not what was said and is not something a
                # readback could ever satisfy.
                if isinstance(value, str) and value:
                    parts.append(f"maintenez avant la piste {self.rwy(value)}")
                else:
                    parts.append("maintenez avant la piste")
        if confirm:
            return _join(aircraft.spoken(True), "confirmez " + ", ".join(parts)) + "."
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
