# -*- coding: utf-8 -*-
"""What the controller does about an arrival without being asked.

:mod:`wilcoatc.atc.watch` is the same idea for the surface and
:mod:`wilcoatc.atc.departure` for a flight held on the runway: things a
controller says because of what the aeroplane is doing rather than because
somebody keyed a microphone. This is the third of them, and it is the one
that was missing.

Everything an arrival needs already existed and every bit of it was reactive.
A pilot who asked was descended, told which approach to expect, cleared for it
and cleared to land; a pilot who said nothing flew from cruise to the flare
without hearing a word, because nothing in the program ever spoke first about
an arrival. That is not a controller. The descent and the approach clearance
are the two instructions a real one *must* issue -- an aeroplane may not leave
its cruising level or fly an approach without them -- and the landing
clearance is the call famous for never being forgotten.

Four things, in the order a flight meets them:

* **the descent**, in steps, from the top of descent down to an altitude the
  approach can be intercepted from;
* **the approach to expect**, which is what makes the runway known early
  enough to be set up for;
* **the approach clearance**, at the range a radar controller gives it, just
  before the aircraft is handed to the tower;
* **the landing clearance**, from the tower, on final.

Each is said once. What stops one arriving twice is the mechanism the surface
advisories already use -- :meth:`~wilcoatc.atc.session.FlightSession
.advise_once` -- keyed by the level or the event rather than by the step, so
the descent sequences itself without this module remembering anything between
one look and the next.

Why it does not fly a profile
-----------------------------

Because the aeroplane is being flown by somebody. A descent issued on a
schedule arrives while the pilot is still level at the last one, and three of
those in a row is a controller talking to itself. So each step waits until
the aeroplane is near the level it was last given, which means a flight that
ignores a descent is not descended again -- it is left where the pilot put it,
exactly as it would be on the radio.

Nothing here vectors, either. Radar vectors need a controller that knows where
the final approach course is and can turn the aircraft on to it, and an
autopilot following a flight plan does not want to be turned off it. What is
issued is the descent and the clearance. The routing stays the pilot's.
"""

from __future__ import annotations

import logging
import math
import time

from ..navdata.build import bearing_deg, haversine_nm
from ..navdata.db import NavDB, Station
from .controller import ControllerBrain, Reply
from ..logs import decision
from .approaches import VISUAL
from .final import final_for
from .phraseology import Weather, language_of
from .procedures import ON_STAR_NM, along_to, frame, star_join
from .procedures import written as written_procedure
from .route import DSC
from . import holds as hold_rules
from . import vectors as vec
from .session import FlightSession, Instruction, Phase, station_key

log = logging.getLogger(__name__)

# Three miles per thousand feet is what a jet descends at and close enough for
# everything slower. The margin is where the profile reaches the intercept
# altitude, short of the field: the level segment before the approach, and
# room to slow down.
#
# The profile used to come down to the *field* at twelve miles and a step was
# issued only once it had come down to the step's own level -- so each step
# arrived when the aeroplane was already late for it, and a model run was
# still at eleven thousand feet when it was cleared for the ILS at eighteen
# miles. Now the profile reaches the intercept altitude, and the next step is
# issued as soon as the profile meets the level the aeroplane is at.
DESCENT_GRADIENT_NM = 3.0
DESCENT_MARGIN_NM = 15.0

# The first descent is cleared this long before the top of descent, at the
# groundspeed, because an FMS works out its own top of descent and starts
# down on it: waiting for the plan's was a flight leaving its level without
# a clearance, or held up there past it (after LocalTC: 5 minutes).
DESCENT_LEAD_MIN = 5.0

# Where a centre takes an arrival in the flight levels down to before
# approach has it, above the field; and the arrival altitude a short, low
# flight is given instead (after LocalTC: 10,000 ft for a jet up high --
# Seattle's 11,000 -- else 2,500 ft over the field and never under 3,000).
CENTRE_ABOVE_FIELD_FT = 10000.0
ARRIVAL_ABOVE_FIELD_FT = 2500.0
ARRIVAL_MIN_FT = 3000.0
# A flight cruising at or above this is "up in the levels".
HIGH_CRUISE_FT = 18000.0

# A crossing restriction is at a fix of the route this far from the field,
# still at least CROSS_AHEAD_NM ahead of the aeroplane and in front of it
# (after LocalTC: 30 to 60 nm, 20 nm ahead).
CROSS_FROM_NM = 30.0
CROSS_TO_NM = 60.0
CROSS_AHEAD_NM = 20.0

# Already going down this fast without a descent clearance: the descent is
# late, and said plainly.
DESCENDING_FPM = -500.0

# Vectors are given to an arrival this close to its field and no further:
# out beyond it the route is still the pilot's (UNVERIFIED: a choice).
VECTOR_FROM_NM = 40.0
# Room between two vectors, or a vector and a level, except the turn onto
# the final (the IFR plan: "room between instructions").
VECTOR_GAP_S = 30.0
# A new heading is said only when it differs from the last by this much.
VECTOR_CHANGE_DEG = 15.0
# A visual approach is offered, by asking for the field in sight, this many
# track miles from the runway.
FIELD_QUESTION_NM = 25.0

# Speed control (the IFR plan, Phase 5 (c)): only below this, only while
# vectoring, and only with traffic landing ahead -- another aeroplane nearer
# the field, inside SPEED_TRAFFIC_NM of it and below SPEED_TRAFFIC_AGL_FT
# (after LocalTC: 10,000 ft, 25 nm, 8,000 ft; slowing every arrival with
# nobody about was "unrealistic").
SPEED_BELOW_FT = 10000.0
SPEED_TRAFFIC_NM = 25.0
SPEED_TRAFFIC_AGL_FT = 8000.0
# The speeds: on the pattern, and on the final to a point out (UNVERIFIED:
# typical figures, 7110.65 5-7-2's minimum 210 kt for a jet beyond 20 nm).
PATTERN_SPEED_KT = 210
FINAL_SPEED_KT = 160
FINAL_SPEED_UNTIL_NM = 4

# On a STAR that runs onto the final, the approach is cleared this far along
# it from the join (after LocalTC: 25 nm).
STAR_CLEAR_NM = 25.0

# The first descent has been cleared. Consulted by the stepped descent,
# which takes over from it, and by "expect lower", which is pointless after.
FIRST_DESCENT_KEY = "arrival:descent_cleared"

# How long before the first descent a centre says "expect lower in N
# minutes": inside the larger figure, and not when there are fewer than the
# smaller left, because then the descent itself is next. UNVERIFIED: what
# is heard on centre frequencies, not a published figure.
EXPECT_LOWER_MIN = (2, 6)

# The lowest a radar position descends an arrival to. Below that it is the
# approach's business and the aeroplane is on a glideslope rather than on a
# level. Measured above the field rather than above the sea: somewhere at six
# thousand feet is not a field anybody is descended to three thousand over.
INTERCEPT_ABOVE_FT = 3000.0

# How near the level it was last given an aeroplane has to be before the next
# step down is issued. Two thousand feet is about a minute of descent, which
# is when a controller reaches for the next one.
NEAR_THE_LEVEL_FT = 2000.0

# Where the runway is named, and where the approach clearance is issued. The
# clearance is measured from the threshold and given only on the approach side
# of it, within APPROACH_CONE_DEG of the final course -- an intercept from a
# base is inside that, a downwind is not (see :mod:`wilcoatc.atc.final`).
#
# The clearance is deliberately further out than the range at which the engine
# hands an arrival to the tower. An aircraft has to be cleared for the
# approach by the radar position *before* it is passed on; the other order is
# a tower being called by an aeroplane nobody has cleared for anything.
EXPECT_APPROACH_NM = 35.0
CLEARED_APPROACH_NM = 18.0
APPROACH_CONE_DEG = 45.0

# Close enough to the intercept altitude for the approach clearance alone.
# Higher than this inside the range and the last descent goes with it.
NEAR_INTERCEPT_FT = 1500.0

# How far out the tower clears an arrival to land. Further out than this and
# the clearance has to be cancelled the moment anything changes, which is the
# same reason :class:`~wilcoatc.atc.controller.ControllerBrain` holds one back
# from a pilot who asks too early.
#
# It has to stay inside that class's own limit, which is twelve miles. The
# clearance itself is composed there, and asking for one from outside it gets
# "continue approach" -- a perfectly good answer to a pilot who called, and a
# strange thing to say to one who has not said anything.
LANDING_CLEARANCE_NM = 10.0

# An unannounced go-around: climbing away below this height, within this of
# the threshold, inside the final-approach cone.
GO_AROUND_BELOW_AGL_FT = 1500.0
GO_AROUND_WITHIN_NM = 5.0
# And it has to be a climb, not a bump: held this long, and this far above
# the lowest point of the final.
GO_AROUND_SUSTAIN_S = 4.0
GO_AROUND_GAIN_FT = 200.0

# The advisory key for the approach clearance. Named because three things
# consult it: the clearance itself, so it is issued once; the runway to
# expect, which is pointless after it; and the descent, which is the
# procedure's business once the approach has been cleared.
CLEARED_APPROACH_KEY = "arrival:cleared_approach"

# Below this an aircraft near the field is arriving at it rather than passing
# over the top of it. Generous on purpose: it is only ever a guard against
# clearing an airliner at cruise to land somewhere it happens to be near.
ARRIVING_BELOW_AGL_FT = 10000.0


class ArrivalWatch:
    """Works an arrival down the descent and on to the runway."""

    def __init__(self, navdb: NavDB, brain: ControllerBrain):
        self.navdb = navdb
        self.brain = brain
        # (field, runway, STAR) already logged as joining the final or not.
        self._star_logged: set = set()

    # ------------------------------------------------------------------

    def look(
        self,
        session: FlightSession,
        station: Station | None,
        state,
        previous,
        weather_for=None,
        language: str = "en",
    ) -> Reply | None:
        """One look at the arrival. Returns something to say, or nothing.

        ``previous`` is the state at the last poll, and it is what says the
        aeroplane is closing on the field rather than leaving it. An aircraft
        eight miles out and going away is a departure, and a departure cleared
        to land is worse than a controller that says nothing at all.

        ``weather_for`` is a function rather than a reading, and it is called
        only where a runway or a wind is about to be spoken. This runs on the
        situation loop several times a second for the whole flight, and the
        engine's weather is a cache in front of an observation fetch -- asking
        it every look would put a network call on the loop once a minute from
        the gate onwards, for a runway nobody is going to be told about until
        thirty-five miles out.
        """
        if not self.brain.staffed(station):
            return None
        # Nothing until the pilot has called this station. Approach used to
        # start descending an aeroplane that had only dialled the frequency,
        # and a tower cleared to land one that had never said a word to it.
        if not session.established_with(station):
            return None
        if state is None or previous is None or not state.has_position:
            return None
        if state.on_ground or session.phase.on_ground:
            return None
        if session.emergency:
            return None

        # Which field this is an arrival at. For a radar position it is the
        # one the flight is going to, and there is nothing to do without one.
        # For a tower it is the tower's own field and never anywhere else --
        # which is also the right answer for a flight that filed no
        # destination, and for one that has diverted to where it is now
        # talking rather than to where it set off for.
        if station.position in ("APP", "DEP", "CTR"):
            field = session.destination
        elif station.position == "TWR":
            field = self.navdb.airport(station.ident)
        else:
            return None
        if field is None:
            return None

        to_run = state.distance_to(field.lat, field.lon)
        # Going away is a departure -- unless approach sent it away: a
        # downwind is flown outbound, and the turn onto base never came.
        if to_run >= previous.distance_to(field.lat, field.lon) \
                and not session.vector_leg and not session.going_around \
                and session.hold is None:
            return None

        # An aeroplane that is still climbing away is a departure, whatever
        # it happens to be pointing at. That matters most where the departure
        # and the destination are the same field: a circuit turns downwind
        # inside ten miles, closing, below a thousand feet, and every other
        # test here would read it as an arrival on final.
        if session.phase.outbound and state.climbing \
                and not session.going_around:
            return None

        p = self.brain.for_station(station, language)
        # The callsign is spoken differently in each language, and this is a
        # transmission like any other, so the aircraft is told which language
        # it is being worked in before a template runs.
        session.aircraft.language = language_of(p)

        if station.position == "TWR":
            return self._tower(session, station, state, field, to_run,
                               weather_for, p)
        return self._radar(session, station, state, field, to_run,
                           weather_for, p)

    # ------------------------------------------------------------------
    # the radar position: down, and on to an approach
    # ------------------------------------------------------------------

    def _radar(self, session, station, state, destination, to_run,
               weather_for, p) -> Reply | None:
        """The descent, the runway to expect, and the approach clearance.

        Nearest first. A flight that comes into range of two of these on the
        same look gets the later one, because the later one is the one that
        supersedes: an aeroplane eighteen miles out wants its approach
        clearance, not the descent it should have had at forty.
        """
        if self.brain.realism.holds and session.ifr:
            held = self._holding(session, station, state, destination,
                                 to_run, weather_for, p)
            if held is not None or session.hold is not None:
                # Holding: nothing else is said meanwhile -- no descent below
                # the hold's level, no vector, no clearance.
                return held
        return (
            self._approach_clearance(session, station, state, destination,
                                     to_run, weather_for, p)
            or self._radar_vectors(session, station, state, destination,
                                   to_run, weather_for, p)
            or self._speed_control(session, station, state, destination,
                                   to_run, p)
            or self._expect_approach(session, station, state, destination,
                                     to_run, weather_for, p)
            or self._first_descent(session, station, state, destination,
                                   to_run, weather_for, p)
            or self._descent(session, station, state, destination, to_run,
                             weather_for, p)
            or self._expect_lower(session, station, state, destination,
                                  to_run, p)
        )

    def _expect_lower(self, session, station, state, destination, to_run,
                      p) -> Reply | None:
        """"Expect lower in five minutes", once, before the first descent.

        A centre says it so the pilot can plan the top of descent rather
        than meet it. Worked out from the same profile :meth:`_descent`
        issues the first step on, so the minutes said are the minutes it
        comes in.
        """
        if not session.ifr or station.position != "CTR":
            return None
        if any(key.startswith("arrival:descend:") or key in (
                CLEARED_APPROACH_KEY, FIRST_DESCENT_KEY)
               for key in session.advisories):
            return None
        pending = session.pending
        if pending is not None and pending.station_key == station_key(station):
            return None
        speed = float(getattr(state, "ground_speed_kt", 0.0) or 0.0)
        if speed < 100.0:
            return None
        current = float(state.altitude_ft or 0.0)
        level = max(current, float(session.assigned_altitude_ft or 0.0))
        floor = self.floor(destination, state, session.arrival_runway)
        if current <= floor + 5000.0:
            return None
        if self.brain.realism.ifr_descent:
            # Where _first_descent will clear it: its own miles, so the
            # minutes said are the minutes it comes in.
            to_go, start, lead = self._descent_point(destination, state,
                                                     level, to_run, session)
            minutes = int(round((to_go - start - lead) / speed * 60.0))
            top_nm = start + lead
        else:
            # Where _descent's profile comes down to a thousand feet over
            # the level, which is where it issues the first step.
            top_nm = DESCENT_MARGIN_NM + (level + 1000.0 - floor) / 1000.0                 * DESCENT_GRADIENT_NM
            minutes = int(round((to_run - top_nm) / speed * 60.0))
        if not EXPECT_LOWER_MIN[0] <= minutes <= EXPECT_LOWER_MIN[1]:
            return None
        if not session.advise_once("arrival:expect_lower"):
            return None
        decision("descent", airport=destination.ident, said="expect lower",
                 minutes=minutes, to_run_nm=round(to_run, 1),
                 top_of_descent_nm=round(top_nm, 1))
        text = p.expect_lower(session.aircraft, minutes)
        instruction = session.remember(
            Instruction("expect_lower", station_key(station), text, {},
                        readback_required=False))
        return Reply(text, station, instruction=instruction)

    # ------------------------------------------------------------------
    # the first descent, before the top of descent (the IFR plan, Phase 2)
    # ------------------------------------------------------------------

    def _descent_point(self, destination, state, level_ft: float,
                       to_run: float, session=None) -> tuple[float, float, float]:
        """(miles still to fly, where the descent starts, the lead), all
        measured the same way: along the filed route when there is one and
        the engine knows where the aeroplane is on it, else straight to the
        field with three miles a thousand feet."""
        route = self._route(destination)
        progress = self.brain.route_progress if route else None
        elevation = float(getattr(destination, "elev_ft", 0.0) or 0.0)
        if route and progress is not None:
            last = route.fixes[-1]
            to_go = progress.to_go_nm + haversine_nm(
                last.lat, last.lon, destination.lat, destination.lon)
            direct = route.fix(getattr(session, "route_direct", "") or "")
            if direct is not None:
                # Sent direct: the legs cut short are not still to fly.
                i = route.fixes.index(direct)
                to_go = (state.distance_to(direct.lat, direct.lon)
                         + route._along(i, len(route.fixes) - 1)
                         + haversine_nm(last.lat, last.lon,
                                        destination.lat, destination.lon))
            start = route.descent_distance_nm(destination.lat, destination.lon,
                                              level_ft, elevation)
        else:
            to_go = to_run
            start = max(30.0, (level_ft - elevation) / 1000.0 * 3.0)
        speed = float(getattr(state, "ground_speed_kt", 0.0) or 0.0)
        return to_go, start, speed / 60.0 * DESCENT_LEAD_MIN

    def arrival_plan(self, destination, state, cruise_ft: float) -> dict:
        """The altitudes an arrival is taken down to: ``centre`` (where the
        first descent goes) and ``arrival`` (the arrival altitude), each kept
        above the terrain round the field and round the aeroplane."""
        elevation = float(getattr(destination, "elev_ft", 0.0) or 0.0)
        cruise = float(cruise_ft or 0.0) or HIGH_CRUISE_FT
        arrival = max(ARRIVAL_MIN_FT, math.ceil(
            (elevation + ARRIVAL_ABOVE_FIELD_FT) / 1000.0) * 1000.0)
        arrival = min(cruise, arrival)
        centre = (max(ARRIVAL_MIN_FT, math.ceil(
            (elevation + CENTRE_ABOVE_FIELD_FT) / 1000.0) * 1000.0)
            if cruise >= HIGH_CRUISE_FT else arrival)
        terrain = self.floor(destination, state)
        return {"centre": max(centre, terrain), "arrival": max(arrival, terrain),
                "terrain": terrain}

    def _crossing_fix(self, destination, state):
        """The route fix 30 to 60 miles from the field, nearest it first,
        still twenty miles ahead and in front of the aeroplane; or None."""
        route = self._route(destination)
        if not route:
            return None
        track = float(getattr(state, "heading_true", 0.0) or 0.0)
        for fix in reversed(route.fixes[:-1]):
            if fix.is_marker:
                continue
            out = haversine_nm(fix.lat, fix.lon, destination.lat, destination.lon)
            if not CROSS_FROM_NM <= out <= CROSS_TO_NM:
                continue
            if state.distance_to(fix.lat, fix.lon) <= CROSS_AHEAD_NM:
                continue
            bearing = bearing_deg(state.latitude, state.longitude,
                                  fix.lat, fix.lon)
            if abs((bearing - track + 540.0) % 360.0 - 180.0) < 90.0:
                return fix
        return None

    def _first_descent(self, session, station, state, destination, to_run,
                       weather_for, p, force: bool = False) -> Reply | None:
        """The descent from the cruise, cleared before the top of descent.

        The stepped descent below waited for a profile to come down to the
        aeroplane, which an FMS holding its level until its own top of
        descent then met late: O'Hare had a flight at FL180 28 miles out,
        asking for lower. Cleared five minutes ahead instead, in the form
        that fits: "descend via" the filed STAR (the STAR named and a level
        given outside the US), else a crossing restriction at a route fix
        30-60 miles out, else at the pilot's discretion -- and, already on
        the way down or past the top of descent, plainly. The pressure
        setting rides on it when it leaves the flight levels.
        """
        if not self.brain.realism.ifr_descent or not session.ifr:
            return None
        if FIRST_DESCENT_KEY in session.advisories \
                or CLEARED_APPROACH_KEY in session.advisories:
            return None
        pending = session.pending
        if pending is not None and pending.station_key == station_key(station):
            return None
        speed = float(getattr(state, "ground_speed_kt", 0.0) or 0.0)
        if speed < 100.0:
            return None
        current = float(state.altitude_ft or 0.0)
        assigned = float(session.assigned_altitude_ft or 0.0)
        level = max(current, assigned)
        to_go, start, lead = self._descent_point(destination, state, level,
                                                 to_run, session)
        if to_go > start + lead and not force:
            return None

        route = self._route(destination)
        cruise = float(session.cruise_altitude_ft
                       or getattr(route, "cruise_ft", 0) or level)
        plan = self.arrival_plan(destination, state, cruise)
        weather = self._weather(weather_for, destination)
        target = self.brain.out_of_the_layer(destination, weather, plan["centre"])
        late = to_go < start or float(getattr(state, "vertical_speed_fpm", 0.0)
                                      or 0.0) <= DESCENDING_FPM
        star = getattr(route, "star", "") if route else ""
        idents = [f.ident for f in route.fixes] if route else []
        faa = getattr(p, "dialect", "") == "faa"
        values: dict = {"climbing": False}
        required = None
        kind = "altitude"
        cross = None

        if target >= current - 500.0:
            form = ""
        elif star:
            star = written_procedure(star, idents)
            if faa:
                # The STAR's own altitudes down to its last: that is where
                # the clearance ends, and the level the session holds.
                floor = float(getattr(route, "arrival_floor_ft", 0)
                              or plan["arrival"])
                target = max(floor, plan["terrain"])
                values["star"] = star
                required = {"star"}
                kind = "descend_via"
                session.descend_via = star
            form = "descend via" if faa else "star named"
        elif not late and (cross := self._crossing_fix(destination, state)):
            form = "crossing"
        elif not late:
            form = "pilot's discretion"
        else:
            form = "late"

        session.advise_once(FIRST_DESCENT_KEY)
        decision("descent", airport=destination.ident, first=True,
                 form=form or "none, already low", target_ft=int(target),
                 from_ft=round(level), to_go_nm=round(to_go, 1),
                 starts_nm=round(start, 1), lead_nm=round(lead, 1),
                 star=star or None, cross=getattr(cross, "ident", None),
                 terrain_ft=int(plan["terrain"]),
                 floor_source=("sector (MSA grid)" if to_run > self.brain.MSA_RADIUS_NM
                               else "intercept, " + self.brain.altitude_source(
                                   destination, session.arrival_runway)))
        if not form:
            return None
        session.advise_once(f"arrival:descend:{int(target)}")
        session.assigned_altitude_ft = target
        session.phase = Phase.DESCENT
        name = self._name(session, station, p)
        abbreviated = bool(session.established_with(station))
        terms = self.brain.descent_terms(destination, weather, p, level, target)
        if form in ("descend via", "star named"):
            text = p.descend_via(session.aircraft, star, station=name,
                                 abbreviated=abbreviated, level_ft=target,
                                 **terms)
        elif form == "crossing":
            values["cross_fix"] = cross.ident
            text = p.descend_crossing(session.aircraft, cross.ident, target,
                                      station=name, abbreviated=abbreviated,
                                      **terms)
        else:
            text = p.descend_maintain(
                session.aircraft, target, station=name, abbreviated=abbreviated,
                pilots_discretion=form == "pilot's discretion", **terms)
        values["altitude_ft"] = int(target)
        instruction = session.remember(
            Instruction(kind, station_key(station), text, values,
                        required_items=required))
        return Reply(text, station, instruction=instruction)

    def _descent(self, session, station, state, destination, to_run,
                 weather_for, p) -> Reply | None:
        """One step down, once the profile has come down to it."""
        if not session.ifr:
            # A VFR flight picks its own altitude. It is told where the field
            # is, not what level to be at.
            return None
        if CLEARED_APPROACH_KEY in session.advisories:
            # Cleared for the approach. What the aeroplane does about its
            # altitude from here is the procedure's, and a level issued over
            # the top of a glideslope is a controller countermanding it.
            return None

        # The first descent is _first_descent's; this takes the steps after it.
        if self.brain.realism.ifr_descent \
                and FIRST_DESCENT_KEY not in session.advisories:
            return None
        # Being vectored, the step-downs go with the vectors (_vector_step).
        if session.vector_leg:
            return None
        # "Descend via": the STAR's own altitudes are the clearance while the
        # flight is on it; a level said over the top cancels them.
        if session.descend_via and self.brain.realism.star_final \
                and self._still_on_star(destination, state):
            return None

        floor = self.floor(destination, state, session.arrival_runway)
        current = float(state.altitude_ft or 0.0)
        if current <= floor + 1000.0:
            return None

        # Not while a readback is owed to this station. Descents are the one
        # thing here that comes in a sequence, so they are the one thing that
        # can stack on a pilot who has not answered the last one.
        #
        # Owed to *this* station, and not to any station: an instruction from
        # a frequency that has been left is never chased and never cleared, so
        # a readback the pilot did not give approach would otherwise be a
        # reason for the tower to stay silent for the rest of the flight.
        pending = session.pending
        if pending is not None and pending.station_key == station_key(station):
            return None

        # Not until the aeroplane is near the level it was last given. A
        # controller does not issue a second descent to somebody who has not
        # started the first, and this program certainly should not.
        assigned = float(session.assigned_altitude_ft or 0.0)
        if assigned and current > assigned + NEAR_THE_LEVEL_FT:
            return None

        # Stepped down from the level the aeroplane was last given rather
        # than from where it happens to be. An aircraft passing seventeen
        # thousand for fifteen is cleared to eleven, not to thirteen: taking
        # the steps off the altimeter reading makes every one after the first
        # a short one, and turns a three-step descent into six.
        weather = self._weather(weather_for, destination)
        target = self.brain.out_of_the_layer(
            destination, weather, self._step_down(assigned or current, floor))
        target = max(target, floor)
        if target >= current - 500.0:
            return None
        # The profile has to have come down to the level the aeroplane is at
        # before the next step is issued, which is what makes the first one a
        # top of descent rather than a descent the moment the destination
        # comes into range -- and what makes every one after it in time.
        if self._profile_ft(to_run, floor) > (assigned or current) + 1000.0:
            return None
        if not session.advise_once(f"arrival:descend:{int(target)}"):
            return None

        # Taken as read rather than waited for. The next step is gated on the
        # aeroplane being near this level, and a level nobody recorded is one
        # the gate cannot see -- which would issue the whole descent at once
        # to a pilot who had not read any of it back.
        cleared_from = max(current, assigned)
        decision("descent", airport=destination.ident, target_ft=target,
                 floor_ft=floor, from_ft=round(cleared_from), to_run_nm=round(to_run, 1),
                 floor_source=("sector (MSA grid)" if to_run > self.brain.MSA_RADIUS_NM
                               else "intercept, " + self.brain.altitude_source(
                                   destination, session.arrival_runway)))
        session.assigned_altitude_ft = target
        session.phase = Phase.DESCENT
        text = p.descend_maintain(
            session.aircraft, target,
            station=self._name(session, station, p),
            abbreviated=bool(session.established_with(station)),
            **self.brain.descent_terms(destination, weather, p,
                                       cleared_from, target),
        )
        instruction = session.remember(
            Instruction("altitude", station_key(station), text,
                        {"altitude_ft": int(target), "climbing": False}))
        return Reply(text, station, instruction=instruction)

    # ------------------------------------------------------------------
    # radar vectors onto the final (the IFR plan, Phase 5 (b))
    # ------------------------------------------------------------------

    def _vectoring(self, session, station, destination) -> bool:
        """Whether this position vectors arrivals: the switch, an IFR
        flight, and approach -- or the centre where there is none."""
        if not (self.brain.realism.vectors and session.ifr):
            return False
        if station.position in ("APP", "DEP"):
            return True
        return station.position == "CTR" and not any(
            self.brain.controller_at(destination.ident, position)
            for position in ("APP", "DEP"))

    def _radar_vectors(self, session, station, state, destination, to_run,
                       weather_for, p) -> Reply | None:
        """The next vector, a level to go with the miles, the question
        about the field for a visual, and the intercept with the clearance.

        Only once the STAR the flight is on has ended -- a STAR that runs
        onto the final is cleared along it -- and inside VECTOR_FROM_NM.
        """
        if not self._vectoring(session, station, destination):
            return None
        if CLEARED_APPROACH_KEY in session.advisories or to_run > VECTOR_FROM_NM:
            return None
        pending = session.pending
        if pending is not None and pending.station_key == station_key(station):
            return None
        runway = self._runway(session, destination, weather_for)
        weather = self._weather(weather_for, destination)
        choice = self.brain.approach_choice(session, destination, runway, weather)
        final = final_for(self.navdb, destination.ident,
                          choice.approach.runway or runway)
        if final is None:
            return None
        if self._on_a_star_to_the_final(destination, final, state) is not None:
            return None
        if self._still_on_star(destination, state) and not session.vector_leg:
            return None

        now = time.monotonic()
        heading = float(getattr(state, "heading_true", 0.0) or 0.0)
        speed = float(getattr(state, "ground_speed_kt", 0.0) or 0.0)
        altitude = float(state.altitude_ft or 0.0)
        elevation = float(getattr(destination, "elev_ft", 0.0) or 0.0)
        visual = choice.kind == VISUAL
        leg = vec.vector(final, state.latitude, state.longitude,
                         speed_kt=speed, heading_true=heading,
                         after=session.vector_leg,
                         side=session.vector_side or None,
                         visual=visual and session.field_in_sight)
        first = not session.vector_leg
        slow = 0 < speed < vec.SLOW_KT
        if first and vec.too_high(leg, altitude, elevation, slow):
            leg = vec.extended(final, state.latitude, state.longitude)
            decision("vectors", airport=destination.ident, event="too high",
                     altitude_ft=round(altitude), track_nm=round(leg.track_nm, 1))

        # A visual is cleared only with the field in sight: asked once, in
        # time, and the instrument approach carried by the intercept if the
        # answer never comes.
        if visual and not session.field_in_sight and leg.track_nm \
                <= FIELD_QUESTION_NM and session.advise_once("arrival:field?"):
            oclock, distance = self.brain.bearing_to(state, destination)
            text = p.report_field_in_sight(session.aircraft, destination.spoken,
                                           oclock, distance)
            decision("vectors", airport=destination.ident,
                     event="field in sight?", track_nm=round(leg.track_nm, 1))
            instruction = session.remember(Instruction(
                "report_field", station_key(station), text, {},
                readback_required=False))
            return Reply(text, station, instruction=instruction)

        if leg.leg in ("intercept", "final"):
            return self._intercept(session, station, state, destination,
                                   runway, choice, leg, weather, p)
        if not first and now - session.vector_at < VECTOR_GAP_S:
            return None
        said = float(session.assigned_heading) if session.assigned_heading \
            is not None else None
        wanted = self._magnetic(leg.heading_true, state)
        if not first and leg.leg == session.vector_leg and said is not None \
                and abs((wanted - said + 540.0) % 360.0 - 180.0) < VECTOR_CHANGE_DEG:
            return self._vector_step(session, station, state, destination,
                                     leg, choice, weather, p, now)
        session.vector_leg, session.vector_side, session.vector_at = \
            leg.leg, leg.side, now
        reason = (p.vectors_for(self.brain.approach_said(p, self._flown(
            session, destination, runway, choice, weather)), runway)
            if first else "")
        out, across = frame(final, state.latitude, state.longitude)
        decision("vectors", airport=destination.ident, leg=leg.leg,
                 heading=round(wanted), track_nm=round(leg.track_nm, 1),
                 side="right" if leg.side > 0 else "left",
                 out_nm=round(out, 1), across_nm=round(across, 1),
                 join_nm=round(leg.join_nm, 1) if leg.join_nm else None,
                 pattern="slow" if 0 < speed < vec.SLOW_KT else "jet")
        if vec.present_heading(heading, leg.heading_true):
            text = p.fly_present_heading(session.aircraft, reason)
            values = {"heading": int(round(self._magnetic(heading, state))) or 360}
        else:
            values = {"heading": int(round(wanted)) or 360}
            text = p.turn_heading(session.aircraft, values["heading"],
                                  self._turn_way(heading, leg.heading_true),
                                  reason=reason)
        instruction = session.remember(Instruction(
            "heading", station_key(station), text, values))
        return Reply(text, station, instruction=instruction)

    def _vector_step(self, session, station, state, destination, leg, choice,
                     weather, p, now) -> Reply | None:
        """A level for the miles still to fly: 250 ft a mile above the field,
        never below the intercept altitude or the terrain along the
        heading."""
        current = float(state.altitude_ft or 0.0)
        assigned = float(session.assigned_altitude_ft or current)
        if current > assigned + NEAR_THE_LEVEL_FT:
            return None
        elevation = float(getattr(destination, "elev_ft", 0.0) or 0.0)
        intercept = self.brain.intercept_altitude(
            destination, choice.approach.runway or choice.runway,
            choice.approach.name)
        mva = vec.mva_ft(state.latitude, state.longitude, leg.heading_true,
                         elevation)
        target = float(max(vec.step_altitude(leg.track_nm, elevation, intercept),
                           math.ceil(mva / 100.0) * 100.0))
        if target >= assigned - 500.0:
            return None
        session.vector_at = now
        session.assigned_altitude_ft = target
        decision("vectors", airport=destination.ident, event="step down",
                 to_ft=int(target), track_nm=round(leg.track_nm, 1),
                 intercept_ft=int(intercept), terrain_ft=int(mva))
        text = p.descend_maintain(
            session.aircraft, target,
            **self.brain.descent_terms(destination, weather, p, assigned, target))
        instruction = session.remember(Instruction(
            "altitude", station_key(station), text,
            {"altitude_ft": int(target), "climbing": False}))
        return Reply(text, station, instruction=instruction)

    def _intercept(self, session, station, state, destination, runway, choice,
                   leg, weather, p) -> Reply:
        """The last vector, with the clearance: the visual only with the
        field in sight, else the instrument approach."""
        visual = leg.leg == "final" and choice.kind == VISUAL \
            and session.field_in_sight
        flown = self._flown(session, destination, runway, choice, weather)
        approach_runway = flown.approach.runway or runway
        intercept = self.brain.intercept_altitude(destination, approach_runway,
                                                  flown.approach.name)
        current = float(state.altitude_ft or 0.0)
        hold = intercept if current >= intercept - 300.0 \
            else float(int(current / 100.0) * 100)
        session.assigned_altitude_ft = hold
        session.advise_once(CLEARED_APPROACH_KEY)
        session.advise_once("arrival:expect_approach")
        session.phase = Phase.APPROACH
        session.vector_leg = leg.leg
        heading = int(round(self._magnetic(leg.heading_true, state))) or 360
        kind_name = flown.approach.name.split()[0]
        text = p.intercept_clearance(
            session.aircraft, heading,
            self._turn_way(float(getattr(state, "heading_true", 0.0) or 0.0),
                           leg.heading_true),
            self.brain.approach_said(p, flown), approach_runway,
            maintain_ft=hold, visual=visual,
            localizer=kind_name in ("ILS", "LOC", "LDA", "SDF"),
            transition_altitude=self.brain.transition_altitude(destination))
        decision("approach_clearance", airport=destination.ident,
                 runway=approach_runway, clearance=(
                     "visual_approach" if visual else "approach_clearance"),
                 approach="VISUAL" if visual else flown.approach.name,
                 via=f"vectors, {leg.leg}", heading=heading,
                 field_in_sight=session.field_in_sight or None,
                 altitude_ft=int(hold))
        values = {"heading": heading, "runway": runway}
        required = {"heading", "runway"}
        if not visual and getattr(p, "dialect", "") == "faa":
            # Said only in the FAA's form ("maintain ... until established").
            values["altitude_ft"] = int(hold)
            required.add("altitude_ft")
        instruction = session.remember(Instruction(
            "visual_approach" if visual else "approach_clearance",
            station_key(station), text, values, required_items=required))
        return Reply(text, station, instruction=instruction)

    def _traffic_ahead(self, state, destination, to_run):
        """The nearest aeroplane landing ahead, or None."""
        elevation = float(getattr(destination, "elev_ft", 0.0) or 0.0)
        best = None
        for contact in self.brain.contacts():
            if contact.on_ground:
                continue
            theirs = haversine_nm(contact.latitude, contact.longitude,
                                  destination.lat, destination.lon)
            if theirs >= to_run or theirs > SPEED_TRAFFIC_NM:
                continue
            if contact.altitude_ft - elevation > SPEED_TRAFFIC_AGL_FT:
                continue
            if best is None or theirs > best[0]:
                best = (theirs, contact)
        return best[1] if best else None

    def _speed_control(self, session, station, state, destination, to_run,
                       p) -> Reply | None:
        """210 kt on the pattern, 160 to four miles on the final, while
        there is somebody to follow; lifted once there is not."""
        if not (self.brain.realism.speed_control and session.vector_leg):
            return None
        if float(state.altitude_ft or 0.0) >= SPEED_BELOW_FT:
            return None
        pending = session.pending
        if pending is not None and pending.station_key == station_key(station):
            return None
        if time.monotonic() - session.vector_at < VECTOR_GAP_S:
            return None
        ahead = self._traffic_ahead(state, destination, to_run)
        cleared = CLEARED_APPROACH_KEY in session.advisories
        if ahead is None:
            if not session.speed_given \
                    and session.advise_once("arrival:speed:none"):
                # Once, so logs arrival says why no speed was ever given.
                decision("speed", airport=destination.ident, event="none",
                         why="nobody ahead on the approach")
            if session.speed_given and not session.speed_lifted:
                session.speed_lifted = True
                decision("speed", airport=destination.ident, event="lifted")
                text = p.no_speed_restrictions(session.aircraft)
                return Reply(text, station, instruction=session.remember(
                    Instruction("speed", station_key(station), text, {},
                                readback_required=False)))
            return None
        if cleared and to_run > FINAL_SPEED_UNTIL_NM + 2 \
                and session.speed_given != "final":
            session.speed_given, session.speed_lifted = "final", False
            decision("speed", airport=destination.ident, event="final",
                     knots=FINAL_SPEED_KT, until_nm=FINAL_SPEED_UNTIL_NM,
                     behind=ahead.written)
            text = p.speed_until(session.aircraft, FINAL_SPEED_KT,
                                 FINAL_SPEED_UNTIL_NM)
            values = {"speed_kt": FINAL_SPEED_KT}
        elif not cleared and not session.speed_given:
            session.speed_given, session.speed_lifted = "pattern", False
            decision("speed", airport=destination.ident, event="pattern",
                     knots=PATTERN_SPEED_KT, behind=ahead.written)
            text = p.speed_restriction(session.aircraft, PATTERN_SPEED_KT)
            values = {"speed_kt": PATTERN_SPEED_KT}
        else:
            return None
        session.vector_at = time.monotonic()
        return Reply(text, station, instruction=session.remember(
            Instruction("speed", station_key(station), text, values)))

    def _flown(self, session, destination, runway, choice, weather):
        """The approach the intercept carries: the choice, or -- a visual
        with the field never reported -- the instrument approach."""
        if choice.kind != VISUAL or session.field_in_sight:
            return choice
        known = self.brain.approach_data.get(destination.ident)
        from .approaches import plan
        return plan(known, runway, weather, region="icao",
                    airliner=bool(getattr(session.profile, "airliner", True)))

    @staticmethod
    def _magnetic(true_heading: float, state) -> float:
        from ..navdata.magvar import to_magnetic, variation

        return to_magnetic(true_heading, variation(state.latitude,
                                                   state.longitude)) % 360.0

    @staticmethod
    def _turn_way(current: float, wanted: float) -> str:
        delta = (wanted - current + 540.0) % 360.0 - 180.0
        if abs(delta) < 3.0:
            return ""
        return "right" if delta > 0 else "left"

    # ------------------------------------------------------------------
    # holding (the IFR plan, Phase 6)
    # ------------------------------------------------------------------

    def _hold_reason(self, session, destination, state, weather_for) -> tuple:
        """("weather" | "sequence" | "requested" | "", arrivals ahead)."""
        ahead = self._arrivals_queued(state, destination)
        if session.hold_requested:
            return "requested", ahead
        runway = session.arrival_runway or self.brain.arrival_runway(
            destination, self._weather(weather_for, destination))
        weather = self._weather(weather_for, destination)
        if hold_rules.below_every_minima(
                self.brain.approach_data.get(destination.ident), runway,
                weather, bool(getattr(session.profile, "airliner", True))):
            return "weather", ahead
        if ahead >= hold_rules.sequence_hold_ahead(destination.ident):
            return "sequence", ahead
        return "", ahead

    def _arrivals_queued(self, state, destination) -> int:
        """The arrivals nearer the field than this flight, inside 40 nm.

        The ones on the traffic's strips -- invented, FSLTL's and the live
        feed's -- which are the aeroplanes the panel shows: counting every
        contact the simulator reports took in its departures and overflights
        too, and made the pilot number thirteen with six on the frequency.
        Only with no traffic running are the contacts read, those coming
        down."""
        mine = state.distance_to(destination.lat, destination.lon)
        traffic = self.brain.traffic
        if traffic is not None:
            count = 0
            for flight in list(getattr(traffic, "flights", []) or []):
                if (not flight.arriving or flight.on_ground or flight.done
                        or getattr(flight, "stuck", False)
                        or not (flight.latitude or flight.longitude)):
                    continue
                theirs = haversine_nm(flight.latitude, flight.longitude,
                                      destination.lat, destination.lon)
                if theirs < mine and theirs <= 40.0:
                    count += 1
            return count
        count = 0
        for contact in self.brain.contacts():
            if contact.on_ground or contact.vertical_rate_fpm > -300.0:
                continue
            theirs = haversine_nm(contact.latitude, contact.longitude,
                                  destination.lat, destination.lon)
            if theirs < mine and theirs <= 40.0 \
                    and contact.altitude_ft < float(state.altitude_ft or 0.0) + 2000:
                count += 1
        return count

    def _holding(self, session, station, state, destination, to_run,
                 weather_for, p) -> Reply | None:
        """The hold given, watched, updated and ended."""
        if station.position not in ("APP", "DEP", "CTR"):
            return None
        if CLEARED_APPROACH_KEY in session.advisories:
            return None
        pending = session.pending
        if pending is not None and pending.station_key == station_key(station):
            return None
        reason, ahead = self._hold_reason(session, destination, state,
                                          weather_for)
        hold = session.hold
        now = time.time()
        if hold is None:
            if not reason:
                return None
            return self._give_hold(session, station, state, destination,
                                   reason, ahead, p, now)
        if not hold.entered and state.distance_to(hold.lat, hold.lon) \
                <= hold_rules.ENTERED_NM:
            hold.entered = True
            decision("hold", airport=destination.ident, event="entered",
                     fix=hold.fix)
        if not reason:
            return self._leave_hold(session, station, destination, p, hold)
        if hold.entered and not hold.asked_off and hold_rules.off_the_pattern(
                hold, state.latitude, state.longitude,
                float(getattr(state, "ground_speed_kt", 0.0) or 0.0)):
            hold.asked_off = True
            decision("hold", airport=destination.ident, event="off the pattern",
                     fix=hold.fix)
            text = p.verify_holding(session.aircraft, hold.fix)
            return Reply(text, station, instruction=session.remember(
                Instruction("verify_holding", station_key(station), text, {},
                            readback_required=False)))
        # The sequence said again when it moves, no more than every two and
        # a half minutes, with a new time only when it has moved more than
        # two; or a new time when the last one has come and the weather has
        # not.
        number = ahead + 1
        expired = now >= self._efc_epoch(hold)
        later = hold_rules.efc(now, self._delay(reason, ahead))
        # Only the queue moves a time: under the weather it is renewed when
        # it comes, twenty minutes on, and never in between.
        moved = reason == "sequence" and hold_rules.minutes_apart(
            later, hold.efc_hhmm) > hold_rules.EFC_MOVED_MIN
        due = now - hold.said_at >= hold_rules.SEQUENCE_REPEAT_S
        if expired or (due and ((reason == "sequence"
                                 and number != hold.number) or moved)):
            hold.number = number
            hold.said_at = now
            if expired or moved:
                hold.efc_hhmm = later
            decision("hold", airport=destination.ident, event="sequence",
                     number=number, efc=hold.efc_hhmm, reason=reason)
            text = p.hold_sequence(session.aircraft, number, hold.efc_hhmm)
            return Reply(text, station, instruction=session.remember(
                Instruction("hold_sequence", station_key(station), text, {},
                            readback_required=False)))
        return None

    @staticmethod
    def _delay(reason, ahead) -> float:
        if reason == "weather":
            return hold_rules.WEATHER_EFC_MIN
        if reason == "requested":
            return 10.0
        return hold_rules.sequence_delay_min(ahead)

    @staticmethod
    def _efc_epoch(hold) -> float:
        """The EFC as a moment: the next time of day it names."""
        stamp = time.gmtime(hold.issued_at)
        hh, mm = int(hold.efc_hhmm[:2]), int(hold.efc_hhmm[2:])
        base = hold.issued_at - (stamp.tm_hour * 3600 + stamp.tm_min * 60
                                 + stamp.tm_sec)
        at = base + hh * 3600 + mm * 60
        return at if at >= hold.issued_at else at + 86400

    def _give_hold(self, session, station, state, destination, reason, ahead,
                   p, now) -> Reply | None:
        route = self._route(destination)
        fix = hold_rules.hold_fix(self._star_path(destination),
                                  route.fixes if route else (),
                                  destination.lat, destination.lon)
        if fix is None:
            if session.advise_once("hold:nowhere"):
                decision("hold", airport=destination.ident, event="no fix",
                         reason=reason, why="no route or STAR fix 15-60 nm out")
            return None
        if reason != "requested" and state.distance_to(fix.lat, fix.lon) \
                > hold_rules.ISSUE_BEFORE_NM:
            return None
        altitude = float(session.assigned_altitude_ft
                         or round(float(state.altitude_ft or 0.0), -3))
        inbound = hold_rules.inbound_course(fix.lat, fix.lon, destination.lat,
                                            destination.lon)
        legs = hold_rules.leg_minutes(altitude)
        hold = hold_rules.Hold(
            fix=fix.ident, lat=fix.lat, lon=fix.lon, inbound_true=inbound,
            side=hold_rules.side_of_fix(inbound), leg_minutes=legs,
            altitude_ft=altitude, reason=reason,
            efc_hhmm=hold_rules.efc(now, self._delay(reason, ahead)),
            number=ahead + 1, issued_at=now)
        session.hold = hold
        session.vector_leg = ""
        session.assigned_heading = None
        decision("hold", airport=destination.ident, event="given",
                 reason=reason, fix=fix.ident, altitude_ft=int(altitude),
                 legs_min=legs, efc=hold.efc_hhmm, ahead=ahead)
        magnetic = int(round(self._magnetic(inbound, state))) or 360
        weather = self._weather(None, destination)
        text = p.hold_clearance(
            session.aircraft, fix.ident, hold.side, magnetic, altitude, legs,
            hold.efc_hhmm, station=self._name(session, station, p),
            abbreviated=bool(session.established_with(station)),
            **{k: v for k, v in self.brain.descent_terms(
                destination, weather, p, altitude, altitude).items()
               if k != "pressure"})
        return Reply(text, station, instruction=session.remember(Instruction(
            "hold", station_key(station), text,
            {"fix": fix.ident, "altitude_ft": int(altitude),
             "inbound": magnetic, "leg_minutes": legs},
            required_items={"altitude_ft"})))

    def _leave_hold(self, session, station, destination, p, hold) -> Reply:
        """The reason has gone: on to the next fix of the route after the
        hold's, or towards the field, where the vectors take it on."""
        route = self._route(destination)
        nxt, spoken = None, ""
        if route:
            fixes = [f for f in route.fixes if not f.is_marker]
            idents = [f.ident for f in fixes]
            if hold.fix in idents:
                after = fixes[idents.index(hold.fix) + 1:]
                nxt = next((f for f in after if f.ident != destination.ident),
                           None)
        ident = nxt.ident if nxt is not None else destination.ident
        if nxt is None:
            spoken = destination.spoken
        session.hold = None
        session.hold_requested = False
        decision("hold", airport=destination.ident, event="left",
                 fix=hold.fix, to=ident)
        text = p.leave_the_hold(session.aircraft, ident, fix_spoken=spoken)
        return Reply(text, station, instruction=session.remember(Instruction(
            "direct", station_key(station), text, {"fix": ident},
            readback_required=False)))

    def _route(self, destination):
        """The filed route, while it still goes where the flight is going:
        after a return or a diversion it does not (Phase 6)."""
        route = self.brain.route
        if route and destination is not None \
                and route.destination == destination.ident:
            return route
        return None

    def _still_on_star(self, destination, state) -> bool:
        fixes = self._star_path(destination)
        if len(fixes) < 2:
            return False
        # The route's progress first: a STAR that comes back on itself --
        # O'Hare's WYNDE3 runs out past the field to BAMBB and the plan
        # turns back -- puts the leg after its end alongside its own legs.
        route = self._route(destination)
        progress = self.brain.route_progress if route else None
        if route and progress is not None and fixes[-1] in route.fixes:
            return progress.leg <= route.fixes.index(fixes[-1])
        found = along_to(fixes, state.latitude, state.longitude, fixes[-1])
        return found is not None and found[0] <= ON_STAR_NM and found[1] > 1.0

    def _star_path(self, destination):
        """The filed STAR's fixes, without the airport the plan ends on."""
        route = self._route(destination)
        if not route or not route.star:
            return []
        return [f for f in route.procedure(route.star, DSC)
                if f.ident != destination.ident and not f.is_marker]

    def _on_a_star_to_the_final(self, destination, final, state):
        """(join fix, miles along the STAR still to it) for a flight on a
        STAR that runs onto ``final``, or None: no STAR, one that ends
        elsewhere, off it, or past the join."""
        if not self.brain.realism.star_final:
            return None
        fixes = self._star_path(destination)
        join = star_join(final, fixes)
        route = self._route(destination)
        key = (destination.ident, getattr(final, "runway", ""),
               getattr(route, "star", ""))
        if fixes and key not in self._star_logged:
            # Once per STAR and runway, for logs arrival: the rule's answer.
            self._star_logged.add(key)
            last = fixes[-1]
            out, side = frame(final, last.lat, last.lon) if final else (0.0, 0.0)
            decision("star", airport=destination.ident, star=key[2],
                     runway=key[1], joins=join.ident if join else None,
                     last_fix=last.ident, last_out_nm=round(out, 1),
                     last_side_nm=round(side, 1),
                     why=("runs onto the final" if join else
                          "ends elsewhere: vectored from its end"))
        if join is None:
            return None
        found = along_to(fixes, state.latitude, state.longitude, join)
        if found is None or found[0] > ON_STAR_NM:
            return None
        return join, found[1]

    def _expect_approach(self, session, station, state, destination, to_run,
                         weather_for, p) -> Reply | None:
        """Name the runway early enough for the pilot to set it up."""
        if to_run > EXPECT_APPROACH_NM:
            return None
        if not session.advise_once("arrival:expect_approach"):
            return None

        runway = self._runway(session, destination, weather_for)
        session.phase = Phase.DESCENT

        if not session.ifr:
            # A VFR arrival is not given an instrument approach. It is told
            # where the field is and asked to report it in sight, which is how
            # a radar position hands one to a tower.
            oclock, distance = self.brain.bearing_to(state, destination)
            text = p.report_field_in_sight(session.aircraft,
                                           destination.spoken, oclock, distance)
            instruction = session.remember(
                Instruction("report_field", station_key(station), text, {},
                            readback_required=False))
            return Reply(text, station, instruction=instruction)

        choice = self.brain.approach_choice(
            session, destination, runway,
            self._weather(weather_for, destination))
        text = p.expect_approach(
            session.aircraft, self.brain.approach_said(p, choice),
            choice.approach.runway or runway, destination.spoken,
            circle=self.brain.circle_said(p, choice))
        instruction = session.remember(
            Instruction("expect_approach", station_key(station), text,
                        {"runway": runway}, readback_required=False))
        return Reply(text, station, instruction=instruction)

    def _approach_clearance(self, session, station, state, destination,
                            to_run, weather_for, p) -> Reply | None:
        """The clearance without which the approach may not be flown.

        On the approach side of the threshold and within the cone, never on
        a radius: fifteen miles past the field is not where an approach
        starts. It carries the altitude to maintain until established, and
        an aeroplane still well above the intercept altitude is given the
        last descent with it rather than a clearance it cannot fly.
        """
        if not session.ifr:
            # A visual approach is an instrument procedure (FAA JO 7110.65
            # 7-4-1, ICAO Doc 4444 6.5.3) and a VFR flight is never cleared
            # for one. It has been asked to report the field in sight; the
            # engine passes it to the tower, and the tower gives it a join.
            return None
        runway = self._runway(session, destination, weather_for)
        choice = self.brain.approach_choice(
            session, destination, runway,
            self._weather(weather_for, destination))
        # A circle is flown off the procedure's own final, not the landing
        # runway's: the aeroplane comes in on 25 to land on 07.
        flown = choice.approach.runway or runway
        final = final_for(self.navdb, destination.ident, flown)
        on_star = self._on_a_star_to_the_final(destination, final, state)
        if on_star is not None:
            # A STAR that runs onto the final: no vectors, the clearance
            # given along it, STAR_CLEAR_NM before the join (the IFR plan,
            # Phase 5). Not yet: nothing else clears it either.
            join, left = on_star
            if left > STAR_CLEAR_NM:
                return None
            session.star_join = join.ident
        elif self._vectoring(session, station, destination):
            # Under vectors the intercept carries the clearance; here only a
            # flight that joined the final by itself (atc/vectors.py
            # joining_final) is cleared -- and handed on, not left to land
            # unannounced.
            # A go-around climbing out on runway heading is "on the final"
            # by the geometry: it is re-vectored first, and cleared on the
            # intercept like any other.
            if session.going_around and self.brain.realism.go_around_vectors:
                return None
            if final is None or not vec.joining_final(
                    final, state.latitude, state.longitude,
                    float(getattr(state, "heading_true", 0.0) or 0.0)):
                return None
            session.joined_final = True
        else:
            if to_run > CLEARED_APPROACH_NM + 5.0:
                return None
            if final is not None:
                if final.distance(state) > CLEARED_APPROACH_NM:
                    return None
                if final.offset(state) > APPROACH_CONE_DEG:
                    return None
            elif to_run > CLEARED_APPROACH_NM:
                return None
        if not session.advise_once(CLEARED_APPROACH_KEY):
            return None
        # And the runway to expect is now behind the flight. A flight that
        # first calls inside eighteen miles would otherwise be cleared for the
        # ILS and then, on the next look, told to expect it.
        session.advise_once("arrival:expect_approach")
        session.phase = Phase.APPROACH

        approach = choice.approach.name
        intercept = self.brain.intercept_altitude(destination, flown, approach)
        current = float(state.altitude_ft or 0.0)
        assigned = float(session.assigned_altitude_ft or 0.0)
        altitude = {}
        if current > intercept + NEAR_INTERCEPT_FT:
            weather = self._weather(weather_for, destination)
            altitude = {"descend_ft": intercept, **self.brain.descent_terms(
                destination, weather, p, max(current, assigned), intercept)}
            session.assigned_altitude_ft = intercept
        else:
            # Never a climb: an aeroplane already below the intercept
            # altitude holds what it has, to the hundred below.
            held = intercept if current >= intercept - 300.0 \
                else float(int(current / 100.0) * 100)
            session.assigned_altitude_ft = held
            altitude = {"maintain_ft": held, "transition_altitude":
                        self.brain.transition_altitude(destination)}
        if approach == VISUAL:
            # Nothing published to this runway, or the pilot asked for
            # it: a visual approach, never an ILS that is not there.
            text = p.cleared_visual_approach(session.aircraft, runway,
                                             destination.spoken)
            kind = "visual_approach"
        else:
            text = p.cleared_approach(session.aircraft,
                                      self.brain.approach_said(p, choice),
                                      flown, circle=self.brain.circle_said(
                                          p, choice), **altitude)
            kind = "approach_clearance"
        # What has to come back: the runway, the altitude to hold or descend
        # to, and the pressure setting when one was given. Only the runway
        # was required, so a clearance carrying an altitude and a QNH could
        # be read back as "four right" and nothing else.
        values = {"runway": runway}
        required = {"runway"}
        if session.ifr and session.assigned_altitude_ft and kind == "approach_clearance":
            values["altitude_ft"] = int(session.assigned_altitude_ft)
            required.add("altitude_ft")
            pressure = altitude.get("pressure") if session.ifr else None
            if pressure:
                values["altimeter"] = (round(float(pressure), 2)
                                       if float(pressure) < 100.0
                                       else float(round(float(pressure))))
                required.add("altimeter")
        instruction = session.remember(
            Instruction(kind, station_key(station), text, values,
                        required_items=required))
        decision("approach_clearance", airport=destination.ident, runway=runway,
                 clearance=kind, approach=(approach if session.ifr
                                           else "VISUAL"),
                 via=(f"STAR, joining at {session.star_join}"
                      if session.star_join else "the final"),
                 circle_to=choice.circle_to or None,
                 altitude_ft=values.get("altitude_ft"),
                 altitude_source=(self.brain.altitude_source(destination, runway)
                                  if session.ifr else ""),
                 aircraft_ft=round(float(state.altitude_ft or 0.0)),
                 threshold_nm=(round(final.distance(state), 1)
                               if final is not None else None),
                 offset_deg=(round(final.offset(state)) if final is not None
                             else None))
        # Cleared for the approach, and then the engine's own handoff sends
        # the flight to the tower a few miles later. That is the order it
        # happens in, and it is the reason this fires further out than the
        # handoff does rather than carrying one itself.
        return Reply(text, station, instruction=instruction)

    # ------------------------------------------------------------------
    # the tower: the circuit
    # ------------------------------------------------------------------

    def circuit(self, session: FlightSession, station: Station | None, state,
                language: str = "en") -> Reply | None:
        """What the tower says about a VFR aeroplane in its circuit unasked.

        Two things, both of which it used to promise and never do: the base
        it said it would call after "extend downwind, I'll call your base",
        called once the traffic ahead is far enough round; and the report it
        asked for, chased once when the aeroplane has visibly flown past the
        point it was asked to report.

        Separate from :meth:`look` because that only looks at an aeroplane
        closing on the field, and a downwind is flown going away from it.
        """
        if station is None or station.position != "TWR":
            return None
        if not self.brain.staffed(station) or not session.established_with(station):
            return None
        if not session.visual or session.emergency:
            return None
        if state is None or not state.has_position or state.on_ground:
            return None
        if not (session.base_owed_behind or session.expected_report
                or session.orbiting or session.holding_at):
            return None
        field = self.navdb.airport(station.ident)
        runway = session.arrival_runway
        circuit = self.brain._circuit_at(session, field, runway)
        if circuit is None:
            return None
        here = circuit.locate(state)
        p = self.brain.for_station(station, language)
        session.aircraft.language = language_of(p)
        if session.holding_at:
            return self._out_of_the_hold(session, station, circuit, here, p,
                                         field, runway)
        if session.orbiting:
            return self._round_the_orbit(session, station, circuit, here,
                                         state, p)
        return (self._call_the_base(session, station, circuit, here, p)
                or self._chase_the_report(session, station, here, p))

    # The track has turned this far and the orbit is flown. Short of 360
    # because the roll-out is still turning when the pilot would call it.
    ORBIT_DONE_DEG = 330.0

    def _round_the_orbit(self, session, station, circuit, here, state,
                         p) -> Reply | None:
        """How far round the orbit the aeroplane is, from its track; once
        round, back into the sequence."""
        from .final import course_over_ground

        track = course_over_ground(state)
        if track is None:
            return None
        if session.orbit_track >= 0.0:
            # Each look adds the turn since the last, whichever way: a look
            # every second or so sees a few degrees at a time.
            step = abs(((track - session.orbit_track) + 540.0) % 360.0 - 180.0)
            session.orbit_turned += min(step, 90.0)
        session.orbit_track = track
        if session.orbit_turned < self.ORBIT_DONE_DEG:
            return None
        return self.brain.orbit_complete(session, station, p, circuit, here,
                                         said_by="geometry")

    def _out_of_the_hold(self, session, station, circuit, here, p, field,
                         runway) -> Reply | None:
        """Called in from the reporting point once the circuit has room."""
        from .phraseology import without_callsign

        busy = len(self.brain.circuit_traffic(circuit))
        if busy >= self.brain.CIRCUIT_FULL:
            return None
        point = session.holding_at
        session.holding_at = ""
        session.expected_report = "downwind"
        session.expected_report_chased = False
        direction = self.brain._pattern_direction(session, None, p, field, runway)
        join = p.enter_pattern(session.aircraft, "", runway,
                               leg=p.leg("downwind"), direction=direction,
                               report=p.join_report())
        text = (f"{session.aircraft.spoken(True)}, "
                f"{p.leave_hold(session.aircraft, p.vrp(point))}, "
                f"{without_callsign(join, session.aircraft)}")
        self.brain.log_circuit(session, station, here, said="leave_hold",
                               point=point, in_circuit=busy)
        instruction = session.remember(
            Instruction("pattern_entry", station_key(station), text,
                        {"runway": runway,
                         "pattern_direction": session.pattern_direction}))
        return Reply(text, station, instruction=instruction)

    def _call_the_base(self, session, station, circuit, here, p) -> Reply | None:
        """The base owed to an aeroplane that was extended behind traffic."""
        behind = session.base_owed_behind
        if not behind:
            return None
        if here.leg in ("base", "final"):
            # It turned without being told. Nothing is owed any more.
            session.base_owed_behind = ""
            return None
        ahead = next(((there, flight) for there, flight
                      in self.brain.circuit_traffic(circuit)
                      if flight.written == behind), None)
        # The distance the user still has to fly if it turns base now.
        base_now = max(here.out_nm, 0.0) + max(0.0, -here.along_nm)
        if ahead is not None:
            there, _flight = ahead
            if there.to_go_nm > base_now - self.brain.CIRCUIT_SPACING_NM:
                return None
        session.base_owed_behind = ""
        session.expected_report = "final"
        session.expected_report_chased = False
        sequence = ""
        if ahead is not None:
            there, flight = ahead
            sequence = p.sequence_number(2, p.traffic_on_leg(
                flight.describe(language_of(p)), p.leg(there.leg)))
        text = p.turn_base(session.aircraft, sequence)
        self.brain.log_circuit(session, station, here, said="turn_base",
                               behind=behind,
                               ahead_to_go_nm=(round(ahead[0].to_go_nm, 2)
                                               if ahead else None))
        instruction = session.remember(
            Instruction("circuit_report", station_key(station), text, {},
                        readback_required=False))
        return Reply(text, station, instruction=instruction)

    # Which legs mean a report has been flown past without being made.
    _PAST = {"downwind": ("base", "final"), "base": ("final",)}

    def _chase_the_report(self, session, station, here, p) -> Reply | None:
        """Once, when the aeroplane is past the point it was asked to report.

        Only downwind and base. A final that is not reported is a final the
        tower can see, and the landing clearance is what it gets.
        """
        wanted = session.expected_report
        if session.expected_report_chased or here.leg not in self._PAST.get(wanted, ()):
            return None
        session.expected_report_chased = True
        text = p.say_position(session.aircraft)
        self.brain.log_circuit(session, station, here, said="say_position",
                               expected=wanted)
        instruction = session.remember(
            Instruction("say_position", station_key(station), text, {},
                        readback_required=False))
        return Reply(text, station, instruction=instruction)

    # ------------------------------------------------------------------
    # the tower: the clearance to land
    # ------------------------------------------------------------------

    def _tower(self, session, station, state, field, to_run,
               weather_for, p) -> Reply | None:
        """Clear the arrival to land, without waiting to be asked.

        An aircraft doing circuits is left alone. It is cleared for the option
        at the end of each one, off its downwind call, and a landing clearance
        issued at it on final is the wrong clearance for what it is doing.
        """
        # Told to continue because the runway was occupied, or cleared behind
        # a departure that has still to go: the tower owes the clearance or
        # the go-around, circuits included.
        if session.landing_withheld or session.landing_anticipated:
            return self._runway_owed(session, station, state, field,
                                     weather_for, p)
        if session.pattern_work:
            return None
        # Crossing the zone, not landing in it: nothing here is for it.
        if session.transition_approved:
            return None
        # A VFR arrival joins the circuit and reports where it is in it, and
        # the tower clears it off that report. Before it has reported a leg it
        # has only been told how to join, and clearing it to land because it
        # happens to be pointing at the runway skips the circuit it was told
        # to fly.
        if session.visual and not session.pattern_leg:
            return None
        # A go-around nobody announced: cleared, or on the final, and now
        # climbing away low in the cone. The tower calls it as it sees it.
        if self._climbed_away(session, state, field):
            return self.brain.go_around(
                session, station, self._weather(weather_for, field), p,
                field, told=True)
        if session.cleared_landing:
            return None
        if session.going_around and not self.brain.re_established(
                session, field, state):
            return None
        # A departure on the climb-out is filtered in :meth:`look`, but one
        # that has levelled off in the circuit is not, and the tower must not
        # clear it to land on the strength of being close and pointing the
        # right way.
        if session.phase.outbound:
            return None
        # On the final, inside the range of its threshold and pointing down
        # it. A radius round the field cleared an aeroplane nine miles the
        # wrong side of it, and one on the downwind.
        runway = session.arrival_runway or self.brain.arrival_runway(
            field, self._weather(weather_for, field))
        final = final_for(self.navdb, field.ident, runway)
        if final is not None:
            if not final.in_cone(state, LANDING_CLEARANCE_NM):
                return None
        elif to_run > LANDING_CLEARANCE_NM:
            return None
        if not self._arriving(state, field):
            return None
        # Guarded by the clearance itself rather than by an advisory that
        # fires once a flight. A landing clearance is spent the moment it is
        # not flown: an aeroplane sent round again has ``cleared_landing``
        # put back to false, and it has to be cleared a second time. A
        # once-per-flight key would leave every go-around uncleared for the
        # rest of the day.
        weather = self._weather(weather_for, field)
        decision("landing_clearance", airport=field.ident, runway=runway,
                 weather=weather.source, wind=f"{round(weather.wind_dir)}/"
                 f"{round(weather.wind_kt)}", threshold_nm=(
                     round(final.distance(state), 1) if final is not None else None))
        return self.brain.clear_to_land(session, station, weather, p, state)

    def _runway_owed(self, session, station, state, field, weather_for,
                     p) -> Reply | None:
        """The landing clearance once the runway is free, or the go-around
        when it is not free in time (``ControllerBrain.GO_AROUND_NM`` from
        the threshold, or two hundred feet above the field)."""
        runway = session.arrival_runway
        occupants = self.brain.runway_occupants(field, runway)
        if session.landing_anticipated:
            occupants = [o for o in occupants
                         if o.written == session.landing_anticipated]
        if occupants:
            final = final_for(self.navdb, field.ident, runway)
            close = (final is not None
                     and final.distance(state) <= self.brain.GO_AROUND_NM)
            if close or self._low(state, field, 200.0):
                return self.brain.order_go_around(
                    session, station, self._weather(weather_for, field), p,
                    field, why=("departure_still_on_runway"
                                if session.landing_anticipated
                                else "runway_not_clear"))
            return None
        if not session.landing_anticipated \
                and self.brain.arrivals_ahead(field, runway, state):
            # The runway is empty but somebody is still ahead on the final.
            return None
        if session.landing_anticipated:
            # It has gone: the clearance stands.
            decision("landing_clearance", airport=field.ident, runway=runway,
                     said="anticipated_departure_gone",
                     departure=session.landing_anticipated)
            session.landing_anticipated = ""
            return None
        session.landing_withheld = False
        weather = self._weather(weather_for, field)
        decision("landing_clearance", airport=field.ident, runway=runway,
                 said="runway_now_clear")
        if session.visual:
            return self.brain._clear_the_circuit(
                session, station, weather, p, runway, "", "",
                caution=self.brain._caution(session, weather, p))
        return self.brain.clear_to_land(session, station, weather, p, state)

    # ------------------------------------------------------------------

    @staticmethod
    def _step_down(current: float, floor: float) -> float:
        """The next level down, in the steps a controller actually uses.

        Large ones in the flight levels and smaller ones underneath, rounded
        to a thousand so what is issued is a level rather than a number.
        """
        step = 10000.0 if current > 24000.0 else 4000.0
        target = int((current - step) / 1000.0) * 1000.0
        return float(max(floor, target))

    @staticmethod
    def _profile_ft(to_run: float, floor: float) -> float:
        """How high a three-to-one descent is at this range from the field,
        reaching the intercept altitude ``floor`` at DESCENT_MARGIN_NM."""
        miles = max(0.0, to_run - DESCENT_MARGIN_NM)
        return floor + (miles / DESCENT_GRADIENT_NM) * 1000.0

    def floor(self, destination, state=None, runway: str = "") -> float:
        """The lowest level a radar position descends an arrival to.

        Far out, the sector altitude off the terrain grid; inside thirty
        miles, the intercept altitude, because from there the approach is
        the terrain protection -- see
        :meth:`~wilcoatc.atc.controller.ControllerBrain.descent_floor`.

        Rounded up to a whole thousand, because it is issued: a floor of
        14,300 ft is cleared as fifteen thousand.
        """
        return float(math.ceil(self.brain.descent_floor(destination, state, runway)
                               / 1000.0) * 1000.0)

    def _climbed_away(self, session, state, field) -> bool:
        """Whether an aeroplane on the final has gone round without saying.

        A climb held for GO_AROUND_SUSTAIN_S *and* GO_AROUND_GAIN_FT above
        the lowest it got on this final. One sample of 300 fpm up was enough
        before, so a bump on a cleared short final was sent round.
        """
        if session.going_around or session.phase not in (Phase.APPROACH,
                                                         Phase.LANDING):
            session.climb_since = 0.0
            return False
        runway = session.arrival_runway
        final = final_for(self.navdb, field.ident, runway) if runway else None
        if final is None or not final.in_cone(state, GO_AROUND_WITHIN_NM) \
                or not self._low(state, field, GO_AROUND_BELOW_AGL_FT):
            session.climb_since = 0.0
            return False
        altitude = float(state.altitude_ft or 0.0)
        if not session.final_low_ft or altitude < session.final_low_ft:
            session.final_low_ft = altitude
        now = time.monotonic()
        if not state.climbing:
            session.climb_since = 0.0
            return False
        if not session.climb_since:
            session.climb_since = now
        seen = (now - session.climb_since >= GO_AROUND_SUSTAIN_S
                and altitude >= session.final_low_ft + GO_AROUND_GAIN_FT)
        if seen:
            decision("go_around_seen", airport=field.ident,
                     climbing_s=round(now - session.climb_since, 1),
                     gained_ft=round(altitude - session.final_low_ft),
                     lowest_ft=round(session.final_low_ft),
                     vs_fpm=round(float(state.vertical_speed_fpm or 0.0)))
        return seen

    @staticmethod
    def _low(state, field, feet: float) -> bool:
        elevation = float(getattr(field, "elev_ft", 0.0) or 0.0)
        above = (getattr(state, "altitude_agl_ft", 0.0)
                 or max(0.0, float(state.altitude_ft or 0.0) - elevation))
        return above <= feet

    @staticmethod
    def _arriving(state, destination) -> bool:
        """Low enough to be arriving rather than passing over the top."""
        elevation = float(getattr(destination, "elev_ft", 0.0) or 0.0)
        above = (getattr(state, "altitude_agl_ft", 0.0)
                 or max(0.0, float(state.altitude_ft or 0.0) - elevation))
        return above <= ARRIVING_BELOW_AGL_FT

    @staticmethod
    def _weather(weather_for, destination) -> Weather:
        """The weather at the field, fetched only where it is about to be used.

        Calm and clear where there is none to be had. Everything downstream
        reads a wind and a pressure off this without asking whether there is
        one, and a controller that says nothing at all because an observation
        did not arrive is a worse answer than a controller that says calm.
        """
        if weather_for is not None:
            try:
                return weather_for(destination.ident)
            except Exception:
                log.debug("no weather for %s", destination.ident, exc_info=True)
        return Weather()

    def _runway(self, session, destination, weather_for) -> str:
        """The runway in use, settled once and then kept.

        Every one of these transmissions names it, and a flight told to expect
        one runway and then cleared for another is a wind shift the pilot
        cannot see the reason for.
        """
        runway = session.arrival_runway or self.brain.arrival_runway(
            destination, self._weather(weather_for, destination))
        session.arrival_runway = runway
        return runway

    def _name(self, session, station: Station, p) -> str:
        """The station's own name, on the first transmission and not after."""
        if session.established_with(station):
            return ""
        return self.brain.station_name(station, p)


__all__ = [
    "ArrivalWatch", "CLEARED_APPROACH_KEY",
    "DESCENT_GRADIENT_NM", "DESCENT_MARGIN_NM",
    "INTERCEPT_ABOVE_FT", "NEAR_THE_LEVEL_FT", "EXPECT_APPROACH_NM",
    "CLEARED_APPROACH_NM", "LANDING_CLEARANCE_NM", "ARRIVING_BELOW_AGL_FT",
    "APPROACH_CONE_DEG", "NEAR_INTERCEPT_FT",
]
