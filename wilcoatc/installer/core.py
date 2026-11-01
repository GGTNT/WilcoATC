"""What the installer does, with no window: checked, unpacked, registered.

Every path is a ``pathlib.Path`` from the moment the pilot types it until a
wide Win32 call receives it. The two places a path has to leave Python are
handled so that no code page ever sees it: PowerShell gets its script as
UTF-16 base64 (``-EncodedCommand``), and the uninstaller is a PowerShell
script saved with a BOM that finds its folder with ``$PSScriptRoot`` -- no
path is written into it at all. A launched program gets its path through
``CreateProcessW``. ``tests/test_installer.py`` runs all of it in a folder
called ``René``.

Where it goes by default is ``%LOCALAPPDATA%\\Programs\\WilcoATC``, a
per-user folder with no administrator prompt, rather than Program Files.
WilcoATC writes its voices, settings and logs beside itself (``paths.py``),
and its updater replaces its own files (``update.py``); under Program Files
neither works without elevation, and the program quietly moves its data to
%LOCALAPPDATA% and can no longer update itself. So a folder the pilot cannot
write to is refused with the reason, rather than half-working.
"""

from __future__ import annotations

import base64
import ctypes
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import COMPANY_NAME, PRODUCT_NAME, __version__
from ..update import PILOT_FILES, RELEASES_PAGE

log = logging.getLogger(__name__)

EXECUTABLE = "WilcoATC.exe"
FOLDER_NAME = "WilcoATC"
SHORTCUT = "WilcoATC.lnk"
UNINSTALLER = "uninstall.ps1"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\WilcoATC"

# What the first launch downloads on top: voices, the recogniser and the
# navigation data, "about 1.5 GB" on the program's own setup screen.
FIRST_RUN_BYTES = 1_500_000_000

# MAX_PATH less the terminator. Long paths work only where the machine has
# them switched on, so the deepest file in the payload is measured against it.
MAX_PATH = 259

_STEAM_APP = "2537590"                     # Microsoft Flight Simulator 2024
_STORE_PACKAGE = "Microsoft.Limitless_8wekyb3d8bbwe"

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


class InstallError(Exception):
    """A failure the window can say: ``key`` is its catalogue entry."""

    def __init__(self, key: str, detail: str = ""):
        super().__init__(detail or key)
        self.key = key
        self.detail = detail


# --------------------------------------------------------------------------
# the payload
# --------------------------------------------------------------------------


class Payload:
    """The release zip inside the installer: ``WilcoATC/`` and what is in it.

    The program's folder is found by its executable rather than assumed, and
    anything outside it -- the release has a note at the top of the zip -- is
    not installed.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._zip = zipfile.ZipFile(self.path)
        names = [i.filename.replace("\\", "/") for i in self._zip.infolist()]
        exe = sorted((n for n in names if n.rsplit("/", 1)[-1] == EXECUTABLE),
                     key=lambda n: n.count("/"))
        if not exe:
            raise InstallError("err.payload", f"{self.path.name} has no {EXECUTABLE}")
        self.prefix = exe[0][: -len(EXECUTABLE)]
        self.members = []
        for info in self._zip.infolist():
            name = info.filename.replace("\\", "/")
            if not name.startswith(self.prefix) or name.endswith("/"):
                continue
            rel = name[len(self.prefix):]
            if not rel or ".." in rel.split("/") or rel.startswith("/"):
                continue
            self.members.append((info, rel))

    @property
    def size(self) -> int:
        return sum(info.file_size for info, _ in self.members)

    @property
    def deepest(self) -> int:
        """Characters the longest file adds to the folder it goes in."""
        return max((len(rel) + 1 for _, rel in self.members), default=0)

    def close(self) -> None:
        self._zip.close()


def find_payload() -> Path | None:
    """The zip built into this installer, or the one named for a test run."""
    named = os.environ.get("WILCOATC_SETUP_PAYLOAD")
    if named:
        return Path(named)
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    found = sorted((root / "payload").glob("*.zip"))
    return found[0] if found else None


def licence_text() -> str:
    """The terms, as the build put them in; the README's section otherwise."""
    root = Path(getattr(sys, "_MEIPASS", ""))
    for candidate in (root / "licence.txt",
                      Path(__file__).resolve().parents[2] / "LICENSE"):
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8", errors="replace")
    readme = Path(__file__).resolve().parents[2] / "README.md"
    if readme.is_file():
        return readme_licence(readme.read_text(encoding="utf-8", errors="replace"))
    return ""


def readme_licence(text: str) -> str:
    match = re.search(r"^## Licen[cs]e[^\n]*\n(.*?)(?=^## |\Z)", text,
                      re.M | re.S)
    if not match:
        return ""
    body = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", match.group(1))
    paragraphs = [" ".join(p.split()) for p in body.strip().split("\n\n")]
    return "\n\n".join(p for p in paragraphs if p)


# --------------------------------------------------------------------------
# where it goes
# --------------------------------------------------------------------------


def default_target() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / FOLDER_NAME


def _nearest_existing(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        try:
            if candidate.exists():
                return candidate
        except OSError:
            continue
    return None


def is_installation(path: Path) -> bool:
    return (path / EXECUTABLE).is_file()


def _invalid_part(path: Path) -> bool:
    for part in path.parts[1:]:
        if re.search(r'[<>:"|?*\x00-\x1f]', part):
            return True
        # Windows drops a trailing dot or space, so the folder created would
        # not be the folder named.
        if part != part.rstrip(". "):
            return True
    return False


def _writable(path: Path) -> bool:
    """Whether a folder can be made where ``path`` is, tried, not guessed."""
    existing = _nearest_existing(path)
    if existing is None or not existing.is_dir():
        return False
    probe = existing / f".wilcoatc-setup-{uuid.uuid4().hex[:8]}"
    try:
        probe.mkdir()
        (probe / "é.txt").write_text("ok", encoding="utf-8")
        shutil.rmtree(probe)
        return True
    except OSError:
        shutil.rmtree(probe, ignore_errors=True)
        return False


def free_bytes(path: Path) -> int:
    existing = _nearest_existing(path)
    if existing is None:
        return 0
    try:
        return shutil.disk_usage(existing).free
    except OSError:
        return 0


@dataclass
class Target:
    """The folder as it will be used, and what to tell the pilot about it."""

    path: Path | None
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    free: int = 0
    upgrade: bool = False

    @property
    def ok(self) -> bool:
        return self.path is not None and not self.problems


def resolve_target(text: str, payload_size: int = 0, deepest: int = 0) -> Target:
    """What the pilot typed or picked, made into the folder to install in.

    A folder that already has other things in it gets a ``WilcoATC`` folder
    of its own inside: the uninstaller removes what it installed, and a pilot
    who points it at ``C:\\Users\\René\\AppData\\Roaming`` must not be one
    uninstall away from losing everything else there.
    """
    raw = (text or "").strip().strip('"').strip()
    if not raw:
        return Target(None, ["path.empty"])
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not path.is_absolute() or not path.drive:
        return Target(None, ["path.relative"])
    if _invalid_part(path):
        return Target(None, ["path.invalid"])
    path = Path(os.path.normpath(str(path)))

    notes: list[str] = []
    try:
        foreign = (path.is_dir() and not is_installation(path)
                   and any(path.iterdir()))
    except OSError:
        foreign = False
    if path.parent == path or foreign:
        path = path / FOLDER_NAME
        notes.append("path.subfolder")

    target = Target(path, notes=notes)
    target.upgrade = is_installation(path)
    if target.upgrade:
        notes.append("path.upgrade")

    windows = os.environ.get("SystemRoot")
    if windows and _inside(path, Path(windows)):
        target.problems.append("path.system")
    elif not _writable(path):
        target.problems.append("path.readonly")
    if deepest and len(str(path)) + deepest > MAX_PATH and not long_paths_enabled():
        target.problems.append("path.too_long")
    target.free = free_bytes(path)
    if payload_size and target.free and target.free < payload_size + 50_000_000:
        target.problems.append("path.space")
    return target


def _inside(path: Path, folder: Path) -> bool:
    a, b = str(path).casefold(), str(folder).casefold().rstrip("\\/")
    return a == b or a.startswith(b + "\\")


def long_paths_enabled() -> bool:
    if sys.platform != "win32":
        return True
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            return winreg.QueryValueEx(key, "LongPathsEnabled")[0] == 1
    except OSError:
        return False


# --------------------------------------------------------------------------
# the simulator
# --------------------------------------------------------------------------


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def steam_libraries() -> list[Path]:
    """Every Steam library folder, from Steam's own list of them.

    ``libraryfolders.vdf`` is UTF-8 with backslashes doubled; a library on
    ``D:\\Jeux\\Été`` comes out of it as that.
    """
    roots: list[Path] = []
    if sys.platform == "win32":
        import winreg

        for hive, key, value in (
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    roots.append(Path(winreg.QueryValueEx(handle, value)[0]))
            except OSError:
                continue
    libraries: list[Path] = []
    for root in roots:
        libraries.append(root)
        text = _read(root / "steamapps" / "libraryfolders.vdf")
        for found in re.findall(r'"path"\s+"((?:[^"\\]|\\.)*)"', text):
            libraries.append(Path(found.replace("\\\\", "\\")))
    unique: list[Path] = []
    for lib in libraries:
        if str(lib).casefold() not in {str(u).casefold() for u in unique}:
            unique.append(lib)
    return unique


def find_steam_install(libraries: list[Path]) -> Path | None:
    for lib in libraries:
        manifest = lib / "steamapps" / f"appmanifest_{_STEAM_APP}.acf"
        text = _read(manifest)
        if not text:
            continue
        name = re.search(r'"installdir"\s+"([^"]+)"', text)
        found = lib / "steamapps" / "common" / (name.group(1) if name else "MSFS2024")
        # Steam keeps its own folder in lower case ("c:\program files
        # (x86)\steam"); the disk knows how it is really written.
        try:
            return found.resolve() if found.exists() else found
        except OSError:
            return found
    return None


def _packages_path(usercfg: Path) -> str:
    match = re.search(r'^\s*InstalledPackagesPath\s+"([^"]+)"', _read(usercfg), re.M)
    return match.group(1) if match else ""


def detect_simulators() -> list[dict]:
    """MSFS 2024 from Steam and from the Microsoft Store, each found or not.

    Steam's install is read from its manifests; its settings folder in
    %APPDATA% is the second sign, for a library Steam no longer lists. The
    Store version is its package folder. Nothing is installed into either:
    WilcoATC talks to the simulator through SimConnect.
    """
    appdata = Path(os.environ.get("APPDATA", ""))
    local = Path(os.environ.get("LOCALAPPDATA", ""))

    steam = find_steam_install(steam_libraries())
    steam_cfg = appdata / "Microsoft Flight Simulator 2024" / "UserCfg.opt"
    steam_found = steam is not None or steam_cfg.is_file()
    store_dir = local / "Packages" / _STORE_PACKAGE
    store_cfg = store_dir / "LocalCache" / "UserCfg.opt"
    store_found = store_dir.is_dir()
    return [
        {"key": "steam", "found": steam_found,
         "path": str(steam) if steam else _packages_path(steam_cfg)},
        {"key": "store", "found": store_found,
         "path": _packages_path(store_cfg) or (str(store_dir) if store_found else "")},
    ]


# --------------------------------------------------------------------------
# installing
# --------------------------------------------------------------------------


def _long(path: Path) -> str:
    """A path every file call accepts, past MAX_PATH as well."""
    text = str(path)
    if sys.platform == "win32" and len(text) >= 240 and not text.startswith("\\\\?\\"):
        return "\\\\?\\" + text
    return text


def is_running(target: Path) -> bool:
    """Whether the program in ``target`` is open.

    A running executable cannot be opened for writing (its image is mapped),
    which answers the question for exactly this folder's copy, without
    listing processes and decoding their names through a code page.
    """
    for name in (EXECUTABLE, "WilcoATC-console.exe"):
        exe = target / name
        if not exe.is_file():
            continue
        try:
            with open(_long(exe), "r+b"):
                pass
        except PermissionError:
            return True
        except OSError:
            continue
    return False


@dataclass
class Options:
    desktop: bool = True
    start_menu: bool = True
    check_updates: bool = True
    # Picked in the installer; empty leaves the program to follow Windows.
    language: str = ""
    # The WilcoATC button in the simulator's toolbar, copied into every
    # Community folder found (wilcoatc/msfs_panel.py).
    toolbar_panel: bool = True


Progress = Callable[[int, int, str], None]


def install(payload: Payload, target: Path, options: Options,
            progress: Progress | None = None, register: bool = True,
            communities: list[Path] | None = None) -> dict:
    """Unpack the program into ``target`` and make it an installed program.

    Over an existing installation ``_internal`` is replaced whole (a file the
    new version no longer has must not be imported in place of the right one)
    and put back if anything fails; the pilot's own files -- config, voices,
    logs, the hand-edited navigation files -- are never written.
    """
    progress = progress or (lambda done, total, name: None)
    if is_running(target):
        raise InstallError("err.running")
    try:
        os.makedirs(_long(target), exist_ok=True)
    except OSError as exc:
        raise InstallError("err.folder", str(exc)) from exc

    old = target / "_internal"
    aside = target / f"_internal.old-{int(time.time())}"
    moved = False
    if old.is_dir():
        try:
            os.replace(_long(old), _long(aside))
            moved = True
        except OSError as exc:
            raise InstallError("err.running", str(exc)) from exc

    total = payload.size
    done = 0
    try:
        for info, rel in payload.members:
            dest = target.joinpath(*rel.split("/"))
            if (rel.startswith("data/navdata/") and dest.name in PILOT_FILES
                    and dest.exists()):
                done += info.file_size
                continue
            os.makedirs(_long(dest.parent), exist_ok=True)
            with payload._zip.open(info) as src, open(_long(dest), "wb") as out:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    progress(done, total, rel)
            progress(done, total, rel)
    except Exception as exc:
        if moved:
            shutil.rmtree(_long(old), ignore_errors=True)
            try:
                os.replace(_long(aside), _long(old))
            except OSError:
                log.warning("could not put the old _internal back", exc_info=True)
        if isinstance(exc, InstallError):
            raise
        raise InstallError("err.copy", str(exc)) from exc
    if moved:
        shutil.rmtree(_long(aside), ignore_errors=True)

    write_settings(target, options.check_updates, options.language)
    write_uninstaller(target)
    result = {"panel": [], "panel_failed": []}
    if options.toolbar_panel:
        if communities is None and register:
            from ..msfs_panel import community_folders

            communities = community_folders()
        result = install_toolbar_panel(target, communities or [])
    if register and sys.platform == "win32":
        shortcuts(target, options)
        register_uninstall(target, payload.size)
    return result


PANEL_FOLDER = "msfs-panel"
PANEL_NAME = "wilcoatc-ingamepanel"           # msfs_panel.NAME
PANEL_LIST = "msfs-panel-installed.txt"
CRLF = chr(13) + chr(10)


def install_toolbar_panel(target: Path, communities: list[Path]) -> dict:
    """The panel the program carries, into each Community folder.

    Never fatal: the program is installed whether or not the simulator's
    folder could be written to (a Community folder on a drive that has gone,
    a simulator running with the package open). The folders it did go into
    are listed beside the program, so the uninstaller takes it out of exactly
    those -- as UTF-8 with a BOM, which Windows PowerShell reads as UTF-8.
    """
    from ..msfs_panel import install as copy_panel

    source = target / PANEL_FOLDER
    done: list[str] = []
    failed: list[str] = []
    if not (source / "manifest.json").is_file():
        return {"panel": done, "panel_failed": failed}
    for community in communities:
        try:
            copy_panel(source, community)
            done.append(str(community))
        except OSError as exc:
            log.warning("toolbar panel not installed into %s: %s", community, exc)
            failed.append(str(community))
    listed = target / PANEL_LIST
    known = []
    if listed.is_file():
        known = [line.strip() for line in
                 listed.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    for path in done:
        if all(path.casefold() != k.casefold() for k in known):
            known.append(path)
    if known:
        listed.write_text(CRLF.join(known) + CRLF, encoding="utf-8-sig", newline="")
    return {"panel": done, "panel_failed": failed}


def write_settings(target: Path, check: bool, language: str = "") -> None:
    """What the installer asked, in the pilot's config, keeping the rest.

    ``ui.check_updates`` and, when one was picked in the installer, the
    language (``ui.language``), so the program opens in the language the
    pilot just chose. Written with ruamel's round trip over an existing file,
    which keeps the pilot's comments and order; no file at all when both are
    the defaults.
    """
    config = target / "config.yaml"
    if not config.exists():
        lines = []
        if language:
            lines.append(f"  language: {language}")
        if not check:
            lines.append("  check_updates: false")
        if lines:
            config.write_text("ui:\n" + "\n".join(lines) + "\n", encoding="utf-8")
        return
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap

    yaml = YAML()
    with open(config, encoding="utf-8") as handle:
        data = yaml.load(handle) or CommentedMap()
    ui = data.get("ui")
    if not isinstance(ui, dict):
        ui = CommentedMap()
        data["ui"] = ui
    changed = {}
    if language and ui.get("language") != language:
        changed["language"] = language
    # An absent key already means "check"; it is written only to say no, or
    # to take back a no written before.
    if ui.get("check_updates", True) != check:
        changed["check_updates"] = check
    if not changed:
        return
    ui.update(changed)
    with open(config, "w", encoding="utf-8") as handle:
        yaml.dump(data, handle)


# --------------------------------------------------------------------------
# Windows: shortcuts, the uninstall entry, the uninstaller
# --------------------------------------------------------------------------


def powershell(script: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    """Run a script handed over as UTF-16, so no code page touches a path."""
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / \
        "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        [str(exe), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", encoded],
        capture_output=True, timeout=timeout,
        creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)


def _ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


_FOLDERS = {
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "programs": "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}",
}


def known_folder(name: str) -> Path | None:
    """A shell folder by its id: the Desktop moved to OneDrive is found too."""
    if sys.platform != "win32":
        return None

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    value = uuid.UUID(_FOLDERS[name])
    guid = GUID(value.fields[0], value.fields[1], value.fields[2],
                (ctypes.c_ubyte * 8).from_buffer_copy(value.bytes[8:]))
    out = ctypes.c_wchar_p()
    shell32 = ctypes.windll.shell32
    if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) != 0:
        return None
    try:
        return Path(out.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


def shortcut_script(link: Path, target: Path) -> str:
    exe = target / EXECUTABLE
    return "\n".join([
        "$shell = New-Object -ComObject WScript.Shell",
        f"$s = $shell.CreateShortcut({_ps_quote(link)})",
        f"$s.TargetPath = {_ps_quote(exe)}",
        f"$s.WorkingDirectory = {_ps_quote(target)}",
        f"$s.IconLocation = {_ps_quote(str(exe) + ',0')}",
        f"$s.Description = {_ps_quote(PRODUCT_NAME)}",
        "$s.Save()",
    ])


def shortcuts(target: Path, options: Options) -> None:
    wanted = []
    if options.desktop:
        wanted.append(known_folder("desktop"))
    if options.start_menu:
        wanted.append(known_folder("programs"))
    for folder in wanted:
        if folder is None:
            continue
        link = folder / SHORTCUT
        result = powershell(shortcut_script(link, target))
        if result.returncode != 0 or not link.exists():
            log.warning("shortcut %s not made: %s", link,
                        result.stderr.decode("utf-8", "replace")[-400:])
            raise InstallError("err.shortcut", str(link))


def register_uninstall(target: Path, size: int) -> None:
    """The entry in Settings > Apps, per user, so no administrator is asked."""
    import winreg

    exe = target / EXECUTABLE
    ps = Path(os.environ.get("SystemRoot", r"C:\Windows")) / \
        "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    command = (f'"{ps}" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden '
               f'-File "{target / UNINSTALLER}"')
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0,
                            winreg.KEY_WRITE) as key:
        for name, value in (
                ("DisplayName", PRODUCT_NAME),
                ("DisplayVersion", __version__),
                ("Publisher", COMPANY_NAME),
                ("DisplayIcon", f"{exe},0"),
                ("InstallLocation", str(target)),
                ("UninstallString", command),
                ("QuietUninstallString", command + " -Quiet"),
                ("URLInfoAbout", RELEASES_PAGE.rsplit("/releases", 1)[0]),
                ("InstallDate", time.strftime("%Y%m%d"))):
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        for name, value in (("NoModify", 1), ("NoRepair", 1),
                            ("EstimatedSize", max(1, size // 1024))):
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)


#: What the uninstaller removes: the program, and only with the pilot's yes
#: what the program made beside it. Nothing else in the folder is touched.
PROGRAM_ITEMS = ("_internal", "WilcoATC.exe", "WilcoATC-console.exe",
                 "WilcoATC.exe.config", "WilcoATC-console.exe.config",
                 "FIRST-RUN.txt", "README.md", "LICENSE.txt", "updates",
                 ".write-test", PANEL_FOLDER, PANEL_LIST)
PILOT_ITEMS = ("data", "logs", "airports", "announcements", "checklists",
               "config.yaml", "config.local.yaml")


def uninstaller_script() -> str:
    """The uninstaller. No path is written into it: it is where it is.

    Saved as UTF-8 with a BOM, which is how Windows PowerShell 5.1 knows to
    read the French in it as UTF-8 rather than in the ANSI code page.
    """
    program = ", ".join(_ps_quote(n) for n in PROGRAM_ITEMS)
    pilot = ", ".join(_ps_quote(n) for n in PILOT_ITEMS)
    return f"""# WilcoATC uninstaller, written by WilcoATC-Setup {__version__}.
# It removes what the installer put in this folder, the shortcuts that point
# at it and its entry in Settings > Apps -- and, only if asked, the voices,
# settings and logs WilcoATC made beside itself. Nothing else here is touched.
param([switch]$Quiet)
$ErrorActionPreference = 'Continue'
Add-Type -AssemblyName System.Windows.Forms
$dir = $PSScriptRoot
$fr = (Get-UICulture).TwoLetterISOLanguageName -eq 'fr'
function Say($en, $french) {{ if ($fr) {{ $french }} else {{ $en }} }}
function Ask($text, $icon) {{
  if ($Quiet) {{ return 'Yes' }}
  return [System.Windows.Forms.MessageBox]::Show($text, 'WilcoATC', 'YesNo', $icon)
}}
function Tell($text) {{
  if (-not $Quiet) {{ [void][System.Windows.Forms.MessageBox]::Show($text, 'WilcoATC', 'OK', 'Information') }}
}}

if ((Ask (Say 'Uninstall WilcoATC?' 'Désinstaller WilcoATC ?') 'Question') -ne 'Yes') {{ exit 0 }}

$open = Get-Process -Name 'WilcoATC', 'WilcoATC-console' -ErrorAction SilentlyContinue |
  Where-Object {{ $_.Path -and $_.Path.StartsWith($dir, [System.StringComparison]::OrdinalIgnoreCase) }}
if ($open) {{
  Tell (Say 'WilcoATC is still open. Close it, then uninstall again.' 'WilcoATC est encore ouvert. Fermez-le, puis relancez la désinstallation.')
  exit 2
}}

$keep = $true
if (-not $Quiet) {{
  $keep = (Ask (Say "Keep your settings, voices and logs?`n`nYes keeps them in this folder for a later reinstall. No deletes them too." "Conserver vos réglages, voix et journaux ?`n`nOui les garde dans ce dossier pour une réinstallation. Non les supprime aussi.") 'Question') -eq 'Yes'
}}

# The toolbar panel, out of the Community folders it was put in.
$panels = Join-Path $dir '{PANEL_LIST}'
if (Test-Path -LiteralPath $panels) {{
  foreach ($community in (Get-Content -LiteralPath $panels -Encoding UTF8)) {{
    if (-not $community) {{ continue }}
    $panel = Join-Path $community '{PANEL_NAME}'
    if (Test-Path -LiteralPath $panel) {{ Remove-Item -LiteralPath $panel -Recurse -Force -ErrorAction SilentlyContinue }}
  }}
}}

Set-Location -LiteralPath $env:TEMP
$items = @({program})
if (-not $keep) {{ $items += @({pilot}) }}
foreach ($name in $items) {{
  $path = Join-Path $dir $name
  if (Test-Path -LiteralPath $path) {{ Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue }}
}}

$exe = Join-Path $dir 'WilcoATC.exe'
$shell = New-Object -ComObject WScript.Shell
foreach ($folder in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {{
  $link = Join-Path $folder '{SHORTCUT}'
  if ((Test-Path -LiteralPath $link) -and ($shell.CreateShortcut($link).TargetPath -eq $exe)) {{
    Remove-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue
  }}
}}

$key = 'HKCU:\\{UNINSTALL_KEY}'
$entry = Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue
if ($entry -and $entry.InstallLocation -eq $dir) {{ Remove-Item -LiteralPath $key -Recurse -Force -ErrorAction SilentlyContinue }}

Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
if (-not (Get-ChildItem -LiteralPath $dir -Force -ErrorAction SilentlyContinue)) {{
  Remove-Item -LiteralPath $dir -Force -ErrorAction SilentlyContinue
}}
Tell (Say 'WilcoATC has been uninstalled.' 'WilcoATC a été désinstallé.')
"""


def write_uninstaller(target: Path) -> Path:
    path = target / UNINSTALLER
    path.write_text(uninstaller_script().replace("\n", "\r\n"),
                    encoding="utf-8-sig", newline="")
    return path


# --------------------------------------------------------------------------
# afterwards
# --------------------------------------------------------------------------


def child_environment() -> dict:
    """This process's environment without the installer's own bootloader in it.

    The installer is a one-file PyInstaller program; a PyInstaller program
    started from one inherits variables that tell its bootloader it is the
    second half of an unpacking that is not its own.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("_MEI", "_PYI", "_PYIBoot"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def launch(target: Path) -> None:
    flags = 0
    if sys.platform == "win32":
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(target / EXECUTABLE)], cwd=str(target),
                     env=child_environment(), creationflags=flags,
                     close_fds=True)


def ui_language() -> str:
    """Windows' own interface language, as a locale name (``fr_FR``)."""
    try:
        import locale

        lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        name = locale.windows_locale.get(lcid, "")
    except Exception:
        name = ""
    return name or "en"
