#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (c) FreeCAD-Web contributors
"""Fetch the reviewed FreeCAD/submodule source archives, without replacing existing trees."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PINS = json.loads((Path(__file__).with_name('native-source-pins.json')).read_text())

def fetch(name, tree, archives=None):
    pin = PINS[name]
    stamp = tree / '.fcweb-source-pin.json'
    if (tree / pin['marker']).exists():
        if not stamp.is_file() or json.loads(stamp.read_text()) != pin:
            raise RuntimeError(f'{tree}: existing source has no matching source pin; preserve it and use a clean build checkout')
        print(f'{name}: existing source marker matches {pin["commit"]}; later patch-content checks still apply')
        return
    tree.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='fcweb-pinned-', dir=tree.parent) as tmp:
        temp = Path(tmp)
        archive = temp / 'source.tar.gz'
        if archives:
            source = Path(archives) / (name + '.tar.gz')
            shutil.copyfile(source, archive)
        else:
            with urllib.request.urlopen(pin['url'], timeout=120) as response, archive.open('wb') as output:
                shutil.copyfileobj(response, output)
        with archive.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != pin['sha256'] or archive.stat().st_size != pin['bytes']:
            raise RuntimeError(f'{name}: downloaded archive does not match reviewed bytes/hash')
        unpack = temp / 'unpack'
        unpack.mkdir()
        with tarfile.open(archive, 'r:gz') as source:
            source.extractall(unpack, filter='data')
        children = list(unpack.iterdir())
        if len(children) != 1 or not children[0].is_dir() or not (children[0] / pin['marker']).is_file():
            raise RuntimeError(f'{name}: unexpected archive structure')
        if tree.exists():
            if any(tree.iterdir()):
                raise RuntimeError(f'{tree}: refusing to overwrite existing content')
            tree.rmdir()
        children[0].rename(tree)
        stamp.write_text(json.dumps(pin, indent=2) + '\n')
    print(f'{name}: verified archive SHA256 {pin["sha256"]}, source {pin["commit"]}')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['freecad', 'submodules'])
    parser.add_argument('tree', nargs='?', default='deps/src/freecad')
    parser.add_argument('--archives', help='offline reviewed archives named freecad/gsl/ondsel/addonmanager.tar.gz')
    args = parser.parse_args()
    tree = Path(args.tree)
    if not tree.is_absolute():
        tree = ROOT / tree
    if args.stage == 'freecad':
        version = ''.join(line.split('#', 1)[0].strip() for line in (ROOT / 'patches/freecad.version').read_text().splitlines())
        if version != PINS['freecad']['version']:
            raise RuntimeError('patches/freecad.version differs from the reviewed source pins')
        fetch('freecad', tree, args.archives)
    else:
        if not (tree / 'CMakeLists.txt').is_file():
            raise RuntimeError(f'{tree}: FreeCAD source is missing')
        for name in ['gsl', 'ondsel', 'addonmanager']:
            fetch(name, tree / PINS[name]['subdir'], args.archives)

if __name__ == '__main__':
    main()
