"""Configuration loading.

Defaults are chosen so the system runs correctly with no config file at all.
``config.yaml`` in the project root overrides any subset of them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

from .paths import bundle_root, config_path

ROOT = bundle_root()
CONFIG_PATH = config_path()


@dataclass
class AudioConfig:
    input_device: int | str | None = None
    output_device: int | str | None = None
    volume: float = 1.0
    capture_rate: int = 16000
    max_transmission_s: float = 30.0
    # Radio effects are what make synthesised speech sound like ATC; turning
    # them off is useful only for debugging the voice itself.
    radio_effects: bool = True
    sidetone: bool = False


@dataclass
class PttConfig:
    mode: str = "keyboard"          # keyboard | joystick | none
    key: str = "grave"
    joystick_index: int = 0
    joystick_button: int = 0


@dataclass
class SpeechConfig:
    model: str = "small.en"
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = 5
    language: str = "en"
    # Which languages you may speak is not a setting: it follows the aeroplane.
    # English everywhere, plus the local language over a state that works one,
    # changing as the flight crosses a border. Leave this empty for that.
    #
    # Filling it in pins the set instead, which is worth doing in exactly one
    # case: ["en"] keeps the small English-only recogniser on a machine that
    # will never leave English-speaking airspace, and it is faster.
    languages: list[str] = field(default_factory=list)
    # How sure detection must be before it switches away from the first
    # language. Below this, the audio is decoded in every candidate and the
    # decoder's own confidence decides, which is what stops heavily accented
    # English being heard as the speaker's mother tongue.
    detection_threshold: float = 0.85
    min_confidence: float = 0.35


@dataclass
class VoiceConfig:
    voice_dir: str = "data/voices"
    seed: str = "wilcoatc"
    cache_size: int = 512
    preload: bool = False
    # Which synthesiser speaks.
    #
    # auto       -- Supertonic if it is installed: ten speakers of each sex in
    #               every controller language, against one speaker per
    #               language from Kokoro. Then Kokoro, which keeps British
    #               English because Supertonic's English is American only.
    #               Then Piper. The last two also keep every foreign accent
    #               pool between them, because an accent is made by
    #               substituting phonemes and Supertonic reads text.
    # kokoro     -- Kokoro only. Nothing is spoken where it has no voice.
    # supertonic -- Supertonic only, likewise. Thirty-one languages, ten
    #               speakers, and no accents.
    # piper      -- the older, faster, more obviously synthetic engine.
    # chatterbox -- the tier above all of them: a voice cloned from a
    #               reference clip in data/voices/chatterbox/prompts, wherever
    #               a clip matches the field's accent, and the auto order
    #               everywhere else. Costs a card, or patience: on a CPU a
    #               clearance takes about as long to render as to say.
    engine: str = "auto"
    # Whether ``auto`` may use the cloning tier when there is a card to run it
    # on. Off, because the card is drawing the simulator. Naming the engine
    # above is the other way in and does not need this.
    gpu: bool = False
    # Which Chatterbox model the cloning tier uses: standard (the default: it
    # stays on the text, losing 14% of its words to the recogniser where the
    # quick one loses 32%), turbo (twice as quick, and worth it on a card
    # where the read-back below catches what it drops), or multilingual (the
    # only one that speaks French, German and the rest).
    hq_model: str = "standard"
    # How many takes the cloning tier may try before it settles for the best
    # one. It samples, and a take can come back with a number mumbled, so
    # each take is read back by the recogniser and re-rolled if the words did
    # not survive. On a CPU every extra take costs about the length of the
    # transmission, and is only paid when the take before it failed.
    hq_takes: int = 2


@dataclass
class ProsodyConfig:
    """Where the controller breathes, and how fast they are talking.

    Punctuation and rate are the only two levers a synthesiser has, and every
    template in the phrasebook arrives at the engine as one comma-separated
    sentence -- so a three-word acknowledgement and a full route clearance are
    read in the same shape at the same speed. This shapes them.

    Mirrors :class:`wilcoatc.audio.prosody.Prosody` field for field. Set
    ``enabled: false`` for the A/B: nothing else changes, so the difference is
    the layer.
    """

    enabled: bool = True
    # Give the callsign -- and the station callup, when the transmission opens
    # with one -- its own falling sentence.
    address: bool = True
    # Give the operative clause, the one the transmission exists to say, its
    # own falling sentence.
    closing: bool = True
    min_clauses: int = 2
    min_body_clauses: int = 2
    min_closing_words: int = 2
    # Read short transmissions quicker and long ones more deliberately.
    pace: bool = True
    short_words: int = 7
    long_words: int = 28
    # Multipliers on the voice's length scale, where larger is slower.
    quick: float = 0.94
    measured: float = 1.07
    jitter: float = 0.025


@dataclass
class ImperfectionsConfig:
    """The operator, as opposed to the radio.

    The radio itself is ``audio.radio_effects`` and the chain behind it, which
    is measured and is not adjustable from here. This is the person holding
    the transmit bar: how long after keying up they start talking, how often
    they let go a syllable early, how often they say "uh", where they breathe,
    what the weather is doing behind them, and how much one transmission
    differs from the next.

    Mirrors :class:`wilcoatc.audio.imperfections.Imperfections`. Every
    probability is low on purpose; ``enabled: false`` turns the lot off and
    leaves the chain running exactly as it did before the layer existed.
    """

    enabled: bool = True
    # Silence between the carrier coming up and the first word, on top of what
    # the radio profile already puts there.
    lead_in_ms: float = 60.0
    lead_in_jitter_ms: float = 55.0
    # Letting go of the bar on the last syllable. Ignored for the profiles
    # that are recordings rather than people.
    clip_chance: float = 0.18
    clip_ms: float = 45.0
    # "Uh" at the head of a transmission -- they keyed up before they were
    # ready -- and "uh" at a comma part way through, where they lost their
    # place. Both stay rare: this is the most noticeable setting here.
    hesitation_chance: float = 0.05
    mid_hesitation_chance: float = 0.06
    # An audible inhale in a pause the synthesiser already left. The chance is
    # per pause, not per transmission: "roger" has nowhere to breathe and a
    # route clearance has two or three places.
    breath_chance: float = 0.5
    breath_db: float = -26.0
    breath_max: int = 2
    breath_pause_ms: float = 170.0
    # Weather behind the controller, under the voice's own peak and gusting.
    # -90 turns it off; the profiles that are recordings never get it at all.
    wind_db: float = -46.0
    wind_gust: float = 0.6
    # The voice itself drifting, the way a person's does over a clause: pitch
    # either side of where the engine put it, as a percentage, and level in
    # dB, both at well under one hertz. Zero turns either off.
    wander_pitch: float = 1.5
    wander_level_db: float = 1.2
    wander_hz: float = 0.7
    # Multiplier on the profile's own level spread, and how far the station's
    # key-up character wanders per transmission on a -1 to 1 scale.
    level: float = 1.0
    carrier: float = 0.25


@dataclass
class ToneConfig:
    """Where the receiver puts its energy.

    A handful of the radio profile's own numbers, restated so the balance can
    be matched to a recording without the measured chain being edited. The
    defaults were measured against Heathrow Director: they take the
    transmitter's presence lift off, tilt the passband darker, move the
    cockpit speaker's resonance down to where the recordings peak, and narrow
    the band to the Annex 10 channel the recordings actually are. Mirrors
    :class:`wilcoatc.audio.tone.Tone`, field for field -- see that module for
    the measurement and for what was tried and left out; ``enabled: false``
    gives the chain's own, brighter balance back exactly.
    """

    enabled: bool = True
    tilt_db: float = -6.0
    presence_db: float = 0.0
    speaker_db: float = 4.0
    speaker_hz: float = 900.0
    band_low: float = 330.0
    band_high: float = 2700.0
    noise_db: float = -8.0
    room_db: float = -10.0


@dataclass
class DeliveryConfig:
    """How a transmission is said, as distinct from what it says.

    Nothing in here can change a word of phraseology -- that comes from the
    templates and only from the templates. Each part is separately switchable
    so that a flat delivery, a clean radio and a bright receiver can be told
    apart.
    """

    prosody: ProsodyConfig = field(default_factory=ProsodyConfig)
    imperfections: ImperfectionsConfig = field(
        default_factory=ImperfectionsConfig)
    tone: ToneConfig = field(default_factory=ToneConfig)


@dataclass
class AiConfig:
    """The optional AI layer that understands non-standard phraseology.

    It only ever classifies what the pilot said. Controller speech always comes
    from the phraseology templates, whatever this is set to.
    """

    # none | ollama | anthropic
    provider: str = "none"
    # Ollama: a small instruction-tuned model is plenty. Claude: claude-opus-5.
    model: str = ""
    host: str = "http://127.0.0.1:11434"
    api_key: str = ""
    timeout_s: float = 12.0
    # Below this confidence the answer is discarded and the controller asks the
    # pilot to say again, which is safer than acting on a guess.
    min_confidence: float = 0.55


@dataclass
class AtcConfig:
    # "auto" follows the region of the facility being spoken to.
    dialect: str = "auto"           # auto | faa | icao
    strict_icao_digits: bool = False
    # Hand the synthesiser figures rather than the words a controller says:
    # "runway 30" instead of "runway three zero", "118.310" instead of "one
    # one eight decimal three one". What comes out is then whatever the voice
    # makes of a number, which is a natural reading rather than a radio one --
    # "thirty" for the runway, "one hundred eighteen point three one zero" for
    # the frequency. It is not the phraseology, and it is off by default for
    # that reason; it is here because reading figures is what some people
    # want to hear and it costs one line to offer.
    spoken_numerals: bool = False
    # Volunteer an ATIS reminder and a handoff when the pilot forgets.
    proactive_handoff: bool = True
    handoff_grace_s: float = 25.0
    # Controllers stop using the full callsign after two-way contact.
    abbreviate_callsigns: bool = True
    # The published frequency data is thin at some large airports. When you
    # tune a frequency nobody is listed on while you are at a field that does
    # staff positions, answer on it anyway rather than leaving you talking to
    # yourself. The controller says that it assumed, and what to do about it.
    answer_unpublished: bool = True
    # How close to the field you have to be for that to apply. A frequency you
    # dial in from fifty miles out is a mistake, not a gap in the data.
    unpublished_radius_nm: float = 6.0
    # Whether anybody works the Ground position. Off, a Ground frequency is an
    # empty room and a pushback request is not answered on any frequency: the
    # pushback is the pilot's own business with the ground handling. Taxi is
    # then worked by the tower (or delivery), exactly as at a field that
    # publishes no Ground, and nobody is ever handed to a Ground frequency.
    ground_control: bool = False


@dataclass
class SimBriefConfig:
    """The flight plan the pilot has already built.

    Read-only, and only when asked. Setting a username lets the flight be
    filled in from the latest plan instead of being typed twice.
    """

    # A SimBrief username, or the numeric pilot ID; both work.
    username: str = ""
    # Fetch the latest plan when the engine starts.
    load_on_start: bool = False


@dataclass
class GsxConfig:
    """What the ground handling is doing, if anything is telling us.

    The controller uses it for two things: not clearing an aeroplane to taxi
    while it is still attached to a tug, a jetway or a fuel truck, and calling
    the tug itself once the push has been approved.
    """

    enabled: bool = True
    # "auto" tries the file first if one is named, then MobiFlight.
    # "file", "mobiflight" and "off" pin it.
    bridge: str = "auto"
    # A JSON file of local variable names to numbers, written by whatever you
    # already run. Leave empty to skip it.
    lvar_file: str = ""
    # Whether an approved push-back actually starts one -- through GSX's own
    # menu where that can be reached, and otherwise through the simulator's
    # pushback. Separate from ``enabled`` because they are different
    # questions: a controller that can see the ramp is not necessarily one
    # you want reaching into it.
    control: bool = True
    # Whether the pilot can ask the ground crew for a service out loud --
    # "ground, request pushback", "ground crew, start the boarding". It needs
    # ``control`` as well, because asking is a write into GSX's menu; this is
    # the narrower switch, for somebody who wants the controller's approval
    # to reach the tug and does not want their own voice to.
    voice: bool = True
    # What this program calls itself on the MobiFlight channel. Each client
    # gets its own set of data areas, so two programs reading local variables
    # do not overwrite each other's list of them -- which is what two clients
    # on the default channel do, and it looks like variables that read zero
    # for no reason at all.
    client: str = "WilcoATC"


@dataclass
class UiConfig:
    """The panel's own language, which is about the reader rather than the flight.

    Nothing here touches what is spoken on the radio: that follows the
    aeroplane and is not a preference. This is the menu, the labels and the
    switches. ``auto`` takes the language from the operating system.
    """

    language: str = "auto"
    # Whether the transcript writes numbers as figures -- runway 31, heading
    # 240, QNH 1013 -- which is how a pilot writes them on a strip. Set false
    # to read the transcript in the words that were actually spoken. Either
    # way it changes nothing that is said: the synthesiser is given the spoken
    # form and never sees this.
    numerals: bool = True
    # The card that comes up a few seconds after touchdown: the landing rate,
    # how the approach and the landing went, and the flight against its
    # SimBrief schedule. The measuring happens either way; this is whether
    # the panel puts it in front of you.
    landing_report: bool = True


@dataclass
class ToolbarConfig:
    """The panel inside the simulator's own toolbar.

    The window on this machine is served on a port the operating system picks,
    because nothing else needs to find it. The toolbar panel does need to find
    it, and it runs inside the simulator where it cannot be told a number that
    changes every launch -- so it gets a fixed one.

    Loopback only, like everything else here. A port already in use is not an
    error: the toolbar panel goes without and the window carries on.
    """

    enabled: bool = True
    port: int = 8787


@dataclass
class TrafficConfig:
    """The other aeroplanes on the frequency, and out of the window.

    Two sources, and they add up rather than compete. The aeroplanes somebody
    else has injected -- FSLTL, AIG, the simulator's own live traffic -- are
    read out of the simulator and worked as they are found. The rest are
    generated here, in whichever language their crew speaks at this field, and
    created in the simulator as AI aircraft -- and only once the simulator has
    made one does it exist on the radio, on the map or on the runway. Nothing
    is heard that cannot be seen out of the window.
    """

    enabled: bool = True
    # Whether the aeroplanes somebody else has put in the simulator are worked
    # as well. FSLTL, AIG, the simulator's own live traffic: with this on they
    # are on the strips, in the landing sequence and on the frequency, and the
    # invented traffic fills in around them rather than on top of them.
    from_simulator: bool = True
    # Whether traffic is generated at all, as aircraft in the simulator.
    # Needs a live SimConnect session, and off -- or with no simulator -- only
    # what somebody else put in the simulator is worked: an invented aeroplane
    # that is not drawn is not invented.
    in_simulator: bool = True
    # How many are drawn at once, and so how many are invented. Each is a
    # real aeroplane in the simulator, so a busy field is capped rather than
    # allowed to fill the ramp.
    in_simulator_limit: int = 12
    # How many aeroplanes stand at the gates on top of that, going nowhere
    # until one of them becomes a departure. Only at a field whose stands the
    # simulator has given us, never on every stand (the moving traffic has
    # to have somewhere to park), and 0 is none. They are drawn after the
    # moving traffic, so they never cost it a place.
    parked: int = 12
    # FSLTL's model matching table, for the airlines' own liveries. Empty
    # finds it in the Community folder the simulator records; a path names
    # the file for an installation that is somewhere else.
    fsltl_rules: str = ""
    # Roughly how many aircraft are worked at once, as a multiple of what the
    # airport's size would normally carry. 0 is silence, 2 is a busy morning.
    density: float = 1.0
    # Fixing the seed makes a session repeatable, which is only useful for
    # testing; left at 0 the traffic differs every time.
    seed: int = 0
    # How the generated traffic identifies itself on the radio.
    #
    # "auto" is what the field would actually carry: airline callsigns at an
    # airline field, registrations at a small one. "registration" gives every
    # generated aeroplane a registration of the shape used where the airport
    # is -- G-HLYM in Britain, F-GXYZ in France -- so the frequency is full of
    # "Golf Hotel Lima Yankee Mike" rather than Speedbird and Beeline.
    #
    # Worth being plain about: at Heathrow the second one is less true to
    # life, not more, because nearly every movement there really is an
    # airline. It is a setting rather than the default for that reason, and
    # it is a setting at all because a frequency of spelled registrations is
    # much easier to follow than a frequency of telephony names you have to
    # know.
    callsigns: str = "auto"        # auto | registration


@dataclass
class WeatherConfig:
    online: bool = True
    prefer_sim: bool = True
    refresh_s: float = 600.0
    station_override: str = ""


@dataclass
class SimConfig:
    enabled: bool = True
    poll_interval_s: float = 0.25
    reconnect_interval_s: float = 10.0
    # Take each airport's frequency list from the simulator rather than from
    # the published dataset. The dataset is thin -- Charles de Gaulle carries
    # nine frequencies in it and thirty in the simulator -- and the simulator
    # is the thing you actually have to talk to.
    frequencies_from_sim: bool = True
    # Reading them needs a SimConnect library new enough to answer facility
    # questions; the usual places are searched. Name one here if yours lives
    # somewhere else.
    simconnect_dll: str = ""


@dataclass
class FlightConfig:
    """Details the sim cannot tell us, which the pilot supplies."""

    callsign: str = ""
    aircraft_type: str = ""
    departure: str = ""
    destination: str = ""
    cruise_altitude_ft: int = 0
    ifr: bool = True


@dataclass
class NetworkConfig:
    """VATSIM or IVAO: who is really on the frequency.

    This program exists to put a controller on frequencies that have nobody on
    them. Where a network is set, it asks first -- and if a human is working
    the frequency the aeroplane is tuned to, it says nothing at all.

    Both feeds are public and anonymous. Nothing is sent to them: the poll is
    a plain GET of the whole online list, and no callsign, position or flight
    of yours goes anywhere.
    """

    # none | vatsim | ivao
    provider: str = "none"
    # How often the online list is refreshed. Long on purpose: it is a picture
    # of who is logged on, which changes on the scale of minutes.
    refresh_s: float = 120.0
    # Read the online controller's own ATIS for a field rather than generating
    # one. Theirs is the real one -- it is the runway they are actually using.
    use_online_atis: bool = True


@dataclass
class ImmersionConfig:
    """The rest of the aeroplane: the other seat, and the cabin behind you.

    Everything here is somebody who is not a controller. The radio is
    untouched by all of it -- a controller says what a controller says
    whatever this is set to -- and the point of keeping them apart is that
    this is the half you are allowed to turn off without changing what
    flying the aeroplane is like.

    It is off by default. A pilot who wants a first officer will go and ask
    for one; a pilot who did not ask should not have somebody start talking
    to them on a flight they set up yesterday.
    """

    enabled: bool = False

    # --- the other seat ---
    copilot: bool = True
    # "auto" casts a voice from the aircraft registration, so the same
    # aeroplane keeps the same first officer. A voice key pins one.
    copilot_voice: str = "auto"
    copilot_gender: str = "auto"        # auto | m | f
    # The standard callouts: eighty knots, V1, rotate, positive rate, the
    # thousand-foot calls on the way down, and the rollout calls.
    callouts: bool = True
    # Reads what the controller just said back to you off the radio, the way
    # the pilot monitoring does. Not a readback: a confirmation of the
    # numbers, on the intercom.
    confirmations: bool = True
    # Checklists, called and answered.
    checklists: bool = True
    # The first officer actually working the radio: if a clearance has gone
    # unanswered for a few seconds, they read it back for you. Off by
    # default, because a pilot practising radio work does not want the
    # aeroplane doing it for them.
    radio_readbacks: bool = False

    # --- the cabin ---
    cabin: bool = True
    # The cabin manager, who makes the announcements. The crew member who
    # answers them is cast separately, so the two are never one person.
    cabin_voice: str = "auto"
    cabin_gender: str = "auto"
    # The safety demonstration, and the crew calls around it.
    briefings: bool = True
    # The captain's and the purser's announcements: welcome aboard, the
    # cruise, the descent, and where you have arrived.
    announcements: bool = True
    # The trolley: drinks after the climb, a second run before the descent.
    service: bool = True
    # The two-tone chime, and the seatbelt sign it goes with.
    chimes: bool = True
    # The bed that plays while people are getting on, from the moment the
    # engines are running until the brakes come off for the push. Its own
    # switch rather than part of "announcements", because it is the one thing
    # in the cabin that is not somebody speaking and the two are liked by
    # different people: a pilot who wants the purser may well not want music
    # under their departure briefing.
    boarding_music: bool = True
    # The cabin clapping for a touchdown gentler than 200 feet a minute. Not
    # somebody speaking either, and a thing some people find charming and
    # some find insufferable, so it has its own switch for the same reason
    # the music does.
    applause: bool = True
    # Whether the crew sound like people on an aircraft's audio system rather
    # than like a file being played: a little of the interphone and the cabin
    # speaker on the voice, and the odd "uh" or "um" where somebody would
    # actually stall. Callouts are never hesitant either way. See
    # wilcoatc/audio/aboard.py.
    realism: bool = True
    # A crew whose own language is not English repeat each cabin announcement
    # in English afterwards, the way a French or German airline's crew do.
    # The flight deck is not repeated: the interphone is two people who
    # already share a language.
    bilingual: bool = True

    # --- recorded announcements ---
    # Real recordings beat a synthesised purser, and there are a lot of packs
    # about already because the Fenix A320 has had this for years. Theirs is
    # the layout and the naming this reads, so a pack drops straight in.
    #
    # Named "recordings" rather than "announcements" because the field above
    # is already called that and means something else: whether the cabin
    # makes its announcements at all. Two settings with one name is one
    # setting, and the switch for the trolley was silently the switch for
    # the sound packs.
    recordings: bool = True
    # Where to look. Empty means the ``announcements`` folder beside the
    # program, which is created on first run. Point it at an existing pack --
    # or at a whole community folder full of them -- and nothing has to move.
    recordings_dir: str = ""

    # --- how it sounds ---
    # Relative to the radio, which stays where the audio settings put it. The
    # cabin is behind you and should not be louder than the controller.
    volume: float = 0.8
    # The language the crew speak. "auto" works it out from the pinned
    # voices, then the airline, then the field the flight starts at; a
    # language code ("fr", "de") decides it outright, and a pinned voice that
    # cannot speak it is replaced by one of the same sex that can.
    language: str = "auto"
    # What the crew calls the airline. Empty derives it from the callsign,
    # which is right nearly always and is at least never wrong on purpose.
    airline: str = ""
    # Who is flying it. Empty casts a surname from the registration, so the
    # same aeroplane keeps the same captain; a pilot who would rather be
    # welcomed aboard by their own name puts it here and the cabin uses it
    # instead. Written as it is to be said -- "Sarah Whitfield" is read out
    # whole, and so is "Whitfield" on its own.
    captain_name: str = ""
    # Whether the cabin answers when it is spoken to. The pilot addresses the
    # crew rather than the controller -- "cabin crew, could I get a coffee" --
    # and none of it reaches the frequency. Off, the same words go to the
    # radio and a controller makes what it can of them.
    interphone: bool = True
    # How much of it there is. "quiet" is the callouts and nothing else;
    # "chatty" adds the small talk a long cruise actually has in it.
    verbosity: str = "normal"          # quiet | normal | chatty


@dataclass
class LoggingConfig:
    """Where the logs go, how much of them there is, and how long they last.

    Four streams are written side by side: the ordinary application log, the
    same records as JSON lines for anything that has to be queried rather than
    read, the radio transcript -- every transmission with the parser's reading
    of it, which is what answers "why did it say that" -- and the crew log,
    which answers the harder question of why the cabin said nothing.
    """

    # What reaches the files. The console is separate and quieter, because a
    # terminal scrolling past at DEBUG is not a log, it is noise.
    level: str = "INFO"
    console_level: str = "WARNING"
    # Empty means the standard place beside the program; set it to put the
    # logs on another drive.
    directory: str = ""
    # Rotation, so a long session cannot fill a disk.
    max_bytes: int = 4_000_000
    backups: int = 5
    # The JSON-lines copy of the application log.
    structured: bool = True
    # The radio transcript, with the intent, the confidence and the numbers
    # the parser pulled out of each transmission.
    radio: bool = True
    # Why the cabin said what it said, and why it did not say the rest:
    # every cue, every line, and the switch or the condition that stopped
    # each one. Kept on by default, because the report it answers -- "I
    # cannot hear the first officer" -- is otherwise unanswerable.
    crew: bool = True
    # Anything in the log folder older than this is deleted at startup. Zero
    # keeps everything.
    keep_days: int = 14


@dataclass
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    ptt: PttConfig = field(default_factory=PttConfig)
    speech: SpeechConfig = field(default_factory=SpeechConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    atc: AtcConfig = field(default_factory=AtcConfig)
    ai: AiConfig = field(default_factory=AiConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    toolbar: ToolbarConfig = field(default_factory=ToolbarConfig)
    simbrief: SimBriefConfig = field(default_factory=SimBriefConfig)
    gsx: GsxConfig = field(default_factory=GsxConfig)
    traffic: TrafficConfig = field(default_factory=TrafficConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    sim: SimConfig = field(default_factory=SimConfig)
    immersion: ImmersionConfig = field(default_factory=ImmersionConfig)
    flight: FlightConfig = field(default_factory=FlightConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    # Kept for the config files that already say it. The logging section is
    # where the level lives now, and takes precedence when it is set.
    log_level: str = "INFO"

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        path = Path(path) if path else CONFIG_PATH
        config = cls()
        if not path.exists():
            return config
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        _merge(config, raw)
        return config

    def save(self, path: Path | str | None = None) -> Path:
        path = Path(path) if path else CONFIG_PATH
        path.write_text(
            yaml.safe_dump(asdict(self), sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
        return path


def _merge(target: Any, values: dict) -> None:
    """Apply a nested dict onto a dataclass, ignoring unknown keys."""
    known = {f.name: f for f in fields(target)}
    for key, value in (values or {}).items():
        if key not in known:
            continue
        current = getattr(target, key)
        if is_dataclass(current) and isinstance(value, dict):
            _merge(current, value)
        else:
            setattr(target, key, value)
