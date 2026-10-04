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
one. Real ATC issues a crossing clearance for that and this project cannot: no
open dataset publishes taxiway layouts, so the taxi routes are synthetic and
the aircraft is bound to cross runways the route never mentioned. Calling those
would mean crying wolf on every taxi at a large field. The discriminator is
alignment: an aircraft lined up on a runway is pointing along it, and an
aircraft crossing one is pointing across it.
"""

from __future__ import annotations

import logging

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
        if not self.brain.staffed(station):
            # An uncontrolled field has nobody in the cab, and a broadcast
            # frequency cannot transmit to one aircraft. Silence is correct.
            return None
        if state is None or previous is None or not state.has_position:
            return None

        airport = self.navdb.airport(station.ident)
        if airport is None:
            return None
        if state.distance_to(airport.lat, airport.lon) > FIELD_RADIUS_NM:
            return None

        runway = self._runway_under(airport.ident, state)
        if state.on_ground:
            session.occupying_runway = runway
            if not runway:
                # Off the pavement again. Entering later is a fresh event, so
                # the advisory for each runway is armed once more.
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
        if state.on_ground and runway and not self._allowed(session, runway):
            if self._aligned(runway, self.magnetic_heading(state)):
                key = f"incursion:{normalize_runway(runway)}"
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

    @staticmethod
    def _allowed(session: FlightSession, runway: str) -> bool:
        """Whether a clearance the aircraft holds covers this runway."""
        cleared = session.cleared_runway
        if not cleared:
            return False
        return normalize_runway(cleared) == normalize_runway(runway)

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


__all__ = ["SurfaceWatch", "FIELD_RADIUS_NM", "ALIGNED_DEG", "ROLLING_KT"]
