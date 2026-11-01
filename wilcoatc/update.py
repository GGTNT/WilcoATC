"""Whether a newer WilcoATC is out, and putting it in place.

The program ships as a folder unpacked from a zip, so until now a new version
meant a pilot noticing one on the releases page, downloading it and unpacking
it over the old folder by hand -- or beside it, which loses the voices and the
config, or into it with Windows' extractor, which marks every file as coming
from the internet. Nobody did it twice.

So at startup the version this program is (``wilcoatc.__version__``) is
compared with the latest release on GitHub, once, on a worker thread, and the
panel offers the newer one. Nothing is fetched beyond that one small JSON
answer until the pilot says yes: the zip is a quarter of a gigabyte. The check
is ``ui.check_updates`` and a version the pilot turned down is
``ui.skipped_version``, never asked about again.

Putting it in place cannot be done by the program itself: Windows will not
let a running executable, or the DLLs it has loaded, be overwritten. So the
zip is unpacked into a staging folder under the data root, a small batch file
is written beside it and started detached, and the program closes. The batch
file waits for this process to be gone, copies the new program over the old
one and starts it again. What it copies is decided here, not there:

* ``_internal`` is mirrored, because a file a new version no longer ships
  must not linger and be imported in place of the right one;
* the executables and their notes at the top are replaced;
* ``data/navdata`` is copied over file by file, *except* the files a pilot
  edits by hand (``PILOT_FILES``), and nothing in it is deleted -- the
  VatGlasses folder a pilot fetched is not part of a release and has to
  survive one;
* everything else -- config.yaml, the voices, the recogniser, the logs -- is
  the pilot's and is not touched at all.

That is only possible in a frozen build whose folder can be written to. In a
checkout the new version is a ``git pull``, and under Program Files it needs
an administrator; both are offered the release page instead (``can_install``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from . import __version__
from .paths import FROZEN, data_root

log = logging.getLogger(__name__)

REPO = "GGTNT/WilcoATC"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
USER_AGENT = f"WilcoATC/{__version__} (https://github.com/{REPO})"

# The check is one small request at startup; it must never hold anything up.
CHECK_TIMEOUT_S = 8.0
DOWNLOAD_TIMEOUT_S = 60.0
CHUNK = 1 << 20

#: In data/navdata, what a pilot writes by hand: an update never replaces it.
PILOT_FILES = ("overrides.csv", "sectors.csv", "service_hours.csv")

EXECUTABLE = "WilcoATC.exe"


# --------------------------------------------------------------------------
# versions
# --------------------------------------------------------------------------


_VERSION = re.compile(r"^\s*v?(\d+(?:\.\d+)*)(?:[-.+]?([0-9A-Za-z.\-]+))?\s*$",
                      re.IGNORECASE)


def parse_version(text: str) -> tuple | None:
    """A tag or a version as something that sorts: ``v1.2.0-beta`` and so on.

    The number is compared part by part, missing parts being zero, so 1.2 is
    1.2.0. A suffix (alpha, beta, rc2) comes *before* the same number without
    one, as it does everywhere else: 1.0.0-alpha is older than 1.0.0. A tag
    with no number in front -- the first release here was tagged "Alpha" --
    is not a version and is never offered.
    """
    match = _VERSION.match(text or "")
    if not match:
        return None
    numbers = [int(part) for part in match.group(1).split(".")]
    while len(numbers) < 3:
        numbers.append(0)
    suffix = (match.group(2) or "").lower()
    # A suffix sorts below none at all; between suffixes, by their own text
    # and the number in them, so rc2 is after rc1 and beta after alpha.
    if suffix:
        digits = re.findall(r"\d+", suffix)
        stage = (0, re.sub(r"[\d.\-]+", "", suffix),
                 int(digits[-1]) if digits else 0)
    else:
        stage = (1, "", 0)
    return (tuple(numbers), stage)


def is_newer(latest: str, current: str) -> bool:
    """Whether ``latest`` is a later version than ``current``."""
    a, b = parse_version(latest), parse_version(current)
    if a is None or b is None:
        return False
    # Trailing zeros do not make a version: 1.0 and 1.0.0 are the same.
    def norm(v):
        numbers = list(v[0])
        while len(numbers) > 3 and numbers[-1] == 0:
            numbers.pop()
        return (tuple(numbers), v[1])
    return norm(a) > norm(b)


# --------------------------------------------------------------------------
# what is out there
# --------------------------------------------------------------------------


@dataclass
class Release:
    version: str
    tag: str
    name: str = ""
    notes: str = ""
    page: str = RELEASES_PAGE
    published: str = ""
    asset: str = ""
    asset_url: str = ""
    asset_size: int = 0
    # "sha256:<hex>" where GitHub publishes one for the asset.
    digest: str = ""


def is_hq_build() -> bool:
    """Whether this is WilcoATC-HQ, which has a zip of its own."""
    from .install import hq_possible

    return hq_possible()


def pick_asset(assets: list[dict], hq: bool) -> dict | None:
    """The zip this build is replaced by.

    The HQ build is replaced only by an HQ zip and the ordinary one only by
    an ordinary zip: the ordinary build has no torch, and an HQ zip over it
    would be two gigabytes of something it was not asked for.
    """
    zips = [a for a in assets
            if str(a.get("name", "")).lower().endswith(".zip")]
    for asset in zips:
        if ("hq" in str(asset.get("name", "")).lower()) == hq:
            return asset
    return None


def release_from(payload: dict, hq: bool) -> Release | None:
    """The answer from GitHub's API, as the one thing that is wanted from it."""
    tag = str(payload.get("tag_name") or "")
    if payload.get("draft") or payload.get("prerelease"):
        return None
    if parse_version(tag) is None:
        return None
    release = Release(
        version=tag.lstrip("vV"),
        tag=tag,
        name=str(payload.get("name") or tag),
        notes=str(payload.get("body") or "")[:8000],
        page=str(payload.get("html_url") or RELEASES_PAGE),
        published=str(payload.get("published_at") or ""),
    )
    asset = pick_asset(list(payload.get("assets") or []), hq)
    if asset:
        release.asset = str(asset.get("name") or "")
        release.asset_url = str(asset.get("browser_download_url") or "")
        release.asset_size = int(asset.get("size") or 0)
        release.digest = str(asset.get("digest") or "")
    return release


def fetch_latest(opener=urllib.request.urlopen,
                 timeout_s: float = CHECK_TIMEOUT_S) -> Release | None:
    request = urllib.request.Request(LATEST_API, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    with opener(request, timeout=timeout_s) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return release_from(payload, is_hq_build())


# --------------------------------------------------------------------------
# where it goes
# --------------------------------------------------------------------------


def install_dir() -> Path:
    return Path(sys.executable).resolve().parent


def updates_dir() -> Path:
    return data_root() / "updates"


def _writable(directory: Path) -> bool:
    try:
        probe = directory / ".update-test"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def why_not_install() -> str:
    """Empty when this installation can replace itself; else the reason.

    The reason is a catalogue key: the panel says it in the reader's words
    and offers the release page instead.
    """
    if not FROZEN or sys.platform != "win32":
        return "update.why_checkout"
    if not (install_dir() / EXECUTABLE).exists():
        return "update.why_checkout"
    if not _writable(install_dir()):
        return "update.why_readonly"
    return ""


def find_program(root: Path) -> Path | None:
    """The folder in an unpacked zip that holds the program.

    A release zip has the program one level down (``WilcoATC/``), beside a
    note at the top; looked for rather than assumed, so a zip made another
    way still works.
    """
    candidates = sorted(root.rglob(EXECUTABLE), key=lambda p: len(p.parts))
    for exe in candidates:
        if (exe.parent / "_internal").is_dir():
            return exe.parent
    return None


# --------------------------------------------------------------------------
# the batch file
# --------------------------------------------------------------------------


def apply_environment(staged: Path, target: Path, log_path: Path) -> dict:
    """The three paths the batch file works on, handed over as variables.

    Not written into the file: cmd.exe reads a batch file in the OEM code
    page (850 on a French Windows) whatever it was saved in, so a pilot whose
    folder is ``C:\\Users\\René`` had ``RenÚ`` copied to. The environment is
    UTF-16 all the way to robocopy, which is a Unicode program.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("_MEI", "_PYI"))}
    env.update({"WILCO_SRC": str(staged), "WILCO_DST": str(target),
                "WILCO_LOG": str(log_path),
                "PYINSTALLER_RESET_ENVIRONMENT": "1"})
    return env


def apply_script(pid: int) -> str:
    """The batch file that swaps the folders once this process has gone.

    robocopy rather than copy or xcopy: it retries a file still held for a
    moment by an antivirus scan, mirrors ``_internal`` in one step, and its
    exit codes below 8 all mean success. Plain ASCII: the paths arrive in
    ``WILCO_SRC``/``WILCO_DST``/``WILCO_LOG`` (:func:`apply_environment`).
    """
    keep = " ".join(f'"{name}"' for name in PILOT_FILES)
    lines = [
        "@echo off",
        "setlocal",
        'set "SRC=%WILCO_SRC%"',
        'set "DST=%WILCO_DST%"',
        'set "LOG=%WILCO_LOG%"',
        f'echo WilcoATC update, waiting for process {pid} > "%LOG%"',
        "set /a tries=0",
        ":wait",
        f'tasklist /FI "PID eq {pid}" 2>NUL | find "{pid}" >NUL',
        "if not errorlevel 1 (",
        "  set /a tries+=1",
        "  if %tries% GEQ 120 goto copy",
        "  timeout /t 1 /nobreak >NUL",
        "  goto wait",
        ")",
        ":copy",
        # A moment more: the process is gone before every handle on its DLLs is.
        "timeout /t 2 /nobreak >NUL",
        'robocopy "%SRC%\\_internal" "%DST%\\_internal" /MIR /R:20 /W:1 /NFL /NDL /NP >> "%LOG%"',
        "if errorlevel 8 goto failed",
        'robocopy "%SRC%" "%DST%" /R:20 /W:1 /NFL /NDL /NP /XF config.yaml config.local.yaml >> "%LOG%"',
        "if errorlevel 8 goto failed",
        'if exist "%SRC%\\data\\navdata" (',
        f'  robocopy "%SRC%\\data\\navdata" "%DST%\\data\\navdata" /E /R:20 /W:1 /NFL /NDL /NP /XF {keep} >> "%LOG%"',
        "  if errorlevel 8 goto failed",
        ")",
        'echo done >> "%LOG%"',
        f'start "" "%DST%\\{EXECUTABLE}"',
        "exit /b 0",
        ":failed",
        'echo FAILED >> "%LOG%"',
        # The old program is still whole wherever robocopy stopped short of
        # it; starting it is better than leaving the pilot with nothing.
        f'start "" "%DST%\\{EXECUTABLE}"',
        "exit /b 1",
    ]
    return "\r\n".join(lines) + "\r\n"


# --------------------------------------------------------------------------
# the one object the panel talks to
# --------------------------------------------------------------------------


@dataclass
class UpdateState:
    current: str = __version__
    checked: bool = False
    checking: bool = False
    error: str = ""
    available: bool = False
    release: dict | None = None
    can_install: bool = False
    why_not: str = ""
    # idle -> downloading -> ready -> applying; or failed.
    phase: str = "idle"
    progress: float = 0.0
    received: int = 0
    total: int = 0
    skipped: str = ""


class Updater:
    """Checks once, downloads on request, hands over to the batch file."""

    def __init__(self, on_change: Callable[[dict], None] | None = None,
                 skipped_version: str = "",
                 fetch: Callable[[], Release | None] = fetch_latest,
                 opener=urllib.request.urlopen):
        self._on_change = on_change
        self._fetch = fetch
        self._opener = opener
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._release: Release | None = None
        self._staged: Path | None = None
        self.state = UpdateState(skipped=skipped_version)

    # -- what the panel reads -------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            return asdict(self.state)

    def _changed(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change(self.snapshot())
            except Exception:                        # pragma: no cover
                log.debug("update listener raised", exc_info=True)

    def _set(self, **values) -> None:
        with self._lock:
            for key, value in values.items():
                setattr(self.state, key, value)
        self._changed()

    # -- checking -------------------------------------------------------------

    def check(self) -> dict:
        """Ask GitHub, on this thread. Failing to is said, never raised."""
        self._set(checking=True, error="")
        try:
            release = self._fetch()
        except Exception as exc:
            log.info("update check failed: %s", exc)
            self._set(checking=False, checked=True, error=str(exc))
            return self.snapshot()
        why = why_not_install()
        newer = bool(release and is_newer(release.version, __version__))
        if release and not release.asset_url and not why:
            why = "update.why_no_asset"
        self._release = release
        self._set(checking=False, checked=True, available=newer,
                  release=asdict(release) if release else None,
                  can_install=newer and not why, why_not=why)
        if newer:
            log.info("WilcoATC %s is out (this is %s)", release.version,
                     __version__)
        return self.snapshot()

    def check_in_background(self) -> None:
        threading.Thread(target=self.check, name="wilcoatc-update-check",
                         daemon=True).start()

    def skip(self, version: str) -> None:
        self._set(skipped=version)

    # -- downloading ----------------------------------------------------------

    def download(self) -> bool:
        """Start fetching and unpacking the new version; False if it cannot."""
        with self._lock:
            busy = self.state.phase in ("downloading", "applying")
            ready = self.state.can_install and self._release is not None
        if busy or not ready:
            return False
        self._cancel.clear()
        self._set(phase="downloading", progress=0.0, received=0,
                  total=self._release.asset_size, error="")
        threading.Thread(target=self._download, name="wilcoatc-update",
                         daemon=True).start()
        return True

    def cancel(self) -> None:
        self._cancel.set()

    def _download(self) -> None:
        release = self._release
        folder = updates_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            archive = folder / (release.asset or "WilcoATC.zip")
            part = archive.with_suffix(archive.suffix + ".part")
            self._fetch_to(release, part)
            if self._cancel.is_set():
                part.unlink(missing_ok=True)
                self._set(phase="idle", progress=0.0)
                return
            self._verify(release, part)
            part.replace(archive)

            staged = folder / "staged"
            shutil.rmtree(staged, ignore_errors=True)
            self._set(progress=-1.0)                # unpacking: a sweep, not a bar
            with zipfile.ZipFile(archive) as zf:
                _safe_extract(zf, staged)
            program = find_program(staged)
            if program is None:
                raise RuntimeError("the release zip has no WilcoATC.exe in it")
            archive.unlink(missing_ok=True)
            self._staged = program
            self._set(phase="ready", progress=1.0)
        except Exception as exc:
            log.warning("update download failed: %s", exc, exc_info=True)
            self._set(phase="failed", error=str(exc))

    def _fetch_to(self, release: Release, path: Path) -> None:
        request = urllib.request.Request(release.asset_url, headers={
            "User-Agent": USER_AGENT})
        received = 0
        last = 0.0
        with self._opener(request, timeout=DOWNLOAD_TIMEOUT_S) as response, \
                open(path, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0) \
                or release.asset_size
            while not self._cancel.is_set():
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                received += len(chunk)
                now = time.monotonic()
                if now - last > 0.25:
                    last = now
                    self._set(received=received, total=total,
                              progress=received / total if total else -1.0)
        self._set(received=received)

    @staticmethod
    def _verify(release: Release, path: Path) -> None:
        size = path.stat().st_size
        if release.asset_size and size != release.asset_size:
            raise RuntimeError(f"the download is {size} bytes, "
                               f"expected {release.asset_size}")
        if release.digest.lower().startswith("sha256:"):
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(CHUNK), b""):
                    digest.update(chunk)
            if digest.hexdigest() != release.digest.split(":", 1)[1].lower():
                raise RuntimeError("the download does not match its checksum")

    # -- handing over ---------------------------------------------------------

    def apply(self, quit_program: Callable[[], None]) -> bool:
        """Write and start the batch file, then close the program."""
        with self._lock:
            staged = self._staged
            if self.state.phase != "ready" or staged is None:
                return False
            self.state.phase = "applying"
        self._changed()
        script = updates_dir() / "apply-update.cmd"
        script.write_text(apply_script(os.getpid()), encoding="ascii")
        env = apply_environment(staged, install_dir(),
                                updates_dir() / "apply-update.log")
        flags = 0
        if sys.platform == "win32":
            flags = (subprocess.CREATE_NEW_PROCESS_GROUP
                     | subprocess.DETACHED_PROCESS
                     | subprocess.CREATE_NO_WINDOW)
        subprocess.Popen(["cmd.exe", "/c", str(script)], creationflags=flags,
                         close_fds=True, cwd=str(updates_dir()), env=env)
        log.info("handing over to %s and closing", script)
        quit_program()
        return True


def _safe_extract(zf: zipfile.ZipFile, target: Path) -> None:
    """Unpack, refusing any name that would land outside ``target``."""
    root = target.resolve()
    for member in zf.infolist():
        name = member.filename.replace("\\", "/")
        dest = (root / name).resolve()
        if root != dest and root not in dest.parents:
            raise RuntimeError(f"unsafe path in the release zip: {name}")
        if name.endswith("/"):
            dest.mkdir(parents=True, exist_ok=True)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out, CHUNK)


def cleanup() -> None:
    """What a finished update left behind, removed at the next start."""
    folder = data_root() / "updates"
    if not folder.exists():
        return
    for name in ("staged", "apply-update.cmd"):
        path = folder / name
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
        except OSError:
            log.debug("could not remove %s", path, exc_info=True)
    for part in folder.glob("*.part"):
        part.unlink(missing_ok=True)


def quit_process(delay_s: float = 0.6) -> None:
    """Close the window if there is one, and the process whatever happens.

    The answer to the panel's request has to get back first, hence the delay;
    the window's own closing stops the engine. If something holds the process
    open regardless, it is ended, because the batch file is waiting on it.
    """
    def later() -> None:
        time.sleep(delay_s)
        try:
            import webview

            for window in list(getattr(webview, "windows", [])):
                window.destroy()
        except Exception:
            log.debug("no window to close", exc_info=True)
        time.sleep(8.0)
        os._exit(0)

    threading.Thread(target=later, name="wilcoatc-update-quit",
                     daemon=True).start()
