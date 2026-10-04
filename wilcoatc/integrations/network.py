"""Who is really on the frequency: VATSIM and IVAO.

The point of this program is to put a controller on frequencies that have
nobody on them. On VATSIM and IVAO some of them do have somebody, and talking
over a real person is the one thing it must never do -- so before it answers,
it asks whether a human is working this frequency here, and if one is it says
nothing at all.

That is the whole rule, and it is deliberately the *frequency* and not the
callsign that decides it. Matching "EGLL_TWR" to Heathrow Tower by parsing the
callsign works until somebody logs on as "EGLL_1_TWR", or as "LON_S_CTR" for a
sector that covers half of England. What is not ambiguous is that a controller
is transmitting on 118.500 and is within range of the aeroplane, which is the
same test a radio makes.

What the online controller publishes is used as well as obeyed. Their ATIS is
the real one for that field -- the runway in use, the transition level, the
information letter -- and reading a generated one over the top of it would put
two different truths on the same aerodrome.

Both feeds are public, anonymous, and polled on a long interval:

  * https://data.vatsim.net/v3/vatsim-data.json
  * https://api.ivao.aero/v2/tracker/whazzup

Neither is required. With ``network.provider`` at ``none`` -- the default --
nothing here runs and nothing is fetched.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

VATSIM_URL = "https://data.vatsim.net/v3/vatsim-data.json"
IVAO_URL = "https://api.ivao.aero/v2/tracker/whazzup"

TIMEOUT_S = 15.0

#: How close two frequencies have to be to be the same one. Both networks
#: publish to the kilohertz; a 25 kHz radio and an 8.33 kHz one disagree in the
#: third decimal, which is the same slack the channel resolver allows.
SAME_FREQUENCY_MHZ = 0.006

#: How far a controller reaches when the feed does not say. VATSIM publishes a
#: visual range per controller; IVAO does not, so the position's own ordinary
#: reach is used instead.
DEFAULT_RANGE_NM = 60.0

# VATSIM numbers its facility types; IVAO names them. Both end up as the
# position codes the rest of this program uses.
_VATSIM_FACILITY = {
    0: "",          # observer, not working a position
    1: "INFO",      # flight service
    2: "DEL",
    3: "GND",
    4: "TWR",
    5: "APP",       # approach and departure are one position on the network
    6: "CTR",
}

_IVAO_POSITION = {
    "DEL": "DEL", "GND": "GND", "TWR": "TWR", "APP": "APP", "DEP": "DEP",
    "CTR": "CTR", "FSS": "INFO", "ATIS": "ATIS",
}


@dataclass(frozen=True)
class OnlineController:
    """One human working a position, as the network reports them."""

    callsign: str                 # EGLL_TWR, LON_S_CTR
    position: str                 # DEL / GND / TWR / APP / CTR / INFO / ATIS
    mhz: float
    name: str = ""                # the person, where the feed gives a name
    latitude: float = 0.0
    longitude: float = 0.0
    range_nm: float = DEFAULT_RANGE_NM
    atis: str = ""
    atis_letter: str = ""
    network: str = ""

    @property
    def ident(self) -> str:
        """The field in the callsign, which is a guess and is treated as one.

        "EGLL_TWR" is Heathrow and "EGLL_N_APP" is Heathrow too, but "LON_CTR"
        is not an airport at all. Used only for looking up an ATIS, never for
        deciding who is on a frequency.
        """
        head = (self.callsign or "").split("_")[0].upper()
        # Four letters, which is what an ICAO aerodrome code is and what both
        # networks use for a callsign that belongs to a field. "LON_CTR" is a
        # sector covering half of England and "LON" is not an airport, so a
        # shorter prefix is treated as naming nothing rather than as naming
        # whatever happens to share those letters.
        return head if len(head) == 4 and head.isalnum() else ""

    @property
    def positioned(self) -> bool:
        return self.latitude != 0.0 or self.longitude != 0.0

    def describe(self) -> str:
        who = f" ({self.name})" if self.name else ""
        return f"{self.callsign}{who} on {self.network.upper()}"


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _joined(lines: Any) -> str:
    if isinstance(lines, str):
        return lines.strip()
    if isinstance(lines, (list, tuple)):
        return " ".join(str(line).strip() for line in lines if line).strip()
    return ""


def parse_vatsim(payload: dict) -> list[OnlineController]:
    """Controllers and ATIS positions from the VATSIM data feed."""
    found: list[OnlineController] = []
    for entry in list(payload.get("controllers") or []):
        position = _VATSIM_FACILITY.get(entry.get("facility"), "")
        mhz = _as_float(entry.get("frequency"))
        # An observer has no position and logs on with a placeholder frequency
        # in the 199 range, which no radio can select.
        if not position or not 108.0 <= mhz <= 137.0:
            continue
        found.append(OnlineController(
            callsign=str(entry.get("callsign") or ""),
            position=position,
            mhz=round(mhz, 3),
            name=str(entry.get("name") or ""),
            range_nm=_as_float(entry.get("visual_range")) or DEFAULT_RANGE_NM,
            atis=_joined(entry.get("text_atis")),
            network="vatsim",
        ))

    for entry in list(payload.get("atis") or []):
        mhz = _as_float(entry.get("frequency"))
        if not 108.0 <= mhz <= 137.0:
            continue
        found.append(OnlineController(
            callsign=str(entry.get("callsign") or ""),
            position="ATIS",
            mhz=round(mhz, 3),
            name=str(entry.get("name") or ""),
            range_nm=_as_float(entry.get("visual_range")) or DEFAULT_RANGE_NM,
            atis=_joined(entry.get("text_atis")),
            atis_letter=str(entry.get("atis_code") or "").strip().upper()[:1],
            network="vatsim",
        ))
    return found


def parse_ivao(payload: dict) -> list[OnlineController]:
    """Controllers from the IVAO whazzup feed."""
    clients = payload.get("clients") or {}
    found: list[OnlineController] = []
    for entry in list(clients.get("atcs") or []):
        session = entry.get("atcSession") or {}
        position = _IVAO_POSITION.get(
            str(session.get("position") or "").upper(), "")
        mhz = _as_float(session.get("frequency"))
        if not position or not 108.0 <= mhz <= 137.0:
            continue
        track = entry.get("lastTrack") or {}
        atis = entry.get("atis") or {}
        found.append(OnlineController(
            callsign=str(entry.get("callsign") or ""),
            position=position,
            mhz=round(mhz, 3),
            name="",  # the feed carries a numeric member id, not a name
            latitude=_as_float(track.get("latitude")),
            longitude=_as_float(track.get("longitude")),
            range_nm=DEFAULT_RANGE_NM,
            atis=_joined(atis.get("lines")),
            atis_letter=str(atis.get("revision") or "").strip().upper()[:1],
            network="ivao",
        ))
    return found


PROVIDERS: dict[str, tuple[str, Callable[[dict], list[OnlineController]]]] = {
    "vatsim": (VATSIM_URL, parse_vatsim),
    "ivao": (IVAO_URL, parse_ivao),
}


class OnlineNetwork:
    """Who is on the network, refreshed in the background.

    Nothing here ever blocks a transmission. The fetch runs on its own thread
    and the answer to "is anybody on this frequency" is whatever the last
    successful poll said -- which is the right trade: a controller who logged
    on four minutes ago and is not yet known about is a controller this
    program will talk over once, and a radio that stutters while an HTTP
    request times out is one nobody will use.
    """

    def __init__(self, provider: str = "none", refresh_s: float = 120.0,
                 opener=urllib.request.urlopen):
        self.provider = (provider or "none").strip().lower()
        self.refresh_s = max(30.0, float(refresh_s or 120.0))
        self._opener = opener
        self._controllers: list[OnlineController] = []
        self._fetched_at = 0.0
        self._started = 0.0
        self._error = ""
        self._lock = threading.Lock()
        self._busy = False

    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.provider in PROVIDERS

    @property
    def controllers(self) -> list[OnlineController]:
        with self._lock:
            return list(self._controllers)

    @property
    def error(self) -> str:
        with self._lock:
            return self._error

    @property
    def age_s(self) -> float:
        """How old the picture is, or zero when there has never been one."""
        return time.monotonic() - self._fetched_at if self._fetched_at else 0.0

    @property
    def known(self) -> bool:
        """Whether anything has been heard from the network at all."""
        return self._fetched_at > 0.0

    # ------------------------------------------------------------------

    def poll(self) -> None:
        """Refresh if the picture is stale. Returns at once either way."""
        if not self.enabled:
            return
        now = time.monotonic()
        if self._busy:
            return
        if self._fetched_at and now - self._started < self.refresh_s:
            return
        self._started = now
        self._busy = True
        threading.Thread(target=self._fetch, name="WilcoATC-network",
                         daemon=True).start()

    def refresh(self) -> list[OnlineController]:
        """Fetch now, on this thread. For the command line and the tests."""
        self._fetch()
        return self.controllers

    def _fetch(self) -> None:
        url, parse = PROVIDERS.get(self.provider, (None, None))
        try:
            if url is None:
                return
            request = urllib.request.Request(
                url, headers={"User-Agent": "WilcoATC"})
            with self._opener(request, timeout=TIMEOUT_S) as response:
                payload = json.loads(response.read().decode("utf-8"))
            found = parse(payload)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            with self._lock:
                self._error = str(exc)
            log.debug("could not read %s", self.provider, exc_info=True)
            return
        except Exception as exc:            # a feed that changed shape
            with self._lock:
                self._error = str(exc)
            log.debug("could not parse %s", self.provider, exc_info=True)
            return
        finally:
            self._busy = False

        with self._lock:
            self._controllers = found
            self._fetched_at = time.monotonic()
            self._error = ""

    # ------------------------------------------------------------------
    # the questions the engine asks
    # ------------------------------------------------------------------

    def on_frequency(self, mhz: float, latitude: float = 0.0,
                     longitude: float = 0.0) -> OnlineController | None:
        """The human working this frequency where the aeroplane is, if any.

        Decided by the frequency and the distance, not by the callsign: a
        controller transmitting on 118.500 within range of the aeroplane is
        somebody the radio would hear, whatever they logged on as.
        """
        if not self.enabled or not mhz:
            return None
        best: OnlineController | None = None
        best_range = 0.0
        for controller in self.controllers:
            if abs(controller.mhz - float(mhz)) > SAME_FREQUENCY_MHZ:
                continue
            if not self._in_range(controller, latitude, longitude):
                continue
            # The closest-reaching one wins, which is the tower rather than the
            # centre when both are somehow on the same number.
            if best is None or controller.range_nm < best_range:
                best, best_range = controller, controller.range_nm
        return best

    def _in_range(self, controller: OnlineController, latitude: float,
                  longitude: float) -> bool:
        """Whether the aeroplane is inside this controller's reach.

        A controller the feed gives no position for is taken to be in range:
        IVAO does not publish one for every session, and refusing to believe in
        a controller because the feed is thin would put this program on air on
        top of them.
        """
        if not controller.positioned or not (latitude or longitude):
            return True
        from ..navdata.build import haversine_nm

        away = haversine_nm(latitude, longitude,
                            controller.latitude, controller.longitude)
        return away <= max(controller.range_nm, 20.0)

    def working(self, ident: str, position: str) -> OnlineController | None:
        """Whether a human is working this position at this field."""
        if not self.enabled or not ident:
            return None
        wanted = (ident or "").upper()
        for controller in self.controllers:
            if controller.position != position:
                continue
            if controller.ident == wanted:
                return controller
        return None

    def atis_for(self, ident: str) -> OnlineController | None:
        """The online ATIS for a field, where somebody is broadcasting one."""
        if not self.enabled or not ident:
            return None
        wanted = (ident or "").upper()
        for controller in self.controllers:
            if controller.position == "ATIS" and controller.ident == wanted \
                    and controller.atis:
                return controller
        return None

    def at(self, ident: str) -> list[OnlineController]:
        """Everybody online whose callsign names this field."""
        if not self.enabled or not ident:
            return []
        wanted = (ident or "").upper()
        return [c for c in self.controllers if c.ident == wanted]


__all__ = [
    "OnlineController", "OnlineNetwork", "parse_vatsim", "parse_ivao",
    "PROVIDERS", "VATSIM_URL", "IVAO_URL", "SAME_FREQUENCY_MHZ",
]
