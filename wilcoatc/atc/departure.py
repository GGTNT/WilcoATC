# -*- coding: utf-8 -*-
"""What the controller does about a departure it has already held.

The third of the watches, after :mod:`wilcoatc.atc.watch` for the surface and
:mod:`wilcoatc.atc.arrival` for the way in. All three exist for the same
reason: a controller is not a thing that answers, it is a thing that works a
flight, and everything here happens because of what the aeroplane is doing
rather than because somebody keyed a microphone.

Three watches. The first is for the moment the program used to lose the
flight entirely, below. The other two are the IFR plan's (Phase 1): the
climb given in steps before each is reached (:class:`ClimbWatch`), and a
flight off the SID it was cleared on (:class:`SidWatch`).

An aeroplane asks for takeoff with somebody landing behind it. The tower does
the right thing: it puts it on the runway and tells it to line up and wait.
Then the arrival lands, the runway clears, and nothing happens. There was no
path anywhere in the controller that went back to a departure it had held, so
the only way out was for the pilot to ask again -- and a pilot who has been
told to wait does not ask again, because they have been told to wait. The
aeroplane sat on the runway for the rest of the flight.

So: when the runway is free, the clearance the aeroplane is waiting for is
issued. That is the whole of this module.

What it does not do
-------------------

It does not un-hold anything else. A pilot told to hold short, hold position
or give way is being held for a reason that is about the ground rather than
about the runway, and the taxi routes here are synthetic -- the program does
not know enough about a taxiway to know when one is clear. The runway is the
one piece of an airport this program does model, and it is the one piece
where being left waiting is the difference between a flight and no flight.
"""

from __future__ import annotations

import logging

from ..navdata.db import NavDB, Station
from ..logs import decision
from . import climb
from .controller import ControllerBrain, Reply
from .phraseology import Weather, language_of
from .route import CLB
from .session import FlightSession, Instruction, station_key

log = logging.getLogger(__name__)

# How long to leave an aeroplane sitting there before the clearance goes out.
#
# Not for realism -- a real tower clears you the moment the wheels of the
# landing traffic are on the ground. It is for the arrival that has touched
# down and not yet slowed: the traffic model frees the runway on a timer, and
# a couple of seconds either side of that is the difference between a
# clearance and a clearance issued over the top of somebody's rollout.
SETTLE_S = 4.0


class DepartureWatch:
    """Releases a departure the tower has put on the runway and held."""

    def __init__(self, navdb: NavDB, brain: ControllerBrain):
        self.navdb = navdb
        self.brain = brain
        # When the runway was last seen to be occupied, so a clearance is not
        # issued in the same instant it comes free.
        self._busy_at = 0.0

    # ------------------------------------------------------------------

    def look(
        self,
        session: FlightSession,
        station: Station | None,
        state,
        now: float,
        weather_for=None,
        language: str = "en",
    ) -> Reply | None:
        """One look at the departure. Returns the clearance, or nothing."""
        if station is None or station.position != "TWR":
            return None
        if state is None or not state.on_ground:
            return None
        if session.emergency:
            return None
        if session.cleared_takeoff and self.brain.realism.delays:
            return self._expedite(session, station, state, now, language)
        # Lined up and not yet cleared. Both halves matter: an aeroplane still
        # at the holding point has not been put anywhere it cannot leave, and
        # one already cleared is not waiting for anything. Held short for an
        # arrival counts as lined up: in the US nothing is lined up behind one.
        if not (session.lined_up or session.held_for_landing) \
                or session.cleared_takeoff:
            return None
        # And on its way out. An aeroplane that has landed and is still on the
        # runway is not waiting for a takeoff clearance.
        if not session.phase.outbound:
            return None

        field = self.navdb.airport(station.ident)
        if not self.brain.runway_available(field, session.departure_runway):
            self._busy_at = now
            return None
        if now - self._busy_at < SETTLE_S:
            return None

        p = self.brain.for_station(station, language)
        session.aircraft.language = language_of(p)
        weather = self._weather(weather_for, field, station)

        reply = self.brain.release_for_takeoff(session, station, weather, p,
                                               state)
        if reply is None or not reply.speak:
            return None
        # Only the clearance. Anything else the tower might have answered --
        # "hold position", "you are already cleared" -- is an answer to a
        # question, and nobody asked one.
        if not session.cleared_takeoff:
            return None
        return reply

    # ------------------------------------------------------------------

    # Cleared and still standing this long, with an arrival this close on
    # the final: "expedite". UNVERIFIED: both figures.
    EXPEDITE_AFTER_S = 12.0
    EXPEDITE_WITHIN_NM = 3.0

    def _expedite(self, session, station, state, now, language) -> Reply | None:
        """A departure cleared in front of an arrival that has not moved."""
        if not session.cleared_takeoff_at:
            return None
        if now - session.cleared_takeoff_at < self.EXPEDITE_AFTER_S:
            return None
        if float(getattr(state, "ground_speed_kt", 0.0) or 0.0) >= 5.0:
            return None
        field = self.navdb.airport(station.ident)
        coming = self.brain.arrivals_on_final(field, session.departure_runway,
                                              self.EXPEDITE_WITHIN_NM)
        if not coming or not session.advise_once("expedite:takeoff"):
            return None
        p = self.brain.for_station(station, language)
        session.aircraft.language = language_of(p)
        described = (coming[0].indefinite(language_of(p))
                     or coming[0].describe(language_of(p)))
        decision("takeoff", airport=station.ident,
                 runway=session.departure_runway, said="expedite",
                 arrival=coming[0].written or None,
                 arrival_nm=round(coming[0].distance_nm, 1),
                 standing_s=round(now - session.cleared_takeoff_at))
        return Reply(p.expedite(session.aircraft, p.traffic_on_final(
            described, coming[0].distance_nm)), station)

    def began(self) -> None:
        """A new flight, so forget which runway was busy on the last one."""
        self._busy_at = 0.0

    @staticmethod
    def _weather(weather_for, field, station) -> Weather:
        """The weather at the field, calm and clear where there is none."""
        ident = (field.ident if field is not None else "") or station.ident
        if weather_for is not None:
            try:
                return weather_for(ident)
            except Exception:
                log.debug("no weather for %s", ident, exc_info=True)
        return Weather()


class ClimbWatch:
    """The next step of an IFR climb, given before the last one is reached.

    Departure's steps up to the top of its airspace, then each centre's own
    level, then the cruise (atc/climb.py). Departure gives the next step to
    a flight still climbing within 1,500 ft of the last, so the climb never
    stops; a centre lets the flight come up to its level and gives the next
    after a pause that is its own. Only to a flight that has checked in and
    owes nothing: a step on top of an unread clearance is two instructions
    where the pilot has acknowledged neither.
    """

    def __init__(self, brain: ControllerBrain):
        self.brain = brain

    def look(self, session: FlightSession, station: Station | None, state,
             now: float, language: str = "en") -> Reply | None:
        brain = self.brain
        if not (brain.realism.climb_steps and session.ifr) or station is None:
            return None
        if station.position not in ("DEP", "APP", "CTR"):
            return None
        if state is None or state.on_ground or session.emergency:
            return None
        if not session.established_with(station):
            return None
        pending = session.pending
        if pending is not None and not pending.read_back:
            return None
        cruise = float(session.cruise_altitude_ft or 0)
        if not brain._climbing_out(session, state, cruise):
            return None
        here = float(state.altitude_ft or 0.0)
        vs = float(state.vertical_speed_fpm or 0.0)
        assigned = float(session.assigned_altitude_ft or 0.0)
        if not assigned or assigned >= cruise:
            return None
        callsign = session.aircraft.callsign.written
        if station.position in ("DEP", "APP"):
            if not climb.departure_step_due(here, vs, assigned, cruise):
                return None
            step = climb.departure_step(station.callsign, callsign, assigned,
                                        here, cruise)
            why = "departure's step"
        else:
            if here < assigned - climb.CENTRE_REACHING_FT:
                session.level_reached_with = ""
                return None
            if session.level_reached_with != station_key(station):
                session.level_reached_with = station_key(station)
                session.level_reached_at = now
                return None
            if now - session.level_reached_at < climb.centre_wait_s(
                    station.callsign):
                return None
            step = climb.centre_step(station.callsign, here, assigned, cruise)
            why = "the centre's step" if step < cruise else "the cruise"
        if step <= assigned:
            return None
        session.level_reached_with = ""
        p = brain.for_station(station, language)
        session.aircraft.language = language_of(p)
        field = self.brain.navdb.airport(station.ident)
        text = p.climb_maintain(
            session.aircraft, step,
            transition_altitude=brain._transition_altitude(field))
        decision("climb", event="step", station=station.callsign,
                 here_ft=round(here), assigned_ft=round(assigned),
                 step_ft=int(step), why=why)
        instruction = session.remember(Instruction(
            "altitude", station_key(station), text, {"altitude_ft": int(step)}))
        return Reply(text, station, instruction=instruction)


class SidWatch:
    """A flight off the SID it was cleared on: "verify you are on the ...
    departure", once.

    More than 2 nm across the SID's path, measured on the route's legs
    between the SID's first fix and its last -- not on the leg from the
    runway to the first fix, which every departure turns onto from runway
    heading and which a plan draws as a straight line it is not. A
    constraint busted is not watched: a SimBrief plan carries the altitude
    the aeroplane is planned to cross each fix at, not the SID's own
    restriction, and calling a deviation from a plan is not a controller's
    business. Nor is "resume own navigation", which follows a vector, and
    departure gives none before the vectors of Phase 5.
    """

    OFF_PATH_NM = 2.0
    # Off for this long, so the turn at a fix is not a deviation.
    OFF_FOR_S = 30.0

    def __init__(self, brain: ControllerBrain):
        self.brain = brain
        self._off_since = 0.0

    def look(self, session: FlightSession, station: Station | None, state,
             now: float, language: str = "en") -> Reply | None:
        brain = self.brain
        route = brain.route
        if not (brain.realism.sid_watch and session.ifr and session.sid
                and route) or station is None:
            return None
        if station.position not in ("DEP", "APP", "TWR", "CTR"):
            return None
        if state is None or state.on_ground or not session.phase.outbound:
            return None
        fixes = route.procedure(route.sid, CLB)
        if len(fixes) < 2:
            return None
        first = route.fixes.index(fixes[0])
        last = route.fixes.index(fixes[-1])
        found = route.progress(state.latitude, state.longitude)
        if found is None or not (first < found.leg <= last):
            self._off_since = 0.0
            return None
        if abs(found.cross_track_nm) <= self.OFF_PATH_NM:
            self._off_since = 0.0
            return None
        if not self._off_since:
            self._off_since = now
            return None
        if now - self._off_since < self.OFF_FOR_S:
            return None
        if not session.advise_once("verify_sid"):
            return None
        p = brain.for_station(station, language)
        session.aircraft.language = language_of(p)
        decision("sid_watch", sid=session.sid, leg=found.leg_name,
                 cross_track_nm=round(found.cross_track_nm, 1),
                 station=station.callsign)
        return Reply(p.verify_on_departure(session.aircraft, session.sid),
                     station)


__all__ = ["DepartureWatch", "ClimbWatch", "SidWatch", "SETTLE_S"]
