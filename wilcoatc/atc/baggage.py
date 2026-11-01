"""The bags: GSX's belt loader, its handler and its carts, as one flow.

The first version spawned a suitcase per bag and drove it along a belt of
its own reckoning, while GSX's handler looped his whole clip on the loader
and the carts counted on their own clock: the bag he picked up was not the
one that rode the belt, the belt's bags floated above it, and he went on
loading after the carts had gone (Innsbruck, 18 October).

GSX's models already are one flow; they only have to be driven together.
Read from GSX's own files (17 October):

* the Tug 660 belt loader animates eleven bags on its belt, ``Luggages``,
  on ``WAGON BACK LINK ORIENTATION`` frames 0-4213 -- the same variable
  and length as the handler's clip -- and the belt surface, ``Belt_Anim``,
  on ``WAGON FRONT LINK ORIENTATION`` 0-650; its bags are children of the
  belt, so they lie on it at whatever angle it is posed;
* the handler carries eleven bags of his own, one per cart slot, and his
  cfg says ``unload = 645 4213 30`` and ``load = 4213 645 30``: one clip,
  backwards to load. Played forwards, belt bag i reaches the foot of the
  belt on the very frame he picks bag i up (``GRAB``), and he puts it down
  on cart slot i at ``RELEASE``; backwards, he takes slot i and the same
  bag rides up the belt into the hold;
* the cart (FSDT_BaggageWagon) shows its slots 0-10 by a count, on its own
  ``WAGON BACK LINK ORIENTATION``: slot i is shown while the count is
  above i. Its slots, from its own model, are 0.25 m apart along its
  length at 0.69-0.73 m -- exactly the handler's eleven release points,
  which put the docked cart 5.44 m behind the loader's origin and 1.02 m
  to its left, across the loader, nose to its left (``CART_AT``).

So one frame number drives the loader and the handler, and the docked
cart's count is that frame's: how many releases it has passed, whichever
way it is played. A cart is one turn of the clip, eleven bags; a run is a
train of carts, docked one after another; a flight's bags are counted from
its seats, its load and its airline (turnaround.json, ``baggage``), and a
departure loads only what fits before its doors close.

It knows nothing of SimConnect: drives and keyframes on the turnaround's
clock, for the vehicle layer to play.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from . import paths

TRACTOR = "bag_tractor"
CART = "bag_cart"

UNLOAD = "unload"
LOAD = "load"

# -- GSX's clip (FSDT_Baggage_Loader_Man's cfg and animation; Tug 660's) --------
FPS = 30.0
#: On the loader, driving: where he is when not working (anim_loaderman_drive
#: 265-324), and the walk to his place (anim_loaderman_mantoposition 324-645).
IDLE = 324.0
READY = 645.0
LAST = 4213.0
#: Per bag i: the frame he picks it up at the foot of the belt, and the frame
#: he puts it on cart slot i (his animation, played forwards).
GRAB = (1023, 1296, 1587, 1902, 2201, 2495, 2774, 3058, 3321, 3609, 3914)
RELEASE = (1148, 1424, 1734, 2045, 2339, 2631, 2915, 3206, 3446, 3747, 4055)
BAGS_PER_CART = len(RELEASE)
CYCLE_S = (LAST - READY) / FPS
WALK_S = (READY - IDLE) / FPS
#: The belt surface's loop, frames.
BELT_FRAMES = 650.0
#: The docked cart's middle from the loader's origin (right, forward), and
#: its heading from the loader's: 1.02 m to its left, 5.44 m behind, across.
CART_AT = (-1.02, -5.44)
CART_TURN = -90.0

# -- the train ------------------------------------------------------------------
#: From one vehicle of the train to the next, reference to reference, as
#: placed in the spike (a tractor and carts 4.5 m apart, looked right).
SPACING_M = 4.5
#: A train is at most this many carts; more bags come in another train. Four
#: reached from the A321's forward hold back over the aft train's way in.
MAX_CARTS = 3
TRAIN_MS = 3.0
#: Moving the next cart up to the handler, at a walk.
SHIFT_MS = 1.0
SHIFT_S = SPACING_M / SHIFT_MS + 2.0
#: Where it sets off from, behind its line and out from the aeroplane.
FROM_OUT_M = 30.0
FROM_BACK_M = 15.0
#: What the clock needs to tell two keyframes apart.
EPS = 0.001
TRACK_STEP_S = 0.25


def _frame(x: float, z: float, heading: float, right: float,
           forward: float) -> tuple[float, float]:
    h = math.radians(heading)
    return (x + right * math.cos(h) + forward * math.sin(h),
            z - right * math.sin(h) + forward * math.cos(h))


def _local(x: float, z: float, heading: float, px: float,
           pz: float) -> tuple[float, float]:
    h = math.radians(heading)
    dx, dz = px - x, pz - z
    return (dx * math.cos(h) - dz * math.sin(h),
            dx * math.sin(h) + dz * math.cos(h))


def following(drive: list, behind_m: float, step: float = TRACK_STEP_S) -> list:
    """A drive that is ``drive`` this many metres further back along its
    own track, all the way: a cart behind its tractor.

    The tractor's drive is cut into points a quarter of a second apart and
    measured along; the follower is at each moment where the leader was
    ``behind_m`` earlier along the track -- before the start, on the
    straight line the track begins along, so the train is strung out
    behind the tractor from the first frame rather than heaped on one
    spot. Stretches where the follower stands still are left out, so it
    stands, facing the way it stopped, instead of turning to north.
    """
    if not drive:
        return []
    t0, t1 = drive[0].t0, paths.ends_at(drive)
    times = [t0 + k * step for k in range(int((t1 - t0) / step) + 1)] + [t1]
    marks = [paths.evaluate(drive, t)[:2] for t in times]
    along = [0.0]
    for a, b in zip(marks, marks[1:]):
        along.append(along[-1] + math.dist(a, b))
    start_heading = math.radians(paths.evaluate(drive, t0)[2])
    back = (-math.sin(start_heading), -math.cos(start_heading))

    def at(distance: float) -> tuple[float, float]:
        if distance <= 0.0:
            return (marks[0][0] + back[0] * -distance, marks[0][1] + back[1] * -distance)
        for i in range(1, len(along)):
            if along[i] >= distance:
                span = along[i] - along[i - 1] or 1.0
                k = (distance - along[i - 1]) / span
                a, b = marks[i - 1], marks[i]
                return a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k
        return marks[-1]

    points = [at(s - behind_m) for s in along]
    segments = []
    for (ta, a), (tb, b) in zip(zip(times, points), zip(times[1:], points[1:])):
        if tb - ta <= 1e-6 or math.dist(a, b) < 0.005:
            continue
        segments.append(paths.Segment(ta, tb, a, paths._lerp(a, b, 1 / 3),
                                      paths._lerp(a, b, 2 / 3), b, v0=1.0, v1=1.0))
    if not segments:
        p = points[0]
        heading = math.radians(paths.evaluate(drive, t0)[2])
        ahead = (p[0] + math.sin(heading) * 0.01, p[1] + math.cos(heading) * 0.01)
        segments.append(paths.Segment(t0, t0 + step, p, p, p, ahead, v0=1.0, v1=1.0))
    return segments


def cart_count(frame: float) -> int:
    """How many bags the docked cart shows at this frame of the clip: the
    releases passed, whichever way the clip is played -- forwards it fills
    at each release, backwards it empties at each pick-up from the cart,
    which is the same frame."""
    return sum(1 for r in RELEASE if frame >= r)


# -- the flight's bags ---------------------------------------------------------------


def bags_for(type_code: str, operator: str, profile: str, table: dict,
             rng: random.Random) -> tuple[int, int, str]:
    """How many bags a flight has in its forward and aft holds, and how
    that was worked out: seats x load factor x bags a passenger (by the
    kind of airline), split forward/aft, with a little spread."""
    if profile in ("light", "business"):
        # Bags go in by hand with the passengers: no hold is worked, no
        # cart is towed. A Cessna at Kortrijk had sixty-six bags.
        return 0, 0, f"{profile}: no hold to work"
    seats = table.get("seats", {}).get((type_code or "").upper())
    if seats is None:
        seats = table.get("seats_default", {}).get(profile, 150)
    low, high = table.get("load_factor", (0.75, 0.95))
    load = rng.uniform(float(low), float(high))
    per = table.get("bags_per_passenger", {})
    if profile == "widebody":
        airline = "long_haul"
    elif (operator or "").upper() in set(table.get("low_cost", ())):
        airline = "low_cost"
    else:
        airline = "legacy"
    each = float(per.get(airline, 0.8))
    spread = float(table.get("spread", 0.15))
    total = seats * load * each * rng.uniform(1.0 - spread, 1.0 + spread)
    share = float(table.get("forward_share", 0.6))
    forward = int(round(total * share))
    aft = int(round(total - forward))
    why = (f"{seats} seats x {load:.0%} x {each} bags a passenger ({airline}) "
           f"= {forward} forward, {aft} aft")
    return forward, aft, why


def carts_for(bags: int) -> int:
    """Whole carts: GSX's clip is a cart of eleven, played through."""
    return max(1, int(round(bags / BAGS_PER_CART))) if bags > 0 else 0


# -- keyframes -------------------------------------------------------------------------


def _steps(start: float, value: float, changes: list[tuple[float, float]]) -> list:
    """A value that holds and then jumps: keyframes for a linear player."""
    keys = [(start, value)]
    for t, new in changes:
        keys += [(t - EPS, value), (t, new)]
        value = new
    return keys


def _belt(spans: list[tuple[float, float]], way: float) -> list:
    """The belt surface looping through each working span, down to the
    handler unloading (forwards), up from him loading (backwards)."""
    keys = [(spans[0][0] - EPS, 0.0)] if spans else [(0.0, 0.0)]
    for t0, t1 in spans:
        t = t0
        frame = 0.0 if way > 0 else BELT_FRAMES
        keys.append((t, frame))
        while t < t1:
            to_end = (BELT_FRAMES - frame if way > 0 else frame) / FPS
            step = min(to_end, t1 - t)
            t += step
            frame += way * step * FPS
            keys.append((t, frame))
            if t < t1:
                frame = 0.0 if way > 0 else BELT_FRAMES
                keys.append((t + EPS, frame))
                t += EPS
    return keys


@dataclass
class Run:
    """One train at one hold: its drives, and the one frame its loader and
    handler play, with each cart's count following it."""

    callsign: str
    hold: str                     # the loader's kind: baggage or baggage_aft
    kind: str                     # UNLOAD | LOAD
    planned: float
    tractor_path: list
    cart_paths: list              # one drive per cart
    cart_counts: list             # per cart, (time, bags shown) keyframes
    frames: list                  # (time, clip frame): loader and handler
    belt: list                    # (time, belt surface frame)
    arrives: float                # the first cart docked
    work: list                    # (start, end) of each cart's turn of the clip
    leaves: float
    gone: float
    cycles: int
    asked: int                    # carts wanted, before the doors had their say

    @property
    def bags_done(self) -> float:
        """When the handler has walked back onto the loader: it may go."""
        return (self.work[-1][1] if self.work else self.arrives) + WALK_S

    @property
    def bags(self) -> int:
        return self.cycles * BAGS_PER_CART

    def finished(self, now: float) -> bool:
        return now >= self.gone

    def out(self, now: float) -> bool:
        return self.planned <= now < self.gone

    def working(self, now: float) -> bool:
        """Whether a cart is docked and its turn of the clip is playing."""
        return any(a <= now < b for a, b in self.work)


class Planner:
    """Plans the baggage runs."""

    def __init__(self, enabled: bool = False, seed: int | None = None):
        self.enabled = enabled
        self._seed = seed
        self.why = ""

    def plan(self, kind: str, callsign: str, hold: str, ac_x: float, ac_z: float,
             ac_heading: float, loader: tuple[float, float, float],
             now: float, carts: int, deadline: float | None = None,
             index: int = 0) -> Run | None:
        """A train of ``carts`` docked one by one at the loader, the clip
        turned once per cart -- or as many as are done by ``deadline``,
        when the doors close; None when not even one is."""
        lx, lz, facing = loader
        heading = (facing + CART_TURN) % 360.0
        dock = _frame(lx, lz, facing, *CART_AT)
        # The first cart docked, the tractor one spacing ahead of it.
        spot = _frame(dock[0], dock[1], heading, 0.0, SPACING_M)
        # The aeroplane is to the train's right (the loader faces it), so it
        # comes in from out on its left and turns onto its line -- one
        # train's length short of where it stops, which is what lays its
        # carts straight behind it (they follow the tractor's track); no
        # further, so a short train does not sweep the galley door.
        length = SPACING_M * carts + 2.0
        start = _frame(spot[0], spot[1], heading, -FROM_OUT_M, -length - FROM_BACK_M)
        swing = _frame(spot[0], spot[1], heading, -8.0, -length - 8.0)
        lined_up = _frame(spot[0], spot[1], heading, 0.0, -length)
        t0 = now + 1.5
        arriving = paths.drive_route([start, swing, lined_up, spot], TRAIN_MS, t0)
        arrives = paths.ends_at(arriving)
        asked = carts
        if deadline is not None:
            room = deadline - (arrives + 2 * WALK_S)
            fits = int((room + SHIFT_S) // (CYCLE_S + SHIFT_S)) if room > 0 else 0
            carts = min(carts, fits)
        if carts <= 0:
            self.why = (f"no time: the doors close {deadline - now:.0f} s from now and "
                        f"a cart takes {CYCLE_S + 2 * WALK_S:.0f} s")
            return None
        forwards = kind == UNLOAD
        frames = [(now, IDLE), (arrives, IDLE), (arrives + WALK_S, READY)]
        belt_spans, work, tractor = [], [], list(arriving)
        counts = [[] for _ in range(carts)]
        clock = arrives + WALK_S
        at = spot
        for k in range(carts):
            if k:
                # The next cart up to the handler: the train one spacing on.
                nxt = _frame(at[0], at[1], heading, 0.0, SPACING_M)
                shift = paths.drive_route([at, nxt], SHIFT_MS, clock,
                                          heading_from=heading)
                tractor += shift
                clock = paths.ends_at(shift)
                at = nxt
            first, last = (READY, LAST) if forwards else (LAST, READY)
            frames += [(clock, READY), (clock + EPS, first),
                       (clock + CYCLE_S, last), (clock + CYCLE_S + EPS, READY)]
            work.append((clock, clock + CYCLE_S))
            belt_spans.append((clock, clock + CYCLE_S))
            if forwards:
                changes = [(clock + (r - READY) / FPS, float(i + 1))
                           for i, r in enumerate(RELEASE)]
                counts[k] = _steps(now, 0.0, changes)
            else:
                changes = sorted((clock + (LAST - r) / FPS, float(i))
                                 for i, r in enumerate(RELEASE))
                counts[k] = _steps(now, float(BAGS_PER_CART), changes)
            clock += CYCLE_S + EPS
        frames += [(clock + WALK_S, IDLE)]
        leaves = clock + WALK_S + 2.0
        ahead = _frame(at[0], at[1], heading, 0.0, 15.0)
        away = _frame(at[0], at[1], heading, -FROM_OUT_M, 40.0)
        leaving = paths.drive_route([at, ahead, away], TRAIN_MS, leaves,
                                    heading_from=heading)
        tractor += leaving
        cart_paths = [following(tractor, SPACING_M * (k + 1)) for k in range(carts)]
        gone = paths.ends_at(leaving) + 1.0
        belt = _belt(belt_spans, 1.0 if forwards else -1.0)
        verb = "off" if forwards else "on"
        self.why = (f"train {index + 1}: a tractor and {carts} cart(s), "
                    f"{carts * BAGS_PER_CART} bags {verb}"
                    + (f" (of {asked} cart(s) wanted: the doors close first)"
                       if carts < asked else "")
                    + f", gone in {gone - now:.0f} s")
        return Run(callsign, hold, kind, now, tractor, cart_paths, counts, frames,
                   belt, arrives, work, leaves, gone, carts, asked)


def cut(run: Run, now: float) -> None:
    """Stop a run now: its loader has been called away. The handler stops
    where he is, walks back onto the loader, every cart keeps the bags it
    shows, and the train drives off -- nobody works without a cart, or a
    cart without anybody."""
    if now >= run.leaves:
        return
    from ..integrations.motion import keyframed
    frame = keyframed(run.frames)(now)
    kept = [k for k in run.frames if k[0] < now]
    if frame < READY - 1.0:
        # Not at work yet: back from wherever his walk had got to.
        run.frames = kept + [(now, frame), (now + (frame - IDLE) / FPS + EPS, IDLE)]
    else:
        run.frames = kept + [(now, frame), (now + EPS, READY),
                             (now + EPS + WALK_S, IDLE)]
    run.belt = [k for k in run.belt if k[0] < now] + [(now, keyframed(run.belt)(now))]
    run.cart_counts = [[k for k in counts if k[0] < now]
                       + [(now, keyframed(counts)(now))] for counts in run.cart_counts]
    run.work = [(a, min(b, now)) for a, b in run.work if a < now]
    run.cycles = len(run.work)
    # A drive under way is finished first: the train leaves from where it
    # stops, not from the middle of a curve.
    kept = [s for s in run.tractor_path if s.t0 < now]
    run.leaves = max(now + WALK_S + 2.0, paths.ends_at(kept) if kept else now)
    x, z, heading, _ = paths.evaluate(kept or run.tractor_path, run.leaves)
    ahead = _frame(x, z, heading, 0.0, 15.0)
    away = _frame(x, z, heading, -FROM_OUT_M, 40.0)
    leaving = paths.drive_route([(x, z), ahead, away], TRAIN_MS, run.leaves,
                                heading_from=heading)
    run.tractor_path = kept + leaving
    run.cart_paths = [following(run.tractor_path, SPACING_M * (k + 1))
                      for k in range(len(run.cart_paths))]
    run.gone = paths.ends_at(leaving) + 1.0


__all__ = ["Planner", "Run", "following", "cut", "cart_count", "bags_for", "carts_for",
           "TRACTOR", "CART", "UNLOAD", "LOAD", "BAGS_PER_CART", "GRAB", "RELEASE",
           "IDLE", "READY", "LAST", "CYCLE_S", "WALK_S", "CART_AT", "MAX_CARTS"]
