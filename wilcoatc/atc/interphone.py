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
from dataclasses import dataclass

# Who was being spoken to.
CABIN = "cabin"
GROUND = "ground"

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


def _addressed(folded: str, language: str, names) -> tuple[str, str]:
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


def parse(text: str, language: str = "en", names=()) -> Said | None:
    """Who in the aeroplane that was for, and what they were asked.

    ``None`` means nobody: the transmission is for the controller, which is
    the answer for all but a handful of the things a pilot says.
    """
    folded = fold(text)
    if not folded:
        return None
    who, address = _addressed(folded, language, names)
    if not who:
        return None
    table = _CABIN_ASKS if who == CABIN else _GROUND_ASKS
    # The address itself is taken out before the request is looked for, so
    # that "ground crew" does not read as a request for boarding by way of
    # its own word, and a purser called Water is not a drinks order.
    rest = folded.replace(address, " ", 1) if address else folded
    return Said(who=who, what=_asked(rest, language, table), text=text,
                language=language, addressed=address)


__all__ = [
    "Said", "parse", "fold",
    "CABIN", "GROUND",
    "DRINK", "MEAL", "STATUS", "SECURE", "TURBULENCE", "THANKS", "ATTENTION",
    "PUSHBACK", "STOP_PUSHBACK", "CONFIRM", "BOARDING", "DEBOARDING",
    "REFUEL", "CATERING", "JETWAY", "STAIRS", "DEICE", "GPU",
    "GROUND_SERVICES", "ADDRESS_WITHIN_WORDS",
]
