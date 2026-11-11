"""Assemble an owned, relocatable runtime. Never copy a user's workspace."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--python-runtime', type=Path)
    parser.add_argument('--node-license', type=Path, help='Full upstream license for the installed Node runtime')
    args = parser.parse_args()
    profile = json.loads((ROOT / 'desktop/portable.json').read_text())
    product = profile['product']
    output = args.output.expanduser().absolute()
    if output.exists():
        raise SystemExit('OUTPUT_EXISTS; select a new empty directory')
    output.mkdir(parents=True)
    app = output / 'app'
    app.mkdir()

    def copy(relative, destination=None):
        source = ROOT / relative
        target = output / destination if destination else app / relative
        if not source.exists() or source.is_symlink():
            raise SystemExit('BUILD_ASSET_MISSING_OR_LINK: ' + relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'node_modules', '.DS_Store'))
        else:
            shutil.copy2(source, target)

    if product == 'skilltape':
        for binary in ['skilltape', 'skilltape-console-api']:
            copy('target/release/' + binary, 'bin/' + binary)
    elif product in {'skillsync', 'haptic-bridge'}:
        node = shutil.which('node')
        if not node:
            raise SystemExit('NODE_REQUIRED')
        (output / 'bin').mkdir()
        shutil.copy2(node, output / 'bin/node')
        for relative in ['package.json', 'package-lock.json']:
            copy(relative)
        for relative in (['dist', 'runner', 'profiles', 'templates', 'config', 'fixtures']
                         if product == 'skillsync' else ['dist/src', 'dist/data']):
            copy(relative)
        # Resolve production dependencies from the lock; no developer modules or cache copied.
        subprocess.run(['npm', 'ci', '--omit=dev', '--ignore-scripts', '--no-audit', '--no-fund'], cwd=app, check=True)
        node_prefix = Path(node).resolve().parent.parent
        candidates = [args.node_license] if args.node_license else [node_prefix / 'LICENSE', node_prefix / 'share/doc/node/LICENSE', node_prefix / 'share/doc/nodejs/LICENSE']
        license_root = next((path for path in candidates if path and path.is_file()), Path('/nonexistent-node-license'))
        if not license_root.is_file():
            raise SystemExit('NODE_LICENSE_REQUIRED')
        shutil.copy2(license_root, output / 'bin/NODE-LICENSE.txt')
    elif product in {'marine', 'inkwise'}:
        if not args.python_runtime or not (args.python_runtime / 'bin/python3').is_file():
            raise SystemExit('RELOCATABLE_PYTHON_312_REQUIRED')
        shutil.copytree(args.python_runtime, output / 'python', symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store'))
        # Only the explicit source/resource closure; no .env, DB, weights, logs or history.
        if product == 'marine':
            directories = ['marine', 'contracts', 'web', 'third_party']
            files = ['pyproject.toml', 'requirements.local.lock', 'tools/start_app.py', 'tools/runtime_environment.py']
        else:
            directories = ['src/inkwise', 'web', 'teaching']
            files = ['pyproject.toml', 'scripts/start_local.py', 'scripts/first_run.py']
        for relative in directories + files:
            copy(relative)
        if product == 'inkwise':
            binary = app / 'bin/inkwise-vision'
            binary.parent.mkdir()
            subprocess.run(['xcrun', 'swiftc', '-O', str(ROOT / 'native/vision-ocr.swift'), '-o', str(binary)], check=True)
        (app / 'embedded-runtime.json').write_text(json.dumps({'schema': 'embedded-runtime/v1', 'product': product}) + '\n')
        files = {str(p.relative_to(app)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(app.rglob('*')) if p.is_file()}
        (app / 'distribution.json').write_text(json.dumps({'schema': 'portable-source/v1', 'files': files}, indent=2) + '\n')
    else:
        raise SystemExit('PRODUCT_UNSUPPORTED')

    if (ROOT / 'desktop/runtime-licenses').is_dir():
        shutil.copytree(ROOT / 'desktop/runtime-licenses', output / 'runtime-licenses')
    for relative in ['LICENSE', 'THIRD_PARTY_NOTICES.md']:
        if (ROOT / relative).is_file():
            shutil.copy2(ROOT / relative, output / relative)
    # Reject external links after assembly, including Python runtime links.
    for p in output.rglob('*'):
        if p.is_symlink() and not p.resolve().is_relative_to(output.resolve()):
            raise SystemExit('EXTERNAL_RUNTIME_LINK: ' + str(p.relative_to(output)))
    manifest = {str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(output.rglob('*')) if p.is_file() and not p.is_symlink()}
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip())
    (output / 'runtime-manifest.json').write_text(json.dumps({'schema': 'runtime-files/v1', 'product': product, 'sourceCommit': revision, 'includesWorkingTreeChanges': dirty, 'files': manifest}, indent=2) + '\n')
    print(json.dumps({'product': product, 'files': len(manifest), 'runtime': str(output)}))


if __name__ == '__main__':
    main()
