# -*- coding: utf-8 -*-
"""The rest of the aeroplane.

Everything on the radio is a controller. This is everybody else: the first
officer in the other seat, the captain on the address system, and the cabin
crew behind you. None of it goes out over the air, and all of it is off
unless somebody has asked for it.

Why it is not more controller
-----------------------------

The controller is bound by phraseology. What it says is prescribed, it is
checked against a readback, and it is wrong if it deviates. Nothing here is
any of that. The moment the two are written in one place the temptation is to
let the crew say things the controller then has to reason about, and they
never do: the crew read the same aircraft state the controller reads, and
that is the whole of the coupling between them.

What decides when somebody speaks
---------------------------------

The flight itself. The phase machine the controller already keeps, plus the
altitude crossings a phase does not capture -- ten thousand feet, a thousand
above the ground -- and two clocks, for the things that happen after a while
rather than at a moment: the drinks service, and the captain getting round to
the welcome announcement.

Each cue fires once per flight. The most obviously wrong thing a cabin crew
can do is give the safety briefing twice.

What stops somebody saying it anyway
------------------------------------

A cue fires on an event; its lines are spoken over the following seconds. That
gap is where the crew stop describing the aeroplane and start reciting at it --
"V one, rotate" to an aeroplane already five hundred feet up, or a purser
announcing the seatbelt sign off while it is still lit. So a line that makes a
claim about a switch names a condition, and the condition is checked when the
line comes due rather than when it was queued. A callout that has been overtaken
is dropped; a cabin announcement about a switch waits for the switch. The
conditions themselves are in :mod:`wilcoatc.atc.crew_lines`, next to the words
they belong to.

What stops two of them arriving at once
---------------------------------------

Cues are minutes apart in the air and they arrive in threes on the ground. The
doors, the push and the captain's welcome all hang on the parking brake coming
off; the taxi checklist, the cabin manager's welcome and the safety
demonstration all hang on the aeroplane starting to roll. A cue's ``delay``
says how long after the event a real crew would start, and each of them was
laid out from that event alone -- as though whatever was in front of it took
no time to say. A departure's first announcement is forty seconds of words.

So the welcome was dated inside the captain's announcement and the briefing
inside the welcome, and because the queue is one list of lines and the drain
took the earliest due one whoever it belonged to, what came out of the speaker
was the three of them cut into each other a paragraph at a time. Every
announcement was complete and in order; what the pilot heard was neither, on
every flight, before the aeroplane had gone anywhere.

Two things hold it apart now, at either end.
:meth:`~CrewDirector._behind_the_queue` makes the delay a *minimum*, so an
announcement is laid out behind what is still to be said rather than on top of
it. :meth:`~CrewDirector._floor` is the guarantee underneath that: an
announcement that has begun holds the speaker until it has finished, however
far the guesses in the queue have drifted from what the words actually took.

And a departure is more words than a short taxi has room for, so what is still
queued when the wheels leave the ground is dropped rather than read to an
aeroplane in the climb -- the same rule as the callout that missed its moment,
applied to the part of the flight that has been left behind.

Where the sound goes
--------------------

Not through the radio player. The first officer is not on the frequency and
must not queue behind it: a controller calling you in the middle of the
safety demonstration has to arrive when they called, not forty seconds later.
:class:`~wilcoatc.audio.io.CabinPlayer` is a second audio path for exactly
this, and it is also why the intercom and the cabin have their own effect
profiles -- a first officer who arrives with a squelch burst gets filed by
the ear as a second controller.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field, replace

import numpy as np

from ..audio.announcements import (AnnouncementLibrary, Flight, flight_seed,
                                   load as load_recording)
from ..audio.applause import SAMPLE_RATE as APPLAUSE_RATE, applause
from ..audio.boarding import SAMPLE_RATE as BOARDING_RATE, bed as boarding_bed
from ..audio.chime import chime as make_chime, silence
from ..audio import aboard
from ..audio.imperfections import rng_for
from ..audio.radio_fx import PROFILE_CABIN, PROFILE_INTERCOM
from ..audio.voices import (accent_for, gender_of as voice_gender,
                            in_language, language_of as voice_language)
from ..logs import CREW
from ..navdata.db import transition_altitude
from .copilot import confirmation
from . import interphone
from .crew_lines import (ANSWERS, CABIN, CABIN_PA, CAPTAIN, CONDITIONS,
                         CUE_GROUPS, CUE_RECORDINGS, FIRST_OFFICER, GROUND,
                         GROUP_SIDE, INTERCOM, PURSER, RECORDED_ONLY, SCRIPTS,
                         Line)
from .landing import APPLAUSE_FPM, touchdown_rate
from .session import FlightSession, Phase

log = logging.getLogger(__name__)


# Surnames the captain can have. Only ever spoken, never matched against
# anything, and picked from the aircraft registration so the same aeroplane
# keeps the same captain.
CAPTAIN_NAMES: tuple[str, ...] = (
    "Bergman", "Calloway", "Doyle", "Enright", "Fairbairn", "Grantham",
    "Hollis", "Ingram", "Jarrett", "Kendall", "Lindqvist", "Marchetti",
    "Novak", "Okafor", "Pemberton", "Quintero", "Radcliffe", "Sandoval",
    "Thackeray", "Ueda", "Vasquez", "Whitfield", "Yarrow", "Zelinski",
)

# The cabin manager's first name, and the names of the crew they say are
# working with them.
#
# First names, unlike the captain's surname, because that is how a cabin crew
# introduce themselves and it is the whole reason the announcement lands:
# "my name is Cassandra" is a person, "your cabin manager" is a job. Picked
# from the registration like everything else here, so the same aeroplane keeps
# the same crew across a re-fly, and by language, because a German cabin
# announcing a purser called Siobhan is the immersion going the other way.
#
# One pool of each gender per language. Which pool the manager comes from
# follows the cabin voice: a pilot who has asked for a man in the back gets a
# man's name with him, and the written lines are worded so that either works.
FIRST_NAMES: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "f": ("Cassandra", "Eleanor", "Fiona", "Harriet", "Imogen", "Joanna",
              "Katherine", "Louise", "Megan", "Nadia", "Olivia", "Rebecca",
              "Siobhan", "Tessa", "Verity", "Yasmin"),
        "m": ("Adrian", "Callum", "Douglas", "Edward", "Gareth", "Howard",
              "Julian", "Malcolm", "Nathan", "Oliver", "Peter", "Roland",
              "Simon", "Trevor", "Vincent", "Warren"),
    },
    "fr": {
        "f": ("Amelie", "Beatrice", "Camille", "Delphine", "Elodie",
              "Fabienne", "Genevieve", "Helene", "Isabelle", "Juliette",
              "Laurence", "Margaux", "Nathalie", "Ophelie", "Sandrine",
              "Veronique"),
        "m": ("Antoine", "Bertrand", "Cedric", "Damien", "Emile", "Fabrice",
              "Gregoire", "Hugues", "Jerome", "Laurent", "Mathieu", "Olivier",
              "Pascal", "Romain", "Sylvain", "Thierry"),
    },
    "de": {
        "f": ("Annika", "Birgit", "Claudia", "Dorothea", "Elke", "Franziska",
              "Gudrun", "Heike", "Ingrid", "Johanna", "Katrin", "Lena",
              "Marlene", "Nadine", "Sabine", "Theresa"),
        "m": ("Andreas", "Bernd", "Christoph", "Dietmar", "Erik", "Florian",
              "Gerhard", "Helmut", "Jonas", "Klaus", "Lukas", "Matthias",
              "Norbert", "Rainer", "Stefan", "Tobias"),
    },
    "nl": {
        "f": ("Anouk", "Bente", "Charlotte", "Dorien", "Els", "Femke",
              "Griet", "Hanne", "Ilse", "Joke", "Katrien", "Lotte",
              "Marieke", "Nele", "Saskia", "Veerle"),
        "m": ("Arne", "Bram", "Christophe", "Dirk", "Elias", "Frederik",
              "Geert", "Hendrik", "Jeroen", "Koen", "Lars", "Maarten",
              "Niels", "Pieter", "Sander", "Wouter"),
    },
}

# How many other cabin crew the manager names. Two is what a narrowbody
# actually carries behind the front pair and is short enough to say in one
# breath; naming nine on a 777 would be a list, not a welcome.
CREW_NAMED = 2

# How long after levelling off in the cruise the captain gets round to the
# welcome, and how long after that the trolley appears. Both are the sort of
# delay a real flight has and a scripted one never does.
WELCOME_DELAY_S = 90.0
SERVICE_DELAY_S = 420.0
# On the flight deck the galley call comes before the cabin service does.
GALLEY_DELAY_S = 330.0
# How often the first officer says something unprompted in a long cruise,
# and only with the chatter turned up.
CHATTER_EVERY_S = 900.0

# The altitude bands the callouts hang on. Above the transition the crew
# would be calling flight levels, but ten thousand is where the signs and the
# lights change and that is what the cabin hears.
TEN_THOUSAND_FT = 10_000.0
# Above the ground, on the way down.
ONE_THOUSAND_AGL = 1000.0
FIVE_HUNDRED_AGL = 500.0
MINIMUMS_AGL = 250.0
# A rotation is called off airspeed, not off the ground.
EIGHTY_KT = 80.0

# The phases that mean the aeroplane has not gone anywhere yet. Boarding
# happens in all of them: a clearance is something the flight deck does while
# the cabin fills up, not something that ends the boarding.
AT_THE_STAND = (Phase.PREFLIGHT, Phase.CLEARED, Phase.PUSHBACK)


def _v_speed(type_code: str) -> float:
    """A plausible V1 for the aeroplane, in knots.

    Not a performance calculation and not pretending to be one. The callout
    exists so that the takeoff roll has the shape a takeoff roll has, and a
    light single rotating at a hundred and thirty knots would be a worse lie
    than a rounded number that is roughly right for the class of aeroplane.
    """
    code = (type_code or "").upper()
    if code.startswith(("A38", "B74", "B77", "B78", "A35", "A34", "MD11")):
        return 150.0
    if code.startswith(("A3", "B7", "B3", "E19", "E17", "CRJ", "MD8")):
        return 135.0
    if code.startswith(("AT4", "AT7", "DH8", "SF3", "C700", "C25", "GL", "SF50")):
        return 105.0
    if code.startswith(("TBM", "PC12", "B350", "BE", "C208", "PA4")):
        return 85.0
    return 60.0


def _about_how_long(text: str) -> float:
    """Roughly how long a line will take to say, in seconds.

    A guess, and only ever used before anything has been rendered: to space a
    cue's own lines when it is queued, and to work out where the end of the
    queue is so that the next cue goes behind it rather than on top of it.
    Roughly three words a second plus a second of margin, deliberately
    generous -- :meth:`CrewDirector._close_up` replaces the guess with the
    real length the moment the line has actually been played.
    """
    return 1.2 + len(text.split()) / 2.6


@dataclass
class _Pending:
    """Something waiting for its moment: a line, or a recording."""

    at: float
    line: Line
    cue: str
    # A file the pilot supplied, played instead of synthesising the line.
    recording: object = None
    # When to give up on a line whose condition has not come true. Equal to
    # ``at`` for a line with no patience, which is a line that is dropped the
    # moment it stops being worth saying.
    expires: float = 0.0
    # The line, already rendered, from before it was due. An announcement is
    # several lines long and the synthesiser is not instant, so a line
    # rendered only once the line before it had finished playing put the
    # whole of that render into the gap between the two. Not compared: a
    # transmission carries a numpy array, and two of those do not answer
    # ``==`` with a yes or a no.
    voice: object = field(default=None, compare=False)


@dataclass
class CrewState:
    """What the crew have already done on this flight."""

    said: set[str] = field(default_factory=set)
    queue: list[_Pending] = field(default_factory=list)
    levelled_at: float = 0.0
    last_chatter: float = 0.0
    # The highest the aeroplane has been, so a descent is a descent rather
    # than a level-off after a climb.
    highest_ft: float = 0.0
    # Which alternative of each cue was used, so a re-armed cue does not
    # repeat the same wording.
    variants: dict[str, int] = field(default_factory=dict)
    # Which switches this aeroplane has been seen to work. A light single
    # reports a seatbelt sign it has not got as permanently off, and a cabin
    # that believed that would never make an announcement about it again --
    # so a switch is only believed once it has been seen used.
    switches: set[str] = field(default_factory=set)
    # Whether this aeroplane has been seen moving under somebody else's
    # power, and since when it has been stationary again. Together they are
    # the end of the push, which is a moment no phase and no variable
    # actually reports: GSX says so when GSX is there, and when it is not,
    # an aeroplane that rolled off the stand and has stopped again has been
    # pushed and is now waiting to taxi.
    pushing: bool = False
    still_since: float = 0.0
    # Whether the brakes have come off at the stand. Its own flag rather
    # than "did the doors-closed cue fire", because that cue belongs to the
    # briefings switch -- and a pilot who turned the briefings off would
    # otherwise have moved the cabin manager's welcome to the taxi without
    # asking for anything of the sort.
    off_stand: bool = False
    # The names the cabin introduced itself by, kept so that a pilot can
    # answer the purser by name and be understood.
    named: tuple[str, ...] = ()
    # When the boarding music is due to start, and whether it is running. The
    # bed is not a queued line -- it has no length and nothing waits for it --
    # so it keeps its own clock rather than a place in the queue.
    music_at: float = 0.0
    music_on: bool = False
    # Which announcement is being said. The queue is a list of lines and the
    # drain used to take the earliest due one of them whoever it belonged to,
    # so two announcements whose schedules overlapped came out of the speaker
    # a paragraph each: the cabin manager's welcome and the safety briefing
    # spliced into one another. This is what holds the floor until the
    # announcement that has begun has finished.
    saying: str = ""
    # The applause after a gentle touchdown: when it is due, and when it
    # stops being worth doing. Like the music it is not a queued line --
    # nobody is speaking -- so it keeps its own clock.
    applause_at: float = 0.0
    applause_until: float = 0.0


class CrewDirector:
    """The first officer and the cabin, following the flight.

    Reads the aircraft state and the session; writes only audio. Nothing here
    can change what the controller does, which is deliberate -- the crew are
    scenery, and scenery that can alter the clearance is not scenery.
    """

    def __init__(self, config, synth, player, emit=None, weather_for=None,
                 here=None, announcements=None, radio_busy=None, ground=None):
        self.config = config
        self.synth = synth
        self.player = player
        # Whether the frequency is in use. The cabin has its own audio path so
        # that a controller never queues behind a safety demonstration, and
        # the price of that is two people talking at once -- which is what a
        # pilot actually hears, and it is worse than either of them waiting.
        # So the path stays separate and the cabin yields: the flight deck and
        # the cabin hold their tongue while the radio is live, and the radio
        # never has to wait for them.
        self.radio_busy = radio_busy or (lambda: False)
        self.emit = emit or (lambda *a, **k: None)
        self.weather_for = weather_for
        # The pilot's own recordings, if they have put any anywhere.
        self.announcements = (announcements if announcements is not None
                              else AnnouncementLibrary())
        # The airport the aeroplane is at, when anything can say. Used only
        # to fill in a destination nobody filed.
        self.here = here
        # What the ground handling is doing, if anything is reading it. Read
        # and never written: the cabin does not call tugs, it only wants to
        # know when the one outside has finished, because that is when the
        # manager picks up the handset.
        self.ground = ground
        self.state = CrewState()
        self._lock = threading.Lock()
        # A stable per-flight number, so the captain, the casting and the
        # choice of wording are the same on a re-fly of the same aeroplane.
        self._seed = 0
        self._callsign = ""
        self._language = "en"
        # What the flight was begun in, kept so that a settings change in
        # the middle of it can work the crew's language out again.
        self._language_heard = "en"
        # Pinned voices already reported as unable to speak the crew's
        # language, so the log says it once rather than on every line.
        self._unspoken: set[str] = set()
        # Which voice each seat was actually cast in, in the crew's own
        # language, so the English repeat can be the same person when the
        # engine has one who speaks both.
        self._cast: dict[str, str] = {}
        self._type = ""
        # When the flight was paused, if it is. Everything on the clock is
        # pushed along by however long that lasts.
        self._held_at: float | None = None
        # The last aircraft state seen, which is what a queued line is checked
        # against when it comes due rather than when it was queued.
        self._now: object = None
        # And the flight it belongs to, kept for the same reason: an answer
        # to something the pilot just said is built outside the situation
        # loop and has no state handed to it.
        self._session: object = None
        # Since when the queue has been held, and when that was last written
        # down. A cabin that cannot get a word in is the failure with no error
        # in it, so it is timed rather than left to be inferred.
        self._blocked_since = 0.0
        self._blocked_said = 0.0
        # Said once per session: a synthesiser that cannot render a crew line
        # will not render the next one either.
        self._render_reported = False
        # Which (cue, reason) pairs have already been written down, so a
        # refusal that is true on every pass of the situation loop is one row
        # rather than four a second.
        self._noted: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.config.immersion.enabled)

    @property
    def language(self) -> str:
        """Which language the crew are speaking on this flight.

        Read from outside as well: what the pilot says to the cabin is
        matched against the language the cabin is answering in, not the one
        the controller works.
        """
        return self._language

    def begin(self, session: FlightSession, language: str = "en") -> None:
        """A new flight: forget everything the last crew did."""
        with self._lock:
            self.state = CrewState()
            self._noted.clear()
        self._session = session
        self._callsign = session.aircraft.callsign.written if session else ""
        self._seed = _stable_number(self._callsign or "wilcoatc")
        self._language_heard = language
        self._unspoken.clear()
        self._cast.clear()
        self._language = self._spoken_language(language)
        self._held_at = None
        self._now = None
        self._blocked_since = 0.0
        self._type = session.aircraft.type_code if session else ""
        CREW.write("flight", callsign=self._callsign, type=self._type,
                   language=self._language,
                   copilot=self._voice_for(FIRST_OFFICER),
                   cabin=self._voice_for(PURSER),
                   settings={
                       name: getattr(self.config.immersion, name)
                       for name in ("enabled", "copilot", "cabin", "callouts",
                                    "checklists", "briefings", "announcements",
                                    "service", "chimes", "boarding_music",
                                    "recordings", "verbosity", "volume")
                   })

    def _spoken_language(self, language: str) -> str:
        """Which language the crew speak on this flight.

        Three answers, in order, and only ever one there are lines for.

        *A pinned voice.* Picking a German first officer is a pilot saying
        what they want to hear, and answering them in English is the setting
        doing nothing at all -- with a text-driven engine like Supertonic it
        is doing *literally* nothing, because the German and the English
        catalogue entries are the same ten speakers and differ only in the
        language they are asked to read.

        *The operator.* A crew belong to the airline, not to the ground
        underneath them, and they do not change at a border: a Swiss crew
        flying Zurich to Prague speaks German for the whole flight, including
        the arrival announcement. That is the opposite of the rule the
        *controller* follows -- see :func:`wilcoatc.atc.language.reply_language`,
        which does hand you to an English-speaking Prague -- and it is the same
        rule the other aeroplanes on the frequency already use, in
        :data:`wilcoatc.atc.operators.OPERATORS`.

        *The field the flight starts at.* For a private registration, which
        has no operator to belong to. This was the whole of the rule, and it
        is what made a Czech Airlines departure from Zurich come with a German
        cabin crew.

        Naming a Spanish voice does not invent a Spanish cabin: there are no
        Spanish lines, so the crew keep their own language and that voice
        speaks it, which is a foreign-sounding first officer rather than a
        silent one.

        Two things go ahead of all three. ``immersion.language``, when it is
        set, is the pilot saying which language outright, and it used to be
        read by nothing at all. And a pinned voice only decides the language
        if the *other* pinned voice can speak it too. A French first officer
        and a British purser used to make a French crew, and the purser --
        an English model with no French in it -- was then quietly dropped for
        an automatic French cast, so changing the cabin voice changed
        nothing. A Supertonic speaker is the same person in every language
        and bends to the other pin; a Kokoro or Piper voice is one language
        and cannot, so it is the one that decides.
        """
        settings = self.config.immersion
        chosen = (settings.language or "auto").strip().lower()[:2]
        if chosen in SCRIPTS:
            return chosen
        pins = [key for key in (settings.copilot_voice, settings.cabin_voice)
                if key and key != "auto" and voice_language(key)]
        for key in pins:
            spoken = voice_language(key)
            if spoken in SCRIPTS and all(in_language(other, spoken)
                                         for other in pins):
                return spoken
        order = self._cabin_order(say_skipped=True)
        if order:
            return order[0]
        return language if language in SCRIPTS else "en"

    def _cabin_order(self, say_skipped: bool = False) -> tuple[str, ...]:
        """The airline's announcement languages this crew can actually read.

        English last; () for something that is not an airline. A language
        with no lines, or that no installed engine can speak, is left out --
        never replaced, because the replacement would be English and English
        is already at the end. ``say_skipped`` logs what was left out, once
        when the crew are chosen rather than on every announcement.
        """
        from .operators import cabin_languages

        readable, skipped = [], []
        for code in cabin_languages(_airline_code(self._callsign)):
            why = self._unreadable(code)
            if why:
                skipped.append(f"{code} ({why})")
            else:
                readable.append(code)
        if skipped and say_skipped:
            log.info("crew: %s skips %s; announcing in %s", self._callsign,
                     ", ".join(skipped), ", ".join(readable) or "nothing")
        return tuple(readable)

    def _unreadable(self, code: str) -> str:
        """Why the cabin cannot announce in this language, or ""."""
        if code not in SCRIPTS:
            return "no crew lines"
        speaks = getattr(self.synth, "speaks", None)
        if callable(speaks):
            try:
                if speaks(code) is False:
                    return "no voice speaks it"
            except Exception:                            # pragma: no cover
                log.debug("could not ask about %s", code, exc_info=True)
        return ""

    def voices_changed(self) -> None:
        """A crew voice, sex or language was changed in the settings.

        Heard on the next line rather than at the next flight. What is
        already rendered is thrown away, since it was rendered in the old
        voice, and if the change moves the crew into another language, what
        is still queued goes too: it was written in the old one, and French
        read by the English voice just picked is the accent this was fixing.
        """
        if self._session is None:
            return
        language = self._spoken_language(self._language_heard)
        with self._lock:
            if language != self._language:
                self.state.queue = [p for p in self.state.queue
                                    if p.recording is not None]
                self.state.named = ()
            for pending in self.state.queue:
                pending.voice = None
            self._cast.clear()
        was, self._language = self._language, language
        CREW.write("voices", language=language, was=was,
                   copilot=self._voice_for(FIRST_OFFICER),
                   cabin=self._voice_for(PURSER))

    def stop(self) -> None:
        """Silence the cabin, now. Used when the switch is turned off."""
        with self._lock:
            self.state.queue.clear()
            self.state.applause_at = self.state.applause_until = 0.0
        self.music(False)
        try:
            self.player.clear()
        except Exception:
            log.debug("could not clear the cabin player", exc_info=True)

    def hold(self) -> None:
        """Stop talking, but do not forget what was still to be said.

        For a simulator that has been paused or a pilot who has gone into the
        menus. Unlike :meth:`stop` the queue survives, because the flight
        has not ended -- it is waiting. What is cut is the line in the middle
        of being spoken, since finishing a cabin announcement into a menu
        screen is exactly the thing being fixed.

        The clock is pushed along with the pause. Without that, a queue held
        for ten minutes comes back due all at once and the cabin says six
        things in a row on the first tick after the menu closes.
        """
        now = time.monotonic()
        with self._lock:
            if self._held_at is None:
                self._held_at = now
        # The music goes too. A paused simulator is a paused aeroplane, and a
        # bed looping over a menu screen is the same complaint as a purser
        # doing it -- except that this one would run for as long as the pilot
        # was away. It comes back with the rest of the queue on the resume.
        self._bed(False)
        try:
            self.player.clear()
        except Exception:
            log.debug("could not clear the cabin player", exc_info=True)

    def _resume(self) -> None:
        """Give back the time that passed while nothing was happening."""
        with self._lock:
            if self._held_at is None:
                return
            held, self._held_at = self._held_at, None
            waited = max(0.0, time.monotonic() - held)
            for pending in self.state.queue:
                pending.at += waited
            if self.state.levelled_at:
                self.state.levelled_at += waited
            if self.state.last_chatter:
                self.state.last_chatter += waited
            # The push, likewise. A pause while the tug is attached would
            # otherwise come back as an aeroplane that has been standing
            # still for ten minutes, and the cabin would welcome everybody
            # aboard in the middle of the push.
            if self.state.still_since:
                self.state.still_since += waited
            # Only if it had not already begun. Music that was playing when
            # the pause started is due again the moment the pause ends;
            # pushing its time along would leave the cabin silent for as long
            # as the pilot had been in the menus.
            if self.state.music_at > held:
                self.state.music_at += waited

    # ------------------------------------------------------------------
    # what is switched on
    # ------------------------------------------------------------------

    def wants(self, cue: str) -> bool:
        """Whether this cue is switched on at all."""
        return not self.refused(cue)

    def refused(self, cue: str) -> str:
        """Which setting stops this cue, by name, or "" if nothing does.

        The name rather than a yes or no, because "the first officer said
        nothing" has eleven possible answers here and ten of them are a switch
        the pilot could turn on. Answering with the switch is the difference
        between a bug report and a setting.
        """
        settings = self.config.immersion
        if not settings.enabled:
            return "immersion.enabled"
        group = CUE_GROUPS.get(cue, "")
        if not group:
            return "unknown cue"
        # The one cue with a switch of its own. It is in the announcements
        # group because that is where the moment belongs, but it is not
        # somebody speaking, and a pilot who wants the purser and not the
        # music has to be able to say so without losing the purser.
        if cue == "boarding_music" and not settings.boarding_music:
            return "immersion.boarding_music"
        side = GROUP_SIDE.get(group, "copilot")
        if side == "copilot" and not settings.copilot:
            return "immersion.copilot"
        if side == "cabin" and not settings.cabin:
            return "immersion.cabin"
        for name in ("callouts", "checklists", "briefings", "announcements",
                     "service"):
            if group == name and not getattr(settings, name):
                return f"immersion.{name}"
        if group == "chatter" and settings.verbosity != "chatty":
            return "immersion.verbosity is not chatty"
        if settings.verbosity == "quiet" and group not in ("callouts",):
            return "immersion.verbosity is quiet"
        return ""

    # ------------------------------------------------------------------
    # the flight, watched
    # ------------------------------------------------------------------

    def follow(self, previous, state, session: FlightSession | None) -> None:
        """One pass over the aircraft state. Called from the situation loop."""
        if session is None:
            return
        self._session = session
        if not self.enabled:
            # Anything already queued when the switch went off is dropped
            # rather than played out over the next four minutes, and the
            # music with it.
            if self.state.queue or self.state.music_on:
                self.stop()
            return

        if state.paused:
            # Nothing is happening in the aeroplane, so nobody in it has
            # anything to say about it.
            self.hold()
            return
        self._resume()

        self._watch_switches(state)
        self._drain()
        self._run_music()
        self._run_applause(state)
        if previous is None:
            return

        self.state.highest_ft = max(self.state.highest_ft, state.altitude_ft)

        self._watch_ground(previous, state, session)
        self._watch_push(previous, state, session)
        self._watch_takeoff(previous, state, session)
        self._watch_climb(previous, state, session)
        self._watch_cruise(state, session)
        self._watch_descent(previous, state, session)
        self._watch_landing(previous, state, session)

    def _watch_switches(self, state) -> None:
        """Keep the aeroplane itself, and note which switches it really has.

        Only ever adds. A sign that was on during the taxi and is off in the
        cruise has still been seen, and it is the *seen* that says this
        aeroplane models one -- which is what the cabin needs to know before
        it will believe an off.
        """
        self._now = state
        if getattr(state, "seatbelt_sign", False):
            self.state.switches.add("seatbelt")
        if getattr(state, "beacon_light", False):
            self.state.switches.add("beacon")

    # -- on the ground, before it moves ---------------------------------

    def _watch_ground(self, previous, state, session) -> None:
        phase = session.phase
        # Any of the phases that mean "still on the stand", not only the
        # first. Taking an IFR clearance moves the flight to CLEARED, and a
        # pilot who asks for their clearance early -- which is most of them --
        # was out of PREFLIGHT before anybody had finished boarding, and lost
        # the welcome and the music along with it.
        if phase in AT_THE_STAND and state.engines_running:
            self._fire("boarding", session, state)
            # Under the welcome, and then the call that everybody is aboard.
            # A pilot's own recording is played the way any other is; failing
            # one, the music is a bed rather than a queue item, because it
            # runs until the aeroplane moves rather than for its own length.
            self._fire("boarding_music", session, state, delay=30.0)
            self._fire("boarding_complete", session, state, delay=150.0)
        if previous.parking_brake and not state.parking_brake and state.on_ground:
            self.state.off_stand = True
            # The brakes are off, so the aeroplane is going somewhere and
            # boarding is over. The music stops here rather than at the end
            # of its loop: the whole point of it was the wait, and the wait
            # is finished.
            self.music(False)
            self._fire("doors_closed", session, state)
            self._fire("pushback", session, state, delay=8.0)
            # The captain, once the push is actually under way. Late enough
            # that the first officer has called the brakes released and the
            # purser has armed the doors, because a captain who welcomes you
            # aboard before the crew have closed up is a captain talking over
            # his own cabin.
            self._fire("captain_welcome", session, state, delay=26.0)
        if phase in (Phase.TAXI_OUT, Phase.PUSHBACK) and state.ground_speed_kt > 5:
            self._fire("taxi", session, state)
            # The cabin manager's welcome, if the push did not already
            # produce it. Most flights are pushed and it belongs to the end
            # of that; a flight that taxis off its own stand is not a flight
            # with no cabin, and this is when a real crew would make it.
            self._fire("cabin_welcome", session, state, delay=4.0)
            # The demonstration runs while the aeroplane taxis, which is when
            # it happens and is also the only part of a flight with four
            # uninterrupted minutes in it.
            self._fire("pre_safety_briefing", session, state, delay=18.0)
            self._fire("safety_briefing", session, state, delay=25.0)
        if phase in (Phase.HOLDING_SHORT, Phase.LINED_UP):
            self._fire("cabin_secure", session, state)
            self._fire("cabin_secure_reply", session, state, delay=12.0)
            # Lights down for a night departure, which is a thing a recording
            # says and a written template cannot.
            if _after_dark():
                self._fire("cabin_dim", session, state, delay=6.0)
        if session.cleared_takeoff and state.on_ground:
            self._fire("before_takeoff", session, state)

    # How long the aeroplane has to have been stationary before a push is
    # over. A tug stops and starts -- for a turn, for a marshaller, for the
    # engine start -- and a welcome announcement made into one of those
    # pauses is made over the top of the rest of the push.
    PUSH_SETTLED_S = 6.0
    # Above this it is moving. Below it, the number is drift and rounding.
    ROLLING_KT = 0.6

    def _watch_push(self, previous, state, session) -> None:
        """The cabin manager's welcome, once the aeroplane is off the stand.

        The end of a push is a moment nothing reports. The phase machine does
        not have one -- PUSHBACK runs on into TAXI_OUT when the aeroplane
        starts moving forwards, which is after this -- and the simulator has
        no variable that says a tug has been disconnected.

        So it is watched rather than asked for, two ways. GSX knows, when GSX
        is there and something can read it. Failing that, an aeroplane whose
        brakes came off at the stand, which then rolled, and which has been
        still for a few seconds since, has been pushed and is now sitting
        waiting to taxi -- and that is exactly when the announcement is made.
        """
        if not state.on_ground:
            return
        # Nothing before the brakes have come off at the stand: an aeroplane
        # shunted around the apron before anybody boarded is not a departure.
        if not self.state.off_stand:
            return

        ground = self._ground()
        if ground is not None and getattr(ground, "being_pushed", False):
            self.state.pushing = True
        if abs(state.ground_speed_kt) > self.ROLLING_KT:
            self.state.pushing = True
            self.state.still_since = 0.0
            return
        if not self.state.pushing:
            return

        now = time.monotonic()
        if not self.state.still_since:
            self.state.still_since = now
        settled = (now - self.state.still_since) >= self.PUSH_SETTLED_S
        if ground is not None and getattr(ground, "pushback_done", False):
            settled = True
        if settled:
            # A breath after the tug has gone, not the instant it stops.
            self._fire("cabin_welcome", session, state, delay=6.0)

    def _ground(self):
        """What the ground handling is doing, or None if nobody is reading it.

        Wrapped because the cabin must never be the thing that raises: the
        source is somebody else's integration and the worst it may cost here
        is one announcement arriving off a clock instead of off a tug.
        """
        if self.ground is None:
            return None
        try:
            return self.ground()
        except Exception:
            log.debug("could not read the ground handling", exc_info=True)
            return None

    # -- the roll and the climb -----------------------------------------

    # What the crew say on the ground and nowhere else.
    #
    # A departure is more words than a short taxi has room for: the
    # demonstration alone is six paragraphs, and at a field where the holding
    # point is two minutes from the stand the cabin can still be on the
    # lifejackets when the wheels leave the ground. Said in the right order
    # and whole, it is then a purser pointing out the floor-level lighting at
    # three thousand feet -- which is the same complaint arriving by a
    # different road.
    #
    # So what has not been said by the time the aeroplane rotates has been
    # overtaken by the flight, exactly as a callout is, and it goes the same
    # way. Only what is still queued: a line already on the speaker is played
    # out rather than cut off, because a cabin announcement that stops
    # mid-sentence is a fault and a cabin announcement that stops between
    # paragraphs is a crew who ran out of taxi.
    AT_THE_STAND_ONLY = frozenset({
        "boarding", "boarding_complete", "boarding_music", "doors_closed",
        "pushback", "captain_welcome", "cabin_welcome", "pre_safety_briefing",
        "safety_briefing", "cabin_dim", "taxi", "cabin_secure",
        "cabin_secure_reply", "before_takeoff",
    })

    def _watch_takeoff(self, previous, state, session) -> None:
        if not state.on_ground and previous.on_ground:
            self._abandon(self.AT_THE_STAND_ONLY,
                          "the aeroplane left the ground")
            self._fire("positive_rate", session, state, delay=3.0)
            self._fire("after_takeoff", session, state, delay=22.0)
            return
        if not state.on_ground:
            return
        speed = state.indicated_airspeed_kt or state.ground_speed_kt
        if speed >= EIGHTY_KT and previous.ground_speed_kt < EIGHTY_KT:
            self._fire("eighty", session, state)
        rotate = _v_speed(state.type_code or session.aircraft.type_code)
        if speed >= rotate:
            self._fire("v_one", session, state)

    def _watch_climb(self, previous, state, session) -> None:
        if state.on_ground:
            return
        if (state.altitude_ft >= TEN_THOUSAND_FT
                and previous.altitude_ft < TEN_THOUSAND_FT
                and state.vertical_speed_fpm > -200):
            self._fire("ten_thousand_up", session, state)

    # -- the cruise ------------------------------------------------------

    def _watch_cruise(self, state, session) -> None:
        if state.on_ground or session.phase is not Phase.ENROUTE:
            self.state.levelled_at = self.state.levelled_at or 0.0
            return
        level = abs(state.vertical_speed_fpm) < 300
        if not level:
            return
        now = time.monotonic()
        if not self.state.levelled_at:
            self.state.levelled_at = now
            return
        since = now - self.state.levelled_at
        if since >= WELCOME_DELAY_S:
            self._fire("welcome", session, state)
        if since >= GALLEY_DELAY_S:
            self._fire("service_cockpit", session, state)
        if since >= SERVICE_DELAY_S:
            self._fire("service", session, state)
        if (since >= CHATTER_EVERY_S
                and now - self.state.last_chatter >= CHATTER_EVERY_S):
            self.state.last_chatter = now
            self._fire("cruise_chat", session, state, again=True)

    # -- coming down -----------------------------------------------------

    # How far the aeroplane has to be going down before anything is treated
    # as a descent. A level cruise wanders by a couple of hundred feet a
    # minute and none of that is the top of descent.
    DESCENDING_FPM = -500.0

    @staticmethod
    def _height(state) -> float:
        """Height above the ground, or the best stand-in there is for it.

        Not every aircraft in every simulator reports a height above ground,
        and the ones that do not report zero -- which reads as being on the
        runway. Every callout below hangs off this number, so believing a
        zero at thirty-five thousand feet is how a cruise ends up being told
        to take its seats for landing.
        """
        if state.altitude_agl_ft > 0:
            return state.altitude_agl_ft
        return max(0.0, state.altitude_ft)

    def _watch_descent(self, previous, state, session) -> None:
        if state.on_ground:
            return
        descending = state.vertical_speed_fpm < self.DESCENDING_FPM
        high = self.state.highest_ft > TEN_THOUSAND_FT
        height = self._height(state)
        was = self._height(previous)

        if descending and high and session.phase in (Phase.DESCENT,
                                                     Phase.APPROACH,
                                                     Phase.ENROUTE):
            self._fire("top_of_descent", session, state)
        if (state.altitude_ft <= TEN_THOUSAND_FT
                and previous.altitude_ft > TEN_THOUSAND_FT
                and state.vertical_speed_fpm < 0):
            self._fire("ten_thousand_down", session, state)

        # Everything from here is on the way down, and has to say so. A
        # climbing aeroplane at two thousand feet with the gear still out is
        # not on approach, and running the landing checklist at it thirty
        # seconds after takeoff is the most obviously wrong thing the crew
        # could do.
        if not (descending or session.phase in (Phase.APPROACH, Phase.LANDING)):
            return
        if session.phase is Phase.APPROACH or height < 3000:
            if high or session.phase is Phase.APPROACH:
                self._fire("cabin_ready", session, state)
                self._fire("cabin_ready_reply", session, state, delay=12.0)
        if state.gear_down and height < 2500:
            self._fire("approach", session, state)

        if was > height:
            if height <= ONE_THOUSAND_AGL < was:
                self._fire("one_thousand", session, state)
            if height <= FIVE_HUNDRED_AGL < was:
                self._fire("five_hundred", session, state)
            if height <= MINIMUMS_AGL < was:
                self._fire("minimums", session, state)

    # -- on the ground again ---------------------------------------------

    # How long after the wheels touch the cabin starts clapping, and how long
    # it will wait for the speaker and the frequency before the moment has
    # gone. People clap once they are sure the aeroplane is staying down,
    # which is a second or two, and nobody claps at the taxiway.
    APPLAUSE_AFTER_S = 1.8
    APPLAUSE_PATIENCE_S = 15.0

    def _watch_landing(self, previous, state, session) -> None:
        if state.on_ground and not previous.on_ground:
            self._fire("touchdown", session, state)
            self._fire("landed", session, state, delay=45.0)
            self._arm_applause(previous, state)
        if session.phase is Phase.TAXI_IN:
            self._fire("taxi_in", session, state)
        if session.phase is Phase.PARKED or (
                state.on_ground and state.parking_brake
                and not state.engines_running
                and self.state.highest_ft > 1000):
            self._fire("parked", session, state)
            self._fire("disembark", session, state, delay=25.0)

    # -- the cabin, pleased ----------------------------------------------

    def _arm_applause(self, previous, state) -> None:
        """Clap for a touchdown gentler than :data:`APPLAUSE_FPM`.

        Only at the end of a flight: a hop along the runway or a sim placed
        on the ground from the menus is a change of the on-ground flag, not
        a landing anybody sat through.
        """
        rate = touchdown_rate(previous, state)
        why = self.refused_applause()
        if not why and self.state.highest_ft <= 1000.0:
            why = "the aeroplane never really left the ground"
        if not why and rate <= APPLAUSE_FPM:
            why = f"{rate:.0f} fpm is not gentle enough"
        if not why and self.state.applause_at:
            why = "already clapping"
        if why:
            CREW.write("applause", playing=False, fpm=round(rate), why=why)
            return
        now = time.monotonic()
        with self._lock:
            self.state.applause_at = now + self.APPLAUSE_AFTER_S
            self.state.applause_until = (now + self.APPLAUSE_AFTER_S
                                         + self.APPLAUSE_PATIENCE_S)
        CREW.write("applause", playing=False, fpm=round(rate), why="waiting",
                   delay=self.APPLAUSE_AFTER_S)

    def refused_applause(self) -> str:
        """Which setting stops the applause, or "" if none does."""
        for name in ("enabled", "cabin", "applause"):
            if not getattr(self.config.immersion, name, True):
                return f"immersion.{name}"
        return ""

    def _run_applause(self, state) -> None:
        """Start the applause once it is due. Called from the situation loop."""
        with self._lock:
            at, until = self.state.applause_at, self.state.applause_until
        if not at:
            return
        now = time.monotonic()
        if now < at:
            return
        why = ""
        if not state.on_ground:
            why = "the aeroplane is off the ground again"
        elif now > until:
            why = "the moment passed waiting for the speaker"
        elif self.refused_applause():
            why = self.refused_applause()
        elif self.player.busy or self._radio_is_live():
            # The cabin waits for the radio, and the passengers wait for the
            # first officer to finish calling the reversers.
            return
        with self._lock:
            self.state.applause_at = self.state.applause_until = 0.0
        if why:
            CREW.write("applause", playing=False, why=why)
            return
        try:
            audio = applause(self._seed, APPLAUSE_RATE)
            self.player.volume = max(
                0.0, min(1.5, float(self.config.immersion.volume)))
            self.player.play(audio, APPLAUSE_RATE, label="applause")
        except Exception as exc:
            CREW.write("applause", playing=False, why=f"it raised: {exc}")
            log.warning("the applause could not be played: %s", exc,
                        exc_info=True)
            return
        CREW.write("applause", playing=True,
                   seconds=round(len(audio) / float(APPLAUSE_RATE), 2),
                   volume=self.player.volume)
        log.info("crew: the cabin applauds the landing")

    # ------------------------------------------------------------------
    # saying it
    # ------------------------------------------------------------------

    def _fire(self, cue: str, session, state, delay: float = 0.0,
              again: bool = False) -> None:
        """Queue a cue, once.

        Every way out of this method writes a row to the crew log, including
        the ways that say nothing. A cue that was switched off, a cue that had
        already happened and a cue with no words behind it are three different
        reasons for silence and they used to be indistinguishable.
        """
        refused = self.refused(cue)
        if refused:
            self._note(cue, refused)
            return
        with self._lock:
            done = not again and cue in self.state.said
            if not done:
                self.state.said.add(cue)
        # Outside the lock. ``_note`` takes it too, and this one is not
        # reentrant -- writing the row from in here deadlocked the situation
        # loop on the first cue that had already happened, which is every cue
        # from the second pass onwards.
        if done:
            self._note(cue, "already said on this flight")
            return

        # A recording the pilot supplied replaces the written lines outright.
        # One file is one announcement: half a recording followed by half a
        # synthesised one would be worse than either.
        if self._play_recording(cue, delay):
            return

        # The music is the one cue with no words behind it. Where the pilot
        # has no recording of their own it becomes the synthesised bed, which
        # is not a queued line: it has no length, nothing waits for it, and it
        # ends when the aeroplane moves rather than when it runs out.
        if cue == "boarding_music":
            self.music(True, delay)
            return

        script = SCRIPTS.get(self._language, SCRIPTS["en"]).get(cue)
        if not script:
            script = SCRIPTS["en"].get(cue)
        if not script:
            self._note(cue, "it exists only as a recording, and there is none")
            return

        # Which wording. Stable per flight, and stepped on for a cue that is
        # allowed to fire again.
        index = self.state.variants.get(cue)
        if index is None:
            index = (self._seed >> 3) % len(script)
        else:
            index = (index + 1) % len(script)
        self.state.variants[cue] = index

        words = self._words(session, state)
        lines = self._with_english(cue, list(script[index]), session, state)
        at = time.monotonic() + max(0.0, delay)
        with self._lock:
            at = max(at, self._behind_the_queue(cue))
            for line in lines:
                # A cue can have both halves of the aeroplane in it -- the
                # purser calls the cabin secure and the first officer
                # answers -- so the switches are applied line by line as
                # well as cue by cue. Which side a line belongs to is where
                # it comes out of: the interphone is the flight deck and the
                # address system is the cabin.
                if not self._audible(line):
                    continue
                at += line.after_s
                if line.language:
                    # Already filled in, in its own language.
                    text = line.text
                else:
                    try:
                        text = line.text.format(**words)
                    except (KeyError, IndexError):
                        text = line.text
                self.state.queue.append(
                    _Pending(at=at, line=replace(line, text=text), cue=cue,
                             expires=at + line.patience_s))
                # A line's own length has to be allowed for, or a sequence
                # arrives on top of itself. Nothing has been rendered yet, so
                # this is only the order and a first guess at the spacing.
                # What the pilot actually hears between two lines is set by
                # :meth:`_close_up` once the real length is known.
                at += _about_how_long(text)
            self.state.queue.sort(key=lambda p: p.at)
            queued = sum(1 for p in self.state.queue if p.cue == cue)
        CREW.write("cue", cue=cue, fired=True, lines=queued,
                   language=self._language, delay=round(delay, 1),
                   waiting=len(self.state.queue))
        log.info("crew: %s queued %d line(s)", cue, queued,
                 extra={"cue": cue, "lines": queued})

    # How long after a line its English repeat begins. A breath, not a
    # paragraph: the two are one announcement in two languages.
    ENGLISH_AFTER_S = 0.6

    def _repeat_languages(self) -> list[str]:
        """The languages the cabin reads an announcement in after its own.

        The airline's order where it has one -- Brussels Airlines reads
        Dutch, French, English -- less the crew's own language and any there
        are no lines or no voice for. English is always last and never twice.
        A crew whose language was chosen another way (a pinned voice, a
        private aeroplane) repeats in English if its own is not English.
        """
        order = list(self._cabin_order())
        if self._language not in order:
            return [] if self._language == "en" else ["en"]
        # Only what comes after: an Air France crew pinned to English has
        # nothing left to say, rather than a French repeat after the English.
        return order[order.index(self._language) + 1:]

    def _with_english(self, cue: str, lines: list, session, state) -> list:
        """The lines of a cue, with the cabin's other languages woven in.

        Only the address system is repeated. The flight deck and the cabin
        on the interphone share a language already, and a first officer who
        said every callout twice would be two first officers.

        Paired paragraph by paragraph where the scripts have the same number
        of paragraphs, which is how a crew read the safety demonstration --
        each part in every language while it is being shown. Where they do
        not, each whole announcement follows the one before, which is the
        other way airlines do it. Either way the chime is rung once, at the
        start.
        """
        settings = self.config.immersion
        if (not getattr(settings, "bilingual", False)
                or not any(line.path == CABIN_PA for line in lines)):
            return lines
        spoken = [n for n, line in enumerate(lines) if line.path == CABIN_PA]
        if not spoken:
            return lines
        blocks = []
        for language in self._repeat_languages():
            block = self._repeat_in(language, cue, session, state)
            if block:
                blocks.append(block)
        if not blocks:
            return lines

        if all(len(block) == len(spoken) for block in blocks):
            out = []
            for n, line in enumerate(lines):
                out.append(line)
                if n in spoken:
                    at = spoken.index(n)
                    out.extend(block[at] for block in blocks)
            return out
        # After the last line of the announcement, so a reply on the
        # interphone that ends the cue still comes after every language
        # rather than between them.
        last = spoken[-1] + 1
        return (lines[:last] + [line for block in blocks for line in block]
                + lines[last:])

    def _repeat_in(self, language: str, cue: str, session, state) -> list:
        """One cue's address lines in another language, filled in."""
        script = SCRIPTS.get(language, {}).get(cue)
        if not script:
            return []
        index = self.state.variants.get(cue, 0) % len(script)
        words = self._words(session, state, language)
        out = []
        for line in script[index]:
            if line.path != CABIN_PA:
                continue
            try:
                text = line.text.format(**words)
            except (KeyError, IndexError):
                text = line.text
            out.append(replace(line, text=text, language=language, chime="",
                               after_s=self.ENGLISH_AFTER_S))
        return out

    def _faltered(self, line: Line, language: str) -> str:
        """The line as the synthesiser gets it: with the odd "uh" in it.

        Seeded by the flight and the words, so the same line on the same
        flight stalls in the same place -- the render is cached by its text,
        and the text is what this changes.
        """
        manner = _manner(line)
        if language != self._language:
            manner = manner.scaled(aboard.SECOND_LANGUAGE)
        rng = rng_for(self._seed ^ _stable_number(line.text), salt=7)
        return aboard.falter(line.text, language, manner, rng)

    def _abandon(self, cues, why: str) -> None:
        """Drop whatever of these cues is still waiting to be said.

        Written down line by line rather than dropped quietly. "The safety
        briefing stopped after the exits" is a pilot's report, and the answer
        to it is four rows saying which paragraphs the takeoff took and why --
        not a silence that looks exactly like a cabin that never worked.
        """
        with self._lock:
            keep, gone = [], []
            for pending in self.state.queue:
                (gone if pending.cue in cues else keep).append(pending)
            self.state.queue = keep
        for pending in gone:
            CREW.write("line", said=False, cue=pending.cue, why=why,
                       text=pending.line.text)
        if gone:
            log.info("crew: dropped %d line(s) because %s", len(gone), why,
                     extra={"lines": len(gone), "why": why})

    def _behind_the_queue(self, cue: str) -> float:
        """Roughly when there will be nobody left talking. Lock held.

        A cue's ``delay`` is how long after the *event* a real crew would
        start, and several cues come off one event: the doors, the push and
        the captain's welcome are all hung on the parking brake coming off,
        and the taxi checklist, the cabin manager's welcome and the safety
        demonstration are all hung on the aeroplane starting to roll. Laid
        out from the event alone, each of them was dated as though the ones
        in front of it took no time to say -- and the first announcement of a
        departure is forty seconds of words. So the welcome was dated inside
        the captain's announcement and the briefing inside the welcome, and
        what came out of the speaker was the three of them interleaved, a
        paragraph each, in an order no crew has ever used.

        The delay therefore means "not before this", and an announcement
        starts when the one in front of it has finished.

        Not the callouts. "Eighty knots" is about a moment rather than a
        piece of writing: it is either said then or it is dropped
        (:meth:`still_true`), and putting one behind a safety briefing would
        turn a call that is quietly discarded into one that arrives two
        minutes late.
        """
        if CUE_GROUPS.get(cue) == "callouts":
            return 0.0
        end = 0.0
        for pending in self.state.queue:
            end = max(end, pending.at + _about_how_long(pending.line.text))
        return end

    def _note(self, cue: str, why: str) -> None:
        """Write down why a cue said nothing, once per flight.

        Once, because the situation loop runs four times a second and every
        cue that has already happened is refused on every pass. Written
        straight through, that is several hundred rows a minute of "already
        said", and a log nobody can read is the same as no log -- which is
        what this whole file exists to stop.
        """
        with self._lock:
            if (cue, why) in self._noted:
                return
            self._noted.add((cue, why))
        CREW.write("cue", cue=cue, fired=False, why=why)

    def _play_recording(self, cue: str, delay: float) -> bool:
        """Queue the pilot's own recording for a cue, if there is one."""
        settings = self.config.immersion
        if not settings.recordings:
            return False
        event = CUE_RECORDINGS.get(cue) or RECORDED_ONLY.get(cue)
        if not event:
            return False
        try:
            found = self.announcements.pick(event, self._flight())
        except Exception:
            log.debug("could not choose a recording for %s", event,
                      exc_info=True)
            return False
        if found is None:
            return False

        # Recorded announcements go out on the address system whichever cue
        # asked for them: a sound pack is a cabin, and there is nothing in one
        # that belongs on the flight deck interphone.
        #
        # Who it is credited to follows the written lines for the same cue,
        # where there are any. Nearly always that is the purser, which is why
        # this used to say so outright -- but the captain's welcome is a cue
        # with a recording and a captain, and announcing it in the transcript
        # as the cabin manager would be this program contradicting the voice
        # the pilot is listening to.
        who = _speaker_for(cue)
        if not self._audible(Line(who, "", CABIN_PA)):
            return False
        with self._lock:
            self.state.queue.append(_Pending(
                at=time.monotonic() + max(0.0, delay),
                line=Line(who, event, CABIN_PA),
                cue=cue, recording=found))
            self.state.queue.sort(key=lambda p: p.at)
        CREW.write("cue", cue=cue, fired=True, recording=str(found.path.name),
                   who=who, delay=round(delay, 1))
        log.info("crew: %s is playing %s", cue, found.path.name,
                 extra={"cue": cue, "recording": found.path.name})
        return True

    # ------------------------------------------------------------------
    # the music under the boarding
    # ------------------------------------------------------------------

    def music(self, on: bool, delay: float = 0.0) -> None:
        """Ask for the boarding bed, or take it away.

        Not a queued line. A line has a length and the queue waits for it;
        this has neither -- it starts when boarding is under way, it runs for
        as long as that takes, and it ends when the brakes come off. Which
        also means it must never make the player report busy, or the crew
        would have nothing to say for the whole of the boarding.
        """
        if not on:
            with self._lock:
                wanted, self.state.music_at = self.state.music_at, 0.0
            if wanted:
                CREW.write("music", playing=False, why="the flight moved")
            self._bed(False)
            return
        with self._lock:
            if self.state.music_at:
                return
            self.state.music_at = time.monotonic() + max(0.0, delay)
        CREW.write("music", playing=False, why="waiting",
                   delay=round(delay, 1))

    def _run_music(self) -> None:
        """Start the bed once it is due. Called from the situation loop."""
        with self._lock:
            due = (self.state.music_at
                   and not self.state.music_on
                   and time.monotonic() >= self.state.music_at)
        if not due:
            return
        for name in ("enabled", "boarding_music", "cabin"):
            if not getattr(self.config.immersion, name):
                CREW.write("music", playing=False, why=f"immersion.{name}")
                return
        self._bed(True)

    def _bed(self, on: bool) -> None:
        """Put the loop on the cabin path, or take it off.

        The player is asked rather than told: a player without a bed is a
        player that plays announcements and no music, which is a degraded
        cabin rather than a broken one, and the crew are scenery.
        """
        setter = getattr(self.player, "bed", None)
        if setter is None:
            CREW.write("music", playing=False,
                       why="this player has no bed to put it on")
            return
        try:
            if not on:
                setter(None)
                with self._lock:
                    self.state.music_on = False
                return
            self.player.volume = max(
                0.0, min(1.5, float(self.config.immersion.volume)))
            setter(boarding_bed(BOARDING_RATE), BOARDING_RATE)
            with self._lock:
                self.state.music_on = True
            CREW.write("music", playing=True, rate=BOARDING_RATE,
                       volume=self.player.volume)
            log.info("crew: boarding music on")
        except Exception as exc:
            CREW.write("music", playing=False, why=f"it raised: {exc}")
            log.warning("the boarding music could not be started: %s", exc,
                        exc_info=True)

    def _flight(self) -> Flight:
        """What a recording is being chosen for."""
        return Flight(
            airline=_airline_code(self._callsign),
            aircraft=(self._type or "").upper(),
            hour=time.localtime().tm_hour,
            seed=self._seed,
        )

    def _audible(self, line: Line) -> bool:
        """Whether this particular line is switched on.

        By who is speaking rather than by where it comes out. The two are the
        same for nearly every line -- the flight deck talk on the interphone
        and the cabin talk on the address system -- but not for the purser
        calling the flight deck to ask whether anybody wants a coffee, which
        came out of the interphone and was therefore switched off with the
        first officer. A pilot who turns the cabin off means the cabin.
        """
        settings = self.config.immersion
        if line.who in (FIRST_OFFICER, CAPTAIN) and line.path == INTERCOM:
            return bool(settings.copilot)
        if line.who in (PURSER, CABIN) or line.path == CABIN_PA:
            return bool(settings.cabin)
        # The ground crew, who are neither, and who never say anything that
        # was not asked for.
        return True

    def still_true(self, line: Line) -> bool:
        """Whether the aeroplane still matches what this line says about it.

        A cue fires on an event and its lines are spoken over the following
        seconds, so what was true when the queue was written need not be true
        when the words come out. "V one, rotate" to an aeroplane already five
        hundred feet up is the clearest case, and the seatbelt sign is the
        commonest: ten thousand feet is when the sign usually goes off, not
        when it must.

        Nothing is checked for a line that makes no claim, which is nearly all
        of them, and nothing is checked before the first aircraft state has
        arrived -- an unknown aeroplane is not evidence against anything.
        """
        if not line.needs or self._now is None:
            return True
        check = CONDITIONS.get(line.needs)
        if check is None:
            return True
        try:
            return bool(check(self._now, self.state.switches))
        except Exception:
            log.debug("could not check %s", line.needs, exc_info=True)
            return True

    def _floor(self) -> str:
        """Which announcement has begun and has not finished. Lock held.

        An announcement is several lines and the queue is one list of lines,
        so the drain used to take the earliest due line whoever it belonged
        to. Two announcements whose schedules overlapped therefore came out
        of the speaker a paragraph each: "on behalf of Captain Doyle, welcome
        aboard", "we ask for your attention while we point out the safety
        features", "my name is Fiona and looking after you today I have", "your
        seatbelt fastens like this". Each announcement was complete and in
        order, and what the pilot heard was neither.

        :meth:`_behind_the_queue` stops the schedules overlapping in the first
        place; this stops them being interleaved when they overlap anyway --
        a render that took longer than the guess, a pilot who transmitted in
        the middle, a recording whose length nothing knew in advance.

        It cannot stick. The floor is only ever held by a cue that has already
        had a line played, so its remaining lines are spaced off a real
        playback rather than off the clock, and the one kind of line that can
        wait indefinitely -- one holding out for a switch -- hands the floor
        back, because that is the wait the drain is explicitly allowed to walk
        past.
        """
        cue = self.state.saying
        if not cue:
            return ""
        rest = [p for p in self.state.queue if p.cue == cue]
        if not rest:
            return ""
        if not self.still_true(min(rest, key=lambda p: p.at).line):
            return ""
        return cue

    def _drain(self) -> None:
        """Say whatever is due, one line at a time.

        A line whose condition has not come true is held rather than said, and
        the queue is walked past it: one announcement waiting five minutes for
        the seatbelt sign must not keep the top of descent behind it.

        An announcement that has *begun*, though, is not walked past. It used
        to be: the earliest due line won whoever it belonged to, so two
        announcements whose schedules overlapped came out of the speaker
        spliced into each other -- see :meth:`_floor`.

        Nothing is said over the top of something else, and the wait for the
        speaker is spent rendering the next line rather than idling and then
        rendering it in the silence afterwards. That silence is what a
        continuous announcement was arriving in chunks through: the safety
        briefing is six paragraphs, and every one of them used to be
        synthesised only once the paragraph before it had stopped.
        """
        now = time.monotonic()
        if self.player.busy or self._radio_is_live():
            # Somebody is already talking. Waiting is the whole of the fix for
            # two voices at once, and the wait is spent rendering what comes
            # next rather than idling through it.
            #
            # Recorded rather than passed over. A queue that is never drained
            # because something is permanently busy is one of the two ways the
            # cabin goes silent with nothing wrong anywhere, and it is
            # invisible from outside: everything is queued, everything is
            # rendered, nothing is ever played.
            if self.player.busy:
                self._blocked("the cabin player", self.BLOCKED_PLAYER_S)
            else:
                self._blocked("the frequency to clear",
                              self.BLOCKED_FREQUENCY_S)
            self._prepare()
            return
        self._blocked_since = 0.0
        due = None
        dropped: list[str] = []
        with self._lock:
            # Only ever one per pass: the queue is a script, not a mixer.
            floor = self._floor()
            keep: list[_Pending] = []
            for pending in self.state.queue:
                if due is not None or pending.at > now:
                    keep.append(pending)
                elif not self.still_true(pending.line):
                    # Waiting for a switch, or overtaken by the flight. Both
                    # are judged whoever holds the floor, so a callout the
                    # aeroplane has left behind still dies on time rather
                    # than sitting in the queue until the purser finishes.
                    if now < pending.expires:
                        keep.append(pending)
                    else:
                        dropped.append(pending.cue)
                elif floor and pending.cue != floor:
                    keep.append(pending)      # somebody is mid-announcement
                else:
                    due = pending
            self.state.queue = keep
            if due is not None:
                self.state.saying = due.cue
        for cue in dropped:
            CREW.write("line", said=False, cue=cue,
                       why="the flight overtook it")
        if due is None:
            self._prepare()
            return
        if due.recording is not None:
            spoken_s = self.play(due.recording, due.line.who)
        else:
            spoken_s = self.say(due.line, due.voice)
        self._close_up(due, spoken_s)

    # How long the cabin may be held before the log says so, and how often it
    # repeats afterwards. The situation loop runs four times a second, so a
    # row per pass would be four a second of "still waiting" and the file
    # would be unreadable.
    #
    # Two thresholds, because the two waits mean opposite things. Waiting for
    # the frequency should be seconds -- a transmission is short, and a wait
    # that outlasts one is a transmit state that has stuck, which is the
    # failure this whole file was written for. Waiting for the cabin's own
    # player is ordinary: a safety demonstration is six paragraphs and a
    # recorded boarding announcement can run for minutes, and a queue sitting
    # patiently behind one of those is the system working.
    BLOCKED_FREQUENCY_S = 30.0
    BLOCKED_PLAYER_S = 240.0
    BLOCKED_EVERY_S = 120.0

    def _blocked(self, why: str, after_s: float) -> None:
        """Note that the queue could not be drained, without saying so often.

        This is the row that answers the worst version of the report. If the
        radio never goes quiet, or the cabin player never stops reporting
        busy, then every announcement of the flight is queued, rendered and
        never played -- and nothing anywhere is in an error state. One line
        saying the cabin has been waiting four minutes for the frequency is
        the whole diagnosis.
        """
        now = time.monotonic()
        with self._lock:
            if not self.state.queue:
                self._blocked_since = 0.0
                return
            if not self._blocked_since:
                self._blocked_since = now
                self._blocked_said = 0.0
            waited = now - self._blocked_since
            if waited < after_s:
                return
            if self._blocked_said and now - self._blocked_said < self.BLOCKED_EVERY_S:
                return
            self._blocked_said = now
            waiting = len(self.state.queue)
        CREW.write("silence", why=why, waited_s=round(waited, 1),
                   waiting=waiting)
        log.warning("the cabin has been waiting %.0fs for %s, with %d line(s) "
                    "to say", waited, why, waiting,
                    extra={"why": why, "waited_s": round(waited, 1)})

    def _radio_is_live(self) -> bool:
        """Whether the frequency is busy, so nobody in here speaks over it.

        A missing answer is a quiet frequency. The crew are scenery and must
        never be the reason a transmission is lost, so a callable that raises
        is read as "go ahead" rather than as a reason to go silent.
        """
        try:
            return bool(self.radio_busy())
        except Exception:
            log.debug("could not read the radio", exc_info=True)
            return False

    def _prepare(self) -> None:
        """Render the next line before it is due.

        On this thread rather than a worker. Every other call into the
        synthesiser is made from the situation loop, and one cabin
        announcement is not a reason to make it an object two threads share.
        The work is the work :meth:`say` would have done anyway, moved to
        while the speaker is busy or the next line is still a few seconds off.
        """
        if not self.enabled:
            return
        with self._lock:
            # One line in hand is all it takes to close the gap, and two is
            # the most that can be in hand at once -- the one being said next
            # and the one after it. Rendering the whole announcement the
            # moment it is queued would be several seconds of work done well
            # before anybody needs it, for lines the flight may yet drop.
            if sum(1 for p in self.state.queue if p.voice is not None) >= 2:
                return
            # Whoever holds the floor comes next whatever the clock says, so
            # that is whose line is rendered. Without it the render went to
            # the earliest line in the queue, which during a long paragraph
            # is some other cue's line that will not be said until this
            # announcement has finished -- and the gap the rendering exists
            # to close opened again inside the announcement.
            floor = self._floor()
            nxt = None
            for pending in self.state.queue:
                if pending.recording is not None or pending.voice is not None:
                    continue
                # Not a line that is only waiting to find out whether it is
                # worth saying. A callout the flight has overtaken is dropped
                # rather than spoken, and rendering it first would be work
                # done to throw away.
                if not self.still_true(pending.line):
                    continue
                if nxt is None or ((pending.cue != floor, pending.at)
                                   < (nxt.cue != floor, nxt.at)):
                    nxt = pending
        if nxt is None:
            return
        voice = self._render(nxt.line)
        if voice is not None:
            # Set on the object rather than back into the queue. A line
            # dropped while it was being rendered is a line nobody will ever
            # look at again, so nothing has to be put back.
            nxt.voice = voice

    def _close_up(self, spoken: _Pending, spoken_s: float) -> None:
        """Move the rest of the announcement up behind what was just said.

        The queue is written before anything has been rendered, so the space
        each line takes up in it is a guess -- roughly three words a second.
        The guess is only ever roughly right, and every line it overshoots
        adds its own error to the silence before the next one. Once a line has
        been played its length is known exactly, so the rest of that
        announcement is put where the words file asked for it: its own
        ``after_s`` after this line stops, and not a second more.

        Only that announcement moves. Another cue's timing is its own.
        """
        if spoken_s <= 0.0:
            return
        ends = time.monotonic() + spoken_s
        with self._lock:
            rest = [p for p in self.state.queue if p.cue == spoken.cue]
            if not rest:
                return
            nxt = min(rest, key=lambda p: p.at)
            shift = (ends + nxt.line.after_s) - nxt.at
            for pending in rest:
                pending.at += shift
                pending.expires += shift
            self.state.queue.sort(key=lambda p: p.at)

    def play(self, recording, who: str = PURSER) -> float:
        """Put one of the pilot's own recordings on the cabin audio path.

        Played as it was recorded: no radio chain, no cabin speaker
        colouring, no chime in front of it. Somebody who has gone to the
        trouble of supplying a real announcement has already decided how it
        should sound, and a processing chain laid over the top of that is
        this program overruling them about their own recording.

        Returns how long it will take to play, which is what the rest of the
        queue is moved up behind.
        """
        settings = self.config.immersion
        try:
            audio, rate = load_recording(recording.path)
        except Exception as exc:
            log.warning("could not read the announcement %s: %s",
                        recording.path.name, exc)
            CREW.write("line", said=False, recording=recording.path.name,
                       why=f"it could not be read: {exc}")
            return 0.0
        if not len(audio):
            CREW.write("line", said=False, recording=recording.path.name,
                       why="the file holds no audio")
            return 0.0
        self.player.volume = max(0.0, min(1.5, float(settings.volume)))
        self.player.play(audio, rate, label=recording.path.stem)
        seconds = len(audio) / float(rate or 1)
        CREW.write("line", said=True, who=who, path=CABIN_PA,
                   recording=recording.path.name, seconds=round(seconds, 2),
                   rate=rate, peak=round(_peak(audio), 4),
                   volume=self.player.volume)
        self.emit("crew", _spoken_name(recording),
                  _SPEAKER_NAMES.get(who, who), path=CABIN_PA)
        return seconds

    def _render(self, line: Line):
        """Synthesise one line, or nothing if the engine could not.

        Split out of :meth:`say` so that the identical call can be made before
        the line is due, which is the whole of what :meth:`_prepare` does.
        """
        position = line.who
        rough = bool(getattr(self.config.immersion, "realism", False))
        if line.path == CABIN_PA:
            profile = aboard.CREW_CABIN if rough else PROFILE_CABIN
        else:
            profile = aboard.CREW_INTERCOM if rough else PROFILE_INTERCOM
        language = line.language or self._language
        # In their own language they are cast from their own accent pool; in
        # a second one they keep their accent, which is what an English
        # announcement from a French purser sounds like.
        own = language == self._language
        accent = self._accent() if own else self._language
        voice_key = self._voice_for(position)
        if not own:
            # The same person where the engine has one who speaks both.
            # Failing that, an automatic cast of the same sex and accent:
            # airlines often hand the English to a colleague anyway.
            voice_key = in_language(self._cast.get(position, ""), language)
        text = self._faltered(line, language) if rough else line.text
        try:
            transmission = self.synth.speak(
                text,
                # Cast from the aeroplane rather than from an airport, so the
                # crew are this aircraft's crew and stay the same people for
                # as long as it does.
                facility=self._callsign or "crew",
                position=position,
                ident=self._callsign or "crew",
                language=language,
                profile=profile,
                radio=True,
                signal_quality=1.0,
                gender=self._gender_for(position),
                voice_key=voice_key,
                accent=accent,
                avoid=self._taken_by_others(position),
                # Nobody in this aeroplane is on a frequency. The delivery
                # shaping assumes a transmission that opens with a callsign
                # and is said into a transmit bar, and neither is true of
                # "positive rate, gear up" -- which it would read as two
                # sentences, or open with an "uh".
                shaped=False,
            )
            if own and getattr(transmission, "voice_key", ""):
                self._cast[position] = transmission.voice_key
            return transmission
        except Exception as exc:
            # Loudly, and once. A synthesiser that cannot render one crew line
            # will not render the next one either, so this is the whole cabin
            # for the rest of the flight rather than one lost announcement --
            # and it used to be a debug line nobody had the level turned up
            # far enough to see.
            CREW.write("line", said=False, who=position,
                       why=f"the synthesiser raised: {exc}",
                       voice=self._voice_for(position),
                       language=self._language)
            if not self._render_reported:
                self._render_reported = True
                log.warning("the crew cannot be synthesised: %s", exc,
                            exc_info=True)
                self.emit("error",
                          f"The crew cannot be synthesised, so the cabin is "
                          f"silent. The radio is unaffected. ({exc})",
                          key="log.crew_no_voice", detail=str(exc))
            else:
                log.debug("crew synthesis failed again: %s", exc)
            return None

    def say(self, line: Line, voice=None) -> float:
        """Put one line on the cabin audio path, and say how long it runs.

        ``voice`` is the line already rendered, which is the ordinary case:
        it was synthesised while the line before it was still playing.
        Rendering here is the fallback for the first line of an announcement
        and for anything said without going through the queue.
        """
        settings = self.config.immersion
        position = line.who
        transmission = voice if voice is not None else self._render(line)
        if transmission is None:
            return 0.0

        audio = transmission.audio
        rate = transmission.sample_rate
        if line.chime and settings.chimes:
            bell = make_chime(up=line.chime != "down", sample_rate=rate)
            audio = np.concatenate([bell, silence(0.45, rate), audio])

        self.player.volume = max(0.0, min(1.5, float(settings.volume)))
        self.player.play(audio, rate, label=f"{position}")
        seconds = len(audio) / float(rate or 1)
        # What was handed over, rather than what was asked for. A line that
        # reaches the player as three seconds of digital silence is a
        # different fault from one that never reached it, and from outside
        # the aeroplane the two are the same nothing.
        CREW.write("line", said=True, who=position, path=line.path,
                   text=line.text, seconds=round(seconds, 2),
                   rate=rate, peak=round(_peak(audio), 4),
                   volume=self.player.volume,
                   voice=getattr(transmission, "voice_key", ""))
        self.emit("crew", line.text, _SPEAKER_NAMES.get(position, position),
                  path=line.path)
        return seconds

    # ------------------------------------------------------------------
    # answering the flight deck
    # ------------------------------------------------------------------

    def names(self) -> tuple[str, ...]:
        """What the people in the back are called on this flight.

        Read by the parser, so that a pilot can call the cabin manager by the
        name they introduced themselves with. Cast here if nothing has needed
        them yet: they come out of the same seed whenever they are asked for,
        and a pilot who addresses the cabin before the cabin has said anything
        should not be the one transmission that goes to the controller.
        """
        if not self.state.named:
            self.state.named = _cabin_names(self._seed, self._language,
                                            self._gender_for(PURSER))
        return self.state.named

    def ask(self, said, answer: str = "") -> bool:
        """Somebody in the aeroplane answers something the pilot just said.

        ``said`` is what :mod:`wilcoatc.atc.interphone` made of the
        transmission. ``answer`` overrides which reply is used, which is how
        the ramp says it could not do the thing it was asked for: the words
        are the same table, the choice is the caller's, because only the
        caller knows whether the tug actually came.

        Nothing here is a cue. A cue happens once a flight because the flight
        reached a point; this happens as often as it is asked for, and two
        coffees on one flight is not a bug.
        """
        settings = self.config.immersion
        if not settings.enabled or said is None:
            return False
        if not settings.interphone:
            return False
        # The cabin switch silences the cabin, including its answers. The
        # ground crew are not the cabin and are not the other seat, so they
        # are only ever behind the immersion switch itself.
        if said.who == interphone.CABIN and not settings.cabin:
            CREW.write("ask", answered=False, who=said.who, what=said.what,
                       why="immersion.cabin")
            return False

        key = answer or f"{said.who}_{said.what}"
        book = ANSWERS.get(self._language, ANSWERS["en"])
        script = book.get(key) or ANSWERS["en"].get(key)
        if not script:
            # Addressed, understood, and nothing written for it. Better to
            # answer the address than to leave a pilot talking to a cabin
            # that does not react at all.
            key = f"{said.who}_{interphone.ATTENTION}"
            script = book.get(key) or ANSWERS["en"].get(key)
        if not script:
            CREW.write("ask", answered=False, who=said.who, what=said.what,
                       why="there are no words for it")
            return False

        index = self.state.variants.get(key)
        index = (self._seed >> 5) % len(script) if index is None else (
            (index + 1) % len(script))
        self.state.variants[key] = index

        try:
            words = self._words(self._session, self._now)
        except Exception:
            # The crew are scenery. A flight with no session yet, or an
            # airport lookup that raised, is not a reason to leave the pilot
            # talking to an aeroplane that does not answer.
            log.debug("could not fill in the crew's words", exc_info=True)
            words = {}
        at = time.monotonic() + self.ANSWER_DELAY_S
        with self._lock:
            for line in script[index]:
                at += line.after_s
                try:
                    text = line.text.format(**words)
                except (KeyError, IndexError):
                    text = line.text
                self.state.queue.append(
                    _Pending(at=at, line=replace(line, text=text), cue=key,
                             expires=at + line.patience_s))
                at += 1.2 + len(text.split()) / 2.6
            self.state.queue.sort(key=lambda p: p.at)
        CREW.write("ask", answered=True, who=said.who, what=said.what,
                   key=key, heard=said.text, addressed=said.addressed,
                   language=self._language)
        log.info("crew: answering %s", key, extra={"key": key})
        return True

    # How long the aeroplane takes to answer. Not instant: the pilot has just
    # let go of the transmit bar, and a reply that begins before they have
    # sounds like a machine that was waiting rather than a person who was
    # listening.
    ANSWER_DELAY_S = 1.2

    # ------------------------------------------------------------------
    # what the other seat says about a clearance
    # ------------------------------------------------------------------

    def confirm(self, instruction) -> None:
        """The first officer reads the numbers back to you, off the radio.

        Not a readback. The controller is still waiting for one; this is the
        pilot monitoring saying what they heard so that two people in the
        aeroplane have the same number. It is the single most useful thing a
        first officer does and it is why the setting exists.
        """
        settings = self.config.immersion
        if not (settings.enabled and settings.copilot and settings.confirmations):
            return
        if instruction is None or not getattr(instruction, "values", None):
            return
        text = confirmation(instruction, self._language)
        if not text:
            return
        with self._lock:
            # A moment behind the controller, so it lands after the
            # transmission rather than under it.
            self.state.queue.append(_Pending(
                at=time.monotonic() + 2.5,
                line=Line(FIRST_OFFICER, text, INTERCOM),
                cue="confirm"))
            self.state.queue.sort(key=lambda p: p.at)

    # ------------------------------------------------------------------
    # the words that go into the words
    # ------------------------------------------------------------------

    def _words(self, session, state, language: str = "") -> dict[str, str]:
        """What fills the blanks in a line, in ``language`` (the crew's own
        when empty).

        The people keep their names in any language. The English repeat of a
        French welcome is the same cabin manager, not a second one with an
        English name.
        """
        settings = self.config.immersion
        # Either may be missing: an answer to the pilot is built off the
        # situation loop, and the crew can be spoken to before the first
        # aircraft state has arrived.
        session = session if session is not None else self._session
        state = state if state is not None else self._now
        callsign = session.aircraft.callsign if session else None
        destination = session.destination if session else None
        departure = session.departure if session else None

        language = language or self._language
        airline = settings.airline.strip() or _airline_from(callsign, language)
        # Where the flight is going. Failing a filed destination, the field
        # the aeroplane is actually at -- which is what makes "welcome to
        # {destination}" work on landing for a flight nobody filed a plan
        # for, and it is right far more often than "our destination" is.
        arrival = destination
        if arrival is None and self.here is not None:
            try:
                arrival = self.here()
            except Exception:
                log.debug("could not find the field below", exc_info=True)
        where = _place(arrival, language)
        temperature, local = self._destination_weather(arrival, language)

        # Who is in the back, and what they are called. Written down as well
        # as spoken: once the cabin manager has said their name, a pilot may
        # use it to call them, and the parser is given this list to match on.
        crew = _cabin_names(self._seed, self._language,
                            self._gender_for(PURSER))
        self.state.named = crew

        return {
            "airline": airline,
            "service": _service(callsign, airline, language),
            "origin": _place(departure, language),
            "destination": where,
            # The pilot's own captain, where they have said who that is.
            # Said exactly as it was typed: "Captain {captain}" is how every
            # line is written, so a full name reads correctly and so does a
            # surname on its own.
            "captain": (settings.captain_name.strip()
                        or CAPTAIN_NAMES[self._seed % len(CAPTAIN_NAMES)]),
            "purser": crew[0],
            "crew": _join(crew[1:], language),
            # Against the region the flight is in, so the captain and the
            # controller call the same level by the same name.
            "cruise": _level(getattr(state, "altitude_ft", 0.0), language,
                             transition_altitude(
                                 getattr(departure, "ident", "")
                                 or getattr(arrival, "ident", ""))),
            "time": _duration(session, state, language),
            "local": local,
            "temperature": temperature,
            "runway": (session.departure_runway or session.arrival_runway
                       or _say(language, "in_use")),
        }

    def _destination_weather(self, airport,
                             language: str = "") -> tuple[str, str]:
        """Temperature and local time at the far end, if either is known."""
        language = language or self._language
        temperature = _say(language, "pleasant")
        if airport is not None and self.weather_for is not None:
            try:
                weather = self.weather_for(airport.ident)
                temperature = _say(language, "degrees",
                                   degrees=round(weather.temperature_c))
            except Exception:
                log.debug("no weather for the arrival announcement",
                          exc_info=True)
        return temperature, _clock_now(language)

    def _gender_for(self, position: str) -> str:
        """The sex the settings ask for in one seat, or "" for either.

        The first officer's setting is the first officer's. It used to be
        the captain's as well, and so was the pinned voice, which is how the
        captain's welcome came out in the first officer's voice from the back
        of the aeroplane: the co-pilot making the cabin's announcements.

        A pinned voice that cannot speak the crew's language is replaced by
        an automatic cast, and keeps its sex: a pilot who picked a woman
        still hears one.
        """
        settings = self.config.immersion
        if position == FIRST_OFFICER:
            wanted, pinned = settings.copilot_gender, self._pinned(position)
        elif position in (PURSER, CABIN):
            wanted, pinned = settings.cabin_gender, self._pinned(position)
        else:
            # The captain and the ground crew. Nobody has a setting for
            # either, so they are cast from the aeroplane and left alone.
            return ""
        if wanted in ("m", "f"):
            return wanted
        return voice_gender(pinned) if pinned else ""

    def _pinned(self, position: str) -> str:
        """The voice the settings name for one seat, as the settings say it.

        One person each. The cabin voice is the cabin manager's: the crew
        member who answers them is somebody else, and giving them the same
        pin made the purser call the doors and then answer herself.
        """
        settings = self.config.immersion
        if position == FIRST_OFFICER:
            key = settings.copilot_voice
        elif position == PURSER:
            key = settings.cabin_voice
        else:
            return ""
        key = (key or "").strip()
        return "" if key == "auto" else key

    def _voice_for(self, position: str) -> str:
        """The pinned voice for one seat, able to speak the crew's language.

        A Supertonic speaker is the same person in any language, so the pin
        follows the crew into it. Anything else speaks one language, and a
        pin that cannot speak this crew's is set aside for an automatic cast
        rather than being made to read words it has no model for -- an
        English voice with French phonemes pushed through it is not a French
        purser, it is an accent nobody can place.
        """
        key = self._pinned(position)
        if not key:
            return ""
        spoken = in_language(key, self._language)
        if not spoken and key not in self._unspoken:
            self._unspoken.add(key)
            CREW.write("voice", who=position, voice=key,
                       why=f"it does not speak {self._language}, which the "
                           f"crew speak on this flight",
                       language=self._language)
        return spoken

    def _taken_by_others(self, position: str) -> tuple[str, ...]:
        """Voices another seat already speaks in, which this one must not draw.

        The two pinned seats avoid each other's pins. The two that are never
        pinned, the captain and the cabin crew member, are cast after them
        and avoid whoever everybody before them turned out to be: a French
        crew has so few voices that the purser and the crew member answering
        her both drew the same one, and so did the first officer and the
        captain.
        """
        if position in (FIRST_OFFICER, PURSER):
            other = PURSER if position == FIRST_OFFICER else FIRST_OFFICER
            key = self._voice_for(other)
            return (key,) if key else ()
        if position not in (CAPTAIN, CABIN):
            return ()
        before = [FIRST_OFFICER, PURSER] + ([CAPTAIN] if position == CABIN
                                            else [])
        cast = getattr(self.synth, "cast", None)
        taken: list[str] = []
        for seat in before:
            key = self._voice_for(seat)
            if not key and cast is not None:
                try:
                    key = cast(self._callsign or "crew", seat,
                               self._callsign or "crew",
                               language=self._language,
                               gender=self._gender_for(seat),
                               accent=self._accent(),
                               # What that seat's own render will avoid, so
                               # the answer here is the voice it gets.
                               avoid=(self._taken_by_others(seat)
                                      if seat != CAPTAIN else tuple(taken)))
                except Exception:
                    log.debug("could not cast the %s", seat, exc_info=True)
                    key = ""
            if key:
                taken.append(key)
        return tuple(taken)

    def _accent(self) -> str:
        """Which accent pool the crew are cast from.

        Their own. A crew speaking their own language are never an accent
        produced by substituting phonemes -- that is for a controller
        speaking English at a foreign field -- so a French crew are French
        voices and an English-speaking one is British or American. American
        where the airline is, or the registration, or the field it left from;
        British everywhere else.

        This used to be the callsign, read as though it were an airport: SWR
        is not in Portugal and THY is not in Spain, but that is where their
        crews' accents came from.
        """
        language = self._language
        if language != "en":
            return language
        code = _airline_code(self._callsign)
        if code:
            try:
                from .operators import by_icao

                found = by_icao(code)
            except Exception:                            # pragma: no cover
                found = None
            if found is not None and found.home:
                home = accent_for(found.home[0].ljust(4, "X"))
                return home if home in ("us", "gb") else "gb"
        if self._callsign.upper().startswith("N"):
            return "us"
        departure = getattr(self._session, "departure", None)
        field = accent_for(getattr(departure, "ident", "") or "")
        return field if field in ("us", "gb") else "gb"


# --------------------------------------------------------------------------
# small conversions
# --------------------------------------------------------------------------
#
# The fragments the announcements are *built* from rather than written from: a
# cruising level, a flight time, a clock. They live here because they are
# arithmetic and the words are in crew_lines, and they are a table because the
# alternative was what this program used to do -- read a German cabin
# announcement and then say "flight level 350" in the middle of it.

_WORDS: dict[str, dict[str, str]] = {
    "en": {
        "somewhere": "our destination",
        "this_flight": "this flight",
        "the_airline": "this flight",
        "in_use": "in use",
        "cruise": "our cruising altitude",
        "flight_level": "flight level {level}",
        "feet": "{feet} feet",
        "about_an_hour": "a little over an hour",
        "minutes": "{minutes} minutes",
        "hours": "{hours} hour{s}",
        "hours_minutes": "{hours} hour{s} and {minutes} minutes",
        "degrees": "{degrees} degrees",
        "pleasant": "pleasant",
        "clock": "{hour} {minute} {part}",
        "morning": "in the morning",
        "afternoon": "in the afternoon",
        "evening": "in the evening",
        "service": "{airline} flight {number}",
        "crew_and": "and",
    },
    "fr": {
        "somewhere": "notre destination",
        "this_flight": "ce vol",
        "the_airline": "notre compagnie",
        "in_use": "en service",
        "cruise": "notre altitude de croisière",
        "flight_level": "niveau de vol {level}",
        "feet": "{feet} pieds",
        "about_an_hour": "un peu plus d'une heure",
        "minutes": "{minutes} minutes",
        "hours": "{hours} heure{s}",
        "hours_minutes": "{hours} heure{s} et {minutes} minutes",
        "degrees": "{degrees} degrés",
        "pleasant": "agréable",
        "clock": "{hour} heures {minute}",
        "service": "ce vol {airline} {number}",
        "crew_and": "et",
    },
    "de": {
        "somewhere": "unserem Zielort",
        "this_flight": "unserem Flug",
        "the_airline": "unserer Fluggesellschaft",
        "in_use": "in Betrieb",
        "cruise": "unsere Reiseflughöhe",
        "flight_level": "Flugfläche {level}",
        "feet": "{feet} Fuß",
        "about_an_hour": "etwas mehr als eine Stunde",
        "minutes": "{minutes} Minuten",
        "hours": "{hours} Stunde{s}",
        "hours_minutes": "{hours} Stunde{s} und {minutes} Minuten",
        "degrees": "{degrees} Grad",
        "pleasant": "angenehm",
        "clock": "{hour} Uhr {minute}",
        "service": "{airline} Flug {number}",
        "crew_and": "und",
    },
    "nl": {
        "somewhere": "onze bestemming",
        "this_flight": "deze vlucht",
        "the_airline": "onze maatschappij",
        "in_use": "in gebruik",
        "cruise": "onze kruishoogte",
        "flight_level": "vliegniveau {level}",
        "feet": "{feet} voet",
        "about_an_hour": "iets meer dan een uur",
        "minutes": "{minutes} minuten",
        "hours": "{hours} uur",
        "hours_minutes": "{hours} uur en {minutes} minuten",
        "degrees": "{degrees} graden",
        "pleasant": "aangenaam",
        "clock": "{hour} uur {minute}",
        "service": "{airline} vlucht {number}",
        "crew_and": "en",
    },
}

# Which languages say a clock on a twelve-hour dial. English does; the rest of
# the crew languages read the twenty-four hour one off the same instrument
# panel as everybody else.
_TWELVE_HOUR: frozenset[str] = frozenset({"en"})

# The plural mark the "hour" templates take. German's is empty because
# "Stunden" is not "Stunde" with an s on it, so the whole word is switched;
# Dutch has none at all, because two hours is "twee uur".
_PLURALS: dict[str, tuple[str, str]] = {
    "en": ("", "s"), "fr": ("", "s"), "de": ("", "n"), "nl": ("", ""),
}


def _manner(line: Line) -> "aboard.Manner":
    """How readily whoever says this line stalls. See wilcoatc.audio.aboard."""
    if line.path == CABIN_PA:
        if line.who == CAPTAIN:
            return aboard.CAPTAIN_PA
        return aboard.PURSER_PA
    if line.who == GROUND:
        return aboard.RAMP
    return aboard.FLIGHT_DECK


def _say(language: str, key: str, **values) -> str:
    """One built fragment, in the language the crew are speaking."""
    book = _WORDS.get(language) or _WORDS["en"]
    template = book.get(key) or _WORDS["en"][key]
    return template.format(**values) if values else template

def _peak(audio) -> float:
    """How loud what was handed to the player actually is.

    Written into the crew log beside every line, because a waveform of digital
    silence and no waveform at all sound identical and are completely
    different faults. One is a synthesiser that produced nothing; the other is
    a device that played nothing.
    """
    try:
        return float(np.max(np.abs(np.asarray(audio, dtype=np.float32))))
    except Exception:
        return 0.0


def _cabin_names(seed: int, language: str, gender: str = "") -> tuple[str, ...]:
    """The cabin manager's own name, then the crew they name with them.

    From the seed, so the same aeroplane keeps the same cabin across a
    re-fly, and from the language, because a German cabin whose manager is
    called Siobhan is the immersion running backwards.

    Mixed on purpose after the first: a cabin of three people with names from
    one pool reads as a list rather than as a crew.
    """
    book = FIRST_NAMES.get(language) or FIRST_NAMES["en"]
    lead = book.get(gender if gender in ("m", "f") else "f") or book["f"]
    names = [lead[seed % len(lead)]]
    for index in range(CREW_NAMED):
        pool = book["m"] if index % 2 == 0 else book["f"]
        at = (seed >> (4 * (index + 1))) % len(pool)
        # Past anybody already named. Bounded rather than a while: a pool
        # shorter than the crew would otherwise spin here forever.
        for _ in range(len(pool)):
            if pool[at] not in names:
                break
            at = (at + 1) % len(pool)
        names.append(pool[at])
    return tuple(names)


def _join(names, language: str = "en") -> str:
    """Two or three names, said the way somebody would say them."""
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    joiner = _say(language, "crew_and")
    return f"{', '.join(names[:-1])} {joiner} {names[-1]}"


def _speaker_for(cue: str) -> str:
    """Who a recorded cue is credited to.

    Read off the written lines for the same cue, so the transcript names the
    person the recording is of rather than a fixed guess. A cue that exists
    only as a recording has no lines to read, and the purser is the right
    default for those: they are all cabin announcements.
    """
    script = SCRIPTS["en"].get(cue)
    if script and script[0]:
        return script[0][0].who
    return PURSER


_SPEAKER_NAMES: dict[str, str] = {
    FIRST_OFFICER: "First officer",
    CAPTAIN: "Captain",
    PURSER: "Cabin manager",
    CABIN: "Cabin crew",
    GROUND: "Ground crew",
}


def _after_dark() -> bool:
    """Whether the cabin lights would be dimmed for departure.

    Local clock time, not a sun angle. The cabin crew dim the lights for a
    night departure so that eyes are adjusted if the aeroplane has to be got
    out of in a hurry, and they go by the time of day like everybody else.
    """
    hour = time.localtime().tm_hour
    return hour >= 19 or hour < 7


def _stable_number(text: str) -> int:
    """A repeatable number from a string, for casting and for wording."""
    import hashlib

    return int(hashlib.sha1(text.encode("utf-8")).hexdigest()[:8], 16)


def _spoken_name(recording) -> str:
    """What the transcript says about a recording that was played.

    There are no words to show -- it is somebody's audio file -- so it says
    which announcement it was and which pack it came from. Enough to know it
    happened, and enough to find the file when it turns out to be the wrong
    one.
    """
    where = f" · {recording.airline}" if recording.airline else ""
    return f"[{recording.path.stem}{where}]"


_FLIGHT_ID = re.compile(r"^[A-Z]{3}[0-9][A-Z0-9]{0,3}$")


def _airline_code(written: str) -> str:
    """The operator designator a pack folder is named for.

    Fenix takes this from ``icao_airline`` in aircraft.cfg, which only exists
    on a liveried airliner. It is taken from the callsign here instead, so a
    pack works in an aeroplane that has no livery -- and a private
    registration simply has none, and falls through to the default folder.
    """
    text = (written or "").strip().upper()
    # An ICAO flight identification is the designator and up to four
    # characters, the first of them a digit: BEL6VF as well as BAW123.
    # Digits only made Brussels Airlines' BEL6VF nobody's flight, so its crew
    # took the language of the field it left from -- French at Nice -- and
    # then repeated everything in English.
    if _FLIGHT_ID.match(text):
        return text[:3]
    return ""


def _airline_from(callsign, language: str = "en") -> str:
    """What the crew call the airline.

    The callsign already carries it: the radio has to say "Delta twelve
    thirty four" rather than "delta alpha lima", so the telephony name was
    resolved when the callsign was built and there is no second table here
    to fall out of step with the first. A private registration has no
    airline, and the crew say "this flight" rather than inventing a company
    for it.
    """
    if callsign is None:
        return _say(language, "the_airline")
    if getattr(callsign, "is_airline", False):
        name = (getattr(callsign, "telephony", "") or "").strip()
        if name:
            return name
    return _say(language, "the_airline")


def _service(callsign, airline: str, language: str = "en") -> str:
    """The flight as an announcement to the cabin names it.

    One substitution rather than an airline and a number side by side,
    because the two do not compose: a private registration has no flight
    number, and "{airline} flight {flight}" came out of the templates as
    "this flight flight November one seven two Sierra Papa".
    """
    spoken = _flight_number(callsign, language)
    if callsign is not None and getattr(callsign, "is_airline", False):
        return _say(language, "service", airline=airline, number=spoken)
    return _say(language, "this_flight")


def _flight_number(callsign, language: str = "en") -> str:
    """The flight, as an announcement to the cabin says it.

    An airline flight is its number, read the way the radio reads it --
    "twelve thirty four", not "one two three four", because a passenger is
    being told a flight number and not a clearance. A private aeroplane has
    no flight number, so its registration is what it is called.
    """
    if callsign is None:
        return _say(language, "this_flight")
    spoken = (getattr(callsign, "spoken_full", "") or "").strip()
    telephony = (getattr(callsign, "telephony", "") or "").strip()
    if getattr(callsign, "is_airline", False) and telephony:
        if spoken.lower().startswith(telephony.lower()):
            rest = spoken[len(telephony):].strip()
            if rest:
                return rest
    return spoken or _say(language, "this_flight")



def _place(airport, language: str = "en") -> str:
    """Where an airport is, as a person would say it."""
    nowhere = _say(language, "somewhere")
    if airport is None:
        return nowhere
    town = getattr(airport, "municipality", "") or ""
    if town:
        return town
    return getattr(airport, "spoken", "") or getattr(airport, "name", "") or \
        getattr(airport, "ident", nowhere)


def _level(altitude_ft: float, language: str = "en",
           transition: int = 18_000) -> str:
    """A cruising level, as the captain says it to the cabin."""
    feet = round(float(altitude_ft or 0) / 500.0) * 500
    if feet <= 0:
        return _say(language, "cruise")
    # Above the transition altitude the crew are on standard pressure and
    # would say a level; below it, a height. The announcement follows the
    # same rule, because it is the same two people saying it -- and it
    # follows the *region's* transition, because a captain over London
    # telling the cabin "twelve thousand feet" for the level air traffic
    # control just called flight level one two zero is the same aeroplane
    # disagreeing with itself.
    if feet >= (transition or 18_000):
        return _say(language, "flight_level", level=int(feet / 100))
    return _say(language, "feet", feet=_thousands(int(feet), language))


def _thousands(feet: int, language: str) -> str:
    """A height, written out rather than punctuated.

    This goes to a speech synthesiser, and a thousands separator is a comma
    to it, which is a pause. English says the two halves separately -- "twelve
    thousand five hundred" is not one word -- and the languages that spell a
    whole number as one word are spelled by :mod:`wilcoatc.atc.numbers`, which
    is the same table the controller reads numbers out of.
    """
    if language != "en":
        try:
            from .numbers import cardinal

            return cardinal(feet, language)
        except (KeyError, ValueError):
            pass
    thousands, hundreds = divmod(feet, 1000)
    words = f"{thousands} thousand" if thousands else ""
    if hundreds:
        words = f"{words} {hundreds}".strip()
    return words or "zero"


def _duration(session, state, language: str = "en") -> str:
    """Roughly how long the flight is, from the distance still to run."""
    destination = getattr(session, "destination", None)
    if destination is None or not getattr(state, "has_position", False):
        return _say(language, "about_an_hour")
    try:
        from ..navdata.db import haversine_nm

        distance = haversine_nm(state.latitude, state.longitude,
                                destination.lat, destination.lon)
    except Exception:
        return _say(language, "about_an_hour")
    speed = max(120.0, float(state.ground_speed_kt or 420.0))
    minutes = int(round(distance / speed * 60.0))
    if minutes < 45:
        return _say(language, "minutes", minutes=max(10, minutes))
    hours, rest = divmod(minutes, 60)
    rest = int(round(rest / 5.0) * 5)
    if rest >= 60:
        hours, rest = hours + 1, 0
    one, many = _PLURALS.get(language, _PLURALS["en"])
    plural = one if hours == 1 else many
    if not rest:
        return _say(language, "hours", hours=hours, s=plural)
    return _say(language, "hours_minutes", hours=hours, s=plural,
                minutes=rest)


def _clock_now(language: str = "en") -> str:
    """The local time, as an announcement says it.

    On a twelve-hour dial where the language uses one and a twenty-four hour
    one everywhere else. A German purser saying "3 15 in the afternoon" is
    the same mistake as an English one saying "fifteen fifteen".
    """
    now = time.localtime()
    if language in _TWELVE_HOUR:
        part = _say(language, "morning" if now.tm_hour < 12 else (
            "afternoon" if now.tm_hour < 18 else "evening"))
        return _say(language, "clock", hour=now.tm_hour % 12 or 12,
                    minute=f"{now.tm_min:02d}", part=part)
    return _say(language, "clock", hour=now.tm_hour,
                minute=f"{now.tm_min:02d}", part="")


__all__ = ["CrewDirector", "CrewState", "CAPTAIN_NAMES"]
