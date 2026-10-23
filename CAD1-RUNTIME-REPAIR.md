<!-- SPDX-License-Identifier: LGPL-2.1-or-later -->
<!-- Copyright (c) Virtastic -->
# CAD1 runtime repair, 2026-10-23

The current browser engine exposes two distinct failures. Draft rectangle
cleanup can hide a tracker after `finalize()` has cleared its Coin switch.
Separately, Qt accessibility's `getActiveElement_js` is a `jj` Wasm import on
wasm64 but returns the Number produced by `Emval.toHandle`. Returning that value
to i64 throws `Cannot convert ... to a BigInt` while linking accessibility DOM.
The rectangle attempt preceded optional screen-reader activation; the two fixes
do not assert that one failure caused the other or that these alone explain an
incorrect dimension entered through automation.

## Repair and scope

- `play-gui/am/fcweb_draft_tracker_fix.py` lazily wraps Draft's tracker cleanup.
  An already removed switch can be hidden or finalized again. Live Coin errors
  propagate. Numeric input and geometry creation are unchanged. `sitecustomize`
  installs the hook before Draft imports, without importing the GUI early.
- `tools/patch-freecad-js.py` converts the accessibility handle to BigInt when
  the existing undefined-handle argument is BigInt. wasm32 keeps Number returns.
  Focus traversal, shadow DOM traversal, handle ownership and accessibility
  remain enabled. No exception is silenced.
- `patches/freecad.patch` carries the tracker guards into future engine builds.
  `patches/qt-wasm-accessibility-emval.patch` carries the same return-width fix
  into future Qt builds. The current pinned engine can use the two small
  overlays without a Qt rebuild.

Primary source references:
[FreeCAD 1.1.3 tracker](https://github.com/FreeCAD/FreeCAD/blob/1.1.3/src/Mod/Draft/draftguitools/gui_trackers.py),
[Qt 6.11.2 accessibility](https://github.com/qt/qtbase/blob/v6.11.2/src/plugins/platforms/wasm/qwasmaccessibility.cpp).

## Focused verification

```sh
python3 tools/patch-freecad-js.py --selftest
python3 tools/test-cad1-runtime-fixes.py --engine-js /path/to/original/FreeCAD.js
python3 tools/check-page-python.py
python3 tools/check-every-patch-is-applied.py
```

The optional engine argument verifies the observed unpatched `jj` import. The
test reproduces its Number-to-i64 failure across a real Wasm boundary, checks
fixed wasm64/wasm32 returns with ordinary, nested-shadow and absent focus, and
checks repeated tracker cleanup plus preservation of genuine errors. CI sets
up Node explicitly for the Wasm fixture. These checks are separate from the
student keyboard workflow.

For a staged browser check, use a **new isolated browser context** with service
workers blocked. Serve the patched page, patched JS, `am/sitecustomize.py` and
`am/fcweb_draft_tracker_fix.py` together from one origin. Verify boot, the Draft
repair log, actual numeric rectangle coordinates and dimensions, outline,
save/download/reopen, screen-reader activation, the resulting accessible
controls, focus/property edits and absence of runtime errors. Preserve existing
student IndexedDB and cached projects.

## Deployment and future rebuild order

The inherited `deploy-ovh.yml` names `freecad.virtastic.app`; it is not evidence
that a classroom host or fork is deployed. Identify the actual authorized host,
keep recoverable originals, deploy the page and both Python overlay files with
the patched engine JS, and update the JS cache stamp. Keep the pinned Wasm/data
pair unchanged for this overlay repair. Do not clear student browser storage.

After deployment, download the exact live JS URL exposed by the refreshed page
and run this on those **untouched downloaded bytes**:

```sh
python3 tools/patch-freecad-js.py /path/to/downloaded-live-FreeCAD.js --check
```

Do not run the mutating patch command on that live readback before checking it:
doing so would prove only that a local copy can be repaired. Compare deployed
page/Python asset hashes and repeat the student browser gate against the real
host. Ordinary source CI instead applies the incoming table to a temporary
downloaded copy and checks compatibility; it makes no deployment-state claim.

For a later full engine rebuild, **finish `build-qt-wasm.yml` successfully
before starting or rerunning `build-freecad.yml`, `build-python-deps.yml` or
`link-freecad.yml` at the same commit**. The Qt producer and all three consumers
use the same base key plus `hashFiles('patches/qt-*.patch')`; old Qt caches cannot
satisfy a changed patch set. Their push workflows are independent and can start
out of order. A consumer that starts before the new Qt cache exists must be
rerun after Qt succeeds; a failed early consumer is not a successful rebuild.
Qt patch-only changes trigger all affected workflows. Check the final engine
and browser gate before publishing or deploying a rebuilt engine.
