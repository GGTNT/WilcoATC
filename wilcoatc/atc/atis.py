"""Automatic Terminal Information Service.

The ATIS is the first thing a pilot listens to and the easiest thing to get
visibly wrong, because it states the runway in use, the pressure setting and
the information letter that the controller will later expect to hear read back.
All three have to agree with what the rest of the system believes.

The broadcast is assembled in the published order (FAA JO 7110.65 2-9-3 for the
US form, ICAO Annex 11 for the international one) and re-issued with the next
letter of the alphabet whenever the conditions behind it change materially --
not on a timer, because a controller does not roll the letter for a one-knot
wind shift.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..navdata.db import (Airport, NavDB,
                          transition_altitude as transition_for)
from . import speech as sp
from .phraseology import Phraseology, Weather

# The information letter cycles through the phonetic alphabet.
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass
class AtisReport:
    """One issued ATIS, with everything derived from it."""

    airport: str
    letter: str
    text: str
    weather: Weather
    landing_runways: list[str] = field(default_factory=list)
    departing_runways: list[str] = field(default_factory=list)
    issued_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    issued_monotonic: float = field(default_factory=time.monotonic)

    @property
    def letter_spoken(self) -> str:
        return sp.letters(self.letter)

    approach: str = ""

    @property
    def approach_in_use(self) -> str:
        return self.approach if self.landing_runways else ""


def _round_to(value: float, step: int) -> int:
    return int(round(float(value) / step) * step)


class AtisGenerator:
    """Builds and maintains the ATIS for one airport."""

    def __init__(
        self,
        navdb: NavDB,
        airport: Airport,
        phraseology: Phraseology | None = None,
        *,
        transition_altitude: int = 0,
        start_letter: str | None = None,
    ):
        self.navdb = navdb
        self.airport = airport
        self.phrase = phraseology or Phraseology(airport.dialect)
        # Where the airport is, unless the caller knows better. It used to be
        # eighteen thousand feet whatever the airport was, which is the
        # American figure and made a European ATIS quote a transition level
        # nobody there uses.
        self.transition_altitude = (
            transition_altitude or transition_for(airport.ident))
        self._letter_index = (
            LETTERS.index(start_letter.upper())
            if start_letter and start_letter.upper() in LETTERS
            else self._letter_from_clock()
        )
        self.current: AtisReport | None = None
        # Which approach a landing runway has, from the same place the
        # controllers ask (``ControllerBrain.approach_for``), so the broadcast
        # never promises an ILS the clearance will not give. The ILS where the
        # engine has not said.
        self.approach_for = lambda runway: "ILS"
        # And the runway, from the same place. Unset, the wind decides.
        self.runway_for = None

    def _letter_from_clock(self) -> int:
        """Start on a plausible letter rather than always on Alpha."""
        now = datetime.now(timezone.utc)
        return (now.hour * 2 + now.minute // 30) % 26

    # ------------------------------------------------------------------

    def update(self, weather: Weather, force: bool = False) -> AtisReport:
        """Issue a new ATIS if conditions have moved enough to warrant one."""
        if self.current is not None and not force:
            if not self._materially_changed(self.current.weather, weather):
                return self.current
        return self.issue(weather)

    def _materially_changed(self, old: Weather, new: Weather) -> bool:
        """Whether the change justifies a new information letter.

        The thresholds mirror what actually triggers a re-record: a pressure
        change of a hundredth of an inch, a wind shift big enough to matter for
        runway selection, a visibility or ceiling change that crosses a
        reportable step.
        """
        if abs(old.altimeter_inhg - new.altimeter_inhg) >= 0.01:
            return True
        if abs(old.wind_kt - new.wind_kt) >= 5:
            return True
        if abs(((old.wind_dir - new.wind_dir) + 540) % 360 - 180) >= 30 and new.wind_kt >= 5:
            return True
        if _round_to(old.visibility_sm, 1) != _round_to(new.visibility_sm, 1):
            return True
        if [c[0] for c in old.clouds] != [c[0] for c in new.clouds]:
            return True
        if abs(old.temperature_c - new.temperature_c) >= 2:
            return True
        # A runway change always forces a new letter.
        return (self._runway_choice(old) != self._runway_choice(new)
                or self._runway_choice(old, True) != self._runway_choice(new, True))

    def _runway_choice(self, weather: Weather,
                       departing: bool = False) -> tuple[str, ...]:
        if self.runway_for is not None:
            end = self.runway_for(weather, departing)
            if not end:
                return ()
        else:
            best = self.navdb.best_runway(
                self.airport.ident, weather.wind_dir, weather.wind_kt,
                variable=weather.variable, departing=departing,
                gust_kt=weather.gust_kt,
            )
            if not best:
                return ()
            end, _rw = best
        parallels = self.navdb.parallel_runways(self.airport.ident, end)
        return tuple([end] + sorted(parallels))

    # ------------------------------------------------------------------

    def issue(self, weather: Weather) -> AtisReport:
        """Force a new ATIS with the next letter."""
        letter = LETTERS[self._letter_index % 26]
        self._letter_index += 1

        runways = list(self._runway_choice(weather))
        # A field with a preferential system lands on one runway and departs
        # from another (Brussels 25L and 25R), and the broadcast has to name
        # the one the departure will be cleared from.
        departing = list(self._runway_choice(weather, departing=True)) or runways
        text = self.compose(letter, weather, runways, departing)

        report = AtisReport(
            airport=self.airport.ident,
            letter=letter,
            text=text,
            weather=weather,
            landing_runways=runways,
            departing_runways=departing,
            approach=self._approach_kind(runways),
        )
        self.current = report
        return report

    # ------------------------------------------------------------------

    def compose(self, letter: str, wx: Weather, runways: list[str],
                departing: list[str] | None = None) -> str:
        """Assemble the spoken broadcast."""
        split = bool(runways and departing and departing[0] != runways[0])
        if getattr(self.phrase, "dialect", "") == "fr":
            return self._compose_french(letter, wx, runways,
                                        departing if split else None)
        p = self.phrase
        now = datetime.now(timezone.utc)
        name = self.airport.spoken

        parts: list[str] = []

        # --- identification and time ---
        if p.dialect == "faa":
            parts.append(
                f"{name} airport information {sp.letters(letter)}, "
                f"{sp.time_utc(now.hour, now.minute, p.digit_dialect)}"
            )
        else:
            parts.append(
                f"This is {name} information {sp.letters(letter)}, "
                f"{sp.time_utc(now.hour, now.minute, p.digit_dialect)}"
            )

        # --- runways in use ---
        if split:
            parts.append(f"landing runway {p.rwy(runways[0])}, "
                         f"departure runway {p.rwy(departing[0])}")
            if p.dialect == "icao":
                parts.append(f"expect {self._approach_type(runways)} approach")
        elif runways:
            if len(runways) == 1:
                parts.append(f"landing and departing runway {p.rwy(runways[0])}")
            else:
                spoken = " and ".join(p.rwy(r) for r in runways)
                parts.append(f"landing and departing runways {spoken}")
            if p.dialect == "icao":
                parts.append(f"expect {self._approach_type(runways)} approach")
        if runways and p.dialect == "faa":
            # JO 7110.65 2-9-3: the instrument approach in use, by name.
            parts.append(f"{self._approach_type(runways)} runway "
                         f"{p.rwy(runways[0])} approach in use")

        # --- wind ---
        parts.append(p.wind(wx))

        # --- visibility ---
        if p.dialect == "faa":
            parts.append(sp.visibility(wx.visibility_sm, p.digit_dialect))
        else:
            metres = int(min(9999, round(wx.visibility_sm * 1609.34 / 100.0) * 100))
            if metres >= 9999:
                parts.append("visibility one zero kilometres or more")
            else:
                parts.append(f"visibility {p.d(metres)} metres")

        # --- cloud ---
        parts.append(self._clouds(wx))

        # --- temperature and dew point ---
        parts.append(
            f"temperature {self._signed(wx.temperature_c)}, "
            f"dew point {self._signed(wx.dewpoint_c)}"
        )

        # --- pressure ---
        parts.append(p.pressure(
            wx.altimeter_inhg if p.dialect == "faa" else wx.qnh_hpa
        ))

        # --- closing ---
        if p.dialect == "faa":
            parts.append(
                f"advise on initial contact you have information {sp.letters(letter)}"
            )
        else:
            parts.append(f"acknowledge receipt of information {sp.letters(letter)}")

        return ". ".join(part.strip().rstrip(".") for part in parts if part.strip()) + "."

    def _compose_french(self, letter: str, wx: Weather,
                        runways: list[str],
                        departing: list[str] | None = None) -> str:
        """The French form of the broadcast.

        Order and wording follow the French AIP rather than being a
        translation: the identification carries the airport and the letter, the
        runway in use comes next, and the closing asks the pilot to acknowledge
        the letter on first contact.
        """
        from . import speech_fr as sfr

        p = self.phrase
        now = datetime.now(timezone.utc)
        parts: list[str] = [
            f"Ici {self.airport.spoken}, information {sfr.letters(letter)}, "
            f"{sfr.time_utc(now.hour, now.minute)}"
        ]

        if runways and departing:
            # The form heard on French ATIS; not checked against a DGAC
            # publication.
            parts.append(f"piste en service à l'atterrissage {p.rwy(runways[0])}, "
                         f"au décollage {p.rwy(departing[0])}")
            parts.append(f"approche {self._approach_type(runways)} à prévoir")
        elif runways:
            if len(runways) == 1:
                parts.append(f"piste en service {p.rwy(runways[0])}")
            else:
                spoken = " et ".join(p.rwy(r) for r in runways)
                parts.append(f"pistes en service {spoken}")
            parts.append(f"approche {self._approach_type(runways)} à prévoir")

        parts.append(p.wind(wx))
        parts.append(sfr.visibility(wx.visibility_sm))
        parts.append(self._clouds_french(wx))
        parts.append(
            f"température {sfr.temperature(wx.temperature_c)}, "
            f"point de rosée {sfr.temperature(wx.dewpoint_c)}"
        )
        parts.append(p.pressure(wx.qnh_hpa))
        parts.append(f"accusez réception de l'information {sfr.letters(letter)}")

        return ". ".join(part.strip().rstrip(".") for part in parts if part.strip()) + "."

    def _clouds_french(self, wx: Weather) -> str:
        from . import speech_fr as sfr

        words = {
            "FEW": "quelques nuages à", "SCT": "nuages épars à",
            "BKN": "nuages fragmentés à", "OVC": "ciel couvert à",
            "VV": "plafond indéfini à",
        }
        if not wx.clouds:
            return "nuages non détectés"
        spoken = []
        for cover, height in wx.clouds[:3]:
            spoken.append(f"{words.get(cover, 'nuages à')} {sfr.height(height)}")
        return ", ".join(spoken)

    def _approach_kind(self, runways: list[str]) -> str:
        return self.approach_for(runways[0]) if runways else ""

    def _approach_type(self, runways: list[str] | None = None) -> str:
        """The approach to expect, as the broadcast says it."""
        runways = runways if runways is not None else (
            self.current.landing_runways if self.current else [])
        kind = self._approach_kind(runways) or "ILS"
        return self.phrase.approach_name(kind) if hasattr(
            self.phrase, "approach_name") else kind

    def _clouds(self, wx: Weather) -> str:
        """Cloud layers, or the phrase used when there are none."""
        from ..weather.metar import CLOUD_WORDS

        if not wx.clouds:
            return "sky clear" if self.phrase.dialect == "faa" else "no cloud detected"
        spoken = []
        for cover, height in wx.clouds[:3]:
            words = CLOUD_WORDS.get(cover, "clouds at")
            if cover == "VV":
                spoken.append(f"indefinite ceiling {self.phrase.alt(height)}")
            else:
                spoken.append(f"{words} {self.phrase.alt(height, 99999)}")
        return ", ".join(spoken)

    def _signed(self, celsius: float) -> str:
        value = int(round(celsius))
        if value < 0:
            return f"minus {self.phrase.d(abs(value))}"
        return self.phrase.d(value)


def letter_from_text(text: str) -> str:
    """Recover the information letter a pilot reported having.

    Whisper transcribes "information Bravo" reliably, but pilots also say just
    "with Bravo" or "I have Bravo", so any phonetic word in the utterance is
    treated as a candidate.
    """
    from .speech import _ALPHABET_REVERSE

    words = [w.strip(".,").lower() for w in (text or "").split()]
    for i, word in enumerate(words):
        if word in _ALPHABET_REVERSE:
            letter = _ALPHABET_REVERSE[word]
            if len(letter) == 1 and letter.isalpha():
                return letter
    return ""
