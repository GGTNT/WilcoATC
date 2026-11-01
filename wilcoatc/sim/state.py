"""The aircraft state the controller reasons about."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field


@dataclass
class AircraftState:
    """One snapshot of the aircraft, in the units ATC thinks in.

    Angles are degrees, altitudes feet, speeds knots. Whatever the source
    reports internally is converted before it gets here, so nothing downstream
    has to know that SimConnect prefers radians.
    """

    # --- position ---
    latitude: float = 0.0
    longitude: float = 0.0
    altitude_ft: float = 0.0            # true altitude, MSL
    indicated_altitude_ft: float = 0.0  # what the altimeter reads
    altitude_agl_ft: float = 0.0
    heading_true: float = 0.0
    heading_magnetic: float = 0.0
    track_true: float = 0.0

    # --- motion ---
    ground_speed_kt: float = 0.0
    indicated_airspeed_kt: float = 0.0
    true_airspeed_kt: float = 0.0
    vertical_speed_fpm: float = 0.0
    on_ground: bool = True

    # --- radios ---
    com1_active: float = 0.0
    com1_standby: float = 0.0
    com2_active: float = 0.0
    com2_standby: float = 0.0
    com1_receive: bool = True
    com1_transmit: bool = True
    com2_receive: bool = False
    transponder_code: str = "1200"
    transponder_mode: int = 0           # 0 off, 1 standby, 3 on, 4 alt, 5 ident

    # --- identity ---
    callsign: str = ""                  # ATC ID, e.g. DAL1234 or N172SP
    airline: str = ""
    flight_number: str = ""
    tail_number: str = ""
    type_code: str = ""                 # ICAO type, e.g. B739
    title: str = ""                     # sim's own aircraft title

    # --- configuration ---
    engines_running: bool = False
    parking_brake: bool = False
    gear_down: bool = True
    flaps_index: int = 0
    # The two switches the crew talk about. Nothing on the radio needs them --
    # a controller never mentions the seatbelt sign -- but the first officer
    # and the cabin do, and saying "the captain has switched off the seatbelt
    # sign" while it is still on is the loudest way this program can show it
    # is reading a script rather than the aeroplane.
    seatbelt_sign: bool = False
    beacon_light: bool = False
    # What kind of aeroplane the model is, for the first officer's
    # checklists (wilcoatc.atc.aircraft_class): ENGINE TYPE (-1 until the
    # simulator has said), NUMBER OF ENGINES and CATEGORY.
    engine_type: int = -1
    engine_count: int = 0
    category: str = ""
    # What a checklist answer is checked against (wilcoatc.atc.checklist).
    # A default here is not evidence: the first officer reads a variable
    # again on its own before objecting, and says nothing if it has no
    # answer.
    landing_light: bool = False
    taxi_light: bool = False
    nav_light: bool = False
    strobe_light: bool = False
    kohlsman_hpa: float = 0.0
    # FUEL TANK SELECTOR:1, the simulator's tank enumeration (0 off, 1 all,
    # 2 left, 3 right, ... 16 both).
    fuel_selector: int = -1
    mixture_pct: float = 0.0
    autopilot: bool = False
    gps_drives_nav1: bool = False
    pitot_heat: bool = False
    fuel_total_gal: float = 0.0
    spoilers_armed: bool = False
    avionics_master: bool = False
    battery_master: bool = False
    # COM STATUS:1 -- 0 working, 1 not fitted, 2 no electricity, 3 failed --
    # and the main bus in volts; -1 for either until the simulator has said.
    com1_status: int = -1
    main_bus_volts: float = -1.0
    # TRANSPONDER STATE:1: 0 off, 1 standby, 2 test, 3 on, 4 altitude;
    # -1 until the simulator has said.
    transponder_state: int = -1
    # The aeroplane's own speeds, where the model declares them (zero where
    # it does not), for the rotate callout.
    design_takeoff_kt: float = 0.0
    min_rotation_kt: float = 0.0
    # What the first officer's callouts watch (wilcoatc.atc.callouts): the
    # bank, signed degrees, right positive; NAV1's localizer and glideslope
    # and how far off them the needles are (+/-127 and +/-119, full scale);
    # the speed set on the autopilot, 0 where there is none.
    bank_deg: float = 0.0
    nav1_has_localizer: bool = False
    nav1_cdi: float = 127.0
    nav1_has_glideslope: bool = False
    nav1_gsi: float = 119.0
    ap_airspeed_kt: float = 0.0
    # What the first officer notices (wilcoatc.atc.notices): engine 1's
    # throttle, per cent; the fuel either side, gallons; the total by
    # weight, pounds.
    throttle_pct: float = 0.0
    fuel_left_gal: float = 0.0
    fuel_right_gal: float = 0.0
    fuel_total_lb: float = 0.0

    # --- ambient conditions the sim is actually rendering ---
    ambient_wind_direction: float = 0.0
    ambient_wind_velocity: float = 0.0
    ambient_temperature_c: float = 15.0
    ambient_visibility_m: float = 16000.0
    ambient_humidity_pct: float = 60.0
    sea_level_pressure_hpa: float = 1013.25

    # --- bookkeeping ---
    timestamp: float = field(default_factory=time.monotonic)
    connected: bool = False
    source: str = "none"
    # Whether the simulator is paused or the user is in its menus -- which
    # is to say, whether anything is happening in the aeroplane at all.
    # Nothing that watches for a change should believe one while this is set:
    # the aircraft did not take off, it was moved by a menu.
    paused: bool = False

    # ------------------------------------------------------------------

    @property
    def airborne(self) -> bool:
        return not self.on_ground

    @property
    def moving(self) -> bool:
        return self.ground_speed_kt > 1.0

    @property
    def climbing(self) -> bool:
        return self.vertical_speed_fpm > 300

    @property
    def descending(self) -> bool:
        return self.vertical_speed_fpm < -300

    @property
    def position(self) -> tuple[float, float]:
        return (self.latitude, self.longitude)

    @property
    def has_position(self) -> bool:
        """Whether the position looks real rather than an uninitialised zero."""
        return not (abs(self.latitude) < 1e-6 and abs(self.longitude) < 1e-6)

    def distance_to(self, lat: float, lon: float) -> float:
        from ..navdata.build import haversine_nm

        return haversine_nm(self.latitude, self.longitude, lat, lon)

    def bearing_to(self, lat: float, lon: float) -> float:
        from ..navdata.build import bearing_deg

        return bearing_deg(self.latitude, self.longitude, lat, lon)

    def relative_bearing(self, lat: float, lon: float) -> float:
        """Bearing to a point relative to the nose, 0-359."""
        return (self.bearing_to(lat, lon) - self.heading_true + 360.0) % 360.0

    def oclock(self, lat: float, lon: float) -> int:
        """Clock position of a point, as ATC gives traffic."""
        relative = self.relative_bearing(lat, lon)
        hour = int(round(relative / 30.0)) % 12
        return 12 if hour == 0 else hour


def normalize_angle(value: float, source_is_radians: bool | None = None) -> float:
    """Return an angle in degrees, tolerating a source that reports radians.

    SimConnect's native unit for headings is radians, but wrappers vary in
    whether they convert. A heading is indistinguishable from radians only when
    it is under about 6.3, which is a real heading of 006 -- rare enough that
    guessing wrong costs little, and detectable when the caller knows.
    """
    if value is None:
        return 0.0
    value = float(value)
    if source_is_radians is True or (
        source_is_radians is None and -2 * math.pi <= value <= 2 * math.pi
    ):
        value = math.degrees(value)
    return value % 360.0
