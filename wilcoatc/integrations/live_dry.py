"""The live traffic end to end, without the simulator: what would be drawn.

Why this exists
---------------

The live traffic is tested on recorded answers, and the simulator is a
session away. Between the two is the whole pipeline -- the feed, the 30 s
roster, the blend, the snapping, the injector's list -- run against the real
sources at a real field. This runs it with no SimConnect and logs, every few
seconds, each aeroplane that would be created in the simulator: where it
comes from (live or invented), its type and airline, the first FSLTL model
that matches them when FSLTL is installed (the injector's own choice also
weighs the level of detail), where it would be, and what it
was put on (the runway's centreline, a stand). So it can be watched
against a flight tracker before the simulator is opened.

    WilcoATC-console.exe traffic --live --dry-run --airport EBBR --minutes 10

What it cannot do without the simulator is the scenery's own layout: the
taxiways and stands come from the simulator's facility data, so a taxiing
or parked aeroplane is shown where the feed has it, and the first line says
so. The runway centreline comes from the database and is applied.

Everything it prints is also written to ``logs/live_dry/<date-time>.jsonl``.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Callable

from ..atc.live import LiveRoster
from ..atc.phraseology import Weather
from ..atc.traffic import TrafficInjector, UserAircraft
from . import adsb

# How often the list is printed. The feed is asked at its own poll.
REPORT_S = 6.0
# Where the pilot is taken to be: above the field, out of the way of every
# stand, so the parked ones are read (they wait for the pilot's position).
USER_ABOVE_FT = 5000.0


def _runway(navdb, ident: str) -> str:
    for rw in navdb.runways(ident):
        for end, _heading, lat, lon in rw.ends():
            if lat is not None and lon is not None:
                return end
    return ""


def _fsltl_model(liveries, operator: str, type_code: str) -> str:
    if liveries is None or not type_code:
        return ""
    try:
        models = liveries.models(operator, type_code) if operator else ()
        if not models:
            models = liveries.models("", type_code)
    except Exception:
        return ""
    return models[0] if models else ""


def dry_run(config, navdb, ident: str, seconds: float, *,
            fetch: Callable | None = None,
            clock: Callable[[], float] = time.time,
            sleep: Callable[[float], None] = time.sleep,
            out: Callable[[str], None] = print,
            log_path=None) -> list[dict]:
    """Run the live pipeline round ``ident`` for ``seconds``; return every
    report written."""
    field = navdb.airport(ident.upper())
    if field is None:
        out(f"{ident}: not in the database")
        return []
    live = config.traffic.live
    feed = adsb.Feed(sources=adsb.sources_for(live.own_url), fetch=fetch,
                     radius_nm=live.radius_nm, clock=clock, wall=clock)
    book = adsb.FlightBook(None, fetch=fetch, clock=clock) if live.routes else None
    thread = adsb.LiveFeedThread(feed, book, lambda: (field.lat, field.lon),
                                 poll_s=live.poll_s, wall=clock)
    roster = LiveRoster(live.fallback_after_s, delay_s=live.live_delay_s)
    traffic = TrafficInjector(navdb, seed=config.traffic.seed or None,
                              density=1.0, callsigns=config.traffic.callsigns)
    runway = _runway(navdb, field.ident)
    traffic.set_field(field.ident, runway, runway,
                      Weather(wind_dir=0, wind_kt=0, qnh_hpa=1013))
    traffic.set_user(UserAircraft(
        callsign="DRYRUN", type_code="", latitude=field.lat,
        longitude=field.lon, altitude_ft=field.elev_ft + USER_ABOVE_FT,
        heading=0.0, ground_speed_kt=0.0, on_ground=False))
    liveries = None
    try:
        from .fsltl import load_installed

        liveries = load_installed(override=config.traffic.fsltl_rules)
    except Exception:
        liveries = None

    out(f"live traffic, dry run round {field.ident} (runway {runway or '?'}),"
        f" {live.radius_nm:.0f} nm, drawn {live.live_delay_s:.0f} s behind"
        f" the feed; no simulator, so no taxiways or stands: those are shown"
        f" where the feed has them. FSLTL "
        f"{'found' if liveries is not None else 'not found'}.")
    reports: list[dict] = []
    said = None
    started = clock()
    next_poll = next_report = started
    handle = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(log_path, "a", encoding="utf-8")
    try:
        while clock() - started < seconds:
            now = clock()
            if now >= next_poll:
                state = thread.round()
                next_poll = now + thread.poll_s
                roster.update(thread.flights, thread.answered_wall,
                              thread.routes)
                if state.status != said:
                    said = state.status
                    out(f"feed: {state.status}, source {state.source or '-'},"
                        f" {state.count} aircraft"
                        + (f", resting {state.resting}" if state.resting
                           else "")
                        + (f", dropped {state.dropped}" if state.dropped
                           else ""))
            seen = roster.seen(now, traffic.field)
            traffic.set_observed(seen, time.monotonic())
            traffic.advance(time.monotonic())
            if now >= next_report:
                next_report = now + REPORT_S
                drawn = traffic.positions(invented_only=True)
                report = {"t": round(now - started, 1), "aircraft": []}
                for p in drawn:
                    flight = traffic.flight_named(p["callsign"])
                    source = (getattr(flight, "source", "") or "invented"
                              if flight is not None else
                              "live (parked)" if p.get("phase") == "parked"
                              and p.get("observed") else "invented")
                    entry = {
                        "callsign": p["callsign"], "source": source,
                        "type": p.get("type", ""),
                        "operator": p.get("operator", ""),
                        "model": _fsltl_model(liveries, p.get("operator", ""),
                                              p.get("type", ""))
                        or "the simulator's own",
                        "lat": p["lat"], "lon": p["lon"],
                        "altitude_ft": p["altitude_ft"],
                        "heading": p["heading"],
                        "ground_speed_kt": p["ground_speed_kt"],
                        "on_ground": p["on_ground"],
                        "on": traffic.live_snapped.get(p["callsign"], "")}
                    report["aircraft"].append(entry)
                live_n = sum(1 for a in report["aircraft"]
                             if a["source"].startswith("live"))
                out(f"[{report['t']:6.0f} s] would inject "
                    f"{len(report['aircraft'])}: {live_n} live, "
                    f"{len(report['aircraft']) - live_n} invented")
                for a in report["aircraft"]:
                    out(f"    {a['callsign']:<9} {a['source']:<13} "
                        f"{a['type'] or '?':<5} {a['operator'] or '-':<4} "
                        f"{a['lat']:9.5f} {a['lon']:10.5f} "
                        f"{a['altitude_ft']:6.0f} ft {a['heading']:3.0f} "
                        f"{a['ground_speed_kt']:3.0f} kt"
                        + (" ground" if a["on_ground"] else "")
                        + (f" on {a['on']}" if a["on"] else "")
                        + f"  FSLTL match: {a['model']}")
                reports.append(report)
                if handle is not None:
                    handle.write(json.dumps(report) + "\n")
                    handle.flush()
            sleep(1.0)
    finally:
        if handle is not None:
            handle.close()
        if book is not None:
            book.save()
    return reports


def default_log_path():
    from ..paths import data_root

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return data_root() / "logs" / "live_dry" / f"{stamp}.jsonl"


__all__ = ["dry_run", "default_log_path"]
