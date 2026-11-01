"""What a centre says about the route, unasked: the en route of an IFR flight.

Before this a centre answered a check-in and handed the flight on, and the
route between was the pilot's alone: a plan with a step climb flew it only
if the pilot asked, nobody was ever offered a shortcut, an aeroplane twenty
miles off its route was never asked about it, and "request deviation for
weather" was not understood. Four things, each switched under
``immersion.controller`` and each leaving a ``route`` decision row:

- **the plan's step climbs**: a cruise fix of the filed route planned higher
  than the flight is, two minutes ahead, gets "climb and maintain" that
  level, once, unless traffic is in the way (``plan_steps``);
- **shortcuts**: a quiet centre offers "proceed direct" a fix further down
  the route when it saves real miles, once a sector and never more often
  than every quarter of an hour, never into the STAR or past the top of
  descent (``shortcuts``; thresholds after LocalTC's ``routing.py``, read for
  the design only);
- **the off-route watch**: more than three miles off the route for a
  minute, not on a direct, a heading or a deviation: "verify you are
  proceeding direct {next fix}", once until it is back on its route
  (``off_route_watch``);
- **weather deviations**, which the controller answers when asked
  (``ControllerBrain._approve_deviation``, ``deviations``); this module ends
  them when the flight is back on its route.

Reroutes round an active restricted area are not here: ``airspace.csv``
carries the classes A to G and no restricted, prohibited or danger area to
reroute round.
"""

from __future__ import annotations

from ..logs import decision
from ..navdata.build import haversine_nm
from ..navdata.db import Station
from .controller import ControllerBrain, Reply
from .phraseology import language_of
from .route import CRZ, DSC
from .session import FlightSession, Instruction, station_key

# A plan's step climb is offered this long before the fix it is planned at.
# UNVERIFIED: a choice, not a measured or published figure.
PLAN_STEP_LEAD_S = 120.0
# Climbing or descending faster than this is not level in the cruise.
LEVEL_FPM = 300.0

# A shortcut saves at least this, goes no further than this, skips at least
# this many fixes, and comes no oftener than this (after LocalTC: 8 nm,
# 250 nm, 2 fixes, 900 s).
SHORTCUT_SAVES_NM = 8.0
SHORTCUT_MAX_NM = 250.0
SHORTCUT_MIN_FIXES = 2
SHORTCUT_EVERY_S = 900.0

# Off the route by more than this for this long (the IFR plan: 3 nm, a
# minute); back within this, a deviation is over.
OFF_ROUTE_NM = 3.0
OFF_ROUTE_FOR_S = 60.0
BACK_ON_COURSE_NM = 1.0
# A direct is flown until the fix is this close or a leg beyond it is.
DIRECT_REACHED_NM = 2.0
# A deviation rejoins at the first fix of the route at least this far on:
# past the weather, usually (after LocalTC: 40 nm).
DEVIATION_REJOIN_NM = 40.0

# The first descent has been cleared (atc/arrival.py): the cruise is over.
_DESCENDING_KEY = "arrival:descent_cleared"


class RouteWatch:
    """The centre's side of the en route, looked at every tick."""

    def __init__(self, brain: ControllerBrain):
        self.brain = brain

    def look(self, session: FlightSession, station: Station | None, state,
             now: float, language: str = "en") -> Reply | None:
        brain = self.brain
        route, progress = brain.route, brain.route_progress
        if not route or progress is None or station is None:
            return None
        if not session.ifr or session.emergency:
            return None
        if state is None or state.on_ground:
            return None
        self._follow_amendments(session, route, progress, state)
        if station.position != "CTR" or not session.established_with(station):
            return None
        pending = session.pending
        if pending is not None and not pending.read_back:
            return None
        p = brain.for_station(station, language)
        session.aircraft.language = language_of(p)
        return (self._off_route(session, station, state, progress, now, p)
                or self._plan_step(session, station, state, route, progress, p)
                or self._shortcut(session, station, state, route, progress,
                                  now, p))

    # ------------------------------------------------------------------

    def _follow_amendments(self, session, route, progress, state) -> None:
        """A direct ends at its fix, a deviation back on the route."""
        if session.route_direct:
            fix = route.fix(session.route_direct)
            if fix is None or progress.leg > route.fixes.index(fix) \
                    or state.distance_to(fix.lat, fix.lon) < DIRECT_REACHED_NM:
                session.route_direct = ""
        # Approved while still on the route: over only once the flight has
        # been away from it and come back.
        off = abs(progress.cross_track_nm)
        if session.deviating == "approved" and off > OFF_ROUTE_NM:
            session.deviating = "away"
        elif session.deviating == "away" and off < BACK_ON_COURSE_NM:
            session.deviating = ""
            decision("route", event="back on course",
                     leg=progress.leg_name,
                     cross_track_nm=round(progress.cross_track_nm, 1))

    def _off_route(self, session, station, state, progress, now, p):
        if not self.brain.realism.off_route_watch:
            return None
        excused = (session.route_direct or session.deviating
                   or session.assigned_heading is not None)
        if abs(progress.cross_track_nm) <= OFF_ROUTE_NM:
            session.off_route_since = 0.0
            session.off_route_asked = False
            return None
        if excused or session.off_route_asked:
            session.off_route_since = 0.0
            return None
        if not session.off_route_since:
            session.off_route_since = now
            return None
        if now - session.off_route_since < OFF_ROUTE_FOR_S:
            return None
        fix = progress.to_fix
        # Once while off: the next fix changes as the flight wanders, and
        # asking again for each would be one question said three times.
        session.off_route_asked = True
        decision("route", event="off route", leg=progress.leg_name,
                 cross_track_nm=round(progress.cross_track_nm, 1),
                 for_s=round(now - session.off_route_since),
                 asked="verify direct " + fix.ident)
        text = p.verify_direct(session.aircraft, fix.ident)
        instruction = session.remember(Instruction(
            "verify_direct", station_key(station), text, {},
            readback_required=False))
        return Reply(text, station, instruction=instruction)

    def _plan_step(self, session, station, state, route, progress, p):
        if not self.brain.realism.plan_steps:
            return None
        if _DESCENDING_KEY in session.advisories:
            return None
        if abs(float(state.vertical_speed_fpm or 0.0)) > LEVEL_FPM:
            return None
        assigned = float(session.assigned_altitude_ft or 0.0)
        if not assigned or float(state.altitude_ft or 0.0) < assigned - 500.0:
            return None
        etas = dict(progress.eta_s)
        for fix in route.fixes[progress.leg:]:
            if fix.stage == DSC:
                break
            if fix.stage != CRZ or fix.is_marker \
                    or fix.alt_ft <= assigned + 500.0:
                continue
            eta = etas.get(fix.ident)
            if eta is None or eta > PLAN_STEP_LEAD_S:
                return None
            step = int(round(fix.alt_ft / 1000.0) * 1000)
            blocking = self.brain._climb_blocked_by(state, step)
            if blocking is not None:
                return None
            if not session.advise_once(f"route:step:{step}"):
                return None
            session.cruise_altitude_ft = max(
                float(session.cruise_altitude_ft or 0.0), float(step))
            decision("route", event="plan step climb", fix=fix.ident,
                     from_ft=int(assigned), to_ft=step, eta_s=round(eta))
            field = self.brain.navdb.airport(station.ident)
            text = p.climb_maintain(
                session.aircraft, step,
                transition_altitude=self.brain._transition_altitude(field))
            instruction = session.remember(Instruction(
                "altitude", station_key(station), text,
                {"altitude_ft": step}))
            return Reply(text, station, instruction=instruction)
        return None

    def _shortcut(self, session, station, state, route, progress, now, p):
        if not self.brain.realism.shortcuts:
            return None
        if _DESCENDING_KEY in session.advisories or session.deviating \
                or session.route_direct or session.assigned_heading is not None:
            return None
        if session.shortcut_at and now - session.shortcut_at < SHORTCUT_EVERY_S:
            return None
        if abs(float(state.vertical_speed_fpm or 0.0)) > LEVEL_FPM:
            return None
        key = f"route:shortcut:{station_key(station)}"
        if key in session.advisories:
            return None
        fixes = route.fixes
        i = progress.leg
        # Nothing at or past the top of descent, and nothing inside the
        # STAR but its first fix: those are the arrival's to fly.
        tod = route.top_of_descent
        end = fixes.index(tod) if tod is not None and tod in fixes \
            else len(fixes) - 1
        star = {f.ident for f in route.procedure(route.star, DSC)[1:]} \
            if route.star else set()
        best = None
        along = progress.to_next_nm          # along the route to fixes[i]
        for j in range(i + 1, end):
            a, b = fixes[j - 1], fixes[j]
            along += haversine_nm(a.lat, a.lon, b.lat, b.lon)
            if j < i + SHORTCUT_MIN_FIXES:
                continue
            fix = fixes[j]
            if fix.is_marker or fix.ident in star \
                    or fix.ident == route.destination:
                continue
            direct = state.distance_to(fix.lat, fix.lon)
            if direct > SHORTCUT_MAX_NM:
                break
            saves = along - direct
            if saves >= SHORTCUT_SAVES_NM and (best is None or saves > best[0]):
                best = (saves, fix, direct)
        if best is None:
            return None
        saves, fix, direct = best
        session.advise_once(key)
        session.shortcut_at = now
        session.route_direct = fix.ident
        decision("route", event="shortcut", station=station.callsign,
                 fix=fix.ident, saves_nm=round(saves, 1),
                 direct_nm=round(direct, 1), from_leg=progress.leg_name)
        text = p.direct_to(session.aircraft, fix.ident)
        instruction = session.remember(Instruction(
            "direct", station_key(station), text, {"fix": fix.ident},
            readback_required=False))
        return Reply(text, station, instruction=instruction)


def rejoin_fix(route, progress, state):
    """Where a deviation rejoins: the first route fix at least
    DEVIATION_REJOIN_NM on, short of the top of descent; or None."""
    if not route or progress is None:
        return None
    for fix in route.fixes[progress.leg:]:
        if fix.is_marker or fix.ident == route.destination:
            continue
        if state.distance_to(fix.lat, fix.lon) >= DEVIATION_REJOIN_NM:
            return fix
    return None


__all__ = ["RouteWatch", "rejoin_fix", "OFF_ROUTE_NM", "SHORTCUT_SAVES_NM"]
