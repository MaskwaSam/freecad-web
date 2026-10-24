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
# Exact reviewed output bridges: original CPP/Qt, plus the successful 4d Python
# output across quoted link-choice and evidence-retention changes only. Original
# producer manifests and native output bytes remain authoritative.
# CPP/Qt bridge: output retention and Python-only Pivy
# Freetype discovery/order and static Ifc wrapper. The current global conservative fingerprint changes;
# original CPP/Qt manifests/output bytes and their executed build paths are retained.
# These boundaries come from code, never
# from its separately retained evidence policy. Existing recipe globs stay intact.
COMPATIBILITY_FILE = 'native-artifact-output-compatibility.json'
COMPATIBILITY_SOURCE = 'a2649e165b168bfbec16827fc5379e9e712316ed'
COMPATIBILITY_RECIPE = '812765ae50b7868410a2376b99fc8332ed8f30abc29d21e2f0222446fb1b67f8'
COMPATIBILITY_GIT_TREE = '4764b677272ad3fb236358696e6518b85f45ab2d301ce5d6bd6e81f35fd014a5'
COMPATIBILITY_RUNS = {'cpp': '40016791813', 'qt': '40016804746'}
COMPATIBILITY_RECIPE_PATHS = {'tools/native-input-bundle.py', '.github/workflows/link-freecad.yml', 'configure-pivy-weh.sh', '.github/workflows/build-python-deps.yml', 'configure-ifcopenshell-weh.sh'}
PYTHON_COMPATIBILITY_FILE = 'native-python-output-compatibility.json'
COMPATIBILITY_GIT_PATHS = COMPATIBILITY_RECIPE_PATHS | {
    COMPATIBILITY_FILE, PYTHON_COMPATIBILITY_FILE, 'play-gui/am/fcweb_share.py', 'play-gui/freecad-gui.html'}
PYTHON_COMPATIBILITY_SOURCE = '4d36689e9667e9f2503c434a8630b590940353d1'
PYTHON_COMPATIBILITY_RUN = '40043926298'
PYTHON_COMPATIBILITY_RECIPE = '79d786cd5caed55e08554eaff4c78bcd24d4c7fe7ca17a61f50f1861968220aa'
PYTHON_COMPATIBILITY_GIT_TREE = 'ec55a52701ceda114ce8c1415d53412dfa73d141c33cf5afbb0734d7e2e71303'
PYTHON_COMPATIBILITY_RECIPE_PATHS = {'tools/native-input-bundle.py', '.github/workflows/link-freecad.yml'}
PYTHON_COMPATIBILITY_GIT_PATHS = PYTHON_COMPATIBILITY_RECIPE_PATHS | {COMPATIBILITY_FILE, PYTHON_COMPATIBILITY_FILE}
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


def recipe_acceptance(kind, manifest, consumer_commit=None):
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
    selected, policies = {}, {}
    for kind in kinds:
        file = workspace / '.native-restored' / (kind + '.manifest.json')
        manifest = json.loads(file.read_text())
        if digest(file) != PRODUCER_MANIFEST_SHA256[kind]:
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
    return {'policies': policies, 'producers': selected}


def receipts(workspace, kinds):
    result = {}
    for kind in kinds:
        receipt = json.loads((workspace / '.native-restored' / (kind + '.json')).read_text())
        if receipt['manifest_file'] != kind + '.manifest.json':
            raise RuntimeError('producer receipt has an unexpected manifest path')
        file = workspace / '.native-restored' / receipt['manifest_file']
        manifest = json.loads(file.read_text())
        if (digest(file) != receipt['manifest_sha256'] or digest(file) != PRODUCER_MANIFEST_SHA256[kind] or
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


def unpack(kind, folder, workspace, run):
    raw_manifest = folder / 'manifest.json'
    if digest(raw_manifest) != PRODUCER_MANIFEST_SHA256[kind]:
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
        if manifest['dependencies'] != selected:
            raise RuntimeError('Python was built against different C++/Qt producer artifacts')
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
    for policy_name in [COMPATIBILITY_FILE, PYTHON_COMPATIBILITY_FILE]:
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
