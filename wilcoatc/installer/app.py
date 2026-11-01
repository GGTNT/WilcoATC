"""The installer's window: the "WilcoATC Installer" design, on pywebview.

The page is handed to the window as one string -- stylesheet, script, the
catalogue, fonts and logo inlined -- rather than served from the folder the
one-file program unpacks itself into. That folder is under %TEMP%, which is
under the pilot's profile, which is ``C:\\Users\\René`` for René; a page that
never has a URL made of it cannot be broken by one.

The page calls :class:`Api` through ``window.pywebview.api``; pywebview runs
each call on a thread of its own, so the long one (installing) starts a
worker and the page asks how it is going.
"""

from __future__ import annotations

import base64
import json
import logging
import sys
import threading
import webbrowser
from pathlib import Path

from .. import LICENCE, __version__
from . import core

log = logging.getLogger(__name__)

UI = Path(__file__).resolve().parent / "ui"
WIDTH, HEIGHT = 800, 590


#: The panel's languages, each in its own name (wilcoatc/web/static/i18n.js,
#: held equal by tests/test_installer.py). The installer offers those it has
#: a catalogue for, and the one picked is the program's language as well.
LANGUAGE_NAMES = {
    "en": "English", "ar": "العربية", "bg": "Български", "ca": "Català",
    "cs": "Čeština", "da": "Dansk", "de": "Deutsch", "el": "Ελληνικά",
    "es": "Español", "et": "Eesti", "fa": "فارسی", "fi": "Suomi",
    "fr": "Français", "gl": "Galego", "he": "עברית", "hi": "हिन्दी",
    "hr": "Hrvatski", "hu": "Magyar", "id": "Indonesia", "it": "Italiano",
    "ja": "日本語", "ko": "한국어", "lt": "Lietuvių", "lv": "Latviešu",
    "ms": "Melayu", "nb": "Norsk", "nl": "Nederlands", "pl": "Polski",
    "pt": "Português", "ro": "Română", "ru": "Русский", "sk": "Slovenčina",
    "sl": "Slovenščina", "sr": "Српски", "sv": "Svenska", "th": "ไทย",
    "tr": "Türkçe", "uk": "Українська", "vi": "Tiếng Việt",
    "zh-Hans": "简体中文", "zh-Hant": "繁體中文",
}


def available() -> list[str]:
    return [code for code in LANGUAGE_NAMES if (UI / "i18n" / f"{code}.json").is_file()]


def pick_language(locale_name: str) -> str:
    """Windows' locale (``fr_FR``, ``zh_TW``, ``no_NO``) as a catalogue code."""
    name = (locale_name or "en").replace("-", "_")
    lang, _, region = name.partition("_")
    lang = lang.lower()
    if lang == "zh":
        lang = "zh-Hant" if region.upper() in ("TW", "HK", "MO") else "zh-Hans"
    elif lang in ("no", "nn"):
        lang = "nb"
    return lang if lang in available() else "en"


def catalogue(lang: str) -> tuple[str, dict]:
    english = json.loads((UI / "i18n" / "en.json").read_text(encoding="utf-8"))
    path = UI / "i18n" / f"{lang}.json"
    if lang != "en" and path.is_file():
        english.update(json.loads(path.read_text(encoding="utf-8")))
        return lang, english
    return "en", english


def catalogues() -> dict:
    """Every language, so switching needs nothing from Python: ~10 kB each."""
    return {code: catalogue(code)[1] for code in available()}


def _data_uri(path: Path, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def page(boot: dict) -> str:
    """The whole page as one document, nothing fetched."""
    css = (UI / "installer.css").read_text(encoding="utf-8")
    for font in (UI / "fonts").glob("*.woff2"):
        css = css.replace(f"url(fonts/{font.name})",
                          f"url({_data_uri(font, 'font/woff2')})")
    html = (UI / "index.html").read_text(encoding="utf-8")
    script = (UI / "installer.js").read_text(encoding="utf-8")
    logo = _data_uri(UI / "logo-wilco.svg", "image/svg+xml")
    # Inside a <script>, "</" would end it early whatever JSON says it is.
    boot_json = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
    return (html.replace("/*STYLE*/", css)
                .replace("/*BOOT*/", f"window.BOOT = {boot_json};")
                .replace("/*SCRIPT*/", script)
                .replace("LOGO_URI", logo))


class Api:
    """What the page may ask for. Every answer is plain JSON."""

    def __init__(self, payload: core.Payload | None, lang: str):
        self._window = None
        self._payload = payload
        self._lang = lang
        self._lock = threading.Lock()
        self._job: dict = {"state": "idle"}
        self._target: Path | None = None

    # -- the folder -----------------------------------------------------------

    def check_path(self, text: str) -> dict:
        size = self._payload.size if self._payload else 0
        deepest = self._payload.deepest if self._payload else 0
        target = core.resolve_target(text, size, deepest)
        total = 0
        if target.path is not None:
            try:
                import shutil

                existing = core._nearest_existing(target.path)
                total = shutil.disk_usage(existing).total if existing else 0
            except OSError:
                total = 0
        return {"path": str(target.path) if target.path else "",
                "drive": target.path.drive if target.path else "",
                "problems": target.problems, "notes": target.notes,
                "free": target.free, "total": total, "needed": size,
                "later": core.FIRST_RUN_BYTES, "upgrade": target.upgrade}

    def browse(self, current: str) -> str:
        import webview

        start = core._nearest_existing(Path(current)) if current else None
        chosen = self._window.create_file_dialog(
            webview.FOLDER_DIALOG,
            directory=str(start) if start else "")
        if not chosen:
            return ""
        return str(chosen[0] if isinstance(chosen, (list, tuple)) else chosen)

    # -- the simulator --------------------------------------------------------

    def simulators(self) -> dict:
        """The simulators, and the Community folders the panel would go in."""
        from ..msfs_panel import community_folders

        found: dict = {"sims": [], "communities": []}
        try:
            found["sims"] = core.detect_simulators()
        except Exception:
            log.warning("simulator detection failed", exc_info=True)
        try:
            found["communities"] = [str(c) for c in community_folders()]
        except Exception:
            log.warning("Community folders not found", exc_info=True)
        return found

    # -- installing -----------------------------------------------------------

    def install(self, text: str, options: dict) -> dict:
        with self._lock:
            if self._job.get("state") == "running":
                return {"ok": False}
            if self._payload is None:
                self._job = {"state": "failed", "reason": "err.payload"}
                return {"ok": False}
            target = core.resolve_target(text, self._payload.size, self._payload.deepest)
            if not target.ok:
                return {"ok": False, "problems": target.problems}
            self._target = target.path
            self._job = {"state": "running", "done": 0,
                         "total": self._payload.size, "name": "", "files": []}
        opts = core.Options(desktop=bool(options.get("desktop", True)),
                            start_menu=bool(options.get("start_menu", True)),
                            check_updates=bool(options.get("updates", True)),
                            language=str(options.get("language") or ""),
                            toolbar_panel=bool(options.get("toolbar_panel", True)))
        threading.Thread(target=self._run, args=(target.path, opts),
                         name="wilcoatc-setup", daemon=True).start()
        return {"ok": True, "path": str(target.path)}

    def _run(self, target: Path, options: core.Options) -> None:
        def progress(done: int, total: int, name: str) -> None:
            with self._lock:
                job = self._job
                job["done"], job["total"] = done, total
                if name != job["name"]:
                    job["name"] = name
                    job["files"] = (job["files"] + [name])[-9:]

        try:
            result = core.install(self._payload, target, options, progress)
            with self._lock:
                self._job.update(state="done", done=self._job["total"], **(result or {}))
        except core.InstallError as exc:
            log.warning("install failed: %s %s", exc.key, exc.detail)
            with self._lock:
                self._job.update(state="failed", reason=exc.key, detail=exc.detail)
        except Exception as exc:
            log.exception("install failed")
            with self._lock:
                self._job.update(state="failed", reason="err.generic", detail=str(exc))

    def progress(self) -> dict:
        with self._lock:
            return dict(self._job, files=list(self._job.get("files", [])))

    def launch(self) -> bool:
        if self._target is None:
            return False
        try:
            core.launch(self._target)
            return True
        except OSError:
            log.warning("could not start WilcoATC", exc_info=True)
            return False

    # -- the window -----------------------------------------------------------

    def set_title(self, title: str) -> None:
        """The taskbar's name for the window, in the language just picked."""
        self._window.set_title(str(title)[:80])

    def open_url(self, url: str) -> None:
        if url.startswith("https://"):
            webbrowser.open(url)

    def minimize(self) -> None:
        self._window.minimize()

    def quit(self) -> None:
        with self._lock:
            if self._job.get("state") == "running":
                return
        self._window.destroy()


def _round_corners(window) -> None:
    """Windows 11 draws a frameless window square; ask it for its corners."""
    try:
        import ctypes

        hwnd = window.native.Handle.ToInt64()
        preference = ctypes.c_int(2)                 # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(   # DWMWA_WINDOW_CORNER_PREFERENCE
            ctypes.c_void_p(hwnd), 33, ctypes.byref(preference), 4)
    except Exception:
        log.debug("corners not rounded", exc_info=True)


def installed_location() -> str:
    """Where an earlier install is, from its entry in Settings > Apps."""
    if sys.platform != "win32":
        return ""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, core.UNINSTALL_KEY) as key:
            found = winreg.QueryValueEx(key, "InstallLocation")[0]
            return found if core.is_installation(Path(found)) else ""
    except OSError:
        return ""


def main() -> int:
    import webview

    logging.basicConfig(level=logging.INFO)
    lang, strings = catalogue(pick_language(core.ui_language()))
    payload_path = core.find_payload()
    try:
        payload = core.Payload(payload_path) if payload_path else None
    except Exception:
        log.exception("payload unreadable")
        payload = None
    earlier = installed_location()
    boot = {
        "lang": lang, "strings": strings, "version": __version__,
        "catalogues": catalogues(),
        "licence_name": LICENCE.replace("-only", ""),
        "languages": [[code, LANGUAGE_NAMES[code]] for code in available()],
        "path": earlier or str(core.default_target()),
        "upgrade": bool(earlier), "licence": core.licence_text(),
        "payload": payload is not None,
        "repo": core.RELEASES_PAGE.rsplit("/releases", 1)[0],
    }
    api = Api(payload, lang)
    window = webview.create_window(
        strings.get("window.title", "WilcoATC Setup"), html=page(boot),
        js_api=api, width=WIDTH, height=HEIGHT, resizable=False,
        frameless=True, easy_drag=False, background_color="#06080f")
    api._window = window
    window.events.shown += lambda: _round_corners(window)
    webview.start()
    if payload is not None:
        payload.close()
    return 0
