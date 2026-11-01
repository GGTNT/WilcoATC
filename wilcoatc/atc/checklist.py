# -*- coding: utf-8 -*-
"""A checklist run with the pilot, challenge and response.

The first officer used to recite: "taxi checklist complete, flaps set",
whatever the flaps were, and nobody had asked. A real one reads an item,
waits for the answer, and looks at the aeroplane: "landing light... I show
it off". This is that conversation, item by item, for the checklists in
:mod:`wilcoatc.atc.checklist_files`.

What it does with an answer
---------------------------

An answer is a kind ("set", "on", "three green", a figure) from
:mod:`wilcoatc.atc.interphone`. "Check" is accepted for any item; any other
kind the item does not accept ("off" to "landing light") gets the expected
response back as a question, once. Where the item names a variable, the
answer is checked against the simulator: first against the state the
situation loop already has, and only if that disagrees, against the
variable read again on its own -- the slow rotation is three seconds round,
and a switch thrown just before the answer is still off in it. No answer
from the simulator is never an objection. A disagreement is said once; the
pilot may put it right (the first officer sees it and moves on), answer
again, or say "continue", which leaves the item open, and the checklist
ends with how many are.

What it does with silence
-------------------------

The wait for an answer starts when the item has been *heard*, not when it
was queued: the crew's lines wait for the frequency (the cabin waits for
the radio, never the reverse), and a pilot cannot answer what is still in
the queue. No answer in :data:`ANSWER_WAIT_S` gets the item once more, then
"holding the checklist", and "continue" picks it up. An item the radio
talked over is read again once the frequency has been quiet a moment,
which is not the one repeat: it was never heard.

When it offers
--------------

At the phase a checklist belongs to, once a flight, never within
:data:`OFFER_EVERY_S` of another offer, never on short final or below
:data:`OFFER_FLOOR_AGL` in the air, and never over the radio. A checklist
done or declined is not offered again.

Every line, every answer, and every silence is a ``checklist`` row in
``logs crew``.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from ..logs import CREW
from . import interphone as ip
from .checklist_files import STANDARD_CHECKLISTS, Book, Check, Checklist, Item
from .crew_lines import CHECKLIST_DIGITS, CHECKLIST_STATES, CHECKLIST_WORDS
from .session import Phase

log = logging.getLogger(__name__)

# How long the first officer waits for an answer before reading the item
# again, and again before holding the checklist. A pilot answering a
# challenge takes a second or two; one finding the switch first, longer.
# UNVERIFIED: chosen, not measured.
ANSWER_WAIT_S = 8.0
# How long the frequency has to have been quiet before an item it talked
# over is read again. The pilot who has just let go of the button may have
# been answering, and their words are still being recognised.
RESUME_AFTER_S = 1.5
# Offers: at most one in this long, an offer stands this long, and none in
# this long after the pilot last spoke to the first officer.
OFFER_EVERY_S = 120.0
OFFER_ANSWER_S = 30.0
OFFER_AFTER_TALK_S = 30.0
# Never an offer below this in the air, nor on short final.
OFFER_FLOOR_AGL = 500.0
# Nor in the first moments of a flight: the pilot has just sat down, and the
# cabin has its own first things to say.
OFFER_SETTLE_S = 20.0
# How long level in the cruise before the cruise checklist is offered.
LEVEL_FOR_CRUISE_S = 60.0

# Hectopascals in an inch of mercury, for an altimeter set in inches.
HPA_PER_INHG = 33.8639

# Where a run is: its item queued to be said, said and waiting for the
# answer, or held.
QUEUED, WAITING, HELD = "queued", "waiting", "held"


@dataclass
class _Run:
    checklist: Checklist
    items: tuple[Item, ...]
    index: int = 0
    state: str = QUEUED
    since: float = 0.0
    # The one repeat after no answer has been used.
    repeated: bool = False
    # This item has had its one objection; and that objection was the
    # simulator disagreeing, so putting the switch right moves it on.
    objected: bool = False
    disagreed: bool = False
    # The radio was live while the item waited, and since when it is quiet.
    interrupted: bool = False
    quiet_since: float = 0.0
    # The items left open, by their English challenge.
    open: list[str] = field(default_factory=list)

    @property
    def item(self) -> Item:
        return self.items[self.index]


def _hpa(value: float) -> float:
    """An altimeter setting in hectopascals, whichever unit it came in."""
    value = float(value)
    if value < 100.0:
        return value * HPA_PER_INHG
    if 2000.0 < value < 3300.0:
        return value / 100.0 * HPA_PER_INHG
    return value


def _same(var: str, actual, expected, tolerance: float) -> bool:
    if isinstance(expected, (list, tuple, set, frozenset)):
        return any(_same(var, actual, one, tolerance) for one in expected)
    if var == "transponder_code":
        try:
            return int(str(actual)) == int(str(expected))
        except ValueError:
            return str(actual) == str(expected)
    if isinstance(expected, bool) or isinstance(actual, bool):
        return bool(actual) == bool(expected)
    if var == "kohlsman_hpa":
        return abs(_hpa(actual) - _hpa(expected)) <= max(tolerance, 0.5)
    if var.startswith("com"):
        return abs(float(actual) - float(expected)) <= max(tolerance, 0.003)
    try:
        return abs(float(actual) - float(expected)) <= max(tolerance, 1e-6)
    except (TypeError, ValueError):
        return str(actual).strip().lower() == str(expected).strip().lower()


def holds(check: Check, actual, expected) -> bool:
    """Whether the aeroplane agrees with the check. An unknown is agreement:
    it is never a reason to object."""
    if actual is None or expected is None:
        return True
    var, op, tol = check.var, check.op, check.tolerance
    if op == "eq":
        return _same(var, actual, expected, tol)
    if op == "ne":
        return not _same(var, actual, expected, tol)
    if op == "in":
        return _same(var, actual, list(expected), tol)
    if op == "near":
        return _same(var, actual, expected, tol)
    a = _hpa(actual) if var == "kohlsman_hpa" else float(actual)
    e = _hpa(expected) if var == "kohlsman_hpa" else float(expected)
    if op == "min":
        return a >= e - tol
    if op == "max":
        return a <= e + tol
    return True


class ChecklistRunner:
    """The first officer's side of the checklists, for one flight.

    ``say`` queues one line in the first officer's voice; ``quiet`` is
    whether everything said has been heard and the frequency is free;
    ``fresh`` reads one variable now (None for no answer); ``references``
    resolves ``$qnh`` and the rest (:data:`checklist_files.REFERENCES`),
    with the figure the pilot said for ``$said``. :meth:`hear` and
    :meth:`start` may be called from any thread; everything happens on
    :meth:`tick`, on the situation loop, which is also the only thread
    that reads the simulator.
    """

    def __init__(self, say, quiet=None, fresh=None, references=None,
                 clock=time.monotonic, free=None, settle_s: float = 0.0):
        self.say_line = say
        self.quiet = quiet or (lambda: True)
        # Whether an unprompted offer may be made: nobody else in the
        # aeroplane has anything still to say. An offer yields to all of
        # it, where a checklist the pilot asked for only waits its turn.
        self.free = free or self.quiet
        self.settle_s = settle_s
        # The timing, the module's figures until configure() is handed the
        # settings (immersion.first_officer).
        self.answer_wait_s = ANSWER_WAIT_S
        self.resume_after_s = RESUME_AFTER_S
        self.offer_every_s = OFFER_EVERY_S
        self.offer_answer_s = OFFER_ANSWER_S
        self.offer_after_talk_s = OFFER_AFTER_TALK_S
        self.offer_floor_agl = OFFER_FLOOR_AGL
        # Whether offers are made at all: not while the pilot has asked for
        # a quiet cockpit, or callouts only.
        self.offers = True
        # The simulator's title for the aeroplane, for the mismatch rows a
        # study-level add-on's type file is built from.
        self.aircraft_title = ""
        self.fresh = fresh
        self.references = references or (lambda name, said=None: None)
        self.clock = clock
        self._lock = threading.Lock()
        self._inbox: list[tuple[str, object]] = []
        self.begin(None)

    # ------------------------------------------------------------------
    # the flight
    # ------------------------------------------------------------------

    def begin(self, book: Book | None, rules: str = "vfr",
              language: str = "en") -> None:
        """A new flight: nothing done, nothing offered."""
        self.book = book
        self.rules = rules
        self.language = language if language in CHECKLIST_WORDS else "en"
        self.done: set[str] = set()
        self._run: _Run | None = None
        self._state = None
        self._offered: set[str] = set()
        self._offer: tuple[str, float] | None = None
        self._last_offer: float | None = None
        self._talked_at: float | None = None
        self._withheld: set[tuple[str, str]] = set()
        self._level_since = 0.0
        self._paused_at: float | None = None
        self._begun_at = self.clock()
        with self._lock:
            self._inbox.clear()

    def use(self, book: Book | None, rules: str | None = None,
            language: str | None = None) -> None:
        """The same flight with the files read again, a rules change (a VFR
        flight picking up an IFR clearance), or the crew's language."""
        self.book = book
        if rules:
            self.rules = rules
        if language in CHECKLIST_WORDS:
            self.language = language

    def configure(self, settings) -> None:
        """Take the timing from ``immersion.first_officer``."""
        for name, attr in (("answer_wait_s", "answer_wait_s"),
                           ("resume_after_s", "resume_after_s"),
                           ("offer_every_s", "offer_every_s"),
                           ("offer_answer_s", "offer_answer_s"),
                           ("offer_after_talk_s", "offer_after_talk_s"),
                           ("offer_settle_s", "settle_s"),
                           ("offer_floor_agl_ft", "offer_floor_agl")):
            value = getattr(settings, name, None)
            if value is not None:
                setattr(self, attr, float(value))

    @property
    def current_item(self) -> str:
        """The English challenge of the item waiting, or ""."""
        run = self._run
        if run is None or run.index >= len(run.items):
            return ""
        return run.item.challenge_in("en")

    def drop(self) -> None:
        """Stop the checklist in hand without a word: the first officer, or
        the checklists, were switched off."""
        if self._run is not None:
            self._note("dropped", self._run)
        self._run = None
        self._offer = None
        with self._lock:
            self._inbox.clear()

    def pause(self) -> None:
        """The simulator is paused: the wait for an answer stops with it."""
        if self._paused_at is None:
            self._paused_at = self.clock()

    @property
    def running(self) -> str:
        return self._run.checklist.name if self._run else ""

    @property
    def waiting(self) -> bool:
        """Whether the first officer is waiting for the pilot: an item
        read, objected to, or the checklist held."""
        return self._run is not None

    @property
    def offering(self) -> str:
        """The checklist just offered and not yet answered, or ""."""
        if self._offer is None or self._run is not None:
            return ""
        name, at = self._offer
        return name if self.clock() - at < self.offer_answer_s else ""

    def titles(self) -> dict[str, tuple[str, ...]]:
        """Every checklist's titles, for the interphone to match names on."""
        if self.book is None:
            return {}
        return {name: tuple(c.title.values()) for name, c in
                self.book.for_rules(self.rules).items()}

    # ------------------------------------------------------------------
    # what the pilot says
    # ------------------------------------------------------------------

    def spoken_to(self) -> None:
        """The pilot spoke to the first officer about something else: no
        offer on top of the answer."""
        self._talked_at = self.clock()

    def start(self, name: str) -> None:
        with self._lock:
            self._inbox.append(("start", name))

    def hear(self, said) -> bool:
        """Something the pilot said to the first officer. False for an
        address the checklists have nothing to do with, which the crew
        answer on their own ("go ahead")."""
        if said.what == ip.ATTENTION and self._run is None:
            return False
        with self._lock:
            self._inbox.append(("heard", said))
        return True

    # ------------------------------------------------------------------
    # one pass of the situation loop
    # ------------------------------------------------------------------

    def tick(self, state, session=None, phase: Phase | None = None,
             height_ft: float | None = None) -> None:
        now = self.clock()
        self._state = state
        if self._paused_at is not None:
            gap = now - self._paused_at
            self._paused_at = None
            if self._run is not None:
                self._run.since += gap
        with self._lock:
            inbox, self._inbox = self._inbox, []
        for kind, what in inbox:
            self._talked_at = now
            if kind == "start":
                self._start(str(what or ""), now)
            else:
                self._heard(what, now)

        run = self._run
        if run is None:
            phase = phase or getattr(session, "phase", None)
            if phase is not None:
                height = (height_ft if height_ft is not None
                          else getattr(state, "altitude_agl_ft", 0.0))
                self._maybe_offer(state, phase, height or 0.0,
                                  getattr(session, "pattern_leg", ""), now)
            return
        self._step(run, now)
        # What was said on this pass and has already been heard starts its
        # wait now, not on the next pass.
        run = self._run
        if run is not None and run.state == QUEUED and self._is_quiet():
            self._heard_it(run, now)

    @staticmethod
    def _heard_it(run: _Run, now: float) -> None:
        run.state, run.since = WAITING, now
        run.interrupted, run.quiet_since = False, 0.0

    def _step(self, run: _Run, now: float) -> None:
        if run.state == HELD:
            return
        quiet = self._is_quiet()
        if run.state == QUEUED:
            if not quiet:
                return
            self._heard_it(run, now)
        if run.disagreed and self._agrees_now(run.item):
            self._note("put right", run)
            self._advance(run, now)
            return
        if not quiet:
            if not run.interrupted:
                self._note("interrupted", run, why="the frequency or the "
                           "cabin speaker is busy")
            run.interrupted, run.quiet_since = True, 0.0
            return
        if run.interrupted:
            if not run.quiet_since:
                run.quiet_since = now
                return
            if now - run.quiet_since < self.resume_after_s:
                return
            run.interrupted = False
            self._read(run, why="read again after the radio")
            return
        if now - run.since >= self.answer_wait_s:
            if not run.repeated and not run.objected:
                run.repeated = True
                self._read(run, why="no answer")
            else:
                self._hold(run, "no answer")

    # ------------------------------------------------------------------

    def _is_quiet(self) -> bool:
        try:
            return bool(self.quiet())
        except Exception:
            log.debug("could not tell whether the radio is quiet",
                      exc_info=True)
            return True

    def _words(self) -> dict[str, str]:
        return CHECKLIST_WORDS.get(self.language, CHECKLIST_WORDS["en"])

    def _title(self, checklist_or_name) -> tuple[str, str]:
        if isinstance(checklist_or_name, Checklist):
            title = checklist_or_name.title_in(self.language)
        else:
            title = str(checklist_or_name).replace("_", " ")
        return title, title[:1].upper() + title[1:]

    def _say(self, key: str, **values) -> None:
        text = self._words()[key].format(**values)
        try:
            self.say_line(text)
        except Exception:
            log.debug("the first officer could not say %r", text,
                      exc_info=True)

    def _note(self, event: str, run: _Run | None = None, **fields) -> None:
        if run is not None:
            fields.setdefault("checklist", run.checklist.name)
            if run.index < len(run.items):
                fields.setdefault("item", run.item.challenge_in("en"))
        CREW.write("checklist", event=event, **fields)

    # -- starting, reading, moving on ------------------------------------

    def _start(self, name: str, now: float) -> None:
        book = self.book.for_rules(self.rules) if self.book else {}
        if not name:
            self._say("which")
            self._note("unknown", why="no checklist named")
            return
        checklist = book.get(name)
        if checklist is None:
            title, Title = self._title(name)
            self._say("none", title=title, Title=Title)
            self._note("unknown", checklist=name,
                       why=f"this aeroplane's {self.rules.upper()} "
                           "checklists have none by that name")
            return
        if self._run is not None and self._run.checklist.name != name:
            self._note("abandoned", self._run, why=f"{name} was asked for")
        self._offered.add(name)
        self._offer = None
        title, Title = self._title(checklist)
        self._say("start", title=title, Title=Title)
        items = checklist.for_rules(self.rules)
        self._run = _Run(checklist, items)
        self._note("start", self._run, rules=self.rules,
                   items=len(items), origin=checklist.origin)
        if not items:
            self._complete(self._run)
            return
        self._read(self._run, why="the first item")

    def _read(self, run: _Run, why: str) -> None:
        text = run.item.challenge_in(self.language).strip()
        if text[-1:] not in ".?!":
            text += "."
        try:
            self.say_line(text)
        except Exception:
            log.debug("the first officer could not say %r", text,
                      exc_info=True)
        run.state = QUEUED
        self._note("read", run, why=why, index=run.index + 1)

    def _advance(self, run: _Run, now: float) -> None:
        run.index += 1
        run.repeated = run.objected = run.disagreed = False
        run.interrupted = False
        if run.index >= len(run.items):
            self._complete(run)
        else:
            self._read(run, why="the next item")

    def _complete(self, run: _Run) -> None:
        title, Title = self._title(run.checklist)
        n = len(run.open)
        if n:
            words = self._words()
            spoken = (words["open_one"] if n == 1 else words["open_many"]
                      .format(n=self._digits(n) if n > 9 else
                              self._count(n)))
            self._say("complete_open", title=title, Title=Title, open=spoken)
        else:
            self._say("complete", title=title, Title=Title)
        self.done.add(run.checklist.name)
        self._note("complete", run, open=list(run.open))
        self._run = None

    def _hold(self, run: _Run, why: str) -> None:
        self._say("holding")
        run.state = HELD
        self._note("held", run, why=why)

    def _open(self, run: _Run, why: str) -> None:
        run.open.append(run.item.challenge_in("en"))
        self._note("open", run, why=why)

    # -- what was heard ----------------------------------------------------

    def _heard(self, said, now: float) -> None:
        what = said.what
        self._note("heard", self._run, what=what,
                   answers=list(getattr(said, "answers", ()) or ()),
                   value=getattr(said, "value", None),
                   text=getattr(said, "text", ""))
        if what == ip.ACCEPT:
            if self.offering:
                self._start(self.offering, now)
            return
        if what == ip.DECLINE:
            if self._offer is not None:
                self._note("declined", checklist=self._offer[0])
            self._offer = None
            return
        if what == ip.CHECKLIST:
            self._start(getattr(said, "checklist", ""), now)
            return
        run = self._run
        if run is None:
            if what != ip.ATTENTION:
                self._say("not_running")
            return
        if what == ip.HOLD:
            self._hold(run, "the pilot asked")
        elif what == ip.CONTINUE:
            if run.state == HELD:
                run.repeated = False
                self._note("resumed", run)
                self._read(run, why="continued")
            else:
                self._open(run, "the pilot carried on")
                self._advance(run, now)
        elif what == ip.SKIP:
            self._open(run, "skipped")
            self._advance(run, now)
        elif what in (ip.SAY_AGAIN, ip.ATTENTION):
            self._read(run, why="the pilot asked again")
        elif what == ip.FROM_THE_TOP:
            run.index, run.open = 0, []
            run.repeated = run.objected = run.disagreed = False
            self._read(run, why="from the top")
        elif what == ip.ANSWER:
            self._answer(run, said, now)

    def _answer(self, run: _Run, said, now: float) -> None:
        item = run.item
        kinds = tuple(getattr(said, "answers", ()) or ())
        if run.state == HELD:
            run.state = WAITING
        accepted = "check" in kinds or any(k in item.accept for k in kinds)
        if not accepted:
            if run.objected:
                self._open(run, f"answered {', '.join(kinds)}")
                self._advance(run, now)
                return
            run.objected = True
            challenge = item.challenge_in(self.language).rstrip(".!? ")
            self._say("expected", challenge=challenge,
                      response=item.response_in(self.language))
            run.state = QUEUED
            self._note("objected", run, why="not the response",
                       answers=list(kinds),
                       expected=item.response_in("en"))
            return
        verdict, actual, expected, fresh = self._verify(item, said)
        if verdict is False:
            # Every one, objected to or not: the list of what a study-level
            # add-on does not publish grows from these
            # (wilcoatc/atc/checklists/types/).
            self._note("mismatch", run, var=item.check.var, read=actual,
                       expected=expected, answer=list(kinds),
                       value=getattr(said, "value", None),
                       title=self.aircraft_title)
            if run.objected:
                self._open(run, "the aeroplane still disagrees")
                self._advance(run, now)
                return
            run.objected = run.disagreed = True
            self._say_disagreement(item, actual, expected)
            run.state = QUEUED
            self._note("objected", run, var=item.check.var, actual=actual,
                       expected=expected, fresh=fresh)
            return
        self._note("accepted", run, verified=verdict,
                   actual=actual if verdict is not None else None)
        self._advance(run, now)

    # -- the aeroplane -------------------------------------------------

    def _expected(self, check: Check, said_value=None):
        value = check.value
        if isinstance(value, str) and value.startswith("$"):
            try:
                return self.references(value, said_value)
            except Exception:
                log.debug("could not resolve %s", value, exc_info=True)
                return None
        return value

    def _verify(self, item: Item, said):
        """(agrees, actual, expected, read again): agrees is None when
        nothing could be checked, which is not an objection."""
        check = item.check
        if check is None:
            return None, None, None, False
        expected = self._expected(check, getattr(said, "value", None))
        if expected is None:
            self._note("unchecked", self._run, var=check.var,
                       why=f"nothing to check {check.value} against")
            return None, None, None, False
        actual = getattr(self._state, check.var, None)
        if holds(check, actual, expected):
            return True, actual, expected, False
        # The state disagrees. Before saying so, the variable itself, now.
        fresh = None
        if self.fresh is not None:
            try:
                fresh = self.fresh(check.var)
            except Exception:
                log.debug("could not read %s again", check.var, exc_info=True)
        if fresh is None:
            self._note("unchecked", self._run, var=check.var, stale=actual,
                       why="the simulator gave no answer when read again")
            return None, None, expected, True
        return holds(check, fresh, expected), fresh, expected, True

    def _agrees_now(self, item: Item) -> bool:
        check = item.check
        if check is None:
            return False
        expected = self._expected(check)
        if expected is None:
            return False
        actual = getattr(self._state, check.var, None)
        return actual is not None and holds(check, actual, expected)

    def _digits(self, value) -> str:
        table = CHECKLIST_DIGITS.get(self.language, CHECKLIST_DIGITS["en"])
        out = []
        for ch in str(value):
            if ch.isdigit():
                out.append(table[int(ch)])
            elif ch == ".":
                out.append(self._words()["decimal"])
        return " ".join(out)

    def _count(self, n: int) -> str:
        return CHECKLIST_DIGITS.get(self.language, CHECKLIST_DIGITS["en"])[n]

    def _say_disagreement(self, item: Item, actual, expected) -> None:
        var = item.check.var
        challenge = item.challenge_in(self.language).rstrip(".!? ")
        states = CHECKLIST_STATES.get(self.language, CHECKLIST_STATES["en"])
        if var == "flaps_index":
            if int(actual) == 0:
                self._say("flaps_up", challenge=challenge)
            else:
                self._say("flaps_at", challenge=challenge,
                          value=self._digits(int(actual)))
            return
        if var == "transponder_state":
            names = states.get("transponder_state",
                               CHECKLIST_STATES["en"]["transponder_state"])
            index = int(actual)
            spoken = names[index] if 0 <= index < len(names) else str(index)
            self._say("show_value", challenge=challenge, value=spoken)
            return
        if isinstance(actual, bool) or var == "fuel_selector":
            truth = bool(actual) if var != "fuel_selector" else actual != 0
            pair = states.get(var) or states["switch"]
            self._say("show_state", challenge=challenge,
                      state=pair[0 if truth else 1])
            return
        if var == "kohlsman_hpa":
            inches = isinstance(expected, (int, float)) and (
                float(expected) < 100.0 or float(expected) > 2000.0)
            figure = (round(float(actual) / HPA_PER_INHG * 100) if inches
                      else round(float(actual)))
            self._say("show_value", challenge=challenge,
                      value=self._digits(figure))
            return
        if var == "mixture_pct":
            spoken = self._words()["percent"].format(
                value=self._digits(int(round(float(actual)))))
            self._say("show_value", challenge=challenge, value=spoken)
            return
        if var.startswith("com"):
            figure = f"{float(actual):.3f}".rstrip("0").rstrip(".")
            self._say("show_value", challenge=challenge,
                      value=self._digits(figure))
            return
        if var == "transponder_code":
            self._say("show_value", challenge=challenge,
                      value=self._digits(str(actual).zfill(4)))
            return
        try:
            figure = int(round(float(actual)))
            spoken = self._digits(figure)
        except (TypeError, ValueError):
            spoken = str(actual)
        self._say("show_value", challenge=challenge, value=spoken)

    # -- offers ----------------------------------------------------------

    def _fits(self, state, phase: Phase, height: float, leg: str) -> str:
        """The checklist this moment belongs to, if one does."""
        speed = getattr(state, "ground_speed_kt", 0.0) or 0.0
        engines = bool(getattr(state, "engines_running", False))
        level = abs(getattr(state, "vertical_speed_fpm", 0.0) or 0.0) < 300
        now = self.clock()
        if phase is Phase.ENROUTE and level:
            self._level_since = self._level_since or now
        else:
            self._level_since = 0.0
        fits = {
            "before_start": phase in (Phase.PREFLIGHT, Phase.CLEARED)
            and not engines,
            "after_start": phase in (Phase.PREFLIGHT, Phase.CLEARED,
                                     Phase.PUSHBACK)
            and engines and speed < 1.0,
            "taxi": phase is Phase.TAXI_OUT and speed > 3.0,
            "before_takeoff": phase is Phase.HOLDING_SHORT,
            "after_takeoff": phase in (Phase.TAKEOFF, Phase.DEPARTURE)
            and height >= 1000.0,
            "cruise": bool(self._level_since)
            and now - self._level_since >= LEVEL_FOR_CRUISE_S,
            "descent": phase is Phase.DESCENT,
            "approach": phase is Phase.APPROACH and height >= 2000.0,
            "before_landing": (phase is Phase.APPROACH and height < 2000.0)
            or (leg or "").lower() == "downwind",
            "after_landing": phase is Phase.TAXI_IN,
            "shutdown": phase is Phase.PARKED,
        }
        have = self.book.for_rules(self.rules) if self.book else {}
        for name in STANDARD_CHECKLISTS:
            if (fits.get(name) and name in have and name not in self.done
                    and name not in self._offered):
                return name
        return ""

    def _withhold(self, name: str, why: str) -> None:
        if (name, why) in self._withheld:
            return
        self._withheld.add((name, why))
        self._note("offer_withheld", checklist=name, why=why)

    def _maybe_offer(self, state, phase: Phase, height: float, leg: str,
                     now: float) -> None:
        if (self.book is None or not self.offers
                or now - self._begun_at < self.settle_s):
            return
        if (self._last_offer is not None
                and now - self._last_offer < self.offer_every_s):
            return
        if (self._talked_at is not None
                and now - self._talked_at < self.offer_after_talk_s):
            return
        name = self._fits(state, phase, height, leg)
        if not name:
            return
        if phase is Phase.LANDING or (not phase.on_ground
                                      and height < self.offer_floor_agl):
            self._withhold(name, "short final, or below "
                                 f"{self.offer_floor_agl:.0f} ft")
            return
        try:
            free = bool(self.free())
        except Exception:
            free = False
        if not free:
            self._withhold(name, "the frequency is busy, or somebody else "
                                 "in the aeroplane has something to say")
            return
        title, Title = self._title(self.book.checklists[name])
        self._say("offer", title=title, Title=Title)
        self._offer = (name, now)
        self._offered.add(name)
        self._last_offer = now
        self._note("offered", checklist=name, phase=phase.value,
                   height_ft=round(height))


__all__ = ["ANSWER_WAIT_S", "ChecklistRunner", "OFFER_EVERY_S",
           "OFFER_FLOOR_AGL", "OFFER_SETTLE_S", "RESUME_AFTER_S", "holds"]
