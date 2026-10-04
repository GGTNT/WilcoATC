"""The real airline liveries, when FSLTL is installed.

The invented traffic is created from the simulator's own AI models, which
come in a house livery: an Air France flight on the radio is a white A320 on
the runway. FSLTL ships two thousand and more liveried models and, with them,
the table that says which one to use for which airline on which type --
``FSLTL_Rules.vmr``, a model matching file of a hundred thousand rules of the
form "callsign prefix, ICAO type, model". That table is what this reads. The
models are not copied or guessed at: FSLTL's own answer is the one asked for,
and the simulator refuses it by name if this installation does not have it,
which puts the flight back on the ordinary models (see
:mod:`wilcoatc.integrations.aitraffic`).

Finding it. FSLTL lives in the Community folder, and the Community folder is
wherever the pilot told the simulator to put its packages -- recorded in
``UserCfg.opt`` as ``InstalledPackagesPath``, in one of four places depending
on the simulator (2020 or 2024) and the store it came from. Those are read in
turn; ``traffic.fsltl_rules`` in the configuration names the file outright
for an installation none of them describes.

Nothing here is written. FSLTL is somebody else's package and this only reads
its table.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

RULES_FILE = "FSLTL_Rules.vmr"

# Where each simulator records where its packages are. 2024 first: it is the
# one this was measured against, and a machine with both is most likely
# flying the newer one.
_USERCFG = (
    ("APPDATA", "Microsoft Flight Simulator 2024/UserCfg.opt"),
    ("LOCALAPPDATA",
     "Packages/Microsoft.Limitless_8wekyb3d8bbwe/LocalCache/UserCfg.opt"),
    ("APPDATA", "Microsoft Flight Simulator/UserCfg.opt"),
    ("LOCALAPPDATA",
     "Packages/Microsoft.FlightSimulator_8wekyb3d8bbwe/LocalCache/UserCfg.opt"),
)

_PACKAGES = re.compile(r'^\s*InstalledPackagesPath\s+"([^"]+)"', re.M)
_RULE = re.compile(r"<ModelMatchRule\s+([^>]*?)/?>")
_ATTRIBUTE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


def _generic(model: str) -> bool:
    """Whether a model is FSLTL's unpainted one for its type.

    FSLTL names those with a run of Zs where the airline would be
    (``FSLTL_B738_ZZZZ``). It is what the table hands out for an airline it
    has no livery for on that type -- the right answer for FSLTL, and not a
    real livery.
    """
    return "_ZZZ" in model.upper()


class FsltlLiveries:
    """FSLTL's model matching table, by airline and type."""

    def __init__(self, rules: dict[tuple[str, str], tuple[str, ...]],
                 source: Path | None = None):
        # (callsign prefix, ICAO type) -> models, best first; the prefix is
        # empty for the rule that covers any airline.
        self._rules = rules
        self.source = source

    @classmethod
    def parse(cls, text: str, source: Path | None = None) -> "FsltlLiveries":
        rules: dict[tuple[str, str], tuple[str, ...]] = {}
        for body in _RULE.findall(text):
            found = dict(_ATTRIBUTE.findall(body))
            type_code = found.get("TypeCode", "").strip().upper()
            models = tuple(m.strip() for m in
                           found.get("ModelName", "").split("//")
                           if m.strip())
            if not type_code or not models:
                continue
            prefix = found.get("CallsignPrefix", "").strip().upper()
            # The first rule for a pair wins, which is how the simulator
            # reads a matching file.
            rules.setdefault((prefix, type_code), models)
        return cls(rules, source)

    @classmethod
    def load(cls, path: Path) -> "FsltlLiveries | None":
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            log.info("could not read %s", path, exc_info=True)
            return None
        liveries = cls.parse(text, Path(path))
        return liveries if liveries else None

    def __bool__(self) -> bool:
        return bool(self._rules)

    def __len__(self) -> int:
        return len(self._rules)

    @property
    def airlines(self) -> int:
        return len({prefix for prefix, _ in self._rules if prefix})

    def models(self, airline: str, type_code: str) -> tuple[str, ...]:
        """What FSLTL would draw this airline's aeroplane of this type as.

        Its own answer, painted or not: an airline it has no livery for on
        this type gets the unpainted model, which is still FSLTL's model of
        the type. Empty when the table does not know the type at all.
        """
        type_code = (type_code or "").upper()
        found = self._rules.get(((airline or "").upper(), type_code))
        if found is None:
            found = self._rules.get(("", type_code), ())
        return found

    def has_livery(self, airline: str, type_code: str) -> bool:
        """Whether this airline flies this type in its own colours here."""
        if not airline:
            return False
        found = self._rules.get(((airline or "").upper(),
                                 (type_code or "").upper()), ())
        return any(not _generic(model) for model in found)


def packages_folders(environ=None) -> list[Path]:
    """Every package folder a simulator on this machine says it uses."""
    environ = os.environ if environ is None else environ
    found: list[Path] = []
    for variable, relative in _USERCFG:
        base = environ.get(variable)
        if not base:
            continue
        config = Path(base) / relative
        try:
            text = config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _PACKAGES.finditer(text):
            folder = Path(match.group(1))
            if folder not in found:
                found.append(folder)
    return found


def find_rules(environ=None, override: str = "") -> Path | None:
    """Where FSLTL's matching table is, or None if it is not installed.

    One folder deep in each Community folder, because FSLTL's is named
    ``fsltl-traffic-base`` today and the name is theirs to change.
    """
    if override:
        path = Path(override)
        return path if path.is_file() else None
    for packages in packages_folders(environ):
        for community in sorted(packages.glob("Community*")):
            direct = community / "fsltl-traffic-base" / RULES_FILE
            if direct.is_file():
                return direct
            try:
                for candidate in sorted(community.glob(f"*/{RULES_FILE}")):
                    return candidate
            except OSError:
                continue
    return None


def load_installed(environ=None, override: str = "") -> FsltlLiveries | None:
    """FSLTL's table, if FSLTL is installed; None if it is not."""
    path = find_rules(environ, override)
    if path is None:
        return None
    return FsltlLiveries.load(path)


__all__ = ["FsltlLiveries", "find_rules", "load_installed",
           "packages_folders", "RULES_FILE"]
