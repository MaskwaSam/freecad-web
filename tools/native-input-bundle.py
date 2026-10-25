#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
"""Clean hosted native producers and fail-closed, complete artifact handoffs."""
import argparse
import fnmatch
import glob
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile

SOURCE = Path(__file__).resolve().parents[1]
PYTHON_PATHS = '''deps/wasm/lib/numpy-mod
deps/wasm/lib/mpl-mod
deps/wasm/lib/kiwi-mod
deps/wasm/lib/ctypes-mod
deps/wasm/lib/pil-mod
deps/wasm/lib/ifc-mod
deps/wasm/lib/pivy-mod
deps/wasm/lib/libffi.a
deps/src/cpython
deps/src/numpy
deps/src/matplotlib
deps/wasm/include/freetype2
deps/src/pivy
deps/src/ifcopenshell
deps/src/pyside-setup
deps/src/eigen
build-numpy
build-pivy-wasm
build-ifcopenshell
deps/host/shiboken6
deps/host/llvm-*
deps/wasm/pyside-pkg
deps/wasm/shiboken6
build-shiboken-wasm
build-pyside-wasm'''.splitlines()
PATHS = {'cpp': ['deps/wasm'], 'qt': ['qt/6.11.2/wasm_mt_weh'], 'python': PYTHON_PATHS}
WORKFLOWS = {'cpp': '.github/workflows/build-deps.yml', 'qt': '.github/workflows/build-qt-wasm.yml',
             'python': '.github/workflows/build-python-deps.yml', 'link': '.github/workflows/link-freecad.yml'}
# Deliberately conservative: every tracked tool, config, patch, workflow, and root
# recipe is an input, including output-pruning helpers. Add/remove is also hashed.
RECIPE_GLOBS = ['tools/**', 'toolchain/**', 'patches/**', '.github/workflows/**', 'bridge/**', 'ci/**', 'infra/**', '*.sh', '*.cmake',
                '*.meson.in', '*requirements*.txt', 'CMakeLists.txt', 'pyproject.toml', 'package*.json']
RESERVE_BYTES = 1024 ** 3
PYTHON_BUILD_DISTRIBUTIONS = ['pip', 'meson', 'ninja', 'pybind11', 'setuptools', 'wheel', 'cppy', 'cython', 'swig']
# Exact reviewed output bridges: original CPP and successful 4d Python,
# separately from a fresh repaired Qt producer. Original manifests, dependencies
# and producer bytes remain authoritative; no producer identity is rewritten.
# CPP bridge: output retention and Python-only Pivy
# Freetype discovery/order and static Ifc wrapper. The current global conservative fingerprint changes;
# original CPP/Qt manifests/output bytes and their executed build paths are retained.
# These boundaries come from code, never
# from its separately retained evidence policy. Existing recipe globs stay intact.
COMPATIBILITY_FILE = 'native-artifact-output-compatibility.json'
COMPATIBILITY_SOURCE = 'a2649e165b168bfbec16827fc5379e9e712316ed'
COMPATIBILITY_RECIPE = '812765ae50b7868410a2376b99fc8332ed8f30abc29d21e2f0222446fb1b67f8'
COMPATIBILITY_GIT_TREE = '4764b677272ad3fb236358696e6518b85f45ab2d301ce5d6bd6e81f35fd014a5'
COMPATIBILITY_RUNS = {'cpp': '40016791813'}
QT_PRODUCER_REBUILD_RECIPE_PATHS = {'patches/qt-wasm-zz-pointer-lifetime-v2-r3.patch', 'tools/native-input-bundle.py', '.github/workflows/link-freecad.yml', 'configure-pivy-weh.sh', '.github/workflows/build-python-deps.yml', 'configure-ifcopenshell-weh.sh'}
COMPATIBILITY_RECIPE_PATHS = QT_PRODUCER_REBUILD_RECIPE_PATHS | {'patches/freecad.patch'}
PYTHON_COMPATIBILITY_FILE = 'native-python-output-compatibility.json'
COMPATIBILITY_GIT_PATHS = COMPATIBILITY_RECIPE_PATHS | {
    COMPATIBILITY_FILE, PYTHON_COMPATIBILITY_FILE, 'native-qt-screen-identity-compatibility.json', 'play-gui/am/fcweb_share.py', 'play-gui/freecad-gui.html'}
PYTHON_COMPATIBILITY_SOURCE = '4d36689e9667e9f2503c434a8630b590940353d1'
PYTHON_COMPATIBILITY_RUN = '40043926298'
PYTHON_COMPATIBILITY_RECIPE = '79d786cd5caed55e08554eaff4c78bcd24d4c7fe7ca17a61f50f1861968220aa'
PYTHON_COMPATIBILITY_GIT_TREE = 'ec55a52701ceda114ce8c1415d53412dfa73d141c33cf5afbb0734d7e2e71303'
PYTHON_COMPATIBILITY_RECIPE_PATHS = {'patches/freecad.patch', 'patches/qt-wasm-zz-pointer-lifetime-v2-r3.patch', 'tools/native-input-bundle.py', '.github/workflows/link-freecad.yml'}
PYTHON_COMPATIBILITY_GIT_PATHS = PYTHON_COMPATIBILITY_RECIPE_PATHS | {COMPATIBILITY_FILE, PYTHON_COMPATIBILITY_FILE, 'native-qt-screen-identity-compatibility.json'}
# Exact original manifests remain authoritative. Neither a matching current recipe
# nor an independently self-consistent replacement manifest grants admission.
PRODUCER_MANIFEST_SHA256 = {
    'cpp': '9e02433d1d31ad462477faa3bc60072bd251c1af0a17007dd5c540c02e1805be',
    'qt': 'e943855d100ebfaa02be7c95558c2cd6c64749c51fb0a517307b0a824badf703',
    'python': '6b70cd52b23cf4a50858ff0a19d85d5fa0932d83b03fafbd51f9c23976d6e6a0'}
PRODUCER_MANIFEST_CANONICAL_SHA256 = {
    'cpp': '161e6008ee267403afbe2626d7b7d7412f9b9a7f4d1bedbaf77bcda8c46cfd02',
    'qt': '53a4a259a3f74a8006e2f8a59d0ade65fc6f5f530ed36fcee855275d9104aa79',
    'python': 'bc065f41671bbefeda503b06175625642dd38c65ba6a2d1d94a6f3c507746244'}
PYTHON_PRODUCER_POLICY_SHA256 = 'b5fc512b247eab67b5e31d82aa9227254f5dfb7f7bc68dcfb51cad59ae2e973b'
PYTHON_PRODUCER_COMPATIBILITY_SHA256 = 'a61c312767a433bb8b5e54ee3abb49e37fbe8d81d43654cea3b7d6bd75b3ea31'


# Repaired Qt is a distinct producer. These output pins intentionally remain
# null in the source-only draft; no producer is admitted until actual successful
# run/artifact/manifest evidence has been independently checked and pinned here.
QT_COMPATIBILITY_FILE = 'native-qt-screen-identity-compatibility.json'
QT_PRODUCER_SOURCE = '847cdaa9d9db7fbe24595539805ee789a1af976b'
QT_PRODUCER_RUN = '40152646092'
QT_PRODUCER_RECIPE = '0b5133f0194a30b2911a6e217123c9fad9d380780f7b9a14b241cd62acf5b885'
QT_PRODUCER_GIT_TREE = 'be05239ccf970fc130937546f7555af4ded29108d7dc324cc99b0d38ed8a433f'
QT_PRODUCER_OUTPUT = {'producer_manifest_raw_sha256': '2eaa9eb9813d3124adb0f9e7ee0f5b633424637963f52257c5e5f11b7dec8c56', 'producer_manifest_raw_bytes': 4944979, 'producer_manifest_canonical_sha256': '98a3ab19334f04beca9ee854448f22c354dc43ed059d1a210f193d6e1cd686b4', 'producer_archive_sha256': 'abedceebb02a3c72b711695ab4b39d0e969f2725455605ec7c7e48a4df27e288', 'producer_archive_bytes': 65744050, 'producer_artifact_id': '12583348593', 'producer_artifact_zip_sha256': 'c137229330f42c6953c90ed1e398b83e0daf1c090f4fbfc7fd0d1efe364ab23d', 'producer_artifact_zip_bytes': 70689279}
QT_CONSUMER_RECIPE_PATHS = {'patches/freecad.patch', 'tools/native-input-bundle.py'}
QT_CONSUMER_GIT_PATHS = QT_CONSUMER_RECIPE_PATHS | {
    COMPATIBILITY_FILE, PYTHON_COMPATIBILITY_FILE, QT_COMPATIBILITY_FILE}
QT_ORIGINAL_RECEIPT_SHA256 = '91301e9f470c3df0fdd2170e7ee64137394348bfef11eb45672cc980a8135355'
QT_ORIGINAL_RECEIPT_CANONICAL_SHA256 = 'e2436840370ee17263bee177f36322b95682ab79bab269fa49e7d350143f1b88'
QT_PRIVATE_PATCH = 'patches/qt-wasm-zz-pointer-lifetime-v2-r3.patch'
QT_PRIVATE_PATCH_BEFORE = '98a8a1eadc75737d8cb545506c76fb25dfe504dc1d12c75a96276acc0f0bfbc9'
QT_PRIVATE_PATCH_AFTER = '2b1836b0691567c3ffa5b66fcfdd78d3d1cb142aac26819ffbac686675e2761f'
QT_IMPLEMENTATION_ARCHIVE = 'qt/6.11.2/wasm_mt_weh/plugins/platforms/libqwasm.a'
QT_ABI_PREFIXES = ['include/', 'mkspecs/', 'lib/cmake/', 'metatypes/', 'modules/']
QT_ABI_SUFFIXES = ['.prl']
QT_SBOM_WITNESS_SHA256 = 'd633afbf0e25f29ef2ce14cb8d0c3a71cb8a6d2e5284aa47655bc09c34751380'
QT_SBOM_KEYS = ['_qt_sbom_cydx_bom_serial_number_uuid', '_qt_sbom_cydx_external_bom_link',
                '_qt_sbom_spdx_id', '_qt_sbom_spdx_repo_document_namespace',
                '_qt_sbom_spdx_v2_external_document_ref', '_qt_sbom_spdx_v2_external_spdx_id']


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':'))


def recipe(kind):
    if kind not in PATHS:
        raise RuntimeError('unknown native input kind')
    files = {Path(p) for pattern in RECIPE_GLOBS for p in glob.glob(str(SOURCE / pattern), recursive=True)
             if Path(p).is_file() and '__pycache__' not in Path(p).parts and not p.endswith('.pyc')}
    data = {str(path.relative_to(SOURCE)): digest(path) for path in sorted(files)}
    for required in ['tools/check-archive-target.py', 'tools/check-archive-eh.sh',
                     'tools/native-input-bundle.py', 'tools/native-toolchain-pins.json']:
        if required not in data:
            raise RuntimeError(f'recipe input is missing: {required}')
    return data, hashlib.sha256(canonical(data).encode()).hexdigest()


def disk_preflight(where, needed):
    free = shutil.disk_usage(where).free
    if free < needed + RESERVE_BYTES:
        raise RuntimeError(f'insufficient disk: need {needed + RESERVE_BYTES} bytes including reserve; have {free}')


def safe_rel(name):
    value = PurePosixPath(name)
    if not name or value.is_absolute() or '..' in value.parts or str(value) != name or '\\' in name:
        raise RuntimeError(f'unsafe/noncanonical archive path: {name}')
    return value


def resolve_paths(kind, workspace):
    included, omitted = [], []
    for pattern in PATHS[kind]:
        matches = sorted(Path(p) for p in glob.glob(str(workspace / pattern)))
        if not matches:
            if '*' in pattern:
                omitted.append(pattern)  # optional host LLVM; system LLVM is recorded separately
                continue
            raise RuntimeError(f'producer handoff incomplete: missing {pattern}')
        included.extend(str(path.relative_to(workspace)) for path in matches)
    return sorted(included), sorted(omitted)


def tree_manifest(root, included):
    entries = {}
    def add(path):
        rel = path.relative_to(root).as_posix()
        safe_rel(rel)
        st = path.lstat()
        mode = stat.S_IMODE(st.st_mode)
        if mode & ~0o777:
            raise RuntimeError(f'special permission bits are not portable: {rel}')
        entry = {'mode': mode}
        if stat.S_ISLNK(st.st_mode):
            entry.update(type='symlink', target=os.readlink(path))
            # Reject escaping links before packing and after staged extraction.
            if Path(entry['target']).is_absolute() or not path.resolve().is_relative_to(root.resolve()):
                raise RuntimeError(f'link escapes workspace: {rel}')
        elif stat.S_ISDIR(st.st_mode):
            entry['type'] = 'directory'
        elif stat.S_ISREG(st.st_mode):
            entry.update(type='file', bytes=st.st_size, sha256=digest(path))
        else:
            raise RuntimeError(f'unsupported native input member: {rel}')
        if rel in entries:
            raise RuntimeError(f'overlapping input roots: {rel}')
        entries[rel] = entry
        if entry['type'] == 'directory':
            for child in sorted(path.iterdir()):
                add(child)
    for rel in included:
        add(root / safe_rel(rel))
    return entries


def tar_manifest(archive, included):
    entries = {}
    with tarfile.open(archive, 'r:gz') as stream:
        for member in stream:
            name = str(safe_rel(member.name))
            if name in entries or not any(name == p or name.startswith(p + '/') for p in included):
                raise RuntimeError(f'duplicate or out-of-contract tar member: {name}')
            if member.mode & ~0o777:
                raise RuntimeError('special permission bits in tar')
            entry = {'mode': member.mode}
            if member.isfile():
                file = stream.extractfile(member)
                with file:
                    sha = hashlib.file_digest(file, 'sha256').hexdigest()
                entry.update(type='file', bytes=member.size, sha256=sha)
            elif member.isdir():
                entry['type'] = 'directory'
            elif member.issym():
                entry.update(type='symlink', target=member.linkname)
            elif member.islnk():
                safe_rel(member.linkname)
                if not any(member.linkname == p or member.linkname.startswith(p + '/') for p in included):
                    raise RuntimeError('hardlink target outside declared input roots')
                entry.update(type='hardlink', target=member.linkname)
            else:
                raise RuntimeError(f'unsupported tar member type: {name}')
            entries[name] = entry
    return entries


def event_contract(kind, event, workflow, event_name, runner_environment):
    expected = WORKFLOWS[kind]
    # workflow_ref includes owner/repo then workflow path then @ref.
    if not workflow.split('@', 1)[0].endswith('/' + expected) or event_name != 'workflow_dispatch':
        raise RuntimeError('native diagnostic inputs require the expected manually dispatched workflow')
    inputs = event.get('inputs', {})
    if runner_environment != 'github-hosted' or inputs.get('runner') != 'hosted' or str(inputs.get('rebuild')).lower() != 'true':
        raise RuntimeError('native diagnostic inputs require hosted runner and rebuild=true')
    if kind == 'python' and (inputs.get('lanes') != 'all' or any(not re.fullmatch(r'[0-9]+', inputs.get(k, '')) for k in ['cpp_run_id', 'qt_run_id'])):
        raise RuntimeError('clean Python producer requires lanes=all and explicit C++/Qt producer IDs')
    if kind == 'link' and (any(not re.fullmatch(r'[0-9]+', inputs.get(k, '')) for k in ['cpp_run_id', 'qt_run_id', 'python_run_id']) or inputs.get('ccache') != 'off'):
        raise RuntimeError('hosted diagnostic link requires all producer IDs and ccache=off')
    return inputs


def begin(kind, workspace):
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    inputs = event_contract(kind, event, os.environ['GITHUB_WORKFLOW_REF'], os.environ['GITHUB_EVENT_NAME'], os.environ['RUNNER_ENVIRONMENT'])
    # This runs immediately after checkout, before any native cache/restore step.
    dirty = [p.name for p in workspace.iterdir() if p.is_dir() and
             (p.name in ['deps', 'emsdk', 'qt', 'qt-src', 'qt-build', 'qt-host', 'freecad-gui-install', 'weh-objs'] or p.name.startswith('build-'))]
    if dirty:
        raise RuntimeError(f'clean hosted producer has preexisting native trees: {dirty}')
    origin = {'mode': 'clean-hosted-rebuild', 'workflow': WORKFLOWS[kind], 'event': 'workflow_dispatch',
              'runner_environment': 'github-hosted', 'inputs': inputs, 'run_id': os.environ['GITHUB_RUN_ID'],
              'run_attempt': os.environ['GITHUB_RUN_ATTEMPT'], 'source_commit': os.environ['GITHUB_SHA'],
              'repository': os.environ['GITHUB_REPOSITORY'], 'kind': kind, 'cache_restored': False}
    (workspace / '.native-build-origin.json').write_text(json.dumps(origin, indent=2) + '\n')


def check_run(kind, run, expected_id, repository):
    if (str(run.get('id')) != expected_id or run.get('path') != WORKFLOWS[kind] or
        run.get('event') != 'workflow_dispatch' or run.get('status') != 'completed' or run.get('conclusion') != 'success' or
        run.get('repository', {}).get('full_name') != repository or not re.fullmatch('[0-9a-f]{40}', run.get('head_sha', ''))):
        raise RuntimeError('selected producer is not the expected successful workflow_dispatch run in this repository')
    return run['head_sha']


def validate_origin(kind, origin, run=None):
    inputs = event_contract(kind, {'inputs': origin['inputs']}, 'repo/' + origin['workflow'] + '@ref', origin['event'], origin['runner_environment'])
    if origin['kind'] != kind or origin['mode'] != 'clean-hosted-rebuild' or origin['cache_restored'] is not False:
        raise RuntimeError('artifact does not certify a clean hosted rebuild')
    if run and (str(run['id']) != origin['run_id'] or str(run['run_attempt']) != origin['run_attempt'] or
                run['head_sha'] != origin['source_commit'] or run['repository']['full_name'] != origin['repository']):
        raise RuntimeError('artifact origin does not match selected producer attempt')
    return inputs


def identity_hash(data):
    return hashlib.sha256(canonical(data).encode()).hexdigest()


def git_tree(commit):
    raw = subprocess.check_output(['git', 'ls-tree', '-r', '-z', commit], cwd=SOURCE)
    return {entry.split(b'\t', 1)[1].decode(): entry.split(b'\t', 1)[0].decode()
            for entry in raw.split(b'\0') if entry}


def checkout_commit():
    return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=SOURCE, text=True).strip()


def qt_policy():
    policy = json.loads((SOURCE / QT_COMPATIBILITY_FILE).read_text())
    return qt_policy_data(policy, digest(SOURCE / QT_PRIVATE_PATCH))


def qt_policy_data(policy, private_patch_sha256):
    if policy['status'] != 'EXACT_QT_PRODUCER_PINNED' or any(
            value is None for value in QT_PRODUCER_OUTPUT.values()):
        raise RuntimeError('new Qt producer output pins are pending; no Qt/dependency bridge is admitted')
    expected = dict(QT_PRODUCER_OUTPUT, producer_source_commit=QT_PRODUCER_SOURCE,
                    producer_run_id=QT_PRODUCER_RUN, producer_run_attempt='1',
                    producer_repository='MaskwaSam/freecad-web')
    if (policy['format'] != 1 or any(policy.get(key) != value for key, value in expected.items()) or
        any(policy.get(key) is not False for key in ['runtime_acceptance', 'student_acceptance', 'live_acceptance']) or
        policy['producer_recipe_sha256'] != QT_PRODUCER_RECIPE or
        len(policy['producer_recipe_files']) != 204 or
        identity_hash(policy['producer_recipe_files']) != QT_PRODUCER_RECIPE or
        policy['producer_git_tree_sha256'] != QT_PRODUCER_GIT_TREE or
        identity_hash(policy['producer_git_tree']) != QT_PRODUCER_GIT_TREE or
        policy['private_patch_path'] != QT_PRIVATE_PATCH or
        policy['private_patch_before_sha256'] != QT_PRIVATE_PATCH_BEFORE or
        policy['private_patch_after_sha256'] != QT_PRIVATE_PATCH_AFTER or
        policy['producer_recipe_files'][QT_PRIVATE_PATCH] != QT_PRIVATE_PATCH_AFTER or
        private_patch_sha256 != QT_PRIVATE_PATCH_AFTER or
        policy['implementation_archive'] != QT_IMPLEMENTATION_ARCHIVE or
        policy['public_abi_inventory_prefixes'] != QT_ABI_PREFIXES or
        policy['public_abi_inventory_suffixes'] != QT_ABI_SUFFIXES):
        raise RuntimeError('new Qt producer policy differs from exact reviewed source/output pins')
    original_qt_evidence(policy)
    return policy


def producer_manifest_sha256(kind):
    return qt_policy()['producer_manifest_raw_sha256'] if kind == 'qt' else PRODUCER_MANIFEST_SHA256[kind]


def original_qt_evidence(policy):
    raw = policy['original_qt_manifest_utf8'].encode('utf-8')
    manifest = json.loads(raw)
    receipt_raw = policy['original_qt_receipt_utf8'].encode('utf-8')
    receipt = json.loads(receipt_raw)
    if (policy['original_qt_manifest_raw_sha256'] != PRODUCER_MANIFEST_SHA256['qt'] or
        policy['original_qt_manifest_raw_bytes'] != 4944949 or
        policy['original_qt_manifest_canonical_sha256'] != PRODUCER_MANIFEST_CANONICAL_SHA256['qt'] or
        policy['original_qt_receipt_sha256'] != QT_ORIGINAL_RECEIPT_SHA256 or
        policy['original_qt_receipt_canonical_sha256'] != QT_ORIGINAL_RECEIPT_CANONICAL_SHA256 or
        hashlib.sha256(raw).hexdigest() != PRODUCER_MANIFEST_SHA256['qt'] or len(raw) != 4944949 or
        identity_hash(manifest) != PRODUCER_MANIFEST_CANONICAL_SHA256['qt'] or
        hashlib.sha256(receipt_raw).hexdigest() != QT_ORIGINAL_RECEIPT_SHA256 or
        identity_hash(receipt) != QT_ORIGINAL_RECEIPT_CANONICAL_SHA256 or
        receipt != make_receipt('qt', manifest, PRODUCER_MANIFEST_SHA256['qt']) or
        manifest['source_commit'] != COMPATIBILITY_SOURCE or
        manifest['build_origin']['run_id'] != '40016804746' or manifest['build_origin']['run_attempt'] != '1' or
        manifest['recipe_sha256'] != COMPATIBILITY_RECIPE or
        identity_hash(manifest['recipe_files']) != COMPATIBILITY_RECIPE or
        manifest['recipe_files'][QT_PRIVATE_PATCH] != QT_PRIVATE_PATCH_BEFORE):
        raise RuntimeError('original Python Qt dependency manifest/receipt differs from immutable original bytes')
    return manifest, receipt


def qt_recipe_acceptance(manifest, consumer_commit=None):
    policy = qt_policy()
    current, fingerprint = recipe('qt')
    origin = manifest['build_origin']
    if (manifest['format'] != 3 or manifest['kind'] != 'qt' or
        manifest['source_commit'] != QT_PRODUCER_SOURCE or origin['source_commit'] != QT_PRODUCER_SOURCE or
        origin['run_id'] != QT_PRODUCER_RUN or origin['run_attempt'] != '1' or
        origin['repository'] != 'MaskwaSam/freecad-web' or
        identity_hash(manifest) != policy['producer_manifest_canonical_sha256'] or
        manifest['archive_sha256'] != policy['producer_archive_sha256'] or
        manifest['archive_bytes'] != policy['producer_archive_bytes'] or
        manifest['recipe_sha256'] != QT_PRODUCER_RECIPE or
        manifest['recipe_files'] != policy['producer_recipe_files'] or
        policy['new_recipe_sha256'] != fingerprint or policy['new_recipe_files'] != current or
        set(current) != set(policy['producer_recipe_files'])):
        raise RuntimeError('Qt differs from exact repaired producer/complete manifest/consumer recipe')
    validate_origin('qt', origin)
    changes = {p: {'before': policy['producer_recipe_files'][p], 'after': current[p]}
               for p in current if policy['producer_recipe_files'][p] != current[p]}
    if set(changes) != QT_CONSUMER_RECIPE_PATHS or policy['reviewed_recipe_changes'] != changes:
        raise RuntimeError('repaired Qt consumer changes exceed exact admission helper')
    original, _ = original_qt_evidence(policy)
    rebuild_changes = {p for p in original['recipe_files']
                       if original['recipe_files'][p] != manifest['recipe_files'].get(p)}
    if (set(original['recipe_files']) != set(manifest['recipe_files']) or
        rebuild_changes != QT_PRODUCER_REBUILD_RECIPE_PATHS or
        any(manifest['recipe_files'][p] != current[p]
            for p in current if p not in QT_CONSUMER_RECIPE_PATHS)):
        raise RuntimeError('Qt rebuild source changed outside exact previously reviewed paths/private comparison')
    baseline, live = policy['producer_git_tree'], git_tree('HEAD')
    changed = {p for p in set(baseline) | set(live) if baseline.get(p) != live.get(p)}
    if changed != QT_CONSUMER_GIT_PATHS:
        raise RuntimeError('repaired Qt consumer Git tree differs outside exact admission files')
    for path in changed:
        blob = subprocess.check_output(['git', 'hash-object', '--', path], cwd=SOURCE, text=True).strip()
        if live.get(path) not in ('100644 blob ' + blob, '100755 blob ' + blob):
            raise RuntimeError('repaired Qt consumer file differs from its committed source')
    return {'format': 1, 'kind': 'qt', 'run_id': QT_PRODUCER_RUN, 'run_attempt': '1',
            'producer_source_commit': QT_PRODUCER_SOURCE,
            'consumer_source_commit': consumer_commit or checkout_commit(),
            'producer_recipe_sha256': QT_PRODUCER_RECIPE, 'consumer_recipe_sha256': fingerprint,
            'producer_manifest_canonical_sha256': identity_hash(manifest),
            'policy_file': QT_COMPATIBILITY_FILE, 'policy_sha256': digest(SOURCE / QT_COMPATIBILITY_FILE),
            'reviewed_recipe_changes': changes, 'changed_git_paths': sorted(changed)}


def qt_abi_inventory(manifest):
    root = 'qt/6.11.2/wasm_mt_weh/'
    inventory = {p: entry for p, entry in manifest['tree'].items()
                 if p.startswith(root) and
                 (any(p.startswith(root + prefix) for prefix in QT_ABI_PREFIXES) or
                  any(p.endswith(suffix) for suffix in QT_ABI_SUFFIXES))}
    if (any(not any(p.startswith(root + prefix) for p in inventory) for prefix in QT_ABI_PREFIXES) or
        not any(p.endswith('.prl') for p in inventory) or
        any(e['type'] not in ['file', 'directory'] for e in inventory.values())):
        raise RuntimeError('Qt public header/configuration inventory is incomplete or has unexpected links/types')
    return inventory


def qt_abi_source_identity_bridge(original, fresh, policy):
    # These immutable original source bytes prove a finite generated SBOM-only
    # delta. This never edits either producer archive or ignores a CMake hash.
    witness = policy['cmake_sbom_source_identity_witness']
    if (identity_hash(witness) != QT_SBOM_WITNESS_SHA256 or
        set(witness) != {'format', 'scope', 'old_source_commit', 'new_source_commit',
                         'property_keys', 'expected_file_count', 'expected_record_count',
                         'expected_changed_line_count', 'original_raw_bytes', 'modules', 'files'} or
        witness['format'] != 1 or
        witness['scope'] != 'exact-generated-Qt-CMake-SBOM-source-identity-properties-only' or
        witness['old_source_commit'] != COMPATIBILITY_SOURCE or
        witness['new_source_commit'] != QT_PRODUCER_SOURCE or
        witness['property_keys'] != QT_SBOM_KEYS or
        (witness['expected_file_count'], witness['expected_record_count'],
         witness['expected_changed_line_count'], witness['original_raw_bytes']) != (225, 233, 1398, 1968946)):
        raise RuntimeError('Qt CMake SBOM witness differs from the exact reviewed source identity proof')
    old_inventory, selected_inventory = qt_abi_inventory(original), qt_abi_inventory(fresh)
    files, modules = witness['files'], witness['modules']
    root = 'qt/6.11.2/wasm_mt_weh/lib/cmake/'
    expected_paths = {p for p, e in old_inventory.items() if p.startswith(root) and
                      p.endswith('Targets.cmake') and 'Versionless' not in PurePosixPath(p).name and
                      e['type'] == 'file'}
    if (set(files) != expected_paths or len(files) != 225 or
        set(modules) != {'qtbase', 'qtdeclarative', 'qtimageformats', 'qtlanguageserver',
                         'qtshadertools', 'qtsvg', 'qttools'} or
        set(old_inventory) != set(selected_inventory)):
        raise RuntimeError('Qt CMake SBOM witness path/module inventory differs')
    module_fields = {'old_uuid', 'new_uuid', 'old_document_ref', 'new_document_ref',
                     'old_namespace', 'new_namespace', 'record_count'}
    uuid_pattern = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
    for module, tokens in modules.items():
        if (set(tokens) != module_fields or type(tokens['record_count']) is not int or tokens['record_count'] < 1 or
            any(not re.fullmatch(uuid_pattern, tokens[k]) for k in ['old_uuid', 'new_uuid']) or
            tokens['old_uuid'] == tokens['new_uuid'] or
            any(not re.fullmatch('DocumentRef-' + module + '-[0-9a-f]{12}', tokens[k])
                for k in ['old_document_ref', 'new_document_ref']) or
            tokens['old_document_ref'] == tokens['new_document_ref'] or
            not tokens['old_namespace'].startswith('https://qt.io/spdxdocs/' + module + '-' + COMPATIBILITY_SOURCE[:7] + '+') or
            tokens['old_namespace'].count(COMPATIBILITY_SOURCE[:7]) != 1 or
            tokens['old_namespace'].replace(COMPATIBILITY_SOURCE[:7], QT_PRODUCER_SOURCE[:7]) != tokens['new_namespace']):
            raise RuntimeError('Qt CMake SBOM module identity mapping differs')
    block_pattern = re.compile(rb'^set_target_properties\(([^()\r\n ]+) PROPERTIES\n(.*?)^\)\n', re.M | re.S)
    expected_inventory = dict(old_inventory)
    records = changed_lines = total_bytes = 0
    module_counts = {module: 0 for module in modules}
    for path in sorted(files):
        if set(files[path]) != {'original_utf8'}:
            raise RuntimeError('Qt CMake SBOM original source witness has unexpected fields')
        raw = files[path]['original_utf8'].encode('utf-8')
        entry = old_inventory[path]
        if (set(entry) != {'mode', 'type', 'bytes', 'sha256'} or entry['type'] != 'file' or
            type(entry['mode']) is not int or entry['mode'] & ~0o777 or
            entry['bytes'] != len(raw) or entry['sha256'] != hashlib.sha256(raw).hexdigest()):
            raise RuntimeError('Qt CMake SBOM original source bytes differ from original manifest')
        output = bytearray()
        cursor = 0
        file_records = 0
        for block in block_pattern.finditer(raw):
            body = block[2]
            values = {key: re.findall(rb'^  ' + key.encode() + rb' "([^"\r\n]*)"\n', body, re.M)
                      for key in QT_SBOM_KEYS}
            if not any(values.values()):
                continue
            if any(len(value) != 1 for value in values.values()):
                raise RuntimeError('Qt CMake SBOM record has missing or duplicate identity properties')
            values = {key: value[0].decode('ascii') for key, value in values.items()}
            projects = re.findall(rb'^  _qt_sbom_spdx_repo_project_name_lowercase "([^"\r\n]*)"\n', body, re.M)
            if len(projects) != 1 or projects[0].decode('ascii') not in modules:
                raise RuntimeError('Qt CMake SBOM record has unknown module identity')
            module = projects[0].decode('ascii')
            tokens = modules[module]
            old_id = values['_qt_sbom_spdx_id']
            old_suffix = tokens['old_document_ref'].rsplit('-', 1)[1]
            new_suffix = tokens['new_document_ref'].rsplit('-', 1)[1]
            if not re.fullmatch('SPDXRef-Package-' + module + '-[A-Za-z0-9_.+-]+-' + old_suffix, old_id):
                raise RuntimeError('Qt CMake SBOM package identity differs from its module')
            new_id = old_id[:-len(old_suffix)] + new_suffix
            old_values = dict(zip(QT_SBOM_KEYS, [tokens['old_uuid'],
                'urn:cdx:' + tokens['old_uuid'] + '/1#' + old_id, old_id,
                tokens['old_namespace'], tokens['old_document_ref'], tokens['old_document_ref'] + ':' + old_id]))
            new_values = dict(zip(QT_SBOM_KEYS, [tokens['new_uuid'],
                'urn:cdx:' + tokens['new_uuid'] + '/1#' + new_id, new_id,
                tokens['new_namespace'], tokens['new_document_ref'], tokens['new_document_ref'] + ':' + new_id]))
            if values != old_values:
                raise RuntimeError('Qt CMake SBOM UUID/link/package/document identity record is incoherent')
            replacement = block[0]
            for key in QT_SBOM_KEYS:
                before = ('  ' + key + ' "' + old_values[key] + '"\n').encode('ascii')
                after = ('  ' + key + ' "' + new_values[key] + '"\n').encode('ascii')
                if replacement.count(before) != 1 or before == after:
                    raise RuntimeError('Qt CMake SBOM identity substitution is not exact and unique')
                replacement = replacement.replace(before, after, 1)
            output.extend(raw[cursor:block.start()])
            output.extend(replacement)
            cursor = block.end()
            file_records += 1
            module_counts[module] += 1
        output.extend(raw[cursor:])
        expected = bytes(output)
        # No unparsed instance of the six admitted properties may be ignored.
        if (file_records < 1 or any(len(re.findall(rb'^  ' + key.encode() + rb' ', raw, re.M)) != file_records
                                   for key in QT_SBOM_KEYS) or
            len(expected) != len(raw) or len(expected.splitlines()) != len(raw.splitlines())):
            raise RuntimeError('Qt CMake SBOM source record coverage/structure differs')
        line_changes = sum(a != b for a, b in zip(raw.splitlines(keepends=True), expected.splitlines(keepends=True)))
        if line_changes != file_records * len(QT_SBOM_KEYS):
            raise RuntimeError('Qt CMake SBOM substitution changed an unexpected source line')
        expected_inventory[path] = dict(entry, sha256=hashlib.sha256(expected).hexdigest())
        records += file_records
        changed_lines += line_changes
        total_bytes += len(raw)
    if (records != 233 or changed_lines != 1398 or total_bytes != 1968946 or
        module_counts != {m: v['record_count'] for m, v in modules.items()} or
        expected_inventory != selected_inventory):
        raise RuntimeError('Qt public headers/configuration differ outside exact generated SBOM source identity properties')
    return {'format': 1, 'scope': witness['scope'], 'witness_sha256': QT_SBOM_WITNESS_SHA256,
            'original_inventory_sha256': identity_hash(old_inventory),
            'selected_inventory_sha256': identity_hash(selected_inventory),
            'source_identity_normalized_inventory_sha256': identity_hash(old_inventory),
            'normalization_direction': 'selected-generated-SBOM-identities-to-original-source-identities',
            'original_source_bytes': total_bytes, 'changed_files': len(files), 'records': records,
            'changed_property_lines': changed_lines, 'module_record_counts': module_counts,
            'all_other_header_configuration_bytes_unchanged': True}


def python_qt_dependency_compatibility(workspace, manifest, selected, consumer_commit=None):
    policy = qt_policy()
    file = workspace / '.native-restored/qt.manifest.json'
    fresh = json.loads(file.read_text())
    if digest(file) != producer_manifest_sha256('qt'):
        raise RuntimeError('selected repaired Qt original manifest bytes differ')
    qt_acceptance = qt_recipe_acceptance(fresh, consumer_commit)
    return python_qt_dependency_data(manifest, fresh, digest(file), selected, qt_acceptance,
                                     policy, digest(SOURCE / QT_COMPATIBILITY_FILE))


def python_qt_dependency_data(manifest, fresh, fresh_raw_sha256, selected, qt_acceptance, policy, policy_sha256):
    # Pure complete-manifest/data validation core, reusable by separately pinned
    # offline artifact validators after their own exact source/policy checks.
    qt_policy_data(policy, QT_PRIVATE_PATCH_AFTER)
    original, original_receipt = original_qt_evidence(policy)
    expected_qt_acceptance = {
        'format': 1, 'kind': 'qt', 'run_id': QT_PRODUCER_RUN, 'run_attempt': '1',
        'producer_source_commit': QT_PRODUCER_SOURCE,
        'consumer_source_commit': qt_acceptance.get('consumer_source_commit'),
        'producer_recipe_sha256': QT_PRODUCER_RECIPE,
        'consumer_recipe_sha256': policy['new_recipe_sha256'],
        'producer_manifest_canonical_sha256': QT_PRODUCER_OUTPUT['producer_manifest_canonical_sha256'],
        'policy_file': QT_COMPATIBILITY_FILE, 'policy_sha256': policy_sha256,
        'reviewed_recipe_changes': policy['reviewed_recipe_changes'],
        'changed_git_paths': sorted(QT_CONSUMER_GIT_PATHS)}
    if (not re.fullmatch('[0-9a-f]{40}', str(qt_acceptance.get('consumer_source_commit'))) or
        qt_acceptance != expected_qt_acceptance):
        raise RuntimeError('selected Qt source/output acceptance sidecar differs from exact bridge policy')
    if (identity_hash(manifest) != PRODUCER_MANIFEST_CANONICAL_SHA256['python'] or
        manifest['dependencies'] != {'cpp': selected['cpp'], 'qt': original_receipt} or
        identity_hash(manifest['recipe_compatibility']) != PYTHON_PRODUCER_COMPATIBILITY_SHA256):
        raise RuntimeError('original Python dependencies/bundle were rewritten or CPP differs')
    if (fresh_raw_sha256 != QT_PRODUCER_OUTPUT['producer_manifest_raw_sha256'] or
        identity_hash(fresh) != QT_PRODUCER_OUTPUT['producer_manifest_canonical_sha256'] or
        fresh['source_commit'] != QT_PRODUCER_SOURCE or
        fresh['build_origin']['source_commit'] != QT_PRODUCER_SOURCE or
        fresh['build_origin']['run_id'] != QT_PRODUCER_RUN or fresh['build_origin']['run_attempt'] != '1' or
        selected['qt'] != make_receipt('qt', fresh, fresh_raw_sha256) or selected['qt'] == original_receipt):
        raise RuntimeError('selected repaired Qt receipt/complete manifest differs or original Qt was substituted')
    if (original['workspace'] != fresh['workspace'] or original['platform'] != fresh['platform'] or
        original['machine'] != fresh['machine'] or
        any(original['input_identities'][key] != fresh['input_identities'][key]
            for key in ['emsdk', 'host_qt', 'qt_source'])):
        raise RuntimeError('repaired Qt changed original workspace/platform/upstream/toolchain/configuration authority')
    original_abi, fresh_abi = qt_abi_inventory(original), qt_abi_inventory(fresh)
    sbom_proof = qt_abi_source_identity_bridge(original, fresh, policy)
    plugin = QT_IMPLEMENTATION_ARCHIVE
    if (original['tree'][plugin]['type'] != 'file' or fresh['tree'][plugin]['type'] != 'file' or
        original['tree'][plugin]['sha256'] == fresh['tree'][plugin]['sha256']):
        raise RuntimeError('repaired Qt Wasm platform archive does not contain distinct rebuilt implementation bytes')
    compare_input_identities('python', manifest['input_identities'], selected, ['emsdk', 'host_qt'])
    return {'format': 1, 'scope': 'original-Python-old-Qt-to-repaired-private-implementation-Qt-only',
            'python_rebuilt': False, 'python_original_dependencies_unchanged': True,
            'runtime_acceptance': False, 'student_acceptance': False, 'live_acceptance': False,
            'python_manifest_sha256': PRODUCER_MANIFEST_SHA256['python'],
            'python_manifest_canonical_sha256': identity_hash(manifest),
            'python_original_dependencies': manifest['dependencies'],
            'python_producer_recipe_compatibility_sha256': PYTHON_PRODUCER_COMPATIBILITY_SHA256,
            'python_producer_output_policy_sha256': PYTHON_PRODUCER_POLICY_SHA256,
            'original_qt_receipt': original_receipt, 'selected_qt_receipt': selected['qt'],
            'cpp_receipt': selected['cpp'], 'public_header_configuration_entries': len(original_abi),
            'public_header_configuration_sha256': identity_hash(original_abi),
            'selected_public_header_configuration_sha256': identity_hash(fresh_abi),
            'cmake_sbom_source_identity_proof': sbom_proof,
            'implementation_archive': plugin,
            'original_implementation_sha256': original['tree'][plugin]['sha256'],
            'selected_implementation_sha256': fresh['tree'][plugin]['sha256'],
            'selected_qt_recipe_compatibility': qt_acceptance,
            'policy_file': QT_COMPATIBILITY_FILE, 'policy_sha256': policy_sha256}


def retain_original_qt_dependency(workspace, manifest):
    policy = qt_policy()
    directory = workspace / '.native-restored/python-original-dependencies'
    directory.mkdir(exist_ok=True)
    (directory / 'qt.manifest.json').write_bytes(policy['original_qt_manifest_utf8'].encode('utf-8'))
    (directory / 'qt.json').write_bytes(policy['original_qt_receipt_utf8'].encode('utf-8'))
    # This is a deterministic view of the original bound field, not a claim that
    # a separate historical raw sidecar was downloaded/copied.
    (directory / 'qt.recipe-compatibility.original-field.json').write_text(
        json.dumps(manifest['recipe_compatibility']['producers']['qt'], indent=2) + '\n')


def verify_python_qt_dependency_evidence(workspace, manifest, consumer_commit=None):
    selected = receipts(workspace, ['cpp', 'qt'])
    expected = python_qt_dependency_compatibility(workspace, manifest, selected, consumer_commit)
    directory = workspace / '.native-restored/python-original-dependencies'
    policy = qt_policy()
    if ((directory / 'qt.manifest.json').read_bytes() != policy['original_qt_manifest_utf8'].encode('utf-8') or
        (directory / 'qt.json').read_bytes() != policy['original_qt_receipt_utf8'].encode('utf-8') or
        (directory / 'qt.recipe-compatibility.original-field.json').read_text() !=
            json.dumps(manifest['recipe_compatibility']['producers']['qt'], indent=2) + '\n'):
        raise RuntimeError('retained original Python Qt dependency witness changed')
    sidecar = workspace / '.native-restored/python.qt-dependency-compatibility.json'
    if json.loads(sidecar.read_text()) != expected:
        raise RuntimeError('retained Python Qt dependency sidecar differs from exact selected/original producers')
    return expected


def recipe_acceptance(kind, manifest, consumer_commit=None):
    if kind == 'qt':
        return qt_recipe_acceptance(manifest, consumer_commit)
    current, fingerprint = recipe(kind)
    if manifest['format'] != 3 or manifest['kind'] != kind:
        raise RuntimeError('producer kind/format does not match this consumer')
    origin = manifest['build_origin']
    if kind == 'python':
        source, run_id = PYTHON_COMPATIBILITY_SOURCE, PYTHON_COMPATIBILITY_RUN
        producer_recipe, git_hash = PYTHON_COMPATIBILITY_RECIPE, PYTHON_COMPATIBILITY_GIT_TREE
        recipe_paths, git_paths = PYTHON_COMPATIBILITY_RECIPE_PATHS, PYTHON_COMPATIBILITY_GIT_PATHS
        policy_name = PYTHON_COMPATIBILITY_FILE
    elif kind in COMPATIBILITY_RUNS:
        source, run_id = COMPATIBILITY_SOURCE, COMPATIBILITY_RUNS[kind]
        producer_recipe, git_hash = COMPATIBILITY_RECIPE, COMPATIBILITY_GIT_TREE
        recipe_paths, git_paths = COMPATIBILITY_RECIPE_PATHS, COMPATIBILITY_GIT_PATHS
        policy_name = COMPATIBILITY_FILE
    else:
        raise RuntimeError('this diagnostic consumer permits only the exact reviewed CPP/Qt/Python producers')
    if (manifest['source_commit'] != source or origin['source_commit'] != source or origin['run_id'] != run_id or
        origin['run_attempt'] != '1' or origin['repository'] != 'MaskwaSam/freecad-web' or
        manifest['recipe_sha256'] != producer_recipe or len(manifest['recipe_files']) != 204 or
        identity_hash(manifest['recipe_files']) != producer_recipe or
        identity_hash(manifest) != PRODUCER_MANIFEST_CANONICAL_SHA256[kind]):
        raise RuntimeError('producer differs from the exact reviewed run, recipe, or complete original manifest')
    file = SOURCE / policy_name
    policy = json.loads(file.read_text())
    old = policy['old_recipe_files']
    if (policy['format'] != 1 or policy['old_recipe_sha256'] != producer_recipe or
        identity_hash(old) != producer_recipe or old != manifest['recipe_files'] or
        policy['new_recipe_sha256'] != fingerprint or policy['new_recipe_files'] != current or set(old) != set(current)):
        raise RuntimeError('output bridge evidence does not match the complete old/current recipe maps')
    changes = {p: {'before': old[p], 'after': current[p]} for p in old if old[p] != current[p]}
    if set(changes) != recipe_paths or policy['reviewed_recipe_changes'] != changes:
        raise RuntimeError('consumer bridge includes a recipe change outside the reviewed paths')
    baseline, live = policy['old_git_tree'], git_tree('HEAD')
    if identity_hash(baseline) != git_hash:
        raise RuntimeError('output bridge baseline Git tree differs from the reviewed producer tree')
    changed = {p for p in set(baseline) | set(live) if baseline.get(p) != live.get(p)}
    if changed != git_paths:
        raise RuntimeError('output bridge checkout has unexpected changed Git paths')
    for path in changed:
        blob = subprocess.check_output(['git', 'hash-object', '--', path], cwd=SOURCE, text=True).strip()
        if live.get(path) not in ('100644 blob ' + blob, '100755 blob ' + blob):
            raise RuntimeError('output bridge source/evidence differs from its consumer commit')
    if kind == 'python':
        producer_policy = policy['producer_output_policy_utf8'].encode('utf-8')
        if (policy['producer_source_commit'] != source or policy['producer_run_id'] != run_id or
            policy['producer_run_attempt'] != '1' or policy['producer_repository'] != 'MaskwaSam/freecad-web' or
            policy['producer_artifact_id'] != '12543085142' or
            policy['producer_artifact_zip_sha256'] != 'b0f82d4ee2cb2869b143d1ea16311c485e4b2eec1ebce7c5bf652849ed8b8342' or
            policy['producer_artifact_zip_bytes'] != 4085875311 or
            policy['producer_archive_sha256'] != manifest['archive_sha256'] or
            policy['producer_archive_bytes'] != manifest['archive_bytes'] or
            policy['producer_manifest_raw_bytes'] != 22472951 or
            policy['producer_manifest_raw_sha256'] != PRODUCER_MANIFEST_SHA256[kind] or
            policy['producer_manifest_canonical_sha256'] != PRODUCER_MANIFEST_CANONICAL_SHA256[kind] or
            hashlib.sha256(producer_policy).hexdigest() != PYTHON_PRODUCER_POLICY_SHA256 or
            policy['producer_output_policy_sha256'] != PYTHON_PRODUCER_POLICY_SHA256 or
            policy['producer_recipe_compatibility'] != manifest['recipe_compatibility'] or
            identity_hash(manifest['recipe_compatibility']) != PYTHON_PRODUCER_COMPATIBILITY_SHA256 or
            manifest['recipe_compatibility']['policy'] != json.loads(producer_policy) or
            manifest['recipe_compatibility']['policy_sha256'] != PYTHON_PRODUCER_POLICY_SHA256):
            raise RuntimeError('Python producer policy or original dependency bridge evidence changed')
    return {'format': 1, 'kind': kind, 'run_id': run_id, 'run_attempt': '1',
            'producer_source_commit': source, 'consumer_source_commit': consumer_commit or checkout_commit(),
            'producer_recipe_sha256': producer_recipe, 'consumer_recipe_sha256': fingerprint,
            'producer_manifest_canonical_sha256': identity_hash(manifest), 'policy_file': policy_name,
            'policy_sha256': digest(file), 'reviewed_recipe_changes': changes,
            'changed_git_paths': sorted(changed)}


def verify_original_python_evidence(workspace, manifest):
    policy = json.loads((SOURCE / PYTHON_COMPATIBILITY_FILE).read_text())
    expected_bundle = json.dumps(manifest['recipe_compatibility'], indent=2) + '\n'
    if (workspace / '.native-restored/python.producer-recipe-compatibility.json').read_text() != expected_bundle:
        raise RuntimeError('retained original Python dependency bridge bundle changed')
    original_policy = workspace / '.native-restored/python.producer-output-policy.json'
    if (original_policy.read_bytes() != policy['producer_output_policy_utf8'].encode('utf-8') or
        digest(original_policy) != PYTHON_PRODUCER_POLICY_SHA256):
        raise RuntimeError('retained original Python output policy bytes changed')


def compatibility_bundle(workspace, kinds, consumer_commit=None):
    selected, policies, dependency_compatibility = {}, {}, {}
    for kind in kinds:
        file = workspace / '.native-restored' / (kind + '.manifest.json')
        manifest = json.loads(file.read_text())
        if digest(file) != producer_manifest_sha256(kind):
            raise RuntimeError('retained producer original manifest bytes changed')
        acceptance = recipe_acceptance(kind, manifest, consumer_commit)
        sidecar = workspace / '.native-restored' / (kind + '.recipe-compatibility.json')
        if consumer_commit is None and json.loads(sidecar.read_text()) != acceptance:
            raise RuntimeError('output bridge sidecar differs from exact retained producer/current consumer')
        selected[kind] = acceptance
        policy_name = acceptance['policy_file']
        source = SOURCE / policy_name
        retained = workspace / '.native-restored' / policy_name
        if retained.read_bytes() != source.read_bytes():
            raise RuntimeError('retained output bridge evidence bytes differ from the current policy')
        policies[policy_name] = {'sha256': digest(source), 'policy': json.loads(source.read_text())}
        if kind == 'python':
            verify_original_python_evidence(workspace, manifest)
            dependency_compatibility['python_qt'] = verify_python_qt_dependency_evidence(workspace, manifest, consumer_commit)
    result = {'policies': policies, 'producers': selected}
    if dependency_compatibility:
        result['dependency_compatibility'] = dependency_compatibility
    return result


def receipts(workspace, kinds):
    result = {}
    for kind in kinds:
        receipt = json.loads((workspace / '.native-restored' / (kind + '.json')).read_text())
        if receipt['manifest_file'] != kind + '.manifest.json':
            raise RuntimeError('producer receipt has an unexpected manifest path')
        file = workspace / '.native-restored' / receipt['manifest_file']
        manifest = json.loads(file.read_text())
        if (digest(file) != receipt['manifest_sha256'] or digest(file) != producer_manifest_sha256(kind) or
            identity_hash(manifest['input_identities']) != receipt['input_identities_sha256']):
            raise RuntimeError('retained complete producer manifest/identity hash mismatch')
        expected = make_receipt(kind, manifest, receipt['manifest_sha256'])
        if receipt != expected:
            raise RuntimeError('producer receipt differs from its complete retained manifest')
        recipe_acceptance(kind, manifest)
        validate_identities(kind, receipt['input_identities'])
        result[kind] = receipt
    compatibility_bundle(workspace, kinds)
    return result


def make_receipt(kind, manifest, manifest_sha256):
    return {'kind': kind, 'run_id': manifest['build_origin']['run_id'], 'run_attempt': manifest['build_origin']['run_attempt'],
            'source_commit': manifest['source_commit'], 'archive_sha256': manifest['archive_sha256'],
            'recipe_sha256': manifest['recipe_sha256'], 'manifest_file': kind + '.manifest.json',
            'manifest_sha256': manifest_sha256, 'input_identities': manifest['input_identities'],
            'input_identities_sha256': identity_hash(manifest['input_identities'])}


def python_tool_versions(identities):
    tools = identities['python_build_tools']
    versions = tools['distribution_versions']
    if any(not isinstance(versions.get(name), str) or not versions[name] for name in PYTHON_BUILD_DISTRIBUTIONS):
        raise RuntimeError('required Python build tool version is missing')
    if versions['swig'] != '4.2.1':
        raise RuntimeError('Python build requires the existing SWIG 4.2.1 ABI choice')
    if any(tools['all_distributions'].get(name) != versions[name] for name in PYTHON_BUILD_DISTRIBUTIONS):
        raise RuntimeError('resolved Python build tool inventory is inconsistent')
    if any(not tools['commands'].get(name) for name in ['meson', 'ninja', 'cython', 'swig']):
        raise RuntimeError('actual Python build command identity is missing')
    return tools['all_distributions']


def compare_input_identities(kind, current, dependencies, components):
    if 'emsdk' in components:
        for producer, receipt in dependencies.items():
            if current['emsdk'] != receipt['input_identities']['emsdk']:
                raise RuntimeError(f'actual emsdk tools differ between {kind} and {producer}')
    if 'host_qt' in components:
        for producer in ['qt', 'python']:
            if producer in dependencies and current['host_qt'] != dependencies[producer]['input_identities']['host_qt']:
                raise RuntimeError(f'actual host Qt/aqt tools differ between {kind} and {producer}')
    if 'python_build_tools' in components:
        reference = dependencies['python']['input_identities']
        if (python_tool_versions(current) != python_tool_versions(reference) or
            current['python_build_tools']['commands'] != reference['python_build_tools']['commands']):
            raise RuntimeError('resolved Python build tool versions differ between Python producer and link')


def verify_inputs(kind, workspace, components=None):
    if kind not in ['python', 'link']:
        raise RuntimeError('only Python/link consume native producers')
    kinds = ['cpp', 'qt'] if kind == 'python' else ['cpp', 'qt', 'python']
    selected = receipts(workspace, kinds)
    current = json.loads((workspace / '.native-toolchain-inputs.json').read_text())
    required = components or (['emsdk', 'host_qt'] + (['python_build_tools'] if kind == 'link' else []))
    compare_input_identities(kind, current, selected, required)
    print(json.dumps({'kind': kind, 'compared_actual_input_components': required,
                      'producer_manifest_sha256': {k: v['manifest_sha256'] for k, v in selected.items()}}))


def validate_identities(kind, identities):
    pins = json.loads((SOURCE / 'tools/native-toolchain-pins.json').read_text())
    if not identities['emsdk'].get('tools'):
        raise RuntimeError('actual emsdk compiler/linker tool identities are missing')
    if identities['emsdk']['pin'] != pins['emsdk'] or identities['emsdk']['verified_commit'] != pins['emsdk']['commit']:
        raise RuntimeError('emsdk input identity differs from reviewed pin')
    if kind in ['qt', 'python', 'link']:
        host = identities['host_qt']
        wheel = host['aqt_wheel']
        if not host.get('tools') or host.get('aqt_distributions', {}).get('aqtinstall') != pins['aqtinstall']['version']:
            raise RuntimeError('actual host Qt/aqt dependency identities are missing')
        if (host['pin'] != pins['host_qt'] or host['verified_qt_version'] != pins['host_qt']['version'] or
            host['aqt_hash_check_enabled'] is not True or host['aqt_hash_algorithm'] != 'sha256' or not host['downloaded_archives'] or
            wheel['sha256'] != pins['aqtinstall']['sha256'] or wheel['bytes'] != pins['aqtinstall']['bytes']):
            raise RuntimeError('host Qt/aqt verified identity differs from reviewed input contract')
    if kind in ['python', 'link']:
        python_tool_versions(identities)
    if kind == 'qt':
        source = identities['qt_source']
        archive = source['verified_archive']
        if source['pin'] != pins['qt_source'] or archive['sha256'] != pins['qt_source']['sha256'] or archive['bytes'] != pins['qt_source']['bytes']:
            raise RuntimeError('Qt source verified identity differs from reviewed archive pin')


def pack(kind, folder, workspace):
    origin = json.loads((workspace / '.native-build-origin.json').read_text())
    validate_origin(kind, origin)
    if origin['source_commit'] != subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=SOURCE, text=True).strip():
        raise RuntimeError('producer checkout differs from its recorded origin')
    identities = json.loads((workspace / '.native-toolchain-inputs.json').read_text())
    required = ['host_environment', 'emsdk'] + (['host_qt'] if kind in ['qt', 'python'] else []) + (['qt_source'] if kind == 'qt' else [])
    if any(k not in identities for k in required):
        raise RuntimeError('producer toolchain/source identities are incomplete')
    validate_identities(kind, identities)
    if kind == 'python':
        verify_inputs(kind, workspace)
    folder.mkdir(parents=True, exist_ok=True)
    included, omitted = resolve_paths(kind, workspace)
    tree = tree_manifest(workspace, included)
    total = sum(e.get('bytes', 0) for e in tree.values())
    # gzip can expand incompressible input; reserve one full uncompressed copy + overhead.
    disk_preflight(folder, total + len(tree) * 2048 + total // 100)
    archive = folder / (kind + '.tar.gz')
    with tarfile.open(archive, 'w:gz', compresslevel=1, dereference=False) as target:
        for rel in included:
            target.add(workspace / rel, arcname=rel, recursive=True)
    members = tar_manifest(archive, included)
    if tree_manifest(workspace, included) != tree:
        raise RuntimeError('native input tree changed during packaging')
    recipes, fingerprint = recipe(kind)
    dependencies = receipts(workspace, ['cpp', 'qt']) if kind == 'python' else {}
    manifest = {'format': 3, 'kind': kind, 'source_commit': origin['source_commit'], 'build_origin': origin,
        'recipe_sha256': fingerprint, 'recipe_files': recipes, 'workspace': str(workspace.resolve()),
        'platform': platform.system(), 'machine': platform.machine(), 'included_paths': included, 'omitted_optional_globs': omitted,
        'archive': archive.name, 'archive_sha256': digest(archive), 'archive_bytes': archive.stat().st_size,
        'unpacked_bytes': total, 'members': members, 'tree': tree, 'input_identities': identities,
        'input_identities_sha256': identity_hash(identities), 'dependencies': dependencies,
        'recipe_compatibility': compatibility_bundle(workspace, ['cpp', 'qt']) if kind == 'python' else {}}
    (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'kind': kind, 'archive_bytes': manifest['archive_bytes'], 'archive_sha256': manifest['archive_sha256'],
        'build_origin': origin, 'recipe_sha256': fingerprint}))



def restore_stage_modes(stage, entries):
    """Undo only data-filter permission normalization in a private extracted tree."""
    ordinary = []
    for name, entry in entries.items():
        if not isinstance(name, str):
            raise RuntimeError('non-string extracted manifest path')
        parts = safe_rel(name).parts
        mode = entry.get('mode')
        if type(mode) is not int or not 0 <= mode <= 0o777:
            raise RuntimeError('extracted manifest mode is not an ordinary 0..0777 integer')
        kind = entry.get('type')
        if kind not in ['file', 'directory', 'symlink']:
            raise RuntimeError('unsupported extracted manifest type')
        if kind != 'symlink':
            ordinary.append((parts, kind, mode))
    # Files first, then deepest directories: restoring a restrictive parent mode
    # must not prevent descriptor traversal to one of its still-unchecked children.
    ordinary.sort(key=lambda item: (item[1] == 'directory', -len(item[0]), item[0]))
    root = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        root_info = os.fstat(root)
        if stat.S_IMODE(root_info.st_mode) != 0o700 or root_info.st_uid != os.geteuid():
            raise RuntimeError('mode restoration requires an owned private extraction stage')
        for parts, kind, mode in ordinary:
            parent = os.dup(root)
            try:
                for component in parts[:-1]:
                    child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    os.close(parent)
                    parent = child
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if kind == 'directory':
                    flags |= os.O_DIRECTORY
                target = os.open(parts[-1], flags, dir_fd=parent)
                try:
                    info = os.fstat(target)
                    if ((kind == 'file' and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)) or
                        (kind == 'directory' and not stat.S_ISDIR(info.st_mode))):
                        raise RuntimeError('extracted member type/link count differs before mode restoration')
                    os.fchmod(target, mode)
                    if stat.S_IMODE(os.fstat(target).st_mode) != mode:
                        raise RuntimeError('extracted member mode readback differs')
                finally:
                    os.close(target)
            finally:
                os.close(parent)
    finally:
        os.close(root)


def unpack(kind, folder, workspace, run):
    raw_manifest = folder / 'manifest.json'
    if digest(raw_manifest) != producer_manifest_sha256(kind):
        raise RuntimeError('producer original manifest bytes differ from the pinned complete artifact')
    manifest = json.loads(raw_manifest.read_text())
    acceptance = recipe_acceptance(kind, manifest)
    validate_origin(kind, manifest['build_origin'], run)
    validate_identities(kind, manifest['input_identities'])
    if manifest['input_identities_sha256'] != identity_hash(manifest['input_identities']):
        raise RuntimeError('producer input identity hash mismatch')
    if manifest['source_commit'] != run['head_sha']:
        raise RuntimeError('artifact source commit does not match selected producer')
    if manifest['workspace'] != str(workspace.resolve()) or manifest['platform'] != platform.system() or manifest['machine'] != platform.machine():
        raise RuntimeError('producer absolute workspace/platform differs; embedded CMake/build paths cannot be reused')
    included, omitted = manifest['included_paths'], manifest['omitted_optional_globs']
    if not isinstance(included, list) or sorted(set(included)) != included:
        raise RuntimeError('input roots must be unique and sorted')
    for p in included:
        safe_rel(p)
        if not any(fnmatch.fnmatchcase(p, pattern) for pattern in PATHS[kind]):
            raise RuntimeError('manifest has undeclared input root')
    for pattern in PATHS[kind]:
        matches = [p for p in included if fnmatch.fnmatchcase(p, pattern)]
        if not matches and not ('*' in pattern and pattern in omitted):
            raise RuntimeError('manifest omits required input root')
    if sorted(omitted) != sorted(p for p in PATHS[kind] if '*' in p and not any(fnmatch.fnmatchcase(x, p) for x in included)):
        raise RuntimeError('invalid optional omission list')
    if kind == 'python':
        selected = receipts(workspace, ['cpp', 'qt'])
        dependency_bridge = python_qt_dependency_compatibility(workspace, manifest, selected)
        # Its original bundle is pinned with the complete manifest. The current link
        # bridge is separate and must not rewrite the Python producer's b5fc policy.
        if identity_hash(manifest['recipe_compatibility']) != PYTHON_PRODUCER_COMPATIBILITY_SHA256:
            raise RuntimeError('Python original dependency bridge evidence changed')
        compare_input_identities(kind, manifest['input_identities'], selected, ['emsdk', 'host_qt'])
    archive = folder / (kind + '.tar.gz')
    if manifest['archive'] != archive.name or archive.stat().st_size != manifest['archive_bytes'] or digest(archive) != manifest['archive_sha256']:
        raise RuntimeError('producer archive bytes/hash mismatch')
    if tar_manifest(archive, included) != manifest['members']:
        raise RuntimeError('complete tar member manifest mismatch')
    declared_bytes = sum(e.get('bytes', 0) for e in manifest['tree'].values())
    if declared_bytes != manifest['unpacked_bytes']:
        raise RuntimeError('unpacked byte count mismatch')
    disk_preflight(workspace, declared_bytes + len(manifest['tree']) * 2048)
    # Complete extraction and full-tree/hash validation precede any workspace overlay.
    with tempfile.TemporaryDirectory(prefix='.native-restore-', dir=workspace) as temporary:
        stage = Path(temporary)
        with tarfile.open(archive, 'r:gz') as source:
            source.extractall(stage, filter='data')
        restore_stage_modes(stage, manifest['tree'])
        if tree_manifest(stage, included) != manifest['tree']:
            raise RuntimeError('full extracted native input tree mismatch')
        for rel in included:
            target = workspace / rel
            for parent in target.parents:
                if parent == workspace:
                    break
                if parent.is_symlink():
                    raise RuntimeError('workspace overlay parent is a symlink')
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(stage / rel), target)
    if tree_manifest(workspace, included) != manifest['tree']:
        raise RuntimeError('native overlay readback mismatch')
    destination = workspace / '.native-restored' / (kind + '.json')
    destination.parent.mkdir(exist_ok=True)
    retained = destination.parent / (kind + '.manifest.json')
    shutil.copyfile(folder / 'manifest.json', retained)
    receipt = make_receipt(kind, manifest, digest(retained))
    destination.write_text(json.dumps(receipt, indent=2) + '\n')
    sidecar = destination.parent / (kind + '.recipe-compatibility.json')
    if acceptance is not None:
        sidecar.write_text(json.dumps(acceptance, indent=2) + '\n')
        policy_name = acceptance['policy_file']
        shutil.copyfile(SOURCE / policy_name, destination.parent / policy_name)
        if kind == 'python':
            policy = json.loads((SOURCE / PYTHON_COMPATIBILITY_FILE).read_text())
            (destination.parent / 'python.producer-recipe-compatibility.json').write_text(
                json.dumps(manifest['recipe_compatibility'], indent=2) + '\n')
            (destination.parent / 'python.producer-output-policy.json').write_bytes(
                policy['producer_output_policy_utf8'].encode('utf-8'))
            retain_original_qt_dependency(workspace, manifest)
            (destination.parent / 'python.qt-dependency-compatibility.json').write_text(
                json.dumps(dependency_bridge, indent=2) + '\n')
    receipts(workspace, [kind])  # read back the retained complete manifest and identities
    print(json.dumps({'kind': kind, 'manifest_sha256': receipt['manifest_sha256'],
                      'input_identities_sha256': receipt['input_identities_sha256']}))


def bind_diagnostic(workspace, folder):
    pins = json.loads((SOURCE / 'tools/native-source-pins.json').read_text())
    destination = folder / 'source-pins'
    destination.mkdir(parents=True, exist_ok=True)
    tree = workspace / 'deps/src/freecad'
    for name in ['freecad', 'gsl', 'ondsel', 'addonmanager']:
        root = tree if name == 'freecad' else tree / pins[name]['subdir']
        receipt = root / '.fcweb-source-pin.json'
        if not (root / pins[name]['marker']).is_file() or json.loads(receipt.read_text()) != pins[name]:
            raise RuntimeError(f'actual source pin receipt differs from reviewed {name} input')
        shutil.copyfile(receipt, destination / (name + '.json'))
    shutil.copyfile(SOURCE / 'tools/native-source-pins.json', destination / 'expected-source-pins.json')
    shutil.copyfile(SOURCE / 'tools/native-toolchain-pins.json', destination / 'expected-toolchain-pins.json')
    compatibility_bundle(workspace, ['cpp', 'qt', 'python'])
    for policy_name in [COMPATIBILITY_FILE, PYTHON_COMPATIBILITY_FILE, QT_COMPATIBILITY_FILE]:
        shutil.copyfile(SOURCE / policy_name, folder / policy_name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['begin', 'check-run', 'pack', 'unpack', 'verify-inputs', 'bind-diagnostic'])
    parser.add_argument('kind', choices=list(WORKFLOWS))
    parser.add_argument('--folder', type=Path)
    parser.add_argument('--workspace', type=Path, default=Path.cwd())
    parser.add_argument('--run-json', type=Path)
    parser.add_argument('--run-id')
    parser.add_argument('--components', nargs='+', choices=['emsdk', 'host_qt', 'python_build_tools'])
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    if args.operation == 'begin':
        begin(args.kind, workspace)
    elif args.operation == 'check-run':
        run = json.loads(args.run_json.read_text())
        print(check_run(args.kind, run, args.run_id, os.environ['GITHUB_REPOSITORY']))
    elif args.operation == 'verify-inputs':
        verify_inputs(args.kind, workspace, args.components)
    elif args.operation == 'pack':
        pack(args.kind, args.folder or Path('native-inputs') / args.kind, workspace)
    elif args.operation == 'bind-diagnostic':
        if args.kind != 'link':
            raise RuntimeError('only link produces a bound native diagnostic')
        bind_diagnostic(workspace, args.folder or Path('native-diagnostic'))
    else:
        run = json.loads(args.run_json.read_text())
        check_run(args.kind, run, args.run_id, os.environ['GITHUB_REPOSITORY'])
        unpack(args.kind, args.folder or Path('native-inputs') / args.kind, workspace, run)

if __name__ == '__main__':
    main()
