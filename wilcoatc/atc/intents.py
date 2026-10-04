"""Understanding what the pilot said.

Speech recognition gives back a line of text. This module turns that into a
structured intent the controller logic can act on, and pulls out the numbers --
altitudes, headings, frequencies, runways, squawk codes -- that the instruction
depends on.

Two properties matter more than breadth of coverage:

*Numbers must be right.* "Descend and maintain one two thousand" and "descend
and maintain two thousand" differ by ten thousand feet. Every number is parsed
from an explicit pattern, in both the spoken-digit form ("one two thousand")
and the numeral form Whisper often produces ("12,000"), and anything ambiguous
is reported as unparsed rather than guessed.

*A failure to understand must be visible.* When nothing matches, the result is
:data:`Intent.UNKNOWN` and the controller asks the pilot to say again, exactly
as a real one would. Silently guessing an intent is worse than admitting the
transmission was unreadable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .speech import _ALPHABET_REVERSE, _WORD_TO_DIGIT


class Intent(str, Enum):
    """What the pilot is trying to do."""

    UNKNOWN = "unknown"

    # --- getting started ---
    RADIO_CHECK = "radio_check"
    REQUEST_CLEARANCE = "request_clearance"       # IFR clearance
    REQUEST_VFR_DEPARTURE = "request_vfr_departure"
    # Circuits: touch-and-goes, the option, staying in the pattern.
    REQUEST_PATTERN_WORK = "request_pattern_work"
    REQUEST_PUSHBACK = "request_pushback"
    REQUEST_TAXI = "request_taxi"
    REQUEST_TAKEOFF = "request_takeoff"           # "ready for departure"

    # --- airborne ---
    CHECK_IN = "check_in"                         # "with you at one two thousand"
    POSITION_REPORT = "position_report"
    REQUEST_CLIMB = "request_climb"
    REQUEST_DESCENT = "request_descent"
    REQUEST_DIRECT = "request_direct"
    REQUEST_VECTORS = "request_vectors"
    REQUEST_APPROACH = "request_approach"
    # VFR: crossing somebody's airspace, and asking for radar advisories.
    REQUEST_TRANSITION = "request_transition"
    REQUEST_FLIGHT_FOLLOWING = "request_flight_following"
    REQUEST_LANDING = "request_landing"
    REPORT_FIELD_IN_SIGHT = "report_field_in_sight"
    REPORT_TRAFFIC_IN_SIGHT = "report_traffic_in_sight"
    REPORT_ESTABLISHED = "report_established"
    GOING_AROUND = "going_around"
    MISSED_APPROACH = "missed_approach"

    # --- ground, after landing ---
    REQUEST_TAXI_TO_PARKING = "request_taxi_to_parking"

    # --- conversational ---
    READBACK = "readback"
    ACKNOWLEDGE = "acknowledge"                   # roger / wilco
    AFFIRMATIVE = "affirmative"
    NEGATIVE = "negative"
    SAY_AGAIN = "say_again"
    STANDBY = "standby"
    UNABLE = "unable"
    REQUEST_FREQUENCY_CHANGE = "request_frequency_change"
    CANCEL_IFR = "cancel_ifr"

    # --- abnormal ---
    EMERGENCY = "emergency"
    URGENCY = "urgency"                           # pan-pan
    # The answer to the controller's questions after a MAYDAY or PAN: what
    # is wrong, how many on board, how much fuel.
    EMERGENCY_DETAILS = "emergency_details"
    MINIMUM_FUEL = "minimum_fuel"


@dataclass
class ParsedIntent:
    """One understood transmission."""

    intent: Intent = Intent.UNKNOWN
    text: str = ""
    normalized: str = ""
    language: str = "en"
    callsign_matched: bool = False
    station_addressed: str = ""
    confidence: float = 0.0
    values: dict[str, Any] = field(default_factory=dict)
    readback_items: dict[str, Any] = field(default_factory=dict)
    # Which rules the pilot said they are flying under: "ifr", "vfr", or
    # nothing said. Its own field rather than a value, because a value ends up
    # in ``readback_items`` and the rules are not something a controller waits
    # to hear read back.
    rules: str = ""

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    @property
    def understood(self) -> bool:
        return self.intent is not Intent.UNKNOWN


# Which rules a transmission declares, where it declares any. The
# abbreviations are international and are said as they are written in every
# language this program works in, so one table covers all of them; the spelt
# out forms are the ones a crew actually says instead.
#
# This exists because the alternative was believing a setting over the pilot.
# A flight configured as VFR that calls up and asks for an IFR clearance was
# read as VFR, answered with a VFR release, and went on being answered that
# way for the whole session -- with the one transmission that said otherwise
# thrown away.
_SAYS_IFR = re.compile(
    r"\b(i\.? ?f\.? ?r|instrument flight rules|aux instruments|"
    r"nach instrumentenflugregeln|instrumentenflugregeln)\b")
_SAYS_VFR = re.compile(
    r"\b(v\.? ?f\.? ?r|visual flight rules|a vue|"
    r"nach sichtflugregeln|sichtflugregeln)\b")


def flight_rules(norm: str) -> str:
    """The rules a transmission states, or "" if it states none.

    Only what was actually said. A transmission that mentions neither leaves
    the session where it was, which is what makes this an override rather than
    a second guess at every call.
    """
    said_ifr = bool(_SAYS_IFR.search(norm))
    said_vfr = bool(_SAYS_VFR.search(norm))
    if said_ifr and not said_vfr:
        return "ifr"
    if said_vfr and not said_ifr:
        return "vfr"
    # "Cancel IFR, VFR on top" says both. Nothing here can tell which way
    # round it meant, and the intent that carries it already knows.
    return ""


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------

# Recogniser output is inconsistent about these, and every one of them changes
# how a number parses.
_CLEANUP = [
    # Join thousands separators first: "3,000" must survive as one number, or
    # stripping the comma leaves "3 000" and the altitude parser sees nothing.
    (r"(\d),(\d{3})\b", r"\1\2"),
    (r"[,’']", " "),
    (r"\bdecimal\b", "point"),
    (r"\bpoint\b", " point "),
    (r"\bniner\b", "nine"),
    (r"\btree\b", "three"),
    (r"\bfower\b", "four"),
    (r"\bfife\b", "five"),
    (r"\bmayday mayday mayday\b", "mayday"),
    (r"\bpan pan pan\b", "panpan"),
    (r"\bpan-pan\b", "panpan"),
    (r"\bflight level\b", "flightlevel"),
    (r"\bf\.?l\.?\s*(\d)", r"flightlevel \1"),
    (r"\bthousand feet\b", "thousand"),
    (r"\bhundred feet\b", "hundred"),
]

_NUMBER_WORDS = {
    **_WORD_TO_DIGIT,
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19", "twenty": "20", "thirty": "30",
    "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70",
    "eighty": "80", "ninety": "90",
}


def normalize(text: str, keep_commas: bool = False) -> str:
    """Lower-case, expand shorthand, and make spacing predictable.

    ``keep_commas`` keeps each comma as a token of its own, for reading
    values: the recogniser writes a pause as a comma, and "zero six, six
    niner Mike" is two numbers, not "0669". :func:`_digit_runs` ends a run at
    it and drops it. The intent rules read the text without them.
    """
    out = (text or "").lower().strip()
    # Strip sentence punctuation, but never a decimal point: removing the dot
    # from "118.5" leaves "118 5", which is not a frequency any more.
    out = re.sub(r"[.!?]+(?!\d)", " ", out)
    for pattern, replacement in _CLEANUP:
        if keep_commas and pattern == r"[,’']":
            out = re.sub(r"(\d),(\d{3})\b", r"\1\2", out)
            out = re.sub(r",", " , ", out)
            out = re.sub(r"[’']", " ", out)
            continue
        out = re.sub(pattern, replacement, out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


# Digit words safe to collapse into a numeric run. The homophones that
# :mod:`speech` accepts when reading a known-numeric field -- "to"/"too" for 2,
# "for" for 4, "won" for 1, "ate" for 8 -- are deliberately excluded here,
# because this runs over free text: "reduce speed to two one zero" would
# otherwise collapse to 2210, and "ready for departure" to "ready 4 departure".
_RUN_DIGITS = {
    "zero": "0", "oh": "0",
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "niner": "9",
    "tree": "3", "fower": "4", "fife": "5",
}


def _digit_runs(text: str) -> str:
    """Collapse runs of spoken digits into numerals.

    ``"one two thousand"`` becomes ``"12 thousand"``; ``"three one left"``
    becomes ``"31 left"``. Multi-digit words like "twelve" are left alone here
    so the altitude parser can tell "one two thousand" from "twelve thousand"
    -- both mean 12,000, but only the first is a digit run.
    """
    tokens = text.split()
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            out.append("".join(run))
            run.clear()

    for token in tokens:
        if token == ",":
            # A pause ends a number, and is not itself a word.
            flush()
        elif token in _RUN_DIGITS:
            run.append(_RUN_DIGITS[token])
        elif token.isdigit() and len(token) == 1:
            run.append(token)
        else:
            flush()
            out.append(token)
    flush()
    return " ".join(out)


def _compact(text: str) -> str:
    """Letters and digits only, with phonetic and digit words as characters."""
    out = []
    for token in re.split(r"[\s,]+", text.lower()):
        if token in _ALPHABET_REVERSE:
            out.append(_ALPHABET_REVERSE[token].lower())
        elif token in _RUN_DIGITS:
            out.append(_RUN_DIGITS[token])
        else:
            out.append(re.sub(r"[^a-z0-9]", "", token))
    return "".join(out)


def _runway_from_hold_short(values: dict[str, Any]) -> None:
    """The runway of a hold-short, when it is the only runway named.

    "Hold short runway two two" names runway two two, and an instruction
    that owed both the runway and the hold-short was otherwise answered
    "negative, runway two two, read back" whatever the pilot said. A runway
    named on its own is still the assignment and is never overwritten --
    "hold short of one three right, runway zero four left" assigns 04L.
    """
    held = values.get("hold_short")
    if isinstance(held, str) and held and "runway" not in values:
        values["runway"] = held


# Words a number follows. A callsign form straight after one of these is the
# number, not the callsign.
_VALUE_WORDS = frozenset({
    "heading", "flightlevel", "level", "altitude", "squawk", "squawking",
    "code", "transponder", "qnh", "altimeter", "runway", "speed", "knots",
    "frequency", "decimal", "point", "climb", "descend", "maintain",
    "climbing", "descending", "contact",
})

# One character of a written callsign, as it is spoken.
_SPOKEN_CHAR = {
    **{d: w for w, d in (("zero", "0"), ("one", "1"), ("two", "2"),
                         ("three", "3"), ("four", "4"), ("five", "5"),
                         ("six", "6"), ("seven", "7"), ("eight", "8"),
                         ("nine", "9"))},
    **{"a": "alpha", "b": "bravo", "c": "charlie", "d": "delta", "e": "echo",
       "f": "foxtrot", "g": "golf", "h": "hotel", "i": "india",
       "j": "juliett", "k": "kilo", "l": "lima", "m": "mike",
       "n": "november", "o": "oscar", "p": "papa", "q": "quebec",
       "r": "romeo", "s": "sierra", "t": "tango", "u": "uniform",
       "v": "victor", "w": "whiskey", "x": "x-ray", "y": "yankee",
       "z": "zulu"},
}


def _spoken_count(words: list[str]) -> int | None:
    """A count said in words: "one five zero", "one hundred fifty", "87"."""
    words = [w for w in words if w and w != "and"]
    if not words:
        return None
    if all(w in _RUN_DIGITS or (w.isdigit() and len(w) == 1) for w in words):
        return int("".join(_RUN_DIGITS.get(w, w) for w in words))
    if len(words) == 1 and words[0].isdigit():
        return int(words[0])
    total = 0
    for word in words:
        if word == "hundred":
            total = max(total, 1) * 100
            continue
        value = _word_number(word)
        if value is None:
            return None
        total += value
    return total


_COUNT_WORDS = set(_RUN_DIGITS) | set(_NUMBER_WORDS) | {"hundred", "and"}


def _count_next_to(words: list[str], at: int, before: bool) -> int | None:
    """The count said just before (or just after) position ``at``."""
    taken: list[str] = []
    span = range(at - 1, -1, -1) if before else range(at + 1, len(words))
    for i in span:
        if words[i] in _COUNT_WORDS or words[i].isdigit():
            taken.append(words[i])
        else:
            break
    if before:
        taken.reverse()
    return _spoken_count(taken)


def extract_emergency_details(norm: str) -> dict[str, Any]:
    """Persons on board and fuel endurance, from the answer to a MAYDAY.

    "One hundred fifty souls on board, two hours of fuel" -> souls 150,
    fuel_minutes 120. Each clause is read on its own, so the number of
    people is never taken for the fuel.
    """
    found: dict[str, Any] = {}
    for clause in re.split(r",|\band\b(?= \w+ (?:souls|fuel))", norm):
        words = clause.split()
        for i, word in enumerate(words):
            if word in ("souls", "persons", "people", "passengers") and \
                    "souls" not in found:
                count = _count_next_to(words, i, before=True)
                if count is None and words[i + 1:i + 3] == ["on", "board"]:
                    count = _count_next_to(words, i + 2, before=False)
                if count:
                    found["souls"] = count
            if word == "pob" and "souls" not in found:
                count = _count_next_to(words, i, before=False)
                if count:
                    found["souls"] = count
        if "fuel" in words or "endurance" in words:
            minutes = 0
            for i, word in enumerate(words):
                if word in ("hour", "hours"):
                    minutes += 60 * (_count_next_to(words, i, before=True) or 0)
                elif word in ("minute", "minutes"):
                    minutes += _count_next_to(words, i, before=True) or 0
            if minutes:
                found["fuel_minutes"] = minutes
    return found


def _word_number(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return int(_NUMBER_WORDS[token]) if token in _NUMBER_WORDS else None


# --------------------------------------------------------------------------
# value extraction
# --------------------------------------------------------------------------

_RE_FLIGHT_LEVEL = re.compile(r"\bflightlevel\s*(\d{2,3})\b")
_RE_THOUSAND = re.compile(r"\b(\d{1,3})\s*thousand(?:\s*(?:and\s*)?(\d{1,2})\s*hundred)?\b")
_RE_HUNDRED = re.compile(r"\b(\d{1,2})\s*hundred\b")
_RE_BARE_ALT = re.compile(r"\b(\d{3,5})\s*(?:feet|ft)\b")


def extract_altitude(text: str) -> int | None:
    """Altitude in feet, from any of the forms controllers and pilots use."""
    collapsed = _digit_runs(normalize(text, keep_commas=True))
    # Never the code in "squawk seven thousand", in either reading below.
    collapsed = _RE_SQUAWK_THOUSAND.sub(" ", collapsed)
    text = re.sub(r"\b(?:squawk|squawking)\s+[a-z]+\s+thousand\b", " ",
                  (text or "").lower())

    m = _RE_FLIGHT_LEVEL.search(collapsed)
    if m:
        return int(m.group(1)) * 100

    # "level three five zero" with "flight" dropped, which pilots say often.
    # Only a two- or three-digit group not followed by a magnitude word can be
    # a flight level, so "level three thousand" is not caught here.
    m = re.search(
        r"\b(?:level|maintaining)\s+(\d{2,3})\b(?!\s*(?:thousand|hundred))", collapsed
    )
    if m and 30 <= int(m.group(1)) <= 600:
        return int(m.group(1)) * 100

    m = _RE_THOUSAND.search(collapsed)
    if m:
        thousands = int(m.group(1))
        hundreds = int(m.group(2)) if m.group(2) else 0
        # "one two thousand" collapses to "12 thousand"; a run longer than two
        # digits is not an altitude.
        if thousands <= 60:
            return thousands * 1000 + hundreds * 100

    # Compound words: "twelve thousand five hundred".
    m = re.search(
        r"\b([a-z]+)\s+thousand(?:\s+([a-z]+)\s+hundred)?\b", text
    )
    if m:
        thousands = _word_number(m.group(1))
        hundreds = _word_number(m.group(2)) if m.group(2) else 0
        if thousands is not None and thousands <= 60:
            return thousands * 1000 + (hundreds or 0) * 100

    m = _RE_BARE_ALT.search(collapsed)
    if m:
        return int(m.group(1))

    m = _RE_HUNDRED.search(collapsed)
    if m:
        return int(m.group(1)) * 100

    # A bare numeral that looks like an altitude, e.g. "maintain 3000".
    m = re.search(r"\b(\d{3,5})\b", collapsed)
    if m:
        value = int(m.group(1))
        if 100 <= value <= 60000 and value % 100 == 0:
            return value
    return None


_RE_HEADING = re.compile(
    r"\b(?:heading|hdg|turn(?:\s+(?:left|right))?(?:\s+to)?|fly)\s+(?:heading\s+)?(\d{1,3})\b"
)


def extract_heading(text: str) -> int | None:
    collapsed = _digit_runs(normalize(text, keep_commas=True))
    m = _RE_HEADING.search(collapsed)
    if not m:
        return None
    value = int(m.group(1))
    if 0 < value <= 360:
        return value % 360 or 360
    return None


_RE_FREQ_POINT = re.compile(r"\b(1[0-3]\d)\s*point\s*(\d{1,3})\b")
_RE_FREQ_PLAIN = re.compile(r"\b(1[0-3]\d\.\d{1,3})\b")


def extract_frequency(text: str) -> float | None:
    """A VHF frequency, from either "one two one point niner" or "121.9"."""
    collapsed = _digit_runs(normalize(text, keep_commas=True))

    m = _RE_FREQ_POINT.search(collapsed)
    if m:
        whole, frac = m.group(1), m.group(2)
        value = float(f"{whole}.{frac}")
        return value if 118.0 <= value <= 136.999 else None

    m = _RE_FREQ_PLAIN.search(normalize(text))
    if m:
        value = float(m.group(1))
        return value if 118.0 <= value <= 136.999 else None
    return None


_RE_HOLD_SHORT = re.compile(
    r"\bhold(?:ing)?\s+short\s+(?:of\s+)?(?:runway\s+)?"
    r"(\d{1,2})\s*(left|right|centre|center|l|r|c)?\b"
)

_RE_RUNWAY = re.compile(
    r"\brunway\s+(\d{1,2})\s*(left|right|centre|center|l|r|c)?\b"
)
# "Cleared ILS two five right": the approach names the runway, and a pilot
# reading it back seldom says the word. Only straight after an approach type,
# so an altitude or a heading is never taken for one.
_RE_APPROACH_RUNWAY = re.compile(
    r"\b(?:ils|rnav|gps|rnp|loc|localizer|localiser|vor|ndb|visual)"
    r"(?:\s+approach)?\s+(\d{1,2})\s*(left|right|centre|center|l|r|c)?\b"
)
_SIDE = {"left": "L", "l": "L", "right": "R", "r": "R",
         "centre": "C", "center": "C", "c": "C"}




def extract_hold_short(text: str) -> str | bool | None:
    """Which runway the pilot said they would hold short of.

    The runway when they named one, ``True`` when they said they were holding
    short without naming it, and ``None`` when they said nothing about it. The
    three are different answers and the readback checker needs all three.
    """
    collapsed = _digit_runs(normalize(text, keep_commas=True))
    m = _RE_HOLD_SHORT.search(collapsed)
    if m:
        number = int(m.group(1))
        if 1 <= number <= 36:
            side = _SIDE.get((m.group(2) or "").lower(), "")
            return f"{number:02d}{side}"
    if re.search(r"\bhold(?:ing)?\s+short\b", collapsed):
        return True
    return None


def extract_runway(text: str) -> str | None:
    """The runway assigned, which is never the one being held short of.

    "Hold short of runway zero four left" names a runway the aircraft must not
    enter. Reading it as the runway assignment meant that answering a
    hold-short correction contradicted the assignment, and the exchange could
    not be got out of.
    """
    collapsed = _digit_runs(normalize(text, keep_commas=True))
    collapsed = _RE_HOLD_SHORT.sub(" ", collapsed)
    m = _RE_RUNWAY.search(collapsed) or _RE_APPROACH_RUNWAY.search(collapsed)
    if not m:
        return None
    number = int(m.group(1))
    if not 1 <= number <= 36:
        return None
    side = _SIDE.get((m.group(2) or "").lower(), "")
    return f"{number:02d}{side}"


_RE_SQUAWK = re.compile(r"\b(?:squawk|squawking|transponder|code)\s+(\d{4})\b")


_RE_SQUAWK_THOUSAND = re.compile(r"\b(?:squawk|squawking)\s+(\d)\s+thousand\b")


def extract_squawk(text: str) -> str | None:
    collapsed = _digit_runs(normalize(text, keep_commas=True))
    # "Squawk seven thousand": a code said as a number, the way 7000 and
    # 2000 usually are. It used to come out as an altitude.
    m = _RE_SQUAWK_THOUSAND.search(collapsed)
    if m and m.group(1) in "01234567":
        return f"{m.group(1)}000"
    m = _RE_SQUAWK.search(collapsed)
    if m and all(c in "01234567" for c in m.group(1)):
        return m.group(1)
    # A bare four-digit octal group right after "squawk" that got split.
    m = re.search(r"\bsquawk\w*\s+((?:[0-7]\s*){4})\b", collapsed)
    if m:
        code = re.sub(r"\s+", "", m.group(1))
        if len(code) == 4:
            return code
    return None


_RE_SPEED = re.compile(r"\b(?:speed|knots?|kts?)\D{0,12}?(\d{2,3})\b|\b(\d{2,3})\s*knots?\b")


def extract_speed(text: str) -> int | None:
    collapsed = _digit_runs(normalize(text, keep_commas=True))
    m = _RE_SPEED.search(collapsed)
    if not m:
        return None
    value = int(m.group(1) or m.group(2))
    return value if 60 <= value <= 400 else None


def extract_atis_letter(text: str) -> str:
    """The information letter, from "with information Bravo" or "have Bravo"."""
    words = normalize(text).split()
    for i, word in enumerate(words):
        if word in ("information", "info", "atis", "with", "have", "got"):
            for candidate in words[i + 1:i + 3]:
                letter = _ALPHABET_REVERSE.get(candidate)
                if letter and letter.isalpha():
                    return letter
    return ""


def extract_altimeter(text: str) -> float | None:
    """Pressure setting: 4 digits of inches (2992) or hectopascals (1013)."""
    collapsed = _digit_runs(normalize(text, keep_commas=True))
    # Three figures as well as four: a QNH below a thousand is "niner niner
    # zero", as it is said.
    m = re.search(r"\b(?:altimeter|qnh)\s+(\d{3,4})\b", collapsed)
    if not m:
        return None
    raw = int(m.group(1))
    if 2700 <= raw <= 3200:
        return raw / 100.0
    if 900 <= raw <= 1100:
        return float(raw)
    return None


def extract_fix(text: str) -> str:
    """A named waypoint the pilot asked for, e.g. "direct CAMRN"."""
    m = re.search(r"\bdirect\s+(?:to\s+)?([a-z]{3,6})\b", normalize(text))
    if not m:
        return ""
    word = m.group(1)
    if word in _NUMBER_WORDS or word in ("the", "for", "and"):
        return ""
    return word.upper()


# --------------------------------------------------------------------------
# intent rules
# --------------------------------------------------------------------------

# Ordered most specific first: an emergency call that also mentions landing is
# an emergency, and a readback that also contains "with you" is a check-in.
_RULES: list[tuple[Intent, str]] = [
    (Intent.EMERGENCY_DETAILS,
     r"\b((souls|persons|people|passengers) on board|on board \d|pob|"
     r"fuel (remaining|endurance|for)|endurance|"
     r"(hours?|minutes) of fuel|fuel \d)"),
    (Intent.EMERGENCY, r"\b(mayday|declaring an emergency|emergency)\b"),
    (Intent.URGENCY, r"\b(panpan|pan pan|declaring an urgency)\b"),
    (Intent.MINIMUM_FUEL, r"\b(minimum fuel|min fuel|fuel emergency)\b"),

    (Intent.GOING_AROUND, r"\b(going around|go(ing)? missed|executing (the )?missed)\b"),
    (Intent.MISSED_APPROACH, r"\bmissed approach\b"),

    (Intent.SAY_AGAIN, r"\b(say again|repeat that|did ?n'?o?t (copy|catch)|"
                       r"unreadable|you'?re broken|garbled)\b"),
    (Intent.RADIO_CHECK, r"\b(radio check|how do you (read|hear)|comm check)\b"),

    # Pilots ask for a clearance a dozen ways and rarely the textbook way:
    # "ready to copy", "IFR to Boston", "clearance on request".
    # Start-up is asked for here, as it is in French ("demande la mise en
    # route"): at a field with a delivery position the two are one call, and
    # asking for one without the other is not something a controller hears.
    (Intent.REQUEST_CLEARANCE, r"\b(ifr clearance|"
                               r"clearance (on request|please|when ready|available)|"
                               r"request (our |my )?(ifr )?clearance|"
                               r"ready to copy|ready for (our |my )?clearance|"
                               r"ifr (to|for) \w+|"
                               r"like (our|my) (ifr )?clearance|"
                               r"request(ing)? (our |my )?(engine )?start( ?-? ?up)?|"
                               r"ready for (our |my )?(engine )?start( ?-? ?up)?)\b"),
    # "Request VFR" on its own is a departure; "request VFR advisories" is not,
    # and reading it as one sends an aircraft in the circuit out of the zone.
    (Intent.REQUEST_VFR_DEPARTURE,
     r"\b(vfr (departure|to the)|"
     r"request vfr(?! (advisor|flight following|traffic))|"
     r"vfr (north|south|east|west)bound)\b"),
    # Before the pushback request: "pushback approved" is the pilot
    # acknowledging one, not asking for it.
    (Intent.ACKNOWLEDGE, r"\b(push ?back( and start)?|push and start|"
                         r"start ?up) approved\b"),
    (Intent.REQUEST_PUSHBACK, r"\b(push ?back|request push|ready for push)\b"),
    (Intent.REQUEST_TAXI_TO_PARKING,
     r"\b(taxi to (the )?(gate|ramp|parking|fbo|terminal)|"
     r"request (taxi to )?(parking|the gate|the ramp))\b"),
    (Intent.REQUEST_TAXI, r"\b(request taxi|ready (to|for) taxi|"
                          r"taxi for (departure|takeoff)|"
                          r"taxi to (the )?(active|runway))\b"),
    # Circuit work has to be read before the takeoff and landing requests: a
    # pilot who says "ready for the option" is asking for both at once, and
    # "stop and go" is not the "full stop" that means a landing.
    (Intent.REQUEST_PATTERN_WORK,
     r"\b(closed traffic|(stay|remain|staying|remaining) in the (pattern|circuit)|"
     r"touch.?and.?go|stop.?and.?go|pattern work|circuits?|"
     r"(request|ready for|for) the option|multiple approaches|"
     r"low approach)\b"),
    (Intent.REQUEST_TRANSITION,
     r"\b(request(ing)? (a )?transition|transition(ing)? (through|your|the)|"
     r"transit (through|your|the)|"
     r"through your (class )?(delta|charlie|bravo|airspace|zone)|"
     r"(request|like) to (cross|transit|overfly)|overfly (the )?(field|airport))\b"),
    (Intent.REQUEST_FLIGHT_FOLLOWING,
     r"\b(flight following|vfr (advisor(y|ies)|flight following)|"
     r"radar advisor(y|ies)|request(ing)? advisories|basic service|"
     r"traffic advisories)\b"),

    (Intent.REQUEST_TAKEOFF, r"\b(ready for (departure|takeoff)|"
                             r"request (departure|takeoff)|holding short.*ready|"
                             r"ready to go)\b"),

    (Intent.REQUEST_FREQUENCY_CHANGE,
     r"\b(request frequency change|frequency change (request|please)|"
     r"like to change frequency)\b"),
    (Intent.CANCEL_IFR, r"\b(cancel (my |our )?ifr)\b"),

    (Intent.REPORT_FIELD_IN_SIGHT,
     r"\b((field|airport|runway) (is )?in sight|have the (field|airport|runway))\b"),
    (Intent.REPORT_TRAFFIC_IN_SIGHT,
     r"\b(traffic in sight|have the traffic|got the traffic)\b"),
    (Intent.REPORT_ESTABLISHED,
     r"\b(established (on )?(the )?(localizer|localiser|ils|approach)|"
     r"we'?re established)\b"),

    (Intent.REQUEST_APPROACH, r"\b(request (the )?(ils|rnav|gps|visual|vor|"
                              r"localizer|localiser)( approach)?|"
                              r"request (an |the )?approach|"
                              r"like the (ils|visual|rnav))\b"),
    (Intent.REQUEST_VECTORS, r"\b(request vectors|vectors (to|for))\b"),
    (Intent.REQUEST_LANDING, r"\b(inbound (for )?(landing|full stop)|"
                             r"request (landing|full stop)|for landing|"
                             r"request (to )?land|"
                             r"full stop|on final|"
                             r"inbound (to|for) (the )?(field|airport))\b"),
    (Intent.REQUEST_DIRECT, r"\b(request direct|direct to)\b"),
    (Intent.REQUEST_DESCENT, r"\b(request (descent|lower|to descend)|"
                             r"like (to descend|lower))\b"),
    (Intent.REQUEST_CLIMB, r"\b(request (climb|higher|to climb)|like (to climb|higher))\b"),

    # Checking in on a new frequency. Pilots do this a dozen ways: "with you",
    # "checking in", or simply stating the level they are at or passing. The
    # level may be a numeral or spelled out, because these rules run before the
    # digit words are collapsed.
    (Intent.CHECK_IN,
     r"\b(with you|checking in|"
     r"good (morning|afternoon|evening|day)"
     r"[, ]+\w+ (climbing|descending|level|passing)|"
     r"(level|maintaining|passing|leaving|climbing to|descending to|"
     r"climbing through|descending through|climbing|descending)\s+"
     r"(flightlevel|\d|one|two|three|four|five|six|seven|eight|nine|ten|"
     r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|"
     r"nineteen|twenty|thirty|zero))\b"),
    (Intent.POSITION_REPORT, r"\b((?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|twenty five|thirty|thirty five|forty|fifty|sixty) miles? (north|south|east|west|"
                             r"northeast|northwest|southeast|southwest)|"
                             r"position report|overhead the field|"
                             r"(entering|midfield|abeam|turning|joining|on) "
                             r"(the )?(downwind|base|crosswind|upwind)|"
                             r"turning final|short final|abeam the numbers|"
                             r"on the (four five|45))\b"),

    (Intent.UNABLE, r"\bunable\b"),
    (Intent.STANDBY, r"\bstand ?by\b"),
    (Intent.AFFIRMATIVE, r"\b(affirmative|affirm|that'?s correct|correct)\b"),
    (Intent.NEGATIVE, r"\b(negative|that'?s (wrong|incorrect))\b"),
    # A clearance restated is how many pilots acknowledge it: "cleared to
    # land", "push and start approved". They were not understood, and got
    # the list of calls the controller was "expecting".
    (Intent.ACKNOWLEDGE, r"\b(roger|wilco|copy that|copied|understood|"
                         r"cleared to land|push(back)?( and start)? approved|"
                         r"start ?up approved)\b"),
]

_COMPILED = [(intent, re.compile(pattern)) for intent, pattern in _RULES]

# Words that signal the pilot is reading an instruction back rather than
# requesting something new.
_READBACK_MARKERS = re.compile(
    r"\b(cleared|climb|descend|maintain|turn|fly|heading|squawk|taxi|"
    r"hold short|line up|contact|altimeter|qnh|runway|via|direct|"
    r"reduce|increase|speed|cross)\b"
)


class IntentParser:
    """Turns a recognised transmission into a :class:`ParsedIntent`."""

    def __init__(self, own_callsign_variants: list[str] | None = None):
        self.callsign_variants = [v.lower() for v in (own_callsign_variants or [])]
        self._compiled_fr = None
        self._compiled_local: dict[str, list] = {}

    def set_callsign(self, variants: list[str]) -> None:
        self.callsign_variants = [v.lower() for v in variants if v]
        self._own_forms = None

    def _own(self) -> list[str]:
        """Every way the pilot says their own callsign, normalised, longest
        first: the full forms, and the tail of the written one digit by digit
        -- "one one four" for DLH114, "six niner Mike" for N169M -- which is
        how a callsign is said at the end of a readback and which none of
        the full forms is."""
        if getattr(self, "_own_forms", None) is not None:
            return self._own_forms
        forms = {normalize(v) for v in self.callsign_variants if v}
        for variant in self.callsign_variants:
            written = re.fullmatch(r"([a-z]{1,3})(\d+[a-z]*)", variant or "")
            if not written:
                continue
            tail = written.group(2)
            for chars in {tail, tail[-3:]}:
                if len(chars) >= 2:
                    spoken = " ".join(
                        _SPOKEN_CHAR.get(c, c) for c in chars)
                    forms.add(normalize(spoken))
        self._own_forms = sorted((f for f in forms if f), key=len, reverse=True)
        return self._own_forms

    def _raw_without_callsign(self, raw: str) -> str:
        """The same, for a language with its own normaliser: the full forms
        taken off either end of the raw text, a comma left in their place."""
        text = (raw or "").strip()
        for form in sorted(self.callsign_variants, key=len, reverse=True):
            if not form:
                continue
            pattern = re.escape(form)
            text = re.sub(rf"(?i)^\s*{pattern}\b[\s,]*", ", ", text)
            text = re.sub(rf"(?i)[\s,]*\b{pattern}[\s.!?]*$", ", ", text)
        return text.strip(" ,")

    def _without_callsign(self, raw: str) -> str:
        """The transmission as values are read from it: commas kept as
        boundaries, and the pilot's own callsign taken off either end, where
        pilots put it. Only the ends, so a heading of one one four in the
        middle of a readback is never taken for DLH114."""
        text = normalize(raw, keep_commas=True)
        forms = self._own()
        if not forms:
            return text
        alternatives = "|".join(re.escape(f) for f in forms)
        text = re.sub(rf"^(?:{alternatives})\b\s*,?\s*", " , ", text)
        # Not at the end when the word before it asks for a number: in
        # "heading one one four" from DLH114 the digits are the heading.
        trailing = re.search(rf"(\S+)?\s*(,)?\s*\b(?:{alternatives})\s*$", text)
        if trailing and not (trailing.group(2) is None
                             and (trailing.group(1) or "") in _VALUE_WORDS):
            text = text[:trailing.start()] + " " + (trailing.group(1) or "") + " , "
        return re.sub(r"\s+", " ", text).strip(" ,")

    def _french_rules(self):
        """Compile the French rule table on first use."""
        if self._compiled_fr is None:
            from .intents_fr import RULES_FR, number_alternation_fr

            numbers = number_alternation_fr()
            self._compiled_fr = [
                (Intent(name), re.compile(pattern.replace("{number}", numbers)))
                for name, pattern in RULES_FR
            ]
        return self._compiled_fr

    def _local_rules(self, grammar):
        """Compile a language's rule table on first use."""
        compiled = self._compiled_local.get(grammar.language)
        if compiled is None:
            from .intents_local import rules_for

            compiled = [(Intent(name), re.compile(pattern))
                        for name, pattern in rules_for(grammar)]
            self._compiled_local[grammar.language] = compiled
        return compiled

    # ------------------------------------------------------------------

    def parse(self, text: str, language: str = "en") -> ParsedIntent:
        """Understand one transmission, in whichever language it was spoken."""
        language = (language or "en").lower()[:2]
        if language == "fr":
            return self._parse_french(text)
        if language != "en":
            from .intents_local import grammar_for

            grammar = grammar_for(language)
            if grammar is not None:
                return self._parse_local(text, grammar)

        raw = text or ""
        norm = normalize(raw)
        result = ParsedIntent(text=raw, normalized=norm, language="en")
        if not norm:
            return result

        result.callsign_matched = self._callsign_present(norm)
        result.station_addressed = self._station(norm)
        # Read without the callsign, so its digits never join a value; then
        # any value that reading lost is taken from the whole transmission,
        # because a callsign form that was really the value -- the heading a
        # DLH114 was given as one one four -- must not take it with it.
        result.values = self._values(self._without_callsign(raw))
        result.values.update(extract_emergency_details(self._without_callsign(raw)))
        for key, value in self._values(normalize(raw, keep_commas=True)).items():
            result.values.setdefault(key, value)

        for intent, pattern in _COMPILED:
            if pattern.search(norm):
                result.intent = intent
                result.confidence = 0.85 if result.callsign_matched else 0.6
                break
        else:
            # No rule fired. If the transmission carries instruction words and
            # numbers, it is almost certainly a readback.
            if _READBACK_MARKERS.search(norm) and result.values:
                result.intent = Intent.READBACK
                result.confidence = 0.7 if result.callsign_matched else 0.5
            # A frequency on its own is the handoff read back: "one two one
            # point six". Nothing else is said with nothing but a frequency.
            elif result.values and set(result.values) <= {"frequency"}:
                result.intent = Intent.READBACK
                result.confidence = 0.7 if result.callsign_matched else 0.5
            # A level and the callsign and nothing to read back: a check-in,
            # "Boston Approach, Delta twelve thirty four, flight level eight
            # zero".
            elif (result.callsign_matched and "altitude_ft" in result.values
                    and set(result.values) <= {"altitude_ft",
                                               "target_altitude_ft"}):
                result.intent = Intent.CHECK_IN
                result.confidence = 0.7

        # A transmission that both reads something back and carries a request
        # keyword is still a readback if it has no request verb of its own.
        if result.intent in (Intent.ACKNOWLEDGE, Intent.AFFIRMATIVE) and result.values:
            if _READBACK_MARKERS.search(norm):
                result.intent = Intent.READBACK

        _runway_from_hold_short(result.values)
        result.readback_items = dict(result.values)
        result.rules = flight_rules(norm)
        return result

    def _parse_local(self, text: str, grammar) -> ParsedIntent:
        """The same pipeline against a phrasebook language's rule table.

        Identical in shape to the English and French passes, because the point
        of a :class:`~wilcoatc.atc.intents_local.Grammar` is that only the
        vocabulary changes.
        """
        from . import intents_local as loc

        raw = text or ""
        norm = loc.normalize(raw, grammar)
        result = ParsedIntent(text=raw, normalized=norm,
                              language=grammar.language)
        if not norm:
            return result

        result.callsign_matched = self._callsign_present(norm)
        station = re.search(grammar.stations, norm)
        result.station_addressed = station.group(1) if station else ""
        result.values = loc.values(
            loc.normalize(self._raw_without_callsign(raw), grammar), grammar)

        for intent, pattern in self._local_rules(grammar):
            if pattern.search(norm):
                result.intent = intent
                result.confidence = 0.85 if result.callsign_matched else 0.6
                break
        else:
            if re.search(grammar.readback, norm) and result.values:
                result.intent = Intent.READBACK
                result.confidence = 0.7 if result.callsign_matched else 0.5

        if result.intent in (Intent.ACKNOWLEDGE, Intent.AFFIRMATIVE) and result.values:
            if re.search(grammar.readback, norm):
                result.intent = Intent.READBACK

        _runway_from_hold_short(result.values)
        result.readback_items = dict(result.values)
        result.rules = flight_rules(norm)
        return result

    def _parse_french(self, text: str) -> ParsedIntent:
        """The same pipeline against the French rule table."""
        from .intents_fr import (
            READBACK_MARKERS_FR, STATION_WORDS_FR, normalize_fr, values_fr,
        )

        raw = text or ""
        norm = normalize_fr(raw)
        result = ParsedIntent(text=raw, normalized=norm, language="fr")
        if not norm:
            return result

        result.callsign_matched = self._callsign_present(norm)
        station = STATION_WORDS_FR.search(norm)
        result.station_addressed = station.group(1) if station else ""
        result.values = values_fr(self._raw_without_callsign(raw))

        for intent, pattern in self._french_rules():
            if pattern.search(norm):
                result.intent = intent
                result.confidence = 0.85 if result.callsign_matched else 0.6
                break
        else:
            if READBACK_MARKERS_FR.search(norm) and result.values:
                result.intent = Intent.READBACK
                result.confidence = 0.7 if result.callsign_matched else 0.5

        if result.intent in (Intent.ACKNOWLEDGE, Intent.AFFIRMATIVE) and result.values:
            if READBACK_MARKERS_FR.search(norm):
                result.intent = Intent.READBACK

        _runway_from_hold_short(result.values)
        result.readback_items = dict(result.values)
        result.rules = flight_rules(norm)
        return result

    # ------------------------------------------------------------------

    def _callsign_present(self, norm: str) -> bool:
        """Whether the pilot identified themselves.

        Matching is deliberately loose: recognition mangles callsigns often,
        and refusing to answer an aircraft because Whisper heard "Delta twelve
        thirty for" would be far more annoying than answering a transmission
        that was not quite addressed to us.
        """
        for variant in self.callsign_variants:
            if variant and variant in norm:
                return True
        # The tail alone, at either end: "..., six niner Mike".
        for form in self._own():
            if norm.startswith(form + " ") or norm.endswith(" " + form) \
                    or norm == form:
                if len(form.split()) >= 2:
                    return True
        # Punctuation is not spoken, and the languages do not normalise it the
        # same way: French turns "F-GJIM" into "f gjim" while English leaves
        # the hyphen where it is. A registration would match in one language
        # and not the other, so the letters and digits are compared on their
        # own as well.
        squashed = re.sub(r"[^a-z0-9]", "", norm)
        for variant in self.callsign_variants:
            bare = re.sub(r"[^a-z0-9]", "", variant)
            if len(bare) >= 4 and bare in squashed:
                return True
        # A flight identification with letters in it comes back from the
        # recogniser as "7A8", "7 alpha 8" or "seven alpha eight", and only
        # the last is a variant. Reducing the alphabet and the digit words to
        # characters on both sides makes all three the same "7a8".
        compact = _compact(norm)
        for variant in self.callsign_variants:
            bare = _compact(variant)
            if len(bare) >= 4 and bare in compact:
                return True
        # Fall back to a fuzzy check on the digits alone.
        for variant in self.callsign_variants:
            digits = re.sub(r"\D", "", variant)
            if len(digits) >= 3 and digits in re.sub(r"\D", "", norm):
                return True
        return False

    _STATION_WORDS = re.compile(
        r"\b(ground|tower|clearance|delivery|approach|departure|"
        r"cent(?:er|re)|control|radar|director|radio|traffic|unicom)\b"
    )

    def _station(self, norm: str) -> str:
        m = self._STATION_WORDS.search(norm)
        return m.group(1) if m else ""

    def _values(self, norm: str) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for key, extractor in (
            ("altitude_ft", extract_altitude),
            ("heading", extract_heading),
            ("frequency", extract_frequency),
            ("runway", extract_runway),
            ("squawk", extract_squawk),
            ("speed_kt", extract_speed),
            ("altimeter", extract_altimeter),
        ):
            found = extractor(norm)
            if found is not None:
                values[key] = found

        # A check-in states two altitudes: the one being left and the one
        # cleared to. "Climbing through five thousand for one two thousand"
        # means the aircraft is at 5,000 and cleared to 12,000.
        m = re.search(r"\bfor\s+(.{0,28}?)(?=$|\s(?:with|and|,))", norm)
        if m:
            target = extract_altitude(m.group(1))
            if target is not None and target != values.get("altitude_ft"):
                values["target_altitude_ft"] = target

        letter = extract_atis_letter(norm)
        if letter:
            values["atis_letter"] = letter
        fix = extract_fix(norm)
        if fix:
            values["fix"] = fix

        held = extract_hold_short(norm)
        if held is not None:
            values["hold_short"] = held
        if re.search(r"\b(left|right) traffic\b", norm):
            values["pattern_direction"] = (
                "left" if "left traffic" in norm else "right"
            )
        for leg in ("downwind", "base", "final", "upwind", "crosswind"):
            if re.search(rf"\b{leg}\b", norm):
                values["pattern_leg"] = leg
                break
        return values


__all__ = [
    "Intent", "ParsedIntent", "IntentParser", "normalize", "flight_rules",
    "extract_altitude", "extract_heading", "extract_frequency",
    "extract_runway", "extract_squawk", "extract_speed",
    "extract_atis_letter", "extract_altimeter", "extract_fix",
    "extract_hold_short",
]
