# -*- mode: python ; coding: utf-8 -*-
"""WilcoATC-Setup.exe: one file that installs the program (wilcoatc/installer).

Built by ``build.ps1 -Setup`` after the program itself, because it carries
it: ``WILCOATC_SETUP_PAYLOAD`` names the zip made by
``wilcoatc.installer.pack`` from ``dist\\WilcoATC``, and
``WILCOATC_SETUP_NAME`` the executable (WilcoATC-Setup, WilcoATC-HQ-Setup).

One file rather than a folder, so it is one download; the cost is that it
unpacks itself into %TEMP% each time it starts, a few seconds for the
quarter of a gigabyte it holds. Only the installer's own code goes in -- the
analysis starts at run_setup.py, which reaches pywebview and ruamel.yaml and
none of the radio -- so the rest of the size is the payload.
"""

import os
import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH)  # noqa: F821
PAYLOAD = os.environ.get("WILCOATC_SETUP_PAYLOAD", "")
NAME = os.environ.get("WILCOATC_SETUP_NAME", "WilcoATC-Setup")
if not PAYLOAD or not Path(PAYLOAD).is_file():
    raise SystemExit("WILCOATC_SETUP_PAYLOAD must name the program zip; "
                     "build with .\\build.ps1 -Setup")


def _stale(payload: str) -> list[str]:
    """The panel files in the payload that differ from this tree's.

    The first installer shipped a dist folder three days older than the
    source -- built before the first-run screens Appearance and First officer
    existed -- and nothing said so: the installer looked right and the
    program inside it was not the one in the tree. build.ps1 -Setup always
    packs the folder it has just built; this catches a payload made any
    other way.
    """
    import zipfile

    static = ROOT / "wilcoatc" / "web" / "static"
    differ = []
    with zipfile.ZipFile(payload) as zf:
        names = {n.replace("\\", "/"): n for n in zf.namelist()}
        for rel in ("index.html", "app.js", "i18n/en.json"):
            inside = next((n for k, n in names.items()
                           if k.endswith(f"_internal/wilcoatc/web/static/{rel}")), None)
            if inside is None or zf.read(inside) != (static / rel).read_bytes():
                differ.append(rel)
        # And the toolbar panel the installer copies into the simulator.
        panel = ROOT / "msfs" / "package" / "html_ui" / "InGamePanels" / "WilcoATC"
        for rel in ("WilcoATC.js", "WilcoATC.html", "WilcoATC.css"):
            inside = next((n for k, n in names.items()
                           if k.endswith(f"msfs-panel/html_ui/InGamePanels/WilcoATC/{rel}")), None)
            if inside is None or zf.read(inside) != (panel / rel).read_bytes():
                differ.append(f"msfs-panel/{rel}")
    return differ


_old = _stale(PAYLOAD)
if _old and os.environ.get("WILCOATC_SETUP_ALLOW_STALE") != "1":
    raise SystemExit(f"the program in {PAYLOAD} is not this tree's ({', '.join(_old)} "
                     "differ): rebuild it with .\\build.ps1 -Setup, or set "
                     "WILCOATC_SETUP_ALLOW_STALE=1 to ship it anyway")

datas = [
    (str(ROOT / "wilcoatc" / "installer" / "ui"), "wilcoatc/installer/ui"),
    (PAYLOAD, "payload"),
]

# The terms shown on the Licence screen: LICENSE if the project has one,
# otherwise the README's own "Licence and data" section.
licence = ROOT / "build" / "setup-licence" / "licence.txt"
licence.parent.mkdir(parents=True, exist_ok=True)
if (ROOT / "LICENSE").is_file():
    licence.write_text((ROOT / "LICENSE").read_text(encoding="utf-8"), encoding="utf-8")
else:
    sys.path.insert(0, str(ROOT))
    from wilcoatc.installer.core import readme_licence

    licence.write_text(readme_licence((ROOT / "README.md").read_text(encoding="utf-8")),
                       encoding="utf-8")
datas.append((str(licence), "."))

a = Analysis(  # noqa: F821
    ["run_setup.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=["ruamel.yaml", "webview.platforms.winforms",
                   "webview.platforms.edgechromium"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["numpy", "torch", "onnxruntime", "ctranslate2", "faster_whisper",
              "piper", "pygame", "sounddevice", "soundfile", "scipy", "librosa",
              "fastapi", "uvicorn", "tkinter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)  # noqa: F821

ICON = ROOT / "wilcoatc" / "web" / "static" / "logo.ico"


def _meta() -> dict:
    text = (ROOT / "wilcoatc" / "__init__.py").read_text(encoding="utf-8")
    return dict(re.findall(r'^(\w+)\s*=\s*"([^"]*)"', text, re.M))


def _version():
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable,
        VarFileInfo, VarStruct, VSVersionInfo)

    meta = _meta()
    parts = [int(p) for p in re.findall(r"\d+", meta["__version__"])]
    numbers = tuple((parts + [0, 0, 0, 0])[:4])
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers, mask=0x3F, flags=0x0,
                          OS=0x40004, fileType=0x1, subtype=0x0),
        kids=[StringFileInfo([StringTable("040904B0", [
                  StringStruct("CompanyName", meta["COMPANY_NAME"]),
                  StringStruct("FileDescription", "WilcoATC Setup"),
                  StringStruct("FileVersion", meta["__version__"]),
                  StringStruct("InternalName", NAME),
                  StringStruct("LegalCopyright", meta.get("COPYRIGHT", "")),
                  StringStruct("OriginalFilename", f"{NAME}.exe"),
                  StringStruct("ProductName", meta["PRODUCT_NAME"]),
                  StringStruct("ProductVersion", meta["__version__"])])]),
              VarFileInfo([VarStruct("Translation", [0x0409, 1200])])])


exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX is what makes antivirus flag PyInstaller builds
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ICON) if ICON.exists() else None,
    version=_version() if sys.platform == "win32" else None,
)
