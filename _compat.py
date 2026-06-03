"""Compatibility helpers for legacy root-level module imports."""

from __future__ import annotations

import importlib
import sys
from typing import Any


def reexport(namespace: dict[str, Any], module_name: str) -> None:
    """Populate a wrapper module with every public and private implementation name."""
    module = importlib.import_module(module_name)
    exported = [
        name
        for name in dir(module)
        if not (name.startswith("__") and name.endswith("__"))
    ]
    namespace.update({name: getattr(module, name) for name in exported})
    namespace["__all__"] = exported
    namespace["__doc__"] = module.__doc__
    wrapper_name = namespace.get("__name__")
    if isinstance(wrapper_name, str):
        sys.modules[wrapper_name] = module
        parent_name, _, child_name = wrapper_name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None and child_name:
            setattr(parent, child_name, module)
