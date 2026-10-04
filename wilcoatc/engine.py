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
from .atc.intents import Intent, IntentParser
from .atc.landing import LandingMonitor
from .atc.phraseology import Aircraft, Phraseology, Weather, wake_suffix
from .atc.callsigns import every_language
from .atc.detect import language_of_text
from .atc.language import (LANGUAGE_NAMES, SUPPORTED_LANGUAGES, languages_for,
                           profile_for, reply_language)
from .atc.session import FlightSession, Instruction, Phase, station_key
from .atc.traffic import TrafficInjector, UserAircraft
from .integrations.aitraffic import SimTraffic
from .integrations.facilities import SimFacilities
from .integrations import gsx
from .integrations.gsx import GsxLink
from .integrations.simaircraft import SimAircraft
from .integrations.network import OnlineNetwork
from .atc.arrival import ArrivalWatch
from .atc.departure import DepartureWatch
from .atc.watch import SurfaceWatch
from .atc.written import as_written
from .logs import decision, RADIO, timed
from .audio.io import (CabinPlayer, MicrophoneCapture, PushToTalk,
                       JoystickPushToTalk, RadioPlayer)
from .audio.imperfections import from_config as imperfections_for
from .audio.prosody import from_config as prosody_for
from .audio.tone import from_config as tone_for
from .audio.radio_fx import PROFILE_ATIS
from .audio.announcements import FOLDER as ANNOUNCEMENTS, AnnouncementLibrary
from .audio.stt import Recognizer, model_for
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
                            near_the_field, weather_from_sim)

log = logging.getLogger(__name__)


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
    at: float = field(default_factory=time.time)
    key: str = ""
    params: dict = field(default_factory=dict)
    #: What the panel can do about it, when there is something. Only
    #: ``"setup"`` so far, which means "put the download dialog up": a
    #: missing download is the one failure a pilot can fix from the window,
    #: and a line of red text in the transcript does not tell them that.
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
            model_size=model_for(self.pinned_languages or list(SUPPORTED_LANGUAGES),
                                 self.config.speech.model),
            device=self.config.speech.device,
            compute_type=self.config.speech.compute_type,
            beam_size=self.config.speech.beam_size,
            languages=self.languages,
            detection_threshold=self.config.speech.detection_threshold,
        )
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
        )
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
        # When the search for an enroute frequency last came up empty, so a
        # flight over a country that publishes none is not made to look for
        # one on every pass of the situation loop.
        self._centre_looked_at = 0.0
        # When a centre last looked at which sector the flight is in.
        self._centre_checked_at = 0.0
        # The half of the controller that watches rather than listens: what
        # the aeroplane is doing on the ground, and what it is doing on the
        # way in. Both speak first; nothing in either waits to be asked.
        self.watch = SurfaceWatch(self.navdb, self.brain)
        self.arrivals = ArrivalWatch(self.navdb, self.brain)
        self.departures = DepartureWatch(self.navdb, self.brain)
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
        self._approaches_asked: set[str] = set()
        self._ground_ready: dict[str, object] = {}
        self.simtraffic = SimTraffic(
            enabled=self.config.traffic.enabled
            and self.config.traffic.in_simulator,
            # The parked aeroplanes come after the moving ones in what it is
            # given, so this is room for them rather than a share of it.
            limit=self.config.traffic.in_simulator_limit
            + max(0, int(self.config.traffic.parked)),
        )
        # And the aeroplanes somebody else put there, which is the other half
        # of the same question: what is actually on this airport.
        self.simwatch = SimAircraft(
            enabled=self.config.traffic.enabled
            and self.config.traffic.from_simulator,
        )

        self.source: StateSource = ManualSource()
        self.state = AircraftState()
        self.session: FlightSession | None = None
        # The last SimBrief plan loaded, if any.
        self.plan = None
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
        dialect = self.config.atc.dialect
        return Phraseology(
            "faa" if dialect in ("auto", "faa") else "icao",
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
        # A filed plan says which rules it was filed under, and it outranks
        # the setting: a pilot who has loaded a flight plan has said more
        # about this flight than the config file ever did.
        flight.ifr = plan.ifr
        self.plan = plan
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
        self.use_fsltl_liveries()
        # Logged rather than shown. "FSLTL is running and the controller is
        # not seeing it" has four possible causes and none of them is
        # something a pilot can act on from the panel, so it goes where
        # somebody reporting the problem will find it.
        log.info("%s", self.simwatch.describe())
        return self.simtraffic.attach(simconnect) or watching

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

    def _start_ptt(self) -> None:
        mode = self.config.ptt.mode
        if mode == "none":
            return
        try:
            if mode == "joystick":
                self._ptt = JoystickPushToTalk(
                    joystick_index=self.config.ptt.joystick_index,
                    button=self.config.ptt.joystick_button,
                    on_press=self.begin_transmission,
                    on_release=self.end_transmission,
                )
            else:
                self._ptt = PushToTalk(
                    key=self.config.ptt.key,
                    on_press=self.begin_transmission,
                    on_release=self.end_transmission,
                )
            self._ptt.start()
            # The label goes inside a translated sentence, so it is the bare
            # key or button rather than an English phrase around one.
            if mode == "joystick":
                label = (f"joystick {self.config.ptt.joystick_index} "
                         f"button {self.config.ptt.joystick_button}")
                self.emit("info", f"Push to talk: hold {label}.",
                          key="log.ptt_button",
                          stick=str(self.config.ptt.joystick_index),
                          button=str(self.config.ptt.joystick_button))
            else:
                label = f"the {self.config.ptt.key} key"
                self.emit("info", f"Push to talk: hold {label}.",
                          key="log.ptt", key_name=self.config.ptt.key)
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
        sim = SimConnectSource(poll_interval=self.config.sim.poll_interval_s)
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

    def stop(self) -> None:
        self._stop.set()
        self.cabin.stop()
        if self._ptt is not None:
            self._ptt.stop()
        if self._situation_thread is not None:
            self._situation_thread.join(timeout=2.0)
        self.player.stop()
        # Take the injected aeroplanes back out before the connection goes, or
        # they are left sitting in the simulator with nobody flying them.
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

    def begin_session(self) -> FlightSession:
        """Build the flight session from the sim state and the config."""
        flight = self.config.flight
        raw_callsign = flight.callsign or self.state.callsign or "N123AB"

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
            dialect=dialect_for(departure.ident if departure else ""))
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
            ifr=flight.ifr,
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
        return session

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
        if sim_weather is not None and near_the_field(self.state, airport):
            sample = merge_weather(sim_weather, sample,
                                   surface=bool(self.state.on_ground),
                                   wind=at_the_surface(self.state, airport))
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
        key = (airport_ident, language)
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
                                self.config.atc.strict_icao_digits),
            )
            generator.approach_for = (
                lambda runway, field=airport: self.brain.approach_for(
                    None, field, runway))
            # The runway the controllers are using, flight plan included, so
            # the broadcast never names another.
            generator.runway_for = (
                lambda weather, departing, field=airport: (
                    self.brain.departure_runway(field, weather) if departing
                    else self.brain.arrival_runway(field, weather)))
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

    def tune(self, mhz: float) -> Station | None:
        """Tune a frequency and work out who is listening."""
        state = self.state
        lat, lon = (state.latitude, state.longitude) if state.has_position else (0.0, 0.0)
        station = self.navdb.resolve_station(mhz, lat, lon, state.altitude_ft)

        previous = self.station
        same_centre = (station is None or (
            station.position == "CTR"
            and station.facility.lower() == previous.facility.lower())
        ) if previous is not None else False
        if (same_centre and previous.position == "CTR"
                and same_channel(mhz, previous.mhz, 4)
                and station_key(station) != station_key(previous)):
            # A centre's frequency is filed at one field and counts as
            # reachable within so many miles of it; the sector is the size of
            # a country. Looked up again from five hundred miles on -- which
            # every frequency read from the simulator does -- it resolved to
            # nobody, and with nobody on the frequency nothing ever handed
            # the flight on. The centre being worked stays until somebody
            # else answers on the frequency.
            #
            # The same goes for the same centre filed at another field on
            # the same number -- Minnipa and Wudinna are both Melbourne on
            # 125.9. Taking the other row made a different station of it, one
            # the flight had never called, and nothing speaks unprompted to a
            # station that has not been called.
            log.info("COM1 %.3f: still %s (%s), resolved here as %s", mhz,
                     previous.callsign, previous.ident,
                     station.ident if station else "nobody")
            station = previous
        elif station_key(station) != station_key(previous):
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
            # Only explain once per frequency: the situation loop re-checks
            # COM1 several times a second and repeating this would bury
            # everything else on screen.
            if round(mhz, 3) != round(self._reported_empty, 3):
                self._reported_empty = mhz
                hint = self._nearby_hint(lat, lon, mhz)
                self.emit(
                    "info",
                    f"{mhz:.3f} - nobody is listening on this frequency here."
                    + hint,
                    key="log.empty_frequency", mhz=f"{mhz:.3f}", hint=hint,
                )
            # Nobody on frequency does not mean nowhere. The language still
            # follows the aeroplane, so an empty frequency over Germany is
            # listened to in German and English rather than in whatever was
            # worked at the last airport that answered.
            self._last_region_check = 0.0
            self._follow_region(state)
            return None
        self._reported_empty = 0.0

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
            elif station.is_controller:
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

        if round(mhz, 3) != round(self._reported_assumed, 3):
            self._reported_assumed = mhz
            self.emit(
                "info",
                f"{mhz:.3f} is not in the published data for {airport.ident}. "
                f"Answering as {station.callsign}. To keep it: "
                f"wilcoatc freq {airport.ident} --add {position} {mhz:.3f}",
                key="log.unpublished", mhz=f"{mhz:.3f}",
                airport=airport.ident, station=station.callsign,
                position=position,
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

    def _nearby_hint(self, lat: float, lon: float, mhz: float) -> str:
        """Say what *is* available, rather than leaving the pilot stuck.

        A frequency with nobody on it is usually either a typo or a real gap in
        the open data, and in both cases the useful response is to list the
        frequencies that do work at the field you are at.
        """
        if not self.state.has_position:
            return (" No aircraft position available, so no frequency can be "
                    "resolved. Is the simulator connected?")

        airport = self.navdb.home_airport(lat, lon) or self.navdb.nearest_airport(
            lat, lon, max_nm=30.0, min_rank=1
        )
        if airport is None:
            return " No airport within range of your position."

        stations = self.navdb.stations(airport.ident)
        if not stations:
            return f" {airport.ident} publishes no ATC frequencies."

        order = ["ATIS", "DEL", "GND", "TWR", "DEP", "APP", "CTR", "CTAF"]
        seen: dict[str, float] = {}
        for st in sorted(stations, key=lambda s: (s.priority, s.mhz)):
            if st.position in order and st.position not in seen:
                seen[st.position] = st.mhz
        listing = "  ".join(
            f"{position} {seen[position]:.3f}" for position in order if position in seen
        )
        # A near-miss is worth naming: 127.850 against a published 127.750 is
        # far more likely to be a mistuned radio than a missing frequency.
        closest = min(stations, key=lambda s: abs(s.mhz - mhz))
        near = ""
        if abs(closest.mhz - mhz) <= 0.2:
            near = (f" Did you mean {closest.mhz:.3f} for {closest.callsign}?")

        return (
            f"{near} At {airport.ident} ({airport.spoken}): {listing}."
            f" If {mhz:.3f} really is published there, add it to"
            f" data/navdata/overrides.csv as"
            f" \"{airport.ident},TWR,{mhz:.3f},,,add\" (change TWR to the right"
            f" position) and rebuild with"
            f" {command_hint('setup --force')}."
        )

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

            self.emit("pilot", transcript.text)
            if transcript.confidence < self.config.speech.min_confidence:
                log.info("low confidence %.2f: %s", transcript.confidence,
                         transcript.text,
                         extra={"confidence": round(transcript.confidence, 3),
                                "heard": transcript.text})

            self.handle_text(transcript.text, transcript.language)
        except Exception as exc:
            log.exception("transmission handling failed")
            self.emit("error", f"Error handling transmission: {exc}",
                       key="log.transmission_failed", detail=exc)
        finally:
            self._busy.release()

    def handle_text(self, text: str, language: str | None = None) -> Reply | None:
        """Process one pilot transmission that has already been transcribed.

        Exposed separately from the audio path so the whole ATC stack can be
        driven from typed input, which is how the tests exercise it.
        ``language`` is what the pilot spoke; the controller answers in the
        same one when the facility works it.
        """
        if self.session is None:
            self.begin_session()
        session = self.session
        assert session is not None

        self.refresh_state()

        # Somebody in the aeroplane, rather than somebody on the frequency.
        # Before the station is resolved on purpose: a pilot can ask their
        # cabin crew for a coffee at a field with nothing published on the
        # box, and being told that nobody is listening would be wrong twice.
        if self.answer_the_aeroplane(text, language):
            return None

        station = self.station or self._current_station()
        if station is None:
            self.emit("info", "No station is listening on this frequency.",
                      key="log.nobody_listening")
            return None

        # Speech arrives with a language the recogniser decided from the sound.
        # Typed text arrives with none, so it is read off the words -- against
        # the languages this facility actually works, so a French phrase at
        # Frankfurt is not answered in French.
        if not language:
            language = language_of_text(text, languages_for(station.ident))
        heard_in = self._language_for(station.ident, language)
        with timed("understanding"):
            parsed = self.understanding.parse(
                text, heard_in, context=self._situation(session, station)
            )
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

        if self.stand_down:
            # Somebody real is working this frequency. What the pilot said is
            # shown, because they said it, and nothing is transmitted over the
            # person who is going to answer it.
            self.emit("info",
                      f"{self.human.describe()} is working this frequency.",
                      key="log.human_online",
                      mhz=f"{station.mhz:.3f}", station=self.human.callsign,
                      network=self.human.network.upper())
            return None

        weather = self.weather_for(station.ident)
        with timed("controller"):
            reply = self.brain.respond(parsed, session, station, weather,
                                       self.state)

        if reply.instruction is not None:
            session.remember(reply.instruction)

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

    # What one word in another language does not change: these are answered
    # in the frequency's language.
    _NOT_A_CHANGE_OF_LANGUAGE = frozenset({
        Intent.SAY_AGAIN, Intent.ACKNOWLEDGE, Intent.AFFIRMATIVE,
        Intent.NEGATIVE, Intent.STANDBY, Intent.UNKNOWN, Intent.RADIO_CHECK,
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
        try:
            said = interphone.parse(text, self.crew.language,
                                    names=self.crew.names())
        except Exception:
            log.debug("could not read the interphone", exc_info=True)
            return False
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
        if state.com1_active and abs(state.com1_active - self._last_com1) > 0.004:
            self.tune(state.com1_active)
        elif self.station is None:
            # Nothing tuned, so the country under the aeroplane decides what
            # the recogniser listens for. Checked rarely: it only changes when
            # a border does.
            self._follow_region(state)

        # The airport under the aeroplane decides what is on the radio there,
        # and the simulator is the authority on that.
        self.follow_facilities(state)

        # And who is on the network, which decides whether this program is
        # entitled to say anything at all. The poll is in the background; the
        # answer is whatever the last one found.
        if self.online.enabled:
            self.online.poll()
            self._watch_for_humans(state)

        # A liftoff off a landing rollout is classified first, so that the
        # surface watch does not take a bounce for a takeoff.
        self._note_rollout_liftoff(previous, state, session)

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
        self._work_the_arrival(previous, state, session)

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
            # Nobody to talk to, so nothing is said -- but whoever is already
            # flying keeps flying, and stays where the simulator can see them.
            self.traffic.advance(time.monotonic())
            self._keep_traffic_real(time.monotonic())
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
        self.simwatch.enabled = self.config.traffic.from_simulator
        if self.config.traffic.from_simulator:
            self.simwatch.own_callsign = session.aircraft.callsign.written
            try:
                self.traffic.set_observed(self.simwatch.poll(), time.monotonic())
            except Exception:
                log.debug("could not read the simulator's traffic",
                          exc_info=True)
            self._say_observed()

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
        )

        self._follow_ground(station.ident)

        now = time.monotonic()
        # Whatever has fallen due joins the back of the queue. It is not spoken
        # here: the frequency may be busy, and a transmission that cannot be
        # heard has to wait rather than disappear.
        self._traffic_queue.extend(self.traffic.poll(now, station.position))
        self._speak_from_the_queue(station, now)

        # And the same aeroplanes, where they can be seen. This is every
        # flight the injector is running, not only the ones on this frequency:
        # an aeroplane does not vanish from the taxiway because you changed
        # channel.
        # Only the invented ones: the rest are already in the simulator, and
        # creating a second copy of an FSLTL aeroplane a foot from the first is
        # the one way this could make the window worse rather than better.
        self._keep_traffic_real(now)

    def ground_layout(self, ident: str):
        """This airport's stands and taxiways, if the simulator has given
        them; asked for in the background if it has not yet."""
        ident = (ident or "").upper()
        layout = self._ground_ready.get(ident)
        if layout is None:
            self._ask_for_ground(ident)
        return layout

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

    def _ask_for_approaches(self, ident: str) -> None:
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

        threading.Thread(target=read, name=f"WilcoATC-approaches-{ident}",
                         daemon=True).start()

    def _keep_traffic_real(self, now: float) -> None:
        """Have the simulator draw the invented traffic, and keep only what
        it has drawn.

        An aeroplane is asked of the simulator first and joins the frequency
        once it is there; one the simulator cannot make is dropped without a
        word ever having been said about it.
        """
        self.simtraffic.sync(self.traffic.positions(invented_only=True), now)
        confirmed, dropped = self.traffic.confirm(
            self.simtraffic.has, self.simtraffic.failed, now)
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

    def _speak_from_the_queue(self, station: Station, now: float) -> None:
        """Put the next queued transmission on the air, if the air is free.

        Not while a person is working it. The invented traffic is this
        program's, and playing it over a real controller's frequency would put
        aeroplanes on their radio that they cannot see and did not clear.
        """
        if self.stand_down:
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
            if call.position != station.position or self.brain.closed(station):
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
        try:
            reply = self.arrivals.look(
                session, station, state, previous,
                weather_for=self.weather_for, language=spoken,
            )
        except Exception:
            log.debug("the arrival watch raised", exc_info=True)
            return
        if reply is None or not reply.speak:
            return

        self.emit("atc", reply.text, station.callsign)
        session.note("atc", as_written(reply.text))
        self.speak(reply.text, station, language=spoken)
        self._radio_unprompted(reply.text, station, "arrival", spoken,
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
        if session.cleared_option or session.pattern_work:
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
        self.emit("atc", reply.text, station.callsign)
        session.note("atc", as_written(reply.text))
        self.speak(reply.text, station, language=spoken)
        self._radio_unprompted(reply.text, station, "go_around_after_touchdown",
                               spoken, reply.instruction)

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
        return self.human is not None

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
        if session.advise_once("handoff:tower"):
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
    HANDOFF_TO_TOWER_NM = 15.0

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

        if station.position == "CTR" and to_run > self.HANDOFF_TO_APPROACH_NM:
            self._centre_hands_on(state, session, station)
            return
        if destination is None:
            return

        if station.position == "CTR" and to_run <= self.HANDOFF_TO_APPROACH_NM:
            if session.advise_once("handoff:approach"):
                self._pass_to(session, station, "APP", "DEP",
                              at=destination.ident)
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
            self._pass_to(session, station, "TWR", at=destination.ident,
                          runway=session.arrival_runway)

    # How often a centre looks at where the flight now is. Thirty seconds is
    # four miles at cruise speed, and a sector is a hundred.
    CENTRE_CHECK_S = 30.0

    def _centre_hands_on(self, state: AircraftState, session: FlightSession,
                         station: Station) -> None:
        """Pass the flight to the next centre when it is in the next one's sector.

        Which centre covers a position is :meth:`NavDB.center_station`'s
        answer: the boundary the aeroplane is inside names the centre, and
        the frequency is one filed under that name. When the answer for here
        is on another channel from the one being worked, this centre is done
        with the flight. Two rows of the same centre on the same number --
        Minnipa and Wudinna are both 125.9 -- are not a handoff.

        Not measured from the field the frequency is filed at, which is what
        the coverage reminder did: a centre is not a circle round a country
        airstrip, and that circle is how a flight crossed Australia on one
        frequency. Nor behind the reminder setting: no centre leaves a flight
        to find the next one for itself.
        """
        now = time.monotonic()
        if now - self._centre_checked_at < self.CENTRE_CHECK_S:
            return
        self._centre_checked_at = now
        here = self.navdb.center_station(
            state.latitude, state.longitude, state.altitude_ft)
        if here is None or not here.mhz or same_channel(here.mhz, station.mhz, 4):
            return
        log.info("centre handoff at %.2f, %.2f: %s %.3f -> %s %.3f",
                 state.latitude, state.longitude, station.callsign,
                 station.mhz, here.callsign, here.mhz)
        self._pass_to_station(session, station, here)

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
        if to_run <= self.HANDOFF_ANYWAY_NM:
            return True
        if final is None:
            return to_run <= self.HANDOFF_TO_TOWER_NM
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

        distance = state.distance_to(
            *self._station_position(station)
        ) if self._station_position(station) else 0.0

        reach = STATION_RANGE_NM.get(station.position, 60.0)
        if distance <= reach * 0.9:
            return

        nxt = self._next_station(state, session, station)
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
            if destination is None or state.distance_to(
                    destination.lat, destination.lon) > self.HANDOFF_TO_APPROACH_NM:
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
        self.recognizer.warm()
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
