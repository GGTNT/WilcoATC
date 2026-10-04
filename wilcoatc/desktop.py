"""The desktop application.

One window, four screens, no browser. The window is a native one -- Edge
WebView2 on Windows, WebKit elsewhere -- drawn by pywebview, and the interface
inside it is the same panel the tablet gets. Sharing it is deliberate: a second
implementation of the same four screens would be a second set of bugs, and the
one thing a pilot must be able to rely on is that what the panel says is what
the engine thinks.

The window talks to the engine over a loopback port that is chosen by the
operating system and never announced. Nothing is reachable from the network
unless the pilot asks for that explicitly, which is what ``wilcoatc gui
--host`` is for.

Everything runs in this one process: the engine, its situation loop, the local
API and the window. Closing the window stops all of it.
"""

from __future__ import annotations

import inspect
import logging
import socket
import sys
import threading
import time
from pathlib import Path

from .config import Config
from .paths import command_hint
from .engine import Engine
from .navdata.db import NavDB
from .web.server import (Broadcaster, create_app, create_setup_app,
                         event_sink)

log = logging.getLogger(__name__)

# The artboard size. Big enough for the three-column comms screen without
# anything folding, and small enough for a 1366-wide laptop to show it.
WINDOW_W = 1440
WINDOW_H = 900
MIN_W = 900
MIN_H = 620

BACKGROUND = "#0e0e10"


class LocalApi:
    """The engine's HTTP surface, on a loopback port nobody else can reach.

    A socket is bound here rather than handing uvicorn a port number, because
    asking the operating system for a free port and then reconnecting to it by
    number is a race: something else can take it in between. Binding once and
    passing the socket cannot lose that race.
    """

    def __init__(self, engine: Engine | None, broadcaster: Broadcaster,
                 toolbar_port: int = 0, config: Config | None = None):
        # ``engine`` is None on a machine where nothing has been downloaded
        # yet: there is no navigation database to build one on. The window
        # still opens, and serves the setup panel instead of the radio.
        self.engine = engine
        self.config = config if engine is None else engine.config
        self.broadcaster = broadcaster
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(16)
        self.port = self.socket.getsockname()[1]
        # And a second, fixed one for the panel in the simulator's toolbar,
        # which cannot be told a number that changes every launch. Failing to
        # get it is not an error: something else has it, and the panel says it
        # cannot reach the application rather than the application refusing to
        # start.
        self.toolbar_socket: socket.socket | None = None
        self.toolbar_port = 0
        if toolbar_port:
            try:
                fixed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                fixed.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                fixed.bind(("127.0.0.1", int(toolbar_port)))
                fixed.listen(16)
            except OSError as exc:
                log.info("the toolbar port %s is not available (%s)",
                         toolbar_port, exc)
            else:
                self.toolbar_socket = fixed
                self.toolbar_port = int(toolbar_port)
        self._server = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        import uvicorn

        app = (create_app(self.engine, self.broadcaster)
               if self.engine is not None
               else create_setup_app(self.config, self.broadcaster))
        config = uvicorn.Config(app, log_level="warning", access_log=False)
        self._server = uvicorn.Server(config)
        listening = [s for s in (self.socket, self.toolbar_socket) if s]
        self._thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": listening},
            name="WilcoATC-api", daemon=True,
        )
        self._thread.start()

    def wait_until_ready(self, timeout_s: float = 15.0) -> bool:
        """Hold the window back until the first request would succeed."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._server is not None and getattr(self._server, "started", False):
                return True
            time.sleep(0.05)
        return False

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=3.0)


def available() -> tuple[bool, str]:
    """Whether a window can be opened here, and why not when it cannot."""
    try:
        import webview  # noqa: F401
    except ImportError:
        return False, (
            "The desktop window needs pywebview.\n"
            "  pip install pywebview\n"
            f"Or run the panel in a browser instead: {command_hint('gui')}"
        )
    return True, ""


def run(
    config: Config,
    connect_sim: bool = True,
    on_ready=None,
) -> int:
    """Open the window and run until it is closed.

    ``on_ready`` is called with the engine once everything is up, which is how
    the command line prints its banner without this module knowing what a
    console is.
    """
    ok, why = available()
    if not ok:
        raise RuntimeError(why)

    import webview

    broadcaster = Broadcaster()
    try:
        engine = Engine(config, NavDB(), event_sink(broadcaster))
    except FileNotFoundError:
        # Nothing downloaded yet. The window opens on the setup panel rather
        # than not opening at all, because somebody who installed this by
        # double-clicking it has no terminal to be told to run setup in.
        log.info("no navigation database; opening the setup window")
        engine = None
    api = LocalApi(engine, broadcaster,
                   toolbar_port=config.toolbar.port
                   if config.toolbar.enabled and engine is not None else 0,
                   config=config)
    api.start()

    if not api.wait_until_ready():
        api.stop()
        raise RuntimeError("the local interface did not start")

    if engine is not None:
        engine.warm()
        engine.start(connect_sim=connect_sim)
    if on_ready is not None:
        on_ready(engine, api)

    window = webview.create_window(
        "WilcoATC",
        api.url,
        width=WINDOW_W,
        height=WINDOW_H,
        min_size=(MIN_W, MIN_H),
        background_color=BACKGROUND,
        text_select=True,
        confirm_close=False,
    )

    # The engine owns a microphone, an audio device and two threads. Closing
    # the window has to put all of them down, or the process lingers with the
    # microphone still open.
    def shutdown() -> None:
        if engine is not None:
            try:
                engine.stop()
            except Exception:
                log.debug("engine shutdown raised", exc_info=True)
        api.stop()

    window.events.closing += lambda: shutdown()

    try:
        webview.start(**_icon(webview))
    except Exception as exc:                       # pragma: no cover - GUI path
        shutdown()
        raise RuntimeError(_explain(exc)) from exc
    finally:
        shutdown()
    return 0


# The application's own mark, which is also what the page loads.
STATIC = Path(__file__).resolve().parent / "web" / "static"

# The mark in the two forms it is needed in. The page gets the square one, in
# a rounded frame the stylesheet draws. The window and the taskbar get the
# round one, because the operating system puts it on its own background at
# sizes down to sixteen pixels.
#
# Windows hands the path straight to System.Drawing.Icon, which reads the .ico
# container and refuses a PNG outright -- and it refuses it on the window's own
# thread, where nothing here can catch it. So the form is chosen before the
# window is asked for, not recovered from afterwards.
#
# Both are built by scripts/make_icons.py from the artwork at the project root.
LOGO = STATIC / "logo.png"
LOGO_ICO = STATIC / "logo.ico"


def _icon(webview=None) -> dict:
    """The icon argument, if there is one this platform can read.

    Everything is decided here rather than recovered from later, because the
    window reads the file on its own thread: a form it cannot use does not come
    back as an exception this code could catch, it takes the process down.
    """
    wanted = LOGO_ICO if sys.platform == "win32" else LOGO
    if not wanted.is_file():
        return {}
    if webview is not None:
        # A toolkit old enough not to know about icons still gets a window.
        try:
            if "icon" not in inspect.signature(webview.start).parameters:
                return {}
        except (TypeError, ValueError):            # pragma: no cover
            return {}
    return {"icon": str(wanted)}


def _explain(exc: Exception) -> str:
    """Turn a toolkit failure into something a pilot can act on."""
    message = str(exc)
    if "Python.Runtime" in message or "Loader.Initialize" in message:
        # Almost always the internet mark on an Explorer-extracted zip: .NET
        # will not load a managed assembly that carries it, and the failure
        # surfaces as an unresolved entry point that says nothing about zips.
        # winzone strips the marks at startup, so reaching this means it could
        # not -- a read-only folder, or an antivirus holding the file.
        return (
            "Windows is blocking the libraries this program was unpacked "
            "from.\n"
            "A zip downloaded from the internet marks every file inside it, "
            "and .NET refuses to load a marked library.\n"
            "In PowerShell, from this folder:\n"
            "  Get-ChildItem -Recurse | Unblock-File\n"
            "then start it again. Extracting with 7-Zip avoids it entirely.\n"
            f"Or run the panel in a browser instead: {command_hint('gui')}"
        )
    if "WebView2" in message or "Microsoft.Web.WebView2" in message:
        return (
            "The window needs the Microsoft Edge WebView2 runtime, which is "
            "normally already on Windows 10 and 11.\n"
            "Install it from https://developer.microsoft.com/microsoft-edge/webview2/\n"
            f"Or run the panel in a browser instead: {command_hint('gui')}"
        )
    return (
        f"Could not open the window: {message}\n"
        f"Run the panel in a browser instead: {command_hint('gui')}"
    )


__all__ = ["run", "available", "LocalApi", "WINDOW_W", "WINDOW_H"]
