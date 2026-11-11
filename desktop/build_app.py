"""Build a local macOS window application; never package user data or secrets."""
import argparse
import json
from pathlib import Path
import plistlib
import re
import platform
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'desktop/build')
    parser.add_argument('--runtime', type=Path, help='Embed an assembled portable runtime')
    parser.add_argument('--preview', action='store_true', help='Allow an explicitly marked working-tree preview')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        raise SystemExit('MACOS_BUILD_REQUIRED')
    config = json.loads((ROOT / ('desktop/portable.json' if args.runtime else 'desktop/app.json')).read_text())
    dirty = bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip())
    if dirty and not args.preview:
        raise SystemExit('CLEAN_SOURCE_REQUIRED; use --preview only for local review')
    if (config.get('version') != 1 or config.get('mode') not in {'static', 'process'}
            or not isinstance(config.get('name'), str) or not re.fullmatch(r'[\w .-]{1,60}', config['name'])
            or config['name'].startswith('.') or config['name'].endswith(' ')
            or not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+){2,}', config.get('identifier', ''))
            or type(config.get('port')) is not int or not 1024 <= config['port'] <= 65535):
        raise SystemExit('APPLICATION_PROFILE_INVALID')
    if not isinstance(config.get('mark'), str) or not 1 <= len(config['mark']) <= 3 or '\n' in config['mark']:
        raise SystemExit('APPLICATION_PROFILE_INVALID')
    if config['mode'] == 'process':
        launch = config.get('launch')
        if (not isinstance(launch, dict) or not isinstance(launch.get('executable'), str)
                or not isinstance(launch.get('arguments'), list) or len(launch['arguments']) > 64
                or not all(isinstance(value, str) and len(value) <= 4096 for value in launch['arguments'])
                or not isinstance(launch.get('workingDirectory'), str)
                or not isinstance(launch.get('healthPath'), str) or not isinstance(launch.get('healthMarker'), str)
                or not 1 <= len(launch['healthMarker']) <= 128
                or type(launch.get('stopOnQuit')) is not bool
                or type(config.get('smokePort')) is not int or not 1024 <= config['smokePort'] <= 65535
                or config['smokePort'] == config['port']):
            raise SystemExit('APPLICATION_PROFILE_INVALID')
    output = args.output.expanduser().absolute()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / (config['name'] + '.app')
    if destination.exists():
        raise SystemExit('APPLICATION_ALREADY_EXISTS; choose a new build directory')
    needs_assets = config['mode'] == 'static' or 'assets' in config
    relative = Path(config.get('assets', 'dist'))
    if relative.is_absolute() or '..' in relative.parts:
        raise SystemExit('FRONTEND_ASSET_INVALID')
    ui = ROOT / relative
    if needs_assets and not (ui / 'index.html').is_file():
        raise SystemExit('FRONTEND_BUILD_REQUIRED; run npm run build first')
    if needs_assets and ui.is_symlink():
        raise SystemExit('FRONTEND_ASSET_INVALID')
    paths = list(ui.rglob('*')) if needs_assets and ui.is_dir() else []
    files = [path for path in paths if path.is_file()]
    allowed = {'.html', '.js', '.css', '.svg', '.png', '.jpg', '.webp', '.woff2', '.txt', '.json', '.ico'}
    if (len(files) > 512 or sum(path.stat().st_size for path in files) > 32 * 1024 * 1024
            or any(path.is_symlink() for path in paths)
            or any(path.stat().st_size > 8 * 1024 * 1024 or path.suffix not in allowed
                   or any(part.startswith('.') for part in path.relative_to(ui).parts) for path in files)):
        raise SystemExit('FRONTEND_ASSET_INVALID')
    staging = Path(tempfile.mkdtemp(prefix='.app-', dir=output))
    try:
        app = staging / destination.name
        binary = app / 'Contents/MacOS/Workspace'
        resources = app / 'Contents/Resources'
        binary.parent.mkdir(parents=True); resources.mkdir()
        info = {'CFBundleIdentifier': config['identifier'], 'CFBundleName': config['name'],
                'CFBundleDisplayName': config['name'], 'CFBundleExecutable': 'Workspace',
                'CFBundlePackageType': 'APPL', 'CFBundleIconFile': 'AppIcon', 'CFBundleShortVersionString': '1.0.0',
                'CFBundleVersion': '1', 'LSMinimumSystemVersion': '13.0',
                'NSHighResolutionCapable': True,
                'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True}}
        (app / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
        installed = {**config, 'includesWorkingTreeChanges': dirty, 'sourceCommit': subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()}
        def resolve(value):
            if isinstance(value, str):
                for token, replacement in {'{{REPO}}': str(ROOT), '{{PYTHON}}': str(ROOT/'.venv/bin/python'), '{{HOME}}': str(Path.home()), '{{NODE}}': shutil.which('node') or ''}.items():
                    value = value.replace(token, replacement)
                return value
            if isinstance(value, list): return [resolve(item) for item in value]
            if isinstance(value, dict): return {key: resolve(item) for key, item in value.items()}
            return value
        installed = installed if args.runtime else resolve(installed)
        if args.runtime:
            runtime = args.runtime.resolve()
            if not (runtime / 'runtime-manifest.json').is_file():
                raise SystemExit('RUNTIME_MANIFEST_REQUIRED')
            import hashlib
            manifest = json.loads((runtime / 'runtime-manifest.json').read_text())
            if manifest.get('sourceCommit') != installed['sourceCommit'] or manifest.get('includesWorkingTreeChanges') != dirty:
                raise SystemExit('RUNTIME_SOURCE_IDENTITY_MISMATCH')
            paths = list(runtime.rglob('*'))
            if len(paths) > 30000 or any(p.is_symlink() and not p.resolve().is_relative_to(runtime) for p in paths):
                raise SystemExit('RUNTIME_LINK_OR_CAPACITY_INVALID')
            actual_files = {str(p.relative_to(runtime)) for p in runtime.rglob('*') if p.is_file() and not p.is_symlink() and p.name != 'runtime-manifest.json'}
            if actual_files != set(manifest['files']):
                raise SystemExit('RUNTIME_FILE_SET_MISMATCH')
            for name, expected in manifest['files'].items():
                candidate = runtime / name
                if not candidate.resolve().is_relative_to(runtime) or candidate.is_symlink() or not candidate.is_file() or hashlib.sha256(candidate.read_bytes()).hexdigest() != expected:
                    raise SystemExit('RUNTIME_DIGEST_MISMATCH')
            shutil.copytree(runtime, resources / 'runtime', symlinks=True)
            for old_token in ['{{REPO}}', '{{NODE}}', '{{PYTHON}}']:
                if old_token in json.dumps(installed):
                    raise SystemExit('SOURCE_BOUND_LAUNCH_PROFILE')
        (resources / 'app.json').write_text(json.dumps(installed, indent=2))
        if needs_assets:
            shutil.copytree(ROOT / config.get('assets', 'dist'), resources / 'ui')
        shutil.copy2(ROOT / 'desktop/guide.html', resources / 'UserGuide.html')
        architecture = platform.machine()
        if architecture not in {'arm64', 'x86_64'}:
            raise SystemExit('MACOS_ARCHITECTURE_UNSUPPORTED')
        subprocess.run(['xcrun','swiftc','-swift-version','5','-target',architecture+'-apple-macosx13.0','-O','-o',str(binary),
                        str(ROOT/'desktop/LocalWebServer.swift'),str(ROOT/'desktop/ServiceOwnership.swift'),str(ROOT/'desktop/BackendRuntime.swift'),str(ROOT/'desktop/main.swift')],check=True)
        iconset = staging / 'AppIcon.iconset'
        subprocess.run([str(binary),'--render-icon',str(iconset)],check=True)
        subprocess.run(['iconutil','-c','icns',str(iconset),'-o',str(resources/'AppIcon.icns')],check=True)
        subprocess.run(['codesign','--force','--sign','-',str(app)],check=True)
        app.rename(destination)
        print(json.dumps({'application':str(destination),'sourceCommit':installed['sourceCommit']}))
    finally:
        shutil.rmtree(staging)

if __name__ == '__main__':
    main()
