# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (c) Virtastic
"""Idempotent Draft tracker cleanup for the pinned FreeCAD browser engine.

Rectangle completion can call off() after a nested finish has finalized its
tracker. finalize() deliberately clears switch; hiding/removing that already
removed switch is therefore a no-op. This repairs that lifecycle boundary only:
live Coin operations, geometry, numeric input and genuine exceptions retain their
original behavior. The source patch carries the same guards into future builds.

Loaded by sitecustomize, before Draft's first import. No GUI is imported early.
"""
import functools
import importlib.abc
import importlib.machinery
import sys

_TARGET = "draftguitools.gui_trackers"


def repair_module(module):
    tracker = module.Tracker
    if getattr(tracker, "_fcweb_idempotent_cleanup", False):
        return False
    original_off = tracker.off
    original_finalize = tracker.finalize

    @functools.wraps(original_off)
    def off(self):
        if self.switch is None:
            self.Visible = False
            return
        return original_off(self)

    @functools.wraps(original_finalize)
    def finalize(self):
        if self.switch is None:
            return
        return original_finalize(self)

    tracker.off = off
    tracker.finalize = finalize
    tracker._fcweb_idempotent_cleanup = True
    print("[fcweb] Draft tracker cleanup is idempotent")
    return True


class _Loader(importlib.abc.Loader):
    def __init__(self, original):
        self.original = original

    def create_module(self, spec):
        creator = getattr(self.original, "create_module", None)
        return creator(spec) if creator else None

    def exec_module(self, module):
        self.original.exec_module(module)
        # Do not catch failures: a changed API must remain visible to the caller.
        repair_module(module)

    def __getattr__(self, name):
        return getattr(self.original, name)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != _TARGET:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and spec.loader is not None:
            spec.loader = _Loader(spec.loader)
        return spec


def install():
    """Repair an existing import or hook the first normal import, once."""
    if _TARGET in sys.modules:
        repair_module(sys.modules[_TARGET])
    if not any(isinstance(finder, _Finder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _Finder())
