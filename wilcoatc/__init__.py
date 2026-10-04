"""Local air traffic control for Microsoft Flight Simulator.

The version number lives here because three things have to agree on it: the
``--version`` a pilot can ask for, the line ``doctor`` prints at the top of a
report someone is about to paste into a bug thread, and the version resource
PyInstaller stamps into WilcoATC.exe.

The last of those is the reason it exists at all. An executable that carries
no publisher metadata is one of the things Windows Defender's classifiers
weigh when they decide whether an unsigned program is malware, and a
PyInstaller build carries none unless the spec is told to add it. See the
version resource in ``wilcoatc.spec``, and "Windows Defender" in the README.
"""

__version__ = "1.0.0"

#: What goes in the executable's version resource. Not decoration: a scanner
#: reads these fields, and a file with a publisher looks less like the packed
#: samples that deliberately have none.
PRODUCT_NAME = "WilcoATC"
COMPANY_NAME = "WilcoATC"
COPYRIGHT = "The code is yours to do as you like with."
DESCRIPTION = "Local air traffic control for Microsoft Flight Simulator"

__all__ = ["__version__", "PRODUCT_NAME", "COMPANY_NAME", "COPYRIGHT", "DESCRIPTION"]
