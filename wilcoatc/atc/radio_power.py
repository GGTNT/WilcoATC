"""Whether the aeroplane's radio has power, so whether anything is heard.

A cold and dark aeroplane has no radio: nothing it says goes out, and
nothing the controller says comes in. Before this, a pilot sitting in an
aeroplane with the battery off could key the microphone and be answered,
and the field's traffic chattered in the headset of a switched-off cockpit.

The simulator says it two ways for the stock aircraft. COM STATUS:1 is the
radio's own state in the simulator's electrical model (0 working, 1 not
fitted, 2 no electricity, 3 failed), and ELECTRICAL MAIN BUS VOLTAGE is the
bus it hangs off. Either saying "powered" is enough; "no power" needs one of
them to say so, with nothing else saying otherwise. An engine running
settles it the other way whatever the variables read: a running engine
turns a generator.

A study-level add-on runs its own electrical system, and the simulator's
standard variables can read "off" in an aeroplane that is fully powered --
the add-on files in ``atc/checklists/types`` already say which (``no_check``
names ``battery_master`` and ``avionics_master`` for Fenix, PMDG and the
rest). For those, nothing here is believed and the radio is always on,
because a controller who will not answer a pilot asking for clearance at
the gate with the APU running is a worse fault than a radio that works a
little early. ``atc.radio_needs_power`` switches the whole thing off.

UNVERIFIED on a running simulator: which stock aircraft drive COM STATUS
from their own electrics, and which add-ons beyond the listed ones leave it
at "no electricity". ``logs radio`` writes a ``radio_power`` row each time
the answer changes, with what it was decided from.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache

log = logging.getLogger(__name__)

COM_WORKING = 0
COM_NO_POWER = 2
# Below this the bus is dead: a stock aircraft's bus reads 0 with the battery
# off and about 24 or 28 with it on.
DEAD_BUS_VOLTS = 1.0

#: The add-on variables that, left unchecked, mean the stock electrics are
#: not the aeroplane's.
_OWN_ELECTRICS = {"battery_master", "avionics_master"}


@lru_cache(maxsize=32)
def own_electrics(title: str) -> bool:
    """Whether the model is an add-on whose electrics the simulator's
    variables do not follow, from its file in ``atc/checklists/types``."""
    from .checklist_files import _add_ons

    for path in _add_ons(title or ""):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if _OWN_ELECTRICS & set(raw.get("no_check", [])):
            return True
    return False


def decide(state) -> tuple[bool, str]:
    """Whether the radio is on, and why -- the reason is for the log."""
    if not getattr(state, "connected", False) or getattr(state, "source", "") != "simconnect":
        # A replay, a stand-in, the tests, or no simulator at all: there is
        # no aeroplane to be switched off.
        return True, "no simulator"
    if own_electrics(getattr(state, "title", "") or ""):
        return True, "add-on electrics"
    if getattr(state, "engines_running", False):
        return True, "engine running"
    status = getattr(state, "com1_status", -1)
    volts = getattr(state, "main_bus_volts", -1.0)
    if status == COM_WORKING:
        return True, "com working"
    if volts is not None and volts > DEAD_BUS_VOLTS:
        return True, f"bus {volts:.0f} V"
    if status == COM_NO_POWER:
        return False, "com no electricity"
    if volts is not None and 0 <= volts <= DEAD_BUS_VOLTS:
        return False, "bus dead"
    # Nothing either way: the radio is not taken away on no evidence.
    return True, "unknown"
