# -*- coding: utf-8 -*-
"""What the pilot says to the people who are not on the radio.

There is one microphone. Everything a pilot says arrives here first, and
nearly all of it is for the controller -- so the question this module answers
is the narrow one: *was that transmission addressed to somebody in the
aeroplane instead?*

Two people can be.

*The cabin.* "Cabin crew, could I get a coffee." The purser answers on the
interphone, nothing goes out over the air, and the controller never hears it.

*The ground crew.* "Ground crew, request pushback." That reaches GSX, which
is a menu rather than a person, and the reply is the tug actually arriving.

*The first officer.* "First officer, before takeoff checklist", or their
name. They are the one person who is also reached without an address, in
two ways that cannot cost a transmission. The name of a checklist on its
own ("before takeoff checklist") is a thing nobody says to a controller.
And a bare checklist answer -- "set", "three green", "one zero one three"
-- goes to the first officer only while they are waiting for one, and the
caller decides that: an item read, no readback owed to the controller, the
first officer the last to speak. A radio call is never short enough and
never made of nothing but answer words, so it still goes to the frequency.

Why an address is required
--------------------------

Because the cost of a false positive is a transmission the controller never
answered. "Ready for pushback" is a thing a pilot says to a controller on
every flight, and a module that took it as a request to the ramp would
silently remove one of the most ordinary calls on the radio. So nothing is
routed off the frequency unless it was *addressed* to someone: "cabin crew",
"purser", the purser's own name, "ground crew", "ground handling". An address
with nothing recognisable after it is still an address -- the crew answer
"go ahead" -- because the alternative is asking the controller to make sense
of a pilot calling for their cabin manager.

The address has to be at the front, within the first few words, for the same
reason a callsign is: that is where you put the name of the person you are
talking to. "Taxi to the ramp" is not a call to the ramp.

Why it is not in :mod:`wilcoatc.atc.intents`
--------------------------------------------

That module parses phraseology, and every one of its intents is something a
controller can act on. None of this is: there is no correct way to ask for a
coffee, there is no readback, and the ground crew are a menu. Keeping the two
apart is what stops the controller growing a case for a drinks order.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace

# Who was being spoken to.
CABIN = "cabin"
GROUND = "ground"
FIRST_OFFICER = "first_officer"

# What the first officer was told. ``ATTENTION`` below is shared: addressed,
# and nothing recognisable after it.
CHECKLIST = "checklist"      # start one, by name (``Said.checklist``)
ANSWER = "answer"            # a response to the item read (``Said.answers``)
SKIP = "skip"
HOLD = "hold"
CONTINUE = "continue"
SAY_AGAIN = "say_again"
FROM_THE_TOP = "from_the_top"
ACCEPT = "accept"            # yes to "before takeoff checklist when ready?"
DECLINE = "decline"
SPEEDS = "speeds"            # "speeds one four five, one four seven, ..."
MINIMUMS = "minimums"        # "minimums two five zero" (``Said.value``)
QUIET = "quiet"              # "quiet cockpit": the first officer stops talking
CALLOUTS_ONLY = "callouts_only"
NORMAL = "normal"            # back to everything
GO_AROUND = "go_around"      # the pilot flying calls it, on the flight deck
RESERVE = "reserve"          # "reserve ten gallons" (value, ``Said.unit``)
QUESTION = "question"        # "what's our squawk" (``Said.asked``, ``.name``)
HANDOVER = "handover"        # "you make the call" (``Said.call``)

# What the cabin was asked for.
DRINK = "drink"
MEAL = "meal"
STATUS = "status"            # "how are we doing back there"
SECURE = "secure"            # prepare the cabin, for departure or for landing
TURBULENCE = "turbulence"    # signs on, everybody sit down
THANKS = "thanks"
ATTENTION = "attention"      # addressed, and nothing recognisable after it

# What the ground crew were asked for. These are the names
# :mod:`wilcoatc.integrations.gsx` knows its menu entries by, so that a word
# heard here is a menu entry there and nothing in between has to translate.
PUSHBACK = "pushback"
STOP_PUSHBACK = "stop_pushback"
CONFIRM = "confirm"
BOARDING = "boarding"
DEBOARDING = "deboarding"
REFUEL = "refuel"
CATERING = "catering"
JETWAY = "jetway"
STAIRS = "stairs"
DEICE = "deice"
GPU = "gpu"

#: Everything the ground crew can be asked for, so a caller can check a name
#: rather than a list of imports.
GROUND_SERVICES: frozenset[str] = frozenset({
    PUSHBACK, STOP_PUSHBACK, CONFIRM, BOARDING, DEBOARDING, REFUEL,
    CATERING, JETWAY, STAIRS, DEICE, GPU, STATUS, ATTENTION,
})


@dataclass(frozen=True)
class Said:
    """One transmission that was not for the controller."""

    who: str                 # CABIN or GROUND
    what: str                # one of the request names above
    text: str = ""           # what was actually said, unchanged
    language: str = "en"
    #: Which words made it an address. Written to the log, because "why did
    #: the controller not answer me" has exactly one useful answer and this
    #: is it.
    addressed: str = ""
    #: For the first officer: the checklist named ("" for one nobody has),
    #: the kinds of answer heard in the order said
    #: (:data:`wilcoatc.atc.checklist_files.ANSWERS`), and the figure in it.
    checklist: str = ""
    answers: tuple[str, ...] = ()
    value: float | None = None
    #: For SPEEDS: which speeds were said, (name, knots), name one of v1,
    #: vr, v2, vapp.
    speeds: tuple[tuple[str, float], ...] = ()
    #: For RESERVE: "gal", "l", "kg" or "lb".
    unit: str = ""
    #: For QUESTION: what was asked (qnh, said, runway, squawk, clearance,
    #: circuit, frequency, distance) and the place named, folded.
    asked: str = ""
    name: str = ""
    #: For HANDOVER: which call (any, ready, taxi, clearance, readback).
    call: str = ""


# --------------------------------------------------------------------------
# who is being spoken to
# --------------------------------------------------------------------------
#
# Every one of these is a phrase nobody says to a controller. That is the
# whole selection rule, and it is why "ground" is not in the ground list and
# "ramp" is not in it on its own: both are ordinary words on the frequency,
# and taking either would cost a real transmission.

_ADDRESS: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        CABIN: (
            "cabin crew", "cabin manager", "flight attendant",
            "flight attendants", "cabin attendant", "purser", "chief purser",
            "senior cabin crew", "galley", "the galley", "number one",
            "steward", "stewardess", "cabin service",
        ),
        GROUND: (
            "ground crew", "ground handling", "ground handler",
            "ground staff", "ground services", "ground ops", "ground agent",
            "ramp crew", "ramp agent", "handling agent", "gsx", "tug",
            "pushback crew", "marshaller", "headset",
        ),
        FIRST_OFFICER: (
            "first officer", "copilot", "co pilot", "pilot monitoring",
        ),
    },
    "fr": {
        CABIN: (
            "personnel de cabine", "chef de cabine", "equipage de cabine",
            "hotesse", "steward", "cabine", "office",
        ),
        GROUND: (
            "personnel au sol", "equipe au sol", "agent de piste",
            "assistance en escale", "placeur", "tracteur", "gsx",
        ),
        FIRST_OFFICER: ("copilote", "co pilote", "officier pilote"),
    },
    "de": {
        CABIN: (
            "kabinenpersonal", "purser", "kabinenchef", "flugbegleiter",
            "flugbegleiterin", "kabine", "bordkuche",
        ),
        GROUND: (
            "bodenpersonal", "bodencrew", "vorfeld", "abfertigung",
            "pushback fahrer", "schlepper", "gsx",
        ),
        FIRST_OFFICER: ("erster offizier", "kopilot", "copilot"),
    },
    "nl": {
        FIRST_OFFICER: ("eerste officier", "copiloot", "co piloot"),
    },
}

# How far into a transmission an address may be and still be an address. A
# callsign in front of it is normal -- "Speedbird one two three, cabin crew"
# is nobody's idea of a sentence, but "cabin crew, this is the flight deck"
# is, and so is a pilot who says their own callsign out of habit first.
ADDRESS_WITHIN_WORDS = 6


# --------------------------------------------------------------------------
# what they were asked for
# --------------------------------------------------------------------------
#
# Ordered: the first pattern that matches wins, so the specific ones come
# first. "Stop the pushback" has to be read before "pushback", and "no more
# drinks" before "drink".

_CABIN_ASKS: dict[str, tuple[tuple[str, str], ...]] = {
    "en": (
        (SECURE, r"\b(secure|prepare|ready)\b.*\b(cabin|landing|departure|"
                 r"takeoff|take off|arrival)\b"),
        (SECURE, r"\bcabin\b.*\b(secure|ready)\b"),
        (TURBULENCE, r"\b(turbulen\w*|rough|bumpy|seat ?belt signs?|"
                     r"signs? (are )?(on|coming on)|strap in|take your seats?)\b"),
        (MEAL, r"\b(meals?|food|something to eat|dinner|lunch|breakfast|"
               r"crew meals?|trays?|snacks?)\b"),
        (DRINK, r"\b(coffees?|teas?|waters?|juices?|coke|soda|drinks?|"
                r"something to drink|refill|cups?|espresso)\b"),
        (STATUS, r"\b(how|what)('s| is| are)?\b.*\b(cabin|back there|"
                 r"boarding|passengers|pax)\b"),
        (STATUS, r"\b(cabin )?(status|report|everything (ok|okay|all right))\b"),
        (THANKS, r"\b(thank you|thanks|cheers|appreciate it|nothing (else|"
                 r"more)|that('s| is) all)\b"),
    ),
    "fr": (
        (SECURE, r"\b(prepar\w+|securis\w+|pret\w*)\b.*\b(cabine|atterrissage|"
                 r"decollage|arrivee)\b"),
        (TURBULENCE, r"\b(turbulence\w*|ceintures?|asseoir|secousses)\b"),
        (MEAL, r"\b(repas|plateau|manger|diner|dejeuner|collation)\b"),
        (DRINK, r"\b(cafe|the|eau|jus|boisson|boire|verre)\b"),
        (STATUS, r"\b(ou en|comment|situation|etat)\b.*\b(cabine|embarquement|"
                 r"passagers)\b"),
        (THANKS, r"\b(merci|c'est tout|rien d'autre)\b"),
    ),
    "de": (
        (SECURE, r"\b(kabine|landung|start|ankunft)\b.*\b(vorbereiten|"
                 r"sichern|fertig)\b"),
        (TURBULENCE, r"\b(turbulenz\w*|anschnall\w*|hinsetzen|unruhig)\b"),
        (MEAL, r"\b(essen|mahlzeit|tablett|imbiss|abendessen)\b"),
        (DRINK, r"\b(kaffee|tee|wasser|saft|getrank\w*|trinken)\b"),
        (STATUS, r"\b(wie|was)\b.*\b(kabine|boarding|passagiere)\b"),
        (THANKS, r"\b(danke|das (war|ist) alles|sonst nichts)\b"),
    ),
}

_GROUND_ASKS: dict[str, tuple[tuple[str, str], ...]] = {
    "en": (
        (STOP_PUSHBACK, r"\b(stop|cancel|abort|hold)\b.*\b(push|pushback|"
                        r"push back|tug)\b"),
        (CONFIRM, r"\b(confirm|confirmed|ready|standing by|go ahead|"
                  r"set (the )?(brakes|parking brake) (off|released)|"
                  r"brakes (are )?(off|released)|cleared to push|"
                  r"you('re| are) cleared)\b"),
        (PUSHBACK, r"\b(push ?back|push|tug|tow)\b"),
        (BOARDING, r"\b(board\w*|passengers|pax|bus)\b"),
        (DEBOARDING, r"\b(deboard\w*|disembark\w*|offload|"
                     r"(get|let) (them|the passengers) off)\b"),
        (REFUEL, r"\b(refuel\w*|fuel|bowser|fuel truck)\b"),
        (CATERING, r"\b(cater\w*|galley (truck|service)|trolleys)\b"),
        (JETWAY, r"\b(jet ?way|jet ?bridge|air ?bridge|bridge)\b"),
        (STAIRS, r"\b(stairs|steps|air ?stairs)\b"),
        (DEICE, r"\b(de ?ic\w+|anti ?ic\w+)\b"),
        (GPU, r"\b(gpu|ground power|external power)\b"),
        (STATUS, r"\b(status|how long|what('s| is) (happening|going on)|"
                 r"report)\b"),
    ),
    "fr": (
        (STOP_PUSHBACK, r"\b(arret\w*|annul\w*|stop)\b.*\b(repouss\w*|push)\b"),
        (CONFIRM, r"\b(confirm\w*|pret|freins? (desserr\w*|relach\w*))\b"),
        (PUSHBACK, r"\b(repouss\w*|push|tracteur|remorqu\w*)\b"),
        (BOARDING, r"\b(embarqu\w*|passagers)\b"),
        (DEBOARDING, r"\b(debarqu\w*|descendre)\b"),
        (REFUEL, r"\b(carburant|avitaill\w*|plein|kerosene)\b"),
        (CATERING, r"\b(commissariat|catering|plateaux)\b"),
        (JETWAY, r"\b(passerelle)\b"),
        (STAIRS, r"\b(escalier\w*|escabeau)\b"),
        (DEICE, r"\b(degivr\w*)\b"),
        (GPU, r"\b(groupe de parc|alimentation|gpu)\b"),
        (STATUS, r"\b(situation|combien de temps|ou en)\b"),
    ),
    "de": (
        (STOP_PUSHBACK, r"\b(stopp?|abbrech\w*|anhalten)\b.*\b(push\w*|"
                        r"schlepp\w*)\b"),
        (CONFIRM, r"\b(bestatig\w*|bereit|fertig|bremsen (gelost|offen))\b"),
        (PUSHBACK, r"\b(push\w*|zuruckstossen|schlepp\w*)\b"),
        (BOARDING, r"\b(boarding|einsteig\w*|passagiere)\b"),
        (DEBOARDING, r"\b(aussteig\w*|deboarding)\b"),
        (REFUEL, r"\b(betank\w*|kerosin|sprit|treibstoff)\b"),
        (CATERING, r"\b(catering|verpflegung)\b"),
        (JETWAY, r"\b(fluggastbrucke|brucke|jetway)\b"),
        (STAIRS, r"\b(treppe|fluggasttreppe)\b"),
        (DEICE, r"\b(enteis\w*)\b"),
        (GPU, r"\b(bodenstrom|gpu|stromversorgung)\b"),
        (STATUS, r"\b(status|wie lange|was ist los)\b"),
    ),
}


# --------------------------------------------------------------------------
# what the first officer can be told
# --------------------------------------------------------------------------
#
# Folded words (no accents, "-" read as a space). Each table is matched
# longest phrase first and a phrase's words are taken out once matched, so
# "three green" is a green and not the figure three, and "on reprend du
# debut" is from the top rather than a "reprends".

# The word that says a checklist is meant. Never said to a controller.
_CHECKLIST_WORD = re.compile(
    r"\b(check ?lists?|checkliste|checklijst|liste de verification)\b")

# A checklist by name with no address is the first officer's only when the
# transmission is about that long: a radio call that mentions a checklist
# ("... before takeoff checklist complete, ready for departure") is longer.
CHECKLIST_WITHIN_WORDS = 8

# A bare answer, with no address, is never longer than this.
BARE_WITHIN_WORDS = 6

_ANSWER_WORDS: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "green": ("three green", "three greens", "green", "greens"),
        "check": ("checked", "check", "checks", "cross checked", "okay", "ok",
                  "confirmed", "verified", "clear", "in", "removed"),
        "set": ("set", "rich", "set and checked"),
        "on": ("on", "armed", "engaged"),
        "off": ("off", "disarmed", "disengaged", "cut off", "cutoff",
                "idle cut off"),
        "up": ("up", "retracted", "stowed"),
        "down": ("down", "extended"),
        "complete": ("complete", "completed", "done", "briefed"),
    },
    "fr": {
        "green": ("trois vertes", "trois verts", "vertes", "verts", "vert",
                  "verte"),
        "check": ("verifie", "verifiee", "verifies", "verifiees", "controle",
                  "ok", "check", "vu"),
        "set": ("regle", "reglee", "regles", "reglees", "affiche", "affichee",
                "affiches", "cale", "calee", "positionne", "set",
                "plein riche"),
        "on": ("allume", "allumee", "allumes", "allumees", "en marche",
               "marche", "en service", "on", "arme", "engage"),
        "off": ("eteint", "eteinte", "eteints", "eteintes", "arret", "coupe",
                "coupee", "coupes", "off", "hors service", "etouffoir",
                "desarme"),
        "up": ("rentre", "rentres", "rentree", "rentrees", "haut"),
        "down": ("sorti", "sortis", "sortie", "sorties", "bas"),
        "complete": ("termine", "terminee", "fait", "effectue", "effectuee",
                     "complet", "briefe"),
    },
    "de": {
        "green": ("drei grun", "grun"),
        "check": ("gepruft", "geprueft", "kontrolliert", "check", "ok"),
        "set": ("gesetzt", "eingestellt"),
        "on": ("eingeschaltet", "an", "ein"),
        "off": ("ausgeschaltet", "aus"),
        "up": ("eingefahren", "oben"),
        "down": ("ausgefahren", "unten"),
        "complete": ("erledigt", "fertig", "abgeschlossen"),
    },
    "nl": {
        "green": ("drie groen", "groen"),
        "check": ("gecontroleerd", "check", "ok"),
        "set": ("ingesteld", "gezet"),
        "on": ("aan",),
        "off": ("uit",),
        "up": ("ingetrokken", "op"),
        "down": ("uitgeklapt", "neer"),
        "complete": ("klaar", "voltooid", "gedaan"),
    },
}

# The commands, which the first officer acts on whichever item is waiting.
_COMMAND_WORDS: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        FROM_THE_TOP: ("from the top", "start again", "start over", "restart"),
        SAY_AGAIN: ("say again", "repeat", "pardon", "come again", "sorry"),
        SKIP: ("skip it", "skip", "next item", "defer"),
        HOLD: ("hold the checklist", "hold it", "hold", "stop", "wait",
               "standby", "stand by"),
        CONTINUE: ("continue the checklist", "continue", "resume",
                   "carry on", "go on"),
    },
    "fr": {
        FROM_THE_TOP: ("on reprend du debut", "reprends du debut",
                       "depuis le debut", "du debut"),
        SAY_AGAIN: ("repete", "repetez", "pardon", "encore une fois"),
        SKIP: ("on passe", "passe", "suivant", "saute"),
        HOLD: ("suspends la check list", "suspends", "attends", "attend",
               "stop", "pause"),
        CONTINUE: ("on continue", "continue", "continuez", "reprends",
                   "la suite"),
    },
    "de": {
        FROM_THE_TOP: ("von vorne", "noch einmal von vorne"),
        SAY_AGAIN: ("wiederholen", "nochmal", "bitte nochmal"),
        SKIP: ("uberspringen", "nachster punkt"),
        HOLD: ("anhalten", "warte", "halt", "stopp"),
        CONTINUE: ("weiter", "fortfahren"),
    },
    "nl": {
        FROM_THE_TOP: ("van voren af aan", "van het begin", "opnieuw"),
        SAY_AGAIN: ("herhaal", "nog eens"),
        SKIP: ("overslaan", "volgende"),
        HOLD: ("wacht", "stop", "pauze"),
        CONTINUE: ("ga door", "doorgaan", "verder"),
    },
}

# The answer to "before takeoff checklist when you're ready?".
_OFFER_WORDS: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        ACCEPT: ("go ahead", "yes please", "yes", "yeah", "ready", "sure",
                 "okay", "ok", "affirm", "affirmative", "let's do it",
                 "lets do it", "go"),
        DECLINE: ("not now", "not yet", "no thanks", "no", "negative",
                  "later"),
    },
    "fr": {
        ACCEPT: ("vas y", "allez", "oui", "d'accord", "ok", "pret",
                 "on y va"),
        DECLINE: ("pas maintenant", "pas encore", "plus tard", "non"),
    },
    "de": {
        ACCEPT: ("ja", "los", "bereit", "okay", "gerne"),
        DECLINE: ("nicht jetzt", "spater", "nein"),
    },
    "nl": {
        ACCEPT: ("ga je gang", "ja", "ok", "prima", "klaar"),
        DECLINE: ("niet nu", "later", "nee"),
    },
}

# Words a bare answer may carry and still be one: "it's on", "set and
# checked", "all three green".
_FILLERS = frozenset({
    "and", "is", "it's", "its", "the", "all", "please", "thanks", "sir",
    "et", "c'est", "est", "le", "la", "les", "tout", "und", "ist", "alles",
    "en", "het", "alle",
})

# The figures, said a digit at a time or as a number.
_DIGITS: dict[str, dict[str, int]] = {
    "en": {"zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "tree": 3,
           "four": 4, "fower": 4, "five": 5, "fife": 5, "six": 6,
           "seven": 7, "eight": 8, "nine": 9, "niner": 9},
    "fr": {"zero": 0, "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4,
           "cinq": 5, "six": 6, "sept": 7, "huit": 8, "neuf": 9},
    "de": {"null": 0, "eins": 1, "zwei": 2, "zwo": 2, "drei": 3, "vier": 4,
           "funf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9},
    "nl": {"nul": 0, "een": 1, "twee": 2, "drie": 3, "vier": 4, "vijf": 5,
           "zes": 6, "zeven": 7, "acht": 8, "negen": 9},
}
# The tens and teens, for a figure said as a number ("sixty five",
# "soixante-cinq", "quatre-vingt-dix"); German and Dutch write those as one
# word, which wilcoatc.atc.numbers reads.
_TENS: dict[str, dict[str, int]] = {
    "en": {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
           "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
           "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
           "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
           "eighty": 80, "ninety": 90},
    "fr": {"dix": 10, "onze": 11, "douze": 12, "treize": 13, "quatorze": 14,
           "quinze": 15, "seize": 16, "vingt": 20, "vingts": 20,
           "trente": 30, "quarante": 40, "cinquante": 50, "soixante": 60},
}
_THOUSAND = frozenset({"thousand", "tousand", "mille", "tausend", "duizend"})
_HUNDRED = frozenset({"hundred", "cent", "cents", "hundert", "honderd"})


def _languages(language: str) -> list[str]:
    """The crew's language first, then English, then the rest."""
    first = (language or "en")[:2]
    return list(dict.fromkeys([first, "en", *_ANSWER_WORDS]))


def _take(words: list[str], table: dict[str, tuple[str, ...]],
          languages=None) -> list[tuple[int, str]]:
    """Every phrase of ``table`` found in ``words``, longest first.

    Returns (position, name) in the order said, and blanks the words it
    took so that a shorter phrase cannot take them again.
    """
    phrases = sorted(((tuple(p.split()), name) for name, ps in table.items()
                      for p in ps), key=lambda pn: -len(pn[0]))
    found = []
    for phrase, name in phrases:
        n = len(phrase)
        for at in range(len(words) - n + 1):
            if tuple(words[at:at + n]) == phrase:
                found.append((at, name))
                words[at:at + n] = [""] * n
    return sorted(found)


def _figure(words: list[str], language: str) -> tuple[float | None, list[str]]:
    """The figure in what is left, and the words that were not part of it.

    "One zero one three" is a run of digits and reads as 1013; "seven
    thousand" and "one two thousand" as 7000 and 12000; a German or Dutch
    compound ("siebentausend") from :mod:`wilcoatc.atc.numbers`.
    """
    from .numbers import word_values

    digits = {}
    for code in _languages(language):
        for word, value in _DIGITS.get(code, {}).items():
            digits.setdefault(word, value)
    compounds = word_values(language) if language in ("de", "nl") else {}
    tens = {}
    for code in _languages(language):
        for word, value in _TENS.get(code, {}).items():
            tens.setdefault(word, value)
    words = [w for w in words if w]
    total, run, seen, rest = 0, "", False, []
    at = 0
    while at < len(words):
        word = words[at]
        at += 1
        if word in tens:
            number = tens[word]
            # "quatre vingt": the four before it multiplies, it is not a digit.
            if word.startswith("vingt") and run.endswith("4") and \
                    at >= 2 and words[at - 2] == "quatre":
                run, number = run[:-1], 80
            while at < len(words) and words[at] == "et":
                at += 1
            if at < len(words) and number in (60, 80) and words[at] in tens \
                    and tens[words[at]] < 20:
                number += tens[words[at]]
                at += 1
            elif at < len(words) and words[at] in digits \
                    and 0 < digits[words[at]] and number % 10 == 0 \
                    and number >= 20:
                number += digits[words[at]]
                at += 1
            run += str(number)
            seen = True
            continue
        if word.isdigit():
            run += word
        elif word in digits:
            run += str(digits[word])
        elif word in _THOUSAND and run:
            total += int(run) * 1000
            run = ""
        elif word in _HUNDRED and run:
            total += int(run) * 100
            run = ""
        elif word in compounds:
            total += compounds[word]
        else:
            rest.append(word)
            continue
        seen = True
    if not seen:
        return None, rest
    return float(total + (int(run) if run else 0)), rest


def _checklist_named(folded: str, checklists) -> str | None:
    """Which checklist the words name: its key, "" for none known, or None
    when no checklist word is there at all."""
    if not _CHECKLIST_WORD.search(folded):
        return None
    compact = folded.replace(" ", "")
    best, length = "", 0
    for name, titles in (checklists or {}).items():
        for title in (name.replace("_", " "), *titles):
            key = fold(title).replace(" ", "")
            if key and key in compact and len(key) > length:
                best, length = name, len(key)
    return best


def _for_the_first_officer(folded: str, language: str, checklists,
                           addressed: str = "", bare: bool = False,
                           offered: bool = False) -> Said | None:
    """What the first officer was told, or None if it was not for them.

    ``bare`` is the strict reading for a transmission with no address: it
    has to be short and made of nothing but checklist words, a figure and a
    filler or two, or it was for the controller.
    """
    def said(what, **extra):
        return Said(who=FIRST_OFFICER, what=what, text=folded,
                    language=language, addressed=addressed, **extra)

    words = folded.split()
    if bare and len(words) > BARE_WITHIN_WORDS:
        return None
    for code in _languages(language):
        trial = list(words)
        commands = _take(trial, _COMMAND_WORDS.get(code, {}))
        offer = _take(trial, _OFFER_WORDS.get(code, {})) if offered else []
        answers = _take(trial, _ANSWER_WORDS.get(code, {}))
        value, rest = _figure(trial, code)
        rest = [w for w in rest if w not in _FILLERS]
        if bare and rest:
            continue
        if commands:
            return said(commands[0][1])
        if offer:
            return said(offer[0][1])
        kinds = [name for _, name in answers]
        if value is not None:
            kinds.insert(0, "value")
        if kinds:
            return said(ANSWER, answers=tuple(dict.fromkeys(kinds)),
                        value=value)
    return None


# What the pilot tells the first officer outside a checklist. Said bare,
# with no address, because none of it is ever said to a controller -- a
# go-around report to the tower has a callsign in it and is longer.
_TOLD: dict[str, tuple[str, ...]] = {
    QUIET: ("quiet cockpit", "sterile cockpit", "silence please",
            "cockpit silencieux", "silence cockpit", "ruhiges cockpit",
            "stille cockpit"),
    CALLOUTS_ONLY: ("callouts only", "only callouts", "just the callouts",
                    "annonces seulement", "seulement les annonces",
                    "nur ausrufe", "alleen callouts"),
    NORMAL: ("normal cockpit", "back to normal", "you can talk",
             "retour a la normale", "cockpit normal", "wieder normal",
             "normales cockpit", "weer normaal"),
    GO_AROUND: ("go around", "going around", "go around flaps",
                "remise de gaz", "remise des gaz", "durchstarten",
                "doorstarten"),
}
TOLD_WITHIN_WORDS = 4

# How a statement of speeds opens, and the label of each speed in it.
_SPEEDS_OPEN = ("speeds", "v speeds", "takeoff speeds", "take off speeds",
                "vitesses", "geschwindigkeiten", "snelheden")
_SPEED_LABELS: dict[str, tuple[str, ...]] = {
    "v1": ("v one", "vee one", "v1", "v un", "v eins", "v een"),
    "vr": ("rotate", "rotation", "vr", "v r", "rotieren", "roteren"),
    "v2": ("v two", "vee two", "v2", "v deux", "v zwei", "v twee"),
    "vapp": ("approach speed", "vref", "v ref", "vapp", "v app",
             "vitesse d'approche", "vitesse d approche",
             "anfluggeschwindigkeit", "naderingssnelheid"),
}
_MINIMUMS_OPEN = ("minimums", "minimum", "minima", "decision altitude",
                  "decision height", "mda", "da", "dh")
SPEEDS_WITHIN_WORDS = 16

# The reserve the pilot states, and in what.
_RESERVE_OPEN = ("fuel reserve", "reserve fuel", "minimum fuel", "reserve",
                 "reserve carburant", "carburant minimum", "minimum carburant",
                 "mindestkraftstoff", "minimum brandstof")
_UNITS: dict[str, tuple[str, ...]] = {
    "gal": ("gallons", "gallon", "gal", "gallonen"),
    "l": ("litres", "liters", "litre", "liter", "l"),
    "kg": ("kilos", "kilograms", "kilogrammes", "kilo", "kg"),
    "lb": ("pounds", "pound", "lbs", "lb", "livres", "pfund", "pond"),
}


def _speeds(text: str, folded: str, language: str):
    """The speeds in a statement of them, or None if it is not one."""
    labelled = any(re.search(rf"^{re.escape(p)}\b", folded)
                   for ps in _SPEED_LABELS.values() for p in ps)
    if not (labelled or any(folded == o or folded.startswith(o + " ")
                            for o in _SPEEDS_OPEN)):
        return None
    if len(folded.split()) > SPEEDS_WITHIN_WORDS:
        return None
    words = folded.split()
    for opening in sorted(_SPEEDS_OPEN, key=len, reverse=True):
        if folded.startswith(opening + " "):
            words = words[len(opening.split()):]
            break
    # A label starts a speed of its own and the figures after it are that
    # speed; figures with no label are V1, VR, V2 in that order.
    labels = sorted(((tuple(p.split()), name) for name, ps in
                     _SPEED_LABELS.items() for p in ps),
                    key=lambda pn: -len(pn[0]))
    pieces: list[tuple[str, list[str]]] = [("", [])]
    at = 0
    while at < len(words):
        for phrase, name in labels:
            if tuple(words[at:at + len(phrase)]) == phrase:
                pieces.append((name, []))
                at += len(phrase)
                break
        else:
            pieces[-1][1].append(words[at])
            at += 1
    found: list[tuple[str, float]] = []
    for name, chunk in pieces:
        value, _rest = _figure(chunk, language)
        if value is None:
            continue
        if name:
            found.append((name, value))
        else:
            unnamed = _split_figures(text, value, language)
            found += list(zip(("v1", "vr", "v2"), unnamed))
    return tuple(found) or None


def _split_figures(text: str, value: float, language: str) -> list[float]:
    """Several speeds said one after another: at the recogniser's commas,
    or, failing those, every three digits."""
    figures = []
    for part in re.split(r"[,;]", text):
        v, _ = _figure(fold(part).split(), language)
        if v is not None:
            figures.append(v)
    if len(figures) > 1:
        return figures
    digits = str(int(value))
    if len(digits) > 3 and len(digits) % 3 == 0:
        return [float(digits[i:i + 3]) for i in range(0, len(digits), 3)]
    return [value]


def _told(folded: str, text: str, language: str, address: str) -> Said | None:
    """A mode, a go-around, speeds or minimums, or None."""
    words = folded.split()
    for what, phrases in _TOLD.items():
        if folded in phrases and len(words) <= TOLD_WITHIN_WORDS:
            return Said(who=FIRST_OFFICER, what=what, text=text,
                        language=language, addressed=address)
    speeds = _speeds(text, folded, language)
    if speeds:
        return Said(who=FIRST_OFFICER, what=SPEEDS, text=text,
                    language=language, addressed=address, speeds=speeds)
    for opening in sorted(_RESERVE_OPEN, key=len, reverse=True):
        if folded.startswith(opening + " ") and len(words) <= 8:
            rest = words[len(opening.split()):]
            unit = "gal"
            for name, spellings in _UNITS.items():
                if rest and rest[-1] in spellings:
                    unit, rest = name, rest[:-1]
                    break
            value, _rest = _figure(rest, language)
            if value is not None:
                return Said(who=FIRST_OFFICER, what=RESERVE, text=text,
                            language=language, addressed=address,
                            value=value, unit=unit)
    for opening in sorted(_MINIMUMS_OPEN, key=len, reverse=True):
        if folded.startswith(opening + " ") and len(words) <= 8:
            value, _rest = _figure(words[len(opening.split()):], language)
            if value is not None:
                return Said(who=FIRST_OFFICER, what=MINIMUMS, text=text,
                            language=language, addressed=address,
                            value=value)
    return None


# What the pilot asks the first officer. Each pattern is matched on the
# folded words; "(?P<name>...)" is the place asked about. A question with a
# station's name in it -- "Kortrijk Information, which runway is in use" --
# is a question to the controller, and so is "what's the QNH" on its own,
# which pilots ask the frequency as often as anybody: the QNH is the first
# officer's only when they are addressed.
_QUESTIONS: tuple[tuple[str, str, bool], ...] = (
    # (asked, pattern, needs an address)
    ("qnh", r"\b(qnh|altimeter( setting)?)\b", True),
    ("said", r"\bwhat did (he|she|they|the controller|tower|ground) say\b"
             r"|\bqu'?est ce qu'?il a dit\b|\bqu'?a t il dit\b"
             r"|\bwas hat (er|sie) gesagt\b|\bwat zei (hij|ze)\b", False),
    ("runway", r"\b(which|what) runway\b|\bquelle piste\b"
               r"|\bwelche (piste|bahn)\b|\bwelke baan\b", False),
    ("squawk", r"\b(our|what) squawk\b|\bnotre (squawk|code)\b"
               r"|\bquoi notre squawk\b|\bquel (squawk|code transpondeur)\b"
               r"|\bunser squawk\b|\bonze squawk\b", False),
    ("clearance", r"\bread (me )?(back )?the clearance\b"
                  r"|\bwhat (was|is|s) (the|our) clearance\b"
                  r"|\brelis (moi )?la clairance\b|\bquelle (etait la )?clairance\b"
                  r"|\blies (mir )?die freigabe\b|\blees de klaring\b", False),
    ("circuit", r"\bwhere are we\b|\bon est ou\b|\bou (sommes nous|on est)\b"
                r"|\bwo sind wir\b|\bwaar zijn we\b", False),
    ("frequency", r"\bfrequency (for|of) (?P<name>.+)$"
                  r"|\bfrequence (de|d'|pour) ?(?P<name2>.+)$"
                  r"|\bfrequenz (fur|von) (?P<name3>.+)$"
                  r"|\bfrequentie (van|voor) (?P<name4>.+)$", False),
    ("distance", r"\bhow far\b|\bhow many miles\b|\ba quelle distance\b"
                 r"|\bon est a combien\b|\bwie weit\b|\bhoe ver\b", False),
)
QUESTION_WITHIN_WORDS = 10
# A word that makes a question one to a controller.
_STATION_WORDS = frozenset({"tower", "ground", "approach", "radar", "control",
                            "delivery", "center", "centre", "departure",
                            "information", "radio", "unicom", "traffic"})

# Handing the radio over. The call asked for, by its words.
_HANDOVERS: tuple[tuple[str, str], ...] = (
    ("readback", r"\byou read (it|that) back\b|\bread (it|that) back for me\b"
                 r"|\byou do the readback\b|\bcollationne\b"
                 r"|\blies (du )?zuruck\b|\blees jij terug\b"),
    ("taxi", r"\bcall (ground|the ground) for (taxi|the taxi)\b"
             r"|\bask (ground )?for taxi\b|\bappelle le sol pour le roulage\b"
             r"|\bdemande le roulage\b|\bruf (ground|das vorfeld) fur rollen\b"
             r"|\broep ground op voor taxi\b"),
    ("clearance", r"\bget us (our|the) clearance\b|\bask for (our|the) "
                  r"clearance\b|\bdemande la clairance\b"
                  r"|\bhol (uns )?die freigabe\b|\bvraag de klaring\b"),
    ("ready", r"\btell (him|her|them) we('re| are) ready\b"
              r"|\bdis (lui|leur) qu'?on est prets?\b"
              r"|\bsag (ihm|ihr) wir sind bereit\b|\bzeg dat we klaar zijn\b"),
    ("any", r"\byou make the call\b|\byou (call|talk to) (him|her|them)\b"
            r"|\byou take the radio\b|\bfais l'?appel\b|\bprends la radio\b"
            r"|\bmach du den funk\b|\bdoe jij de oproep\b"),
)

# The circuit reports, by the words for each in the four languages. "Report
# final" on its own is the tower's instruction, and a pilot repeating it is
# reading it back, so the bare verb hands over only when the first officer
# was addressed; otherwise it takes "call", "for me", or "you".
_CIRCUIT_CALLS: tuple[tuple[str, str], ...] = (
    ("downwind", r"downwind|vent arriere|gegenanflug|wind ?mee"),
    ("base", r"base|queranflug"),
    ("final", r"finale?|endanflug"),
    ("vacated", r"(runway )?vacated|(the )?runway clear|piste degagee"
                r"|piste (frei|verlassen)|baan vrij"),
)
for _call, _words in _CIRCUIT_CALLS:
    _HANDOVERS += ((_call,
                    rf"\b(call|you call|you report) (the )?({_words})\b"
                    rf"|\breport (the )?({_words}) for me\b"
                    rf"|\bannonce (la |le |l )?({_words})\b"
                    rf"|\bmelde du (den |die )?({_words})\b"
                    rf"|\bmeld jij (de )?({_words})\b"),)
_ADDRESSED_HANDOVERS: tuple[tuple[str, str], ...] = tuple(
    (_call, rf"\breport (the )?({_words})\b")
    for _call, _words in _CIRCUIT_CALLS)


def _question(folded: str, text: str, language: str,
              address: str) -> Said | None:
    words = folded.split()
    if not address and (len(words) > QUESTION_WITHIN_WORDS):
        return None
    for asked, pattern, needs_address in _QUESTIONS:
        m = re.search(pattern, folded)
        if not m:
            continue
        if needs_address and not address:
            return None
        name = next((v for k, v in m.groupdict().items() if v), "") \
            if m.groupdict() else ""
        if not address and asked != "frequency" and any(
                w in _STATION_WORDS for w in words):
            return None
        if not address and asked == "frequency":
            before = folded[:m.start()].split()
            if any(w in _STATION_WORDS for w in before):
                return None
        return Said(who=FIRST_OFFICER, what=QUESTION, text=text,
                    language=language, addressed=address, asked=asked,
                    name=name.strip())
    return None


def _handover(folded: str, text: str, language: str,
              address: str) -> Said | None:
    calls = _HANDOVERS + (_ADDRESSED_HANDOVERS if address else ())
    for call, pattern in calls:
        if re.search(pattern, folded):
            return Said(who=FIRST_OFFICER, what=HANDOVER, text=text,
                        language=language, addressed=address, call=call)
    return None


def fold(text: str) -> str:
    """Lower case, no accents, no punctuation, single spaces.

    The accents go because the recogniser's French is not reliably accented
    and a table written with them would match half of what it should. Nothing
    downstream reads this -- it is only ever matched against -- so the loss
    does not reach anything a pilot sees.
    """
    out = (text or "").replace("\u00df", "ss")
    out = unicodedata.normalize("NFKD", out)
    out = "".join(c for c in out if not unicodedata.combining(c))
    out = out.lower()
    out = re.sub(r"[^a-z0-9' ]+", " ", out)
    return re.sub(r"\s+", " ", out).strip()


def _addressed(folded: str, language: str, names,
               first_officer=()) -> tuple[str, str]:
    """Who this was said to, and the words that say so.

    Every language's table is tried, not only the one the recogniser decided
    on: a pilot flying a German crew says "cabin crew" as often as not, and
    the languages here share no words with each other, so trying all of them
    costs nothing and catches that.
    """
    head = " ".join(folded.split()[:ADDRESS_WITHIN_WORDS])
    if not head:
        return "", ""

    # The purser's own first name, which is the most natural way to address
    # somebody and the only address this program invents rather than reads.
    for name in names or ():
        cleaned = fold(name)
        if cleaned and re.search(rf"\b{re.escape(cleaned)}\b", head):
            return CABIN, cleaned
    # And the first officer's.
    for name in first_officer or ():
        cleaned = fold(name)
        if cleaned and re.search(rf"{re.escape(cleaned)}", head):
            return FIRST_OFFICER, cleaned

    tables = [_ADDRESS.get(language, {})] + [
        table for code, table in _ADDRESS.items() if code != language
    ]
    best = ("", "")
    for table in tables:
        for who, phrases in table.items():
            for phrase in phrases:
                if re.search(rf"\b{re.escape(phrase)}\b", head):
                    # The longest match wins, so "ground crew" is not read as
                    # a cabin address by way of "crew" appearing in both.
                    if len(phrase) > len(best[1]):
                        best = (who, phrase)
    return best


def _asked(folded: str, language: str, table) -> str:
    """The first request the words match, in this language then in English."""
    for code in (language, "en"):
        for what, pattern in table.get(code, ()):
            if re.search(pattern, folded):
                return what
    return ATTENTION


def parse(text: str, language: str = "en", names=(), first_officer=(),
          checklists=None, expecting: bool = False,
          offered: bool = False) -> Said | None:
    """Who in the aeroplane that was for, and what they were asked.

    ``None`` means nobody: the transmission is for the controller, which is
    the answer for all but a handful of the things a pilot says.

    ``first_officer`` is the first officer's name, ``checklists`` the
    checklists they have (key -> titles, in any language). ``expecting``
    is the caller saying a bare answer may be for them -- an item is
    waiting, no readback is owed, they spoke last -- and ``offered`` that
    they have just offered a checklist.
    """
    folded = fold(text)
    if not folded:
        return None
    who, address = _addressed(folded, language, names, first_officer)
    # The address itself is taken out before the request is looked for, so
    # that "ground crew" does not read as a request for boarding by way of
    # its own word, and a purser called Water is not a drinks order.
    rest = folded.replace(address, " ", 1) if address else folded
    rest = " ".join(rest.split())
    if who == FIRST_OFFICER:
        told = (_told(rest, text, language, address)
                or _handover(rest, text, language, address)
                or _question(rest, text, language, address))
        if told is not None:
            return told
        return (_to_the_first_officer(rest, language, checklists,
                                      address, offered)
                or Said(who=who, what=ATTENTION, text=text,
                        language=language, addressed=address))
    if who:
        table = _CABIN_ASKS if who == CABIN else _GROUND_ASKS
        return Said(who=who, what=_asked(rest, language, table), text=text,
                    language=language, addressed=address)

    # Nobody addressed. What is only ever said on the flight deck -- a
    # mode, a go-around called bare, speeds, minimums -- is the first
    # officer's.
    told = (_told(folded, text, language, "")
            or _handover(folded, text, language, "")
            or _question(folded, text, language, ""))
    if told is not None:
        return told
    # A checklist by name, or a command that says "checklist", is the first
    # officer's whatever is waiting -- if it is short enough not to be a
    # radio call that mentions one.
    if (_checklist_named(folded, checklists) is not None
            and len(folded.split()) <= CHECKLIST_WITHIN_WORDS):
        return _to_the_first_officer(folded, language, checklists, "",
                                     offered)
    if expecting or offered:
        found = _for_the_first_officer(folded, language, checklists,
                                       bare=True, offered=offered)
        if found is not None and (expecting or found.what in (ACCEPT,
                                                              DECLINE)):
            return replace(found, text=text)
    return None


def _to_the_first_officer(rest: str, language: str, checklists,
                          address: str, offered: bool) -> Said | None:
    """What the first officer was told, read leniently: they were addressed,
    or the words say checklist."""
    named = _checklist_named(rest, checklists)
    found = _for_the_first_officer(rest, language, checklists,
                                   addressed=address, offered=offered)
    if found is not None and found.what in (HOLD, CONTINUE, FROM_THE_TOP,
                                            SKIP, SAY_AGAIN):
        return found
    if named is not None:
        return Said(who=FIRST_OFFICER, what=CHECKLIST, text=rest,
                    language=language, addressed=address, checklist=named)
    return found


__all__ = [
    "Said", "parse", "fold",
    "CABIN", "GROUND", "FIRST_OFFICER",
    "CHECKLIST", "ANSWER", "SKIP", "HOLD", "CONTINUE", "SAY_AGAIN",
    "FROM_THE_TOP", "ACCEPT", "DECLINE", "SPEEDS", "MINIMUMS", "QUIET",
    "CALLOUTS_ONLY", "NORMAL", "GO_AROUND", "RESERVE", "QUESTION",
    "HANDOVER",
    "DRINK", "MEAL", "STATUS", "SECURE", "TURBULENCE", "THANKS", "ATTENTION",
    "PUSHBACK", "STOP_PUSHBACK", "CONFIRM", "BOARDING", "DEBOARDING",
    "REFUEL", "CATERING", "JETWAY", "STAIRS", "DEICE", "GPU",
    "GROUND_SERVICES", "ADDRESS_WITHIN_WORDS",
]
