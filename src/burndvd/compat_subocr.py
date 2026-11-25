"""Backwards-compatibility facade re-exporting all legacy `subocr.py` symbols."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys

from burndvd import config
from burndvd._compat import install_compat_proxy
from burndvd.cli import subocr as cli_subocr
from burndvd.subocr import pgs, pipeline, srt, tracks, vision

_TARGET_MODULES = (
    cli_subocr,
    pipeline,
    tracks,
    pgs,
    vision,
    srt,
    config,
)


def install_subocr_compat(module_name: str) -> None:
    """Turn a shim module (e.g. `subocr`) into a live proxy over `burndvd.subocr`."""
    install_compat_proxy(module_name, _TARGET_MODULES)


install_compat_proxy(__name__, _TARGET_MODULES)
