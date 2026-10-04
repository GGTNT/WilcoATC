"""Understanding a French-speaking pilot.

The French half of :mod:`wilcoatc.atc.intents`. Same :class:`Intent` values and
the same value keys, so the controller logic is language-blind: it receives a
``ParsedIntent`` and never learns which language produced it.

The number extraction is where French differs most. A French pilot reads a
flight level digit by digit but a QNH, an altitude in feet and a frequency's
megahertz part as whole numbers, so "niveau de vol trois cinq zéro" is 35,000
while "QNH mille treize" is 1013 and "cinq mille pieds" is 5,000. Each extractor
below applies the convention for the field it is reading rather than one rule
for every number.
"""

from __future__ import annotations

import re
from typing import Any

from . import speech_fr as sfr
from .vocabulary import snap

# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------

# Recognition output varies in accents and elisions; these make it predictable
# without changing what any number means.
_CLEANUP_FR: list[tuple[str, str]] = [
    (r"(\d)[  ](\d{3})\b", r"\1\2"),
    # Punctuation has to go before anything else. A comma left in place stops a
    # digit run dead: "trois cinq zero, Air France" reads as 35 rather than 350,
    # and "QNH mille treize," as 1000 rather than 1013.
    (r"[,;:()\[\]]+", " "),
    # The elided article is written both ways; collapsing it to a space lets a
    # single pattern match "point d'attente" and "point d attente".
    (r"[’']\s*", " "),
    # Recognition hyphenates French compounds it is unsure about, and writes
    # "prêt au décollage" as "prêt-au-décollage". To a rule that matches words
    # a hyphen is a space. The numbers are read from the raw text, not from
    # this, so "quatre-vingt-dix" is not disturbed by it.
    (r"-", " "),
    (r"\bniveau de vol\b", "niveaudevol"),
    (r"\bniveau\b", "niveaudevol"),
    (r"\bF\.?L\.?\s*(\d)", r"niveaudevol \1"),
    (r"\bdécimale\b", "decimale"),
    (r"\bdecimal\b", "decimale"),
    (r"\bvirgule\b", "decimale"),
    (r"\bmayday mayday mayday\b", "mayday"),
    (r"\bpan pan pan\b", "panpan"),
    (r"\bpan-pan\b", "panpan"),
]

# Accents are folded so a transcript written without them still matches.
_ACCENTS = str.maketrans("àâäéèêëîïôöùûüçÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ",
                         "aaaeeeeiioouuucAAAEEEEIIOOUUUC")


def normalize_fr(text: str) -> str:
    """Lower-case, fold accents, and make the number words predictable."""
    out = (text or "").lower().strip()
    out = re.sub(r"[.!?]+(?!\d)", " ", out)
    for pattern, replacement in _CLEANUP_FR:
        out = re.sub(pattern, replacement, out)
    out = out.translate(_ACCENTS)
    out = re.sub(r"\s+", " ", out).strip()
    # Recognition spells the aviation words worst, because they are the ones it
    # has heard least: "clairance" comes back as "clerance". Put them back
    # before any rule has to match on them.
    return snap(out, "fr")


# Words that can appear inside a spoken French number. Used to walk backwards
# from a keyword and pick up exactly the number in front of it, rather than
# letting a regex swallow the rest of the sentence with it.
_NUMBER_TOKENS = {
    "zero", "un", "une", "deux", "trois", "quatre", "cinq", "six", "sept",
    "huit", "neuf", "dix", "onze", "douze", "treize", "quatorze", "quinze",
    "seize", "vingt", "vingts", "trente", "quarante", "cinquante", "soixante",
    "cent", "cents", "mille", "et",
}


def number_before(norm: str, keyword: str) -> int | None:
    """The French number immediately preceding a keyword.

    ``"contactez paris controle cent vingt-sept decimale ..."`` yields 127 for
    the keyword ``decimale``: the walk stops at "controle", which is not part
    of a number, so the facility name never contaminates the value.
    """
    m = re.search(rf"\b{keyword}\b", norm)
    if not m:
        return None
    tokens = [t for t in re.split(r"[\s-]+", norm[:m.start()].strip()) if t]

    taken: list[str] = []
    for token in reversed(tokens):
        if token in _NUMBER_TOKENS or token.isdigit():
            taken.insert(0, token)
        else:
            break
    while taken and taken[0] == "et":
        taken.pop(0)
    if not taken:
        return None
    if all(t.isdigit() for t in taken):
        return int("".join(taken))
    return sfr.words_to_number(" ".join(taken))


def number_after(norm: str, keyword: str) -> int | None:
    """The French number immediately following a keyword."""
    m = re.search(rf"\b{keyword}\b", norm)
    if not m:
        return None
    tokens = [t for t in re.split(r"[\s-]+", norm[m.end():].strip()) if t]
    taken: list[str] = []
    for token in tokens:
        if token in _NUMBER_TOKENS or token.isdigit():
            taken.append(token)
        elif taken:
            break
        else:
            break
    while taken and taken[-1] == "et":
        taken.pop()
    if not taken:
        return None
    if all(t.isdigit() for t in taken):
        return int("".join(taken))
    return sfr.words_to_number(" ".join(taken))


# Digit words safe to collapse into a run. As on the English side, words that
# double as ordinary vocabulary are excluded: "un" is also the article "a", so
# it only joins a run that has already started.
_RUN_DIGITS_FR = {
    "zero": "0", "un": "1", "une": "1", "deux": "2", "trois": "3",
    "quatre": "4", "cinq": "5", "six": "6", "sept": "7", "huit": "8",
    "neuf": "9",
}


def digit_runs_fr(text: str) -> str:
    """``"trois cinq zero"`` -> ``"350"``, leaving other words alone."""
    tokens = text.split()
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            out.append("".join(run))
            run.clear()

    for token in tokens:
        if token in _RUN_DIGITS_FR:
            run.append(_RUN_DIGITS_FR[token])
        elif token.isdigit() and len(token) == 1:
            run.append(token)
        else:
            flush()
            out.append(token)
    flush()
    return " ".join(out)


# --------------------------------------------------------------------------
# value extraction
# --------------------------------------------------------------------------

_RE_LEVEL_FR = re.compile(r"\bniveaudevol\s*(\d{2,3})\b")
_RE_FEET_FR = re.compile(r"\b(\d{3,5})\s*(?:pieds|ft)\b")


# What introduces a level the flight is only to expect. A clearance carries
# that as well as the level it is cleared to now, and taking the wrong one
# refused a readback that was word for word what had just been said.
# Only the verb that introduces a level. "Route prevue" -- the route as filed
# -- is in every clearance and matching it cut the sentence in half before the
# altitude had been read.
_EXPECT_FR = r"prevoyez|prevoyons|attendez vous a"


def split_expected_fr(norm: str) -> tuple[str, str]:
    """Everything before a level to expect, and everything from it on."""
    match = re.search(rf"\b(?:{_EXPECT_FR})\b", norm)
    if match is None:
        return norm, ""
    return norm[:match.start()], norm[match.start():]


def extract_altitude_fr(text: str) -> int | None:
    """The level the flight is cleared to, not the one to expect later."""
    norm = normalize_fr(text)
    cleared, expected = split_expected_fr(norm)
    if expected:
        found = _one_altitude_fr(cleared)
        if found is not None:
            return found
    return _one_altitude_fr(norm)


def _one_altitude_fr(norm: str) -> int | None:
    """Feet, from a flight level (digits) or an altitude (a whole number)."""
    collapsed = digit_runs_fr(norm)

    m = _RE_LEVEL_FR.search(collapsed)
    if m:
        return int(m.group(1)) * 100

    m = _RE_FEET_FR.search(collapsed)
    if m:
        return int(m.group(1))

    # "cinq mille pieds", "mille cinq cents pieds" -- read as a whole number.
    value = number_before(norm, "pieds")
    if value is None:
        value = number_before(norm, "ft")
    if value is not None and 100 <= value <= 60000:
        return int(round(value / 100.0) * 100)

    # A level stated without the word "pieds": "montez cinq mille".
    for verb in ("montez", "descendez", "maintenez"):
        value = number_after(norm, verb)
        if value is not None and 500 <= value <= 60000 and value % 100 == 0:
            return int(value)
    return None


_RE_HEADING_FR = re.compile(r"\bcap\s*(\d{1,3})\b")


def extract_heading_fr(text: str) -> int | None:
    collapsed = digit_runs_fr(normalize_fr(text))
    m = _RE_HEADING_FR.search(collapsed)
    if not m:
        return None
    value = int(m.group(1))
    return (value % 360) or 360 if 0 < value <= 360 else None


def extract_frequency_fr(text: str) -> float | None:
    """``cent vingt et un decimale sept`` -> 121.7."""
    norm = normalize_fr(text)

    whole = number_before(norm, "decimale")
    if whole is not None and 118 <= whole <= 136:
        tail = norm.split("decimale", 1)[1] if "decimale" in norm else ""
        frac_tokens = digit_runs_fr(tail).split()
        frac = frac_tokens[0] if frac_tokens and frac_tokens[0].isdigit() else ""
        if frac:
            value = float(f"{whole}.{frac}")
            if 118.0 <= value <= 136.999:
                return round(value, 3)

    m = re.search(r"\b(1[0-3]\d[.,]\d{1,3})\b", norm.replace(",", "."))
    if m:
        value = float(m.group(1))
        return value if 118.0 <= value <= 136.999 else None
    return None


_SIDE_FR = {"gauche": "L", "droite": "R", "centre": "C"}
_RE_RUNWAY_FR = re.compile(r"\bpiste\s*(\d{1,2})\s*(gauche|droite|centre)?\b")


def _without_hold_short_fr(collapsed: str) -> str:
    """Drop the hold-short clause, whose runway is not the assigned one."""
    return re.sub(
        rf"\b(?:{_HOLD_SHORT_FR})\s+avant\b"
        r"(?:\s+(?:de\s+)?la)?(?:\s+piste)?\s+\d{1,2}\s*"
        r"(?:gauche|droite|centre)?", " ", collapsed)


def extract_runway_fr(text: str) -> str | None:
    """The runway assigned, which is never the one being held short of.

    "Maintenez avant la piste zero deux" names a runway the aircraft must not
    enter. Reading it as the assignment meant answering a hold-short
    correction contradicted an item nobody had challenged, and the exchange
    could not be got out of.
    """
    collapsed = digit_runs_fr(normalize_fr(text))
    collapsed = re.sub(
        rf"\b(?:{_HOLD_SHORT_FR})\s+avant\b"
        r"(?:\s+(?:de\s+)?la)?(?:\s+piste)?\s+\d{1,2}\s*"
        r"(?:gauche|droite|centre)?", " ", collapsed)
    m = _RE_RUNWAY_FR.search(collapsed)
    if not m:
        return None
    number = int(m.group(1))
    if not 1 <= number <= 36:
        return None
    return f"{number:02d}{_SIDE_FR.get(m.group(2) or '', '')}"


# "Affichez" from the controller, "affichons" or "on affiche" back from the
# crew: the whole verb, rather than the three forms one side happens to use.
_RE_SQUAWK_FR = re.compile(r"\b(?:affich\w*|transpondeur|code)\s*(\d{4})\b")

# "Maintenez avant la piste 02" is the instruction; a crew reads it back as
# "maintenons avant". The verb only -- the runway follows it.
_HOLD_SHORT_FR = (r"maintenez|maintenons|maintien|maintenu|arretez|arretons|"
                  r"stoppez|stoppons")


def extract_squawk_fr(text: str) -> str | None:
    collapsed = digit_runs_fr(normalize_fr(text))
    m = _RE_SQUAWK_FR.search(collapsed)
    if m and all(c in "01234567" for c in m.group(1)):
        return m.group(1)
    return None


def extract_speed_fr(text: str) -> int | None:
    """``deux cent dix noeuds`` -- a whole number, unlike a heading."""
    norm = normalize_fr(text)
    value = number_before(norm, "noeuds")
    if value is not None and 60 <= value <= 400:
        return value
    m = re.search(r"\b(?:vitesse|reduisez a|maintenez)\s+(\d{2,3})\b",
                  digit_runs_fr(norm))
    if m:
        value = int(m.group(1))
        return value if 60 <= value <= 400 else None
    return None


def extract_qnh_fr(text: str) -> float | None:
    """``QNH mille treize`` -- a whole number, never digits."""
    norm = normalize_fr(text)
    value = number_after(norm, "qnh")
    if value is not None and 900 <= value <= 1100:
        return float(value)
    return None


_ALPHABET_FR_REVERSE = {
    v.lower(): k for k, v in sfr.ALPHABET_FR.items()
}
_ALPHABET_FR_REVERSE.update({"alfa": "A", "juliett": "J", "x-ray": "X"})


def extract_atis_letter_fr(text: str) -> str:
    words = normalize_fr(text).split()
    for i, word in enumerate(words):
        if word in ("information", "info", "atis", "avec", "recu", "j'ai"):
            for candidate in words[i + 1:i + 3]:
                letter = _ALPHABET_FR_REVERSE.get(candidate)
                if letter:
                    return letter
    return ""


def extract_fix_fr(text: str) -> str:
    m = re.search(r"\bdirect\s+(?:sur\s+)?([a-z]{3,6})\b", normalize_fr(text))
    if not m:
        return ""
    word = m.group(1)
    if word in ("le", "la", "les", "vers", "pour"):
        return ""
    return word.upper()


_ALTERNATION_FR: str = ""


def number_alternation_fr() -> str:
    """Every way a small whole number can arrive, as a regex alternation.

    A position report is spoken -- "dix milles au nord" -- so a rule that only
    accepts figures misses most of them. Built from the same words the number
    parser knows, so the two cannot drift.
    """
    global _ALTERNATION_FR
    if not _ALTERNATION_FR:
        words = {str(n) for n in range(1, 61)}
        for value, word in _CARDINAL_WORDS_BY_VALUE.items():
            if 1 <= value <= 60:
                words.add(word)
        for tens in ("vingt", "trente", "quarante", "cinquante", "soixante"):
            words.add(tens)
            for unit in ("un", "deux", "trois", "quatre", "cinq"):
                words.add(f"{tens}-{unit}")
                words.add(f"{tens} {unit}")
        _ALTERNATION_FR = r"\d+|" + "|".join(
            sorted(words, key=lambda w: (-len(w), w)))
    return _ALTERNATION_FR


# The single-word cardinals, keyed the other way round.
_CARDINAL_WORDS_BY_VALUE: dict[int, str] = {
    1: "un", 2: "deux", 3: "trois", 4: "quatre", 5: "cinq", 6: "six",
    7: "sept", 8: "huit", 9: "neuf", 10: "dix", 11: "onze", 12: "douze",
    13: "treize", 14: "quatorze", 15: "quinze", 16: "seize",
    17: "dix-sept", 18: "dix-huit", 19: "dix-neuf", 20: "vingt",
    30: "trente", 40: "quarante", 50: "cinquante", 60: "soixante",
}


# --------------------------------------------------------------------------
# intent rules
# --------------------------------------------------------------------------

# Ordered most specific first, exactly as the English table is.
RULES_FR: list[tuple[str, str]] = [
    ("emergency_details", r"\b((personnes?|ames|passagers) a bord|autonomie|"
                          r"carburant (restant|pour))\b"),
    ("emergency", r"\b(mayday|detresse|je declare une urgence)\b"),
    ("urgency", r"\b(panpan|pan pan)\b"),
    ("minimum_fuel", r"\b(carburant minimum|minimum de carburant)\b"),

    ("going_around", r"\b(remise de gaz|on remet les gaz|je remets les gaz|"
                     r"remets? les gaz)\b"),
    ("missed_approach", r"\b(approche interrompue|api)\b"),

    ("say_again", r"\b(repetez|je n\s?ai pas compris|pas compris|"
                  r"vous etes inaudible|repetition)\b"),
    ("radio_check", r"\b(essai radio|comment (me|nous|vous) recevez|controle radio|test radio)\b"),

    # A French VFR flight asks for the same thing an IFR one does -- "demande
    # l'autorisation" -- and says which rules it is flying under in the same
    # breath: "pour un vol VFR à destination de Saint-Nazaire". The VFR
    # mention is the only thing that tells the two apart, so it is read first.
    ("request_vfr_departure", r"\b(vol vfr|depart vfr|demande (un )?depart vfr|"
                              r"vfr vers|autorisation vfr)\b"),
    # The article is dropped as often as it is said on frequency, so both
    # "demande l'autorisation" and "demande autorisation" have to be read. The
    # lookahead keeps a request to land, take off, taxi or cross out of it:
    # those are their own calls, and their rules come later in this table.
    ("request_clearance", r"\b(demande (la )?clairance|autorisation de depart|"
                          r"pret? a copier|demande (la )?mise en route|"
                          r"clairance (ifr|de depart)|"
                          r"demande (?:l |la |une )?autorisation"
                          r"(?! (?:d\s?atterr|de (?:decoll|roul|travers|penetr)|"
                          r"d\s?entrer)))\b"),
    ("request_pushback", r"\b(repoussage|demande (le )?repoussage|pret? (au|pour le) repoussage|"
                         r"demande (le )?push)\b"),
    ("request_taxi_to_parking", r"\b(roulage vers (le )?parking|vers le parking|"
                                r"demande le parking|vers (mon|le) poste)\b"),
    ("request_taxi", r"\b(demande (le |du )?roulage|pret? (au|pour le) roulage|"
                     r"demande (a )?rouler)\b"),
    # Le tour de piste avant le decollage et l\'atterrissage : "pret pour
    # l\'option" demande les deux a la fois.
    ("request_pattern_work",
     r"\b(tour de piste|tours de piste|circuit d\s?aerodrome|"
     r"touch and go|pose(r|s)? decolle|option|"
     r"reste(r|)? (dans le|en) (circuit|tour de piste)|passage bas)\b"),
    ("request_transition", r"\b(demande (la )?traversee|traversee de (la )?(ctr|cta|tma|zone)|"
                           r"demande a traverser|transit(er)? (la|votre) (ctr|zone)|"
                           r"survol (du|le) terrain)\b"),
    ("request_flight_following",
     r"\b(service d\s?information|information de vol|demande le suivi radar|"
     r"suivi radar|service radar)\b"),

    ("request_takeoff", r"\b(pret? (au|pour le) depart|pret? pour le decollage|"
                        r"pret? au decollage|demande (le )?decollage|"
                        r"demande (l\s?)?autorisation (de|au) decoll\w+|"
                        r"au point d\s?\s*attente)\b"),

    ("request_frequency_change", r"\b(demande (le )?changement (de )?frequence|"
                                 r"quitte la frequence)\b"),
    ("cancel_ifr", r"\b(annul(e|ation) (de l\s?)?ifr)\b"),

    ("report_field_in_sight", r"\b((terrain|piste|aerodrome) en vue)\b"),
    ("report_traffic_in_sight", r"\b(trafic en vue|j\s?ai le trafic)\b"),
    ("report_established", r"\b(etabli(s|e)? sur (le|l\s?)\s*(loc|localizer|ils|axe)|"
                           r"nous sommes etablis)\b"),

    ("request_approach", r"\b(demande (l\s?)?approche|approche (ils|rnav|gps|a vue)|"
                         r"souhaite l\s?(ils|approche a vue))\b"),
    ("request_vectors", r"\b(demande (un )?guidage|guidage radar|demande (des )?vecteurs)\b"),
    ("request_landing", r"\b(pour (l\s?)?atterrissage|demande l\s?atterrissage|"
                        r"demande (l\s?)?autorisation d\s?atterr\w+|"
                        r"en finale|complet|en vent arriere pour)\b"),
    ("request_direct", r"\b(demande direct|direct sur)\b"),
    ("request_descent", r"\b(demande (la )?descente|demande (a|de) descendre|"
                        r"demande (un )?niveau(?:devol)? inferieur)\b"),
    ("request_climb", r"\b(demande (la )?montee|demande (a|de) monter|"
                      r"demande (un )?niveau(?:devol)? superieur)\b"),

    # Checking in, not being instructed. The level alone is not enough: an
    # instruction being read back also states a level, and "montez niveau de
    # vol trois cinq zero" is a readback, not a check-in. A greeting, an
    # explicit check-in phrase, or a level with no instruction verb in front
    # of it are what distinguish the two.
    ("check_in", r"\b(bonjour|bonsoir|on vous ecoute|a vous|"
                 r"(?<!montez )(?<!descendez )(?<!maintenez )"
                 r"(en (montee|descente)|stable|passons|passant|maintenons))\b"),
    ("position_report", r"\b((?:{number}) (milles?|nautiques?) "
                        r"(au )?(nord|sud|est|ouest)|"
                        r"verticale du terrain|en (vent arriere|base|finale|"
                        r"etape de base))\b"),

    ("unable", r"\bimpossible\b"),
    ("standby", r"\b(attendez|patientez)\b"),
    ("affirmative", r"\b(affirme|affirmatif|c\s?est exact|exact)\b"),
    ("negative", r"\b(negatif|c\s?est faux)\b"),
    ("acknowledge", r"\b(bien recu|recu|compris|d\s?accord|wilco)\b"),
]

# Words that mean the pilot is reading an instruction back.
READBACK_MARKERS_FR = re.compile(
    r"\b(autoris\w*|mont\w*|descend\w*|maintenez|maintien|tourn\w*|cap|"
    r"affich\w*|roul\w*|point d'?\s*attente|contact\w*|qnh|piste|niveaudevol|"
    r"direct|reduis\w*|vitesse|travers\w*|align\w*|noeuds|degres)\b"
)


def values_fr(text: str) -> dict[str, Any]:
    """Extract every value a French transmission may carry."""
    values: dict[str, Any] = {}
    for key, extractor in (
        ("altitude_ft", extract_altitude_fr),
        ("heading", extract_heading_fr),
        ("frequency", extract_frequency_fr),
        ("runway", extract_runway_fr),
        ("squawk", extract_squawk_fr),
        ("speed_kt", extract_speed_fr),
    ):
        found = extractor(text)
        if found is not None:
            values[key] = found

    qnh = extract_qnh_fr(text)
    if qnh is not None:
        values["altimeter"] = qnh

    # The answer to a MAYDAY: "cent cinquante personnes a bord, deux heures
    # d'autonomie".
    details = normalize_fr(text)
    for word in ("personnes", "personne", "ames", "passagers"):
        souls = number_before(details, word)
        if souls:
            values["souls"] = souls
            break
    if re.search(r"\b(autonomie|carburant)\b", details):
        minutes = (60 * (number_before(details, "heures")
                         or number_before(details, "heure") or 0)
                   + (number_before(details, "minutes") or 0))
        if minutes:
            values["fuel_minutes"] = minutes

    letter = extract_atis_letter_fr(text)
    if letter:
        values["atis_letter"] = letter
    fix = extract_fix_fr(text)
    if fix:
        values["fix"] = fix

    norm = normalize_fr(text)
    # The runway is read out digit by digit -- "point d'attente piste zero
    # six" -- so the digits have to be collapsed before the runway can be
    # picked out of it. Without this the item is seen but never the runway,
    # and the readback checker cannot tell the right runway from the wrong
    # one, which is the one readback error that has actually killed people.
    # Only the hold-short phrase. "Point d'attente piste 06" names the runway
    # being taxied to -- its holding point -- and the runway extractor already
    # reads it. Treating it as a hold-short meant a pilot who read the taxi
    # instruction back correctly was told the hold-short runway was wrong, in
    # a phrase that could not be read back either, for ever.
    held = re.search(
        rf"\b(?:{_HOLD_SHORT_FR})\s+avant\b"
        r"(?:\s+(?:de\s+)?la)?(?:\s+piste)?\s+(\d{1,2})\s*"
        r"(gauche|droite|centre)?", digit_runs_fr(norm))
    if held and 1 <= int(held.group(1)) <= 36:
        side = {"gauche": "L", "droite": "R", "centre": "C"}.get(
            (held.group(2) or ""), "")
        values["hold_short"] = f"{int(held.group(1)):02d}{side}"
    elif re.search(rf"\b(?:{_HOLD_SHORT_FR})\s+avant\b"
                   r"|\bpoint d'?\s*attente\b", norm):
        # Held short of something, without saying what. A crew reporting "point
        # d'attente piste 06" has acknowledged the restriction even though the
        # runway they named is the one they are taxiing to.
        values["hold_short"] = True
    if re.search(r"\bmain gauche\b", norm):
        values["pattern_direction"] = "left"
    elif re.search(r"\bmain droite\b", norm):
        values["pattern_direction"] = "right"
    for french, english in (("vent arriere", "downwind"), ("base", "base"),
                            ("finale", "final"), ("montee initiale", "upwind"),
                            ("etape de base", "base")):
        if re.search(rf"\b{french}\b", norm):
            values["pattern_leg"] = english
            break
    return values


# Station words, for working out who the pilot addressed.
STATION_WORDS_FR = re.compile(
    r"\b(sol|tour|prevol|pre-vol|approche|depart|controle|centre|"
    r"information|radio|trafic|attente)\b"
)


__all__ = [
    "normalize_fr", "digit_runs_fr", "values_fr", "RULES_FR",
    "number_alternation_fr",
    "READBACK_MARKERS_FR", "STATION_WORDS_FR",
    "extract_altitude_fr", "extract_heading_fr", "extract_frequency_fr",
    "extract_runway_fr", "extract_squawk_fr", "extract_speed_fr",
    "extract_qnh_fr", "extract_atis_letter_fr", "extract_fix_fr",
]
