"""Putting a mis-heard word back.

Recognition is very good and not perfect, and the words it gets wrong are the
ones that matter most: the aviation vocabulary it has heard least. "demande la
clairance" comes back as "demande la clérance", "prévol" as "prévole",
"repoussage" as "repoussarge". Each of those is one letter or two from the
word that was said, and each of them defeats a rule that matches on the phrase.

So before a rule sees the transcript, every token in it is compared against the
handful of words that actually mean something on a frequency, and a token that
is within a letter or two of one of them is replaced by it. It is the same idea
as snapping a published frequency onto a channel a radio can select: the
written form is wrong, the intent is not in doubt, and refusing to act on it
helps nobody.

The conservatism is the point. A word has to be long enough for an edit to be
evidence rather than coincidence, the distance allowed grows with the length,
and only words already on the list can be snapped to -- so "roulage" can become
"roulage" but never "repoussage", and an ordinary word the pilot happened to
use is left exactly as it was.
"""

from __future__ import annotations

import re
from functools import lru_cache

# The words worth protecting, per language: the ones a rule keys on and that a
# general-purpose recogniser has the least reason to spell correctly. Ordinary
# vocabulary is deliberately absent -- there is nothing to gain from snapping a
# word that was probably heard right.
VOCABULARY: dict[str, tuple[str, ...]] = {
    "fr": (
        "clairance", "repoussage", "roulage", "rouler", "decollage", "depart",
        "atterrissage", "attente", "prevol", "montee", "descente", "guidage",
        "approche", "localizer", "terrain", "trafic", "finale", "parking",
        "frequence", "collationnement", "affirme", "negatif", "impossible",
        "attendez", "repetez", "compris", "recu", "carburant", "minimum",
        "interrompue", "remise", "autorisation", "autorise", "niveaudevol",
        "piste", "cap", "transpondeur", "affiche", "contact", "contactez",
        "decimale", "noeuds", "degres", "pieds", "gauche", "droite", "centre",
        "vue", "vent", "arriere", "verticale", "nautiques", "milles",
        "urgence", "detresse", "essai", "radio", "changement", "mise",
        "route", "montez", "descendez", "maintenez", "alignez",
        # The infinitives the rules themselves use. A word already on the list
        # is returned untouched, which is the only way to stop "descendre"
        # being snapped onto "descente" and breaking the rule it belongs to.
        "descendre", "monter", "attendre", "repeter", "contacter", "afficher",
        "autoriser", "atterrir", "decoller", "quitter", "annuler",
    ),
    "es": (
        "autorizacion", "retroceso", "rodaje", "rodar", "despegue", "despegar",
        "aterrizaje", "aterrizar", "espera", "ascenso", "descenso", "vectores",
        "aproximacion", "localizador", "campo", "trafico", "final",
        "plataforma", "frecuencia", "colacion", "afirmo", "negativo",
        "imposible", "espere", "repita", "entendido", "recibido",
        "combustible", "minimo", "frustrada", "pista", "rumbo",
        "transpondedor", "contacte", "decimal", "nudos", "grados", "pies",
        "izquierda", "derecha", "centro", "vista", "viento", "millas",
        "emergencia", "prueba", "nivel", "vuelo", "ascienda", "descienda",
        "mantenga", "alineese", "ruede", "solicito",
        "ascender", "descender", "esperar", "repetir", "contactar",
    ),
    "de": (
        "freigabe", "pushback", "rollen", "rollbereit", "rollhalt", "start",
        "startbereit", "landung", "steigflug", "sinkflug", "flugflache",
        "radarfuhrung", "anflug", "localizer", "platz", "verkehr",
        "endanflug", "abflug", "gegenanflug", "queranflug",
        "parkposition", "frequenzwechsel", "verstanden",
        "negativ", "moglich", "warten", "wiederholen", "durchstarten",
        "fehlanflug", "piste", "kurs", "squawk", "transponder", "rufen",
        "komma", "knoten", "grad", "fuss", "links", "rechts", "mitte",
        "sicht", "wind", "meilen", "notfall", "funkprobe", "erbitte",
        "steigen", "sinken", "halten", "drehen", "einreihen",
        # On the list so it is left alone: without it "erwarten" is two edits
        # from "warten" and was being snapped onto it, which turns "expect
        # flight level three five zero" into "wait flight level three five
        # zero" and loses the only word that says which of a clearance's two
        # levels is which.
        "erwarten",
    ),
    "it": (
        "autorizzazione", "push", "rullaggio", "rullare", "decollo",
        "partenza", "atterraggio", "attesa", "salita", "discesa", "vettori",
        "avvicinamento", "localizzatore", "campo", "traffico", "finale",
        "parcheggio", "frequenza", "affermo", "negativo", "impossibile",
        "attenda", "ripeta", "capito", "ricevuto", "carburante", "minimo",
        "riattacchiamo", "mancato", "pista", "prua", "transponder",
        "contatti", "decimale", "nodi", "gradi", "piedi", "sinistra",
        "destra", "centro", "vista", "vento", "miglia", "emergenza",
        "prova", "livello", "volo", "salga", "scenda", "mantenga", "allinei",
        "chiedo", "riduca",
        "salire", "scendere", "attendere", "ripetere", "contattare",
    ),
    "pt": (
        "autorizacao", "push", "recuo", "taxi", "taxiar", "rolagem",
        "descolagem", "descolar", "partida", "aterragem", "aterrar", "espera",
        "subida", "descida", "vetores", "aproximacao", "localizador", "campo",
        "trafego", "final", "estacionamento", "frequencia", "afirmativo",
        "negativo", "impossivel", "aguarde", "repita", "entendido",
        "recebido", "combustivel", "minimo", "arremeter", "falhada", "pista",
        "proa", "transponder", "contacte", "decimal", "graus", "esquerda",
        "direita", "centro", "vista", "vento", "milhas", "emergencia",
        "teste", "nivel", "voo", "suba", "desca", "mantenha", "alinhe",
        "peco",
        "subir", "descer", "aguardar", "repetir", "contactar",
    ),
}

# How far a token may be from a word before the resemblance stops being
# evidence. Short words are left alone: two edits on five letters is a
# different word, not a mis-hearing.
def _allowed(length: int) -> int:
    if length >= 12:
        return 3
    if length >= 8:
        return 2
    if length >= 6:
        return 1
    return 0


_TOKEN = re.compile(r"[^\W\d_]+", re.UNICODE)


def _distance(a: str, b: str, limit: int) -> int:
    """Levenshtein distance, abandoned as soon as it exceeds the limit."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        best = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            current.append(value)
            best = min(best, value)
        if best > limit:
            return limit + 1
        previous = current
    return previous[-1]


_LITERAL = re.compile(r"[^\W\d_]{4,}", re.UNICODE)


def _rule_words(language: str) -> set[str]:
    """Every plain word inside a language's rules and value keywords.

    Pulled out of the regexes themselves: whatever the grammar matches on is
    something the grammar needs left alone.
    """
    patterns: list[str] = []
    try:
        if language == "fr":
            from .intents_fr import RULES_FR

            patterns = [pattern for _, pattern in RULES_FR]
        else:
            from .intents_local import GRAMMARS

            grammar = GRAMMARS.get(language)
            if grammar is None:
                return set()
            patterns = [pattern for _, pattern in grammar.rules]
            patterns += [
                grammar.level, grammar.feet, grammar.heading, grammar.decimal,
                grammar.runway, grammar.squawk, grammar.knots,
                grammar.speed_lead, grammar.vertical, grammar.atis_lead,
                grammar.direct, grammar.hold_short, grammar.readback,
                grammar.stations, grammar.pattern_left, grammar.pattern_right,
            ]
            patterns += [local for local, _ in grammar.legs]
    except Exception:                              # pragma: no cover
        return set()

    found: set[str] = set()
    for pattern in patterns:
        clean = re.sub(r"\[a-zA-Z]", " ", pattern)
        clean = re.sub(r"[()\[\]{}|?*+^$.]", " ", clean)
        found |= {word.lower() for word in _LITERAL.findall(clean)}
    return found


@lru_cache(maxsize=8)
def protected(language: str) -> frozenset[str]:
    """Words that must come out of the snapper exactly as they went in."""
    from . import numbers as num

    words: set[str] = set(_rule_words(language))
    try:
        words |= set(num.word_values(language))
    except Exception:                              # pragma: no cover
        pass
    return frozenset(words)


@lru_cache(maxsize=8)
def _by_length(language: str) -> dict[int, tuple[str, ...]]:
    """The vocabulary bucketed by length, so only plausible words are tried."""
    words = VOCABULARY.get(language, ())
    buckets: dict[int, list[str]] = {}
    for word in words:
        buckets.setdefault(len(word), []).append(word)
    return {length: tuple(found) for length, found in buckets.items()}


@lru_cache(maxsize=20000)
def nearest(token: str, language: str) -> str:
    """The vocabulary word this token was probably meant to be.

    Returns the token unchanged when nothing is close enough, which is the
    common case and the one that matters: a transcript full of ordinary words
    must come out the way it went in.
    """
    if len(token) < 6:
        return token
    words = VOCABULARY.get(language, ())
    if not words:
        return token
    if token in words:
        return token
    # A word the rules already name is never snapped, whatever it is near.
    # Numbers are in there because "ciento" is one letter from "viento" and a
    # hundred is not a wind; so are the words the grammar itself matches on,
    # because moving "quitte" to "quitter" or "abflug" to "anflug" would break
    # the very rule the word belongs to. Built from the grammars rather than
    # listed by hand, so a new rule protects its own vocabulary.
    if token in protected(language):
        return token

    limit = _allowed(len(token))
    if limit <= 0:
        return token

    best, best_distance = token, limit + 1
    buckets = _by_length(language)
    for length in range(len(token) - limit, len(token) + limit + 1):
        for word in buckets.get(length, ()):
            if _would_negate(token, word):
                continue
            distance = _distance(token, word, limit)
            if distance < best_distance:
                best, best_distance = word, distance
                if distance == 1:
                    break
        if best_distance == 1:
            break
    return best if best_distance <= limit else token


def _would_negate(token: str, word: str) -> bool:
    """Whether snapping would put a prefix on the front and reverse the sense.

    "possible" is two letters from "impossible" and means the opposite of it.
    Letters added at the *front* of a word are a different word far more often
    than they are a mis-hearing, so that direction is refused; letters added at
    the end or in the middle -- "prevole" for "prevol", "atterissage" for
    "atterrissage" -- are the shape a mis-hearing actually takes.
    """
    return len(word) > len(token) and word.endswith(token)


def snap(text: str, language: str) -> str:
    """Put every mis-heard aviation word in a transcript back.

    Runs over the already-normalised, accent-folded form, because that is what
    the vocabulary is written in and what the rules will see.
    """
    if not text or language not in VOCABULARY:
        return text

    def replace(match: re.Match) -> str:
        return nearest(match.group(0), language)

    return _TOKEN.sub(replace, text)


__all__ = ["snap", "nearest", "protected", "VOCABULARY"]
