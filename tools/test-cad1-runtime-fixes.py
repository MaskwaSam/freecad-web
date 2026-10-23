#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (c) Virtastic
"""Focused lifecycle and actual Wasm import regression checks.

    python3 tools/test-cad1-runtime-fixes.py [--engine-js /path/to/FreeCAD.js]

The optional engine file checks the observed production import rather than only
the pinned fixture. Neither these checks nor an API geometry check proves the
student keyboard workflow; exercise the corrected browser separately.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FIX = load("cad1_tracker_fix", ROOT / "play-gui/am/fcweb_draft_tracker_fix.py")
JS = load("cad1_js_patches", ROOT / "tools/patch-freecad-js.py")
ENTRY = next(p for p in JS.PATCHES if p[0] == "Qt accessibility: preserve the EM_VAL return width")
ENGINE = None


def legacy_module(delay=None):
    # The two original FreeCAD 1.1.3 methods, before the source guard patch.
    calls = []
    todo = types.SimpleNamespace(delay=delay or (lambda callback, switch: calls.append((callback, switch))))

    class Tracker:
        def __init__(self, switch):
            self.switch = switch
            self.Visible = True

        def off(self):
            self.switch.whichChild = -1
            self.Visible = False

        def finalize(self):
            todo.delay(self._removeSwitch, self.switch)
            self.switch = None

        def _removeSwitch(self, switch):
            pass

    return types.SimpleNamespace(Tracker=Tracker), calls


class TrackerTests(unittest.TestCase):
    def test_legacy_failure_is_reproduced(self):
        module, _ = legacy_module()
        tracker = module.Tracker(types.SimpleNamespace(whichChild=0))
        tracker.finalize()
        with self.assertRaises(AttributeError):
            tracker.off()

    def test_hide_live_tracker(self):
        module, calls = legacy_module()
        FIX.repair_module(module)
        switch = types.SimpleNamespace(whichChild=0)
        tracker = module.Tracker(switch)
        tracker.off()
        self.assertEqual(switch.whichChild, -1)
        self.assertFalse(tracker.Visible)
        self.assertEqual(calls, [])

    def test_finalize_then_repeated_finish(self):
        module, calls = legacy_module()
        FIX.repair_module(module)
        switch = types.SimpleNamespace(whichChild=0)
        tracker = module.Tracker(switch)
        tracker.finalize()
        tracker.off()
        tracker.finalize()
        tracker.off()
        self.assertIsNone(tracker.switch)
        self.assertFalse(tracker.Visible)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][1], switch)

    def test_invalid_live_switch_still_raises(self):
        module, _ = legacy_module()
        FIX.repair_module(module)
        tracker = module.Tracker(object())
        with self.assertRaises(AttributeError):
            tracker.off()

    def test_removal_failure_still_raises(self):
        def fail(*args):
            raise RuntimeError("real scene-graph failure")
        module, _ = legacy_module(fail)
        FIX.repair_module(module)
        switch = types.SimpleNamespace(whichChild=0)
        tracker = module.Tracker(switch)
        with self.assertRaisesRegex(RuntimeError, "real scene-graph failure"):
            tracker.finalize()
        self.assertIs(tracker.switch, switch)

    def test_install_is_lazy_and_idempotent(self):
        target = "draftguitools.gui_trackers"
        self.assertNotIn(target, sys.modules)
        previous = list(sys.meta_path)
        try:
            FIX.install()
            FIX.install()
            self.assertNotIn(target, sys.modules)
            self.assertEqual(sum(isinstance(f, FIX._Finder) for f in sys.meta_path), 1)
        finally:
            sys.meta_path[:] = previous

    def test_loader_patches_before_module_is_exposed(self):
        import importlib
        previous = list(sys.meta_path)
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "draftguitools"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "gui_trackers.py").write_text(
                "class Tracker:\n"
                " def off(self):\n  self.switch.whichChild = -1\n  self.Visible = False\n"
                " def finalize(self):\n  self.switch = None\n")
            sys.path.insert(0, directory)
            try:
                FIX.install()
                module = importlib.import_module("draftguitools.gui_trackers")
                tracker = module.Tracker()
                tracker.switch = None
                tracker.Visible = True
                tracker.off()
                self.assertFalse(tracker.Visible)
                self.assertFalse(FIX.repair_module(module))
            finally:
                sys.path.remove(directory)
                sys.meta_path[:] = previous
                sys.modules.pop("draftguitools.gui_trackers", None)
                sys.modules.pop("draftguitools", None)


WASM_TEST = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const data = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
// A real Wasm boundary: (i64)->i64 or (i32)->i32, forwarding to the JS import.
const vec = a => [a.length, ...a];
const str = s => vec([...Buffer.from(s)]);
const section = (id, bytes) => [id, ...vec(bytes)];
function caller(type, callback) {
  const bytes = new Uint8Array([
    0,97,115,109,1,0,0,0,
    ...section(1,[1,0x60,1,type,1,type]),
    ...section(2,[1,...str('env'),...str('active'),0,0]),
    ...section(3,[1,0]),
    ...section(7,[1,...str('call'),0,1]),
    ...section(10,[1,6,0,0x20,0,0x10,0,0x0b]),
  ]);
  return new WebAssembly.Instance(new WebAssembly.Module(bytes), {env:{active:callback}}).exports.call;
}
function setup(source, active, handle) {
  const context = {document:{activeElement:active}, Emval:{toHandle(value) {
    assert.equal(value, handle.element);
    handle.calls++;
    return handle.id;
  }}};
  vm.createContext(context);
  vm.runInContext(source, context);
  return context.getActiveElement_js;
}
const element = {name:'input'};
const before = setup(data.before, element, {element,id:562,calls:0});
assert.throws(() => caller(0x7e,before)(2n), TypeError);
let checks = 1;
for (const [type,undefinedHandle,expected] of [[0x7e,2n,562n],[0x7f,2,562]]) {
  for (const active of [element,{shadowRoot:{activeElement:element}}]) {
    const handle = {element,id:562,calls:0};
    const actual = caller(type,setup(data.after,active,handle))(undefinedHandle);
    assert.equal(actual,expected);
    assert.equal(handle.calls,1);
    checks++;
  }
  for (const active of [null,{shadowRoot:{activeElement:null}}]) {
    const handle = {element,id:562,calls:0};
    assert.equal(caller(type,setup(data.after,active,handle))(undefinedHandle),undefinedHandle);
    assert.equal(handle.calls,0);
    checks++;
  }
}
console.log(JSON.stringify({status:'PASS',wasm_abi_checks:checks,legacy_number_to_i64_failure_reproduced:true}));
'''


class AccessibilityTests(unittest.TestCase):
    def test_real_wasm_abi_including_shadow_dom_and_wasm32(self):
        before, after = ENTRY[1:3]
        if ENGINE:
            engine = Path(ENGINE).read_text()
            match = re.search(r'function getActiveElement_js\(undefHandle\).*?getActiveElement_js.sig="jj";', engine)
            self.assertIsNotNone(match, "exact pinned wasm64 accessibility import was not found")
            actual = match.group().split('getActiveElement_js.sig=')[0]
            self.assertEqual(actual, before, "test expects the observed unpatched current engine")
            self.assertEqual(engine.count(before), 1)
        result = subprocess.run(["node", "-e", WASM_TEST], input=json.dumps({"before":before,"after":after}),
                                text=True, capture_output=True, check=True)
        report = json.loads(result.stdout)
        self.assertEqual(report["wasm_abi_checks"], 9)
        print(result.stdout.strip())

    def test_patch_is_exact_idempotent_and_missing_site_is_visible(self):
        patched, status = JS.apply(ENTRY[1], counting=False)
        self.assertIn(ENTRY[2], patched)
        again, _ = JS.apply(patched, counting=False)
        self.assertEqual(patched, again)
        _, status = JS.apply("function unrelated(){}", counting=False)
        self.assertEqual(dict(status)[ENTRY[0]], "NOT FOUND")
        bad = JS.check_postconditions(ENTRY[1])
        self.assertTrue(any("accessibility" in item[0] for item in bad))


class RuntimeWiringTests(unittest.TestCase):
    def test_overlay_is_fetched_and_installed_before_draft_import(self):
        page = (ROOT / "play-gui/freecad-gui.html").read_text()
        site = (ROOT / "play-gui/am/sitecustomize.py").read_text()
        self.assertIn("'fcweb_draft_tracker_fix.py'", page)
        self.assertIn("import fcweb_draft_tracker_fix", site)
        self.assertIn("fcweb_draft_tracker_fix.install()", site)

    def test_qt_cache_producer_and_consumers_bind_all_qt_patches(self):
        base = "fcweb-qt-6.11.2-wasm64-eh-jspi-mt-emsdk6.0.9-v5"
        suffix = "-${{ hashFiles('patches/qt-*.patch') }}"
        for name in ["build-qt-wasm", "build-freecad", "link-freecad", "build-python-deps"]:
            source = (ROOT / (".github/workflows/" + name + ".yml")).read_text()
            self.assertIn("- 'patches/qt-*.patch'", source, name)
            if "QT_KEY:" in source:
                self.assertIn("QT_KEY: " + base, source, name)
                self.assertIn("key: ${{ env.QT_KEY }}" + suffix, source, name)
            else:
                self.assertIn("key: " + base + suffix, source, name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine-js")
    args, rest = parser.parse_known_args()
    ENGINE = args.engine_js
    unittest.main(argv=[sys.argv[0]] + rest, verbosity=2)
