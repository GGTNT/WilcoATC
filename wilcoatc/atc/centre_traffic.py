"""The other aeroplanes on a centre's frequency.

At cruise the radio was silent. The traffic (``traffic.py``) works one
airfield -- its stands, taxiways, runway and the approach to it -- and a
pilot handed to Brussels Control or Maastricht Radar flew for an hour on a
frequency nobody else was on, which is the least real thing about an
airliner's cruise: a centre is a steady murmur of other flights checking in,
being moved up and down, and being sent on.

This is that murmur, and only that. Nothing here flies: a centre's
traffic is heard, not seen, so an invented flight is a callsign, a type, a
level, a track and a time to leave, never an aeroplane drawn in the
simulator. The calm is deliberate (``GAP_S``): one exchange every minute or
two at the default density, never two at once, and always behind the pilot
(the engine's queue speaks none of it over the pilot or the controller).

Three kinds of exchange, the ones a cruise is mostly made of:

- a check-in, level ("flight level 350": identified) or climbing/descending
  and given its level, read back;
- a level change, climb or descend to the next level for its direction
  (the semicircular rule, odd thousands eastbound), read back;
- a transfer to the sector its track runs into (``EnRoute.next_along``),
  read back, and the flight gone. Where nothing is found along its track it
  leaves without a word rather than be sent to a frequency that is not there.

The airlines are the region's own (``operators.roster_for`` on the sector's
FIR, short-haul only), the crews speak as they would to that centre
(``Operator.speaks_at``), and the controller is the one the pilot is talking
to -- same station, same voice.

**The real flights**, when live traffic is on: an aeroplane from the feed
above ``LIVE_FLOOR_FT`` inside the sector the pilot is in takes an invented
one's place, checks in at its real level, is cleared to the level it is
seen climbing or descending to, and is transferred when it leaves. They
count against the density, the way they do at a field.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field

from ..logs import decision
from ..navdata.build import project
from ..navdata.db import Station
from .callsigns import every_language
from .language import phraseology_for
from .operators import by_icao, roster_for
from .phraseology import Aircraft, wake_suffix
from .pilot import PilotSpeech
from .traffic import Call, Flight, READBACK_GAP_S, observed_type

log = logging.getLogger(__name__)

# Between two exchanges, in seconds, at density 1. Measured against nothing:
# a quiet upper sector at night is minutes of silence, a busy one in the
# afternoon a call every twenty seconds, and the pilot asked for calm.
GAP_S = (55.0, 140.0)
# The first exchange after the pilot arrives on a frequency.
FIRST_S = (15.0, 45.0)
# How long an invented flight is on the frequency before it is sent on.
STAY_S = (300.0, 900.0)
# How many invented flights at density 1; scaled with it, never more than
# MAX_ON. Fewer when the real ones are there.
ON_AT_DENSITY_1 = 3
MAX_ON = 6
# A real flight below this is the airfield's, not the centre's.
LIVE_FLOOR_FT = 10_000.0
# A real flight this far off its last cleared level, climbing or
# descending, has been given another one.
LIVE_LEVEL_OFF_FT = 1_200.0
LIVE_VS_FPM = 500.0
# How often a real flight's sector is looked up again (a polygon test each).
LIVE_SECTOR_S = 20.0
# Exchange marks of their own, well clear of the field traffic's, so the
# engine never takes one of these for half of one of those.
FIRST_EXCHANGE = 1_000_000_000


@dataclass
class OnCentre:
    """One flight on the frequency."""

    flight: Flight
    level_ft: float                  # where it is
    cleared_ft: float                # where it is going
    track: float
    latitude: float
    longitude: float
    leave_at: float = 0.0
    hex: str = ""                    # a real one: its address in the feed
    checked_in: bool = False
    gone: bool = False
    sector_at: float = 0.0           # when its sector was last looked up
    in_sector: bool = True
    vertical_fpm: float = 0.0


def level_for(track: float, near_ft: float, floor_ft: float,
              ceiling_ft: float) -> float:
    """The level for a direction nearest ``near_ft`` inside the sector:
    odd thousands of flight levels eastbound (track 000-179), even
    westbound -- the ICAO semicircular rule, RVSM included."""
    east = (track % 360.0) < 180.0
    low = max(floor_ft, LIVE_FLOOR_FT)
    high = min(ceiling_ft, 41_000.0)
    levels = [fl * 1000.0 for fl in range(10, 42)
              if (fl % 2 == 1) == east and low <= fl * 1000.0 <= high]
    if not levels:
        return round(near_ft / 1000.0) * 1000.0
    return min(levels, key=lambda ft: abs(ft - near_ft))


class CentreTraffic:
    """Everybody else on the centre frequency the pilot is tuned to."""

    def __init__(self, navdb, *, rng: random.Random | None = None,
                 profile=None) -> None:
        from .profile import DEFAULT

        self.navdb = navdb
        self.random = rng or random.Random()
        # The pilot's flight profile: the region the words follow and how a
        # callsign's digits are read, as for the field's traffic.
        self.profile = profile or DEFAULT
        self._ident = ""
        self.on: list[OnCentre] = []
        self.pending: list[Call] = []
        self.next_at = 0.0
        self._station_key = ""
        self._sector = None
        self._exchanges = FIRST_EXCHANGE
        self._used: set[str] = set()
        self._own = ""
        self._phrases: dict[tuple[str, str], object] = {}

    # -- the frequency ----------------------------------------------------

    def poll(self, now: float, station: Station, *, latitude: float,
             longitude: float, altitude_ft: float, own_callsign: str = "",
             live=(), density: float = 1.0) -> list[Call]:
        """Move the frequency on to ``now`` and return what is due on it."""
        key = f"{station.ident}:{station.mhz:.3f}"
        if key != self._station_key:
            # A new frequency is a new set of people on it.
            self._station_key = key
            self._ident = station.ident
            self.on.clear()
            self.pending.clear()
            self._sector = self._sector_at(latitude, longitude, altitude_ft)
            self.next_at = now + self.random.uniform(*FIRST_S)
        self._own = (own_callsign or "").upper()
        self._used.add(self._own)
        if density > 0:
            self._follow_live(now, live, station)
            if now >= self.next_at and not self.pending:
                self._next_exchange(now, station, latitude, longitude,
                                    altitude_ft, density)
        due = [c for c in self.pending if c.at <= now]
        self.pending = [c for c in self.pending if c.at > now]
        self.on = [o for o in self.on if not o.gone]
        return due

    def _sector_at(self, lat: float, lon: float, alt: float):
        try:
            return self.navdb.enroute.which(lat, lon, alt)
        except Exception:
            log.debug("no sector for the centre traffic", exc_info=True)
            return None

    def _next_exchange(self, now: float, station: Station, lat: float,
                       lon: float, alt: float, density: float) -> None:
        self.next_at = now + self.random.uniform(*GAP_S) / max(density, 0.25)
        # The real ones first: what they do is happening, not made up.
        for one in self.on:
            if one.hex and not one.checked_in and one.in_sector:
                self._check_in(one, now, station)
                return
        for one in self.on:
            if one.hex and one.checked_in and not one.in_sector:
                self._transfer(one, now, station)
                return
        for one in self.on:
            if one.hex and one.checked_in and one.cleared_ft != \
                    self._live_target(one):
                self._change_level(one, now, station, self._live_target(one))
                return
        invented = [o for o in self.on if not o.hex]
        for one in invented:
            if now >= one.leave_at:
                self._transfer(one, now, station)
                return
        live = sum(1 for o in self.on if o.hex)
        wanted = min(MAX_ON, round(ON_AT_DENSITY_1 * density)) - live
        if len(invented) < wanted:
            one = self._invent(now, station, lat, lon, alt)
            if one is not None:
                self.on.append(one)
                self._check_in(one, now, station)
                return
        level = [o for o in invented if o.checked_in]
        if level and self.random.random() < 0.5:
            one = self.random.choice(level)
            target = self._new_level(one)
            if target != one.cleared_ft:
                self._change_level(one, now, station, target)

    # -- the invented ones ------------------------------------------------

    def _invent(self, now: float, station: Station, lat: float, lon: float,
                alt: float) -> OnCentre | None:
        candidates = [c for c in roster_for(station.ident, 3)
                      if not c.operator.long_haul]
        if not candidates:
            return None
        operator = self.random.choices(
            [c.operator for c in candidates],
            weights=[c.weight for c in candidates])[0]
        low, high = operator.number_range
        written = ""
        for _ in range(20):
            tried = f"{operator.icao}{self.random.randint(low, high)}"
            if tried not in self._used:
                written = tried
                break
        if not written:
            return None
        self._used.add(written)
        type_code = self.random.choice(operator.fleet)
        flight = self._flight(written, type_code, operator,
                              operator.speaks_at(station.ident))
        track = self.random.uniform(0.0, 360.0)
        floor, ceiling = self._limits()
        # Near the pilot's level, as the flights in one sector are: a
        # sector is a band of levels as much as an area.
        cleared = level_for(track, alt + self.random.uniform(-6000, 6000),
                            floor, ceiling)
        # One in three is still climbing or already descending to it, from
        # a level inside the sector and below the ceiling of the airliners.
        level = cleared
        if self.random.random() < 0.35:
            low, high = max(floor, LIVE_FLOOR_FT), min(ceiling, 41_000.0)
            level = min(high, max(low, cleared + self.random.choice((-1, 1))
                                  * self.random.uniform(3000, 8000)))
        lat2, lon2 = project(lat, lon, self.random.uniform(0.0, 360.0),
                             self.random.uniform(10.0, 60.0))
        flight.altitude_ft = level
        return OnCentre(flight=flight, level_ft=level, cleared_ft=cleared,
                        track=track, latitude=lat2, longitude=lon2,
                        leave_at=now + self.random.uniform(*STAY_S))

    def _limits(self) -> tuple[float, float]:
        sector = self._sector
        if sector is None:
            return 0.0, 41_000.0
        return sector.floor_ft, sector.ceiling_ft

    def _new_level(self, one: OnCentre) -> float:
        floor, ceiling = self._limits()
        step = self.random.choice((-2000.0, 2000.0))
        return level_for(one.track, one.cleared_ft + step, floor, ceiling)

    def _flight(self, written: str, type_code: str, operator,
                language: str) -> Flight:
        forms = every_language(written, type_code,
                               dialect=self.profile.dialect_for(self._ident))
        aircraft = Aircraft(forms["en"], type_code, wake_suffix(type_code),
                            callsigns=forms, language=language)
        return Flight(written=written, type_code=type_code, aircraft=aircraft,
                      language=language, operator=operator, arriving=False,
                      phase="cruise", on_ground=False)

    # -- the real ones ----------------------------------------------------

    def _follow_live(self, now: float, live, station: Station) -> None:
        heard = {getattr(one, "hex", "") or one.callsign: one for one in live
                 if not one.on_ground and one.callsign
                 and (one.altitude_ft or 0.0) >= LIVE_FLOOR_FT
                 and one.callsign.strip().upper() != self._own}
        known = {o.hex: o for o in self.on if o.hex}
        for hex_, one in heard.items():
            mine = known.get(hex_)
            if mine is None:
                if not self._inside(one.latitude, one.longitude,
                                    one.altitude_ft):
                    continue
                written = one.callsign.strip().upper()
                operator = (by_icao(written[:3]) if len(written) > 3
                            and written[:3].isalpha()
                            and written[3].isdigit() else None)
                language = (operator.speaks_at(station.ident) if operator
                            else "en")
                flight = self._flight(written, observed_type(one.type_code),
                                      operator, language)
                flight.observed, flight.source = True, "live"
                self._used.add(written)
                mine = OnCentre(flight=flight, level_ft=one.altitude_ft,
                                cleared_ft=self._rounded(one.altitude_ft),
                                track=getattr(one, "track", 0.0) or one.heading,
                                latitude=one.latitude, longitude=one.longitude,
                                hex=hex_, sector_at=now)
                self.on.append(mine)
                decision("centre_traffic", event="live joined",
                         flight=written, level=round(one.altitude_ft),
                         station=station.callsign)
                continue
            mine.level_ft = one.altitude_ft
            mine.flight.altitude_ft = one.altitude_ft
            mine.latitude, mine.longitude = one.latitude, one.longitude
            mine.track = getattr(one, "track", 0.0) or one.heading
            mine.vertical_fpm = one.vertical_speed_fpm or 0.0
            if now - mine.sector_at >= LIVE_SECTOR_S:
                mine.sector_at = now
                mine.in_sector = self._inside(one.latitude, one.longitude,
                                              one.altitude_ft)
        for hex_, mine in known.items():
            if hex_ not in heard:
                if mine.checked_in:
                    mine.in_sector = False   # sent on at the next exchange
                else:
                    mine.gone = True         # never heard: nothing to say

    def _inside(self, lat: float, lon: float, alt: float) -> bool:
        if self._sector is None:
            return False
        found = self._sector_at(lat, lon, alt)
        return found is not None and found.key == self._sector.key

    @staticmethod
    def _rounded(feet: float) -> float:
        return round(feet / 1000.0) * 1000.0

    def _live_target(self, one: OnCentre) -> float:
        """The level a real flight is seen going to: where it is, rounded,
        once level; the next one for its direction past where it is while
        it climbs or descends. Its cleared level until it has clearly left
        it."""
        vs = one.vertical_fpm
        if abs(one.level_ft - one.cleared_ft) < LIVE_LEVEL_OFF_FT \
                or abs(vs) < LIVE_VS_FPM:
            return one.cleared_ft
        floor, ceiling = self._limits()
        ahead = one.level_ft + (4000.0 if vs > 0 else -4000.0)
        return level_for(one.track, ahead, floor, ceiling)

    # -- what is said -----------------------------------------------------

    def _check_in(self, one: OnCentre, now: float, station: Station) -> None:
        one.checked_in = True
        level = one.level_ft
        cleared = one.cleared_ft
        if abs(level - cleared) < LIVE_LEVEL_OFF_FT:
            self._exchange(one, now, station, [
                (True, lambda p, a, s: p.maintaining(a, s, cleared)),
                (False, lambda c, a, s: c.centre_check_in(a, s)),
            ], "check in")
            return
        climbing = cleared > level
        # Identified and given its level; a descent is given as one, the
        # phrasebooks having no descent clause on its own.
        answer = ((lambda c, a, s: c.radar_contact(
                      a, s, instruction=c.climb_clause(cleared)))
                  if climbing else
                  (lambda c, a, s: c.descend_maintain(
                      a, cleared, station=s, abbreviated=False)))
        self._exchange(one, now, station, [
            (True, lambda p, a, s: p.checking_in(a, s, self._rounded(level),
                                                 climbing)),
            (False, answer),
            (True, lambda p, a, s: p.level(a, cleared, climbing)),
        ], "check in moving")

    def _change_level(self, one: OnCentre, now: float, station: Station,
                      target: float) -> None:
        climbing = target > one.cleared_ft
        one.cleared_ft = target
        self._exchange(one, now, station, [
            (False, (lambda c, a, s: c.climb_maintain(a, target)) if climbing
             else (lambda c, a, s: c.descend_maintain(a, target))),
            (True, lambda p, a, s: p.level(a, target, climbing)),
        ], "climb" if climbing else "descend")

    def _transfer(self, one: OnCentre, now: float, station: Station) -> None:
        one.gone = True
        nxt = None
        try:
            crossing = self.navdb.enroute.next_along(
                one.latitude, one.longitude, one.track, 450.0, one.cleared_ft,
                minutes_ahead=20.0)
            nxt = crossing.sector.station() if crossing is not None else None
        except Exception:
            log.debug("no sector ahead of %s", one.flight.written,
                      exc_info=True)
        if nxt is None or not nxt.mhz or (nxt.ident == station.ident
                                          and nxt.mhz == station.mhz):
            decision("centre_traffic", event="left unsaid",
                     flight=one.flight.written, station=station.callsign)
            return
        language = one.flight.language
        name = nxt.callsign_in(language)
        mhz = nxt.mhz
        self._exchange(one, now, station, [
            (False, lambda c, a, s: c.handoff(a, name, mhz, farewell=True)),
            (True, lambda p, a, s: p.contact(a, name, mhz)),
        ], "transfer")

    def _exchange(self, one: OnCentre, now: float, station: Station,
                  halves, what: str) -> None:
        """Compose every half now and queue them spaced as a real exchange
        is; nothing if any half will not compose (half an exchange is heard
        as a lost transmission)."""
        flight = one.flight
        language = flight.language
        phrase = self._phrase(language, station.ident)
        pilot = PilotSpeech(language, phrase)
        name = station.callsign_in(language)
        self._exchanges += 1
        calls: list[Call] = []
        at = now
        for from_pilot, make in halves:
            try:
                text = make(pilot if from_pilot else phrase, flight.aircraft,
                            name)
            except Exception:
                log.debug("could not compose the centre's %s for %s", what,
                          flight.written, exc_info=True)
                return
            if not text:
                return
            calls.append(Call(
                text=text, language=language, from_pilot=from_pilot,
                position=station.position,
                speaker=(flight.aircraft.for_language(language).spoken_short
                         if from_pilot else name),
                flight=flight, at=at, exchange=self._exchanges,
                mhz=station.mhz))
            at += READBACK_GAP_S + self.random.uniform(0.4, 1.6)
        self.pending.extend(calls)
        decision("centre_traffic", event=what, flight=flight.written,
                 live=bool(one.hex), level=round(one.cleared_ft),
                 station=station.callsign)

    def _phrase(self, language: str, ident: str):
        key = (language, ident)
        found = self._phrases.get(key)
        if found is None:
            found = phraseology_for(language, ident,
                                    region=self.profile.region)
            self._phrases[key] = found
        return found


__all__ = ["CentreTraffic", "OnCentre", "level_for"]
