"""Configuration loading, checking and saving.

Defaults are chosen so the system runs correctly with no config file at all.
``config.yaml`` in the project root overrides any subset of them.

Every field also carries a :class:`Setting`, declared beside its dataclass:
its kind, its range and its choices, whether a change needs a restart.
Everything the panel writes is checked against it (:func:`check`), so a
value out of range is refused with the reason rather than written -- the
panel's own hand-kept table of thirty settings used to drop anything not in
it without a word. ``tests/test_config_file.py`` fails for a field that is
not described.

It is written with ruamel.yaml's round trip, so a pilot who does open the
file keeps their comments, their order and their own keys.
"""

from __future__ import annotations

import io
import math
import re
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML as RoundTripYAML
from ruamel.yaml.comments import CommentedMap

from .paths import bundle_root, config_path

ROOT = bundle_root()
CONFIG_PATH = config_path()


# --------------------------------------------------------------------------
# what each setting is
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Setting:
    """How one field is checked.

    ``kind`` is derived from the default when it is left empty: a bool is a
    toggle, a number a number, a string text, a list a list, a dict a map. A
    ``widget`` is a control the panel draws itself (a device list, the key
    capture, the voice picker); it changes the control, not the checking.

    ``advanced`` keeps a field in its tab under "more"; ``developer`` puts it
    in the Advanced tab behind the warning; ``measured`` is a figure fitted
    to a recording, read-only, whose only other value is its measured one.
    ``restart`` is a change the running program cannot pick up.
    """

    tab: str = "advanced"
    group: str = ""
    kind: str = ""
    choices: tuple = ()
    # The catalogue keys for the choices, where they already have words.
    choice_keys: tuple = ()
    min: float | None = None
    max: float | None = None
    step: float | None = None
    unit: str = ""
    # Shown multiplied by this: a volume of 0.8 is "80 %".
    scale: float = 1.0
    restart: bool = False
    advanced: bool = False
    developer: bool = False
    readonly: bool = False
    measured: bool = False
    # State the program keeps (the tour has been shown), not a choice.
    hidden: bool = False
    widget: str = ""
    # For a level shown as a switch: the value that means "off".
    off: Any = None
    # Shown only while another setting has this value: "ptt.mode=keyboard".
    when: str = ""
    label: str = ""
    help: str = ""


def measured(group: str, **more) -> Setting:
    """A figure measured or fitted against a recording (see CLAUDE.md)."""
    return Setting(tab="advanced", group=group, readonly=True, measured=True,
                   developer=True, **more)


def describe(cls, **settings: Setting):
    """Hang each field's Setting on its dataclass."""
    known = {f.name for f in fields(cls)}
    unknown = set(settings) - known
    if unknown:
        raise TypeError(f"{cls.__name__} has no field {sorted(unknown)}")
    cls.__settings__ = settings
    return cls


def describe_field(path: str, default: Any,
                   setting: Setting | None = None) -> Setting:
    """The Setting for a field, its kind derived from the default.

    A field nobody described still renders, with a generic label, so a new
    one is never invisible; the test names it.
    """
    setting = setting or Setting()
    if setting.kind:
        return setting
    if setting.choices:
        kind = "choice"
    elif isinstance(default, bool):
        kind = "toggle"
    elif isinstance(default, (int, float)):
        kind = "number"
    elif isinstance(default, list):
        kind = "list"
    elif isinstance(default, dict):
        kind = "map"
    else:
        kind = "text"
    return Setting(**{**_fields_of(setting), "kind": kind})


def _fields_of(setting: Setting) -> dict:
    return {f.name: getattr(setting, f.name) for f in fields(setting)}


class SettingRefused(ValueError):
    """A value the screen may not write, with the catalogue key that says
    why and the numbers that go in it."""

    def __init__(self, key: str, text: str, **params):
        super().__init__(text.format(**params) if params else text)
        self.key = key
        self.params = params


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


describe(
    AudioConfig,
    input_device=Setting("radio", "audio", widget="input_device",
                         restart=True, label="set.microphone"),
    output_device=Setting("radio", "audio", widget="output_device",
                          restart=True, label="set.output"),
    volume=Setting("radio", "audio", min=0, max=1.5, step=0.05, unit="%",
                   scale=100, widget="slider", label="set.volume"),
    capture_rate=Setting("advanced", "internals", min=8000, max=48000,
                         unit="Hz", restart=True, developer=True),
    max_transmission_s=Setting("radio", "audio", min=5, max=120, unit="s",
                               restart=True, advanced=True),
    radio_effects=Setting("radio", "audio", advanced=True,
                          label="set.radio_effects",
                          help="set.radio_effects_why"),
)


@dataclass
class PttConfig:
    mode: str = "keyboard"          # keyboard | joystick | none
    # The key by its name, as it was pressed -- "$" on an AZERTY keyboard --
    # for showing, and for a file written before the scan code was kept.
    key: str = "grave"
    # The same key by where it is on the keyboard (a Windows scan code),
    # which is what is matched when it is set: a name is whatever the
    # layout prints, and the layout can change under a running program.
    # 0 is none, and the name is matched instead.
    scan_code: int = 0
    # The controller by vendor and product ("294b:1900"), as read out of
    # SDL's GUID, and by name behind that: its index in SDL's list moved
    # whenever another device was plugged in ahead of it, and the button
    # that keyed the radio was then on the throttle. The index is kept for
    # a file written before these existed (audio/controllers.py).
    joystick_id: str = ""
    joystick_name: str = ""
    joystick_index: int = 0
    joystick_button: int = 0


describe(
    PttConfig,
    mode=Setting("radio", "ptt", choices=("keyboard", "joystick", "none"),
                 choice_keys=("set.keyboard", "set.joystick", "set.off"),
                 label="set.ptt"),
    key=Setting("radio", "ptt", kind="key", widget="key",
                when="ptt.mode=keyboard", label="set.key", help="set.ptt_why"),
    scan_code=Setting("radio", "ptt", min=0, max=1023, hidden=True),
    joystick_id=Setting("radio", "ptt", widget="joystick",
                        when="ptt.mode=joystick", label="set.controller"),
    joystick_name=Setting("radio", "ptt", hidden=True),
    joystick_index=Setting("radio", "ptt", min=0, max=31, hidden=True),
    joystick_button=Setting("radio", "ptt", widget="joystick_button", min=0,
                            max=127, when="ptt.mode=joystick",
                            label="set.button", help="set.button_why"),
)


@dataclass
class SpeechConfig:
    # Read as its multilingual twin whatever is written here (audio/stt.py
    # engine_model): the pilot may speak any language the radio is worked in.
    model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = 5
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
    # Below this confidence a readback item heard nearly -- a SID's name
    # close to the one given, a frequency, squawk, altitude or runway one
    # figure off -- is asked about ("confirm ...") rather than refused
    # ("negative ..."); above it, the recogniser heard what was said. Well
    # above min_confidence, since one name or figure can be doubtful in a
    # transcript that is not. UNVERIFIED: not measured on the corpus; a
    # value to revisit with flight logs. 0 turns the question off.
    confirm_below: float = 0.6


#: The recogniser models the screen offers (audio/stt.py MODEL_CHOICES).
SPEECH_MODELS = ("tiny.en", "base.en", "small.en", "small", "medium",
                 "large-v3")

describe(
    SpeechConfig,
    model=Setting("radio", "recogniser", choices=SPEECH_MODELS, restart=True),
    device=Setting("radio", "recogniser", choices=("cpu", "cuda", "auto"),
                   restart=True, advanced=True),
    compute_type=Setting("radio", "recogniser",
                         choices=("int8", "int8_float16", "float16",
                                  "float32"),
                         restart=True, advanced=True),
    beam_size=Setting("radio", "recogniser", min=1, max=10, restart=True,
                      advanced=True),
    languages=Setting("radio", "recogniser", widget="languages",
                      restart=True, advanced=True),
    detection_threshold=Setting("radio", "recogniser", min=0, max=1,
                                step=0.05, restart=True, advanced=True),
    min_confidence=Setting("radio", "recogniser", min=0, max=1, step=0.05,
                           advanced=True),
    confirm_below=Setting("radio", "recogniser", min=0, max=1, step=0.05,
                          advanced=True),
)


@dataclass
class VoiceConfig:
    voice_dir: str = "data/voices"
    seed: str = "wilcoatc"
    cache_size: int = 512
    preload: bool = False
    # Which synthesiser speaks.
    #
    # auto       -- Supertonic if it is installed: ten speakers of each sex in
    #               every controller language, and American English. Then
    #               Piper: British English, French, and every country's
    #               accent, because an accent is made by substituting
    #               phonemes and Supertonic reads text.
    # kokoro     -- no longer cast; read as auto. Its module stays for one
    #               release.
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
    # Which Chatterbox model the cloning tier uses -- on the panel, the
    # performance profile: turbo is "Normal" and the default (about the time
    # it takes to say a clearance on a CPU, where standard takes twice that;
    # the read-back below catches most of what it drops), standard is "High"
    # (it stays on the text, losing 14% of its words to the recogniser where
    # the quick one loses 32%), multilingual (the
    # only one that speaks French, German and the rest), or flash (block
    # diffusion: measured three times quicker than standard on a CPU for the
    # same words lost -- a twenty-word clearance in 1.35x the time it takes
    # to say, against 4.1x -- English only, 3.2 GB).
    hq_model: str = "turbo"
    # How many takes the cloning tier may try before it settles for the best
    # one. It samples, and a take can come back with a number mumbled, so
    # each take is read back by the recogniser and re-rolled if the words did
    # not survive. On a CPU every extra take costs about the length of the
    # transmission, and is only paid when the take before it failed.
    hq_takes: int = 2


describe(
    VoiceConfig,
    # "kokoro" is still read, as auto, and is not offered.
    engine=Setting("voices", "voices",
                   choices=("auto", "supertonic", "piper", "chatterbox"),
                   restart=True),
    gpu=Setting("voices", "voices", restart=True),
    # Not a restart: the engine loads the other model in place
    # (web/server._apply_settings), once it is on disk.
    hq_model=Setting("voices", "voices",
                     choices=("standard", "turbo", "multilingual", "flash"),
                     when="voice.engine=chatterbox"),
    hq_takes=Setting("voices", "voices", min=1, max=6, restart=True,
                     when="voice.engine=chatterbox"),
    seed=Setting("voices", "voices", restart=True, advanced=True),
    preload=Setting("voices", "voices", advanced=True),
    voice_dir=Setting("advanced", "internals", kind="path", restart=True,
                      developer=True),
    cache_size=Setting("advanced", "internals", min=0, max=10000,
                       restart=True, developer=True),
)


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


describe(
    ProsodyConfig,
    enabled=Setting("voices", "delivery", advanced=True),
    **{name: measured("prosody") for name in (
        "address", "closing", "min_clauses", "min_body_clauses",
        "min_closing_words", "pace", "short_words", "long_words", "quick",
        "measured", "jitter")},
)


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
    # ready -- and "uh" part way through, at a comma or in front of a number,
    # where they lost their place. Both stay rare: this is the most
    # noticeable setting here.
    hesitation_chance: float = 0.05
    mid_hesitation_chance: float = 0.06
    # How long the voice may stop after an "uh" part way through, in ms,
    # before the word it was looking for. Each one draws up to this; 650 is
    # what a recorded clearance exchange measured. 0 runs straight on.
    stall_ms: float = 650.0
    # Each phrase on its own note and at its own pace (audio/intonation.py):
    # the spread of the phrases' pitch in semitones, how far the callsign is
    # lifted, the spread of their pace, and the instructions' pace against
    # the engine's. All zero and 1.0 puts it back as the engine said it.
    phrase_pitch_st: float = 1.3
    address_lift_st: float = 3.5
    phrase_rate: float = 0.07
    instruction_pace: float = 1.04
    # Stopping for air in a long transmission: after about this many seconds
    # of speech a pause is lengthened and an inhale goes in it, at this level
    # under the speech. 0 seconds turns it off.
    breath_group_s: float = 5.5
    catch_breath_db: float = -23.0
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


describe(
    ImperfectionsConfig,
    enabled=Setting("voices", "operator", label="set.operator",
                    help="set.operator_why"),
    # Levels, shown as switches: on is the measured level, off is silence.
    breath_db=Setting("voices", "operator", widget="switch_value", off=-90.0,
                      min=-90, max=-10, unit="dB", label="set.breathing",
                      help="set.breathing_why"),
    wind_db=Setting("voices", "operator", widget="switch_value", off=-90.0,
                    min=-90, max=-10, unit="dB", label="set.wind",
                    help="set.wind_why"),
    hesitation_chance=Setting("voices", "operator", widget="switch_value",
                              off=0.0, min=0, max=0.5, label="set.hesitation",
                              help="set.hesitation_why"),
    mid_hesitation_chance=Setting("voices", "operator", widget="switch_value",
                                  off=0.0, min=0, max=0.5),
    **{name: measured("imperfections") for name in (
        "lead_in_ms", "lead_in_jitter_ms", "clip_chance", "clip_ms",
        "stall_ms", "phrase_pitch_st", "address_lift_st", "phrase_rate",
        "instruction_pace", "breath_group_s", "catch_breath_db",
        "breath_chance", "breath_max", "breath_pause_ms", "wind_gust",
        "wander_pitch", "wander_level_db", "wander_hz", "level", "carrier")},
)


@dataclass
class ToneConfig:
    """Where the receiver puts its energy.

    A handful of the radio profile's own numbers, restated so the balance can
    be matched to a recording without the measured chain being edited. The
    defaults were measured against the controllers in a reference flight
    video: the presence lift off, the passband tilted darker, the cockpit
    speaker's resonance at 1.9 kHz, the band 400-2700 Hz, the floor 20 dB
    under the voice and a quiet key-up. The earlier fit, to Heathrow
    Director, is ``tone.HEATHROW``. Mirrors
    :class:`wilcoatc.audio.tone.Tone`, field for field -- see that module for
    the measurement and for what was tried and left out; ``enabled: false``
    gives the chain's own, brighter balance back exactly.
    """

    enabled: bool = True
    tilt_db: float = -6.0
    presence_db: float = 0.0
    speaker_db: float = 6.0
    speaker_hz: float = 1900.0
    band_low: float = 400.0
    band_high: float = 2700.0
    noise_db: float = 0.0
    room_db: float = -10.0
    click_db: float = -7.0
    rush_db: float = -5.0
    tail_burst_db: float = -11.0
    tail_ms: float = 0.0


describe(
    ToneConfig,
    enabled=Setting("voices", "delivery", advanced=True),
    **{name: measured("tone") for name in (
        "tilt_db", "presence_db", "speaker_db", "speaker_hz", "band_low",
        "band_high", "noise_db", "room_db", "click_db", "rush_db",
        "tail_burst_db", "tail_ms")},
)


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


describe(
    AiConfig,
    provider=Setting("understanding", "understanding",
                     choices=("none", "ollama", "anthropic"), restart=True),
    model=Setting("understanding", "understanding", restart=True,
                  when="ai.provider!=none"),
    host=Setting("understanding", "understanding", restart=True,
                 advanced=True, when="ai.provider=ollama"),
    # Never sent back to the panel: shown as set or not set.
    api_key=Setting("understanding", "understanding", kind="secret",
                    restart=True, when="ai.provider=anthropic"),
    timeout_s=Setting("understanding", "understanding", min=1, max=120,
                      unit="s", restart=True, advanced=True),
    min_confidence=Setting("understanding", "understanding", min=0, max=1,
                           step=0.05, restart=True, advanced=True),
)


@dataclass
class AtcConfig:
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
    # On by default: it was off from the first commit, and a pilot calling
    # the Ground the chart lists -- Centennial on 121.800 -- got silence at
    # every field for a month without knowing there was a switch.
    ground_control: bool = True
    # Whether the radio needs the aeroplane's power (atc/radio_power.py): cold
    # and dark, nothing is heard and nothing goes out. Off, the radio works
    # whatever the switches say -- for an add-on whose own electrics the
    # simulator's variables do not follow and that is not yet listed.
    radio_needs_power: bool = True


describe(
    AtcConfig,
    strict_icao_digits=Setting("radio", "phraseology", restart=True,
                               label="set.strict_digits",
                               help="set.strict_digits_why"),
    spoken_numerals=Setting("radio", "phraseology", advanced=True),
    proactive_handoff=Setting("radio", "phraseology", label="set.handoffs",
                              help="set.handoffs_why"),
    handoff_grace_s=Setting("radio", "phraseology", min=0, max=300,
                            unit="s", advanced=True),
    answer_unpublished=Setting("radio", "phraseology", advanced=True),
    unpublished_radius_nm=Setting("radio", "phraseology", min=0, max=50,
                                  unit="nm", advanced=True),
    ground_control=Setting("radio", "phraseology", restart=True),
    radio_needs_power=Setting("radio", "phraseology", label="set.radio_power",
                              help="set.radio_power_why"),
)


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


describe(
    SimBriefConfig,
    username=Setting("flight", "simbrief", label="set.simbrief_user",
                     help="set.simbrief_why"),
    load_on_start=Setting("flight", "simbrief"),
)


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


describe(
    GsxConfig,
    enabled=Setting("traffic", "ground", label="set.ground_watch",
                    help="set.ground_why"),
    control=Setting("traffic", "ground", label="set.ground_call",
                    help="set.ground_call_why"),
    voice=Setting("traffic", "ground", label="set.ground_voice",
                  help="set.ground_voice_why"),
    bridge=Setting("traffic", "ground",
                   choices=("auto", "file", "mobiflight", "off"),
                   restart=True, advanced=True),
    lvar_file=Setting("traffic", "ground", kind="path", restart=True,
                      advanced=True, when="gsx.bridge!=mobiflight"),
    client=Setting("advanced", "internals", restart=True, developer=True),
)


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
    # How the window looks: Settings, Personalisation. Kept here rather than
    # in the browser's own storage because the desktop window runs in private
    # mode, which forgets everything a page stored the moment it closes.
    # ``theme`` is dark or light; ``accent`` a #rrggbb colour; ``scene`` the
    # colour of the light behind the glass (accent, dusk, night, aurora);
    # ``blur`` how frosted the glass is, in pixels; ``lights`` the slow
    # coloured glows drifting in the corners; ``grid`` the faint grid;
    # ``gradient`` whether what is filled with the accent is filled with a
    # gradient of it or with the flat colour.
    theme: str = "dark"
    accent: str = "#697f9b"
    scene: str = "accent"
    blur: int = 24
    lights: bool = True
    grid: bool = True
    gradient: bool = True
    # Whether the guided tour of the window has been shown -- finished or
    # skipped. It opens by itself on the first run, after the startup notice
    # and any download, and from Settings, Interface whenever asked for.
    tour_done: bool = False
    # Whether the first-run setup has been walked through: the downloads,
    # the microphone, the key and SimBrief, one screen each. It comes up by
    # itself before the tour until it is finished, and from Settings,
    # Interface whenever asked for.
    onboarding_done: bool = False
    # Which revision of the first-run setup was walked through (OB_VERSION in
    # app.js). A pilot who finished an older one is walked through the new
    # one once: otherwise a screen added later -- Appearance, First officer --
    # is never seen by anybody who installed before it existed.
    onboarding_version: int = 0
    # Whether the program asks GitHub at startup if a newer version is out
    # (wilcoatc/update.py): one small request, nothing downloaded until the
    # pilot says yes. ``skipped_version`` is the one they turned down.
    check_updates: bool = True
    skipped_version: str = ""


#: The looks the panel offers. Anything else is refused rather than stored,
#: because it ends up in the page's style as written.
THEMES = ("dark", "light")
SCENES = ("accent", "dusk", "night", "aurora")

describe(
    UiConfig,
    language=Setting("appearance", "interface", widget="ui_language",
                     label="set.ui_language", help="set.ui_language_why"),
    numerals=Setting("appearance", "interface"),
    landing_report=Setting("flight", "flight", label="set.landing_report",
                           help="set.landing_report_why"),
    # The look is drawn by its own card (swatches, previews) and checked
    # here like everything else.
    theme=Setting("appearance", "look", choices=THEMES, widget="look",
                  label="set.look_theme", help="set.look_theme_why"),
    accent=Setting("appearance", "look", kind="colour", widget="look",
                   label="set.look_accent", help="set.look_accent_why"),
    scene=Setting("appearance", "look", choices=SCENES, widget="look",
                  label="set.look_scene", help="set.look_scene_why"),
    blur=Setting("appearance", "look", min=0, max=48, step=2, unit="px",
                 widget="look", label="set.look_blur"),
    lights=Setting("appearance", "look", widget="look",
                   label="set.look_lights", help="set.look_lights_why"),
    grid=Setting("appearance", "look", widget="look", label="set.look_grid",
                 help="set.look_grid_why"),
    gradient=Setting("appearance", "look", widget="look",
                     label="set.look_gradient", help="set.look_gradient_why"),
    tour_done=Setting("appearance", "interface", hidden=True),
    onboarding_done=Setting("appearance", "interface", hidden=True),
    onboarding_version=Setting("appearance", "interface", hidden=True, min=0, max=1000),
    check_updates=Setting("appearance", "interface", label="update.check",
                          help="update.check_why"),
    skipped_version=Setting("appearance", "interface", hidden=True),
)


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


describe(
    ToolbarConfig,
    enabled=Setting("weather_data", "simulator", restart=True, advanced=True),
    port=Setting("weather_data", "simulator", min=1024, max=65535,
                 restart=True, advanced=True, when="toolbar.enabled=true"),
)


@dataclass
class LodConfig:
    """Which model each invented aeroplane is drawn with, by distance.

    Two kinds of model can draw the traffic. The simulator's own AI models
    have doors and holds that open and cost what a full aeroplane costs.
    FSLTL's, when FSLTL is installed, are painted in the airline's colours,
    light enough to have an apron full of, and have no door animation at
    all. So the aeroplanes near you are drawn with the first and the rest
    with the second, and an aeroplane standing on its stand is swapped from
    one to the other as you move -- new model first, old one removed once
    it has loaded, doors carried across. Nothing moving is ever swapped.
    The aeroplane is the same aeroplane throughout; only the drawing
    changes.
    """

    # Off, every aeroplane is drawn the way it always was: FSLTL's painted
    # model when FSLTL is installed, the simulator's own otherwise, and
    # nothing is ever swapped.
    enabled: bool = True
    # Nearer than this, detailed; further than light_m, light; in between,
    # whatever it already is, so the stands do not flicker as you taxi past.
    detailed_m: float = 1500.0
    light_m: float = 2500.0
    # How many may be detailed at once, the nearest first.
    limit: int = 6
    # How many swaps a second at most, because each is a model loading.
    swaps_per_s: float = 0.5
    # How long the new model is given to load before the old one goes.
    settle_s: float = 3.0
    # "distance": detailed by how far it is from you, as above. "schedule":
    # detailed only in its turnaround -- from block-in, or from a couple of
    # minutes before boarding, until its doors read shut for the push --
    # and only within detail_radius_km, under the same limit. Parked with
    # its doors shut, it is light however near.
    mode: str = "distance"
    detail_radius_km: float = 3.0
    # The simulator's own models are painted for a couple of dozen airlines
    # only. "match": detailed only in the airline's own livery. "any": in
    # the house livery where it has none, and then a swap that would change
    # the colours is only made with you further than hide_m from it.
    # Schedule mode only.
    livery: str = "match"
    hide_m: float = 1200.0
    # Schedule mode: a departure is booked, starts boarding this long
    # after (the minutes in which its model can change with the doors
    # shut), boards for boarding_s, and only then calls for its clearance.
    boarding_lead_s: float = 120.0
    boarding_s: float = 240.0
    # Models of your own, by type or "AIRLINE/TYPE", each with a detailed
    # and a light title, for example
    #   models: {A20N: {detailed: "Asobo PassiveAircraft A320 NEO",
    #                   light: "FSLTL_FAIB_A20N"}}
    # A type with no detailed model anywhere stays light.
    models: dict = field(default_factory=dict)


_LOD = dict(tab="traffic", group="lod", restart=True, advanced=True)
describe(
    LodConfig,
    enabled=Setting(**_LOD),
    detailed_m=Setting(min=100, max=10000, unit="m", **_LOD),
    light_m=Setting(min=100, max=20000, unit="m", **_LOD),
    limit=Setting(min=0, max=40, **_LOD),
    swaps_per_s=Setting(min=0.05, max=5, step=0.05, **_LOD),
    settle_s=Setting(min=0, max=30, unit="s", **_LOD),
    mode=Setting(choices=("distance", "schedule"), **_LOD),
    detail_radius_km=Setting(min=0.5, max=20, unit="km",
                             when="traffic.lod.mode=schedule", **_LOD),
    livery=Setting(choices=("match", "any"),
                   when="traffic.lod.mode=schedule", **_LOD),
    hide_m=Setting(min=0, max=10000, unit="m",
                   when="traffic.lod.livery=any", **_LOD),
    boarding_lead_s=Setting(min=0, max=3600, unit="s",
                            when="traffic.lod.mode=schedule", **_LOD),
    boarding_s=Setting(min=0, max=3600, unit="s",
                       when="traffic.lod.mode=schedule", **_LOD),
    # A table of model titles; read-only on the screen.
    models=Setting("advanced", "debug", readonly=True, developer=True,
                   restart=True),
)


@dataclass
class HandlingConfig:
    """The ground handling around the parked traffic.

    ``traffic.handling: true`` or ``false`` is still understood: a plain
    value is taken as ``enabled``.
    """

    # The turnaround at all: who calls on which aeroplane, and when, and so
    # when its doors open and shut.
    enabled: bool = True
    # Whether this program draws the vehicles. Off, the turnaround and the
    # doors still run on this program's schedule and no vehicle is put in
    # the simulator -- for a pilot who prefers another tool's (ApronLife's)
    # vehicles around the same aeroplanes.
    vehicles: bool = True
    # With GSX Pro installed, its own vehicles (stairs, loaders, caterers,
    # fuel, power, tugs) for this program's traffic instead of the
    # simulator's stock ones. Ignored without GSX: nothing is asked for that
    # the simulator does not list.
    gsx_vehicles: bool = True
    # Passengers between the aeroplane and a bus that drives along the
    # airport's service roads (wilcoatc.atc.passengers): GSX Pro's own
    # people and bus, because nothing else in the simulator walks, so this
    # does nothing without it. Off by default: every passenger is an object
    # in the simulator.
    passengers: bool = False
    # How many passengers each bus run shows walking.
    passengers_visible: int = 12
    # The bags (wilcoatc.atc.baggage): a tractor and carts beside each
    # loader, and with GSX Pro the handler on the loader at work and the
    # bags on the belt. Off by default, like the passengers.
    baggage: bool = False

    def __bool__(self) -> bool:
        return self.enabled


describe(
    HandlingConfig,
    enabled=Setting("traffic", "handling"),
    vehicles=Setting("traffic", "handling", restart=True),
    gsx_vehicles=Setting("traffic", "handling", label="set.traffic_gsx",
                         help="set.traffic_gsx_why"),
    passengers=Setting("traffic", "handling", restart=True),
    passengers_visible=Setting("traffic", "handling", min=0, max=60,
                               restart=True,
                               when="traffic.handling.passengers=true"),
    baggage=Setting("traffic", "handling", restart=True),
)


@dataclass
class LiveTrafficConfig:
    """The real aeroplanes round the pilot, from the free ADS-B sources
    (integrations/adsb.py): adsb.lol, adsb.fi, and a receiver of the pilot's
    own. Off until a real flight has been flown with it.

    What leaves the machine is the pilot's position and a radius, nothing
    else; adsbdb.com is asked the routes and types of the callsigns the
    sources have just broadcast, and its answers are kept on this PC.
    """

    enabled: bool = False
    # One area request this often; the sources' fair use is one a second at
    # most (adsb.fi), and 6 s is what LocalTC asks at. Never less than 6.
    poll_s: float = 6.0
    # Round the pilot. One request covers the area; 250 nm is the most the
    # sources serve.
    radius_nm: float = 60.0
    # A local readsb's aircraft.json (http://<pi>:8080/data/aircraft.json),
    # asked after the public sources; {lat} {lon} {nm} filled in if present.
    own_url: str = ""
    # Routes and missing types from adsbdb.com, kept in data/adsb_cache.json.
    routes: bool = True
    # How long the feed may be quiet before the invented traffic takes the
    # whole density back. Within it nothing changes: the live flights are
    # carried on from their last positions, so pulling the feed is not heard.
    fallback_after_s: float = 60.0
    # Each live aeroplane is drawn this far behind the feed (after LocalTC),
    # always between two positions already heard: a new one appears this
    # long after it is first heard, and what it is about to do is known
    # before it does it.
    live_delay_s: float = 30.0


_LIVE = dict(tab="traffic", group="live")
describe(
    LiveTrafficConfig,
    enabled=Setting(restart=True, **_LIVE),
    poll_s=Setting(min=6, max=60, unit="s", restart=True, advanced=True,
                   **_LIVE),
    radius_nm=Setting(min=5, max=250, unit="nm", restart=True,
                      advanced=True, **_LIVE),
    own_url=Setting(restart=True, advanced=True, **_LIVE),
    routes=Setting(restart=True, advanced=True, **_LIVE),
    fallback_after_s=Setting(min=10, max=600, unit="s", advanced=True,
                             **_LIVE),
    live_delay_s=Setting(min=0, max=120, unit="s", restart=True,
                         advanced=True, **_LIVE),
)


@dataclass
class TrafficDebugConfig:
    """Tools for testing the apron, every one of them off by default.

    Best set in config.local.yaml, so they never reach the shared file.
    """

    # Invent only these types (ICAO codes, e.g. [A321]) instead of the
    # airlines' real fleets. The airlines and callsigns stay real.
    force_types: list = field(default_factory=list)
    # Every status_every_s, one line per invented aeroplane in the log
    # (logger wilcoatc.traffic.debug): type, stand, level, window, doors,
    # and how long until the next thing happens to it.
    status: bool = False
    status_every_s: float = 30.0
    # Two buttons in the traffic bay: board the parked aeroplane nearest
    # you now, and an arrival on short final to the free stand nearest you.
    triggers: bool = False
    # How much faster the turnaround runs: the ramp's timings and the
    # schedule's booking and boarding are divided by it. 1 is real time.
    time_scale: float = 1.0


_DEBUG = dict(tab="advanced", group="debug", developer=True)
describe(
    TrafficDebugConfig,
    force_types=Setting(restart=True, **_DEBUG),
    status=Setting(**_DEBUG),
    status_every_s=Setting(min=1, max=600, unit="s", **_DEBUG),
    triggers=Setting(**_DEBUG),
    time_scale=Setting(min=0.1, max=60, restart=True, **_DEBUG),
)


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
    # Drawn every frame between the traffic's ticks (wilcoatc.atc.follow),
    # frozen so the simulator does not move them itself; off writes each
    # aeroplane once a tick, which is what jumped on the taxiway and made a
    # takeoff a teleport.
    smooth_motion: bool = True
    # How many aeroplanes stand at the gates on top of that, going nowhere
    # until one of them becomes a departure. Only at a field whose stands the
    # simulator has given us, never on every stand (the moving traffic has
    # to have somewhere to park), and 0 is none. They are drawn after the
    # moving traffic, so they never cost it a place.
    parked: int = 12
    # Which model each invented aeroplane is drawn with, by how far it is
    # from you. See LodConfig.
    lod: LodConfig = field(default_factory=LodConfig)
    # Tools for testing the apron. See TrafficDebugConfig; all off.
    debug: TrafficDebugConfig = field(default_factory=TrafficDebugConfig)
    # The old name of traffic.debug.force_types, still read.
    debug_force_types: list = field(default_factory=list)
    # FSLTL's model matching table, for the airlines' own liveries. Empty
    # finds it in the Community folder the simulator records; a path names
    # the file for an installation that is somewhere else.
    fsltl_rules: str = ""
    # Whether the aeroplanes this program parks are handled: a power unit,
    # stairs at a ramp, a baggage loader, a caterer and a fuel truck call on
    # an arrival, and a tug comes for a departure. See HandlingConfig;
    # "handling: false" still switches all of it off.
    handling: HandlingConfig = field(default_factory=HandlingConfig)
    # Whether somebody else's parked aeroplanes -- FSLTL's, AIG's -- are
    # handled too. Microsoft Flight Simulator 2024 sends nothing to them
    # (measured: seven FSLTL aeroplanes at Madeira's gates with not a
    # vehicle between them), so they are by default; off, for a setup
    # where something else already does it.
    handling_observed: bool = True
    # Whether a handled aeroplane's doors and holds are opened with the
    # stairs and the loaders and read shut before it pushes. Done with the
    # exit event aimed at the aeroplane itself, on the models the door
    # table knows (integrations/doors.json); a model not in it is left
    # alone, which is right for FSLTL's, which have no door animation.
    handling_doors: bool = True
    # Whether this program's own aeroplanes have their engines and lights
    # run the way an airline runs them: off on the stand, the beacon before
    # the push, started during it, taxi/strobe/landing lights by phase,
    # shut down a minute or two after block-in. Never the pilot's, never
    # anybody else's traffic.
    engines_and_lights: bool = True
    # A door table of your own, in the same shape as the shipped one, for
    # models scripts/scan_doors.py has been run on. Empty is the shipped
    # table.
    doors_table: str = ""
    # How long a departure's doors may go on closing before the close is
    # forced and the aeroplane released: a door that answers slowly holds
    # a departure for this long, never for ever.
    doors_force_after_s: float = 300.0
    # How many vehicles are out on the apron at once, not counting the
    # stairs and the tugs. Each is a drawn object; the nearest aeroplanes
    # to the pilot are served first and the rest wait their turn or go
    # without. Twenty-four is five aeroplanes being fully worked at once.
    handling_limit: int = 24
    # Roughly how many aircraft are worked at once, as a multiple of what the
    # field would normally carry (atc.fleet: an airport by its size, a
    # general-aviation field with a tower four to six, an airfield without
    # one two or three), and how often one starts. 0 is silence, 2 is a busy
    # morning.
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
    # Who flies the traffic: "wilcoatc" (this program: the live flights when
    # traffic.live is on, and invented ones in the gaps) or "fsltl" (FSLTL,
    # or whatever else the pilot runs, worked as it is found; nothing live
    # injected and the feed not asked, because the two together would put a
    # second copy of every real flight in the sky). The pilot chooses;
    # nothing is detected. "auto" is fsltl when FSLTL is in the Community
    # folder, else wilcoatc (integrations/fsltl.traffic_source).
    source: str = "auto"           # auto | wilcoatc | fsltl
    # The real aeroplanes round you. See LiveTrafficConfig; off.
    live: LiveTrafficConfig = field(default_factory=LiveTrafficConfig)


describe(
    TrafficConfig,
    enabled=Setting("traffic", "traffic", label="set.traffic_on",
                    help="set.traffic_why"),
    from_simulator=Setting("traffic", "traffic"),
    in_simulator=Setting("traffic", "traffic", restart=True),
    in_simulator_limit=Setting("traffic", "traffic", min=0, max=60,
                               restart=True),
    density=Setting("traffic", "traffic", min=0, max=3, step=0.25,
                    widget="slider", label="set.how_busy"),
    parked=Setting("traffic", "traffic", min=0, max=80, restart=True),
    source=Setting("traffic", "traffic",
                   choices=("auto", "wilcoatc", "fsltl"), restart=True),
    callsigns=Setting("traffic", "traffic", choices=("auto", "registration"),
                      choice_keys=("set.callsigns_auto", "set.callsigns_reg"),
                      restart=True, label="set.traffic_callsigns",
                      help="set.traffic_callsigns_why"),
    smooth_motion=Setting("traffic", "traffic", restart=True, advanced=True),
    fsltl_rules=Setting("traffic", "traffic", kind="path", restart=True,
                        advanced=True),
    handling_observed=Setting("traffic", "handling", advanced=True),
    handling_doors=Setting("traffic", "handling", restart=True,
                           advanced=True),
    engines_and_lights=Setting("traffic", "handling", restart=True,
                               advanced=True),
    doors_table=Setting("traffic", "handling", kind="path", restart=True,
                        advanced=True),
    doors_force_after_s=Setting("traffic", "handling", min=10, max=3600,
                                unit="s", restart=True, advanced=True),
    handling_limit=Setting("traffic", "handling", min=0, max=200,
                           restart=True, advanced=True),
    seed=Setting("advanced", "debug", min=0, max=2**31 - 1, restart=True,
                 developer=True),
    # The old name of traffic.debug.force_types, still read.
    debug_force_types=Setting("advanced", "debug", restart=True,
                              developer=True, hidden=True),
)


@dataclass
class WeatherConfig:
    online: bool = True
    prefer_sim: bool = True
    refresh_s: float = 600.0
    station_override: str = ""


describe(
    WeatherConfig,
    online=Setting("weather_data", "weather", restart=True,
                   label="set.real_obs", help="set.real_obs_why"),
    prefer_sim=Setting("weather_data", "weather", label="set.sim_wins",
                       help="set.sim_wins_why"),
    refresh_s=Setting("weather_data", "weather", min=60, max=7200, unit="s",
                      restart=True, advanced=True),
    station_override=Setting("weather_data", "weather", advanced=True),
)


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


describe(
    SimConfig,
    enabled=Setting("weather_data", "simulator", label="set.use_sim",
                    help="set.use_sim_why"),
    poll_interval_s=Setting("weather_data", "simulator", min=0.05, max=5,
                            step=0.05, unit="s", advanced=True),
    reconnect_interval_s=Setting("weather_data", "simulator", min=1, max=120,
                                 unit="s", advanced=True),
    frequencies_from_sim=Setting("weather_data", "simulator", advanced=True),
    simconnect_dll=Setting("weather_data", "simulator", kind="path",
                           restart=True, advanced=True),
)


@dataclass
class FlightConfig:
    """Details the sim cannot tell us, which the pilot supplies."""

    callsign: str = ""
    aircraft_type: str = ""
    departure: str = ""
    destination: str = ""
    cruise_altitude_ft: int = 0
    ifr: bool = True
    # What kind of flying this is, as the pilot said it (atc.profile):
    # airliner | general_aviation | helicopter. The cabin, the first
    # officer's callouts and checklists, the callsign and the stand follow
    # it; the simulator's aeroplane only questions it. Airliner by default,
    # which is what every flight was taken for before there was a choice.
    profile: str = "airliner"
    # The phraseology region: auto (each controller's own field) or pinned,
    # faa | icao | uk | fr | de | metric (navdata.db.vfr_region's regions).
    region: str = "auto"


describe(
    FlightConfig,
    callsign=Setting("flight", "flight"),
    aircraft_type=Setting("flight", "flight"),
    departure=Setting("flight", "flight"),
    destination=Setting("flight", "flight"),
    cruise_altitude_ft=Setting("flight", "flight", min=0, max=60000,
                               step=500, unit="ft"),
    ifr=Setting("flight", "flight"),
    profile=Setting("flight", "flight",
                    choices=("airliner", "general_aviation", "helicopter")),
    region=Setting("flight", "flight",
                   choices=("auto", "faa", "icao", "uk", "fr", "de",
                            "metric")),
)


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


describe(
    NetworkConfig,
    provider=Setting("radio", "network", choices=("none", "vatsim", "ivao"),
                     restart=True),
    refresh_s=Setting("radio", "network", min=30, max=1800, unit="s",
                      restart=True, advanced=True,
                      when="network.provider!=none"),
    use_online_atis=Setting("radio", "network",
                            when="network.provider!=none"),
)


@dataclass
class ControllerRealismConfig:
    """What a real controller does that the program's did not: Special VFR,
    a runway change with the wind, orbits and holds, the conditional
    clearances, the reporting points chased, a busy frequency, the readback
    corrected item by item, the circuit in the ATIS (Phase 6 of the VFR
    plan).

    It sits under ``immersion`` because the plan put it there, and unlike the
    rest of that section it is on whatever ``immersion.enabled`` says: it is
    the controller, not the crew. Each switch turns one behaviour back into
    what it was before.
    """

    special_vfr: bool = True
    runway_change: bool = True
    delays: bool = True
    conditional_clearances: bool = True
    reporting_points: bool = True
    congestion: bool = True
    readback_items: bool = True
    atis_briefing: bool = True
    # The IFR plan, Phase 1. The departure as a controller works it: an
    # initial altitude of the field's own, the SID read back by name, the
    # takeoff clearance with the SID's first fix, departure's check-in with
    # a step and a direct; off, every clearance is 5,000 ft and the climb
    # goes straight to the cruise, as before.
    ifr_departure: bool = True
    # The climb in steps, departure's then each centre's, and the handoff to
    # the centre where departure's airspace ends.
    climb_steps: bool = True
    # "Verify you are on the ... departure", once, to a flight off its SID.
    sid_watch: bool = True
    # The IFR plan, Phase 2. The descent cleared before the top of descent,
    # in the form that fits -- "descend via" the STAR, a crossing
    # restriction, at pilot's discretion, or plain -- with the QNH; off, the
    # first descent waits for the old profile, as before.
    ifr_descent: bool = True
    # The en route (atc/routing.py): the plan's own step climbs offered by
    # the centre; a quiet centre's "proceed direct" further down the route
    # when it saves real miles; "verify you are proceeding direct" to a
    # flight a minute off its route; weather deviations approved with a fix
    # to rejoin at. Off, each is what it was: nothing said unasked, and a
    # deviation request not understood.
    plan_steps: bool = True
    shortcuts: bool = True
    off_route_watch: bool = True
    deviations: bool = True
    # The IFR plan, Phase 3. The approach areas (navdata.approach_areas):
    # departure hands a flight to the centre out of its field's area or
    # above it, and the centre hands an arrival to approach inside the
    # destination's area and below its ceiling. Off, or at a field with no
    # area, 30 nm / 17,000 ft and 60 nm, as before.
    approach_areas: bool = True
    # The IFR plan, Phase 4. The approach catalogue (atc/approaches.py):
    # the chart's letter, the minima the aeroplane can fly against the
    # weather, the visual in good weather in the US and Canada, a circle to
    # land where the runway has nothing. Off, the ILS, then an RNAV, then a
    # non-precision approach, whatever the weather, as before.
    approach_catalogue: bool = True
    # The IFR plan, Phase 5 (a). A STAR that runs onto the final: the
    # approach cleared along it, no level said over a "descend via", and the
    # handoff to the tower once established about ten miles out. Off, the
    # clearance waits for the final's cone and the tower takes the flight at
    # fifteen miles, as before.
    star_final: bool = True
    # The IFR plan, Phase 5 (b). Radar vectors onto the final
    # (atc/vectors.py): straight in, or a downwind, base and intercept, the
    # levels with the miles and never below the terrain along the heading,
    # the intercept carrying the clearance and a visual only with the field
    # in sight. Off, approach clears a flight only in the final's cone.
    vectors: bool = True
    # The IFR plan, Phase 5 (c). Speed control while vectoring, with traffic
    # landing ahead: 210 kt on the pattern, 160 kt to four miles on the
    # final, lifted when nobody is ahead. Off, no speed is ever given.
    speed_control: bool = True
    # The IFR plan, Phase 5 (d). A go-around is re-vectored into the pattern
    # from where it is, its old vectors and speed forgotten. Off, approach
    # carries on from the pattern the flight was in before it went round.
    go_around_vectors: bool = True
    # The IFR plan, Phase 5 (e). The tower answers an IFR arrival checking
    # in on the final -- the landing clearance, or "runway NN, continue,
    # I'll call your clearance" until the runway is free -- and Ground gives
    # an arrival its taxi in when it reports the runway vacated. Off, the
    # check-in is not understood and "runway vacated" gets "roger".
    tower_continue: bool = True
    # The IFR plan, Phase 6. Holds (atc/holds.py): over the arrival's last
    # fix, for a queue the runway cannot take, weather below every minima or
    # the pilot's asking, with an EFC, the sequence when it changes, and
    # "cleared to leave the hold" when the reason is gone. Off, nobody holds.
    holds: bool = True
    # The IFR plan, Phase 6. The pilot's requests answered: a speed, a hold,
    # a return or a diversion ("cleared to ... via direct"), "request
    # descent" before it is due ("expect lower in N minutes"), each a
    # clearance or "unable" with the reason. Off, they are not understood.
    pilot_requests: bool = True


describe(
    ControllerRealismConfig,
    **{name: Setting("immersion", "controller") for name in (
        "special_vfr", "runway_change", "delays", "conditional_clearances",
        "reporting_points", "congestion", "readback_items", "atis_briefing",
        "ifr_departure", "climb_steps", "sid_watch", "ifr_descent",
        "plan_steps", "shortcuts", "off_route_watch", "deviations",
        "approach_areas", "approach_catalogue", "star_final", "vectors",
        "speed_control", "go_around_vectors", "tower_continue",
        "holds", "pilot_requests")},
)


@dataclass
class FirstOfficerConfig:
    """The first officer's timing (wilcoatc.atc.checklist and
    wilcoatc.atc.callouts); the switches are ``copilot``, ``callouts`` and
    ``checklists`` beside it. A section of its own rather than inside
    ``copilot``, which is the on/off the panel writes."""

    # How long an item waits for an answer before it is read again, then
    # held; and how long the radio has to be quiet before an item it talked
    # over is read again.
    answer_wait_s: float = 8.0
    resume_after_s: float = 1.5
    # Offers: at most one in this long, standing this long, none this soon
    # after the pilot spoke to the first officer or the flight began, and
    # none below this height in the air.
    offer_every_s: float = 120.0
    offer_answer_s: float = 30.0
    offer_after_talk_s: float = 30.0
    offer_settle_s: float = 20.0
    offer_floor_agl_ft: float = 500.0
    # A callout the radio held back longer than this is dropped: a late
    # "rotate" is worse than none.
    callout_late_s: float = 3.0
    # Each deviation call (speed, sink rate, bank, altitude) at most once
    # in this long.
    deviation_every_s: float = 30.0
    # 50 / 30 / 10 in the flare, in a light aeroplane.
    flare_callouts: bool = False
    # What the first officer notices unasked (wilcoatc.atc.notices): "gear's
    # still up", the standby transponder, the altimeter. Off on its own,
    # without the checklists or the callouts.
    notices: bool = True
    # Whether the first officer may take the radio for ONE call when the
    # pilot hands it over in so many words ("you make the call", "call
    # ground for taxi"). Never by default, never a readback that was not
    # handed over too.
    radio: bool = True


_FO_TIMING = dict(tab="first_officer", group="fo_timing", advanced=True,
                  unit="s")
describe(
    FirstOfficerConfig,
    answer_wait_s=Setting(min=2, max=60, **_FO_TIMING),
    resume_after_s=Setting(min=0, max=10, step=0.5, **_FO_TIMING),
    offer_every_s=Setting(min=10, max=1800, **_FO_TIMING),
    offer_answer_s=Setting(min=5, max=300, **_FO_TIMING),
    offer_after_talk_s=Setting(min=0, max=600, **_FO_TIMING),
    offer_settle_s=Setting(min=0, max=600, **_FO_TIMING),
    offer_floor_agl_ft=Setting("first_officer", "fo_timing", min=0,
                               max=10000, step=100, unit="ft",
                               advanced=True),
    callout_late_s=Setting(min=0.5, max=15, step=0.5, **_FO_TIMING),
    deviation_every_s=Setting(min=5, max=600, **_FO_TIMING),
    flare_callouts=Setting("first_officer", "first_officer"),
    notices=Setting("first_officer", "first_officer"),
    radio=Setting("first_officer", "first_officer"),
)


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
    # The first officer's timing (see FirstOfficerConfig).
    first_officer: FirstOfficerConfig = field(
        default_factory=FirstOfficerConfig)
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
    # The controller's half (see ControllerRealismConfig).
    controller: ControllerRealismConfig = field(
        default_factory=ControllerRealismConfig)


#: The languages the crew have scripts in (atc/crew_lines.py), and "auto".
CREW_LANGUAGES = ("auto", "en", "fr", "de", "nl")

_FO = dict(tab="first_officer", group="first_officer")
_CABIN = dict(tab="immersion", group="cabin")
_MANNER = dict(tab="immersion", group="crew_manner")
describe(
    ImmersionConfig,
    enabled=Setting("immersion", "crew", label="set.immersion",
                    help="set.immersion_why"),
    copilot=Setting(label="set.copilot", help="set.copilot_why", **_FO),
    # The picker writes the voice and the sex together: "a woman" is a sex
    # and clears the voice; a person clears the sex.
    copilot_voice=Setting(widget="voice", label="set.copilot_voice", **_FO),
    copilot_gender=Setting(choices=("auto", "m", "f"), hidden=True, **_FO),
    callouts=Setting(label="set.callouts", help="set.callouts_why", **_FO),
    confirmations=Setting(label="set.confirmations",
                          help="set.confirmations_why", **_FO),
    checklists=Setting(label="set.checklists", help="set.checklists_why",
                       **_FO),
    radio_readbacks=Setting(label="set.readbacks", help="set.readbacks_why",
                            **_FO),
    cabin=Setting(label="set.cabin_crew", help="set.cabin_crew_why",
                  **_CABIN),
    cabin_voice=Setting(widget="voice", label="set.cabin_voice", **_CABIN),
    cabin_gender=Setting(choices=("auto", "m", "f"), hidden=True, **_CABIN),
    briefings=Setting(label="set.briefings", help="set.briefings_why",
                      **_CABIN),
    announcements=Setting(label="set.announcements",
                          help="set.announcements_why", **_CABIN),
    service=Setting(label="set.service", help="set.service_why", **_CABIN),
    chimes=Setting(label="set.chimes", help="set.chimes_why", **_CABIN),
    boarding_music=Setting(label="set.boarding_music",
                           help="set.boarding_music_why", **_CABIN),
    applause=Setting(label="set.applause", help="set.applause_why",
                     **_CABIN),
    realism=Setting(label="set.crew_realism", help="set.crew_realism_why",
                    advanced=True, **_CABIN),
    bilingual=Setting(label="set.bilingual", help="set.bilingual_why",
                      advanced=True, **_CABIN),
    recordings=Setting("immersion", "recorded", label="set.use_recordings",
                       help="set.use_recordings_why"),
    recordings_dir=Setting("immersion", "recorded", kind="path",
                           label="set.announcements_dir",
                           help="set.announcements_dir_why"),
    volume=Setting(min=0, max=1.2, step=0.05, unit="%", scale=100,
                   widget="slider", label="set.crew_volume", **_MANNER),
    language=Setting(choices=CREW_LANGUAGES, advanced=True, **_MANNER),
    airline=Setting(label="set.airline_name", help="set.airline_why",
                    **_MANNER),
    captain_name=Setting(label="set.captain_name",
                         help="set.captain_name_why", **_MANNER),
    interphone=Setting(label="set.talk_to_crew", help="set.talk_to_crew_why",
                       **_MANNER),
    verbosity=Setting(choices=("quiet", "normal", "chatty"),
                      choice_keys=("set.quiet", "set.normal", "set.chatty"),
                      label="set.how_much", **_MANNER),
)


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
    # The apron, in the log: every door opened and shut, every model
    # swapped for a nearer or a lighter one. Off, those loggers
    # (wilcoatc.doors, wilcoatc.lod) say nothing short of a warning.
    apron: bool = True


_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_LOG = dict(tab="advanced", group="logging")
describe(
    LoggingConfig,
    level=Setting(choices=_LEVELS, **_LOG),
    console_level=Setting(choices=_LEVELS, **_LOG),
    directory=Setting(kind="path", restart=True, **_LOG),
    max_bytes=Setting(min=100_000, max=100_000_000, restart=True, **_LOG),
    backups=Setting(min=0, max=50, restart=True, **_LOG),
    structured=Setting(**_LOG),
    radio=Setting(**_LOG),
    crew=Setting(restart=True, **_LOG),
    keep_days=Setting(min=0, max=365, restart=True, **_LOG),
    apron=Setting(restart=True, **_LOG),
)


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

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        """``config.yaml``, then ``config.local.yaml`` beside it on top.

        The local file is for settings one machine wants and the tracked
        file must not carry -- a developer testing ``livery: any`` while the
        tracked file holds what ships. It is never written: :meth:`save`
        puts the tracked file's own values back for every key it sets.
        """
        path = Path(path) if path else CONFIG_PATH
        config = cls()
        if path.exists():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            _merge(config, raw)
            _read_legacy(config, raw)
        local = path.with_name(LOCAL_NAME)
        if local.exists():
            overlay = yaml.safe_load(local.read_text(encoding="utf-8")) or {}
            if isinstance(overlay, dict):
                base = asdict(config)
                _merge(config, overlay)
                object.__setattr__(config, "_local_base",
                                   _leaves(overlay, base))
        # Where a save goes: the file this was read from, which is the one
        # --config named, not always the default one.
        object.__setattr__(config, "_path", path)
        # What was read, so a save writes only what this run changed.
        object.__setattr__(config, "_saved", asdict(config))
        return config

    def override(self, path: str, value: Any) -> None:
        """Set a value for this run only -- a command-line option.

        ``gui --no-sim`` used to be saved with the first setting changed on
        the screen, and the simulator stayed off for good. The file's own
        value is what a save writes, until the pilot changes the same setting
        on the screen (:meth:`release`).
        """
        held = self.__dict__.setdefault("_held", {})
        held.setdefault(path, get_path(self, path))
        set_path(self, path, value)

    def release(self, path: str) -> None:
        """The pilot chose this one: a save writes it from now on."""
        self.__dict__.get("_held", {}).pop(path, None)

    def save(self, path: Path | str | None = None) -> Path:
        """Write what this run changed over the file as it is now.

        Only what changed since the file was read (or last saved), when it
        is the file this was read from: a save used to write back every
        value the program held, so ``ground_control: true`` written into the
        file while the program ran was ``false`` again after the next change
        of accent colour on the screen. Anything else -- a new file, another
        path, a Config built in code -- is written whole.
        """
        path = Path(path) if path else getattr(self, "_path", CONFIG_PATH)
        data = asdict(self)
        # What config.local.yaml set stays in config.local.yaml, and what the
        # command line set stays on the command line.
        held = [(tuple(key.split(".")), value)
                for key, value in self.__dict__.get("_held", {}).items()]
        kept = [*getattr(self, "_local_base", []), *held]
        for keys, value in kept:
            where = data
            for key in keys[:-1]:
                where = where[key]
            where[keys[-1]] = value
        saved = self.__dict__.get("_saved")
        own_file = Path(path) == Path(getattr(self, "_path", ""))
        if saved is not None and own_file and path.exists():
            changed = _changed(data, saved)
            for keys, _value in kept:
                changed = _without(changed, keys)
            _write_keeping_comments(path, changed)
        else:
            _write_keeping_comments(path, data)
        object.__setattr__(self, "_saved", data)
        return path


def _changed(now: dict, before: dict) -> dict:
    """The leaves of ``now`` that differ from ``before``, nested as they are."""
    out = {}
    for key, value in now.items():
        was = before.get(key) if isinstance(before, dict) else None
        if isinstance(value, dict) and isinstance(was, dict):
            inner = _changed(value, was)
            if inner:
                out[key] = inner
        elif key not in (before or {}) or value != was:
            out[key] = value
    return out


def _without(data: dict, keys: tuple) -> dict:
    """``data`` with one leaf taken out, by its key path."""
    if not keys or keys[0] not in data:
        return data
    out = dict(data)
    if len(keys) == 1:
        out.pop(keys[0])
    elif isinstance(out[keys[0]], dict):
        inner = _without(out[keys[0]], keys[1:])
        if inner:
            out[keys[0]] = inner
        else:
            out.pop(keys[0])
    return out


def _write_keeping_comments(path: Path, data: dict) -> None:
    """Write ``data`` over the file without losing what the pilot wrote.

    ruamel.yaml's round trip keeps comments, key order and the keys this
    program does not know (an old name, a section of somebody's own); a
    value that has not changed is not even touched, so its formatting stays
    as written. PyYAML, which reads the file, cannot do this: its dump is the
    whole file regenerated, and every comment in it went the first time the
    panel saved anything.
    """
    rt = RoundTripYAML()
    rt.preserve_quotes = True
    rt.width = 4096
    # "null", as the file has always said it; ruamel's default is nothing.
    rt.representer.add_representer(
        type(None),
        lambda dumper, _: dumper.represent_scalar("tag:yaml.org,2002:null",
                                                  "null"))
    document = None
    if path.exists():
        try:
            document = rt.load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            # A file the round trip cannot read is rewritten whole rather
            # than left unsaved: the setting the pilot just changed matters
            # more than the formatting of a file that does not parse.
            import logging
            logging.getLogger(__name__).warning(
                "config.yaml could not be read back (%s); writing it afresh",
                exc)
    if not isinstance(document, dict):
        document = CommentedMap()
    _update_in_place(document, data)
    buffer = io.StringIO()
    rt.dump(document, buffer)
    path.write_text(buffer.getvalue(), encoding="utf-8")


def _update_in_place(target, data: dict) -> None:
    for key, value in data.items():
        current = target.get(key) if key in target else None
        if isinstance(value, dict) and isinstance(current, dict):
            _update_in_place(current, value)
        elif key not in target or current != value:
            target[key] = value


#: The untracked overlay beside config.yaml (see Config.load).
LOCAL_NAME = "config.local.yaml"


def _leaves(overlay: dict, base: dict, prefix: tuple = ()) -> list:
    """(key path, the base value) for every setting the overlay changes."""
    found = []
    for key, value in overlay.items():
        if not isinstance(base, dict) or key not in base:
            continue
        if isinstance(value, dict) and isinstance(base[key], dict):
            found += _leaves(value, base[key], prefix + (key,))
        else:
            found.append((prefix + (key,), base[key]))
    return found


def _merge(target: Any, values: dict) -> None:
    """Apply a nested dict onto a dataclass, ignoring unknown keys."""
    known = {f.name: f for f in fields(target)}
    for key, value in (values or {}).items():
        if key not in known:
            continue
        current = getattr(target, key)
        if is_dataclass(current) and isinstance(value, dict):
            _merge(current, value)
        elif (is_dataclass(current) and isinstance(value, bool)
              and hasattr(current, "enabled")):
            # A section that used to be a switch: "handling: false".
            current.enabled = value
        else:
            setattr(target, key, value)


def _read_legacy(config: "Config", raw: dict) -> None:
    """An old name that still means something, read into its new one.

    ``atc.dialect`` pinned FAA or ICAO wording and was never read (only a
    fallback object nobody asked took it), so a pilot who set it believed
    the radio was pinned. ``flight.region`` is the pin that works; a region
    written beside the old name wins, because it is the newer.
    """
    if not isinstance(raw, dict):
        return
    atc = raw.get("atc")
    flight = raw.get("flight")
    dialect = atc.get("dialect") if isinstance(atc, dict) else None
    if dialect in ("faa", "icao") and not (
            isinstance(flight, dict) and "region" in flight):
        config.flight.region = dialect


#: Names a config file may still carry that no longer mean anything. They
#: load -- an unknown key is ignored -- and are listed so that nobody brings
#: one back meaning something else: each was in the file with no reader
#: (``audio.sidetone``, ``atc.abbreviate_callsigns``, ``speech.language``),
#: or was shadowed by the section that replaced it (``log_level`` under
#: ``logging.level``). ``atc.dialect`` is read once more, into
#: ``flight.region`` (:func:`_read_legacy`).
LEGACY_NAMES = ("audio.sidetone", "atc.abbreviate_callsigns",
                "speech.language", "log_level", "atc.dialect")


def _leaf_fields(instance: Any, prefix: str = ""):
    """(path, field name, owning class, default) for every setting."""
    for item in fields(instance):
        value = getattr(instance, item.name)
        path = prefix + item.name
        if is_dataclass(value):
            yield from _leaf_fields(value, path + ".")
        else:
            yield path, item.name, type(instance), value


#: Every setting, by its dotted path, and what it was by default.
SETTINGS: dict[str, Setting] = {}
DEFAULTS: dict[str, Any] = {}
for _path, _name, _owner, _default in _leaf_fields(Config()):
    DEFAULTS[_path] = _default
    _described = getattr(_owner, "__settings__", {}).get(_name)
    if _described is not None:
        SETTINGS[_path] = describe_field(_path, _default, _described)

#: The sections a section-sized write can name: "traffic", "traffic.lod".
SECTIONS = sorted({path.rsplit(".", 1)[0] for path in DEFAULTS if "." in path})

_COLOUR = re.compile(r"#[0-9a-f]{6}")
_TEXT_LIMIT = 1000


def _plain(number: float):
    return int(number) if float(number).is_integer() else number


def check(path: str, value: Any) -> Any:
    """``value`` as the setting at ``path`` stores it, or SettingRefused.

    What the screen sends is coerced here rather than trusted -- it is a
    browser's idea of a number -- and a value outside what the setting
    allows is refused with the reason, so nothing out of range is ever
    written to the file.
    """
    setting = _setting_or_generic(path)
    if setting is None:
        raise SettingRefused("cfg.err.unknown",
                             "There is no setting called {path}.", path=path)
    default = DEFAULTS[path]
    value = _coerce(setting, value, default)
    if setting.readonly and value != default:
        raise SettingRefused(
            "cfg.err.readonly",
            "This one was measured; only its measured value can be put back.")
    return value


def _coerce(setting: Setting, value: Any, default: Any) -> Any:
    kind = setting.kind
    if setting.widget in ("input_device", "output_device"):
        # Nothing chosen is the system's default device, which is not the
        # same as device zero.
        if value in (None, "", "default"):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return str(value)
    if kind == "toggle":
        if isinstance(value, str) and value.strip().lower() in (
                "true", "false", "on", "off", "1", "0"):
            return value.strip().lower() in ("true", "on", "1")
        if isinstance(value, (bool, int)) and value in (0, 1):
            return bool(value)
        raise SettingRefused("cfg.err.toggle", "This is either on or off.")
    if kind == "number":
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = math.nan
        if not math.isfinite(number):
            raise SettingRefused("cfg.err.number", "This has to be a number.")
        if setting.off is not None and number == setting.off:
            return type(default)(number)
        if setting.min is not None and number < setting.min:
            raise SettingRefused("cfg.err.below", "The lowest it goes is {min}.",
                                 min=_plain(setting.min))
        if setting.max is not None and number > setting.max:
            raise SettingRefused("cfg.err.above",
                                 "The highest it goes is {max}.",
                                 max=_plain(setting.max))
        if isinstance(default, int) and not isinstance(default, bool):
            if not number.is_integer():
                raise SettingRefused("cfg.err.whole",
                                     "This has to be a whole number.")
            return int(number)
        return number
    if kind == "choice":
        wanted = str(value if value is not None else "").strip().lower()
        for choice in setting.choices:
            if str(choice).lower() == wanted:
                return choice
        raise SettingRefused("cfg.err.choice",
                             "That is not one of the choices.")
    if kind == "colour":
        colour = str(value or "").strip().lower()
        if not _COLOUR.fullmatch(colour):
            raise SettingRefused("cfg.err.colour",
                                 "A colour is written #rrggbb.")
        return colour
    if kind == "list":
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, (list, tuple)):
            raise SettingRefused("cfg.err.list", "This is a list.")
        items = [str(item).strip() for item in value if str(item).strip()]
        if setting.widget == "languages":
            # Empty is the normal setting: the languages follow the
            # aeroplane. A pinned set always keeps English, which is worked
            # everywhere.
            items = [item.lower()[:2] for item in items]
            if items and "en" not in items:
                items.insert(0, "en")
        return items
    if kind == "map":
        if not isinstance(value, dict):
            raise SettingRefused("cfg.err.map", "This is a table.")
        return dict(value)
    text = "" if value is None else str(value).strip()
    if len(text) > _TEXT_LIMIT:
        raise SettingRefused("cfg.err.long", "That is too long.")
    return text


def _setting_or_generic(path: str) -> Setting | None:
    """A field's Setting; a field nobody described yet gets a generic one,
    so it can still be saved (and the test names it)."""
    if path in SETTINGS:
        return SETTINGS[path]
    if path in DEFAULTS:
        return describe_field(path, DEFAULTS[path],
                              Setting(tab="advanced", group="internals",
                                      developer=True))
    return None


def get_path(config: Config, path: str) -> Any:
    target = config
    for step in path.split("."):
        target = getattr(target, step)
    return target


def set_path(config: Config, path: str, value: Any) -> None:
    *sections, name = path.split(".")
    target = config
    for step in sections:
        target = getattr(target, step)
    setattr(target, name, value)

