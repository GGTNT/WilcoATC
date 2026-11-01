"""The engine that runs a flight.

Ties the pieces together and owns the two loops that matter:

*The radio loop.* Push to talk, record, transcribe, parse, decide, synthesise,
play. Everything in that chain is local, and on a modern desktop the whole
round trip takes on the order of a second and a half for a normal transmission.

*The situation loop.* Polls the simulator, notices when the pilot tunes a new
frequency, keeps the ATIS current, and offers a handoff when the flight has
outrun the controller it is talking to.

The engine never composes controller speech itself; it asks
:class:`~wilcoatc.atc.controller.ControllerBrain` what to say and
:mod:`~wilcoatc.atc.phraseology` supplies the words.
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from .atc.atis import AtisGenerator, AtisReport
from .atc.controller import ControllerBrain, Reply
from .atc import interphone
from .atc.crew import CrewDirector
from .atc.intents import Intent, IntentParser, ParsedIntent
from .atc.landing import LandingMonitor
from .atc.phraseology import Aircraft, Phraseology, Weather, wake_suffix
from .atc.profile import FlightProfile
from .atc.callsigns import every_language, heard_registration
from .atc import letters
from .atc.detect import by_words, clearly_in, language_of_text
from .atc.language import (LANGUAGE_NAMES, SUPPORTED_LANGUAGES, languages_for,
                           profile_for, reply_language)
from .atc.session import FlightSession, Instruction, Phase, station_key
from .atc.handling import Handling, fuselage_length
from .atc.traffic import TrafficInjector, UserAircraft
from .atc import lod as lod_windows
from .atc.lod import Candidate, Lod
from .atc.systems import wanted as systems_wanted
from .atc.servicelayout import fittings_for, load_layout, spots_for
from .atc import handling as handling_spots
from .integrations.aitraffic import SimTraffic
from .integrations.systems import SimSystems
from .integrations.groundvehicles import gsx_installed
from .integrations.servicepoints import SimServicePoints
from .integrations.doors import DoorTable, SimDoors
from .integrations.groundvehicles import SimVehicles
from .integrations.facilities import SimFacilities
from .integrations import gsx
from .integrations.gsx import GsxLink
from .integrations.simaircraft import SimAircraft
from .integrations.network import OnlineNetwork
from .atc.arrival import ArrivalWatch
from .atc.climb import leaving_departure
from .atc.departure import ClimbWatch, DepartureWatch, SidWatch
from .atc.routing import RouteWatch
from .atc.watch import (AirspaceWatch, RadarServiceWatch,
                        ReportingPointWatch, RestrictionWatch, RunwayWatch,
                        SurfaceWatch)
from .atc.written import as_written
from .logs import decision, RADIO, timed
from .audio.io import (CabinPlayer, MicrophoneCapture, PushToTalk,
                       JoystickPushToTalk, RadioPlayer)
from .audio.controllers import ControllerMissing, ControllersUnavailable
from .audio.imperfections import from_config as imperfections_for
from .audio.prosody import from_config as prosody_for
from .audio.tone import from_config as tone_for
from .audio.radio_fx import PROFILE_ATIS
from .audio.announcements import FOLDER as ANNOUNCEMENTS, AnnouncementLibrary
from .audio.stt import Recognizer, SpeechModelMissing, engine_model
from .audio.tts import Synthesizer
from .audio.voices import VoicesMissing
from .paths import announcements_dir, command_hint, resolve as _resolve
from .config import Config
from .llm.providers import build_provider
from .llm.understanding import Understanding
from .navdata.channels import is_valid_channel, same_channel
from .navdata.db import (STATION_RANGE_NM, NavDB, Station, dialect_for,
                         haversine_nm, normalize_runway)
from .sim.source import ManualSource, SimConnectSource, StateSource
from .sim.state import AircraftState
from .navdata.magvar import variation
from .weather.metar import (WeatherProvider, at_the_surface, merge_weather,
                            in_the_circuit, near_the_field, weather_from_sim)

log = logging.getLogger(__name__)
status_log = logging.getLogger("wilcoatc.traffic.debug")

#: How long COM1 has to stay on a frequency nobody answers before that is
#: said. A swap passes through the standby frequency, and a knob being
#: turned through every one between.
EMPTY_NOTICE_S = 2.5


@dataclass
class Event:
    """Something worth showing the user.

    ``text`` is English and is what the terminal console prints. ``key`` and
    ``params``, when present, let the panel say the same thing in whatever
    language it is being read in; a notice with no key, or a language with no
    entry for it, falls back to the English.
    """

    kind: str          # "pilot" / "atc" / "info" / "error" / "atis" / "traffic"
    text: str
    speaker: str = ""
    at: float = field(default_factory=lambda: time.time())
    key: str = ""
    params: dict = field(default_factory=dict)
    #: What the panel can do about it, when there is something. Only
    #: ``"setup"`` so far, which means "put the download dialog up": a
    #: missing download is the one failure a pilot can fix from the window,
    #: and a line of red text in the transcript does not tell them that.
    #: ``"spoken"`` on a pilot transmission that came through the
    #: microphone: the panel empties its text box.
    action: str = ""


EventSink = Callable[[Event], None]


def announcement_roots(config) -> list:
    """Where to look for recorded cabin announcements.

    The folder beside the program always, so that dropping a file in works
    with no configuration at all -- and whatever else the pilot named, which
    is how an existing Fenix pack is used where it already sits rather than
    being copied.

    A name that is not a path is looked for inside the announcements folder
    as well as beside the program. "SWR" is a folder somebody has in their
    pack, not a folder next to the executable, and typing it in the settings
    box is a pilot pointing at the one they can see; resolving it only
    against the program's own folder made that setting a silent no-op.
    """
    roots = [announcements_dir()]
    named = (getattr(config.immersion, "recordings_dir", "") or "").strip()
    if not named:
        return roots
    for candidate in _named_roots(named):
        if candidate.is_dir():
            roots.insert(0, candidate)
            return roots
    # Nothing there under any reading of it. The path is still returned, so
    # the settings screen shows what was looked for rather than quietly
    # substituting something else, and `announcements_missing` is what turns
    # that into a line the pilot can act on.
    roots.insert(0, _resolve(named))
    return roots


def _named_roots(named: str):
    """Every place a typed announcements folder might mean, best first."""
    here = announcements_dir()
    return [_resolve(named), here / named, here / ANNOUNCEMENTS / named]


def announcements_missing(config) -> str:
    """The folder the pilot named, if it is not there. Empty if it is fine."""
    named = (getattr(config.immersion, "recordings_dir", "") or "").strip()
    if not named:
        return ""
    if any(path.is_dir() for path in _named_roots(named)):
        return ""
    return named


class NotAFlightPlan(ValueError):
    """A file handed in as a ``.pln`` that the simulator did not write."""


class Engine:
    """Runs the ATC simulation for one flight."""

    def __init__(
        self,
        config: Config | None = None,
        navdb: NavDB | None = None,
        on_event: EventSink | None = None,
    ):
        self.config = config or Config()
        self.navdb = navdb or NavDB()
        self.on_event = on_event or (lambda event: None)

        self.weather_provider = WeatherProvider(
            refresh_s=self.config.weather.refresh_s,
            online=self.config.weather.online,
        )
        self.synth = Synthesizer(
            voice_dir=_resolve(self.config.voice.voice_dir),
            cache_size=self.config.voice.cache_size,
            seed=self.config.voice.seed,
            engine=self.config.voice.engine,
            prosody_settings=prosody_for(self.config.delivery.prosody),
            imperfection_settings=imperfections_for(
                self.config.delivery.imperfections),
            tone_settings=tone_for(self.config.delivery.tone),
            gpu=self.config.voice.gpu,
            hq_model=self.config.voice.hq_model,
            hq_takes=self.config.voice.hq_takes,
        )
        # Said once. Without voices every single transmission fails, and a
        # transcript filling with the same red line is noise rather than
        # information.
        self._voices_reported = False
        # The languages the pilot may speak, which nobody chooses: they follow
        # the aeroplane. English everywhere, plus whatever the state below is
        # worked in, and the set changes as the flight crosses a border. The
        # config can still pin it, which is only useful for keeping the
        # English-only recogniser on a machine that will never leave the US.
        self.pinned_languages = [
            code.lower()[:2] for code in (self.config.speech.languages or [])
        ]
        self.languages = self.pinned_languages or ["en"]
        self.recognizer = Recognizer(
            # Where the flight will go is not known at startup, so the model
            # has to be one that can hear anything. Pinning the languages to
            # English is what keeps the smaller English-only model.
            model_size=engine_model(self.config),
            device=self.config.speech.device,
            compute_type=self.config.speech.compute_type,
            beam_size=self.config.speech.beam_size,
            languages=self.languages,
            detection_threshold=self.config.speech.detection_threshold,
        )
        # Never a download from here: the setup screen fetches the model with
        # its own bar. Missing, the recogniser says so (SpeechModelMissing).
        self.recognizer.allow_download = False
        # The cloning tier reads its takes back through this recogniser.
        # Nothing happens here unless that tier is in the cast.
        self.synth.verify_with(self.recognizer)
        self.player = RadioPlayer(
            device=self.config.audio.output_device,
            volume=self.config.audio.volume,
        )
        self.microphone = MicrophoneCapture(
            device=self.config.audio.input_device,
            sample_rate=self.config.audio.capture_rate,
            max_seconds=self.config.audio.max_transmission_s,
        )
        # Everybody in this aeroplane who is not a controller: the other
        # seat, and the cabin. On its own audio path, because they are not on
        # the frequency and must not queue behind it.
        self.cabin = CabinPlayer(
            device=self.config.audio.output_device,
            volume=self.config.immersion.volume,
            on_trouble=self._cabin_trouble,
        )
        # Whatever recordings the pilot has put in the announcements folder,
        # in the layout the Fenix packs already use.
        self.announcements = AnnouncementLibrary(announcement_roots(self.config))
        self.crew = CrewDirector(
            self.config, self.synth, self.cabin,
            emit=lambda kind, text, speaker="", **extra: self.emit(
                kind, text, speaker, **extra),
            weather_for=self.weather_for,
            here=self._field_below,
            announcements=self.announcements,
            radio_busy=self.radio_busy,
            # So the cabin knows when the push is over, which is when the
            # cabin manager picks up the handset. Read through a callable
            # rather than handed the object, because the link is built below
            # this and the crew must not hold a reference to something that
            # can be replaced.
            ground=lambda: self.gsx.state,
            # One variable read now, before the first officer says the
            # aeroplane disagrees with a checklist answer. Read only.
            fresh=self._read_now,
        )
        # Where the circuit puts the aeroplane, for the first officer's
        # circuit calls (crew.circuit; the brain is built below).
        self.crew.circuit = self._where_in_the_circuit
        # What the first officer answers questions from, and the radio call
        # it makes when the pilot hands one over (Phase 4).
        self.crew.frequency_for = self._frequency_for
        self.crew.distance_to_field = self._distance_to_field
        self.crew.radio_call = self.first_officer_call
        self.crew.transmit = self.first_officer_transmits
        # Rules first; the AI layer only sees what they could not classify.
        self.parser = IntentParser()
        self.understanding = Understanding(
            self.parser,
            build_provider(
                self.config.ai.provider,
                model=self.config.ai.model,
                host=self.config.ai.host,
                api_key=self.config.ai.api_key,
                timeout_s=self.config.ai.timeout_s,
            ),
            min_confidence=self.config.ai.min_confidence,
        )
        self.brain = ControllerBrain(
            self.navdb, self._base_phraseology(),
            strict_icao_digits=self.config.atc.strict_icao_digits,
            ground_control=self.config.atc.ground_control,
        )
        self.brain.realism = self.config.immersion.controller
        self.brain.hearing = self.config.speech
        # When the search for an enroute frequency last came up empty, so a
        # flight over a country that publishes none is not made to look for
        # one on every pass of the situation loop.
        self._centre_looked_at = 0.0
        # When a centre last looked at which sector the flight is in.
        self._centre_checked_at = 0.0
        # The sector the flight was last seen in, for the row written as it
        # enters the next; and the SimBrief route as points, per plan.
        self._sector_noted = None
        self._route_cache = None
        # The half of the controller that watches rather than listens: what
        # the aeroplane is doing on the ground, and what it is doing on the
        # way in. Both speak first; nothing in either waits to be asked.
        self.watch = SurfaceWatch(self.navdb, self.brain)
        self.airspace_watch = AirspaceWatch(self.navdb, self.brain)
        self.point_watch = ReportingPointWatch(self.navdb, self.brain)
        # And the radar half of it for a VFR flight: where its service ends,
        # and whether it is keeping under the altitude it was held at.
        self.radar_service_watch = RadarServiceWatch(self.navdb, self.brain)
        self.restriction_watch = RestrictionWatch(self.navdb, self.brain)
        # Through lambdas, so a replaced weather_for or atis_for is the one
        # asked.
        self.runway_watch = RunwayWatch(
            self.navdb, self.brain, lambda ident: self.weather_for(ident),
            lambda ident, language: self.atis_for(ident, language))
        self.arrivals = ArrivalWatch(self.navdb, self.brain)
        # The controller answers some requests with what the arrival watch
        # would say unprompted: a hold, the first descent (Phase 6).
        self.brain.arrivals = self.arrivals
        self.departures = DepartureWatch(self.navdb, self.brain)
        # The IFR climb in steps and the SID watched (the IFR plan, Phase 1).
        self.climbs = ClimbWatch(self.brain)
        self.sid_watch = SidWatch(self.brain)
        self.route_watch = RouteWatch(self.brain)
        # What the ground handling is doing, if anything is saying -- and
        # the tug, once a push has been approved. The tug is handed over as a
        # callable rather than as the source, so nothing in integrations has
        # to know what a simulator link is.
        self.gsx = GsxLink(enabled=self.config.gsx.enabled,
                           control=self.config.gsx.control,
                           tug=self._sim_pushback)
        self.brain.ground = self.gsx

        # The other aeroplanes on the frequency.
        self.traffic = TrafficInjector(
            self.navdb,
            seed=self.config.traffic.seed or None,
            density=self.config.traffic.density
            if self.config.traffic.enabled else 0.0,
            callsigns=self.config.traffic.callsigns,
        )
        # Nothing invented exists until the simulator has drawn it. A pilot
        # told there is an aeroplane on the runway looks for it, and the
        # traffic used to be on the radio and the map whether the simulator
        # had made it or not -- which, in 2024, it never had.
        self.traffic.requires_sim = True
        self.traffic.max_invented = self.config.traffic.in_simulator_limit
        # And the ones standing at the gates, on a budget of their own.
        self.traffic.parked = max(0, int(self.config.traffic.parked))
        # The aeroplanes the pilot put on the runway, waiting to be said once
        # the simulator has actually made them: callsign -> runway.
        self._spawned: dict[str, str] = {}
        # The controller sequences the user against the same traffic the user
        # can hear, rather than against nothing.
        if self.config.traffic.enabled:
            self.brain.traffic = self.traffic
        # The same aeroplanes, out of the window. Without a simulator this
        # stays dormant and the radio is unchanged.
        # The simulator's own frequency list, which beats the published one.
        self.facilities = SimFacilities(self.config.sim.simconnect_dll)
        # Airports already read, so each is asked for once per session.
        self._facilities_done: set[str] = set()
        # And their stands and taxiways, for the traffic to drive on rather
        # than be placed on. Read in the background; handed over when ready.
        from .integrations.taxiways import SimGround

        self.simground = SimGround(self.config.sim.simconnect_dll)
        self._ground_asked: set[str] = set()
        # And the approaches each airport publishes, so the ILS is only
        # promised where there is one. Asked once per airport, on a worker,
        # and handed to the brain as the answers come in.
        from .integrations.approaches import SimApproaches

        self.simapproaches = SimApproaches(self.config.sim.simconnect_dll)
        # And each runway's pattern altitude, behind the circuit files.
        from .integrations.runways import SimRunways

        self.simrunways = SimRunways(self.config.sim.simconnect_dll)
        # Where each field's runways have been said to come from (logs radio).
        self._runways_said: dict[str, str] = {}
        self._approaches_asked: set[str] = set()
        self._ground_ready: dict[str, object] = {}
        # The controller taxis the pilot on the same layouts the traffic
        # drives on, and reaches them the way it reaches the traffic: handed
        # a service, so nothing in atc/ imports the engine.
        self.brain.ground_layout = self.ground_layout
        # And the ATIS each field is broadcasting, so a call reporting an old
        # letter is told the current one.
        self.brain.atis = self.atis_for
        self.brain.weather_for = self.weather_for
        # And everything the simulator reports, the pilot's own excluded,
        # for the controller's picture (ControllerBrain.contacts).
        self.brain.sim_traffic = self._sim_contacts
        from .atc.trafficinfo import TrafficWatch

        self.traffic_watch = TrafficWatch(self.brain)
        self.simtraffic = SimTraffic(
            enabled=self.config.traffic.enabled
            and self.config.traffic.in_simulator,
            # The parked aeroplanes come after the moving ones in what it is
            # given, so this is room for them rather than a share of it.
            limit=self.config.traffic.in_simulator_limit
            + max(0, int(self.config.traffic.parked)),
            settle_s=self.config.traffic.lod.settle_s,
            smooth=self.config.traffic.smooth_motion,
        )
        self.simtraffic.lod_models = dict(self.config.traffic.lod.models or {})
        # Which model each parked aeroplane is drawn with, by how far it is
        # from the pilot: the simulator's own, with doors, nearby; FSLTL's
        # painted one further out. The doors follow the aeroplane across.
        lod = self.config.traffic.lod
        self.lod = Lod(enabled=lod.enabled,
                       detailed_m=lod.detailed_m,
                       light_m=lod.light_m,
                       limit=lod.limit,
                       swaps_per_s=lod.swaps_per_s,
                       mode=str(lod.mode).strip().lower(),
                       radius_m=float(lod.detail_radius_km) * 1000.0,
                       hide_m=lod.hide_m,
                       livery=str(lod.livery).strip().lower())
        self._ai_liveries_asked = False
        # The schedule changes the traffic's day as well as the models:
        # departures board before they call, arrivals hold their stand.
        self.traffic.schedule = self.lod.scheduled
        # The test tools (traffic.debug), all off by default. time_scale
        # shortens the turnaround and the schedule's booking and boarding.
        debug = self.config.traffic.debug
        self.time_scale = max(0.01, float(debug.time_scale or 1.0))
        self.traffic.boarding_s = max(0.0, float(lod.boarding_s)) / self.time_scale
        self.traffic.boarding_lead_s = (max(0.0, float(lod.boarding_lead_s))
                                        / self.time_scale)
        self._status_at = 0.0
        self._motion_status_at = 0.0
        self.simtraffic.on_swapped = self._model_swapped
        self.simtraffic.on_removed = self._model_removed
        # A departure asks for taxi only once its doors are read shut.
        self.traffic.can_push = self._may_push
        # A test setting: every invented aeroplane is one of these types.
        # traffic.debug.force_types, or the older traffic.debug_force_types.
        self.traffic.force_types = tuple(
            str(code).strip().upper() for code in
            (debug.force_types or self.config.traffic.debug_force_types or [])
            if str(code).strip())
        if self.traffic.force_types:
            log.info("traffic.debug: every invented aeroplane is one of %s",
                     ", ".join(self.traffic.force_types))
        # And the aeroplanes somebody else put there, which is the other half
        # of the same question: what is actually on this airport.
        # Read whether or not this program draws aeroplanes of its own: a
        # pilot who has switched the invented traffic off still has FSLTL
        # running, and the controller has to see it.
        self.simwatch = SimAircraft(enabled=self.config.traffic.from_simulator)
        # And the ramp around the aeroplanes this program parks: who calls
        # on them and when is decided here, and drawn by the simulator.
        # Passengers and their buses are GSX's people and GSX's bus: without
        # GSX Pro there is nobody to walk, and an aeroplane's stairs held
        # for a bus that never comes would only keep its tug waiting.
        handling_config = self.config.traffic.handling
        gsx = gsx_installed()
        visible = (max(0, int(handling_config.passengers_visible))
                   if handling_config.passengers and gsx else 0)
        if handling_config.passengers and not gsx:
            log.info("traffic.handling.passengers is on, but GSX Pro is not "
                     "installed: there is nobody to walk, so no buses")
        self.handling = Handling(seed=self.config.traffic.seed or None,
                                 limit=self.config.traffic.handling_limit,
                                 schedule=self.lod.scheduled,
                                 time_scale=self.time_scale,
                                 passengers=visible,
                                 baggage=bool(handling_config.baggage))
        self._handling_raised = False
        self.simvehicles = SimVehicles(
            enabled=self.config.traffic.enabled
            and self.config.traffic.in_simulator
            and self.config.traffic.handling.enabled
            and self.config.traffic.handling.vehicles,
            # The turnaround keeps the budget; this is the ceiling under
            # it, with room for the tugs the turnaround lets through over
            # the budget (see Handling._admit).
            limit=self.config.traffic.handling_limit + 4,
            dll_path=self.config.sim.simconnect_dll,
            prefer_gsx=self.config.traffic.handling.gsx_vehicles
            and gsx,
            # Two buses and their passengers, with room for the ones still
            # walking off as the next appear.
            people=visible > 0 or bool(handling_config.baggage),
            # Two buses and their passengers, two trains (a tractor, two
            # carts and at most two bags on a belt), and room for the ones
            # still going as the next appear.
            people_limit=Handling.BUSES * (visible + 1) + Handling.TRAINS * 5 + 6,
        )
        if self.simvehicles.prefer_gsx:
            log.info("GSX Pro is installed: its vehicles serve this "
                     "program's traffic where the simulator lists them")
        # And the aeroplanes' own doors, opened with the stairs and the
        # loaders and read shut before anything pushes.
        self.simdoors = SimDoors(
            enabled=self.config.traffic.enabled
            and self.config.traffic.in_simulator
            and self.config.traffic.handling_doors,
            table=DoorTable.load(self.config.traffic.doors_table),
            force_after_s=self.config.traffic.doors_force_after_s,
        )
        # And their engines and lights (wilcoatc.atc.systems decides,
        # this makes the simulator agree).
        # And where each model's doors are, which is where the vehicles go.
        self.servicepoints = SimServicePoints(
            enabled=self.config.traffic.enabled
            and self.config.traffic.in_simulator
            and self.config.traffic.handling.enabled)
        self._service_layout = load_layout()
        self._stairs_said: set[tuple[str, str, str]] = set()
        self.simsystems = SimSystems(
            enabled=self.config.traffic.enabled
            and self.config.traffic.in_simulator
            and self.config.traffic.engines_and_lights)

        self.source: StateSource = ManualSource()
        self.state = AircraftState()
        self.session: FlightSession | None = None
        # The last SimBrief plan loaded, if any.
        self.plan = None
        # The filed route as a line on the chart (atc/route.py): from the
        # SimBrief plan, or a flight plan file. None is a flight with no plan,
        # worked by distance to the airport as before.
        self.route = None
        # The leg the aeroplane was last found on, and where, for the panel.
        self.route_progress = None
        # How the landing went, and the flight against the plan's schedule.
        # Read through a callable so a plan loaded mid-flight still counts.
        self.landing = LandingMonitor(plan=lambda: self.plan)
        self.station: Station | None = None

        self._atis: dict[tuple[str, str], AtisGenerator] = {}
        # Fields whose ATIS must be re-issued on the next ask, set from the
        # approaches worker.
        self._atis_stale: set[str] = set()
        self._atis_lock = threading.Lock()
        self._weather_cache: dict[str, Weather] = {}
        self._ptt = None
        self._stop = threading.Event()
        self._situation_thread: threading.Thread | None = None
        self._busy = threading.Lock()
        # Who is really on the frequency, where a network is configured. The
        # one thing this program must never do is talk over a person.
        self.online = OnlineNetwork(config.network.provider,
                                    config.network.refresh_s)
        self.human: Any = None
        self._last_com1 = 0.0
        self._reported_empty = 0.0
        # (frequency, since when) of an empty COM1 not yet explained.
        self._empty_pending: tuple[float, float] | None = None
        self._reported_assumed = 0.0
        # The simulator link, watched rather than assumed. Connecting once at
        # startup is not enough: the panel is usually opened before the sim
        # has finished loading, and a sim that is shut down mid-session used
        # to leave the aeroplane frozen wherever it last was, with the panel
        # cheerfully reporting a position that had stopped being true.
        self._sim_wanted = bool(self.config.sim.enabled)
        self._next_sim_try = 0.0
        self._sim_was_connected = False
        # The live simulator link, when there is one. Held by name rather
        # than recognised by type: the question is whether *this* engine has
        # a simulator source in use, and a type check answers a subtly
        # different one that goes wrong the moment anything wraps the class.
        self._sim_source: SimConnectSource | None = None
        # Whether the last tick found the simulator paused, so it is said
        # once rather than four times a second.
        self._was_paused = False
        # The language each frequency is being worked in (_frequency_language).
        self._languages_by_frequency: dict[str, str] = {}
        # When a controller last spoke, so a bare "say again" after them is
        # theirs and not the first officer's (answer_the_aeroplane).
        self._atc_said_at = 0.0
        # The debounced on-ground flag (see _debounced_ground).
        self._ground_seen: bool | None = None
        self._ground_disagrees = 0
        # A frequency this program put into the radio itself. The situation
        # loop reads COM1 back a moment later and must not treat its own
        # write as the pilot having reached for the radio.
        self._tuned_by_us = 0.0
        # The language of the last exchange, so an ATIS or a volunteered
        # handoff continues in whatever the pilot has been speaking.
        self.last_language = self.languages[0]
        # When each field's weather was last looked up. Per field: one shared
        # clock meant any field asked about every minute kept every other
        # field's first reading for the rest of the flight.
        self._weather_checked: dict[str, float] = {}
        self.started_at = time.monotonic()
        self._last_region_check = 0.0
        # Everything that has been said on the radio this session, newest
        # last. Short: it is what a panel shows, not an archive, and the log
        # files are where a transmission goes to be kept.
        self.transcript: deque = deque(maxlen=200)
        # The traffic goes quiet for a moment after anybody else transmits, so
        # two aeroplanes are never heard talking over each other.
        self._frequency_clear_at = 0.0
        # An answer decided while the frequency was busy with somebody
        # else's exchange, said once it is over: (reply, station, language,
        # when it was held).
        self._held_reply = None
        # What the other aeroplanes have said but has not reached the speakers
        # yet, in the order they said it.
        self._traffic_queue: list = []
        # What has been decided about each AI exchange: whether any of it has
        # been put on the air, and when that was decided. Its halves arrive
        # here one at a time, so the decision has to outlive the transmission
        # it was made about.
        self._exchanges: dict[int, tuple[bool, float]] = {}
        # How many aeroplanes of the simulator's own the pilot has been told
        # about, so the line is news rather than a running commentary.
        self._observed_said = 0

    def _watch_landing(self, previous, state, session) -> None:
        """Hand the landing report to whoever is watching, once it exists."""
        try:
            written = self.landing.follow(previous, state)
        except Exception:
            log.debug("the landing monitor raised", exc_info=True)
            return
        if not written:
            return
        report = self.landing.report
        # Where it actually landed, which is the filed destination only when
        # nothing went wrong.
        here = self._field_below()
        report.airport = (here.ident if here is not None else
                          session.destination.ident if session.destination
                          else "")
        log.info("landing: %s", report.describe())
        self.emit("info", f"Landing: {report.describe()}", key="log.landing",
                  fpm=int(round(report.fpm)), score=report.score)

    def _frequency_for(self, name: str):
        """(callsign, MHz) of the station whose name the pilot asked about,
        within reach, or None."""
        from .atc.interphone import fold

        state = self.state
        if not state.has_position or not name:
            return None
        wanted = fold(name).split()
        best = None
        for station in self.navdb.all_stations_near(
                state.latitude, state.longitude, 120.0):
            if station.position == "ATIS":
                continue
            called = fold(f"{station.callsign} {station.facility} "
                          f"{station.description}")
            hits = sum(1 for w in wanted if w in called.split())
            if hits and (best is None or hits > best[0]
                         or (hits == best[0]
                             and station.distance_nm < best[1].distance_nm)):
                best = (hits, station)
        if best is None:
            return None
        return best[1].callsign, best[1].mhz

    def _distance_to_field(self):
        """(name, nautical miles) to where the flight is going, or None."""
        session, state = self.session, self.state
        if session is None or not state.has_position:
            return None
        airport = session.destination or self._field_below()
        if airport is None:
            return None
        name = getattr(airport, "name", "") or airport.ident
        return (name.split(" ")[0].title() if name else airport.ident,
                state.distance_to(airport.lat, airport.lon))

    def first_officer_call(self, kind: str) -> tuple[str, str]:
        """The one call the pilot handed over: its words, or "" and why not.

        Built with the same PilotSpeech the AI readback uses, so the
        controller reads it as it reads the pilot's. A readback is made only
        when it is what was handed over: a clearance addressed to the pilot
        is theirs to read back.
        """
        from .atc.copilot import readback as build_readback
        from .atc.pilot import PilotSpeech

        session = self.session
        station = self.station or self._current_station()
        if session is None or station is None:
            return "", "no station"
        spoken = self._language_for(station.ident, self.last_language)
        p = self.brain.for_station(station, spoken)
        speech = PilotSpeech(spoken, p)
        aircraft = session.aircraft
        name = station.callsign_in(spoken)
        pending = session.pending
        if kind == "readback":
            if pending is None or pending.read_back:
                return "", "nothing to read back"
            handoff = session.handoff_pending
            return build_readback(
                pending, aircraft, p, spoken,
                station_name=(handoff.callsign_short if handoff is not None
                              else "")), ""
        if pending is not None and not pending.read_back:
            return "", "readback"
        phase = session.phase
        if kind == "any":
            if phase in (Phase.PREFLIGHT, Phase.CLEARED) and session.ifr \
                    and not session.clearance_issued:
                kind = "clearance"
            elif phase in (Phase.PREFLIGHT, Phase.CLEARED, Phase.PUSHBACK,
                           Phase.TAXI_OUT):
                kind = "taxi"
            elif phase in (Phase.HOLDING_SHORT, Phase.LINED_UP):
                kind = "ready"
            else:
                return "", "which"
        if kind == "clearance":
            destination = session.destination
            return speech.request_clearance(
                aircraft, name, getattr(destination, "name", "")
                or getattr(destination, "ident", "")), ""
        if kind == "taxi":
            if station.position not in ("GND", "INFO", "CTAF", "UNICOM",
                                        "RDO", "AFIS"):
                has_ground = any(s.position == "GND"
                                 for s in self.navdb.stations(station.ident))
                if has_ground:
                    return "", "not_on:ground"
            return speech.request_taxi(aircraft, name), ""
        if kind == "ready":
            return speech.ready_for_departure(
                aircraft, session.departure_runway or ""), ""
        if kind in ("downwind", "base", "final"):
            return speech.circuit_report(aircraft, p.leg(kind)), ""
        if kind == "vacated":
            return speech.runway_vacated(aircraft), ""
        return "", "which"

    def first_officer_transmits(self, text: str) -> None:
        """The first officer's call on the frequency: shown as theirs, and
        through the ordinary path so the controller checks it."""
        station = self.station or self._current_station()
        spoken = self._language_for(station.ident if station else "",
                                    self.last_language)
        self.emit("pilot", text, "First officer")
        try:
            self.handle_text(text, spoken, speaker="first_officer")
        except Exception:
            log.debug("the first officer's call failed", exc_info=True)

    def _where_in_the_circuit(self):
        """Where the circuit puts the aeroplane, for the first officer's
        "downwind", "base", "final, runway 24, cleared": the controller's own
        circuit for the field and runway, so the two never disagree. None on
        the ground, or with nothing to place it by."""
        session, state = self.session, self.state
        if session is None or state is None or state.on_ground \
                or not state.has_position:
            return None
        runway = session.arrival_runway or session.departure_runway
        airport = session.destination or self._field_below()
        if not runway or airport is None:
            return None
        try:
            circuit = self.brain._circuit_at(session, airport, runway)
            return self.brain._where_in(circuit, state)
        except Exception:
            log.debug("could not place the aeroplane in the circuit",
                      exc_info=True)
            return None

    def _field_below(self):
        """The airport the aeroplane is at, if it is at one.

        The cabin announcements use it to fill in a destination nobody
        filed, which is most flights: "welcome to Nantes" beats "welcome to
        our destination" and is no less true.
        """
        state = self.state
        if not state.has_position:
            return None
        return self.navdb.home_airport(state.latitude, state.longitude)

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------

    def _base_phraseology(self) -> Phraseology:
        # The brain's fallback, for a station with no field. Each controller
        # is built for its own station (ControllerBrain.for_station).
        return Phraseology(
            FlightProfile.of(self.config.flight).dialect_for(""),
            strict_icao_digits=self.config.atc.strict_icao_digits,
        )

    # Kinds that are somebody talking on the radio, and therefore get written
    # down the way a pilot writes on a strip rather than the way it was said.
    #
    # The crew are on the list too. They are not on the radio, but they are
    # somebody speaking, and a first officer calling "ten thousand feet"
    # should read as figures in the transcript for the same reason a
    # controller does.
    _TRANSCRIBED = frozenset({"pilot", "atc", "atis", "traffic", "crew"})

    def emit(self, kind: str, text: str, speaker: str = "", *,
             key: str = "", action: str = "", **params) -> None:
        """Show something to the user.

        A transmission reaches the screen in figures where a pilot would write
        figures -- runway 31, QNH 1013, 127.750 rather than "one two seven
        decimal seven five zero". The spoken form is what goes to the
        synthesiser and is untouched by this; the two are different strings on
        purpose, and ``ui.numerals`` decides only which one is shown.
        """
        if kind in self._TRANSCRIBED:
            text = as_written(text, figures=self.config.ui.numerals)
            # And kept, because this is the only place every transmission goes
            # through. The window has a live connection and does not need it;
            # the panel inside the simulator polls, and a poll that arrives
            # between two transmissions would otherwise never see them.
            self.transcript.append({
                "at": time.time(), "kind": kind,
                "speaker": speaker, "text": text,
            })
        try:
            self.on_event(Event(kind, text, speaker, key=key, action=action,
                                params={k: str(v) for k, v in params.items()}))
        except Exception:
            log.debug("event sink raised", exc_info=True)

    def voices_installed(self) -> None:
        """Something was just downloaded into the voice folder.

        Called by the panel when the installer finishes. The engine is running
        and has already read an empty folder; without this the pilot has to
        restart the program they have only just finished setting up, which is
        the worst moment to be asked to.
        """
        self.synth.refresh()
        self._voices_reported = False
        try:
            self.synth.warm(piper=self.config.voice.preload)
        except Exception:
            log.debug("warming after a download raised", exc_info=True)

    def use_cloned_voices(self) -> None:
        """The cloned voices were asked for from the window, and have arrived.

        Asking for them is the pilot's choice of engine, made the only way a
        pilot without a text editor can make it, so it is written to
        ``config.yaml`` and survives a restart. The synthesiser is switched
        in place (:meth:`Synthesizer.use_engine`), because nothing that is
        downloaded requires a restart to take effect.
        """
        voice = self.config.voice
        if voice.engine != "chatterbox":
            voice.engine = "chatterbox"
            try:
                self.config.save()
            except Exception as exc:
                log.warning("could not write config.yaml: %s", exc)
        self.synth.use_engine(voice.engine, gpu=voice.gpu,
                              hq_model=voice.hq_model,
                              hq_takes=voice.hq_takes)
        self.synth.verify_with(self.recognizer)
        self.voices_installed()

    def load_flight_plan(self, username: str = "") -> str:
        """Fill the flight in from the latest SimBrief plan.

        Returns a line describing what was loaded. Raises
        :class:`~wilcoatc.integrations.simbrief.SimBriefError` with something
        worth showing a person if it could not be.
        """
        from .integrations.simbrief import fetch

        plan = fetch(username or self.config.simbrief.username)
        self.apply_flight_plan(plan)
        self.emit("info", f"SimBrief: {plan.described}",
                  key="log.simbrief", detail=plan.described)
        return plan.described

    def load_plan_on_start(self) -> None:
        """The latest SimBrief plan, when the pilot asked for it at startup.

        The same path as the panel's button (:meth:`load_flight_plan`), on a
        thread of its own because it is a network fetch and the radio must
        not wait for it. The session is begun again with what it filled in,
        exactly as a change to the flight on the screen does.
        """
        self._plan_thread = None
        simbrief = self.config.simbrief
        if not (simbrief.load_on_start and simbrief.username.strip()):
            return

        def fetch() -> None:
            from .integrations.simbrief import SimBriefError

            try:
                self.load_flight_plan(simbrief.username.strip())
            except SimBriefError as why:
                self.emit("warning", f"SimBrief: {why}",
                          key="log.simbrief_failed", detail=str(why))
                return
            except Exception as exc:
                log.warning("SimBrief at startup failed: %s", exc)
                return
            self.begin_session()

        self._plan_thread = threading.Thread(
            target=fetch, name="WilcoATC-simbrief", daemon=True)
        self._plan_thread.start()

    def apply_flight_plan(self, plan) -> None:
        """Fill the flight in from a parsed plan."""
        flight = self.config.flight
        if plan.callsign:
            flight.callsign = plan.callsign
        if plan.aircraft_type:
            flight.aircraft_type = plan.aircraft_type
        flight.departure = plan.origin
        flight.destination = plan.destination
        if plan.cruise_altitude_ft:
            flight.cruise_altitude_ft = plan.cruise_altitude_ft
        # A filed plan that says which rules it was filed under outranks the
        # setting: a pilot who has loaded a flight plan has said more about
        # this flight than the config file ever did. One that does not say
        # leaves the pilot's choice alone -- it used to make every such plan
        # IFR, whatever the pilot of a VFR flight had chosen.
        if plan.ifr is not None:
            flight.ifr = plan.ifr
        self.plan = plan
        from .atc.route import from_ofp

        self.use_route(from_ofp(plan))
        # The route's fixes, which are what a centre grants a direct to.
        from .integrations.simbrief import route_fixes

        self.brain.plan_fixes = route_fixes(plan)
        # The planned runways and SID, which the controllers use while the
        # wind allows them.
        self.brain.plan_runways = {}
        if plan.origin:
            self.brain.plan_runways[plan.origin] = {
                "departing": plan.planned_runway, "sid": plan.sid}
        if plan.destination:
            self.brain.plan_runways.setdefault(plan.destination, {})
            self.brain.plan_runways[plan.destination]["landing"] = \
                plan.arrival_runway

        # A session already under way is rebuilt so the new callsign is what
        # the controller answers to from the next transmission.
        if self.session is not None:
            self.begin_session()

    def attach_simulator_traffic(self) -> bool:
        """Let the other aeroplanes be seen, not only heard.

        Needs the live SimConnect session, so it is done once the source is
        connected. A simulator that will not have them leaves the radio alone.
        """
        simconnect = getattr(self.source, "_sim", None)
        if simconnect is None:
            return False
        # Reading is attached whichever way the writing goes: a pilot who has
        # switched the injected traffic off still has FSLTL running.
        watching = self.simwatch.attach(simconnect)
        # The stands are not set out until the simulator has said what is
        # already standing on them (or has had its chance to).
        self.traffic.await_observed = watching
        self.use_fsltl_liveries()
        # Logged rather than shown. "FSLTL is running and the controller is
        # not seeing it" has four possible causes and none of them is
        # something a pilot can act on from the panel, so it goes where
        # somebody reporting the problem will find it.
        log.info("%s", self.simwatch.describe())
        drawing = self.simtraffic.attach(simconnect)
        if drawing:
            self._read_ai_liveries()
            # Nothing to handle unless the aeroplanes are drawn.
            self.simvehicles.attach(simconnect)
            self.simdoors.attach(simconnect)
            self.simsystems.attach(simconnect)
            self.servicepoints.attach(simconnect)
            log.info("%s", self.simdoors.describe())
        return drawing or watching

    # How long the simulator is given to list its aircraft and liveries.
    # A running 2024 lists six thousand in a second or two.
    AI_LIVERIES_S = 20.0

    def _read_ai_liveries(self) -> None:
        """Learn which airlines the simulator's own AI models are painted
        for, on a worker, once per session.

        Until it answers -- and for ever, on a library without the call --
        the detailed models are drawn in their default livery, as they
        always were.
        """
        if self.simtraffic.ai_liveries is not None or self._ai_liveries_asked:
            return
        self._ai_liveries_asked = True
        from .integrations.ailiveries import AiLiveries
        from .integrations.groundvehicles import enumerate_simobjects

        def read() -> None:
            try:
                pairs = enumerate_simobjects(
                    2, self.config.sim.simconnect_dll,
                    timeout_s=self.AI_LIVERIES_S,
                    name=b"WilcoATC liveries")
            except Exception:
                log.info("listing the simulator's aircraft raised",
                         exc_info=True)
                return
            if not pairs:
                log.info("the simulator did not list its aircraft; the "
                         "detailed models keep their default livery")
                return
            found = AiLiveries.from_pairs(pairs)
            self.simtraffic.ai_liveries = found
            log.info("AI liveries: %d models painted for airlines, out of "
                     "%d aircraft listed", len(found), len(pairs))

        threading.Thread(target=read, name="WilcoATC-ai-liveries",
                         daemon=True).start()

    def use_fsltl_liveries(self) -> bool:
        """Paint the traffic in its airlines' colours, if FSLTL is here.

        Read once per session: the table is eleven megabytes and does not
        change while the simulator is running.
        """
        if self.simtraffic.liveries is not None:
            return True
        from .integrations.fsltl import load_installed

        try:
            liveries = load_installed(
                override=self.config.traffic.fsltl_rules)
        except Exception:
            log.info("could not read FSLTL's liveries", exc_info=True)
            liveries = None
        if not liveries:
            log.info("FSLTL is not installed; the traffic uses the "
                     "simulator's own models")
            return False
        self.simtraffic.liveries = liveries
        self.traffic.has_livery = liveries.has_livery
        log.info("FSLTL liveries from %s (%d rules)", liveries.source,
                 len(liveries))
        self.emit("info",
                  f"Using FSLTL's liveries for {liveries.airlines} airlines.",
                  key="log.fsltl_liveries", count=str(liveries.airlines))
        return True

    def open_simulator_frequencies(self) -> bool:
        """Get ready to read airport frequencies out of the simulator.

        Needs a SimConnect library new enough to answer facility questions.
        Without one the published data is used and nothing is said about it,
        because there is nothing the pilot can do differently.
        """
        if not self.config.sim.frequencies_from_sim:
            return False
        return self.facilities.open()

    def follow_facilities(self, state: AircraftState) -> None:
        """Read the airport under the aeroplane, once, in the background."""
        if not self.facilities.available or not state.has_position:
            return
        airport = self.navdb.home_airport(state.latitude, state.longitude)
        if airport is None:
            airport = self.navdb.nearest_airport(
                state.latitude, state.longitude, max_nm=30.0, min_rank=2)
        if airport is None or airport.ident in self._facilities_done:
            return
        self._facilities_done.add(airport.ident)
        threading.Thread(
            target=self._read_facilities, args=(airport.ident,),
            name=f"WilcoATC-freq-{airport.ident}", daemon=True).start()

    def _read_facilities(self, ident: str) -> None:
        """Replace one airport's published frequencies with the simulator's."""
        from .navdata.localdata import replace_airport

        try:
            found = self.facilities.frequencies(ident)
            if not found:
                return
            added, gone = replace_airport(ident, found)
            if not added:
                return
        except Exception:
            log.debug("could not read %s from the simulator", ident,
                      exc_info=True)
            return

        self.emit("info",
                  f"{ident}: {added} frequencies from the simulator.",
                  key="log.frequencies_from_sim", airport=ident,
                  count=added)
        # Whatever is tuned was resolved against the old list, so it is
        # resolved again -- this is how 123.605 at de Gaulle starts answering.
        if self.state.com1_active:
            try:
                self.tune(self.state.com1_active)
            except Exception:
                log.debug("re-tuning after the import raised", exc_info=True)

    def attach_lvar_bridge(self) -> str:
        """Give the ground handling a way to read the simulator's variables.

        Called when the simulator arrives, because until it has there is
        nothing to open a channel to. Safe to call again: a bridge that is
        already reading is kept, and a failed attempt leaves the ground
        handling exactly where it was -- watching the aeroplane's own
        movement, which needs nothing installed.
        """
        if not self.config.gsx.enabled:
            return ""
        existing = self.gsx.bridge
        if existing is not None and getattr(existing, "available", False):
            return existing.name

        from .integrations.lvars import open_bridge

        simconnect = getattr(self.source, "_sim", None)
        bridge = open_bridge(self.config.gsx.bridge,
                             self.config.gsx.lvar_file, simconnect,
                             client=self.config.gsx.client)
        self.gsx.attach(bridge if bridge.available else None)
        if not bridge.available:
            # Not an error and not shown. A pilot without the MobiFlight
            # module has a controller that still watches the ramp through the
            # aeroplane, and there is nothing here for them to act on that
            # ``doctor`` does not say better.
            log.info("nothing can read the simulator's local variables")
            return ""
        log.info("ground handling read through %s", bridge.name)
        return bridge.name

    def start(self, connect_sim: bool = True) -> None:
        """Connect everything and begin the situation loop."""
        self.player.start()
        # The simulator's runways kept from earlier sessions, ahead of the
        # database's (navdata/runway_cache.py). Here and not in __init__, so
        # an engine a test builds reads the database whatever is cached.
        self._use_runway_cache()

        self._sim_wanted = bool(connect_sim and self.config.sim.enabled)
        if self._sim_wanted and not self.attach_simulator():
            self.emit(
                "info",
                "Simulator not detected. Waiting for it, and flying from the "
                "manually set aircraft state until it appears.",
                key="log.sim_waiting",
            )
        if self.config.immersion.enabled:
            self.cabin.start()
        self.refresh_state()
        self.begin_session()
        self.load_plan_on_start()
        self.start_live_feed()
        self.emit("info", self.understanding.describe(),
                  key=("set.rules_plus" if self.understanding.model_available
                       else "set.rules_only"),
                  model=(f"{self.understanding.provider.name} "
                         f"({getattr(self.understanding.provider, 'model', '?')})"
                         if self.understanding.model_available else ""))
        self._start_ptt()

        self._stop.clear()
        self._situation_thread = threading.Thread(
            target=self._situation_loop, daemon=True
        )
        self._situation_thread.start()

    def _ptt_controller_lost(self, controller) -> None:
        """The push-to-talk controller was unplugged mid-session. Said, not
        left silently dead: a pilot pressing a button that does nothing
        would otherwise think the radio had failed."""
        self.emit("warning", f"Push-to-talk: {controller.name} was "
                  "disconnected.", key="log.ptt_controller_lost",
                  name=controller.name)

    def _ptt_controller_back(self, controller) -> None:
        self.emit("info", f"Push-to-talk: {controller.name} is connected "
                  "again.", key="log.ptt_controller_back",
                  name=controller.name)

    def _start_ptt(self) -> None:
        mode = self.config.ptt.mode
        if mode == "none":
            return
        try:
            if mode == "joystick":
                self._ptt = JoystickPushToTalk(
                    joystick_index=self.config.ptt.joystick_index,
                    joystick_id=self.config.ptt.joystick_id,
                    joystick_name=self.config.ptt.joystick_name,
                    button=self.config.ptt.joystick_button,
                    on_press=self.begin_transmission,
                    on_release=self.end_transmission,
                    on_lost=self._ptt_controller_lost,
                    on_back=self._ptt_controller_back,
                )
            else:
                self._ptt = PushToTalk(
                    key=self.config.ptt.key,
                    scan_code=self.config.ptt.scan_code,
                    on_press=self.begin_transmission,
                    on_release=self.end_transmission,
                )
            self._ptt.start()
            # The label goes inside a translated sentence, so it is the bare
            # key or button rather than an English phrase around one.
            if mode == "joystick":
                controller = getattr(self._ptt, "controller", None)
                stick = controller.name if controller is not None \
                    else str(self.config.ptt.joystick_index)
                label = f"{stick} button {self.config.ptt.joystick_button}"
                self.emit("info", f"Push to talk: hold {label}.",
                          key="log.ptt_button", stick=stick,
                          button=str(self.config.ptt.joystick_button))
            else:
                label = f"the {self.config.ptt.key} key"
                self.emit("info", f"Push to talk: hold {label}.",
                          key="log.ptt", key_name=self.config.ptt.key)
        except ControllerMissing as exc:
            # The controller it is set to is not plugged in. Said by name,
            # in the pilot's language; it is picked up when it arrives only
            # by setting it again, so the panel's list is where to go.
            self.emit("error", f"Push-to-talk: {exc.wanted} is not connected.",
                      key="log.ptt_controller_missing", name=exc.wanted)
            self._ptt = None
        except ControllersUnavailable as exc:
            if exc.reason == "missing":
                self.emit("error", "Joystick support is missing from this "
                          "installation. Please report it.",
                          key="log.ptt_joystick_missing")
            else:
                self.emit("error", f"Push-to-talk unavailable: {exc.reason}",
                          key="log.ptt_failed", detail=exc.reason)
            self._ptt = None
        except Exception as exc:
            self.emit("error", f"Push-to-talk unavailable: {exc}",
                       key="log.ptt_failed", detail=exc)
            self._ptt = None

    # ------------------------------------------------------------------
    # the simulator link, watched rather than assumed
    # ------------------------------------------------------------------

    def attach_simulator(self) -> bool:
        """Try to take the aircraft state from the simulator, now.

        Returns whether it worked. Safe to call repeatedly: a failed attempt
        leaves whatever source was already in place, so a flight that started
        without a simulator carries on rather than losing its position to a
        half-built connection.
        """
        sim = SimConnectSource(poll_interval=self.config.sim.poll_interval_s,
                               dll_path=self.config.sim.simconnect_dll)
        if not sim.connect():
            sim.close()
            return False

        previous = self.source
        self.source = sim
        self._sim_source = sim
        self._sim_was_connected = True
        # Read once before anything is told the sim is here, so the first
        # thing that asks gets the aeroplane's real position rather than the
        # placeholder the manual source was holding.
        self.refresh_state()
        if previous is not sim:
            try:
                previous.close()
            except Exception:
                log.debug("could not close the previous state source",
                          exc_info=True)

        self.emit("info", "Connected to the simulator.", key="log.sim_connected")
        # The ground handling, which needs the simulator to be there before
        # it can open a channel of its own. It used to be written and never
        # called, which is the whole of why GSX appeared to do nothing: the
        # variables were never read and the menu was never reachable.
        try:
            self.attach_lvar_bridge()
        except Exception:
            log.debug("could not attach the local variable bridge",
                      exc_info=True)
        if self.attach_simulator_traffic():
            self.emit("info", "Traffic will be visible in the simulator.",
                      key="log.traffic_visible")
        self.open_simulator_frequencies()
        # The airports whose frequencies were read from the last session are
        # not this session's; ask again.
        self._facilities_done.clear()
        return True

    def want_simulator(self, wanted: bool) -> None:
        """Start or stop looking for the simulator, without a restart.

        Turning it on tries immediately rather than waiting out the
        reconnect interval, because a pilot who has just flicked the switch
        is watching for something to happen.
        """
        self._sim_wanted = bool(wanted)
        if not self._sim_wanted:
            self._next_sim_try = 0.0
            return
        self._next_sim_try = 0.0
        if self.source is not self._sim_source and self.attach_simulator():
            self.begin_session()

    # The shortest the engine will wait between attempts, whatever the
    # config says. Asking SimConnect for a connection it cannot give costs a
    # library call and a timeout, and doing that four times a second while
    # the simulator loads is how a panel becomes unresponsive waiting for the
    # thing it is waiting for.
    MIN_SIM_RETRY_S = 2.0

    def _retry_interval(self) -> float:
        return max(self.MIN_SIM_RETRY_S, self.config.sim.reconnect_interval_s)

    def _watch_simulator(self) -> None:
        """Connect when the simulator appears, and notice when it goes.

        This is the whole of the fix for a panel that sat at the departure
        airport with numbers that never moved. The old code connected once,
        during startup, and a pilot who opened this before the simulator had
        finished loading -- which is nearly everybody -- got the manual source
        for the entire flight with no way back.
        """
        if not self._sim_wanted:
            return
        now = time.monotonic()

        link = self._sim_source
        if link is not None and self.source is link:
            if link.connected:
                return
            # It was there and has gone. Say so once, drop back to a manual
            # source holding the last known position, and start looking
            # again. The position is kept rather than zeroed: it is the best
            # guess there is, and zeroing it puts the aeroplane in the
            # Atlantic and every frequency out of range.
            self._sim_was_connected = False
            self.emit("info", "Lost the simulator. Looking for it again.",
                      key="log.sim_lost")
            held = replace(self.state)
            held.connected = True
            held.source = "manual"
            try:
                link.close()
            except Exception:
                log.debug("could not close the simulator source", exc_info=True)
            self.source = ManualSource(held)
            self._sim_source = None
            self._next_sim_try = now + self._retry_interval()
            return

        if now < self._next_sim_try:
            return
        self._next_sim_try = now + self._retry_interval()
        if self.attach_simulator():
            # A flight built around a typed departure field is not the flight
            # the simulator is holding. Rebuild it from what is actually
            # there, which is what stops the panel insisting on the old
            # airport once the aeroplane is somewhere else.
            self.begin_session()

    # ------------------------------------------------------------------

    def rebind_ptt(self) -> None:
        """Move push-to-talk onto whatever the settings now say.

        Picking a different button used to save the number and change
        nothing: the listener was built once at startup, so the old button
        went on keying the microphone and the new one did nothing at all.
        From the pilot's side that is indistinguishable from the setting
        being ignored, because it was.

        Also the way a controller plugged in after startup gets used, and the
        way a failed binding recovers -- both leave no listener running, and
        both are fixed by building a new one.
        """
        if self._situation_thread is None or self._stop.is_set():
            return          # not running yet: start() will do this properly
        if self._ptt is not None:
            try:
                self._ptt.stop()
            except Exception as exc:
                log.debug("could not stop push-to-talk: %s", exc)
            self._ptt = None
        # A transmission keyed on the old button has nothing left to unkey
        # it, and would otherwise record until the microphone's own limit.
        self.end_transmission()
        self._start_ptt()

    # ------------------------------------------------------------------
    # the real aeroplanes round the pilot (integrations/adsb.py)
    # ------------------------------------------------------------------

    live_feed = None
    _live_said: tuple = ("", "")
    _live_down_said = False

    def start_live_feed(self, run: bool = True):
        """Start asking the ADS-B sources, when the pilot has switched live
        traffic on and this program flies the traffic. With FSLTL flying it
        (traffic.source) the feed is never asked: the two together would put
        a second copy of every real flight in the sky."""
        from .integrations import adsb
        from .integrations.fsltl import traffic_source

        live = self.config.traffic.live
        if not live.enabled or self.live_feed is not None:
            return None
        source = traffic_source(self.config.traffic)
        if source != "wilcoatc":
            decision("live_feed", event="not started", source=source,
                     why="traffic.source: FSLTL flies the traffic")
            self.emit("info", "Live traffic stays off: FSLTL flies the "
                      "traffic (Settings, Traffic).", key="log.live_fsltl")
            return None
        from .paths import data_dir

        feed = adsb.Feed(sources=adsb.sources_for(live.own_url),
                         radius_nm=live.radius_nm)
        book = (adsb.FlightBook(data_dir() / "adsb_cache.json")
                if live.routes else None)
        self.live_feed = adsb.LiveFeedThread(feed, book, self._live_centre,
                                             poll_s=live.poll_s)
        decision("live_feed", event="started", radius_nm=feed.radius_nm,
                 poll_s=self.live_feed.poll_s,
                 sources=[s.name for s in feed.sources])
        if run:
            self.live_feed.start()
        return self.live_feed

    def _take_observed(self, session) -> None:
        """Hand the traffic everything somebody else flies: the simulator's
        aeroplanes (FSLTL, AIG, its own) and the real ones from the live feed,
        in one list, because set_observed finishes the strip of anything it
        is not given."""
        self.simwatch.enabled = self.config.traffic.from_simulator
        seen: list = []
        if self.config.traffic.from_simulator:
            self.simwatch.own_callsign = session.aircraft.callsign.written
            try:
                seen = list(self.simwatch.poll() or ())
            except Exception:
                log.debug("could not read the simulator's traffic",
                          exc_info=True)
        live = self._live_seen()
        if not self.config.traffic.from_simulator and self.live_feed is None:
            return
        try:
            self.traffic.set_observed(seen + live, time.monotonic())
        except Exception:
            log.debug("could not take the observed traffic", exc_info=True)
        if self.config.traffic.from_simulator:
            self._say_observed()

    live_roster = None

    def _live_seen(self) -> list:
        """The live flights for this moment (atc/live.py): the feed's last
        answer carried on, held through a quiet minute, then only an arrival
        on final, landed by us."""
        if self.live_feed is None:
            return []
        from .atc.live import LiveRoster

        if self.live_roster is None:
            live = self.config.traffic.live
            self.live_roster = LiveRoster(live.fallback_after_s,
                                          delay_s=live.live_delay_s)
            roster = self.live_roster
            # How our tower holds one short of the pilot's runway, or has it
            # give way, and lets it go (TrafficInjector._protect_the_pilot).
            self.traffic.hold_live = lambda hex_, on: (
                roster.hold(hex_, time.time()) if on
                else roster.release(hex_, time.time()))
        feed = self.live_feed
        self.live_roster.update(feed.flights, feed.answered_wall, feed.routes)
        field = self.traffic.field if self.traffic.field.ident else None
        seen = self.live_roster.seen(time.time(), field)
        source = feed.state.source
        for one in seen:
            one.feed = source
        return seen

    def _live_centre(self):
        """Where the feed asks round: the aeroplane, else the field it is
        departing; nothing (and nothing sent) before either is known."""
        state = self.state
        if state is not None and state.has_position:
            return state.latitude, state.longitude
        field = self.session.departure if self.session is not None else None
        if field is not None:
            return field.lat, field.lon
        return None

    def _watch_live_feed(self) -> None:
        """A row each time the feed's state or source changes, and the
        outage said once, on the panel, in the pilot's language."""
        if self.live_feed is None:
            return
        from .integrations.adsb import DOWN

        state = self.live_feed.state
        now = (state.status, state.source)
        if now == self._live_said:
            return
        self._live_said = now
        decision("live_feed", event="state", status=state.status,
                 source=state.source, count=state.count, age_s=state.age_s,
                 resting=[list(r) for r in state.resting],
                 dropped=list(state.dropped))
        if state.status == DOWN and not self._live_down_said:
            self._live_down_said = True
            self.emit("warning", "Live traffic: no source answering, invented "
                      "traffic in use.", key="log.live_down")
        elif state.status != DOWN and self._live_down_said and state.source:
            self._live_down_said = False
            self.emit("info", f"Live traffic is back, from {state.source}.",
                      key="log.live_back", source=state.source)

    def stop(self) -> None:
        self._stop.set()
        if self.live_feed is not None:
            self.live_feed.stop()
        self.cabin.stop()
        if self._ptt is not None:
            self._ptt.stop()
        if self._situation_thread is not None:
            self._situation_thread.join(timeout=2.0)
        self.player.stop()
        # Take the injected aeroplanes back out before the connection goes, or
        # they are left sitting in the simulator with nobody flying them.
        self.simvehicles.detach()
        self.simdoors.detach()
        self.simsystems.detach()
        self.servicepoints.detach()
        self.simtraffic.detach()
        self.simwatch.detach()
        self.facilities.close()
        self.source.close()

    # ------------------------------------------------------------------
    # session
    # ------------------------------------------------------------------

    # How far the aeroplane may be from the field the flight says it is
    # departing before the field is treated as wrong rather than the aeroplane
    # as taxiing. Generous: a long taxi at a big airport is a few miles, and a
    # flight that is already airborne on the way somewhere is not a mistake.
    DEPARTURE_TOLERANCE_NM = 30.0

    def _plausible_departure(self, airport) -> bool:
        """Whether the aircraft could actually be departing this field.

        Airborne is always plausible: a flight can be an hour out of the field
        it departed. It is only an aeroplane sitting on the ground somewhere
        else that says the departure field is stale.
        """
        if airport is None or not self.state.has_position:
            return True
        if not self.state.on_ground:
            return True
        distance = haversine_nm(self.state.latitude, self.state.longitude,
                                airport.lat, airport.lon)
        return distance <= self.DEPARTURE_TOLERANCE_NM

    def use_region(self) -> None:
        """A region pinned or unpinned on the screen, from the next
        transmission, without rebuilding the flight."""
        profile = FlightProfile.of(self.config.flight)
        self.brain.profile = profile
        self.traffic.profile = profile
        if self.session is not None:
            self.session.profile = profile

    def _rules_for(self, flight) -> bool:
        """The rules a rebuilt session flies under.

        Every save of the flight, every reconnection of the simulator and
        every setting under ``flight`` rebuilds the session, and each one
        used to put the rules back to the setting -- so a VFR release, or
        "IFR" said on the radio, lasted until the pilot next pressed Save.
        What the radio changed is kept unless the setting itself has moved
        since the session was built, which is the pilot changing them.
        """
        old = self.session
        if old is not None and old.ifr_setting == flight.ifr:
            return old.ifr
        return flight.ifr

    def begin_session(self) -> FlightSession:
        """Build the flight session from the sim state and the config."""
        flight = self.config.flight
        profile = FlightProfile.of(flight)
        self.brain.profile = profile
        self.traffic.profile = profile
        # Whether this is an airline flight is the pilot's to say. In general
        # aviation the simulator's airline-and-flight-number callsign is the
        # livery's, not the flight's, so it is the tail -- unless the pilot
        # typed a company callsign themselves (an EJA123 business jet).
        if profile.airliner:
            raw_callsign = flight.callsign or self.state.callsign or "N123AB"
            as_airline = True
        else:
            raw_callsign = (flight.callsign or self.state.tail_number
                            or self.state.callsign or "N123AB")
            as_airline = bool(flight.callsign)

        # The aircraft type follows the simulator, not the config file.
        #
        # This is not a preference the way the callsign is. The type decides
        # the wake-turbulence suffix, how the controller sequences you and
        # what it expects you to be able to do, and there is no version of
        # "I am a Cessna 172" that is true while the simulator is holding an
        # A321. A typed type is the fallback for when the simulator is not
        # there, and it is corrected -- and written back, so the settings
        # screen stops disagreeing with the aeroplane -- when it is.
        type_code = self.state.type_code or flight.aircraft_type or ""
        if (self.state.type_code
                and flight.aircraft_type != self.state.type_code):
            if flight.aircraft_type:
                self.emit("info",
                          f"The simulator has a {self.state.type_code}, not a "
                          f"{flight.aircraft_type}.",
                          key="log.type_moved", type=self.state.type_code,
                          was=flight.aircraft_type)
            self.config.flight.aircraft_type = self.state.type_code
            flight = self.config.flight

        departure = self.navdb.airport(flight.departure) if flight.departure else None
        if self.state.has_position:
            under = self.navdb.home_airport(self.state.latitude, self.state.longitude)
            if departure is None:
                departure = under
            elif not self._plausible_departure(departure):
                # The aeroplane is not where the flight says it is starting.
                # The simulator is the authority on where it is, so the typed
                # field is corrected rather than believed -- otherwise the
                # panel reports an airport half a continent away, resolves
                # every frequency against it, and nothing on the radio works.
                moved = under or self.navdb.nearest_airport(
                    self.state.latitude, self.state.longitude, max_nm=60.0)
                if moved is not None and moved.ident != departure.ident:
                    self.emit(
                        "info",
                        f"The aircraft is at {moved.ident}, not "
                        f"{departure.ident}. Using {moved.ident}.",
                        key="log.departure_moved",
                        airport=moved.ident, was=departure.ident,
                    )
                    departure = moved
                    self.config.flight.departure = moved.ident
                    flight = self.config.flight

        # Without a simulator there is no position, so nothing resolves and the
        # radio is dead. If a departure airport was named, park the aircraft
        # there: it is the only place the flight could sensibly be starting.
        if (departure is not None and not self.state.has_position
                and hasattr(self.source, "update")):
            self.source.update(
                latitude=departure.lat, longitude=departure.lon,
                altitude_ft=departure.elev_ft, on_ground=True,
            )
            self.refresh_state()
            self.emit("info", f"No simulator, so placing the aircraft at "
                              f"{departure.ident}.",
                      key="log.placed", airport=departure.ident)

        # The callsign in every language a controller might use, built once.
        # Which one gets used is decided per transmission, by where the
        # aeroplane is and what the pilot said.
        #
        # Built here rather than earlier because it needs the departure field,
        # and the departure field is not settled until the aeroplane has been
        # found: where the flight starts decides whether English groups the
        # flight number into words -- "Delta twelve thirty four" -- or reads it
        # out in numerals. The group form is a US rule and was being applied
        # everywhere, so an English-speaking controller in Czech airspace said
        # it the American way.
        alternatives = every_language(
            raw_callsign, type_code,
            dialect=profile.dialect_for(departure.ident if departure else ""),
            airline=as_airline)
        callsign = alternatives["en"]
        aircraft = Aircraft(callsign, type_code, wake_suffix(type_code),
                            callsigns=alternatives)

        destination = (
            self.navdb.airport(flight.destination) if flight.destination else None
        )

        session = FlightSession(
            aircraft=aircraft,
            departure=departure,
            destination=destination,
            destination_input=flight.destination,
            cruise_altitude_ft=float(flight.cruise_altitude_ft or 0),
            ifr=self._rules_for(flight),
            ifr_setting=flight.ifr,
            profile=profile,
        )
        session.phase = Phase.PREFLIGHT if self.state.on_ground else Phase.ENROUTE
        self.session = session

        self.understanding.set_callsign(aircraft.variants())
        # A new flight is a new crew: whatever the last one had already said
        # is forgotten, or the safety briefing never happens again.
        self.crew.begin(session, self.last_language)
        self._check_announcements()
        # And a new runway, so a departure held on the last flight is not
        # still waiting for it to clear on this one.
        self.departures.began()
        self._centre_looked_at = 0.0
        self._centre_checked_at = 0.0
        self._sector_noted = None

        where = departure.ident if departure else "an unknown field"
        self.emit(
            "info",
            f"Flight {callsign.written} ({callsign.spoken_full}) at {where}"
            + (f", destination {destination.ident}." if destination else "."),
            key=("log.flight_to" if destination else "log.flight"),
            callsign=callsign.written, spoken=callsign.spoken_full,
            airport=where,
            destination=(destination.ident if destination else ""),
        )
        if departure is not None:
            self.follow_languages(departure.ident, announce=True)
        self.route_progress = self.brain.route_progress = None
        if not self.route and session.ifr:
            # Said, because every distance the controller uses is then to the
            # airport's reference point rather than along a route.
            decision("route", event="none",
                     why="no flight plan loaded: distances are to the airport")
        return session

    def use_route(self, route) -> None:
        """Take a route as the flight's, and say so in the transcript."""
        self.route = route if route else None
        self.brain.route = self.route
        self.route_progress = self.brain.route_progress = None
        if self.route is None:
            decision("route", event="none", source=getattr(route, "source", ""),
                     why="the plan has fewer than two placed fixes")
            return
        decision("route", event="loaded", source=route.source,
                 route=route.described(), fixes=len(route.fixes),
                 sid=route.sid, sid_runway=route.sid_runway, star=route.star,
                 star_runway=route.star_runway, approach=route.approach,
                 cruise_ft=route.cruise_ft,
                 toc=getattr(route.top_of_climb, "ident", ""),
                 tod=getattr(route.top_of_descent, "ident", ""),
                 floor_ft=route.arrival_floor_ft or 0,
                 notes=list(route.notes))

    def load_route_file(self, path) -> str:
        """A simulator flight plan (``.pln``) on disk as the flight's route."""
        from pathlib import Path

        return self.load_route_text(
            Path(path).read_text(encoding="utf-8-sig", errors="replace"))

    def load_route_text(self, text: str) -> str:
        """A simulator flight plan (``.pln``) as the flight's route: the
        file's text, which is what the panel's button sends.

        Each fix the file does not place itself is asked of the simulator,
        then of the database (atc/route.py ``resolver``); what neither knows
        is listed in the route's notes. Returns the route, described. Raises
        :class:`NotAFlightPlan` for a file that is not one, so the panel can
        say so in the pilot's language rather than show a parser's error.
        """
        from xml.etree.ElementTree import ParseError

        from .atc.route import from_pln, resolver

        sim = None
        if self.config.sim.enabled:
            from .integrations.fixes import SimFixes

            sim = SimFixes(self.config.sim.simconnect_dll)
        departure = self.navdb.airport(self.config.flight.departure or "")
        near = (departure.lat, departure.lon) if departure else None
        try:
            route = from_pln(text, resolver(self.navdb, sim, near=near))
        except ParseError as exc:
            raise NotAFlightPlan(str(exc)) from exc
        if not (route.origin or route.destination or route.fixes):
            raise NotAFlightPlan("no departure, destination or fix")
        flight = self.config.flight
        if route.origin:
            flight.departure = route.origin
        if route.destination:
            flight.destination = route.destination
        if route.cruise_ft:
            flight.cruise_altitude_ft = route.cruise_ft
        self.brain.plan_runways = {}
        if route.origin:
            self.brain.plan_runways[route.origin] = {
                "departing": route.sid_runway, "sid": route.sid}
        if route.destination:
            self.brain.plan_runways.setdefault(route.destination, {})
            self.brain.plan_runways[route.destination]["landing"] = \
                route.star_runway
        self.use_route(route)
        if self.session is not None:
            self.begin_session()
        return route.described()

    def _follow_route(self, state: AircraftState) -> None:
        """Where the aeroplane is along the route; a row at each new leg."""
        route = self.route
        if not route or state.on_ground:
            return
        last = self.route_progress
        found = route.progress(state.latitude, state.longitude,
                               state.ground_speed_kt or 0.0,
                               hint=last.leg if last is not None else 1)
        if found is None:
            return
        self.route_progress = found
        self.brain.route_progress = found
        if last is None or found.leg != last.leg:
            decision("route", event="leg", leg=found.leg_name,
                     via=found.to_fix.via,
                     cross_track_nm=round(found.cross_track_nm, 1),
                     to_go_nm=round(found.to_go_nm, 1),
                     altitude_ft=round(state.altitude_ft))

    # ------------------------------------------------------------------
    # the language follows the aeroplane
    # ------------------------------------------------------------------

    def follow_languages(self, ident: str, announce: bool = False) -> list[str]:
        """Listen for whatever is worked where the aeroplane now is.

        English is always in the set. The local language joins it over a state
        that works one this system can speak, and drops out again on the way
        out. Nothing about this is configurable, because nothing about it is a
        preference: it is where you are.
        """
        worked = languages_for(ident)
        if self.pinned_languages:
            worked = [l for l in worked if l in self.pinned_languages] or ["en"]
        if worked == self.languages:
            return worked

        self.languages = worked
        self.recognizer.set_languages(worked)
        if self.last_language not in worked:
            self.last_language = worked[0]

        if announce or len(worked) > 1:
            local = [l for l in worked if l != "en"]
            if local:
                name = LANGUAGE_NAMES.get(local[0], local[0])
                self.emit("info",
                          f"{profile_for(ident).name}: {name} is worked here "
                          f"alongside English. Speak either; the controller "
                          f"answers in the one you used.",
                          key="log.bilingual", where=profile_for(ident).name,
                          language=local[0])
            elif announce:
                self.emit("info", f"{profile_for(ident).name}: English on frequency.",
                          key="log.english_only", where=profile_for(ident).name)
        return worked

    # ------------------------------------------------------------------
    # state and weather
    # ------------------------------------------------------------------

    def refresh_state(self) -> AircraftState:
        try:
            self.state = self.source.read()
        except Exception as exc:
            log.debug("state read failed: %s", exc)
        return self.state

    def weather_for(self, airport_ident: str) -> Weather:
        """Current weather at a field.

        The simulator reports what is around the aeroplane, so it speaks for
        a field only when the aeroplane is at it or about to touch it (see
        :func:`~wilcoatc.weather.metar.near_the_field`). Everywhere else the
        field's own observation, then the nearest one, then calm and standard
        with the simulator's sea-level pressure. Laying the ambient values
        over every field read the jet stream out as a destination's surface
        wind and rolled the ATIS letter every thousand feet of descent.

        Never waits for the network: a fetch that is due is started on a
        worker and a later look picks it up.
        """
        cached = self._weather_cache.get(airport_ident)
        now = time.monotonic()
        checked = self._weather_checked.get(airport_ident)
        if cached is not None and checked is not None and (now - checked) < 60.0:
            return cached

        airport = self.navdb.airport(airport_ident)
        station = self.config.weather.station_override or (
            airport.icao if airport and airport.icao else airport_ident
        )
        sample = self.weather_provider.metar(station, wait=False)
        if sample.source == "default" and airport is not None:
            sample = self.weather_provider.nearest_metar(
                airport.lat, airport.lon, self.navdb, wait=False)

        sim_weather = (weather_from_sim(self.state)
                       if self.config.weather.prefer_sim else None)
        # A field with no report of its own: in its circuit the simulator's
        # wind is the only one that is this field's, so it is used in the air
        # too (in_the_circuit); beyond it, the nearest observation.
        own_report = (airport is not None and sample.source in ("metar", "cache")
                      and getattr(sample, "station", "")
                      == (airport.icao or airport.ident))
        circuit = (not own_report and sim_weather is not None
                   and in_the_circuit(self.state, airport))
        if sim_weather is not None and (near_the_field(self.state, airport)
                                        or circuit):
            sample = merge_weather(sim_weather, sample,
                                   surface=bool(self.state.on_ground),
                                   wind=(at_the_surface(self.state, airport)
                                         or circuit))
        elif sample.source == "default" and sim_weather is not None:
            # Nothing observed anywhere near. Sea-level pressure is a regional
            # figure and the simulator's is as good as any; its wind and
            # temperature are the aeroplane's and are not used.
            sample = replace(sample, weather=replace(
                sample.weather, qnh_hpa=sim_weather.qnh_hpa,
                altimeter_inhg=sim_weather.altimeter_inhg))

        # A copy, always: the provider caches the object it returned, and the
        # nearest-observation fallback hands the same one to every field near
        # it -- writing this field's variation into it gave them all the last
        # field's.
        weather = replace(sample.weather,
                                      clouds=list(sample.weather.clouds))
        if not weather.wind_from:
            weather.wind_from = dict.fromkeys(
                ("direction", "speed", "gust"),
                f"{sample.source} {getattr(sample, 'station', '') or ''}".strip())
        if airport is not None:
            # Spoken winds are magnetic; the observation is true.
            weather.variation = variation(airport.lat, airport.lon)
        station_name = getattr(sample, "station", "") or ""
        weather.source = {
            "metar": f"metar {station_name}", "cache": f"cached {station_name}",
            "sim": f"sim {station_name}".strip(), "default": "default",
        }.get(sample.source, sample.source)
        if sample.source == "sim":
            weather.source += (" (sim surface wind)"
                               if at_the_surface(self.state, airport)
                               else " (observed wind)")
        if (airport is not None and station_name
                and station_name != (airport.icao or airport.ident)
                and sample.source in ("metar", "cache")):
            weather.source = f"nearest {station_name}"
        self._weather_cache[airport_ident] = weather
        # A miss is looked at again in ten seconds, because the fetch it
        # started will have landed by then; a hit is good for the minute.
        self._weather_checked[airport_ident] = (
            now if sample.source != "default" else now - 50.0)
        return weather

    def atis_for(self, airport_ident: str,
                 language: str | None = None) -> AtisReport | None:
        """The current ATIS, in the requested language.

        A field is only broadcast in a language it actually works in, so asking
        for French at Kennedy quietly gives English rather than inventing a
        French ATIS that no such airport transmits.
        """
        airport = self.navdb.airport(airport_ident)
        if airport is None:
            return None

        # A controller on the network broadcasting their own ATIS is the
        # authority on this field: it names the runway they are actually
        # using and the letter they are actually expecting to hear back.
        # Generating one over the top would put two truths on one aerodrome.
        online = self._online_atis(airport_ident)
        if online is not None:
            return online

        language = self._language_for(airport_ident, language)
        # The pinned region is the broadcast's too: a pilot who pinned FAA
        # wording does not get ICAO from the ATIS.
        region = FlightProfile.of(self.config.flight).region
        key = (airport_ident, language, region)
        with self._atis_lock:
            if airport_ident in self._atis_stale:
                self._atis_stale.discard(airport_ident)
                for (field, _spoken), stale in self._atis.items():
                    if field == airport_ident:
                        stale.current = None
        generator = self._atis.get(key)
        if generator is None:
            from .atc.language import phraseology_for

            generator = AtisGenerator(
                self.navdb, airport,
                phraseology_for(language, airport_ident,
                                self.config.atc.strict_icao_digits,
                                region=region),
            )
            # The approach the controller will give: the pilot's own flight's
            # kept choice when it is landing here, and the field's weather,
            # so the broadcast says "ILS Zulu" when approach will.
            generator.approach_for = (
                lambda runway, field=airport: self.brain.approach_for(
                    self.session if self.session is not None
                    and getattr(self.session.destination, "ident", "")
                    == field.ident else None,
                    field, runway, self.weather_for(field.ident)))
            # The runway the controllers are using, flight plan included, so
            # the broadcast never names another.
            generator.runway_for = (
                lambda weather, departing, field=airport: (
                    self.brain.departure_runway(field, weather) if departing
                    else self.brain.arrival_runway(field, weather)))
            generator.vfr_briefing = (
                lambda runway, field=airport, p=generator.phrase:
                self.brain.atis_briefing(field, runway, p))
            self._atis[key] = generator
            self._ask_for_approaches(airport.ident)
        # Not under an aeroplane on final: a new letter there is a readback
        # nobody can give and a runway nobody can change to. It rolls once
        # the aeroplane is down.
        if generator.current is not None and self._on_final_at(airport_ident):
            return generator.current
        before = generator.current
        report = generator.update(self.weather_for(airport_ident))
        if report is not before:
            decision("atis", airport=airport_ident, letter=report.letter,
                     language=language, weather=report.weather.source,
                     wind_from=report.weather.wind_from or None,
                     runways=report.landing_runways[:1],
                     departing=report.departing_runways[:1],
                     approach=report.approach_in_use)
        return report

    # How far out "on final" begins, for holding the ATIS letter.
    FINAL_FOR_ATIS_NM = 15.0

    def _on_final_at(self, ident: str) -> bool:
        session = self.session
        state = self.state
        if session is None or state is None or state.on_ground:
            return False
        if session.phase not in (Phase.APPROACH, Phase.LANDING):
            return False
        field = self.navdb.airport(ident)
        if field is None or not state.has_position:
            return False
        destination = session.destination
        if destination is not None and destination.ident != field.ident \
                and (self.station is None or self.station.ident != field.ident):
            return False
        return state.distance_to(field.lat, field.lon) <= self.FINAL_FOR_ATIS_NM

    def _online_atis(self, ident: str) -> AtisReport | None:
        """The ATIS a network controller is broadcasting for this field."""
        if not self.online.enabled or not self.config.network.use_online_atis:
            return None
        found = self.online.atis_for(ident)
        if found is None or not found.atis:
            return None
        return AtisReport(
            airport=ident,
            letter=found.atis_letter or "",
            text=found.atis,
            weather=self.weather_for(ident),
        )

    def _language_for(self, ident: str, requested: str | None = None) -> str:
        """Resolve a language against what is worked at this facility."""
        wanted = (requested or self.last_language or "en").lower()[:2]
        return reply_language(ident, wanted,
                              self.pinned_languages or None)

    # ------------------------------------------------------------------
    # frequency
    # ------------------------------------------------------------------

    def set_radio(self, mhz: float, radio: int = 1,
                  standby: bool = False) -> tuple[bool, str]:
        """Put a frequency into the aeroplane's radio, and follow it here.

        The panel used to be able to tune only when there was no simulator: a
        frequency picked on the Frequencies screen was refused with a note
        saying COM1 belongs to the aircraft. It does -- but a radio is a thing
        you set, and SimConnect has an event for setting it. So the panel
        writes into the aircraft's radio and then reads it back like any other
        change, which means the number in the cockpit, the number on this
        screen and the controller who answers are the same three things.

        Returns whether it took, and a sentence saying why not when it did
        not.
        """
        try:
            value = round(float(mhz), 3)
        except (TypeError, ValueError):
            return False, "that is not a frequency"
        if not (118.0 <= value <= 136.999):
            return False, "that is not an airband frequency"

        setter = getattr(self.source, "set_com", None)
        if setter is None:
            return False, "this aircraft state source has no radios"
        if not setter(value, radio=radio, standby=standby):
            if getattr(self.source, "connected", False):
                return False, ("the simulator would not accept a frequency "
                               "for this aircraft")
            return False, "no aircraft to tune"

        # Believe the write until the next read confirms it. Without this the
        # panel shows the old frequency for a poll interval, which reads as
        # the button not having worked.
        if radio == 1 and not standby:
            self.state.com1_active = value
            self._tuned_by_us = value
            return True, ""
        field_name = f"com{int(radio)}_{'standby' if standby else 'active'}"
        if hasattr(self.state, field_name):
            setattr(self.state, field_name, value)
        return True, ""

    def swap_radio(self, radio: int = 1) -> tuple[bool, str]:
        """Swap active and standby in the aircraft, as the radio's button does."""
        swap = getattr(self.source, "swap_com", None)
        if swap is None or not swap(radio):
            return False, "no aircraft to tune"
        active = f"com{int(radio)}_active"
        standby = f"com{int(radio)}_standby"
        if hasattr(self.state, active) and hasattr(self.state, standby):
            was = getattr(self.state, active)
            setattr(self.state, active, getattr(self.state, standby))
            setattr(self.state, standby, was)
            if radio == 1:
                self._tuned_by_us = self.state.com1_active
        return True, ""

    def set_squawk(self, code: str) -> tuple[bool, str]:
        """Dial a transponder code into the aircraft."""
        setter = getattr(self.source, "set_squawk", None)
        if setter is None or not setter(code):
            return False, "the transponder would not take that code"
        self.state.transponder_code = "".join(
            c for c in str(code) if c.isdigit())[:4]
        return True, ""

    def spawn_on_runway(self) -> tuple[bool, str, str]:
        """Line an AI departure up on the runway in use, for the panel.

        Returns whether it was asked for, the callsign, and on refusal a
        reason key the panel maps to a ``comms.spawn_`` line in the
        catalogues. A busy runway is not a refusal: the aeroplane waits at
        the holding point. Nor is it on the runway yet: it goes into the
        simulator on the next tick, through the same sync every other flight
        takes, and the notice goes out once the simulator has drawn it -- or
        a different one if it cannot.
        """
        if not self.config.traffic.enabled:
            return False, "", "traffic_off"
        self.traffic.can_invent = self.simtraffic.drawing
        flight, why = self.traffic.spawn_on_runway(time.monotonic())
        if flight is None:
            return False, "", why
        # Said when the simulator has made it, not now: until then there is
        # nothing on the runway to look for.
        self._spawned[flight.written] = flight.runway
        return True, flight.written, ""

    def use_gsx_vehicles(self, wanted: bool) -> bool:
        """GSX Pro's vehicles for the traffic, or the simulator's own, from
        the panel and without a restart. Returns whether GSX's are now used:
        never without GSX installed, whatever was asked."""
        self.config.traffic.handling.gsx_vehicles = bool(wanted)
        use = bool(wanted) and gsx_installed()
        self.simvehicles.set_prefer_gsx(use)
        log.info("ground vehicles: %s", "GSX Pro's" if use
                 else "the simulator's own")
        return use

    # -- test tools (traffic.debug) --------------------------------------

    def debug_board_nearest(self) -> tuple[bool, str, str]:
        """The panel's "board nearest now": the parked aeroplane nearest the
        pilot boards now. Off unless traffic.debug.triggers."""
        if not self.config.traffic.debug.triggers:
            return False, "", "debug_off"
        if not self.config.traffic.enabled:
            return False, "", "traffic_off"
        flight, why = self.traffic.board_nearest(time.monotonic())
        return flight is not None, flight.written if flight else "", why

    def debug_arrival_nearest(self) -> tuple[bool, str, str]:
        """The panel's "arrival to nearest stand". Off unless
        traffic.debug.triggers."""
        if not self.config.traffic.debug.triggers:
            return False, "", "debug_off"
        if not self.config.traffic.enabled:
            return False, "", "traffic_off"
        self.traffic.can_invent = self.simtraffic.drawing
        flight, why = self.traffic.arrival_to_nearest_stand(time.monotonic())
        return flight is not None, flight.written if flight else "", why

    def _say_traffic_status(self, now: float) -> None:
        """Every status_every_s, one log line per invented aeroplane, for
        testing the apron (traffic.debug.status). English, like every log."""
        debug = self.config.traffic.debug
        if not debug.status or now - self._status_at < max(
                1.0, float(debug.status_every_s)):
            return
        self._status_at = now
        try:
            lines = self.traffic_status(now)
        except Exception:
            log.debug("the traffic status raised", exc_info=True)
            return
        status_log.info("traffic.debug: %d invented aeroplane(s)", len(lines))
        for line in lines:
            status_log.info("  %s", line)

    # How often the motion loop's own figures are logged with the status on.
    MOTION_STATUS_S = 10.0

    def _say_motion_status(self, now: float) -> None:
        """Every ten seconds with traffic.debug.status, how the loop that
        moves the vehicles and the people really ran: its rate, the time a
        frame took, the actors, the camera, the nearest person's rate, and
        each frame reader's share. The figures are measured on the message
        thread and only read here, so the log costs the frame nothing."""
        if not self.config.traffic.debug.status or                 now - self._motion_status_at < self.MOTION_STATUS_S:
            return
        self._motion_status_at = now
        report = getattr(self.simvehicles, "rates", None)
        if report is None:
            return
        try:
            line = report()
        except Exception:
            log.debug("the motion status raised", exc_info=True)
            return
        if line:
            status_log.info("traffic.debug: %s", line)

    def traffic_status(self, now: float) -> list[str]:
        """What the status dump says: callsign, type, stand, level, window,
        each door, and the next thing that happens to it and when."""
        found = []
        for position in self.traffic.positions(invented_only=True):
            callsign = position["callsign"]
            flight = self.traffic.flight_named(callsign)
            kind, _ = self.traffic.apron_state(callsign)
            window = self._apron_window(callsign, kind, now)
            level = self.simtraffic.target_level(callsign) or "-"
            stand = "-"
            if flight is not None:
                held = (flight.stand if flight.stand is not None
                        else flight.planned_stand)
                stand = str(held) if held is not None else "-"
            found.append(
                f"{callsign:<9} {position.get('type', ''):<5} stand {stand:<4} "
                f"{level:<8} {window:<12} doors {self._doors_said(callsign)}"
                f"  systems {self._systems_said(callsign)}"
                f"  next: {self._next_event(flight, kind, callsign, now)}")
        return found

    def _systems_said(self, callsign: str) -> str:
        """Engines and lights as read back, upper case on: ENG NAV BCN STR
        TAX LDG; a '>' marks one still wanted the other way."""
        simsystems = getattr(self, "simsystems", None)
        object_id = self.simtraffic.object_id(callsign)
        plan = simsystems.plan(object_id) if simsystems and object_id else None
        if plan is None or plan.reading is None:
            return "unread"
        said = []
        for name, short in (("engines", "eng"), ("nav", "nav"), ("beacon", "bcn"),
                            ("strobe", "str"), ("taxi", "tax"), ("landing", "ldg")):
            on = getattr(plan.reading, name)
            word = short.upper() if on else short
            if getattr(plan.goal, name) != on:
                word += ">"
            said.append(word)
        return " ".join(said)

    def _doors_said(self, callsign: str) -> str:
        object_id = self.simtraffic.object_id(callsign)
        plan = self.simdoors.plan(object_id) if object_id else None
        if plan is None:
            return "none (no door animation, or not drawn)"
        said = []
        for role, index in sorted(plan.doors.items()):
            state = plan.is_open(index)
            word = ("deaf" if index in plan.deaf else "open" if state is True
                    else "shut" if state is False else "moving")
            if plan.goal.get(index) and state is not True:
                word += ">open"
            elif not plan.goal.get(index) and state is True:
                word += ">shut"
            said.append(f"{role}={word}")
        return ",".join(said)

    def _next_event(self, flight, kind: str, callsign: str, now: float) -> str:
        def clock(seconds: float) -> str:
            seconds = max(0, int(round(seconds)))
            return f"{seconds // 60}:{seconds % 60:02d}"

        traffic = self.traffic
        if flight is None:
            return "-"
        if kind == "booked":
            return f"boarding in {clock(flight.boarding_at - now)}"
        if kind == "boarding":
            return ("boarded, calls for clearance in "
                    f"{clock(flight.boarding_at + traffic.boarding_s - now)}")
        if kind == "departing":
            tug = self.handling.tug_due_at(callsign)
            if tug is not None:
                return f"pushback (tug) in {clock(tug - now)}"
            if not self.simdoors.can_push(self.simtraffic.object_id(callsign)):
                return "doors closing, then the tug"
            return f"waiting for push clearance ({flight.phase})"
        if kind in ("arrived", "static"):
            ends = self.handling.arrival_ends_at(callsign)
            if ends is not None and ends > now:
                return f"doors shut in {clock(ends - now)}"
            if kind == "arrived":
                return "deplaning"
            return ("parked until booked" if traffic.schedule
                    else "parked until chosen as a departure")
        if kind == "arriving":
            return f"{flight.phase}, {flight.distance_nm:.1f} nm to run"
        return flight.phase

    def _sim_pushback(self) -> bool:
        """The simulator's own tug. Handed to the ground handling as a
        callable so that nothing in ``integrations`` knows what a sim is."""
        push = getattr(self.source, "pushback", None)
        if push is None:
            return False
        try:
            return bool(push())
        except Exception:
            log.debug("the simulator refused the pushback", exc_info=True)
            return False

    def _start_pushback(self) -> None:
        """Call the tug, and say which one answered.

        Never in the way of the transmission. A push that cannot be started
        is reported and nothing else: the aeroplane is where it was, and the
        pilot can still push it themselves.
        """
        if not self.config.gsx.enabled:
            return
        try:
            said = self.gsx.start_pushback()
        except Exception:
            log.debug("could not start the pushback", exc_info=True)
            return
        if said:
            self.emit("info", said, key="log.ground_handling", detail=said)

    def _room_on_the_channel(self, station: Station | None,
                             mhz: float) -> Station | None:
        """Which of a field's radar positions answers a channel they share.

        Centennial's 132.750 is Denver App/Dep, filed twice, and the resolver
        took the Approach row whoever was calling: the tower sent a
        departure to Denver Departure and Denver Approach answered it. The
        position already being worked keeps the channel; then the one the
        pilot was sent to; then Departure on the way out and Approach on the
        way in.
        """
        if station is None or station.position not in ("APP", "DEP"):
            return station
        same = [s for s in self.navdb.stations(station.ident)
                if s.position in ("APP", "DEP")
                and same_channel(s.mhz, mhz, 0)]
        if len({s.position for s in same}) < 2:
            return station
        previous = self.station
        session = self.session
        handed = session.handoff_pending if session is not None else None
        for held in (previous, handed):
            if (held is not None and held.ident == station.ident
                    and held.position in ("APP", "DEP")
                    and same_channel(held.mhz, mhz, 0)):
                return held
        outbound = session is not None and session.phase.outbound
        wanted = "DEP" if outbound else "APP"
        return next((s for s in same if s.position == wanted), station)

    def tune(self, mhz: float) -> Station | None:
        """Tune a frequency and work out who is listening."""
        state = self.state
        lat, lon = (state.latitude, state.longitude) if state.has_position else (0.0, 0.0)
        station = self.navdb.resolve_station(mhz, lat, lon, state.altitude_ft)
        station = self._room_on_the_channel(station, mhz)

        previous = self.station
        # A centre answers anywhere in its sector and carries the FIR's
        # ident whichever field its number is filed at (atc/enroute.py), so
        # retuning it -- five hundred miles on, or past Minnipa to Wudinna,
        # both Melbourne on 125.9 -- finds the same station. That used to
        # take a patch here that kept the old station alive.
        oceanic = (self.session.oceanic
                   if self.session is not None else None)
        if station is None and oceanic is not None:
            # Over the ocean the flight is with an HF station no COM1 can
            # tune; the VHF box is left on whatever the pilot likes.
            station = oceanic
        handed = (self.session.handoff_pending
                  if self.session is not None else None)
        if (handed is not None and handed.position == "CTR" and handed.mhz
                and same_channel(handed.mhz, mhz, 4)):
            # The centre the pilot was just sent to, dialled up to four
            # minutes before its line: the number they were given is theirs,
            # wherever its sector is drawn from here.
            station = handed
            if station_key(handed) != station_key(previous):
                self.session.with_since = time.monotonic()
            pending = self.session.pending
            if (pending is not None and pending.kind == "handoff"
                    and not pending.read_back):
                # Switching is as good as reading it back.
                self.session.clear_pending()
        if station_key(station) != station_key(previous):
            log.info("COM1 %.3f at %.2f, %.2f: %s -> %s", mhz, lat, lon,
                     previous.callsign if previous else "nobody",
                     station.callsign if station else "nobody listening")
        self.station = station
        self._last_com1 = mhz

        if station is None and self.config.atc.answer_unpublished:
            station = self._unpublished_station(mhz, lat, lon)
            if station is not None:
                self.station = station

        if station is None:
            # Explained by the situation loop once COM1 has stayed here
            # (``_explain_empty``), not now: a swap passes through the
            # standby frequency for a moment, and that moment was explained.
            # By channel: 126.630 after 126.625 is the same empty frequency.
            if not same_channel(mhz, self._reported_empty, 0) and (
                    self._empty_pending is None
                    or not same_channel(self._empty_pending[0], mhz, 0)):
                self._empty_pending = (round(mhz, 3), time.monotonic())
            # Nobody on frequency does not mean nowhere. The language still
            # follows the aeroplane, so an empty frequency over Germany is
            # listened to in German and English rather than in whatever was
            # worked at the last airport that answered.
            self._last_region_check = 0.0
            self._follow_region(state)
            return None
        self._reported_empty = 0.0
        self._empty_pending = None
        self._say_invented(station)

        # Somebody real may be working this frequency, in which case this
        # program has nothing to do here.
        self.human = self.online.on_frequency(mhz, lat, lon)

        if previous is None or station_key(previous) != station_key(station) \
                or self.human is not None:
            if self.human is not None:
                self.emit(
                    "info",
                    f"{mhz:.3f} - {self.human.describe()} is working this "
                    "frequency. WilcoATC will stay quiet on it.",
                    key="log.human_online", mhz=f"{mhz:.3f}",
                    station=self.human.callsign,
                    network=self.human.network.upper(),
                )
            # A traffic advisory frequency has nobody on it. Saying so is the
            # difference between a quiet frequency and a broken program: a
            # pilot who tunes one and hears nothing back has no way to tell
            # which of the two they are looking at.
            # Somebody is there only inside their hours: a closed tower or
            # information service is as empty as a traffic frequency.
            elif self.brain.staffed(station):
                self.emit(
                    "info",
                    f"{mhz:.3f} - {station.callsign} "
                    f"({station.ident}, {station.distance_nm:.0f} NM).",
                    key="log.tuned", mhz=f"{mhz:.3f}", station=station.callsign,
                    airport=station.ident, distance=f"{station.distance_nm:.0f}",
                )
            else:
                self.emit(
                    "info",
                    f"{mhz:.3f} - {station.callsign}: an advisory frequency, "
                    "so nobody answers. Announce your position and listen out.",
                    key="log.tuned_advisory", mhz=f"{mhz:.3f}",
                    station=station.callsign, airport=station.ident,
                )
            if self.session is not None:
                self.session.current_station = station
        # Whoever is on the frequency decides what is spoken on it.
        self.follow_languages(station.ident)
        if previous is None or station_key(previous) != station_key(station):
            # A new frequency is a new conversation. Whatever was queued was
            # said on the old one and nobody here would have heard it.
            self._traffic_queue.clear()
        return station

    def _take_the_callsign_heard(self, session: FlightSession,
                                 text: str) -> bool:
        """The pilot's own callsign, when it is not the one in the settings.

        At Kortrijk the pilot said "Oscar Oscar Alpha Bravo Charlie" on every
        call and was answered "Echo Tango Delta two zero four", the ETD204
        an old flight had left in ``config.yaml``. What the pilot says on the
        radio is who they are: it is taken for the rest of the session, the
        strip follows it, and the change is logged with where the old one
        came from. Only a whole registration near the start of a
        transmission counts (``callsigns.heard_registration``).
        """
        heard = heard_registration(text)
        if not heard:
            return False
        written = session.aircraft.callsign.written
        if heard.replace("-", "") == written.replace("-", "").upper():
            return False
        was_from = ("config.yaml flight.callsign" if self.config.flight.callsign
                    else "the simulator's ATC ID" if self.state.callsign
                    else "the default")
        departure = session.departure
        # A registration heard is a registration, whatever the profile.
        alternatives = every_language(
            heard, session.aircraft.type_code,
            dialect=session.profile.dialect_for(
                departure.ident if departure else ""),
            airline=False)
        old = session.aircraft
        session.aircraft = Aircraft(
            alternatives["en"], old.type_code, old.wake, is_heavy=old.is_heavy,
            callsigns=alternatives, language=old.language)
        self.understanding.set_callsign(session.aircraft.variants())
        decision("callsign_heard", was=written, now=heard, was_from=was_from)
        self.emit("info", f"Callsign {heard}, as said on the radio "
                          f"(was {written}, from {was_from}).",
                  key="log.callsign_heard", callsign=heard, was=written,
                  source=was_from)
        return True

    # Which position answers an unlisted frequency, by where the flight is.
    _POSITION_FOR_PHASE = {
        Phase.PREFLIGHT: "DEL", Phase.CLEARED: "GND", Phase.PUSHBACK: "GND",
        Phase.TAXI_OUT: "GND", Phase.HOLDING_SHORT: "TWR",
        Phase.LINED_UP: "TWR", Phase.TAKEOFF: "TWR", Phase.DEPARTURE: "DEP",
        Phase.ENROUTE: "CTR", Phase.DESCENT: "APP", Phase.APPROACH: "APP",
        Phase.LANDING: "TWR",
    }

    def _unpublished_station(self, mhz: float, lat: float,
                             lon: float) -> Station | None:
        """The controller for where you are, on a frequency nobody publishes.

        The open frequency data is thin at some large airports, and a pilot
        sitting at one of them reading a number off the panel is not making it
        up. If the aeroplane is at a field that staffs positions, that field's
        controller takes the call.
        """
        if not self.state.has_position:
            return None
        # A number no radio could be tuned to is a typo, not a gap in the
        # data, and answering it would hide the mistake.
        if not is_valid_channel(mhz):
            return None
        airport = self.navdb.home_airport(lat, lon)
        if airport is None:
            airport = self.navdb.nearest_airport(
                lat, lon, max_nm=self.config.atc.unpublished_radius_nm,
                min_rank=2)
        if airport is None:
            return None
        distance = haversine_nm(lat, lon, airport.lat, airport.lon)
        if distance > self.config.atc.unpublished_radius_nm:
            return None
        # Only a field that staffs a position of its own. Kortrijk staffs
        # none -- an AFIS and nothing else -- and a "Kortrijk Clearance
        # Delivery" and a "Kortrijk Tower" were invented on 124.850 for it,
        # the second calling a takeoff there a pilot deviation.
        if not any((found := self.navdb.station_for(airport.ident, held))
                   and found.position == held and found.is_controller
                   for held in ("DEL", "GND", "TWR", "APP", "DEP")):
            return None

        phase = self.session.phase if self.session is not None else Phase.PREFLIGHT
        position = self._POSITION_FOR_PHASE.get(phase, "GND")
        if position == "GND" and not self.brain.ground_control:
            # Nobody works Ground, so a frequency assumed for it would be
            # assumed for nobody. The tower is who taxis the aeroplane now.
            position = "TWR"
        station = self.navdb.station_on(airport.ident, mhz, position)
        if station is None:
            return None
        station = replace(station, distance_nm=distance)

        if not same_channel(mhz, self._reported_assumed, 0):
            self._reported_assumed = mhz
            # The command as this installation spells it: there is no
            # "wilcoatc" program in a shipped build, and that is what the
            # notice used to tell the pilot to run.
            how = command_hint(f"freq {airport.ident} --add {position} {mhz:.3f}")
            self.emit(
                "info",
                f"{mhz:.3f} is not in the published data for {airport.ident}. "
                f"Answering as {station.callsign}. To keep it: {how}",
                key="log.unpublished", mhz=f"{mhz:.3f}",
                airport=airport.ident, station=station.callsign,
                position=position, command=how,
            )
        return station

    def _situation(self, session: FlightSession, station: Station) -> dict:
        """A few facts that help the AI layer disambiguate a vague request.

        "we\'d like to get going" means taxi to a controller on the ground
        frequency and takeoff to one on tower, and the phase of flight is what
        separates them.
        """
        return {
            "talking to": station.callsign,
            "position": station.position,
            "phase of flight": session.phase.value,
            "on the ground": "yes" if self.state.on_ground else "no",
            "altitude": f"{self.state.altitude_ft:.0f} feet"
            if not self.state.on_ground else "",
            "awaiting readback of": session.pending.kind if session.pending else "",
        }

    def _explain_empty(self, now: float | None = None) -> None:
        """Say that nobody is listening, once COM1 has stayed there.

        Only the active frequency, only once it has been held for
        ``EMPTY_NOTICE_S``, and once per frequency: the situation loop
        re-checks COM1 several times a second, and a swap passes through the
        standby frequency on its way.
        """
        pending = self._empty_pending
        if pending is None or self.station is not None:
            return
        mhz, since = pending
        if round(self.state.com1_active or 0.0, 3) != mhz:
            self._empty_pending = None
            return
        now = time.monotonic() if now is None else now
        if now - since < EMPTY_NOTICE_S:
            return
        self._empty_pending = None
        self._reported_empty = mhz
        state = self.state
        lat, lon = (state.latitude, state.longitude) if state.has_position             else (0.0, 0.0)
        self._say_empty(lat, lon, mhz)

    def _say_empty(self, lat: float, lon: float, mhz: float) -> None:
        """What to say about a frequency nobody answers: the field, what it
        does publish, and the nearest of those when it is a likely mistune.

        One translated line with its values as parameters. It used to be a
        translated head with an English hint glued on as a parameter, half in
        each language, and the hint told the pilot to edit a CSV file and
        run a Python command, which a shipped build has no way to do.
        """
        heard = f"{mhz:.3f}"
        airport = None
        if self.state.has_position:
            airport = self.navdb.home_airport(lat, lon)                 or self.navdb.nearest_airport(lat, lon, max_nm=30.0, min_rank=1)
        stations = self.navdb.stations(airport.ident) if airport else []
        if airport is None or not stations:
            self.emit("info",
                      f"{heard} - nobody is listening on this frequency here.",
                      key="log.empty_frequency_alone", mhz=heard)
            return

        # Every position a pilot can call, the information services included:
        # Kortrijk has nothing else, and its list came out empty.
        order = ["ATIS", "DEL", "GND", "TWR", "DEP", "APP", "CTR",
                 "INFO", "CTAF", "AWOS"]
        seen: dict[str, float] = {}
        for st in sorted(stations, key=lambda s: (s.priority, s.mhz)):
            if st.position in order and st.position not in seen:
                seen[st.position] = st.mhz
        listing = ", ".join(f"{position} {seen[position]:.3f}"
                            for position in order if position in seen)
        field = f"{airport.spoken} ({airport.ident})"
        text = (f"{heard} - nobody is listening on this frequency at {field}."
                f" Published there: {listing}.")
        # A near-miss is worth naming: 127.850 against a published 127.750 is
        # far more likely to be a mistuned radio than a missing frequency.
        closest = min(stations, key=lambda s: abs(s.mhz - mhz))
        if abs(closest.mhz - mhz) <= 0.2:
            self.emit("info", text + f" Did you mean {closest.mhz:.3f}"
                      f" ({closest.callsign})?",
                      key="log.empty_frequency_near", mhz=heard, field=field,
                      listing=listing, near=f"{closest.mhz:.3f}",
                      station=closest.callsign)
            return
        self.emit("info", text, key="log.empty_frequency", mhz=heard,
                  field=field, listing=listing)

    # ------------------------------------------------------------------
    # transmission
    # ------------------------------------------------------------------

    def begin_transmission(self) -> None:
        if self.microphone.recording:
            return
        try:
            self.microphone.start()
            self.emit("info", "Transmitting...", key="log.transmitting")
        except Exception as exc:
            # Logged as well as shown. A panel line is gone as soon as the
            # next one arrives, and this is the failure that then explains
            # everything else that happens for the rest of the session.
            log.warning("could not open the microphone: %s", exc, exc_info=True)
            self.emit("error", f"Could not open the microphone: {exc}",
                       key="log.mic_failed", detail=exc)

    # The longest a transmission may run before the program stops believing
    # in it. Well past the configured limit on a transmission's length, so
    # this only ever fires on a transmit state that is stuck rather than on a
    # pilot who is being thorough.
    STUCK_TRANSMIT_FACTOR = 2.0
    STUCK_TRANSMIT_MIN_S = 60.0

    def _watch_transmission(self) -> None:
        """End a transmission that has plainly stopped being one.

        A transmit state that never ends is the most expensive stuck flag in
        the program, because so much waits on it politely. The cabin crew and
        the first officer will not speak over the frequency, and the
        controller volunteers nothing while the pilot has the bar down -- so a
        microphone that is wedged open does not announce itself, it just
        quietly removes half the aeroplane and every unprompted call, for the
        rest of the flight, with the radio still answering normally when
        spoken to. That is unfalsifiable from the flight deck.

        The causes are real and none of them are the pilot: a Bluetooth
        headset changing profile under the stream, a device unplugged
        mid-transmission, a joystick that reported the button down and then
        stopped reporting anything. So it is bounded rather than trusted.
        """
        limit = max(self.STUCK_TRANSMIT_MIN_S,
                    self.config.audio.max_transmission_s
                    * self.STUCK_TRANSMIT_FACTOR)
        held = self.microphone.recording_for
        if held < limit:
            return
        log.warning("the transmit button has been down for %.0fs; ending the "
                    "transmission", held, extra={"held_s": round(held, 1)})
        self.emit("error",
                  f"The transmit button has been down for {held:.0f} seconds, "
                  f"so the transmission was ended. Nothing else could be said "
                  f"while it was.",
                  key="log.transmit_stuck", seconds=f"{held:.0f}")
        try:
            self.end_transmission()
        except Exception:
            log.debug("could not end the stuck transmission", exc_info=True)
        # Whatever happened above, the state must not survive it: everything
        # this method exists to protect is waiting on that one flag.
        if self.microphone.recording:
            self.microphone.abandon()

    def end_transmission(self) -> None:
        if not self.microphone.recording:
            return
        captured = self.microphone.stop()
        if captured.too_quiet:
            self.emit("info", "Nothing heard on that transmission.",
                      key="log.nothing_heard")
            return
        threading.Thread(
            target=self._handle_transmission, args=(captured,), daemon=True
        ).start()

    def _handle_transmission(self, captured) -> None:
        if not self._busy.acquire(blocking=False):
            self.emit("info", "Still working on the last transmission.",
                      key="log.still_working")
            return
        try:
            self._tell_the_recogniser()
            with timed("recognition", warn_over_ms=4000):
                transcript = self.recognizer.transcribe(
                    captured.audio, captured.sample_rate)
            if transcript.empty:
                self.emit("info", "Transmission was unreadable.",
                          key="log.unreadable")
                RADIO.write("pilot", text="", heard=False,
                            seconds=round(captured.seconds, 2)
                            if hasattr(captured, "seconds") else None)
                return

            # "spoken": the panel empties its box, so a draft left in it is
            # not sent again on the end of the next typed call.
            self.emit("pilot", transcript.text, action="spoken")
            if transcript.confidence < self.config.speech.min_confidence:
                log.info("low confidence %.2f: %s", transcript.confidence,
                         transcript.text,
                         extra={"confidence": round(transcript.confidence, 3),
                                "heard": transcript.text})

            self.handle_text(transcript.text, transcript.language,
                             confidence=transcript.confidence)
        except SpeechModelMissing:
            self.emit("error", "The speech recognition has not been downloaded "
                      "yet. Open Settings, then Components, and press Download.",
                      key="log.speech_missing", action="setup")
        except Exception as exc:
            log.exception("transmission handling failed")
            self.emit("error", f"Error handling transmission: {exc}",
                       key="log.transmission_failed", detail=exc)
        finally:
            self._busy.release()

    # How far a station may be and still be one the pilot might call.
    RECOGNISER_STATIONS_NM = 40.0

    def _tell_the_recogniser(self) -> None:
        """The pilot's own callsign and the stations in reach, as hotwords.

        Measured on sixty pilot calls (scripts/stt_vocabulary_bench.py) they
        bring the callsign back whole in 98% of transmissions against 38%:
        "Oscar Oscar Alpha Bravo Shirley" was a callsign the recogniser had
        not been told about.
        """
        try:
            session = self.session
            callsign = (session.aircraft.callsign.spoken_full
                        if session is not None else "")
            names = []
            if self.station is not None:
                names.append(self.station.callsign)
            state = self.state
            if state.has_position:
                for one in self.navdb.all_stations_near(
                        state.latitude, state.longitude,
                        self.RECOGNISER_STATIONS_NM):
                    if one.position != "ATIS":          # never called
                        names.append(one.callsign)
            # The nearest first; a dozen is all the budget is worth.
            names = list(dict.fromkeys(names))[:12]
            self.recognizer.set_context(callsign, names)
        except Exception:
            log.debug("could not give the recogniser its context",
                      exc_info=True)

    def handle_text(self, text: str, language: str | None = None,
                    speaker: str = "", confidence: float = 1.0) -> Reply | None:
        """Process one pilot transmission that has already been transcribed.

        Exposed separately from the audio path so the whole ATC stack can be
        driven from typed input, which is how the tests exercise it.
        ``language`` is what the pilot spoke; the controller answers in the
        same one when the facility works it. ``speaker`` is "first_officer"
        for a call the pilot handed over, written to the radio log so the
        transcript says who spoke. ``confidence`` is the recogniser's own
        (1.0 for typed text): a name heard nearly is asked about, not
        refused, when the recogniser was unsure of it.
        """
        # The recogniser's spacing is not the keyboard's; the same words are
        # the same transmission.
        text = " ".join((text or "").split())
        if self.session is None:
            self.begin_session()
        session = self.session
        assert session is not None

        self.refresh_state()

        # Somebody in the aeroplane, rather than somebody on the frequency.
        # Before the station is resolved on purpose: a pilot can ask their
        # cabin crew for a coffee at a field with nothing published on the
        # box, and being told that nobody is listening would be wrong twice.
        if speaker != "first_officer":
            if self.answer_the_aeroplane(text, language):
                return None
            # A go-around reported to the tower is also the call on the
            # flight deck; the transmission still goes to the tower.
            try:
                self.crew.heard_on_the_radio(text)
            except Exception:
                log.debug("the first officer raised", exc_info=True)

        # A dead radio sends nothing. Said rather than silently ignored: a
        # pilot who keys a switched-off aeroplane should know why nobody
        # answers, and that it is the aeroplane and not the program.
        if not self.radio_powered:
            self.emit("info", "The radio has no power: nothing was "
                      "transmitted. Switch on the battery and the avionics.",
                      key="log.radio_no_power")
            return None

        station = self.station or self._current_station()
        if station is None:
            self.emit("info", "No station is listening on this frequency.",
                      key="log.nobody_listening")
            return None

        tagged = language
        # A letter heard as a word ("Bravo, Shirley") is read as the letter
        # where a letter belongs; the log keeps what was heard.
        heard = text
        text, _ = letters.snap(text)
        parsed, heard_in = self._read(text, tagged, session, station)
        parsed.heard_confidence = float(confidence)
        if not parsed.callsign_matched and self._take_the_callsign_heard(
                session, text):
            parsed.callsign_matched = True
        # The frequency keeps the language it is being worked in. Read in
        # whatever the recogniser heard, so "répétez" on an English frequency
        # is still a request to repeat -- but answered in English, because
        # one word in another language is not a change of language. Only a
        # transmission of substance in the other language moves it.
        spoken = self._frequency_language(station, heard_in, parsed)
        parsed.language = spoken
        if spoken != self.last_language:
            self.last_language = spoken
            if len(self.languages) > 1:
                self.emit("info", f"Speaking {LANGUAGE_NAMES.get(spoken, spoken)}.",
                          key="log.speaking", language=spoken)

        # The transcript records the reading as well as the words. "Why did it
        # say that" is a question about the intent and the numbers, and neither
        # of them survives in a log of the sentence alone.
        pending = session.pending
        RADIO.write(
            "pilot", text=text, language=spoken,
            speaker=speaker or "pilot",
            # The recogniser's own guess, for a spoken one: the language it
            # was read in is the frequency's unless the words said otherwise.
            **({"heard_as": tagged} if tagged else {}),
            **({"heard": heard} if heard != text else {}),
            station=station.callsign, frequency=round(station.mhz, 3),
            intent=parsed.intent.value, confidence=round(parsed.confidence, 3),
            callsign_matched=parsed.callsign_matched,
            values=dict(parsed.values), phase=session.phase.value,
            owed=pending.owed_items if pending is not None else [],
        )

        # An ATIS letter the pilot reports is remembered so the controller can
        # tell whether they have the current information.
        if parsed.get("atis_letter"):
            session.atis_acknowledged = parsed.get("atis_letter")

        if self.human is not None:
            # Somebody real is working this frequency. What the pilot said is
            # shown, because they said it, and nothing is transmitted over the
            # person who is going to answer it.
            self.emit("info",
                      f"{self.human.describe()} is working this frequency.",
                      key="log.human_online",
                      mhz=f"{station.mhz:.3f}", station=self.human.callsign,
                      network=self.human.network.upper())
            return None

        # Somebody else's exchange is half way through on this frequency:
        # the first call of a flight the controller has not worked yet may
        # go unanswered, and any other is answered "stand by" now and in full
        # once the exchange is over.
        congested = self._congested(station, parsed)
        if congested and not session.established_with(station) \
                and self._first_call_missed(session, station):
            return Reply("", station, silent=True)

        weather = self.weather_for(station.ident)
        with timed("controller"):
            reply = self.brain.respond(parsed, session, station, weather,
                                       self.state)

        if reply.instruction is not None:
            session.remember(reply.instruction)

        if congested and reply.speak and parsed.intent is Intent.READBACK:
            # A readback is not a request: "standby" to it told the pilot
            # their correct readback was something to wait on. Its answer
            # waits, unannounced, for the other exchange to end.
            self._held_reply = (reply, station, spoken, time.monotonic())
            decision("congestion", station=station.callsign, said="nothing",
                     held="readback")
            reply = Reply("", station, silent=True)
        elif congested and reply.speak:
            self._held_reply = (reply, station, spoken, time.monotonic())
            p = self.brain.for_station(station, spoken)
            session.aircraft.language = getattr(p, "language", "en")
            decision("congestion", station=station.callsign, said="standby",
                     held=(reply.instruction.kind
                           if reply.instruction is not None else "answer"))
            reply = Reply(p.standby(session.aircraft), station)

        if reply.speak:
            self.emit("atc", reply.text, station.callsign)
            session.note("atc", as_written(reply.text))
            RADIO.write(
                "atc", text=reply.text, language=spoken,
                station=station.callsign, frequency=round(station.mhz, 3),
                instruction=(reply.instruction.kind
                             if reply.instruction is not None else ""),
                owed=(reply.instruction.owed_items
                      if reply.instruction is not None else []),
                handoff_to=(reply.handoff_to.callsign
                            if reply.handoff_to is not None else ""),
            )
            self.speak(reply.text, station, language=spoken)

        if reply.handoff_to is not None:
            session.handoff_pending = reply.handoff_to
            session.handoff_offered_at = time.monotonic()

        # An approval that reaches the ramp. A controller who says "push-back
        # approved" and then leaves the pilot to go and find the tug in a
        # menu has broken the one thing this program is for.
        if reply.ground == "pushback":
            self._start_pushback()

        # And the other seat says the numbers back to you, on the interphone.
        # After the controller, never over them, and never on the air.
        if reply.instruction is not None:
            try:
                self.crew.confirm(reply.instruction)
            except Exception:
                log.debug("the first officer raised", exc_info=True)

        return reply

    # ------------------------------------------------------------------
    # the people who are not on the radio
    # ------------------------------------------------------------------

    def _read(self, text: str, tagged: str | None, session,
              station: Station) -> tuple[ParsedIntent, str]:
        """Read one transmission, typed or spoken, the same way.

        Once a frequency is worked in a language it is read in that one,
        unless the words are in another over several of them. The
        recogniser's tag does not move it: it is a guess from the sound, and
        at Kortrijk it called an English taxi call French, which the French
        grammar made nothing of -- "say again" to a sentence that typed got
        "roger".

        On a frequency not worked yet the words decide when they say
        anything (:func:`detect.by_words`), and the tag only when they do
        not. At Heubach "Heubach Tower, DEHUH, request radio check" was
        tagged German, from the sound of the field's name, and answered in
        German.

        Where that reading makes nothing of it, it is read in the field's
        other languages, and the first that understands it is the language
        it was in: answered in it, and the frequency goes over to it
        (:meth:`_frequency_language`), save for the one-word replies
        ("répétez", "affirm") that never move it. That is how a pilot
        changes language mid-session. At Heubach, before it, a frequency
        answered once in German heard every English call after as nothing,
        and nothing could bring it back.

        Returns what was understood and the language it was read in.
        """
        ident = station.ident
        established = self._languages_by_frequency.get(station_key(station))
        worked = [self._language_for(ident, lang)
                  for lang in languages_for(ident)]
        words = self._language_for(
            ident, language_of_text(text, languages_for(ident)))
        tag = self._language_for(ident, tagged) if tagged else ""
        if established:
            first = (words if clearly_in(text, words, established)
                     else established)
        else:
            decided = by_words(text, worked)
            first = (self._language_for(ident, decided) if decided
                     else tag or words)
        others = [words, tag, established or self._language_for(ident),
                  *worked]
        context = self._situation(session, station)
        with timed("understanding"):
            parsed = self.understanding.parse(text, first, context=context)
        for other in dict.fromkeys(o for o in others if o and o != first):
            if parsed.intent is not Intent.UNKNOWN:
                break
            with timed("understanding"):
                again = self.understanding.parse(text, other, context=context)
            if again.intent is not Intent.UNKNOWN:
                return again, other
        return parsed, first

    # What one word in another language does not change: these are answered
    # in the frequency's language. A radio check is not on it: it is the
    # call a pilot opens with, and at Heubach the one an English pilot made
    # on a frequency stuck in German.
    _NOT_A_CHANGE_OF_LANGUAGE = frozenset({
        Intent.SAY_AGAIN, Intent.ACKNOWLEDGE, Intent.AFFIRMATIVE,
        Intent.NEGATIVE, Intent.STANDBY, Intent.UNKNOWN,
    })

    def _frequency_language(self, station: Station, heard_in: str,
                            parsed) -> str:
        """The language to answer in on this frequency."""
        key = station_key(station)
        established = self._languages_by_frequency.get(key)
        if (established and heard_in != established
                and parsed.intent in self._NOT_A_CHANGE_OF_LANGUAGE):
            return established
        self._languages_by_frequency[key] = heard_in
        return heard_in

    def answer_the_aeroplane(self, text: str, language: str | None) -> bool:
        """Answer a transmission that was addressed to somebody on board.

        True when it was, and when it was, nothing goes out over the air and
        the controller never hears it. See
        :mod:`wilcoatc.atc.interphone` for what counts as an address and why
        it has to be one.
        """
        settings = self.config.immersion
        if not (settings.enabled and settings.interphone):
            return False
        session = self.session
        # A bare checklist answer is the first officer's only while they
        # are waiting for one, no readback is owed to the controller, and
        # they spoke after the controller did.
        expecting = (self.crew.waiting_for_the_pilot()
                     and (session is None or session.pending is None)
                     and self.crew.spoke_after(self._atc_said_at))
        try:
            said = interphone.parse(
                text, self.crew.language, names=self.crew.names(),
                first_officer=self.crew.first_officer_names(),
                checklists=self.crew.checklist_titles(),
                expecting=expecting, offered=bool(self.crew.offering()))
        except Exception:
            log.debug("could not read the interphone", exc_info=True)
            return False
        if said is None and expecting:
            said = self._checklist_answer_from_the_model(text)
        if said is None:
            return False

        # Written to the transcript under the names the transcript already
        # reads: who was talking, and what the machine made of it. A row
        # nobody can read is the same as no row.
        RADIO.write("interphone", text=text, speaker=said.who,
                    intent=said.what, addressed=said.addressed,
                    language=self.crew.language)
        if said.who == interphone.GROUND:
            self.ask_the_ground(said)
        else:
            self.crew.ask(said)
        # True whether or not anybody answered. A pilot who addressed their
        # cabin and has the cabin switched off gets silence and a row in the
        # crew log saying which switch it was; what they must not get is the
        # controller being handed a drinks order and asking them to say
        # again. The whole of the decision was made by the address.
        return True

    def _checklist_answer_from_the_model(self, text: str):
        """The understanding layer's model, asked whether a transmission
        the rules made nothing of is a checklist answer. None for no model,
        or for "not an answer", which is the controller's."""
        try:
            kind = self.understanding.checklist_answer(text)
        except Exception:
            log.debug("the model could not read a checklist answer",
                      exc_info=True)
            return None
        if not kind:
            return None
        return interphone.Said(who=interphone.FIRST_OFFICER,
                               what=interphone.ANSWER, text=text,
                               language=self.crew.language,
                               answers=(kind,))

    def _read_now(self, var: str):
        """One simulator variable, read now, or None (see
        SimConnectSource.read_now)."""
        reader = getattr(self.source, "read_now", None)
        if not callable(reader):
            return None
        return reader(var)

    def ask_the_ground(self, said) -> bool:
        """Ask the ground handling for whatever the pilot just asked for.

        The words are the crew's and the doing is GSX's, and the two are kept
        apart here: the ground crew acknowledge what was actually asked for,
        and where nothing could be asked -- no bridge, no GSX, or a switch
        turned off -- they say so instead of promising a truck that is never
        coming.
        """
        settings = self.config.gsx
        answer = ""
        wanted = said.what in gsx.SERVICES
        if wanted and not (settings.enabled and settings.control
                           and settings.voice):
            answer = "ground_none"
        elif wanted:
            try:
                notice = self.gsx.request(said.what)
            except Exception:
                log.debug("could not ask the ground handling", exc_info=True)
                notice = ""
            if notice:
                self.emit("info", notice, key="log.ground_handling",
                          detail=notice)
            else:
                answer = "ground_none"
        return self.crew.ask(said, answer=answer)

    # ------------------------------------------------------------------
    # speaking
    # ------------------------------------------------------------------

    def spoken_form(self, text: str) -> str:
        """What the synthesiser is actually given.

        The phraseology composes what a controller says, which is a quantity
        one numeral at a time: "one one eight decimal three one". Some people
        would rather the figures were read instead, and ``atc.spoken_numerals``
        hands over "118.310" for the voice to make what it will of. The same
        converter the transcript uses, pointed at the other output, so the two
        can never disagree about what counts as a number -- and callsigns stay
        as words in both, because "Delta twelve thirty four" is a name.
        """
        if not self.config.atc.spoken_numerals:
            return text
        return as_written(text)

    def speak(self, text: str, station: Station, atis: bool = False,
              language: str = "en") -> None:
        """Synthesise and queue a controller transmission."""
        # The last guard: whatever path got here, a dead radio plays nothing.
        if not self.radio_powered:
            return
        if not atis:
            self._atc_said_at = time.monotonic()
        try:
            with timed("synthesis", warn_over_ms=4000):
                transmission = self.synth.speak(
                    self.spoken_form(text),
                    facility=station.facility or station.ident,
                    position=station.position,
                    ident=station.ident,
                    language=language,
                    distance_nm=station.distance_nm,
                    altitude_ft=self.state.altitude_ft,
                    profile=PROFILE_ATIS if atis else None,
                    radio=self.config.audio.radio_effects,
                )
        except VoicesMissing as exc:
            # Not a fault: a download that has not happened. It reads as one
            # every time the controller opens their mouth, so it is said once
            # per session and then the panel is asked to put the download
            # dialog up, which is the only thing that actually fixes it.
            first = not self._voices_reported
            self._voices_reported = True
            log.warning("no controller voices: %s", exc.where())
            self.emit(
                "error",
                "The controller voices have not been downloaded yet. "
                "Open Settings, then Components, and press Download.",
                key="log.voices_missing",
                action="setup" if first else "",
            )
            return
        except Exception as exc:
            log.exception("synthesis failed")
            self.emit("error", f"Could not synthesise the transmission: {exc}",
                       key="log.synthesis_failed", detail=exc)
            return
        self.player.play(
            transmission.audio, transmission.sample_rate, label=station.callsign
        )

    def play_atis(self, airport_ident: str | None = None,
                  language: str | None = None) -> AtisReport | None:
        """Broadcast the ATIS for a field."""
        if airport_ident is None:
            station = self.station
            airport_ident = station.ident if station else (
                self.session.departure.ident
                if self.session and self.session.departure else ""
            )
        if not airport_ident:
            return None
        if not self.radio_powered:
            self.emit("info", "The radio has no power: nothing was "
                      "transmitted. Switch on the battery and the avionics.",
                      key="log.radio_no_power")
            return None

        spoken = self._language_for(airport_ident, language)
        report = self.atis_for(airport_ident, spoken)
        if report is None:
            self.emit("info", f"No ATIS available for {airport_ident}.",
                      key="log.no_atis", airport=airport_ident)
            return None

        atis_station = self.navdb.station_for(airport_ident, "ATIS")
        if atis_station is None:
            airport = self.navdb.airport(airport_ident)
            atis_station = Station(
                ident=airport_ident, position="ATIS", mhz=0.0,
                facility=airport.spoken if airport else airport_ident,
                dialect=airport.dialect if airport else "faa",
            )

        self.emit("atis", report.text, f"{airport_ident} ATIS {report.letter}")
        self.speak(report.text, atis_station, atis=True, language=spoken)
        return report

    # ------------------------------------------------------------------
    # situation loop
    # ------------------------------------------------------------------

    def _current_station(self) -> Station | None:
        if self.state.com1_active:
            return self.tune(self.state.com1_active)
        return self.station

    def _situation_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.debug("situation loop error", exc_info=True)
            self._stop.wait(self.config.sim.poll_interval_s)

    def _tick(self) -> None:
        with timed("tick", warn_over_ms=1500):
            self._one_tick()

    def _one_tick(self) -> None:
        # Before anything is read: is the thing being read from still there?
        self._watch_simulator()
        self._watch_live_feed()

        # And is the microphone still a microphone? First, because a transmit
        # state that is stuck silences everything below this line.
        self._watch_transmission()

        previous = self.state
        state = self.refresh_state()
        session = self.session
        if session is None:
            return
        # Every watcher below reads the debounced on-ground flag: the raw one
        # flickers on a hard landing, and each flicker was a takeoff and a
        # touchdown. ``self.state`` keeps the raw reading for the panel.
        previous, state = self._debounced_ground(previous, state)

        # Nothing is happening in the aeroplane.
        #
        # While the simulator is paused or the user is in its menus, every
        # watcher below is watching for a change -- a takeoff, a touchdown, a
        # runway entered, a thousand feet passed -- and none of the changes
        # that arrive are things the aeroplane did. A menu that moves you to
        # another airport is not a flight, and a first officer calling the
        # landing checklist at it is the program talking over the top of
        # somebody who is not even flying.
        #
        # The state is still read, so the panel keeps telling the truth about
        # where the aeroplane is, and it keeps being written to ``self.state``
        # -- which is what makes the first tick after the menu compare two
        # readings from after the jump rather than straddling it.
        if state.paused:
            if not self._was_paused:
                self._was_paused = True
                self.crew.hold()
                self.emit("info", "The simulator is paused.",
                          key="log.sim_paused")
            return
        if self._was_paused:
            self._was_paused = False
            self.emit("info", "Back in the aeroplane.",
                      key="log.sim_unpaused")

        # The pilot changed frequency: work out who is now listening.
        retuned = bool(state.com1_active) \
            and abs(state.com1_active - self._last_com1) > 0.004
        if retuned:
            self.tune(state.com1_active)
        self._settle_oceanic(session)
        self._explain_empty()
        if not retuned and self.station is None:
            # Nothing tuned, so the country under the aeroplane decides what
            # the recogniser listens for. Checked rarely: it only changes when
            # a border does.
            self._follow_region(state)

        # The airport under the aeroplane decides what is on the radio there,
        # and the simulator is the authority on that.
        self.follow_facilities(state)
        # And its taxiways, for the pilot's own taxi, at both ends.
        self.follow_ground_layouts(state, session)

        # And who is on the network, which decides whether this program is
        # entitled to say anything at all. The poll is in the background; the
        # answer is whatever the last one found.
        if self.online.enabled:
            self.online.poll()
            self._watch_for_humans(state)

        # A liftoff off a landing rollout is classified first, so that the
        # surface watch does not take a bounce for a takeoff.
        self._note_rollout_liftoff(previous, state, session)
        self._follow_route(state)

        # What the aircraft is doing is checked before the phase is updated,
        # because a departure and a touchdown are transitions: updating first
        # would clear the very flags that say whether either was cleared.
        self._watch_surface(previous, state, session)

        # Track the phase from what the aircraft is actually doing, so the
        # controller stays in step even if the pilot skips a call.
        self._update_phase(previous, state, session)

        # And what the rollout liftoff turns out to be: back on the ground
        # (a bounce), round the circuit, or round again.
        self._after_rollout_liftoff(previous, state, session)
        # Or nothing at all: still on the ground, so it was a full stop.
        self._full_stop_after_touchdown(state, session)

        # The landing, measured. Written up a few seconds after the wheels
        # touch, once a bounce has had time to show itself.
        self._watch_landing(previous, state, session)

        # What the tug and the jetway are doing, so the controller does not
        # clear an aeroplane to move that is bolted to the stand.
        if self.config.gsx.enabled:
            self.gsx.poll(state)
            for line in (self.gsx.changed(), self.gsx.notice()):
                if line:
                    self.emit("info", line, key="log.ground_handling",
                              detail=line)

        # A readback that never came. Chasing it is what makes a readback
        # mandatory rather than merely expected: without this the instruction
        # sat pending for the rest of the flight and nothing was ever said.
        self._chase_readback(session)

        # The departure that was told to line up and wait, and the arrival,
        # worked rather than waited for. The arrival is before the handoff on
        # purpose: an aircraft is cleared for the approach by the radar
        # position that then passes it to the tower, not by the tower it
        # arrives on.
        self._work_the_departure(state, session)
        self._work_the_climb(state, session)
        self._work_the_arrival(previous, state, session)
        self._watch_airspace(state, session)
        self._watch_restriction(state, session)
        self._watch_radar_service(state, session)
        self._watch_traffic(state, session)
        self._watch_runway(state, session)
        self._watch_points(state, session)
        if not self.stand_down:
            self._say_held_reply(time.monotonic())

        # The handoffs the flight itself calls for: off the tower after
        # departure, back to ground after landing. These are not reminders and
        # are not optional, which is why they are not behind the setting.
        self._hand_off_on_phase(state, session)

        if self.config.atc.proactive_handoff:
            self._offer_handoff(state, session)

        # The rest of the aeroplane, following the same state the controller
        # is reading. It can only make noise; it cannot change anything here.
        #
        # The cabin is held under the radio first. Nothing in there ever
        # starts while the frequency is live -- that is the crew's own rule --
        # so what this is for is the announcement already half said when the
        # pilot keys the microphone, and the answer that lands in the middle
        # of it.
        self.cabin.ducked = self.radio_busy()
        try:
            self.crew.follow(previous, state, session)
        except Exception:
            log.debug("the crew raised", exc_info=True)

        if self.config.traffic.enabled:
            self._run_traffic(state, session)
            # A simulator that will not create AI aircraft says so once,
            # rather than leaving a pilot looking out of the window for
            # aeroplanes that were promised at startup and never made.
            if (self.simtraffic.refused
                    and session.advise_once("sim_traffic_refused")):
                self.emit("info",
                          "This simulator will not create AI aircraft, so "
                          "WilcoATC adds no traffic of its own. Aircraft "
                          "already in the simulator are still worked.",
                          key="log.traffic_refused")
        elif self.brain.traffic is not None:
            # Switched off mid-flight: the aeroplanes stop existing, and the
            # controller stops sequencing the user against them.
            self.brain.traffic = None
            self.traffic.flights.clear()
            self.traffic._drop_invented()
            self._spawned.clear()
            self.traffic.observed_count = 0
            self.traffic.pending.clear()
            self._traffic_queue.clear()
            self.simtraffic.sync([])
            self.simvehicles.sync([])

    # ------------------------------------------------------------------
    # the other aeroplanes
    # ------------------------------------------------------------------

    def _run_traffic(self, state: AircraftState, session: FlightSession) -> None:
        """Speak whatever the other traffic is saying on this frequency.

        The injector runs whether or not the user is listening -- aeroplanes
        keep taxiing when you change frequency -- but only what is said on the
        frequency in COM1 reaches the speakers.
        """
        station = self.station
        self.traffic.can_invent = self.simtraffic.drawing
        if station is None or not station.ident:
            # Nobody to talk to, so nothing is said -- but the airport is
            # still there. Whoever is already flying keeps flying, and the
            # field under the aeroplane is worked silently: its stands set
            # out, FSLTL's aeroplanes read off the simulator and handled,
            # all of it on the map. A pilot on 122.800 at the gate used to
            # see an empty airport with nobody on it, because every one of
            # those things waited for a controller to be tuned.
            self._work_the_field_here(state, session)
            self.traffic.advance(time.monotonic())
            self._keep_traffic_real(time.monotonic())
            return

        from .atc.enroute import is_oceanic

        if station.position == "CTR" and not is_oceanic(station):
            # A centre's frequency is worked by its own traffic
            # (atc/centre_traffic.py); the field's goes on silently, as
            # with nobody tuned. Pointing the field's injector at the FIR
            # was an airfield with no runway and an hour of silence.
            self._work_the_field_here(state, session)
            now = time.monotonic()
            self.traffic.advance(now)
            self._keep_traffic_real(now)
            self._work_the_centre(state, session, station, now)
            return

        # The density is a live setting, so it is read here rather than only
        # at startup: turning the frequency quiet has to take effect now, not
        # on the next run.
        self.traffic.density = self.config.traffic.density
        self.brain.traffic = self.traffic

        # The aeroplanes somebody else put in the simulator, before anything
        # else: how many of them are on the field is what decides how many
        # more to invent, and a strip for one of them has to exist before the
        # tower looks at the field.
        self._take_observed(session)

        # And where the user is, so the traffic works a field with the
        # aeroplane that matters on it rather than around a hole. A departure
        # is held for the user on final, and an AI on short final to a runway
        # the user is sitting on is sent around.
        self.traffic.set_user(UserAircraft(
            callsign=session.aircraft.callsign.written,
            type_code=session.aircraft.type_code,
            latitude=state.latitude,
            longitude=state.longitude,
            altitude_ft=state.altitude_ft,
            heading=state.heading_true,
            ground_speed_kt=state.ground_speed_kt,
            on_ground=state.on_ground,
        ) if state.has_position else None)

        # The traffic uses the runway the user has been given, so the whole
        # field is working one configuration rather than two.
        self.traffic.set_field(
            station.ident,
            departure_runway=session.departure_runway,
            arrival_runway=session.arrival_runway or session.departure_runway,
            weather=self._weather_cache.get(station.ident),
            tuned=station,
        )

        self._follow_ground(station.ident)

        now = time.monotonic()
        # Whatever has fallen due joins the back of the queue. It is not spoken
        # here: the frequency may be busy, and a transmission that cannot be
        # heard has to wait rather than disappear.
        self._traffic_queue.extend(self.traffic.poll(now, station.position,
                                                     station.mhz))
        self._speak_from_the_queue(station, now)

        # And the same aeroplanes, where they can be seen. This is every
        # flight the injector is running, not only the ones on this frequency:
        # an aeroplane does not vanish from the taxiway because you changed
        # channel.
        # Only the invented ones: the rest are already in the simulator, and
        # creating a second copy of an FSLTL aeroplane a foot from the first is
        # the one way this could make the window worse rather than better.
        self._keep_traffic_real(now)

    centre_traffic = None

    def _work_the_centre(self, state: AircraftState, session: FlightSession,
                         station: Station, now: float) -> None:
        """The other flights on the centre frequency, invented and, with
        live traffic on, real; spoken through the same queue as a field's."""
        from .atc.centre_traffic import CentreTraffic

        if not state.has_position:
            return
        if self.centre_traffic is None:
            self.centre_traffic = CentreTraffic(self.navdb,
                                                profile=self.traffic.profile)
        live = self._live_seen() if self.live_feed is not None else []
        try:
            calls = self.centre_traffic.poll(
                now, station, latitude=state.latitude,
                longitude=state.longitude, altitude_ft=state.altitude_ft,
                own_callsign=session.aircraft.callsign.written, live=live,
                density=self.config.traffic.density)
        except Exception:
            log.debug("the centre's traffic raised", exc_info=True)
            return
        self._traffic_queue.extend(calls)
        self._speak_from_the_queue(station, now)

    def ground_layout(self, ident: str):
        """This airport's stands and taxiways, if the simulator has given
        them; asked for in the background if it has not yet."""
        ident = (ident or "").upper()
        layout = self._ground_ready.get(ident)
        if layout is None:
            self._ask_for_ground(ident)
        return layout

    # How close to the destination a layout is asked for whatever the phase
    # says. The read takes seconds on a worker; at 250 knots sixty miles is a
    # quarter of an hour, and a pilot who never called for descent is still
    # descending.
    LAYOUT_AHEAD_NM = 60.0

    def follow_ground_layouts(self, state: AircraftState,
                              session: FlightSession) -> None:
        """Ask for the taxiways the pilot is going to be taxied on.

        The traffic asks only for the field it is working, and only when it
        is switched on, which left the pilot's own departure field unread
        with the traffic off. So: the field the aeroplane is on the ground
        at, as soon as it is there, and the destination during the descent,
        well before the taxi in is asked for -- the read is asynchronous and
        the controller never waits for it.
        """
        if not state.has_position:
            return
        if state.on_ground:
            here = self.navdb.home_airport(state.latitude, state.longitude)
            if here is not None:
                self._ask_for_ground(here.ident)
            return
        destination = session.destination
        if destination is None:
            return
        descending = session.phase in (Phase.DESCENT, Phase.APPROACH,
                                       Phase.LANDING)
        if descending or self.brain._distance_to(state, destination) \
                <= self.LAYOUT_AHEAD_NM:
            self._ask_for_ground(destination.ident)

    def _work_the_field_here(self, state: AircraftState,
                             session: FlightSession) -> None:
        """Point the traffic at the airport the aeroplane is at, with nobody
        tuned to say so, and read the simulator's own traffic there.

        Only when the field can be told: an aeroplane on the ground, or
        within a few miles of one. Airborne and far from anywhere, whatever
        field was being worked stays.
        """
        if not state.has_position:
            return
        airport = self.navdb.home_airport(state.latitude, state.longitude)
        if airport is None:
            return
        self.traffic.set_field(
            airport.ident,
            departure_runway=session.departure_runway,
            arrival_runway=session.arrival_runway or session.departure_runway,
            weather=self._weather_cache.get(airport.ident),
        )
        self._follow_ground(airport.ident)
        self.traffic.set_user(UserAircraft(
            callsign=session.aircraft.callsign.written,
            type_code=session.aircraft.type_code,
            latitude=state.latitude,
            longitude=state.longitude,
            altitude_ft=state.altitude_ft,
            heading=state.heading_true,
            ground_speed_kt=state.ground_speed_kt,
            on_ground=state.on_ground,
        ))
        self._take_observed(session)

    def _follow_ground(self, ident: str) -> None:
        """Give the traffic this airport's taxiways, once the simulator has
        said what they are."""
        ident = (ident or "").upper()
        if not ident or self.traffic.field.ident != ident:
            return
        layout = self._ground_ready.get(ident)
        if layout is not None:
            if self.traffic.ground is None:
                self.traffic.set_ground(layout)
            return
        self._ask_for_ground(ident)

    def _ask_for_ground(self, ident: str) -> None:
        if not ident or ident in self._ground_asked:
            return
        if not (self.state.source == "simconnect" and self.state.connected):
            return
        self._ground_asked.add(ident)

        def read() -> None:
            try:
                found = self.simground.layout(ident)
            except Exception:
                log.debug("could not read %s's taxiways", ident, exc_info=True)
                return
            if found is not None:
                log.info("%s: %d stands and %d taxi nodes from the simulator",
                         ident, len(found.stands), len(found.xy))
                self._ground_ready[ident] = found

        threading.Thread(target=read, name=f"WilcoATC-taxi-{ident}",
                         daemon=True).start()

    runway_cache = None

    def _use_runway_cache(self) -> None:
        from .navdata.runway_cache import RunwayCache
        from .paths import data_root

        self.runway_cache = RunwayCache(data_root() / "airports")
        self.navdb.use_runway_cache(self.runway_cache)

    def say_runway_source(self, ident: str) -> None:
        """A row for ``logs radio`` and ``logs arrival``: where the field's
        runways come from (sim, cache or db), and, when the simulator's,
        what they disagree with the database on -- the reason the pilot
        heard "26" where the chart says "25". Once per field and source."""
        if not ident:
            return
        source = self.navdb.runway_source(ident)
        if self._runways_said.get(ident) == source:
            return
        self._runways_said[ident] = source
        ends = sorted({end for rw in self.navdb.runways(ident)
                       for end, *_ in rw.ends()})
        decision("airport", airport=ident, runways_from=source, ends=ends,
                 **self.navdb.runway_differences(ident))

    def _ask_for_approaches(self, ident: str) -> None:
        # Said whether or not the simulator can be asked: without it, the
        # row says the cache or the database is what is being used.
        self.say_runway_source(ident)
        if not ident or ident in self._approaches_asked:
            return
        if not (self.state.source == "simconnect" and self.state.connected):
            return
        self._approaches_asked.add(ident)

        def read() -> None:
            try:
                found = self.simapproaches.approaches(ident)
            except Exception:
                log.debug("could not read %s's approaches", ident, exc_info=True)
                return
            if found is not None:
                log.info("%s: approaches from the simulator: %s", ident,
                         {rw: sorted(kinds) for rw, kinds in found.by_runway.items()})
                self.brain.approach_data[ident] = found
                # Any ATIS already issued for the field named the ILS; the
                # next one is re-issued on what is really there. Marked here
                # and done by atis_for on the engine's own thread: walking
                # the table from this worker raced the engine adding to it.
                with self._atis_lock:
                    self._atis_stale.add(ident)
            # The runways themselves, and from the same answer each one's
            # pattern altitude: the scenery's are the ones the aeroplane
            # rolls on, so from here they are the field's.
            try:
                runways = self.simrunways.for_airport(ident)
                patterns = self.simrunways.patterns(ident)
            except Exception:
                log.debug("could not read %s's runways", ident, exc_info=True)
                return
            if runways:
                from .integrations.runways import with_ils_kinds

                # Which end's navaid is a localizer and which a VOR, from
                # the approaches read just above.
                runways = with_ils_kinds(runways, found)
                self.navdb.set_sim_runways(ident, runways)
                cache = getattr(self, "runway_cache", None)
                if cache is not None:
                    cache.save(ident, runways)
                self.say_runway_source(ident)
            if patterns:
                self.brain.sim_patterns[ident] = patterns

        threading.Thread(target=read, name=f"WilcoATC-approaches-{ident}",
                         daemon=True).start()

    def _keep_traffic_real(self, now: float) -> None:
        """Have the simulator draw the invented traffic, and keep only what
        it has drawn.

        An aeroplane is asked of the simulator first and joins the frequency
        once it is there; one the simulator cannot make is dropped without a
        word ever having been said about it.
        """
        positions = self.traffic.positions(invented_only=True)
        if self.state.has_position:
            # For how often each is drawn: thirty-five times a second near
            # the pilot, five at ten kilometres.
            self.simtraffic.viewer = (self.state.latitude, self.state.longitude)
        self._choose_models(positions, now)
        self.simtraffic.sync(positions, now)
        confirmed, dropped = self.traffic.confirm(
            self.simtraffic.has, self.simtraffic.failed, now)
        self._keep_handling_real(now)
        self._keep_systems_real(now)
        self._say_traffic_status(now)
        self._say_motion_status(now)
        if not self.simtraffic.drawing:
            # What was already queued from an aeroplane that no longer exists
            # is not said either.
            self._traffic_queue = [c for c in self._traffic_queue
                                   if c.flight.observed]
        for flight in confirmed:
            runway = self._spawned.pop(flight.written, None)
            if runway is None:
                continue
            if flight.phase == "lineup":
                self.emit("info",
                          f"{flight.written} is lined up on runway {runway}.",
                          key="log.traffic_spawned", callsign=flight.written,
                          runway=runway)
            else:
                self.emit("info",
                          f"{flight.written} is holding short of runway "
                          f"{runway} until the runway is free.",
                          key="log.traffic_spawned_holding",
                          callsign=flight.written, runway=runway)
        for flight in dropped:
            if self._spawned.pop(flight.written, None) is not None:
                self.emit("info",
                          f"The simulator could not create {flight.written}, "
                          f"so it is not on the runway.",
                          key="log.traffic_spawn_failed",
                          callsign=flight.written)

    def _choose_models(self, positions: list[dict], now: float) -> None:
        """Mark each invented aeroplane with the level of detail it should
        be drawn at, for the injector to follow.

        Only with LOD on: without it no position carries a level and the
        injector draws what it always drew. One not yet drawn is created
        at the level its distance says; one standing on its stand is
        swapped when the decision says, so many a second; one moving is
        left as it is until it parks.
        """
        if not self.lod.enabled:
            return
        if self.lod.scheduled:
            self._choose_models_by_schedule(positions, now)
            return
        try:
            user = self.traffic.user
            user_xy = None
            layout = self.traffic.ground
            if user is not None and user.has_position:
                if layout is not None:
                    user_xy = layout.to_xy(user.latitude, user.longitude)
                else:
                    user_xy = (0.0, 0.0)

            def xy(position: dict) -> tuple[float, float]:
                if layout is not None:
                    return layout.to_xy(position["lat"], position["lon"])
                # Metres east and north of the pilot, near enough.
                from .navdata.db import haversine_nm
                east = haversine_nm(user.latitude, user.longitude,
                                    user.latitude, position["lon"]) * 1852.0
                north = haversine_nm(user.latitude, user.longitude,
                                     position["lat"], user.longitude) * 1852.0
                return (east if position["lon"] >= user.longitude else -east,
                        north if position["lat"] >= user.latitude else -north)

            detailed_now = sum(
                1 for p in positions
                if self.simtraffic.target_level(p["callsign"]) == "detailed")
            candidates: list[Candidate] = []
            for position in positions:
                callsign = position["callsign"]
                has_detailed = self.simtraffic.has_detailed(
                    position.get("type", ""), position.get("operator", ""))
                if not self.simtraffic.has(callsign):
                    distance = None
                    if user_xy is not None:
                        x, z = xy(position)
                        distance = math.hypot(x - user_xy[0], z - user_xy[1])
                    level = self.lod.level_for_new(distance, detailed_now,
                                                   has_detailed)
                    if level == "detailed":
                        detailed_now += 1
                    position["level"] = level
                    continue
                # Stamped with where it is going, not where it was: on
                # the tick a swap finishes, the old level would be read as
                # an order to swap back.
                level = self.simtraffic.target_level(callsign)
                position["level"] = level
                busy = self.simtraffic.swapping(callsign)
                stationary = bool(position.get("on_ground")) and not position.get(
                    "ground_speed_kt") and (position.get("static")
                                            or position.get("intent") == "parked")
                x, z = xy(position) if user_xy is not None else (0.0, 0.0)
                candidates.append(Candidate(callsign, x, z, level=level,
                                            stationary=stationary,
                                            has_detailed=has_detailed,
                                            busy=busy))
            by_callsign = {p["callsign"]: p for p in positions}
            for callsign, level in self.lod.decide(candidates, user_xy, now):
                by_callsign[callsign]["level"] = level
        except Exception:
            log.debug("choosing the models raised", exc_info=True)

    def _apron_window(self, callsign: str, kind: str, now: float) -> str:
        """Where one of this program's aeroplanes is in its turnaround, from
        what the traffic, the ramp and the doors each know of it."""
        object_id = self.simtraffic.object_id(callsign)
        doors_open = self.simdoors.open_now(object_id)
        if kind == "booked":
            return lod_windows.PRE_BOARDING
        if kind == "boarding":
            return lod_windows.BOARDING
        if kind == "departing":
            # Boarding until every door reads shut, then waiting for the tug.
            if self.simdoors.can_push(object_id) and not doors_open:
                return lod_windows.CLOSED
            return lod_windows.BOARDING
        if kind == "arrived":
            # Blocked in and still on the radio: inside its deplaning round
            # by definition. Asking the ramp instead read it as closed up
            # on the tick it stopped -- the ramp had not seen it yet -- and
            # it was lightened at the gate and detailed again a tick later.
            return lod_windows.ARRIVAL
        if kind == "static":
            if self.handling.arrival_open(callsign, now) or doors_open:
                return lod_windows.ARRIVAL
            return lod_windows.PARKED
        return lod_windows.MOVING

    def _choose_models_by_schedule(self, positions: list[dict],
                                   now: float) -> None:
        """The schedule's choice of model (traffic.lod.mode: schedule).

        Detailed only in the turnaround window, near the pilot and under the
        cap; created detailed where it can be, which is an arrival whose
        stand is near and the departure booked beside the pilot, so that it
        never has to change at the gate. See wilcoatc.atc.lod.
        """
        try:
            user = self.traffic.user
            layout = self.traffic.ground
            user_xy = None
            if (user is not None and user.has_position
                    and layout is not None):
                user_xy = layout.to_xy(user.latitude, user.longitude)
            detailed_now = sum(
                1 for p in positions
                if self.simtraffic.target_level(p["callsign"]) == "detailed")
            roles = self.handling.doors(now)
            candidates: list[Candidate] = []
            for position in positions:
                callsign = position["callsign"]
                type_code = position.get("type", "")
                operator = position.get("operator", "")
                has_detailed = self.simtraffic.has_detailed(type_code, operator)
                matches = self.simtraffic.livery_matches(type_code, operator,
                                                         callsign)
                kind, stand = self.traffic.apron_state(callsign)
                if layout is not None:
                    here = layout.to_xy(position["lat"], position["lon"])
                else:
                    here = None
                # Measured from where it will turn round while it is on
                # its way there; from where it is otherwise.
                where = stand if kind == "arriving" and stand else here
                if not self.simtraffic.has(callsign):
                    window = (lod_windows.ARRIVAL if kind == "arriving"
                              and stand else self._apron_window(
                                  callsign, kind, now))
                    distance = None
                    if user_xy is not None and where is not None:
                        distance = math.hypot(where[0] - user_xy[0],
                                              where[1] - user_xy[1])
                    level = self.lod.level_for_new(
                        distance, detailed_now, has_detailed, window=window,
                        livery_matches=matches)
                    if level == "detailed":
                        detailed_now += 1
                    position["level"] = level
                    continue
                level = self.simtraffic.target_level(callsign)
                position["level"] = level
                window = self._apron_window(callsign, kind, now)
                object_id = self.simtraffic.object_id(callsign)
                flight = self.traffic.flight_named(callsign)
                if (flight is not None and flight.by_hand
                        and self.lod.livery != "match"):
                    # Boarded with the test button: its doors are what the
                    # pilot asked to see, so the hide rule does not apply.
                    matches = True
                stationary = bool(position.get("on_ground")) and not position.get(
                    "ground_speed_kt") and kind in ("static", "booked",
                                                    "boarding", "departing",
                                                    "arrived")
                x, z = where if where is not None else (0.0, 0.0)
                candidates.append(Candidate(
                    callsign, x, z, level=level, stationary=stationary,
                    has_detailed=has_detailed,
                    busy=self.simtraffic.swapping(callsign),
                    window=window,
                    doors_open=bool(roles.get(callsign))
                    or self.simdoors.open_now(object_id),
                    doors_moving=self.simdoors.moving(object_id, now),
                    livery_matches=matches))
            by_callsign = {p["callsign"]: p for p in positions}
            for callsign, level in self.lod.decide(candidates, user_xy, now):
                by_callsign[callsign]["level"] = level
        except Exception:
            log.debug("choosing the models by schedule raised", exc_info=True)

    def _read_the_doors(self, standing: list, now: float) -> None:
        """Where each vehicle goes, from each model's own doors: ask the
        simulator once per model, and hand every aeroplane its spots."""
        points_reader = getattr(self, "servicepoints", None)
        if points_reader is None or not points_reader.available:
            return
        titles = {role: self.simvehicles.title_for(role)
                  for role in list(self._service_layout.get("roles", {}))
                  + list(handling_spots.SPOTS)}
        for one in standing:
            if one.observed:
                object_id, title = one.object_id, one.title
            else:
                object_id = self.simtraffic.object_id(one.callsign)
                title = self.simtraffic.title_of(one.callsign)
            if not object_id or not title:
                continue
            points = points_reader.points(title)
            if points is None:
                points_reader.ask(object_id, title, now)
                continue
            length = handling_spots.fuselage_length(one.type_code, one.light)
            fallback = {role: (right, fraction * length + extra, facing)
                        for role, (right, fraction, extra, facing)
                        in handling_spots.SPOTS.items()}
            # How high each door is on this model, and a loader docked by
            # its own belt: both from the points and the height the model
            # stands at (servicepoints.standing).
            # This aeroplane's own height where it is one of ours: the one
            # read with the points is another object's, at another moment.
            standing = None if one.observed else (
                self.simtraffic.height_above_ground(one.callsign))
            pitch = 0.0
            if standing is None:
                standing = points_reader.standing(title)
            else:
                pitch = self.simtraffic.resting_pitch(one.callsign)
            fittings = fittings_for(points, standing,
                                    self._service_layout, titles, pitch,
                                    listed=self.simvehicles.listed_gsx())
            chosen = dict(titles)
            for role, fitting in fittings.items():
                if fitting.get("title"):
                    chosen[role] = fitting["title"]
                    said = (one.callsign, role, fitting["title"])
                    if said not in self._stairs_said:
                        self._stairs_said.add(said)
                        log.info("stairs: %s %s, sill %.2f m: %s (%s)", one.callsign,
                                 role, fitting["sill"], fitting["title"], fitting["why"])
            one.fittings = fittings or None
            one.spots = spots_for(points, self._service_layout, chosen,
                                  one.type_code, fallback, fittings) or None

    def _keep_systems_real(self, now: float) -> None:
        """This program's aeroplanes' engines and lights, by where each is
        in its day (wilcoatc.atc.systems). Only objects this program drew;
        never the pilot's, never anybody else's."""
        simsystems = getattr(self, "simsystems", None)
        if simsystems is None or not simsystems.available:
            return
        try:
            simsystems.user_object_id = getattr(self.simwatch,
                                                "user_object_id", None)
            elevation = self.traffic.field.elevation_ft or 0.0
            wanted = {}
            for position in self.traffic.positions(invented_only=True):
                callsign = position["callsign"]
                flight = self.traffic.flight_named(callsign)
                if flight is None:
                    continue
                kind, _ = self.traffic.apron_state(callsign)
                objects = self.simtraffic.objects_of(callsign)
                if not objects:
                    continue
                goal = systems_wanted(
                    kind, flight.phase, now, parked_at=flight.parked_at,
                    pushing=flight.target.intent == "pushing back",
                    doors_shut=self.simdoors.can_push(objects[0]),
                    altitude_agl_ft=max(0.0, flight.altitude_ft - elevation),
                    on_ground=flight.on_ground, callsign=callsign)
                for object_id in objects:
                    wanted[object_id] = (callsign, goal)
            simsystems.sync(wanted, now)
        except Exception:
            log.debug("the engines and lights raised", exc_info=True)

    def _model_swapped(self, callsign: str, old: int, new: int,
                       title: str) -> None:
        """An aeroplane has changed object: its doors go with it."""
        self.simdoors.carry(old, new, title)

    def _model_removed(self, object_id: int, what: str) -> None:
        """An object of ours has been taken out of the simulator. The door
        layer keeps an aeroplane it no longer wants until its doors read
        shut, and one that has gone never reads anything again: at
        Innsbruck (15 October) an arrival removed as its doors were closing
        was asked after every four seconds for the rest of the session,
        each one an UNRECOGNIZED_ID."""
        self.simdoors.forget(object_id)

    def _may_push(self, callsign: str) -> bool:
        """Whether this program's aeroplane may push: every door the door
        layer knows of read shut. One it is not working is not held."""
        return self.simdoors.can_push(self.simtraffic.object_id(callsign))

    def _keep_handling_real(self, now: float) -> None:
        """Send the ground handling to the aeroplanes the simulator has
        drawn on their stands, and keep the vehicles where the turnaround
        says they are.

        Only aeroplanes the simulator has actually made: a baggage loader
        at an empty stand is the one thing worse than an empty one. The
        turnaround runs whether or not the simulator will draw vehicles, so
        the decision is cheap and the drawing fails closed on its own.
        """
        # The turnaround runs whenever handling is on: with vehicles off
        # (another tool draws them) the doors still follow its schedule.
        if not self.config.traffic.handling.enabled:
            return
        try:
            # Somebody else's parked aeroplanes are in the simulator by
            # definition; this program's only once it has drawn them.
            handle_observed = self.config.traffic.handling_observed
            # The pilot's own aeroplane, as the simulator names it: never
            # handled, its doors never touched (see SimDoors.is_the_pilot).
            pilot = getattr(self.simwatch, "user_object_id", None)
            if hasattr(self.simdoors, "user_object_id"):
                self.simdoors.user_object_id = pilot
            standing = [one for one in self.traffic.standing()
                        if ((one.observed and handle_observed)
                            or (not one.observed
                                and self.simtraffic.has(one.callsign)))
                        and not (one.observed and one.object_id in (0, pilot))]
            layout = self.traffic.ground
            user = self.traffic.user
            user_xy = None
            if layout is not None and user is not None and user.latitude:
                user_xy = layout.to_xy(user.latitude, user.longitude)
            # The pilot's aeroplane, for keeping the buses off it: its type
            # if known, else as long as the longest the buses serve.
            self.handling.pilot_footprint = None
            if user_xy is not None and user.on_ground:
                self.handling.pilot_footprint = (
                    user_xy[0], user_xy[1], float(user.heading),
                    fuselage_length(user.type_code) if user.type_code else 45.0)
            self.handling.elevation_ft = self.traffic.field.elevation_ft
            # Whose doors read shut, for the tug: the door layer's answer
            # where it is working, everybody's otherwise.
            ready = None
            if self.config.traffic.handling_doors and self.simdoors.available:
                ready = set()
                for one in standing:
                    object_id = (one.object_id if one.observed
                                 else self.simtraffic.object_id(one.callsign))
                    if self.simdoors.can_push(object_id):
                        ready.add(one.callsign)
            # The motion engine's rates go by how far each actor is from
            # the pilot, in the layout's metres.
            if hasattr(self.simvehicles, "motion"):
                self.simvehicles.motion.viewer_xy = user_xy
            self._read_the_doors(standing, now)
            vehicles = self.handling.positions(standing, layout, user_xy, now,
                                               ready=ready)
            self.simvehicles.sync(vehicles, now)
            if self.config.traffic.handling_doors:
                # Which simulator object each handled aeroplane is, and
                # which model: this program's by what the injector drew it
                # as, somebody else's by what the reader saw.
                roles = self.handling.doors(now)
                wanted = {}
                for one in standing:
                    if one.observed:
                        object_id, title = one.object_id, one.title
                    else:
                        object_id = self.simtraffic.object_id(one.callsign)
                        title = self.simtraffic.title_of(one.callsign)
                    if object_id:
                        wanted[object_id] = (title, one.callsign,
                                             roles.get(one.callsign, set()))
                self.simdoors.sync(wanted, now)
        except Exception:
            # Once at INFO, because a pass that raises every tick looks,
            # from the apron, exactly like one that is not running.
            if not self._handling_raised:
                self._handling_raised = True
                log.info("the ground handling raised; it is being retried "
                         "every tick", exc_info=True)
            else:
                log.debug("the ground handling raised", exc_info=True)

    def _say_observed(self) -> None:
        """Say once that the simulator's own traffic is being worked.

        Only when the count changes by enough to be news. A pilot who has
        started FSLTL wants to know the radio has noticed it -- "I have a
        hundred aeroplanes out there and the controller is talking to none of
        them" is otherwise a question with no answer on screen -- and a line
        every time an aeroplane taxis out of range is not news.
        """
        count = self.traffic.observed_count
        if count == self._observed_said:
            return
        if count and abs(count - self._observed_said) < 3 and self._observed_said:
            return
        self._observed_said = count
        if not count:
            return
        self.emit("info",
                  f"Working {count} aircraft from the simulator's own traffic.",
                  key="log.traffic_observed", count=str(count))

    # Nobody waits forever. A transmission still queued this long after it fell
    # due has been overtaken by the flight it belongs to, and saying it now
    # would be worse than not saying it.
    TRAFFIC_STALE_S = 25.0

    # How long an exchange is remembered once something has been decided about
    # it. Its halves reach this queue separately, as each falls due, so the
    # decision about the first has to still be here when the last arrives --
    # and the last of a three-part exchange is queued about six seconds after
    # the first, so a minute and a half is generous rather than tight.
    EXCHANGE_MEMORY_S = 90.0

    def _exchange_heard(self, mark: int, heard: bool, now: float) -> None:
        """Remember what was decided about an exchange, and forget old ones."""
        if not mark:
            return
        self._exchanges[mark] = (heard, now)
        if len(self._exchanges) > 64:
            self._exchanges = {
                key: value for key, value in self._exchanges.items()
                if now - value[1] < self.EXCHANGE_MEMORY_S
            }

    # The chance a flight's first call goes unanswered while the frequency
    # is busy with somebody else's exchange -- never when it is not, because
    # then there was nobody to be talking over. Half of them was a frequency
    # that sounded broken rather than busy. UNVERIFIED: 12% is a figure
    # chosen, not measured.
    MISSED_FIRST_CALL = 0.12

    def _congested(self, station: Station, parsed) -> bool:
        """Whether an exchange between the controller and another aeroplane
        is half said on this frequency: its opening heard
        (``_exchanges``), and the rest still to come -- in this queue, or
        still in the traffic's own schedule, where a readback waits until it
        falls due. Reading only this queue ended the wait in the gap before
        the readback. Never for a distress or urgency call, which is
        answered whatever else is going on."""
        if not self.config.immersion.controller.congestion:
            return False
        if parsed.intent in (Intent.EMERGENCY, Intent.URGENCY,
                             Intent.MINIMUM_FUEL):
            return False
        to_come = list(self._traffic_queue)
        if self.traffic is not None:
            to_come += self.traffic.pending
        for call in to_come:
            if not self._on_this_channel(call, station):
                continue
            decided = self._exchanges.get(call.exchange)
            if decided is not None and decided[0]:
                return True
        return False

    def _first_call_missed(self, session: FlightSession, station: Station) -> bool:
        """Whether this first call goes unanswered, decided once per flight
        and station and seeded from both, so the same flight is treated the
        same way every time it is replayed. The next call is answered."""
        key = station_key(station)
        if session.first_call_missed == key:
            return False
        seed = hashlib.sha256(
            f"{session.aircraft.callsign.written}|{key}".encode()).digest()
        missed = seed[0] / 255.0 < self.MISSED_FIRST_CALL
        decision("congestion", station=station.callsign,
                 said="nothing" if missed else "answered", why="first call")
        if missed:
            session.first_call_missed = key
        return missed

    def _say_held_reply(self, now: float) -> bool:
        """The answer held behind somebody else's exchange, once it is over.
        True when it went out."""
        held = self._held_reply
        if held is None:
            return False
        reply, station, spoken, since = held
        if self.station is None or station_key(self.station) != station_key(station):
            self._held_reply = None
            return False
        # The exchange is over when its last half has been said, not when it
        # has been taken off the queue to be said: so not while the radio is
        # still carrying it, nor in the breath the queue leaves after one.
        if (self._congested(station, ParsedIntent()) or not self.quiet_enough()
                or now < self._frequency_clear_at):
            return False
        self._held_reply = None
        session = self.session
        if reply.instruction is not None:
            # Issued now, so the readback is chased from when it was heard.
            reply.instruction.issued_at = time.monotonic()
        decision("congestion", station=station.callsign, said="answered_after",
                 waited_s=round(now - since, 1))
        self.emit("atc", reply.text, station.callsign)
        if session is not None:
            session.note("atc", as_written(reply.text))
        RADIO.write("atc", text=reply.text, language=spoken,
                    station=station.callsign, frequency=round(station.mhz, 3),
                    instruction=(reply.instruction.kind
                                 if reply.instruction is not None else ""),
                    held=True)
        self.speak(reply.text, station, language=spoken)
        self._frequency_clear_at = now + 0.6
        return True

    def _speak_from_the_queue(self, station: Station, now: float) -> None:
        """Put the next queued transmission on the air, if the air is free.

        Not while a person is working it. The invented traffic is this
        program's, and playing it over a real controller's frequency would put
        aeroplanes on their radio that they cannot see and did not clear.
        """
        if self.stand_down:
            return
        # An answer the pilot was told to stand by for goes before anything
        # new from the traffic.
        if self._say_held_reply(now):
            return
        if not self._traffic_queue:
            return

        # Anything that belongs to a frequency the pilot has since left is
        # dropped, and so is anything that has waited too long -- but by the
        # exchange rather than by the transmission. A call, its clearance and
        # its readback go stale one after another, so a busy frequency used to
        # bin the aeroplane's call and then say the controller's answer to it,
        # or say the call and bin the answer. Either way the frequency sounded
        # like it was losing transmissions, which it was.
        #
        # So: an exchange that has been started is always finished, and one
        # that has gone stale before a word of it was said is dropped whole.
        keep: list = []
        for call in self._traffic_queue:
            # A closed position has no controller for the traffic to talk to
            # either, so its frequency carries nobody's half of an exchange.
            if not self._on_this_channel(call, station) \
                    or self.brain.closed(station):
                continue
            decided = self._exchanges.get(call.exchange)
            if decided is not None and not decided[0]:
                continue                    # its opening was never heard
            if decided is not None or now - call.at < self.TRAFFIC_STALE_S:
                keep.append(call)
                continue
            # Stale, and nothing of it has been said. The whole exchange goes,
            # including the halves that have not reached this queue yet.
            self._exchange_heard(call.exchange, False, now)
        self._traffic_queue = [
            call for call in keep
            if not (call.exchange in self._exchanges
                    and not self._exchanges[call.exchange][0])
        ]
        if not self._traffic_queue:
            return

        # A go-around does not queue behind two taxi instructions. It is the
        # one thing a tower says that the rest of the frequency waits for, and
        # the aeroplane is already climbing away while it is said.
        self._traffic_queue.sort(key=lambda call: not call.urgent)

        # Never on top of the user, never on top of the controller answering
        # the user, and never on top of the cabin.
        if not self.quiet_enough() or now < self._frequency_clear_at:
            return

        call = self._traffic_queue.pop(0)
        # From here the rest of this exchange is heard however long the
        # frequency stays busy: half an exchange is the thing being fixed.
        self._exchange_heard(call.exchange, True, now)
        self._speak_traffic(call, station)
        # Long enough for the transmission to finish before the next one
        # starts, which the player being busy also enforces.
        self._frequency_clear_at = now + 0.6

    @staticmethod
    def _on_this_channel(call, station: Station) -> bool:
        """Whether a queued call is heard on the station tuned: by channel
        where the call knows its own (Ground and Tower on one frequency are
        one frequency), else by position."""
        if getattr(call, "mhz", 0.0) and station.mhz:
            return same_channel(call.mhz, station.mhz, 0)
        return call.position == station.position

    def _speak_traffic(self, call, station: Station) -> None:
        """Put one AI transmission on the air.

        A controller line is spoken by the controller the user is already
        talking to -- same facility, same position, so the same voice. A pilot
        line gets a voice of its own, keyed on the callsign, and drawn from the
        accent pool of the operator's own country: an Air France crew at
        Frankfurt still sounds French.
        """
        self.emit("traffic", call.text, call.speaker)
        RADIO.write(
            "traffic", text=call.text, language=call.language,
            speaker=call.speaker, from_pilot=call.from_pilot,
            station=station.callsign, frequency=round(station.mhz, 3),
            flight=call.flight.written if call.flight else "",
            rules=("IFR" if call.flight and call.flight.ifr else "VFR"),
            phase=call.flight.phase if call.flight else "",
        )
        try:
            if call.from_pilot:
                operator = call.flight.operator
                home = (operator.home[0] if operator and operator.home
                        else station.ident)
                transmission = self.synth.speak(
                    self.spoken_form(call.text),
                    facility=call.flight.written,
                    position="PILOT",
                    ident=home,
                    language=call.language,
                    distance_nm=6.0,
                    altitude_ft=call.flight.altitude_ft,
                    radio=self.config.audio.radio_effects,
                )
            else:
                transmission = self.synth.speak(
                    self.spoken_form(call.text),
                    facility=station.facility or station.ident,
                    position=station.position,
                    ident=station.ident,
                    language=call.language,
                    distance_nm=station.distance_nm,
                    altitude_ft=self.state.altitude_ft,
                    radio=self.config.audio.radio_effects,
                )
        except Exception:
            log.debug("traffic synthesis failed", exc_info=True)
            return
        self.player.play(transmission.audio, transmission.sample_rate,
                         label=call.speaker)

    def _follow_region(self, state: AircraftState) -> None:
        """Keep the language set in step with where the aeroplane is."""
        if not state.has_position:
            return
        now = time.monotonic()
        if now - self._last_region_check < 20.0:
            return
        self._last_region_check = now
        airport = self.navdb.nearest_airport(
            state.latitude, state.longitude, max_nm=250.0, min_rank=1
        )
        if airport is not None:
            self.follow_languages(airport.ident)

    def _watch_surface(self, previous: AircraftState, state: AircraftState,
                       session: FlightSession) -> None:
        """Let the controller react to what the aircraft actually did."""
        station = self.station
        if station is None or self.stand_down:
            return
        spoken = self._language_for(station.ident, self.last_language)
        try:
            reply = self.watch.look(
                session, station, state, previous, language=spoken
            )
        except Exception:
            log.debug("surface watch failed", exc_info=True)
            return
        if reply is None or not reply.speak:
            return

        # A takeoff is one poll wide. Skipping the look while the speaker is
        # busy would lose the whole event rather than delay it, and the player
        # queues anyway, so this transmits into the queue like any other.
        self.emit("atc", reply.text, station.callsign)
        session.note("atc", as_written(reply.text))
        self.speak(reply.text, station, language=spoken)
        self._radio_unprompted(reply.text, station, "surface", spoken,
                               reply.instruction)

    def _work_the_departure(self, state: AircraftState,
                            session: FlightSession) -> None:
        """Clear a departure the tower has already put on the runway.

        Not behind the reminders setting, for the same reason the handoffs are
        not: an aeroplane told to line up and wait has been given half an
        instruction, and the other half is the tower's to give.
        """
        station = self.station
        if station is None or self.stand_down or not self.quiet_enough():
            return
        try:
            reply = self.departures.look(
                session, station, state, time.monotonic(),
                weather_for=self.weather_for,
                language=self._language_for(station.ident, self.last_language),
            )
        except Exception:
            log.debug("the departure watch raised", exc_info=True)
            return
        if reply is None or not reply.speak:
            return
        spoken = self._language_for(station.ident, self.last_language)
        self.emit("atc", reply.text, station.callsign)
        session.note("atc", as_written(reply.text))
        self.speak(reply.text, station, language=spoken)
        self._radio_unprompted(reply.text, station, "departure", spoken,
                               reply.instruction)

    def _work_the_climb(self, state: AircraftState,
                        session: FlightSession) -> None:
        """The next step of an IFR climb, a flight off its SID, and the
        centre's side of the route (atc/routing.py)."""
        now = time.monotonic()
        self._say_watched(state, session, "climb",
                          lambda station, spoken: self.climbs.look(
                              session, station, state, now, spoken))
        self._say_watched(state, session, "sid",
                          lambda station, spoken: self.sid_watch.look(
                              session, station, state, now, spoken))
        self._say_watched(state, session, "route",
                          lambda station, spoken: self.route_watch.look(
                              session, station, state, now, spoken))

    def _sim_contacts(self) -> list:
        """The simulator's aeroplanes, without the pilot's own."""
        if not self.config.traffic.from_simulator:
            return []
        own = (self.session.aircraft.callsign.written.upper()
               if self.session is not None else "")
        pilot = getattr(self.simwatch, "user_object_id", None)
        found = [one for one in self.simwatch.poll()
                 if one.object_id != pilot
                 and (one.callsign or "").strip().upper() != own]
        self._log_picture(found)
        return found

    # How often the controller's picture of the simulator's traffic is
    # written to the arrival log when nothing in it has changed.
    PICTURE_EVERY_S = 60.0

    def _log_picture(self, found: list) -> None:
        """A row of what the simulator's traffic feed gave the controller:
        how many, how many placed, and the nearest few with what they were
        read as. Written when the count changes and once a minute otherwise,
        so a flight's log shows whether FSLTL's and the simulator's
        aeroplanes ever reached the tower."""
        now = time.monotonic()
        count = len(found)
        last = getattr(self, "_picture_logged", None)
        if last is not None and last[0] == count and now - last[1] < self.PICTURE_EVERY_S:
            return
        self._picture_logged = (count, now)
        state = self.state
        nearest = []
        if state is not None and state.has_position:
            placed = [one for one in found if getattr(one, "positioned", True)]
            placed.sort(key=lambda one: state.distance_to(one.latitude, one.longitude))
            for one in placed[:5]:
                nearest.append(
                    f"{(one.callsign or one.registration or '?').strip()}"
                    f"/{one.type_code or '?'}"
                    f" {state.distance_to(one.latitude, one.longitude):.1f}nm"
                    f" {round(one.altitude_ft or 0)}ft"
                    f"{' gnd' if one.on_ground else ''}")
        within = (sum(1 for one in found if getattr(one, "positioned", True)
                      and state.distance_to(one.latitude, one.longitude) <= 10.0)
                  if state is not None and state.has_position else None)
        decision("picture", observed=count, within_10nm=within,
                 nearest=nearest or None)

    def _watch_traffic(self, state: AircraftState, session: FlightSession) -> None:
        """Traffic calls for a flight on radar advisories
        (:class:`~wilcoatc.atc.trafficinfo.TrafficWatch`)."""
        station = self.station
        if station is None or self.stand_down or not self.quiet_enough():
            return
        spoken = self._language_for(station.ident, self.last_language)
        try:
            reply = self.traffic_watch.look(session, station, state,
                                            language=spoken)
        except Exception:
            log.debug("the traffic watch raised", exc_info=True)
            return
        if reply is None or not reply.speak:
            return
        self.emit("atc", reply.text, station.callsign)
        session.note("atc", as_written(reply.text))
        self.speak(reply.text, station, language=spoken)
        self._radio_unprompted(reply.text, station, "traffic", spoken,
                               reply.instruction)

    def _watch_points(self, state: AircraftState, session: FlightSession) -> None:
        """A reporting point flown past without a report
        (:class:`~wilcoatc.atc.watch.ReportingPointWatch`)."""
        station = self.station
        if station is None or self.stand_down or not self.quiet_enough():
            return
        spoken = self._language_for(station.ident, self.last_language)
        try:
            reply = self.point_watch.look(session, station, state,
                                          language=spoken)
        except Exception:
            log.debug("the reporting-point watch raised", exc_info=True)
            return
        if reply is None or not reply.speak:
            return
        self.emit("atc", reply.text, station.callsign)
        session.note("atc", as_written(reply.text))
        self.speak(reply.text, station, language=spoken)
        self._radio_unprompted(reply.text, station, "reporting_point", spoken,
                               reply.instruction)

    def _watch_runway(self, state: AircraftState, session: FlightSession) -> None:
        """The runway in use changing with the wind
        (:class:`~wilcoatc.atc.watch.RunwayWatch`)."""
        station = self.station
        if station is None or self.stand_down or not self.quiet_enough():
            return
        spoken = self._language_for(station.ident, self.last_language)
        try:
            reply = self.runway_watch.look(session, station, state,
                                           language=spoken)
        except Exception:
            log.debug("the runway watch raised", exc_info=True)
            return
        if reply is None or not reply.speak:
            return
        self.emit("atc", reply.text, station.callsign)
        session.note("atc", as_written(reply.text))
        self.speak(reply.text, station, language=spoken)
        self._radio_unprompted(reply.text, station, "runway_change", spoken,
                               reply.instruction)

    def _watch_airspace(self, state: AircraftState, session: FlightSession) -> None:
        """A VFR aeroplane about to enter a zone it needs a word for
        (:class:`~wilcoatc.atc.watch.AirspaceWatch`)."""
        station = self.station
        if station is None or self.stand_down or not self.quiet_enough():
            return
        spoken = self._language_for(station.ident, self.last_language)
        try:
            reply = self.airspace_watch.look(session, station, state,
                                             language=spoken)
        except Exception:
            log.debug("the airspace watch raised", exc_info=True)
            return
        if reply is None or not reply.speak:
            return
        self.emit("atc", reply.text, station.callsign)
        session.note("atc", as_written(reply.text))
        self.speak(reply.text, station, language=spoken)
        self._radio_unprompted(reply.text, station, "airspace", spoken,
                               reply.instruction)

    def _watch_restriction(self, state: AircraftState,
                           session: FlightSession) -> None:
        """A VFR flight above the altitude radar holds it under
        (:class:`~wilcoatc.atc.watch.RestrictionWatch`)."""
        self._say_watched(state, session, "restriction",
                          lambda station, spoken: self.restriction_watch.look(
                              session, station, state, time.monotonic(),
                              language=spoken))

    def _watch_radar_service(self, state: AircraftState,
                             session: FlightSession) -> None:
        """A VFR flight's radar service ended as it leaves
        (:class:`~wilcoatc.atc.watch.RadarServiceWatch`)."""
        self._say_watched(state, session, "radar_service_end",
                          lambda station, spoken: self.radar_service_watch.look(
                              session, station, state, language=spoken))

    def _say_watched(self, state: AircraftState, session: FlightSession,
                     why: str, look) -> None:
        station = self.station
        if station is None or self.stand_down or not self.quiet_enough():
            return
        if not self.brain.staffed(station):
            return
        spoken = self._language_for(station.ident, self.last_language)
        try:
            reply = look(station, spoken)
        except Exception:
            log.debug("the %s watch raised", why, exc_info=True)
            return
        if reply is None or not reply.speak:
            return
        self.emit("atc", reply.text, station.callsign)
        session.note("atc", as_written(reply.text))
        self.speak(reply.text, station, language=spoken)
        self._radio_unprompted(reply.text, station, why, spoken,
                               reply.instruction)

    def _work_the_arrival(self, previous: AircraftState, state: AircraftState,
                          session: FlightSession) -> None:
        """Let the controller work the arrival instead of waiting to be asked.

        None of this is a reminder for a forgetful pilot, so none of it sits
        behind the setting that governs reminders. An aeroplane may not leave
        its cruising level or fly an approach without being told to, and a
        tower that has to be asked for a landing clearance is not a tower.
        """
        station = self.station
        if station is None or self.stand_down:
            return
        if session.destination is not None:
            self._ask_for_approaches(session.destination.ident)
        # Never over the top of something else: not the speaker, not the
        # pilot with the button down, not the controller working out what the
        # pilot just said, and not the cabin. Unlike a takeoff, none of these
        # is one poll wide -- an arrival eighteen miles out is still eighteen
        # miles out on the next look -- so each of them can wait for the
        # frequency to go quiet rather than talking across it.
        if not self.quiet_enough():
            return
        spoken = self._language_for(station.ident, self.last_language)
        why = "arrival"
        try:
            # The circuit first: a base the tower promised to call, and a
            # report it asked for and did not get. One transmission a look.
            reply = self.arrivals.circuit(session, station, state,
                                          language=spoken)
            if reply is not None and reply.speak:
                why = "circuit"
            else:
                reply = self.arrivals.look(
                    session, station, state, previous,
                    weather_for=self.weather_for, language=spoken,
                )
        except Exception:
            log.debug("the arrival watch raised", exc_info=True)
            return
        if reply is None or not reply.speak:
            return

        if why != "circuit":
            # The circuit's own calls log themselves; anything else the tower
            # says unprompted to a VFR flight gets the same row.
            self.brain.note_circuit(
                session, station, state,
                reply.instruction.kind if reply.instruction else why)
        self.emit("atc", reply.text, station.callsign)
        session.note("atc", as_written(reply.text))
        self.speak(reply.text, station, language=spoken)
        self._radio_unprompted(reply.text, station, why, spoken,
                               reply.instruction)

    #: How close to the departure runway counts as being at the holding
    #: point. A holding point sits a few hundred feet back from the threshold,
    #: which is inside a third of a mile; a taxiway running the length of the
    #: runway is never that close to the *end* it is measured from.
    HOLDING_POINT_NM = 0.35

    #: The phases an aeroplane can still be taxiing out in. Anything later is
    #: already at the runway or past it.
    _TAXIING_OUT = frozenset({Phase.PREFLIGHT, Phase.CLEARED, Phase.PUSHBACK,
                              Phase.TAXI_OUT})

    #: And the ones that mean it has not started rolling yet, so that starting
    #: to roll is a transition out of any of them rather than only out of the
    #: first.
    _NOT_MOVING_YET = frozenset({Phase.PREFLIGHT, Phase.CLEARED,
                                 Phase.PUSHBACK})

    def _reached_the_holding_point(self, state: AircraftState,
                                   session: FlightSession) -> bool:
        """Whether the aeroplane has taxied up to the runway it is leaving from.

        Measured to the end it is departing from rather than to the runway as
        a whole. A taxiway running the length of a runway is a few hundred
        feet from it for its whole length, and an aeroplane halfway down that
        taxiway has not reached anything.
        """
        if not (state.on_ground and state.has_position):
            return False
        runway = normalize_runway(session.departure_runway or "")
        airport = session.departure
        if not runway or airport is None:
            return False
        for name, _heading, published in self.navdb.runway_ends(airport.ident):
            if name != runway:
                continue
            if name == published.le_ident:
                lat, lon = published.le_lat, published.le_lon
            else:
                lat, lon = published.he_lat, published.he_lon
            if not (lat or lon):
                return False
            return haversine_nm(state.latitude, state.longitude,
                                lat, lon) <= self.HOLDING_POINT_NM
        return False

    # The on-ground flag has to say the same thing this many looks running
    # before it is believed. Two looks is half a second at the usual poll.
    GROUND_SAMPLES = 2

    def _debounced_ground(self, previous: AircraftState,
                          state: AircraftState) -> tuple[AircraftState, AircraftState]:
        """The two readings with the on-ground flag debounced.

        A change is believed once the raw flag has said it GROUND_SAMPLES
        looks running; until then the watchers see the old value.
        """
        if self._ground_seen is None:
            self._ground_seen = bool(previous.on_ground)
        before = self._ground_seen
        if bool(state.on_ground) != self._ground_seen:
            self._ground_disagrees += 1
            if self._ground_disagrees >= self.GROUND_SAMPLES:
                self._ground_seen = bool(state.on_ground)
                self._ground_disagrees = 0
        else:
            self._ground_disagrees = 0
        return (replace(previous, on_ground=before),
                replace(state, on_ground=self._ground_seen))

    # A liftoff this soon after a touchdown, still fast and on the same
    # runway, is off the rollout and not a takeoff. Back on the ground within
    # BOUNCE_S it was a bounce; past it, or past BOUNCE_AGL_FT, it is a
    # touch-and-go or a go-around.
    ROLLOUT_WINDOW_S = 60.0
    ROLLOUT_MIN_KT = 40.0
    BOUNCE_S = 5.0
    BOUNCE_AGL_FT = 200.0

    def _note_rollout_liftoff(self, previous: AircraftState, state: AircraftState,
                              session: FlightSession) -> None:
        """Whether this liftoff is off a landing rollout, decided before the
        surface watch reads it as a takeoff."""
        if not (previous.on_ground and not state.on_ground):
            return
        if not session.touchdown_at:
            return
        elapsed = time.monotonic() - session.touchdown_at
        runway = ""
        if self.station is not None:
            runway = self.navdb.runway_at(self.station.ident, previous.latitude,
                                          previous.longitude, previous.heading_true)
        same_runway = (not runway or not session.touchdown_runway
                       or normalize_runway(runway) == normalize_runway(
                           session.touchdown_runway))
        if (elapsed <= self.ROLLOUT_WINDOW_S and same_runway
                and session.rollout_min_kt >= self.ROLLOUT_MIN_KT):
            session.rollout_liftoff_at = time.monotonic()
            # The landing clearance covered this landing and is wanted again
            # if it was only a bounce.
            session.cleared_landing = session.touchdown_cleared
            decision("liftoff_after_touchdown", verdict="pending",
                     runway=session.touchdown_runway,
                     since_touchdown_s=round(elapsed, 1),
                     slowest_kt=round(session.rollout_min_kt),
                     speed_kt=round(float(previous.ground_speed_kt or 0.0)))

    def _after_rollout_liftoff(self, previous: AircraftState, state: AircraftState,
                               session: FlightSession) -> None:
        """A bounce, a touch-and-go, or a go-around: what a liftoff off the
        rollout turned out to be."""
        if not session.rollout_liftoff_at:
            return
        airborne_s = time.monotonic() - session.rollout_liftoff_at
        if state.on_ground:
            # Down again: it was a bounce. The touchdown has just been
            # recorded again by _update_phase, with the landing clearance it
            # was restored for, so nothing is said.
            session.rollout_liftoff_at = 0.0
            decision("bounce", runway=session.touchdown_runway,
                     airborne_s=round(airborne_s, 1),
                     speed_kt=round(float(state.ground_speed_kt or 0.0)))
            return
        field = self.navdb.airport(self.station.ident) if self.station else None
        elevation = float(getattr(field, "elev_ft", 0.0) or 0.0)
        above = state.altitude_agl_ft or max(0.0, state.altitude_ft - elevation)
        if airborne_s < self.BOUNCE_S and above < self.BOUNCE_AGL_FT:
            return
        session.rollout_liftoff_at = 0.0
        # Where nobody clears anything -- an AFIS, an unattended field -- a
        # VFR aeroplane off the rollout is flying the circuit it was flying:
        # there was no touch-and-go to clear, so none was recorded, and every
        # one at Kortrijk was taken for a go-around and told to join.
        nobody_clears = not session.ifr and not self.brain.issues_clearances(
            self.station)
        if session.cleared_option or session.pattern_work or nobody_clears:
            # Round the circuit: the touch-and-go it was cleared for.
            session.cleared_option = False
            session.cleared_landing = False
            session.phase = Phase.DEPARTURE
            decision("liftoff_after_touchdown", verdict="touch-and-go",
                     airborne_s=round(airborne_s, 1), agl_ft=round(above))
            return
        # Round again: a go-around after touchdown, worked like any other.
        decision("liftoff_after_touchdown", verdict="go-around",
                 airborne_s=round(airborne_s, 1), agl_ft=round(above))
        station = self.station
        if station is None or field is None or not self.brain.staffed(station):
            session.cleared_landing = False
            session.going_around = True
            session.phase = Phase.APPROACH
            return
        spoken = self._language_for(station.ident, self.last_language)
        p = self.brain.for_station(station, spoken)
        session.aircraft.language = getattr(p, "language", "en")
        reply = self.brain.go_around(session, station,
                                     self.weather_for(field.ident), p, field,
                                     told=True)
        self.brain.note_circuit(session, station, state, "go_around")
        self.emit("atc", reply.text, station.callsign)
        session.note("atc", as_written(reply.text))
        self.speak(reply.text, station, language=spoken)
        self._radio_unprompted(reply.text, station, "go_around_after_touchdown",
                               spoken, reply.instruction)

    # Still on the ground this long after a touchdown, the circuits are over.
    # A touch-and-go is airborne again in well under that; a stop-and-go
    # (which needs a clearance of its own) does not slow below the rollout
    # speed the bounce detection above already uses.
    FULL_STOP_AFTER_S = 20.0

    def _full_stop_after_touchdown(self, state: AircraftState,
                                   session: FlightSession) -> None:
        """End the circuits of an aeroplane that landed and stayed down.

        Asking for circuits set a flag that only a pilot's words could clear,
        and a pilot who simply landed and taxied in kept it -- so the next
        arrival of the session, hours later, was cleared for the option.
        """
        if not session.pattern_work or not session.touchdown_at:
            return
        if session.full_stop_judged_at == session.touchdown_at:
            return
        if not state.on_ground or session.rollout_liftoff_at:
            return
        if time.monotonic() - session.touchdown_at < self.FULL_STOP_AFTER_S:
            return
        session.full_stop_judged_at = session.touchdown_at
        session.pattern_work = False
        decision("circuits_ended", why="landed and stayed down",
                 runway=session.touchdown_runway)

    def _update_phase(self, previous: AircraftState, state: AircraftState,
                      session: FlightSession) -> None:
        if previous.on_ground and not state.on_ground and session.rollout_liftoff_at:
            # Off the rollout, not a takeoff: back to the approach, and the
            # "landed without a clearance" advisory stays spent so a bounce
            # is not told off twice.
            session.phase = Phase.APPROACH
            session.occupying_runway = ""
            return
        if previous.on_ground and not state.on_ground:
            if session.phase.outbound:
                session.phase = Phase.DEPARTURE
            # The clearance was for this departure and is now spent. Leaving
            # it set meant a circuit could be flown all day on one clearance.
            session.cleared_takeoff = False
            # A new flight from the ground, so the last circuit's legs are
            # history: the tower waits for this one to report its own.
            session.pattern_leg = ""
            session.vrp_reported = ""
            session.expected_vrp = ""
            session.cleared_runway = ""
            session.occupying_runway = ""
            # A new landing is a new event, so its advisory is armed again,
            # and the last one is no longer the one being taxied in from.
            session.advisories.discard("landed_without_clearance")
            session.landed_cleared = False
        elif not previous.on_ground and state.on_ground:
            session.phase = Phase.LANDED
            session.going_around = False
            # Remembered, so a liftoff straight off this rollout can be told
            # from a takeoff.
            session.touchdown_at = time.monotonic()
            session.touchdown_cleared = bool(session.cleared_landing
                                             or session.cleared_option)
            session.rollout_min_kt = float(state.ground_speed_kt or 0.0)
            session.touchdown_runway = (session.arrival_runway
                                        or session.occupying_runway)
            # Kept before it is spent: the welcome on the way to the gate is
            # for an aeroplane that was cleared to be here.
            session.landed_cleared = session.cleared_landing
            session.cleared_landing = False
            session.cleared_takeoff = False
            session.landing_withheld = False
            session.landing_anticipated = ""
            if session.flight_plan_open:
                # Nobody closes it on landing: that is the pilot's call to
                # flight service, or a controller asked to pass it on (FAA
                # AIM 5-1-14). Logged, so a flight plan left open is seen.
                decision("flight_plan", said="still_open_after_landing",
                         airport=getattr(session.destination, "ident", "") or None)
            # The landing clearance covers the rollout, so the runway stays
            # allowed until the aircraft is off it. That holds even for a
            # landing nobody cleared: it has already been called, and calling
            # the rollout again as an incursion would be telling the pilot off
            # twice for one thing.
            session.cleared_runway = (
                session.arrival_runway or session.occupying_runway
                or session.cleared_runway
            )
            session.advisories.discard("departed_without_clearance")
        elif state.on_ground and session.touchdown_at and \
                session.phase is Phase.LANDED and \
                float(state.ground_speed_kt or 0.0) < session.rollout_min_kt:
            # The slowest the rollout got: below forty knots it is taxiing,
            # and a liftoff after that is a takeoff.
            session.rollout_min_kt = float(state.ground_speed_kt or 0.0)
        elif (state.on_ground and state.ground_speed_kt > 5
                and session.phase in self._NOT_MOVING_YET):
            # From any of the phases that mean "at the stand", not only from
            # preflight. An aeroplane that has read back its IFR clearance is
            # in CLEARED, and CLEARED had no way out of it except a taxi
            # clearance -- so a pilot who took their clearance and then simply
            # taxied stayed "at the stand" at twenty knots all the way to the
            # runway. Everything hung on the phase went with it: the holding
            # point was never reached, the tower was never called, and the
            # cabin never did the taxi checklist or the safety demonstration,
            # because those ask whether the aeroplane is taxiing and it said
            # no.
            session.phase = Phase.TAXI_OUT
        elif (state.on_ground and session.phase in self._TAXIING_OUT
                and self._reached_the_holding_point(state, session)):
            # Nothing ever set this phase, which is why nobody was sent to the
            # tower and why the cabin was never told to sit down: a flight went
            # from taxiing to airborne with the holding point in between and no
            # name for the part in the middle.
            session.phase = Phase.HOLDING_SHORT
        elif not state.on_ground:
            if session.phase is Phase.DEPARTURE and self._enroute_now(state,
                                                                     session):
                session.phase = Phase.ENROUTE
            elif session.phase is Phase.ENROUTE and state.descending and \
                    state.altitude_ft < 25000:
                session.phase = Phase.DESCENT

    #: Above this, a departure is en route whatever else it is doing. Below
    #: it, the aeroplane has to have stopped climbing and left the circuit.
    ENROUTE_FT = 10000.0

    #: How far from the departure field a levelled-off aeroplane has to be
    #: before it counts as having gone somewhere. A circuit is flown inside
    #: three miles of the runway; this is well outside any of them.
    LEFT_THE_CIRCUIT_NM = 12.0

    def _enroute_now(self, state: AircraftState,
                     session: FlightSession) -> bool:
        """Whether a departure has become a flight going somewhere.

        Ten thousand feet used to be the whole of the test, and it silently
        ended the flight for anybody who never went that high. The phase stays
        DEPARTURE, DEPARTURE is an outbound phase, and every outbound phase is
        excluded from the arrival watch -- so a short hop at eight thousand
        feet flew its whole approach, lined up on final, and was never cleared
        to land, because as far as the controller was concerned it was still
        departing. Nothing in the log said so: the tower simply never spoke.

        So height is one way of qualifying and no longer the only one. An
        aeroplane that has stopped climbing and is a dozen miles from where it
        took off has gone somewhere, whatever its altitude. Circuits are
        excluded by both halves of that -- a circuit is flown within three
        miles of the runway, and by ``pattern_work`` where the pilot has said
        outright that is what they are doing.
        """
        if state.altitude_ft > self.ENROUTE_FT:
            return True
        if session.pattern_work or state.climbing:
            return False
        field = session.departure
        if field is None or not state.has_position:
            return False
        return state.distance_to(field.lat, field.lon) > self.LEFT_THE_CIRCUIT_NM

    def _watch_for_humans(self, state: AircraftState) -> None:
        """Notice a controller logging on or off the frequency in COM1."""
        if not state.com1_active:
            return
        lat, lon = ((state.latitude, state.longitude)
                    if state.has_position else (0.0, 0.0))
        found = self.online.on_frequency(state.com1_active, lat, lon)
        before = self.human
        self.human = found
        if found is not None and (before is None
                                  or before.callsign != found.callsign):
            self.emit(
                "info",
                f"{state.com1_active:.3f} - {found.describe()} is working "
                "this frequency. WilcoATC will stay quiet on it.",
                key="log.human_online", mhz=f"{state.com1_active:.3f}",
                station=found.callsign, network=found.network.upper(),
            )
        elif found is None and before is not None and self.station is not None:
            # They have logged off, so this program has the frequency again.
            self.emit(
                "info",
                f"{state.com1_active:.3f} - {self.station.callsign} "
                f"({self.station.ident}, {self.station.distance_nm:.0f} NM).",
                key="log.tuned", mhz=f"{state.com1_active:.3f}",
                station=self.station.callsign, airport=self.station.ident,
                distance=f"{self.station.distance_nm:.0f}",
            )

    def _check_announcements(self) -> None:
        """Say so if the recorded announcements were asked for and are absent.

        Two ways that happens and both used to be silent. The folder the pilot
        typed is not there under any reading of the name, or it is there and
        holds nothing this program recognises. Either way the switch is on,
        the cabin falls back to the synthesised crew, and a pilot who has just
        installed a sound pack is left listening for it.

        Once per flight, and only when the recordings were actually asked
        for: somebody who has never turned them on does not need a row about
        a folder they have never heard of.
        """
        if not (self.config.immersion.enabled
                and self.config.immersion.recordings):
            return
        missing = announcements_missing(self.config)
        if missing:
            self.emit("info",
                      f"No announcements folder called \"{missing}\". The "
                      f"cabin is using its own voices.",
                      key="log.announcements_missing", folder=missing)
            return
        try:
            found = self.announcements.count
        except Exception:
            log.debug("could not count the announcements", exc_info=True)
            return
        if not found:
            self.emit("info",
                      "Recorded announcements are switched on, but no audio "
                      "files were found. The cabin is using its own voices.",
                      key="log.announcements_empty")

    def _cabin_trouble(self, detail: str) -> None:
        """The cabin cannot reach the speaker. Say so rather than go quiet.

        This is the difference between a bug report that says "the crew
        stopped working" and one that says which device refused. The cabin
        opens a second output stream alongside the radio's, and an audio stack
        can refuse that for reasons this program has no say in -- a device
        held exclusively by the simulator, a sample rate the driver will not
        take. Every one of those used to arrive as a first officer who had
        simply stopped talking, because the failure happened on the player's
        own thread and nothing was reading it.

        The radio is untouched by it, which is why this is a notice rather
        than an error: the controller still answers, and what has been lost is
        the half of the aeroplane that was optional to begin with.
        """
        self.emit("error",
                  "The cabin and the first officer cannot reach the speaker. "
                  f"The radio is unaffected. ({detail})",
                  key="log.cabin_no_audio", detail=detail)

    def radio_busy(self) -> bool:
        """Whether the frequency is in use right now.

        The speaker, the transmit button, and the gap in between while what
        the pilot said is being recognised and answered. A method rather than
        a property because the cabin is handed it as a callable: the crew are
        given a way to ask, not a reference to the engine.
        """
        return bool(self.player.busy or self.microphone.recording
                    or self._busy.locked())

    def quiet_enough(self) -> bool:
        """Whether an unprompted transmission can go out now.

        The radio, plus the cabin. An answer to something the pilot just said
        goes out regardless -- they asked, and they are waiting -- but nothing
        the controller volunteers is worth talking over the purser for. The
        two audio paths are separate so that neither has to queue behind the
        other, and this is what keeps that from becoming both at once.
        """
        return not (self.radio_busy() or self.cabin.busy)

    @property
    def stand_down(self) -> bool:
        """Whether a person is working this frequency, so nothing is said.

        Read before every transmission this program would otherwise make --
        an answer, an advisory, a handoff, a readback chased, the other
        aeroplanes. Talking over a real controller is the one failure that
        matters more than any of them.
        """
        return self.human is not None or not self.radio_powered

    @property
    def radio_powered(self) -> bool:
        """Whether the aeroplane's radio has power (atc/radio_power.py).

        Cold and dark, the radio is dead both ways: nothing the pilot says
        goes out and nothing on the frequency comes in -- which is why
        :attr:`stand_down`, read before every transmission, reads this too.
        Each change is a ``radio_power`` row in ``logs radio`` with what it
        was decided from, because "the controller will not answer" is a
        report that has to be answerable.
        """
        if not self.config.atc.radio_needs_power:
            return True
        from .atc.radio_power import decide

        on, why = decide(self.state)
        if on != getattr(self, "_radio_on", None):
            first = getattr(self, "_radio_on", None) is None
            self._radio_on = on
            RADIO.write("radio_power", powered=on, why=why,
                        com1_status=getattr(self.state, "com1_status", -1),
                        bus_volts=getattr(self.state, "main_bus_volts", -1.0),
                        title=getattr(self.state, "title", ""))
            if not on:
                self.emit("info", "The radio has no power: nothing is heard "
                          "and nothing goes out until the battery and the "
                          "avionics are on.", key="log.radio_off")
            elif not first:
                self.emit("info", "The radio has power.", key="log.radio_on")
        return on

    def _copilot_reads_back(self, session: FlightSession) -> bool:
        """The first officer works the radio, if that is what you asked for.

        Off by default and deliberately slow when it is on. A pilot who is
        practising radio work must never have the transmission taken out of
        their mouth, so the aeroplane waits long enough to be sure that
        somebody who was going to answer has answered.

        What it sends goes through the ordinary text path, so the controller
        checks it exactly as it checks yours -- including finding it wrong. A
        readback that skipped the parser would be a readback nothing could
        disagree with, which is not a readback.
        """
        settings = self.config.immersion
        if not (settings.enabled and settings.copilot
                and settings.radio_readbacks):
            return False
        pending = session.pending
        station = self.station
        if pending is None or station is None or pending.read_back:
            return False
        if pending.since_prompt_s < self.COPILOT_READBACK_WAIT_S:
            return False
        if not self.quiet_enough():
            pending.hold_off()
            return False

        from .atc.copilot import readback as build_readback

        spoken = self._language_for(station.ident, self.last_language)
        p = self.brain.for_station(station, spoken)
        session.aircraft.language = getattr(p, "language", "en")
        handoff = session.handoff_pending
        text = build_readback(
            pending, session.aircraft, p, spoken,
            station_name=(handoff.callsign_short if handoff is not None else ""),
        )
        if not text:
            return False
        self.emit("pilot", text, "First officer")
        try:
            self.handle_text(text, spoken)
        except Exception:
            log.debug("the first officer's readback failed", exc_info=True)
            return False
        return True

    def _chase_readback(self, session: FlightSession) -> None:
        """Ask again for a readback the pilot has gone quiet on."""
        station = self.station
        if not self.brain.staffed(station) or self.stand_down:
            return
        pending = session.pending
        if pending is None:
            return

        # If the other seat is working the radio, it answers before the
        # controller has to ask.
        if self._copilot_reads_back(session):
            return

        # The pilot cannot answer while the controller is still talking, while
        # they have the transmit button down, or while what they just said is
        # being recognised. The clock is restarted rather than merely paused,
        # so the wait is time the pilot actually had to reply in.
        if (self.player.busy or self.microphone.recording
                or self._busy.locked()):
            pending.hold_off()
            return
        spoken = self._language_for(station.ident, self.last_language)
        p = self.brain.for_station(station, spoken)
        session.aircraft.language = getattr(p, "language", "en")
        try:
            reply = self.brain.chase_readback(session, station, p)
        except Exception:
            log.debug("readback chase failed", exc_info=True)
            return
        if reply is None or not reply.speak:
            return
        self.emit("atc", reply.text, station.callsign)
        session.note("atc", as_written(reply.text))
        self.speak(reply.text, station, language=spoken)
        self._radio_unprompted(reply.text, station, "readback_chase", spoken,
                               reply.instruction)

    # How long the first officer waits before reading a clearance back for
    # you. Shorter than the controller's own patience, so the readback lands
    # before the chase rather than after it -- and long enough that a pilot
    # who was merely thinking still gets there first.
    COPILOT_READBACK_WAIT_S = 12.0

    # How high a departure has to be before the tower is finished with it, and
    # how slow an arrival has to be before it is off the runway and the tower
    # is finished with that. Both are the point at which a real controller
    # stops watching and passes you on.
    HANDOFF_ABOVE_AGL_FT = 700.0
    HANDOFF_BELOW_KT = 35.0

    def _hand_off_on_phase(self, state: AircraftState,
                           session: FlightSession) -> None:
        """Pass the aircraft on when the flight, not the map, says to.

        A tower hands a departure to radar as soon as it is safely climbing
        away and an arrival to ground as soon as it is clear of the runway; a
        departure sector passes the flight to the centre as it leaves the
        terminal area, and the centre passes it on again as it arrives in the
        next one. None of that is a reminder for a forgetful pilot, which is
        why none of it is behind the setting that governs reminders.

        Waiting instead until the aeroplane had flown past ninety per cent of
        the tuned station's range meant the tower never came back after
        departure, and for a centre -- two hundred and seventy miles -- meant
        the flight sat on one frequency from the climb to the flare.
        """
        station = self.station
        if not self.brain.staffed(station) \
                or self.stand_down or not self.quiet_enough():
            return
        # A station that has not been called has nobody to hand on: telling
        # an aeroplane that only tuned approach to contact the centre is a
        # controller talking to a frequency.
        if not session.established_with(station):
            return
        grace = self.config.atc.handoff_grace_s
        if time.monotonic() - session.handoff_offered_at < grace:
            return

        if station.position == "TWR":
            self._tower_hands_on(state, session, station)
        elif station.position in ("GND", "DEL"):
            self._ground_hands_on(state, session, station)
        elif station.position in ("DEP", "APP", "CTR"):
            self._radar_hands_on(state, session, station)

    def _ground_hands_on(self, state: AircraftState, session: FlightSession,
                         station: Station) -> None:
        """Send a departure to the tower as it reaches the holding point.

        Ground taxis you to the runway and then has nothing left to give you:
        the runway is the tower's. A pilot who taxied up to the hold and was
        left sitting on the ground frequency had to work out for themselves
        that the takeoff clearance was somebody else's to give, and this is
        the one handoff a real ground controller never forgets.

        Not a reminder for a forgetful pilot, so not behind the setting that
        governs reminders -- the same reasoning as the tower passing a
        departure to radar.
        """
        if session.phase is not Phase.HOLDING_SHORT:
            return
        # Its own key: "handoff:tower" is the arrival's, and spending it at
        # the holding point left every arrival on approach's frequency until
        # the pilot called the tower at six miles.
        if session.advise_once("handoff:ground_to_tower"):
            self._pass_to(session, station, "TWR",
                          runway=session.departure_runway)

    def _tower_hands_on(self, state: AircraftState, session: FlightSession,
                        station: Station) -> None:
        """Off the tower once climbing away, back to ground once clear of it."""
        airport = self.navdb.airport(station.ident)
        elevation = airport.elev_ft if airport else 0.0
        above = state.altitude_agl_ft or max(0.0, state.altitude_ft - elevation)

        # Sent round: back to whoever sequences arrivals, once climbing away.
        # Re-armed by each go-around, so a second one is handed on as well.
        if (session.going_around and not state.on_ground
                and above >= self.HANDOFF_ABOVE_AGL_FT):
            if session.advise_once("handoff:missed"):
                self._pass_to(session, station, "APP", "DEP")
            return

        climbing_out = (not state.on_ground and session.phase.outbound
                        and above >= self.HANDOFF_ABOVE_AGL_FT)
        if climbing_out:
            if self._kept_by_the_tower(session, station):
                # A VFR departure with no code and no advisories is the
                # tower's until it leaves the zone, and then it is let go:
                # handed to Departure, it was a radar service nobody had
                # asked for (and nobody ended).
                if self._in_the_zone(station, state):
                    return
                if session.advise_once("handoff:departure"):
                    decision("vfr_departure", airport=station.ident,
                             said="frequency_change", why="left the zone")
                    self._pass_to_station(session, station, None)
                return
            if session.advise_once("handoff:departure"):
                self._pass_to(session, station, "DEP", "APP")
            return

        # Slow, and off the runway: below 35 knots on the rollout is still on
        # the runway, with the next arrival a mile out, and the tower keeps
        # it until it has turned off.
        clear_of_the_runway = (
            state.on_ground and state.ground_speed_kt < self.HANDOFF_BELOW_KT
            and session.phase in (Phase.LANDED, Phase.TAXI_IN)
            and not self._on_a_runway(station.ident, state))
        if clear_of_the_runway and session.advise_once("handoff:arrival"):
            # And a word about where you have arrived, for an aeroplane that
            # was cleared to land here. A tower that has just had to tell
            # somebody they landed without a clearance does not then welcome
            # them to the city, so the one that was not cleared gets the
            # ordinary handoff.
            self._pass_to(session, station, "GND",
                          welcome=self._welcome_to(station)
                          if session.landed_cleared else "")

    # A field's zone where the airspace files have none for it: the radius of
    # an FAA Class D surface area, give or take.
    ZONE_NM = 4.5

    def _kept_by_the_tower(self, session: FlightSession,
                           station: Station) -> bool:
        """A VFR departure the tower does not hand to radar: no advisories
        asked for, no code given."""
        if session.ifr or session.flight_following or session.emergency:
            return False
        p = self.brain.for_station(station, self.last_language)
        return str(session.squawk) == str(p.vfr_squawk)

    def _in_the_zone(self, station: Station, state: AircraftState) -> bool:
        """Whether the aeroplane is still inside the tower's own airspace."""
        def own(volume) -> bool:
            return volume.airport == station.ident and volume.cls in ("B", "C", "D")

        try:
            if any(own(v) for v in self.navdb.airspace_volumes()):
                return any(own(v) for v in self.navdb.airspace_at(
                    state.latitude, state.longitude, state.altitude_ft))
        except Exception:
            log.debug("zone lookup failed at %s", station.ident, exc_info=True)
        where = self._station_position(station)
        return where is not None and state.distance_to(*where) < self.ZONE_NM

    def _on_a_runway(self, ident: str, state: AircraftState) -> bool:
        try:
            return bool(self.navdb.runway_at(ident, state.latitude,
                                             state.longitude))
        except Exception:
            log.debug("runway lookup failed at %s", ident, exc_info=True)
            return False

    # Where each radar position stops being the one working the flight. These
    # are the ordinary shape of a sector rather than anything published: a
    # departure is passed to the centre as it leaves the terminal area, and
    # the centre passes it to approach as it comes into the next one.
    #
    # A departure leaves that area by climbing out of it or by flying out of
    # it, and either will do. Requiring the climb stranded everything that
    # levels off low -- which is most of what flies VFR.
    # How long to leave it before looking again for an enroute frequency
    # there turned out not to be one of. The boundary lookup is a polygon
    # scan and a query, the situation loop runs four times a second, and the
    # ground underneath does not change that fast.
    CENTRE_RETRY_S = 60.0

    HANDOFF_TO_CENTRE_FT = 10000.0
    HANDOFF_TO_CENTRE_NM = 30.0
    HANDOFF_TO_APPROACH_NM = 60.0
    # Below an approach area's ceiling, an arrival this close is handed to
    # approach even outside a small area (after LocalTC: three miles a
    # thousand feet above the field, plus five).
    SMALL_AREA_NM_PER_1000FT = 3.0
    SMALL_AREA_SPARE_NM = 5.0
    HANDOFF_TO_TOWER_NM = 15.0
    TOWER_AT_NM = 6.0
    TOWER_MARGIN_NM = 4.0

    # The phases in which a flight still belongs to the field it left. Enroute
    # is one of them: the phase tracker sets it at ten thousand feet, before
    # anybody has said who to call next.
    _CLIMBING_OUT = (Phase.TAKEOFF, Phase.DEPARTURE, Phase.ENROUTE)

    def _climbing_out_with(self, session: FlightSession,
                           station: Station) -> bool:
        """Whether a radar position is working this flight as a departure.

        ENROUTE is one of the climbing-out phases because the tracker sets
        it at ten thousand feet, before anybody has passed the flight on --
        but it is also what an arrival is in when it checks in with its
        destination's approach a hundred miles out, and counting that as a
        climb-out handed it straight back to the centre it had just left.
        So ENROUTE is a climb-out only on a position that is not the
        destination's, unless the flight is going back where it came from.
        """
        if session.phase is not Phase.ENROUTE:
            return session.phase in self._CLIMBING_OUT
        destination = session.destination
        if destination is None or station.ident != destination.ident:
            return True
        origin = session.departure
        return origin is not None and origin.ident == destination.ident

    def _terminal_area(self, airport):
        """A field's approach area (navdata.approach_areas), when the switch
        is on and the field's coordinates fall in one; else None."""
        if airport is None or not self.brain.realism.approach_areas:
            return None
        return self.navdb.approach_area(airport.ident)

    def _arriving_in_area(self, state: AircraftState, destination,
                          to_run: float) -> str:
        """Why the destination's approach now has the flight, or "".

        Below the area's ceiling, and inside the field's approach area or
        close enough for a small one -- the area alone is not enough, since
        an arrival still at FL300 over it is the centre's. Without an area,
        the old sixty miles.
        """
        if destination is None:
            return ""
        area = self._terminal_area(destination)
        if area is not None:
            if area.contains(state.latitude, state.longitude, state.altitude_ft):
                return f"inside {area.id} below {int(area.ceiling_ft)} ft"
            # A small area and a fast arrival: handed over in time to get
            # down, three miles a thousand feet above the field and room to
            # slow and turn (after LocalTC). Heathrow Director's area is
            # twenty miles across, and without this London Control gave the
            # descent to 3,000 ft and approach took the flight at FL063.
            above = float(state.altitude_ft) - float(destination.elev_ft or 0.0)
            reach = above / 1000.0 * self.SMALL_AREA_NM_PER_1000FT \
                    + self.SMALL_AREA_SPARE_NM
            if state.altitude_ft < area.ceiling_ft and to_run <= reach:
                return (f"below {int(area.ceiling_ft)} ft, {to_run:.0f} nm "
                        f"out, within {reach:.0f} nm of a small area")
            return ""
        if to_run <= self.HANDOFF_TO_APPROACH_NM:
            return f"within {self.HANDOFF_TO_APPROACH_NM:g} nm, no approach area"
        return ""

    def _radar_hands_on(self, state: AircraftState, session: FlightSession,
                        station: Station) -> None:
        """Departure to centre, centre to approach, approach to the tower."""
        if state.on_ground:
            return

        if station.position in ("DEP", "APP") \
                and self._climbing_out_with(session, station):
            if time.monotonic() - self._centre_looked_at < self.CENTRE_RETRY_S:
                return
            home = self.navdb.airport(station.ident)
            out = state.distance_to(home.lat, home.lon) if home else 0.0
            if self.brain.realism.climb_steps and session.ifr:
                # Where departure's airspace ends (atc/climb.py), and only
                # once the flight has checked in: a handoff before the first
                # word is a frequency the pilot never spoke on.
                area = self._terminal_area(session.departure or home)
                why = leaving_departure(
                    state.altitude_agl_ft, out, state.altitude_ft,
                    state.vertical_speed_fpm, session.assigned_altitude_ft,
                    session.cruise_altitude_ft or 0,
                    in_area=(area.covers(state.latitude, state.longitude)
                             if area is not None else None),
                    area_ceiling_ft=getattr(area, "ceiling_ft", 0.0))
                leaving = bool(why) and session.established_with(station)
                if leaving and "handoff:centre" not in session.advisories:
                    decision("climb", event="handoff", station=station.callsign,
                             why=why, here_ft=round(state.altitude_ft),
                             out_nm=round(out, 1),
                             area=getattr(area, "id", None))
            else:
                leaving = (state.altitude_ft >= self.HANDOFF_TO_CENTRE_FT
                           or out >= self.HANDOFF_TO_CENTRE_NM)
            if leaving and session.advise_once("handoff:centre"):
                centre = self.navdb.center_station(
                    state.latitude, state.longitude, state.altitude_ft)
                if centre is None or not centre.mhz \
                        or station_key(centre) == station_key(station):
                    # No enroute frequency on file above this sector, which is
                    # the ordinary case over the United Kingdom and France --
                    # the open data has almost no area control frequencies in
                    # Europe. The flight stays with the radar position it is
                    # already talking to. Releasing it here is what left a
                    # departure out of Manchester with nobody at all: told
                    # "frequency change approved", handed nothing to change
                    # to, and never spoken to again.
                    #
                    # Re-armed rather than spent, because the answer changes
                    # with the ground underneath: the same flight an hour
                    # later, over Germany, does have a centre to call.
                    session.advisories.discard("handoff:centre")
                    self._centre_looked_at = time.monotonic()
                    self._no_enroute_frequency(session, centre)
                    return
                self._pass_to_station(session, station, centre)
            return

        destination = session.destination
        if not state.has_position:
            return
        to_run = (state.distance_to(destination.lat, destination.lon)
                  if destination is not None else float("inf"))

        arriving_why = self._arriving_in_area(state, destination, to_run)
        if station.position == "CTR" and not arriving_why:
            self._centre_hands_on(state, session, station)
            return
        if destination is None:
            return

        if station.position == "CTR" and arriving_why:
            if session.advise_once("handoff:approach"):
                approach = self._approach_at(destination, station)
                decision("handoff", to="approach", station=station.callsign,
                         field=destination.ident, why=arriving_why,
                         to_run_nm=round(to_run, 1),
                         altitude_ft=round(state.altitude_ft),
                         worked_by=("approach" if approach is not None
                                    else "the centre: no approach control"))
                if approach is None:
                    # No approach control at the field: the centre works the
                    # arrival itself -- the descent, the approach clearance
                    # and the handoff to the tower, which ArrivalWatch and
                    # the tower handoff below give any radar position. It
                    # used to say "frequency change approved" here and let
                    # the flight go with nobody to call.
                    return
                self._pass_to_station(session, station, approach)
                return
            # That has already been said. A field that publishes no approach
            # control is worked by the centre the whole way in, so the flight
            # falls through to the tower handoff below rather than being left
            # on a sector frequency with nobody to clear it to land.

        # The centre is in here as well as the terminal positions. Where a
        # field publishes no approach control the flight is worked by the
        # centre all the way in, and leaving it out meant that flight was
        # never passed to the tower at all -- it arrived on a sector
        # frequency with nobody to clear it to land.
        arriving = (station.position in ("APP", "DEP", "CTR")
                    and not session.phase.outbound
                    and self._on_the_way_in(state, session, destination, to_run))
        if arriving and session.advise_once("handoff:tower"):
            from .atc.arrival import CLEARED_APPROACH_KEY

            decision("handoff", to="tower", station=station.callsign,
                     field=destination.ident, to_run_nm=round(to_run, 1),
                     why=("cleared and established on the final"
                          if CLEARED_APPROACH_KEY in session.advisories
                          and self.brain.realism.star_final
                          else "in the final's cone or close in"))
            self._pass_to(session, station, "TWR", at=destination.ident,
                          runway=session.arrival_runway)

    # How often a centre looks ahead. Ten seconds is a mile and a third at
    # cruise speed, against a handoff window two minutes wide.
    CENTRE_CHECK_S = 10.0
    # How long before the boundary a centre hands a flight on: a moment in
    # this window, the same one every time for a given flight and sector, so
    # one controller is early and the next a little later, as people are.
    HANDOFF_LEAD_S = (120.0, 240.0)
    # A sector the flight is across in less than this is coordinated by the
    # controllers either side, not handed into.
    SHORT_SECTOR_S = 240.0
    # How far ahead the centre looks: the window, a short sector, and margin.
    LOOK_AHEAD_MIN = 10.0
    # A centre does not hand a flight back to the one it has just left.
    BACK_AGAIN_S = 600.0
    # Climbing or descending faster than this towards its assigned level,
    # a flight is handed to the sector at that level.
    CHANGING_LEVEL_FPM = 500.0
    # Not switched by then, the handoff is said again, once.
    REOFFER_AFTER_S = 90.0
    # However short the time on a frequency, a flight half a minute from the
    # line is handed on.
    LAST_MOMENT_S = 30.0

    def _centre_hands_on(self, state: AircraftState, session: FlightSession,
                         station: Station) -> None:
        """Pass the flight to the next centre before it reaches the boundary.

        The sector ahead along the track (or the SimBrief route) is
        :meth:`EnRoute.crossings`'s answer; the handoff is given two to four
        minutes before the line, to the next controller in the region's
        words. It used to be given only once the aeroplane was inside the
        next sector, which is late by the whole of a real controller's
        margin, and before that it was measured from the field a frequency
        is filed at -- the circle round a country airstrip that took a
        flight across Australia on one frequency.

        A sector crossed in under four minutes is skipped; the handoff never
        goes to the station already worked, nor back to the one just left,
        nor while a readback is owed. Where the moment was missed -- the
        pilot was mid-exchange, or the data put the line closer than the
        window -- the flight is still handed on once it is inside the next
        sector, so it is never left with nobody. Every decision is a row of
        ``logs radio`` with its reason.
        """
        from .atc.enroute import is_oceanic

        now = time.monotonic()
        if now - self._centre_checked_at < self.CENTRE_CHECK_S:
            return
        self._centre_checked_at = now
        handed = session.handoff_pending
        if handed is not None and station_key(handed) == station_key(station) \
                and session.established_with(station):
            # The flight is with the station it was sent to and has called
            # it: that handoff is done, and the next one may be given.
            session.handoff_pending = None
        if self._reoffer_handoff(session, station, now):
            return
        if session.handoff_pending is not None:
            return
        pending = session.pending
        if pending is not None and pending.readback_required \
                and not pending.read_back:
            return

        enroute = self.navdb.enroute
        lat, lon, alt = state.latitude, state.longitude, state.altitude_ft
        sector = enroute.which(lat, lon, alt)
        # On the way to a level it was given, the flight belongs to the
        # sector at that level, not to each one it passes through: coming
        # down into London it was handed 135.0, 134.9 and 120.525 inside a
        # minute, one stratum after another.
        assigned = float(session.assigned_altitude_ft or 0.0)
        vs = float(state.vertical_speed_fpm or 0.0)
        if assigned and abs(vs) > self.CHANGING_LEVEL_FPM \
                and (assigned - alt) * vs > 0:
            at_level = enroute.which(lat, lon, assigned)
            if at_level is not None:
                if sector is not None and at_level.key != sector.key \
                        and session.advise_once(
                            f"enroute:to_level:{at_level.key}"):
                    decision("enroute", event="to the level",
                             sector=at_level.facility,
                             passing=sector.facility, level_ft=int(assigned))
                sector, alt = at_level, assigned
        self._note_sector(sector)

        # Already inside a sector that is not this station's: late, but
        # handed on rather than left.
        if sector is not None and sector.oceanic:
            if not is_oceanic(station):
                self._go_oceanic(session, station, sector)
                return
        elif sector is None:
            here = self.navdb.center_station(lat, lon, alt)
            if here is not None and here.mhz \
                    and not same_channel(here.mhz, station.mhz, 4):
                self._pass_to_station(session, station, here)
            return

        crossings = enroute.crossings(
            lat, lon, state.heading_true, state.ground_speed_kt, alt,
            self.LOOK_AHEAD_MIN, self._route_points())
        if not sector.oceanic and not sector.answers(station.mhz):
            # Already over the line. Into a sector the flight leaves again
            # within four minutes it is not handed at all, or the late
            # handoff hands it in and straight out again: Maastricht, Reims,
            # Maastricht inside four miles.
            if crossings and crossings[0].minutes * 60.0 < self.SHORT_SECTOR_S:
                if session.advise_once(f"enroute:skipped:{sector.key}"):
                    decision("enroute", event="skipped",
                             sector=sector.facility,
                             reason=f"left again in "
                                    f"{crossings[0].minutes:.1f} min",
                             to=crossings[0].sector.facility)
                target = self._past_short_sectors(session, crossings)
                if target.sector.oceanic:
                    self._go_oceanic(session, station, target.sector)
                else:
                    self._centre_hands_to(session, station, target.sector,
                                          minutes=0.0, late=True)
                return
            self._centre_hands_to(session, station, sector, minutes=0.0,
                                  late=True)
            return
        if not crossings:
            return
        first = crossings[0]
        if session.advise_once(f"enroute:boundary:{first.sector.key}"):
            decision("enroute", event="boundary", sector=first.sector.facility,
                     minutes=round(first.minutes, 1),
                     distance_nm=round(first.distance_nm, 1))
        lead = self._handoff_lead_s(session, first.sector)
        if first.minutes * 60.0 > lead:
            return
        held = time.monotonic() - session.with_since if session.with_since else None
        if (held is not None and held < self.SHORT_SECTOR_S
                and first.minutes * 60.0 > self.LAST_MOMENT_S):
            # Handed in early to a sector not much wider than the window,
            # and out early again, the flight was on a frequency for thirty
            # seconds: Hong Kong Radar on one number, then on another four
            # miles on. A controller who has just taken a flight keeps it
            # four minutes, or until the line is half a minute off.
            if session.advise_once(f"enroute:held:{first.sector.key}"):
                decision("enroute", event="held", station=station.callsign,
                         sector=first.sector.facility,
                         reason=f"on this frequency {held / 60.0:.1f} min")
            return

        nxt = self._past_short_sectors(session, crossings).sector
        if nxt.oceanic:
            if not is_oceanic(station):
                self._go_oceanic(session, station, nxt)
            return
        self._centre_hands_to(session, station, nxt, minutes=first.minutes)

    def _past_short_sectors(self, session: FlightSession, crossings):
        """The first crossing whose sector the flight stays in for four
        minutes or more; those it is across sooner are logged as skipped."""
        target = crossings[0]
        for later in crossings[1:]:
            across = (later.minutes - target.minutes) * 60.0
            if across >= self.SHORT_SECTOR_S:
                break
            if session.advise_once(f"enroute:skipped:{target.sector.key}"):
                decision("enroute", event="skipped",
                         sector=target.sector.facility,
                         reason=f"crossed in {across / 60.0:.1f} min",
                         to=later.sector.facility)
            target = later
        return target

    def _centre_hands_to(self, session: FlightSession, station: Station,
                         sector, minutes: float, late: bool = False) -> None:
        """Say the handoff to ``sector``'s controller, unless it is a rule
        not to: the same station, or the one just left."""
        nxt = sector.station()
        if not nxt.mhz:
            return
        if sector.answers(station.mhz) or station_key(nxt) == station_key(station):
            if session.advise_once(f"enroute:same:{sector.key}"):
                decision("enroute", event="skipped", sector=sector.facility,
                         reason="the station already worked")
            return
        if self._handing_back(nxt):
            if session.advise_once(f"enroute:back:{station_key(nxt)}"):
                decision("enroute", event="skipped", sector=sector.facility,
                         reason="left less than ten minutes ago")
            return
        log.info("centre handoff%s, %.1f min before the line: %s %.3f -> "
                 "%s %.3f", " (late)" if late else "", minutes,
                 station.callsign, station.mhz, nxt.callsign, nxt.mhz)
        state = self.state
        here = (self.navdb.enroute.which(state.latitude, state.longitude,
                                         float(state.altitude_ft or 0.0))
                if state is not None and state.has_position else None)
        decision("enroute", event="offered", station=station.callsign,
                 to=nxt.callsign, frequency=round(nxt.mhz, 3),
                 minutes=round(minutes, 1), late=late,
                 from_sector=here.id if here is not None else None,
                 sector=sector.id,
                 source=sector.frequency_source, invented=sector.invented)
        session.left_at[station_key(station)] = time.monotonic()
        session.oceanic = None
        self._pass_to_station(session, station, nxt)

    def _handing_back(self, station: Station) -> bool:
        """Whether ``station`` is one the flight left under ten minutes ago."""
        session = self.session
        if session is None:
            return False
        left = session.left_at.get(station_key(station))
        return left is not None and time.monotonic() - left < self.BACK_AGAIN_S

    def _handoff_lead_s(self, session: FlightSession, sector) -> float:
        """How long before this boundary this flight is handed on.

        Seeded from the callsign and the sector, so a test flight -- or a
        pilot flying the same route twice -- meets the same controller
        habits, while two boundaries in a row are not handed on alike.
        """
        low, high = self.HANDOFF_LEAD_S
        seed = f"{session.aircraft.callsign.written}:{sector.id}:{sector.facility}"
        fraction = int(hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8], 16)
        return low + (high - low) * fraction / 0xFFFFFFFF

    def _note_sector(self, sector) -> None:
        """A row in ``logs radio`` each time the flight enters a sector."""
        key = sector.key if sector is not None else None
        if key == self._sector_noted:
            return
        self._sector_noted = key
        if sector is not None:
            decision("enroute", event="entered", sector=sector.facility,
                     id=sector.id, source=sector.source,
                     frequency=round(sector.mhz, 3), invented=sector.invented)

    def _reoffer_handoff(self, session: FlightSession, station: Station,
                         now: float) -> bool:
        """Say a centre handoff again when the flight has not switched.

        Ninety seconds after it was given, with the flight still on this
        frequency: "did you get the frequency?", and the handoff once more.
        Once; after that it is the pilot's frequency to find.
        """
        handed = session.handoff_pending
        if handed is None or handed.position != "CTR" or not handed.mhz:
            return False
        if now - session.handoff_offered_at < self.REOFFER_AFTER_S:
            return False
        if not session.advise_once(f"enroute:reoffer:{station_key(handed)}"):
            return False
        spoken = self._language_for(station.ident, self.last_language)
        p = self.brain.for_station(station, spoken)
        session.aircraft.language = getattr(p, "language", "en")
        text = p.handoff_reoffer(session.aircraft,
                                 handed.callsign_in(spoken, short=True),
                                 handed.mhz)
        decision("enroute", event="reoffered", station=station.callsign,
                 to=handed.callsign, frequency=round(handed.mhz, 3))
        self.emit("atc", text, station.callsign)
        session.note("atc", as_written(text))
        self.speak(text, station, language=spoken)
        self._radio_unprompted(text, station, "handoff_reoffer", spoken)
        session.handoff_offered_at = now
        return True

    def _route_points(self) -> list[tuple[float, float]] | None:
        """The loaded route as points, for looking ahead along it."""
        if self.route:
            return self.route.points()
        plan = self.plan
        if plan is None:
            return None
        if self._route_cache is not None and self._route_cache[0] is plan:
            return self._route_cache[1]
        from .integrations.simbrief import route_points

        points = route_points(plan) or None
        self._route_cache = (plan, points)
        return points

    def _say_invented(self, station: Station) -> None:
        """Say once that a frequency was made up, and ask for the real one.

        A sector with no published frequency in any data this program has is
        given one rather than left silent (atc/enroute.py), and a made-up
        number that looks like a real one is a number nobody corrects.
        """
        from .atc.enroute import is_invented

        session = self.session
        if session is None or not is_invented(station):
            return
        if not session.advise_once(f"invented:{station_key(station)}"):
            return
        mhz = f"{station.mhz:.3f}"
        self.emit("info",
                  f"No published frequency is known for {station.callsign}; "
                  f"{mhz} is invented. If you know the real one, please "
                  "report it on GitHub.",
                  key="log.invented_frequency", station=station.callsign,
                  mhz=mhz)

    def _go_oceanic(self, session: FlightSession, station: Station,
                    sector) -> None:
        """The last radar controller sends the flight to the ocean's HF station.

        Over the ocean there is no VHF radar, so the honest transmission is
        "radar service terminated, contact ... Radio", with the HF family
        where the data has one. Nothing a simulator's COM radio can tune, so
        the station is held in the session (``session.oceanic``) and taken
        over once the readback is in (:meth:`_settle_oceanic`).
        """
        oceanic = sector.station()
        spoken = self._language_for(station.ident, self.last_language)
        p = self.brain.for_station(station, spoken)
        session.aircraft.language = getattr(p, "language", "en")
        text = p.oceanic_contact(session.aircraft,
                                 oceanic.callsign_in(spoken, short=True),
                                 sector.hf_khz)
        log.info("oceanic: %s hands %s to %s (HF %s)", station.callsign,
                 session.aircraft.callsign.written, oceanic.callsign,
                 sector.hf_khz or "unknown")
        decision("enroute_oceanic", station=station.callsign,
                 to=oceanic.callsign, hf=list(sector.hf_khz))
        self.emit("atc", text, station.callsign)
        session.note("atc", as_written(text))
        self.speak(text, station, language=spoken)
        self._radio_unprompted(text, station, "oceanic", spoken)
        session.oceanic = oceanic
        session.handoff_pending = oceanic
        session.handoff_offered_at = time.monotonic()
        # Nothing in it can be checked: the HF family is not a number the
        # simulator's radio takes. The first call the HF station hears --
        # the readback or a position report -- is its first contact.
        session.remember(Instruction("handoff", station_key(station), text,
                                     {"station": oceanic.callsign},
                                     readback_required=False))

    def _settle_oceanic(self, session: FlightSession) -> None:
        """Once the oceanic handoff is read back, the flight is with HF.

        There is no frequency to tune, so the readback is the switch. The
        station is established at once: a flight checks in with its first
        position report, and nothing in the ocean volunteers anything
        before it.
        """
        from .atc.enroute import is_oceanic

        oceanic = session.oceanic
        if oceanic is None or is_oceanic(self.station):
            return
        pending = session.pending
        if pending is not None and pending.readback_required \
                and not pending.read_back:
            return
        self.station = oceanic
        session.establish(oceanic)
        log.info("now with %s on HF", oceanic.callsign)

    # Close enough to the field that the tower has it whichever way it is
    # pointing. Inside this an aeroplane flying a visual circuit to the
    # runway, which is never in the cone until short final, is still passed.
    HANDOFF_ANYWAY_NM = 6.0

    def _on_the_way_in(self, state: AircraftState, session: FlightSession,
                       destination, to_run: float) -> bool:
        """Whether an arrival is where the tower takes it on.

        In the final-approach cone, within HANDOFF_TO_TOWER_NM of the
        threshold of the runway it is landing on -- not a radius round the
        field, which handed an aeroplane flying past the far side to a tower
        it was not going to land with. Very close in, anywhere.
        """
        from .atc.arrival import CLEARED_APPROACH_KEY
        from .atc.final import final_for

        final = final_for(self.navdb, destination.ident,
                          session.arrival_runway or "")
        if session.going_around:
            # Sent round: approach sequences it again first. Only a new
            # approach clearance and the final give it back to the tower --
            # "within six miles" handed a go-around straight back, climbing
            # out, twenty-five seconds after it had left.
            if CLEARED_APPROACH_KEY not in session.advisories:
                return False
            if final is None:
                return to_run <= self.HANDOFF_TO_TOWER_NM
            return final.in_cone(state, self.HANDOFF_TO_TOWER_NM, aligned=False)
        if self.brain.realism.vectors and session.ifr \
                and CLEARED_APPROACH_KEY not in session.advisories:
            # Vectored arrivals are cleared on the intercept; one handed to
            # the tower before that -- a downwind passes close to the field
            # -- was sent back to approach to be vectored.
            return False
        if to_run <= self.HANDOFF_ANYWAY_NM:
            return True
        if final is None:
            return to_run <= self.HANDOFF_TO_TOWER_NM
        if CLEARED_APPROACH_KEY in session.advisories \
                and self.brain.realism.star_final:
            # Cleared and established: to the tower about TOWER_AT_NM +
            # TOWER_MARGIN_NM out, lined up and tracking down the final
            # (after LocalTC: 6 + 4 nm).
            return final.in_cone(state, self.TOWER_AT_NM + self.TOWER_MARGIN_NM,
                                 aligned=True)
        return final.in_cone(state, self.HANDOFF_TO_TOWER_NM, aligned=False)

    def _no_enroute_frequency(self, session: FlightSession,
                              centre: Station | None) -> None:
        """Tell the pilot why nobody took the flight on, and how to fix it.

        The sector has a name -- the boundary data knows it is London -- and
        no frequency, because the open dataset has almost no area control
        frequencies outside North America. Staying with approach is the right
        thing to do about that and it looks exactly like a program that has
        stopped working, so it is said once, in the panel, with the command
        that adds the frequency for good.
        """
        sector = (centre.facility if centre is not None else "") or ""
        if not sector:
            # No boundary and no frequency: there is nothing to name and
            # nothing useful to say beyond what the pilot can already see.
            return
        if not session.advise_once("centre:unpublished"):
            return
        how = command_hint("freq")
        self.emit(
            "info",
            f"No enroute frequency is published for this area ({sector}). "
            "Staying with the radar controller you are on. Add one with: "
            f"{how} <ICAO> --add CTR <frequency> --name <name>",
            key="log.no_enroute_frequency", sector=sector, command=how,
        )

    def _welcome_to(self, station: Station) -> str:
        """Where the aeroplane has arrived, as a controller would name it.

        The field's spoken name rather than its ident: "welcome to Prague",
        not "welcome to LKPR". Empty where there is no name to use, which
        leaves the handoff exactly as it was.
        """
        airport = self.navdb.airport(station.ident)
        if airport is None:
            return ""
        return (getattr(airport, "municipality", "")
                or getattr(airport, "spoken", "") or "")

    def _radio_unprompted(self, text: str, station: Station, why: str,
                          language: str, instruction=None) -> None:
        """Put a transmission nobody asked for into the transcript.

        Only answers to the pilot were written, so the approach clearance,
        the handoffs and the readback chases -- the calls a pilot most often
        wants to check after a flight -- were missing from ``logs radio``.
        ``why`` is which watch said it.
        """
        RADIO.write(
            "atc", text=text, language=language, unprompted=True, why=why,
            station=station.callsign, frequency=round(station.mhz, 3),
            instruction=instruction.kind if instruction is not None else "",
            owed=instruction.owed_items if instruction is not None else [],
            values=dict(instruction.values) if instruction is not None else {},
        )

    def _approach_at(self, field, station: Station):
        """The field's approach or departure control, from this program's
        station data (never the approach area's name), or None."""
        for position in ("APP", "DEP"):
            found = self.brain.controller_at(field.ident, position)
            if found is not None and station_key(found) != station_key(station):
                return found
        return None

    def _pass_to(self, session: FlightSession, station: Station,
                 *positions: str, at: str = "", welcome: str = "",
                 runway: str = "") -> None:
        """Say who to call next, or release the frequency if nobody is next.

        ``at`` is the field whose positions to look through, which is the one
        being talked to unless the flight is being handed forward to where it
        is going.

        A field with no radar and no ground has nobody to pass you to, and the
        honest answer there is "frequency change approved" rather than silence
        -- which is what the pilot got, and what looked like a program that had
        stopped working.
        """
        nxt = None
        for position in positions:
            found = self.brain.controller_at(at or station.ident, position,
                                             runway=runway)
            if found is not None and station_key(found) != station_key(station):
                nxt = found
                break
        self._pass_to_station(session, station, nxt, welcome=welcome)

    def _pass_to_station(self, session: FlightSession, station: Station,
                         nxt: Station | None, welcome: str = "") -> None:
        """Say the handoff, in the language the frequency is being worked in."""
        spoken = self._language_for(station.ident, self.last_language)
        p = self.brain.for_station(station, spoken)
        session.aircraft.language = getattr(p, "language", "en")

        if nxt is not None:
            text = p.handoff(session.aircraft,
                             nxt.callsign_in(spoken, short=True), nxt.mhz,
                             farewell=True, welcome=welcome)
            instruction = Instruction("handoff", station_key(station), text,
                                      {"frequency": nxt.mhz})
        else:
            text = p.frequency_change_approved(session.aircraft)
            instruction = Instruction("frequency_change", station_key(station),
                                      text, {}, readback_required=False)

        self.emit("atc", text, station.callsign)
        session.note("atc", as_written(text))
        self.speak(text, station, language=spoken)
        self._radio_unprompted(text, station, "handoff", spoken, instruction)
        session.handoff_pending = nxt
        session.handoff_offered_at = time.monotonic()
        session.remember(instruction)

    def _offer_handoff(self, state: AircraftState, session: FlightSession) -> None:
        """Prompt the pilot when they have flown past the controller they are on.

        Real controllers hand off before you leave their airspace. Here the
        cue is coverage: once the tuned station is out of plausible range, the
        pilot is told who to call next rather than being left talking to
        somebody who cannot hear them.
        """
        station = self.station
        if not self.brain.staffed(station) or not state.has_position:
            return
        if station.position == "CTR":
            # A centre hands on by sector (_centre_hands_on). Its range from
            # the field its frequency is filed at says nothing about where
            # its airspace ends.
            return
        if session.pending is not None or self.stand_down \
                or not self.quiet_enough():
            return
        if time.monotonic() - session.handoff_offered_at < self.config.atc.handoff_grace_s:
            return
        if station.position == "INFO":
            # A VFR flight on an information service is not handed to a
            # radar position. The next information sector, or a free call.
            self._leave_information(session, station, state)
            return

        distance = state.distance_to(
            *self._station_position(station)
        ) if self._station_position(station) else 0.0

        reach = STATION_RANGE_NM.get(station.position, 60.0)
        if distance <= reach * 0.9:
            return

        nxt = self._next_station(state, session, station)
        if nxt is None and session.flight_following and not session.ifr:
            # Out of range of the advisories with nobody to hand them on to:
            # the service ends, in the region's words, rather than going
            # quiet on a flight that thinks it is still being watched.
            if not session.advise_once(f"ff:end:{station_key(station)}"):
                return
            spoken = self._language_for(station.ident, self.last_language)
            p = self.brain.for_station(station, spoken)
            session.flight_following = False
            session.squawk = p.vfr_squawk
            text = p.radar_service_terminated(session.aircraft)
            decision("flight_following", station=station.callsign, said="terminated")
            self.emit("atc", text, station.callsign)
            self.speak(text, station, language=spoken)
            self._radio_unprompted(text, station, "flight_following_end", spoken)
            return
        if nxt is None or station_key(nxt) == station_key(station):
            return

        spoken = self._language_for(station.ident, self.last_language)
        p = self.brain.for_station(station, spoken)
        text = p.handoff(session.aircraft, nxt.callsign_short, nxt.mhz, farewell=True)
        self.emit("atc", text, station.callsign)
        self.speak(text, station, language=spoken)
        self._radio_unprompted(text, station, "handoff_reminder", spoken)
        session.handoff_pending = nxt
        session.handoff_offered_at = time.monotonic()
        session.remember(
            Instruction("handoff", station_key(station), text,
                        {"frequency": nxt.mhz})
        )

    def _leave_information(self, session: FlightSession, station: Station,
                           state: AircraftState) -> None:
        """An en-route information service letting a flight go at the edge
        of its range: to the next information sector where one can hear it,
        else "squawk 7000, frequency change approved".

        The coverage handoff above sends a flight to a centre or an
        approach, which for a VFR flight talking to Langen Information was
        a radar position that had never heard of it. An aerodrome service
        says nothing: the pilot reports leaving it.
        """
        from .atc import afis

        if afis.kind_of(self.navdb, station) != afis.FIS:
            return
        if not session.established_with(station):
            return
        where = self._station_position(station)
        if where is None:
            return
        reach = STATION_RANGE_NM.get("INFO", 120.0)
        if state.distance_to(*where) <= reach * 0.9:
            return
        if not session.advise_once(f"fis:left:{station_key(station)}"):
            return
        spoken = self._language_for(station.ident, self.last_language)
        p = self.brain.for_station(station, spoken)
        nxt = self._next_information(station, state)
        if nxt is not None:
            text = p.handoff(session.aircraft,
                             self.brain.station_name(nxt, p, short=True),
                             nxt.mhz, farewell=True)
            session.handoff_pending = nxt
            values = {"frequency": nxt.mhz}
        else:
            session.squawk = p.vfr_squawk
            text = p.frequency_change_approved(session.aircraft,
                                               squawk=p.vfr_squawk)
            values = {}
        decision("fis", station=station.callsign, said="leaving_range",
                 next=nxt.callsign if nxt is not None else None)
        session.handoff_offered_at = time.monotonic()
        session.remember(Instruction("handoff", station_key(station), text,
                                     values, readback_required=bool(values)))
        self.emit("atc", text, station.callsign)
        self.speak(text, station, language=spoken)
        self._radio_unprompted(text, station, "handoff_reminder", spoken)

    def _next_information(self, current: Station,
                          state: AircraftState) -> Station | None:
        """The nearest other en-route information sector in reach."""
        from .atc import afis

        best = None
        for airport, _distance in self.navdb.nearest_airports(
                state.latitude, state.longitude, limit=25, max_nm=120.0):
            for other in self.navdb.stations(airport.ident):
                if other.position != "INFO" or not other.mhz:
                    continue
                if other.facility.strip().lower() == current.facility.strip().lower():
                    continue
                if afis.kind_of(self.navdb, other) != afis.FIS:
                    continue
                if not self._within_reach(other, state):
                    continue
                distance = state.distance_to(airport.lat, airport.lon)
                if best is None or distance < best[0]:
                    best = (distance, other)
        return best[1] if best else None

    def _station_position(self, station: Station) -> tuple[float, float] | None:
        airport = self.navdb.airport(station.ident)
        return (airport.lat, airport.lon) if airport else None

    def _next_station(self, state, session, current: Station) -> Station | None:
        """Who the flight should be talking to next, and can actually reach.

        Every candidate is range-checked. Without that, a departure out of
        Manchester that had flown past Manchester Approach was told to contact
        Palma de Mallorca Approach -- correct about where the flight was going
        and nine hundred miles wrong about whether anybody there could hear
        it. Saying nothing is the right answer until the destination is in
        range, and this is called again on every pass, so the moment it is,
        the pilot is told.

        In the climb and the cruise only a centre takes the flight on, and
        the destination's approach once it is as close as the centre would
        hand it over. Falling through to whatever was near handed a flight
        at FL350 to an approach underneath it, or to Melbourne Approach from
        a hundred and fifty miles out, because both were in radio range.
        """
        en_route = session.phase in (Phase.DEPARTURE, Phase.ENROUTE)
        if en_route:
            centre = self.navdb.center_station(
                state.latitude, state.longitude, state.altitude_ft
            )
            if centre and centre.mhz and self._within_reach(centre, state):
                return centre
            destination = session.destination
            if destination is None or not self._arriving_in_area(
                    state, destination, state.distance_to(
                        destination.lat, destination.lon)):
                return None
            found = self.brain.controller_at(destination.ident, "APP")
            if found is not None and self._within_reach(found, state):
                return found
            return None
        if session.destination is not None:
            for position in ("APP", "TWR"):
                found = self.brain.controller_at(session.destination.ident,
                                                 position)
                if found is not None and self._within_reach(found, state):
                    return found
        nearby = self.navdb.nearest_airport(
            state.latitude, state.longitude, max_nm=60.0, min_rank=2
        )
        if nearby:
            found = self.brain.controller_at(nearby.ident, "APP")
            if found is not None and self._within_reach(found, state):
                return found
        return None

    def _within_reach(self, station: Station, state: AircraftState) -> bool:
        """Whether the aeroplane could raise this station from where it is.

        The same arithmetic :meth:`~wilcoatc.navdata.db.NavDB
        .candidate_stations` uses to decide who answers on a tuned frequency,
        so the station named in a handoff is one that will actually be there
        when the pilot dials it. A centre with no airport behind it -- the
        name comes from a boundary, not from a field -- is taken on trust:
        a sector is the size of a country and there is nothing to measure to.
        """
        where = self._station_position(station)
        if where is None or not state.has_position:
            return True
        distance = state.distance_to(*where)
        base = STATION_RANGE_NM.get(station.position, 40.0)
        if station.position in ("DEL", "GND"):
            return distance <= base
        los = 1.23 * math.sqrt(max(0.0, state.altitude_ft))
        return distance <= max(base, min(los, base * 4.0))

    # ------------------------------------------------------------------

    def warm(self) -> None:
        """Load models so the first transmission is not delayed."""
        self.emit("info", "Loading speech recognition...",
                  key="log.loading_stt")
        from .audio.stt import SpeechModelMissing

        try:
            self.recognizer.warm()
        except SpeechModelMissing:
            # A fresh install: the first-run setup's Downloads screen fetches
            # it, and the recogniser loads on the first transmission after.
            log.info("speech model not downloaded yet; the setup fetches it")
        # Kokoro is loaded whatever ``preload`` says: it is one model for
        # nearly every controller, and paying for it here rather than on the
        # first clearance is the difference between a two-second wait for the
        # taxi instruction and no wait at all. ``preload`` still decides
        # whether the Piper voices behind it are loaded too, because that is
        # the one that costs a gigabyte.
        self.emit("info", "Loading voices...", key="log.loading_voices")
        if self.synth.registry.hq_missing():
            # Asked for the cloned voices and cannot have them. Said here,
            # once, rather than on every transmission: the controllers will
            # answer in the ordinary voices, which is a working radio in the
            # wrong cast, and the pilot who set the tier up should know.
            self.emit(
                "warn",
                "The cloned voices are not installed, so the controllers "
                "are using the standard voices. Download the cloning model "
                f"with: {command_hint('setup --hq')}",
                key="log.hq_missing", command=command_hint("setup --hq"),
            )
        self.synth.warm(piper=self.config.voice.preload)
        self.emit("info", "Ready.", key="log.ready")
