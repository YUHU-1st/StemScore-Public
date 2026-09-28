from __future__ import annotations

"""Compatibility entrypoint for Demucs checkpoints with PyTorch 2.6+.

Demucs v4 checkpoints are full trusted model packages, not tensor-only state dicts.
PyTorch changed torch.load's default to weights_only=True, while the Demucs CLI
still relies on the historical default. StemScore hashes and records every local
UVR checkpoint before this entrypoint is used.
"""

import torch


_torch_load = torch.load


def _load_legacy_demucs(*args, **kwargs):
    kwargs.setdefault("weights_only", False)
    return _torch_load(*args, **kwargs)


torch.load = _load_legacy_demucs

from demucs.separate import main


if __name__ == "__main__":
    main()

