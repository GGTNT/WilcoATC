"""Microphone capture, push-to-talk, and radio playback.

Push-to-talk is the right model for this rather than open-mic voice activation:
a pilot already holds a PTT switch, keying it is unambiguous, and it stops the
system trying to answer the sound of the engine or a passing conversation.

Playback runs through a queue so a controller finishing one transmission can
start the next without gaps, and so that a transmission arriving while another
is playing waits its turn rather than talking over it -- a real frequency
carries one voice at a time, and simultaneous transmissions block each other.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np

from ..logs import CREW

log = logging.getLogger(__name__)

CAPTURE_RATE = 16000        # what the recogniser wants
BLOCK_MS = 30


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------


@dataclass
class CaptureResult:
    audio: np.ndarray
    sample_rate: int
    duration_s: float
    peak: float
    truncated: bool = False

    @property
    def too_quiet(self) -> bool:
        """Whether the transmission is likely to be silence or a stray key."""
        return self.peak < 0.012 or self.duration_s < 0.35


class MicrophoneCapture:
    """Records from the default input device while the PTT is held."""

    def __init__(
        self,
        device: int | str | None = None,
        sample_rate: int = CAPTURE_RATE,
        max_seconds: float = 30.0,
        channels: int = 1,
    ):
        self.device = device
        self.sample_rate = sample_rate
        self.max_seconds = max_seconds
        self.channels = channels
        self._frames: list[np.ndarray] = []
        self._stream = None
        self._recording = False
        self._opened_at = 0.0
        self._lock = threading.Lock()

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def recording_for(self) -> float:
        """How long the microphone has been open, in seconds. Zero if shut.

        Read by the watchdog in the engine. A transmit button that is somehow
        still down after a minute is not a pilot talking, and the cost of
        believing it is the whole of the crew and every reminder the
        controller would have volunteered.
        """
        return (time.monotonic() - self._opened_at) if self._recording else 0.0

    def start(self) -> None:
        """Open the microphone. Raises if the device will not open.

        The flag is set last, and cleared again if anything below it throws.
        It used to be set first, which cost a great deal more than it looks
        like: an input stream that fails to open -- a Bluetooth headset
        switching itself to hands-free mode, a device that has gone away, a
        rate the driver will not take -- left the program believing the
        microphone was live for the rest of the session.

        Nothing recovers from that on its own. The transmit button stops
        working, because keying up sees a transmission already in progress;
        and every unprompted thing the program says goes silent, because
        "somebody is transmitting" is exactly what the controller's reminders
        and the whole of the cabin crew wait for. One failed device open, and
        the aeroplane loses its first officer for the flight.
        """
        import sounddevice as sd

        with self._lock:
            if self._recording:
                return
            self._frames = []

            def callback(indata, _frames, _time_info, status):
                if status:
                    log.debug("input stream status: %s", status)
                self._frames.append(indata.copy().reshape(-1))

            try:
                self._stream = sd.InputStream(
                    device=self.device,
                    samplerate=self.sample_rate,
                    channels=self.channels,
                    dtype="float32",
                    blocksize=int(self.sample_rate * BLOCK_MS / 1000),
                    callback=callback,
                )
                self._stream.start()
            except BaseException:
                self._stream = None
                self._recording = False
                raise
            self._recording = True
            self._opened_at = time.monotonic()

    def abandon(self) -> None:
        """Forget that a transmission was ever in progress.

        The last resort behind the engine's transmit watchdog, for when even
        stopping has failed. What is being protected is not the recording --
        that is already lost -- but the flag: everything unprompted this
        program says waits for the microphone to be shut, so a flag that
        survives a broken device takes the crew and the controller's own
        reminders with it for the rest of the flight.
        """
        with self._lock:
            self._recording = False
            self._frames = []
            stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.close()
            except Exception:
                log.debug("could not close the input stream", exc_info=True)

    def stop(self) -> CaptureResult:
        with self._lock:
            if not self._recording:
                return CaptureResult(np.zeros(0, dtype=np.float32), self.sample_rate, 0.0, 0.0)
            self._recording = False
            stream, self._stream = self._stream, None

        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:
                log.warning("error closing input stream: %s", exc)

        if not self._frames:
            return CaptureResult(np.zeros(0, dtype=np.float32), self.sample_rate, 0.0, 0.0)

        audio = np.concatenate(self._frames).astype(np.float32)
        self._frames = []

        truncated = False
        limit = int(self.max_seconds * self.sample_rate)
        if audio.shape[0] > limit:
            audio = audio[:limit]
            truncated = True

        return CaptureResult(
            audio=audio,
            sample_rate=self.sample_rate,
            duration_s=audio.shape[0] / float(self.sample_rate),
            peak=float(np.max(np.abs(audio))) if audio.size else 0.0,
            truncated=truncated,
        )


# --------------------------------------------------------------------------
# push to talk
# --------------------------------------------------------------------------


class PushToTalk:
    """Global hotkey that keys the microphone while held.

    A joystick or yoke button can be used instead, which is what most people
    fly with; the keyboard path is the fallback that always works.
    """

    def __init__(
        self,
        key: str = "grave",
        on_press: Callable[[], None] | None = None,
        on_release: Callable[[], None] | None = None,
        scan_code: int = 0,
    ):
        self.key = key
        # Where the key is on the keyboard, when it is known. Matched before
        # the name: the name is whatever the layout prints, so "$" is a
        # different key on an AZERTY keyboard and a US one, and the scan
        # code is the same key on both.
        self.scan_code = int(scan_code or 0)
        self.on_press = on_press
        self.on_release = on_release
        self._listener = None
        self._down = False

    def _matches(self, key) -> bool:
        if self.scan_code:
            # pynput's Windows hook carries the scan code on each key.
            scan = getattr(key, "_scan", None)
            if scan is not None:
                return int(scan) == self.scan_code
        name = self.key.lower()
        special = {
            "grave": "`", "backquote": "`", "tilde": "`",
            "space": " ",
        }
        target = special.get(name, name)

        try:
            char = getattr(key, "char", None)
            if char and char.lower() == target:
                return True
        except Exception:
            pass
        try:
            named = getattr(key, "name", None) or str(key).split(".")[-1]
            if named and named.lower() == name:
                return True
        except Exception:
            pass
        # Fall back to the raw virtual-key code for keys pynput reports oddly.
        vk = getattr(key, "vk", None)
        if vk is not None and name in ("grave", "backquote", "tilde") and vk == 192:
            return True
        return False

    def start(self) -> None:
        from pynput import keyboard

        def pressed(key):
            if self._down or not self._matches(key):
                return
            self._down = True
            if self.on_press:
                self.on_press()

        def released(key):
            if not self._down or not self._matches(key):
                return
            self._down = False
            if self.on_release:
                self.on_release()

        self._listener = keyboard.Listener(on_press=pressed, on_release=released)
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None


class JoystickPushToTalk:
    """A joystick or yoke button as the PTT.

    A thin layer over :mod:`wilcoatc.audio.controllers`, which owns SDL on
    one thread and is where the history is: the controller is named by
    ``joystick_id`` (vendor:product) and ``joystick_name``, and the index is
    only for a configuration written before those existed.
    """

    #: Why no controller was found (see :mod:`wilcoatc.audio.controllers`).
    MISSING = "missing"
    NONE = "none"

    def __init__(
        self,
        joystick_index: int = 0,
        button: int = 0,
        poll_hz: float = 60.0,
        on_press: Callable[[], None] | None = None,
        on_release: Callable[[], None] | None = None,
        joystick_id: str = "",
        joystick_name: str = "",
        on_lost: Callable | None = None,
        on_back: Callable | None = None,
    ):
        self.joystick_index = joystick_index
        self.joystick_id = joystick_id
        self.joystick_name = joystick_name
        self.button = button
        self.poll_hz = poll_hz
        self.on_press = on_press
        self.on_release = on_release
        self.on_lost = on_lost
        self.on_back = on_back
        self._watch = None

    @property
    def controller(self):
        """The controller being read, once started."""
        return self._watch.controller if self._watch is not None else None

    @staticmethod
    def _hub():
        from .controllers import shared

        return shared()

    @classmethod
    def available(cls) -> bool:
        return cls.scan()[1] != cls.MISSING

    @classmethod
    def scan(cls, rescan: bool | str = "auto"):
        """``([Controller, ...], reason)``: the reason is empty when something
        was found, :data:`MISSING` when pygame-ce is not in this
        installation, :data:`NONE` when nothing is plugged in, and otherwise
        SDL's own error. ``"auto"`` restarts SDL's joystick subsystem only
        when no button is being learnt or read: a restart makes every held
        button read as pressed afresh (audio/controllers.py)."""
        return cls._hub().list(rescan=rescan)

    @classmethod
    def list_devices(cls):
        return cls.scan()[0]

    @classmethod
    def learn(cls, timeout_s: float = 8.0):
        """Wait for a button: ``(Controller, button)``, or None in time."""
        return cls._hub().learn(timeout_s)

    @staticmethod
    def remembered() -> list[str]:
        """Controllers this computer has seen before, connected or not.

        Windows keeps the name of every game controller that has ever been
        attached to it. Held up against what is connected now, that turns "it
        does not detect my yoke" into a question with an answer: either the
        yoke is unplugged, or it is plugged in and not presenting itself as a
        game controller. Empty everywhere else, which costs nothing -- the
        list is only ever shown beside the real one.
        """
        if sys.platform != "win32":
            return []
        try:
            import winreg
        except ImportError:
            return []

        path = "\\".join((
            "System", "CurrentControlSet", "Control", "MediaProperties",
            "PrivateProperties", "Joystick", "OEM",
        ))
        names: list[str] = []
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                index = 0
                while True:
                    try:
                        child = winreg.EnumKey(key, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(key, child) as entry:
                            name, _kind = winreg.QueryValueEx(entry, "OEMName")
                    except OSError:
                        continue
                    name = str(name).strip()
                    if name and name not in names:
                        names.append(name)
        except OSError:
            return []
        return names

    def start(self) -> None:
        """Raises :class:`~wilcoatc.audio.controllers.ControllerMissing` or
        :class:`~wilcoatc.audio.controllers.ControllersUnavailable`, which
        the engine says in the pilot's language."""
        from .controllers import ButtonWatch

        index = None if (self.joystick_id or self.joystick_name) \
            else self.joystick_index
        watch = ButtonWatch(
            self._hub(), controller_id=self.joystick_id,
            name=self.joystick_name, index=index, button=self.button,
            on_press=self.on_press, on_release=self.on_release,
            on_lost=self.on_lost, on_back=self.on_back, poll_hz=self.poll_hz)
        watch.start()
        self._watch = watch

    def stop(self) -> None:
        if self._watch is not None:
            self._watch.stop()
            self._watch = None


# --------------------------------------------------------------------------
# playback
# --------------------------------------------------------------------------


@dataclass
class PlaybackItem:
    audio: np.ndarray
    sample_rate: int
    label: str = ""
    on_start: Callable[[], None] | None = None
    on_done: Callable[[], None] | None = None


class RadioPlayer:
    """Serialises transmissions onto one frequency.

    A real VHF channel carries a single voice: if two stations transmit at once
    they block each other and nobody is readable. Queueing rather than mixing
    reproduces the important half of that -- you never hear two controllers
    speaking over one another.
    """

    def __init__(self, device: int | str | None = None, volume: float = 1.0):
        self.device = device
        self.volume = volume
        self._queue: queue.Queue[PlaybackItem | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._playing = threading.Event()
        self._current: str = ""

    @property
    def busy(self) -> bool:
        return self._playing.is_set() or not self._queue.empty()

    @property
    def now_playing(self) -> str:
        return self._current

    def start(self) -> None:
        # A runner that has exited leaves the attribute set, so the test is
        # whether it is alive rather than whether it exists. Otherwise a
        # player that had been stopped once accepted transmissions for the
        # rest of the session and played none of them.
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        import sounddevice as sd

        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            self._playing.set()
            self._current = item.label
            try:
                if item.on_start:
                    item.on_start()
                audio = np.asarray(item.audio, dtype=np.float32) * self.volume
                audio = np.clip(audio, -1.0, 1.0)
                sd.play(audio, item.sample_rate, device=self.device, blocking=True)
            except Exception as exc:
                log.error("playback failed: %s", exc)
            finally:
                if item.on_done:
                    try:
                        item.on_done()
                    except Exception:
                        log.debug("playback callback raised", exc_info=True)
                self._current = ""
                self._playing.clear()
                self._queue.task_done()

    def play(
        self,
        audio: np.ndarray,
        sample_rate: int,
        label: str = "",
        on_start: Callable[[], None] | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        self.start()
        self._queue.put(PlaybackItem(audio, sample_rate, label, on_start, on_done))

    def wait_until_idle(self, timeout: float = 60.0) -> bool:
        deadline = time.monotonic() + timeout
        while self.busy and time.monotonic() < deadline:
            time.sleep(0.05)
        return not self.busy

    def clear(self) -> None:
        """Drop anything queued but not yet started."""
        while True:
            try:
                item = self._queue.get_nowait()
                self._queue.task_done()
                if item is None:
                    break
            except queue.Empty:
                break

    def stop(self) -> None:
        self._stop.set()
        self.clear()
        # Only if there is a runner to read it: a sentinel left in an empty
        # queue is read by the *next* runner as its first item, which kills it
        # before it has played anything.
        thread, self._thread = self._thread, None
        if thread is not None:
            self._queue.put(None)
            thread.join(timeout=2.0)
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:
            pass


class CabinPlayer:
    """A second audio path, for everything that is not the radio.

    The first officer and the cabin are not on the frequency, so they must not
    queue behind it. A controller calling you in the middle of the safety
    demonstration has to arrive when they called, not forty seconds later --
    and in the aeroplane both of those really are separate paths: the radio
    comes out of the headset and the cabin address out of a speaker.

    Which is why this is a whole player rather than a second queue on the
    existing one. :class:`RadioPlayer` plays through ``sounddevice.play``,
    which owns one module-level stream; two of those fight over it and stop
    each other mid-word. This one opens a stream of its own and writes into
    it, so the two paths genuinely run at once.

    Within itself it is still a queue: the first officer does not talk over
    the purser.

    Running at once is what the separate path is *for*, and it is also the
    thing a pilot complains about, because two voices at the same time is one
    voice and a noise. Two rules keep both. Nothing in here starts while the
    frequency is live -- that is the crew's own rule, in
    :meth:`wilcoatc.atc.crew.CrewDirector._drain`, and it covers everything
    that has not begun yet. What is left is the announcement already half said
    when the pilot keys the microphone, and ``ducked`` is what this player
    does about that: the level drops out of the way for as long as the radio
    is talking, and comes back afterwards. The announcement is not cut and the
    transmission is not queued.

    Under all of it there can be a *bed*: the boarding music, looping in the
    gaps between announcements. It is not a queue item, because a queue item
    would have to end before anything else could be said and boarding music
    ends when the aeroplane pushes back rather than after ninety seconds. So
    it is played in the space where this thread would otherwise be waiting for
    something to do, and the moment something is queued the bed stops and the
    voice gets the device. That is also what a real cabin does: the music is
    not mixed under the purser, it stops for them.

    Why it says so when it fails
    ----------------------------

    Everything above is a second output stream on the same device as the
    radio, opened per announcement, which is a thing an audio stack can refuse
    for reasons that have nothing to do with this program -- a device grabbed
    exclusively by the simulator, a rate the driver will not take, a stream
    that opens and then never accepts a block. Every one of those used to
    arrive as *silence*: a crew that queued lines, rendered them, played them
    into nothing, and reported nothing, because the exception was either never
    raised or raised on a worker thread nobody was reading.

    So two things are guaranteed here rather than hoped for. A failure is
    handed to ``on_trouble`` and becomes something the pilot can see. And
    ``busy`` cannot be true forever: an item that overstays its own length by
    a wide margin is treated as gone, because a player wedged inside a write
    is otherwise indistinguishable from a busy one -- and a busy one silences
    the whole crew, which is precisely the failure being fixed.
    """

    # Written to the device in blocks this long. Small enough that stopping is
    # prompt, large enough not to underrun on a busy machine.
    BLOCK = 1024

    # How far under the voices the boarding music sits. Music in a cabin is
    # not something you listen to, it is something you notice the absence of,
    # and this is about where that is on a headset.
    BED_LEVEL = 0.16

    # How long an item may take before it is written off, as a multiple of its
    # own length plus a fixed allowance for opening the device. Generous on
    # purpose: this is a wedged-stream detector, not a scheduler, and a
    # machine that is swapping can genuinely take a second or two to start
    # playing a line.
    STALL_FACTOR = 3.0
    STALL_GRACE_S = 12.0

    # What the cabin drops to while the radio is live.
    #
    # Nothing here waits for the frequency -- a line is only ever started when
    # the frequency is clear, which is the crew's business rather than the
    # player's. What is left is the announcement already half said when the
    # pilot keys the microphone, and a controller answering into the middle of
    # it. Ducking is what a cabin address system does about that, and it beats
    # both of the alternatives: talking over the top, and cutting the purser
    # off mid-sentence to start the paragraph again afterwards.
    DUCKED = 0.22

    # How quickly it gets there and back, in seconds. Long enough not to
    # click, short enough that the first word of a clearance is already clear
    # of the announcement.
    DUCK_RAMP_S = 0.12

    def __init__(self, device: int | str | None = None, volume: float = 1.0,
                 on_trouble: Callable[[str], None] | None = None):
        self.device = device
        self.volume = volume
        # Where a failure to reach the device goes. The player is on its own
        # thread and nobody is reading its return value, so without this the
        # only symptom of a broken output is a cabin that never speaks.
        self.on_trouble = on_trouble
        self._queue: queue.Queue[PlaybackItem | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._playing = threading.Event()
        self._current: str = ""
        # Set while something is being written, so a cancel can cut into the
        # middle of an announcement rather than only between them.
        self._skip = threading.Event()
        # Whether the radio is live. Read block by block while an
        # announcement is playing, so it takes effect in the middle of one.
        self.ducked = False
        self._gain = 1.0
        # When the item now playing has outstayed its welcome, and therefore
        # when ``busy`` stops believing in it.
        self._stall_at = 0.0
        # Said once. A device that cannot be opened cannot be opened for every
        # line of a six-paragraph safety briefing either, and six identical
        # red lines is noise rather than information.
        self._reported = False
        # How much of the item now playing has actually reached the device.
        self._wrote = 0
        # The boarding music, and where in it the loop has got to.
        self._bed: np.ndarray | None = None
        self._bed_rate = 0
        self._bed_at = 0

    @property
    def busy(self) -> bool:
        """Whether a voice is playing or waiting to.

        The bed does not count. Boarding music is not somebody speaking, and
        a crew that waited for it would never say anything while it ran.

        A write that never returns is the one case this cannot answer
        honestly, and it is also the one that matters: nothing in Python can
        unblock a call that is stuck inside the audio library, so an
        announcement wedged in the device would report busy for the rest of
        the flight -- and busy is what keeps the crew quiet. Nobody is ever
        told about it, because nothing raised. So the overstay is noticed
        here, which is the only code still running while it happens, and it is
        reported once. The cabin is still lost; what changes is that the pilot
        is told which device lost it instead of watching a first officer
        quietly stop existing.
        """
        if not self._queue.empty():
            return True
        if not self._playing.is_set():
            return False
        if self._stall_at and time.monotonic() >= self._stall_at:
            self._trouble(f"{self._current or 'an announcement'} never "
                          f"finished playing")
            return False
        return True

    @property
    def now_playing(self) -> str:
        return self._current

    def start(self) -> None:
        # ``is_alive`` rather than ``is not None``: a runner that has exited
        # leaves the attribute set, and treating that as "already running"
        # left a player that accepted everything it was given and played none
        # of it -- queue filling, ``busy`` stuck true, crew silent, no error.
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="WilcoATC-cabin")
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                # Nothing to say. If there is a bed, this is where it plays:
                # in the space this thread would otherwise spend waiting.
                self._play_bed()
                continue
            if item is None:
                break
            self._playing.set()
            self._current = item.label
            self._skip.clear()
            seconds = len(item.audio) / float(item.sample_rate or 1)
            self._stall_at = (time.monotonic()
                              + seconds * self.STALL_FACTOR + self.STALL_GRACE_S)
            began = time.monotonic()
            self._wrote = 0
            try:
                if item.on_start:
                    item.on_start()
                self._write(item)
                self._reported = False
                # What reached the device, rather than what was asked for. A
                # stream that opens, accepts nothing and closes without
                # complaining is the quietest of all the failures here, and
                # this is the only place it leaves a mark.
                CREW.write("player", label=item.label, wrote=self._wrote,
                           expected=len(item.audio), rate=item.sample_rate,
                           took_s=round(time.monotonic() - began, 2),
                           device=str(self.device))
                if self._wrote < len(item.audio) and not self._skip.is_set():
                    log.warning("the cabin wrote %d of %d samples of %s",
                                self._wrote, len(item.audio), item.label)
            except Exception as exc:
                log.error("cabin playback failed: %s", exc)
                CREW.write("player", label=item.label, wrote=self._wrote,
                           expected=len(item.audio), error=str(exc),
                           device=str(self.device))
                self._trouble(str(exc))
            finally:
                if item.on_done:
                    try:
                        item.on_done()
                    except Exception:
                        log.debug("cabin callback raised", exc_info=True)
                self._current = ""
                self._stall_at = 0.0
                self._playing.clear()
                self._queue.task_done()

    def _trouble(self, detail: str) -> None:
        """Tell somebody the cabin cannot reach the speaker."""
        if self._reported or self.on_trouble is None:
            return
        self._reported = True
        try:
            self.on_trouble(detail)
        except Exception:
            log.debug("cabin trouble sink raised", exc_info=True)

    def _write(self, item: PlaybackItem) -> None:
        import sounddevice as sd

        audio = np.asarray(item.audio, dtype=np.float32) * self.volume
        audio = np.clip(audio, -1.0, 1.0)
        if audio.ndim == 1:
            audio = audio.reshape(-1, 1)
        rate = int(item.sample_rate or 1)
        # How much of the way to the target one block can move the gain. A
        # step would click, and the ramp is short enough that the first word
        # of a transmission is already in the clear.
        step = self.BLOCK / max(1.0, self.DUCK_RAMP_S * rate)
        with sd.OutputStream(samplerate=item.sample_rate,
                             channels=audio.shape[1],
                             device=self.device,
                             dtype="float32",
                             blocksize=self.BLOCK) as stream:
            for start in range(0, len(audio), self.BLOCK):
                if self._stop.is_set() or self._skip.is_set():
                    break
                block = audio[start:start + self.BLOCK]
                stream.write(self._shaped(block, step))
                self._wrote += len(block)

    # ------------------------------------------------------------------
    # the bed
    # ------------------------------------------------------------------

    def bed(self, audio: np.ndarray | None, sample_rate: int = 0) -> None:
        """Loop this underneath everything until told otherwise.

        ``None`` clears it. Setting a different bed while one is playing
        replaces it at the next block rather than at the end of the loop:
        pushback does not wait thirty seconds for the music to come round.
        """
        if audio is None or not len(audio):
            self._bed = None
            self._bed_rate = 0
            return
        block = np.asarray(audio, dtype=np.float32)
        if block.ndim == 1:
            block = block.reshape(-1, 1)
        self._bed_at = 0
        self._bed_rate = int(sample_rate or 1)
        self._bed = block
        self.start()

    def _play_bed(self) -> None:
        """Write the loop until something is queued, or it is turned off.

        And until the radio speaks. A voice in here only *ducks* for the
        frequency, because cutting a purser off mid-sentence is worse than
        having them quiet under a clearance -- but music has no sentence to be
        cut off in the middle of, and the device is worth more than the music
        is. Boarding lasts minutes, and holding a second stream open on the
        same device for all of them, on every machine, to play something
        nobody is listening to, is a bad trade against the one stream that
        actually matters. So it fades down and gives the device back.
        """
        import sounddevice as sd

        bed, rate = self._bed, self._bed_rate
        if bed is None or rate <= 0 or self.ducked:
            return
        step = self.BLOCK / max(1.0, self.DUCK_RAMP_S * rate)
        level = self.BED_LEVEL * self.volume
        try:
            with sd.OutputStream(samplerate=rate, channels=bed.shape[1],
                                 device=self.device, dtype="float32",
                                 blocksize=self.BLOCK) as stream:
                while (self._bed is bed and not self._stop.is_set()
                       and self._queue.empty()):
                    stream.write(self._shaped(self._next_bed_block(bed), step)
                                 * level)
                    # Ducked, and all the way down: the fade is finished, so
                    # this is where the stream closes rather than at the first
                    # block of it, which would be a click.
                    if self.ducked and self._gain <= self.DUCKED:
                        break
            self._reported = False
        except Exception as exc:
            # The music is the least important thing this program does, so a
            # device that will not take it loses the music rather than
            # retrying four times a second for the rest of the flight.
            log.error("boarding music failed: %s", exc)
            CREW.write("music", playing=False, error=str(exc),
                       device=str(self.device), rate=rate)
            self._bed = None
            self._trouble(str(exc))

    def _next_bed_block(self, bed: np.ndarray) -> np.ndarray:
        """One block of the loop, wrapping round the end without a seam."""
        end = self._bed_at + self.BLOCK
        if end <= len(bed):
            block = bed[self._bed_at:end]
            self._bed_at = end % len(bed)
        else:
            block = np.concatenate([bed[self._bed_at:], bed[:end - len(bed)]])
            self._bed_at = end - len(bed)
        return block

    def _shaped(self, block: np.ndarray, step: float) -> np.ndarray:
        """One block, on its way to whatever the gain should be by now."""
        target = self.DUCKED if self.ducked else 1.0
        was = self._gain
        if was == target:
            return block if target == 1.0 else block * target
        if target > was:
            self._gain = min(target, was + step)
        else:
            self._gain = max(target, was - step)
        ramp = np.linspace(was, self._gain, len(block), dtype=np.float32)
        return block * ramp.reshape(-1, 1)

    def play(
        self,
        audio: np.ndarray,
        sample_rate: int,
        label: str = "",
        on_start: Callable[[], None] | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        self.start()
        self._queue.put(PlaybackItem(audio, sample_rate, label, on_start, on_done))

    def clear(self) -> None:
        """Drop what is queued, and cut what is playing.

        The bed survives. Turning the crew off during boarding should leave
        the cabin sounding like a cabin, not switch the aeroplane off.
        """
        self._skip.set()
        while True:
            try:
                item = self._queue.get_nowait()
                self._queue.task_done()
                if item is None:
                    break
            except queue.Empty:
                break

    def stop(self) -> None:
        self._stop.set()
        self._skip.set()
        self.bed(None)
        self.clear()
        # The sentinel is only worth queueing if somebody is there to read it.
        # Left in an empty queue it outlives the shutdown, and the next runner
        # to start reads it as its first item and exits immediately -- a
        # player that then swallowed everything it was given in silence.
        thread, self._thread = self._thread, None
        if thread is not None:
            self._queue.put(None)
            thread.join(timeout=2.0)


# --------------------------------------------------------------------------
# device discovery
# --------------------------------------------------------------------------


def list_audio_devices() -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """``(inputs, outputs)`` as ``[(index, name), ...]``."""
    import sounddevice as sd

    inputs, outputs = [], []
    for index, info in enumerate(sd.query_devices()):
        name = info.get("name", "?")
        if info.get("max_input_channels", 0) > 0:
            inputs.append((index, name))
        if info.get("max_output_channels", 0) > 0:
            outputs.append((index, name))
    return inputs, outputs


class InputMeter:
    """How loud the microphone is, for the setup screen's level bar.

    The capture above only runs while the transmit key is held, and the first
    thing the setup screen asks is "is this the right microphone" -- before
    any key has been chosen. So this opens its own stream on the device
    being looked at, keeps only the loudest sample since it was last read,
    and closes itself once nobody has asked for a while: a stream left open
    is a microphone indicator lit in the taskbar for no reason.

    Opened per device and reopened on a change, because the screen offers
    the list and the pilot goes down it. A device that will not open is
    reported, not raised: the bar sits still and says so.
    """

    IDLE_S = 3.0

    def __init__(self, sample_rate: int = CAPTURE_RATE):
        self.sample_rate = sample_rate
        self._lock = threading.Lock()
        self._stream = None
        self._device: int | str | None = None
        self._peak = 0.0
        self._asked = 0.0
        self._error = ""
        self._watch: threading.Thread | None = None

    def read(self, device: int | str | None = None) -> dict:
        """The peak since the last read, 0 to 1, opening the device if needed."""
        with self._lock:
            self._asked = time.monotonic()
            if self._stream is None or device != self._device:
                self._open(device)
            peak, self._peak = self._peak, 0.0
            return {"ok": self._stream is not None, "peak": round(peak, 4),
                    "error": self._error}

    def close(self) -> None:
        with self._lock:
            self._shut()

    def _open(self, device) -> None:
        import sounddevice as sd

        self._shut()
        self._device = device
        self._error = ""

        def callback(indata, _frames, _time_info, _status):
            loudest = float(np.max(np.abs(indata))) if indata.size else 0.0
            if loudest > self._peak:
                self._peak = loudest

        try:
            self._stream = sd.InputStream(
                device=device, samplerate=self.sample_rate, channels=1,
                dtype="float32", callback=callback,
                blocksize=int(self.sample_rate * BLOCK_MS / 1000))
            self._stream.start()
        except Exception as exc:
            self._stream = None
            self._error = str(exc)
            log.debug("level meter could not open %r: %s", device, exc)
            return
        if self._watch is None or not self._watch.is_alive():
            self._watch = threading.Thread(target=self._idle, daemon=True,
                                           name="wilcoatc-level")
            self._watch.start()

    def _shut(self) -> None:
        stream, self._stream = self._stream, None
        self._peak = 0.0
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:                       # pragma: no cover
                log.debug("level meter did not close", exc_info=True)

    def _idle(self) -> None:
        while True:
            time.sleep(0.5)
            with self._lock:
                if self._stream is None:
                    return
                if time.monotonic() - self._asked > self.IDLE_S:
                    self._shut()
                    return
