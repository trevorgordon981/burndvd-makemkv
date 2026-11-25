"""Compatibility proxy helper so legacy shim modules propagate monkeypatches."""

from __future__ import annotations

import sys
import types
from collections.abc import Iterable


def install_compat_proxy(
    module_name: str,
    target_modules: Iterable[types.ModuleType],
) -> None:
    """Install a lightweight ModuleType proxy on `sys.modules[module_name]`.

    Reads fall back to the first target module defining an attribute; writes
    (such as `unittest.mock.patch.object(ripqueue, ...)`) update BOTH the shim
    module and every target module that defines that symbol so decoupled
    submodules immediately observe the patched dependency.
    """
    mod = sys.modules.get(module_name)
    if mod is None:
        return
    targets = tuple(target_modules)

    class _CompatProxyModule(types.ModuleType):
        def __getattr__(self, name: str):
            for target in targets:
                if hasattr(target, name):
                    return getattr(target, name)
            raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")

        def __setattr__(self, name: str, value) -> None:
            super().__setattr__(name, value)
            if name.startswith("__") and name.endswith("__"):
                return
            for target in targets:
                if name in target.__dict__:
                    setattr(target, name, value)

        def __delattr__(self, name: str) -> None:
            super().__delattr__(name)
            if name.startswith("__") and name.endswith("__"):
                return
            for target in targets:
                if name in target.__dict__:
                    delattr(target, name)

    # Populate shim __dict__ with current public/internal symbols for dir() & mocks.
    for target in reversed(targets):
        for k, v in target.__dict__.items():
            if k.startswith("__") and k.endswith("__"):
                continue
            mod.__dict__.setdefault(k, v)

    mod.__class__ = _CompatProxyModule
