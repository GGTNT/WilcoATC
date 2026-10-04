# -*- mode: python ; coding: utf-8 -*-
"""How WilcoATC is turned into a program that needs nothing installed.

The result is a folder: WilcoATC.exe, a WilcoATC-console.exe beside it for the
command line, and an _internal folder holding the Python runtime and every
native library the radio uses -- CTranslate2 for the recogniser, ONNX Runtime
and espeak-ng for the voices, PortAudio for the sound card, SimConnect for the
simulator. A pilot copies the folder and runs it. There is no Python to
install, no pip, no virtual environment.

What is deliberately *not* in here is the 2.4 GB of voices and recogniser
weights. They are downloaded on first run, in the languages the pilot actually
flies, which is what keeps the download a few hundred megabytes rather than
three gigabytes of German and Polish for someone who only ever flies Kennedy.

    .venv\\Scripts\\python -m PyInstaller --noconfirm --clean wilcoatc.spec
"""

import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

ROOT = Path(SPECPATH)  # noqa: F821 -- PyInstaller injects it
WINDOWS = sys.platform == "win32"

# --- what ships alongside the code ----------------------------------------
#
# The panel is the interface for both the window and the tablet, so its HTML,
# CSS, JavaScript and thirty-odd translations have to be in the bundle. The
# navigation database is not here: it is copied in beside the executable by
# build.ps1, because it gets rebuilt and hand-corrected and neither can be
# done to a file sealed inside the program.

datas = [
    (str(ROOT / "wilcoatc" / "web" / "static"), "wilcoatc/web/static"),
    # The magnetic model's coefficients, read beside navdata/magvar.py. A
    # build without them speaks true winds, which is wrong by the variation.
    (str(ROOT / "wilcoatc" / "navdata" / "WMM_2025.COF"), "wilcoatc/navdata"),
    # The highest terrain per eighth of a degree (1.6 MB), for the descent
    # floor. Without it the floor is field elevation plus 3000 ft, which in
    # the mountains is below the ridges.
    (str(ROOT / "wilcoatc" / "navdata" / "terrain_max.bin"), "wilcoatc/navdata"),
]

# The airline telephony names do ship inside, as the fallback: build.ps1 only
# copies the navigation folder beside the program when there is a database to
# copy, and an airliner must not be spelled out because there was not. A copy
# beside the program, edited by the pilot, is read first.
_callsigns = ROOT / "data" / "navdata" / "icao_callsigns.json"
if _callsigns.exists():
    datas.append((str(_callsigns), "data/navdata"))

# Piper carries espeak-ng's pronunciation data, which is what turns written
# words into the phonemes a voice model speaks. Without it there is no speech
# at all, in any language.
datas += collect_data_files("piper")

# Kokoro's tokeniser ships its phoneme vocabulary as a package data file, and
# its espeak loader carries a second copy of the pronunciation data for the
# languages Piper's build does not cover.
datas += collect_data_files("kokoro_onnx")
datas += collect_data_files("espeakng_loader")

# The recogniser's voice-activity model, which faster-whisper loads from its
# own package directory.
datas += collect_data_files("faster_whisper")

# PortAudio, and libsndfile for reading and writing wav.
datas += collect_data_files("_sounddevice_data")
datas += collect_data_files("_soundfile_data")

if WINDOWS:
    # The Python SimConnect package reaches its DLL by path, so PyInstaller
    # cannot see the dependency by itself.
    datas += collect_data_files("SimConnect", includes=["*.dll"])

# --- native libraries ------------------------------------------------------

binaries = []
binaries += collect_dynamic_libs("ctranslate2")   # the recogniser's engine
binaries += collect_dynamic_libs("onnxruntime")   # the voices' engine
binaries += collect_dynamic_libs("piper")         # espeakbridge
binaries += collect_dynamic_libs("espeakng_loader")
binaries += collect_dynamic_libs("av")            # audio decoding

# CUDA is never used: the recogniser runs int8 on the CPU by design, so that
# the program is not asking a pilot's graphics card for anything while the
# simulator is using all of it.
binaries = [b for b in binaries if "cudnn" not in Path(b[0]).name.lower()
            and "cublas" not in Path(b[0]).name.lower()]

# --- imports the analysis cannot see --------------------------------------

hiddenimports = [
    "wilcoatc.audio.tts_kokoro",
    # Reached only from inside a function, so the analysis cannot see it: the
    # doctor's Windows-block check, and the two modules behind the setup and
    # diagnostics buttons.
    "wilcoatc.winzone",
    "wilcoatc.health",
    "wilcoatc.install",
    "encodings.idna",
    "websockets",
    "httptools",
]
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("pynput")
hiddenimports += collect_submodules("piper")
hiddenimports += collect_submodules("kokoro_onnx")

# --- what stays out --------------------------------------------------------
#
# The virtual environment has grown a research half -- librosa, numba,
# scikit-learn, the MOS scorer -- used by the scripts that measured the voice
# chain. None of it is on the path a transmission takes, and together it is
# well over two hundred megabytes.

excludes = [
    "numba", "llvmlite", "librosa", "sklearn", "scikit_learn", "speechmos",
    "soxr", "pooch", "joblib", "threadpoolctl", "lazy_loader",
    "matplotlib", "IPython", "notebook", "tkinter",
    "pytest", "_pytest", "pluggy", "pyflakes", "iniconfig",
    "torch", "tensorflow", "onnx", "transformers",
    "pip", "setuptools", "wheel",
    "fpdf", "fontTools",          # the printed phrasebook is built, not flown
    "anthropic",                  # optional: only for the Claude provider
]

a = Analysis(  # noqa: F821
    ["run_wilcoatc.py"],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

ICON = ROOT / "wilcoatc" / "web" / "static" / "logo.ico"
icon = str(ICON) if ICON.exists() else None

# --- what a scanner reads before it reads anything else --------------------
#
# Windows Defender called an early build a trojan, and the shape it was
# reacting to is the one every PyInstaller program has: an unsigned binary
# with no publisher, carrying a compressed archive it unpacks at startup,
# which then installs a global keyboard hook for push-to-talk and opens the
# microphone. That is also, feature for feature, the description of a
# keylogger, and the classifier is a classifier rather than a reader.
#
# Only an Authenticode signature answers this properly, and build.ps1 -Sign
# applies one when there is a certificate to apply. The version resource is
# what can be done without one: it is weighed by the same classifiers, it
# costs nothing, and a file with a product name and a version is not in the
# same bucket as a file that went out of its way to have neither. It also
# gives a pilot something to read in the file's Properties dialog, which is
# the first place anyone looks at a program they were just warned about.


def _read_metadata() -> dict[str, str]:
    """Pull the version and publisher strings out of wilcoatc/__init__.py.

    Read as text rather than imported: a spec file runs before the analysis
    has worked out what is importable, and the package must not have to be on
    the path for the build to know what to call itself.
    """
    text = (ROOT / "wilcoatc" / "__init__.py").read_text(encoding="utf-8")
    found = dict(re.findall(r'^(\w+)\s*=\s*"([^"]*)"', text, re.MULTILINE))
    missing = {"__version__", "PRODUCT_NAME", "COMPANY_NAME", "COPYRIGHT",
               "DESCRIPTION"} - set(found)
    if missing:
        raise SystemExit(
            f"wilcoatc/__init__.py is missing {', '.join(sorted(missing))}, "
            "which the executable's version resource is built from"
        )
    return found


META = _read_metadata()


def version_resource(name: str, description: str):
    """The VS_VERSION_INFO block stamped into one of the two executables."""
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable,
        VarFileInfo, VarStruct, VSVersionInfo,
    )

    # The fixed block wants exactly four integers whatever the version string
    # looks like, so a two- or three-part version is padded rather than
    # rejected.
    parts = [int(p) for p in re.findall(r"\d+", META["__version__"])]
    numbers = tuple((parts + [0, 0, 0, 0])[:4])

    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers, mask=0x3F,
                          flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", META["COMPANY_NAME"]),
                StringStruct("FileDescription", description),
                StringStruct("FileVersion", META["__version__"]),
                StringStruct("InternalName", name),
                StringStruct("LegalCopyright", META["COPYRIGHT"]),
                StringStruct("OriginalFilename", f"{name}.exe"),
                StringStruct("ProductName", META["PRODUCT_NAME"]),
                StringStruct("ProductVersion", META["__version__"]),
            ])]),
            # 0x0409 is US English and 1200 is the UTF-16 code page. They have
            # to spell out the "040904B0" key above or Windows reads the whole
            # string table as empty and the resource is worse than useless.
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )


# Two executables over one analysis. The windowed one is what a pilot double
# clicks; the console one is the same program with a terminal attached, which
# is what `doctor`, `freq --audit` and `setup` need in order to be read.

exe_window = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WilcoATC",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX is what makes antivirus flag PyInstaller builds
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
    version=version_resource(
        "WilcoATC", META["DESCRIPTION"]) if WINDOWS else None,
)

exe_console = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WilcoATC-console",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
    version=version_resource(
        "WilcoATC-console",
        META["DESCRIPTION"] + " (command line)") if WINDOWS else None,
)

coll = COLLECT(  # noqa: F821
    exe_window,
    exe_console,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="WilcoATC",
)
