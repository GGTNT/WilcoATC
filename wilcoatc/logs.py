"""Logging: what happened, why the controller said it, and how long it took.

A flight simulator's ATC is hard to debug from a screenshot. The interesting
failures are not crashes -- those leave a traceback -- but transmissions that
were understood as something else, readbacks that were rejected for a reason
the pilot could not see, and pauses that came from a slow model rather than
from the controller thinking. None of that is visible in a flat text log of
"INFO wilcoatc.engine: something happened".

So there are four streams, written side by side, each answering a different
question.

*The application log* (``wilcoatc.log``) is the ordinary one: levels, module
names, tracebacks. It rotates, so a long session cannot fill a disk, and every
line carries the session id of the run that wrote it, so one flight can be
pulled out of a file that holds a fortnight of them.

*The structured log* (``wilcoatc.jsonl``) is the same records as JSON, one per
line, with whatever fields the call site attached. It exists because grep is a
poor tool for "every transmission where the parser's confidence was under
0.5", and jq is a good one.

*The radio log* (``radio.jsonl``) is the transcript: every transmission in
both directions, with what the parser made of it -- the intent, the confidence,
the numbers it pulled out, and the readback verdict. This is the one that
answers "why did it say that", because it records the decision rather than its
consequence.

*The crew log* (``crew.jsonl``) is the same idea for the half of the aeroplane
that is not on the radio. It answers "why did it say nothing", which is the
harder question: a cue that is switched off, a line the flight has overtaken,
a synthesiser that returned nothing and a device that will not open are four
completely different faults that all sound like a first officer who has
stopped talking. Each of them writes a row here saying which it was.

Two more things are kept in memory rather than on disk. A ring buffer of the
last few hundred records, so the panel can show the log without reading a file
the browser cannot reach; and a table of timings, so the cost of recognition,
synthesis and the situation loop can be read off rather than guessed at.

Nothing here logs a secret. The one credential this program holds is the LLM
API key, and :func:`scrub` removes it and anything shaped like it before a
record is written.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .paths import log_dir

#: Identifies one run of the program. Stamped on every record so a log file
#: holding several sessions can be split back into them.
SESSION_ID = uuid.uuid4().hex[:12]

#: How many records the panel can look back over without touching the disk.
RING_SIZE = 500

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# keeping credentials out
# --------------------------------------------------------------------------

# An API key, wherever it appears: as a value in a mapping, as a bearer token,
# or as one of the provider prefixes on its own. Written to match the shape
# rather than a particular provider, because the next provider will be a
# different prefix and the same mistake.
_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|authorization|bearer|token|secret|password)\b"
    r"\s*[:=]?\s*['\"]?([A-Za-z0-9._\-]{8,})"
)
_KEY_SHAPED = re.compile(r"\b(sk-[A-Za-z0-9._\-]{8,}|xoxb-[A-Za-z0-9._\-]{8,})\b")


def scrub(text: str) -> str:
    """Replace anything that looks like a credential with a marker."""
    if not text:
        return text
    text = _SECRET.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    return _KEY_SHAPED.sub("<redacted>", text)


# --------------------------------------------------------------------------
# the extra fields every record carries
# --------------------------------------------------------------------------


class SessionFilter(logging.Filter):
    """Stamp the session id, and the thread's name, onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.session = SESSION_ID
        if isinstance(record.msg, str):
            record.msg = scrub(record.msg)
        return True


# Fields ``logging`` puts on every record itself. Anything else on a record is
# something a call site attached with ``extra=``, and is worth writing out.
_STANDARD = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    "session",
}


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in _STANDARD}


class JsonlFormatter(logging.Formatter):
    """One JSON object per line, with whatever the call site attached."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "at": round(record.created, 3),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S",
                                  time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "session": getattr(record, "session", SESSION_ID),
            "thread": record.threadName,
            "message": scrub(record.getMessage()),
        }
        extras = _extras(record)
        if extras:
            payload["fields"] = {k: _plain(v) for k, v in extras.items()}
        if record.exc_info:
            payload["traceback"] = scrub(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def _plain(value: Any) -> Any:
    """Something json can write, without losing what it was."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return scrub(value) if isinstance(value, str) else value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    return scrub(str(value))


# --------------------------------------------------------------------------
# the last few hundred records, for the panel
# --------------------------------------------------------------------------


class RingHandler(logging.Handler):
    """Keeps the most recent records in memory.

    The panel is served over HTTP from the same process, so it can read this
    directly. Reading the file instead would mean the browser needed access to
    the disk, which it does not have and should not be given.
    """

    def __init__(self, size: int = RING_SIZE):
        super().__init__()
        self.records: deque[dict[str, Any]] = deque(maxlen=size)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "at": round(record.created, 3),
                "level": record.levelname,
                "logger": record.name,
                "message": scrub(record.getMessage()),
            }
            extras = _extras(record)
            if extras:
                entry["fields"] = {k: _plain(v) for k, v in extras.items()}
            if record.exc_info:
                entry["traceback"] = scrub(
                    logging.Formatter().formatException(record.exc_info))
            with self._lock:
                self.records.append(entry)
        except Exception:       # a logging handler must never raise
            pass

    def tail(self, limit: int = 200, level: str = "") -> list[dict[str, Any]]:
        """The most recent records, newest last, optionally filtered."""
        wanted = logging.getLevelName((level or "").upper())
        floor = wanted if isinstance(wanted, int) else 0
        with self._lock:
            found = list(self.records)
        if floor:
            found = [r for r in found
                     if logging.getLevelName(r["level"]) >= floor]
        return found[-max(1, limit):]


#: The live ring, set up by :func:`configure`.
RING = RingHandler()


# --------------------------------------------------------------------------
# the radio transcript
# --------------------------------------------------------------------------


@dataclass
class RadioLog:
    """Every transmission, and what the machine made of it.

    Written as JSON lines because the interesting queries are about fields --
    which intents were misread, how confidence tracked with the language, which
    readbacks were rejected -- and none of them are answerable from prose.
    """

    path: Path | None = None
    enabled: bool = True
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def write(self, kind: str, **fields: Any) -> None:
        """Record one line of the transcript.

        ``kind`` is who was talking and in what capacity: ``pilot``, ``atc``,
        ``traffic``, ``atis``, or ``parse`` for the machine's reading of a
        transmission it has just heard.
        """
        if not self.enabled or self.path is None:
            return
        entry = {
            "at": round(time.time(), 3),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "session": SESSION_ID,
            "kind": kind,
            **{k: _plain(v) for k, v in fields.items()},
        }
        line = json.dumps(entry, ensure_ascii=False, default=str)
        try:
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError:
            # A transcript that cannot be written is not worth stopping a
            # flight for. It is noted once, by the handler that can still work.
            log.debug("could not write the radio log", exc_info=True)


#: The live transcript, set up by :func:`configure`.
RADIO = RadioLog(enabled=False)


def decision(what: str, **fields: Any) -> None:
    """Why the controller did what it did unprompted, as a row of the
    transcript (kind ``decision``): the simulator's approach data and whether
    it was believed, each descent and clearance with where its altitude came
    from, a go-around seen, the weather a broadcast or clearance was read off.
    ``logs arrival`` prints them; a test flight is read back from these."""
    RADIO.write("decision", what=what, **fields)


class CrewLog(RadioLog):
    """Why the cabin said what it said, and why it did not say the rest.

    The same JSON-lines writer as the transcript, pointed at a different file,
    and it exists because "I cannot hear the first officer" was for a long
    time an unanswerable bug report. Everything between the aeroplane and the
    speaker was either silent or logged at debug: a cue that was switched off,
    a line the flight had overtaken, a synthesiser that returned nothing, a
    device that would not open. All four look identical from the flight deck,
    and none of them left a mark.

    So every step writes a row. A cue is recorded when it fires *and* when it
    is refused, with which switch refused it; a line is recorded when it is
    queued, when it is rendered, when it is played -- with how many samples
    and how loud they were -- and when it is dropped. What that turns the
    report into is one line of a file: the last thing that happened before the
    silence, by name.

    Kinds written here:

    ``cue``        a cue fired, or was refused, with ``why``
    ``line``       one line queued, rendered, played, dropped or held
    ``music``      the boarding bed started or stopped
    ``player``     what the cabin player did with what it was handed
    ``silence``    a pass where nothing could be said, and what was in the way
    """


#: Why the cabin did what it did, set up by :func:`configure`.
CREW = CrewLog(enabled=False)


# --------------------------------------------------------------------------
# how long things take
# --------------------------------------------------------------------------


@dataclass
class Timing:
    """What one named operation has cost so far."""

    count: int = 0
    total_ms: float = 0.0
    worst_ms: float = 0.0
    last_ms: float = 0.0

    @property
    def mean_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0

    def record(self, ms: float) -> None:
        self.count += 1
        self.total_ms += ms
        self.last_ms = ms
        self.worst_ms = max(self.worst_ms, ms)


class Metrics:
    """Timings for the things that can make the radio feel slow.

    Recognition, synthesis and the situation loop are the three, and which of
    them is costing the pause before a reply is not something you can tell by
    listening. Sampled here rather than measured with a profiler, because the
    question is what it does on the pilot's machine during a real flight.
    """

    def __init__(self) -> None:
        self._timings: dict[str, Timing] = {}
        self._lock = threading.Lock()

    def record(self, name: str, ms: float) -> None:
        with self._lock:
            self._timings.setdefault(name, Timing()).record(ms)

    def snapshot(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {
                name: {
                    "count": t.count,
                    "mean_ms": round(t.mean_ms, 1),
                    "last_ms": round(t.last_ms, 1),
                    "worst_ms": round(t.worst_ms, 1),
                }
                for name, t in sorted(self._timings.items())
            }

    def reset(self) -> None:
        with self._lock:
            self._timings.clear()


METRICS = Metrics()


@contextmanager
def timed(name: str, warn_over_ms: float = 0.0) -> Iterator[None]:
    """Time a block, and say so if it took longer than it should have."""
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed = (time.perf_counter() - started) * 1000.0
        METRICS.record(name, elapsed)
        if warn_over_ms and elapsed > warn_over_ms:
            log.warning("%s took %.0f ms", name, elapsed,
                        extra={"operation": name, "ms": round(elapsed, 1)})


# --------------------------------------------------------------------------
# setting it all up
# --------------------------------------------------------------------------

_configured = False
_directory: Path | None = None


def directory() -> Path:
    """Where the logs are actually being written.

    Not always :func:`~wilcoatc.paths.log_dir`: the config can put them
    somewhere else, and everything that reports a path has to report the one
    in use rather than the default.
    """
    return _directory or log_dir()


def configure(config=None, *, force: bool = False) -> Path:
    """Set logging up for this run, and return the folder it writes to.

    Safe to call more than once: the second call is a no-op unless ``force``
    is set, which is what stops three entry points each adding their own set
    of handlers to the root logger.
    """
    global _configured, _directory
    if _configured and not force:
        return directory()

    settings = getattr(config, "logging", None)
    level = _level(getattr(settings, "level", None)
                   or getattr(config, "log_level", "INFO"))
    console_level = _level(getattr(settings, "console_level", "WARNING"))
    folder = log_dir()
    if getattr(settings, "directory", ""):
        folder = Path(settings.directory).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
    _directory = folder

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    root.setLevel(min(level, console_level))
    root.addFilter(SessionFilter())

    max_bytes = int(getattr(settings, "max_bytes", 4_000_000) or 4_000_000)
    backups = int(getattr(settings, "backups", 5) or 5)

    plain = logging.handlers.RotatingFileHandler(
        folder / "wilcoatc.log", maxBytes=max_bytes,
        backupCount=backups, encoding="utf-8",
    )
    plain.setLevel(level)
    plain.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(session)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    plain.addFilter(SessionFilter())
    root.addHandler(plain)

    if getattr(settings, "structured", True):
        structured = logging.handlers.RotatingFileHandler(
            folder / "wilcoatc.jsonl", maxBytes=max_bytes,
            backupCount=backups, encoding="utf-8",
        )
        structured.setLevel(level)
        structured.setFormatter(JsonlFormatter())
        structured.addFilter(SessionFilter())
        root.addHandler(structured)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    console.addFilter(SessionFilter())
    root.addHandler(console)

    RING.setLevel(level)
    RING.addFilter(SessionFilter())
    root.addHandler(RING)

    RADIO.path = folder / "radio.jsonl"
    RADIO.enabled = bool(getattr(settings, "radio", True))

    CREW.path = folder / "crew.jsonl"
    CREW.enabled = bool(getattr(settings, "crew", True))

    _install_hooks()
    _prune(folder, int(getattr(settings, "keep_days", 14) or 0))

    _configured = True
    log.info("logging started", extra={
        "session": SESSION_ID, "directory": str(folder),
        "level": logging.getLevelName(level),
    })
    return folder


def _level(value) -> int:
    if isinstance(value, int):
        return value
    found = logging.getLevelName(str(value or "INFO").upper())
    return found if isinstance(found, int) else logging.INFO


def _install_hooks() -> None:
    """Send an unhandled exception to the log before it reaches the console.

    A crash in a background thread otherwise prints to a console the pilot
    closed, and is gone. This is the one place that can still write it down.
    """
    previous = sys.excepthook

    def hook(kind, value, tb) -> None:
        if not issubclass(kind, KeyboardInterrupt):
            logging.getLogger("wilcoatc").critical(
                "unhandled exception", exc_info=(kind, value, tb))
        previous(kind, value, tb)

    sys.excepthook = hook

    def thread_hook(args) -> None:
        if not issubclass(args.exc_type, KeyboardInterrupt):
            logging.getLogger("wilcoatc").critical(
                "unhandled exception in %s", args.thread and args.thread.name,
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = thread_hook


def _prune(directory: Path, keep_days: int) -> None:
    """Delete logs older than the retention window."""
    if keep_days <= 0:
        return
    cutoff = time.time() - keep_days * 86400
    for path in directory.glob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


# --------------------------------------------------------------------------
# what the panel asks for
# --------------------------------------------------------------------------


def diagnostics() -> dict[str, Any]:
    """Everything worth putting in a bug report, in one object.

    Deliberately not the config: that holds an API key, and a pilot pasting
    this into an issue should not be pasting their credentials with it.
    """
    folder = directory()
    files = []
    for path in sorted(folder.glob("*")):
        try:
            if path.is_file():
                files.append({"name": path.name, "bytes": path.stat().st_size})
        except OSError:
            continue
    # What the machine offers as a push-to-talk button, which is the thing
    # most often reported as broken and the thing hardest to describe over
    # text. Both halves matter: what is connected, and what is merely known.
    controllers: dict[str, Any] = {}
    try:
        from .audio.io import JoystickPushToTalk

        found, why = JoystickPushToTalk.scan()
        controllers = {
            "connected": [{"index": i, "name": n, "buttons": b}
                          for i, n, b in found],
            "reason": why,
            "remembered": JoystickPushToTalk.remembered(),
        }
    except Exception as exc:
        controllers = {"error": str(exc)}

    return {
        "session": SESSION_ID,
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "pid": os.getpid(),
        "directory": str(folder),
        "files": files,
        "metrics": METRICS.snapshot(),
        "controllers": controllers,
    }


__all__ = [
    "SESSION_ID", "RING", "RADIO", "CREW", "METRICS", "configure", "timed",
    "scrub",
    "diagnostics", "directory", "RadioLog", "CrewLog", "Metrics", "RingHandler",
    "JsonlFormatter",
]
