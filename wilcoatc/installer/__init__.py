"""WilcoATC-Setup.exe: the program installed from one file.

The release used to be a zip, and a zip is three ways to get it wrong:
Windows' own extractor marks every file as coming from the internet (and the
.NET runtime behind the window then refuses to load), a pilot unpacks it into
Downloads and loses it, or unpacks a new version beside the old one and loses
the voices and the config. The installer unpacks the same folder itself --
no marks, a known place, over the old one without touching what the pilot
owns -- and adds what a zip cannot: shortcuts, an entry in Windows' list of
installed programs, and an uninstaller.

``core`` is everything it does, testable without a window; ``app`` is the
window (pywebview, the same engine the program itself uses) drawn from the
"WilcoATC Installer" design.

Paths with accents are the case this was written around. A pilot called René
has ``C:\\Users\\René`` in every default path, and every way an installer
usually breaks on that -- a batch file read in the OEM code page, a command
line built from a string in the ANSI one, ``tasklist`` output decoded wrong --
is avoided rather than handled: nothing here passes a path through a code
page. Python and the Win32 wide APIs carry it, PowerShell is handed its
script as UTF-16 (``-EncodedCommand``), and the uninstaller finds its own
folder from ``$PSScriptRoot`` instead of having it written into it.
"""
