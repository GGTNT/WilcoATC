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

from ..navdata.db import NavDB, Station
from .controller import ControllerBrain, Reply
from ..logs import decision
from .approaches import VISUAL
from .final import final_for
from .phraseology import Weather, language_of
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
        if to_run >= previous.distance_to(field.lat, field.lon):
            return None

        # An aeroplane that is still climbing away is a departure, whatever
        # it happens to be pointing at. That matters most where the departure
        # and the destination are the same field: a circuit turns downwind
        # inside ten miles, closing, below a thousand feet, and every other
        # test here would read it as an arrival on final.
        if session.phase.outbound and state.climbing:
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
        return (
            self._approach_clearance(session, station, state, destination,
                                     to_run, weather_for, p)
            or self._expect_approach(session, station, state, destination,
                                     to_run, weather_for, p)
            or self._descent(session, station, state, destination, to_run,
                             weather_for, p)
        )

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

        approach = self.brain.approach_for(session, destination, runway)
        text = p.expect_approach(session.aircraft, p.approach_name(approach), runway,
                                 destination.spoken)
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
        if to_run > CLEARED_APPROACH_NM + 5.0:
            return None
        runway = self._runway(session, destination, weather_for)
        final = final_for(self.navdb, destination.ident, runway)
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

        if session.ifr:
            approach = self.brain.approach_for(session, destination, runway)
            intercept = self.brain.intercept_altitude(destination, runway, approach)
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
                                          p.approach_name(approach),
                                          runway, **altitude)
                kind = "approach_clearance"
        else:
            text = p.cleared_visual_approach(session.aircraft, runway,
                                             destination.spoken)
            kind = "visual_approach"
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
                 clearance=kind, approach=(self.brain.approach_for(session, destination,
                                                              runway)
                                      if session.ifr else "VISUAL"),
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
    # the tower: the clearance to land
    # ------------------------------------------------------------------

    def _tower(self, session, station, state, field, to_run,
               weather_for, p) -> Reply | None:
        """Clear the arrival to land, without waiting to be asked.

        An aircraft doing circuits is left alone. It is cleared for the option
        at the end of each one, off its downwind call, and a landing clearance
        issued at it on final is the wrong clearance for what it is doing.
        """
        if session.pattern_work:
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
