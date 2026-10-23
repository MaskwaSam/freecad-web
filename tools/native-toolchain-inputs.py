#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
"""Install pinned native inputs and record the bytes/tools actually used."""
import argparse
import configparser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

SOURCE = Path(__file__).resolve().parents[1]
PINS = json.loads((SOURCE / 'tools/native-toolchain-pins.json').read_text())
PYTHON_BUILD_DISTRIBUTIONS = ['pip', 'meson', 'ninja', 'pybind11', 'setuptools', 'wheel', 'cppy', 'cython', 'swig']


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run(*args, cwd=None):
    subprocess.run([str(x) for x in args], cwd=cwd, check=True)


def output(*args, cwd=None):
    return subprocess.check_output([str(x) for x in args], cwd=cwd, text=True, stderr=subprocess.STDOUT).strip()


def verified_file(path, pin):
    if path.stat().st_size != pin['bytes'] or digest(path) != pin['sha256']:
        raise RuntimeError(f'pinned input byte size/SHA256 mismatch: {path.name}')
    return {'filename': path.name, 'bytes': path.stat().st_size, 'sha256': digest(path)}


def download(pin, directory):
    directory.mkdir(exist_ok=True, parents=True)
    path = directory / pin.get('filename', pin['url'].rsplit('/', 1)[1])
    if not path.exists():
        partial = path.with_name(path.name + '.part')
        try:
            with urllib.request.urlopen(pin['url'], timeout=90) as source, partial.open('wb') as dest:
                shutil.copyfileobj(source, dest)
            verified_file(partial, pin)
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
    verified_file(path, pin)
    return path


def record(workspace, kind, data):
    file = workspace / '.native-toolchain-inputs.json'
    all_inputs = json.loads(file.read_text()) if file.exists() else {}
    all_inputs[kind] = data
    file.write_text(json.dumps(all_inputs, indent=2) + '\n')


def distribution_versions(python):
    # Import metadata in the interpreter whose build environment is being measured.
    code = "import importlib.metadata,json,re; print(json.dumps({re.sub(r'[-_.]+','-',d.metadata['Name']).lower():d.version for d in importlib.metadata.distributions()},sort_keys=True))"
    versions = json.loads(output(python, '-c', code))
    for name, version in versions.items():
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', name) or not re.fullmatch(r'[A-Za-z0-9_.+!-]+', version):
            raise RuntimeError('invalid resolved package name/version')
    return versions


def install_versions(python, versions, wheel=None):
    # Replay the exact first producer resolution, including pip and transitives.
    if 'pip' not in versions:
        raise RuntimeError('producer package inventory omitted pip')
    run(python, '-m', 'pip', 'install', '--disable-pip-version-check', 'pip==' + versions['pip'])
    requirements = [name + '==' + version for name, version in sorted(versions.items()) if name != 'pip' and not (wheel and name == 'aqtinstall')]
    if wheel:
        requirements.append(str(wheel))
    run(python, '-m', 'pip', 'install', '--disable-pip-version-check', *requirements)
    if distribution_versions(python) != versions:
        raise RuntimeError('replayed package environment differs from producer resolved versions')


def reference_identities(workspace, kind):
    # Import the handoff validator so a modified receipt/manifest cannot seed installs.
    import importlib.util
    spec = importlib.util.spec_from_file_location('native_bundle', SOURCE / 'tools/native-input-bundle.py')
    bundle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bundle)
    return bundle.receipts(workspace, [kind])[kind]['input_identities']


def python_build_tools(workspace, python, required=False):
    versions = distribution_versions(python)
    selected = {name: versions.get(name) for name in PYTHON_BUILD_DISTRIBUTIONS}
    if required and any(version is None for version in selected.values()):
        raise RuntimeError('required Python build tool is missing from the measured interpreter')
    commands = {}
    for name, flag in [('meson', '--version'), ('ninja', '--version'), ('cython', '--version'), ('swig', '-version')]:
        path = Path(python).parent / name
        if not path.is_file():
            if required:
                raise RuntimeError(f'Python build environment has no {name} executable')
            commands[name] = None
            continue
        text = output(path, flag)
        commands[name] = {'version': text, 'bytes': path.stat().st_size, 'sha256': digest(path)}
        if name == 'swig' and '4.2.1' not in text and required:
            raise RuntimeError('actual SWIG executable differs from required version 4.2.1')
    data = {'python_version': output(python, '--version'), 'distribution_versions': selected,
            'all_distributions': versions, 'commands': commands}
    file = workspace / '.native-toolchain-inputs.json'
    previous = json.loads(file.read_text()).get('python_build_tools') if file.exists() else None
    if previous and previous != data:
        raise RuntimeError('Python build-tool environment changed after its pre-build measurement')
    record(workspace, 'python_build_tools', data)


def replay_python_tools(workspace, python):
    if (workspace / '.qtvenv').exists():
        raise RuntimeError('refusing existing link Python build environment')
    reference = reference_identities(workspace, 'python')['python_build_tools']
    run(python, '-m', 'venv', workspace / '.qtvenv')
    dest = workspace / '.qtvenv/bin/python'
    install_versions(dest, reference['all_distributions'])
    python_build_tools(workspace, dest, required=True)


def emcc_version_identity(text, expected):
    # emcc may emit first-use cache diagnostics before its actual version banner.
    # Match the complete real banner, never a version substring in diagnostics.
    pattern = re.compile(
        r'^emcc \(Emscripten gcc/clang-like replacement \+ linker emulating GNU ld\) '
        r'(?P<version>[0-9]+(?:\.[0-9]+){2}(?:[-+][0-9A-Za-z.-]+)?)'
        r'(?:[ \t]+\([^\r\n]*\))?[ \t]*$', re.MULTILINE)
    matches = list(pattern.finditer(text))
    if len(matches) != 1 or matches[0]['version'] != expected:
        observed = [match['version'] for match in matches[:4]]
        excerpt = text if len(text) <= 2048 else text[:1024] + '\n... output truncated ...\n' + text[-1024:]
        raise RuntimeError(
            f'installed emcc banner must identify exactly {expected}; observed versions={observed!r}; '
            f'observed output (bounded): {excerpt!r}')
    return {'version': matches[0]['version'], 'banner': matches[0].group(0).rstrip()}


def emsdk(workspace):
    pin = PINS['emsdk']
    dest = workspace / 'emsdk'
    if dest.exists():
        raise RuntimeError('refusing existing emsdk: clean install required for diagnostic inputs')
    run('git', 'init', dest)
    run('git', '-C', dest, 'remote', 'add', 'origin', pin['url'])
    run('git', '-C', dest, 'fetch', '--depth=1', 'origin', pin['commit'])
    run('git', '-C', dest, 'checkout', '--detach', 'FETCH_HEAD')
    if output('git', '-C', dest, 'rev-parse', 'HEAD') != pin['commit']:
        raise RuntimeError('emsdk checkout identity mismatch')
    tags = json.loads((dest / 'emscripten-releases-tags.json').read_text())
    if tags['releases'][pin['version']] != pin['release_hash']:
        raise RuntimeError('emsdk release map differs from reviewed pinned SDK')
    run(dest / 'emsdk', 'install', pin['version'], cwd=dest)
    run(dest / 'emsdk', 'activate', pin['version'], cwd=dest)
    # emsdk validates its package downloads. Record the selected release plus the
    # actual compiler/linker bytes rather than claiming the runner image is pinned.
    tools = {}
    for rel in ['upstream/emscripten/emcc', 'upstream/bin/clang',
                'upstream/bin/wasm-ld', 'upstream/bin/llvm-ar']:
        path = dest / rel
        if not path.is_file():
            raise RuntimeError(f'installed emsdk missing {rel}')
        tools[rel] = {'sha256': digest(path), 'bytes': path.stat().st_size}
    version_output = output('bash', '-c', 'set -e; export EMSDK_QUIET=1; . ./emsdk_env.sh >/dev/null; emcc --version', cwd=dest)
    version = emcc_version_identity(version_output, pin['version'])
    # Preserve diagnostics without making their incidental order part of the ABI
    # identity compared across clean C++/Qt/Python/link installations.
    record(workspace, 'emsdk_version_output', version_output)
    record(workspace, 'emsdk', {'pin': pin, 'verified_commit': pin['commit'], 'emcc_version': version['banner'],
                              'emcc_version_number': version['version'], 'tools': tools,
                              'release_map_sha256': digest(dest / 'emscripten-releases-tags.json')})


def host_qt(workspace, reference_kind=None):
    pin, host = PINS['aqtinstall'], PINS['host_qt']
    dest, venv = workspace / 'qt-host', workspace / '.native-aqt'
    if dest.exists() or venv.exists():
        raise RuntimeError('refusing existing host Qt/installer environment')
    wheel = download(pin, workspace / '.native-downloads')
    run(sys.executable, '-m', 'venv', venv)
    python = venv / 'bin/python'
    if reference_kind:
        reference = reference_identities(workspace, reference_kind)['host_qt']['aqt_distributions']
        install_versions(python, reference, wheel)
    else:
        run(python, '-m', 'pip', 'install', '--disable-pip-version-check', str(wheel))
    if output(python, '-c', 'import importlib.metadata; print(importlib.metadata.version("aqtinstall"))') != pin['version']:
        raise RuntimeError('installed aqt version differs from reviewed wheel')
    archives = workspace / '.native-host-archives'
    # Use the reviewed wheel's defaults, then explicitly force SHA256 checks.
    settings = configparser.ConfigParser(interpolation=None)
    with zipfile.ZipFile(wheel) as package:
        settings.read_string(package.read('aqt/settings.ini').decode())
    settings['requests']['hash_algorithm'] = 'sha256'
    settings['requests']['INSECURE_NOT_FOR_PRODUCTION_ignore_hash'] = 'False'
    settings['aqt']['baseurl'] = 'https://download.qt.io'
    config = workspace / '.native-aqt-settings.ini'
    with config.open('w') as stream:
        settings.write(stream)
    # Official metadata/hash checking stays enabled. Retained archives establish
    # exact downloaded host-tool identities, even when Qt updates mirror metadata.
    run(python, '-m', 'aqt', '--config', config, 'install-qt', host['host'], host['target'], host['version'], host['architecture'],
        '-m', *host['modules'], '-O', dest, '--keep', '--archive-dest', archives)
    archive_ids = {p.name: {'bytes': p.stat().st_size, 'sha256': digest(p)} for p in sorted(archives.iterdir()) if p.is_file()}
    if not archive_ids:
        raise RuntimeError('aqt did not retain any verified host Qt archives')
    root = dest / host['version'] / 'gcc_64'
    version = output(root / 'bin/qmake', '-query', 'QT_VERSION')
    if version != host['version']:
        raise RuntimeError('host Qt does not report the requested exact version')
    tools = {}
    for name in ['moc', 'rcc', 'uic', 'qsb']:
        candidates = [root / 'libexec' / name, root / 'bin' / name]
        path = next((p for p in candidates if p.is_file()), None)
        if not path:
            raise RuntimeError(f'host Qt missing {name}')
        version_text = output(path, '--version')
        if host['version'] not in version_text:
            raise RuntimeError(f'host Qt {name} version differs from requested exact version')
        tools[name] = {'path': str(path.relative_to(workspace)), 'version': version_text,
                       'sha256': digest(path), 'bytes': path.stat().st_size}
    record(workspace, 'host_qt', {'pin': host, 'aqt_wheel': verified_file(wheel, pin), 'verified_qt_version': version,
        'aqt_dependencies': output(python, '-m', 'pip', 'freeze').splitlines(),
        'aqt_distributions': distribution_versions(python), 'downloaded_archives': archive_ids,
        'tools': tools, 'aqt_hash_check_enabled': True, 'aqt_hash_algorithm': 'sha256', 'aqt_config_sha256': digest(config)})
    # SHA identities are retained; archive bytes can be reclaimed before the large native build.
    shutil.rmtree(archives)


def qt_source(workspace):
    pin = PINS['qt_source']
    dest = workspace / 'qt-src'
    if dest.exists():
        raise RuntimeError('refusing existing Qt source: clean verified extraction required')
    archive = download(pin, workspace / '.native-downloads')
    with tarfile.open(archive, 'r:xz') as stream:
        members = stream.getmembers()
        expected = 'qt-everywhere-src-' + pin['version']
        size = sum(m.size for m in members if m.isfile())
        if shutil.disk_usage(workspace).free < size + 1024 ** 3:
            raise RuntimeError('insufficient disk for complete Qt source extraction')
        for m in members:
            path = PurePosixPath(m.name)
            if path.is_absolute() or '..' in path.parts or path.parts[0] != expected:
                raise RuntimeError('Qt archive has an unexpected/unsafe member root')
        with tempfile.TemporaryDirectory(prefix='.native-qtsrc-', dir=workspace) as temporary:
            stream.extractall(temporary, filter='data')
            source = Path(temporary) / expected
            if not (source / 'configure').is_file():
                raise RuntimeError('Qt source archive lacks configure')
            source.rename(dest)
    record(workspace, 'qt_source', {'pin': pin, 'verified_archive': verified_file(archive, pin)})
    archive.unlink()  # verified source tree is now staged; retain identity, reclaim 1 GB


def environment(workspace):
    python = workspace / '.qtvenv/bin/python'
    python_build_tools(workspace, python if python.exists() else sys.executable, required=python.exists())
    tools = {}
    for tool in ['python3', 'cmake', 'ninja', 'gcc', 'g++', 'perl', 'git']:
        path = shutil.which(tool)
        if path:
            tools[tool] = {'path': path, 'version': output(path, '--version').splitlines()[:3], 'sha256': digest(Path(path))}
    record(workspace, 'host_environment', {'system': platform.platform(), 'tools': tools,
        'runner_image_os': os.environ.get('ImageOS'), 'runner_image_version': os.environ.get('ImageVersion')})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['emsdk', 'host-qt', 'qt-source', 'environment', 'python-build-tools', 'replay-python-tools'])
    parser.add_argument('--workspace', type=Path, default=Path.cwd())
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    parser.add_argument('--reference-kind', choices=['qt'])
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    if args.operation == 'host-qt':
        host_qt(workspace, args.reference_kind)
        return
    if args.operation == 'python-build-tools':
        python_build_tools(workspace, args.python, required=True)
        return
    if args.operation == 'replay-python-tools':
        replay_python_tools(workspace, args.python)
        return
    {'emsdk': emsdk, 'host-qt': host_qt, 'qt-source': qt_source, 'environment': environment}[args.operation](args.workspace.resolve())

if __name__ == '__main__':
    main()
