# -*- coding: utf-8 -*-
"""What the controller does about a departure it has already held.

The third of the watches, after :mod:`wilcoatc.atc.watch` for the surface and
:mod:`wilcoatc.atc.arrival` for the way in. All three exist for the same
reason: a controller is not a thing that answers, it is a thing that works a
flight, and everything here happens because of what the aeroplane is doing
rather than because somebody keyed a microphone.

This one is small, because there is only one moment on a departure where the
controller owes the pilot a call it has not been asked for -- and it is the
moment the program used to lose the flight entirely.

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
from .controller import ControllerBrain, Reply
from .phraseology import Weather, language_of
from .session import FlightSession

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
        # Lined up and not yet cleared. Both halves matter: an aeroplane still
        # at the holding point has not been put anywhere it cannot leave, and
        # one already cleared is not waiting for anything.
        if not session.lined_up or session.cleared_takeoff:
            return None
        # And on its way out. An aeroplane that has landed and is still on the
        # runway is not waiting for a takeoff clearance.
        if not session.phase.outbound:
            return None

        if not self.brain.runway_available():
            self._busy_at = now
            return None
        if now - self._busy_at < SETTLE_S:
            return None

        field = self.navdb.airport(station.ident)
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


__all__ = ["DepartureWatch", "SETTLE_S"]
