import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class PipelineTestCase(unittest.TestCase):
    def module(self):
        path = ROOT / 'scripts/pipeline.py'
        self.assertTrue(path.exists(), 'pipeline implementation missing')
        spec = importlib.util.spec_from_file_location('pipeline', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

class DiscoveryTests(PipelineTestCase):
    def test_change_detection_and_deterministic_noop(self):
        p = self.module()
        current = [{'pkgbase': 'i7z', 'commit': 'a' * 40, 'aur_version': '1-1'},
                   {'pkgbase': 'google-chrome', 'commit': 'b' * 40, 'aur_version': '2-1'}]
        self.assertEqual(p.changed(current, {'packages': {}}), sorted(current, key=lambda x: x['pkgbase']))
        old = {'packages': {x['pkgbase']: dict(x, packages=[]) for x in current}}
        self.assertEqual(p.changed(current, old), [])
        old['packages']['i7z']['commit'] = 'c' * 40
        self.assertEqual(p.changed(current, old), [current[0]])
        old['packages']['i7z']['commit'] = 'a' * 40
        old['packages']['i7z']['aur_version'] = '0-1'
        self.assertEqual(p.changed(current, old), [current[0]])

class ManifestTests(PipelineTestCase):
    def test_http_404_bootstrap_other_errors_fail(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading
        p = self.module()
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                code = int(self.path[1:])
                self.send_response(code)
                self.end_headers()
                self.wfile.write(b'{"packages":{}}')
            def log_message(self, *args):
                pass
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}'
            self.assertEqual(p.load_manifest(url + '/404'), {'packages': {}})
            self.assertEqual(p.load_manifest(url + '/200'), {'packages': {}})
            for code in (403, 429, 500):
                with self.assertRaises(Exception):
                    p.load_manifest(url + f'/{code}')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

class CurrentTests(PipelineTestCase):
    def test_resolves_all_bases_and_records_recipe_commit_and_rpc_version(self):
        p = self.module()
        bases = ['i7z', 'google-chrome']
        calls = []
        def rpc(url):
            calls.append(url)
            return {'type': 'multiinfo', 'results': [
                {'Name': base, 'PackageBase': base, 'Version': '1-1'} for base in bases]}
        rows = p.discover_current(bases, rpc, lambda base: 'a' * 40)
        self.assertEqual(rows, [{'pkgbase': b, 'commit': 'a' * 40, 'aur_version': '1-1'} for b in sorted(bases)])
        self.assertIn('aur.archlinux.org/rpc/v5/info?', calls[0])
        with self.assertRaises(ValueError):
            p.discover_current(bases, lambda url: {'results': []}, lambda base: 'a' * 40)
        with self.assertRaises(ValueError):
            p.discover_current(bases, rpc, lambda base: 'bad-ref')
        self.assertEqual(len(p.PACKAGE_BASES), 10)

class ArtifactTests(PipelineTestCase):
    def test_collect_excludes_debug_and_rejects_symlinks(self):
        p = self.module()
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            source = root / 'source'
            source.mkdir()
            (source / 'i7z-1-1-x86_64.pkg.tar.zst').write_bytes(b'archive-test')
            (source / 'i7z-debug-1-1-x86_64.pkg.tar.zst').write_bytes(b'debug')
            row = {'pkgbase': 'i7z', 'commit': 'a' * 40, 'aur_version': '1-1'}
            p.collect(source, root / 'artifacts', row)
            target = root / 'artifacts/i7z'
            self.assertEqual(sorted(x.name for x in target.iterdir()), ['i7z-1-1-x86_64.pkg.tar.zst', 'provenance.json'])
            self.assertEqual(json.loads((target / 'provenance.json').read_text()), row)
            (source / 'evil-1-1-x86_64.pkg.tar.zst').symlink_to(source / 'i7z-1-1-x86_64.pkg.tar.zst')
            with self.assertRaises(ValueError):
                p.collect(source, root / 'bad', row)

    def test_transfer_contract_and_bootstrap_completeness(self):
        import tarfile
        p = self.module()
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            base = root / 'artifacts/i7z'
            base.mkdir(parents=True)
            row = {'pkgbase': 'i7z', 'commit': 'a' * 40, 'aur_version': '1-1'}
            (base / 'provenance.json').write_text(json.dumps(row))
            (base / 'i7z-1-1-x86_64.pkg.tar.zst').write_bytes(b'archive-test')
            p.pack(root / 'artifacts', root / 'upload.tar', [row], require_all=False)
            with tarfile.open(root / 'upload.tar') as archive:
                self.assertEqual(archive.getnames(), ['i7z/i7z-1-1-x86_64.pkg.tar.zst', 'i7z/provenance.json'])
                self.assertTrue(all(not member.pax_headers for member in archive.getmembers()),
                                'receiver rejects PAX extensions; transfer must be USTAR')
            with self.assertRaises(ValueError):
                p.pack(root / 'artifacts', root / 'bad.tar', [row, dict(row, pkgbase='google-chrome')], require_all=True)
            (base / 'provenance.json').write_text(json.dumps(dict(row, commit='b' * 40)))
            with self.assertRaises(ValueError):
                p.pack(root / 'artifacts', root / 'bad.tar', [row], require_all=False)

class CliTests(PipelineTestCase):
    def test_collect_and_pack_cli(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            source = root / 'source'
            source.mkdir()
            (source / 'i7z-1-1-x86_64.pkg.tar.zst').write_bytes(b'archive-test')
            row = {'pkgbase': 'i7z', 'commit': 'a' * 40, 'aur_version': '1-1'}
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/pipeline.py'), 'collect',
                                     str(source), str(root / 'artifacts'), json.dumps(row)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / 'artifacts/i7z/provenance.json').exists())
            matrix = root / 'matrix.json'
            matrix.write_text(json.dumps({'include': [row]}))
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/pipeline.py'), 'pack',
                                     str(root / 'artifacts'), str(root / 'upload.tar'), str(matrix), '--require-all'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / 'upload.tar').exists())

class VerificationTests(PipelineTestCase):
    def test_readback_checks_commits_versions_and_each_archive_hash(self):
        import hashlib
        p = self.module()
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            base = root / 'i7z'
            base.mkdir()
            row = {'pkgbase': 'i7z', 'commit': 'a' * 40, 'aur_version': '1-1'}
            (base / 'provenance.json').write_text(json.dumps(row))
            filename = 'i7z-1-1-x86_64.pkg.tar.zst'
            (base / filename).write_bytes(b'archive-test')
            digest = hashlib.sha256(b'archive-test').hexdigest()
            published = filename.removesuffix('.pkg.tar.zst') + '.' + digest + '.pkg.tar.zst'
            manifest = {'packages': {'i7z': dict(row, packages=[{'name':'i7z', 'version':'1-1', 'filename':published, 'sha256':digest}])}}
            self.assertEqual(p.verify(root, 'unused', fetch=lambda url: manifest), ['i7z'])
            manifest['packages']['i7z']['packages'][0]['sha256'] = '0' * 64
            with self.assertRaises(ValueError):
                p.verify(root, 'unused', fetch=lambda url: manifest)
            manifest['packages']['i7z']['commit'] = 'b' * 40
            with self.assertRaises(ValueError):
                p.verify(root, 'unused', fetch=lambda url: manifest)

if __name__ == '__main__':
    unittest.main()
