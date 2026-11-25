"""Internal test-only write-through facades for legacy module-level patch suites."""

from __future__ import annotations

import importlib
import sys
import types
from types import ModuleType
from typing import Iterable


class _CompatModule(ModuleType):
    """Module proxy that resolves attributes across submodules and mirrors writes."""

    _compat_targets: tuple[str, ...] = ()

    def _loaded_targets(self) -> list[ModuleType]:
        mods: list[ModuleType] = []
        for name in self._compat_targets:
            mod = sys.modules.get(name)
            if mod is None:
                mod = importlib.import_module(name)
            mods.append(mod)
        return mods

    def __getattr__(self, name: str):
        for mod in self._loaded_targets():
            if hasattr(mod, name):
                return getattr(mod, name)
        raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")

    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        if name.startswith("_compat_") or name in ("__spec__", "__loader__", "__package__", "__path__"):
            return
        for mod in self._loaded_targets():
            if hasattr(mod, name):
                setattr(mod, name, value)

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)


def _make_proxy(proxy_name: str, target_modules: Iterable[str]) -> ModuleType:
    targets = tuple(target_modules)
    mod = _CompatModule(proxy_name)
    mod._compat_targets = targets
    for target in reversed(targets):
        loaded = importlib.import_module(target)
        for k, v in vars(loaded).items():
            if k.startswith("__"):
                continue
            ModuleType.__setattr__(mod, k, v)
    return mod


ripqueue = _make_proxy(
    "tests._compat.ripqueue",
    (
        "burndvd.cli.ripqueue",
        "burndvd.ripper",
        "burndvd.fallback",
        "burndvd.selection",
        "burndvd.probe",
        "burndvd.drive",
        "burndvd.nas",
        "burndvd.receipts",
        "burndvd.models",
        "burndvd.metadata",
        "burndvd.config",
    ),
)

subocr = _make_proxy(
    "tests._compat.subocr",
    (
        "burndvd.cli.subocr",
        "burndvd.subocr.pipeline",
        "burndvd.subocr.tracks",
        "burndvd.subocr.vobsub",
        "burndvd.subocr.pgs",
        "burndvd.subocr.srt",
        "burndvd.subocr.vision",
        "burndvd.models",
        "burndvd.config",
    ),
)

# expose pipeline submodule
import burndvd.subocr.pipeline as _subocr_pipeline
ModuleType.__setattr__(subocr, 'pipeline', _subocr_pipeline)
