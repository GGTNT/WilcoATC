"""Understanding a pilot who is not speaking English or French.

English and French each have a hand-written parser, because each is doing
something the other is not: English carries the FAA/ICAO split and French reads
flight numbers as whole numbers. Spanish, German, Italian and Portuguese differ
from each other only in vocabulary, so they share one parser and bring their
own :class:`Grammar` -- the same arrangement as
:mod:`wilcoatc.atc.phrasebook`, and for the same reason.

Two things make a single parser possible.

*Accents are folded before anything is matched.* A transcript may or may not
carry them, and Whisper is inconsistent about it inside a noisy transmission.
Every pattern below is therefore written unaccented, which is not a spelling
mistake but the normalised form.

*Numbers are read by :func:`wilcoatc.atc.numbers.parse_number`*, which is
built by inverting the same spellers the controller speaks with. That is what
lets "eintausenddreizehn" and "mil e treze" both come back as 1013 without
either language needing its own number code here.

What a language still has to say for itself is which words introduce a value
and which words mean which intent. That is what a ``Grammar`` holds, and it is
data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import numbers as num
from .vocabulary import snap

# --------------------------------------------------------------------------
# the shape of a language
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Grammar:
    """Everything the parser needs to know about one language.

    Every string is a regular expression fragment, written against folded
    text: lower case, no accents, no punctuation. Alternations are bare, so
    they can be dropped into a larger pattern without bracketing.
    """

    language: str

    # Words that introduce or follow a value.
    level: str            # "flight level", collapsed to a single token
    feet: str             # the unit, for an altitude read as a whole number
    heading: str          # introduces a heading
    decimal: str          # sits between the megahertz and the fraction
    runway: str           # introduces a runway
    sides: dict[str, str]  # side words, mapped to L / R / C
    squawk: str           # introduces a transponder code
    knots: str            # the unit, for a speed
    speed_lead: str       # introduces a speed given without its unit
    qnh: str              # introduces a pressure setting
    vertical: str         # verbs a bare altitude may follow
    # What introduces a level the flight is only to expect, as opposed to the
    # one it is cleared to now. A clearance carries both and they have to be
    # told apart, or reading one back correctly is refused.
    expect_lead: str
    atis_lead: str        # words an ATIS letter may follow
    direct: str           # introduces a fix
    # Being held short of a runway that is not the one you are taxiing to.
    # This is the instruction, and the runway it names is checked.
    hold_short: str
    # The holding point of the runway you are taxiing to, which is a report of
    # where you are rather than an instruction about somewhere else. Reading
    # the two as one phrase meant a correct taxi readback was refused, and
    # refused with the wrong runway.
    holding_point: str
    pattern_left: str
    pattern_right: str
    legs: tuple[tuple[str, str], ...]   # local leg name -> English leg name

    rules: tuple[tuple[str, str], ...]  # intent name -> pattern, most specific first
    readback: str                       # words that mean an instruction is coming back
    stations: str                       # who the pilot addressed

    # Substitutions applied before anything else, for the few places where a
    # phrase has to become one token so a pattern can anchor on it.
    cleanup: tuple[tuple[str, str], ...] = ()

    # Digit words beyond the ones the controller speaks, because a pilot may
    # use the ordinary word where the controller uses the aviation one.
    extra_digits: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------

_PUNCTUATION = re.compile(r"[,;:()\[\]!?]+")
_SENTENCE_STOP = re.compile(r"\.(?!\d)")


def normalize(text: str, grammar: Grammar) -> str:
    """Fold a transmission into the form every pattern is written against."""
    out = num.fold(text or "").strip()
    out = _SENTENCE_STOP.sub(" ", out)
    out = _PUNCTUATION.sub(" ", out)
    out = re.sub(r"['’]\s*", " ", out)
    # A thousands separator inside a spoken figure, which recognition writes
    # both ways: "1 013" and "1013" have to end up the same.
    out = re.sub(r"(\d)[\s ](\d{3})\b", r"\1\2", out)
    out = re.sub(rf"\b(?:{grammar.level})\b", " flightlevel ", out)
    out = re.sub(r"\bf\.?\s?l\.?\s*(\d)", r"flightlevel \1", out)
    out = re.sub(rf"\b(?:{grammar.decimal})\b", " decimalpoint ", out)
    out = re.sub(r"\bmayday mayday mayday\b", "mayday", out)
    out = re.sub(r"\bpan pan pan\b", "panpan", out)
    out = re.sub(r"\bpan[- ]pan\b", "panpan", out)
    for pattern, replacement in grammar.cleanup:
        out = re.sub(pattern, replacement, out)
    out = re.sub(r"\s+", " ", out).strip()
    # The aviation words are the ones recognition spells worst; put them back
    # before any rule has to match on them.
    return snap(out, grammar.language)


_RUN_DIGITS: dict[str, dict[str, str]] = {}


def run_digits(grammar: Grammar) -> dict[str, str]:
    """The words that may be collapsed into a run of figures.

    The controller's own digit words, plus whatever else the language calls
    those digits. A pilot who says "due" where the controller says "due" is no
    trouble; a German pilot who says "zwei" where the controller says "zwo"
    would be, so both are here.
    """
    table = _RUN_DIGITS.get(grammar.language)
    if table is None:
        table = {num.fold(word): figure
                 for figure, word in num.DIGITS[grammar.language].items()}
        table.update(grammar.extra_digits)
        _RUN_DIGITS[grammar.language] = table
    return table


def digit_runs(norm: str, grammar: Grammar) -> str:
    """``"tres cinco cero"`` -> ``"350"``, leaving every other word alone."""
    table = run_digits(grammar)
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            out.append("".join(run))
            run.clear()

    for token in norm.split():
        if token in table:
            run.append(table[token])
        elif token.isdigit() and len(token) == 1:
            run.append(token)
        else:
            flush()
            out.append(token)
    flush()
    return " ".join(out)


# --------------------------------------------------------------------------
# picking a number out of a sentence
# --------------------------------------------------------------------------


def _number_tokens(language: str) -> frozenset[str]:
    """Every word that may appear inside a spoken number."""
    return frozenset(num.word_values(language)) | num._GLUE.get(
        language, frozenset())


def number_before(norm: str, keyword: str, language: str) -> int | None:
    """The whole number immediately preceding a keyword.

    The walk backwards stops at the first word that cannot be part of a
    number, so "contacte madrid torre ciento veintiuno decimalpoint" yields 121
    and never drags the facility name in with it.
    """
    match = re.search(rf"\b(?:{keyword})\b", norm)
    if not match:
        return None
    return _walk(norm[:match.start()].split(), language, backwards=True)


def number_after(norm: str, keyword: str, language: str) -> int | None:
    """The whole number immediately following a keyword."""
    match = re.search(rf"\b(?:{keyword})\b", norm)
    if not match:
        return None
    return _walk(norm[match.end():].split(), language, backwards=False)


def _walk(tokens: list[str], language: str, backwards: bool) -> int | None:
    allowed = _number_tokens(language)
    glue = num._GLUE.get(language, frozenset())
    taken: list[str] = []
    for token in (reversed(tokens) if backwards else tokens):
        if token in allowed or token.isdigit():
            taken.insert(0, token) if backwards else taken.append(token)
        else:
            break
    while taken and taken[0] in glue:
        taken.pop(0)
    while taken and taken[-1] in glue:
        taken.pop()
    if not taken:
        return None
    if all(t.isdigit() for t in taken):
        return int("".join(taken))
    return num.parse_number(" ".join(taken), language)


# --------------------------------------------------------------------------
# value extraction
# --------------------------------------------------------------------------


def split_expected(norm: str, grammar: Grammar) -> tuple[str, str]:
    """Everything before a level to expect, and everything from it on.

    "Ascienda inicialmente a cinco mil pies, espere nivel de vuelo tres cinco
    cero" holds two levels, and only the first one is a clearance. Splitting
    at the word that introduces the second is what lets the first be read.
    """
    if not grammar.expect_lead:
        return norm, ""
    match = re.search(rf"\b(?:{grammar.expect_lead})\b", norm)
    if match is None:
        return norm, ""
    return norm[:match.start()], norm[match.start():]


def extract_altitude(norm: str, grammar: Grammar) -> int | None:
    """The level the flight is cleared to, not the one it is told to expect."""
    cleared, expected = split_expected(norm, grammar)
    if expected:
        found = _one_altitude(cleared, grammar)
        if found is not None:
            return found
    return _one_altitude(norm, grammar)


def _one_altitude(norm: str, grammar: Grammar) -> int | None:
    """Feet, from a flight level read in digits or an altitude read whole."""
    collapsed = digit_runs(norm, grammar)

    match = re.search(r"\bflightlevel\s*(\d{2,3})\b", collapsed)
    if match:
        return int(match.group(1)) * 100

    match = re.search(rf"\b(\d{{3,5}})\s*(?:{grammar.feet})\b", collapsed)
    if match:
        return int(match.group(1))

    value = number_before(norm, grammar.feet, grammar.language)
    if value is not None and 100 <= value <= 60000:
        return int(round(value / 100.0) * 100)

    # A level given without its unit: "suba cinco mil".
    value = number_after(norm, grammar.vertical, grammar.language)
    if value is not None and 500 <= value <= 60000 and value % 100 == 0:
        return int(value)
    return None


def extract_heading(norm: str, grammar: Grammar) -> int | None:
    collapsed = digit_runs(norm, grammar)
    match = re.search(rf"\b(?:{grammar.heading})\s*(\d{{1,3}})\b", collapsed)
    if not match:
        return None
    value = int(match.group(1))
    return ((value % 360) or 360) if 0 < value <= 360 else None


def extract_frequency(norm: str, grammar: Grammar) -> float | None:
    """``ciento veintiuno decimal siete`` -> 121.7."""
    whole = number_before(norm, "decimalpoint", grammar.language)
    if whole is not None and 118 <= whole <= 136:
        tail = norm.split("decimalpoint", 1)[1] if "decimalpoint" in norm else ""
        pieces = digit_runs(tail, grammar).split()
        fraction = pieces[0] if pieces and pieces[0].isdigit() else ""
        if fraction:
            value = float(f"{whole}.{fraction[:3]}")
            if 118.0 <= value <= 136.999:
                return round(value, 3)

    match = re.search(r"\b(1[0-3]\d[.,]\d{1,3})\b", norm.replace(",", "."))
    if match:
        value = float(match.group(1))
        return value if 118.0 <= value <= 136.999 else None
    return None


def extract_runway(norm: str, grammar: Grammar) -> str | None:
    """The runway assigned, which is never the one being held short of.

    A hold-short names a runway the aircraft must not enter. Reading it as the
    assignment meant answering a hold-short correction contradicted an item
    nobody had challenged, and the exchange could not be got out of.
    """
    collapsed = digit_runs(norm, grammar)
    sides = "|".join(grammar.sides)
    collapsed = re.sub(
        rf"\b(?:{grammar.hold_short})\b"
        rf"(?:\s+\w+){{0,3}}?\s+(?:{grammar.runway})?\s*\d{{1,2}}\b\s*"
        rf"(?:{sides})?", " ", collapsed)
    match = re.search(
        rf"\b(?:{grammar.runway})\s*(\d{{1,2}})\s*(?:({sides})\b)?", collapsed)
    if not match:
        return None
    number = int(match.group(1))
    if not 1 <= number <= 36:
        return None
    return f"{number:02d}{grammar.sides.get(match.group(2) or '', '')}"


def extract_squawk(norm: str, grammar: Grammar) -> str | None:
    collapsed = digit_runs(norm, grammar)
    match = re.search(rf"\b(?:{grammar.squawk})\s*(\d{{4}})\b", collapsed)
    if match and all(c in "01234567" for c in match.group(1)):
        return match.group(1)
    return None


def extract_speed(norm: str, grammar: Grammar) -> int | None:
    """A speed is a whole number, unlike a heading."""
    value = number_before(norm, grammar.knots, grammar.language)
    if value is not None and 60 <= value <= 400:
        return value
    match = re.search(rf"\b(?:{grammar.speed_lead})\s+(\d{{2,3}})\b",
                      digit_runs(norm, grammar))
    if match:
        value = int(match.group(1))
        return value if 60 <= value <= 400 else None
    return None


def extract_qnh(norm: str, grammar: Grammar) -> float | None:
    value = number_after(norm, grammar.qnh, grammar.language)
    if value is not None and 900 <= value <= 1100:
        return float(value)
    # Inches, where the state uses them: "dos nueve nueve dos".
    collapsed = digit_runs(norm, grammar)
    match = re.search(rf"\b(?:{grammar.qnh}|altimetro|altimeter)\s*(\d{{4}})\b",
                      collapsed)
    if match:
        value = int(match.group(1))
        if 2700 <= value <= 3200:
            return value / 100.0
    return None


_ALPHABET_REVERSE: dict[str, str] = {}


def _alphabet_reverse() -> dict[str, str]:
    if not _ALPHABET_REVERSE:
        from .speech import _ALPHABET

        _ALPHABET_REVERSE.update({num.fold(v): k for k, v in _ALPHABET.items()})
        _ALPHABET_REVERSE.update({"alfa": "A", "juliett": "J", "xray": "X",
                                  "x ray": "X"})
    return _ALPHABET_REVERSE


def extract_atis_letter(norm: str, grammar: Grammar) -> str:
    words = norm.split()
    lead = re.compile(rf"^(?:{grammar.atis_lead})$")
    for index, word in enumerate(words):
        if lead.match(word):
            for candidate in words[index + 1:index + 3]:
                letter = _alphabet_reverse().get(candidate)
                if letter:
                    return letter
    return ""


def extract_fix(norm: str, grammar: Grammar) -> str:
    match = re.search(rf"\b(?:{grammar.direct})\s+([a-z]{{3,6}})\b", norm)
    if not match:
        return ""
    word = match.group(1)
    if num.word_values(grammar.language).get(word) is not None:
        return ""
    return word.upper()


def values(norm: str, grammar: Grammar) -> dict[str, Any]:
    """Every value a transmission carries, in whichever language it was said."""
    found: dict[str, Any] = {}
    for key, extractor in (
        ("altitude_ft", extract_altitude),
        ("heading", extract_heading),
        ("frequency", extract_frequency),
        ("runway", extract_runway),
        ("squawk", extract_squawk),
        ("speed_kt", extract_speed),
    ):
        value = extractor(norm, grammar)
        if value is not None:
            found[key] = value

    # The level to expect later, kept apart from the one just issued so a
    # readback of both is read as a readback of both.
    _, expected = split_expected(norm, grammar)
    if expected:
        later = _one_altitude(expected, grammar)
        if later is not None and later != found.get("altitude_ft"):
            found["target_altitude_ft"] = later

    qnh = extract_qnh(norm, grammar)
    if qnh is not None:
        found["altimeter"] = qnh

    letter = extract_atis_letter(norm, grammar)
    if letter:
        found["atis_letter"] = letter
    fix = extract_fix(norm, grammar)
    if fix:
        found["fix"] = fix

    # "Manténgase antes de la pista cero dos" puts an article and a
    # preposition between the instruction and the runway, and German puts
    # nothing at all. A few short words are allowed to sit in the gap rather
    # than every language having to spell its own out, and the digits have to
    # have been collapsed first or the runway is two separate words.
    runs = digit_runs(norm, grammar)
    held = re.search(
        rf"\b(?:{grammar.hold_short})\b"
        rf"(?:\s+\w+){{0,3}}?\s+(?:{grammar.runway})?\s*(\d{{1,2}})\b\s*"
        rf"({'|'.join(grammar.sides)})?", runs)
    if held and 1 <= int(held.group(1)) <= 36:
        side = grammar.sides.get((held.group(2) or ""), "")
        found["hold_short"] = f"{int(held.group(1)):02d}{side}"
    elif re.search(rf"\b(?:{grammar.hold_short}|{grammar.holding_point})\b",
                   norm):
        # Held short of something, without saying what. A pilot at the holding
        # point saying so, or a recogniser that dropped the runway: either way
        # the restriction was acknowledged, and refusing it would be pedantry.
        found["hold_short"] = True
    if re.search(rf"\b(?:{grammar.pattern_left})\b", norm):
        found["pattern_direction"] = "left"
    elif re.search(rf"\b(?:{grammar.pattern_right})\b", norm):
        found["pattern_direction"] = "right"
    for local, english in grammar.legs:
        if re.search(rf"\b(?:{local})\b", norm):
            found["pattern_leg"] = english
            break
    return found


# --------------------------------------------------------------------------
# the languages
# --------------------------------------------------------------------------

SPANISH = Grammar(
    language="es",
    level=r"nivel de vuelo|nivel",
    feet=r"pies|ft",
    heading=r"rumbo",
    decimal=r"decimal|coma",
    runway=r"pista",
    sides={"izquierda": "L", "derecha": "R", "centro": "C",
           "central": "C", "hierba": "G", "agua": "W"},
    expect_lead=r"espere|esperen|prevea|preveamos",
    squawk=r"transpondedor|squawk|codigo",
    knots=r"nudos|kt",
    speed_lead=r"velocidad|reduzca a|reduzca|mantenga",
    qnh=r"qnh",
    vertical=r"ascienda a|ascienda|suba a|suba|descienda a|descienda|mantenga",
    atis_lead=r"informacion|info|atis|con|recibi|tengo",
    direct=r"directo a|directo",
    hold_short=r"mantengase antes|mantengan antes|mantengamonos antes|mantenemos antes|mantenga antes|esperamos antes|espere antes",
    holding_point=r"punto de espera",
    pattern_left=r"circuito por la izquierda|circuito izquierdo",
    pattern_right=r"circuito por la derecha|circuito derecho",
    legs=(("viento en cola", "downwind"), ("base", "base"),
          ("final", "final"), ("tramo inicial", "upwind")),
    rules=(
        ("emergency", r"\b(mayday|declaro emergencia|emergencia)\b"),
        ("urgency", r"\bpanpan\b"),
        ("minimum_fuel", r"\b(combustible minimo|minimo de combustible)\b"),

        ("going_around", r"\b(motor y al aire|vamos al aire|al aire)\b"),
        ("missed_approach", r"\b(aproximacion frustrada|frustrada)\b"),

        ("say_again", r"\b(repita|no (le )?(copie|copio|entendi)|"
                      r"no he entendido|repita por favor)\b"),
        ("radio_check", r"\b(prueba de radio|como me (copia|recibe)|"
                        r"control de radio)\b"),

        ("request_clearance", r"\b(solicito (la )?autorizacion|"
                              r"autorizacion de salida|listo para copiar|"
                              r"solicito (la )?puesta en marcha|"
                              r"autorizacion ifr)\b"),
        ("request_vfr_departure", r"\b(salida vfr|solicito salida vfr)\b"),
        ("request_pushback", r"\b(retroceso|solicito (el )?retroceso|"
                             r"listo para (el )?retroceso|solicito push)\b"),
        ("request_taxi_to_parking", r"\b(rodaje a (la )?(plataforma|"
                                    r"estacionamiento)|a la plataforma|"
                                    r"al estacionamiento|solicito parking)\b"),
        ("request_taxi", r"\b(solicito (el )?rodaje|listo para rodar|"
                         r"para (el )?rodaje)\b"),
        ("request_pattern_work",
         r"\b(circuito de trafico|circuitos|toco y despego|"
         r"toque y despegue|la opcion|permanecer en (el )?circuito|"
         r"pasada baja)\b"),
        ("request_transition",
         r"\b(solicito (el )?transito|transito por (su|la) (zona|ctr)|"
         r"atravesar (su|la) (zona|ctr)|sobrevolar el campo)\b"),
        ("request_flight_following",
         r"\b(servicio de informacion|informacion de vuelo|"
         r"seguimiento radar|solicito asesoramiento)\b"),
        ("request_takeoff", r"\b(listo para (la )?salida|listo para despegar|"
                            r"listo para (el )?despegue|solicito (el )?despegue|"
                            r"en (el )?punto de espera)\b"),

        ("request_frequency_change", r"\b(solicito cambio de frecuencia|"
                                     r"dejo la frecuencia)\b"),
        ("cancel_ifr", r"\b(cancelo (el )?ifr|cancelacion ifr)\b"),

        ("report_field_in_sight", r"\b((campo|pista|aerodromo) a la vista)\b"),
        ("report_traffic_in_sight", r"\b(trafico a la vista|tengo el trafico)\b"),
        ("report_established", r"\b(establecidos? en (el )?(localizador|loc|ils|"
                               r"eje))\b"),

        ("request_approach", r"\b(solicito (la )?aproximacion|"
                             r"aproximacion (ils|rnav|gps|visual))\b"),
        ("request_vectors", r"\b(solicito vectores|vectores radar|"
                            r"guiado radar)\b"),
        ("request_landing", r"\b(para (el )?aterrizaje|solicito aterrizar|"
                            r"en final|completo)\b"),
        ("request_direct", r"\b(solicito directo|directo a)\b"),
        ("request_descent", r"\b(solicito (el )?descenso|solicito descender|"
                            r"(?:nivel|flightlevel) inferior)\b"),
        ("request_climb", r"\b(solicito (el )?ascenso|solicito ascender|"
                          r"(?:nivel|flightlevel) superior)\b"),

        ("check_in", r"\b(buenos dias|buenas tardes|buenas noches|hola|"
                     r"le escuchamos|(?<!ascienda )(?<!descienda )"
                     r"(?<!mantenga )(ascendiendo|descendiendo|estable|"
                     r"pasando|mantenemos))\b"),
        ("position_report", r"\b((?:{number}) millas al (norte|sur|este|oeste)|"
                            r"vertical del campo|en (viento en cola|base|final))\b"),

        ("unable", r"\b(imposible|no puedo)\b"),
        ("standby", r"\b(espere|aguarde)\b"),
        ("affirmative", r"\b(afirmo|afirmativo|correcto|exacto)\b"),
        ("negative", r"\b(negativo|incorrecto)\b"),
        ("acknowledge", r"\b(recibido|entendido|copiado|de acuerdo|wilco)\b"),
    ),
    readback=r"\b(autoriz\w*|ascien\w*|sub\w*|descien\w*|mantenga|vire|gire|"
             r"rumbo|transpondedor|ruede|rodaje|punto de espera|contacte|qnh|"
             r"pista|flightlevel|directo|reduzca|velocidad|nudos|grados|"
             r"alinee|alineese)\b",
    stations=r"\b(tierra|torre|autorizaciones|entrega|aproximacion|salida|"
             r"control|centro|informacion|radio|trafico)\b",
    extra_digits={"un": "1", "una": "1"},
)


GERMAN = Grammar(
    language="de",
    level=r"flugflache|flugflaeche|flight level",
    feet=r"fuss|fuß|feet|ft",
    heading=r"kurs|steuerkurs",
    decimal=r"komma|dezimal",
    runway=r"piste|bahn|startbahn|landebahn",
    sides={"links": "L", "rechts": "R", "mitte": "C",
           "gras": "G", "wasser": "W"},
    expect_lead=r"erwarten sie|erwarten wir|erwarten",
    squawk=r"squawk|transponder|kennung",
    knots=r"knoten|kt",
    speed_lead=r"geschwindigkeit|reduzieren sie auf|halten sie",
    qnh=r"qnh",
    vertical=r"steigen sie auf|steigen|sinken sie auf|sinken|halten sie|halten",
    atis_lead=r"information|info|atis|mit|habe",
    direct=r"direkt nach|direkt",
    hold_short=r"halten (?:sie |wir )?kurz vor|halten (?:sie |wir )?vor|warten (?:sie |wir )?vor",
    holding_point=r"rollhalt",
    pattern_left=r"linksverkehr|linke platzrunde",
    pattern_right=r"rechtsverkehr|rechte platzrunde",
    legs=(("gegenanflug", "downwind"), ("queranflug", "base"),
          ("endanflug", "final"), ("final", "final"), ("abflug", "upwind")),
    rules=(
        ("emergency", r"\b(mayday|notfall|ich erklare einen notfall)\b"),
        ("urgency", r"\bpanpan\b"),
        ("minimum_fuel", r"\b(minimum fuel|mindestkraftstoff)\b"),

        ("going_around", r"\b(durchstarten|wir starten durch|"
                         r"ich starte durch|durchgestartet)\b"),
        ("missed_approach", r"\b(fehlanflug|fehlanflugverfahren)\b"),

        ("say_again", r"\b(wiederholen sie|nicht verstanden|"
                      r"sagen sie (es )?noch einmal|unverstandlich)\b"),
        ("radio_check", r"\b(funkprobe|funkcheck|wie horen sie mich)\b"),

        ("request_clearance", r"\b(erbitte (die )?freigabe|streckenfreigabe|"
                              r"bereit zum mitschreiben|erbitte anlassfreigabe|"
                              r"ifr freigabe)\b"),
        ("request_vfr_departure", r"\b(vfr abflug|erbitte vfr abflug)\b"),
        ("request_pushback", r"\b(pushback|erbitte push|zuruckstossen|"
                             r"erbitte rucksto\w*)\b"),
        ("request_taxi_to_parking", r"\b(zum vorfeld|zur parkposition|"
                                    r"zum standplatz|zur abstellposition)\b"),
        ("request_taxi", r"\b(erbitte (das )?rollen|rollbereit|zum rollen)\b"),
        ("request_pattern_work",
         r"\b(platzrunde|platzrunden|touch and go|die option|"
         r"in der platzrunde bleiben|tiefanflug)\b"),
        ("request_transition",
         r"\b(erbitte (den )?durchflug|durchflug durch|transit durch|"
         r"uberflug des platzes|kontrollzone durchfliegen)\b"),
        ("request_flight_following",
         r"\b(fluginformationsdienst|radarberatung|"
         r"erbitte verkehrsinformation)\b"),
        ("request_takeoff", r"\b(startbereit|erbitte (den )?start|"
                            r"bereit zum abflug|am rollhalt)\b"),

        ("request_frequency_change", r"\b(erbitte frequenzwechsel|"
                                     r"verlasse die frequenz)\b"),
        ("cancel_ifr", r"\b(ifr annullieren|annulliere ifr)\b"),

        ("report_field_in_sight", r"\b((platz|piste|feld|bahn) in sicht)\b"),
        ("report_traffic_in_sight", r"\b(verkehr in sicht)\b"),
        ("report_established", r"\b(etabliert auf (dem )?(localizer|loc|ils|"
                               r"landekurs))\b"),

        ("request_approach", r"\b(erbitte (den )?anflug|ils anflug|"
                             r"sichtanflug|rnav anflug)\b"),
        ("request_vectors", r"\b(erbitte radarfuhrung|radarfuhrung|vektoren)\b"),
        ("request_landing", r"\b(zur landung|erbitte (die )?landung|"
                            r"im endanflug|voll)\b"),
        ("request_direct", r"\b(erbitte direkt|direkt nach)\b"),
        ("request_descent", r"\b(erbitte (den )?sinkflug|"
                            r"erbitte (eine )?tiefere (flugflache|flightlevel))\b"),
        ("request_climb", r"\b(erbitte (den )?steigflug|"
                          r"erbitte (eine )?hohere (flugflache|flightlevel))\b"),

        ("check_in", r"\b(guten (tag|morgen|abend)|hallo|"
                     r"(?<!steigen )(?<!sinken )(?<!halten )"
                     r"(im steigflug|im sinkflug|stabil|passieren|passieren wir))\b"),
        ("position_report", r"\b((?:{number}) meilen (nordlich|sudlich|ostlich|westlich)|"
                            r"uber dem platz|im (gegenanflug|queranflug|"
                            r"endanflug))\b"),

        ("unable", r"\b(nicht moglich|unable)\b"),
        ("standby", r"\b(warten sie|bleiben sie auf empfang)\b"),
        ("affirmative", r"\b(affirm|positiv|richtig|korrekt)\b"),
        ("negative", r"\b(negativ|falsch)\b"),
        ("acknowledge", r"\b(verstanden|roger|wilco|in ordnung)\b"),
    ),
    readback=r"\b(freigab\w*|steig\w*|sink\w*|halten|drehen|kurs|squawk|"
             r"transponder|roll\w*|rollhalt|rufen|qnh|piste|bahn|flightlevel|"
             r"direkt|reduzier\w*|geschwindigkeit|knoten|grad|einreihen)\b",
    stations=r"\b(rollkontrolle|boden|turm|tower|vorfeld|anflug|abflug|radar|"
             r"kontrolle|zentrale|information|funk|verkehr|director)\b",
    extra_digits={"zwei": "2", "ein": "1", "eine": "1", "eins": "1",
                  "drei": "3", "fuenf": "5"},
)


ITALIAN = Grammar(
    language="it",
    level=r"livello di volo|livello",
    feet=r"piedi|ft",
    heading=r"prua|rotta",
    decimal=r"decimale|virgola",
    runway=r"pista",
    sides={"sinistra": "L", "destra": "R", "centro": "C",
           "centrale": "C", "erba": "G", "acqua": "W"},
    expect_lead=r"attenda|attendiamo|preveda|prevediamo",
    squawk=r"transponder|squawk|codice",
    knots=r"nodi|kt",
    speed_lead=r"velocita|riduca a|riduca|mantenga",
    qnh=r"qnh",
    vertical=r"salga a|salga|scenda a|scenda|mantenga",
    atis_lead=r"informazione|informazioni|info|atis|con|ricevuto|ho",
    direct=r"diretto a|diretto|diretti a|diretti",
    hold_short=r"mantenga prima|manteniamo prima|attenda prima|attendiamo prima",
    holding_point=r"punto attesa|attesa pista",
    pattern_left=r"circuito sinistro|circuito a sinistra",
    pattern_right=r"circuito destro|circuito a destra",
    legs=(("sottovento", "downwind"), ("base", "base"),
          ("finale", "final"), ("iniziale", "upwind")),
    rules=(
        ("emergency", r"\b(mayday|dichiaro emergenza|emergenza)\b"),
        ("urgency", r"\bpanpan\b"),
        ("minimum_fuel", r"\b(carburante minimo|minimo carburante)\b"),

        ("going_around", r"\b(riattacc\w+)\b"),
        ("missed_approach", r"\b(mancato avvicinamento)\b"),

        ("say_again", r"\b(ripeta|non ho capito|non copiato|ripetere|"
                      r"non ho ricevuto)\b"),
        ("radio_check", r"\b(prova radio|come mi ricev\w+|controllo radio)\b"),

        ("request_clearance", r"\b(chiedo (l )?autorizzazione|"
                              r"autorizzazione alla partenza|pronto a copiare|"
                              r"chiedo (la )?messa in moto|autorizzazione ifr)\b"),
        ("request_vfr_departure", r"\b(partenza vfr|chiedo partenza vfr)\b"),
        ("request_pushback", r"\b(chiedo (il )?push|spinta indietro|"
                             r"pronto al push|pushback)\b"),
        ("request_taxi_to_parking", r"\b(rullaggio al parcheggio|"
                                    r"verso il parcheggio|al piazzale|"
                                    r"alla piazzola)\b"),
        ("request_taxi", r"\b(chiedo (il )?rullaggio|pronto al rullaggio|"
                         r"per il rullaggio)\b"),
        ("request_pattern_work",
         r"\b(circuito|circuiti|tocca e riparti|opzione|"
         r"rimanere in circuito|passaggio basso)\b"),
        ("request_transition",
         r"\b(chiedo (il )?transito|transito nella (vostra )?(zona|ctr)|"
         r"attraversare la (zona|ctr)|sorvolo del campo)\b"),
        ("request_flight_following",
         r"\b(servizio informazioni|informazioni di volo|"
         r"assistenza radar)\b"),
        ("request_takeoff", r"\b(pronto al decollo|chiedo il decollo|"
                            r"pronto per la partenza|al punto attesa)\b"),

        ("request_frequency_change", r"\b(chiedo (il )?cambio (di )?frequenza|"
                                     r"lascio la frequenza)\b"),
        ("cancel_ifr", r"\b(cancello (l )?ifr|cancellazione ifr)\b"),

        ("report_field_in_sight", r"\b((campo|pista|aeroporto) in vista)\b"),
        ("report_traffic_in_sight", r"\b(traffico in vista|ho il traffico)\b"),
        ("report_established", r"\b(stabilizzati? sul (localizzatore|loc|ils|"
                               r"sentiero))\b"),

        ("request_approach", r"\b(chiedo (l )?avvicinamento|"
                             r"avvicinamento (ils|rnav|gps|a vista))\b"),
        ("request_vectors", r"\b(chiedo vettori|guida radar|vettoramento)\b"),
        ("request_landing", r"\b(per (l )?atterraggio|chiedo (l )?atterraggio|"
                            r"in finale|completo)\b"),
        ("request_direct", r"\b(chiedo diretto|diretto a)\b"),
        ("request_descent", r"\b(chiedo (la )?discesa|chiedo di scendere|"
                            r"(?:livello|flightlevel) inferiore)\b"),
        ("request_climb", r"\b(chiedo (la )?salita|chiedo di salire|"
                          r"(?:livello|flightlevel) superiore)\b"),

        ("check_in", r"\b(buongiorno|buonasera|salve|"
                     r"(?<!salga )(?<!scenda )(?<!mantenga )"
                     r"(in salita|in discesa|stabile|passando|manteniamo))\b"),
        ("position_report", r"\b((?:{number}) miglia a (nord|sud|est|ovest)|"
                            r"verticale (del )?campo|in (sottovento|base|finale))\b"),

        ("unable", r"\b(impossibile|non posso)\b"),
        ("standby", r"\b(attenda|attendete|resti in ascolto)\b"),
        ("affirmative", r"\b(affermo|affermativo|corretto|esatto)\b"),
        ("negative", r"\b(negativo|sbagliato)\b"),
        ("acknowledge", r"\b(ricevuto|capito|copiato|va bene|wilco)\b"),
    ),
    readback=r"\b(autorizz\w*|salg\w*|scend\w*|mantenga|viri|prua|transponder|"
             r"rull\w*|punto attesa|contatti|qnh|pista|flightlevel|diretto|"
             r"riduca|velocita|nodi|gradi|allinei)\b",
    stations=r"\b(terra|ground|torre|autorizzazioni|avvicinamento|partenza|"
             r"controllo|centro|informazioni|radio|traffico)\b",
    extra_digits={"un": "1", "una": "1", "tre": "3"},
)


PORTUGUESE = Grammar(
    language="pt",
    level=r"nivel de voo|nivel",
    feet=r"pes|ft",
    heading=r"proa|rumo",
    decimal=r"decimal|virgula",
    runway=r"pista",
    sides={"esquerda": "L", "direita": "R", "centro": "C",
           "central": "C", "relva": "G", "agua": "W"},
    expect_lead=r"espere|esperamos|aguarde|aguardamos",
    squawk=r"transponder|squawk|codigo",
    knots=r"nos|kt",
    speed_lead=r"velocidade|reduza para|reduza|mantenha",
    qnh=r"qnh",
    vertical=r"suba para|suba|desca para|desca|mantenha",
    atis_lead=r"informacao|info|atis|com|recebi|tenho",
    direct=r"directo para|directo a|directo|direto para|direto a|direto",
    hold_short=r"mantenha antes|mantemos antes|aguarde antes|aguardamos antes|espere antes",
    holding_point=r"ponto de espera",
    pattern_left=r"circuito a esquerda|circuito pela esquerda",
    pattern_right=r"circuito a direita|circuito pela direita",
    legs=(("vento em cauda", "downwind"), ("perna do vento", "downwind"),
          ("base", "base"), ("final", "final")),
    rules=(
        ("emergency", r"\b(mayday|declaro emergencia|emergencia)\b"),
        ("urgency", r"\bpanpan\b"),
        ("minimum_fuel", r"\b(combustivel minimo|minimo de combustivel)\b"),

        ("going_around", r"\b(arremet\w+|motor e no ar)\b"),
        ("missed_approach", r"\b(aproximacao (falhada|perdida))\b"),

        ("say_again", r"\b(repita|nao percebi|nao copiei|repetir|"
                      r"nao entendi)\b"),
        ("radio_check", r"\b(teste de radio|como me recebe|controlo de radio)\b"),

        ("request_clearance", r"\b(peco (a )?autorizacao|autorizacao de partida|"
                              r"pronto para copiar|peco (o )?arranque|"
                              r"autorizacao ifr)\b"),
        ("request_vfr_departure", r"\b(partida vfr|peco partida vfr)\b"),
        ("request_pushback", r"\b(peco push|pushback|recuo|"
                             r"pronto para recuar)\b"),
        ("request_taxi_to_parking", r"\b(para a placa|para o estacionamento|"
                                    r"para o parque|para a plataforma)\b"),
        ("request_taxi", r"\b(peco (o )?taxi|pronto para taxiar|"
                         r"para (a )?rolagem|peco rolagem)\b"),
        ("request_pattern_work",
         r"\b(circuito|circuitos|toca e arranca|a opcao|"
         r"permanecer no circuito|passagem baixa)\b"),
        ("request_transition",
         r"\b(peco (o )?transito|transito pela (vossa )?(zona|ctr)|"
         r"atravessar a (zona|ctr)|sobrevoar o campo)\b"),
        ("request_flight_following",
         r"\b(servico de informacao|informacao de voo|"
         r"seguimento radar)\b"),
        ("request_takeoff", r"\b(pronto para descolar|peco (a )?descolagem|"
                            r"pronto para (a )?partida|no ponto de espera|"
                            r"pronto para decolar)\b"),

        ("request_frequency_change", r"\b(peco mudanca de frequencia|"
                                     r"deixo a frequencia)\b"),
        ("cancel_ifr", r"\b(cancelo (o )?ifr|cancelamento ifr)\b"),

        ("report_field_in_sight", r"\b((campo|pista|aerodromo) a vista)\b"),
        ("report_traffic_in_sight", r"\b(trafego a vista|tenho o trafego)\b"),
        ("report_established", r"\b(estabelecidos? no (localizador|loc|ils|"
                               r"eixo))\b"),

        ("request_approach", r"\b(peco (a )?aproximacao|"
                             r"aproximacao (ils|rnav|gps|visual))\b"),
        ("request_vectors", r"\b(peco vetores|peco vectores|guiamento radar)\b"),
        ("request_landing", r"\b(para aterrar|peco (a )?aterragem|"
                            r"peco (o )?pouso|em final|completo)\b"),
        ("request_direct", r"\b(peco directo|peco direto|direct[oa] para)\b"),
        ("request_descent", r"\b(peco (a )?descida|peco para descer|"
                            r"(?:nivel|flightlevel) inferior)\b"),
        ("request_climb", r"\b(peco (a )?subida|peco para subir|"
                          r"(?:nivel|flightlevel) superior)\b"),

        ("check_in", r"\b(bom dia|boa tarde|boa noite|ola|"
                     r"(?<!suba )(?<!desca )(?<!mantenha )"
                     r"(a subir|a descer|estavel|a passar|mantemos))\b"),
        ("position_report", r"\b((?:{number}) milhas a (norte|sul|este|oeste)|"
                            r"vertical do campo|em (final|base|vento em cauda))\b"),

        ("unable", r"\b(impossivel|nao posso)\b"),
        ("standby", r"\b(aguarde|espere)\b"),
        ("affirmative", r"\b(afirmativo|afirmo|correcto|correto|exacto)\b"),
        ("negative", r"\b(negativo|errado)\b"),
        ("acknowledge", r"\b(recebido|entendido|copiado|wilco|esta bem)\b"),
    ),
    readback=r"\b(autoriz\w*|sub\w*|desc\w*|mantenha|vire|proa|transponder|"
             r"taxi\w*|rolagem|ponto de espera|contacte|contate|qnh|pista|"
             r"flightlevel|directo|direto|reduza|velocidade|nos|graus|alinhe)\b",
    stations=r"\b(solo|terra|torre|autorizacoes|entrega|aproximacao|partida|"
             r"controlo|controle|centro|informacao|radio|trafego)\b",
    extra_digits={"um": "1", "uma": "1", "tres": "3"},
)


_ALTERNATION: dict[str, str] = {}


def number_alternation(language: str) -> str:
    """Every way a small whole number can arrive, as a regex alternation.

    A position report is spoken -- "dieci miglia a nord" -- so a rule that
    only accepts figures misses most of them. Built by spelling out the
    numbers with the same speller the controller uses, so the rule and the
    speech cannot drift apart.
    """
    found = _ALTERNATION.get(language)
    if found is None:
        words = {str(n) for n in range(1, 61)}
        for value in range(1, 61):
            words.add(num.fold(num.cardinal(value, language)))
        found = r"\d+|" + "|".join(sorted(words, key=lambda w: (-len(w), w)))
        _ALTERNATION[language] = found
    return found


_RULES: dict[str, tuple[tuple[str, str], ...]] = {}


def rules_for(grammar: Grammar) -> tuple[tuple[str, str], ...]:
    """A language's rule table, with its placeholders filled in."""
    found = _RULES.get(grammar.language)
    if found is None:
        numbers = number_alternation(grammar.language)
        found = tuple((name, pattern.replace("{number}", numbers))
                      for name, pattern in grammar.rules)
        _RULES[grammar.language] = found
    return found


GRAMMARS: dict[str, Grammar] = {
    "es": SPANISH, "de": GERMAN, "it": ITALIAN, "pt": PORTUGUESE,
}


def grammar_for(language: str) -> Grammar | None:
    """The grammar for a language, or ``None`` if it has its own parser."""
    return GRAMMARS.get((language or "").lower()[:2])


__all__ = [
    "Grammar", "GRAMMARS", "grammar_for", "rules_for",
    "number_alternation", "normalize", "digit_runs", "values",
    "number_before", "number_after", "extract_altitude", "extract_heading",
    "extract_frequency", "extract_runway", "extract_squawk", "extract_speed",
    "extract_qnh", "extract_atis_letter", "extract_fix",
    "SPANISH", "GERMAN", "ITALIAN", "PORTUGUESE",
]
