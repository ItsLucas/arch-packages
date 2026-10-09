#!/usr/bin/python3
"""Restricted AUR artifact intake. No package data is ever executed."""
import argparse
import fcntl
import hashlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
import re
import resource
import tarfile
from pathlib import Path

ALLOWED = {name: {name} for name in (
    'android-sdk-build-tools', 'android-sdk-cmdline-tools-latest',
    'android-sdk-platform-tools', 'claude-code', 'cockpit-file-sharing',
    'cockpit-sensors', 'github-copilot-cli', 'google-chrome', 'i7z',
    'visual-studio-code-bin')}
MAX_TOTAL = 3 * 1024**3
MAX_MEMBER = 1024**3
MAX_PROVENANCE = 16 * 1024
MAX_ENTRIES = 100
FILENAME = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9+_.-]{0,220}\.pkg\.tar\.zst\Z')
VERSION = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9+_.:~-]{0,127}\Z')
COMMIT = re.compile(r'[0-9a-f]{40}\Z')


class Rejected(ValueError):
    """Untrusted input rejected without publishing."""


def require(condition, message):
    if not condition:
        raise Rejected(message)


def read_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate JSON key')
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as exc:
        raise Rejected('invalid JSON') from exc


def parse_pkginfo(data, base, aur_version, filename):
    require(len(data) <= 128 * 1024 and b'\0' not in data, 'invalid PKGINFO size')
    try:
        text = data.decode('utf-8')
    except UnicodeError as exc:
        raise Rejected('invalid PKGINFO encoding') from exc
    fields = {}
    safe_keys = {'pkgname', 'pkgbase', 'pkgver', 'pkgdesc', 'url', 'builddate', 'packager',
                 'size', 'arch', 'license', 'group', 'depend', 'optdepend', 'makedepend',
                 'checkdepend', 'conflict', 'replaces', 'provides', 'backup', 'xdata'}
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        require(' = ' in line, 'malformed PKGINFO')
        key, value = line.split(' = ', 1)
        # repo-add 7.1 uses Bash `declare "$var=$val"`: an unknown key can
        # overwrite its locals or trigger command substitution in an array key.
        require(key in safe_keys, 'unsafe PKGINFO key')
        require(not any(ord(char) < 32 or ord(char) == 127 for char in value), 'control character in PKGINFO')
        if key in {'pkgname', 'pkgbase', 'pkgver', 'arch'}:
            require(key not in fields, 'duplicate PKGINFO field')
            fields[key] = value
    require(set(fields) == {'pkgname', 'pkgbase', 'pkgver', 'arch'}, 'missing PKGINFO field')
    require(fields['pkgbase'] == base and fields['pkgname'] in ALLOWED[base], 'package identity mismatch')
    require(fields['arch'] in {'x86_64', 'any'}, 'unsupported package architecture')
    require(VERSION.fullmatch(fields['pkgver']) and fields['pkgver'] == aur_version, 'package version mismatch')
    # Epoch is metadata-only in makepkg filenames.
    version = fields['pkgver'].split(':', 1)[-1]
    expected = f"{fields['pkgname']}-{version}-{fields['arch']}.pkg.tar.zst"
    require(filename == expected, 'package filename mismatch')
    return {'name': fields['pkgname'], 'version': fields['pkgver'], 'filename': filename}


class TransportArchive:
    """Parse physical tar headers before any extension can allocate memory."""
    def __init__(self, stream):
        self.stream = stream
        self.remaining = 0
        self.wire = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raw(self, size):
        result = bytearray()
        while len(result) < size:
            chunk = self.stream.read(min(size - len(result), 1024 * 1024))
            require(bool(chunk), 'truncated transport')
            result.extend(chunk)
        self.wire += len(result)
        require(self.wire <= MAX_TOTAL + MAX_ENTRIES * 1024 + 65536, 'wire size limit exceeded')
        return bytes(result)

    def __iter__(self):
        while True:
            header = self.raw(512)
            if header == bytes(512):
                require(self.raw(512) == bytes(512), 'invalid tar end marker')
                padding = 0
                while True:
                    chunk = self.stream.read(8192)
                    if not chunk:
                        return
                    padding += len(chunk)
                    require(padding <= 65536 and not any(chunk), 'unexpected trailing tar data')
            require(header[257:263] in (b'ustar\0', b'ustar '), 'transport must be USTAR')
            member = tarfile.TarInfo.frombuf(header, 'utf-8', 'strict')
            require(member.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE), 'tar extensions and links forbidden')
            self.remaining = member.size
            yield member
            require(self.remaining == 0, 'member not fully consumed')
            padding = (-member.size) % 512
            if padding:
                require(not any(self.raw(padding)), 'nonzero tar padding')

    def extractfile(self, member):
        return self

    def read(self, size):
        require(0 <= size <= self.remaining, 'invalid member read')
        data = self.raw(size)
        self.remaining -= size
        return data


def intake(stream, destination):
    """Read a raw transport tar, never extractall; destination must be private."""
    destination = Path(destination)
    seen, records = set(), {}
    total = 0
    try:
        with TransportArchive(stream) as archive:
            for member in archive:
                name = member.name
                require(len(seen) < MAX_ENTRIES, 'too many entries')
                require(name not in seen, 'duplicate tar path')
                seen.add(name)
                require(not member.pax_headers, 'PAX metadata is not allowed')
                parts = name.rstrip('/').split('/') if member.isdir() else name.split('/')
                require(parts[0] in ALLOWED, 'unknown package base')
                require(all(p and p not in ('.', '..') for p in parts), 'unsafe path')
                if member.isdir():
                    require(len(parts) == 1 and member.size == 0, 'unexpected directory')
                    (destination / parts[0]).mkdir(exist_ok=True)
                    continue
                require(member.isreg() and len(parts) == 2, 'not a regular artifact')
                base, filename = parts
                require(filename == 'provenance.json' or FILENAME.fullmatch(filename), 'unexpected filename')
                require('-debug-' not in filename, 'debug packages forbidden')
                limit = MAX_PROVENANCE if filename == 'provenance.json' else MAX_MEMBER
                require(0 <= member.size <= limit, 'member too large')
                total += member.size
                require(total <= MAX_TOTAL, 'transfer too large')
                folder = destination / base
                folder.mkdir(exist_ok=True)
                source = archive.extractfile(member)
                with (folder / filename).open('xb') as target:
                    remaining = member.size
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        require(bool(chunk), 'truncated member')
                        target.write(chunk)
                        remaining -= len(chunk)
                records.setdefault(base, {'filenames': []})
                if filename != 'provenance.json':
                    records[base]['filenames'].append(filename)
        require(bool(records), 'empty transfer')
        for base, record in records.items():
            provenance = destination / base / 'provenance.json'
            require(provenance.is_file(), 'missing provenance')
            value = read_json(provenance.read_bytes())
            require(isinstance(value, dict) and set(value) == {'pkgbase', 'commit', 'aur_version'}, 'invalid provenance schema')
            require(value['pkgbase'] == base, 'provenance base mismatch')
            require(isinstance(value['commit'], str) and COMMIT.fullmatch(value['commit']), 'invalid commit')
            require(isinstance(value['aur_version'], str) and VERSION.fullmatch(value['aur_version']), 'invalid AUR version')
            require(bool(record['filenames']), 'no packages for base')
            record.update(value)
            record['filenames'].sort()
        return records
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise Rejected('invalid or incomplete transfer') from exc


@dataclass
class Tools:
    # Dependency injection is for local tests only. The CLI accepts no tool paths.
    gpg: str = '/usr/bin/gpg'
    zstd: str = '/usr/bin/zstd'
    bsdtar: str = '/usr/bin/bsdtar'
    gnupg: Path = Path('/srv/arch-aur/gnupg')
    vercmp: str = '/usr/bin/vercmp'
    repo_add: list = field(default_factory=lambda: ['/usr/bin/repo-add'])


def index_limits():
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024**2, 32 * 1024**2))
    resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (180, 180))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def run(argv, timeout=300, extra_env=None, **kwargs):
    process = None
    try:
        environment = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        if extra_env:
            environment.update(extra_env)
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True,
                                   env=environment, **kwargs)
        output, _ = process.communicate(timeout=timeout)
        require(process.returncode == 0, 'external tool failed')
        return output
    except (OSError, subprocess.SubprocessError) as exc:
        raise Rejected('external tool failed') from exc
    finally:
        if process is not None:
            # Kill descendants even when the parent exited while a pipe remained
            # open, or when the CLI alarm interrupted communicate().
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()


MAX_PACKAGE_TAR = 4 * 1024**3
MAX_METADATA_SCAN = 16 * 1024**2
MAX_PACKAGE_HEADERS = 200000
MAX_TAR_EXTENSION = 128 * 1024


class MetadataReader:
    """Fixed-size reads, output budget and wall deadline; no disk spool."""
    def __init__(self, pipe, limit=MAX_PACKAGE_TAR, timeout=300):
        self.pipe = pipe
        self.total = 0
        self.limit = limit
        self.deadline = time.monotonic() + timeout
        self.selector = selectors.DefaultSelector()
        self.selector.register(pipe, selectors.EVENT_READ)

    def read(self, size):
        require(0 <= size <= 1024**2, 'package read allocation limit exceeded')
        result = bytearray()
        while len(result) < size:
            remaining = self.deadline - time.monotonic()
            require(remaining > 0 and self.selector.select(remaining), 'package validation timeout')
            chunk = os.read(self.pipe.fileno(), min(size - len(result), 64 * 1024))
            if not chunk:
                break
            self.total += len(chunk)
            require(self.total <= self.limit, 'package decompression limit exceeded')
            result.extend(chunk)
        return bytes(result)


class BoundedTarInfo(tarfile.TarInfo):
    """Cap extension allocations/recursion before Python's tar parser runs."""
    @classmethod
    def frombuf(cls, buf, encoding, errors):
        try:
            return super().frombuf(buf, encoding, errors)
        except tarfile.EOFHeaderError:
            require(buf == bytes(512), 'invalid package tar end marker')
            raise
        except tarfile.HeaderError as exc:
            # TarFile.next otherwise treats an invalid/truncated later header
            # as end-of-archive. Only a real zero block is an acceptable end.
            raise Rejected('invalid package tar header') from exc

    def _proc_member(self, archive):
        archive.package_headers = getattr(archive, 'package_headers', 0) + 1
        depth = getattr(archive, 'package_depth', 0)
        require(archive.package_headers <= MAX_PACKAGE_HEADERS and depth < 16,
                'package tar header limit exceeded')
        require(0 <= self.size <= MAX_PACKAGE_TAR, 'oversized package tar member')
        require(self.type != tarfile.GNUTYPE_SPARSE, 'sparse package tar unsupported')
        extension = self.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE,
                                  tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK)
        if extension:
            require(self.size <= MAX_TAR_EXTENSION, 'oversized package tar extension')
        archive.package_depth = depth + 1
        try:
            return super()._proc_member(archive)
        finally:
            archive.package_depth = depth

    def _apply_pax_info(self, headers, encoding, errors):
        require(sum(len(k) + len(v) for k, v in headers.items()) <= MAX_TAR_EXTENSION,
                'oversized package PAX metadata')
        require(not any(k.startswith('GNU.sparse') or k in {'SCHILY.realsize', 'SCHILY.filetype'}
                        for k in headers), 'sparse package tar unsupported')
        if 'size' in headers:
            require(re.fullmatch(r'[0-9]{1,10}', headers['size']) and
                    int(headers['size']) <= MAX_PACKAGE_TAR, 'invalid PAX size')
        super()._apply_pax_info(headers, encoding, errors)

    def _proc_gnusparse_00(self, *args):
        raise Rejected('sparse package tar unsupported')

    _proc_gnusparse_01 = _proc_gnusparse_00
    _proc_gnusparse_10 = _proc_gnusparse_00


def checked_package_stream(argv, consume, limit=MAX_PACKAGE_TAR, timeout=300):
    """Require real EOF and a successful child exit, not an early metadata hit."""
    process = None
    reader = None
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True,
                                   preexec_fn=index_limits,
                                   env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        reader = MetadataReader(process.stdout, limit, timeout)
        result = consume(reader)
        remaining = reader.deadline - time.monotonic()
        require(remaining > 0, 'package validation timeout')
        require(process.wait(timeout=remaining) == 0, 'package decoder failed')
        return result
    except (tarfile.TarError, OSError, EOFError, ValueError, subprocess.SubprocessError) as exc:
        raise Rejected('invalid package archive') from exc
    finally:
        if reader is not None:
            reader.selector.close()
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.stdout.close()
            process.wait(timeout=5)


def package_info(path, base, version, tools):
    def validate(reader):
        data = None
        full_deadline = reader.deadline
        reader.deadline = min(full_deadline, time.monotonic() + 30)
        with tarfile.open(fileobj=reader, mode='r|', bufsize=64 * 1024, tarinfo=BoundedTarInfo,
                          encoding='utf-8', errors='surrogateescape') as archive:
            for member in archive:
                require(data is not None or reader.total <= MAX_METADATA_SCAN,
                        'package metadata scan limit exceeded')
                require(0 <= member.size <= MAX_PACKAGE_TAR, 'oversized package tar member')
                if '.PKGINFO' in member.name.split('/'):
                    require(member.name == '.PKGINFO', 'noncanonical PKGINFO path')
                if member.name == '.PKGINFO':
                    require(data is None, 'duplicate PKGINFO member')
                    require(member.isreg() and 0 < member.size <= 128 * 1024, 'unsafe PKGINFO')
                    data = archive.extractfile(member).read(member.size)
                    reader.deadline = full_deadline
                elif member.isreg():
                    # Consume every payload, even after PKGINFO. No extraction or
                    # allocation proportional to the member's claimed size.
                    source = archive.extractfile(member)
                    remaining = member.size
                    while remaining:
                        chunk = source.read(min(64 * 1024, remaining))
                        require(bool(chunk), 'truncated package tar payload')
                        remaining -= len(chunk)
                        require(data is not None or reader.total <= MAX_METADATA_SCAN,
                                'package metadata scan limit exceeded')
                # Python <=3.12 caches members even in streaming mode.
                archive.members.clear()
            require(archive.fileobj.tell() == archive.offset + 512,
                    'package tar end marker missing')
            require(archive.fileobj.read(512) == bytes(512), 'invalid package tar end marker')
            padding = 0
            while True:
                chunk = archive.fileobj.read(64 * 1024)
                if not chunk:
                    break
                padding += len(chunk)
                require(padding <= 65536 and not any(chunk), 'unexpected trailing package tar data')
        require(data is not None, 'PKGINFO missing')
        return data

    data = checked_package_stream([tools.zstd, '-dc', '-M256MB', '--', str(path)], validate)
    # repo-add uses libarchive, not Python tarfile. Use its exact metadata
    # selector and compare bytes, but omit -q so it also validates the full tar.
    def native_metadata(reader):
        result = bytearray()
        while True:
            chunk = reader.read(64 * 1024)
            if not chunk:
                return bytes(result)
            result.extend(chunk)
    native = checked_package_stream([tools.bsdtar, '-xOf', str(path), '.PKGINFO'], native_metadata,
                                    limit=128 * 1024)
    require(native == data, 'package metadata parser mismatch')
    return parse_pkginfo(data, base, version, path.name)


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def validate_manifest(value):
    require(isinstance(value, dict) and isinstance(value.get('packages'), dict), 'invalid manifest')
    names, filenames = set(), set()
    for base, record in value['packages'].items():
        require(base in ALLOWED and isinstance(record, dict), 'invalid manifest base')
        require(isinstance(record.get('commit'), str) and COMMIT.fullmatch(record['commit']), 'invalid manifest commit')
        require(isinstance(record.get('aur_version'), str) and VERSION.fullmatch(record['aur_version']), 'invalid manifest version')
        require(isinstance(record.get('packages'), list) and 0 < len(record['packages']) <= len(ALLOWED[base]), 'invalid manifest packages')
        for package in record['packages']:
            require(isinstance(package, dict) and set(package) == {'name', 'version', 'filename', 'sha256'}, 'invalid manifest package')
            require(package['name'] in ALLOWED[base] and package['name'] not in names, 'manifest name mismatch')
            require(isinstance(package['version'], str) and VERSION.fullmatch(package['version']), 'invalid package version')
            require(package['version'] == record['aur_version'], 'manifest version mismatch')
            require(isinstance(package['filename'], str) and FILENAME.fullmatch(package['filename']) and '-debug-' not in package['filename'], 'invalid package filename')
            require(package['filename'] not in filenames, 'duplicate package filename')
            require(isinstance(package['sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', package['sha256']), 'invalid package hash')
            names.add(package['name']); filenames.add(package['filename'])
    retained = value.get('retained', [])
    require(isinstance(retained, list) and len(retained) <= len(names), 'invalid retained packages')
    retained_names = set()
    for package in retained:
        require(isinstance(package, dict) and set(package) == {'name', 'version', 'filename', 'sha256'}, 'invalid retained record')
        require(package['name'] in names and package['name'] not in retained_names, 'invalid retained name')
        require(isinstance(package['version'], str) and VERSION.fullmatch(package['version']), 'invalid retained version')
        require(isinstance(package['filename'], str) and FILENAME.fullmatch(package['filename']) and package['filename'] not in filenames, 'invalid retained filename')
        require(isinstance(package['sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', package['sha256']), 'invalid retained hash')
        retained_names.add(package['name']); filenames.add(package['filename'])
    return value


def load_manifest(path):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 256 * 1024, 'missing or unsafe manifest')
    return validate_manifest(read_json(path.read_bytes()))


def current_snapshot(root):
    public = root / 'public'
    if not os.path.lexists(public):
        return None
    require(public.is_symlink(), 'public is not a symlink')
    target = os.readlink(public)
    require(re.fullmatch(r'snapshots/[0-9a-f]{32}', target), 'unsafe public target')
    path = root / target
    require(path.is_dir() and not path.is_symlink(), 'unsafe current snapshot')
    return path


def sign(path, key, tools):
    signature = Path(str(path) + '.sig')
    run([tools.gpg, '--homedir', str(tools.gnupg), '--batch', '--no-tty', '--pinentry-mode', 'loopback',
         '--local-user', key, '--output', str(signature), '--detach-sign', str(path)])
    verify(path, key, tools)
    signature.chmod(0o644)


def verify(path, key, tools):
    result = run([tools.gpg, '--homedir', str(tools.gnupg), '--batch', '--status-fd', '1', '--verify', str(path) + '.sig', str(path)])
    valid = False
    for line in result.splitlines():
        parts = line.split()
        if len(parts) >= 11 and parts[:2] == [b'[GNUPG:]', b'VALIDSIG']:
            valid = valid or parts[2] == key.encode() or (len(parts) == 12 and parts[-1] == key.encode())
    require(valid, 'signature key mismatch')


def link_verified(source, folder, package, key, tools):
    path = source / 'x86_64' / package['filename']
    signature = Path(str(path) + '.sig')
    require(path.is_file() and not path.is_symlink() and signature.is_file() and not signature.is_symlink(), 'unsafe inherited package')
    require(digest(path) == package['sha256'], 'inherited package hash mismatch')
    verify(path, key, tools)
    os.link(path, folder / path.name)
    os.link(signature, folder / signature.name)


def verify_indexes(folder, manifest):
    expected = {p['name']: (p['version'], p['filename'], p['sha256']) for r in manifest['packages'].values() for p in r['packages']}
    for suffix in ('db', 'files'):
        path = folder / f'lucas-aur.{suffix}.tar.gz'
        require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 16 * 1024**2, 'missing repo index')
        actual = {}
        total = 0
        try:
            with tarfile.open(path, 'r:gz') as archive:
                for member in archive:
                    total += member.size
                    require(total <= 16 * 1024**2, 'oversized repo index')
                    if not member.name.endswith('/desc'):
                        continue
                    require(member.isreg() and member.size <= 128 * 1024, 'invalid repo description')
                    text = archive.extractfile(member).read().decode('utf-8')
                    fields = {}
                    for block in text.strip().split('\n\n'):
                        lines = block.splitlines()
                        if lines[0] in {'%NAME%', '%VERSION%', '%FILENAME%', '%SHA256SUM%'}:
                            require(len(lines) == 2 and lines[0] not in fields, 'invalid repo field')
                            fields[lines[0]] = lines[1]
                    require(set(fields) == {'%NAME%', '%VERSION%', '%FILENAME%', '%SHA256SUM%'}, 'missing repo fields')
                    name = fields['%NAME%']
                    require(name not in actual, 'duplicate repo entry')
                    actual[name] = (fields['%VERSION%'], fields['%FILENAME%'], fields['%SHA256SUM%'])
            require(actual == expected, 'repo entries mismatch')
        except (tarfile.TarError, UnicodeError, OSError) as exc:
            raise Rejected('invalid repo index') from exc
        alias = folder / f'lucas-aur.{suffix}'
        require(alias.is_symlink() and os.readlink(alias) == path.name, 'unsafe repo alias')


def prune_snapshots(snapshots, current, previous):
    # Keep current, previously active, and one extra rollback snapshot. Hardlinks
    # prevent duplicating unchanged archives; each snapshot has <=2 versions/name.
    candidates = sorted((p for p in snapshots.iterdir() if re.fullmatch(r'[0-9a-f]{32}', p.name)
                         and p.is_dir() and not p.is_symlink()), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    keep = {current}
    if previous is not None:
        keep.add(previous)
    for path in candidates:
        if len(keep) >= 3:
            break
        keep.add(path)
    for path in candidates:
        if path not in keep:
            shutil.rmtree(path)


def publish(stream, root=Path('/srv/arch-aur'), tools=None):
    tools = tools or Tools()
    root = Path(root)
    require(root.is_dir() and not root.is_symlink(), 'unsafe state root')
    key = (root / 'signing-key').read_text().strip()
    require(re.fullmatch(r'[A-F0-9]{40}', key), 'invalid signing fingerprint')
    snapshots = root / 'snapshots'
    snapshots.mkdir(mode=0o755, exist_ok=True)
    require(not snapshots.is_symlink(), 'unsafe snapshots directory')
    snapshots.chmod(0o755)
    with (root / '.publish.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = current_snapshot(root)
        previous = load_manifest(old / 'manifest.json') if old else {'packages': {}}
        snapshot_id = uuid.uuid4().hex
        stage = snapshots / snapshot_id
        stage.mkdir(mode=0o700)
        folder = stage / 'x86_64'
        folder.mkdir(mode=0o755)
        folder.chmod(0o755)
        switched = False
        try:
            with tempfile.TemporaryDirectory(prefix='.intake-', dir=root) as tmp:
                records = intake(stream, Path(tmp))
                manifest = {'packages': dict(previous['packages']), 'retained': []}
                for base, record in records.items():
                    packages = []
                    names = set()
                    for filename in record['filenames']:
                        path = Path(tmp) / base / filename
                        package = package_info(path, base, record['aur_version'], tools)
                        prior = next((p for p in previous['packages'].get(base, {}).get('packages', []) if p['name'] == package['name']), None)
                        if prior is not None:
                            comparison = run([tools.vercmp, package['version'], prior['version']]).strip()
                            require(re.fullmatch(rb'-?[0-9]{1,10}', comparison) and int(comparison) >= 0, 'package downgrade forbidden')
                        require(package['name'] not in names, 'duplicate output package')
                        names.add(package['name'])
                        package['sha256'] = digest(path)
                        # A recipe can change without changing pkgver. Content-
                        # addressed public names prevent stale DB/hash races.
                        filename = filename.removesuffix('.pkg.tar.zst') + '.' + package['sha256'] + '.pkg.tar.zst'
                        require(FILENAME.fullmatch(filename), 'published filename too long')
                        package['filename'] = filename
                        shutil.move(str(path), folder / filename)
                        (folder / filename).chmod(0o644)
                        sign(folder / filename, key, tools)
                        packages.append(package)
                    require(names == ALLOWED[base], 'incomplete package output set')
                    manifest['packages'][base] = {'commit': record['commit'], 'aur_version': record['aur_version'], 'packages': packages}
                latest = [p for r in manifest['packages'].values() for p in r['packages']]
                for package in latest:
                    if not (folder / package['filename']).exists():
                        link_verified(old, folder, package, key, tools)
                latest_names = {p['name']: p['filename'] for p in latest}
                old_candidates = [p for r in previous['packages'].values() for p in r['packages']] + previous.get('retained', [])
                retained_names = set()
                for package in old_candidates:
                    name = package['name']
                    if name not in retained_names and package['filename'] != latest_names[name]:
                        link_verified(old, folder, package, key, tools)
                        manifest['retained'].append(package)
                        retained_names.add(name)
                validate_manifest(manifest)
                (stage / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
                (stage / 'manifest.json').chmod(0o644)
                with tempfile.TemporaryDirectory(prefix='.repo-tmp-', dir=stage) as repo_tmp:
                    run(list(tools.repo_add) + ['--include-sigs', '--prevent-downgrade', 'lucas-aur.db.tar.gz'] + [p['filename'] for p in latest],
                        cwd=folder, extra_env={'TMPDIR': repo_tmp}, preexec_fn=index_limits)
                verify_indexes(folder, manifest)
                for package in latest + manifest['retained']:
                    path = folder / package['filename']
                    require(not path.is_symlink() and digest(path) == package['sha256'], 'post-index package hash mismatch')
                    verify(path, key, tools)
                for suffix in ('db', 'files'):
                    path = folder / f'lucas-aur.{suffix}.tar.gz'
                    path.chmod(0o644)
                    sign(path, key, tools)
                    (folder / f'lucas-aur.{suffix}.sig').symlink_to(path.name + '.sig')
                exported = run([tools.gpg, '--homedir', str(tools.gnupg), '--batch', '--armor', '--export', key])
                require(exported.startswith(b'-----BEGIN PGP PUBLIC KEY BLOCK-----'), 'public key export failed')
                (stage / 'repo-key.asc').write_bytes(exported)
                (stage / 'repo-key.asc').chmod(0o644)
                # A complete snapshot is immutable to the receiver after promotion.
                stage.chmod(0o755)
                for path in stage.rglob('*'):
                    if path.is_file() and not path.is_symlink():
                        with path.open('rb') as handle:
                            os.fsync(handle.fileno())
                for directory in (folder, stage):
                    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                key_alias = root / ('.key-' + snapshot_id)
                key_alias.symlink_to('public/repo-key.asc')
                os.replace(key_alias, root / 'repo-key.asc')
                link = root / ('.public-' + snapshot_id)
                link.symlink_to('snapshots/' + snapshot_id)
                os.replace(link, root / 'public')
                switched = True
                fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                try:
                    prune_snapshots(snapshots, stage, old)
                except OSError:
                    # GC failure must not turn an already committed publish into
                    # a rejected transfer; operators can reclaim space separately.
                    print('warning: snapshot cleanup failed', file=sys.stderr)
                return snapshot_id
        finally:
            if not switched:
                shutil.rmtree(stage)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        require(os.environ.get('SSH_ORIGINAL_COMMAND', '') in {'', 'publish'}, 'remote command forbidden')
        require(os.geteuid() != 0, 'receiver must not run as root')
        os.umask(0o077)
        # Also bounds slow tar senders and flock waits; fail closed on timeout.
        def expired(signum, frame):
            raise Rejected('publication timeout')
        signal.signal(signal.SIGALRM, expired)
        signal.alarm(1800)
        result = publish(sys.stdin.buffer)
        signal.alarm(0)
        print(json.dumps({'snapshot': result}))
        return 0
    except Exception:
        print('publication rejected; current snapshot unchanged unless promotion already completed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
