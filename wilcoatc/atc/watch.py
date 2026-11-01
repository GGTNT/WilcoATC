"""What the controller notices without being told.

Everything else in this package answers the pilot. This watches the aircraft.

The two are not the same job, and leaving the second one out is why a flight
could take a runway, depart and land again without a single clearance and never
hear a word about it. A controller sitting in the cab is not waiting to be
addressed; they are looking out of the window, and an aircraft that rolls
without a clearance gets called on it before it is airborne.

This module is the surface half of it. The other two are
:mod:`wilcoatc.atc.departure`, for a flight the tower has put on the runway
and told to wait, and :mod:`wilcoatc.atc.arrival`, for one nobody has said
anything to on the way in.

Three things are worth calling, and they are exactly the three where being
somewhere without a clearance is an event in its own right rather than a matter
of taste:

* **taking a runway** you have not been cleared onto;
* **departing** without a takeoff clearance;
* **landing** without a landing clearance.

Each is said once. A controller does not repeat an advisory four times a second
because the simulator is being polled that often.

What is deliberately *not* called is crossing a runway on the way to another
one. Real ATC issues a crossing clearance for that and this project does not
yet: a route read off the simulator's taxiways (:mod:`wilcoatc.atc.taxi`)
names the first runway to hold short of, but nothing clears the aircraft
across it, and without the simulator the route is invented and the aircraft
is bound to cross runways it never mentioned. Calling those would mean crying
wolf on every taxi at a large field. The discriminator is
alignment: an aircraft lined up on a runway is pointing along it, and an
aircraft crossing one is pointing across it.
"""

from __future__ import annotations

import logging

from ..navdata.build import bearing_deg, project
from ..navdata.db import NavDB, Station, normalize_runway
from ..logs import decision
from .controller import ControllerBrain, Reply
from .phraseology import language_of
from .session import FlightSession

log = logging.getLogger(__name__)

# How far from the field the surface watch still applies. Beyond this the
# aircraft is flying, not manoeuvring on an airport.
FIELD_RADIUS_NM = 8.0

# An aircraft pointing within this many degrees of the runway heading is lined
# up on it. Anything further off is crossing it, which is not called.
ALIGNED_DEG = 35.0

# A takeoff roll and a landing rollout both pass through this speed. Requiring
# it stops a simulator that has just connected, or an aircraft placed in the
# air by the user, from reading as a departure.
ROLLING_KT = 20.0


class SurfaceWatch:
    """Watches the aircraft and speaks when it does something uncleared."""

    def __init__(self, navdb: NavDB, brain: ControllerBrain):
        self.navdb = navdb
        self.brain = brain

    # ------------------------------------------------------------------

    def look(
        self,
        session: FlightSession,
        station: Station | None,
        state,
        previous,
        language: str = "en",
    ) -> Reply | None:
        """One look at the aircraft. Returns something to say, or nothing.

        ``previous`` is the state at the last poll, which is what makes a
        takeoff or a touchdown visible: both are transitions, and neither can
        be read off a single snapshot.
        """
        if not self.brain.issues_clearances(station):
            # An uncontrolled field has nobody in the cab, and a broadcast
            # frequency cannot transmit to one aircraft. Silence is correct.
            # Nor does an information service: at Kortrijk's AFIS, after
            # "take off at your discretion", this called the line-up an
            # incursion and the takeoff a pilot deviation.
            return None
        if state is None or previous is None or not state.has_position:
            return None

        airport = self.navdb.airport(station.ident)
        if airport is None:
            return None
        if state.distance_to(airport.lat, airport.lon) > FIELD_RADIUS_NM:
            return None

        # A long taxi cut in two: at the taxiway it was told to hold short
        # of, the rest of the route.
        if station.position in ("GND", "TWR", "DEL"):
            rest = self.brain.rest_of_taxi(
                session, station, self.brain.for_station(station, language),
                state)
            if rest is not None:
                return rest

        runway = self._runway_under(airport.ident, state)
        if state.on_ground:
            session.occupying_runway = runway
            if not runway:
                # Off the pavement again. Entering later is a fresh event, so
                # the advisory for each runway is armed once more. A runway
                # is the strip, both its ends (``NavDB.runway_strip``): one
                # entry backtracked along 06 to the 24 threshold was two.
                session.advisories -= {
                    key for key in session.advisories if key.startswith("incursion:")
                }

        p = self.brain.for_station(station, language)
        aircraft = session.aircraft
        # The callsign is spoken differently in each language, and an advisory
        # is a transmission like any other, so the aircraft is told which one
        # this is in before a template runs.
        aircraft.language = language_of(p)

        # --- departed without a takeoff clearance ---------------------
        # Not a liftoff off a landing rollout: a bounce, a touch-and-go or a
        # go-around after touchdown is classified by the engine, and none of
        # them is a takeoff (``session.rollout_liftoff_at``).
        if (previous.on_ground and not state.on_ground
                and previous.ground_speed_kt > ROLLING_KT
                and not session.cleared_takeoff
                and not session.rollout_liftoff_at
                and session.advise_once("departed_without_clearance")):
            used = session.occupying_runway or session.departure_runway or runway
            decision("unauthorised_departure", runway=used,
                     speed_kt=round(float(previous.ground_speed_kt or 0.0)),
                     phase=session.phase.value)
            return Reply(
                p.departed_without_clearance(
                    aircraft, used, self._tower_name(station, p)
                ),
                station,
            )

        # --- landed without a landing clearance -----------------------
        if (not previous.on_ground and state.on_ground
                and state.ground_speed_kt > ROLLING_KT
                and not session.cleared_landing
                and not session.cleared_option
                and session.advise_once("landed_without_clearance")):
            used = runway or session.occupying_runway or session.arrival_runway
            return Reply(
                p.landed_without_clearance(
                    aircraft, used, self._tower_name(station, p)
                ),
                station,
            )

        # --- lined up on a runway without a clearance -----------------
        if state.on_ground and runway and not self._allowed(
                airport.ident, session, runway):
            if self._aligned(runway, self.magnetic_heading(state)):
                key = f"incursion:{self.navdb.runway_strip(airport.ident, runway)}"
                if session.advise_once(key):
                    return Reply(p.hold_position_incursion(aircraft, runway), station)

        return None

    # ------------------------------------------------------------------

    def _runway_under(self, ident: str, state) -> str:
        # True, because the runway ends it is compared with are true.
        heading = getattr(state, "heading_true", None)
        try:
            return self.navdb.runway_at(
                ident, state.latitude, state.longitude, heading
            )
        except Exception:
            log.debug("runway lookup failed at %s", ident, exc_info=True)
            return ""

    def _allowed(self, ident: str, session: FlightSession, runway: str) -> bool:
        """Whether a clearance the aircraft holds covers this runway -- the
        whole strip, so an aeroplane cleared onto 24 may backtrack along it
        pointing at 06."""
        cleared = session.cleared_runway
        if not cleared:
            return False
        return (self.navdb.runway_strip(ident, cleared)
                == self.navdb.runway_strip(ident, runway))

    @staticmethod
    def magnetic_heading(state) -> float | None:
        """The aeroplane's heading from magnetic north, which is what a
        runway's number is.

        Worked out from the true heading and the model rather than read off
        the simulator's magnetic one, which a stand-in source leaves at
        zero -- and ``heading_true or heading_magnetic`` took a real heading
        of zero for no heading at all.
        """
        from ..navdata.magvar import to_magnetic, variation

        heading = getattr(state, "heading_true", None)
        if heading is None or not getattr(state, "has_position", False):
            return heading
        return to_magnetic(heading, variation(state.latitude, state.longitude))

    @staticmethod
    def _aligned(runway: str, heading: float | None) -> bool:
        """Lined up along the runway rather than crossing it.

        ``heading`` is magnetic: the runway's number is. Compared with a
        true heading it was out by the variation, which at Kennedy turned a
        forty-degree crossing into a line-up.
        """
        if heading is None:
            return False
        from ..navdata.db import _heading_from_ident

        runway_heading = _heading_from_ident(runway)
        if runway_heading is None:
            return False
        apart = abs(((float(heading) - runway_heading) + 180.0) % 360.0 - 180.0)
        return apart <= ALIGNED_DEG

    def _tower_name(self, station: Station, p) -> str:
        """Who to call about it afterwards: the tower, whoever you are on."""
        tower = self.navdb.station_for(station.ident, "TWR") or station
        return tower.callsign_in(language_of(p))




# ----------------------------------------------------------------------
# the airspace
# ----------------------------------------------------------------------

# What each class asks of a VFR flight before it enters (ICAO Annex 11
# 2.6; FAA AIM 3-2): A is closed to VFR, B needs a clearance, C and D need
# two-way contact (and, outside the US, the tower's leave to enter), E, F
# and G need nothing.
ENTRY_RULES = {"A": "no_vfr", "B": "clearance", "C": "contact", "D": "contact"}
# How far ahead the watch looks for the edge: a minute at the aeroplane's
# ground speed, which is the warning a controller tries to give.
LOOKAHEAD_S = 60.0
# Below this an aeroplane is not going anywhere fast enough to warn.
MOVING_KT = 40.0
# A climb or descent this quick is looked along too, a minute's worth.
CLIMBING_FPM = 300.0
# The positions that give a VFR flight a radar service.
RADAR = ("DEP", "APP", "CTR")


class AirspaceWatch:
    """A VFR aeroplane about to enter airspace it needs something for.

    The infringement half of a controller's job, which nothing did: a
    Cessna could fly through Denver's Class B at 9,000 ft and hear nothing.
    Looking a minute ahead along the track, with the volumes from
    ``NavDB.airspace_at``:

    * an aeroplane that has not called the unit that owns the volume, and
      is listening to it, is told it is entering without clearance, given a
      heading out of it and told to remain outside -- firmly, once;
    * one that is on frequency is let in where two-way contact is not
      enough on its own: outside the US by "enter the zone via (point)",
      and anywhere a Class B by being told to remain clear until it is
      cleared into it.

    Only the volume's own unit speaks -- the field the volume belongs to --
    because nobody else is listening for that aeroplane. Each volume is
    said once.
    """

    def __init__(self, navdb: NavDB, brain: ControllerBrain):
        self.navdb = navdb
        self.brain = brain

    def _speaks_for(self, volume, station: Station) -> bool:
        """Whether the unit on this frequency owns the volume.

        The field it is filed under, and for a Class B or C the radar room
        that works it, whichever field that room is filed at: Denver's Class
        B belongs to KDEN in the data and is Denver Departure's on
        Centennial's 132.750. A Class D is the tower's own.
        """
        if volume.airport == station.ident:
            return True
        if station.position not in RADAR or volume.cls not in ("B", "C"):
            return False
        room = (station.facility or "").strip().lower()
        if not room:
            return False
        if volume.name.lower().startswith(room + " "):
            return True
        return any(other.facility.strip().lower() == room
                   for other in self.navdb.stations(volume.airport)
                   if other.position in RADAR)

    def look(self, session: FlightSession, station: Station | None, state,
             language: str = "en") -> Reply | None:
        # Not on an information service: it clears nobody into anything, so
        # it has no "without a clearance" to say.
        if station is None or not self.brain.issues_clearances(station):
            return None
        if session.ifr or session.emergency or state is None:
            return None
        if not state.has_position or state.on_ground:
            return None
        speed = float(getattr(state, "ground_speed_kt", 0.0) or 0.0)
        if speed < MOVING_KT:
            return None
        from .final import course_over_ground

        track = course_over_ground(state)
        if track is None:
            return None
        altitude = float(state.altitude_ft or 0.0)
        here = {v.id for v in self.navdb.airspace_at(
            state.latitude, state.longitude, altitude)}
        lat, lon = project(state.latitude, state.longitude, track,
                           speed * LOOKAHEAD_S / 3600.0)
        ahead = [(lat, lon, altitude)]
        # And through a floor or a ceiling: climbing at 8,200 ft into the
        # shelf over Centennial was never across an edge at the same height,
        # so it was never seen.
        climb = float(getattr(state, "vertical_speed_fpm", 0.0) or 0.0)
        if abs(climb) >= CLIMBING_FPM:
            level = altitude + climb * LOOKAHEAD_S / 60.0
            ahead += [(state.latitude, state.longitude, level),
                      (lat, lon, level)]
        entering, seen = [], set(here)
        for at_lat, at_lon, at_ft in ahead:
            for v in self.navdb.airspace_at(at_lat, at_lon, at_ft):
                if v.id not in seen and v.cls in ENTRY_RULES:
                    seen.add(v.id)
                    entering.append(v)
        entering = [v for v in sorted(entering, key=lambda v: v.cls)
                    if self._speaks_for(v, station)]
        if not entering:
            return None
        volume = entering[0]
        key = f"airspace:{volume.id}"
        if key in session.advisories:
            return None
        rule = ENTRY_RULES[volume.cls]
        established = session.established_with(station)
        p = self.brain.for_station(station, language)
        session.aircraft.language = language_of(p)
        airspace = p.airspace(volume.cls)
        if established and rule == "contact":
            if p.region == "faa":
                return None
            if (session.transition_approved or session.expected_vrp
                    or session.pattern_leg or session.phase.name in (
                        "APPROACH", "LANDING")):
                return None
            session.advise_once(key)
            airport = self.navdb.airport(volume.airport)
            point = self.brain.vrp_ahead(airport, state) if airport else None
            text = p.enter_zone(session.aircraft, airspace,
                                p.vrp(point.name) if point else "")
            decision("airspace", volume=volume.name, cls=volume.cls,
                     said="enter", via=point.name if point else None)
            return Reply(text, station)
        if established and rule == "clearance":
            if session.cleared_bravo == volume.airport or session.cleared_landing:
                return None
            session.advise_once(key)
            decision("airspace", volume=volume.name, cls=volume.cls,
                     said="remain_clear")
            return Reply(p.remain_clear_of_airspace(session.aircraft, airspace),
                         station)
        if established and rule != "no_vfr":
            return None
        # Not on frequency, or Class A: the way out is straight away from
        # the middle of the volume, as a magnetic heading.
        session.advise_once(key)
        south, west, north, east = volume.bbox
        away = bearing_deg((south + north) / 2.0, (west + east) / 2.0,
                           state.latitude, state.longitude)
        from ..navdata.magvar import to_magnetic, variation

        heading = round(to_magnetic(away, variation(state.latitude,
                                                    state.longitude)) / 10.0) * 10.0
        heading = heading % 360.0 or 360.0
        turn = "right" if ((away - track) % 360.0) < 180.0 else "left"
        name = self.brain.station_name(station, p) if not established else ""
        decision("airspace", volume=volume.name, cls=volume.cls,
                 said="infringement", heading=heading, turn=turn,
                 established=established)
        return Reply(p.infringement(session.aircraft, name, airspace,
                                    p.side(turn), heading), station)


class ReportingPointWatch:
    """A reporting point a VFR flight was told to report and flew past
    without a word: "say position", once.

    The arrival's entry point (``session.expected_vrp``) is past when the
    aeroplane is nearer the field than the point is; the departure's exit
    point (``session.expected_exit``) when it is further out than the point.
    Either way it is half a mile past it and still more than half a mile
    from it, so an aeroplane flying over the point is not asked where it is.
    """

    PAST_NM = 0.5

    def __init__(self, navdb: NavDB, brain: ControllerBrain):
        self.navdb = navdb
        self.brain = brain

    def look(self, session: FlightSession, station: Station | None, state,
             language: str = "en") -> Reply | None:
        from ..navdata.build import haversine_nm
        from .session import Instruction, station_key

        if not self.brain.realism.reporting_points:
            return None
        if station is None or station.position != "TWR":
            return None
        if not self.brain.staffed(station) or not session.established_with(station):
            return None
        if session.ifr or state is None or not state.has_position or state.on_ground:
            return None
        field = self.navdb.airport(station.ident)
        if field is None:
            return None
        for name, outbound in ((session.expected_vrp, False),
                               (session.expected_exit, True)):
            if not name:
                continue
            point = next((v for v in self.navdb.vrps(field.ident)
                          if v.name == name), None)
            if point is None:
                continue
            point_out = haversine_nm(field.lat, field.lon, point.lat, point.lon)
            out = state.distance_to(field.lat, field.lon)
            there = state.distance_to(point.lat, point.lon)
            past = (out > point_out + self.PAST_NM if outbound
                    else out < point_out - self.PAST_NM)
            if not past or there <= self.PAST_NM:
                continue
            if not session.advise_once(f"vrp_chase:{field.ident}:{name}"):
                continue
            p = self.brain.for_station(station, language)
            session.aircraft.language = language_of(p)
            decision("vrp", airport=field.ident, use="chased", point=name,
                     outbound=outbound, out_nm=round(out, 2),
                     point_nm=round(point_out, 2), from_point_nm=round(there, 2))
            text = p.say_position(session.aircraft)
            instruction = session.remember(Instruction(
                "say_position", station_key(station), text, {},
                readback_required=False))
            return Reply(text, station, instruction=instruction)
        return None


class RunwayWatch:
    """The tower telling the frequency the runway in use has changed.

    The runway is kept until the wind takes it away
    (``ControllerBrain._wind_runway``); when it does, a pilot on the tower
    frequency is told -- everybody at once, "all stations, runway in use now
    two four", with the new information letter where the field has one --
    and a VFR flight still in the circuit for the old runway is told which
    way round to join for the new one. A flight cleared to land, lined up or
    cleared for takeoff keeps the runway it was given; one waiting on the
    ground is moved to the new one for its takeoff clearance.

    Said once per change and only to a flight that was given the old runway:
    the first look at a field, before any METAR has come, chooses from calm,
    and the real wind arriving is not a change anybody was told about.
    """

    def __init__(self, navdb: NavDB, brain: ControllerBrain, weather_for,
                 atis_for=None):
        self.navdb = navdb
        self.brain = brain
        self.weather_for = weather_for
        self.atis_for = atis_for

    def look(self, session: FlightSession, station: Station | None, state,
             language: str = "en") -> Reply | None:
        from .controller import _airborne
        from .session import Instruction, station_key

        if not self.brain.realism.runway_change:
            return None
        if station is None or station.position != "TWR":
            return None
        if not self.brain.staffed(station) or not session.established_with(station):
            return None
        airport = self.navdb.airport(station.ident)
        if airport is None:
            return None
        weather = self.weather_for(station.ident)
        # Asking is what notices the wind has taken the runway away.
        self.brain.arrival_runway(airport, weather)
        self.brain.departure_runway(airport, weather)
        change = self.brain.runway_changes.get(station.ident)
        if change is None:
            return None
        was, now = change
        heard = f"{station.ident}:{now}"
        if session.runway_change_heard == heard:
            return None
        session.runway_change_heard = heard
        if was not in (session.arrival_runway, session.departure_runway):
            decision("runway_change", airport=station.ident, said="nothing",
                     was=was, now=now, why="never given the old runway")
            return None

        p = self.brain.for_station(station, language)
        name = self.brain.station_name(station, p)
        def capital(sentence: str) -> str:
            return sentence[:1].upper() + sentence[1:]

        text = capital(p.runway_change(name, now))
        atis = self.atis_for(station.ident, language) if self.atis_for else None
        letter = getattr(atis, "letter", "") if atis is not None else ""
        if letter:
            pressure = (weather.altimeter_inhg if getattr(p, "dialect", "") == "faa"
                        else weather.qnh_hpa)
            text = f"{text} {capital(p.information_current(letter, pressure))}"

        airborne = _airborne(state)
        flight = "untouched"
        instruction = None
        if (airborne and session.visual and not session.cleared_landing
                and session.arrival_runway == was):
            session.arrival_runway = now
            session.allow_runway("")
            session.pattern_leg = ""
            session.expected_report = "downwind"
            session.expected_report_chased = False
            session.base_owed_behind = ""
            direction = self.brain._pattern_direction(session, None, p, airport, now)
            session.aircraft.language = language_of(p)
            rejoin = p.enter_pattern(session.aircraft, "", now, leg="",
                                     direction=direction, report=p.join_report())
            text = f"{text} {rejoin}"
            flight = "rejoin"
            instruction = session.remember(Instruction(
                "pattern_entry", station_key(station), rejoin,
                {"runway": now, "pattern_direction": session.pattern_direction}))
        elif (not airborne and session.departure_runway == was
                and not session.lined_up and not session.cleared_takeoff):
            session.departure_runway = now
            flight = "departure_moved"
        elif session.cleared_landing or session.cleared_takeoff or session.lined_up:
            flight = "kept"
        decision("runway_change", airport=station.ident, said="broadcast",
                 was=was, now=now, letter=letter or None, flight=flight)
        return Reply(text, station, instruction=instruction)


__all__ = ["AirspaceWatch", "ENTRY_RULES", "ReportingPointWatch",
           "RunwayWatch", "SurfaceWatch",
           "FIELD_RADIUS_NM", "ALIGNED_DEG", "ROLLING_KT"]


class RadarServiceWatch:
    """The end of a VFR flight's radar service, said by the controller.

    "Radar service terminated, squawk VFR, frequency change approved" was
    only ever said to flight following, at the edge of its range. A VFR
    departure the tower handed to Denver with a code got "radar contact"
    and then nothing, all the way north out of the area (Centennial, 26
    October). The service it has is the same, and ends the same way: once
    the aeroplane is plainly leaving -- ``LEAVING_NM`` out from the field
    the room is filed at, flying away from it, with no destination near
    it. Flight following is handed on by range instead (Engine
    ._offer_handoff), because the flight asked to be watched the whole way.
    """

    LEAVING_NM = 15.0
    # Within this of straight away from the field.
    AWAY_DEG = 70.0

    def __init__(self, navdb: NavDB, brain: ControllerBrain):
        self.navdb = navdb
        self.brain = brain

    def look(self, session: FlightSession, station: Station | None, state,
             language: str = "en") -> Reply | None:
        from ..navdata.build import haversine_nm
        from .final import course_over_ground
        from .session import Instruction, station_key

        if station is None or station.position not in RADAR:
            return None
        if session.ifr or session.emergency or session.flight_following:
            return None
        key = station_key(station)
        if session.radar_service != key:
            return None
        if state is None or not state.has_position or state.on_ground:
            return None
        airport = self.navdb.airport(station.ident)
        if airport is None:
            return None
        distance = haversine_nm(airport.lat, airport.lon,
                                state.latitude, state.longitude)
        if distance < self.LEAVING_NM:
            return None
        track = course_over_ground(state)
        away = bearing_deg(airport.lat, airport.lon,
                           state.latitude, state.longitude)
        if track is None or abs((track - away + 540.0) % 360.0 - 180.0) \
                > self.AWAY_DEG:
            return None
        destination = session.destination
        if destination is not None and haversine_nm(
                airport.lat, airport.lon, destination.lat,
                destination.lon) <= 2 * self.LEAVING_NM:
            # Going somewhere in this room's own area: it is kept.
            return None
        if not session.advise_once(f"radar:end:{key}"):
            return None
        p = self.brain.for_station(station, language)
        session.aircraft.language = language_of(p)
        session.radar_service = ""
        session.squawk = p.vfr_squawk
        text = p.radar_service_terminated(session.aircraft)
        decision("radar_service", station=station.callsign, said="terminated",
                 distance_nm=round(distance, 1), track=round(track))
        instruction = session.remember(Instruction(
            "frequency_change", key, text, {}, readback_required=False))
        return Reply(text, station, instruction=instruction)


class RestrictionWatch:
    """A VFR flight above the altitude a radar controller holds it under.

    At 8,200 ft into Denver's Class B shelf with "maintain VFR at or below
    seven thousand five hundred", nobody said a word: the restriction was
    given and never looked at again. ``ABOVE_FT`` over it the controller
    asks first -- an encoder can be wrong, and "say altitude" is how one is
    checked -- and if the answer leaves it up there, ``ANSWER_S`` later,
    tells it to descend back under. Back within ``BACK_UNDER_FT`` of the
    restriction, the next climb through it is asked about again.
    """

    ABOVE_FT = 300.0
    BACK_UNDER_FT = 100.0
    ANSWER_S = 15.0

    def __init__(self, navdb: NavDB, brain: ControllerBrain):
        self.navdb = navdb
        self.brain = brain

    def look(self, session: FlightSession, station: Station | None, state,
             now: float, language: str = "en") -> Reply | None:
        from .session import Instruction, station_key

        if station is None or station.position not in RADAR:
            return None
        if session.ifr or session.emergency:
            return None
        ceiling = float(session.vfr_ceiling_ft or 0.0)
        key = station_key(station)
        if not ceiling or session.radar_service != key:
            return None
        if state is None or not state.has_position or state.on_ground:
            return None
        altitude = float(state.altitude_ft or 0.0)
        if altitude < ceiling + self.BACK_UNDER_FT:
            session.restriction_asked_at = 0.0
            session.advisories.discard(f"restriction:descend:{key}")
            return None
        if altitude < ceiling + self.ABOVE_FT:
            return None
        p = self.brain.for_station(station, language)
        session.aircraft.language = language_of(p)
        if not session.restriction_asked_at:
            session.restriction_asked_at = now
            decision("vfr_restriction", station=station.callsign,
                     said="say_altitude", altitude_ft=round(altitude),
                     ceiling_ft=round(ceiling))
            return Reply(p.say_altitude(session.aircraft), station)
        if now - session.restriction_asked_at < self.ANSWER_S:
            return None
        if not session.advise_once(f"restriction:descend:{key}"):
            return None
        transition = self.brain._transition_altitude(
            self.navdb.airport(station.ident))
        text = p.descend_at_or_below(session.aircraft, ceiling,
                                     transition_altitude=transition)
        decision("vfr_restriction", station=station.callsign, said="descend",
                 altitude_ft=round(altitude), ceiling_ft=round(ceiling))
        instruction = session.remember(Instruction(
            "vfr_ceiling", key, text, {"vfr_ceiling_ft": ceiling}))
        return Reply(text, station, instruction=instruction)
