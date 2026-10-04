"""What the other aeroplanes say.

The controller's half of a transmission is already written, six times over, in
:mod:`wilcoatc.atc.phraseology` and :mod:`wilcoatc.atc.phrasebook`. This is
the other half: the requests and readbacks an AI aircraft makes, in whichever
language its crew is speaking.

A readback is not free composition. It is the instruction's key elements said
back, in the order they were given, with the callsign on the end -- so almost
everything here is a short frame around a value the controller's own number
renderer produces. That is why a :class:`PilotSpeech` is built around a
phraseology object rather than duplicating its digits: a runway is spelled the
same way whoever is saying it, and a French crew reading back "piste zéro six"
has to say it exactly as the controller did or the readback is not a readback.

The requests are the part that genuinely differs, and there are not many of
them, because an aeroplane on frequency mostly listens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .phraseology import Aircraft, _join


@dataclass(frozen=True)
class PilotBook:
    """The pilot's side of the radio, in one language."""

    language: str

    # Requests, made once each per flight.
    request_clearance: str        # {dest}
    request_taxi: str
    ready_for_departure: str      # {runway}
    checking_in_climbing: str     # {altitude}
    checking_in_descending: str   # {altitude}
    established: str              # {runway}
    field_in_sight: str
    traffic_in_sight: str

    # Readbacks: the instruction, said back.
    taxi: str                     # {route} {runway}
    hold_short: str               # {runway}
    line_up: str                  # {runway}
    takeoff: str                  # {runway}
    land: str                     # {runway}
    go_around: str
    contact: str                  # {station} {frequency}
    squawk: str                   # {code}
    climb: str                    # {altitude}
    descend: str                  # {altitude}
    heading: str                  # {heading}
    speed: str                    # {speed}
    with_information: str         # {letter}

    # The short ones.
    roger: str
    wilco: str
    good_day: str

    # A greeting to open with, and the calls a controller can name when it is
    # waiting for one. These are the plain form of the request, with no number
    # in them, because a controller telling a pilot what to say cannot know the
    # runway or the level the pilot is about to give it.
    hello: str = ""
    check_in: str = ""
    request_vfr_departure: str = ""
    request_pushback: str = ""
    request_pattern_work: str = ""
    request_landing: str = ""
    request_taxi_to_parking: str = ""
    request_transition: str = ""
    request_flight_following: str = ""
    ready_for_departure_plain: str = ""
    # The clearance request with no destination in it, for a flight
    # that has not filed one: naming the call must not leave the
    # preposition dangling ("request IFR clearance to").
    request_clearance_plain: str = ""
    # The readback of a join: {leg} {direction} {runway}.
    pattern_entry: str = "{direction} {leg} runway {runway}"
    # An approach clearance, read back: "cleared ILS approach runway two five
    # left". Each book sets its own.
    cleared_approach: str = "cleared {approach} approach runway {runway}"
    # Reporting a leg of the circuit, and reading back the option.
    on_leg: str = "turning {leg}"
    option: str = "cleared for the option runway {runway}"


ENGLISH = PilotBook(
    language="en",
    request_clearance="request IFR clearance to {dest}",
    request_clearance_plain="request IFR clearance",
    request_taxi="request taxi",
    ready_for_departure="ready for departure runway {runway}",
    checking_in_climbing="climbing {altitude}",
    checking_in_descending="descending {altitude}",
    established="established on the localiser runway {runway}",
    field_in_sight="field in sight",
    traffic_in_sight="traffic in sight",
    taxi="taxi via {route} runway {runway}",
    hold_short="hold short runway {runway}",
    line_up="line up and wait runway {runway}",
    takeoff="cleared for takeoff runway {runway}",
    land="cleared to land runway {runway}",
    go_around="going around",
    contact="contact {station} {frequency}",
    squawk="squawk {code}",
    climb="climb {altitude}",
    descend="descend {altitude}",
    heading="heading {heading}",
    speed="{speed} knots",
    with_information="with information {letter}",
    roger="roger",
    wilco="wilco",
    good_day="good day",
    hello="good day",
    check_in="with you",
    request_vfr_departure="request VFR departure",
    request_pushback="request pushback",
    request_pattern_work="request closed traffic",
    request_landing="inbound for landing",
    request_taxi_to_parking="request taxi to parking",
    request_transition="request transition",
    request_flight_following="request flight following",
    ready_for_departure_plain="ready for departure",
)

FRENCH = PilotBook(
    language="fr",
    cleared_approach="autorisés approche {approach} piste {runway}",
    request_clearance="demande la clairance IFR pour {dest}",
    request_clearance_plain="demande la clairance IFR",
    request_taxi="demande le roulage",
    ready_for_departure="prêt au départ piste {runway}",
    checking_in_climbing="en montée {altitude}",
    checking_in_descending="en descente {altitude}",
    established="établi sur le localizer piste {runway}",
    field_in_sight="terrain en vue",
    traffic_in_sight="trafic en vue",
    taxi="roulage par {route} piste {runway}",
    hold_short="point d'attente piste {runway}",
    line_up="alignement piste {runway}",
    takeoff="autorisé décollage piste {runway}",
    land="autorisé atterrissage piste {runway}",
    go_around="remise de gaz",
    contact="contact {station} {frequency}",
    squawk="affiche {code}",
    climb="montons {altitude}",
    descend="descendons {altitude}",
    heading="cap {heading}",
    speed="{speed} nœuds",
    with_information="avec information {letter}",
    roger="bien reçu",
    wilco="wilco",
    good_day="bonne journée",
    hello="bonjour",
    check_in="pour vous",
    request_vfr_departure="demande un départ VFR",
    request_pushback="demande le repoussage",
    request_pattern_work="pour un tour de piste",
    request_landing="pour l'atterrissage",
    request_taxi_to_parking="demande le parking",
    request_transition="demande la traversée",
    request_flight_following="demande le service d'information de vol",
    ready_for_departure_plain="prêt au départ",
    pattern_entry="{leg} main {direction} piste {runway}",
    on_leg="en {leg}",
    option="autorisé option piste {runway}",
)

SPANISH = PilotBook(
    language="es",
    cleared_approach="autorizados aproximación {approach} pista {runway}",
    request_clearance="solicito autorización IFR para {dest}",
    request_clearance_plain="solicito autorización IFR",
    request_taxi="solicito rodaje",
    ready_for_departure="listo para salida pista {runway}",
    checking_in_climbing="ascendiendo {altitude}",
    checking_in_descending="descendiendo {altitude}",
    established="establecidos en el localizador pista {runway}",
    field_in_sight="campo a la vista",
    traffic_in_sight="tráfico a la vista",
    taxi="rodaje por {route} pista {runway}",
    hold_short="punto de espera pista {runway}",
    line_up="alineados y esperando pista {runway}",
    takeoff="autorizado a despegar pista {runway}",
    land="autorizado a aterrizar pista {runway}",
    go_around="motor y al aire",
    contact="contactamos {station} {frequency}",
    squawk="transpondedor {code}",
    climb="ascendemos {altitude}",
    descend="descendemos {altitude}",
    heading="rumbo {heading}",
    speed="{speed} nudos",
    with_information="con información {letter}",
    roger="recibido",
    wilco="wilco",
    good_day="buen día",
    hello="buenos días",
    check_in="le escuchamos",
    request_vfr_departure="solicito salida VFR",
    request_pushback="solicito retroceso",
    request_pattern_work="solicito circuitos de tráfico",
    request_landing="para el aterrizaje",
    request_taxi_to_parking="solicito rodaje a plataforma",
    request_transition="solicito el tránsito",
    request_flight_following="solicito servicio de información",
    ready_for_departure_plain="listo para salida",
    pattern_entry="{leg} por la {direction} pista {runway}",
    on_leg="en {leg}",
    option="autorizada la opción pista {runway}",
)

GERMAN = PilotBook(
    language="de",
    cleared_approach="freigegeben {approach} Anflug Piste {runway}",
    request_clearance="erbitte IFR Freigabe nach {dest}",
    request_clearance_plain="erbitte IFR Freigabe",
    request_taxi="erbitte Rollen",
    ready_for_departure="startbereit Piste {runway}",
    checking_in_climbing="im Steigflug {altitude}",
    checking_in_descending="im Sinkflug {altitude}",
    established="etabliert auf dem Landekurs Piste {runway}",
    field_in_sight="Platz in Sicht",
    traffic_in_sight="Verkehr in Sicht",
    taxi="rollen über {route} Piste {runway}",
    hold_short="Rollhalt Piste {runway}",
    line_up="einreihen und warten Piste {runway}",
    takeoff="Start frei Piste {runway}",
    land="Landung frei Piste {runway}",
    go_around="wir starten durch",
    contact="rufen {station} {frequency}",
    squawk="Squawk {code}",
    climb="steigen {altitude}",
    descend="sinken {altitude}",
    heading="Kurs {heading}",
    speed="{speed} Knoten",
    with_information="mit Information {letter}",
    roger="verstanden",
    wilco="wilco",
    good_day="schönen Tag",
    hello="guten Tag",
    check_in="bei Ihnen",
    request_vfr_departure="erbitte VFR Abflug",
    request_pushback="erbitte Pushback",
    request_pattern_work="erbitte Platzrunden",
    request_landing="zur Landung",
    request_taxi_to_parking="erbitte Rollen zur Parkposition",
    request_transition="erbitte den Durchflug",
    request_flight_following="erbitte Fluginformationsdienst",
    ready_for_departure_plain="startbereit",
    pattern_entry="{leg} {direction} Piste {runway}",
    on_leg="im {leg}",
    option="Option genehmigt Piste {runway}",
)

ITALIAN = PilotBook(
    language="it",
    cleared_approach="autorizzati avvicinamento {approach} pista {runway}",
    request_clearance="chiedo autorizzazione IFR per {dest}",
    request_clearance_plain="chiedo autorizzazione IFR",
    request_taxi="chiedo rullaggio",
    ready_for_departure="pronti al decollo pista {runway}",
    checking_in_climbing="in salita {altitude}",
    checking_in_descending="in discesa {altitude}",
    established="stabilizzati sul localizzatore pista {runway}",
    field_in_sight="campo in vista",
    traffic_in_sight="traffico in vista",
    taxi="rulliamo via {route} pista {runway}",
    hold_short="punto attesa pista {runway}",
    line_up="allineati e in attesa pista {runway}",
    takeoff="autorizzati al decollo pista {runway}",
    land="autorizzati all'atterraggio pista {runway}",
    go_around="riattacchiamo",
    contact="contattiamo {station} {frequency}",
    squawk="transponder {code}",
    climb="saliamo {altitude}",
    descend="scendiamo {altitude}",
    heading="prua {heading}",
    speed="{speed} nodi",
    with_information="con informazione {letter}",
    roger="ricevuto",
    wilco="wilco",
    good_day="buona giornata",
    hello="buongiorno",
    check_in="con voi",
    request_vfr_departure="chiedo partenza VFR",
    request_pushback="chiedo push back",
    request_pattern_work="chiedo circuiti",
    request_landing="per l'atterraggio",
    request_taxi_to_parking="chiedo rullaggio al parcheggio",
    request_transition="chiedo il transito",
    request_flight_following="chiedo servizio informazioni",
    ready_for_departure_plain="pronti al decollo",
    pattern_entry="{leg} {direction} pista {runway}",
    on_leg="in {leg}",
    option="autorizzato all'opzione pista {runway}",
)

PORTUGUESE = PilotBook(
    language="pt",
    cleared_approach="autorizados aproximação {approach} pista {runway}",
    request_clearance="peço autorização IFR para {dest}",
    request_clearance_plain="peço autorização IFR",
    request_taxi="peço táxi",
    ready_for_departure="prontos para descolar pista {runway}",
    checking_in_climbing="a subir {altitude}",
    checking_in_descending="a descer {altitude}",
    established="estabelecidos no localizador pista {runway}",
    field_in_sight="campo à vista",
    traffic_in_sight="tráfego à vista",
    taxi="taxiamos por {route} pista {runway}",
    hold_short="ponto de espera pista {runway}",
    line_up="alinhados e a aguardar pista {runway}",
    takeoff="autorizados a descolar pista {runway}",
    land="autorizados a aterrar pista {runway}",
    go_around="arremetemos",
    contact="contactamos {station} {frequency}",
    squawk="transponder {code}",
    climb="subimos {altitude}",
    descend="descemos {altitude}",
    heading="proa {heading}",
    speed="{speed} nós",
    with_information="com informação {letter}",
    roger="recebido",
    wilco="wilco",
    good_day="bom dia",
    hello="bom dia",
    check_in="convosco",
    request_vfr_departure="peço partida VFR",
    request_pushback="peço push back",
    request_pattern_work="peço circuitos",
    request_landing="para a aterragem",
    request_taxi_to_parking="peço táxi para o estacionamento",
    request_transition="peço o trânsito",
    request_flight_following="peço serviço de informação",
    ready_for_departure_plain="prontos para descolar",
    pattern_entry="{leg} pela {direction} pista {runway}",
    on_leg="na {leg}",
    option="autorizada a opção pista {runway}",
)


BOOKS: dict[str, PilotBook] = {
    "en": ENGLISH, "fr": FRENCH, "es": SPANISH, "de": GERMAN,
    "it": ITALIAN, "pt": PORTUGUESE,
}


class PilotSpeech:
    """One aircraft's side of the radio.

    Built around a phraseology object so the numbers come out identically to
    the way the controller said them, which is the whole point of a readback.
    """

    def __init__(self, language: str, phraseology):
        self.book = BOOKS.get((language or "en").lower()[:2], ENGLISH)
        self.p = phraseology
        # Whether the next call should open with a greeting. You say good
        # morning the first time you speak to a position, not every time --
        # and at an uncontrolled field, where every call goes to the same
        # traffic frequency, greeting each one sounds like a stuck record.
        self.greet = True

    @property
    def language(self) -> str:
        return self.book.language

    # ------------------------------------------------------------------
    # calling up
    # ------------------------------------------------------------------

    def _sign(self, aircraft: Aircraft, abbreviated: bool = False) -> str:
        return aircraft.spoken(abbreviated)

    def call(self, aircraft: Aircraft, station: str, body: str,
             greeting: bool = False) -> str:
        """An initial call: who you are calling, who you are, what you want.

        A first call to a position carries a greeting, because a real one does.
        "Orly Sol, Air France mille deux cent trente-quatre, bonjour, demande
        le roulage" is how the frequency actually sounds, and a roomful of
        aeroplanes that never says good morning sounds like a machine.
        """
        parts = [station, self._sign(aircraft)]
        if greeting and self.greet and self.book.hello:
            parts.append(self.book.hello)
        parts.append(body)
        return _join(*parts) + "."

    def report(self, aircraft: Aircraft, body: str,
               farewell: bool = False) -> str:
        """A report or a readback: what you are doing, then who you are.

        The callsign goes last, which is the real convention: the controller
        already knows they are being spoken to and needs the identity to file
        what they just heard against. A transmission that is the last one on
        this frequency signs off before it, which is the other half of the
        greeting.
        """
        # A slot left empty -- a handoff with no station name -- leaves a
        # double space or a space before a comma in the frame: "contact  one
        # one eight". Closed up here, once, for every frame.
        body = re.sub(r"\s+", " ", body).replace(" ,", ",").strip()
        parts = [body]
        if farewell and self.book.good_day:
            parts.append(self.book.good_day)
        parts.append(self._sign(aircraft, True))
        return _join(*parts) + "."

    # ------------------------------------------------------------------
    # requests
    # ------------------------------------------------------------------

    def request_clearance(self, aircraft: Aircraft, station: str,
                          destination: str, atis_letter: str = "") -> str:
        body = self.book.request_clearance.format(dest=destination)
        if atis_letter:
            body = _join(body, self.book.with_information.format(
                letter=self.p.d(atis_letter) if atis_letter.isdigit()
                else _spell(atis_letter)))
        return self.call(aircraft, station, body, greeting=True)

    def request_taxi(self, aircraft: Aircraft, station: str) -> str:
        return self.call(aircraft, station, self.book.request_taxi,
                         greeting=True)

    def request_vfr_departure(self, aircraft: Aircraft, station: str) -> str:
        return self.call(aircraft, station, self.book.request_vfr_departure,
                         greeting=True)

    def inbound_for_landing(self, aircraft: Aircraft, station: str) -> str:
        """A VFR arrival calling the tower up, from outside the circuit."""
        return self.call(aircraft, station, self.book.request_landing,
                         greeting=True)

    def pattern_entry(self, aircraft: Aircraft, runway: str,
                      leg: str = "", direction: str = "") -> str:
        """The readback of a join: the leg, the side and the runway."""
        return self.report(aircraft, self.book.pattern_entry.format(
            leg=leg or self.p.leg("downwind"),
            direction=direction or self.p.side("left"),
            runway=self.p.rwy(runway)))

    def ready_for_departure(self, aircraft: Aircraft, runway: str,
                            station: str = "") -> str:
        body = self.book.ready_for_departure.format(runway=self.p.rwy(runway))
        return (self.call(aircraft, station, body, greeting=True) if station
                else self.report(aircraft, body))

    def checking_in(self, aircraft: Aircraft, station: str, altitude_ft: float,
                    climbing: bool) -> str:
        frame = (self.book.checking_in_climbing if climbing
                 else self.book.checking_in_descending)
        return self.call(aircraft, station,
                         frame.format(altitude=self.p.alt(altitude_ft)),
                         greeting=True)

    def established(self, aircraft: Aircraft, runway: str) -> str:
        return self.report(
            aircraft, self.book.established.format(runway=self.p.rwy(runway)))

    def field_in_sight(self, aircraft: Aircraft) -> str:
        return self.report(aircraft, self.book.field_in_sight)

    def traffic_in_sight(self, aircraft: Aircraft) -> str:
        return self.report(aircraft, self.book.traffic_in_sight)

    def going_around(self, aircraft: Aircraft, altitude_ft: float = 0.0) -> str:
        """"Going around, climb three thousand": with the altitude the
        controller gave, which is what the readback is for."""
        body = self.book.go_around
        if altitude_ft:
            body += ", " + self.book.climb.format(altitude=self.p.alt(altitude_ft))
        return self.report(aircraft, body)

    def approach(self, aircraft: Aircraft, approach: str, runway: str,
                 altitude_ft: float = 0.0, pressure: float = 0.0) -> str:
        """An approach clearance read back: the approach and runway, the
        altitude, and the pressure setting if one came with it."""
        body = self.book.cleared_approach.format(
            approach=approach, runway=self.p.rwy(runway))
        if altitude_ft:
            body += ", " + self.p.alt(altitude_ft)
        if pressure:
            body += ", " + self.p.pressure(pressure)
        return self.report(aircraft, body)

    def on_leg(self, aircraft: Aircraft, leg: str) -> str:
        """Reporting a leg of the circuit: "turning base", "en finale"."""
        return self.report(aircraft, self.book.on_leg.format(leg=leg))

    def option(self, aircraft: Aircraft, runway: str) -> str:
        return self.report(
            aircraft, self.book.option.format(runway=self.p.rwy(runway)))

    # ------------------------------------------------------------------
    # readbacks
    # ------------------------------------------------------------------

    def taxi(self, aircraft: Aircraft, runway: str, route: str = "",
             hold_short: str = "") -> str:
        parts = []
        if route:
            parts.append(self.book.taxi.format(route=route,
                                               runway=self.p.rwy(runway)))
        else:
            parts.append(self.book.hold_short.format(runway=self.p.rwy(runway)))
        if hold_short:
            parts.append(self.book.hold_short.format(
                runway=self.p.rwy(hold_short)))
        return self.report(aircraft, _join(*parts))

    def line_up(self, aircraft: Aircraft, runway: str) -> str:
        return self.report(
            aircraft, self.book.line_up.format(runway=self.p.rwy(runway)))

    def takeoff(self, aircraft: Aircraft, runway: str) -> str:
        return self.report(
            aircraft, self.book.takeoff.format(runway=self.p.rwy(runway)))

    def land(self, aircraft: Aircraft, runway: str) -> str:
        return self.report(
            aircraft, self.book.land.format(runway=self.p.rwy(runway)))

    def contact(self, aircraft: Aircraft, station: str, mhz: float) -> str:
        """The readback of a handoff, which is the last call on this frequency."""
        return self.report(aircraft, self.book.contact.format(
            station=station, frequency=self.p.freq(mhz)), farewell=True)

    def leaving(self, aircraft: Aircraft) -> str:
        """Signing off a frequency with nowhere in particular to go next."""
        return self.report(aircraft, self.book.wilco, farewell=True)

    def squawk(self, aircraft: Aircraft, code: str) -> str:
        return self.report(aircraft,
                           self.book.squawk.format(code=self.p.d(code)))

    def clearance(self, aircraft: Aircraft, destination: str, squawk: str,
                  initial_altitude: float = 0.0) -> str:
        """The readback of a departure clearance: limit, altitude, squawk."""
        parts = [destination]
        if initial_altitude:
            parts.append(self.book.climb.format(
                altitude=self.p.alt(initial_altitude)))
        parts.append(self.book.squawk.format(code=self.p.d(squawk)))
        return self.report(aircraft, _join(*parts))

    def level(self, aircraft: Aircraft, altitude_ft: float,
              climbing: bool = True) -> str:
        frame = self.book.climb if climbing else self.book.descend
        return self.report(aircraft,
                           frame.format(altitude=self.p.alt(altitude_ft)))

    def heading(self, aircraft: Aircraft, degrees: float) -> str:
        return self.report(
            aircraft, self.book.heading.format(heading=self.p.hdg(degrees)))

    def speed(self, aircraft: Aircraft, knots: float) -> str:
        return self.report(
            aircraft, self.book.speed.format(speed=self.p.d(int(knots))))

    # ------------------------------------------------------------------
    # the short ones
    # ------------------------------------------------------------------

    def roger(self, aircraft: Aircraft) -> str:
        return self.report(aircraft, self.book.roger)

    def wilco(self, aircraft: Aircraft) -> str:
        return self.report(aircraft, self.book.wilco)

    def farewell(self, aircraft: Aircraft) -> str:
        return self.report(aircraft, self.book.good_day)


def _spell(letter: str) -> str:
    """The ICAO word for an ATIS letter, which is the same in every language."""
    from .speech import _ALPHABET

    return _ALPHABET.get((letter or "").upper()[:1], letter)


__all__ = ["PilotBook", "PilotSpeech", "BOOKS", "ENGLISH", "FRENCH", "SPANISH",
           "GERMAN", "ITALIAN", "PORTUGUESE"]
