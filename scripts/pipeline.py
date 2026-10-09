#!/usr/bin/env python3
"""Trusted runner-side discovery and artifact validation; never source AUR data."""

import json
from pathlib import Path
import shutil
import tarfile
import re
import subprocess
import urllib.parse

PACKAGE_BASES = (
    'android-sdk-build-tools', 'android-sdk-cmdline-tools-latest',
    'android-sdk-platform-tools', 'claude-code', 'cockpit-file-sharing',
    'cockpit-sensors', 'github-copilot-cli', 'google-chrome', 'i7z',
    'visual-studio-code-bin',
)


def get_json(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def resolve_commit(base):
    output = subprocess.check_output(
        ['git', 'ls-remote', '--exit-code', f'https://aur.archlinux.org/{base}.git', 'refs/heads/master'],
        text=True, timeout=90)
    fields = output.strip().split()
    if len(fields) != 2 or fields[1] != 'refs/heads/master':
        raise ValueError('unexpected AUR ref response')
    return fields[0]


def discover_current(bases=PACKAGE_BASES, fetch=get_json, resolve=resolve_commit):
    commits = {base: resolve(base) for base in sorted(bases)}
    url = 'https://aur.archlinux.org/rpc/v5/info?' + urllib.parse.urlencode([('arg[]', base) for base in bases])
    response = fetch(url)
    versions = {}
    for item in response.get('results', []):
        base, version = item['PackageBase'], item['Version']
        if base in versions and versions[base] != version:
            raise ValueError('inconsistent split-package RPC version')
        versions[base] = version
    rows = [{'pkgbase': base, 'commit': commits[base], 'aur_version': versions.get(base)} for base in sorted(bases)]
    for row in rows:
        validate_provenance(row, bases)
    return rows


def validate_provenance(row, bases=PACKAGE_BASES):
    if set(row) != {'pkgbase', 'commit', 'aur_version'} or row['pkgbase'] not in bases:
        raise ValueError('invalid provenance base or keys')
    if not isinstance(row['commit'], str) or not re.fullmatch(r'[0-9a-f]{40}', row['commit']):
        raise ValueError('invalid AUR commit')
    if not isinstance(row['aur_version'], str) or not re.fullmatch(r'[^\s/\x00-\x1f]{1,200}', row['aur_version']):
        raise ValueError('invalid AUR version')

import urllib.error
import urllib.request


def load_manifest(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            manifest = json.load(response)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code == 404:
            return {'packages': {}}
        raise
    if not isinstance(manifest, dict) or not isinstance(manifest.get('packages'), dict):
        raise ValueError('invalid manifest')
    return manifest


def package_files(directory):
    result = []
    for path in sorted(Path(directory).glob('*.pkg.tar.zst')):
        if path.is_symlink() or not path.is_file():
            raise ValueError('package must be a regular file')
        if not re.fullmatch(r'[A-Za-z0-9@_+.:~-]+\.pkg\.tar\.zst', path.name):
            raise ValueError('unsafe package filename')
        if re.search(r'-debug-[^-]+-[^-]+-[^-]+\.pkg\.tar\.zst$', path.name):
            continue
        result.append(path)
    if not result:
        raise ValueError('no non-debug package archives')
    return result


def collect(source, output, provenance):
    validate_provenance(provenance)
    files = package_files(source)
    target = Path(output) / provenance['pkgbase']
    target.mkdir(parents=True, exist_ok=False)
    for path in files:
        shutil.copyfile(path, target / path.name, follow_symlinks=False)
    (target / 'provenance.json').write_text(json.dumps(provenance, sort_keys=True) + '\n')


def pack(root, output, expected, require_all=False):
    root = Path(root)
    wanted = {row['pkgbase']: row for row in expected}
    if len(wanted) != len(expected):
        raise ValueError('duplicate expected bases')
    files = []
    seen = set()
    for directory in sorted(root.iterdir()):
        base = directory.name
        if directory.is_symlink() or not directory.is_dir() or base not in wanted:
            raise ValueError('unexpected artifact directory')
        provenance_path = directory / 'provenance.json'
        if provenance_path.is_symlink() or not provenance_path.is_file():
            raise ValueError('invalid provenance file')
        row = json.loads(provenance_path.read_text())
        validate_provenance(row)
        if row != wanted[base]:
            raise ValueError('provenance does not match discovery')
        packages = package_files(directory)
        allowed = {path.name for path in packages} | {'provenance.json'}
        if {path.name for path in directory.iterdir()} != allowed:
            raise ValueError('extra artifact contents')
        files.extend(sorted(packages + [provenance_path]))
        seen.add(base)
    if not seen or (require_all and seen != set(wanted)):
        raise ValueError('missing required build artifacts')
    with tarfile.open(output, 'w', format=tarfile.USTAR_FORMAT) as archive:
        for path in files:
            archive.add(path, arcname=path.relative_to(root), recursive=False)
    return sorted(seen)


def changed(current, manifest):
    previous = manifest['packages']
    return sorted((item for item in current if any(
        previous.get(item['pkgbase'], {}).get(key) != item[key]
        for key in ('commit', 'aur_version'))), key=lambda item: item['pkgbase'])


def verify(root, url, fetch=get_json):
    import hashlib
    manifest = fetch(url)
    verified = []
    for directory in sorted(Path(root).iterdir()):
        row = json.loads((directory / 'provenance.json').read_text())
        validate_provenance(row)
        entry = manifest.get('packages', {}).get(row['pkgbase'], {})
        if any(entry.get(key) != row[key] for key in ('commit', 'aur_version')):
            raise ValueError(f'public provenance mismatch: {directory.name}')
        local = {}
        for path in package_files(directory):
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                published = path.name.removesuffix('.pkg.tar.zst') + '.' + digest + '.pkg.tar.zst'
                local[published] = digest
        remote = {item['filename']: item['sha256'] for item in entry.get('packages', [])}
        if local != remote:
            raise ValueError(f'public package hashes mismatch: {directory.name}')
        verified.append(directory.name)
    if not verified:
        raise ValueError('no uploaded artifacts to verify')
    return verified


def main():
    import argparse
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    discovery = commands.add_parser('discover')
    discovery.add_argument('--manifest', default='https://mirrors.5cena.cc/arch/aur/manifest.json')
    discovery.add_argument('--force', action='store_true')
    discovery.add_argument('--output', default='matrix.json')
    collection = commands.add_parser('collect')
    collection.add_argument('source')
    collection.add_argument('output')
    collection.add_argument('provenance')
    packing = commands.add_parser('pack')
    packing.add_argument('root')
    packing.add_argument('output')
    packing.add_argument('matrix')
    packing.add_argument('--require-all', action='store_true')
    verification = commands.add_parser('verify')
    verification.add_argument('root')
    verification.add_argument('--manifest', default='https://mirrors.5cena.cc/arch/aur/manifest.json')
    args = parser.parse_args()
    if args.command == 'discover':
        manifest = load_manifest(args.manifest)
        current = discover_current()
        rows = current if args.force else changed(current, manifest)
        matrix = json.dumps({'include': rows}, sort_keys=True, separators=(',', ':'))
        Path(args.output).write_text(matrix + '\n')
        outputs = {'matrix': matrix, 'has_changes': str(bool(rows)).lower(),
                   'require_all': str(args.force or not manifest['packages']).lower()}
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
                for key, value in outputs.items():
                    stream.write(f'{key}={value}\n')
        print(json.dumps(outputs, sort_keys=True))
    elif args.command == 'collect':
        collect(Path(args.source), Path(args.output), json.loads(args.provenance))
    elif args.command == 'pack':
        expected = json.loads(Path(args.matrix).read_text())['include']
        print(json.dumps(pack(args.root, args.output, expected, args.require_all)))
    elif args.command == 'verify':
        print(json.dumps(verify(args.root, args.manifest)))


if __name__ == '__main__':
    main()

