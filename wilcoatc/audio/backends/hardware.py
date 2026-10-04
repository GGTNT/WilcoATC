"""Whether there is a GPU worth using, answered once.

The answer matters less here than it would in most programs: the card is
already drawing a flight simulator, and taking it for speech is a frame-rate
cost the pilot did not ask for. So this exists to *decline* the GPU tier on
almost every machine, not to reach for it -- a backend that needs a GPU is
only ever chosen when the hardware is there **and** the pilot has said so in
the config.

Detection goes through onnxruntime's provider list rather than through a
CUDA library or nvidia-smi. It is already a dependency, it answers for the
runtime that would actually do the work, and it is right about the case that
matters: a machine with a card but no CUDA build installed cannot use it.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

_answer: bool | None = None


def gpu_available() -> bool:
    """Whether an accelerated execution provider is actually usable.

    Cached: the import is not free and the answer does not change while the
    program is running. ``WILCOATC_FORCE_CPU=1`` pins it to False, which is
    how a machine with a card that is nonetheless busy is told so.
    """
    global _answer
    if _answer is not None:
        return _answer

    if os.environ.get("WILCOATC_FORCE_CPU", "").strip() not in ("", "0"):
        _answer = False
        return _answer

    try:
        import onnxruntime as ort

        providers = set(ort.get_available_providers())
    except Exception:                                # pragma: no cover
        log.debug("could not ask onnxruntime for its providers", exc_info=True)
        _answer = False
        return _answer

    _answer = bool(providers & {"CUDAExecutionProvider",
                                "DmlExecutionProvider",
                                "ROCMExecutionProvider",
                                "CoreMLExecutionProvider"})
    if not _answer:
        # The cloning tier runs on torch, and a CUDA build of torch sees a
        # card that a CPU build of onnxruntime does not. Only asked when torch
        # is there at all, which on a shipped build without that tier it is
        # not, so the ordinary case pays nothing.
        _answer = _torch_sees_a_card()
    log.debug("gpu available: %s", _answer)
    return _answer


def _torch_sees_a_card() -> bool:
    from importlib.util import find_spec

    try:
        if find_spec("torch") is None:
            return False
        import torch

        return bool(torch.cuda.is_available())
    except Exception:                                # pragma: no cover
        return False


def forget() -> None:
    """Drop the cached answer. For tests, and for nothing else."""
    global _answer
    _answer = None


__all__ = ["gpu_available", "forget"]
