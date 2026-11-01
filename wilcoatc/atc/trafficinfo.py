# -*- coding: utf-8 -*-
"""Traffic information: who else is out there, from the pilot's seat.

The phrases for it existed and nothing called them. Flight following
assigned a code and never mentioned another aeroplane again, and the
controller's whole picture of the sky was the traffic this program invents:
a pilot flying with FSLTL, or with the simulator's own traffic, or with the
invented traffic switched off, had a controller blind to everything out of
the window.

So there are three things here.

:class:`Contact` is one aeroplane in the controller's picture, whatever put
it there -- an invented :class:`~wilcoatc.atc.traffic.Flight`, or a
:class:`~wilcoatc.integrations.simaircraft.Seen` the simulator reported --
with the few things a controller reads off a radar label.
``ControllerBrain.contacts`` builds the picture out of both.

:func:`relative` is where a contact is from the pilot: the clock position off
the pilot's own track, the distance, the height above or below, which way it
is moving across the windscreen, and whether it is still a factor or has
gone behind and is opening.

:class:`TrafficWatch` is what a radar controller giving a VFR flight
advisories does without being asked: a traffic call for anything converging
inside five miles and 1,500 ft, once, and "no factor" once it has passed --
unless the pilot has said they have it in sight. A centre does the same for
an IFR flight in cruise inside five miles and 1,000 ft; it used to say
nothing to one, because the watch was for advisories only.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ..logs import decision
from ..navdata.build import bearing_deg, haversine_nm
from .session import Instruction, station_key

# A traffic call is made for anything converging inside these (FAA JO
# 7110.65 2-1-21 leaves it to judgement; these are the figures commonly
# used for VFR advisories).
CALL_WITHIN_NM = 5.0
CALL_WITHIN_FT = 1500.0
# An IFR flight in a centre's cruise is told about traffic inside five miles
# and a thousand feet: the RVSM level next to its own, which is the only
# traffic a controller separating it would mention.
IFR_CALL_WITHIN_FT = 1000.0
# Past these, a called contact is no longer a factor whatever its geometry.
NO_FACTOR_NM = 6.0
NO_FACTOR_FT = 2000.0


@dataclass(frozen=True)
class Contact:
    """One aeroplane in the controller's picture."""

    written: str
    type_code: str
    latitude: float
    longitude: float
    altitude_ft: float
    heading: float
    ground_speed_kt: float = 0.0
    vertical_rate_fpm: float = 0.0
    on_ground: bool = False
    observed: bool = False          # the simulator's, not ours
    runway: str = ""

    def describe(self, language: str = "en") -> str:
        """"the Piper", "le Cessna": by make, as it is pointed out."""
        from .traffic import article, make_of

        return article(make_of(self.type_code), language) or {
            "fr": "le trafic", "es": "el tráfico", "de": "der Verkehr",
            "it": "il traffico", "pt": "o tráfego"}.get(language, "the traffic")

    def indefinite(self, language: str = "en") -> str:
        """"a Piper", for a traffic call, which introduces it."""
        from .traffic import make_of

        make = make_of(self.type_code)
        if not make:
            return ""
        if language == "en":
            return f"{'an' if make[0] in 'AEIOU' else 'a'} {make}"
        articles = {"fr": "un", "es": "un", "de": "eine", "it": "un", "pt": "um"}
        return f"{articles.get(language, '')} {make}".strip()


def from_flight(flight) -> Contact:
    return Contact(
        written=flight.written, type_code=flight.type_code,
        latitude=flight.latitude, longitude=flight.longitude,
        altitude_ft=float(flight.altitude_ft or 0.0),
        heading=float(getattr(flight, "heading", 0.0) or 0.0),
        ground_speed_kt=float(getattr(flight, "ground_speed_kt", 0.0) or 0.0),
        vertical_rate_fpm=float(getattr(flight, "vertical_rate_fpm", 0.0) or 0.0),
        on_ground=bool(getattr(flight, "on_ground", False)),
        observed=bool(getattr(flight, "observed", False)),
        runway=getattr(flight, "runway", "") or "")


def from_seen(one) -> Contact:
    return Contact(
        written=(one.callsign or one.registration or "").strip().upper(),
        type_code=one.type_code, latitude=one.latitude,
        longitude=one.longitude, altitude_ft=float(one.altitude_ft or 0.0),
        heading=float(one.heading or 0.0),
        ground_speed_kt=float(one.ground_speed_kt or 0.0),
        vertical_rate_fpm=float(one.vertical_speed_fpm or 0.0),
        on_ground=bool(one.on_ground), observed=True)


# ----------------------------------------------------------------------
# where it is
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Relative:
    oclock: int
    distance_nm: float
    relative_ft: int            # its height minus the pilot's, to 100 ft
    motion: str                 # same, opposite, left_to_right, right_to_left
    factor: bool                # converging, or close enough to matter


def _angle(a: float) -> float:
    return ((a + 540.0) % 360.0) - 180.0


def relative(own_lat: float, own_lon: float, own_alt: float, own_track: float,
             lat: float, lon: float, alt: float, heading: float, *,
             own_speed: float = 100.0, speed: float = 100.0) -> Relative:
    """A contact as the pilot sees it."""
    distance = haversine_nm(own_lat, own_lon, lat, lon)
    bearing = bearing_deg(own_lat, own_lon, lat, lon)
    off = (bearing - own_track) % 360.0
    oclock = int(round(off / 30.0)) % 12 or 12
    turn = _angle(heading - own_track)
    if abs(turn) < 30.0:
        motion = "same"
    elif abs(turn) > 150.0:
        motion = "opposite"
    else:
        motion = "left_to_right" if turn > 0 else "right_to_left"
    # Closing or opening: the rate of change of the distance, from the two
    # velocities, in a flat frame centred on the pilot.
    rx = distance * math.sin(math.radians(bearing))
    ry = distance * math.cos(math.radians(bearing))
    vx = speed * math.sin(math.radians(heading)) - own_speed * math.sin(math.radians(own_track))
    vy = speed * math.cos(math.radians(heading)) - own_speed * math.cos(math.radians(own_track))
    opening = rx * vx + ry * vy > 0.0
    height = int(round((alt - own_alt) / 100.0) * 100)
    behind = oclock in (4, 5, 6, 7, 8)
    factor = (distance <= NO_FACTOR_NM and abs(height) <= NO_FACTOR_FT
              and not (opening and (behind or distance > 2.0)))
    return Relative(oclock, distance, height, motion, factor)


# ----------------------------------------------------------------------
# cautions
# ----------------------------------------------------------------------

# Precipitation in a METAR's present weather, not in the vicinity (VC) and
# not a recent (RE) one. UNVERIFIED as a basis for "runway wet": no runway
# condition report is available, so falling rain or snow at the field is
# taken as the runway being wet.
_PRECIPITATION = re.compile(
    r"(?<!\S)[-+]?(?:TS|SH|FZ)?(?:DZ|RA|SN|SG|PL|GR|GS|UP)+(?!\S)")


def runway_wet(weather) -> bool:
    raw = getattr(weather, "raw", "") or ""
    if not raw:
        return False
    body = raw.split(" RMK ")[0]
    return bool(_PRECIPITATION.search(body))


def caution(p, *, wet: bool = False, wake: bool = False) -> str:
    """The cautions for a clearance, in the phrasebook's words."""
    parts = []
    if wake:
        parts.append(p.caution_wake())
    if wet:
        parts.append(p.runway_wet())
    return ", ".join(parts)


def needs_wake_caution(ahead_type: str, own_type: str,
                       light: bool | None = None) -> bool:
    """Behind an airliner in anything that is not one. The categories are
    not modelled finer than that: a light aircraft behind a jet is the case
    that matters (ICAO Doc 4444 chapter 5 wake turbulence categories; FAA
    JO 7110.65 2-1-20 for the caution).

    ``light`` is the pilot's profile saying whether their own aeroplane is
    not an airliner; None asks the type, which did not know a C25C."""
    from . import speech as sp

    if light is None:
        light = not sp.is_airliner(own_type or "")
    return bool(ahead_type) and sp.is_airliner(ahead_type) and light


# ----------------------------------------------------------------------
# the watch
# ----------------------------------------------------------------------

class TrafficWatch:
    """Traffic calls for a VFR flight receiving radar advisories, and for
    an IFR flight with a radar centre."""

    def __init__(self, brain):
        self.brain = brain

    def look(self, session, station, state, language: str = "en"):
        from .controller import Reply
        from .final import course_over_ground

        if station is None or station.position not in ("APP", "DEP", "CTR"):
            return None
        within_ft = CALL_WITHIN_FT
        if session.ifr:
            # A centre with radar, and only a centre: departure and approach
            # sequence their IFR traffic, and an HF station over the ocean
            # cannot see any.
            from .enroute import is_oceanic

            if station.position != "CTR" or is_oceanic(station):
                return None
            within_ft = IFR_CALL_WITHIN_FT
        elif not session.flight_following:
            return None
        if not session.established_with(station) or not self.brain.staffed(station):
            return None
        if state is None or not state.has_position or state.on_ground:
            return None
        track = course_over_ground(state)
        if track is None:
            return None
        p = self.brain.for_station(station, language)
        own = (state.latitude, state.longitude, float(state.altitude_ft or 0.0), track)
        own_speed = float(getattr(state, "ground_speed_kt", 0.0) or 100.0)
        called = session.traffic_called
        best = None
        for contact in self.brain.contacts():
            if contact.on_ground or not contact.written:
                continue
            where = relative(*own, contact.latitude, contact.longitude,
                             contact.altitude_ft, contact.heading,
                             own_speed=own_speed,
                             speed=contact.ground_speed_kt or 100.0)
            said = called.get(contact.written, "")
            if said in ("called", "looking", "negative") and not where.factor:
                called[contact.written] = "done"
                decision("traffic", contact=contact.written, said="no_factor")
                return Reply(p.traffic_no_factor(session.aircraft), station)
            if said or not where.factor:
                continue
            if where.distance_nm > CALL_WITHIN_NM or abs(where.relative_ft) > within_ft:
                continue
            if best is None or where.distance_nm < best[0].distance_nm:
                best = (where, contact)
        if best is None:
            return None
        where, contact = best
        called[contact.written] = "called"
        session.aircraft.language = getattr(p, "language", "en")
        text = p.traffic_call(
            session.aircraft, oclock=where.oclock, distance_nm=where.distance_nm,
            motion=where.motion, heading=contact.heading,
            traffic=contact.indefinite(getattr(p, "language", "en")),
            relative_ft=where.relative_ft)
        decision("traffic", contact=contact.written, said="call",
                 oclock=where.oclock, miles=round(where.distance_nm, 1),
                 relative_ft=where.relative_ft, motion=where.motion,
                 observed=contact.observed)
        instruction = session.remember(Instruction(
            "traffic", station_key(station), text, {}, readback_required=False))
        return Reply(text, station, instruction=instruction)


__all__ = ["Contact", "Relative", "TrafficWatch", "caution", "from_flight",
           "from_seen", "needs_wake_caution", "relative", "runway_wet"]
