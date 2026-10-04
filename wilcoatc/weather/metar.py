"""Weather for ATIS and controller wind checks.

Two sources, in priority order:

1. **The simulator**, for the field the aircraft is at or about to touch.
   If the user has set custom weather in MSFS, or is flying a historical
   scenario, the real-world METAR is wrong and the ATIS must follow the sim.
   SimConnect reports *ambient* wind, pressure and temperature -- what is
   around the aeroplane -- so it is the field's weather only near the field
   (:func:`near_the_field`). Used everywhere, it read the jet stream out as a
   destination's surface wind.

2. **Real-world METAR** from the NOAA Aviation Weather Center, which is what
   MSFS live weather is itself derived from, or the nearest field's when this
   one does not report. Cached so a lost network connection does not stop the
   ATIS; a station that failed is not asked again for five minutes; and
   nothing on the reply path waits for a fetch.

The parser handles raw METAR text as well as the JSON API, because the raw
observation is the authoritative form and the API occasionally omits fields
that are present in it.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from ..atc.phraseology import Weather
from ..paths import data_dir

log = logging.getLogger(__name__)

AWC_URL = "https://aviationweather.gov/api/data/metar"
CACHE_PATH = data_dir() / "metar_cache.json"

HPA_PER_INHG = 33.8638866667

# A station that failed to answer is left alone for this long. Without it a
# field with no reporting station was asked on every look: one session in the
# logs asked for LFDU seventy-one times.
FAILURE_RETRY_S = 300.0

# What the observation service accepts as a station: four letters. Strips in
# the navigation data carry idents such as ES-0058 or a US "3W5", and sending
# those is a request that can only fail.
_RE_STATION = re.compile(r"[A-Z]{4}")


def reportable(station: str) -> bool:
    """Whether an ident is something the observation service could know."""
    return bool(_RE_STATION.fullmatch((station or "").strip().upper()))


def inhg_from_hpa(hpa: float) -> float:
    return float(hpa) / HPA_PER_INHG


def hpa_from_inhg(inhg: float) -> float:
    return float(inhg) * HPA_PER_INHG


# --------------------------------------------------------------------------
# raw METAR parsing
# --------------------------------------------------------------------------

_RE_WIND = re.compile(r"\b(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?(KT|MPS|KMH)\b")
_RE_VIS_SM = re.compile(r"\b(\d{1,2})?\s?(\d/\d)?SM\b")
_RE_VIS_M = re.compile(r"\b(\d{4})\b")
_RE_TEMP = re.compile(r"\b(M?\d{2})/(M?\d{2})\b")
_RE_ALTIM_IN = re.compile(r"\bA(\d{4})\b")
_RE_ALTIM_HPA = re.compile(r"\bQ(\d{4})\b")
_RE_CLOUD = re.compile(r"\b(FEW|SCT|BKN|OVC|VV)(\d{3})(CB|TCU)?\b")
_RE_TIME = re.compile(r"\b(\d{2})(\d{2})(\d{2})Z\b")

CLOUD_WORDS = {
    "FEW": "few clouds at",
    "SCT": "scattered clouds at",
    "BKN": "broken clouds at",
    "OVC": "overcast at",
    "VV": "indefinite ceiling",
}


def _num(text: str) -> float:
    """Parse a METAR signed number, where a leading M means minus."""
    text = text.strip()
    return -float(text[1:]) if text.upper().startswith("M") else float(text)


def parse_metar(raw: str) -> Weather:
    """Parse a raw METAR observation into a :class:`Weather`."""
    wx = Weather(raw=raw or "")
    if not raw:
        return wx
    text = raw.upper()

    # Everything after RMK is a coded remark, not part of the observation.
    body = text.split(" RMK", 1)[0]

    m = _RE_WIND.search(body)
    if m:
        direction, speed, gust, unit = m.groups()
        factor = {"KT": 1.0, "MPS": 1.94384, "KMH": 0.539957}[unit]
        wx.wind_dir = 0.0 if direction == "VRB" else float(direction)
        wx.wind_kt = float(speed) * factor
        wx.gust_kt = float(gust) * factor if gust else 0.0
        if direction == "VRB":
            # Not a wind from the north: no direction at all. Read as 360 it
            # chose the northerly runway in every light variable wind.
            wx.variable = True
            wx.remarks = (wx.remarks + " variable").strip()

    if "CAVOK" in body:
        wx.visibility_sm = 10.0
        wx.clouds = []
    else:
        vis = _parse_visibility(body)
        if vis is not None:
            wx.visibility_sm = vis

    m = _RE_TEMP.search(body)
    if m:
        wx.temperature_c = _num(m.group(1))
        wx.dewpoint_c = _num(m.group(2))

    m = _RE_ALTIM_IN.search(body)
    if m:
        wx.altimeter_inhg = float(m.group(1)) / 100.0
        wx.qnh_hpa = hpa_from_inhg(wx.altimeter_inhg)
    else:
        m = _RE_ALTIM_HPA.search(body)
        if m:
            wx.qnh_hpa = float(m.group(1))
            wx.altimeter_inhg = inhg_from_hpa(wx.qnh_hpa)

    clouds: list[tuple[str, int]] = []
    for cover, height, _kind in _RE_CLOUD.findall(body):
        clouds.append((cover, int(height) * 100))
    if "CLR" in body or "SKC" in body or "NCD" in body or "NSC" in body:
        clouds = []
    wx.clouds = sorted(clouds, key=lambda c: c[1])

    return wx


def _parse_visibility(body: str) -> float | None:
    """Visibility in statute miles, from either US or metric encoding."""
    if "9999" in body:
        return 10.0
    m = _RE_VIS_SM.search(body)
    if m and (m.group(1) or m.group(2)):
        whole = float(m.group(1)) if m.group(1) else 0.0
        if m.group(2):
            num, _, den = m.group(2).partition("/")
            whole += float(num) / float(den)
        return whole
    # Metric visibility is a bare four-digit group, but so are many other
    # things, so only accept it where a visibility group can legally appear.
    for token in body.split():
        if re.fullmatch(r"\d{4}", token):
            return float(token) / 1609.34
    return None


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------


@dataclass
class WeatherSample:
    weather: Weather
    source: str            # "sim" / "metar" / "cache" / "default"
    station: str = ""
    observed_utc: str = ""
    age_s: float = 0.0


class WeatherProvider:
    """Fetches and caches weather, preferring the simulator's own conditions."""

    def __init__(
        self,
        cache_path: Path = CACHE_PATH,
        refresh_s: float = 600.0,
        timeout_s: float = 8.0,
        online: bool = True,
    ):
        self.cache_path = Path(cache_path)
        self.refresh_s = refresh_s
        self.timeout_s = timeout_s
        self.online = online
        self._lock = threading.Lock()
        self._memory: dict[str, tuple[float, Weather, str]] = {}
        # When each station last failed, so it is not asked again at once.
        self._failed: dict[str, float] = {}
        # Stations being fetched on a worker, so a second caller does not
        # start a second fetch of the same one.
        self._inflight: set[str] = set()
        self._load_cache()

    # -- cache ---------------------------------------------------------

    def _load_cache(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("could not read weather cache: %s", exc)
            return
        for station, entry in data.items():
            wx = parse_metar(entry.get("raw", ""))
            self._memory[station] = (entry.get("fetched", 0.0), wx, entry.get("raw", ""))

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                station: {"fetched": fetched, "raw": raw}
                for station, (fetched, _wx, raw) in self._memory.items()
                if raw
            }
            self.cache_path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        except Exception as exc:
            log.debug("could not write weather cache: %s", exc)

    # -- fetch ---------------------------------------------------------

    def metar(self, station: str, force: bool = False,
              wait: bool = True) -> WeatherSample:
        """Real-world observation for an ICAO station.

        ``wait=False`` is for anything on the reply path or the situation
        loop: a fetch that is due is started on a worker and whatever is
        already known is returned at once. The timeout is eight seconds, and
        a controller who answers eight seconds late has not answered.
        """
        station = (station or "").strip().upper()
        if not reportable(station):
            return WeatherSample(Weather(), "default", station)

        now = time.time()
        with self._lock:
            cached = self._memory.get(station)
            failed_at = self._failed.get(station)
        if cached and not force and (now - cached[0]) < self.refresh_s:
            return WeatherSample(cached[1], "metar", station, age_s=now - cached[0])

        recently_failed = (failed_at is not None and not force
                           and (now - failed_at) < FAILURE_RETRY_S)
        if self.online and not recently_failed:
            if not wait:
                self._fetch_in_background(station)
            else:
                fetched = self._fetch_and_store(station)
                if fetched is not None:
                    return WeatherSample(fetched, "metar", station)

        return self._known(station, now)

    def _known(self, station: str, now: float) -> WeatherSample:
        """Whatever is already in hand for a station, however old."""
        with self._lock:
            cached = self._memory.get(station)
        if cached:
            return WeatherSample(cached[1], "cache", station, age_s=now - cached[0])
        return WeatherSample(Weather(), "default", station)

    def _fetch_and_store(self, station: str) -> Weather | None:
        fetched = self._fetch(station)
        now = time.time()
        if fetched is None:
            with self._lock:
                self._failed[station] = now
            return None
        raw, wx = fetched
        with self._lock:
            self._memory[station] = (now, wx, raw)
            self._failed.pop(station, None)
        self._save_cache()
        return wx

    def _fetch_in_background(self, station: str) -> None:
        with self._lock:
            if station in self._inflight:
                return
            self._inflight.add(station)

        def run() -> None:
            try:
                self._fetch_and_store(station)
            finally:
                with self._lock:
                    self._inflight.discard(station)

        threading.Thread(target=run, name=f"metar-{station}", daemon=True).start()

    def _fetch(self, station: str) -> tuple[str, Weather] | None:
        try:
            response = requests.get(
                AWC_URL,
                params={"ids": station, "format": "json"},
                timeout=self.timeout_s,
                headers={"User-Agent": "wilcoatc/1.0"},
            )
            response.raise_for_status()
            rows = response.json()
        except Exception as exc:
            log.info("METAR fetch failed for %s: %s", station, exc)
            return None
        if not rows:
            return None

        row = rows[0]
        raw = row.get("rawOb") or ""
        wx = parse_metar(raw)

        # The API carries a few fields more reliably than the raw text.
        if row.get("altim") is not None and not _RE_ALTIM_IN.search(raw.upper()):
            wx.qnh_hpa = float(row["altim"])
            wx.altimeter_inhg = inhg_from_hpa(wx.qnh_hpa)
        if row.get("temp") is not None:
            wx.temperature_c = float(row["temp"])
        if row.get("dewp") is not None:
            wx.dewpoint_c = float(row["dewp"])
        return raw, wx

    def nearest_metar(self, lat: float, lon: float, navdb, max_nm: float = 90.0,
                      wait: bool = True) -> WeatherSample:
        """METAR from the nearest field likely to have one.

        Small strips do not report, so only fields with scheduled service or a
        published weather frequency are tried, nearest first. Without
        waiting, no more than two fetches are started per call: the next call
        finds them answered, and twelve threads for one wind is not a lookup.
        """
        candidates = navdb.nearest_airports(lat, lon, limit=12, max_nm=max_nm, min_rank=1)
        started = 0
        for airport, _d in candidates:
            if not reportable(airport.icao):
                continue
            if airport.size_rank < 2 and not airport.scheduled:
                continue
            with self._lock:
                known = airport.icao.upper() in self._memory
            if not wait and not known:
                if started >= 2:
                    continue
                started += 1
            sample = self.metar(airport.icao, wait=wait)
            if sample.source in ("metar", "cache") and sample.weather.raw:
                return sample
        return WeatherSample(Weather(), "default")


def weather_from_sim(state) -> Weather | None:
    """Build a :class:`Weather` from what the simulator reports.

    Returns ``None`` when the sim is not supplying usable values, so the caller
    can fall back to a real-world observation.
    """
    if state is None:
        return None
    pressure = getattr(state, "sea_level_pressure_hpa", 0.0) or 0.0
    if pressure <= 800 or pressure >= 1100:
        return None

    wx = Weather()
    wx.wind_dir = float(getattr(state, "ambient_wind_direction", 0.0) or 0.0)
    wx.wind_kt = float(getattr(state, "ambient_wind_velocity", 0.0) or 0.0)
    wx.qnh_hpa = pressure
    wx.altimeter_inhg = inhg_from_hpa(pressure)
    wx.temperature_c = float(getattr(state, "ambient_temperature_c", 15.0) or 15.0)
    wx.dewpoint_c = wx.temperature_c - max(
        0.0, (100.0 - float(getattr(state, "ambient_humidity_pct", 60.0) or 60.0)) / 5.0
    )
    visibility_m = float(getattr(state, "ambient_visibility_m", 0.0) or 0.0)
    wx.visibility_sm = min(10.0, visibility_m / 1609.34) if visibility_m else 10.0
    wx.raw = ""
    return wx


# How close the aeroplane has to be for the simulator's ambient values to be
# the field's: a few miles, and about a circuit's height. Beyond either, the
# sim is reporting the wind somewhere the field's windsock is not.
NEAR_FIELD_NM = 5.0
NEAR_FIELD_AGL_FT = 2500.0
# The ambient wind is the wind at the aeroplane, and on a short final that is
# not the surface wind: below this, or on the ground, it is close enough.
SURFACE_WIND_AGL_FT = 500.0


def at_the_surface(state, airport) -> bool:
    """On the ground, or low enough that the wind here is the windsock's."""
    if not near_the_field(state, airport):
        return False
    if state.on_ground:
        return True
    elevation = float(getattr(airport, "elev_ft", 0.0) or 0.0)
    above = (getattr(state, "altitude_agl_ft", 0.0)
             or max(0.0, float(state.altitude_ft or 0.0) - elevation))
    return above <= SURFACE_WIND_AGL_FT


def near_the_field(state, airport) -> bool:
    """Whether the aeroplane is at a field or about to touch down on it."""
    if state is None or airport is None or not getattr(state, "has_position", False):
        return False
    if state.distance_to(airport.lat, airport.lon) > NEAR_FIELD_NM:
        return False
    if state.on_ground:
        return True
    elevation = float(getattr(airport, "elev_ft", 0.0) or 0.0)
    above = (getattr(state, "altitude_agl_ft", 0.0)
             or max(0.0, float(state.altitude_ft or 0.0) - elevation))
    return above <= NEAR_FIELD_AGL_FT


def merge_weather(sim: Weather | None, real: WeatherSample,
                  surface: bool = True, wind: bool = True) -> WeatherSample:
    """Prefer the simulator's conditions, filling gaps from the observation.

    The sim knows wind and pressure exactly, because that is what the aircraft
    is flying in. It does not report cloud layers in a form worth reading on
    an ATIS, so those come from the observation.

    ``surface=False`` is an aeroplane in the air near the field: its wind is
    close enough to the field's, its temperature is not -- two degrees a
    thousand feet is a new ATIS letter on every circuit -- so the thermometer
    stays the observation's while there is one.

    ``wind=False`` is the same for the wind: on a short final at 1500 ft the
    simulator's wind is the wind at 1500 ft, which the landing clearance read
    out as the surface wind. The observation's is kept until the aeroplane is
    on the ground or below :data:`SURFACE_WIND_AGL_FT`.
    """
    if sim is None:
        return real
    temperature, dewpoint = sim.temperature_c, sim.dewpoint_c
    if not surface and real.source != "default":
        temperature = real.weather.temperature_c
        dewpoint = real.weather.dewpoint_c
    use_sim_wind = wind or real.source == "default"
    merged = Weather(
        wind_dir=sim.wind_dir if use_sim_wind else real.weather.wind_dir,
        wind_kt=sim.wind_kt if use_sim_wind else real.weather.wind_kt,
        gust_kt=(sim.gust_kt or real.weather.gust_kt) if use_sim_wind
        else real.weather.gust_kt,
        variable=False if use_sim_wind else real.weather.variable,
        visibility_sm=sim.visibility_sm,
        altimeter_inhg=sim.altimeter_inhg,
        qnh_hpa=sim.qnh_hpa,
        temperature_c=temperature,
        dewpoint_c=dewpoint,
        clouds=real.weather.clouds,
        raw=real.weather.raw,
    )
    return WeatherSample(merged, "sim", real.station, real.observed_utc)


def flight_category(wx: Weather) -> str:
    """VFR / MVFR / IFR / LIFR, from ceiling and visibility."""
    ceiling = 99999
    for cover, height in wx.clouds:
        if cover in ("BKN", "OVC", "VV"):
            ceiling = min(ceiling, height)
    vis = wx.visibility_sm
    if ceiling < 500 or vis < 1:
        return "LIFR"
    if ceiling < 1000 or vis < 3:
        return "IFR"
    if ceiling <= 3000 or vis <= 5:
        return "MVFR"
    return "VFR"
