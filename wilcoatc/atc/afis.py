# -*- coding: utf-8 -*-
"""The Information frequencies: AFIS at a field, FIS en route, flight service.

The controller's routing had a branch for every position but INFO, and an
INFO frequency was not a controller's (``Station.is_controller``), so about
four thousand of them answered nothing. In Europe that is most of the fields
a VFR pilot flies to -- Kortrijk Information, Egelsbach Radio, Bresso Radio
-- and the panel told the pilot it was an advisory frequency with nobody on
it.

One position in the data covers three different services, because the
build folds OurAirports' AFIS, A/G, FIS, RDO and FSS types into it:

``aerodrome``
    An aerodrome flight information service, or an air/ground radio at a
    field (the UK's and Italy's "Radio"). It gives information and never a
    clearance: the runway in use, the wind, the pressure, the traffic, and
    then "take off / land at your discretion". ICAO Doc 4444 chapter 12 for
    the phraseology, Doc 9426 for the service, CAP 413 chapter 2 for the
    UK's form; the paragraph numbers were not checked.
``fis``
    An en-route flight information service -- Langen Information, Lille
    Information -- which gives the area's pressure, tells a lost aeroplane
    where it is and lets it go with "squawk 7000, frequency change
    approved". The UK's is a Basic Service (CAP 774).
``fss``
    A flight service station: the FAA's "Denver Radio", Canada's "London
    Radio". It opens and closes VFR flight plans and reads the weather; it
    gives no radar service and no traffic.

Which one a station is comes from what it is called and where it is filed
(:func:`kind_of`), because the type the source gave it was not kept.

Opening hours come from ``service_hours.csv`` beside the database, which
ships empty: no open source has them, and a frequency with no hours known
is open (:class:`Hours`).
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..logs import decision
from ..navdata.build import bearing_deg, haversine_nm
from ..navdata.db import NavDB, Station
from ..navdata.magvar import to_magnetic, variation
from .controller import Reply
from .intents import Intent
from .phraseology import language_of
from .session import Instruction, Phase, station_key

AERODROME = "aerodrome"
FIS = "fis"
FSS = "fss"


# ----------------------------------------------------------------------
# which service
# ----------------------------------------------------------------------

def _folded(text: str) -> set[str]:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return {w for w in re.findall(r"[a-z]{3,}", text)
            if w not in {"radio", "rdo", "info", "information", "airport",
                         "airfield", "aerodrome", "international", "afis"}}


def _own_field(navdb: NavDB, station: Station) -> bool:
    """Whether the station is filed under the name of the field it is at.

    "Kortrijk" at Kortrijk is the field's own service; "Langen" at
    Egelsbach is the sector's.
    """
    airport = navdb.airport(station.ident)
    if airport is None:
        return False
    names = _folded(f"{airport.name} {airport.spoken}")
    for other in navdb.stations(station.ident):
        if other.position in ("TWR", "GND", "ATIS", "CTAF", "AWOS"):
            names |= _folded(other.facility)
    return bool(_folded(station.facility) & names)


# Asked several times a look (staffed() is), and the answer is a fact about
# the data, so it is worked out once per station.
_KINDS: dict[tuple, str] = {}


def kind_of(navdb: NavDB, station: Station | None) -> str:
    """``aerodrome``, ``fis`` or ``fss`` for an INFO station; "" otherwise.

    Filed under another place's name it is a sector filed against the field
    (Langen Information at Frankfurt), or, called a Radio, a flight service.
    Filed under the field's own name, an Information is:

    * at a field with no tower, its AFIS;
    * at a field with a tower but no approach, its aerodrome information
      service, staffed while the tower is closed (Sion; :func:`is_open`);
    * at a field with a tower and an approach, the approach's regional
      information service -- the French SIV, Lille Information for the
      whole Lille region -- always.

    An own-named Radio is the field's air/ground service whatever else the
    field has (Shoreham).
    """
    if station is None or station.position != "INFO":
        return ""
    key = (station.ident, station.mhz, station.facility, station.description)
    if key not in _KINDS:
        if station.dialect == "faa":
            kind = FSS
        elif not _own_field(navdb, station):
            kind = FSS if station.is_radio else FIS
        elif station.is_radio:
            kind = AERODROME
        else:
            others = navdb.stations(station.ident)
            towered = any(s.position == "TWR" for s in others)
            approach = any(s.position == "APP" and _civil_approach(s)
                           for s in others)
            kind = FIS if towered and approach else AERODROME
        _KINDS[key] = kind
    return _KINDS[key]


def _civil_approach(station: Station) -> bool:
    """An approach unit that runs a regional information service.

    UNVERIFIED heuristic: a unit filed as nothing but "RADAR" is taken to be
    a military radar (Sion Radar), which runs no SIV; "LILLE APP" and
    "APP/RADAR" (Marseille) are approaches. The data has no field that says
    which approach units provide flight information.
    """
    return (station.description or "").strip().upper() != "RADAR"


def _tower_at(navdb: NavDB, ident: str) -> Station | None:
    return next((s for s in navdb.stations(ident) if s.position == "TWR"), None)


# ----------------------------------------------------------------------
# hours
# ----------------------------------------------------------------------

def _sun_elevation(when: datetime, lat: float, lon: float) -> float:
    """The sun's elevation in degrees, to a fraction of a degree (the NOAA
    approximation), which is all "HJ" needs."""
    when = when.astimezone(timezone.utc)
    day = when.timetuple().tm_yday
    hours = when.hour + when.minute / 60.0 + when.second / 3600.0
    gamma = 2.0 * math.pi / 365.0 * (day - 1 + (hours - 12.0) / 24.0)
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma)
                       - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma)
                       - 0.040849 * math.sin(2 * gamma))
    solar_minutes = hours * 60.0 + eqtime + 4.0 * lon
    hour_angle = math.radians(solar_minutes / 4.0 - 180.0)
    phi = math.radians(lat)
    cos_zenith = (math.sin(phi) * math.sin(decl)
                  + math.cos(phi) * math.cos(decl) * math.cos(hour_angle))
    return 90.0 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zenith))))


# The sun's centre this far below the horizon is sunrise and sunset, which is
# what the AIP's HJ means.
_HORIZON_DEG = -0.833


_DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def _days(spec: str) -> frozenset[int]:
    """"MON-FRI", "SAT", "SAT-SUN", "FRI-MON" as weekday numbers."""
    found: set[int] = set()
    for part in spec.split(","):
        ends = [d.strip() for d in part.split("-")]
        if not all(d in _DAYS for d in ends):
            continue
        first, last = _DAYS.index(ends[0]), _DAYS.index(ends[-1])
        day = first
        while True:
            found.add(day)
            if day == last:
                break
            day = (day + 1) % 7
    return frozenset(found)


def _eu_summer(when: datetime) -> bool:
    """European summer time: last Sunday of March to last Sunday of
    October, changing at 0100 UTC (EU Directive 2000/84/EC)."""
    def last_sunday(month: int) -> datetime:
        day = datetime(when.year, month, 31, 1, 0, tzinfo=timezone.utc)
        return day.replace(day=31 - (day.weekday() + 1) % 7)
    return last_sunday(3) <= when < last_sunday(10)


@dataclass(frozen=True)
class Hours:
    """When a service is staffed. Nothing known means always.

    Written as the AIPs write it, in UTC: ``H24``, ``HJ``, ``HN``, or
    sessions such as ``0800-1700`` or ``0715-1100,1215-1600``, each optionally
    behind days (``MON-FRI 0715-1100; SAT 0800-1200``; a day with no
    session is closed) and followed by ``SUM-1`` where the AIP says its
    times are an hour earlier in European summer time (France: "HOR ETE :
    -1HR"). Days are read on the UTC date, which is the local date for every
    European session that does not cross midnight.
    """

    kind: str = ""                              # "", H24, HJ, HN, or "sessions"
    sessions: tuple = ()                        # ((days or None, ((start, end), ...)), ...)
    summer_shift_min: int = 0

    @classmethod
    def parse(cls, text: str) -> "Hours":
        text = (text or "").strip().upper()
        if not text:
            return cls()
        if text in ("H24", "HJ", "HN"):
            return cls(text)
        shift = 0
        summer = re.search(r"\bSUM\s*-\s*(\d)\b", text)
        if summer:
            shift = -60 * int(summer.group(1))
            text = text[:summer.start()] + text[summer.end():]
        sessions = []
        for part in text.split(";"):
            days_spec = re.match(r"\s*([A-Z]{3}(?:\s*[-,]\s*[A-Z]{3})*)\s+", part)
            days = _days(days_spec.group(1).replace(" ", "")) if days_spec else None
            spans = tuple(
                (int(a[:2]) * 60 + int(a[2:]), int(b[:2]) * 60 + int(b[2:]))
                for a, b in re.findall(r"(\d{4})\s*-\s*(\d{4})", part))
            if spans:
                sessions.append((days, spans))
        if not sessions:
            return cls()
        return cls("sessions", tuple(sessions), shift)

    def open_at(self, when: datetime, lat: float, lon: float) -> bool:
        if self.kind in ("", "H24"):
            return True
        if self.kind in ("HJ", "HN"):
            daylight = _sun_elevation(when, lat, lon) > _HORIZON_DEG
            return daylight if self.kind == "HJ" else not daylight
        when = when.astimezone(timezone.utc)
        if self.summer_shift_min and _eu_summer(when):
            # An hour earlier in summer is the same check an hour later.
            from datetime import timedelta

            when = when - timedelta(minutes=self.summer_shift_min)
        minute = when.hour * 60 + when.minute
        today, yesterday = when.weekday(), (when.weekday() - 1) % 7
        for days, spans in self.sessions:
            for start, end in spans:
                if start <= end:
                    if start <= minute < end and (days is None or today in days):
                        return True
                elif minute >= start and (days is None or today in days):
                    return True
                elif minute < end and (days is None or yesterday in days):
                    return True
        return False


def _within_hours(navdb: NavDB, station: Station, when: datetime) -> bool:
    hours = Hours.parse(navdb.service_hours(station.ident, station.position))
    if not hours.kind:
        return True
    airport = navdb.airport(station.ident)
    lat = airport.lat if airport is not None else 0.0
    lon = airport.lon if airport is not None else 0.0
    return hours.open_at(when, lat, lon)


def is_open(navdb: NavDB, station: Station | None,
            when: datetime | None = None) -> bool:
    """Whether a station is staffed now, by ``service_hours.csv``.

    Any position: a tower outside its hours is as empty as an AFIS outside
    its own. And a field's own information service at a towered field is
    staffed only while the tower is not -- with no tower hours known, the
    tower is always open and the AFIS never is.
    """
    if station is None:
        return False
    when = when or datetime.now(timezone.utc)
    if not _within_hours(navdb, station, when):
        return False
    if station.position == "INFO" and kind_of(navdb, station) == AERODROME:
        tower = _tower_at(navdb, station.ident)
        if tower is not None and _within_hours(navdb, tower, when):
            return False
    return True


# ----------------------------------------------------------------------
# where a lost aeroplane is
# ----------------------------------------------------------------------

def nearest_field(navdb: NavDB, lat: float, lon: float):
    """The nearest aerodrome with a runway worth naming to a lost pilot."""
    return (navdb.nearest_airport(lat, lon, max_nm=80.0, min_rank=1)
            or navdb.nearest_airport(lat, lon, max_nm=200.0))


def qdm(airport, lat: float, lon: float) -> int:
    """The magnetic bearing from the aeroplane to the field: the heading to
    steer in still air (ICAO Doc 8400)."""
    true = bearing_deg(lat, lon, airport.lat, airport.lon)
    magnetic = to_magnetic(true, variation(airport.lat, airport.lon))
    return int(round(magnetic)) % 360 or 360


def qdr(airport, lat: float, lon: float) -> int:
    """The magnetic bearing from the field to the aeroplane."""
    return (qdm(airport, lat, lon) + 180) % 360 or 360


_COMPASS = ("north", "northeast", "east", "southeast", "south", "southwest",
            "west", "northwest")


def compass(true_bearing: float) -> str:
    return _COMPASS[int(((true_bearing % 360.0) + 22.5) // 45.0) % 8]


# ----------------------------------------------------------------------
# answering
# ----------------------------------------------------------------------

def answer(brain, parsed, session, station, weather, p, state) -> Reply:
    """Answer one transmission on an information frequency."""
    kind = kind_of(brain.navdb, station)
    aircraft = session.aircraft
    first = not session.established_with(station)
    name = brain.station_name(station, p) if first else ""
    intent = parsed.intent

    if intent is Intent.RADIO_CHECK:
        return Reply(p.radio_check(aircraft), station)
    if intent in (Intent.ACKNOWLEDGE, Intent.AFFIRMATIVE):
        return Reply("", station, silent=True)
    if intent in (Intent.REQUEST_QDM, Intent.REQUEST_QDR,
                  Intent.REQUEST_POSITION):
        found = _where_am_i(brain, session, station, p, state, intent, kind)
        if found is not None:
            return found

    if kind == FSS:
        return _flight_service(brain, parsed, session, station, weather, p,
                               state, name)
    if kind == AERODROME:
        return _aerodrome(brain, parsed, session, station, weather, p, state,
                          name)
    return _enroute(brain, parsed, session, station, weather, p, state, name)


def _note(session, station, text, kind, values=None) -> Reply:
    """Information is not read back: nothing here is owed by the pilot."""
    instruction = session.remember(
        Instruction(kind, station_key(station), text, values or {},
                    readback_required=False))
    return Reply(text, station, instruction=instruction)


def _where_am_i(brain, session, station, p, state, intent, kind) -> Reply | None:
    if state is None or not getattr(state, "has_position", False):
        return None
    if kind == AERODROME:
        here = brain.navdb.airport(station.ident)
    else:
        here = nearest_field(brain.navdb, state.latitude, state.longitude)
    if here is None:
        return None
    lat, lon = state.latitude, state.longitude
    if intent is Intent.REQUEST_POSITION:
        miles = max(1, int(round(haversine_nm(here.lat, here.lon, lat, lon))))
        direction = p.bearing(compass(bearing_deg(here.lat, here.lon, lat, lon)))
        text = p.position_fix(session.aircraft, miles, direction, here.spoken)
        decision("position_fix", field=here.ident, miles=miles,
                 direction=direction)
    else:
        code = "QDM" if intent is Intent.REQUEST_QDM else "QDR"
        value = (qdm if code == "QDM" else qdr)(here, lat, lon)
        text = p.bearing_fix(session.aircraft, code, value)
        decision("bearing_fix", field=here.ident, code=code, bearing=value)
    return _note(session, station, text, "position_fix")


# --- the aerodrome service --------------------------------------------

_ARRIVING = frozenset({Intent.REQUEST_LANDING, Intent.CHECK_IN,
                       Intent.POSITION_REPORT, Intent.REPORT_FIELD_IN_SIGHT,
                       Intent.REQUEST_PATTERN_WORK, Intent.REQUEST_TRANSITION,
                       Intent.REQUEST_FLIGHT_FOLLOWING})
_DEPARTING = frozenset({Intent.REQUEST_TAXI, Intent.REQUEST_VFR_DEPARTURE,
                        Intent.REQUEST_CLEARANCE, Intent.CHECK_IN,
                        Intent.REQUEST_PATTERN_WORK, Intent.REQUEST_PUSHBACK,
                        Intent.POSITION_REPORT})


def _traffic(brain, session, airport, runway, p) -> tuple[str, bool]:
    """The traffic to pass, and whether something is on the runway or about
    to be. Information, not sequencing: an AFISO says what is there."""
    occupied = brain._runway_blocked()
    circuit = brain._circuit_at(session, airport, runway)
    placed = brain.circuit_traffic(circuit)
    if not placed:
        return "", occupied
    there, flight = placed[0]
    occupied = occupied or (there.leg == "final" and there.to_go_nm < 1.0)
    return (p.traffic_on_leg(flight.describe(language_of(p)), p.leg(there.leg)),
            occupied)


def _aerodrome(brain, parsed, session, station, weather, p, state, name) -> Reply:
    aircraft = session.aircraft
    airport = brain.navdb.airport(station.ident)
    intent = parsed.intent
    airborne = state is not None and not getattr(state, "on_ground", True)
    said = parsed.normalized or parsed.text.lower()
    asked = bool(_ASKED_FOR_THE_FIELD.search(said))

    if intent in (Intent.REQUEST_FREQUENCY_CHANGE, Intent.REPORT_LEAVING):
        return _note(session, station, p.frequency_change_approved(aircraft),
                     "frequency_change")

    if airborne:
        runway = (session.arrival_runway or parsed.get("runway")
                  or brain._arrival_runway(airport, weather))
        session.arrival_runway = runway
        leg = (parsed.get("pattern_leg") or "").lower()
        if leg:
            session.pattern_leg = leg
        traffic, occupied = _traffic(brain, session, airport, runway, p)
        if leg == "final" or brain._on_final(state, airport, runway):
            session.phase = Phase.LANDING
            text = (p.afis_runway_occupied(aircraft, traffic) if occupied
                    else p.afis_land(aircraft, weather))
            decision("afis", wind_from=weather.wind_from or None,
                     airport=station.ident, said="land", runway=runway,
                     occupied=occupied)
            return _note(session, station, text, "afis_land")
        if leg in ("downwind", "base", "crosswind", "upwind"):
            # In the circuit. The field was given on the first call, and
            # "downwind, for touch and go" was given all of it again at
            # Kortrijk: now only what is new -- the traffic, or a roger where
            # there is none, and the final report either way, which is how
            # the AFISO keeps the picture.
            if _field_given(session, station) and not asked:
                text = p.afis_traffic_report(aircraft, traffic, p.leg("final"))
                decision("afis", airport=station.ident, said="circuit_report",
                         leg=leg, traffic=traffic or None)
                return _note(session, station, text, "afis_report")
            session.field_given_by.add(station_key(station))
            text = p.afis_field(aircraft, name, runway, weather,
                                traffic=traffic, report=p.leg("final"))
            return _note(session, station, text, "afis_report")
        if intent in _ARRIVING or intent is Intent.REQUEST_TAKEOFF or asked:
            session.phase = Phase.APPROACH
            report = (p.REPORT_LEAVING_ZONE if intent is Intent.REQUEST_TRANSITION
                      else p.leg("downwind"))
            point = (brain.vrp_ahead(airport, state)
                     if intent is not Intent.REQUEST_TRANSITION else None)
            if point is not None:
                # "Report November, 1500 ft": the point, then the circuit.
                report = p.vrp(point.name, point.altitude_ft)
                session.expected_vrp = point.name
            session.field_given_by.add(station_key(station))
            text = p.afis_field(aircraft, name, runway, weather,
                                direction=brain._pattern_direction(
                                    session, parsed, p, airport, runway),
                                traffic=traffic, report=report)
            decision("afis", wind_from=weather.wind_from or None,
                     airport=station.ident, said="arrival_information",
                     runway=runway, traffic=traffic or None)
            return _note(session, station, text, "afis_information")
        return Reply(p.roger(aircraft), station)

    # On the ground.
    runway = (session.departure_runway or parsed.get("runway")
              or brain._departure_runway(airport, weather))
    session.departure_runway = runway
    traffic, occupied = _traffic(brain, session, airport, runway, p)
    if intent is Intent.REPORT_RUNWAY_VACATED:
        return Reply(p.roger(aircraft), station)
    if (_field_given(session, station) and not asked
            and (intent in _DEPARTING or intent is Intent.REQUEST_TAKEOFF)):
        # The field was given on the first call. "Ready for departure, for
        # circuits" was given all of it again at Kortrijk; now only what is
        # new: the runway free and at the pilot's discretion where nothing
        # is on it or on final, else the traffic and a report lined up.
        ready = intent is Intent.REQUEST_TAKEOFF or bool(_READY.search(said))
        if ready and not traffic and not occupied:
            session.phase = Phase.LINED_UP
            decision("afis", wind_from=weather.wind_from or None,
                     airport=station.ident, said="takeoff", runway=runway,
                     occupied=False, why="ready, nothing on final")
            return _note(session, station, p.afis_takeoff(aircraft, weather),
                         "afis_takeoff")
        text = (p.afis_runway_occupied(aircraft, traffic) if occupied
                else p.afis_traffic_report(aircraft, traffic, p.REPORT_LINED_UP))
        decision("afis", wind_from=weather.wind_from or None,
                 airport=station.ident, said="traffic", runway=runway,
                 traffic=traffic or None)
        return _note(session, station, text, "afis_traffic")
    if intent in (Intent.REPORT_LINED_UP, Intent.REQUEST_TAKEOFF) \
            and not first_call(session, station):
        text = (p.afis_runway_occupied(aircraft, traffic) if occupied
                else p.afis_takeoff(aircraft, weather))
        session.phase = Phase.LINED_UP
        decision("afis", wind_from=weather.wind_from or None,
                 airport=station.ident, said="takeoff", runway=runway,
                 occupied=occupied)
        return _note(session, station, text, "afis_takeoff")
    if asked or intent in _DEPARTING or intent in (Intent.REPORT_LINED_UP,
                                                   Intent.REQUEST_TAKEOFF):
        session.phase = Phase.TAXI_OUT
        session.field_given_by.add(station_key(station))
        text = p.afis_field(aircraft, name, runway, weather, traffic=traffic,
                            report=p.REPORT_LINED_UP)
        decision("afis", wind_from=weather.wind_from or None,
                 airport=station.ident, said="departure_information",
                 runway=runway, traffic=traffic or None)
        return _note(session, station, text, "afis_information")
    return Reply(p.roger(aircraft), station)


def first_call(session, station) -> bool:
    return not session.established_with(station)


# A pilot asking for the field outright, which gets it whenever asked.
_ASKED_FOR_THE_FIELD = re.compile(
    r"\b(airfield|aerodrome|field) (information|info|details)\b|"
    r"\brequest (the )?information\b|\binformations? (terrain|aerodrome)\b")
_READY = re.compile(r"\b(ready|pret|prete|prets)\b")


def _field_given(session, station) -> bool:
    return station_key(station) in session.field_given_by


# --- the en-route information service ---------------------------------

def _enroute(brain, parsed, session, station, weather, p, state, name) -> Reply:
    aircraft = session.aircraft
    intent = parsed.intent
    if intent in (Intent.REQUEST_FREQUENCY_CHANGE, Intent.REPORT_LEAVING):
        session.squawk = p.vfr_squawk
        text = p.frequency_change_approved(aircraft, squawk=p.vfr_squawk)
        decision("fis", station=station.callsign, said="frequency_change")
        return _note(session, station, text, "frequency_change")
    if name or intent in (Intent.REQUEST_FLIGHT_FOLLOWING, Intent.CHECK_IN):
        region = brain.region_for(station.ident)
        text = p.fis_service(aircraft, name, weather.qnh_hpa, region=region)
        decision("fis", station=station.callsign, said="service", region=region,
                 qnh=round(weather.qnh_hpa))
        return _note(session, station, text, "fis_service")
    return Reply(p.roger(aircraft), station)


# --- flight service ---------------------------------------------------

def _flight_service(brain, parsed, session, station, weather, p, state,
                    name) -> Reply:
    aircraft = session.aircraft
    intent = parsed.intent
    if intent is Intent.OPEN_FLIGHT_PLAN:
        session.flight_plan_open = True
        decision("flight_plan", station=station.callsign, said="opened")
        return _note(session, station, p.flight_plan_activated(aircraft, name),
                     "flight_plan")
    if intent is Intent.CLOSE_FLIGHT_PLAN:
        session.flight_plan_open = False
        decision("flight_plan", station=station.callsign, said="closed")
        return _note(session, station, p.flight_plan_closed(aircraft, name),
                     "flight_plan")
    if intent is Intent.REQUEST_WEATHER:
        field_, report = _weather_for(brain, session, station, state, weather)
        return _note(session, station,
                     p.weather_report(aircraft, field_.spoken, report),
                     "weather")
    if intent in (Intent.REQUEST_FREQUENCY_CHANGE, Intent.REPORT_LEAVING):
        return _note(session, station, p.frequency_change_approved(aircraft),
                     "frequency_change")
    if name:
        # A first call that is not a request yet: the specialist answers and
        # waits for it.
        return Reply(_join_names(p, aircraft, name), station)
    return Reply(p.roger(aircraft), station)


def _join_names(p, aircraft, name) -> str:
    return p.addressed(aircraft, name) + "."


def _weather_for(brain, session, station, state, weather):
    """The field the pilot means: where they are going, or where they are."""
    field_ = session.destination
    if field_ is None and state is not None and getattr(state, "has_position", False):
        field_ = nearest_field(brain.navdb, state.latitude, state.longitude)
    if field_ is None:
        field_ = brain.navdb.airport(station.ident)
    report = weather
    if brain.weather_for is not None and field_ is not None:
        try:
            report = brain.weather_for(field_.ident) or weather
        except Exception:
            report = weather
    return field_, report


__all__ = ["AERODROME", "FIS", "FSS", "Hours", "answer", "compass",
           "is_open", "kind_of", "nearest_field", "qdm", "qdr"]
