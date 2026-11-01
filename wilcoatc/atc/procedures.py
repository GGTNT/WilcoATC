"""SIDs and STARs as a controller names them and as a route draws them.

A plan carries a procedure the way a database stores it, six characters
wide: ROUS1L, OPAL7K, WYNDE3. That is not what is said. The letters are the
fix the procedure is named after, cut short to fit -- ROUSY, OPALE -- the
digit is its issue number and the last letter its variant, so ROUS1L is
"ROUSY one Lima" on the radio and in a readback. This module puts the name
back from the route's own fixes (the procedure's namesake is almost always
one of them), and reads a readback for it.

It also answers the two geometric questions the departure asks of a route,
after LocalTC's ``procedures.py`` (read for the design only):

- the SID's first fix, when the SID is flown as a path from the runway: an
  RNAV SID's first fix is a few miles out, a radar-vector SID in a plan
  lists only the fixes far out where the vectors end;
- the first fix of the route still ahead of a flight on runway heading,
  which is where departure sends it ("proceed direct ...").
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Iterable

from ..navdata.build import bearing_deg, haversine_nm
from .route import CLB, Route, RouteFix

# A SID whose first fix is this close to the field is flown as a path from
# the runway -- an RNAV SID -- and its first fix is what the US tower names
# in the takeoff clearance (7110.65 4-3-2, "RNAV to ..."). Further out it is
# where a radar-vector SID's vectors end.
SID_PATH_NM = 15.0
# A route fix closer than this is behind or abeam already: departure sends a
# flight on runway heading to the next one (after LocalTC: 5 nm).
ON_COURSE_MIN_NM = 5.0

_DESIGNATOR = re.compile(r"^([A-Z]+?)(\d)([A-Z]?)$")

# The issue number as every controller language here says it, for reading a
# readback whatever language it came back in.
_NUMBER_WORDS = {
    "1": ("one", "wun", "un", "une", "uno", "una", "eins", "ein", "um"),
    "2": ("two", "too", "deux", "dos", "zwei", "zwo", "due", "dois"),
    "3": ("three", "tree", "trois", "tres", "drei", "tre", "três"),
    "4": ("four", "fower", "quatre", "cuatro", "vier", "quattro", "quatro"),
    "5": ("five", "fife", "cinq", "cinco", "fünf", "funf", "cinque"),
    "6": ("six", "seis", "sechs", "sei"),
    "7": ("seven", "sept", "siete", "sieben", "sette", "sete"),
    "8": ("eight", "huit", "ocho", "acht", "otto", "oito"),
    "9": ("nine", "niner", "neuf", "nueve", "neun", "nove"),
}


def designator(name: str) -> tuple[str, str, str] | None:
    """(letters, number, variant) of ``ROUS1L``, or None if it is not one."""
    found = _DESIGNATOR.match((name or "").strip().upper().replace(" ", ""))
    if not found:
        return None
    return found.group(1), found.group(2), found.group(3)


def namesake(name: str, fixes: Iterable[str] = ()) -> str:
    """The word a procedure is named after: the fix of the route whose ident
    the procedure's letters start, ROUS1L's ROUSY; else the letters."""
    parts = designator(name)
    if parts is None:
        return (name or "").strip().upper()
    letters = parts[0]
    for ident in fixes:
        ident = (ident or "").strip().upper()
        if ident.isalpha() and len(ident) > len(letters) \
                and ident.startswith(letters):
            return ident
    return letters


def written(name: str, fixes: Iterable[str] = ()) -> str:
    """``ROUSY 1L``: the name as it is printed on the chart."""
    parts = designator(name)
    if parts is None:
        return (name or "").strip().upper()
    return f"{namesake(name, fixes)} {parts[1]}{parts[2]}"


def spoken(name: str, number, word, alphabet: dict) -> str:
    """``ROUSY 1L`` as a controller says it, in the language whose digit
    (``number``), fix-name (``word``) and alphabet are handed in: "Rousy
    one Lima". A name that is not a designator is the fix name alone."""
    text = (name or "").strip().upper()
    found = re.match(r"^([A-Z]+)\s*(\d)([A-Z]?)$", text)
    if not found:
        return word(text)
    base, digit, variant = found.groups()
    bits = [word(base), number(digit)]
    if variant:
        bits.append(alphabet.get(variant, variant))
    return " ".join(bits)


def _plain(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.findall(r"[a-z]+|\d", folded.lower())


def heard(text: str, name: str, fixes: Iterable[str] = ()) -> bool:
    """Whether a readback named this procedure.

    The namesake, said or recognised close enough -- its first four letters
    -- and the issue number after it, as a digit or a word in any of the
    controller languages. The variant letter is not insisted on: the
    recogniser drops a lone "Lima" often enough, and two variants of one
    issue are the same departure on the same runway.
    """
    parts = designator(name)
    if parts is None:
        return bool(name) and name.strip().lower() in (text or "").lower()
    base = namesake(name, fixes).lower()
    words = _plain(text)
    for i, word in enumerate(words):
        if not (word == base or (len(word) >= 4 and len(base) >= 4
                                 and word[:4] == base[:4])):
            continue
        after = words[i + 1:i + 3]
        if any(w == parts[1] or w in _NUMBER_WORDS.get(parts[1], ())
               for w in after):
            return True
    return False


def _distance(a: str, b: str) -> int:
    """Edits between two words (Levenshtein), for a name heard nearly."""
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def _any_number(word: str) -> bool:
    return word.isdigit() or any(word in words for words in _NUMBER_WORDS.values())


def near(text: str, name: str, fixes: Iterable[str] = ()) -> bool:
    """Whether a readback said something *like* this procedure without
    :func:`heard` finding it: the namesake with its number missing or
    another number, or a word two letters off the namesake ("Rosy",
    "Rousey") with a number after it. That is a pilot who read it back and a
    recogniser that did not quite catch it -- or a pilot who said another
    departure -- and only the recogniser's own confidence tells which.
    """
    parts = designator(name)
    if parts is None or heard(text, name, fixes):
        return False
    base = namesake(name, fixes).lower()
    words = _plain(text)
    for i, word in enumerate(words):
        if len(word) < 3 or _any_number(word):
            continue
        same = word == base or (len(word) >= 4 and word[:4] == base[:4])
        close = same or (len(base) >= 4 and _distance(word, base) <= 2)
        if not close:
            continue
        if same or any(_any_number(w) for w in words[i + 1:i + 3]):
            return True
    return False


# A STAR joins the final at its first fix this far out on the extended
# centreline, this close to it, reached along it (after LocalTC: 4-30 nm,
# 2 nm, 45 degrees).
JOIN_OUT_NM = (4.0, 30.0)
JOIN_SIDE_NM = 2.0
JOIN_TRACK_DEG = 45.0
# Up to this far across, a fix still joins when the leg into it converges on
# the centreline: closing on it, at between CONVERGE_DEG and
# CONVERGE_MAX_DEG to it -- an intercept the procedure flies itself. A leg
# parallel to the final, or a downwind, ends elsewhere and is vectored from
# (the user's rule). Rome's XIBI3A runs RF427-RF428-SUVOK 4.7 nm off 16L,
# parallel: vectored. UNVERIFIED: CONVERGE_DEG, the least angle that is not
# "parallel", is a choice.
JOIN_CONVERGING_SIDE_NM = 5.0
CONVERGE_DEG = 5.0
CONVERGE_MAX_DEG = 30.0
# On a STAR: within this of its path (after LocalTC: 6 nm).
ON_STAR_NM = 6.0


def frame(final, lat: float, lon: float) -> tuple[float, float]:
    """(out, side) in the runway's frame, nautical miles: ``out`` along the
    extended centreline from the threshold, positive before it; ``side``
    across it, positive to the right of the landing direction."""
    distance = haversine_nm(final.lat, final.lon, lat, lon)
    if distance < 1e-6:
        return 0.0, 0.0
    bearing = bearing_deg(final.lat, final.lon, lat, lon)
    outbound = (final.course + 180.0) % 360.0
    angle = math.radians(((bearing - outbound) + 540.0) % 360.0 - 180.0)
    return distance * math.cos(angle), -distance * math.sin(angle)


def star_join(final, fixes) -> RouteFix | None:
    """The first STAR fix on ``final``'s extended centreline that the STAR
    arrives at along the final, or None when the STAR ends elsewhere."""
    if final is None:
        return None
    fixes = [f for f in fixes if not f.is_marker]
    for before, fix in zip(fixes, fixes[1:]):
        out, side = frame(final, fix.lat, fix.lon)
        if not (JOIN_OUT_NM[0] <= out <= JOIN_OUT_NM[1]
                and abs(side) <= JOIN_CONVERGING_SIDE_NM):
            continue
        was_out, was_side = frame(final, before.lat, before.lon)
        inbound = was_out - out
        if inbound <= 0:
            continue                    # flown outbound: a downwind
        angle = math.degrees(math.atan2(abs(side - was_side), inbound))
        if abs(side) <= JOIN_SIDE_NM:
            if angle <= JOIN_TRACK_DEG:
                return fix
            continue
        # Further across: only a leg closing on the centreline at an
        # intercept's angle, not one parallel to it.
        closing = abs(was_side) - abs(side)
        if closing > 0 and CONVERGE_DEG <= angle <= CONVERGE_MAX_DEG:
            return fix
    return None


def along_to(fixes, lat: float, lon: float, to: RouteFix):
    """(off the path, miles still to fly along it to ``to``) for an
    aeroplane near the path through ``fixes``; None if ``to`` is not on it
    or the aeroplane is already past it."""
    fixes = [f for f in fixes if not f.is_marker]
    if to not in fixes or len(fixes) < 2:
        return None
    target = fixes.index(to)
    best = None
    for i, (a, b) in enumerate(zip(fixes, fixes[1:])):
        lat0 = math.radians((a.lat + b.lat) / 2.0)
        ax, ay = a.lon * 60.0 * math.cos(lat0), a.lat * 60.0
        bx, by = b.lon * 60.0 * math.cos(lat0), b.lat * 60.0
        px, py = lon * 60.0 * math.cos(lat0), lat * 60.0
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        f = 0.0 if not length2 else max(0.0, min(1.0, ((px - ax) * dx
                                                       + (py - ay) * dy) / length2))
        off = math.hypot(px - (ax + f * dx), py - (ay + f * dy))
        if best is None or off < best[0]:
            best = (off, i, f)
    off, leg, f = best
    if leg >= target:
        return None
    a, b = fixes[leg], fixes[leg + 1]
    left = (1.0 - f) * haversine_nm(a.lat, a.lon, b.lat, b.lon)
    for p, q in zip(fixes[leg + 1:target], fixes[leg + 2:target + 1]):
        left += haversine_nm(p.lat, p.lon, q.lat, q.lon)
    return off, left


def sid_first_fix(route: Route | None, field_lat: float, field_lon: float,
                  sid: str = "") -> RouteFix | None:
    """The first fix of a SID flown as a path from the runway, or None."""
    if not route:
        return None
    fixes = route.procedure(sid or route.sid, CLB)
    if not fixes:
        return None
    first = fixes[0]
    if haversine_nm(field_lat, field_lon, first.lat, first.lon) <= SID_PATH_NM:
        return first
    return None


def on_course_fix(route: Route | None, lat: float, lon: float,
                  hint: int = 1) -> RouteFix | None:
    """The first fix of the route still ahead and at least five miles off."""
    if not route:
        return None
    progress = route.progress(lat, lon, hint=hint)
    start = progress.leg if progress is not None else 1
    for fix in route.fixes[start:]:
        if fix.is_marker:
            continue
        if haversine_nm(lat, lon, fix.lat, fix.lon) >= ON_COURSE_MIN_NM:
            return fix
    return None


__all__ = ["designator", "namesake", "written", "spoken", "heard", "near",
           "sid_first_fix", "frame", "star_join", "along_to", "ON_STAR_NM",
           "on_course_fix", "SID_PATH_NM", "ON_COURSE_MIN_NM"]
