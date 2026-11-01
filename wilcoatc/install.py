"""Fetching the parts the program cannot ship with, with something to watch.

Three things have to arrive before WilcoATC can work, and together they are
about 1.5 GB: the navigation database, the controller voices, and the speech
recogniser's model. None of them can be bundled -- the nav data is refreshed
nightly and the two models are 300 MB each -- so the first run has to download
them.

That used to be ``wilcoatc setup`` in a terminal, which is fine for the pilot
who installed this from a command prompt and no use at all to the one who
double-clicked an exe. This is the same work, run on a worker thread, reporting
what it is doing as it does it, so the window can show a list with a bar
against each line.

The design constraint that shapes everything here: **it must be safe to ask
for again.** A download that failed halfway, a pilot who closed the window, a
second click on the button -- all of those end up back in this module, and none
of them may start a second copy of the same download or destroy what already
arrived. So there is one runner, it refuses to start while it is running, and
every step checks whether its work is already done before doing it.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .config import Config
from .paths import model_dir, navdata_dir, resolve, voice_dir

log = logging.getLogger(__name__)

# Roughly what each step costs, for a progress bar that does not jump. These
# are the sizes on a clean machine; a step that is already done reports at once.
#
# "voices" is the first run: Supertonic's 400 MB and the British and French
# Piper voices. "Every country's accent" adds the rest of the Piper catalogue
# (voices.accents_download_gb), and the bar is no more exact for it.
SIZES_MB: dict[str, int] = {"navdata": 70, "voices": 930, "speech": 480}

# The optional downloads, for the setup screen's button. The accents are the
# catalogue's own sum; the sectors and the cloning model (standard, the
# default) were measured on disk.
def _accents_mb() -> int:
    try:
        from .audio.voices import accents_download_gb

        return round(accents_download_gb() * 1000)
    except Exception:                                # pragma: no cover
        return 2300


EXTRAS_MB: dict[str, int] = {"all_voices": _accents_mb(), "vatglasses": 25,
                             "hq": 3000}


@dataclass
class Step:
    """One piece of the install, and how far through it is."""

    key: str                      # navdata | voices | speech
    name: str
    state: str = "pending"        # pending | running | done | failed | skipped
    # 0.0 to 1.0, or -1.0 for work whose length is not knowable in advance.
    progress: float = 0.0
    detail: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _State:
    running: bool = False
    finished_at: float = 0.0
    cancelled: bool = False
    steps: list[Step] = field(default_factory=list)
    # The cloned voices were asked for, which is the panel's cue to switch
    # the controllers over to them once the voices step is done.
    hq: bool = False


class Installer:
    """Runs the setup steps on a worker thread.

    One per process. ``on_change`` is called -- from the worker thread -- every
    time anything moves, which is how the panel's websocket hears about it.
    It is called often, so it should be cheap and must not raise.
    """

    # No more than this many updates a second per step. A 1.2 GB download
    # fires the progress callback thousands of times a second and every one
    # of them would otherwise be a websocket frame.
    THROTTLE_S = 0.15

    def __init__(self, config: Config | None = None,
                 on_change: Callable[[dict], None] | None = None):
        self.config = config or Config()
        self.on_change = on_change or (lambda payload: None)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = _State()
        self._cancel = threading.Event()
        self._last_sent = 0.0

    # ------------------------------------------------------------------
    # what is here already
    # ------------------------------------------------------------------

    def installed(self) -> dict[str, bool]:
        """Which pieces are already on disk.

        Cheap enough to call on every page load, because it only looks for
        files. Whether they are any *good* is the doctor's question, not this
        one.
        """
        return {
            "navdata": _navdata_present(),
            "voices": _voices_present(self.config),
            "speech": _speech_present(self.config),
        }

    def missing(self) -> list[str]:
        return [key for key, here in self.installed().items() if not here]

    def state(self) -> dict[str, Any]:
        """Everything the panel needs to draw the setup screen."""
        with self._lock:
            steps = [step.as_dict() for step in self._state.steps]
            running = self._state.running
            cancelled = self._state.cancelled
            finished = self._state.finished_at
            hq = self._state.hq
        installed = self.installed()
        from .audio.voices import core_voice_packages

        return {
            "running": running,
            "cancelled": cancelled,
            "finished_at": finished,
            "installed": installed,
            "missing": [k for k, here in installed.items() if not here],
            "steps": steps,
            "sizes_mb": SIZES_MB,
            "voice_packages": core_voice_packages(
                resolve(self.config.voice.voice_dir), self.config.voice.engine),
            "hq": hq,
            # Whether this build can clone at all -- the HQ build carries
            # torch and Chatterbox, the ordinary one does not -- and whether
            # the controllers already are clones. The panel offers the
            # cloned voices only when the first is true.
            "hq_available": hq_possible(),
            "hq_engine": self.config.voice.engine == "chatterbox",
            # The optional downloads, and which of them are already here.
            # They have no step of their own -- each rides on the step it
            # belongs to -- so without this the screen could only ever say
            # "optional": ticking one that was already installed downloaded
            # nothing, finished in a second, and looked like a button that
            # did nothing.
            "extras": extras_present(self.config),
            "extras_mb": {**EXTRAS_MB, "hq": hq_mb(self.config)},
        }

    # ------------------------------------------------------------------
    # running
    # ------------------------------------------------------------------

    def start(self, keys: list[str] | None = None, force: bool = False,
              all_voices: bool = False, hq: bool = False,
              vatglasses: bool = False) -> bool:
        """Begin, unless something is already going. Returns whether it began.

        Refusing rather than queueing is deliberate. Two copies of a 1.2 GB
        download writing to the same folder is the one failure here that could
        leave a half-file behind and be hard to explain afterwards.
        """
        wanted = [k for k in (keys or list(SIZES_MB)) if k in SIZES_MB]
        if not wanted:
            return False
        with self._lock:
            if self._state.running:
                return False
            self._cancel.clear()
            self._state = _State(running=True, hq=bool(hq), steps=[
                Step(key, _NAMES[key]) for key in wanted
            ])
            thread = threading.Thread(
                target=self._run,
                args=(wanted, force, all_voices, hq, vatglasses),
                name="wilcoatc-install", daemon=True,
            )
            self._thread = thread
        self._publish(force=True)
        thread.start()
        return True

    def cancel(self) -> None:
        """Ask it to stop after the file it is on.

        There is no way to interrupt a socket read mid-chunk without leaving a
        part file, so this stops between chunks. The pilot sees it stop within
        a second on a normal connection.
        """
        self._cancel.set()

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # ------------------------------------------------------------------

    def _run(self, keys: list[str], force: bool, all_voices: bool,
             hq: bool = False, vatglasses: bool = False) -> None:
        runners = {"navdata": self._navdata, "voices": self._voices,
                   "speech": self._speech}
        try:
            for key in keys:
                if self._cancel.is_set():
                    self._set(key, state="skipped", detail="cancelled")
                    continue
                step = self._find(key)
                if step is None:
                    continue
                # Asking for the sectors is asking for the nav data again:
                # they are part of it once they are there.
                if not force and _REDOING_COSTS.get(key, False) \
                        and not (key == "navdata" and vatglasses) \
                        and self.installed().get(key):
                    # Already here, and doing it again is not free. The nav
                    # database is rebuilt from scratch every time, which is
                    # half a minute of a pilot watching a bar for a file they
                    # already have -- and, when the engine is running, half a
                    # minute that ends in failure, because rebuilding starts
                    # by deleting a file the engine has open. The voices are
                    # not in this set: there the download skips file by file,
                    # and running it again is how an install gets the
                    # accents added.
                    self._set(key, state="skipped", progress=1.0,
                              detail="already installed")
                    continue
                self._set(key, state="running", progress=0.0)
                try:
                    runners[key](force=force, all_voices=all_voices, hq=hq,
                                 vatglasses=vatglasses)
                except _Cancelled:
                    self._set(key, state="skipped", detail="cancelled")
                except Exception as exc:
                    log.exception("setup step %s failed", key)
                    self._set(key, state="failed", error=str(exc)[:200],
                              detail="")
                else:
                    self._set(key, state="done", progress=1.0)
        finally:
            with self._lock:
                self._state.running = False
                self._state.cancelled = self._cancel.is_set()
                self._state.finished_at = time.time()
            self._publish(force=True)

    # --- the steps ----------------------------------------------------

    def _navdata(self, force: bool = False, vatglasses: bool = False,
                 **_: Any) -> None:
        from .navdata.build import build
        from .navdata.fetch import fetch_all

        # The download is most of the wait and the build is the rest, so the
        # bar is split between them rather than sitting at 100% through a
        # thirty-second build that looks like a hang.
        fetch_all(force=force, on_progress=self._progress("navdata", 0.0, 0.75),
                  vatglasses=vatglasses)
        self._set("navdata", progress=0.78, detail="building the database")
        self._raise_if_cancelled()
        build()
        # What was built, rather than the last file that went past.
        try:
            from .navdata.build import DB_PATH, pending_path
            from .navdata.db import NavDB

            waiting = pending_path(DB_PATH)
            built = waiting if waiting.exists() else DB_PATH
            # Closed straight after, or this count would be the next
            # thing holding a file open against the swap.
            counted = NavDB(built)
            try:
                airports = counted._conn.execute(
                    "SELECT COUNT(*) FROM airports").fetchone()[0]
            finally:
                counted.close()
            summary = f"{airports:,} airports"
            if waiting.exists():
                # The running engine has the old one open; see put_in_place.
                summary += ", used from the next start"
        except Exception:                            # pragma: no cover
            summary = ""
        self._set("navdata", progress=1.0, detail=summary)

    def _voices(self, force: bool = False, all_voices: bool = False,
                hq: bool = False, **_: Any) -> None:
        from .audio.voices import CATALOGUE, download_voices

        from .audio.voices import VoiceRegistry, core_voice_packages

        root = resolve(self.config.voice.voice_dir)
        # The cloning model is two gigabytes on top and only asked for by
        # name, so when it is asked for it shares the bar with the ordinary
        # voices rather than having a step of its own the panel would have to
        # know about.
        which = "all" if all_voices else "core"
        if self.config.voice.engine == "piper" and not all_voices and not hq:
            which = ",".join(spec.key for spec in CATALOGUE if spec.core)
        download_voices(
            which, root=root, force=force,
            on_progress=self._progress("voices", 0.0, 0.5 if hq else 1.0),
        )
        if hq:
            download_voices(
                "chatterbox", root=root, force=force,
                on_progress=self._progress("voices", 0.5, 1.0),
                hq_model=self.config.voice.hq_model,
            )
            shipped_prompts(root)
        # The last filename to go past is not a result. What the pilot wants
        # to read when the bar fills is how many controllers they now have.
        registry = VoiceRegistry(root, engine=self.config.voice.engine)
        if any(not package["installed"] for package in core_voice_packages(
                root, self.config.voice.engine)):
            raise RuntimeError("The voice download is incomplete. Please try again.")
        available = registry.available()
        accents = len({spec.accent for spec in available
                       if spec.engine == "piper" and spec.accent not in ("gb", "us")})
        self._set("voices", detail=f"{len(available)} voices, "
                                   f"{accents} accents")

    def _speech(self, **_: Any) -> None:
        """Pull the recogniser's model by loading it once.

        There is no download call to make: faster-whisper fetches the model the
        first time it is asked for one, and gives no usable progress while it
        does. So the bar for this step is indeterminate and the detail line
        says what is happening, which is better than a bar that does not move.
        """
        from .audio.stt import Recognizer, engine_model

        wanted = engine_model(self.config)
        self._set("speech", progress=-1.0,
                  detail=f"fetching and loading {wanted}")
        self._raise_if_cancelled()
        recognizer = Recognizer(
            model_size=wanted,
            languages=list(self.config.speech.languages) or ["en"],
            device=self.config.speech.device,
            compute_type=self.config.speech.compute_type,
        )
        recognizer.warm()
        self._set("speech", progress=1.0, detail=wanted)

    # --- reporting ----------------------------------------------------

    def _progress(self, key: str, low: float, high: float):
        """A callback for a downloader, mapped onto part of one step's bar."""

        def report(label: str, done: int, total: int) -> None:
            self._raise_if_cancelled()
            fraction = (done / total) if total else 0.0
            self._set(key, progress=low + (high - low) * min(fraction, 1.0),
                      detail=f"{label}  {done / 1e6:.0f} MB", throttle=True)

        return report

    def _find(self, key: str) -> Step | None:
        with self._lock:
            for step in self._state.steps:
                if step.key == key:
                    return step
        return None

    def _set(self, key: str, throttle: bool = False, **fields: Any) -> None:
        with self._lock:
            for step in self._state.steps:
                if step.key != key:
                    continue
                for name, value in fields.items():
                    setattr(step, name, value)
                break
        self._publish(force=not throttle)

    def _publish(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_sent < self.THROTTLE_S:
            return
        self._last_sent = now
        try:
            self.on_change(self.state())
        except Exception:                            # pragma: no cover
            log.debug("setup listener raised", exc_info=True)

    def _raise_if_cancelled(self) -> None:
        if self._cancel.is_set():
            raise _Cancelled()


class _Cancelled(Exception):
    """The pilot asked it to stop."""


# Steps where running again over a finished install is expensive rather than
# merely redundant. See the skip in ``_run``.
_REDOING_COSTS: dict[str, bool] = {"navdata": True, "speech": True}


_NAMES: dict[str, str] = {
    "navdata": "Navigation data",
    "voices": "Controller voices",
    "speech": "Speech recognition",
}


# --------------------------------------------------------------------------
# what counts as present
# --------------------------------------------------------------------------


def _navdata_present() -> bool:
    from .navdata.db import DB_PATH

    try:
        return DB_PATH.exists() and DB_PATH.stat().st_size > 1_000_000
    except OSError:                                  # pragma: no cover
        return False


def hq_possible() -> bool:
    """Whether this program can clone a voice: torch and Chatterbox are here.

    True in a checkout with the optional requirements and in the HQ build;
    false in the ordinary build, which leaves both out on purpose. Asked by
    looking, not by importing, because importing torch takes seconds.
    """
    from importlib.util import find_spec

    try:
        return find_spec("chatterbox") is not None             and find_spec("torch") is not None
    except (ImportError, ValueError):                # pragma: no cover
        return False


def shipped_prompts(root) -> list:
    """Put the clips the program ships beside the pilot's own, once.

    The HQ build carries a set of reference clips from an open-licence
    corpus (``wilcoatc/audio/clones``, with its attribution), because the
    model with no clips is a model with nobody to be. They are copied into
    the voice folder rather than read where they are so that the casting
    has one folder to look in, and a clip already there -- a pilot's own
    with the same name, or one they edited -- is never overwritten.
    """
    import shutil
    from pathlib import Path

    from .audio.voices import CHATTERBOX_DIR, SHIPPED_CLONES

    target = Path(root) / CHATTERBOX_DIR / "prompts"
    copied = []
    if not SHIPPED_CLONES.is_dir():
        return copied
    target.mkdir(parents=True, exist_ok=True)
    for clip in sorted(SHIPPED_CLONES.glob("*.wav")):
        if not (target / clip.name).exists():
            shutil.copy2(clip, target / clip.name)
            copied.append(target / clip.name)
    return copied


def _voices_present(config: Config) -> bool:
    from .audio.voices import core_voice_packages

    try:
        return all(package["installed"] for package in core_voice_packages(
            resolve(config.voice.voice_dir), config.voice.engine))
    except Exception:                                # pragma: no cover
        return False


def hq_mb(config: Config) -> int:
    """The cloning download for the profile chosen: the 3,000 MB measured on
    disk for the standard model, moved by the difference between the models'
    own sizes, so "Normal" (turbo) is not announced at the standard's size."""
    try:
        from .audio.backends.chatterbox import VARIANTS, variant_of

        chosen = VARIANTS[variant_of(config.voice.hq_model)]["size_mb"]
        return EXTRAS_MB["hq"] + chosen - VARIANTS["standard"]["size_mb"]
    except Exception:                                # pragma: no cover
        return EXTRAS_MB["hq"]


def extras_present(config: Config) -> dict[str, bool]:
    """Whether each optional download is already on disk."""
    return {"all_voices": _accents_present(config),
            "vatglasses": _vatglasses_present(),
            "hq": _chatterbox_present(config)}


def _accents_present(config: Config) -> bool:
    """Every voice of the "every country's accent" download, both files."""
    from .audio.voices import CATALOGUE

    root = resolve(config.voice.voice_dir)
    try:
        return all((root / (spec.filename + suffix)).is_file()
                   for spec in CATALOGUE for suffix in ("", ".json"))
    except OSError:                                  # pragma: no cover
        return False


def _vatglasses_present() -> bool:
    from .navdata.fetch import DATA_DIR, VATGLASSES_DOWNLOADS

    return all((DATA_DIR / name).is_file() for name in VATGLASSES_DOWNLOADS)


def _chatterbox_present(config: Config) -> bool:
    try:
        from .audio.backends.chatterbox import VARIANTS, model_dir, variant_of

        variant = variant_of(config.voice.hq_model)
        base = model_dir(resolve(config.voice.voice_dir), variant)
        return (base / VARIANTS[variant]["sentinel"]).exists()
    except Exception:                                # pragma: no cover
        return False


def _speech_present(config: Config) -> bool:
    from .audio.stt import engine_model

    folder = model_dir()
    if not folder.exists():
        return False
    # The engine's own model, and only that one: "any model.bin" said a
    # small.en on disk was the small the engine loads, and the setup screen
    # showed "done" while the radio said the recogniser was missing.
    wanted = engine_model(config)
    return bool(list(folder.glob(f"**/*{wanted}/**/model.bin")))


__all__ = ["Installer", "Step", "SIZES_MB"]
