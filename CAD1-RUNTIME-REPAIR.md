<!-- SPDX-License-Identifier: LGPL-2.1-or-later -->
<!-- Copyright (c) Virtastic -->
# CAD1 runtime repair, 2026-10-23

The current browser runtime exposes three distinct failures. Draft rectangle
cleanup can hide a tracker after `finalize()` has cleared its Coin switch.
Separately, Qt accessibility's `getActiveElement_js` is a `jj` Wasm import on
wasm64 but returns the Number produced by `Emval.toHandle`. Returning that value
to i64 throws `Cannot convert ... to a BigInt` while linking accessibility DOM.
The rectangle attempt preceded optional screen-reader activation. After that
activation, the page's keyboard forwarder can send a second copy of keys that
already bubble from an accessibility button to Qt. An actual property edit from
41 to 40 became 4400: the trace records both synthetic canvas and original
trusted button keys delivered to the same Qt handler. These findings do not
establish the cause of the separately observed native pointer memory trap.

## Repair and scope

- `play-gui/am/fcweb_draft_tracker_fix.py` lazily wraps Draft's tracker cleanup.
  An already removed switch can be hidden or finalized again. Live Coin errors
  propagate. Numeric input and geometry creation are unchanged. `sitecustomize`
  installs the hook before Draft imports, without importing the GUI early.
- `tools/patch-freecad-js.py` converts the accessibility handle to BigInt when
  the existing undefined-handle argument is BigInt. wasm32 keeps Number returns.
  Focus traversal, shadow DOM traversal, handle ownership and accessibility
  remain enabled. No exception is silenced.
- The page forwards keys only when their composed event path does not already
  contain a Qt window. Accessibility controls and direct canvas events keep
  their original route, while keys outside Qt retain the existing shortcut
  fallback. The guard neither cancels nor redispatches an original Qt key.
- `patches/freecad.patch` carries the tracker guards into future engine builds.
  `patches/qt-wasm-accessibility-emval.patch` carries the same return-width fix
  into future Qt builds. The current pinned engine can use the two small
  overlays without a Qt rebuild.

Primary source references:
[FreeCAD 1.1.3 tracker](https://github.com/FreeCAD/FreeCAD/blob/1.1.3/src/Mod/Draft/draftguitools/gui_trackers.py),
[Qt 6.11.2 accessibility](https://github.com/qt/qtbase/blob/v6.11.2/src/plugins/platforms/wasm/qwasmaccessibility.cpp).
Qt registers bubbling keyboard listeners on each
[Qt window](https://github.com/qt/qtbase/blob/v6.11.2/src/plugins/platforms/wasm/qwasmwindow.cpp).

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
checks repeated tracker cleanup plus preservation of genuine errors. Routing
checks exercise the recorded button case, keydown/up, direct canvas and shadow
paths, text focus, synthetic recursion, and the outside-Qt shortcut fallback.
The routing test fails against the previous page with two deliveries instead
of one. CI sets up Node explicitly for these checks. They remain separate from
the student keyboard workflow.

For a staged browser check, use a **new isolated browser context**. Serve the
patched page, patched JS, `am/sitecustomize.py` and
`am/fcweb_draft_tracker_fix.py` together from one origin. Verify boot, the Draft
repair log, actual numeric rectangle coordinates and dimensions, outline,
save/download/reopen, screen-reader activation, the resulting accessible
controls, focus/property edits and absence of runtime errors. In particular,
repeat two property edits with accessibility enabled, commit the second edit,
click the view without an intervening model probe, and only then read the exact
dimension. Preserve original key and pointer traces. A negative pointer-only
test does not clear an edit-triggered crash. Route-based previews must retain
origin-isolation headers and return successful asset statuses; a live gate
uses normal service workers with no response interception. Preserve existing
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
