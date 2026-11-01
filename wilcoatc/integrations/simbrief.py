"""The flight plan the pilot already built.

SimBrief holds the operational flight plan: the callsign, the aeroplane, where
it is going, how high, by what route and with what alternate. Typing all of
that into a second window is exactly the kind of chore that makes people stop
using a tool, so this reads the latest plan for a username and fills the flight
in from it.

It is read-only and it is optional. Nothing here changes what the controller
says; it changes what the controller already knows before you press the key.

The API is a single GET that returns the whole plan as JSON:

    https://www.simbrief.com/api/xml.fetcher.php?username=<name>&json=1

A numeric argument is treated as a SimBrief pilot ID rather than a username,
because both are in circulation and people rarely remember which they have.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

ENDPOINT = "https://www.simbrief.com/api/xml.fetcher.php"
TIMEOUT_S = 20.0


class SimBriefError(Exception):
    """The plan could not be fetched, with a reason worth showing a person."""


@dataclass
class Ofp:
    """The parts of an operational flight plan a controller cares about."""

    callsign: str = ""              # what you are called on the radio
    aircraft_type: str = ""         # ICAO type, e.g. B738
    registration: str = ""
    origin: str = ""
    destination: str = ""
    alternate: str = ""
    cruise_altitude_ft: int = 0
    route: str = ""
    airline: str = ""               # ICAO designator, e.g. AFR
    flight_number: str = ""
    planned_runway: str = ""
    # The destination's planned runway, and the departure and arrival
    # procedures off the navigation log: each fix there says whether it is
    # part of a SID or STAR and which one ("is_sid_star", "via_airway", as
    # the MSFS avionics SDK's SimbriefNavlogFix documents them). The plan's
    # approach and STAR transition are not read: no field for either was
    # confirmed.
    arrival_runway: str = ""
    sid: str = ""
    star: str = ""
    # Which rules the plan was filed under, None where it does not say. It
    # used to be IFR for want of an answer, and loading a plan silently made
    # a VFR pilot's flight IFR; a plan that says nothing now leaves the
    # pilot's own choice (flight.ifr) alone.
    ifr: bool | None = None
    generated_at: str = ""
    # The schedule, as UTC epoch seconds, and zero where the plan does not
    # say. Out and in are the blocks, off and on are the wheels. Nothing on
    # the radio reads these; they are what the landing report measures the
    # flight against, and a plan built without a departure time has no
    # schedule to be late for.
    sched_out: int = 0
    sched_off: int = 0
    sched_on: int = 0
    sched_in: int = 0
    # Planned time in the air, in seconds: the one comparison that means
    # something whatever the simulator's clock says.
    planned_air_s: int = 0
    # The navigation log, every fix of it, in order. It used to be read out
    # of ``raw`` in two places for two lists -- points for the en-route
    # look-ahead, names for a direct -- and the altitudes, stages and times
    # were dropped, which is what a top of descent is made of.
    navlog: tuple["NavlogFix", ...] = ()
    # (lat, lon, elevation ft) of the departure, which the log leaves out.
    origin_position: tuple[float, float, float] | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def described(self) -> str:
        """One line, for the console and the panel."""
        legs = " to ".join(x for x in (self.origin, self.destination) if x)
        bits = [self.callsign or "no callsign", self.aircraft_type or "?", legs]
        if self.cruise_altitude_ft:
            bits.append(f"FL{self.cruise_altitude_ft // 100:03d}")
        if self.alternate:
            bits.append(f"alternate {self.alternate}")
        return ", ".join(b for b in bits if b)


@dataclass(frozen=True)
class NavlogFix:
    """One fix of the navigation log, as the plan has it."""

    ident: str
    name: str
    lat: float
    lon: float
    alt_ft: int = 0
    stage: str = ""          # CLB, CRZ, DSC
    via: str = ""            # the airway, SID or STAR; "DCT"
    on_procedure: bool = False   # is_sid_star
    time_s: int = 0          # time_total: seconds from takeoff
    time_leg_s: int = 0
    kind: str = ""           # wpt, vor, ndb, apt, ltlg
    region: str = ""


def _navlog(data: dict) -> tuple[NavlogFix, ...]:
    """Every fix with a position; one without is not a point on a route."""
    fixes = _section(data, "navlog").get("fix") or []
    if isinstance(fixes, dict):
        fixes = [fixes]
    out = []
    for fix in fixes:
        if not isinstance(fix, dict):
            continue
        try:
            lat, lon = float(fix["pos_lat"]), float(fix["pos_long"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append(NavlogFix(
            ident=str(fix.get("ident") or "").strip().upper(),
            name=str(fix.get("name") or ""),
            lat=lat, lon=lon,
            alt_ft=_number(fix, "altitude_feet"),
            stage=_text(fix, "stage").upper(),
            via=_text(fix, "via_airway").upper(),
            on_procedure=_text(fix, "is_sid_star") == "1",
            time_s=_number(fix, "time_total"),
            time_leg_s=_number(fix, "time_leg"),
            kind=_text(fix, "type").lower(),
            region=_text(fix, "icao_region").upper()))
    return tuple(out)


def fetch(user: str, timeout_s: float = TIMEOUT_S,
          opener=urllib.request.urlopen) -> Ofp:
    """The latest plan for a SimBrief username or pilot ID.

    ``opener`` exists so the parsing can be exercised without the network.
    """
    user = (user or "").strip()
    if not user:
        raise SimBriefError("No SimBrief username set.")

    key = "userid" if user.isdigit() else "username"
    url = f"{ENDPOINT}?{urllib.parse.urlencode({key: user, 'json': '1'})}"
    try:
        with opener(url, timeout=timeout_s) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise SimBriefError(
            f"SimBrief refused the request ({exc.code}). "
            f"Check the username.") from exc
    except urllib.error.URLError as exc:
        raise SimBriefError(f"Could not reach SimBrief: {exc.reason}") from exc
    except Exception as exc:                       # pragma: no cover - defensive
        raise SimBriefError(f"Could not reach SimBrief: {exc}") from exc

    return parse(body)


def _ifr(general: dict) -> bool | None:
    """Whether the plan was filed IFR, or None where it does not say.

    Read from whichever key this payload uses. ICAO writes the rules as a
    single letter -- I, V, Y, Z -- and the two mixed ones both start under
    instrument rules.
    """
    said = ""
    for key in ("flight_rules", "flightrules", "rules"):
        found = _text(general, key)
        if found:
            said = found.strip().lower()
            break
    if not said:
        return None
    return not said.startswith("v")


def parse(body: bytes | str | dict) -> Ofp:
    """Turn the API's JSON into the handful of facts a flight needs.

    The payload is large and its shape has changed over the years, so every
    field is read defensively: a plan missing an alternate is a plan, not an
    error.
    """
    if isinstance(body, (bytes, str)):
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise SimBriefError("SimBrief returned something that is not a "
                                "flight plan.") from exc
    else:
        data = body

    if not isinstance(data, dict):
        raise SimBriefError("SimBrief returned something that is not a "
                            "flight plan.")

    fetched = _text(_section(data, "fetch"), "status")
    if fetched and fetched.lower() not in ("success", "ok"):
        raise SimBriefError(f"SimBrief said: {fetched}")

    general = _section(data, "general")
    origin = _section(data, "origin")
    destination = _section(data, "destination")
    alternate = _section(data, "alternate")
    aircraft = _section(data, "aircraft")
    atc = _section(data, "atc")
    params = _section(data, "params")
    times = _section(data, "times")

    airline = _text(general, "icao_airline")
    number = _text(general, "flight_number")
    callsign = _text(atc, "callsign") or (f"{airline}{number}"
                                          if airline and number else number)

    plan = Ofp(
        callsign=callsign.upper(),
        aircraft_type=_text(aircraft, "icaocode", "icao_code").upper(),
        registration=_text(aircraft, "reg").upper(),
        origin=_text(origin, "icao_code").upper(),
        destination=_text(destination, "icao_code").upper(),
        alternate=_text(alternate, "icao_code").upper(),
        cruise_altitude_ft=_altitude(general),
        ifr=_ifr(general),
        route=_text(general, "route"),
        airline=airline.upper(),
        flight_number=number,
        planned_runway=_text(origin, "plan_rwy").upper(),
        arrival_runway=_text(destination, "plan_rwy").upper(),
        sid=_procedures(data)[0],
        star=_procedures(data)[1],
        generated_at=_text(params, "time_generated"),
        sched_out=_number(times, "sched_out"),
        sched_off=_number(times, "sched_off"),
        sched_on=_number(times, "sched_on"),
        sched_in=_number(times, "sched_in"),
        planned_air_s=_number(times, "est_time_enroute", "sched_time_enroute"),
        navlog=_navlog(data),
        origin_position=_position(origin),
        raw=data,
    )
    if not plan.origin or not plan.destination:
        raise SimBriefError("That plan has no origin or destination.")
    return plan


def _procedures(data: dict) -> tuple[str, str]:
    """The SID and the STAR, from the navigation log.

    Procedure fixes before the first en-route fix are the SID, after the
    last one the STAR; each is named by its fixes' airway.
    """
    fixes = _section(data, "navlog").get("fix") or []
    if isinstance(fixes, dict):
        fixes = [fixes]
    fixes = [f for f in fixes if isinstance(f, dict)]
    on_procedure = [str(f.get("is_sid_star") or "0").strip() == "1" for f in fixes]
    names = [str(f.get("via_airway") or "").strip().upper() for f in fixes]
    enroute = [i for i, flag in enumerate(on_procedure) if not flag]
    first = enroute[0] if enroute else len(fixes)
    last = enroute[-1] if enroute else -1
    sid = next((names[i] for i in range(first)
                if on_procedure[i] and names[i] not in ("", "DCT")), "")
    star = next((names[i] for i in range(len(fixes) - 1, last, -1)
                 if on_procedure[i] and names[i] not in ("", "DCT")), "")
    return sid, star


def route_points(plan) -> list[tuple[float, float]]:
    """The navigation log's fixes as (lat, lon), in order.

    What the en-route controller looks ahead along (atc/enroute.py): an
    airway does not run straight for ten minutes into a corner of a FIR the
    flight never enters, and the track alone would hand it there.
    """
    return [(f.lat, f.lon) for f in _fixes_of(plan)]


@dataclass(frozen=True)
class PlanFix:
    ident: str
    name: str
    lat: float
    lon: float


def route_fixes(plan) -> list[PlanFix]:
    """The navigation log's fixes by name, in order.

    What a centre grants a direct to: a point on the route the flight
    filed, not any navaid that sounds like the word the recogniser heard.
    """
    return [PlanFix(f.ident, f.name, f.lat, f.lon)
            for f in _fixes_of(plan) if f.ident]


def _fixes_of(plan) -> tuple[NavlogFix, ...]:
    """The parsed log, or the raw one for a plan built without it."""
    found = getattr(plan, "navlog", None)
    if found:
        return tuple(found)
    return _navlog(getattr(plan, "raw", None) or {})


def _position(section: dict) -> tuple[float, float, float] | None:
    try:
        return (float(section["pos_lat"]), float(section["pos_long"]),
                float(section.get("elevation") or 0))
    except (KeyError, TypeError, ValueError):
        return None


def _section(data: dict, name: str) -> dict:
    """One block of the payload, which may be absent or a list of one."""
    found = data.get(name)
    if isinstance(found, list):
        found = found[0] if found else None
    return found if isinstance(found, dict) else {}


def _text(section: dict, *names: str) -> str:
    for name in names:
        value = section.get(name)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return ""


def _number(section: dict, *names: str) -> int:
    """A whole number of seconds, or zero for anything that is not one."""
    raw = _text(section, *names)
    try:
        return max(0, int(float(raw)))
    except ValueError:
        return 0


def _altitude(general: dict) -> int:
    """The cruise level in feet.

    SimBrief gives it as a string of feet, and occasionally as a flight level
    for a plan built in a metric region.
    """
    raw = _text(general, "initial_altitude", "cruise_altitude", "altitude")
    if not raw:
        return 0
    digits = "".join(c for c in raw if c.isdigit())
    if not digits:
        return 0
    value = int(digits)
    # A number under 1,000 is a flight level, not feet.
    return value * 100 if value < 1000 else value


__all__ = ["Ofp", "NavlogFix", "SimBriefError", "fetch", "parse", "ENDPOINT"]
