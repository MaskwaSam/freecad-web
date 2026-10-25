#!/bin/bash
# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (c) FreeCAD-Web contributors
# Preserve source/module scope; fetch the reviewed immutable submodule archives.
set -e
cd "$(dirname "$0")/.."
exec python3 tools/fetch-pinned-freecad.py submodules "${1:-deps/src/freecad}"
