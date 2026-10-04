"""Where aircraft state comes from.

Three sources implement the same interface:

* :class:`SimConnectSource` talks to Microsoft Flight Simulator. It is the real
  one, and it is deliberately forgiving: MSFS variables change names between
  versions, wrappers disagree about units, and a variable that is missing must
  degrade to a sensible default rather than crash a flight in progress.

* :class:`ManualSource` is driven by the operator, for testing the whole ATC
  stack -- voices, frequencies, phraseology -- with no simulator installed.

* :class:`ReplaySource` plays back a recorded flight, which is what makes the
  test suite able to check a whole departure without a human at the controls.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Iterable, Protocol

from .state import AircraftState, normalize_angle

log = logging.getLogger(__name__)


class StateSource(Protocol):
    """Anything that can report the current aircraft state."""

    def connect(self) -> bool: ...
    def read(self) -> AircraftState: ...
    def close(self) -> None: ...
    @property
    def connected(self) -> bool: ...


class RadioWriter(Protocol):
    """A source the radios can also be set *through*.

    Reading the aeroplane is not enough once the panel is the thing you tune
    from. A frequency picked on the Frequencies screen has to end up in the
    aircraft's radio, or the pilot is talking to a controller their own COM1
    has never heard of -- and the moment they touch the radio stack in the
    cockpit, whatever they had picked here is gone.
    """

    def set_com(self, mhz: float, radio: int = 1,
                standby: bool = False) -> bool: ...
    def swap_com(self, radio: int = 1) -> bool: ...
    def pushback(self) -> bool: ...
    def set_squawk(self, code: str) -> bool: ...


# --------------------------------------------------------------------------
# frequency decoding
# --------------------------------------------------------------------------


def decode_frequency(raw) -> float:
    """Turn whatever SimConnect returned into MHz.

    The same variable comes back as MHz, kHz, Hz or packed BCD depending on the
    unit the wrapper asked for and the simulator version, and all four are
    plausible-looking numbers. Each is recognised by its magnitude, and BCD by
    the fact that its nibbles decode to a valid airband channel.
    """
    if raw is None:
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0:
        return 0.0

    # Already MHz.
    if 118.0 <= value <= 136.999:
        return round(value, 3)
    # kHz, e.g. 121900.
    if 118000.0 <= value <= 136999.0:
        return round(value / 1000.0, 3)
    # Hz.
    if 118_000_000.0 <= value <= 136_999_999.0:
        return round(value / 1_000_000.0, 3)

    # BCD16: 0x2190 means 121.90, with the leading "1" implied.
    packed = int(value)
    if 0 < packed <= 0xFFFF:
        digits = []
        for shift in (12, 8, 4, 0):
            nibble = (packed >> shift) & 0xF
            if nibble > 9:
                digits = []
                break
            digits.append(str(nibble))
        if digits:
            candidate = float(f"1{digits[0]}{digits[1]}.{digits[2]}{digits[3]}")
            if 118.0 <= candidate <= 136.999:
                return round(candidate, 3)
    # BCD32 used for 8.33 kHz channels.
    if 0 < packed <= 0xFFFFFFFF:
        digits = []
        for shift in (28, 24, 20, 16, 12, 8, 4, 0):
            nibble = (packed >> shift) & 0xF
            if nibble > 9:
                digits = []
                break
            digits.append(str(nibble))
        if len(digits) == 8:
            candidate_text = f"{digits[2]}{digits[3]}{digits[4]}.{digits[5]}{digits[6]}{digits[7]}"
            try:
                candidate = float(candidate_text)
            except ValueError:
                candidate = 0.0
            if 118.0 <= candidate <= 136.999:
                return round(candidate, 3)
    return 0.0


def encode_bcd16(mhz: float) -> int:
    """A COM frequency as the packed BCD the older SimConnect events want.

    ``COM_RADIO_SET`` reads its parameter as four BCD nibbles with the leading
    ``1`` implied: 121.900 goes in as 0x2190. It is a 25 kHz world -- the last
    nibble only has room for one digit -- so an 8.33 channel has to go through
    the Hz event instead, and :func:`encode_hz` is what that one takes.
    """
    channel = round(float(mhz) * 1000.0)          # kHz
    digits = f"{channel:06d}"                     # e.g. "121900"
    if not digits.startswith("1"):
        return 0
    packed = 0
    for digit in digits[1:5]:                     # "2190"
        packed = (packed << 4) | int(digit)
    return packed


def encode_hz(mhz: float) -> int:
    """A COM frequency in whole Hz, for ``COM_RADIO_SET_HZ``.

    The newer event, and the only one that can express an 8.33 kHz channel.
    Tried first for that reason; the BCD one is the fallback for a simulator
    or an aircraft that does not have it.
    """
    return int(round(float(mhz) * 1_000_000.0))


def encode_squawk(code: str) -> int:
    """A transponder code as the BCD ``XPNDR_SET`` wants."""
    text = "".join(c for c in str(code) if c.isdigit())[:4].rjust(4, "0")
    if any(c not in "01234567" for c in text):
        return 0
    packed = 0
    for digit in text:
        packed = (packed << 4) | int(digit)
    return packed


def decode_squawk(raw) -> str:
    """Transponder code, from BCD or a plain integer."""
    if raw is None:
        return "1200"
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return "1200"
    if 0 <= value <= 7777:
        text = f"{value:04d}"
        if all(c in "01234567" for c in text):
            return text
    digits = []
    for shift in (12, 8, 4, 0):
        nibble = (value >> shift) & 0xF
        if nibble > 7:
            return "1200"
        digits.append(str(nibble))
    return "".join(digits)


def _text(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace").strip("\x00 ").strip()
    return str(raw).strip("\x00 ").strip()


# --------------------------------------------------------------------------
# SimConnect
# --------------------------------------------------------------------------

# Aircraft title fragments mapped to ICAO type designators. The sim reports a
# marketing title ("Boeing 737 MAX 8 Asobo"), not a type code, and the type is
# needed for the wake-turbulence suffix and the GA manufacturer callsign.
TITLE_TO_TYPE: tuple[tuple[str, str], ...] = (
    ("737 max 8", "B38M"), ("737 max", "B38M"), ("737-800", "B738"),
    ("737-700", "B737"), ("737-900", "B739"), ("737", "B738"),
    ("747-8", "B748"), ("747", "B744"),
    ("757", "B752"), ("767", "B763"),
    ("777-300", "B77W"), ("777-200", "B772"), ("777", "B77W"),
    ("787-10", "B78X"), ("787-9", "B789"), ("787-8", "B788"), ("787", "B789"),
    ("a319", "A319"), ("a320neo", "A20N"), ("a320", "A320"), ("a321neo", "A21N"),
    ("a321", "A321"), ("a330", "A333"), ("a340", "A343"), ("a350", "A359"),
    ("a380", "A388"),
    ("crj 700", "CRJ7"), ("crj 900", "CRJ9"), ("crj 550", "CRJ5"), ("crj", "CRJ7"),
    ("e170", "E170"), ("e175", "E75L"), ("e190", "E190"), ("e195", "E195"),
    ("embraer 175", "E75L"), ("embraer 190", "E190"),
    ("atr 42", "AT43"), ("atr 72", "AT76"), ("atr", "AT76"),
    ("dash 8", "DH8D"), ("q400", "DH8D"),
    ("cessna 152", "C152"), ("cessna 172", "C172"), ("152", "C152"),
    ("172", "C172"), ("182", "C182"), ("208", "C208"), ("caravan", "C208"),
    ("citation cj4", "C25C"), ("citation longitude", "C700"), ("citation", "C25C"),
    ("cirrus sr22", "SR22"), ("sr22", "SR22"), ("sr20", "SR20"),
    ("tbm 930", "TBM9"), ("tbm", "TBM9"),
    ("pilatus pc-12", "PC12"), ("pc-12", "PC12"), ("pc-6", "PC6T"),
    ("king air", "B350"), ("baron", "BE58"), ("bonanza", "BE36"),
    ("da40", "DA40"), ("da62", "DA62"), ("diamond da40", "DA40"),
    ("bonanza g36", "BE36"), ("longitude", "C700"),
    ("cub", "PA18"), ("piper", "P28A"), ("archer", "P28A"), ("arrow", "P28R"),
    ("mooney", "M20P"), ("husky", "BL8"), ("icon a5", "A5"),
    ("vision jet", "SF50"), ("sf50", "SF50"),
    ("global 7500", "GL7T"), ("longitude", "C700"),
    ("h125", "AS50"), ("cabri", "CBRI"), ("h145", "EC45"),
)


def type_from_title(title: str, model: str = "") -> str:
    """Best-effort ICAO type designator from the sim's aircraft title."""
    text = f"{title} {model}".lower()
    for fragment, code in TITLE_TO_TYPE:
        if fragment in text:
            return code
    return ""


class SimConnectSource:
    """Reads aircraft state from Microsoft Flight Simulator over SimConnect."""

    # Variable name, attribute, the unit the wrapper actually returns, and how
    # often it is worth asking.
    #
    # The units are declared here rather than guessed from the value, because
    # guessing is not safe. SimConnect's native unit for a heading is radians
    # and for a position is degrees, and a longitude of 2.38 (Paris) is a
    # perfectly plausible radian value as well as a real longitude -- a
    # heuristic that converts it puts the aircraft in the Pacific. Every unit
    # below is taken from the wrapper's own request table.
    #
    #   deg    degrees already, only wrapped into 0-360
    #   rad    radians, converted to degrees
    #   raw    a number used as-is (feet, knots, feet/minute, millibars ...)
    #
    # The last column is the tier, and it is what makes the panel keep up.
    # The wrapper does one blocking round trip per variable -- it clears the
    # cached value, sends a request, and sleeps until the answer arrives --
    # so asking for all thirty-five costs about half a second, and a
    # situation loop that wants to run four times a second instead ran twice
    # a second with two seconds of lag behind it.
    #
    # So only the things that actually move are asked for every time. The
    # aircraft's title does not change four times a second; neither does the
    # parking brake, the ambient temperature or the flight number. Those are
    # rotated a few per pass, which keeps every one of them within a second
    # or so of the truth at a fraction of the cost.
    #
    #   fast   every read: position, motion, and the radio you are on
    #   slow   a few per read, in rotation
    REQUESTS: tuple[tuple[str, str, str, str], ...] = (
        ("PLANE_LATITUDE", "latitude", "raw", "fast"),            # Degrees
        ("PLANE_LONGITUDE", "longitude", "raw", "fast"),          # Degrees
        ("PLANE_ALTITUDE", "altitude_ft", "raw", "fast"),         # Feet
        ("INDICATED_ALTITUDE", "indicated_altitude_ft", "raw", "slow"),
        ("PLANE_ALT_ABOVE_GROUND", "altitude_agl_ft", "raw", "fast"),
        ("PLANE_HEADING_DEGREES_TRUE", "heading_true", "rad", "fast"),
        ("PLANE_HEADING_DEGREES_MAGNETIC", "heading_magnetic", "rad", "slow"),
        ("GPS_GROUND_TRUE_TRACK", "track_true", "rad", "slow"),
        ("GROUND_VELOCITY", "ground_speed_kt", "raw", "fast"),    # Knots
        ("AIRSPEED_INDICATED", "indicated_airspeed_kt", "raw", "fast"),
        ("AIRSPEED_TRUE", "true_airspeed_kt", "raw", "slow"),
        ("VERTICAL_SPEED", "vertical_speed_fpm", "raw", "fast"),  # feet/minute
        ("SIM_ON_GROUND", "on_ground", "bool", "fast"),
        ("COM_ACTIVE_FREQUENCY:1", "com1_active", "freq", "fast"),    # MHz
        ("COM_STANDBY_FREQUENCY:1", "com1_standby", "freq", "slow"),
        ("COM_ACTIVE_FREQUENCY:2", "com2_active", "freq", "slow"),
        ("COM_STANDBY_FREQUENCY:2", "com2_standby", "freq", "slow"),
        ("COM_RECEIVE_ALL", "com2_receive", "bool", "slow"),
        ("TRANSPONDER_CODE:1", "transponder_code", "squawk", "slow"),  # BCO16
        ("ATC_ID", "callsign", "text", "slow"),
        ("ATC_AIRLINE", "airline", "text", "slow"),
        ("ATC_FLIGHT_NUMBER", "flight_number", "text", "slow"),
        ("ATC_MODEL", "type_code", "text", "slow"),
        ("TITLE", "title", "text", "slow"),
        ("GENERAL_ENG_COMBUSTION:1", "engines_running", "bool", "slow"),
        ("BRAKE_PARKING_POSITION", "parking_brake", "bool", "slow"),
        ("GEAR_HANDLE_POSITION", "gear_down", "bool", "slow"),
        ("FLAPS_HANDLE_INDEX", "flaps_index", "int", "slow"),
        # For the crew rather than for the controller. Both are standard
        # variables present on nearly every aircraft; one that does not model
        # a seatbelt sign simply reports it off for the whole flight, which
        # is what wilcoatc.atc.crew allows for.
        ("CABIN_SEATBELTS_ALERT_SWITCH", "seatbelt_sign", "bool", "slow"),
        ("LIGHT_BEACON", "beacon_light", "bool", "slow"),
        ("AMBIENT_WIND_DIRECTION", "ambient_wind_direction", "deg", "slow"),
        ("AMBIENT_WIND_VELOCITY", "ambient_wind_velocity", "raw", "slow"),  # Knots
        ("AMBIENT_TEMPERATURE", "ambient_temperature_c", "raw", "slow"),    # Celsius
        ("AMBIENT_VISIBILITY", "ambient_visibility_m", "raw", "slow"),      # Meters
        ("SEA_LEVEL_PRESSURE", "sea_level_pressure_hpa", "raw", "slow"),    # Millibars
    )

    def __init__(self, poll_interval: float = 0.25, request_timeout_ms: int = 400):
        self.poll_interval = poll_interval
        self.request_timeout_ms = request_timeout_ms
        self._sim = None
        self._requests = None
        self._connected = False
        self._missing: set[str] = set()
        self._state = AircraftState()
        self._lock = threading.Lock()
        # Events are mapped to the simulator once each and kept. Mapping is a
        # round trip, and tuning a radio should not pay for one.
        self._events: dict[bytes, object] = {}
        # The two tiers, split once rather than on every read.
        self._fast = tuple((n, a, k) for n, a, k, tier in self.REQUESTS
                           if tier == "fast")
        self._slow = tuple((n, a, k) for n, a, k, tier in self.REQUESTS
                           if tier != "fast")
        self._next_slow = 0
        # Whether the next read takes every variable rather than a slice.
        self._full_sweep = True
        # The latest value of every variable, whichever tier it came from, so
        # a read that only refreshed some of them still returns a whole
        # aircraft.
        self._values: dict[str, object] = {}
        self._empty_reads = 0
        # Whether the simulator has ever told us the flight was running. The
        # wrapper's flag starts False and only turns True on a SimStart
        # event, so connecting to a sim that is already flying leaves it
        # False for ever -- which means "not running" is only evidence of
        # anything once it has been True at least once.
        self._seen_running = False
        # Which of the two ways of setting a frequency works, per radio and
        # per active/standby. Not one answer for the whole simulator: MSFS
        # 2024 takes COM_RADIO_SET_HZ for the active frequency and does not
        # recognise COM_STBY_RADIO_SET_HZ at all, so a single flag learned
        # from the active radio silently breaks the standby one.
        self._freq_event_style: dict[tuple[int, bool], str] = {}

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> bool:
        try:
            from SimConnect import AircraftRequests, SimConnect
        except ImportError:
            log.warning("SimConnect package not installed; sim link unavailable")
            return False
        try:
            self._sim = SimConnect()
            self._requests = AircraftRequests(self._sim, _time=self.request_timeout_ms)
            self._connected = True
            # A reconnection is to a different simulator session: the event
            # handles from the last one are stale, and a variable that was
            # missing from the last aircraft may be present in this one.
            self._events.clear()
            self._freq_event_style.clear()
            self._missing.clear()
            self._values.clear()
            self._empty_reads = 0
            self._next_slow = 0
            self._full_sweep = True
            self._seen_running = False
            log.info("connected to the simulator")
            return True
        except Exception as exc:
            log.info("simulator not available: %s", exc)
            self._sim = None
            self._requests = None
            self._connected = False
            return False

    # ------------------------------------------------------------------

    def _get(self, name: str):
        if self._requests is None or name in self._missing:
            return None
        try:
            return self._requests.get(name)
        except Exception as exc:
            # A variable that is not present in this sim version should be
            # asked for once, not every quarter second for the whole flight.
            self._missing.add(name)
            log.debug("simvar %s unavailable: %s", name, exc)
            return None

    # How many of the slowly-changing variables are refreshed per read. Four
    # covers the whole set in six passes, which at a quarter-second interval
    # is a second and a half -- well inside the time it takes a parking brake
    # or an aircraft title to matter.
    SLOW_PER_READ = 4

    def read(self) -> AircraftState:
        if not self._connected:
            return AircraftState(connected=False, source="simconnect")

        # Everything that moves, every time.
        fresh = 0
        for name, attribute, kind in self._fast:
            if self._fetch(name, attribute, kind):
                fresh += 1

        # And everything else. The first read after connecting takes the lot,
        # because the flight is built from it: the callsign, the aircraft
        # type and the transponder are all in the slow tier, and a session
        # started from a state that has not got round to them yet is a
        # session with no aeroplane in it. After that they rotate.
        if self._full_sweep:
            for name, attribute, kind in self._slow:
                self._fetch(name, attribute, kind)
            self._full_sweep = False
        elif self._slow:
            for _ in range(min(self.SLOW_PER_READ, len(self._slow))):
                name, attribute, kind = self._slow[self._next_slow]
                self._next_slow = (self._next_slow + 1) % len(self._slow)
                self._fetch(name, attribute, kind)

        if not fresh and not self._values:
            # The connection object exists but the sim has gone away.
            self._connected = False
            return AircraftState(connected=False, source="simconnect")
        if not fresh:
            # Nothing that moves came back. One empty pass is a hiccup; a run
            # of them is the simulator having gone.
            self._empty_reads += 1
            if self._empty_reads >= self.EMPTY_READS_BEFORE_GONE:
                self._connected = False
                return AircraftState(connected=False, source="simconnect")
        else:
            self._empty_reads = 0

        state = AircraftState(connected=True, source="simconnect")
        for attribute, value in self._values.items():
            setattr(state, attribute, value)
        state.paused = self._is_paused()
        self._derive(state)
        with self._lock:
            self._state = state
        return state

    # How many reads in a row may come back with nothing moving before the
    # simulator is taken to have gone. A single empty pass happens: the sim
    # pauses, a menu opens, a variable is momentarily unavailable.
    EMPTY_READS_BEFORE_GONE = 8

    def _is_paused(self) -> bool:
        """Whether the flight is stopped, or the user is in the menus.

        Read from the two system events SimConnect actually sends -- Paused
        and SimStop, the latter documented as firing when the user is
        navigating the interface -- rather than from a camera-state number,
        because those are renumbered between simulator versions and a magic
        number that means "menu" in one release means a cockpit view in the
        next.

        Deliberately one-sided: it answers no unless there is positive
        evidence. A false yes silences the radio and freezes the flight,
        which is far worse than the thing it is fixing.
        """
        sim = self._sim
        if sim is None:
            return False
        if getattr(sim, "paused", False):
            return True
        # SimStart has to have been seen before its absence means anything.
        if getattr(sim, "running", False):
            self._seen_running = True
            return False
        return self._seen_running

    def _fetch(self, name: str, attribute: str, kind: str) -> bool:
        """Read one variable and keep it. True if a value came back."""
        raw = self._get(name)
        if raw is None:
            return False
        try:
            self._values[attribute] = self._convert(raw, kind, attribute)
        except Exception:
            log.debug("could not convert %s=%r", name, raw, exc_info=True)
            return False
        return True

    @staticmethod
    def _convert(raw, kind: str, attribute: str):
        if kind == "text":
            return _text(raw)
        if kind == "freq":
            return decode_frequency(raw)
        if kind == "squawk":
            return decode_squawk(raw)
        if kind == "bool":
            return bool(float(raw))
        if kind == "int":
            return int(float(raw))
        if kind == "rad":
            return normalize_angle(float(raw), source_is_radians=True)
        if kind == "deg":
            return normalize_angle(float(raw), source_is_radians=False)
        return float(raw)

    @staticmethod
    def _derive(state: AircraftState) -> None:
        """Fill in what the sim does not report directly."""
        if not state.type_code or len(state.type_code) > 4:
            derived = type_from_title(state.title, state.type_code)
            if derived:
                state.type_code = derived
        state.type_code = (state.type_code or "").upper()

        # ATC_ID is the tail number; the airline callsign has to be rebuilt
        # from the airline and flight number when the aircraft is a liveried
        # airliner rather than a private registration.
        state.tail_number = state.callsign
        if state.airline and state.flight_number:
            state.callsign = _radio_callsign(
                state.callsign, state.airline, state.flight_number,
                state.type_code)

        if not state.track_true:
            state.track_true = state.heading_true
        if state.altitude_agl_ft <= 0 and state.on_ground:
            state.altitude_agl_ft = 0.0

    # ------------------------------------------------------------------
    # writing back: the panel is a radio too
    # ------------------------------------------------------------------

    def _event(self, name: bytes):
        """Map a SimConnect event once, and keep it.

        A name this simulator does not know comes back as None from the
        wrapper, which is remembered as None rather than asked for again --
        ``COM_RADIO_SET_HZ`` does not exist on every version and the fallback
        has to be decided once, not on every keystroke.
        """
        if name in self._events:
            return self._events[name]
        found = None
        if self._sim is not None:
            try:
                found = self._sim.map_to_sim_event(name)
            except Exception as exc:
                log.debug("could not map event %s: %s", name, exc)
        self._events[name] = found
        return found

    def _send(self, name: bytes, value: int) -> bool:
        event = self._event(name)
        if event is None or self._sim is None:
            return False
        try:
            from SimConnect.Enum import DWORD
        except Exception:                              # pragma: no cover
            try:
                from ctypes import c_ulong as DWORD
            except Exception:
                return False
        try:
            return bool(self._sim.send_event(event, DWORD(int(value) & 0xFFFFFFFF)))
        except Exception as exc:
            log.debug("event %s failed: %s", name, exc)
            return False

    # Which event sets which radio, in both the Hz and the BCD form.
    _COM_EVENTS: dict[tuple[int, bool], tuple[bytes, bytes]] = {
        (1, False): (b"COM_RADIO_SET_HZ", b"COM_RADIO_SET"),
        (1, True): (b"COM_STBY_RADIO_SET_HZ", b"COM_STBY_RADIO_SET"),
        (2, False): (b"COM2_RADIO_SET_HZ", b"COM2_RADIO_SET"),
        (2, True): (b"COM2_STBY_RADIO_SET_HZ", b"COM2_STBY_RADIO_SET"),
    }

    # The simulator variable each radio reads back from, so a write can be
    # checked rather than assumed.
    _COM_VARIABLES: dict[tuple[int, bool], str] = {
        (1, False): "COM_ACTIVE_FREQUENCY:1",
        (1, True): "COM_STANDBY_FREQUENCY:1",
        (2, False): "COM_ACTIVE_FREQUENCY:2",
        (2, True): "COM_STANDBY_FREQUENCY:2",
    }

    # How long to wait for a radio to show the frequency that was just sent
    # to it, while working out which event this simulator understands.
    CONFIRM_S = 1.2
    CONFIRM_STEP_S = 0.15

    def set_com(self, mhz: float, radio: int = 1, standby: bool = False) -> bool:
        """Put a frequency into the aircraft's radio.

        There are two events for this and no way to ask which one a
        simulator has. Sending an event it does not recognise is not an
        error: the call succeeds, the exception arrives later on a channel
        nothing here is reading, and the radio simply does not move. So the
        first write to each radio is *checked* -- sent, then read back -- and
        the encoding that actually moved the needle is remembered for the
        rest of the session.

        Per radio, and per active-versus-standby, because they genuinely
        differ: MSFS 2024 takes ``COM_RADIO_SET_HZ`` for the active frequency
        and does not know ``COM_STBY_RADIO_SET_HZ`` at all. One flag learned
        from the active radio is how the standby quietly stopped working.

        The Hz form is tried first wherever it is not yet known, because it
        is the only one that can express an 8.33 kHz channel; the packed BCD
        fallback is a 25 kHz event and rounds to the nearest channel it can
        say.
        """
        if not self._connected:
            return False
        try:
            value = float(mhz)
        except (TypeError, ValueError):
            return False
        if not (118.0 <= value <= 136.999):
            return False

        key = (int(radio), bool(standby))
        hz_event, bcd_event = self._COM_EVENTS.get(key, (b"", b""))
        if not hz_event:
            return False

        known = self._freq_event_style.get(key)
        if known == "hz":
            return self._send(hz_event, encode_hz(value))
        if known == "bcd":
            packed = encode_bcd16(value)
            return bool(packed) and self._send(bcd_event, packed)

        # Not known yet: try each and see which one the radio obeys.
        #
        # A radio that is already on the wanted frequency teaches nothing --
        # it reads back correctly whether or not the event did anything, and
        # believing that is how the standby radio came to be "learned" from a
        # write that never landed. So the probe is skipped, the event is sent
        # anyway because it costs nothing, and the next tune to a different
        # frequency is what settles the question.
        if self._reads(key, value):
            self._send(hz_event, encode_hz(value))
            return True

        if self._send(hz_event, encode_hz(value)) and self._took(key, value):
            self._freq_event_style[key] = "hz"
            return True
        packed = encode_bcd16(value)
        if packed and self._send(bcd_event, packed) and self._took(key, value):
            self._freq_event_style[key] = "bcd"
            return True
        return False

    def _reads(self, key: tuple[int, bool], wanted: float) -> bool:
        """Whether a radio is already showing a frequency."""
        name = self._COM_VARIABLES.get(key, "")
        if not name:
            return False
        found = decode_frequency(self._get(name))
        return bool(found) and abs(found - wanted) < 0.013

    def _took(self, key: tuple[int, bool], wanted: float) -> bool:
        """Whether the radio actually moved to the frequency just sent.

        The tolerance is a 25 kHz step, because the BCD event cannot express
        anything finer and a radio that rounded to the nearest channel it has
        did what it was asked.
        """
        name = self._COM_VARIABLES.get(key, "")
        if not name:
            return False
        deadline = time.monotonic() + self.CONFIRM_S
        while time.monotonic() < deadline:
            time.sleep(self.CONFIRM_STEP_S)
            found = decode_frequency(self._get(name))
            if found and abs(found - wanted) < 0.013:
                return True
        return False

    def pushback(self) -> bool:
        """Start the simulator's own pushback.

        One event, unchecked, unlike the radios: there is no variable that
        reads back "a tug is attached", so there is nothing to confirm it
        against. GSX takes this over when it is set to, which is why it is
        worth sending even at a stand GSX is working.
        """
        if not self._connected:
            return False
        return self._send(b"TOGGLE_PUSHBACK", 0)

    def swap_com(self, radio: int = 1) -> bool:
        """Swap active and standby, the way the button on the radio does."""
        if not self._connected:
            return False
        name = b"COM_STBY_RADIO_SWAP" if int(radio) == 1 else b"COM2_RADIO_SWAP"
        return self._send(name, 0)

    def set_squawk(self, code: str) -> bool:
        """Set the transponder, so an assigned code is dialled in for you."""
        if not self._connected:
            return False
        packed = encode_squawk(code)
        if not packed and str(code).strip("0"):
            return False
        return self._send(b"XPNDR_SET", packed)

    def close(self) -> None:
        self._connected = False
        self._events.clear()
        self._freq_event_style.clear()
        if self._sim is not None:
            try:
                self._sim.exit()
            except Exception:
                pass
            self._sim = None
            self._requests = None


# What an aeroplane calls itself, from what the simulator reports about it.
#
# The tables moved to :mod:`wilcoatc.atc.speech`, next to the telephony names
# they are the inverse of, because the user's aeroplane was not the only thing
# that needed them: every other aeroplane in the simulator was spelling its
# registration on the radio for want of exactly this.
def _radio_callsign(registration: str, airline: str, number: str,
                    aircraft_type: str = "") -> str:
    from ..atc.speech import radio_callsign

    return radio_callsign(registration, airline, number, aircraft_type)



# --------------------------------------------------------------------------
# offline sources
# --------------------------------------------------------------------------


class ManualSource:
    """A state the operator sets directly, for use without a simulator.

    Everything in the ATC stack -- frequency resolution, phraseology, voices,
    ATIS -- can be exercised through this, which is what makes the system
    testable and demonstrable on a machine with no sim installed.
    """

    def __init__(self, state: AircraftState | None = None):
        self._state = state or AircraftState(connected=True, source="manual")
        self._state.connected = True
        self._state.source = "manual"

    @property
    def connected(self) -> bool:
        return True

    def connect(self) -> bool:
        return True

    def read(self) -> AircraftState:
        self._state.timestamp = time.monotonic()
        # A copy, not the live object. The engine compares the previous read
        # with the current one to spot a takeoff or a landing, and handing back
        # the same mutable instance makes every comparison see itself and no
        # transition ever fire.
        return replace(self._state)

    def update(self, **fields) -> AircraftState:
        for key, value in fields.items():
            if hasattr(self._state, key):
                setattr(self._state, key, value)
            else:
                raise AttributeError(f"AircraftState has no field {key!r}")
        return self._state

    # The same writing surface the simulator has, so nothing above a source
    # has to ask which kind it is holding before it tunes a radio.

    def set_com(self, mhz: float, radio: int = 1, standby: bool = False) -> bool:
        try:
            value = round(float(mhz), 3)
        except (TypeError, ValueError):
            return False
        if not (118.0 <= value <= 136.999):
            return False
        field_name = f"com{int(radio)}_{'standby' if standby else 'active'}"
        if not hasattr(self._state, field_name):
            return False
        setattr(self._state, field_name, value)
        return True

    def swap_com(self, radio: int = 1) -> bool:
        active = f"com{int(radio)}_active"
        standby = f"com{int(radio)}_standby"
        if not hasattr(self._state, active):
            return False
        was = getattr(self._state, active)
        setattr(self._state, active, getattr(self._state, standby))
        setattr(self._state, standby, was)
        return True

    def set_squawk(self, code: str) -> bool:
        text = "".join(c for c in str(code) if c.isdigit())[:4]
        if len(text) != 4 or any(c not in "01234567" for c in text):
            return False
        self._state.transponder_code = text
        return True

    def pushback(self) -> bool:
        """No simulator, so no tug. Answered rather than raised, so that a
        controller working without one still finishes its sentence."""
        return False

    def close(self) -> None:
        return None


@dataclass
class ReplayFrame:
    at_s: float
    fields: dict


class ReplaySource:
    """Plays a scripted sequence of state changes against a wall clock."""

    def __init__(self, frames: Iterable[ReplayFrame], loop: bool = False,
                 speed: float = 1.0):
        self.frames = sorted(frames, key=lambda f: f.at_s)
        self.loop = loop
        self.speed = max(0.01, speed)
        self._state = AircraftState(connected=True, source="replay")
        self._started = time.monotonic()
        self._index = 0

    @property
    def connected(self) -> bool:
        return True

    def connect(self) -> bool:
        self._started = time.monotonic()
        self._index = 0
        return True

    def read(self) -> AircraftState:
        elapsed = (time.monotonic() - self._started) * self.speed
        while self._index < len(self.frames) and self.frames[self._index].at_s <= elapsed:
            for key, value in self.frames[self._index].fields.items():
                if hasattr(self._state, key):
                    setattr(self._state, key, value)
            self._index += 1
        if self.loop and self._index >= len(self.frames) and self.frames:
            self._started = time.monotonic()
            self._index = 0
        self._state.timestamp = time.monotonic()
        return replace(self._state)

    def close(self) -> None:
        return None


def open_source(prefer_sim: bool = True, poll_interval: float = 0.25):
    """Connect to the simulator if it is running, otherwise fall back."""
    if prefer_sim:
        source = SimConnectSource(poll_interval=poll_interval)
        if source.connect():
            return source
    return ManualSource()
