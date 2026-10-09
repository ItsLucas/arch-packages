import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

import os
import shutil
import subprocess
import sys

import receive

BASE = 'i7z'
FILENAME = 'i7z-0.27.2-1-x86_64.pkg.tar.zst'


def transfer(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        for name, data, kind in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(data) if kind == tarfile.REGTYPE else 0
            member.linkname = '/etc/passwd' if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ''
            archive.addfile(member, io.BytesIO(data) if member.size else None)
    stream.seek(0)
    return stream


def entries():
    return [(BASE + '/provenance.json', json.dumps({'pkgbase': BASE, 'commit': 'a' * 40, 'aur_version': '0.27.2-1'}).encode(), tarfile.REGTYPE),
            (BASE + '/' + FILENAME, b'package', tarfile.REGTYPE)]


class IntakeTests(unittest.TestCase):
    def test_intake_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = receive.intake(transfer(entries()), Path(tmp))
            self.assertEqual(result[BASE]['commit'], 'a' * 40)
            self.assertEqual(result[BASE]['filenames'], [FILENAME])
            self.assertEqual((Path(tmp) / BASE / FILENAME).read_bytes(), b'package')


    def test_rejects_unsafe_transport_entries(self):
        bad = [('../i7z/x.pkg.tar.zst', b'x', tarfile.REGTYPE),
               ('/i7z/x.pkg.tar.zst', b'x', tarfile.REGTYPE),
               ('i7z/../x.pkg.tar.zst', b'x', tarfile.REGTYPE),
               ('i7z/' + FILENAME, b'', tarfile.SYMTYPE),
               ('i7z/' + FILENAME, b'', tarfile.LNKTYPE),
               ('i7z/' + FILENAME, b'', tarfile.FIFOTYPE),
               ('i7z/subdir', b'', tarfile.DIRTYPE),
               ('evil/evil.pkg.tar.zst', b'x', tarfile.REGTYPE),
               ('i7z/i7z-debug-1-1-x86_64.pkg.tar.zst', b'x', tarfile.REGTYPE),
               ('i7z/install.sh', b'x', tarfile.REGTYPE)]
        for entry in bad:
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as tmp, self.assertRaises(receive.Rejected):
                receive.intake(transfer(entries() + [entry]), Path(tmp))
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(receive.Rejected):
            receive.intake(transfer(entries() + entries()), Path(tmp))

    def test_transport_requires_end_marker_and_rejects_appended_payload(self):
        valid = transfer(entries()).getvalue()
        # Both entries occupy 4 blocks; no end-of-archive markers.
        for bad in (valid[:2048], valid + b'not-tar'):
            with self.subTest(size=len(bad)), tempfile.TemporaryDirectory() as tmp, self.assertRaises(receive.Rejected):
                receive.intake(io.BytesIO(bad), Path(tmp))


    def test_size_caps_are_checked_from_header(self):
        for name, size in ((BASE + '/' + FILENAME, receive.MAX_MEMBER + 1),
                           (BASE + '/provenance.json', receive.MAX_PROVENANCE + 1)):
            member = tarfile.TarInfo(name); member.size = size
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(receive.Rejected):
                receive.intake(io.BytesIO(member.tobuf(format=tarfile.USTAR_FORMAT)), Path(tmp))

    def test_tar_extension_rejected_before_payload_allocation(self):
        member = tarfile.TarInfo('i7z/provenance.json')
        member.type = tarfile.XHDTYPE; member.size = 4 * 1024**3
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(receive.Rejected):
            receive.intake(io.BytesIO(member.tobuf(format=tarfile.USTAR_FORMAT)), Path(tmp))

    def test_provenance_rejects_bad_schema_commit_and_missing_packages(self):
        original = json.loads(entries()[0][1])
        for value in ({**original, 'commit': 'A' * 40}, {**original, 'pkgbase': 'evil'},
                      {**original, 'aur_version': '$(id)'}, {**original, 'extra': True}, []):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp, self.assertRaises(receive.Rejected):
                receive.intake(transfer([(BASE + '/provenance.json', json.dumps(value).encode(), tarfile.REGTYPE)] + entries()[1:]), Path(tmp))
        for contents in (entries()[:1], entries()[1:], []):
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(receive.Rejected):
                receive.intake(transfer(contents), Path(tmp))


class MetadataTests(unittest.TestCase):
    def test_pkginfo_is_parsed_not_executed(self):
        info = b'pkgname = i7z\npkgbase = i7z\npkgver = 0.27.2-1\narch = x86_64\ndepend = glibc\ndepend = ncurses\n'
        result = receive.parse_pkginfo(info, BASE, '0.27.2-1', FILENAME)
        self.assertEqual(result, {'name': BASE, 'version': '0.27.2-1', 'filename': FILENAME})
        for bad in (info.replace(b'pkgname = i7z', b'pkgname = evil'),
                    info.replace(b'pkgbase = i7z', b'pkgbase = evil'),
                    info.replace(b'arch = x86_64', b'arch = aarch64'),
                    info + b'pkgname = i7z\n',
                    info.replace(b'pkgver = 0.27.2-1', b'pkgver = $(touch /tmp/evil)')):
            with self.subTest(bad=bad), self.assertRaises(receive.Rejected):
                receive.parse_pkginfo(bad, BASE, '0.27.2-1', FILENAME)


    def test_all_allowed_bases_support_any_arch_and_epoch(self):
        self.assertEqual(set(receive.ALLOWED), {'android-sdk-build-tools', 'android-sdk-cmdline-tools-latest', 'android-sdk-platform-tools', 'claude-code', 'cockpit-file-sharing', 'cockpit-sensors', 'github-copilot-cli', 'google-chrome', 'i7z', 'visual-studio-code-bin'})
        for base in receive.ALLOWED:
            data = f'pkgname = {base}\npkgbase = {base}\npkgver = 2:1.0-1\narch = any\n'.encode()
            with self.subTest(base=base):
                result = receive.parse_pkginfo(data, base, '2:1.0-1', base + '-1.0-1-any.pkg.tar.zst')
                self.assertEqual(result['version'], '2:1.0-1')

    def test_pkginfo_rejects_repo_add_bash_variable_injection(self):
        valid = b'pkgname = i7z\npkgbase = i7z\npkgver = 0.27.2-1\narch = x86_64\n'
        for malicious in (b'x[$(touch evil)] = value\n', b'PATH = /evil\n', b'tmpdir = /srv/arch-aur/gnupg\n', b'pkgfile = /etc/shadow\n'):
            with self.subTest(malicious=malicious), self.assertRaises(receive.Rejected):
                receive.parse_pkginfo(valid + malicious, BASE, '0.27.2-1', FILENAME)


class PublisherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.home = cls.root / 'gnupg'
        cls.home.mkdir(mode=0o700)
        subprocess.run(['gpg', '--homedir', str(cls.home), '--batch', '--passphrase', '', '--quick-generate-key', 'Receiver Test <test@invalid>', 'ed25519', 'sign', '0'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        output = subprocess.check_output(['gpg', '--homedir', str(cls.home), '--with-colons', '--list-secret-keys'], stderr=subprocess.DEVNULL).decode()
        cls.fingerprint = next(line.split(':')[9] for line in output.splitlines() if line.startswith('fpr:'))
        cls.indexer = cls.root / 'indexer.py'
        cls.indexer.write_text('''import io,json,pathlib,subprocess,sys,tarfile
root=pathlib.Path(sys.argv[1]); stage=pathlib.Path.cwd().parent
assert sys.argv[2:5] == ['--include-sigs','--prevent-downgrade','lucas-aur.db.tar.gz']
manifest=json.loads((stage/'manifest.json').read_text())
old=json.loads((root/'public'/'manifest.json').read_text()) if (root/'public').exists() else {'packages':{}}
for base,record in manifest['packages'].items():
 for package in record['packages']:
  for previous in old['packages'].get(base,{}).get('packages',[]):
   if previous['name']==package['name'] and int(subprocess.check_output(['vercmp',package['version'],previous['version']]))<0: sys.exit(42)
for suffix in ('db','files'):
 path=stage/'x86_64'/('lucas-aur.'+suffix+'.tar.gz')
 with tarfile.open(path,'w:gz') as tar:
  for record in manifest['packages'].values():
   for package in record['packages']:
    data=('%NAME%\\n'+package['name']+'\\n\\n%VERSION%\\n'+package['version']+'\\n\\n%FILENAME%\\n'+package['filename']+'\\n\\n%SHA256SUM%\\n'+package['sha256']+'\\n').encode()
    info=tarfile.TarInfo(package['name']+'-'+package['version']+'/desc'); info.size=len(data)
    tar.addfile(info,io.BytesIO(data))
 (stage/'x86_64'/('lucas-aur.'+suffix)).symlink_to(path.name)
''')

    @classmethod
    def tearDownClass(cls):
        subprocess.run(['gpgconf', '--homedir', str(cls.home), '--kill', 'gpg-agent'], check=False)
        cls.temp.cleanup()

    def setUp(self):
        self.previous_umask = os.umask(0o077)
        self.state_temp = tempfile.TemporaryDirectory()
        self.state = Path(self.state_temp.name)
        (self.state / 'signing-key').write_text(self.fingerprint + '\n')
        self.tools = receive.Tools(gpg='gpg', zstd='zstd', gnupg=self.home,
                                   repo_add=[sys.executable, str(self.indexer), str(self.state)])

    def tearDown(self):
        self.state_temp.cleanup()
        os.umask(self.previous_umask)

    def artifact(self, base=BASE, version='0.27.2-1', commit='a' * 40, extra=''):
        filename = f'{base}-{version}-x86_64.pkg.tar.zst'
        package = io.BytesIO()
        with tarfile.open(fileobj=package, mode='w') as tar:
            data = (f'pkgname = {base}\npkgbase = {base}\npkgver = {version}\narch = x86_64\n' + extra).encode()
            info = tarfile.TarInfo('.PKGINFO'); info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        compressed = subprocess.check_output(['zstd', '-q', '-c'], input=package.getvalue())
        provenance = json.dumps({'pkgbase': base, 'commit': commit, 'aur_version': version}).encode()
        return transfer([(base + '/provenance.json', provenance, tarfile.REGTYPE),
                         (base + '/' + filename, compressed, tarfile.REGTYPE)])

    def test_atomic_signed_snapshot_and_preservation(self):
        first = receive.publish(self.artifact(), self.state, self.tools)
        current = self.state / 'public'
        manifest = json.loads((current / 'manifest.json').read_text())
        self.assertEqual(manifest['packages'][BASE]['packages'][0]['name'], BASE)
        published_filename = manifest['packages'][BASE]['packages'][0]['filename']
        package = current / 'x86_64' / published_filename
        subprocess.run(['gpg', '--homedir', str(self.home), '--verify', str(package) + '.sig', str(package)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        db = current / 'x86_64' / 'lucas-aur.db.tar.gz'
        subprocess.run(['gpg', '--homedir', str(self.home), '--verify', str(db) + '.sig', str(db)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertTrue((current / 'repo-key.asc').is_file())
        second = receive.publish(self.artifact('cockpit-sensors', '1.0-1'), self.state, self.tools)
        self.assertNotEqual(first, second)
        self.assertIn(BASE, json.loads((current / 'manifest.json').read_text())['packages'])
        receive.publish(self.artifact(version='0.28-1'), self.state, self.tools)
        self.assertTrue((current / 'x86_64' / published_filename).is_file())
        self.assertTrue((self.state / 'snapshots' / first).exists())
        before = os.readlink(current)
        with self.assertRaises(receive.Rejected):
            receive.publish(self.artifact(version='0.26-1'), self.state, self.tools)
        self.assertEqual(os.readlink(current), before)
        with self.assertRaises(receive.Rejected):
            receive.publish(transfer([(BASE + '/bad', b'x', tarfile.REGTYPE)]), self.state, self.tools)
        self.assertEqual(os.readlink(current), before)


    def test_recipe_only_rebuild_has_immutable_content_addressed_urls(self):
        receive.publish(self.artifact(), self.state, self.tools)
        first = json.loads((self.state / 'public' / 'manifest.json').read_text())['packages'][BASE]['packages'][0]
        self.assertIn(first['sha256'], first['filename'])
        receive.publish(self.artifact(commit='b' * 40, extra='packager = recipe-rebuild\n'), self.state, self.tools)
        manifest = json.loads((self.state / 'public' / 'manifest.json').read_text())
        second = manifest['packages'][BASE]['packages'][0]
        self.assertEqual(manifest['packages'][BASE]['commit'], 'b' * 40)
        self.assertNotEqual(first['filename'], second['filename'])
        self.assertEqual(first['version'], second['version'])
        self.assertTrue((self.state / 'public' / 'x86_64' / first['filename']).is_file())
        self.assertEqual(receive.digest(self.state / 'public' / 'x86_64' / first['filename']), first['sha256'])

    def test_concurrent_publications_are_serialized(self):
        code = 'import pathlib,sys,receive; receive.publish(sys.stdin.buffer,pathlib.Path(sys.argv[1]),receive.Tools(gpg="gpg",zstd="zstd",gnupg=pathlib.Path(sys.argv[2]),repo_add=[sys.executable,sys.argv[3],sys.argv[1]]))'
        processes = []
        handles = []
        try:
            for base in (BASE, 'cockpit-sensors'):
                input_path = self.state / (base + '.input')
                input_path.write_bytes(self.artifact(base, '1.0-1').getvalue())
                handle = input_path.open('rb'); handles.append(handle)
                processes.append(subprocess.Popen([sys.executable, '-c', code, str(self.state), str(self.home), str(self.indexer)],
                                                  stdin=handle, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                  cwd=Path(receive.__file__).parent))
            for process in processes:
                output, error = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, error.decode())
            manifest = json.loads((self.state / 'public' / 'manifest.json').read_text())
            self.assertEqual(set(manifest['packages']), {BASE, 'cockpit-sensors'})
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill(); process.wait()
            for handle in handles:
                handle.close()

    def test_signing_subkey_is_bound_to_configured_primary_key(self):
        # Real signing-only subkey; public fingerprint remains the primary key.
        subprocess.run(['gpg', '--homedir', str(self.home), '--batch', '--passphrase', '', '--quick-add-key', self.fingerprint, 'ed25519', 'sign', '0'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        path = self.state / 'signed'; path.write_bytes(b'signed by subkey')
        receive.sign(path, self.fingerprint, self.tools)
        receive.verify(path, self.fingerprint, self.tools)

    def test_native_repo_add_integration(self):
        self.tools.repo_add = [shutil.which('repo-add')]
        self.tools.vercmp = shutil.which('vercmp')
        marker = self.state / 'must-not-run'
        receive.publish(self.artifact(extra=f'pkgdesc = $(touch {marker})\n'), self.state, self.tools)
        self.assertFalse(marker.exists())
        database = self.state / 'public' / 'x86_64' / 'lucas-aur.db.tar.gz'
        with tarfile.open(database, 'r:gz') as archive:
            desc = next(member for member in archive if member.name.endswith('/desc'))
            text = archive.extractfile(desc).read().decode()
        self.assertIn('%PGPSIG%', text)
        self.assertIn('%SHA256SUM%', text)
        before = os.readlink(self.state / 'public')
        with self.assertRaises(receive.Rejected):
            receive.publish(self.artifact(version='0.26-1'), self.state, self.tools)
        self.assertEqual(os.readlink(self.state / 'public'), before)

    def test_retention_permissions_and_top_level_public_key(self):
        os.umask(0o077)
        for version in ('1.0-1', '2.0-1', '3.0-1', '4.0-1', '5.0-1'):
            receive.publish(self.artifact(version=version), self.state, self.tools)
        current = self.state / 'public'
        self.assertEqual(len(list((self.state / 'snapshots').iterdir())), 3)
        manifest = json.loads((current / 'manifest.json').read_text())
        self.assertEqual([p['version'] for p in manifest['retained']], ['4.0-1'])
        self.assertEqual(len(list((current / 'x86_64').glob('*.pkg.tar.zst'))), 2)
        self.assertEqual((self.state / 'repo-key.asc').read_bytes(), (current / 'repo-key.asc').read_bytes())
        for path in current.rglob('*'):
            if not path.is_dir():
                self.assertTrue(path.stat().st_mode & 0o004, str(path))
        self.assertTrue((current / 'x86_64').stat().st_mode & 0o001)


    def test_repo_index_with_wrong_hash_is_not_promoted(self):
        receive.publish(self.artifact(), self.state, self.tools)
        before = os.readlink(self.state / 'public')
        bad_indexer = self.state / 'bad-indexer.py'
        bad_indexer.write_text(self.indexer.read_text().replace("package['sha256']", "'0' * 64"))
        self.tools.repo_add = [sys.executable, str(bad_indexer), str(self.state)]
        with self.assertRaises(receive.Rejected):
            receive.publish(self.artifact(version='1.0-1'), self.state, self.tools)
        self.assertEqual(os.readlink(self.state / 'public'), before)

    def test_failures_do_not_promote_and_cleanup_stage(self):
        receive.publish(self.artifact(), self.state, self.tools)
        current = self.state / 'public'
        before = os.readlink(current)
        for command in (['/bin/false'], ['/bin/true'], ['/nonexistent/repo-add']):
            self.tools.repo_add = command
            with self.subTest(command=command), self.assertRaises(receive.Rejected):
                receive.publish(self.artifact(version='1.0-1'), self.state, self.tools)
            self.assertEqual(os.readlink(current), before)
            self.assertEqual(len(list((self.state / 'snapshots').iterdir())), 1)
            self.assertFalse(list(self.state.glob('.intake-*')))

    def test_corrupt_existing_package_is_not_reused(self):
        receive.publish(self.artifact(), self.state, self.tools)
        before = os.readlink(self.state / 'public')
        filename = json.loads((self.state / 'public' / 'manifest.json').read_text())['packages'][BASE]['packages'][0]['filename']
        (self.state / 'public' / 'x86_64' / filename).write_bytes(b'tampered')
        with self.assertRaises(receive.Rejected):
            receive.publish(self.artifact('cockpit-sensors', '1.0-1'), self.state, self.tools)
        self.assertEqual(os.readlink(self.state / 'public'), before)

    def test_alternate_pkginfo_path_cannot_bypass_validation(self):
        package = io.BytesIO()
        with tarfile.open(fileobj=package, mode='w') as archive:
            for name, data in (('./.PKGINFO', b'x[$(touch evil)] = bad\n'), ('.PKGINFO', b'pkgname = i7z\npkgbase = i7z\npkgver = 0.27.2-1\narch = x86_64\n')):
                member = tarfile.TarInfo(name); member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        path = self.state / FILENAME
        path.write_bytes(subprocess.check_output(['zstd', '-q', '-c'], input=package.getvalue()))
        with self.assertRaises(receive.Rejected):
            receive.package_info(path, BASE, '0.27.2-1', self.tools)

    def package_transfer(self, compressed):
        return transfer([(BASE + '/provenance.json', entries()[0][1], tarfile.REGTYPE),
                         (BASE + '/' + FILENAME, compressed, tarfile.REGTYPE)])

    def test_truncated_later_zstd_frame_is_rejected_before_signing(self):
        self.tools.repo_add = [shutil.which('repo-add')]
        receive.publish(self.artifact(), self.state, self.tools)
        before = os.readlink(self.state / 'public')
        package = io.BytesIO()
        with tarfile.open(fileobj=package, mode='w') as archive:
            data = b'pkgname = i7z\npkgbase = i7z\npkgver = 0.27.2-1\narch = x86_64\n'
            member = tarfile.TarInfo('.PKGINFO'); member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
            data = os.urandom(512 * 1024)
            member = tarfile.TarInfo('usr/bin/payload'); member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        raw = package.getvalue()
        split = 262144
        first = subprocess.check_output(['zstd', '-q', '-c'], input=raw[:split])
        second = subprocess.check_output(['zstd', '-q', '-c'], input=raw[split:])
        damaged = first + second[:-32]
        path = self.state / FILENAME; path.write_bytes(damaged)
        self.assertNotEqual(subprocess.run(['zstd', '-qt', str(path)], stderr=subprocess.DEVNULL).returncode, 0)
        self.assertNotEqual(subprocess.run(['bsdtar', '-tf', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode, 0)
        self.assertEqual(subprocess.run(['bsdtar', '-xOqf', str(path), '.PKGINFO'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode, 0)
        # Record actual sign calls without replacing real GPG or repo-add.
        from unittest.mock import patch
        with patch.object(receive, 'sign', wraps=receive.sign) as signing:
            with self.assertRaises(receive.Rejected):
                receive.publish(self.package_transfer(damaged), self.state, self.tools)
            signing.assert_not_called()
        self.assertEqual(os.readlink(self.state / 'public'), before)
        self.assertEqual(len(list((self.state / 'snapshots').iterdir())), 1)
        self.assertFalse(list(self.state.glob('.intake-*')))

    def test_later_duplicate_pkginfo_is_rejected_before_signing(self):
        self.assert_later_pkginfo_rejected('.PKGINFO')

    def test_later_noncanonical_pkginfo_is_rejected_before_signing(self):
        for name in ('./.PKGINFO', 'nested/.PKGINFO', '/.PKGINFO', 'nested/../.PKGINFO'):
            with self.subTest(name=name):
                self.assert_later_pkginfo_rejected(name)

    def assert_later_pkginfo_rejected(self, name):
        if not (self.state / 'public').exists():
            self.tools.repo_add = [shutil.which('repo-add')]
            receive.publish(self.artifact(), self.state, self.tools)
        before = os.readlink(self.state / 'public')
        valid = b'pkgname = i7z\npkgbase = i7z\npkgver = 0.27.2-1\narch = x86_64\n'
        raw = transfer([('.PKGINFO', valid, tarfile.REGTYPE),
                        ('usr/share/payload', b'x' * 262144, tarfile.REGTYPE),
                        (name, valid, tarfile.REGTYPE)]).getvalue()
        compressed = subprocess.check_output(['zstd', '-q', '-c'], input=raw)
        from unittest.mock import patch
        with patch.object(receive, 'sign', wraps=receive.sign) as signing:
            with self.assertRaises(receive.Rejected):
                receive.publish(self.package_transfer(compressed), self.state, self.tools)
            signing.assert_not_called()
        self.assertEqual(os.readlink(self.state / 'public'), before)
        self.assertEqual(len(list((self.state / 'snapshots').iterdir())), 1)

    def test_decoder_checksum_failure_after_complete_tar_is_rejected(self):
        valid = b'pkgname = i7z\npkgbase = i7z\npkgver = 0.27.2-1\narch = x86_64\n'
        raw = transfer([('.PKGINFO', valid, tarfile.REGTYPE)]).getvalue()
        compressed = subprocess.check_output(['zstd', '-q', '--check', '-c'], input=raw)
        empty_frame = subprocess.check_output(['zstd', '-q', '--check', '-c'], input=b'')
        damaged = compressed + empty_frame[:-1] + bytes([empty_frame[-1] ^ 1])
        decoded = subprocess.run(['zstd', '-dc'], input=damaged, capture_output=True)
        self.assertNotEqual(decoded.returncode, 0)
        self.assertEqual(decoded.stdout, raw)  # Tar is complete; only decoder status catches it.
        path = self.state / FILENAME; path.write_bytes(damaged)
        with self.assertRaises(receive.Rejected):
            receive.package_info(path, BASE, '0.27.2-1', self.tools)

    def test_python_libarchive_metadata_disagreement_is_rejected(self):
        valid = b'pkgname = i7z\npkgbase = i7z\npkgver = 0.27.2-1\narch = x86_64\n'
        package = io.BytesIO()
        with tarfile.open(fileobj=package, mode='w', format=tarfile.PAX_FORMAT) as archive:
            member = tarfile.TarInfo('metadata'); member.size = len(valid)
            # Python strips the trailing slash; libarchive interprets it
            # differently and can exit zero despite different extracted bytes.
            member.pax_headers = {'path': '.PKGINFO/'}
            archive.addfile(member, io.BytesIO(valid))
        path = self.state / FILENAME
        path.write_bytes(subprocess.check_output(['zstd', '-q', '-c'], input=package.getvalue()))
        native = subprocess.run(['bsdtar', '-xOf', str(path), '.PKGINFO'], capture_output=True)
        self.assertNotEqual(native.stdout, valid)
        with self.assertRaises(receive.Rejected):
            receive.package_info(path, BASE, '0.27.2-1', self.tools)

    def test_valid_multiframe_large_payload_and_tar_extensions(self):
        self.tools.repo_add = [shutil.which('repo-add')]
        valid = b'pkgname = i7z\npkgbase = i7z\npkgver = 0.27.2-1\narch = x86_64\n'
        for format in (tarfile.PAX_FORMAT, tarfile.GNU_FORMAT):
            with self.subTest(format=format):
                package = io.BytesIO()
                with tarfile.open(fileobj=package, mode='w', format=format) as archive:
                    member = tarfile.TarInfo('.PKGINFO'); member.size = len(valid)
                    if format == tarfile.PAX_FORMAT:
                        member.pax_headers = {'mtime': '1234567890.123', 'SCHILY.xattr.user.test': 'value'}
                    archive.addfile(member, io.BytesIO(valid))
                    member = tarfile.TarInfo('usr/share/' + 'long-name/' * 20 + 'payload')
                    member.size = 17 * 1024**2
                    archive.addfile(member, io.BytesIO(bytes(member.size)))
                    member = tarfile.TarInfo('usr/bin/link'); member.type = tarfile.SYMTYPE
                    member.linkname = '../share/' + 'long-name/' * 20 + 'payload'
                    archive.addfile(member)
                raw = package.getvalue()
                split = 262144
                compressed = (subprocess.check_output(['zstd', '-q', '-c'], input=raw[:split]) +
                              subprocess.check_output(['zstd', '-q', '-c'], input=raw[split:]))
                receive.publish(self.package_transfer(compressed), self.state, self.tools)
                self.assertTrue((self.state / 'public' / 'x86_64' / 'lucas-aur.files').is_file())

    def test_later_damaged_tar_payload_headers_and_trailing_data_are_rejected(self):
        valid = b'pkgname = i7z\npkgbase = i7z\npkgver = 0.27.2-1\narch = x86_64\n'
        raw = transfer([('.PKGINFO', valid, tarfile.REGTYPE),
                        ('usr/share/payload', b'x' * 2048, tarfile.REGTYPE)]).getvalue()
        bad_header = bytearray(raw); bad_header[1024] ^= 1
        for damaged in (raw[:1536 + 100], raw[:3584], bytes(bad_header), raw + b'junk',
                        raw[:-512] + b'junk' + bytes(508)):
            with self.subTest(length=len(damaged)):
                path = self.state / FILENAME
                path.write_bytes(subprocess.check_output(['zstd', '-q', '-c'], input=damaged))
                with self.assertRaises(receive.Rejected):
                    receive.package_info(path, BASE, '0.27.2-1', self.tools)

    def test_later_tar_extension_allocation_is_bounded(self):
        valid = b'pkgname = i7z\npkgbase = i7z\npkgver = 0.27.2-1\narch = x86_64\n'
        prefix = transfer([('.PKGINFO', valid, tarfile.REGTYPE)]).getvalue()[:1024]
        for kind in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
            with self.subTest(kind=kind):
                header = tarfile.TarInfo('extension'); header.type = kind; header.size = 4 * 1024**3
                compressed = subprocess.check_output(['zstd', '-q', '-c'], input=prefix + header.tobuf())
                path = self.state / FILENAME; path.write_bytes(compressed)
                with self.assertRaises(receive.Rejected):
                    receive.package_info(path, BASE, '0.27.2-1', self.tools)

    def test_package_stream_output_and_wall_time_are_bounded(self):
        def consume(reader):
            while reader.read(64 * 1024):
                pass
        path = self.state / 'bomb.zst'
        path.write_bytes(subprocess.check_output(['zstd', '-q', '-c'], input=bytes(256 * 1024)))
        with self.assertRaises(receive.Rejected):
            receive.checked_package_stream(['zstd', '-dc', str(path)], consume, limit=128 * 1024)
        import time
        started = time.monotonic()
        with self.assertRaises(receive.Rejected):
            receive.checked_package_stream([sys.executable, '-c', 'import time; time.sleep(10)'], consume, timeout=0.05)
        self.assertLess(time.monotonic() - started, 2)

    def test_decompression_metadata_bomb_is_bounded(self):
        package = io.BytesIO()
        with tarfile.open(fileobj=package, mode='w') as archive:
            member = tarfile.TarInfo('payload'); member.size = 17 * 1024**2
            archive.addfile(member, io.BytesIO(bytes(member.size)))
        compressed = subprocess.check_output(['zstd', '-q', '-c'], input=package.getvalue())
        path = self.state / FILENAME; path.write_bytes(compressed)
        with self.assertRaises(receive.Rejected):
            receive.package_info(path, BASE, '0.27.2-1', self.tools)

    def test_repo_add_child_file_size_is_bounded(self):
        target = self.state / 'oversized-output'
        with self.assertRaises(receive.Rejected):
            receive.run([sys.executable, '-c', 'import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(bytes(40*1024**2))', str(target)], preexec_fn=receive.index_limits)
        self.assertLessEqual(target.stat().st_size, 32 * 1024**2)

    def test_external_tool_timeout_kills_process_group(self):
        marker = self.state / 'should-not-exist'
        command = [sys.executable, '-c',
                   'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c", "import time,pathlib; time.sleep(0.3); pathlib.Path("+repr(sys.argv[1])+").touch()"]); time.sleep(10)', str(marker)]
        with self.assertRaises(receive.Rejected):
            receive.run(command, timeout=0.05)
        import time
        time.sleep(0.5)
        self.assertFalse(marker.exists())


class CliTests(unittest.TestCase):
    def test_help_is_side_effect_free(self):
        result = subprocess.run([sys.executable, '-I', str(Path(receive.__file__)), '--help'], capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn(b'Restricted AUR', result.stdout)

    def test_original_command_is_rejected_before_reading_input(self):
        for command in ('id', 'publish --anything', 'publish\n', 'scp -t /srv/arch-aur', 'internal-sftp'):
            result = subprocess.run([sys.executable, '-I', str(Path(receive.__file__))],
                                    env={**os.environ, 'SSH_ORIGINAL_COMMAND': command},
                                    input=b'not-tar', capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, b'')
            self.assertIn(b'publication rejected', result.stderr)


if __name__ == '__main__':
    unittest.main()
