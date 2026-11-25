"""Backwards-compatibility facade re-exporting all legacy `ripqueue.py` symbols."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

from burndvd import config, drive, fallback, metadata, models, nas, probe, receipts, ripper, selection
from burndvd.cli import ripqueue as cli_ripqueue
from burndvd._compat import install_compat_proxy

# Re-export stdlib modules tests commonly patch on `ripqueue.<mod>`
__all__ = [
    "os",
    "shutil",
    "subprocess",
    "sys",
    "time",
    "config",
    "drive",
    "fallback",
    "metadata",
    "models",
    "nas",
    "probe",
    "receipts",
    "ripper",
    "selection",
    "cli_ripqueue",
]

_TARGET_MODULES = (
    cli_ripqueue,
    ripper,
    fallback,
    nas,
    selection,
    probe,
    drive,
    receipts,
    models,
    config,
)


def install_ripqueue_compat(module_name: str) -> None:
    """Turn a shim module (e.g. `ripqueue`) into a live proxy over `burndvd`."""
    install_compat_proxy(module_name, _TARGET_MODULES)


# Also install on this module itself so `from burndvd.compat_ripqueue import *` works.
install_compat_proxy(__name__, _TARGET_MODULES)
