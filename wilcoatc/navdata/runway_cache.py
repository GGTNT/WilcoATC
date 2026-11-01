"""The simulator's runways, kept per airport between sessions.

Why this exists
---------------

The runways a controller speaks and measures against are the simulator's
when it is running (``integrations/runways.py``): the scenery is what the
aeroplane rolls on, and where it differs from OurAirports -- a runway
renumbered since, a threshold moved, a runway one side has and the other
does not -- the database was the one that was wrong. But the simulator is
not always there: a replay, a test flight without it, the planner before a
flight. So the first time an airport is read from the simulator its runways
are written to ``airports/<ICAO>.json`` in the data root, and read back from
there, for 60 days, before the database is (after LocalTC, which keeps each
airport it reads as JSON in its data folder; nothing copied).

Plain JSON of the same fields :class:`wilcoatc.navdata.db.Runway` has, so
nothing downstream can tell a cached runway from a live one except by its
``source``.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, fields, replace
from pathlib import Path

log = logging.getLogger(__name__)

MAX_AGE_DAYS = 60.0
FORMAT = 1


class RunwayCache:
    """``airports/<ICAO>.json`` under ``folder``."""

    def __init__(self, folder: Path, max_age_days: float = MAX_AGE_DAYS,
                 clock=time.time):
        self.folder = Path(folder)
        self.max_age_s = float(max_age_days) * 86400.0
        self._clock = clock

    def path(self, ident: str) -> Path:
        return self.folder / f"{(ident or '').strip().upper()}.json"

    def save(self, ident: str, runways) -> None:
        from .db import Runway  # noqa: F401  (the shape written)

        data = {"format": FORMAT, "ident": ident.upper(),
                "read_at": self._clock(),
                "runways": [asdict(rw) for rw in runways]}
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            self.path(ident).write_text(json.dumps(data, indent=1),
                                        encoding="utf-8")
        except OSError:
            log.debug("could not keep %s's runways", ident, exc_info=True)

    def age_days(self, ident: str) -> float | None:
        data = self._read(ident)
        if data is None:
            return None
        return (self._clock() - float(data.get("read_at", 0.0))) / 86400.0

    def load(self, ident: str):
        """The cached runways, marked ``source="cache"``, or None when there
        are none or they are older than ``max_age_days`` (the simulator is
        then asked again; until it answers, the database is used)."""
        from .db import Runway

        data = self._read(ident)
        if data is None or data.get("format") != FORMAT:
            return None
        if self._clock() - float(data.get("read_at", 0.0)) > self.max_age_s:
            return None
        known = {f.name for f in fields(Runway)}
        try:
            return tuple(replace(Runway(**{k: v for k, v in row.items()
                                           if k in known}), source="cache")
                         for row in data.get("runways", []))
        except (TypeError, ValueError):
            log.debug("%s's cached runways are unreadable", ident,
                      exc_info=True)
            return None

    def _read(self, ident: str):
        path = self.path(ident)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def airports(self) -> list[tuple[str, float]]:
        """Every airport kept, with its age in days: for doctor."""
        out = []
        if not self.folder.is_dir():
            return out
        for path in sorted(self.folder.glob("*.json")):
            age = self.age_days(path.stem)
            if age is not None:
                out.append((path.stem, age))
        return out


__all__ = ["RunwayCache", "MAX_AGE_DAYS"]
